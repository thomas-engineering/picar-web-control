#!/usr/bin/env python3
"""Tune and check the follow-mode control law against a simulated car. No hardware needed.

    .venv/bin/python tools/follow_sim.py [-v]

The car is a bicycle model with a rate-driven camera (pinhole, with pipeline latency and pixel
noise) that looks at a target moving on a path. The plant constants are guesses for the PiCar-V:
change them below and the checks show how much margin the gains have. Exit status 1 means a
check failed.
"""
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.follow import Track  # noqa: E402
from server.follow_control import FollowControl  # noqa: E402

WIDTH, HEIGHT = 320, 240
FOCAL = (WIDTH / 2) / math.tan(math.radians(FollowControl.HALF_FOV))
TARGET_HEIGHT = 0.12  # m
LEARN_DISTANCE = 0.6  # m, the distance at which the object was shown to the camera
REF_HEIGHT = FOCAL * TARGET_HEIGHT / LEARN_DISTANCE

FRAME_DT = 1 / 15  # one control step per camera frame
SIM_DT = 1 / 60
WHEELBASE = 0.15  # m
MAX_WHEEL_ANGLE = math.radians(30)
SPEED_PER_DUTY = 0.8  # m/s at 100 % duty
MIN_DUTY, MAX_DUTY, DEADZONE = 20, 60, 0.05  # as in the calibration and hardware.py
CAMERA_RATE = 90.0  # degrees per second at rate 1.0 (Controller.CAMERA_SPEED)
PAN_LIMITS = (30.0, 150.0)
TILT_LIMITS = (50.0, 130.0)
HALF_FOV_V = 20.0  # degrees, half the vertical field of view
SPEED_CAP_DUTY = 30  # percent, the speed cap of follow mode
MAX_THROTTLE = FollowControl.throttle_for_duty(SPEED_CAP_DUTY, MIN_DUTY, MAX_DUTY, DEADZONE)


def duty_speed(throttle):
    """Speed in m/s for a throttle, using the mapping of Hardware.drive."""
    if throttle < DEADZONE:
        return 0.0
    span = (throttle - DEADZONE) / (1 - DEADZONE)
    return SPEED_PER_DUTY * (MIN_DUTY + (MAX_DUTY - MIN_DUTY) * span) / 100


