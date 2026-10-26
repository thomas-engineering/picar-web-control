"""Follow mode: tracks the learned object in the camera frames and drives the car after it.

The tracker runs in its own thread on the newest frame. Its results are handed to the event loop, which
runs the control law and talks to the hardware, so all hardware calls stay on one thread. If the
tracker thread stalls, Controller's watchdog stops the car.
"""
import asyncio
import concurrent.futures
import logging
import os
import re
import threading
import time

import cv2
import numpy as np

from .follow import NANO_MODELS, ObjectTracker, Track, draw, draw_reticle, reticle_box
from .follow_control import FollowControl

log = logging.getLogger("picar.follow")

SPEED_CAP_DUTY = 30  # percent motor duty; follow mode never drives faster
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")
THUMBNAIL = 96  # pixels, longest side of a saved object's picture
TRACKER_NICE = 10  # scheduling niceness of the tracker thread
JOB_TIMEOUT = 6.0  # seconds to wait for the tracker thread to take a job; the camera needs a few to start


class Busy(Exception):
    """The request is not possible while follow mode drives the car."""


class Follower:
    def __init__(self, controller, loop, models_dir, objects_dir):
        self.ctl = controller
        self.loop = loop
        self.objects_dir = objects_dir
        self.models = all(os.path.exists(os.path.join(models_dir, n)) for n in NANO_MODELS)
        self.tracker = ObjectTracker(models_dir)
        self.control = None
        # Until PICAR_FOLLOW_DRIVE=1 is set, steering and throttle stay at zero and only the camera follows
        # the object: the pan and tilt directions must be checked that way before the car may drive.
        self.drive = os.environ.get("PICAR_FOLLOW_DRIVE") == "1"
        self.latest = Track("idle")
        self.latest_time = 0.0
        self.name = None  # name of the saved object in use
        self.ended = ""  # why follow mode last ended
        self.rate = 0.0  # tracker results per second, smoothed
        self.delay = 0.0  # seconds from the camera capturing a frame to its result reaching the control law
        self.thumbnail = None
        controller.on_follow_end = self._ended
        cv2.setNumThreads(int(os.environ.get("PICAR_TRACKER_THREADS", "3")))  # the Pi 3 has 4 cores
        self._cond = threading.Condition()
        self._frame = None
        self._stamp = 0.0
        self._seq = 0
        self._jobs = []
        self._wants_frames = False
        self._shape = None  # frame shape the object was learned at
        self._closed = False
        self._event = None  # last (state, reason) that was logged
        self._info_time = 0.0
        self._thread = threading.Thread(target=self._run, name="tracker", daemon=True)
        self._thread.start()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify()

    # ---- camera thread ----

    def offer(self, frame, stamp):
        """Hand over a captured frame; it is copied, because the camera thread draws on it afterwards."""
        if not (self._wants_frames or self._jobs):
            return
        with self._cond:
            self._frame, self._stamp = frame.copy(), stamp
            self._seq += 1
            self._cond.notify()

    def overlay(self, frame):
        track = self.latest
        if track.state == "idle":
            draw_reticle(frame)
        else:
            draw(frame, track)

    # ---- tracker thread ----

    def _run(self):
        # Lower priority, so that the event loop, the video encoder and the Wi-Fi driver win the CPU
        # when the Pi is busy; OpenCV's worker threads inherit this when they are started from here.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), TRACKER_NICE)
        except OSError:
            log.warning("cannot lower the tracker priority")
        seen = 0
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._closed or (self._seq != seen and (self._jobs or self._wants_frames)))
                if self._closed:
                    return
                frame, stamp, seen = self._frame, self._stamp, self._seq
                jobs, self._jobs = self._jobs, []
            try:
                for job, future in jobs:
                    if future.set_running_or_notify_cancel():
                        try:
                            future.set_result(job(frame))
                        except Exception as error:
                            future.set_exception(error)
                if jobs or not self._wants_frames:
                    continue
                if self._shape is None:
                    self._shape = frame.shape
                if frame.shape != self._shape:  # the resolution changed: the learned object no longer fits
                    self._reset()
                    self.loop.call_soon_threadsafe(self.ctl.stop_following, "camera size changed")
                    continue
                began = time.monotonic()
                previous, self.latest, self.latest_time = self.latest_time, self.tracker.update(frame), stamp
                took = time.monotonic() - began
                if took > 0.2:
                    log.warning("slow tracker step: %.0f ms (%s, %s)", took * 1000, self.latest.state, self.latest.reason or "-")
                if previous and stamp > previous:
                    self.rate += 0.1 * (1 / (stamp - previous) - self.rate)
                event = (self.latest.state, self.latest.reason)
                if self.latest.state == "lost" and self.tracker.search_info and stamp - self._info_time > 2.0:
                    self._info_time = stamp
                    log.info("searching: %s", self.tracker.search_info)
                if event != self._event:
                    self._event = event
                    log.info("tracker: %s %s (%s object, confidence %.2f)", event[0], event[1] or "-", self.tracker.tier,
                             self.latest.confidence)
                self.loop.call_soon_threadsafe(self._step, self.latest, frame.shape, stamp)
            except Exception:
                log.exception("tracker failed")
                self._reset()
                self.loop.call_soon_threadsafe(self.ctl.stop_following, "tracker error")

    def _reset(self):
        self.tracker.reset()
        self.latest, self._wants_frames, self._shape = Track("idle"), False, None
        self.name = self.thumbnail = None

    async def _submit(self, job):
        """Run job(frame) in the tracker thread on the next camera frame."""
        future = concurrent.futures.Future()
        with self._cond:
            self._jobs.append((job, future))
            self._cond.notify()
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), JOB_TIMEOUT)
        except asyncio.TimeoutError:
            if not future.cancel():  # the tracker thread had just started it: let it finish
                return await asyncio.wrap_future(future)
            raise ValueError("the camera is not delivering frames") from None

    # ---- event loop ----

    def _step(self, track, shape, stamp):
        ctl = self.ctl
        now = time.monotonic()
        self.delay += 0.1 * (now - stamp - self.delay)
        if not ctl.following:
            return
        try:
            command = self.control.update(track, (shape[1], shape[0]), now - stamp, ctl.pan, ctl.tilt, now)
            if command.mode == "gave_up":
                ctl.halt("target lost")
            elif self.drive:
                ctl.follow_step(command.steer, command.throttle, command.pan_rate, command.tilt_rate)
            else:
                ctl.follow_step(0.0, 0.0, command.pan_rate, command.tilt_rate)
        except Exception:
            log.exception("follow step failed")
            ctl.halt("error")

    def _ended(self, reason):
        self.ended = reason

    def start(self):
        """Start driving after the object. Returns None, or why it cannot."""
        if self.ctl.following:
            return None
        if time.monotonic() - self.latest_time > 0.3 or self.latest.state != "tracking":
            return "the object is not in view"
        if self.latest.confidence < FollowControl.MIN_CONFIDENCE:
            return "the object is not clearly in view"
        cal = self.ctl.hw.cal
        cap = FollowControl.throttle_for_duty(SPEED_CAP_DUTY, cal.min_speed, cal.max_speed)
        self.control = FollowControl((cal.pan_min, cal.pan_max), cap, (cal.tilt_min, cal.tilt_max))
        self.ended = ""
        self.ctl.start_following()
        return None

    def _guard(self):
        if self.ctl.following:
            raise Busy("stop following first")

    async def learn(self):
        """Learn what is inside the reticle."""
        self._guard()

        def job(frame):
            height, width = frame.shape[:2]
            box = reticle_box(width, height)
            track = self.tracker.learn(frame, box)
            x, y, w, h = box
            crop = frame[y:y + h, x:x + w]
            scale = THUMBNAIL / max(w, h)
            ok, jpg = cv2.imencode(".jpg", cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA))
            self.thumbnail = jpg.tobytes() if ok else None
            self.name, self._shape, self._wants_frames, self.ended = None, frame.shape, True, ""
            self.latest, self.latest_time = track, time.monotonic()

        await self._submit(job)

    async def forget(self):
        self.ctl.stop_following("forgotten")

        def job(frame):
            self._reset()
            self.ended = ""

        await self._submit(job)

    # ---- saved objects ----

    def _path(self, name, extension):
        if not NAME_PATTERN.match(name):
            raise ValueError("use 1 to 32 letters, digits, spaces, - or _")
        return os.path.join(self.objects_dir, name + extension)

    def list_objects(self):
        try:
            names = sorted(f[:-4] for f in os.listdir(self.objects_dir) if f.endswith(".npz"))
        except FileNotFoundError:
            return []
        return [n for n in names if NAME_PATTERN.match(n)]

    def thumbnail_of(self, name):
        with open(self._path(name, ".jpg"), "rb") as f:
            return f.read()

    async def save(self, name):
        """Save the learned object, which needs colour, under name."""
        self._guard()
        path = self._path(name, ".npz")

        def job(frame):
            profile = self.tracker.export()
            os.makedirs(self.objects_dir, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                np.savez(f, **{k: np.asarray(v) for k, v in profile.items()})
            os.replace(path + ".tmp", path)
            if self.thumbnail:
                with open(self._path(name, ".jpg"), "wb") as f:
                    f.write(self.thumbnail)
            self.name = name

        await self._submit(job)

    async def load(self, name):
        """Use a saved object: the car looks for it and tracks it once it is found."""
        self._guard()
        path = self._path(name, ".npz")
        if not os.path.exists(path):
            raise FileNotFoundError(name)

        def job(frame):
            with np.load(path, allow_pickle=False) as data:
                profile = {k: data[k] for k in data.files}
            profile["version"] = int(profile.get("version", 0))
            self._reset()
            self.tracker.load(profile)
            try:
                self.thumbnail = self.thumbnail_of(name)
            except OSError:
                self.thumbnail = None
            self.name, self._shape, self._wants_frames, self.ended = name, frame.shape, True, ""
            self.latest, self.latest_time = Track("lost", reason="searching"), time.monotonic()

        await self._submit(job)

    def delete(self, name):
        for extension in (".npz", ".jpg"):
            try:
                os.remove(self._path(name, extension))
            except FileNotFoundError:
                if extension == ".npz":
                    raise

    def status(self):
        track = self.latest
        return {
            "learned": self._wants_frames,
            "state": track.state,
            "reason": track.reason,
            "confidence": round(track.confidence, 2),
            "tier": self.tracker.tier,
            "name": self.name,
            "following": self.ctl.following,
            "ended": self.ended,
            "models": self.models,
            "speed_cap_duty": SPEED_CAP_DUTY,
            "drive": self.drive,
            "tracker_fps": round(self.rate, 1),
            "tracker_delay_ms": round(self.delay * 1000),
        }
