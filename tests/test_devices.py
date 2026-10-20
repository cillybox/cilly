import json
import os
import stat
import tempfile
import unittest

from cilly.devices import MAX_PENDING, PAIR_TTL, DeviceError, DeviceStore, clean_name


class DeviceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "cfg", "devices.json")
        self.t = 1000.0
        self.store = DeviceStore(self.path, now=lambda: self.t)

    def tearDown(self):
        self.tmp.cleanup()

    def pair(self, name="iPhone"):
        s = self.store.start(name)
        pid = self.store.list()["pending"][-1]["id"]
        self.store.decide(pid, True)
        r = self.store.poll(s["pair"])
        self.assertEqual(r["state"], "approved")
        return r

    def test_pair_approve_gives_token_once(self):
        s = self.store.start("iPhone")
        self.assertRegex(s["code"], r"^\d{6}$")
        self.assertEqual(self.store.poll(s["pair"]), {"state": "pending"})
        pend = self.store.list()["pending"]
        self.assertEqual([(p["name"], p["code"]) for p in pend], [("iPhone", s["code"])])
        self.store.decide(pend[0]["id"], True)
        r = self.store.poll(s["pair"])
        self.assertEqual(r["state"], "approved")
        self.assertEqual(self.store.check(r["token"]), {"id": r["device"]["id"], "name": "iPhone"})
        self.assertEqual(self.store.poll(s["pair"]), {"state": "expired"})  # handed out once

    def test_only_a_hash_is_stored_with_0600(self):
        r = self.pair()
        raw = open(self.path).read()
        self.assertNotIn(r["token"], raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        again = DeviceStore(self.path)
        self.assertEqual(again.check(r["token"])["name"], "iPhone")

    def test_deny_and_wrong_tokens(self):
        s = self.store.start("x")
        self.store.decide(self.store.list()["pending"][0]["id"], False)
        self.assertEqual(self.store.poll(s["pair"]), {"state": "denied"})
        self.assertIsNone(self.store.check("nope"))
        self.assertIsNone(self.store.check(""))
        self.assertEqual(self.store.poll("unknown"), {"state": "expired"})
        with self.assertRaises(DeviceError):
            self.store.decide("deadbeef", True)

    def test_revoke_is_immediate(self):
        r = self.pair()
        self.store.revoke(r["device"]["id"])
        self.assertIsNone(self.store.check(r["token"]))
        self.assertEqual(json.load(open(self.path))["devices"], [])

    def test_requests_expire_and_are_limited(self):
        s = self.store.start("a")
        self.t += PAIR_TTL + 1
        self.assertEqual(self.store.poll(s["pair"]), {"state": "expired"})
        for _ in range(MAX_PENDING):
            self.store.start("b")
        with self.assertRaises(DeviceError) as e:
            self.store.start("c")
        self.assertEqual(e.exception.code, 429)
        pid = self.store.list()["pending"][0]["id"]
        self.t += PAIR_TTL + 1  # expired before you approved it
        with self.assertRaises(DeviceError):
            self.store.decide(pid, True)

    def test_names_are_cleaned(self):
        self.assertEqual(clean_name("<b>Phone</b>\n"), "bPhoneb")
        self.assertEqual(clean_name(""), "Device")
        self.assertEqual(len(clean_name("x" * 100)), 40)


if __name__ == "__main__":
    unittest.main()
