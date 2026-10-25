"""Colours, fonts and the Qt stylesheet. Same identity as before: near-black, pink → orange."""

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

BG = '#080810'
SURFACE = '#0f0f1a'
SURFACE2 = '#13131f'
BORDER = '#1f1f33'
BORDER2 = '#2a2a42'
ACCENT = '#ff2d6b'
ACCENT2 = '#ff8c2d'
TEXT = '#e0e0f0'
MUTED = '#6a6a88'
DIM = '#44445a'
SUCCESS = '#2dffb0'
INFO = '#2d9fff'
WARN = '#ffcc2d'

DISPLAY = 'Bebas Neue'          # headings, big numbers, main buttons
MONO = 'DM Mono'                # everything else


def stylesheet(asset_dir: Path):
    return QSS.replace('@CHECK@', (Path(asset_dir) / 'check.svg').as_posix())


def load_fonts(font_dir: Path):
    for f in sorted(font_dir.glob('*.ttf')):
        QFontDatabase.addApplicationFont(str(f))


def display_font(px, spacing=1.5):
    f = QFont(DISPLAY)
    f.setPixelSize(px)
    f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


def caps_font(px=10, spacing=1.6, weight=QFont.Normal):
    """Small tracked-out labels (section names, row labels)."""
    f = QFont(MONO)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


