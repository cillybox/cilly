import json
import os
import threading
import time
import unittest
from unittest import mock

from cilly.vm import RELAY, VMError, VMPaths, bwrap_argv, qemu_argv, virtiofsd_argv


class QemuArgvTests(unittest.TestCase):
    def setUp(self):
        self.paths = VMPaths("/data/vms/abcd1234", "/run/user/1000/cilly/abcd1234")

    def _argv(self, **kw):
        return qemu_argv(qemu="qemu-system-x86_64", name="abcd1234", paths=self.paths, cpus=2,
                         memory_mb=2048, python="/usr/bin/python3", **kw)

    def _opt(self, argv, flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag]

    def test_network_is_restricted(self):
        argv = self._argv()
        nets = self._opt(argv, "-netdev")
        self.assertEqual(len(nets), 1)
        self.assertTrue(nets[0].startswith("user,"))
        self.assertIn("restrict=on", nets[0])
        self.assertIn("guestfwd=tcp:10.0.2.100:3128-cmd:", nets[0])
        self.assertIn(RELAY, nets[0])
        self.assertNotIn("hostfwd", " ".join(argv))
        self.assertIn("-nodefaults", argv)

    def test_task_disk_readonly(self):
        drives = self._opt(self._argv(), "-drive")
        task = [d for d in drives if "id=task" in d][0]
        self.assertIn("readonly=on", task)
        root = [d for d in drives if "id=root" in d][0]
        self.assertIn(self.paths.disk, root)

    def test_no_host_paths_shared_by_default(self):
        argv = self._argv()
        joined = " ".join(argv)
        for bad in ("virtfs", "vhost-user-fs", "9p", "fat:"):
            self.assertNotIn(bad, joined)
        # the only files the guest gets are this VM's own disks
        files = [kv.split("=", 1)[1] for d in self._opt(argv, "-drive") for kv in d.split(",") if kv.startswith("file=")]
        self.assertTrue(files)
        for f in files:
            self.assertTrue(f.startswith(self.paths.sandbox_dir + "/"), f)

    def test_live_share_adds_virtiofs(self):
        argv = self._argv(live_share=True)
        self.assertIn("vhost-user-fs-pci,chardev=vfs,tag=inject", argv)
        self.assertTrue(any("share=on" in a for a in argv))

    def test_has_vga_but_no_display(self):
        argv = self._argv()
        self.assertEqual(self._opt(argv, "-vga"), ["std"])  # Debian guests reboot-loop without one
        self.assertEqual(self._opt(argv, "-display"), ["none"])

    def test_seccomp_sandbox(self):
        sb = self._opt(self._argv(), "-sandbox")[0]
        self.assertTrue(sb.startswith("on,"))
        self.assertIn("obsolete=deny", sb)
        # libslirp's guestfwd child calls setsid(); elevateprivileges=deny would kill it
        self.assertNotIn("elevateprivileges=deny", sb)
        self.assertNotIn("spawn=deny", sb)

    def test_no_nested_virtualization_by_default(self):
        self.assertEqual(self._opt(self._argv(), "-cpu"), ["host,vmx=off,svm=off"])
        self.assertEqual(self._opt(self._argv(cpu_model="host,pmu=off"), "-cpu"), ["host,pmu=off,vmx=off,svm=off"])
        self.assertEqual(self._opt(self._argv(nested_virt=True), "-cpu"), ["host"])

    def test_free_memory_goes_back_to_the_pc(self):
        for argv in (self._argv(), self._argv(live_share=True, memout=True)):
            balloons = [d for d in self._opt(argv, "-device") if d.startswith("virtio-balloon")]
            self.assertEqual(balloons, ["virtio-balloon-pci,free-page-reporting=on"])  # no target size, no stats polling

    def test_commas_rejected(self):
        with self.assertRaises(VMError):
            qemu_argv(qemu="q", name="x", paths=VMPaths("/a,b", "/run"), cpus=1, memory_mb=512)

    def test_bwrap_unshares_network(self):
        argv = bwrap_argv("bwrap", ["qemu"], ro=["/img"], rw=["/vm", "/run/x"])
        self.assertIn("--unshare-all", argv)
        self.assertIn("--die-with-parent", argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv[-2:], ["--", "qemu"])
        home = os.path.expanduser("~")
        self.assertNotIn(home, argv)

    def test_the_egress_policy_is_out_of_qemus_sandbox(self):
        """An escaped QEMU sees what bwrap binds: it must not read a route's login, nor change
        where the proxy sends traffic or what it lets through."""
        p = self.paths
        argv = bwrap_argv("bwrap", ["qemu"], ro=["/img"], rw=[p.sandbox_dir, p.run_dir])  # as an agent starts
        binds = [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind", "--dev-bind")]
        for f in (p.route_file, p.allow_file, p.never_file):
            for b in binds:
                self.assertFalse(f == b or f.startswith(b.rstrip("/") + "/"), (f, b))
        self.assertTrue(p.route_file.startswith("/run/user/1000/"))  # still in memory

    def test_virtiofsd_readonly(self):
        a = virtiofsd_argv("virtiofsd", "/run/s", "/data/x")
        self.assertIn("--readonly", a)
        self.assertIn("--sandbox=namespace", a)


if __name__ == "__main__":
    unittest.main()


class ControlReaderTests(unittest.TestCase):
    """A VM can send anything on its control port: a bad message is skipped, and the next one still counts."""

    def setUp(self):
        import tempfile
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        os.makedirs(os.path.join(t, "run"))
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
                                                "XDG_RUNTIME_DIR": os.path.join(t, "run")})
        self.env.start()
        from cilly import config as C
        from cilly import vm as V
        cfg = C.load()
        self.mgr = V.Manager(lambda: cfg)
        self.a = self.mgr.create(name="t", mode="persistent", repo="me/repo", agent="claude", cpus=1, memory_mb=1024,
                                 auto_stop=False, branch_prefix="agent/")
        self.a.meta["tasks"] = [{"id": "t1", "state": "queued"}]
        self.a.fileops["f1"] = {"event": __import__("threading").Event(), "result": None}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_secret_split_by_invisible_characters_is_still_hidden(self):
        import json
        self.a.hide = frozenset({"ghp_secretvalue1234"})
        # the same secret broken up with a zero-width space, a byte-order mark and a zero-width joiner
        for dirty in ("ghp_secret\u200bvalue1234", "ghp_\ufeffsecretvalue1234", "ghp_sec\u200dretvalue1234",
                      "ghp\u2060_secretvalue1234"):
            red = self.mgr.redact_obj(self.a, {"k": f"here: {dirty} done"})
            self.assertNotIn("ghp_", json.dumps(red), dirty)  # no fragment of the secret is left visible
            self.assertIn("[hidden]", red["k"])
            for ch in "\u200b\ufeff\u200d\u2060":
                self.assertNotIn(ch, json.dumps(red))  # the invisible characters are gone too

    def test_log_bytes_hide_a_split_secret_and_keep_their_lines(self):
        self.a.hide = frozenset({"sk-ant-abcdef12345"})
        data = ("first line\n" + "token sk-ant-abc\u200bdef12345 here\n" + "last\ttab\n").encode()
        out = self.mgr.redact_bytes(self.a, data).decode("utf-8", "surrogateescape")
        self.assertIn("[hidden]", out)
        self.assertNotIn("sk-ant-", out)
        self.assertNotIn("\u200b", out)
        self.assertEqual(out.count("\n"), 3)  # the three lines are kept
        self.assertIn("last\ttab", out)  # and tabs

    def test_every_default_ignorable_and_a_lone_cr_is_caught(self):
        self.a.hide = frozenset({"sk-ant-abcdef12345"})
        for ch in ("\ufe00", "\ufe0f", "\U000e0100", "\U000e01ef", "\u034f", "\u180b", "\u180f", "\u3164", "\u115f",
                   "\u1160", "\uffa0", "\u17b4", "\u17b5", "\u00ad", "\u061c", "\u2065", "\ufff0", "\U000e0000",
                   "\U000e0fff", "\U0001bca0", "\U0001d173", "\u200b", "\u202e", "\x1b[1;31m", "\x1b7", "\x9b0m", "\r"):
            dirty = f"x sk-ant-abc{ch}def12345 y"
            self.assertEqual(self.mgr.redact_obj(self.a, dirty), "x [hidden] y", repr(ch))
            self.assertEqual(self.mgr.redact_bytes(self.a, dirty.encode()), b"x [hidden] y", repr(ch))
        # a carriage return before a newline ends a line, so it's kept
        self.assertEqual(self.mgr.redact_bytes(self.a, b"sk-ant-abcdef12345\r\nnext"), b"[hidden]\r\nnext")

    def test_only_the_secret_changes(self):
        self.a.hide = frozenset({"ghp_secretvalue1234"})
        around = ("\x1b[1;32mok\x1b[0m \U0001f469\u200d\U0001f4bb \U0001f1e9\U0001f1ea \u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 "
                  "\u0915\u094d\u200d\u0937 \u200fabc\u200e \f\tend\r\n")
        for text in (around, around + "ghp_secret\u200bvalue1234" + around):
            want = text.replace("ghp_secret\u200bvalue1234", "[hidden]")
            self.assertEqual(self.mgr.redact_obj(self.a, {"k": text})["k"], want)
            self.assertEqual(self.mgr.redact_bytes(self.a, text.encode()), want.encode())
        # the escapes around a secret stay, the ones inside it go with it
        self.assertEqual(self.mgr.redact_obj(self.a, "\x1b[31mghp_secret\x1b[0mvalue1234\x1b[0m"), "\x1b[31m[hidden]\x1b[0m")
        self.assertEqual(self.mgr.redact_obj(self.a, "\x1b]0;ghp_secretvalue1234\x07"), "\x1b]0;[hidden]\x07")  # a window title

    def test_binary_findings_are_still_refused(self):
        from cilly import handoff as H
        self.a.hide = frozenset({"ghp_secretvalue1234"})
        for content in ("PK\x03\x04\x00 data", "ghp_secret\x00value1234"):  # a NUL beside a secret, and inside one
            files = self.mgr.redact_obj(self.a, [{"path": "a.bin", "content": content}])
            self.assertNotIn("ghp_", files[0]["content"])
            with self.assertRaisesRegex(H.HandoffError, "binary content"):
                H.validate_files(files, 1 << 20)

    def test_a_nul_in_overlapping_secrets_is_kept(self):
        from cilly import handoff as H
        self.a.hide = frozenset({"abcdefgh12", "gh12345678"})  # two secrets that overlap in the text
        files = self.mgr.redact_obj(self.a, [{"path": "a.bin", "content": "x abcdefgh12\x00345678 y"}])
        self.assertEqual(files[0]["content"], "x [hidden]\x00 y")  # the NUL was in the second one's part
        with self.assertRaisesRegex(H.HandoffError, "binary content"):
            H.validate_files(files, 1 << 20)
        self.assertEqual(self.mgr.redact_obj(self.a, "abcdefgh12gh12345678"), "[hidden][hidden]")  # side by side stay two

    def test_a_secret_across_a_long_lines_flush_is_hidden(self):
        from cilly.vm import LOG_LINE_MAX, _LogSink
        self.a.hide = frozenset({"sk-ant-abcdef12345"})
        for split in ("", "\u200b", "\x1b[0m"):
            path = os.path.join(self.tmp.name, "log")
            if os.path.exists(path):
                os.unlink(path)
            sink = _LogSink(path, lambda data, hold: self.mgr.redact_stream(self.a, data, hold))
            sink.write(b"x" * (LOG_LINE_MAX - 5) + ("sk-ant-abc" + split).encode())  # past the limit with no newline
            sink.write(b"def12345 tail\n")
            sink.close()
            with open(path, "rb") as f:
                out = f.read()
            self.assertEqual(out, b"x" * (LOG_LINE_MAX - 5) + b"[hidden] tail\n", repr(split))
        # a run of invisible characters is never held back for ever
        sink = _LogSink(path, lambda data, hold: self.mgr.redact_stream(self.a, data, hold))
        sink.write(b"sk" + "\u200b".encode() * LOG_LINE_MAX)
        self.assertLess(len(sink.pending), LOG_LINE_MAX)
        sink.close()

    def test_a_megabyte_of_log_is_quick(self):
        self.a.hide = frozenset({"sk-ant-abcdef12345", "ghp_secretvalue1234", "another-secret-value"})
        log = ("\x1b[32mINFO\x1b[0m compiling module foo/bar.rs with -O2 \u200b and some more text\n" * 13000)[:1 << 20]
        for data in (log, log[:500000] + "sk-ant-abc\u200bdef12345" + log[500000:], "a\u200b" * (1 << 19)):
            raw = data.encode()
            took = min(self._timed(lambda: self.mgr.redact_bytes(self.a, raw)) for _ in range(3))
            self.assertLess(took, 0.3)  # about 0.03 s here: room for a slow or busy machine

    @staticmethod
    def _timed(fn):
        t = time.perf_counter()
        fn()
        return time.perf_counter() - t

    def test_a_chat_event_cleans_before_it_hides(self):
        import json
        import socket
        import threading
        self.a.hide = frozenset({"ghp_livesecret99"})
        host, guest = socket.socketpair()
        reader = threading.Thread(target=self.mgr._control_reader, args=(self.a, host), daemon=True)
        reader.start()
        ev = {"type": "chat", "task": "t1", "events": [{"k": "text", "text": "leak ghp_live\u200bsecret99 ok"}]}
        guest.sendall(json.dumps(ev).encode() + b"\n")
        deadline = time.time() + 5
        while not self.mgr.read_chat(self.a.id)["events"] and time.time() < deadline:
            time.sleep(0.02)
        texts = [e.get("text", "") for e in self.mgr.read_chat(self.a.id)["events"]]
        self.assertTrue(any("[hidden]" in t for t in texts), texts)
        self.assertFalse(any("ghp_" in t for t in texts), texts)
        guest.close()

    def test_context_reports_are_checked_and_saved_sparingly(self):
        a, on = self.a, self.mgr._on_context
        a.meta["tasks"] = [{"id": "t1", "state": "running"}, {"id": "t0", "state": "done"}]
        saves = []
        save = a.save_meta
        a.save_meta = lambda: (saves.append(1), save())
        on(a, {"type": "context", "task": "t1", "used": 124000, "window": 200000, "note": "<b>hi</b>"})
        self.assertEqual(a.meta["context"], {"used": 124000, "window": 200000})  # numbers only, nothing else kept
        for bad in ({"used": -1}, {"used": True}, {"used": 1.5}, {"used": "9"}, {"used": 10**8}, {"used": None},
                    {"used": 5, "window": 10**12}, {"used": 5, "window": 999}, {"used": 5, "window": False},
                    {"used": 5, "task": "t0"}, {"used": 5, "task": "nope"}):  # bad numbers, or not the running task
            on(a, dict({"type": "context", "task": "t1"}, **bad))
        self.assertEqual(a.meta["context"], {"used": 124000, "window": 200000})
        for n in range(100):  # a flood: kept in memory, saved at most every few seconds
            on(a, {"type": "context", "task": "t1", "used": 130000 + n})
        self.assertEqual(a.meta["context"], {"used": 130099, "window": 200000})  # a report without a window keeps it
        self.assertEqual(a.meta["tasks"][0]["context"], {"used": 130099, "window": 200000})
        self.assertEqual(len(saves), 1)

    def test_a_model_servers_window_beats_the_programs_guess(self):
        """Claude Code assumes 200k for a model it doesn't know: what the server said wins for the meter."""
        a, on = self.a, self.mgr._on_context
        a.meta["tasks"] = [{"id": "t1", "state": "running", "context_window": 65536}]
        on(a, {"type": "context", "task": "t1", "used": 28000, "window": 200000})
        self.assertEqual(a.meta["context"], {"used": 28000, "window": 65536})
        for got, kept in ((65536, 65536), (999, None), ("65536", None), (True, None), (10**9, None)):
            t = self.mgr.new_task(a.id, {"instructions": "hi", "context_window": got}, queue=True)
            rec = next(r for r in a.meta["tasks"] if r["id"] == t["id"])
            self.assertEqual(rec.get("context_window"), kept, got)

    def test_the_context_window_falls_back_by_model(self):
        from cilly import vm as V
        self.assertEqual([V.context_window(m) for m in ("opus", "sonnet[1m]", "claude-sonnet-4-5[1M]", None, 3)],
                         [200000, 1000000, 1000000, 200000, 200000])
        self.a.meta.update(context={"used": 50000, "window": None}, model="sonnet[1m]",
                           tasks=[{"id": "t1", "state": "done", "model": "opus"}])
        self.assertEqual(self.a.public()["context"], {"used": 50000, "window": 200000})  # the conversation's model
        self.a.meta["tasks"] = []
        self.assertEqual(self.a.public()["context"], {"used": 50000, "window": 1000000})  # else the agent's
        self.assertEqual(self.a.meta["context"]["window"], None)  # only filled in for showing

    def test_bad_messages_are_skipped(self):
        import json
        import socket
        import threading
        bad = [b'{"type": []}', b'{"type": {"a": 1}}', b'{"type": "status", "task": ["t1"], "state": "failed"}',
               b'{"type": "status", "task": {"x": 1}, "state": "failed"}', b'{"type": "chat", "task": 7, "events": [{"k": []}]}',
               b'{"type": "chat", "task": "t1", "events": [{"k": []}, {"k": {"x": 1}}, "text", null]}',
               b'{"type": "files_list", "id": "f1", "entries": [{"name": "a", "kind": "file", "size": "abc", "mtime": 1e400}]}',
               b'{"type": "outbox_images", "task": "t1", "files": {"a.png": 1}}', b'{"type": "ready", "selftest": [1, 2]}',
               b'{"type": "export_chunk", "id": [], "seq": "x"}', b'[' * 100_000 + b']' * 100_000, b'{"type": "phase", "phase": []}',
               b'\xff\xfe not json', b'{"type": "status", "task": "t1", "state": "done", "agent_rc": "x", "error": 5}']
        self.a.meta["tasks"][0]["state"] = "running"
        host, guest = socket.socketpair()
        reader = threading.Thread(target=self.mgr._control_reader, args=(self.a, host), daemon=True)
        reader.start()
        for i, line in enumerate(bad):
            images = [f"{i}.png"]
            guest.sendall(line + b"\n" + json.dumps({"type": "outbox_images", "task": "t1", "files": images}).encode() + b"\n")
            deadline = time.time() + 5
            while self.a.meta["tasks"][0].get("images") != images and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.a.meta["tasks"][0].get("images"), images, line[:80])
        self.assertTrue(reader.is_alive())
        rec = self.a.meta["tasks"][0]
        self.assertEqual((rec["state"], "agent_rc" in rec, "error" in rec), ("done", False, False))  # the last one, without its bad fields
        self.assertEqual(self.a.fileops["f1"]["result"]["entries"][0]["size"], 0)
        guest.close()
        reader.join(5)

    def test_secrets_are_hidden_however_json_escapes_them(self):
        import json
        import socket
        import threading
        secrets = ['pa"ss\\wörd-123', "tok\\en-\u00e9\u00e9\u00e9-x", "key-🔑-🔑-abc", 'q"u"o"t"e"s"!!']
        self.a.hide = frozenset(secrets)
        self.a.meta["tasks"][0]["state"] = "running"
        from cilly import vm as V
        ra = self.mgr.create(name="r", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                             auto_stop=False, branch_prefix="agent/", profile="research")
        ra.hide = self.a.hide
        ra.meta["tasks"] = [{"id": "t1", "state": "running"}]
        got = []
        self.mgr.hooks = {"on_requests": lambda ag, m: got.append(m), "on_outbox": lambda ag, m: got.append(m)}
        for agent in (self.a, ra):
            host, guest = socket.socketpair()
            reader = threading.Thread(target=self.mgr._control_reader, args=(agent, host), daemon=True)
            reader.start()
            for ascii_only in (True, False):
                for sec in secrets:
                    for m in ({"type": "chat", "task": "t1", "events": [{"k": "text", "text": f"the token is {sec}"}]},
                              {"type": "research_requests", "task": "t1", "requests": [{"name": sec, "text": f"look up {sec}"}]},
                              {"type": "status", "task": "t1", "state": "running", "error": f"auth failed for {sec}"},
                              {"type": "outbox", "task": "t1", "files": [{"path": "f.md", "content": f"found {sec}", sec: sec}]}):
                        guest.sendall(json.dumps(m, ensure_ascii=ascii_only).encode() + b"\n")
            import base64
            import hashlib
            data = json.dumps([{"path": "g.md", "content": "in pieces " + secrets[0]}], ensure_ascii=False).encode()
            for m in ({"type": "status", "task": "t1", "state": "running"},  # findings come while their task runs
                      {"type": "outbox_chunk", "task": "t1", "seq": 0, "data": base64.b64encode(data).decode()},
                      {"type": "outbox_end", "task": "t1", "chunks": 1, "sha256": hashlib.sha256(data).hexdigest()}):
                guest.sendall(json.dumps(m).encode() + b"\n")
            guest.close()
            reader.join(5)
        time.sleep(0.3)  # the hooks run on their own threads
        dirs = [self.a.paths.vm_dir, ra.paths.vm_dir]
        stored = "".join(open(os.path.join(d, f), encoding="utf-8").read() for d in dirs for f in os.listdir(d)
                         if os.path.isfile(os.path.join(d, f)))
        seen = stored + json.dumps(got, ensure_ascii=False) + json.dumps([self.a.meta, ra.meta], ensure_ascii=False)
        for sec in secrets:
            self.assertNotIn(sec, seen)
            self.assertNotIn(json.dumps(sec)[1:-1], seen)
        self.assertIn(V.HIDDEN, open(os.path.join(self.a.paths.vm_dir, "chat.jsonl"), encoding="utf-8").read())
        self.assertTrue(got and all(V.HIDDEN in json.dumps(m, ensure_ascii=False) for m in got))
        self.assertIn("in pieces " + V.HIDDEN, json.dumps(got, ensure_ascii=False))

    def test_findings_in_pieces_only_for_the_running_task(self):
        import base64
        import hashlib
        import json
        import socket
        import threading
        ra = self.mgr.create(name="r", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                             auto_stop=False, branch_prefix="agent/", profile="research")
        ra.meta["tasks"] = [{"id": f"t{i}", "state": "done"} for i in range(5)] + [{"id": "t5", "state": "running"}]
        got, refused = [], []
        self.mgr.hooks = {"on_outbox": lambda ag, m: got.append(m["task"])}
        ra.send = lambda m: refused.append(m["task"]) if m.get("type") == "outbox_refused" else None

        def piece(tid, data=b"[]", seq=0):
            return json.dumps({"type": "outbox_chunk", "task": tid, "seq": seq, "data": base64.b64encode(data).decode()}).encode() + b"\n"

        def end(tid, data=b"[]"):
            return json.dumps({"type": "outbox_end", "task": tid, "chunks": 1, "sha256": hashlib.sha256(data).hexdigest()}).encode() + b"\n"
        host, guest = socket.socketpair()
        reader = threading.Thread(target=self.mgr._control_reader, args=(ra, host), daemon=True)
        reader.start()
        guest.sendall(b"".join(piece(f"t{i}") for i in range(5)))  # finished tasks, or ones that never existed: refused
        guest.sendall(piece("nope") + piece("t5") + piece("t5") + end("t5"))  # cut off once, then whole
        guest.close()
        reader.join(5)
        time.sleep(0.2)
        self.assertEqual(sorted(refused), ["nope", "t0", "t1", "t2", "t3", "t4"])
        self.assertEqual(got, ["t5"])  # never silent, never stuck
        self.assertIn("cut off", ra.meta["error"])
        self.assertEqual(ra.outbox_parts, {})
        # a run that stopped halfway leaves nothing behind for the next one
        ra.outbox_parts["t5"] = {"seq": 3}
        host, guest = socket.socketpair()
        guest.close()
        self.mgr._control_reader(ra, host)
        self.assertEqual(ra.outbox_parts, {})

    def test_a_skipped_message_is_logged_without_its_content(self):
        import contextlib
        import io
        import socket
        self.a.hide = frozenset(['pa"ss\\wörd-123'])
        out = io.StringIO()
        host, guest = socket.socketpair()
        msgs = [b'{"type": "status", "task": ["t1"], "note": "pa\\"ss\\\\w\\u00f6rd-123"}', 'not json pa"ss\\wörd-123'.encode()]
        guest.sendall(b"".join(m + b"\n" for m in msgs))
        guest.close()
        with contextlib.redirect_stdout(out), mock.patch.object(self.mgr, "_on_guest", side_effect=TypeError("boom")):
            self.mgr._control_reader(self.a, host)
        lines = out.getvalue().splitlines()
        self.assertEqual([ln.split(" bytes")[0].rsplit(" ", 1)[1] for ln in lines], [str(len(m)) for m in msgs])
        self.assertEqual(["TypeError" in lines[0], "JSONDecodeError" in lines[1]], [True, True])
        self.assertNotIn("rd-123", out.getvalue())

    def test_a_panel_bug_in_a_handler_is_printed(self):
        import contextlib
        import io
        import socket
        err = io.StringIO()
        host, guest = socket.socketpair()
        guest.sendall(b'{"type": "status", "task": "t1", "state": "running"}\n')
        guest.close()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                mock.patch.object(self.mgr, "_on_guest", side_effect=KeyError("missing")):
            self.mgr._control_reader(self.a, host)
        self.assertIn("Traceback", err.getvalue())
        self.assertIn("KeyError", err.getvalue())


