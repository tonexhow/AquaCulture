# AquaCulture: auto-start at boot

**Placement:** Copy this entire `startup/` folder into your existing **AquaCulture/** project folder, at the same level as `.env`, `server/`, `network/`, `models/`, `database/` and `driver/`.

```text
AquaCulture/
  .env
  client/
  database/
  driver/
  models/
  network/
    rpi/
      main.py
  server/
    app.py
    requirements.txt
  startup/
    install.sh
    uninstall.sh
    restart.sh
    status.sh
    logs.sh
    README.md
```

## Services

- `aquaculture-web.service` runs `server/app.py` (Flask backend).
- `aquaculture-ble.service` runs `network/rpi/main.py` (independent Bluetooth setup service).

Both are `systemd` services enabled at boot, with `Restart=on-failure` and a 10-second retry interval. Neither waits for internet connectivity. The installer does NOT create additional camera, sensor, or inference processes because those might already run under the backend.

**Important:** The entry-point filenames are based on the AquaCulture project structure previously provided. The installer checks both paths. Actual application behavior, dependencies and BLE hardware permissions are not yet verified on your target device.

## Prerequisites (on the Linux device, not Windows PowerShell)

Use a systemd-based Linux operating system. The project's Python packages must already be installed. The scripts **do not** change or install packages, models, credentials, or a database. Make sure `.env` exists with your required settings and is private:

```bash
cd ~/AquaCulture
chmod 600 .env
```

The installer chooses `AquaCulture/.venv/bin/python` when present, otherwise `AquaCulture/venv/bin/python`, otherwise `python3`. On modern Raspberry Pi OS, using a virtual environment may be necessary due to system Python's externally managed package policy. Example **only if you have not already installed project dependencies**:

```bash
cd ~/AquaCulture
sudo apt update
sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
# If the Bluetooth requirements file exists:
.venv/bin/python -m pip install -r network/rpi/requirements.txt
```

Native libraries (NCNN, OpenCV, libcamera, BlueZ D-Bus, GPIO, serial) might require additional system packages or a Python environment compatible with those bindings. Do not assume a generic `pip install` is sufficient.

The systemd services run under the project owner's **non-root Linux account**, not root. That account must have appropriate access to the application directory, serial port, Bluetooth services and required devices. NetworkManager Wi-Fi changes through Bluetooth provisioning may require separate authorization or a narrowly scoped privileged helper. This startup ZIP deliberately does not bypass device permissions or create permissive D-Bus rules. If BLE is not ready, use `--web-only`.

## Install once

On your Linux machine, from the root AquaCulture directory:

```bash
cd ~/AquaCulture
bash startup/install.sh --dry-run
sudo bash startup/install.sh
```

You can run it again safely to update the managed units. It automatically discovers the project's actual Linux directory and user (not hardcoded to `/home/pi`). Use `--user` if copied under another account:

```bash
sudo bash startup/install.sh --user your_linux_username
```

Choose a Python executable explicitly, if necessary:

```bash
sudo bash startup/install.sh --python /usr/bin/python3
# OR
sudo bash startup/install.sh --python /home/your_linux_username/AquaCulture/.venv/bin/python
```

Optional partial setups:

```bash
sudo bash startup/install.sh --web-only
sudo bash startup/install.sh --ble-only
```

**Collision safety:** If `/etc/systemd/system/aquaculture-web.service` or `/etc/systemd/system/aquaculture-ble.service` already exists and was NOT created by this kit, the installer refuses to overwrite it. Review existing services before installing; there may already be a BLE unit in `network/rpi/aquaculture-ble.service`, and duplicate advertising services can conflict.

## Manage after installation

```bash
bash startup/status.sh
sudo bash startup/restart.sh
bash startup/logs.sh
sudo bash startup/uninstall.sh
```

Individual service commands:

```bash
sudo systemctl status aquaculture-web.service
sudo systemctl status aquaculture-ble.service
sudo systemctl restart aquaculture-web.service
sudo systemctl restart aquaculture-ble.service
systemctl is-enabled aquaculture-web.service
systemctl is-enabled aquaculture-ble.service
journalctl -u aquaculture-web.service -n 50 --no-pager
journalctl -u aquaculture-ble.service -n 50 --no-pager
```

To test on-boot behavior after a successful installation, reboot at a convenient time and check `bash startup/status.sh`.

## Troubleshooting

- **Service starts and immediately fails:** `sudo journalctl -u aquaculture-web.service -n 100 --no-pager` or `sudo journalctl -u aquaculture-ble.service -n 100 --no-pager`. Check missing imports, native libraries, and device permissions.
- **BLE works only under sudo:** do not automatically run every component as root; establish appropriate D-Bus/NetworkManager/device authorization for the BLE user or a specific privileged helper.
- **Web UI inaccessible remotely:** check the Flask bind host/firewall and app logs. Autostart itself does not change the network bind address.
- **Moved the project folder or switched Python environments:** rerun the installer to regenerate `ExecStart` and `WorkingDirectory`.
- **No internet connection:** services do not wait for online networking; application-specific external APIs may still be unavailable.
- **Existing unmanaged service detected:** inspect `/etc/systemd/system/aquaculture-ble.service` or `aquaculture-web.service`. Do not blindly delete an existing working service.

## Safety and limitations

- **No Python code is changed** and no service is started until you actually run `sudo bash startup/install.sh` on the target Linux system.
- Service working directory is the AquaCulture project root. Python must handle loading root `.env` itself; the installer does not pass `.env` as `EnvironmentFile` (the parsing formats can differ).
- No extra development/production server is installed; if `server/app.py` uses Flask's development server, systemd does not automatically turn it into a production-grade HTTP server.
- `uninstall.sh` only disables and removes the **managed systemd units**. It deliberately leaves the project files, SQLite database, models, credentials and virtual environments untouched.
- Raspberry Pi 4B hardware boot, auto-restart behavior under failure, Bluetooth authorization, and app startup **have not been tested here**. Only syntax and no-change dry-run validation can be performed without that device/source tree.
