"""Admin authorization over MITM-authenticated encrypted BlueZ characteristics."""
import json
import secrets
import time
from dataclasses import dataclass, field

from server.auth import AuthUnavailable, CredentialStore, DurableLimiter, RateLimited


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class AuthorizationRequired(ValueError):
    pass


@dataclass
class Session:
    sid: str = field(default_factory=lambda: secrets.token_hex(16))
    expires: float = field(default_factory=lambda: time.monotonic() + 600)
    revision: str | None = None
    received: int = 0
    requests: set = field(default_factory=set)

    def check(self):
        if time.monotonic() >= self.expires:
            raise AuthorizationRequired("Session expired. Reconnect and verify the Admin password.")


class Authorization:
    def __init__(self, store, limiter):
        self.store, self.limiter = store, limiter
        self.sessions = {}
        self.challenge_times = {}
        self.delay_until = 0

    def bootstrap(self, peer):
        now = time.monotonic()
        if now - self.challenge_times.get(peer, -100) < 2:
            raise ValueError("Wait before reading another challenge.")
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
        self.challenge_times = {k: v for k, v in self.challenge_times.items() if now - v < 60}
        if len(self.sessions) >= 8 and peer not in self.sessions:
            raise ValueError("Too many connected setup clients.")
        session = Session()
        self.sessions[peer] = session
        self.challenge_times[peer] = now
        return {"v": 2, "device_name": "AquaCulture-Pi", "session": session.sid,
                "authentication_required": True, "authentication": "admin_password",
                "security": "authenticated_ble_pairing_required", "expires_in": 600}

    def accept(self, peer, request):
        session = self.sessions.get(peer)
        if session is None:
            raise AuthorizationRequired("Reconnect to begin a new session.")
        session.check()
        seq = request.get("counter")
        if request.get("session") != session.sid or type(seq) is not int or seq != session.received + 1:
            raise ValueError("Invalid or replayed session request.")
        request_id = request.get("id")
        if request_id in session.requests:
            raise ValueError("Duplicate request.")
        if len(session.requests) >= 256:
            raise AuthorizationRequired("Session request limit reached. Reconnect.")
        session.received = seq
        session.requests.add(request_id)
        return session

    def authenticate(self, peer, session, request_id, password):
        # Called only after BlueZ's authenticated-write permission check.
        if self.sessions.get(peer) is not session:
            raise AuthorizationRequired("Bluetooth disconnected.")
        session.revision = None
        if time.monotonic() < self.delay_until:
            raise RateLimited(self.delay_until - time.monotonic())
        reservation = self.limiter.reserve("ble_admin", peer, session.sid + "|" + request_id, hour_limit=20)
        try:
            revision = self.store.verify(password)
        except Exception:
            self.limiter.finish("ble_admin", reservation, False)
            raise AuthUnavailable("Local Admin verification is unavailable.") from None
        delay = self.limiter.finish("ble_admin", reservation, bool(revision))
        if not revision:
            self.delay_until = time.monotonic() + delay
            return {"authorized": False, "message": "Unable to verify the Admin password.",
                    "retry_after_seconds": delay}
        if self.sessions.get(peer) is not session:
            raise AuthorizationRequired("Bluetooth disconnected.")
        current = self.store.state()
        if not current["enabled"] or current["revision"] != revision:
            raise AuthorizationRequired("Admin credential changed. Verify again.")
        session.revision, session.expires = revision, time.monotonic() + 600
        return {"authorized": True, "message": "Admin verified.", "expires_in": 600}

    def get(self, peer):
        session = self.sessions.get(peer)
        if session is None or not session.revision:
            raise AuthorizationRequired("Admin verification required.")
        try:
            session.check()
        except AuthorizationRequired:
            session.revision = None
            raise
        try:
            current = self.store.state()
        except Exception:
            session.revision = None
            raise AuthorizationRequired("Local Admin verification unavailable. Reconnect later.") from None
        if not current["enabled"] or current["revision"] != session.revision:
            session.revision = None
            raise AuthorizationRequired("Admin password changed. Verify again.")
        return session

    def disconnect(self, peer):
        self.sessions.pop(peer, None)
        self.challenge_times.pop(peer, None)
