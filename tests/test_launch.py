"""Getting to the panel (cilly/launch.py, `setup`, `open`) and starting it with your login."""

import io
import os
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

from cilly import launch as L

URL = "http://127.0.0.1:8765/#t=SECRET-token_123"


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(self.run_dir, mode=0o700)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.run_dir, "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "cfg"),
                                           "XDG_DATA_HOME": os.path.join(self.tmp.name, "data")})
        env.start()
        self.addCleanup(env.stop)

    def test_the_link_file_is_private_and_only_its_own_panel_removes_it(self):
        self.assertIsNone(L.read_link())
        L.save_link(URL)
        self.assertEqual(L.read_link(), URL)
        self.assertEqual(os.stat(L.link_path()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(L.link_path())).st_mode & 0o777, 0o700)
        L.remove_link("http://127.0.0.1:9999/#t=other")  # a panel that started earlier stops
        self.assertEqual(L.read_link(), URL)
        L.remove_link(URL)
        self.assertIsNone(L.read_link())
        with open(L.link_path(), "w") as f:
            f.write("https://evil.example/\n")
        self.assertIsNone(L.read_link())  # only ever a link to this PC

    def test_the_browser_gets_a_private_page_not_the_token(self):
        started = []
        popen = lambda argv, **kw: started.append(argv)  # noqa: E731
        no_browser = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"firefox.desktop\n", b"")  # noqa: E731
        with mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            self.assertEqual(L.open_in_browser(URL, run=no_browser, popen=popen), "no desktop session here")
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}), \
                mock.patch.object(L.shutil, "which", return_value="/usr/bin/xdg-open"):
            self.assertIsNone(L.open_in_browser(URL, run=no_browser, popen=popen))
            self.assertEqual(len(started), 1)
            argv = started[0]
            self.assertEqual(argv[0], "/usr/bin/xdg-open")
            self.assertNotIn("SECRET", " ".join(argv))  # other users can read command lines
            page = argv[1][len("file://"):]
            self.assertEqual(os.stat(page).st_mode & 0o777, 0o600)
            with open(page) as f:
                self.assertIn(f'content="0;url={URL}"', f.read())
            # a Snap or Flatpak browser can't read the page: say so instead
            apps = os.path.join(self.tmp.name, "snap-apps")
            os.makedirs(apps)
            open(os.path.join(apps, "firefox_firefox.desktop"), "w").close()
            snap = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"firefox_firefox.desktop\n", b"")  # noqa: E731
            with mock.patch.object(L, "_sandbox_app_dirs", return_value=(apps,)):
                self.assertIn("Snap or Flatpak", L.open_in_browser(URL, run=snap, popen=popen))
                self.assertFalse(L.sandboxed_browser("../../etc/passwd"))
            self.assertEqual(len(started), 1)
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), mock.patch.object(L.shutil, "which", return_value=None):
            self.assertIn("xdg-open", L.open_in_browser(URL, run=no_browser, popen=popen))

    def test_the_page_escapes_the_link(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), mock.patch.object(L.shutil, "which", return_value="/usr/bin/xdg-open"):
            pages = []
            L.open_in_browser('http://127.0.0.1:1/#t="><script>', run=lambda a, **k: subprocess.CompletedProcess(a, 1, b"", b""),
                              popen=lambda argv, **kw: pages.append(argv[1][7:]))
            with open(pages[0]) as f:
                self.assertNotIn("<script>", f.read())


