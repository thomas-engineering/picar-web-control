#!/usr/bin/env python3
"""Run several trackers over a recorded clip (tools/record_clip.py) and compare them.

    python3 tools/compare_trackers.py clips/bright-ball --box 168,96,32,29 --out OUT \
        [--trackers color,kcf,csrt,mil,nano] [--models DIR] [--sheets 12]

All trackers start from the same box in the first frame (x,y,w,h in pixels). The script prints the
time per frame and how long each one keeps reporting a result, writes OUT/results.json (boxes,
scores, times) and OUT/sheet*.png, which show every tracker's box on sampled frames so the
result can be judged by eye (the clips have no ground truth).

DIR must hold nanotrack_backbone_sim.onnx and nanotrack_head_sim.onnx for the "nano" tracker.
"""
import argparse
import glob
import json
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.follow import ColorTracker  # noqa: E402

COLOURS = {"color": (0, 255, 0), "kcf": (255, 0, 0), "csrt": (255, 0, 255), "mil": (0, 255, 255), "nano": (255, 255, 0)}


def make(name, models):
    if name == "kcf":
        return cv2.TrackerKCF_create()
    if name == "csrt":
        return cv2.TrackerCSRT_create()
    if name == "mil":
        return cv2.TrackerMIL_create()
    if name == "nano":
        params = cv2.TrackerNano_Params()
        params.backbone = os.path.join(models, "nanotrack_backbone_sim.onnx")
        params.neckhead = os.path.join(models, "nanotrack_head_sim.onnx")
        return cv2.TrackerNano_create(params)
    raise ValueError(name)


def run(name, frames, box, models):
    """Return a list with one (box or None, score or None, seconds) entry per frame."""
    results = []
    if name == "color":
        tracker = ColorTracker()
        try:
            tracker.learn(frames[0], box)
        except ValueError as error:
            print("  color: %s" % error)
            return [(None, None, 0.0)] * len(frames)
        for frame in frames:
            start = time.perf_counter()
            track = tracker.update(frame)
            spent = time.perf_counter() - start
            found = track.state == "tracking" and track.confidence > 0
            results.append((track.box if found else None, round(track.confidence, 2), spent))
        return results
    tracker = make(name, models)
    tracker.init(frames[0], tuple(box))
    for frame in frames:
        start = time.perf_counter()
        ok, found = tracker.update(frame)
        spent = time.perf_counter() - start
        score = round(float(tracker.getTrackingScore()), 2) if name == "nano" else None
        results.append(([int(v) for v in found] if ok else None, score, spent))
    return results


def sheets(frames, runs, indices, out):
    scale = 2
    tiles = []
    for i in indices:
        image = cv2.resize(frames[i], None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        for k, (name, results) in enumerate(runs.items()):
            found = results[i][0]
            if found:
                x, y, w, h = (v * scale for v in found)
                cv2.rectangle(image, (x - k, y - k), (x + w + k, y + h + k), COLOURS[name], 1)
        cv2.putText(image, "frame %d" % i, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        tiles.append(image)
    legend = np.zeros((24, tiles[0].shape[1] * 2, 3), np.uint8)
    x = 6
    for name in runs:
        cv2.putText(legend, name, (x, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOURS[name], 1)
        x += 90
    for n in range(0, len(tiles), 4):
        group = tiles[n:n + 4]
        while len(group) < 4:
            group.append(np.zeros_like(tiles[0]))
        sheet = np.vstack([legend, np.hstack(group[:2]), np.hstack(group[2:])])
        cv2.imwrite(os.path.join(out, "sheet%d.png" % (n // 4)), sheet)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("clip")
    parser.add_argument("--box", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--trackers", default="color,kcf,csrt,mil,nano")
    parser.add_argument("--models", default=".")
    parser.add_argument("--sheets", type=int, default=12, help="number of sampled frames to draw")
    args = parser.parse_args()
    box = tuple(int(v) for v in args.box.split(","))
    files = sorted(glob.glob(os.path.join(args.clip, "*.jpg")))
    frames = [cv2.imread(f) for f in files]
    os.makedirs(args.out, exist_ok=True)
    print("%d frames of %s, start box %s, OpenCV %s" % (len(frames), args.clip, box, cv2.__version__))

    runs = {}
    for name in args.trackers.split(","):
        results = run(name, frames, box, args.models)
        runs[name] = results
        times = sorted(r[2] for r in results[1:])
        kept = sum(r[0] is not None for r in results)
        first_loss = next((i for i, r in enumerate(results) if r[0] is None), None)
        print("%-6s %5.1f ms mean, %5.1f ms p95 | result in %3d of %d frames, first without result: %s" % (
            name, 1000 * sum(times) / len(times), 1000 * times[int(len(times) * 0.95)], kept, len(frames),
            first_loss if first_loss is not None else "never"))
    json.dump({n: [[r[0], r[1], round(r[2], 4)] for r in rs] for n, rs in runs.items()},
              open(os.path.join(args.out, "results.json"), "w"))
    indices = [int(i * (len(frames) - 1) / (args.sheets - 1)) for i in range(args.sheets)]
    sheets(frames, runs, indices, args.out)


main()
