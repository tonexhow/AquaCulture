"""
AquaCulture - sensors.py

Purpose:
- Single source of truth for Arduino Nano USB-serial sensor readings.
- Keeps one backend-owned live sensor cache for all API and Socket.IO clients.
- Gives scanner/fish_recommendation.py and scanner/size_detection.py the same
  simple functions they need: read_water_once, read_water_for_duration,
  read_ultrasonic_distance, and sensor_manager.

Arduino Nano code stays unchanged. The Raspberry Pi only reads JSON lines from USB.
Expected Nano JSON example:
{"device":"aqua_sensor_bridge","temperature":28.50,"ph":7.02,"turbidity":16.00,"oxygen":null,"ammonia":null,"distance_cm":30.00}
"""

from __future__ import annotations

import glob
import json
import math
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional


SensorEventCallback = Callable[[Dict[str, Any]], None]

WATER_KEYS = ("temperature", "ph", "turbidity", "oxygen", "ammonia")
WATER_ALIAS = {
    "temperature": ("temperature", "temperature_c", "temp", "temp_c"),
    "ph": ("ph", "pH"),
    "turbidity": ("turbidity", "turbidity_ntu", "ntu"),
    "oxygen": ("oxygen", "oxygen_mg_l", "dissolved_oxygen", "do"),
    "ammonia": ("ammonia_gas_ppm", "ammonia", "ammonia_ppm", "nh3"),
}
DISTANCE_ALIAS = ("distance_cm", "distance", "ultrasonic", "ultrasonic_cm")
WATER_LIMITS = {
    "temperature": (-5.0, 60.0),
    "ph": (0.0, 14.0),
    "turbidity": (0.0, 5000.0),
    "oxygen": (0.0, 25.0),
    "ammonia": (0.0, 1000.0),
}
RAW_KEYS = (
    "temperature_raw",
    "ph_raw",
    "turbidity_raw",
    "oxygen_raw",
    "ammonia_raw",
    "temperature_voltage",
    "ph_voltage",
    "turbidity_voltage",
    "oxygen_voltage",
    "ammonia_voltage",
    "temperature_digital",
    "ph_digital",
    "turbidity_digital",
    "oxygen_digital",
    "ammonia_digital",
)


@dataclass
class SerialConfig:
    port: str = ""
    baudrate: int = 115200
    timeout_seconds: float = 1.0
    reconnect_delay_seconds: float = 2.0
    stale_after_seconds: float = 6.0
    history_limit: int = 120


@dataclass
class SensorState:
    running: bool = False
    connected: bool = False
    port: Optional[str] = None
    message: str = "Sensor reader is stopped."
    last_error: Optional[str] = None
    last_raw_line: Optional[str] = None
    last_reading: Optional[Dict[str, Any]] = None
    last_read_at: Optional[str] = None
    started_at: Optional[str] = None
    total_lines: int = 0
    valid_lines: int = 0
    invalid_lines: int = 0
    reconnects: int = 0

    def to_dict(self) -> Dict[str, Any]:
        age_seconds: Optional[float] = None
        if self.last_reading and self.last_reading.get("received_epoch"):
            try:
                age_seconds = round(time.time() - float(self.last_reading["received_epoch"]), 2)
            except Exception:
                age_seconds = None
        return {
            "running": self.running,
            "connected": self.connected,
            "port": self.port,
            "message": self.message,
            "last_error": self.last_error,
            "last_read_at": self.last_read_at,
            "started_at": self.started_at,
            "age_seconds": age_seconds,
            "total_lines": self.total_lines,
            "valid_lines": self.valid_lines,
            "invalid_lines": self.invalid_lines,
            "reconnects": self.reconnects,
        }


