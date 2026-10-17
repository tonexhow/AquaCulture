"""Bounded, private SQLite connection observations, never credentials."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConnectionHistory:
    def __init__(self, path, limit=100):
        self.path = Path(path)
        self.limit = max(1, min(1000, limit))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY, ssid TEXT, attempted_at TEXT,
                connected_at TEXT, disconnected_at TEXT, status TEXT NOT NULL,
                error_category TEXT, manual INTEGER NOT NULL)""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, ssid, status, *, manual=False, error_category=None):
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute("INSERT INTO connections (ssid,attempted_at,connected_at,status,error_category,manual) VALUES (?,?,?,?,?,?)",
                (ssid, now if manual else None, now if status.startswith("CONNECTED") else None, status, error_category, int(manual)))
            conn.execute("DELETE FROM connections WHERE id NOT IN (SELECT id FROM connections ORDER BY id DESC LIMIT ?)", (self.limit,))
            return cursor.lastrowid

    def finish(self, record_id, status, error_category=None):
        with self.connect() as conn:
            conn.execute("UPDATE connections SET status=?,error_category=?,connected_at=? WHERE id=?",
                         (status, error_category, utcnow() if status.startswith("CONNECTED") else None, record_id))

    def disconnect(self, ssid):
        with self.connect() as conn:
            conn.execute("UPDATE connections SET disconnected_at=? WHERE ssid=? AND connected_at IS NOT NULL AND disconnected_at IS NULL", (utcnow(), ssid))

    def recent(self, limit=30):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return [{**dict(row), "manual": bool(row["manual"])} for row in conn.execute("SELECT * FROM connections ORDER BY id DESC LIMIT ?", (min(limit, self.limit),))]

    def metadata(self):
        rows = self.recent(self.limit)
        success = next((r for r in rows if r["connected_at"]), None)
        failure = next((r for r in rows if r["status"] in {"FAILED", "TIMED_OUT"}), None)
        return {"last_connected_ssid": success["ssid"] if success else None,
                "last_successful_connection": success, "last_failed_connection": failure,
                "recent_error": failure["error_category"] if failure else None}
