import io
import os
import shutil
import tarfile
import tempfile
import unittest

from cilly.exports import ExportError, check_destination, safe_extract_tar

# a tree from a VM with git repositories in it: none of them may land on the host
D = "dir"
GIT_TREE = [("keep.txt", b"k"), (".git", D), (".git/config", b"[core]\n\tfsmonitor = touch /tmp/pwned\n"),
            (".GIT/config", b"[core]\n\tfsmonitor = x\n"), ("sub", D), ("sub/keep.md", b"m"),
            ("sub/.git/config", b"[core]\n\tfsmonitor = x\n"), ("sub/.git/HEAD", b"ref: refs/heads/main\n"),
            ("work/.git", b"gitdir: /tmp/elsewhere\n"), ("work/main.c", b"int main;"),
            ("bare/HEAD", b"ref: refs/heads/main\n"), ("bare/config", b"[core]\n\tbare = true\n\tpager = touch /tmp/pwned\n"),
            ("bare/objects", D), ("bare/refs", D), ("bare/refs/heads", D), ("bare/description", b"x"),
            ("notbare/HEAD", b"just a file called HEAD"), ("notbare/refs", D)]
KEPT = ["keep.txt", "notbare/HEAD", "sub/keep.md", "work/main.c"]
# a worktree-style repository: HEAD, and a commondir file pointing at objects, refs and config
WORKTREE = [("wt/HEAD", b"ref: refs/heads/main\n"), ("wt/commondir", b"c\n"), ("wt/c/objects/info", b"x"),
            ("wt/c/refs/heads/main", b"0" * 40 + b"\n"), ("wt/c/config", b"[core]\n\tpager = touch /tmp/cilly-pwned\n"),
            ("elsewhere/HEAD", b"ref: refs/heads/main\n"), ("elsewhere/commondir", b"../../earlier-export/wt/c\n"),
            ("detached/HEAD", b"1" * 64 + b"\n"), ("notes/HEAD", b"HEAD of the queue: fix the build\n"), ("readme.md", b"r")]
WT_KEPT = ["notes/HEAD", "readme.md"]


