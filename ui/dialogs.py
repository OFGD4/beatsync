"""First-run tool setup and the Settings window."""

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QWidget, QFrame, QLabel, QCheckBox, QProgressBar, QScrollArea, QVBoxLayout,
                               QFileDialog, QGridLayout)

import tools
from . import theme as T
from . import files
from .engine import run_bg
from .widgets import label, caps, button, hbox, vbox, amp

NEED_LABEL = {'required': 'required', 'links': 'for links', 'quality': 'better audio'}
TOOL_ORDER = ['ffmpeg', 'yt-dlp', 'deno', 'rubberband']


def ordered(d):
    t = d['tools']
    return [(n, t[n]) for n in TOOL_ORDER if n in t] + [(n, s) for n, s in t.items() if n not in TOOL_ORDER]


def tool_state(t):
    if t.get('state') in ('installing', 'queued'):
        return T.MUTED, t.get('msg') or 'Working…'
    if t.get('state') == 'error' and not t['ok']:
        return T.ACCENT, t.get('error') or 'Failed'
    if t['ok']:
        v = t.get('version')
        return T.SUCCESS, '✓ ' + (v[:22] if v and v != 'installed' else 'installed')
    if not t['installable']:
        return T.ACCENT, 'install manually'
    return T.MUTED, f'~{t["size_mb"]} MB'


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            it.widget().deleteLater()
        elif it.layout():
            clear_layout(it.layout())


