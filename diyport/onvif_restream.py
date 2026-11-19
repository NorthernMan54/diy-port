#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""One process per ONVIF camera for its video push to Protect (AIPORT_ONVIF_MODE=isolated).

PushManager keeps deciding what each camera should push: channels, stream names, sizes, the
profile lookup and the replies to Protect. Only the running pushes move: a RemotePush stands in
for each FlvPush, and the camera's process runs the real ones (with their ffmpeg or gst-launch).

So a crash in a restream costs that camera's video and nothing else, and its clock stamps don't
wait for detection's turn at the interpreter lock. Bench 2026-11-04 of the push code under
pure-Python load, an upper bound for detection: stamps 11 ms late at p95 with 1 camera's load
and 32 ms with 5, under 1 ms with the load in another process. The live log showed no effect at
one camera, so this is for crash containment first. It only helps one way: the process takes its
orders over the Protect connection in avclient, so if avclient dies the process exits too.

The process reads JSON commands, one per line, on stdin and exits when stdin closes, so it can't
outlive avclient. It reports each channel's last video to Protect on stdout once a second.
With shared decode on (the default for a transcoded camera), detection reads this process's
video1 socket, so replacing a dead process briefly interrupts that camera's detection too.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import onvif_push

log = logging.getLogger("aiport-onvif")

REPORT_S = 1.0
SILENT_KILL_S = 15.0    # no report for this long: the process is stuck, so it is replaced
RESPAWN_MAX_S = 30.0
RESPAWN_HEALTHY_S = 60.0  # a process that lasted this long starts the backoff again
IDLE_S = 10.0           # with no pushes left; a re-send stops one push just before starting the next


def uses_process(device_id):
    return onvif_push.mode_of(device_id) == onvif_push.ISOLATED


def _command_summary(command):
    """A command safe to log: never the RTSP login."""
    safe = dict(command)
    if isinstance(safe.get("url"), str):
        safe["url"] = onvif_push._describe(safe["url"])
    return safe


class RemotePush:
    """What PushManager needs of an FlvPush, for one running in the camera's process."""

    def __init__(self, worker, channel, source_url, stream_name, destination, ffmpeg_bin,
                 camera_audio, renditions):
        onvif_push.parse_destination(destination)  # ValueError here, as FlvPush
        self.worker = worker
        self.channel = channel
        self.source_url = source_url
        self.stream_name = stream_name
        self.destination = destination
        self.ffmpeg_bin = ffmpeg_bin
        self.camera_audio = camera_audio
        self.renditions = renditions
        self.last_sent = time.monotonic()

    def matches(self, source_url, stream_name, destination, renditions=None, camera_audio=None):
        same = (self.source_url, self.stream_name, self.destination, self.renditions) == \
               (source_url, stream_name, destination, renditions)
        return same and (camera_audio is None or bool(camera_audio) == bool(self.camera_audio))

    def start(self):
        self.worker.add(self)
        return self

    def stop(self):
        self.worker.remove(self)

    def restart_source(self, reason):
        self.worker.send({"op": "restart_source", "channel": self.channel, "reason": reason})

    def reconnect(self, reason):
        self.worker.send({"op": "reconnect", "channel": self.channel, "reason": reason})

    def command(self):
        return {"op": "start", "channel": self.channel, "url": self.source_url,
                "name": self.stream_name, "dest": self.destination, "ffmpeg": self.ffmpeg_bin,
                "audio": self.camera_audio, "renditions": self.renditions}


