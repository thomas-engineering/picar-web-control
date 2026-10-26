#!/usr/bin/env python3
"""Run the real server on a development machine, with fake hardware and a synthetic camera.

    python -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]" opencv-contrib-python-headless==4.10.0.84 "numpy<2.3" websockets
    deploy/fetch_models.sh                                # for follow mode
    .venv/bin/python tools/mock_server.py [port]          # then open http://localhost:8765

Servo and motor calls are discarded and the "camera" draws the moving balls of tools/synthetic_scene.py. The calibration is
kept in a temporary file, and so are the saved objects, so the Pi's data/ folder is never touched.
"""
import os
import sys
import tempfile
import time
import types

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synthetic_scene import render  # noqa: E402

# The Pi-only modules are replaced before the server imports them.
sys.modules["smbus2"] = types.SimpleNamespace(
    SMBus=lambda bus: types.SimpleNamespace(
        write_byte_data=lambda *a: None, read_byte_data=lambda *a: 0,
        write_i2c_block_data=lambda *a: None, close=lambda: None,
    )
)


class FakePin:
    value = 0

    def __init__(self, *args):
        pass

    def close(self):
        pass


sys.modules["gpiozero"] = types.SimpleNamespace(OutputDevice=FakePin)


class FakeCapture:
    def __init__(self, *args):
        self.frame = 0
        self.size = (320, 240)

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self.size = (int(value), self.size[1])
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self.size = (self.size[0], int(value))

    def isOpened(self):
        return True

    def release(self):
        pass

    def read(self):
        time.sleep(1 / 15)
        width, height = self.size
        # The ball waits in the middle for 30 frames, so that Learn can capture it, and from frame 45 on
        # it shrinks to half its size over 4 seconds, as if it walked away.
        shrink = max(0.5, 1 - (self.frame - 45) / 120)
        image, _ = render(max(self.frame - 30, 0), self.size, shrink)
        cv2.putText(image, "MOCK %dx%d" % self.size, (10, height - 12), cv2.FONT_HERSHEY_SIMPLEX, height / 300, (255, 255, 255), 2)
        self.frame += 1
        return True, image


import server.camera  # noqa: E402
import server.hardware  # noqa: E402

server.camera.cv2.VideoCapture = FakeCapture
os.environ.setdefault("PICAR_OBJECTS", os.path.join(tempfile.gettempdir(), "picar_mock_objects"))
calibration = os.path.join(tempfile.gettempdir(), "picar_mock_calibration.json")
# CALIBRATION_PATH is bound as a default argument, so the defaults are patched.
server.hardware.Calibration.load.__func__.__defaults__ = (calibration,)
server.hardware.Calibration.save.__defaults__ = (calibration,)

import uvicorn  # noqa: E402

uvicorn.run("server.app:app", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765, log_level="warning")
