# AquaCulture camera deployment on Raspberry Pi

Apply this update to the existing `/home/mike/AquaCulture` installation on Raspberry Pi OS Trixie. The application continues to run as `mike` using `/home/mike/AquaCulture/.venv/bin/python`. The updater preserves `.env`, live SQLite databases, `/var/lib/aquaculture-ble`, shared credentials, models, and systemd units/drop-ins, and operates only on `aquaculture-web.service`.

## Apply the verified package

Build the current package with `python startup/build_camera_fix.py`. It is written inside `startup/` to keep the project root free of generated archives. Authentication now lives in `server/auth.py`; the package includes that module and its backend callers. It does not ship server test files or change the running BLE service.

Copy `AquaCulture_Camera_Fix_Overwrite.zip` to `/home/mike/` on the Pi. Do not extract the whole package into the live project before taking the backup. Run the following as `mike` in a Pi Bash terminal; the bootstrap writes only a temporary helper, then the helper validates the package and backs up targets before replacing them.

```bash
set -euo pipefail
cd /home/mike/AquaCulture
CAMERA_FIX_ZIP=/home/mike/AquaCulture_Camera_Fix_Overwrite.zip
CAMERA_FIX_BOOTSTRAP=$(mktemp -d /tmp/aquaculture-camera-bootstrap.XXXXXX)
/usr/bin/python3 - "$CAMERA_FIX_ZIP" "$CAMERA_FIX_BOOTSTRAP" <<'PY'
from pathlib import Path
import sys
import zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    for name in ('deploy-camera-fix.sh', 'rollback-camera-fix.sh'):
        member = 'startup/' + name
        if archive.namelist().count(member) != 1:
            raise SystemExit('Missing or duplicate deployment helper')
        (Path(sys.argv[2]) / name).write_bytes(archive.read(member))
PY
bash "$CAMERA_FIX_BOOTSTRAP/deploy-camera-fix.sh" --check "$CAMERA_FIX_ZIP"
sudo bash "$CAMERA_FIX_BOOTSTRAP/deploy-camera-fix.sh" "$CAMERA_FIX_ZIP"
```

The bootstrap executes code from the supplied package, so use the original package delivered with this report. The updater checks the exact manifest paths and SHA256 hashes; hashes inside a ZIP detect corruption and mismatched files, not authenticity of an untrusted replacement archive.

The helper validates the existing web unit's account, working directory, and executable; verifies BLE is active/enabled; takes private backups under `/var/backups/aquaculture-camera-fix/`; stops only web; installs only manifest files; starts web; waits for health/camera endpoints; and shows camera logs. It checks that BLE's invocation did not change. New source timestamps invalidate stale bytecode without deleting caches. It installs no packages and never reboots.

No camera connected is an acceptable deployment state: the web service must stay operational and the camera must report its retry state. A real capture is verified separately below. Preflight refuses an old `prediction.data NOT NULL` media schema because the existing legacy migration could rebuild it; do not bypass that guard or reset the database. A separately reviewed migration is needed for that older schema.

Do not run `startup/install.sh` or `network/rpi/install.sh` for this update. They can replace the working BLE unit and its state-directory configuration.

## Roll back affected files

Deployment failure triggers a web-only automatic rollback when possible. The helper prints the retained backup path and a rollback command using private helper copies inside that backup. For a later manual rollback, use that exact printed command, for example:

```bash
sudo bash /var/backups/aquaculture-camera-fix/BACKUP_NAME/tools/rollback-camera-fix.sh \
  /var/backups/aquaculture-camera-fix/BACKUP_NAME
```

Replace `BACKUP_NAME` with the actual printed directory. Rollback verifies backup hashes, restores prior file ownership/modes/timestamps, removes only files introduced by this package, and restores the previous web active/inactive state. It refuses to overwrite subsequent edits to a target. It does not restore or modify databases, credentials, `.env`, BLE state, dependencies, or service overrides because the updater never replaced those resources. Backups remain available after rollback.

## Camera configuration

Existing installations work with the default automatic selection. Edit the existing private `.env` only if a different setting is needed; do not copy `.env.example` over it.

```dotenv
AQUACULTURE_CAMERA_SOURCE=auto
AQUACULTURE_CAMERA_USB_DEVICE=
AQUACULTURE_CAMERA_WIDTH=1280
AQUACULTURE_CAMERA_HEIGHT=720
AQUACULTURE_CAMERA_FPS=15
AQUACULTURE_CAMERA_RETRY_SECONDS=5
AQUACULTURE_CAMERA_FRAME_TIMEOUT=4
OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT=3
```

