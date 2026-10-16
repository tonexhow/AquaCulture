#!/usr/bin/env bash
# Removes ONLY units made by startup/install.sh; never deletes project data.
set -Eeuo pipefail
MARKER='# Managed by AquaCulture startup/install.sh'
[[ "$(id -u)" -eq 0 ]] || { echo 'Run: sudo bash startup/uninstall.sh' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'systemctl unavailable' >&2; exit 1; }
changed=0
for unit in aquaculture-web.service aquaculture-ble.service; do
  dest="/etc/systemd/system/$unit"
  if [[ ! -f "$dest" ]]; then
    echo "[AquaCulture] $unit not installed"
  elif ! grep -Fqx "$MARKER" "$dest"; then
    echo "[AquaCulture] Skipped unmanaged unit $dest" >&2
  else
    systemctl disable --now "$unit" || true
    rm -- "$dest"
    echo "[AquaCulture] Removed $unit"
    changed=1
  fi
done
if (( changed )); then
  systemctl daemon-reload
  systemctl reset-failed aquaculture-web.service aquaculture-ble.service 2>/dev/null || true
fi
echo '[AquaCulture] Finished. Project source, .env, database, Python and model binaries are unchanged.'