class SensorManager:
    """Owns the serial connection and shared latest-reading cache."""

    def __init__(self, config: Optional[SerialConfig] = None) -> None:
        self.config = config or SerialConfig(
            port=os.environ.get("AQUACULTURE_SERIAL_PORT", "").strip(),
            baudrate=_safe_int(os.environ.get("AQUACULTURE_SERIAL_BAUDRATE"), 115200),
        )
        self.state = SensorState()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._reading_ready = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._serial: Any = None
        self._callback: Optional[SensorEventCallback] = None
        self._history: Deque[Dict[str, Any]] = deque(maxlen=self.config.history_limit)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def configure_serial(
        self,
        port: str = "",
        baudrate: int = 115200,
        timeout_seconds: float = 1.0,
        reconnect_delay_seconds: float = 2.0,
        stale_after_seconds: float = 6.0,
    ) -> Dict[str, Any]:
        with self._lifecycle_lock:
            next_port = str(port or "").strip()
            next_baudrate = max(1, _safe_int(baudrate, 115200))
            next_timeout = max(0.1, _safe_float(timeout_seconds) or 1.0)
            reconnect = (next_port, next_baudrate, next_timeout) != (
                self.config.port, self.config.baudrate, self.config.timeout_seconds,
            )
            was_running = bool(self._thread and self._thread.is_alive() and self.state.running)
            if reconnect and was_running:
                stopped = self.stop_background_reader()
                if stopped.get("reader_alive"):
                    return {**stopped, "ok": False, "message": "Sensor reader is still stopping; retry configuration shortly."}
            with self._lock:
                self.config.port = next_port
                self.config.baudrate = next_baudrate
                self.config.timeout_seconds = next_timeout
                self.config.reconnect_delay_seconds = max(0.5, _safe_float(reconnect_delay_seconds) or 2.0)
                self.config.stale_after_seconds = max(2.0, _safe_float(stale_after_seconds) or 6.0)
                self.state.message = "Serial configuration updated."
            if reconnect and was_running:
                return self.start_background_reader()
        return self.get_status()

    def start_background_reader(self, event_callback: Optional[SensorEventCallback] = None) -> Dict[str, Any]:
        with self._lifecycle_lock:
            with self._lock:
                if event_callback is not None:
                    self._callback = event_callback
                if self._thread and self._thread.is_alive():
                    if self._stop_event.is_set():
                        return {**self.get_status(), "ok": False, "message": "Sensor reader is still stopping; retry shortly."}
                    self.state.running = True
                    self.state.message = "Sensor reader is already running."
                    return self.get_status()
                self._stop_event.clear()
                self.state.running = True
                self.state.connected = False
                self.state.message = "Sensor reader is starting."
                self.state.started_at = _now_iso()
                self._thread = threading.Thread(target=self._reader_loop, name="aquaculture-sensor-reader", daemon=True)
                self._thread.start()
        return self.get_status()

    def stop_background_reader(self) -> Dict[str, Any]:
        with self._lifecycle_lock:
            self._stop_event.set()
            with self._reading_ready:
                thread = self._thread
                self.state.running = False
                self.state.connected = False
                self.state.message = "Sensor reader is stopping."
                self._reading_ready.notify_all()
            # Closing interrupts a blocking pyserial readline before the join.
            self._close_serial()
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2.0)
            with self._lock:
                if not thread or not thread.is_alive():
                    self._thread = None
                    self.state.message = "Sensor reader is stopped."
        return self.get_status()

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            port = self._find_serial_port()
            if not port:
                self._set_state(connected=False, port=None, message="No Arduino Nano serial port found.")
                self._emit({"type": "sensor_status", "status": self.get_status()})
                self._stop_event.wait(self.config.reconnect_delay_seconds)
                continue

            try:
                serial_conn = self._open_serial(port)
                if self._stop_event.is_set():
                    self._close_serial()
                    break
                self._set_state(connected=True, port=port, message=f"Reading Arduino Nano on {port}.", last_error=None)
                self._emit({"type": "sensor_status", "status": self.get_status()})

                while not self._stop_event.is_set():
                    raw_line = serial_conn.readline()
                    if not raw_line:
                        continue
                    if isinstance(raw_line, bytes):
                        text = raw_line.decode("utf-8", errors="replace").strip()
                    else:
                        text = str(raw_line).strip()
                    if not text:
                        continue
                    self._handle_serial_line(text)
            except Exception as error:
                self._close_serial()
                if self._stop_event.is_set():
                    break
                self._set_state(
                    connected=False,
                    message="Sensor serial connection lost. Reconnecting.",
                    last_error=_safe_error(error),
                )
                with self._lock:
                    self.state.reconnects += 1
                self._emit({"type": "sensor_status", "status": self.get_status()})
                self._stop_event.wait(self.config.reconnect_delay_seconds)

        self._close_serial()
        self._set_state(running=False, connected=False, message="Sensor reader is stopped.")
        self._emit({"type": "sensor_status", "status": self.get_status()})

    def _handle_serial_line(self, text: str) -> None:
        with self._lock:
            self.state.total_lines += 1
            self.state.last_raw_line = text

        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("Serial JSON line is not an object.")
            if not any(alias in payload for aliases in (*WATER_ALIAS.values(), DISTANCE_ALIAS) for alias in aliases):
                raise ValueError("Serial JSON line contains no sensor readings.")
            reading = normalize_sensor_payload(payload)
        except Exception as error:
            with self._lock:
                self.state.invalid_lines += 1
                self.state.last_error = f"Invalid serial line: {_safe_error(error)}"
            return

        with self._lock:
            self.state.valid_lines += 1
            self.state.last_reading = reading
            self.state.last_read_at = reading["timestamp"]
            self.state.last_error = None if reading["ok"] else "No usable sensor values in the latest reading."
            self._history.append(reading)
            self._reading_ready.notify_all()

        self._emit({"type": "sensor_reading", "reading": reading, "status": self.get_status()})

    # ------------------------------------------------------------------
    # Public reads used by API and scanner modules
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            status = self.state.to_dict()
            latest = dict(self.state.last_reading) if self.state.last_reading else None
        status["latest"] = latest
        status["fresh"] = bool(status["running"] and status["connected"] and latest and latest.get("ok") and not self._is_stale(latest))
        status["reader_alive"] = bool(self._thread and self._thread.is_alive())
        status["configured_port"] = self.config.port or "auto"
        status["baudrate"] = self.config.baudrate
        status["stale_after_seconds"] = self.config.stale_after_seconds
        return status

    def get_latest(self, allow_stale: bool = True) -> Dict[str, Any]:
        with self._lock:
            reading = dict(self.state.last_reading) if self.state.last_reading else None
        if not reading:
            return {"ok": False, "message": "No sensor reading is available yet.", "reading": None, "status": self.get_status()}
        if not reading.get("ok"):
            return {"ok": False, "message": "Latest sensor reading has no usable values.", "reading": reading, "status": self.get_status()}
        status = self.get_status()
        stale = self._is_stale(reading) or not status["connected"] or not status["running"]
        if stale and not allow_stale:
            return {"ok": False, "message": "Latest sensor reading is stale or disconnected.", "reading": reading, "stale": True, "status": status}
        return {"ok": True, "message": "Latest sensor reading returned.", "reading": reading, "stale": stale, "status": status}

    def read_water_once(self) -> Dict[str, Any]:
        """Wait for the reader's fresh cache; only the reader owns serial I/O."""
        if not self.state.running:
            self.start_background_reader(self._callback)
        deadline = time.monotonic() + max(1.0, self.config.timeout_seconds + 1.0)
        with self._reading_ready:
            while True:
                latest = self.get_latest(allow_stale=False)
                remaining = deadline - time.monotonic()
                if latest.get("ok") or remaining <= 0 or self._stop_event.is_set():
                    return latest
                self._reading_ready.wait(timeout=remaining)

    def read_water_for_duration(
        self,
        duration_seconds: float = 5.0,
        sample_gap_seconds: float = 1.0,
        cancel_checker: Optional[Callable[[], bool]] = None,
        event_callback: Optional[SensorEventCallback] = None,
    ) -> Dict[str, Any]:
        duration_seconds = max(1.0, float(duration_seconds or 5.0))
        sample_gap_seconds = max(0.2, float(sample_gap_seconds or 1.0))
        # Clock adjustments must never lengthen a selected scan.
        deadline = time.monotonic() + duration_seconds
        readings: List[Dict[str, Any]] = []
        errors: List[str] = []
        seen_timestamps: set[str] = set()

        if not self.state.running:
            self.start_background_reader(self._callback)

        while time.monotonic() < deadline:
            if cancel_checker and cancel_checker():
                return {"ok": False, "cancelled": True, "message": "Water reading was cancelled.", "readings": readings, "errors": errors}
            latest = self.get_latest(allow_stale=False)
            if latest.get("ok") and latest.get("reading"):
                reading = latest["reading"]
                stamp = str(reading.get("received_epoch") or reading.get("timestamp") or len(readings))
                if stamp not in seen_timestamps:
                    readings.append(dict(reading))
                    seen_timestamps.add(stamp)
                    if event_callback:
                        event_callback({"type": "sensor_sample", "reading": reading, "count": len(readings)})
            else:
                message = str(latest.get("message") or "No reading available.")
                if message not in errors:
                    errors.append(message)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                # A cancellation check should not wait through an entire scan gap.
                self._stop_event.wait(min(sample_gap_seconds, remaining))

        if not readings:
            return {"ok": False, "message": "No water readings were collected.", "readings": [], "errors": errors, "summary": {}}
        return {
            "ok": True,
            "message": f"Collected {len(readings)} water reading(s).",
            "readings": readings,
            "errors": errors,
            "summary": self.summarize_water_readings(readings),
        }

    def summarize_water_readings(self, readings: List[Dict[str, Any]]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"count": len(readings or []), "average": {}, "median": {}, "latest": readings[-1] if readings else None}
        for key in WATER_KEYS:
            values = [_safe_float(row.get(key)) for row in readings or []]
            numbers = [value for value in values if value is not None]
            summary["average"][key] = round(float(statistics.mean(numbers)), 4) if numbers else None
            summary["median"][key] = round(float(statistics.median(numbers)), 4) if numbers else None
        return summary

    def read_ultrasonic_distance(self) -> Dict[str, Any]:
        latest = self.get_latest(allow_stale=False)
        reading = latest.get("reading") if latest.get("ok") else None
        distance = _safe_float(reading.get("distance_cm")) if isinstance(reading, dict) else None
        if distance is None:
            return {
                "ok": False,
                "message": "Distance sensor reading is unavailable.",
                "distance_cm": None,
                "reading": reading,
                "status": self.get_status(),
            }
        return {
            "ok": True,
            "message": "Distance sensor reading returned.",
            "distance_cm": distance,
            "distance": distance,
            "reading": reading,
            "status": self.get_status(),
        }

    def get_history(self, limit: int = 50) -> Dict[str, Any]:
        limit = max(1, min(500, _safe_int(limit, 50)))
        with self._lock:
            rows = list(self._history)[-limit:]
        return {"ok": True, "count": len(rows), "readings": rows}

    # ------------------------------------------------------------------
    # Serial internals
    # ------------------------------------------------------------------
    def _find_serial_port(self) -> Optional[str]:
        configured = (self.config.port or "").strip()
        if configured:
            return configured

        try:
            from serial.tools import list_ports  # type: ignore

            ports = list(list_ports.comports())
            preferred_words = ("arduino", "nano", "ch340", "ch341", "usb serial", "usb2.0-serial")
            for info in ports:
                text = f"{getattr(info, 'device', '')} {getattr(info, 'description', '')} {getattr(info, 'manufacturer', '')}".lower()
                if any(word in text for word in preferred_words):
                    return str(info.device)
            if ports:
                return str(ports[0].device)
        except Exception:
            pass

        candidates: List[str] = []
        for pattern in ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usbserial*", "/dev/cu.usbmodem*"):
            candidates.extend(sorted(glob.glob(pattern)))
        if os.name == "nt":
            candidates.extend([f"COM{number}" for number in range(1, 33)])
        return candidates[0] if candidates else None

    def _open_serial(self, port: str) -> Any:
        try:
            import serial  # type: ignore
        except Exception as error:
            raise RuntimeError("pyserial is not installed. Install it with: python -m pip install pyserial") from error

        if self._serial is not None:
            try:
                if self._serial.is_open and getattr(self._serial, "port", None) == port:
                    return self._serial
            except Exception:
                pass
            self._close_serial()

        serial_conn = serial.Serial(port=port, baudrate=self.config.baudrate, timeout=self.config.timeout_seconds)
        self._serial = serial_conn
        time.sleep(0.8)
        try:
            serial_conn.reset_input_buffer()
        except Exception:
            pass
        return serial_conn

    def _close_serial(self) -> None:
        with self._lock:
            serial_conn = self._serial
            self._serial = None
        try:
            if serial_conn is not None:
                serial_conn.close()
        except Exception:
            pass

    def _is_stale(self, reading: Dict[str, Any]) -> bool:
        try:
            received_at = _safe_float(reading.get("received_epoch"))
            if received_at is None:
                return True
            age = time.time() - received_at
            return age < -1.0 or age > self.config.stale_after_seconds
        except Exception:
            return True

    def _set_state(self, **updates: Any) -> None:
        with self._lock:
            for key, value in updates.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)
            self._reading_ready.notify_all()

    def _emit(self, event: Dict[str, Any]) -> None:
        callback = self._callback
        if not callback:
            return
        try:
            callback(event)
        except Exception:
            pass


