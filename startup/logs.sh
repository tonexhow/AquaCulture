#!/usr/bin/env bash
set -euo pipefail
command -v journalctl >/dev/null || { echo 'journalctl unavailable' >&2; exit 1; }
echo 'Following AquaCulture logs (Ctrl+C to exit)...'
exec journalctl -u aquaculture-web.service -u aquaculture-ble.service -n 40 -f
