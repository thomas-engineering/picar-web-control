#!/usr/bin/env python3
"""Record a test clip from the car's webcam, for tuning the follow-mode tracker offline.

Run it on the Pi (python3-opencv from apt is enough):

    python3 tools/record_clip.py NAME [--seconds 25] [--size 320x240] [--fps 15] [--countdown 5]

NAME is a short label such as bright-ball or grey-box. The frames are kept in memory while
recording, so the slow SD card cannot drop any, and are written to clips/NAME/ afterwards as
JPEG files (quality 95) with a meta.json. Copy them to the PC with:

    scp -r picar:video_car/clips/NAME .

Close the control page in every browser first: the server only releases the camera when no
viewer is left. Move the object slowly through the picture, closer and farther, partly behind
something once, with other things in view.
"""
import argparse
import json
import os
import sys
import time

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("name")
    parser.add_argument("--seconds", type=float, default=25)
    parser.add_argument("--size", default="320x240", help="WIDTHxHEIGHT, like the car (default 320x240)")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--countdown", type=int, default=5, help="seconds before recording starts")
    parser.add_argument("--device", default="/dev/video0")
    args = parser.parse_args()
    if not args.name or "/" in args.name or args.name.startswith("."):
        sys.exit("NAME must be a plain label")
    width, height = (int(v) for v in args.size.split("x"))
    out = os.path.join(ROOT, "clips", args.name)
    if os.path.exists(out) and os.listdir(out):
        sys.exit("%s is not empty; pick another name" % out)

    cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2) if args.device.startswith("/dev/") else cv2.VideoCapture(args.device)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        sys.exit("cannot open %s. Is the control page still open in a browser?" % args.device)
    for _ in range(10):  # let the automatic exposure settle
        cap.read()

    for left in range(args.countdown, 0, -1):
        print("starting in %d ..." % left, flush=True)
        end = time.monotonic() + 1
        while time.monotonic() < end:
            cap.read()  # keep draining so the first recorded frame is fresh
    print("recording %.0f s" % args.seconds, flush=True)

    frames, stamps = [], []
    start = time.monotonic()
    while time.monotonic() - start < args.seconds:
        ok, image = cap.read()
        if not ok:
            print("frame read failed", file=sys.stderr)
            break
        stamps.append(time.monotonic() - start)
        ok, jpg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if ok:
            frames.append(jpg.tobytes())
    cap.release()
    if not frames:
        sys.exit("no frames recorded")

    os.makedirs(out, exist_ok=True)
    for i, data in enumerate(frames):
        with open(os.path.join(out, "%05d.jpg" % i), "wb") as f:
            f.write(data)
    duration = stamps[-1]
    meta = {
        "name": args.name,
        "frames": len(frames),
        "width": image.shape[1],
        "height": image.shape[0],
        "seconds": round(duration, 2),
        "fps": round((len(frames) - 1) / duration, 1) if duration else None,
        "timestamps": [round(t, 3) for t in stamps],
    }
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f)
    print("%d frames, %.1f fps, %dx%d, saved to %s" % (len(frames), meta["fps"] or 0, meta["width"], meta["height"], out))


main()
