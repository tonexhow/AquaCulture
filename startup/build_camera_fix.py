"""Build the exact overwrite payload without credentials, databases, models or caches."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile

PAYLOAD = (
    ".env.example", "client/static/index/index.js", "database/catalog_migration.py",
    "database/create_database.py", "database/maintenance.py", "database/media_storage.py",
    "server/__init__.py", "server/auth.py", "server/api_manager.py", "server/app.py",
    "server/camera_devices.py", "server/live_streaming.py",
    "server/scanner/fish_recommendation.py", "server/scanner/size_detection.py", "server/sensors.py",
    "server/settings/admin_password.py", "server/settings/database_tables.py",
    "server/settings/password_recovery.py", "server/settings/species.py",
    "server/settings/notification/reading_worker.py", "server/settings/notification/summary_scheduler.py",
    "startup/deploy-camera-fix.sh", "startup/rollback-camera-fix.sh",
    "startup/CAMERA_FIX_REPORT.md", "startup/CAMERA_FIX_DEPLOYMENT.md",
    "startup/build_camera_fix.py",
)
MANIFEST = "startup/camera-fix-manifest.json"


def build(root: Path, output: Path) -> None:
    contents = {path: (root / path).read_bytes() for path in PAYLOAD}
    manifest = {"format": 1,
                "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                          for path, data in sorted(contents.items())]}
    encoded = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (root / MANIFEST).write_bytes(encoded)
    contents[MANIFEST] = encoded
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, data in sorted(contents.items()):
            info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if path.endswith(".sh") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            archive.writestr(info, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(contents):
            raise RuntimeError("Archive integrity or member verification failed.")
        for path, data in contents.items():
            if archive.read(path) != data:
                raise RuntimeError(f"Archive content mismatch: {path}")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"Built {output.name}: {len(PAYLOAD)} changed/new files + manifest, {output.stat().st_size:,} bytes")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project = args.root.resolve()
    build(project, (args.output or project / "startup/AquaCulture_Camera_Fix_Overwrite.zip").resolve())
