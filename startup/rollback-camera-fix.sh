#!/usr/bin/env bash
# Restore only the camera package's recorded files and original web service state.
set -Eeuo pipefail
if [[ $# -ne 1 || "$1" == -h || "$1" == --help ]]; then
  printf 'Usage: sudo bash startup/rollback-camera-fix.sh /var/backups/aquaculture-camera-fix/BACKUP\n'
  exit 1
fi
CAMERA_HELPER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ ! -f "$CAMERA_HELPER_DIR/deploy-camera-fix.sh" || -L "$CAMERA_HELPER_DIR/deploy-camera-fix.sh" ]]; then
  printf 'Camera deployment helper is unavailable or unsafe; restore the supplied helper from the verified package first.\n' >&2
  exit 1
fi
exec bash "$CAMERA_HELPER_DIR/deploy-camera-fix.sh" --rollback "$1"
