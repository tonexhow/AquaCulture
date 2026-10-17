#!/usr/bin/env bash
# Install AquaCulture systemd auto-start units; safe to rerun.
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$HERE/.." && pwd -P)"
MARKER='# Managed by AquaCulture startup/install.sh'
help_msg() { cat <<'TXT'
Usage: sudo bash startup/install.sh [--dry-run] [--web-only|--ble-only] [--python /absolute/python] [--user linuxuser]
Does not install packages, change .env, or modify the database or ML models.
TXT
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
DRY=0; WEB=1; BLE=1; PY=''; RUN_USER=''
while (( $# )); do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --web-only) BLE=0; shift ;;
    --ble-only) WEB=0; shift ;;
    --python) (( $# >= 2 )) || die 'Missing --python argument'; PY="$2"; shift 2 ;;
    --user) (( $# >= 2 )) || die 'Missing --user argument'; RUN_USER="$2"; shift 2 ;;
    -h|--help) help_msg; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done
(( WEB || BLE )) || die 'Cannot specify both --web-only and --ble-only'
# Unit-file paths must not contain systemd expansion codes or unit-breaking whitespace.
if [[ "$ROOT" =~ [[:space:]%\\] ]]; then die "Project path has unsupported whitespace or special characters: $ROOT"; fi
(( !WEB )) || [[ -f "$ROOT/server/app.py" ]] || die 'Expected server/app.py not found'
(( !BLE )) || [[ -f "$ROOT/network/rpi/main.py" ]] || die 'Expected network/rpi/main.py not found. Use --web-only if unavailable.'
if [[ -z "$RUN_USER" ]]; then
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
    RUN_USER="$SUDO_USER"
  else
    RUN_USER="$(stat -c '%U' "$ROOT")"
  fi
fi
[[ "$RUN_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*$ ]] || die 'Unrecognized Linux username'
id "$RUN_USER" >/dev/null 2>&1 || die "Unknown user: $RUN_USER"
[[ "$RUN_USER" != root ]] || die 'Refusing to run application as root: use --user YOUR_LINUX_USERNAME'
RUN_GROUP="$(id -gn "$RUN_USER")"
if [[ -z "$PY" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"
  elif [[ -x "$ROOT/venv/bin/python" ]]; then PY="$ROOT/venv/bin/python"
  else PY="$(command -v python3 || true)"; fi
fi
[[ "$PY" == /* && -x "$PY" ]] || die "Python path must be an absolute executable path: $PY"
"$PY" -c 'import sys; assert sys.version_info.major == 3' || die 'Python 3 required'
printf '[AquaCulture] Folder: %s\n[AquaCulture] User: %s:%s\n[AquaCulture] Python: %s\n' "$ROOT" "$RUN_USER" "$RUN_GROUP" "$PY"
(( !WEB )) || echo '[AquaCulture] Main web service: aquaculture-web.service'
(( !BLE )) || echo '[AquaCulture] Bluetooth service: aquaculture-ble.service'
[[ -f "$ROOT/.env" ]] || echo '[AquaCulture] NOTE: No .env found; configure it if the application needs it.'
if (( DRY )); then echo '[AquaCulture] DRY RUN: no files changed.'; exit 0; fi
[[ $(id -u) -eq 0 ]] || die 'Please run using sudo bash startup/install.sh'
command -v systemctl >/dev/null || die 'systemctl not installed'
[[ -d /run/systemd/system ]] || die 'systemd is not running as init'
# Check BOTH selected unit names before writing any unit.
for unit in $( (( WEB )) && echo aquaculture-web.service; (( BLE )) && echo aquaculture-ble.service ); do
  dest="/etc/systemd/system/$unit"
  if [[ -e "$dest" ]] && ! grep -Fqx "$MARKER" "$dest"; then
    die "Not overwriting unmanaged unit: $dest. Inspect it before continuing."
  fi
done
write_unit() {
  local name="$1" script="$2" after="$3" wants="$4" tmp
  tmp="$(mktemp "/etc/systemd/system/.${name}.XXXXXX")"
  cat > "$tmp" <<UNIT
$MARKER
[Unit]
Description=AquaCulture Python - $name
After=$after
Wants=$wants
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$ROOT
ExecStart=$PY -u $ROOT/$script
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=10
TimeoutStopSec=25
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
  chmod 644 "$tmp"
  mv -f "$tmp" "/etc/systemd/system/$name"
  echo "[AquaCulture] Installed $name"
}
(( !WEB )) || write_unit 'aquaculture-web.service' 'server/app.py' 'network.target' 'network.target'
(( !BLE )) || write_unit 'aquaculture-ble.service' 'network/rpi/main.py' 'dbus.service bluetooth.service' 'bluetooth.service'
systemctl daemon-reload
start() {
  local name="$1"
  systemctl enable "$name" >/dev/null
  if ! systemctl restart "$name"; then
    journalctl -u "$name" -n 35 --no-pager >&2 || true
    die "Failed to start $name; examine logs and dependencies"
  fi
  echo "[AquaCulture] $name: $(systemctl is-active "$name") / $(systemctl is-enabled "$name")"
}
(( !WEB )) || start aquaculture-web.service
(( !BLE )) || start aquaculture-ble.service
echo '[AquaCulture] Installed. Check: bash startup/status.sh'
