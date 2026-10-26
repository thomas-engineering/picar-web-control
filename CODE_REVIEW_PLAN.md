# Code review plan: follow mode

Review plan for the "follow a learned object" feature (`server/follow.py`, changes in `camera.py`,
`app.py` and `web/`). The goal is to catch safety and latency problems before the first motor test.

## When to review

1. **After the offline tracker and control law are done** (no hardware involved): correctness and
   simplicity review.
2. **Before the first motor test, wheels off the ground**: safety review, the one that matters most.
3. **After tuning on the floor**: a final pass over the whole diff.

## How to run it

- Local review: `/code-review high` (use `max` for the safety pass). Add `--fix` only after reading
  the findings.
- Deep review: `/code-review ultra` runs a multi-agent review in the cloud. It is billed and only
  the user can start it.
- Model: the skill does not say which model reviews. To be sure it is Opus 5.5, run `/model`, switch,
  and then start the review.

## Safety checklist (review pass 2)

- [ ] Follow commands go through `Controller.command` and must be refreshed every tick. If the
      tracker thread stalls or dies, the 0.3 s watchdog in `control.py` still stops the car. The
      watchdog is not loosened or bypassed.
- [ ] Follow turns off when the page loses focus, the WebSocket drops, the stick is touched, or Stop
      is pressed.
- [ ] Follow only runs while a viewer is connected.
- [ ] Target lost, low confidence, implausible box size jump, or a box covering most of the frame
      leads to zero throttle at once.
- [ ] A frame older than about 200 ms leads to zero throttle.
- [ ] The speed cap is enforced on the server, not only in the page, and it respects `min_speed`.
- [ ] Too close to the target (box too large) stops the car; no reverse unless decided explicitly.
- [ ] Exceptions in the tracker or control code stop the motors instead of leaving the last PWM value.
- [ ] Stopping the service normally halts the motors (no hard kill needed).
- [ ] Absolute camera moves are still sent once, so a stale page cannot override the camera.
- [ ] The camera pan inversion is kept as it is.

## Correctness and performance checklist (review passes 1 and 3)

- [ ] Thread safety between the camera thread, the tracker thread and the asyncio loop (shared
      frame, shared state, learn and forget racing with tracking).
- [ ] The tracker never blocks the video stream; the byte budget and `TCP_NOTSENT_LOWAT` are
      unchanged.
- [ ] Frame handling at 320x240 by default; 640x480 still works.
- [ ] HSV mask and histogram edge cases: dark or gray objects, empty region, learn with no frame.
- [ ] Control loop: no integral windup, derivative filtered, deadbands present, pan and steering
      do not fight each other.
- [ ] No new dependencies without a reason (OpenCV comes from apt).
- [ ] The repository stays public-safe: no IP addresses, MAC addresses, SSIDs, key paths or
      credentials in files.
- [ ] Style matches the code around it: short docstrings, comments only for the why, English text.

## Measurements to accompany the review

- CPU load and frame rate on the Pi with tracking active.
- `tools/lag_test.py`: control round trips under video load with follow on, over the real Wi-Fi path,
  not on localhost.
- `tools/slow_viewer.py`: the video byte budget still holds.
- Control law against a simulated plant (target moving, latency added, target lost).

## Handling findings

1. Fix safety findings before any motor test, then re-run the safety pass.
2. Judge latency findings by measurement, not by opinion.
3. Record anything deliberately not fixed, with the reason, in the commit message or README.
