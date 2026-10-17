"""
AquaCulture - app.py

Entry point only.
- Creates Flask app.
- Creates Socket.IO server.
- Registers API routes from api_manager.py.
- Starts the single backend sensor reader.
- Runs on Raspberry Pi / local machine.

Run:
    python app.py

Open:
    http://127.0.0.1:5000
    http://<raspberry-pi-ip>:5000
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from flask import Flask
from flask_socketio import SocketIO

BASE_DIR = Path(__file__).resolve().parents[1]
SERVER_DIR = BASE_DIR / "server"
CLIENT_DIR = BASE_DIR / "client"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# Read the root .env before setting host, port, camera, notification and network options.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass

APP_TITLE = "AquaCulture"
HOST = os.environ.get("AQUACULTURE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AQUACULTURE_PORT", "5000"))
DEBUG = os.environ.get("AQUACULTURE_DEBUG", "0").lower() in {"1", "true", "yes"}
SECRET_KEY = os.environ.get("AQUACULTURE_SECRET_KEY") or os.urandom(32).hex()
_services_lock = threading.RLock()
_services_started = False


def create_app(*, start_services: bool = True, prepare_storage: bool = True) -> tuple[Flask, SocketIO]:
    """Build the server; isolated callers can disable hardware and storage work."""
    app = Flask(
        __name__,
        template_folder=str(CLIENT_DIR / "templates"),
        static_folder=str(CLIENT_DIR / "static"),
    )
    app.config["APP_TITLE"] = APP_TITLE
    app.config["JSON_SORT_KEYS"] = False
    app.config["SECRET_KEY"] = SECRET_KEY

    socketio = SocketIO(
        app,
        async_mode="threading",
        ping_timeout=30,
        ping_interval=10,
    )

    # Socket.IO's default same-origin policy also protects cookie-authenticated
    # hardware events. Do not allow arbitrary websites to open admin sockets.
    if prepare_storage:
        ensure_required_folders()
        prepare_database()
    register_backend(app, socketio)
    if start_services:
        start_backend_services(socketio)
    return app, socketio


def ensure_required_folders() -> None:
    for folder in (
        BASE_DIR / "database",
        CLIENT_DIR / "templates",
        CLIENT_DIR / "static",
        SERVER_DIR / "network",
    ):
        folder.mkdir(parents=True, exist_ok=True)


def prepare_database() -> None:
    try:
        from database.database import db

        db.create_tables()
        from database.media_storage import migrate_legacy_media
        media = migrate_legacy_media(db.db_path)
        if media.get("changed"):
            print(f"[AquaCulture] Media migrated: {media}")
        from database.catalog_migration import curate_species
        catalog = curate_species(db.db_path, archive_legacy=False)
        if catalog.get("applied"):
            print(f"[AquaCulture] Catalog curated: {catalog}")
        from database.maintenance import ensure_query_indexes
        indexes = ensure_query_indexes()
        print(f"[AquaCulture] Database ready ({indexes} safe indexes added).")
    except Exception as error:
        print(f"[AquaCulture] Database warning: {type(error).__name__}: {error}")


def register_backend(app: Flask, socketio: SocketIO) -> None:
    import api_manager

    api_manager.register_routes(app, socketio)
    api_manager.register_socket_events(socketio)
    import live_streaming

    set_status_callback = getattr(live_streaming.camera_manager, "set_status_callback", None)
    if set_status_callback:
        set_status_callback(lambda status: api_manager.emit(socketio, "camera_status", status))
    print("[AquaCulture] API and Socket.IO routes registered.")


def start_backend_services(socketio: SocketIO) -> None:
    global _services_started
    with _services_lock:
        if _services_started:
            return
        _services_started = True
        try:
            from settings.notification.reading_worker import start_reading_worker
            from settings.notification.summary_scheduler import start_summary_scheduler

            start_reading_worker()
            start_summary_scheduler()
        except Exception as error:
            print(f"[AquaCulture] Notification worker warning: {type(error).__name__}: {error}")
        try:
            import api_manager
            import sensors

            sensors.start_background_reader(event_callback=api_manager.socket_event_callback(socketio))
            print("[AquaCulture] Sensor reader started.")
        except Exception as error:
            print(f"[AquaCulture] Sensor reader warning: {type(error).__name__}: {error}")

        if os.environ.get("AQUACULTURE_CAMERA_AUTOSTART", "0") in {"1", "true", "True", "yes"}:
            try:
                import live_streaming

                live_streaming.start_camera()
                print("[AquaCulture] Camera started.")
            except Exception as error:
                print(f"[AquaCulture] Camera warning: {type(error).__name__}: {error}")


def stop_backend_services() -> None:
    """Stop creating work, release hardware, and bound worker shutdown waits."""
    global _services_started
    with _services_lock:
        if not _services_started:
            return
        _services_started = False
        # Modules have already been loaded during startup; avoid importing or
        # initializing credentials/storage as a side effect of process cleanup.
        stops = (
            ("settings.notification.reading_worker", "stop_reading_worker"),
            ("settings.notification.summary_scheduler", "stop_summary_scheduler"),
            ("sensors", "stop_background_reader"),
            ("live_streaming", "shutdown_camera"),
        )
        for module_name, function_name in stops:
            module = sys.modules.get(module_name)
            stop = getattr(module, function_name, None) if module else None
            if stop:
                try:
                    stop()
                except Exception as error:
                    print(f"[AquaCulture] Shutdown warning ({function_name}): {type(error).__name__}")


def _shutdown_signal(signum: int, frame: Any) -> None:
    stop_backend_services()
    raise SystemExit(0)


def start_optional_local_tools() -> None:
    """Keep optional local-only helpers separate from the main backend."""
    if os.environ.get("AQUACULTURE_NETWORK_PANEL", "0") not in {"1", "true", "True", "yes"}:
        return
    try:
        from network_app import start_network_server_in_background

        start_network_server_in_background(port=5001)
        print("[AquaCulture] Network Configuration running on port 5001.")
    except Exception as error:
        print(f"[AquaCulture] Network Configuration warning: {type(error).__name__}: {error}")


atexit.register(stop_backend_services)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _shutdown_signal)
    signal.signal(signal.SIGINT, _shutdown_signal)
    app, socketio = create_app()
    start_optional_local_tools()

    print("=" * 60)
    print("AquaCulture Backend")
    print("Flask + Socket.IO realtime server")
    print(f"Local: http://127.0.0.1:{PORT}")
    print(f"LAN:   http://<raspberry-pi-ip>:{PORT}")
    print("=" * 60)

    try:
        socketio.run(app, host=HOST, port=PORT, debug=DEBUG, use_reloader=False, allow_unsafe_werkzeug=True)
    finally:
        stop_backend_services()
