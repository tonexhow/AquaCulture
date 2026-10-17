"""
AquaCulture - scanner/size_detection.py

Purpose:
- Runs visual size prediction only when requested by the user.
- Uses the shared live_streaming.py manager for snapshot gathering.
- Tries ultrasonic distance per snapshot through sensors.py, but never requires it.
- Uses YOLO model files from models/ when ultralytics is installed.
- Falls back to OpenCV contour detection when YOLO/model is unavailable.
- Saves prediction ZIP files to database/media/predictions; SQLite stores relative paths.

Architecture:
PHASE 1: RECORD VIDEO FRAMES
- Sample the existing camera feed for the user-selected recording duration.
- Store temporary JPEG bytes + timestamp + frame number + optional distance_cm.

PHASE 2: SELECT FRAMES
- Shortlist the best 5 or 10 readable frames using quality/exposure/duplicates.

PHASE 3: ANALYTICS (separately timed, background HTTP job)
- Detect aquatic targets using the isolated NCNN subprocess.
- Draw boxes/crops.
- Estimate size approximately.
- Package prediction_info.txt, result.txt, result.json, images, and crops into a ZIP.
- Save ZIP to the managed media folder; store its relative path in SQLite.

Important:
- The model is NOT continuous.
- Prediction runs only when start_prediction(...) is called.
- Estimates are approximate only.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


try:
    from openpyxl import Workbook  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Workbook = None  # type: ignore

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    np = None  # type: ignore

# IMPORTANT:
# Do NOT import Ultralytics/Torch at module import time on Raspberry Pi.
# Some Torch wheels can terminate Python with SIGILL (illegal instruction).
# When that happens Flask cannot catch the error and /api/test/visual/upload
# returns an empty reply / 502 through Cloudflare.
#
# YOLO is now loaded lazily only when it is considered safe. On Raspberry Pi,
# it is disabled by default and OpenCV fallback is used instead. Set
# AQUACULTURE_FORCE_YOLO=1 only after installing a Pi-compatible Torch build.
YOLO = None  # type: ignore
_YOLO_IMPORT_ATTEMPTED = False
_YOLO_IMPORT_ERROR: Optional[str] = None


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _runtime_is_raspberry_pi() -> bool:
    try:
        model_text = Path("/proc/device-tree/model").read_text(errors="ignore").lower()
        if "raspberry pi" in model_text:
            return True
    except Exception:
        pass
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    return machine in {"armv7l", "aarch64", "arm64"}


def _get_yolo_class() -> Any:
    """Safely returns the Ultralytics YOLO class or None.

    On Raspberry Pi, importing torch/ultralytics can kill the interpreter with
    SIGILL if the wheel is incompatible. Because SIGILL cannot be caught by
    normal try/except, we do not attempt the import on Pi unless explicitly
    forced by AQUACULTURE_FORCE_YOLO=1.
    """
    global YOLO, _YOLO_IMPORT_ATTEMPTED, _YOLO_IMPORT_ERROR

    if _truthy_env("AQUACULTURE_DISABLE_YOLO"):
        _YOLO_IMPORT_ERROR = "YOLO is disabled by AQUACULTURE_DISABLE_YOLO=1; OpenCV fallback will be used."
        return None

    if _runtime_is_raspberry_pi() and not _truthy_env("AQUACULTURE_FORCE_YOLO"):
        _YOLO_IMPORT_ERROR = (
            "YOLO/Torch is disabled by default on Raspberry Pi to prevent illegal-instruction crashes. "
            "OpenCV fallback will be used. Set AQUACULTURE_FORCE_YOLO=1 only after installing a Pi-compatible Torch build."
        )
        return None

    if YOLO is not None:
        return YOLO
    if _YOLO_IMPORT_ATTEMPTED:
        return YOLO

    _YOLO_IMPORT_ATTEMPTED = True
    try:
        from ultralytics import YOLO as _YOLO  # type: ignore
        YOLO = _YOLO  # type: ignore
        _YOLO_IMPORT_ERROR = None
        return YOLO
    except BaseException as error:  # pragma: no cover - optional dependency/native crash protection
        YOLO = None  # type: ignore
        _YOLO_IMPORT_ERROR = f"Ultralytics/YOLO import failed: {type(error).__name__}: {error}"
        return None

try:
    from database.database import db
except Exception:  # pragma: no cover - database might not exist yet during early setup
    db = None  # type: ignore

try:
    import live_streaming as camera_module
except Exception:  # pragma: no cover
    camera_module = None  # type: ignore

try:
    import sensors as sensor_module
except Exception:  # pragma: no cover
    sensor_module = None  # type: ignore


BASE_DIR = Path(__file__).resolve().parent
ML_DIR = Path(__file__).resolve().parents[2] / "models"

# ML flow, current recommended path:
# 1) Marine NCNN model folders first. These are Raspberry Pi friendly exports.
# 2) Optional raw .pt models only when explicitly enabled.
# 3) OpenCV fallback only when no model can return boxes.
#
# The NCNN models are run in a separate Python worker process so the Flask
# backend does not die even if an ML runtime crashes. This is intentionally
# slower but much safer on Raspberry Pi.
MARINE_NCNN_MODEL_PRIORITY = [
    ML_DIR / "FishInv_ncnn_model",
    ML_DIR / "MegaFauna_ncnn_model",
    ML_DIR / "marine" / "FishInv_ncnn_model",
    ML_DIR / "marine" / "MegaFauna_ncnn_model",
]

# Raw PyTorch models are not recommended on Raspberry Pi because incompatible
# Torch wheels can terminate Python with SIGILL. Keep these only for laptop
# testing or when AQUACULTURE_FORCE_PT_YOLO=1 is set.
PT_MODEL_PRIORITY = [
    ML_DIR / "model-1.pt",       # OBSEA fish detector
    ML_DIR / "model-2.pt",       # Marine Detect FishInv
    ML_DIR / "model-3.pt",       # Marine Detect MegaFauna
    ML_DIR / "aquatic-yolo.pt",  # optional custom model
    ML_DIR / "yolo26n-seg.pt",
    ML_DIR / "yolo26n.pt",
    ML_DIR / "yolo26n-obb.pt",
]

# Kept for compatibility with the old in-process YOLO path.
MODEL_PRIORITY = PT_MODEL_PRIORITY

# Keep Ultralytics local/cache files inside the project when possible.
# This avoids OS-specific permission issues on Windows/Raspberry Pi deployments.
try:
    os.environ.setdefault("YOLO_CONFIG_DIR", str(BASE_DIR / ".yolo_config"))
    (BASE_DIR / ".yolo_config").mkdir(parents=True, exist_ok=True)
except Exception:
    pass

SPECIALIZED_MODEL_ROLES = {
    "model-1.pt": "fish",
    "model-2.pt": "aquatic",
    "model-3.pt": "megafauna",
    "aquatic-yolo.pt": "aquatic",
    "fishinv_ncnn_model": "aquatic",
    "megafauna_ncnn_model": "megafauna",
}

GENERAL_MODEL_NAMES = {
    "yolo26n-seg.pt",
    "yolo26n.pt",
    "yolo26n-obb.pt",
}

TARGET_CLASS_ALIASES = {
    "bolbometopon muricatum": "fish",
    "bolbometopon_muricatum": "fish",
    "chaetodontidae": "fish",
    "cheilinus undulatus": "fish",
    "cheilinus_undulatus": "fish",
    "cromileptes altivelis": "fish",
    "cromileptes_altivelis": "fish",
    "haemulidae": "fish",
    "lutjanidae": "fish",
    "muraenidae": "fish",
    "scaridae": "fish",
    "serranidae": "fish",
    "giant clam": "clam",
    "giant_clam": "clam",
    "sea cucumber": "sea cucumber",
    "sea_cucumber": "sea cucumber",
    "urchin": "urchin",
    "crown of thorns": "starfish",
    "crown_of_thorns": "starfish",
    "marine life": "marine life",
    "marine life candidate": "marine life",
    "fish": "fish",
    "goldfish": "fish",
    "shark": "shark",
    "whale shark": "shark",
    "ray": "ray",
    "stingray": "ray",
    "manta ray": "ray",
    "turtle": "turtle",
    "sea turtle": "turtle",
    "crocodile": "crocodile",
    "alligator": "crocodile",
    "jellyfish": "jellyfish",
    "jelly fish": "jellyfish",
    "coral": "coral",
    "corals": "coral",
    "octopus": "octopus",
    "squid": "squid",
    "crab": "crab",
    "lobster": "lobster",
    "shrimp": "shrimp",
    "seahorse": "seahorse",
    "sea horse": "seahorse",
    "starfish": "starfish",
    "sea star": "starfish",
    "clam": "clam",
    "oyster": "oyster",
    "aquatic plant": "aquatic plant",
    "water plant": "aquatic plant",
    "plant": "aquatic plant",
}

# These common classes must never be treated as aquatic detections.
# If a YOLO model confidently reports only these classes, OpenCV fallback is blocked
# to avoid turning a person/phone/chair into "unknown aquatic object".
IGNORED_CLASSES = {
    "person", "human", "man", "woman", "child",
    "ball", "sports ball", "chair", "bench", "table", "desk",
    "bottle", "cup", "cell phone", "phone", "mobile phone",
    "car", "bus", "truck", "motorcycle", "bicycle",
    "dog", "cat", "horse", "cow", "sheep",
    "keyboard", "mouse", "laptop", "tv", "television", "remote",
    "book", "clock", "bag", "backpack", "umbrella",
}

# Note: "bird" is intentionally not a hard blocker. Many fish/turtle/pond
# images are misread by general COCO-style models as bird, which caused real
# aquatic test images to be skipped before the aquatic models could help.

# Used only when ultrasonic distance is unavailable. This keeps laptop/test mode useful.
# The result is explicitly marked as a visual-only estimate, not a measured size.
DEFAULT_VISUAL_FRAME_WIDTH_CM = 45.0
MIN_VISUAL_CM_PER_PX = 0.015
MAX_VISUAL_CM_PER_PX = 0.35

# Optional reference marker for better size estimates.
# Print an ArUco 4x4 marker and measure its real side length.
# Default is 5 cm because it is easy to print and place near the pond/tank.
ARUCO_MARKER_REAL_SIZE_CM = 5.0
ARUCO_DICT_NAME = "DICT_4X4_50"

# Keep inference small enough for laptop/Raspberry Pi testing. The model files
# still work at higher resolution later, but 640 is safer while developing.
YOLO_IMAGE_SIZE = 640
SPECIALIZED_YOLO_IMAGE_SIZE = 640
MAX_DETECTIONS_PER_IMAGE = 30

# Test mode should be marine-model first. OpenCV fallback is disabled by default
# so the UI does not show generic "unknown aquatic object" boxes for non-marine images.
# Set AQUACULTURE_ALLOW_OPENCV_FALLBACK=1 only for emergency/no-model demos.
# Set AQUACULTURE_ALLOW_UNKNOWN_AQUATIC=1 only if you intentionally want generic fallback labels.

# Before accepting fish/aquatic predictions, run a small general-model guard
# when available. This prevents a person/phone/chair from being accepted as fish
# by an aquatic-only model that has no non-fish classes.
NON_AQUATIC_GUARD_CONFIDENCE = 0.45
NON_AQUATIC_GUARD_MIN_AREA_RATIO = 0.015

_last_yolo_meta: Dict[str, Any] = {}


@dataclass
class PredictionObject:
    image_id: str
    object_id: str
    class_name: str
    confidence: float
    distance_cm: Optional[float]
    box_px: List[int]
    estimated_length_cm: Optional[float]
    estimated_width_cm: Optional[float]
    estimated_height_cm: Optional[float]
    estimated_weight_g: Optional[float]
    crop_path: str = ""
    estimate_method: str = ""
    estimate_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_id": self.image_id,
            "object_id": self.object_id,
            "class": self.class_name,
            "confidence": round(float(self.confidence), 4),
            "distance_cm": self.distance_cm,
            "box_px": self.box_px,
            "estimated_length_cm": self.estimated_length_cm,
            "estimated_width_cm": self.estimated_width_cm,
            "estimated_height_cm": self.estimated_height_cm,
            "estimated_weight_g": self.estimated_weight_g,
            "crop_path": self.crop_path,
            "estimate_method": self.estimate_method,
            "estimate_note": self.estimate_note,
        }


@dataclass
class PredictionState:
    running: bool = False
    cancel_requested: bool = False
    current_prediction_id: Optional[int] = None
    job_id: Optional[str] = None
    stage: str = "idle"
    stage_progress: int = 0
    message: str = "Prediction is idle."
    started_at: Optional[str] = None
    progress: int = 0
    duration_seconds: float = 0.0
    recorded_seconds: float = 0.0
    snapshots_captured: int = 0
    frames_total: int = 0
    frames_done: int = 0
    frames: List[Dict[str, Any]] = field(default_factory=list)
    best_frame_id: Optional[str] = None
    last_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": True, "running": self.running,
            "cancel_requested": self.cancel_requested,
            "current_prediction_id": self.current_prediction_id,
            "job_id": self.job_id, "stage": self.stage,
            "stage_progress": self.stage_progress,
            "message": self.message, "started_at": self.started_at,
            "progress": self.progress,
            "duration_seconds": self.duration_seconds,
            "recorded_seconds": self.recorded_seconds,
            "snapshots_captured": self.snapshots_captured,
            "frames_total": self.frames_total, "frames_done": self.frames_done,
            "frames": [dict(frame) for frame in self.frames],
            "best_frame_id": self.best_frame_id,
            "last_result": self.last_result,
        }


_state = PredictionState()
_state_lock = threading.RLock()

# Loaded model cache. A true waterfall is used during analysis:
# model-1 -> model-2 -> model-3 -> aquatic-yolo -> yolo26 fallbacks -> OpenCV.
_loaded_model: Any = None  # legacy compatibility: first loaded model
_loaded_model_path: Optional[Path] = None
_loaded_models: Dict[str, Any] = {}
_model_load_errors: Dict[str, str] = {}
_model_error: Optional[str] = None


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


def get_prediction_status() -> Dict[str, Any]:
    with _state_lock:
        return _state.to_dict()


def cancel_prediction() -> Dict[str, Any]:
    with _state_lock:
        if not _state.running:
            return {"ok": True, "message": "No prediction is currently running.", "running": False}
        _state.cancel_requested = True
        _state.message = "Cancel requested. Stopping recording or current model inference."
        return _state.to_dict()


def _is_cancel_requested() -> bool:
    with _state_lock:
        return _state.cancel_requested


def _push_event(event_callback: Optional[Callable[[Dict[str, Any]], None]], event: Dict[str, Any]) -> None:
    if event_callback:
        try:
            event_callback(event)
        except Exception:
            pass


def _set_state(**kwargs: Any) -> None:
    with _state_lock:
        for key, value in kwargs.items():
            if hasattr(_state, key):
                setattr(_state, key, value)


def _begin_prediction(duration_seconds: float) -> Optional[str]:
    """Reserve the only scanner slot before starting a background thread."""
    with _state_lock:
        if _state.running:
            return None
        job_id = uuid.uuid4().hex
        _state.running = True
        _state.cancel_requested = False
        _state.current_prediction_id = None
        _state.job_id = job_id
        _state.stage = "recording"
        _state.stage_progress = 0
        _state.started_at = _now_iso()
        _state.message = "Recording camera frames."
        _state.progress = 0
        _state.duration_seconds = duration_seconds
        _state.recorded_seconds = 0.0
        _state.snapshots_captured = 0
        _state.frames_total = 0
        _state.frames_done = 0
        _state.frames = []
        _state.best_frame_id = None
        _state.last_result = None
        return job_id


def _stage_update(
    callback: Optional[Callable[[Dict[str, Any]], None]],
    stage: str, message: str, progress: int, stage_progress: int = 0, **values: Any,
) -> None:
    progress = max(0, min(100, int(progress)))
    stage_progress = max(0, min(100, int(stage_progress)))
    _set_state(stage=stage, message=message, progress=progress,
               stage_progress=stage_progress, **values)
    # Socket events are small and never contain frame images. Authenticated
    # clients retrieve previews/results from the prediction/status endpoint.
    _push_event(callback, {"type": "prediction", "job_id": _state.job_id,
                           "stage": stage, "message": message,
                           "progress": progress, "stage_progress": stage_progress})


def start_prediction_background(
    session_id: Optional[int] = None,
    duration_seconds: float = 5.0,
    top: int = 5,
    target_fps: float = 4.0,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Return immediately; status polling survives reverse-proxy request timeouts."""
    duration_seconds = max(1.0, min(10.0, float(duration_seconds or 5.0)))
    job_id = _begin_prediction(duration_seconds)
    if job_id is None:
        return {"ok": False, "message": "A visual scan is already running.",
                "status": get_prediction_status()}

    def run_background() -> None:
        _execute_prediction(job_id, session_id, duration_seconds, top,
                            target_fps, event_callback)

    try:
        threading.Thread(target=run_background, daemon=True,
                         name=f"aquaculture-visual-{job_id[:8]}").start()
    except Exception as error:
        return _finish_with_error("Could not launch visual scan.",
                                  _safe_error(error), event_callback=event_callback)
    return {"ok": True, "accepted": True, "job_id": job_id,
            "message": "Recording started. AI processing will continue after the recording.",
            "status": get_prediction_status()}


