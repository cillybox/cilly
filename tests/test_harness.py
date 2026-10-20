"""The dev harness (tools/harness.py): a second run must not wipe a running one."""

import glob
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "tools", "harness.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def folders() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "cilly-harness*")))


class HarnessTests(unittest.TestCase):
    def start(self, port, *extra):
        p = subprocess.Popen([sys.executable, HARNESS, "--port", str(port), *extra], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop, p)
        return p

    def stop(self, p):
        if p.poll() is None:
            p.terminate()  # SIGTERM, so it removes its folder
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        p.stdout.close()

    def wait_ready(self, p, timeout=120) -> str:
        """Its READY line, or "" if it exits or takes longer than timeout (a slow CI runner needs time).
        What it printed before is kept in self.said, for a failure to show."""
        got, self.said = [], []
        def read():
            for line in p.stdout:
                if line.startswith("READY"):
                    got.append(line)
                    return
                self.said.append(line)
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout)
        return got[0] if got else ""

    def test_a_second_harness_bails_before_touching_any_folder(self):
        before = folders()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen()  # a running harness holds the port
        try:
            r = subprocess.run([sys.executable, HARNESS, "--port", str(holder.getsockname()[1])],
                               capture_output=True, text=True, timeout=60)
        finally:
            holder.close()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("busy", (r.stderr + r.stdout).lower())
        self.assertEqual(folders(), before, "it created or wiped a harness folder before failing on the port")

    def test_two_harnesses_started_together_never_both_pass(self):
        for _ in range(3):  # free_port() lets go of the port, and something else on the machine may take it first
            port = free_port()
            a, b = self.start(port), self.start(port)
            deadline = time.time() + 120
            while time.time() < deadline and a.poll() is None and b.poll() is None:
                time.sleep(0.1)
            self.assertTrue(a.poll() is not None or b.poll() is not None, "neither harness gave up the port")
            loser, winner = (a, b) if a.poll() is not None else (b, a)
            self.assertIn("busy", loser.stdout.read())
            line = self.wait_ready(winner)
            if line or not any("busy" in s for s in self.said):
                break  # one kept it: what this test is about.  Both busy: a third one had it, so try another port
        self.assertIn(f"127.0.0.1:{port}/", line, "the harness that kept the port " + (
            f"exited with {winner.poll()}" if winner.poll() is not None else "is still starting")
            + ", after it said:\n" + "".join(self.said[-40:]))

    def test_it_cleans_up_when_terminated(self):
        before = folders()
        p = self.start(free_port())
        self.assertTrue(self.wait_ready(p).startswith("READY"))
        mine = folders() - before
        self.assertEqual(len(mine), 1)
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(30), 0)
        self.assertFalse(mine & folders(), "its folder is left behind")

    def test_the_sample_agents_start(self):
        """--seed, which the UI checks in CLAUDE.md use: its images must pass the panel's own checks."""
        p = self.start(0, "--seed")
        self.assertTrue(self.wait_ready(p).startswith("READY"), "".join(self.said[-20:]))

    def test_port_0_picks_a_free_port(self):
        line = self.wait_ready(self.start(0))
        self.assertRegex(line, r"^READY http://127\.0\.0\.1:\d+/#t=testtoken")
        self.assertNotIn(":0/", line)


if __name__ == "__main__":
    unittest.main()
