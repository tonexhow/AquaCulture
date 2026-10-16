# AquaCulture standalone network setup — 1.1.0 / protocol 2

The existing BLE/NetworkManager architecture is retained. This update replaces the
separate setup-key authorization with the current AquaCulture Admin credential,
adds password recovery in the main app, and verifies the static app's offline cache.
See [protocol.md](protocol.md), [../SECURITY.md](../SECURITY.md) and [TESTING.md](TESTING.md).

IMPLEMENTED and mock/browser tested. Physical Pi/Android pairing, Wi-Fi provisioning
and the complete headless recovery scenario remain PENDING HARDWARE VALIDATION.

## Install/upgrade on Raspberry Pi

Target Raspberry Pi OS Bookworm or later, Python 3.11–3.14, BlueZ >=5.66,
NetworkManager >=1.42, systemd and Polkit. dbus-next 0.2.3 is not ready for Python
3.15 (deprecated typing API). No other network manager or hotspot is installed.
First inspect the Pi itself:
```bash
cat /etc/os-release
python3 --version
bluetoothctl --version
nmcli --version
systemctl is-active NetworkManager bluetooth
nmcli -t -f DEVICE,TYPE,STATE device status
rfkill list
```

Use a maintenance window for the one-time credential migration: stop the main
AquaCulture backend using its existing launch method so settings are not being
edited concurrently. Then, from the existing checkout:
```bash
sudo bash network/rpi/install.sh
sudo systemctl status aquaculture-ble.service --no-pager
sudo journalctl -u aquaculture-ble.service -n 50 --no-pager
```

If the backend runs under a different OS user from the invoking sudo user:
```bash
sudo AQUACULTURE_APP_USER=your-backend-user bash network/rpi/install.sh
```

The installer verifies compatibility, installs BlueZ/Polkit/venv support, copies the
Pi code and shared credential module to /opt/aquaculture/network, installs
dbus-next/Werkzeug in a separate venv, and installs systemd/Polkit rules. It imports
the existing Admin password once into a scrypt-hashed store and removes only the
legacy JSON password field, preserving other configuration. An existing hashed
store is imported with SQLite backup rather than resetting the account.
No password, SMTP secret, reset link or hash is printed.

The authoritative Linux store is /var/lib/aquaculture-auth/admin.sqlite3. Its
directory is owned by the backend user with setgid aquaculture-auth; the file is
0640. The BLE user has read-only access through that supplementary group and never
writes Admin credentials. The backend discovers this installed location without
changing .env; /etc/aquaculture-auth/installed prevents fallback to an old verifier
if the shared database becomes unavailable. If you already explicitly configured
AQUACULTURE_AUTH_DB, set both processes to the same absolute path and adapt the
unit's filesystem access. Restart the main backend after initial installation.
Normal password changes/resets then take effect in BLE without a service/Pi restart.

The unit starts without network-online.target or Flask. It restarts on errors,
re-registers after BlueZ resets, has a non-login user and restricted filesystem/
capability access. Wi-Fi uses narrowly granted NetworkManager permissions (scan,
network-control, system profile modification). No shell commands are accepted.
The service still operates with Wi-Fi down; unavailable Admin data denies
verification while public discovery remains available.

## Pair a phone securely before relying on headless recovery

Protocol 1 setup.key is obsolete and is not read; no permanent BLE password exists.
Retire that old file through your normal secret-handling process after validation.
The Admin password is not used to derive a custom encryption key.

On a trusted local Pi console, ensure Bluetooth is powered/unblocked:
```bash
sudo rfkill unblock bluetooth
sudo bluetoothctl power on
sudo /opt/aquaculture/network/.venv/bin/python /opt/aquaculture/network/rpi/main.py --open-pairing-window
```

The window lasts 120 seconds. Open the Web Bluetooth page, choose the known
AquaCulture-Pi and start connecting. At Android's OS pairing prompt, privately read
the passkey on the Pi console:
```bash
sudo cat /run/aquaculture-ble/pairing-code
```

Enter that BlueZ-generated passkey on the phone. It is ephemeral, never sent through
BLE or logged. The agent rejects Just Works and automatic numeric confirmation.
The command and response characteristics require authenticated encrypted BLE
permissions. The browser performs a protected read BEFORE enabling Admin entry.
If this fails, no Admin password is submitted. There is no insecure fallback.
An old Just Works bond may need to be forgotten on both devices and paired again.

Keep the OS bond. An already paired phone can reconnect later with no phone/Pi
internet, no dashboard, no monitor and no hotspot. First-time pairing of a new phone
still requires the trusted physical/local procedure. Web Bluetooth does not expose
link security flags directly; enforcement relies on the genuine Pi's BlueZ service
and its physical pairing. Select only that known device. See protocol.md for the
security assumptions and pending hardware validation.

## Admin access and limits

After the protected Bluetooth connection, enter the current AquaCulture Admin
password. Local Werkzeug verification uses the same authoritative scrypt hash as
the web app. No remote API, SMTP, internet or Flask process is involved.

Until verified, the page hides privileged UI and the Pi rejects info/status/history/
scan/configure. Only basic device name, protocol and auth requirement are public.
Private response queues are read by peer rather than broadcast, so another paired
but unverified client cannot see saved profiles/history.

Authorization lasts ten minutes and ends on disconnect, restart, credential
revision change or account disable. Password changes through Settings, SMTP reset
or local recovery all change that revision. Private reads and commands recheck it;
the idle page detects revocation on its next poll. Password fields are cleared;
no Admin/Wi-Fi password or bearer credential is saved in browser storage/cache.

