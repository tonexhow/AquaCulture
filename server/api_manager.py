"""
AquaCulture - api_manager.py

Purpose:
- All HTTP API endpoints for frontend <-> backend communication.
- All Socket.IO handlers for live frontend updates.
- Keeps app.py as entry point only.

This file does not read hardware directly. It talks to:
- sensors.py for Arduino Nano readings
- live_streaming.py for camera/stream state
- scanner/fish_recommendation.py for water recommendation
- scanner/size_detection.py for fish size prediction
- settings/* for admin settings, notifications, species, and database tools
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import secrets
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file
from server.auth import AuthUnavailable, RateLimited, GENERIC_RECOVERY

import live_streaming
import sensors

BASE_DIR = Path(__file__).resolve().parents[1]

ADMIN_LOGIN_ATTEMPTS: Dict[str, Dict[str, Any]] = {}
ADMIN_SESSIONS: Dict[str, Dict[str, Any]] = {}
LOGIN_MAX_WRONG = 5
LOGIN_COOLDOWN_SECONDS = 300
ADMIN_SESSION_SECONDS = 8 * 60 * 60
ADMIN_SESSION_COOKIE = "aquaculture_admin_token"
ADMIN_SESSION_STORE = BASE_DIR / "database" / "admin_sessions.json"
ADMIN_SESSIONS_LOADED = False


def _load_admin_sessions_once() -> None:
    """Keep admin sessions valid after a page refresh or backend restart."""
    global ADMIN_SESSIONS_LOADED
    if ADMIN_SESSIONS_LOADED:
        return
    ADMIN_SESSIONS_LOADED = True
    try:
        if not ADMIN_SESSION_STORE.exists():
            return
        data = json.loads(ADMIN_SESSION_STORE.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return
        now = time.time()
        for token, session in data.items():
            if not isinstance(token, str) or not isinstance(session, dict):
                continue
            if float(session.get("expires_at") or 0) > now:
                ADMIN_SESSIONS[token] = session
    except Exception:
        # Session persistence must never block the server.
        pass


def _save_admin_sessions() -> None:
    try:
        ADMIN_SESSION_STORE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        active = {
            token: session
            for token, session in ADMIN_SESSIONS.items()
            if float(session.get("expires_at") or 0) > now
        }
        tmp = ADMIN_SESSION_STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(active, indent=2), encoding="utf-8")
        tmp.replace(ADMIN_SESSION_STORE)
    except Exception:
        pass


def register_routes(app: Flask, socketio: Any = None) -> None:
    """Register every backend endpoint on the Flask app."""
    # Install query-token redaction before the first reset-page access is logged.
    from settings import password_recovery as _password_recovery

    @app.after_request
    def protect_recovery_responses(response):
        if request.path in {"/forgot-password", "/reset-password", "/api/prediction/status", "/api/size-detection/status"} or request.path.startswith("/api/admin/password/") or request.path == "/api/admin/recovery-email":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            if request.path in {"/forgot-password", "/reset-password"}:
                response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.get("/forgot-password")
    @app.get("/reset-password")
    def password_recovery_page():
        return render_template("password_recovery.html", resetting=request.path == "/reset-password")

    @app.post("/api/admin/password/forgot")
    def api_password_forgot():
        from settings.password_recovery import request_reset
        try:
            return ok(request_reset(payload().get("email"), get_client_key()))
        except RateLimited as error:
            return rate_limit_response(error, message=GENERIC_RECOVERY)
        except ValueError as error:
            return ok({"ok": False, "message": str(error)}, status=400)
        except Exception:
            return ok({"ok": True, "message": GENERIC_RECOVERY})

    @app.post("/api/admin/password/reset")
    def api_password_reset():
        from settings import admin_password
        limiter = reservation = None
        try:
            limiter, reservation = reserve_web_auth("web_reset")
            data = payload()
            changed = admin_password.get_store().reset(data.get("token"), data.get("new_password"), data.get("confirm_password"))
            limiter.finish("web_reset", reservation, changed)
            if not changed:
                return ok({"ok": False, "message": "This reset link is invalid or expired. Request a new link."}, status=400)
            revoke_admin_sessions()
            return ok({"ok": True, "message": "Password reset. Sign in again with your new password."})
        except RateLimited as error:
            return rate_limit_response(error)
        except ValueError as error:
            if limiter and reservation:
                limiter.finish("web_reset", reservation, False)
            return ok({"ok": False, "message": str(error)}, status=400)
        except Exception:
            return ok({"ok": False, "message": "Password reset is temporarily unavailable."}, status=503)

    @app.route("/api/admin/recovery-email", methods=["GET", "PATCH"])
    def api_recovery_email():
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        from settings import admin_password
        try:
            store = admin_password.get_store()
            if request.method == "GET":
                return ok({"ok": True, "email": store.state()["email"]})
            limiter, reservation = reserve_web_auth("web_admin")
            data = payload()
            changed = store.set_recovery_email(data.get("current_password"), data.get("email"))
            limiter.finish("web_admin", reservation, changed)
            return ok({"ok": changed, "message": "Recovery email saved." if changed else "Unable to verify the Admin password."}, status=200 if changed else 400)
        except RateLimited as error:
            return rate_limit_response(error)
        except ValueError as error:
            return ok({"ok": False, "message": str(error)}, status=400)
        except Exception:
            return ok({"ok": False, "message": "Recovery settings are temporarily unavailable."}, status=503)

    # ------------------------------------------------------------------
    # Pages / basic health
    # ------------------------------------------------------------------
    @app.get("/")
    def index_page() -> Any:
        template = BASE_DIR / "client" / "templates" / "index.html"
        if template.exists():
            return render_template("index.html")
        return jsonify({"ok": True, "name": "AquaCulture", "message": "Backend is running. Frontend can be added next."})

    @app.get("/water-testing")
    @app.get("/water")
    def water_testing_page() -> Any:
        return render_template("index.html")

    @app.get("/fish-scanning")
    @app.get("/fish")
    def fish_scanning_page() -> Any:
        return render_template("index.html")

    @app.get("/test")
    @app.get("/test.html")
    def legacy_test_redirect() -> Any:
        return redirect("/water-testing", code=302)

    @app.get("/live")
    def legacy_live_redirect() -> Any:
        return redirect("/fish-scanning", code=302)

    @app.get("/settings")
    @app.get("/settings.html")
    def settings_page() -> Any:
        if not read_admin_session():
            return redirect("/?login=settings-required", code=302)
        template = BASE_DIR / "client" / "templates" / "settings.html"
        if template.exists():
            return render_template("settings.html")
        return fail("Settings page is not installed.", 404)

    @app.get("/admin")
    @app.get("/admin.html")
    def admin_page() -> Any:
        return redirect("/settings", code=302)

    @app.get("/network")
    @app.get("/network-settings")
    @app.get("/network-settings.html")
    def network_settings_page() -> Any:
        """Open the local Network Setup app without exposing its control code on port 5000."""
        hostname = (request.host or "").split(":", 1)[0].strip().lower()
        online_host = hostname.endswith(".qzz.io") or hostname.endswith(".trycloudflare.com") or "cloudflare" in hostname
        if online_host:
            target = "http://10.42.0.1:5001/"
        elif hostname in {"", "0.0.0.0", "localhost", "127.0.0.1"}:
            target = "http://127.0.0.1:5001/"
        else:
            target = f"http://{hostname}:5001/"
        return redirect(target, code=302)

    @app.get("/api/health")
    def api_health() -> Any:
        return ok(
            {
                "name": "AquaCulture",
                "status": "healthy",
                "timestamp": now_iso(),
                "sensors": sensors.get_status(),
                "camera": live_streaming.get_status(),
            }
        )

    @app.get("/api/state")
    @app.get("/api/live/state")
    def api_live_state() -> Any:
        return ok(current_state(include_admin=bool(read_admin_session())))

    # ------------------------------------------------------------------
    # Sensor endpoints: no polling loop needed, frontend can use Socket.IO.
    # These endpoints are for one-time commands/status/manual refresh.
    # ------------------------------------------------------------------
    @app.get("/api/sensors/status")
    @app.get("/api/sensor/status")
    def api_sensor_status() -> Any:
        return ok(sensors.get_status())

    @app.post("/api/sensors/start")
    def api_sensors_start() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = sensors.start_background_reader(event_callback=socket_event_callback(socketio))
        emit(socketio, "sensor_status", result)
        return ok(result)

    @app.post("/api/sensors/stop")
    def api_sensors_stop() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = sensors.stop_background_reader()
        emit(socketio, "sensor_status", result)
        return ok(result)

    @app.post("/api/sensors/configure")
    def api_sensors_configure() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        data = payload()
        result = sensors.configure_serial(
            port=str(data.get("port", "") or ""),
            baudrate=safe_int(data.get("baudrate"), 115200),
            timeout_seconds=safe_float(data.get("timeout_seconds"), 1.0),
            reconnect_delay_seconds=safe_float(data.get("reconnect_delay_seconds"), 2.0),
        )
        emit(socketio, "sensor_status", result)
        return ok(result)

    @app.get("/api/sensors/latest")
    @app.get("/api/sensor/water/live")
    @app.get("/api/sensor/water/read")
    def api_sensors_latest() -> Any:
        allow_stale = request.args.get("allow_stale", "1") not in {"0", "false", "False", "no"}
        return ok(sensors.get_latest(allow_stale=allow_stale))

    @app.get("/api/sensors/history")
    def api_sensors_history() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        return ok(sensors.get_history(limit=safe_int(request.args.get("limit"), 50)))

    @app.get("/api/sensors/ultrasonic")
    @app.get("/api/sensor/ultrasonic")
    def api_sensors_ultrasonic() -> Any:
        return ok(sensors.read_ultrasonic_distance())

    # ------------------------------------------------------------------
    # Camera / streaming endpoints
    # ------------------------------------------------------------------
    @app.get("/api/camera/status")
    def api_camera_status() -> Any:
        return ok(live_streaming.get_status())

    @app.get("/api/stream/info")
    @app.get("/api/camera/stream-info")
    def api_stream_info() -> Any:
        return ok(live_streaming.get_stream_info())

    @app.post("/api/camera/start")
    @app.post("/api/camera/on")
    def api_camera_start() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = live_streaming.start_camera()
        emit(socketio, "camera_status", result)
        return ok(result)

    @app.post("/api/camera/stop")
    @app.post("/api/camera/off")
    def api_camera_stop() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = live_streaming.stop_camera()
        emit(socketio, "camera_status", result)
        return ok(result)

    @app.post("/api/camera/restart")
    def api_camera_restart() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = live_streaming.restart_camera()
        emit(socketio, "camera_status", result)
        return ok(result)

    @app.post("/api/stream/relay/start")
    def api_stream_relay_start() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = live_streaming.start_relay()
        emit(socketio, "stream_status", result)
        return ok(result)

    @app.post("/api/stream/relay/stop")
    def api_stream_relay_stop() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        result = live_streaming.stop_relay()
        emit(socketio, "stream_status", result)
        return ok(result)

    @app.get("/api/camera/snapshot")
    def api_camera_snapshot() -> Any:
        require_frame = request.args.get("require_frame") == "1"
        packet = live_streaming.snapshot_frame(allow_fallback=not require_frame)
        image = packet["image_bytes"]
        if not image:
            return fail("No camera frame is available.", 503)
        return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store",
            "X-Camera-Available": "0" if packet["placeholder"] else "1",
            "X-Camera-Source": packet["camera_source"] or "unavailable"})

    @app.get("/api/camera/snapshot-json")
    def api_camera_snapshot_json() -> Any:
        return ok(live_streaming.snapshot_data_url())

    @app.get("/api/camera/stream")
    def api_camera_stream() -> Any:
        return Response(live_streaming.mjpeg_frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------
    # Scanner: water recommendation
    # ------------------------------------------------------------------
    @app.post("/api/water/scan")
    @app.post("/api/recommendation/start")
    def api_water_scan() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from scanner import fish_recommendation

            session = read_admin_session() or {}
            data = payload()
            emit(socketio, "scan_status", {"kind": "recommendation", "running": True, "message": "Water recommendation scan started.", "started_by": session_public_data(session) if session else {}})
            result = fish_recommendation.scan_water_recommendation(
                session_id=safe_int_or_none(data.get("session_id")),
                duration_seconds=safe_float(data.get("duration_seconds"), 5.0),
                top=safe_int(data.get("top"), 3),
                sample_gap_seconds=safe_float(data.get("sample_gap_seconds"), 1.0),
                event_callback=socket_event_callback(socketio),
            )
            emit(socketio, "recommendation_status", fish_recommendation.get_recommendation_status())
            emit(socketio, "scan_status", {"kind": "recommendation", "running": False, "message": "Water recommendation scan finished.", "result": result})
            return ok(result)
        except Exception as error:
            emit(socketio, "scan_status", {"kind": "recommendation", "running": False, "message": "Water recommendation scan failed."})
            return exception_response("Water scan failed safely.", error)

    @app.post("/api/water/cancel")
    @app.post("/api/recommendation/cancel")
    def api_water_cancel() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from scanner import fish_recommendation

            result = fish_recommendation.cancel_recommendation()
            emit(socketio, "recommendation_status", fish_recommendation.get_recommendation_status())
            return ok(result)
        except Exception as error:
            return exception_response("Water scan cancel failed safely.", error)

    @app.get("/api/recommendation/status")
    def api_recommendation_status() -> Any:
        try:
            from scanner import fish_recommendation

            return ok(fish_recommendation.get_recommendation_status())
        except Exception as error:
            return exception_response("Recommendation status failed safely.", error)

    @app.post("/api/test/recommendation")
    @app.post("/api/recommendation/test")
    def api_recommendation_test() -> Any:
        try:
            from scanner import fish_recommendation

            data = payload()
            values = data.get("values") if isinstance(data.get("values"), dict) else data
            result = fish_recommendation.recommend_from_values(values=values, top=safe_int(data.get("top"), 3))
            return ok(result)
        except Exception as error:
            return exception_response("Recommendation test failed safely.", error)

    # ------------------------------------------------------------------
    # Scanner: visual size detection
    # ------------------------------------------------------------------
    @app.get("/api/models/status")
    def api_models_status() -> Any:
        return ok(_model_readiness())

    @app.post("/api/prediction/start")
    @app.post("/api/size-detection/start")
    def api_prediction_start() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        if not _model_readiness()["ready"]:
            return fail("Trained fish detection models have not been installed yet.", 409, code="MODEL_NOT_INSTALLED")
        try:
            from scanner import size_detection

            session = read_admin_session() or {}
            data = payload()
            emit(socketio, "scan_status", {"kind": "visual", "running": True,
                    "message": "Visual recording started.",
                    "started_by": session_public_data(session) if session else {}})

            emit_event = socket_event_callback(socketio)

            def visual_progress(event: Dict[str, Any]) -> None:
                # Background worker events must not require a Flask request context.
                emit_event(event)
                if event.get("stage") in {"complete", "failed", "cancelled"}:
                    emit(socketio, "scan_status", {"kind": "visual", "running": False,
                         "message": event.get("message") or "Visual scan finished."})
                    emit(socketio, "prediction_status", {"stage": event.get("stage"),
                         "running": False, "message": event.get("message") or ""})

            result = size_detection.start_prediction_background(
                session_id=safe_int_or_none(data.get("session_id")),
                duration_seconds=safe_float(data.get("duration_seconds"), 5.0),
                top=safe_int(data.get("top"), 5),
                target_fps=safe_float(data.get("target_fps"), 4.0),
                event_callback=visual_progress,
            )
            if not result.get("ok"):
                return ok(result, status=409)
            return ok(result, status=202)
        except Exception as error:
            emit(socketio, "scan_status", {"kind": "visual", "running": False, "message": "Visual size scan failed."})
            return exception_response("Prediction failed safely.", error)

    @app.post("/api/prediction/cancel")
    @app.post("/api/size-detection/cancel")
    def api_prediction_cancel() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from scanner import size_detection

            result = size_detection.cancel_prediction()
            emit(socketio, "prediction_status", size_detection.get_prediction_status())
            return ok(result)
        except Exception as error:
            return exception_response("Prediction cancel failed safely.", error)

    @app.get("/api/prediction/status")
    @app.get("/api/size-detection/status")
    def api_prediction_status() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from scanner import size_detection

            return ok(size_detection.get_prediction_status())
        except Exception as error:
            return exception_response("Prediction status failed safely.", error)

    @app.get("/api/prediction/<int:prediction_id>/download")
    def api_prediction_download(prediction_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from scanner import size_detection

            from database.database import db
            saved_path = db.get_prediction_zip_path(prediction_id)
            if saved_path:
                return send_file(saved_path, mimetype="application/zip", as_attachment=True,
                                 download_name=f"aquaculture_prediction_{prediction_id}.zip", conditional=True)
            data = size_detection.get_prediction_zip(prediction_id)
            if not data:
                return fail("Prediction ZIP was not found.", 404)
            return send_file(
                io.BytesIO(data),
                mimetype="application/zip",
                as_attachment=True,
                download_name=f"aquaculture_prediction_{prediction_id}.zip",
            )
        except Exception as error:
            return exception_response("Prediction download failed safely.", error)

    # ------------------------------------------------------------------
    # Settings: species, admin password, database tables, notifications
    # ------------------------------------------------------------------
    @app.get("/api/species")
    def api_species_list() -> Any:
        try:
            from settings import species

            include_image = request.args.get("include_image", "0") in {"1", "true", "True", "yes"}
            return ok(species.list_species(include_image=include_image))
        except Exception as error:
            return exception_response("Species list failed safely.", error)

    @app.get("/api/species/<int:species_id>")
    def api_species_get(species_id: int) -> Any:
        try:
            from settings import species

            return ok(species.get_species(species_id, include_image=True))
        except Exception as error:
            return exception_response("Species read failed safely.", error)

    @app.get("/api/species/reference")
    def api_species_reference() -> Any:
        try:
            from settings import species

            return ok(species.list_reference_species())
        except Exception as error:
            return exception_response("Reference species list failed safely.", error)

    @app.get("/api/species/<int:species_id>/image")
    def api_species_image(species_id: int) -> Any:
        try:
            from settings import species

            from database.media_storage import resolve_media_path, image_type
            with sqlite3.connect(species.DB_PATH, timeout=15) as connection:
                row = connection.execute("SELECT image_path, image FROM species WHERE id=?", (species_id,)).fetchone()
            if row:
                path = resolve_media_path(row[0], kind="species")
                if path and path.is_file():
                    # Older clients may send thumb=1; all views share one original.
                    with path.open("rb") as picture:
                        _, mime = image_type(picture.read(32))
                    response = send_file(path, mimetype=mime, max_age=86400, conditional=True)
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    return response
                # Compatibility with a legacy record until migration has completed.
                if isinstance(row[1], bytes):
                    mime = guess_mime(row[1])
                    if mime != "application/octet-stream":
                        return Response(row[1], mimetype=mime, headers={"X-Content-Type-Options": "nosniff"})
            return send_file(BASE_DIR / "client" / "static" / "images" / "fallback-species.svg",
                             mimetype="image/svg+xml", max_age=3600)
        except Exception as error:
            return exception_response("Species image failed safely.", error)

    @app.post("/api/admin/species")
    def api_species_add() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import species

            data = payload(include_form=True)
            if "image" in request.files:
                data["image"] = request.files["image"].read()
            return ok(species.add_species(data))
        except Exception as error:
            return exception_response("Species add failed safely.", error)

    @app.patch("/api/admin/species/<int:species_id>")
    @app.put("/api/admin/species/<int:species_id>")
    @app.post("/api/admin/species/<int:species_id>")
    def api_species_update(species_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import species

            data = payload(include_form=True)
            if "image" in request.files:
                data["image"] = request.files["image"].read()
            return ok(species.update_species(species_id, data))
        except Exception as error:
            return exception_response("Species update failed safely.", error)

    @app.delete("/api/admin/species/<int:species_id>")
    def api_species_delete(species_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import species

            return ok(species.delete_species(species_id))
        except Exception as error:
            return exception_response("Species delete failed safely.", error)


    @app.get("/api/admin/species/duplicates")
    @app.post("/api/admin/species/duplicates")
    def api_species_duplicates() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import species

            return ok(species.find_duplicate_names())
        except Exception as error:
            return exception_response("Species duplicate scan failed safely.", error)

    @app.post("/api/admin/password")
    @app.patch("/api/admin/password")
    @app.put("/api/admin/password")
    def api_admin_password() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import admin_password

            limiter, reservation = reserve_web_auth("web_admin")
            data = payload()
            result = admin_password.change_admin_password(
                current_password=str(data.get("current_password", "")),
                new_password=str(data.get("new_password", "")),
                require_current=True,
            )
            limiter.finish("web_admin", reservation, bool(result.get("ok")))
            status = 200 if result.get("ok") else 400
            if result.get("ok"):
                revoke_admin_sessions()
            return ok(result, status=status)
        except RateLimited as error:
            return rate_limit_response(error)
        except Exception as error:
            return ok({"ok": False, "message": "Admin password update is temporarily unavailable."}, status=503)

    @app.post("/api/admin/password/verify")
    def api_admin_password_verify() -> Any:
        try:
            from settings import admin_password

            limiter, reservation = reserve_web_auth("web_admin")

            client_key = get_client_key()
            cooldown = login_cooldown_remaining(client_key)
            if cooldown > 0:
                return ok(
                    {
                        "ok": False,
                        "locked": True,
                        "retry_after_seconds": cooldown,
                        "message": f"Too many wrong passwords. Try again in {format_seconds(cooldown)}.",
                    },
                    status=429,
                )

            data = payload()
            result = admin_password.verify_admin_password(str(data.get("password", "")))
            limiter.finish("web_admin", reservation, bool(result.get("ok")))
            if result.get("ok"):
                reset_login_attempts(client_key)
                session = create_admin_session(str(data.get("session_id") or ""), result.pop("credential_revision", None))
                result.update(session)
                response, status_code = ok(result, status=200)
                response.set_cookie(ADMIN_SESSION_COOKIE, session["admin_token"], max_age=ADMIN_SESSION_SECONDS, httponly=True, samesite="Lax", secure=admin_cookie_is_secure())
                return response, status_code

            result.pop("credential_revision", None)
            attempt = record_failed_login(client_key)
            retry_after = int(attempt.get("retry_after_seconds") or 0)
            result["remaining_attempts"] = max(0, LOGIN_MAX_WRONG - int(attempt.get("wrong_count", 0)))
            if retry_after > 0:
                result["locked"] = True
                result["retry_after_seconds"] = retry_after
                result["message"] = f"Too many wrong passwords. Try again in {format_seconds(retry_after)}."
                return ok(result, status=429)
            return ok(result, status=401)
        except RateLimited as error:
            return rate_limit_response(error)
        except Exception:
            return ok({"ok": False, "message": "Admin verification is temporarily unavailable."}, status=503)

    @app.post("/api/admin/logout")
    def api_admin_logout() -> Any:
        _load_admin_sessions_once()
        token = get_admin_token()
        if token:
            ADMIN_SESSIONS.pop(token, None)
            _save_admin_sessions()
        response, status_code = ok({"ok": True, "message": "Logged out.", "session_active": False})
        response.delete_cookie(ADMIN_SESSION_COOKIE)
        return response, status_code

    @app.get("/api/admin/session")
    def api_admin_session() -> Any:
        session = read_admin_session()
        if not session:
            return ok({"ok": True, "session_active": False})
        return ok({"ok": True, "session_active": True, **session_public_data(session)})

    @app.get("/api/configuration")
    def api_configuration() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings.notification import alert_system
            from settings import admin_password

            return ok({
                "notification": alert_system.get_notification_settings(),
                "alert_status": alert_system.get_alert_status(),
                "password_set": bool(admin_password.get_admin_password().get("password_set")),
            })
        except Exception as error:
            return exception_response("Configuration read failed safely.", error)

    @app.get("/api/admin/library")
    def api_admin_library() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from database.media_storage import list_media_library

            return ok(list_media_library(str(request.args.get("folder") or "species")))
        except ValueError as error:
            return fail(str(error), 404)
        except Exception as error:
            return exception_response("Library read failed safely.", error)

    @app.get("/api/admin/library/<folder_id>/<path:filename>")
    def api_admin_library_file(folder_id: str, filename: str) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from database.media_storage import image_type, resolve_library_file

            path = resolve_library_file(folder_id, filename)
            if path is None:
                return fail("Library file was not found.", 404)
            mime = "application/octet-stream"
            try:
                with path.open("rb") as image:
                    _, mime = image_type(image.read(32))
            except ValueError:
                if path.suffix.lower() == ".zip":
                    mime = "application/zip"
            download = request.args.get("download") == "1" or not mime.startswith("image/")
            response = send_file(path, mimetype=mime, as_attachment=download,
                                 download_name=path.name, conditional=True, max_age=0)
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except Exception as error:
            return exception_response("Library file read failed safely.", error)

    @app.route("/api/admin/notification", methods=["GET", "POST", "PATCH", "PUT"])
    def api_admin_notification() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings.notification import alert_system

            if request.method == "GET":
                return ok({"notification": alert_system.get_notification_settings(), "status": alert_system.get_alert_status()})
            notification = alert_system.save_notification_settings(payload())
            emit(socketio, "notification_settings", {"updated": True})
            return ok({"message": "Notification settings updated.", "notification": notification})
        except ValueError as error:
            return jsonify({"ok": False, "message": str(error)}), 400
        except Exception as error:
            return exception_response("Notification settings failed safely.", error)

    @app.get("/api/notification/status")
    def api_notification_status() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings.notification import alert_system

            return ok(alert_system.get_alert_status())
        except Exception as error:
            return exception_response("Notification status failed safely.", error)

    @app.route("/api/subscribers", methods=["GET", "POST", "DELETE"])
    def api_subscribers() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings.notification import alert_system

            if request.method == "GET":
                return ok(alert_system.get_subscribers())
            if request.method == "DELETE":
                return ok(alert_system.delete_subscribers(str(request.args.get("type") or "")))
            data = payload()
            result = alert_system.add_subscriber(str(data.get("type") or ""), str(data.get("contact") or ""))
            return ok(result, status=200 if result.get("ok") else 400)
        except Exception as error:
            return exception_response("Subscriber operation failed safely.", error)

    @app.route("/api/subscribers/<int:subscriber_id>", methods=["POST", "PATCH", "PUT", "DELETE"])
    def api_subscriber_id(subscriber_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings.notification import alert_system

            if request.method == "DELETE":
                return ok(alert_system.delete_subscriber(subscriber_id))
            return ok(alert_system.update_subscriber(subscriber_id, payload()))
        except Exception as error:
            return exception_response("Subscriber update failed safely.", error)


    @app.post("/api/database/reset-clear")
    @app.post("/api/admin/database/reset-clear")
    def api_database_reset_clear() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            result = database_tables.clear_everything_except_species()
            emit(socketio, "database_reset", result)
            return ok(result, status=200 if result.get("ok") else 500)
        except Exception as error:
            return exception_response("Database reset and clear failed safely.", error)

    @app.get("/api/database/tables")
    def api_database_tables() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            return ok(database_tables.list_tables())
        except Exception as error:
            return exception_response("Database table list failed safely.", error)

    @app.get("/api/database/<table_name>")
    def api_database_table(table_name: str) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            return ok(
                database_tables.read_table(
                    table_name,
                    limit=safe_int(request.args.get("limit"), 100),
                    offset=safe_int(request.args.get("offset"), 0),
                )
            )
        except Exception as error:
            return exception_response("Database read failed safely.", error)

    @app.get("/api/database/<table_name>/export.csv")
    def api_database_export_csv(table_name: str) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        from settings.database_tables import READ_ONLY_TABLES
        from database.model import MODEL_REGISTRY
        from database.database import quote_identifier
        import csv
        if table_name not in set(MODEL_REGISTRY) | READ_ONLY_TABLES:
            return fail("Table is not allowed.", 404)
        try:
            from database.database import db
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            with db.connection() as conn:
                # Skip deprecated binary columns: media is exported by reference.
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
                        if r[1] not in {"image", "data"}]
                writer.writerow(["rowid"] + cols if table_name == "log" else cols)
                sql_cols = ", ".join(quote_identifier(c) for c in cols)
                if table_name == "log":
                    sql_cols = "rowid, " + sql_cols
                for record in conn.execute(f"SELECT {sql_cols} FROM {quote_identifier(table_name)} LIMIT 20000"):
                    # Avoid CSV formula injection when opened in Excel.
                    values = []
                    for value in record:
                        text_value = "" if value is None else str(value)
                        if text_value.startswith(("=", "+", "-", "@", "\t", "\r")):
                            text_value = "'" + text_value
                        values.append(text_value)
                    writer.writerow(values)
            payload_csv = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
            result = send_file(io.BytesIO(payload_csv), mimetype="text/csv; charset=utf-8",
                               as_attachment=True, download_name=f"aquaculture_{table_name}.csv")
            result.headers["Cache-Control"] = "no-store"
            return result
        except Exception as error:
            return exception_response("Export failed safely.", error)

    @app.get("/api/database/<table_name>/<int:row_id>")
    def api_database_row(table_name: str, row_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            return ok(database_tables.get_row(table_name, row_id))
        except Exception as error:
            return exception_response("Database row read failed safely.", error)

    @app.post("/api/database/<table_name>")
    def api_database_insert(table_name: str) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            return ok(database_tables.insert_row(table_name, payload()))
        except Exception as error:
            return exception_response("Database insert failed safely.", error)

    @app.patch("/api/database/<table_name>/<int:row_id>")
    @app.put("/api/database/<table_name>/<int:row_id>")
    def api_database_update(table_name: str, row_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            return ok(database_tables.update_row(table_name, row_id, payload()))
        except Exception as error:
            return exception_response("Database update failed safely.", error)

    @app.delete("/api/database/<table_name>/<int:row_id>")
    def api_database_delete(table_name: str, row_id: int) -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        try:
            from settings import database_tables

            return ok(database_tables.delete_row(table_name, row_id))
        except Exception as error:
            return exception_response("Database delete failed safely.", error)

    @app.post("/api/notification/test")
    @app.post("/api/admin/notification/test")
    def api_notification_test() -> Any:
        auth_error = require_admin_session_response()
        if auth_error is not None:
            return auth_error
        data = payload()
        channel = str(data.get("channel", "all"))
        try:
            from settings.notification import alert_system

            result = alert_system.send_test_notification(channel)
            return ok(result, status=200 if result.get("ok") else 400)
        except Exception as error:
            return exception_response("Notification test failed safely.", error)



def get_client_key() -> str:
    # Forwarded headers are not trusted client identities without a configured proxy.
    return str(request.remote_addr or "local")


def reserve_web_auth(scope):
    from settings.admin_password import get_store
    from server.auth import DurableLimiter
    limiter = DurableLimiter(get_store().path.parent / "web_auth_limits.sqlite3")
    return limiter, limiter.reserve(scope, get_client_key(), secrets.token_hex(16))


def rate_limit_response(error, message=None):
    response, status = ok({"ok": False, "message": message or str(error), "retry_after_seconds": error.retry_after}, status=429)
    response.headers["Retry-After"] = str(error.retry_after)
    return response, status


def revoke_admin_sessions():
    ADMIN_SESSIONS.clear()
    _save_admin_sessions()


def login_cooldown_remaining(client_key: str) -> int:
    now = time.time()
    data = ADMIN_LOGIN_ATTEMPTS.get(client_key) or {}
    locked_until = float(data.get("locked_until") or 0)
    if locked_until <= now:
        if locked_until:
            ADMIN_LOGIN_ATTEMPTS.pop(client_key, None)
        return 0
    return max(1, int(locked_until - now))


def record_failed_login(client_key: str) -> Dict[str, Any]:
    now = time.time()
    data = ADMIN_LOGIN_ATTEMPTS.setdefault(client_key, {"wrong_count": 0, "locked_until": 0})
    if float(data.get("locked_until") or 0) > now:
        data["retry_after_seconds"] = max(1, int(float(data["locked_until"]) - now))
        return data
    data["wrong_count"] = int(data.get("wrong_count") or 0) + 1
    if int(data["wrong_count"]) >= LOGIN_MAX_WRONG:
        data["locked_until"] = now + LOGIN_COOLDOWN_SECONDS
        data["retry_after_seconds"] = LOGIN_COOLDOWN_SECONDS
    else:
        data["retry_after_seconds"] = 0
    return data


def reset_login_attempts(client_key: str) -> None:
    ADMIN_LOGIN_ATTEMPTS.pop(client_key, None)


def create_admin_session(frontend_session_id: str = "", credential_revision=None) -> Dict[str, Any]:
    _load_admin_sessions_once()
    token = secrets.token_urlsafe(32)
    created_at = time.time()
    session = {
        "admin_token": token,
        "frontend_session_id": frontend_session_id.strip() or "browser",
        "created_at": created_at,
        "expires_at": created_at + ADMIN_SESSION_SECONDS,
        "client_key": get_client_key(),
        "credential_revision": credential_revision,
    }
    ADMIN_SESSIONS[token] = session
    _save_admin_sessions()
    return {"session_active": True, **session_public_data(session), "admin_token": token}


def get_admin_token() -> str:
    header = request.headers.get("X-AquaCulture-Admin-Token", "").strip()
    if header:
        return header
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization.split(None, 1)[1].strip()
    cookie = request.cookies.get(ADMIN_SESSION_COOKIE, "").strip()
    if cookie:
        return cookie
    body = request.get_json(silent=True) if request.is_json else None
    if isinstance(body, dict):
        return str(body.get("admin_token") or body.get("token") or "").strip()
    return ""


def admin_cookie_is_secure() -> bool:
    """Keep public HTTPS cookies secure without disabling the separate LAN login.

    The public recovery URL describes a different origin from a Pi's HTTP LAN
    address. Do not trust arbitrary forwarded headers to choose cookie policy.
    """
    if request.is_secure:
        return True
    public = urlsplit(os.environ.get("AQUACULTURE_PUBLIC_ORIGIN", ""))
    return bool(public.scheme == "https" and public.hostname
                and public.hostname == urlsplit(request.url_root).hostname)


def read_admin_session() -> Optional[Dict[str, Any]]:
    _load_admin_sessions_once()
    token = get_admin_token()
    if not token:
        return None
    session = ADMIN_SESSIONS.get(token)
    if not session:
        return None
    try:
        from settings.admin_password import get_store
        current = get_store().state()
        if not current["enabled"] or current["revision"] != session.get("credential_revision"):
            ADMIN_SESSIONS.pop(token, None)
            _save_admin_sessions()
            return None
    except Exception:
        return None
    if float(session.get("expires_at") or 0) <= time.time():
        ADMIN_SESSIONS.pop(token, None)
        _save_admin_sessions()
        return None
    return session


def session_public_data(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "frontend_session_id": session.get("frontend_session_id") or "browser",
        "created_at": datetime.fromtimestamp(float(session.get("created_at") or time.time())).isoformat(timespec="seconds"),
        "expires_at": datetime.fromtimestamp(float(session.get("expires_at") or time.time())).isoformat(timespec="seconds"),
        "expires_in_seconds": max(0, int(float(session.get("expires_at") or 0) - time.time())),
    }


def require_admin_session_response() -> Any | None:
    session = read_admin_session()
    if session:
        return None
    return ok(
        {
            "ok": False,
            "session_required": True,
            "message": "An active admin session is required for this action.",
        },
        status=401,
    )


def format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, remaining = divmod(seconds, 60)
    if minutes and remaining:
        return f"{minutes} minute(s) and {remaining} second(s)"
    if minutes:
        return f"{minutes} minute(s)"
    return f"{remaining} second(s)"

def register_socket_events(socketio: Any) -> None:
    """Register Socket.IO events. Works with Flask-SocketIO."""
    if socketio is None:
        return

    @socketio.on("connect")
    def socket_connect() -> None:
        emit(socketio, "system_state", current_state(include_admin=bool(read_admin_session())), broadcast=False)

    @socketio.on("client:ping")
    def socket_ping(data: Optional[Dict[str, Any]] = None) -> None:
        emit(socketio, "server:pong", {"ok": True, "timestamp": now_iso(), "echo": data or {}}, broadcast=False)

    @socketio.on("sensors:start")
    def socket_sensors_start(data: Optional[Dict[str, Any]] = None) -> None:
        if not require_socket_admin_session(socketio):
            return
        result = sensors.start_background_reader(event_callback=socket_event_callback(socketio))
        emit(socketio, "sensor_status", result)

    @socketio.on("sensors:stop")
    def socket_sensors_stop(data: Optional[Dict[str, Any]] = None) -> None:
        if not require_socket_admin_session(socketio):
            return
        result = sensors.stop_background_reader()
        emit(socketio, "sensor_status", result)

    @socketio.on("sensors:latest")
    def socket_sensors_latest(data: Optional[Dict[str, Any]] = None) -> None:
        emit(socketio, "sensor_latest", sensors.get_latest(), broadcast=False)

    @socketio.on("camera:start")
    def socket_camera_start(data: Optional[Dict[str, Any]] = None) -> None:
        if not require_socket_admin_session(socketio):
            return
        result = live_streaming.start_camera()
        emit(socketio, "camera_status", result)

    @socketio.on("camera:stop")
    def socket_camera_stop(data: Optional[Dict[str, Any]] = None) -> None:
        if not require_socket_admin_session(socketio):
            return
        result = live_streaming.stop_camera()
        emit(socketio, "camera_status", result)

    @socketio.on("camera:status")
    def socket_camera_status(data: Optional[Dict[str, Any]] = None) -> None:
        emit(socketio, "camera_status", live_streaming.get_status(), broadcast=False)


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------
def require_socket_admin_session(socketio: Any) -> bool:
    if read_admin_session():
        return True
    emit(socketio, "admin_required", {
        "ok": False, "session_required": True,
        "message": "An active admin session is required for this action.",
    }, broadcast=False)
    return False


def current_state(*, include_admin: bool = False) -> Dict[str, Any]:
    return {
        "timestamp": now_iso(),
        "sensors": sensors.get_status(),
        "camera": live_streaming.get_status(),
        "stream": live_streaming.get_stream_info(),
        "scanner": scanner_status(),
        "alerts": notification_status_safe() if include_admin else {"ok": True, "admin_only": True},
    }


def notification_status_safe() -> Dict[str, Any]:
    try:
        from settings.notification import alert_system

        return alert_system.get_alert_status()
    except Exception as error:
        return {"ok": False, "message": _safe_error(error)}


def scanner_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {}
    try:
        from scanner import fish_recommendation

        status["recommendation"] = fish_recommendation.get_recommendation_status()
    except Exception as error:
        status["recommendation"] = {"ok": False, "message": _safe_error(error)}
    try:
        from scanner import size_detection

        status["prediction"] = size_detection.get_prediction_status()
    except Exception as error:
        status["prediction"] = {"ok": False, "message": _safe_error(error)}
    return status


def socket_event_callback(socketio: Any) -> Optional[Any]:
    if socketio is None:
        return None

    def on_alert_result(alert_result: Dict[str, Any]) -> None:
        if alert_result.get("sent") or alert_result.get("pending") or alert_result.get("invalid_reading"):
            emit(socketio, "alert_status", alert_result)

    def callback(event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "system_event")
        if event_type == "sensor_reading":
            reading = event.get("reading") or event
            sensor_status = event.get("status") or sensors.get_status()
            emit(socketio, "sensor_latest", reading)
            emit(socketio, "sensor_status", sensor_status)
            try:
                from settings.notification.reading_worker import queue_sensor_reading

                queue_sensor_reading(reading, sensor_status, on_alert_result)
            except Exception:
                pass
        elif event_type == "sensor_status":
            sensor_status = event.get("status") or event
            emit(socketio, "sensor_status", sensor_status)
            if (sensor_status.get("connected") is False or sensor_status.get("running") is False
                    or sensor_status.get("fresh") is False):
                try:
                    from settings.notification.reading_worker import queue_sensor_reading

                    queue_sensor_reading({}, sensor_status, on_alert_result)
                except Exception:
                    pass
        elif event_type in {"water", "sensor_sample"}:
            emit(socketio, "recommendation_event", event)
        elif event_type == "prediction":
            emit(socketio, "prediction_event", event)
        else:
            emit(socketio, event_type, event)

    return callback


def emit(socketio: Any, event_name: str, data: Dict[str, Any], broadcast: bool = True) -> None:
    if socketio is None:
        return
    if not broadcast:
        # Server-level Socket.IO emit broadcasts unless an explicit recipient is
        # supplied. Never send an admin's initial state to every connected guest.
        recipient = getattr(request, "sid", None)
        if recipient:
            try:
                socketio.emit(event_name, data, to=recipient)
            except Exception:
                pass
        return
    try:
        socketio.emit(event_name, data, broadcast=broadcast)
    except TypeError:
        try:
            socketio.emit(event_name, data)
        except Exception:
            pass
    except Exception:
        pass


def payload(include_form: bool = False) -> Dict[str, Any]:
    if request.is_json:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}
    if include_form and request.form:
        return dict(request.form.items())
    if request.data:
        try:
            value = json.loads(request.data.decode("utf-8", errors="replace"))
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    return {}


def ok(data: Dict[str, Any], status: int = 200) -> Any:
    if isinstance(data, dict) and "ok" not in data:
        data = {"ok": True, **data}
    return jsonify(data), status


def fail(message: str, status: int = 400, **extra: Any) -> Any:
    return jsonify({"ok": False, "message": message, **extra}), status


def exception_response(message: str, error: Exception, status: int = 500) -> Any:
    return fail(
        message,
        status,
        error=_safe_error(error),
        traceback=traceback.format_exc(limit=3),
    )


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_int_or_none(value: Any) -> Optional[int]:
    try:
        if value in {None, "", "null", "None"}:
            return None
        return int(value)
    except Exception:
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def decode_blob_value(value: Any) -> Optional[bytes]:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]
        try:
            return base64.b64decode(value)
        except Exception:
            return None
    return None


def guess_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) > 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return "application/octet-stream"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _model_readiness() -> Dict[str, Any]:
    """Check for complete local NCNN exports or explicitly enabled .pt weights.

    Detecting files is not proof that inference/calibration will succeed.
    """
    import os
    model_dir = BASE_DIR / "models"
    ncnn = False
    pt = False
    if model_dir.is_dir():
        for item in model_dir.rglob("*.param"):
            if item.name.endswith(".ncnn.param") and item.with_suffix(".bin").is_file():
                ncnn = True
                break
        pt = any(model_dir.rglob("*.pt"))
    pt_enabled = os.environ.get("AQUACULTURE_FORCE_PT_YOLO", "0").strip().lower() in {"1", "true", "yes"}
    ready = ncnn or (pt and pt_enabled)
    return {"ready": ready, "ncnn_present": ncnn, "pt_present": pt,
            "message": "Candidate model files found; inference has not been verified." if ready
            else "No enabled compatible model files are installed."}
