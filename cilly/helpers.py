"""Helper programs cilly runs on this PC for routes: Tor, and wireproxy for WireGuard VPNs.

Each runs as you, with no root, inside bubblewrap when bubblewrap is there (the same rule as for
QEMU).  It has the network, because it has to reach the internet, but sees only /usr, the folders
it's given and its own program, never your home.  cilly keeps it running while a route wants it:
if it stops on its own, cilly starts it again, a little later each time it stops soon after
starting.  It stops with cilly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from . import host as H

LOG_MAX = 256 * 1024
RESTART_MIN, RESTART_MAX = 5, 300  # seconds before starting a stopped helper again
STEADY = 60  # one that ran this long before stopping starts again after RESTART_MIN


def bwrap_net_argv(bwrap: str, inner: list[str], rw: list[str], ro: list[str] = (),
                   files: list[tuple[str, str]] = ()) -> list[str]:
    """Like QEMU's sandbox, but with the network.  `files` are (file, where) pairs, read-only."""
    argv = [bwrap, "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr"]
    for d in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(d):
            argv += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    argv += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
             "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf",
             "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in ro:
        argv += ["--ro-bind", p, p]
    for p in rw:
        argv += ["--bind", p, p]
    for src, dst in files:
        argv += ["--ro-bind", src, dst]
    return argv + ["--"] + inner


def private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)  # the helpers refuse folders others can read, and so would we
    return path


class Helper:
    """One helper process.  A subclass says what to run and where it keeps its files."""

    label = "helper"

    def __init__(self, use_bwrap=lambda: "auto"):
        self.use_bwrap = use_bwrap
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self.wanted = False  # set by ensure, cleared by stop: start it again if it stops on its own
        self.restart_in = RESTART_MIN
        self._started = 0.0
        self._lock = threading.Lock()

    # -- what a subclass gives ----------------------------------------------------
    def binary(self) -> str | None:
        raise NotImplementedError

    def missing(self) -> str:
        return f"{self.label} isn't installed"

    def prepare(self) -> list[str]:
        """Make its folders and files, and return the folders it may write in the sandbox."""
        raise NotImplementedError

    def argv(self, binary: str, owner_pid: int | None) -> list[str]:
        raise NotImplementedError

    def log_path(self) -> str:
        raise NotImplementedError

    def ro_paths(self) -> list[str]:
        """More it may read in the sandbox, besides /usr and its own program."""
        return []

    def files(self) -> list[tuple[str, str]]:
        """Files it sees at another path in the sandbox, read-only: (file, where)."""
        return []

    def env(self) -> dict | None:
        """Its environment, or None for cilly's own."""
        return None

    def on_start(self) -> None:
        pass

    def on_line(self, line: bytes) -> None:
        pass

    # -- running it -----------------------------------------------------------------
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> None:
        """Start it if it isn't running.  Doesn't wait for it to be ready."""
        with self._lock:
            self.wanted = True
            if self.running():
                return
            binary = self.binary()
            if not binary:
                self.error = self.missing()
                return
            try:
                self._start(binary)
            except (OSError, ValueError) as e:
                self.error = f"{self.label} didn't start: {e}"

    def command(self, binary: str) -> list[str]:
        """What to run: its argv, inside bubblewrap when that's there.  Makes its files first."""
        rw = self.prepare()
        mode = self.use_bwrap()
        bwrap = shutil.which("bwrap") if mode != "off" else None
        if bwrap:
            problem = H.bwrap_check(bwrap, fresh=True)
            if problem:
                raise ValueError(H.bwrap_refusal(problem))
            real = os.path.realpath(binary)
            ro = [] if real.startswith("/usr/") else [real]  # a program outside /usr, such as in ~/go/bin
            return bwrap_net_argv(bwrap, self.argv(real, None), rw, ro + self.ro_paths(),  # its own PID namespace
                                  self.files())                                        # hides cilly: --die-with-parent
        if mode == "required":                                                          # watches it instead
            raise ValueError("vm.use_bwrap is 'required' but bwrap is not installed")
        return self.argv(binary, os.getpid())

    def _start(self, binary: str) -> None:
        argv = self.command(binary)
        self.error, self._started = None, time.monotonic()
        self.on_start()
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     cwd="/", start_new_session=True, env=self.env())
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        """Its log (capped) for you to read, and its lines for the subclass."""
        written = 0
        fd = os.open(self.log_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as log:
            for line in proc.stdout:
                self.on_line(line)
                if written < LOG_MAX:
                    log.write(line)
                    log.flush()
                    written += len(line)
        proc.stdout.close()
        code = proc.wait()
        with self._lock:
            if proc is not self.proc or not self.wanted:
                return
            ran = time.monotonic() - self._started
            self.restart_in = RESTART_MIN if ran >= STEADY else min(RESTART_MAX, self.restart_in * 2)
            self.error = (f"{self.label} stopped (exit {code}). Starting it again in {self.restart_in} s. "
                          f"Its log is in {self.log_path()}.")
            timer = threading.Timer(self.restart_in, self._restart, args=(proc,))
            timer.daemon = True
            timer.start()

    def _restart(self, gone: subprocess.Popen) -> None:
        with self._lock:
            if self.proc is not gone or not self.wanted:  # stopped, or already started again
                return
        self.ensure()

    def stop(self) -> None:
        with self._lock:
            self.wanted = False
            if self.running():
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
