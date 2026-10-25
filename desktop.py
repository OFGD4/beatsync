"""
BeatSync desktop app.

Runs the BeatSync engine (server.py) in the background on a private local port
and shows the UI in a native window (Microsoft Edge WebView2 on Windows).

    BeatSync.exe                 normal app window
    BeatSync.exe --headless      engine only; open the printed URL in a browser
    BeatSync.exe --debug         window with developer tools (right-click → Inspect)
    BeatSync.exe --selftest      check the engine, the page and the window component, then exit
"""

import os
import sys
import json
import time
import socket
import shutil
import logging
import argparse
import faulthandler
import threading
import subprocess
import webbrowser
from pathlib import Path

import tools
from app_info import APP_NAME, APP_VERSION, GITHUB_REPO

# ── Environment that must exist before the engine / librosa are imported ────
DATA = tools.data_dir()
os.environ.setdefault('NUMBA_CACHE_DIR', str(DATA / 'cache' / 'numba'))   # compiled code survives restarts
os.environ['BEATSYNC_TEMP'] = str(DATA / 'temp')
os.environ['BEATSYNC_DESKTOP'] = '1'
INSTANCE_PORT = 47823                                                   # single-instance lock
WEBVIEW2_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'


def setup_logging(to_console):
    log_path = tools.logs_dir() / 'beatsync.log'
    try:
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            os.replace(log_path, log_path.with_suffix('.log.1'))
    except OSError:
        pass
    stream = open(log_path, 'a', encoding='utf-8', buffering=1)
    if not to_console or sys.stdout is None:            # windowed .exe has no console
        sys.stdout = stream
        sys.stderr = stream
    logging.basicConfig(stream=stream, level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    try:
        faulthandler.enable(file=stream, all_threads=True)       # native crashes → traceback in the log
    except Exception:
        pass
    logging.info('%s %s starting (frozen=%s)', APP_NAME, APP_VERSION, tools.FROZEN)
    return log_path


def hide_child_consoles():
    """Stop ffmpeg / yt-dlp / rubberband from flashing console windows (also inside libraries)."""
    if os.name != 'nt':
        return
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        # Always add CREATE_NO_WINDOW, also when a caller passes other flags (e.g. a lower
        # CPU priority) — replacing only empty flags let those calls open a cmd window.
        flags = kwargs.get('creationflags') or 0
        if not flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
            flags |= subprocess.CREATE_NO_WINDOW
        kwargs['creationflags'] = flags
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched


def message_box(title, text, yes_no=False):
    """Native message box. Returns True for Yes / OK."""
    logging.info('dialog: %s | %s', title, text.replace('\n', ' '))
    if os.name == 'nt':
        import ctypes
        flags = (0x04 | 0x30) if yes_no else 0x40            # MB_YESNO|MB_ICONWARNING  /  MB_ICONINFORMATION
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000) in (1, 6)   # MB_SETFOREGROUND
    print(f'{title}: {text}')
    return False


