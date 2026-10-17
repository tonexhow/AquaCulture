#!/usr/bin/env bash
set -Eeuo pipefail
MARKER='# Managed by AquaCulture startup/install.sh'
[[ "$(id -u)" -eq 0 ]] || { echo 'Run: sudo bash startup/restart.sh' >&2; exit 1; }
count=0
for unit in aquaculture-web.service aquaculture-ble.service; do
  file="/etc/systemd/system/$unit"
  if [[ -f "$file" ]] && grep -Fqx "$MARKER" "$file"; then
    echo "[AquaCulture] Restarting $unit"
    systemctl restart "$unit"
    systemctl --no-pager is-active "$unit"
    count=$((count + 1))
  else
    echo "[AquaCulture] Skipping $unit (not installed by this kit)"
  fi
done
(( count > 0 )) || { echo 'No managed services found. Run install.sh first.' >&2; exit 1; }
