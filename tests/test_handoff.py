"""Research agent ↔ coding agent handoff, end to end against the fake QEMU."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        env = {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
               "XDG_RUNTIME_DIR": os.path.join(t, "run"), "PATH": FAKE_BIN + os.pathsep + os.environ["PATH"],
               "FAKE_REQUEST": "1", "FAKE_MEMOUT": "none"}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "cilly"))
        with open(os.path.join(env["XDG_CONFIG_HOME"], "cilly", "config.json"), "w") as f:
            json.dump({"tokens": {"bot": {"repos": ["me/repo"], "secret": {}}}, "vm": {"use_bwrap": "off"}}, f)
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for f in os.listdir(FAKE_BIN):
            os.chmod(os.path.join(FAKE_BIN, f), 0o755)
        from cilly import vm as V
        from cilly.server import App
        open(os.path.join(V.images_dir(), "base-test.qcow2"), "w").close()
        self.tools = mock.patch.object(V, "detect_tools", lambda cfg: {
            "qemu": os.path.join(FAKE_BIN, "qemu-system-x86_64"), "qemu-img": os.path.join(FAKE_BIN, "qemu-img"),
            "bwrap": None, "virtiofsd": None, "xorriso": None, "kvm": True})
        self.tools.start()
        self.app = App(0)
        self.app.secrets.set_pasted("token:bot", "ghp_bot")
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-coding")
        self.app.secrets.set_pasted("research:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-research")

    def tearDown(self):
        self.app.manager.shutdown_all()
        self.tools.stop()
        self.env.stop()
        self.tmp.cleanup()

    def wait(self, cond, what, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def agents(self, repo="me/repo"):
        r = self.app.api_create_agent({"name": "researcher", "profile": "research", "mode": "persistent"})
        c = self.app.api_create_agent({"name": "coder", "profile": "coding", "mode": "persistent",
                                       "repo": repo})
        ra, ca = self.app.manager.get(r["id"]), self.app.manager.get(c["id"])
        self.wait(lambda: ra.meta["state"] == "ready" and ca.meta["state"] == "ready", "both ready")
        return ra, ca

    def log(self, a):
        return self.app.manager.read_log(a.id, "agent", 0)["text"]

    def test_research_agent_isolation(self):
        ra, ca = self.agents()
        with open(ra.paths.allow_file) as f:
            self.assertEqual(f.read().split(), ["*"])
        with open(ca.paths.allow_file) as f:
            self.assertNotIn("*", f.read().split())
        self.assertIsNone(ra.meta["repo"])
        self.app.api_agent_message(ra.id, {"instructions": "What is new in Rust 1.80?"})
        self.wait(lambda: "research task" in self.log(ra), "research task")
        self.assertIn("env=CLAUDE_CODE_OAUTH_TOKEN q=What is new in Rust 1.80?", self.log(ra))  # no GitHub token
        # the findings are stored by a background thread: let it finish before cleanup
        self.wait(lambda: self.app.api_deliveries(ra.id, {})["deliveries"], "delivery stored")
        from cilly.server import ApiError
        with self.assertRaises(ApiError):
            self.app.api_create_agent({"name": "r2", "profile": "research",
                                       "inject": {"manifest_id": "x", "mode": "copy"}})

    def test_inbox_collects_and_dismisses(self):
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        self.app.api_agent_message(ca.id, {"instructions": "Add a custom recipe"})
        self.wait(lambda: ca.meta.get("requests"), "research request")
        with open(ca.paths.egress_log, "a") as f:
            for ts in ("2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"):
                f.write(json.dumps({"ts": ts, "action": "deny", "host": "registry.example.org", "port": 443,
                                    "reason": "not allowlisted"}) + "\n")
        inbox = self.app.api_inbox({})
        kinds = sorted(i["kind"] for i in inbox["items"])
        self.assertEqual(kinds, ["network", "research"])
        net = next(i for i in inbox["items"] if i["kind"] == "network")
        self.assertEqual((net["host"], net["count"], len(net["lines"])), ("registry.example.org", 2, 2))
        self.assertIn("deny registry.example.org:443", net["lines"][-1])
        # dismissed: gone, and noted for the chat ...
        self.app.api_denial_dismiss(ca.id, {"host": "registry.example.org", "port": 443})
        self.assertEqual([i["kind"] for i in self.app.api_inbox({})["items"]], ["research"])
        self.assertEqual(ca.meta["denied_hosts"][-1]["host"], "registry.example.org")
        # ... until the agent tries again
        with open(ca.paths.egress_log, "a") as f:
            f.write(json.dumps({"ts": "2026-01-01T00:00:09Z", "action": "deny", "host": "registry.example.org",
                                "port": 443}) + "\n")
        self.assertIn("network", [i["kind"] for i in self.app.api_inbox({})["items"]])
        from cilly.server import ApiError
        with self.assertRaises(ApiError):
            self.app.api_denial_dismiss(ca.id, {"host": "never.example", "port": 443})
        # the open web's port limit: shown for the research agent, and allowing the host can't fix it
        with open(ra.paths.egress_log, "a") as f:
            f.write(json.dumps({"ts": "2026-01-01T00:00:10Z", "action": "deny", "host": "nas.example.org", "port": 22,
                                "code": "open_web_port", "reason": "port 22: the open web is only reachable on ports 443 and 80"}) + "\n")
        net = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "network" and i["agent"] == ra.id)
        self.assertEqual((net["host"], net["port"], net["fixable"]), ("nas.example.org", 22, False))
        self.assertTrue(next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "network" and i["agent"] == ca.id)["fixable"])
        with open(ca.paths.egress_log, "a") as f:  # a port not in egress.ports: allowing host:port fixes it
            f.write(json.dumps({"ts": "2026-01-01T00:00:11Z", "action": "deny", "host": "github.com", "port": 22,
                                "code": "port", "reason": "port 22 isn't allowed for github.com"}) + "\n")
        net = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "network" and i["host"] == "github.com")
        self.assertEqual((net["port"], net["fixable"]), (22, True))  # shown, though github.com is allowed
        self.app.api_allow(ca.id, {"host": "github.com:22"})
        self.assertFalse([i for i in self.app.api_inbox({})["items"] if i.get("host") == "github.com"])  # approved since
        with open(ra.paths.egress_log, "a") as f:  # a research agent has only the open web: Dismiss only
            f.write(json.dumps({"ts": "2026-01-01T00:00:12Z", "action": "deny", "host": "nas.example.org", "port": 22,
                                "code": "port", "reason": "port 22 not allowed"}) + "\n")
        net = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "network" and i["agent"] == ra.id)
        self.assertFalse(net["fixable"])
        self.app.api_denial_dismiss(ra.id, {"host": "nas.example.org", "port": 22})
        self.assertFalse([i for i in self.app.api_inbox({})["items"] if i["agent"] == ra.id and i["kind"] == "network"])
        with open(ca.paths.egress_log, "a") as f:  # the route failed: the inbox says so, not "your own network"
            f.write(json.dumps({"ts": "2026-01-01T00:00:13Z", "action": "deny", "host": "github.com", "port": 443,
                                "code": "route_down", "reason": "the route refused: host unreachable"}) + "\n")
        net = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "network" and i["host"] == "github.com")
        self.assertEqual((net["fixable"], net["code"]), (False, "route_down"))
        self.app.api_denial_dismiss(ca.id, {"host": "github.com", "port": 443})
        # findings: approve the request, the research agent delivers, the inbox offers them
        req = ca.meta["requests"][0]
        self.app.api_request_action(ca.id, req["id"], "approve", {"text": "Edited: how do recipes work?"})
        self.wait(lambda: any(i["kind"] == "findings" for i in self.app.api_inbox({})["items"]), "findings in inbox")
        self.assertIn("Edited: how do recipes work?", self.log(ra))  # the edited question is what left the sandbox
        fin = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "findings")
        self.assertEqual((fin["agent"], fin["for_agent"]), (ra.id, ca.id))
        self.assertEqual(ca.meta["requests"][0]["state"], "delivered")
        self.app.api_delivery_dismiss(ra.id, fin["id"])
        self.assertNotIn("findings", [i["kind"] for i in self.app.api_inbox({})["items"]])
        self.assertEqual(ca.meta["requests"][0]["state"], "dismissed")  # Done on the coding agent's Research tab
        self.assertEqual(self.app.api_inbox({})["count"], 1)  # the network one
        # findings for a question you asked the research agent yourself stay on its Findings tab
        from cilly import handoff as H
        before = len(H.list_all(ra.paths.vm_dir))
        self.app.api_agent_message(ra.id, {"instructions": "What is new in Rust 1.80?"})
        self.wait(lambda: len(H.list_all(ra.paths.vm_dir)) > before, "own findings")
        self.wait(lambda: ra.meta["tasks"][-1].get("images") == ["screenshot.png"], "its image in the chat")
        self.assertEqual([f["path"] for f in H.list_all(ra.paths.vm_dir)[0]["files"]], ["findings.md"])  # text only
        self.assertNotIn("findings", [i["kind"] for i in self.app.api_inbox({})["items"]])

    def test_projects(self):
        import base64
        os.environ["FAKE_NOTES"] = "- The app targets Node 22.\n- The app targets Node 22.\n- The token is sk-secret-for-notes-1234.\n"
        self.app.secrets.set_pasted("mcp:LEAKY", "sk-secret-for-notes-1234")  # a secret the panel holds
        ra, ca = self.agents()
        app = self.app
        pid = app.api_project_create({"name": "Lantern"})["id"]
        r = app.api_project_update(pid, {"instructions": "Use pnpm for packages.", "allow_text": "crates.io\n*",
                                         "research_agent": ra.id, "defaults": {"model": "opus", "effort": "high"}})
        self.assertEqual(r["allow"], ["crates.io"])
        self.assertNotIn("*", r["allow"])  # never the open web for coding agents
        app.api_project_file(pid, {"name": "guide.md", "data": base64.b64encode(b"# Guide").decode()})
        app.api_agent_organize(ca.id, {"project": pid})
        app.api_agent_organize(ra.id, {"project": pid})
        # the coding agent follows the project: sites, research agent, instructions and files
        with open(ca.paths.allow_file) as f:
            self.assertIn("crates.io", f.read().split())
        self.assertEqual(ca.meta["research_agent"], ra.id)
        md = app.policy_for(ca)["claude_md"]
        for s in ("# Project: Lantern", "Use pnpm for packages.", "/attachments/project/", "guide.md", "project-notes.md"):
            self.assertIn(s, md)
        self.wait(lambda: "attached /attachments/project/guide.md" in self.log(ca), "project file in the VM")
        # the research agent only shares the sidebar: no files, instructions or notes (it has the open web)
        self.assertNotIn("# Project:", app.policy_for(ra)["claude_md"])
        self.assertNotIn("attached /attachments/project", self.log(ra))
        # a note it suggests waits in the inbox, once
        app.api_agent_message(ca.id, {"instructions": "Research results read, carry on"})
        self.wait(lambda: any(i["kind"] == "note" for i in app.api_inbox({})["items"]), "suggested note")
        notes = [i for i in app.api_inbox({})["items"] if i["kind"] == "note"]
        self.assertEqual(sorted(n["text"] for n in notes), ["The app targets Node 22.", "The token is [hidden]."])
        self.assertTrue(all(n["project"] == pid for n in notes))
        self.assertEqual(len([i for i in app._inbox_items(ca.id) if i["kind"] == "note"]), 2)  # what Stop When Idle checks
        self.assertFalse([i for i in app._inbox_items(ra.id) if i["kind"] == "note"])
        notes = [n for n in notes if "Node 22" in n["text"]]
        self.assertTrue(all(i["project"] == pid for i in app.api_inbox({})["items"] if i["agent"] == ca.id))
        self.assertNotIn("The app targets Node 22", app.policy_for(ca)["claude_md"])  # not before you approve
        app.api_project_note(pid, notes[0]["id"], "approve", {})
        self.assertIn("- The app targets Node 22.", app.policy_for(ca)["claude_md"])
        # a new agent in the project starts with its model and effort
        n = app.api_create_agent({"name": "new", "repo": "me/repo", "mode": "persistent", "project": pid})
        self.assertEqual((n["project"], n["model"], n["effort"], n["research_agent"]), (pid, "opus", "high", ra.id))
        # deleting the project keeps its agents, under Other Agents
        app.api_project_delete(pid)
        self.assertIsNone(ca.meta.get("project"))
        self.assertNotIn("# Project:", app.policy_for(ca)["claude_md"])
        with open(ca.paths.allow_file) as f:
            self.assertNotIn("crates.io", f.read().split())

    def test_groups_become_projects(self):
        _, ca = self.agents()
        ca.set(group="Old Group")
        self.app._groups_to_projects()
        p = self.app.projects.find("Old Group")
        self.assertEqual((ca.meta["project"], ca.meta.get("group")), (p["id"], None))

    def test_review_mode(self):
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        self.app.api_agent_message(ca.id, {"instructions": "Add a custom recipe"})
        self.wait(lambda: ca.meta.get("requests"), "research request")
        # told how to ask (the log and the request travel on different channels: wait for the log too)
        self.wait(lambda: "Answers will be delivered read-only under /inbox/" in self.log(ca), "coding hint in the task")
        req = ca.meta["requests"][0]
        self.assertEqual(req["state"], "pending")  # review mode: nothing happens on its own
        time.sleep(0.3)
        self.assertEqual(ra.meta["tasks"], [])
        self.app.api_request_action(ca.id, req["id"], "approve", {})
        self.wait(lambda: req["state"] == "delivered" or ca.meta["requests"][0]["state"] == "delivered", "delivery")
        dels = self.app.api_deliveries(ra.id, {})["deliveries"]
        self.assertEqual(len(dels), 1)
        got = self.app.api_delivery_get(ra.id, dels[0]["id"])
        self.assertIn("Flavored Markdown", got["files"][0]["content"])
        self.assertNotIn("\ninbox ", self.log(ca))  # not forwarded until you say so
        self.app.api_delivery_forward(ra.id, dels[0]["id"], {"to": ca.id, "notify": True})
        self.wait(lambda: f"inbox {dels[0]['id']} files=findings.md" in self.log(ca), "inbox delivered")
        self.wait(lambda: "Research results (markdown-tables)" in self.log(ca), "notify task")
        self.assertEqual(ca.meta["requests"][0]["state"], "forwarded")

    def test_the_inbox_says_what_findings_left_out(self):
        os.environ["FAKE_GIT_FINDINGS"] = "1"
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        self.app.api_agent_message(ca.id, {"instructions": "Add a custom recipe"})
        self.wait(lambda: ca.meta.get("requests"), "research request")
        self.app.api_request_action(ca.id, ca.meta["requests"][0]["id"], "approve", {})
        self.wait(lambda: any(i["kind"] == "findings" for i in self.app.api_inbox({})["items"]), "findings in inbox")
        fin = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "findings")
        self.assertEqual((fin["files"], fin["left_out"]), (["findings.md"], 1))
        self.assertEqual(self.app.api_deliveries(ra.id, {})["deliveries"][0]["left_out"], 1)  # its own Findings list too

    def test_yolo_mode(self):
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": True})
        self.app.api_agent_message(ca.id, {"instructions": "Add a custom recipe"})
        self.wait(lambda: "Research results (markdown-tables) are available read-only in /inbox/" in self.log(ca),
                  "automatic round trip")
        self.assertIn("\ninbox ", self.log(ca))
        self.assertEqual(ca.meta["requests"][0]["state"], "forwarded")

    def test_yolo_stops_after_the_round_limit(self):
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": True})
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["research"]["yolo_rounds"] = 2
        self.app.set_cfg(cfg)
        def ask(n):
            ca.meta["tasks"].append({"id": f"ask{n}", "state": "done"})  # questions come at the end of a task
            self.app.on_requests(ca, {"task": f"ask{n}", "requests": [{"name": f"q{n}", "text": f"Question {n}?"}]})
        for n in (1, 2, 3):
            ask(n)
        states = {r["name"]: r["state"] for r in ca.meta["requests"]}
        self.assertNotEqual(states["q1"], "pending")
        self.assertNotEqual(states["q2"], "pending")
        self.assertEqual(states["q3"], "pending")  # waits in the inbox
        self.assertTrue(ca.meta["yolo_paused"])
        self.assertIn("q3", [i.get("name") for i in self.app.api_inbox({})["items"]])
        # a message from you starts the count again
        self.app.api_agent_message(ca.id, {"instructions": "Research results read, carry on"})  # the fake VM asks nothing back
        self.assertEqual((ca.meta["yolo_rounds"], ca.meta["yolo_paused"]), (0, False))
        ask(4)
        self.assertNotEqual(next(r for r in ca.meta["requests"] if r["name"] == "q4")["state"], "pending")
        # no limit: never pauses
        cfg["research"]["yolo_rounds"] = None
        self.app.set_cfg(cfg)
        for n in range(5, 10):
            ask(n)
        self.assertFalse(ca.meta["yolo_paused"])
        self.assertFalse([r for r in ca.meta["requests"] if r["name"] in [f"q{n}" for n in range(5, 10)] and r["state"] == "pending"])
        # let the approved ones finish their round trips before the agents are torn down
        self.wait(lambda: all(r["state"] in ("pending", "forwarded") for r in ca.meta["requests"]), "round trips", timeout=30)
        self.wait(lambda: not any(t["state"] in ("queued", "running") for t in ca.meta["tasks"] + ra.meta["tasks"]), "idle", timeout=30)

    def test_findings_dismissed_as_soon_as_they_show_stay_dismissed(self):
        """The inbox offers findings once they're stored. Dismissing them then must close the question
        too, and nothing that comes later may open it again."""
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        self.app.api_agent_message(ca.id, {"instructions": "Add a custom recipe"})
        self.wait(lambda: ca.meta.get("requests"), "research request")
        send, seen = ra.send, []

        def dismiss_first(msg, *a, **kw):  # the panel tells the VM they're stored: the user is quicker
            if msg.get("type") == "outbox_ok":
                fin = next(i for i in self.app.api_inbox({})["items"] if i["kind"] == "findings")
                self.app.api_delivery_dismiss(ra.id, fin["id"])
                seen.append(fin["id"])
            return send(msg, *a, **kw)
        with mock.patch.object(ra, "send", side_effect=dismiss_first):
            self.app.api_request_action(ca.id, ca.meta["requests"][0]["id"], "approve", {})
            self.wait(lambda: seen, "findings stored")
        self.assertEqual(ca.meta["requests"][0]["state"], "dismissed")

    def test_round_trip_for_an_agent_without_a_repository(self):
        ra, ca = self.agents(repo=None)
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": True})
        self.app.api_agent_message(ca.id, {"instructions": "Check the Node docs"})
        self.wait(lambda: "Research results (markdown-tables) are available read-only in /inbox/" in self.log(ca),
                  "the agent is told the answer arrived")
        self.assertEqual(ca.meta["requests"][0]["state"], "forwarded")

    def test_forward_to_stopped_agent_delivers_on_start(self):
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        self.app.api_agent_message(ra.id, {"instructions": "Markdown tables"})
        self.wait(lambda: self.app.api_deliveries(ra.id, {})["deliveries"], "delivery")
        did = self.app.api_deliveries(ra.id, {})["deliveries"][0]["id"]
        self.app.manager.stop(ca.id)
        self.wait(lambda: not ca.running and ca.meta["state"] == "stopped", "stop")
        res = self.app.api_delivery_forward(ra.id, did, {"to": ca.id, "notify": False})
        self.assertFalse(res["delivered_now"])
        self.assertEqual(ca.meta["inbox_pending"][0]["did"], did)
        self.app.api_agent_start(ca.id, {})
        self.wait(lambda: f"inbox {did} files=findings.md" in self.log(ca).rsplit("===== cilly: start", 1)[1],
                  "delivered at start")
        self.assertEqual(ca.meta["inbox_pending"], [])

    def test_big_findings_arrive_whole(self):
        os.environ["FAKE_BIG_FINDINGS"] = "chunks"
        ra, _ = self.agents()
        self.app.api_agent_message(ra.id, {"instructions": "Lots of text"})
        self.wait(lambda: self.app.api_deliveries(ra.id, {})["deliveries"], "delivery")
        d = self.app.api_deliveries(ra.id, {})["deliveries"][0]
        files = self.app.api_delivery_get(ra.id, d["id"])["files"]
        want = ("Größe ✓ 🔬 " * 70_000)[:340_000]
        self.assertEqual([f["content"] == want for f in files], [True] * 3)
        self.wait(lambda: f"outbox_ok {d['task']}" in self.log(ra), "the runner is told they arrived")
        self.assertIsNone(ra.meta.get("error"))

    def test_a_dropped_line_is_reported(self):
        os.environ["FAKE_BIG_FINDINGS"] = "old"  # an older runner sends everything in one line
        ra, _ = self.agents()
        self.app.api_agent_message(ra.id, {"instructions": "Lots of text"})
        self.wait(lambda: ra.meta.get("error"), "an error on the agent")
        self.assertIn("larger than 1 MiB", ra.meta["error"])
        dropped = []
        with mock.patch.object(self.app.manager, "_dropped", lambda ag, n: dropped.append(n)):
            ra.meta["tasks"].append({"id": "again", "state": "running"})
            line = json.dumps({"type": "outbox", "task": "again", "files": [{"path": "a.md", "content": "é" * 600_000}]}).encode()
            import socket
            import threading
            host, guest = socket.socketpair()
            t = threading.Thread(target=self.app.manager._control_reader, args=(ra, host))
            t.start()
            guest.sendall(line + b"\n" + json.dumps({"type": "status", "task": "again", "state": "done"}).encode() + b"\n")
            guest.close()
            t.join(5)
        self.assertEqual(dropped, [len(line)])  # once, for the whole line
        self.assertEqual(ra.meta["tasks"][-1]["state"], "done")  # and the next message still counts
        self.assertEqual(self.app.api_deliveries(ra.id, {})["deliveries"], [])
        self.app.api_agent_organize(ra.id, {"clear_error": True})
        self.assertIsNone(ra.meta["error"])

    def test_refused_findings_are_answered(self):
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["research"]["max_delivery_bytes"] = 20  # lower than what the agent writes
        self.app.set_cfg(cfg)
        ra, _ = self.agents()
        self.app.api_agent_message(ra.id, {"instructions": "Markdown tables"})
        self.wait(lambda: "outbox_refused" in self.log(ra), "the runner is told")
        self.assertIn(f"outbox_refused {ra.meta['tasks'][-1]['id']}: delivery too large", self.log(ra))
        self.assertIn("rejected", ra.meta["error"])

    def test_a_vm_that_stops_reading_holds_up_nothing(self):
        import socket
        import subprocess
        import threading
        mgr = self.app.manager
        ra = mgr.create(name="mute", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/", profile="research")
        host, guest = socket.socketpair()  # the VM's end, never read
        host.setblocking(False)
        try:
            while True:
                host.send(b"x" * 65536)
        except BlockingIOError:
            pass  # the buffer is full: every send from now on blocks
        host.setblocking(True)
        ra.ctl = host
        ra.procs["qemu"] = subprocess.Popen(["sleep", "30"])  # looks running
        try:
            for i in range(3):  # old-style findings for tasks that don't exist, short and long ids
                for tid in (f"nope{i}", "x" * 40 + str(i)):
                    threading.Thread(target=self.app.on_outbox, args=(ra, {"task": tid, "files": []}), daemon=True).start()
            time.sleep(0.5)

            def answers(fn):
                t = threading.Thread(target=fn, daemon=True)
                t.start()
                t.join(3)
                return not t.is_alive()
            self.assertTrue(answers(ra.public))
            self.assertTrue(answers(lambda: self.app.api_state({})))
            self.assertTrue(answers(lambda: ra.set(note="still writable")))
            self.assertTrue(answers(lambda: self.app.api_agent_action(ra.id, "stop", {})))
        finally:
            guest.close()  # the blocked sends fail and their threads end
            ra.procs["qemu"].kill()
            ra.procs["qemu"].wait()

    def test_a_question_whose_findings_never_arrive_can_be_asked_again(self):
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["research"]["max_delivery_bytes"] = 20  # the findings will be refused
        self.app.set_cfg(cfg)
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        self.app.api_agent_message(ca.id, {"instructions": "Add a custom recipe"})
        self.wait(lambda: ca.meta.get("requests"), "research request")
        rid = ca.meta["requests"][0]["id"]
        self.app.api_request_action(ca.id, rid, "approve", {})
        self.wait(lambda: ca.meta["requests"][0]["state"] == "failed", "the question fails")
        self.assertIn("refused the findings: delivery too large", ca.meta["requests"][0]["error"])
        self.app.api_request_action(ca.id, rid, "approve", {})  # and can be approved again
        self.assertEqual(ca.meta["requests"][0]["state"], "researching")
        self.wait(lambda: ca.meta["requests"][0]["state"] == "failed", "fails again")
        # findings the runner tried to send that never arrived (it set them aside, unconfirmed)
        self.app.FINDINGS_GRACE = 0.2
        ra.meta["tasks"].append({"id": "lost1", "state": "done", "request": rid, "for_agent": ca.id, "outbox_files": 2})
        self.app._set_request(ca, rid, state="researching", research_task="lost1")
        self.app.after_task(ra, "lost1")
        self.assertEqual((ca.meta["requests"][0]["state"], ca.meta["requests"][0]["error"]),
                         ("failed", "the findings didn't reach the panel"))
        self.assertIn("~/sent/unconfirmed-lost1", ra.meta["error"])  # shown on the research agent too

    def test_a_question_whose_research_agent_was_deleted_can_be_asked_again(self):
        """Its findings went with that agent, so Review, Dismiss and Forward had nothing to go through."""
        ra, ca = self.agents()
        self.app.api_link(ca.id, {"research_agent": ra.id, "auto": False})
        ca.meta["requests"] = [{"id": "r1", "name": "angles", "text": "Which angles?", "state": "delivered", "research_agent": ra.id,
                                "delivery": "d1", "created": "2026-10-16T15:11:00Z"},
                               {"id": "r2", "name": "other", "text": "?", "state": "pending", "created": "2026-10-16T15:12:00Z"}]
        ca.save_meta()
        self.app.manager.stop(ra.id)
        self.wait(lambda: not ra.running, "the research agent stops")
        self.app.api_agent_action(ra.id, "delete", {})
        r1, r2 = ca.meta["requests"]
        self.assertEqual((r1["state"], r1["delivery"]), ("failed", None))
        self.assertIn("research agent was deleted", r1["error"])
        self.assertEqual(r2["state"], "pending")  # not taken by it: as it was
        # one lost before this version: found when the panel starts
        from cilly.server import App
        ca.meta["requests"][0].update(state="delivered", research_agent="0badc0de", error=None)
        ca.save_meta()
        self.app.manager.shutdown_all()
        app = App(0)
        self.assertEqual(app.manager.get(ca.id).meta["requests"][0]["state"], "failed")
        app.manager.shutdown_all()

    def test_bad_delivery_rejected(self):
        ra, _ = self.agents()
        answers, send = [], ra.send
        ra.send = lambda m: answers.append(m) if m.get("type") == "outbox_refused" else send(m)
        ra.meta["tasks"] += [{"id": "x1", "state": "done"}, {"id": "x2", "state": "done"}, {"id": "x3", "state": "done"}]
        self.app.on_outbox(ra, {"task": "x1", "files": [{"path": "../../etc/passwd", "content": "x"}]})
        self.assertIn("rejected", ra.meta["error"])
        self.app.on_outbox(ra, {"task": "x2", "files": [{"path": "a.md", "content": "x" * (3 * 1024 * 1024)}]})
        self.app.on_outbox(ra, {"task": "x3", "files": [{"path": "a", "content": "x"}, {"path": "a/b", "content": "y"}]})
        self.assertIn("inside a file", ra.meta["error"])
        self.assertEqual(ra.meta["error"], "The panel rejected research findings. 'a/b' is inside a file of the same name.")
        ra.meta["tasks"].append({"id": "x4", "state": "done"})
        self.app.on_outbox(ra, {"task": "x4", "files": [{"path": "a.md", "content": "\ud800"}]})
        self.assertEqual(ra.meta["error"], "The panel rejected research findings. They aren't valid UTF-8 text.")
        self.app.on_outbox(ra, {"task": "nope", "files": [{"path": "a", "content": "x"}]})
        self.assertEqual([(m["type"], m["task"]) for m in answers],
                         [("outbox_refused", t) for t in ("x1", "x2", "x3", "x4", "nope")])  # each one answered, with why
        self.assertIn("unsafe file name", answers[0]["error"])
        self.assertEqual(self.app.api_deliveries(ra.id, {})["deliveries"], [])
        self.assertEqual(os.listdir(os.path.join(ra.paths.vm_dir, "deliveries")) if os.path.isdir(
            os.path.join(ra.paths.vm_dir, "deliveries")) else [], [])  # nothing half-written

    def test_a_flood_of_deliveries_is_refused(self):
        from cilly import server as S
        ra, ca = self.agents()
        files = [{"path": "f.md", "content": "findings"}]
        self.app.on_outbox(ra, {"task": "not-a-task", "files": files})  # only for tasks you started
        for i in range(S.MAX_UNDECIDED_FINDINGS + 5):
            ra.meta["tasks"].append({"id": f"r{i}", "state": "done", "for_agent": ca.id})
            for _ in range(3):  # and once per task
                self.app.on_outbox(ra, {"task": f"r{i}", "files": files})
        self.assertEqual(len(self.app.api_deliveries(ra.id, {})["deliveries"]), S.MAX_UNDECIDED_FINDINGS)
        self.assertIn("waiting for you", ra.meta["error"])
        # handling some makes room again
        did = self.app.api_deliveries(ra.id, {})["deliveries"][0]["id"]
        self.app.api_delivery_dismiss(ra.id, did)
        ra.meta["tasks"].append({"id": "after", "state": "done", "for_agent": ca.id})
        self.app.on_outbox(ra, {"task": "after", "files": files})
        self.assertEqual(len(self.app.api_deliveries(ra.id, {})["deliveries"]), S.MAX_UNDECIDED_FINDINGS + 1)

    def test_a_flood_of_research_questions_is_refused(self):
        from cilly import server as S
        _, ca = self.agents()
        self.app.on_requests(ca, {"task": "not-a-task", "requests": [{"name": "q", "text": "?"}]})
        self.assertEqual(ca.meta["requests"], [])
        for i in range(S.MAX_PENDING_REQUESTS):
            ca.meta["tasks"].append({"id": f"q{i}", "state": "done"})
            for _ in range(3):
                self.app.on_requests(ca, {"task": f"q{i}", "requests": [{"name": f"q{i}-{j}", "text": "?"} for j in range(10)]})
        self.assertEqual(len(ca.meta["requests"]), S.MAX_PENDING_REQUESTS)
        self.assertIn("dropped", ca.meta["error"])

    def test_mcp_per_agent_with_secret_prompt(self):
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["mcp_servers"] = {"pixellab": {"type": "http", "url": "https://api.pixellab.ai/mcp",
                                           "headers": {"Authorization": "Bearer ${PIXELLAB_API_KEY}"}}}
        self.app.set_cfg(cfg)
        _, ca = self.agents()
        with open(ca.paths.allow_file) as f:
            self.assertNotIn("api.pixellab.ai", f.read())
        pol = self.app.policy_for(ca)
        self.assertEqual(pol["managed_mcp"], {"mcpServers": {}})  # nothing unless ticked
        self.app.api_agent_mcp(ca.id, {"servers": ["pixellab"]})
        with open(ca.paths.allow_file) as f:
            self.assertIn("api.pixellab.ai", f.read().split())  # its host is allowed live
        self.wait(lambda: "policy updated" in self.log(ca), "live policy push")
        from cilly.server import ApiError
        with self.assertRaises(ApiError) as cm:  # key not known yet: the UI asks for it
            self.app.api_agent_message(ca.id, {"instructions": "draw a sprite"})
        self.assertEqual(cm.exception.extra["needs_secret"], "mcp:PIXELLAB_API_KEY")
        self.app.secrets.set_pasted("mcp:PIXELLAB_API_KEY", "pxl-secret")
        self.app.api_agent_message(ca.id, {"instructions": "draw a sprite"})
        self.wait(lambda: "PIXELLAB_API_KEY" in self.log(ca), "key sent with the task")
        self.assertEqual(self.app.policy_for(ca)["managed_mcp"]["mcpServers"]["pixellab"]["headers"]["Authorization"],
                         "Bearer ${PIXELLAB_API_KEY}")
        for root, _, files in os.walk(self.tmp.name):  # the key never lands on disk
            for fn in files:
                path = os.path.join(root, fn)
                if os.path.isfile(path) and not os.path.islink(path):
                    try:
                        with open(path, "rb") as fh:
                            self.assertNotIn(b"pxl-secret", fh.read(), fn)
                    except FileNotFoundError:
                        pass  # a temp file replaced while we walked
        with self.assertRaises(ApiError):
            self.app.api_agent_mcp(ca.id, {"servers": ["nope"]})

    def test_scratch_agent_without_repo(self):
        c = self.app.api_create_agent({"name": "scratch", "profile": "coding", "mode": "ephemeral"})
        ag = self.app.manager.get(c["id"])
        self.wait(lambda: ag.meta["state"] == "ready", "ready")
        self.app.api_agent_message(ag.id, {"instructions": "hello"})
        self.wait(lambda: "head='hello'" in self.log(ag), "task sent")
        self.assertIn("env=CLAUDE_CODE_OAUTH_TOKEN ", self.log(ag))  # no GitHub token without a repo

    def test_missing_claude_login_asks_for_it(self):
        self.app.secrets.forget("agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
        c = self.app.api_create_agent({"name": "x", "profile": "coding", "mode": "ephemeral"})
        ag = self.app.manager.get(c["id"])
        self.wait(lambda: ag.meta["state"] == "ready", "ready")
        from cilly.server import ApiError
        with self.assertRaises(ApiError) as cm:
            self.app.api_agent_message(ag.id, {"instructions": "hello"})
        self.assertEqual(cm.exception.extra.get("needs_secret"), "agent:claude:CLAUDE_CODE_OAUTH_TOKEN")

    def test_denial_approvals(self):
        _, ca = self.agents()
        with open(ca.paths.egress_log, "a") as f:
            for host in ("repo.maven.apache.org", "repo.maven.apache.org", "services.gradle.org"):
                f.write(json.dumps({"action": "deny", "host": host, "port": 443, "reason": "host not in allowlist",
                                    "ts": "2026-10-14T05:00:00Z"}) + "\n")
        den = self.app.api_denials(ca.id, {})["denials"]
        self.assertEqual({d["host"]: d["count"] for d in den},
                         {"repo.maven.apache.org": 2, "services.gradle.org": 1})
        self.app.api_allow(ca.id, {"host": "repo.maven.apache.org", "scope": "agent"})
        with open(ca.paths.allow_file) as f:
            self.assertIn("repo.maven.apache.org", f.read().split())
        self.assertEqual([d["host"] for d in self.app.api_denials(ca.id, {})["denials"]], ["services.gradle.org"])
        self.app.api_allow(ca.id, {"host": "*.gradle.org", "scope": "global"})
        self.assertIn("*.gradle.org", self.app.cfg()["egress"]["allow"])
        self.assertEqual(self.app.api_denials(ca.id, {})["denials"], [])
        from cilly.server import ApiError
        with self.assertRaises(ApiError):
            self.app.api_allow(ca.id, {"host": "*", "scope": "global"})  # no open web for coding agents

    def test_the_boot_self_test_is_not_a_blocked_request(self):
        _, ca = self.agents()
        win = ca.meta["selftest_windows"][-1]  # recorded when the VM said it was ready
        self.assertLessEqual(win[0], win[1])
        def deny(host, port, ts):
            with open(ca.paths.egress_log, "a") as f:
                f.write(json.dumps({"action": "deny", "host": host, "port": port, "reason": "x", "ts": ts}) + "\n")
        deny("example.com", 443, win[0])           # the self-test checking the proxy refuses it
        deny("api.github.com", 22, win[1])
        deny("evil.example.net", 443, win[0])      # anything else during boot still shows
        self.assertEqual([d["host"] for d in self.app.api_denials(ca.id, {})["denials"]], ["evil.example.net"])
        deny("example.com", 443, "2099-01-01T00:00:00Z")  # the agent itself, later
        self.assertEqual({d["host"] for d in self.app.api_denials(ca.id, {})["denials"]}, {"evil.example.net", "example.com"})


class NameTests(unittest.TestCase):
    def test_trailing_newline_is_not_a_safe_name(self):
        from cilly import handoff
        with self.assertRaises(handoff.HandoffError):
            handoff.validate_files([{"path": "a\n", "content": "x"}], 1000)


if __name__ == "__main__":
    unittest.main()
