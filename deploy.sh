#!/usr/bin/env bash
# Copy the project to the Pi. Usage: ./deploy.sh user@picar.local
set -euo pipefail

target="${1:?usage: ./deploy.sh user@host}"
cd "$(dirname "$0")"

# data/ holds the Pi's own calibration, so it is never overwritten from here.
rsync -av --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude 'data' --exclude 'imager' \
    ./ "$target:video_car/"