def attach_debug_console():
    """--debug on Windows: open a console window and print the log there as well."""
    if os.name != 'nt':
        return
    import ctypes
    if ctypes.windll.kernel32.AllocConsole():
        con = open('CONOUT$', 'w', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = con
        logging.getLogger().addHandler(logging.StreamHandler(con))


def hook_dotnet_errors(log_path):
    """The Windows window runs on .NET; report its unhandled errors instead of vanishing."""
    if os.name != 'nt':
        return
    try:
        import clr  # noqa: F401  (pythonnet — pywebview loads it anyway)
        from System import AppDomain

        def on_unhandled(_sender, e):
            logging.error('.NET unhandled exception: %s', e.ExceptionObject)
            message_box(APP_NAME, ('BeatSync crashed while opening its window:\n\n'
                                   f'{e.ExceptionObject}'[:1200] + f'\n\nLog: {log_path}'))

        AppDomain.CurrentDomain.UnhandledException += on_unhandled
    except Exception:
        logging.exception('could not hook .NET errors')


# ── Single instance ──────────────────────────────────────────────────────────
# The first copy holds a local socket. A second launch asks it to come to the
# front. If the first copy is stuck (window never appeared), the second launch
# offers to end it instead of silently doing nothing.

PID_FILE = DATA / 'instance.pid'
STATE = {'window': None, 'shown': False, 'started': time.time()}


def _bind_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == 'nt':
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # ignore old TIME_WAIT connections
    try:
        s.bind(('127.0.0.1', INSTANCE_PORT))
        s.listen(4)
        return s
    except OSError:
        s.close()
        return None


def _ask_running_instance():
    """Returns 'ok' (focused), 'starting', 'stuck', 'none' (no answer) or 'refused' (nobody listening)."""
    try:
        with socket.create_connection(('127.0.0.1', INSTANCE_PORT), timeout=3) as c:
            c.settimeout(3)
            c.sendall(b'focus')
            return c.recv(16).decode('ascii', 'replace') or 'none'
    except ConnectionRefusedError:
        return 'refused'
    except OSError:
        return 'none'


def _is_beatsync(pid):
    """Make sure a PID really is BeatSync before ever ending it (PIDs get reused)."""
    try:
        if os.name == 'nt':
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True, timeout=10).stdout.lower()
            return 'beatsync' in out or (not tools.FROZEN and 'python' in out)
        cmd = Path(f'/proc/{pid}/cmdline').read_bytes().decode('utf-8', 'replace').lower()
        return 'beatsync' in cmd or 'desktop.py' in cmd
    except Exception:
        return False


def _old_instance_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid() or not _is_beatsync(pid):
        return None
    return pid


def _kill_old_instance(pid):
    logging.warning('ending unresponsive instance pid %s', pid)
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            return False
    return True


def _retry_bind(seconds):
    for _ in range(int(seconds * 2)):
        time.sleep(0.5)
        s = _bind_lock()
        if s:
            return s
    return None


def acquire_instance_lock():
    """Returns (proceed, lock_socket_or_None)."""
    s = _bind_lock()
    if s is None:
        answer = _ask_running_instance()
        logging.info('lock busy, other side answered: %s', answer)
        if answer in ('ok', 'starting'):
            return False, None                           # it is alive and now in front
        if answer == 'refused':
            # Nobody is listening: the port is only briefly blocked (or used by another
            # program). Never refuse to start because of that.
            s = _retry_bind(3)
            if s is None:
                logging.warning('single-instance port unavailable, continuing without it')
                return True, None
        else:
            old = _old_instance_pid()
            if old is None:                              # the port belongs to some other program
                logging.warning('single-instance port used by another program, continuing without it')
                return True, None
            if not message_box(APP_NAME, 'BeatSync is already running but is not responding.\n\n'
                                         'Close it and start a new one?', yes_no=True):
                return False, None
            _kill_old_instance(old)
            s = _retry_bind(10)
            if s is None:
                logging.warning('could not take over from the old instance, continuing without the lock')
                return True, None
    try:
        PID_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    return True, s


def listen_for_focus(lock_sock):
    def loop():
        while True:
            try:
                conn, _ = lock_sock.accept()
            except OSError:
                return
            with conn:
                try:
                    if conn.recv(16) != b'focus':
                        continue
                    w = STATE['window']
                    if w is not None and STATE['shown']:
                        w.restore()
                        w.show()
                        w.on_top = True
                        w.on_top = False
                        conn.sendall(b'ok')
                    elif time.time() - STATE['started'] < 60:
                        conn.sendall(b'starting')
                    else:
                        conn.sendall(b'stuck')
                    conn.settimeout(2)
                    try:
                        conn.recv(1)      # let the caller hang up first (keeps our port free of TIME_WAIT)
                    except OSError:
                        pass
                except Exception:
                    logging.exception('focus request failed')
    threading.Thread(target=loop, daemon=True).start()


