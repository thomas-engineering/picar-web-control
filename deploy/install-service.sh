#!/usr/bin/env bash
# Install and start the PiCar server as a systemd service (starts at boot).
# Run from the project directory on the Pi: ./deploy/install-service.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as your normal user, not root; sudo is used where needed." >&2
    exit 1
fi

sed -e "s|__USER__|$USER|" -e "s|__DIR__|$PWD|g" deploy/picar.service \
    | sudo tee /etc/systemd/system/picar.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable picar.service
sudo systemctl restart picar.service
echo "Installed. Logs: journalctl -u picar -f"
