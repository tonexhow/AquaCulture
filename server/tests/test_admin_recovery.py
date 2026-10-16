"""Authentication/recovery regressions. Synthetic accounts, SMTP mocked, temp DBs."""
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "server")]
from aquaculture_auth import CredentialStore, DurableLimiter, RateLimited, AuthUnavailable, GENERIC_RECOVERY, connection
from server import api_manager as api
from settings import admin_password, password_recovery as recovery

PASSWORD = "Existing Admin 123!"
NEW_PASSWORD = "New Admin password 456!"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = CredentialStore(self.path / "admin.sqlite3")
        self.store.initialize(password=PASSWORD, email="admin@example.test")
        self.env = patch.dict(os.environ, {"AQUACULTURE_AUTH_DB": str(self.store.path), "AQUACULTURE_PUBLIC_ORIGIN": "https://aqua.example.test"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.session_patch = patch.object(api, "ADMIN_SESSION_STORE", self.path / "sessions.json")
        self.session_patch.start(); self.addCleanup(self.session_patch.stop)
        api.ADMIN_SESSIONS.clear(); api.ADMIN_LOGIN_ATTEMPTS.clear(); api.ADMIN_SESSIONS_LOADED = True
        from flask import Flask
        app = Flask(__name__, template_folder=str(ROOT / "client/templates"), static_folder=str(ROOT / "client/static"))
        app.config["TESTING"] = True
        api.register_routes(app)
        self.app, self.client = app, app.test_client()

    @staticmethod
    def immediate(function, *args):
        future = Future()
        try: future.set_result(function(*args))
        except Exception as error: future.set_exception(error)
        return future

    def request_token(self, email="admin@example.test", success=True):
        with patch.object(recovery._sender, "submit", side_effect=self.immediate), patch.object(recovery, "send_email_alert", return_value={"ok": success}) as smtp:
            response = self.client.post("/api/admin/password/forgot", json={"email": email})
        token = None
        if smtp.called:
            token = parse_qs(urlsplit(smtp.call_args.kwargs["text_body"].split(": ", 1)[1]).query)["token"][0]
        return response, token, smtp

    def test_generic_existing_unknown_and_existing_smtp_configuration(self):
        known, token, smtp = self.request_token()
        unknown, other, no_mail = self.request_token("unknown@example.test")
        self.assertEqual(known.get_json(), unknown.get_json())
        self.assertEqual(known.get_json()["message"], GENERIC_RECOVERY)
        self.assertIsNone(other); no_mail.assert_not_called()
        self.assertIn("https://aqua.example.test/reset-password?token=", smtp.call_args.kwargs["text_body"])
        with connection(self.store.path) as conn:
            row = conn.execute("SELECT digest,expires_at,used_at FROM reset_tokens").fetchone()
            self.assertEqual(row[0], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(row[0], token); self.assertIsNone(row[2])

    def test_smtp_failure_generic_and_token_invalidated(self):
        response, token, _ = self.request_token(success=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], GENERIC_RECOVERY)
        self.assertFalse(self.store.reset(token, NEW_PASSWORD, NEW_PASSWORD))

    def test_expired_reused_invalid_mismatch_and_disabled(self):
        token = self.store.issue_reset("admin@example.test", now=1000)
        self.assertFalse(self.store.reset(token, NEW_PASSWORD, NEW_PASSWORD, now=1900))
        self.assertFalse(self.store.reset("not-a-token", NEW_PASSWORD, NEW_PASSWORD))
        with self.assertRaises(ValueError): self.store.reset(token, NEW_PASSWORD, "different", now=1100)
        self.assertTrue(self.store.reset(token, NEW_PASSWORD, NEW_PASSWORD, now=1100))
        self.assertFalse(self.store.reset(token, NEW_PASSWORD, NEW_PASSWORD, now=1101))
        with connection(self.store.path) as conn: conn.execute("UPDATE admin SET enabled=0")
        self.assertIsNone(self.store.issue_reset("admin@example.test"))

    def test_atomic_single_use_and_other_tokens_invalidated(self):
        token = self.store.issue_reset("admin@example.test")
        other = self.store.issue_reset("admin@example.test")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.reset(token, NEW_PASSWORD, NEW_PASSWORD), range(2)))
        self.assertEqual(sum(results), 1)
        self.assertFalse(self.store.reset(other, NEW_PASSWORD, NEW_PASSWORD))
        self.assertIsNotNone(self.store.verify(NEW_PASSWORD)); self.assertIsNone(self.store.verify(PASSWORD))

    def test_http_reset_revokes_existing_and_persisted_sessions(self):
        login = self.client.post("/api/admin/password/verify", json={"password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        token = login.get_json()["admin_token"]
        headers = {"X-AquaCulture-Admin-Token": token}
        self.assertTrue(self.client.get("/api/admin/session", headers=headers).get_json()["session_active"])
        reset = self.store.issue_reset("admin@example.test")
        response = self.client.post("/api/admin/password/reset", json={"token": reset, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/api/admin/session", headers=headers).get_json()["session_active"])
        self.assertEqual(json.loads((self.path / "sessions.json").read_text()), {})

    def test_settings_password_change_and_recovery_email_require_current(self):
        login = self.client.post("/api/admin/password/verify", json={"password": PASSWORD}).get_json()
        headers = {"X-AquaCulture-Admin-Token": login["admin_token"]}
        email = self.client.patch("/api/admin/recovery-email", headers=headers, json={"email": "new@example.test", "current_password": PASSWORD})
        self.assertEqual(email.status_code, 200)
        self.assertEqual(self.store.state()["email"], "new@example.test")
        changed = self.client.patch("/api/admin/password", headers=headers, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(self.client.get("/api/admin/session", headers=headers).get_json()["session_active"])

    def test_rate_limited_reset_requests_and_safe_origin(self):
        for _ in range(3): self.request_token("unknown@example.test")
        response, _, _ = self.request_token("unknown@example.test")
        self.assertEqual(response.status_code, 429); self.assertIn("Retry-After", response.headers)
        for origin in ("", "http://example.test", "https://user@example.test", "https://example.test/path", "https://example.test?token=x", "https://example.test\\evil"):
            with patch.dict(os.environ, {"AQUACULTURE_PUBLIC_ORIGIN": origin}), self.assertRaises(ValueError): recovery.reset_origin()

    def test_recovery_pages_no_cache_referrer_and_access_log_redaction(self):
        response = self.client.get("/reset-password?token=synthetic-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("synthetic-token", response.get_data(as_text=True))
        record = logging.LogRecord("werkzeug", 20, "", 0, '"%s"', ("GET /reset-password?token=synthetic-token HTTP/1.1",), None)
        recovery.ResetTokenRedaction().filter(record)
        self.assertNotIn("synthetic-token", record.getMessage())

    def test_legacy_migration_preserves_other_settings_and_removes_plaintext(self):
        legacy = self.path / "configuration.json"
        legacy.write_text(json.dumps({"password": PASSWORD, "notification": {"Summary": 172800}, "theme": {"accent": "teal"}}), encoding="utf-8")
        original_mode = legacy.stat().st_mode
        target = CredentialStore(self.path / "migrated.sqlite3")
        target.initialize(legacy)
        self.assertIsNotNone(target.verify(PASSWORD))
        data = json.loads(legacy.read_text())
        self.assertNotIn("password", data); self.assertEqual(data["notification"]["Summary"], 172800)
        self.assertNotIn(PASSWORD.encode(), target.path.read_bytes())
        self.assertEqual(legacy.stat().st_mode, original_mode)
        # A failed first initialization can leave an empty schema. The physical
        # recovery CLI must initialize its account rather than UPDATE zero rows.
        empty = CredentialStore(self.path / "empty.sqlite3")
        with self.assertRaises(AuthUnavailable): empty.initialize()
        import aquaculture_auth
        with patch.object(sys, "argv", ["aquaculture_auth.py", "--database", str(empty.path), "--set-password"]), patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]), patch.object(os, "geteuid", return_value=0, create=True), patch("builtins.print"):
            aquaculture_auth.main()
        self.assertIsNotNone(empty.verify(NEW_PASSWORD))


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "limits.sqlite3"

    def test_100_rolling_ceiling_survives_restart_and_identifier_rotation(self):
        for index in range(100):
            limiter = DurableLimiter(self.path)
            slot = limiter.reserve("ble_admin", "rotated-" + str(index), str(index), now=1000 + index)
            limiter.finish("ble_admin", slot, True, now=1000 + index)
        with self.assertRaises(RateLimited): DurableLimiter(self.path).reserve("ble_admin", "another", "101", now=1100)
        DurableLimiter(self.path).reserve("ble_admin", "another", "102", now=87400)

    def test_short_window_progressive_delays_and_deduplication(self):
        limiter = DurableLimiter(self.path)
        for index in range(5):
            now = 1000 + 20 * index
            reservation = limiter.reserve("ble_admin", "phone", str(index), now=now)
            delay = limiter.finish("ble_admin", reservation, False, now=now)
            self.assertEqual(delay, 2 ** index)
            with self.assertRaises(RateLimited): limiter.reserve("ble_admin", "rotated", "limited-" + str(index), now=now + .1)
        with self.assertRaises(RateLimited): DurableLimiter(self.path).reserve("ble_admin", "rotated", "sixth", now=1200)
        with self.assertRaises(ValueError): limiter.reserve("ble_admin", "phone", "0", now=1200)


if __name__ == "__main__": unittest.main()
