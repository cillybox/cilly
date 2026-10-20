"""Host detection (cilly/host.py), install.sh, and running QEMU inside bubblewrap or not at all."""

import os
import pwd
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from cilly import host as H

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, "cilly", "install.sh")

RELEASES = {  # /etc/os-release of each, the lines that matter
    "arch": 'NAME="Arch Linux"\nID=arch\n',
    "manjaro": 'NAME="Manjaro Linux"\nID=manjaro\nID_LIKE=arch\n',
    "ubuntu": 'PRETTY_NAME="Ubuntu 24.04.5 LTS"\nID=ubuntu\nID_LIKE=debian\n',
    "mint": 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n',
    "debian": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n',
    "fedora": 'NAME="Fedora Linux"\nID=fedora\n',
    "tumbleweed": 'NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n',
    "rhel": 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\n',
    "alma": 'NAME="AlmaLinux"\nID="almalinux"\nID_LIKE="rhel centos fedora"\n',
    "stream": 'NAME="CentOS Stream"\nID="centos"\nID_LIKE="rhel fedora"\n',
    "alpine": 'NAME="Alpine Linux"\nID=alpine\n',
}
FAMILY = {"arch": "arch", "manjaro": "arch", "ubuntu": "debian", "mint": "debian", "debian": "debian",
          "fedora": "fedora", "tumbleweed": "suse", "rhel": "rhel", "alma": "rhel", "stream": "rhel", "alpine": None}


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text, mode=None):
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path


class DistroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def release(self, name):
        H.os_release.cache_clear()
        return H.os_release((write(os.path.join(self.tmp.name, name), RELEASES[name]),))

    def test_family_from_id_and_id_like(self):
        for name, fam in FAMILY.items():
            self.assertEqual(H.family(self.release(name)), fam, name)
        self.assertEqual(self.release("ubuntu")["PRETTY_NAME"], "Ubuntu 24.04.5 LTS")  # quotes taken off
        self.assertEqual(H.os_release((os.path.join(self.tmp.name, "missing"),)), {})
        self.assertIsNone(H.family({}))

    def test_install_command_per_family(self):
        self.assertEqual(H.install_command("debian", ["qemu", "xorriso", "bwrap"]),
                         "sudo apt install qemu-system-x86 qemu-utils xorriso bubblewrap")
        self.assertEqual(H.install_command("arch", ["qemu"]), "sudo pacman -S --needed qemu-base")
        self.assertEqual(H.install_command("fedora", ["qemu"]), "sudo dnf install qemu-system-x86-core qemu-img")
        self.assertEqual(H.install_command("suse", ["bwrap"]), "sudo zypper install bubblewrap")
        self.assertEqual(H.install_command(None, ["qemu"]), "")
        self.assertEqual(H.install_command("arch", []), "")
        for fam in H.FAMILIES.values():
            self.assertEqual(set(H.NEEDS) | {"label", "install"}, set(fam))

    def test_info_offers_install_sh_only_where_it_helps(self):
        tools = {"qemu": "/usr/bin/qemu-system-x86_64", "qemu-img": None, "xorriso": None, "bwrap": None,
                 "bwrap_problem": None, "use_bwrap": "auto"}
        blocked = lambda kind: dict(tools, qemu="q", **{"qemu-img": "q"}, xorriso="x",  # noqa: E731
                                    bwrap_problem={"why": "denied", "kind": kind, "fix": ""})
        with mock.patch.object(H, "os_release", return_value={"ID": "fedora", "PRETTY_NAME": "Fedora Linux 43"}), \
                mock.patch.object(H, "kvm_problem", return_value=None):
            info = H.info(tools)
            self.assertEqual((info["distro"], info["family"], info["unsupported"]), ("Fedora Linux 43", "fedora", ""))
            self.assertEqual(info["missing_cmd"], "sudo dnf install qemu-system-x86-core qemu-img xorriso bubblewrap")
            self.assertEqual(info["root_steps"], ["qemu", "xorriso", "bwrap"])
            self.assertTrue(info["install_sh"].startswith("sudo sh ") and info["install_sh"].endswith("install.sh"))
            # bubblewrap blocked by AppArmor: install.sh adds a profile.  By a kernel setting: it can't help.
            self.assertEqual(H.info(blocked("apparmor"))["root_steps"], ["apparmor"])
            for kind in ("clone", "max", "timeout", "error"):
                self.assertEqual((H.info(blocked(kind))["root_steps"], H.info(blocked(kind))["install_sh"]), ([], ""), kind)
            # with vm.use_bwrap off, bubblewrap isn't needed at all
            self.assertEqual(H.info(dict(blocked("apparmor"), use_bwrap="off"))["root_steps"], [])
            self.assertEqual(H.info(dict(tools, use_bwrap="off"))["root_steps"], ["qemu", "xorriso"])
        with mock.patch.object(H, "os_release", return_value={"ID": "fedora"}), mock.patch.object(H, "kvm_problem", return_value="no-access"):
            self.assertEqual(H.info(blocked("error"))["root_steps"], ["kvm"])
        with mock.patch.object(H, "os_release", return_value={"ID": "fedora"}), mock.patch.object(H, "kvm_problem", return_value="missing"):
            self.assertEqual(H.info(blocked("error"))["install_sh"], "")  # no virtualization: install.sh can't turn it on
        for rel in ({}, {"ID": "alpine"}):  # a distribution install.sh doesn't know
            with mock.patch.object(H, "os_release", return_value=rel):
                self.assertEqual((H.info(tools)["install_sh"], H.info(tools)["missing_cmd"]), ("", ""))
        with mock.patch.object(H, "os_release", return_value={"ID": "rocky", "ID_LIKE": "rhel centos fedora"}):
            info = H.info(tools)
            self.assertEqual((info["family"], info["install_sh"], info["missing_cmd"]), ("rhel", "", ""))
            self.assertIn("aren't supported yet", info["unsupported"])

    def test_virtiofsd_outside_path(self):
        d = self.tmp.name
        exe = write(os.path.join(d, "virtiofsd"), "#!/bin/sh\n", 0o755)
        with mock.patch.object(H.shutil, "which", return_value=None), \
                mock.patch.object(H, "VIRTIOFSD_PATHS", (os.path.join(d, "nope"), exe)):
            self.assertEqual(H.find_virtiofsd(), exe)
        with mock.patch.object(H.shutil, "which", return_value=None), mock.patch.object(H, "VIRTIOFSD_PATHS", ()):
            self.assertIsNone(H.find_virtiofsd())

    def test_kvm_and_user_namespace_settings(self):
        d = self.tmp.name
        self.assertEqual(H.kvm_problem(os.path.join(d, "kvm")), "missing")
        dev = write(os.path.join(d, "dev-kvm"), "", 0o600)
        self.assertIsNone(H.kvm_problem(dev))  # it's yours (root reads anything, so only "no access" is skipped here)
        settings = lambda a, c, m: {"apparmor": (write(os.path.join(d, "a"), a), "1"),  # noqa: E731
                                    "clone": (write(os.path.join(d, "c"), c), "0"), "max": (write(os.path.join(d, "m"), m), "0")}
        self.assertEqual(H.userns_block(settings("1\n", "1\n", "1000\n")), "apparmor")
        self.assertEqual(H.userns_block(settings("0\n", "0\n", "1000\n")), "clone")
        self.assertEqual(H.userns_block(settings("0\n", "1\n", "0\n")), "max")
        self.assertIsNone(H.userns_block(settings("0\n", "1\n", "1000\n")))
        self.assertIsNone(H.userns_block({"apparmor": (os.path.join(d, "missing"), "1")}))
        self.assertEqual(set(H.BWRAP_FIX), {"apparmor", "clone", "max"})
        self.assertIn("install.sh", H.BWRAP_FIX["apparmor"])
        self.assertIn("sudo sysctl kernel.unprivileged_userns_clone=1", H.BWRAP_FIX["clone"])
        self.assertIn("sudo sysctl user.max_user_namespaces=", H.BWRAP_FIX["max"])

    def test_cli_name(self):
        d = self.tmp.name
        checkout, installed = os.path.join(d, "repo", "cilly"), os.path.join(d, "venv", "lib", "python3.13", "site-packages", "cilly")
        os.makedirs(checkout), os.makedirs(installed)
        write(os.path.join(d, "repo", "pyproject.toml"), "")
        py3, py311 = write(os.path.join(d, "python3"), ""), write(os.path.join(d, "python3.11"), "")
        which = lambda n: {"python3": py3, "python3.11": py311}.get(n)  # noqa: E731
        with mock.patch.object(H, "PKG_DIR", checkout), mock.patch.object(H.shutil, "which", which):
            with mock.patch.object(H.sys, "executable", py3):
                self.assertEqual(H.cli(), "python3 -m cilly")
            with mock.patch.object(H.sys, "executable", py311):  # Ubuntu 22.04: python3 is 3.10, cilly runs with 3.11
                self.assertEqual(H.cli(), "python3.11 -m cilly")
            with mock.patch.object(H.sys, "executable", "/opt/my py/bin/python"):
                self.assertEqual(H.cli(), "'/opt/my py/bin/python' -m cilly")
        with mock.patch.object(H, "PKG_DIR", installed):
            # pipx: the service starts it as python -m cilly from the venv, but you type cilly
            with mock.patch.object(H.sys, "argv", ["/x/site-packages/cilly/__main__.py", "serve"]), \
                    mock.patch.object(H.shutil, "which", return_value="/home/me/.local/bin/cilly"):
                self.assertEqual(H.cli(), "cilly")
            with mock.patch.object(H.sys, "argv", ["/x/site-packages/cilly/__main__.py"]), \
                    mock.patch.object(H.shutil, "which", return_value=None), mock.patch.object(H.sys, "executable", "/opt/py/bin/python3"):
                self.assertEqual(H.cli(), "/opt/py/bin/python3 -m cilly")


