# Admin recovery and local BLE access

This repository has a single Admin account, not a username/email account table.
Recovery therefore needs an explicit association; notification subscribers and
the SMTP sender are **not** automatically treated as the Admin's email address.

## Configure recovery

Keep the existing root .env and EMAIL_USER / EMAIL_APP_PASSWORD SMTP values.
Set AQUACULTURE_PUBLIC_ORIGIN to the HTTPS origin actually serving the main Flask
backend (scheme + hostname + optional port; no path, user info, query or fragment).
The static-only Vercel network site cannot serve password reset endpoints.
No domain or localhost default is hardcoded. Missing/invalid origin or SMTP settings
leave recovery unavailable with a generic public response and a safe diagnostic.

Set AQUACULTURE_ADMIN_EMAIL before the first credential migration, or sign in and
use Settings → Access → Password recovery email. Saving an association requires
the current Admin password. Once initialized, the database association is the
authority; changing an environment variable does not silently redirect recovery.

The legacy JSON password is migrated unchanged to Werkzeug's standard scrypt hash,
then removed from configuration.json. Other settings remain intact. Perform the
initial migration in a maintenance window so concurrent settings edits cannot race
that one-time JSON update. The development task did not migrate real workspace
credentials: tests use temporary synthetic accounts. Use Settings or the trusted
local CLI to choose a new 12–128-character password before deployment, especially
if the original installation still used its old default. No default password is
created when credentials are missing or corrupt.

Windows store: database/admin_auth.sqlite3. Pi installer store:
/var/lib/aquaculture-auth/admin.sqlite3. AQUACULTURE_AUTH_DB can explicitly select
an absolute path. Both main app and BLE must use the same store, never separate
copies. The installer preserves an existing hashed account via SQLite backup and
installs a location marker so a temporarily missing shared database cannot cause
fallback to an old local verifier. The BLE service reads this store only; its
own request limits and history live separately under /var/lib/aquaculture-ble.
No machine-learning models or running Flask process are required for BLE verification.

## Reset flow and API

The login modal links to /forgot-password. POST /api/admin/password/forgot accepts
email and returns the same message for an eligible/unknown account or SMTP failure:
"If the account exists, a password reset link has been sent."
Malformed email input is rejected without revealing account state. Requests use
durable global and per-address/client limits: 20/day globally, 5/hour per source,
3/hour per submitted address. Client/address identifiers are hashed for storage.
An asynchronous, bounded, single-worker delivery queue avoids making SMTP latency
an account-existence signal. A server crash before delivery can lose that queued
email; request another link after the appropriate rate window.

The trusted backend generates 32 random bytes in a URL-safe token, stores only its
SHA-256 digest with a 900-second expiration and current credential revision, and
sends an HTTPS link through the existing SMTP sender. No webhook dependency was
added: existing notification delivery is unchanged. SMTP failure invalidates the
new token. Recovery mail contains a reset link, never a generated password.

/reset-password reads the token into foreground memory and immediately removes
it from browser history. The page has only local assets, no-referrer and no-store
headers, no analytics, eye buttons, confirmation and 12–128-character validation.
POST /api/admin/password/reset takes token/new_password/confirm_password in JSON.
One SQLite transaction checks expiry, eligibility, revision and single-use state,
changes the scrypt hash/revision, consumes all outstanding reset tokens, and records
a bounded security event. Concurrent use of the same token cannot succeed twice.
Invalid/expired links receive the same error. The new password must be chosen by
the user; it is never included in a URL, webhook or log.

Password changes in Settings, resets and local recovery revoke prior HTTP and BLE
sessions by revision. Web session metadata is checked against the current revision
on each protected access, including another worker's cached sessions. The existing
web UI signs out after a password change. Password checks/change/email-verification
share a durable web authentication ceiling of 100/day with failed-attempt limits;
reset-token submissions use a separate protected 100/day scope. These web limits
are separate from the Pi's explicit 100 BLE verifications/day ceiling.

The JSON session file remains an existing compatibility mechanism; no new browser
credential persistence was introduced. Password fields are cleared after use.
The independent BLE page stores only the theme preference, no passwords or bearer
credentials. Protect the trusted GitHub/Vercel/main HTTPS frontend: modified
JavaScript or a compromised browser could steal passwords entered there.

## Access logs and HTTPS deployment

Werkzeug query-token redaction is registered before routes handle reset requests.
Never turn on SMTP debug logging or log request bodies/full reset URLs. External
reverse proxies and WSGI servers must also omit query strings; app filtering cannot
change logs written by a separate proxy. For example:

```nginx
log_format aqua_safe '$remote_addr $request_method $uri $status';
access_log /var/log/nginx/aquaculture-access.log aqua_safe;
```

For Gunicorn use a format containing %(m)s %(U)s %(s)s, and omit %(r)s/%(q)s.
Configure HTTPS at the main backend's public origin. The code never derives reset
links from untrusted Host/forwarded headers. Authentication rate limits use the
direct peer address and a global ceiling; configure a trusted proxy deliberately
if you need accurate per-client addresses behind one. Spoofing X-Forwarded-For
cannot bypass the global limits.

## BLE protection and local recovery

See [network/README.md](../network/README.md) for exact installation, first physical
passkey pairing, the current Admin verification flow, persisted rolling limits,
ten-minute authorization, lockout recovery and the mandatory real-device test.
The old setup-key cryptographic exchange is retired. Current transport uses BlueZ
authenticated encrypted GATT and per-peer reads; no custom encryption algorithms
or permanent secondary BLE password are introduced. First-time pairing needs a
trusted local console; a prepaired phone can subsequently recover Wi-Fi offline.
If the supported stack cannot meet authenticated GATT access, the page blocks
password submission rather than downgrading transport.

Trusted root console password recovery (prompts privately):

```bash
sudo /opt/aquaculture/network/.venv/bin/python /opt/aquaculture/network/server/auth.py --database /var/lib/aquaculture-auth/admin.sqlite3 --set-password
```

Windows development/local-owner equivalent:

```powershell
.\.venv\Scripts\python.exe server/auth.py --set-password
```

These are local administrative commands, not network or unauthenticated BLE APIs.
Routine upgrades should preserve all authentication/limit state. Back up the
credential database privately; do not commit it, runtime sessions or configuration.

## Verification boundaries

SMTP delivery is mocked in automated tests; no real recovery email was sent.
Physical MITM-authenticated BLE pairing, protected characteristic enforcement,
systemd/Polkit/group permissions and offline Wi-Fi provisioning remain pending on
the supported Pi 4B/Android Chrome combination. Local Chrome did verify caching,
missing-file repair, version update/cleanup, offline reload, themes and UI controls.

Security contracts: [Werkzeug password hashing](https://werkzeug.palletsprojects.com/en/stable/utils/),
[OWASP recovery guidance](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html),
[BlueZ Agent API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Agent.rst),
[BlueZ authenticated GATT flags](https://github.com/bluez/bluez/blob/master/doc/org.bluez.GattCharacteristic.rst).