`auto` selects verified CSI, then verified USB. `csi` and `usb` force a source; forced CSI intentionally does not fall back to USB. The optional USB path should use a link such as `/dev/v4l/by-id/usb-...-video-index0`. The existing `AQUACULTURE_CAMERA_INDEX` remains a legacy preference, but Linux candidates still must pass USB/capture validation. Retry delay has a two-second minimum. A working USB camera is checked for CSI promotion at most once per minute.

`AQUACULTURE_CAMERA_AUTOSTART=0` preserves lazy capture: a snapshot/stream request starts the camera worker. Set it to `1` only if capture should start with web. A camera stopped through the Admin controls stays stopped until explicitly started or used by an Admin scan. Width, height, and FPS are requests to the device; a driver may negotiate different values. The timeout settings do not guarantee interruption of every native driver call.

After an intentional `.env` edit:

```bash
sudo systemctl restart aquaculture-web.service
```

## Dependency and permission checks

The patch adds no mandatory Python package. Check the current venv without starting hardware:

```bash
/home/mike/AquaCulture/.venv/bin/python -c 'import sys, cv2; print(sys.version); print("OpenCV", cv2.__version__)'
/home/mike/AquaCulture/.venv/bin/python -c 'from picamera2 import Picamera2; print(Picamera2.global_camera_info())'
id mike
ls -l /dev/video* /dev/v4l/by-id/ /dev/v4l/by-path/ 2>/dev/null || true
```

Missing Picamera2 does not prevent USB operation. If CSI is required and the matching Pi OS bindings are missing, inspect the installed package versions first:

```bash
apt-cache policy python3-picamera2 python3-libcamera python3-opencv python3-numpy
grep '^include-system-site-packages' /home/mike/AquaCulture/.venv/pyvenv.cfg
```

On Raspberry Pi OS, use compatible apt bindings and a venv created with system-site-packages. Only if these packages are missing, install them through the OS package manager:

```bash
sudo apt update
sudo apt install --no-install-recommends python3-picamera2 python3-opencv
```

Do not recreate the working `.venv`, upgrade NumPy/OpenCV blindly, or append a different Python version's native package path to repair an ABI mismatch. A private venv may need deliberate enablement of matching system packages; preserve and review its existing configuration first. Apt-installed Picamera2 support is for the supported Pi OS/libcamera stack, not a promise that every plain Debian image has a CSI pipeline. [Pi installation guidance](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf), [Trixie OpenCV bindings](https://packages.debian.org/trixie/python3-opencv).

If logs show permission denial, compare `mike`'s groups with the actual camera device group. Add only the required device group when necessary, then restart web; never use world-writable camera nodes or run the application as root. Keep the existing BLE state directory and service drop-ins intact. Diagnostic `v4l2-ctl` and `rpicam-*` tools are optional and are not required by application discovery.

## Verify service identity and automatic startup

```bash
systemctl show aquaculture-web.service \
  -p User -p Group -p WorkingDirectory -p ExecStart -p Restart -p RestartUSec -p After -p Wants
systemctl show aquaculture-ble.service \
  -p User -p Group -p WorkingDirectory -p Restart -p StateDirectory -p RuntimeDirectory
systemctl is-active aquaculture-web.service aquaculture-ble.service
systemctl is-enabled aquaculture-web.service aquaculture-ble.service
sudo systemctl status aquaculture-web.service aquaculture-ble.service --no-pager --lines=8
sudo journalctl -u aquaculture-web.service --since '-10 minutes' --no-pager \
  | grep -Ei 'aquaculture.camera|Camera:|CSI|USB|V4L2|Picamera2'
```

Web should use `mike`, `/home/mike/AquaCulture`, and its `.venv/bin/python`; both existing services should remain active and enabled. The BLE unit may use `StateDirectory=aquaculture-ble` or an existing equivalent writable-directory override. Preserve whichever working configuration is installed. Inspect unit definitions locally only if needed; do not paste unit environment values or `.env` into shared logs. Enabled status verifies configuration, not a real reboot test.

## Verify detection and actual dashboard capture

Enumeration alone does not prove frame capture:

