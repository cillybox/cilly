"""SSH tunnels to model servers on another machine, such as a rented GPU.

cilly opens the tunnel itself with `ssh -L`, and the model server is then at a port on this PC's
loopback.  It never touches your own SSH setup:

- It makes a key pair just for this server.  The private key goes to Secrets and is written to a
  private folder in memory only while the tunnel runs.  You put the public key on the server.
- ssh reads no config (-F /dev/null), no key but that one, no agent, and a known-hosts file of
  cilly's own.  The first time, cilly fetches the server's host key and shows its fingerprint.
  Once you confirm it, ssh refuses any other.
- ssh runs as you, inside bubblewrap when it's there (helpers.py): it sees /usr, its own folder
  and a passwd file cilly writes, never your home.  It runs no command on the server (-N).
- With a route, the SSH connection itself leaves through Tor, your VPN or a proxy.  ssh's
  ProxyCommand is egress_proxy.py --pipe, which dials the route as an agent's proxy does.

The tunnel listens on 127.0.0.1 only.  Before anything is sent to it, cilly checks that the
port is held by you, so another account on this PC can't take it while ssh reconnects.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

from .helpers import Helper, private_dir
from .wireguard import HOST_RE, free_port

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}\$?", re.IGNORECASE)
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
KEY_BEGIN, KEY_END = "-----BEGIN OPENSSH PRIVATE KEY-----", "-----END OPENSSH PRIVATE KEY-----"
SCAN_TIMEOUT = 60
READY_TIMEOUT = 60  # Tor can take a while to build a circuit
PROC_TCP = ("/proc/net/tcp", "/proc/net/tcp6")
NO_LOGIN = ["-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no", "-o", "HostbasedAuthentication=no"]


class TunnelError(Exception):
    pass


def ssh_binary() -> str | None:
    return shutil.which("ssh")


def check_ssh(s) -> dict:
    """The tunnel's settings as config.json keeps them.  Raises ValueError."""
    if not isinstance(s, dict) or set(s) - {"host", "port", "user", "route", "host_key", "public_key"}:
        raise ValueError("ssh takes host, port, user, route, host_key and public_key")
    host = str(s.get("host") or "").lower()
    if not (HOST_RE.match(host) or re.fullmatch(r"[0-9a-f:.]{2,45}", host)) or host.startswith("-"):
        raise ValueError("ssh needs the server's host name or IP address")
    port = s.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("ssh has a port that isn't a number from 1 to 65535")
    user = str(s.get("user") or "")
    if not USER_RE.fullmatch(user):
        raise ValueError("ssh needs a user name, like root or ubuntu")
    route = s.get("route") or "direct"
    if not isinstance(route, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", route):
        raise ValueError("ssh has a route name that isn't valid")
    out = {"host": host, "port": port, "user": user, "route": route}
    if s.get("host_key"):
        out["host_key"] = check_host_key(s["host_key"])
    if s.get("public_key"):
        out["public_key"] = check_public_key(s["public_key"])
    return out


def _blob(line: str) -> tuple[str, bytes]:
    parts = str(line).split()
    if len(parts) != 2 or parts[0] not in KEY_TYPES:
        raise ValueError("that isn't an SSH key cilly knows")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise ValueError("that isn't an SSH key cilly knows") from None
    n = int.from_bytes(blob[:4], "big")
    if len(blob) < 4 + n or blob[4:4 + n].decode("ascii", "replace") != parts[0] or len(blob) > 4096:
        raise ValueError("that isn't an SSH key cilly knows")
    return parts[0], blob


def check_host_key(line) -> str:
    """"type base64", the server's host key."""
    kind, blob = _blob(line)
    return f"{kind} {base64.b64encode(blob).decode()}"


def check_public_key(line) -> str:
    """cilly's own public key for the server, with its comment."""
    parts = str(line).split()
    if len(parts) != 3 or not re.fullmatch(r"cilly-[a-z0-9-]{1,32}", parts[2]):
        raise ValueError("that isn't a public key cilly made")
    return check_host_key(" ".join(parts[:2])) + " " + parts[2]


def fingerprint(line: str) -> str:
    """SHA256:..., as ssh-keygen -l prints it."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(_blob(line)[1]).digest()).decode().rstrip("=")


def pack_key(text: str) -> str:
    """A private key file as one line, for Secrets."""
    lines = [ln.strip() for ln in text.strip().splitlines()]
    if len(lines) < 3 or lines[0] != KEY_BEGIN or lines[-1] != KEY_END:
        raise ValueError("that isn't an OpenSSH private key")
    body = "".join(lines[1:-1])
    base64.b64decode(body, validate=True)
    return body


def unpack_key(line: str) -> str:
    try:
        body = base64.b64encode(base64.b64decode(str(line).strip(), validate=True)).decode()
    except ValueError:
        raise ValueError("the SSH key in Secrets isn't readable") from None
    return "\n".join([KEY_BEGIN] + [body[i:i + 70] for i in range(0, len(body), 70)] + [KEY_END, ""])


def make_key(name: str) -> tuple[str, str]:
    """A new ed25519 key pair for one server: (private key as one line, public key line)."""
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise TunnelError("ssh-keygen isn't installed. It comes with the OpenSSH client.")
    try:
        with tempfile.TemporaryDirectory(prefix="cilly-key-") as d:  # 0700, and gone right after
            path = os.path.join(d, "key")
            p = subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"cilly-{name}", "-f", path],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
            if p.returncode != 0:
                raise TunnelError(f"ssh-keygen failed: {p.stderr.decode(errors='replace').strip()[:200]}")
            with open(path, encoding="ascii") as f:
                private = pack_key(f.read())
            with open(path + ".pub", encoding="ascii") as f:
                public = check_public_key(f.read().strip())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise TunnelError(f"couldn't make a key: {e}") from None
    return private, public


def authorized_line(public: str, remote_host: str, remote_port: int) -> str:
    """What goes in the server's ~/.ssh/authorized_keys: the key may forward to the model server's
    port and do nothing else, no shell and no command."""
    target = f"[{remote_host}]:{remote_port}" if ":" in remote_host else f"{remote_host}:{remote_port}"
    return f'restrict,port-forwarding,permitopen="{target}" {public}'


def listening(port: int, uid: int | None = None, paths=PROC_TCP) -> bool:
    """Whether `uid` (you, by default) listens on 127.0.0.1:port, from /proc/net/tcp."""
    uid = os.getuid() if uid is None else uid
    want = {f"0100007F:{port:04X}", f"00000000000000000000FFFF0100007F:{port:04X}"}
    for path in paths:
        try:
            with open(path, encoding="ascii") as f:
                lines = f.read(4 * 1024 * 1024).splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            p = line.split()
            if len(p) > 7 and p[1] in want and p[3] == "0A" and p[7] == str(uid):
                return True
    return False


class Tunnel(Helper):
    """The ssh -L for one model server."""

    label = "ssh"

    def __init__(self, name: str, ssh: dict, target: tuple[str, int], route: dict, key: str | None,
                 run_dir: str, log_dir: str, use_bwrap=lambda: "auto", scan: bool = False):
        super().__init__(use_bwrap)
        self.name, self.ssh, self.target, self.route = name, check_ssh(ssh), target, route
        self.key, self.scan = key, scan
        if not scan and not self.ssh.get("host_key"):
            raise TunnelError("Confirm the server's host key first.")
        if not scan:
            unpack_key(key or "")
        self.dir = os.path.join(run_dir, f"{name}-scan" if scan else name)
        self.log_dir = log_dir
        self.port = free_port()  # kept across restarts, so agents' proxies keep the right address
        self.last = ""  # ssh's last line, which says why it stopped
        self._lost_port = False

    def binary(self) -> str | None:
        return ssh_binary()

    def missing(self) -> str:
        return "ssh isn't installed. Install the OpenSSH client."

    def log_path(self) -> str:
        return os.path.join(self.log_dir, f"{self.name}.log")

    def _write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    def prepare(self) -> list[str]:
        private_dir(self.log_dir)
        private_dir(self.dir)
        private_dir(os.path.join(self.dir, "home"))
        self._write("passwd", f"cilly:x:{os.getuid()}:{os.getgid()}:cilly:{self.dir}/home:/bin/sh\n")
        known = f"cilly-{self.name} {self.ssh['host_key']}\n" if not self.scan else ""
        self._write("known_hosts", known)
        if self.route.get("kind", "direct") != "direct":
            self._write("route.json", json.dumps(self.route))
        if not self.scan:
            self._write("key", unpack_key(self.key))
            if self._lost_port:  # someone else took it while ssh was down
                self.port, self._lost_port = free_port(), False
        return [self.dir]

    def ro_paths(self) -> list[str]:
        out = []
        if self.route.get("kind", "direct") != "direct":
            from .vm import sandbox_python
            out += [PKG_DIR] + sandbox_python()[1]
            if "unix" in self.route:  # cilly's own Tor
                out.append(os.path.dirname(self.route["unix"]))
        return out

    def files(self) -> list[tuple[str, str]]:
        out = [(os.path.join(self.dir, "passwd"), "/etc/passwd")]
        if os.path.exists("/etc/hosts"):
            out.append(("/etc/hosts", "/etc/hosts"))
        return out

    def env(self) -> dict:
        """Not yours: no SSH_AUTH_SOCK, no DISPLAY for an askpass."""
        return {"PATH": "/usr/bin:/bin", "HOME": os.path.join(self.dir, "home"), "LANG": "C.UTF-8"}

    def proxy_command(self) -> str | None:
        if self.route.get("kind", "direct") == "direct":
            return None
        from .vm import sandbox_python
        py = sandbox_python()[0] if shutil.which("bwrap") and self.use_bwrap() != "off" else os.path.realpath(
            shutil.which("python3") or "/usr/bin/python3")
        cmd = shlex.join([py, "-I", os.path.join(PKG_DIR, "egress_proxy.py"), "--pipe",
                          f"{self.ssh['host']}:{self.ssh['port']}", "--route-file", os.path.join(self.dir, "route.json"),
                          "--isolate", f"ssh-{self.name}"])
        return cmd.replace("%", "%%")  # ssh expands %h and such in it

    def argv(self, binary: str, owner_pid: int | None) -> list[str]:
        s = self.ssh
        argv = [binary, "-N", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                "-o", f"HostKeyAlias=cilly-{self.name}", "-o", f"UserKnownHostsFile={self.dir}/known_hosts",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "CheckHostIP=no", "-o", "UpdateHostKeys=no",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no",
                "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ConnectTimeout=30", "-p", str(s["port"])]
        if pc := self.proxy_command():
            argv += ["-o", f"ProxyCommand={pc}"]
        if self.scan:  # only the host key: no login is tried
            argv += ["-o", "StrictHostKeyChecking=accept-new"] + NO_LOGIN
        else:
            host, port = self.target
            target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            argv += ["-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-i", f"{self.dir}/key",
                     "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                     "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                     "-L", f"127.0.0.1:{self.port}:{target}"]
        return argv + ["--", f"{s['user']}@{s['host']}"]

    def on_start(self) -> None:
        self.last = ""

    def on_line(self, line: bytes) -> None:
        text = re.sub(r"[^\x20-\x7e]", " ", line.decode("utf-8", "replace")).strip()
        if text:
            self.last = text[:300]
        if "Address already in use" in text or "cannot listen to port" in text:
            self._lost_port = True

    def ready(self) -> bool:
        return self.running() and listening(self.port)

    def wait(self, timeout: float = READY_TIMEOUT) -> None:
        """Until the forward is up, which ssh does only after it logged in.  Raises TunnelError."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ready():
                return
            if self.error and not self.running():
                raise TunnelError(self.why())
            time.sleep(0.2)
        raise TunnelError(self.why() if self.last else f"the SSH tunnel didn't come up within {timeout:g} s")

    def why(self) -> str:
        """ssh's own reason, in words where cilly knows it."""
        last = self.last
        if "Host key verification failed" in last or "REMOTE HOST IDENTIFICATION HAS CHANGED" in last:
            return "the server's host key isn't the one you confirmed. If you reinstalled it, fetch the key again."
        if "Permission denied" in last:
            return f"the server didn't take cilly's key. Add the public key to ~{self.ssh['user']}/.ssh/authorized_keys there."
        if "open failed" in last or "administratively prohibited" in last:
            return "the server refused the forward. Check the model server's address as the server sees it."
        return last or self.error or "ssh stopped"

    def status(self) -> dict:
        return {"running": self.running(), "ready": self.ready(), "port": self.port,
                "error": None if self.ready() else (self.why() if self.last or self.error else None)}

    def stop(self) -> None:
        super().stop()
        for name in ("key", "route.json"):
            try:
                os.unlink(os.path.join(self.dir, name))
            except FileNotFoundError:
                pass


def fetch_host_key(name: str, ssh: dict, route: dict, run_dir: str, log_dir: str, use_bwrap=lambda: "auto",
                   timeout: float = SCAN_TIMEOUT) -> str:
    """The server's host key, the way the tunnel will see it (through the same route), without
    logging in.  Raises TunnelError."""
    t = Tunnel(name, ssh, ("127.0.0.1", 1), route, None, run_dir, log_dir, use_bwrap, scan=True)
    binary = t.binary()
    if not binary:
        raise TunnelError(t.missing())
    try:
        argv = t.command(binary)
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False,
                           cwd="/", env=t.env())
        known = os.path.join(t.dir, "known_hosts")
        with open(known, encoding="ascii", errors="replace") as f:
            for line in f.read(64 * 1024).splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[0] == f"cilly-{name}":
                    return check_host_key(" ".join(parts[1:]))
        for raw in p.stdout.decode(errors="replace").splitlines() + p.stderr.decode(errors="replace").splitlines():
            if raw.strip():
                t.on_line(raw.encode())
        raise TunnelError(f"couldn't get its host key: {t.last or f'ssh exited with {p.returncode}'}")
    except subprocess.TimeoutExpired:
        raise TunnelError(f"it didn't answer within {timeout:g} s") from None
    except (OSError, ValueError) as e:
        raise TunnelError(str(e)) from None
    finally:
        shutil.rmtree(t.dir, ignore_errors=True)