class MemoryUseTests(unittest.TestCase):
    def test_a_process_tree_in_mib(self):
        import tempfile
        from cilly.vm import tree_rss_mb
        with tempfile.TemporaryDirectory() as proc:
            def put(pid, status, kids=None):
                os.makedirs(f"{proc}/{pid}/task/{pid}")
                with open(f"{proc}/{pid}/status", "w") as f:
                    f.write(status)
                with open(f"{proc}/{pid}/task/{pid}/children", "w") as f:
                    f.write(kids or "")
            put(100, "Name:\tbwrap\nVmRSS:\t    2048 kB\n", "101 ")
            put(101, "Name:\tqemu\nVmRSS:\t  409600 kB\n", "102 100 ")  # a loop back is ignored
            put(102, "VmRSS:\tlots kB\n")  # unreadable: counts as nothing
            self.assertEqual(tree_rss_mb(100, proc), 402)
            self.assertEqual(tree_rss_mb(102, proc), None)
            self.assertEqual(tree_rss_mb(999, proc), None)

    def test_read_at_most_every_few_seconds(self):
        from cilly import vm as V
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp"}):
            a = V.Agent("a1b2c3d4", {})
        a.procs["qemu"] = mock.Mock(pid=os.getpid(), poll=lambda: None)
        with mock.patch.object(V, "tree_rss_mb", side_effect=[300, 500]) as rss:
            self.assertEqual((a.mem_used_mb(), a.mem_used_mb()), (300, 300))
            a.mem_read = (0.0, 300)
            self.assertEqual(a.mem_used_mb(), 500)
        self.assertEqual(rss.call_count, 2)