# ── Engine ───────────────────────────────────────────────────────────────────

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_engine(port):
    import server
    from werkzeug.serving import make_server
    server.wipe_temp()                                   # leftovers from a crash / forced shutdown
    httpd = make_server('127.0.0.1', port, server.app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    for _ in range(100):                                 # wait until it answers
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return server, httpd


def warm_up():
    """Compile librosa's numba code in the background (cached to disk after the first launch)."""
    t0 = time.time()
    try:
        import numpy as np
        import librosa
        import numba
        t1 = time.time()
        logging.info('librosa imported in %.1fs (numba cache: %s)', t1 - t0, numba.config.CACHE_DIR or 'in-tree')
        y = (np.random.RandomState(0).randn(22050 * 4) * 0.1).astype('float32')
        oenv = librosa.onset.onset_strength(y=y, sr=22050, hop_length=256)
        librosa.beat.beat_track(onset_envelope=oenv, sr=22050, hop_length=256)
        librosa.feature.tempogram(onset_envelope=oenv, sr=22050, hop_length=256)
        librosa.beat.plp(onset_envelope=oenv, sr=22050, hop_length=256)
        librosa.effects.hpss(y[:22050])
        logging.info('warm-up done in %.1fs', time.time() - t0)
    except Exception:
        logging.exception('warm-up failed')


# ── JavaScript bridge (window.pywebview.api.* in the page) ───────────────────

FIRST_SIZE = (900, 620)          # first launch: compact and centred; the user can maximise
MIN_SIZE = (800, 520)


def initial_window_size():
    """First-open window size in logical pixels (what pywebview expects): 900×620, but never more
    than 92 % of the free desktop area, so small or high-scaling screens (e.g. a 1080p laptop at
    150 %) get a window that fits instead of a near-maximised one."""
    w, h = FIRST_SIZE
    if os.name != 'nt':
        return w, h
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):     # SPI_GETWORKAREA
            return w, h
        try:
            dpi = user32.GetDpiForSystem()
        except Exception:
            hdc = user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)                         # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
        scale = (dpi or 96) / 96.0
        # Work area and DPI come from the same DPI context, so this is logical pixels either way.
        aw, ah = (rect.right - rect.left) / scale, (rect.bottom - rect.top) / scale
        w, h = int(min(w, aw * 0.92)), int(min(h, ah * 0.92))
        logging.info('window: work area %dx%d at %d%% -> %dx%d', aw, ah, round(scale * 100), w, h)
    except Exception:
        logging.exception('window size: using the default')
    return w, h


def save_folder():
    """Where the Save-as dialog opens: the last folder used, else Downloads, else home."""
    last = tools._load_state().get('last_save_dir')
    for d in (last, Path.home() / 'Downloads', Path.home()):
        if d and Path(d).is_dir():
            return Path(d)
    return None


def remember_save_folder(folder):
    if folder and os.path.isdir(folder):
        tools._save_state(last_save_dir=str(folder))


