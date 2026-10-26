#!/usr/bin/env python3
"""End-to-end check of follow mode against the mock server (fake hardware, synthetic camera).

    .venv/bin/python tools/follow_e2e.py [--camera-only]

It plays the part of the page: a video viewer, a WebSocket that sends idle control messages every
30 ms, and the HTTP calls. --camera-only starts the server with PICAR_FOLLOW_DRIVE=0 and checks that
it never drives or steers (the server default, without PICAR_FOLLOW_DRIVE=1). Then it checks every way follow mode has to end and the limits it keeps.
Needs models/ (deploy/fetch_models.sh). Exit status 1 means a check failed.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8766
URL = "http://localhost:%d" % PORT
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-4s %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if response.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


async def call(method, path, body=None):
    return await asyncio.to_thread(http, method, path, body)


class Viewer(threading.Thread):
    """Holds /video open, which is what keeps the camera running."""

    def __init__(self):
        super().__init__(daemon=True)
        self.response = urllib.request.urlopen(URL + "/video", timeout=8)
        self.start()

    def run(self):
        try:
            while self.response.read(4096):
                pass
        except Exception:
            pass

    def close(self):
        self.response.close()


class Page:
    """A fake control page: idle messages every 30 ms unless it is made silent."""

    def __init__(self):
        self.ws = None
        self.silent = False
        self.messages = []

    async def open(self):
        self.ws = await websockets.connect("ws://localhost:%d/ws" % PORT)
        self.task = asyncio.create_task(self._send())
        self.reader = asyncio.create_task(self._read())

    async def _send(self):
        try:
            while True:
                if not self.silent:
                    await self.ws.send(json.dumps({"steer": 0, "throttle": 0, "pan_rate": 0, "tilt_rate": 0}))
                await asyncio.sleep(0.03)
        except Exception:
            pass

    async def _read(self):
        try:
            async for text in self.ws:
                self.messages.append(json.loads(text))
        except Exception:
            pass

    async def send(self, message):
        await self.ws.send(json.dumps(message))

    async def close(self):
        self.task.cancel()
        self.reader.cancel()
        await self.ws.close()


async def status():
    return (await call("GET", "/api/status"))[1]


async def until(predicate, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def following():
    return (await status())["following"]


async def not_following():
    return not await following()


async def tracking():
    return (await call("GET", "/api/follow"))[1]["state"] == "tracking"


async def start_following(page):
    """Learn the ball in the middle of the picture and start following it."""
    viewer = Viewer()
    code, body = await call("POST", "/api/follow/learn")
    if code != 200:
        return viewer, "learn failed: %s %s" % (code, body)
    await asyncio.sleep(0.4)
    await page.send({"follow": True})
    if await until(following, 1.5):
        return viewer, None
    return viewer, "follow did not start: %s" % [m for m in page.messages if m.get("message")][-1:]


async def ended(reason):
    return (await call("GET", "/api/follow"))[1]["ended"] == reason


async def main():
    models = os.path.join(ROOT, "models")
    if not os.path.exists(os.path.join(models, "nanotrack_head_sim.onnx")):
        print("SKIP: run deploy/fetch_models.sh first")
        return 0
    objects = tempfile.mkdtemp(prefix="picar_e2e_")
    camera_only = "--camera-only" in sys.argv  # the server is started with PICAR_FOLLOW_DRIVE=0
    env = dict(os.environ, PICAR_OBJECTS=objects, PICAR_FOLLOW_DRIVE="0" if camera_only else "1")  # driving has to be switched on
    server = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "mock_server.py"), str(PORT)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                http("GET", "/api/follow")
                break
            except Exception:
                time.sleep(0.25)

        # Not learned yet: follow mode must refuse.
        page = Page()
        await page.open()
        await page.send({"follow": True})
        await asyncio.sleep(0.5)
        refused = [m for m in page.messages if m.get("type") == "follow" and m.get("message")]
        check("refuses to start without an object", not await following() and bool(refused),
              refused[-1]["message"] if refused else "no message")

        viewer, problem = await start_following(page)
        check("learn, then follow starts", problem is None, problem or "")
        st = await status()
        check("object has colour (tier)", st["follow"]["tier"] == "colour", str(st["follow"]["tier"]))
        code, _ = await call("POST", "/api/follow/learn")
        check("learn while following is refused (409)", code == 409, str(code))

        # Follow for a few seconds and watch what it commands.
        limits = st["limits"]["pan"]
        samples = []
        for _ in range(70):
            samples.append(await status())
            await asyncio.sleep(0.1)
        throttles = [s["throttle"] for s in samples]
        pans = [s["pan"] for s in samples]
        cap = 0.2875  # 30 % duty with the default min and max speed
        check("still following after 7 s", samples[-1]["following"], samples[-1]["follow"]["ended"])
        if camera_only:
            check("camera only: never drives or steers", all(t == 0 for t in throttles) and all(s["steer"] == 0 for s in samples),
                  "max throttle %.3f" % max(throttles))
        else:
            check("it drives when the ball gets smaller (farther away)", max(throttles) > 0.09, "max throttle %.3f" % max(throttles))
        check("throttle stays within 0 and the 30 % duty cap", all(0 <= t <= cap + 1e-6 for t in throttles),
              "max %.3f, cap %.3f" % (max(throttles), cap))
        check("steering stays within -1 and 1", all(-1 <= s["steer"] <= 1 for s in samples))
        check("camera follows the ball inside its limits",
              max(pans) - min(pans) > 4 and min(pans) >= limits[0] and max(pans) <= limits[1],
              "pan %.0f..%.0f, limits %s" % (min(pans), max(pans), limits))

        # Every way of ending follow mode.
        await page.send({"steer": 0.4})
        ok = await until(not_following, 0.5)
        check("a stick movement takes over", ok and await ended("manual control"))

        await page.send({"follow": True})
        await until(following, 1.5)
        await page.send({"follow": False})
        ok = await until(not_following, 0.5)
        check("the stop message ends it", ok and await ended("stopped"))

        await page.send({"follow": True})
        await until(following, 1.5)
        page.silent = True
        started = time.monotonic()
        ok = await until(not_following, 1.0)
        waited = time.monotonic() - started
        st = await status()
        check("a silent page stops the car within 0.6 s", ok and waited < 0.6 and st["throttle"] == 0,
              "%.2f s, throttle %.2f, ended: %s" % (waited, st["throttle"], st["follow"]["ended"]))
        page.silent = False

        await page.send({"follow": True})
        await until(following, 1.5)
        viewer.close()  # the camera stops, so no more frames arrive
        started = time.monotonic()
        ok = await until(not_following, 1.5)
        waited = time.monotonic() - started
        st = await status()
        check("a stopped camera stops the car within 0.8 s", ok and waited < 0.8 and st["throttle"] == 0,
              "%.2f s, ended: %s" % (waited, st["follow"]["ended"]))

        viewer, problem = await start_following(page)
        check("follow starts again after the camera restarts", problem is None, problem or "")
        other = Page()
        await other.open()  # a second device takes over the controls
        ok = await until(not_following, 0.5)
        check("another device taking over ends it", ok and (await status())["throttle"] == 0)
        await other.close()
        await page.close()

        # Saved objects.
        page = Page()
        await page.open()
        await call("POST", "/api/follow/learn")
        await asyncio.sleep(0.2)
        code, listing = await call("POST", "/api/objects", {"name": "orange ball"})
        check("save an object", code == 200 and listing == ["orange ball"], "%s %s" % (code, listing))
        code, jpg = await call("GET", "/api/objects/orange%20ball/thumbnail")
        check("its picture is a JPEG", code == 200 and jpg[:2] == b"\xff\xd8", str(code))
        code, _ = await call("POST", "/api/objects", {"name": "../evil"})
        check("a bad name is refused", code == 422, str(code))
        await call("DELETE", "/api/follow")
        st = (await call("GET", "/api/follow"))[1]
        check("forget clears the object", not st["learned"] and st["state"] == "idle")
        code, _ = await call("POST", "/api/objects/orange%20ball/load")
        found = await until(tracking, 4)
        check("a loaded object is found again", code == 200 and found, str(code))
        await page.send({"follow": True})
        check("and followed", await until(following, 1.5))
        await page.send({"follow": False})
        code, listing = await call("DELETE", "/api/objects/orange%20ball")
        check("delete an object", code == 200 and listing == [], "%s %s" % (code, listing))
        code, _ = await call("DELETE", "/api/objects/orange%20ball")
        check("deleting it again gives 404", code == 404, str(code))
        await page.close()
        viewer.close()
    finally:
        server.terminate()
        server.wait(10)
    print("PASS" if all(RESULTS) else "FAIL")
    return 0 if all(RESULTS) else 1


sys.exit(asyncio.run(main()))
