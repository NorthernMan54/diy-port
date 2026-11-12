# Migrating a macOS instance from pi-port to diy-port

The project was renamed, and this repository's `onvif-gst-b` branch adds ONVIF changes on top.
The scripts don't read the old names or move old files, so each existing instance has to be
moved. This guide moves them one at a time, keeping the old checkout and files for rollback.

It covers macOS instances run by `macos_launchd.sh`. Run every `sudo` command yourself. Stop if
a check fails. Don't print `RTSP` URLs or credentials into logs or tickets.

## What changes

| | Before | After |
|---|---|---|
| Package folder | `piport/` | `diyport/` |
| Service script | `scripts/piport_service.sh` | `scripts/diyport_service.sh` |
| Service settings | `PIPORT_*` | `DIYPORT_*` |
| Detection settings | `AIPORT_*` | `AIPORT_*` (unchanged) |
| Instance folder | `~/.pi-port/<instance>/` | `~/.diy-port/<instance>/` |
| Settings file | `piport.env` | `diyport.env` |
| Log | `logs/piport.log` | `logs/diyport.log` |
| launchd job | `local.piport.<instance>` | `local.diyport.<instance>` |

Models and the Apple helper build aren't in git, so they have to be copied.

Behaviour also changes, even with the new settings left alone:

- Upstream's fixes are included, among them how track timing is calculated. Watch detection
  events after the move.
- Without `AIPORT_ONVIF_MODE`, ONVIF cameras use `copy`, the existing ffmpeg path, with the
  watchdog warnings, lookup retries and reply changes from this branch.
- A restream restart leaves detection running, and detection failures leave the restream
  running, in every mode.

## Why a second checkout

Instances share a checkout. Updating it in place removes `piport/` from under every instance at
once, and each would fail at its next restart. With a second checkout, each instance moves on
its own, and the old one stays ready for rollback.

## Step 1: checks (read-only)

```bash
OLD=~/Code/pi-port                 # the existing checkout
NEW=~/Code/diy-port                # the new one; it must not exist yet
git -C "$OLD" status --short       # commit or note anything listed
git -C "$OLD" rev-parse HEAD       # note this for rollback
ls ~/.pi-port/                     # the instances to move
launchctl print system/local.piport.<instance> | grep -E 'state|pid'
ls /Library/LaunchDaemons/ ~/Library/LaunchAgents/ | grep -i piport
memory_pressure | tail -1
sysctl vm.swapusage
```

For each instance, note its running state, cameras, `PIPORT_BIND_IP` address, and
any `PIPORT_STATE_DIR`. Check that its `adopt_state.json` exists.

## Step 2: new checkout (doesn't affect anything running)

```bash
git clone --branch onvif-gst-b https://github.com/NorthernMan54/pi-port.git "$NEW"
"$OLD/venv/bin/pip" freeze > /tmp/diyport-freeze.txt
"$OLD/venv/bin/python" -m venv "$NEW/venv"
"$NEW/venv/bin/pip" install -r /tmp/diyport-freeze.txt -r "$NEW/diyport/requirements.txt"
cp -p "$OLD"/piport/models/*.onnx "$NEW/diyport/models/"
[ -d "$OLD/piport/apple/build" ] && cp -Rp "$OLD/piport/apple/build" "$NEW/diyport/apple/build"
(cd "$NEW/diyport" && ../venv/bin/python -m unittest discover -s tests)
```

Expect `OK`. A few ERROR and IndexError log lines in its output are normal, and tests needing
optional packages are skipped. Copy anything else your settings point to in the old checkout.

## Step 3: move one instance

Start with the least important instance. It's down from the first command until the last, about
a minute if all goes well. Other instances keep running on the old checkout.

Stop the old job, and move its plist aside so that it can't start again at boot:

```bash
i=<instance>
sudo launchctl bootout system/local.piport.$i
sudo mv /Library/LaunchDaemons/local.piport.$i.plist ~/local.piport.$i.plist.disabled
pgrep -fl "piport/run_all.py.*$i"     # expect nothing for this instance's state folder
```

Copy its files, leaving the originals in place:

```bash
mkdir -p ~/.diy-port/$i/logs
sudo cp -Rp ~/.pi-port/$i/state ~/.diy-port/$i/state
sed -e 's/PIPORT_/DIYPORT_/g' \
    -e "s#$HOME/.pi-port/$i/#$HOME/.diy-port/$i/#g" \
    -e "s#$OLD/piport/#$NEW/diyport/#g" \
    ~/.pi-port/$i/piport.env > ~/.diy-port/$i/diyport.env
grep -nE 'pi-port|piport|PIPORT' ~/.diy-port/$i/diyport.env
```

Read that output. Old paths in comments don't matter. Any other setting still pointing at an old
path needs fixing, such as a `DIYPORT_STATE_DIR` outside `~/.pi-port`, which would otherwise be
shared with the old instance. Leave an `AIPORT_HOSTNAME` as it is.