def start_prediction(
    session_id: Optional[int] = None,
    duration_seconds: float = 5.0,
    top: int = 5,
    target_fps: float = 4.0,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Synchronous compatibility entry point for offline tests and tools."""
    duration_seconds = max(1.0, min(10.0, float(duration_seconds or 5.0)))
    job_id = _begin_prediction(duration_seconds)
    if job_id is None:
        return {"ok": False, "message": "A visual scan is already running.",
                "status": get_prediction_status()}
    return _execute_prediction(job_id, session_id, duration_seconds,
                               top, target_fps, event_callback)


def _frame_preview(image_bytes: bytes, size: int = 220) -> str:
    """Bounded thumbnail for status polling, not an archive of raw video."""
    if not image_bytes or cv2 is None or np is None:
        return ""
    try:
        image = _decode_image(image_bytes)
        if image is None:
            return ""
        height, width = image.shape[:2]
        scale = min(1.0, size / max(1.0, float(max(width, height))))
        if scale < 1:
            image = cv2.resize(image, (max(1, int(width * scale)),
                                       max(1, int(height * scale))),
                               interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 62])
        return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii") if ok else ""
    except Exception:
        return ""


def _summarize_frame(record: Dict[str, Any], *, selected: bool = False) -> Dict[str, Any]:
    return {
        "image_id": _safe_image_id(str(record.get("selected_id") or f"frame-{record.get('frame_number') or 0}"), 0),
        "frame_number": record.get("frame_number"),
        "quality_score": round(float(record.get("quality_score") or 0), 1),
        "selected": selected,
        "status": "queued" if selected else "discarded",
        "object_count": 0,
        "confidence": None,
        "preview": _frame_preview(record.get("image_bytes") or b""),
    }


def _best_frame_id(images: List[Dict[str, Any]], selected: List[Dict[str, Any]]) -> Optional[str]:
    """Prefer confirmed aquatic detection, then confidence and image quality."""
    if not images:
        return None
    qualities = {_safe_image_id(str(r.get("selected_id")), 0): float(r.get("quality_score") or 0)
                 for r in selected}
    best = max(images, key=lambda r: (
        int(r.get("object_count") or 0) > 0,
        max((float(o.get("confidence") or 0) for o in (r.get("objects") or [])), default=0),
        int(r.get("object_count") or 0),
        qualities.get(str(r.get("image_id")), 0),
    ))
    return str(best.get("image_id") or "") or None


def _execute_prediction(
    job_id: str, session_id: Optional[int], duration_seconds: float,
    top: int, target_fps: float,
    event_callback: Optional[Callable[[Dict[str, Any]], None]],
) -> Dict[str, Any]:
    started_time = time.monotonic()
    top = max(1, min(10, _safe_int(top, 5)))
    # Sample more often than the desired short list. This is cached camera video
    # frames, not repeated heavy image uploads or a separate camera owner.
    target_fps = max(1.0, min(10.0, float(target_fps or 4.0)))

    try:
        if camera_module is None:
            return _finish_with_error("Camera module is unavailable.", event_callback=event_callback)

        active_sensor_manager = getattr(sensor_module, "sensor_manager", None) if sensor_module else None
        try:
            camera_before = camera_module.get_status() if hasattr(camera_module, "get_status") else {}
        except Exception:
            camera_before = {}

        # The chosen 3/5/10 seconds apply ONLY to this camera recording stage.
        # Inference can take minutes on Pi 4B and must use its own deadline.
        def recording_progress(elapsed: float, count: int) -> None:
            pct = min(100, int(100 * elapsed / duration_seconds))
            _stage_update(event_callback, "recording",
                          f"Recording {min(elapsed, duration_seconds):.1f}s / {duration_seconds:g}s",
                          min(30, round(30 * pct / 100)), pct,
                          recorded_seconds=round(min(elapsed, duration_seconds), 2),
                          snapshots_captured=count)

        snapshots = camera_module.capture_burst(
            duration_seconds=duration_seconds,
            target_fps=max(target_fps, (top * 2) / duration_seconds),
            sensor_manager=active_sensor_manager,
            cancel_checker=_is_cancel_requested,
            progress_callback=recording_progress,
        )
        capture_elapsed = time.monotonic() - started_time
        if _is_cancel_requested():
            return _finish_with_error("Fish scan cancelled.", cancelled=True,
                                      event_callback=event_callback)
        if not snapshots:
            return _finish_with_error("Camera recorded no usable frames. Check camera connection.",
                                      event_callback=event_callback)

        _stage_update(event_callback, "selecting", "Assessing sharpness and lighting.",
                      32, 15, recorded_seconds=round(capture_elapsed, 2),
                      snapshots_captured=len(snapshots))
        selected = select_best_snapshots(snapshots, minimum=1, maximum=top)
        if not selected:
            return _finish_with_error("No readable frames were recorded.",
                                      event_callback=event_callback)
        selected_ids = {id(record) for record in selected}
        frames = [_summarize_frame(record, selected=id(record) in selected_ids)
                  for record in snapshots]
        # Frames dropped before inference are not deleted from a user's saved
        # recordings: only these temporary in-memory previews are discarded.
        _stage_update(event_callback, "selecting",
                      f"Selected {len(selected)} of {len(snapshots)} candidate frames.",
                      42, 100, frames=frames, frames_total=len(selected), frames_done=0)
        if _is_cancel_requested():
            return _finish_with_error("Fish scan cancelled.", cancelled=True,
                                      event_callback=event_callback)

        processing_timeout = max(120.0, min(900.0, float(_safe_int(
            os.environ.get("AQUACULTURE_VISUAL_PROCESS_TIMEOUT"), top * 60))))
        processing_deadline = time.monotonic() + processing_timeout
        _stage_update(event_callback, "predicting", "Analyzing frame 1 of " + str(len(selected)),
                      43, 0)

        def on_frame_processed(record: Dict[str, Any], processed: int, total: int,
                               boxed_bytes: Optional[bytes]) -> None:
            with _state_lock:
                for frame in _state.frames:
                    if frame.get("image_id") == str(record.get("image_id")):
                        frame["status"] = "done" if not record.get("worker_error") else "failed"
                        frame["object_count"] = int(record.get("object_count") or 0)
                        frame["confidence"] = max((float(obj.get("confidence") or 0)
                                                   for obj in record.get("objects") or []), default=None)
                        if boxed_bytes:
                            frame["preview"] = _frame_preview(boxed_bytes)
                        if record.get("warning"):
                            frame["warning"] = record["warning"]
                        break
            pct = min(100, int(100 * processed / max(1, total)))
            _stage_update(event_callback, "predicting",
                          f"Analyzed {processed} / {total} frames.",
                          43 + round(49 * pct / 100), pct, frames_done=processed)

        detection = analyze_selected_snapshots(
            selected, top=top, deadline=processing_deadline,
            cancel_checker=_is_cancel_requested, progress_callback=on_frame_processed,
        )
        if _is_cancel_requested():
            return _finish_with_error("Fish scan cancelled.", cancelled=True,
                                      event_callback=event_callback)
        if detection.get("timed_out"):
            return _finish_with_error(
                f"AI analysis timed out after up to {processing_timeout:g}s. "
                "Recording completed; try fewer frames or one model.",
                timed_out=True, event_callback=event_callback)
        if detection.get("model_failed"):
            return _finish_with_error("No AI model completed inference. Check NCNN runtime/errors.",
                                      error="; ".join(detection.get("model_errors") or [])[:700],
                                      event_callback=event_callback)
        if not detection.get("images"):
            return _finish_with_error("No frames completed image analysis.",
                                      event_callback=event_callback)

        image_records = detection.get("images") or []
        best_id = _best_frame_id(image_records, selected)
        with _state_lock:
            _state.best_frame_id = best_id
            for frame in _state.frames:
                if frame.get("selected") and frame.get("image_id") == best_id:
                    frame["status"] = "best"
                elif frame.get("selected"):
                    frame["status"] = "discarded"
        _stage_update(event_callback, "packaging", "Preparing final result and archive.",
                      94, 10, best_frame_id=best_id)

        try:
            camera_after = camera_module.get_status() if hasattr(camera_module, "get_status") else camera_before
        except Exception:
            camera_after = camera_before
        if not detection.get("objects"):
            # Even a legitimate empty-scene scan retains thumbnails and per-frame
            # counts. This is not a timeout or a bogus model success.
            result = {
                "ok": True, "objects": [], "images": [
                    {"image_id": r.get("image_id"), "frame_number": r.get("frame_number"),
                     "object_count": 0, "model_attempts": r.get("model_attempts") or [],
                     "raw_image_data_url": _frame_preview(
                         next((v.get("image_bytes") for v in selected
                               if v.get("selected_id") == r.get("image_id")), b""))}
                    for r in image_records],
                "object_count": 0, "snapshot_count": len(snapshots),
                "selected_snapshot_count": len(selected), "best_frame_id": best_id,
                "prediction_id": None, "download_available": False,
                "scan_limit_seconds": duration_seconds,
                "capture_duration": round(capture_elapsed, 2),
                "elapsed_seconds": round(time.monotonic() - started_time, 3),
                "detector_used": detection.get("detector_used"),
                "warnings": detection.get("model_errors") or [],
                "message": "No aquatic object detected in the analyzed frames.",
            }
        else:
            zip_bytes, result = build_prediction_zip(
                session_id=session_id,
                started_at=_state.started_at or _now_iso(),
                capture_duration=round(capture_elapsed, 2),
                snapshots=snapshots,
                selected_snapshots=selected,
                detection_result=detection,
                camera_status=camera_after or camera_before,
            )
            prediction_id: Optional[int] = None
            if db is not None:
                try:
                    prediction_id = db.insert_prediction_zip(zip_bytes, session_id=session_id)
                except Exception as error:
                    result["database_warning"] = (
                        f"Prediction archive was created but not saved: {_safe_error(error)}")
            result.update(prediction_id=prediction_id,
                          download_available=prediction_id is not None,
                          best_frame_id=best_id,
                          scan_limit_seconds=duration_seconds,
                          elapsed_seconds=round(time.monotonic() - started_time, 3),
                          message="Aquatic objects detected. Measurements are approximate.")

        _stage_update(event_callback, "complete", result["message"], 100, 100,
                      running=False, cancel_requested=False,
                      current_prediction_id=result.get("prediction_id"),
                      last_result=result, best_frame_id=best_id)
        return {"ok": True, "message": result["message"],
                "job_id": job_id, "prediction_id": result.get("prediction_id"),
                "result": result}
    except Exception as error:
        return _finish_with_error("Prediction failed safely.", error=_safe_error(error),
                                  event_callback=event_callback)


def _finish_with_error(
    message: str, error: Optional[str] = None, timed_out: bool = False,
    cancelled: bool = False,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    payload = {"ok": False, "message": message, "error": error,
               "timed_out": timed_out, "cancelled": cancelled,
               "timestamp": _now_iso()}
    stage = "cancelled" if cancelled else "failed"
    _stage_update(event_callback, stage, message, _state.progress,
                  _state.stage_progress, running=False,
                  cancel_requested=False, last_result=payload)
    return payload


def select_best_snapshots(
    snapshots: List[Dict[str, Any]],
    minimum: int = 5,
    maximum: int = 30,
    deadline: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Removes unusable frames and ranks remaining images by sharpness/brightness.
    If OpenCV is not installed, it still returns usable JPEG records in original order.
    """
    candidates: List[Tuple[float, int, Dict[str, Any]]] = []
    seen_hashes: set[str] = set()
    maximum = max(1, min(10, int(maximum)))

    for index, record in enumerate(snapshots):
        if deadline is not None and time.monotonic() >= deadline:
            break
        image_bytes = record.get("image_bytes")
        if not image_bytes:
            continue
        digest = hashlib.sha256(image_bytes).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        sharpness, brightness = 0.0, 128.0
        if cv2 is not None and np is not None:
            image = _decode_image(image_bytes)
            if image is None:
                continue
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            sharpness = max(0.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()))
            brightness = float(gray.mean())
        # Rank by sharpness/exposure, but never discard a readable blank frame:
        # white/empty scenes should finish as genuine 'no detection', not an error.
        sharp_score = min(70.0, 17.5 * math.log1p(sharpness))
        exposure_score = max(0.0, 30.0 - abs(brightness - 128.0) * 30.0 / 128.0)
        quality = round(sharp_score + exposure_score, 2)
        record["quality_score"] = quality
        candidates.append((quality, index, record))

    if not candidates:
        return []

    # Ranked best-to-worst; retain their original chronology after selection.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected = [record for _, _, record in candidates[:maximum]]
    selected.sort(key=lambda row: int(row.get("frame_number") or 0))
    for idx, row in enumerate(selected, start=1):
        row["selected_id"] = f"selected_{idx:03d}"
    return selected



