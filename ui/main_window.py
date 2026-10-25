"""BeatSync main window (native Qt — no web page).

    ┌ sidebar ───────────┬ main ───────────────────────────────────┐
    │ BEATSYNC        ⚙  │  before a match: what to do / progress   │
    │ 1 Video            │  after a match:  preview + transport     │
    │ 2 Song             │                  Timing · Beat FX · …    │
    │ 3 Match settings   │                                          │
    │ [ MATCH BEATS ]    │                                          │
    ├────────────────────┴──────────────────────────────────────────┤
    │ DOWNLOAD  status          [Video MP4] [Synced audio] [Full song] │
    └────────────────────────────────────────────────────────────────┘
"""

import copy
import os
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QEvent
from PySide6.QtGui import QPixmap, QIcon, QGuiApplication
from PySide6.QtWidgets import (QMainWindow, QWidget, QFrame, QLabel, QLineEdit, QSlider, QProgressBar,
                               QStackedWidget, QScrollArea, QGridLayout, QHBoxLayout, QVBoxLayout, QFileDialog,
                               QMessageBox, QSizePolicy, QApplication, QAbstractSlider, QAbstractSpinBox,
                               QAbstractButton, QButtonGroup, QPushButton, QDoubleSpinBox)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices

from .player import VideoPlayer, FullscreenPlayer

from . import theme as T
from . import files
from .engine import Engine, run_bg
from .widgets import (Card, GradientTitle, Toggle, Segmented, FlowLayout, Spinner, Leds, Toast, ElidedLabel,
                      DoubleClickLabel, label, caps, button, hbox, vbox, set_prop)

# ── Static data (same values as the engine expects) ─────────────────────────
EFFECTS = [
    ('punch', '\U0001F4A5', 'Punch zoom'), ('zoom_pulse', '\U0001F50D', 'Zoom pulse'), ('shake', '〰', 'Shake'),
    ('rotation', '⟳', 'Rotate kick'), ('mirror', '⇆', 'Mirror flip'), ('flash', '⚡', 'Flash'),
    ('strobe', '◼', 'Strobe'), ('invert_flash', '◐', 'Invert'), ('color_pop', '✨', 'Color pop'),
    ('color_shift', '\U0001F308', 'Hue shift'), ('rgb_split', '▦', 'RGB split'), ('glitch', '\U0001F4FA', 'Glitch'),
    ('blur_pulse', '\U0001F4A8', 'Blur pulse'), ('vignette', '◉', 'Vignette'), ('scanlines', '☰', 'Scanlines'),
]
# slider units → engine values
FILTERS = [
    ('Light', [
        ('brightness', 'Brightness', -50, 50, 0, lambda v: v / 100),
        ('contrast', 'Contrast', 50, 200, 100, lambda v: v / 100),
        ('gamma', 'Gamma', 40, 250, 100, lambda v: v / 100),
    ]),
    ('Color', [
        ('saturation', 'Saturation', 0, 300, 100, lambda v: v / 100),
        ('warmth', 'Warmth', -100, 100, 0, lambda v: v / 100),
        ('tint', 'Tint', -100, 100, 0, lambda v: v / 100),
        ('hue', 'Hue', -180, 180, 0, lambda v: v),
        ('splittone', 'Teal/Orange', 0, 100, 0, lambda v: v / 100),
    ]),
    ('Texture', [
        ('fade', 'Film fade', 0, 100, 0, lambda v: v / 100),
        ('sharpness', 'Sharpen', 0, 100, 0, lambda v: v / 100 * 3),
        ('denoise', 'Denoise', 0, 100, 0, lambda v: v / 100),
        ('vignette', 'Vignette', 0, 100, 0, lambda v: v / 100),
        ('grain', 'Grain', 0, 100, 0, lambda v: v / 100),
    ]),
]
FDEF = {f[0]: f[4] for _, lst in FILTERS for f in lst}
FMAP = {f[0]: f[5] for _, lst in FILTERS for f in lst}
PRESETS = [
    ('natural', 'Natural', {}),
    ('warm', 'Warm', {'warmth': 30, 'saturation': 115, 'contrast': 108, 'brightness': 3}),
    ('cool', 'Cool', {'warmth': -32, 'tint': -5, 'contrast': 106}),
    ('vivid', 'Vivid', {'saturation': 170, 'contrast': 115, 'sharpness': 15}),
    ('punchy', 'High contrast', {'contrast': 145, 'saturation': 110, 'brightness': 3, 'sharpness': 10}),
    ('cinema', 'Cinema', {'contrast': 120, 'saturation': 90, 'splittone': 55, 'vignette': 35, 'gamma': 95}),
    ('teal', 'Teal & orange', {'splittone': 90, 'saturation': 120, 'contrast': 112}),
    ('vintage', 'Vintage', {'warmth': 35, 'fade': 60, 'saturation': 80, 'grain': 30, 'vignette': 40, 'contrast': 95}),
    ('film', 'Film', {'fade': 35, 'grain': 25, 'contrast': 110, 'warmth': 10, 'vignette': 25}),
    ('faded', 'Faded', {'fade': 75, 'saturation': 75, 'contrast': 90, 'brightness': 3}),
    ('noir', 'Noir', {'saturation': 0, 'contrast': 140, 'gamma': 90, 'grain': 35, 'vignette': 55}),
    ('bw', 'B&W', {'saturation': 0, 'contrast': 110}),
    ('cyber', 'Cyberpunk', {'tint': 45, 'hue': -15, 'saturation': 145, 'contrast': 120, 'splittone': 30}),
    ('glow', 'Soft glow', {'brightness': 8, 'contrast': 92, 'saturation': 110, 'gamma': 110, 'denoise': 30}),
    ('sharp', 'Sharp', {'sharpness': 70, 'contrast': 105}),
]
UNIT_BEATS = {'bar': None, 'beat': 1, 'half': 0.5, 'quarter': 0.25, 'eighth': 0.125}
UNIT_NAME = {'bar': 'bar', 'beat': 'beat', 'half': '½ beat', 'quarter': '¼ beat', 'eighth': '⅛ beat'}
UNIT_STEP = {'bar': '1 bar', 'beat': '1 beat', 'half': '½ beat', 'quarter': '¼ beat', 'eighth': '⅛ beat'}
UNITS = ['bar', 'beat', 'half', 'quarter', 'eighth']
DL_KINDS = [('video', 'Video MP4', 'dl', 'The synced video'),
            ('synced', 'Synced audio', 'dl2', 'The exact soundtrack of the video (offset, mix and fade applied)'),
            ('full', 'Full song', 'dl2', 'The whole song, tempo-matched')]
KEYS_HELP = ('←  →   nudge one step\nShift + ←  →   ±10 ms (Grid lock off)\n1 – 5   step size\n'
             'L   Grid lock     R   reset to auto\nSpace   play / pause\nCtrl + Enter   match\n'
             'Double-click the video   full screen (Esc to leave)')


def default_opts():
    return {'song_vol': 1.0, 'hflip': False, 'filters': {k: FMAP[k](FDEF[k]) for k in FDEF}, 'effects': [],
            'fx_division': 'beat', 'fx_intensity': 1.0, 'aspect': 'original', 'fade_out': 0,
            'quality': 'balanced', 'preview_sec': 15, 'speed': 1.0}


