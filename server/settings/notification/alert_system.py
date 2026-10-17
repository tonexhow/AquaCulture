"""
AquaCulture Notification Alert System

Purpose:
- Owns notification/alert settings stored in database/configuration.json.
- Sends alerts only for valid numeric readings.
- Ignores missing/no-reading sensors so unavailable sensors never trigger alerts.
- Uses subscriber rows in the existing SQLite database.
"""

from __future__ import annotations

import copy
import html
import json
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from sensors import normalize_sensor_payload
except ModuleNotFoundError:
    from server.sensors import normalize_sensor_payload

try:
    from database.database import db
except Exception:  # pragma: no cover
    db = None  # type: ignore

from .email_alert import check_email_env, send_email_alert
from .sms_alert import check_sms_env, send_sms_alert

BASE_DIR = Path(__file__).resolve().parents[3]
CONFIG_PATH = BASE_DIR / "database" / "configuration.json"

SENSOR_META: Dict[str, Dict[str, Any]] = {
    "Temperature": {"aliases": ("temperature", "temperature_c", "temp", "temp_c"), "unit": "°C", "safe": (-5.0, 60.0)},
    "PH": {"aliases": ("ph", "pH", "PH"), "unit": "pH", "safe": (0.0, 14.0)},
    "Turbidity": {"aliases": ("turbidity", "turbidity_ntu", "ntu"), "unit": "NTU", "safe": (0.0, 5000.0)},
    "Ammonia": {"aliases": ("ammonia", "ammonia_ppm", "nh3"), "unit": "ppm", "safe": (0.0, 1000.0)},
    "Oxygen": {"aliases": ("oxygen", "oxygen_mg_l", "dissolved_oxygen", "do"), "unit": "mg/L", "safe": (0.0, 25.0)},
}

DEFAULT_NOTIFICATION: Dict[str, Any] = {
    "Temperature": {
        "min": {"value": 24.0, "msg": "Water temperature is below the safe range. Average: !{average_reading} °C at !{time}."},
        "max": {"value": 32.0, "msg": "Water temperature is above the safe range. Average: !{average_reading} °C at !{time}."},
        "status": 1,
    },
    "PH": {
        "min": {"value": 6.5, "msg": "Water pH is below the safe range. Average: !{average_reading} at !{time}."},
        "max": {"value": 8.5, "msg": "Water pH is above the safe range. Average: !{average_reading} at !{time}."},
        "status": 1,
    },
    "Turbidity": {
        "min": {"value": 0.0, "msg": "Water turbidity is below the configured range. Average: !{average_reading} NTU at !{time}."},
        "max": {"value": 80.0, "msg": "Water turbidity is above the configured range. Average: !{average_reading} NTU at !{time}."},
        "status": 1,
    },
    "Ammonia": {
        "min": {"value": 0.0, "msg": "Ammonia is below the configured range. Average: !{average_reading} ppm at !{time}."},
        "max": {"value": 0.05, "msg": "Ammonia is above the safe range. Average: !{average_reading} ppm at !{time}."},
        "status": 0,  # No dissolved ammonia probe; MQ-137 gas cannot use water thresholds.
    },
    "Oxygen": {
        "min": {"value": 5.0, "msg": "Dissolved oxygen is below the safe range. Average: !{average_reading} mg/L at !{time}."},
        "max": {"value": 10.0, "msg": "Dissolved oxygen is above the configured range. Average: !{average_reading} mg/L at !{time}."},
        "status": 1,
    },
    "Summary": 86400,
    "Gap": 60,
    "Hold": 5,
    "Email": 1,
    "SMS": 0,
}

_PENDING: Dict[str, Dict[str, Any]] = {}
_LAST_SENT_AT: Dict[str, float] = {}
_LAST_STATUS: Dict[str, Any] = {"ok": True, "sent": False, "message": "No alert checked yet."}
_LAST_SAMPLE_ID: Optional[tuple[Any, ...]] = None


