"""SSH tunnels to model servers: cilly's own key and known-hosts file, never yours, a host key you
confirm, ssh in bubblewrap without your home, the route as its ProxyCommand, and nothing sent to the
tunnel's port unless you hold it."""

import base64
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import egress_proxy as E  # noqa: E402
from cilly import helpers as HP  # noqa: E402
from cilly import models as M  # noqa: E402
from cilly import sshtunnel as ST  # noqa: E402

FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBERERERERERERERERERERERERERERERERERERERERER"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIi"
SSH = {"host": "gpu.example.net", "port": 2222, "user": "ubuntu", "route": "direct"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.run_dir, self.log_dir = f"{t}/run/ssh", f"{t}/data/ssh"
        os.makedirs(f"{t}/run")
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": f"{t}/data", "XDG_RUNTIME_DIR": f"{t}/run",
                                           "PATH": FAKE_BIN + os.pathsep + os.environ["PATH"],
                                           "SSH_AUTH_SOCK": f"{t}/agent.sock", "FAKE_SSH_LOG": f"{t}/ssh.log"})
        env.start()
        self.addCleanup(env.stop)
        clean = ST.Tunnel.env  # ssh gets a clean environment: the fake ssh still needs its own settings
        fake = mock.patch.object(ST.Tunnel, "env", lambda t: dict(clean(t), **{k: v for k, v in os.environ.items()
                                                                               if k.startswith("FAKE_SSH")}))
        fake.start()
        self.addCleanup(fake.stop)

    def ssh_calls(self):
        try:
            with open(f"{self.tmp.name}/ssh.log") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class CheckTests(Base):
    def test_what_settings_take(self):
        self.assertEqual(ST.check_ssh(dict(SSH, host="GPU.example.net")), SSH)
        self.assertEqual(ST.check_ssh({"host": "10.0.0.5", "user": "root"})["port"], 22)
        for bad in (dict(SSH, host="-oProxyCommand=sh"), dict(SSH, host="a b"), dict(SSH, host=""),
                    dict(SSH, user="me; rm"), dict(SSH, user="-o"), dict(SSH, port=0), dict(SSH, port="22"),
                    dict(SSH, route="../x"), dict(SSH, extra=1), dict(SSH, host_key="ssh-ed25519 notbase64"),
                    dict(SSH, host_key="ssh-dss AAAA"), dict(SSH, public_key=HOST_KEY + " me@laptop")):
            with self.assertRaises(ValueError, msg=bad):
                ST.check_ssh(bad)

    def test_a_host_key_and_its_fingerprint(self):
        blob = base64.b64decode(HOST_KEY.split()[1])
        want = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")  # as ssh-keygen -l
        self.assertEqual(ST.fingerprint(HOST_KEY), want)
        self.assertEqual(ST.check_host_key(HOST_KEY), HOST_KEY)
        with self.assertRaises(ValueError):  # the type inside the key must be the one it says
            ST.check_host_key("ecdsa-sha2-nistp256 " + HOST_KEY.split()[1])

    def test_the_private_key_goes_to_secrets_as_one_line(self):
        private, public = ST.make_key("gpu")
        self.assertNotIn("\n", private)
        self.assertTrue(public.startswith("ssh-ed25519 ") and public.endswith(" cilly-gpu"))
        text = ST.unpack_key(private)
        self.assertTrue(text.startswith(ST.KEY_BEGIN + "\n") and text.endswith(ST.KEY_END + "\n"))
        self.assertEqual(ST.pack_key(text), private)
        self.assertEqual([n for n in os.listdir(tempfile.gettempdir()) if n.startswith("cilly-key-")], [])  # nothing left behind

    def test_the_server_may_only_forward_to_the_model_server(self):
        line = ST.authorized_line("ssh-ed25519 AAAA cilly-gpu", "127.0.0.1", 11434)
        self.assertEqual(line, 'restrict,port-forwarding,permitopen="127.0.0.1:11434" ssh-ed25519 AAAA cilly-gpu')

    def test_who_listens_on_a_port(self):
        path = os.path.join(self.tmp.name, "tcp")
        with open(path, "w") as f:
            f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
                    "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000\n"
                    "   1: 0100007F:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001\n"
                    "   2: 0100007F:1F92 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1000\n")
        self.assertTrue(ST.listening(0x1F90, 1000, [path]))
        self.assertFalse(ST.listening(0x1F91, 1000, [path]))  # another account's
        self.assertFalse(ST.listening(0x1F92, 1000, [path]))  # a connection, not a listener


