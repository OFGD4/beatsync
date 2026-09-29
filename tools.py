"""
External tools BeatSync needs, where they live, and how they are installed/updated.

  ffmpeg + ffprobe   required        downloaded on first run (BtbN GPL build)
  yt-dlp             URL downloads   downloaded on first run, self-updates daily
  deno               URL downloads   downloaded on first run (YouTube needs a JS runtime)
  rubberband         audio quality   shipped with the installer (can also be downloaded)

Lookup order for every tool:
  1. user folder   %LOCALAPPDATA%\\BeatSync\\bin   (downloaded here, can self-update)
  2. bundled       <app folder>\\bin               (shipped with the installer)
  3. system PATH
"""

import os
import re
import sys
import json
import time
import shutil
import tarfile
import zipfile
import tempfile
import threading
import subprocess
import urllib.request
from pathlib import Path

from app_info import APP_NAME, APP_VERSION, GITHUB_REPO

IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
EXE = '.exe' if IS_WIN else ''
FROZEN = bool(getattr(sys, 'frozen', False))
NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
UA = {'User-Agent': f'{APP_NAME}/{APP_VERSION}'}


# ─────────────────────────────────────────────────────────────────────────────
# Folders
# ─────────────────────────────────────────────────────────────────────────────

def app_dir() -> Path:
    """Folder that contains the program (BeatSync.exe, or this file when run from source)."""
    return Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Read-only resources (templates, assets). Inside the bundle when frozen."""
    return Path(getattr(sys, '_MEIPASS', app_dir()))


def data_dir() -> Path:
    """Per-user writable folder: tools, temp files, logs, cookies, settings."""
    env = os.environ.get('BEATSYNC_HOME')
    if env:
        base = Path(env)
    elif IS_WIN:
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / APP_NAME
    elif IS_MAC:
        base = Path.home() / 'Library' / 'Application Support' / APP_NAME
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / APP_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


USER_BIN = data_dir() / 'bin'
BUNDLED_BIN = app_dir() / 'bin'


def logs_dir() -> Path:
    d = data_dir() / 'logs'
    d.mkdir(exist_ok=True)
    return d


def cookies_path():
    for p in (data_dir() / 'cookies.txt', app_dir() / 'cookies.txt'):
        if p.is_file():
            return str(p)
    return None


def setup_path():
    """Put the user and bundled tool folders first on PATH so every subprocess finds them."""
    parts = os.environ.get('PATH', '').split(os.pathsep)
    for d in (str(BUNDLED_BIN), str(USER_BIN)):
        if d not in parts:
            parts.insert(0, d)
    os.environ['PATH'] = os.pathsep.join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Tool catalogue
# ─────────────────────────────────────────────────────────────────────────────

GH = 'https://github.com'
TOOLS = {
    'ffmpeg': {'label': 'ffmpeg + ffprobe', 'files': ['ffmpeg', 'ffprobe'], 'need': 'required', 'size_mb': 150,
               'why': 'Reads and writes every audio and video file.'},
    'yt-dlp': {'label': 'yt-dlp', 'files': ['yt-dlp'], 'need': 'links', 'size_mb': 18,
               'why': 'Downloads from YouTube, TikTok, X, Instagram and more.'},
    'deno': {'label': 'Deno', 'files': ['deno'], 'need': 'links', 'size_mb': 45,
             'why': 'YouTube only unlocks its formats with a JavaScript runtime.'},
    'rubberband': {'label': 'Rubber Band', 'files': ['rubberband'], 'need': 'quality', 'size_mb': 3,
                   'why': 'Studio-quality tempo stretching (fallback is noticeably worse).'},
}

SOURCES = {
    'win': {
        'ffmpeg': {'btbn': r'^ffmpeg-n([\d.]+)-latest-win64-gpl-[\d.]+\.zip$',
                   'url': f'{GH}/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'},
        'yt-dlp': {'url': f'{GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe'},
        'deno': {'url': f'{GH}/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip'},
        'rubberband': {'url': 'https://breakfastquay.com/files/releases/rubberband-4.0.0-gpl-executable-windows.zip'},
    },
    'linux': {
        'ffmpeg': {'btbn': r'^ffmpeg-n([\d.]+)-latest-linux64-gpl-[\d.]+\.tar\.xz$',
                   'url': f'{GH}/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz'},
        'yt-dlp': {'url': f'{GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux'},
        'deno': {'url': f'{GH}/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip'},
    },
}


def _plat():
    return 'win' if IS_WIN else 'mac' if IS_MAC else 'linux'


def find(filename):
    """Full path of a tool executable (e.g. 'ffprobe'), or None."""
    for d in (USER_BIN, BUNDLED_BIN):
        p = d / (filename + EXE)
        if p.is_file():
            return str(p)
    return shutil.which(filename)


def _source(path):
    if not path:
        return None
    p = Path(path).resolve()
    if USER_BIN.resolve() in p.parents:
        return 'app'
    if BUNDLED_BIN.resolve() in p.parents:
        return 'bundled'
    return 'system'


# ─────────────────────────────────────────────────────────────────────────────
# Versions (cached per file + mtime)
# ─────────────────────────────────────────────────────────────────────────────

_ver_cache = {}


def version(name):
    path = find(TOOLS[name]['files'][0])
    if not path:
        return None
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _ver_cache:
        return _ver_cache[key]
    arg = {'ffmpeg': '-version', 'rubberband': '--version'}.get(name, '--version')
    try:
        r = subprocess.run([path, arg], capture_output=True, text=True, timeout=20, **NO_WINDOW)
        text = (r.stdout or '') + '\n' + (r.stderr or '')
    except Exception:
        return None
    v = None
    if name == 'ffmpeg':
        m = re.search(r'ffmpeg version (\S+)', text)
        v = m.group(1) if m else None
    elif name == 'deno':
        m = re.search(r'deno (\d+\.\d+\.\d+)', text)
        v = m.group(1) if m else None
    else:
        m = re.search(r'(\d+(?:\.\d+)+)', text)
        v = m.group(1) if m else None
    _ver_cache[key] = v or 'installed'
    return _ver_cache[key]


# ─────────────────────────────────────────────────────────────────────────────
# Install / update
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_jobs = {}          # name -> {'state': 'queued'|'installing'|'done'|'error', 'pct', 'msg'}
_queue_thread = None
_queue = []


def _set(name, **kw):
    with _lock:
        _jobs.setdefault(name, {}).update(kw)


def _http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={**UA, 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _resolve_url(name):
    src = SOURCES.get(_plat(), {}).get(name)
    if not src:
        raise RuntimeError(f'No automatic download for {name} on this system — install it and add it to PATH.')
    if 'btbn' in src:
        # Prefer the newest *release* build (e.g. n8.0) over the nightly master build.
        try:
            rel = _http_json('https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest')
            best = None
            for a in rel.get('assets', []):
                m = re.match(src['btbn'], a.get('name', ''))
                if m:
                    ver = tuple(int(x) for x in m.group(1).split('.') if x.isdigit())
                    if best is None or ver > best[0]:
                        best = (ver, a['browser_download_url'])
            if best:
                return best[1]
        except Exception:
            pass
    return src['url']


def _download(url, dest, name, p0, p1):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        done, last = 0, 0.0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 0.25:
                last = now
                mb = done / 1e6
                if total:
                    frac = done / total
                    _set(name, pct=int(p0 + (p1 - p0) * frac), msg=f'Downloading {mb:.0f} / {total / 1e6:.0f} MB')
                else:
                    _set(name, msg=f'Downloading {mb:.0f} MB')
    if total and os.path.getsize(dest) < total:
        raise RuntimeError('Download was cut off — check your connection and retry.')


def _extract(archive, name, out_dir):
    """Pull the executables we need (plus DLLs for Rubber Band) out of an archive, flat."""
    wanted = {f + EXE for f in TOOLS[name]['files']}
    take_all_bins = name == 'rubberband'
    got = set()

    def pick(member_name):
        base = member_name.replace('\\', '/').rsplit('/', 1)[-1]
        if not base:
            return None
        if base in wanted:
            return base
        if take_all_bins and base.lower().endswith(('.exe', '.dll')):
            return base
        return None

    if archive.endswith('.zip'):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = pick(info.filename)
                if base and not info.is_dir():
                    with z.open(info) as src, open(os.path.join(out_dir, base), 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    got.add(base)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                base = pick(m.name)
                if base and m.isfile():
                    with t.extractfile(m) as src, open(os.path.join(out_dir, base), 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    got.add(base)
    missing = wanted - got
    if missing:
        raise RuntimeError(f'Archive did not contain {", ".join(sorted(missing))}.')
    return got


def install(name, target_dir=None, progress=None):
    """Download and install one tool (blocking). Returns a status message."""
    target = Path(target_dir) if target_dir else USER_BIN
    target.mkdir(parents=True, exist_ok=True)
    _set(name, state='installing', pct=1, msg='Finding the latest version...', error=None)
    work = tempfile.mkdtemp(prefix=f'dl_{name}_', dir=str(data_dir()))
    try:
        url = _resolve_url(name)
        fname = url.rsplit('/', 1)[-1].split('?')[0] or name
        archive = os.path.join(work, fname)
        _download(url, archive, name, 2, 90)
        _set(name, pct=92, msg='Unpacking...')
        # Stage inside the target folder: the final rename then never crosses drives
        # (e.g. download on C:, app or repo on D:), which Windows refuses to do.
        stage = tempfile.mkdtemp(prefix='.stage_', dir=str(target))
        if archive.endswith(('.zip', '.tar.xz', '.tar.gz', '.tgz')):
            files = _extract(archive, name, stage)
        else:
            files = {TOOLS[name]['files'][0] + EXE}
            shutil.move(archive, os.path.join(stage, next(iter(files))))
        for f in files:
            dst = target / f
            src = os.path.join(stage, f)
            if not IS_WIN:
                os.chmod(src, 0o755)
            try:
                os.replace(src, dst)
            except PermissionError:
                raise RuntimeError(f'{f} is in use - wait for the current render to finish, then retry.')
        shutil.rmtree(stage, ignore_errors=True)
        _ver_cache.clear()
        v = version(name) if target == USER_BIN else None
        msg = f'Installed {TOOLS[name]["label"]}' + (f' {v}' if v and v != 'installed' else '')
        _set(name, state='done', pct=100, msg=msg)
        if progress:
            progress(msg)
        return msg
    except Exception as e:
        _set(name, state='error', pct=0, msg='', error=str(e)[:300])
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for leftover in target.glob('.stage_*'):
            shutil.rmtree(leftover, ignore_errors=True)


def _worker():
    global _queue_thread
    while True:
        with _lock:
            if not _queue:
                _queue_thread = None
                return
            name = _queue.pop(0)
        try:
            install(name)
        except Exception:
            pass                                    # error is recorded in _jobs


def install_async(names):
    """Queue downloads (one at a time, in order). Returns immediately."""
    global _queue_thread
    with _lock:
        for n in names:
            if n in TOOLS and n not in _queue and _jobs.get(n, {}).get('state') not in ('installing', 'queued'):
                _queue.append(n)
                _jobs[n] = {'state': 'queued', 'pct': 0, 'msg': 'Waiting...', 'error': None}
        if _queue_thread is None and _queue:
            _queue_thread = threading.Thread(target=_worker, daemon=True)
            _queue_thread.start()


def busy():
    with _lock:
        return any(j.get('state') in ('queued', 'installing') for j in _jobs.values())


def _state_file():
    return data_dir() / 'state.json'


def _load_state():
    try:
        return json.loads(_state_file().read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_state(**kw):
    s = _load_state()
    s.update(kw)
    try:
        _state_file().write_text(json.dumps(s, indent=2), encoding='utf-8')
    except Exception:
        pass


def get_settings():
    return _load_state().get('settings', {})


def save_settings(**kw):
    s = get_settings()
    s.update({k: v for k, v in kw.items() if k in ('setup_dismissed',)})
    _save_state(settings=s)
    return s


def update(names=('yt-dlp', 'deno')):
    """Self-update the tools that support it. Returns {name: message}."""
    out = {}
    for name in names:
        path = find(TOOLS[name]['files'][0])
        src = _source(path)
        if not path:
            out[name] = 'not installed'
            continue
        if src != 'app':
            out[name] = ('managed outside BeatSync — update it with pip / your package manager'
                         if src == 'system' else 'bundled with the app — updates with the installer')
            continue
        cmd = [path, '-U'] if name == 'yt-dlp' else [path, 'upgrade'] if name == 'deno' else None
        if not cmd:
            out[name] = 'no self-update'
            continue
        _set(name, state='installing', pct=50, msg='Updating...', error=None)
        before = version(name)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, **NO_WINDOW)
            text = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()
            last = text[-1] if text else ''
            if r.returncode != 0:
                raise RuntimeError(last or f'exit code {r.returncode}')
            _ver_cache.clear()
            now = version(name)
            out[name] = f'updated {before} → {now}' if now != before else f'up to date ({now})'
            _set(name, state='done', pct=100, msg=out[name])
        except Exception as e:
            # Self-update can fail (API rate limit, proxy, file lock). The direct
            # "latest release" download is independent of that, so fall back to it.
            try:
                install(name)
                now = version(name)
                out[name] = f'reinstalled latest ({now})' if now != before else f'up to date ({now})'
            except Exception as e2:
                out[name] = f'update failed: {str(e2)[:200]}'
                _set(name, state='error', pct=0, error=f'{str(e)[:150]} / {str(e2)[:150]}')
    _save_state(last_update=time.time())
    return out


def update_if_due(max_age_hours=24):
    """Called at start-up: keep yt-dlp fresh (YouTube breaks old versions)."""
    last = _load_state().get('last_update', 0)
    if time.time() - last < max_age_hours * 3600:
        return
    threading.Thread(target=update, daemon=True).start()


def status():
    out = {}
    plat = _plat()
    for name, spec in TOOLS.items():
        paths = [find(f) for f in spec['files']]
        ok = all(paths)
        src = _source(paths[0]) if ok else None
        with _lock:
            job = dict(_jobs.get(name, {}))
        out[name] = {
            'label': spec['label'], 'need': spec['need'], 'why': spec['why'], 'size_mb': spec['size_mb'],
            'ok': ok, 'source': src, 'path': paths[0] if ok else None,
            'version': version(name) if ok else None,
            'state': job.get('state'), 'pct': job.get('pct', 0), 'msg': job.get('msg', ''), 'error': job.get('error'),
            'installable': name in SOURCES.get(plat, {}),
            'updatable': name in ('yt-dlp', 'deno') and src == 'app',
        }
    return out


def missing(needs=('required', 'links', 'quality')):
    return [n for n, s in status().items() if not s['ok'] and s['need'] in needs and s['installable']]


# ─────────────────────────────────────────────────────────────────────────────
# App update check (GitHub Releases)
# ─────────────────────────────────────────────────────────────────────────────

_app_update = {'checked': 0, 'result': None}


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '')[:3]) or (0,)


def check_app_update(force=False):
    if 'YOUR_GITHUB_USERNAME' in GITHUB_REPO:
        return {'enabled': False, 'current': APP_VERSION}
    if not force and _app_update['result'] and time.time() - _app_update['checked'] < 6 * 3600:
        return _app_update['result']
    res = {'enabled': True, 'current': APP_VERSION, 'newer': False}
    try:
        rel = _http_json(f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest', timeout=10)
        res.update(latest=rel.get('tag_name', ''), url=rel.get('html_url', ''),
                   setup_url=next((a['browser_download_url'] for a in rel.get('assets', [])
                                   if a.get('name', '').endswith('-Setup.exe')), ''),
                   newer=_vtuple(rel.get('tag_name')) > _vtuple(APP_VERSION))
    except Exception as e:
        res['error'] = str(e)[:200]
    _app_update.update(checked=time.time(), result=res)
    return res


def start_app_update(on_ready):
    """Download the newest BeatSync Setup in the background, then call on_ready(path)."""
    res = check_app_update(force=True)
    if not res.get('newer') or not res.get('setup_url'):
        return {'ok': False, 'error': 'No update available.'}
    with _lock:
        if _jobs.get('app-update', {}).get('state') in ('downloading', 'launching'):
            return {'ok': True}                                 # already running
        _jobs['app-update'] = {'state': 'downloading', 'pct': 0, 'msg': 'Starting download...', 'error': None}

    def work():
        try:
            dest = Path(tempfile.gettempdir()) / 'BeatSync-Setup.exe'
            _download(res['setup_url'], dest, 'app-update', 0, 100)
            _set('app-update', state='launching', pct=100, msg='Installing...')
            on_ready(dest)
        except Exception as e:
            _set('app-update', state='error', msg='', error=str(e)[:200])

    threading.Thread(target=work, daemon=True).start()
    return {'ok': True}


def app_update_status():
    with _lock:
        return dict(_jobs.get('app-update', {}))

def just_updated_from():
    """First launch after an update? Returns the old version (only once), else ''."""
    prev = _load_state().get('last_version', '')
    if prev != APP_VERSION:
        _save_state(last_version=APP_VERSION)
    return prev if prev and prev != APP_VERSION else ''