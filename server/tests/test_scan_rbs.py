"""AquaCulture scanner regression tests (no connected hardware or ML weights required)."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "server"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from database.database import AquaCultureDB
from scanner import fish_recommendation as rbs
from scanner import size_detection as vision
from sensors import SensorManager, normalize_sensor_payload


class RBSTests(unittest.TestCase):
    @staticmethod
    def candidate(**rules):
        return {
            "id": 1, "name": "Fish A", "image": None,
            "temperature": rules.get("temperature", [20, 30, 40]),
            "ph": rules.get("ph", [6, 8, 35]),
            "oxygen": rules.get("oxygen", [5, 15, 25]),
            "turbidity": rules.get("turbidity", []),
            "ammonia": rules.get("ammonia", [0, 0.05, 20]),
        }

    def test_center_distance_example(self):
        detail = rbs.score_range_rule(29.5, [21, 32, 30])
        self.assertAlmostEqual(detail["suitability_percentage"], 45.45, places=2)
        self.assertEqual(rbs.score_range_rule(50, [21, 32, 30])["suitability_percentage"], 0)

    def test_weighted_rbs_81_percent_and_mq_exclusion(self):
        input_reading = {"temperature": 26, "ph": 7.1, "oxygen": 8.5, "ammonia": 1.0}
        fish = rbs.score_species(input_reading, [self.candidate()])[0]
        self.assertAlmostEqual(fish["percentage"], 81.0, places=2)
        self.assertEqual(fish["compared_water_parameter_count"], 3)
        self.assertNotIn("ammonia", fish["used_sensors"])
        self.assertEqual(fish["gas_risk_applied"], False)
        self.assertAlmostEqual(fish["confidence_percentage"], 100.0)

    def test_missing_sensor_not_penalized_as_zero(self):
        fish = rbs.score_species({"temperature": 26, "ph": 7.1, "ammonia": 1.0}, [self.candidate()])[0]
        self.assertAlmostEqual(fish["percentage"], (32 + 31.5) / 75 * 100, places=2)
        self.assertAlmostEqual(fish["confidence_percentage"], 75.0)
        self.assertIn("oxygen", fish["ignored_sensors"])
        self.assertIn("unverified", fish["safety_status"])

    def test_critical_oxygen_cannot_be_hidden_by_high_scores(self):
        fish = rbs.score_species({"temperature": 25, "ph": 7, "oxygen": 1}, [self.candidate()])[0]
        self.assertEqual(fish["safety_status"], "critical")
        self.assertTrue(any("oxygen" in warning.lower() for warning in fish["critical_warnings"]))
        self.assertGreater(fish["percentage"], 50)

    def test_isolated_spikes_are_removed_from_average(self):
        samples = [
            {"temperature": 28, "ph": 7, "oxygen": 6},
            {"temperature": 28, "ph": 7, "oxygen": 6},
            {"temperature": 42, "ph": 7, "oxygen": 6},
        ]
        summary = rbs.analyze_water_readings(samples)
        self.assertEqual(summary["average"]["temperature"], 28)
        self.assertEqual(summary["stability"]["temperature"]["rejected_spikes"], 1)
        self.assertTrue(summary["unstable"])

    def test_insufficient_samples_explicitly_warned(self):
        summary = rbs.analyze_water_readings([{"temperature": 28, "ph": 7, "oxygen": 6}])
        self.assertEqual(summary["stability"]["temperature"]["status"], "insufficient_samples")
        self.assertTrue(summary["warning_message"])

    def test_three_sensors_including_gas_can_pass_hardware_gate(self):
        fish = self.candidate()
        with patch.object(rbs, "load_species_rows", return_value=[fish]):
            report = rbs.test_recommendation({"temperature": 26, "ph": 7, "ammonia_gas_ppm": 2})
        self.assertTrue(report["ok"], report)
        result = report["result"]
        self.assertEqual(result["available_sensor_count"], 3)
        self.assertEqual(result["water_parameter_count"], 2)
        self.assertNotIn("ammonia", result["suggestions"][0]["used_sensors"])

    def test_live_scan_duration_clamped_and_gas_saved_separately(self):
        class FakeManager:
            def __init__(self):
                self.kwargs = None
            def read_water_for_duration(self, **kwargs):
                self.kwargs = kwargs
                return {"ok": True, "readings": [
                    {"temperature": 26, "ph": 7.1, "ammonia_gas_ppm": 2.5},
                    {"temperature": 26, "ph": 7.1, "ammonia_gas_ppm": 2.5},
                    {"temperature": 26, "ph": 7.1, "ammonia_gas_ppm": 2.5},
                ], "errors": []}
        class FakeDB:
            def __init__(self):
                self.data = None
            def insert(self, name, data):
                assert name == "water_reading"
                self.data = data
                return 12
        from types import SimpleNamespace
        fake_manager, fake_db = FakeManager(), FakeDB()
        with patch.object(rbs, "sensor_module", SimpleNamespace(sensor_manager=fake_manager)), \
             patch.object(rbs, "db", fake_db), \
             patch.object(rbs, "load_species_rows", return_value=[self.candidate()]):
            result = rbs.scan_water_recommendation(duration_seconds=12, top=28)
        self.assertTrue(result["ok"], result)
        self.assertEqual(fake_manager.kwargs["duration_seconds"], 5)
        self.assertEqual(result["result"]["top"], 10)
        self.assertEqual(result["result"]["water_reading_id"], 12)
        self.assertIsNone(fake_db.data["ammonia"])
        self.assertEqual(fake_db.data["ammonia_gas_ppm"], 2.5)
        self.assertFalse(rbs.get_recommendation_status()["running"])

    def test_two_operational_sensors_rejected(self):
        result = rbs.test_recommendation({"temperature": 25, "ph": 7})
        self.assertFalse(result["ok"])
        self.assertEqual(result["minimum_sensor_count"], 3)

    def test_nan_infinity_invalid_not_counted(self):
        report = rbs.test_recommendation({"temperature": "nan", "ph": "inf", "oxygen": 6})
        self.assertFalse(report["ok"])

    def test_real_species_dataset_weighted_scoring_smoke(self):
        species = rbs.load_species_rows()
        self.assertGreater(len(species), 0)
        result = rbs.score_species({"temperature": 27, "ph": 7.5, "oxygen": 5.0}, species)
        self.assertGreater(len(result), 0)
        self.assertTrue(all(0 <= item["percentage"] <= 100 for item in result))
        self.assertTrue(all("ammonia" not in item["used_sensors"] for item in result))


class SensorConnectionTests(unittest.TestCase):
    def test_nano_ammonia_gas_key_calibrated(self):
        packet = {
            "temperature_c": 27.1, "ph": 7.2, "ammonia_gas_ppm": 3.2,
            "calibrated": {"ammonia_gas": True, "ph": True},
        }
        reading = normalize_sensor_payload(packet)
        self.assertEqual(reading["ammonia_gas_ppm"], 3.2)
        self.assertEqual(reading["ammonia_medium"], "gas")
        self.assertEqual(reading["usable_sensors"], 3)
        self.assertTrue(reading["ready_for_recommendation"])

    def test_uncalibrated_mq_does_not_count(self):
        reading = normalize_sensor_payload({
            "temperature_c": 27, "ph": 7, "ammonia_gas_ppm": 100,
            "calibrated": {"ammonia_gas": False},
        })
        self.assertIsNone(reading["ammonia"])
        self.assertEqual(reading["usable_sensors"], 2)

    def test_arduino_calibration_flags_exclude_disabled_water_channels(self):
        packet = {
            "temperature_c": 27, "ph": 7, "oxygen_mg_l": 6,
            "turbidity_ntu": 25, "ammonia_gas_ppm": 1,
            "calibrated": {"ph": False, "oxygen": False, "turbidity": False, "ammonia_gas": False},
        }
        reading = normalize_sensor_payload(packet)
        self.assertEqual(reading["usable_sensors"], 1)
        self.assertFalse(reading["ready_for_recommendation"])

    def test_monotonic_hard_deadline_with_no_serial_data(self):
        class Clock:
            t = 0.0
            def monotonic(self):
                return self.t
            def wait(self, duration):
                self.t += duration
                return False
        clock = Clock()
        manager = SensorManager()
        manager.state.running = True
        manager._stop_event.wait = clock.wait
        manager.get_latest = lambda allow_stale=False: {"ok": False, "message": "No Nano connected."}
        with patch("sensors.time.monotonic", side_effect=clock.monotonic):
            result = manager.read_water_for_duration(duration_seconds=3, sample_gap_seconds=1)
        self.assertFalse(result["ok"])
        self.assertEqual(clock.t, 3)
        self.assertEqual(result["readings"], [])


class DatabaseCompatibilityTests(unittest.TestCase):
    def test_legacy_database_migration_is_non_destructive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE water_reading (id INTEGER PRIMARY KEY, creation TEXT NOT NULL, temperature REAL, ammonia REAL)")
                conn.execute("INSERT INTO water_reading(creation,temperature,ammonia) VALUES('legacy',28,0.01)")
            db = AquaCultureDB(path)
            db.create_tables()
            db.create_tables()  # idempotent
            with closing(sqlite3.connect(path)) as conn, conn:
                columns = {col[1] for col in conn.execute("PRAGMA table_info(water_reading)")}
                self.assertIn("ammonia_gas_ppm", columns)
                row = conn.execute("SELECT creation,ammonia,ammonia_gas_ppm FROM water_reading").fetchone()
                self.assertEqual(row, ("legacy", 0.01, None))


class FishScanDeadlineTests(unittest.TestCase):
    def test_empty_scene_completes_without_zip_or_database(self):
        class FakeCamera:
            def get_status(self):
                return {"mode": "mock_camera", "connected": True}
            def capture_burst(self, **kwargs):
                return [{"image_bytes": b"frame", "frame_number": 1, "timestamp": "now"}]
        empty_detection = {"objects": [], "images": [], "timed_out": False, "detector_used": "test"}
        with patch.object(vision, "camera_module", FakeCamera()), \
             patch.object(vision, "db", None), \
             patch.object(vision, "select_best_snapshots", return_value=[{"image_bytes": b"frame"}]), \
             patch.object(vision, "analyze_selected_snapshots", return_value=empty_detection):
            result = vision.start_prediction(duration_seconds=3, top=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["object_count"], 0)
        self.assertEqual(result["result"]["scan_limit_seconds"], 3)
        self.assertFalse(result["result"]["download_available"])
        self.assertFalse(vision.get_prediction_status()["running"])

    def test_slow_model_reports_terminal_timeout(self):
        class FakeCamera:
            def get_status(self):
                return {"mode": "mock_camera", "connected": True}
            def capture_burst(self, **kwargs):
                return [{"image_bytes": b"frame", "frame_number": 1}]
        with patch.object(vision, "camera_module", FakeCamera()), \
             patch.object(vision, "db", None), \
             patch.object(vision, "select_best_snapshots", return_value=[{"image_bytes": b"frame"}]), \
             patch.object(vision, "analyze_selected_snapshots", return_value={"objects": [], "timed_out": True}):
            result = vision.start_prediction(duration_seconds=3)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertFalse(vision.get_prediction_status()["running"])

    def test_real_child_can_be_cancelled_while_running(self):
        if vision.cv2 is None or vision.np is None:
            self.skipTest("OpenCV not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "marine_ncnn_worker.py").write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            image = vision.np.zeros((32, 32, 3), dtype=vision.np.uint8)
            started = time.monotonic()
            with patch.object(vision, "BASE_DIR", root):
                _, _, data = vision._detect_with_marine_ncnn_worker(
                    image, [root / "fake.pt"], deadline=time.monotonic() + 5,
                    cancel_checker=lambda: time.monotonic() - started > 0.25,
                )
            self.assertTrue(data.get("cancelled"), data)
            self.assertLess(time.monotonic() - started, 2)

    def test_real_child_process_killed_on_budget(self):
        if vision.cv2 is None or vision.np is None:
            self.skipTest("OpenCV not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "marine_ncnn_worker.py").write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            image = vision.np.zeros((64, 64, 3), dtype=vision.np.uint8)
            start = time.monotonic()
            with patch.object(vision, "BASE_DIR", root):
                _, label, meta = vision._detect_with_marine_ncnn_worker(
                    image, [root / "fake.pt"], deadline=time.monotonic() + 0.45
                )
            elapsed = time.monotonic() - start
            self.assertTrue(meta.get("timed_out"), (label, meta))
            self.assertLess(elapsed, 2.0, (elapsed, meta))


if __name__ == "__main__":
    unittest.main()
