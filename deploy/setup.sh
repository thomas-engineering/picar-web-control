#!/usr/bin/env bash
# One-time setup on the Pi. Run from the project directory: ./deploy/setup.sh
# Targets current Raspberry Pi OS (Debian 12 "Bookworm" or newer).
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as your normal user, not root; sudo is used where needed." >&2
    exit 1
fi

. /etc/os-release
if [ "${VERSION_ID:-0}" -lt 12 ] 2>/dev/null; then
    echo "Needs Debian 12 or newer (found ${PRETTY_NAME:-unknown})." >&2
    exit 1
fi

echo "==> Installing system packages"
sudo apt-get update
# lgpio is the GPIO backend gpiozero uses on current Pi OS, and ships as an apt package.
sudo apt-get install -y python3-venv python3-lgpio i2c-tools python3-opencv

echo "==> Enabling I2C"
sudo raspi-config nonint do_i2c 0

echo "==> Granting hardware access"
sudo usermod -aG i2c,gpio "$USER"

echo "==> Creating virtualenv"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r deploy/requirements.txt

echo
echo "Done. Log out and back in (or reboot) so the group changes apply, then:"
echo "  i2cdetect -y 1                      # expect 40 (and 70)"
echo "  .venv/bin/python -m server.hwtest scan"
echo "Then start the server at boot with: ./deploy/install-service.sh"
