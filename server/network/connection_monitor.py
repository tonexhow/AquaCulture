"""Read-only health of local network access (safe for Windows and Raspberry Pi)."""

from __future__ import annotations
import socket


def get_local_addresses() -> list[str]:
    """Addresses are hints, not proof that a phone can reach the server."""
    try:
        host = socket.gethostname()
        results = socket.getaddrinfo(host, None, family=socket.AF_INET)
        return sorted({entry[4][0] for entry in results if not entry[4][0].startswith("127.")})
    except OSError:
        return []


def get_connection_status() -> dict:
    addresses = get_local_addresses()
    return {"connected_to_lan": bool(addresses), "addresses": addresses,
            "hint": "Bluetooth provisioning is not available in this build."}
