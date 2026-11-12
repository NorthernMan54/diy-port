# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""norfair_match.distance: the plain-float rewrite must match Frigate's numpy original."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import norfair_match as nm  # noqa: E402


def numpy_distance(detection, estimate):
    """Frigate's original (numpy) distance, kept as the reference."""
    estimate_dim = np.diff(estimate, axis=0).flatten()
    detection_dim = np.diff(detection, axis=0).flatten()
    if (not np.all(np.isfinite(estimate_dim)) or not np.all(np.isfinite(detection_dim))
            or estimate_dim[0] <= 0 or estimate_dim[1] <= 0
            or detection_dim[0] <= 0 or detection_dim[1] <= 0):
        return float("inf")
    detection_pos = np.array([np.average(detection[:, 0]), np.max(detection[:, 1])])
    estimate_pos = np.array([np.average(estimate[:, 0]), np.max(estimate[:, 1])])
    offset = (detection_pos - estimate_pos).astype(float)
    offset[0] /= estimate_dim[0]
    offset[1] /= estimate_dim[1]
    widths = np.sort([estimate_dim[0], detection_dim[0]])
    heights = np.sort([estimate_dim[1], detection_dim[1]])
    change = np.append(offset, [widths[1] / widths[0] - 1.0, heights[1] / heights[0] - 1.0])
    return float(np.linalg.norm(change))


def box(x, y, w, h):
    return np.array([[x, y], [x + w, y + h]], dtype=float)


class TestDistance(unittest.TestCase):
    def test_matches_frigates_numpy_distance(self):
        # Live 2026-11-11: the numpy version was ~74% of avclient's GIL time.
        rng = random.Random(1)
        for _ in range(5000):
            a = box(rng.uniform(0, 900), rng.uniform(0, 900), rng.uniform(-5, 100), rng.uniform(-5, 100))
            b = box(rng.uniform(0, 900), rng.uniform(0, 900), rng.uniform(-5, 100), rng.uniform(-5, 100))
            want, got = numpy_distance(a, b), nm.distance(a, b)
            if want == float("inf"):
                self.assertEqual(got, want)
            else:
                self.assertAlmostEqual(got, want, delta=1e-12 * max(1.0, want))

    def test_degenerate_boxes_are_infinitely_far(self):
        good = box(10, 10, 50, 80)
        for bad in (box(10, 10, 0, 80), box(10, 10, 50, -1),
                    np.array([[np.nan, 0], [5, 5]]), np.array([[0, 0], [np.inf, 5]])):
            self.assertEqual(nm.distance(bad, good), float("inf"))
            self.assertEqual(nm.distance(good, bad), float("inf"))

    def test_same_box_is_zero(self):
        self.assertEqual(nm.distance(box(10, 20, 50, 100), box(10, 20, 50, 100)), 0.0)


if __name__ == "__main__":
    unittest.main()