class Tunnels:
    """One tunnel per model server reached over SSH."""

    def __init__(self, run_dir, log_dir, use_bwrap=lambda: "auto"):
        self.run_dir, self.log_dir, self.use_bwrap = run_dir, log_dir, use_bwrap
        self.procs: dict[str, Tunnel] = {}
        self._lock = threading.Lock()

    def ensure(self, name: str, ssh: dict, target: tuple[str, int], route: dict, key: str) -> Tunnel:
        """The tunnel for this server, started (or started again with changed settings)."""
        with self._lock:
            t = old = self.procs.get(name)
            if t and (t.ssh != check_ssh(ssh) or t.target != target or t.route != route or t.key != key):
                t.stop()
                t = None
            if t is None:
                t = self.procs[name] = Tunnel(name, ssh, target, route, key, self.run_dir(), self.log_dir(),
                                              self.use_bwrap)
                if old:
                    t.port = old.port  # agents that have its address keep it
        t.ensure()
        return t

    def status(self) -> dict:
        return {"installed": bool(ssh_binary()), "servers": {n: t.status() for n, t in self.procs.items()}}

    def stop(self, name: str | None = None) -> None:
        with self._lock:
            gone = [self.procs.pop(n, None) for n in ([name] if name else list(self.procs))]
        for t in gone:
            if t:
                t.stop()
