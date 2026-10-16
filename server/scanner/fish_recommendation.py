"""
AquaCulture - scanner/fish_recommendation.py

Purpose:
- Rule-based species recommendation using water readings.
- Reads sensor values through sensors.py when doing real scans.
- Can also score manually supplied/test values.
- Uses database/species rows seeded from species_rules.xlsx.
- Saves real scan summary into water_reading table.

Architecture:
PHASE 1: DATA GATHERING
- Gather readings first through sensors.py.
- Keep raw reading array for analysis.

PHASE 2: ANALYTICS
- Validate readings.
- Compute average and median.
- Detect sudden spikes.
- Parse species rule TEXT arrays.
- Score each species.
- Return top suggestions.

Important:
- Sensor unavailable returns a safe error.
- Test recommendation does not save to database.
"""

from __future__ import annotations

import ast
import json
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from database.database import db
except Exception:  # pragma: no cover
    db = None  # type: ignore

try:
    import sensors as sensor_module
except Exception:  # pragma: no cover
    try:
        import sensor as sensor_module  # compatibility with the old codebase name
    except Exception:
        sensor_module = None  # type: ignore


WATER_KEYS = ("temperature", "ph", "oxygen", "turbidity", "ammonia")
RULE_KEYS = ("temperature", "ph", "oxygen", "turbidity", "ammonia")
MINIMUM_RECOMMENDATION_SENSOR_COUNT = 3
SENSOR_LABELS = {
    "temperature": "Temperature",
    "ph": "pH",
    "oxygen": "Oxygen",
    "turbidity": "Turbidity",
    "ammonia": "Ammonia",
}


@dataclass
class RecommendationState:
    running: bool = False
    cancel_requested: bool = False
    message: str = "Recommendation scan is idle."
    started_at: Optional[str] = None
    progress: int = 0
    last_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "running": self.running,
            "cancel_requested": self.cancel_requested,
            "message": self.message,
            "started_at": self.started_at,
            "progress": self.progress,
            "last_result": self.last_result,
        }


_state = RecommendationState()
_state_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _available_sensor_keys(reading: Dict[str, Any]) -> List[str]:
    return [key for key in WATER_KEYS if _safe_float(reading.get(key)) is not None]


def _missing_sensor_keys(reading: Dict[str, Any]) -> List[str]:
    return [key for key in WATER_KEYS if _safe_float(reading.get(key)) is None]


def _sensor_names(keys: Iterable[str]) -> List[str]:
    return [SENSOR_LABELS.get(key, str(key)) for key in keys]


def _availability_payload(reading: Dict[str, Any]) -> Dict[str, Any]:
    available = _available_sensor_keys(reading)
    missing = _missing_sensor_keys(reading)
    return {
        "available_sensors": available,
        "available_sensor_names": _sensor_names(available),
        "missing_sensors": missing,
        "missing_sensor_names": _sensor_names(missing),
        "available_sensor_count": len(available),
        "minimum_sensor_count": MINIMUM_RECOMMENDATION_SENSOR_COUNT,
        "accuracy_note": "Recommendations use the readings currently available. More sensor readings improve accuracy.",
    }


