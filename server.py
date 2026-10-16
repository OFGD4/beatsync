"""
BEATSYNC v3 — beat-align any song to any video.

Pipeline
  1. /api/start   → analyse both sources, find the beat-aligned offset, stretch,
                    render a preview, then the full export.
  2. /api/adjust  → every later change (nudge, grid-lock, seek, pitch, look,
                    audio, export options) creates a *derived* job that re-uses
                    the analysed audio. No re-analysis, so it is fast.
                    A new adjust cancels any still-running render in the same
                    session, so rapid clicking never piles up renders.

Old endpoints (/api/nudge, /api/seek, /api/repitch, /api/rerender) still work
and are thin wrappers around /api/adjust.
"""

from flask import Flask, Request, request, jsonify, send_file, send_from_directory, Response
from flask_cors import CORS
import subprocess
import threading
import queue
import json
import uuid
import os
import re
import copy
import time
import math
import shutil
import tempfile
import hashlib
import collections
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from werkzeug.utils import secure_filename

import tools
from app_info import APP_NAME, APP_VERSION

tools.setup_path()                      # bundled / downloaded tools first on PATH
APP_DIR = tools.resource_dir()          # templates live here (inside the bundle when frozen)
PORT = int(os.environ.get('BEATSYNC_PORT', '5002'))
HOST = os.environ.get('BEATSYNC_HOST', '127.0.0.1')     # set 0.0.0.0 to allow other devices on your network
TEMP_ROOT = os.environ.get('BEATSYNC_TEMP') or None     # desktop app: %LOCALAPPDATA%\BeatSync\temp
MAX_ANALYSIS_WORKERS = int(os.environ.get('BEATSYNC_WORKERS', '2'))
JOB_TTL_SEC = 2 * 60 * 60

class _UploadRequest(Request):
    """Spool uploaded files straight into the BeatSync temp folder, so a multi-GB
    video is written once and then just renamed (not copied through the system temp)."""
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        if TEMP_ROOT:
            os.makedirs(TEMP_ROOT, exist_ok=True)
        return tempfile.NamedTemporaryFile('wb+', dir=TEMP_ROOT, prefix='beatsync_spool_', delete=False)


app = Flask(__name__)
app.request_class = _UploadRequest
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 ** 3    # uploads go straight to disk; local app

# cookies.txt (Netscape format) helps with YouTube "Sign in to confirm" checks.
# Looked up in the user data folder first (Settings → Import cookies), then next to the app.

ALLOWED_VIDEO = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v'}
ALLOWED_AUDIO = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus'}

jobs = {}
jobs_lock = threading.Lock()
_analysis_slots = threading.BoundedSemaphore(MAX_ANALYSIS_WORKERS)


class Cancelled(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities (checked once at start-up)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_caps():
    caps = {'rubberband': bool(shutil.which('rubberband')), 'amix_normalize': False,
            'ffmpeg': shutil.which('ffmpeg') is not None, 'ffmpeg_version': ''}
    try:
        out = subprocess.run(['ffmpeg', '-hide_banner', '-h', 'filter=amix'],
                             capture_output=True, text=True, timeout=10).stdout
        caps['amix_normalize'] = 'normalize' in out
        ver = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=10).stdout
        caps['ffmpeg_version'] = ver.split('\n')[0].replace('ffmpeg version ', '').split(' ')[0]
    except Exception:
        pass
    return caps


CAPS = _detect_caps()


def refresh_caps():
    CAPS.update(_detect_caps())


def _mkdtemp(prefix):
    if TEMP_ROOT:
        os.makedirs(TEMP_ROOT, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=TEMP_ROOT)


def wipe_temp():
    """Remove every session and everything under TEMP_ROOT (desktop: at start-up and on exit)."""
    with jobs_lock:
        roots = {j.get('root_id') for j in jobs.values() if j.get('root_id')}
    for r in roots:
        _delete_lineage(r)
    with _inflight_lock:
        roots_inflight = list(_inflight)
    for r in roots_inflight:
        _cancel_inflight(r)
    with jobs_lock:
        ups = [jobs.pop(k) for k, j in list(jobs.items()) if j.get('kind') == 'upload']
    for up in ups:
        if up.get('tmp'):                                # picked files (external) are never touched
            shutil.rmtree(up['tmp'], ignore_errors=True)
    if TEMP_ROOT and os.path.isdir(TEMP_ROOT):
        for name in os.listdir(TEMP_ROOT):
            p = os.path.join(TEMP_ROOT, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)                         # leftover upload spool files
                except OSError:
                    pass


