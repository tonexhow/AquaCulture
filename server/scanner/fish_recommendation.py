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
import math
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
    sensor_module = None  # type: ignore


# MQ-137 ammonia is GAS and counts only toward hardware availability.
# The species ammonia rule is for WATER; it cannot be scored from MQ-137 output.
WATER_KEYS = ("temperature", "ph", "oxygen", "turbidity", "ammonia")
RULE_KEYS = ("temperature", "ph", "oxygen", "turbidity")
DEFAULT_TOP = 3
MAX_TOP = 10
STABILITY_ABSOLUTE_TOLERANCE = {
    "temperature": 0.75, "ph": 0.3, "oxygen": 0.75,
    "turbidity": 5.0, "ammonia": 2.0,
}
MINIMUM_RECOMMENDATION_SENSOR_COUNT = 3
SENSOR_LABELS = {
    "temperature": "Temperature",
    "ph": "pH",
    "oxygen": "Oxygen",
    "turbidity": "Turbidity",
    "ammonia": "Ammonia gas (MQ-137)",
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
        number = float(value)
        return number if math.isfinite(number) else None
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
        "water_parameter_count": sum(key in available for key in RULE_KEYS),
        "comparable_water_parameters": [key for key in RULE_KEYS if key in available],
        "operational_sensor_coverage": f"{len(available)}/{len(WATER_KEYS)}",
        "accuracy_note": (
            "Hardware coverage and scientifically comparable water parameters are separate. "
            "MQ-137 measures gas and does not contribute to water suitability. "
            "Calibration and repeated samples are required for reliable recommendations."
        ),
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


def scan_water_recommendation(
    session_id: Optional[int] = None,
    duration_seconds: float = 5.0,
    top: int = DEFAULT_TOP,
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

    duration_seconds = max(3.0, min(5.0, float(duration_seconds or 5.0)))
    top = max(1, min(MAX_TOP, _safe_int(top, DEFAULT_TOP)))
    sample_gap_seconds = max(0.2, float(sample_gap_seconds or 1.0))

    try:
        if sensor_module is None:
            return _finish_with_error("Sensor module is unavailable.")

        _push_event(event_callback, {"type": "water", "message": "Gathering water readings.", "progress": 10})

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

        return {"ok": True, "message": "Water recommendation completed.", "result": result}
    except Exception as error:
        return _finish_with_error("Water recommendation failed safely.", _safe_error(error))


def _finish_with_error(message: str, error: Any = None) -> Dict[str, Any]:
    payload = {"ok": False, "message": message, "error": error, "timestamp": _now_iso()}
    _set_state(running=False, cancel_requested=False, message=message, progress=0, last_result=payload)
    return payload


def test_recommendation(values: Dict[str, Any], top: int = DEFAULT_TOP) -> Dict[str, Any]:
    """
    Used by /api/test/recommendation.
    Does not save to water_reading database.

    Every sensor is allowed to be absent during prototype/testing, but the
    recommendation engine needs at least 3 valid operational sensor channels
    out of 5. MQ-137 gas may meet hardware coverage, but its gas reading
    never enters the weighted dissolved-water suitability score.
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


def recommend_from_values(values: Dict[str, Any], top: int = DEFAULT_TOP) -> Dict[str, Any]:
    """Alias for test/manual use."""
    return test_recommendation(values, top=top)


def recommend_from_readings(
    readings: List[Dict[str, Any]],
    top: int = DEFAULT_TOP,
    save: bool = False,
    session_id: Optional[int] = None,
) -> Dict[str, Any]:
    clean_readings = [normalize_reading(row) for row in readings]
    clean_readings = [row for row in clean_readings if any(row.get(key) is not None for key in WATER_KEYS)]

    if not clean_readings:
        return {"ok": False, "message": "No valid readings are available for recommendation.", "readings": []}

    analytics = analyze_water_readings(clean_readings)
    # Use an average computed AFTER removing isolated spikes, not the last frame.
    final_reading = analytics["average"]
    availability = _availability_payload(final_reading)

    if availability["available_sensor_count"] < MINIMUM_RECOMMENDATION_SENSOR_COUNT:
        payload = _not_enough_sensor_payload(final_reading, clean_readings)
        payload["summary"] = analytics
        return payload

    species_rows = load_species_rows()
    suggestions = score_species(final_reading, species_rows)
    if not suggestions:
        return {"ok": False, "message": "No species has usable, scientifically comparable water rules for these measurements.",
                "summary": analytics, "reading": final_reading, **availability}
    top = max(1, min(MAX_TOP, _safe_int(top, DEFAULT_TOP)))
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
                # Legacy ammonia column is dissolved-water chemistry and must
                # remain NULL for an MQ-137 gas sample.
                "ammonia": None,
                "ammonia_gas_ppm": final_reading.get("ammonia"),
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
        "final_reading": {**final_reading, "ammonia_gas_ppm": final_reading.get("ammonia"), "ammonia_medium": "gas"},
        "calibration_note": "MQ-137 readings are gas-phase only; no unvalidated numerical gas penalty is applied.",
        "stability_warning": analytics.get("warning_message", ""),
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
            "turbidity": (0.0, 5000.0),
            "ammonia": (0.0, 1000.0),
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
        "ammonia": valid("ammonia", pick("ammonia_gas_ppm", "ammonia", "ammonia_ppm", "nh3")),
        "timestamp": row.get("timestamp") or _now_iso(),
    }


def analyze_water_readings(readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Filter isolated spikes per channel and report stability/coverage.

    A 3-5-second window can contain too few samples to establish stability,
    especially because the Nano currently publishes every 2 seconds. This
    uncertainty is reported rather than silently claiming a stable signal.
    """
    averages: Dict[str, Optional[float]] = {}
    medians: Dict[str, Optional[float]] = {}
    ranges: Dict[str, Dict[str, Optional[float]]] = {}
    spikes: List[Dict[str, Any]] = []
    stability: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []

    for key in WATER_KEYS:
        values = [_safe_float(row.get(key)) for row in readings]
        values = [value for value in values if value is not None]
        if not values:
            averages[key] = medians[key] = None
            ranges[key] = {"min": None, "max": None}
            stability[key] = {"status": "missing", "sample_count": 0, "rejected_spikes": 0}
            continue

        median = statistics.median(values)
        deviations = [abs(v - median) for v in values]
        mad = statistics.median(deviations)
        # Keep channel-specific minimum tolerances; a single offset sample
        # cannot be treated as a shift in an otherwise consistent signal.
        tolerance = max(
            STABILITY_ABSOLUTE_TOLERANCE[key],
            abs(median) * (0.15 if key == "turbidity" else 0.10),
            3.0 * 1.4826 * mad,
        )
        retained = [v for v in values if len(values) < 3 or abs(v - median) <= tolerance]
        rejected = len(values) - len(retained)
        if rejected:
            spikes.append({
                "key": key, "rejected_count": rejected,
                "sample_count": len(values), "message": f"Rejected {rejected} isolated {key} spike(s).",
            })
        if not retained:
            # Defensive fallback only; a true set of >=3 finite readings has
            # its median within the tolerance by construction.
            retained = [median]

        averages[key] = round(statistics.mean(retained), 4)
        medians[key] = round(statistics.median(retained), 4)
        ranges[key] = {"min": round(min(retained), 4), "max": round(max(retained), 4)}
        spread = max(retained) - min(retained)
        if len(retained) < 3:
            state = "insufficient_samples"
            warnings.append(f"{SENSOR_LABELS[key]}: fewer than 3 retained samples; stability not established.")
        elif spread > max(STABILITY_ABSOLUTE_TOLERANCE[key] * 2, abs(median) * 0.15):
            state = "unstable"
            warnings.append(f"{SENSOR_LABELS[key]}: readings vary; verify sensor stability.")
        else:
            state = "stable"
        stability[key] = {
            "status": state, "sample_count": len(retained),
            "received_count": len(values), "rejected_spikes": rejected,
            "spread": round(spread, 4),
        }

    availability = _availability_payload(averages)
    warnings.extend(item["message"] for item in spikes)
    return {
        "count": len(readings), "average": averages, "median": medians,
        "range": ranges, "stability": stability,
        "unstable": bool(warnings), "spike_warnings": spikes,
        "warnings": warnings, "warning_message": " ".join(warnings),
        **availability,
    }


def detect_spike(values: List[float], key: str) -> Optional[Dict[str, Any]]:
    """Backward-compatible helper used by older diagnostics."""
    if len(values) < 3 or key not in STABILITY_ABSOLUTE_TOLERANCE:
        return None
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    threshold = max(STABILITY_ABSOLUTE_TOLERANCE[key], abs(median) * 0.1, 3 * 1.4826 * mad)
    count = sum(abs(value - median) > threshold for value in values)
    return {"key": key, "rejected_count": count, "message": f"Isolated {key} spike detected."} if count else None


def load_species_rows() -> List[Dict[str, Any]]:
    if db is None:
        return []
    try:
        return db.fetch_many("species", order_by="id ASC")
    except Exception:
        return []


def score_species(final_reading: Dict[str, Any], species_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Weighted RBS = sum(valid comparable parameter weight * suitability) / sum(valid weights).

    MQ-137 gas is hardware coverage only, never used to score a dissolved-
    ammonia species rule. Missing or invalid rules are excluded, not zeroed.
    """
    suggestions: List[Dict[str, Any]] = []
    operational = set(_available_sensor_keys(final_reading))
    for row in species_rows:
        weighted_score = 0.0
        used_weight = 0.0
        all_rule_weight = 0.0
        matches: Dict[str, Dict[str, Any]] = {}
        critical_warnings: List[str] = []
        parameter_warnings: List[str] = []
        used: List[str] = []
        priority_column = None
        priority_score = -1.0

        for key in RULE_KEYS:
            value = _safe_float(final_reading.get(key))
            rule = parse_rule_array(row.get(key))
            detail = score_range_rule(value, rule)
            weight = detail["score_possible"] if detail.get("rule_valid") else 0.0
            detail["available"] = value is not None
            detail["ignored"] = value is None or not detail.get("rule_valid", False)
            detail["rule_weight"] = weight
            matches[key] = detail
            all_rule_weight += weight
            if value is None or not detail.get("rule_valid"):
                continue

            used.append(key)
            weighted_score += detail["score_awarded"]
            used_weight += weight
            if detail["score_awarded"] > priority_score:
                priority_score, priority_column = detail["score_awarded"], key

            if key == "oxygen" and value < detail["min"]:
                critical_warnings.append(
                    f"Dissolved oxygen {value:g} mg/L is below the saved minimum "
                    f"{detail['min']:g} mg/L for this species; possible survival risk."
                )
            elif detail.get("outside_range"):
                parameter_warnings.append(
                    f"{SENSOR_LABELS[key]} {value:g} is outside the saved species range "
                    f"{detail['min']:g}-{detail['max']:g}."
                )

        if used_weight <= 0:
            # Never rank a catalog entry for which no water parameter can be compared.
            continue

        if "oxygen" not in used:
            parameter_warnings.append("Dissolved oxygen was not evaluated; survival safety is unverified.")
        if "ammonia" in operational:
            parameter_warnings.append("MQ-137 ammonia gas excluded from water compatibility and risk scoring.")
        percentage = round(100.0 * weighted_score / used_weight, 2)
        confidence_percentage = round(100.0 * used_weight / all_rule_weight, 2) if all_rule_weight > 0 else 0.0
        missing_possible = max(0.0, all_rule_weight - used_weight)
        safety_status = "critical" if critical_warnings else "unverified" if parameter_warnings else "within_measured_ranges"

        suggestions.append({
            "id": row.get("id"), "name": row.get("name"),
            "score": round(weighted_score, 2), "max_score": round(used_weight, 2),
            "total_rule_score": round(all_rule_weight, 2),
            "percentage": percentage, "overall_percentage": percentage,
            "confidence_percentage": confidence_percentage,
            "missing_dependency_percentage": round(100 * missing_possible / all_rule_weight, 2) if all_rule_weight else 0.0,
            "priority_column": priority_column, "priority_score": round(max(0.0, priority_score), 2),
            "used_sensors": used,
            "ignored_sensors": [key for key in WATER_KEYS if key not in used],
            "operational_sensor_count": len(operational),
            "compared_water_parameter_count": len(used),
            "critical_warnings": critical_warnings, "parameter_warnings": parameter_warnings,
            "safety_status": safety_status, "gas_risk_applied": False,
            "matches": matches,
            "environment": parse_rule_array(row.get("environment")),
            "regulation": parse_rule_array(row.get("regulation")),
            "ecology": parse_rule_array(row.get("ecology")),
            "safety": parse_rule_array(row.get("safety")),
            "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
            "has_image": bool(row.get("image")),
            "image_url": f"/api/species/{row.get('id')}/image" if row.get("id") else "",
        })

    # Critical conditions must not silently outrank an otherwise viable option.
    suggestions.sort(
        key=lambda item: (
            not bool(item["critical_warnings"]),
            item["percentage"], item["confidence_percentage"], item["score"],
        ), reverse=True,
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
    """Center-distance suitability curve in [0, 100].

    E.g. 21-32 degrees, reading 29.5: (1 - 3 / 5.5) * 100 = 45.45%.
    Outside range clamps to zero; it cannot make a weighted RBS negative.
    """
    if len(rule) < 3:
        return {"ok": False, "rule_valid": False, "reading": reading,
                "score_possible": 0.0, "score_awarded": 0.0,
                "message": "Species rule missing; excluded from scoring."}
    minimum, maximum, weight = (_safe_float(x) for x in rule[:3])
    if minimum is None or maximum is None or weight is None or maximum <= minimum or weight <= 0:
        return {"ok": False, "rule_valid": False, "reading": reading,
                "score_possible": 0.0, "score_awarded": 0.0,
                "message": "Species rule invalid; excluded from scoring."}
    center = (minimum + maximum) / 2.0
    half_range = (maximum - minimum) / 2.0
    detail = {"rule_valid": True, "reading": reading, "min": minimum,
              "max": maximum, "center": round(center, 4),
              "score_possible": float(weight)}
    if reading is None:
        return {**detail, "ok": False, "score_awarded": 0.0, "suitability_percentage": None,
                "message": "Missing reading; excluded from weighted mean."}
    value = _safe_float(reading)
    if value is None:
        return {**detail, "ok": False, "score_awarded": 0.0, "suitability_percentage": None,
                "message": "Invalid reading; excluded from weighted mean."}
    suitability = max(0.0, min(100.0, 100.0 * (1.0 - abs(value - center) / half_range)))
    awarded = float(weight) * suitability / 100.0
    outside = value < minimum or value > maximum
    return {**detail, "ok": not outside and suitability > 0,
            "outside_range": outside, "score_awarded": round(awarded, 5),
            "score_ratio": round(suitability / 100.0, 6),
            "suitability_percentage": round(suitability, 2),
            "message": "Outside species tolerance." if outside else "Center-distance water suitability."}
