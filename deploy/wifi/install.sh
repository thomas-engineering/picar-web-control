#!/usr/bin/env bash
# Install the Wi-Fi selector on the Pi. Run from this directory with sudo.
set -euo pipefail
cd "$(dirname "$0")"
install -m 755 picar-wifi-select /usr/local/sbin/picar-wifi-select
install -m 755 90-picar-wifi /etc/NetworkManager/dispatcher.d/90-picar-wifi
install -m 644 picar-wifi.service picar-wifi.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now picar-wifi.timer
