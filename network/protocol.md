# AquaCulture BLE protocol 2 — service 1.1.0

This is a security upgrade of the existing service, not a second network service.
Service UUID remains `7a1c0001-9e67-4a21-bde4-33c5eab07a01`.
The custom PSK/HMAC/AES session exchange from protocol 1 is retired. The old
setup.key is not used. No new permanent BLE password is created.

| Characteristic (same remaining UUID) | UUID prefix | Permissions |
| --- | --- | --- |
| Bootstrap | 7a1c0002 | Public read, basic device name/protocol/auth requirement only |
| Command | 7a1c0003 | encrypt-authenticated-write (MITM-authenticated encrypted BLE) |
| Response | 7a1c0004 | encrypt-authenticated-read, private response queue per BlueZ peer |

BlueZ's GATT security permissions enforce the protected transport. No Admin or
Wi-Fi password may be written until the protected Response read succeeds.
An unsupported browser, old Just Works bond, closed initial-pairing window, or
stack that cannot satisfy authenticated encryption must fail closed. There is no
plaintext or Just Works fallback and no custom application cryptography.

## Trust and pairing

First pair at a trusted Pi console: open the 120-second physical pairing window,
connect the phone, read the BlueZ-generated passkey locally, and enter it in
Android's system pairing dialog. DisplayOnly is the agent capability; automatic
numeric confirmation and Just Works authorization are rejected. The ephemeral
passkey is held in /run/aquaculture-ble/pairing-code with restricted permissions,
never BLE, logs, browser storage, or a cloud service. Keep the resulting OS bond.
See README.md for commands. Only select the known Pi paired through this physical
procedure; an advertised name alone is not device identity.

Web Bluetooth cannot directly report a link's MITM/security level. The supported
Pi service therefore enforces it in BlueZ, and the page gates password entry on
its protected read. The browser is not a generic verifier of arbitrary third-party
peripheral implementations. Real pairing/security enforcement on the supported
Pi/Android combination is pending hardware validation. If the stack refuses the
protected access, credentials remain blocked.

## Bootstrap and connection-bound requests

Example public read:
```json
{"v":2,"device_name":"AquaCulture-Pi","session":"<32 hex>","authentication_required":true,"authentication":"admin_password","security":"authenticated_ble_pairing_required","expires_in":600}
```

The random session is freshly created for this BlueZ connection. It exposes no
hardware identifiers, SSIDs, profiles or history. Maximum eight active sessions.
Read offsets are supported. A new bootstrap clears this peer's old response queue.

Every request is UTF-8 JSON inside the framing below:
```json
{"id":"unique-request-id","session":"<bootstrap session>","counter":1,"type":"admin_auth","payload":{"password":"<current AquaCulture Admin password>"}}
```

IDs are 1–64 ASCII letters/digits/underscore/hyphen. Counter starts at 1 and must
increase by exactly one. Duplicate IDs or counters are rejected before a credential
verification or configuration side effect. Retain up to 256 IDs, then reconnect.
A replay from a previous connection has the wrong random session ID. Transport
messages are accepted only from the BlueZ peer attached to that connection.

Before Admin verification, only admin_auth and auth_state are allowed. auth_state
returns only this session's authorized boolean. admin_auth checks the authoritative
local scrypt hash, never a remote API. It returns:
```json
{"id":"unique-request-id","session":"...","type":"result","status":"IDLE","payload":{"authorized":true,"message":"Admin verified.","expires_in":600},"final":true}
```

A failed password returns authorized=false, the generic message
"Unable to verify the Admin password.", and retry_after_seconds. No passwords,
hashes or permanent bearer credentials are returned.

Authorization expires after ten minutes, disconnect, service restart, account
disable, or password revision change. Each privileged command and queued private
response read validates the current revision. An idle foreground client detects
revocation on its next protected response poll. Accepted Wi-Fi operations can
finish after the client disconnects, but subsequent private replies are withheld
once authorization is invalid. Reconnect/verify and inspect status before retrying.

## Persistent attempt limits

