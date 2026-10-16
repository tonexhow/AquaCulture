"""Summary persistence and safety alert regressions, using temporary storage only."""
import copy
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.settings.notification import alert_system as alerts
from server.settings.notification.summary_scheduler import SummarySchedule, tick


class NotificationScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "configuration.json"
        self.patcher = patch.object(alerts, "CONFIG_PATH", self.config)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        alerts._PENDING.clear()
        alerts._LAST_SENT_AT.clear()
        alerts._LAST_SAMPLE_ID = None

    def test_defaults_edit_persist_longer_intervals_and_preserve_existing(self):
        self.assertEqual(alerts.get_notification_settings()["Summary"], 86400)
        for value in (3600, 21600, 43200, 86400, 172800, -1):
            alerts.save_notification_settings({"Summary": value})
            self.assertEqual(alerts.get_notification_settings()["Summary"], value)
        alerts.save_notification_settings({"Summary": 3600})
        alerts.save_notification_settings({"Email": 0})
        self.assertEqual(alerts.get_notification_settings()["Summary"], 3600)

    def test_reject_invalid_intervals_without_overwriting(self):
        alerts.save_notification_settings({"Summary": 172800})
        for value in (None, True, 0, -2, 1.5, "nonsense", 31536001, float("nan")):
            with self.assertRaises(ValueError, msg=repr(value)):
                alerts.save_notification_settings({"Summary": value})
        self.assertEqual(alerts.get_notification_settings()["Summary"], 172800)

    def test_schedule_restart_duplicate_and_interval_calculation(self):
        path = Path(self.temp.name) / "schedule.sqlite3"
        self.assertFalse(SummarySchedule(path).claim(86400, 100))
        self.assertFalse(SummarySchedule(path).claim(86400, 86499))
        self.assertTrue(SummarySchedule(path).claim(86400, 86500))
        self.assertFalse(SummarySchedule(path).claim(86400, 86500))
        self.assertFalse(SummarySchedule(path).claim(172800, 172900))
        self.assertTrue(SummarySchedule(path).claim(172800, 259300))
        self.assertFalse(SummarySchedule(path).claim(-1, 999999))

    def test_concurrent_scheduler_claim_only_once(self):
        path = Path(self.temp.name) / "schedule.sqlite3"
        SummarySchedule(path).claim(10, 100)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: SummarySchedule(path).claim(10, 110), range(8)))
        self.assertEqual(sum(results), 1)

    def test_summary_tick_channels_and_no_threshold_path(self):
        alerts.save_notification_settings({"Summary": 10, "SMS": 0})
        schedule = SummarySchedule(Path(self.temp.name) / "schedule.sqlite3")
        snapshot = lambda: {"ok": False, "reading": None}
        with patch.object(alerts, "get_subscribers_grouped", return_value={"EMAIL": ["test@example.invalid"], "SMS": []}), patch.object(alerts, "send_email_alert") as mail, patch.object(alerts, "send_alerts") as safety:
            self.assertFalse(tick(schedule, snapshot, 100))
            self.assertTrue(tick(schedule, snapshot, 110))
            self.assertFalse(tick(SummarySchedule(schedule.path), snapshot, 110))
            mail.assert_called_once()
            safety.assert_not_called()
            self.assertIn("Unavailable", mail.call_args.args[2])

    def test_http_admin_authorization_validation_and_persistence(self):
        import importlib
        from flask import Flask
        sys.path.insert(0, str(ROOT / "server"))
        from server import api_manager
        route_alerts = importlib.import_module("settings.notification.alert_system")
        app = Flask(__name__, template_folder=str(ROOT / "client/templates"))
        api_manager.register_routes(app)
        client = app.test_client()
        with patch.object(api_manager, "read_admin_session", return_value=None):
            self.assertEqual(client.patch("/api/admin/notification", json={"Summary": 172800}).status_code, 401)
        with patch.object(api_manager, "read_admin_session", return_value={"admin": True}), patch.object(route_alerts, "CONFIG_PATH", self.config), patch.object(route_alerts, "get_alert_status", return_value={}):
            response = client.patch("/api/admin/notification", json={"Summary": 172800})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/admin/notification").get_json()["notification"]["Summary"], 172800)
            self.assertEqual(client.patch("/api/admin/notification", json={"Summary": 0}).status_code, 400)
        settings = (ROOT / "client/templates/settings.html").read_text(encoding="utf-8")
        self.assertNotIn("networkSettingsNavLink", settings)
        for tab in ("notification", "species", "database", "library", "access"):
            self.assertIn(f'data-tab="{tab}"', settings)

    def test_missing_stale_and_malformed_never_send(self):
        samples = ({}, {"temperature": None}, {"temperature": "bad"}, {"temperature": float("nan")},
                   {"temperature": 99}, {"temperature": 40, "connected": False},
                   {"temperature": 40, "stale": True}, {"temperature": 40, "received_epoch": 1})
        with patch.object(alerts, "send_alerts") as send:
            for reading in samples:
                for _ in range(3):
                    self.assertFalse(alerts.process_sensor_reading(reading)["sent"])
            send.assert_not_called()

    def test_legitimate_zero_values_and_immediate_safety_hold_gap(self):
        self.assertEqual(alerts.normalize_reading({"turbidity": 0})["Turbidity"], 0)
        self.assertEqual(alerts.normalize_reading({"ammonia": 0})["Ammonia"], 0)
        self.assertEqual(alerts.normalize_reading({"oxygen": 0, "valid": {"oxygen": True}})["Oxygen"], 0)
        settings = copy.deepcopy(alerts.DEFAULT_NOTIFICATION)
        settings.update({"Summary": 172800, "Hold": 1, "Gap": 60})
        with patch.object(alerts, "get_notification_settings", return_value=settings), patch.object(alerts, "send_alerts", return_value={"ok": True, "sent": True}), patch.object(alerts, "save_log"), patch.object(alerts.time, "time", return_value=100):
            self.assertFalse(alerts.process_sensor_reading({"temperature": 40, "seq": 1})["sent"])
        with patch.object(alerts, "get_notification_settings", return_value=settings), patch.object(alerts, "send_alerts", return_value={"ok": True, "sent": True}) as send, patch.object(alerts, "save_log"), patch.object(alerts.time, "time", return_value=102):
            self.assertTrue(alerts.process_sensor_reading({"temperature": 40, "seq": 2})["sent"])
            self.assertFalse(alerts.process_sensor_reading({"temperature": 40, "seq": 2})["sent"])
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
