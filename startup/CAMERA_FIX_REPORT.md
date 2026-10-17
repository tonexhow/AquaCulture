# AquaCulture camera repair and system audit

Assessment date: October 17, 2026. Source: the supplied AquaCulture workspace, starting at commit `fffbcd5`, with the existing untracked `startup/` kit preserved. Development validation used Windows x64 and Python 3.13.13. The running Raspberry Pi, its device permissions, and its installed systemd overrides were not accessed.

## Camera root cause

The original `server/live_streaming.py` already tried Picamera2 before OpenCV and validated an initial CSI frame. It did **not** simply hardcode USB index zero. These verified source defects explain unreliable detection and recovery:

| Original function | Verified defect and effect | Correction |
| --- | --- | --- |
| `CameraManager._discover_opencv_indexes` and `_start_opencv` | Tried indexes 0 through 31 plus video nodes through several backends, without checking USB identity or node capture capabilities. Codec, ISP, output, and metadata nodes could consume repeated probe time. Stable device links were discarded. | `server/camera_devices.py` queries each V4L2 node's capabilities, checks USB identity, prefers by-id/by-path links, deduplicates nodes, and opens Linux candidates through V4L2. |
| `_capture_loop` | An initial failure entered a permanent placeholder loop and never attempted initialization again. | The single worker retries initialization with an interruptible delay, normally five seconds. Every recovery round follows the configured source priority. |
| `_capture_loop`, `_save_frame`, `snapshot_jpeg`, `capture_burst` | Placeholder bytes were saved through the real-frame cache, setting `connected=True`. Disconnected or old images could be returned to a scan even when fallback was disabled. | Only validated captures enter the cache. Snapshots and bursts check connectivity and frame age. Bursts retain original capture metadata and skip repeated frame numbers. |
| `_start_picamera2`, `_release_picamera2` | Used the default libcamera camera without filtering USB entries; failure cleanup stopped a locally created camera without closing it. | Enumerates CSI candidates explicitly, captures and JPEG-encodes a frame before adoption, and calls both stop and close on rejected/released handles. |
| `stop`, `restart`, `_read_camera_frame` | Request threads could release handles while the capture worker was reading them. Restart could return the old thread without actually restarting it. | The capture worker owns initialization, reading, switching, and release. Restart queues a replacement only after the old owner exits; HTTP lifecycle calls do not wait for camera initialization. |
| `_encode_image` | Converted Picamera2 RGB888 arrays as if their bytes were RGB, swapping the red and blue channels. | RGB888 arrays already contain OpenCV's BGR byte order; the unnecessary conversion is removed. |
| `client/static/index/index.js` | Loading an MJPEG placeholder could overwrite the dashboard state with “Server camera loaded.” | The badge follows backend source/freshness status, with bounded stream reconnection and status polling only while the camera page is open. |

No target-device evidence establishes a specific permission, cable, firmware, driver, or webcam-format failure. Those possibilities remain separate from the source defects above. Initialization errors now report the selected device and failure reason through camera status and logs.

## Camera flow after repair

```text
CSI sensor -> libcamera -> optional Picamera2 --+
                                               +-> one CameraManager worker
USB webcam -> V4L2 capability check -> OpenCV --+       |
                                                       v
                                                fresh JPEG cache
                                                 /           \
                                       snapshots and MJPEG   capture_burst
                                              |                 |
                                       Flask dashboard      size_detection
```

Automatic selection tries a physically enumerated CSI device first and accepts it only after successful capture and encoding. Failed CSI initialization closes its resources before USB probing. USB candidates must identify as capture-capable USB devices and provide a readable frame. With neither camera available, Flask remains healthy, the camera worker waits before retrying, and the dashboard reports `Camera: Unavailable — Retrying`.

After a runtime failure, the cached image is invalidated and handles are released before another attempt. While USB is working in automatic mode, the worker checks for a newly available CSI device at most once per minute. It validates CSI before retaining it and returns to USB if CSI initialization fails. Explicit CSI/USB modes limit selection to the chosen source.