# ----------------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------------
def normalize_sensor_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Sensor payload must be an object.")
    reading: Dict[str, Any] = {
        "ok": True,
        "source": "arduino_nano",
        "timestamp": _now_iso(),
        "received_epoch": time.time(),
        "device": payload.get("device") or payload.get("board") or "aqua_sensor_bridge",
        "seq": payload.get("seq"),
    }

    candidates = {key: _pick_float(payload, aliases) for key, aliases in WATER_ALIAS.items()}
    present = [value for value in candidates.values() if value is not None]
    # Multiple zeroes usually indicate a missing-device placeholder. Preserve a
    # verified oxygen zero: anoxia is critical even when other channels are absent.
    placeholder_frame = len(present) >= 3 and all(value == 0 for value in present) and not (
        candidates["oxygen"] == 0 and _sensor_verified(payload, "oxygen")
    )
    unavailable_frame = any(_flag_is_false(payload.get(key)) for key in ("ok", "connected", "fresh", "valid") if key in payload)
    unavailable_frame = unavailable_frame or _flag_is_true(payload.get("stale"))
    for key, number in candidates.items():
        minimum, maximum = WATER_LIMITS[key]
        aliases = (key, "ammonia_gas") if key == "ammonia" else (key,)
        unavailable = any(
            _flag_is_false(payload[flag].get(alias))
            for flag in ("installed", "calibrated", "valid")
            if isinstance(payload.get(flag), dict) for alias in aliases if alias in payload[flag]
        )
        if (
            unavailable_frame or placeholder_frame or unavailable or number is None
            or not minimum <= number <= maximum
            or (number == 0 and key in {"temperature", "ph"})
            or (number == 0 and key == "oxygen" and not _sensor_verified(payload, key))
        ):
            number = None
        reading[key] = number

    reading["temperature_c"] = reading["temperature"]
    reading["turbidity_ntu"] = reading["turbidity"]
    reading["oxygen_mg_l"] = reading["oxygen"]
    # MQ-137 samples air. Never label this reading as dissolved ammonia in water.
    reading["ammonia_gas_ppm"] = reading["ammonia"]
    reading["ammonia_medium"] = "gas"
    reading["distance_cm"] = _pick_float(payload, DISTANCE_ALIAS)

    installed = payload.get("installed") if isinstance(payload.get("installed"), dict) else {}
    reading["installed"] = {
        key: not _flag_is_false(installed[key]) if key in installed else reading[key] is not None
        for key in WATER_KEYS
    }
    if "ammonia_gas" in installed:
        reading["installed"]["ammonia"] = not _flag_is_false(installed["ammonia_gas"]) and reading["ammonia"] is not None
    reading["installed"]["distance"] = not _flag_is_false(installed.get("distance", True)) and reading["distance_cm"] is not None
    if unavailable_frame or _flag_is_false(installed.get("distance", True)) or (reading["distance_cm"] is not None and reading["distance_cm"] <= 0):
        reading["distance_cm"] = None
        reading["installed"]["distance"] = False
    if isinstance(payload.get("calibrated"), dict):
        reading["calibrated"] = dict(payload["calibrated"])
    reading["ok"] = any(reading[key] is not None for key in WATER_KEYS) or reading["distance_cm"] is not None
    reading["usable_sensors"] = sum(1 for key in WATER_KEYS if reading.get(key) is not None)
    reading["minimum_usable_sensors"] = max(1, min(len(WATER_KEYS), _safe_int(payload.get("minimum_usable_sensors"), 3)))
    reading["ready_for_recommendation"] = reading["usable_sensors"] >= reading["minimum_usable_sensors"] and not _flag_is_false(
        payload.get("ready_for_recommendation", reading["usable_sensors"] >= reading["minimum_usable_sensors"])
    )

    for key in RAW_KEYS:
        if key in payload:
            reading[key] = payload.get(key)

    # Keep the original line content available for debugging without requiring frontend parsing.
    reading["raw"] = {k: v for k, v in payload.items() if k not in {"image", "frame"}}
    return reading


