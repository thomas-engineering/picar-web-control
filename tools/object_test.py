#!/usr/bin/env python3
"""Check ObjectTracker on the recorded clips (tools/record_clip.py). Skipped if the clips are missing.

    .venv/bin/python tools/object_test.py [--models DIR]

The expected positions were read off the frames by eye. clips/bright-ball is a red disc that is
fully hidden by a hand at frames 234-258 while a second red object sits at the left edge;
clips/grey-box is a small plain grey cube that a second hand covers at frames 63-68. Exit status 1 means a check failed.
"""
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.follow import ObjectTracker  # noqa: E402

DISC_START = (168, 96, 32, 29)
CUBE_START = (202, 88, 19, 25)
DISC_AT = {226: (143, 131), 317: (133, 141), 362: (129, 140), 408: (57, 101), 453: (168, 126), 499: (171, 114)}
TOLERANCE = 14  # pixels, centre of the box to the centre read off by eye
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-4s %-44s %s" % ("PASS" if ok else "FAIL", name, detail))


def perturb(image, kind):
    """The frame as it would look in other light: darker, brighter, warmer, cooler, other gamma, more or less saturated."""
    f = image.astype(np.float32)
    if kind.startswith("gain"):
        f *= float(kind[4:])
    elif kind == "warm":
        f[..., 0] *= 0.82
        f[..., 2] *= 1.18
    elif kind == "cool":
        f[..., 0] *= 1.18
        f[..., 2] *= 0.82
    elif kind.startswith("gamma"):
        f = 255 * (f / 255) ** float(kind[5:])
    elif kind in ("desat", "sat"):
        grey = f.mean(2, keepdims=True)
        f = grey + (f - grey) * (0.65 if kind == "desat" else 1.35)
    return np.clip(f, 0, 255).astype(np.uint8)


def load(clip):
    return [cv2.imread(f) for f in sorted(glob.glob(os.path.join(ROOT, "clips", clip, "*.jpg")))]


def centre(track):
    x, y, w, h = track.box
    return x + w / 2, y + h / 2


def near(track, point):
    return track.state == "tracking" and abs(centre(track)[0] - point[0]) < TOLERANCE and abs(centre(track)[1] - point[1]) < TOLERANCE


def main():
    models = None
    if "--models" in sys.argv:
        models = sys.argv[sys.argv.index("--models") + 1]
    models = models or os.path.join(ROOT, "models")
    ball, cube = load("bright-ball"), load("grey-box")
    if not ball or not cube:
        print("SKIP: clips/bright-ball and clips/grey-box are needed")
        return 0

    tracker = ObjectTracker(models)
    tracker.learn(ball[0], DISC_START)
    tracks = [tracker.update(f) for f in ball]
    check("red disc: tier is colour", tracker.tier == "colour")
    check("red disc: followed before the hand", all(near(tracks[i], DISC_AT[226]) for i in (226,)),
          "box at 226 %s" % (tracks[226].box,))
    lost = next(i for i, t in enumerate(tracks) if i > 200 and t.state == "lost")
    check("red disc: hidden disc is noticed", 232 <= lost <= 250, "lost at frame %d (%s)" % (lost, tracks[lost].reason))
    back = next(i for i, t in enumerate(tracks) if i > lost and t.state == "tracking")
    check("red disc: found again after the hand", 258 <= back <= 295, "tracking again at frame %d" % back)
    check("red disc: never follows the red object at the left edge",
          all(t.state != "tracking" or t.box[0] > 40 for t in tracks[back:]), "")
    for i in (317, 362, 408, 453, 499):
        check("red disc: on the disc at frame %d" % i, near(tracks[i], DISC_AT[i]), "box %s" % (tracks[i].box,))
    after = [t.confidence for t in tracks[back + 20:] if t.state == "tracking"]  # the disc is still half under the hand before
    check("red disc: confident while following", sum(c > 0.5 for c in after) >= 0.98 * len(after),
          "%.1f %% of frames above 0.5" % (100 * sum(c > 0.5 for c in after) / len(after)))

    fresh = ObjectTracker(models)
    fresh.learn(ball[0], DISC_START)
    profile = fresh.export()
    loaded = ObjectTracker(models)
    loaded.load(profile)
    again = [loaded.update(f) for f in ball[300:]]
    first = next((i for i, t in enumerate(again) if t.state == "tracking"), None)
    check("saved object: found from a loaded profile", first is not None and first < 20
          and near(again[first + 6], DISC_AT[317]) if first is not None and first + 6 < len(again) else False,
          "tracking after %s frames" % first)

    tracker = ObjectTracker(models)
    tracker.learn(cube[0], CUBE_START)
    tracks = [tracker.update(f) for f in cube]
    check("grey cube: tier is track-only", tracker.tier == "track-only")
    check("grey cube: followed at the start", tracks[45].state == "tracking" and tracks[45].box[0] > 100, "box at 45 %s" % (tracks[45].box,))
    lost = next((i for i, t in enumerate(tracks) if t.state == "lost"), None)
    check("grey cube: no false alarm while it is in view", lost is None or lost >= 64, "first loss at frame %s" % lost)
    check("grey cube: a hand covering it is noticed (frames 63-68)", lost is not None and lost <= 80,
          "lost at frame %s (%s)" % (lost, tracks[lost].reason if lost is not None else ""))
    check("grey cube: it never sits on the wrong thing for long afterwards",
          sum(t.state == "tracking" for t in tracks[lost:]) <= 0.1 * len(tracks[lost:]) if lost is not None else False,
          "%d of %d frames still tracking after the loss" % (sum(t.state == "tracking" for t in tracks[lost:]), len(tracks[lost:])) if lost is not None else "")
    try:
        tracker.export()
        check("grey cube: cannot be saved", False)
    except ValueError:
        check("grey cube: cannot be saved", True)

    # The same clips in other light: no false alarm, and the hand is still noticed.
    for kind in ("gain0.6", "gain1.6", "warm", "cool", "gamma0.7", "gamma1.5", "sat"):
        tracker = ObjectTracker(models)
        tracker.learn(perturb(ball[0], kind), DISC_START)
        tracks = [tracker.update(perturb(f, kind)) for f in ball]
        lost = next((i for i, t in enumerate(tracks) if t.state == "lost"), None)
        back = next((i for i, t in enumerate(tracks) if lost is not None and i > lost and t.state == "tracking"), None)
        check("%s: red disc is judged by hue, not brightness" % kind,
              tracker.tier == "colour" and lost is not None and 232 <= lost <= 265 and back is not None and back <= 300
              and near(tracks[362], DISC_AT[362]),
              "lost at %s, found again at %s" % (lost, back))
        tracker = ObjectTracker(models)
        tracker.learn(perturb(cube[0], kind), CUBE_START)
        tracks = [tracker.update(perturb(f, kind)) for f in cube]
        lost = next((i for i, t in enumerate(tracks) if t.state == "lost"), None)
        check("%s: grey cube is not mistaken for a coloured one" % kind,
              tracker.tier == "track-only" and lost is not None and 64 <= lost <= 80,
              "%s, lost at %s" % (tracker.tier, lost))

    print("PASS" if all(RESULTS) else "FAIL")
    return 0 if all(RESULTS) else 1


sys.exit(main())
