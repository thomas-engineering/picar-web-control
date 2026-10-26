"""Control law of follow mode: turns the tracker's result into steering, throttle and camera rates.

It has no hardware or clock of its own, so tools/follow_sim.py can tune it against a simulated car.
Every call must be given fresh input; whenever the target is not clearly seen the car stops.
"""
import dataclasses
import math


def _clip(value, lo, hi):
    return max(lo, min(hi, value))


@dataclasses.dataclass
class Command:
    steer: float = 0.0  # -1 left .. 1 right
    throttle: float = 0.0  # never negative: follow mode does not reverse
    pan_rate: float = 0.0
    tilt_rate: float = 0.0
    mode: str = "stopped"  # "following", "stopped", "searching" or "gave_up"


class FollowControl:
    # Camera geometry. The field of view is a guess for the webcam and only scales the bearing.
    HALF_FOV = 27.0  # degrees from the image centre to its left and right edge
    # Directions follow the page's stick convention (web/app.js): the camera looks right when the pan
    # angle falls, and up when the tilt angle rises. Check both on the car, camera only, before driving.
    PAN_SIGN = -1.0
    TILT_SIGN = -1.0

    # The camera is rate driven (an integrator), so a plain P term on the pixel error is enough.
    KP_PAN = 0.8
    KP_TILT = 0.8
    IMAGE_DEADBAND = 0.05  # image error (-1..1) that counts as centred

    STEER_FULL = 30.0  # bearing in degrees that asks for full steering lock
    STEER_SMOOTHING = 0.5  # weight of the new steering value

    KP_SPEED = 0.6  # throttle per unit of distance error, in multiples of the learned distance
    START_EXCESS = 0.20  # start driving when the target is this much farther than when it was learned
    STOP_EXCESS = 0.08  # stop again when it is this close to that distance
    MIN_MOVE = 0.10  # smallest throttle while driving; the hardware deadzone is 0.05
    MAX_ACCEL = 1.5  # throttle per second
    SLOW_BEARING = 60.0  # degrees; the speed is reduced when the target is far to the side
    SIZE_SMOOTHING = 0.4

    MIN_CONFIDENCE = 0.35
    MAX_FRAME_AGE = 0.25  # seconds; older frames stop the car
    REACQUIRE_HOLD = 0.4  # seconds of steady tracking before driving after the target was lost
    SEARCH_DELAY = 0.5  # seconds without the target before the camera starts sweeping
    SEARCH_TIME = 8.0  # seconds of sweeping before giving up
    SEARCH_RATE = 0.25  # camera rate while sweeping

    def __init__(self, pan_limits=(30.0, 150.0), max_throttle=0.4):
        self.pan_limits = pan_limits
        self.max_throttle = max_throttle
        self.reset()

    def reset(self):
        self._last_time = None
        self._throttle = 0.0
        self._steer = 0.0
        self._size = None
        self._moving = False
        self._good_since = None
        self._last_good = None
        self._last_error = 0.0
        self._search_dir = 1.0
        self._searching = False

    def update(self, track, frame_size, frame_age, pan, tilt, now):
        """One control step. frame_age is how old the image behind track is, in seconds."""
        dt = 1 / 15 if self._last_time is None else _clip(now - self._last_time, 0.001, 0.2)
        self._last_time = now
        good = (
            track.state == "tracking"
            and track.confidence >= self.MIN_CONFIDENCE
            and frame_age <= self.MAX_FRAME_AGE
        )
        if not good:
            return self._unseen(frame_age, pan, now)
        if self._good_since is None:
            self._good_since = now
            self._size = None
        self._last_good = now
        self._searching = False
        width, height = frame_size
        x, y, w, h = track.box
        error_x = (x + w / 2 - width / 2) / (width / 2)  # positive: target right of the image centre
        error_y = (y + h / 2 - height / 2) / (height / 2)  # positive: below the centre
        self._last_error = error_x
        command = Command(mode="following")
        command.pan_rate = self._camera_rate(error_x, self.PAN_SIGN * self.KP_PAN)
        command.tilt_rate = self._camera_rate(error_y, self.TILT_SIGN * self.KP_TILT)

        # The camera looks at the target, so its offset from straight ahead plus the image error is
        # the bearing of the target as seen from the car.
        bearing = (90.0 - pan) + error_x * self.HALF_FOV  # degrees, positive: right
        steer = _clip(bearing / self.STEER_FULL, -1.0, 1.0)
        self._steer += self.STEER_SMOOTHING * (steer - self._steer)
        command.steer = self._steer

        self._size = track.size if self._size is None else self._size + self.SIZE_SMOOTHING * (track.size - self._size)
        command.throttle = self._speed(1 / max(self._size, 0.05) - 1, bearing, now, dt)
        return command

    def _camera_rate(self, error, gain):
        if abs(error) < self.IMAGE_DEADBAND:
            return 0.0
        return _clip(gain * error, -1.0, 1.0)

    def _speed(self, excess, bearing, now, dt):
        """Throttle from how much farther the target is than at learn time (a ratio, 0 is equal)."""
        if now - self._good_since < self.REACQUIRE_HOLD:
            target = 0.0
            self._moving = False
        else:
            if self._moving and excess < self.STOP_EXCESS:
                self._moving = False
            elif not self._moving and excess > self.START_EXCESS:
                self._moving = True
            target = 0.0
            if self._moving:
                slow = _clip(1 - abs(bearing) / self.SLOW_BEARING, 0.4, 1.0)
                target = _clip((self.MIN_MOVE + self.KP_SPEED * (excess - self.STOP_EXCESS)) * slow,
                               self.MIN_MOVE, self.max_throttle)
        if target <= 0:
            self._throttle = 0.0
        else:
            self._throttle = max(self.MIN_MOVE, min(target, self._throttle + self.MAX_ACCEL * dt))
        return self._throttle

    def _unseen(self, frame_age, pan, now):
        """The target is not clearly in view: stop at once, and sweep the camera for it after a while."""
        self._throttle = 0.0
        self._steer = 0.0
        self._moving = False
        self._good_since = None
        if self._last_good is None:
            self._last_good = now
        command = Command(mode="stopped")
        if frame_age > self.MAX_FRAME_AGE:
            return command  # the pipeline stalled: do not move the camera blind either
        gone = now - self._last_good
        if gone > self.SEARCH_DELAY + self.SEARCH_TIME:
            command.mode = "gave_up"
        elif gone > self.SEARCH_DELAY:
            low, high = self.pan_limits
            if not self._searching:  # start toward the side where the target was last seen
                self._searching = True
                self._search_dir = self.PAN_SIGN * (1.0 if self._last_error >= 0 else -1.0)
            if pan <= low + 2:
                self._search_dir = 1.0
            elif pan >= high - 2:
                self._search_dir = -1.0
            command.mode = "searching"
            command.pan_rate = self.SEARCH_RATE * self._search_dir
        return command
