"""Mocked protocol/NetworkManager tests; these do NOT exercise Bluetooth hardware."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

NETWORK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(NETWORK), str(NETWORK / ".dev-deps")]
from rpi.authentication import Authorization, Session, AuthorizationRequired, encode
from rpi.connection_history import ConnectionHistory
from rpi.controller import Controller
from rpi.protocol import HEADER, Reassembler, frames
from rpi.wifi_manager import NM, ROOT, NetworkManager, ProvisionError, normalize_networks, validate_configuration


def authorized(peer="phone"):
    store = Mock()
    store.verify.return_value = "fixture-revision"
    store.state.return_value = {"enabled": 1, "revision": "fixture-revision"}
    limiter = Mock()
    limiter.finish.return_value = 0
    auth = Authorization(store, limiter)
    hello = auth.bootstrap(peer)
    auth.authenticate(peer, auth.sessions[peer], "fixture-auth", "fixture-password")
    return auth, hello


def wire(auth, peer, request, counter):
    return encode({**request, "session": auth.sessions[peer].sid, "counter": counter})


class ProtocolTests(unittest.TestCase):
    def test_real_shared_credential_verification_and_live_password_change(self):
        from aquaculture_auth import CredentialStore, DurableLimiter
        with tempfile.TemporaryDirectory() as temporary:
            store = CredentialStore(Path(temporary) / "admin.sqlite3")
            store.initialize(password="Existing Admin 123!")
            auth = Authorization(store, DurableLimiter(Path(temporary) / "limits.sqlite3"))
            auth.bootstrap("phone")
            session = auth.sessions["phone"]
            self.assertTrue(auth.authenticate("phone", session, "first", "Existing Admin 123!")["authorized"])
            store.change("Existing Admin 123!", "Updated Admin 456!")
            with self.assertRaises(AuthorizationRequired): auth.get("phone")
            self.assertTrue(auth.authenticate("phone", session, "second", "Updated Admin 456!")["authorized"])
            self.assertFalse(auth.authenticate("phone", session, "third", "Existing Admin 123!")["authorized"])
            with self.assertRaises(AuthorizationRequired): auth.get("phone")

    def test_unicode_and_maximum_size_fragment_roundtrip(self):
        for raw in (encode({"ssid": "魚塘 Wi-Fi 🐟"}), b"z" * 32768):
            receiver, result = Reassembler(), None
            for frame in frames(raw, 65535):
                self.assertLessEqual(len(frame), 20)
                result = receiver.feed(frame)
            self.assertEqual(raw, result)
        for raw in (b"", b"z" * 32769):
            with self.assertRaises(ValueError):
                frames(raw, 1)

    def test_incomplete_duplicate_oversize_invalid_and_timeout(self):
        chunks = frames(b"x" * 30, 2)
        receiver = Reassembler()
        receiver.feed(chunks[0], 100)
        with self.assertRaises(ValueError):
            receiver.feed(chunks[0], 101)
        receiver.feed(chunks[0], 100)
        with self.assertRaises(ValueError):
            receiver.feed(chunks[1], 131)
        for raw in (b"bad", HEADER.pack(0xA1, 2, 0, 40000) + b"a", HEADER.pack(0xA2, 2, 0, 1) + b"a"):
            with self.assertRaises(ValueError):
                Reassembler().feed(raw)

    def test_admin_verification_and_session_binding(self):
        auth, hello = authorized()
        self.assertEqual(hello["v"], 2)
        self.assertNotIn("device_id", hello)
        self.assertNotIn("password_hash", json.dumps(hello))
        self.assertTrue(auth.get("phone").revision)
        with self.assertRaises(AuthorizationRequired):
            auth.get("another-phone")
        session = auth.sessions["phone"]
        request = {"id": "one", "session": session.sid, "counter": 1}
        auth.accept("phone", request)
        with self.assertRaises(ValueError):
            auth.accept("phone", request)
        auth.disconnect("phone")
        with self.assertRaises(AuthorizationRequired):
            auth.get("phone")

    def test_expiry_password_change_unavailable_store(self):
        for condition in ("expiry", "change", "unavailable"):
            auth, _ = authorized()
            if condition == "expiry":
                auth.sessions["phone"].expires = 0
            elif condition == "change":
                auth.store.state.return_value = {"enabled": 1, "revision": "new"}
            else:
                auth.store.state.side_effect = OSError("unavailable")
            with self.assertRaises(AuthorizationRequired):
                auth.get("phone")

    def test_wrong_empty_password_and_no_password_cache(self):
        auth, _ = authorized()
        auth.store.verify.return_value = None
        self.assertFalse(auth.authenticate("phone", auth.sessions["phone"], "wrong", "")["authorized"])
        self.assertNotIn("password", vars(auth.sessions["phone"]))


class HistoryAndValidationTests(unittest.TestCase):
    def test_history_is_persistent_bounded_and_password_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.sqlite3"
            history = ConnectionHistory(path, limit=3)
            for index in range(5):
                record = history.add(str(index), "CONNECTING", manual=True)
                history.finish(record, "CONNECTED_LOCAL")
            history.disconnect("4")
            rows = ConnectionHistory(path, 3).recent()
            self.assertEqual(len(rows), 3)
            self.assertIsNotNone(rows[0]["disconnected_at"])
            self.assertTrue(rows[0]["manual"])
            self.assertNotIn("password", json.dumps(rows))

    def test_configuration_validation_hidden_wpa2_wpa3_enterprise(self):
        for mode, password in (("open", ""), ("wpa-psk", "12345678"), ("wpa-psk", "f" * 64), ("sae", "x")):
            self.assertEqual(validate_configuration({"ssid": "Hidden", "security": mode, "password": password, "hidden": True})[3], True)
        for payload in ({"ssid": "x", "security": "wpa-eap"}, {"ssid": "x" * 33, "security": "open"}, {"ssid": "x", "security": "wpa-psk", "password": "short"}, {"ssid": "a\0b", "security": "open"}, {"ssid": "x", "security": "open", "shell": "x"}):
            with self.assertRaises(ProvisionError):
                validate_configuration(payload)

    def test_scan_deduplicates_preserving_security_and_connected_ap(self):
        aps = [("/a", {"Ssid": b"Home", "Strength": 70, "RsnFlags": 0x100}),
               ("/b", {"Ssid": b"Home", "Strength": 30, "RsnFlags": 0x100}),
               ("/c", {"Ssid": b"Home", "Strength": 80, "RsnFlags": 0x400}),
               ("/hidden", {"Ssid": b"", "Strength": 90})]
        rows = normalize_networks(aps, [{"ssid": "Home"}], "/b")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["signal"], 70)
        self.assertTrue(rows[0]["connected"])
        self.assertEqual(rows[0]["access_points"], 2)
        self.assertTrue(rows[0]["saved"])
        self.assertEqual(normalize_networks([]), [])


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_dispatch_duplicate_and_unknown_command(self):
        auth, _ = authorized()
        manager, responses = AsyncMock(), []
        manager.status.return_value = {"state": "DISCONNECTED", "ssid": None}
        async def send(peer, raw, **options):
            responses.append(json.loads(raw))
        controller = Controller(auth, manager, send)
        for index, request in enumerate(({"id": "one", "type": "status", "payload": {}},
                {"id": "one", "type": "status", "payload": {}},
                {"id": "two", "type": "execute_shell", "payload": {}}), 1):
            await controller.handle("phone", wire(auth, "phone", request, index))
        self.assertEqual(responses[0]["status"], "DISCONNECTED")
        self.assertEqual(responses[1]["error"]["category"], "INVALID_REQUEST")
        self.assertEqual(responses[2]["error"]["category"], "INVALID_REQUEST")
        manager.status.assert_awaited_once()

    async def test_missing_authorization_never_reaches_manager(self):
        auth, _ = authorized()
        auth.sessions["phone"].revision = None
        manager, send = AsyncMock(), AsyncMock()
        controller = Controller(auth, manager, send)
        for index, command in enumerate(("info", "history", "status", "scan", "configure"), 1):
            await controller.handle("phone", wire(auth, "phone", {"id": str(index), "type": command, "payload": {}}, index))
        manager.configure.assert_not_called()
        manager.scan.assert_not_called()
        manager.status.assert_not_called()
        for call in send.call_args_list:
            self.assertNotIn("saved_profiles", call.args[1].decode())
            self.assertEqual(json.loads(call.args[1])["error"]["category"], "AUTH_REQUIRED")


class MockNetworkManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = ConnectionHistory(Path(self.temp.name) / "history.sqlite3")
        self.manager = NetworkManager(None, self.history)
        self.root = AsyncMock()
        self.root.call_add_and_activate_connection2.return_value = ("/profile/new", "/active/new", {})
        self.root.call_activate_connection.return_value = "/active/old"
        self.profile = AsyncMock()
        async def iface(path, kind):
            return self.root if path == ROOT else self.profile
        self.manager.iface = iface
        self.manager.device = AsyncMock(return_value=("/device", {"State": 100, "ActiveConnection": "/active/old"}))
        self.manager.props = AsyncMock(return_value={"Connection": "/profile/old"})
        self.progress = AsyncMock()

    async def test_success_saves_only_new_profile_after_connection(self):
        self.manager.wait_connected = AsyncMock()
        self.manager.status = AsyncMock(return_value={"state": "CONNECTED_LOCAL", "ssid": "Home", "ip_address": "192.0.2.1"})
        result = await self.manager.configure({"ssid": "Home", "security": "wpa-psk", "password": "fixture-password"}, self.progress)
        self.assertEqual(result["state"], "CONNECTED_LOCAL")
        self.profile.call_save.assert_awaited_once()
        self.root.call_activate_connection.assert_not_called()
        self.assertEqual(self.history.recent()[0]["status"], "CONNECTED_LOCAL")

    async def test_failure_restores_old_profile_without_saving_or_deleting(self):
        self.manager.wait_connected = AsyncMock(side_effect=[ProvisionError("AUTHENTICATION_REJECTED", "Rejected."), None])
        with self.assertRaisesRegex(ProvisionError, "restored"):
            await self.manager.configure({"ssid": "New", "security": "wpa-psk", "password": "fixture-password"}, self.progress)
        self.root.call_activate_connection.assert_awaited_once_with("/profile/old", "/device", "/")
        self.profile.call_save.assert_not_called()
        self.profile.call_delete.assert_not_called()
        self.assertEqual(self.history.recent()[0]["error_category"], "AUTHENTICATION_REJECTED")

    async def test_parallel_configure_rejected(self):
        await self.manager.operation.acquire()
        try:
            with self.assertRaisesRegex(ProvisionError, "already running"):
                await self.manager.configure({"ssid": "Home", "security": "open"}, self.progress)
        finally:
            self.manager.operation.release()

    async def test_scan_requests_new_results_deduplicates_and_rate_limits(self):
        self.manager.device = AsyncMock(return_value=("/device", {}))
        wireless = AsyncMock()
        wireless.call_get_all_access_points.return_value = ["/ap"]
        self.manager.iface = AsyncMock(return_value=wireless)
        self.manager.props = AsyncMock(side_effect=[{"LastScan": 1}, {"LastScan": 2, "ActiveAccessPoint": "/ap"}, {"Ssid": b"Home", "Strength": 80, "RsnFlags": 0x100}])
        self.manager.saved_profiles = AsyncMock(return_value=[])
        result = await self.manager.scan()
        wireless.call_request_scan.assert_awaited_once_with({})
        self.assertEqual(result[0]["ssid"], "Home")
        self.assertTrue(result[0]["connected"])
        with self.assertRaisesRegex(ProvisionError, "15 seconds"):
            await self.manager.scan()

    async def test_progress_ip_failure_and_timeout_categories(self):
        self.manager.props = AsyncMock(side_effect=[{"State": 60}, {"State": 70}, {"State": 100, "ActiveConnection": "/active/new"}])
        with patch("rpi.wifi_manager.asyncio.sleep", new=AsyncMock()):
            await self.manager.wait_connected("/device", "/active/new", self.progress, timeout=75)
        self.assertEqual([c.args[0] for c in self.progress.await_args_list], ["AUTHENTICATING", "OBTAINING_IP"])
        self.manager.props = AsyncMock(return_value={"State": 120, "StateReason": [120, 17]})
        with self.assertRaises(ProvisionError) as failed:
            await self.manager.wait_connected("/device", "/active/new", self.progress, timeout=75)
        self.assertEqual(failed.exception.category, "IP_ASSIGNMENT_FAILED")
        with self.assertRaises(ProvisionError) as timed_out:
            await self.manager.wait_connected("/device", "/active/new", self.progress, timeout=0)
        self.assertEqual(timed_out.exception.category, "TIMED_OUT")

    async def test_device_local_connectivity_not_inferred_from_global_internet(self):
        self.manager.device = AsyncMock(return_value=("/device", {"State": 100, "ActiveConnection": "/active", "Ip4Config": "/ip", "Ip4Connectivity": 3, "Interface": "wlan0"}))
        async def props(path, interface):
            return {"ActiveAccessPoint": "/ap"} if path == "/device" else {"Ssid": b"Local", "Strength": 70} if path == "/ap" else {"AddressData": [{"address": "192.0.2.2"}]}
        self.manager.props = props
        self.manager.saved_profiles = AsyncMock(return_value=[])
        self.root.call_check_connectivity.return_value = 4  # Ethernet is online, Wi-Fi is not.
        result = await self.manager.status(test_internet=True)
        self.assertEqual(result["state"], "CONNECTED_LOCAL")
        self.assertIs(result["internet_available"], False)

    async def test_bluez_interfaces_have_valid_dbus_signatures(self):
        from rpi.ble_service import Advertisement, Characteristic, GattService, ManagedObjects, PairingAgent
        from rpi.protocol import BOOTSTRAP_UUID
        service = GattService()
        self.assertEqual(service.UUID, "7a1c0001-9e67-4a21-bde4-33c5eab07a01")
        for obj in (service, Advertisement(), Characteristic(BOOTSTRAP_UUID, ["read"], None), PairingAgent(), ManagedObjects({})):
            self.assertTrue(obj.introspect().name.startswith("org."))

    async def test_private_responses_are_peer_scoped_and_revoked_on_password_change(self):
        from rpi.ble_service import BleService
        auth, _ = authorized()
        controller = Controller(auth, self.manager, None)
        ble = BleService(None, controller)
        self.assertEqual(ble.command.flags, ["encrypt-authenticated-write"])
        self.assertEqual(ble.event.flags, ["encrypt-authenticated-read"])
        self.assertNotIn("notify", ble.event.flags)
        await ble.send("phone", encode({"saved_profiles": ["private"]}), sensitive=True, revision="fixture-revision")
        self.assertEqual(ble.read_event("another-phone"), b"")
        auth.store.state.return_value = {"enabled": 1, "revision": "changed"}
        receiver, raw = Reassembler(), None
        while ble.queues["phone"]:
            raw = receiver.feed(ble.read_event("phone"))
        self.assertEqual(json.loads(raw)["type"], "authorization_expired")
        self.assertNotIn("private", raw.decode())

    async def test_pairing_agent_rejects_just_works_and_closed_physical_window(self):
        from rpi.pairing import PairingWindow
        from rpi.ble_service import PairingAgent
        from dbus_next import DBusError
        window = PairingWindow(Path(self.temp.name))
        self.assertFalse(window.permitted("phone"))
        with self.assertRaises(ValueError): window.display("phone", 123456)
        agent = PairingAgent(window)
        # Call the service implementation beneath dbus-next's dispatch wrapper.
        for item in agent._get_methods(agent):
            if item.name in {"RequestAuthorization", "RequestConfirmation"}:
                args = ["/phone"] + ([123456] if item.name == "RequestConfirmation" else [])
                with self.assertRaises(DBusError): item.fn(agent, *args)


if __name__ == "__main__":
    unittest.main()
