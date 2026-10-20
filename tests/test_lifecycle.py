"""End-to-end agent lifecycles against a fake QEMU (no KVM needed)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")
REPO = "me/repo"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        env = {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
               "XDG_RUNTIME_DIR": os.path.join(t, "run"), "PATH": FAKE_BIN + os.pathsep + os.environ["PATH"],
               "FAKE_MEMOUT": "good", "FAKE_EXPORT": "good", "FAKE_ATTACH": "ok"}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for f in os.listdir(FAKE_BIN):
            os.chmod(os.path.join(FAKE_BIN, f), 0o755)
        from cilly import config as C
        from cilly import vm as V
        self.C, self.V = C, V
        self.cfg = C.load()
        self.cfg["vm"]["use_bwrap"] = "off"
        open(os.path.join(V.images_dir(), "base-test.qcow2"), "w").close()
        self.tools = mock.patch.object(V, "detect_tools", lambda cfg: {
            "qemu": os.path.join(FAKE_BIN, "qemu-system-x86_64"), "qemu-img": os.path.join(FAKE_BIN, "qemu-img"),
            "bwrap": None, "virtiofsd": None, "xorriso": None, "kvm": True})
        self.tools.start()
        self.mgr = V.Manager(lambda: self.cfg)

    def tearDown(self):
        self.mgr.shutdown_all()
        self.tools.stop()
        self.env.stop()
        self.tmp.cleanup()

    # -- helpers ------------------------------------------------------------
    def wait(self, cond, what, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def task(self, instructions="do it", **kw):
        return dict({"instructions": instructions, "branch": "agent/x", "new_branch": False, "continue": True,
                     "agent_argv": ["claude", "-p", "{instructions}"], "continue_args": ["--continue"],
                     "agent_env": {}, "env": {"GH_TOKEN": "tok-123", "ANTHROPIC_API_KEY": "sk-ant-secret"},
                     "permission_mode": "bypassPermissions", "timeout_s": 60, "pr_title": ""}, **kw)

    def make(self, mode, auto_stop=False):
        return self.mgr.create(name="t", mode=mode, repo=REPO, agent="claude", cpus=1, memory_mb=1024,
                               auto_stop=auto_stop, branch_prefix="agent/")

    def start(self, a, first=None):
        self.mgr.start(a.id, allow=["github.com"], policy=self.C.effective_policy(self.cfg), first_task=first)

    def log(self, a):
        return self.mgr.read_log(a.id, "agent", 0)["text"]

    def assert_no_secret_on_disk(self):
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                p = os.path.join(root, f)
                if os.path.isfile(p) and not os.path.islink(p):
                    try:
                        with open(p, "rb") as fh:
                            data = fh.read()
                    except FileNotFoundError:
                        continue  # a temp file replaced while we walked
                    self.assertNotIn(b"tok-123", data, p)
                    self.assertNotIn(b"sk-ant-secret", data, p)

    # -- tests ---------------------------------------------------------------
    def test_ephemeral_first_task_autostop(self):
        a = self.make("ephemeral", auto_stop=True)
        self.assertIsNone(a.meta["branch"])  # kind/name: the first task names it
        t = self.mgr.new_task(a.id, self.task(branch="fix/menu", timeout_s=4321,
                                              agent_env={"BASH_MAX_TIMEOUT_MS": "2700000"}), queue=True)
        self.start(a)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "agent to stop")
        rec = a.meta["tasks"][-1]
        self.assertEqual(rec["state"], "done", rec)
        self.assertEqual(rec["pr"], f"https://github.com/{REPO}/pull/7")
        self.assertEqual(a.meta["pr"], f"https://github.com/{REPO}/pull/7")
        self.assertEqual(a.meta["branch"], "fix/menu")  # spoofed branch "main" ignored
        self.assertTrue(a.meta["selftest"]["direct TCP blocked"])
        log = self.log(a)
        self.assertIn("env=ANTHROPIC_API_KEY,GH_TOKEN", log)
        self.assertIn("timeout=4321 bash=2700000", log)  # the agent's limits, in the clear: they aren't secrets
        self.assertIn("policy mode=bypassPermissions", log)
        self.assertFalse(os.path.exists(a.paths.disk), "ephemeral disk must be deleted")
        self.assertEqual(t["env"], {}, "secrets dropped after sending")
        self.assert_no_secret_on_disk()

    def test_the_route_reaches_the_proxy_in_memory_only(self):
        route = {"kind": "socks5", "name": "vpn", "host": "10.64.0.1", "port": 1080, "user": "me", "password": "route-pw-123"}
        self.mgr.route_of = lambda a: dict(route)
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        argv = a.procs["proxy"].args
        self.assertEqual(argv[argv.index("--route-file") + 1], a.paths.route_file)
        self.assertEqual(argv[argv.index("--isolate") + 1], a.id)  # its own Tor circuits
        self.assertTrue(a.paths.route_file.startswith(os.environ["XDG_RUNTIME_DIR"]))  # tmpfs, not the data folder
        for seen in (a.paths.run_dir, a.paths.sandbox_dir):  # not where an escaped QEMU could reach it
            self.assertFalse(a.paths.route_file.startswith(seen + os.sep), seen)
        self.assertEqual(os.stat(a.paths.route_file).st_mode & 0o777, 0o600)
        with open(a.paths.route_file) as f:
            self.assertEqual(json.load(f), route)
        self.assertEqual(a.meta["route_now"], "vpn")
        route["kind"] = "tor"
        self.mgr.write_route(a)  # live, while it runs
        with open(a.paths.route_file) as f:
            self.assertEqual(json.load(f)["kind"], "tor")
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stopped")
        self.assertFalse(os.path.exists(a.paths.route_file))  # gone when it stops
        for root, _, files in os.walk(self.tmp.name):  # the login never reached the disk
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    self.assertNotIn(b"route-pw-123", fh.read(), os.path.join(root, f))

    def test_tokens_stay_on_this_pc(self):
        """A start with token hosts gets its own CA, the VM gets only that CA's certificate, and the
        real tokens go to the proxy in a file outside QEMU's sandbox, gone after Stop."""
        from cilly import tlsca
        if not tlsca.openssl_binary():
            self.skipTest("needs openssl")
        self.mgr.token_hosts = lambda a: ["api.anthropic.com", "github.com", "api.github.com"]
        a = self.make("persistent")
        stand = self.mgr.stand_in(a, "ghp_" + "R" * 36)
        self.assertTrue(stand.startswith("ghp_cilly") and "R" not in stand)
        self.assertEqual(self.mgr.stand_in(a, "ghp_" + "R" * 36), stand)  # the same for every task
        self.mgr.set_tokens(a, [{"stand_in": stand, "value": "ghp_" + "R" * 36, "hosts": ["github.com", "api.github.com"],
                                 "github": {"repo": REPO, "push": {"agent": a.id, "create": None}}}])
        self.assertFalse(os.path.exists(a.paths.tokens_file))  # no certificates before the start, so no file
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIn("boot ca=yes", self.log(a))
        argv = a.procs["proxy"].args
        self.assertEqual(argv[argv.index("--tokens-file") + 1], a.paths.tokens_file)
        self.assertEqual(argv[argv.index("--branches-file") + 1], a.paths.branches_file)  # the branches it created, kept
        with open(a.paths.tokens_file) as f:
            doc = json.load(f)
        self.assertEqual(sorted(doc["hosts"]), ["api.anthropic.com", "api.github.com", "github.com"])
        self.assertEqual(doc["tokens"][0]["stand_in"], stand)
        from cilly.egress_proxy import parse_tokens
        parse_tokens(doc)  # what the proxy reads
        for f in (a.paths.tokens_file, a.paths.tls_dir):
            self.assertTrue(f.startswith(os.environ["XDG_RUNTIME_DIR"]))  # in memory
            for seen in (a.paths.run_dir, a.paths.sandbox_dir):  # out of an escaped QEMU's reach
                self.assertFalse(f.startswith(seen + os.sep), (f, seen))
        self.assertEqual(os.stat(a.paths.tokens_file).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(os.path.join(a.paths.tls_dir, "ca.key")))
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stopped")
        self.assertFalse(os.path.exists(a.paths.tokens_file))
        self.assertFalse(os.path.exists(a.paths.tls_dir))
        for root, _, files in os.walk(self.tmp.name):  # the real token never reached the disk
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    self.assertNotIn(b"R" * 36, fh.read(), os.path.join(root, f))
        # without openssl, the start is refused rather than handing the VM real tokens
        from unittest import mock as M
        with M.patch.object(tlsca, "openssl_binary", lambda: None):
            with self.assertRaises(self.V.VMError) as cm:
                self.start(a)
        self.assertIn("Install openssl", str(cm.exception))
        self.mgr.token_hosts = lambda a: []
        self.start(a)  # the setting off: no CA
        self.wait(lambda: a.meta["state"] == "ready", "ready without")
        self.assertIn("boot ca=no", self.log(a))

    def test_a_model_server_without_keeping_tokens(self):
        """A model server needs the CA too: with Keep tokens on this PC off, the start's certificates
        are for model servers only, and the proxy's file has only the server this agent's model is on."""
        from cilly import tlsca
        if not tlsca.openssl_binary():
            self.skipTest("needs openssl")
        self.mgr.token_hosts = lambda a: []
        self.mgr.model_hosts = lambda a: ["gpu.model.cilly.internal", "lab.model.cilly.internal"]
        a = self.make("persistent")
        entry = {"gpu.model.cilly.internal": {"url": "http://192.168.1.50:11434", "key": "sk-" + "K" * 30, "model": "qwen3-coder:30b"}}
        a.models = dict(entry)  # left from an earlier start: a start begins without any
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIn("boot ca=yes", self.log(a))
        self.assertFalse(a.tls["tokens"])  # certificates, but this start doesn't keep tokens
        with open(a.paths.tokens_file) as f:
            self.assertEqual(json.load(f)["models"], {})
        # the entry goes along with a task and takes effect when it's sent, never to the VM
        self.mgr.new_task(a.id, dict(self.task(branch=a.meta["branch"]), proxy_models=dict(entry)), queue=True)
        self.wait(lambda: "task " in self.log(a), "task sent")
        self.assertNotIn("LEAK", self.log(a))
        self.assertEqual(a.models, entry)
        with open(a.paths.tokens_file) as f:
            doc = json.load(f)
        self.assertEqual((sorted(doc["hosts"]), doc["tokens"], list(doc["models"])),
                         (["gpu.model.cilly.internal", "lab.model.cilly.internal"], [], ["gpu.model.cilly.internal"]))
        from cilly.egress_proxy import parse_tokens
        parse_tokens(doc)
        self.mgr.set_models(a, {})  # back to a Claude model: the server is out of reach at once
        with open(a.paths.tokens_file) as f:
            self.assertEqual(json.load(f)["models"], {})
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stopped")
        for root, _, files in os.walk(self.tmp.name):  # the key never reached the disk once the agent stopped
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    self.assertNotIn(b"K" * 30, fh.read(), os.path.join(root, f))

    def test_the_agents_summary_reaches_the_list(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]), queue=True)
        self.wait(lambda: a.meta.get("summary") == "Added per-site ports, tests pass, PR open", "summary")
        saves = []
        save = a.save_meta
        a.save_meta = lambda: (saves.append(a.meta.get("summary")), save())
        with mock.patch.object(self.V, "SUMMARY_GAP", 1.0):  # a hostile guest: 200 summaries of several lines
            self.mgr.new_task(a.id, self.task("flood summaries", branch=a.meta["branch"]), queue=True)
            self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task")
            time.sleep(1.5)
        stored = [x for i, x in enumerate(saves) if not i or x != saves[i - 1]]
        self.assertLessEqual(len(set(stored)), 3, stored)
        self.assertNotIn("\n", "".join(stored))
        self.assertEqual(a.meta["summary"], "Added per-site ports, tests pass, PR open")  # the newest wins

    def test_the_boot_time_is_kept_for_started_in(self):
        a = self.make("persistent")
        a.set(boot_secs=99)  # from the last start
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIsInstance(a.meta["boot_secs"], int)
        self.assertLess(a.meta["boot_secs"], 99)

    def test_the_boot_time_is_split_into_its_steps(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        steps = [name for name, _ in a.meta["boot_times"]]
        for want in ("Preparing", "Disk", "QEMU and firmware", "Policy and setup", "Self-test"):
            self.assertIn(want, steps)
        self.assertEqual(len(steps), len(set(steps)))  # each step once, though the fake VM sends a phase it doesn't know
        self.assertTrue(all(isinstance(secs, float) and secs >= 0 for _, secs in a.meta["boot_times"]))
        self.assertLessEqual(sum(secs for _, secs in a.meta["boot_times"]), a.meta["boot_secs"] + 1)

    def test_a_second_ready_keeps_the_boot_time(self):
        os.environ["FAKE_READY_AGAIN"] = "1.5"
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        first, windows, times = a.meta["boot_secs"], list(a.meta["selftest_windows"]), a.meta["boot_times"]
        time.sleep(2.5)
        self.assertEqual(a.meta["boot_secs"], first)
        self.assertEqual(a.meta["boot_times"], times)
        self.assertEqual(a.meta["selftest_windows"], windows)

    def test_a_fresh_ephemeral_vm_drops_the_old_summary(self):
        eph, kept = self.make("ephemeral"), self.make("persistent")
        for a in (eph, kept):
            a.set(summary="Old work from the last VM", context={"used": 90000, "window": 200000})
            self.start(a)
        self.wait(lambda: eph.meta["state"] == "ready" and kept.meta["state"] == "ready", "both ready")
        self.assertIsNone(eph.meta["summary"])  # its status.txt went with the old disk
        self.assertEqual(kept.meta["summary"], "Old work from the last VM")
        self.assertEqual(eph.meta["context"], {"used": 0, "window": None})  # its conversation went too
        self.assertEqual(kept.meta["context"], {"used": 90000, "window": 200000})

    def test_the_context_size_follows_the_conversation(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")

        def run(text="do it", record=None, **kw):
            n = len(a.meta.get("tasks", []))
            self.mgr.new_task(a.id, self.task(text, branch=a.meta["branch"], **kw), queue=True, record=record)
            self.wait(lambda: len(a.meta["tasks"]) > n and a.meta["tasks"][-1]["state"] == "done", "task")
            return a.meta["tasks"][-1]
        first = run()
        self.assertEqual(a.meta["context"], {"used": 40000, "window": 200000})  # the hostile reports were refused
        self.assertEqual(first["context"], {"used": 40000, "window": 200000})
        run()
        self.assertEqual(a.meta["context"]["used"], 80000)  # Continue keeps the count
        run("no context", **{"continue": False})
        self.assertEqual(a.meta["context"], {"used": 0, "window": None})  # a new conversation starts at zero
        self.assertEqual(a.public()["context"], {"used": 0, "window": 200000})  # the window by model until it reports
        run("no context", record={"resumed_from": first["id"]}, resume=first["session"], **{"continue": False})
        self.assertEqual(a.meta["context"]["used"], 40000)  # back to that conversation: its count comes back

    def test_an_older_runner_sends_no_summary(self):
        os.environ["FAKE_OLD_RUNNER"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_OLD_RUNNER", None)
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task("# Fix the **build**", branch=a.meta["branch"]), queue=True)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task")
        self.assertNotIn("summary", a.meta)  # the list falls back to the last message
        self.assertNotIn("context", a.public())  # and there's no context meter

    def test_a_streaming_task_still_answers_an_interrupt(self):
        os.environ.update(FAKE_STREAM="40", FAKE_STREAM_DELAY="0.25")  # ten seconds of steps
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_STREAM", "FAKE_STREAM_DELAY")])
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task("stream events", branch=a.meta["branch"]), queue=True)
        self.wait(lambda: a.meta["state"] == "busy", "busy")
        time.sleep(0.5)
        started = time.time()
        with mock.patch.object(self.V, "INTERRUPT_WAIT", 3):
            self.assertEqual(self.mgr.interrupt(a.id), "stopping")
        self.assertLess(time.time() - started, 3)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "interrupted", "interrupted", timeout=5)

    def test_the_host_ends_a_task_the_vm_lets_run_past_its_time_limit(self):
        self.enterContext(mock.patch.object(self.V, "BACKSTOP_GRACE", 0))
        self.enterContext(mock.patch.object(self.V, "BACKSTOP_WAIT", 1))
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task("ignore the limit", branch=a.meta["branch"], timeout_s=1), queue=True)
        self.wait(lambda: a.meta["state"] == "busy", "busy")
        time.sleep(0.5)
        self.assertNotIn("timed_out", a.meta["tasks"][-1])  # not before its time
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "the VM to be stopped", timeout=15)
        rec = a.meta["tasks"][-1]
        self.assertEqual((rec["state"], rec.get("timed_out")), ("interrupted", True))
        self.assertEqual(a.backstops, {})

    def test_the_host_interrupts_a_task_past_its_time_limit_and_keeps_the_vm(self):
        os.environ.update(FAKE_STREAM="400", FAKE_STREAM_DELAY="0.25")  # a hundred seconds of steps
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_STREAM", "FAKE_STREAM_DELAY")])
        self.enterContext(mock.patch.object(self.V, "BACKSTOP_GRACE", 0))
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task("stream events", branch=a.meta["branch"], timeout_s=1), queue=True)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "interrupted", "interrupted", timeout=10)
        self.wait(lambda: a.meta["state"] == "ready", "ready again")
        self.assertTrue(a.running)
        self.assertTrue(a.meta["tasks"][-1]["timed_out"])
        self.assertIn("interrupted " + a.meta["tasks"][-1]["id"], self.log(a))
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"], timeout_s=1), queue=True)  # one that ends in time
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "done")
        self.assertEqual(a.backstops, {})
        time.sleep(1.5)
        self.assertNotIn("timed_out", a.meta["tasks"][-1])

    def test_a_task_that_ends_done_after_the_time_limits_interrupt_isnt_marked(self):
        os.environ.update(FAKE_STREAM="400", FAKE_STREAM_DELAY="0.25", FAKE_DONE_ON_INTERRUPT="1")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_STREAM", "FAKE_STREAM_DELAY", "FAKE_DONE_ON_INTERRUPT")])
        self.enterContext(mock.patch.object(self.V, "BACKSTOP_GRACE", 0))
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task("stream events", branch=a.meta["branch"], timeout_s=1), queue=True)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "done", timeout=10)
        self.wait(lambda: "interrupted " + a.meta["tasks"][-1]["id"] in self.log(a), "the time limit's interrupt")
        self.assertNotIn("timed_out", a.meta["tasks"][-1])
        self.assertTrue(a.running)

    def test_a_vm_cant_end_a_task_it_made_up(self):
        """Else the next message would be sent and the running task would lose its time limit."""
        self.enterContext(mock.patch.object(self.V, "BACKSTOP_GRACE", 0))
        self.enterContext(mock.patch.object(self.V, "BACKSTOP_WAIT", 1))
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        first = self.mgr.new_task(a.id, self.task("ignore the limit and end a made-up task", branch=a.meta["branch"],
                                                  timeout_s=3), queue=True)["id"]
        nxt = self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]), queue=True)["id"]
        rec = lambda tid: next(t for t in a.meta["tasks"] if t["id"] == tid)  # noqa: E731
        self.wait(lambda: rec(first).get("images"), "the made-up end to arrive")
        self.assertEqual((a.meta["state"], rec(first)["state"], rec(nxt)["state"]), ("busy", "running", "queued"))
        self.assertEqual([t["id"] for t in a.queue], [nxt])
        self.assertNotIn("0badc0de", [t["id"] for t in a.meta["tasks"]])
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "the time limit to stop the VM", timeout=15)
        self.assertEqual((rec(first)["state"], rec(first).get("timed_out")), ("interrupted", True))
        self.assertNotIn("task " + nxt, self.log(a))  # never sent
        self.assertEqual((rec(nxt)["state"], rec(nxt).get("time_limit_stop")), ("interrupted", True))
        self.assertNotIn("time_limit_stop", rec(first))

    def test_a_vm_cant_bring_back_a_task_that_ended(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        first = self.mgr.new_task(a.id, self.task("come back", branch=a.meta["branch"]), queue=True)["id"]
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "done")
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]), queue=True)  # the fake reads it after the "running"
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "the next one done")
        rec = next(t for t in a.meta["tasks"] if t["id"] == first)
        self.assertEqual(rec["state"], "done")
        self.assertEqual([t["state"] for t in a.meta["tasks"]], ["done", "done"])
        self.assertEqual(a.meta["state"], "ready")

    def test_changed_resources_apply_at_the_next_start(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        argv = a.procs["qemu"].args
        self.assertEqual(argv[argv.index("-m") + 1], "1024M")
        used = a.public()["mem_used_mb"]  # the fake VM's own process, read from /proc
        self.assertTrue(isinstance(used, int) and used > 0, used)
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stopped")
        self.assertIsNone(a.public()["mem_used_mb"])
        a.set(cpus=2, memory_mb=1536)  # what the Resources control saves
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready again")
        argv = a.procs["qemu"].args
        self.assertEqual((argv[argv.index("-smp") + 1], argv[argv.index("-m") + 1]), ("2", "1536M"))

    def test_stop_after_each_task_can_change_while_running(self):
        a = self.make("ephemeral", auto_stop=True)
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        a.set(auto_stop=False)  # switched off in the ··· menu while it runs
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]), queue=True)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "first task")
        time.sleep(0.5)
        self.assertTrue(a.running, "it keeps running, and so does the conversation")
        a.set(auto_stop=True)  # and back on
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]), queue=True)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "agent to stop")
        self.assertEqual([t["state"] for t in a.meta["tasks"]], ["done", "done"])

    def test_secrets_an_agent_prints_are_hidden(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        tok = "ghp_leaky0123456789"
        self.mgr.new_task(a.id, self.task("print your token", env={"GH_TOKEN": tok, "ANTHROPIC_API_KEY": "sk-ant-secret"}), queue=True)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task")
        log = self.log(a)
        self.assertNotIn(tok, log)
        self.assertIn("the agent printed [hidden] to the log", log)
        chat = [e for e in self.mgr.read_chat(a.id)["events"] if e["k"] == "text"]
        self.assertIn("Your token is [hidden].", [e["text"] for e in chat])
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                p = os.path.join(root, f)
                if os.path.isfile(p) and not os.path.islink(p):
                    with open(p, "rb") as fh:
                        self.assertNotIn(tok.encode(), fh.read(), p)
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running, "stop")

    def test_chat_events_are_validated_and_stored(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIsNone(a.meta["boot_phase"])
        self.assertEqual(a.meta["policy_version"], self.cfg["policy_version"])
        t = self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]))
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task")
        self.wait(lambda: len(self.mgr.read_chat(a.id)["events"]) >= 11, "chat")
        ev = self.mgr.read_chat(a.id)["events"]
        self.assertEqual([e["k"] for e in ev], ["text", "tool", "result", "tool", "result", "diff", "text", "text", "status", "done", "done"])
        self.assertEqual(ev[8]["text"], "Compacting the conversation… <b>")  # one line; shown as text
        sid = f"{t['id']}-0000-4000-8000-000000000000"
        self.assertEqual(ev[9]["sid"], sid)
        self.assertNotIn("sid", ev[10])  # not a session id: dropped
        self.assertEqual(a.meta["tasks"][-1]["session"], sid)  # "continue from here" can find it
        self.assertTrue(all(e["task"] == t["id"] for e in ev))
        self.assertEqual(ev[5]["lines"], [["+", "int x = 1;"]])  # unknown diff sign dropped
        self.assertEqual(len(ev[6]["text"]), 20000)  # truncated
        self.assertEqual([e["seq"] for e in ev], list(range(1, 12)))
        later = self.mgr.read_chat(a.id, after=4)
        self.assertEqual([e["seq"] for e in later["events"]], list(range(5, 12)))
        self.assertEqual(later["last"], 11)
        # survives a panel restart (read back from chat.jsonl)
        mgr2 = self.V.Manager(lambda: self.cfg)
        self.assertEqual([e["seq"] for e in mgr2.read_chat(a.id)["events"]], list(range(1, 12)))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    def fake_bwrap(self, works):
        """A bubblewrap stand-in: its check passes or fails, and a real call runs what follows "--"."""
        d = os.path.join(self.tmp.name, "bwrap-ok" if works else "bwrap-blocked")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "bwrap")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + ("" if works else 'echo "bwrap: setting up uid map: Permission denied" >&2; exit 1\n')
                    + 'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
        os.chmod(path, 0o755)
        return path

    def test_the_agent_log_says_how_qemu_runs(self):
        from cilly import host as H
        H._bwrap_cache.clear()
        self.addCleanup(H._bwrap_cache.clear)
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIn("cilly: QEMU runs without bubblewrap: vm.use_bwrap is off.\n", self.log(a))
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop")
        self.cfg["vm"]["use_bwrap"] = "auto"
        good = self.fake_bwrap(True)
        with mock.patch.object(self.V, "which", side_effect=lambda n: good if n == "bwrap" else shutil.which(n)):
            self.start(a)
            self.wait(lambda: a.meta["state"] == "ready", "ready inside bubblewrap")
        self.assertIn(f"cilly: QEMU runs inside bubblewrap ({good}).\n", self.log(a))
        with open(os.path.join(a.paths.vm_dir, "argv.json")) as f:
            self.assertEqual(json.load(f)[0], good)

    def test_a_blocked_bubblewrap_never_runs_qemu_unwrapped(self):
        """auto fails closed: installed but blocked means no VM, never a QEMU outside the sandbox."""
        from cilly import host as H
        H._bwrap_cache.clear()
        self.addCleanup(H._bwrap_cache.clear)
        self.cfg["vm"]["use_bwrap"] = "auto"
        bad = self.fake_bwrap(False)
        a = self.make("persistent")
        with mock.patch.object(self.V, "which", side_effect=lambda n: bad if n == "bwrap" else shutil.which(n)), \
                mock.patch.object(H, "userns_block", return_value="apparmor"), \
                mock.patch.object(H, "UBUNTU_BWRAP_PROFILE", __file__):  # Ubuntu has its profile, which install.sh turns on
            with self.assertRaises(self.V.VMError) as cm:
                self.start(a)
        msg = str(cm.exception)
        for part in ("can't build its sandbox", "Permission denied", "install.sh", 'set vm.use_bwrap to "off"'):
            self.assertIn(part, msg)
        self.assertFalse(a.running)
        self.assertFalse(os.path.exists(os.path.join(a.paths.vm_dir, "argv.json")))  # QEMU never got a command line
        # fixed (install.sh ran): the next start checks again at once, without waiting for the host status to
        good = self.fake_bwrap(True)
        with open(bad, "w") as f, open(good) as g:
            f.write(g.read())
        with mock.patch.object(self.V, "which", side_effect=lambda n: bad if n == "bwrap" else shutil.which(n)):
            self.start(a)
            self.wait(lambda: a.meta["state"] == "ready", "ready once fixed")
        self.assertIn("cilly: QEMU runs inside bubblewrap", self.log(a))

    # -- moving to a new image -----------------------------------------------------
    def moved_agent(self):
        """A persistent agent that has booted once on base-test, stopped, and a newer image next to it."""
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stopped")
        open(os.path.join(self.V.images_dir(), "base-new.qcow2"), "w").close()
        with open(a.paths.disk, "rb") as f:
            old = f.read()
        self.assertIn(b"boot", old)
        return a, old

    def move(self, a, image="base-new.qcow2"):
        self.mgr.move_image(a.id, image, policy=self.C.effective_policy(self.cfg))
        self.wait(lambda: not a.meta.get("move") and not a.running, "the move", timeout=20)

    def test_a_move_is_done_only_once_its_leftovers_are(self):
        """A slow cleanup must neither show a finished move with its transfer disk still there, nor
        make a start right after boot as a move step."""
        real = self.V._rm

        def slow(path):
            if path.endswith("transfer.img"):
                time.sleep(0.5)
            real(path)
        a, _ = self.moved_agent()
        with mock.patch.object(self.V, "_rm", slow):
            self.move(a)
            self.assertFalse(os.path.exists(a.paths.transfer))
            self.start(a)
            self.wait(lambda: a.meta["state"] == "ready", "ready on the new image")

    def test_moving_to_a_new_image_keeps_the_home_folder(self):
        a, old = self.moved_agent()
        self.move(a)
        self.assertIsNone(a.meta.get("error"))
        self.assertEqual((a.meta["base_image"], a.meta["image"]), ("base-new.qcow2", "base-new.qcow2"))
        self.assertEqual((a.meta["moved"]["from"], a.meta["moved"]["to"]), ("base-test.qcow2", "base-new.qcow2"))
        with open(a.paths.disk, "rb") as f:
            new = f.read()
        self.assertIn(b"home from the old disk:\n" + old, new)  # a fresh disk, with the old home in it
        for gone in (a.paths.disk_old, a.paths.transfer):
            self.assertFalse(os.path.exists(gone), gone)
        with open(os.path.join(a.paths.vm_dir, "argv.json")) as f:
            argv = " ".join(json.load(f))  # the import boot: the transfer disk read-only, and never probed
        self.assertIn("id=transfer,file=" + a.paths.transfer + ",format=raw,readonly=on", argv)
        self.assertEqual(a.meta["state"], "stopped")
        self.start(a)  # and it starts as usual on its new image
        self.wait(lambda: a.meta["state"] == "ready", "ready on the new image")
        with open(a.paths.disk, "rb") as f:
            self.assertIn(b"home from the old disk:\n" + old, f.read())  # the same disk, not made again

    def test_a_disk_from_before_the_rename_moves_to_a_new_image(self):
        """Its own runner only knows vmpanel's names, so the boot that packs its home folder uses them:
        that runner finds the boot disk and hands over to the current one.  Nothing else starts it."""
        a, old = self.moved_agent()
        with open(os.path.join(self.V.images_dir(), "base-test.json"), "w") as f:
            json.dump({"toolchains": [], "preset": "full", "built": "2026-10-17T05:35:52Z"}, f)  # before the rename
        with open(os.path.join(self.V.images_dir(), "base-new.json"), "w") as f:
            json.dump({"toolchains": [], "preset": "full", "runner": "cilly"}, f)
        with self.assertRaises(self.V.VMError):
            self.start(a)  # an ordinary start is still refused
        argvs = []
        real = self.V.qemu_argv
        with mock.patch.object(self.V, "qemu_argv", lambda **kw: argvs.append(real(**kw)) or argvs[-1]):
            self.move(a)
        self.assertIsNone(a.meta.get("error"))
        self.assertEqual(a.meta["base_image"], "base-new.qcow2")
        with open(a.paths.disk, "rb") as f:
            self.assertIn(b"home from the old disk:\n" + old, f.read())
        export, imp = (" ".join(x) for x in argvs)
        for name in ("serial=vmpanel-root", "serial=vmpanel-task", "serial=vmpanel-transfer", "name=org.vmpanel.control", "name=org.vmpanel.log"):
            self.assertIn(name, export)
            self.assertIn(name.replace("vmpanel", "cilly"), imp)  # the new image's own runner: cilly's names
        self.assertNotIn("vmpanel", imp)
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready on the new image")

    def test_a_failed_move_leaves_the_agent_as_it_was(self):
        for step in ("export", "import"):
            with self.subTest(step=step), mock.patch.dict(os.environ, {"FAKE_MOVE": f"fail_{step}"}):
                a, old = self.moved_agent()
                self.move(a)
                self.assertIn(f"Couldn't move to base-new.qcow2: no room left to {step}.", a.meta["error"])
                self.assertIn("still on its old image", a.meta["error"])
                self.assertEqual((a.meta["base_image"], a.meta.get("image")), ("base-test.qcow2", None))
                with open(a.paths.disk, "rb") as f:
                    self.assertTrue(f.read().startswith(old))  # its own disk, back where it was
                for gone in (a.paths.disk_old, a.paths.transfer):
                    self.assertFalse(os.path.exists(gone), gone)
                self.assertNotIn("moved", a.meta)

    def test_a_runner_that_doesnt_know_the_move_changes_nothing(self):
        a, old = self.moved_agent()
        with mock.patch.dict(os.environ, {"FAKE_MOVE": "old"}):
            t0 = time.time()
            self.move(a)
        self.assertLess(time.time() - t0, 15)  # stopped at its "ready", not after an hour
        self.assertIn("started as usual instead of moving", a.meta["error"])
        self.assertEqual(a.meta["base_image"], "base-test.qcow2")

    def test_stop_during_a_move_goes_back(self):
        a, old = self.moved_agent()
        with mock.patch.dict(os.environ, {"FAKE_HELLO_WAIT": "1"}):
            self.mgr.move_image(a.id, "base-new.qcow2", policy=self.C.effective_policy(self.cfg))
            self.wait(lambda: a.running, "the first boot")
            self.mgr.stop(a.id)
            self.wait(lambda: not a.meta.get("move") and not a.running, "the move", timeout=20)
        self.assertIn("Couldn't move to base-new.qcow2: you stopped it.", a.meta["error"])
        self.assertEqual(a.meta["base_image"], "base-test.qcow2")
        with open(a.paths.disk, "rb") as f:
            self.assertTrue(f.read().startswith(old))

    def test_a_move_cut_short_by_a_restart_goes_back(self):
        a, old = self.moved_agent()
        os.replace(a.paths.disk, a.paths.disk_old)  # as if cilly stopped during the second boot
        with open(a.paths.disk, "wb") as f:
            f.write(b"half a new disk")
        with open(a.paths.transfer, "wb") as f:
            f.write(b"packed home")
        a.set(move={"to": "base-new.qcow2", "from": "base-test.qcow2", "image": None, "step": 2},
              base_image="base-new.qcow2", image="base-new.qcow2")
        mgr = self.V.Manager(lambda: self.cfg)
        b = mgr.get(a.id)
        self.assertIsNone(b.meta["move"])
        self.assertEqual((b.meta["base_image"], b.meta["image"]), ("base-test.qcow2", None))
        self.assertIn("cilly stopped while the agent moved", b.meta["error"])
        with open(a.paths.disk, "rb") as f:
            self.assertEqual(f.read(), old)
        self.assertFalse(os.path.exists(a.paths.transfer))
        # a move that had worked, cut short only before it forgot the old disk's name: it's done
        a.set(move={"to": "base-new.qcow2", "from": "base-test.qcow2", "image": None, "step": 2},
              base_image="base-new.qcow2", error=None)
        b = self.V.Manager(lambda: self.cfg).get(a.id)
        self.assertEqual((b.meta["move"], b.meta["base_image"], b.meta.get("error")), (None, "base-new.qcow2", None))
        self.assertEqual(b.meta["moved"]["to"], "base-new.qcow2")

    def test_what_a_move_refuses(self):
        a, _ = self.moved_agent()
        pol = self.C.effective_policy(self.cfg)
        for image, why in (("base-test.qcow2", "already runs on this image"), ("nope.qcow2", "unknown VM image")):
            with self.assertRaisesRegex(self.V.VMError, why):
                self.mgr.move_image(a.id, image, policy=pol)
        e = self.make("ephemeral")
        with self.assertRaisesRegex(self.V.VMError, "Only a persistent agent"):
            self.mgr.move_image(e.id, "base-new.qcow2", policy=pol)
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        with self.assertRaisesRegex(self.V.VMError, "Stop the agent first"):
            self.mgr.move_image(a.id, "base-new.qcow2", policy=pol)
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stopped")
        with mock.patch.object(self.V.shutil, "disk_usage", lambda p: mock.Mock(free=10 << 20)):
            with self.assertRaisesRegex(self.V.VMError, "Not enough free space"):
                self.mgr.move_image(a.id, "base-new.qcow2", policy=pol)
        # while it moves, nothing else may touch the agent
        with mock.patch.dict(os.environ, {"FAKE_HELLO_WAIT": "1"}):
            self.mgr.move_image(a.id, "base-new.qcow2", policy=pol)
            for act in (lambda: self.start(a), lambda: self.mgr.reset_disk(a.id), lambda: self.mgr.delete(a.id),
                        lambda: self.mgr.move_image(a.id, "base-new.qcow2", policy=pol)):
                with self.assertRaises(self.V.VMError):
                    act()
            self.wait(lambda: not a.meta.get("move") and not a.running, "the move", timeout=20)
        self.assertEqual(a.meta["base_image"], "base-new.qcow2")

    def test_persistent_keeps_disk_across_restarts(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]))
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task done")
        self.assertEqual(a.meta["state"], "ready")
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop")
        self.assertTrue(os.path.exists(a.paths.disk))
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready again")
        self.assertIn("boots on this disk: 2", self.log(a))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop 2")
        self.mgr.reset_disk(a.id)
        self.assertFalse(os.path.exists(a.paths.disk))
        self.assert_no_secret_on_disk()

    def test_an_image_from_before_the_rename_asks_for_a_rebuild(self):
        """Its runner listens under the old names, so it would never answer: refused with what to do instead."""
        note = os.path.join(self.V.images_dir(), "base-test.json")

        def write(info):
            with open(note, "w") as f:
                json.dump(info, f)
        write({"toolchains": [], "preset": "full", "built": "2026-10-01T00:00:00Z"})  # an older build's note
        self.assertTrue(self.V.image_info("base-test.qcow2")["outdated"])
        a = self.make("ephemeral")
        with self.assertRaises(self.V.VMError) as cm:
            self.start(a)
        self.assertIn("built before cilly's rename. Rebuild it under Settings", str(cm.exception))
        self.assertFalse(a.running)
        write({"toolchains": [], "preset": "full", "runner": "cilly"})  # rebuilt
        self.assertFalse(self.V.image_info("base-test.qcow2")["outdated"])
        os.unlink(note)
        self.assertFalse(self.V.image_info("base-test.qcow2")["outdated"])  # no note at all: placed by hand, as before
        # a persistent agent whose disk came from an old image is told how to keep its files
        b = self.make("persistent")
        self.start(b)
        self.wait(lambda: b.meta.get("state") == "ready", "ready")
        self.mgr.stop(b.id)
        self.wait(lambda: not b.running, "stopped")
        write({"toolchains": [], "preset": "full"})
        with self.assertRaises(self.V.VMError) as cm:
            self.start(b)
        self.assertIn("Move to new image", str(cm.exception))

    def test_memory_mode_round_trip(self):
        a = self.make("memory")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop")
        self.assertTrue(a.meta["memory"]["imported"], a.meta["memory"])
        mem = os.path.join(a.paths.vm_dir, "memory", "projects", "-home-agent-work-repo", "s1.jsonl")
        with open(mem) as f:
            self.assertEqual(f.read(), "conversation-v1")
        self.assertFalse(os.path.exists(a.paths.disk), "memory mode starts clean every time")
        # second run gets the memory back
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready 2")
        log = self.log(a)
        self.assertIn("restored memory/projects/-home-agent-work-repo/s1.jsonl: conversation-v1", log)
        second_run = log.rsplit("===== cilly: start", 1)[1]
        self.assertIn("boots on this disk: 1", second_run)
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop 2")

    def test_long_or_deep_memory_paths(self):
        import io
        import tarfile
        from cilly import memory as M
        a = self.make("memory")
        # refused on import: the previous memory stays
        for name in ("projects/" + "/".join(["d"] * 40) + "/f.jsonl", "projects/" + "/".join(["x" * 200] * 6) + "/f.jsonl"):
            img = os.path.join(self.tmp.name, "memout.img")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
                ti = tarfile.TarInfo(name)
                tar.addfile(ti, io.BytesIO(b""))
            with open(img, "wb") as f:
                f.write(buf.getvalue())
            with self.assertRaisesRegex(M.MemoryError_, "longer than|deeper than"):
                M.import_memout(img, a.paths.vm_dir)
        # memory stored before the limits doesn't stop a start: those files stay out, the rest goes in
        root = os.path.join(a.paths.vm_dir, "memory", "projects")
        os.makedirs(os.path.join(root, *["d"] * 40))
        with open(os.path.join(root, *["d"] * 40, "deep.jsonl"), "w") as f:
            f.write("deep")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(20):  # a path longer than the host can even open by name
            os.mkdir("y" * 250, dir_fd=fd)
            nfd = os.open("y" * 250, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nfd
        os.close(os.open("long.jsonl", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
        os.close(fd)
        with open(os.path.join(root, "ok.jsonl"), "w") as f:
            f.write("kept")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIn("restored memory/projects/ok.jsonl: kept", self.log(a))
        self.assertNotIn("deep.jsonl", self.log(a))
        self.assertGreaterEqual(a.meta["memory"]["skipped"], 2)
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop")

    def test_stop_during_a_task_keeps_the_memory(self):
        """The panel's side of Stop during a task: it waits while the VM finishes the task, then imports
        the memory, and the next start restores it.  The fake QEMU plays the runner and always finishes a
        task before exporting, so this doesn't test the runner's own order, and it passed before that was
        fixed: test_runner's test_stop_lets_the_task_finish_before_the_memory_export is the real check."""
        os.environ["FAKE_SUB_DELAY"] = "1.5"  # a task that takes a while
        a = self.make("memory")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        t = self.mgr.new_task(a.id, self.task("split this up with subagents"), queue=True)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "running", "task running")
        self.mgr.stop(a.id)  # in the middle of it
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop")
        self.assertEqual(next(x for x in a.meta["tasks"] if x["id"] == t["id"])["state"], "done")  # it finished up first
        self.assertTrue(a.meta["memory"]["imported"], a.meta["memory"])
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready 2")
        self.assertIn("restored memory/cilly-state.json", self.log(a).rsplit("===== cilly: start", 1)[1])
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop 2")

    def test_quitting_the_panel_shuts_agents_down_properly(self):
        a, p = self.make("memory"), self.make("persistent")
        self.start(a)
        self.start(p)
        self.wait(lambda: a.meta["state"] == "ready" and p.meta["state"] == "ready", "ready")
        self.mgr.shutdown_all()  # Ctrl+C, systemctl stop, logging out
        self.assertFalse(a.running or p.running)
        self.assertEqual((a.meta["state"], p.meta["state"]), ("stopped", "stopped"))
        self.assertTrue(a.meta["memory"]["imported"], "the memory agent saved its conversation first")
        self.assertIn("powering off", self.log(p))

    def test_memory_export_with_symlink_rejected_previous_kept(self):
        a = self.make("memory")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop")
        os.environ["FAKE_MEMOUT"] = "evil"
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready 2")
        self.mgr.stop(a.id)
        self.wait(lambda: a.meta["state"] == "stopped" and not a.running, "stop 2")
        self.assertIn("rejected", a.meta["memory"]["error"])
        mem = os.path.join(a.paths.vm_dir, "memory", "projects", "-home-agent-work-repo", "s1.jsonl")
        with open(mem) as f:
            self.assertEqual(f.read(), "conversation-v1")  # previous memory kept
        self.assertFalse(os.path.lexists(os.path.join(a.paths.vm_dir, "memory", "projects", "escape")))

    def test_live_policy(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        pol = dict(self.cfg["claude_policy"], claude_md="NEW STYLE RULES")
        self.cfg["claude_policy"] = pol
        self.assertEqual(self.mgr.apply_policy(lambda a: self.C.effective_policy(self.cfg)), 1)
        self.wait(lambda: a.meta.get("policy_applied_at"), "policy ack")
        self.assertIn("policy updated: NEW STYLE RULES", self.log(a))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    def busy(self, a):
        """A task that keeps working (the fake's slow subagent run) until it's interrupted."""
        os.environ["FAKE_SUB_DELAY"] = "30"
        self.addCleanup(os.environ.pop, "FAKE_SUB_DELAY", None)
        t = self.mgr.new_task(a.id, self.task("use subagents", branch=a.meta["branch"]))
        self.wait(lambda: "Delegated" in str(self.mgr.read_chat(a.id)["events"]), "working")
        return t

    def test_interrupt_keeps_the_vm_on(self):
        a = self.make("persistent", auto_stop=True)  # even so: you stopped it to say something else
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        with self.assertRaisesRegex(self.V.VMError, "Nothing is running"):
            self.mgr.interrupt(a.id)
        t = self.busy(a)
        self.assertEqual(self.mgr.interrupt(a.id), "stopping")
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "interrupted", "interrupted")
        rec = a.meta["tasks"][-1]
        self.assertEqual((rec["id"], rec["pr"]), (t["id"], f"https://github.com/{REPO}/pull/7"))  # it finished up
        self.assertTrue(rec["by_you"])
        time.sleep(0.5)
        self.assertTrue(a.running)
        self.assertEqual(a.meta["state"], "ready")
        self.mgr.new_task(a.id, self.task("go on", branch=a.meta["branch"]))
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "the next message")
        self.assertIn("continue=True", next(ln for ln in self.log(a).splitlines() if "head='go on'" in ln))

    def test_a_task_the_vm_cut_off_wasnt_stopped_by_you(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.busy(a)
        queued = self.mgr.new_task(a.id, self.task("never ran", branch=a.meta["branch"]), queue=True)
        a.procs["qemu"].kill()  # a crash or an out-of-memory kill
        self.wait(lambda: a.meta["state"] == "stopped", "stopped")
        ended = [t for t in a.meta["tasks"] if t["state"] == "interrupted"]
        self.assertEqual(len(ended), 2)
        self.assertIn(queued["id"], [t["id"] for t in ended])
        self.assertFalse(any(t.get("by_you") for t in ended))

    def test_an_older_runner_says_to_restart(self):
        os.environ["FAKE_OLD_RUNNER"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_OLD_RUNNER", None)
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.busy(a)
        with mock.patch.object(self.V, "INTERRUPT_WAIT", 0.5), \
                self.assertRaisesRegex(self.V.VMError, "The VM didn't answer, so the task wasn't stopped"):
            self.mgr.interrupt(a.id)
        self.assertEqual(a.meta["tasks"][-1]["state"], "running")  # nothing changed
        self.assertNotIn("by_you", a.meta["tasks"][-1])
        self.assertFalse(a.can_deliver)  # and Send now never asks it to take a message into the run
        now = self.mgr.new_task(a.id, self.task("send me now", branch=a.meta["branch"]), queue=True)
        self.assertEqual(a.public()["deliver"], [])
        with mock.patch.object(self.V, "INTERRUPT_WAIT", 0.5):
            self.assertEqual(self.mgr.send_now(a.id, now["id"]), "moved")
        self.assertNotIn("delivered", self.log(a))

    def test_send_now_goes_into_the_running_task(self):
        os.environ["FAKE_SUB_DELAY"] = "4"
        self.addCleanup(os.environ.pop, "FAKE_SUB_DELAY", None)
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertTrue(a.can_deliver)
        run = self.mgr.new_task(a.id, self.task("use subagents", branch=a.meta["branch"]))
        self.wait(lambda: a.meta["tasks"][-1].get("live"), "the run to take messages")
        first = self.mgr.new_task(a.id, self.task("queued first", branch=a.meta["branch"]), queue=True)
        now = self.mgr.new_task(a.id, self.task("send me now", branch=a.meta["branch"]), queue=True)
        self.assertEqual(a.public()["deliver"], [first["id"], now["id"]])
        self.assertEqual(self.mgr.send_now(a.id, now["id"]), "delivered")
        rec = next(t for t in a.meta["tasks"] if t["id"] == now["id"])
        self.assertEqual((rec["state"], rec["into"]), ("delivered", run["id"]))
        self.assertEqual([t["id"] for t in a.queue], [first["id"]])
        self.assertEqual(next(t for t in a.meta["tasks"] if t["id"] == run["id"])["state"], "running")  # nothing stopped
        evs = self.mgr.read_chat(a.id)["events"]
        self.assertEqual([(e["task"], e["msg"], e["text"]) for e in evs if e["k"] == "you"], [(run["id"], now["id"], "send me now")])
        self.wait(lambda: "Got it: send me now" in str(self.mgr.read_chat(a.id)["events"]), "the answer")
        with self.assertRaisesRegex(self.V.VMError, "It already started"):
            self.mgr.cancel_task(a.id, now["id"])
        self.wait(lambda: next(t for t in a.meta["tasks"] if t["id"] == first["id"])["state"] == "done", "the queued one", timeout=20)
        self.assertEqual(next(t for t in a.meta["tasks"] if t["id"] == run["id"])["state"], "done")  # it ran to the end
        self.assertNotIn("head='send me now'", self.log(a))  # never a task of its own: it ran once
        self.assertIn(f"delivered {now['id']} into {run['id']}", self.log(a))
        self.assertFalse(any(e["k"] == "sub" and e["state"] == "stopped" and e["id"] == "ag1"
                             for e in self.mgr.read_chat(a.id)["events"]))  # the subagent wasn't stopped
        self.assertNotIn("not-a-task", str(a.meta["tasks"]))

    def test_a_refused_message_shows_in_the_chat(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        run = self.busy(a)
        self.wait(lambda: a.meta["tasks"][-1].get("live"), "the run to take messages")
        now = self.mgr.new_task(a.id, self.task("please refuse this", branch=a.meta["branch"]), queue=True)
        self.assertEqual(self.mgr.send_now(a.id, now["id"], deliver_only=True), "delivered")
        said = lambda: [e for e in self.mgr.read_chat(a.id)["events"] if e["k"] == "status" and "refused" in e["text"]]
        self.wait(said, "the report")
        self.assertEqual(said()[0]["task"], run["id"])
        self.assertTrue(said()[0]["text"].startswith('Claude Code refused your message "please refuse this"'))
        self.assertEqual(next(t for t in a.meta["tasks"] if t["id"] == now["id"])["lost"], "refused")

    def test_send_now_with_files_stops_the_task_instead(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.busy(a)
        self.wait(lambda: a.meta["tasks"][-1].get("live"), "the run to take messages")
        up = os.path.join(a.paths.vm_dir, "upload-1")
        with open(up, "w") as f:
            f.write("x")
        now = self.mgr.new_task(a.id, self.task("with a file", branch=a.meta["branch"], attachment_set="s1",
                                                attachments=[{"name": "a.txt", "path": up}]), queue=True)
        self.assertEqual(a.public()["deliver"], [])
        self.assertEqual(self.mgr.send_now(a.id, now["id"]), "stopping")
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "it ran as its own task")
        self.assertEqual(a.meta["tasks"][-2]["state"], "interrupted")

    def test_send_now_jumps_the_queue(self):
        os.environ["FAKE_NO_LIVE"] = "1"  # a Claude Code that doesn't take messages while it works
        self.addCleanup(os.environ.pop, "FAKE_NO_LIVE", None)
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.busy(a)
        first = self.mgr.new_task(a.id, self.task("queued first", branch=a.meta["branch"]), queue=True)
        now = self.mgr.new_task(a.id, self.task("send me now", branch=a.meta["branch"]), queue=True)
        with self.assertRaises(self.V.VMError):
            self.mgr.send_now(a.id, "0badc0de")
        self.assertEqual(self.mgr.send_now(a.id, now["id"]), "stopping")
        self.wait(lambda: all(t["state"] == "done" for t in a.meta["tasks"] if t["id"] in (first["id"], now["id"])), "both ran")
        order = [ln for ln in self.log(a).splitlines() if "head='send me now'" in ln or "head='queued first'" in ln]
        self.assertIn("send me now", order[0])
        self.assertIn("continue=True", order[0])  # the same conversation
        self.assertEqual(a.meta["tasks"][-3]["state"], "interrupted")
        with self.assertRaisesRegex(self.V.VMError, "It already started"):
            self.mgr.send_now(a.id, now["id"])

    def test_the_hello_waits_for_the_boot(self):
        sent, real = [], self.V.Agent.send

        def send(agent, msg, timeout=None):
            sent.append((msg.get("type"), timeout))
            return real(agent, msg, timeout)
        a = self.make("persistent")
        with mock.patch.object(self.V.Agent, "send", send):
            self.start(a)
            self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.assertIn(("hello", self.V.BOOT_WAIT), sent)  # a big policy can't sit in the socket until the guest reads

    def test_stop_while_the_hello_waits_for_the_boot(self):
        os.environ["FAKE_HELLO_WAIT"] = "60"
        self.addCleanup(os.environ.pop, "FAKE_HELLO_WAIT", None)
        a = self.make("persistent")
        policy = dict(self.C.effective_policy(self.cfg), claude_md="x" * (4 << 20))  # more than the socket buffer holds
        with mock.patch.object(self.V, "SEND_TIMEOUT", 0.5):
            self.mgr.start(a.id, allow=["github.com"], policy=policy)
            self.wait(lambda: a.ctl is not None, "the hello is on its way")
            time.sleep(0.3)
            self.mgr.stop(a.id)
            self.wait(lambda: not a.running and a.meta["state"] not in ("booting", "stopping"), "stopped", timeout=30)
        self.assertEqual((a.meta["state"], a.meta.get("error")), ("stopped", None))  # not "failed: Broken pipe"

    def test_message_rejected_when_not_ready(self):
        a = self.make("persistent")
        with self.assertRaises(self.V.VMError):
            self.mgr.new_task(a.id, self.task())

    def test_delete_keeps_logs(self):
        a = self.make("ephemeral")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.delete(a.id)
        self.assertFalse(os.path.exists(a.paths.vm_dir))
        kept = [d for d in os.listdir(self.V.logs_dir()) if d.startswith(a.id)]
        self.assertEqual(len(kept), 1)

    def test_attachments_arrive_before_task_and_leave_the_host(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready", timeout=60)
        up = os.path.join(a.paths.vm_dir, "uploads")
        os.makedirs(up)
        paths = []
        for i, size in enumerate((10, 1_300_000)):  # the second needs several chunks
            p = os.path.join(up, f"u{i}")
            with open(p, "wb") as f:
                f.write(os.urandom(size))
            paths.append(p)
        task = self.task(branch=a.meta["branch"], attachment_set="s1",
                         attachments=[{"name": "a.png", "path": paths[0]}, {"name": "b.bin", "path": paths[1]}])
        self.mgr.new_task(a.id, task)
        # 1.3 MB in chunks through the fake VM: a few seconds here, much longer on a slow CI runner
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task", timeout=120)
        log = self.log(a)
        self.assertIn("attached /attachments/s1/a.png size=10 sha_ok=True", log)
        self.assertIn("attached /attachments/s1/b.bin size=1300000 sha_ok=True", log)
        self.assertLess(log.index("b.bin size="), log.index(f"task {a.meta['tasks'][-1]['id']}"))
        self.assertNotIn("LEAK", log)
        self.assertEqual(os.listdir(up), [], "uploads are deleted once sent")
        # the chat can show both: your image from /attachments, and images the agent left in ~/outbox
        self.assertEqual(a.meta["tasks"][-1]["images"], ["screenshot.png"])  # unsafe names were dropped
        got = self.mgr.read_file(a.id, "attachments", "s1/a.png", 1 << 20)
        self.assertEqual(len(got["data"]), 10)
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop", timeout=60)

    def test_queued_attachments_dropped_when_agent_stops(self):
        a = self.make("persistent")
        p = os.path.join(a.paths.vm_dir, "upload")
        with open(p, "wb") as f:
            f.write(b"x")
        self.mgr.new_task(a.id, self.task(attachment_set="s1", attachments=[{"name": "a", "path": p}]), queue=True)
        self.start(a)
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "task")
        self.assertFalse(os.path.exists(p))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    # -- export / attach --------------------------------------------------------
    def make_scratch(self):
        return self.mgr.create(name="my scratch", mode="persistent", repo=None, agent="claude", cpus=1,
                               memory_mb=1024, auto_stop=False, branch_prefix="agent/")

    def test_export_lands_in_new_subfolder_without_exec_bits(self):
        dest = os.path.join(self.tmp.name, "out")
        os.mkdir(dest)
        a = self.make_scratch()
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        rec = self.mgr.start_export(a.id, "scratch", dest)
        self.wait(lambda: a.meta["exports"][-1]["state"] != "running", "export")
        done = a.meta["exports"][-1]
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(done["id"], rec["id"])
        self.assertEqual(done["files"], 2)
        sub = done["path"]
        self.assertEqual(os.path.dirname(sub), dest)
        self.assertTrue(os.path.basename(sub).startswith("my_scratch-scratch-"))
        png = os.path.join(sub, "art", "sprite.png")
        self.assertEqual(os.path.getsize(png), 4 + 256 * 3000)  # several chunks, reassembled
        self.assertEqual(os.stat(png).st_mode & 0o111, 0)
        self.assertEqual(a.meta["export_dest"], dest)
        self.assertEqual([f for f in os.listdir(a.paths.vm_dir) if f.startswith("export-")], [])
        # a second export goes to another new folder, the first one is untouched
        self.mgr.start_export(a.id, "scratch", dest)
        self.wait(lambda: a.meta["exports"][-1]["state"] != "running", "export 2")
        self.assertEqual(len(os.listdir(dest)), 2)
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    def test_an_ephemeral_agent_keeps_its_outbox_when_it_stops(self):
        """Stop after each task: ~/outbox is copied into a new subfolder before the disk goes, then it stops."""
        dest = os.path.join(self.tmp.name, "kept")
        os.mkdir(dest)
        a = self.make("ephemeral", auto_stop=True)
        a.set(keep_outbox=dest)
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.new_task(a.id, self.task(branch=a.meta["branch"]))
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stopped after its task", timeout=30)
        done = a.meta["exports"][-1]
        self.assertEqual((done["what"], done["state"]), ("outbox", "done"), done)
        self.assertTrue(os.path.basename(done["path"]).startswith("t-outbox-"))
        self.assertTrue(os.path.isfile(os.path.join(done["path"], "README.md")))
        # the folder is gone meanwhile: the copy fails, says why, and the agent stops anyway
        shutil.rmtree(dest)
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready again")
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stopped")
        self.assertEqual(a.meta["exports"][-1]["state"], "failed")
        self.assertIn("destination", a.meta["exports"][-1]["error"])

    def test_the_outbox_isnt_copied_when_it_stays_or_cilly_quits(self):
        dest = os.path.join(self.tmp.name, "kept")
        os.mkdir(dest)
        for mode, closing in (("persistent", False), ("ephemeral", True)):
            a = self.make(mode)
            a.set(keep_outbox=dest)  # set when it was ephemeral, say: only an agent that loses its disk copies
            self.start(a)
            self.wait(lambda: a.meta["state"] == "ready", "ready")
            self.mgr.closing = closing  # cilly quitting doesn't wait for a copy
            try:
                self.mgr.stop(a.id)
                self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stopped")
            finally:
                self.mgr.closing = False
            self.assertEqual(a.meta.get("exports", []), [], mode)
        self.assertEqual(os.listdir(dest), [])

    def test_viewing_files_from_a_running_agent(self):
        from cilly import filecheck
        a = self.make_scratch()
        with self.assertRaisesRegex(self.V.VMError, "(?i)start the agent"):
            self.mgr.list_files(a.id, "outbox", "")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        r = self.mgr.list_files(a.id, "outbox", "")
        self.assertEqual(sorted(e["name"] for e in r["entries"]), ["deploy.sh", "notes", "photo.png", "report.md", "screenshot.png"])
        self.assertEqual([e["name"] for e in self.mgr.list_files(a.id, "outbox", "notes")["entries"]], ["todo.txt"])
        f = self.mgr.read_file(a.id, "outbox", "screenshot.png", 1 << 20)
        info = filecheck.check(f["path"], f["data"])
        self.assertEqual((info["preview"], info["width"], info["height"]), ("image", 320, 200))
        self.assertEqual(filecheck.check("photo.png", self.mgr.read_file(a.id, "outbox", "photo.png", 1 << 20)["data"])["kind"], "zip")
        with self.assertRaisesRegex(self.V.VMError, "not a regular file"):
            self.mgr.read_file(a.id, "outbox", "notes", 1 << 20)
        with self.assertRaisesRegex(self.V.VMError, "larger than"):
            self.mgr.read_file(a.id, "outbox", "screenshot.png", 100)
        for what, path in (("outbox", "../x"), ("outbox", ".git/config"), ("repo", ""), ("home", "")):
            with self.assertRaises(self.V.VMError, msg=(what, path)):
                self.mgr.list_files(a.id, what, path)
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    def test_a_file_the_agent_rewrites_is_listed_anew(self):
        a = self.make("persistent")
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        names = lambda: {e["name"]: e for e in self.mgr.list_files(a.id, "outbox", "")["entries"]}  # noqa: E731
        self.assertNotIn("config.yml", names())
        for content in ("first\n", "second, longer\n"):
            self.mgr.new_task(a.id, self.task("write outbox config.yml\n" + content, branch=a.meta["branch"]))
            self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "written")
            e = names()["config.yml"]
            self.assertEqual(e["size"], len(content))  # what the Files tab compares to fetch it again
            self.assertGreater(e["mtime"], 0)
            self.assertEqual(self.mgr.read_file(a.id, "outbox", "config.yml", 1 << 20)["data"], content.encode())

    def test_export_leaves_out_git(self):
        dest = os.path.join(self.tmp.name, "out")
        os.mkdir(dest)
        os.environ["FAKE_EXPORT"] = "git"
        a = self.make_scratch()
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        self.mgr.start_export(a.id, "scratch", dest)
        self.wait(lambda: a.meta["exports"][-1]["state"] != "running", "export")
        done = a.meta["exports"][-1]
        self.assertEqual((done["state"], done["files"], done["left_out"]), ("done", 2, 1), done)
        self.assertFalse(os.path.exists(os.path.join(done["path"], ".git")))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    def test_evil_or_broken_export_leaves_nothing(self):
        dest = os.path.join(self.tmp.name, "out")
        os.mkdir(dest)
        a = self.make_scratch()
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        for kind, err in (("evil", "unsafe path"), ("badseq", "out of order")):
            os.environ["FAKE_EXPORT"] = kind
            self.mgr.stop(a.id)
            self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")
            self.start(a)
            self.wait(lambda: a.meta["state"] == "ready", "ready")
            self.mgr.start_export(a.id, "scratch", dest)
            self.wait(lambda: a.meta["exports"][-1]["state"] != "running", "export")
            self.assertEqual(a.meta["exports"][-1]["state"], "failed")
            self.assertIn(err, a.meta["exports"][-1]["error"])
            self.assertEqual(os.listdir(dest), [])
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "escape.txt")))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")

    def test_export_refuses_bad_destinations_and_stopped_agents(self):
        a = self.make_scratch()
        dest = os.path.join(self.tmp.name, "out")
        os.mkdir(dest)
        with self.assertRaisesRegex(self.V.VMError, "(?i)start the agent"):
            self.mgr.start_export(a.id, "scratch", dest)
        os.symlink(dest, os.path.join(self.tmp.name, "link"))
        for bad in (os.path.expanduser("~"), os.path.join(self.tmp.name, "link"), "relative/path",
                    os.path.join(self.tmp.name, "missing"), "/etc"):
            with self.assertRaises(self.V.VMError, msg=bad):
                self.mgr.start_export(a.id, "scratch", bad)
        with self.assertRaises(self.V.VMError):
            self.mgr.start_export(a.id, "repo", dest)  # no repo yet

    def test_attach_repository(self):
        a = self.make_scratch()
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        a.set(repo=REPO, repo_in_scratch=True, branch="agent/x-1")
        self.mgr.new_task(a.id, {"attach": True, "repo": REPO, "branch": "agent/x-1", "instructions": "Attach",
                                 "env": {"GH_TOKEN": "tok-123"}}, queue=True, record={"attach": REPO})
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "done", "attach")
        self.assertEqual(a.meta["pr"], f"https://github.com/{REPO}/pull/1")
        self.assertEqual(a.meta["repo"], REPO)
        self.assertIn("attach me/repo branch=agent/x-1 env=GH_TOKEN", self.log(a))
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")
        self.assert_no_secret_on_disk()

    def test_failed_attach_goes_back_to_scratch(self):
        os.environ["FAKE_ATTACH"] = "fail"
        a = self.make_scratch()
        self.start(a)
        self.wait(lambda: a.meta["state"] == "ready", "ready")
        a.set(repo=REPO, repo_in_scratch=True, branch="agent/x-1")
        self.mgr.new_task(a.id, {"attach": True, "repo": REPO, "branch": "agent/x-1", "instructions": "Attach",
                                 "env": {"GH_TOKEN": "tok-123"}}, queue=True, record={"attach": REPO})
        self.wait(lambda: a.meta["tasks"][-1]["state"] == "failed", "attach failure")
        self.assertIsNone(a.meta["repo"])
        self.assertFalse(a.meta["repo_in_scratch"])
        self.mgr.stop(a.id)
        self.wait(lambda: not a.running and a.meta["state"] == "stopped", "stop")


if __name__ == "__main__":
    unittest.main()
