"""
AquaCulture Email Alert Sender

Purpose:
- Send email notifications through Gmail SMTP.
- Read confidential EMAIL_USER and EMAIL_APP_PASSWORD from settings/notification/.env.
- Fail gracefully when credentials or recipients are missing.

This module does not store or modify non-confidential app settings.
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USE_TLS = True
SENDER_TITLE = "AquaCulture Notification"
DEFAULT_TIMEOUT_SECONDS = 15


@dataclass(slots=True)
class EmailCredentials:
    email_user: str = ""
    email_app_password: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.email_user and self.email_app_password)


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


def get_email_credentials(env_path: str | Path = ENV_PATH) -> EmailCredentials:
    file_values = load_env_file(env_path)

    def pick(key: str) -> str:
        return os.environ.get(key, file_values.get(key, "")).strip()

    return EmailCredentials(
        email_user=pick("EMAIL_USER"),
        email_app_password=pick("EMAIL_APP_PASSWORD"),
    )


def check_email_env(env_path: str | Path = ENV_PATH) -> dict[str, object]:
    credentials = get_email_credentials(env_path)
    missing: list[str] = []
    if not credentials.email_user:
        missing.append("EMAIL_USER")
    if not credentials.email_app_password:
        missing.append("EMAIL_APP_PASSWORD")

    return {
        "ok": not missing,
        "email_ready": credentials.ready,
        "missing": missing,
        "message": "Email environment ready." if not missing else "Email .env values are missing.",
    }


def normalize_recipients(recipients: str | Iterable[str]) -> list[str]:
    if isinstance(recipients, str):
        parts = recipients.replace(";", ",").split(",")
        return [part.strip() for part in parts if part.strip()]
    return [str(item).strip() for item in recipients if str(item).strip()]


def send_email_alert(
    to: str | Iterable[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    env_path: str | Path = ENV_PATH,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Send an HTML email alert."""
    recipients = normalize_recipients(to)
    if not recipients:
        return {"ok": False, "channel": "EMAIL", "message": "No email recipients supplied.", "sent": 0}

    credentials = get_email_credentials(env_path)
    if not credentials.ready:
        return {
            "ok": False,
            "channel": "EMAIL",
            "message": "Email credentials are missing. Set EMAIL_USER and EMAIL_APP_PASSWORD in settings/notification/.env.",
            "sent": 0,
        }

    plain_text = text_body or html_to_text(html_body)

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{SENDER_TITLE} <{credentials.email_user}>"
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(plain_text, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(credentials.email_user, credentials.email_app_password)
            smtp.sendmail(credentials.email_user, recipients, message.as_string())
        return {"ok": True, "channel": "EMAIL", "message": "Email sent.", "sent": len(recipients)}
    except Exception as exc:  # noqa: BLE001 - backend should not crash from email failure
        return {"ok": False, "channel": "EMAIL", "message": f"Email sending failed: {exc}", "sent": 0}


def html_to_text(html_body: str) -> str:
    text = html_body.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    for tag in ("</p>", "</div>", "</h1>", "</h2>", "</h3>", "</li>"):
        text = text.replace(tag, "\n")

    cleaned: list[str] = []
    in_tag = False
    for char in text:
        if char == "<":
            in_tag = True
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            cleaned.append(char)
    return html.unescape("".join(cleaned)).strip()
