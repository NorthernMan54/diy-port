# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""envutil.py: one parser per kind of AIPORT_* setting."""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import envutil  # noqa: E402


class TestEnv(unittest.TestCase):
    def env(self, value):
        return mock.patch.dict(os.environ, {"AIPORT_T": value})

    def test_bool(self):
        self.assertTrue(envutil.env_bool("AIPORT_UNSET_XYZ", True))
        for off in ("0", "false", "No", " OFF "):
            with self.env(off):
                self.assertFalse(envutil.env_bool("AIPORT_T", True))
        with self.env(""):
            self.assertFalse(envutil.env_bool("AIPORT_T", False))  # empty -> default
        with self.env("1"):
            self.assertTrue(envutil.env_bool("AIPORT_T", False))

    def test_number_typo_is_default_not_crash(self):
        # A bare float() at import used to take the instance down on a diyport.env typo.
        with self.env("1.5s"), self.assertLogs("aiport-env", "WARNING"):
            self.assertEqual(envutil.env_float("AIPORT_T", 3), 3)
        with self.env("0"):
            self.assertEqual(envutil.env_float("AIPORT_T", 3), 0.0)  # 0 is a value, not unset
        with self.env(""):
            self.assertEqual(envutil.env_int("AIPORT_T", 7), 7)
        with self.env("12"):
            self.assertEqual(envutil.env_int("AIPORT_T", 7), 12)

    def test_keyed(self):
        self.assertEqual(envutil.parse_keyed("5, abc=3 ,x", "N", 1), (5, {"ABC": 3}))
        self.assertEqual(envutil.parse_keyed(None, "N", 1), (1, {}))
        self.assertEqual(envutil.parse_keyed("person=0.8", "N", 0.7, float, str),
                         (0.7, {"person": 0.8}))


if __name__ == "__main__":
    unittest.main()
