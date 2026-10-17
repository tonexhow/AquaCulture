"""One-time HTTPS reset links through AquaCulture's existing SMTP configuration."""
import html
import logging
import os
import secrets
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

from server.auth import DurableLimiter, GENERIC_RECOVERY, email_address
from . import admin_password
from .notification.email_alert import send_email_alert

_sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aquaculture-recovery")
_slots = threading.BoundedSemaphore(20)


class ResetTokenRedaction(logging.Filter):
    def filter(self, record):
        # Werkzeug's default access log includes query strings. Proxy logs require
        # the corresponding deployment configuration documented in SECURITY.md.
        record.msg = re.sub(r"([?&]token=)[^\s&\"']+", r"\1[REDACTED]", str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"([?&]token=)[^\s&\"']+", r"\1[REDACTED]", value) if isinstance(value, str) else value for value in record.args)
        return True


logging.getLogger("werkzeug").addFilter(ResetTokenRedaction())


def reset_origin():
    value = os.environ.get("AQUACULTURE_PUBLIC_ORIGIN", "").strip()
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise ValueError("Configure a valid HTTPS public origin.") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(c.isspace() or c in "\\\r\n" for c in value):
        raise ValueError("Configure AQUACULTURE_PUBLIC_ORIGIN as an HTTPS origin, without a path.")
    return value.rstrip("/")


def request_reset(email, client):
    email = email_address(email)
    store = admin_password.get_store()
    limiter = DurableLimiter(store.path.parent / "web_auth_limits.sqlite3")
    global_slot = limiter.reserve("reset_request_global", client, secrets.token_hex(16), daily=20, hour_limit=5)
    limiter.finish("reset_request_global", global_slot, True)
    address_slot = limiter.reserve("reset_request_address", email, secrets.token_hex(16), daily=100, hour_limit=3)
    limiter.finish("reset_request_address", address_slot, True)
    if _slots.acquire(blocking=False):
        try:
            future = _sender.submit(deliver_reset, store, email)
            future.add_done_callback(lambda _: _slots.release())
        except Exception:
            _slots.release()
    return {"ok": True, "message": GENERIC_RECOVERY}


def deliver_reset(store, email):
    token = None
    try:
        origin = reset_origin()
        token = store.issue_reset(email)
        if token:
            url = origin + "/reset-password?" + urlencode({"token": token})
            result = send_email_alert([email], "Reset your AquaCulture Admin password",
                "<h1>AquaCulture password reset</h1><p>This single-use link expires in 15 minutes.</p>"
                f'<p><a href="{html.escape(url, quote=True)}">Choose a new password</a></p>'
                "<p>If you did not request this, ignore this email.</p>",
                text_body=f"Choose a new AquaCulture Admin password (expires in 15 minutes): {url}")
            if not result.get("ok"):
                store.discard_reset(token)
                logging.getLogger(__name__).warning("Password reset email delivery failed.")
    except Exception:
        if token:
            store.discard_reset(token)
        logging.getLogger(__name__).warning("Password recovery delivery unavailable; check local configuration.")