class ErrorTextTests(unittest.TestCase):
    def test_errors_an_agent_shows_are_in_sentence_case(self):
        """They show in the agent's banner."""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bad = []
        for name in ("vm.py", "server.py"):
            tree = ast.parse(open(os.path.join(root, "cilly", name)).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "set":
                    for kw in node.keywords:
                        v = kw.value
                        first = v.values[0] if isinstance(v, ast.JoinedStr) and v.values else v
                        if kw.arg == "error" and isinstance(first, ast.Constant) and isinstance(first.value, str) \
                                and first.value[:1].islower():
                            bad.append(f"{name}:{node.lineno} {first.value[:40]}")
        self.assertEqual(bad, [])


class BootcheckTests(unittest.TestCase):
    def test_each_variant_changes_what_its_label_says(self):
        from cilly.bootcheck import variants
        paths = VMPaths("/data/vms/abcd1234", "/run/user/1000/cilly/abcd1234")
        base = qemu_argv(qemu="qemu-system-x86_64", name="abcd1234", paths=paths, cpus=2, memory_mb=2048, python="/usr/bin/python3")
        v = variants(base, "/images/base.qcow2", paths)
        self.assertEqual(v[0][1], base)
        for label, argv in v[1:]:
            self.assertNotEqual(argv, base, label)  # a variant that boots the same command line tells you nothing
        self.assertEqual(len({tuple(a) for _, a in v}), len(v))
        vga = dict(v)["without a VGA card (-vga none)"]
        self.assertEqual(vga[vga.index("-vga") + 1], "none")
        self.assertNotIn("-vga", dict(v)["with QEMU's default devices (no -nodefaults, no -vga)"])


class StopWaitTests(unittest.TestCase):
    def test_quitting_waits_as_long_as_stop(self):
        import inspect
        import re
        from cilly import vm as V
        self.assertEqual(inspect.signature(V.Manager.shutdown_all).parameters["wait"].default, V.STOP_WAIT)
        self.assertIn("q.wait(STOP_WAIT)", inspect.getsource(V.Manager._stop_sync))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "contrib", "cilly.service")) as f:
            limit = int(re.search(r"^TimeoutStopSec=(\d+)$", f.read(), re.M).group(1))
        self.assertGreaterEqual(limit, V.STOP_WAIT + 15)  # systemd doesn't kill cilly while it still waits


class ManagerCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        os.makedirs(os.path.join(t, "run"))
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(t, "cfg"), "XDG_DATA_HOME": os.path.join(t, "data"),
                                                "XDG_RUNTIME_DIR": os.path.join(t, "run")})
        self.env.start()
        from cilly import config as C
        from cilly import vm as V
        self.V = V
        cfg = C.load()
        self.mgr = V.Manager(lambda: cfg)
        self.a = self.mgr.create(name="t", mode="persistent", repo=None, agent="claude", cpus=1, memory_mb=1024,
                                 auto_stop=False, branch_prefix="agent/")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class BootTimeTests(ManagerCase):
    """Where a start's time went, measured by the panel's clock; the VM can't add steps or move the ones it's had."""

    def test_steps_in_order_with_their_names(self):
        got = self.V.boot_times([("qemu", 2.0), ("start", 0.0), ("disk", 0.5), ("selftest", 6.0), ("ready", 9.25)])
        self.assertEqual(got, [["Preparing", 0.5], ["Disk", 1.5], ["QEMU and firmware", 4.0], ["Self-test", 3.2]])

    def test_a_vm_cant_add_or_repeat_steps(self):
        a = self.a
        a.meta["state"] = "booting"
        a.boot_marks = [("start", time.monotonic())]
        for phase in ("selftest", "selftest", "policy", "rm -rf /"):
            self.mgr._on_guest(a, {"type": "phase", "phase": phase})
        self.assertEqual([k for k, _ in a.boot_marks], ["start", "selftest", "policy"])
        a.meta["state"] = "ready"  # once it's up, phases change nothing
        self.mgr._on_guest(a, {"type": "phase", "phase": "memory"})
        self.assertEqual([k for k, _ in a.boot_marks], ["start", "selftest", "policy"])


