#!/usr/bin/env bash
set -uo pipefail
command -v systemctl >/dev/null || { echo 'Requires Linux with systemd' >&2; exit 1; }
for unit in aquaculture-web.service aquaculture-ble.service; do
  printf '\n========== %s ==========\n' "$unit"
  state="$(systemctl show "$unit" --property=LoadState --value 2>/dev/null || true)"
  if [[ "$state" == not-found || -z "$state" ]]; then
    echo 'Not installed.'
    continue
  fi
  systemctl --no-pager --full status "$unit" || true
  echo '----- Last 15 journal entries -----'
  journalctl -u "$unit" -n 15 --no-pager 2>/dev/null || echo 'Logs unavailable; try sudo bash startup/status.sh'
done
