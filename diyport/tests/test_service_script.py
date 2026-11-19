# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""scripts/diyport_service.sh against a stub diy-port (DIYPORT_SERVICE_TEST): settings from the
env file, a restart on touch, a clean stop on Ctrl-C, diy-port in its own process group, a
diy-port left by an earlier start stopped first; and scripts/log_writer.py's rotation."""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
SCRIPT = os.path.join(SCRIPTS, "diyport_service.sh")
sys.path.insert(0, SCRIPTS)
import log_writer  # noqa: E402

STUB = textwrap.dedent("""\
    import os, signal, sys, time
    out = os.environ["STUB_OUT"]
    with open(out, "a") as f:
        f.write("start pid=%d pgid=%d args=%s mode=%s\\n" % (
            os.getpid(), os.getpgid(0), " ".join(sys.argv[1:]), os.environ.get("AIPORT_MODE")))
    def stop(sig, frame):
        with open(out, "a") as f:
            f.write("int %d\\n" % os.getpid())
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    while True:
        time.sleep(0.2)
    """)


class TestServiceScript(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(repo, "venv", "bin"))
        os.makedirs(os.path.join(repo, "diyport"))
        os.symlink(sys.executable, os.path.join(repo, "venv", "bin", "python"))
        with open(os.path.join(repo, "diyport", "run_all.py"), "w") as f:
            f.write(STUB)
        self.env_file = os.path.join(self.dir, "diyport.env")
        with open(self.env_file, "w") as f:
            f.write("DIYPORT_IFACE=en0\nDIYPORT_MAC=AA\nDIYPORT_DEVICE_ID=dev\n"
                    "DIYPORT_STATE_DIR=/tmp/x\nAIPORT_MODE=one  # a comment\nrm -rf /\n")
        self.trigger = os.path.join(self.dir, "restart")
        self.out = os.path.join(self.dir, "stub.out")
        self.log = os.path.join(self.dir, "service.log")
        self.pidfile = os.path.join(self.dir, "service.pid")
        self.env = dict(os.environ, DIYPORT_SERVICE_TEST=repo, DIYPORT_ENV_FILE=self.env_file,
                        DIYPORT_RESTART_FILE=self.trigger, DIYPORT_LOG=self.log,
                        DIYPORT_PIDFILE=self.pidfile, STUB_OUT=self.out)
        self.orphan = None
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(["/bin/bash", SCRIPT], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)

    def tearDown(self):
        # SIGINT, as Ctrl-C: SIGKILL left the script's diy-port (in its own process group)
        # and log writer running, one more after every suite run.
        for p in (self.proc, self.orphan):
            if p is not None and p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        if self.proc is not None:
            self.proc.stdout.close()

    def logged(self):
        try:
            with open(self.log) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def lines(self, prefix):
        try:
            with open(self.out) as f:
                return [line.split() for line in f if line.startswith(prefix)]
        except FileNotFoundError:
            return []

    def wait_for(self, prefix, count, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.lines(prefix)) >= count:
                return self.lines(prefix)
            time.sleep(0.1)
        self.fail("timed out waiting for %d %r line(s); got %s" % (count, prefix, self.lines("")))

    def test_bind_ip_and_the_instance_state_dir(self):
        # DIYPORT_STATE_DIR left out: the instance's own state/ under ~/.diy-port.
        with open(self.env_file, "w") as f:
            f.write("DIYPORT_IFACE=en0\nDIYPORT_MAC=AA\nDIYPORT_DEVICE_ID=dev\n"
                    "DIYPORT_BIND_IP=192.0.2.9\n")
        self.env["DIYPORT_INSTANCE"] = "test-instance"
        self.start()
        args = " ".join(self.wait_for("start", 1)[0])
        self.assertIn("--bind-ip 192.0.2.9", args)
        self.assertIn("/.diy-port/test-instance/state ", args + " ")

    def test_restart_and_stop(self):
        self.start()
        first = self.wait_for("start", 1)[0]
        pid, pgid = first[1].split("=")[1], first[2].split("=")[1]
        self.assertEqual(pid, pgid)                       # its own process group
        self.assertIn("--iface en0", " ".join(first))
        self.assertIn("mode=one", " ".join(first))        # comment stripped
        time.sleep(1.2)                                   # mtime has 1 s resolution
        with open(self.trigger, "w"):
            pass
        second = self.wait_for("start", 2)[1]
        self.assertEqual(len(self.wait_for("int", 1)), 1)  # the first was asked to stop
        self.proc.send_signal(signal.SIGINT)              # Ctrl-C
        self.proc.wait(timeout=15)
        self.assertEqual(len(self.wait_for("int", 2)), 2)
        self.assertNotEqual(second[1], first[1])
        time.sleep(0.5)                                   # the log writer drains the pipe
        output = self.logged()
        self.assertIn("ignoring line", output)            # the rm line isn't run
        self.assertIn("stopping", output)
        self.assertNotIn("setpgid", output)
        self.assertFalse(os.path.exists(self.pidfile))    # removed on a clean stop

    def test_a_pi_port_left_by_an_earlier_start_is_stopped_first(self):
        stub = os.path.join(self.env["DIYPORT_SERVICE_TEST"], "diyport", "run_all.py")
        self.orphan = subprocess.Popen([sys.executable, stub, "orphan"], env=self.env,
                                       start_new_session=True)
        self.wait_for("start", 1)
        with open(self.pidfile, "w") as f:
            f.write("%d\n" % self.orphan.pid)
        self.start()
        self.wait_for("int %d" % self.orphan.pid, 1)       # asked to stop
        self.orphan.wait(timeout=10)
        self.wait_for("start", 2)                          # then the new one starts
        time.sleep(0.5)
        self.assertIn("left running by an earlier start", self.logged())
        self.proc.send_signal(signal.SIGINT)
        self.proc.wait(timeout=15)


