# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""sysinfo_server bind defaults: the status page is loopback unless asked otherwise."""
import sys
import unittest
from unittest import mock

import sysinfo_server


class TestBindDefault(unittest.TestCase):
    def _bind(self, *argv):
        with mock.patch.object(sysinfo_server, "ThreadingHTTPServer") as server, \
                mock.patch.object(sys, "argv", ["sysinfo_server.py", *argv]):
            sysinfo_server.main()
        return server.call_args[0][0]

    def test_default_is_loopback(self):
        self.assertEqual(self._bind(), ("127.0.0.1", 8090))

    def test_explicit_lan_bind_is_kept(self):
        self.assertEqual(self._bind("--bind-ip", "0.0.0.0", "--port", "9000"),
                         ("0.0.0.0", 9000))


if __name__ == "__main__":
    unittest.main()