@dataclass(slots=True)
class Trigger:
    sensor: str
    direction: str
    value: float
    limit: float
    message: str
    unit: str
    average_reading: Optional[float] = None
    sample_count: int = 1
    hold_seconds: int = 0
    first_seen_at: Optional[float] = None
    confirmed_at: Optional[float] = None

    @property
    def key(self) -> str:
        return f"{self.sensor}:{self.direction}"


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"", "--", "null", "none", "nan", "n/a"}:
                return None
            value = text
        number = float(value)
        if not math.isfinite(number):
            return None
        return number
    except Exception:
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _now_text(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _read_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"notification": copy.deepcopy(DEFAULT_NOTIFICATION)}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"notification": copy.deepcopy(DEFAULT_NOTIFICATION)}
    except Exception:
        return {"notification": copy.deepcopy(DEFAULT_NOTIFICATION)}


def _write_config(data: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile
    fd, name = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".configuration-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, CONFIG_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def get_notification_settings() -> Dict[str, Any]:
    raw = _read_config().get("notification", {})
    return normalize_notification(raw if isinstance(raw, dict) else {})


def save_notification_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    if "Summary" in updates:
        validate_summary_interval(updates["Summary"])
    data = _read_config()
    current = normalize_notification(data.get("notification", {}) if isinstance(data.get("notification"), dict) else {})
    merged = deep_merge(current, updates if isinstance(updates, dict) else {})
    data["notification"] = normalize_notification(merged)
    _write_config(data)
    return data["notification"]


def normalize_notification(raw: Dict[str, Any]) -> Dict[str, Any]:
    clean = copy.deepcopy(DEFAULT_NOTIFICATION)
    for sensor in SENSOR_META:
        incoming = raw.get(sensor, {}) if isinstance(raw, dict) else {}
        if not isinstance(incoming, dict):
            continue
        clean[sensor]["status"] = 1 if _safe_int(incoming.get("status"), clean[sensor]["status"]) else 0
        for bound_name in ("min", "max"):
            incoming_bound = incoming.get(bound_name, {}) if isinstance(incoming.get(bound_name), dict) else {}
            value = _safe_float(incoming_bound.get("value"))
            if value is not None:
                clean[sensor][bound_name]["value"] = value
            if "msg" in incoming_bound:
                clean[sensor][bound_name]["msg"] = str(incoming_bound.get("msg") or "")

    try:
        clean["Summary"] = validate_summary_interval(raw.get("Summary", clean["Summary"]))
    except ValueError:
        pass
    clean["Gap"] = max(5, min(86400, _safe_int(raw.get("Gap"), clean["Gap"]))) if isinstance(raw, dict) else clean["Gap"]
    clean["Hold"] = max(1, min(3600, _safe_int(raw.get("Hold"), clean["Hold"]))) if isinstance(raw, dict) else clean["Hold"]
    clean["Email"] = 1 if _safe_int(raw.get("Email"), clean["Email"]) else 0 if isinstance(raw, dict) else clean["Email"]
    clean["SMS"] = 1 if _safe_int(raw.get("SMS"), clean["SMS"]) else 0 if isinstance(raw, dict) else clean["SMS"]
    return clean


def validate_summary_interval(value: Any) -> int:
    number = _safe_float(value)
    if number is None or not number.is_integer() or (number != -1 and not 1 <= number <= 31536000):
        raise ValueError("Summary must be whole seconds from 1 to 31536000, or -1 to disable.")
    return int(number)


def deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def normalize_reading(reading: Dict[str, Any]) -> Dict[str, float]:
    """Return only real alert-safe numeric readings.

    Missing, disabled, uncalibrated, stale, and impossible readings are ignored.
    Sensor normalization also distinguishes placeholder zeroes from measured
    zero ammonia/turbidity and explicitly verified zero oxygen.
    """
    values: Dict[str, float] = {}
    if not isinstance(reading, dict) or not _reading_available(reading):
        return values
    canonical = dict(reading)
    for sensor, meta in SENSOR_META.items():
        for alias in meta["aliases"]:
            number = _safe_float(reading.get(alias))
            if number is not None:
                canonical[meta["aliases"][0]] = number
                break
    normalized = normalize_sensor_payload(canonical)
    for sensor, meta in SENSOR_META.items():
        if sensor == "Ammonia":
            # Saved ammonia alert thresholds refer to dissolved water, not MQ-137 gas.
            # Do not apply these thresholds to a gas-phase sample without new,
            # separately calibrated and clearly labeled gas-risk settings.
            continue
        number = normalized.get(meta["aliases"][0])
        if number is not None:
            values[sensor] = number
    return values


def _flag_false(value: Any) -> bool:
    return value is False or value == 0 or (isinstance(value, str) and value.strip().lower() in {"false", "0", "off", "no", "disconnected", "invalid", "unavailable"})


def _reading_available(reading: Dict[str, Any], sensor_status: Optional[Dict[str, Any]] = None) -> bool:
    status = sensor_status if isinstance(sensor_status, dict) else {}
    for item in (reading, status):
        if any(_flag_false(item[key]) for key in ("ok", "connected", "fresh", "valid", "running") if key in item):
            return False
        stale = item.get("stale")
        if stale is True or stale == 1 or (isinstance(stale, str) and stale.strip().lower() in {"true", "1", "yes"}):
            return False
    stale_after = max(2.0, _safe_float(status.get("stale_after_seconds")) or 6.0)
    if "received_epoch" in reading:
        received_at = _safe_float(reading.get("received_epoch"))
        if received_at is None or not -1.0 <= time.time() - received_at <= stale_after:
            return False
    elif "timestamp" in reading:
        try:
            received_at = datetime.fromisoformat(str(reading["timestamp"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, OverflowError):
            return False
        if not -1.0 <= time.time() - received_at <= stale_after:
            return False
    if status.get("age_seconds") is not None:
        age = _safe_float(status.get("age_seconds"))
        if age is None or not -1.0 <= age <= stale_after:
            return False
    return True


def _sample_id(reading: Dict[str, Any]) -> Optional[tuple[Any, ...]]:
    raw = reading.get("raw") if isinstance(reading.get("raw"), dict) else {}
    for field, value in (
        ("seq", reading.get("seq")), ("uptime_ms", raw.get("uptime_ms", reading.get("uptime_ms"))),
        ("received_epoch", reading.get("received_epoch")), ("timestamp", reading.get("timestamp")),
    ):
        if value is not None:
            return (str(reading.get("device") or reading.get("source") or "sensor"), field, str(value))
    return None


def find_triggers(reading: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> List[Trigger]:
    settings = settings or get_notification_settings()
    values = normalize_reading(reading)
    triggers: List[Trigger] = []
    for sensor, value in values.items():
        sensor_settings = settings.get(sensor, {})
        if _safe_int(sensor_settings.get("status"), 0) != 1:
            continue
        meta = SENSOR_META[sensor]
        for direction in ("min", "max"):
            rule = sensor_settings.get(direction, {}) if isinstance(sensor_settings.get(direction), dict) else {}
            limit = _safe_float(rule.get("value"))
            if limit is None:
                continue
            if direction == "min" and value < limit:
                triggers.append(Trigger(sensor, direction, value, limit, str(rule.get("msg") or ""), meta["unit"]))
            if direction == "max" and value > limit:
                triggers.append(Trigger(sensor, direction, value, limit, str(rule.get("msg") or ""), meta["unit"]))
    return triggers


def process_sensor_reading(
    reading: Dict[str, Any], *, creation: str = "SYSTEM", sensor_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    global _LAST_STATUS, _LAST_SAMPLE_ID
    settings = get_notification_settings()
    available = isinstance(reading, dict) and _reading_available(reading, sensor_status)
    values = normalize_reading(reading) if available else {}
    if not values:
        _clear_pending(set())
        _LAST_STATUS = {"ok": True, "sent": False, "message": "No valid reading; alert skipped.", "valid_readings": {}, "invalid_reading": True}
        return _LAST_STATUS

    sample_id = _sample_id(reading)
    if sample_id is not None and sample_id == _LAST_SAMPLE_ID:
        _LAST_STATUS = {"ok": True, "sent": False, "message": "Repeated sensor sample; alert skipped.", "duplicate_reading": True}
        return _LAST_STATUS
    _LAST_SAMPLE_ID = sample_id

    triggers = find_triggers(reading, settings)
    if not triggers:
        _clear_pending(set())
        _LAST_STATUS = {"ok": True, "sent": False, "message": "No alert trigger matched.", "valid_readings": values, "triggers": []}
        return _LAST_STATUS

    now = time.time()
    hold_seconds = max(1, _safe_int(settings.get("Hold"), 5))
    gap_seconds = max(5, _safe_int(settings.get("Gap"), 60))
    active_keys = {item.key for item in triggers}
    _clear_pending(active_keys)

    confirmed: List[Trigger] = []
    pending: List[Dict[str, Any]] = []
    for trigger in triggers:
        item = _PENDING.get(trigger.key)
        stale_after = max(2.0, _safe_float((sensor_status or {}).get("stale_after_seconds")) or 6.0)
        if not item or now - float(item.get("last_seen_at", 0)) > stale_after or item.get("limit") != trigger.limit:
            item = {"started_at": now, "values": [], "sensor": trigger.sensor, "direction": trigger.direction, "limit": trigger.limit}
            _PENDING[trigger.key] = item
        item["last_seen_at"] = now
        item.setdefault("values", []).append(trigger.value)
        elapsed = now - float(item.get("started_at") or now)
        if elapsed >= hold_seconds and len(item["values"]) >= 2:
            numbers = [float(v) for v in item.get("values", []) if _safe_float(v) is not None]
            trigger.average_reading = round(statistics.mean(numbers), 4) if numbers else trigger.value
            trigger.sample_count = len(numbers) or 1
            trigger.hold_seconds = hold_seconds
            trigger.first_seen_at = float(item.get("started_at") or now)
            trigger.confirmed_at = now
            trigger.message = render_message(trigger.message, trigger, creation)
            confirmed.append(trigger)
        else:
            pending.append({
                "sensor": trigger.sensor,
                "direction": trigger.direction,
                "value": trigger.value,
                "limit": trigger.limit,
                "elapsed_seconds": round(elapsed, 2),
                "remaining_seconds": round(max(0, hold_seconds - elapsed), 2),
            })

    if not confirmed:
        _LAST_STATUS = {"ok": True, "sent": False, "message": f"Alert matched; holding for {hold_seconds} second(s) before sending.", "pending": pending, "valid_readings": values}
        return _LAST_STATUS

    allowed = [trigger for trigger in confirmed if _gap_allows(trigger.key, gap_seconds)]
    if not allowed:
        for trigger in confirmed:
            _PENDING.pop(trigger.key, None)
        _LAST_STATUS = {"ok": True, "sent": False, "message": "Alert confirmed but duplicate gap blocked sending.", "triggers": [trigger_to_dict(t) for t in confirmed]}
        return _LAST_STATUS

    result = send_alerts(allowed, settings)
    for trigger in allowed:
        _LAST_SENT_AT[trigger.key] = time.time()
        _PENDING.pop(trigger.key, None)

    _LAST_STATUS = {"ok": bool(result.get("ok")), "sent": bool(result.get("sent")), "message": result.get("message", "Alert processed."), "triggers": [trigger_to_dict(t) for t in allowed], "results": result.get("results", [])}
    save_log(_LAST_STATUS["message"])
    return _LAST_STATUS


def render_message(template: str, trigger: Trigger, creation: str) -> str:
    average = trigger.average_reading if trigger.average_reading is not None else trigger.value
    text = template or f"{trigger.sensor} reached the {trigger.direction} limit. Average: !{{average_reading}} {trigger.unit} at !{{time}}."
    replacements = {
        "sensor": trigger.sensor,
        "direction": trigger.direction,
        "reading": trigger.value,
        "average_reading": average,
        "limit": trigger.limit,
        "time": _now_text(trigger.confirmed_at),
        "creation": creation,
        "hold_seconds": trigger.hold_seconds,
        "sample_count": trigger.sample_count,
    }
    for key, value in replacements.items():
        text = text.replace(f"!{{{key}}}", str(value))
    return text


def trigger_to_dict(trigger: Trigger) -> Dict[str, Any]:
    return {
        "sensor": trigger.sensor,
        "direction": trigger.direction,
        "value": trigger.value,
        "limit": trigger.limit,
        "unit": trigger.unit,
        "message": trigger.message,
        "average_reading": trigger.average_reading,
        "sample_count": trigger.sample_count,
        "hold_seconds": trigger.hold_seconds,
    }


def send_alerts(triggers: List[Trigger], settings: Dict[str, Any]) -> Dict[str, Any]:
    if not triggers:
        return {"ok": True, "sent": False, "message": "No valid alert triggers; delivery skipped.", "results": []}
    subscribers = get_subscribers_grouped()
    results: List[Dict[str, Any]] = []
    html_body = build_alert_email_html(triggers)
    sms_text = build_alert_sms_text(triggers)

    if _safe_int(settings.get("Email"), 0) == 1:
        contacts = subscribers.get("EMAIL", [])
        if contacts:
            results.append(send_email_alert(contacts, "AquaCulture Water Alert", html_body))
        else:
            results.append({"ok": False, "channel": "EMAIL", "message": "Email is enabled but no email subscribers are saved.", "sent": 0})

    if _safe_int(settings.get("SMS"), 0) == 1:
        contacts = subscribers.get("SMS", [])
        if contacts:
            results.append(send_sms_alert(contacts, sms_text))
        else:
            results.append({"ok": False, "channel": "SMS", "message": "SMS is enabled but no SMS subscribers are saved.", "sent": 0})

    if not results:
        return {"ok": True, "sent": False, "message": "Alert confirmed but all delivery channels are turned off.", "results": []}
    sent_any = any(bool(item.get("ok")) for item in results)
    return {"ok": sent_any, "sent": sent_any, "message": "Alert notification processed.", "results": results}


def send_test_notification(channel: str = "all") -> Dict[str, Any]:
    selected = str(channel or "all").upper()
    settings = get_notification_settings()
    subscribers = get_subscribers_grouped()
    results: List[Dict[str, Any]] = []
    now = _now_text()
    html_body = build_email_shell(
        title="AquaCulture Email Test",
        intro="Your email notification channel is working correctly.",
        rows=[("Status", "Ready"), ("Sent at", now), ("Purpose", "Settings test message")],
        accent="info",
    )
    sms_text = f"AquaCulture test notification sent at {now}."

    if selected in {"ALL", "EMAIL"}:
        contacts = subscribers.get("EMAIL", [])
        if contacts:
            results.append(send_email_alert(contacts, "AquaCulture Email Test", html_body, text_body=sms_text))
        else:
            results.append({"ok": False, "channel": "EMAIL", "message": "No email subscribers are saved.", "sent": 0})
    if selected in {"ALL", "SMS"}:
        contacts = subscribers.get("SMS", [])
        if contacts:
            results.append(send_sms_alert(contacts, sms_text))
        else:
            results.append({"ok": False, "channel": "SMS", "message": "No SMS subscribers are saved.", "sent": 0})

    ok = any(bool(item.get("ok")) for item in results)
    return {"ok": ok, "sent": ok, "message": "Test notification processed." if ok else "No test notification was sent.", "settings": settings, "subscriber_count": {k: len(v) for k, v in subscribers.items()}, "results": results}


def get_subscribers() -> Dict[str, Any]:
    if db is None:
        return {"ok": False, "message": "Database helper is unavailable.", "subscribers": [], "count": 0}
    try:
        db.create_tables()
        rows = db.fetch_many("subscriber", order_by="id DESC")
        return {"ok": True, "subscribers": rows, "count": len(rows)}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "subscribers": [], "count": 0}


def get_subscribers_grouped() -> Dict[str, List[str]]:
    grouped = {"EMAIL": [], "SMS": []}
    rows = get_subscribers().get("subscribers", [])
    for row in rows:
        typ = str(row.get("type") or "").upper()
        contact = str(row.get("contact") or "").strip()
        if typ in grouped and contact:
            grouped[typ].append(contact)
    return grouped


def add_subscriber(contact_type: str, contact: str) -> Dict[str, Any]:
    typ = str(contact_type or "").upper().strip()
    clean_contact = str(contact or "").strip()
    if typ not in {"EMAIL", "SMS"} or not clean_contact:
        return {"ok": False, "message": "Subscriber type must be EMAIL or SMS and contact is required."}
    if db is None:
        return {"ok": False, "message": "Database helper is unavailable."}
    db.create_tables()
    new_id = db.insert("subscriber", {"type": typ, "contact": clean_contact})
    return {"ok": True, "message": "Subscriber added.", "id": new_id}


def update_subscriber(subscriber_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    if db is None:
        return {"ok": False, "message": "Database helper is unavailable."}
    update: Dict[str, Any] = {}
    if "type" in data:
        typ = str(data.get("type") or "").upper().strip()
        if typ not in {"EMAIL", "SMS"}:
            return {"ok": False, "message": "Subscriber type must be EMAIL or SMS."}
        update["type"] = typ
    if "contact" in data:
        update["contact"] = str(data.get("contact") or "").strip()
    changed = db.update_by_id("subscriber", int(subscriber_id), update)
    return {"ok": True, "message": "Subscriber updated." if changed else "No subscriber row changed.", "updated": changed}


def delete_subscriber(subscriber_id: int) -> Dict[str, Any]:
    if db is None:
        return {"ok": False, "message": "Database helper is unavailable."}
    deleted = db.delete_by_id("subscriber", int(subscriber_id))
    return {"ok": True, "message": "Subscriber removed." if deleted else "Subscriber not found.", "deleted": deleted}


def delete_subscribers(contact_type: str = "") -> Dict[str, Any]:
    if db is None:
        return {"ok": False, "message": "Database helper is unavailable."}
    typ = str(contact_type or "").upper().strip()
    db.create_tables()
    if typ in {"EMAIL", "SMS"}:
        with db.connect() as conn:
            cur = conn.execute("DELETE FROM subscriber WHERE type = ?", (typ,))
            conn.commit()
            deleted = int(cur.rowcount)
        return {"ok": True, "message": f"{typ} subscribers deleted.", "deleted": deleted}
    deleted = db.delete_all("subscriber")
    return {"ok": True, "message": "All subscribers deleted.", "deleted": deleted}


def get_alert_status() -> Dict[str, Any]:
    subscribers = get_subscribers_grouped()
    return {
        "ok": True,
        "last_status": copy.deepcopy(_LAST_STATUS),
        "pending": copy.deepcopy(_PENDING),
        "subscribers": {key: len(value) for key, value in subscribers.items()},
        "environment": {
            "email": check_email_env(),
            "sms": check_sms_env(),
        },
        "settings": get_notification_settings(),
    }


def build_alert_email_html(triggers: List[Trigger]) -> str:
    rows: List[tuple[str, str]] = []
    for trigger in triggers:
        average = trigger.average_reading if trigger.average_reading is not None else trigger.value
        reading = f"{average:.3g} {trigger.unit}" if isinstance(average, (int, float)) else f"{average} {trigger.unit}"
        limit = f"{trigger.limit:.3g} {trigger.unit}"
        direction = "Below minimum" if trigger.direction == "min" else "Above maximum"
        rows.append((trigger.sensor, f"{direction} · Average {reading} · Limit {limit}"))
        rows.append(("Message", trigger.message))
    return build_email_shell(
        title="AquaCulture Water Alert",
        intro="One or more monitored water readings needs attention.",
        rows=rows,
        accent="alert",
    )


def build_email_shell(title: str, intro: str, rows: List[tuple[str, str]], accent: str = "alert") -> str:
    sent_at = _now_text()
    safe_title = html.escape(title)
    safe_intro = html.escape(intro)
    accent_color = "#dc2626" if accent == "alert" else "#0f766e"
    row_html = "".join(
        "<tr>"
        f"<td style='padding:12px 14px;border-bottom:1px solid #e5e7eb;color:#475569;font-weight:700;width:34%;'>{html.escape(str(label))}</td>"
        f"<td style='padding:12px 14px;border-bottom:1px solid #e5e7eb;color:#0f172a;'>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"""
<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:#f3faf7;font-family:Arial,Helvetica,sans-serif;color:#0f172a;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3faf7;padding:24px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;background:#ffffff;border:1px solid #dbe7df;border-radius:20px;overflow:hidden;box-shadow:0 18px 42px rgba(15,23,42,.08);">
            <tr>
              <td style="padding:22px 24px;background:linear-gradient(135deg,#0f766e,#14b884);color:#ffffff;">
                <div style="font-size:13px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;opacity:.86;">AquaCulture Monitoring</div>
                <h1 style="margin:8px 0 0;font-size:26px;line-height:1.15;">{safe_title}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:22px 24px;">
                <p style="margin:0 0 16px;font-size:16px;line-height:1.55;color:#334155;">{safe_intro}</p>
                <div style="border-left:5px solid {accent_color};background:#f8fafc;border-radius:14px;padding:14px 16px;margin-bottom:18px;">
                  <strong style="display:block;color:{accent_color};font-size:14px;text-transform:uppercase;letter-spacing:.08em;">Action recommended</strong>
                  <span style="display:block;margin-top:4px;color:#334155;line-height:1.45;">Please check the tank condition and verify the sensor readings.</span>
                </div>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;border-collapse:separate;border-spacing:0;">
                  {row_html}
                  <tr>
                    <td style="padding:12px 14px;color:#475569;font-weight:700;width:34%;">Sent at</td>
                    <td style="padding:12px 14px;color:#0f172a;">{html.escape(sent_at)}</td>
                  </tr>
                </table>
                <p style="margin:18px 0 0;font-size:13px;line-height:1.5;color:#64748b;">This message was generated automatically by AquaCulture. Missing or invalid sensor values are ignored.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()


def build_alert_sms_text(triggers: List[Trigger]) -> str:
    parts = [f"{t.sensor} {t.direction}: avg {t.average_reading if t.average_reading is not None else t.value} {t.unit}" for t in triggers[:3]]
    return "AquaCulture Alert: " + "; ".join(parts)


def save_log(message: str) -> None:
    if db is None:
        return
    try:
        db.create_tables()
        db.insert("log", {"session_id": None, "message": str(message)})
    except Exception:
        return


def _gap_allows(key: str, gap_seconds: int) -> bool:
    return time.time() - float(_LAST_SENT_AT.get(key, 0.0)) >= gap_seconds


def _clear_pending(active_keys: set[str]) -> None:
    for key in list(_PENDING.keys()):
        if key not in active_keys:
            _PENDING.pop(key, None)
