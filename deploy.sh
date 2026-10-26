#!/usr/bin/env bash
# Copy the project to the Pi. Usage: ./deploy.sh user@picar.local
set -euo pipefail

target="${1:?usage: ./deploy.sh user@host}"
cd "$(dirname "$0")"

# data/ holds the Pi's own calibration and clips/ its recorded test clips, so neither is overwritten
# or deleted from here. The .lgd-* files belong to the running server (lgpio creates them).
rsync -av --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude 'data' --exclude 'clips' --exclude 'imager' --exclude '.lgd-*' \
    ./ "$target:video_car/"
