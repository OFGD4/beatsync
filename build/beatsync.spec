# PyInstaller spec for the BeatSync desktop app (native Qt window, engine in-process).
# Run from the repository root:
#     python -m PyInstaller build/beatsync.spec --noconfirm --clean --workpath .pyi-work --distpath dist
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

datas = [
    (os.path.join(ROOT, 'assets'), 'assets'),      # fonts (TTF), icons, check mark
]
datas += collect_data_files('librosa')          # lazy-loader .pyi stubs + data registry

# Qt modules BeatSync never uses (keeps the download smaller)
QT_UNUSED = ['QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebEngineQuick', 'QtWebChannel', 'QtWebSockets',
             'QtQml', 'QtQuick', 'QtQuick3D', 'QtQuickWidgets', 'QtQuickControls2', 'Qt3DCore', 'Qt3DRender',
             'Qt3DInput', 'Qt3DLogic', 'Qt3DAnimation', 'Qt3DExtras', 'QtCharts', 'QtDataVisualization',
             'QtGraphs', 'QtPdf', 'QtPdfWidgets', 'QtDesigner', 'QtHelp', 'QtSql', 'QtTest', 'QtBluetooth',
             'QtNfc', 'QtPositioning', 'QtLocation', 'QtSensors', 'QtSerialPort', 'QtSerialBus',
             'QtRemoteObjects', 'QtScxml', 'QtStateMachine', 'QtTextToSpeech', 'QtSpatialAudio',
             'QtHttpServer', 'QtUiTools', 'QtXml', 'QtConcurrent', 'QtDBus', 'QtNetworkAuth', 'QtOpenGLWidgets']

a = Analysis(
    [os.path.join(ROOT, 'desktop.py')],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=['server', 'tools', 'app_info',
                   'ui', 'ui.main_window', 'ui.dialogs', 'ui.engine', 'ui.files', 'ui.theme', 'ui.widgets',
                   'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets'] + collect_submodules('librosa'),
    excludes=['matplotlib', 'tkinter', 'IPython', 'pandas', 'pytest', 'yt_dlp', 'notebook', 'sphinx',
              'PyQt5', 'PyQt6', 'PySide2', 'webview', 'clr', 'pythonnet'] + ['PySide6.' + m for m in QT_UNUSED],
    # numba caches compiled code next to the source; ship librosa's .py files so
    # that cache works (first launch compiles, later launches start fast).
    module_collection_mode={'librosa': 'pyz+py'},
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='BeatSync',
    console=False,
    icon=os.path.join(ROOT, 'assets', 'beatsync.ico'),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='BeatSync', upx=False)
