import http.client
import json
import os
import tempfile
import shutil
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from cilly import config as C


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"XDG_CONFIG_HOME": os.path.join(cls.tmp.name, "cfg"),
               "XDG_DATA_HOME": os.path.join(cls.tmp.name, "data"),
               "XDG_RUNTIME_DIR": os.path.join(cls.tmp.name, "run")}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        cls.env = mock.patch.dict(os.environ, env)
        cls.env.start()
        from cilly.server import App, make_handler
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = cls.httpd.server_address[1]
        cls.app = App(port)
        cls.httpd.RequestHandlerClass = make_handler(cls.app)
        cls.port = port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        c.close()
        return out

    def auth(self):
        return {"X-Panel-Token": self.app.token}

    def paired_device(self, name="iPhone"):
        self.app.devices.starts.clear()  # the tests share one panel: its pairing rate limit is tested on its own
        st, _, b = self.req("POST", "/api/pair/start", {"name": name})
        self.assertEqual(st, 200)
        start = json.loads(b)
        st, _, b = self.req("POST", "/api/pair/poll", {"pair": start["pair"]})
        self.assertEqual(json.loads(b), {"state": "pending"})
        st, _, b = self.req("GET", "/api/state", headers=self.auth())
        pend = [x for x in json.loads(b)["pairing"] if x["code"] == start["code"]]
        self.assertEqual(len(pend), 1)
        st, _, _ = self.req("POST", f"/api/devices/pending/{pend[0]['id']}/approve", {}, headers=self.auth())
        self.assertEqual(st, 200)
        st, _, b = self.req("POST", "/api/pair/poll", {"pair": start["pair"]})
        r = json.loads(b)
        self.assertEqual(r["state"], "approved")
        self.addCleanup(self._unpair, r["device"]["id"])  # the panel takes at most 20 devices
        return {"X-Panel-Token": r["token"]}, r["device"]

    def _unpair(self, did):
        if did in self.app.devices.devices:
            self.app.devices.revoke(did)

    def test_paired_device_gets_the_smaller_role(self):
        dev, info = self.paired_device()
        st, _, b = self.req("GET", "/api/state", headers=dev)
        self.assertEqual(st, 200)
        s = json.loads(b)
        self.assertEqual((s["role"], s["device"]["name"], s["pairing"]), ("device", "iPhone", []))
        for method, path, body in [("GET", "/api/settings", None), ("GET", "/api/fs?path=/", None),
                                   ("GET", "/api/policy", None), ("PUT", "/api/policy", {}), ("GET", "/api/devices", None),
                                   ("POST", "/api/inject/scan", {"path": "/tmp"}), ("POST", "/api/secrets", {}),
                                   ("POST", "/api/fetch-build", {}), ("POST", "/api/agents/0123abcd/delete", {}),
                                   ("POST", "/api/agents/0123abcd/export", {}), ("POST", "/api/images/build", {}),
                                   ("POST", "/api/host/check", {}),
                                   ("POST", "/api/agents", {"name": "x", "inject": {"manifest": "m"}}),
                                   ("POST", "/api/agents/0123abcd/allow", {"host": "a.example", "scope": "global"}),
                                   # projects shape what every agent in them gets: settings, so from the PC
                                   ("POST", "/api/projects", {"name": "x"}), ("POST", "/api/projects/0123abcd", {}),
                                   ("POST", "/api/projects/0123abcd/files", {}), ("POST", "/api/projects/0123abcd/delete", {}),
                                   ("POST", "/api/projects/0123abcd/notes/0123abcd/approve", {})]:
            st, _, b = self.req(method, path, body, headers=dev)
            self.assertEqual(st, 403, (method, path))
            self.assertIn("paired device", json.loads(b)["error"])  # DEVICE_DENIED
        # allowed calls get through to the real handler (here: no such agent)
        st, _, b = self.req("POST", "/api/agents/0123abcd/allow", {"host": "a.example"}, headers=dev)
        self.assertEqual(st, 400)
        # the owner sees it and can revoke it; it stops working at once
        st, _, b = self.req("GET", "/api/devices", headers=self.auth())
        self.assertIn(info["id"], [d["id"] for d in json.loads(b)["devices"]])
        self.req("POST", f"/api/devices/{info['id']}/revoke", {}, headers=self.auth())
        st, _, _ = self.req("GET", "/api/state", headers=dev)
        self.assertEqual(st, 401)

    def test_a_device_follows_a_projects_yolo_like_the_pc(self):
        """Intended: a paired device may turn YOLO on, so an agent it puts in a project takes the project's YOLO."""
        mgr = self.app.manager
        ra = mgr.create(name="r", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/", profile="research")
        p = self.app.projects.create("Yolo Project")
        self.app.projects.update(p["id"], research_agent=ra.id, research_auto=True)
        dev, _ = self.paired_device("Phone")
        made = []
        try:
            with mock.patch.object(mgr, "start"):  # no VM here
                for who, headers, yolo in (("device", dev, True), ("owner", self.auth(), True)):
                    st, _, b = self.req("POST", "/api/agents", {"name": f"new-{who}", "project": p["id"]}, headers=headers)
                    self.assertEqual(st, 200, b)
                    r = json.loads(b)
                    made.append(r["id"])
                    self.assertEqual((r["research_agent"], r["research_auto"]), (ra.id, yolo), who)
                    self.assertNotIn("note", r, who)
                    ag = mgr.create(name=f"moved-{who}", mode="persistent", repo=None, agent="claude", cpus=1,
                                    memory_mb=1024, auto_stop=False, branch_prefix="agent/")
                    made.append(ag.id)
                    st, _, b = self.req("POST", f"/api/agents/{ag.id}/organize", {"project": p["id"]}, headers=headers)
                    r = json.loads(b)
                    self.assertEqual((st, r["research_agent"], r["research_auto"]), (200, ra.id, yolo), who)
                    self.assertNotIn("note", r, who)
            for auto in (False, True):  # and straight from its research settings
                st, _, b = self.req("POST", f"/api/agents/{made[0]}/link", {"research_agent": ra.id, "auto": auto}, headers=dev)
                self.assertEqual((st, json.loads(b)["research_auto"]), (200, auto), b)
        finally:
            for aid in made + [ra.id]:
                mgr.delete(aid)
            self.app.projects.delete(p["id"])

    def test_a_project_change_from_the_pc_reaches_agents_a_phone_moved_in(self):
        """Intended: changing the project's research agent or YOLO on the PC applies to every member."""
        mgr = self.app.manager
        ra = mgr.create(name="r2", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/", profile="research")
        p = self.app.projects.create("Later Yolo")
        self.app.projects.update(p["id"], research_agent=ra.id, research_auto=False)
        dev, _ = self.paired_device("Phone 2")
        ag = mgr.create(name="moved", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/")
        try:
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/organize", {"project": p["id"]}, headers=dev)
            self.assertEqual((st, json.loads(b)["research_auto"]), (200, False))  # the project's setting
            st, _, b = self.req("POST", f"/api/projects/{p['id']}", {"research_auto": True}, headers=self.auth())
            self.assertEqual(st, 200, b)
            self.assertTrue(ag.meta["research_auto"])  # the PC can, for every member
            st, _, b = self.req("POST", f"/api/projects/{p['id']}", {"research_auto": False}, headers=self.auth())
            self.assertFalse(ag.meta["research_auto"])
        finally:
            for aid in (ag.id, ra.id):
                mgr.delete(aid)
            self.app.projects.delete(p["id"])

    def test_instructions_for_one_agent(self):
        mgr = self.app.manager
        p = self.app.projects.create("Instr Project")
        self.app.projects.update(p["id"], instructions="PROJECT PART")
        ag = mgr.create(name="i", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/")
        ra = mgr.create(name="ri", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/", profile="research")
        dev, _ = self.paired_device("Instructor")
        try:
            ag.set(project=p["id"])
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/claude-md", {"text": "AGENT PART\x1b[31m\u202e"}, headers=dev)
            self.assertEqual(st, 403)  # owner only
            self.assertNotIn("claude_md", ag.meta)
            sent = []
            ag.ready.set()
            with mock.patch.object(type(ag), "running", True), mock.patch.object(ag, "send", sent.append):
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/claude-md", {"text": "AGENT PART\x1b[31m\u202e"}, headers=self.auth())
            self.assertEqual((st, json.loads(b)["claude_md"]), (200, "AGENT PART"))
            md = sent[0]["policy"]["claude_md"]  # pushed live, so the next task reads it
            self.assertLess(md.index(self.app.cfg()["claude_policy"]["claude_md"][:40]), md.index("PROJECT PART"))
            self.assertLess(md.index("PROJECT PART"), md.index("AGENT PART"))
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/claude-md", {"text": "x" * 20001}, headers=self.auth())
            self.assertEqual(st, 400)
            st, _, _ = self.req("POST", f"/api/agents/{ra.id}/claude-md", {"text": "RESEARCH PART"}, headers=self.auth())
            self.assertEqual(st, 200)
            self.assertIn("RESEARCH PART", self.app.policy_for(ra)["claude_md"])
            self.req("POST", f"/api/agents/{ag.id}/claude-md", {"text": ""}, headers=self.auth())
            self.assertNotIn("Instructions for this agent", self.app.policy_for(ag)["claude_md"])
        finally:
            for aid in (ag.id, ra.id):
                mgr.delete(aid)
            self.app.projects.delete(p["id"])

    def test_sites_can_carry_a_port(self):
        from cilly.server import parse_host_list
        hosts, skipped = parse_host_list("sync.example.net:8080\nhttps://Repo.Example.org:8443/maven\n"
                                         "- *.games.example:7777, plain.org\nbad.org:0 bad2.org:99999 x.org:abc\n*:8080\n"
                                         "sup.org:\u00b2 fw.org:\uff18\uff10 empty.org:")
        self.assertEqual(hosts, ["sync.example.net:8080", "repo.example.org:8443", "*.games.example:7777", "plain.org"])
        self.assertEqual([w for w, _ in skipped], ["bad.org:0", "bad2.org:99999", "x.org:abc", "*:8080", "sup.org:\u00b2",
                                                   "fw.org:\uff18\uff10", "empty.org:"])
        self.assertIn("1 to 65535", dict(skipped)["bad2.org:99999"])
        self.assertEqual(dict(skipped)["empty.org:"], "no port after the colon")  # refused, as in Settings and /allow
        ag = self.app.manager.create(name="ports", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        dev, _ = self.paired_device("Porter")
        pid = None
        try:
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/egress", {"text": "sync.example.net:8080 x.org:70000"},
                                headers=self.auth())
            self.assertEqual((st, json.loads(b)["agent"]["egress_extra"]), (200, ["sync.example.net:8080"]))
            # a phone may allow a site for one agent on the ports every site gets, and no other port
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/allow", {"host": "mc.example:443"}, headers=dev)
            self.assertEqual(st, 200)
            for path, body in (("allow", {"host": "mc.example:7777"}), ("allow", {"host": "mc.example:\u00b2"}),
                               ("egress", {"text": "ok.example\nmc.example:7777"})):
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/{path}", body, headers=dev)
                self.assertEqual((st, json.loads(b)["error"]), (403, "Other ports can only be allowed from the panel on your PC."), body)
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/allow", {"host": "mc.example:25566", "scope": "global"}, headers=dev)
            self.assertEqual(st, 403)
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/allow", {"host": "mc.example:7777"}, headers=self.auth())
            self.assertEqual(st, 200)
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/allow", {"host": "mc.example:\u00b2"}, headers=self.auth())
            self.assertEqual(st, 400)  # not an internal error
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/allow", {"host": "mc.example:0"}, headers=self.auth())
            self.assertEqual(st, 400)
            with open(ag.paths.allow_file) as f:
                self.assertTrue({"sync.example.net:8080", "mc.example:7777"} <= set(f.read().split()))
            st, _, b = self.req("POST", "/api/projects", {"name": "Ports"}, headers=self.auth())
            pid = json.loads(b)["id"]
            st, _, b = self.req("POST", f"/api/projects/{pid}", {"allow_text": "maven.example:8443\n*.cdn.example:8080"},
                                headers=self.auth())
            self.assertEqual(json.loads(b)["allow"], ["maven.example:8443", "*.cdn.example:8080"])
            cfg = json.loads(json.dumps(self.app.cfg()))
            cfg["egress"]["allow"].append("bad.example:65536")
            with self.assertRaises(ValueError):
                self.app.set_cfg(cfg)
        finally:
            self.app.manager.delete(ag.id)
            if pid:
                self.app.projects.delete(pid)

    def test_a_tasks_whole_message(self):
        ag = self.app.manager.create(name="m", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            text = "- step\n" * 1000
            tid = self.app.manager.new_task(ag.id, {"instructions": text, "summary": text}, queue=True)["id"]
            dev, _ = self.paired_device("Reader")
            for headers in (self.auth(), dev):  # read-only: a paired phone may read it too
                st, _, b = self.req("GET", f"/api/agents/{ag.id}/tasks/{tid}/message", headers=headers)
                self.assertEqual((st, json.loads(b)["text"]), (200, text))
            st, _, _ = self.req("GET", f"/api/agents/{ag.id}/tasks/0badc0de/message", headers=self.auth())
            self.assertEqual(st, 404)
        finally:
            self.app.manager.delete(ag.id)

    def test_a_phone_may_stop_a_task(self):
        ag = self.app.manager.create(name="i", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            dev, _ = self.paired_device("Stopper")
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/interrupt", {}, headers=dev)
            self.assertEqual((st, json.loads(b)["error"]), (409, "Nothing is running right now."))  # past the device check
            tid = self.app.manager.new_task(ag.id, {"instructions": "later"}, queue=True)["id"]
            with mock.patch.object(self.app.manager, "interrupt", return_value="stopping"):
                ag.meta["state"] = "busy"
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{tid}/send-now", {}, headers=dev)
                self.assertEqual((st, json.loads(b)["result"]), (200, "stopping"))
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/0badc0de/send-now", {}, headers=dev)
                self.assertEqual(st, 404)
            # the page didn't ask before Send now, so the server must not stop the task for it either
            for headers in (self.auth(), dev):
                tid = self.app.manager.new_task(ag.id, {"instructions": "not asked"}, queue=True)["id"]
                with mock.patch.object(self.app.manager, "interrupt", side_effect=AssertionError("stopped the task")):
                    st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{tid}/send-now", {"deliver_only": True}, headers=headers)
                self.assertEqual((st, json.loads(b)["result"]), (200, "not_delivered"))
                self.assertEqual(ag.queue[0]["id"], tid)  # first in line instead
            # Send now into the running task, when it takes messages: the same call, no new rights
            tid = self.app.manager.new_task(ag.id, {"instructions": "also this", "continue": True}, queue=True)["id"]
            ag.can_deliver = True
            ag.meta["tasks"].insert(0, {"id": "aaaa0001", "state": "running", "instructions": "go", "live": True})

            def send(msg, timeout=None):
                self.app.manager._on_guest(ag, {"type": "deliver_ack", "task": "aaaa0001", "id": msg["id"], "result": "delivered"})
            with mock.patch.object(ag, "send", send):
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{tid}/send-now", {}, headers=dev)
            self.assertEqual((st, json.loads(b)["result"]), (200, "delivered"))
            from cilly.server import device_may
            self.assertFalse(device_may("POST", f"/api/agents/{ag.id}/deliver"))
            self.assertFalse(device_may("POST", f"/api/agents/{ag.id}/tasks/{tid}/deliver"))
        finally:
            ag.meta["state"] = "stopped"
            self.app.manager.delete(ag.id)

    def test_move_a_queued_message_up_from_the_pc_only(self):
        ag = self.app.manager.create(name="u", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            first, second = (self.app.manager.new_task(ag.id, {"instructions": t}, queue=True)["id"] for t in ("a", "b"))
            dev, _ = self.paired_device("Mover")
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{second}/move-up", {}, headers=dev)
            self.assertEqual(st, 403)
            self.assertEqual(ag.public()["queue"], [first, second])
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{second}/move-up", {}, headers=self.auth())
            self.assertEqual((st, json.loads(b)["place"], json.loads(b)["agent"]["queue"]), (200, 0, [second, first]))
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/tasks/0badc0de/move-up", {}, headers=self.auth())
            self.assertEqual(st, 404)
        finally:
            self.app.manager.delete(ag.id)

    def test_cancel_a_queued_message_from_a_phone(self):
        ag = self.app.manager.create(name="q", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            dev, _ = self.paired_device("Canceller")
            tid = self.app.manager.new_task(ag.id, {"instructions": "later"}, queue=True)["id"]
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{tid}/cancel", {}, headers=dev)
            self.assertEqual((st, json.loads(b)["task"]["state"]), (200, "cancelled"))
            tid = self.app.manager.new_task(ag.id, {"instructions": "now"}, queue=True)["id"]
            ag.queue.clear()  # it started
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/tasks/{tid}/cancel", {}, headers=self.auth())
            self.assertEqual((st, json.loads(b)["error"]), (409, "It already started."))
        finally:
            self.app.manager.delete(ag.id)

    def test_tor_starts_as_soon_as_a_route_may_use_it(self):
        """Not only when an agent's VM starts: its self-test waits only 10 s, too little for a
        cold Tor to build its first circuits."""
        a = self.auth()
        started = []
        p_ = mock.patch.object(self.app.tor, "ensure", lambda: started.append(1))  # no real Tor in tests
        p_.start()
        self.addCleanup(p_.stop)
        ag = self.app.manager.create(name="tor-later", mode="ephemeral", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=True, branch_prefix="agent/")
        self.addCleanup(lambda: ag.set(route=None))  # other tests mustn't start a real Tor
        before = len(started)
        self.req("POST", f"/api/agents/{ag.id}/route", {"route": "tor"}, headers=a)
        self.assertFalse(ag.running)
        self.assertGreater(len(started), before)  # warm before the agent starts

    def test_testing_a_route(self):
        """Settings, Network: Test connects through a saved route, to GitHub's API or a site on the allowlist only."""
        import copy
        from cilly import routed
        from cilly.egress_proxy import RouteDown
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["egress"]["routes"] = {"vpn": {"kind": "socks5", "host": "10.64.0.1", "port": 1080}}
        cfg["egress"]["allow"] = cfg["egress"]["allow"] + ["api.domain.com"]
        self.app.set_cfg(cfg)
        url, a = "/api/routes/test", self.auth()
        tried = []

        def works(route, host, port=443, timeout=30):
            tried.append((route["name"], host, port))
        with mock.patch.object(routed, "check", works):
            st, _, b = self.req("POST", url, {"name": "vpn"}, headers=a)
            r = json.loads(b)
            self.assertEqual((st, r["ok"], r["host"], type(r["ms"])), (200, True, "api.github.com", int))
            st, _, b = self.req("POST", url, {"name": "vpn", "host": "API.domain.com."}, headers=a)
            self.assertEqual((json.loads(b)["host"], tried[-1]), ("api.domain.com", ("vpn", "api.domain.com", 443)))
            for body, msg in (({"name": "vpn", "host": "example.net"}, "isn't on the allowlist"),
                              ({"name": "vpn", "host": "10.0.0.1:22"}, "the name of a site"),
                              ({"name": "nope"}, "Save settings with this route first")):
                st, _, b = self.req("POST", url, body, headers=a)
                self.assertEqual(st, 400, body)
                self.assertIn(msg, json.loads(b)["error"])
            self.assertEqual(len(tried), 2)  # nothing else was reached
        with mock.patch.object(routed, "check", side_effect=RouteDown("the route closed the connection")):
            r = json.loads(self.req("POST", url, {"name": "vpn"}, headers=a)[2])
            self.assertEqual((r["ok"], r["error"]), (False, "the route closed the connection"))
        dev, _ = self.paired_device("Route phone")
        self.assertEqual(self.req("POST", url, {"name": "vpn"}, headers=dev)[0], 403)  # from the PC only

    def test_a_route_that_hangs_up_is_named_in_the_message(self):
        ag = self.app.manager.create(name="vpn-agent", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        ag.set(route="vpn1")
        log = [{"host": "api.domain.com", "port": 443, "ts": "2026-10-20T06:00:00Z", "code": "route_down",
                "reason": "the route closed the connection"},
               {"host": "evil.example", "port": 443, "ts": "2026-10-20T06:00:01Z", "code": "allowlist", "reason": "not on the allowlist"}]
        with mock.patch.object(self.app, "_deny_log", return_value=log):
            got = {d["host"]: d for d in self.app._denials(ag)}
        self.assertEqual(got["api.domain.com"]["reason"],
                         "Route vpn1: the route closed the connection. Test it in Settings, under Network.")
        self.assertFalse(got["api.domain.com"]["fixable"])  # allowing it wouldn't help
        self.assertEqual(got["evil.example"]["reason"], "not on the allowlist")

    def test_routing_a_real_connection_that_hangs_up(self):
        """routed.check through a SOCKS5 proxy that closes the connection: the route failed, not the site."""
        import socket as so
        from cilly import routed
        from cilly.egress_proxy import RouteDown
        srv = so.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def hang_up():
            c, _ = srv.accept()
            c.recv(16)  # the hello, then nothing
            c.close()
        threading.Thread(target=hang_up, daemon=True).start()
        route = {"kind": "socks5", "name": "vpn", "host": "127.0.0.1", "port": srv.getsockname()[1]}
        with mock.patch("cilly.egress_proxy.loopback_owner", return_value=os.getuid()), self.assertRaises(RouteDown) as cm:
            routed.check(route, "api.github.com", timeout=5)
        self.assertEqual(str(cm.exception), "the route closed the connection")

    def test_routes_pick_and_kill_switch(self):
        import copy
        a = self.auth()
        started = []
        p_ = mock.patch.object(self.app.tor, "ensure", lambda: started.append(1))  # no real Tor in tests
        p_.start()
        self.addCleanup(p_.stop)
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        ag = self.app.manager.create(name="routed", mode="ephemeral", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=True, branch_prefix="agent/")
        self.assertEqual(self.app._route(ag), {"kind": "direct"})  # off by default
        cfg = copy.deepcopy(old)
        cfg["egress"]["routes"] = {"vpn": {"kind": "socks5", "host": "10.64.0.1", "port": 1080, "user": "me"},
                                   "office": {"kind": "http", "host": "proxy.example.org", "port": 3128}}
        cfg["egress"]["route"] = "office"
        self.app.set_cfg(cfg)
        self.assertEqual(self.app._route(ag), {"kind": "http", "name": "office", "host": "proxy.example.org", "port": 3128})
        p = self.app.projects.create("Routed project")
        self.req("POST", f"/api/projects/{p['id']}", {"route": "tor"}, headers=a)
        self.addCleanup(lambda: self.app.projects.update(p["id"], route=None))  # other tests mustn't start a real Tor
        self.req("POST", f"/api/agents/{ag.id}/organize", {"project": p["id"]}, headers=a)
        self.assertEqual(self.app._route(ag)["kind"], "tor")  # the project's pick wins over Settings
        self.assertTrue(started)  # Tor starts the first time a route needs it
        self.assertTrue(self.app._route(ag)["unix"].endswith("/tor/socks.sock"))
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/route", {"route": "vpn"}, headers=a)
        self.assertEqual((st, json.loads(b)["route"]), (200, "vpn"))  # and the agent's own wins over both
        r = self.app._route(ag)
        self.assertEqual((r["user"], r["password"]), ("me", ""))  # no password yet: the proxy refuses it
        from cilly.egress_proxy import parse_route
        with self.assertRaises(ValueError):
            parse_route(r)
        self.req("POST", "/api/secrets", {"key": "route:vpn", "value": "hunter2"}, headers=a)
        self.assertEqual(parse_route(self.app._route(ag))["password"], "hunter2")
        state = json.loads(self.req("GET", "/api/state", headers=a)[2])
        self.assertEqual({k: v for k, v in state["routes"].items() if k not in ("tor", "wireguard", "ssh")},
                         {"standard": "office", "names": ["office", "vpn"], "kinds": {"office": "http", "vpn": "socks5"}})
        self.assertEqual(set(state["routes"]["tor"]), {"installed", "running", "progress", "error"})
        self.assertNotIn("hunter2", json.dumps(state))
        st, _, _ = self.req("POST", f"/api/agents/{ag.id}/route", {"route": "nope"}, headers=a)
        self.assertEqual(st, 400)
        del cfg["egress"]["routes"]["vpn"]
        cfg["egress"]["route"] = ""
        self.app.set_cfg(cfg)
        self.assertEqual(self.app._route(ag), {"kind": "missing", "name": "vpn"})  # deleted: refused, never straight out
        self.req("POST", f"/api/agents/{ag.id}/route", {"route": "direct"}, headers=a)
        self.assertEqual(self.app._route(ag), {"kind": "direct"})  # straight out, picked on purpose
        # a paired phone picks one of the routes, and sets up none
        dev, info = self.paired_device("Route phone")
        self.addCleanup(self.app.devices.revoke, info["id"])  # the tests share one panel, and it pairs only so many
        st, _, _ = self.req("POST", f"/api/agents/{ag.id}/route", {"route": "tor"}, headers=dev)
        self.assertEqual((st, self.app._route(ag)["kind"]), (200, "tor"))
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=dev)[0], 403)
        self.req("POST", f"/api/agents/{ag.id}/delete", {}, headers=a)

    def test_a_wireguard_vpn_from_its_config(self):
        import base64
        import copy
        from cilly import config as C
        from cilly import wireguard as W
        from cilly.egress_proxy import parse_route
        a = self.auth()
        priv, pub, psk = (base64.b64encode(bytes([n]) * 32).decode() for n in (7, 8, 9))
        conf = (f"[Interface]\nPrivateKey = {priv}\nAddress = 10.8.0.2/24\nDNS = 10.8.0.1\nPostUp = touch /tmp/pwned\n\n"
                f"[Peer]\nPublicKey = {pub}\nPresharedKey = {psk}\nEndpoint = vpn.example.org:51820\nAllowedIPs = 0.0.0.0/0\n")
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: (self.app.set_cfg(old), self.app.wg.stop()))
        p_ = mock.patch.object(W, "wireproxy_binary", lambda: None)  # no real wireproxy in tests
        p_.start()
        self.addCleanup(p_.stop)
        st, _, b = self.req("POST", "/api/routes/wireguard", {"name": "office", "conf": conf}, headers=a)
        self.assertEqual(st, 200, b)
        res = json.loads(b)
        self.assertEqual(res["left_out"], ["postup"])
        saved = self.app.cfg()["egress"]["routes"]["office"]
        self.assertEqual((saved["kind"], saved["endpoint"], saved["dns"]), ("wireguard", "vpn.example.org:51820", ["10.8.0.1"]))
        with open(C.config_path()) as f:
            on_disk = f.read()
        for key in (priv, psk):
            self.assertNotIn(key, on_disk)  # the keys are in Secrets, never in config.json
            self.assertNotIn(key, b.decode())
        self.assertEqual(self.app.secrets.get("route:office", {}), f"{priv} {psk}")
        # an agent on it gets wireproxy's SOCKS5 server and the VPN's resolver, never the keys
        ag = self.app.manager.create(name="vpn-agent", mode="ephemeral", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=True, branch_prefix="agent/")
        self.addCleanup(lambda: self.req("POST", f"/api/agents/{ag.id}/delete", {}, headers=a))
        self.req("POST", f"/api/agents/{ag.id}/route", {"route": "office"}, headers=a)
        r = parse_route(self.app._route(ag))
        self.assertEqual((r["kind"], r["host"], r["dns"]), ("socks5", "127.0.0.1", "10.8.0.1"))
        self.assertNotIn(priv, json.dumps(r))
        self.assertEqual(json.loads(self.req("GET", "/api/state", headers=a)[2])["routes"]["wireguard"]["routes"]["office"]["error"],
                         "wireproxy isn't installed")
        # private hosts it may reach, set in Settings
        cfg = copy.deepcopy(self.app.cfg())
        cfg["egress"]["routes"]["office"]["private"] = ["git.office.lan", "10.8.0.20"]
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=a)[0], 200)
        self.assertEqual(parse_route(self.app._route(ag))["private"], {"names": ["git.office.lan"], "ips": ["10.8.0.20"]})
        cfg["egress"]["routes"]["office"]["private"] = ["8.8.8.8"]  # not a private address
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=a)[0], 400)
        # a new config keeps the private hosts; a route that isn't WireGuard can't be overwritten
        st, _, b = self.req("POST", "/api/routes/wireguard", {"name": "office", "conf": conf.replace("vpn.example.org", "vpn2.example.org")}, headers=a)
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.cfg()["egress"]["routes"]["office"]["private"], ["git.office.lan", "10.8.0.20"])
        for body in ({"name": "office", "conf": "not a config"}, {"name": "Tor", "conf": conf}, {"name": "tor", "conf": conf}):
            self.assertEqual(self.req("POST", "/api/routes/wireguard", body, headers=a)[0], 400, body)
        # without its keys, it's refused rather than going straight out
        self.app.secrets.forget("route:office")
        self.assertEqual(self.app._route(ag), {"kind": "missing", "name": "office"})
        # a paired phone can't add one
        dev, info = self.paired_device("VPN phone")
        self.addCleanup(self.app.devices.revoke, info["id"])
        self.assertEqual(self.req("POST", "/api/routes/wireguard", {"name": "phone", "conf": conf}, headers=dev)[0], 403)
        # removed in Settings: its wireproxy goes
        self.assertIn("office", self.app.wg.procs)
        cfg = copy.deepcopy(self.app.cfg())
        del cfg["egress"]["routes"]["office"]
        self.req("POST", f"/api/agents/{ag.id}/route", {"route": "direct"}, headers=a)
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=a)[0], 200)
        self.assertNotIn("office", self.app.wg.procs)

    def test_stand_ins_for_tokens(self):
        """With Keep tokens on this PC, a VM's task gets stand-ins, and only the agent's proxy the real tokens."""
        import copy
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        ag = self.app.manager.create(name="tokens", mode="ephemeral", repo="owner/repo", agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=True, branch_prefix="agent/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        self.assertEqual(self.app._token_hosts(ag), ["api.anthropic.com", "api.github.com", "claude.ai", "github.com",
                                                     "platform.claude.com"])
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-REAL1", "GH_TOKEN": "ghp_REAL2", "GITHUB_TOKEN": "ghp_REAL2",
               "SOME_MCP_KEY": "mcp-REAL3"}
        out = self.app._stand_ins(ag, env)
        self.assertTrue(out["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-cilly"))
        self.assertEqual(out["GH_TOKEN"], out["GITHUB_TOKEN"])  # one token, one stand-in
        self.assertTrue(out["GH_TOKEN"].startswith("ghp_cilly"))
        self.assertEqual(out["SOME_MCP_KEY"], "mcp-REAL3")  # no MCP server of this agent sends it in a login header: unchanged
        self.assertNotIn("REAL1", json.dumps(out))
        self.assertNotIn("REAL2", json.dumps(out))
        gh = [t for t in ag.tokens if t["value"] == "ghp_REAL2"][0]
        self.assertEqual((gh["hosts"], gh["github"]), (["api.github.com", "github.com"],
                                                       {"repo": "owner/repo", "push": {"agent": ag.id, "create": {"kinds": list(C.BRANCH_KINDS)}}}))
        cfg = copy.deepcopy(old)
        cfg["egress"]["push_own_branches"] = False
        self.app.set_cfg(cfg)
        self.app._stand_ins(ag, env)
        self.assertIsNone([t for t in ag.tokens if t["value"] == "ghp_REAL2"][0]["github"]["push"])  # pushes anywhere
        cfg["egress"]["keep_tokens"] = False
        self.app.set_cfg(cfg)
        self.assertEqual(self.app._stand_ins(ag, env), env)  # the setting off: as before
        ag.procs["qemu"] = mock.Mock(poll=lambda: None)  # but one running with this start's certificates
        self.addCleanup(ag.procs.pop, "qemu", None)
        ag.tls = {"ca": "x", "hosts": {h: {} for h in ("api.anthropic.com", "api.github.com", "claude.ai", "github.com",
                                                       "platform.claude.com")}}
        with mock.patch.object(self.app.manager, "write_tokens", lambda a: None):
            out = self.app._stand_ins(ag, env)
        self.assertNotIn("REAL2", json.dumps(out))  # keeps stand-ins until it starts again
        ag.tls = None
        self.assertEqual(self.app._stand_ins(ag, env), env)  # started without them: real ones until then
        self.assertEqual(self.app._token_hosts(ag), [])

    def test_mcp_keys_in_a_login_header_stay_on_this_pc(self):
        """A remote MCP server's key in its Authorization or x-api-key header: the VM gets a stand-in, and the
        agent's proxy puts the real key in for that server's host.  Keys the VM itself needs stay as they were."""
        import copy
        from cilly.server import mcp_fingerprint
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["mcp_servers"] = {
            "docs": {"type": "http", "url": "https://mcp.docs.example/mcp", "headers": {"Authorization": "Bearer ${DOCS_KEY}"}},
            "local": {"type": "stdio", "command": "npx", "args": ["some-mcp"], "env": {"LOCAL_KEY": "${LOCAL_KEY}"}},
            "tiny": {"type": "http", "url": "https://tiny.example/mcp", "headers": {"x-api-key": "${TINY_KEY}"}}}
        self.app.set_cfg(cfg)
        for n, srv in cfg["mcp_servers"].items():
            self.app._mcp_seen[n] = mcp_fingerprint(srv)
        ag = self.app.manager.create(name="mcpkeys", mode="ephemeral", repo="owner/repo", agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=True, branch_prefix="agent/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        ag.meta["mcp"] = ["docs", "local", "tiny"]
        self.assertIn("mcp.docs.example", self.app._token_hosts(ag))  # this start's certificates cover it
        self.assertIn("tiny.example", self.app._token_hosts(ag))
        env = {"DOCS_KEY": "docs-REAL-key-1", "LOCAL_KEY": "local-REAL-key-2", "TINY_KEY": "abc"}
        out = self.app._stand_ins(ag, env)
        self.assertTrue(out["DOCS_KEY"].startswith("cilly"))
        self.assertNotIn("REAL-key-1", json.dumps(out))
        self.assertEqual(out["LOCAL_KEY"], "local-REAL-key-2")  # the stdio server runs in the VM and needs it
        self.assertEqual(out["TINY_KEY"], "abc")  # too short for a stand-in: as before
        docs = [t for t in ag.tokens if t["value"] == "docs-REAL-key-1"][0]
        self.assertEqual((docs["stand_in"], docs["hosts"], docs.get("github")), (out["DOCS_KEY"], ["mcp.docs.example"], None))
        # running with certificates from before "docs" was ticked: the real key, as before, and no error
        ag.procs["qemu"] = mock.Mock(poll=lambda: None)
        self.addCleanup(ag.procs.pop, "qemu", None)
        ag.tls = {"ca": "x", "hosts": {h: {} for h in ("api.anthropic.com", "api.github.com", "claude.ai", "github.com",
                                                       "platform.claude.com")}}
        ag.stand_ins.clear()
        ag.tokens = []
        with mock.patch.object(self.app.manager, "write_tokens", lambda a: None):
            out = self.app._stand_ins(ag, env)
        self.assertEqual(out["DOCS_KEY"], "docs-REAL-key-1")
        ag.tls["hosts"]["mcp.docs.example"] = {}  # started with it ticked: the stand-in
        with mock.patch.object(self.app.manager, "write_tokens", lambda a: None):
            out = self.app._stand_ins(ag, env)
        self.assertTrue(out["DOCS_KEY"].startswith("cilly"))

    def test_attaching_a_repository_hands_over_only_stand_ins(self):
        """The attach task pushes from inside the VM, so it gets a stand-in like every other task."""
        import copy
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["tokens"]["at"] = {"description": "", "repos": ["me/attached"], "secret": {}}
        self.app.set_cfg(cfg)
        self.app.secrets.set_pasted("token:at", "ghp_REALATTACH")
        ag = self.app.manager.create(name="scratch", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        ag.procs["qemu"] = mock.Mock(poll=lambda: None)  # running
        self.addCleanup(ag.procs.pop, "qemu")
        ag.meta["state"] = "ready"
        sent = []
        with mock.patch.object(self.app.manager, "new_task", lambda aid, payload, **kw: sent.append(payload) or {"id": "t1"}), \
                mock.patch.object(self.app.manager, "write_tokens", lambda a: None):
            ag.tls = {"ca": "x", "hosts": {"api.anthropic.com": {}}}  # started without GitHub's certificates
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/attach-repo", {"repo": "me/attached"}, headers=self.auth())
            self.assertEqual(st, 400, b)
            self.assertIn("Stop it and start it again", json.loads(b)["error"])
            self.assertEqual((sent, ag.meta.get("repo")), ([], None))  # nothing went, and nothing changed
            ag.tls = {"ca": "x", "hosts": {h: {} for h in ("github.com", "api.github.com", "api.anthropic.com")}}
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/attach-repo", {"repo": "me/attached"}, headers=self.auth())
            self.assertEqual(st, 200, b)
        self.assertNotIn("ghp_REALATTACH", json.dumps(sent))
        self.assertTrue(sent[0]["env"]["GH_TOKEN"].startswith("ghp_cilly"))
        gh = [t for t in ag.tokens if t["value"] == "ghp_REALATTACH"][0]
        self.assertEqual(gh["github"], {"repo": "me/attached", "push": {"agent": ag.id, "create": {"kinds": list(C.BRANCH_KINDS)}}})

    def test_themes_and_fonts(self):
        import base64
        a = self.auth()
        st, _, b = self.req("POST", "/api/themes", {"name": "Mine", "colors": {"--accent": "#AA3311"}, "font": "geist-pixel-square"},
                            headers=a)
        self.assertEqual(st, 200, b)
        tid = json.loads(b)["id"]
        self.addCleanup(lambda: self.req("POST", f"/api/themes/{tid}/delete", {}, headers=a))
        for bad in ({"name": "x", "colors": {"--bg": "url(x)"}}, {"name": "x", "colors": {"--nope": "#000000"}}, {"name": ""}):
            self.assertEqual(self.req("POST", "/api/themes", bad, headers=a)[0], 400, bad)
        self.assertEqual(self.req("POST", "/api/themes/classic/delete", {}, headers=a)[0], 404)  # not a theme you saved
        st, _, b = self.req("PUT", "/api/appearance", {"theme": tid}, headers=a)
        self.assertEqual((st, json.loads(b)["theme"]), (200, tid))
        self.assertEqual(self.req("PUT", "/api/appearance", {"theme": "deadbeef"}, headers=a)[0], 400)
        font = b"wOF2" + b"\0" * 60
        st, _, b = self.req("POST", "/api/fonts", {"name": "Mondwest", "data": base64.b64encode(font).decode()}, headers=a)
        self.assertEqual(st, 200, b)
        fid = json.loads(b)["id"]
        st, _, b = self.req("POST", "/api/fonts", {"name": "Page", "data": base64.b64encode(b"<html>").decode()}, headers=a)
        self.assertEqual(st, 400)
        self.req("POST", f"/api/themes/{tid}", {"name": "Mine", "colors": {"--accent": "#aa3311"}, "font": fid, "heading": fid}, headers=a)

        # a paired phone shows the standard theme and its fonts, and changes neither
        dev, info = self.paired_device("Theme phone")
        self.addCleanup(self.app.devices.revoke, info["id"])  # the tests share one panel, and it pairs only so many
        st, _, b = self.req("GET", "/api/state", headers=dev)
        look = json.loads(b)["appearance"]
        self.assertEqual(look["theme"], tid)
        self.assertEqual(look["themes"][tid], {"name": "Mine", "colors": {"--accent": "#aa3311"}, "style": "classic", "font": fid,
                                               "heading": fid, "prose": "", "mono": "geist-mono", "size": 100, "prose_size": 100,
                                               "mono_size": 100, "builtin": False})
        self.assertEqual(look["fonts"], [{"id": fid, "name": "Mondwest"}])
        st, _, b = self.req("GET", f"/api/fonts/{fid}", headers=dev)
        self.assertEqual((st, base64.b64decode(json.loads(b)["data"])), (200, font))
        for method, path, body in (("PUT", "/api/appearance", {"theme": "classic"}), ("POST", "/api/themes", {"name": "Phone"}),
                                   ("POST", f"/api/themes/{tid}", {"name": "Phone"}), ("POST", f"/api/themes/{tid}/delete", {}),
                                   ("POST", "/api/fonts", {"name": "x", "data": base64.b64encode(font).decode()}),
                                   ("POST", f"/api/fonts/{fid}/delete", {})):
            self.assertEqual(self.req(method, path, body, headers=dev)[0], 403, path)

        # removing a font puts the panel's own font back in the themes that used it
        st, _, b = self.req("POST", f"/api/fonts/{fid}/delete", {}, headers=a)
        self.assertEqual((st, json.loads(b)["themes"][tid]["font"], json.loads(b)["themes"][tid]["heading"], json.loads(b)["fonts"]),
                         (200, "geist", "", []))
        self.assertEqual(self.req("GET", f"/api/fonts/{fid}", headers=a)[0], 404)
        st, _, b = self.req("POST", f"/api/themes/{tid}/delete", {}, headers=a)
        self.assertEqual(json.loads(b)["theme"], "dusk")  # the standard theme was deleted: back to the default

    def test_device_can_sign_itself_out(self):
        dev, _ = self.paired_device("Laptop")
        st, _, _ = self.req("POST", "/api/devices/me/forget", {}, headers=dev)
        self.assertEqual(st, 200)
        st, _, _ = self.req("GET", "/api/state", headers=dev)
        self.assertEqual(st, 401)

    def test_pairing_needs_the_right_host(self):
        st, _, _ = self.req("POST", "/api/pair/start", {"name": "x"}, host="evil.example")
        self.assertEqual(st, 421)
        st, _, _ = self.req("POST", "/api/pair/start", {"name": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)

    def test_build_download_refuses_sensitive_folders(self):
        from cilly.server import ApiError
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["tokens"] = {"bot": {"repos": ["me/r"], "secret": {}}}
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, ".config"), exist_ok=True)
        with mock.patch.object(self.app, "cfg", return_value=cfg), mock.patch.dict(os.environ, {"HOME": home}):
            for dest in ("/", home, os.path.join(home, ".config"), "/etc"):
                with self.assertRaises(ApiError, msg=dest) as e:
                    self.app.api_fetch_build({"repo": "me/r", "dest": dest})
                self.assertIn("not a safe destination", str(e.exception))

    def test_static_has_csp(self):
        st, h, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_remote_host_only_over_https_proxy(self):
        from cilly import config as C
        remote = {"Host": "cilly.home", **self.auth()}
        st, _, _ = self.req("GET", "/api/state", headers=remote)
        self.assertEqual(st, 421)  # not configured: refused
        new = json.loads(json.dumps(self.app.cfg()))
        new["remote_hosts"] = ["cilly.home"]
        self.app.set_cfg(new)
        try:
            st, _, b = self.req("GET", "/api/state", headers=remote)
            self.assertEqual(st, 403)
            self.assertIn("HTTPS", json.loads(b)["error"])  # plain HTTP would put the token on the LAN
            via = {**remote, "X-Forwarded-Proto": "https"}
            st, _, b = self.req("GET", "/api/state", headers=via)
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(b)["remote_hosts"], ["cilly.home"])
            st, _, _ = self.req("POST", "/api/setup", {}, headers={**via, "Origin": "http://cilly.home"})
            self.assertEqual(st, 403)
            st, _, _ = self.req("POST", "/api/setup", {}, headers={**via, "Origin": "https://evil.home"})
            self.assertEqual(st, 403)
            st, _, _ = self.req("GET", "/api/state", headers={**self.auth(), "Host": "evil.home", "X-Forwarded-Proto": "https"})
            self.assertEqual(st, 421)
            # the web UI can't add names: config file only
            s = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
            s["remote_hosts"] = ["cilly.home", "attacker.example"]
            st, _, b = self.req("PUT", "/api/settings", {"config": s}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("remote_hosts", json.loads(b)["error"])
        finally:
            new["remote_hosts"] = []
            self.app.set_cfg(new)
        for bad in (["CILLY.home"], ["cilly.home/x"], ["*.home"], "cilly.home", [""]):
            c = json.loads(json.dumps(new))
            c["remote_hosts"] = bad
            with self.assertRaises(ValueError):
                C.validate(c)

    def test_icons(self):
        st, h, b = self.req("GET", "/favicon.ico")
        self.assertEqual((st, h["Content-Type"]), (200, "image/x-icon"))
        self.assertTrue(b.startswith(b"\x00\x00\x01\x00"))  # an ICO file
        for p in ("/icon-32.png", "/icon-180.png", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"):
            st, _, b = self.req("GET", p)
            self.assertEqual((st, b[:4]), (200, b"\x89PNG"), p)

    def test_config_file_edits_are_picked_up(self):
        from cilly import config as C
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["egress"]["allow"] = cfg["egress"]["allow"] + ["edited.example.com"]
        os.makedirs(os.path.dirname(C.config_path()), exist_ok=True)
        time.sleep(0.01)
        with open(C.config_path(), "w") as f:  # like `cilly secrets set` or your editor
            json.dump(cfg, f)
        self.assertIn("edited.example.com", self.app.cfg()["egress"]["allow"])
        with open(C.config_path(), "w") as f:
            f.write("{ not json")
        self.assertIn("edited.example.com", self.app.cfg()["egress"]["allow"])  # keeps the last good one
        cfg["egress"]["allow"].remove("edited.example.com")
        self.app.set_cfg(cfg)
        self.assertNotIn("edited.example.com", self.app.cfg()["egress"]["allow"])

    def test_pwa_files(self):
        st, h, b = self.req("GET", "/manifest.webmanifest")
        self.assertEqual((st, h["Content-Type"]), (200, "application/manifest+json"))
        self.assertEqual(json.loads(b)["start_url"], "/")
        st, h, b = self.req("GET", "/sw.js")
        self.assertEqual(st, 200)
        self.assertIn("worker-src 'self'", h["Content-Security-Policy"])
        self.assertNotIn(b"fetch", b.replace(b"no fetch handling", b""))  # it never sees the panel's requests

    def test_api_requires_token(self):
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Panel-Token": "wrong"})[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers=self.auth())[0], 200)

    def test_dns_rebinding_blocked(self):
        st, _, _ = self.req("GET", "/api/state", headers=self.auth(), host=f"evil.example:{self.port}")
        self.assertEqual(st, 421)
        st, _, _ = self.req("GET", "/", host="attacker.test")
        self.assertEqual(st, 421)

    def test_bad_origin_blocked(self):
        st, _, _ = self.req("POST", "/api/inject/scan", {"path": "/x"},
                            headers={**self.auth(), "Origin": "https://evil.example"})
        self.assertEqual(st, 403)

    def test_cors_preflight_refused(self):
        st, h, _ = self.req("OPTIONS", "/api/vms")
        self.assertNotIn("Access-Control-Allow-Origin", h)
        self.assertNotEqual(st, 200)

    def test_json_content_type_required(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/inject/scan", body=b'{"path":"/tmp"}',
                  headers={"Host": f"127.0.0.1:{self.port}", "Content-Type": "text/plain", **self.auth()})
        self.assertEqual(c.getresponse().status, 415)

    def test_scan_refuses_home(self):
        st, _, body = self.req("POST", "/api/inject/scan", {"path": os.path.expanduser("~")}, headers=self.auth())
        self.assertEqual(st, 400)
        self.assertIn("refusing", json.loads(body)["error"])

    def test_create_requires_configured_repo(self):
        st, _, body = self.req("POST", "/api/agents", {"name": "x", "repo": "someone/else", "mode": "ephemeral"},
                               headers=self.auth())
        self.assertEqual(st, 400)
        self.assertIn("not configured", json.loads(body)["error"])

    def test_resources_of_a_stopped_agent(self):
        from unittest import mock
        ag = self.app.manager.create(name="r", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.addCleanup(lambda: (ag.procs.pop("qemu", None), self.app.manager.delete(ag.id)))
        url = f"/api/agents/{ag.id}/resources"
        st, _, b = self.req("POST", url, {"cpus": 2, "memory_mb": 3072}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual((ag.meta["cpus"], ag.meta["memory_mb"]), (2, 3072))
        with open(ag.paths.meta) as f:  # saved, so the next start uses it
            self.assertEqual(json.load(f)["memory_mb"], 3072)
        vm = self.app.cfg()["vm"]
        for bad in ({"cpus": 0, "memory_mb": 1024}, {"cpus": vm["max_cpus"] + 1, "memory_mb": 1024},
                    {"cpus": 1, "memory_mb": 511}, {"cpus": 1, "memory_mb": vm["max_memory_mb"] + 1},
                    {"cpus": "2", "memory_mb": 1024}, {"cpus": 2.5, "memory_mb": 1024}, {"cpus": True, "memory_mb": 1024},
                    {"cpus": 2}, {}):
            st, _, bb = self.req("POST", url, bad, headers=self.auth())
            self.assertEqual(st, 400, bad)
            msg = json.loads(bb)["error"]
            self.assertEqual(msg[:1], msg[:1].upper(), msg)  # sentence case
        self.assertEqual((ag.meta["cpus"], ag.meta["memory_mb"]), (2, 3072))
        ag.procs["qemu"] = mock.Mock(poll=lambda: None)  # its VM is up
        for state in ("booting", "ready", "busy", "stopping"):
            ag.meta["state"] = state
            st, _, b = self.req("POST", url, {"cpus": 4, "memory_mb": 4096}, headers=self.auth())
            self.assertEqual(st, 409, state)
            self.assertIn("Stop the agent first", json.loads(b)["error"])
        ag.procs.pop("qemu")
        ag.meta["state"] = "stopped"
        self.assertEqual((ag.meta["cpus"], ag.meta["memory_mb"]), (2, 3072))
        with mock.patch.object(self.app.devices, "starts", []):  # a fresh pairing allowance for this test
            dev, _ = self.paired_device("Resources phone")
        st, _, b = self.req("POST", url, {"cpus": 4, "memory_mb": 4096}, headers=dev)
        self.assertEqual(st, 403)  # owner only
        self.assertEqual(ag.meta["cpus"], 2)

    def test_bash_and_task_limits_of_an_agent(self):
        ag = self.app.manager.create(name="l", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-x")
        self.addCleanup(lambda: (self.app.secrets.forget("agent:claude:CLAUDE_CODE_OAUTH_TOKEN"), self.app.manager.delete(ag.id)))
        url, glob = f"/api/agents/{ag.id}/limits", self.app.cfg()["vm"]["task_timeout_minutes"]

        def payload():
            return self.app._task_payload(ag, "hello", new_branch=False, cont=True, pr_title="")
        p = payload()  # the defaults: the global task limit, Claude Code's own Bash limit
        self.assertEqual(p["timeout_s"], glob * 60)
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", {**p["agent_env"], **p["env"]})
        self.assertNotIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", p["agent_env"])  # Claude Code's own 10 min
        st, _, b = self.req("GET", "/api/state", headers=self.auth())
        self.assertEqual(json.loads(b)["vm_defaults"]["task_timeout_minutes"], glob)

        st, _, b = self.req("POST", url, {"bash_limit_minutes": 45, "task_limit_minutes": 480}, headers=self.auth())
        self.assertEqual(st, 200, b)
        r = json.loads(b)
        self.assertEqual((r["bash_limit_minutes"], r["task_limit_minutes"]), (45, 480))
        with open(ag.paths.meta) as f:
            self.assertEqual(json.load(f)["task_limit_minutes"], 480)
        p = payload()
        self.assertEqual((p["timeout_s"], p["agent_env"]["BASH_MAX_TIMEOUT_MS"]), (480 * 60, "2700000"))
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", p["env"])  # env holds secrets, which the logs hide
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", self.app.cfg()["agents"]["claude"]["env"])  # a copy, not the config

        for bad in ({"bash_limit_minutes": 1}, {"bash_limit_minutes": 241}, {"task_limit_minutes": 4},
                    {"task_limit_minutes": 1441}, {"bash_limit_minutes": "30"}, {"bash_limit_minutes": 30.5},
                    {"task_limit_minutes": True}, {"bash_limit_minutes": "1; rm -rf /"}, {}):
            st, _, bb = self.req("POST", url, bad, headers=self.auth())
            self.assertEqual(st, 400, bad)
            msg = json.loads(bb)["error"]
            self.assertEqual(msg[:1], msg[:1].upper(), msg)  # sentence case
        self.assertEqual((ag.meta["bash_limit_minutes"], ag.meta["task_limit_minutes"]), (45, 480))

        with mock.patch.object(self.app.devices, "starts", []):
            dev, _ = self.paired_device("Limits phone")
        # a paired phone may change them too, within the same bounds
        for bad in ({"task_limit_minutes": 1441}, {"bash_limit_minutes": 241}, {"bash_limit_minutes": "30"}):
            st, _, _ = self.req("POST", url, bad, headers=dev)
            self.assertEqual(st, 400, bad)
        self.assertEqual((ag.meta["bash_limit_minutes"], ag.meta["task_limit_minutes"]), (45, 480))
        st, _, b = self.req("POST", url, {"task_limit_minutes": 1440}, headers=dev)
        self.assertEqual((st, json.loads(b)["task_limit_minutes"]), (200, 1440))
        self.req("POST", url, {"task_limit_minutes": 480}, headers=self.auth())

        # background work: minutes, or 0 to wait for it until the task ends
        for v, ms in ((30, "1800000"), (0, "0")):
            st, _, b = self.req("POST", url, {"bg_limit_minutes": v}, headers=self.auth())
            self.assertEqual((st, json.loads(b)["bg_limit_minutes"]), (200, v))
            self.assertEqual(payload()["agent_env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], ms)
        for bad in ({"bg_limit_minutes": -1}, {"bg_limit_minutes": 1441}, {"bg_limit_minutes": False}, {"bg_limit_minutes": "0"}):
            st, _, _ = self.req("POST", url, bad, headers=self.auth())
            self.assertEqual(st, 400, bad)
        self.req("POST", url, {"bg_limit_minutes": None}, headers=self.auth())
        self.assertNotIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", payload()["agent_env"])

        st, _, b = self.req("POST", url, {"bash_limit_minutes": None}, headers=self.auth())  # back to the default, one at a time
        self.assertEqual((st, json.loads(b)["task_limit_minutes"]), (200, 480))
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", payload()["agent_env"])
        self.req("POST", url, {"task_limit_minutes": None}, headers=self.auth())
        self.assertEqual(payload()["timeout_s"], glob * 60)
        ag.meta["task_limit_minutes"] = "999999"  # a meta file edited by hand: ignored, never sent
        ag.meta["bash_limit_minutes"] = 10 ** 9
        p = payload()
        self.assertEqual(p["timeout_s"], glob * 60)
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", p["agent_env"])
        shown = next(x for x in json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["agents_list"] if x["id"] == ag.id)
        self.assertEqual((shown["bash_limit_minutes"], shown["task_limit_minutes"]), (None, None))  # what the server uses: the defaults
        self.assertEqual(sorted(shown["limits_ignored"]), ["bash_limit_minutes", "task_limit_minutes"])
        ag.meta["task_limit_minutes"] = 90
        shown = next(x for x in json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["agents_list"] if x["id"] == ag.id)
        self.assertEqual((shown["task_limit_minutes"], shown["limits_ignored"]), (90, ["bash_limit_minutes"]))
        self.assertEqual(payload()["timeout_s"], 90 * 60)

    def test_vm_notes_follow_the_agent_and_the_switch(self):
        from cilly.server import vm_notes
        ag = self.app.manager.create(name="s", mode="ephemeral", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            md = self.app.policy_for(ag)["claude_md"]
            self.assertIn("About this machine", md)
            self.assertIn("~/scratch", md)
            self.assertIn("~/outbox/", md)
            self.assertIn("ephemeral", md)
            self.assertNotIn("~/requests/", md)
            self.assertNotIn("vm_notes", self.app.policy_for(ag))
            st, _, body = self.req("GET", "/api/policy", headers=self.auth())
            pol = json.loads(body)["policy"]
            pol["vm_notes"] = False
            st, _, _ = self.req("PUT", "/api/policy", {"policy": pol}, headers=self.auth())
            self.assertEqual(st, 200)
            self.assertNotIn("About this machine", self.app.policy_for(ag)["claude_md"])
        finally:
            self.app.manager.delete(ag.id)
        repo = vm_notes({"repo": "me/app", "mode": "persistent", "research_agent": "abcd1234",
                         "inject": {"path_in_vm": "/inject"}})
        for s in ("git checkout of me/app", "pull request", "~/pull-request.md", "~/requests/", "/inject"):
            self.assertIn(s, repo)
        self.assertNotIn("ephemeral", repo)
        self.assertNotIn("git branch -m", repo)  # without the settings: the name it got
        cfg = json.loads(json.dumps(self.app.cfg()))
        self.assertIn("git branch -m and a name like fix/login-timeout: feat, fix, docs", vm_notes({"repo": "me/app"}, (), cfg))
        cfg["identity"]["branch_names"] = "prefix"
        self.assertIn("like cilly/login-timeout: a few lowercase", vm_notes({"repo": "me/app"}, (), cfg))
        cfg["identity"]["branch_names"] = "id"
        self.assertNotIn("git branch -m", vm_notes({"repo": "me/app"}, (), cfg))
        self.assertNotIn("git branch -m", vm_notes({"mode": "persistent"}, (), self.app.cfg()))  # no repository
        research = vm_notes({"profile": "research", "mode": "persistent"})
        self.assertIn("findings to ~/outbox/", research)
        self.assertNotIn("Export button", research)
        self.assertNotIn("\u2014", repo + research)  # no em dashes
        # it knows what survives its VM stopping, and when that happens
        mem = vm_notes({"repo": "me/app", "mode": "memory", "auto_stop": True, "cpus": 2, "memory_mb": 2048})
        for s in ("ephemeral with memory", "conversation and your memory notes are saved", "Commits on your branch are pushed",
                  "stops after each of your turns", "2 CPUs and 2048 MiB"):
            self.assertIn(s, mem)
        self.assertIn("disk is kept", repo)
        self.assertIn("keeps running between messages", repo)
        self.assertIn("as a new conversation", vm_notes({"mode": "ephemeral", "auto_stop": True}))
        for notes in (repo, research):
            self.assertIn("~/status.txt", notes)  # its one-line summary for the agent list

    def idle_agent(self, mode="persistent", app=None, aid=None):
        """An agent whose VM is up with nothing to do (a stand-in for QEMU that never exits)."""
        app = app or self.app
        ag = app.manager.get(aid) if aid else app.manager.create(
            name="idle", mode=mode, repo=None, agent="claude", cpus=1, memory_mb=1024, auto_stop=False, branch_prefix="agent/")
        if not aid:
            self.addCleanup(lambda: (ag.procs.pop("qemu", None), self.app.manager.agents.get(ag.id) and self.app.manager.delete(ag.id)))
        ag.procs["qemu"] = mock.Mock(poll=lambda: None)
        ag.meta["state"] = "ready"
        return ag

    def test_stop_when_idle_defaults(self):
        from cilly.server import idle_stop_minutes
        self.assertEqual([idle_stop_minutes({"mode": m}, 30) for m in ("persistent", "memory", "ephemeral")], [30, 0, 0])
        self.assertEqual(idle_stop_minutes({"mode": "persistent", "profile": "research"}, 30), 0)
        self.assertEqual(idle_stop_minutes({"mode": "ephemeral", "idle_stop": 15}, 30), 15)  # your own choice wins
        self.assertEqual(idle_stop_minutes({"mode": "memory", "idle_stop": 0}, 30), 0)
        self.assertEqual(idle_stop_minutes({"mode": "persistent", "idle_stop": 45}, 60), 60)  # not a choice: the default
        self.assertEqual(idle_stop_minutes({"mode": "memory", "idle_stop": 45}, 60), 0)
        self.assertEqual(self.app.cfg()["vm"]["idle_stop_minutes"], 30)
        eph = self.idle_agent("ephemeral")
        eph.meta["idle_since"] = time.time() - 10 * 3600
        with mock.patch.object(self.app.manager, "_stop_sync") as stop:
            self.app.check_idle()
        stop.assert_not_called()  # ephemeral agents start with it off

    def test_stop_when_idle_clock(self):
        from cilly.server import agent_status
        ag = self.idle_agent()
        t0 = time.time() - 3 * 3600
        with mock.patch.object(self.app.manager, "_stop_sync") as stop:
            self.app.check_idle(t0)
            self.assertEqual(ag.meta["idle_since"], t0)  # the clock starts when the panel sees it idle
            self.assertEqual(agent_status({**ag.public(), "running": True}, 0, 30), {"key": "idle", "stops_at": int(t0) + 1800})
            self.app.check_idle(t0 + 29 * 60)
            stop.assert_not_called()
            # each kind of activity starts it again
            ag.update_task("x", state="done")  # a task (its start, end or any change)
            self.assertIsNone(ag.meta["idle_since"])
            self.app.check_idle(t0 + 30 * 60)
            self.assertEqual(ag.meta["idle_since"], t0 + 30 * 60)
            with mock.patch.object(self.app.manager, "_dispatch"):
                self.app.manager.new_task(ag.id, {"instructions": "hi", "env": {}})  # a message
            self.assertIsNone(ag.meta["idle_since"])
            self.app.check_idle(t0 + 40 * 60)
            ag.queue.append({"id": "q1"})  # a queued message counts as running
            self.app.check_idle(t0 + 80 * 60)
            self.assertIsNone(ag.meta["idle_since"])
            ag.queue.clear()
            self.app.check_idle(t0 + 81 * 60)
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/keep-running", {}, headers=self.auth())
            self.assertEqual(st, 200)
            self.assertGreater(ag.meta["idle_since"], t0 + 81 * 60)  # Keep Running: from now
            stop.assert_not_called()
            # it fires after its time, with a normal Stop
            self.app.check_idle(ag.meta["idle_since"] + 30 * 60)
            self.assertEqual((ag.meta["state"], ag.meta["idle_since"]), ("stopping", None))
            for _ in range(50):
                if stop.called:
                    break
                time.sleep(0.02)
            stop.assert_called_once_with(ag)

    def test_stop_when_idle_waits_while_running_or_waiting_for_you(self):
        ag = self.idle_agent()
        t0 = 1_800_000_000.0
        with mock.patch.object(self.app.manager, "_stop_sync") as stop:
            ag.meta["idle_since"] = t0
            ag.meta["state"] = "busy"  # Running
            self.app.check_idle(t0 + 3 * 3600)
            self.assertIsNone(ag.meta["idle_since"])
            ag.meta["state"] = "ready"
            self.app.check_idle(t0 + 3 * 3600)
            with open(ag.paths.egress_log, "a") as f:  # a blocked site: Waiting for you
                f.write(json.dumps({"ts": "2026-10-14T09:00:00Z", "action": "deny", "host": "blocked.example", "port": 443}) + "\n")
            for h in range(4, 10):
                self.app.check_idle(t0 + h * 3600)
                self.assertIsNone(ag.meta["idle_since"])
            stop.assert_not_called()
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/denials/dismiss", {"host": "blocked.example", "port": 443},
                                headers=self.auth())
            self.assertEqual(st, 200)
            self.app.check_idle(t0 + 10 * 3600)  # the inbox item is gone, which counts as activity...
            self.assertIsNone(ag.meta["idle_since"])
            self.app.check_idle(t0 + 10 * 3600 + 15)  # ...so the clock starts again at the next check
            self.assertEqual(ag.meta["idle_since"], t0 + 10 * 3600 + 15)
            self.app.check_idle(t0 + 10 * 3600 + 29 * 60)
            stop.assert_not_called()

    def test_a_restarted_panel_starts_the_clock_afresh(self):
        """VMs stop with the panel, so a saved clock is stale: an agent started soon after a restart
        must not be stopped at once."""
        from cilly.server import App
        ag = self.idle_agent()
        ag.set(idle_since=time.time() - 3 * 3600)  # saved long ago
        ag.procs.pop("qemu")
        again = App(self.port)  # the panel restarts and reads the agents from disk
        loaded = again.manager.get(ag.id)
        self.assertEqual((loaded.meta["state"], loaded.meta["idle_since"]), ("stopped", None))
        self.idle_agent(app=again, aid=ag.id)  # it's started again
        with mock.patch.object(again.manager, "stop_if_idle") as stop:
            now = time.time()
            again.check_idle(now)
            self.assertEqual(loaded.meta["idle_since"], now)  # the clock starts now...
            again.check_idle(now + 29 * 60)
        stop.assert_not_called()  # ...so nothing is stopped yet
        loaded.procs.pop("qemu", None)

    def test_stop_if_idle_decides_and_stops_in_one_step(self):
        ag = self.idle_agent()
        since = time.time() - 3600
        ag.meta["idle_since"] = since
        with mock.patch.object(self.app.manager, "_stop_sync") as stop_sync:
            ag.meta["state"] = "busy"  # a message was dispatched in between
            self.assertFalse(self.app.manager.stop_if_idle(ag.id, since))
            ag.meta["state"] = "ready"
            ag.queue.append({"id": "q"})  # or queued
            self.assertFalse(self.app.manager.stop_if_idle(ag.id, since))
            ag.queue.clear()
            ag.meta["idle_since"] = None  # or any activity cleared the clock
            self.assertFalse(self.app.manager.stop_if_idle(ag.id, since))
            ag.meta["idle_since"] = since
            ag.exports["e1"] = {}  # an export in progress
            self.assertFalse(self.app.manager.stop_if_idle(ag.id, since))
            ag.exports.clear()
            self.assertEqual(ag.meta["state"], "ready")
            stop_sync.assert_not_called()
            self.assertTrue(self.app.manager.stop_if_idle(ag.id, since))  # still the same idle stretch
            self.assertEqual((ag.meta["state"], ag.meta["idle_since"]), ("stopping", None))
            for _ in range(50):
                if stop_sync.called:
                    break
                time.sleep(0.02)
            stop_sync.assert_called_once_with(ag)

    def test_a_message_in_the_gap_is_never_stopped(self):
        """check_idle decides, then a message arrives before the stop: the clock it decided on is gone."""
        ag = self.idle_agent()
        t0 = time.time() - 3 * 3600
        ag.meta["idle_since"] = t0
        real = self.app.manager.stop_if_idle

        def message_first(aid, since, *more):
            with mock.patch.object(self.app.manager, "_dispatch"):
                self.app.manager.new_task(ag.id, {"instructions": "hi", "env": {}})
            return real(aid, since, *more)
        with mock.patch.object(self.app.manager, "stop_if_idle", side_effect=message_first) as stop_if_idle, \
                mock.patch.object(self.app.manager, "_stop_sync") as stop_sync:
            self.app.check_idle(t0 + 31 * 60)
        stop_if_idle.assert_called_once_with(ag.id, t0, mock.ANY)
        stop_sync.assert_not_called()
        self.assertEqual(ag.meta["state"], "ready")

    def test_an_inbox_item_in_the_gap_is_never_stopped(self):
        """check_idle saw an empty inbox, then a blocked site comes in before the stop."""
        ag = self.idle_agent()
        t0 = time.time() - 3 * 3600
        self.app.check_idle(t0)
        real = self.app.manager.stop_if_idle

        def blocked_site_first(*args):
            with open(ag.paths.egress_log, "a") as f:
                f.write(json.dumps({"ts": "2026-10-14T09:00:00Z", "action": "deny", "host": "blocked.example", "port": 443}) + "\n")
            return real(*args)
        with mock.patch.object(self.app.manager, "stop_if_idle", side_effect=blocked_site_first), \
                mock.patch.object(self.app.manager, "_stop_sync") as stop_sync:
            self.app.check_idle(t0 + 31 * 60)
        stop_sync.assert_not_called()
        self.assertEqual(ag.meta["state"], "ready")

    def test_transfers_count_as_activity(self):
        ag = self.idle_agent()
        t0 = time.time() - 3 * 3600
        ag.meta["idle_since"] = t0
        with mock.patch.object(self.app.manager, "stop_if_idle") as stop:
            for busy in (ag.exports, ag.fileops):  # an export, or a file being viewed in the panel
                busy["x"] = {}
                self.app.check_idle(t0 + 31 * 60)
                self.assertIsNone(ag.meta["idle_since"])
                busy.clear()
                ag.meta["idle_since"] = t0
        stop.assert_not_called()

    def test_deleting_an_agent_forgets_its_inbox(self):
        ag = self.app.manager.create(name="gone", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.app.check_idle()
        self.assertIn(ag.id, self.app._inbox_seen)
        st, _, _ = self.req("POST", f"/api/agents/{ag.id}/delete", {}, headers=self.auth())
        self.assertEqual(st, 200)
        self.assertNotIn(ag.id, self.app._inbox_seen)

    def test_stop_when_idle_setting(self):
        ag = self.idle_agent()
        for bad in (45, "30", 30.0, True, None, -1):
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/idle-stop", {"value": bad}, headers=self.auth())
            self.assertEqual(st, 400, bad)
        ag.meta["idle_since"] = 5.0
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/idle-stop", {"value": 120}, headers=self.auth())
        self.assertEqual((st, ag.meta["idle_stop"], ag.meta["idle_since"]), (200, 120, None))
        st, _, b = self.req("GET", "/api/state", headers=self.auth())
        self.assertEqual(next(a for a in json.loads(b)["agents_list"] if a["id"] == ag.id)["idle_stop"], 120)
        from cilly.server import device_may
        for action in ("idle-stop", "keep-running", "stop", "auto-stop"):  # a phone may, like Stop and Stop After Each Task
            self.assertTrue(device_may("POST", f"/api/agents/{ag.id}/{action}"), action)
        import copy
        from cilly import config as C
        bad = copy.deepcopy(C.DEFAULT_CONFIG)
        bad["vm"]["idle_stop_minutes"] = 10
        with self.assertRaisesRegex(ValueError, "idle_stop_minutes"):
            C.validate(bad)

    def test_new_agents_keep_the_idle_defaults(self):
        mgr, made = self.app.manager, []
        self.addCleanup(lambda: [mgr.delete(aid) for aid in made])
        shown = {}
        with mock.patch.object(mgr, "start"):  # no VM here
            for mode, profile in (("ephemeral", "coding"), ("persistent", "coding"), ("memory", "coding"),
                                  ("ephemeral", "research"), ("persistent", "research")):
                st, _, b = self.req("POST", "/api/agents", {"name": f"{mode}-{profile}", "mode": mode, "profile": profile},
                                    headers=self.auth())
                self.assertEqual(st, 200, b)
                made.append(json.loads(b)["id"])
        for a in json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["agents_list"]:
            shown[a["name"]] = (a["auto_stop"], a["idle_stop"])
        # ephemeral: Stop right away; persistent: 30 min; the others: Keep running
        self.assertEqual(shown, {"ephemeral-coding": (True, 0), "persistent-coding": (False, 30), "memory-coding": (False, 0),
                                 "ephemeral-research": (False, 0), "persistent-research": (False, 0)})

    def test_moving_to_a_new_image_is_for_the_pc_only(self):
        """It replaces an agent's disk, like Reset to clean: never from a paired device."""
        from cilly.server import device_may
        mgr = self.app.manager
        ag = mgr.create(name="mv", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                        auto_stop=False, branch_prefix="agent/")
        self.addCleanup(mgr.delete, ag.id)
        url = f"/api/agents/{ag.id}/move-image"
        self.assertFalse(device_may("POST", url))
        dev, _ = self.paired_device("Phone")
        calls = []
        with mock.patch.object(mgr, "move_image", lambda aid, image, policy: calls.append((aid, image, policy))):
            st, _, b = self.req("POST", url, {"image": "base-new.qcow2"}, headers=dev)
            self.assertEqual(st, 403, b)
            self.assertEqual(calls, [])
            st, _, b = self.req("POST", url, {"image": ""}, headers=self.auth())
            self.assertEqual(st, 400, b)
            st, _, b = self.req("POST", url, {"image": "base-new.qcow2"}, headers=self.auth())
            self.assertEqual(st, 200, b)
        self.assertEqual([(c[0], c[1]) for c in calls], [(ag.id, "base-new.qcow2")])
        self.assertIn("claude_md", calls[0][2])  # the agent's own policy, for the hello

    def test_the_first_message_of_a_new_agent_is_queued(self):
        """Also for an agent without a repository, which works in its scratch folder."""
        mgr, made = self.app.manager, []
        self.addCleanup(lambda: [mgr.delete(aid) for aid in made])
        for key in ("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "research:CLAUDE_CODE_OAUTH_TOKEN"):
            self.app.secrets.set_pasted(key, "sk-x")
            self.addCleanup(self.app.secrets.forget, key)
        with mock.patch.object(mgr, "start"):  # no VM here
            for profile in ("coding", "research"):
                st, _, b = self.req("POST", "/api/agents", {"name": f"first-{profile}", "profile": profile,
                                                            "instructions": "List the files in the scratch folder"},
                                    headers=self.auth())
                self.assertEqual(st, 200, b)
                ag = mgr.get(json.loads(b)["id"])
                made.append(ag.id)
                self.assertEqual(len(ag.queue), 1, profile)
                self.assertEqual([t["state"] for t in ag.meta["tasks"]], ["queued"], profile)
                self.assertIn("List the files in the scratch folder", ag.meta["tasks"][0]["instructions"])

    def test_when_it_has_nothing_to_do_is_one_choice(self):
        from cilly.server import agent_status, device_may, idle_stop_minutes
        ag = self.idle_agent()
        post = lambda action, v: self.req("POST", f"/api/agents/{ag.id}/{action}", {"value": v}, headers=self.auth())[0]
        shown = lambda: next((a["auto_stop"], a["idle_stop"]) for a in json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["agents_list"]
                             if a["id"] == ag.id)
        self.assertEqual(shown(), (False, 30))  # a persistent agent's default
        for bad in ("x", 0, 45, True, None, "30", 30.0, ["keep"], {"v": 1}):
            self.assertEqual(post("when-idle", bad), 400, bad)
        self.assertEqual(shown(), (False, 30))
        ag.meta["idle_since"] = 5.0
        self.assertEqual(post("when-idle", "task"), 200)
        self.assertEqual((ag.meta["auto_stop"], ag.meta["idle_stop"], ag.meta["idle_since"], shown()), (True, 0, None, (True, 0)))
        self.assertEqual(post("when-idle", 60), 200)
        self.assertEqual((ag.meta["auto_stop"], ag.meta["idle_stop"], shown()), (False, 60, (False, 60)))
        self.assertEqual(post("when-idle", "keep"), 200)
        self.assertEqual((ag.meta["auto_stop"], ag.meta["idle_stop"], shown()), (False, 0, (False, 0)))
        # the two older calls keep them apart too
        post("idle-stop", 120); post("auto-stop", True)
        self.assertEqual((ag.meta["auto_stop"], ag.meta["idle_stop"]), (True, 0))
        post("idle-stop", 15)
        self.assertEqual((ag.meta["auto_stop"], ag.meta["idle_stop"]), (False, 15))
        # older data with both: Stop After Each Task is what applies, and the idle clock doesn't run
        ag.meta.update(auto_stop=True, idle_stop=120, idle_since=time.time() - 10 * 3600)
        self.assertEqual(shown(), (True, 0))
        self.assertEqual(idle_stop_minutes(ag.meta, 30), 0)
        self.assertEqual(agent_status({**ag.meta, "running": True, "queue": []}, 0, 30), {"key": "idle"})
        with mock.patch.object(self.app.manager, "_stop_sync") as stop:
            self.app.check_idle()
        stop.assert_not_called()
        # a research agent has no Stop After Each Task, so its old flag doesn't hide its clock
        self.assertEqual(idle_stop_minutes({"profile": "research", "auto_stop": True, "idle_stop": 15}, 30), 15)
        res = self.app.manager.create(name="res", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                      auto_stop=False, branch_prefix="agent/", profile="research")
        self.addCleanup(lambda: self.app.manager.delete(res.id))
        rpost = lambda v: self.req("POST", f"/api/agents/{res.id}/when-idle", {"value": v}, headers=self.auth())[0]
        self.assertEqual([rpost("task"), rpost(30), rpost("keep")], [400, 200, 200])
        self.assertEqual((res.meta["auto_stop"], res.meta["idle_stop"]), (False, 0))
        # a paired phone may, as it may set each of the two already; nothing else changes for it
        self.assertTrue(device_may("POST", f"/api/agents/{ag.id}/when-idle"))
        self.assertFalse(device_may("GET", f"/api/agents/{ag.id}/when-idle"))
        self.assertFalse(device_may("POST", f"/api/agents/{ag.id}/when-idle/x"))

    def test_agent_status_is_worked_out_by_the_panel(self):
        from cilly.server import agent_status
        cases = [({"state": "booting"}, 0, {"key": "starting"}), ({"state": "stopping"}, 3, {"key": "stopping"}),
                 ({"state": "failed", "running": False}, 2, {"key": "failed"}),
                 ({"state": "busy", "running": True, "queue": []}, 0, {"key": "running", "queued": 0}),
                 ({"state": "busy", "running": True, "queue": ["a", "b"]}, 4, {"key": "running", "queued": 2}),
                 ({"state": "ready", "running": True, "queue": []}, 1, {"key": "waiting", "items": 1}),
                 ({"state": "stopped", "running": False, "mode": "persistent"}, 2, {"key": "waiting", "items": 2}),  # it comes back
                 ({"state": "stopped", "running": False, "mode": "memory"}, 2, {"key": "waiting", "items": 2}),
                 ({"state": "stopped", "running": False, "mode": "ephemeral"}, 2, {"key": "waiting", "items": 2}),
                 ({"state": "ready", "running": True, "mode": "ephemeral", "queue": []}, 1, {"key": "waiting", "items": 1}),
                 ({"state": "ready", "running": True, "queue": []}, 0, {"key": "idle"}),
                 ({"state": "stopped", "running": False}, 0, {"key": "stopped"}),
                 ({"state": "ready", "running": False}, 0, {"key": "stopped"}),  # its VM died
                 ({"state": "ready", "running": True, "queue": ["q"]}, 0, {"key": "running", "queued": 1}),
                 ({"state": "ready", "running": True, "queue": ["q"]}, 2, {"key": "running", "queued": 1}),  # before the inbox
                 ({"state": "stopped", "running": False, "move": {"step": 2}}, 1, {"key": "starting"})]  # between a move's boots
        for a, waiting, want in cases:
            self.assertEqual(agent_status(dict(a, summary="Running and waiting for you"), waiting), want, (a, waiting))

    def test_state_lists_each_agents_status_from_its_inbox(self):
        ag = self.app.manager.create(name="w", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.addCleanup(self.app.manager.delete, ag.id)

        def status():
            st, _, b = self.req("GET", "/api/state", headers=self.auth())
            return next(a["status"] for a in json.loads(b)["agents_list"] if a["id"] == ag.id)
        self.assertEqual(status(), {"key": "stopped"})
        with open(ag.paths.egress_log, "a") as f:
            f.write(json.dumps({"ts": "2026-10-14T09:00:00Z", "action": "deny", "host": "blocked.example", "port": 443}) + "\n")
        self.assertEqual(status(), {"key": "waiting", "items": 1})
        with open(ag.paths.egress_log, "a") as f:  # the log changed, so it's read again
            f.write(json.dumps({"ts": "2026-10-14T09:01:00Z", "action": "deny", "host": "other.example", "port": 443}) + "\n")
        ag.set(requests=[{"id": "r1", "state": "pending", "name": "q", "text": "?"}])
        self.assertEqual(status(), {"key": "waiting", "items": 3})
        self.assertEqual(json.loads(self.req("GET", "/api/inbox", headers=self.auth())[2])["count"], 3)

    def test_a_browser_leaving_early_is_not_an_error(self):
        from http.server import BaseHTTPRequestHandler
        from unittest import mock
        Handler = self.httpd.RequestHandlerClass
        h = object.__new__(Handler)
        for err in (BrokenPipeError, ConnectionResetError):
            with mock.patch.object(BaseHTTPRequestHandler, "handle", side_effect=err):
                Handler.handle(h)  # no traceback in the terminal
        with mock.patch.object(BaseHTTPRequestHandler, "handle", side_effect=ValueError):
            with self.assertRaises(ValueError):
                Handler.handle(h)  # real errors still show

    def test_pull_requests_say_when_the_panel_has_no_token(self):
        from unittest import mock
        from cilly import github as GH
        def nope(repo, tok, prefix):
            raise GH.GitHubError("GitHub API 404: {\"message\":\"Not Found\"}")
        from cilly import config as C
        two = mock.patch.object(C, "repos", lambda cfg: ["me/a", "me/b"])
        with two, mock.patch.object(GH, "list_agent_prs", nope), mock.patch.object(self.app, "panel_token", lambda: None):
            errors = self.app.api_prs({})["errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("panel_token", errors[0])
        with two, mock.patch.object(GH, "list_agent_prs", nope), mock.patch.object(self.app, "panel_token", lambda: "tok"):
            self.assertIn("can't see this repository", self.app.api_prs({})["errors"][0])

    def test_branches_are_named_after_the_task(self):
        """The first task names the branch, a new branch per task gets its own name, and the payload says
        which names the agent may rename it to.  Branches GitHub confirmed are the agent's in the PR list."""
        from unittest import mock
        from cilly import github as GH
        ag = self.app.manager.create(name="named", mode="persistent", repo="owner/repo", agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="cilly/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        self.assertIsNone(ag.meta["branch"])
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
        self.addCleanup(self.app.secrets.forget, "agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
        with mock.patch.object(self.app, "_secret", lambda *a: "ghp_x"), mock.patch.object(C, "token_for_repo", lambda cfg, r, prefer=None: "bot"):
            p1 = self.app._task_payload(ag, "Fix the login timeout", new_branch=False, cont=False, pr_title="")
            p2 = self.app._task_payload(ag, "And the tests", new_branch=False, cont=True, pr_title="")
            p3 = self.app._task_payload(ag, "Fix the login timeout", new_branch=True, cont=False, pr_title="")
        self.assertEqual((p1["branch"], p2["branch"], p3["branch"], ag.meta["branch"]),
                         ("fix/login-timeout", "fix/login-timeout", "fix/login-timeout-2", "fix/login-timeout"))
        self.assertTrue(p1["branch_fresh"])
        self.assertEqual(p1["branch_rename"], C.branch_pattern(self.app.cfg()))
        with open(ag.paths.branches_file, "w") as f:  # the egress proxy saw GitHub create it
            f.write('{"branch": "fix/login-timeout"}\nnot json\n{"branch": "../x"}\n')
        self.assertEqual(self.app.manager.branches(ag), {"fix/login-timeout"})
        with mock.patch.object(self.app, "_secret", lambda *a: "ghp_x"), mock.patch.object(C, "token_for_repo", lambda cfg, r, prefer=None: "bot"):
            self.assertFalse(self.app._task_payload(ag, "More", new_branch=False, cont=True, pr_title="")["branch_fresh"])
        self.assertTrue(self.app.manager.branch_ok(ag, "fix/other") and self.app.manager.branch_ok(ag, f"vmpanel/{ag.id}-2"))
        self.assertFalse(self.app.manager.branch_ok(ag, "main") or self.app.manager.branch_ok(ag, "release/1.0")
                         or self.app.manager.branch_ok(ag, "fix/../x"))
        prs = [{"number": n, "head": {"ref": ref, "repo": {"full_name": "owner/repo"}}, "html_url": f"https://github.com/owner/repo/pull/{n}"}
               for n, ref in ((1, "fix/login-timeout"), (2, "fix/someones"), (3, f"agent/{ag.id}"), (4, "cilly/x"), (5, "main"))]
        with mock.patch.object(GH, "api_get", lambda *a, **k: prs), mock.patch.object(C, "repos", lambda cfg: ["owner/repo"]):
            self.assertEqual([p["number"] for p in self.app.api_prs({})["prs"]], [1, 3, 4])

    def test_resume_and_effort_reach_the_payload(self):
        from unittest import mock
        ag = self.app.manager.create(name="s", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        sid = "0b3e8f4c-1d2a-4c5b-9e6f-7a8b9c0d1e2f"
        try:
            self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "tok")
            ag.meta["tasks"] = [{"id": "aaaa1111", "session": sid}, {"id": "bbbb2222"}]
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/model", {"effort": "turbo"}, headers=self.auth())
            self.assertEqual(st, 400)
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/model", {"effort": "high"}, headers=self.auth())
            self.assertEqual((st, json.loads(body)["effort"]), (200, "high"))
            got = []
            with mock.patch.object(self.app.manager, "new_task", side_effect=lambda aid, p, **kw: got.append((p, kw)) or {"id": "t"}):
                st, _, body = self.req("POST", f"/api/agents/{ag.id}/message", {"instructions": "hi", "resume": "aaaa1111"},
                                       headers=self.auth())
                self.assertEqual(st, 200, body)
                p, kw = got[-1]
                self.assertEqual((p["resume"], p["continue"], p["effort"]), (sid, False, "high"))
                self.assertEqual(kw["record"], {"resumed_from": "aaaa1111"})
                for bad in ("bbbb2222", "nope"):  # no conversation recorded / unknown task
                    st, _, body = self.req("POST", f"/api/agents/{ag.id}/message", {"instructions": "hi", "resume": bad},
                                           headers=self.auth())
                    self.assertEqual(st, 400)
                st, _, body = self.req("POST", f"/api/agents/{ag.id}/message", {"instructions": "hi"}, headers=self.auth())
                self.assertNotIn("resume", got[-1][0])
                st, _, body = self.req("POST", f"/api/agents/{ag.id}/message", {"instructions": "x" * 50_001}, headers=self.auth())
                self.assertEqual(st, 400)
                self.assertIn("This message has 50,001 characters, and one can have at most 50,000.", json.loads(body)["error"])
        finally:
            self.app.secrets.forget("agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
            self.app.manager.delete(ag.id)

    def test_images_and_toolchains(self):
        from cilly import vm as V
        from cilly.server import TOOLCHAIN_NOTES, vm_notes
        name = "base-20261014-100000.qcow2"
        open(os.path.join(V.images_dir(), name), "w").close()
        self.addCleanup(os.remove, os.path.join(V.images_dir(), name))
        with open(os.path.join(V.images_dir(), name[:-6] + ".json"), "w") as f:
            json.dump({"toolchains": ["java", "rust", 5], "built": "2026-10-14T10:00:00Z"}, f)
        st, _, body = self.req("GET", "/api/state", headers=self.auth())
        s = json.loads(body)
        self.assertEqual([i for i in s["images"] if i["name"] == name][0]["toolchains"], ["java", "rust"])
        self.assertIn("java", s["recipes"])
        st, _, body = self.req("POST", "/api/agents", {"name": "x", "mode": "ephemeral", "repo": None, "image": "../etc/passwd"},
                               headers=self.auth())
        self.assertEqual(st, 400)
        self.assertIn("unknown VM image", json.loads(body)["error"])
        ag = self.app.manager.create(name="x", mode="ephemeral", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/image", {"image": name}, headers=self.auth())
            self.assertEqual((st, ag.meta["image"]), (200, name))
            md = self.app.policy_for(ag)["claude_md"]
            self.assertIn("Java 25", md)
            self.assertIn("Rust stable", md)
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/image", {"image": "nope.qcow2"}, headers=self.auth())
            self.assertEqual(st, 400)
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/image", {"image": None}, headers=self.auth())
            self.assertIsNone(json.loads(body)["image"])
            st, _, _ = self.req("POST", "/api/images/default", {"name": "nope.qcow2"}, headers=self.auth())
            self.assertEqual(st, 400)
            st, _, body = self.req("POST", "/api/images/default", {"name": name}, headers=self.auth())
            self.assertEqual((st, self.app.cfg()["base_image"]), (200, name))
        finally:
            self.app.manager.delete(ag.id)
        self.assertNotIn("Java", vm_notes({"mode": "ephemeral"}, []))
        from cilly.base_image import RECIPES
        self.assertLessEqual(set(RECIPES), set(TOOLCHAIN_NOTES))  # agents learn about every toolchain
        dec = vm_notes({"mode": "ephemeral"}, ["java", "decompilers"])
        for s in ("jar-diff <old.jar> <new.jar>", "vineflower [options] <jar> <outdir>", "cfr <jar> <outdir>", "read-only in /inject or /attachments",
                  "~/scratch"):
            self.assertIn(s, dec)
        rea = vm_notes({"mode": "ephemeral"}, ["java", "rea"])
        for s in ("REA", "rea --help", "MCP tools", "run the target inside this VM", "can't update itself", "~/scratch"):
            self.assertIn(s, rea)

    def test_deleting_an_image(self):
        from unittest import mock
        from cilly import vm as V
        names = ["base-20261015-100000.qcow2", "base-20261015-110000.qcow2", "base-20261015-120000.qcow2", "base-20261015-130000.qcow2"]
        for n in names:
            with open(os.path.join(V.images_dir(), n), "w") as f:
                f.write("x" * 1000)
            with open(os.path.join(V.images_dir(), n[:-6] + ".json"), "w") as f:
                json.dump({"toolchains": [], "preset": "full"}, f)
            self.addCleanup(lambda n=n: [os.remove(p) for p in (os.path.join(V.images_dir(), n), os.path.join(V.images_dir(), n[:-6] + ".json"))
                                         if os.path.exists(p)])
        coding, research, used, spare = names
        before = {k: self.app.cfg().get(k) for k in ("base_image", "research_base_image")}
        new = json.loads(json.dumps(self.app.cfg()))
        new.update(base_image=coding, research_base_image=research)
        self.app.set_cfg(new)
        self.addCleanup(lambda: self.app.set_cfg(dict(json.loads(json.dumps(self.app.cfg())), **before)))
        mk = lambda name, mode: self.app.manager.create(name=name, mode=mode, repo=None, agent="claude", cpus=1,  # noqa: E731
                                                        memory_mb=1024, auto_stop=False, branch_prefix="agent/")
        picker, keeper = mk("picker", "ephemeral"), mk("keeper", "persistent")
        self.addCleanup(self.app.manager.delete, picker.id)
        self.addCleanup(self.app.manager.delete, keeper.id)
        picker.set(image=used)
        p = self.app.projects.create("Image Project")
        self.addCleanup(self.app.projects.delete, p["id"])
        self.app.projects.update(p["id"], defaults={"image": used})
        s = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])
        info = {i["name"]: i for i in s["images"]}
        self.assertEqual((info[used]["used_by"], info[used]["size"], info[spare]["used_by"]), ([picker.id], 1000, []))
        self.assertIn(keeper.id, info[coding]["used_by"])  # no pick: the coding default
        self.app.devices.starts.clear()  # a fresh pairing allowance for this test
        dev, _ = self.paired_device("Image phone")
        st, _, _ = self.req("POST", "/api/images/delete", {"name": spare}, headers=dev)
        self.assertEqual(st, 403)  # owner only
        for name, msg in (("../config.json", "unknown"), ("base-nope.qcow2", "unknown"), (coding, "default image"),
                          (research, "default image")):
            st, _, b = self.req("POST", "/api/images/delete", {"name": name}, headers=self.auth())
            self.assertEqual(st, 400, name)
            self.assertIn(msg, json.loads(b)["error"])
        with mock.patch.object(self.app, "build_state", {"state": "running"}):
            st, _, b = self.req("POST", "/api/images/delete", {"name": spare}, headers=self.auth())
            self.assertIn("build to finish", json.loads(b)["error"])
        with mock.patch.object(type(picker), "running", property(lambda a: a.id == picker.id)):
            st, _, b = self.req("POST", "/api/images/delete", {"name": used}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("Stop picker first", json.loads(b)["error"])
        # booted from it before its pick changed: the running VM's disk still sits on that file
        picker.set(image=spare, base_image=used)
        with mock.patch.object(type(picker), "running", property(lambda a: a.id == picker.id)):
            info = {i["name"]: i for i in json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["images"]}
        self.assertEqual((info[used]["used_by"], info[spare]["used_by"]), ([picker.id], [picker.id]))
        for state in ("booting", "running"):
            with mock.patch.object(type(picker), "running", property(lambda a: state == "running" and a.id == picker.id)):
                picker.meta["state"] = state
                st, _, b = self.req("POST", "/api/images/delete", {"name": used}, headers=self.auth())
                self.assertEqual(st, 400, state)
                self.assertIn("Stop picker first", json.loads(b)["error"])
        picker.set(image=used, base_image=None, state="stopped")
        keeper.set(image=used)  # a persistent agent with a disk built on it: deleting would break that disk
        with open(keeper.paths.disk, "w"):
            pass
        keeper.set(base_image=used)
        st, _, b = self.req("POST", "/api/images/delete", {"name": used}, headers=self.auth())
        self.assertIn("keeper keeps a disk", json.loads(b)["error"])
        os.remove(keeper.paths.disk)
        self.assertTrue(os.path.exists(os.path.join(V.images_dir(), used)))
        st, _, b = self.req("POST", "/api/images/delete", {"name": used}, headers=self.auth())
        self.assertEqual((st, json.loads(b)), (200, {"deleted": used, "freed": 1000}))
        self.assertFalse(os.path.exists(os.path.join(V.images_dir(), used)))
        self.assertFalse(os.path.exists(os.path.join(V.images_dir(), used[:-6] + ".json")))
        self.assertEqual((picker.meta["image"], keeper.meta["image"]), (None, None))  # back to the default
        self.assertIsNone(self.app.projects.get(p["id"])["defaults"]["image"])

    def test_build_progress_steps(self):
        from cilly.server import _BuildLog
        log = _BuildLog()
        self.assertEqual(log.step, 1)
        log.write("booting build VM (/x) …")
        log.write("\n")
        self.assertEqual(log.step, 2)
        log.write("flattening image …")
        log.write("booting build VM again")  # never goes back
        self.assertEqual(log.step, 3)
        # step 2, part by part: what it's installing comes from marker lines, named only with the panel's own names
        log = _BuildLog(["java", "rust", "../x"])
        log.write("booting build VM (/x) …\n")
        self.assertEqual(log.progress(), {"percent": 10, "part": 1, "of": 5, "label": "System packages"})
        for line in ("[ 313.7] cloud-init[528]: |\u25a0\u25a0\ufffd\ufffd |  60% of 2.3 MiB", "[ 313.8] cloud-init[528]: \ufffd\ufffd\u2588",
                     "cilly: toolchain nope", "cilly: toolchain rust is fine <b>", "cilly: toolch"):
            log.write(line + ("\n" if not line.endswith("toolch") else ""))
        self.assertEqual(log.progress()["label"], "System packages")  # unknown names and other text move nothing
        log.write("ain rust\n")  # a marker can come in two pieces
        self.assertEqual(log.progress(), {"percent": 42, "part": 3, "of": 5, "label": "Rust"})
        log.write("cilly: toolchain java\n")  # never goes back
        log.write("cilly: installing agent CLIs: @anthropic-ai/claude-code\n")
        self.assertEqual(log.progress()["label"], "Claude Code and agent tools")
        log.write("cilly: checking the image\nflattening image …\n")
        self.assertEqual(log.progress()["percent"], 95)
        tail = "\n".join(log.tail())
        self.assertNotIn("\ufffd", tail)
        self.assertNotIn("\u25a0", tail)
        self.assertIn("60% of 2.3 MiB", tail)
        st, _, b = self.req("POST", "/api/images/build", {"preset": "full", "source": "/x", "rebuild": "../etc"}, headers=self.auth())
        self.assertIn("unknown VM image", json.loads(b)["error"])

    def test_adding_an_mcp_server(self):
        before = json.loads(json.dumps(self.app.cfg()))
        self.addCleanup(lambda: self.app.set_cfg(before))
        new = json.loads(json.dumps(before))
        new["mcp_secrets"] = {"LINEAR_API_KEY": {}, "GH_TOKEN": {}}
        self.app.set_cfg(new)
        self.assertNotIn("mcp_secret_names", json.loads(self.req("GET", "/api/state", headers=self.auth())[2]))  # the dialog names its own key now
        self.app.devices.starts.clear()  # a fresh pairing allowance for this test
        dev, _ = self.paired_device("MCP phone")
        st, _, _ = self.req("POST", "/api/mcp", {"name": "linear", "url": "https://mcp.linear.app/mcp"}, headers=dev)
        self.assertEqual(st, 403)  # policy is the owner's
        for body, msg in (({"name": "bad name", "url": "https://x.example/mcp"}, "letters, digits"),
                          ({"name": "plain", "url": "http://x.example/mcp"}, "https://"),
                          ({"name": "both", "url": "https://x.example", "command": "npx"}, "a URL or a command"),
                          ({"name": "key", "url": "https://x.example", "secret": "lower_case"}, "in capitals"),
                          ({"name": "brace", "url": "https://x.example", "key": {"name": "LINEAR_API_KEY} ${NEW_KEY"}}, "in capitals"),
                          ({"name": "hdr", "url": "https://x.example", "key": {"name": "K_1", "header": "Cookie"}}, "x-api-key"),
                          ({"name": "valtype", "url": "https://x.example", "key": {"name": "K_1", "value": 5}}, "must be text"),
                          # a key goes in the key field, never hidden in a URL or a command's arguments
                          ({"name": "inurl", "url": "https://x.example/${NEW_KEY}"}, "in the key field"),
                          ({"name": "inargs", "command": "npx", "args": ["--key", "${NEW_KEY}"]}, "in the key field"),
                          ({"name": "reserved", "url": "https://x.example", "key": {"name": "GH_TOKEN"}}, "reserved"),
                          ({"name": "argsbad", "command": "npx", "args": "-y x"}, "arguments"),
                          ({"name": ["linear"], "url": "https://x.example"}, "letters, digits"),
                          ({"name": "urltype", "url": {"https://x.example": 1}}, "must be text"),
                          ({"name": "urllong", "url": "https://x.example/" + "a" * 2000}, "up to 2000"),
                          ({"name": "secrettype", "url": "https://x.example", "key": "A"}, "in capitals"),
                          ({"name": "cmdlong", "command": "n" * 1001}, "arguments"),
                          ({"name": "arglong", "command": "npx", "args": ["a" * 1001]}, "arguments"),
                          ({"name": "argmany", "command": "npx", "args": ["a"] * 101}, "arguments"),
                          ({"name": "argtype", "command": "npx", "args": [1]}, "arguments")):
            st, _, b = self.req("POST", "/api/mcp", body, headers=self.auth())
            self.assertEqual(st, 400, body)
            self.assertIn(msg, json.loads(b)["error"])
        st, _, b = self.req("POST", "/api/mcp", {"name": "linear", "url": "https://mcp.linear.app/mcp", "secret": "LINEAR_API_KEY"},
                            headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.cfg()["mcp_servers"]["linear"],
                         {"type": "http", "url": "https://mcp.linear.app/mcp", "headers": {"Authorization": "Bearer ${LINEAR_API_KEY}"}})
        st, _, b = self.req("GET", "/api/settings", headers=self.auth())
        self.assertEqual(json.loads(b)["secrets"]["mcp:LINEAR_API_KEY"], "missing")  # asked for on first use
        st, _, b = self.req("POST", "/api/mcp", {"name": "linear", "command": "npx"}, headers=self.auth())
        self.assertIn("already exists", json.loads(b)["error"])
        st, _, b = self.req("POST", "/api/mcp", {"name": "files", "command": "npx", "args": ["-y", "@mcp/fs", "/work"]}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.cfg()["mcp_servers"]["files"], {"type": "stdio", "command": "npx", "args": ["-y", "@mcp/fs", "/work"]})
        self.assertNotIn("NEW_KEY", json.dumps(self.app.cfg()))
        # a new key, named and pasted here: as a header for a remote server, in the environment for a local one
        st, _, b = self.req("POST", "/api/mcp", {"name": "pixel", "url": "https://api.pixellab.example/mcp",
                                                 "key": {"name": "PIXEL_KEY", "value": "pk-REAL-0123456789", "header": "x-api-key"}}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertNotIn("pk-REAL", b.decode())  # never back to the page
        self.assertEqual(self.app.cfg()["mcp_servers"]["pixel"]["headers"], {"x-api-key": "${PIXEL_KEY}"})
        self.assertNotIn("pk-REAL", json.dumps(self.app.cfg()))  # the config only names it
        self.assertEqual(self.app.secrets.get("mcp:PIXEL_KEY", {}), "pk-REAL-0123456789")
        pol = json.loads(self.req("GET", "/api/policy", headers=self.auth())[2])
        self.assertEqual((pol["mcp_keys"]["PIXEL_KEY"], pol["mcp_keys"]["LINEAR_API_KEY"]), ("pasted", "missing"))
        self.assertNotIn("pk-REAL", json.dumps(pol))
        st, _, b = self.req("POST", "/api/mcp", {"name": "localkey", "command": "npx", "args": ["-y", "some-mcp"], "key": {"name": "LOCAL_KEY"}},
                            headers=self.auth())
        self.assertEqual(self.app.cfg()["mcp_servers"]["localkey"]["env"], {"LOCAL_KEY": "${LOCAL_KEY}"})
        # a pasted claude mcp add command, read on the page: checked like the JSON, with its own keys only
        whole = {"type": "http", "url": "https://mcp.ref.example/mcp", "headers": {"Authorization": "Bearer ${REF_API_KEY}", "X-Team": "${REF_TEAM}"}}
        for body, msg in (({"name": "w1", "server": dict(whole, url="http://plain.example/mcp")}, "https"),
                          ({"name": "w2", "server": dict(whole, url="https://x.example/${REF_API_KEY}")}, "not in the address"),
                          ({"name": "w3", "server": whole, "keys": {"OTHER_KEY": "x"}}, "own keys"),
                          ({"name": "w4", "server": "nope"}, "isn't a server definition"),
                          ({"name": "w5", "server": dict(whole, headers={"Authorization": "Bearer ${GH_TOKEN}"})}, "reserved")):
            st, _, b = self.req("POST", "/api/mcp", body, headers=self.auth())
            self.assertEqual((st, msg in json.loads(b)["error"]), (400, True), body)
        st, _, b = self.req("POST", "/api/mcp", {"name": "ref", "server": whole, "keys": {"REF_API_KEY": "ref-REAL-123456789", "REF_TEAM": ""}},
                            headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.cfg()["mcp_servers"]["ref"], whole)
        self.assertEqual(self.app.secrets.get("mcp:REF_API_KEY", {}), "ref-REAL-123456789")
        self.assertNotIn("ref-REAL", json.dumps(self.app.cfg()))

    def test_removing_and_duplicating_an_mcp_server(self):
        before = json.loads(json.dumps(self.app.cfg()))
        self.addCleanup(lambda: self.app.set_cfg(before))
        st, _, b = self.req("POST", "/api/mcp", {"name": "docs", "url": "https://docs.example/mcp", "key": {"name": "DOCS_KEY"}}, headers=self.auth())
        self.assertEqual(st, 200, b)
        ag = self.app.manager.create(name="mcp-remover", mode="ephemeral", repo=None, agent="claude", cpus=1,
                                     memory_mb=1024, auto_stop=False, branch_prefix="agent/")
        self.addCleanup(self.app.manager.delete, ag.id)
        self.req("POST", f"/api/agents/{ag.id}/mcp", {"servers": ["docs"]}, headers=self.auth())
        self.assertIn("docs.example", self.app._allowlist(ag))
        # a copy: the same definition and key, on for nobody
        st, _, b = self.req("POST", "/api/mcp/docs/duplicate", {"name": "docs-work"}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.cfg()["mcp_servers"]["docs-work"], self.app.cfg()["mcp_servers"]["docs"])
        self.assertEqual(ag.meta["mcp"], ["docs"])
        for body, msg in (({"name": "docs"}, "already exists"), ({"name": "bad name"}, "letters, digits"), ({}, "letters, digits")):
            st, _, b = self.req("POST", "/api/mcp/docs/duplicate", body, headers=self.auth())
            self.assertEqual((st, msg in json.loads(b)["error"]), (400, True), body)
        self.assertEqual(self.req("POST", "/api/mcp/nope/duplicate", {"name": "x"}, headers=self.auth())[0], 404)
        # removed: out of the library, off for the agent, its host off the agent's allowlist
        pushed = []
        with mock.patch.object(self.app, "_push_policy", lambda a: pushed.append(a.id)):
            st, _, b = self.req("POST", "/api/mcp/docs/delete", {}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertNotIn("docs", self.app.cfg()["mcp_servers"])
        self.assertEqual((ag.meta["mcp"], pushed), ([], [ag.id]))
        self.assertNotIn("docs.example", self.app._allowlist(ag))
        self.assertEqual(self.req("POST", "/api/mcp/docs/delete", {}, headers=self.auth())[0], 404)
        # a paired phone can do neither: the library is the owner's
        self.app.devices.starts.clear()
        dev, _ = self.paired_device("MCP remover phone")
        for path in ("/api/mcp/docs-work/delete", "/api/mcp/docs-work/duplicate"):
            self.assertEqual(self.req("POST", path, {"name": "x"}, headers=dev)[0], 403, path)
        self.assertIn("docs-work", self.app.cfg()["mcp_servers"])

    def test_a_removed_mcp_server_is_unticked_everywhere(self):
        # a new server added under an old name must never be on by itself
        st, _, b = self.req("POST", "/api/mcp", {"name": "gone", "command": "npx"}, headers=self.auth())
        self.assertEqual(st, 200, b)
        ag = self.app.manager.create(name="mcp-user", mode="ephemeral", repo=None, agent="claude", cpus=1,
                                     memory_mb=1024, auto_stop=False, branch_prefix="agent/")
        self.addCleanup(self.app.manager.delete, ag.id)
        p = self.app.projects.create("MCP Project")
        self.addCleanup(self.app.projects.delete, p["id"])
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/mcp", {"servers": ["gone"]}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.app.projects.update(p["id"], defaults={"mcp": ["gone"]})
        pol = json.loads(self.req("GET", "/api/policy", headers=self.auth())[2])
        servers = {k: v for k, v in pol["mcp_servers"].items() if k != "gone"}
        st, _, b = self.req("PUT", "/api/policy", {"policy": pol["policy"], "mcp_servers": servers}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual((ag.meta["mcp"], self.app.projects.get(p["id"])["defaults"]["mcp"]), ([], []))
        # removed behind the panel's back (config.json edited by hand): cleared when the name comes back
        ag.set(mcp=["gone"])
        self.app.projects.update(p["id"], defaults={"mcp": ["gone"]})
        st, _, b = self.req("POST", "/api/mcp", {"name": "gone", "url": "https://other.example/mcp"}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual((ag.meta["mcp"], self.app.projects.get(p["id"])["defaults"]["mcp"]), ([], []))
        self.assertEqual(self.app.policy_for(ag)["managed_mcp"], {"mcpServers": {}})
        new = json.loads(json.dumps(self.app.cfg()))
        del new["mcp_servers"]["gone"]
        self.app.set_cfg(new)

    def test_an_mcp_server_edited_under_its_name_is_unticked_everywhere(self):
        from cilly import config as C
        before = json.loads(json.dumps(self.app.cfg()))
        self.addCleanup(lambda: self.app.set_cfg(before))
        for body in ({"name": "edited", "url": "https://one.example/mcp"}, {"name": "same", "command": "npx"}):
            st, _, b = self.req("POST", "/api/mcp", body, headers=self.auth())
            self.assertEqual(st, 200, b)
        ag = self.app.manager.create(name="mcp-editee", mode="ephemeral", repo=None, agent="claude", cpus=1,
                                     memory_mb=1024, auto_stop=False, branch_prefix="agent/")
        self.addCleanup(self.app.manager.delete, ag.id)
        p = self.app.projects.create("MCP Edits")
        self.addCleanup(self.app.projects.delete, p["id"])

        def tick():
            ag.set(mcp=["edited", "same"])
            self.app.projects.update(p["id"], defaults={"mcp": ["edited", "same"]})

        def ticked():
            return ag.meta["mcp"], self.app.projects.get(p["id"])["defaults"]["mcp"]

        tick()
        pol = json.loads(self.req("GET", "/api/policy", headers=self.auth())[2])
        for edit in ({"type": "http", "url": "https://two.example/mcp"},
                     {"type": "http", "url": "https://two.example/mcp", "headers": {"Authorization": "Bearer ${OTHER_KEY}"}},
                     {"type": "stdio", "command": "uvx", "args": ["one"]}):
            servers = dict(pol["mcp_servers"], edited=edit)
            st, _, b = self.req("PUT", "/api/policy", {"policy": pol["policy"], "mcp_servers": servers}, headers=self.auth())
            self.assertEqual(st, 200, b)
            self.assertEqual(ticked(), (["same"], ["same"]), edit)  # the unchanged one stays on
            self.assertEqual(list(self.app.policy_for(ag)["managed_mcp"]["mcpServers"]), ["same"])
            pol = json.loads(self.req("GET", "/api/policy", headers=self.auth())[2])
            tick()
        # edited by hand in config.json: never handed to the agent, and unticked by the next idle sweep
        new = json.loads(json.dumps(self.app.cfg()))
        new["mcp_servers"]["edited"] = {"type": "stdio", "command": "uvx", "args": ["two"]}
        C.save(new)
        self.app._cfg_mtime = None  # the file's time can match within one clock tick
        self.assertEqual(list(self.app.policy_for(ag)["managed_mcp"]["mcpServers"]), ["same"])
        self.app._sweep_mcp(self.app.cfg()["mcp_servers"])
        self.assertEqual(ticked(), (["same"], ["same"]))
        # and while the panel was off: what was last seen is kept on disk
        tick()
        new["mcp_servers"]["edited"] = {"type": "stdio", "command": "uvx", "args": ["three"]}
        C.save(new)
        self.app._cfg_mtime = None
        self.app._mcp_seen = self.app._load_mcp_seen({})
        self.app._sweep_mcp(self.app.cfg()["mcp_servers"])
        self.assertEqual(ticked(), (["same"], ["same"]))

    def test_devices_waiting_to_pair_are_in_the_owners_inbox_only(self):
        self.app.devices.starts.clear()  # a fresh pairing allowance for this test
        dev, _ = self.paired_device("Inbox phone")
        st, _, b = self.req("POST", "/api/pair/start", {"name": "Work laptop"})
        code = json.loads(b)["code"]
        st, _, b = self.req("GET", "/api/inbox", headers=self.auth())
        r = json.loads(b)
        mine = [it for it in r["items"] if it["kind"] == "device" and it["code"] == code]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["name"], mine[0]["agent"]), ("Work laptop", None))
        self.assertGreaterEqual(r["count"], 1)
        st, _, b = self.req("GET", "/api/inbox", headers=dev)
        self.assertEqual(st, 200)
        self.assertEqual([it for it in json.loads(b)["items"] if it["kind"] == "device"], [])
        self.assertNotIn(code, b.decode())
        for action in ("approve", "deny"):  # only the owner pairs devices
            st, _, _ = self.req("POST", f"/api/devices/pending/{mine[0]['id']}/{action}", {}, headers=dev)
            self.assertEqual(st, 403, action)
        st, _, b = self.req("GET", "/api/devices", headers=dev)
        self.assertEqual(st, 403)
        self.assertNotIn(code, b.decode())
        from cilly.server import device_may
        self.assertFalse(any(device_may(m, f"/api/devices/pending/{mine[0]['id']}/{a}") for m in ("GET", "POST") for a in ("approve", "deny")))
        st, _, _ = self.req("POST", f"/api/devices/pending/{mine[0]['id']}/deny", {}, headers=self.auth())
        self.assertEqual(st, 200)
        st, _, b = self.req("GET", "/api/inbox", headers=self.auth())
        self.assertEqual([it for it in json.loads(b)["items"] if it["kind"] == "device" and it["code"] == code], [])

    def test_never_reach_stays_addresses_only(self):
        s = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
        for bad in (["pastebin.com"], ["*.ngrok.io"], ["10.0.0.1 # home"], ["203.0.113.0/33"]):
            c = json.loads(json.dumps(s))
            c["egress"]["never_reach"] = bad
            st, _, b = self.req("PUT", "/api/settings", {"config": c}, headers=self.auth())
            self.assertEqual(st, 400, bad)
            self.assertIn("never_reach", json.loads(b)["error"])
        c = json.loads(json.dumps(s))
        c["egress"]["never_reach"] = ["203.0.113.7", "2001:db8:1234::/48"]
        st, _, b = self.req("PUT", "/api/settings", {"config": c}, headers=self.auth())
        self.assertEqual(st, 200, b)
        c["egress"]["never_reach"] = s["egress"]["never_reach"]
        self.req("PUT", "/api/settings", {"config": c}, headers=self.auth())

    def test_a_blocked_bubblewrap_stops_a_build_before_the_download(self):
        from unittest import mock
        blocked = {"why": "bwrap: setting up uid map: Permission denied", "kind": "apparmor", "fix": "", "cmd": ""}
        for tools, msg in (({"bwrap": None, "bwrap_problem": blocked, "use_bwrap": "auto"}, "can't build its sandbox"),
                           ({"bwrap": None, "bwrap_problem": None, "use_bwrap": "required"}, "'required' but bwrap is not installed")):
            with mock.patch.object(self.app.manager, "tools", return_value=tools), \
                    mock.patch("cilly.debian_image.fetch", side_effect=AssertionError("downloaded")):
                st, _, b = self.req("POST", "/api/images/build", {"preset": "full"}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn(msg, json.loads(b)["error"])
            self.assertNotEqual(self.app.build_state.get("state"), "running")

    def test_check_again_checks_a_failed_bubblewrap_again(self):
        from unittest import mock
        from cilly import host as HOST
        HOST._bwrap_cache.clear()
        self.addCleanup(HOST._bwrap_cache.clear)
        HOST._bwrap_cache["/usr/bin/bwrap"] = (time.monotonic(), {"why": "x", "kind": "error", "fix": "", "cmd": ""})
        HOST._bwrap_cache["/ok/bwrap"] = (time.monotonic(), None)
        st, _, _ = self.req("POST", "/api/host/check", {}, headers=self.auth())
        self.assertEqual(st, 200)
        self.assertEqual(list(HOST._bwrap_cache), ["/ok/bwrap"])  # a working one stays known

    def test_image_build_from_panel(self):
        from unittest import mock
        from cilly import vm as V
        from cilly.server import _BuildLog
        src = os.path.join(self.tmp.name, "debian.qcow2")
        open(src, "w").close()
        for body, msg in (({"preset": "huge", "source": src}, "preset must be"), ({"preset": "full", "source": "relative.qcow2"}, "not found"),
                          ({"preset": "full", "source": src + ".missing"}, "not found")):
            st, _, b = self.req("POST", "/api/images/build", body, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn(msg, json.loads(b)["error"])
        names = {"full": "base-20261014-110000.qcow2", "slim": "base-20261014-120000.qcow2"}
        gate = threading.Event()

        def fake_build(image, cfg, *, toolchains, preset, out, **kw):
            self.assertEqual(image, src)
            out.write("step 1\n\x1b[31mred\x1b[0m and \x07bell\npartial")
            gate.wait(10)
            open(os.path.join(V.images_dir(), names[preset]), "w").close()
            self.addCleanup(os.remove, os.path.join(V.images_dir(), names[preset]))
            return names[preset]
        with mock.patch("cilly.base_image.build", side_effect=fake_build):
            for preset, key in (("full", "base_image"), ("slim", "research_base_image")):
                gate.clear()
                st, _, b = self.req("POST", "/api/images/build", {"preset": preset, "source": src}, headers=self.auth())
                self.assertEqual((st, json.loads(b)["state"]), (200, "running"), b)
                st, _, b = self.req("POST", "/api/images/build", {"preset": preset, "source": src}, headers=self.auth())
                queued = json.loads(b)  # one at a time: another one waits its turn
                self.assertEqual([j["id"] for j in queued["queue"]], [queued["queued"]])
                st, _, b = self.req("POST", "/api/images/queue", {"remove": queued["queued"]}, headers=self.auth())
                self.assertEqual(json.loads(b)["queue"], [])
                gate.set()
                for _ in range(100):
                    s = json.loads(self.req("GET", "/api/images/build", headers=self.auth())[2])
                    if s["state"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual((s["state"], s["image"]), ("done", names[preset]))
                self.assertEqual(s["lines"], ["step 1", "red and bell", "partial"])  # no escape codes reach the page
                self.assertEqual(self.app.cfg()[key], names[preset])
        self.assertEqual(self.app.cfg()["build_source"], src)
        self.assertEqual(V.default_image(self.app.cfg(), "research"), names["slim"])
        self.assertEqual(V.default_image(self.app.cfg(), "coding"), names["full"])
        with mock.patch("cilly.base_image.build", side_effect=SystemExit("build timed out; logs in /x")):
            self.req("POST", "/api/images/build", {"preset": "full", "source": src}, headers=self.auth())
            for _ in range(100):
                s = json.loads(self.req("GET", "/api/images/build", headers=self.auth())[2])
                if s["state"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual((s["state"], s["error"]), ("failed", "build timed out; logs in /x"))
        self.assertEqual(self.app.cfg()["base_image"], names["full"])  # a failed build changes nothing
        log = _BuildLog()
        log.write("x" * 5000 + "\n" + "a line\n" * 500)
        self.assertEqual(len(log.tail(1000)), 400)
        self.assertEqual(len(_BuildLog().tail()), 0)

    def test_your_own_mix_of_toolchains(self):
        """New image → Custom: the toolchains you tick, with what they need, under a name you give.
        It doesn't replace a default: you make it one."""
        from unittest import mock
        from cilly import vm as V
        src = os.path.join(self.tmp.name, "debian.qcow2")
        open(src, "w").close()
        for body, msg in (({"preset": "custom", "source": src, "toolchains": ["nope"], "name": "X"}, "unknown toolchain"),
                          ({"preset": "custom", "source": src, "toolchains": ["java"], "name": "a\x07b"}, "one line"),
                          ({"preset": "custom", "source": src, "toolchains": ["java"], "name": "x" * 41}, "one line")):
            st, _, b = self.req("POST", "/api/images/build", body, headers=self.auth())
            self.assertEqual(st, 400, body)
            self.assertIn(msg, json.loads(b)["error"])
        got, before = {}, dict(self.app.cfg())

        def fake_build(image, cfg, *, toolchains, preset, out, label="", **kw):
            got.update(toolchains=toolchains, preset=preset, label=label)
            n = "base-20261018-010000.qcow2"
            open(os.path.join(V.images_dir(), n), "w").close()
            self.addCleanup(os.remove, os.path.join(V.images_dir(), n))
            return n
        with mock.patch("cilly.base_image.build", side_effect=fake_build):
            st, _, b = self.req("POST", "/api/images/build", {"preset": "custom", "source": src, "toolchains": ["ghidra", "decompilers"],
                                                              "name": "  Plugin   RE "}, headers=self.auth())
            self.assertEqual(st, 200, b)
            s = self.wait_build()
        self.assertEqual(s["state"], "done", s)
        self.assertEqual(got, {"toolchains": ["java", "ghidra", "decompilers"], "preset": "custom", "label": "Plugin RE"})
        for k in ("base_image", "research_base_image"):
            self.assertEqual(self.app.cfg()[k], before[k])  # no default changed
        r = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["recipes"]
        self.assertEqual((r["ghidra"]["needs"], r["ghidra"]["optional"], r["ghidra"]["size_mb"]), (["java"], True, 900))

    def test_image_builds_wait_in_a_queue(self):
        """Builds pressed while one runs wait their turn and run in order. One that fails doesn't stop the
        next, and how each ended is kept.  You can take one off the queue, and there's a limit."""
        from unittest import mock
        from cilly import vm as V
        from cilly.server import MAX_BUILD_QUEUE
        src = os.path.join(self.tmp.name, "debian.qcow2")
        open(src, "w").close()
        gate, ran = threading.Event(), []

        def fake_build(image, cfg, *, toolchains, preset, out, label="", **kw):
            ran.append(label or preset)
            if len(ran) == 1:
                gate.wait(10)
            if label == "Broken":
                raise SystemExit("the toolchain check failed")
            n = f"base-20261018-02000{len(ran)}.qcow2"
            open(os.path.join(V.images_dir(), n), "w").close()
            self.addCleanup(os.remove, os.path.join(V.images_dir(), n))
            return n
        post = lambda body: json.loads(self.req("POST", "/api/images/build", dict(body, source=src), headers=self.auth())[2])  # noqa: E731
        with mock.patch("cilly.base_image.build", side_effect=fake_build):
            self.assertEqual(post({"preset": "custom", "toolchains": ["java"], "name": "First"})["state"], "running")
            q = [post({"preset": "custom", "toolchains": ["java"], "name": n})["queued"] for n in ("Broken", "Gone", "Last")]
            s = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["build"]
            self.assertEqual([j["label"] for j in s["queue"]], ["Broken", "Gone", "Last"])
            self.assertEqual(self.req("POST", "/api/images/queue", {"remove": q[1]}, headers=self.auth())[0], 200)
            st, _, b = self.req("POST", "/api/images/queue", {"remove": q[1]}, headers=self.auth())
            self.assertIn("isn't waiting", json.loads(b)["error"])
            dev, _ = self.paired_device()  # building is for this PC
            self.assertEqual(self.req("POST", "/api/images/queue", {"remove": q[2]}, headers=dev)[0], 403)
            while len(self.app.build_queue) < MAX_BUILD_QUEUE:
                self.app.build_queue.append({"id": "x", "preset": "full", "label": "", "rebuild": None, "toolchains": [], "src": src})
            self.assertIn("are waiting already", post({"preset": "full"})["error"])
            del self.app.build_queue[2:]
            gate.set()
            for _ in range(200):
                s = json.loads(self.req("GET", "/api/images/build", headers=self.auth())[2])
                if s["state"] != "running" and not s["queue"]:
                    break
                time.sleep(0.05)
        self.assertEqual(ran, ["First", "Broken", "Last"])
        self.assertEqual([(r["label"], r["state"]) for r in s["results"][-3:]], [("First", "done"), ("Broken", "failed"), ("Last", "done")])
        self.assertEqual(s["results"][-2]["error"], "the toolchain check failed")  # still there after the next one ran

    def wait_build(self):
        for _ in range(100):
            s = json.loads(self.req("GET", "/api/images/build", headers=self.auth())[2])
            if s["state"] != "running":
                return s
            time.sleep(0.05)
        self.fail("the build didn't finish")

    def test_image_build_downloads_debian_when_no_file_is_given(self):
        from unittest import mock
        from cilly import vm as V
        got = os.path.join(self.tmp.name, "downloaded.qcow2")
        before = self.app.cfg()["base_image"]

        gate = threading.Event()

        def fake_fetch(out, progress, urlopen):
            self.assertTrue(callable(urlopen))  # straight out, or through the route for cilly's own downloads
            out.write("downloading https://cloud.debian.org/...\n")
            progress(180_000_000, 400_000_000)
            gate.wait(10)
            return got

        def fake_build(image, cfg, *, toolchains, preset, out, **kw):
            self.assertEqual(image, got)
            open(os.path.join(V.images_dir(), "base-20261016-010000.qcow2"), "w").close()
            self.addCleanup(os.remove, os.path.join(V.images_dir(), "base-20261016-010000.qcow2"))
            return "base-20261016-010000.qcow2"
        with mock.patch("cilly.debian_image.fetch", side_effect=fake_fetch) as fetch, \
                mock.patch("cilly.base_image.build", side_effect=fake_build):
            st, _, b = self.req("POST", "/api/images/build", {"preset": "full", "source": ""}, headers=self.auth())
            self.assertEqual(st, 200, b)
            for _ in range(100):  # the page shows how far the download is
                if json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["build"]["download"]:
                    break
                time.sleep(0.05)
            self.assertEqual(json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["build"]["download"], [180_000_000, 400_000_000])
            self.assertEqual(json.loads(self.req("GET", "/api/images/build", headers=self.auth())[2])["download"], [180_000_000, 400_000_000])
            gate.set()
            s = self.wait_build()
        self.assertIsNone(s["download"])  # done downloading
        self.assertEqual((s["state"], s["image"]), ("done", "base-20261016-010000.qcow2"))
        self.assertIn("downloading https://cloud.debian.org/...", s["lines"])
        fetch.assert_called_once()
        self.assertEqual(self.app.cfg()["build_source"], "")  # empty still means "download it" next time
        # a download that doesn't match Debian's sums fails the build, and says why
        with mock.patch("cilly.debian_image.fetch", side_effect=SystemExit("doesn't match Debian's SHA512SUMS")), \
                mock.patch("cilly.base_image.build", side_effect=AssertionError("never built")):
            self.req("POST", "/api/images/build", {"preset": "full"}, headers=self.auth())
            s = self.wait_build()
        self.assertEqual((s["state"], s["error"]), ("failed", "doesn't match Debian's SHA512SUMS"))
        st = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])
        self.assertEqual(st["build"]["error"], "doesn't match Debian's SHA512SUMS")
        self.app.set_cfg(dict(self.app.cfg(), base_image=before))

    def test_state_says_what_this_pc_needs(self):
        st = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])
        self.assertEqual(set(st["host"]), {"distro", "family", "unsupported", "cmds", "missing_cmd", "root_steps", "install_sh", "kvm", "cli"})
        self.assertIn("bwrap_problem", st["tools"])
        self.assertEqual(set(st["service"]), {"available", "enabled", "ours"})
        self.assertIn("build_source_ok", st)
        # a paired phone gets no paths on this PC
        from unittest import mock
        from cilly import host as H
        with mock.patch.object(H, "kvm_problem", return_value="no-access"):
            self.assertTrue(json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["host"]["install_sh"])
            dev, _ = self.paired_device("Host phone")
            st = json.loads(self.req("GET", "/api/state", headers=dev)[2])
        self.assertNotIn("install_sh", st["host"])
        self.assertNotIn("install.sh", json.dumps(st))

    def test_start_with_login_from_settings(self):
        from unittest import mock
        from cilly import launch as L
        with mock.patch.object(L, "install_service") as inst, mock.patch.object(L, "remove_service") as rm:
            st, _, b = self.req("POST", "/api/service", {"enabled": True}, headers=self.auth())
            self.assertEqual(st, 200, b)
            self.assertEqual(set(json.loads(b)), {"available", "enabled", "ours"})
            self.req("POST", "/api/service", {"enabled": False}, headers=self.auth())
            st, _, b = self.req("POST", "/api/service", {"enabled": "yes"}, headers=self.auth())
            self.assertEqual(st, 400)
            dev, _ = self.paired_device("Service phone")
            st, _, _ = self.req("POST", "/api/service", {"enabled": True}, headers=dev)
            self.assertEqual(st, 403)  # a paired phone can't make this PC run something at login
        self.assertEqual((inst.call_count, rm.call_count), (1, 1))
        with mock.patch.object(L, "install_service", side_effect=SystemExit("systemd isn't running your session")):
            st, _, b = self.req("POST", "/api/service", {"enabled": True}, headers=self.auth())
            self.assertEqual((st, json.loads(b)["error"]), (400, "systemd isn't running your session"))

    def test_save_on_this_pc_uses_the_keyring_without_systemd_256(self):
        from unittest import mock
        from tests.test_creds import FakeKeyring
        from cilly import creds
        ring, key = FakeKeyring(), "research:CLAUDE_CODE_OAUTH_TOKEN"
        with mock.patch.object(creds.subprocess, "run", ring), mock.patch.object(self.app, "_creds_ok", "secret-tool"):
            st, _, b = self.req("POST", "/api/secrets", {"key": key, "value": "sk-kr-1", "remember": True}, headers=self.auth())
            r = json.loads(b)
            self.assertEqual((st, r["note"], r["status"]), (200, "", "saved on this PC (loaded)"), b)
            ref = self.app.cfg()["research"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"]
            self.assertEqual(ref["command"][:2], ["secret-tool", "lookup"])
            self.assertEqual(ring.items, {key: "sk-kr-1"})
            self.req("POST", "/api/secrets", {"key": key, "forget": True}, headers=self.auth())
            self.assertEqual((self.app.cfg()["research"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], ring.items), ({}, {}))

    def test_agent_sites_list(self):
        ag = self.app.manager.create(name="s", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        ra = self.app.manager.create(name="r", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/", profile="research")
        try:
            text = "- https://api.example.com/v1\ncdn.example.net, *.googleapis.com # fonts\nfoo.com:8080\nbar.com:0\n*\n"
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/egress", {"text": text}, headers=self.auth())
            r = json.loads(b)
            self.assertEqual(st, 200, b)
            self.assertEqual(r["agent"]["egress_extra"], ["api.example.com", "cdn.example.net", "*.googleapis.com", "foo.com:8080"])
            self.assertEqual([w for w, _ in r["skipped"]], ["bar.com:0"])
            allow = self.app._allowlist(ag)
            self.assertIn("cdn.example.net", allow)
            self.assertIn("github.com", allow)  # the global list still applies
            self.assertNotIn("*", allow)
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/egress", {"text": "only.example.com"}, headers=self.auth())
            self.assertEqual(json.loads(b)["agent"]["egress_extra"], ["only.example.com"])  # replaces, not appends
            st, _, b = self.req("POST", f"/api/agents/{ra.id}/egress", {"text": "x.com"}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertFalse(self.app._to_scratch(ag, {"to_scratch": "yes"}))
            self.assertTrue(self.app._to_scratch(ag, {"to_scratch": True}))
            self.assertFalse(self.app._to_scratch(ra, {"to_scratch": True}))
            ag.set(repo="me/repo")
            self.assertFalse(self.app._to_scratch(ag, {"to_scratch": True}))
        finally:
            self.app.manager.delete(ag.id)
            self.app.manager.delete(ra.id)

    def test_rename_pin_project_archive(self):
        ag = self.app.manager.create(name="s", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        url = f"/api/agents/{ag.id}/organize"
        try:
            st, _, b = self.req("POST", url, {"name": "  pixel   art "}, headers=self.auth())
            self.assertEqual(json.loads(b)["name"], "pixel art")
            pid = self.app.api_project_create({"name": "Lantern"})["id"]
            for bad in ({"name": "<b>x</b>"}, {"name": "x" * 49}, {"project": "deadbeef"}, {}):
                st, _, _ = self.req("POST", url, bad, headers=self.auth())
                self.assertEqual(st, 400, bad)
            st, _, b = self.req("POST", url, {"pinned": True, "project": pid}, headers=self.auth())
            d = json.loads(b)
            self.assertEqual((d["pinned"], d["project"]), (True, pid))
            st, _, b = self.req("POST", url, {"project": None, "pinned": "yes"}, headers=self.auth())
            d = json.loads(b)
            self.assertEqual((d["pinned"], d["project"]), (False, None))  # only true pins
            st, _, b = self.req("POST", url, {"archived": True}, headers=self.auth())
            self.assertTrue(json.loads(b)["archived"])  # stopped: fine
            ag.meta["state"] = "ready"
            st, _, b = self.req("POST", url, {"archived": True}, headers=self.auth())
            self.assertIn("stop the agent", json.loads(b)["error"])
        finally:
            ag.meta["state"] = "stopped"
            self.app.manager.delete(ag.id)
            self.app.api_project_delete(pid)

    def test_injected_folder_is_remembered(self):
        from cilly.server import ApiError
        folder = os.path.join(self.tmp.name, "proj")
        os.makedirs(folder)
        with open(os.path.join(folder, "a.txt"), "w") as f:
            f.write("hi")
        ag = self.app.manager.create(name="p", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            st, _, b = self.req("POST", "/api/inject/scan", {"path": folder}, headers=self.auth())
            self.assertEqual(st, 200, b)
            m, mode, ts = self.app._inject_for_start(ag, {"manifest_id": json.loads(b)["id"], "mode": "copy", "to_scratch": True})
            self.assertEqual((m.root, mode, ts), (os.path.realpath(folder), "copy", True))
            self.assertEqual(ag.meta["inject_source"]["root"], os.path.realpath(folder))
            # next start (e.g. after the panel restarted): the same folder, scanned again
            with open(os.path.join(folder, "b.txt"), "w") as f:
                f.write("new file")
            m2, mode2, ts2 = self.app._inject_for_start(ag, None)
            self.assertEqual((m2.root, m2.file_count, mode2, ts2), (m.root, 2, "copy", True))
            # a new warning you haven't seen stops it
            with open(os.path.join(folder, ".env"), "w") as f:
                f.write("API_KEY=sk-live-123456789012345678901234")
            with self.assertRaisesRegex(ApiError, "new warning"):
                self.app._inject_for_start(ag, None)
            os.remove(os.path.join(folder, ".env"))
            shutil.rmtree(folder)
            with self.assertRaisesRegex(ApiError, "can't be used now"):
                self.app._inject_for_start(ag, None)
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/organize", {"forget_inject": True}, headers=self.auth())
            self.assertIsNone(json.loads(b)["inject_source"])
            self.assertEqual(self.app._inject_for_start(ag, None), (None, None, False))
        finally:
            self.app.manager.delete(ag.id)

    def test_a_host_folder_saved_without_starting(self):
        from cilly.server import ApiError
        folder, outside = os.path.join(self.tmp.name, "spec"), os.path.join(self.tmp.name, "secret.txt")
        os.makedirs(folder)
        with open(os.path.join(folder, "a.txt"), "w") as f:
            f.write("hi")
        with open(os.path.join(folder, ".env"), "w") as f:
            f.write("API_KEY=sk-live-123456789012345678901234")
        with open(outside, "w") as f:
            f.write("not for the VM")
        os.symlink(outside, os.path.join(folder, "leak"))  # points outside: never followed
        ag = self.app.manager.create(name="h", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        rs = self.app.manager.create(name="r", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/", profile="research")
        try:
            st, _, b = self.req("POST", "/api/inject/scan", {"path": folder}, headers=self.auth())
            scan = json.loads(b)
            self.assertTrue(scan["needs_ack"])
            self.assertNotIn("leak", [e["rel"] for e in scan["entries"]])
            body = {"manifest_id": scan["id"], "mode": "copy"}
            cfg = json.loads(json.dumps(self.app.cfg()))
            cfg["inject"]["allow_live_share"] = False
            self.app.set_cfg(cfg)
            dev, _ = self.paired_device("Folder")
            st, _, _ = self.req("POST", f"/api/agents/{ag.id}/inject", dict(body, ack_warnings=True), headers=dev)
            self.assertEqual(st, 403)  # host folders only from the PC
            for bad, why in [(body, "acknowledged"), ({"manifest_id": "nope", "mode": "copy"}, "scan the folder again"),
                             ({}, "scan the folder first"), (dict(body, ack_warnings=True, mode="share"), "live share"),
                             (dict(body, ack_warnings=True, mode="rw"), "copy or share")]:
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/inject", bad, headers=self.auth())
                self.assertEqual(st, 400, bad)
                self.assertIn(why, json.loads(b)["error"])
            st, _, b = self.req("POST", f"/api/agents/{rs.id}/inject", dict(body, ack_warnings=True), headers=self.auth())
            self.assertIn("research agents", json.loads(b)["error"])
            self.assertIsNone(ag.meta.get("inject_source"))
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/inject", dict(body, ack_warnings=True, to_scratch=True), headers=self.auth())
            self.assertEqual(st, 200, b)
            src = json.loads(b)["inject_source"]
            self.assertEqual((src["root"], src["mode"], src["to_scratch"], src["files"]), (os.path.realpath(folder), "copy", True, 2))
            # the next start scans it again: the same folder goes in read-only, without the link
            m, mode, ts = self.app._inject_for_start(ag, None)
            self.assertEqual((m.root, mode, ts), (os.path.realpath(folder), "copy", True))
            self.assertNotIn("leak", [e.rel for e in m.entries])
            # and a warning you haven't seen stops it
            with open(os.path.join(folder, "id_rsa"), "w") as f:
                f.write("-----BEGIN OPENSSH PRIVATE KEY-----\n")
            with self.assertRaisesRegex(ApiError, "new warning"):
                self.app._inject_for_start(ag, None)
        finally:
            cfg = json.loads(json.dumps(self.app.cfg()))
            cfg["inject"]["allow_live_share"] = True
            self.app.set_cfg(cfg)
            self.app.manager.delete(ag.id)
            self.app.manager.delete(rs.id)

    def test_export_and_attach_checks(self):
        ag = self.app.manager.create(name="s", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/export",
                                   {"what": "scratch", "dest": os.path.expanduser("~")}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("not a safe destination", json.loads(body)["error"])
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/attach-repo", {"repo": "someone/else"},
                                   headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("add the repository", json.loads(body)["error"])
            self.assertIsNone(self.app.manager.get(ag.id).meta["repo"])
        finally:
            self.app.manager.delete(ag.id)

    def test_keeping_the_outbox_when_it_stops(self):
        import tempfile
        dest = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, dest)
        ag = self.app.manager.create(name="e", mode="ephemeral", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=True, branch_prefix="agent/")
        kept = self.app.manager.create(name="p", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                       auto_stop=False, branch_prefix="agent/")
        self.addCleanup(self.app.manager.delete, ag.id)
        self.addCleanup(self.app.manager.delete, kept.id)
        url = f"/api/agents/{ag.id}/keep-outbox"
        for bad, why in ((os.path.expanduser("~"), "not a safe destination"), ("relative/path", "absolute folder"),
                         (dest + "/missing", "existing absolute folder")):
            st, _, b = self.req("POST", url, {"dest": bad}, headers=self.auth())
            self.assertEqual(st, 400, bad)
            self.assertIn(why, json.loads(b)["error"])
        st, _, b = self.req("POST", f"/api/agents/{kept.id}/keep-outbox", {"dest": dest}, headers=self.auth())
        self.assertIn("keeps its outbox on its disk", json.loads(b)["error"])  # its disk stays anyway
        st, _, b = self.req("POST", url, {"dest": dest}, headers=self.auth())
        self.assertEqual((st, json.loads(b)["keep_outbox"]), (200, dest))
        dev, _ = self.paired_device()  # a host folder: from this PC only
        st, _, _ = self.req("POST", url, {"dest": None}, headers=dev)
        self.assertEqual(st, 403)
        self.assertEqual(ag.meta["keep_outbox"], dest)
        st, _, b = self.req("POST", url, {"dest": None}, headers=self.auth())
        self.assertIsNone(json.loads(b)["keep_outbox"])

    def test_a_new_agent_keeps_its_outbox(self):
        """Set in New agent, or for every new agent in a project. A folder you give must work. The project's is
        skipped with a note when it doesn't. A paired device can't name a folder, but the project's still applies."""
        import shutil
        import tempfile
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, True)
        mgr, made = self.app.manager, []
        self.addCleanup(lambda: [mgr.delete(aid) for aid in made])

        def new(body, headers=None):
            with mock.patch.object(mgr, "start"):  # no VM here
                st, _, b = self.req("POST", "/api/agents", dict({"mode": "ephemeral"}, **body), headers=headers or self.auth())
            r = json.loads(b)
            if st == 200:
                made.append(r["id"])
            return st, r
        st, r = new({"name": "kept", "keep_outbox": dest})
        self.assertEqual((st, r["keep_outbox"]), (200, dest))
        for body, why in (({"mode": "persistent"}, "persistent one keeps it on its disk"),
                          ({"profile": "research"}, "persistent one keeps it on its disk"),
                          ({}, "not a safe destination")):
            st, r = new(dict({"name": "bad", "keep_outbox": dest if body else os.path.expanduser("~")}, **body))
            self.assertEqual(st, 400, body)
            self.assertIn(why, r["error"])
        dev, _ = self.paired_device("Phone")
        st, _ = new({"name": "phone", "keep_outbox": dest}, dev)
        self.assertEqual(st, 403)  # a folder on this PC: from the PC only
        # a project's folder
        pid = json.loads(self.req("POST", "/api/projects", {"name": "Outboxes"}, headers=self.auth())[2])["id"]
        self.addCleanup(self.app.projects.delete, pid)
        st, _, b = self.req("POST", f"/api/projects/{pid}", {"defaults": {"keep_outbox": dest + "/missing"}}, headers=self.auth())
        self.assertIn("existing absolute folder", json.loads(b)["error"])
        st, _, b = self.req("POST", f"/api/projects/{pid}", {"defaults": {"keep_outbox": dest}}, headers=self.auth())
        self.assertEqual(json.loads(b)["defaults"]["keep_outbox"], dest)
        self.assertEqual(new({"name": "in-project", "project": pid})[1]["keep_outbox"], dest)
        self.assertEqual(new({"name": "from-phone", "project": pid}, dev)[1]["keep_outbox"], dest)
        self.assertIsNone(new({"name": "turned-off", "project": pid, "keep_outbox": None})[1]["keep_outbox"])
        self.assertIsNone(new({"name": "persistent", "project": pid, "mode": "persistent"})[1]["keep_outbox"])
        os.rmdir(dest)  # the folder went away since
        st, r = new({"name": "gone", "project": pid})
        self.assertEqual((st, r["keep_outbox"]), (200, None))
        self.assertIn("project's folder can't be used", r["note"])

    def test_skills(self):
        """Your own skills: uploaded from this PC only, checked there, ticked per agent or by a project, and sent
        to a coding agent's VM with their hash. A phone may tick, never upload or remove."""
        import base64
        import io
        import zipfile
        mgr, made, store = self.app.manager, [], self.app.skills
        self.addCleanup(lambda: [mgr.delete(aid) for aid in made])
        self.addCleanup(lambda: [store.remove(n) for n in list(store.list())])
        post = lambda path, body, h=None: self.req("POST", path, body, headers=h or self.auth())  # noqa: E731
        b64 = lambda data: base64.b64encode(data).decode()  # noqa: E731
        md = b"---\nname: pixel-eye\ndescription: Draws eyes.\n---\nBody\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("pixel-eye/SKILL.md", md.replace(b"pixel-eye", b"zipped"))
            z.writestr("pixel-eye/scripts/run.sh", b"echo\n")
        for bad in ({"name": "x.txt", "data": b64(b"hello")}, {"name": "SKILL.md", "data": "not base64!"},
                    {"name": "SKILL.md", "data": b64(b"no frontmatter")}):
            st, _, b = post("/api/skills", bad)
            self.assertEqual(st, 400, b)
        st, _, b = post("/api/skills", {"name": "SKILL.md", "data": b64(md)})
        self.assertEqual((st, json.loads(b)["skill"]["name"], json.loads(b)["skill"]["replaced"]), (200, "pixel-eye", False), b)
        self.assertEqual(post("/api/skills", {"name": "zipped.zip", "data": b64(buf.getvalue())})[0], 200)
        state = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])
        self.assertEqual(state["skills"]["zipped"]["files"], 2)
        self.assertEqual(state["skills"]["pixel-eye"]["description"], "Draws eyes.")
        self.assertNotIn("sha256", state["skills"]["pixel-eye"])
        p = self.app.projects.create("Skill Project")
        self.addCleanup(self.app.projects.delete, p["id"])
        self.assertEqual(post(f"/api/projects/{p['id']}", {"defaults": {"skills": ["zipped", "nope"]}})[0], 200)
        self.assertEqual(self.app.projects.get(p["id"])["defaults"]["skills"], ["zipped"])
        with mock.patch.object(mgr, "start"):  # no VM here
            st, _, b = post("/api/agents", {"name": "sk", "mode": "persistent", "skills": ["pixel-eye"]})
            self.assertEqual(st, 200, b)
            ag = mgr.get(json.loads(b)["id"])
            made.append(ag.id)
            st, _, b = post("/api/agents", {"name": "sk2", "project": p["id"]})
            made.append(json.loads(b)["id"])
            self.assertEqual(mgr.get(made[-1]).meta["skills"], ["zipped"])  # the project's
            self.assertEqual(post("/api/agents", {"name": "x", "skills": ["nope"]})[0], 400)
            st, _, b = post("/api/agents", {"name": "res", "profile": "research", "skills": ["pixel-eye"]})
            made.append(json.loads(b)["id"])
            res = mgr.get(made[-1])
        self.assertEqual(ag.meta["skills"], ["pixel-eye"])
        self.assertEqual(res.meta["skills"], [])  # research agents never get them
        self.assertEqual(post(f"/api/agents/{res.id}/skills", {"skills": ["pixel-eye"]})[0], 400)
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
        self.addCleanup(self.app.secrets.forget, "agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
        payload = self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")
        self.assertEqual(payload["skills"], [{"name": "pixel-eye", "sha256": store.list()["pixel-eye"]["sha256"]}])
        with mock.patch.object(mgr, "send_skill_files") as send:  # a starting VM gets its skills before any task
            self.app.on_agent_ready(ag)
        self.assertEqual([f["name"] for f in send.call_args[0][1]], ["pixel-eye.tar"])
        # a phone: may tick and untick, never upload or remove
        dev, _ = self.paired_device("Skill phone")
        st, _, b = post(f"/api/agents/{ag.id}/skills", {"skills": ["pixel-eye", "zipped"]}, dev)
        self.assertEqual((st, json.loads(b)["skills"]), (200, ["pixel-eye", "zipped"]))
        self.assertEqual(post(f"/api/agents/{ag.id}/skills", {"skills": ["nope"]}, dev)[0], 400)
        for path, body in (("/api/skills", {"name": "SKILL.md", "data": b64(md)}), ("/api/skills/zipped/delete", {})):
            self.assertEqual(post(path, body, dev)[0], 403, path)
        # removing one turns it off everywhere
        self.assertEqual(post("/api/skills/zipped/delete", {})[0], 200)
        self.assertEqual(post("/api/skills/zipped/delete", {})[0], 404)
        self.assertEqual((ag.meta["skills"], mgr.get(made[1]).meta["skills"]), (["pixel-eye"], []))
        self.assertEqual(self.app.projects.get(p["id"])["defaults"]["skills"], [])

    def test_plugins(self):
        """A library of plugins, ticked per agent or by a project. The VM installs them from its own proxy, so
        their host joins the agent's allowlist. /plugin in the chat is cilly's. A phone may tick, never add."""
        import copy
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        mgr, made = self.app.manager, []
        self.addCleanup(lambda: [mgr.delete(aid) for aid in made])
        post = lambda path, body, h=None: self.req("POST", path, body, headers=h or self.auth())  # noqa: E731
        for bad in ({"name": "../x", "marketplace": "o/r"}, {"name": "x", "marketplace": "https://me:pw@git.example.org/r"},
                    {"name": "x", "marketplace": "git@github.com:o/r"}, {"name": "x", "marketplace": "o/r x"},
                    {"name": "x", "marketplace": "-o/r"}):  # an argument of claude plugin install, never an option
            self.assertEqual(post("/api/plugins", bad)[0], 400, bad)
        self.assertEqual(post("/api/plugins", {"name": "impeccable", "marketplace": "pbakaus/impeccable"})[0], 200)
        self.assertEqual(post("/api/plugins", {"name": "own", "marketplace": "https://git.example.org/me/plugins.git"})[0], 200)
        st, _, b = post("/api/plugins", {"name": "impeccable", "marketplace": "someone/else"})
        self.assertIn("already a plugin called impeccable", json.loads(b)["error"])
        state = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])
        self.assertEqual(state["plugins"]["impeccable"], {"marketplace": "pbakaus/impeccable", "accepted": False})
        with mock.patch.object(mgr, "start"):  # no VM here
            st, _, b = post("/api/agents", {"name": "plug", "mode": "persistent", "plugins": ["impeccable", "own"]})
            self.assertEqual(st, 200, b)
            ag = mgr.get(json.loads(b)["id"])
            made.append(ag.id)
            self.assertEqual(post("/api/agents", {"name": "x", "plugins": ["nope"]})[0], 400)
        self.assertEqual(ag.meta["plugins"], ["impeccable", "own"])
        allow = self.app._allowlist(ag)
        self.assertIn("git.example.org", allow)
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
        self.addCleanup(self.app.secrets.forget, "agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
        payload = self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")
        self.assertEqual(payload["plugins"], [{"name": "impeccable", "marketplace": "pbakaus/impeccable"},
                                              {"name": "own", "marketplace": "https://git.example.org/me/plugins.git"}])
        # an install command you allowed goes along by its sha256
        self.assertEqual(post("/api/plugins/own/accept", {"sha256": "nope"})[0], 400)
        self.assertEqual(post("/api/plugins/own/accept", {"sha256": "cd" * 32})[0], 200)
        self.assertEqual(self.app._agent_plugins(ag)[1]["accept"], "cd" * 32)
        # what the VM says about them: only its own plugins, known states, plain text
        self.app.manager._on_plugins(ag, {"type": "plugins", "results": [
            {"name": "impeccable", "state": "installed"}, {"name": "own", "state": "needs_command", "sha256": "ef" * 32,
                                                           "command": "npm i \x1b[31mx\nrm -rf /"},
            {"name": "other", "state": "installed"}, {"name": "impeccable", "state": "weird"}, "junk",
            {"name": "own", "state": "needs_command", "sha256": "not-hex"}]})
        ps = ag.meta["plugin_state"]
        self.assertEqual(sorted(ps), ["impeccable", "own"])
        self.assertEqual((ps["impeccable"]["state"], ps["own"]["sha256"]), ("installed", "ef" * 32))
        self.assertNotIn("\x1b", ps["own"]["command"])
        self.assertNotIn("\n", ps["own"]["command"])
        # /plugin in the chat: handled here, never a task
        say = lambda text, h=None: post(f"/api/agents/{ag.id}/message", {"instructions": text}, h)  # noqa: E731
        st, _, b = say("/plugins")
        self.assertEqual((st, json.loads(b)["plugin"]), (200, "On for plug: impeccable, own."))
        st, _, b = say("/plugin remove own")
        self.assertIn("own is off for plug", json.loads(b)["plugin"])
        self.assertEqual(ag.meta["plugins"], ["impeccable"])
        st, _, b = say("/plugin add anthropics/claude-plugins-official frontend-design")
        self.assertIn("frontend-design is on for plug", json.loads(b)["plugin"])
        self.assertEqual(self.app.cfg()["plugins"]["frontend-design"], {"marketplace": "anthropics/claude-plugins-official"})
        st, _, b = say("/plugin add someone/impeccable")  # the name taken by another marketplace
        self.assertIn("already a plugin called impeccable", json.loads(b)["error"])
        for bad, why in (("/plugin marketplace add pbakaus/impeccable", "in one go"), ("/plugin frobnicate", "Use /plugin add"),
                         ("/plugin remove nope", "doesn't have nope")):
            st, _, b = say(bad)
            self.assertIn(why, json.loads(b)["error"], bad)
        self.assertFalse([t for t in ag.meta.get("tasks", []) if t.get("instructions", "").startswith("/plugin")])
        # a phone: may tick and untick, and add from GitHub, never from another site (it would join the allowlist)
        dev, _ = self.paired_device()
        for bad in ("/plugin add https://git.example.org/me/thing.git", "/PLUGIN ADD -x/y", "/plugin add"):
            self.assertEqual(say(bad, dev)[0], 403, bad)
        st, _, b = say("/plugin add someone/thing", dev)
        self.assertEqual((st, self.app.cfg()["plugins"]["thing"]), (200, {"marketplace": "someone/thing"}), b)
        self.assertEqual(say("/plugin remove frontend-design", dev)[0], 200)
        self.assertEqual(post(f"/api/agents/{ag.id}/plugins", {"plugins": ["own"]}, dev)[0], 200)
        for path in ("/api/plugins", "/api/plugins/own/delete", "/api/plugins/own/accept"):
            self.assertEqual(post(path, {"name": "x", "marketplace": "o/x", "sha256": ""}, dev)[0], 403, path)
        # a project's defaults, and taking one out of the library
        pid = json.loads(post("/api/projects", {"name": "Plugged"})[2])["id"]
        self.addCleanup(self.app.projects.delete, pid)
        post(f"/api/projects/{pid}", {"defaults": {"plugins": ["impeccable", "nope"]}})
        self.assertEqual(self.app.projects.get(pid)["defaults"]["plugins"], ["impeccable"])
        with mock.patch.object(mgr, "start"):
            b = json.loads(post("/api/agents", {"name": "proj", "project": pid})[2])
            made.append(b["id"])
            r = json.loads(post("/api/agents", {"name": "research", "project": pid, "profile": "research"})[2])
            made.append(r["id"])
        self.assertEqual((b["plugins"], r["plugins"]), (["impeccable"], []))  # research agents take none
        self.assertEqual(post(f"/api/agents/{r['id']}/plugins", {"plugins": ["impeccable"]})[0], 400)
        self.assertEqual(post("/api/plugins/impeccable/delete", {})[0], 200)
        self.assertNotIn("impeccable", self.app.cfg()["plugins"])
        self.assertEqual((mgr.get(b["id"]).meta["plugins"], self.app.projects.get(pid)["defaults"]["plugins"]), ([], []))

    def test_uploads(self):
        import base64
        ag = self.app.manager.create(name="u", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        other = self.app.manager.create(name="o", mode="persistent", repo=None, agent="claude", cpus=1,
                                        memory_mb=1024, auto_stop=False, branch_prefix="agent/")
        try:
            def up(name, data, aid=ag.id):
                st, _, body = self.req("POST", f"/api/agents/{aid}/uploads",
                                       {"name": name, "data": base64.b64encode(data).decode()}, headers=self.auth())
                return st, json.loads(body)
            st, res = up("../../etc/passwd", b"x")
            self.assertEqual((st, res["name"]), (200, "passwd"))
            st, res = up("..", b"x")
            self.assertEqual(res["name"], "file")
            st, res = up("<script>.png", b"x")
            self.assertEqual(res["name"], "_script_.png")
            st, res = up("😀.png", b"x")
            self.assertEqual(res["name"], "_.png")
            st, res = up("bedrock-toast-handoff(1).md", b"x")  # how browsers name a second download
            self.assertEqual(res["name"], "bedrock-toast-handoff_1_.md")
            path = self.app.uploads[res["id"]]["path"]
            self.assertTrue(path.startswith(ag.paths.vm_dir + "/uploads/"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            st, res2 = up("big.bin", b"x" * (self.app.cfg()["attachments"]["max_file_bytes"] + 1))
            self.assertEqual(st, 400)
            self.assertIn("MiB", res2["error"])
            st, res3 = up("empty", b"")
            self.assertEqual(st, 400)
            # another agent's upload can't be attached here
            st, theirs = up("t.txt", b"x", other.id)
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/message",
                                   {"instructions": "hi", "attachments": [theirs["id"]]}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("Attach it again", json.loads(body)["error"])
        finally:
            self.app.manager.delete(ag.id)
            self.app.manager.delete(other.id)

    def test_a_file_alone_is_a_message(self):
        import base64
        ag = self.app.manager.create(name="f", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-x")
        self.addCleanup(lambda: (self.app.secrets.forget("agent:claude:CLAUDE_CODE_OAUTH_TOKEN"), self.app.manager.delete(ag.id)))
        sent = []

        def new_task(aid, payload, **kw):
            sent.append(payload)
            return {"id": "0123abcd"}

        def up(name):
            return json.loads(self.req("POST", f"/api/agents/{ag.id}/uploads", {"name": name, "data": base64.b64encode(b"x").decode()},
                                       headers=self.auth())[2])["id"]
        with mock.patch.object(self.app.manager, "new_task", new_task):
            for ids, first in (([up("shot.png")], "Have a look at the attached file."),
                               ([up("a.png"), up("b.png")], "Have a look at the attached files.")):
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/message", {"instructions": "  ", "attachments": ids}, headers=self.auth())
                self.assertEqual(st, 200, b)
                text = sent[-1]["instructions"]
                self.assertTrue(text.startswith(first + "\n\nThe user attached these files"), text)
                self.assertIn("/attachments/", text)
            # no text and no file is still nothing to send
            st, _, b = self.req("POST", f"/api/agents/{ag.id}/message", {"instructions": ""}, headers=self.auth())
            self.assertEqual((st, json.loads(b)["error"]), (400, "Write some instructions first."))
        self.assertEqual(len(sent), 2)

    def test_transcribe_endpoint(self):
        import base64
        from tests.test_speech import FakeWhisper, make_wav
        from http.server import ThreadingHTTPServer
        st, _, body = self.req("POST", "/api/transcribe", {"audio": ""}, headers=self.auth())
        self.assertEqual(st, 400)
        self.assertIn("Voice input is off", json.loads(body)["error"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWhisper)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cfg = self.app.cfg()
        cfg["speech"]["whisper_url"] = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with mock.patch.object(self.app, "cfg", lambda: cfg):
                ok = base64.b64encode(make_wav()).decode()
                st, _, body = self.req("POST", "/api/transcribe", {"audio": ok}, headers=self.auth())
                self.assertEqual((st, json.loads(body)), (200, {"text": "add a button"}))
                st, _, body = self.req("POST", "/api/transcribe",
                                       {"audio": base64.b64encode(make_wav(rate=44100)).decode()}, headers=self.auth())
                self.assertEqual(st, 502)
                self.assertIn("16 kHz", json.loads(body)["error"])
                st, _, _ = self.req("POST", "/api/transcribe", {"audio": ok})  # no token
                self.assertEqual(st, 401)
                st, hdrs, _ = self.req("GET", "/")
                self.assertIn("microphone=(self)", hdrs.get("Permissions-Policy", ""))
                self.assertIn("camera=()", hdrs.get("Permissions-Policy", ""))
        finally:
            cfg["speech"]["whisper_url"] = ""
            srv.shutdown()
            srv.server_close()

    def test_speech_test_and_policy_version(self):
        from tests.test_speech import FakeWhisper
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWhisper)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            st, _, body = self.req("POST", "/api/speech/test", {"url": url}, headers=self.auth())
            self.assertTrue(json.loads(body)["ok"], body)
            st, _, body = self.req("POST", "/api/speech/test", {"url": "http://127.0.0.1:1"}, headers=self.auth())
            self.assertFalse(json.loads(body)["ok"])
            st, _, body = self.req("POST", "/api/speech/test", {"url": "file:///etc/passwd"}, headers=self.auth())
            self.assertEqual(st, 400)
        finally:
            srv.shutdown()
            srv.server_close()
        v = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["policy_version"]
        pol = json.loads(self.req("GET", "/api/policy", headers=self.auth())[2])
        st, _, body = self.req("PUT", "/api/policy", {"policy": pol["policy"]}, headers=self.auth())
        self.assertEqual((st, json.loads(body)["policy_version"]), (200, v + 1))
        st, hdrs, _ = self.req("GET", "/fonts/Geist-Variable.woff2")
        self.assertEqual((st, hdrs["Content-Type"]), (200, "font/woff2"))
        self.assertIn("font-src 'self'", hdrs["Content-Security-Policy"])

    def test_setup_state_and_steps(self):
        st = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["setup"]
        self.assertFalse(st["done"])
        self.assertEqual((st["claude"], st["token"]), ("missing", "bot"))
        st, _, body = self.req("POST", "/api/setup", {"step": "network"}, headers=self.auth())
        self.assertEqual(json.loads(body)["steps"], ["network"])
        st, _, body = self.req("POST", "/api/setup", {"step": "rm -rf"}, headers=self.auth())
        self.assertEqual(st, 400)
        self.req("POST", "/api/setup", {"done": True}, headers=self.auth())
        su = json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["setup"]
        self.assertTrue(su["done"])
        self.assertNotIn("value", json.dumps(su))
        self.req("POST", "/api/setup", {"done": False}, headers=self.auth())
        self.app.secrets.forget("token:bot")  # no real GitHub call: without the token the UI is asked for it
        st, _, body = self.req("POST", "/api/tokens/repos", {"name": "bot"}, headers=self.auth())
        self.assertEqual((st, json.loads(body).get("needs_secret")), (400, "token:bot"))  # the UI asks for it

    def test_agent_model(self):
        ag = self.app.manager.create(name="m", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            for bad in ("--dangerously-skip-permissions", "Opus", "a b", "x" * 80):
                st, _, _ = self.req("POST", f"/api/agents/{ag.id}/model", {"model": bad}, headers=self.auth())
                self.assertEqual(st, 400, bad)
            st, _, body = self.req("POST", f"/api/agents/{ag.id}/model", {"model": "sonnet"}, headers=self.auth())
            self.assertEqual((st, json.loads(body)["model"]), (200, "sonnet"))
            self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-x")
            payload = self.app._task_payload(ag, "hello", new_branch=False, cont=True, pr_title="")
            self.assertEqual(payload["model"], "sonnet")
            self.req("POST", f"/api/agents/{ag.id}/model", {"model": None}, headers=self.auth())
            self.assertNotIn("model", self.app._task_payload(ag, "hello", new_branch=False, cont=True, pr_title=""))
        finally:
            self.app.secrets.forget("agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
            self.app.manager.delete(ag.id)

    def test_unique_attachment_names(self):
        from cilly.server import _unique_names
        self.assertEqual(_unique_names(["a.png", "a.png", "A.png", "b"]), ["a.png", "a-2.png", "A-3.png", "b"])

    def test_create_rejects_bad_mode_and_name(self):
        st, _, _ = self.req("POST", "/api/agents", {"name": "x", "mode": "forever"}, headers=self.auth())
        self.assertEqual(st, 400)
        st, _, _ = self.req("POST", "/api/agents", {"name": "<script>", "mode": "ephemeral"}, headers=self.auth())
        self.assertEqual(st, 400)

    def test_policy_roundtrip_and_validation(self):
        st, _, body = self.req("GET", "/api/policy", headers=self.auth())
        pol = json.loads(body)["policy"]
        self.assertEqual(pol["permission_mode"], "bypassPermissions")
        # the recommended rules and their switches, for setup and Claude policy
        from cilly import config as C
        got = json.loads(body)
        self.assertEqual(got["claude_md_recommended"], C.CLAUDE_MD_RECOMMENDED)
        self.assertEqual([o["key"] for o in got["claude_md_options"]], ["no_dashes", "ascii", "no_emojis"])
        self.assertEqual((pol["claude_md"], pol["dash_check"], pol["keep_plan"]), ("", False, False))
        pol["claude_md"] = "Use tabs."
        pol["dash_check"] = pol["keep_plan"] = True
        pol["permission_mode"] = "dontAsk"
        st, _, body = self.req("PUT", "/api/policy", {"policy": pol}, headers=self.auth())
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["applied_to_running_agents"], 0)
        st, _, body = self.req("GET", "/api/policy", headers=self.auth())
        self.assertEqual(json.loads(body)["policy"]["claude_md"], "Use tabs.")
        self.assertTrue(json.loads(body)["policy"]["dash_check"])
        self.assertTrue(json.loads(body)["policy"]["keep_plan"])
        pol["permission_mode"] = "yolo"
        self.assertEqual(self.req("PUT", "/api/policy", {"policy": pol}, headers=self.auth())[0], 400)
        pol["permission_mode"] = "dontAsk"
        pol["managed_settings"] = ["not", "an", "object"]
        self.assertEqual(self.req("PUT", "/api/policy", {"policy": pol}, headers=self.auth())[0], 400)

    def test_unknown_agent_actions(self):
        self.assertEqual(self.req("POST", "/api/agents/deadbeef/stop", {}, headers=self.auth())[0], 400)
        self.assertEqual(self.req("POST", "/api/agents/deadbeef/explode", {}, headers=self.auth())[0], 404)
        self.assertEqual(self.req("POST", "/api/agents/../../x/stop", {}, headers=self.auth())[0], 404)

    def test_settings_never_store_secret_values(self):
        st, _, body = self.req("GET", "/api/settings", headers=self.auth())
        cfg = json.loads(body)["config"]
        cfg["tokens"]["bot"]["secret"] = {"value": "ghp_xxx"}
        st, _, body = self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())
        self.assertEqual(st, 400)

    def test_ui_cannot_change_host_commands(self):
        st, _, body = self.req("GET", "/api/settings", headers=self.auth())
        cfg = json.loads(body)["config"]
        cfg["panel_token"]["secret"] = {"command": ["sh", "-c", "touch /tmp/pwned"]}
        st, _, body = self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())
        self.assertEqual(st, 400)
        cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
        cfg["vm"]["qemu_binary"] = "/tmp/evil"
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())[0], 400)
        cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
        for mode in ("off", "required"):  # the sandbox around QEMU: config file only, either way
            cfg["vm"]["use_bwrap"] = mode
            st, _, b = self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())
            self.assertEqual(st, 400, mode)
            self.assertIn("vm.use_bwrap", json.loads(b)["error"])
        self.assertEqual(self.app.cfg()["vm"]["use_bwrap"], "auto")
        cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
        cfg["egress"]["allow"].append("example.org")
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())[0], 200)

    def test_state_says_which_ports_the_open_web_gets(self):
        st, _, b = self.req("GET", "/api/state", headers=self.auth())
        self.assertEqual(json.loads(b)["open_web_ports"], [443])  # 80 only when egress.ports has it

    def test_never_reach_from_settings(self):
        ag = self.app.manager.create(name="n", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        try:
            cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
            self.assertEqual(cfg["egress"]["never_reach"], [])
            cfg["egress"]["never_reach"] = ["my-home.example"]
            st, _, b = self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("never_reach", json.loads(b)["error"])
            cfg["egress"]["never_reach"] = ["203.0.113.7", "2001:db8:1234::/48"]
            self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())[0], 200)
            with open(ag.paths.never_file) as f:  # what the agent's proxy re-reads
                self.assertEqual(f.read(), "203.0.113.7\n2001:db8:1234::/48\n")
            # a paired device can't change it (or read Settings at all)
            dev, _ = self.paired_device("Tablet")
            cfg["egress"]["never_reach"] = []
            st, _, b = self.req("PUT", "/api/settings", {"config": cfg}, headers=dev)
            self.assertEqual(st, 403)
            self.assertEqual(self.app.cfg()["egress"]["never_reach"], ["203.0.113.7", "2001:db8:1234::/48"])
        finally:
            self.app.manager.delete(ag.id)
            cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
            cfg["egress"]["never_reach"] = []
            self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())

    def test_verify_access_says_why_in_plain_words(self):
        from cilly import github as GH
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["tokens"]["vt"] = {"description": "", "repos": ["me/ok", "me/ro", "me/gone", "me/expired", "me/net"], "secret": {}}
        self.app.set_cfg(cfg)

        def perms(repo, tok):
            if repo == "me/gone":
                raise GH.GitHubError("GitHub API 404: Not Found")
            if repo == "me/expired":
                raise GH.GitHubError("GitHub API 401: Bad credentials")
            if repo == "me/net":
                raise GH.GitHubError("network error: timed out")
            return {"full_name": repo, "private": True, "permissions": {"push": repo == "me/ok", "pull": True}}
        try:
            st, _, b = self.req("POST", "/api/tokens/verify", {"name": "vt"}, headers=self.auth())
            self.assertEqual(st, 400)
            self.assertIn("The token can't be read", json.loads(b)["error"])
            self.app.secrets.set_pasted("token:vt", "ghp_test")
            with mock.patch.object(GH, "repo_permissions", perms):
                st, _, b = self.req("POST", "/api/tokens/verify", {"name": "vt"}, headers=self.auth())
            got = {r["full_name"]: (r["ok"], r["reason"]) for r in json.loads(b)["repos"]}
            self.assertEqual(got["me/ok"], (True, ""))
            self.assertIn("No push access", got["me/ro"][1])
            self.assertIn("Not found", got["me/gone"][1])
            self.assertIn("refused the token", got["me/expired"][1])
            self.assertIn("couldn't be reached", got["me/net"][1])
        finally:
            cfg = json.loads(json.dumps(self.app.cfg()))
            del cfg["tokens"]["vt"]
            self.app.set_cfg(cfg)

    def test_which_repos_suit_attach_repository(self):
        from cilly import github as GH
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["tokens"]["at"] = {"description": "", "repos": ["me/fresh", "me/big", "me/ro", "me/bare", "me/gone", "me/odd"], "secret": {}}
        cfg["tokens"]["locked"] = {"description": "", "repos": ["me/locked"], "secret": {}}
        self.app.set_cfg(cfg)
        info = {"me/fresh": (True, 1), "me/big": (True, 214), "me/ro": (False, 1), "me/bare": (True, 0)}

        def overview(repo, tok):
            self.assertEqual(tok, "ghp_at")
            if repo == "me/gone":
                raise GH.GitHubError("GitHub API 404: Not Found")
            if repo == "me/odd":
                raise GH.GitHubError("unexpected reply")
            push, n = info[repo]
            return {"push": push, "commits": n, "created_at": "2026-10-14T05:00:00Z", "pushed_at": "2026-10-15T05:00:00Z"}
        try:
            self.app.secrets.set_pasted("token:at", "ghp_at")
            dev, _ = self.paired_device("Attacher")
            with mock.patch.object(GH, "repo_overview", overview):
                st, _, _ = self.req("GET", "/api/repos/attachable", headers=dev)
                self.assertEqual(st, 403)  # a phone attaches from the plain list; asking GitHub is for the PC
                st, _, b = self.req("GET", "/api/repos/attachable", headers=self.auth())
            self.assertEqual(st, 200)
            got = {r["repo"]: (r["ok"], r["reason"]) for r in json.loads(b)["repos"]}
            self.assertEqual(got["me/fresh"], (True, "Empty, has a README"))
            self.assertEqual(got["me/big"], (False, "Has 214 commits. Pick an empty one"))
            self.assertEqual(got["me/ro"], (False, "Read only for the token"))
            self.assertFalse(got["me/bare"][0])
            self.assertEqual(got["me/gone"][0], True)
            self.assertIn("Not found", got["me/gone"][1])
            self.assertEqual(got["me/locked"], (True, "Not checked: the token isn't unlocked yet"))
            self.assertEqual(got["me/odd"], (True, "Not checked. GitHub sent an unexpected reply."))
        finally:
            cfg = json.loads(json.dumps(self.app.cfg()))
            del cfg["tokens"]["at"], cfg["tokens"]["locked"]
            self.app.set_cfg(cfg)

    def test_attachable_repos_are_kept_for_a_minute(self):
        from cilly import github as GH
        from cilly import server as S
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["tokens"]["ac"] = {"description": "", "repos": ["me/one", "me/flaky"], "secret": {}}
        self.app.set_cfg(cfg)
        asked = []

        def overview(repo, tok):
            asked.append(repo)
            if repo == "me/flaky":
                raise GH.GitHubError("network error: down")
            return {"push": True, "commits": 1, "created_at": "2026-10-14T05:00:00Z", "pushed_at": ""}

        def reasons():
            st, _, b = self.req("GET", "/api/repos/attachable", headers=self.auth())
            self.assertEqual(st, 200)
            return {r["repo"]: r["reason"] for r in json.loads(b)["repos"] if r["repo"] in ("me/one", "me/flaky")}
        try:
            self.app._attachable.clear()
            self.app.secrets.set_pasted("token:ac", "ghp_cached")
            with mock.patch.object(GH, "repo_overview", overview):
                first = reasons()
                self.assertEqual(reasons(), first)
                self.assertEqual(sorted(asked), ["me/flaky", "me/flaky", "me/one"])  # an error is asked again
                self.assertNotIn("ghp_cached", repr(self.app._attachable))
                self.assertEqual({k for k in self.app._attachable if k[0].startswith("me/")}, {("me/one", "ac")})
                with mock.patch.object(S, "ATTACHABLE_TTL", 0):
                    reasons()
                self.assertEqual(asked.count("me/one"), 2)
                ag = self.app.manager.create(name="s", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                             auto_stop=False, branch_prefix="agent/")
                st, _, b = self.req("POST", f"/api/agents/{ag.id}/attach-repo", {"repo": "me/one"}, headers=self.auth())
                self.assertIn("Start the agent first", json.loads(b)["error"])
                self.assertEqual(self.app._attachable, {})  # it may have commits soon: asked again next time
                self.app.manager.delete(ag.id)
        finally:
            self.app._attachable.clear()
            cfg = json.loads(json.dumps(self.app.cfg()))
            del cfg["tokens"]["ac"]
            self.app.set_cfg(cfg)

    def test_ui_can_add_token_with_pasted_secret(self):
        cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
        cfg["tokens"]["second"] = {"description": "x", "repos": ["me/other"], "secret": {}}
        st, _, body = self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())
        self.assertEqual(st, 200, body)
        cfg = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
        del cfg["tokens"]["second"]
        self.assertEqual(self.req("PUT", "/api/settings", {"config": cfg}, headers=self.auth())[0], 200)

    def test_negative_content_length(self):
        st, _, _ = self.req("POST", "/api/inject/scan", None,
                            headers={**self.auth(), "Content-Type": "application/json", "Content-Length": "-1"})
        self.assertEqual(st, 413)

    def test_command_secret_runs_once_and_is_cached(self):
        from cilly.secretstore import SecretStore
        counter = os.path.join(self.tmp.name, "count")
        cmd = ["sh", "-c", f"echo x >> {counter}; echo tok-from-pass"]
        st = SecretStore()
        self.assertEqual(st.status("k", {"command": cmd}), "command (not loaded yet)")
        for _ in range(3):
            self.assertEqual(st.get("k", {"command": cmd}), "tok-from-pass")
        with open(counter) as f:
            self.assertEqual(f.read().count("x"), 1)  # one prompt per panel start, not per task
        self.assertEqual(st.status("k", {"command": cmd}), "command (loaded)")
        st.forget("k")
        st.get("k", {"command": cmd})
        with open(counter) as f:
            self.assertEqual(f.read().count("x"), 2)

    def test_failing_command_says_why_and_pasted_value_takes_over(self):
        from cilly.secretstore import SecretError, SecretStore
        cmd = ["sh", "-c", "echo 'Error: claude/research is not in the password store.' >&2; exit 1"]
        st = SecretStore()
        with self.assertRaises(SecretError) as cm:
            st.get("research:T", {"command": cmd})
        self.assertIn("is not in the password store", str(cm.exception))
        self.assertIn("exited with 1", str(cm.exception))
        st.set_pasted("research:T", "pasted-tok")
        self.assertEqual(st.get("research:T", {"command": cmd}), "pasted-tok")
        self.assertEqual(st.status("research:T", {"command": cmd}), "pasted, used instead of the command")

    def test_a_new_value_replaces_the_loaded_one(self):
        from cilly.secretstore import SecretStore
        cmd = ["sh", "-c", "echo old-token"]
        st = SecretStore()
        self.assertEqual(st.get("token:bot", {"command": cmd}), "old-token")
        st.set_pasted("token:bot", "new-token")  # replaced, or saved again on this PC
        self.assertEqual(st.get("token:bot", {"command": cmd}), "new-token")
        self.assertEqual(st.status("token:bot", {"command": cmd}), "pasted, used instead of the command")
        st.forget("token:bot")
        self.assertEqual(st.get("token:bot", {"command": cmd}), "old-token")

    def test_settings_say_when_a_secret_was_set_never_its_value(self):
        from cilly.secretstore import SecretStore
        st = SecretStore()
        self.assertEqual(st.times("k"), {"pasted": None, "read": None})
        t0 = time.time()
        st.set_pasted("k", "tok-pasted")
        st.get("c", {"command": ["sh", "-c", "echo tok-cmd"]})
        self.assertGreaterEqual(st.times("k")["pasted"], t0)
        self.assertGreaterEqual(st.times("c")["read"], t0)
        st.forget("k")
        self.assertIsNone(st.times("k")["pasted"])
        key = "mcp:TIMES_TEST_KEY"
        self.app.secrets.set_pasted(key, "s3cr3t-value-xyz")
        new = json.loads(json.dumps(self.app.cfg()))
        new["mcp_servers"]["timed"] = {"type": "http", "url": "https://t.example/mcp", "headers": {"Authorization": "Bearer ${TIMES_TEST_KEY}"}}
        self.app.set_cfg(new)
        try:
            st_, _, b = self.req("GET", "/api/settings", headers=self.auth())
            r = json.loads(b)
            self.assertGreaterEqual(r["secret_times"][key]["pasted"], t0)
            self.assertIsNone(r["secret_times"][key]["saved"])
            self.assertNotIn("s3cr3t", b.decode())  # not the value, not a part of it
        finally:
            self.app.secrets.forget(key)
            del new["mcp_servers"]["timed"]
            self.app.set_cfg(new)

    def test_failing_command_offers_paste(self):
        from cilly.server import ApiError
        ref = {"command": ["sh", "-c", "echo nope >&2; exit 1"]}
        with self.assertRaises(ApiError) as cm:
            self.app._secret("research:CLAUDE_CODE_OAUTH_TOKEN", ref, "the key")
        self.assertEqual(cm.exception.extra.get("needs_secret"), "research:CLAUDE_CODE_OAUTH_TOKEN")
        self.assertIn("nope", str(cm.exception))

    def test_save_on_this_pc_from_the_ui(self):
        from unittest import mock
        from tests.test_creds import FakeCreds
        from cilly import creds
        fake = FakeCreds()
        key = "agent:claude:CLAUDE_CODE_OAUTH_TOKEN"
        with mock.patch.object(creds.subprocess, "run", fake), mock.patch.object(self.app, "_creds_ok", True):
            st, _, b = self.req("POST", "/api/secrets", {"key": key, "value": "sk-ui-123", "remember": True}, headers=self.auth())
            r = json.loads(b)
            self.assertEqual((st, r["note"]), (200, ""), b)
            ref = self.app.cfg()["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"]
            self.assertTrue(creds.is_ours(ref, key))  # the config points at its own decrypt command
            self.assertNotIn("sk-ui-123", json.dumps(self.app.cfg()))
            got = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])
            self.assertAlmostEqual(got["secret_times"][key]["saved"], os.stat(ref["command"][4]).st_mtime)  # when it was saved
            # a Settings page loaded before that still saves fine, and keeps the saved key
            s = json.loads(self.req("GET", "/api/settings", headers=self.auth())[2])["config"]
            s["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"] = {}
            st, _, b = self.req("PUT", "/api/settings", {"config": s}, headers=self.auth())
            self.assertEqual(st, 200, b)
            self.assertTrue(creds.is_ours(self.app.cfg()["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], key))
            # ...but it can't write a command of its own
            s["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"] = {"command": ["sh", "-c", "evil"]}
            st, _, _ = self.req("PUT", "/api/settings", {"config": s}, headers=self.auth())
            self.assertEqual(st, 400)
            # forget deletes the file and the reference
            st, _, b = self.req("POST", "/api/secrets", {"key": key, "forget": True}, headers=self.auth())
            self.assertEqual(self.app.cfg()["agents"]["claude"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], {})
            self.assertFalse(os.path.exists(ref["command"][4]))
        with mock.patch.object(self.app, "_creds_ok", False):  # no systemd-creds 256+: still works for this session
            st, _, b = self.req("POST", "/api/secrets", {"key": key, "value": "sk-ui-123", "remember": True}, headers=self.auth())
            r = json.loads(b)
            self.assertEqual((r["status"], "systemd 256" in r["note"]), ("pasted", True))
        self.app.secrets.forget(key)

    def test_saving_secrets_is_checked_again_after_installing_secret_tool(self):
        from cilly import creds
        from cilly import server as S
        with mock.patch.object(creds, "backend", return_value=None) as check:
            self.app._creds_ok, self.app._creds_at = None, 0.0
            self.assertFalse(self.app.creds_available())
            self.assertFalse(self.app.creds_available())  # not every state poll
            self.assertEqual(check.call_count, 1)
            check.return_value = "secret-tool"  # sudo apt install libsecret-tools
            self.app._creds_at -= S.CREDS_RETRY + 1
            self.assertEqual(self.app.creds_backend(), "secret-tool")
            self.app._creds_at -= S.CREDS_RETRY + 1
            check.return_value = None
            self.assertEqual(self.app.creds_backend(), "secret-tool")  # a found one is kept
        self.app._creds_ok = None

    def test_route_keys_saved_on_this_pc(self):
        """Remember on a VPN's keys or a proxy's password: saved like any other secret."""
        import base64
        import copy
        from tests.test_creds import FakeCreds
        from cilly import creds
        from cilly import wireguard as W
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: (self.app.set_cfg(old), self.app.wg.stop()))
        p_ = mock.patch.object(W, "wireproxy_binary", lambda: None)
        p_.start()
        self.addCleanup(p_.stop)
        priv, pub = (base64.b64encode(bytes([n]) * 32).decode() for n in (7, 8))
        conf = (f"[Interface]\nPrivateKey = {priv}\nAddress = 10.8.0.2/32\n\n"
                f"[Peer]\nPublicKey = {pub}\nEndpoint = 185.107.56.1:51820\nAllowedIPs = 0.0.0.0/0\n")
        cfg = copy.deepcopy(old)
        cfg["egress"].setdefault("routes", {})["myproxy"] = {"kind": "socks5", "host": "proxy.example.org", "port": 1080, "user": "me"}
        self.app.set_cfg(cfg)
        with mock.patch.object(creds.subprocess, "run", FakeCreds()), mock.patch.object(self.app, "_creds_ok", True):
            st, _, b = self.req("POST", "/api/routes/wireguard", {"name": "serbiaivpn", "conf": conf, "remember": True},
                                headers=self.auth())
            self.assertEqual((st, json.loads(b)["note"]), (200, ""), b)
            self.assertTrue(creds.is_ours(self.app.cfg()["egress"]["routes"]["serbiaivpn"]["secret"], "route:serbiaivpn"))
            st, _, b = self.req("POST", "/api/secrets", {"key": "route:myproxy", "value": "pw-123", "remember": True},
                                headers=self.auth())
            self.assertEqual((st, json.loads(b)["note"]), (200, ""), b)
            self.assertTrue(creds.is_ours(self.app.cfg()["egress"]["routes"]["myproxy"]["secret"], "route:myproxy"))
            self.assertNotIn(priv, json.dumps(self.app.cfg()))
            st, _, b = self.req("POST", "/api/secrets", {"key": "route:serbiaivpn", "forget": True}, headers=self.auth())
            self.assertEqual(st, 200, b)
            self.assertEqual(self.app.cfg()["egress"]["routes"]["serbiaivpn"]["secret"], {})
        for k in ("route:serbiaivpn", "route:myproxy"):
            self.app.secrets.forget(k)

    def test_a_project_defaults_to_a_local_model(self):
        """New agents in a project can start on a model you run. A server that's gone falls back to the default."""
        import copy
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["model_servers"] = {"gpu": {"url": "http://192.168.0.91:11434"}}
        self.app.set_cfg(cfg)
        mgr, made = self.app.manager, []
        self.addCleanup(lambda: [mgr.delete(aid) for aid in made])
        pid = json.loads(self.req("POST", "/api/projects", {"name": "Local"}, headers=self.auth())[2])["id"]
        self.addCleanup(self.app.projects.delete, pid)
        url = f"/api/projects/{pid}"
        for bad in ({"model": "qwen3:14b", "model_server": "nope"}, {"model": "bad name", "model_server": "gpu"}):
            self.assertEqual(self.req("POST", url, {"defaults": bad}, headers=self.auth())[0], 400, bad)
        st, _, b = self.req("POST", url, {"defaults": {"effort": "high"}}, headers=self.auth())
        st, _, b = self.req("POST", url, {"defaults": {"model": "qwen3.6:35b-a3b-q4_K_M", "model_server": "gpu"}}, headers=self.auth())
        d = json.loads(b)["defaults"]
        self.assertEqual((st, d["model"], d["model_server"]), (200, "qwen3.6:35b-a3b-q4_K_M", "gpu"))  # a ":" in its name is kept

        def new(name):
            with mock.patch.object(mgr, "start"):  # no VM here
                st, _, b = self.req("POST", "/api/agents", {"name": name, "project": pid}, headers=self.auth())
            self.assertEqual(st, 200, b)
            made.append(json.loads(b)["id"])
            return mgr.get(made[-1]).meta
        m = new("on-gpu")
        self.assertEqual((m["model"], m["model_server"], m.get("effort")), ("qwen3.6:35b-a3b-q4_K_M", "gpu", "high"))  # its effort too
        self.app.set_cfg(old)  # the server was taken out of Settings
        m = new("gpu-gone")
        self.assertEqual((m["model"], m["model_server"]), (None, None))
        st, _, b = self.req("POST", url, {"defaults": {"model": "opus"}}, headers=self.auth())
        d = json.loads(b)["defaults"]
        self.assertEqual((d["model"], d["model_server"]), ("opus", None))
        m = new("on-opus")
        self.assertEqual((m["model"], m["model_server"], m["effort"]), ("opus", None, "high"))

    def test_an_agent_on_a_local_model(self):
        """Claude Code pointed at a model server you run: no Claude login needed or sent, the key stays
        on this PC, and only while the agent's model is on that server."""
        import copy
        from tests.test_models import FakeOllama
        from cilly import creds
        from cilly.server import ApiError
        srv = FakeOllama()
        self.addCleanup(srv.close)
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["model_servers"] = {"gpu": {"url": srv.url, "api_key": True, "secret": {}}}
        self.app.set_cfg(cfg)
        self.assertIn("model:gpu", self.app.secret_refs())
        self.assertEqual(set(self.app.secret_refs()) - set(creds._slots(copy.deepcopy(self.app.cfg()))), set())  # all can be saved
        st, _, b = self.req("POST", "/api/secrets", {"key": "model:gpu", "value": "sk-local-123"}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.addCleanup(self.app.secrets.forget, "model:gpu")
        ag = self.app.manager.create(name="local", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        self.assertEqual(self.app._model_hosts(ag), ["gpu.model.cilly.internal"])
        for bad in ({"model": "qwen3-coder:30b", "server": "nope"}, {"model": "bad name", "server": "gpu"}):
            self.assertEqual(self.req("POST", f"/api/agents/{ag.id}/model", bad, headers=self.auth())[0], 400, bad)
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/model", {"model": "qwen3-coder:30b", "server": "gpu", "effort": "max"},
                            headers=self.auth())
        self.assertEqual(st, 200, b)
        self.assertEqual((ag.meta["model"], ag.meta["model_server"], ag.meta.get("effort")), ("qwen3-coder:30b", "gpu", "max"))
        self.app.secrets.forget("agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
        p = self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")  # no Claude login: not needed
        e = p["agent_env"]
        self.assertEqual((e["ANTHROPIC_BASE_URL"], e["ANTHROPIC_MODEL"], e["ANTHROPIC_DEFAULT_HAIKU_MODEL"]),
                         ("https://gpu.model.cilly.internal", "qwen3-coder:30b", "qwen3-coder:30b"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", p["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", p["env"])
        self.assertNotIn("sk-local-123", json.dumps({k: v for k, v in p.items() if k != "proxy_models"}))  # to the agent's proxy only
        self.assertEqual((p.get("model"), p.get("effort"), p["local_model"]), (None, "max", "gpu/qwen3-coder:30b"))  # --effort
        model = lambda body: (self.req("POST", f"/api/agents/{ag.id}/model", body, headers=self.auth())[0], ag.meta.get("effort"))  # noqa: E731
        self.assertEqual(model({"model": "qwen3:8b", "server": "gpu"}), (200, "max"))  # another of yours: kept
        self.assertEqual(model({"effort": "medium"}), (200, "medium"))  # on its own, as for Claude's
        self.assertEqual(model({"effort": "turbo"})[0], 400)
        self.assertEqual(model({"model": "opus"}), (200, None))  # picked for your model, maybe not right for Claude's
        model({"effort": "xhigh"})
        self.assertEqual(model({"model": "qwen3-coder:30b", "server": "gpu"}), (200, None))  # nor the other way round
        # FakeOllama has it loaded with 4,096 tokens: Claude Code is told, rather than assuming 200k
        self.assertEqual((e["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], e["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], p["context_window"]),
                         ("4096", "4096", 4096))
        entry = {"gpu.model.cilly.internal": {"url": srv.url, "key": "sk-local-123", "model": "qwen3-coder:30b", "source": srv.url}}
        self.assertEqual(p["proxy_models"], entry)  # for the agent's proxy, when the task is sent
        self.assertEqual(ag.models, {})  # queueing a message doesn't change what a running task can reach
        self.app.secrets.set_pasted("model:gpu", "has space")
        with self.assertRaises(ApiError) as cm:
            self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")
        self.assertIn("spaces or line breaks", str(cm.exception))
        self.app.secrets.set_pasted("model:gpu", "sk-local-123")
        # running without this start's certificate for it: refused, never anything less safe
        ag.procs["qemu"] = mock.Mock(poll=lambda: None)
        self.addCleanup(ag.procs.pop, "qemu", None)
        ag.tls = {"ca": "x", "hosts": {"api.anthropic.com": {}}, "tokens": True}
        with self.assertRaises(ApiError) as cm:
            self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")
        self.assertIn("started before gpu was added", str(cm.exception))
        ag.tls = None
        ag.procs.pop("qemu")
        # back to a Claude model: the server can't be reached any more
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-local-test")
        self.addCleanup(self.app.secrets.forget, "agent:claude:CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(self.req("POST", f"/api/agents/{ag.id}/model", {"model": "sonnet"}, headers=self.auth())[0], 200)
        self.assertIsNone(ag.meta["model_server"])
        p = self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")
        self.assertNotIn("ANTHROPIC_BASE_URL", p["agent_env"])
        self.assertEqual((p["model"], p["proxy_models"]), ("sonnet", {}))
        # the model list, also for a paired phone's picker, and the test from Settings
        got = json.loads(self.req("GET", "/api/models", headers=self.auth())[2])
        self.assertEqual(got["servers"]["gpu"], {"models": ["gpt-oss:20b", "qwen3-coder:30b"], "error": None,
                                                 "small": {"qwen3-coder:30b": 4096}, "host": "127.0.0.1", "ssh": None})  # Ollama's loaded one: too little
        dev, info = self.paired_device("Model phone")
        self.addCleanup(self.app.devices.revoke, info["id"])
        self.assertEqual(self.req("GET", "/api/models", headers=dev)[0], 200)
        self.assertEqual(self.req("POST", "/api/models/test", {"server": "gpu"}, headers=dev)[0], 403)  # from the PC only
        res = json.loads(self.req("POST", "/api/models/test", {"server": "gpu", "model": "qwen3-coder:30b"}, headers=self.auth())[2])
        self.assertEqual((res["ok"], res["text"], res["model"]), (True, "OK", "qwen3-coder:30b"))
        self.assertIn("OLLAMA_CONTEXT_LENGTH", res["warning"])
        self.assertTrue(all(a == "Bearer sk-local-123" for _, _, a in srv.seen))  # the key went along every time
        self.assertEqual(json.loads(self.req("GET", "/api/state", headers=self.auth())[2])["model_servers"], ["gpu"])
        # a server taken out of Settings: its agents are told to pick another model
        self.req("POST", f"/api/agents/{ag.id}/model", {"model": "qwen3-coder:30b", "server": "gpu"}, headers=self.auth())
        self.app.manager.set_models(ag, entry)  # a task on it is running
        changed = copy.deepcopy(self.app.cfg())
        changed["model_servers"]["gpu"]["url"] = "http://192.168.1.77:11434"  # another address: not the one it was allowed
        self.app.set_cfg(changed)
        self.assertEqual(ag.models, {})
        self.app.manager.set_models(ag, dict(entry, **{"gpu.model.cilly.internal": dict(entry["gpu.model.cilly.internal"],
                                                                                         url=changed["model_servers"]["gpu"]["url"])}))
        self.app.set_cfg(old)
        self.assertEqual(ag.models, {})  # taken out of Settings: out of reach at once
        with self.assertRaises(ApiError) as cm:
            self.app._task_payload(ag, "hi", new_branch=False, cont=False, pr_title="")
        self.assertIn("isn't in Settings any more", str(cm.exception))

    def test_a_research_agent_on_a_local_model(self):
        """Research agents can use your models too: pointed at the server through their proxy, and
        without their Claude login."""
        import copy
        from tests.test_models import FakeOllama
        srv = FakeOllama(context=131072)
        self.addCleanup(srv.close)
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["model_servers"] = {"gpu": {"url": srv.url, "api_key": False, "secret": {}}}
        self.app.set_cfg(cfg)
        ag = self.app.manager.create(name="local-research", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/", profile="research")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        self.assertEqual(self.app._model_hosts(ag), ["gpu.model.cilly.internal"])  # its start gets the certificate
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/model", {"model": "qwen3-coder:30b", "server": "gpu"}, headers=self.auth())
        self.assertEqual(st, 200, b)
        self.app.secrets.forget("research:CLAUDE_CODE_OAUTH_TOKEN")
        p = self.app._task_payload(ag, "find out", new_branch=False, cont=False, pr_title="")  # no research login: not needed
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", p["env"])
        self.assertEqual((p["agent_env"]["ANTHROPIC_BASE_URL"], p["agent_env"]["ANTHROPIC_MODEL"]),
                         ("https://gpu.model.cilly.internal", "qwen3-coder:30b"))
        self.assertEqual(p["proxy_models"]["gpu.model.cilly.internal"]["url"], srv.url)
        self.assertNotIn("model", p)  # not passed to claude --model as if it were one of Claude's
        self.assertTrue(p["instructions"].startswith(self.app.cfg()["research"]["preamble"]))
        self.app.secrets.set_pasted("research:CLAUDE_CODE_OAUTH_TOKEN", "sk-oat-research")
        self.addCleanup(self.app.secrets.forget, "research:CLAUDE_CODE_OAUTH_TOKEN")
        self.req("POST", f"/api/agents/{ag.id}/model", {"model": "sonnet"}, headers=self.auth())
        p = self.app._task_payload(ag, "find out", new_branch=False, cont=False, pr_title="")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", p["env"])  # back on Claude's: its login (a stand-in, with tokens kept here)
        self.assertEqual(p["proxy_models"], {})

    def test_model_servers_are_saved_at_once(self):
        """Settings → Local models saves each change, so agents' model menus see a server right away."""
        import copy
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        a = self.auth()
        st, _, b = self.req("POST", "/api/models/servers", {"name": "gpu", "url": "http://192.168.1.50:11434/"}, headers=a)
        self.assertEqual(st, 200, b)
        self.assertEqual(json.loads(b)["servers"]["gpu"], {"url": "http://192.168.1.50:11434", "api_key": False, "secret": {}})
        self.assertEqual(json.loads(self.req("GET", "/api/state", headers=a)[2])["model_servers"], ["gpu"])
        cfg = copy.deepcopy(self.app.cfg())
        cfg["model_servers"]["gpu"].update(api_key=True, secret={"command": ["true"]})  # a key saved from the config file
        self.app.set_cfg(cfg)
        st, _, b = self.req("POST", "/api/models/servers", {"name": "gpu", "url": "http://192.168.1.50:11434", "api_key": True}, headers=a)
        self.assertEqual(json.loads(b)["servers"]["gpu"]["secret"], {"command": ["true"]})  # kept when it changes
        for bad in ({"name": "GPU", "url": "http://x:1"}, {"name": "gpu", "url": "ftp://x"}, {"name": "gpu", "url": "http://127.0.0.1:8765"},
                    {"name": "nope", "remove": True}):
            self.assertEqual(self.req("POST", "/api/models/servers", bad, headers=a)[0], 400, bad)
        dev, info = self.paired_device("Servers phone")
        self.addCleanup(self.app.devices.revoke, info["id"])
        self.assertEqual(self.req("POST", "/api/models/servers", {"name": "x", "url": "http://x:1"}, headers=dev)[0], 403)
        st, _, b = self.req("POST", "/api/models/servers", {"name": "gpu", "remove": True}, headers=a)
        self.assertEqual((st, json.loads(b)["servers"]), (200, {}))
        self.assertNotIn("gpu", self.app.cfg()["model_servers"])

    def test_the_web_ui_cant_add_route_or_model_server_commands(self):
        """A route's password and a model server's API key can come from a command, like a token: only
        from config.json.  One saved on this PC by cilly itself may still be removed with its route."""
        import copy
        from cilly import creds
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        a = self.auth()
        c = copy.deepcopy(old)
        c["egress"].setdefault("routes", {})["corp"] = {"kind": "socks5", "host": "10.0.0.1", "port": 1080, "user": "me",
                                                        "secret": {"command": ["sh", "-c", "id"]}}
        st, _, b = self.req("PUT", "/api/settings", {"config": c}, headers=a)
        self.assertEqual(st, 400, b)
        self.assertIn(b"editing", b)
        c = copy.deepcopy(old)
        c["model_servers"] = {"gpu": {"url": "http://10.0.0.2:11434", "api_key": True, "secret": {"command": ["id"]}}}
        self.assertEqual(self.req("PUT", "/api/settings", {"config": c}, headers=a)[0], 400)
        self.assertNotIn("gpu", self.app.cfg().get("model_servers", {}))
        saved = copy.deepcopy(old)  # saved on this PC from the panel: cilly's own decrypt of its own file
        saved["egress"].setdefault("routes", {})["corp"] = {"kind": "socks5", "host": "10.0.0.1", "port": 1080, "user": "me",
                                                            "secret": {"command": creds.command_for("route:corp")}}
        self.app.set_cfg(saved)
        c = copy.deepcopy(saved)
        del c["egress"]["routes"]["corp"]
        st, _, b = self.req("PUT", "/api/settings", {"config": c}, headers=a)
        self.assertEqual(st, 200, b)
        self.assertNotIn("corp", self.app.cfg()["egress"]["routes"])

    def test_a_model_server_over_ssh(self):
        """cilly makes the key, you confirm the host key, and the panel and agents reach the server
        through the tunnel on this PC's loopback.  Removing the server takes the key and the tunnel."""
        import copy
        from tests.test_models import FakeOllama
        from tests.test_sshtunnel import FAKE_BIN, HOST_KEY
        from cilly import sshtunnel as ST
        srv = FakeOllama()
        self.addCleanup(srv.close)
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        cfg = copy.deepcopy(old)
        cfg["vm"]["use_bwrap"] = "off"
        self.app.set_cfg(cfg)
        env = mock.patch.dict(os.environ, {"PATH": FAKE_BIN + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.app.ssh.stop)
        a, port = self.auth(), int(srv.url.rsplit(":", 1)[1])
        body = {"name": "gpu", "url": srv.url, "ssh": {"host": "gpu.example.net", "port": 22, "user": "ubuntu", "route": "direct"}}
        st, _, b = self.req("POST", "/api/models/servers", body, headers=a)
        self.assertEqual(st, 200, b)
        got = json.loads(b)
        s = self.app.cfg()["model_servers"]["gpu"]
        self.assertTrue(s["ssh"]["public_key"].endswith(" cilly-gpu"))  # a key just for this server
        self.assertNotIn("host_key", s["ssh"])
        self.assertEqual(got["ssh"]["gpu"]["authorized"], f'restrict,port-forwarding,permitopen="127.0.0.1:{port}" '
                                                          + s["ssh"]["public_key"])
        self.assertNotIn("authorized", got["servers"]["gpu"]["ssh"])  # shown, never saved back with the settings
        private = self.app.secrets.get("model-ssh:gpu", s.get("ssh_secret", {}))
        self.assertNotIn(private.encode(), b)
        self.assertNotIn(private, json.dumps(self.app.cfg()))
        listed = json.loads(self.req("GET", "/api/models?refresh=1", headers=a)[2])["servers"]["gpu"]
        self.assertIn("Confirm the server's host key first", listed["error"])
        dev, info = self.paired_device("SSH phone")
        self.addCleanup(self.app.devices.revoke, info["id"])
        self.assertEqual(self.req("POST", "/api/models/ssh", {"name": "gpu", "action": "fetch"}, headers=dev)[0], 403)
        st, _, b = self.req("POST", "/api/models/ssh", {"name": "gpu", "action": "fetch"}, headers=a)
        fetched = json.loads(b)
        self.assertEqual((st, fetched["ok"], fetched["fingerprint"]), (200, True, ST.fingerprint(HOST_KEY)))
        st, _, b = self.req("POST", "/api/models/ssh", {"name": "gpu", "action": "confirm", "fingerprint": "SHA256:other"}, headers=a)
        self.assertEqual(st, 400, b)  # only the key it fetched
        st, _, b = self.req("POST", "/api/models/ssh", {"name": "gpu", "action": "confirm", "fingerprint": fetched["fingerprint"]},
                            headers=a)
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.cfg()["model_servers"]["gpu"]["ssh"]["host_key"], HOST_KEY)
        listed = json.loads(self.req("GET", "/api/models?refresh=1", headers=a)[2])["servers"]["gpu"]
        self.assertEqual((listed["models"], listed["error"], listed["host"]), (["gpt-oss:20b", "qwen3-coder:30b"], None, "gpu.example.net"))
        self.assertTrue(listed["ssh"]["tunnel"]["ready"])
        tunnel = self.app.ssh.procs["gpu"]
        local = {"name": "gpu", "host": "gpu.model.cilly.internal", "model": "qwen3-coder:30b", "server": self.app.cfg()["model_servers"]["gpu"]}
        entry = self.app._model_entry(mock.Mock(running=False), local)["gpu.model.cilly.internal"]
        self.assertEqual((entry["url"], entry["tunnel"], entry["source"]), (f"http://127.0.0.1:{tunnel.port}", True, srv.url))
        # the web UI can't make the panel run a command for these keys
        for where in (("model_servers", "gpu", "ssh_secret"), ("model_servers", "gpu", "secret")):
            c = copy.deepcopy(self.app.cfg())
            c[where[0]][where[1]][where[2]] = {"command": ["sh", "-c", "touch /tmp/pwned"]}
            if where[2] == "secret":
                c["model_servers"]["gpu"]["api_key"] = True
            st, _, b = self.req("PUT", "/api/settings", {"config": c}, headers=a)
            self.assertEqual(st, 400, (where, b))
        c = copy.deepcopy(self.app.cfg())
        c["egress"].setdefault("routes", {})["corp"] = {"kind": "socks5", "host": "10.0.0.1", "port": 1080, "user": "me",
                                                        "secret": {"command": ["sh", "-c", "id"]}}
        self.assertEqual(self.req("PUT", "/api/settings", {"config": c}, headers=a)[0], 400)
        ag = self.app.manager.create(name="tunneled", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        self.app.manager.set_models(ag, {"gpu.model.cilly.internal": entry})  # a task on it is running
        # the same server at another address: the confirmed host key goes, the key stays
        body["ssh"]["host"] = "gpu2.example.net"
        self.assertEqual(self.req("POST", "/api/models/servers", body, headers=a)[0], 200)
        s2 = self.app.cfg()["model_servers"]["gpu"]
        self.assertNotIn("host_key", s2["ssh"])
        self.assertEqual(s2["ssh"]["public_key"], s["ssh"]["public_key"])
        self.assertNotIn("gpu", self.app.ssh.procs)  # its tunnel stopped
        self.assertEqual(ag.models, {})  # and the agent forgot its port, which another tunnel may take next
        self.assertNotIn(tunnel.port, [t.port for t in self.app.ssh.procs.values()])
        st, _, b = self.req("POST", "/api/models/servers", {"name": "gpu", "remove": True}, headers=a)
        self.assertEqual(st, 200, b)
        self.assertNotIn("model-ssh:gpu", self.app.secret_refs())
        self.assertFalse(self.app.secrets.has_pasted("model-ssh:gpu"))

    def test_a_refused_forward_explains_a_failed_request(self):
        """Through a tunnel, a request fails as a closed connection when the server refused the forward.
        ssh says why a moment later, and Settings shows that instead."""
        from cilly import models as MS
        why = "nothing is listening on 127.0.0.1:18000 on the server. Start the model server there, or fix Address on that server."
        fake = mock.Mock()
        fake.forward_error.side_effect = [None, None, why]  # ssh's line comes in just after the failure
        over_ssh = {"url": "http://127.0.0.1:18000", "ssh": {"host": "gpu.example.net", "port": 22, "user": "root", "route": "direct"}}
        with mock.patch.dict(self.app.ssh.procs, {"gpu": fake}):
            got = self.app._tunnel_why("gpu", over_ssh, MS.ModelServerError("127.0.0.1:51455 closed the connection"))
            self.assertEqual(got, f"the SSH tunnel: {why}")
            fake.forward_error.side_effect = None
            fake.forward_error.return_value = None  # no refused forward: the request's own error stands
            self.assertEqual(self.app._tunnel_why("gpu", over_ssh, MS.ModelServerError("it timed out")), "it timed out")
            self.assertEqual(self.app._tunnel_why("gpu", {"url": "http://10.0.0.5:11434"}, MS.ModelServerError("down")), "down")

    def test_leaving_global_sites_out_for_one_agent(self):
        """Settings' allowlist holds defaults: one agent can go without some of them, from the start or
        later, live.  Nothing else changes: its project's and its own sites stay."""
        a = self.auth()
        no_vm = mock.patch.object(self.app.manager, "start")  # no VM here
        no_vm.start()
        self.addCleanup(no_vm.stop)
        allow = self.app.cfg()["egress"]["allow"]
        self.assertIn("pypi.org", allow)
        st, _, b = self.req("POST", "/api/agents", {"name": "lean", "mode": "ephemeral", "skip_global": ["pypi.org", "nope.example", 5]},
                            headers=a)
        self.assertEqual(st, 400, b)  # not a list of sites: nothing is made
        self.assertFalse(any(x.meta["name"] == "lean" for x in self.app.manager.agents.values()))
        st, _, b = self.req("POST", "/api/agents", {"name": "lean", "mode": "ephemeral", "skip_global": ["pypi.org", "nope.example"]},
                            headers=a)
        self.assertEqual(st, 200, b)
        ag = self.app.manager.get(json.loads(b)["id"])
        self.addCleanup(lambda: self.app.manager.delete(ag.id))
        self.assertEqual(ag.meta["egress_skip"], ["pypi.org"])  # only sites that are on the global list
        self.assertNotIn("pypi.org", self.app._allowlist(ag))
        self.assertIn("github.com", self.app._allowlist(ag))
        dev, info = self.paired_device("Lean phone")  # it only narrows what an agent reaches
        self.addCleanup(self.app.devices.revoke, info["id"])
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/global-sites", {"skip": ["pypi.org", "registry.npmjs.org"]}, headers=dev)
        self.assertEqual(st, 200, b)
        self.assertNotIn("registry.npmjs.org", self.app._allowlist(ag))  # live
        with open(ag.paths.allow_file) as f:  # what its proxy reads
            written = f.read().split()
        self.assertNotIn("registry.npmjs.org", written)
        self.assertNotIn("pypi.org", written)
        self.assertIn("github.com", written)
        ag.set(egress_extra=["registry.npmjs.org"])  # its own sites still count
        self.assertIn("registry.npmjs.org", self.app._allowlist(ag))
        ag.set(egress_extra=[])
        st, _, b = self.req("POST", f"/api/agents/{ag.id}/allow", {"host": "pypi.org", "scope": "agent"}, headers=a)
        self.assertEqual(st, 200, b)
        self.assertEqual((ag.meta["egress_skip"], ag.meta["egress_extra"]), (["registry.npmjs.org"], []))  # back on, not an extra
        self.assertIn("pypi.org", self.app._allowlist(ag))
        self.assertEqual(self.req("POST", f"/api/agents/{ag.id}/global-sites", {"skip": "pypi.org"}, headers=a)[0], 400)
        st, _, b = self.req("POST", "/api/agents", {"name": "lean-r", "profile": "research", "skip_global": ["pypi.org"]}, headers=a)
        r = self.app.manager.get(json.loads(b)["id"])
        self.addCleanup(lambda: self.app.manager.delete(r.id))
        self.assertEqual(r.meta["egress_skip"], [])  # research agents reach the open web anyway
        self.assertEqual(self.req("POST", f"/api/agents/{r.id}/global-sites", {"skip": ["pypi.org"]}, headers=a)[0], 400)
        # a project's default for its new agents, which New agent can still change
        pid = json.loads(self.req("POST", "/api/projects", {"name": "Lean"}, headers=a)[2])["id"]
        self.addCleanup(lambda: self.app.projects.delete(pid))
        st, _, b = self.req("POST", f"/api/projects/{pid}", {"defaults": {"skip_global": ["deb.debian.org", "evil.example"]}}, headers=a)
        self.assertEqual(st, 200, b)
        self.assertEqual(self.app.projects.get(pid)["defaults"]["skip_global"], ["deb.debian.org"])
        st, _, b = self.req("POST", "/api/agents", {"name": "lean-p", "project": pid}, headers=a)
        pa = self.app.manager.get(json.loads(b)["id"])
        self.addCleanup(lambda: self.app.manager.delete(pa.id))
        self.assertEqual(pa.meta["egress_skip"], ["deb.debian.org"])
        st, _, b = self.req("POST", "/api/agents", {"name": "lean-q", "project": pid, "skip_global": []}, headers=a)
        qa = self.app.manager.get(json.loads(b)["id"])
        self.addCleanup(lambda: self.app.manager.delete(qa.id))
        self.assertEqual(qa.meta["egress_skip"], [])  # New agent checked them all again

    def test_the_bots_commit_identity_from_its_token(self):
        """Settings asks GitHub who the bot's token belongs to, for the agents' commits: its handle and
        noreply address.  The setup wizard saves it only over the made-up default."""
        import copy
        from cilly import github as GH
        old = copy.deepcopy(self.app.cfg())
        self.addCleanup(lambda: self.app.set_cfg(old))
        self.app.secrets.set_pasted("token:bot", "ghp_x")
        self.addCleanup(self.app.secrets.forget, "token:bot")
        a, asked = self.auth(), []
        reply = {"login": "cillybox", "id": 123456789}
        with mock.patch.object(GH, "api_get", side_effect=lambda path, tok, **kw: asked.append((path, tok)) or reply):
            self.assertTrue(C.placeholder_identity(self.app.cfg()))
            st, _, b = self.req("POST", "/api/identity/lookup", {}, headers=a)
            self.assertEqual(json.loads(b), {"name": "cillybox", "email": "123456789+cillybox@users.noreply.github.com", "saved": False})
            self.assertEqual(asked, [("/user", "ghp_x")])
            self.assertTrue(C.placeholder_identity(self.app.cfg()))  # only shown: Save settings keeps it
            st, _, b = self.req("POST", "/api/identity/lookup", {"save": True}, headers=a)
            self.assertTrue(json.loads(b)["saved"])
            self.assertEqual(self.app.cfg()["tokens"]["bot"]["identity"],  # the token's own account
                             {"name": "cillybox", "email": "123456789+cillybox@users.noreply.github.com"})
            self.assertEqual(self.app.cfg()["identity"]["name"], old["identity"]["name"])
            reply = {"login": "someone-else", "id": 42}
            st, _, b = self.req("POST", "/api/identity/lookup", {"save": True}, headers=a)
            self.assertFalse(json.loads(b)["saved"])  # one you set is never replaced
            self.assertEqual(C.account_identity(self.app.cfg(), "bot")["name"], "cillybox")
            for bad in ({"login": "bad name", "id": 1}, {"login": "x", "id": "1"}, {"login": "x", "id": 0}, ["x"]):
                reply = bad
                self.assertEqual(self.req("POST", "/api/identity/lookup", {}, headers=a)[0], 502, bad)
            dev, info = self.paired_device("Identity phone")
            self.addCleanup(self.app.devices.revoke, info["id"])
            self.assertEqual(self.req("POST", "/api/identity/lookup", {"save": True}, headers=dev)[0], 403)

    def test_pasted_secret_not_echoed(self):
        st, _, body = self.req("POST", "/api/secrets", {"key": "token:bot", "value": "ghp_secretvalue"},
                               headers=self.auth())
        self.assertEqual(st, 200)
        self.assertNotIn(b"ghp_secretvalue", body)
        st, _, body = self.req("GET", "/api/settings", headers=self.auth())
        self.assertNotIn(b"ghp_secretvalue", body)
        self.assertEqual(json.loads(body)["secrets"]["token:bot"], "pasted")
        # and nothing on disk
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    self.assertNotIn(b"ghp_secretvalue", fh.read())


if __name__ == "__main__":
    unittest.main()