def fmt_time(sec, dec=False):
    neg = sec < 0
    sec = abs(sec)
    m = int(sec // 60)
    s = sec - m * 60
    body = f'{s:04.1f}' if dec else f'{int(s):02d}'
    return ('-' if neg else '') + f'{m}:{body}'


def parse_time(v):
    v = (v or '').strip()
    if not v:
        return None
    try:
        if ':' in v:
            m, s = v.split(':', 1)
            return (int(m or 0)) * 60 + float(s or 0)
        return float(v)
    except ValueError:
        return None


def frac_beats(b):
    sign = '+' if b > 0 else '−' if b < 0 else ''
    a = abs(b)
    whole = int(a + 1e-6)
    f = a - whole
    for v, g in ((0, ''), (0.125, '⅛'), (0.25, '¼'), (0.375, '⅜'), (0.5, '½'), (0.625, '⅝'), (0.75, '¾'),
                 (0.875, '⅞'), (1, '')):
        if abs(f - v) < 0.01:
            num = str(whole + (1 if v == 1 else 0)) if (whole or not g) else ''
            return sign + num + g
    return sign + f'{a:.2f}'


def is_url(s):
    return s.startswith(('http://', 'https://')) and '.' in s


class ModeButton(QPushButton):
    """Match Video / Match Song choice: a title and one line of explanation."""

    def __init__(self, title, desc):
        super().__init__()
        self.setProperty('kind', 'mode')
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.t = QLabel(title.upper())
        self.t.setFont(T.display_font(16, 1.5))
        self.d = label(desc, muted=True, px=10)
        for w in (self.t, self.d):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = vbox(self.t, self.d, spacing=0, margins=(9, 5, 9, 6))
        self.setLayout(lay)
        self.setMinimumHeight(46)
        self.toggled.connect(self._tone)
        self._tone(False)

    def _tone(self, on):
        self.t.setStyleSheet(f'color: {T.ACCENT if on else T.ACCENT2};')


class SourceBox(Card):
    """Video or song: a link or a local file, plus an optional trim."""
    picked = Signal(str)                 # path chosen in the dialog or dropped

    def __init__(self, win, src):
        is_vid = src == 'vid'
        super().__init__('Video' if is_vid else 'Song', step=1 if is_vid else 2,
                         dot=T.ACCENT if is_vid else T.ACCENT2, padding=(10, 8, 10, 8), spacing=6)
        self.win, self.src = win, src
        self.mode = 'url'
        self.file_id = None
        self.seg = Segmented([('url', 'Link'), ('file', 'File')], 'url', stretch=True)
        self.seg.changed.connect(self.set_mode)
        self.add(self.seg)

        self.stack = QStackedWidget()
        # link
        self.url = QLineEdit()
        self.url.setPlaceholderText('YouTube, TikTok, X, Instagram…' if is_vid else 'YouTube, SoundCloud, any link…')
        self.fetch = button('Fetch', tip='Look up the title and length')
        self.fetch.clicked.connect(self.fetch_info)
        self.url.returnPressed.connect(self.fetch_info)
        self.url.textEdited.connect(self._maybe_fetch)
        self.thumb = QLabel()
        self.thumb.setFixedSize(52, 30)
        self.thumb.setStyleSheet(f'background: {T.BG}; border-radius: 3px;')
        self.title = ElidedLabel()
        self.meta = label('', muted=True, px=10)
        self.info_row = QWidget()
        self.info_row.setLayout(hbox(self.thumb, vbox(self.title, self.meta, spacing=1), spacing=8))
        self.info_row.hide()
        url_page = QWidget()
        url_page.setLayout(vbox(hbox(self.url, self.fetch, spacing=6), self.info_row, spacing=6))
        # file
        self.drop = button('Choose a file  ·  or drop it here', kind='drop')
        self.drop.setAcceptDrops(False)
        self.drop.clicked.connect(self.choose_file)
        file_page = QWidget()
        file_page.setLayout(vbox(self.drop))
        self.stack.addWidget(url_page)
        self.stack.addWidget(file_page)
        self.stack.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.add(self.stack)

        # trim
        self.trim_btn = button('Trim  ▸', tip='Use only part of the ' + ('video' if is_vid else 'song'))
        self.trim_btn.setStyleSheet(f'border: none; padding: 0; color: {T.MUTED}; font-size: 10px;')
        self.trim_btn.clicked.connect(self.toggle_trim)
        self.t_start, self.t_end = QLineEdit(), QLineEdit()
        for e, ph in ((self.t_start, '0:00'), (self.t_end, 'end')):
            e.setPlaceholderText(ph)
            e.setFixedWidth(62)
        self.trim_row = QWidget()
        self.trim_row.setLayout(hbox(self.t_start, label('→', muted=True), self.t_end,
                                     label('1:30 or 90', dim=True), None, spacing=6))
        self.trim_row.hide()
        self.badge = label('', tone='info', px=10)
        self.badge.hide()
        self.add(hbox(self.trim_btn, None, self.badge))
        self.add(self.trim_row)

    # ── behaviour ─────────────────────────────────────────────────────────
    def set_mode(self, mode):
        self.mode = mode
        self.seg.set_value(mode)
        self.stack.setCurrentIndex(0 if mode == 'url' else 1)

    def toggle_trim(self):
        show = self.trim_row.isHidden()
        self.trim_row.setVisible(show)
        self.trim_btn.setText('Trim  ▾' if show else 'Trim  ▸')

    def _maybe_fetch(self, text):
        # a pasted link is looked up straight away
        if is_url(text.strip()) and len(text) > 12 and not getattr(self, '_last_fetch', '') == text.strip():
            QTimer.singleShot(250, self.fetch_info)

    def fetch_info(self):
        u = self.url.text().strip()
        if not is_url(u) or not self.fetch.isEnabled():
            return
        self._last_fetch = u
        self.fetch.setEnabled(False)
        self.fetch.setText('…')

        def done(d):
            self.fetch.setEnabled(True)
            self.fetch.setText('Fetch')
            self.title.set_text(d.get('title') or 'Unknown')
            dur = d.get('duration') or 0
            self.meta.setText((d.get('platform') or 'Web') + (f'  ·  {fmt_time(dur)}' if dur else ''))
            self.thumb.clear()
            self.info_row.show()
            if d.get('thumbnail'):
                run_bg(lambda: urllib.request.urlopen(d['thumbnail'], timeout=8).read(), ok=self._set_thumb,
                       err=lambda e: None)

        def fail(e):
            self.fetch.setEnabled(True)
            self.fetch.setText('Fetch')
            self.win.show_error(str(e))
        run_bg(lambda: self.win.engine.api('/api/info', {'url': u}), ok=done, err=fail)

    def _set_thumb(self, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self.thumb.setPixmap(pm.scaled(52, 30, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                                 .copy(0, 0, 52, 30))

    def choose_file(self):
        exts = self.win.exts_video | self.win.exts_audio if self.src == 'vid' else self.win.exts_audio
        pat = ' '.join('*' + e for e in sorted(exts))
        kind = 'Video or audio' if self.src == 'vid' else 'Audio'
        path, _ = QFileDialog.getOpenFileName(self, 'Choose a ' + ('video' if self.src == 'vid' else 'song'),
                                              str(Path.home()), f'{kind} ({pat});;All files (*.*)')
        if path:
            self.use_file(path)

    def use_file(self, path):
        try:
            r = self.win.server.register_local(path)
        except Exception as e:                       # noqa: BLE001
            self.file_id = None
            self.win.show_error(str(e))
            return False
        self.file_id = r['file_id']
        self.set_mode('file')
        self.drop.setText(f'✓ {r["name"]}   ({r["size"] / 1048576:.1f} MB)')
        self.drop.setToolTip(path + '\nClick to choose another file')
        set_prop(self.drop, 'has', True)
        self.win.hide_error()
        return True

    def set_drop_hover(self, on):
        set_prop(self.drop, 'over', bool(on))

    def reset(self):
        self.url.clear()
        self.info_row.hide()
        self.badge.hide()
        self.t_start.clear()
        self.t_end.clear()
        self.trim_row.hide()
        self.trim_btn.setText('Trim  ▸')
        self.file_id = None
        self.drop.setText('Choose a file  ·  or drop it here')
        self.drop.setToolTip('')
        set_prop(self.drop, 'has', False)


class MainWindow(QMainWindow):
    raise_requested = Signal()

    def __init__(self, server, ctx):
        super().__init__()
        self.server = server
        self.ctx = ctx
        self.engine = Engine(server)
        self.engine.event.connect(self.on_event)
        self.exts_video = set(server.ALLOWED_VIDEO)
        self.exts_audio = set(server.ALLOWED_AUDIO)

        # state (mirrors the job lifecycle in the engine)
        self.root_job = self.current_job = self.done_job = self.last_good_job = self.preview_for = None
        self.timing = self.shown_timing = None
        self.timing_by_job = {}
        self.phase = 'idle'                      # idle | analysing | studio
        self.pending = None
        self.bpm_mode = 'video'
        self.preserve_pitch = True
        self.unit, self.lock = 'beat', True
        self.opts = default_opts()
        self.filter_ui = dict(FDEF)
        self.saving = False
        self.last_saved = None
        self.tools_data = None
        self.app_update = None
        self._t0 = 0
        self._seek_drag = False
        self._pending_load = None
        self._last_beat = None

        self.setWindowTitle(ctx.name)
        self.setAcceptDrops(True)
        self._make_player()
        self._build()
        self._wire_player()

        self.flush_timer = QTimer(self)
        self.flush_timer.setSingleShot(True)
        self.flush_timer.timeout.connect(self.flush)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick_clock)
        self.led_timer = QTimer(self)
        self.led_timer.timeout.connect(self._led_loop)
        self.led_timer.start(33)
        self.keepalive = QTimer(self)                 # sessions never expire while the window is open
        self.keepalive.timeout.connect(lambda: self.engine.touch(self.current_job or self.root_job))
        self.keepalive.start(60_000)
        self.raise_requested.connect(self._bring_to_front)
        QApplication.instance().installEventFilter(self)

        QTimer.singleShot(0, self._startup)

    # ════════════════════════════════════════════════════════════════════
    # Layout
    # ════════════════════════════════════════════════════════════════════
    def _build(self):
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # update banner
        self.banner = QFrame()
        self.banner.setObjectName('banner')
        self.banner_text = label('', tone='info', px=11)
        self.banner_btn = button('Update now')
        self.banner_btn.clicked.connect(self._banner_action)
        self.banner.setLayout(hbox(self.banner_text, None, self.banner_btn, margins=(14, 6, 10, 6)))
        self.banner.hide()
        outer.addWidget(self.banner)

        body = QHBoxLayout()
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(10)
        outer.addLayout(body, 1)
        body.addWidget(self._build_sidebar())
        self.main = QStackedWidget()
        self.main.addWidget(self._build_start_page())
        self.main.addWidget(self._build_studio())
        body.addWidget(self.main, 1)

        outer.addWidget(self._build_footer())
        self.footer.hide()
        self.toast = Toast(root)

    # ── sidebar ───────────────────────────────────────────────────────────
    def _build_sidebar(self):
        side = QWidget()
        lay = vbox(spacing=7)
        side.setLayout(lay)

        self.gear = button('⚙  Settings', kind='gear', tip='Tools, YouTube sign-in, storage, updates')
        self.gear.clicked.connect(self.open_settings)
        lay.addLayout(hbox(GradientTitle('BEATSYNC', 30), None, self.gear))

        self.vid = SourceBox(self, 'vid')
        self.song = SourceBox(self, 'song')
        lay.addWidget(self.vid)
        lay.addWidget(self.song)

        card = Card('Match settings', step=3, dot='#2d6bff', padding=(10, 8, 10, 9), spacing=6)
        self.mode_vid = ModeButton('Match video', 'stretch the song')
        self.mode_song = ModeButton('Match song', 'speed the video')
        self.mode_vid.setToolTip("Stretch the song to the video's tempo. The video is untouched.")
        self.mode_song.setToolTip("Keep the song's tempo. The video plays a little faster or slower instead.")
        grp = QButtonGroup(self)
        for b in (self.mode_vid, self.mode_song):
            grp.addButton(b)
        self.mode_vid.setChecked(True)
        self.mode_vid.clicked.connect(lambda: self.set_bpm_mode('video'))
        self.mode_song.clicked.connect(lambda: self.set_bpm_mode('song'))
        card.add(hbox(self.mode_vid, self.mode_song, spacing=6))

        self.mix = QSlider(Qt.Horizontal)
        self.mix.setRange(0, 100)
        self.mix.setValue(100)
        self.mix.setToolTip('Original video audio ↔ new song')
        self.mix_val = label('100%', obj='val')
        self.mix_val.setFixedWidth(36)
        self.mix_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mix.valueChanged.connect(self.on_mix)
        card.add(hbox(caps('Mix'), 6, label('Orig', muted=True, px=10), self.mix, label('Song', muted=True, px=10),
                      self.mix_val, spacing=6))
        self.pitch = Toggle('Pitch locked', True, small=True)
        self.pitch.setToolTip('On: stretching keeps the key. Off: pitch follows speed, like a turntable.')
        self.pitch.toggled.connect(self.on_pitch)
        self.pitch_note = label('', dim=True, wrap=True, px=10)
        self.pitch_note.hide()
        card.add(self.pitch)
        card.add(self.pitch_note)
        lay.addWidget(card)

        self.errbox = QFrame()
        self.errbox.setObjectName('errbox')
        self.err_text = label('', tone='err', wrap=True, px=11)
        self.err_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        x = button('×')
        x.setStyleSheet(f'border: none; color: {T.ACCENT}; font-size: 15px; padding: 0 4px;')
        x.clicked.connect(self.hide_error)
        self.errbox.setLayout(hbox(self.err_text, x, margins=(10, 8, 6, 8)))
        self.errbox.hide()
        lay.addWidget(self.errbox)

        self.run_btn = button('MATCH BEATS', kind='primary', tip='Ctrl + Enter')
        self.run_btn.setMinimumHeight(40)
        self.run_btn.clicked.connect(self.run)
        lay.addWidget(self.run_btn)
        lay.addStretch(1)
        self.caps_line = label('', dim=True, wrap=True, px=10)
        lay.addWidget(self.caps_line)

        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(300)
        side.setFixedWidth(290)
        return scroll

    # ── before matching: what to do / progress ────────────────────────────
    def _build_start_page(self):
        page = QWidget()
        page.setObjectName('page')
        lay = vbox(spacing=12)
        page.setLayout(lay)

        self.empty = QWidget()
        h = QLabel('ALIGN ANY SONG TO ANY VIDEO')
        h.setFont(T.display_font(34, 2.5))
        h.setStyleSheet(f'color: {T.BORDER2};')
        h.setAlignment(Qt.AlignCenter)
        s = label('Add a video and a song on the left — links or files — then press Match Beats.\n'
                  'You can also drop both files anywhere in this window.', dim=True)
        s.setAlignment(Qt.AlignCenter)
        self.empty.setLayout(vbox(None, h, s, None, spacing=10))

        self.progress = Card(padding=(16, 14, 16, 16), spacing=12)
        self.prog_spin = Spinner(12)
        self.prog_text = ElidedLabel('Starting…')
        self.prog_timer = label('0:00', muted=True, px=11)
        self.progress.add(hbox(self.prog_spin, self.prog_text, self.prog_timer, spacing=10))
        self.prog_bar = QProgressBar()
        self.prog_bar.setRange(0, 100)
        self.prog_bar.setTextVisible(False)
        self.progress.add(self.prog_bar)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.stat = {}
        for i, (k, name) in enumerate((('vid', 'Video BPM'), ('song', 'Song BPM'), ('stretch', 'Stretch'),
                                       ('offset', 'Offset'), ('meter', 'Beats / bar'))):
            tile = QFrame()
            tile.setObjectName('tile')
            v = QLabel('—')
            v.setFont(T.display_font(24, 1.5))
            v.setStyleSheet(f'color: {T.ACCENT2};')
            v.setAlignment(Qt.AlignCenter)
            n = caps(name, 9, spacing=1.2)
            n.setAlignment(Qt.AlignCenter)
            dot = None
            if k in ('vid', 'song'):
                dot = QLabel()
                dot.setFixedSize(6, 6)
                dot.setStyleSheet(f'background: {T.DIM}; border-radius: 3px;')
                tile.setLayout(vbox(v, hbox(None, n, dot, None, spacing=4), spacing=2, margins=(6, 8, 6, 8)))
            else:
                tile.setLayout(vbox(v, n, spacing=2, margins=(6, 8, 6, 8)))
            self.stat[k] = (v, dot)
            grid.addWidget(tile, 0, i)
            grid.setColumnStretch(i, 1)
        self.progress.add(grid)
        self.warnbox = QFrame()
        self.warnbox.setObjectName('warnbox')
        self.warn_text = label('', tone='warn', wrap=True, px=11)
        self.warnbox.setLayout(hbox(self.warn_text, margins=(10, 7, 10, 7)))
        self.warnbox.hide()
        self.progress.add(self.warnbox)
        self.progress.hide()

        lay.addWidget(self.empty, 1)
        lay.addWidget(self.progress)
        lay.addStretch(0)
        return page

    # ── after matching: preview + controls ────────────────────────────────
    def _build_studio(self):
        page = QWidget()
        page.setObjectName('page')
        lay = vbox(spacing=10)
        page.setLayout(lay)

        # preview
        self.render_spin = Spinner(9)
        self.render_text = label('', muted=True, px=10)
        self.stats_line = ElidedLabel('')
        self.stats_line.setStyleSheet(f'color: {T.DIM}; font-size: 10px;')
        self.stats_line.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pv = Card(padding=(10, 8, 10, 8), spacing=6)
        self.leds = Leds()
        self.beatpos = label('Bar – · Beat –', muted=True, px=10)
        self.beatpos.setMinimumWidth(96)
        self.loop = Toggle('Loop', True, small=True)
        self.keep_pos = Toggle('Keep spot', True, small=True)
        self.keep_pos.setToolTip('After a change, keep playing from the same moment')
        head = hbox(caps('Preview'), 10, self.leds, self.beatpos, 6, self.render_spin, self.render_text, spacing=6)
        head.addWidget(self.stats_line, 1)
        head.addSpacing(6)
        head.addWidget(self.loop)
        head.addWidget(self.keep_pos)
        pv.add(head)
        self.warn_line = ElidedLabel('')
        self.warn_line.setStyleSheet(f'color: {T.WARN}; font-size: 10px;')
        self.warn_line.hide()
        pv.add(self.warn_line)
        self.video = VideoPlayer(self.player)
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video.fullscreen_toggled.connect(self.toggle_fullscreen)
        pv.add(self.video, 1)
        lay.addWidget(pv, 10)

        # controls
        ctl = Card(padding=(10, 4, 10, 8), spacing=6)
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(2)
        self.tab_btns = {}
        tab_grp = QButtonGroup(self)
        self.panes = QStackedWidget()
        for i, (key, name, builder) in enumerate((('timing', 'Timing', self._pane_timing),
                                                 ('speed', 'Speed', self._pane_speed),
                                                 ('fx', 'Beat FX', self._pane_fx),
                                                 ('color', 'Color', self._pane_color),
                                                 ('frame', 'Frame & fade', self._pane_frame),
                                                 ('export', 'Export', self._pane_export))):
            b = button(name, kind='tab', checkable=True)
            tab_grp.addButton(b)
            tabs_row.addWidget(b)
            self.tab_btns[key] = b
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            inner.setObjectName('page')
            inner.setStyleSheet(f'QWidget#page {{ background: {T.SURFACE}; }}')
            inner.setLayout(builder())
            sc.setWidget(inner)
            self.panes.addWidget(sc)
            b.clicked.connect(lambda _=False, i=i: self.panes.setCurrentIndex(i))
        self.tab_btns['timing'].setChecked(True)
        tabs_row.addStretch(1)
        keys = label('Keys', dim=True, px=10)
        keys.setToolTip(KEYS_HELP)
        keys.setCursor(Qt.WhatsThisCursor)
        tabs_row.addWidget(keys)
        ctl.add(tabs_row)
        sep = QFrame()
        sep.setObjectName('sep')
        ctl.add(sep)
        ctl.add(self.panes, 1)
        self.status_spin = Spinner(8)
        self.status = ElidedLabel('')
        self.status.setStyleSheet(f'color: {T.MUTED}; font-size: 10px;')
        ctl.add(hbox(self.status_spin, self.status, spacing=6))
        lay.addWidget(ctl, 10)
        return page

    def _row(self, name, *items, spacing=8):
        lab = caps(name)
        lab.setFixedWidth(78)
        return hbox(lab, *items, spacing=spacing)

    def _pane_timing(self):
        lay = vbox(spacing=8, margins=(0, 8, 4, 2))
        self.unit_seg = Segmented([('bar', 'Bar'), ('beat', 'Beat'), ('half', '½'), ('quarter', '¼'), ('eighth', '⅛')],
                                  'beat')
        self.unit_seg.changed.connect(self.set_unit)
        self.lock_t = Toggle('Grid lock', True, small=True)
        self.lock_t.setToolTip('Keep every nudge on the beat grid. Turn off for millisecond fine-tuning.')
        self.lock_t.clicked.connect(self.toggle_lock)
        self.reset_btn = button('Reset', tip='Back to the automatic match (R)')
        self.reset_btn.clicked.connect(lambda: self.schedule_adjust(reset=True))
        self.ro_main = QLabel('AUTO')
        self.ro_main.setFont(T.display_font(22, 1.5))
        self.ro_main.setStyleSheet(f'color: {T.ACCENT2};')
        self.ro_main.setToolTip('How far the song is moved from the automatic match')
        lay.addLayout(self._row('Step', self.unit_seg, 4, self.lock_t, None, self.ro_main, 4, self.reset_btn))
        self.n_back = button('« −1 beat', kind='nudge', tip='← key')
        self.n_fine_back = button('‹ −10 ms', kind='nudge')
        self.n_fine_fwd = button('+10 ms ›', kind='nudge')
        self.n_fwd = button('+1 beat »', kind='nudge', tip='→ key')
        for b in (self.n_back, self.n_fwd):
            b.setProperty('big', True)
        self.n_back.clicked.connect(lambda: self.schedule_adjust(beats=-self.unit_beats()))
        self.n_fwd.clicked.connect(lambda: self.schedule_adjust(beats=self.unit_beats()))
        self.n_fine_back.clicked.connect(lambda: self.schedule_adjust(ms=-10))
        self.n_fine_fwd.clicked.connect(lambda: self.schedule_adjust(ms=10))
        g = QGridLayout()
        g.setSpacing(6)
        for i, b in enumerate((self.n_back, self.n_fine_back, self.n_fine_fwd, self.n_fwd)):
            g.addWidget(b, 0, i)
        lay.addLayout(g)
        self.ro_sub = ElidedLabel('—')
        self.ro_sub.setStyleSheet(f'color: {T.MUTED}; font-size: 10px;')
        lay.addWidget(self.ro_sub)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 3000)
        self.seek.setToolTip('Where the song starts. Jump moves in whole bars so downbeats stay aligned.')
        self.seek_val = label('0:00.0', obj='val')
        self.seek_val.setFixedWidth(48)
        self.seek_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.seek.valueChanged.connect(lambda v: self.seek_val.setText(fmt_time(v / 10, True)))
        go = button('Jump')
        go.clicked.connect(lambda: self.schedule_adjust(seek_to=self.seek.value() / 10))
        lay.addLayout(self._row('Song start', self.seek, self.seek_val, go))
        lay.addStretch(1)
        self.update_unit_labels()
        return lay

    def _pane_speed(self):
        lay = vbox(spacing=10, margins=(0, 10, 4, 2))
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(25, 400)
        self.speed_slider.setValue(100)
        self.speed_slider.setToolTip('Drag, or type any value in the box (0.25× – 4×)')
        self.speed_box = QDoubleSpinBox()
        self.speed_box.setRange(0.25, 4.0)
        self.speed_box.setDecimals(2)
        self.speed_box.setSingleStep(0.05)
        self.speed_box.setValue(1.0)
        self.speed_box.setSuffix(' ×')
        self.speed_box.setKeyboardTracking(False)          # apply on Enter / leaving the box, not per digit
        self.speed_box.setFixedWidth(84)
        self.speed_box.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.speed_box.setToolTip('Any speed: 0.25 = four times slower, 2 = twice as fast')
        self.speed_slider.valueChanged.connect(lambda v: self.set_speed(v / 100, source='slider'))
        self.speed_box.valueChanged.connect(lambda v: self.set_speed(v, source='box'))
        reset = button('1×', tip='Normal speed')
        reset.clicked.connect(lambda: self.set_speed(1.0))
        lay.addLayout(self._row('Speed', self.speed_slider, self.speed_box, reset))
        self.pitch2 = Toggle('Pitch locked', True, small=True)
        self.pitch2.setToolTip('On: the song keeps its key at any speed.\n'
                               'Off: pitch follows speed, like a turntable (faster = higher).')
        self.pitch2.toggled.connect(self.on_pitch)
        self.pitch2_note = label('the song keeps its key', dim=True, px=10)
        lay.addLayout(self._row('Pitch', self.pitch2, self.pitch2_note, None))
        self.speed_info = label('', muted=True, px=11)
        lay.addWidget(self.speed_info)
        lay.addWidget(label('Changes the exported video together with its sound (and the Synced audio and Full song '
                            'downloads). The 1× button on the player only changes how the preview plays.',
                            dim=True, wrap=True, px=10))
        lay.addStretch(1)
        return lay

    def set_speed(self, v, source=None):
        v = round(max(0.25, min(4.0, float(v))), 2)
        if source != 'slider':
            self.speed_slider.blockSignals(True)
            self.speed_slider.setValue(int(round(v * 100)))
            self.speed_slider.blockSignals(False)
        if source != 'box':
            self.speed_box.blockSignals(True)
            self.speed_box.setValue(v)
            self.speed_box.blockSignals(False)
        self.tab_btns['speed'].setText('Speed' if v == 1.0 else f'Speed  {v:g}×')
        if abs(self.opts.get('speed', 1.0) - v) > 1e-6:
            self.opts['speed'] = v
            self._speed_info()
            self.schedule_adjust(opts=True)

    def _speed_info(self):
        sp = self.opts.get('speed', 1.0)
        j = self.engine.job(self.current_job) if self.current_job else None
        st = j.get('state') if j else None
        t = self.timing
        if not st or not t:
            self.speed_info.setText('')
            return
        base = st['vid_info']['dur'] / (st.get('video_speed') or 1.0)
        bpm = t['target_bpm'] / (t.get('speed') or 1.0)
        if abs(sp - 1.0) < 1e-6:
            self.speed_info.setText(f'Normal speed  ·  {fmt_time(base)} long  ·  {bpm:.1f} BPM')
        else:
            word = 'faster' if sp > 1 else 'slower'
            self.speed_info.setText(f'{sp:g}× {word}  ·  {fmt_time(base)} → {fmt_time(base / sp)}  ·  '
                                    f'{bpm:.1f} → {bpm * sp:.1f} BPM')

    def _pane_fx(self):
        lay = vbox(spacing=8, margins=(0, 8, 4, 2))
        self.div_seg = Segmented([('bar', 'Bar'), ('beat', 'Beat'), ('half', '½ beat'), ('quarter', '¼ beat')],
                                 'beat', tone='orange')
        self.div_seg.changed.connect(self.on_div)
        self.fx_int = QSlider(Qt.Horizontal)
        self.fx_int.setRange(25, 200)
        self.fx_int.setSingleStep(5)
        self.fx_int.setValue(100)
        self.fx_int_val = label('100%', obj='val')
        self.fx_int_val.setFixedWidth(38)
        self.fx_int.valueChanged.connect(self.on_fx_int)
        clear = button('Clear', tip='Turn all effects off')
        clear.clicked.connect(self.clear_fx)
        lay.addLayout(hbox(caps('Trigger'), self.div_seg, 10, caps('Amount'), self.fx_int, self.fx_int_val, clear,
                           spacing=8))
        g = QGridLayout()
        g.setSpacing(6)
        self.fx_btns = {}
        for i, (key, icon, name) in enumerate(EFFECTS):
            b = button(f'{icon}\n{name}', kind='fx', checkable=True)
            b.setMinimumHeight(42)
            b.clicked.connect(lambda _=False, k=key: self.toggle_fx(k))
            self.fx_btns[key] = b
            g.addWidget(b, i // 5, i % 5)
        lay.addLayout(g)
        lay.addWidget(label("Effects hit on the song's kick drum and are exported exactly as previewed.",
                            dim=True, wrap=True, px=10))
        lay.addStretch(1)
        return lay

    def _pane_color(self):
        lay = vbox(spacing=8, margins=(0, 8, 4, 2))
        flow = FlowLayout(6)
        self.preset_btns = {}
        grp = QButtonGroup(self)
        for key, name, _ in PRESETS:
            b = button(name, kind='chip', checkable=True)
            grp.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self.apply_preset(k))
            self.preset_btns[key] = b
            flow.addWidget(b)
        self.preset_grp = grp
        self.preset_btns['natural'].setChecked(True)
        holder = QWidget()
        holder.setLayout(flow)
        lay.addWidget(holder)
        cols = QHBoxLayout()
        cols.setSpacing(18)
        self.f_sliders, self.f_vals, self.f_labels = {}, {}, {}
        left, right = vbox(spacing=3), vbox(spacing=3)
        for gi, (group, lst) in enumerate(FILTERS):
            target = left if gi < 2 and group == 'Light' else right if group == 'Texture' else left
            target.addWidget(caps(group, 9, color=T.DIM, spacing=2))
            for k, name, lo, hi, d, _ in lst:
                lab = DoubleClickLabel(name.upper())
                lab.setFont(T.caps_font(10, 0.8))
                lab.setStyleSheet(f'color: {T.MUTED};')
                lab.setFixedWidth(88)
                lab.setToolTip('Double-click to reset')
                lab.doubleClicked.connect(lambda k=k: self.set_filter(k, FDEF[k], user=True))
                s = QSlider(Qt.Horizontal)
                s.setRange(lo, hi)
                s.setValue(d)
                s.valueChanged.connect(lambda v, k=k: self.set_filter(k, v, user=True, from_slider=True))
                val = label(str(d), obj='val', px=11)
                val.setFixedWidth(30)
                val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.f_sliders[k], self.f_vals[k], self.f_labels[k] = s, val, lab
                target.addLayout(hbox(lab, s, val, spacing=6))
            target.addSpacing(4)
        left.addStretch(1)
        right.addStretch(1)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        lay.addLayout(cols)
        lay.addWidget(label('Double-click a slider name to reset it.', dim=True, px=10))
        lay.addStretch(1)
        return lay

    def _pane_frame(self):
        lay = vbox(spacing=10, margins=(0, 10, 4, 2))
        self.aspect_seg = Segmented([('original', 'Original'), ('9:16', '9:16'), ('1:1', '1:1'), ('4:5', '4:5'),
                                     ('16:9', '16:9')], 'original', tone='blue')
        self.aspect_seg.changed.connect(lambda v: self.set_opt('aspect', v))
        lay.addLayout(self._row('Aspect', self.aspect_seg, None))
        self.flip = Toggle('Flip horizontally', False, small=True)
        self.flip.toggled.connect(lambda on: self.set_opt('hflip', bool(on)))
        lay.addLayout(self._row('Mirror', self.flip, None))
        self.fade_seg = Segmented([('0', 'Off'), ('1', '1s'), ('2', '2s'), ('3', '3s')], '0')
        self.fade_seg.changed.connect(lambda v: self.set_opt('fade_out', int(v)))
        lay.addLayout(self._row('Fade out', self.fade_seg, label('audio + picture at the end', dim=True, px=10), None))
        lay.addStretch(1)
        return lay

    def _pane_export(self):
        lay = vbox(spacing=10, margins=(0, 10, 4, 2))
        self.quality_seg = Segmented([('fast', 'Fast'), ('balanced', 'Balanced'), ('high', 'High')], 'balanced')
        self.quality_seg.changed.connect(lambda v: self.set_opt('quality', v))
        lay.addLayout(self._row('Quality', self.quality_seg, None))
        self.plen_seg = Segmented([('15', '15s'), ('30', '30s'), ('0', 'Full')], '15')
        self.plen_seg.changed.connect(lambda v: self.set_opt('preview_sec', int(v)))
        lay.addLayout(self._row('Preview', self.plen_seg, label('length of the preview clip', dim=True, px=10), None))
        lay.addWidget(label('With no effects, filters, crop or speed change the video is copied as-is (lossless and '
                            'instant) when it is H.264 or H.265. Otherwise it is re-encoded at this quality.',
                            dim=True, wrap=True, px=10))
        lay.addStretch(1)
        return lay

    # ── download bar ──────────────────────────────────────────────────────
    def _build_footer(self):
        self.footer = QFrame()
        self.footer.setObjectName('footer')
        title = QLabel('DOWNLOAD')
        title.setFont(T.display_font(22, 2.5))
        title.setStyleSheet(f'color: {T.SUCCESS};')
        self.ex_sub = ElidedLabel('')
        self.ex_sub.setStyleSheet(f'color: {T.MUTED}; font-size: 10px;')
        self.ex_status = ElidedLabel('')
        self.ex_status.setStyleSheet(f'color: {T.MUTED}; font-size: 11px;')
        self.ex_bar = QProgressBar()
        self.ex_bar.setRange(0, 100)
        self.ex_bar.setTextVisible(False)
        self.ex_bar.setFixedWidth(110)
        self.ex_bar.hide()
        self.saved_show = button('Show in folder', kind='link')
        self.saved_show.setStyleSheet(f'border: none; color: {T.INFO}; padding: 0; font-size: 11px;')
        self.saved_show.clicked.connect(lambda: files.reveal(self.last_saved))
        self.saved_show.hide()
        info = QWidget()
        info.setLayout(vbox(self.ex_sub, hbox(self.ex_status, self.ex_bar, self.saved_show, spacing=8), spacing=2))
        info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.dl_btns = {}
        row = hbox(title, 6, spacing=8, margins=(14, 8, 12, 8))
        row.addWidget(info, 1)
        for kind, name, style, tip in DL_KINDS:
            b = button(name.upper(), kind=style, tip=tip)
            b.clicked.connect(lambda _=False, k=kind: self.save(k))
            b.setEnabled(False)
            self.dl_btns[kind] = b
            row.addWidget(b)
        row.addSpacing(6)
        self.start_over = button('Start over', tip='Clear everything and begin again')
        self.start_over.clicked.connect(self.reset_all)
        row.addWidget(self.start_over)
        self.footer.setLayout(row)
        return self.footer

    # ════════════════════════════════════════════════════════════════════
    # Small UI helpers
    # ════════════════════════════════════════════════════════════════════
    def show_toast(self, msg, tone=None):
        self.toast.show_msg(msg, tone)

    def show_error(self, msg):
        self.err_text.setText(msg)
        self.errbox.show()

    def hide_error(self):
        self.errbox.hide()

    def set_status(self, msg, kind=None):
        self.status.set_text(msg or '')
        self.status.setStyleSheet(f'color: {T.ACCENT if kind == "err" else T.MUTED}; font-size: 10px;')
        if kind == 'busy':
            self.status_spin.start()
        else:
            self.status_spin.stop()

    def set_rendering(self, text):
        if text:
            self.render_text.setText(text)
            self.render_spin.start()
        else:
            self.render_text.setText('')
            self.render_spin.stop()

    def export_status(self, msg, pct=None):
        self.ex_status.set_text(msg or '')
        if pct is not None:
            self.ex_bar.show()
            self.ex_bar.setValue(int(pct))
            for b in self.dl_btns.values():
                b.setEnabled(False)
        else:
            self.ex_bar.hide()

    def set_progress(self, msg=None, pct=None):
        if msg:
            self.prog_text.set_text(msg)
        if pct is not None:
            self.prog_bar.setValue(int(pct))

    def _tick_clock(self):
        self.prog_timer.setText(fmt_time(time.time() - self._t0))

    # ════════════════════════════════════════════════════════════════════
    # Sources / settings before the match
    # ════════════════════════════════════════════════════════════════════
    def set_bpm_mode(self, mode):
        self.bpm_mode = mode
        self.mode_vid.setChecked(mode == 'video')
        self.mode_song.setChecked(mode == 'song')
        if self.phase == 'studio':
            self.show_toast('Tempo mode changes need a new match — press Match Again')

    def on_mix(self, v):
        self.mix_val.setText(f'{v}%')
        self.opts['song_vol'] = v / 100
        if self.phase == 'studio':
            self.schedule_adjust(opts=True)

    def on_pitch(self, on):
        on = bool(on)
        changed = on != self.preserve_pitch
        self.preserve_pitch = on
        text = 'Pitch locked' if on else 'Pitch follows speed'
        for t in (self.pitch, getattr(self, 'pitch2', None)):
            if t is not None:
                t.blockSignals(True)
                t.setChecked(on)
                t.setText(text)
                t.blockSignals(False)
                t.updateGeometry()
                t.update()
        if hasattr(self, 'pitch2_note'):
            self.pitch2_note.setText('the song keeps its key' if on else 'like a turntable: faster = higher')
        if changed and self.phase == 'studio':
            self.schedule_adjust(preserve_pitch=on)

    def _pitch_note(self):
        note = ('Match Song keeps the song untouched — pitch only changes the Full song download.'
                if self.bpm_mode == 'song' and self.phase == 'studio' else '')
        self.pitch_note.setText(note)
        self.pitch_note.setVisible(bool(note))

    # ════════════════════════════════════════════════════════════════════
    # Match
    # ════════════════════════════════════════════════════════════════════
    def run(self):
        self.hide_error()
        p = {'bpm_mode': self.bpm_mode, 'preserve_pitch': self.preserve_pitch, 'opts': copy.deepcopy(self.opts)}
        for box, key, name in ((self.vid, 'video', 'video'), (self.song, 'song', 'song')):
            if box.mode == 'url':
                u = box.url.text().strip()
                if not u:
                    return self.show_error(f'Enter a {name} link, or switch to File.')
                if not is_url(u):
                    return self.show_error(f"The {name} link doesn't look right — it should start with https://")
                p[key + '_url'] = u
            else:
                if not box.file_id:
                    return self.show_error(f'Choose a {name} file first.')
                p[key + '_file'] = box.file_id
            ts, te = parse_time(box.t_start.text()), parse_time(box.t_end.text())
            if ts is not None and te is not None and te <= ts:
                return self.show_error(f'{name.capitalize()} trim: the end must be after the start.')
            if ts is not None:
                p[box.src + '_trim_start'] = ts
            if te is not None:
                p[box.src + '_trim_end'] = te

        self._release_player()
        if self.root_job:
            old = self.root_job
            run_bg(lambda: self.engine.api('/api/cleanup', {'job_id': old}))
        self.engine.stop()
        self.flush_timer.stop()
        self.root_job = self.current_job = self.done_job = self.last_good_job = self.preview_for = None
        self.timing = self.shown_timing = None
        self.timing_by_job = {}
        self.pending = None
        self.phase = 'analysing'
        self.reset_downloads()
        self.footer.hide()
        self.main.setCurrentIndex(0)
        self.empty.hide()
        self.progress.show()
        self.warnbox.hide()
        self.warn_line.hide()
        for v, dot in self.stat.values():
            v.setText('—')
            if dot:
                dot.setStyleSheet(f'background: {T.DIM}; border-radius: 3px;')
        self.vid.badge.hide()
        self.song.badge.hide()
        self.run_btn.setEnabled(False)
        self.run_btn.setText('MATCHING…')
        self.prog_spin.start()
        self.set_progress('Starting…', 0)
        self._t0 = time.time()
        self._tick_clock()
        self.clock.start(1000)
        try:
            d = self.engine.api('/api/start', p)
        except Exception as e:                        # noqa: BLE001
            self.show_error(str(e))
            self.end_analysis(False)
            return
        self.root_job = self.current_job = d['job_id']
        self.engine.listen(d['job_id'])

    def end_analysis(self, ok):
        self.clock.stop()
        self.run_btn.setEnabled(True)
        self.run_btn.setText('MATCH AGAIN' if ok else 'MATCH BEATS')
        self.prog_spin.stop()
        if not ok:
            self.progress.hide()
            self.empty.show()
            self.phase = 'idle'
        else:
            self.set_progress('Matched — fine-tune on the right', 100)

    # ── job events ────────────────────────────────────────────────────────
    def on_event(self, jid, m):
        if jid != self.current_job:
            return
        t = m.get('type')
        analysing = self.phase == 'analysing'
        if t == 'progress':
            if analysing:
                self.set_progress(m.get('msg'), m.get('pct'))
            elif self.preview_for != jid:
                self.set_rendering(m.get('msg'))
                self.set_status(m.get('msg'), 'busy')
            else:
                self.export_status(m.get('msg'), m.get('pct'))
        elif t == 'info':
            self.on_info(m)
        elif t == 'warn':
            self.warn_text.setText('⚠ ' + m.get('msg', ''))
            self.warnbox.show()
            self.warn_line.set_text('⚠ ' + m.get('msg', ''))
            self.warn_line.show()
        elif t == 'timing':
            self.timing_by_job[jid] = m
            self.timing = m
            self.render_timing()
        elif t == 'preview_ready':
            self.on_preview(jid)
        elif t == 'done':
            self.on_done(jid)
        elif t == 'error':
            if analysing:
                self.show_error(m.get('msg', 'Something went wrong.'))
                self.end_analysis(False)
            else:
                self.set_status('⚠ ' + m.get('msg', ''), 'err')
                self.set_rendering(None)
                self.export_status('Latest change failed — downloads are the last good version.'
                                   if self.done_job else 'Export failed.')
                if self.last_good_job:
                    self.current_job = self.last_good_job
                if self.done_job:
                    self.set_downloads(self.done_job, stale=True)

    def on_info(self, m):
        def conf_color(c):
            return T.SUCCESS if c >= 0.6 else T.WARN if c >= 0.3 else T.ACCENT
        if m.get('vid_bpm'):
            self.stat['vid'][0].setText(f'{float(m["vid_bpm"]):.1f}')
        if m.get('song_bpm'):
            self.stat['song'][0].setText(f'{float(m["song_bpm"]):.1f}')
        for k, ck in (('vid', 'vid_confidence'), ('song', 'song_confidence')):
            if m.get(ck) is not None:
                self.stat[k][1].setStyleSheet(f'background: {conf_color(m[ck])}; border-radius: 3px;')
                self.stat[k][1].setToolTip(f'Beat confidence {m[ck]:.0%}')
        if m.get('stretch') is not None:
            self.stat['stretch'][0].setText(f'{float(m["stretch"]):.3f}x')
        if m.get('offset') is not None:
            self.stat['offset'][0].setText(f'{float(m["offset"]):.2f}s')
        if m.get('beats_per_bar'):
            self.stat['meter'][0].setText(str(m['beats_per_bar']) + ('' if m.get('meter_detected') else '?'))
        for box, k in ((self.vid, 'vid_music_start'), (self.song, 'song_music_start')):
            if (m.get(k) or 0) > 0.5:
                box.badge.setText(f'⚡ beat starts at {m[k]}s')
                box.badge.show()
        self._stats_line()

    def _stats_line(self):
        s = self.stat
        parts = [f'{s["vid"][0].text()} → {s["song"][0].text()} BPM', f'stretch {s["stretch"][0].text()}',
                 f'{s["meter"][0].text()} beats/bar']
        self.stats_line.set_text('   '.join(parts))

    def on_preview(self, jid):
        self.preview_for = jid
        self.last_good_job = jid
        self.shown_timing = self.timing_by_job.get(jid) or self.timing
        first = self.phase == 'analysing'
        keep = self.player.position() if (self.keep_pos.isChecked() and not first) else 0
        new_sp = (self.timing_by_job.get(jid) or {}).get('speed') or 1.0
        if keep and self._shown_speed and abs(new_sp - self._shown_speed) > 1e-6:
            keep = int(keep * self._shown_speed / new_sp)          # same moment of the song at the new speed
        self._shown_speed = new_sp
        playing = first or self.player.playbackState() == QMediaPlayer.PlayingState
        path = self.engine.preview_path(jid)
        if path:
            self._load_preview(path, keep, playing)
        self.set_rendering(None)
        self.set_status('Preview up to date — exporting the full video…', 'busy')
        self.export_status('Exporting full video…', 0)
        plen = self.opts['preview_sec']
        self.render_text.setText('')
        self.video.seek.setToolTip(f'Preview: first {plen}s' if plen else 'Preview: full length')
        if first:
            self.phase = 'studio'
            self.end_analysis(True)
            self.main.setCurrentIndex(1)
            self.footer.show()
            self.leds.set_count((self.timing or {}).get('beats_per_bar') or 4)
            self._pitch_note()

    def on_done(self, jid):
        self.done_job = jid
        self.set_downloads(jid)
        self.set_status('Preview and downloads up to date.')

    # ── downloads ─────────────────────────────────────────────────────────
    def set_downloads(self, jid, stale=False):
        for b in self.dl_btns.values():
            b.setEnabled(True)
        if not stale:
            self.export_status('✓ Ready — matches the preview.')
        t = self.timing_by_job.get(jid) or self.timing
        if t:
            mode = 'Song tempo kept' if self.bpm_mode == 'song' else 'Song stretched to video'
            self.ex_sub.set_text(f'{mode}  ·  {t["target_bpm"]:.1f} BPM')

    def reset_downloads(self):
        for b in self.dl_btns.values():
            b.setEnabled(False)
        self.export_status('')
        self.ex_sub.set_text('')
        self.saved_show.hide()
        self.last_saved = None

    def save(self, kind):
        ready = self.dl_btns[kind].isEnabled() and self.done_job
        if not ready:
            self.show_toast('Still exporting — the buttons light up when it is ready')
            return
        if self.saving:
            return
        self.saving = True
        btn, jid = self.dl_btns[kind], self.done_job
        name = dict((k, n) for k, n, _, _ in DL_KINDS)[kind]
        btn.setText('SAVING…')

        def finish():
            btn.setText(name.upper())
            self.saving = False

        def ask(res):
            path, suggested = res
            ext = os.path.splitext(suggested)[1]
            folder = files.save_folder()
            filt = 'Video (*.mp4)' if ext == '.mp4' else 'MP3 audio (*.mp3)'
            dest, _ = QFileDialog.getSaveFileName(self, 'Save ' + name, str(folder / files.free_name(folder, suggested)),
                                                  filt)
            if not dest:
                return finish()
            if not dest.lower().endswith(ext):
                dest += ext

            def placed(_):
                files.remember_save_folder(os.path.dirname(dest))
                self.last_saved = dest
                self.ex_status.set_text('Saved: ' + dest)
                self.ex_bar.hide()
                self.saved_show.show()
                self.show_toast('Saved', 'ok')
                finish()

            def failed(e):
                self.show_toast(f'Could not save: {e}', 'err')
                finish()
            run_bg(lambda: files.place_file(path, dest), ok=placed, err=failed)

        def fail(e):
            self.show_toast(str(e), 'err')
            finish()
        run_bg(lambda: self.server.resolve_file(kind, jid), ok=ask, err=fail)

    # ════════════════════════════════════════════════════════════════════
    # Live changes (coalesced, like before)
    # ════════════════════════════════════════════════════════════════════
    def unit_beats(self):
        if self.unit == 'bar':
            return (self.timing or {}).get('beats_per_bar') or 4
        return UNIT_BEATS[self.unit]

    def schedule_adjust(self, beats=0, ms=0, reset=False, seek_to=None, preserve_pitch=None, opts=False):
        if self.phase != 'studio' or not self.current_job:
            return
        p = self.pending or {'beats': 0, 'ms': 0, 'opts': False}
        self.pending = p
        p['beats'] += beats
        p['ms'] += ms
        if reset:
            p.update(reset=True, beats=0, ms=0)
            p.pop('seek_to', None)
        if seek_to is not None:
            p.update(seek_to=seek_to, beats=0, ms=0)
            p.pop('reset', None)
        if preserve_pitch is not None:
            p['preserve_pitch'] = preserve_pitch
        if opts:
            p['opts'] = True
        self.set_rendering('Rendering preview…')
        self.set_status('Waiting for more changes…', 'busy')
        self.flush_timer.start(450 if opts else 220)

    def flush(self):
        p, self.pending = self.pending, None
        if not p or self.phase != 'studio':
            return
        body = {'job_id': self.current_job, 'lock': self.unit if self.lock else None}
        for k in ('beats', 'ms'):
            if p.get(k):
                body[k] = p[k]
        if p.get('reset'):
            body['reset'] = True
        if p.get('seek_to') is not None:
            body['seek_to'] = p['seek_to']
        if p.get('preserve_pitch') is not None:
            body['preserve_pitch'] = p['preserve_pitch']
        if p.get('opts'):
            body['opts'] = copy.deepcopy(self.opts)
        try:
            d = self.engine.api('/api/adjust', body)
        except Exception as e:                        # noqa: BLE001
            self.set_rendering(None)
            self.set_status('⚠ ' + str(e), 'err')
            return
        self.current_job = d['job_id']
        self.timing = d['timing']
        self.timing_by_job[d['job_id']] = d['timing']
        self.render_timing()
        self.set_status('Rendering preview…', 'busy')
        self.export_status('Waiting for preview…', 0)
        self.engine.listen(d['job_id'])

    def render_timing(self):
        t = self.timing
        if not t:
            return
        auto = abs(t['delta_ms']) < 0.5
        db = t['delta_beats']
        self.ro_main.setText('AUTO' if auto else frac_beats(db) + (' BEAT' if abs(db) <= 1.001 else ' BEATS'))
        parts = [f'offset {t["offset"]:.3f}s']
        if not auto:
            parts.append(f'{"+" if t["delta_ms"] > 0 else ""}{round(t["delta_ms"])} ms from auto')
        parts.append(f'{t["target_bpm"]:.2f} BPM')
        parts.append(f'{t["beats_per_bar"]} beats/bar')
        if t.get('micro_align_ms') and abs(t['micro_align_ms']) >= 1:
            parts.append(f'kick-locked {"+" if t["micro_align_ms"] > 0 else ""}{round(t["micro_align_ms"])} ms')
        self.ro_sub.set_text('    '.join(parts))
        self.reset_btn.setEnabled(not auto)
        self.seek.blockSignals(True)
        self.seek.setRange(0, int(t['song_duration'] * 10))
        if not self.seek.isSliderDown():
            self.seek.setValue(int(max(0, t['offset']) * 10))
        self.seek.blockSignals(False)
        self.seek_val.setText(fmt_time(self.seek.value() / 10, True))
        self.stat['offset'][0].setText(f'{t["offset"]:.2f}s')
        self.update_unit_labels()
        self._speed_info()

    def update_unit_labels(self):
        step = UNIT_STEP[self.unit]
        self.n_back.setText(f'« −{step}')
        self.n_fwd.setText(f'+{step} »')
        for b in (self.n_fine_back, self.n_fine_fwd):
            b.setEnabled(not self.lock)
            b.setToolTip('Turn off Grid lock for ms fine-tuning' if self.lock else 'Shift + ← / →')

    def set_unit(self, u):
        self.unit = u
        self.unit_seg.set_value(u)
        self.update_unit_labels()

    def toggle_lock(self):
        self.lock = self.lock_t.isChecked()
        self.update_unit_labels()
        if self.lock:
            self.show_toast(f'Grid lock on — nudges snap to {UNIT_NAME[self.unit]}s')

    def set_opt(self, key, value):
        self.opts[key] = value
        self.schedule_adjust(opts=True)

    def on_div(self, v):
        self.opts['fx_division'] = v
        if self.opts['effects']:
            self.schedule_adjust(opts=True)

    def on_fx_int(self, v):
        self.fx_int_val.setText(f'{v}%')
        self.opts['fx_intensity'] = v / 100
        if self.opts['effects']:
            self.schedule_adjust(opts=True)

    def toggle_fx(self, key):
        fx = self.opts['effects']
        if key in fx:
            fx.remove(key)
        else:
            fx.append(key)
        self.render_fx()
        self.schedule_adjust(opts=True)

    def clear_fx(self):
        if not self.opts['effects']:
            return
        self.opts['effects'] = []
        self.render_fx()
        self.schedule_adjust(opts=True)

    def render_fx(self):
        for k, b in self.fx_btns.items():
            b.setChecked(k in self.opts['effects'])
        n = len(self.opts['effects'])
        self.tab_btns['fx'].setText(f'Beat FX  {n}' if n else 'Beat FX')

    def set_filter(self, k, v, user=False, from_slider=False, silent=False):
        self.filter_ui[k] = v
        self.opts['filters'][k] = FMAP[k](v)
        if not from_slider:
            s = self.f_sliders[k]
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        self.f_vals[k].setText(str(v))
        self.f_labels[k].setStyleSheet(f'color: {T.ACCENT2 if v != FDEF[k] else T.MUTED};')
        if user:
            self._mark_preset(None)
        self._color_count()
        if not silent:
            self.schedule_adjust(opts=True)

    def _color_count(self):
        n = sum(1 for k in FDEF if self.filter_ui[k] != FDEF[k])
        self.tab_btns['color'].setText(f'Color  {n}' if n else 'Color')

    def _mark_preset(self, key):
        self.preset_grp.setExclusive(False)
        for k, b in self.preset_btns.items():
            b.setChecked(k == key)
        self.preset_grp.setExclusive(True)

    def apply_preset(self, key):
        p = next((x[2] for x in PRESETS if x[0] == key), None)
        if p is None:
            return
        for k in FDEF:
            self.set_filter(k, p.get(k, FDEF[k]), silent=True)
        self._mark_preset(key)
        self.schedule_adjust(opts=True)

    # ════════════════════════════════════════════════════════════════════
    # Player
    # ════════════════════════════════════════════════════════════════════
    def _make_player(self):
        self.player = QMediaPlayer(self)
        self.audio = None
        self._shown_speed = 1.0
        self.fs_window = None
        self._dev_timer = QTimer(self)
        self._dev_timer.timeout.connect(self._follow_default_audio)
        if not os.environ.get('BEATSYNC_NO_AUDIO'):                  # (tests on machines without sound)
            self.audio = QAudioOutput(QMediaDevices.defaultAudioOutput(), self)
            self.player.setAudioOutput(self.audio)
            # The preview's sound follows the Windows default device (e.g. monitor → headphones):
            # Qt keeps an output on the device it started with unless told otherwise.
            self.media_devices = QMediaDevices(self)
            self.media_devices.audioOutputsChanged.connect(self._follow_default_audio)
        self._dev_timer.start(1500)
        if hasattr(self.player, 'setPitchCompensation'):            # Qt 6.10+: playback speed keeps the key
            try:
                self.player.setPitchCompensation(True)
            except Exception:
                pass

    def _follow_default_audio(self):
        if self.audio is None:
            return
        dev = QMediaDevices.defaultAudioOutput()
        if dev.isNull():
            return
        if dev.id() != self.audio.device().id():
            self.audio.setDevice(dev)

    def _wire_player(self):
        self.video.activate()
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(lambda e, s: s and self.set_status('⚠ Preview: ' + s, 'err'))
        self.loop.toggled.connect(lambda on: self.player.setLoops(QMediaPlayer.Infinite if on else 1))
        self.player.setLoops(QMediaPlayer.Infinite)

    def toggle_play(self):
        self.video.toggle()

    def toggle_fullscreen(self):
        if self.fs_window is not None:
            self.fs_window.close()
            return
        if self.player.source().isEmpty():
            return
        fs = FullscreenPlayer(self.player, self.screen())
        fs.closed.connect(self._fullscreen_closed)
        self.fs_window = fs
        fs.view.activate()
        fs.showFullScreen()
        fs.activateWindow()
        fs.view.show_controls()

    def _fullscreen_closed(self):
        self.fs_window = None
        self.video.activate()
        self.activateWindow()

    def _load_preview(self, path, keep_ms, play):
        # Swapping the source of a playing player leaves Qt's player stuck at 0 — stop it first,
        # and only seek / play once the *new* file has loaded.
        url = QUrl.fromLocalFile(path)
        self._follow_default_audio()
        self.player.stop()
        self._pending_load = (url, keep_ms, play)
        self.player.setSource(url)
        self.player.setLoops(QMediaPlayer.Infinite if self.loop.isChecked() else 1)

    def _on_media_status(self, st):
        if self._pending_load and st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            url, keep, play = self._pending_load
            if self.player.source() != url:
                return
            self._pending_load = None
            if keep and keep < self.player.duration() - 100:
                self.player.setPosition(keep)
            if play:
                self.player.play()

    def _release_player(self):
        """Let go of the preview file (Windows keeps files that are open from being deleted)."""
        self._pending_load = None
        if self.fs_window is not None:
            self.fs_window.close()
        self.player.stop()
        self.player.setSource(QUrl())

    def _led_loop(self):
        t = self.shown_timing
        if not t or self.phase != 'studio' or self.player.source().isEmpty():
            return
        bi, bpb = t['beat_interval'], t['beats_per_bar']
        tt = self.player.position() / 1000 - t['beat_phase_out']
        beat = int(tt // bi)
        in_bar = ((round((t['beat_phase_out'] - t['bar_phase_out']) / bi) + beat) % bpb + bpb) % bpb
        if self.leds.n != min(bpb, 8):
            self.leds.set_count(bpb)
        if beat != self._last_beat:
            self._last_beat = beat
            hit = self.player.playbackState() == QMediaPlayer.PlayingState
            self.leds.set_state(in_bar if in_bar < 8 else -1, hit)
            if hit:
                QTimer.singleShot(90, lambda: self.leds.set_state(self.leds.cur, False))
            bar = int((self.player.position() / 1000 - t['bar_phase_out']) // (bi * bpb)) + 1
            self.beatpos.setText('Bar – · Beat –' if tt < 0 else f'Bar {bar} · Beat {in_bar + 1}')

    # ════════════════════════════════════════════════════════════════════
    # Start over
    # ════════════════════════════════════════════════════════════════════
    def reset_all(self):
        self._release_player()
        if self.root_job:
            jid = self.current_job or self.root_job
            run_bg(lambda: self.engine.api('/api/cleanup', {'job_id': jid}))
        self.engine.stop()
        self.flush_timer.stop()
        self.root_job = self.current_job = self.done_job = self.last_good_job = self.preview_for = None
        self.timing = self.shown_timing = None
        self.timing_by_job = {}
        self.phase = 'idle'
        self.pending = None
        self.reset_downloads()
        self.footer.hide()
        self.main.setCurrentIndex(0)
        self.progress.hide()
        self.empty.show()
        self.vid.reset()
        self.song.reset()
        self.opts = default_opts()
        for k in FDEF:
            self.set_filter(k, FDEF[k], silent=True)
        self._mark_preset('natural')
        self.render_fx()
        self.mix.setValue(100)
        self.div_seg.set_value('beat')
        self.fx_int.setValue(100)
        self.aspect_seg.set_value('original')
        self.flip.setChecked(False)
        self.fade_seg.set_value('0')
        self.quality_seg.set_value('balanced')
        self.plen_seg.set_value('15')
        self.set_speed(1.0)
        self._shown_speed = 1.0
        self.opts = default_opts()                     # the widget resets above must not leave changes behind
        self.pitch.setChecked(True)
        self.set_bpm_mode('video')
        self.set_rendering(None)
        self.set_status('')
        self.warn_line.hide()
        self.run_btn.setEnabled(True)
        self.run_btn.setText('MATCH BEATS')
        self.hide_error()
        self._pitch_note()

    # ════════════════════════════════════════════════════════════════════
    # Keyboard, drag & drop, window
    # ════════════════════════════════════════════════════════════════════
    def eventFilter(self, obj, e):
        if e.type() != QEvent.KeyPress or not self.isActiveWindow():
            return False
        key, mods = e.key(), e.modifiers()
        if key in (Qt.Key_Return, Qt.Key_Enter) and mods & Qt.ControlModifier:
            if self.run_btn.isEnabled():
                self.run()
            return True
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QAbstractSlider, QAbstractSpinBox)):
            return False
        if self.phase != 'studio' or mods & (Qt.ControlModifier | Qt.AltModifier):
            return False
        if key in (Qt.Key_Left, Qt.Key_Right):
            d = 1 if key == Qt.Key_Right else -1
            if mods & Qt.ShiftModifier:
                if not self.lock:
                    self.schedule_adjust(ms=10 * d)
                else:
                    self.show_toast('Turn off Grid lock (L) for ms fine-tuning')
            else:
                self.schedule_adjust(beats=self.unit_beats() * d)
            return True
        if key == Qt.Key_Space:
            if isinstance(focus, QAbstractButton):
                return False
            if e.isAutoRepeat():
                return True
            self.toggle_play()
            return True
        if key == Qt.Key_R:
            self.schedule_adjust(reset=True)
            return True
        if key == Qt.Key_L:
            self.lock_t.click()
            return True
        if Qt.Key_1 <= key <= Qt.Key_5:
            self.set_unit(UNITS[key - Qt.Key_1])
            return True
        return False

    def _local_paths(self, e):
        md = e.mimeData()
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []

    def _box_at(self, pos):
        for box in (self.vid, self.song):
            if box.rect().contains(box.mapFrom(self, pos)):
                return box
        return None

    def dragEnterEvent(self, e):
        if self._local_paths(e):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        box = self._box_at(e.position().toPoint())
        for b in (self.vid, self.song):
            b.set_drop_hover(b is box)
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        for b in (self.vid, self.song):
            b.set_drop_hover(False)

    def dropEvent(self, e):
        for b in (self.vid, self.song):
            b.set_drop_hover(False)
        paths = [p for p in self._local_paths(e) if os.path.isfile(p)]
        if not paths:
            return
        box = self._box_at(e.position().toPoint())
        if box and len(paths) == 1:
            box.use_file(paths[0])
            return
        # dropped elsewhere (or two files): audio goes to the song, video to the video
        for p in paths[:2]:
            ext = Path(p).suffix.lower()
            if ext in self.exts_audio and (self.song.file_id is None or len(paths) > 1):
                self.song.use_file(p)
            elif ext in self.exts_video:
                self.vid.use_file(p)
            elif ext in self.exts_audio:
                self.song.use_file(p)
            else:
                self.show_error(f'Unsupported file type: {ext or "unknown"}')
        e.acceptProposedAction()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            par = self.toast.parentWidget()
            self.toast.move((par.width() - self.toast.width()) // 2, par.height() - self.toast.height() - 18)

    def _bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.server._active_jobs():
            r = QMessageBox.question(self, 'Quit BeatSync?', 'A render is still running. Quit anyway?',
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self._release_player()
        e.accept()

    # ════════════════════════════════════════════════════════════════════
    # Tools, settings, updates
    # ════════════════════════════════════════════════════════════════════
    def _startup(self):
        self.refresh_caps()
        self.refresh_tools(first=True)
        self.check_app_update(False)
        if self.ctx.updated_from:
            self.banner_text.setText(f'BeatSync was updated to {self.ctx.version} (from {self.ctx.updated_from}).')
            self.banner_btn.hide()
            self.banner.show()

    def refresh_caps(self):
        def show(c):
            bad = []
            if not c.get('ffmpeg_version'):
                bad.append('ffmpeg missing')
            if not c.get('rubberband'):
                bad.append('basic stretch (Rubber Band missing)')
            if not c.get('yt_dlp'):
                bad.append('links off (yt-dlp missing)')
            elif not c.get('js_runtime'):
                bad.append('YouTube may fail (no JS runtime)')
            self.caps_line.setText('  ·  '.join(bad))
            self.caps_line.setStyleSheet(f'color: {T.WARN}; font-size: 10px;')
            self.caps_line.setVisible(bool(bad))
        run_bg(lambda: self.engine.api('/api/caps'), ok=show, err=lambda e: None)

    def refresh_tools(self, first=False, then=None):
        def got(d):
            self.tools_data = d
            attention = bool(d['missing_required']) or any(
                (not t['ok']) and t['need'] == 'links' and t['installable'] for t in d['tools'].values())
            self.gear.setText('⚙  Settings  •' if attention else '⚙  Settings')
            self.gear.setStyleSheet(f'color: {T.WARN}; border-color: rgba(255,204,45,0.5);' if attention else '')
            if first:
                dismissed = bool((self.engine.api('/api/settings') or {}).get('setup_dismissed'))
                optional = any((not t['ok']) and t['installable'] and t['need'] != 'required'
                               for t in d['tools'].values())
                if d['missing_required'] or (optional and not dismissed):
                    QTimer.singleShot(200, self.open_setup)
            if then:
                then(d)
        run_bg(lambda: self.engine.api('/api/tools'), ok=got, err=lambda e: None)

    def open_setup(self):
        from .dialogs import SetupDialog
        SetupDialog(self).exec()
        self.refresh_tools()
        self.refresh_caps()

    def open_settings(self):
        from .dialogs import SettingsDialog
        SettingsDialog(self).exec()
        self.refresh_tools()
        self.refresh_caps()

    def check_app_update(self, force, then=None):
        def got(u):
            self.app_update = u
            if u.get('enabled') and u.get('newer') and not self.ctx.updated_from:
                self.banner_text.setText(f'BeatSync {u["latest"]} is available (you have {u["current"]}).')
                self.banner_btn.setText('Update now' if self._can_self_update() else 'Download')
                self.banner_btn.show()
                self.banner.show()
            if then:
                then(u)
        run_bg(lambda: self.engine.api('/api/app/update' + ('?force=1' if force else '')), ok=got,
               err=lambda e: then and then({'error': str(e)}))

    def _can_self_update(self):
        return bool(self.ctx.frozen and self.app_update and self.app_update.get('setup_url'))

    def _banner_action(self):
        u = self.app_update
        if not u:
            return
        if not self._can_self_update():
            if u.get('url'):
                import webbrowser
                webbrowser.open(u['url'])
            return
        if self.server._active_jobs():
            self.show_toast('Finish or cancel the running render first.', 'err')
            return
        import tools
        r = tools.start_app_update(on_ready=self.ctx.run_setup)
        if not r or not r.get('ok'):
            self.show_toast((r or {}).get('error') or 'Update failed', 'err')
            return
        self.banner_btn.setEnabled(False)
        poll = QTimer(self)

        def tick():
            s = tools.app_update_status()
            if s.get('state') == 'error':
                poll.stop()
                self.banner_text.setText('Update failed: ' + str(s.get('error')))
                self.banner_btn.setText('Retry')
                self.banner_btn.setEnabled(True)
            elif s.get('state') == 'launching':
                self.banner_text.setText(f'Installing {u.get("latest")}… BeatSync will restart.')
            elif s.get('msg'):
                self.banner_text.setText(s['msg'] + (f' ({s["pct"]}%)' if s.get('pct') else ''))
        poll.timeout.connect(tick)
        poll.start(500)
