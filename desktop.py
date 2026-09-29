"""
BeatSync desktop app.

Runs the BeatSync engine (server.py) in the background on a private local port
and shows the UI in a native window (Microsoft Edge WebView2 on Windows).

    BeatSync.exe                 normal app window
    BeatSync.exe --headless      engine only; open the printed URL in a browser
    BeatSync.exe --debug         window with developer tools (right-click → Inspect)
"""

import os
import sys
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
        if not kwargs.get('creationflags'):
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
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
        """Native Save-as dialog for a finished download."""
        try:
            path, name = self._server.resolve_file(kind, job_id)
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        ext = os.path.splitext(name)[1]
        types = ('Video (*.mp4)',) if ext == '.mp4' else ('MP3 audio (*.mp3)',)
        downloads = Path.home() / 'Downloads'
        dest = self._first(self._window.create_file_dialog(
            self._dialog('save'), directory=str(downloads) if downloads.is_dir() else '',
            save_filename=name, file_types=types))
        if not dest:
            return {'ok': False, 'cancelled': True}
        if not dest.lower().endswith(ext):
            dest += ext
        try:
            shutil.copyfile(path, dest)
        except OSError as e:
            return {'ok': False, 'error': f'Could not save: {e}'}
        return {'ok': True, 'path': dest}

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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument('--headless', action='store_true', help='run the engine only (no window)')
    ap.add_argument('--port', type=int, default=0, help='engine port (default: random free port)')
    ap.add_argument('--debug', action='store_true', help='console + developer tools (right-click → Inspect)')
    args, _unknown = ap.parse_known_args()

    log_path = setup_logging(to_console=args.headless)
    if args.debug:
        attach_debug_console()
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
    window = webview.create_window(
        APP_NAME, url=url, js_api=api, width=1200, height=900, min_size=(880, 640),
        background_color='#080810', text_select=True)
    api._window = window
    STATE['window'] = window

    def on_shown():
        STATE['shown'] = True
        logging.info('window shown')

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