def temp_usage_mb():
    base = TEMP_ROOT
    if not base or not os.path.isdir(base):
        with jobs_lock:
            dirs = [j['tmp'] for j in jobs.values() if j.get('tmp')]
    else:
        dirs = [base]
    total, seen = 0, set()
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    s = os.stat(os.path.join(root, f))
                except OSError:
                    continue
                ident = (s.st_dev, s.st_ino)
                if s.st_ino and ident in seen:       # hard links (shared uploads) count once
                    continue
                seen.add(ident)
                total += s.st_size
    return round(total / 1e6, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Render options — everything the user can change after analysis
# ─────────────────────────────────────────────────────────────────────────────

EFFECTS = ['punch', 'zoom_pulse', 'shake', 'rotation', 'flash', 'strobe', 'invert_flash',
           'color_pop', 'color_shift', 'rgb_split', 'glitch', 'blur_pulse', 'vignette',
           'scanlines', 'mirror']
EFFECT_ALIASES = {'edge_glow': 'color_pop'}
DIVISIONS = {'bar': None, 'beat': 1.0, 'half': 0.5, 'quarter': 0.25}
LOCK_UNITS = {'bar': None, 'beat': 1.0, 'half': 0.5, 'quarter': 0.25, 'eighth': 0.125}
ASPECTS = {'original': None, '9:16': (9, 16), '1:1': (1, 1), '4:5': (4, 5), '16:9': (16, 9)}
QUALITY = {'fast': ('veryfast', 23), 'balanced': ('fast', 20), 'high': ('slow', 17)}

FILTER_RANGES = {            # name: (default, min, max)
    'brightness': (0.0, -0.5, 0.5),
    'contrast':   (1.0, 0.5, 2.0),
    'saturation': (1.0, 0.0, 3.0),
    'gamma':      (1.0, 0.4, 2.5),
    'warmth':     (0.0, -1.0, 1.0),
    'tint':       (0.0, -1.0, 1.0),
    'hue':        (0.0, -180.0, 180.0),
    'splittone':  (0.0, 0.0, 1.0),
    'fade':       (0.0, 0.0, 1.0),
    'sharpness':  (0.0, 0.0, 4.0),
    'denoise':    (0.0, 0.0, 1.0),
    'vignette':   (0.0, 0.0, 1.0),
    'grain':      (0.0, 0.0, 1.0),
}


def _default_opts():
    return {
        'song_vol': 1.0,
        'hflip': False,
        'filters': {k: v[0] for k, v in FILTER_RANGES.items()},
        'effects': [],
        'fx_division': 'beat',
        'fx_intensity': 1.0,
        'aspect': 'original',
        'fade_out': 0.0,
        'quality': 'balanced',
        'preview_sec': 15,
    }


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return default


def _clean_opts(raw, base=None):
    """Merge user-supplied options onto `base`, validating every field."""
    o = copy.deepcopy(base) if base else _default_opts()
    if not isinstance(raw, dict):
        return o
    if 'song_vol' in raw:
        o['song_vol'] = _clamp(raw['song_vol'], 0.0, 1.0, o['song_vol'])
    if 'hflip' in raw:
        o['hflip'] = bool(raw['hflip'])
    if isinstance(raw.get('filters'), dict):
        for k, (d, lo, hi) in FILTER_RANGES.items():
            if k in raw['filters']:
                o['filters'][k] = _clamp(raw['filters'][k], lo, hi, d)
    if isinstance(raw.get('effects'), list):
        fx = []
        for e in raw['effects']:
            e = EFFECT_ALIASES.get(str(e), str(e))
            if e in EFFECTS and e not in fx:
                fx.append(e)
        o['effects'] = fx
    if raw.get('fx_division') in DIVISIONS:
        o['fx_division'] = raw['fx_division']
    if 'fx_intensity' in raw:
        o['fx_intensity'] = _clamp(raw['fx_intensity'], 0.25, 2.0, 1.0)
    if raw.get('aspect') in ASPECTS:
        o['aspect'] = raw['aspect']
    if 'fade_out' in raw:
        o['fade_out'] = _clamp(raw['fade_out'], 0.0, 5.0, 0.0)
    if raw.get('quality') in QUALITY:
        o['quality'] = raw['quality']
    if 'preview_sec' in raw:
        o['preview_sec'] = int(_clamp(raw['preview_sec'], 0, 120, 15))
    return o


# ─────────────────────────────────────────────────────────────────────────────
# Job bookkeeping
# ─────────────────────────────────────────────────────────────────────────────

def _new_job(kind, root_id=None, state=None):
    jid = str(uuid.uuid4())
    now = time.time()
    with jobs_lock:
        jobs[jid] = {
            'kind': kind, 'status': 'pending', 'queue': queue.Queue(),
            'created_at': now, 'touched_at': now,
            'root_id': root_id or jid, 'tmp': None, 'out_dir': None, 'state': state,
            'file_mp4': None, 'file_preview': None, 'file_synced_mp3': None,
            'cancel': threading.Event(), 'procs': [], 'pct': 0,
        }
    return jid


def _push(job_id, msg):
    with jobs_lock:
        j = jobs.get(job_id)
        if j and 'queue' in j:
            j['queue'].put(msg)


def _step(job_id, msg, pct):
    # Never move the bar backwards (video and song are prepared at the same time).
    with jobs_lock:
        j = jobs.get(job_id)
        if j is not None:
            pct = max(int(pct), j.get('pct', 0))
            j['pct'] = pct
    _push(job_id, {'type': 'progress', 'msg': msg, 'pct': int(pct)})


def _touch(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
        if j:
            root = jobs.get(j.get('root_id'))
            now = time.time()
            j['touched_at'] = now
            if root:
                root['touched_at'] = now


def _check_cancel(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j or j['cancel'].is_set():
        raise Cancelled()


def _kill_procs(j):
    for p in list(j.get('procs', [])):
        try:
            p.kill()
        except Exception:
            pass


def _cancel_job(j):
    j['cancel'].set()
    _kill_procs(j)


def _cancel_lineage(root_id, except_id=None):
    with jobs_lock:
        victims = [j for jid, j in jobs.items()
                   if j.get('root_id') == root_id and jid != except_id
                   and j.get('status') in ('pending', 'running', 'preview')]
    for j in victims:
        _cancel_job(j)


def _delete_lineage(root_id):
    with jobs_lock:
        ids = [jid for jid, j in jobs.items() if j.get('root_id') == root_id]
        victims = [jobs.pop(jid) for jid in ids]
    for j in victims:
        _cancel_job(j)
        if j.get('tmp'):
            _cancel_inflight(j['tmp'])                   # the session's background export encode
    for j in victims:
        if j.get('tmp'):
            shutil.rmtree(j['tmp'], ignore_errors=True)


def _prune(job_id, keep_last_done):
    """Free disk space: delete the render files of older adjustments in this session. Before, every
    nudge left a full-size export behind until the session expired (tens of GB with 4K video).
    Keeps the source/analysis files, any job still running and — until this job is done — the
    last finished one, whose downloads stay valid if this change fails."""
    with jobs_lock:
        me = jobs.get(job_id)
        if not me:
            return
        root = me['root_id']
        others = [(jid, j) for jid, j in jobs.items() if j.get('root_id') == root and jid != job_id]
        last_done = None
        if keep_last_done:
            done = [(j['created_at'], jid) for jid, j in others if j.get('status') == 'done' and j.get('out_dir')]
            last_done = max(done)[1] if done else None
        dirs = []
        for jid, j in others:
            if jid == last_done or not j.get('out_dir') or j.get('status') in ('pending', 'running', 'preview'):
                continue
            dirs.append(j['out_dir'])
            j.update(out_dir=None, file_mp4=None, file_preview=None, file_synced_mp3=None, synced_wav=None)
            if jid != root:
                j['tmp'] = None
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def _prune_later(job_id, keep_last_done, delay=5.0):
    # A short delay lets the page switch to the new preview before the old file disappears.
    t = threading.Timer(delay, _prune, args=(job_id, keep_last_done))
    t.daemon = True
    t.start()


def _cleanup_loop():
    while True:
        time.sleep(600)
        now = time.time()
        stale_roots, stale_uploads = [], []
        with jobs_lock:
            for jid, j in jobs.items():
                if j.get('kind') == 'upload':
                    if now - j['created_at'] > JOB_TTL_SEC and not j.get('pinned') and not j.get('external'):
                        stale_uploads.append(jid)
                elif j.get('root_id') == jid and now - j.get('touched_at', now) > JOB_TTL_SEC:
                    stale_roots.append(jid)
            ups = [jobs.pop(jid) for jid in stale_uploads]
        for up in ups:
            if up.get('tmp'):
                shutil.rmtree(up['tmp'], ignore_errors=True)
        # Spool files of uploads that were aborted half-way
        spool_dir = TEMP_ROOT or tempfile.gettempdir()
        try:
            for name in os.listdir(spool_dir):
                f = os.path.join(spool_dir, name)
                if name.startswith('beatsync_spool_') and now - os.path.getmtime(f) > JOB_TTL_SEC:
                    os.remove(f)
        except OSError:
            pass
        for rid in stale_roots:
            _delete_lineage(rid)


threading.Thread(target=_cleanup_loop, daemon=True).start()


def _friendly(err):
    s = re.sub(r'\x1b\[[0-9;]*m', '', str(err)).replace('ERROR: ', '').strip()
    low = s.lower()
    if 'requested format is not available' in low or 'no video formats' in low or 'challenge' in low:
        return ('YouTube hid the download formats. Fix: 1) pip install -U "yt-dlp[default]"  '
                '2) install Deno (deno.com) or Node 22+, then restart the server. '
                + ('' if (tools.find('deno') or tools.find('node')) else 'No JavaScript runtime was found on this PC. ')
                + '(' + s[:160] + ')')
    if 'sign in to confirm' in low or 'not a bot' in low:
        return ('YouTube wants a signed-in session. Export cookies.txt from your browser while logged in, '
                'then Settings → Import cookies. (' + s[:160] + ')')
    if 'yt-dlp' in low and 'not installed' in low:
        return s
    return s[:400] or 'Unknown error'


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg runner with real progress + cancellation
# ─────────────────────────────────────────────────────────────────────────────

# Encoders run one step below normal priority: they still get every idle CPU
# cycle (same speed), but the rest of the PC stays responsive during long renders.
if os.name == 'nt':
    # CREATE_NO_WINDOW must be included: passing any creationflags switches off the desktop
    # app's console-hiding default, and every ffmpeg run would flash a cmd window.
    _LOW_PRIO = {'creationflags': getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
                                  | getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    _NICE = []
else:
    _LOW_PRIO = {}
    _NICE = ['nice', '-n', '5'] if shutil.which('nice') else []


def _ffmpeg(job_id, args, dur=None, p0=None, p1=None, label=None, timeout=None, holder=None, on_progress=None):
    """holder: a list the process is added to while it runs (so a shared background encode can be
    stopped); on_progress(frac): progress callback for encodes not tied to one job."""
    if timeout is None:
        # Long, heavy sources on the "high" preset can take several times real time;
        # a fixed 1 h limit used to kill those exports. Cancel still works at any time.
        timeout = max(4 * 3600, (dur or 0) * 40)
    cmd = _NICE + ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-y']
    if dur:
        cmd += ['-progress', 'pipe:1']
    cmd += args
    with tempfile.TemporaryFile(mode='w+') as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if dur else subprocess.DEVNULL,
                                stderr=errf, text=True, **_LOW_PRIO)
        j = None
        if job_id:
            with jobs_lock:
                j = jobs.get(job_id)
                if j is not None:
                    j['procs'].append(proc)
        if holder is not None:
            holder.append(proc)
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        try:
            if j is not None and j['cancel'].is_set():
                proc.kill()
            if proc.stdout is not None:
                last = 0.0
                for line in proc.stdout:
                    if line.startswith('out_time_us=') and dur:
                        try:
                            sec = int(line.split('=')[1]) / 1e6
                        except ValueError:
                            continue
                        now = time.time()
                        if now - last > 0.4:
                            last = now
                            frac = max(0.0, min(1.0, sec / dur))
                            if on_progress:
                                on_progress(frac)
                            if job_id and p0 is not None:
                                _step(job_id, f'{label} {int(frac * 100)}%', p0 + (p1 - p0) * frac)
            proc.wait()
        finally:
            killer.cancel()
            if j is not None:
                with jobs_lock:
                    if proc in j['procs']:
                        j['procs'].remove(proc)
            if holder is not None and proc in holder:
                holder.remove(proc)
        if j is not None and j['cancel'].is_set():
            raise Cancelled()
        if proc.returncode != 0:
            errf.seek(0)
            tail = errf.read().strip()[-600:]
            raise RuntimeError(f'ffmpeg failed: {tail or "exit code " + str(proc.returncode)}')


def _probe_video(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate:stream_disposition=attached_pic:'
         'stream_side_data=rotation:stream_tags=rotate:format=duration',
         '-of', 'json', path],
        capture_output=True, text=True, timeout=30)
    try:
        info = json.loads(r.stdout or '{}')
    except json.JSONDecodeError:
        info = {}
    streams = info.get('streams', [])
    # Cover art / thumbnails show up as one-frame "video" streams — skip them.
    v = next((s for s in streams if s.get('codec_type') == 'video'
              and not (s.get('disposition') or {}).get('attached_pic')), None)
    if not v:
        raise RuntimeError('No video stream found in the video source.')
    w, h = int(v.get('width') or 0), int(v.get('height') or 0)
    rot = 0
    for sd in v.get('side_data_list', []) or []:
        if 'rotation' in sd:
            rot = int(float(sd['rotation']))
    if not rot and v.get('tags', {}).get('rotate'):
        rot = int(float(v['tags']['rotate']))
    if abs(rot) % 180 == 90:
        w, h = h, w                                   # ffmpeg auto-rotates on decode

    def _rate(s):
        try:
            n, d = s.split('/')
            return float(n) / float(d) if float(d) else 0.0
        except Exception:
            return 0.0
    fps = _rate(v.get('avg_frame_rate', '0/0')) or _rate(v.get('r_frame_rate', '0/0')) or 30.0
    dur = float(info.get('format', {}).get('duration') or 0)
    return {'w': w, 'h': h, 'fps': round(fps, 3), 'dur': dur, 'codec': v.get('codec_name', ''),
            'has_audio': any(s.get('codec_type') == 'audio' for s in streams)}


# ─────────────────────────────────────────────────────────────────────────────
# Source download / upload resolution
# ─────────────────────────────────────────────────────────────────────────────

# yt-dlp runs as its own program (yt-dlp.exe in the user folder), so it can
# update itself daily without a new BeatSync release. YouTube also needs a
# JavaScript runtime (Deno, or Node 22+) to unlock its formats.
YT_CLIENTS = [c.strip() for c in os.environ.get('BEATSYNC_YT_CLIENTS', '').split(',') if c.strip()]


def _ytdlp_cmd():
    exe = tools.find('yt-dlp')
    if exe:
        return [exe]
    if not tools.FROZEN:
        try:
            import yt_dlp  # noqa: F401  (running from source with the pip package)
            import sys
            return [sys.executable, '-m', 'yt_dlp']
        except ImportError:
            pass
    return None


def _ytdlp_common():
    a = ['--no-playlist', '--no-warnings', '--color', 'never', '--remote-components', 'ejs:github']
    deno, node = tools.find('deno'), tools.find('node')
    if deno:
        a += ['--js-runtimes', f'deno:{deno}']
    elif node:
        a += ['--js-runtimes', f'node:{node}']
    ff = tools.find('ffmpeg')
    if ff:
        a += ['--ffmpeg-location', os.path.dirname(ff)]
    cookies = tools.cookies_path()
    if cookies:
        a += ['--cookies', cookies]
    if YT_CLIENTS:
        a += ['--extractor-args', 'youtube:player_client=' + ','.join(YT_CLIENTS)]
    return a


def _ytdlp_missing():
    return RuntimeError('The link downloader (yt-dlp) is not installed yet — open Settings → Tools, '
                        'or use Local file.')


def _ytdlp_error(lines, rc):
    errs = [l for l in lines if l.startswith('ERROR')]
    return RuntimeError((errs[-1] if errs else (lines[-1] if lines else f'yt-dlp exit code {rc}')))


def _ytdlp(job_id, args, label, p0, p1, timeout=4 * 3600):
    cmd = _ytdlp_cmd()
    if not cmd:
        raise _ytdlp_missing()
    prog = ['--newline', '--progress-template',
            'download:[bs] %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s']
    proc = subprocess.Popen(cmd + _ytdlp_common() + prog + args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    j = None
    if job_id:
        with jobs_lock:
            j = jobs.get(job_id)
            if j is not None:
                j['procs'].append(proc)
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    tail = collections.deque(maxlen=40)
    last = 0.0
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith('[bs] '):
                parts = line.split()
                try:
                    done = float(parts[1])
                    total = float(parts[2]) if parts[2] not in ('NA', 'None') else float(parts[3])
                except (ValueError, IndexError):
                    continue
                now = time.time()
                if job_id and total > 0 and now - last > 0.4:
                    last = now
                    frac = max(0.0, min(1.0, done / total))
                    _step(job_id, f'{label} {int(frac * 100)}% ({done / 1e6:.0f} MB)', p0 + (p1 - p0) * frac)
            elif line:
                tail.append(line)
        proc.wait()
    finally:
        killer.cancel()
        if j is not None:
            with jobs_lock:
                if proc in j['procs']:
                    j['procs'].remove(proc)
    if j is not None and j['cancel'].is_set():
        raise Cancelled()
    if proc.returncode != 0:
        raise _ytdlp_error(list(tail), proc.returncode)


def _ytdlp_info(url):
    cmd = _ytdlp_cmd()
    if not cmd:
        raise _ytdlp_missing()
    r = subprocess.run(cmd + _ytdlp_common() + ['--dump-single-json', '--skip-download',
                                                '--ignore-no-formats-error', url],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        raise _ytdlp_error((r.stderr or '').strip().splitlines(), r.returncode)
    return json.loads(r.stdout)


def _dl_audio(url, tmp, name, job_id=None, p0=0, p1=0):
    _ytdlp(job_id, ['-f', 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best',
                    '-x', '--audio-format', 'flac', '--audio-quality', '0',
                    '-o', os.path.join(tmp, f'{name}.%(ext)s'), url], 'Downloading song', p0, p1)
    files = [f for f in Path(tmp).glob(f'{name}.*') if not f.name.endswith(('.part', '.ytdl'))]
    if not files:
        raise RuntimeError(f'Failed to download: {name}')
    return str(files[0])


def _dl_video(url, tmp, name, job_id=None, p0=0, p1=0):
    # Prefer H.264 ≤1080p: plays everywhere, lets the export stream-copy,
    # and keeps every re-render fast. Falls back to whatever exists.
    _ytdlp(job_id, ['-f', ('bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/'
                           'bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b'),
                    '--merge-output-format', 'mp4',
                    '-o', os.path.join(tmp, f'{name}.%(ext)s'), url], 'Downloading video', p0, p1)
    files = [f for f in Path(tmp).glob(f'{name}.*') if f.suffix.lower() in ALLOWED_VIDEO | ALLOWED_AUDIO]
    if not files:
        raise RuntimeError(f'Failed to download: {name}')
    return str(files[0])


def _upload_entry(file_id):
    with jobs_lock:
        up = jobs.get(file_id)
    if not up or up.get('kind') != 'upload':
        raise RuntimeError('Uploaded file expired — please upload it again.')
    return up


def _use_upload(file_id, tmp, name):
    """The session's handle on an uploaded or picked file — never a full copy. Files picked in
    the desktop dialog are read where they are; uploads are hard-linked into the session folder."""
    up = _upload_entry(file_id)
    src, ext = up['path'], up['ext']
    if not os.path.isfile(src):
        raise RuntimeError(f'The file is no longer there: {src}')
    if up.get('external'):
        return src, ext
    dest = os.path.join(tmp, name + ext)
    try:
        os.link(src, dest)
        return dest, ext
    except OSError:
        with jobs_lock:
            up['pinned'] = True                  # read it in place and keep it until the app closes
        return src, ext


def register_local(path):
    """Desktop: use a file chosen in the native Open dialog where it is (no upload, no copy)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in ALLOWED_VIDEO | ALLOWED_AUDIO:
        raise ValueError(f'Unsupported file type: {ext or "unknown"}')
    if not p.is_file():
        raise ValueError('File not found.')
    file_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[file_id] = {'kind': 'upload', 'path': str(p), 'tmp': None, 'ext': ext,
                         'created_at': time.time(), 'external': True}
    return {'file_id': file_id, 'name': p.name, 'ext': ext, 'size': p.stat().st_size}


def _trim_args(start, end):
    a = []
    if start is not None:
        a += ['-ss', f'{float(start):.3f}']
    if end is not None:
        a += ['-t', f'{max(0.1, float(end) - float(start or 0)):.3f}']
    return a


def _trim_in(trim):
    """Input options that cut the source to [start, start+dur). With re-encoding ffmpeg seeks
    frame- and sample-accurately, so no trimmed copy of the video is needed."""
    a = []
    if trim:
        if trim[0] > 0:
            a += ['-ss', f'{trim[0]:.3f}']
        if trim[1]:
            a += ['-t', f'{trim[1]:.3f}']
    return a


def _vid_in(st):
    return _trim_in(st.get('vid_trim')) + ['-i', st['vid_file']]


def _stream_info(path):
    """(has_video, duration_sec) via ffprobe. Cover art in an MP3/M4A does not count as video."""
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'stream=codec_type:stream_disposition=attached_pic:format=duration',
                        '-of', 'json', path], capture_output=True, text=True, timeout=60)
    try:
        info = json.loads(r.stdout or '{}')
    except json.JSONDecodeError:
        info = {}
    has_video = any(st.get('codec_type') == 'video' and not (st.get('disposition') or {}).get('attached_pic')
                    for st in info.get('streams', []))
    try:
        dur = float(info.get('format', {}).get('duration') or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return has_video, dur


def _audio_as_video(job_id, src, tmp, trim_start, trim_end):
    """An audio file in the video slot: give it a plain black picture so the
    preview / export pipeline works unchanged. The audio stays lossless (FLAC),
    and the trim is applied in the same pass."""
    _, dur = _stream_info(src)
    start = trim_start or 0.0
    out_dur = (trim_end - start) if trim_end is not None else max(0.1, dur - start)
    out = os.path.join(tmp, 'vid_from_audio.mkv')
    t = _trim_args(trim_start, trim_end)
    _step(job_id, 'Audio in the video slot — adding a black picture...', 9)
    _ffmpeg(job_id, ['-f', 'lavfi', '-i', 'color=c=black:s=1280x720:r=30'] + t[:2] + ['-i', src] +
            ['-t', f'{out_dur:.3f}', '-map', '0:v:0', '-map', '1:a:0',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage', '-crf', '28',
             '-pix_fmt', 'yuv420p', '-c:a', 'flac', out],
            dur=out_dur, p0=9, p1=11, label='Preparing audio')
    return out


def _prepare_video(job_id, url, file_id, tmp, trim_start, trim_end):
    if file_id:
        vid, _ext = _use_upload(file_id, tmp, 'vid')
    else:
        vid = _dl_video(url, tmp, 'vid', job_id, 3, 9)

    trim = None
    if not _stream_info(vid)[0]:
        vid = _audio_as_video(job_id, vid, tmp, trim_start, trim_end)    # trim applied in the same pass
        info = _probe_video(vid)
        info['audio_only'] = True
    else:
        info = _probe_video(vid)
        # The trim is applied when reading (frame-accurate). It used to re-encode the whole clip
        # up front: a full encode before analysis even started, plus a second generation of loss.
        if trim_start is not None or trim_end is not None:
            full = info['dur']
            ss = float(trim_start or 0.0)
            end = float(trim_end) if trim_end is not None else None
            if full > 0:
                if ss >= full - 0.05:
                    raise RuntimeError('Video trim start is past the end of the video.')
                if end is not None:
                    end = min(end, full)
            if end is not None:
                d = max(0.1, end - ss)
            else:
                d = (full - ss) if full > 0 else None
            if ss > 0 or d:
                trim = [round(ss, 3), round(d, 3) if d else None]
                if d:
                    info['dur'] = round(d, 3)
    if not info['has_audio']:
        raise RuntimeError('The video has no audio track, so its beat cannot be detected.')
    audio = os.path.join(tmp, 'vid_audio.wav')
    # Analyse the audio that is actually inside the video file (not a second download).
    _ffmpeg(job_id, _trim_in(trim) + ['-i', vid, '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '22050',
                                      '-acodec', 'pcm_s16le', audio])
    return vid, audio, info, trim


def _prepare_song(job_id, url, file_id, tmp, trim_start, trim_end):
    if file_id:
        raw, _ext = _use_upload(file_id, tmp, 'song_src')
    else:
        raw = _dl_audio(url, tmp, 'song_src', job_id, 12, 18)
    wav = os.path.join(tmp, 'song.wav')
    t = _trim_args(trim_start, trim_end)
    # Always decode to PCM (stream-copying FLAC/Opus into .wav fails).
    _ffmpeg(job_id, t[:2] + ['-i', raw] + t[2:] + ['-map', '0:a:0', '-vn', '-acodec', 'pcm_s16le', wav])
    if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
        raise RuntimeError('Could not decode the song audio.')
    return wav


# ─────────────────────────────────────────────────────────────────────────────
# Analysis: BPM, music start, meter, downbeats, offset, beat grid
# ─────────────────────────────────────────────────────────────────────────────

HOP = 256                    # analysis hop at 22050 Hz (11.6 ms)
N_FFT = 2048
CHUNK_SEC = 60.0             # long files are analysed in chunks of this size (flat memory)


def _refine_bpm(beat_times):
    """Tempo from a line through beat number vs beat time. The beat number counts beats the
    tracker skipped (noise, breaks); the old mean of the gaps counted a skipped beat as one long
    gap, which pulled the tempo down (1.4% on a noisy test video = seconds of drift)."""
    import numpy as np
    bt = np.atleast_1d(np.asarray(beat_times, dtype=float))
    if len(bt) < 4:
        return None
    d = np.diff(bt)
    med = float(np.median(d))
    if med <= 0:
        return None
    k = np.concatenate([[0], np.cumsum(np.maximum(1, np.round(d / med)))])
    slope, icpt = np.polyfit(k, bt, 1)
    good = np.abs(bt - (slope * k + icpt)) < 0.25 * med
    if good.sum() >= 4:
        slope, icpt = np.polyfit(k[good], bt[good], 1)
    if not 0.5 * med < slope < 1.5 * med:
        return None
    return round(60.0 / float(slope), 4)


def _median_along(S, size, axis):
    """scipy.ndimage.median_filter(S, size along `axis` only, mode='reflect'), run as one 1-D
    filter per row. Same numbers, but SciPy's 1-D median (SciPy >= 1.15) is ~10x faster than
    its N-D one."""
    import numpy as np
    from scipy.ndimage import median_filter
    A = np.moveaxis(S, axis, -1)
    out = np.empty(A.shape, dtype=S.dtype)
    for i in range(A.shape[0]):
        out[i] = median_filter(A[i], size=size, mode='reflect')
    return np.moveaxis(out, -1, axis)


def _hpss_percussive(y, margin=3.0, kernel=31):
    """Percussive part of librosa.effects.hpss(y, margin=margin) — identical output, ~5x faster:
    skips the unused harmonic resynthesis and uses the fast median filter."""
    import librosa
    try:
        stft = librosa.stft(y, n_fft=2048, hop_length=None, win_length=None, window='hann',
                            center=True, pad_mode='constant')
        S, phase = librosa.magphase(stft)
        del stft
        harm = _median_along(S, kernel, axis=-1)
        perc = _median_along(S, kernel, axis=-2)
        mask = librosa.util.softmask(perc, harm * margin, power=2.0, split_zeros=False)
        del harm, perc
        return librosa.istft((S * mask) * phase, dtype=y.dtype, n_fft=2048, hop_length=None,
                             win_length=None, center=True, length=y.shape[-1])
    except Exception:                                   # unexpected librosa API change → reference path
        return librosa.effects.hpss(y, margin=margin)[1]


def _analysis_features(y, sr, hop=HOP, chunk_sec=CHUNK_SEC):
    """
    Every spectral feature the analysis needs, in one pass over the signal.
    Runs in overlapping chunks, so memory stays flat for hour-long files (a
    single STFT + HPSS of a long file needs several GB), and the features are
    shared by every later step instead of being recomputed.
      oenv      — onset strength, same as librosa.onset.onset_strength(y, hop)
      oenv_perc — onset strength of the percussive part (HPSS, margin 3)
      low_env   — onset envelope of the ≤250 Hz band (kicks / downbeats)
    """
    import numpy as np
    import librosa
    y = np.asarray(y, dtype=np.float32)
    n = len(y)
    step = 2 * hop                                   # HPSS uses a 2×hop STFT; keep chunks on its grid
    chunk = max(step, int(chunk_sec * sr) // step * step)
    pad = (N_FFT // step + 40) * step                # STFT window + HPSS median-filter context
    n_frames = 1 + n // hop
    low = librosa.fft_frequencies(sr=sr, n_fft=N_FFT) <= 250
    mel_basis = librosa.filters.mel(sr=sr, n_fft=N_FFT)
    mel, mel_perc, low_mag = [], [], []
    for a in range(0, max(n, 1), chunk):
        b = min(n, a + chunk)
        s0, s1 = max(0, a - pad), min(n, b + pad)
        seg = y[s0:s1]
        f0 = a // hop - s0 // hop
        f1 = (b // hop if b < n else n_frames) - s0 // hop
        D = np.abs(librosa.stft(seg, n_fft=N_FFT, hop_length=hop))[:, f0:f1]
        mel.append(mel_basis @ (D ** 2))
        low_mag.append(D[low])
        y_perc = _hpss_percussive(seg, margin=3.0)
        P = np.abs(librosa.stft(y_perc, n_fft=N_FFT, hop_length=hop))[:, f0:f1]
        mel_perc.append(mel_basis @ (P ** 2))
        del D, P, y_perc
    S = librosa.power_to_db(np.concatenate(mel, axis=1))
    oenv = librosa.onset.onset_strength(S=S, sr=sr, hop_length=hop)
    S = librosa.power_to_db(np.concatenate(mel_perc, axis=1))
    oenv_perc = librosa.onset.onset_strength(S=S, sr=sr, hop_length=hop)
    L = librosa.amplitude_to_db(np.concatenate(low_mag, axis=1), ref=np.max)
    low_env = np.concatenate([[0], np.maximum(0, np.diff(L, axis=1)).mean(axis=0)])
    return {'oenv': oenv, 'oenv_perc': oenv_perc, 'low_env': low_env, 'sr': sr, 'hop': hop}


def _retime(env, rate):
    """Envelope as it would be after a time-stretch by `rate` (>1 = faster).
    Far cheaper than stretching the audio and analysing it again."""
    import numpy as np
    env = np.asarray(env, dtype=np.float64)
    if abs(rate - 1.0) < 1e-6:
        return env
    n_out = max(1, int(len(env) / rate))
    return np.interp(np.arange(n_out) * rate, np.arange(len(env)), env)


def _music_start_frame(oenv, sr, hop, window_sec=4.0, threshold_ratio=0.15):
    import numpy as np
    window_frames = int(window_sec * sr / hop)
    threshold = (float(np.max(oenv)) + 1e-9) * threshold_ratio
    for i in range(0, max(1, len(oenv) - window_frames), max(1, window_frames // 2)):
        if float(np.mean(oenv[i:i + window_frames])) >= threshold:
            return max(0, i - window_frames // 4)
    return 0


def _chunks(n, chunk, ctx):
    """(core_start, core_end, seg_start, seg_end) for processing n frames in chunks with context."""
    for a in range(0, max(n, 1), chunk):
        b = min(n, a + chunk)
        yield a, b, max(0, a - ctx), min(n, b + ctx)


def _mean_tempogram(oenv, sr, hop, win=384, chunk=4096):
    import librosa
    total, count = None, 0
    for a, b, s0, s1 in _chunks(len(oenv), chunk, win):
        tg = librosa.feature.tempogram(onset_envelope=oenv[s0:s1], sr=sr, hop_length=hop, win_length=win)
        part = tg[:, a - s0:b - s0].sum(axis=1)
        total = part if total is None else total + part
        count += b - a
    return total / max(1, count)


def _tracker_tempo(oenv, sr, hop, start_bpm=120.0, std_bpm=1.0, max_tempo=320.0):
    """Exactly the tempo librosa.beat.beat_track estimates, but from a chunked
    tempogram: beat_track builds one for the whole file (several GB for an hour)."""
    import numpy as np
    import librosa
    win = int(librosa.time_to_frames(8.0, sr=sr, hop_length=hop))
    tg = _mean_tempogram(oenv, sr, hop, win=win)
    bpms = librosa.tempo_frequencies(win, sr=sr, hop_length=hop)
    with np.errstate(divide='ignore'):
        logprior = -0.5 * ((np.log2(bpms) - np.log2(start_bpm)) / std_bpm) ** 2
    logprior[:int(np.argmax(bpms < max_tempo))] = -np.inf
    return float(bpms[int(np.argmax(np.log1p(1e6 * tg) + logprior))])


def _detect_bpm(y, sr, feat=None):
    """Returns (bpm, beat_times, confidence, music_start_samples, raw_beat_times).
    beat_times are back-tracked to the onset; raw_beat_times are the tracker's
    pulse positions (better for fitting a grid)."""
    import numpy as np
    import librosa

    hop = HOP
    if feat is None:
        feat = _analysis_features(y, sr, hop)
    f0 = _music_start_frame(feat['oenv'], sr, hop)
    music_start = f0 * hop
    oenv_full = feat['oenv'][f0:]
    oenv_perc = feat['oenv_perc'][f0:]

    tempo_a = _tracker_tempo(oenv_perc, sr, hop)

    mean_tg = _mean_tempogram(oenv_full, sr, hop)
    bpm_axis = librosa.tempo_frequencies(len(mean_tg), sr=sr, hop_length=hop)

    # The beat tracker's tempo is the anchor; the tempogram picks the octave.
    # (Earlier versions also "voted" with a tempogram peak and a PLP estimate,
    # but both were broken — lag 0 / rising edges — so they never took part.
    # Making them work caused octave errors, so they are gone.)
    anchor = tempo_a

    candidates = {round(anchor * m, 2) for m in (0.5, 1.0, 2.0) if 40 <= round(anchor * m, 2) <= 240}
    best_bpm, best_score = anchor, -1.0
    for cand in candidates:
        idx = int(np.argmin(np.abs(bpm_axis - cand)))
        score = float(mean_tg[idx])
        ratio = cand / anchor if anchor != 0 else 1.0
        if ratio > 1.5:
            score *= 0.85
        elif ratio < 0.67:
            score *= 0.80
        if score > best_score:
            best_score, best_bpm = score, cand

    _, beat_frames = librosa.beat.beat_track(onset_envelope=oenv_perc, sr=sr, hop_length=hop, bpm=best_bpm)
    raw_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop) + (music_start / sr)
    beat_frames = librosa.onset.onset_backtrack(beat_frames, oenv_full)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop) + (music_start / sr)

    refined = _refine_bpm(beat_times)
    if refined is not None:
        best_bpm = refined
    # Beat-tracker times are quantised to 11.6 ms frames and wobble when
    # back-tracked, which leaves ~0.5% tempo error: seconds of drift on a long
    # video. Fit the period to the kick attacks of the whole track instead.
    try:
        precise = _precise_period(y, sr, 60.0 / best_bpm)
        if precise:
            best_bpm = 60.0 / precise
    except Exception:
        pass

    idx_best = int(np.argmin(np.abs(bpm_axis - best_bpm)))
    peak_energy = float(mean_tg[idx_best])
    total_energy = float(np.sum(mean_tg) + 1e-9)
    confidence = min(1.0, peak_energy / (total_energy / len(bpm_axis)) / 10.0)
    return round(best_bpm, 4), beat_times, round(confidence, 3), music_start, raw_times


_METER_BIAS = {2: 0.95, 3: 1.00, 4: 1.05, 5: 0.85, 6: 1.00, 7: 0.80}


def _detect_meter(beat_energies):
    import numpy as np
    n = len(beat_energies)
    mean_all = float(np.mean(beat_energies)) + 1e-9
    best_meter, best_phase, best_score = 4, 0, -1.0
    for B in (2, 3, 4, 5, 6, 7):
        if n < B * 2:
            continue
        for phase in range(B):
            score = float(np.mean(beat_energies[np.arange(phase, n, B)])) / mean_all * _METER_BIAS.get(B, 0.9)
            if score > best_score:
                best_score, best_meter, best_phase = score, B, phase
    return best_meter, best_phase


def _detect_downbeats(low_env, sr, beat_times, hop=HOP):
    import numpy as np
    import librosa
    beat_times = np.atleast_1d(beat_times)
    if len(beat_times) < 4:
        return beat_times, 1
    beat_frames = np.clip(librosa.time_to_frames(beat_times, sr=sr, hop_length=hop), 0, len(low_env) - 1)
    beat_energies = low_env[beat_frames]
    bpb, phase = _detect_meter(beat_energies)
    idx = np.arange(phase, len(beat_times), bpb)
    if float(np.mean(beat_energies[idx])) < (float(np.mean(beat_energies)) + 1e-9) * 1.05:
        return beat_times, 1
    return beat_times[idx], bpb


def _lcm(a, b):
    from math import gcd
    return a * b // gcd(a, b)


def _find_beat_offset(vid_feat, song_feat, vid_beats, song_beats, sr, vid_rate=1.0, song_rate=1.0):
    """
    Where in the song the video should start. Beat times / features are in each
    source's original timeline; `*_rate` is the speed each one is played at in
    the output (the song stretch, or the video speed-up in Match Song mode).
    Returns (offset_samples @ sr, song_beat_times, song_downbeats, song_beats_per_bar),
    all in output time.

    Every candidate is scored at once with an FFT cross-correlation, so long
    videos cost the same as short ones. A video longer than the song is matched
    on the part that overlaps (it used to fall back to offset 0).
    """
    import numpy as np
    from scipy.signal import correlate

    hop = HOP
    song_beats = np.atleast_1d(np.asarray(song_beats, dtype=float))
    vid_beats = np.atleast_1d(np.asarray(vid_beats, dtype=float))
    if len(song_beats) == 0 or len(vid_beats) == 0:
        sb = song_beats / song_rate
        return 0, sb, sb, 1

    vid_downbeats, vid_bpb = _detect_downbeats(vid_feat['low_env'], sr, vid_beats, hop)
    song_downbeats, song_bpb = _detect_downbeats(song_feat['low_env'], sr, song_beats, hop)
    vid_beats, vid_downbeats = vid_beats / vid_rate, np.atleast_1d(vid_downbeats) / vid_rate
    song_beats, song_downbeats = song_beats / song_rate, np.atleast_1d(song_downbeats) / song_rate
    vid_first_downbeat = float(vid_downbeats[0]) if len(vid_downbeats) > 0 else float(vid_beats[0])

    lcm_beats = _lcm(vid_bpb, song_bpb)
    super_step = lcm_beats // song_bpb
    if lcm_beats <= 28 and len(song_downbeats) >= 2 and super_step >= 1:
        super_times = song_downbeats[::super_step]
        search_times = super_times if len(super_times) >= 2 else (
            song_downbeats if len(song_downbeats) >= 3 else song_beats)
    elif len(song_downbeats) >= 3:
        search_times = song_downbeats
    else:
        search_times = song_beats

    vid_env = _retime(vid_feat['low_env'], vid_rate)
    song_env = _retime(song_feat['low_env'], song_rate)
    vid_env = vid_env / (np.max(vid_env) + 1e-9)
    song_env = song_env / (np.max(song_env) + 1e-9)
    nv, ns = len(vid_env), len(song_env)
    corr = correlate(song_env, vid_env, mode='full', method='fft')     # corr[L+nv-1] = Σ song[i+L]·vid[i]

    cands = []
    min_overlap = 0.5 * min(nv, ns)
    for sb_time in search_times:
        offset_sec = float(sb_time) - vid_first_downbeat
        if offset_sec < 0:
            continue
        lag = int(offset_sec * sr / hop)
        ov = min(nv, ns - lag)
        if ov <= 0 or ov < min_overlap:
            continue
        cands.append((lag + nv <= ns, offset_sec, lag, ov))
    pool = [c for c in cands if c[0]]                   # offsets where the whole video fits
    if not pool and cands:
        # Video longer than what is left of the song: start the song at its first
        # usable downbeat so as much of it as possible is used (used to give offset 0
        # with no beat alignment at all).
        pool = [min(cands, key=lambda c: c[1])]

    best_score, best_offset_sec = -np.inf, 0.0
    for _, offset_sec, lag, ov in pool:
        seg = song_env[lag:lag + ov]
        k = max(1, ov // 20)
        score = float(corr[lag + nv - 1]) + 0.2 * float(np.sum(np.partition(seg, len(seg) - k)[-k:]))
        if score > best_score:
            best_score, best_offset_sec = score, offset_sec

    # (Older versions snapped the offset itself to the nearest song beat. That put a
    # song beat at video time 0 rather than on the video's beat — up to half a beat
    # off. Every candidate is already song downbeat − video downbeat, so it is on-beat.)

    # Fine-tune ±50 ms on the full-band onset envelopes
    best_offset_samp = int(round(best_offset_sec * sr))
    vo = _retime(vid_feat['oenv'], vid_rate)
    so = _retime(song_feat['oenv'], song_rate)
    base = int(round(best_offset_sec * sr / hop))
    span = int(round(0.05 * sr / hop))
    min_frames = sr / hop                                 # at least 1 s of overlap
    best_d, best_fs = 0, -np.inf
    for d in range(-span, span + 1):
        lag = base + d
        if lag < 0:
            continue
        ov = min(len(vo), len(so) - lag)
        if ov < min_frames:
            continue
        s = float(np.dot(vo[:ov], so[lag:lag + ov])) / ov
        if s > best_fs:
            best_fs, best_d = s, d
    best_offset_samp += best_d * hop

    return max(0, best_offset_samp), song_beats, song_downbeats, int(song_bpb)


def _kick_attacks(y, sr, fc=180.0, chunk_sec=CHUNK_SEC):
    """Kick-drum attack times (s) + strengths: rising edge of the low-band log envelope (~1 ms precision).
    Chunked with O(N) moving averages, so an hour of audio takes seconds, not minutes."""
    import numpy as np
    from scipy.signal import butter, sosfiltfilt, find_peaks
    sos = butter(4, fc, 'low', fs=sr, output='sos')
    hop = max(1, int(sr * 0.001))
    win = max(1, int(sr * 0.010))
    n = len(y)
    if n == 0:
        return np.array([]), np.array([])
    chunk = max(hop, int(chunk_sec * sr) // hop * hop)
    pad = (int(sr) // hop + 1) * hop                 # 1 s of filter context on each side
    h = (win - 1) // 2
    env = []
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        s0, s1 = max(0, a - pad), min(n, b + pad)
        yl = sosfiltfilt(sos, np.asarray(y[s0:s1], dtype=np.float64))
        c = np.concatenate([[0.0], np.cumsum(yl * yl)])
        idx = np.arange(a, b, hop) - s0              # same samples as a full-length [::hop]
        hi = np.minimum(len(yl), idx + h + 1)
        lo = np.maximum(0, idx + h + 1 - win)
        env.append(np.sqrt(np.maximum(0.0, (c[hi] - c[lo]) / win)))
    e = np.concatenate(env) + 1e-9
    db = 20 * np.log10(e / e.max())
    lag = 5
    rise = np.maximum(0, db[lag:] - db[:-lag])
    if rise.max() <= 0:
        return np.array([]), np.array([])
    pk, _ = find_peaks(rise, height=0.3 * rise.max(), distance=60)
    return (pk + lag / 2) * hop / sr + 0.0063, rise[pk]


def _precise_period(y, sr, period, span=0.02, min_conc=0.4, min_prominence=8.0):
    """
    Refine a beat period to sub-millisecond precision: the period at which the
    kick attacks line up best (circular concentration), searched within ±span.
    Precision grows with the length of the track, which is what long videos need.
    Returns None when there is no clear kick pulse (then the tracker's tempo stays).
    The pulse strength counts the beat and the half-beat (off-beat bass / hats used to cancel
    the kicks and hide a clear pulse), and a peak is accepted when it stands far above the
    noise floor of the search range, not only above a fixed level.
    """
    import numpy as np
    t, w = _kick_attacks(y, sr)
    if len(t) < 16 or w.sum() <= 0 or t[-1] - t[0] < 8 * period:
        return None
    t = t - t[0]
    T = float(t[-1])
    # peak width ≈ period² / T, so sample it at ~1/8 of that
    step = max(period * period / T / 8, 1e-7)
    grid = np.arange(period * (1 - span), period * (1 + span), step)
    if len(grid) > 4000:
        grid = np.linspace(period * (1 - span), period * (1 + span), 4000)
    conc = np.empty(len(grid))
    for i in range(0, len(grid), 256):            # bounded memory for very long tracks
        g = grid[i:i + 256]
        ph = np.exp(2j * np.pi * t[None, :] / g[:, None])
        conc[i:i + len(g)] = (np.abs(ph @ w) + np.abs((ph * ph) @ w)) / (2 * w.sum())
    k = int(np.argmax(conc))
    clear = conc[k] >= min_conc or conc[k] >= min_prominence * float(np.median(conc))
    if not clear or k == 0 or k == len(grid) - 1:
        return None
    p = float(grid[k])
    # Off-beat hits (hats, bass) bias the concentration peak slightly, so finish
    # with a weighted line fit through the attacks that sit on the pulse.
    for _ in range(3):
        z = np.sum(w * np.exp(2j * np.pi * t / p))
        ph = (np.angle(z) % (2 * np.pi)) * p / (2 * np.pi)
        idx = np.round((t - ph) / p)
        near = np.abs(t - ph - idx * p) < 0.1 * p
        if near.sum() < 16:
            break
        slope, _ = np.polyfit(idx[near], t[near], 1, w=np.sqrt(w[near]))
        if abs(slope - p) > step * 8:
            break
        p = float(slope)
    return p


def _attack_phase(y, sr, period):
    """Circular mean of kick attacks on a grid of `period`. Returns (phase, concentration 0..1)."""
    import numpy as np
    t, w = _kick_attacks(y, sr)
    if len(t) < 4 or w.sum() <= 0:
        return None, 0.0
    z = np.sum(w * np.exp(2j * np.pi * t / period))
    conc = float(abs(z) / w.sum())
    ph = (np.angle(z) % (2 * np.pi)) * period / (2 * np.pi)
    # Second pass: only attacks near the pulse (drops off-beat hats / bass notes)
    near = np.abs(((t - ph + period / 2) % period) - period / 2) < 0.15 * period
    if near.sum() >= 4:
        z2 = np.sum(w[near] * np.exp(2j * np.pi * t[near] / period))
        ph = (np.angle(z2) % (2 * np.pi)) * period / (2 * np.pi)
    return float(ph), conc


def _fit_grid(y, sr, beat_times, fallback_bi):
    """
    Fit a constant beat grid (interval, phase) to the song.
    Interval: least-squares line through the tracked beats (outliers dropped).
    Phase: snapped to the kick attacks when they form a clear pulse — more
    precise than beat-tracker peaks, and immune to it locking onto hi-hats.
    Returns (beat_interval_sec, phase_sec, kick_concentration) with 0 <= phase < interval.
    """
    import numpy as np
    bt = np.asarray(beat_times, dtype=float)
    bi, ph = float(fallback_bi), float(bt[0]) if len(bt) else 0.0
    if len(bt) >= 4:
        med = float(np.median(np.diff(bt)))
        if med > 0:
            # Beat index from consecutive gaps (tolerates skipped beats without
            # accumulating the frame-quantisation error of the median).
            k = np.concatenate([[0], np.cumsum(np.maximum(1, np.round(np.diff(bt) / med)))])
            slope, icpt = np.polyfit(k, bt, 1)
            good = np.abs(bt - (slope * k + icpt)) < 0.2 * med
            if good.sum() >= 4:
                slope, icpt = np.polyfit(k[good], bt[good], 1)
            if 0.5 * med < slope < 1.5 * med:
                bi, ph = float(slope), float(icpt)
    conc = 0.0
    try:
        ph2, conc = _attack_phase(y, sr, bi)
        if ph2 is not None and conc > 0.4:
            ph = ph2
    except Exception:
        pass
    return bi, ph % bi, conc


def _bar_phase(downbeats, grid_phase, bi, bpb):
    import numpy as np
    db = np.asarray(downbeats, dtype=float)
    if bpb <= 1 or len(db) < 2:
        return grid_phase
    k = np.round((db - grid_phase) / bi).astype(int) % bpb
    m = int(np.bincount(k, minlength=bpb).argmax())
    return (grid_phase + m * bi) % (bi * bpb)


def _time_stretch(y, sr, rate):
    """Stretch (rate>1 = faster), pitch kept. Returns (audio, engine). Rubber Band when available."""
    import librosa
    if CAPS['rubberband']:
        try:
            import pyrubberband as pyrb
            out = pyrb.time_stretch(y.T if y.ndim == 2 else y, sr, rate)
            return (out.T if y.ndim == 2 else out), 'rubberband'
        except Exception:
            pass
    return librosa.effects.time_stretch(y, rate=rate), 'librosa'

# ─────────────────────────────────────────────────────────────────────────────
# Timing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bpb(st):
    return st['beats_per_bar'] if st.get('beats_per_bar', 1) > 1 else 4


def _pitch_val(st, key):
    v = st[key]
    if isinstance(v, dict):
        return v['preserve' if st['preserve_pitch'] else 'resample']
    return v


def _timing(st):
    sr = st['render_sr']
    bi = st['beat_interval']
    bpb = _bpb(st)
    off = st['offset_samples'] / sr
    auto = st['auto_offset_samples'] / sr
    return {
        'offset': round(off, 4),
        'auto_offset': round(auto, 4),
        'delta_ms': round((off - auto) * 1000, 1),
        'delta_beats': round((off - auto) / bi, 3),
        'beat_interval': round(bi, 6),
        'beats_per_bar': bpb,
        'beat_phase_out': round((_pitch_val(st, 'grid_phase') - off) % bi, 5),
        'bar_phase_out': round((_pitch_val(st, 'bar_phase') - off) % (bi * bpb), 5),
        'micro_align_ms': st.get('micro_align_ms', 0.0),
        'song_duration': st['song_duration'],
        'preserve_pitch': st['preserve_pitch'],
        'bpm_mode': st['bpm_mode'],
        'target_bpm': round(60.0 / bi, 3),
    }


def _trigger(st, division):
    """Period and phase (output-time seconds) of the effect trigger grid."""
    t = _timing(st)
    bi, bpb = t['beat_interval'], t['beats_per_bar']
    if division == 'bar':
        return bi * bpb, t['bar_phase_out']
    frac = DIVISIONS.get(division) or 1.0
    period = bi * frac
    return period, t['beat_phase_out'] % period


# ─────────────────────────────────────────────────────────────────────────────
# Video filter graph
# ─────────────────────────────────────────────────────────────────────────────

def _even(x):
    return max(2, int(round(x / 2.0)) * 2)


def _color_filters(f):
    out = []
    dn = f['denoise']
    if dn > 0.01:
        out.append(f'hqdn3d={4*dn:.2f}:{3*dn:.2f}:{6*dn:.2f}:{4.5*dn:.2f}')
    eq = []
    if abs(f['brightness']) > 0.004:
        eq.append(f'brightness={f["brightness"]:.3f}')
    if abs(f['contrast'] - 1) > 0.004:
        eq.append(f'contrast={f["contrast"]:.3f}')
    if abs(f['saturation'] - 1) > 0.004:
        eq.append(f'saturation={f["saturation"]:.3f}')
    if abs(f['gamma'] - 1) > 0.004:
        eq.append(f'gamma={f["gamma"]:.3f}')
    if eq:
        out.append('eq=' + ':'.join(eq))
    if abs(f['hue']) > 0.5:
        out.append(f'hue=h={f["hue"]:.1f}')
    w, tn, sp = f['warmth'], f['tint'], f['splittone']
    if abs(w) > 0.004 or abs(tn) > 0.004 or sp > 0.004:
        c = lambda v: max(-1.0, min(1.0, v))
        cb = {'rs': -0.12 * sp, 'bs': 0.12 * sp,
              'rm': 0.25 * w, 'gm': -0.25 * tn, 'bm': -0.25 * w,
              'rh': 0.12 * sp + 0.15 * w, 'gh': -0.15 * tn, 'bh': -0.12 * sp - 0.15 * w}
        out.append('colorbalance=' + ':'.join(f'{k}={c(v):.3f}' for k, v in cb.items() if abs(v) > 0.0005))
    fd = f['fade']
    if fd > 0.01:
        out.append(f"curves=m='0/{0.13*fd:.3f} 1/{1-0.07*fd:.3f}'")
    sh = f['sharpness']
    if sh > 0.02:
        out.append(f'unsharp=5:5:{min(sh, 4.0):.2f}:5:5:0')
    return out


def _overlay_filters(f):
    out = []
    if f['vignette'] > 0.01:
        out.append(f'vignette=angle={0.25 + 0.6 * f["vignette"]:.3f}')
    if f['grain'] > 0.01:
        out.append(f'noise=alls={int(6 + 22 * f["grain"])}:allf=t+u')
    return out


def _beat_effects(o, W, H, period, phase):
    fx = set(o['effects'])
    if not fx or period < 0.05:
        return []
    I = o['fx_intensity']
    P, ph = f'{period:.6f}', f'{phase:.5f}'

    def env(decay):
        d = min(decay, period * 0.85)
        return f'max(0\\,1-mod(t-{ph}\\,{P})/{d:.4f})'

    def gate(win):
        return f"'lt(mod(t-{ph}\\,{P})\\,{min(win, period * 0.6):.4f})'"

    out = []
    z = []
    if 'punch' in fx:
        z.append(f'{0.06 * I:.4f}*{env(0.12)}')
    if 'zoom_pulse' in fx:
        z.append(f'{0.13 * I:.4f}*{env(0.22)}')
    if z:
        zexpr = '1+' + '+'.join(z)
        out.append(f"scale=w='2*trunc(iw*({zexpr})/2)':h='2*trunc(ih*({zexpr})/2)':eval=frame")
        out.append(f'crop={W}:{H}')
    if 'shake' in fx:
        sw, sh = _even(W * 1.06), _even(H * 1.06)
        amp = min((sw - W) / 2 * 0.9, W * 0.012 * I)
        e = env(0.16)
        out.append(f'scale={sw}:{sh}')
        out.append(f"crop={W}:{H}:x='clip((iw-{W})/2+{amp:.2f}*sin(t*53)*{e}\\,0\\,iw-{W})'"
                   f":y='clip((ih-{H})/2+{amp:.2f}*cos(t*41)*{e}\\,0\\,ih-{H})'")
    if 'rotation' in fx:
        sw, sh = _even(W * 1.1), _even(H * 1.1)
        out.append(f'scale={sw}:{sh}')
        out.append(f"rotate='{0.045 * I:.4f}*sin(t*29)*{env(0.22)}':ow=iw:oh=ih:c=black")
        out.append(f'crop={W}:{H}')
    if 'mirror' in fx:
        out.append(f"hflip=enable='mod(floor((t-{ph})/{P})\\,2)'")
    if 'flash' in fx:
        out.append(f"eq=brightness='{0.22 * I:.4f}*{env(0.09)}':eval=frame")
    if 'color_pop' in fx:
        e = env(0.18)
        out.append(f"eq=saturation='1+{0.7 * I:.3f}*{e}':contrast='1+{0.22 * I:.3f}*{e}':eval=frame")
    if 'color_shift' in fx:
        e = env(0.25)
        out.append(f"hue=h='{70 * I:.2f}*{e}':s='1+{0.5 * I:.3f}*{e}'")
    if 'rgb_split' in fx:
        s = max(2, int(round(W / 640 * 5 * I)))
        out.append(f'chromashift=cbh=-{s}:crh={s}:enable={gate(0.1)}')      # stays in YUV (no quality loss)
    if 'glitch' in fx:
        s = max(2, int(round(W / 640 * 7 * I)))
        out.append(f'chromashift=crh={s}:cbv=-{s}:cbh=-{max(1, s // 2)}:enable={gate(0.08)}')
        out.append(f'noise=alls={int(18 * I) + 6}:allf=t:enable={gate(0.08)}')
    if 'blur_pulse' in fx:
        out.append(f'gblur=sigma={max(0.5, 3.5 * I * W / 1280):.2f}:enable={gate(0.1)}')
    if 'vignette' in fx:
        out.append(f"vignette=angle='min(1.5\\,0.3+{0.6 * I:.3f}*{env(0.3)})':eval=frame")
    if 'invert_flash' in fx:
        out.append(f'negate=enable={gate(0.03 + 0.02 * I)}')
    if 'strobe' in fx:
        out.append(f'drawbox=x=0:y=0:w=iw:h=ih:c=black:t=fill:enable={gate(0.035 + 0.015 * I)}')
    if 'scanlines' in fx:
        gap = max(3, int(round(H / 240)))
        out.append(f'drawgrid=x=-8:y=0:w=iw+64:h={gap}:t=1:c=black@{min(0.6, 0.3 * I):.2f}')
    return out


_TEXTURE_FX = {'glitch', 'scanlines'}


def _build_vf(st, o, out_dur, preview):
    vi = st['vid_info']
    W, H = _even(vi['w']), _even(vi['h'])
    parts = []
    vs = st.get('video_speed') or 1.0
    if abs(vs - 1.0) > 1e-3:
        parts.append(f'setpts=PTS/{vs:.6f}')
        # Re-grid to the source frame rate by timestamp. Without this the muxer
        # force-fits the faster frames into the old rate and the picture drifts
        # up to ~2 frames behind the audio.
        parts.append(f'fps=fps={vi.get("fps") or 30:.3f}')
    if (W, H) != (vi['w'], vi['h']):
        parts.append(f'crop={W}:{H}')
    asp = ASPECTS.get(o['aspect'])
    if asp:
        r = asp[0] / asp[1]
        if W / H > r + 1e-3:
            nw, nh = _even(H * r), H
        else:
            nw, nh = W, _even(W / r)
        if (nw, nh) != (W, H):
            parts.append(f'crop={nw}:{nh}')
            W, H = nw, nh
    f = o['filters']
    # Preview of a big (1080p/4K) video: shrink to 720p *before* the colour work and effects, so
    # they run on ~4-9x fewer pixels. Skipped when a filter's look depends on pixel size (grain,
    # sharpen, denoise, scanlines, glitch noise), so the preview still shows what the export does.
    texture = (f['denoise'] > 0.01 or f['sharpness'] > 0.02 or f['grain'] > 0.01
               or bool(_TEXTURE_FX & set(o['effects'])))
    early = preview and H > 720 and not texture
    if early:
        W, H = _even(W * 720.0 / H), 720
        parts.append(f'scale={W}:{H}')
    if o['hflip']:
        parts.append('hflip')
    parts += _color_filters(f)
    if o['effects']:
        period, phase = _trigger(st, o['fx_division'])
        parts += _beat_effects(o, W, H, period, phase)
    parts += _overlay_filters(f)
    fo = o['fade_out']
    if fo > 0 and out_dur > fo * 1.5 and (not preview or o['preview_sec'] <= 0 or o['preview_sec'] >= out_dur):
        parts.append(f'fade=t=out:st={out_dur - fo:.3f}:d={fo:.3f}')
    if preview and H > 720:
        parts.append('scale=-2:720')
    if parts or preview:
        parts.append('format=yuv420p')
    return ','.join(parts) if parts else None


def _mixing(st, o):
    return (1.0 - o['song_vol']) > 0.01 and bool(st['vid_info'].get('has_audio'))


def _audio_graph(st, o, out_dur, fade_ok=True, orig='0:a:0', song='1:a:0'):
    """Returns (filter_complex_parts, amap). `orig` = the video's own audio, `song` = synced wav."""
    new_vol = o['song_vol']
    orig_vol = 1.0 - new_vol
    mixing = _mixing(st, o)
    fo = o['fade_out']
    tail = f',afade=t=out:st={out_dur - fo:.3f}:d={fo:.3f}' if (fade_ok and fo > 0 and out_dur > fo * 1.5) else ''
    if mixing:
        k = 1.0 if CAPS['amix_normalize'] else 2.0      # old ffmpeg: amix halves each input
        vs = st.get('video_speed') or 1.0
        at = (_build_atempo(vs) + ',') if abs(vs - 1.0) > 1e-3 else ''   # keep original audio in sync with sped video
        norm = ':normalize=0' if CAPS['amix_normalize'] else ''
        return ([f'[{orig}]{at}aformat=channel_layouts=stereo,volume={orig_vol * k:.4f}[oa]',
                 f'[{song}]aformat=channel_layouts=stereo,volume={new_vol * k:.4f}[na]',
                 f'[oa][na]amix=inputs=2:duration=longest{norm}{tail}[a]'], '[a]')
    if abs(new_vol - 1.0) > 0.004 or tail:
        return [f'[{song}]volume={new_vol:.4f}{tail}[a]'], '[a]'
    return [], song


def _build_atempo(speed):
    filters, s = [], speed
    while s > 2.0:
        filters.append('atempo=2.0')
        s /= 2.0
    while s < 0.5:
        filters.append('atempo=0.5')
        s *= 2.0
    filters.append(f'atempo={s:.6f}')
    return ','.join(filters)


# ─────────────────────────────────────────────────────────────────────────────
# Video track cache
#
# The picture of a render depends only on the look (filters, effects, aspect, speed, trim,
# quality) — not on where the song sits. So each look is encoded once per session, video only,
# and cached; then the synced audio is muxed on (seconds, stream copy). Timing nudges, pitch and
# mix changes no longer re-encode the whole video. Beat effects follow the offset, so with an
# effect on, a nudge still re-encodes the picture (as it must).
#
# The export encode belongs to the session, not to one adjustment: it starts in the background
# (low priority) while the preview renders, and a new adjustment with the same look simply joins
# it instead of killing it and starting over.
# ─────────────────────────────────────────────────────────────────────────────

VCACHE_KEEP = {'p': 12, 'e': 2}          # cached looks kept per session (previews / full exports)
COPY_CODECS = {'h264': [], 'hevc': ['-tag:v', 'hvc1']}
_inflight = {}                           # session root dir -> {key: _Encode}
_inflight_lock = threading.Lock()


def _t_lim(o, out_dur, preview):
    if preview and o['preview_sec'] > 0:
        return min(out_dur, float(o['preview_sec']))
    return out_dur


def _copy_args(st, vf):
    """Stream copy when the picture can go into the MP4 untouched: instant and lossless.
    (H.265/HEVC too now — it used to be re-encoded to H.264 even with nothing changed.)"""
    codec = st['vid_info']['codec']
    if vf is None and not st.get('vid_trim') and codec in COPY_CODECS:
        return ['-c:v', 'copy'] + COPY_CODECS[codec]
    return None


def _venc_args(o, vf, preview):
    if preview:
        return ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26']
    if vf is None:
        # Picture unchanged; re-encoded only for a trim (or a codec MP4 can't hold). Visually
        # lossless, exactly like the old frame-accurate trim — but now it is the only encode.
        preset = QUALITY['high'][0] if o['quality'] == 'high' else 'veryfast'
        return ['-c:v', 'libx264', '-preset', preset, '-crf', '17', '-pix_fmt', 'yuv420p']
    preset, crf = QUALITY[o['quality']]
    return ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf), '-pix_fmt', 'yuv420p']


def _track_spec(st, o, out_dur, preview):
    vf = _build_vf(st, o, out_dur, preview)
    t = _t_lim(o, out_dur, preview)
    enc = _venc_args(o, vf, preview)
    key = hashlib.sha1(json.dumps([st['vid_file'], st.get('vid_trim'), vf, enc, round(t, 3), preview])
                       .encode('utf-8')).hexdigest()[:20]
    path = os.path.join(st['root_tmp'], 'vcache', ('p_' if preview else 'e_') + key + '.mp4')
    return vf, t, enc, path


def _track_args(st, vf, t, enc, out):
    args = _vid_in(st)
    if vf:
        args += ['-filter_complex', f'[0:v:0]{vf}[v]', '-map', '[v]']
    else:
        args += ['-map', '0:v:0']
    return args + ['-an', '-t', f'{t:.3f}'] + enc + [out]


def _evict(path, kind):
    """Keep only the newest few cached looks of this kind."""
    d = os.path.dirname(path)
    try:
        files = [os.path.join(d, n) for n in os.listdir(d) if n.startswith(kind + '_') and n.endswith('.mp4')
                 and '.part' not in n]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        return
    for old in files[VCACHE_KEEP[kind]:]:
        if old != path:
            try:
                os.remove(old)
            except OSError:
                pass


def _reuse(path):
    if os.path.exists(path):
        try:
            os.utime(path)
        except OSError:
            pass
        return True
    return False


def _preview_track(job_id, st, o, out_dur, p0, p1):
    vf, t, enc, path = _track_spec(st, o, out_dur, True)
    if _reuse(path):
        return path, t
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = f'{path}.{job_id[:8]}.part.mp4'
    try:
        _ffmpeg(job_id, _track_args(st, vf, t, enc, part), dur=t, p0=p0, p1=p1, label='Rendering preview')
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    _evict(path, 'p')
    return path, t


class _Encode:
    def __init__(self, path):
        self.path = path
        self.done = threading.Event()
        self.error = None
        self.cancelled = False
        self.procs = []
        self.frac = 0.0

    def cancel(self):
        self.cancelled = True
        for p in list(self.procs):
            try:
                p.kill()
            except Exception:
                pass


def _cancel_inflight(root_tmp):
    with _inflight_lock:
        sess = _inflight.pop(root_tmp, {})
    for e in sess.values():
        e.cancel()


def _export_track(st, o, out_dur):
    """Start (or join) the session's encode of the export picture for this look.
    Returns an _Encode (already finished if cached), or None when the source can be stream-copied."""
    vf, t, enc, path = _track_spec(st, o, out_dur, False)
    if _copy_args(st, vf) is not None:
        return None
    root = st['root_tmp']
    with _inflight_lock:
        sess = _inflight.setdefault(root, {})
        e = sess.get(path)
        if e is not None and not e.cancelled:
            return e
        if _reuse(path):
            e = _Encode(path)
            e.done.set()
            return e
        for other in sess.values():              # a different look replaces any unfinished encode
            other.cancel()
        e = _Encode(path)
        sess[path] = e
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def work():
        part = f'{path}.part.mp4'
        try:
            _ffmpeg(None, _track_args(st, vf, t, enc, part), dur=t, holder=e.procs, on_progress=lambda f: setattr(e, 'frac', f))
            if e.cancelled:
                raise Cancelled()
            os.replace(part, path)
            _evict(path, 'e')
        except BaseException as ex:
            e.error = Cancelled() if e.cancelled else ex
            try:
                os.remove(part)
            except OSError:
                pass
        finally:
            with _inflight_lock:
                if _inflight.get(root, {}).get(path) is e:
                    del _inflight[root][path]
            e.done.set()

    threading.Thread(target=work, daemon=True).start()
    return e


def _wait_export(job_id, st, o, out_dur, e, p0, p1):
    """Wait for the export picture (forwarding its progress). Restarts it if it was interrupted
    while this adjustment still wants it."""
    for _ in range(3):
        while not e.done.wait(0.4):
            _check_cancel(job_id)
            _step(job_id, f'Exporting video {int(e.frac * 100)}%', p0 + (p1 - p0) * e.frac)
        _check_cancel(job_id)
        if e.error is None:
            return e.path
        if not isinstance(e.error, Cancelled):
            raise e.error
        e = _export_track(st, o, out_dur)
    raise RuntimeError('Export was interrupted — make another change to retry.')


_orig_audio_locks = collections.defaultdict(threading.Lock)


def _orig_audio_in(job_id, st, full):
    """Input args for the video's own audio (for the mix). For a full-length mux it is decoded once
    per session to a lossless float WAV, so a big video file isn't read again on every export."""
    if not full:
        return _vid_in(st)                       # previews read only their first seconds anyway
    path = os.path.join(st['root_tmp'], 'orig_audio.wav')
    with _orig_audio_locks[path]:
        if not os.path.exists(path):
            part = path + '.part.wav'
            _ffmpeg(job_id, _vid_in(st) + ['-map', '0:a:0', '-vn', '-c:a', 'pcm_f32le', part])
            os.replace(part, path)
    return ['-i', path]


def _mux_final(job_id, st, o, vtrack, wav, out_path, out_dur, t_lim, preview, p0, p1, label):
    """Picture (a cached track, or the source itself when stream-copying) + synced soundtrack → MP4."""
    if vtrack:
        args = ['-i', vtrack, '-i', wav]
        vcodec = ['-c:v', 'copy']
        orig = '2:a:0'
        if _mixing(st, o):
            args += _orig_audio_in(job_id, st, full=not preview)   # the video's own audio, for the mix
    else:
        args = _vid_in(st) + ['-i', wav]
        vcodec = _copy_args(st, None)
        orig = '0:a:0'
    fc, amap = _audio_graph(st, o, out_dur, fade_ok=(t_lim >= out_dur - 0.01), orig=orig, song='1:a:0')
    if fc:
        args += ['-filter_complex', ';'.join(fc)]
    args += ['-map', '0:v:0', '-map', amap, '-t', f'{t_lim:.3f}'] + vcodec
    args += ['-c:a', 'aac', '-b:a', '160k' if preview else '256k', '-movflags', '+faststart', out_path]
    _ffmpeg(job_id, args, dur=t_lim, p0=p0, p1=p1, label=label)


# ─────────────────────────────────────────────────────────────────────────────
# Render (shared by the first run and every adjustment)
# ─────────────────────────────────────────────────────────────────────────────

def _slice_audio(st, out_wav):
    """Cut the song at the current offset. Negative offset = song starts late (silence first)."""
    import numpy as np
    import soundfile as sf
    full = st['full_preserve_wav'] if st['preserve_pitch'] else st['full_resample_wav']
    sr = st['render_sr']
    vs = st.get('video_speed') or 1.0
    vid_out_dur = st['vid_info']['dur'] / vs
    need = int((vid_out_dur + 0.5) * sr)
    off = int(st['offset_samples'])
    total = sf.info(full).frames
    start, pad = max(0, off), max(0, -off)
    stop = min(total, start + max(0, need - pad))
    data, _ = sf.read(full, start=start, stop=stop, always_2d=True)
    if pad:
        data = np.vstack([np.zeros((pad, data.shape[1]), dtype=data.dtype), data])
    sf.write(out_wav, data, sr)
    return min(vid_out_dur, len(data) / sr)


_full_mp3_locks = collections.defaultdict(threading.Lock)


def _ensure_full_mp3(job_id, st):
    """Full song, BPM-matched, in the current pitch mode (cached in the session root dir).
    No longer built before every export: it is made in the background after the export, or on
    demand when downloaded."""
    key = 'preserve' if st['preserve_pitch'] else 'resample'
    path = os.path.join(st['root_tmp'], f'full_song_{key}.mp3')
    with _full_mp3_locks[path]:
        if os.path.exists(path):
            return path
        return _build_full_mp3(job_id, st, key, path)


def _full_mp3_later(st):
    def work():
        try:
            _ensure_full_mp3(None, st)
        except Exception:
            pass                                 # retried on download
    threading.Thread(target=work, daemon=True).start()


def _build_full_mp3(job_id, st, key, path):
    import numpy as np
    import soundfile as sf
    src = st['full_preserve_wav'] if st['preserve_pitch'] else st['full_resample_wav']
    if st['bpm_mode'] == 'song' and abs(st['mp3_stretch'] - 1.0) > 1e-3:
        import librosa
        y, sr = sf.read(st['full_preserve_wav'], always_2d=True)
        y = y.T
        rate = st['mp3_stretch']
        if st['preserve_pitch']:
            y, _ = _time_stretch(y, sr, rate)
        else:
            y = librosa.resample(y, orig_sr=sr * rate, target_sr=sr)
        peak = float(np.max(np.abs(y))) or 1.0
        src = os.path.join(st['root_tmp'], f'full_song_{key}.wav')
        sf.write(src, (y / peak * 0.9).T, sr)
    tmp_path = path + '.part.mp3'
    _ffmpeg(job_id, ['-i', src, '-codec:a', 'libmp3lame', '-b:a', '320k', tmp_path])
    os.replace(tmp_path, path)
    return path


def _render(job_id, st, out_dir, p_start=5):
    _check_cancel(job_id)
    o = st['opts']
    out_wav = os.path.join(out_dir, 'synced.wav')
    out_dur = _slice_audio(st, out_wav)
    if out_dur < 0.5:
        raise RuntimeError('Not enough song left after this offset — nudge back or reset.')
    timing = _timing(st)
    timing['out_dur'] = round(out_dur, 3)
    _push(job_id, dict(type='timing', **timing))

    # The full export picture starts right away in the background (or is already cached / running).
    export = _export_track(st, o, out_dur)

    span = 100 - p_start
    p_prev_end = p_start + span * 0.25
    vtrack, t_prev = _preview_track(job_id, st, o, out_dur, p_start, p_prev_end - 2)
    preview = os.path.join(out_dir, 'preview.mp4')
    _mux_final(job_id, st, o, vtrack, out_wav, preview, out_dur, t_prev, True,
               p_prev_end - 2, p_prev_end, 'Finishing preview')
    with jobs_lock:
        jobs[job_id]['file_preview'] = preview
        jobs[job_id]['status'] = 'preview'
    _push(job_id, {'type': 'preview_ready'})
    _prune_later(job_id, keep_last_done=True)

    _check_cancel(job_id)
    out_mp4 = os.path.join(out_dir, 'syncedVid.mp4')
    if export is not None:
        vtrack_e = _wait_export(job_id, st, o, out_dur, export, p_prev_end + 2, 95)
        _mux_final(job_id, st, o, vtrack_e, out_wav, out_mp4, out_dur, out_dur, False, 95, 99, 'Writing MP4')
    else:
        _mux_final(job_id, st, o, None, out_wav, out_mp4, out_dur, out_dur, False, p_prev_end + 2, 99,
                   'Exporting video')
    with jobs_lock:
        j = jobs[job_id]
        j.update({'status': 'done', 'file_mp4': out_mp4, 'synced_wav': out_wav, 'out_dur': out_dur})
    _step(job_id, 'Done!', 100)
    _push(job_id, {'type': 'done'})
    _prune_later(job_id, keep_last_done=False)
    _full_mp3_later(st)


# ─────────────────────────────────────────────────────────────────────────────
# First run: analysis + render
# ─────────────────────────────────────────────────────────────────────────────

def _run(job_id, p):
    import numpy as np
    import librosa
    import soundfile as sf

    tmp = _mkdtemp('beatsync_')
    refresh_caps()
    with jobs_lock:
        jobs[job_id]['tmp'] = tmp
        jobs[job_id]['status'] = 'running'
    published = False
    try:
        if not _analysis_slots.acquire(blocking=False):
            _step(job_id, 'Queued — waiting for a free worker...', 1)
            while not _analysis_slots.acquire(timeout=1):
                _check_cancel(job_id)
        try:
            _step(job_id, 'Preparing video and song...', 3)
            # Video and song are fetched / decoded at the same time (two downloads in parallel).
            song_box = {}

            def stop_other_side():
                with jobs_lock:
                    j = jobs.get(job_id)
                if j:
                    _kill_procs(j)

            def prep_song():
                try:
                    song_box['wav'] = _prepare_song(job_id, p['song_url'], p['song_file'], tmp,
                                                    p['song_trim_start'], p['song_trim_end'])
                except BaseException as e:   # noqa: B902 — re-raised in the main thread
                    song_box['err'] = e
                    if not isinstance(e, Cancelled):
                        stop_other_side()        # no point finishing the video download
            song_thread = threading.Thread(target=prep_song, daemon=True)
            song_thread.start()
            try:
                vid_file, vid_audio, vid_info, vid_trim = _prepare_video(
                    job_id, p['video_url'], p['video_file'], tmp, p['vid_trim_start'], p['vid_trim_end'])
            except BaseException:
                stop_other_side()                # stop the song download too
                song_thread.join(30)
                err = song_box.get('err')
                if err is not None and not isinstance(err, Cancelled):
                    raise err                    # the song failed first: report that, not the side effect
                raise
            if vid_info.get('audio_only'):
                _push(job_id, {'type': 'warn', 'msg': 'An audio file is in the video slot, so the MP4 has a plain '
                                                      'black picture. "Synced audio" is the mix you want.'})

            # When the export picture can't depend on the analysis (Match Video tempo, no beat
            # effects), start encoding it now, in the background, while the audio is analysed.
            opts = p['opts']
            if p['bpm_mode'] == 'video' and not opts['effects'] and vid_info['dur'] > 0:
                _export_track({'root_tmp': tmp, 'vid_file': vid_file, 'vid_trim': vid_trim,
                               'vid_info': vid_info, 'video_speed': None, 'opts': opts},
                              opts, vid_info['dur'])

            while song_thread.is_alive():
                song_thread.join(0.5)
                _check_cancel(job_id)
            if 'err' in song_box:
                raise song_box['err']
            song_wav = song_box['wav']
            _check_cancel(job_id)

            sr, render_sr = 22050, 44100
            _step(job_id, 'Loading audio...', 20)
            vid_y, _ = librosa.load(vid_audio, sr=sr, mono=True)
            song_hq, _ = librosa.load(song_wav, sr=render_sr, mono=False)
            song_y = librosa.resample(librosa.to_mono(song_hq), orig_sr=render_sr, target_sr=sr)
            if len(vid_y) < sr * 2:
                raise RuntimeError('Video is too short to detect a beat (needs at least 2 seconds).')
            if vid_info['dur'] <= 0:
                vid_info['dur'] = len(vid_y) / sr

            # Video and song are analysed at the same time (independent work, separate CPU cores).
            _step(job_id, 'Detecting video and song BPM...', 28)

            def analyse(y):
                feat = _analysis_features(y, sr)
                return feat, _detect_bpm(y, sr, feat)
            with ThreadPoolExecutor(2) as ex:
                f_vid, f_song = ex.submit(analyse, vid_y), ex.submit(analyse, song_y)
                vid_feat, (vid_tempo, vid_beats, vid_conf, vid_music_start, _) = f_vid.result()
                _check_cancel(job_id)
                _step(job_id, 'Detecting song BPM...', 38)
                song_feat, (song_tempo, _, song_conf, song_music_start, song_beats) = f_song.result()
            del song_y                       # features hold everything the analysis needs
            _check_cancel(job_id)
            _push(job_id, {'type': 'info', 'vid_music_start': round(vid_music_start / sr, 2),
                           'song_music_start': round(song_music_start / sr, 2)})

            bpm_mode = p['bpm_mode']
            video_speed = None
            stretch_ratio = 1.0
            engine = 'none'
            if bpm_mode == 'song':
                video_speed = song_tempo / vid_tempo if vid_tempo > 0 else 1.0
                _step(job_id, f'Keeping song tempo ({song_tempo:.2f} BPM); video will play at {video_speed:.3f}x...', 46)
                preserve = resample = song_hq
                mp3_stretch = vid_tempo / song_tempo if song_tempo > 0 else 1.0
            else:
                stretch_ratio = vid_tempo / song_tempo if song_tempo > 0 else 1.0
                mp3_stretch = 1.0
                if abs(stretch_ratio - 1.0) <= 1e-3:
                    _step(job_id, 'BPMs already match — no stretch needed...', 46)
                    preserve = resample = song_hq
                else:
                    _step(job_id, f'Stretching {song_tempo:.2f} → {vid_tempo:.2f} BPM ({stretch_ratio:.4f}x)...', 46)
                    with ThreadPoolExecutor(1) as ex:    # the turntable version is made alongside
                        f_res = ex.submit(librosa.resample, song_hq, orig_sr=render_sr * stretch_ratio,
                                          target_sr=render_sr)
                        preserve, engine = _time_stretch(song_hq, render_sr, stretch_ratio)
                        resample = f_res.result()
                    _check_cancel(job_id)
            _check_cancel(job_id)

            target_tempo = song_tempo if bpm_mode == 'song' else vid_tempo
            _push(job_id, {'type': 'info', 'vid_bpm': round(vid_tempo, 4), 'vid_confidence': vid_conf,
                           'song_bpm': round(song_tempo, 4), 'song_confidence': song_conf,
                           'target_bpm': round(target_tempo, 4), 'mode': bpm_mode,
                           'stretch': round(stretch_ratio, 4), 'stretch_engine': engine})
            low = [n for n, c in (('video', vid_conf), ('song', song_conf)) if c < 0.3]
            if low:
                _push(job_id, {'type': 'warn', 'msg': f'Low beat confidence on the {" and ".join(low)} — '
                                                      'check the preview and nudge if needed.'})

            _step(job_id, 'Finding beat-aligned match point...', 56)
            # The stretch / speed-up is applied to the analysis by re-timing the
            # features, not by stretching and re-analysing the audio.
            best_off, song_beat_grid, song_downbeats, song_bpb = _find_beat_offset(
                vid_feat, song_feat, vid_beats, song_beats, sr,
                vid_rate=video_speed or 1.0, song_rate=stretch_ratio)
            del vid_feat, song_feat
            _check_cancel(job_id)

            _step(job_id, 'Locking beat grid to the kick drum...', 60)
            # The grid phase is measured on the audio that will actually be heard
            # (the HQ render), because a phase-vocoder stretch smears transients
            # in the low-res analysis copy by tens of ms.
            mono = lambda a: a.mean(axis=0) if a.ndim > 1 else a
            bi, gp_preserve, conc_p = _fit_grid(mono(preserve), render_sr, song_beat_grid, 60.0 / target_tempo)
            if resample is preserve:
                gp_resample, conc_r = gp_preserve, conc_p
            else:
                _, gp_resample, conc_r = _fit_grid(mono(resample), render_sr, song_beat_grid, 60.0 / target_tempo)
            bpb = song_bpb if song_bpb > 1 else 4

            # Micro-align: nudge the offset (max ±15% of a beat) so the song's kicks
            # land exactly on the video's kicks. Only when both have a clear pulse.
            micro_ms = 0.0
            vs_ = video_speed or 1.0
            pv, conc_v = _attack_phase(vid_y, sr, bi * vs_)
            ps, conc_s = (gp_preserve, conc_p) if p['preserve_pitch'] else (gp_resample, conc_r)
            if pv is not None and conc_v > 0.5 and conc_s > 0.5:
                off_sec = best_off / sr
                err = ((ps - off_sec - pv / vs_ + bi / 2) % bi) - bi / 2
                if abs(err) < 0.15 * bi:
                    best_off = int(round((off_sec + err) * sr))
                    micro_ms = err * 1000

            _step(job_id, 'Writing high-quality audio...', 62)

            def _norm(d):
                peak = float(np.max(np.abs(d)))
                return d / peak * 0.9 if peak > 0 else d

            full_preserve = os.path.join(tmp, 'full_preserve.wav')
            full_resample = os.path.join(tmp, 'full_resample.wav')
            pn = _norm(preserve)
            sf.write(full_preserve, pn.T if pn.ndim > 1 else pn, render_sr)
            if resample is preserve:
                full_resample = full_preserve
            else:
                rn = _norm(resample)
                sf.write(full_resample, rn.T if rn.ndim > 1 else rn, render_sr)

            auto_off = int(round(best_off * render_sr / sr))
            st = {
                'root_tmp': tmp,
                'vid_file': vid_file, 'vid_info': vid_info, 'vid_trim': vid_trim,
                'full_preserve_wav': full_preserve, 'full_resample_wav': full_resample,
                'preserve_pitch': p['preserve_pitch'], 'render_sr': render_sr,
                'auto_offset_samples': auto_off, 'offset_samples': auto_off,
                'beat_interval': bi, 'beats_per_bar': int(song_bpb),
                'grid_phase': {'preserve': gp_preserve, 'resample': gp_resample},
                'bar_phase': {'preserve': _bar_phase(song_downbeats, gp_preserve, bi, bpb),
                              'resample': _bar_phase(song_downbeats, gp_resample, bi, bpb)},
                'micro_align_ms': round(micro_ms, 1),
                'song_beat_times': [round(float(x), 4) for x in song_beat_grid],
                'song_duration': round(preserve.shape[-1] / render_sr, 2),
                'video_speed': video_speed, 'bpm_mode': bpm_mode, 'stretch_ratio': stretch_ratio,
                'mp3_stretch': mp3_stretch, 'opts': p['opts'],
            }
            if p['beat_nudge']:
                st['offset_samples'] = auto_off + int(round(p['beat_nudge'] * bi * render_sr))
        finally:
            _analysis_slots.release()

        _push(job_id, {'type': 'info', 'offset': round(st['offset_samples'] / render_sr, 3),
                       'beat_interval': round(bi, 5), 'song_duration': st['song_duration'],
                       'beats_per_bar': bpb, 'meter_detected': song_bpb > 1,
                       'micro_align_ms': round(micro_ms, 1)})
        out_dir = os.path.join(tmp, 'render')
        os.makedirs(out_dir, exist_ok=True)
        with jobs_lock:
            jobs[job_id]['state'] = st           # from here on, adjustments are possible
            jobs[job_id]['out_dir'] = out_dir
        published = True
        _render(job_id, st, out_dir, p_start=66)

    except Cancelled:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['status'] = 'cancelled'
        if not published:
            _cancel_inflight(tmp)
            shutil.rmtree(tmp, ignore_errors=True)
        _push(job_id, {'type': 'cancelled'})
    except Exception as e:
        if not published:
            _cancel_inflight(tmp)
            shutil.rmtree(tmp, ignore_errors=True)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['status'] = 'error'
        _push(job_id, {'type': 'error', 'msg': _friendly(e)})


def _run_derived(job_id):
    tmp = _mkdtemp('beatsync_adj_')
    with jobs_lock:
        j = jobs.get(job_id)
        if not j:
            return
        j['tmp'] = tmp
        j['out_dir'] = tmp
        j['status'] = 'running'
        st = j['state']
    try:
        _render(job_id, st, tmp, p_start=5)
    except Cancelled:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['status'] = 'cancelled'
        _push(job_id, {'type': 'cancelled'})
    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['status'] = 'error'
        _push(job_id, {'type': 'error', 'msg': _friendly(e)})


def _derive(src_id, changes):
    """Create a new job from `src_id` with timing / pitch / option changes applied."""
    with jobs_lock:
        src = jobs.get(src_id)
        st = copy.deepcopy(src.get('state')) if src else None
        root_id = src.get('root_id') if src else None
    if not src:
        return None, ('Session expired or not found — run Match Beats again.', 404)
    if not st:
        return None, ('Still analysing — wait for the preview first.', 409)

    sr = st['render_sr']
    bi_s = st['beat_interval'] * sr
    bar_s = bi_s * _bpb(st)
    auto = st['auto_offset_samples']
    off = float(st['offset_samples'])

    if changes.get('reset'):
        off = auto
    if changes.get('seek_to') is not None:
        target = _clamp(changes['seek_to'], 0, st['song_duration'], 0) * sr
        off = auto + round((target - auto) / bar_s) * bar_s        # whole bars keep downbeats aligned
    off += _clamp(changes.get('beats', 0), -64, 64, 0) * bi_s
    off += _clamp(changes.get('ms', 0), -5000, 5000, 0) / 1000.0 * sr
    lock = changes.get('lock')
    if lock in LOCK_UNITS:
        unit = (LOCK_UNITS[lock] or _bpb(st)) * bi_s
        off = auto + round((off - auto) / unit) * unit

    vs = st.get('video_speed') or 1.0
    vid_out = st['vid_info']['dur'] / vs
    lo = -max(0.0, vid_out - 1.0) * sr
    hi = max(0.0, st['song_duration'] - 1.0) * sr
    st['offset_samples'] = int(round(max(lo, min(hi, off))))

    if 'preserve_pitch' in changes and changes['preserve_pitch'] is not None:
        st['preserve_pitch'] = bool(changes['preserve_pitch'])
    if changes.get('opts') is not None:
        st['opts'] = _clean_opts(changes['opts'], st['opts'])

    new_id = _new_job('derived', root_id=root_id, state=st)
    _cancel_lineage(root_id, except_id=new_id)
    _touch(new_id)
    threading.Thread(target=_run_derived, args=(new_id,), daemon=True).start()
    return new_id, None


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # Served raw (no Jinja) and re-read each time so template edits apply instantly.
    html = (APP_DIR / 'templates' / 'index.html').read_text(encoding='utf-8')
    return Response(html, mimetype='text/html')


@app.route('/assets/<path:name>')
def assets(name):
    return send_from_directory(APP_DIR / 'assets', name, max_age=86400)


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        return jsonify(tools.save_settings(**(request.json or {})))
    return jsonify(tools.get_settings())


@app.route('/api/caps')
def api_caps():
    rt = 'deno' if tools.find('deno') else 'node' if tools.find('node') else None
    return jsonify({'rubberband': CAPS['rubberband'], 'ffmpeg_version': CAPS['ffmpeg_version'],
                    'js_runtime': rt, 'yt_dlp': tools.version('yt-dlp'),
                    'effects': EFFECTS, 'defaults': _default_opts(),
                    'app': {'name': APP_NAME, 'version': APP_VERSION,
                            'desktop': os.environ.get('BEATSYNC_DESKTOP') == '1'}})


# ── Tools / settings ─────────────────────────────────────────────────────────

def _active_jobs():
    with jobs_lock:
        return any(j.get('status') in ('pending', 'running', 'preview') for j in jobs.values())


@app.route('/api/tools')
def api_tools():
    st = tools.status()
    return jsonify({'tools': st, 'busy': tools.busy(),
                    'missing_required': [n for n, s in st.items() if not s['ok'] and s['need'] == 'required'],
                    'cookies': bool(tools.cookies_path()), 'data_dir': str(tools.data_dir()),
                    'temp_mb': temp_usage_mb(), 'jobs_active': _active_jobs(),
                    'app': {'name': APP_NAME, 'version': APP_VERSION,
                            'desktop': os.environ.get('BEATSYNC_DESKTOP') == '1'}})


@app.route('/api/tools/install', methods=['POST'])
def api_tools_install():
    names = (request.json or {}).get('names') or 'missing'
    if names == 'missing':
        names = tools.missing()
    tools.install_async([n for n in names if n in tools.TOOLS])
    return jsonify({'ok': True, 'queued': names})


@app.route('/api/tools/update', methods=['POST'])
def api_tools_update():
    threading.Thread(target=tools.update, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/app/update')
def api_app_update():
    return jsonify(tools.check_app_update(force=request.args.get('force') == '1'))


@app.route('/api/temp/clear', methods=['POST'])
def api_temp_clear():
    if _active_jobs():
        return jsonify({'error': 'A render is running — wait for it to finish first.'}), 409
    wipe_temp()
    return jsonify({'ok': True, 'temp_mb': temp_usage_mb()})


@app.route('/api/info', methods=['POST'])
def api_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided.'}), 400
    try:
        info = _ytdlp_info(url)          # title/thumbnail still load if formats are blocked
        return jsonify({'title': info.get('title', 'Unknown'), 'thumbnail': info.get('thumbnail', ''),
                        'duration': info.get('duration', 0), 'platform': info.get('extractor_key', 'Web')})
    except Exception as e:
        return jsonify({'error': _friendly(e)}), 500


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Two forms: the raw file as the request body (?name=...), streamed straight to disk — no
    multipart parsing — or the old multipart form field 'file' (spooled into the temp folder
    and then just renamed)."""
    f = request.files.get('file') if request.mimetype == 'multipart/form-data' else None
    name = f.filename if f else (request.args.get('name') or '')
    if not f and not request.content_length:
        return jsonify({'error': 'No file received.'}), 400
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_VIDEO | ALLOWED_AUDIO:
        return jsonify({'error': f'Unsupported file type: {ext or "unknown"}'}), 400
    fname = secure_filename(name)
    if not fname.lower().endswith(ext):          # e.g. an Arabic file name becomes empty when made ASCII
        fname = 'upload' + ext
    tmp = _mkdtemp('beatsync_up_')
    dest = os.path.join(tmp, fname)
    try:
        if f:
            spool = getattr(f.stream, 'name', None)
            if isinstance(spool, str) and os.path.isfile(spool):
                f.stream.close()
                try:
                    os.replace(spool, dest)
                except OSError:                  # different drive: copy, then drop the spool
                    shutil.copyfile(spool, dest)
                    os.remove(spool)
            else:
                f.save(dest, buffer_size=4 * 1024 * 1024)
        else:
            with open(dest, 'wb') as out:
                shutil.copyfileobj(request.stream, out, 4 * 1024 * 1024)
        if os.path.getsize(dest) == 0:
            raise RuntimeError('The file is empty.')
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify({'error': f'Upload failed: {e}'}), 400
    file_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[file_id] = {'kind': 'upload', 'path': dest, 'tmp': tmp, 'ext': ext, 'created_at': time.time()}
    return jsonify({'file_id': file_id, 'name': name, 'ext': ext})


def _opt_float(v):
    if v is None or v == '':
        return None
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return None


@app.route('/api/start', methods=['POST'])
def api_start():
    d = request.json or {}
    opts = _clean_opts(d.get('opts', {}))
    if 'song_vol' in d:                         # v2 compatibility
        opts['song_vol'] = _clamp(d['song_vol'], 0.0, 1.0, 1.0)
    p = {
        'video_url': (d.get('video_url') or '').strip(), 'video_file': (d.get('video_file') or '').strip(),
        'song_url': (d.get('song_url') or '').strip(), 'song_file': (d.get('song_file') or '').strip(),
        'bpm_mode': 'song' if d.get('bpm_mode') == 'song' else 'video',
        'beat_nudge': _clamp(d.get('beat_nudge', 0), -64, 64, 0),
        'preserve_pitch': bool(d.get('preserve_pitch', True)),
        'vid_trim_start': _opt_float(d.get('vid_trim_start')), 'vid_trim_end': _opt_float(d.get('vid_trim_end')),
        'song_trim_start': _opt_float(d.get('song_trim_start')), 'song_trim_end': _opt_float(d.get('song_trim_end')),
        'opts': opts,
    }
    if not p['video_url'] and not p['video_file']:
        return jsonify({'error': 'Provide a video URL or upload a video file.'}), 400
    if not p['song_url'] and not p['song_file']:
        return jsonify({'error': 'Provide a song URL or upload a song file.'}), 400
    for a, b, name in (('vid_trim_start', 'vid_trim_end', 'Video'), ('song_trim_start', 'song_trim_end', 'Song')):
        if p[a] is not None and p[b] is not None and p[b] <= p[a]:
            return jsonify({'error': f'{name} trim: end must be after start.'}), 400
    job_id = _new_job('run')
    threading.Thread(target=_run, args=(job_id, p), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/adjust', methods=['POST'])
def api_adjust():
    """
    Body: {job_id, beats?, ms?, lock?: bar|beat|half|quarter|eighth, reset?, seek_to?,
           preserve_pitch?, opts?: {...}}
    """
    d = request.json or {}
    new_id, err = _derive((d.get('job_id') or '').strip(), d)
    if err:
        return jsonify({'error': err[0]}), err[1]
    with jobs_lock:
        st = jobs[new_id]['state']
    return jsonify({'job_id': new_id, 'timing': _timing(st), 'opts': st['opts']})


# ── v2-compatible wrappers ───────────────────────────────────────────────────

def _compat(changes):
    d = request.json or {}
    new_id, err = _derive((d.get('job_id') or '').strip(), changes(d))
    if err:
        return jsonify({'error': err[0]}), err[1]
    return jsonify({'job_id': new_id})


@app.route('/api/nudge', methods=['POST'])
def api_nudge():
    return _compat(lambda d: {'beats': d.get('beat_nudge', d.get('beats', 0)), 'ms': d.get('ms', 0),
                              'lock': d.get('lock')})


@app.route('/api/seek', methods=['POST'])
def api_seek():
    return _compat(lambda d: {'seek_to': d.get('target_time', 0)})


@app.route('/api/repitch', methods=['POST'])
def api_repitch():
    return _compat(lambda d: {'preserve_pitch': bool(d.get('preserve_pitch', True))})


@app.route('/api/rerender', methods=['POST'])
def api_rerender():
    return _compat(lambda d: {'opts': {'hflip': d.get('hflip', False), 'effects': d.get('effects', []),
                                       'filters': d.get('filters', {})}})


@app.route('/api/progress/<job_id>')
def api_progress(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j or 'queue' not in j:
        return jsonify({'error': 'Not found'}), 404
    _touch(job_id)

    def stream():
        q = j['queue']
        while True:
            try:
                msg = q.get(timeout=15)
                yield f'data: {json.dumps(msg)}\n\n'
                if msg['type'] in ('done', 'error', 'cancelled'):
                    break
            except queue.Empty:
                if j.get('status') in ('cancelled', 'error') and q.empty():
                    break
                yield 'data: {"type":"ping"}\n\n'

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _done_job(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j or j.get('status') != 'done':
        return None
    _touch(job_id)
    return j


DOWNLOAD_NAMES = {'video': 'syncedVid.mp4', 'synced': 'synced_audio.mp3',
                  'full': 'full_song_bpm_matched.mp3'}


def resolve_file(kind, job_id):
    """Return (path, suggested_name) for a finished job's download, building it if needed."""
    j = _done_job(job_id)
    if not j:
        raise FileNotFoundError('Not ready')
    if kind == 'video':
        path = j.get('file_mp4')
    elif kind == 'full':
        path = _ensure_full_mp3(None, j['state'])
    elif kind == 'synced':
        path = j.get('file_synced_mp3')
        if not path or not os.path.exists(path):
            st = j['state']
            base = j.get('out_dir')
            if not base or not j.get('synced_wav'):
                raise FileNotFoundError('File not found')
            path = os.path.join(base, 'synced_audio.mp3')
            if _mixing(st, st['opts']):          # only touch the video's audio when it is used
                args = _orig_audio_in(None, st, full=True) + ['-i', j['synced_wav']]
                fc, amap = _audio_graph(st, st['opts'], j['out_dur'], orig='0:a:0', song='1:a:0')
            else:
                args = ['-i', j['synced_wav']]
                fc, amap = _audio_graph(st, st['opts'], j['out_dur'], song='0:a:0')
            if fc:
                args += ['-filter_complex', ';'.join(fc)]
            part = path + '.part.mp3'
            args += ['-map', amap, '-t', f'{j["out_dur"]:.3f}', '-codec:a', 'libmp3lame', '-b:a', '320k', part]
            _ffmpeg(None, args)
            os.replace(part, path)
            with jobs_lock:
                j['file_synced_mp3'] = path
    else:
        raise FileNotFoundError('Unknown download type')
    if not path or not os.path.exists(path):
        raise FileNotFoundError('File not found')
    return path, DOWNLOAD_NAMES[kind]


def _send_download(kind, job_id):
    try:
        path, name = resolve_file(kind, job_id)
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        return jsonify({'error': _friendly(e)}), 500
    return send_file(path, as_attachment=True, download_name=name)


@app.route('/api/file/<job_id>')
def api_file(job_id):
    return _send_download('video', job_id)


@app.route('/api/file/audio/<job_id>')
def api_file_audio(job_id):
    return _send_download('full', job_id)


@app.route('/api/file/synced/<job_id>')
def api_file_synced(job_id):
    """The exact soundtrack of the exported video (offset, mix and fade applied) as MP3."""
    return _send_download('synced', job_id)


@app.route('/api/file/preview/<job_id>')
def api_file_preview(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j:
        return jsonify({'error': 'Not found'}), 404
    preview = j.get('file_preview')
    if not preview or not os.path.exists(preview):
        return jsonify({'error': 'Preview not ready'}), 404
    resp = send_file(preview, mimetype='video/mp4', conditional=True)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/cleanup', methods=['POST'])
def api_cleanup():
    job_id = ((request.json or {}).get('job_id') or '').strip()
    with jobs_lock:
        j = jobs.get(job_id)
        root = j.get('root_id') if j else None
    if root:
        _delete_lineage(root)
    return jsonify({'ok': True})


@app.route('/api/bpm', methods=['POST'])
def api_bpm():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400
    tmp = _mkdtemp('beatsync_bpm_')
    try:
        import librosa
        audio = _dl_audio(url, tmp, 'bpm_check')
        y, sr = librosa.load(audio, sr=22050, mono=True)
        bpm, _, confidence, music_start, _ = _detect_bpm(y, sr)
        return jsonify({'bpm': bpm, 'confidence': confidence, 'music_starts_at': round(music_start / sr, 2)})
    except Exception as e:
        return jsonify({'error': _friendly(e)}), 500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.route('/bpm')
def bpm_page():
    return '''<!doctype html><meta charset="utf-8"><title>BPM check</title>
    <body style="background:#080810;color:#e0e0f0;font-family:monospace;padding:40px">
    <input id="url" placeholder="paste url" style="width:400px;padding:8px">
    <button onclick="check()">Check BPM</button><p id="result"></p>
    <script>
    async function check() {
      const r = document.getElementById('result'); r.textContent = 'Analysing...';
      const res = await fetch('/api/bpm', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url: document.getElementById('url').value})});
      const d = await res.json();
      r.textContent = d.bpm ? `BPM: ${d.bpm} (confidence: ${(d.confidence*100).toFixed(0)}%) — music starts at ${d.music_starts_at}s`
                            : 'Error: ' + d.error;
    }
    </script>'''


def banner(port):
    rt = 'deno' if tools.find('deno') else 'node' if tools.find('node') else None
    print('=' * 60)
    print(f'  {APP_NAME} {APP_VERSION} — http://localhost:{port}')
    print(f'  ffmpeg: {CAPS["ffmpeg_version"] or "NOT FOUND"}   Rubber Band: '
          f'{"yes" if CAPS["rubberband"] else "not found (lower-quality stretch)"}')
    print(f'  yt-dlp: {tools.version("yt-dlp") or "not installed"}   JS runtime for YouTube: '
          f'{rt or "NOT FOUND (install Deno or Node 22+)"}')
    print('=' * 60)


if __name__ == '__main__':
    banner(PORT)
    app.run(debug=False, port=PORT, host=HOST, threaded=True)