def _not_enough_sensor_payload(reading: Dict[str, Any], readings: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    availability = _availability_payload(reading)
    return {
        "ok": False,
        "message": f"At least {MINIMUM_RECOMMENDATION_SENSOR_COUNT} valid sensor readings are required before recommending species.",
        "reading": reading,
        "readings": readings or [],
        **availability,
    }


def get_recommendation_status() -> Dict[str, Any]:
    with _state_lock:
        return _state.to_dict()


def cancel_recommendation() -> Dict[str, Any]:
    with _state_lock:
        if not _state.running:
            return {"ok": True, "message": "No recommendation scan is currently running.", "running": False}
        _state.cancel_requested = True
        _state.message = "Cancel requested. The system will analyze already gathered readings."
        return _state.to_dict()


def _is_cancel_requested() -> bool:
    with _state_lock:
        return _state.cancel_requested


def _set_state(**kwargs: Any) -> None:
    with _state_lock:
        for key, value in kwargs.items():
            if hasattr(_state, key):
                setattr(_state, key, value)


def _push_event(event_callback: Optional[Callable[[Dict[str, Any]], None]], event: Dict[str, Any]) -> None:
    if event_callback:
        try:
            event_callback(event)
        except Exception:
            pass


def _oled_activity(step: str, message: str, progress: int = 50, busy: bool = True) -> None:
    try:
        from hosting import publish_oled_activity

        publish_oled_activity(step=step, message=message, progress=progress, busy=busy)
    except Exception:
        pass


def scan_water_recommendation(
    session_id: Optional[int] = None,
    duration_seconds: float = 5.0,
    top: int = 5,
    sample_gap_seconds: float = 1.0,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Main route-ready entry point for POST /api/water/scan.

    routing.py should check registered session before calling this function.
    """
    with _state_lock:
        if _state.running:
            return {"ok": False, "message": "A water scan is already running.", "status": _state.to_dict()}
        _state.running = True
        _state.cancel_requested = False
        _state.message = "Water scan started. Gathering readings."
        _state.started_at = _now_iso()
        _state.progress = 5
        _state.last_result = None

    duration_seconds = max(3.0, float(duration_seconds or 5.0))
    top = max(3, min(50, _safe_int(top, 5)))
    sample_gap_seconds = max(0.2, float(sample_gap_seconds or 1.0))

    try:
        if sensor_module is None:
            return _finish_with_error("Sensor module is unavailable.")

        _push_event(event_callback, {"type": "water", "message": "Gathering water readings.", "progress": 10})
        _oled_activity("SCANNING SENSORS", "Reading water sensors.", 18, True)

        sensor_manager = getattr(sensor_module, "sensor_manager", None)
        if sensor_manager is not None:
            gather_result = sensor_manager.read_water_for_duration(
                duration_seconds=duration_seconds,
                sample_gap_seconds=sample_gap_seconds,
                cancel_checker=_is_cancel_requested,
                event_callback=event_callback,
            )
        else:
            gather_result = sensor_module.read_water_for_duration(
                duration_seconds=duration_seconds,
                sample_gap_seconds=sample_gap_seconds,
                cancel_checker=_is_cancel_requested,
                event_callback=event_callback,
            )

        _set_state(message="Analyzing water readings.", progress=55)
        _oled_activity("ANALYZING WATER", "Checking sensor values.", 58, True)

        if not gather_result.get("ok") or not gather_result.get("readings"):
            return _finish_with_error(gather_result.get("message", "No water readings were collected."), gather_result.get("errors"))

        readings = gather_result.get("readings", [])
        result = recommend_from_readings(readings=readings, top=top, save=True, session_id=session_id)
        result["sensor_errors"] = gather_result.get("errors", [])

        if not result.get("ok"):
            message = result.get("message", "Water recommendation could not be completed.")
            _set_state(running=False, cancel_requested=False, message=message, progress=0, last_result=result)
            _push_event(event_callback, {"type": "water", "message": message, "progress": 0, "result": result})
            return {"ok": False, "message": message, "result": result}

        _set_state(running=False, cancel_requested=False, message="Water recommendation completed.", progress=100, last_result=result)
        _push_event(event_callback, {"type": "water", "message": "Water recommendation completed.", "progress": 100, "result": result})
        _oled_activity("DONE", "Water scan completed.", 100, False)

        return {"ok": True, "message": "Water recommendation completed.", "result": result}
    except Exception as error:
        return _finish_with_error("Water recommendation failed safely.", _safe_error(error))


def _finish_with_error(message: str, error: Any = None) -> Dict[str, Any]:
    payload = {"ok": False, "message": message, "error": error, "timestamp": _now_iso()}
    _set_state(running=False, message=message, progress=0, last_result=payload)
    _oled_activity("ERROR", message, 100, False)
    return payload


def test_recommendation(values: Dict[str, Any], top: int = 5) -> Dict[str, Any]:
    """
    Used by /api/test/recommendation.
    Does not save to water_reading database.

    Every sensor is allowed to be absent during prototype/testing, but the
    recommendation engine needs at least 3 valid water values to avoid a weak
    suggestion. This lets the system work when any two sensors are missing
    today, and still work later if a different sensor is unavailable.
    """
    reading = normalize_reading(values)
    availability = _availability_payload(reading)
    if availability["available_sensor_count"] < MINIMUM_RECOMMENDATION_SENSOR_COUNT:
        return _not_enough_sensor_payload(reading)

    result = recommend_from_readings([reading], top=top, save=False, session_id=None)
    status = bool(result.get("ok"))
    return {
        "ok": status,
        "message": "Test recommendation completed. Result was not saved." if status else result.get("message", "Test recommendation could not run."),
        "result": result,
        **availability,
    }


def recommend_from_values(values: Dict[str, Any], top: int = 5) -> Dict[str, Any]:
    """Alias for test/manual use."""
    return test_recommendation(values, top=top)


def recommend_from_readings(
    readings: List[Dict[str, Any]],
    top: int = 5,
    save: bool = False,
    session_id: Optional[int] = None,
) -> Dict[str, Any]:
    clean_readings = [normalize_reading(row) for row in readings]
    clean_readings = [row for row in clean_readings if any(row.get(key) is not None for key in WATER_KEYS)]

    if not clean_readings:
        return {"ok": False, "message": "No valid readings are available for recommendation.", "readings": []}

    analytics = analyze_water_readings(clean_readings)
    final_reading = analytics["median"]
    availability = _availability_payload(final_reading)

    if availability["available_sensor_count"] < MINIMUM_RECOMMENDATION_SENSOR_COUNT:
        payload = _not_enough_sensor_payload(final_reading, clean_readings)
        payload["summary"] = analytics
        return payload

    species_rows = load_species_rows()
    suggestions = score_species(final_reading, species_rows)
    top = max(1, min(50, _safe_int(top, 5)))
    top_suggestions = suggestions[:top]

    water_reading_id: Optional[int] = None
    if save and db is not None:
        try:
            creation = str(session_id) if session_id is not None else "SYSTEM"
            water_reading_id = db.insert("water_reading", {
                "creation": creation,
                "temperature": final_reading.get("temperature"),
                "ph": final_reading.get("ph"),
                "oxygen": final_reading.get("oxygen"),
                "turbidity": final_reading.get("turbidity"),
                "ammonia": final_reading.get("ammonia"),
            })
        except Exception as error:
            analytics["database_warning"] = f"Water reading was analyzed but not saved: {_safe_error(error)}"

    return {
        "ok": True,
        "message": "Species recommendation completed.",
        "created_at": _now_iso(),
        "water_reading_id": water_reading_id,
        "readings": clean_readings,
        "summary": analytics,
        "final_reading": final_reading,
        "top": top,
        "suggestions": top_suggestions,
        "species_count_scored": len(suggestions),
        **availability,
    }


def normalize_reading(row: Dict[str, Any]) -> Dict[str, Any]:
    def pick(*keys: str) -> Any:
        for key in keys:
            if key in row:
                return row.get(key)
        return None

    def valid(key: str, value: Any) -> Optional[float]:
        number = _safe_float(value)
        if number is None:
            return None
        limits = {
            "temperature": (0.0, 60.0),
            "ph": (0.0, 14.0),
            "oxygen": (0.0, 25.0),
            "turbidity": (0.0, 100.0),
            "ammonia": (0.0, 10.0),
        }
        minimum, maximum = limits[key]
        if number < minimum or number > maximum:
            return None
        return round(number, 4 if key == "ammonia" else 2)

    return {
        "temperature": valid("temperature", pick("temperature", "temperature_c", "temp", "temp_c", "water_temperature", "waterTemperature")),
        "ph": valid("ph", pick("ph", "pH", "PH")),
        "oxygen": valid("oxygen", pick("oxygen", "oxygen_mg_l", "dissolved_oxygen", "dissolvedOxygen", "do", "DO", "oxy")),
        "turbidity": valid("turbidity", pick("turbidity", "turbidity_ntu", "ntu", "NTU", "turb")),
        "ammonia": valid("ammonia", pick("ammonia", "ammonia_mg_l", "nh3", "NH3", "ammo")),
        "timestamp": row.get("timestamp") or _now_iso(),
    }


def analyze_water_readings(readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    averages: Dict[str, Optional[float]] = {}
    medians: Dict[str, Optional[float]] = {}
    ranges: Dict[str, Dict[str, Optional[float]]] = {}
    spike_warnings: List[Dict[str, Any]] = []

    for key in WATER_KEYS:
        values = [_safe_float(row.get(key)) for row in readings]
        values = [value for value in values if value is not None]
        if not values:
            averages[key] = None
            medians[key] = None
            ranges[key] = {"min": None, "max": None}
            continue

        averages[key] = round(statistics.mean(values), 4)
        medians[key] = round(statistics.median(values), 4)
        smallest = min(values)
        biggest = max(values)
        ranges[key] = {"min": round(smallest, 4), "max": round(biggest, 4)}

        warning = detect_spike(values, key)
        if warning:
            spike_warnings.append(warning)

    unstable = bool(spike_warnings)
    availability = _availability_payload(medians)
    return {
        "count": len(readings),
        "average": averages,
        "median": medians,
        "range": ranges,
        "unstable": unstable,
        "spike_warnings": spike_warnings,
        "warning_message": "Sudden high spike detected. You may rescan, but a result is still shown." if unstable else "",
        **availability,
    }


def detect_spike(values: List[float], key: str) -> Optional[Dict[str, Any]]:
    if len(values) < 3:
        return None
    smallest = min(values)
    biggest = max(values)

    # From plan: if big - small >= small and big_reading_count <= 10% of total.
    # For values near zero, use a small positive guard so ammonia can still be checked safely.
    baseline = max(abs(smallest), 0.0001)
    if biggest - smallest < baseline:
        return None

    threshold = smallest + baseline
    high_count = sum(1 for value in values if value >= threshold)
    allowed_count = max(1, int(len(values) * 0.10))
    if high_count <= allowed_count:
        return {
            "key": key,
            "min": round(smallest, 4),
            "max": round(biggest, 4),
            "high_reading_count": high_count,
            "total_reading_count": len(values),
            "message": f"Sudden high spike detected for {key}.",
        }
    return None


def load_species_rows() -> List[Dict[str, Any]]:
    if db is None:
        return []
    try:
        return db.fetch_many("species", order_by="id ASC")
    except Exception:
        return []


def score_species(final_reading: Dict[str, Any], species_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    available_keys = set(_available_sensor_keys(final_reading))
    missing_keys = set(WATER_KEYS) - available_keys

    for row in species_rows:
        score = 0.0
        available_possible = 0.0
        total_possible = 0.0
        missing_possible = 0.0
        matches: Dict[str, Dict[str, Any]] = {}
        priority_column = None
        priority_score = -1.0

        for key in RULE_KEYS:
            reading = _safe_float(final_reading.get(key))
            rule = parse_rule_array(row.get(key))
            rule_weight = float(rule[2]) if len(rule) >= 3 else 0.0
            total_possible += rule_weight

            detail = score_range_rule(reading, rule)
            detail["available"] = key in available_keys
            detail["ignored"] = key in missing_keys
            detail["rule_weight"] = rule_weight
            matches[key] = detail

            if key in available_keys:
                score += detail["score_awarded"]
                available_possible += detail["score_possible"]
                if detail["score_awarded"] > priority_score:
                    priority_score = detail["score_awarded"]
                    priority_column = key
            else:
                missing_possible += rule_weight

        percentage = round((score / available_possible) * 100, 2) if available_possible > 0 else 0.0
        overall_percentage = round((score / total_possible) * 100, 2) if total_possible > 0 else 0.0
        confidence_percentage = round((available_possible / total_possible) * 100, 2) if total_possible > 0 else 0.0
        missing_dependency_percentage = round((missing_possible / total_possible) * 100, 2) if total_possible > 0 else 0.0

        suggestions.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "score": round(score, 2),
            "max_score": round(available_possible, 2),
            "total_rule_score": round(total_possible, 2),
            "percentage": percentage,
            "overall_percentage": overall_percentage,
            "confidence_percentage": confidence_percentage,
            "missing_dependency_percentage": missing_dependency_percentage,
            "priority_column": priority_column,
            "priority_score": round(max(priority_score, 0.0), 2),
            "used_sensors": sorted(available_keys),
            "ignored_sensors": sorted(missing_keys),
            "matches": matches,
            "environment": parse_rule_array(row.get("environment")),
            "regulation": parse_rule_array(row.get("regulation")),
            "ecology": parse_rule_array(row.get("ecology")),
            "safety": parse_rule_array(row.get("safety")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "has_image": bool(row.get("image")),
            "image_url": f"/api/species/{row.get('id')}/image" if row.get("id") else "",
        })

    # Primary ranking uses only available readings. When species have similar
    # available-match scores, prefer species whose rules relied less on missing
    # sensor categories.
    suggestions.sort(
        key=lambda item: (
            item["percentage"],
            item["score"],
            item["confidence_percentage"],
            -item["missing_dependency_percentage"],
        ),
        reverse=True,
    )
    return suggestions


def parse_rule_array(value: Any) -> List[float]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_safe_float(item) or 0.0 for item in value]

    text = str(value).strip()
    if not text:
        return []

    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, (list, tuple)):
            return [_safe_float(item) or 0.0 for item in parsed]
    except Exception:
        pass

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [_safe_float(item) or 0.0 for item in parsed]
    except Exception:
        pass

    # Fallback for text like "22, 32, 30".
    cleaned = text.replace("[", "").replace("]", "")
    result: List[float] = []
    for part in cleaned.split(","):
        number = _safe_float(part.strip())
        if number is not None:
            result.append(number)
    return result


def score_range_rule(reading: Optional[float], rule: List[float]) -> Dict[str, Any]:
    if reading is None:
        return {
            "ok": False,
            "reading": None,
            "min": None,
            "max": None,
            "center": None,
            "score_possible": 0.0,
            "score_awarded": 0.0,
            "message": "Reading unavailable; this factor was ignored for this scan.",
        }

    if len(rule) < 3:
        return {
            "ok": False,
            "reading": reading,
            "min": None,
            "max": None,
            "center": None,
            "score_possible": 0.0,
            "score_awarded": 0.0,
            "message": "Rule is missing or invalid.",
        }

    minimum, maximum, possible = float(rule[0]), float(rule[1]), float(rule[2])
    if maximum <= minimum or possible <= 0:
        return {
            "ok": False,
            "reading": reading,
            "min": minimum,
            "max": maximum,
            "center": None,
            "score_possible": 0.0,
            "score_awarded": 0.0,
            "message": "Rule range is invalid.",
        }

    center = minimum + ((maximum - minimum) / 2.0)
    half_range = max((maximum - minimum) / 2.0, 0.000001)
    distance_ratio = abs(float(reading) - center) / half_range
    score_awarded = possible * (1.0 - distance_ratio)

    # Keep very bad out-of-range readings from destroying the total ranking,
    # while still making them count as a real negative match.
    score_awarded = max(-possible, min(possible, score_awarded))
    score_awarded = round(score_awarded, 4)
    in_range = minimum < float(reading) < maximum

    if abs(float(reading) - minimum) < 0.000001 or abs(float(reading) - maximum) < 0.000001:
        message = "On the range boundary; this factor gives 0 score."
    elif score_awarded > 0:
        message = "Near the ideal middle of the saved range."
    elif score_awarded == 0:
        message = "At the edge of the saved range."
    else:
        message = "Outside the saved range; this factor gives a negative score."

    return {
        "ok": in_range and score_awarded > 0,
        "reading": reading,
        "min": minimum,
        "max": maximum,
        "center": round(center, 4),
        "score_possible": float(possible),
        "score_awarded": float(score_awarded),
        "score_ratio": round(score_awarded / possible, 4) if possible else 0.0,
        "message": message,
    }
