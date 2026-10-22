"""Hardware layer for the SunFounder PiCar-V (Pi 3, PCA9685 + TB6612).

Pin/channel layout taken from the stock SunFounder_PiCar drivers:
  PCA9685 (I2C 0x40, 60 Hz): ch0 steering, ch1 pan, ch2 tilt, ch4/ch5 motor PWM
  TB6612 direction pins (BCM): 17 left motor, 27 right motor
"""
import json
import os
import time
from dataclasses import asdict, dataclass, fields

from smbus2 import SMBus

CALIBRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "calibration.json"
)


@dataclass
class Calibration:
    i2c_bus: int = 1
    pca_address: int = 0x40
    # Servo trims in degrees, added to the centre position.
    steering_trim: float = 0.0
    pan_trim: float = 0.0
    tilt_trim: float = 0.0
    # Steering travel either side of centre; the stock firmware uses 45.
    steering_range: float = 45.0
    # Camera travel limits in degrees (90 = centre). Conservative until checked on the car.
    pan_min: float = 30.0
    pan_max: float = 150.0
    tilt_min: float = 50.0
    tilt_max: float = 130.0
    # Flip if a wheel turns the wrong way for "forward".
    invert_left: bool = False
    invert_right: bool = False
    # Motor duty in percent: the lowest that reliably turns the wheels (below it the motors
    # only hum) and a hard cap.
    min_speed: int = 20
    max_speed: int = 60

    @classmethod
    def load(cls, path=CALIBRATION_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path=CALIBRATION_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)


class PCA9685:
    MODE1, MODE2, PRESCALE, LED0_ON_L = 0x00, 0x01, 0xFE, 0x06
    OSC_HZ = 25_000_000

    def __init__(self, bus, address=0x40):
        self.bus = SMBus(bus)
        self.address = address
        self.bus.write_byte_data(address, self.MODE2, 0x04)  # totem-pole outputs
        self.bus.write_byte_data(address, self.MODE1, 0x01)  # ALLCALL, awake
        time.sleep(0.005)
        self.frequency = 60

    @property
    def frequency(self):
        return self._frequency

    @frequency.setter
    def frequency(self, hz):
        prescale = int(self.OSC_HZ / 4096 / hz - 1 + 0.5)
        old = self.bus.read_byte_data(self.address, self.MODE1)
        self.bus.write_byte_data(self.address, self.MODE1, (old & 0x7F) | 0x10)  # sleep
        self.bus.write_byte_data(self.address, self.PRESCALE, prescale)
        self.bus.write_byte_data(self.address, self.MODE1, old)
        time.sleep(0.005)
        self.bus.write_byte_data(self.address, self.MODE1, old | 0xA0)  # restart + auto-increment
        self._frequency = hz

    def set_counts(self, channel, off):
        """Duty as 0..4095 counts; 0 is full off, 4095 is full on."""
        off = max(0, min(4095, int(off)))
        if off == 0:
            data = [0, 0, 0, 0x10]
        elif off == 4095:
            data = [0, 0x10, 0, 0]
        else:
            data = [0, 0, off & 0xFF, off >> 8]
        self.bus.write_i2c_block_data(self.address, self.LED0_ON_L + 4 * channel, data)

    def close(self):
        self.bus.close()


class Hardware:
    STEER_CH, PAN_CH, TILT_CH = 0, 1, 2
    MOTOR_PWM_LEFT, MOTOR_PWM_RIGHT = 4, 5
    MOTOR_DIR_LEFT, MOTOR_DIR_RIGHT = 17, 27
    SERVO_MIN_US, SERVO_MAX_US = 600, 2400
    DEADZONE = 0.05  # throttle below this magnitude counts as zero

    def __init__(self, calibration=None):
        # Imported here so the module can be imported on a machine without GPIO.
        from gpiozero import OutputDevice

        self.cal = calibration or Calibration.load()
        self.pca = PCA9685(self.cal.i2c_bus, self.cal.pca_address)
        self._dir_left = OutputDevice(self.MOTOR_DIR_LEFT)
        self._dir_right = OutputDevice(self.MOTOR_DIR_RIGHT)
        self.stop()
        self.center()

    def _servo(self, channel, angle):
        angle = max(0.0, min(180.0, angle))
        us = self.SERVO_MIN_US + (self.SERVO_MAX_US - self.SERVO_MIN_US) * angle / 180
        self.pca.set_counts(channel, us / 1_000_000 * self.pca.frequency * 4096)

    def steer(self, value):
        """value -1 (full left) .. 1 (full right)."""
        value = max(-1.0, min(1.0, value))
        self._servo(self.STEER_CH, 90 + self.cal.steering_trim + value * self.cal.steering_range)

    def pan_tilt(self, pan=None, tilt=None):
        """Absolute angles in degrees, 90 is centre; clamped to the calibrated limits."""
        c = self.cal
        if pan is not None:
            self._servo(self.PAN_CH, max(c.pan_min, min(c.pan_max, pan)) + c.pan_trim)
        if tilt is not None:
            self._servo(self.TILT_CH, max(c.tilt_min, min(c.tilt_max, tilt)) + c.tilt_trim)

    def drive(self, value):
        """value -1 (full reverse) .. 1 (full forward).

        Inside the deadzone the motors are switched off; outside it the duty runs from
        min_speed to max_speed.
        """
        value = max(-1.0, min(1.0, value))
        if abs(value) < self.DEADZONE:
            self.stop()
            return
        forward = value >= 0
        # Set direction before duty so a reversal never drives the wrong way.
        self._dir_left.value = forward != self.cal.invert_left
        self._dir_right.value = forward != self.cal.invert_right
        c = self.cal
        # Just outside the deadzone the duty is exactly min_speed, at full throttle max_speed.
        span = (abs(value) - self.DEADZONE) / (1 - self.DEADZONE)
        counts = (c.min_speed + (c.max_speed - c.min_speed) * span) / 100 * 4095
        self.pca.set_counts(self.MOTOR_PWM_LEFT, counts)
        self.pca.set_counts(self.MOTOR_PWM_RIGHT, counts)

    def stop(self):
        self.pca.set_counts(self.MOTOR_PWM_LEFT, 0)
        self.pca.set_counts(self.MOTOR_PWM_RIGHT, 0)

    def center(self):
        self.steer(0)
        self.pan_tilt(90, 90)

    def close(self):
        self.stop()
        self._dir_left.close()
        self._dir_right.close()
        self.pca.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