def analyze_selected_snapshots(
    selected_snapshots: List[Dict[str, Any]], top: int = 5,
    deadline: Optional[float] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[Dict[str, Any], int, int, Optional[bytes]], None]] = None,
) -> Dict[str, Any]:
    """
    Analyze selected images and keep the result grouped by image.

    Waterfall rule:
    1. Try aquatic-focused YOLO models first (model-1, model-2, model-3).
    2. Try optional custom aquatic model.
    3. Try old yolo26 fallbacks only after aquatic models fail.
    4. Use OpenCV fallback only when YOLO does not confidently identify non-aquatic objects.
    """
    marine_ncnn_models = _available_marine_ncnn_models()
    use_marine_ncnn_worker = bool(marine_ncnn_models) and not _truthy_env("AQUACULTURE_DISABLE_MARINE_NCNN")
    pt_worker_models = [path for path in PT_MODEL_PRIORITY if path.is_file()] if _truthy_env("AQUACULTURE_FORCE_PT_YOLO") else []
    # A timed scan must not run PyTorch in Flask: it cannot be interrupted.
    # Both NCNN and explicitly enabled .pt models use the bounded child worker.
    use_pt_worker = bool(deadline is not None and not use_marine_ncnn_worker and pt_worker_models)
    worker_model_paths = marine_ncnn_models if use_marine_ncnn_worker else pt_worker_models if use_pt_worker else []
    models = _load_yolo_models() if (deadline is None and not use_marine_ncnn_worker and _truthy_env("AQUACULTURE_FORCE_PT_YOLO")) else []

    all_objects: List[PredictionObject] = []
    raw_images: Dict[str, bytes] = {}
    boxed_images: Dict[str, bytes] = {}
    crop_images: Dict[str, bytes] = {}
    image_records: List[Dict[str, Any]] = []
    detector_used_values: List[str] = []

    object_counter = 0
    selected_limit = max(1, min(30, _safe_int(top, 5)))
    selected_for_analysis = selected_snapshots[:selected_limit]
    timed_out = False
    completed_model_frames = 0
    model_errors: List[str] = []

    for index, record in enumerate(selected_for_analysis, start=1):
        if (cancel_checker and cancel_checker()) or (deadline is not None and time.monotonic() >= deadline - 0.15):
            timed_out = True
            break
        image_bytes = record.get("image_bytes")
        selected_id = record.get("selected_id") or f"IMG-{index}"
        selected_id = _safe_image_id(str(selected_id), index)
        if not image_bytes:
            continue

        raw_path = f"raw/{selected_id}.jpg"
        boxed_path = f"boxed/{selected_id}_boxed.jpg"
        raw_images[raw_path] = image_bytes

        image_record = {
            "image_id": selected_id,
            "frame_number": record.get("frame_number"),
            "timestamp": record.get("timestamp"),
            "distance_cm": _safe_float(record.get("distance_cm")),
            "distance_status": record.get("distance_status") or "unavailable",
            "raw_path": raw_path,
            "boxed_path": boxed_path,
            "object_count": 0,
            "object_ids": [],
            "detector_used": "",
            "model_attempts": [],
        }

        image = _decode_image(image_bytes)
        if image is None:
            boxed_images[boxed_path] = image_bytes
            image_records.append(image_record)
            continue

        detections: List[Dict[str, Any]] = []
        yolo_meta: Dict[str, Any] = {}

        if use_marine_ncnn_worker or use_pt_worker:
            detections, detector_label, yolo_meta = _detect_with_marine_ncnn_worker(
                image, worker_model_paths, deadline=deadline, cancel_checker=cancel_checker
            )
            if yolo_meta.get("timed_out") or yolo_meta.get("cancelled"):
                timed_out = True
            if yolo_meta.get("worker_error"):
                model_errors.append(str(yolo_meta["worker_error"]))
                image_record["worker_error"] = str(yolo_meta["worker_error"])
            else:
                completed_model_frames += 1
            image_record["model_attempts"] = yolo_meta.get("attempts", [])
            for model_attempt in image_record["model_attempts"]:
                if model_attempt.get("error"):
                    model_errors.append(f"{model_attempt.get('model', 'model')}: {model_attempt['error']}")
            image_record["non_aquatic_guard"] = {"blocked": False, "message": "Aquatic model worker; general detector guard skipped."}
            if detector_label:
                image_record["detector_used"] = detector_label
                detector_used_values.append(detector_label)
            if yolo_meta.get("worker_error"):
                image_record["warning"] = f"Model worker: {yolo_meta.get('worker_error')}"
        else:
            non_aquatic_guard = _detect_non_aquatic_guard(models, image) if models else {"blocked": False}
            image_record["non_aquatic_guard"] = non_aquatic_guard

            if non_aquatic_guard.get("blocked"):
                yolo_meta = {
                    "blocked_by_non_aquatic": True,
                    "ignored_classes": non_aquatic_guard.get("classes", []),
                    "attempts": [{
                        "model": non_aquatic_guard.get("model", "general_guard"),
                        "raw_count": non_aquatic_guard.get("raw_count", 0),
                        "target_count": 0,
                        "ignored_high_conf_count": len(non_aquatic_guard.get("classes", [])),
                        "blocked_by_non_aquatic": True,
                        "ignored_classes": non_aquatic_guard.get("classes", []),
                    }],
                }
                image_record["model_attempts"] = yolo_meta.get("attempts", [])
                image_record["detector_used"] = f"general_guard:{non_aquatic_guard.get('model', 'non_aquatic')}"
                detector_used_values.append(image_record["detector_used"])
            elif models:
                detections, detector_label, yolo_meta = _detect_with_yolo_waterfall(models, image)
                image_record["model_attempts"] = yolo_meta.get("attempts", [])
                if detector_label:
                    image_record["detector_used"] = detector_label
                    detector_used_values.append(detector_label)

        blocked_by_non_aquatic = bool(yolo_meta.get("blocked_by_non_aquatic"))

        allow_opencv_fallback = not timed_out and (
            _truthy_env("AQUACULTURE_ALLOW_OPENCV_FALLBACK") or (not worker_model_paths and not models)
        )
        if not detections and not blocked_by_non_aquatic and allow_opencv_fallback:
            detections = _detect_with_opencv(image)
            if detections:
                image_record["detector_used"] = "opencv_fallback"
                detector_used_values.append("opencv_fallback")
        elif not detections and not blocked_by_non_aquatic and worker_model_paths and not timed_out:
            image_record["warning"] = "No fish or marine life was detected by the marine NCNN model."
        elif blocked_by_non_aquatic:
            ignored = ", ".join(yolo_meta.get("ignored_classes", [])[:4])
            image_record["warning"] = (
                "Non-aquatic objects were detected"
                + (f" ({ignored})" if ignored else "")
                + ", so this frame was not treated as fish/aquatic life."
            )

        if not image_record.get("detector_used"):
            image_record["detector_used"] = "no_detection"

        boxed = image.copy()
        frame_height, frame_width = image.shape[:2]
        aruco_info = _detect_aruco_scale_cm_per_px(image)
        aruco_scale = aruco_info.get("cm_per_px") if aruco_info.get("ok") else None
        image_record["aruco"] = aruco_info

        for detection in detections:
            class_name = _normalize_class_name(detection.get("class_name", "unknown aquatic object"))
            if not _is_target_class(class_name):
                continue

            object_counter += 1
            object_id = f"OBJ-{object_counter:03d}"
            box = _clip_box(detection.get("box_px", [0, 0, 1, 1]), image)
            estimates = estimate_size_from_box(
                box,
                record.get("distance_cm"),
                frame_width_px=frame_width,
                frame_height_px=frame_height,
                aruco_scale_cm_per_px=aruco_scale,
            )

            crop_bytes = _crop_to_jpeg(image, box)
            crop_path = f"crops/{selected_id}_{object_id}.jpg"
            if crop_bytes:
                crop_images[crop_path] = crop_bytes

            _draw_box(boxed, box, class_name, detection.get("confidence", 0.0), object_id)

            prediction_object = PredictionObject(
                image_id=selected_id,
                object_id=object_id,
                class_name=class_name,
                confidence=float(detection.get("confidence", 0.0) or 0.0),
                distance_cm=_safe_float(record.get("distance_cm")),
                box_px=box,
                estimated_length_cm=estimates["estimated_length_cm"],
                estimated_width_cm=estimates["estimated_width_cm"],
                estimated_height_cm=estimates["estimated_height_cm"],
                estimated_weight_g=estimates["estimated_weight_g"],
                crop_path=crop_path if crop_bytes else "",
                estimate_method=estimates.get("estimate_method") or "",
                estimate_note=estimates.get("estimate_note") or "",
            )
            all_objects.append(prediction_object)
            image_record["object_count"] += 1
            image_record["object_ids"].append(object_id)

        boxed_jpeg = _encode_image(boxed)
        boxed_images[boxed_path] = boxed_jpeg or image_bytes
        image_record["objects"] = [obj.to_dict() for obj in all_objects
                                   if obj.image_id == selected_id]
        image_records.append(image_record)
        if progress_callback:
            try:
                progress_callback(image_record, index, len(selected_for_analysis), boxed_jpeg or image_bytes)
            except Exception:
                pass
        if timed_out:
            break

    objects_dict = [obj.to_dict() for obj in all_objects]
    objects_by_image: Dict[str, List[Dict[str, Any]]] = {}
    for obj in objects_dict:
        objects_by_image.setdefault(str(obj.get("image_id")), []).append(obj)

    for record in image_records:
        record["objects"] = objects_by_image.get(str(record.get("image_id")), [])

    all_model_names = [path.name for path in worker_model_paths] + [path.name for path, _ in models]
    detector_used = _summarize_detector_used(detector_used_values, bool(all_model_names))

    return {
        "detector_used": detector_used,
        "ml_flow": "marine_ncnn_subprocess" if use_marine_ncnn_worker else (
            "pt_yolo_subprocess" if use_pt_worker else "pt_yolo_inprocess" if models else "opencv_fallback"
        ),
        "timed_out": timed_out,
        "model_failed": bool(worker_model_paths and completed_model_frames == 0 and not timed_out),
        "model_errors": model_errors,
        "completed_model_frames": completed_model_frames,
        "model_error": _model_error,
        "model_load_errors": dict(_model_load_errors),
        "models_available": all_model_names,
        "objects": objects_dict,
        "images": image_records,
        "selected_images": image_records,
        "all_object_count": len(objects_dict),
        "raw_images": raw_images,
        "boxed_images": boxed_images,
        "crop_images": crop_images,
    }



