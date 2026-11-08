"""
Tempo Lab — BeatSync's second window.

  • Load a song from a link (yt-dlp) or a file
  • Tempo curve: the BPM over time, for songs that speed up or slow down
    (live bands, rubato intros, "Stairway to Heaven" style builds), split into sections
  • Pick the part you want and export it — as it is, or at a new BPM:
      lock   every beat of the part plays at the target BPM (tempo changes flattened)
      scale  the whole part is sped up / slowed down by one factor (tempo changes kept)

server.py hands this module its helpers with attach(app, server_module), so nothing here
imports server.py (running server.py directly would otherwise load it twice).
"""

import os
import re
import math
import time
import uuid
import shutil
import logging
import threading
import subprocess
import collections
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

import tools

bp = Blueprint('tempo_lab', __name__)
log = logging.getLogger('tempo_lab')

SR, HOP = 22050, 256
AUDIO_OUT = {'mp3': ['-c:a', 'libmp3lame', '-q:a', '0'],
             'm4a': ['-c:a', 'aac', '-b:a', '256k'],
             'flac': ['-c:a', 'flac'],
             'wav': ['-c:a', 'pcm_s16le']}
KEEP_SESSIONS = 3

S = None                         # the server module (set by attach)
_sessions = {}
_lock = threading.Lock()


def attach(app, server_module):
    global S
    S = server_module
    app.register_blueprint(bp)


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────

def _new_session():
    sid = uuid.uuid4().hex[:12]
    sess = {'id': sid, 'state': 'loading', 'msg': 'Starting…', 'pct': 0, 'error': None,
            'name': '', 'src': None, 'tmp': S._mkdtemp('tempo_'), 'analysis': None,
            'export': None, 'procs': [], 'created': time.time(), 'gen': 0}
    with _lock:
        _sessions[sid] = sess
        old = sorted(_sessions.values(), key=lambda s: s['created'])[:-KEEP_SESSIONS]
        for o in old:
            _drop(o)
    return sess


def _drop(sess):
    for p in sess.get('procs', []):
        try:
            p.kill()
        except Exception:
            pass
    _sessions.pop(sess['id'], None)
    shutil.rmtree(sess['tmp'], ignore_errors=True)


def _get(sid):
    with _lock:
        sess = _sessions.get(sid or '')
    if not sess:
        raise KeyError('This song is no longer loaded — load it again.')
    return sess


def result_file(sid):
    """(path, suggested name) of the last export — used by the desktop Save dialog."""
    sess = _get(sid)
    ex = sess.get('export') or {}
    if ex.get('state') != 'done' or not os.path.isfile(ex.get('path', '')):
        raise RuntimeError('Nothing exported yet.')
    return ex['path'], ex['name']


def _set(sess, **kw):
    with _lock:
        sess.update(kw)


