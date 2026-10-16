# AquaCulture

Admin recovery/BLE security update: [SECURITY.md](SECURITY.md) covers the shared
hashed credential store, recovery email/public HTTPS origin configuration,
single-use reset links, session revocation, persistent attempt limits and trusted
local recovery. [network/README.md](network/README.md) describes protocol 2 pairing
and Admin verification plus the verified versioned offline cache. Existing SMTP
settings are reused; no additional email provider or webhook is required.

Existing Flask/Socket.IO application, sensor drivers, database tools and client UI.
The new independent Bluetooth Wi-Fi setup component is in [network/](network/README.md).

The main app still runs with its existing root `.env` and Python environment:

```powershell
python -m pip install -r server/requirements.txt
python server/app.py
```

Settings → Notifications uses seconds. New configurations default to **86400 seconds
(24 hours)**. Existing saved intervals are preserved: the old file format has no
metadata distinguishing the previous one-hour default from a deliberate choice.
Admins can enter 172800 for 48 hours, or -1 to disable periodic summaries. Immediate
threshold alerts retain their separate hold, cooldown and sensor validity rules.
Periodic summaries use `database/notification_schedule.sqlite3` for durable atomic
reservations across restarts/processes. Delivery is at most once per interval;
a crash after reservation may skip a summary instead of duplicating it. Keep this
file when restarting/upgrading. Changing the interval uses the last reservation as
the baseline. Disabled intervals do not reserve slots; re-enabling may send a due
summary immediately. The first run starts a new interval, without a startup email.

The Network item has been removed from Settings. Legacy `server/network/`,
`server/network_app.py` and existing `/network*` compatibility routes remain for
the optional local panel (port 5001); they are not used by the standalone BLE page.
The new component imports as `rpi` from `network/`, avoiding a package-name collision
with the existing backend's `network` package. The Flask app does not import BlueZ.

Only `network/webbluetooth` is suitable for the requested Vercel deployment. Do not
deploy the full repository there. No cloud backend participates in provisioning.
Installation, authorization, offline setup and hardware acceptance steps are in
[network/README.md](network/README.md); the wire format is in
[network/protocol.md](network/protocol.md).

`.gitignore` excludes secrets, live databases/media and optional large model weights,
while preserving source schemas/migrations/docs. Existing local data and `.env` are
not changed. If `models/` is supplied later, retain its documentation and install
weights locally using the existing scanner's expected paths. BLE has no model dependency.
