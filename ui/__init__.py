"""Native BeatSync window (Qt for Python)."""

import os
import sys
from pathlib import Path

FIRST_SIZE = (900, 620)          # first launch: compact and centred; the user can maximise
MIN_SIZE = (800, 520)


def create_app(font_dir, icon_path=None):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QPalette, QColor, QIcon
    from PySide6.QtWidgets import QApplication
    from . import theme as T

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('BeatSync')
    app.setStyle('Fusion')
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)      # dark title bar on Windows 10/11 (Qt 6.8+)
    except Exception:
        pass
    pal = QPalette()
    for role, color in ((QPalette.Window, T.BG), (QPalette.WindowText, T.TEXT), (QPalette.Base, T.BG),
                        (QPalette.AlternateBase, T.SURFACE), (QPalette.Text, T.TEXT), (QPalette.Button, T.SURFACE),
                        (QPalette.ButtonText, T.TEXT), (QPalette.Highlight, T.ACCENT),
                        (QPalette.HighlightedText, '#ffffff'), (QPalette.ToolTipBase, T.SURFACE2),
                        (QPalette.ToolTipText, T.TEXT), (QPalette.PlaceholderText, T.DIM), (QPalette.Link, T.INFO)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    T.load_fonts(font_dir)
    f = app.font()
    f.setFamily(T.MONO)
    f.setPixelSize(12)
    app.setFont(f)
    app.setStyleSheet(T.stylesheet(Path(font_dir).parent))
    if icon_path and os.path.exists(icon_path):
        app.setWindowIcon(QIcon(str(icon_path)))
    return app


def show_main_window(server, ctx):
    from PySide6.QtGui import QGuiApplication
    from .engine import init_relay
    from .main_window import MainWindow

    init_relay()
    w = MainWindow(server, ctx)
    scr = QGuiApplication.primaryScreen().availableGeometry()       # logical pixels (DPI-aware)
    width = min(FIRST_SIZE[0], int(scr.width() * 0.92))
    height = min(FIRST_SIZE[1], int(scr.height() * 0.92))
    w.setMinimumSize(min(MIN_SIZE[0], width), min(MIN_SIZE[1], height))
    w.resize(width, height)
    w.move(scr.x() + (scr.width() - width) // 2, scr.y() + (scr.height() - height) // 2)
    w.show()
    _dark_title_bar(w)
    return w


def _dark_title_bar(w):
    """Older Qt / Windows builds: ask DWM directly for a dark title bar."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        hwnd = int(w.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):                  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / pre-20H1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass
