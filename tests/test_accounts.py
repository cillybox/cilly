"""GitHub accounts: each token is a bot account, its agents commit as that account, and an agent whose
repository more than one account has ticked can be given one of them.  Whatever is picked, an agent only
ever gets a token that has its repository ticked."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from cilly import config as C

FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")
WORK = {"name": "acme-agent", "email": "98765432+acme-agent@users.noreply.github.com"}


def cfg_with(**tokens):
    cfg = json.loads(json.dumps(C.DEFAULT_CONFIG))
    cfg["tokens"] = tokens
    return cfg


class ConfigTests(unittest.TestCase):
    def test_the_first_account_with_the_repository_is_the_default(self):
        cfg = cfg_with(bot={"repos": ["me/a", "me/b"], "secret": {}}, work={"repos": ["me/a"], "secret": {}})
        self.assertEqual(C.tokens_for_repo(cfg, "me/a"), ["bot", "work"])
        self.assertEqual(C.token_for_repo(cfg, "me/a"), "bot")
        self.assertEqual(C.token_for_repo(cfg, "me/a", "work"), "work")
        self.assertEqual(C.token_for_repo(cfg, "me/b", "work"), "bot")  # work can't reach it: never its token
        self.assertEqual(C.token_for_repo(cfg, "me/b", "gone"), "bot")
        self.assertIsNone(C.token_for_repo(cfg, "me/c", "work"))

    def test_an_account_without_its_own_identity_uses_the_shared_one(self):
        cfg = cfg_with(bot={"repos": [], "secret": {}}, work={"repos": [], "secret": {}, "identity": WORK})
        shared = {"name": cfg["identity"]["name"], "email": cfg["identity"]["email"]}
        self.assertEqual(C.account_identity(cfg, "bot"), shared)
        self.assertEqual(C.account_identity(cfg, "work"), WORK)
        self.assertEqual(C.account_identity(cfg, None), shared)
        self.assertEqual(C.account_identity(cfg, "gone"), shared)
        self.assertTrue(C.placeholder_identity(cfg))  # the first account's
        self.assertFalse(C.placeholder_identity(cfg, "work"))

    def test_an_accounts_identity_is_checked_like_the_shared_one(self):
        for bad in ("x", {"name": "a b", "email": WORK["email"]}, {"name": "ok", "email": "me@example.com"}, {"name": "ok"}):
            cfg = cfg_with(bot={"repos": [], "secret": {}, "identity": bad})
            with self.assertRaises(ValueError, msg=bad):
                C.validate(cfg)
        C.validate(cfg_with(bot={"repos": [], "secret": {}, "identity": WORK}))


class AgentTests(unittest.TestCase):
    """Against the fake QEMU, like the rest of an agent's life."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        env = {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
               "XDG_RUNTIME_DIR": os.path.join(t, "run"), "PATH": FAKE_BIN + os.pathsep + os.environ["PATH"], "FAKE_MEMOUT": "none"}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "cilly"))
        with open(os.path.join(env["XDG_CONFIG_HOME"], "cilly", "config.json"), "w") as f:
            json.dump({"tokens": {"bot": {"repos": ["me/repo", "me/other"], "secret": {}},
                                  "work": {"repos": ["me/repo"], "secret": {}, "identity": WORK}},
                       "vm": {"use_bwrap": "off"}}, f)
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
        a = self.app.api_create_agent(dict({"profile": "coding", "mode": "persistent"}, **body))
        return self.app.manager.get(a["id"])

    def payload(self, ag):
        """Which token a task asks for, and who its commits are by."""
        asked = []
        with mock.patch.object(self.app, "_secret", side_effect=lambda key, ref, what: asked.append(key) or "ghp_" + key):
            p = self.app._task_payload_for(ag, "Fix it", new_branch=False, cont=True, pr_title="")
        return asked[0], p["env"]["GH_TOKEN"], {k: v for k, v in p["agent_env"].items() if k.startswith("GIT_")}

    def test_an_agent_gets_its_accounts_token_and_commits_as_it(self):
        shared = {"name": "agent-bot", "email": "00000000+agent-bot@users.noreply.github.com"}
        env = lambda i: {"GIT_AUTHOR_NAME": i["name"], "GIT_AUTHOR_EMAIL": i["email"],  # noqa: E731
                         "GIT_COMMITTER_NAME": i["name"], "GIT_COMMITTER_EMAIL": i["email"]}
        plain = self.agent(name="plain", repo="me/repo")
        self.assertEqual(self.payload(plain), ("token:bot", "ghp_token:bot", env(shared)))  # the first account
        work = self.agent(name="work", repo="me/repo", account="work")
        self.assertEqual(work.meta["account"], "work")
        self.assertEqual(self.payload(work), ("token:work", "ghp_token:work", env(WORK)))
        # untick the repository from work: its agent falls back to the account that still has it
        cfg = json.loads(json.dumps(self.app.cfg()))
        cfg["tokens"]["work"]["repos"] = []
        self.app.set_cfg(cfg)
        self.assertEqual(self.payload(work)[0], "token:bot")

    def test_an_account_that_doesnt_reach_the_repository_is_refused(self):
        from cilly.server import ApiError
        for body in ({"repo": "me/other", "account": "work"}, {"repo": "me/repo", "account": "gone"}, {"account": "bot"}):
            with self.assertRaises(ApiError, msg=body):
                self.agent(name="x", **body)
        self.assertEqual(self.app.manager.list(), [])

    def test_a_projects_account_applies_where_it_reaches(self):
        from cilly.server import ApiError
        p = self.app.api_project_create({"name": "Acme"})
        pid = p.get("id") or p["project"]["id"]
        with self.assertRaises(ApiError):
            self.app.api_project_update(pid, {"defaults": {"account": "gone"}})
        self.app.api_project_update(pid, {"defaults": {"account": "work"}})
        here = self.agent(name="here", repo="me/repo", project=pid)
        self.assertEqual(here.meta["account"], "work")
        other = self.agent(name="other", repo="me/other", project=pid)  # work doesn't have it: the default
        self.assertIsNone(other.meta["account"])
        picked = self.agent(name="picked", repo="me/repo", project=pid, account="bot")  # New agent says otherwise
        self.assertEqual(picked.meta["account"], "bot")

    def test_add_account(self):
        from cilly import github as GH
        from cilly.server import ApiError, device_may
        asked = []
        reply = {"login": "acme2", "id": 5}
        with mock.patch.object(GH, "api_get", side_effect=lambda path, tok, **kw: asked.append((path, tok)) or reply):
            r = self.app.api_account_add({"token": " ghp_new "})
            self.assertEqual(asked, [("/user", "ghp_new")])
            self.assertEqual((r["token"], r["name"], r["email"]), ("acme2", "acme2", "5+acme2@users.noreply.github.com"))
            t = self.app.cfg()["tokens"]["acme2"]
            self.assertEqual((t["repos"], t["identity"]), ([], {"name": "acme2", "email": "5+acme2@users.noreply.github.com"}))
            self.assertEqual(self.app.secrets.get("token:acme2", t["secret"]), "ghp_new")
            with self.assertRaisesRegex(ApiError, "added already"):
                self.app.api_account_add({"token": "ghp_again"})
            with self.assertRaisesRegex(ApiError, "Paste"):
                self.app.api_account_add({"token": ""})
            reply = {"login": "acme-agent", "id": 98765432}  # work's account, under another token name
            with self.assertRaisesRegex(ApiError, "acme-agent is added already, as the token work"):
                self.app.api_account_add({"token": "ghp_work_again"})
        self.assertNotIn("acme2", [a["token"] for a in self.app.api_state({})["accounts"] if a["token"] != "acme2"])
        self.assertFalse(device_may("POST", "/api/accounts"))  # a paired phone can't add one

    def test_the_state_lists_the_accounts(self):
        accounts = self.app.api_state({})["accounts"]
        self.assertEqual([(a["token"], a["name"], a["repos"], a["placeholder"]) for a in accounts],
                         [("bot", "agent-bot", ["me/repo", "me/other"], True), ("work", "acme-agent", ["me/repo"], False)])


if __name__ == "__main__":
    unittest.main()
