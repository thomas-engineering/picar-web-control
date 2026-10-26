# PiCar web control

Python server (FastAPI) on a Raspberry Pi 3B that drives a SunFounder PiCar-V, plus a
vanilla-JS page in `web/` (no build step, no framework). See README.md for the full picture.

## Language
- Write all documentation, code comments, docstrings, commit messages and UI text in English,
  even when the conversation in the terminal is in German.

## Working on it
- The server needs the car's hardware (`smbus2`, `gpiozero`, `/dev/video0`), so it only runs on
  the Pi. `tools/mock_server.py` runs it locally with fake hardware and a synthetic camera; use
  it with headless Chrome for screenshots at phone, tablet and PC sizes (390x844, 844x390,
  820x1180, 1180x820, 1920x1080).
- Deploy with `./deploy.sh picar`, then `ssh picar 'sudo systemctl restart picar'` for Python
  changes. Files in `web/` need no restart. Never overwrite `data/` (the Pi's calibration).
- Logs: `ssh picar 'journalctl -u picar -n 50'`. Do not start a second server by hand.
- `tools/lag_test.py` measures control latency under video load over the real Wi-Fi path;
  `tools/slow_viewer.py` checks the video byte budget.
- Commit with the GitHub noreply address (set in this repo's local git config), not the private
  address from the global git config; GitHub rejects pushes that expose it.

## Safety rules
- The car must stop when commands stop (0.3 s watchdog in `control.py`) and when the page loses
  focus. Never remove or loosen this without asking.
- Motors keep their last PWM value if the process is killed hard. Stop the service normally.
- No authentication exists. Do not expose the server beyond the local network.
- Keep the wheels off the ground for any motor test, and ask before running motors.
- Follow mode must end, and the car must stop, whenever its tracker results or the page's messages
  stop (see `Controller.run`), on any manual input, and when the page loses focus. Keep its speed
  cap. `tools/follow_e2e.py` checks all of this; run it after any change to `control.py`,
  `follower.py` or `app.py`.
- Follow mode drives only with `PICAR_FOLLOW_DRIVE=1` set for the service; by default only the camera
  follows. The camera directions and, with the wheels off the ground, the steering direction, forward
  drive, speed and stop checks were verified on the car; driving on the floor was not. Ask before
  enabling driving, and keep the wheels off the ground until the floor test is agreed.

## Conventions
- Match the existing style: short docstrings, comments only for the why, no new dependencies
  without a reason (the Pi 3 is slow; OpenCV comes from apt).
- Controls are sent continuously from the page; absolute camera moves are sent once, so a stale
  page never overrides the camera.
- Video must never delay control: keep `TCP_NOTSENT_LOWAT` (`run.py`) and the byte budget
  (`Camera.mjpeg`). Judge latency changes by measuring control round trips under video load over
  Wi-Fi, not on localhost.
- The camera pan is inverted relative to the stick on purpose (physically correct for this car).
- The default camera size is 320x240; 640x480 is an opt-in setting.
- The repository is public: no IP addresses, MAC addresses, SSIDs, key paths or credentials in
  files.
