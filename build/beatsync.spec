# PyInstaller spec for the BeatSync desktop app.
# Run from the repository root:
#     python -m PyInstaller build/beatsync.spec --noconfirm --clean --workpath .pyi-work --distpath dist
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

datas = [
    (os.path.join(ROOT, 'templates'), 'templates'),
    (os.path.join(ROOT, 'assets'), 'assets'),
]
datas += collect_data_files('librosa')          # lazy-loader .pyi stubs + data registry

a = Analysis(
    [os.path.join(ROOT, 'desktop.py')],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=['server', 'tools', 'app_info'] + collect_submodules('librosa'),
    excludes=['matplotlib', 'tkinter', 'IPython', 'pandas', 'pytest', 'yt_dlp', 'notebook',
              'sphinx', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
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
