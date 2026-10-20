import io
import json
import os
import tarfile
import tempfile
import unittest

from cilly.inject import InjectError, build_task_disk, check_allowed_root, scan


class InjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.base, "home")
        os.makedirs(os.path.join(self.home, ".ssh"))
        with open(os.path.join(self.home, ".ssh", "id_ed25519"), "w") as f:
            f.write("-----BEGIN OPENSSH PRIVATE KEY-----\nxxx\n")
        self.root = os.path.join(self.home, "proj")
        os.makedirs(os.path.join(self.root, "docs", "sub"))
        with open(os.path.join(self.root, "README.md"), "w") as f:
            f.write("hello\n")
        with open(os.path.join(self.root, "docs", "a.txt"), "w") as f:
            f.write("a\n")
        with open(os.path.join(self.root, "run.sh"), "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(os.path.join(self.root, "run.sh"), 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self):
        return scan(self.root, home=self.home)

    def test_basic_manifest(self):
        m = self._scan()
        rels = {e.rel: e.kind for e in m.entries}
        self.assertEqual(rels["README.md"], "file")
        self.assertEqual(rels["docs"], "dir")
        self.assertEqual(rels["docs/sub"], "dir")
        self.assertEqual(m.file_count, 3)
        self.assertFalse(m.needs_ack)

    def test_symlink_outside_rejected(self):
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"), os.path.join(self.root, "key"))
        os.symlink("../../.ssh", os.path.join(self.root, "docs", "ssh"))
        m = self._scan()
        rels = {e.rel for e in m.entries}
        self.assertNotIn("key", rels)
        self.assertNotIn("docs/ssh", rels)
        codes = {(i.rel, i.code) for i in m.issues}
        self.assertIn(("key", "symlink_outside"), codes)
        self.assertIn(("docs/ssh", "symlink_outside"), codes)
        self.assertTrue(m.needs_ack)

    def test_symlink_inside_rewritten_relative(self):
        os.symlink(os.path.join(self.root, "docs", "a.txt"), os.path.join(self.root, "abs-link"))
        os.symlink("docs/a.txt", os.path.join(self.root, "rel-link"))
        m = self._scan()
        links = {e.rel: e.link_target for e in m.entries if e.kind == "symlink"}
        self.assertEqual(links["abs-link"], "docs/a.txt")
        self.assertEqual(links["rel-link"], "docs/a.txt")

    def test_symlink_chain_escaping_rejected(self):
        os.symlink(self.home, os.path.join(self.root, "docs", "h"))
        os.symlink("docs/h/.ssh/id_ed25519", os.path.join(self.root, "sneaky"))
        m = self._scan()
        rels = {e.rel for e in m.entries}
        self.assertNotIn("sneaky", rels)
        self.assertNotIn("docs/h", rels)

    def test_dangling_symlink_rejected(self):
        os.symlink("nope", os.path.join(self.root, "dangling"))
        m = self._scan()
        self.assertNotIn("dangling", {e.rel for e in m.entries})

    def test_hardlink_flagged(self):
        os.link(os.path.join(self.home, ".ssh", "id_ed25519"), os.path.join(self.root, "innocent.txt"))
        m = self._scan()
        codes = {(i.rel, i.code) for i in m.issues}
        self.assertIn(("innocent.txt", "hardlink"), codes)
        self.assertIn(("innocent.txt", "secret_content"), codes)

    def test_fifo_skipped(self):
        os.mkfifo(os.path.join(self.root, "pipe"))
        m = self._scan()  # must not block
        self.assertNotIn("pipe", {e.rel for e in m.entries})
        self.assertIn(("pipe", "special_file"), {(i.rel, i.code) for i in m.issues})

    def test_secret_detection(self):
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("X=1\n")
        with open(os.path.join(self.root, "docs", "notes.md"), "w") as f:
            f.write("token: ghp_" + "a" * 36 + "\n")
        os.makedirs(os.path.join(self.root, ".git"))
        with open(os.path.join(self.root, ".git", "config"), "w") as f:
            f.write('[remote "origin"]\n\turl = https://me:secret@github.com/x/y\n')
        m = self._scan()
        codes = {(i.rel, i.code) for i in m.issues}
        self.assertIn((".env", "secret_name"), codes)
        self.assertIn(("docs/notes.md", "secret_content"), codes)
        self.assertIn((".git", "git_dir"), codes)
        self.assertIn((".git/config", "git_credentials"), codes)
        self.assertTrue(m.needs_ack)

    def test_refuses_broad_roots(self):
        for p in ("/", "/home", "/etc", self.home, self.base, os.path.join(self.home, ".ssh"),
                  os.path.join(self.home, ".config", "x"), os.path.join(self.home, ".claude"),
                  os.path.join(self.home, ".claude", "projects")):
            with self.assertRaises(InjectError, msg=p):
                check_allowed_root(p, self.home)
        with self.assertRaises(InjectError):
            check_allowed_root("relative/path", self.home)

    def test_root_symlink_to_home_refused(self):
        link = os.path.join(self.base, "looks-harmless")
        os.symlink(self.home, link)
        with self.assertRaises(InjectError):
            check_allowed_root(link, self.home)

    def test_task_disk_roundtrip(self):
        os.symlink("docs/a.txt", os.path.join(self.root, "rel-link"))
        m = self._scan()
        out = os.path.join(self.base, "task.tar")
        size = build_task_disk(out, {"task_id": "t1"}, m)
        self.assertEqual(size % 512, 0)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        with tarfile.open(out) as tf:
            names = tf.getnames()
            self.assertEqual(json.load(tf.extractfile("task.json"))["task_id"], "t1")
            self.assertIn("inject/README.md", names)
            self.assertIn("inject/docs/sub", names)
            link = tf.getmember("inject/rel-link")
            self.assertTrue(link.issym())
            self.assertEqual(link.linkname, "docs/a.txt")
            sh = tf.getmember("inject/run.sh")
            self.assertEqual(sh.mode, 0o755)
            self.assertEqual(sh.uid, 0)
            # the guest extracts with the "data" filter; make sure it accepts everything
            dest = os.path.join(self.base, "extract")
            tf.extractall(dest, filter="data")
        with open(os.path.join(dest, "inject", "README.md")) as f:
            self.assertEqual(f.read(), "hello\n")

    def test_task_disk_without_manifest(self):
        out = os.path.join(self.base, "task.tar")
        build_task_disk(out, {"a": 1}, None)
        with tarfile.open(out) as tf:
            self.assertEqual(tf.getnames(), ["task.json"])

    def test_change_after_scan_aborts(self):
        m = self._scan()
        with open(os.path.join(self.root, "README.md"), "a") as f:
            f.write("more\n")
        with self.assertRaises(InjectError):
            build_task_disk(os.path.join(self.base, "t.tar"), {}, m)

    def test_swap_dir_for_symlink_after_scan_aborts(self):
        m = self._scan()
        os.rename(os.path.join(self.root, "docs"), os.path.join(self.base, "docs-moved"))
        os.symlink(os.path.join(self.home, ".ssh"), os.path.join(self.root, "docs"))
        with self.assertRaises((InjectError, OSError)):
            build_task_disk(os.path.join(self.base, "t.tar"), {}, m)

    def test_many_directories_with_low_fd_limit(self):
        import subprocess
        import sys
        for i in range(300):
            os.makedirs(os.path.join(self.root, "many", f"d{i:03}", "x"))
        code = (
            "import resource, sys; resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64));"
            "from cilly.inject import scan, build_task_disk;"
            f"m = scan({self.root!r}, home={self.home!r});"
            f"build_task_disk({os.path.join(self.base, 'many.tar')!r}, {{}}, m); print('ok')"
        )
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_existing_output_not_overwritten(self):
        out = os.path.join(self.base, "exists.tar")
        open(out, "w").close()
        with self.assertRaises(FileExistsError):
            build_task_disk(out, {}, None)


if __name__ == "__main__":
    unittest.main()
