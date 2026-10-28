#!/usr/bin/env python3
"""Stress the car's power supply and check the logs afterwards (needs `pip install websockets`).

    python tools/power_stress.py --host 192.168.x.x:8000 --ssh picar --wheels-off

THE MOTORS RUN. Lift the car so the wheels spin freely, keep the power switch within reach and
ask the user first. Stages, each --secs long, from light to heavy load: video only, video plus
tracker, then the same with the motors at a steady speed, with the steering and camera sweeping,
and with fast direction changes (the hardest on a weak supply). While it runs, the Pi's
throttle flags are sampled; the first live undervoltage stops the motors and ends the test.
Afterwards it compares dmesg, the boot id and the picar journal with the state before and
prints a verdict. Exit status 1 means the supply (or something else) failed a check.

The motors are driven through the server's WebSocket like the web page does, so the 0.3 s
watchdog still stops them if this script or the link dies.
"""
import argparse
import asyncio
import json
import subprocess
import threading
import time
import urllib.request

import websockets

# journal lines that say the server or a stop path misbehaved
JOURNAL_PATTERN = r"Traceback|Error|ERROR|watchdog|Stopped:|lost the signal|silent|OSError|I2C|Remote I/O"
UNDERVOLT = r"[Uu]nder-?voltage|brown-?out|voltage"


def ssh(target, command, timeout=30):
    out = subprocess.run(["ssh", target, command], capture_output=True, text=True, timeout=timeout)
    return out.stdout.strip()


def pi_state(target):
    """Boot id, uptime, undervoltage lines in dmesg and the throttle flags right now."""
    command = (
        "cat /proc/sys/kernel/random/boot_id; cut -d' ' -f1 /proc/uptime; "
        f"sudo -n dmesg -T | grep -Ei '{UNDERVOLT}'; echo ---; vcgencmd get_throttled"
    )
    lines = ssh(target, command).splitlines()
    split = lines.index("---")
    return {
        "boot_id": lines[0], "uptime": float(lines[1]),
        "dmesg": lines[2:split], "throttled": lines[split + 1].split("=")[-1],
    }


def call(base, method, path):
    request = urllib.request.Request(base + path, method=method)
    return json.load(urllib.request.urlopen(request, timeout=15))


