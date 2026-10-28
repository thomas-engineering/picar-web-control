---
name: power-stress-test
description: Stress-test the PiCar's power supply with the motors, camera, video and tracker running, then check the Pi's logs for undervoltage and errors. Use when asked to test or verify the power supply, battery, undervoltage, or whether the car is safe to drive on the floor.
---

# Power-supply stress test

Checks that the supply holds under the worst realistic load and that the logs stay clean. The
open TODO "Fix the power supply" is only done when this passes.

## Safety first (from CLAUDE.md)

- The motors run. **Ask the user before starting**, and get a yes for this run.
- Wheels off the ground (car on a box), power switch within reach, nothing near the wheels.
- Undervoltage while the motors run can reboot the Pi and leave the motors running (the driver
  keeps its last setting). The script stops on the first live undervoltage, but the user's hand
  on the switch is the real protection.
- Do not start a second server; the test drives the running `picar` service over its WebSocket,
  so the 0.3 s watchdog stays active.
- Do not put the Pi's IP address in files or commit messages (the repo is public).

## Steps

1. Ask which supply is in use (external supply with its voltage, or the battery) and note it for
   the report. Ask the user to aim the camera at something in front of it (the tracker learns
   whatever is in the reticle).
2. Get the Pi's IPv4 address: `ssh picar 'hostname -I'` (the first entry). The name
   `picar1.fritz.box` can resolve to a stale IPv6 address.
3. Check the service is up and the baseline is known:
   `ssh picar 'systemctl is-active picar; vcgencmd get_throttled; sudo dmesg -T | grep -i voltage | tail'`.
   `0x50000` only means undervoltage happened earlier since boot; the test looks for new events.
4. Run the test from the project root on the PC:
   `.venv/bin/python tools/power_stress.py --host <ip>:8000 --ssh picar --wheels-off`
   It takes about two minutes (`--secs` changes the stage length). Stages: video only, video plus
   tracker, motors steady, motors with steering and camera sweeping, fast motor reversals.
   Run it in the background and watch for the end; do not poll.
5. Read the result and the logs yourself, do not only quote the verdict:
   - `ssh picar 'sudo dmesg -T | grep -Ei "voltage|brown|usb|mmc|i2c" | tail -30'`
   - `ssh picar 'journalctl -u picar --since "10 min ago" --no-pager | tail -60'`
   - `ssh picar 'vcgencmd get_throttled; vcgencmd measure_volts; uptime'`
   Look for: undervoltage or brownout lines, USB resets of the camera or Wi-Fi dongle
   (a dip can drop them), I2C errors from the servo/motor board, tracebacks, "Stopped: lost the
   signal", "tracker silent", camera reopen messages, and a changed uptime (a reboot).
6. Make sure the motors are off at the end: `curl -s localhost:8000/api/status` on the Pi shows
   `"throttle": 0.0`. If the Pi rebooted or the server died, check the wheels first and tell the
   user to use the power switch if they still turn.

## Interpreting the result

- **PASS**: no new voltage lines, no live undervoltage, no throttling flags in any stage, no reboot,
  clean journal. Also say which supply was used; a pass on the external supply says nothing about
  the battery, so offer a second run on the battery.
- **FAIL at motor stages only**: the supply sags under motor current. Raise the supply voltage
  within the board's limit, use a stronger supply or fresh batteries, or shorter/thicker leads.
  Reversals are the harshest stage; if only they fail, a bulk capacitor at the board input helps.
- **FAIL already at "video + tracker"**: the supply is too weak for the Pi itself (check the
  cable and the voltage at the Pi's 5 V pin).
- Camera or dongle USB resets without undervoltage lines also point at the supply.
- Record the outcome in `TODO.md` (tick or annotate "Fix the power supply") and in the memory
  notes, without IP addresses.
