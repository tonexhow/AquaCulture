"""
AquaCulture SMS Alert Sender

Purpose:
- Send SMS notifications using the configured UniSMS URL.
- Read confidential UNISMS_URL from settings/notification/.env.
- Support URL templates with {contact} and {message} placeholders.
- Fail gracefully when credentials or recipients are missing.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
DEFAULT_TIMEOUT_SECONDS = 15
USER_AGENT = "AquaCulture"


@dataclass(slots=True)
class SmsCredentials:
    unisms_url: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.unisms_url)


def load_env_file(env_path: str | Path = ENV_PATH) -> dict[str, str]:
    """Load simple KEY=VALUE lines without requiring python-dotenv."""
    path = Path(env_path)
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def get_sms_credentials(env_path: str | Path = ENV_PATH) -> SmsCredentials:
    file_values = load_env_file(env_path)
    unisms_url = os.environ.get("UNISMS_URL", file_values.get("UNISMS_URL", "")).strip()
    return SmsCredentials(unisms_url=unisms_url)


def check_sms_env(env_path: str | Path = ENV_PATH) -> dict[str, object]:
    credentials = get_sms_credentials(env_path)
    missing = [] if credentials.unisms_url else ["UNISMS_URL"]
    return {
        "ok": not missing,
        "sms_ready": credentials.ready,
        "missing": missing,
        "message": "SMS environment ready." if not missing else "SMS .env value is missing.",
    }


def normalize_recipients(recipients: str | Iterable[str]) -> list[str]:
    if isinstance(recipients, str):
        parts = recipients.replace(";", ",").split(",")
        return [part.strip() for part in parts if part.strip()]
    return [str(item).strip() for item in recipients if str(item).strip()]


def send_sms_alert(
    to: str | Iterable[str],
    message: str,
    *,
    env_path: str | Path = ENV_PATH,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Send an SMS alert through the configured UniSMS URL."""
    recipients = normalize_recipients(to)
    if not recipients:
        return {"ok": False, "channel": "SMS", "message": "No SMS recipients supplied.", "sent": 0, "results": []}

    credentials = get_sms_credentials(env_path)
    if not credentials.ready:
        return {
            "ok": False,
            "channel": "SMS",
            "message": "SMS URL is missing. Set UNISMS_URL in settings/notification/.env.",
            "sent": 0,
            "results": [],
        }

    results: list[dict[str, object]] = []
    sent = 0

    for contact in recipients:
        url = build_sms_url(credentials.unisms_url, contact, message)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - admin-configured URL
                body = response.read(500).decode("utf-8", errors="replace")
                status = int(response.status)
            ok = 200 <= status < 300
            if ok:
                sent += 1
            results.append({"contact": contact, "ok": ok, "status": status, "response": body})
        except Exception as exc:  # noqa: BLE001 - backend should not crash from SMS failure
            results.append({"contact": contact, "ok": False, "status": None, "response": str(exc)})

    return {
        "ok": sent == len(recipients),
        "channel": "SMS",
        "message": "SMS sending completed." if sent else "SMS sending failed.",
        "sent": sent,
        "results": results,
    }


def build_sms_url(base_url: str, contact: str, message: str) -> str:
    encoded_contact = urllib.parse.quote_plus(contact)
    encoded_message = urllib.parse.quote_plus(message)

    if "{contact}" in base_url or "{message}" in base_url:
        return base_url.replace("{contact}", encoded_contact).replace("{message}", encoded_message)

    parsed = urllib.parse.urlsplit(base_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend([("contact", contact), ("message", message)])
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