class SummaryTests(ManagerCase):
    """The agent's own one-line summary (~/status.txt) is untrusted text from the VM."""

    def test_one_clean_short_line(self):
        c = self.V.clean_summary
        self.assertEqual(c("\n\n  \x1b[31mFixed\u200b the\tbuild \x07\nrm -rf /\nmore"), "Fixed the build")
        self.assertEqual(c("a\u2028b\u202ec\r\nd"), "abc")
        self.assertEqual(c("\x9b31mred\x80 text\x85next line"), "31mred text")
        long = c("word " * 40)
        self.assertEqual((len(long), long[-1]), (self.V.SUMMARY_MAX, "…"))
        for bad in (None, 5, ["a"], {"text": "a"}, "", " \n\t "):
            self.assertEqual(c(bad), "")

    def test_secrets_stay_hidden_even_when_split(self):
        self.a.hide = frozenset({"ghp_secretvalue123"})
        self.mgr._on_guest(self.a, {"type": "summary", "text": "pushed with ghp_secret\u200bvalue123, tests pass"})
        self.assertEqual(self.a.meta["summary"], "pushed with [hidden], tests pass")

    def test_a_flood_is_stored_at_most_once_per_gap(self):
        from unittest import mock
        saves = []
        save = self.a.save_meta
        self.a.save_meta = lambda: (saves.append(self.a.meta.get("summary")), save())
        with mock.patch.object(self.V, "SUMMARY_GAP", 0.4):
            for i in range(300):
                self.mgr._on_guest(self.a, {"type": "summary", "text": f"step {i}\nsecond line"})
            self.assertEqual(saves, ["step 0"])
            time.sleep(0.6)
        self.assertEqual(saves, ["step 0", "step 299"])  # the newest one, once the gap has passed
        with open(self.a.paths.meta) as f:
            self.assertEqual(__import__("json").load(f)["summary"], "step 299")

    def test_start_and_load_clear_the_idle_clock(self):
        from unittest import mock
        self.a.set(idle_since=1.0)
        with mock.patch.object(self.mgr, "_prepare", side_effect=RuntimeError("no image")):
            with self.assertRaises(self.V.VMError):
                self.mgr.start(self.a.id, allow=[], policy={})
        self.assertIsNone(self.a.meta["idle_since"])  # a new VM, a new clock
        self.a.set(state="ready", idle_since=2.0)  # as saved before the panel stopped
        again = self.V.Manager(self.mgr.cfg)
        self.assertEqual((again.get(self.a.id).meta["state"], again.get(self.a.id).meta["idle_since"]), ("stopped", None))

    def test_reset_to_clean_forgets_it(self):
        self.a.set(summary="Old work")
        self.mgr.reset_disk(self.a.id)
        self.assertIsNone(self.a.meta["summary"])


