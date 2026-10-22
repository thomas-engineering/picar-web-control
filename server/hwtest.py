"""Interactive hardware checks. Run on the Pi:

    python -m server.hwtest scan
    python -m server.hwtest servos
    python -m server.hwtest motors --yes     # lift the car so the wheels spin freely
"""
import argparse
import signal
import sys
import time

from smbus2 import SMBus

from .hardware import Calibration, Hardware


def scan(cal):
    with SMBus(cal.i2c_bus) as bus:
        found = []
        for addr in range(0x03, 0x78):
            try:
                bus.read_byte(addr)
                found.append(addr)
            except OSError:
                pass
    print("I2C devices on bus %d: %s" % (cal.i2c_bus, ", ".join(hex(a) for a in found) or "none"))
    if cal.pca_address not in found:
        print("PCA9685 not found at %s: check wiring, battery switch and I2C setting." % hex(cal.pca_address))
        return 1
    print("PCA9685 found at %s." % hex(cal.pca_address))
    return 0


def sweep(setter, lo, hi, step=0.05, delay=0.02):
    n = int(abs(hi - lo) / step)
    for i in range(n + 1):
        setter(lo + (hi - lo) * i / n)
        time.sleep(delay)


def servos(hw):
    print("Centre"); hw.center(); time.sleep(1)
    print("Steering left/right")
    sweep(hw.steer, 0, -1); sweep(hw.steer, -1, 1); sweep(hw.steer, 1, 0)
    print("Pan")
    c = hw.cal
    sweep(lambda a: hw.pan_tilt(pan=a), 90, c.pan_min, 1, 0.02)
    sweep(lambda a: hw.pan_tilt(pan=a), c.pan_min, c.pan_max, 1, 0.02)
    sweep(lambda a: hw.pan_tilt(pan=a), c.pan_max, 90, 1, 0.02)
    print("Tilt")
    sweep(lambda a: hw.pan_tilt(tilt=a), 90, c.tilt_min, 1, 0.02)
    sweep(lambda a: hw.pan_tilt(tilt=a), c.tilt_min, c.tilt_max, 1, 0.02)
    sweep(lambda a: hw.pan_tilt(tilt=a), c.tilt_max, 90, 1, 0.02)
    hw.center()


def motors(hw):
    for label, sign in (("forward", 1), ("reverse", -1)):
        print("Both wheels %s: expect them to turn the same way, towards the %s of the car"
              % (label, "front" if sign > 0 else "rear"))
        for level in (0.3, 0.6, 1.0):
            hw.drive(sign * level)
            time.sleep(1.5)
        hw.stop()
        time.sleep(1)


def main():
    p = argparse.ArgumentParser(prog="hwtest")
    p.add_argument("test", choices=["scan", "servos", "motors", "all"])
    p.add_argument("--yes", action="store_true", help="confirm the wheels are off the ground")
    args = p.parse_args()

    cal = Calibration.load()
    if args.test == "scan":
        return scan(cal)
    if scan(cal):
        return 1
    if args.test in ("motors", "all") and not args.yes:
        print("Motor test needs --yes: lift the car off the ground first.")
        return 2

    # A killed test must not leave the motors running.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    with Hardware(cal) as hw:
        try:
            if args.test in ("servos", "all"):
                servos(hw)
            if args.test in ("motors", "all"):
                motors(hw)
        except KeyboardInterrupt:
            print("Interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
