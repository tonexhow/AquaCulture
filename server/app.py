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

import os
import sys
from pathlib import Path
from typing import Any

from flask import Flask
from flask_socketio import SocketIO

BASE_DIR = Path(__file__).resolve().parents[1]
SERVER_DIR = BASE_DIR / "server"
CLIENT_DIR = BASE_DIR / "client"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

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


def create_app() -> tuple[Flask, SocketIO]:
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
        cors_allowed_origins="*",
        async_mode="threading",
        ping_timeout=30,
        ping_interval=10,
    )

    ensure_required_folders()
    prepare_database()
    register_backend(app, socketio)
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
        catalog = curate_species(db.db_path)
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
    print("[AquaCulture] API and Socket.IO routes registered.")


def start_backend_services(socketio: SocketIO) -> None:
    from settings.notification.summary_scheduler import start_summary_scheduler
    start_summary_scheduler()
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


app, socketio = create_app()


if __name__ == "__main__":
    start_optional_local_tools()

    print("=" * 60)
    print("AquaCulture Backend")
    print("Flask + Socket.IO realtime server")
    print(f"Local: http://127.0.0.1:{PORT}")
    print(f"LAN:   http://<raspberry-pi-ip>:{PORT}")
    print("=" * 60)

    socketio.run(app, host=HOST, port=PORT, debug=DEBUG, use_reloader=False, allow_unsafe_werkzeug=True)
