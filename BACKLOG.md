# diy-port backlog

Open items, newest first within each section. Background for the motion, audio and face items
is in [docs/motion-audio-findings.md](docs/motion-audio-findings.md). "Olli" is
[Olli0103/unifi-protect-local-ai](https://github.com/Olli0103/unifi-protect-local-ai)
(Apache-2.0: code can be reused with its notice and a `CREDITS.md` entry).

## Research (running on another agent)

- **Face embeddings for Protect** (input to the face items under Build). Questions:
  1. **Size and model.** What does Protect 7.3.x expect in an AI Port's
     `trackerIDAttrMap.faceEmbed`?
     - Our `uiface.db` reading shows 128-float slots, up to three per face, matching SFace.
     - Olli sends 512-value ArcFace R100 or AdaFace IR101 vectors, L2-normalised.
     - Does Protect accept both, and does it read the length or assume one?
  2. **Which model real devices use.** Which face model does a real AI Port, an AI Key, or a
     camera with face detection on board (G5/G6) use for `faceEmbed`? Is it SFace, ArcFace,
     MobileFaceNet or something else? Does the vector need its own alignment or normalisation?
  3. **How naming works.** Where is the match decided: the console's face service
     (dot product against `uiface.db`), the AI Key, or the device? What threshold applies, and
     what must happen before a face sent by diy-port gets a name in Protect? So far the
     vectors are stored (cosine 1.000 in `uiface.db`) but nobody is named live.
  4. **Better models.** Olli measured AdaFace at an EER of 5.7% vs 10.5% for ArcFace on the
     owner's faces. If Protect needs 128-d vectors, is there a 128-d model as accurate, or
     can AdaFace be used locally for `facedb.py` matching while SFace stays on the wire?
  5. **The 6.0 cut-off.** Is our SFace vector-norm drop (`AIPORT_FACE_DROP_NORM` 6.0) still the
     right quality gate under another model, or does it need Olli's alignment step
     (5 YuNet landmarks to the 112x112 ArcFace template) instead?

## Build

- **EventSmartMotion for paired cameras (gap 2).** Protect drops a paired camera's own
  motion events, so motion-only events, motion recording and motion alerts are lost.
  - Payload: Olli's AI Port shape, start and stop only:
    `deviceID`, `eventType: "motion"`, `edgeType`, `levels {zone: 0-100}`, `clockWall`,
    `clockBestWall`, other clocks 0, empty `motionHeatmap` and `motionSnapshot`.
  - Detection: drive it from our motion gate, scoped to the zones in
    `ChangeSmartMotionSettings`. Use its linger start, linger stop and max-duration timings.
  - Put it behind an env toggle, default off, and confirm in Protect.
  - Pulses and heatmaps (unifi-cam-proxy-kinda §8) only if Protect needs them.
  - Reference: Olli `aiport_motion.py`.
- **EventSmartAudio: speech (gap 3, step 1).** `alrmSpeak` enter and leave from the audio
  diy-port already pulls for ONVIF cameras.
  - Every audio type must be in every message, each `enter`, `leave` or `none`.
  - Use a time-derived `eventId`.
  - Advertise the audio types in the feature flags.
  - Protect 7.3.60 accepts this from an AI Port and hands `alrmSpeak` to the AI Key for
    speech to text.
  - Reference: Olli `aiport_audio.py`, unifi-cam-proxy-kinda §10.
- **EventSmartAudio: alarms and household sounds (gap 3, step 2).** Use a YAMNet (AudioSet,
  ONNX) classifier for smoke, CO, siren, baby cry, bark, burglar, horn and glass break.
  - Tell smoke from CO by the beep pattern (T3 vs T4).
  - Keep one open audio type per camera, by priority.
  - Reference: Olli `aiport_sounds.py`.
- **Face model upgrade.** Depends on the research above. Options: AdaFace or ArcFace with
  landmark alignment for local `facedb.py` matching, and the wire `faceEmbed` in whatever
  form Protect expects. Reference: Olli `aiport_faces.py`.

## Investigate

- **Protect's detection settings are ignored** (2026-11-12, spec review 2). Protect sends these
  in `ChangeSmartDetectSettings`, and diy-port reads none of them:
  - Per zone: `sensitivity` (50 seen).
  - Per camera: `minPresenceProbability` 40, `maxPresenceProbability` 80,
    `reVerificationPolicy {person: {enable}}`, `recognitionAccuracy {face: auto}`,
    `enableTamperDetection`.
  - Our thresholds are env-only (`AIPORT_TRUE_THRESH`, `AIPORT_FACE_THRESH`), so Protect's
    sliders do nothing. Map zone sensitivity and the presence probabilities onto confirm
    thresholds. Find out first how the real AI Port interprets them: firmware strings or a
    capture.
- **Clock and timestamp text read as a plate** (ONVIF LPR). Hikvision cameras burn in a clock
  and timestamp, and fast-alpr could read them as a plate. Check `face dropped`-style logs
  and plate reads for date-like text. Ignore plate boxes that sit still across frames, or
  mask the camera's on-screen text area.

- **Privacy masks aren't applied** (2026-11-12, from the feature spec review). We advertise
  `privacyMask: True` with 16 zones, and Protect sends masks in `ChangeIspSettings.masks`.
  diy-port acks them but doesn't black them out:
  - not before detection or face reads;
  - not in snapshots;
  - for ONVIF, not in the video we push, so Protect would record the masked area.
  Fix: black out the masks in the transcode and on the detection frames, or stop
  advertising `privacyMask`.
- **Loitering.** `loiterZones` always arrives empty, so it's untested. If Protect offers loiter
  zones for AI Port cameras, add the dwell-time timer.
- **Audio settings.** Protect sends `ChangeAudioEventsSettings` per camera
  (`enableAlrmSmoke`, `...Cmonx`, `...Siren`, `...Speak`, `...BabyCry`, `...Bark`,
  `...Burglar`, `...CarHorn`, `...GlassBreak`, `recordEvent*`, `sendPulse`). Gate the audio
  events above on these flags.
- **We advertise `speaker: True` but have no talkback.** The spec says two-way audio isn't
  available through an AI Port's ONVIF path. Check whether Protect shows a talk button
  for paired cameras that does nothing; if it does, report `speaker: False` for ONVIF cameras.

- **Zone edge strip.** Olli counts a box as inside a zone at 90% overlap, and treats zone
  edges within 5% of the frame border as the border. Protect's default full-frame zone is
  inset by about 5%, so people right under a camera fall in that strip. Compare with our zone
  test on Porch and Side Door clips. Reference: Olli `aiport_zones.py`.
- **Stationary objects in a stationary heartbeat.** A real camera reports a still object
  outside every zone about every 5 min, with `edgeType: "none"`, `objectTypes: []` and
  `stationary: true`. In a `leave`, still bystanders go in `descriptors` only. Compare with
  our parking logic. Reference: unifi-cam-proxy-kinda §7.
- **Snapshots from the wrong camera (gap 4).** ucp4 `getSnapshot` has no camera id. Not seen
  on onvif (0 requests), but dropping TLV `0x3F` changes the adopt path, so test it with
  someone watching.
- **Detect on the 720p substream**, with main-stream keyframes for snapshots and face reads.
  This would cut about half the decode CPU.

## Later

- **From spec review 2 (2026-11-12), not needed now:**
  - ONVIF doorbells: none here.
  - Hallway (9:16) ONVIF streams: our native Doorbell is portrait and works.
  - Recovering from IR/night switching.
  - Person and Vehicle of Interest alarms: Protect raises these from our face and plate
    fields, so they come with the face-naming research.

- **From the feature spec (2026-11-12), not built:**
  - Locate (LED or beacon) and the support bundle: both currently get a bare `{}` ack.
  - ONVIF PTZ proxy and auto-tracking: our cameras are fixed, so not needed.
  - Edge recording backup while Protect is down.
- **The spec's ONVIF capacity disagrees with what we see live.** The spec gives 3× HD or
  2× 2K. Live, one diy-port runs four ONVIF cameras within 10 points, and Protect
  enforces capacity from the points we report. Keep reporting the measured points.
- **The spec gets one thing wrong:** the AI Port doesn't run WS-Discovery. Protect
  discovers the ONVIF camera and sends its `streamUrl` in `OnvifStreamControl`.

- **Package detection.** diy-port has no `package` type. If it's added, Olli's
  `aiport_held_followup.py` (night IR, box vs surroundings) stops a sleeping cat counting as a
  parcel.
- **Echo stored LED and speaker settings.** Protect pushes them though no such hardware
  exists. Reference: Olli `aiport_virtual_hardware.py`.
