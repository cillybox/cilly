"""Runs the real egress_proxy.py and relay.py scripts the way QEMU would."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from cilly.vm import PKG_DIR, RELAY


class RelayProxyIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self.tmp.name, "proxy.sock")
        self.log = os.path.join(self.tmp.name, "egress.log")
        self.proxy = subprocess.Popen([sys.executable, "-I", os.path.join(PKG_DIR, "egress_proxy.py"),
                                       "--socket", self.sock, "--log", self.log, "--allow", "api.github.com"])
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)

    def tearDown(self):
        self.proxy.terminate()
        self.proxy.wait()
        self.tmp.cleanup()

    def relay(self, data: bytes) -> bytes:
        p = subprocess.run([sys.executable, "-I", RELAY, self.sock], input=data, capture_output=True, timeout=10)
        return p.stdout

    def test_socket_is_private(self):
        self.assertEqual(os.stat(self.sock).st_mode & 0o077, 0)

    def test_denied_via_relay(self):
        self.assertIn(b"403", self.relay(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"))
        self.assertIn(b"403", self.relay(b"CONNECT 127.0.0.1:8765 HTTP/1.1\r\n\r\n"))
        self.assertIn(b"403", self.relay(b"GET http://127.0.0.1:8765/api/state HTTP/1.1\r\n\r\n"))
        time.sleep(0.2)
        with open(self.log) as f:
            log = f.read()
        self.assertEqual(log.count('"action": "deny"'), 3)


    def test_unread_never_reach_file_lets_nothing_through(self):
        sock, log = os.path.join(self.tmp.name, "p2.sock"), os.path.join(self.tmp.name, "p2.log")
        p = subprocess.Popen([sys.executable, "-I", os.path.join(PKG_DIR, "egress_proxy.py"), "--socket", sock, "--log", log,
                              "--allow", "api.github.com", "--never-reach-file", os.path.join(self.tmp.name, "missing.txt")])
        try:
            for _ in range(100):
                if os.path.exists(sock):
                    break
                time.sleep(0.05)
            out = subprocess.run([sys.executable, "-I", RELAY, sock], input=b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n",
                                 capture_output=True, timeout=10).stdout
            self.assertIn(b"403", out)
            time.sleep(0.2)
            with open(log) as f:
                self.assertIn('"code": "no_route_info"', f.read())
        finally:
            p.terminate()
            p.wait()

if __name__ == "__main__":
    unittest.main()
