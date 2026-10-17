"""Scanner pipeline regression suite that runs without hardware, models, or DB."""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import cv2
import numpy as np

from scanner import size_detection as scan
from scanner import marine_ncnn_worker as worker


def jpeg(value: int = 90) -> bytes:
    image = np.full((120, 160, 3), value, dtype=np.uint8)
    cv2.rectangle(image, (35, 30), (78, 67), (30, 150, 90), 2)
    ok, compressed = cv2.imencode('.jpg', image)
    assert ok
    return bytes(compressed)


class MockCamera:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots if snapshots is not None else [
            {"image_bytes": jpeg(45 + 9 * i), "frame_number": i + 1,
             "timestamp": f"2026-10-17T10:00:{i:02d}"}
            for i in range(12)
        ]
        self.durations = []

    def get_status(self):
        return {"mode": "mock", "connected": True}

    def capture_burst(self, *, duration_seconds, target_fps, sensor_manager,
                      cancel_checker, progress_callback):
        self.durations.append(duration_seconds)
        progress_callback(duration_seconds / 2, len(self.snapshots) // 2)
        progress_callback(duration_seconds, len(self.snapshots))
        return self.snapshots


def empty_inference(selected, top, deadline, cancel_checker, progress_callback):
    images = []
    for idx, frame in enumerate(selected, 1):
        image_id = scan._safe_image_id(frame["selected_id"], idx)
        record = {"image_id": image_id, "frame_number": frame["frame_number"],
                  "object_count": 0, "objects": [], "model_attempts": []}
        progress_callback(record, idx, len(selected), frame["image_bytes"])
        images.append(record)
    return {"objects": [], "images": images, "timed_out": False,
            "model_failed": False, "detector_used": "mock_ncnn"}


class VisualPipelineTests(unittest.TestCase):
    def setUp(self):
        # Background tests wait for threads before proceeding.
        self.assertFalse(scan.get_prediction_status()["running"])

    def test_3_seconds_controls_recording_only(self):
        cam = MockCamera()
        with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None), \
             patch.object(scan, "analyze_selected_snapshots", side_effect=empty_inference):
            result = scan.start_prediction(duration_seconds=3, top=5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(cam.durations, [3.0])
        self.assertEqual(result["result"]["scan_limit_seconds"], 3)
        self.assertEqual(result["result"]["selected_snapshot_count"], 5)
        self.assertEqual(len(result["result"]["images"]), 5)
        status = scan.get_prediction_status()
        self.assertEqual(status["stage"], "complete")
        self.assertEqual(status["progress"], 100)
        self.assertEqual(status["frames_done"], 5)
        self.assertEqual(len(status["frames"]), 12)
        self.assertEqual(sum(int(f["status"] == "best") for f in status["frames"]), 1)
        self.assertTrue(all(f["preview"].startswith("data:image/jpeg") for f in status["frames"]))

    def test_5_and_10_frames_are_supported(self):
        cam = MockCamera()
        for top in (5, 10):
            with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None), \
                 patch.object(scan, "analyze_selected_snapshots", side_effect=empty_inference):
                result = scan.start_prediction(duration_seconds=3, top=top)
            self.assertEqual(result["result"]["selected_snapshot_count"], top)

    def test_blank_readable_frame_not_rejected(self):
        ok, image = cv2.imencode('.jpg', np.full((160, 160, 3), 255, dtype=np.uint8))
        self.assertTrue(ok)
        selected = scan.select_best_snapshots([{"frame_number": 1, "image_bytes": image.tobytes()}], maximum=5)
        self.assertEqual(len(selected), 1)

    def test_exact_duplicate_frames_are_removed(self):
        identical = jpeg()
        selected = scan.select_best_snapshots([{"frame_number": i, "image_bytes": identical} for i in range(8)], maximum=5)
        self.assertEqual(len(selected), 1)

    def test_background_job_is_immediate_and_pollable(self):
        cam = MockCamera()
        started = threading.Event()
        release = threading.Event()

        def slow_inference(selected, top, deadline, cancel_checker, progress_callback):
            started.set()
            self.assertTrue(release.wait(3))
            return empty_inference(selected, top, deadline, cancel_checker, progress_callback)

        with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None), \
             patch.object(scan, "analyze_selected_snapshots", side_effect=slow_inference):
            response = scan.start_prediction_background(duration_seconds=3, top=5)
            self.assertTrue(response["accepted"])
            self.assertTrue(started.wait(2))
            self.assertTrue(scan.get_prediction_status()["running"])
            second = scan.start_prediction_background(duration_seconds=3)
            self.assertFalse(second["ok"])
            release.set()
            until = time.monotonic() + 5
            while scan.get_prediction_status()["running"] and time.monotonic() < until:
                time.sleep(.03)
            self.assertFalse(scan.get_prediction_status()["running"])
            self.assertEqual(scan.get_prediction_status()["stage"], "complete")
            self.assertEqual(scan.get_prediction_status()["job_id"], response["job_id"])

    def test_cancel_stops_processing_and_resets_busy_lock(self):
        cam = MockCamera()
        started = threading.Event()
        def blocked(selected, top, deadline, cancel_checker, progress_callback):
            started.set()
            until = time.monotonic() + 3
            while not cancel_checker() and time.monotonic() < until:
                time.sleep(.015)
            return {"objects": [], "images": [], "timed_out": True}
        with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None), \
             patch.object(scan, "analyze_selected_snapshots", side_effect=blocked):
            self.assertTrue(scan.start_prediction_background(top=5)["ok"])
            self.assertTrue(started.wait(2))
            self.assertTrue(scan.cancel_prediction()["cancel_requested"])
            until = time.monotonic() + 4
            while scan.get_prediction_status()["running"] and time.monotonic() < until:
                time.sleep(.03)
            self.assertFalse(scan.get_prediction_status()["running"])
            self.assertEqual(scan.get_prediction_status()["stage"], "cancelled")

    def test_failed_model_is_not_reported_as_empty_scene(self):
        cam = MockCamera()
        def failed(selected, top, deadline, cancel_checker, progress_callback):
            return {"objects": [], "images": [], "model_failed": True,
                    "model_errors": ["missing runtime"], "timed_out": False}
        with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None), \
             patch.object(scan, "analyze_selected_snapshots", side_effect=failed):
            result = scan.start_prediction(duration_seconds=3)
        self.assertFalse(result["ok"])
        self.assertEqual(scan.get_prediction_status()["stage"], "failed")
        self.assertIn("model", result["message"].lower())

    def test_no_camera_frames_is_terminal_error(self):
        cam = MockCamera([])
        with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None):
            result = scan.start_prediction(duration_seconds=3)
        self.assertFalse(result["ok"])
        self.assertFalse(scan.get_prediction_status()["running"])
        self.assertEqual(scan.get_prediction_status()["stage"], "failed")

    def test_ai_processing_timeout_does_not_claim_no_fish(self):
        cam = MockCamera()
        with patch.object(scan, "camera_module", cam), patch.object(scan, "db", None), \
             patch.object(scan, "analyze_selected_snapshots", return_value={
                 "objects": [], "images": [], "timed_out": True, "model_failed": False
             }):
            result = scan.start_prediction(duration_seconds=3)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIn("AI analysis timed out", result["message"])

    def test_best_frame_prefers_real_detection_over_sharpness(self):
        selection = [{"selected_id": "selected_001", "quality_score": 99},
                     {"selected_id": "selected_002", "quality_score": 20}]
        analysis = [{"image_id": "IMG-001", "object_count": 0, "objects": []},
                    {"image_id": "IMG-002", "object_count": 1,
                     "objects": [{"confidence": .78}]}]
        self.assertEqual(scan._best_frame_id(analysis, selection), "IMG-002")

    def test_successful_worker_with_zero_boxes_is_not_an_error(self):
        class EmptyYOLO:
            def __init__(self, model_path):
                self.model_path = model_path
            def predict(self, **kwargs):
                return []
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            image_path = Path(folder) / 'blank.jpg'
            image_path.write_bytes(jpeg())
            model_dir = Path(folder) / 'test_model'
            model_dir.mkdir()
            (model_dir / 'model.ncnn.param').write_text('placeholder')
            (model_dir / 'model.ncnn.bin').write_bytes(b'data')
            with patch.object(worker, "_import_runtime", return_value=(cv2, EmptyYOLO, "")):
                outcome = worker.run(image_path, [model_dir], 0.2, 320, 10)
            self.assertTrue(outcome["ok"])
            self.assertEqual(outcome["detections"], [])
            self.assertEqual(outcome["models_used"], ["test_model"])

    def test_worker_reports_failure_if_all_models_raise(self):
        class BrokenModel:
            def __init__(self, path):
                raise RuntimeError("bad model")
        with patch.object(worker, "_import_runtime", return_value=(cv2, BrokenModel, "")):
            result = worker.run(Path('/tmp/never-used.jpg'), [], 0.2, 320, 10)
        # Missing image itself is already a failure; test with a real temporary one below.
        self.assertFalse(result["ok"])
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            image_path = Path(folder) / 'blank.jpg'
            image_path.write_bytes(jpeg())
            model_dir = Path(folder) / 'test_model'
            model_dir.mkdir()
            (model_dir / 'model.ncnn.param').write_text('placeholder')
            (model_dir / 'model.ncnn.bin').write_bytes(b'data')
            with patch.object(worker, "_import_runtime", return_value=(cv2, BrokenModel, "")):
                outcome = worker.run(image_path, [model_dir], 0.2, 320, 10)
            self.assertFalse(outcome['ok'])
            self.assertEqual(outcome['models_used'], [])
            self.assertTrue(outcome['attempts'][0]['error'])

if __name__ == '__main__':
    unittest.main()