def free_name(folder, name):
    """name, or 'stem (1).ext', 'stem (2).ext' … — the first one not already in `folder`
    (so saving never asks to replace an earlier export)."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while (Path(folder) / candidate).exists():
        candidate = f'{stem} ({n}){ext}'
        n += 1
    return candidate


def place_file(src, dest):
    """Put a finished file at `dest`. On the same drive this is a hard link: instant and no extra
    disk space, even for a multi-GB export (the temp name is simply removed later). Otherwise —
    another drive, FAT/exFAT, a synced folder that refuses links — a normal copy."""
    part = dest + '.beatsync-part'
    try:
        if os.path.exists(part):
            os.remove(part)
        os.link(src, part)
        os.replace(part, dest)
        return
    except OSError:
        try:
            os.remove(part)
        except OSError:
            pass
    shutil.copyfile(src, dest)



class Api:
    def __init__(self):
        self._window = None
        self._server = None

    def _dialog(self, kind):
        import webview
        fd = getattr(webview, 'FileDialog', None)
        if fd is not None:
            return {'save': fd.SAVE, 'open': fd.OPEN}[kind]
        return {'save': webview.SAVE_DIALOG, 'open': webview.OPEN_DIALOG}[kind]

    @staticmethod
    def _first(result):
        if not result:
            return None
        return result if isinstance(result, str) else result[0]

    def info(self):
        return {'name': APP_NAME, 'version': APP_VERSION, 'repo': GITHUB_REPO, 'data_dir': str(DATA)}

    def save(self, kind, job_id):
        """Native Save-as dialog for a finished download. The suggested name is never one that
        already exists in that folder: syncedVid.mp4 → syncedVid (1).mp4 → syncedVid (2).mp4 …"""
        try:
            path, name = self._server.resolve_file(kind, job_id)
        except Exception as e:
            logging.warning('save %s %s: %s', kind, job_id, e)
            return {'ok': False, 'error': str(e)}
        ext = os.path.splitext(name)[1]
        types = ('Video (*.mp4)',) if ext == '.mp4' else ('MP3 audio (*.mp3)',)
        folder = save_folder()
        dest = self._first(self._window.create_file_dialog(
            self._dialog('save'), directory=str(folder) if folder else '',
            save_filename=free_name(folder, name) if folder else name, file_types=types))
        if not dest:
            return {'ok': False, 'cancelled': True}
        if not dest.lower().endswith(ext):
            dest += ext
        try:
            place_file(path, dest)
        except OSError as e:
            logging.warning('save to %s failed: %s', dest, e)
            return {'ok': False, 'error': f'Could not save: {e}'}
        remember_save_folder(os.path.dirname(dest))
        return {'ok': True, 'path': dest}

    def pick_file(self, src):
        """Native Open dialog. The file is used where it is — no upload and no copy, so even a
        multi-GB video is ready instantly."""
        audio = '*.mp3;*.wav;*.flac;*.aac;*.ogg;*.m4a;*.opus'
        if src == 'vid':
            types = (f'Video or audio (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v;{audio})', 'All files (*.*)')
        else:
            types = (f'Audio ({audio})', 'All files (*.*)')
        path = self._first(self._window.create_file_dialog(self._dialog('open'), file_types=types))
        if not path:
            return {'ok': False, 'cancelled': True}
        try:
            return dict(self._server.register_local(path), ok=True)
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def reveal(self, path):
        """Show a file in Explorer / Finder."""
        if not path or not os.path.exists(path):
            return False
        if os.name == 'nt':
            subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', path])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(path)])
        return True

    def open_folder(self, which):
        target = {'data': DATA, 'logs': tools.logs_dir(), 'tools': tools.USER_BIN}.get(which)
        if not target:
            return False
        Path(target).mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(str(target))                    # noqa: S606 (local folder)
        else:
            subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(target)])
        return True

    def import_cookies(self):
        src = self._first(self._window.create_file_dialog(
            self._dialog('open'), file_types=('Cookies file (*.txt)', 'All files (*.*)')))
        if not src:
            return {'ok': False, 'cancelled': True}
        try:
            head = Path(src).read_text(encoding='utf-8', errors='replace')[:4000]
        except OSError as e:
            return {'ok': False, 'error': str(e)}
        if 'Netscape' not in head and '\t' not in head:
            return {'ok': False, 'error': 'That does not look like a cookies.txt (Netscape format) file.'}
        shutil.copyfile(src, DATA / 'cookies.txt')
        return {'ok': True}

    def remove_cookies(self):
        try:
            (DATA / 'cookies.txt').unlink()
        except FileNotFoundError:
            pass
        return {'ok': True}

    def open_url(self, url):
        if isinstance(url, str) and url.startswith(('https://', 'http://')):
            webbrowser.open(url)
            return True
        return False

    def fullscreen(self, on):
        """Real full screen for the preview (the page itself makes the player fill the window)."""
        w = self._window
        if w is None:
            return False
        on = bool(on)
        if STATE.get('fullscreen', False) != on:
            STATE['fullscreen'] = on
            try:
                w.toggle_fullscreen()
            except Exception:
                logging.exception('full screen toggle failed')
        return True

    def install_update(self):
        if not tools.FROZEN:
            return {'ok': False, 'error': 'Updates can only be installed in the installed app.'}
        if self._server._active_jobs():
            return {'ok': False, 'error': 'Finish or cancel the running render first.'}
        return tools.start_app_update(on_ready=self._run_setup)

    def update_status(self):
        return tools.app_update_status()

    def pop_updated_from(self):
        old = STATE.pop('updated_from', '')
        return {'from': old, 'to': APP_VERSION} if old else None

    def _run_setup(self, path):
        log = tools.logs_dir() / 'update-setup.log'
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        subprocess.Popen([str(path), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                          '/CLOSEAPPLICATIONS', '/FORCECLOSEAPPLICATIONS', f'/LOG={log}'], env=env)
        logging.info('update: started %s, quitting', path)
        try:
            self._server.wipe_temp()
            PID_FILE.unlink(missing_ok=True)
        finally:
            os._exit(0)


# ── Window extras ────────────────────────────────────────────────────────────

def dark_title_bar():
    """Windows 10/11: a dark title bar to match the app (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        pid, found = os.getpid(), []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == APP_NAME:
                    found.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        on = ctypes.c_int(1)
        for hwnd in found:
            for attr in (20, 19):                        # Windows 11 / 10 2004+, then older Windows 10
                if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)   # redraw the frame
    except Exception:
        logging.exception('dark title bar failed')