class Sampler:
    """Reads the Pi's throttle flags twice a second; bit 0 means undervoltage right now."""

    def __init__(self, target):
        self.flags = set()
        self.live_undervoltage = threading.Event()
        self.proc = subprocess.Popen(
            ["ssh", target, "while true; do vcgencmd get_throttled; sleep 0.5; done"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                value = int(line.split("=")[-1], 16)
            except ValueError:
                continue
            self.flags.add(value)
            if value & 0x1:
                self.live_undervoltage.set()

    def take(self):
        flags, self.flags = self.flags, set()
        return flags

    def close(self):
        self.proc.terminate()


async def drain(ws):
    """Read and drop the server's state messages; an unread queue also stops the answers to its pings."""
    try:
        async for _ in ws:
            pass
    except websockets.WebSocketException:
        pass


async def drive(ws, stage, secs, abort):
    """Send the page's continuous message every 30 ms for `secs` seconds."""
    start = time.monotonic()
    while time.monotonic() - start < secs and not abort.is_set():
        t = time.monotonic() - start
        message = {"steer": 0, "throttle": 0, "pan_rate": 0, "tilt_rate": 0}
        if stage == "steady":
            message["throttle"] = 0.6
        elif stage == "sweep":
            message["throttle"] = 1.0
            message["steer"] = 1 if int(t * 0.8) % 2 == 0 else -1
            message["pan_rate"] = 1 if int(t * 1.5) % 2 == 0 else -1
            message["tilt_rate"] = 1 if int(t * 1.1) % 2 == 0 else -1
        elif stage == "reversals":
            message["throttle"] = 1.0 if int(t * 2) % 2 == 0 else -1.0  # direction change every 0.5 s
        await ws.send(json.dumps(message))
        await asyncio.sleep(0.03)
    return time.monotonic() - start


async def run(args):
    base = f"http://{args.host}"
    before = pi_state(args.ssh)
    since = ssh(args.ssh, "date '+%Y-%m-%d %H:%M:%S'")
    print(f"before: boot {before['boot_id'][:8]}, uptime {before['uptime']:.0f} s, "
          f"throttled {before['throttled']}, {len(before['dmesg'])} voltage lines in dmesg")

    stop = threading.Event()

    def pull_video():
        try:
            stream = urllib.request.urlopen(f"{base}/video", timeout=8)
            while not stop.is_set():
                stream.read(16384)
        except OSError:
            pass

    threading.Thread(target=pull_video, daemon=True).start()
    sampler = Sampler(args.ssh)
    ws = await websockets.connect(f"ws://{args.host}/ws", ping_interval=None)
    drainer = asyncio.create_task(drain(ws))
    results, failed, failed_link = [], False, False
    stages = [("video only", "idle"), ("video + tracker", "idle"), ("motors steady", "steady"),
              ("motors + steering + camera", "sweep"), ("motor reversals", "reversals")]
    tracker_on = False
    try:
        for name, kind in stages:
            if kind != "idle" or "tracker" in name:
                if not tracker_on:
                    call(base, "POST", "/api/follow/learn")  # learns whatever is in the reticle
                    tracker_on = True
            sampler.take()
            print(f"stage: {name} ({args.secs} s)", flush=True)
            try:
                await drive(ws, kind, args.secs, sampler.live_undervoltage)
                await ws.send(json.dumps({"steer": 0, "throttle": 0, "pan_rate": 0, "tilt_rate": 0}))
            except websockets.WebSocketException as error:
                # The server's watchdog has stopped the motors; report it and carry on with a new link.
                print(f"  CONNECTION DROPPED: {error!r}, close code {getattr(ws, 'close_code', None)}")
                results.append((name + " (link dropped)", sampler.take(), False))
                failed_link = True
                ws = await websockets.connect(f"ws://{args.host}/ws", ping_interval=None)
                drainer = asyncio.create_task(drain(ws))
                continue
            flags = sampler.take()
            live = sampler.live_undervoltage.is_set()
            results.append((name, flags, live))
            if live:
                print("  UNDERVOLTAGE seen live: motors stopped, test ended early")
                failed = True
                break
            await asyncio.sleep(2)  # let the supply recover between stages
    finally:
        try:
            await ws.send(json.dumps({"steer": 0, "throttle": 0, "pan_rate": 0, "tilt_rate": 0}))
            await asyncio.sleep(0.5)
            await ws.close()
        except websockets.WebSocketException:
            pass
        if tracker_on:
            try:
                call(base, "DELETE", "/api/follow")
            except OSError:
                pass
        sampler.close()
        stop.set()

    print()
    for name, flags, live in results:
        shown = " ".join(sorted("0x%x" % f for f in flags)) or "no samples"
        print(f"  {name:28s} throttled flags: {shown}{'  <- UNDERVOLTAGE' if live else ''}")

    await asyncio.sleep(1)
    try:
        status = call(base, "GET", "/api/status")
        stopped = status["throttle"] == 0
    except OSError:
        stopped = False
        print("  server did not answer after the test")
    after = pi_state(args.ssh)
    new_dmesg = [l for l in after["dmesg"] if l not in before["dmesg"]]
    journal = ssh(args.ssh, f"sudo -n journalctl -u picar --since '{since}' --no-pager | grep -E '{JOURNAL_PATTERN}' | tail -20")

    print()
    checks = [
        ("motors stopped at the end", stopped),
        ("Pi did not reboot", after["boot_id"] == before["boot_id"]),
        ("no new voltage lines in dmesg", not new_dmesg),
        ("no live undervoltage during any stage", not failed),
        ("control link stayed up in every stage", not failed_link),
        ("no throttling or frequency cap flags (0x2, 0x4, 0x8) during any stage",
         not any(f & 0xE for _, flags, _ in results for f in flags)),
    ]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    if new_dmesg:
        print("new dmesg lines:\n  " + "\n  ".join(new_dmesg))
    print("picar journal since the test began (matching errors/stop events):")
    print("  " + (journal.replace("\n", "\n  ") if journal else "nothing"))
    print(f"throttled now: {after['throttled']} (0x50000 only means it happened earlier since boot)")
    ok = all(ok for _, ok in checks)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="the Pi's IPv4 address and port, for example 192.168.x.x:8000")
    parser.add_argument("--ssh", default="picar", help="ssh target for the Pi (default: picar)")
    parser.add_argument("--secs", type=int, default=20, help="seconds per stage")
    parser.add_argument("--wheels-off", action="store_true", required=True,
                        help="confirm the wheels are off the ground and the user agreed to run the motors")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