class BwrapTests(unittest.TestCase):
    """Installed bubblewrap that can't build its sandbox stops VMs: QEMU never runs outside it by surprise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        H._bwrap_cache.clear()
        self.addCleanup(H._bwrap_cache.clear)
        self.calls = os.path.join(self.tmp.name, "calls")
        self.no_block = mock.patch.object(H, "userns_block", return_value=None)
        self.no_block.start()
        self.addCleanup(self.no_block.stop)

    def fake(self, ok, name=None):
        body = 'echo "$@" >> "%s"\n' % self.calls + ("exit 0\n" if ok else "echo 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n")
        d = os.path.join(self.tmp.name, name or ("ok" if ok else "bad"))
        os.makedirs(d, exist_ok=True)
        return write(os.path.join(d, "bwrap"), "#!/bin/sh\n" + body, 0o755)

    def test_a_working_result_is_kept_and_a_failed_one_only_for_the_status(self):
        good, bad = self.fake(True), self.fake(False)
        self.assertIsNone(H.bwrap_check(good))
        self.assertIsNone(H.bwrap_check(good, fresh=True))
        self.assertEqual(read(self.calls).splitlines(), ["--unshare-all --die-with-parent --ro-bind / / -- true"])  # kept
        self.assertEqual(H.bwrap_check(bad), {"why": "bwrap: setting up uid map: Permission denied", "kind": "error", "fix": "", "cmd": ""})
        # the host status shows the failure for a while without checking again
        self.assertEqual(H.bwrap_check(bad, run=lambda *a, **k: self.fail("cached"))["kind"], "error")
        # a VM start always checks again: it ran install.sh, so it works now
        self.assertIsNone(H.bwrap_check(bad, fresh=True, run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"", b"")))
        ubuntus = write(os.path.join(self.tmp.name, "bwrap-userns-restrict"), "profile bwrap /usr/bin/bwrap {}\n")
        with mock.patch.object(H, "userns_block", return_value="apparmor"), mock.patch.object(H, "UBUNTU_BWRAP_PROFILE", ubuntus):
            H._bwrap_cache.clear()
            got = H.bwrap_check(bad)
            self.assertEqual(got["kind"], "apparmor")
            self.assertIn("install.sh", H.bwrap_refusal(got))

    def test_without_ubuntus_profile_install_sh_isnt_offered(self):
        """cilly writes no profile of its own, so where Ubuntu has none, install.sh can't help."""
        bad = self.fake(False)
        missing = os.path.join(self.tmp.name, "no-such-profile")
        with mock.patch.object(H, "userns_block", return_value="apparmor"), mock.patch.object(H, "UBUNTU_BWRAP_PROFILE", missing):
            got = H.bwrap_check(bad)
        self.assertEqual((got["kind"], got["fix"]), ("apparmor", H.NO_UBUNTU_PROFILE))
        self.assertNotIn("install.sh", H.bwrap_refusal(got))
        tools = {"qemu": "/q", "qemu-img": "/q", "xorriso": "/x", "bwrap": None, "bwrap_problem": got, "use_bwrap": "auto"}
        with mock.patch.object(H, "os_release", return_value={"ID": "ubuntu"}), mock.patch.object(H, "kvm_problem", return_value=None):
            self.assertNotIn("apparmor", H.info(tools)["root_steps"])

    def test_settings_that_block_everyone_come_before_apparmor(self):
        """Ubuntu has the AppArmor setting on by default, so it says least about why bubblewrap failed."""
        files = {}
        for kind, (path, blocked) in H.USERNS_SETTINGS.items():
            files[kind] = (write(os.path.join(self.tmp.name, kind), "1\n" if kind == "apparmor" else "0\n"), blocked)
        userns_block = self.no_block.temp_original  # the real one, not setUp's stand-in
        self.assertEqual(userns_block(files), "clone")
        write(files["clone"][0], "1\n")
        self.assertEqual(userns_block(files), "max")
        write(files["max"][0], "15000\n")
        self.assertEqual(userns_block(files), "apparmor")

    def test_a_bubblewrap_that_doesnt_run_isnt_blamed_on_apparmor(self):
        def missing(argv, **kw):
            raise PermissionError(13, "Permission denied")
        with mock.patch.object(H, "userns_block", return_value="apparmor"):
            got = H.bwrap_check("/x/bwrap", run=missing, fresh=True)
        self.assertEqual((got["kind"], got["fix"]), ("error", ""))

    def test_check_again_forgets_only_failures(self):
        good, bad = self.fake(True), self.fake(False)
        H.bwrap_check(good), H.bwrap_check(bad)
        H.bwrap_check_again()
        self.assertIsNone(H.bwrap_check(good, run=lambda *a, **k: self.fail("a working one is kept")))
        self.assertIsNone(H.bwrap_check(bad, run=lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"", b"")))

    def test_timeout_and_errors_are_problems_too(self):
        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        def missing(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory")
        got = H.bwrap_check("/x/bwrap", run=timeout, fresh=True)
        self.assertEqual((got["kind"], got["why"]), ("timeout", f"it didn't finish within {H.BWRAP_TIMEOUT} s"))
        got = H.bwrap_check("/x/bwrap", run=missing, fresh=True)
        self.assertEqual((got["kind"], got["why"]), ("error", "it couldn't run (No such file or directory)"))
        got = H.bwrap_check("/x/bwrap", run=lambda argv, **k: subprocess.CompletedProcess(argv, 3, b"", b""), fresh=True)
        self.assertEqual(got["why"], "it exited with 3")
        for problem in (got, {"why": "x", "kind": "clone", "fix": H.BWRAP_FIX["clone"]}):
            msg = H.bwrap_refusal(problem)
            self.assertIn('set vm.use_bwrap to "off" in config.json yourself', msg)
            self.assertNotIn("install.sh", msg)  # it can't help with these

    def test_auto_fails_closed(self):
        from cilly import vm as V
        good, bad = self.fake(True), self.fake(False)
        for path in (good, bad, None):
            with mock.patch.object(V, "which", side_effect=lambda n, p=path: p if n == "bwrap" else None):
                self.assertEqual(V.bwrap_decision("off"), (None, "QEMU runs without bubblewrap: vm.use_bwrap is off."))
                tools = V.detect_tools({"vm": {"qemu_binary": "qemu-system-x86_64", "use_bwrap": "auto"}})
                self.assertEqual((tools["bwrap"], bool(tools["bwrap_problem"])), (good if path == good else None, path == bad))
                off = V.detect_tools({"vm": {"qemu_binary": "qemu-system-x86_64", "use_bwrap": "off"}})
                self.assertIsNone(off["bwrap_problem"])  # off: nothing to fix
                for mode in ("auto", "required"):
                    if path == good:
                        self.assertEqual(V.bwrap_decision(mode), (good, f"QEMU runs inside bubblewrap ({good})."))
                    elif path == bad or mode == "required":
                        with self.assertRaises(V.VMError):
                            V.bwrap_decision(mode)
                    else:
                        self.assertEqual(V.bwrap_decision(mode), (None, "QEMU runs without bubblewrap: it isn't installed."))
        # a check that hangs is a refusal too, not a reason to run QEMU without the sandbox
        hangs = write(os.path.join(self.tmp.name, "bwrap-hangs"), "#!/bin/sh\nsleep 5\n", 0o755)
        with mock.patch.object(V, "which", return_value=hangs), mock.patch.object(H, "BWRAP_TIMEOUT", 0.2):
            with self.assertRaises(V.VMError) as cm:
                V.bwrap_decision("auto")
        self.assertIn("didn't finish within 0.2 s", str(cm.exception))

    def test_bootcheck_makes_the_same_choice(self):
        from cilly import bootcheck as B
        from cilly import vm as V
        bad = self.fake(False)
        tools = {"qemu": "/usr/bin/qemu-system-x86_64", "qemu-img": "/usr/bin/qemu-img"}
        cfg = {"vm": {"use_bwrap": "auto"}}
        with mock.patch.object(B, "detect_tools", return_value=tools), mock.patch.object(B.C, "load", return_value=cfg), \
                mock.patch.object(V, "which", side_effect=lambda n: bad if n == "bwrap" else None), \
                mock.patch.object(B.subprocess, "run", side_effect=AssertionError("no disk is made")):
            with self.assertRaises(SystemExit) as cm, open(os.devnull, "w") as out:
                B.run("/x/image.qcow2", out=out)
        self.assertIn("can't build its sandbox", str(cm.exception))


class InstallScriptTests(unittest.TestCase):
    # what install.sh itself uses, so a test can leave QEMU, bubblewrap and the rest out of $PATH
    TOOLS = ("cat", "grep", "id", "stat", "tr", "head", "sed", "readlink", "dirname", "basename", "getent", "runuser")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        for t in self.TOOLS:
            if shutil.which(t):
                os.symlink(shutil.which(t), os.path.join(self.bin, t))

    def have(self, *commands, script="#!/bin/sh\nexit 0\n"):
        for c in commands:
            write(os.path.join(self.bin, c), script, 0o755)

    ME = pwd.getpwuid(os.getuid()).pw_name  # --user is checked, so it's a user that exists

    def run_sh(self, release, *args, userns="0", ubuntu_profile=None, user=ME, apparmor_d=None, dry=True):
        env = {"PATH": self.bin, "CILLY_OS_RELEASE": write(os.path.join(self.tmp.name, "os-release"), RELEASES[release]),
               "CILLY_USERNS_SYSCTL": write(os.path.join(self.tmp.name, "userns"), userns),
               "CILLY_UBUNTU_BWRAP_PROFILE": ubuntu_profile or os.path.join(self.tmp.name, "no-such-profile"),
               **({"CILLY_APPARMOR_D": apparmor_d} if apparmor_d else {})}
        return subprocess.run(["/bin/sh", INSTALL_SH, *(["--dry-run"] if dry else []), "--user", user, *args], env=env,
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)

    def test_packages_match_host_py(self):
        table = dict(re.findall(r'^  (\w+)\)\s+NEED="([^"]+)" ;;$', read(INSTALL_SH), re.M))
        self.assertEqual(set(table), set(H.FAMILIES))
        for fam, need in table.items():
            pairs = [p.split(":") for p in need.split()]
            self.assertEqual({c for c, _ in pairs}, {"python3", "qemu-system-x86_64", "qemu-img", "xorriso", "bwrap", "openssl"}, fam)
            want = {p for n in ("qemu", "xorriso", "bwrap", "openssl") for p in H.FAMILIES[fam][n].split()}
            self.assertEqual({p for _, p in pairs} - {"python", "python3"}, want, fam)
        self.assertIn('OPTIONAL="virtiofsd"', read(INSTALL_SH))

    def test_the_last_line_names_a_python_that_runs_cilly(self):
        """Ubuntu 22.04's python3 is 3.10: the command at the end uses a newer one beside it, or the one to install."""
        old = "#!/bin/sh\nexit 1\n"  # fails the version check
        self.have("python3", script=old)
        r = self.run_sh("ubuntu")
        self.assertIn("needs Python 3.11 or newer", r.stdout)
        self.assertIn("&& python3.11 -m cilly setup", r.stdout)  # what it says to install
        self.have("python3.12")
        self.have("python3.11", script=old)  # one that isn't new enough after all is passed over
        r = self.run_sh("ubuntu")
        self.assertNotIn("needs Python 3.11 or newer", r.stdout)
        self.assertIn("start cilly with python3.12", r.stdout)
        self.assertIn("&& python3.12 -m cilly setup", r.stdout)
        self.have("python3")  # new enough: as before
        r = self.run_sh("ubuntu")
        self.assertIn("&& python3 -m cilly setup", r.stdout)
        self.assertNotIn("start cilly with", r.stdout)

    def test_dry_run_on_each_family(self):
        managers = {"arch": "pacman -S --needed --noconfirm", "ubuntu": "env DEBIAN_FRONTEND=noninteractive apt-get install -y",
                    "fedora": "dnf install -y", "tumbleweed": "zypper --non-interactive install"}
        for release, pm in managers.items():
            r = self.run_sh(release)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f"+ {pm}", r.stdout)
            self.assertIn(f"for the user {self.ME}", r.stdout)
            self.assertNotIn("apparmor", r.stdout.lower())
        self.assertIn("+ apt-get update", self.run_sh("ubuntu").stdout)

    def test_packages_that_are_there_are_skipped(self):
        self.have("qemu-system-x86_64", "qemu-img")  # say, qemu-full on Arch, which conflicts with qemu-base
        r = self.run_sh("arch")
        self.assertIn("+ pacman -S --needed --noconfirm python libisoburn bubblewrap openssl\n", r.stdout)
        self.assertNotIn("qemu-base", r.stdout)
        self.have("python3", "xorriso", "bwrap", "virtiofsd", "openssl")
        r = self.run_sh("ubuntu")
        self.assertIn("The packages cilly needs are there already.", r.stdout)
        self.assertNotIn("apt-get", r.stdout)

    def test_rhel_and_its_rebuilds_stop_before_anything(self):
        for release in ("rhel", "alma", "stream"):
            r = self.run_sh(release)
            self.assertEqual(r.returncode, 1, release)
            self.assertIn("isn't supported yet", r.stdout)
            self.assertIn("Nothing changed.", r.stdout)
            self.assertNotIn("dnf", r.stdout)

    def test_ubuntu_uses_its_own_profile_when_it_has_one(self):
        prof = write(os.path.join(self.tmp.name, "bwrap-userns-restrict"), "profile bwrap /usr/bin/bwrap flags=(unconfined) {\n  userns,\n}\n")
        r = self.run_sh("ubuntu", userns="1", ubuntu_profile=prof)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("turn on Ubuntu's own AppArmor profile for it (bwrap-userns-restrict)", r.stdout)
        self.assertIn(f"+ cp {prof} /etc/apparmor.d/bwrap-userns-restrict", r.stdout)
        self.assertIn("    profile bwrap /usr/bin/bwrap flags=(unconfined) {", r.stdout)  # the dry run shows it
        self.assertIn("+ apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict", r.stdout)
        self.assertNotIn("cilly-bwrap", r.stdout)

    def test_without_ubuntus_profile_it_writes_none(self):
        """A profile that only allows user namespaces would let every program that runs bubblewrap have them."""
        r = self.run_sh("ubuntu", userns="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("This Ubuntu has no AppArmor profile for bubblewrap (bwrap-userns-restrict) to turn on, "
                      "and cilly doesn't write one of its own.", r.stdout)
        self.assertIn('set vm.use_bwrap to "off" in config.json', r.stdout)
        for never in ("cilly-bwrap", "+ cp", "apparmor_parser", "+ write"):
            self.assertNotIn(never, r.stdout)

    def test_an_older_profile_of_cillys_is_replaced_with_ubuntus(self):
        d = os.path.join(self.tmp.name, "apparmor.d")
        os.makedirs(d)
        old = write(os.path.join(d, "cilly-bwrap"), "profile cilly-bwrap /usr/bin/bwrap flags=(unconfined) {\n  userns,\n}\n")
        prof = write(os.path.join(self.tmp.name, "bwrap-userns-restrict"), "profile bwrap /usr/bin/bwrap flags=(unconfined) {\n}\n")
        self.have("bwrap")  # it works, thanks to the old profile: replaced all the same
        out = self.run_sh("ubuntu", userns="1", ubuntu_profile=prof, apparmor_d=d).stdout
        self.assertIn(f"Replace the AppArmor profile an older cilly wrote for bubblewrap ({old}), which lets every program", out)
        steps = [out.index(f"+ apparmor_parser -R {old}"), out.index(f"+ rm -f {old}"),
                 out.index(f"+ cp {prof} {d}/bwrap-userns-restrict"), out.index(f"+ apparmor_parser -r {d}/bwrap-userns-restrict")]
        self.assertEqual(steps, sorted(steps))
        # with no profile of Ubuntu's to put in its place, it stays, and the plan says what it does
        out = self.run_sh("ubuntu", userns="1", apparmor_d=d).stdout
        self.assertIn(f"An older cilly wrote an AppArmor profile for bubblewrap ({old}). It lets every program", out)
        for never in ("+ apparmor_parser", "+ rm", "+ cp"):
            self.assertNotIn(never, out)

    def test_another_enabled_profile_isnt_doubled_and_says_bubblewrap_still_fails(self):
        d = os.path.join(self.tmp.name, "apparmor.d")
        os.makedirs(os.path.join(d, "disable"))
        prof = write(os.path.join(self.tmp.name, "bwrap-userns-restrict"), "profile bwrap /usr/bin/bwrap {\n}\n")
        self.have("bwrap", script="#!/bin/sh\nexit 1\n")  # blocked
        theirs = write(os.path.join(d, "flatpak-bwrap"), f"profile flatpak {self.bin}/bwrap flags=(unconfined) {{\n}}\n")
        out = self.run_sh("ubuntu", userns="1", ubuntu_profile=prof, apparmor_d=d).stdout
        self.assertIn(f"AppArmor has a profile for bubblewrap already ({theirs}), so this adds none. But bubblewrap", out)
        self.assertIn("sudo aa-status", out)
        self.assertNotIn("+ cp", out)
        write(os.path.join(d, "disable", "flatpak-bwrap"), "")  # a disabled one doesn't count
        out = self.run_sh("ubuntu", userns="1", ubuntu_profile=prof, apparmor_d=d).stdout
        self.assertIn(f"+ cp {prof} {d}/bwrap-userns-restrict", out)

    def test_test_paths_count_only_on_a_dry_run(self):
        """Under sudo -E they'd come from your environment into a script that runs as root."""
        env = {"PATH": self.bin, "CILLY_OS_RELEASE": write(os.path.join(self.tmp.name, "evil"), "echo SOURCED-AS-ROOT\nID=arch\n"),
               "CILLY_UBUNTU_BWRAP_PROFILE": "/x", "CILLY_USERNS_SYSCTL": "/x", "CILLY_APPARMOR_D": "/x"}
        r = subprocess.run(["/bin/sh", INSTALL_SH, "--user", self.ME], env=env, capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        self.assertNotIn("SOURCED-AS-ROOT", r.stdout + r.stderr)
        r = self.run_sh("arch")
        self.assertNotIn("SOURCED-AS-ROOT", r.stdout)
        self.assertIn("SOURCED-AS-ROOT", subprocess.run(["/bin/sh", INSTALL_SH, "--dry-run", "--user", self.ME], env=env,
                                                        capture_output=True, text=True, timeout=30).stdout)

    def test_the_package_lists_are_updated_before_any_apt_install(self):
        self.have("python3", "qemu-system-x86_64", "qemu-img", "xorriso", "bwrap", "openssl")  # only virtiofsd is missing
        out = self.run_sh("debian").stdout
        self.assertLess(out.index("+ apt-get update"), out.index("+ env DEBIAN_FRONTEND=noninteractive apt-get install -y virtiofsd"))
        self.assertEqual(self.run_sh("ubuntu").stdout.count("+ apt-get update"), 1)  # once, with both installs

    def test_the_dry_run_checks_bubblewrap(self):
        me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.have("bwrap")  # works
        prof = write(os.path.join(self.tmp.name, "bwrap-userns-restrict"), "profile bwrap /usr/bin/bwrap {\n}\n")
        r = self.run_sh("ubuntu", userns="1", user=me, ubuntu_profile=prof)
        self.assertIn(f"bubblewrap works for {me} already, so it needs no AppArmor profile.", r.stdout)
        self.assertNotIn("+ write", r.stdout)
        self.have("bwrap", script="#!/bin/sh\nexit 1\n")  # blocked
        r = self.run_sh("ubuntu", userns="1", user=me, ubuntu_profile=prof)
        self.assertIn(f"bubblewrap can't create user namespaces for {me}, so it gets Ubuntu's AppArmor profile:", r.stdout)

    def test_unknown_systems_and_options(self):
        r = self.run_sh("alpine")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Install these yourself", r.stdout)
        r = self.run_sh("arch", "--bogus")
        self.assertEqual(r.returncode, 2)
        r = subprocess.run(["sh", INSTALL_SH, "--help"], capture_output=True, text=True, timeout=30)
        self.assertIn("--dry-run", r.stdout)
        self.assertNotIn("set -eu", r.stdout)  # only the comment at the top
        r = subprocess.run(["sh", INSTALL_SH, "--dry-run", "--user"], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (2, "--user needs a user name\n"))
        r = self.run_sh("ubuntu", user="nosuchuser-cilly")
        self.assertEqual(r.returncode, 1)
        self.assertIn("There's no user called nosuchuser-cilly here. Nothing changed.", r.stdout)
        self.assertNotIn("+ ", r.stdout)

    def test_it_never_changes_anything_on_a_dry_run(self):
        src = read(INSTALL_SH)
        # every command that changes the system goes through run(), which only prints on a dry run
        for cmd in ("pacman", "apt-get", "dnf", "zypper", "usermod", "groupadd", "apparmor_parser", "cp", "rm"):
            for line in src.splitlines():
                if re.search(rf"(^|\s){cmd}\s", line) and not line.lstrip().startswith("#") and "say " not in line:
                    self.assertRegex(line, r"\brun\b", line)
        self.assertNotIn("userns,", src)  # no profile of its own


if __name__ == "__main__":
    unittest.main()
