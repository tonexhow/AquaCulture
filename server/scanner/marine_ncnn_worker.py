"""Marine NCNN worker for AquaCulture.

Runs Ultralytics-exported NCNN model folders outside the Flask process.
The worker prints exactly ONE JSON object to stdout so size_detection.py can
parse it reliably. Any ML logs are captured and returned inside the JSON.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


FISH_FAMILY_NAMES = {
    "bolbometopon_muricatum",
    "chaetodontidae",
    "cheilinus_undulatus",
    "cromileptes_altivelis",
    "fish",
    "haemulidae",
    "lutjanidae",
    "muraenidae",
    "scaridae",
    "serranidae",
}

LABEL_MAP = {
    "giant_clam": "clam",
    "giant clam": "clam",
    "clam": "clam",
    "sea_cucumber": "sea cucumber",
    "sea cucumber": "sea cucumber",
    "crown_of_thorns": "starfish",
    "crown of thorns": "starfish",
    "starfish": "starfish",
    "sea star": "starfish",
    "urchin": "urchin",
    "lobster": "lobster",
    "crab": "crab",
    "shrimp": "shrimp",
    "ray": "ray",
    "manta ray": "ray",
    "stingray": "ray",
    "shark": "shark",
    "turtle": "turtle",
    "sea turtle": "turtle",
}

TARGET_LABELS = {
    "fish",
    "clam",
    "sea cucumber",
    "starfish",
    "urchin",
    "lobster",
    "crab",
    "shrimp",
    "ray",
    "shark",
    "turtle",
}


def normalize_label(raw_name: str) -> str:
    clean = str(raw_name or "").strip().lower().replace(" ", "_")
    if clean in FISH_FAMILY_NAMES:
        return "fish"
    mapped = LABEL_MAP.get(clean, clean.replace("_", " ").strip())
    return mapped


def box_iou(a: List[int], b: List[int]) -> float:
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


def merge_detections(detections: List[Dict[str, Any]], iou_threshold: float = 0.50) -> List[Dict[str, Any]]:
    """Class-agnostic NMS so one fish does not appear as many overlapping boxes."""
    detections = sorted(detections, key=lambda d: float(d.get("confidence") or 0.0), reverse=True)
    kept: List[Dict[str, Any]] = []
    for det in detections:
        box = det.get("box_px") or [0, 0, 1, 1]
        if any(box_iou(box, existing.get("box_px") or [0, 0, 1, 1]) >= iou_threshold for existing in kept):
            continue
        kept.append(det)
    return kept


def _import_runtime() -> Tuple[Any, Any, str]:
    """Import heavy ML packages inside the worker only."""
    logs = io.StringIO()
    with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
        import cv2  # type: ignore
        from ultralytics import YOLO  # type: ignore
    return cv2, YOLO, logs.getvalue()


def _safe_log(text: str, limit: int = 3000) -> str:
    text = str(text or "").strip()
    if len(text) > limit:
        return text[-limit:]
    return text


def run(image_path: Path, model_paths: List[Path], conf: float, imgsz: int, max_det: int) -> Dict[str, Any]:
    started = time.time()
    detections: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    models_used: List[str] = []
    raw_count = 0
    ignored_count = 0
    runtime_logs = ""

    try:
        cv2, YOLO, import_logs = _import_runtime()
        runtime_logs += import_logs
    except Exception as error:
        return {
            "ok": False,
            "message": "Marine NCNN runtime import failed.",
            "error": f"{type(error).__name__}: {error}",
            "hint": "Install dependencies in the SAME Python used to run app.py: python -m pip install ultralytics ncnn opencv-python numpy",
            "detections": [],
            "attempts": [],
            "elapsed_seconds": round(time.time() - started, 3),
        }

    image = cv2.imread(str(image_path))
    if image is None:
        return {"ok": False, "message": "Image could not be read.", "detections": [], "attempts": []}

    for model_path in model_paths:
        model_path = model_path.resolve()
        model_name = model_path.name
        attempt = {"model": model_name, "raw_count": 0, "target_count": 0, "ignored_count": 0, "error": "", "log": ""}

        is_ncnn = model_path.is_dir() and (
            model_path / "model.ncnn.param").exists() and (model_path / "model.ncnn.bin").exists()
        is_pt = model_path.is_file() and model_path.suffix.lower() == ".pt"
        if not (is_ncnn or is_pt):
            attempt["error"] = f"Model files missing or unsupported: {model_path}"
            attempts.append(attempt)
            continue

        logs = io.StringIO()
        try:
            with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
                model = YOLO(str(model_path))
                results = model.predict(
                    source=image,
                    conf=conf,
                    imgsz=imgsz,
                    max_det=max_det,
                    verbose=False,
                    device="cpu",
                )
            attempt["log"] = _safe_log(logs.getvalue())
            models_used.append(model_name)

            if not results:
                attempts.append(attempt)
                continue

            result = results[0]
            names = getattr(result, "names", {}) or getattr(model, "names", {}) or {}
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                attempts.append(attempt)
                continue

            for box in boxes:
                try:
                    raw_count += 1
                    attempt["raw_count"] += 1
                    cls_id = int(box.cls[0].item()) if getattr(box, "cls", None) is not None else -1
                    confidence = float(box.conf[0].item()) if getattr(box, "conf", None) is not None else 0.0
                    raw_name = str(names.get(cls_id, f"class_{cls_id}")) if isinstance(names, dict) else f"class_{cls_id}"
                    label = normalize_label(raw_name)
                    if label not in TARGET_LABELS:
                        ignored_count += 1
                        attempt["ignored_count"] += 1
                        continue
                    xyxy = box.xyxy[0].tolist()
                    detections.append({
                        "class_name": label,
                        "raw_class": raw_name,
                        "confidence": confidence,
                        "box_px": [int(round(float(v))) for v in xyxy],
                        "model": model_name,
                    })
                    attempt["target_count"] += 1
                except Exception as box_error:
                    attempt["error"] = attempt["error"] or f"Box parse warning: {type(box_error).__name__}: {box_error}"
                    continue
        except Exception as error:
            attempt["error"] = f"{type(error).__name__}: {error}"
            attempt["log"] = _safe_log(logs.getvalue())
        attempts.append(attempt)

    detections = merge_detections(detections)

    return {
        "ok": True,
        "models_used": models_used,
        "raw_count": raw_count,
        "ignored_count": ignored_count,
        "target_count": len(detections),
        "detections": detections,
        "attempts": attempts,
        "runtime_log": _safe_log(runtime_logs),
        "elapsed_seconds": round(time.time() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--conf", type=float, default=0.20)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--max-det", type=int, default=30)
    args = parser.parse_args()

    try:
        payload = run(
            image_path=Path(args.image),
            model_paths=[Path(p) for p in args.model],
            conf=max(0.05, min(0.95, float(args.conf))),
            imgsz=max(320, min(1280, int(args.imgsz))),
            max_det=max(1, min(100, int(args.max_det))),
        )
    except BaseException as error:
        payload = {
            "ok": False,
            "message": "Marine NCNN worker crashed safely.",
            "error": f"{type(error).__name__}: {error}",
            "detections": [],
            "attempts": [],
        }

    # IMPORTANT: stdout must be JSON only. Flask parent process parses this.
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
