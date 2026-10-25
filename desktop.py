"""
BeatSync desktop app.

The engine (server.py) runs inside this process and the window is a native Qt window — no web
page, no browser component and no local web server.

    BeatSync.exe              the app
    BeatSync.exe --debug      with a console that shows the log
    BeatSync.exe --selftest   load the engine and the window, check video playback support, exit
"""

import os
import sys
import time
import socket
import logging
import argparse
import faulthandler
import threading
import subprocess
from pathlib import Path
from types import SimpleNamespace

import tools
from app_info import APP_NAME, APP_VERSION, GITHUB_REPO

# ── Environment that must exist before the engine / librosa are imported ────
DATA = tools.data_dir()
os.environ.setdefault('NUMBA_CACHE_DIR', str(DATA / 'cache' / 'numba'))   # compiled code survives restarts
os.environ['BEATSYNC_TEMP'] = str(DATA / 'temp')
os.environ['BEATSYNC_DESKTOP'] = '1'
INSTANCE_PORT = 47823                                                   # single-instance lock


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
    """A second launch asks this copy to come to the front."""
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
                        w.raise_requested.emit()              # handled on the GUI thread
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


def run_setup(path):
    """Self-update: start the downloaded installer and quit (it replaces this copy)."""
    log = tools.logs_dir() / 'update-setup.log'
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    subprocess.Popen([str(path), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                      '/CLOSEAPPLICATIONS', '/FORCECLOSEAPPLICATIONS', f'/LOG={log}'], env=env)
    logging.info('update: started %s, quitting', path)
    try:
        import server
        server.wipe_temp()
        PID_FILE.unlink(missing_ok=True)
    finally:
        os._exit(0)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument('--debug', action='store_true', help='show a console with the log')
    ap.add_argument('--selftest', action='store_true', help='check the build and exit (used by the release build)')
    args, _unknown = ap.parse_known_args()

    log_path = setup_logging(to_console=False)
    if args.debug:
        attach_debug_console()
    if args.selftest:
        return selftest(log_path)
    try:
        return run(log_path)
    except SystemExit:
        raise
    except BaseException as e:                              # never fail silently
        logging.exception('fatal error')
        message_box(APP_NAME, f'BeatSync could not start:\n\n{type(e).__name__}: {e}\n\n'
                              f'Details are in the log:\n{log_path}')
        return 1


def _context(log_path):
    return SimpleNamespace(
        name=APP_NAME, version=APP_VERSION, repo=GITHUB_REPO, frozen=tools.FROZEN,
        data_dir=DATA, logs_dir=tools.logs_dir(), tools_dir=tools.USER_BIN, log_path=log_path,
        run_setup=run_setup, updated_from=tools.just_updated_from() if tools.FROZEN else '')


def _create_app():
    import ui
    res = tools.resource_dir() / 'assets'
    return ui.create_app(res / 'fonts', res / ('beatsync.ico' if os.name == 'nt' else 'icon-256.png'))


def selftest(log_path):
    """Release-build check: engine imports, the window builds, the video backend is bundled."""
    try:
        hide_child_consoles()
        import server
        app = _create_app()
        from PySide6.QtMultimedia import QMediaFormat
        from ui.engine import init_relay, Engine
        from ui.main_window import MainWindow
        init_relay()
        caps = Engine(server).api('/api/caps')
        window = MainWindow(server, _context(log_path))
        decodable = QMediaFormat().supportedFileFormats(QMediaFormat.Decode)
        if QMediaFormat.MPEG4 not in decodable:
            raise RuntimeError(f'video playback backend missing (decodable formats: {decodable})')
        window.close()
        app.processEvents()
        logging.info('selftest ok: %s %s, effects=%d, formats=%d', APP_NAME, APP_VERSION,
                     len(caps.get('effects', [])), len(decodable))
        return 0
    except Exception:
        logging.exception('selftest failed')
        return 1


def run(log_path):
    hide_child_consoles()

    proceed, lock = acquire_instance_lock()
    if not proceed:
        return 0
    if lock is not None:
        listen_for_focus(lock)

    logging.info('loading engine')
    import server
    server.wipe_temp()                                   # leftovers from a crash / forced shutdown
    threading.Thread(target=warm_up, daemon=True).start()
    threading.Thread(target=tools.update_if_due, daemon=True).start()

    logging.info('opening window')
    import ui
    app = _create_app()
    try:
        window = ui.show_main_window(server, _context(log_path))
        STATE['window'] = window
        STATE['shown'] = True
        logging.info('window shown')
        return app.exec()
    finally:
        try:
            server.wipe_temp()
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            logging.exception('shutdown cleanup failed')
        logging.info('bye')


if __name__ == '__main__':
    sys.exit(main())