def _available_marine_ncnn_models() -> List[Path]:
    """Return NCNN model folders that contain the required exported files."""
    if _truthy_env("AQUACULTURE_DISABLE_MARINE_NCNN"):
        return []

    seen: set[str] = set()
    available: List[Path] = []
    for model_path in MARINE_NCNN_MODEL_PRIORITY:
        try:
            resolved = str(model_path.resolve())
        except Exception:
            resolved = str(model_path)
        if resolved in seen:
            continue
        seen.add(resolved)
        if not model_path.is_dir():
            continue
        if not (model_path / "model.ncnn.param").exists():
            continue
        if not (model_path / "model.ncnn.bin").exists():
            continue
        available.append(model_path)
    return available


def _run_model_child(
    command: List[str], cwd: str, timeout: float,
    environment: Dict[str, str],
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> subprocess.CompletedProcess[str]:
    """Run a model in a killable process group with a strict monotonic deadline.

    Ordinary subprocess.run(timeout=N) cannot react to a scan cancellation
    until timeout. Poll in short slices and stop the child and descendants when
    time runs out, so the GUI never waits for a hung ML runtime.
    """
    child = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment, start_new_session=(os.name == "posix"),
    )
    deadline = time.monotonic() + max(0.05, timeout)
    try:
        while True:
            if cancel_checker and cancel_checker():
                raise InterruptedError("Fish scan cancelled while inference was running.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = child.communicate(timeout=min(0.10, remaining))
                return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if child.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                child.communicate(timeout=0.5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    child.kill()
                except OSError:
                    pass


def _detect_with_marine_ncnn_worker(
    image: Any, model_paths: List[Path],
    deadline: Optional[float] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """Run marine NCNN inference in a separate process.

    This is intentionally isolated from Flask. If Ultralytics/NCNN crashes, the
    worker process dies but the web server stays alive and can return a safe
    error/fallback result.
    """
    if cv2 is None or image is None or not model_paths:
        return [], "", {"attempts": [], "worker_error": "OpenCV/image/model unavailable."}

    worker_path = BASE_DIR / "marine_ncnn_worker.py"
    if not worker_path.exists():
        return [], "", {"attempts": [], "worker_error": "marine_ncnn_worker.py was not found."}

    timeout = max(1.0, min(900.0, float(_safe_int(os.environ.get("AQUACULTURE_ML_TIMEOUT"), 300))))
    if deadline is not None:
        # Leave time to construct a response and release scanning state.
        timeout = min(timeout, deadline - time.monotonic() - 0.15)
        if timeout <= 0.05:
            return [], "model_worker:deadline", {"attempts": [], "timed_out": True, "worker_error": "Scan deadline reached."}
    if cancel_checker and cancel_checker():
        return [], "model_worker:cancelled", {"attempts": [], "cancelled": True}
    conf = float(os.environ.get("AQUACULTURE_MARINE_NCNN_CONF", "0.20") or 0.20)
    imgsz = max(320, min(1280, _safe_int(os.environ.get("AQUACULTURE_MARINE_NCNN_IMGSZ"), 640)))

    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="aquaculture_marine_", suffix=".jpg")
        os.close(fd)
        cv2.imwrite(tmp_name, image)

        command = [
            sys.executable,
            str(worker_path),
            "--image",
            tmp_name,
            "--conf",
            str(conf),
            "--imgsz",
            str(imgsz),
            "--max-det",
            str(MAX_DETECTIONS_PER_IMAGE),
        ]
        for model_path in model_paths:
            command.extend(["--model", str(model_path)])

        # This child is the ONLY timed-scan ML owner. Cancel and deadline
        # both terminate it; no uninterruptible Torch work runs in Flask.
        completed = _run_model_child(
            command, cwd=str(BASE_DIR), timeout=timeout,
            environment={**os.environ, "PYTHONIOENCODING": "utf-8"},
            cancel_checker=cancel_checker,
        )

        # Worker is designed to output JSON only, but parse the last JSON-looking
        # line defensively in case a native library prints a banner.
        stdout_text = (completed.stdout or "").strip()
        json_line = stdout_text
        for line in reversed(stdout_text.splitlines()):
            if line.strip().startswith("{") and line.strip().endswith("}"):
                json_line = line.strip()
                break

        payload: Dict[str, Any] = {}
        if json_line:
            try:
                parsed_payload = json.loads(json_line)
                if isinstance(parsed_payload, dict):
                    payload = parsed_payload
            except Exception:
                payload = {}

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[-1000:]
            stdout = (completed.stdout or "").strip()[-1000:]
            attempts = payload.get("attempts") if isinstance(payload.get("attempts"), list) else []
            worker_message = payload.get("message") or payload.get("error") or "worker_failed"
            return [], "marine_ncnn_worker:failed", {
                "attempts": attempts or [{"model": path.name, "raw_count": 0, "target_count": 0, "error": str(worker_message)} for path in model_paths],
                "worker_error": f"exit={completed.returncode}; message={worker_message}; stderr={stderr}; stdout={stdout}",
            }
        if payload.get("ok") is False or not payload.get("models_used"):
            return [], "marine_ncnn_worker:failed", {
                "attempts": payload.get("attempts") or [],
                "worker_error": str(payload.get("message") or payload.get("error") or
                                    "No NCNN model successfully executed."),
            }
        detections = payload.get("detections") if isinstance(payload, dict) else []
        if not isinstance(detections, list):
            detections = []
        attempts = payload.get("attempts") if isinstance(payload, dict) else []
        if not isinstance(attempts, list):
            attempts = []
        models_used = payload.get("models_used") if isinstance(payload, dict) else []
        if not isinstance(models_used, list):
            models_used = [path.name for path in model_paths]

        label = "marine_ncnn:" + "+".join(str(name) for name in models_used if name) if models_used else "marine_ncnn"
        return detections, label, {
            "raw_count": int(payload.get("raw_count", len(detections))) if isinstance(payload, dict) else len(detections),
            "target_count": len(detections),
            "blocked_by_non_aquatic": False,
            "attempts": attempts,
            "worker_elapsed_seconds": payload.get("elapsed_seconds") if isinstance(payload, dict) else None,
        }
    except InterruptedError:
        return [], "marine_ncnn_worker:cancelled", {"cancelled": True, "attempts": []}
    except subprocess.TimeoutExpired:
        return [], "marine_ncnn_worker:timeout", {
            "attempts": [{"model": path.name, "raw_count": 0, "target_count": 0, "error": "timeout"} for path in model_paths],
            "worker_error": f"Model timeout within the {timeout:.2f}-second remaining budget.",
            "timed_out": True,
        }
    except Exception as error:
        return [], "marine_ncnn_worker:error", {
            "attempts": [{"model": path.name, "raw_count": 0, "target_count": 0, "error": type(error).__name__} for path in model_paths],
            "worker_error": _safe_error(error),
        }
    finally:
        if tmp_name:
            try:
                os.remove(tmp_name)
            except Exception:
                pass

def _load_best_model() -> Any:
    """
    Legacy helper kept for compatibility. The actual prediction path uses
    _load_yolo_models() so it can try all models in waterfall order.
    """
    models = _load_yolo_models()
    return models[0][1] if models else None


def _load_yolo_models() -> List[Tuple[Path, Any]]:
    """
    Load all available Ultralytics YOLO models in priority order.

    Safe behavior:
    - Missing files are skipped.
    - Broken/incompatible model files are skipped and recorded.
    - The server never crashes if a model fails to load.
    """
    global _loaded_model, _loaded_model_path, _model_error

    if not _truthy_env("AQUACULTURE_FORCE_PT_YOLO"):
        _model_error = "Raw .pt YOLO is disabled. Marine NCNN subprocess mode is preferred on Raspberry Pi."
        return []

    yolo_class = _get_yolo_class()
    if yolo_class is None:
        available_files = [path.name for path in MODEL_PRIORITY if path.exists()]
        if available_files:
            _model_error = _YOLO_IMPORT_ERROR or (
                "YOLO/Ultralytics is unavailable, so the downloaded YOLO fish/aquatic models cannot run. "
                "OpenCV fallback will be used."
            )
        else:
            _model_error = "No YOLO model files were found in models/. OpenCV fallback will be used."
        return []

    loaded: List[Tuple[Path, Any]] = []
    _model_load_errors.clear()

    for model_path in MODEL_PRIORITY:
        if not model_path.exists():
            continue

        cache_key = str(model_path.resolve())
        model = _loaded_models.get(cache_key)

        if model is None:
            try:
                model = yolo_class(str(model_path))
                _loaded_models[cache_key] = model
            except Exception as error:
                _model_load_errors[model_path.name] = f"{type(error).__name__}: {error}"
                continue

        loaded.append((model_path, model))

    if loaded:
        _loaded_model_path = loaded[0][0]
        _loaded_model = loaded[0][1]
        _model_error = None if not _model_load_errors else "Some models could not be loaded; available models were still used."
        return loaded

    if _model_load_errors:
        _model_error = "No YOLO model could be loaded. OpenCV fallback will be used."
    else:
        _model_error = "No YOLO model file was found in models/. OpenCV fallback will be used."
    _loaded_model = None
    _loaded_model_path = None
    return []


def _specialized_model_role(model_path: Optional[Path]) -> str:
    if model_path is None:
        return ""
    return SPECIALIZED_MODEL_ROLES.get(model_path.name.lower(), "")


def _is_general_model(model_path: Optional[Path]) -> bool:
    if model_path is None:
        return False
    return model_path.name.lower() in GENERAL_MODEL_NAMES


def _summarize_detector_used(detectors: List[str], has_yolo_models: bool) -> str:
    if not detectors:
        return "yolo_waterfall:no_detections" if has_yolo_models else "opencv_fallback"
    unique: List[str] = []
    for item in detectors:
        if item and item not in unique:
            unique.append(item)
    return "+".join(unique)


def _detect_non_aquatic_guard(models: List[Tuple[Path, Any]], image: Any) -> Dict[str, Any]:
    """
    Uses a general YOLO model as a safety guard before aquatic-only models.

    Why this exists:
    - A fish-only model may label any large foreground object as fish because it
      was not trained with person/phone/chair negative classes.
    - The guard detects common non-aquatic objects first. If a strong ignored
      object is present, the frame is skipped instead of becoming a false fish.

    This guard is safe:
    - It runs only when a general fallback model is available.
    - It blocks only high-confidence ignored classes with a meaningful area.
    - If no general model is available, it returns blocked:false.
    """
    if _get_yolo_class() is None or image is None:
        return {"blocked": False, "message": "General guard unavailable."}

    frame_height, frame_width = image.shape[:2]
    frame_area = max(1, int(frame_width) * int(frame_height))

    general_candidates = [(path, model) for path, model in models if _is_general_model(path)]
    # Prefer the ordinary detector for the guard when it exists; segmentation/OBB
    # models are still okay as fallback.
    general_candidates.sort(key=lambda item: 0 if item[0].name.lower() == "yolo26n.pt" else 1)

    for model_path, model in general_candidates:
        try:
            results = model.predict(
                image,
                verbose=False,
                conf=NON_AQUATIC_GUARD_CONFIDENCE,
                imgsz=YOLO_IMAGE_SIZE,
                max_det=MAX_DETECTIONS_PER_IMAGE,
            )
            if not results:
                continue

            names = getattr(results[0], "names", {}) or getattr(model, "names", {}) or {}
            boxes = getattr(results[0], "boxes", None)
            if boxes is None:
                continue

            blocked_classes: List[str] = []
            raw_count = 0
            for box in boxes:
                try:
                    raw_count += 1
                    cls_index = int(box.cls[0]) if getattr(box, "cls", None) is not None else -1
                    raw_name = str(names.get(cls_index, f"class_{cls_index}")) if isinstance(names, dict) else f"class_{cls_index}"
                    normalized = _normalize_class_name(raw_name)
                    if not (_is_ignored_class(raw_name) or _is_ignored_class(normalized)):
                        continue

                    xyxy = [float(v) for v in box.xyxy[0].tolist()]
                    x1, y1, x2, y2 = xyxy
                    area_ratio = max(0.0, (x2 - x1) * (y2 - y1)) / frame_area
                    if area_ratio >= NON_AQUATIC_GUARD_MIN_AREA_RATIO:
                        blocked_classes.append(raw_name)
                except Exception:
                    continue

            if blocked_classes:
                return {
                    "blocked": True,
                    "model": model_path.name,
                    "classes": blocked_classes[:8],
                    "raw_count": raw_count,
                    "message": "General object guard found non-aquatic objects in the frame.",
                }
        except Exception as error:
            _model_load_errors[f"{model_path.name}:guard"] = f"Guard failed: {type(error).__name__}: {error}"
            continue

    return {"blocked": False, "message": "No strong non-aquatic object was found by the guard."}


def _detect_with_yolo_waterfall(models: List[Tuple[Path, Any]], image: Any) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """
    Try YOLO models one by one.

    The first three user models are aquatic-focused, so unknown species labels
    from them are allowed as aquatic detections. The old yolo26 models are
    treated as general fallback and are stricter to prevent phone/person/chair
    false positives.
    """
    attempts: List[Dict[str, Any]] = []
    final_meta: Dict[str, Any] = {
        "raw_count": 0,
        "target_count": 0,
        "ignored_high_conf_count": 0,
        "blocked_by_non_aquatic": False,
        "ignored_classes": [],
        "attempts": attempts,
    }

    for model_path, model in models:
        detections = _detect_with_yolo(model, image, model_path=model_path)
        meta = dict(_last_yolo_meta)
        attempt = {
            "model": model_path.name,
            "raw_count": meta.get("raw_count", 0),
            "target_count": meta.get("target_count", len(detections)),
            "ignored_high_conf_count": meta.get("ignored_high_conf_count", 0),
            "blocked_by_non_aquatic": bool(meta.get("blocked_by_non_aquatic")),
            "ignored_classes": meta.get("ignored_classes", []),
        }
        attempts.append(attempt)

        if detections:
            meta["attempts"] = attempts
            return detections, f"yolo:{model_path.name}", meta

        # Only old/general models are allowed to block OpenCV fallback based on
        # strong non-aquatic results. Specialized aquatic models are allowed to
        # fail quietly so the next model can try.
        if bool(meta.get("blocked_by_non_aquatic")) and _is_general_model(model_path):
            meta["attempts"] = attempts
            return [], f"yolo:{model_path.name}", meta

        final_meta = meta

    final_meta["attempts"] = attempts
    return [], "", final_meta


def _detect_with_yolo(model: Any, image: Any, model_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    global _last_yolo_meta

    detections: List[Dict[str, Any]] = []
    ignored_high_conf: List[str] = []
    raw_count = 0
    role = _specialized_model_role(model_path)
    is_specialized = bool(role)

    _last_yolo_meta = {
        "raw_count": 0,
        "target_count": 0,
        "ignored_high_conf_count": 0,
        "blocked_by_non_aquatic": False,
        "ignored_classes": [],
    }

    try:
        # Keep confidence conservative for general models and lower for
        # fish/aquatic models because underwater/fishpond detections may be small
        # or less crisp. Specialized models also use a larger image size to help
        # find small fish in pond images during laptop testing.
        confidence = 0.10 if is_specialized else 0.35
        image_size = SPECIALIZED_YOLO_IMAGE_SIZE if is_specialized else YOLO_IMAGE_SIZE
        results = model.predict(
            image,
            verbose=False,
            conf=confidence,
            imgsz=image_size,
            max_det=MAX_DETECTIONS_PER_IMAGE,
        )
        if not results:
            return []

        names = getattr(results[0], "names", {}) or getattr(model, "names", {}) or {}
        boxes = getattr(results[0], "boxes", None)
        if boxes is None:
            return []

        for box in boxes:
            try:
                raw_count += 1
                xyxy = box.xyxy[0].tolist()
                conf = float(box.conf[0]) if getattr(box, "conf", None) is not None else 0.0
                cls_index = int(box.cls[0]) if getattr(box, "cls", None) is not None else -1
                raw_name = str(names.get(cls_index, f"class_{cls_index}")) if isinstance(names, dict) else f"class_{cls_index}"
                normalized = _normalize_class_name(raw_name)

                if _is_ignored_class(raw_name) or _is_ignored_class(normalized):
                    if conf >= 0.38:
                        ignored_high_conf.append(raw_name)
                    continue

                if not _is_target_class(normalized):
                    # The aquatic models may output species names not listed in
                    # TARGET_CLASS_ALIASES. Since these model files are trained
                    # for aquatic life, accept them instead of dropping them.
                    if role in {"fish", "aquatic"}:
                        normalized = "fish"
                    elif role == "megafauna":
                        normalized = _normalize_megafauna_label(raw_name)
                    else:
                        continue

                detections.append({
                    "class_name": normalized,
                    "raw_class": raw_name,
                    "confidence": conf,
                    "box_px": [int(round(v)) for v in xyxy],
                    "model": model_path.name if model_path else "",
                })
            except Exception:
                continue
    except Exception as error:
        if model_path is not None:
            _model_load_errors[model_path.name] = f"Inference failed: {type(error).__name__}: {error}"
        return []

    _last_yolo_meta = {
        "raw_count": raw_count,
        "target_count": len(detections),
        "ignored_high_conf_count": len(ignored_high_conf),
        "blocked_by_non_aquatic": raw_count > 0 and not detections and len(ignored_high_conf) > 0,
        "ignored_classes": ignored_high_conf[:8],
    }
    return detections


def _normalize_megafauna_label(raw_name: str) -> str:
    text = str(raw_name or "").lower().replace("_", " ")
    if "shark" in text:
        return "shark"
    if "ray" in text or "manta" in text or "stingray" in text:
        return "ray"
    if "turtle" in text:
        return "turtle"
    if "fish" in text:
        return "fish"
    return "unknown aquatic object"


def _detect_with_opencv(image: Any) -> List[Dict[str, Any]]:
    """
    Conservative fallback detector for laptop/test mode.

    This does not classify species. It only tries to find the most visible
    foreground object, then labels it as unknown aquatic object. It is useful
    when YOLO has no matching fish/aquatic class or when the model is absent.
    """
    if cv2 is None or np is None:
        return []

    try:
        height, width = image.shape[:2]
        total_area = max(1, width * height)
        detections: List[Dict[str, Any]] = []

        # 1) Edge-based contours.
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 35, 130)
        kernel = np.ones((5, 5), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # 2) Color/texture foreground guard. Fish images often have low edges
        # around smooth bodies; saturation helps find the main object.
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1]
        value = hsv[:, :, 2]
        sat_mask = cv2.inRange(saturation, 35, 255)
        val_mask = cv2.inRange(value, 25, 245)
        mask = cv2.bitwise_and(sat_mask, val_mask)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask_contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = list(contours) + list(mask_contours)

        min_area = max(120, int(total_area * 0.004))
        max_area = int(total_area * 0.78)
        candidates: List[Tuple[float, List[int]]] = []

        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < min_area or area > max_area:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if w < 12 or h < 12:
                continue
            # Ignore near full-frame/background boxes.
            touches_left = x <= 2
            touches_top = y <= 2
            touches_right = (x + w) >= (width - 3)
            touches_bottom = (y + h) >= (height - 3)
            if (touches_left and touches_right and w > width * 0.82) or (touches_top and touches_bottom and h > height * 0.82):
                continue
            if w > width * 0.92 and h > height * 0.70:
                continue
            aspect = w / max(1, h)
            if aspect < 0.12 or aspect > 9.0:
                continue

            # Prefer bigger central objects but avoid full-frame boxes.
            center_x = x + w / 2
            center_y = y + h / 2
            center_distance = abs(center_x - width / 2) / width + abs(center_y - height / 2) / height
            score = area - (center_distance * total_area * 0.08)
            candidates.append((score, [x, y, x + w, y + h]))

        # Merge overlapping/high-confidence candidates into one main object.
        candidates.sort(key=lambda item: item[0], reverse=True)
        used: List[List[int]] = []
        for score, box in candidates[:8]:
            if any(_box_iou(box, existing) > 0.35 for existing in used):
                continue
            used.append(box)
            detections.append({
                "class_name": "marine life candidate",
                "confidence": 0.35,
                "box_px": box,
            })
            if len(detections) >= 5:
                break

        return detections
    except Exception:
        return []


def _box_iou(a: List[int], b: List[int]) -> float:
    try:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1, (bx2 - bx1) * (by2 - by1))
        return inter / max(1, area_a + area_b - inter)
    except Exception:
        return 0.0

def _normalize_class_name(name: str) -> str:
    clean = str(name or "").strip().lower().replace("_", " ")
    if clean in TARGET_CLASS_ALIASES:
        return TARGET_CLASS_ALIASES[clean]

    checks = [
        ("marine life", "marine life"),
        ("marine life candidate", "marine life"),
        ("jellyfish", "jellyfish"),
        ("jelly fish", "jellyfish"),
        ("fish", "fish"),
        ("shark", "shark"),
        ("ray", "ray"),
        ("turtle", "turtle"),
        ("crocodile", "crocodile"),
        ("alligator", "crocodile"),
        ("coral", "coral"),
        ("octopus", "octopus"),
        ("squid", "squid"),
        ("crab", "crab"),
        ("lobster", "lobster"),
        ("shrimp", "shrimp"),
        ("seahorse", "seahorse"),
        ("sea horse", "seahorse"),
        ("starfish", "starfish"),
        ("sea star", "starfish"),
        ("clam", "clam"),
        ("oyster", "oyster"),
        ("water plant", "aquatic plant"),
        ("aquatic plant", "aquatic plant"),
    ]
    for needle, mapped in checks:
        if needle in clean:
            return mapped

    if clean == "plant":
        return "aquatic plant"

    return clean or "unknown aquatic object"


def _is_ignored_class(class_name: str) -> bool:
    clean = str(class_name or "").strip().lower().replace("_", " ")
    normalized = _normalize_class_name(clean)
    return clean in IGNORED_CLASSES or normalized in IGNORED_CLASSES


def _is_target_class(class_name: str) -> bool:
    clean = _normalize_class_name(class_name)
    if _is_ignored_class(clean):
        return False
    if clean in set(TARGET_CLASS_ALIASES.values()) or clean == "marine life":
        return True
    return clean == "unknown aquatic object" and _truthy_env("AQUACULTURE_ALLOW_UNKNOWN_AQUATIC")


def _detect_aruco_scale_cm_per_px(image: Any) -> Dict[str, Any]:
    """
    Detects an ArUco marker and returns cm-per-pixel scale when available.

    This is optional and safe:
    - Works only when OpenCV has the aruco module.
    - If unavailable/not detected, it returns ok:false and detection continues.
    """
    if cv2 is None:
        return {"ok": False, "message": "OpenCV is unavailable."}

    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        return {"ok": False, "message": "OpenCV ArUco module is unavailable. Install opencv-contrib-python for marker support."}

    try:
        dictionary_id = getattr(aruco, ARUCO_DICT_NAME, None)
        if dictionary_id is None:
            dictionary_id = getattr(aruco, "DICT_4X4_50")
        dictionary = aruco.getPredefinedDictionary(dictionary_id)

        if hasattr(aruco, "ArucoDetector"):
            parameters = aruco.DetectorParameters()
            detector = aruco.ArucoDetector(dictionary, parameters)
            corners, ids, _ = detector.detectMarkers(image)
        else:
            parameters = aruco.DetectorParameters_create()
            corners, ids, _ = aruco.detectMarkers(image, dictionary, parameters=parameters)

        if ids is None or not corners:
            return {"ok": False, "message": "Reference marker not detected."}

        marker = corners[0][0]
        side_lengths = []
        for index in range(4):
            p1 = marker[index]
            p2 = marker[(index + 1) % 4]
            side_lengths.append(float(np.linalg.norm(p1 - p2)) if np is not None else 0.0)

        marker_px = sum(side_lengths) / max(1, len(side_lengths))
        if marker_px <= 0:
            return {"ok": False, "message": "Reference marker size could not be measured."}

        return {
            "ok": True,
            "marker_id": int(ids[0][0]) if ids is not None else None,
            "marker_size_cm": ARUCO_MARKER_REAL_SIZE_CM,
            "marker_px": round(marker_px, 2),
            "cm_per_px": ARUCO_MARKER_REAL_SIZE_CM / marker_px,
            "message": "Reference marker detected.",
        }
    except Exception as error:
        return {"ok": False, "message": f"Reference marker detection failed: {_safe_error(error)}"}


def estimate_size_from_box(
    box_px: List[int],
    distance_cm: Any,
    frame_width_px: Optional[int] = None,
    frame_height_px: Optional[int] = None,
    aruco_scale_cm_per_px: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Approximate estimation only.

    Preferred mode:
    - Use ultrasonic distance when available.

    Laptop/test fallback:
    - If ultrasonic is unavailable, estimate using a conservative visual scale
      based on frame width. This prevents the frontend from showing only dashes,
      but the result is clearly marked as visual-only and low-confidence.
    """
    x1, y1, x2, y2 = box_px
    box_w = max(1, abs(x2 - x1))
    box_h = max(1, abs(y2 - y1))
    distance = _safe_float(distance_cm)

    if aruco_scale_cm_per_px is not None and aruco_scale_cm_per_px > 0:
        cm_per_px = float(aruco_scale_cm_per_px)
        method = "aruco_reference_marker"
        note = "Estimated from bounding box and an ArUco reference marker in the frame."
    elif distance is not None and distance > 0:
        assumed_horizontal_fov_degrees = 62.0
        assumed_frame_width_px = float(frame_width_px or 1280.0)
        cm_per_px = (2 * distance * math.tan(math.radians(assumed_horizontal_fov_degrees / 2))) / max(1.0, assumed_frame_width_px)
        method = "distance_sensor"
        note = "Estimated from bounding box and camera-to-water distance sensor reading."
    else:
        # Fallback for uploaded images/laptop webcam where ultrasonic is not present.
        # This is not a measured size. It gives a useful approximate number while
        # preserving distance_cm as null.
        assumed_frame_width_px = float(frame_width_px or 640.0)
        cm_per_px = DEFAULT_VISUAL_FRAME_WIDTH_CM / max(1.0, assumed_frame_width_px)
        cm_per_px = max(MIN_VISUAL_CM_PER_PX, min(MAX_VISUAL_CM_PER_PX, cm_per_px))
        method = "visual_only_fallback"
        note = "Distance sensor and ArUco marker were unavailable; size is a rough visual estimate from image scale only."

    length_cm = round(max(box_w, box_h) * cm_per_px, 2)
    width_cm = round(min(box_w, box_h) * cm_per_px, 2)
    height_cm = round(max(width_cm * 0.45, 0.1), 2)
    weight_g = round(max(length_cm * width_cm * height_cm * 0.12, 0.1), 2)

    return {
        "estimated_length_cm": length_cm,
        "estimated_width_cm": width_cm,
        "estimated_height_cm": height_cm,
        "estimated_weight_g": weight_g,
        "estimate_method": method,
        "estimate_note": note,
    }


def build_prediction_zip(
    session_id: Optional[int],
    started_at: str,
    capture_duration: float,
    snapshots: List[Dict[str, Any]],
    selected_snapshots: List[Dict[str, Any]],
    detection_result: Dict[str, Any],
    camera_status: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    objects = detection_result.get("objects", [])
    image_records = detection_result.get("images") or detection_result.get("selected_images") or []
    raw_images = detection_result.get("raw_images", {})
    boxed_images = detection_result.get("boxed_images", {})
    crop_images = detection_result.get("crop_images", {})

    ultrasonic_ok = sum(1 for row in snapshots if row.get("distance_status") == "ok")
    ultrasonic_status = "available" if ultrasonic_ok > 0 else "unavailable"
    created_at = _now_iso()

    images_for_summary: List[Dict[str, Any]] = []
    for image_record in image_records:
        image_id = str(image_record.get("image_id") or "")
        raw_path = str(image_record.get("raw_path") or f"raw/{image_id}.jpg")
        boxed_path = str(image_record.get("boxed_path") or f"boxed/{image_id}_boxed.jpg")
        image_objects = [dict(obj) for obj in image_record.get("objects", [])]

        for obj in image_objects:
            crop_path = obj.get("crop_path") or ""
            crop_bytes = crop_images.get(crop_path)
            obj["crop_data_url"] = _image_data_url(crop_bytes) if crop_bytes else ""

        raw_bytes = raw_images.get(raw_path)
        boxed_bytes = boxed_images.get(boxed_path)

        images_for_summary.append({
            "image_id": image_id,
            "frame_number": image_record.get("frame_number"),
            "timestamp": image_record.get("timestamp"),
            "distance_cm": image_record.get("distance_cm"),
            "distance_status": image_record.get("distance_status"),
            "aruco": image_record.get("aruco") or {"ok": False, "message": "Reference marker not detected."},
            "warning": image_record.get("warning") or "",
            "raw_path": raw_path,
            "boxed_path": boxed_path,
            "raw_image_data_url": _image_data_url(raw_bytes) if raw_bytes else "",
            "boxed_image_data_url": _image_data_url(boxed_bytes) if boxed_bytes else "",
            "detector_used": image_record.get("detector_used") or "",
            "model_attempts": image_record.get("model_attempts") or [],
            "non_aquatic_guard": image_record.get("non_aquatic_guard") or {},
            "object_count": int(image_record.get("object_count") or len(image_objects)),
            "objects": image_objects,
        })

    camera_status = camera_status or {}
    camera_mode = str(camera_status.get("mode") or "unknown")
    warnings: List[str] = []
    if camera_mode == "opencv":
        warnings.append("Server camera uses OpenCV; this may be a Raspberry Pi USB camera or a local webcam.")
    elif camera_mode == "uploaded":
        warnings.append("Uploaded test images were used, not the server's camera.")
    elif camera_mode not in {"picamera2"}:
        warnings.append("Server camera was not confirmed for this scan.")

    model_error_text = str(detection_result.get("model_error") or "")
    if model_error_text:
        warnings.append(model_error_text)

    if ultrasonic_status != "available":
        warnings.append("Distance sensor reading was unavailable, so size uses visual/reference fallback estimates.")

    if not any((image.get("aruco") or {}).get("ok") for image in images_for_summary):
        warnings.append("ArUco reference marker was not detected. Print/place a known-size ArUco marker for better size accuracy.")

    for image in images_for_summary:
        if image.get("warning"):
            warnings.append(str(image.get("warning")))
        for attempt in image.get("model_attempts") or []:
            err = str(attempt.get("error") or "").strip()
            if err:
                warnings.append(f"{attempt.get('model', 'model')} error: {err}")

    # Keep warning list unique but ordered.
    seen_warning = set()
    warnings = [w for w in warnings if not (w in seen_warning or seen_warning.add(w))]

    result_summary = {
        "session_id": session_id,
        "created_at": created_at,
        "started_at": started_at,
        "capture_duration": capture_duration,
        "snapshot_count": len(snapshots),
        "selected_snapshot_count": len(selected_snapshots),
        "camera_mode": camera_mode,
        "camera_message": camera_status.get("message"),
        "ultrasonic_status": ultrasonic_status,
        "distance_sensor_status": ultrasonic_status,
        "detector_used": detection_result.get("detector_used"),
        "ml_flow": detection_result.get("ml_flow"),
        "models_available": detection_result.get("models_available") or [],
        "model_error": detection_result.get("model_error"),
        "model_load_errors": detection_result.get("model_load_errors") or {},
        "warnings": warnings,
        "object_count": len(objects),
        "image_count": len(images_for_summary),
        "images": images_for_summary,
        "objects": objects,
        "note": "Result is estimated only. Weight may not be accurate. Distance sensor readings and reference markers improve accuracy.",
    }

    prediction_info = _build_prediction_info_text(result_summary)
    result_txt = _build_result_text(result_summary)
    result_json = json.dumps(_strip_image_data_urls(result_summary), indent=2, ensure_ascii=False)

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("prediction_settings.txt", _build_prediction_settings_text(result_summary, snapshots))
        zf.writestr("prediction_info.txt", prediction_info)
        zf.writestr("result.txt", result_txt)
        zf.writestr("result.json", result_json)

        for path, image_bytes in raw_images.items():
            if image_bytes:
                zf.writestr(path, image_bytes)
                # Backward-compatible path used by older UI/testing builds.
                image_id = Path(path).stem
                zf.writestr(f"images/{image_id}.jpg", image_bytes)

        for path, image_bytes in boxed_images.items():
            if image_bytes:
                zf.writestr(path, image_bytes)
                image_id = Path(path).stem.replace("_boxed", "")
                zf.writestr(f"images/{image_id}_boxed.jpg", image_bytes)

        for path, image_bytes in crop_images.items():
            if image_bytes:
                zf.writestr(path, image_bytes)

        overview_xlsx = _make_overview_excel(result_summary)
        if overview_xlsx:
            zf.writestr("excel/OVERVIEW.xlsx", overview_xlsx)

        for image_record in images_for_summary:
            image_xlsx = _make_image_excel(image_record, result_summary)
            if image_xlsx:
                safe_name = _safe_filename(str(image_record.get("image_id") or "IMG"))
                zf.writestr(f"excel/{safe_name}.xlsx", image_xlsx)

    return output.getvalue(), result_summary


def _safe_image_id(value: str, index: int) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-")
    if not text:
        text = f"IMG-{index}"
    if text.lower().startswith("selected_"):
        suffix = text.split("_", 1)[-1]
        text = f"IMG-{suffix}"
    return text.upper()


def _safe_filename(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-")
    return text or "FILE"


def _image_data_url(image_bytes: Optional[bytes]) -> str:
    """
    Builds a browser-friendly image data URL.

    The ZIP keeps the original image bytes, but the JSON response should not
    send huge 4K/phone-camera images because it can freeze the browser and make
    result cards look like they never rendered. For previews, resize large
    images to a safer display size.
    """
    if not image_bytes:
        return ""

    preview_bytes = _make_preview_image_bytes(image_bytes) or image_bytes
    mime = "image/jpeg"
    if preview_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif preview_bytes.startswith(b"RIFF") and b"WEBP" in preview_bytes[:16]:
        mime = "image/webp"
    elif preview_bytes.startswith(b"GIF87a") or preview_bytes.startswith(b"GIF89a"):
        mime = "image/gif"
    encoded = base64.b64encode(preview_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _make_preview_image_bytes(image_bytes: bytes, max_side: int = 1280, max_bytes: int = 900_000) -> Optional[bytes]:
    if not image_bytes or cv2 is None or np is None:
        return None
    if len(image_bytes) <= max_bytes:
        return image_bytes
    try:
        array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            return image_bytes
        height, width = image.shape[:2]
        longest = max(height, width)
        if longest > max_side:
            scale = max_side / float(longest)
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        return buffer.tobytes() if ok else image_bytes
    except Exception:
        return image_bytes



def _strip_image_data_urls(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_image_data_urls(item) for item in value]
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for key, entry in value.items():
            if isinstance(entry, str) and entry.startswith("data:image/"):
                clean[key] = "[image data kept as file inside ZIP]"
            else:
                clean[key] = _strip_image_data_urls(entry)
        return clean
    return value


def _make_overview_excel(summary: Dict[str, Any]) -> Optional[bytes]:
    if Workbook is None:
        return None
    wb = Workbook()
    ws = wb.active
    ws.title = "OVERVIEW"
    ws.append(["imageID", "rawImages", "imageWithBoxingDrawn", "detectedCount"])
    for image in summary.get("images") or []:
        ws.append([
            image.get("image_id"),
            image.get("raw_path"),
            image.get("boxed_path"),
            image.get("object_count"),
        ])
    return _workbook_to_bytes(wb)


def _make_image_excel(image_record: Dict[str, Any], summary: Dict[str, Any]) -> Optional[bytes]:
    if Workbook is None:
        return None
    wb = Workbook()
    title = _safe_filename(str(image_record.get("image_id") or "IMG"))[:31]
    ws = wb.active
    ws.title = title
    ws.append(["objectID", "class", "confidence", "length_cm", "width_cm", "height_cm", "weight_g", "croppedImage"])
    for obj in image_record.get("objects") or []:
        ws.append([
            obj.get("object_id"),
            obj.get("class"),
            obj.get("confidence"),
            obj.get("estimated_length_cm"),
            obj.get("estimated_width_cm"),
            obj.get("estimated_height_cm"),
            obj.get("estimated_weight_g"),
            obj.get("crop_path"),
        ])
    return _workbook_to_bytes(wb)


def _workbook_to_bytes(workbook: Any) -> bytes:
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _build_prediction_settings_text(summary: Dict[str, Any], snapshots: List[Dict[str, Any]]) -> str:
    distance_values = [row.get("distance_cm") for row in snapshots if row.get("distance_cm") is not None]
    return "\n".join([
        "AquaCulture Visual Scan Settings",
        "================================",
        f"Scan started at: {summary.get('started_at')}",
        f"ZIP/result created at: {summary.get('created_at')}",
        f"Session ID: {summary.get('session_id')}",
        f"Scan/capture time: {summary.get('capture_duration')} seconds",
        f"Total snapshots captured: {summary.get('snapshot_count')}",
        f"Selected images: {summary.get('selected_snapshot_count')}",
        f"Detector used: {summary.get('detector_used')}",
        f"Models available: {summary.get('models_available')}",
        f"Model load errors: {summary.get('model_load_errors')}",
        f"Distance sensor status: {summary.get('ultrasonic_status')}",
        f"Camera-to-water distances: {distance_values if distance_values else 'unavailable'}",
        "",
        "NOTE:",
        "- Result is estimated only.",
        "- Weight may not be accurate.",
        "- Distance sensor measures camera/device to water surface only and does not know fish depth underwater.",
        "",
    ])




def _build_prediction_info_text(summary: Dict[str, Any]) -> str:
    return "\n".join([
        "AquaCulture Prediction Information",
        "=================================",
        f"Prediction ID: {summary.get('prediction_id', 'assigned after database save')}",
        f"Session ID: {summary.get('session_id')}",
        f"Created At: {summary.get('created_at')}",
        f"Capture Duration: {summary.get('capture_duration')} seconds",
        f"Selected Snapshots: {summary.get('selected_snapshot_count')}",
        f"Distance sensor: {summary.get('ultrasonic_status')}",
        f"Detector Used: {summary.get('detector_used')}",
        "Note: Result is estimated only.",
        "",
    ])


def _build_result_text(summary: Dict[str, Any]) -> str:
    lines = [
        "AquaCulture Visual Size Result",
        "==============================",
        "Result is estimated only.",
        "Distance sensor measures camera/device to water surface only, not fish depth underwater.",
        "",
    ]

    objects = summary.get("objects") or []
    if not objects:
        lines.append("No aquatic target was detected.")
    for obj in objects:
        lines.extend([
            f"image id: {obj.get('image_id')}",
            f"object id: {obj.get('object_id')}",
            f"class: {obj.get('class')}",
            f"confidence: {obj.get('confidence')}",
            f"distance_cm: {obj.get('distance_cm')}",
            f"box_px: {obj.get('box_px')}",
            f"estimated_length_cm: {obj.get('estimated_length_cm')}",
            f"estimated_width_cm: {obj.get('estimated_width_cm')}",
            f"estimated_height_cm: {obj.get('estimated_height_cm')}",
            f"estimated_weight_g: {obj.get('estimated_weight_g')}",
            f"estimate_method: {obj.get('estimate_method')}",
            f"estimate_note: {obj.get('estimate_note')}",
            "",
        ])

    lines.extend([
        f"final summary: {len(objects)} object(s) returned.",
        "note: result is estimated only.",
        "",
    ])
    return "\n".join(lines)


def get_prediction_zip(prediction_id: int) -> Optional[bytes]:
    if db is None:
        return None
    try:
        return db.get_prediction_zip(int(prediction_id))
    except Exception:
        return None


def analyze_uploaded_images(
    image_bytes_list: List[bytes],
    top: int = 5,
) -> Dict[str, Any]:
    """
    Used by /api/test/visual/upload and /api/test/visual/url.

    Test mode is intentionally single-image now:
    - One uploaded image is scanned immediately by the frontend.
    - If multiple images are submitted, only the first one is analyzed.
    - The camera is never required.
    """
    all_images = [img for img in image_bytes_list if img]
    ignored_count = max(0, len(all_images) - 1)
    image_bytes_list = all_images[:1]

    snapshots = []
    for index, image_bytes in enumerate(image_bytes_list, start=1):
        snapshots.append({
            "frame_number": index,
            "timestamp": _now_iso(),
            "image_bytes": image_bytes,
            "distance_cm": None,
            "distance_status": "unavailable",
            "selected_id": f"IMG-{index:03d}",
        })
    selected = select_best_snapshots(snapshots, minimum=1, maximum=1)
    if not selected:
        return {
            "ok": False,
            "message": "Uploaded image could not be decoded or analyzed.",
            "camera_required": False,
            "source": "uploaded_images",
        }

    detection_result = analyze_selected_snapshots(selected, top=1)
    _, result = build_prediction_zip(
        session_id=None,
        started_at=_now_iso(),
        capture_duration=0,
        snapshots=snapshots,
        selected_snapshots=selected,
        detection_result=detection_result,
        camera_status={"mode": "uploaded", "message": "One uploaded test image was used. Camera was not required."},
    )

    if ignored_count:
        warnings = result.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"Only the first image was analyzed. {ignored_count} extra image(s) were ignored in single-image test mode.")

    object_count = int(result.get("object_count") or 0)
    if object_count <= 0:
        result["message"] = "No fish or marine life was detected in the uploaded image."

    return {
        "ok": True,
        "message": "Test visual analysis completed from one uploaded image. Camera was not required.",
        "source": "uploaded_image_single",
        "camera_required": False,
        "result": result,
    }


def _decode_image(image_bytes: bytes) -> Any:
    if cv2 is None or np is None:
        return None
    try:
        array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        return image
    except Exception:
        return None


def _encode_image(image: Any) -> Optional[bytes]:
    if cv2 is None:
        return None
    try:
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
        return buffer.tobytes() if ok else None
    except Exception:
        return None


def _clip_box(box: Iterable[Any], image: Any) -> List[int]:
    height, width = image.shape[:2]
    values = list(box)
    x1 = max(0, min(width - 1, _safe_int(values[0], 0)))
    y1 = max(0, min(height - 1, _safe_int(values[1], 0)))
    x2 = max(0, min(width - 1, _safe_int(values[2], width - 1)))
    y2 = max(0, min(height - 1, _safe_int(values[3], height - 1)))
    if x2 <= x1:
        x2 = min(width - 1, x1 + 1)
    if y2 <= y1:
        y2 = min(height - 1, y1 + 1)
    return [x1, y1, x2, y2]


def _crop_to_jpeg(image: Any, box: List[int]) -> Optional[bytes]:
    try:
        x1, y1, x2, y2 = box
        crop = image[y1:y2, x1:x2]
        if crop is None or crop.size == 0:
            return None
        return _encode_image(crop)
    except Exception:
        return None


def _draw_box(image: Any, box: List[int], class_name: str, confidence: float, object_id: str) -> None:
    if cv2 is None:
        return
    try:
        x1, y1, x2, y2 = box
        height, width = image.shape[:2]
        box_color = (0, 255, 0)
        cv2.rectangle(image, (x1, y1), (x2, y2), box_color, 2)

        # Keep labels compact. The UI often displays the boxed image smaller than
        # the source image, so large OpenCV text can cover nearby boxes.
        clean_class = str(class_name or "marine life").replace("_", " ").strip()
        if len(clean_class) > 18:
            clean_class = clean_class[:17] + "…"
        label = f"{clean_class} {max(0.0, min(1.0, float(confidence))) * 100:.0f}%"

        font_scale = max(0.34, min(0.46, width / 1800.0))
        thickness = 1
        (text_w, text_h), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        pad_x, pad_y = 5, 4
        label_w = min(text_w + (pad_x * 2), max(20, width - x1 - 2))
        label_h = text_h + baseline + (pad_y * 2)

        # Put the label inside the box when possible; otherwise keep it at the top edge.
        label_x1 = max(0, min(x1, width - label_w - 1))
        label_y1 = y1 + 1 if (y1 + label_h + 2) < height else max(0, y1 - label_h - 1)
        label_x2 = min(width - 1, label_x1 + label_w)
        label_y2 = min(height - 1, label_y1 + label_h)

        cv2.rectangle(image, (label_x1, label_y1), (label_x2, label_y2), (0, 0, 0), -1)
        cv2.putText(
            image,
            label,
            (label_x1 + pad_x, max(label_y1 + text_h + pad_y, text_h + pad_y)),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
    except Exception:
        pass
