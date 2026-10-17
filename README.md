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
