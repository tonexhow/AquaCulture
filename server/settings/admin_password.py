"""
AquaCulture Admin Password Settings

Verify/change the one authoritative hashed Admin credential. The legacy JSON
password is imported once without changing the existing password or preferences.
"""

from __future__ import annotations

from pathlib import Path
import os
from aquaculture_auth import CredentialStore, AuthUnavailable

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "database" / "configuration.json"
def get_store():
    store = CredentialStore()
    try:
        store.state()
    except AuthUnavailable:
        store.initialize(CONFIG_PATH, email=os.environ.get("AQUACULTURE_ADMIN_EMAIL"))
    return store


def get_admin_password() -> dict[str, object]:
    return {"ok": True, "password_set": bool(get_store().state()["enabled"])}


def verify_admin_password(password: str) -> dict[str, object]:
    revision = get_store().verify(password)
    return {"ok": bool(revision), "credential_revision": revision,
            "message": "Admin verified." if revision else "Unable to verify the Admin password."}


def change_admin_password(
    new_password: str,
    *,
    current_password: str | None = None,
    require_current: bool = False,
) -> dict[str, object]:
    """Change the saved admin password.

    api_manager.py can set require_current=True when the frontend asks for current-password verification.
    """
    if not require_current:
        return {"ok": False, "message": "Current password verification is required."}
    try:
        changed = get_store().change(current_password, new_password)
        return {"ok": changed, "message": "Admin password updated. Sign in again." if changed else "Unable to verify the Admin password."}
    except ValueError as error:
        return {"ok": False, "message": str(error)}
