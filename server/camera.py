"""USB webcam to MJPEG. The camera only runs while at least one viewer is connected."""
import asyncio
import logging
import threading
import time

import cv2

log = logging.getLogger("picar.camera")


class Camera:
    # Byte budget of each viewer's video stream, in bytes per second. It shrinks when the client cannot
    # keep up (a weak Wi-Fi link) and grows back slowly, so video never queues up in front of
    # the control messages. Frames over the budget are dropped, never delayed.
    START_BUDGET = 150 * 1024
    MIN_BUDGET = 25 * 1024
    MAX_BUDGET = 300 * 1024
    RECOVERY = 20 * 1024  # bytes per second gained per second of smooth sending
    SLOW_WRITE = 0.05  # seconds; accepting one frame took longer than this: the client is behind

    def __init__(self, device="/dev/video0", width=640, height=480, fps=30, quality=70):
        self.device, self.width, self.height, self.fps = device, width, height, fps
        self.quality = quality
        self._cond = threading.Condition()
        self._viewers = 0
        self._thread = None
        self._frame = None
        self._seq = 0
        self.streams = []  # one {"budget"} per connected viewer, for /api/status
        self._generation = 0  # bumped when the resolution changes, so the capture thread reopens

    def set_resolution(self, width, height):
        with self._cond:
            if (width, height) != (self.width, self.height):
                self.width, self.height = width, height
                self._generation += 1

    def _open(self):
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep latency low: never queue old frames
        return cap

    def _run(self):
        cap = None
        generation = self._generation
        params = [cv2.IMWRITE_JPEG_QUALITY, self.quality]
        while True:
            with self._cond:
                if self._viewers == 0:
                    self._thread = None
                    break
                if generation != self._generation:
                    generation = self._generation
                    if cap is not None:
                        cap.release()
                        cap = None
            if cap is None:
                cap = self._open()
                if not cap.isOpened():
                    log.warning("cannot open %s, retrying", self.device)
                    cap.release()
                    cap = None
                    time.sleep(2)
                    continue
            ok, img = cap.read()
            if not ok:
                log.warning("frame read failed, reopening camera")
                cap.release()
                cap = None
                time.sleep(0.5)
                continue
            ok, jpg = cv2.imencode(".jpg", img, params)
            if not ok:
                continue
            with self._cond:
                self._frame = jpg.tobytes()
                self._seq += 1
                self._cond.notify_all()
        if cap is not None:
            cap.release()

    def acquire(self):
        with self._cond:
            self._viewers += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
                self._thread.start()

    def release(self):
        with self._cond:
            self._viewers -= 1

    @property
    def viewers(self):
        return self._viewers

    def _wait_frame(self, last_seq, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout)
            return self._frame, self._seq

    async def mjpeg(self):
        """Async generator of multipart/x-mixed-replace chunks. Slow clients skip frames."""
        self.acquire()
        stream = {"budget": self.START_BUDGET}
        self.streams.append(stream)
        try:
            seq = 0
            next_allowed = last_sent = time.monotonic()
            while True:
                # Wait out the byte budget first, then take the newest frame, not a stale one.
                delay = next_allowed - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                frame, new_seq = await asyncio.to_thread(self._wait_frame, seq, 2.0)
                if frame is None or new_seq == seq:
                    continue
                seq = new_seq
                chunk = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                start = time.monotonic()
                yield chunk
                now = time.monotonic()  # the server only resumes us once the client took the chunk
                if now - start > self.SLOW_WRITE:
                    stream["budget"] = max(self.MIN_BUDGET, stream["budget"] * 0.6)
                else:
                    stream["budget"] = min(self.MAX_BUDGET, stream["budget"] + self.RECOVERY * (now - last_sent))
                last_sent = now
                next_allowed = start + len(chunk) / stream["budget"]
        finally:
            self.streams.remove(stream)
            self.release()
