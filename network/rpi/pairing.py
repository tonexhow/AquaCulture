"""Physical-console pairing gate; passkeys are never sent over BLE or logged."""
import json
import os
from pathlib import Path
import time


class PairingWindow:
    def __init__(self, directory="/run/aquaculture-ble"):
        self.directory = Path(directory)
        self.peer = None
        self.window_id = None
        self.deadline = 0

    def permitted(self, peer):
        try:
            data = json.loads((self.directory / "pairing-window.json").read_text())
            if data["opened_at"] != self.window_id:
                self.window_id = data["opened_at"]
                self.deadline = time.monotonic() + max(0, min(120, data["expires_at"] - time.time()))
                self.peer = None
            if time.monotonic() >= self.deadline:
                return False
            if self.peer is None:
                self.peer = peer
            return self.peer == peer
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def display(self, peer, passkey):
        if not self.permitted(peer):
            raise ValueError("Open a physical pairing window at the Pi console.")
        target = self.directory / "pairing-code"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{passkey:06d}\n")

    def clear(self):
        (self.directory / "pairing-code").unlink(missing_ok=True)

    @staticmethod
    def open_local(directory="/run/aquaculture-ble"):
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise PermissionError("Use sudo from a trusted local console.")
        directory = Path(directory)
        owner = directory.stat()
        target = directory / "pairing-window.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"opened_at": time.time(), "expires_at": time.time() + 120}, handle)
        os.chown(target, owner.st_uid, owner.st_gid)
        (directory / "pairing-code").unlink(missing_ok=True)