Check the identity survived, without printing its token:

```bash
sudo test -s ~/.diy-port/$i/state/adopt_state.json && echo adoption-state-present
grep -E '^DIYPORT_(BIND_IP|MAC|DEVICE_ID)=' ~/.diy-port/$i/diyport.env
```

Install it. For an agent instead of a root daemon, leave out `sudo` and `--daemon`:

```bash
cd "$NEW"
sudo DIYPORT_INSTANCE=$i scripts/macos_launchd.sh install --daemon
```

### Is it healthy?

After about 2 minutes, in `~/.diy-port/$i/logs/diyport.log`:

- `diyport_service: starting diy-port`, and no `Traceback`.
- `CLASSIC AVCLIENT WSS CONNECTED`, with no repeating `connection ended` or
  `was this AI Port removed in Protect?`. **The instance should not need adopting again.**
- Native cameras: `UiStreamControl`, a `starting merged RTSP pull` per camera.
- ONVIF cameras: `OnvifStreamControl`, then `push ... started`, with no repeating
  `ended after`.
- `frames for deviceID=...` about once a minute per camera, at about the camera's rate, without
  `falling behind` lasting.
- In Protect: camera online, live view, a recording, and a real detection event.

Let each instance run before moving the next. A day is a sensible gap for production.

## Step 4: clean up

Once every instance has run well for a few days:

```bash
sudo rm ~/local.piport.*.plist.disabled
sudo rm -rf ~/.pi-port
rm -rf "$OLD"
```

Check that nothing still points at them first, including your own scripts and shell history.

## Rollback

For one instance, and safe at any point:

```bash
i=<instance>
cd "$NEW" && sudo DIYPORT_INSTANCE=$i scripts/macos_launchd.sh uninstall
sudo mv ~/local.piport.$i.plist.disabled /Library/LaunchDaemons/local.piport.$i.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/local.piport.$i.plist
```

The old instance starts from its untouched files, with its old adoption state. If Protect changed
something in the meantime, such as stream names, it re-sends them when the instance connects.

## Step 5: choose ONVIF modes (optional, later)

Do this only after the rename has been stable, so that each change can be judged by itself. Set
`AIPORT_ONVIF_MODE` in the instance's `diyport.env`, then `touch ~/.diy-port/<instance>/restart`.
This briefly interrupts all of that instance's cameras.

| Mode | Video | Pushes run in | Camera sessions | Cost per camera |
|---|---|---|---|---|
| `copy` (default) | ffmpeg copies the streams | `avclient.py` | 3 | About 1% of a core, 22 MB |
| `transcode` | GStreamer encodes each channel | `avclient.py` | 2 | About 8-15% of a core, 130-290 MB |
| `isolated` | As `transcode` | A process per camera | 2 | As `transcode`, plus 15 MB |

Move one camera at a time: `copy,<deviceID>=isolated`.

The transcode needs GStreamer. Without it, the camera logs an error and uses `copy`:

```bash
brew install gstreamer
for e in vtdec vtenc_h264 rtspsrc rtph264depay flvmux h264parse videoscale videorate videoconvert; do
  gst-inspect-1.0 --exists $e && echo ok $e || echo MISSING $e
done
```

A root daemon should also test the encoder as root:

```bash
sudo /opt/homebrew/bin/gst-launch-1.0 videotestsrc num-buffers=100 ! video/x-raw,width=640,height=360 ! vtenc_h264 ! fakesink
```

For a transcoded camera:

- Healthy: one `transcode ... started` line with three sizes, and `pull ... video1 over 60 s` at
  the camera's rate.
- Take it off the transcode: repeating `stalled` or `restarting`, zero frames, or sustained frames
  below the camera's rate.
- Set `AIPORT_BOX_DELAY_MS=0` for these cameras: shared decode (on by default) gives boxes
  each frame's time from the video pushed to Protect. With `AIPORT_ONVIF_SHARED_DECODE=0`,
  recalibrate the box delay by checking boxes in playback instead.
- Watch CPU, memory and swap as cameras add up. They share the hardware media engine with
  anything else using it, such as Frigate.

## Moving a camera between AI Ports

Remove it from the old AI Port before pairing it to the new one: pairing fails silently if it's
still paired elsewhere. Protect issues new stream names, so expect about a minute of
`ended after ... BrokenPipeError` before it settles. The camera's per-camera settings don't
follow it; add them to the new instance's `diyport.env`.

## Known limits

- A wedged `copy` pull logs `restream: no video to Protect`, but only a transcode has a pipeline
  restart. A quick reconnect loop may also hide from that warning.
- Starting several cameras at once can send Protect a fallback `1920x1080` reply before the right
  size. A later reply corrects it.
- Restarts drop the recording segment in progress on the main channel, so expect a gap of up to
  about 9 s in that channel's recording.
- `isolated` only contains failures one way: if `avclient.py` dies, the pushes stop too.
