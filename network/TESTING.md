# Network verification

The retained `network/tests/` suites protect the provisioning protocol, Admin
authorization, authenticated GATT requirements, NetworkManager behavior, and
offline browser cache. They use mocked hardware and synthetic authentication
stores. They do not prove physical Bluetooth pairing or live Wi-Fi provisioning.

## Local checks

Run from the repository root after installing development dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r server/requirements.txt -r network/rpi/requirements.txt
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s network/tests -v
node --test network/tests/web.test.mjs
node --check network/webbluetooth/app.js
node --check network/webbluetooth/ble.js
node --check network/webbluetooth/offline.js
node --check network/webbluetooth/service-worker.js
```

Node 20+ is required for the browser protocol/cache tests. Optional installed-Chrome
checks run with `node network/tools/browser-check.mjs`; the helper uses a temporary
profile, a loopback server, and `network/.qa/` screenshots. It checks
responsive layouts, themes, password controls, offline readiness, missing-cache
repair, version changes, offline reload, and unsupported browser states.

Linux equivalents use `.venv/bin/python`. The BlueZ service itself requires
Linux system D-Bus. Never run installer scripts on Windows to validate them;
use `bash -n network/rpi/install.sh` for syntax only.

## Covered behavior

The Python suite checks the shared Admin hash store, wrong/empty passwords,
connection-bound authorization, replay rejection, unavailable stores, expiry,
credential revision changes, private per-peer responses, authenticated GATT flags,
and the physical pairing gate. Persistent verification limits are exercised with
synthetic accounts. No production credential migration is required for these tests.

NetworkManager fixtures check scan normalization, hidden/WPA2/WPA3 input,
rate limits, connection progress, timeouts, successful persistence, rollback,
history bounds, and adapter internet reporting.

JavaScript checks cover the static manifest, offline cache readiness, missing-file
handling, repair, version changes, obsolete app-cache cleanup, unrelated-cache
retention, and exclusion of sensitive queries, non-GETs, and credentials.

## Raspberry Pi acceptance

Verify on the supported Pi 4B and Android Chrome combination:

1. Perform physical passkey pairing and reject Just Works/old insecure bonds.
2. Confirm authenticated GATT permissions and correct/incorrect Admin verification.
3. Change the Admin password while connected and verify authorization revocation.
4. Confirm limits survive service restarts and `/var/lib/aquaculture-ble` stays writable.
5. Exercise Wi-Fi scan, connect, failure rollback, SSID/IP reporting, reboot, and BlueZ recovery.
6. Prepare the browser cache until Ready, then disable phone and Pi internet and
   remove the previous Wi-Fi connection. Reopen the cached page, reconnect the
   prepaired phone, authenticate locally, connect another network, and open the
   dashboard at its reported address.

Physical pairing, radio encryption, real SMTP delivery, service permissions,
automatic startup, and actual provisioning require device testing. A passing
mocked suite must not be reported as hardware verification. First-time pairing
requires a trusted Pi console; cleared browser storage requires an online cache
load. See [README.md](README.md) for installation and
[../server/SECURITY.md](../server/SECURITY.md) for password recovery and logging.
