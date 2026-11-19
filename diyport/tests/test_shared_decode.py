# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import onvif_push  # noqa: E402
import shared_decode as sd  # noqa: E402

DETECT_FPS = 10  # tests pin the rate they exercise; avclient.DETECT_FPS is env-tunable


class TestSharedDecodeKeep(unittest.TestCase):
    """Live 2026-11-07 on a 15 fps camera: a whole stride, round(15/10) = 2, gave
    7.5 fps and "falling behind" every minute; keep 10 of every 15, evenly spread."""

    def kept(self, fps, n=60):
        keep = sd.keep_ratio(fps, DETECT_FPS)
        return [i for i in range(n) if sd.keeps(i, keep)]

    def test_15fps_keeps_the_detection_rate(self):
        self.assertEqual(sd.keep_ratio(15, DETECT_FPS), (DETECT_FPS, 15))
        kept = self.kept(15, 30)
        self.assertEqual(len(kept), 2 * DETECT_FPS)
        gaps = {b - a for a, b in zip(kept, kept[1:])}
        self.assertLessEqual(gaps, {1, 2})               # evenly spread, no bursts

    def test_whole_multiples_are_a_plain_stride(self):
        self.assertEqual(len(self.kept(20)), 30)
        self.assertEqual({b - a for a, b in zip(self.kept(20), self.kept(20)[1:])}, {2})
        self.assertEqual(len(self.kept(30)), 20)

    def test_slow_or_odd_sources_keep_every_frame(self):
        self.assertEqual(self.kept(8, 16), list(range(16)))
        self.assertEqual(sd.keep_ratio(14.985, DETECT_FPS), (DETECT_FPS, 15))
        self.assertEqual(sd.keep_ratio(0.2, DETECT_FPS), (1, 1))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_ffmpeg_select_matches_the_wall_time_feeder(self):
        # Each kept output frame takes the wall time of the input frame the feeder queued
        # for it, so ffmpeg's select must pick exactly the same input frames.
        keep = sd.keep_ratio(15, DETECT_FPS)
        out = subprocess.run(
            ["ffmpeg", "-v", "info", "-f", "lavfi", "-i", "testsrc=size=32x32:rate=15:duration=3",
             "-vf", sd.select_filter(keep) + ",showinfo",
             "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
        pts = [int(line.split("pts:")[1].split()[0]) for line in out.splitlines()
               if "pts:" in line and "showinfo" in line]
        expect = [i for i in range(45) if sd.keeps(i, keep)]
        self.assertEqual(pts, expect)


class TestFeed(unittest.TestCase):
    class _Stdin:
        def __init__(self):
            self.data = b""
        def write(self, data):
            self.data += data
        def close(self):
            pass

    class _Proc:
        def __init__(self):
            self.stdin = TestFeed._Stdin()
        def poll(self):
            return None

    def test_records_reach_ffmpeg_and_kept_frames_queue_their_wall_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "detect.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(path)
            server.listen(1)

            def serve():
                conn, _ = server.accept()
                with conn:
                    conn.sendall(onvif_push.DETECT_RECORD.pack(
                        onvif_push.DETECT_FRAME, 1000.0, 1) + b"a")
                    conn.sendall(onvif_push.DETECT_RECORD.pack(
                        onvif_push.DETECT_BYTES, 0.0, 3) + b"hdr")
                    conn.sendall(onvif_push.DETECT_RECORD.pack(
                        onvif_push.DETECT_FRAME, 2000.0, 1) + b"b")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            proc = self._Proc()
            walls = []
            sd.feed("CAM", proc, path, (1, 1), walls)
            thread.join(2.0)
            self.assertEqual(walls, [1000.0, 2000.0])
            self.assertEqual(proc.stdin.data, b"ahdrb")


if __name__ == "__main__":
    unittest.main()
