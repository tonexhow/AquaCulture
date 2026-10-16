#!/bin/bash
# Run on the Pi: sudo bash network/rpi/install.sh
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run with sudo on the Raspberry Pi.' >&2
    exit 1
fi
if ! command -v nmcli >/dev/null || ! systemctl is-active --quiet NetworkManager; then
    echo 'An active NetworkManager installation is required. No network manager will be installed or replaced.' >&2
    exit 1
fi
python3 -c 'import sys; assert (3, 11) <= sys.version_info[:2] < (3, 15), "Use Python 3.11 through 3.14 with this service dependency set"'
nm_version="$(LC_ALL=C nmcli --version)"
nm_version="${nm_version##* }"
if ! dpkg --compare-versions "$nm_version" ge 1.42; then
    echo 'NetworkManager 1.42 or later is required; no automatic manager migration will be attempted.' >&2
    exit 1
fi
nmcli --version
nmcli -t -f DEVICE,TYPE,STATE device status
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$source_dir/../.." && pwd)"
app_user="${AQUACULTURE_APP_USER:-${SUDO_USER:-$(stat -c '%U' "$project_root")}}"
install_dir=/opt/aquaculture/network
apt-get update
apt-get install -y bluez python3-venv polkitd
bluez_version="$(bluetoothctl --version)"
bluez_version="${bluez_version##* }"
if ! dpkg --compare-versions "$bluez_version" ge 5.66; then
    echo 'BlueZ 5.66 or later is required for the GATT security flags.' >&2
    exit 1
fi
if ! id aquaculture-ble >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/aquaculture-ble --no-create-home --shell /usr/sbin/nologin aquaculture-ble
fi
install -d -m 0755 "$install_dir/rpi"
install -m 0644 "$source_dir/"*.py "$install_dir/rpi/"
install -m 0644 "$source_dir/requirements.txt" "$install_dir/rpi/requirements.txt"
python3 -m venv "$install_dir/.venv"
"$install_dir/.venv/bin/python" -m pip install -r "$install_dir/rpi/requirements.txt"
install -m 0644 "$project_root/aquaculture_auth.py" "$install_dir/aquaculture_auth.py"
if ! getent group aquaculture-auth >/dev/null; then
    groupadd --system aquaculture-auth
fi
install -d -m 2750 -o "$app_user" -g aquaculture-auth /var/lib/aquaculture-auth
# Run during a maintenance window; the legacy JSON password is imported once,
# hashed, then removed while all unrelated configuration fields are preserved.
"$install_dir/.venv/bin/python" "$install_dir/aquaculture_auth.py" \
    --database /var/lib/aquaculture-auth/admin.sqlite3 \
    --legacy-config "$project_root/database/configuration.json" \
    --import-existing "$project_root/database/admin_auth.sqlite3"
chown "$app_user":aquaculture-auth /var/lib/aquaculture-auth/admin.sqlite3
chmod 0640 /var/lib/aquaculture-auth/admin.sqlite3
install -d -m 0755 /etc/aquaculture-auth
install -m 0644 /dev/null /etc/aquaculture-auth/installed
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$source_dir/50-aquaculture-ble.rules" /etc/polkit-1/rules.d/50-aquaculture-ble.rules
install -m 0644 "$source_dir/aquaculture-ble.service" /etc/systemd/system/aquaculture-ble.service
systemctl daemon-reload
systemctl enable --now bluetooth.service
systemctl enable aquaculture-ble.service
systemctl restart aquaculture-ble.service
echo 'Installed. Check: systemctl status aquaculture-ble.service'
echo 'Pair a phone in a trusted local pairing window before relying on headless recovery.'
