"""In-process bridge to the BeatSync engine (server.py). No web page and no network socket:
requests go through Flask's test client (same validation as before), job progress is read
straight from the job queues, and slow work runs on background threads."""

import threading

from PySide6.QtCore import QObject, Signal, QTimer


class EngineError(Exception):
    pass


class _Relay(QObject):
    """Runs callables on the GUI thread (signals across threads are queued)."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


_relay = None


def init_relay():
    global _relay
    if _relay is None:
        _relay = _Relay()


def on_gui(fn):
    _relay.call.emit(fn)


def run_bg(fn, ok=None, err=None):
    """fn() on a worker thread; ok(result) / err(exception) back on the GUI thread."""
    def work():
        try:
            res = fn()
        except Exception as e:                       # noqa: BLE001 — reported to the UI
            if err:
                on_gui(lambda e=e: err(e))
            return
        if ok:
            on_gui(lambda: ok(res))
    threading.Thread(target=work, daemon=True).start()


class Engine(QObject):
    event = Signal(str, dict)          # job_id, message (progress / info / warn / timing / preview_ready / done / error)

    def __init__(self, server):
        super().__init__()
        self.s = server
        self._job = None
        self._poll_t = QTimer(self)
        self._poll_t.setInterval(60)
        self._poll_t.timeout.connect(self._poll)

    # ── requests ──────────────────────────────────────────────────────────
    def api(self, path, body=None, method=None):
        c = self.s.app.test_client()
        if (method or ('POST' if body is not None else 'GET')) == 'POST':
            r = c.post(path, json=body or {})
        else:
            r = c.get(path)
        data = r.get_json(silent=True)
        r.close()
        if data is None:
            data = {}
        if r.status_code >= 400 or (isinstance(data, dict) and data.get('error')):
            raise EngineError(data.get('error') if isinstance(data, dict) and data.get('error')
                              else f'Engine error (HTTP {r.status_code})')
        return data

    def job(self, jid):
        with self.s.jobs_lock:
            return self.s.jobs.get(jid)

    def preview_path(self, jid):
        j = self.job(jid)
        return j.get('file_preview') if j else None

    def touch(self, jid):
        if jid:
            self.s._touch(jid)

    # ── progress of one job at a time ─────────────────────────────────────
    def listen(self, jid):
        self._job = jid
        self._poll_t.start()

    def stop(self):
        self._job = None
        self._poll_t.stop()

    def _poll(self):
        jid = self._job
        if not jid:
            return
        j = self.job(jid)
        if not j or 'queue' not in j:
            self.stop()
            return
        q = j['queue']
        for _ in range(64):
            if self._job != jid:
                return
            try:
                msg = q.get_nowait()
            except Exception:
                return
            if msg.get('type') in ('done', 'error', 'cancelled'):
                self.stop()
            self.event.emit(jid, msg)
            if self._job != jid:
                return
