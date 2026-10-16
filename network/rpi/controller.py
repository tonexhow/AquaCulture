"""Per-connection authorization and command routing; no Flask dependency."""
import asyncio
import json
import logging
import re
import time
from aquaculture_auth import AuthUnavailable, RateLimited
from .authentication import AuthorizationRequired, encode
from .protocol import Reassembler
from .wifi_manager import ProvisionError, device_information


class Controller:
    def __init__(self, authorization, manager, send):
        self.auth, self.manager, self.send = authorization, manager, send
        self.buffers, self.tasks, self.inflight, self.requests_at = {}, set(), {}, {}

    def disconnect(self, peer):
        self.auth.disconnect(peer)
        self.buffers.pop(peer, None)
        self.requests_at.pop(peer, None)
        self.inflight.pop(peer, None)

    def write(self, peer, frame):
        if peer not in self.auth.sessions:
            raise ValueError("Read device information first.")
        raw = self.buffers.setdefault(peer, Reassembler()).feed(frame)
        if raw is None:
            return
        if peer in self.inflight:
            raise ValueError("Wait for the current request.")
        now = time.monotonic()
        if now - self.requests_at.get(peer, -100) < 0.1:
            raise ValueError("Requests are arriving too quickly.")
        self.requests_at[peer] = now
        task = asyncio.create_task(self.handle(peer, raw))
        self.inflight[peer] = task
        self.tasks.add(task)
        def completed(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                logging.getLogger(__name__).warning("BLE response delivery unavailable.")
        task.add_done_callback(completed)

    def release(self, peer):
        if self.inflight.get(peer) is asyncio.current_task():
            self.inflight.pop(peer, None)

    async def reply(self, peer, response, session=None):
        try:
            current = self.auth.get(peer)
            if session is not None and current is not session:
                return
            await self.send(peer, encode({"session": current.sid, **response}),
                            sensitive=True, revision=current.revision)
        except (AuthorizationRequired, AuthUnavailable):
            return

    async def handle(self, peer, raw):
        request_id, session = None, None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or set(request) - {"id", "type", "payload", "session", "counter"}:
                raise ValueError("Invalid request.")
            request_id = request.get("id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
                raise ValueError("A unique request ID is required.")
            session = self.auth.accept(peer, request)
            command, payload = request.get("type"), request.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("Invalid parameters.")
            if command == "admin_auth":
                if set(payload) != {"password"}:
                    raise ValueError("A password is required.")
                try:
                    result = await asyncio.to_thread(self.auth.authenticate, peer, session, request_id, payload.get("password"))
                finally:
                    payload.clear()
                self.release(peer)
                if self.auth.sessions.get(peer) is session:
                    await self.send(peer, encode({"id": request_id, "session": session.sid, "type": "result",
                        "status": "IDLE" if result["authorized"] else "FAILED", "payload": result, "final": True}))
                return
            if command == "auth_state":
                try:
                    self.auth.get(peer)
                    authorized = True
                except AuthorizationRequired:
                    authorized = False
                self.release(peer)
                await self.send(peer, encode({"id": request_id, "session": session.sid, "type": "result",
                    "status": "IDLE", "payload": {"authorized": authorized}, "final": True}))
                return
            session = self.auth.get(peer)  # Every privileged command checks current credentials.
            if command != "configure" and payload:
                raise ValueError("This command takes no parameters.")

            async def progress(status, data):
                await self.reply(peer, {"id": request_id, "type": "progress", "status": status, "payload": data, "final": False}, session)

            if command == "info":
                result = {**device_information(), "network": await self.manager.status()}
                status = "IDLE"
            elif command == "status":
                result = await self.manager.status(test_internet=True)
                status = result["state"]
            elif command == "history":
                result, status = {"connections": self.manager.history.recent()}, "IDLE"
            elif command == "scan":
                await progress("SCANNING", {})
                self.auth.get(peer)
                result, status = {"networks": await self.manager.scan()}, "SCAN_COMPLETE"
            elif command == "configure":
                result = await self.manager.configure(payload, progress)
                status = result["state"]
            else:
                raise ValueError("Unknown command.")
            self.release(peer)
            await self.reply(peer, {"id": request_id, "type": "result", "status": status, "payload": result, "final": True}, session)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            category = ("RATE_LIMITED" if isinstance(error, RateLimited) else
                        "AUTH_REQUIRED" if isinstance(error, AuthorizationRequired) else
                        "AUTH_UNAVAILABLE" if isinstance(error, AuthUnavailable) else
                        error.category if isinstance(error, ProvisionError) else "INVALID_REQUEST")
            message = str(error) if isinstance(error, (RateLimited, AuthorizationRequired, ProvisionError)) else "Unable to process this request."
            response = {"id": request_id, "type": "error", "status": "FAILED",
                        "error": {"category": category, "message": message}, "final": True}
            if isinstance(error, RateLimited):
                response["retry_after_seconds"] = error.retry_after
            if session:
                response["session"] = session.sid
            self.release(peer)
            if peer in self.auth.sessions:
                await self.send(peer, encode(response))
        finally:
            self.release(peer)

    async def publish(self, status):
        for peer, session in list(self.auth.sessions.items()):
            if peer in self.inflight:
                continue
            previous = session.revision
            try:
                self.auth.get(peer)
            except AuthorizationRequired:
                if previous:
                    session.revision = None
                    await self.send(peer, encode({"type": "authorization_expired", "session": session.sid,
                                                 "message": "Admin authorization expired or changed."}))
                continue
            await self.reply(peer, {"id": None, "type": "network", "status": status["state"], "payload": status, "final": False}, session)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
