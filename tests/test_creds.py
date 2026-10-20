import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from cilly import config as C
from cilly import creds


class FakeCreds:
    """Stands in for systemd-creds: 'encrypts' by reversing, checks the --name on decrypt."""

    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def __call__(self, argv, input=None, **kw):
        self.calls.append(argv)
        assert argv[:2] == ["systemd-creds", "--user"] and argv[2].startswith("--name=cilly-")
        verb = argv[3]
        if verb == self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"Unknown option --user")
        if verb == "encrypt":
            with open(argv[5], "wb") as f:
                f.write(argv[2].encode() + b"\n" + input[::-1])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with open(argv[4], "rb") as f:
            name, data = f.read().split(b"\n", 1)
        if name.decode() != argv[2]:
            return subprocess.CompletedProcess(argv, 1, b"", b"name mismatch")
        return subprocess.CompletedProcess(argv, 0, data[::-1], b"")


class CredsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.cfg = C.load()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_store_points_config_at_decrypt(self):
        run = FakeCreds()
        ref = creds.store(self.cfg, creds.resolve(self.cfg, "claude"), "  sk-ant-oat01-abc  \n", run=run)
        cmd = ref["command"]
        self.assertEqual(cmd[:4], ["systemd-creds", "--user", "--name=cilly-agent_claude_CLAUDE_CODE_OAUTH_TOKEN", "decrypt"])
        self.assertEqual(self.cfg["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], ref)
        self.assertEqual(os.stat(cmd[4]).st_mode & 0o777, 0o600)
        with open(cmd[4], "rb") as f:
            self.assertNotIn(b"sk-ant-oat01-abc", f.read())  # not stored in clear
        self.assertEqual(run(cmd).stdout.decode(), "sk-ant-oat01-abc")  # what the panel will run
        C.validate(self.cfg)
        self.assertEqual(creds.describe(ref, creds.resolve(self.cfg, "claude")), "stored with systemd-creds (no prompt)")
        self.assertFalse([p for p in os.listdir(os.path.dirname(cmd[4])) if p.endswith(".new")])

    def test_failures_change_nothing(self):
        key = creds.resolve(self.cfg, "research")
        creds.store(self.cfg, key, "first", run=FakeCreds())
        before = json.dumps(self.cfg)
        for fail in ("encrypt", "decrypt"):
            with self.assertRaises(SystemExit):
                creds.store(self.cfg, key, "second", run=FakeCreds(fail=fail))
            self.assertEqual(json.dumps(self.cfg), before)
            ref = self.cfg["research"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"]
            self.assertEqual(FakeCreds()(ref["command"]).stdout.decode(), "first")  # the old one still works
        for bad in ("", "two\nlines"):
            with self.assertRaises(SystemExit):
                creds.store(self.cfg, key, bad, run=FakeCreds())

    def test_available_needs_256(self):
        def fake(ver):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, f"systemd {ver} (x)\n+PAM".encode(), b"")
        self.assertTrue(creds.available(run=fake(258)))
        self.assertFalse(creds.available(run=fake(255)))  # no --user before 256

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        self.assertFalse(creds.available(run=missing))

    def test_aliases_forget_and_unknown(self):
        self.assertEqual(creds.resolve(self.cfg, "github"), "token:bot")
        with self.assertRaises(SystemExit):
            creds.resolve(self.cfg, "nope")
        key = creds.resolve(self.cfg, "github")
        ref = creds.store(self.cfg, key, "ghp_x", run=FakeCreds())
        creds.forget(self.cfg, key)
        self.assertEqual(self.cfg["tokens"]["bot"]["secret"], {})
        self.assertFalse(os.path.exists(ref["command"][4]))
        self.assertEqual(creds.describe({}, "x"), "pasted in the panel each start")


class FakeKeyring:
    """Stands in for secret-tool and the login keyring behind it."""

    def __init__(self, fail=None):
        self.items, self.calls, self.fail = {}, [], fail

    def __call__(self, argv, input=None, **kw):
        self.calls.append(argv[:2])
        assert argv[0] == "secret-tool" and argv[2:5] == ["service", "cilly", "key"] or argv[1] == "store"
        if argv[1] == self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"Cannot autolaunch D-Bus without X11 $DISPLAY")
        if argv[1] == "store":
            assert argv[2].startswith("--label=cilly: ") and argv[3:6] == ["service", "cilly", "key"]
            self.items[argv[6]] = input.decode()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[1] == "lookup":
            v = self.items.get(argv[5])
            return subprocess.CompletedProcess(argv, 0 if v is not None else 1, (v or "").encode(), b"")
        self.items.pop(argv[5], None)  # clear
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class KeyringTests(unittest.TestCase):
    """systemd before 256 (Ubuntu 24.04, Debian 12) has no systemd-creds --user: the login keyring instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.cfg = C.load()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_backend_prefers_systemd_creds(self):
        ver = lambda v: (lambda argv, **kw: subprocess.CompletedProcess(argv, 0, f"systemd {v} (x)".encode(), b""))  # noqa: E731
        have = lambda name: "/usr/bin/secret-tool"  # noqa: E731
        none = lambda name: None  # noqa: E731
        self.assertEqual(creds.backend(run=ver(258), which=have), "systemd-creds")
        self.assertEqual(creds.backend(run=ver(255), which=have), "secret-tool")
        self.assertIsNone(creds.backend(run=ver(255), which=none))
        self.assertFalse(creds.available(run=ver(252), which=none))

    def test_store_read_back_and_forget(self):
        ring = FakeKeyring()
        key = creds.resolve(self.cfg, "claude")
        ref = creds.store(self.cfg, key, " sk-ant-oat01-kr \n", run=ring, kind="secret-tool")
        self.assertEqual(ref["command"], ["secret-tool", "lookup", "service", "cilly", "key", key])
        self.assertEqual(ring.items[key], "sk-ant-oat01-kr")
        self.assertNotIn("sk-ant-oat01-kr", json.dumps(self.cfg))
        self.assertTrue(creds.is_ours(ref, key))
        self.assertFalse(creds.is_ours({"command": ["secret-tool", "lookup", "service", "cilly", "key", "other"]}, key))
        self.assertEqual(creds.describe(ref, key), "stored in your login keyring")
        # your own keyring or systemd-creds command is a command like any other: cilly didn't save it
        from cilly.secretstore import SecretStore
        for mine in (["secret-tool", "lookup", "service", "myvault", "key", "claude"], ["systemd-creds", "decrypt", "/home/me/c.cred", "-"]):
            self.assertEqual(creds.describe({"command": mine}, key), "command: " + " ".join(mine))
            self.assertEqual(SecretStore().status(key, {"command": mine}), "command (not loaded yet)")
        C.validate(self.cfg)
        from cilly.secretstore import SecretStore
        self.assertEqual(SecretStore().status(key, ref), "saved on this PC")
        creds.forget(self.cfg, key, run=ring)
        self.assertEqual((self.cfg["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], ring.items), ({}, {}))

    def test_failures_change_nothing(self):
        key = creds.resolve(self.cfg, "research")
        creds.store(self.cfg, key, "first", run=FakeKeyring(), kind="secret-tool")
        before = json.dumps(self.cfg)
        for fail, why in (("store", "is your login keyring running?"), ("lookup", "didn't read back")):
            with self.assertRaises(SystemExit) as cm:
                creds.store(self.cfg, key, "second", run=FakeKeyring(fail=fail), kind="secret-tool")
            self.assertIn(why, str(cm.exception))
            self.assertEqual(json.dumps(self.cfg), before)

    def test_a_keyring_that_doesnt_answer_is_a_clear_error(self):
        key = creds.resolve(self.cfg, "claude")

        def hangs(argv, input=None, **kw):  # stored, then the read-back waits on a locked keyring
            if argv[1] == "store":
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        before = json.dumps(self.cfg)
        with self.assertRaises(SystemExit) as cm:
            creds.store(self.cfg, key, "sk-ant-oat01-kr", run=hangs, kind="secret-tool")
        self.assertIn("reading it back failed (TimeoutExpired)", str(cm.exception))
        self.assertEqual(json.dumps(self.cfg), before)
