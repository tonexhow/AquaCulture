# Verification report — Admin recovery / BLE Admin access / offline readiness

Implementation completed in the existing Windows workspace. No real recovery
email, Pi BLE pairing, Wi-Fi change or Vercel deployment was performed.

## Results

| Check | Actual result |
| --- | --- |
| Recovery/shared credential/durable limiter tests | 11 passed |
| BLE protocol/privacy/pairing gate and NetworkManager tests | 20 passed, mocked hardware |
| JavaScript protocol/cache/security tests | 6 passed |
| Existing notification regression tests | 8 passed |
| Full server discovery | 38 passed, 2 existing Windows cleanup errors |
| Installed Chrome checks | Passed: phone/tablet/desktop widths, themes, Admin/Wi-Fi eyes, hidden unauthorized controls, readiness, missing-file repair, new-version activation/cleanup, offline reload, unsupported BLE/SW |
| Python compile / JS syntax / installer Bash syntax | Passed |
| Existing application database checksum | Identical before/after |

The 37 focused tests cover this update. The notification checks verify the prior
summary/safety work remains intact. The full server count includes the 11 recovery
and 8 notification cases. The two unchanged failures are:
test_database_index_utility_idempotent_on_temporary_copy and
test_migrations_idempotent_on_database_copy. Their original tests/helpers leave
SQLite handles open and Windows refuses temporary-file deletion (WinError 32).
Those unrelated files were not changed. UTF-8 mode is required by older Windows
tests that read templates without an explicit encoding.

## Commands executed

```powershell
python -X utf8 -m unittest discover -s server/tests -p test_admin_recovery.py -v
python -X utf8 -m unittest discover -s server/tests -p test_notification_schedule.py -v
python -X utf8 -m unittest discover -s network/tests -v
python -X utf8 -m unittest discover -s server/tests -v
node --test network/tests/web.test.mjs
node network/tools/browser-check.mjs
python -m compileall -q aquaculture_auth.py server/api_manager.py server/settings/admin_password.py server/settings/password_recovery.py network/rpi
node --check network/webbluetooth/app.js
node --check network/webbluetooth/ble.js
node --check network/webbluetooth/offline.js
node --check network/webbluetooth/service-worker.js
node --check client/static/auth/recovery.js
node --check client/static/index/index.js
node --check client/static/settings/settings.js
```

The installer was syntax-checked with the installed Git Bash using -n; it was not
run on Windows. Network tests used the prior isolated, ignored network/.dev-deps
dbus-next dependency. No new global dependency installation was needed. The browser
helper used a hidden installed Chrome, a loopback-only asset server and a temporary
profile. It closed the browser/server and removed only that generated profile.
Inspected screenshots remain in ignored network/.qa. Its version-update test
virtually served 1.1.1; the source release remains 1.1.0.

## Covered behavior

Recovery: existing/unknown account generic responses, SMTP failure without leaking
account state, origin validation, token hash storage, 15-minute expiration, invalid/
reused tokens, confirmation mismatch, disabled account, concurrent single-use
reset, all-token invalidation, successful password verification/update, web session
revocation, recovery email changes, request limits, no-cache/no-referrer pages,
query-token log redaction, preserved legacy preferences/mode and local initialization
recovery. SMTP is mocked; queued delivery uses synthetic addresses and tokens.

BLE: current password against the real shared hash store, wrong/empty password,
disconnection, connection-bound counters/IDs, replay rejection, unavailable store,
expiry, live password change, unauthorized info/status/history/scan/configuration,
per-peer private responses, queued-data revocation, authenticated GATT flags and
a closed physical pairing gate/Just Works rejection. The rolling 100 ceiling,
restart persistence, rotating identifiers, progressive delays and short-window
failures are verified at the shared limiter used by BLE. Successful pairing and
radio encryption are NOT simulated as proof of hardware success.

