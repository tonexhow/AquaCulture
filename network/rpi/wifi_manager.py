"""NetworkManager native D-Bus adapter. No shell, passwords never in argv."""
import asyncio
import hashlib
import os
import platform
import socket
import time
import uuid
from pathlib import Path

NM = "org.freedesktop.NetworkManager"
ROOT = "/org/freedesktop/NetworkManager"
PROPS = "org.freedesktop.DBus.Properties"


class ProvisionError(Exception):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def unwrap(value):
    if hasattr(value, "value"):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [unwrap(v) for v in value]
    return value


def security_modes(ap):
    flags = int(ap.get("WpaFlags", 0)) | int(ap.get("RsnFlags", 0))
    modes = []
    if flags & 0x200:
        modes.append("wpa-eap")
    if flags & 0x400:
        modes.append("sae")
    if flags & 0x100:
        modes.append("wpa-psk")
    if flags & (0x800 | 0x1000):
        modes.append("owe")
    return modes or (["wep"] if ap.get("Flags", 0) & 1 else ["open"])


def normalize_networks(aps, saved=(), active="/"):
    groups = {}
    saved_ssids = {p["ssid"] for p in saved or ()}
    for path, ap in aps:
        ssid = bytes(ap.get("Ssid", [])).decode("utf-8", errors="replace")
        if not ssid:
            continue
        for mode in security_modes(ap):
            key = (ssid, mode)
            row = {"ssid": ssid, "signal": int(ap.get("Strength", 0)), "security": mode,
                   "connected": path == active, "saved": ssid in saved_ssids if saved is not None else None,
                   "authentication_required": mode != "open", "access_points": 1}
            if key in groups:
                old = groups[key]
                row["signal"] = max(row["signal"], old["signal"])
                row["connected"] |= old["connected"]
                row["access_points"] += old["access_points"]
            groups[key] = row
    return sorted(groups.values(), key=lambda n: (not n["connected"], -n["signal"], n["ssid"]))[:64]


def validate_configuration(payload):
    if not isinstance(payload, dict) or set(payload) - {"ssid", "security", "password", "hidden"}:
        raise ProvisionError("INVALID_REQUEST", "Unsupported configuration fields.")
    ssid, mode, password = payload.get("ssid"), payload.get("security"), payload.get("password", "")
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ProvisionError("INVALID_REQUEST", "SSID must contain 1–32 UTF-8 bytes without control characters.")
    if mode not in {"open", "wpa-psk", "sae"}:
        raise ProvisionError("UNSUPPORTED_SECURITY", "Only open, WPA/WPA2 personal and WPA3 personal are supported; enterprise/OWE require another setup flow.")
    if not isinstance(password, str) or "\x00" in password or not isinstance(payload.get("hidden", False), bool):
        raise ProvisionError("INVALID_REQUEST", "Invalid password or hidden-network flag.")
    length = len(password.encode("utf-8"))
    raw_psk = len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password)
    if mode == "open" and password or mode == "wpa-psk" and not (8 <= length <= 63 or raw_psk) or mode == "sae" and not 1 <= length <= 63:
        raise ProvisionError("INVALID_REQUEST", "Check the password length for the selected security mode.")
    return ssid, mode, password, payload.get("hidden", False)


