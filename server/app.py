"""Web server: static UI, WebSocket control (/ws) and MJPEG video (/video).

Run: uvicorn server.app:app --host 0.0.0.0 --port 8000
There is no authentication; only run it on a network you trust.
"""
import asyncio
import contextlib
import dataclasses
import json
import logging
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .camera import Camera
from .control import Controller
from .follower import Busy, Follower
from .hardware import Hardware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("picar")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT_DIR, "web")


def _size(text):
    width, height = text.split("x")
    return int(width), int(height)


@contextlib.asynccontextmanager
async def lifespan(app):
    hw = Hardware()
    app.state.controller = Controller(hw)
    app.state.default_resolution = _size(os.environ.get("PICAR_RESOLUTION", "320x240"))
    app.state.max_resolution = _size(os.environ.get("PICAR_MAX_RESOLUTION", "640x480"))
    app.state.max_camera = False
    width, height = app.state.default_resolution
    app.state.camera = Camera(
        device=os.environ.get("PICAR_CAMERA", "/dev/video0"),
        width=width,
        height=height,
        quality=int(os.environ.get("PICAR_QUALITY", "55")),
    )
    app.state.active_ws = None
    follower = Follower(
        app.state.controller,
        asyncio.get_running_loop(),
        models_dir=os.environ.get("PICAR_MODELS", os.path.join(ROOT_DIR, "models")),
        objects_dir=os.environ.get("PICAR_OBJECTS", os.path.join(ROOT_DIR, "data", "objects")),
    )
    app.state.follower = follower
    app.state.camera.on_frame = follower.offer
    app.state.camera.overlay = follower.overlay
    task = asyncio.create_task(app.state.controller.run())
    log.info("ready")
    try:
        yield
    finally:
        task.cancel()
        app.state.controller.halt("shutdown")
        follower.close()
        hw.close()  # stops the motors


app = FastAPI(lifespan=lifespan)


class CameraUpdate(BaseModel):
    max: bool