NetworkManager's existing scan normalization, hidden/WPA2/WPA3 validation,
rate limiting, progress, timeout/IP failure, successful persistence, failed-attempt
rollback, history bounds and per-adapter internet reporting remain covered.

Cache: complete manifest/assets, active page controller requirement, real/mocked
cache entry checks, missing asset/error state, repair, safe version changes,
obsolete app-cache removal, retained unrelated cache, actual offline navigation,
unsupported Bluetooth/SW, no sensitive query/POST caching and storage boundaries.
Timestamp is generated only after a successful preparation. Chrome tests detected
no runtime exceptions at 320/390/768/1280px in either theme. The Admin form was made
visible as a UI fixture for screenshots; that does not represent actual BLE login.

## Files created in this update

- aquaculture_auth.py
- SECURITY.md
- server/settings/password_recovery.py
- server/tests/test_admin_recovery.py
- client/templates/password_recovery.html
- client/static/auth/recovery.js
- client/static/auth/recovery.css
- network/rpi/pairing.py
- network/webbluetooth/offline.js
- network/webbluetooth/version.js

## Existing files modified in this update

- .env.example
- README.md
- server/api_manager.py
- server/settings/admin_password.py
- client/templates/index.html
- client/templates/settings.html
- client/static/index/index.js
- client/static/settings/settings.js
- network/rpi/authentication.py
- network/rpi/controller.py
- network/rpi/ble_service.py
- network/rpi/main.py
- network/rpi/wifi_manager.py (protocol/service metadata only)
- network/rpi/requirements.txt
- network/rpi/aquaculture-ble.service
- network/rpi/install.sh
- network/webbluetooth/ble.js
- network/webbluetooth/app.js
- network/webbluetooth/index.html
- network/webbluetooth/style.css
- network/webbluetooth/service-worker.js
- network/tools/browser-check.mjs
- network/tests/test_provisioning.py
- network/tests/web.test.mjs
- network/README.md
- network/protocol.md
- network/TESTING.md

Retired: network/tests/crypto_client.mjs, the obsolete protocol-1 custom-crypto
test helper. Its behavior was replaced by shared Admin verification and authenticated
GATT/privacy checks. No user data was removed.

The real .env, application database, credentials/configuration, drivers, sensor
rules, scoring, camera/model code and notification implementation were not edited
by this task. Credential migration is implemented for first application use/Pi
installation; development tests ran only against temporary stores. Existing
.gitignore already excludes all new runtime SQLite files. This workspace has no
.git directory, so no commit/diff was produced.

## Pending validation / operational prerequisites

PENDING HARDWARE: Pi/Android physical passkey pairing, rejection of an old Just
Works bond, actual authenticated-encryption permission enforcement, systemd/Polkit/
group access, boot/adapter recovery, offline Admin verification and Wi-Fi scan/
provision/rollback with SSID/IP reporting.

The final mandatory scenario remains: prepair the authorized phone; prepare the
cache to Ready; remove both phone/Pi internet and previous Wi-Fi; reopen cached
setup; authenticate with the current Admin password locally; scan/connect a new
network; receive its new IP; open the existing dashboard.

PENDING CONFIGURATION/DELIVERY: set the real main-backend HTTPS public origin,
associate the actual Admin recovery email, confirm existing SMTP delivery and
configure reverse-proxy/WSGI access logs to omit token queries. None of these
values were guessed or changed in the developer's real .env.

LIMITATIONS: first-time secure pairing needs a trusted Pi console; browser storage
clearing needs a new online cache load; Web Bluetooth cannot introspect link security
flags and relies on the genuine physically paired Pi's BlueZ permissions; initial
credential migration should be performed during a maintenance window; async email
delivery can be lost if the process exits before delivery. A missing/unavailable
credential store fails closed for Admin verification while discovery remains public.

Installation and physical lockout/password recovery commands are in README.md and
../SECURITY.md. No requested implementation work is awaiting approval. Physical
and deployment validation require the stated devices/configuration.
