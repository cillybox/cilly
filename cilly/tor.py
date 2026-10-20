"""Tor, run by cilly for routes that use it.

cilly starts its own `tor` the first time an agent's route needs it, as you, with no root.  It
runs inside bubblewrap when bubblewrap is there (the same rule as for QEMU): it sees /usr, its
own data folder and the folder with its socket, never your home.  It needs the network, so the
sandbox shares it.  It only listens on a Unix socket in a private folder of your runtime folder,
so only processes of yours can reach it, and each agent's proxy keeps to its own circuits
(IsolateSOCKSAuth).  It has no control port and stops with cilly.

Installing tor is optional: without it, an agent whose route is Tor reaches nothing (the kill
switch), and the panel says Tor isn't installed.

How cilly keeps it running is in helpers.py.
"""

from __future__ import annotations

import os
import re
import shutil

from . import config as C
from .helpers import Helper, bwrap_net_argv, private_dir

BOOT_RE = re.compile(rb"Bootstrapped (\d{1,3})%")


def tor_binary() -> str | None:
    return shutil.which("tor")


def tor_argv(tor: str, data_dir: str, sock: str, owner_pid: int | None) -> list[str]:
    """Everything on the command line: no torrc is read, not even the system's."""
    argv = [tor, "-f", "/dev/null", "--defaults-torrc", "/dev/null", "--ignore-missing-torrc",
            "DataDirectory", data_dir,
            "SocksPort", f"unix:{sock} IsolateSOCKSAuth",  # nothing on a TCP port
            "ControlPort", "0", "DNSPort", "0", "TransPort", "0",
            "ClientOnly", "1", "AvoidDiskWrites", "1",
            "Log", "notice stdout"]
    if owner_pid:  # it exits if cilly is gone.  Not in bubblewrap: its own PID namespace hides cilly
        argv += ["__OwningControllerProcess", str(owner_pid)]  # there, and --die-with-parent does this
    return argv


def bwrap_tor_argv(bwrap: str, inner: list[str], data_dir: str, run_dir: str) -> list[str]:
    return bwrap_net_argv(bwrap, inner, [data_dir, run_dir])


class Tor(Helper):
    label = "Tor"

    def __init__(self, use_bwrap=lambda: "auto"):
        super().__init__(use_bwrap)
        self.progress = 0

    @staticmethod
    def data_dir() -> str:
        return os.path.join(C.data_dir(), "tor")

    @staticmethod
    def run_dir() -> str:
        return os.path.join(C.runtime_dir(), "tor")

    def socket(self) -> str:
        return os.path.join(self.run_dir(), "socks.sock")

    def log_path(self) -> str:
        return os.path.join(self.data_dir(), "tor.log")

    def binary(self) -> str | None:
        return tor_binary()

    def status(self) -> dict:
        return {"installed": bool(tor_binary()), "running": self.running(), "progress": self.progress, "error": self.error}

    def prepare(self) -> list[str]:
        data, run = private_dir(self.data_dir()), private_dir(self.run_dir())
        try:
            os.unlink(self.socket())
        except FileNotFoundError:
            pass
        return [data, run]

    def argv(self, binary: str, owner_pid: int | None) -> list[str]:
        return tor_argv(binary, self.data_dir(), self.socket(), owner_pid)

    def on_start(self) -> None:
        self.progress = 0

    def on_line(self, line: bytes) -> None:
        if m := BOOT_RE.search(line):
            self.progress = min(100, int(m.group(1)))
