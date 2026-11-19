#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""Unit and real-process tests for onvif_restream.py: a camera's video push in its own process."""
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import onvif_push  # noqa: E402
import onvif_restream as rs  # noqa: E402

DEST = "tcp://127.0.0.1:1?retryInterval=1&connectTimeout=5"


def _wait(check, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return check()


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _modes(default, **per_camera):
    return mock.patch.multiple(onvif_push, MODES=(default, per_camera),
                               transcode_available=lambda: True)


class TestMode(unittest.TestCase):
    def test_only_isolated_uses_a_process(self):
        with _modes("isolated", COPIED="copy", TRANSCODED="transcode"):
            self.assertTrue(rs.uses_process("CAM"))
            self.assertFalse(rs.uses_process("COPIED"))
            self.assertFalse(rs.uses_process("TRANSCODED"))

    def test_isolated_without_gstreamer_stays_in_this_process(self):
        with mock.patch.multiple(onvif_push, MODES=("isolated", {}),
                                 transcode_available=lambda: False):
            self.assertFalse(rs.uses_process("NOGST2"))

    def test_a_camera_not_isolated_keeps_its_push_in_this_process(self):
        with _modes("copy", CAM="isolated"):
            push = rs.Workers().push("OTHER", "video1", "rtsp://c", "S", DEST)
            self.assertIsInstance(push, onvif_push.FlvPush)
            remote = rs.Workers().push("CAM", "video1", "rtsp://c", "S", DEST)
            self.assertIsInstance(remote, rs.RemotePush)

    def test_bad_destination_raises_as_a_real_push_does(self):
        # PushManager._sync catches this ValueError and logs it.
        with _modes("isolated"), self.assertRaises(ValueError):
            rs.Workers().push("CAM", "video1", "rtsp://c", "S", "udp://nope")


class TestPushManagerUsesTheFactory(unittest.TestCase):
    def test_factory_makes_the_pushes(self):
        made = []

        def factory(*args, **kwargs):
            push = mock.Mock(args=args, kwargs=kwargs)
            push.start.return_value = push
            made.append(push)
            return push
        m = onvif_push.PushManager(fetch_profiles=lambda url: [], background=False,
                                   push_factory=factory)
        m.set_source("CAM", "rtsp://cam/101")
        m.configure("CAM", {"video1": {"avSerializer": {
            "destinations": [DEST], "parameters": {"streamName": "S1"}}}})
        self.assertEqual(made[0].args[:4], ("CAM", "video1", "rtsp://cam/101", "S1"))


class TestRemotePush(unittest.TestCase):
    def setUp(self):
        self.worker = mock.Mock()
        self.renditions = (("video1", 2560, 1440, 20, 4000, 20),)
        self.push = rs.RemotePush(self.worker, "video1", "rtsp://c", "S", DEST, "ffmpeg", True,
                                  self.renditions)

    def test_matches_as_a_real_push_does(self):
        self.assertTrue(self.push.matches("rtsp://c", "S", DEST, self.renditions))
        self.assertFalse(self.push.matches("rtsp://c", "S2", DEST, self.renditions))
        self.assertFalse(self.push.matches("rtsp://c", "S", DEST))
        self.assertTrue(self.push.matches("rtsp://c", "S", DEST, self.renditions, True))
        self.assertFalse(self.push.matches("rtsp://c", "S", DEST, self.renditions, False))

    def test_forwards_to_its_worker(self):
        self.assertIs(self.push.start(), self.push)
        self.worker.add.assert_called_once_with(self.push)
        self.push.restart_source("re-send")
        self.worker.send.assert_called_once_with(
            {"op": "restart_source", "channel": "video1", "reason": "re-send"})
        self.push.reconnect("re-send")
        self.assertEqual(self.worker.send.call_args,
                         mock.call({"op": "reconnect", "channel": "video1",
                                    "reason": "re-send"}))
        self.push.stop()
        self.worker.remove.assert_called_once_with(self.push)


class TestProcessCommands(unittest.TestCase):
    def setUp(self):
        self.made = []

        def make(device, channel, url, name, dest, ffmpeg, camera_audio=True, pool=None,
                 renditions=None):
            push = mock.Mock(last_sent=100.0, renditions=renditions, args=(device, channel, url))
            push.start.return_value = push
            self.made.append(push)
            return push
        self.p = rs.Process("CAM", make_push=make)

    def _start(self, **extra):
        self.p.handle({"op": "start", "channel": "video1", "url": "rtsp://c", "name": "S",
                       "dest": DEST, "ffmpeg": "ffmpeg", "audio": True, **extra})

    def test_renditions_come_back_hashable(self):
        # JSON turned them into lists; the pull pool uses them as a dict key.
        self._start(renditions=[["video1", 2560, 1440, 20, 4000, 20]])
        self.assertEqual(self.made[0].renditions, (("video1", 2560, 1440, 20, 4000, 20),))
        hash(self.made[0].renditions)

    def test_start_replaces_and_stop_stops(self):
        self._start()
        self._start()
        self.made[0].stop.assert_called_once()
        self.p.handle({"op": "stop", "channel": "video1"})
        self.made[1].stop.assert_called_once()
        self.assertEqual(self.p.pushes, {})

    def test_restart_source_reaches_the_push(self):
        self._start()
        self.p.handle({"op": "restart_source", "channel": "video1", "reason": "re-send"})
        self.made[0].restart_source.assert_called_once_with("re-send")
        self.p.handle({"op": "restart_source", "channel": "video9"})    # none: ignored

    def test_reconnect_reaches_the_push(self):
        self._start()
        self.p.handle({"op": "reconnect", "channel": "video1", "reason": "re-send"})
        self.made[0].reconnect.assert_called_once_with("re-send")
        self.p.handle({"op": "reconnect", "channel": "video9"})         # none: ignored

    def test_report_says_nothing_until_a_frame_is_sent(self):
        # A new push's last_sent is its creation: reporting that would make a replacement
        # look healthy, and the restream warning would never fire.
        self._start()
        self.assertEqual(self.p.report(now=130.0), {"video1": None})
        self.made[0].last_sent = 125.0
        self.assertEqual(self.p.report(now=130.0), {"video1": 5.0})


class TestCommandLogging(unittest.TestCase):
    def test_a_failed_command_never_logs_the_rtsp_login(self):
        safe = rs._command_summary({"op": "start", "channel": "video1",
                                    "url": "rtsp://admin:s3cr3t@192.0.2.10/1"})
        self.assertNotIn("s3cr3t", str(safe))
        self.assertIn("192.0.2.10/1", str(safe))


class TestMainLogging(unittest.TestCase):
    def test_bad_json_is_not_echoed(self):
        line = '{"op": "start", "url": "rtsp://admin:s3cr3t@192.0.2.10/1"\n'

        class IdleProcess:
            def handle(self, command):
                raise AssertionError("bad JSON must not reach handle")
            def report(self):
                return {}
            def stop_all(self):
                pass

        with mock.patch.object(rs, "_private_pipes",
                               return_value=(io.StringIO(line), io.StringIO())), \
                mock.patch.object(rs, "Process", return_value=IdleProcess()), \
                mock.patch("config.apply_logging"), \
                self.assertLogs("aiport-onvif", "WARNING") as logs:
            rs.main(["diyport-onvif", "CAM"])
        output = "\n".join(logs.output)
        self.assertNotIn("s3cr3t", output)
        self.assertIn("bad command", output)

    def test_the_main_loop_redacts_a_failed_command(self):
        line = json.dumps({"op": "start", "channel": "video1",
                           "url": "rtsp://admin:s3cr3t@192.0.2.10/1"}) + "\n"

        class BrokenProcess:
            def handle(self, command):
                raise KeyError("name")
            def report(self):
                return {}
            def stop_all(self):
                pass

        with mock.patch.object(rs, "_private_pipes",
                               return_value=(io.StringIO(line), io.StringIO())), \
                mock.patch.object(rs, "Process", return_value=BrokenProcess()), \
                mock.patch("config.apply_logging"), \
                self.assertLogs("aiport-onvif", "ERROR") as logs:
            rs.main(["diyport-onvif", "CAM"])
        output = "\n".join(logs.output)
        self.assertNotIn("s3cr3t", output)
        self.assertIn("192.0.2.10/1", output)


class TestWorkerReports(unittest.TestCase):
    def test_a_report_moves_last_sent_forward_only(self):
        worker = rs.Worker("CAM")
        push = rs.RemotePush(worker, "video1", "rtsp://c", "S", DEST, "ffmpeg", True, None)
        worker.pushes["video1"] = push
        push.last_sent = 50.0
        worker._note_report({"video1": None}, now=100.0)
        self.assertEqual(push.last_sent, 50.0)      # an inherited time survives a new process
        worker._note_report({"video1": 10.0}, now=100.0)
        self.assertEqual(push.last_sent, 90.0)
        worker._note_report({"video1": 30.0}, now=100.0)
        self.assertEqual(push.last_sent, 90.0)
        worker._note_report({"video9": 1.0}, now=100.0)     # stopped since: ignored


class TestRealProcess(unittest.TestCase):
    """A real process and a stand-in ffmpeg, pushing to a local socket."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.pidfile = os.path.join(self.dir.name, "ffmpeg.pid")
        self.ffmpeg = os.path.join(self.dir.name, "ffmpeg")
        with open(self.ffmpeg, "w") as f:
            f.write(f"""#!{sys.executable}
import os, struct, sys, time
open({self.pidfile!r}, "w").write(str(os.getpid()))
w = sys.stdout.buffer
def tag(t, ts, p):
    return bytes([t]) + len(p).to_bytes(3, "big") + (ts & 0xffffff).to_bytes(3, "big") + b"\\0\\0\\0\\0" + p + struct.pack(">I", len(p) + 11)
w.write(b"FLV\\x01\\x01\\0\\0\\0\\x09\\0\\0\\0\\0")
w.write(tag(9, 0, bytes([0x17, 0, 0, 0, 0, 1, 2, 3])))
n = 0
while True:
    w.write(tag(9, n * 20, bytes([0x17 if n % 10 == 0 else 0x27, 1, 0, 0, 0, 9, 9, 9]))); w.flush()
    n += 1
    time.sleep(0.02)
""")
        os.chmod(self.ffmpeg, os.stat(self.ffmpeg).st_mode | stat.S_IXUSR)
        self.received = [0]
        self.server = socket.socket()
        self.addCleanup(self.server.close)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()
        self.dest = f"tcp://127.0.0.1:{self.server.getsockname()[1]}?retryInterval=1&connectTimeout=5"

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn):
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                self.received[0] += len(data)

    def _ffmpeg_pid(self):
        try:
            return int(open(self.pidfile).read() or 0)
        except (OSError, ValueError):
            return 0

    def _start_push(self):
        worker = rs.Worker("CAM")
        push = rs.RemotePush(worker, "video1", "rtsp://cam/101", "S", self.dest, self.ffmpeg, True,
                             None)
        self.addCleanup(self._finish, worker)
        return worker, push.start()

    def _finish(self, worker):
        worker.pushes.clear()
        proc = worker.proc
        worker._retire()
        if proc:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker._kill_group(proc)
                proc.wait()
            proc.stdout.close()

    def test_video_reaches_protect_and_is_reported(self):
        worker, push = self._start_push()
        first = push.last_sent
        self.assertTrue(_wait(lambda: self.received[0] > 1000), "no video arrived")
        self.assertTrue(_wait(lambda: push.last_sent > first), "no report of it")

    def test_a_killed_process_is_replaced_and_takes_its_ffmpeg_with_it(self):
        worker, push = self._start_push()
        self.assertTrue(_wait(lambda: self.received[0] > 1000 and self._ffmpeg_pid()))
        first, old_ffmpeg = worker.proc, self._ffmpeg_pid()
        os.kill(first.pid, signal.SIGKILL)
        self.assertTrue(_wait(lambda: not _alive(old_ffmpeg) or
                              subprocess.run(["ps", "-o", "stat=", "-p", str(old_ffmpeg)],
                                             capture_output=True, text=True).stdout[:1] == "Z"),
                        "its ffmpeg outlived it")
        self.assertTrue(_wait(lambda: worker.proc is not first and worker.proc is not None),
                        "not replaced")
        before = self.received[0]
        self.assertTrue(_wait(lambda: self.received[0] > before + 1000), "no video after")
        first.stdout.close()
        first.stdin.close()

    def test_a_stuck_process_is_killed(self):
        with mock.patch.object(rs, "SILENT_KILL_S", 0.5):
            worker, push = self._start_push()
            first = worker.proc
            self.assertTrue(_wait(lambda: self.received[0] > 1000))
            os.kill(first.pid, signal.SIGSTOP)
            self.assertTrue(_wait(lambda: first.poll() is not None), "still there")
            self.assertTrue(_wait(lambda: worker.proc is not first and worker.proc is not None))
        first.stdout.close()
        first.stdin.close()

    def test_it_retires_once_no_push_is_left(self):
        with mock.patch.object(rs, "IDLE_S", 0.1):
            worker, push = self._start_push()
            proc = worker.proc
            self.assertTrue(_wait(lambda: self.received[0] > 1000 and self._ffmpeg_pid()))
            push.stop()
            self.assertEqual(proc.wait(timeout=15), 0)
            self.assertIsNone(worker.proc)
        self.assertTrue(_wait(lambda: not _alive(self._ffmpeg_pid())), "its ffmpeg lives on")
        proc.stdout.close()

    def test_a_push_back_in_time_keeps_the_process(self):
        with mock.patch.object(rs, "IDLE_S", 0.1), \
                mock.patch.object(rs.threading, "Timer"):
            worker, push = self._start_push()
            proc = worker.proc
            push.stop()
            again = rs.RemotePush(worker, "video1", "rtsp://cam/101", "S", self.dest, self.ffmpeg,
                                  True, None).start()
            worker._retire()  # what the idle timer would do
            self.assertIs(worker.proc, proc)
            self.assertIs(worker.pushes["video1"], again)

    def test_it_exits_when_its_parent_is_gone(self):
        # stdin closing is all the process needs: it can't outlive avclient.
        proc = subprocess.Popen([sys.executable, rs.__file__, "CAM"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, text=True, start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        import json
        proc.stdin.write(json.dumps({"op": "start", "channel": "video1", "url": "rtsp://c",
                                     "name": "S", "dest": self.dest, "ffmpeg": self.ffmpeg,
                                     "audio": True, "renditions": None}) + "\n")
        proc.stdin.flush()
        self.assertTrue(_wait(lambda: self.received[0] > 1000 and self._ffmpeg_pid()))
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertTrue(_wait(lambda: not _alive(self._ffmpeg_pid())), "its ffmpeg lives on")


if __name__ == "__main__":
    unittest.main()
