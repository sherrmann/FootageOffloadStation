"""Run history and device statistics in SQLite (/var/lib/footage-offload/state.db)."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    serial TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    run_folder TEXT,
    files INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    files_left INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT 'running',   -- running | ok | partial | error
    error_id TEXT,
    error_detail TEXT,
    errors TEXT NOT NULL DEFAULT '[]',        -- JSON list of {"file", "id", "detail"}
    nas_state TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    serial TEXT PRIMARY KEY,
    manufacturer TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0
);
"""


class State:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            # A run still marked "running" was interrupted by a crash or power loss.
            self._db.execute(
                "UPDATE runs SET result='error', error_id='error.interrupted', "
                "finished_at=COALESCE(finished_at, started_at) WHERE result='running'")

    def start_run(self, info, profile_id, started_at=None):
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO runs (started_at, serial, manufacturer, model, profile) "
                "VALUES (?, ?, ?, ?, ?)",
                (started_at or time.time(), info.serial, info.manufacturer, info.model, profile_id))
            return cur.lastrowid

    def finish_run(self, run_id, *, run_folder, files, bytes_, files_left, result,
                   error_id=None, error_detail=None, errors=()):
        """Record the end of a run. Runs that found nothing are deleted (#18)."""
        now = time.time()
        with self._lock, self._db:
            if files == 0 and files_left == 0 and result == "ok" and not errors:
                self._db.execute("DELETE FROM runs WHERE id=?", (run_id,))
                return False
            self._db.execute(
                "UPDATE runs SET finished_at=?, run_folder=?, files=?, bytes=?, files_left=?, "
                "result=?, error_id=?, error_detail=?, errors=? WHERE id=?",
                (now, run_folder, files, bytes_, files_left, result, error_id, error_detail,
                 json.dumps(list(errors)), run_id))
            row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row["serial"]:
                self._db.execute(
                    "INSERT INTO devices (serial, manufacturer, model, first_seen, last_seen, runs, files, bytes) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT(serial) DO UPDATE SET last_seen=excluded.last_seen, "
                    "manufacturer=excluded.manufacturer, model=excluded.model, runs=runs+1, "
                    "files=files+excluded.files, bytes=bytes+excluded.bytes",
                    (row["serial"], row["manufacturer"], row["model"], row["started_at"], now, files, bytes_))
            return True

    def set_nas_state(self, run_id, state):
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET nas_state=? WHERE id=?", (state, run_id))

    def runs(self, limit=100, offset=0):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d["errors"] or "[]")
            d["duration_s"] = (d["finished_at"] - d["started_at"]) if d["finished_at"] else None
            out.append(d)
        return out

    def devices(self):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM devices ORDER BY last_seen DESC").fetchall()]

    def close(self):
        with self._lock:
            self._db.close()
