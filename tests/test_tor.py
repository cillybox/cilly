"""Tor run by cilly: no torrc, a Unix socket only, inside bubblewrap without your home, and a
clear message when it isn't installed."""

import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import helpers as HP  # noqa: E402
from cilly import tor as T  # noqa: E402

FAKE_TOR = """#!/bin/sh
echo "[notice] Bootstrapped 5% (conn): Connecting to a relay"
echo "[notice] Bootstrapped 100% (done): Done"
exec sleep 30
"""


class TorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": f"{t}/data", "XDG_RUNTIME_DIR": f"{t}/run"})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(f"{t}/run")

    def test_the_command_line(self):
        argv = T.tor_argv("/usr/bin/tor", "/d/tor", "/r/tor/socks.sock", 1234)
        self.assertEqual(argv[1:5], ["-f", "/dev/null", "--defaults-torrc", "/dev/null"])  # no torrc, not even the system's
        opts = dict(zip(argv[6::2], argv[7::2]))
        self.assertEqual(opts["SocksPort"], "unix:/r/tor/socks.sock IsolateSOCKSAuth")  # no TCP port anyone could use
        self.assertEqual((opts["ControlPort"], opts["DNSPort"], opts["TransPort"]), ("0", "0", "0"))
        self.assertEqual(opts["__OwningControllerProcess"], "1234")

    def test_the_sandbox_has_the_network_and_no_home(self):
        argv = T.bwrap_tor_argv("/usr/bin/bwrap", ["tor"], "/home/me/.local/share/cilly/tor", "/run/user/1000/cilly/tor")
        self.assertIn("--share-net", argv)
        self.assertIn("--die-with-parent", argv)
        rw = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        self.assertEqual(rw, ["/home/me/.local/share/cilly/tor", "/run/user/1000/cilly/tor"])  # only its own folders
        self.assertFalse(any(a in ("/home", "/home/me", os.path.expanduser("~")) for a in argv))

    def test_without_tor(self):
        with mock.patch.object(T, "tor_binary", lambda: None):
            t = T.Tor()
            t.ensure()
            self.assertEqual(t.status()["installed"], False)
        self.assertEqual((t.running(), t.error), (False, "Tor isn't installed"))

    def test_it_starts_reports_progress_and_stops(self):
        fake = os.path.join(self.tmp.name, "tor")
        with open(fake, "w") as f:
            f.write(FAKE_TOR)
        os.chmod(fake, 0o755)
        with mock.patch.object(T, "tor_binary", lambda: fake):
            t = T.Tor(use_bwrap=lambda: "off")
            t.ensure()
            self.addCleanup(t.stop)
            end = time.time() + 5
            while t.progress < 100 and time.time() < end:
                time.sleep(0.05)
        self.assertEqual((t.running(), t.progress, t.error), (True, 100, None))
        for d in (t.data_dir(), t.run_dir()):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(t.log_path()).st_mode), 0o600)
        t.ensure()  # already running: not a second one
        first = t.proc
        t.ensure()
        self.assertIs(t.proc, first)
        t.stop()
        self.assertFalse(t.running())

    def test_in_bubblewrap_it_doesnt_wait_for_a_pid_it_cant_see(self):
        """bwrap gives tor its own PID namespace, where cilly's PID is no process at all: tor would
        think cilly had gone and exit after 15 s.  --die-with-parent does that job there."""
        started = []

        class Proc:
            stdout, args = iter(()), None
            def poll(self): return None
            def wait(self, *a): return 0
        def popen(argv, **kw):
            started.append(argv)
            return Proc()
        with mock.patch.object(T, "tor_binary", lambda: "/usr/bin/tor"), \
                mock.patch.object(T.shutil, "which", lambda n: "/usr/bin/bwrap"), \
                mock.patch.object(HP.H, "bwrap_check", lambda *a, **kw: None), \
                mock.patch.object(HP.subprocess, "Popen", popen), \
                mock.patch.object(HP.threading, "Thread"):
            T.Tor(use_bwrap=lambda: "auto").ensure()
            T.Tor(use_bwrap=lambda: "off").ensure()
        sandboxed, plain = started
        self.assertEqual(sandboxed[0], "/usr/bin/bwrap")
        self.assertIn("--die-with-parent", sandboxed)
        self.assertNotIn("__OwningControllerProcess", sandboxed)
        self.assertEqual(plain[plain.index("__OwningControllerProcess") + 1], str(os.getpid()))

    def test_it_starts_again_when_it_stops(self):
        fake = os.path.join(self.tmp.name, "tor")
        runs = os.path.join(self.tmp.name, "runs")
        with open(fake, "w") as f:  # the first run stops by itself, the next one keeps going
            f.write(f"#!/bin/sh\necho x >> {runs}\n"
                    f"[ $(wc -l < {runs}) -gt 1 ] && exec sleep 30\nexit 1\n")
        os.chmod(fake, 0o755)
        with mock.patch.object(T, "tor_binary", lambda: fake), mock.patch.object(HP, "RESTART_MIN", 0.2):
            t = T.Tor(use_bwrap=lambda: "off")
            t.restart_in = 0.1
            t.ensure()
            self.addCleanup(t.stop)
            first = t.proc
            end = time.time() + 10
            while not (t.proc is not first and t.running()) and time.time() < end:
                time.sleep(0.05)
        self.assertTrue(t.running())
        self.assertIsNot(t.proc, first)
        self.assertIsNone(t.error)  # running again, so nothing to report
        t.stop()  # stopped on purpose: it stays stopped
        time.sleep(0.6)
        self.assertFalse(t.running())
        with open(runs) as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_bubblewrap_required_but_missing(self):
        fake = os.path.join(self.tmp.name, "tor")
        with open(fake, "w") as f:
            f.write(FAKE_TOR)
        os.chmod(fake, 0o755)
        with mock.patch.object(T, "tor_binary", lambda: fake), mock.patch.object(T.shutil, "which", lambda n: None):
            t = T.Tor(use_bwrap=lambda: "required")
            t.ensure()
        self.assertFalse(t.running())
        self.assertIn("bwrap is not installed", t.error)


if __name__ == "__main__":
    unittest.main()