def make_tar(path, entries):
    with tarfile.open(path, "w") as tar:
        for e in entries:
            ti = tarfile.TarInfo(e["name"])
            ti.type = e.get("type", tarfile.REGTYPE)
            ti.mode = e.get("mode", 0o644)
            ti.linkname = e.get("link", "")
            data = e.get("data", b"")
            ti.size = len(data) if ti.type == tarfile.REGTYPE else 0
            tar.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self.tmp.name)
        self.dest = os.path.join(self.d, "dest")
        os.mkdir(self.dest)
        self.tar = os.path.join(self.d, "x.tar")

    def tearDown(self):
        self.tmp.cleanup()

    def extract(self, entries, **kw):
        make_tar(self.tar, entries)
        return safe_extract_tar(self.tar, self.dest, "sub", max_bytes=kw.get("max_bytes", 10_000),
                                max_files=kw.get("max_files", 100))

    def assert_rejected(self, entries, msg, **kw):
        with self.assertRaisesRegex(ExportError, msg):
            self.extract(entries, **kw)
        self.assertEqual(os.listdir(self.dest), [], "a rejected export must leave nothing behind")

    def test_git_repositories_are_left_out(self):
        res = self.extract([{"name": n, "type": tarfile.DIRTYPE} if d == D else {"name": n, "data": d} for n, d in GIT_TREE])
        self.assertEqual(sorted(res["files"]), KEPT)
        self.assertEqual(res["left_out"], 5)  # .git, .GIT, sub/.git, work/.git and the bare repository
        out = os.path.join(self.dest, "sub")
        found = [os.path.relpath(os.path.join(dp, n), out) for dp, dn, fn in os.walk(out) for n in fn + dn]
        self.assertEqual([f for f in found if "git" in f.lower() or f.startswith("bare")], [])

    def test_other_spellings_of_git(self):
        for i, name in enumerate((".git\u200c", ".Git.", "GIT~1", ".git ")):  # git or a case-insensitive filesystem reads these as .git
            make_tar(self.tar, [{"name": f"{name}/config", "data": b"x"}, {"name": "ok", "data": b"y"}])
            res = safe_extract_tar(self.tar, self.dest, f"s{i}", max_bytes=100, max_files=10)
            self.assertEqual((res["files"], res["left_out"]), (["ok"], 1), repr(name))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_takes_a_commondir_folder_for_a_repository(self):
        import subprocess
        d = os.path.join(self.d, "shape")
        for n, data in WORKTREE[:5]:
            os.makedirs(os.path.dirname(os.path.join(d, n)), exist_ok=True)
            with open(os.path.join(d, n), "wb") as f:
                f.write(data)
        os.makedirs(os.path.join(d, "wt", "c", "refs", "tags"))
        r = subprocess.run(["git", "-C", os.path.join(d, "wt"), "rev-parse", "--git-dir"], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "GIT_CEILING_DIRECTORIES": self.d})
        self.assertEqual(r.stdout.strip(), ".", r.stderr)  # the threat is real: git runs this folder's config

    def test_any_folder_with_a_valid_head_is_left_out(self):
        res = self.extract([{"name": n, "data": d} for n, d in WORKTREE])
        self.assertEqual((sorted(res["files"]), res["left_out"]), (WT_KEPT, 3))  # wt, elsewhere and detached

    def test_a_whole_bare_repository_is_left_out(self):
        res = self.extract([{"name": "HEAD", "data": b"ref: refs/heads/main\n"}, {"name": "config", "data": b"x"},
                            {"name": "objects", "type": tarfile.DIRTYPE}, {"name": "refs/heads/main", "data": b"0" * 40}])
        self.assertEqual((res["files"], res["left_out"]), ([], 1))

    def test_good(self):
        res = self.extract([{"name": "a", "type": tarfile.DIRTYPE}, {"name": "a/b.sh", "data": b"echo", "mode": 0o4755},
                            {"name": "./c.txt", "data": b"c"}])
        self.assertEqual(sorted(res["files"]), ["a/b.sh", "c.txt"])
        self.assertEqual(res["bytes"], 5)
        st = os.stat(os.path.join(self.dest, "sub", "a", "b.sh"))
        self.assertEqual(st.st_mode & 0o7777, 0o644)  # no exec, setuid or sticky bits

    def test_never_overwrites(self):
        os.mkdir(os.path.join(self.dest, "sub"))
        with self.assertRaises(FileExistsError):
            self.extract([{"name": "c.txt", "data": b"c"}])

    def test_links_and_devices_rejected(self):
        self.assert_rejected([{"name": "l", "type": tarfile.SYMTYPE, "link": "/etc/passwd"}], "not a regular file")
        self.assert_rejected([{"name": "h", "type": tarfile.LNKTYPE, "link": "x"}], "not a regular file")
        self.assert_rejected([{"name": "d", "type": tarfile.CHRTYPE}], "not a regular file")
        self.assert_rejected([{"name": "f", "type": tarfile.FIFOTYPE}], "not a regular file")

    def test_path_escapes_rejected(self):
        for name in ("../x", "a/../../x", "/etc/x", "a\\..\\x", ".."):
            self.assert_rejected([{"name": name, "data": b"x"}], "unsafe path")
        self.assertFalse(os.path.exists(os.path.join(self.d, "x")))

    def test_file_used_as_folder_and_duplicates_rejected(self):
        self.assert_rejected([{"name": "a", "data": b"x"}, {"name": "a/b", "data": b"y"}], "file name as a folder")
        self.assert_rejected([{"name": "a", "data": b"x"}, {"name": "a", "data": b"y"}], "same file twice")

    def test_limits(self):
        self.assert_rejected([{"name": "big", "data": b"x" * 2000}], "limit", max_bytes=1000)
        self.assert_rejected([{"name": f"f{i}", "data": b"x"} for i in range(5)], "limit", max_files=4)

    def test_garbage_rejected(self):
        with open(self.tar, "wb") as f:
            f.write(b"not a tar" * 100)
        with self.assertRaises(ExportError):
            safe_extract_tar(self.tar, self.dest, "sub", max_bytes=100, max_files=10)
        self.assertEqual(os.listdir(self.dest), [])

    def test_bad_subfolder_name(self):
        for sub in ("..", "a/b", "", "x" * 300):
            with self.assertRaises(ExportError):
                safe_extract_tar(self.tar, self.dest, sub, max_bytes=100, max_files=10)


