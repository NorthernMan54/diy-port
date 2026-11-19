# Changelog

<details>
<summary><b>Changelog</b></summary>

- **2026-10-29** - PR #4 (NorthernMan54): Frigate-style detection (motion gate,
  region crops, true-positive gating, parked objects, optional Norfair), optional
  enrichment (plates, faces against Protect's face DB, vehicle attributes incl.
  Apple's model on macOS), one event per camera with batched leaves and real zone
  ids, per-camera box timing, stream recovery/backoff, replay harness, per-camera
  frames and host load lines, macOS launchd service. Fixes: track timers follow
  the analysed-frame rate, and the auto-hwdec tests pass on an RK3588 host.
- **Unreleased** - the same-host Docker stack names its host bridge `br1`
  (`AIPORT_BRIDGE_NAME`), because Protect's discovery scan ignores Docker's usual
  `br-<hash>` names; the console must run with host networking. Paired cameras now
  send ordinary motion events (`EventSmartMotion` start/stop, timed by Protect's
  `ChangeSmartMotionSettings`), so motion-only events and motion-triggered recording
  no longer depend on the camera's own events that Protect drops. Pluggable detector
  backends (`diyport/detectors/`): RK3588
  NPU, Coral Edge TPU (Raspberry Pi 5) and CPU/Core ML, chosen by `[hardware]
  detector` / `install.sh --accel`; ONVIF cameras (video push to Protect plus
  detection); a Docker bridge setup for a console on the same host; credentials
  masked in logs; macOS bare-metal support (Core ML on Apple silicon); CPU
  Docker image + Komodo stack (x86_64 / arm64); Pi 4 hardware-decode + detector
  tuning; hardened HTTPS API (per-connection TLS, request limits), supervised
  `run_all.py`, drained ffmpeg stderr, real per-zone IDs, localhost-only web
  instance management, and persistent `/data` state in the LAN Compose stacks.
- **2026-10** - firmware-update faking; optional sysinfo web UI; Pi 4 hardware
  decode and detector tuning; Docker CPU image/Komodo stack.
- **2026-09** - Line Crossing; paired-camera snapshot retrieval over UCP4; the
  `connectionHost` upload fix; event/feature-flag dedupe; multi-instance systemd
  units; one-shot `install.sh`.
- **2026-08** - initial protocol stack: discovery, adoption, both WebSocket
  connections, NPU detection and `EventSmartDetect` with thumbnails.

</details>
