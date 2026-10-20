import copy
import json
import os
import re
import subprocess
import tempfile
import unittest

from cilly import config as C


class MigrationTests(unittest.TestCase):
    def test_old_claude_argv_gets_streaming(self):
        import json
        import tempfile
        from unittest import mock
        from cilly import config as C
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            os.makedirs(os.path.join(d, "cilly"))
            old = {"agents": {"claude": dict(C.DEFAULT_CONFIG["agents"]["claude"],
                                             argv=["claude", "-p", "{instructions}", "--permission-mode",
                                                   "{permission_mode}"])}}
            with open(os.path.join(d, "cilly", "config.json"), "w") as f:
                json.dump(old, f)
            self.assertEqual(C.load()["agents"]["claude"]["argv"], C.ARGV_CLAUDE)
            custom = ["claude", "-p", "{instructions}", "--model", "opus"]
            old["agents"]["claude"]["argv"] = custom
            with open(os.path.join(d, "cilly", "config.json"), "w") as f:
                json.dump(old, f)
            self.assertEqual(C.load()["agents"]["claude"]["argv"], custom)  # your own command is left alone


class SavedChangesTests(unittest.TestCase):
    """The file keeps only what you changed, so a later version's new defaults reach your panel."""

    def setUp(self):
        from unittest import mock
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir.name})
        env.start()
        self.addCleanup(env.stop)

    def file(self):
        import json
        with open(C.config_path()) as f:
            return json.load(f)

    def test_only_changes_are_written(self):
        C.save(C.load())
        self.assertEqual(self.file(), {})  # a fresh setup: nothing changed yet
        cfg = C.load()
        cfg["vm"]["cpus"] = 6
        cfg["egress"]["allow"] = cfg["egress"]["allow"] + ["crates.io"]
        cfg["tokens"]["bot"]["repos"] = ["me/app"]
        cfg["appearance"]["theme"] = "classic"
        C.save(cfg)
        bot = dict(C.DEFAULT_CONFIG["tokens"]["bot"], repos=["me/app"])
        self.assertEqual(self.file(), {"vm": {"cpus": 6}, "egress": {"allow": cfg["egress"]["allow"]},
                                       "tokens": {"bot": bot}, "appearance": {"theme": "classic"}})  # tokens replace as a whole
        self.assertEqual(C.load(), cfg)

    def test_reading_back_gives_the_same_config(self):
        cfg = copy.deepcopy(C.DEFAULT_CONFIG)
        cfg["vm"]["memory_mb"] = 8192
        cfg["claude_policy"]["managed_settings"]["permissions"]["allow"] = ["Bash(npm test)"]
        cfg["mcp_servers"] = {"docs": {"type": "http", "url": "https://docs.example.com/mcp"}}
        cfg["setup"]["done"] = True
        cfg["something_new"] = {"kept": 1}
        changes = C._changes(C.DEFAULT_CONFIG, cfg)
        self.assertEqual(C._merge(C.DEFAULT_CONFIG, changes), cfg)
        self.assertEqual(changes["claude_policy"], {"managed_settings": cfg["claude_policy"]["managed_settings"]})
        self.assertEqual(C._changes(C.DEFAULT_CONFIG, C.DEFAULT_CONFIG), {})

    def test_a_new_default_reaches_a_saved_config(self):
        from unittest import mock
        cfg = C.load()
        cfg["vm"]["cpus"] = 6
        C.save(cfg)
        newer = copy.deepcopy(C.DEFAULT_CONFIG)
        newer["vm"]["memory_mb"] = 6144
        newer["appearance"]["theme"] = "classic"
        with mock.patch.object(C, "DEFAULT_CONFIG", newer):
            got = C.load()
        self.assertEqual((got["vm"]["cpus"], got["vm"]["memory_mb"], got["appearance"]["theme"]), (6, 6144, "classic"))

    def test_an_old_full_file_shrinks_to_its_changes_on_the_next_save(self):
        import json
        old = copy.deepcopy(C.DEFAULT_CONFIG)  # how the panel used to write it: everything
        old["vm"]["cpus"] = 2
        os.makedirs(C.config_dir(), exist_ok=True)
        with open(C.config_path(), "w") as f:
            json.dump(old, f)
        C.save(C.load())
        self.assertEqual(self.file(), {"vm": {"cpus": 2}})


class McpLoginVarsTests(unittest.TestCase):
    def test_only_keys_in_a_remote_login_header_qualify(self):
        servers = {
            "a": {"type": "http", "url": "https://a.example/mcp", "headers": {"Authorization": "Bearer ${A_KEY}"}},
            "b": {"type": "sse", "url": "https://b.example:443/sse", "headers": {"X-API-Key": "${B_KEY}"}},
            "c": {"type": "http", "url": "https://c.example:8443/mcp", "headers": {"Authorization": "Bearer ${C_KEY}"}},
            "d": {"type": "http", "url": "https://d.example/mcp", "headers": {"X-Custom-Token": "${D_KEY}"}},
            "e": {"type": "http", "url": "https://e.example/mcp?key=${E_KEY}", "headers": {"Authorization": "${E_KEY}"}},
            "f": {"type": "stdio", "command": "npx", "env": {"SHARED": "${SHARED_KEY}"}},
            "g": {"type": "http", "url": "https://g.example/mcp", "headers": {"Authorization": "Bearer ${SHARED_KEY}"}},
            "h": {"type": "http", "url": "https://203.0.113.5/mcp", "headers": {"Authorization": "Bearer ${H_KEY}"}},
            "i": {"type": "http", "url": "https://i.example/mcp", "headers": {"Authorization": "Bearer ${A_KEY}"}},
            "j": {"type": "http", "url": "https://my_box.lan/mcp", "headers": {"Authorization": "Bearer ${J_KEY}"}},
        }
        self.assertEqual(C.mcp_login_vars(servers), {"A_KEY": ["a.example", "i.example"], "B_KEY": ["b.example"]})
        # c: another port.  d: another header, which the proxy doesn't swap in.  e: also in the URL.
        # SHARED_KEY: a stdio server in the VM needs it too.  h: an IP address, and j: a name no certificate can have.


class ConfigTests(unittest.TestCase):
    def cfg(self):
        return copy.deepcopy(C.DEFAULT_CONFIG)

    def test_default_valid(self):
        C.validate(self.cfg())

    def test_only_port_443_by_default(self):
        self.assertEqual(C.DEFAULT_CONFIG["egress"]["ports"], [443])

    def test_an_mcp_servers_port_comes_along(self):
        self.assertEqual(C.mcp_hosts({"type": "http", "url": "https://MCP.example.com:8443/mcp"}), ["mcp.example.com:8443"])
        self.assertEqual(C.mcp_hosts({"type": "sse", "url": "https://api.pixellab.ai/mcp"}), ["api.pixellab.ai"])
        self.assertEqual(C.mcp_hosts({"type": "http", "url": "https://x.example:99999/"}), [])  # refused as a server

    def test_never_reach(self):
        c = self.cfg()
        c["egress"]["never_reach"] = ["203.0.113.7", "2001:db8:1234::/48", "198.51.100.0/24", "81.2.69.160/27"]
        C.validate(c)
        for bad in (["example.com"], ["203.0.113.300"], ["1.2.3.4/33"], [""], [7], ["fe80::1%eth0"], "203.0.113.7",
                    ["10.0.0.0/8"] * 201):
            c["egress"]["never_reach"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                C.validate(c)

    def test_wireguard_routes(self):
        import base64
        pub = base64.b64encode(b"k" * 32).decode()
        vpn = {"kind": "wireguard", "address": ["10.8.0.2/24"], "dns": ["10.8.0.1"], "public_key": pub,
               "endpoint": "vpn.example.org:51820", "allowed_ips": ["0.0.0.0/0"], "private": ["git.office.lan"]}
        c = self.cfg()
        c["egress"]["routes"] = {"office": vpn}
        c["egress"]["route"] = "office"
        C.validate(c)
        for bad in ({"user": "me"}, {"private_key": pub}, {"endpoint": "vpn.example.org"}, {"public_key": "short"},
                    {"address": []}, {"dns": ["vpn.example.org"]}, {"private": ["*.office.lan"]}, {"private": ["1.1.1.1"]},
                    {"mtu": 100}):
            c["egress"]["routes"] = {"office": dict(vpn, **bad)}
            with self.assertRaises(ValueError, msg=repr(bad)):
                C.validate(c)
        c["egress"]["routes"] = {"office": {"kind": "socks5", "host": "10.64.0.1", "port": 1080, "private": ["10.8.0.20"]}}
        C.validate(c)  # a proxy into a private network can list hosts too

    def test_the_folders_are_cillys(self):
        from unittest import mock
        from cilly import config as C
        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_CONFIG_HOME": os.path.join(d, "cfg"), "XDG_DATA_HOME": os.path.join(d, "data")}
            with mock.patch.dict(os.environ, env):
                os.makedirs(os.path.join(d, "cfg", "vmpanel"))  # a folder from before the rename is never picked up
                self.assertEqual((C.config_dir(), C.data_dir()), (os.path.join(d, "cfg", "cilly"), os.path.join(d, "data", "cilly")))

    def test_yolo_rounds(self):
        import copy
        from cilly import config as C
        self.assertEqual(C.DEFAULT_CONFIG["research"]["yolo_rounds"], 3)
        for ok in (1, 100, None):
            cfg = copy.deepcopy(C.DEFAULT_CONFIG)
            cfg["research"]["yolo_rounds"] = ok
            C.validate(cfg)
        for bad in (0, 101, "3", 2.5, True):
            cfg = copy.deepcopy(C.DEFAULT_CONFIG)
            cfg["research"]["yolo_rounds"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                C.validate(cfg)

    def test_agent_sudo_is_gone(self):
        cfg = self.cfg()
        cfg["vm"]["agent_sudo"] = False  # left over in saved configs: fine
        C.validate(cfg)
        cfg["vm"]["agent_sudo"] = True
        with self.assertRaisesRegex(ValueError, "agent_sudo was removed"):
            C.validate(cfg)
        self.assertNotIn("agent_sudo", C.DEFAULT_CONFIG["vm"])

    def test_rejects_secret_values(self):
        c = self.cfg()
        c["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"] = {"value": "sk-ant-oat-x"}
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_the_github_token_is_never_an_agent_types_own_secret(self):
        """Every agent of that kind would get it, those without a repository too, and the proxy
        would let it go with any request to GitHub."""
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            c = self.cfg()
            c["agents"]["claude"]["secrets"][name] = {}
            with self.assertRaisesRegex(ValueError, f"agents.claude.secrets: {name} is set by cilly"):
                C.validate(c)
            c = self.cfg()
            c["research"]["secrets"][name] = {}
            with self.assertRaisesRegex(ValueError, "research agents never get a GitHub token"):
                C.validate(c)

    def test_secret_command_must_be_argv(self):
        c = self.cfg()
        c["panel_token"]["secret"] = {"command": "pass show x"}
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_identity_must_be_noreply(self):
        c = self.cfg()
        c["identity"]["email"] = "me@example.com"
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_branch_prefix_not_main(self):
        c = self.cfg()
        c["identity"]["branch_prefix"] = "main/"
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_branch_names(self):
        c = self.cfg()
        self.assertEqual((C.branch_names(c), c["identity"]["branch_prefix"]), ("kind", "cilly/"))
        name = lambda text, taken=(): C.new_branch_name(c, "9a70b774", text, 3, taken)  # noqa: E731
        self.assertEqual([name(t) for t in ("Fix the login timeout on mobile", "Add dark mode to the settings page",
                                            "docs: explain routes", "Refactor the proxy", "", "Écrire des tests 😀",
                                            "Please make the whole thing faster and smaller and nicer")],
                         ["fix/login-timeout-mobile", "feat/add-dark-mode-settings-page", "docs/explain-routes",
                          "refactor/proxy", "feat/work", "feat/ecrire-des-tests", "feat/make-whole-thing-faster-smaller"])
        self.assertEqual(name("Fix login", {"fix/login", "fix/login-2"}), "fix/login-3")
        c["identity"]["branch_names"] = "prefix"
        self.assertEqual(name("Fix login"), "cilly/fix-login")
        c["identity"]["branch_names"] = "id"
        self.assertEqual((name("Fix login"), C.branch_pattern(c)), ("cilly/9a70b774-3", None))
        self.assertTrue(C.is_id_branch("vmpanel/9a70b774-12", "9a70b774"))
        self.assertFalse(C.is_id_branch("vmpanel/9a70b774x", "9a70b774"))
        c["identity"]["branch_names"] = "random"
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_names_the_panel_makes_are_ones_the_proxy_lets_through(self):
        """config.py and egress_proxy.py each have the naming rule: they must agree."""
        from cilly import egress_proxy as E
        names = ["fix/login", "feat/a-b-c", "docs/x", "cilly/login", "fix/Login", "fix/a/b", "fix/-a", "fix/a--b",
                 "wip/a", "fix/" + "a" * 60, "fix/" + "a" * 61, "cilly/a/b", "main", "fix/", "fix/a_b"]
        for mode in ("kind", "prefix"):
            c = self.cfg()
            c["identity"]["branch_names"] = mode
            rule = E.parse_push_rule(C.push_rule(c, "9a70b774"))
            for n in names:
                self.assertEqual(bool(re.fullmatch(C.branch_pattern(c), n)), E.push_allowed(rule, "refs/heads/" + n, True, set()), (mode, n))
            for text in ("Fix the login timeout", "", "Ünïcode only", "a" * 300):
                made = C.new_branch_name(c, "9a70b774", text, 1)
                self.assertTrue(E.push_allowed(rule, "refs/heads/" + made, True, set()), made)
                self.assertTrue(E.push_allowed(rule, "refs/heads/" + made + "-9", True, set()), made)  # with the runner's -2 ... -9

    def test_repos_and_token_lookup(self):
        c = self.cfg()
        c["tokens"]["bot"]["repos"] = ["me/a", "me/b"]
        C.validate(c)
        self.assertEqual(C.repos(c), ["me/a", "me/b"])
        self.assertEqual(C.token_for_repo(c, "me/b"), "bot")
        self.assertIsNone(C.token_for_repo(c, "me/c"))
        c["tokens"]["bot"]["repos"] = ["../x"]
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_default_policy(self):
        pol = C.DEFAULT_CONFIG["claude_policy"]
        ms = pol["managed_settings"]
        self.assertEqual(ms["attribution"], {"commit": "", "pr": "", "sessionUrl": False})
        for k in ("DISABLE_ERROR_REPORTING", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY", "DISABLE_FEEDBACK_COMMAND"):
            self.assertEqual(ms["env"][k], "1")
        self.assertEqual((pol["claude_md"], pol["dash_check"]), ("", False))  # yours to write, or the recommended rules from setup
        self.assertTrue(ms["allowManagedHooksOnly"])

    def test_the_recommended_claude_md(self):
        rec = C.CLAUDE_MD_RECOMMENDED
        self.assertTrue(rec.startswith("# Working style\n"))
        self.assertLess(len(rec.splitlines()), 40)  # short: it's read at every start
        for text in [rec] + [o[1] + o[2] for o in C.CLAUDE_MD_OPTIONS]:
            self.assertFalse({"\u2014", "\u2013"} & set(text), text[:40])
        self.assertEqual([o[0] for o in C.CLAUDE_MD_OPTIONS], ["no_dashes", "ascii", "no_emojis"])

    def test_keep_the_plan_adds_rules_and_a_hook_after_compaction(self):
        c = self.cfg()
        c["claude_policy"]["claude_md"] = "# Mine\n\n- Be brief.\n"
        c["claude_policy"]["managed_settings"]["hooks"] = {"SessionStart": [{"matcher": "startup", "hooks": []}]}
        off = C.effective_policy(c)
        self.assertNotIn("plan.md", json.dumps(off))  # off by default
        c["claude_policy"]["keep_plan"] = True
        C.validate(c)
        eff = C.effective_policy(c)
        self.assertNotIn("keep_plan", eff)  # not a Claude Code setting
        self.assertTrue(eff["claude_md"].startswith("# Mine\n\n- Be brief.\n\n# Long tasks (added by cilly)\n"))  # after yours
        self.assertIn("~/plan.md", eff["claude_md"])
        start = eff["managed_settings"]["hooks"]["SessionStart"]
        self.assertEqual([h["matcher"] for h in start], ["startup", "compact"])  # yours kept
        c["claude_policy"]["claude_md"] = ""
        self.assertTrue(C.effective_policy(c)["claude_md"].startswith("# Long tasks"))
        # the command, as Claude Code runs it: what it prints goes back into the conversation
        cmd = start[1]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as home:
            run = lambda: subprocess.run(cmd, shell=True, capture_output=True, text=True, env=dict(os.environ, HOME=home))  # noqa: E731
            self.assertEqual((run().returncode, run().stdout), (0, ""))  # no plan yet: nothing
            with open(os.path.join(home, "plan.md"), "w") as f:
                f.write("- [x] one\n- [ ] two\n" + "x" * 20000)
            r = run()
            self.assertEqual(r.returncode, 0)
            self.assertTrue(r.stdout.startswith("Your plan, from ~/plan.md (the conversation was just compacted):\n- [x] one\n- [ ] two\n"))
            self.assertLess(len(r.stdout), 8200)  # a big file doesn't fill the fresh context
        c["claude_policy"]["keep_plan"] = 1
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_the_dash_check_is_a_managed_hook(self):
        c = self.cfg()
        c["claude_policy"]["managed_settings"]["hooks"] = {"PostToolUse": [{"matcher": "Bash", "hooks": []}]}
        self.assertNotIn("Edit|MultiEdit|Write", json.dumps(C.effective_policy(c)))  # off by default
        c["claude_policy"]["dash_check"] = True
        C.validate(c)
        eff = C.effective_policy(c)
        self.assertNotIn("dash_check", eff)  # not a Claude Code setting
        post = eff["managed_settings"]["hooks"]["PostToolUse"]
        self.assertEqual([h["matcher"] for h in post], ["Bash", "Edit|MultiEdit|Write"])  # yours kept
        self.assertTrue(eff["managed_settings"]["allowManagedHooksOnly"])
        self.assertEqual(len(c["claude_policy"]["managed_settings"]["hooks"]["PostToolUse"]), 1)  # the config itself untouched
        # the command itself, as Claude Code runs it: exit 2 with the lines tells Claude to fix them
        cmd = post[1]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as d:
            for name, text, code in (("ok.md", "a - b\n", 0), ("bad.md", "one\ntwo \u2014 three\nfour \u2013 five\n", 2)):
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                r = subprocess.run(cmd, shell=True, input=json.dumps({"tool_input": {"file_path": path}}), capture_output=True, text=True)
                self.assertEqual(r.returncode, code, r.stderr)
            self.assertIn("line 2, 3.", r.stderr)
        for stdin in ("{}", json.dumps({"tool_input": {"file_path": "/nonexistent"}})):
            self.assertEqual(subprocess.run(cmd, shell=True, input=stdin, capture_output=True, text=True).returncode, 0)
        c["claude_policy"]["dash_check"] = "yes"
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_effective_policy_locks_bypass_off(self):
        c = self.cfg()
        self.assertNotIn("disableBypassPermissionsMode", C.effective_policy(c)["managed_settings"]["permissions"])
        c["claude_policy"]["permission_mode"] = "dontAsk"
        eff = C.effective_policy(c)
        self.assertEqual(eff["managed_settings"]["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertNotIn("disableBypassPermissionsMode", c["claude_policy"]["managed_settings"]["permissions"])

    def test_bad_allowlist(self):
        c = self.cfg()
        c["egress"]["allow"].append("*")
        with self.assertRaises(ValueError):
            C.validate(c)



class RepoNameTests(unittest.TestCase):
    def test_dot_names_rejected(self):
        for bad in ("../evil", "me/..", "./x", "me/.", "me", "me/a/b", "me/a b"):
            self.assertFalse(C.valid_repo(bad), bad)
        for ok in ("me/repo", "me/a.b", "my-org/x_y.z"):
            self.assertTrue(C.valid_repo(ok), ok)


class BuildBaseCommandTests(unittest.TestCase):
    def test_with_adds_to_a_preset(self):
        from unittest import mock
        from cilly import __main__ as M
        from cilly import base_image as BI
        got, saved = [], []

        def fake_build(image, cfg, **kw):
            got.append((kw["toolchains"], kw["preset"]))
            return "base-x.qcow2"
        with mock.patch.object(BI, "build", fake_build), mock.patch.object(M.C, "load", lambda: {}), \
                mock.patch.object(M.C, "save", lambda cfg: saved.append(dict(cfg))), mock.patch("os.geteuid", lambda: 1000):
            self.assertEqual(M.main(["build-base", "--image", "x.qcow2", "--preset", "full", "--with", "ghidra"]), 0)
            self.assertEqual(M.main(["build-base", "--image", "x.qcow2", "--with", "ghidra"]), 0)
            self.assertEqual(M.main(["build-base", "--image", "x.qcow2", "--preset", "slim"]), 0)
            self.assertEqual(M.main(["build-base", "--image", "x.qcow2", "--preset", "slim", "--with", "nope"]), 2)
        self.assertEqual(got[0], (BI.PRESETS["full"]["toolchains"] + ["ghidra"], ""))  # preset + --with: custom, no label
        self.assertEqual(got[1], (["java", "ghidra"], ""))
        self.assertEqual(got[2], (BI.PRESETS["slim"]["toolchains"], "slim"))
        self.assertEqual(saved[0], {"base_image": "base-x.qcow2"})          # preset + --with is only the coding default
        self.assertEqual(saved[1], {"base_image": "base-x.qcow2"})
        self.assertEqual(saved[2], {"research_base_image": "base-x.qcow2"})  # a plain slim stays the research default


if __name__ == "__main__":
    unittest.main()
