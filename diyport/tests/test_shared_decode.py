# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import shared_decode as sd  # noqa: E402

DETECT_FPS = 10


class TestSharedDecodeKeep(unittest.TestCase):
    """Live 2026-11-07 on 92DE633106E8 (15 fps): a whole stride, round(15/10) = 2, gave
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


if __name__ == "__main__":
    unittest.main()
