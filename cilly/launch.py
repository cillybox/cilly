"""Getting to the panel: its link, opening it in your browser, and starting it when you log in.

The link holds the panel's token, so it never goes on a command line, where other users on
this PC could read it (/proc/<pid>/cmdline).  The panel keeps it in a file only you can read,
in your runtime folder, and the browser opens a page there that forwards to it.
"""

from __future__ import annotations

import fcntl
import html
import os
import re
import shutil
import subprocess
import sys

from . import config as C
from . import host as H

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
UNIT = "cilly.service"
UNIT_MARK = "# Written by cilly"
SAFE_PATH = re.compile(r"/[A-Za-z0-9/._+@-]+")  # what a unit file can hold without quoting or escaping


def _private_file(path: str, text: str) -> None:
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def link_path() -> str:
    return os.path.join(C.ensure_private_dir(C.runtime_dir()), "panel-link")


def panel_lock() -> int | None:
    """One panel per data folder.  A second one would tidy up the first one's agents as it starts
    (their staged uploads, a move to a new image) and take over its link.  The file stays open,
    and so locked, while the panel runs.  None when another panel holds it."""
    fd = os.open(os.path.join(C.ensure_private_dir(C.data_dir()), "panel.lock"),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def save_link(url: str) -> None:
    _private_file(link_path(), url + "\n")


def read_link() -> str | None:
    try:
        with open(link_path()) as f:
            url = f.read().strip()
    except (OSError, RuntimeError):
        return None
    return url if url.startswith("http://127.0.0.1:") else None


def remove_link(url: str) -> None:
    """When the panel stops: only its own link (another panel may have started since)."""
    if read_link() == url:
        for p in (link_path(), os.path.join(C.runtime_dir(), "open.html")):
            try:
                os.unlink(p)
            except OSError:
                pass


def _default_browser(run=subprocess.run) -> str:
    try:
        r = run(["xdg-settings", "get", "default-web-browser"], capture_output=True, timeout=5, check=False)
        return r.stdout.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _sandbox_app_dirs() -> tuple:
    """Where Snap and Flatpak put the .desktop files of the apps they run."""
    return ("/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications",
            os.path.join(C._xdg("XDG_DATA_HOME", "~/.local/share"), "flatpak/exports/share/applications"))


def sandboxed_browser(desktop: str) -> bool:
    """A browser from Snap or Flatpak can't read files in your runtime folder."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.desktop", desktop or ""):
        return False
    return any(os.path.exists(os.path.join(d, desktop)) for d in _sandbox_app_dirs())


def open_in_browser(url: str, run=subprocess.run, popen=subprocess.Popen) -> str | None:
    """Open the panel in your browser.  Returns None when it did, or why it didn't."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no desktop session here"
    opener = shutil.which("xdg-open")
    if not opener:
        return "xdg-open isn't installed"
    if sandboxed_browser(_default_browser(run)):
        return "your browser comes from Snap or Flatpak and can't open the private page"
    try:
        page = os.path.join(C.ensure_private_dir(C.runtime_dir()), "open.html")
    except (OSError, RuntimeError) as e:
        return str(e)
    u = html.escape(url, quote=True)
    _private_file(page, f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={u}">'
                        f'<title>cilly</title><a href="{u}">Open cilly</a>\n')
    try:
        popen([opener, "file://" + page], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"xdg-open failed: {e}"
    return None


# -- starting with your login: a systemd user service ------------------------

def unit_dir() -> str:
    return os.path.join(C._xdg("XDG_CONFIG_HOME", "~/.config"), "systemd", "user")


def _workdir() -> str:
    """The checkout when cilly runs from one.  Installed (pipx), your home: the venv's python finds
    cilly in its own site-packages, so the unit names no versioned site-packages path that a
    Python upgrade would move."""
    checkout = os.path.dirname(PKG_DIR)
    return checkout if os.path.isfile(os.path.join(checkout, "pyproject.toml")) else "%h"


def _exec_start(python: str, workdir: str) -> str:
    """From your home, Python runs isolated (-I): `python -m` puts its folder first on sys.path, so a
    json.py or ssl.py lying in your home would be imported in place of the standard library.  From a
    checkout it needs that folder to find cilly, and it holds only cilly's own files."""
    return f"{python}{' -I' if workdir == '%h' else ''} -m cilly serve"


def unit_text(python: str = sys.executable, workdir: str | None = None) -> str:
    workdir = workdir or _workdir()
    for p in (python, workdir):
        if p != "%h" and not SAFE_PATH.fullmatch(p):
            raise SystemExit(f"{p} has characters a systemd unit can't hold safely. Use contrib/cilly.service instead.")
    return f"""{UNIT_MARK} (Settings, or the service install command).  It starts the panel when you log in, as you.
# Open the panel with `{H.cli()} open`: its link changes at every start, and it's never in the journal.
[Unit]
Description=cilly: Claude Code agents in their own VMs

[Service]
WorkingDirectory={workdir}
ExecStart={_exec_start(python, workdir)}
# stopping shuts the VMs down properly, and cilly waits up to 180 s for them, like Stop
TimeoutStopSec=210
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _systemd_user() -> bool:
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def service_status() -> dict:
    path = os.path.join(unit_dir(), UNIT)
    ours = False
    try:
        with open(path) as f:
            ours = f.readline().startswith(UNIT_MARK)
    except OSError:
        pass
    return {"available": _systemd_user(),
            "enabled": os.path.exists(os.path.join(unit_dir(), "default.target.wants", UNIT)), "ours": ours}


def _systemctl(*args, run=subprocess.run) -> None:
    try:
        r = run(["systemctl", "--user", *args], capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"systemctl --user {' '.join(args)} didn't answer within 30 s") from None
    if r.returncode != 0:
        raise SystemExit(f"systemctl --user {' '.join(args)} failed: " + r.stderr.decode(errors="replace").strip()[:300])


def install_service(run=subprocess.run) -> str:
    """Write the unit (unless it's one you wrote yourself) and enable it for your next login."""
    if not service_status()["available"]:
        raise SystemExit("systemd isn't running your session, so cilly can't start with your login here")
    os.makedirs(unit_dir(), exist_ok=True)
    path = os.path.join(unit_dir(), UNIT)
    if not os.path.exists(path) or service_status()["ours"]:
        _private_file(path, unit_text())
        os.chmod(path, 0o644)
    _systemctl("daemon-reload", run=run)
    _systemctl("enable", UNIT, run=run)
    return path


def refresh_service(run=subprocess.run) -> bool:
    """A unit cilly wrote before it ran isolated from your home: it gets -I, the rest as it was.  True when
    it changed (it applies at its next start).  A unit you wrote yourself is never touched."""
    path = os.path.join(unit_dir(), UNIT)
    if not service_status()["ours"]:
        return False
    with open(path) as f:
        text = f.read()
    if "\nWorkingDirectory=%h\n" not in text:
        return False
    new = re.sub(r"(?m)^ExecStart=(\S+) -m cilly serve$", r"ExecStart=\1 -I -m cilly serve", text)
    if new == text:
        return False
    _private_file(path, new)
    os.chmod(path, 0o644)
    _systemctl("daemon-reload", run=run)
    return True


def remove_service(run=subprocess.run) -> None:
    """Disable it, and delete the unit if cilly wrote it."""
    st = service_status()
    if st["enabled"]:
        _systemctl("disable", UNIT, run=run)
    if st["ours"]:
        os.unlink(os.path.join(unit_dir(), UNIT))
        _systemctl("daemon-reload", run=run)