class Run:
    def __init__(self, path, hidden=lambda t: False, latency=0.15, speed_scale=1.0, noise=True, seed=1,
                 max_throttle=MAX_THROTTLE, confidence=0.9, elevation=lambda t: 0.0):
        self.path, self.hidden, self.latency, self.speed_scale = path, hidden, latency, speed_scale
        self.elevation = elevation  # degrees of the target above the level of the camera, as a function of time
        self.noise = noise
        self.confidence = confidence
        self.rng = random.Random(seed)
        self.control = FollowControl(PAN_LIMITS, max_throttle, TILT_LIMITS)
        self.max_throttle = max_throttle
        self.x = self.y = self.theta = 0.0  # theta: heading in radians, positive turns right
        self.pan = 90.0
        self.tilt = 90.0
        self.t = 0.0
        self.history = []  # (t, x, y, theta, pan, tilt)
        self.log = []  # per control step: dict

    def target(self, t):
        return self.path(max(t, 0.0))

    def measure(self):
        """The Track the camera pipeline delivers now: an image of the world `latency` ago."""
        t = self.t - self.latency
        past = min(self.history, key=lambda h: abs(h[0] - t)) if self.history else (0, 0, 0, 0, 90.0, 90.0)
        _, cx, cy, theta, pan, tilt = past
        tx, ty = self.target(t)
        dx, dy = tx - cx, ty - cy
        distance = math.hypot(dx, dy)
        psi = math.atan2(dx, dy) - (theta + math.radians(90.0 - pan))
        psi = (psi + math.pi) % (2 * math.pi) - math.pi
        depth = distance * math.cos(psi)
        up = self.elevation(t) - (tilt - 90.0)  # degrees above the optical axis
        if self.hidden(t) or depth <= 0.05 or abs(up) >= HALF_FOV_V:
            return Track("lost"), distance
        px = WIDTH / 2 + FOCAL * math.tan(psi)
        py = HEIGHT / 2 - FOCAL * math.tan(math.radians(up))
        size_px = FOCAL * TARGET_HEIGHT / depth
        if self.noise:
            px += self.rng.gauss(0, 2)
            size_px *= 1 + self.rng.gauss(0, 0.05)
        if not -size_px / 2 < px < WIDTH + size_px / 2 or size_px > HEIGHT:
            return Track("lost"), distance
        box = (int(px - size_px / 2), int(py - size_px / 2), int(size_px), int(size_px))
        return Track("tracking", box, self.confidence, size_px / REF_HEIGHT), distance

    def step(self, frame_age=None):
        track, distance = self.measure()
        age = self.latency if frame_age is None else frame_age
        command = self.control.update(track, (WIDTH, HEIGHT), age, self.pan, self.tilt, self.t)
        speed = duty_speed(command.throttle) * self.speed_scale
        wheel = command.steer * MAX_WHEEL_ANGLE
        end = self.t + FRAME_DT
        while self.t < end - 1e-9:
            self.x += speed * math.sin(self.theta) * SIM_DT
            self.y += speed * math.cos(self.theta) * SIM_DT
            self.theta += speed / WHEELBASE * math.tan(wheel) * SIM_DT
            self.pan = max(PAN_LIMITS[0], min(PAN_LIMITS[1], self.pan + command.pan_rate * CAMERA_RATE * SIM_DT))
            self.tilt = max(TILT_LIMITS[0], min(TILT_LIMITS[1], self.tilt + command.tilt_rate * CAMERA_RATE * SIM_DT))
            self.t += SIM_DT
            self.history.append((self.t, self.x, self.y, self.theta, self.pan, self.tilt))
        tx, ty = self.target(self.t)
        self.log.append({
            "t": self.t, "distance": math.hypot(tx - self.x, ty - self.y), "throttle": command.throttle,
            "steer": command.steer, "mode": command.mode, "seen": track.state == "tracking",
            "moved": speed > 0, "pan": self.pan, "tilt": self.tilt, "pan_rate": command.pan_rate,
            "in_view": self.in_view(),
        })

    def in_view(self):
        tx, ty = self.target(self.t)
        psi = math.atan2(tx - self.x, ty - self.y) - (self.theta + math.radians(90.0 - self.pan))
        psi = (psi + math.pi) % (2 * math.pi) - math.pi
        return abs(math.degrees(psi)) < FollowControl.HALF_FOV

    def run(self, seconds):
        for _ in range(int(seconds / FRAME_DT)):
            self.step()
        return self.log


# ---- target paths: position (x right, y forward) in metres as a function of time ----

def stationary(x, y):
    return lambda t: (x, y)


def walk_away(speed, start=LEARN_DISTANCE):
    return lambda t: (0.0, start + speed * t)


def weave(speed=0.15, amplitude=0.3, period=10.0, start=LEARN_DISTANCE):
    return lambda t: (amplitude * math.sin(2 * math.pi * t / period), start + speed * t)


def circle(radius=1.0, speed=0.15, centre=(0.0, 1.6)):
    return lambda t: (centre[0] + radius * math.sin(speed / radius * t), centre[1] - radius * math.cos(speed / radius * t))


# ---- checks ----

RESULTS = []