def bind_native_drop(window, server):
    """Files dropped on the window are used where they are — no upload, no copy — because
    pywebview hands over their real path. (The page falls back to uploading if this is missing.)"""
    try:
        from webview.dom import DOMEventHandler
    except Exception:
        logging.info('native drop unavailable in this pywebview')
        return

    def on_drop(e):
        out = []
        for f in ((e or {}).get('dataTransfer') or {}).get('files') or []:
            path = f.get('pywebviewFullPath')
            if not path:
                continue
            try:
                out.append(dict(server.register_local(path), ok=True))
            except Exception as ex:
                out.append({'ok': False, 'name': os.path.basename(path), 'error': str(ex)})
        if out:
            window.evaluate_js('window.beatsyncNativeDrop && window.beatsyncNativeDrop(%s)' % json.dumps(out))

    def bind():
        try:
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, False)
            window.evaluate_js('window.beatsyncNativeDropReady = true')
            logging.info('native file drop ready')
        except Exception:
            logging.exception('native drop binding failed')

    window.events.loaded += bind


def selftest(log_path):
    """Release-build check: the engine starts, the page and its files load, the window component imports."""
    try:
        hide_child_consoles()
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))       # never via a proxy
        port = free_port()
        server, httpd = start_engine(port)
        base = f'http://127.0.0.1:{port}'
        page = opener.open(base + '/', timeout=30).read().decode('utf-8')
        if 'BEATSYNC' not in page:
            raise RuntimeError('the page did not load')
        for name in ('fonts/bebas-neue-latin-400-normal.woff2', 'fonts/dm-mono-latin-400-normal.woff2', 'icon-256.png'):
            opener.open(f'{base}/assets/{name}', timeout=30).read()
        caps = json.loads(opener.open(base + '/api/caps', timeout=120).read())
        import webview  # noqa: F401
        if os.name == 'nt':
            import clr  # noqa: F401  (pythonnet)
            from webview.platforms import winforms  # noqa: F401  (the WebView2 window)
        httpd.shutdown()
        server.wipe_temp()
        logging.info('selftest ok: %s %s, effects=%d', APP_NAME, APP_VERSION, len(caps.get('effects', [])))
        return 0
    except Exception:
        logging.exception('selftest failed')
        return 1


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument('--headless', action='store_true', help='run the engine only (no window)')
    ap.add_argument('--port', type=int, default=0, help='engine port (default: random free port)')
    ap.add_argument('--debug', action='store_true', help='console + developer tools (right-click → Inspect)')
    ap.add_argument('--selftest', action='store_true', help='check the build and exit (used by the release build)')
    args, _unknown = ap.parse_known_args()

    log_path = setup_logging(to_console=args.headless)
    if args.debug:
        attach_debug_console()
    if args.selftest:
        return selftest(log_path)
    try:
        return run(args, log_path)
    except SystemExit:
        raise
    except BaseException as e:                              # never fail silently
        logging.exception('fatal error')
        message_box(APP_NAME, f'BeatSync could not start:\n\n{type(e).__name__}: {e}\n\n'
                              f'Details are in the log:\n{log_path}')
        return 1


