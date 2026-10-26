"""Turns client commands into hardware calls, and stops the car when commands stop arriving."""
import asyncio
import collections
import logging
import math
import time

log = logging.getLogger("picar.control")


def _num(value, lo, hi):
    """Parse a client-supplied number; None if it is missing or not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(lo, min(hi, float(value)))


class Controller:
    TICK = 1 / 30
    TIMEOUT = 0.3  # seconds without a command before the car is stopped
    CAMERA_SPEED = 90.0  # degrees per second at full pan/tilt rate

    def __init__(self, hw):
        self.hw = hw
        self.steer = 0.0
        self.throttle = 0.0
        self.pan = 90.0
        self.tilt = 90.0
        self.pan_rate = 0.0
        self.tilt_rate = 0.0
        self.last_command = 0.0
        # Follow mode drives the car itself: the page's own drive fields are ignored while it runs, and
        # it only goes on while both its own steps and the page's messages keep arriving.
        self.following = False
        self.last_follow = 0.0
        self.last_client = 0.0
        self._follow_gaps = collections.deque(maxlen=400)  # (time, gap in seconds) of follow steps and of page messages
        self._client_gaps = collections.deque(maxlen=1000)
        self.on_follow_end = None  # called with a reason when follow mode ends
        # (arrival time, seconds since the previous message), to spot network stalls
        self._gaps = collections.deque(maxlen=1000)

    def _worst_gap(self, gaps, window=10.0):
        now = time.monotonic()
        recent = [g for t, g in gaps if now - t <= window]
        return round(max(recent) * 1000) if recent else None

    def _gap_stats(self, window=10.0):
        now = time.monotonic()
        gaps = [g for t, g in self._gaps if now - t <= window]
        return {
            "window_s": window,
            "messages": len(gaps),
            "max_ms": round(max(gaps) * 1000) if gaps else None,
            "over_100ms": sum(g > 0.1 for g in gaps),
            "over_300ms": sum(g > self.TIMEOUT for g in gaps),
        }

    def command(self, msg):
        """Apply one message: {steer, throttle, pan, tilt, pan_rate, tilt_rate, center_camera}.

        pan/tilt are absolute angles in degrees (90 is centre); pan_rate/tilt_rate move the
        camera continuously instead. Fields that are absent keep their value; the car still
        stops if no message arrives.
        """
        if not isinstance(msg, dict):
            return
        now = time.monotonic()
        if self.following and self.last_client:
            self._client_gaps.append((now, now - self.last_client))
        self.last_client = now
        if self.following:
            if not self._manual(msg):
                return  # the idle messages of the page must not count as commands
            self.halt("manual control")  # touching a stick or the camera takes over
        if self.last_command and now - self.last_command < 5:  # a longer gap is just idle
            self._gaps.append((now, now - self.last_command))
        steer = _num(msg.get("steer"), -1, 1)
        if steer is not None:
            self._set_steer(steer)
        throttle = _num(msg.get("throttle"), -1, 1)
        if throttle is not None:
            self._set_throttle(throttle)
        c = self.hw.cal
        pan = _num(msg.get("pan"), 0, 180)
        tilt = _num(msg.get("tilt"), 0, 180)
        if pan is not None:
            pan = max(c.pan_min, min(c.pan_max, pan))
        if tilt is not None:
            tilt = max(c.tilt_min, min(c.tilt_max, tilt))
        if (pan is not None and pan != self.pan) or (tilt is not None and tilt != self.tilt):
            self.pan = self.pan if pan is None else pan
            self.tilt = self.tilt if tilt is None else tilt
            self.hw.pan_tilt(self.pan, self.tilt)
        rate = _num(msg.get("pan_rate"), -1, 1)
        if rate is not None:
            self.pan_rate = rate
        rate = _num(msg.get("tilt_rate"), -1, 1)
        if rate is not None:
            self.tilt_rate = rate
        if msg.get("center_camera") is True:
            self.pan = self.tilt = 90.0
            self.hw.pan_tilt(self.pan, self.tilt)
        self.last_command = time.monotonic()

    @staticmethod
    def _manual(msg):
        """Whether a message asks for any movement, as opposed to the page's idle zeros."""
        for field in ("steer", "throttle", "pan_rate", "tilt_rate"):
            if _num(msg.get(field), -1, 1):
                return True
        return any(_num(msg.get(f), 0, 180) is not None for f in ("pan", "tilt")) or msg.get("center_camera") is True

    def _set_steer(self, value):
        if value != self.steer:
            self.steer = value
            self.hw.steer(value)

    def _set_throttle(self, value):
        if value != self.throttle:
            self.throttle = value
            self.hw.drive(value)

    def start_following(self):
        self.following = True
        self.last_follow = time.monotonic()

    def follow_step(self, steer, throttle, pan_rate, tilt_rate):
        """One step of follow mode; it also keeps follow mode's own watchdog alive."""
        if not self.following:
            return
        self._set_steer(steer)
        self._set_throttle(throttle)
        self.pan_rate, self.tilt_rate = pan_rate, tilt_rate
        now = time.monotonic()
        self._follow_gaps.append((now, now - self.last_follow))
        self.last_follow = now

    def stop_following(self, reason):
        if self.following:
            self.halt(reason)

    def halt(self, reason="halt"):
        """Stop driving and camera movement; steering and camera position are left as they are."""
        if self.following:
            self.following = False
            log.info("follow mode ended: %s", reason)
            if self.on_follow_end:
                self.on_follow_end(reason)
        self.throttle = 0.0
        self.pan_rate = self.tilt_rate = 0.0
        self.hw.stop()

    def apply_calibration(self):
        """Move the servos to where the current calibration puts the last commanded position."""
        c = self.hw.cal
        self.pan = max(c.pan_min, min(c.pan_max, self.pan))
        self.tilt = max(c.tilt_min, min(c.tilt_max, self.tilt))
        self.hw.steer(self.steer)
        self.hw.pan_tilt(self.pan, self.tilt)

    def _move_camera(self, dt):
        if not (self.pan_rate or self.tilt_rate):
            return
        c = self.hw.cal
        self.pan = max(c.pan_min, min(c.pan_max, self.pan + self.pan_rate * self.CAMERA_SPEED * dt))
        self.tilt = max(c.tilt_min, min(c.tilt_max, self.tilt + self.tilt_rate * self.CAMERA_SPEED * dt))
        self.hw.pan_tilt(self.pan, self.tilt)

    async def run(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.TICK)
            now = time.monotonic()
            if self.following:
                if now - self.last_follow > self.TIMEOUT or now - self.last_client > self.TIMEOUT:
                    log.warning("follow mode lost its input (tracker silent %.2f s, page silent %.2f s), stopping",
                                now - self.last_follow, now - self.last_client)
                    self.halt("no input")
            elif now - self.last_command > self.TIMEOUT and (
                self.throttle or self.pan_rate or self.tilt_rate
            ):
                log.warning("no command for %.2fs, stopping", self.TIMEOUT)
                self.halt()
            self._move_camera(now - last)
            last = now

    def status(self):
        c = self.hw.cal
        return {
            "steer": self.steer,
            "throttle": self.throttle,
            "pan": self.pan,
            "tilt": self.tilt,
            "limits": {"pan": [c.pan_min, c.pan_max], "tilt": [c.tilt_min, c.tilt_max]},
            "command_gaps": self._gap_stats(),
            "following": self.following,
            "follow_gaps_ms": {"tracker": self._worst_gap(self._follow_gaps), "page": self._worst_gap(self._client_gaps)},
            "command_age": round(time.monotonic() - self.last_command, 2) if self.last_command else None,
        }
