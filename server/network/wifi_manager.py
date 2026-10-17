"""
AquaCulture - network_manager.py

Real network helper for the hidden port-5001 panel.

Rules:
- Do not fake Wi-Fi scan results.
- Do not pretend OS-level changes succeeded.
- Raspberry Pi/Linux uses NetworkManager/nmcli.
- Windows development fallback uses real netsh commands when available.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

BASE_DIR = Path(__file__).resolve().parents[2]
NETWORK_DIR = BASE_DIR / "server" / "network"
HOTSPOT_CONFIG = NETWORK_DIR / "hotspot.json"
MODE_CONFIG = NETWORK_DIR / "network_mode.json"
WINDOWS_PROFILE_DIR = NETWORK_DIR / "windows_wifi_profiles"

VALID_WIRED_MODES = {"ethernet", "usb_tether"}
VALID_MODES = {"wlan", *VALID_WIRED_MODES}
DEFAULT_HOTSPOT_NAME = "AquaCulture-Setup"
DEFAULT_HOTSPOT_CONNECTION = "AquaCulture-Hotspot"
DEFAULT_HOTSPOT_PASSWORD = os.environ.get("AQUACULTURE_HOTSPOT_PASSWORD", "AquaCulture123")


# -----------------------------------------------------------------------------
# Public status helpers
# -----------------------------------------------------------------------------


def get_status() -> dict[str, Any]:
    """Return real network status from the local operating system."""
    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    nmcli_available = bool(shutil.which("nmcli"))
    netsh_available = bool(shutil.which("netsh")) and is_windows()
    mode_data = read_json(MODE_CONFIG, {"mode": "wlan"})
    hotspot_data = read_json(HOTSPOT_CONFIG, {"name": DEFAULT_HOTSPOT_NAME})

    status: dict[str, Any] = {
        "platform": platform.system(),
        "is_raspberry_pi": is_raspberry_pi(),
        "nmcli_available": nmcli_available,
        "windows_netsh_available": netsh_available,
        "network_tools_available": nmcli_available or netsh_available,
        "network_tool": "NetworkManager/nmcli" if nmcli_available else ("Windows netsh" if netsh_available else "none"),
        "mode": mode_data.get("mode", "wlan"),
        "hotspot": normalize_hotspot_config(hotspot_data),
        "wireless": {"interfaces": [], "active": None},
        "wired": {"ethernet": [], "usb_tether": []},
        "devices": [],
        "active_connections": [],
        "message": "Ready",
    }

    if nmcli_available:
        devices_result = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
        devices = parse_colon_rows(devices_result.get("stdout", ""), ["device", "type", "state", "connection"])
        active_result = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"])
        status["devices"] = devices
        status["active_connections"] = parse_colon_rows(active_result.get("stdout", ""), ["name", "type", "device"])
        status["wireless"] = get_wireless_status(devices)
        status["wired"] = get_wired_status(devices)
        return status

    if netsh_available:
        status["message"] = "Windows fallback active. Results come from real netsh commands; Raspberry Pi hotspot/Go Live actions still require Linux nmcli."
        status["wireless"] = windows_wireless_status()
        status["wired"] = windows_wired_status()
        return status

    status["message"] = "No supported network command was found. Use Raspberry Pi OS with nmcli, or Windows with netsh for development testing."
    return status


def get_saved_networks() -> dict[str, Any]:
    """Return saved Wi-Fi connections only. No mock data is returned."""
    if shutil.which("nmcli"):
        return linux_saved_networks()
    if is_windows() and shutil.which("netsh"):
        return windows_saved_networks()
    return fail("Saved networks cannot be read because neither nmcli nor Windows netsh is available on this computer.")


def scan_wifi(interface: str = "wlan0") -> dict[str, Any]:
    """Scan real nearby Wi-Fi networks. Never returns fabricated networks."""
    if shutil.which("nmcli"):
        return linux_scan_wifi(interface)
    if is_windows() and shutil.which("netsh"):
        return windows_scan_wifi()
    return fail("Wi-Fi scan is not available because no supported OS network scanner was found.")


# -----------------------------------------------------------------------------
# Wireless actions
# -----------------------------------------------------------------------------


def connect_wifi(ssid: str, password: str = "", interface: str = "wlan0", hidden: bool = False) -> dict[str, Any]:
    ssid = (ssid or "").strip()
    if not ssid:
        return fail("SSID is required.")

    if shutil.which("nmcli"):
        return linux_connect_wifi(ssid, password=password, interface=interface, hidden=hidden)
    if is_windows() and shutil.which("netsh"):
        return windows_connect_wifi(ssid, password=password, hidden=hidden)
    return fail("Cannot connect to Wi-Fi because no supported OS network command was found.")


def connect_saved(name: str, interface: str = "wlan0") -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return fail("Saved network name is required.")

    if shutil.which("nmcli"):
        return linux_connect_saved(name, interface=interface)
    if is_windows() and shutil.which("netsh"):
        return windows_connect_saved(name)
    return fail("Cannot connect saved network because no supported OS network command was found.")


def forget_network(name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return fail("Network name is required.")

    if shutil.which("nmcli"):
        result = run(["nmcli", "connection", "delete", "id", name], timeout=25)
        if result.get("returncode") == 0:
            return success("Saved Wi-Fi network removed.", {"name": name})
        return fail("Could not forget saved network.", details={"name": name, "output": result.get("stderr") or result.get("stdout")})

    if is_windows() and shutil.which("netsh"):
        result = run(["netsh", "wlan", "delete", "profile", f"name={name}"], timeout=25)
        if result.get("returncode") == 0:
            return success("Windows Wi-Fi profile removed.", {"name": name, "output": result.get("stdout", "")})
        return fail("Could not forget Windows Wi-Fi profile.", details={"name": name, "output": result.get("stderr") or result.get("stdout")})

    return fail("Cannot forget network because no supported OS network command was found.")


def modify_network(name: str, new_name: str = "", password: str = "", autoconnect: Any = None) -> dict[str, Any]:
    name = (name or "").strip()
    new_name = (new_name or "").strip()
    password = password or ""
    if not name:
        return fail("Network name is required.")

    if shutil.which("nmcli"):
        return linux_modify_network(name, new_name=new_name, password=password, autoconnect=autoconnect)
    if is_windows() and shutil.which("netsh"):
        return windows_modify_network(name, new_name=new_name, password=password, autoconnect=autoconnect)
    return fail("Cannot modify network because no supported OS network command was found.")


# -----------------------------------------------------------------------------
# Wired / live mode actions
# -----------------------------------------------------------------------------


def select_mode(mode: str) -> dict[str, Any]:
    mode = (mode or "wlan").strip().lower()
    if mode not in VALID_MODES:
        return fail("Invalid network mode.", details={"allowed": sorted(VALID_MODES)})

    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    write_json(MODE_CONFIG, {"mode": mode, "updated_at": time.time()})

    if mode == "wlan":
        return success("Wireless mode selected.", {"mode": mode})

    devices = get_status().get("wired", {})
    key = "ethernet" if mode == "ethernet" else "usb_tether"
    return success("Wired mode selected. Confirm Go Live when the cable or tether is ready.", {
        "mode": mode,
        "available_devices": devices.get(key, []),
    })


def go_live(mode: str | None = None) -> dict[str, Any]:
    """Connect selected online path and stop the local hotspot if it is active."""
    selected = (mode or read_json(MODE_CONFIG, {"mode": "wlan"}).get("mode") or "wlan").strip().lower()
    if selected not in VALID_MODES:
        return fail("Invalid Go Live mode.", details={"mode": selected})
    if not shutil.which("nmcli"):
        return fail("Go Live is only supported on Raspberry Pi/Linux with NetworkManager/nmcli. Windows fallback is for UI and WLAN testing only.")

    if selected == "wlan":
        return success("Wireless mode is already selected. Connect to a Wi-Fi network to go online.", {"mode": "wlan"})

    status = get_status()
    wired = status.get("wired", {})
    candidates = wired.get("ethernet" if selected == "ethernet" else "usb_tether", [])
    if not candidates:
        return fail(
            "No matching wired device was detected.",
            details={"mode": selected, "hint": "Connect the Ethernet cable or enable USB tethering, then refresh."},
        )

    device = choose_best_wired_device(candidates)
    if not device:
        return fail("No usable wired device was detected.", details={"mode": selected})

    connect_result = run(["nmcli", "device", "connect", device["device"]], timeout=40)
    if connect_result.get("returncode") != 0:
        return fail("Could not activate the selected wired device.", details={
            "mode": selected,
            "device": device,
            "output": connect_result.get("stderr") or connect_result.get("stdout"),
        })

    hotspot_stop = stop_hotspot()
    write_json(MODE_CONFIG, {"mode": selected, "updated_at": time.time(), "live_at": time.time()})
    return success("Go Live completed. The local hotspot was stopped if it was active.", {
        "mode": selected,
        "device": device,
        "hotspot_stop": hotspot_stop,
    })


# -----------------------------------------------------------------------------
# Hotspot actions
# -----------------------------------------------------------------------------


def set_hotspot_name(name: str) -> dict[str, Any]:
    name = normalize_ssid(name)
    if not name:
        return fail("Hotspot name is required.")
    if len(name) > 32:
        return fail("Hotspot name must be 32 characters or fewer.")

    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    config = normalize_hotspot_config(read_json(HOTSPOT_CONFIG, {}))
    config["name"] = name
    config["updated_at"] = time.time()
    config["applied"] = False
    write_json(HOTSPOT_CONFIG, config)

    if not shutil.which("nmcli"):
        return fail(
            "Hotspot name was saved locally, but cannot be applied because nmcli is not available on this computer.",
            details={"hotspot": config, "hint": "Apply on Raspberry Pi OS with NetworkManager/nmcli."},
        )

    connection_name = find_hotspot_connection_name() or config.get("connection_name") or DEFAULT_HOTSPOT_CONNECTION
    apply_result = ensure_hotspot_connection(connection_name, name)
    if not apply_result.get("ok"):
        return apply_result

    config["connection_name"] = connection_name
    config["applied"] = True
    config["applied_at"] = time.time()
    write_json(HOTSPOT_CONFIG, config)

    return success("Hotspot name applied. Devices connected through the old hotspot name may disconnect.", {
        "hotspot": config,
        "connection": connection_name,
    })


def stop_hotspot() -> dict[str, Any]:
    if not shutil.which("nmcli"):
        return fail("Cannot stop hotspot because nmcli is not available.")

    connection_name = find_active_hotspot_connection_name() or find_hotspot_connection_name()
    if not connection_name:
        return success("No active AquaCulture hotspot connection was found.", {"stopped": False})

    result = run(["nmcli", "connection", "down", "id", connection_name], timeout=25)
    if result.get("returncode") == 0:
        return success("Hotspot stopped.", {"stopped": True, "connection": connection_name})

    return fail("Could not stop hotspot.", details={"connection": connection_name, "output": result.get("stderr") or result.get("stdout")})


# -----------------------------------------------------------------------------
# Linux / nmcli helpers
# -----------------------------------------------------------------------------


def linux_saved_networks() -> dict[str, Any]:
    result = run(["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT,DEVICE", "connection", "show"])
    if result.get("returncode") != 0:
        return fail("Could not read saved Wi-Fi networks.", details={"output": result.get("stderr") or result.get("stdout")})

    rows = parse_colon_rows(result.get("stdout", ""), ["name", "type", "autoconnect", "device"])
    saved: list[dict[str, Any]] = []
    for row in rows:
        ctype = (row.get("type") or "").lower()
        if "wireless" not in ctype and ctype not in {"wifi", "802-11-wireless"}:
            continue
        saved.append({
            "name": row.get("name") or "",
            "ssid": row.get("name") or "",
            "type": row.get("type") or "wifi",
            "autoconnect": (row.get("autoconnect") or "").lower() in {"yes", "true"},
            "device": row.get("device") or "",
            "connected": bool(row.get("device")),
        })
    return success("Saved Wi-Fi networks loaded.", {"saved": saved})


def linux_scan_wifi(interface: str = "wlan0") -> dict[str, Any]:
    interface = (interface or "wlan0").strip() or "wlan0"
    run(["nmcli", "device", "wifi", "rescan", "ifname", interface], timeout=20)
    result = run([
        "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,FREQ,CHAN,IN-USE",
        "device", "wifi", "list", "ifname", interface,
    ], timeout=25)
    if result.get("returncode") != 0:
        return fail("Wi-Fi scan failed.", details={"interface": interface, "output": result.get("stderr") or result.get("stdout")})

    rows = parse_colon_rows(result.get("stdout", ""), ["ssid", "signal", "security", "frequency", "channel", "in_use"])
    saved_names = {item.get("ssid") or item.get("name") for item in (linux_saved_networks().get("data") or {}).get("saved", [])}
    networks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        ssid = (row.get("ssid") or "").strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        networks.append({
            "ssid": ssid,
            "signal": safe_int(row.get("signal"), 0),
            "security": row.get("security") or "Open",
            "frequency": row.get("frequency") or "",
            "channel": row.get("channel") or "",
            "in_use": (row.get("in_use") or "").strip() == "*",
            "saved": ssid in saved_names,
        })
    networks.sort(key=lambda item: item.get("signal", 0), reverse=True)
    return success("Wi-Fi scan complete.", {"interface": interface, "networks": networks})


def linux_connect_wifi(ssid: str, password: str = "", interface: str = "wlan0", hidden: bool = False) -> dict[str, Any]:
    interface = (interface or "wlan0").strip() or "wlan0"
    command = ["nmcli", "device", "wifi", "connect", ssid, "ifname", interface]
    if password:
        command.extend(["password", password])
    if hidden:
        command.extend(["hidden", "yes"])
    result = run(command, timeout=45)
    if result.get("returncode") == 0:
        write_json(MODE_CONFIG, {"mode": "wlan", "updated_at": time.time()})
        return success("Connected to Wi-Fi.", {"ssid": ssid, "output": result.get("stdout", "")})
    return fail("Wi-Fi connection failed.", details={"ssid": ssid, "output": result.get("stderr") or result.get("stdout")})


def linux_connect_saved(name: str, interface: str = "wlan0") -> dict[str, Any]:
    interface = (interface or "wlan0").strip() or "wlan0"
    result = run(["nmcli", "connection", "up", "id", name, "ifname", interface], timeout=45)
    if result.get("returncode") == 0:
        write_json(MODE_CONFIG, {"mode": "wlan", "updated_at": time.time()})
        return success("Saved Wi-Fi network connected.", {"name": name, "output": result.get("stdout", "")})
    return fail("Saved Wi-Fi connection failed.", details={"name": name, "output": result.get("stderr") or result.get("stdout")})


def linux_modify_network(name: str, new_name: str = "", password: str = "", autoconnect: Any = None) -> dict[str, Any]:
    changes: list[str] = []
    if password:
        result = run(["nmcli", "connection", "modify", name, "wifi-sec.psk", password], timeout=25)
        if result.get("returncode") != 0:
            return fail("Could not update saved password.", details={"output": result.get("stderr") or result.get("stdout")})
        changes.append("password")

    if autoconnect is not None:
        value = "yes" if bool(autoconnect) else "no"
        result = run(["nmcli", "connection", "modify", name, "connection.autoconnect", value], timeout=25)
        if result.get("returncode") != 0:
            return fail("Could not update autoconnect.", details={"output": result.get("stderr") or result.get("stdout")})
        changes.append("autoconnect")

    final_name = new_name or name
    if new_name and new_name != name:
        result = run(["nmcli", "connection", "modify", name, "connection.id", new_name], timeout=25)
        if result.get("returncode") != 0:
            return fail("Could not rename saved network.", details={"output": result.get("stderr") or result.get("stdout")})
        changes.append("name")
    return success("Saved network updated.", {"name": final_name, "changes": changes})


# -----------------------------------------------------------------------------
# Windows / netsh fallback helpers
# -----------------------------------------------------------------------------


def windows_saved_networks() -> dict[str, Any]:
    result = run(["netsh", "wlan", "show", "profiles"], timeout=20)
    if result.get("returncode") != 0:
        return fail("Could not read Windows Wi-Fi profiles.", details={"output": result.get("stderr") or result.get("stdout")})
    active = (windows_wireless_status().get("active") or {}).get("connection") or ""
    saved = []
    for line in result.get("stdout", "").splitlines():
        match = re.search(r"(?:All User Profile|User Profile)\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
        if not match:
            continue
        name = match.group(1).strip()
        saved.append({
            "name": name,
            "ssid": name,
            "type": "wifi",
            "autoconnect": True,
            "device": "Windows WLAN",
            "connected": name == active,
        })
    return success("Windows saved Wi-Fi profiles loaded.", {"saved": saved})


def windows_scan_wifi() -> dict[str, Any]:
    result = run(["netsh", "wlan", "show", "networks", "mode=bssid"], timeout=30)
    if result.get("returncode") != 0:
        return fail("Windows Wi-Fi scan failed.", details={"output": result.get("stderr") or result.get("stdout")})

    saved_names = {item.get("ssid") or item.get("name") for item in (windows_saved_networks().get("data") or {}).get("saved", [])}
    active = (windows_wireless_status().get("active") or {}).get("connection") or ""
    networks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in result.get("stdout", "").splitlines():
        line = raw.strip()
        ssid_match = re.match(r"SSID\s+\d+\s*:\s*(.*)$", line, re.IGNORECASE)
        if ssid_match:
            if current and current.get("ssid"):
                networks.append(current)
            current = {"ssid": ssid_match.group(1).strip(), "signal": 0, "security": "", "in_use": False}
            continue
        if current is None:
            continue
        if line.lower().startswith("authentication"):
            current["security"] = line.split(":", 1)[1].strip() if ":" in line else ""
        elif line.lower().startswith("signal"):
            signal_text = line.split(":", 1)[1].strip() if ":" in line else "0"
            current["signal"] = max(safe_int(signal_text.replace("%", ""), 0), int(current.get("signal") or 0))
    if current and current.get("ssid"):
        networks.append(current)

    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for item in networks:
        ssid = (item.get("ssid") or "").strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        clean.append({
            "ssid": ssid,
            "signal": safe_int(item.get("signal"), 0),
            "security": item.get("security") or "Open",
            "frequency": "",
            "channel": "",
            "in_use": ssid == active,
            "saved": ssid in saved_names,
        })
    clean.sort(key=lambda item: item.get("signal", 0), reverse=True)
    return success("Windows Wi-Fi scan complete.", {"interface": "Windows WLAN", "networks": clean})


def windows_connect_wifi(ssid: str, password: str = "", hidden: bool = False) -> dict[str, Any]:
    if password:
        profile_path = create_windows_wifi_profile(ssid, password=password, hidden=hidden)
        add = run(["netsh", "wlan", "add", "profile", f"filename={str(profile_path)}", "user=current"], timeout=25)
        if add.get("returncode") != 0:
            return fail("Could not create Windows Wi-Fi profile.", details={"output": add.get("stderr") or add.get("stdout")})

    result = run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"], timeout=45)
    if result.get("returncode") == 0:
        write_json(MODE_CONFIG, {"mode": "wlan", "updated_at": time.time()})
        return success("Windows Wi-Fi connection started.", {"ssid": ssid, "output": result.get("stdout", "")})
    return fail("Windows Wi-Fi connection failed.", details={"ssid": ssid, "output": result.get("stderr") or result.get("stdout")})


def windows_connect_saved(name: str) -> dict[str, Any]:
    result = run(["netsh", "wlan", "connect", f"name={name}"], timeout=45)
    if result.get("returncode") == 0:
        write_json(MODE_CONFIG, {"mode": "wlan", "updated_at": time.time()})
        return success("Windows saved Wi-Fi profile connection started.", {"name": name, "output": result.get("stdout", "")})
    return fail("Windows saved Wi-Fi connection failed.", details={"name": name, "output": result.get("stderr") or result.get("stdout")})


def windows_modify_network(name: str, new_name: str = "", password: str = "", autoconnect: Any = None) -> dict[str, Any]:
    if new_name and new_name != name:
        return fail("Windows netsh cannot safely rename a Wi-Fi profile from this panel. Create/connect the new SSID profile instead.")
    changes: list[str] = []
    if password:
        profile_path = create_windows_wifi_profile(name, password=password, hidden=False)
        add = run(["netsh", "wlan", "add", "profile", f"filename={str(profile_path)}", "user=current"], timeout=25)
        if add.get("returncode") != 0:
            return fail("Could not update Windows Wi-Fi password/profile.", details={"output": add.get("stderr") or add.get("stdout")})
        changes.append("password")
    if autoconnect is not None:
        mode = "auto" if bool(autoconnect) else "manual"
        result = run(["netsh", "wlan", "set", "profileparameter", f"name={name}", f"connectionmode={mode}"], timeout=20)
        if result.get("returncode") != 0:
            return fail("Could not update Windows Wi-Fi autoconnect.", details={"output": result.get("stderr") or result.get("stdout")})
        changes.append("autoconnect")
    return success("Windows Wi-Fi profile updated.", {"name": name, "changes": changes})


def windows_wireless_status() -> dict[str, Any]:
    result = run(["netsh", "wlan", "show", "interfaces"], timeout=20)
    if result.get("returncode") != 0:
        return {"interfaces": [], "active": None}
    interfaces: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in result.get("stdout", "").splitlines():
        if ":" not in raw:
            continue
        key, value = [part.strip() for part in raw.split(":", 1)]
        key_l = key.lower()
        if key_l == "name":
            if current.get("device"):
                interfaces.append(current)
            current = {"device": value, "state": "", "connection": ""}
        elif key_l == "state":
            current["state"] = value
        elif key_l == "ssid" and not key_l.startswith("bssid"):
            if value and value.lower() != "n/a":
                current["connection"] = value
    if current.get("device"):
        interfaces.append(current)
    active = next((item for item in interfaces if item.get("connection")), None)
    return {"interfaces": interfaces, "active": active}


def windows_wired_status() -> dict[str, list[dict[str, Any]]]:
    result = run(["netsh", "interface", "show", "interface"], timeout=20)
    ethernet: list[dict[str, Any]] = []
    usb_tether: list[dict[str, Any]] = []
    if result.get("returncode") != 0:
        return {"ethernet": ethernet, "usb_tether": usb_tether}
    for raw in result.get("stdout", "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("admin") or line.startswith("---"):
            continue
        parts = re.split(r"\s{2,}", line, maxsplit=3)
        if len(parts) < 4:
            continue
        admin_state, state, interface_type, name = parts
        low = name.lower()
        if any(word in low for word in ("wi-fi", "wifi", "wlan", "wireless", "loopback", "bluetooth")):
            continue
        row = {
            "device": name,
            "state": state,
            "connection": interface_type,
            "connected": state.lower() == "connected",
        }
        if any(word in low for word in ("usb", "tether", "rndis", "remote ndis")):
            usb_tether.append(row)
        elif "ethernet" in low or interface_type.lower() == "dedicated":
            ethernet.append(row)
    return {"ethernet": ethernet, "usb_tether": usb_tether}


def create_windows_wifi_profile(ssid: str, password: str, hidden: bool = False) -> Path:
    WINDOWS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", ssid).strip("_") or "wifi"
    path = WINDOWS_PROFILE_DIR / f"{safe_name}.xml"
    ssid_xml = xml_escape(ssid)
    password_xml = xml_escape(password)
    non_broadcast = "true" if hidden else "false"
    xml = f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid_xml}</name>
  <SSIDConfig>
    <SSID><name>{ssid_xml}</name></SSID>
    <nonBroadcast>{non_broadcast}</nonBroadcast>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{password_xml}</keyMaterial>
      </sharedKey>
    </security>
  </MSM>
</WLANProfile>
'''
    path.write_text(xml, encoding="utf-8")
    return path


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


def get_wireless_status(devices: list[dict[str, Any]]) -> dict[str, Any]:
    interfaces = []
    active = None
    for item in devices:
        if (item.get("type") or "").lower() != "wifi":
            continue
        row = {
            "device": item.get("device") or "",
            "state": item.get("state") or "",
            "connection": item.get("connection") or "",
        }
        interfaces.append(row)
        if row["connection"]:
            active = row
    return {"interfaces": interfaces, "active": active}


def get_wired_status(devices: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    ethernet: list[dict[str, Any]] = []
    usb_tether: list[dict[str, Any]] = []
    for item in devices:
        if (item.get("type") or "").lower() != "ethernet":
            continue
        device_name = item.get("device") or ""
        row = {
            "device": device_name,
            "state": item.get("state") or "",
            "connection": item.get("connection") or "",
            "connected": (item.get("state") or "").lower() in {"connected", "connecting"},
        }
        if is_usb_tether_device(device_name):
            usb_tether.append(row)
        else:
            ethernet.append(row)
    return {"ethernet": ethernet, "usb_tether": usb_tether}


def is_usb_tether_device(device_name: str) -> bool:
    name = (device_name or "").lower()
    return name.startswith("usb") or name.startswith("enx") or (name.startswith("enp0s") and "u" in name)


def choose_best_wired_device(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    for item in candidates:
        if (item.get("state") or "").lower() == "connected":
            return item
    return candidates[0]


def ensure_hotspot_connection(connection_name: str, ssid: str) -> dict[str, Any]:
    existing = connection_exists(connection_name)
    if not existing:
        password = DEFAULT_HOTSPOT_PASSWORD
        if len(password) < 8:
            return fail("Hotspot password must be at least 8 characters.", details={"connection": connection_name})
        add = run([
            "nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0",
            "con-name", connection_name, "ssid", ssid,
        ], timeout=25)
        if add.get("returncode") != 0:
            return fail("Could not create hotspot connection.", details={"output": add.get("stderr") or add.get("stdout")})
        modify = run([
            "nmcli", "connection", "modify", connection_name,
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg",
            "ipv4.method", "shared",
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", password,
            "connection.autoconnect", "no",
        ], timeout=25)
        if modify.get("returncode") != 0:
            return fail("Hotspot connection was created but configuration failed.", details={"output": modify.get("stderr") or modify.get("stdout")})
        return success("Hotspot connection created.", {"connection": connection_name, "ssid": ssid})

    modify = run(["nmcli", "connection", "modify", connection_name, "802-11-wireless.ssid", ssid], timeout=25)
    if modify.get("returncode") != 0:
        return fail("Could not apply hotspot name to the NetworkManager connection.", details={"output": modify.get("stderr") or modify.get("stdout")})
    return success("Hotspot connection updated.", {"connection": connection_name, "ssid": ssid})


def connection_exists(connection_name: str) -> bool:
    result = run(["nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=15)
    if result.get("returncode") != 0:
        return False
    return connection_name in set(line.strip() for line in result.get("stdout", "").splitlines())


def find_hotspot_connection_name() -> str | None:
    configured = normalize_hotspot_config(read_json(HOTSPOT_CONFIG, {})).get("connection_name")
    if configured and connection_exists(configured):
        return configured
    result = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=15)
    if result.get("returncode") != 0:
        return None
    for row in parse_colon_rows(result.get("stdout", ""), ["name", "type"]):
        name = row.get("name") or ""
        if "hotspot" in name.lower() or "aquaculture" in name.lower():
            return name
    if connection_exists(DEFAULT_HOTSPOT_CONNECTION):
        return DEFAULT_HOTSPOT_CONNECTION
    return None


def find_active_hotspot_connection_name() -> str | None:
    result = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], timeout=15)
    if result.get("returncode") != 0:
        return None
    configured = normalize_hotspot_config(read_json(HOTSPOT_CONFIG, {})).get("connection_name")
    for row in parse_colon_rows(result.get("stdout", ""), ["name", "type", "device"]):
        name = row.get("name") or ""
        if configured and name == configured:
            return name
        if "hotspot" in name.lower() or "aquaculture" in name.lower():
            return name
    return None


def normalize_hotspot_config(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": normalize_ssid(str(data.get("name") or DEFAULT_HOTSPOT_NAME)),
        "connection_name": str(data.get("connection_name") or DEFAULT_HOTSPOT_CONNECTION),
        "applied": bool(data.get("applied", False)),
        "updated_at": data.get("updated_at"),
        "applied_at": data.get("applied_at"),
    }


def normalize_ssid(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def is_raspberry_pi() -> bool:
    try:
        model = Path("/proc/device-tree/model").read_text(errors="ignore").lower()
        return "raspberry pi" in model
    except Exception:
        return False


def read_json(path: Path, fallback: Any) -> Any:
    try:
        if not path.exists():
            return fallback
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_colon_rows(text: str, keys: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = split_nmcli_line(line)
        row = {key: parts[index] if index < len(parts) else "" for index, key in enumerate(keys)}
        rows.append(row)
    return rows


def split_nmcli_line(line: str) -> list[str]:
    parts: list[str] = []
    current = []
    escape = False
    for char in line:
        if escape:
            current.append(char)
            escape = False
        elif char == "\\":
            escape = True
        elif char == ":":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return fallback


def run(command: list[str], timeout: int = 15) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return {
            "command": " ".join(shlex.quote(part) for part in command),
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except FileNotFoundError:
        return {"command": command[0] if command else "", "returncode": 127, "stdout": "", "stderr": "Command not found."}
    except subprocess.TimeoutExpired:
        return {"command": " ".join(command), "returncode": 124, "stdout": "", "stderr": "Command timed out."}
    except Exception as exc:
        return {"command": " ".join(command), "returncode": 1, "stdout": "", "stderr": str(exc)}


def success(message: str, data: Any | None = None) -> dict[str, Any]:
    return {"ok": True, "message": message, "data": data if data is not None else {}}


def fail(message: str, details: Any | None = None, status_code: int = 400) -> dict[str, Any]:
    return {"ok": False, "message": message, "error": message, "details": details if details is not None else {}, "status_code": status_code}