class Worker:
    """The camera's process: started on its first push and replaced if it dies or sticks,
    with every current push started again. A push keeps its last_sent across that, so the
    restream warning sees a process that keeps dying."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.pushes = {}
        self.proc = None
        self._failures = 0
        self._lock = threading.Lock()

    def add(self, push):
        with self._lock:
            self.pushes[push.channel] = push
            if self.proc is None:
                self._spawn()       # sends every push, this one included
            else:
                self._send(push.command())

    def remove(self, push):
        with self._lock:
            if self.pushes.get(push.channel) is not push:
                return
            del self.pushes[push.channel]
            self._send({"op": "stop", "channel": push.channel})
            if not self.pushes:
                timer = threading.Timer(IDLE_S, self._retire)
                timer.daemon = True
                timer.start()

    def send(self, command):
        with self._lock:
            self._send(command)

    def _send(self, command):
        proc = self.proc
        if proc is None:
            return
        try:
            proc.stdin.write(json.dumps(command) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):   # it died; its replacement gets every push again
            pass

    def _retire(self):
        with self._lock:
            if self.pushes or self.proc is None:
                return
            proc, self.proc = self.proc, None
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _spawn(self):
        """With self._lock held."""
        try:
            # Its own group, so a stuck or killed one's ffmpeg or gst-launch can go with it.
            proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), self.device_id],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                    start_new_session=True)
        except OSError as exc:
            log.error("restream process for %s failed to start: %r", self.device_id, exc)
            self.proc = None
            timer = threading.Timer(RESPAWN_MAX_S, self._respawn_if_wanted, args=(None,))
            timer.daemon = True
            timer.start()
            return
        self.proc = proc
        log.info("restream process for %s started (pid %d)", self.device_id, proc.pid)
        reported = [time.monotonic()]
        threading.Thread(target=self._read, args=(proc, reported), daemon=True,
                         name=f"restream-read-{self.device_id}").start()
        threading.Thread(target=self._watch, args=(proc, reported), daemon=True,
                         name=f"restream-watch-{self.device_id}").start()
        for push in self.pushes.values():
            self._send(push.command())

    def _note_report(self, sent, now=None):
        """A process's report: {channel: seconds since its last video to Protect, or None}."""
        now = time.monotonic() if now is None else now
        with self._lock:
            for channel, age in sent.items():
                push = self.pushes.get(channel)
                if push is not None and age is not None:
                    push.last_sent = max(push.last_sent, now - age)

    def _read(self, proc, reported):
        started = time.monotonic()
        try:
            for line in proc.stdout:
                reported[0] = time.monotonic()
                try:
                    self._note_report(json.loads(line)["sent"], reported[0])
                except (ValueError, KeyError, TypeError):
                    log.warning("restream process for %s: bad report %r", self.device_id,
                                line[:200])
        except Exception:
            log.exception("restream process for %s: report reader failed", self.device_id)
        rc = proc.wait()
        with self._lock:
            if self.proc is not proc:   # retired: it stopped its own pushes on the way out
                return
        self._kill_group(proc)
        lasted = time.monotonic() - started
        self._failures = 0 if lasted >= RESPAWN_HEALTHY_S else self._failures + 1
        wait = min(RESPAWN_MAX_S, 2.0 ** self._failures) if self._failures else 1.0
        log.warning("restream process for %s exited rc=%s after %.0f s, replacing it in %.0f s; "
                    "its pushes resume when replaced", self.device_id, rc, lasted, wait)
        time.sleep(wait)
        self._respawn_if_wanted(proc)

    def _respawn_if_wanted(self, dead):
        with self._lock:
            if self.proc is not dead:
                return
            self.proc = None
            if self.pushes:
                self._spawn()

    def _watch(self, proc, reported):
        while proc.poll() is None:
            if time.monotonic() - reported[0] >= SILENT_KILL_S:
                log.warning("restream process for %s has not reported for %.0f s, killing it "
                            "(pid %d)", self.device_id, SILENT_KILL_S, proc.pid)
                self._kill_group(proc)
                return
            time.sleep(1.0)

    @staticmethod
    def _kill_group(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Workers:
    """PushManager's push_factory: one Worker per camera that uses a process."""

    def __init__(self):
        self._workers = {}
        self._lock = threading.Lock()

    def push(self, device_id, channel, source_url, stream_name, destination, ffmpeg_bin="ffmpeg",
             camera_audio=True, pool=None, renditions=None):
        if not uses_process(device_id):
            return onvif_push.FlvPush(device_id, channel, source_url, stream_name, destination,
                                      ffmpeg_bin, camera_audio=camera_audio, pool=pool,
                                      renditions=renditions)
        with self._lock:
            worker = self._workers.setdefault(device_id, Worker(device_id))
        return RemotePush(worker, channel, source_url, stream_name, destination, ffmpeg_bin,
                          camera_audio, renditions)


# --- the camera's process -----------------------------------------------------------

def _private_pipes():
    """(commands, reports) as files only this process holds. fds 0 and 1 go elsewhere first:
    ffmpeg inherits stdin and reads commands from it ('q' quits), and any child holding stdout
    would hide this process's death from the parent."""
    commands = os.fdopen(os.dup(0), "r")
    reports = os.fdopen(os.dup(1), "w")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    return commands, reports


class Process:
    """The pushes of one camera, driven by commands."""

    def __init__(self, device_id, make_push=None):
        self.device_id = device_id
        self.pool = None    # made on the first start: it needs avclient's FFMPEG_BIN
        self.make_push = make_push or onvif_push.FlvPush
        self.pushes = {}    # channel -> (FlvPush, its last_sent when new)
        self.lock = threading.Lock()

    def handle(self, command):
        op, channel = command.get("op"), command.get("channel")
        if op == "start":
            self._stop(channel)
            renditions = command.get("renditions")
            # JSON made these lists; the pull pool hashes them and FlvPush compares them.
            renditions = tuple(tuple(r) for r in renditions) if renditions else None
            ffmpeg = command.get("ffmpeg") or "ffmpeg"
            self.pool = self.pool or onvif_push.PullPool(ffmpeg)
            push = self.make_push(self.device_id, channel, command["url"], command["name"],
                                  command["dest"], ffmpeg,
                                  camera_audio=command.get("audio", True), pool=self.pool,
                                  renditions=renditions).start()
            with self.lock:
                self.pushes[channel] = (push, push.last_sent)
        elif op == "stop":
            self._stop(channel)
        elif op == "restart_source":
            with self.lock:
                entry = self.pushes.get(channel)
            if entry:
                entry[0].restart_source(command.get("reason") or "asked to")
        elif op == "reconnect":
            with self.lock:
                entry = self.pushes.get(channel)
            if entry:
                entry[0].reconnect(command.get("reason") or "asked to")
        else:
            log.warning("restream process for %s: unknown command %r", self.device_id, command)

    def _stop(self, channel):
        with self.lock:
            entry = self.pushes.pop(channel, None)
        if entry:
            entry[0].stop()

    def report(self, now=None):
        """{channel: seconds since its last video to Protect, None before any}."""
        now = time.monotonic() if now is None else now
        with self.lock:
            return {channel: None if push.last_sent == first else now - push.last_sent
                    for channel, (push, first) in self.pushes.items()}

    def stop_all(self):
        for channel in list(self.pushes):
            self._stop(channel)


def main(argv):
    import config
    device_id = argv[1]
    config.apply_logging()
    commands, reports = _private_pipes()
    process = Process(device_id)

    def _exit(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)

    def report_loop():
        while True:
            try:
                reports.write(json.dumps({"sent": process.report()}) + "\n")
                reports.flush()
            except (OSError, ValueError):
                return      # the parent is gone; stdin ending stops this process
            time.sleep(REPORT_S)
    threading.Thread(target=report_loop, daemon=True, name="restream-report").start()

    log.info("restream process for %s running (pid %d)", device_id, os.getpid())
    try:
        for line in commands:
            try:
                command = json.loads(line)
            except ValueError:
                # Do not echo malformed input: truncating it can cut before the '@' and
                # leave a password fragment in the log.
                log.warning("restream process for %s: bad command (%d bytes, not logged)",
                            device_id, len(line.rstrip("\n")))
                continue
            try:
                process.handle(command)
            except Exception:
                log.exception("restream process for %s: command failed: %s", device_id,
                              _command_summary(command))
    finally:
        process.stop_all()
    log.info("restream process for %s exiting", device_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