class ObjectName(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class CalibrationUpdate(BaseModel):
    """Fields the calibration page may change; absent fields keep their value."""

    steering_trim: float | None = Field(None, ge=-30, le=30)
    pan_trim: float | None = Field(None, ge=-30, le=30)
    tilt_trim: float | None = Field(None, ge=-30, le=30)
    steering_range: float | None = Field(None, ge=5, le=60)
    pan_min: float | None = Field(None, ge=0, le=180)
    pan_max: float | None = Field(None, ge=0, le=180)
    tilt_min: float | None = Field(None, ge=0, le=180)
    tilt_max: float | None = Field(None, ge=0, le=180)
    invert_left: bool | None = None
    invert_right: bool | None = None
    min_speed: int | None = Field(None, ge=0, le=100)
    max_speed: int | None = Field(None, ge=0, le=100)


@app.get("/api/status")
def status():
    cam = app.state.camera
    return {
        **app.state.controller.status(),
        "viewers": cam.viewers,
        "follow": app.state.follower.status(),
        "video": [
            {"budget_kib_s": round(st["budget"] / 1024)}
            for st in cam.streams
        ],
    }


def camera_info():
    cam = app.state.camera
    return {
        "max": app.state.max_camera,
        "width": cam.width,
        "height": cam.height,
        "default_resolution": "%dx%d" % app.state.default_resolution,
        "max_resolution": "%dx%d" % app.state.max_resolution,
    }


@app.get("/api/camera")
def get_camera():
    return camera_info()


@app.post("/api/camera")
def set_camera(update: CameraUpdate):
    """Switch between the default and the maximum resolution; it is not saved across restarts."""
    app.state.max_camera = update.max
    app.state.camera.set_resolution(
        *(app.state.max_resolution if update.max else app.state.default_resolution)
    )
    return camera_info()


async def follow_request(call, *args):
    """Run a follower action and turn its failures into HTTP errors."""
    try:
        await call(*args)
    except Busy as error:
        raise HTTPException(409, str(error))
    except FileNotFoundError:
        raise HTTPException(404, "no such object")
    except ValueError as error:
        raise HTTPException(422, str(error))
    return app.state.follower.status()


@app.get("/api/follow")
def get_follow():
    return app.state.follower.status()


@app.post("/api/follow/learn")
async def learn_object():
    """Learn what is inside the reticle in the middle of the picture."""
    return await follow_request(app.state.follower.learn)


@app.delete("/api/follow")
async def forget_object():
    return await follow_request(app.state.follower.forget)


@app.get("/api/objects")
def list_objects():
    return app.state.follower.list_objects()


@app.post("/api/objects")
async def save_object(body: ObjectName):
    await follow_request(app.state.follower.save, body.name)
    return app.state.follower.list_objects()


@app.post("/api/objects/{name}/load")
async def load_object(name: str):
    return await follow_request(app.state.follower.load, name)


@app.delete("/api/objects/{name}")
def delete_object(name: str):
    try:
        app.state.follower.delete(name)
    except FileNotFoundError:
        raise HTTPException(404, "no such object")
    except ValueError as error:
        raise HTTPException(422, str(error))
    return app.state.follower.list_objects()


@app.get("/api/objects/{name}/thumbnail")
def object_thumbnail(name: str):
    try:
        return Response(app.state.follower.thumbnail_of(name), media_type="image/jpeg")
    except (OSError, ValueError):
        raise HTTPException(404, "no such object")


@app.get("/api/calibration")
def get_calibration():
    return dataclasses.asdict(app.state.controller.hw.cal)


@app.post("/api/calibration")
def set_calibration(update: CalibrationUpdate):
    ctl = app.state.controller
    changes = update.model_dump(exclude_none=True)
    new = dataclasses.replace(ctl.hw.cal, **changes)
    if new.pan_min >= new.pan_max or new.tilt_min >= new.tilt_max:
        raise HTTPException(422, "a minimum must be below its maximum")
    if new.min_speed > new.max_speed:
        raise HTTPException(422, "min_speed must not exceed max_speed")
    for name, value in changes.items():
        setattr(ctl.hw.cal, name, value)
    ctl.hw.cal.save()
    ctl.apply_calibration()
    return dataclasses.asdict(ctl.hw.cal)


@app.get("/video")
async def video():
    return StreamingResponse(
        app.state.camera.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws")
async def control(ws: WebSocket):
    await ws.accept()
    ctl = app.state.controller
    follower = app.state.follower
    # A new connection (for example the phone reconnecting) takes over from the old one.
    previous = app.state.active_ws
    app.state.active_ws = ws
    if previous is not None:
        with contextlib.suppress(Exception):
            await previous.close(code=4000)
    ctl.halt()
    send_lock = asyncio.Lock()  # the state pusher and the pong replies share this socket

    async def send(message):
        async with send_lock:
            await ws.send_json(message)

    async def push_state():
        """Report the camera position when it moves, so the UI follows rate-driven movement."""
        last = last_follow = None
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(0.2)
                position = (round(ctl.pan, 1), round(ctl.tilt, 1))
                if position != last:
                    last = position
                    await send({"type": "state", **ctl.status()})
                status = follower.status()
                key = {**status, "confidence": round(status["confidence"], 1)}
                if key != last_follow:
                    last_follow = key
                    await send({"type": "follow", **status})

    pusher = asyncio.create_task(push_state())
    try:
        # Tell the client where the camera really is and its limits, so the sliders match.
        await send({"type": "state", **ctl.status()})
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            ctl.command(msg)
            if isinstance(msg, dict) and "follow" in msg:
                if msg["follow"] is True:
                    problem = follower.start()
                    if problem:
                        await send({"type": "follow", **follower.status(), "message": problem})
                elif msg["follow"] is False:
                    ctl.stop_following("stopped")
            # The client times this round trip to show its connection latency.
            if isinstance(msg, dict) and "ping" in msg:
                await send({"type": "pong", "t": msg["ping"]})
    except WebSocketDisconnect:
        pass
    finally:
        pusher.cancel()
        if app.state.active_ws is ws:
            app.state.active_ws = None
            ctl.halt()


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
