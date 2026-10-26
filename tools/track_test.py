#!/usr/bin/env python3
"""Run the colour tracker on the synthetic scene and check it. No hardware needed.

    .venv/bin/python tools/track_test.py [frames] [--save DIR]

The object is learned in frame 0 from a box in the middle of the image, like the Learn button
will do. Exit status 1 means the tracker failed one of the checks.
"""
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.follow import ColorTracker, draw  # noqa: E402
from synthetic_scene import DIM, HIDDEN, render  # noqa: E402

FRAMES = 600
SIZE = (320, 240)


def main():
    args = sys.argv[1:]
    save = None
    if "--save" in args:
        i = args.index("--save")
        save = args[i + 1]
        del args[i:i + 2]
        os.makedirs(save, exist_ok=True)
    frames = int(args[0]) if args else FRAMES

    width, height = SIZE
    tracker = ColorTracker()
    side = height // 3
    image, _ = render(0, SIZE)
    tracker.learn(image, ((width - side) // 2, (height - side) // 2, side, side))

    try:
        ColorTracker().learn(np.full((height, width, 3), 128, np.uint8), (100, 80, 80, 80))
        print("FAIL: a grey region was learned")
        return 1
    except ValueError:
        pass

    errors, wrong, recover, spent = [], 0, [], 0.0
    lost_since = None
    for i in range(frames):
        image, target = render(i, SIZE)
        start = time.perf_counter()
        track = tracker.update(image)
        spent += time.perf_counter() - start
        if target is not None and track.state == "tracking":
            x, y, r = target
            bx, by, bw, bh = track.box
            error = ((bx + bw / 2 - x) ** 2 + (by + bh / 2 - y) ** 2) ** 0.5 / r
            errors.append(error)
            if error > 1.5:
                wrong += 1  # on a distractor or off target
        if target is None and track.state == "tracking":
            wrong += 1
        if target is not None and i % 300 not in DIM and lost_since is None and track.state != "tracking":
            lost_since = i
        if lost_since is not None and track.state == "tracking" and target is not None:
            recover.append(i - lost_since)
            lost_since = None
        if save and i % 50 == 0:
            draw(image, track)
            cv2.imwrite(os.path.join(save, "frame%04d.png" % i), image)

    visible = sum(1 for i in range(frames) if i % 300 not in HIDDEN)
    print("frames %d, visible %d, tracked %d" % (frames, visible, len(errors)))
    print("mean error %.2f radii, worst %.2f" % (sum(errors) / len(errors), max(errors)))
    print("wrong frames %d, recoveries %s" % (wrong, recover))
    print("%.1f ms per frame" % (spent / frames * 1000))
    ok = len(errors) > 0.9 * visible and wrong <= 0.02 * frames and all(r <= 15 for r in recover)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(main())