class NetworkManager:
    def __init__(self, bus, history, interface=None):
        self.bus, self.history = bus, history
        self.interface = interface or os.environ.get("AQUACULTURE_WIFI_INTERFACE")
        self.operation = asyncio.Lock()
        self.last_scan = -100.0
        self.last_configure = -100.0
        self.changed = asyncio.Event()

    async def iface(self, path, interface):
        obj = self.bus.get_proxy_object(NM, path, await self.bus.introspect(NM, path))
        return obj.get_interface(interface)

    async def props(self, path, interface):
        return unwrap(await (await self.iface(path, PROPS)).call_get_all(interface))

    async def device(self):
        manager = await self.iface(ROOT, NM)
        for path in await manager.call_get_devices():
            props = await self.props(path, NM + ".Device")
            if props.get("DeviceType") == 2 and (not self.interface or props.get("Interface") == self.interface):
                if not props.get("Managed"):
                    raise ProvisionError("ADAPTER_UNAVAILABLE", "Wi-Fi adapter is not managed by NetworkManager.")
                return path, props
        raise ProvisionError("ADAPTER_UNAVAILABLE", "No managed Wi-Fi adapter is available.")

    async def saved_profiles(self):
        try:
            settings = await self.iface(ROOT + "/Settings", NM + ".Settings")
            paths = await settings.call_list_connections()
        except Exception:
            return None
        profiles = []
        for path in paths:
            # GetSettings excludes secrets; never call GetSecrets.
            try:
                cfg = unwrap(await (await self.iface(path, NM + ".Settings.Connection")).call_get_settings())
            except Exception:
                return None
            if "802-11-wireless" in cfg:
                profiles.append({"ssid": bytes(cfg["802-11-wireless"].get("ssid", [])).decode("utf-8", "replace"),
                                 "name": cfg.get("connection", {}).get("id"), "path": path})
        return profiles

    async def scan(self):
        if self.operation.locked():
            raise ProvisionError("BUSY", "A network operation is already running.")
        if time.monotonic() - self.last_scan < 15:
            raise ProvisionError("RATE_LIMITED", "Wait 15 seconds between scans.")
        async with self.operation:
            self.last_scan = time.monotonic()
            path, _ = await self.device()
            wireless = await self.iface(path, NM + ".Device.Wireless")
            before = (await self.props(path, NM + ".Device.Wireless")).get("LastScan", -1)
            await wireless.call_request_scan({})
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                props = await self.props(path, NM + ".Device.Wireless")
                if props.get("LastScan", -1) != before:
                    break
                await asyncio.sleep(1)
            else:
                raise ProvisionError("TIMED_OUT", "Wi-Fi scan timed out; try again.")
            aps = [(p, await self.props(p, NM + ".AccessPoint")) for p in await wireless.call_get_all_access_points()]
            return normalize_networks(aps, await self.saved_profiles(), props.get("ActiveAccessPoint", "/"))

    async def status(self, test_internet=False):
        manager = await self.iface(ROOT, NM)
        if test_internet:
            try:
                await asyncio.wait_for(manager.call_check_connectivity(), 12)
            except Exception:
                pass  # Preserve local status even if the connectivity probe is unavailable.
        try:
            path, device = await self.device()
        except ProvisionError:
            return {"state": "DISCONNECTED", "connected": False, "adapter": None, "ssid": None, "ip_address": None,
                    "internet_available": None, "local_connectivity": False, **self.history.metadata()}
        wifi = await self.props(path, NM + ".Device.Wireless")
        ap_path = wifi.get("ActiveAccessPoint", "/")
        ap = await self.props(ap_path, NM + ".AccessPoint") if ap_path != "/" else {}
        ip = None
        for key, kind in (("Ip4Config", "IP4Config"), ("Ip6Config", "IP6Config")):
            ip_path = device.get(key, "/")
            if ip_path != "/":
                data = await self.props(ip_path, NM + "." + kind)
                ip = next((a.get("address") for a in data.get("AddressData", []) if a.get("address") and not a["address"].lower().startswith("fe80:")), None)
                if ip:
                    break
        connected = device.get("State") == 100 and ap_path != "/"
        # Per-adapter check results prevent Ethernet connectivity masquerading as Wi-Fi internet.
        connectivity = max(device.get("Ip4Connectivity", 0), device.get("Ip6Connectivity", 0))
        internet = (connectivity == 4 if connectivity in {1, 2, 3, 4} else None) if connected else None
        profiles = await self.saved_profiles()
        return {"state": "CONNECTED_ONLINE" if internet is True else "CONNECTED_LOCAL" if connected else "DISCONNECTED",
                "connected": connected, "ssid": bytes(ap.get("Ssid", [])).decode("utf-8", "replace") or None,
                "ip_address": ip, "adapter": device.get("Interface"), "signal": ap.get("Strength"),
                "security": security_modes(ap) if ap else None, "local_connectivity": bool(connected and ip),
                "internet_available": internet, "connectivity_check": "NetworkManager per Wi-Fi adapter", "saved_profiles": [{k: v for k, v in p.items() if k != "path"} for p in profiles[:64]] if profiles is not None else None,
                **self.history.metadata()}

    async def configure(self, payload, progress):
        ssid, mode, password, hidden = validate_configuration(payload)
        if self.operation.locked():
            raise ProvisionError("BUSY", "A network operation is already running.")
        if time.monotonic() - self.last_configure < 10:
            raise ProvisionError("RATE_LIMITED", "Wait ten seconds before another connection attempt.")
        async with self.operation:
            self.last_configure = time.monotonic()
            path, device = await self.device()
            manager = await self.iface(ROOT, NM)
            previous = device.get("ActiveConnection", "/")
            previous_profile = None
            if previous != "/" and device.get("State") == 100:
                previous_profile = (await self.props(previous, NM + ".Connection.Active")).get("Connection")
            from dbus_next import Variant
            profile = {"connection": {"id": Variant("s", "AquaCulture-" + uuid.uuid4().hex[:10]),
                         "uuid": Variant("s", str(uuid.uuid4())), "type": Variant("s", "802-11-wireless"),
                         "autoconnect": Variant("b", False)},
                       "802-11-wireless": {"ssid": Variant("ay", ssid.encode()), "mode": Variant("s", "infrastructure"), "hidden": Variant("b", hidden)},
                       "ipv4": {"method": Variant("s", "auto")}, "ipv6": {"method": Variant("s", "auto")}}
            if mode != "open":
                profile["802-11-wireless-security"] = {"key-mgmt": Variant("s", mode), "psk": Variant("s", password)}
                if mode == "sae":
                    profile["802-11-wireless-security"]["pmf"] = Variant("i", 3)
            record = self.history.add(ssid, "CONNECTING", manual=True)
            try:
                await progress("CONNECTING", {})
                # New profile, in memory until success. Existing known-good profiles are never modified/deleted.
                profile_path, active_path, _ = await manager.call_add_and_activate_connection2(profile, path, "/", {"persist": Variant("s", "memory")})
                await self.wait_connected(path, active_path, progress, timeout=75)
                # Only successful replacements become persistent/autoconnectable.
                profile["connection"]["autoconnect"] = Variant("b", True)
                connection = await self.iface(profile_path, NM + ".Settings.Connection")
                await connection.call_update(profile)
                await connection.call_save()
                result = await self.status(test_internet=True)
                self.history.finish(record, result["state"])
                result.update(self.history.metadata())
                return result
            except Exception as error:
                category = error.category if isinstance(error, ProvisionError) else "UNKNOWN_ERROR"
                self.history.finish(record, "TIMED_OUT" if category == "TIMED_OUT" else "FAILED", category)
                restored = False
                if previous_profile:
                    try:
                        restored_path = await manager.call_activate_connection(previous_profile, path, "/")
                        await self.wait_connected(path, restored_path, self.no_progress, timeout=25)
                        restored = True
                    except Exception:
                        pass
                if isinstance(error, ProvisionError):
                    raise ProvisionError(category, str(error) + (" Previous connection restored." if restored else " Previous profiles retained; reconnect over Bluetooth to retry.")) from None
                raise ProvisionError("UNKNOWN_ERROR", "NetworkManager operation failed; profiles retained. Check service permissions and retry.") from None
            finally:
                password = None
                profile.clear()
                self.changed.set()

    @staticmethod
    async def no_progress(*args):
        pass

    async def wait_connected(self, path, active_path, progress, timeout):
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            props = await self.props(path, NM + ".Device")
            state, reason = props.get("State", 0), props.get("StateReason", [0, 0])[1]
            if props.get("ActiveConnection") == active_path and state == 100:
                return
            if state == 120 or state == 30 and reason not in {0, 1, 39, 40}:
                category, message = {
                    7: ("AUTHENTICATION_REJECTED", "Authentication rejected; check credentials."),
                    10: ("AUTHENTICATION_REJECTED", "Wi-Fi authentication failed."),
                    11: ("TIMED_OUT", "Wi-Fi association timed out."),
                    53: ("SSID_UNAVAILABLE", "The selected network is unavailable."),
                    5: ("IP_ASSIGNMENT_FAILED", "IP assignment failed."),
                    17: ("IP_ASSIGNMENT_FAILED", "DHCP could not assign an IP address."),
                }.get(reason, ("UNKNOWN_ERROR", "NetworkManager could not activate this network."))
                raise ProvisionError(category, message)
            stage = "OBTAINING_IP" if state in {70, 80, 90} else "AUTHENTICATING" if state == 60 else "CONNECTING"
            if stage != last:
                await progress(stage, {})
                last = stage
            await asyncio.sleep(1)
        raise ProvisionError("TIMED_OUT", "Network connection timed out.")


def device_information():
    def read(path):
        try:
            return Path(path).read_text().strip("\x00\n ") or None
        except (OSError, UnicodeError):
            return None
    machine = read("/etc/machine-id")
    try:
        operating_system = platform.freedesktop_os_release().get("PRETTY_NAME")
    except OSError:
        operating_system = None
    uptime = read("/proc/uptime")
    return {"device_name": "AquaCulture-Pi", "hostname": socket.gethostname(),
            "device_id": hashlib.sha256(machine.encode()).hexdigest()[:12] if machine else None,
            "model": read("/proc/device-tree/model"), "operating_system": operating_system,
            "protocol_version": 2, "service_version": "1.1.0", "software_version": "1.1.0",
            "uptime_seconds": int(float(uptime.split()[0])) if uptime else None,
            "capabilities": ["scan", "hidden_ssid", "wpa-psk", "sae", "history", "authenticated_ble", "admin_verification", "private_responses"]}
