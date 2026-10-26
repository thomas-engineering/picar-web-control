#!/usr/bin/env bash
# Download the NanoTrack model files (about 1.8 MB) that follow mode uses into models/.
# Run it on the Pi. The files are not part of the repository: check the licence of the model
# before copying them anywhere else.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
base=https://github.com/HonglinChu/SiamTrackers/raw/master/NanoTrack/models/nanotrackv2

fetch() {
    local name=$1 sum=$2
    if [ -f "models/$name" ] && echo "$sum  models/$name" | sha256sum -c --quiet -; then
        echo "$name is up to date"
        return
    fi
    curl -fsSL -o "models/$name.part" "$base/$name"
    echo "$sum  models/$name.part" | sha256sum -c --quiet - || { rm -f "models/$name.part"; echo "$name: checksum mismatch" >&2; exit 1; }
    mv "models/$name.part" "models/$name"
    echo "$name downloaded"
}

fetch nanotrack_backbone_sim.onnx 530bdd0cd00f19afab79a863e71ba71e3312395a5dc9151af675082bdaaa2fc4
fetch nanotrack_head_sim.onnx 0d8c0637be849f092cc7236cae02e55c8b9455ebe37ba50601d6115db4247cd9
