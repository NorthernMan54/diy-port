# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import apple_fm  # noqa: E402


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestHelperStop(unittest.TestCase):
    """The helper runs in its own session and _stop leaves no process behind."""

    def _classifier(self, script):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        binary = os.path.join(d, "helper")
        with open(binary, "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(binary, 0o755)
        vc = apple_fm.VehicleClassifier(binary)
        with mock.patch.object(apple_fm, "_build", return_value=True):
            vc._start()
        self.assertTrue(vc.available)
        return vc

    def test_own_session_and_eof_stop(self):
        vc = self._classifier('echo ready\nwhile read l; do echo "{}"; done\n')
        proc = vc._proc
        self.assertEqual(os.getsid(proc.pid), proc.pid)       # group signals can't reach it
        self.assertNotEqual(os.getpgid(proc.pid), os.getpgid(0))
        vc._stop()
        self.assertIsNotNone(proc.returncode)                 # reaped, no zombie
        self.assertIsNone(vc._proc)

    def test_stop_kills_a_helper_ignoring_eof(self):
        vc = self._classifier("trap '' TERM\necho ready\nexec sleep 60 </dev/null\n")
        proc = vc._proc
        with mock.patch.object(apple_fm, "STOP_WAIT_S", 0.3):
            t = time.monotonic()
            vc._stop()
        self.assertLess(time.monotonic() - t, 3)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(_alive(proc.pid))


if __name__ == "__main__":
    unittest.main()