QSS = f"""
* {{ color: {TEXT}; outline: none; }}
QMainWindow, QDialog, QWidget#root, QWidget#page {{ background: {BG}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel {{ background: transparent; }}
QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel[dim="true"] {{ color: {DIM}; }}
QLabel[tone="warn"] {{ color: {WARN}; }}
QLabel[tone="err"] {{ color: {ACCENT}; }}
QLabel[tone="ok"] {{ color: {SUCCESS}; }}
QLabel[tone="info"] {{ color: {INFO}; }}
QLabel#val {{ color: {ACCENT2}; }}

QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#card[focus="true"] {{ border-color: {ACCENT}; }}
QFrame#card[tone="ok"] {{ border-color: rgba(45,255,176,0.45); }}
QFrame#footer {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QFrame#banner {{ background: rgba(45,159,255,0.10); border-bottom: 1px solid rgba(45,159,255,0.35); }}
QFrame#errbox {{ background: rgba(255,45,107,0.08); border: 1px solid {ACCENT}; border-radius: 6px; }}
QFrame#warnbox {{ background: rgba(255,204,45,0.07); border: 1px solid rgba(255,204,45,0.4); border-radius: 6px; }}
QFrame#tile {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#readout {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#video {{ background: #000; border-radius: 6px; }}
QFrame#sep {{ background: {BORDER}; max-height: 1px; min-height: 1px; }}

QPushButton {{ background: transparent; border: 1px solid {BORDER2}; border-radius: 5px; color: {MUTED};
              padding: 6px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:pressed {{ background: rgba(255,45,107,0.08); }}
QPushButton:disabled {{ color: #34344a; border-color: {BORDER}; }}
QPushButton[kind="primary"] {{ border: none; border-radius: 7px; color: #fff; padding: 9px 16px;
    font-family: "{DISPLAY}"; font-size: 21px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QPushButton[kind="primary"]:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ff4a80, stop:1 #ffa04d); }}
QPushButton[kind="primary"]:disabled {{ background: #2a1a24; color: #6a4a58; }}
QPushButton[kind="nudge"] {{ background: {BG}; color: {TEXT}; border-radius: 6px; padding: 8px 6px; }}
QPushButton[kind="nudge"][big="true"] {{ font-family: "{DISPLAY}"; font-size: 17px; }}
QPushButton[kind="nudge"]:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton[kind="nudge"]:disabled {{ color: #34344a; border-color: {BORDER}; background: {BG}; }}
QPushButton[kind="dl"] {{ background: {SUCCESS}; color: #000; border: none; border-radius: 5px; padding: 7px 14px;
    font-family: "{DISPLAY}"; font-size: 16px; }}
QPushButton[kind="dl"]:hover {{ background: #fff; color: #000; }}
QPushButton[kind="dl2"] {{ background: transparent; color: {SUCCESS}; border: 1px solid {SUCCESS}; border-radius: 5px;
    padding: 6px 14px; font-family: "{DISPLAY}"; font-size: 16px; }}
QPushButton[kind="dl2"]:hover {{ background: {SUCCESS}; color: #000; }}
QPushButton[kind="dl"]:disabled, QPushButton[kind="dl2"]:disabled {{ background: transparent; color: #1f5c47;
    border: 1px solid #1a4436; }}
QPushButton[kind="tab"] {{ border: none; border-bottom: 2px solid transparent; border-radius: 0; color: {MUTED};
    padding: 7px 8px; font-size: 11px; }}
QPushButton[kind="tab"]:hover {{ color: {TEXT}; }}
QPushButton[kind="tab"]:checked {{ color: {TEXT}; border-bottom-color: {ACCENT}; }}
QPushButton[kind="link"] {{ border: none; color: {INFO}; padding: 0; text-decoration: underline; }}
QPushButton[kind="icon"] {{ background: {BG}; padding: 4px 8px; border-radius: 5px; }}
QPushButton[kind="fx"] {{ background: {BG}; border-radius: 6px; padding: 5px 2px; color: {MUTED}; font-size: 10px; }}
QPushButton[kind="fx"]:hover {{ border-color: {MUTED}; color: {TEXT}; }}
QPushButton[kind="fx"]:checked {{ border-color: {ACCENT}; background: rgba(255,45,107,0.10); color: {ACCENT}; }}
QPushButton[kind="chip"] {{ background: {BG}; border-radius: 11px; padding: 3px 10px; font-size: 11px; color: {MUTED}; }}
QPushButton[kind="chip"]:hover {{ border-color: {ACCENT2}; color: {ACCENT2}; }}
QPushButton[kind="chip"]:checked {{ border-color: {ACCENT2}; background: rgba(255,140,45,0.12); color: {ACCENT2}; }}
QPushButton[kind="mode"] {{ background: {BG}; border-radius: 6px; padding: 7px 9px; text-align: left; }}
QPushButton[kind="mode"]:checked {{ border-color: {ACCENT}; background: rgba(255,45,107,0.07); }}
QPushButton[kind="gear"] {{ background: {SURFACE}; padding: 5px 9px; border-radius: 6px; font-size: 13px; }}
QPushButton[kind="drop"] {{ border: 1px dashed {BORDER2}; border-radius: 5px; padding: 9px 8px; color: {MUTED};
    background: transparent; }}
QPushButton[kind="drop"]:hover, QPushButton[kind="drop"][over="true"] {{ border-color: {ACCENT}; color: {TEXT};
    background: rgba(255,45,107,0.04); }}
QPushButton[kind="drop"][has="true"] {{ border: 1px solid {SUCCESS}; color: {SUCCESS}; }}

QFrame#seg {{ border: 1px solid {BORDER2}; border-radius: 5px; background: transparent; }}
QFrame#seg QPushButton {{ border: none; border-left: 1px solid {BORDER2}; border-radius: 0; padding: 4px 9px;
    color: {MUTED}; font-size: 11px; }}
QFrame#seg QPushButton[first="true"] {{ border-left: none; border-top-left-radius: 4px; border-bottom-left-radius: 4px; }}
QFrame#seg QPushButton[last="true"] {{ border-top-right-radius: 4px; border-bottom-right-radius: 4px; }}
QFrame#seg QPushButton:hover:!checked {{ color: {TEXT}; background: rgba(255,255,255,0.03); }}
QFrame#seg QPushButton:checked {{ background: {ACCENT}; color: #fff; }}
QFrame#seg[tone="orange"] QPushButton:checked {{ background: {ACCENT2}; color: #000; }}
QFrame#seg[tone="blue"] QPushButton:checked {{ background: {INFO}; color: #000; }}

QLineEdit {{ background: {BG}; border: 1px solid {BORDER2}; border-radius: 5px; padding: 5px 8px;
    selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled {{ color: {DIM}; }}

QSlider::groove:horizontal {{ height: 3px; background: {BORDER2}; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ height: 3px; background: rgba(255,45,107,0.6); border-radius: 1px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 13px; margin: -5px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: #ff5a8a; }}
QSlider#pos::sub-page:horizontal {{ background: {ACCENT}; }}
QSlider#pos::handle:horizontal {{ background: {TEXT}; width: 11px; margin: -4px 0; border-radius: 5px; }}

QProgressBar {{ background: {BORDER}; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ border-radius: 2px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 1px; }}
QScrollBar::handle:vertical {{ background: {BORDER2}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {DIM}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 1px 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER2}; border-radius: 3px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER2}; padding: 4px 6px; }}
QMenu {{ background: {SURFACE2}; border: 1px solid {BORDER2}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 24px; border-radius: 4px; color: {TEXT}; }}
QMenu::item:selected {{ background: rgba(255,45,107,0.20); }}
QMenu::indicator {{ width: 12px; height: 12px; left: 6px; }}
QMenu::indicator:checked {{ image: url("@CHECK@"); }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QAbstractSpinBox {{ background: {BG}; border: 1px solid {BORDER2}; border-radius: 5px; padding: 4px 7px;
    color: {ACCENT2}; selection-background-color: {ACCENT}; }}
QAbstractSpinBox:focus {{ border-color: {ACCENT}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; border: none; }}
QInputDialog QLabel {{ color: {TEXT}; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT}; }}
QMessageBox QPushButton {{ min-width: 72px; color: {TEXT}; }}
QCheckBox {{ spacing: 7px; color: {TEXT}; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {BORDER2}; border-radius: 3px; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url("@CHECK@"); }}
QCheckBox::indicator:disabled {{ background: {BORDER}; border-color: {BORDER}; }}
"""
