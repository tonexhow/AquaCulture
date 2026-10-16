"""Durable at-most-once periodic summaries, independent of threshold alerts."""
from __future__ import annotations

import atexit
from contextlib import closing
import logging
import sqlite3
import threading
import time
from pathlib import Path

from . import alert_system as alerts

STATE_PATH = alerts.CONFIG_PATH.parent / "notification_schedule.sqlite3"
_thread = None
_lock = threading.Lock()
_stop = threading.Event()


class SummarySchedule:
    def __init__(self, path: Path = STATE_PATH):
        self.path = Path(path)

    def claim(self, interval: int, now: float | None = None) -> bool:
        """Reserve before delivery, atomically across threads/processes/restarts.

        Crash after reservation can miss a summary; retrying could duplicate email.
        No backlog is sent when the application resumes after downtime.
        """
        if interval <= 0:
            return False
        now = time.time() if now is None else now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS summary_schedule (id INTEGER PRIMARY KEY CHECK(id=1), last_claim REAL NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO summary_schedule VALUES (1, ?)", (now,))
            cursor = conn.execute("UPDATE summary_schedule SET last_claim=? WHERE id=1 AND last_claim <= ?", (now, now - interval))
            return cursor.rowcount == 1


def tick(schedule: SummarySchedule, snapshot, now=None):
    settings = alerts.get_notification_settings()
    interval = settings["Summary"]
    if interval <= 0:
        return False
    # Initialize the durable baseline even with no configured delivery channel.
    if not schedule.claim(interval, now):
        return False
    latest = snapshot()
    reading = latest.get("reading") or {}
    valid = alerts.normalize_reading(reading) if latest.get("ok") and alerts._reading_available(reading, latest.get("status")) else {}
    rows = [(name, f"{valid[name]} {meta['unit']}" if name in valid else "Unavailable (no fresh measurement)") for name, meta in alerts.SENSOR_META.items()]
    subscribers = alerts.get_subscribers_grouped()
    if settings["Email"] and subscribers["EMAIL"]:
        alerts.send_email_alert(subscribers["EMAIL"], "AquaCulture Periodic Summary",
                                alerts.build_email_shell("Water monitoring summary", "Latest available measurements; unavailable sensors are not threshold alerts.", rows, accent="info"))
    if settings["SMS"] and subscribers["SMS"]:
        alerts.send_sms_alert(subscribers["SMS"], "AquaCulture summary: " + "; ".join(f"{k}: {v}" for k, v in rows))
    return True


def start_summary_scheduler():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return _thread
        _stop.clear()

        def run():
            try:
                from sensors import get_latest
            except ImportError:
                from server.sensors import get_latest
            schedule = SummarySchedule()
            while not _stop.is_set():
                try:
                    tick(schedule, lambda: get_latest(allow_stale=False))
                except Exception:
                    logging.getLogger(__name__).error("Periodic summary failed; check local notification configuration.")
                interval = alerts.get_notification_settings()["Summary"]
                _stop.wait(min(30, max(1, interval)) if interval > 0 else 30)

        _thread = threading.Thread(target=run, name="aquaculture-summary", daemon=True)
        _thread.start()
        return _thread


atexit.register(_stop.set)
