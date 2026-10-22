#!/usr/bin/env python3
"""Measure how video traffic affects control latency, from the client's side (needs `pip install websockets`).

    python tools/lag_test.py --host picar.local:8000 --ssh user@picar.local

Per run, for the default and the maximum camera resolution, it streams the video, moves the camera
over the WebSocket like the web page does, pings the Pi, and prints:
video rate, control round trip (median, 95th percentile, worst), ping loss and the Pi's CPU,
socket backlog, undervoltage events and TCP retransmissions (the last group needs --ssh).

Run it from a machine on the same Wi-Fi path as the phone. It takes over the controls and moves
the camera, so do not run it while driving. It leaves the camera on the default resolution.
"""
import argparse
import asyncio
import json
import math
import os
import subprocess
import threading
import time
import urllib.request

import websockets


def set_max_resolution(base, enabled):
    request = urllib.request.Request(
        f"{base}/api/camera", json.dumps({"max": enabled}).encode(), {"content-type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


async def run(args, label, max_res):
    base = f"http://{args.host}"
    set_max_resolution(base, max_res)
    await asyncio.sleep(3)  # let the camera reopen

    video = {"bytes": 0, "frames": 0}
    stop = threading.Event()

    def pull_video():
        try:
            stream = urllib.request.urlopen(f"{base}/video", timeout=8)
            while not stop.is_set():
                chunk = stream.read(16384)
                video["bytes"] += len(chunk)
                video["frames"] += chunk.count(b"\xff\xd8")
        except OSError:
            pass

    threading.Thread(target=pull_video, daemon=True).start()
    await asyncio.sleep(2)

    probe = None
    if args.ssh:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_probe.sh")
        with open(script) as f:
            probe = subprocess.Popen(
                ["ssh", args.ssh, f"bash -s -- {args.secs}"], stdin=f,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
    host = args.host.split(":")[0]
    ping = subprocess.Popen(
        ["ping", "-c", str(int((args.secs - 2) / 0.2)), "-i", "0.2", host],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )

    rtts = []
    ws = await websockets.connect(f"ws://{args.host}/ws", ping_interval=None)

    async def reader():
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") == "pong":
                rtts.append((time.monotonic() - message["t"]) * 1000)

    task = asyncio.create_task(reader())
    last_ping = 0.0
    start = time.monotonic()
    while time.monotonic() - start < args.secs - 3:
        now = time.monotonic()
        message = {"steer": 0, "throttle": 0, "pan_rate": math.sin((now - start) * 2), "tilt_rate": math.cos((now - start) * 2)}
        if now - last_ping > 0.25:
            message["ping"] = now
            last_ping = now
        await ws.send(json.dumps(message))
        await asyncio.sleep(0.03)
    await ws.send(json.dumps({"pan_rate": 0, "tilt_rate": 0, "throttle": 0}))
    task.cancel()
    await ws.close()
    stop.set()

    rtts.sort()
    pct = lambda p: rtts[min(len(rtts) - 1, int(len(rtts) * p))]
    elapsed = time.monotonic() - start
    ping_lines = ping.communicate()[0].strip().splitlines()
    print(f"=== {label}")
    print(f"video: {video['frames'] / elapsed:.1f} fps, {video['bytes'] / elapsed / 1024:.0f} KiB/s, "
          f"avg frame {video['bytes'] / max(1, video['frames']) / 1024:.0f} KiB")
    print(f"control round trip: median {pct(.5):.0f} ms, p95 {pct(.95):.0f} ms, worst {rtts[-1]:.0f} ms; "
          f"over 100 ms: {sum(r > 100 for r in rtts)}/{len(rtts)}")
    if len(ping_lines) >= 2:
        print(f"ping: {ping_lines[-2].split(',')[2].strip()}; {ping_lines[-1]}")
    if probe:
        print(probe.communicate()[0].rstrip(), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="server as host:port, for example picar.local:8000")
    parser.add_argument("--ssh", help="user@host of the Pi, to also sample its CPU, queues and power")
    parser.add_argument("--secs", type=int, default=30, help="length of each run (default 30)")
    parser.add_argument("--only", choices=["default", "max"], help="test just one resolution")
    args = parser.parse_args()
    try:
        if args.only != "max":
            await run(args, "default resolution", False)
        if args.only != "default":
            await run(args, "maximum resolution", True)
    finally:
        set_max_resolution(f"http://{args.host}", False)


asyncio.run(main())