def _pick_float(payload: Dict[str, Any], aliases: Iterable[str]) -> Optional[float]:
    for alias in aliases:
        if alias in payload:
            number = _safe_float(payload.get(alias))
            if number is not None:
                return number
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool) or value == "":
            return None
        if isinstance(value, str) and value.strip().lower() in {"null", "none", "nan", "--", "n/a"}:
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return round(number, 4)
    except Exception:
        return None


def _flag_is_false(value: Any) -> bool:
    return value is False or value == 0 or (isinstance(value, str) and value.strip().lower() in {"false", "0", "off", "no", "disconnected", "invalid", "unavailable"})


def _flag_is_true(value: Any) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"true", "1", "on", "yes"})


def _sensor_verified(payload: Dict[str, Any], key: str) -> bool:
    return any(
        _flag_is_true(payload[flag].get(key))
        for flag in ("installed", "calibrated", "valid")
        if isinstance(payload.get(flag), dict)
    )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Global scanner/API-ready instance and module wrappers.
sensor_manager = SensorManager()


def configure_serial(port: str = "", baudrate: int = 115200, timeout_seconds: float = 1.0, reconnect_delay_seconds: float = 2.0) -> Dict[str, Any]:
    return sensor_manager.configure_serial(port=port, baudrate=baudrate, timeout_seconds=timeout_seconds, reconnect_delay_seconds=reconnect_delay_seconds)


