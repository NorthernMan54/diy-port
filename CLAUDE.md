# diy-port

Emulates a UniFi AI Port so UniFi Protect adopts it and receives smart detections (person,
vehicle, animal, face, licence plate) from cameras that have none of their own. Python, runs
on macOS (Apple Silicon) or a Pi/RK3588. AGPL-3.0; every source file starts with an SPDX line.

## Layout
- `diyport/avclient.py`: the core. Protect WSS client, RTSP pull (ffmpeg), detection loop,
  tracker (parking, stillness, resume), one smart-detect event per camera with
  enter/moving/leave and a multi-participant leave.
- `diyport/enrich.py` (faces: YuNet + SFace; plates: fast-alpr), `facedb.py` (Protect's known-faces DB).
- `diyport/onvif_push.py`, `onvif_media.py`: ONVIF cameras. diy-port pulls the camera's RTSP
  itself and pushes video1/2 (main stream) and video3 (substream) to Protect.
- `diyport/replay.py`: replays a clip through the detector and tracker. It reads a sidecar JSON
  next to the clip (`start` must be an ISO string; `env`, `smartDetect`/`smartMotion` zones,
  optional `expect`). Add `--faces` for face and plate reads.
- `scripts/`:
  - `diyport_service.sh` (run loop) and `diyport_paths.sh` (per-instance paths);
  - `macos_launchd.sh` (install, start, stop, status, logs; `install --daemon` for root);
  - `compare_cameras.py` (side-by-side log summary);
  - `record_clip.py`, `macos_move_host.sh`.
- `docs/`: protocol notes (aiport-interface, protect-face-matching, firmware notes).

## Tests
```bash
cd diyport && ../venv/bin/python -m unittest discover -s tests
```
The suite takes about 20 s, and a few ERROR/IndexError log lines in its output are normal. Every behaviour change gets a test.

## Conventions
- Comments and commit messages cite the live evidence behind a rule ("Live 15:44 on
  Driveway: ..."). Match the existing dense, explanatory comment style.
- Settings are `AIPORT_*` (detection) and `DIYPORT_*` (service) environment variables.
  A new behaviour gets an env toggle when it's risky.
- Shell scripts must run under macOS bash 3.2.

## Running instances (macOS)
Each instance has `~/.diy-port/<instance>/`, containing:
- `diyport.env`
- `state/`
- `logs/diyport.log`
- `restart` (touch it to restart; no sudo needed)
- `service.pid`

ONVIF cameras need a root launchd daemon because macOS Local Network privacy blocks user
agents from reaching them. Once one instance is a root daemon bound to a specific IP, every
instance on that Mac must be a root daemon with its own `DIYPORT_BIND_IP` alias; otherwise
:443 gives EADDRINUSE. `sudo` commands are run by the user.

Site-specific details (hosts, IPs, cameras, current work) are in `CLAUDE.local.md`.