class TunnelTests(Base):
    def tunnel(self, ssh=None, key=None, route=None, target=("127.0.0.1", 11434)):
        key = key or ST.make_key("gpu")[0]
        return ST.Tunnel("gpu", ssh or dict(SSH, host_key=HOST_KEY), target, route or {"kind": "direct"}, key,
                         self.run_dir, self.log_dir, lambda: "off")

    def test_nothing_of_yours_is_used(self):
        t = self.tunnel()
        t.prepare()
        argv = t.argv("/usr/bin/ssh", None)
        opts = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
        for want in ("BatchMode=yes", "IdentityAgent=none", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                     "GlobalKnownHostsFile=/dev/null", f"UserKnownHostsFile={t.dir}/known_hosts", "HostKeyAlias=cilly-gpu",
                     "ForwardAgent=no", "PermitLocalCommand=no", "ControlPath=none", "ExitOnForwardFailure=yes",
                     "PasswordAuthentication=no"):
            self.assertIn(want, opts)
        self.assertEqual(argv[1:4], ["-N", "-F", "/dev/null"])  # no command on the server, no config of yours
        self.assertEqual(argv[argv.index("-i") + 1], f"{t.dir}/key")
        self.assertEqual(argv[argv.index("-L") + 1], f"127.0.0.1:{t.port}:127.0.0.1:11434")  # this PC's loopback only
        self.assertEqual(argv[-2:], ["--", "ubuntu@gpu.example.net"])  # nothing after it is read as an option
        self.assertFalse(any(os.path.expanduser("~/.ssh") in a for a in argv))
        self.assertFalse(any(o.startswith("ProxyCommand") for o in opts))  # straight: ssh connects itself
        self.assertNotIn("SSH_AUTH_SOCK", t.env())
        mode = stat.S_IMODE(os.stat(f"{t.dir}/key").st_mode)
        self.assertEqual(mode, 0o600)
        with open(f"{t.dir}/known_hosts") as f:
            self.assertEqual(f.read(), f"cilly-gpu {HOST_KEY}\n")  # the key you confirmed, and only that one

    def test_not_before_the_host_key_is_confirmed(self):
        with self.assertRaises(ST.TunnelError):
            self.tunnel(ssh=SSH)

    def test_in_bubblewrap_it_sees_no_home(self):
        started = []

        class Proc:
            stdout, args = iter(()), None
            def poll(self): return None
            def wait(self, *a): return 0
            def terminate(self): pass
        tor = os.path.join(self.tmp.name, "run", "tor", "socks.sock")
        t = ST.Tunnel("gpu", dict(SSH, host_key=HOST_KEY, route="tor"), ("127.0.0.1", 11434),
                      {"kind": "tor", "name": "Tor", "unix": tor}, ST.make_key("gpu")[0], self.run_dir, self.log_dir)
        with mock.patch.object(HP.shutil, "which", lambda n: "/usr/bin/" + n), \
                mock.patch.object(ST.shutil, "which", lambda n: "/usr/bin/" + n), \
                mock.patch.object(HP.H, "bwrap_check", lambda *a, **kw: None), \
                mock.patch.object(HP.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw)) or Proc()), \
                mock.patch.object(HP.threading, "Thread"):
            t.ensure()
        argv, kw = started[0]
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        binds = [(argv[i], argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
        self.assertIn(("--bind", t.dir, t.dir), binds)
        self.assertIn(("--ro-bind", f"{t.dir}/passwd", "/etc/passwd"), binds)  # a passwd of its own, home in its folder
        self.assertIn(("--ro-bind", ST.PKG_DIR, ST.PKG_DIR), binds)  # for the route pipe
        self.assertIn(("--ro-bind", os.path.dirname(tor), os.path.dirname(tor)), binds)  # cilly's Tor
        self.assertFalse(any(b[1] in ("/home", os.path.expanduser("~"), "/root") for b in binds))
        self.assertNotIn("SSH_AUTH_SOCK", kw["env"])
        pc = next(a for a in argv if a.startswith("ProxyCommand="))
        self.assertIn("egress_proxy.py --pipe gpu.example.net:2222 --route-file", pc)
        self.assertIn(f"{t.dir}/route.json", pc)
        with open(f"{t.dir}/route.json") as f:
            self.assertEqual(json.load(f)["unix"], tor)
        with open(f"{t.dir}/passwd") as f:
            self.assertEqual(f.read(), f"cilly:x:{os.getuid()}:{os.getgid()}:cilly:{t.dir}/home:/bin/sh\n")
        t.stop()
        self.assertFalse(os.path.exists(f"{t.dir}/key"))  # only while it runs
        self.assertFalse(os.path.exists(f"{t.dir}/route.json"))

    def test_fetching_the_host_key(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_HOST_KEY": HOST_KEY}):
            got = ST.fetch_host_key("gpu", SSH, {"kind": "direct"}, self.run_dir, self.log_dir, lambda: "off")
        self.assertEqual(got, HOST_KEY)
        call = self.ssh_calls()[-1]
        opts = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "-o"]
        self.assertIn("StrictHostKeyChecking=accept-new", opts)
        self.assertIn("PubkeyAuthentication=no", opts)  # it doesn't try to log in
        self.assertNotIn("-i", call["argv"])
        self.assertNotIn("SSH_AUTH_SOCK", call["env"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "gpu-scan")))

    def test_a_tunnel_to_a_model_server(self):
        from tests.test_models import FakeOllama
        srv = FakeOllama()
        self.addCleanup(srv.close)
        port = int(srv.url.rsplit(":", 1)[1])
        with mock.patch.dict(os.environ, {"FAKE_SSH_HOST_KEY": HOST_KEY}):
            t = self.tunnel(target=("127.0.0.1", port))
            self.addCleanup(t.stop)
            t.ensure()
            t.wait(20)
            self.assertTrue(t.status()["ready"])
            M.OWN_PORTS.add(t.port)
            self.assertEqual(M.list_models(f"http://127.0.0.1:{t.port}"), ["gpt-oss:20b", "qwen3-coder:30b"])

    def test_a_changed_host_key_or_a_refused_key_says_why(self):
        for env, words in (({"FAKE_SSH_HOST_KEY": OTHER_KEY}, "isn't the one you confirmed"),
                           ({"FAKE_SSH_HOST_KEY": HOST_KEY, "FAKE_SSH_DENY": "1"}, "authorized_keys")):
            with mock.patch.dict(os.environ, env):
                t = self.tunnel()
                t.ensure()
                with self.assertRaises(ST.TunnelError) as cm:
                    t.wait(10)
                t.stop()
            self.assertIn(words, str(cm.exception))


class OwnPortTests(Base):
    def test_the_panel_sends_nothing_to_a_port_someone_else_took(self):
        from tests.test_models import FakeOllama
        srv = FakeOllama()
        self.addCleanup(srv.close)
        port = int(srv.url.rsplit(":", 1)[1])
        M.OWN_PORTS.add(port)
        self.addCleanup(M.OWN_PORTS.discard, port)
        self.assertEqual(M.list_models(srv.url), ["gpt-oss:20b", "qwen3-coder:30b"])  # held by us: fine
        with mock.patch.object(E, "loopback_owner", lambda s: os.getuid() + 1):
            with self.assertRaises(M.ModelServerError) as cm:
                M.list_models(srv.url, "sk-secret")
        self.assertIn("isn't held by cilly's ssh", str(cm.exception))
        self.assertEqual(len([s for s in srv.seen if s[2] == "Bearer sk-secret"]), 0)  # the key never went


class PipeTests(Base):
    def socks(self, login=None):
        """A SOCKS5 proxy that records what it was asked for and echoes what it gets."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)
        self.asked = []

        def serve():
            c, _ = srv.accept()
            c.recv(3)
            if login:
                c.sendall(b"\x05\x02")
                c.recv(512)
                c.sendall(b"\x01\x00")
            else:
                c.sendall(b"\x05\x00")
            head = c.recv(4)
            n = c.recv(1)[0]
            self.asked.append((head[3], c.recv(n).decode(), int.from_bytes(c.recv(2), "big")))
            c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            while data := c.recv(100):
                c.sendall(data.upper())
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        return srv.getsockname()[1]

    def route_file(self, route):
        path = os.path.join(self.tmp.name, "route.json")
        with open(path, "w") as f:
            json.dump(route, f)
        return path

    def test_ssh_goes_through_the_route_with_the_name_looked_up_there(self):
        port = self.socks()
        path = self.route_file({"kind": "socks5", "name": "proxy", "host": "127.0.0.1", "port": port})
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        os.write(w_in, b"ssh-2.0-hello")
        os.close(w_in)
        with mock.patch.object(E, "loopback_owner", lambda s: os.getuid()), \
                os.fdopen(r_in, "rb") as fin, os.fdopen(w_out, "wb") as fout:
            code = E.pipe("gpu.example.net:2222", path, stdin=fin, stdout=fout)
        self.assertEqual(code, 0)
        self.assertEqual(self.asked, [(3, "gpu.example.net", 2222)])  # a name: looked up at the far end, not here
        self.assertEqual(os.read(r_out, 100), b"SSH-2.0-HELLO")

    def test_no_route_means_no_connection(self):
        for route in (None, {"kind": "direct"}, {"kind": "socks5"}):
            path = self.route_file(route) if route else os.path.join(self.tmp.name, "missing.json")
            with mock.patch.object(E, "dial_route", side_effect=AssertionError("dialed")):
                self.assertEqual(E.pipe("gpu.example.net:22", path), 1)
        for bad in ("gpu.example.net", "-oProxyCommand=x:22", "a b:22", "host:0"):
            self.assertEqual(E.pipe(bad, path), 2)

    def test_a_route_proxy_on_this_pc_must_be_yours(self):
        port = self.socks()
        path = self.route_file({"kind": "socks5", "name": "vpn", "host": "127.0.0.1", "port": port})
        with mock.patch.object(E, "loopback_owner", lambda s: os.getuid() + 1):
            self.assertEqual(E.pipe("gpu.example.net:22", path), 1)
        self.assertEqual(self.asked, [])


class ProxyTests(unittest.TestCase):
    def test_a_tunnel_entry(self):
        base = {"hosts": {"gpu.model.cilly.internal": {"cert": "/c", "key": "/k"}}}
        got = E.parse_tokens(dict(base, models={"gpu.model.cilly.internal": {
            "url": "http://127.0.0.1:40000", "key": None, "model": "m", "tunnel": True, "source": "http://127.0.0.1:11434"}}))
        self.assertTrue(got["models"]["gpu.model.cilly.internal"]["tunnel"])
        got = E.parse_tokens(dict(base, models={"gpu.model.cilly.internal": {"url": "http://127.0.0.1:40000", "model": "m"}}))
        self.assertFalse(got["models"]["gpu.model.cilly.internal"]["tunnel"])


class ConfigTests(unittest.TestCase):
    def test_a_server_over_ssh(self):
        ok = {"gpu": {"url": "http://127.0.0.1:11434", "api_key": False, "secret": {}, "ssh": dict(SSH), "ssh_secret": {}}}
        M.validate(ok, 8765)
        M.validate({"gpu": dict(ok["gpu"], url="http://127.0.0.1:8765")}, 8765)  # the server's own loopback, not this PC's
        for bad in (dict(ok["gpu"], url="https://127.0.0.1:11434"), dict(ok["gpu"], ssh=dict(SSH, host="-x")),
                    dict(ok["gpu"], ssh=dict(SSH, host="GPU.example.net")), dict(ok["gpu"], other=1)):
            with self.assertRaises(ValueError, msg=bad):
                M.validate({"gpu": bad}, 8765)


if __name__ == "__main__":
    unittest.main()