def start_background_reader(event_callback: Optional[SensorEventCallback] = None) -> Dict[str, Any]:
    return sensor_manager.start_background_reader(event_callback=event_callback)


def stop_background_reader() -> Dict[str, Any]:
    return sensor_manager.stop_background_reader()


def get_status() -> Dict[str, Any]:
    return sensor_manager.get_status()


def get_latest(allow_stale: bool = True) -> Dict[str, Any]:
    return sensor_manager.get_latest(allow_stale=allow_stale)


def get_history(limit: int = 50) -> Dict[str, Any]:
    return sensor_manager.get_history(limit=limit)


def read_water_once() -> Dict[str, Any]:
    return sensor_manager.read_water_once()


def read_water_for_duration(
    duration_seconds: float = 5.0,
    sample_gap_seconds: float = 1.0,
    cancel_checker: Optional[Callable[[], bool]] = None,
    event_callback: Optional[SensorEventCallback] = None,
) -> Dict[str, Any]:
    return sensor_manager.read_water_for_duration(
        duration_seconds=duration_seconds,
        sample_gap_seconds=sample_gap_seconds,
        cancel_checker=cancel_checker,
        event_callback=event_callback,
    )


def summarize_water_readings(readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    return sensor_manager.summarize_water_readings(readings)


def read_ultrasonic_distance() -> Dict[str, Any]:
    return sensor_manager.read_ultrasonic_distance()
