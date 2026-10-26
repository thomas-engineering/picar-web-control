# PiCar web control

Drive a SunFounder PiCar-V from a phone, tablet or PC browser: live camera video, two on-screen
joysticks, keyboard control and a calibration page. A small Python server on the Raspberry Pi talks
to the car's hardware directly, so the SunFounder software is not needed.

- Two multi-touch joysticks: drive (steering and throttle) and camera (pan and tilt)
- Live MJPEG video with an adaptive byte budget, so a weak Wi-Fi link does not delay the controls
- Responsive page for phone (portrait and landscape), tablet and PC; keyboard control on PC
- Speed limit, camera resolution switch, invertible axes, connection indicator with round-trip time
- Calibration page: servo trims and limits, motor minimum and maximum speed, motor direction
- Follow mode: show the car an object, and it drives after it (see [Follow mode](#follow-mode))
- Safety: the car stops when commands stop arriving (0.3 s watchdog) and when the page loses focus

There is no authentication. Anyone on the network can drive the car, so only run it on a network
you trust.

## Hardware

Tested with:

- SunFounder PiCar-V on a Raspberry Pi 3 Model B, Raspberry Pi OS (Debian 13 "Trixie"), headless
- USB webcam (`/dev/video0`)
- Optional: a second USB Wi-Fi adapter (see [Wi-Fi](#wi-fi))

The server uses the car's stock wiring:

| Part | Connection |
|---|---|
| PCA9685 PWM chip | I2C bus 1, address `0x40`, 60 Hz |
| Steering servo, camera pan, camera tilt | PCA9685 channels 0, 1, 2 |
| Motor speed (left, right) | PCA9685 channels 4, 5 |
| Motor direction (left, right) | BCM GPIO 17, 27 (TB6612) |

## Install

On the Pi, with SSH, I2C support and an internet connection:

```bash
# on your computer: copy the project to the Pi
./deploy.sh user@picar.local        # rsync to ~/video_car on the Pi

# on the Pi
cd ~/video_car
./deploy/setup.sh                   # apt packages, I2C, virtualenv, Python dependencies
# log out and back in (or reboot) so the i2c and gpio groups apply
.venv/bin/python -m server.hwtest scan      # expect the PCA9685 at 0x40
./deploy/install-service.sh         # start the server at boot
```

`setup.sh` installs OpenCV from apt, which takes a long time on a slow SD card.

Then open `http://<pi-address>:8000` in a browser (for example `http://picar.local:8000`).

Run `deploy.sh` again after changing code. The `data/` folder (the Pi's own calibration) is never
overwritten. Changes to files in `web/` take effect on a page reload; Python changes need
`sudo systemctl restart picar`. Logs: `journalctl -u picar -f`.

### Hardware checks

```bash
python -m server.hwtest scan                # find the PCA9685
python -m server.hwtest servos              # move the servos
python -m server.hwtest motors --yes        # lift the car so the wheels spin freely
```

## Using it

Open the page on the phone. The left stick steers and drives, the right stick moves the camera
(the camera keeps moving while the stick is held). Both spring back to the middle on release.
The speed limit slider caps the throttle. Settings holds the camera sliders, the 640x480 switch,
the axis inversions and the latency numbers. Another device that connects takes over the controls;
the first one then shows a button to take them back.

| Key | Action |
|---|---|
| W A S D or arrow keys | Drive |
| I J K L | Move the camera |
| C | Centre the camera |
| Space | Stop |

### Follow mode

Hold an object inside the white corners in the video and tap **Learn**, then tap **Follow**. The
camera keeps the object centred, the car steers toward it and drives to the distance at which it was
learned. The speed is capped at 30 % motor duty (`SPEED_CAP_DUTY` in `server/follower.py`).

Follow mode ends, and the car stops, when:

- you touch a stick, press STOP or space, or tap **Stop following**
- the page loses focus, closes, is replaced by another device, or its messages stop for 0.3 s
- the tracker stops delivering results for 0.3 s (the same watchdog as for manual driving)
- the camera stops, or the object is not clearly in view (the car stops at once, the camera sweeps
  for it for about 8 s, then follow mode gives up)

Two kinds of objects:

- **With colour** (at least half of the learn box is colourful): checked against the learned
  colours, so the car notices when the tracker has drifted onto something else, and finds the
  object again after it was hidden. A coloured object can be saved by name in Settings and loaded
  later; the car then looks for it.
- **Without colour** (a plain grey object): followed only while it stays in view. The tracker cannot
  notice that it switched to something else, for example a hand in front of it, and cannot find the
  object again, so learn it again after a loss. The page says so.

Setup on the Pi: `deploy/fetch_models.sh` downloads the NanoTrack model files into `models/`
(not part of the repository). Without them only objects with colour work. Saved objects go to
`data/objects/` (override with `PICAR_OBJECTS`), `PICAR_MODELS` changes the model directory and
`PICAR_TRACKER_THREADS` the number of OpenCV threads (default 3).

### Calibration

Open `/calibrate.html` (link in Settings). Changes apply at once and are saved in
`data/calibration.json` on the Pi. Hold-to-run buttons spin both motors at the configured maximum
or minimum speed; keep the wheels off the ground. The minimum speed is the lowest duty at which the
wheels reliably turn.

## Configuration

Environment variables of the server (set them in `deploy/picar.service` with `Environment=`):

| Variable | Default | Meaning |
|---|---|---|
| `PICAR_CAMERA` | `/dev/video0` | Video device |
| `PICAR_RESOLUTION` | `320x240` | Default stream size |
| `PICAR_MAX_RESOLUTION` | `640x480` | Size used by the "max resolution" setting |
| `PICAR_QUALITY` | `55` | JPEG quality |

The server listens on port 8000 (`python -m server.run [port]`) on IPv4 and IPv6.

## How it works

```
browser ──WebSocket /ws──► server/app.py ─► server/control.py ─► server/hardware.py ─► PCA9685, TB6612
        ◄──MJPEG /video──  server/camera.py ◄── USB webcam
```

- `server/hardware.py`: PCA9685 and motor driver layer, plus the calibration file
- `server/control.py`: turns commands into hardware calls and stops the car if commands stop
- `server/camera.py`: webcam to MJPEG; the camera only runs while someone watches
- `server/follow.py`, `follow_control.py`, `follower.py`: the object tracker, the control law that
  turns it into steering, throttle and camera moves, and the glue to the camera and the controller
- `server/app.py`: HTTP, WebSocket and the static `web/` page
- `web/`: `index.html` (driving), `calibrate.html`, `app.js`, `link.js` (WebSocket link), `style.css`

### WebSocket `/ws`

The client sends JSON about every 30 ms. Absent fields keep their value.

| Field | Meaning |
|---|---|
| `steer`, `throttle` | -1 to 1 |
| `pan_rate`, `tilt_rate` | -1 to 1, camera moves continuously |
| `pan`, `tilt` | Absolute camera angle in degrees (90 is the centre) |
| `center_camera` | `true` centres the camera |
| `follow` | `true` starts follow mode, `false` stops it. While it runs, the drive and camera fields above are ignored unless non-zero, which ends it |
| `ping` | Any number; the server answers `{"type": "pong", "t": <same>}` |

The server sends `{"type": "state", ...}` with the camera position and limits on connect and
whenever the camera moves, and `{"type": "follow", ...}` with the follow state whenever it changes. A newer connection replaces the older one, which is closed with code
4000. If no command arrives for 0.3 s the throttle and camera movement are stopped.

### HTTP

| Endpoint | |
|---|---|
| `GET /video` | MJPEG stream |
| `GET /api/status` | Controller state, command gaps, per-viewer video budget |
| `GET`, `POST /api/camera` | `{"max": true}` switches to the maximum resolution (not saved across restarts) |
| `GET`, `POST /api/calibration` | Read and change calibration values; changes are saved |
| `GET /api/follow`, `POST /api/follow/learn`, `DELETE /api/follow` | Follow state; learn what is in the reticle; forget it |
| `GET`, `POST /api/objects`, `POST /api/objects/NAME/load`, `DELETE /api/objects/NAME`, `GET /api/objects/NAME/thumbnail` | Saved objects |

## Wi-Fi

Lag on this car turned out to be a weak Wi-Fi link, not the Pi. Two things in the server help:
the kernel holds back at most 16 KB of unsent data per connection (`TCP_NOTSENT_LOWAT` in
`server/run.py`), and each video viewer gets a byte budget that shrinks when the client cannot keep
up (`Camera.mjpeg`). A better signal at the car still helps most: a USB extension cable for the
adapter, or a closer router.

`deploy/wifi/` is optional: it prefers a USB Wi-Fi adapter and switches the onboard radio off while
the adapter is connected, and back on if the adapter drops. It finds the radios by how they are
attached (any Wi-Fi interface on USB is the adapter, the other one is the onboard radio), so it
needs no configuration. Install it with `sudo deploy/wifi/install.sh` on the Pi. It assumes
NetworkManager and one onboard plus one USB Wi-Fi interface.

## Tools

`tools/` holds development helpers (they are not deployed to the Pi's service):

| Script | Purpose |
|---|---|
| `mock_server.py` | Run the real server on a PC with fake hardware and a synthetic camera |
| `lag_test.py` | Measure control latency, ping loss and the Pi's load while video streams over Wi-Fi |
| `slow_viewer.py` | Check that the video byte budget shrinks for a slow viewer |
| `pi_probe.sh` | Sampler that `lag_test.py --ssh` runs on the Pi |
| `follow_e2e.py` | Follow mode against the mock server: every way it must end, the speed cap, saved objects |
| `follow_sim.py` | Check and tune the control law against a simulated car |
| `track_test.py`, `object_test.py` | Check the trackers on a synthetic scene and on recorded clips |
| `record_clip.py`, `compare_trackers.py` | Record a clip with the car's camera (on the Pi) and compare trackers on it |

Each script's docstring shows how to run it.

## License

[MIT](LICENSE)

## Limits

- No authentication or encryption (plain HTTP and WebSocket)
- The webcam only offers uncompressed YUYV, so 640x480 is about the practical maximum
- Only tested on the hardware listed above
