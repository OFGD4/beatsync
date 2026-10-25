"""Small custom widgets used across the window."""

from PySide6.QtCore import Qt, QSize, QRect, QPoint, QRectF, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QPen, QLinearGradient, QFontMetrics, QPainterPath
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
                               QWidget, QLayout, QSizePolicy, QButtonGroup, QGraphicsOpacityEffect)

from . import theme as T


def label(text='', muted=False, dim=False, tone=None, wrap=False, px=None, obj=None):
    w = QLabel(text)
    if muted:
        w.setProperty('muted', True)
    if dim:
        w.setProperty('dim', True)
    if tone:
        w.setProperty('tone', tone)
    if wrap:
        w.setWordWrap(True)
    if px:
        f = w.font()
        f.setPixelSize(px)
        w.setFont(f)
    if obj:
        w.setObjectName(obj)
    return w


def caps(text, px=10, color=T.MUTED, spacing=1.6):
    w = QLabel(text.upper())
    w.setFont(T.caps_font(px, spacing))
    w.setStyleSheet(f'color: {color};')
    return w


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def set_prop(w, name, value):
    if w.property(name) != value:
        w.setProperty(name, value)
        repolish(w)


def amp(text):
    """'&' in a button label is a keyboard-shortcut marker in Qt; show it as a real '&'."""
    return text.replace('&', '&&')


def button(text, kind=None, tip=None, checkable=False):
    b = QPushButton(amp(text))
    if kind:
        b.setProperty('kind', kind)
    if tip:
        b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.TabFocus)
    return b


def hbox(*items, spacing=8, margins=(0, 0, 0, 0)):
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


def vbox(*items, spacing=8, margins=(0, 0, 0, 0)):
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


class Card(QFrame):
    """Rounded panel. Optional header: step number · coloured dot · TITLE … right-hand widget."""

    def __init__(self, title=None, step=None, dot=None, right=None, padding=(12, 10, 12, 12), spacing=8):
        super().__init__()
        self.setObjectName('card')
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*padding)
        self.body.setSpacing(spacing)
        if title:
            row = QHBoxLayout()
            row.setSpacing(8)
            if step:
                n = QLabel(str(step))
                n.setFont(T.display_font(15, 1))
                n.setStyleSheet(f'color: {T.DIM};')
                row.addWidget(n)
            if dot:
                d = QLabel()
                d.setFixedSize(6, 6)
                d.setStyleSheet(f'background: {dot}; border-radius: 3px;')
                row.addWidget(d)
            row.addWidget(caps(title))
            row.addStretch(1)
            if right is not None:
                row.addWidget(right)
            self.body.addLayout(row)

    def add(self, w, stretch=0):
        if isinstance(w, QLayout):
            self.body.addLayout(w, stretch)
        else:
            self.body.addWidget(w, stretch)
        return w


class GradientTitle(QWidget):
    """The BEATSYNC word mark, painted with the pink → orange gradient."""

    def __init__(self, text='BEATSYNC', px=30):
        super().__init__()
        self.text = text
        self.font_ = T.display_font(px, 2.5)
        fm = QFontMetrics(self.font_)
        self.setFixedSize(fm.horizontalAdvance(text) + 6, fm.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        fm = QFontMetrics(self.font_)
        path.addText(0, fm.ascent(), self.font_, self.text)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0, QColor(T.ACCENT))
        g.setColorAt(1, QColor(T.ACCENT2))
        p.fillPath(path, g)


class Toggle(QAbstractButton):
    """On/off switch with an optional label to its right."""

    def __init__(self, text='', checked=False, small=False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setText(text)
        self.setCursor(Qt.PointingHandCursor)
        self._small = small
        self._tw, self._th = (24, 13) if small else (30, 16)
        f = self.font()
        f.setPixelSize(11 if small else 12)
        self.setFont(f)

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        w = self._tw + (8 + fm.horizontalAdvance(self.text()) if self.text() else 0)
        return QSize(w + 2, max(self._th, fm.height()) + 4)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        y = (h - self._th) / 2
        on = self.isChecked()
        track = QColor(45, 255, 176, 64) if on else QColor(T.BORDER2)
        if not self.isEnabled():
            track = QColor(T.BORDER)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, y, self._tw, self._th), self._th / 2, self._th / 2)
        k = self._th - 4
        kx = self._tw - k - 2 if on else 2
        p.setBrush(QColor(T.SUCCESS) if on and self.isEnabled() else QColor(T.MUTED))
        p.drawEllipse(QRectF(kx, y + 2, k, k))
        if self.text():
            p.setPen(QColor(T.TEXT if on else T.MUTED))
            p.setFont(self.font())
            p.drawText(QRect(self._tw + 8, 0, self.width() - self._tw - 8, h), Qt.AlignVCenter | Qt.AlignLeft,
                       self.text())
        if self.hasFocus():
            p.setPen(QPen(QColor(T.INFO), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(0.5, y - 1.5, self._tw - 1, self._th + 3), 8, 8)


class Segmented(QFrame):
    """A row of mutually exclusive buttons. `options` = [(value, label), …]."""
    changed = Signal(str)

    def __init__(self, options, value=None, tone=None, stretch=False):
        super().__init__()
        self.setObjectName('seg')
        if tone:
            self.setProperty('tone', tone)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for i, (val, text) in enumerate(options):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.TabFocus)
            if i == 0:
                b.setProperty('first', True)
            if i == len(options) - 1:
                b.setProperty('last', True)
            if stretch:
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(b)
            self.buttons[str(val)] = b
            lay.addWidget(b)
            b.clicked.connect(lambda _=False, v=str(val): self.changed.emit(v))
        if value is not None:
            self.set_value(value)

    def value(self):
        for v, b in self.buttons.items():
            if b.isChecked():
                return v
        return None

    def set_value(self, value):
        b = self.buttons.get(str(value))
        if b:
            b.setChecked(True)


