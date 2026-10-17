# AquaCulture

Raspberry Pi aquaculture monitoring with a Flask/Socket.IO backend, sensor and
camera monitoring, SQLite storage, and Bluetooth Wi-Fi provisioning.

## Project layout

| Path | Purpose |
| --- | --- |
| `client/` | Frontend templates, styles, scripts, and local assets |
| `database/` | Database tools, schemas, migrations, and local runtime data |
| `server/` | Backend, shared Admin authentication (`auth.py`), and security documentation |
| `driver/` | Arduino Nano sketch and wiring references |
| `models/` | Inference models and weights |
| `network/` | BLE/NetworkManager configuration and the standalone setup frontend |
| `startup/` | Startup, deployment, and rollback scripts |
| `.env` | Private machine configuration; excluded from Git |
| `.env.example` | Shareable environment configuration template |
| `prompt.txt` | Project requests and notes; intended for Git |

Git metadata, editor settings, dependency environments, and caches are development
support files rather than application components. Source files, model files, data
assets, and `prompt.txt` are trackable; `.env` remains private. Local environments,
dependencies, and caches stay ignored. Git tracking changes do not commit or push
anything automatically.

## Windows development

Run these commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r server/requirements.txt -r network/rpi/requirements.txt
.\.venv\Scripts\python.exe server/app.py
```

Open `http://127.0.0.1:5000`. In VS Code, select
`.venv\Scripts\python.exe` as the Python interpreter. The network requirements
provide the shared authentication and D-Bus packages needed for development;
the actual BlueZ/NetworkManager service runs on Raspberry Pi OS, not Windows.
For a fresh installation, copy `.env.example` to `.env` and configure it locally.
Preserve an existing `.env` when updating.

## Deployment and security

[startup/README.md](startup/README.md) covers startup scripts.
[server/SECURITY.md](server/SECURITY.md) covers the shared hashed Admin store,
password recovery, session revocation, and local recovery. Both the Flask backend
and BLE service use `server/auth.py`; it belongs to the backend even though BLE
also imports it.

[network/README.md](network/README.md) describes secure pairing, Admin verification,
the offline setup page, and Pi hardware acceptance. The wire format is in
[network/protocol.md](network/protocol.md). Only `network/webbluetooth/` is intended
for static hosting; the main backend remains on the Pi. Working installations
must preserve `/var/lib/aquaculture-ble` and their existing service overrides.

## Runtime notes

Settings → Notifications uses seconds. New configurations default to 86400
(24 hours); existing saved intervals are preserved. Use 172800 for 48 hours or
-1 to disable periodic summaries. Immediate threshold alerts have separate sensor
validity, hold, and cooldown rules. Preserve
`database/notification_schedule.sqlite3`: it holds durable reservations that avoid
duplicate summaries across restarts. A delivery failure after reservation can
skip an interval; the first run starts an interval without a startup email.

The optional legacy network panel uses `server/network/`,
`server/network_app.py`, and `/network*` compatibility routes. It is separate from
BLE setup and is not required for normal deployment.

Do not reseed a production database from the legacy species workbook or run
`database/create_database.py --yes` against real records. Keep runtime databases
and media when updating. Reference-only species do not enter rule-based scoring
without verified tolerance ranges.

NCNN model paths are `models/FishInv_ncnn_model/` and
`models/MegaFauna_ncnn_model/`, each containing `model.ncnn.param` and
`model.ncnn.bin`. Optional laptop weights use `models/model-2.pt` and
`models/model-3.pt`. Model availability does not affect BLE provisioning.
`driver/nano.ino` and `driver/wiring.txt` define the existing hardware interface;
the MQ-137 gas measurement must not be interpreted as a calibrated water-ammonia
measurement without a separate hardware/calibration review.

## Visual scanning update (2026-10-17)

The **Fish Weight Scanning** page uses a four-stage pipeline:

1. **Record:** Capture cached camera video frames for the selected **3, 5, or 10 seconds**. The selection is a recording duration, **not** the overall AI processing deadline. The capture is sampled into memory; this update does not create a permanent MP4 file.
2. **Select:** Evaluate readable frames for image sharpness, brightness and duplicate bytes. Shortlist up to **5 frames (default) or 10 frames**; show thumbnails and animate rejected candidates.
3. **Predict:** Run the existing `models/FishInv_ncnn_model` and `models/MegaFauna_ncnn_model` in isolated processes on the shortlisted frames. Display frame-by-frame progress, annotations, confidence and errors. An AI error is **not** treated as an empty scene. This can take several minutes on Raspberry Pi 4B.
4. **Results:** Prefer the frame with model-confirmed aquatic detections and confidence (quality breaks ties). Highlight the best frame green; show boxes, counts and size estimates if available. A successfully processed empty scene produces **No aquatic object detected**.

Visual jobs start via `POST /api/prediction/start` (`202 Accepted`) and are tracked via authenticated `GET /api/prediction/status`. `POST /api/prediction/cancel` stops recording or terminates an in-flight isolated worker. Do not configure the reverse proxy or frontend to require an HTTP request to stay open for the entire prediction. Only one visual job can run per backend process.

The recording duration and NCNN processing limit are independent. The processing limit defaults to `max(120, 60 × selected_frame_count)` seconds, bounded to 900 seconds. Set `AQUACULTURE_VISUAL_PROCESS_TIMEOUT=600` in your existing `.env` if needed. Each child also respects `AQUACULTURE_ML_TIMEOUT` (default 300 seconds).

**Deployment:** Merge these source files into your *existing* Pi install. Preserve your `.env`, `database/` records, `models/` binaries, `driver/` configuration and connected camera hardware. This upload is intentionally a source-only update, not a full Raspberry Pi installation. After restarting the Flask service, hard-refresh the browser and test both an empty camera scene and a fish-containing scene. Cancel during inference once to verify responsiveness. Real fish length and weight require proper camera geometry and scale calibration; no generic bounding box provides accurate centimeters by itself.

For implementation details and regression checks, see [`SCAN_PIPELINE_UPDATE.md`](SCAN_PIPELINE_UPDATE.md).