class ServeTests(unittest.TestCase):
    """serve keeps the link with its token out of the terminal and the journal (decision 5)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(self.run_dir, mode=0o700)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.run_dir, "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "cfg"),
                                           "XDG_DATA_HOME": os.path.join(self.tmp.name, "data"), "DISPLAY": "", "WAYLAND_DISPLAY": ""})
        env.start()
        self.addCleanup(env.stop)

    def serve(self, **kw):
        from cilly import server as S
        seen = {}

        class FakeHTTPServer:
            def __init__(self, addr, handler):
                self.daemon_threads = False

            def serve_forever(self):  # look while it "runs", then stop like Ctrl+C
                seen["link"] = L.read_link()
                seen["mode"] = os.stat(L.link_path()).st_mode & 0o777
                seen["dir"] = os.stat(os.path.dirname(L.link_path())).st_mode & 0o777
                raise KeyboardInterrupt

            def server_close(self):
                pass
        out = io.StringIO()
        with mock.patch.object(S, "ThreadingHTTPServer", FakeHTTPServer), mock.patch.object(S.os, "geteuid", return_value=1000), \
                mock.patch("sys.stdout", out):
            S.serve(8799, **kw)
        return out.getvalue(), seen

    def test_the_token_is_never_printed(self):
        printed, seen = self.serve(open_browser=True)
        token = seen["link"].split("#t=", 1)[1]
        self.assertTrue(seen["link"].startswith("http://127.0.0.1:8799/#t=") and len(token) > 20)
        self.assertNotIn(token, printed)
        self.assertNotIn("#t=", printed)
        self.assertIn("Open the panel with: python3 -m cilly open\n", printed)
        self.assertIn("Couldn't open your browser (no desktop session here)", printed)
        self.assertEqual((seen["mode"], seen["dir"]), (0o600, 0o700))
        self.assertIsNone(L.read_link())  # gone once it stopped

    def test_a_second_panel_stops_before_it_touches_anything(self):
        from cilly import config as C
        from cilly import server as S
        first = L.panel_lock()  # the panel that runs already
        L.save_link(URL)
        uploads = os.path.join(C.data_dir(), "agents", "0123abcd", "uploads")
        os.makedirs(uploads)
        with open(os.path.join(C.data_dir(), "agents", "0123abcd", "meta.json"), "w") as f:
            f.write('{"state": "running"}')
        err = io.StringIO()
        with mock.patch.object(S, "App", side_effect=AssertionError("started")), mock.patch("sys.stderr", err):
            printed, seen = self.serve()
        self.assertEqual((printed, seen), ("", {}))
        self.assertIn("cilly is running already. `python3 -m cilly open` opens it.", err.getvalue())
        self.assertTrue(os.path.isdir(uploads))  # its staged attachments are still there
        self.assertEqual(L.read_link(), URL)  # and so is its link
        os.close(first)
        printed, seen = self.serve()  # once it stopped, the next one starts
        self.assertIn("cilly listening on 127.0.0.1:8799", printed)
        self.assertFalse(os.path.exists(uploads))

    def test_no_runtime_folder_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            with self.assertRaises(SystemExit) as cm:
                self.serve(open_browser=True)
        self.assertIn("XDG_RUNTIME_DIR is not set", str(cm.exception))
        # opening the browser says why instead of crashing
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "", "DISPLAY": ":0"}), \
                mock.patch.object(L.shutil, "which", return_value="/usr/bin/xdg-open"):
            why = L.open_in_browser(URL, popen=lambda *a, **k: self.fail("opened"),
                                    run=lambda a, **k: subprocess.CompletedProcess(a, 1, b"", b""))
        self.assertIn("XDG_RUNTIME_DIR is not set", why)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def systemctl(self, argv, **kw):
        self.calls.append(argv[2:])
        if argv[2] == "enable":
            os.makedirs(os.path.join(L.unit_dir(), "default.target.wants"), exist_ok=True)
            os.symlink(os.path.join(L.unit_dir(), L.UNIT), os.path.join(L.unit_dir(), "default.target.wants", L.UNIT))
        if argv[2] == "disable":
            os.unlink(os.path.join(L.unit_dir(), "default.target.wants", L.UNIT))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def test_install_and_remove(self):
        with mock.patch.object(L, "_systemd_user", return_value=True):
            path = L.install_service(run=self.systemctl)
            with open(path) as f:
                unit = f.read()
            self.assertTrue(unit.startswith(L.UNIT_MARK))
            self.assertIn(f"ExecStart={L.sys.executable} -m cilly serve\n", unit)
            self.assertIn(f"WorkingDirectory={os.path.dirname(L.PKG_DIR)}\n", unit)
            self.assertEqual(self.calls, [["daemon-reload"], ["enable", "cilly.service"]])
            self.assertEqual(L.service_status(), {"available": True, "enabled": True, "ours": True})
            L.remove_service(run=self.systemctl)
            self.assertFalse(os.path.exists(path))
            self.assertEqual(L.service_status(), {"available": True, "enabled": False, "ours": False})

    def test_systemctl_that_doesnt_answer_is_a_clear_error(self):
        def hangs(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        with mock.patch.object(L, "_systemd_user", return_value=True), self.assertRaises(SystemExit) as cm:
            L.install_service(run=hangs)
        self.assertEqual(str(cm.exception), "systemctl --user daemon-reload didn't answer within 30 s")

    def test_an_installed_cilly_names_no_versioned_path(self):
        """pipx: the venv's python, with your home as its folder, survives a Python upgrade."""
        site = os.path.join(self.tmp.name, "venvs", "cilly", "lib", "python3.12", "site-packages", "cilly")
        os.makedirs(site)
        with mock.patch.object(L, "PKG_DIR", site):
            unit = L.unit_text(python="/home/me/.local/share/pipx/venvs/cilly/bin/python")
        self.assertIn("WorkingDirectory=%h\n", unit)
        self.assertIn("ExecStart=/home/me/.local/share/pipx/venvs/cilly/bin/python -I -m cilly serve\n", unit)  # isolated
        self.assertNotIn("python3.12", unit)
        self.assertNotIn("journalctl", unit)

    def test_python_runs_isolated_from_your_home(self):
        """python -m puts its folder first on sys.path: a json.py in your home would replace the standard library's."""
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "json.py"), "w") as f:
                f.write("raise SystemExit('the json.py in your home ran')\n")
            code = "import json; print(json.__file__)"
            plain = subprocess.run([sys.executable, "-c", code], cwd=home, capture_output=True, text=True)
            isolated = subprocess.run([sys.executable, "-I", "-c", code], cwd=home, capture_output=True, text=True)
        self.assertIn("the json.py in your home ran", plain.stderr)  # what the unit did before
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertNotIn(home, isolated.stdout)

    def test_an_older_unit_gets_isolated_once(self):
        os.makedirs(L.unit_dir())
        path = os.path.join(L.unit_dir(), L.UNIT)
        old = L.unit_text(python="/home/me/.local/share/pipx/venvs/cilly/bin/python", workdir="%h").replace(" -I -m", " -m")
        with open(path, "w") as f:
            f.write(old)
        self.assertTrue(L.refresh_service(run=self.systemctl))
        with open(path) as f:
            new = f.read()
        self.assertEqual(new, old.replace(" -m cilly serve", " -I -m cilly serve"))  # only that changed
        self.assertEqual(self.calls, [["daemon-reload"]])
        self.assertFalse(L.refresh_service(run=self.systemctl))  # once
        # a checkout's unit runs in the checkout, which it needs to find cilly: as it is
        with open(path, "w") as f:
            f.write(L.unit_text(python="/usr/bin/python3", workdir="/home/me/cilly"))
        self.assertFalse(L.refresh_service(run=self.systemctl))
        # and one you wrote yourself is never touched
        with open(path, "w") as f:
            f.write("[Service]\nWorkingDirectory=%h\nExecStart=/usr/bin/python3 -m cilly serve\n")
        self.assertFalse(L.refresh_service(run=self.systemctl))
        self.assertEqual(self.calls, [["daemon-reload"]])

    def test_a_unit_you_wrote_yourself_is_kept(self):
        os.makedirs(L.unit_dir())
        mine = os.path.join(L.unit_dir(), L.UNIT)
        with open(mine, "w") as f:
            f.write("[Service]\nExecStart=/usr/bin/python -m cilly serve\n")
        with mock.patch.object(L, "_systemd_user", return_value=True):
            L.install_service(run=self.systemctl)
            L.remove_service(run=self.systemctl)
        with open(mine) as f:
            self.assertTrue(f.read().startswith("[Service]"))
        self.assertEqual(self.calls, [["daemon-reload"], ["enable", "cilly.service"], ["disable", "cilly.service"]])

    def test_refusals(self):
        with mock.patch.object(L, "_systemd_user", return_value=False), self.assertRaises(SystemExit):
            L.install_service(run=self.systemctl)
        for bad in ("/home/my name/.venv/bin/python", "/usr/bin/python%h", "/x\nExecStartPre=/bin/evil", "python"):
            with self.assertRaises(SystemExit):
                L.unit_text(python=bad)
        with mock.patch.object(L, "_systemd_user", return_value=True), self.assertRaises(SystemExit):
            L.install_service(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"Failed to connect to bus"))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(run_dir)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run_dir, "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "cfg"),
                                           "XDG_DATA_HOME": os.path.join(self.tmp.name, "data")})
        env.start()
        self.addCleanup(env.stop)

    def main(self, *argv):
        from cilly import __main__ as M
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(M.os, "geteuid", return_value=1000), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = M.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_setup_writes_the_config_and_opens_the_panel(self):
        from cilly import config as C
        with mock.patch("cilly.server.serve") as serve:
            code, out, _ = self.main("setup")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(C.config_path()))
        self.assertIn("It holds no secrets", out)
        serve.assert_called_once_with(None, open_browser=True)
        with mock.patch("cilly.server.serve") as serve:
            self.main("setup", "--no-browser", "--port", "9000")
        serve.assert_called_once_with(9000, open_browser=False)

    def test_open_print(self):
        L.save_link(URL)
        code, out, _ = self.main("open", "--print")
        self.assertEqual((code, out), (0, URL + "\n"))

    def test_open(self):
        code, _, err = self.main("open")
        self.assertEqual(code, 1)
        self.assertIn("isn't running", err)
        L.save_link(URL)
        with mock.patch.object(L, "open_in_browser", return_value="no desktop session here"):
            code, out, _ = self.main("open")
        self.assertEqual(code, 0)
        self.assertIn(URL, out)
        with mock.patch.object(L, "open_in_browser", return_value=None):
            self.assertEqual(self.main("open")[1], "")


if __name__ == "__main__":
    unittest.main()