class DestinationTests(unittest.TestCase):
    def test_sensitive_destinations_refused(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            for sub in (".config", ".ssh", ".local/share", "projects"):
                os.makedirs(os.path.join(home, sub))
            for bad in (home, os.path.join(home, ".config"), os.path.join(home, ".ssh"),
                        os.path.join(home, ".local", "share"), "/", "/etc", "/usr", "rel"):
                with self.assertRaises(ExportError, msg=bad):
                    check_destination(bad, home)
            os.symlink(os.path.join(home, "projects"), os.path.join(home, "lnk"))
            with self.assertRaises(ExportError):
                check_destination(os.path.join(home, "lnk"), home)
            self.assertEqual(check_destination(os.path.join(home, "projects"), home), os.path.join(home, "projects"))


if __name__ == "__main__":
    unittest.main()


class GitLeftOutElsewhereTests(unittest.TestCase):
    """CI artifacts, research findings and memory follow the same rule as exports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_artifacts(self):
        import zipfile
        from cilly.github import safe_extract_zip
        z = os.path.join(self.d, "a.zip")
        with zipfile.ZipFile(z, "w") as zf:
            for n, d in GIT_TREE:
                zf.writestr(n + "/" if d == D else n, b"" if d == D else d)
        res = safe_extract_zip(z, self.d, "art", max_bytes=10_000, max_files=100)
        self.assertEqual((sorted(res["files"]), res["left_out"]), (KEPT, 5))

    def test_worktree_shapes_everywhere(self):
        import zipfile
        from cilly import handoff as H
        from cilly import memory as M
        from cilly.github import safe_extract_zip
        z = os.path.join(self.d, "wt.zip")
        with zipfile.ZipFile(z, "w") as zf:
            for n, d in WORKTREE:
                zf.writestr(n, d)
        res = safe_extract_zip(z, self.d, "wt", max_bytes=10_000, max_files=100)
        self.assertEqual((sorted(res["files"]), res["left_out"]), (WT_KEPT, 3))
        keep, left = H.validate_files([{"path": n, "content": d.decode()} for n, d in WORKTREE if n.count("/") < 4], 100_000)
        self.assertEqual((sorted(p for p, _ in keep), left), (WT_KEPT, 3))
        img = os.path.join(self.d, "memout.img")
        make_tar(img, [{"name": "projects/" + n, "data": d} for n, d in WORKTREE])
        agent = os.path.join(self.d, "agent")
        os.mkdir(agent)
        res = M.import_memout(img, agent)
        self.assertEqual((res["files"], res["left_out"]), (2, 3))

    def test_research_findings(self):
        from cilly import handoff as H
        files = [{"path": n, "content": d.decode()} for n, d in GIT_TREE if d != D and not n.startswith(".GIT")]
        files += [{"path": "bare/objects/info", "content": "x"}, {"path": "bare/refs/heads/main", "content": "0" * 40}]
        keep, left = H.validate_files(files, 100_000)  # a delivery only has folders that hold files
        self.assertEqual((sorted(p for p, _ in keep), left), (KEPT, 4))
        with self.assertRaises(H.HandoffError):
            H.validate_files([{"path": ".git/config", "content": "x"}], 1000)  # nothing left: rejected

    def test_memory(self):
        from cilly import memory as M
        img = os.path.join(self.d, "memout.img")
        make_tar(img, [{"name": "projects", "type": tarfile.DIRTYPE}, {"name": "projects/p/s.jsonl", "data": b"chat"},
                       {"name": "projects/p/.git/config", "data": b"[core]\n\tfsmonitor = x\n"},
                       {"name": "projects/q/HEAD", "data": b"ref: refs/heads/main\n"},
                       {"name": "projects/q/objects", "type": tarfile.DIRTYPE}, {"name": "projects/q/refs", "type": tarfile.DIRTYPE}])
        agent = os.path.join(self.d, "agent")
        os.mkdir(agent)
        res = M.import_memout(img, agent)
        self.assertEqual((res["files"], res["left_out"]), (1, 2))
        self.assertEqual(sorted(os.listdir(os.path.join(agent, "memory", "projects"))), ["p"])
        self.assertEqual(os.listdir(os.path.join(agent, "memory", "projects", "p")), ["s.jsonl"])


class FolderFloodTests(unittest.TestCase):
    """Folders count toward the limits too, also ones only implied by a path."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tar = os.path.join(cls.tmp.name, "flood.tar")
        with tarfile.open(cls.tar, "w") as tar:
            for i in range(100_000):
                ti = tarfile.TarInfo(f"projects/d{i}")
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_export(self):
        dest = os.path.join(self.tmp.name, "dest")
        os.mkdir(dest)
        with self.assertRaisesRegex(ExportError, "limit"):
            safe_extract_tar(self.tar, dest, "sub", max_bytes=1 << 20, max_files=20_000)
        self.assertEqual(os.listdir(dest), [])

    def test_implied_folders(self):
        dest = os.path.join(self.tmp.name, "dest2")
        os.mkdir(dest)
        tar = os.path.join(self.tmp.name, "deep.tar")
        make_tar(tar, [{"name": "/".join(f"d{i}" for i in range(30)) + "/f", "data": b"x"}])
        with self.assertRaisesRegex(ExportError, "limit"):
            safe_extract_tar(tar, dest, "sub", max_bytes=100, max_files=20)

    def test_one_very_deep_path_is_refused_quickly(self):
        import time
        import zipfile
        from cilly.github import GitHubError, safe_extract_zip
        name = "/".join(["d"] * 20_000) + "/f"
        tar = os.path.join(self.tmp.name, "deep2.tar")
        with tarfile.open(tar, "w", format=tarfile.PAX_FORMAT) as tf:
            ti = tarfile.TarInfo(name)
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
        dest = os.path.join(self.tmp.name, "dest3")
        os.mkdir(dest)
        t0 = time.time()
        with self.assertRaisesRegex(ExportError, "unsafe path"):
            safe_extract_tar(tar, dest, "sub", max_bytes=100, max_files=100_000)
        z = os.path.join(self.tmp.name, "deep.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(name, b"x")
            zf.writestr("/".join(["d"] * 40) + "/f", b"x")
        with self.assertRaisesRegex(GitHubError, "unsafe path"):
            safe_extract_zip(z, dest, "art", max_bytes=100, max_files=100)
        self.assertLess(time.time() - t0, 2)
        ok = "/".join(["d"] * 31) + "/f"  # 32 levels still works
        make_tar(tar, [{"name": ok, "data": b"x"}])
        self.assertEqual(safe_extract_tar(tar, dest, "ok", max_bytes=100, max_files=100)["files"], [ok])

    def test_memory(self):
        from cilly import memory as M
        agent = os.path.join(self.tmp.name, "agent")
        os.mkdir(agent)
        with self.assertRaisesRegex(M.MemoryError_, "limits"):
            M.import_memout(self.tar, agent)
        self.assertFalse(os.path.exists(os.path.join(agent, "memory")))


class MemoryStateNameTests(unittest.TestCase):
    """Memory saved before the rename to cilly goes back into the VM under the new state file name."""

    def test_the_old_state_file_goes_in_under_the_new_name(self):
        from cilly import memory as M
        with tempfile.TemporaryDirectory() as agent:
            mem = os.path.join(agent, "memory")
            os.makedirs(os.path.join(mem, "projects"))
            with open(os.path.join(mem, "vmpanel-state.json"), "w") as f:
                f.write('{"old": true}')
            names = lambda: (lambda b: tarfile.open(fileobj=b).getnames())(self._pack(M, agent))  # noqa: E731
            self.assertIn("memory/cilly-state.json", names())
            self.assertNotIn("memory/vmpanel-state.json", names())
            with open(os.path.join(mem, "cilly-state.json"), "w") as f:  # both there: the new one wins, once
                f.write('{"new": true}')
            got = self._pack(M, agent)
            with tarfile.open(fileobj=got) as t:
                self.assertEqual([n for n in t.getnames() if n.endswith("state.json")], ["memory/cilly-state.json"])
                self.assertEqual(t.extractfile("memory/cilly-state.json").read(), b'{"new": true}')

    @staticmethod
    def _pack(M, agent):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            M.add_to_tar(t, agent)
        buf.seek(0)
        return buf