The Pi stores reservations in /var/lib/aquaculture-ble/auth_limits.sqlite3.
All processed Admin verifications (success or failure) count toward 100 per rolling
86400 seconds globally. Restarting or rotating identifiers does not reset it.
Five failed or still-in-flight verifications per rolling 15 minutes, plus
progressive failure delays 1,2,4,8,16… seconds (capped at 300), limit brute force.
A supplemental 20/hour per BlueZ peer limit does not replace the global limits;
a MAC/address alone is never treated as trusted identity. In-process delays use
monotonic time; durable windows use timestamps and a non-decreasing clock watermark.
An unreliable/backwards Pi clock can prolong lockout; use the physical recovery
procedure, not an unauthenticated BLE command.

Rate-limit error includes category RATE_LIMITED and retry_after_seconds. Blocked
or replayed requests do not invoke the password verifier. The UI cannot reset
limits. Local root recovery is documented in README.md.

## Existing privileged operations

After Admin verification: info, status, history, scan, configure. Only configure
takes parameters; unknown fields/commands are rejected.

```json
{"id":"wifi-2","session":"...","counter":2,"type":"configure","payload":{"ssid":"Home","security":"wpa-psk","password":"<Wi-Fi password>","hidden":false}}
```

Security: open, wpa-psk (WPA/WPA2 Personal), sae (WPA3 Personal). SSID 1–32 UTF-8
bytes without controls. WPA2 passphrase 8–63 bytes or 64 hex PSK; SAE 1–63 bytes.
Enterprise, WEP and OWE require another setup flow. Captive portals may require
separate sign-in. Failed replacement profiles do not modify/delete known-good
profiles; restoration of the prior profile is attempted.

Responses retain id/type/status/payload/final; private responses also contain the
session ID. Progress states: IDLE, SCANNING, SCAN_COMPLETE, CONNECTING,
AUTHENTICATING, OBTAINING_IP, CONNECTED_LOCAL, CONNECTED_ONLINE, FAILED,
TIMED_OUT, DISCONNECTED. info/status/history payloads remain the existing formats:
real device information, network/IP/signal/security, bounded profile metadata and
connection observations, never saved passwords. Internet=true is reported only
from NetworkManager's actual per-Wi-Fi connectivity result, otherwise false/null.

Error:
```json
{"id":"wifi-2","session":"...","type":"error","status":"FAILED","error":{"category":"AUTH_REQUIRED","message":"Admin verification required."},"final":true}
```

Network categories retain AUTHENTICATION_REJECTED, SSID_UNAVAILABLE,
IP_ASSIGNMENT_FAILED, TIMED_OUT, ADAPTER_UNAVAILABLE, UNSUPPORTED_SECURITY,
BUSY, RATE_LIMITED, INVALID_REQUEST and UNKNOWN_ERROR. There is no promise to
distinguish an incorrect password unless NetworkManager actually reports it.
Authorization expiry events have type authorization_expired and no private data.

## Framing, private reads and limits

Existing seven-byte binary header is unchanged:

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 0 | 1 | 0xA1 |
| 1 | 2 | Transport message ID, big endian |
| 3 | 2 | Zero-based sequential chunk index, big endian |
| 5 | 2 | Total UTF-8 message length, big endian |
| 7 | 1–13 | Message fragment |

20-byte values work at the minimum ATT MTU. Reassemble before decoding UTF-8/JSON.
Maximum 32768 encoded bytes; one incomplete message per peer, 30-second inactivity
expiry, reset on disconnect/malformed/duplicate/out-of-order fragments.
Writes use ATT write-with-response. Private response frames are fetched with
protected ReadValue calls on Response. ReadValue's device option isolates queues
by peer; no sensitive GATT Value property or broadcast notification is used.
Empty read means no queued response. At most 6000 queued frames per peer.
Reads recheck Admin revision before returning private bytes. The page serializes
GATT operations, drains responses during requests, and polls about once a second
when idle. Read offset >0 returns empty, so a long-read continuation cannot consume
a second frame.

Final responses acknowledge requests; do not automatically replay configuration
after a lost response. Client timeout 180 seconds; Wi-Fi activation 75 seconds,
rollback 25. Scan requests are at least 15 seconds apart, configurations ten seconds,
and only one network operation runs at a time. Local BLE discovery and public
bootstrap do not depend on Flask, Wi-Fi, internet, or the credential database.