class TokensFileTests(ManagerCase):
    def test_a_task_and_a_start_writing_at_once(self):
        """A task's stand-ins and a VM's start both write the agent's tokens file: never at once."""
        self.a.tls = {"hosts": {"github.com": {"cert": "/c", "key": "/k"}}, "ca": ""}
        errors = []

        def write():
            try:
                for _ in range(150):
                    self.mgr.set_tokens(self.a, [{"stand_in": "ghp_cillyx", "value": "ghp_real1", "hosts": ["github.com"]}])
            except OSError as e:
                errors.append(e)
        ts = [threading.Thread(target=write) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        with open(self.a.paths.tokens_file) as f:
            self.assertEqual(json.load(f)["tokens"][0]["value"], "ghp_real1")
        self.mgr._drop_tls(self.a)
        self.assertFalse(os.path.exists(self.a.paths.tokens_file))


class StopTests(ManagerCase):
    def test_stop_finishes_when_the_vm_never_reads(self):
        import socket
        import subprocess
        host, guest = socket.socketpair()
        self.addCleanup(guest.close)  # the VM side, which never reads
        while True:  # its buffer is full, so a plain sendall would block for good
            try:
                host.send(b"x" * 65536, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break
        q = subprocess.Popen(["sleep", "60"])
        self.addCleanup(q.kill)
        self.a.ctl, self.a.procs["qemu"] = host, q
        start = time.monotonic()
        with mock.patch.object(self.V, "SEND_TIMEOUT", 0.5):
            self.mgr.stop(self.a.id)
            q.wait(10)  # the shutdown message times out, the link is dropped, and the VM is powered off
        self.assertLess(time.monotonic() - start, 10)
        self.assertIsNone(self.a.ctl)
        self.assertIsNone(self.a.meta.get("error"))  # you stopped it: no "lost contact"
        with self.assertRaises(VMError):
            self.a.send({"type": "shutdown"})

    def full_link(self):
        import socket
        host, guest = socket.socketpair()
        self.addCleanup(guest.close)
        self.addCleanup(host.close)
        self.a.ctl = host
        return guest

    def test_a_lock_that_comes_too_late_keeps_the_link(self):
        self.full_link()
        real = self.a.ctl_lock

        class Late:  # acquired, but only once the deadline has passed
            def acquire(self, timeout):
                time.sleep(timeout + 0.05)
                return real.acquire()

            def release(self):
                real.release()
        self.a.ctl_lock, link = Late(), self.a.ctl
        with mock.patch.object(self.V, "SEND_TIMEOUT", 0.2), self.assertRaisesRegex(VMError, "busy"):
            self.a.send({"type": "policy"})
        self.assertIs(self.a.ctl, link)  # nothing was sent, so nothing is dropped
        self.assertIsNone(self.a.meta.get("error"))

    def test_a_policy_bigger_than_the_socket_buffer_waits_for_the_boot(self):
        import threading
        guest, got = self.full_link(), []

        def boots_then_reads():
            time.sleep(1.5)  # longer than a send may usually take
            buf = b""
            while not buf.endswith(b"\n"):
                buf += guest.recv(1 << 20)
            got.append(buf)
        threading.Thread(target=boots_then_reads, daemon=True).start()
        hello = {"type": "hello", "policy": {"claude_md": "x" * (2 << 20)}}
        with mock.patch.object(self.V, "SEND_TIMEOUT", 0.5):
            self.a.send(hello, timeout=10)
        deadline = time.monotonic() + 10
        while not got and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(got[0]), len(self.V.json.dumps(hello)) + 1)

    def test_a_long_send_doesnt_hold_up_the_others(self):
        import threading
        self.full_link()  # the guest never reads
        self.a.meta["state"] = "booting"
        hello = threading.Thread(target=lambda: self.assertRaises(VMError, self.a.send,
                                                                  {"type": "hello", "policy": {"x": "y" * (2 << 20)}}, timeout=2))
        hello.start()
        time.sleep(0.2)
        start = time.monotonic()
        with mock.patch.object(self.V, "SEND_TIMEOUT", 0.5), self.assertRaisesRegex(VMError, "busy"):
            self.a.send({"type": "shutdown"})  # Stop's message gives up after its own deadline
        self.assertLess(time.monotonic() - start, 1.5)
        hello.join(5)
        self.assertEqual(self.a.meta["error"], self.V.LOST_CONTACT)  # outside Stop: you're told


class TaskRecordTests(ManagerCase):
    """What the panel keeps of each message you send, and the queue."""

    def send(self, text):
        return self.mgr.new_task(self.a.id, {"instructions": "prefix\n" + text, "summary": text}, queue=True)["id"]

    def test_a_long_message_is_kept_whole(self):
        long = "\n".join(f"line {i}: " + "word " * 20 for i in range(200))  # about 21,000 characters
        tid = self.send(long)
        rec = self.a.meta["tasks"][-1]
        self.assertEqual((len(rec["instructions"]), rec["full_message"]), (2000, True))
        path = os.path.join(self.a.paths.vm_dir, "messages", tid + ".txt")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(self.mgr.read_message(self.a.id, tid), long)
        short = self.send("fix the build")
        self.assertNotIn("full_message", self.a.meta["tasks"][-1])
        self.assertFalse(os.path.exists(os.path.join(self.a.paths.vm_dir, "messages", short + ".txt")))
        self.assertEqual(self.mgr.read_message(self.a.id, short), "fix the build")
        with self.assertRaises(self.V.VMError):
            self.mgr.read_message(self.a.id, "deadbeef")

    def test_the_file_goes_with_its_task_record(self):
        tid = self.send("x" * 3000)
        path = os.path.join(self.a.paths.vm_dir, "messages", tid + ".txt")
        for _ in range(self.V.TASKS_KEPT):
            self.send("next")
        self.assertNotIn(tid, [t["id"] for t in self.a.meta["tasks"]])
        self.assertFalse(os.path.exists(path))

    def test_cancel_a_queued_message(self):
        up = os.path.join(self.a.paths.vm_dir, "upload-1")
        with open(up, "w") as f:
            f.write("attached")
        tid = self.mgr.new_task(self.a.id, {"instructions": "later", "env": {"TOKEN": "secret"},
                                            "attachments": [{"name": "a.txt", "path": up}]}, queue=True)["id"]
        self.assertEqual([t["id"] for t in self.a.queue], [tid])  # stopped: it waits
        rec = self.mgr.cancel_task(self.a.id, tid)
        self.assertEqual((rec["state"], self.a.meta["tasks"][-1]["state"], self.a.queue), ("cancelled", "cancelled", []))
        self.assertFalse(os.path.exists(up))  # its upload is gone with it
        with self.assertRaisesRegex(self.V.VMError, "already cancelled"):
            self.mgr.cancel_task(self.a.id, tid)

    def test_cancel_after_it_started(self):
        tid = self.mgr.new_task(self.a.id, {"instructions": "now"}, queue=True)["id"]
        with self.a.lock:  # what the dispatch does when the agent is ready
            self.a.queue.clear()
            self.a.meta["tasks"][-1]["state"] = "running"
        with self.assertRaisesRegex(self.V.VMError, "It already started"):
            self.mgr.cancel_task(self.a.id, tid)
        self.assertEqual(self.a.meta["tasks"][-1]["state"], "running")

    def test_the_queue_order_is_public(self):
        first, now = self.send("first"), self.send("now")
        self.a.meta["state"] = "booting"
        self.mgr.send_now(self.a.id, now)
        self.assertEqual(self.a.public()["queue"], [now, first])
        from unittest import mock
        self.a.meta["state"] = "ready"
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running
        with mock.patch.object(self.a, "send", lambda m: None):
            self.mgr.dispatch_next(self.a)
            self.a.meta["state"] = "ready"
            self.mgr.dispatch_next(self.a)
        order = {t["id"]: t.get("order") for t in self.a.meta["tasks"]}
        self.assertLess(order[now], order[first])  # also within one second, which "started" can't tell apart

    def test_a_task_the_vm_ends_at_once_stays_ended(self):
        from unittest import mock
        self.a.meta["state"] = "ready"
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running

        def instant(msg):  # the VM's answer arrives before send() returns
            self.mgr._on_guest(self.a, {"type": "status", "task": msg["id"], "state": "done", "agent_rc": 0})
        with mock.patch.object(self.a, "send", instant), mock.patch.object(self.mgr, "_after_task"):
            tid = self.mgr.new_task(self.a.id, {"instructions": "quick", "timeout_s": 60}, queue=True)["id"]
        rec = next(t for t in self.a.meta["tasks"] if t["id"] == tid)
        self.assertEqual((rec["state"], self.a.meta["state"]), ("done", "ready"))
        self.assertEqual(self.a.backstops, {})  # nothing left to end it later

    def test_sending_a_task_keeps_the_time_limit_of_one_still_running(self):
        """However the agent came to be ready for the next one while the first still runs."""
        import threading
        from unittest import mock
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running
        self.a.meta["state"] = "ready"
        fired = threading.Event()
        with mock.patch.object(self.a, "send"), mock.patch.object(self.V, "BACKSTOP_GRACE", 0), \
                mock.patch.object(self.mgr, "_backstop", lambda a, tid: fired.set() if tid == first else None):
            first = self.mgr.new_task(self.a.id, {"instructions": "long", "timeout_s": 1}, queue=True)["id"]
            self.a.meta["state"] = "ready"
            self.mgr.new_task(self.a.id, {"instructions": "next", "timeout_s": 60}, queue=True)
            self.assertTrue(fired.wait(3))
            self.mgr._disarm_backstop(self.a)

    def test_the_time_limit_never_stops_the_vm_under_the_next_task(self):
        """The task's end may arrive right as the backstop makes its last check."""
        import threading
        from unittest import mock
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running
        self.a.meta["state"] = "ready"
        sent, checks, racer = [], [], []
        real = self.mgr._still_running

        def racing(a, tid):
            checks.append(real(a, tid))
            if len(checks) == 2:  # the last check
                racer.append(threading.Thread(target=self.mgr._on_guest, args=(a, {"type": "status", "task": tid, "state": "done"})))
                racer[0].start()
                racer[0].join(0.5)
            return checks[-1]
        with mock.patch.object(self.a, "send", lambda msg, timeout=None: sent.append(msg)), \
                mock.patch.object(self.mgr, "_after_task"), mock.patch.object(self.V, "BACKSTOP_WAIT", 0), \
                mock.patch.object(self.mgr, "_stop_sync") as stop:
            first = self.mgr.new_task(self.a.id, {"instructions": "long"}, queue=True)["id"]
            nxt = self.mgr.new_task(self.a.id, {"instructions": "next"}, queue=True)["id"]
            with mock.patch.object(self.mgr, "_still_running", racing):
                self.mgr._backstop(self.a, first)
            racer[0].join(5)
            time.sleep(0.1)  # the stop's thread
        nxt_sent = any(m.get("type") == "task" and m["id"] == nxt for m in sent)
        self.assertTrue(stop.called or nxt_sent)
        self.assertFalse(stop.called and nxt_sent, "the VM was stopped under the next task")

    def test_the_time_limit_of_a_deleted_agent(self):
        from unittest import mock
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # still looks running
        self.a.meta["state"] = "ready"
        with mock.patch.object(self.a, "send"), mock.patch.object(self.V, "BACKSTOP_WAIT", 0), \
                mock.patch.object(self.mgr, "_stop_sync") as stop:
            tid = self.mgr.new_task(self.a.id, {"instructions": "long"}, queue=True)["id"]
            self.mgr.delete(self.a.id)
            self.mgr._backstop(self.a, tid)  # the timer's thread: nothing to raise, nothing to stop
        self.assertFalse(stop.called)

    def test_only_the_running_task_reports(self):
        from unittest import mock
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running
        self.a.meta.update(state="ready", repo="x/y")
        sent = []
        status = lambda tid, state, **kw: self.mgr._on_guest(self.a, dict(type="status", task=tid, state=state, **kw))  # noqa: E731
        with mock.patch.object(self.a, "send", lambda msg, timeout=None: sent.append(msg)), \
                mock.patch.object(self.mgr, "_after_task"):
            run = self.send("runs")
            queued = self.send("waits")
            rec = {t["id"]: t for t in self.a.meta["tasks"]}
            for tid in ("0badc0de", queued):  # made up, or not sent yet
                status(tid, "done", agent_rc=0)
                status(tid, "running", pr="https://github.com/x/y/pull/1")
            self.assertEqual((rec[run]["state"], rec[queued]["state"], self.a.meta["state"]), ("running", "queued", "busy"))
            self.assertIsNone(self.a.meta["pr"])
            self.a.meta["state"] = "stopping"  # Stop or Restart: the runner ends its task before it powers off
            status(run, "interrupted", agent_rc=-15)
            self.assertEqual((rec[run]["state"], self.a.meta["state"], [m["id"] for m in sent]), ("interrupted", "stopping", [run]))
            self.mgr._after_stop(self.a)
            self.a.meta["state"] = "ready"  # started again
            for tid in (run, queued):  # what the last run had: ended, and cut off by the stop
                status(tid, "running")
                status(tid, "done", agent_rc=0)
            self.assertEqual((rec[run]["state"], rec[queued]["state"]), ("interrupted", "interrupted"))
            self.assertNotIn("time_limit_stop", rec[queued])  # Stop, not the time limit
            self.assertEqual((self.a.meta["state"], len(sent)), ("ready", 1))

    def test_move_up_swaps_with_the_one_before(self):
        first, second, third = self.send("first"), self.send("second"), self.send("third")
        self.assertEqual(self.mgr.move_up(self.a.id, third), 1)
        self.assertEqual(self.a.public()["queue"], [first, third, second])
        self.assertEqual(self.mgr.move_up(self.a.id, third), 0)
        self.assertEqual(self.mgr.move_up(self.a.id, third), 0)  # already next: stays there
        self.assertEqual(self.a.public()["queue"], [third, first, second])
        self.mgr.cancel_task(self.a.id, first)
        with self.assertRaisesRegex(self.V.VMError, "It already started"):
            self.mgr.move_up(self.a.id, first)
        with self.assertRaisesRegex(self.V.VMError, "no such task"):
            self.mgr.move_up(self.a.id, "0badc0de")

    def test_send_now_when_nothing_can_be_stopped(self):
        from unittest import mock
        first, now = self.send("first"), self.send("now")
        self.a.meta["state"] = "booting"
        self.assertEqual(self.mgr.send_now(self.a.id, now), "moved")
        self.assertEqual([t["id"] for t in self.a.queue], [now, first])
        self.a.meta["state"] = "busy"  # but the interrupt fails, or finds the task already over
        for err in ("The VM didn't answer, so the task wasn't stopped.", "Nothing is running right now."):
            with mock.patch.object(self.mgr, "interrupt", side_effect=self.V.VMError(err)):
                self.assertEqual(self.mgr.send_now(self.a.id, first), "moved")
        self.assertEqual([t["id"] for t in self.a.queue], [first, now])

    def test_cancel_racing_the_dispatch(self):
        import threading
        from unittest import mock
        sent, errors = [], []
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running

        def catching(fn):  # a thread's exception would only be printed
            def run():
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            return run
        outcomes = set()
        with mock.patch.object(self.mgr, "_dispatch", lambda a, task: sent.append(task["id"])):
            # each order once for certain, then both at the same moment
            for order in ["cancel", "ready"] + [None] * 200:
                self.a.meta["state"] = "booting"  # it waits in the queue
                tid = self.mgr.new_task(self.a.id, {"instructions": "x"}, queue=True)["id"]
                cancelled, first_done, start = [], threading.Event(), threading.Barrier(2)

                def cancel():
                    try:
                        self.mgr.cancel_task(self.a.id, tid)
                        cancelled.append(tid)
                    except self.V.VMError:
                        pass

                def ready():
                    self.a.meta["state"] = "ready"
                    self.mgr.dispatch_next(self.a)

                def ordered(name, fn):
                    def run():
                        if order is None:
                            start.wait()
                        elif order != name:
                            first_done.wait(5)
                        try:
                            fn()
                        finally:
                            if order == name:
                                first_done.set()
                    return catching(run)
                ts = [threading.Thread(target=ordered("cancel", cancel)), threading.Thread(target=ordered("ready", ready))]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                self.assertEqual(errors, [])
                self.assertEqual((tid in sent) + (tid in cancelled), 1)  # sent or cancelled, never both or neither
                outcomes.add("cancelled" if tid in cancelled else "sent")
                if order:
                    self.assertEqual(outcomes, {"cancelled"} if order == "cancel" else {"cancelled", "sent"}, order)
        self.assertEqual(outcomes, {"cancelled", "sent"})


class DeliverTests(ManagerCase):
    """Send now into a running session: what the panel takes from the VM, and when it asks."""

    def running(self, live=True):
        self.a.can_deliver = True
        self.a.procs["qemu"] = mock.Mock(poll=lambda: None)  # looks running
        self.a.meta["state"] = "busy"
        rec = {"id": "aaaa0001", "state": "running", "instructions": "go", "model": None, "effort": None}
        if live:
            rec["live"] = True
        self.a.meta["tasks"] = [rec]
        return rec

    def queued(self, **kw):
        return self.mgr.new_task(self.a.id, dict({"instructions": "prefix\nmore", "summary": "more", "continue": True,
                                                  "env": {"GH_TOKEN": "secret-token"}}, **kw), queue=True)

    def test_only_a_plain_message_for_the_same_conversation_goes_in(self):
        live = self.running()
        ok = {"instructions": "x", "continue": True, "model": None, "effort": None}
        self.assertTrue(self.V.deliverable(ok, live))
        for bad in ({"attachments": [{"name": "a"}]}, {"attach": "me/repo"}, {"continue": False}, {"resume": "s1"},
                    {"new_branch": True}, {"model": "opus"}, {"effort": "max"}):
            self.assertFalse(self.V.deliverable(dict(ok, **bad), live), bad)

    def test_the_vm_cant_make_a_task_take_messages_or_answer_for_another(self):
        rec = self.running(live=False)
        self.a.can_deliver = False  # its runner never said it can
        self.mgr._on_guest(self.a, {"type": "live", "task": rec["id"]})
        self.assertNotIn("live", rec)
        self.a.can_deliver = True
        for msg in ({"type": "live", "task": "bbbb0002"}, {"type": "live", "task": [rec["id"]]}):
            self.mgr._on_guest(self.a, msg)
        self.assertNotIn("live", rec)
        with mock.patch.object(self.a, "save_meta") as save:
            for _ in range(3):
                self.mgr._on_guest(self.a, {"type": "live", "task": rec["id"]})
        self.assertTrue(rec["live"])
        self.assertEqual(save.call_count, 1)  # only when it changes, however often the VM says it
        w = self.a.delivers["cccc0003"] = {"event": threading.Event(), "result": None, "task": rec["id"]}
        for msg in ({"id": "cccc0003", "task": "other", "result": "delivered"}, {"id": "cccc0003", "task": rec["id"], "result": "yes"},
                    {"id": "dddd0004", "task": rec["id"], "result": "delivered"}, {"id": ["cccc0003"], "task": rec["id"], "result": "delivered"}):
            self.mgr._on_guest(self.a, dict(msg, type="deliver_ack"))
        self.assertIsNone(w["result"])
        self.a.meta["tasks"].append({"id": "eeee0005", "state": "done", "instructions": "x"})
        self.mgr._on_chat(self.a, {"type": "chat", "task": rec["id"], "events": [{"k": "you", "msg": "eeee0005", "text": "I said so"}]})
        self.assertEqual(self.mgr.read_chat(self.a.id)["events"], [])  # only the panel writes your messages into the chat

    def test_send_now_delivers_and_keeps_no_secret(self):
        live = self.running()
        t = self.queued()
        sent = []

        def send(msg, timeout=None):
            sent.append(msg)
            threading.Timer(0.05, self.mgr._on_guest, (self.a, {"type": "deliver_ack", "task": live["id"], "id": t["id"],
                                                                "result": "delivered"})).start()
        with mock.patch.object(self.a, "send", send):
            self.assertEqual(self.a.public()["deliver"], [t["id"]])
            self.assertEqual(self.mgr.send_now(self.a.id, t["id"]), "delivered")
        self.assertEqual(sent, [{"type": "deliver", "task": live["id"], "id": t["id"], "text": "more"}])  # your words, no secrets
        self.assertEqual(t["env"], {})
        rec = self.a.meta["tasks"][-1]
        self.assertEqual((rec["state"], rec["into"], self.a.queue), ("delivered", live["id"], []))
        ev = self.mgr.read_chat(self.a.id)["events"]
        self.assertEqual([(e["k"], e["task"], e["msg"], e["text"]) for e in ev], [("you", live["id"], t["id"], "more")])

    def test_a_second_send_now_while_the_first_waits(self):
        """The running task can take seconds to answer. A second click isn't told the message started."""
        live = self.running()
        t = self.queued()
        got, answered = [], threading.Event()

        def send(msg, timeout=None):
            if msg.get("type") != "deliver":
                return
            try:
                self.mgr.send_now(self.a.id, t["id"])
            except VMError as e:
                got.append(str(e))
            self.mgr._on_guest(self.a, {"type": "deliver_ack", "task": live["id"], "id": t["id"], "result": "delivered"})
            answered.set()
        with mock.patch.object(self.a, "send", send):
            self.assertEqual(self.mgr.send_now(self.a.id, t["id"]), "delivered")
        self.assertTrue(answered.is_set())
        self.assertEqual(got, ["It's on its way into the running task. It runs next if Claude doesn't take it there."])

    def test_a_message_claude_code_didnt_answer_is_reported_once(self):
        live = self.running()
        self.a.meta["tasks"] += [{"id": "bbbb0002", "state": "delivered", "into": live["id"], "instructions": "do\nthis " + "x" * 80},
                                 {"id": "cccc0003", "state": "done", "instructions": "ran"}]
        for msg in ({"id": "bbbb0002", "task": "other", "state": "refused"},  # not where it went
                    {"id": "cccc0003", "task": live["id"], "state": "refused"},  # not a delivered message
                    {"id": "bbbb0002", "task": live["id"], "state": "completed"}, {"id": "bbbb0002", "task": live["id"], "state": ["refused"]},
                    {"id": ["bbbb0002"], "task": live["id"], "state": "refused"}):
            self.mgr._on_guest(self.a, dict(msg, type="delivery"))
        self.assertEqual(self.mgr.read_chat(self.a.id)["events"], [])
        for _ in range(2):
            self.mgr._on_guest(self.a, {"type": "delivery", "id": "bbbb0002", "task": live["id"], "state": "refused"})
        ev = self.mgr.read_chat(self.a.id)["events"]
        self.assertEqual([(e["k"], e["task"]) for e in ev], [("status", live["id"])])
        self.assertEqual(ev[0]["text"], 'Claude Code refused your message "do this ' + "x" * 51 + '…", so Claude won\'t read it. '
                                        "Send it again if you still need it.")
        self.assertEqual(self.a.meta["tasks"][1]["lost"], "refused")

    def test_when_the_run_cant_take_it(self):
        live = self.running()
        for answer, result, interrupted in (("unsupported", "not_delivered", False), ("closing", "finishing", False),
                                            (None, "not_delivered", False)):
            t = self.queued()
            calls = []

            def send(msg, timeout=None, answer=answer, t=t):
                if answer:
                    self.mgr._on_guest(self.a, {"type": "deliver_ack", "task": live["id"], "id": t["id"], "result": answer})
            with mock.patch.object(self.a, "send", send), mock.patch.object(self.V, "DELIVER_WAIT", 0.2), \
                    mock.patch.object(self.mgr, "interrupt", lambda aid: calls.append(aid) or "stopping"):
                self.assertEqual(self.mgr.send_now(self.a.id, t["id"]), result, answer)
            self.assertEqual(bool(calls), interrupted, answer)
            self.assertEqual(self.a.queue[0]["id"], t["id"])  # first in line, not lost
            self.assertEqual(self.a.meta["tasks"][-1]["state"], "queued")
            self.assertEqual(self.mgr.read_chat(self.a.id)["events"], [])
        # the agent stopped while it waited: the message is no one's to run, so its secrets go
        for stop in ("vm gone", "record interrupted"):
            t = self.queued()

            def send(msg, timeout=None, stop=stop, t=t):
                if stop == "vm gone":
                    self.a.procs.pop("qemu")
                else:
                    self.a.meta["tasks"][-1]["state"] = "interrupted"
            with mock.patch.object(self.a, "send", send), mock.patch.object(self.V, "DELIVER_WAIT", 0.2), \
                    mock.patch.object(self.mgr, "interrupt", lambda aid: self.fail("stopped a task")):
                with self.assertRaisesRegex(self.V.VMError, "stopped before"):
                    self.mgr.send_now(self.a.id, t["id"])
            self.assertNotIn(t["id"], [q["id"] for q in self.a.queue], stop)
            self.assertEqual(t["env"], {}, stop)
            self.a.procs["qemu"] = mock.Mock(poll=lambda: None)
        # without "live" on the running task (an older Claude Code), it's the interrupt, as before
        del live["live"]
        t = self.queued()
        with mock.patch.object(self.a, "send", lambda m, timeout=None: self.fail("asked the VM to deliver")), \
                mock.patch.object(self.mgr, "interrupt", lambda aid: "stopping"):
            self.assertEqual(self.mgr.send_now(self.a.id, t["id"]), "stopping")
        # unless you weren't asked: then it waits first in line
        for t in (self.queued(), self.queued(attachments=[])):
            with mock.patch.object(self.a, "send", lambda m, timeout=None: self.fail("asked the VM to deliver")), \
                    mock.patch.object(self.mgr, "interrupt", lambda aid: self.fail("stopped a task")):
                self.assertEqual(self.mgr.send_now(self.a.id, t["id"], deliver_only=True), "not_delivered")
            self.assertEqual(self.a.queue[0]["id"], t["id"])


class AtomicWriteTests(unittest.TestCase):
    def test_two_threads_saving_at_once(self):
        """meta.json is saved from several threads; with one shared temporary name, one write could
        take the other's file, or fail with "File exists"."""
        import tempfile
        from cilly.vm import _atomic_write
        with tempfile.TemporaryDirectory() as d:
            path, errors = os.path.join(d, "meta.json"), []

            def save(i):
                for n in range(200):
                    try:
                        _atomic_write(path, json.dumps({"by": i, "n": n}).encode())
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)
            threads = [threading.Thread(target=save, args=(i,)) for i in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(errors, [])
            with open(path) as f:
                self.assertEqual(json.load(f)["n"], 199)  # whole, the last write of one of them
            self.assertEqual(os.listdir(d), ["meta.json"])  # no temporary file left behind
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            _atomic_write(path, b"x", 0o644)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        with self.assertRaises(FileNotFoundError):  # a deleted agent: save_meta counts on this
            _atomic_write(os.path.join(d, "gone", "meta.json"), b"x")