class FlowLayout(QLayout):
    """Wraps its items onto new lines (preset chips)."""

    def __init__(self, spacing=6):
        super().__init__()
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do(self, rect, test):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + self._sp
            if nx - self._sp > rect.right() and line > 0:
                x, y = rect.x(), y + line + self._sp
                nx, line = x + hint.width() + self._sp, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x, line = nx, max(line, hint.height())
        return y + line - rect.y()


class Spinner(QWidget):
    def __init__(self, size=10, color=T.ACCENT):
        super().__init__()
        self.setFixedSize(size, size)
        self._a = 0
        self._color = QColor(color)
        self._t = QTimer(self)
        self._t.timeout.connect(self._tick)
        self.hide()

    def _tick(self):
        self._a = (self._a + 30) % 360
        self.update()

    def start(self):
        self.show()
        self._t.start(60)

    def stop(self):
        self._t.stop()
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        p.setPen(QPen(QColor(T.BORDER2), 2))
        p.drawEllipse(r)
        p.setPen(QPen(self._color, 2))
        p.drawArc(r, -self._a * 16, 100 * 16)


class Leds(QWidget):
    """Beats of the bar: the current one lights up, the downbeat in pink, a flash on each hit."""

    def __init__(self):
        super().__init__()
        self.n, self.cur, self.hit = 4, -1, False
        self.setFixedHeight(12)
        self._sync()

    def _sync(self):
        self.setFixedWidth(self.n * 12 + (self.n - 1) * 5)

    def set_count(self, n):
        self.n = max(1, min(8, n))
        self._sync()
        self.update()

    def set_state(self, cur, hit):
        if cur != self.cur or hit != self.hit:
            self.cur, self.hit = cur, hit
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for i in range(self.n):
            r = QRectF(i * 17, 0, 12, 12)
            if i == self.cur:
                c = QColor(T.ACCENT if i == 0 else T.ACCENT2)
                if self.hit:
                    glow = QColor(c)
                    glow.setAlpha(90)
                    p.setBrush(glow)
                    p.drawEllipse(r.adjusted(-2, -2, 2, 2) if r.x() >= 2 else r)
            else:
                c = QColor(T.BORDER2)
            p.setBrush(c)
            p.drawEllipse(r)


class Toast(QLabel):
    """Short message that floats at the bottom of the window and fades out."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.hide()
        self._fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b'opacity', self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)

    def show_msg(self, text, tone=None, ms=2800):
        color = {'ok': T.SUCCESS, 'err': T.ACCENT}.get(tone, T.TEXT)
        border = {'ok': T.SUCCESS, 'err': T.ACCENT}.get(tone, T.BORDER2)
        self.setStyleSheet(f'background: {T.SURFACE2}; color: {color}; border: 1px solid {border};'
                           f'border-radius: 6px; padding: 8px 16px; font-size: 11px;')
        self.setText(text)
        par = self.parentWidget()
        self.setMaximumWidth(min(520, par.width() - 40))
        self.adjustSize()
        self.move((par.width() - self.width()) // 2, par.height() - self.height() - 18)
        self._anim.stop()
        self._fx.setOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start(ms)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.hide)
        self._anim.start()


class ElidedLabel(QLabel):
    """One line; long text ends in … and the full text is the tooltip."""

    def __init__(self, text=''):
        super().__init__()
        self._full = ''
        self.setMinimumWidth(10)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_text(text)

    def set_text(self, text):
        self._full = text or ''
        self.setToolTip(self._full if len(self._full) > 40 else '')
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideRight, max(10, self.width())))


class DoubleClickLabel(QLabel):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()