class TestLogWriter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "diyport.log")

    def test_rotates_at_the_size_and_keeps_the_newest(self):
        log = log_writer.RotatingLog(self.path, max_bytes=100, keep=2)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        for i in range(12):
            log.write(b"line %02d %s\n" % (i, b"x" * 30))   # 40 bytes each
        log.close()
        names = sorted(os.listdir(self.dir))
        self.assertEqual(names, ["diyport.log", "diyport.log.1", "diyport.log.2"])
        # Three 40-byte lines fill a file; the twelfth rotated, so the current one is empty.
        with open(self.path + ".2", "rb") as f:
            self.assertTrue(f.read().startswith(b"line 06"))  # lines 00-05 were dropped
        with open(self.path + ".1", "rb") as f:
            self.assertTrue(f.read().startswith(b"line 09"))
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_a_log_already_over_the_size_is_rotated_on_open(self):
        with open(self.path, "wb") as f:
            f.write(b"y" * 500)
        log_writer.RotatingLog(self.path, max_bytes=100, keep=3).close()
        self.assertEqual(os.path.getsize(self.path + ".1"), 500)
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_copies_stdin_until_every_writer_closes(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "log_writer.py"), self.path],
                              input=b"one\ntwo\n", capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")                 # not a terminal: no echo
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"one\ntwo\n")


class TestStaleGroup(unittest.TestCase):
    """A dead run_all.py leader must not hide its orphaned stage children."""

    LEADER = textwrap.dedent("""\
        import os, signal, subprocess, sys, time
        out = os.environ["STUB_OUT"]
        child = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avclient.py")
        p = subprocess.Popen([sys.executable, child])
        with open(out, "a") as f:
            f.write("leader %d child %d\\n" % (os.getpid(), p.pid))
        def stop(sig, frame):
            p.terminate()
            p.wait()
            sys.exit(0)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        while True:
            time.sleep(0.2)
    """)
    CHILD = "import time\nwhile True: time.sleep(0.2)\n"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(repo, "venv", "bin"))
        os.makedirs(os.path.join(repo, "diyport"))
        os.symlink(sys.executable, os.path.join(repo, "venv", "bin", "python"))
        with open(os.path.join(repo, "diyport", "run_all.py"), "w") as f:
            f.write(self.LEADER)
        with open(os.path.join(repo, "diyport", "avclient.py"), "w") as f:
            f.write(self.CHILD)
        self.env_file = os.path.join(self.dir, "diyport.env")
        with open(self.env_file, "w") as f:
            f.write("DIYPORT_IFACE=en0\nDIYPORT_MAC=AA\nDIYPORT_DEVICE_ID=dev\n")
        self.out = os.path.join(self.dir, "stub.out")
        self.pidfile = os.path.join(self.dir, "service.pid")
        self.env = dict(os.environ, DIYPORT_SERVICE_TEST=repo,
                        DIYPORT_ENV_FILE=self.env_file,
                        DIYPORT_LOG=os.path.join(self.dir, "service.log"),
                        DIYPORT_PIDFILE=self.pidfile, STUB_OUT=self.out)
        self.proc = None
        self.children = []

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc is not None:
            self.proc.stdout.close()
        for pid in self.children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _lines(self):
        try:
            with open(self.out) as f:
                return [line.split() for line in f if line.startswith("leader ")]
        except FileNotFoundError:
            return []

    def test_orphaned_stage_children_are_stopped(self):
        self.proc = subprocess.Popen(["/bin/bash", SCRIPT], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline and not self._lines():
            time.sleep(0.1)
        first = self._lines()[0]
        leader, child = int(first[1]), int(first[3])
        self.children.append(child)
        os.kill(leader, signal.SIGKILL)
        deadline = time.time() + 20
        while time.time() < deadline and len(self._lines()) < 2:
            time.sleep(0.2)
        self.assertGreaterEqual(len(self._lines()), 2)
        deadline = time.time() + 5
        while time.time() < deadline and _alive(child):
            time.sleep(0.1)
        self.assertFalse(_alive(child))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
