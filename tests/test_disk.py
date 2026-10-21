"""An agent's disk size: picked on New agent or in a project's defaults, within the limit in Settings,
and only ever grown for a disk that's kept."""

import json
import os
import tempfile
import unittest
from unittest import mock

from cilly import config as C

FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")


class DiskSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        env = {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
               "XDG_RUNTIME_DIR": os.path.join(t, "run"), "PATH": FAKE_BIN + os.pathsep + os.environ["PATH"], "FAKE_MEMOUT": "none"}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "cilly"))
        with open(os.path.join(env["XDG_CONFIG_HOME"], "cilly", "config.json"), "w") as f:
            json.dump({"tokens": {"bot": {"repos": ["me/repo"], "secret": {}}}, "vm": {"use_bwrap": "off", "max_disk_gb": 100}}, f)
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        from cilly import vm as V
        from cilly.server import App
        open(os.path.join(V.images_dir(), "base-test.qcow2"), "w").close()
        self.tools = mock.patch.object(V, "detect_tools", lambda cfg: {
            "qemu": os.path.join(FAKE_BIN, "qemu-system-x86_64"), "qemu-img": os.path.join(FAKE_BIN, "qemu-img"),
            "bwrap": None, "virtiofsd": None, "xorriso": None, "kvm": True})
        self.tools.start()
        self.app = App(0)
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-coding")

    def tearDown(self):
        self.app.manager.shutdown_all()
        self.tools.stop()
        self.env.stop()
        self.tmp.cleanup()

    def agent(self, **body):
        a = self.app.api_create_agent(dict({"profile": "coding", "mode": "persistent", "repo": "me/repo"}, **body))
        return self.app.manager.get(a["id"])

    def test_the_size_comes_from_new_agent_the_project_or_settings(self):
        from cilly.server import ApiError
        self.assertEqual(self.agent(name="plain").meta["disk_gb"], 20)  # the default in Settings, kept with the agent
        self.assertEqual(self.agent(name="big", disk_gb=60).meta["disk_gb"], 60)
        for bad in (19, 101, "x"):
            with self.assertRaises(ApiError, msg=bad):
                self.agent(name="bad", disk_gb=bad)
        p = self.app.api_project_create({"name": "Builds"})
        pid = p.get("id") or p["project"]["id"]
        with self.assertRaises(ApiError):
            self.app.api_project_update(pid, {"defaults": {"disk_gb": 500}})  # over the limit
        self.app.api_project_update(pid, {"defaults": {"disk_gb": 80}})
        self.assertEqual(self.agent(name="here", project=pid).meta["disk_gb"], 80)
        self.assertEqual(self.agent(name="said", project=pid, disk_gb=40).meta["disk_gb"], 40)  # New agent says otherwise
        state = self.app.api_state({})["vm_defaults"]
        self.assertEqual((state["disk_gb"], state["max_disk_gb"], state["min_disk_gb"]), (20, 100, 20))

    def test_a_kept_disk_only_grows(self):
        from cilly.server import ApiError
        ag = self.app.manager.create(name="keeper", mode="persistent", repo="me/repo", agent="claude", cpus=1, memory_mb=1024,
                                     auto_stop=False, branch_prefix="agent/")  # never started: stopped
        ag.set(disk_gb=40)
        res = lambda disk: self.app.api_agent_resources(ag.id, {"cpus": 1, "memory_mb": 1024, "disk_gb": disk})  # noqa: E731
        os.makedirs(os.path.dirname(ag.paths.disk), exist_ok=True)
        open(ag.paths.disk, "a").close()  # it has a disk
        with self.assertRaisesRegex(ApiError, "only grow"):
            res(30)
        res(60)
        self.assertEqual(ag.meta["disk_gb"], 60)
        os.remove(ag.paths.disk)  # no disk yet: any size within the limit
        res(30)
        self.assertEqual(ag.meta["disk_gb"], 30)
        self.app.api_agent_resources(ag.id, {"cpus": 1, "memory_mb": 1024})  # without a disk size: kept
        self.assertEqual(ag.meta["disk_gb"], 30)
        with self.assertRaises(ApiError):
            res(101)

    def test_the_limits_are_checked(self):
        for vm in ({"disk_gb": 10}, {"disk_gb": 300, "max_disk_gb": 200}, {"max_disk_gb": 9000}, {"disk_gb": True}):
            cfg = json.loads(json.dumps(C.DEFAULT_CONFIG))
            cfg["vm"].update(vm)
            with self.assertRaises(ValueError, msg=vm):
                C.validate(cfg)
        self.assertEqual((C.disk_gb({"disk_gb": 50}, C.DEFAULT_CONFIG), C.disk_gb({}, C.DEFAULT_CONFIG),
                          C.disk_gb({"disk_gb": 5}, C.DEFAULT_CONFIG)), (50, 20, 20))


if __name__ == "__main__":
    unittest.main()
