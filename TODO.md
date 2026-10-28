# TODO

State at the end of the follow-mode session (2026-10-27): follow mode is built, deployed and
tested with the car on a box. Camera directions, steering direction, forward drive and every stop
path worked. Driving is switched off on the Pi (`PICAR_FOLLOW_DRIVE` unset), so follow mode only
moves the camera. Nothing has been pushed to GitHub; all commits are local.

## Before the car drives on the floor

- [x] **Fix the power supply.** Done 2026-10-28: the cause was a poor connector. With a lab supply
      at 8.5 V (2 A limit, the car drew at most 0.8 A) `tools/power_stress.py` passes: no undervoltage,
      throttling or reboot in any stage, including full-speed reversals. Before the fix the same
      supply at 8.1 V and 8.5 V gave undervoltage as soon as the motors ran. Still to do: run the
      test again on the car's own battery (`power-stress-test` skill, wheels off the ground).
- [ ] **Run the safety review** of follow mode from `CODE_REVIEW_PLAN.md` (`/code-review high` or
      `max`, ideally after `/model` to Opus 5.5): watchdog paths in `control.py`, `follower.py`,
      the cancel conditions, the speed cap. It has not been run yet.
- [ ] **Decide on the page-silence limit.** Follow mode ends when the page's messages stop for
      0.3 s, and short Wi-Fi stalls trigger it ("Stopped: lost the signal"). Options: keep 0.3 s,
      or raise it for follow mode only (for example 0.6 s). The project's safety rule says this
      must not be loosened without the user's approval. The tracker's own 0.3 s limit stays.

## Floor test

- [ ] Ask first. Clear area, someone ready to press STOP, car's power switch within reach.
- [ ] Enable driving: `ssh picar 'sudo systemctl set-environment PICAR_FOLLOW_DRIVE=1; sudo systemctl restart picar'`.
      It does not survive a reboot. Disable again with `sudo systemctl unset-environment PICAR_FOLLOW_DRIVE`
      and a restart.
- [ ] Start with the object held still, then moved slowly; check it stops at the learned distance
      and does not weave. Then let it follow a walking person-held object.
- [ ] Tune on the floor: `KP_SPEED`, `START_EXCESS`, `STOP_EXCESS`, `KP_PAN`, `STEER_FULL` in
      `server/follow_control.py`, and `SPEED_CAP_DUTY` (30 %) in `server/follower.py`. Re-check
      with `tools/follow_sim.py` and `tools/follow_e2e.py`.
- [ ] **Measure the camera's field of view** (`HALF_FOV`, now a guess of 27 degrees): it scales how
      far the steering turns. Mark two points a known distance apart at a known range.

## Rework (found in real tests)

- [ ] **The camera locks onto a very different object when the learned one is hidden** (reported
      as "my throat", so probably skin). Reproduce it, read the Pi's log
      (`journalctl -u picar | grep -i "tracker\|searching"`, the `searching:` lines say why
      candidates were turned down), record a clip with `tools/record_clip.py`, and add it to
      `tools/object_test.py`. Suspects: the loose hue gate in `ObjectTracker._search` (skin is
      near red and orange), the size gate, or NanoTrack drifting while the drift check stays quiet.
- [ ] **Test objects without colour that have texture** (a grey object with a logo or edges):
      record a clip, see whether the "track-only" tier holds up or whether it needs more cues.
- [ ] **Try saving and loading an object on the real page** (Settings, "Saved objects"). Only
      the mock server has exercised it.
- [ ] The page's follow bar was only checked in headless Chrome at the five sizes; look at it on
      the phone.

## Camera picture quality

The webcam (a generic UVC camera) only has brightness, contrast, saturation, hue, gamma, sharpness,
backlight compensation and power-line frequency; no exposure, gain or white-balance control, so
auto exposure and auto white balance cannot be locked. Raw YUYV only, 640x480 at most.

- [ ] **A/B test camera settings with real clips.** Add an option to `tools/record_clip.py` to set
      controls (for example `--set sharpness=0 --set saturation=120`), record the same scene with
      the object a few times (default, sharpness 0 to 2, saturation 110 to 130, both), and compare
      with `tools/object_test.py` and `tools/compare_trackers.py`. Needs the user to hold the object
      in front of the camera for a few short clips. Keep power-line frequency at 50 Hz.
- [ ] **Apply the best settings in `Camera._open`** (`server/camera.py`, via `cv2.CAP_PROP_*` or
      `v4l2-ctl`) so they apply on every reopen, and check they survive the camera restarting.
- [ ] **Add light** (a lamp or a small LED light on the car): in dim rooms the camera drops to 10 to
      15 fps and blurs motion. Compare frame rate and tracker results with and without it.
- [ ] **Try capturing at 640x480 for the tracker** and sending 320x240 video. Small objects get four
      times the pixels; measure CPU, tracker rate and control latency on the Pi
      (`tools/lag_test.py --tracker`) before keeping it.
- [ ] Check the lens focus at the distance the car is used (many cheap webcams have a manual focus
      ring), and keep the lens clean.
- [ ] If the picture is still the limit: a webcam with manual exposure and white balance, or a
      Raspberry Pi camera module (full manual control, higher frame rate). That needs a code change
      to read from it.

## Housekeeping

- [ ] Push the commits to GitHub (`git push`) when the user says so. Check `git log` and that no
      address, SSID or key path is in the files first.
- [ ] `tools/lag_test.py` needs the Pi's IPv4 address as `--host`: the name `picar1.fritz.box`
      resolves first to a stale IPv6 address of the switched-off onboard radio. Clean the stale
      router entry and reserve an address for the USB adapter in the router.
- [ ] The iPhone lag and disconnect investigation from before is still open (see the project
      memory); recheck with `tools/lag_test.py --tracker`.
- [ ] Clips (`clips/`) and the NanoTrack models (`models/`) are git-ignored. The models come from
      `deploy/fetch_models.sh`; check the model's licence before sharing the files.

## How to check things

- On a PC (no hardware): `.venv/bin/python tools/follow_e2e.py` (also `--camera-only`),
  `tools/follow_sim.py`, `tools/track_test.py`, and `tools/object_test.py --models models` with
  the clips in `clips/`.
- Deploy: `./deploy.sh picar`, then `ssh picar 'sudo systemctl restart picar'` for Python changes.
  Reload the page fully after a deploy.