class SetupDialog(QDialog):
    """Downloads the free tools BeatSync uses (once, into the user folder)."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle('Set up BeatSync')
        self.setModal(True)
        self.setMinimumWidth(520)
        self.choice = None
        lay = vbox(spacing=10, margins=(22, 18, 22, 18))
        self.setLayout(lay)
        t = QLabel('SET UP BEATSYNC')
        t.setFont(T.display_font(28, 3))
        t.setStyleSheet(f'color: {T.ACCENT};')
        lay.addWidget(t)
        lay.addWidget(label('BeatSync uses a few free tools. They download once into your user folder, and the '
                            'link downloader keeps itself up to date.', muted=True, wrap=True, px=11))
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        lay.addLayout(self.rows)
        self.note = label('', tone='warn', wrap=True, px=11)
        lay.addWidget(self.note)
        self.skip = button('Skip for now')
        self.go = button('DOWNLOAD & INSTALL', kind='primary')
        self.go.setStyleSheet('font-size: 17px; padding: 8px 16px;')
        self.skip.clicked.connect(self.on_skip)
        self.go.clicked.connect(self.on_go)
        lay.addLayout(hbox(None, self.skip, self.go))
        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.timeout.connect(self.refresh)
        self.data = win.tools_data
        if self.data:
            self.render(self.data)
        self.refresh()

    def refresh(self):
        def got(d):
            self.data = d
            self.win.tools_data = d
            if self.isVisible() or not self.data:
                self.render(d)
            if d['busy']:
                self.poll.start(600)
        run_bg(lambda: self.win.engine.api('/api/tools'), ok=got, err=lambda e: None)

    def render(self, d):
        if self.choice is None:
            self.choice = {n: (not t['ok']) and t['installable'] for n, t in d['tools'].items()}
        clear_layout(self.rows)
        total = 0
        for n, t in ordered(d):
            row = QFrame()
            row.setStyleSheet(f'QFrame {{ border-top: 1px solid {T.BORDER}; }} QLabel, QCheckBox {{ border: none; }}')
            g = QGridLayout(row)
            g.setContentsMargins(0, 10, 0, 10)
            g.setHorizontalSpacing(10)
            cb = QCheckBox()
            cb.setChecked(t['ok'] or bool(self.choice.get(n)))
            cb.setEnabled(not (t['ok'] or not t['installable'] or t['need'] == 'required' or d['busy']))
            cb.toggled.connect(lambda on, n=n: (self.choice.__setitem__(n, on), self.render(self.data)))
            name = label(t['label'])
            tag = caps(NEED_LABEL.get(t['need'], ''), 9, color=T.ACCENT if t['need'] == 'required' else T.DIM)
            why = label(t['why'], muted=True, wrap=True, px=10)
            color, text = tool_state(t)
            st = label(text, px=10)
            st.setStyleSheet(f'color: {color};')
            st.setWordWrap(True)
            st.setMaximumWidth(200)
            st.setAlignment(Qt.AlignRight | Qt.AlignTop)
            g.addWidget(cb, 0, 0, Qt.AlignTop)
            g.addLayout(hbox(name, tag, None, spacing=6), 0, 1)
            g.addWidget(why, 1, 1)
            g.addWidget(st, 0, 2, 2, 1)
            g.setColumnStretch(1, 1)
            if t.get('state') in ('installing', 'queued'):
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(int(t.get('pct') or 0))
                bar.setTextVisible(False)
                g.addWidget(bar, 2, 1, 1, 2)
            self.rows.addWidget(row)
            if not t['ok'] and self.choice.get(n):
                total += t['size_mb']
        missing_req = bool(d['missing_required'])
        pending = [n for n, t in d['tools'].items() if not t['ok'] and self.choice.get(n)]
        if d['busy']:
            self.go.setEnabled(False)
            self.go.setText('INSTALLING…')
        elif not pending:
            self.go.setEnabled(True)
            self.go.setText(amp('DOWNLOAD & INSTALL') if missing_req else 'DONE')
        else:
            self.go.setEnabled(True)
            self.go.setText(amp(f'DOWNLOAD & INSTALL  (~{total} MB)'))
        errs = [t for t in d['tools'].values() if t.get('state') == 'error' and not t['ok']]
        self.note.setText('Some downloads failed — check your internet connection and press the button to retry.'
                          if errs else 'ffmpeg is required — matching will not work until it is installed.'
                          if missing_req else '')
        self.adjustSize()

    def on_skip(self):
        run_bg(lambda: self.win.engine.api('/api/settings', {'setup_dismissed': True}))
        self.accept()

    def on_go(self):
        d = self.data
        if not d:
            return
        names = [n for n, t in d['tools'].items() if not t['ok'] and self.choice.get(n) and t['installable']]
        if not names:
            if not d['missing_required']:
                run_bg(lambda: self.win.engine.api('/api/settings', {'setup_dismissed': True}))
                self.accept()
            return
        self.go.setEnabled(False)
        run_bg(lambda: self.win.engine.api('/api/tools/install', {'names': names}), ok=lambda _: self.refresh(),
               err=lambda e: self.note.setText(str(e)))


class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle('Settings')
        self.setModal(True)
        self.resize(520, min(640, max(420, win.height() - 40)))
        outer = vbox(margins=(0, 0, 0, 0))
        self.setLayout(outer)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName('page')
        self.lay = vbox(spacing=10, margins=(22, 16, 22, 18))
        body.setLayout(self.lay)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        t = QLabel('SETTINGS')
        t.setFont(T.display_font(26, 3))
        self.lay.addWidget(t)

        # tools
        self.section('Tools')
        self.tool_rows = QVBoxLayout()
        self.tool_rows.setSpacing(4)
        self.lay.addLayout(self.tool_rows)
        self.b_update = button('Update yt-dlp & Deno')
        self.b_missing = button('Install missing')
        b_folder = button('Open tools folder')
        self.b_update.clicked.connect(self.update_tools)
        self.b_missing.clicked.connect(self.install_missing)
        b_folder.clicked.connect(lambda: files.open_folder(win.ctx.tools_dir))
        self.lay.addLayout(hbox(self.b_update, self.b_missing, b_folder, None, spacing=6))
        self.tools_note = label('', dim=True, wrap=True, px=10)
        self.lay.addWidget(self.tools_note)

        # cookies
        self.section('YouTube sign-in')
        self.cookie_state = label('—', muted=True, px=11)
        self.lay.addWidget(self.cookie_state)
        b_imp, self.b_rm = button('Import cookies.txt'), button('Remove')
        b_imp.clicked.connect(self.import_cookies)
        self.b_rm.clicked.connect(self.remove_cookies)
        self.lay.addLayout(hbox(b_imp, self.b_rm, None, spacing=6))
        self.lay.addWidget(label('Only needed if YouTube says "Sign in to confirm you\'re not a bot". Export '
                                 'cookies.txt from your browser while logged in to YouTube (for example with the '
                                 '"Get cookies.txt LOCALLY" extension).', dim=True, wrap=True, px=10))

        # storage
        self.section('Storage')
        self.temp_size = label('—', muted=True, px=11)
        self.b_clear = button('Clear temp files')
        self.b_clear.clicked.connect(self.clear_temp)
        self.lay.addLayout(hbox(label('Temp files'), self.temp_size, None, self.b_clear, spacing=10))
        b_data, b_logs = button('Open data folder'), button('Open logs')
        b_data.clicked.connect(lambda: files.open_folder(win.ctx.data_dir))
        b_logs.clicked.connect(lambda: files.open_folder(win.ctx.logs_dir))
        self.lay.addLayout(hbox(b_data, b_logs, None, spacing=6))
        self.lay.addWidget(label('Temp files are also removed when you close BeatSync or press Start over.',
                                 dim=True, wrap=True, px=10))

        # about
        self.section('About')
        self.about_update = label('', muted=True, px=11)
        self.lay.addLayout(hbox(label(f'{win.ctx.name} {win.ctx.version}'), self.about_update, None, spacing=10))
        b_check, self.b_repo = button('Check for updates'), button('Source code')
        b_check.clicked.connect(lambda: (self.about_update.setText('checking…'),
                                         win.check_app_update(True, then=self.show_update)))
        self.b_repo.clicked.connect(self.open_repo)
        self.lay.addLayout(hbox(b_check, self.b_repo, None, spacing=6))
        self.lay.addWidget(label('Free software under the GNU GPL v3. Uses ffmpeg, Rubber Band, yt-dlp, Deno, '
                                 'librosa and Qt — see THIRD_PARTY_NOTICES.', dim=True, wrap=True, px=10))
        self.lay.addStretch(1)
        close = button('Close')
        close.clicked.connect(self.accept)
        self.lay.addLayout(hbox(None, close))

        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.timeout.connect(self.refresh)
        if win.tools_data:
            self.render(win.tools_data)
        if win.app_update:
            self.show_update(win.app_update)
        self.refresh()

    def section(self, title):
        line = QFrame()
        line.setObjectName('sep')
        self.lay.addSpacing(4)
        self.lay.addWidget(line)
        self.lay.addWidget(caps(title))

    def refresh(self):
        def got(d):
            self.win.tools_data = d
            self.render(d)
            if d['busy']:
                self.poll.start(600)
            elif getattr(self, '_was_busy', False):
                self.win.refresh_caps()
            self._was_busy = d['busy']
        run_bg(lambda: self.win.engine.api('/api/tools'), ok=got, err=lambda e: None)

    def render(self, d):
        clear_layout(self.tool_rows)
        for n, t in ordered(d):
            name = label(t['label'])
            name.setFixedWidth(150)
            if t['ok']:
                meta = label(t.get('version') or 'installed', muted=True, px=10)
            else:
                meta = label(t.get('error') if t.get('state') == 'error' else (t.get('msg') if t.get('state')
                             else 'not installed'), tone='warn', px=10)
            meta.setWordWrap(True)
            items = [name, meta, None]
            if t['ok'] and t.get('source'):
                b = caps({'app': 'downloaded', 'bundled': 'built-in', 'system': 'system'}.get(t['source'], ''),
                         9, color=T.DIM, spacing=1)
                items.append(b)
            if t['installable'] and t.get('source') not in ('system', 'bundled'):
                btn = button('Reinstall' if t['ok'] else 'Install')
                btn.setEnabled(not d['busy'])
                btn.clicked.connect(lambda _=False, n=n: self.install([n]))
                items.append(btn)
            self.tool_rows.addLayout(hbox(*items, spacing=8))
        self.b_update.setEnabled(not d['busy'] and any(t.get('updatable') for t in d['tools'].values()))
        self.b_missing.setEnabled(not d['busy'] and any(not t['ok'] and t['installable']
                                                         for t in d['tools'].values()))
        self.tools_note.setText('Working…' if d['busy'] else 'yt-dlp updates itself once a day in the background.')
        self.cookie_state.setText('✓ cookies.txt in use' if d.get('cookies') else 'No cookies (normal)')
        self.b_rm.setEnabled(bool(d.get('cookies')))
        self.temp_size.setText(f'{d.get("temp_mb", 0)} MB')
        self.b_clear.setEnabled(not d.get('jobs_active') and (d.get('temp_mb') or 0) > 0)

    def install(self, names):
        run_bg(lambda: self.win.engine.api('/api/tools/install', {'names': names}), ok=lambda _: self.refresh())

    def install_missing(self):
        self.install('missing')

    def update_tools(self):
        self.win.show_toast('Checking for tool updates…')
        run_bg(lambda: self.win.engine.api('/api/tools/update', {}), ok=lambda _: QTimer.singleShot(400, self.refresh))

    def import_cookies(self):
        src, _ = QFileDialog.getOpenFileName(self, 'Import cookies.txt', str(Path.home()),
                                             'Cookies file (*.txt);;All files (*.*)')
        if not src:
            return
        try:
            head = Path(src).read_text(encoding='utf-8', errors='replace')[:4000]
        except OSError as e:
            self.win.show_toast(str(e), 'err')
            return
        if 'Netscape' not in head and '\t' not in head:
            self.win.show_toast('That does not look like a cookies.txt (Netscape format) file.', 'err')
            return
        shutil.copyfile(src, Path(tools.data_dir()) / 'cookies.txt')
        self.win.show_toast('Cookies imported', 'ok')
        self.refresh()

    def remove_cookies(self):
        try:
            (Path(tools.data_dir()) / 'cookies.txt').unlink()
        except FileNotFoundError:
            pass
        self.win.show_toast('Cookies removed')
        self.refresh()

    def clear_temp(self):
        def done(r):
            self.temp_size.setText(f'{r.get("temp_mb", 0)} MB')
            self.win.show_toast('Temp files cleared', 'ok')
            self.win.reset_all()                   # the cleared session and picked files are gone
            self.refresh()
        self.b_clear.setEnabled(False)
        self.win._release_player()
        run_bg(lambda: self.win.engine.api('/api/temp/clear', {}), ok=done,
               err=lambda e: (self.win.show_toast(str(e), 'err'), self.refresh()))

    def show_update(self, u):
        if not u or not u.get('enabled'):
            self.about_update.setText('update checks off')
            self.b_repo.setEnabled(False)
        elif u.get('error'):
            self.about_update.setText('could not check for updates')
        elif u.get('newer'):
            self.about_update.setText(f'{u.get("latest")} available')
        else:
            self.about_update.setText('up to date')

    def open_repo(self):
        u = self.win.app_update or {}
        url = (u.get('url') or '').split('/releases')[0] or f'https://github.com/{self.win.ctx.repo}'
        import webbrowser
        webbrowser.open(url)
