"""Keep network notification delivery off the single hardware-reader thread."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable


class SensorNotificationWorker:
    """One worker and one newest pending sample, with no unbounded backlog."""

    def __init__(self, processor: Callable[..., dict[str, Any]], status_supplier: Callable[[], dict[str, Any]]):
        self._processor = processor
        self._status_supplier = status_supplier
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return not self._stop.is_set()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="aquaculture-sensor-notifications", daemon=True)
            self._thread.start()
            return True

    def submit(self, reading: dict[str, Any], status: dict[str, Any], callback: Callable[[dict[str, Any]], None]) -> bool:
        with self._lock:
            if self._stop.is_set() or not self._thread or not self._thread.is_alive():
                return False
            # Intermediate readings are replaceable; old buffered data must never
            # accumulate behind a slow SMTP/SMS request.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            self._queue.put_nowait((dict(reading), dict(status), callback))
            return True

    def stop(self, timeout: float = 1.0) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                reading, event_status, callback = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if self._stop.is_set():
                    continue
                # Check current connectivity and the ORIGINAL sample timestamp
                # immediately before processing; queueing never renews its age.
                status = {**event_status, **self._status_supplier()}
                result = self._processor(reading, creation="SYSTEM", sensor_status=status)
                if not self._stop.is_set():
                    callback(result)
            except Exception:
                logging.getLogger(__name__).error("Sensor notification processing failed; hardware ingestion continues.")
            finally:
                self._queue.task_done()


_worker: SensorNotificationWorker | None = None
_worker_lock = threading.Lock()


def start_reading_worker() -> SensorNotificationWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            from .alert_system import process_sensor_reading
            from sensors import get_status

            _worker = SensorNotificationWorker(process_sensor_reading, get_status)
        _worker.start()
        return _worker


def queue_sensor_reading(reading, status, callback) -> bool:
    worker = _worker
    return bool(worker and worker.submit(reading, status, callback))


def stop_reading_worker(timeout: float = 1.0) -> None:
    worker = _worker
    if worker:
        worker.stop(timeout)