def run(args, log_path):
    hide_child_consoles()

    proceed, lock = acquire_instance_lock()
    if not proceed:
        return 0
    if lock is not None:
        listen_for_focus(lock)

    port = args.port or (5002 if args.headless else free_port())
    logging.info('starting engine on port %s', port)
    server, httpd = start_engine(port)
    logging.info('engine ready')
    threading.Thread(target=warm_up, daemon=True).start()
    threading.Thread(target=tools.update_if_due, daemon=True).start()
    url = f'http://127.0.0.1:{port}/'

    if args.headless:
        server.banner(port)
        print(f'  Log: {log_path}')
        STATE['shown'] = True
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.wipe_temp()
        return 0

    logging.info('loading window component')
    import webview
    hook_dotnet_errors(log_path)
    STATE['updated_from'] = tools.just_updated_from() if tools.FROZEN else ''
    api = Api()
    api._server = server
    width, height = initial_window_size()
    window = webview.create_window(
        APP_NAME, url=url, js_api=api, width=width, height=height,
        min_size=(min(MIN_SIZE[0], width), min(MIN_SIZE[1], height)),   # opens centred; can be maximised
        background_color='#080810', text_select=True)
    api._window = window
    STATE['window'] = window
    bind_native_drop(window, server)

    def on_shown():
        STATE['shown'] = True
        logging.info('window shown')
        dark_title_bar()

    def on_closing():
        if server._active_jobs() and hasattr(window, 'create_confirmation_dialog'):
            return bool(window.create_confirmation_dialog(
                'Quit BeatSync?', 'A render is still running. Quit anyway?'))
        return True

    window.events.shown += on_shown
    window.events.closing += on_closing

    def watchdog():
        time.sleep(45)
        if STATE['shown']:
            return
        logging.error('window did not appear within 45 s')
        if message_box(APP_NAME, 'BeatSync started, but its window did not appear.\n\n'
                                 'This is almost always the Microsoft Edge WebView2 Runtime being missing or '
                                 'broken. Open the download page to install / repair it?\n\n'
                                 f'Log: {log_path}', yes_no=True):
            webbrowser.open(WEBVIEW2_URL)
        try:
            server.wipe_temp()
        finally:
            os._exit(1)                                     # don't leave a hidden copy running
    threading.Thread(target=watchdog, daemon=True).start()

    logging.info('starting window (gui=%s)', 'edgechromium' if os.name == 'nt' else 'default')
    try:
        # Windows window icon: the packaged .exe already carries it (pywebview reads it from
        # there). Never pass a PNG on Windows — .NET's Icon() rejects it and the app dies.
        opts = {}
        if os.name != 'nt':
            opts['icon'] = str(tools.resource_dir() / 'assets' / 'beatsync.png')
        elif not tools.FROZEN:
            opts['icon'] = str(tools.resource_dir() / 'assets' / 'beatsync.ico')
        webview.start(gui='edgechromium' if os.name == 'nt' else None, debug=args.debug,
                      private_mode=True, **opts)
    except Exception as e:
        logging.exception('window failed to start')
        if os.name == 'nt' and message_box(
                APP_NAME, f'BeatSync could not open its window:\n{type(e).__name__}: {e}\n\n'
                          'This is usually the Microsoft Edge WebView2 Runtime. '
                          'Open the download page to install / repair it?', yes_no=True):
            webbrowser.open(WEBVIEW2_URL)
        return 1
    finally:
        try:
            server.wipe_temp()
            httpd.shutdown()
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            logging.exception('shutdown cleanup failed')
        logging.info('bye')
    return 0


if __name__ == '__main__':
    sys.exit(main())
