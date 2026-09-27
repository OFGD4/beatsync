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

from flask import Flask, request, jsonify, send_file, send_from_directory, Response
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
import collections
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

app = Flask(__name__)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2 GB

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
    with jobs_lock:
        for jid in [k for k, j in jobs.items() if j.get('kind') == 'upload']:
            shutil.rmtree(jobs.pop(jid).get('tmp', ''), ignore_errors=True)
    if TEMP_ROOT and os.path.isdir(TEMP_ROOT):
        for name in os.listdir(TEMP_ROOT):
            shutil.rmtree(os.path.join(TEMP_ROOT, name), ignore_errors=True)


def temp_usage_mb():
    base = TEMP_ROOT
    if not base or not os.path.isdir(base):
        with jobs_lock:
            dirs = [j['tmp'] for j in jobs.values() if j.get('tmp')]
    else:
        dirs = [base]
    total = 0
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
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
            'root_id': root_id or jid, 'tmp': None, 'state': state,
            'file_mp4': None, 'file_preview': None, 'file_synced_mp3': None,
            'cancel': threading.Event(), 'procs': [],
        }
    return jid


def _push(job_id, msg):
    with jobs_lock:
        j = jobs.get(job_id)
        if j and 'queue' in j:
            j['queue'].put(msg)


def _step(job_id, msg, pct):
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


def _cancel_job(j):
    j['cancel'].set()
    for p in list(j.get('procs', [])):
        try:
            p.kill()
        except Exception:
            pass


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
            shutil.rmtree(j['tmp'], ignore_errors=True)


def _cleanup_loop():
    while True:
        time.sleep(600)
        now = time.time()
        stale_roots, stale_uploads = [], []
        with jobs_lock:
            for jid, j in jobs.items():
                if j.get('kind') == 'upload':
                    if now - j['created_at'] > JOB_TTL_SEC:
                        stale_uploads.append(jid)
                elif j.get('root_id') == jid and now - j.get('touched_at', now) > JOB_TTL_SEC:
                    stale_roots.append(jid)
            for jid in stale_uploads:
                shutil.rmtree(jobs.pop(jid).get('tmp', ''), ignore_errors=True)
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

def _ffmpeg(job_id, args, dur=None, p0=None, p1=None, label=None, timeout=3600):
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-y']
    if job_id and dur:
        cmd += ['-progress', 'pipe:1']
    cmd += args
    with tempfile.TemporaryFile(mode='w+') as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if (job_id and dur) else subprocess.DEVNULL,
                                stderr=errf, text=True)
        j = None
        if job_id:
            with jobs_lock:
                j = jobs.get(job_id)
                if j is not None:
                    j['procs'].append(proc)
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        try:
            if j is not None and j['cancel'].is_set():
                proc.kill()
            if proc.stdout is not None:
                last = 0.0
                for line in proc.stdout:
                    if line.startswith('out_time_us=') and dur and p0 is not None:
                        try:
                            sec = int(line.split('=')[1]) / 1e6
                        except ValueError:
                            continue
                        now = time.time()
                        if now - last > 0.4:
                            last = now
                            frac = max(0.0, min(1.0, sec / dur))
                            _step(job_id, f'{label} {int(frac * 100)}%', p0 + (p1 - p0) * frac)
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
            errf.seek(0)
            tail = errf.read().strip()[-600:]
            raise RuntimeError(f'ffmpeg failed: {tail or "exit code " + str(proc.returncode)}')


def _probe_video(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate:'
         'stream_side_data=rotation:stream_tags=rotate:format=duration',
         '-of', 'json', path],
        capture_output=True, text=True, timeout=30)
    try:
        info = json.loads(r.stdout or '{}')
    except json.JSONDecodeError:
        info = {}
    streams = info.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), None)
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


def _ytdlp(job_id, args, label, p0, p1, timeout=3600):
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
    files = [f for f in Path(tmp).glob(f'{name}.*') if f.suffix.lower() in ALLOWED_VIDEO]
    if not files:
        raise RuntimeError(f'Failed to download: {name}')
    return str(files[0])


def _upload_path(file_id):
    with jobs_lock:
        up = jobs.get(file_id)
    if not up or up.get('kind') != 'upload':
        raise RuntimeError('Uploaded file expired — please upload it again.')
    return up['path'], up['ext']