def check(name, ok, detail):
    RESULTS.append(ok)
    print("%-4s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))


def basics(log, max_throttle):
    """Limits that must hold in every scenario."""
    return (
        all(0 <= r["throttle"] <= max_throttle + 1e-9 for r in log)
        and all(-1 <= r["steer"] <= 1 for r in log)
        and all(PAN_LIMITS[0] <= r["pan"] <= PAN_LIMITS[1] for r in log)
        and all(TILT_LIMITS[0] <= r["tilt"] <= TILT_LIMITS[1] for r in log)
    )


def main():
    verbose = "-v" in sys.argv
    min_gap = 0.45  # m; closer than this the car would touch the target

    run = Run(stationary(0.5, 1.2))
    log = run.run(20)
    final = log[-1]
    settled = next((r["t"] for r in log if r["distance"] < 0.75), None)
    rest = [r["pan"] for r in log if r["t"] > 15]
    wobble = max(rest) - min(rest)  # a stable camera loop settles; an unstable one keeps swinging
    check("stationary target, 25 deg aside", basics(log, MAX_THROTTLE) and 0.5 <= final["distance"] <= 0.8
          and min(r["distance"] for r in log) > min_gap and not final["moved"] and wobble < 3,
          "final distance %.2f m, reached 0.75 m at %s s, min %.2f m, pan wobble %.1f deg" % (
              final["distance"], "%.1f" % settled if settled else "never", min(r["distance"] for r in log), wobble))

    run = Run(stationary(0.0, 2.0), confidence=0.2)
    log = run.run(10)
    check("weak match (confidence 0.2)", basics(log, MAX_THROTTLE) and all(r["throttle"] == 0 for r in log),
          "no motion at all: %s" % all(r["throttle"] == 0 for r in log))

    run = Run(walk_away(0.15))
    log = run.run(30)
    far = max(r["distance"] for r in log[60:])
    near = min(r["distance"] for r in log)
    view = sum(r["in_view"] for r in log) / len(log)
    check("target walks straight away", basics(log, MAX_THROTTLE) and near > min_gap and far < 1.1 and view > 0.99,
          "distance %.2f..%.2f m, in view %.0f %%" % (near, far, view * 100))

    run = Run(weave())
    log = run.run(40)
    view = sum(r["in_view"] for r in log) / len(log)
    near, far = min(r["distance"] for r in log), max(r["distance"] for r in log[60:])
    check("target weaves left and right", basics(log, MAX_THROTTLE) and near > min_gap and far < 1.2 and view > 0.95,
          "distance %.2f..%.2f m, in view %.0f %%" % (near, far, view * 100))

    run = Run(circle())
    log = run.run(40)
    view = sum(r["in_view"] for r in log) / len(log)
    near = min(r["distance"] for r in log)
    check("target circles in front", basics(log, MAX_THROTTLE) and near > min_gap and view > 0.9,
          "min distance %.2f m, in view %.0f %%" % (near, view * 100))

    hide = lambda t: 8.0 <= t < 10.0
    run = Run(walk_away(0.12), hidden=hide)
    log = run.run(20)
    delay = run.latency
    during = [r for r in log if 8.0 + delay + FRAME_DT <= r["t"] < 10.0 + delay]
    after = [r for r in log if r["t"] > 10.5 + delay + FollowControl.REACQUIRE_HOLD + 0.5]
    check("target hidden for 2 s", basics(log, MAX_THROTTLE) and all(r["throttle"] == 0 for r in during)
          and any(r["throttle"] > 0 for r in after),
          "no motion while hidden: %s, drives again after: %s" % (
              all(r["throttle"] == 0 for r in during), any(r["throttle"] > 0 for r in after)))

    run = Run(walk_away(0.12), hidden=lambda t: t >= 5.0)
    log = run.run(60)
    lost_at = 5.0 + run.latency + FRAME_DT
    gave_up = next((r["t"] for r in log if r["mode"] == "gave_up"), None)
    scan = [r for r in log if r["mode"] == "searching"]
    stopped = all(r["throttle"] == 0 for r in log if r["t"] > lost_at)
    covered = bool(scan) and min(r["pan"] for r in scan) <= PAN_LIMITS[0] + 3 and max(r["pan"] for r in scan) >= PAN_LIMITS[1] - 3 \
        and min(r["tilt"] for r in scan) <= TILT_LIMITS[0] + 10 and max(r["tilt"] for r in scan) >= TILT_LIMITS[1] - 10
    check("target lost for good: the whole range is scanned", basics(log, MAX_THROTTLE) and stopped and covered
          and gave_up is not None and gave_up - lost_at < 40,
          "pan %.0f..%.0f, tilt %.0f..%.0f, gave up %s s after the loss, no motion: %s" % (
              min(r["pan"] for r in scan), max(r["pan"] for r in scan), min(r["tilt"] for r in scan), max(r["tilt"] for r in scan),
              "%.0f" % (gave_up - lost_at) if gave_up else "never", stopped))

    # While a candidate is being confirmed the camera has to stand still, otherwise it scans on.
    control = FollowControl(PAN_LIMITS, MAX_THROTTLE, TILT_LIMITS)
    control.update(Track("tracking", (150, 110, 20, 20), 0.9, 1.0), (WIDTH, HEIGHT), 0.1, 90.0, 90.0, 0.0)
    moving = control.update(Track("lost", reason="searching"), (WIDTH, HEIGHT), 0.1, 90.0, 90.0, 1.5)
    still = control.update(Track("lost", reason="confirming"), (WIDTH, HEIGHT), 0.1, 90.0, 90.0, 1.6)
    check("camera stands still while a candidate is confirmed", moving.pan_rate != 0 and still.pan_rate == 0 and still.tilt_rate == 0 and still.throttle == 0,
          "scanning pan rate %.2f, confirming %.2f" % (moving.pan_rate, still.pan_rate))

    # The target jumps to the side and above the camera's view: only the scan can find it again.
    for label, jump_x, jump_elevation in (("aside and above", 0.9, 30.0), ("aside and below", -0.9, -30.0), ("straight above", 0.0, 32.0)):
        run = Run(lambda t, jx=jump_x: (jx if t > 3 else 0.0, 0.6),
                  elevation=lambda t, e=jump_elevation: e if t > 3 else 0.0)
        log = run.run(60)
        lost_at = 3.0 + run.latency
        found = next((r["t"] for r in log if r["t"] > lost_at + 1 and r["seen"] and r["mode"] == "following"), None)
        idle = all(r["throttle"] == 0 for r in log if lost_at < r["t"] < (found or 99))
        check("target jumps %s: found again by the scan" % label, basics(log, MAX_THROTTLE) and found is not None and found - lost_at < 40 and idle,
              "found %s s after it left, no motion while lost: %s" % ("%.0f" % (found - lost_at) if found else "never", idle))

    run = Run(walk_away(0.15), latency=0.4)
    log = run.run(10)
    check("stale frames (0.4 s old)", basics(log, MAX_THROTTLE) and all(r["throttle"] == 0 for r in log),
          "no motion at all: %s" % all(r["throttle"] == 0 for r in log))

    for latency in (0.05, 0.1, 0.2):
        run = Run(weave(), latency=latency)
        log = run.run(40)
        view = sum(r["in_view"] for r in log) / len(log)
        swing = max(abs(r["steer"]) for r in log[100:])
        check("weave, latency %.2f s" % latency, basics(log, MAX_THROTTLE) and view > 0.9 and min(r["distance"] for r in log) > min_gap,
              "in view %.0f %%, max steer %.2f" % (view * 100, swing))

    for scale in (0.5, 1.5):
        run = Run(weave(), speed_scale=scale)
        log = run.run(40)
        view = sum(r["in_view"] for r in log) / len(log)
        check("weave, car %.1fx as fast" % scale, basics(log, MAX_THROTTLE) and view > 0.9 and min(r["distance"] for r in log) > min_gap,
              "in view %.0f %%, min distance %.2f m" % (view * 100, min(r["distance"] for r in log)))

    if verbose:
        for r in log[::15]:
            print("  t=%5.1f d=%.2f thr=%.2f steer=%+.2f pan=%5.1f %s" % (r["t"], r["distance"], r["throttle"], r["steer"], r["pan"], r["mode"]))
    print("PASS" if all(RESULTS) else "FAIL")
    return 0 if all(RESULTS) else 1


sys.exit(main())
