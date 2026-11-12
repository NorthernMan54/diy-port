# Motion and audio events: findings from other projects (2026-11-12)

Goal: close diy-port gap 2 (no motion events for paired cameras) and gap 3 (no audio
detections). Nothing is built yet; this note collects what other projects have shown.

## Sources

| Source | What it is | Relevance |
|---|---|---|
| `~/unifi-cam-proxy-kinda` (`EVENTSMARTDETECT_PROTOCOL_NOTES.md` §7, §8, §10; `unifi/cams/managers/smart_motion_manager.py`) | Our camera emulator (Frigate to Protect). Notes are from real camera captures | **Camera** wire format, not an AI Port |
| [Olli0103/unifi-protect-local-ai](https://github.com/Olli0103/unifi-protect-local-ai) (`src/aikey/aiport_motion.py`, `aiport_audio.py`) | Independent AI Key and AI Port emulator, alpha, actively developed (updated 2026-11-12) | **AI Port** payloads, taken from Protect's own parser (7.2.105 / 7.3.60) |

## EventSmartMotion (gap 2)

**AI Port shape (Olli, from Protect's parser, the minimum Protect accepts):**
```json
{"deviceID": "<camera MAC>", "eventType": "motion", "edgeType": "start|stop",
 "eventId": 0, "levels": {"<zoneId>": 0-100},
 "clockWall": <ms>, "clockBestWall": <ms>, "clockBestMonotonic": 0,
 "clockMonotonic": 0, "clockStream": 0, "clockStreamRate": 0,
 "motionHeatmap": "", "motionSnapshot": ""}
```
- `deviceID` (the camera's MAC) routes the event to the paired camera, as on our
  EventSmartDetect.
- Only start and stop. No pulses, empty heatmap and snapshot, `eventId` 0.

**Camera shape (unifi-cam-proxy-kinda, real captures), the fuller version:**
- start, then a pulse every 2–3 s (`edgeType: "unknown"`, `eventType: "pulse"`,
  `eventId` 18446744073709551615), then stop.
- Its own `eventId` counter, separate from smart detect.
- `levels` per motion zone, 0–100.
- `clockBest*` are 0 on start and pulse. On stop they hold the time motion started.
- Start and stop name heatmap and snapshot files (160x90 heatmap, 360x360 crop,
  640x360 full view), and Protect fetches them with `GetRequest` after stop.

**Timing settings come from `ChangeSmartMotionSettings`.** diy-port already receives and
stores it (`_motion_settings`, `_event_max_ms`). Live payload on onvif:
`enable`, `lingerEventStartMSec` 1000, `lingerEventStopMSec` 3000,
`eventMaxDurationMSec` 300000, `zones {id: {coord, level, triggerLight}}`.

**Olli's detection approach:**
- 64x36 grayscale thumbnail with a running background, measured inside the motion
  zones only.
- A zone counts as moving when 0.2%–3.2% of its cells change: the threshold scales
  with the zone's `level`, where 0 is most sensitive.
- A whole-scene change (a light switching) resets the background instead of
  firing.
- Start after `lingerEventStartMSec` of motion; stop after `lingerEventStopMSec`
  without it, or at `eventMaxDurationMSec`.
- `levels` is the peak per zone.

**Fit for diy-port:** the motion gate (`AIPORT_MOTION_GATE`) already computes per-frame
motion on every analysed frame, so a per-camera start/stop state machine on top of it is
small. Start with Olli's minimal shape (start and stop, no files), behind an env toggle
default off, and confirm in Protect that motion events and motion-triggered recording
appear. Add pulses and heatmaps only if Protect needs them.

## EventSmartAudio (gap 3)

From unifi-cam-proxy-kinda §10 (a real capture) and Olli's `aiport_audio.py`
(Protect's `audioMessageSchema`; Olli confirms Protect 7.3.60 accepts it from an AI Port
and routes it by `deviceID`):
- One message per state change. **Every** audio field must be present every time,
  because Protect ends the event only when all read `leave` or `none`:
  `alrmSmoke`, `alrmCmonx`, `alrmSiren`, `alrmBabyCry`, `alrmSpeak`, `alrmBark`,
  `alrmBurglar`, `alrmCarHorn`, `alrmGlassBreak`, `loudNoise`, `soundLoss`.
  Each is `enter`, `leave` or `none`.
- Other fields: `clockMonotonic`, `clockStream`, `clockStreamRate` 1000, `clockWall`,
  `eventId` (time-derived, not the smart-detect counter), `leveldB`, `levels` (numbers,
  0 in the captures). Add `deviceID` for an AI Port.
- No updates between enter and leave. The real camera reports about 14 s late but keeps
  the original time in `clockWall`.
- The audio types must also be advertised in the feature flags, or the camera's
  `smartDetectAudioTypes` stays empty.
- Olli only detects speech presence (`alrmSpeak`); the AI Key turns it into text. Smoke,
  CO and glass break need an audio classifier (for example YAMNet), which is a bigger
  piece of work.

## Other details worth keeping

- **Stationary objects (unifi-cam-proxy-kinda §7, real capture):** a still object
  outside every zone is reported with `edgeType: "none"`, `objectTypes: []`,
  `stationary: true`, `displayTimeoutMSec` about 300, every `zonesStatus` 0/none,
  about every 5 min. In a `leave`, still bystanders go in `descriptors` only, not
  `smartDetectSnapshots` or `trackerIDAttrMap`. This bears on our parking logic.
- Real devices batch several trackers in one `descriptors` array.
- Olli's project covers AI Key functions diy-key may lack: face-enhancement
  callbacks, key-moment search indexing with CLIP embeddings, audio-thumbnail tagging,
  and speech-to-text. Its `docs/evidence/compatibility-matrix.md` records what is
  confirmed on a real controller.
- Olli also says ONVIF ingest is not implemented there; diy-port is ahead on that.

## Suggested order

1. EventSmartMotion, minimal shape, behind a toggle. Low risk, high value: it brings
   back motion events and motion recording on paired cameras.
2. EventSmartAudio for speech or loud noise only, from the audio diy-port already
   pulls for ONVIF cameras.
3. Classified alarms (smoke, CO, glass) with an audio model.

## Other parts of Olli's project worth reusing (2026-11-12)

Paths are under `src/aikey/` in Olli0103/unifi-protect-local-ai.

| Module | What it does | Use in diy-port |
|---|---|---|
| `aiport_motion.py` | EventSmartMotion: zone-scoped thumbnail motion and the payload | Gap 2, as above |
| `aiport_audio.py` | Speech presence to `alrmSpeak` (Protect 7.3.60 accepts it from an AI Port) | Gap 3, step 1 |
| `aiport_sounds.py` | YAMNet (AudioSet ONNX) to smoke, CO, siren, baby, bark, burglar, horn and glass. Smoke and CO are told apart by beep pattern (T3 vs T4). One open audio type per camera, by priority | Gap 3, step 2 |
| `aiport_faces.py` | YuNet, then aligned to the ArcFace template, then ArcFace R100 or AdaFace IR101 (512-d). AdaFace halved the error on the owner's faces (EER 5.7% vs 10.5%) | Our SFace reads haven't named anyone live. Protect's `uiface.db` holds 128-float slots, which matches SFace, so check the dimension Protect expects before switching. Worth trying for our local `facedb` matching |
| `aiport_zones.py` | A box counts in a zone at 90% inside. Zone edges within 5% of the frame border count as the border, since Protect's default full-frame zone is inset about that much | Compare with our zone test: objects close under a camera fall in that strip |
| `aiport_held_followup.py` | Night-IR package check: box vs surroundings change, so a breathing cat isn't a package | Only if we add `package` detection, which diy-port doesn't have |
| `aiport_virtual_hardware.py` | Keeps LED and speaker settings Protect pushes, though no hardware exists | Small: replies that echo stored settings |
| AI Key side (`clip.py`, face-enhancement and speech callbacks) | CLIP search indexing, enhanced face images, speech to text | For diy-key, not diy-port |

Licence: Olli's project is Apache-2.0, which can be used in AGPL-3.0 diy-port with
attribution (keep its notice; add a CREDITS.md entry).
