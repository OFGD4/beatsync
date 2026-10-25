"""Preview player with browser-style controls on top of the picture — native Qt, no web view.

The picture is a QGraphicsVideoItem (a normal widget surface, so controls can float over it).
Playback speed here only changes how the preview plays; it is never exported (the Speed tab is).
"""

from PySide6.QtCore import Qt, QTimer, QEvent, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPolygonF, QActionGroup
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (QWidget, QFrame, QGraphicsView, QGraphicsScene, QSlider, QToolButton, QMenu,
                               QInputDialog, QApplication, QAbstractButton)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

from . import theme as T
from .widgets import label, hbox

RATES = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


def fmt(ms):
    s = max(0, int(ms // 1000))
    return f'{s // 60}:{s % 60:02d}'


def rate_text(r):
    return f'{r:g}×' if abs(r - 1.0) > 1e-3 else '1×'


class BigPlay(QAbstractButton):
    """The round play button in the middle of a paused video."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(64, 64)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('Play (Space)')

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 8, 16, 170 if not self.underMouse() else 215))
        p.drawEllipse(QRectF(1, 1, 62, 62))
        p.setBrush(QColor(T.ACCENT))
        p.drawPolygon(QPolygonF([QPointF(26, 20), QPointF(26, 44), QPointF(46, 32)]))


class VideoPlayer(QWidget):
    fullscreen_toggled = Signal()

    def __init__(self, player: QMediaPlayer, fullscreen=False, parent=None):
        super().__init__(parent)
        self.player = player
        self.fullscreen = fullscreen
        self.setMouseTracking(True)
        self.setMinimumHeight(140)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName('player')
        self.setStyleSheet('QWidget#player { background: #000; border-radius: 6px; }')

        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(Qt.black)
        self.item = QGraphicsVideoItem()
        self.item.setAspectRatioMode(Qt.KeepAspectRatio)
        self.scene.addItem(self.item)
        self.view = QGraphicsView(self.scene, self)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setStyleSheet('background: #000; border: none;')
        self.view.setFocusPolicy(Qt.NoFocus)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)

        # controls bar
        self.bar = QFrame(self)
        self.bar.setObjectName('ctrlbar')
        self.bar.setStyleSheet(
            'QFrame#ctrlbar { background: rgba(8,8,16,0.82); border-radius: 6px; }'
            'QToolButton { background: transparent; border: none; color: #e0e0f0; padding: 3px 6px; font-size: 13px; }'
            'QToolButton:hover { color: #ff2d6b; }'
            'QToolButton::menu-indicator { image: none; width: 0; }'
            'QToolButton#rate { font-size: 11px; border: 1px solid #2a2a42; border-radius: 4px; padding: 2px 7px; }')
        self.play = QToolButton()
        self.play.setText('▶')
        self.play.setToolTip('Play / pause (Space)')
        self.play.clicked.connect(self.toggle)
        self.time = label('0:00 / 0:00', px=10)
        self.time.setStyleSheet('color: #c8c8dc; background: transparent;')
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setObjectName('pos')
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.seek.sliderPressed.connect(lambda: self.player.setPosition(self.seek.value()))
        self.seek.sliderReleased.connect(lambda: self.player.setPosition(self.seek.value()))
        self.rate = QToolButton()
        self.rate.setObjectName('rate')
        self.rate.setPopupMode(QToolButton.InstantPopup)
        self.rate.setToolTip('Playback speed — only for watching the preview; the export is not changed.\n'
                             'To change the speed of the video itself, use the Speed tab.')
        self._build_rate_menu()
        self.fs = QToolButton()
        self.fs.setText('⤢' if not fullscreen else '⤡')
        self.fs.setToolTip('Exit full screen (Esc)' if fullscreen else 'Full screen (double-click the video)')
        self.fs.clicked.connect(self.fullscreen_toggled.emit)
        self.bar.setLayout(hbox(self.play, self.time, self.seek, self.rate, self.fs, spacing=8, margins=(8, 4, 8, 4)))

        self.big = BigPlay(self)
        self.big.clicked.connect(self.toggle)
        self.big.hide()

        self._hide_t = QTimer(self)
        self._hide_t.setSingleShot(True)
        self._hide_t.timeout.connect(self._auto_hide)
        self._click_t = QTimer(self)
        self._click_t.setSingleShot(True)
        self._click_t.timeout.connect(self.toggle)

        player.positionChanged.connect(self._on_pos)
        player.durationChanged.connect(self._on_dur)
        player.playbackStateChanged.connect(self._on_state)
        player.playbackRateChanged.connect(lambda r: self.rate.setText(rate_text(r)))
        player.sourceChanged.connect(lambda _: self._on_state(player.playbackState()))
        self.rate.setText(rate_text(player.playbackRate()))
        self._on_state(player.playbackState())

    # ── wiring ────────────────────────────────────────────────────────────
    def activate(self):
        self.player.setVideoOutput(self.item)

    def _build_rate_menu(self):
        m = QMenu(self.rate)
        grp = QActionGroup(m)
        self._rate_actions = {}
        for r in RATES:
            a = m.addAction('Normal' if r == 1.0 else rate_text(r))
            a.setCheckable(True)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, r=r: self.set_rate(r))
            self._rate_actions[r] = a
        m.addSeparator()
        custom = m.addAction('Custom…')
        custom.triggered.connect(self.custom_rate)
        m.aboutToShow.connect(self._mark_rate)
        self.rate.setMenu(m)

    def _mark_rate(self):
        cur = self.player.playbackRate()
        for r, a in self._rate_actions.items():
            a.setChecked(abs(r - cur) < 1e-3)

    def set_rate(self, r):
        self.player.setPlaybackRate(float(r))

    def custom_rate(self):
        v, ok = QInputDialog.getDouble(self, 'Playback speed',
                                       'Speed for watching the preview (the export is not changed):',
                                       self.player.playbackRate(), 0.1, 4.0, 2)
        if ok:
            self.set_rate(v)

    def toggle(self):
        if self.player.source().isEmpty():
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    # ── state → controls ──────────────────────────────────────────────────
    def _on_pos(self, ms):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.time.setText(f'{fmt(ms)} / {fmt(self.player.duration())}')

    def _on_dur(self, d):
        self.seek.setRange(0, max(0, d))
        self.time.setText(f'{fmt(self.player.position())} / {fmt(d)}')

    def _on_state(self, st):
        playing = st == QMediaPlayer.PlayingState
        self.play.setText('❚❚' if playing else '▶')
        has = not self.player.source().isEmpty()
        self.big.setVisible(has and not playing)
        if playing:
            self._hide_t.start(2200)
        else:
            self.show_controls(keep=True)

    def show_controls(self, keep=False):
        self.bar.show()
        self.bar.raise_()
        self.big.raise_()
        self.unsetCursor()
        if not keep and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._hide_t.start(2200)

    def _auto_hide(self):
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        if self.bar.underMouse() or self.rate.menu().isVisible():
            self._hide_t.start(1500)
            return
        self.bar.hide()
        if self.fullscreen:
            self.setCursor(Qt.BlankCursor)

    # ── layout / mouse ────────────────────────────────────────────────────
    def resizeEvent(self, e):
        super().resizeEvent(e)
        r = self.rect()
        self.view.setGeometry(r)
        self.scene.setSceneRect(0, 0, r.width(), r.height())
        self.item.setPos(0, 0)
        self.item.setSize(r.size().toSizeF())
        bh = self.bar.sizeHint().height()
        m = 8 if self.fullscreen else 6
        self.bar.setGeometry(m, r.height() - bh - m, r.width() - 2 * m, bh)
        self.big.move((r.width() - self.big.width()) // 2, (r.height() - self.big.height()) // 2)
        self.bar.raise_()
        self.big.raise_()

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseMove:
            self.show_controls()
        elif t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            self._click_t.start(QApplication.doubleClickInterval())       # single click = play / pause
            return True
        elif t == QEvent.MouseButtonDblClick and e.button() == Qt.LeftButton:
            self._click_t.stop()                                            # double click = full screen
            self.fullscreen_toggled.emit()
            return True
        elif t == QEvent.Leave and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._hide_t.start(600)
        return False

    def mouseMoveEvent(self, e):
        self.show_controls()
        super().mouseMoveEvent(e)


class FullscreenPlayer(QWidget):
    """The preview filling the screen, with the same controls. Esc, F or ⤡ to leave."""
    closed = Signal()

    def __init__(self, player, screen=None):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle('BeatSync — preview')
        self.setObjectName('fs')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet('QWidget#fs { background: #000; }')
        self.view = VideoPlayer(player, fullscreen=True, parent=self)
        self.view.fullscreen_toggled.connect(self.close)
        lay = hbox(self.view, margins=(0, 0, 0, 0))
        self.setLayout(lay)
        if screen is not None:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())

    def keyPressEvent(self, e):
        k = e.key()
        p = self.view.player
        if k in (Qt.Key_Escape, Qt.Key_F):
            self.close()
        elif k == Qt.Key_Space:
            self.view.toggle()
        elif k in (Qt.Key_Left, Qt.Key_Right):
            step = 5000 if k == Qt.Key_Right else -5000
            p.setPosition(max(0, min(p.duration(), p.position() + step)))
            self.view.show_controls()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)