Existing endpoints and Socket.IO event names remain intact. `GET /api/camera/snapshot?require_frame=1` is an additive verification option: it returns HTTP 503 until a real fresh capture exists. The normal snapshot still permits a placeholder, marked by `X-Camera-Available: 0`; a successful strict snapshot identifies its source with `X-Camera-Source`. A default snapshot/MJPEG HTTP 200 alone does not prove hardware capture.

## Backend integration audit

| Component | Finding and change | Validation and remaining limits |
| --- | --- | --- |
| Flask startup | Importing `server/app.py` previously initialized storage and started workers. Imports now have no such startup work; direct `python server/app.py` builds and starts the application once. The factory supports isolated callers with `start_services=False, prepare_storage=False`. | Factory and startup tests pass. External WSGI launchers must invoke the factory explicitly; the supported systemd command remains direct script execution. |
| Shutdown | Added SIGTERM/SIGINT and exit cleanup for notification workers, summary scheduler, serial reader, camera, and relay. | Idempotent cleanup tested with mocked resources. Driver stalls can outlast bounded cleanup waits; the manager will not create a second capture owner while one is stuck. |
| Serial sensors | A one-shot read could open a second serial connection alongside the background reader. Stop/start also raced; scans and distance could reuse disconnected/stale values. | All reads now use the shared cache, lifecycle changes serialize and join, and scan/distance readings require current data. Mocked Nano/serial tests pass. No sensor or wiring specification changed. |
| Notification delivery | SMTP/SMS delivery ran on the serial ingestion callback and could block new readings. | A single worker retains at most one newest pending sample and rechecks current connection state and original sample age. Slow-delivery and stop/restart tests pass. A network send already in progress cannot be canceled. |
| Camera APIs and events | Preserved camera routes, MJPEG boundary, scanner burst keys, and `camera_status`. Added status callbacks and strict snapshot checking. | Flask/Socket.IO tests cover guarded mutation, private status responses, stream closure, source metadata, and no-camera health. |
| Socket.IO authentication | Wildcard origin acceptance could combine with ambient admin cookies. | Default same-origin enforcement now rejects unrelated browser origins. BLE provisioning uses its independent protocol and does not depend on cross-origin Socket.IO. |
| Admin sessions | Public HTTPS recovery-origin configuration could force a Secure cookie onto the distinct HTTP LAN login. | Cookie policy follows actual HTTPS or the configured public hostname; public HTTPS remains Secure while the separate LAN origin works. Password hashing, revision checks, durable limiters, and recovery behavior retain their contracts. |
| SQLite connections | The migration/index helpers used transaction context managers without explicitly closing their connections. | Helpers now close connections deterministically; temporary database cleanup tests pass on Windows. Existing core DB helpers already close per-operation connections. |
| Catalog startup | First-run curation could archive user records by numeric ID and ignore an archive-ID conflict before deleting an active row. | Normal startup is additive. Legacy archiving requires explicit opt-in; a conflicting archive insert rolls back instead of losing the active row. Production DB rows were not changed during this task. |
| Media compatibility | The existing legacy prediction migration can rebuild a fixed-column table when its BLOB is NOT NULL. Custom schema could be lost in that legacy path. | Rebuild logic is not rewritten here. Deployment preflight refuses that legacy schema before changing files or services; it requires a separately reviewed migration. Already migrated databases keep their schema. |
| BLE authentication and Wi-Fi | Shared credential revisions, BLE session revocation, bounded durable limiter state, NetworkManager workflow, and `AquaCulture-Pi` advertising remain connected in the source. | BLE/NetworkManager behavior is tested through mocks, not live Bluetooth or D-Bus. No `network/rpi/` implementation or working service/state directory is in the overwrite payload. |
| Systemd installation | The project startup kit and dedicated BLE installer generate different deployments. Rerunning either can replace a working BLE unit or lose the writable-state fix. | The new updater uses the existing mike web service and never runs either installer, rewrites a unit/drop-in, or restarts BLE. It checks service identity and preserves `/var/lib/aquaculture-ble`. |
| Dependencies | Trixie provides Python 3.13 ARM OpenCV bindings. Picamera2/libcamera must match the Pi OS system Python and native stack; pip packages may shadow apt packages. | Existing dependencies are reused. USB discovery uses Python's Linux `fcntl`; Picamera2 remains optional. No dependency installation is part of the update. See deployment guidance for conditional CSI setup. |

