"""What this Linux host is, and how to get what cilly needs onto it.

cilly runs as you.  Only a few one-time steps need root: installing distribution packages,
access to /dev/kvm and, on Ubuntu, an AppArmor profile that lets bubblewrap build its sandbox.
install.sh does those.  This module finds out which are missing and what the command is on
this distribution, so the doctor and setup commands and the setup page can say it.
"""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALL_SH = os.path.join(PKG_DIR, "install.sh")
OS_RELEASE = ("/etc/os-release", "/usr/lib/os-release")
# Kernel settings that stop unprivileged user namespaces, which bubblewrap needs, and the value that stops them
USERNS_SETTINGS = {  # in this order: the first two block user namespaces for everyone, AppArmor only without a profile
    "clone": ("/proc/sys/kernel/unprivileged_userns_clone", "0"),  # older Debian and some hardened kernels
    "max": ("/proc/sys/user/max_user_namespaces", "0"),
    "apparmor": ("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "1"),  # Ubuntu 23.10 and newer, on by default
}
# Arch installs virtiofsd outside $PATH, Debian and Fedora in libexec, older Ubuntu next to QEMU
VIRTIOFSD_PATHS = ("/usr/lib/virtiofsd", "/usr/libexec/virtiofsd", "/usr/lib/qemu/virtiofsd")

# The package manager and package names per family.  install.sh holds the same table, and a
# test keeps them equal.
FAMILIES = {
    "arch": {"label": "Arch Linux", "install": "sudo pacman -S --needed",
             "qemu": "qemu-base", "xorriso": "libisoburn", "bwrap": "bubblewrap", "virtiofsd": "virtiofsd",
             "secret-tool": "libsecret", "tor": "tor", "openssl": "openssl"},
    "debian": {"label": "Debian or Ubuntu", "install": "sudo apt install",
               "qemu": "qemu-system-x86 qemu-utils", "xorriso": "xorriso", "bwrap": "bubblewrap", "virtiofsd": "virtiofsd",
               "secret-tool": "libsecret-tools", "tor": "tor", "openssl": "openssl"},
    "fedora": {"label": "Fedora", "install": "sudo dnf install",
               "qemu": "qemu-system-x86-core qemu-img", "xorriso": "xorriso", "bwrap": "bubblewrap", "virtiofsd": "virtiofsd",
               "secret-tool": "libsecret", "tor": "tor", "openssl": "openssl"},
    "suse": {"label": "openSUSE", "install": "sudo zypper install",
             "qemu": "qemu-x86 qemu-tools", "xorriso": "xorriso", "bwrap": "bubblewrap", "virtiofsd": "virtiofsd",
             "secret-tool": "libsecret-tools", "tor": "tor", "openssl": "openssl"},
}
NEEDS = ("qemu", "xorriso", "bwrap", "virtiofsd", "secret-tool", "tor", "openssl")  # tor: optional, for Tor routes
# Families cilly knows but doesn't support (install.sh stops before it installs anything there)
UNSUPPORTED = {
    "rhel": "RHEL, CentOS Stream, Rocky Linux and AlmaLinux aren't supported yet. Their QEMU is qemu-kvm in "
            "/usr/libexec, which cilly hasn't been tried with.",
}


@functools.lru_cache(maxsize=4)
def os_release(paths=OS_RELEASE) -> dict:
    for p in paths:
        try:
            with open(p) as f:
                text = f.read()
        except OSError:
            continue
        out = {}
        for line in text.splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and not k.startswith("#"):
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
        return out
    return {}


def family(rel: dict) -> str | None:
    """arch, debian, fedora, suse or rhel (not supported), from ID and ID_LIKE.  None for anything else."""
    ids = [rel.get("ID", "").lower()] + rel.get("ID_LIKE", "").lower().split()
    # before Fedora: these say "fedora" in ID_LIKE, but have RHEL's packages
    if any(i in ("rhel", "centos", "rocky", "almalinux", "ol") for i in ids):
        return "rhel"
    for i in ids:
        if i in ("arch", "archarm", "manjaro", "endeavouros"):
            return "arch"
        if i in ("debian", "ubuntu"):
            return "debian"
        if i == "fedora":
            return "fedora"
        if i in ("suse", "opensuse") or i.startswith("opensuse"):
            return "suse"
    return None


def install_command(fam: str | None, needs) -> str:
    """One command that installs the packages for `needs` on this family, or "" if unknown."""
    f = FAMILIES.get(fam or "")
    if not f:
        return ""
    pkgs = [p for n in needs for p in f[n].split()]
    return f"{f['install']} {' '.join(dict.fromkeys(pkgs))}" if pkgs else ""


def find_virtiofsd() -> str | None:
    return shutil.which("virtiofsd") or next((p for p in VIRTIOFSD_PATHS if os.access(p, os.X_OK)), None)


def userns_block(settings=None) -> str | None:
    """Which kernel setting stops bubblewrap's user namespaces: "apparmor", "clone", "max", or None."""
    for kind, (path, blocked) in (settings or USERNS_SETTINGS).items():
        try:
            with open(path) as f:
                if f.read().strip() == blocked:
                    return kind
        except OSError:
            pass
    return None


# What to do about each, in the words the panel and the VM's error show.  install.sh only helps with AppArmor.
BWRAP_FIX = {
    "apparmor": "Ubuntu's AppArmor blocks the user namespaces it needs until bubblewrap has a profile. "
                "cilly's install.sh turns on Ubuntu's own profile for it.",
    "clone": "The kernel setting kernel.unprivileged_userns_clone is 0. Turn it on with "
             "sudo sysctl kernel.unprivileged_userns_clone=1, and put that line in a file in /etc/sysctl.d/ to keep it.",
    "max": "The kernel setting user.max_user_namespaces is 0. Raise it with sudo sysctl user.max_user_namespaces=15000, "
           "and put that line in a file in /etc/sysctl.d/ to keep it.",
}

# What install.sh turns on.  cilly never writes a profile of its own: one that only allows user
# namespaces would let every program that runs bubblewrap have them.
UBUNTU_BWRAP_PROFILE = "/usr/share/apparmor/extra-profiles/bwrap-userns-restrict"
NO_UBUNTU_PROFILE = ("Ubuntu's AppArmor blocks the user namespaces it needs until bubblewrap has a profile, "
                     "and this Ubuntu has none to turn on. Updating Ubuntu may add one.")

# The command for each, for the page to copy (install.sh for AppArmor, which the page shows anyway)
BWRAP_CMD = {"clone": "sudo sysctl kernel.unprivileged_userns_clone=1", "max": "sudo sysctl user.max_user_namespaces=15000"}

_bwrap_cache: dict = {}
_bwrap_lock = threading.Lock()
BWRAP_RETRY = 30  # seconds the host status keeps showing a failed check (a VM start always checks again)
BWRAP_TIMEOUT = 15


def bwrap_check(bwrap: str, run=subprocess.run, fresh: bool = False) -> dict | None:
    """None when bubblewrap can build a sandbox like the one around QEMU.  Otherwise
    {"why": what went wrong, "kind": apparmor, clone, max, timeout or error, "fix": what to do,
    "cmd": a command that does it, or ""}.

    A working result is kept for the life of the panel.  A failed one is kept for BWRAP_RETRY
    seconds for the host status only: with fresh=True, as at every VM start, it's checked again."""
    with _bwrap_lock:
        hit = _bwrap_cache.get(bwrap)
        if hit and (hit[1] is None or (not fresh and time.monotonic() - hit[0] < BWRAP_RETRY)):
            return hit[1]
    kind, ran = "error", True
    try:
        r = run([bwrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--", "true"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=BWRAP_TIMEOUT, check=False)
        why = None if r.returncode == 0 else (
            next((ln.strip() for ln in r.stderr.decode(errors="replace").splitlines() if ln.strip()), "")[:200]
            or f"it exited with {r.returncode}")
    except subprocess.TimeoutExpired:
        why, kind = f"it didn't finish within {BWRAP_TIMEOUT} s", "timeout"
    except OSError as e:  # not a namespace problem: the program itself didn't start
        why, ran = f"it couldn't run ({e.strerror or e.__class__.__name__})", False
    out = None
    if why is not None:
        block = userns_block() if kind == "error" and ran else None
        kind = block or kind
        fix = NO_UBUNTU_PROFILE if kind == "apparmor" and not os.path.isfile(UBUNTU_BWRAP_PROFILE) else BWRAP_FIX.get(kind, "")
        out = {"why": why, "kind": kind, "fix": fix, "cmd": BWRAP_CMD.get(kind, "")}
    with _bwrap_lock:
        _bwrap_cache[bwrap] = (time.monotonic(), out)
    return out


def bwrap_check_again() -> None:
    """Check again in setup: the next host status runs a failed check once more (a fix may have just gone in)."""
    with _bwrap_lock:
        for b in [b for b, (_, out) in _bwrap_cache.items() if out is not None]:
            del _bwrap_cache[b]


def bwrap_refusal(problem: dict) -> str:
    """Why a VM doesn't start when bubblewrap is installed but can't build its sandbox."""
    return (f"bubblewrap is installed but can't build its sandbox here: {problem['why']}. "
            + (problem["fix"] + " " if problem["fix"] else "")
            + 'To run QEMU without it, set vm.use_bwrap to "off" in config.json yourself.')


def kvm_problem(dev: str = "/dev/kvm") -> str | None:
    if os.access(dev, os.R_OK | os.W_OK):
        return None
    if not os.path.exists(dev):
        return "missing"  # no virtualization in the firmware, or the kvm module isn't loaded
    return "no-access"    # not in the kvm group (yet: it takes a new login)


def _python() -> str:
    """The Python that runs cilly, as you'd type it: python3 when that's the one, as on most systems."""
    exe = os.path.realpath(sys.executable)
    for name in ("python3", os.path.basename(sys.executable)):
        found = shutil.which(name)
        if found and os.path.realpath(found) == exe:
            return name
    return shlex.quote(sys.executable)  # python3 is older here, say, and cilly runs with python3.11


def cli() -> str:
    """How to run cilly here: python3 -m cilly in its folder when it runs from a checkout, else the
    installed `cilly` command (pipx starts the panel as python -m cilly too, from its own venv)."""
    if os.path.isfile(os.path.join(os.path.dirname(PKG_DIR), "pyproject.toml")):
        return f"{_python()} -m cilly"
    if shutil.which("cilly") or os.path.basename(sys.argv[0] or "") == "cilly":
        return "cilly"
    return f"{shlex.quote(sys.executable)} -m cilly"


def info(tools: dict) -> dict:
    """For the setup page and the doctor command: the distribution, the command for each missing piece,
    and what install.sh would do here.  It's only offered where it helps: on a distribution it
    supports, for packages, /dev/kvm access, or bubblewrap blocked by AppArmor."""
    rel = os_release()
    fam = family(rel)
    supported = fam in FAMILIES
    problem = tools.get("bwrap_problem") or {}
    use_bwrap = tools.get("use_bwrap", "auto") != "off"
    kvm = kvm_problem()
    root = [n for n, missing in (("qemu", not (tools.get("qemu") and tools.get("qemu-img"))), ("xorriso", not tools.get("xorriso")),
                                 ("openssl", tools.get("keep_tokens") and not tools.get("openssl")),
                                 ("bwrap", use_bwrap and not tools.get("bwrap") and not problem),
                                 ("kvm", kvm == "no-access"), ("apparmor", use_bwrap and problem.get("kind") == "apparmor" and problem.get("fix") != NO_UBUNTU_PROFILE))
            if missing] if supported else []
    return {"distro": rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux", "family": fam,
            "unsupported": UNSUPPORTED.get(fam or "", ""),
            "cmds": {n: install_command(fam, [n]) for n in NEEDS},
            "missing_cmd": install_command(fam, [n for n in root if n in ("qemu", "xorriso", "bwrap", "openssl")]),
            "root_steps": root, "install_sh": f"sudo sh {shlex.quote(INSTALL_SH)}" if root else "",
            "kvm": kvm, "cli": cli()}