def _trim_args(start, end):
    a = []
    if start is not None:
        a += ['-ss', f'{float(start):.3f}']
    if end is not None:
        a += ['-t', f'{max(0.1, float(end) - float(start or 0)):.3f}']
    return a


def _prepare_video(job_id, url, file_id, tmp, trim_start, trim_end):
    if file_id:
        src, ext = _upload_path(file_id)
        vid = os.path.join(tmp, f'vid{ext}')
        shutil.copy2(src, vid)
    else:
        vid = _dl_video(url, tmp, 'vid', job_id, 3, 9)

    if trim_start is not None or trim_end is not None:
        # Frame-accurate trim (stream-copy would snap to the previous keyframe
        # and silently shift the beat grid).
        _step(job_id, 'Trimming video (frame-accurate)...', 9)
        trimmed = os.path.join(tmp, 'vid_trimmed.mp4')
        _ffmpeg(job_id, _trim_args(trim_start, trim_end)[:2] + ['-i', vid] + _trim_args(trim_start, trim_end)[2:] +
                ['-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-crf', '17', '-preset', 'veryfast',
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', trimmed])
        vid = trimmed

    info = _probe_video(vid)
    if not info['has_audio']:
        raise RuntimeError('The video has no audio track, so its beat cannot be detected.')
    audio = os.path.join(tmp, 'vid_audio.wav')
    # Analyse the audio that is actually inside the video file (not a second download).
    _ffmpeg(job_id, ['-i', vid, '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '22050',
                     '-acodec', 'pcm_s16le', audio])
    return vid, audio, info


def _prepare_song(job_id, url, file_id, tmp, trim_start, trim_end):
    if file_id:
        src, ext = _upload_path(file_id)
        raw = os.path.join(tmp, f'song_src{ext}')
        shutil.copy2(src, raw)
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

def _refine_bpm(beat_times):
    import numpy as np
    bt = np.atleast_1d(beat_times)
    if len(bt) < 4:
        return None
    intervals = np.diff(bt)
    med = float(np.median(intervals))
    clean = intervals[(intervals > med * 0.5) & (intervals < med * 2.0)]
    if len(clean) < 2:
        return None
    return round(60.0 / float(np.mean(clean)), 4)


def _find_music_start(y, sr, hop=512, window_sec=4.0, threshold_ratio=0.15):
    import numpy as np
    import librosa
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    window_frames = int(window_sec * sr / hop)
    threshold = (float(np.max(oenv)) + 1e-9) * threshold_ratio
    for i in range(0, max(1, len(oenv) - window_frames), max(1, window_frames // 2)):
        if float(np.mean(oenv[i:i + window_frames])) >= threshold:
            return int(librosa.frames_to_samples(max(0, i - window_frames // 4), hop_length=hop))
    return 0


def _detect_bpm(y, sr):
    import numpy as np
    import librosa

    hop = 256
    music_start = _find_music_start(y, sr, hop=hop)
    y_rhythmic = y[music_start:] if music_start > 0 else y

    _, y_perc = librosa.effects.hpss(y_rhythmic, margin=3.0)
    oenv_full = librosa.onset.onset_strength(y=y_rhythmic, sr=sr, hop_length=hop)
    oenv_perc = librosa.onset.onset_strength(y=y_perc, sr=sr, hop_length=hop)

    tempo_a, _ = librosa.beat.beat_track(onset_envelope=oenv_perc, sr=sr, hop_length=hop, units='time')
    tempo_a = float(np.atleast_1d(tempo_a)[0])

    tempogram = librosa.feature.tempogram(onset_envelope=oenv_full, sr=sr, hop_length=hop)
    bpm_axis = librosa.tempo_frequencies(tempogram.shape[0], sr=sr, hop_length=hop)
    tempo_b = float(bpm_axis[np.argmax(np.mean(tempogram, axis=1))])

    try:
        plp = librosa.beat.plp(onset_envelope=oenv_perc, sr=sr, hop_length=hop)
        plp_frames = np.where((plp[:-1] < plp[1:]) & (plp[1:] < np.roll(plp, -2)[1:]))[0]
        if len(plp_frames) > 2:
            intervals = np.diff(librosa.frames_to_time(plp_frames, sr=sr, hop_length=hop))
            tempo_c = float(60.0 / np.median(intervals[intervals > 0]))
        else:
            tempo_c = tempo_a
    except Exception:
        tempo_c = tempo_a

    raw = [tempo_a, tempo_b, tempo_c]
    anchor = None
    for i in range(len(raw)):
        for j in range(i + 1, len(raw)):
            ratio = raw[i] / raw[j] if raw[j] != 0 else 999
            if 0.9 <= ratio <= 1.1:
                anchor = (raw[i] + raw[j]) / 2.0
                break
        if anchor:
            break
    if anchor is None:
        anchor = tempo_a

    candidates = {round(anchor * m, 2) for m in (0.5, 1.0, 2.0) if 40 <= round(anchor * m, 2) <= 240}
    best_bpm, best_score = anchor, -1.0
    for cand in candidates:
        idx = int(np.argmin(np.abs(bpm_axis - cand)))
        score = float(np.mean(tempogram[idx]))
        ratio = cand / anchor if anchor != 0 else 1.0
        if ratio > 1.5:
            score *= 0.85
        elif ratio < 0.67:
            score *= 0.80
        if score > best_score:
            best_score, best_bpm = score, cand

    _, beat_frames = librosa.beat.beat_track(onset_envelope=oenv_perc, sr=sr, hop_length=hop, bpm=best_bpm)
    beat_frames = librosa.onset.onset_backtrack(beat_frames, oenv_full)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop) + (music_start / sr)

    refined = _refine_bpm(beat_times)
    if refined is not None:
        best_bpm = refined

    idx_best = int(np.argmin(np.abs(bpm_axis - best_bpm)))
    peak_energy = float(np.mean(tempogram[idx_best]))
    total_energy = float(np.sum(np.mean(tempogram, axis=1)) + 1e-9)
    confidence = min(1.0, peak_energy / (total_energy / len(bpm_axis)) / 10.0)
    return round(best_bpm, 4), beat_times, round(confidence, 3), music_start


def _low_freq_onset_strength(y, sr, hop=512, fmax=250):
    import numpy as np
    import librosa
    S = np.abs(librosa.stft(y, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr)
    S_low_db = librosa.amplitude_to_db(S[freqs <= fmax, :], ref=np.max)
    onset_env = np.maximum(0, np.diff(S_low_db, axis=1)).mean(axis=0)
    return np.concatenate([[0], onset_env])


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


def _detect_downbeats(y, sr, beat_times, hop=256):
    import numpy as np
    import librosa
    if len(beat_times) < 4:
        return beat_times, 1
    low_oenv = _low_freq_onset_strength(y, sr, hop=hop)
    beat_frames = np.clip(librosa.time_to_frames(beat_times, sr=sr, hop_length=hop), 0, len(low_oenv) - 1)
    beat_energies = low_oenv[beat_frames]
    bpb, phase = _detect_meter(beat_energies)
    idx = np.arange(phase, len(beat_times), bpb)
    if float(np.mean(beat_energies[idx])) < (float(np.mean(beat_energies)) + 1e-9) * 1.05:
        return beat_times, 1
    return beat_times[idx], bpb


def _lcm(a, b):
    from math import gcd
    return a * b // gcd(a, b)


def _find_beat_offset(vid_y, song_y, vid_beats, sr, hop=256):
    """Returns (offset_samples, song_beat_times, song_downbeats, song_beats_per_bar)."""
    import numpy as np
    import librosa

    _, song_beat_times = librosa.beat.beat_track(y=song_y, sr=sr, hop_length=hop, units='time')
    song_beat_times = np.atleast_1d(song_beat_times)
    if len(song_beat_times) == 0 or len(vid_beats) == 0:
        return 0, song_beat_times, song_beat_times, 1

    vid_duration, song_duration = len(vid_y) / sr, len(song_y) / sr
    vid_downbeats, vid_bpb = _detect_downbeats(vid_y, sr, vid_beats, hop)
    song_downbeats, song_bpb = _detect_downbeats(song_y, sr, song_beat_times, hop)
    vid_first_downbeat = float(vid_downbeats[0]) if len(vid_downbeats) > 0 else float(vid_beats[0])

    lcm_beats = _lcm(vid_bpb, song_bpb)
    super_step = lcm_beats // song_bpb
    if lcm_beats <= 28 and len(song_downbeats) >= 2 and super_step >= 1:
        super_times = song_downbeats[::super_step]
        search_times = super_times if len(super_times) >= 2 else (
            song_downbeats if len(song_downbeats) >= 3 else song_beat_times)
    elif len(song_downbeats) >= 3:
        search_times = song_downbeats
    else:
        search_times = song_beat_times

    vid_env = _low_freq_onset_strength(vid_y, sr, hop=hop)
    song_env = _low_freq_onset_strength(song_y, sr, hop=hop)
    vid_env = vid_env / (np.max(vid_env) + 1e-9)
    song_env = song_env / (np.max(song_env) + 1e-9)
    vid_frames, song_frames = len(vid_env), len(song_env)
    best_score, best_offset_sec = -np.inf, 0.0

    for sb_time in search_times:
        offset_sec = float(sb_time) - vid_first_downbeat
        if offset_sec < 0 or offset_sec + vid_duration > song_duration:
            continue
        start_frame = int(offset_sec * sr / hop)
        segment = song_env[start_frame:start_frame + vid_frames]
        if len(segment) < vid_frames:
            continue
        score = float(np.dot(vid_env, segment)) + 0.2 * float(np.sum(np.sort(segment)[-max(1, vid_frames // 20):]))
        if score > best_score:
            best_score, best_offset_sec = score, offset_sec

    if best_offset_sec > 0 and len(song_beat_times) > 1:
        nearest = float(song_beat_times[np.argmin(np.abs(song_beat_times - best_offset_sec))])
        if abs(nearest - best_offset_sec) < float(np.median(np.diff(song_beat_times))) * 0.5:
            best_offset_sec = nearest

    # Fine-tune ±50 ms by cross-correlating onset envelopes
    best_offset_samp = int(best_offset_sec * sr)
    fine = int(0.05 * sr)
    if best_offset_samp > fine and len(vid_y) > sr:
        vid_oenv = librosa.onset.onset_strength(y=vid_y, sr=sr, hop_length=hop)
        seg_start = max(0, best_offset_samp - fine)
        seg_end = min(len(song_y), best_offset_samp + len(vid_y) + fine)
        if seg_end > seg_start + sr:
            song_oenv = librosa.onset.onset_strength(y=song_y[seg_start:seg_end], sr=sr, hop_length=hop)
            if len(song_oenv) > len(vid_oenv):
                corr = np.correlate(song_oenv, vid_oenv, mode='valid')
                if len(corr):
                    refined = seg_start + int(np.argmax(corr)) * hop
                    if abs(refined - best_offset_samp) <= fine:
                        best_offset_samp = refined

    return max(0, best_offset_samp), song_beat_times, np.atleast_1d(song_downbeats), int(song_bpb)


def _kick_attacks(y, sr, fc=180.0):
    """Kick-drum attack times (s) + strengths: rising edge of the low-band log envelope (~1 ms precision)."""
    import numpy as np
    from scipy.signal import butter, sosfiltfilt, find_peaks
    sos = butter(4, fc, 'low', fs=sr, output='sos')
    yl = sosfiltfilt(sos, y)
    hop = max(1, int(sr * 0.001))
    win = max(1, int(sr * 0.010))
    e = np.sqrt(np.convolve(yl * yl, np.ones(win) / win, mode='same'))[::hop] + 1e-9
    db = 20 * np.log10(e / e.max())
    lag = 5
    rise = np.maximum(0, db[lag:] - db[:-lag])
    if rise.max() <= 0:
        return np.array([]), np.array([])
    pk, _ = find_peaks(rise, height=0.3 * rise.max(), distance=60)
    return (pk + lag / 2) * hop / sr + 0.0063, rise[pk]


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


def _time_stretch_pair(y_hq, sr_hq, y_lo, sr_lo, rate):
    """Stretch (rate>1 = faster). Uses Rubber Band for both or librosa for both."""
    import librosa
    if CAPS['rubberband']:
        try:
            import pyrubberband as pyrb
            hq = pyrb.time_stretch(y_hq.T if y_hq.ndim == 2 else y_hq, sr_hq, rate)
            hq = hq.T if y_hq.ndim == 2 else hq
            lo = pyrb.time_stretch(y_lo, sr_lo, rate)
            return hq, lo, 'rubberband'
        except Exception:
            pass
    return (librosa.effects.time_stretch(y_hq, rate=rate),
            librosa.effects.time_stretch(y_lo, rate=rate), 'librosa')


def _time_stretch(y, sr, rate):
    import librosa
    if CAPS['rubberband']:
        try:
            import pyrubberband as pyrb
            out = pyrb.time_stretch(y.T if y.ndim == 2 else y, sr, rate)
            return out.T if y.ndim == 2 else out
        except Exception:
            pass
    return librosa.effects.time_stretch(y, rate=rate)


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
    if o['hflip']:
        parts.append('hflip')
    parts += _color_filters(o['filters'])
    period, phase = _trigger(st, o['fx_division'])
    parts += _beat_effects(o, W, H, period, phase)
    parts += _overlay_filters(o['filters'])
    fo = o['fade_out']
    if fo > 0 and out_dur > fo * 1.5 and (not preview or o['preview_sec'] <= 0 or o['preview_sec'] >= out_dur):
        parts.append(f'fade=t=out:st={out_dur - fo:.3f}:d={fo:.3f}')
    if preview and H > 720:
        parts.append('scale=-2:720')
    if parts or preview:
        parts.append('format=yuv420p')
    return ','.join(parts) if parts else None


def _audio_graph(st, o, out_dur, fade_ok=True):
    """Returns (filter_complex_parts, amap). Input 0 = video, input 1 = song wav."""
    vi = st['vid_info']
    new_vol = o['song_vol']
    orig_vol = 1.0 - new_vol
    mixing = orig_vol > 0.01 and vi.get('has_audio')
    fo = o['fade_out']
    tail = f',afade=t=out:st={out_dur - fo:.3f}:d={fo:.3f}' if (fade_ok and fo > 0 and out_dur > fo * 1.5) else ''
    if mixing:
        k = 1.0 if CAPS['amix_normalize'] else 2.0      # old ffmpeg: amix halves each input
        vs = st.get('video_speed') or 1.0
        at = (_build_atempo(vs) + ',') if abs(vs - 1.0) > 1e-3 else ''   # keep original audio in sync with sped video
        norm = ':normalize=0' if CAPS['amix_normalize'] else ''
        return ([f'[0:a:0]{at}aformat=channel_layouts=stereo,volume={orig_vol * k:.4f}[oa]',
                 f'[1:a:0]aformat=channel_layouts=stereo,volume={new_vol * k:.4f}[na]',
                 f'[oa][na]amix=inputs=2:duration=longest{norm}{tail}[a]'], '[a]')
    if abs(new_vol - 1.0) > 0.004 or tail:
        return [f'[1:a:0]volume={new_vol:.4f}{tail}[a]'], '[a]'
    return [], '1:a:0'


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


def _mux(job_id, st, wav, out_path, out_dur, preview, p0, p1, label):
    o = st['opts']
    t_lim = out_dur
    if preview and o['preview_sec'] > 0:
        t_lim = min(out_dur, float(o['preview_sec']))
    vf = _build_vf(st, o, out_dur, preview)
    fc, amap = _audio_graph(st, o, out_dur, fade_ok=(t_lim >= out_dur - 0.01))
    vmap = '0:v:0'
    if vf:
        fc = [f'[0:v:0]{vf}[v]'] + fc
        vmap = '[v]'
    args = ['-i', st['vid_file'], '-i', wav]
    if fc:
        args += ['-filter_complex', ';'.join(fc)]
    args += ['-map', vmap, '-map', amap, '-t', f'{t_lim:.3f}']
    if preview:
        args += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26', '-c:a', 'aac', '-b:a', '160k']
    elif vf is None and st['vid_info']['codec'] == 'h264':
        args += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k']
    else:
        preset, crf = QUALITY[o['quality']]
        args += ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf), '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-b:a', '256k']
    args += ['-movflags', '+faststart', out_path]
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


def _ensure_full_mp3(job_id, st):
    """Full song, BPM-matched, in the current pitch mode (cached in the session root dir)."""
    import numpy as np
    import soundfile as sf
    key = 'preserve' if st['preserve_pitch'] else 'resample'
    path = os.path.join(st['root_tmp'], f'full_song_{key}.mp3')
    if os.path.exists(path):
        return path
    src = st['full_preserve_wav'] if st['preserve_pitch'] else st['full_resample_wav']
    if st['bpm_mode'] == 'song' and abs(st['mp3_stretch'] - 1.0) > 1e-3:
        import librosa
        y, sr = sf.read(st['full_preserve_wav'], always_2d=True)
        y = y.T
        rate = st['mp3_stretch']
        if st['preserve_pitch']:
            y = _time_stretch(y, sr, rate)
        else:
            y = librosa.resample(y, orig_sr=sr * rate, target_sr=sr)
        peak = float(np.max(np.abs(y))) or 1.0
        src = os.path.join(st['root_tmp'], f'full_song_{key}.wav')
        sf.write(src, (y / peak * 0.9).T, sr)
    tmp_path = path + '.part.mp3'
    _ffmpeg(job_id, ['-i', src, '-codec:a', 'libmp3lame', '-b:a', '320k', tmp_path])
    os.replace(tmp_path, path)
    return path


def _render(job_id, st, tmp, p_start=5):
    _check_cancel(job_id)
    out_wav = os.path.join(tmp, 'synced.wav')
    out_dur = _slice_audio(st, out_wav)
    if out_dur < 0.5:
        raise RuntimeError('Not enough song left after this offset — nudge back or reset.')
    timing = _timing(st)
    timing['out_dur'] = round(out_dur, 3)
    _push(job_id, dict(type='timing', **timing))

    span = 100 - p_start
    p_prev_end = p_start + span * 0.25
    preview = os.path.join(tmp, 'preview.mp4')
    _mux(job_id, st, out_wav, preview, out_dur, True, p_start, p_prev_end, 'Rendering preview')
    with jobs_lock:
        jobs[job_id]['file_preview'] = preview
        jobs[job_id]['status'] = 'preview'
    _push(job_id, {'type': 'preview_ready'})

    _check_cancel(job_id)
    _step(job_id, 'Preparing full-song MP3...', p_prev_end + 2)
    mp3 = _ensure_full_mp3(job_id, st)

    out_mp4 = os.path.join(tmp, 'syncedVid.mp4')
    _mux(job_id, st, out_wav, out_mp4, out_dur, False, p_prev_end + 5, 99, 'Exporting video')
    with jobs_lock:
        j = jobs[job_id]
        j.update({'status': 'done', 'file_mp4': out_mp4, 'file_mp3': mp3, 'synced_wav': out_wav,
                  'out_dur': out_dur})
    _step(job_id, 'Done!', 100)
    _push(job_id, {'type': 'done'})


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
            _step(job_id, 'Preparing video...', 3)
            vid_file, vid_audio, vid_info = _prepare_video(
                job_id, p['video_url'], p['video_file'], tmp, p['vid_trim_start'], p['vid_trim_end'])
            _check_cancel(job_id)

            _step(job_id, 'Preparing song...', 12)
            song_wav = _prepare_song(job_id, p['song_url'], p['song_file'], tmp,
                                     p['song_trim_start'], p['song_trim_end'])
            _check_cancel(job_id)

            sr, render_sr = 22050, 44100
            _step(job_id, 'Loading audio...', 20)
            vid_y, _ = librosa.load(vid_audio, sr=sr, mono=True)
            song_y, _ = librosa.load(song_wav, sr=sr, mono=True)
            song_hq, _ = librosa.load(song_wav, sr=render_sr, mono=False)
            if len(vid_y) < sr * 2:
                raise RuntimeError('Video is too short to detect a beat (needs at least 2 seconds).')
            if vid_info['dur'] <= 0:
                vid_info['dur'] = len(vid_y) / sr

            _step(job_id, 'Detecting video BPM...', 28)
            vid_tempo, vid_beats, vid_conf, vid_music_start = _detect_bpm(vid_y, sr)
            _check_cancel(job_id)
            _step(job_id, 'Detecting song BPM...', 38)
            song_tempo, _song_beats, song_conf, song_music_start = _detect_bpm(song_y, sr)
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
                song_stretched, preserve, resample = song_y, song_hq, song_hq
                if abs(video_speed - 1.0) > 1e-3:
                    vid_y_aligned = librosa.effects.time_stretch(vid_y, rate=video_speed)
                    vid_beats_aligned = vid_beats / video_speed
                else:
                    vid_y_aligned, vid_beats_aligned = vid_y, vid_beats
                mp3_stretch = vid_tempo / song_tempo if song_tempo > 0 else 1.0
            else:
                stretch_ratio = vid_tempo / song_tempo if song_tempo > 0 else 1.0
                mp3_stretch = 1.0
                if abs(stretch_ratio - 1.0) <= 1e-3:
                    _step(job_id, 'BPMs already match — no stretch needed...', 46)
                    song_stretched, preserve, resample = song_y, song_hq, song_hq
                else:
                    _step(job_id, f'Stretching {song_tempo:.2f} → {vid_tempo:.2f} BPM ({stretch_ratio:.4f}x)...', 46)
                    preserve, song_stretched, engine = _time_stretch_pair(song_hq, render_sr, song_y, sr, stretch_ratio)
                    resample = librosa.resample(song_hq, orig_sr=render_sr * stretch_ratio, target_sr=render_sr)
                vid_y_aligned, vid_beats_aligned = vid_y, vid_beats
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
            best_off, song_beat_grid, song_downbeats, song_bpb = _find_beat_offset(
                vid_y_aligned, song_stretched, vid_beats_aligned, sr)
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
                'vid_file': vid_file, 'vid_info': vid_info,
                'full_preserve_wav': full_preserve, 'full_resample_wav': full_resample,
                'preserve_pitch': p['preserve_pitch'], 'render_sr': render_sr,
                'auto_offset_samples': auto_off, 'offset_samples': auto_off,
                'beat_interval': bi, 'beats_per_bar': int(song_bpb),
                'grid_phase': {'preserve': gp_preserve, 'resample': gp_resample},
                'bar_phase': {'preserve': _bar_phase(song_downbeats, gp_preserve, bi, bpb),
                              'resample': _bar_phase(song_downbeats, gp_resample, bi, bpb)},
                'micro_align_ms': round(micro_ms, 1),
                'song_beat_times': [round(float(x), 4) for x in song_beat_grid],
                'song_duration': round(len(song_stretched) / sr, 2),
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
        with jobs_lock:
            jobs[job_id]['state'] = st           # from here on, adjustments are possible
        published = True
        _render(job_id, st, tmp, p_start=66)

    except Cancelled:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['status'] = 'cancelled'
        if not published:
            shutil.rmtree(tmp, ignore_errors=True)
        _push(job_id, {'type': 'cancelled'})
    except Exception as e:
        if not published:
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
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file received.'}), 400
    fname = secure_filename(f.filename) or 'upload'
    ext = Path(fname).suffix.lower()
    if ext not in ALLOWED_VIDEO | ALLOWED_AUDIO:
        return jsonify({'error': f'Unsupported file type: {ext or "unknown"}'}), 400
    tmp = _mkdtemp('beatsync_up_')
    dest = os.path.join(tmp, fname)
    f.save(dest)
    file_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[file_id] = {'kind': 'upload', 'path': dest, 'tmp': tmp, 'ext': ext, 'created_at': time.time()}
    return jsonify({'file_id': file_id, 'name': f.filename, 'ext': ext})


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
        path = j.get('file_mp3')
    elif kind == 'synced':
        path = j.get('file_synced_mp3')
        if not path or not os.path.exists(path):
            st = j['state']
            path = os.path.join(j['tmp'], 'synced_audio.mp3')
            fc, amap = _audio_graph(st, st['opts'], j['out_dur'])
            args = ['-i', st['vid_file'], '-i', j['synced_wav']]
            if fc:
                args += ['-filter_complex', ';'.join(fc)]
            args += ['-map', amap, '-t', f'{j["out_dur"]:.3f}', '-codec:a', 'libmp3lame', '-b:a', '320k', path]
            _ffmpeg(None, args)
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
        bpm, _, confidence, music_start = _detect_bpm(y, sr)
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