## Remaining risks and operational boundaries

- Physical CSI/USB capture, hotplug behavior, Raspberry Pi permissions, actual SMTP/SMS delivery, and reboot startup still need the Pi validation commands in the deployment guide. Previously reported active/enabled services are user-provided deployment context, not remotely reverified results.
- NCNN/PyTorch inference, calibration, and performance on the Pi were not executed. Model files remain untouched; the scanner retains its existing burst interface and now receives only current captured frames.
- Native camera drivers may block in operations beyond the bounded Picamera2 job wait or OpenCV V4L2 select timeout. The worker isolates those calls from requests; it does not promise that every kernel/native operation can be canceled. Use one backend process per camera; separate external capture programs or multiple server processes can still compete for hardware.
- Existing Admin session JSON persistence has no cross-process lock, and SQLite write contention remains workload-dependent. This patch does not introduce a multi-process server or change journal mode. These concerns were not reproduced as production failures here.
- The optional legacy network panel lacks the standalone BLE service's protections and trusts forwarded client headers. Keep `AQUACULTURE_NETWORK_PANEL=0`; do not expose port 5001 as part of this deployment. The working BLE provisioning path is unchanged.
- The dedicated BLE installer uses a different account, `/opt` layout, Polkit policy, and credential migration from the supplied mike deployment. It also does not import the configured recovery email automatically. Treat it as a separate installation workflow, not an update command.

## Validation before repository cleanup

Before removing the requested `server/tests/` files, validation copied source/media and took a read-only SQLite backup into a temporary project. It used an empty `.env` fixture and synthetic authentication stores; the live `.env`, database, limiter state, Wi-Fi, and services were not test targets. The same checks were rerun after relocating the shared authentication module to `server/auth.py`. The removed server tests and their staging runner are no longer part of the repository or update package.

| Suite | Result |
| --- | --- |
| Camera lifecycle, capability discovery, and JPEG colors | 29 passing checks, mocked native cameras |
| New Flask/Socket.IO camera integration | 12 passing checks, mocked captures, including placeholder provenance races |
| New backend lifecycle, queue, and additive catalog | 15 passing checks, temporary state |
| Complete server regression suite | 96 passing checks |
| Existing BLE provisioning regression suite | 24 passing checks with mocked D-Bus/NetworkManager, including D-Bus signature compatibility; no live adapter exercised |
| Existing browser BLE/offline protocol tests | 6 passing Node.js tests |
| Deployment helpers | Bash syntax, embedded Python syntax, and synthetic package checks; real systemd deployment remains Pi-only |

Initial full-run failures exposed old “models absent/BLE deferred” tests and SQLite handle cleanup issues; those were corrected. `dbus-next==0.2.3` was installed into a temporary development-only dependency directory for the BLE suite. Native Raspberry Pi hardware verification is not included in the automated pass count.

## Package and deployment

`AquaCulture_Camera_Fix_Overwrite.zip` contains only the changed/new files enumerated by `startup/camera-fix-manifest.json`. It has no outer project directory, so its paths match `/home/mike/AquaCulture`. It excludes real `.env`, live databases, configuration/session files, model files, caches, logs, and unchanged source. `.env.example` is a blank configuration template, not the live environment.

Use [CAMERA_FIX_DEPLOYMENT.md](CAMERA_FIX_DEPLOYMENT.md) to validate, back up, apply, verify, and roll back the package. The updater backs up only affected files and restarts only `aquaculture-web.service`; it leaves BLE state, credentials, configuration, and service overrides intact.

## Primary interface references

- [Raspberry Pi Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf): camera enumeration, capture jobs, RGB888 byte order, cleanup, and native installation.
- [Linux V4L2 device naming](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/open.html) and [capability query](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-querycap.html): persistent device paths and node-specific capture capabilities.
- [OpenCV environment reference](https://docs.opencv.org/4.x/d6/dea/tutorial_env_reference.html): V4L2 select timeout in seconds. This bounds select waits, not every native operation.
- [Debian Trixie python3-opencv](https://packages.debian.org/trixie/python3-opencv): Python 3.13 and ARM package availability; this alone does not establish CSI support on non-Pi Debian installations.
