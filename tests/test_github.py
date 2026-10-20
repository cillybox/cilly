import os
import stat
import tempfile
import unittest
import zipfile

from unittest import mock

from cilly import github as G
from cilly.github import GitHubError, safe_extract_zip


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        self.dest = os.path.join(self.dir, "dest")
        os.mkdir(self.dest)

    def tearDown(self):
        self.tmp.cleanup()

    def _zip(self, entries, symlink=None):
        p = os.path.join(self.dir, "a.zip")
        with zipfile.ZipFile(p, "w") as z:
            for name, data in entries:
                zi = zipfile.ZipInfo(name)
                zi.external_attr = (0o100755 << 16)
                z.writestr(zi, data)
            if symlink:
                zi = zipfile.ZipInfo(symlink[0])
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, symlink[1])
        return p

    def _x(self, z, sub="out"):
        return safe_extract_zip(z, self.dest, sub, max_bytes=10_000, max_files=100)

    def test_ok_and_no_exec_bits(self):
        r = self._x(self._zip([("bin/app", b"\x7fELF"), ("README", b"hi")]))
        self.assertEqual(sorted(r["files"]), ["README", "bin/app"])
        mode = os.stat(os.path.join(self.dest, "out", "bin", "app")).st_mode
        self.assertFalse(mode & 0o111)

    def test_zip_slip(self):
        for bad in ["../evil", "/abs", "a/../../evil", "a\\..\\evil"]:
            sub = "o" + str(abs(hash(bad)))
            with self.assertRaises(GitHubError, msg=bad):
                self._x(self._zip([(bad, b"x")]), sub)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evil")))

    def test_symlink_entry_rejected(self):
        with self.assertRaises(GitHubError):
            self._x(self._zip([("a", b"x")], symlink=("link", "/etc/passwd")))

    def test_never_overwrites(self):
        z = self._zip([("a", b"x")])
        self._x(z)
        with self.assertRaises(FileExistsError):
            self._x(z)

    def test_dest_symlink_refused(self):
        os.symlink(self.dest, os.path.join(self.dir, "link"))
        with self.assertRaises(OSError):
            safe_extract_zip(self._zip([("a", b"x")]), os.path.join(self.dir, "link"), "o",
                             max_bytes=100, max_files=10)

    def test_size_limit(self):
        with self.assertRaises(GitHubError):
            self._x(self._zip([("big", b"x" * 20_000)]))

    def test_bad_subdir(self):
        with self.assertRaises(GitHubError):
            safe_extract_zip(self._zip([("a", b"x")]), self.dest, "../x", max_bytes=100, max_files=10)


class ProvenanceTests(unittest.TestCase):
    def fake_api(self, runs, pr=None):
        def api_get(path, token, **params):
            if "/pulls/" in path:
                return pr
            if path.endswith("/runs"):
                return {"workflow_runs": runs}
            if "/artifacts" in path:
                return {"artifacts": [{"id": 9, "name": "build", "size_in_bytes": 10, "expired": False}]}
            raise AssertionError(path)
        return api_get

    def test_fork_run_with_same_branch_name_ignored(self):
        runs = [{"id": 1, "head_repository": {"full_name": "attacker/repo"}, "event": "pull_request"},
                {"id": 2, "head_repository": {"full_name": "me/repo"}, "event": "push", "head_sha": "abc"}]
        with mock.patch.object(G, "api_get", self.fake_api(runs)):
            art = G.find_artifact("me/repo", None, branch="agent/x")
        self.assertEqual(art["run_id"], 2)

    def test_only_fork_runs_means_nothing_found(self):
        runs = [{"id": 1, "head_repository": {"full_name": "attacker/repo"}, "event": "pull_request"}]
        with mock.patch.object(G, "api_get", self.fake_api(runs)):
            with self.assertRaises(GitHubError):
                G.find_artifact("me/repo", None, branch="agent/x")

    def test_fork_pr_refused(self):
        pr = {"head": {"ref": "agent/x", "sha": "abc", "repo": {"full_name": "attacker/repo"}}}
        with mock.patch.object(G, "api_get", self.fake_api([], pr)):
            with self.assertRaises(GitHubError):
                G.find_artifact("me/repo", None, pr=5)


if __name__ == "__main__":
    unittest.main()


class ListReposTests(unittest.TestCase):
    def test_list_repos_filters_and_pages(self):
        from unittest import mock
        from cilly import github as GH
        page1 = [{"full_name": f"me/r{i}", "private": True, "permissions": {"admin": i == 0, "push": True, "pull": True},
                  "language": "Java", "pushed_at": "2026-10-01T00:00:00Z"} for i in range(100)]
        page2 = [{"full_name": "../evil", "permissions": {}}, {"full_name": "./x"}, {"full_name": "me/last", "permissions": {"pull": True}}, "junk"]
        calls = []

        def fake(path, token, **params):
            calls.append(params["page"])
            return page1 if params["page"] == 1 else page2
        with mock.patch.object(GH, "api_get", fake):
            repos = GH.list_repos("tok")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(repos), 101)
        self.assertTrue(repos[0]["permissions"]["admin"])
        self.assertEqual(repos[-1]["full_name"], "me/last")
        self.assertNotIn("../evil", [r["full_name"] for r in repos])


    def test_repo_overview_counts_commits(self):
        import io
        from cilly import github as GH

        class Resp(io.BytesIO):
            def __init__(self, body, link=""):
                super().__init__(body)
                self.headers = {"Link": link} if link else {}

        def info(path, token):
            return {"permissions": {"push": True}, "created_at": "2026-10-14T05:00:00Z", "pushed_at": "2026-10-15T05:00:00Z"}
        last = '<https://api.github.com/repositories/1/commits?per_page=1&page=2>; rel="next", ' \
               '<https://api.github.com/repositories/1/commits?per_page=1&page=214>; rel="last"'
        cases = [(lambda *a, **k: Resp(b"[{}]", last), 214), (lambda *a, **k: Resp(b"[{}]"), 1),
                 (mock.Mock(side_effect=GitHubError("GitHub API 409: Git Repository is empty.")), 0),
                 (mock.Mock(side_effect=GitHubError("network error: down")), None)]
        with mock.patch.object(GH, "api_get", info):
            for req, n in cases:
                with mock.patch.object(GH, "_request", req):
                    got = GH.repo_overview("me/app", "tok")
                self.assertEqual((got["commits"], got["push"], got["created_at"]), (n, True, "2026-10-14T05:00:00Z"))

    def test_repo_overview_refuses_a_reply_that_is_not_an_object(self):
        import io
        from cilly import github as GH

        class Resp(io.BytesIO):
            headers = {}
        for reply in ([], "x", None, 3):
            with mock.patch.object(GH, "api_get", lambda *a, reply=reply: reply), self.assertRaises(GitHubError):
                GH.repo_overview("me/app", "tok")
        with mock.patch.object(GH, "api_get", mock.Mock(side_effect=ValueError("not JSON"))), self.assertRaises(GitHubError):
            GH.repo_overview("me/app", "tok")
        info = lambda *a: {"permissions": [], "created_at": 5}  # noqa: E731
        for body in (b"<html>", b'{"a": 1}'):
            with mock.patch.object(GH, "api_get", info), mock.patch.object(GH, "_request", lambda *a, body=body, **k: Resp(body)):
                got = GH.repo_overview("me/app", "tok")
            self.assertEqual((got["push"], got["created_at"], got["commits"]), (False, "5", None))
