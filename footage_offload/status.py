"""Live status shared by the transfer engine, the web UI (SSE) and MQTT.

The engine calls `update()`; subscribers get the full status dict on every
change. Progress updates are throttled so a fast transfer does not flood a
phone browser or the MQTT broker.
"""
import copy
import queue
import threading
import time

IDLE = "waiting"
TRANSFERRING = "transferring"
DONE = "done"
ERROR = "error"


def initial_status():
    return {
        "state": IDLE,              # waiting | transferring | done | error
        "device": None,             # {"model", "serial", "manufacturer", "profile"}
        "run_folder": None,
        "current_file": None,
        "files_total": 0,
        "files_done": 0,
        "bytes_total": 0,
        "bytes_done": 0,
        "speed_bps": 0.0,
        "eta_s": None,
        "started_at": None,
        "finished_at": None,
        "error": None,              # {"id": message id, "detail": str}
        "warnings": [],             # [{"id": message id, "params": {...}}]
        "stick": None,              # filled by the stick watcher
        "clock": None,              # {"ntp_synchronised": bool}
        "throttled": None,          # {"undervoltage_now": bool, "undervoltage_occurred": bool}
        "nas": None,                # {"state": "running|done|error", "detail": str, "run": folder}
        "updated_at": time.time(),
    }


class StatusBus:
    def __init__(self, min_interval=0.5):
        self._lock = threading.Lock()
        self._status = initial_status()
        self._subscribers = []
        self._min_interval = min_interval
        self._last_push = 0.0
        self._pending = False
        self._timer = None

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._status)

    def update(self, throttle=False, **fields):
        with self._lock:
            self._status.update(fields)
            self._status["updated_at"] = time.time()
            now = time.monotonic()
            if throttle and now - self._last_push < self._min_interval:
                if not self._pending:
                    self._pending = True
                    delay = self._min_interval - (now - self._last_push)
                    self._timer = threading.Timer(delay, self._flush)
                    self._timer.daemon = True
                    self._timer.start()
                return
            self._last_push = now
            snap = copy.deepcopy(self._status)
            subs = list(self._subscribers)
        self._publish(subs, snap)

    def add_warning(self, message_id, **params):
        with self._lock:
            warnings = [w for w in self._status["warnings"] if w["id"] != message_id]
            warnings.append({"id": message_id, "params": params})
        self.update(warnings=warnings)

    def clear_warning(self, message_id):
        with self._lock:
            warnings = [w for w in self._status["warnings"] if w["id"] != message_id]
            changed = len(warnings) != len(self._status["warnings"])
        if changed:
            self.update(warnings=warnings)

    def _flush(self):
        with self._lock:
            self._pending = False
            self._last_push = time.monotonic()
            snap = copy.deepcopy(self._status)
            subs = list(self._subscribers)
        self._publish(subs, snap)

    @staticmethod
    def _publish(subs, snap):
        for cb in subs:
            try:
                cb(snap)
            except Exception:  # noqa: BLE001 - one bad subscriber must not stop others
                pass

    def subscribe(self, callback):
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def queue_subscription(self, maxsize=20):
        """A Queue that receives status snapshots; for SSE handlers.
        Returns (queue, unsubscribe_function)."""
        q = queue.Queue(maxsize=maxsize)

        def push(snap):
            try:
                q.put_nowait(snap)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    pass

        self.subscribe(push)
        return q, (lambda: self.unsubscribe(push))
