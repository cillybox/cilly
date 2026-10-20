"""Questions an agent asks you (cilly/asks.py and the panel around it): the ~/ask/ file format, checked
like anything else from a VM, and the round trip to one answer message, end to end against the fake QEMU."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from cilly import asks as AK

FAKE_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin")


def doc(**over):
    q = {"header": "Database", "question": "Which database should the plugin use?", "multi": False,
         "options": [{"label": "SQLite", "description": "One file, no server."}, {"label": "MySQL"}]}
    return json.dumps(dict({"version": 1, "questions": [dict(q, **over)]}))


class FormatTests(unittest.TestCase):
    def test_a_well_formed_file(self):
        text = json.dumps({"version": 1, "extra": "ignored", "questions": [
            {"header": " Data" + chr(0x200B) + "base ", "question": "Which\n  database\x1b[31m?", "options": [
                {"label": "SQLite", "description": "One file.", "icon": "ignored"}, {"label": "MySQL"}]},
            {"header": "Features", "question": "Which first?", "multi": True,
             "options": [{"label": "Shop"}, {"label": "Auctions"}, {"label": "Mail"}, {"label": "Bank"}]}]})
        qs = AK.parse("db.json", text)
        self.assertEqual(qs[0], {"header": "Database", "question": "Which database?", "multi": False,
                                 "options": [{"label": "SQLite", "description": "One file."}, {"label": "MySQL", "description": ""}]})
        self.assertTrue(qs[1]["multi"])

    def test_anything_else_is_refused_with_a_reason(self):
        many = [{"header": "H", "question": "Q?", "options": [{"label": "a"}, {"label": "b"}]}] * 5
        cases = [
            ("../up.json", doc(), "file name"), ("x.txt", doc(), "file name"), (".hidden.json", doc(), "file name"),
            ("a.json", "x" * (AK.FILE_MAX + 1), "larger than 16 KiB"), ("a.json", "{not json", "valid JSON"),
            ("a.json", json.dumps({"questions": []}), '"version": 1'), ("a.json", doc().replace('"version": 1', '"version": 2'), '"version": 1'),
            ("a.json", json.dumps({"version": 1, "questions": []}), "1 to 4 questions"),
            ("a.json", json.dumps({"version": 1, "questions": many}), "1 to 4 questions"),
            ("a.json", json.dumps({"version": 1, "questions": ["q"]}), "isn't an object"),
            ("a.json", doc(header="Much too long"), "longer than 12"), ("a.json", doc(header=chr(0x200B) + " "), "header is empty"),
            ("a.json", doc(question="x" * 601), "longer than 600"), ("a.json", doc(question=5), "must be text"),
            ("a.json", doc(options=[{"label": "only"}]), "2 to 4 options"),
            ("a.json", doc(options=[{"label": str(i)} for i in range(5)]), "2 to 4 options"),
            ("a.json", doc(options=[{"label": "a"}, "b"]), "option 2 isn't an object"),
            ("a.json", doc(options=[{"label": "a"}, {"label": "x" * 81}]), "longer than 80"),
            ("a.json", doc(options=[{"label": "a"}, {"label": "b", "description": "x" * 201}]), "longer than 200"),
            ("a.json", doc(options=[{"label": "Same"}, {"label": "same"}]), "same label"),
            ("a.json", doc(multi="yes"), "true or false"),
        ]
        for name, text, why in cases:
            with self.assertRaises(AK.AskError, msg=(name, text[:60])) as e:
                AK.parse(name, text)
            self.assertIn(why, str(e.exception), (name, text[:60]))

    def test_answers_are_checked_and_quoted(self):
        qs = AK.parse("plan.json", json.dumps({"version": 1, "questions": [
            {"header": "Database", "question": "Which database?", "options": [{"label": "SQLite"}, {"label": "MySQL"}]},
            {"header": "Features", "question": "Which first?", "multi": True, "options": [{"label": "Shop"}, {"label": "Auctions"}, {"label": "Mail"}]},
            {"header": "Name", "question": "What should the command be called?", "options": [{"label": "/shop"}, {"label": "/market"}]}]}))
        picked = AK.answers(qs, [{"picked": [0]}, {"picked": [2, 0], "other": "and a bank"}, {"other": " /bazaar\n"}])
        self.assertEqual(picked, [{"picked": ["SQLite"], "other": ""}, {"picked": ["Shop", "Mail"], "other": "and a bank"},
                                  {"picked": [], "other": "/bazaar"}])
        self.assertEqual(AK.answer_message("plan.json", qs, picked),
                         "My answers to your questions in ~/ask/plan.json:\n\n"
                         "> Database: Which database?\nSQLite\n\n"
                         "> Features: Which first?\nShop, Mail, in my own words: and a bank\n\n"
                         "> Name: What should the command be called?\nin my own words: /bazaar")
        for bad, why in (([{"picked": [0]}], "every question"), ([{"picked": [0]}, {"picked": [0]}, {"picked": [5]}], "isn't there"),
                         ([{"picked": [0, 1]}, {"picked": [0]}, {"picked": [0]}], "takes one answer"),
                         ([{"picked": [0], "other": "x"}, {"picked": [0]}, {"picked": [0]}], "takes one answer"),
                         ([{"picked": [True]}, {"picked": [0]}, {"picked": [0]}], "isn't there"),
                         ([{"picked": [0]}, {"picked": [1, 1]}, {"picked": [0]}], "isn't there"),
                         ([{"picked": [0]}, {"picked": []}, {"picked": [0]}], "no answer yet"), ("all", "every question")):
            with self.assertRaises(AK.AskError) as e:
                AK.answers(qs, bad)
            self.assertIn(why, str(e.exception), bad)

    def test_the_agent_is_told_how_to_ask_in_plain_terms(self):
        from cilly.server import vm_notes
        for meta in ({"mode": "persistent", "repo": "me/repo"}, {"mode": "ephemeral"}, {"mode": "persistent", "profile": "research"}):
            line = next(ln for ln in vm_notes(meta).splitlines() if "~/ask/" in ln)
            for want in ('"version": 1', '"header"', '"question"', '"options"', '"label"', '"description"', '"multi"',
                         "1 to 4 questions", "2 to 4", "12 characters", "recommend first", "end your turn"):
                self.assertIn(want, line, meta)
            self.assertNotIn("Claude", line)  # the same text works for any agent

    def test_a_paired_phone_may_answer(self):
        from cilly.server import device_may
        for action in ("answer", "dismiss"):
            self.assertTrue(device_may("POST", f"/api/agents/abcdef12/asks/12345678/{action}"))


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        env = {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
               "XDG_RUNTIME_DIR": os.path.join(t, "run"), "PATH": FAKE_BIN + os.pathsep + os.environ["PATH"], "FAKE_MEMOUT": "none"}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "cilly"))
        with open(os.path.join(env["XDG_CONFIG_HOME"], "cilly", "config.json"), "w") as f:
            json.dump({"tokens": {"bot": {"repos": ["me/repo"], "secret": {}}}, "vm": {"use_bwrap": "off"}}, f)
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
        self.app.secrets.set_pasted("token:bot", "ghp_bot")
        self.app.secrets.set_pasted("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-coding")
        a = self.app.api_create_agent({"name": "coder", "profile": "coding", "mode": "persistent", "repo": "me/repo"})
        self.ag = self.app.manager.get(a["id"])
        self.wait(lambda: self.ag.meta["state"] == "ready", "ready")

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

    def message(self, text):
        n = len(self.ag.meta.get("tasks", []))
        self.app.api_agent_message(self.ag.id, {"instructions": text})
        self.wait(lambda: len(self.ag.meta["tasks"]) > n and self.ag.meta["tasks"][-1]["state"] == "done", "task")
        return self.ag.meta["tasks"][-1]

    def test_questions_round_trip_to_one_answer_message(self):
        from cilly.server import agent_status
        asked = self.message("Make the sprites, ask me what you need")
        self.wait(lambda: self.ag.meta.get("asks"), "questions")
        rec = self.ag.meta["asks"][-1]
        self.assertEqual((rec["state"], rec["task"], rec["name"], [q["header"] for q in rec["questions"]]),
                         ("open", asked["id"], "sprites.json", ["Style", "Animations", "Output"]))
        item = next(it for it in self.app.api_inbox({})["items"] if it["kind"] == "questions")
        self.assertEqual((item["agent"], item["id"]), (self.ag.id, rec["id"]))
        state = next(a for a in self.app.api_state({})["agents_list"] if a["id"] == self.ag.id)
        self.assertEqual(state["status"]["key"], "waiting")
        r = self.app.api_ask_answer(self.ag.id, rec["id"], {"answers": [
            {"picked": [0]}, {"picked": [0, 1, 3]}, {"picked": [], "other": "Put them in assets/sprites/ please"}]})
        self.wait(lambda: self.ag.meta["tasks"][-1]["state"] == "done", "the answer ran")
        t = self.ag.meta["tasks"][-1]
        self.assertEqual(t["id"], r["task"])
        self.assertTrue(t["continue"])  # the same conversation
        self.assertEqual(t["instructions"], "My answers to your questions in ~/ask/sprites.json:\n\n"
                         "> Style: Which pixel art style should the sprites use?\n16x16, 4 colors\n\n"
                         "> Animations: Which animations do you need first?\nIdle, Walk, Death\n\n"
                         "> Output: Where should the finished sprites go?\nin my own words: Put them in assets/sprites/ please")
        self.assertEqual((rec["state"], rec["answer_task"], rec["answers"][1]["picked"]), ("answered", t["id"], ["Idle", "Walk", "Death"]))
        self.assertFalse([it for it in self.app.api_inbox({})["items"] if it["kind"] == "questions"])
        with self.assertRaisesRegex(Exception, "already answered"):  # twice is refused
            self.app.api_ask_answer(self.ag.id, rec["id"], {"answers": [{"picked": [0]}, {"picked": [0]}, {"picked": [0]}]})

    def test_two_submits_at_once_send_one_message(self):
        import threading
        from cilly.server import ApiError
        self.message("Make the sprites, ask me what you need")
        self.wait(lambda: self.ag.meta.get("asks"), "questions")
        rec = self.ag.meta["asks"][-1]
        answers = {"answers": [{"picked": [0]}, {"picked": [1]}, {"picked": [0]}]}
        real, results = self.app.api_agent_message, []

        def slow(*a, **k):  # the send takes a moment, and the other device's Submit comes in meanwhile
            time.sleep(0.3)
            return real(*a, **k)

        def submit():
            try:
                results.append(self.app.api_ask_answer(self.ag.id, rec["id"], answers)["task"])
            except ApiError as e:
                results.append(str(e))
        with mock.patch.object(self.app, "api_agent_message", side_effect=slow) as send:
            threads = [threading.Thread(target=submit) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(send.call_count, 1, results)  # one message
        self.assertEqual(sorted(len(r) for r in results), [8, len("These questions are already being answered.")], results)
        self.assertIn("These questions are already being answered.", results)
        self.assertEqual(rec["state"], "answered")

    def test_a_failed_send_leaves_the_questions_open(self):
        from cilly.server import ApiError
        self.message("Make the sprites, ask me what you need")
        self.wait(lambda: self.ag.meta.get("asks"), "questions")
        rec = self.ag.meta["asks"][-1]
        answers = {"answers": [{"picked": [0]}, {"picked": [1]}, {"picked": [0]}]}
        with mock.patch.object(self.app, "api_agent_message", side_effect=ApiError("the agent is gone")):
            with self.assertRaisesRegex(ApiError, "gone"):
                self.app.api_ask_answer(self.ag.id, rec["id"], answers)
        self.assertEqual(rec["state"], "open")  # so you can try again
        self.app.api_ask_answer(self.ag.id, rec["id"], answers)
        self.assertEqual(rec["state"], "answered")

    def test_a_hostile_vm_gets_nothing_shown(self):
        self.message("hostile ask")
        self.wait(lambda: len(self.ag.meta.get("asks", [])) >= 5, "refusals")
        time.sleep(0.3)  # the second batch for the same task, which must be ignored
        recs = self.ag.meta["asks"]
        self.assertEqual([r["state"] for r in recs], ["refused"] * 5)
        self.assertEqual([r["error"].split(",")[0][:24] for r in recs],
                         ["the file is larger than ", "the file name must be le", "the file isn't valid JSO",
                          "question 1: the header i", "only 4 question files ar"])
        self.assertEqual(recs[1]["name"], "?")  # a bad name isn't kept
        self.assertFalse([it for it in self.app.api_inbox({})["items"] if it["kind"] == "questions"])

    def test_one_open_set_and_secrets_hidden(self):
        tid = self.message("hi")["id"]
        good = lambda q: {"name": "a.json", "text": json.dumps({"version": 1, "questions": [  # noqa: E731
            {"header": "Token", "question": q, "options": [{"label": "Yes"}, {"label": "No"}]}]})}
        self.app.on_questions(self.ag, {"task": tid, "files": [good("Use sk-ant-oat-coding here?"), good("Or this?")]})
        first, second = self.ag.meta["asks"]
        self.assertEqual((first["state"], second["state"]), ("replaced", "open"))  # the newest wins
        self.assertEqual(first["questions"][0]["question"], "Use [hidden] here?")
        tid2 = self.message("again")["id"]
        self.app.on_questions(self.ag, {"task": tid2, "files": [good("Newer?")]})
        self.assertEqual([r["state"] for r in self.ag.meta["asks"]], ["replaced", "chat", "open"])  # "again" answered it in the chat
        third = self.ag.meta["asks"][-1]
        self.app.api_ask_dismiss(self.ag.id, third["id"])  # Chat About This
        self.assertEqual(third["state"], "chat")
        self.app.on_questions(self.ag, {"task": self.message("more")["id"], "files": [good("Last?")]})
        self.message("I'll just tell you: no")  # a message of your own answers them too
        self.assertEqual(self.ag.meta["asks"][-1]["state"], "chat")


if __name__ == "__main__":
    unittest.main()