def _run(sess, cmd, timeout=3600):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sess['procs'].append(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RuntimeError('Timed out.')
    finally:
        if p in sess['procs']:
            sess['procs'].remove(p)
    if p.returncode != 0:
        raise RuntimeError((err or b'').decode('utf-8', 'replace').strip()[-400:] or f'exit code {p.returncode}')
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Loading (link or file)
# ─────────────────────────────────────────────────────────────────────────────

def _download(sess, url):
    cmd = S._ytdlp_cmd()
    if not cmd:
        raise S._ytdlp_missing()
    prog = ['--newline', '--progress-template',
            'download:[bs] %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s']
    args = ['-f', 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best',
            '-x', '--audio-format', 'flac', '--audio-quality', '0',
            '-o', os.path.join(sess['tmp'], '%(title).90B.%(ext)s'), url]
    p = subprocess.Popen(cmd + S._ytdlp_common() + prog + args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    sess['procs'].append(p)
    tail = collections.deque(maxlen=30)
    try:
        for line in p.stdout:
            line = line.rstrip()
            if line.startswith('[bs] '):
                parts = line.split()
                try:
                    done = float(parts[1])
                    total = float(parts[2]) if parts[2] not in ('NA', 'None') else float(parts[3])
                    _set(sess, msg=f'Downloading {int(done / total * 100)}%  ({done / 1e6:.1f} MB)',
                         pct=int(5 + 55 * done / total))
                except (ValueError, IndexError, ZeroDivisionError):
                    pass
            elif line:
                tail.append(line)
                if '[ExtractAudio]' in line:
                    _set(sess, msg='Converting…', pct=62)
        p.wait()
    finally:
        if p in sess['procs']:
            sess['procs'].remove(p)
    if p.returncode != 0:
        raise S._ytdlp_error(list(tail), p.returncode)
    files = [f for f in Path(sess['tmp']).iterdir()
             if f.suffix.lower() in S.ALLOWED_AUDIO and not f.name.endswith(('.part', '.ytdl'))]
    if not files:
        raise RuntimeError('The download finished but no audio file was found.')
    return str(max(files, key=lambda f: f.stat().st_size))


def _load_worker(sess, url, file_id):
    try:
        if url:
            _set(sess, msg='Looking up the link…', pct=3)
            path = _download(sess, url)
        else:
            path = S._upload_entry(file_id)['path']
            if not os.path.isfile(path):
                raise RuntimeError(f'The file is no longer there: {path}')
        _set(sess, src=path, name=Path(path).stem, msg='Reading audio…', pct=65)
        y = _decode_mono(sess, path)
        _set(sess, msg='Finding the tempo curve…', pct=72)
        an = analyze(y)
        an['peaks'] = _peaks(y)
        _set(sess, analysis=an, state='ready', msg='', pct=100)
        log.info('tempo lab: %s  %.1fs  %s', sess['name'], an['duration'], an['summary'])
    except Exception as e:
        log.exception('tempo lab load failed')
        _set(sess, state='error', error=S._friendly(e) if hasattr(S, '_friendly') else str(e))


def _decode_mono(sess, path, sr=SR):
    import numpy as np
    raw = _run(sess, ['ffmpeg', '-v', 'error', '-i', path, '-vn', '-ac', '1', '-ar', str(sr), '-f', 'f32le', '-'])
    if not raw:
        raise RuntimeError('No audio in that file.')
    return np.frombuffer(raw, np.float32).copy()


def _peaks(y, n=1600):
    import numpy as np
    if len(y) < n:
        return [0.0] * n
    k = len(y) // n
    p = np.abs(y[:k * n]).reshape(n, k).max(axis=1)
    p = p / (p.max() + 1e-9)
    return [round(float(v), 3) for v in p]


# ─────────────────────────────────────────────────────────────────────────────
# Tempo curve
# ─────────────────────────────────────────────────────────────────────────────

def _medfilt(x, k):
    import numpy as np
    k = max(1, int(k) | 1)
    if k == 1 or len(x) < 3:
        return x.copy()
    pad = np.pad(x, k // 2, mode='edge')
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)


def _smooth(x, k):
    import numpy as np
    k = max(1, int(k))
    if k == 1 or len(x) < 2:
        return x.copy()
    pad = np.pad(x, (k // 2, k - 1 - k // 2), mode='edge')
    return np.convolve(pad, np.ones(k) / k, mode='valid')


def analyze(y, sr=SR):
    """Beat-level tempo curve. Returns a JSON-able dict:
       times/bpm (one point per beat), sections, steady flag, a single summary BPM."""
    import numpy as np
    import librosa

    dur = len(y) / sr
    if dur < 4:
        raise RuntimeError('The audio is too short to find a tempo.')
    fps = sr / HOP
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)

    # 1. local tempo per frame (10 s window), all in the octave of the overall tempo
    g = float(np.atleast_1d(librosa.feature.tempo(onset_envelope=oenv, sr=sr, hop_length=HOP))[0])
    loc = np.asarray(librosa.feature.tempo(onset_envelope=oenv, sr=sr, hop_length=HOP, aggregate=None,
                                           ac_size=10.0, start_bpm=g, std_bpm=0.5), float).reshape(-1)
    for _ in range(4):
        loc = np.where(loc > g * 1.414, loc / 2, loc)
        loc = np.where(loc < g / 1.414, loc * 2, loc)
    loc = _smooth(_medfilt(loc, fps * 6), fps * 2)
    e = int(fps * 6)
    if len(loc) > 3 * e:                                   # the tempogram is unreliable at the edges
        loc[:e] = loc[e]
        loc[-e:] = loc[-e]

    # 2. beats that follow the changing tempo
    _, beats = librosa.beat.beat_track(onset_envelope=oenv, sr=sr, hop_length=HOP, bpm=loc,
                                       tightness=300, trim=False, units='time')
    beats = np.asarray(beats, float)

    # 3. ignore silence and the beat-less fade at the start / end
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    rdb = 20 * np.log10(np.maximum(_smooth(rms, fps), 1e-6))
    loud = rdb > rdb.max() - 35
    active = np.flatnonzero(loud) / fps
    if len(active):
        beats = beats[(beats >= active[0]) & (beats <= active[-1])]
    tg = librosa.feature.tempogram(onset_envelope=oenv, sr=sr, hop_length=HOP, win_length=384)
    axis = librosa.tempo_frequencies(tg.shape[0], sr=sr, hop_length=HOP)
    clar = _smooth(tg[(axis > 40) & (axis < 240)].max(axis=0), fps * 4)
    n = min(len(clar), len(loud))
    ok = (clar[:n] > 0.82 * np.median(clar[:n][loud[:n]])) & loud[:n]
    # the rhythmic body = from the first to the last stretch of >= 4 s with a clear pulse
    # (a short blip inside a fade-out does not count)
    edges = np.flatnonzero(np.diff(np.concatenate([[0], ok.astype(np.int8), [0]])))
    runs = [(s0, s1) for s0, s1 in zip(edges[::2], edges[1::2]) if s1 - s0 >= 4 * fps]
    if runs and len(beats):
        keep = (beats >= runs[0][0] / fps - 1) & (beats <= runs[-1][1] / fps + 1)
        if keep.sum() >= 8:
            beats = beats[keep]
    if len(beats) < 8:
        raise RuntimeError('Could not find a steady beat in this audio.')

    # 4. repair missed / doubled beats against the local tempo
    lt = np.arange(len(loc)) / fps
    clean = [beats[0]]
    for bt in beats[1:]:
        per = 60.0 / np.interp(bt, lt, loc)
        gap = bt - clean[-1]
        if gap < 0.7 * per:
            continue
        k = int(round(gap / per))
        if 2 <= k <= 4:
            clean += list(clean[-1] + gap * np.arange(1, k) / k)
        clean.append(bt)
    beats = np.asarray(clean)

    # 5. BPM at each beat = slope of a line through 9 neighbouring beats (cancels frame rounding)
    gaps = np.diff(beats)
    run = np.concatenate([[0], np.cumsum(gaps > 2.5 * np.median(gaps))])
    tb, bb = [], []
    for i in range(len(beats)):
        idx = np.arange(max(0, i - 4), min(len(beats), i + 5))
        idx = idx[run[idx] == run[i]]
        if len(idx) < 5:
            continue
        slope = np.polyfit(idx, beats[idx], 1)[0]
        tb.append(beats[i])
        bb.append(60.0 / slope)
    if len(tb) < 8:
        raise RuntimeError('Could not find a steady beat in this audio.')
    tb, bb = np.asarray(tb), np.asarray(bb)
    # the tracker needs a few beats to settle: drop first / last points that disagree with what follows
    if len(bb) > 40:
        head, tail = np.median(bb[8:24]), np.median(bb[-24:-8])
        keep = np.ones(len(bb), bool)
        keep[:8] = np.abs(bb[:8] / head - 1) < 0.06
        keep[-8:] = np.abs(bb[-8:] / tail - 1) < 0.06
        tb, bb = tb[keep], bb[keep]
    bb = _smooth(_medfilt(bb, 7), 3)

    lo, hi = np.percentile(bb, [10, 90])
    steady = hi / lo < 1.04
    if steady:
        k = np.arange(len(beats))
        summary = 60.0 / np.polyfit(k, beats, 1)[0]
    else:
        w = np.diff(tb, append=tb[-1])
        summary = float(np.average(bb, weights=np.maximum(w, 1e-3)))
    r_lo, r_hi = np.percentile(bb, [3, 97])
    return {'duration': round(dur, 3), 'times': [round(float(t), 3) for t in tb],
            'bpm': [round(float(b), 3) for b in bb], 'beats': [round(float(t), 3) for t in beats],
            'sections': _sections(tb, bb, dur), 'steady': bool(steady),
            'summary': round(float(summary), 2), 'range': [round(float(r_lo), 1), round(float(r_hi), 1)]}


def _sections(t, b, dur):
    import numpy as np
    n = len(b)
    segs, start = [], 0
    for i in range(1, n + 1):
        if i == n or abs(b[i] / np.median(b[start:i]) - 1) > 0.035:
            segs.append([start, i])
            start = i
    changed = True
    while changed and len(segs) > 1:                       # fold tiny pieces into the closer neighbour
        changed = False
        for k, (s, e) in enumerate(segs):
            if e - s < 12:
                m = np.median(b[s:e])
                cands = []
                if k > 0:
                    cands.append((abs(np.median(b[segs[k - 1][0]:segs[k - 1][1]]) - m), k - 1))
                if k < len(segs) - 1:
                    cands.append((abs(np.median(b[segs[k + 1][0]:segs[k + 1][1]]) - m), k + 1))
                j = min(cands)[1]
                a, z = min(k, j), max(k, j)
                segs[a] = [segs[a][0], segs[z][1]]
                del segs[z]
                changed = True
                break
    out = [segs[0]]
    for s, e in segs[1:]:
        ps, pe = out[-1]
        if abs(np.median(b[s:e]) / np.median(b[ps:pe]) - 1) < 0.025:
            out[-1] = [ps, e]
        else:
            out.append([s, e])
    res = []
    for k, (s, e) in enumerate(out):
        seg = b[s:e]
        slope = seg[-1] - seg[0] if len(seg) > 1 else 0
        res.append({'t0': 0.0 if k == 0 else round(float(t[s]), 3),
                    't1': round(float(dur if k == len(out) - 1 else t[out[k + 1][0]]), 3),
                    'bpm': round(float(np.median(seg)), 2),
                    'lo': round(float(seg.min()), 1), 'hi': round(float(seg.max()), 1),
                    'trend': 'up' if slope > 2 else ('down' if slope < -2 else '')})
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Export: cut a part, optionally change its BPM
# ─────────────────────────────────────────────────────────────────────────────

_filter_cache = {}


def _has_filter(name):
    if name not in _filter_cache:
        try:
            out = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True,
                                 timeout=20).stdout
            _filter_cache[name] = f' {name} ' in out
        except Exception:
            _filter_cache[name] = False
    return _filter_cache[name]


def _rubberband_cli():
    exe = tools.find('rubberband')
    if not exe:
        return None, False
    v = tools.version('rubberband') or ''
    m = re.match(r'(\d+)', v)
    return exe, bool(m and int(m.group(1)) >= 3)


def _plan(an, a, b, target, mode, scale):
    """Piecewise-constant speed over the part: [(t_rel, ratio), ...] (ratio > 1 = faster)."""
    import numpy as np
    t = np.asarray(an['times'])
    bpm = np.asarray(an['bpm']) * scale
    if mode == 'scale':
        grid = np.linspace(a, b, 400)
        ref = float(np.mean(np.interp(grid, t, bpm)))
        return [(0.0, target / ref)], ref
    inside = (t > a) & (t < b)
    pts = [(0.0, target / float(np.interp(a, t, bpm)))]
    for ti, bi in zip(t[inside], bpm[inside]):
        r = target / bi
        if abs(r / pts[-1][1] - 1) > 0.0015:
            pts.append((float(ti - a), float(r)))
    grid = np.linspace(a, b, 400)
    return pts, float(np.mean(np.interp(grid, t, bpm)))


def _out_time(plan, t):
    """Output time for input time t (piecewise integration of 1/ratio)."""
    out = 0.0
    for i, (t0, r) in enumerate(plan):
        t1 = plan[i + 1][0] if i + 1 < len(plan) else float('inf')
        if t <= t0:
            break
        out += (min(t, t1) - t0) / r
    return out


def _source_rate(sess, path):
    try:
        out = _run(sess, ['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                          'stream=sample_rate', '-of', 'csv=p=0', path]).decode().strip()
        sr = int(out.split()[0])
        return sr if 22050 <= sr <= 96000 else 44100
    except Exception:
        return 44100


def _varispeed(wav_in, wav_out, plan, dur, sr):
    """Pitch follows speed (vinyl / nightcore style) for a changing speed: resample along the map."""
    import numpy as np
    import soundfile as sf
    y, sr = sf.read(wav_in, dtype='float32', always_2d=True)
    total = _out_time(plan, dur)
    n_out = int(round(total * sr))
    # inverse map: output time -> input time (sample the forward map densely, then invert)
    ts = np.linspace(0, dur, 4000)
    outs = np.array([_out_time(plan, x) for x in ts])
    src = np.interp(np.arange(n_out) / sr, outs, ts) * sr
    pos = np.arange(len(y))
    res = np.stack([np.interp(src, pos, y[:, c]) for c in range(y.shape[1])], axis=1)
    sf.write(wav_out, res.astype(np.float32), sr, subtype='FLOAT')


def _export_worker(sess, opts, gen):
    ex = sess['export']
    try:
        an = sess['analysis']
        dur_all = an['duration']
        a = max(0.0, float(opts.get('start') or 0))
        b = min(dur_all, float(opts.get('end') or dur_all))
        if b - a < 0.5:
            raise RuntimeError('The selected part is too short.')
        part = b - a
        change = bool(opts.get('change_bpm'))
        target = float(opts.get('target') or 0)
        mode = 'scale' if opts.get('mode') == 'scale' else 'lock'
        keep_pitch = opts.get('pitch', 'keep') != 'follow'
        scale = float(opts.get('scale') or 1.0)
        fmt = opts.get('format') if opts.get('format') in AUDIO_OUT else 'mp3'
        if change and not 20 <= target <= 400:
            raise RuntimeError('Target BPM must be between 20 and 400.')

        whole = a < 0.05 and b > dur_all - 0.05
        stem = sess['name'][:80] or 'song'
        tag = '' if whole else f' [{_ts(a)}-{_ts(b)}]'
        name = f'{stem}{tag}' + (f' {target:g}bpm' if change else '') + f'.{fmt}'
        out = os.path.join(sess['tmp'], f'export_{gen}.{fmt}')
        cut = ['-ss', f'{a:.3f}', '-t', f'{part:.3f}', '-i', sess['src'], '-vn']
        fade = 'afade=t=in:d=0.006,afade=t=out:st={st:.3f}:d=0.012'
        sr = _source_rate(sess, sess['src'])

        if not change:
            ex.update(msg='Cutting…', pct=30)
            _run(sess, ['ffmpeg', '-v', 'error', '-y'] + cut +
                 ['-af', fade.format(st=max(0, part - 0.012))] + AUDIO_OUT[fmt] + [out])
            engine, ref = 'cut', None
        else:
            plan, ref = _plan(an, a, b, target, mode, scale)
            r_lo, r_hi = min(r for _, r in plan), max(r for _, r in plan)
            if r_lo < 0.25 or r_hi > 4:
                raise RuntimeError(f'That is a {r_hi:.2f}× speed change — too far. '
                                   'Check the song BPM (÷2 / ×2) or choose another target.')
            out_dur = _out_time(plan, part)
            wav = os.path.join(sess['tmp'], f'part_{gen}.wav')
            st_wav = os.path.join(sess['tmp'], f'stretched_{gen}.wav')
            ex.update(msg='Cutting…', pct=10)
            _run(sess, ['ffmpeg', '-v', 'error', '-y'] + cut + ['-ac', '2', '-ar', str(sr), '-c:a', 'pcm_f32le', wav])
            rb, fine = _rubberband_cli()
            ex.update(msg='Changing the tempo…', pct=35)
            if not keep_pitch:
                if len(plan) == 1:
                    r = plan[0][1]
                    _run(sess, ['ffmpeg', '-v', 'error', '-y', '-i', wav, '-af',
                                f'asetrate={sr * r:.3f},aresample={sr}', '-c:a', 'pcm_f32le', st_wav])
                else:
                    _varispeed(wav, st_wav, plan, part, sr)
                engine = 'varispeed'
            elif rb:
                cmd = [rb, '-q']
                if fine:
                    cmd.append('--fine')
                cmd += ['-t', f'{out_dur / part:.8f}']
                if len(plan) > 1:
                    mp = os.path.join(sess['tmp'], f'map_{gen}.txt')
                    n_in = int(round(part * sr))
                    with open(mp, 'w') as f:
                        for t0, _ in plan:
                            f.write(f'{int(round(t0 * sr))} {int(round(_out_time(plan, t0) * sr))}\n')
                        f.write(f'{n_in} {int(round(out_dur * sr))}\n')
                    cmd += ['--timemap', mp]
                try:
                    _run(sess, cmd + [wav, st_wav])
                except RuntimeError:
                    if not fine:
                        raise
                    cmd.remove('--fine')                   # older engine if R3 refuses the map
                    _run(sess, cmd + [wav, st_wav])
                engine = 'rubberband'
            else:
                use_rb = _has_filter('rubberband')
                if not use_rb and (r_lo < 0.5 or r_hi > 2.0) and len(plan) > 1:
                    raise RuntimeError('This speed change needs Rubber Band — install it in Settings → Tools.')
                if use_rb:
                    base = f'rubberband=tempo={plan[0][1]:.6f}:transients=crisp:detector=compound'
                    cmds = ';'.join(f'{t0:.3f} rubberband tempo {r:.6f}' for t0, r in plan[1:])
                else:
                    base = S._build_atempo(plan[0][1]) if len(plan) == 1 else f'atempo={plan[0][1]:.6f}'
                    cmds = ';'.join(f'{t0:.3f} atempo tempo {r:.6f}' for t0, r in plan[1:])
                af = (f"asendcmd=c='{cmds}'," if cmds else '') + base
                _run(sess, ['ffmpeg', '-v', 'error', '-y', '-i', wav, '-af', af, '-c:a', 'pcm_f32le', st_wav])
                engine = 'ffmpeg-rubberband' if use_rb else 'atempo'
            ex.update(msg='Saving…', pct=75)
            real = _wav_seconds(st_wav) or out_dur
            _run(sess, ['ffmpeg', '-v', 'error', '-y', '-i', st_wav, '-af', fade.format(st=max(0, real - 0.012))]
                 + AUDIO_OUT[fmt] + [out])
            for f in (wav, st_wav):
                try:
                    os.remove(f)
                except OSError:
                    pass

        measured = None
        if change:
            ex.update(msg='Checking the new BPM…', pct=88)
            try:
                m = analyze(_decode_mono(sess, out))
                f = 2.0 ** round(math.log2(target / m['summary'])) if m['summary'] else 1.0   # same octave as asked
                measured = {'summary': round(m['summary'] * f, 2), 'steady': m['steady'],
                            'range': [round(x * f, 1) for x in m['range']]}
            except Exception:
                log.info('tempo lab: could not measure the result')
        if gen == sess['gen']:
            ex.update(state='done', msg='', pct=100, path=out, name=name, engine=engine, measured=measured,
                      ref_bpm=round(ref, 2) if ref else None, seconds=round(_wav_seconds(out) or 0, 2), gen=gen)
        log.info('tempo lab export: %s (%s)', name, engine)
    except Exception as e:
        log.exception('tempo lab export failed')
        if gen == sess['gen']:
            ex.update(state='error', error=str(e)[:400], msg='')


def _wav_seconds(path):
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return None


def _ts(t):
    m, s = divmod(int(round(t)), 60)
    return f'{m}m{s:02d}s'


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@bp.route('/tempo')
def page():
    html = (tools.resource_dir() / 'templates' / 'tempo.html').read_text(encoding='utf-8')
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/api/tempo/load', methods=['POST'])
def api_load():
    d = request.json or {}
    url, file_id = (d.get('url') or '').strip(), (d.get('file_id') or '').strip()
    if not url and not file_id:
        return jsonify({'error': 'Paste a link or choose a file.'}), 400
    with _lock:
        for s in _sessions.values():                     # a new song stops downloads of older ones
            if s['state'] == 'loading':
                for p in s['procs']:
                    try:
                        p.kill()
                    except Exception:
                        pass
    sess = _new_session()
    threading.Thread(target=_load_worker, args=(sess, url, file_id), daemon=True).start()
    return jsonify({'ok': True, 'sid': sess['id']})


@bp.route('/api/tempo/status/<sid>')
def api_status(sid):
    try:
        sess = _get(sid)
    except KeyError as e:
        return jsonify({'error': str(e)}), 404
    with _lock:
        out = {k: sess[k] for k in ('id', 'state', 'msg', 'pct', 'error', 'name')}
        if request.args.get('full') == '1' and sess['analysis']:
            out['analysis'] = sess['analysis']
        ex = sess.get('export')
        if ex:
            out['export'] = {k: v for k, v in ex.items() if k != 'path'}
    return jsonify(out)


@bp.route('/api/tempo/export', methods=['POST'])
def api_export():
    d = request.json or {}
    try:
        sess = _get(d.get('sid'))
    except KeyError as e:
        return jsonify({'error': str(e)}), 404
    if sess['state'] != 'ready':
        return jsonify({'error': 'The song is not ready yet.'}), 409
    with _lock:
        sess['gen'] += 1
        gen = sess['gen']
        sess['export'] = {'state': 'running', 'msg': 'Starting…', 'pct': 2, 'error': None, 'gen': gen}
    threading.Thread(target=_export_worker, args=(sess, d, gen), daemon=True).start()
    return jsonify({'ok': True, 'gen': gen})


@bp.route('/api/tempo/media/<sid>/<which>')
def api_media(sid, which):
    try:
        sess = _get(sid)
        path = sess['src'] if which == 'source' else result_file(sid)[0]
    except Exception as e:
        return jsonify({'error': str(e)}), 404
    return send_file(path, conditional=True, max_age=0)


@bp.route('/api/tempo/download/<sid>')
def api_download(sid):
    try:
        path, name = result_file(sid)
    except Exception as e:
        return jsonify({'error': str(e)}), 404
    return send_file(path, as_attachment=True, download_name=name)