```bash
if command -v rpicam-hello >/dev/null; then rpicam-hello --list-cameras; fi
if command -v v4l2-ctl >/dev/null; then v4l2-ctl --list-devices; fi
ls -l /dev/v4l/by-id/ /dev/v4l/by-path/ 2>/dev/null || true
curl --fail --silent http://127.0.0.1:5000/api/camera/status | /usr/bin/python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in ("source","device","running","connected","fresh","frame_number","last_error")},indent=2))'
```

Request a strict snapshot and retry briefly while the background worker initializes. A successful JPEG here cannot be the application's placeholder:

```bash
CAMERA_FRAME_DIR=$(mktemp -d /tmp/aquaculture-camera-frame.XXXXXX)
for attempt in $(seq 1 20); do
  if curl --fail --silent --show-error \
    -D "$CAMERA_FRAME_DIR/headers.txt" \
    'http://127.0.0.1:5000/api/camera/snapshot?require_frame=1' \
    -o "$CAMERA_FRAME_DIR/frame.jpg"; then break; fi
  sleep 2
done
grep -Ei '^HTTP/|^X-Camera-' "$CAMERA_FRAME_DIR/headers.txt"
/home/mike/AquaCulture/.venv/bin/python - "$CAMERA_FRAME_DIR/frame.jpg" <<'PY'
import cv2
import sys
image = cv2.imread(sys.argv[1])
if image is None or image.size == 0:
    raise SystemExit('No decodable live camera JPEG; inspect camera status and logs')
print('Live JPEG decoded:', image.shape)
PY
curl --fail --silent http://127.0.0.1:5000/api/camera/status | /usr/bin/python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in ("source","device","running","connected","fresh","frame_number","last_error")},indent=2))'
```

Expect `source: csi` with working CSI, or `source: usb` and its actual stable path with USB fallback, plus `connected: true`, `fresh: true`, and increasing `frame_number`. With no hardware, expect a healthy web service, strict snapshot HTTP 503, and retry status with useful `last_error`.

Open `http://192.168.1.60:5000/fish-scanning` and check the preview and source badge. `/api/camera/stream` is the existing MJPEG feed. Unplug the selected USB camera, confirm it becomes unavailable without crashing web, then reconnect and wait for the next retry. If CSI is present, confirm it has priority. Observe the image itself; byte delivery and software tests cannot confirm focus, exposure, framing, calibration, or model accuracy.

## Optional direct hardware diagnosis

Stop web before opening either camera from another program. This Bash subshell restarts web when the diagnostic ends and never touches BLE:

```bash
(
  set -euo pipefail
  sudo systemctl stop aquaculture-web.service
  trap 'sudo systemctl start aquaculture-web.service' EXIT
  cd /home/mike/AquaCulture
  .venv/bin/python - <<'PY'
import sys
import time
from dotenv import load_dotenv
load_dotenv('/home/mike/AquaCulture/.env', override=False)
sys.path.insert(0, '/home/mike/AquaCulture/server')
from live_streaming import CameraManager
manager = CameraManager()
try:
    manager.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if manager.snapshot_jpeg(allow_fallback=False):
            status = manager.get_status()
            print({key: status[key] for key in ('source', 'device', 'connected', 'fresh', 'frame_number')})
            break
        time.sleep(0.25)
    else:
        status = manager.get_status()
        print({key: status.get(key) for key in ('source', 'device', 'connected', 'fresh', 'last_error')})
        raise SystemExit('No real capture; inspect device permissions and camera initialization errors')
finally:
    manager.shutdown()
PY
)
```

For a source-specific test, set `AQUACULTURE_CAMERA_SOURCE=csi` or `usb` for that diagnostic command. Use a USB path discovered above rather than assuming `/dev/video0`. If available, `rpicam-still --camera N --nopreview --timeout 1500 --output /tmp/csi-check.jpg` can separately validate a CSI camera, with `N` taken from its camera list and web stopped. Do not run `rpicam-still` or a separate OpenCV capture concurrently with the application.

## Check the installed source

```bash
cd /home/mike/AquaCulture
.venv/bin/python -m compileall -q server database network/rpi
.venv/bin/python -B -c 'from server.auth import CredentialStore, DurableLimiter; print("Authentication imports OK")'
```

The previous server regression files and their staging runner were removed during repository cleanup. The camera/backend regressions passed before removal; the package contains runtime code and deployment helpers. Current BLE/browser checks are documented in `network/TESTING.md` and require its existing Python dependencies and Node.js. Hardware capture, NetworkManager permissions, BLE pairing, and reboot behavior still need their real Pi checks. Reboot only at a time you choose.
