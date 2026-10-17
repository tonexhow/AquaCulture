"""
AquaCulture - network_app.py

Separate local network panel.
- Runs on port 5001.
- Not imported by the main app on port 5000.
- Intended to stay local / hidden from the hosted Cloudflare tunnel.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from threading import Thread
from typing import Any

from flask import Flask, jsonify, render_template, request

from network import wifi_manager as network_manager

APP_TITLE = "AquaCulture Network"
HOST = os.environ.get("AQUACULTURE_NETWORK_HOST", "127.0.0.1")
PORT = int(os.environ.get("AQUACULTURE_NETWORK_PORT", "5001"))
DEBUG = os.environ.get("AQUACULTURE_NETWORK_DEBUG", "0") in {"1", "true", "True", "yes"}
LOCAL_ONLY = os.environ.get("AQUACULTURE_NETWORK_LOCAL_ONLY", "1") not in {"0", "false", "False", "no", "NO"}

BASE_DIR = Path(__file__).resolve().parents[1]


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "client" / "templates"),
        static_folder=str(BASE_DIR / "client" / "static"),
    )
    app.config["APP_TITLE"] = APP_TITLE
    app.config["JSON_SORT_KEYS"] = False

    ensure_required_folders()

    @app.before_request
    def keep_panel_local() -> Any:
        if not LOCAL_ONLY:
            return None
        if (request.path or "").startswith("/static/"):
            return None

        remote = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        if not remote:
            return None
        try:
            ip = ipaddress.ip_address(remote)
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                return None
        except ValueError:
            return None

        return jsonify({
            "ok": False,
            "error": "Network panel is local-only.",
            "hint": "Open it from the Raspberry Pi, LAN, local hotspot, or USB-tethered device.",
        }), 403

    @app.get("/")
    @app.get("/network")
    @app.get("/network.html")
    def network_page() -> Any:
        return render_template("network.html")

    @app.get("/api/network/status")
    def api_status() -> Any:
        return jsonify({"ok": True, "data": network_manager.get_status()})

    @app.get("/api/network/saved")
    def api_saved() -> Any:
        return result(network_manager.get_saved_networks())

    @app.post("/api/network/scan")
    def api_scan() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.scan_wifi(interface=str(data.get("interface") or "wlan0")))

    @app.post("/api/network/connect")
    def api_connect() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.connect_wifi(
            ssid=str(data.get("ssid") or "").strip(),
            password=str(data.get("password") or ""),
            interface=str(data.get("interface") or "wlan0").strip() or "wlan0",
            hidden=bool(data.get("hidden", False)),
        ))

    @app.post("/api/network/saved/connect")
    def api_saved_connect() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.connect_saved(
            name=str(data.get("name") or data.get("ssid") or "").strip(),
            interface=str(data.get("interface") or "wlan0").strip() or "wlan0",
        ))

    @app.post("/api/network/forget")
    def api_forget() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.forget_network(str(data.get("name") or data.get("ssid") or "").strip()))

    @app.post("/api/network/modify")
    def api_modify() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.modify_network(
            name=str(data.get("name") or data.get("ssid") or "").strip(),
            new_name=str(data.get("new_name") or data.get("ssid") or data.get("name") or "").strip(),
            password=str(data.get("password") or ""),
            autoconnect=data.get("autoconnect"),
        ))

    @app.post("/api/network/mode")
    def api_mode() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.select_mode(str(data.get("mode") or "wlan").strip().lower()))

    @app.post("/api/network/hotspot")
    def api_hotspot() -> Any:
        data = request.get_json(silent=True) or {}
        return result(network_manager.set_hotspot_name(str(data.get("name") or data.get("ssid") or "").strip()))

    @app.post("/api/network/go-live")
    def api_go_live() -> Any:
        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode") or "").strip().lower() or None
        return result(network_manager.go_live(mode=mode))

    return app


def ensure_required_folders() -> None:
    for folder in (
        BASE_DIR / "client" / "templates",
        BASE_DIR / "client" / "static",
        BASE_DIR / "client" / "static" / "network",
        BASE_DIR / "client" / "static" / "global",
        BASE_DIR / "server" / "network",
    ):
        folder.mkdir(parents=True, exist_ok=True)


def result(data: dict[str, Any]) -> Any:
    status = 200 if data.get("ok", False) else int(data.get("status_code") or 400)
    return jsonify(data), status


def start_network_server_in_background(port: int = PORT) -> Thread:
    """Optional legacy panel. Disabled unless explicitly requested in app.py.

    The panel defaults to loopback only; a future BLE service will provide
    authenticated network provisioning without needing a screen.
    """
    worker = Thread(
        target=lambda: create_app().run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True),
        name="AquaCulture-Network-Panel", daemon=True,
    )
    worker.start()
    return worker


if __name__ == "__main__":
    app = create_app()
    print(f"[AquaCulture Network] Running on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)