All processed verifications count toward 100 per rolling 24 hours globally on the
Pi. Counters survive reconnects/restarts. Five failed/in-flight requests per rolling
15 minutes, progressive delays, and a supplementary 20/hour per-peer limit apply.
Changing MAC/client IDs does not evade the global ceiling. Safe retry-after
information is displayed, without revealing account existence.

Physical lockout recovery (no BLE endpoint can do this):
```bash
sudo systemctl stop aquaculture-ble.service
sudo /opt/aquaculture/network/.venv/bin/python /opt/aquaculture/network/rpi/main.py --reset-auth-limits
sudo systemctl start aquaculture-ble.service
```

Correct a bad Pi clock too; durable windows deliberately fail conservatively on
backward time changes. Lost-password recovery from a trusted root console:
```bash
sudo /opt/aquaculture/network/.venv/bin/python /opt/aquaculture/network/aquaculture_auth.py --database /var/lib/aquaculture-auth/admin.sqlite3 --set-password
```

This prompts privately, never takes the password in argv, and revokes sessions by
revision change. Backend password/email/origin configuration is in ../SECURITY.md.

## Existing provisioning workflow

After verification, read device/network information, scan, select a network or type
a hidden SSID, enter the Wi-Fi password and connect. WPA/WPA2 and WPA3 personal are
supported where hardware/NM support them. Enterprise/WEP/OWE need another flow;
captive portals may need sign-in after local association.

Progress, actual error categories, SSID/IP/signal and recent bounded history are
preserved. Internet reporting uses NM's per-Wi-Fi connectivity checks; disabled
checks yield unknown. Existing profiles are retained and rollback is attempted on
failure. Reconnect and inspect status after a dropped connection before retrying.
The Pi's accepted Wi-Fi operation can finish even if the phone disconnects.

History: /var/lib/aquaculture-ble/history.sqlite3, bounded to 100 observations.
Limits: /var/lib/aquaculture-ble/auth_limits.sqlite3. Do not remove runtime limit
state during routine upgrades. Use the explicit local recovery command instead.
For a chosen adapter, systemctl edit aquaculture-ble.service:
```ini
[Service]
Environment=AQUACULTURE_WIFI_INTERFACE=wlan0
```

Then daemon-reload and restart the service. It deliberately does not load SMTP or
other secrets from the main app's .env.

## Offline readiness and Vercel

Application: AquaCulture Network Setup. Stable cache ID:
aquaculture-network-setup. Current name: aquaculture-network-setup-v1.1.0.
version.js is the shared version source; bump it whenever frontend shell assets
change. The classic service-worker format permits upgrading existing installations.

Readiness states: NOT_SUPPORTED, NOT_INSTALLED, CACHING, READY, UPDATE_AVAILABLE,
ERROR. Registration alone is not readiness. The foreground checks an active
controller and asks that worker to verify every required file in its exact version's
cache. Timestamp comes from the last completed cache preparation; unknown stays
unknown. Check offline files detects missing entries. Prepare offline access repairs
them while online. A waiting version is offered explicitly; disconnect BLE before
installing it. Old app cache versions are removed; unrelated site caches are retained.
An old cache will not be filled with another deployed version's files.

Only the static shell (HTML/CSS/JS/icons/manifest) is cached. Requests with token
query strings, non-GETs, device responses and credentials are excluded.
Open online once, prepare until Ready, and optionally use Android Chrome's Add to
Home screen / Install app menu. Later both phone and Pi can be offline while BLE
authentication/configuration operate locally. Keep the page foreground; Android
may suspend BLE when backgrounded/locked. Browser storage clearing or worker removal
can require a new online load. Safari/iPhone and Firefox without Web Bluetooth
receive compatibility guidance.

Vercel settings remain:

| Setting | Value |
| --- | --- |
| Root Directory | network/webbluetooth |
| Framework | Other |
| Build/install commands | Empty/disabled |
| Output directory | . |
| Environment variables | None |

Deploy only that static folder, never .env, Flask, Python, SQLite, models or drivers.
The password reset public origin must be the HTTPS **main backend**, not this
static-only host. No Vercel deployment was made during this task.

## Development, verification and hardware acceptance

Windows supports protocol/auth/mocked adapter tests and the static frontend, not
the real BlueZ/system D-Bus service. Install requirements in an isolated environment:
```powershell
python -m pip install -r server/requirements.txt -r network/rpi/requirements.txt
python -X utf8 -m unittest discover -s server/tests -p test_admin_recovery.py -v
python -X utf8 -m unittest discover -s network/tests -v
node --test network/tests/web.test.mjs
node network/tools/browser-check.mjs
python -m http.server 8080 --bind 127.0.0.1 --directory network/webbluetooth
```

Node 20+ is needed for tests. The optional browser helper uses installed Chrome,
a temporary profile and loopback server, with screenshots in ignored network/.qa.
Use localhost on that computer or trusted HTTPS; a phone's plain LAN-IP URL is not
a secure Web Bluetooth context.

Pending on an actual Pi 4B + Android Chrome: physical passkey pairing, rejection of
Just Works, protected GATT enforcement, correct/wrong Admin password, password
changes while connected, persistent lockout, Wi-Fi scan/connect/rollback, boot/
BlueZ restart, and PWA installation. Final acceptance: move away from Home-WiFi,
disable phone internet, open the Ready cached page, reconnect the prepaired phone,
verify the Admin password locally, scan/connect another network, receive the new
SSID/IP, and open the existing dashboard. Do not claim real provisioning verified
until this succeeds. Test outcomes and file inventory are in TESTING.md.
