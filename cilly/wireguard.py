"""WireGuard VPN routes, through wireproxy.

wireproxy is a WireGuard client that runs as you, with no root and no network interface, and
offers the tunnel as a SOCKS5 proxy.  cilly runs one per WireGuard route that agents use, the
same way it runs Tor (helpers.py): inside bubblewrap when it's there, kept running while a
route wants it, stopped with cilly.

You paste the VPN's .conf file.  cilly keeps what it needs from it: the private key and the
preshared key go to Secrets, the rest to the route in Settings.  It never runs anything the file
says (wg-quick's PostUp and such are left out), and wireproxy gets a config cilly writes itself,
from checked values only, in a private folder in memory.

wireproxy's SOCKS5 server listens on 127.0.0.1 only, on a free port, with a login made up for
each run, so other users and programs on this PC can't use the tunnel.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import secrets
import shutil
import socket

from .helpers import Helper, private_dir

CONF_MAX = 16 * 1024
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*$")
IGNORED = {"postup", "postdown", "preup", "predown", "table", "saveconfig", "fwmark", "listenport"}
INTERFACE = {"privatekey", "address", "dns", "mtu"}
PEER = {"publickey", "presharedkey", "endpoint", "allowedips", "persistentkeepalive"}


def wireproxy_binary() -> str | None:
    return shutil.which("wireproxy")


def _no_zone(value) -> str:
    """Python's ipaddress takes an IPv6 zone after % with any characters, line breaks too."""
    value = str(value)
    if "%" in value or any(c < " " for c in value):
        raise ValueError(f"has an address that isn't valid: {value[:60]!r}")
    return value


def check_key(value: str, what: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise ValueError(f"the {what} isn't a WireGuard key")
    return value


def check_endpoint(value: str) -> str:
    """host:port, or [IPv6]:port."""
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([^:\[\]\s]+):(\d{1,5})", value)
    if not m:
        raise ValueError("Endpoint should be host:port")
    host, port = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    if m.group(1):
        ipaddress.IPv6Address(host)
    else:
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            if not HOST_RE.match(host):
                raise ValueError("Endpoint has a host name that isn't valid") from None
    if not 1 <= int(port) <= 65535:
        raise ValueError("Endpoint has a port that isn't valid")
    return value


def check_fields(r: dict) -> dict:
    """A WireGuard route as Settings keeps it.  Raises ValueError."""
    out = {"kind": "wireguard"}
    addrs = r.get("address")
    if not isinstance(addrs, list) or not 1 <= len(addrs) <= 4:
        raise ValueError("needs one to four addresses")
    out["address"] = [str(ipaddress.ip_interface(_no_zone(a))) for a in addrs if isinstance(a, str)]
    if len(out["address"]) != len(addrs):
        raise ValueError("has an address that isn't valid")
    dns = r.get("dns") or []
    if not isinstance(dns, list) or len(dns) > 4:
        raise ValueError("has more than four DNS servers")
    out["dns"] = [str(ipaddress.ip_address(_no_zone(d))) for d in dns]
    if r.get("mtu") is not None:
        if not isinstance(r["mtu"], int) or not 576 <= r["mtu"] <= 9000:
            raise ValueError("has an MTU outside 576 to 9000")
        out["mtu"] = r["mtu"]
    out["public_key"] = check_key(str(r.get("public_key") or ""), "public key")
    out["endpoint"] = check_endpoint(str(r.get("endpoint") or ""))
    allowed = r.get("allowed_ips") or ["0.0.0.0/0", "::/0"]
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 64:
        raise ValueError("needs one to 64 allowed IP ranges")
    out["allowed_ips"] = [str(ipaddress.ip_network(_no_zone(a), strict=False)) for a in allowed]
    if r.get("keepalive") is not None:
        if not isinstance(r["keepalive"], int) or not 0 <= r["keepalive"] <= 65535:
            raise ValueError("has a keepalive outside 0 to 65535")
        out["keepalive"] = r["keepalive"]
    return out


def parse_keys(value: str) -> tuple[str, str | None]:
    """The secret holds the private key, and after a space the preshared key, if any."""
    parts = (value or "").split()
    if len(parts) not in (1, 2):
        raise ValueError("the keys aren't readable")
    return check_key(parts[0], "private key"), check_key(parts[1], "preshared key") if len(parts) == 2 else None


def parse_conf(text: str) -> tuple[dict, str, list[str]]:
    """A pasted WireGuard .conf: (the route's fields, its keys for Secrets, what was left out)."""
    if len(text) > CONF_MAX:
        raise ValueError("That's too long for a WireGuard config")
    section, iface, peers, left_out = None, {}, [], []
    for raw in text.splitlines():
        line = re.split(r"\s[#;]|^[#;]", raw, maxsplit=1)[0].strip()
        if not line:
            continue
        if m := re.fullmatch(r"\[(\w+)\]", line):
            section = m.group(1).lower()
            if section == "peer":
                peers.append({})
            elif section != "interface":
                raise ValueError(f"cilly doesn't know the [{m.group(1)}] section")
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or section is None:
            raise ValueError("That doesn't look like a WireGuard config")
        known = INTERFACE if section == "interface" else PEER
        if key in IGNORED:
            left_out.append(key)
            continue
        if key not in known:
            raise ValueError(f"cilly doesn't know {key} in [{section.capitalize()}]")
        (iface if section == "interface" else peers[-1])[key] = value
    if len(peers) != 1:
        raise ValueError("A WireGuard route takes a config with exactly one [Peer]")
    peer = peers[0]
    for need, where in (("privatekey", iface), ("address", iface), ("publickey", peer), ("endpoint", peer)):
        if need not in where:
            raise ValueError(f"The config has no {need}")
    split = lambda v: [x.strip() for x in v.split(",") if x.strip()]  # noqa: E731
    dns = []
    for d in split(iface.get("dns", "")):
        try:
            dns.append(str(ipaddress.ip_address(d)))
        except ValueError:
            left_out.append(f"dns {d}")  # a search domain: wireproxy has no use for it
    try:
        fields = check_fields({
            "address": split(iface["address"]), "dns": dns,
            "mtu": int(iface["mtu"]) if "mtu" in iface else None,
            "public_key": peer["publickey"], "endpoint": peer["endpoint"],
            "allowed_ips": split(peer.get("allowedips", "")) or None,
            "keepalive": int(peer["persistentkeepalive"]) if "persistentkeepalive" in peer else None})
    except ValueError as e:
        raise ValueError(f"The config {e}" if str(e).startswith(("needs", "has")) else str(e)) from None
    keys = check_key(iface["privatekey"], "private key")
    if "presharedkey" in peer:
        keys += " " + check_key(peer["presharedkey"], "preshared key")
    return {k: v for k, v in fields.items() if v is not None}, keys, left_out


def wireproxy_conf(fields: dict, keys: str, port: int, user: str, password: str) -> str:
    """wireproxy's config, from checked values only: no value can hold a line break, and a last
    check makes sure of it."""
    f = check_fields(fields)
    private, psk = parse_keys(keys)
    for v in (user, password):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", v):
            raise ValueError("bad login for wireproxy")
    lines = ["[Interface]", f"PrivateKey = {private}", f"Address = {', '.join(f['address'])}"]
    if f["dns"]:
        lines.append(f"DNS = {', '.join(f['dns'])}")
    if "mtu" in f:
        lines.append(f"MTU = {f['mtu']}")
    lines += ["", "[Peer]", f"PublicKey = {f['public_key']}"]
    if psk:
        lines.append(f"PresharedKey = {psk}")
    lines += [f"Endpoint = {f['endpoint']}", f"AllowedIPs = {', '.join(f['allowed_ips'])}"]
    if "keepalive" in f:
        lines.append(f"PersistentKeepalive = {f['keepalive']}")
    lines += ["", "[Socks5]", f"BindAddress = 127.0.0.1:{int(port)}", f"Username = {user}", f"Password = {password}", ""]
    if any(c < " " or c == "\x7f" for line in lines for c in line):  # one line per setting, whatever changes above
        raise ValueError("a value for wireproxy has a control character")
    return "\n".join(lines)


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Wireproxy(Helper):
    """The wireproxy for one WireGuard route."""

    label = "wireproxy"

    def __init__(self, name: str, fields: dict, keys: str, run_dir: str, log_dir: str, use_bwrap=lambda: "auto"):
        super().__init__(use_bwrap)
        self.name, self.fields, self.keys = name, check_fields(fields), keys
        parse_keys(keys)
        self.dir = os.path.join(run_dir, name)
        self.log_dir = log_dir
        self.port = free_port()  # kept across restarts, so agents' route files stay right
        self.user, self.password = secrets.token_urlsafe(12), secrets.token_urlsafe(24)

    def conf_path(self) -> str:
        return os.path.join(self.dir, "wireproxy.conf")

    def log_path(self) -> str:
        return os.path.join(self.log_dir, f"{self.name}.log")

    def binary(self) -> str | None:
        return wireproxy_binary()

    def missing(self) -> str:
        return "wireproxy isn't installed"

    def prepare(self) -> list[str]:
        private_dir(self.log_dir)
        private_dir(self.dir)
        path = self.conf_path()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)  # in memory: it holds the key
        with os.fdopen(fd, "w") as f:
            f.write(wireproxy_conf(self.fields, self.keys, self.port, self.user, self.password))
        return [self.dir]

    def argv(self, binary: str, owner_pid: int | None) -> list[str]:
        return [binary, "-c", self.conf_path()]

    def route(self) -> dict:
        """What the agents' proxies get: wireproxy's SOCKS5 server, names looked up at the VPN's DNS."""
        out = {"kind": "socks5", "name": self.name, "host": "127.0.0.1", "port": self.port,
               "user": self.user, "password": self.password}
        if self.fields["dns"]:
            out["dns"] = self.fields["dns"][0]
        return out

    def status(self) -> dict:
        return {"running": self.running(), "error": self.error}

    def stop(self) -> None:
        super().stop()
        try:
            os.unlink(self.conf_path())
        except FileNotFoundError:
            pass


class WireGuard:
    """One wireproxy per WireGuard route in use."""

    def __init__(self, run_dir, log_dir, use_bwrap=lambda: "auto"):
        self.run_dir, self.log_dir, self.use_bwrap = run_dir, log_dir, use_bwrap
        self.procs: dict[str, Wireproxy] = {}

    def ensure(self, name: str, fields: dict, keys: str) -> Wireproxy:
        """The running wireproxy for this route, started (or started again with a changed config)."""
        p = self.procs.get(name)
        if p and (p.fields != check_fields(fields) or p.keys != keys):
            p.stop()
            p = None
        if p is None:
            p = self.procs[name] = Wireproxy(name, fields, keys, self.run_dir(), self.log_dir(), self.use_bwrap)
        p.ensure()
        return p

    def status(self) -> dict:
        return {"installed": bool(wireproxy_binary()), "routes": {n: p.status() for n, p in self.procs.items()}}

    def stop(self, name: str | None = None) -> None:
        for n in [name] if name else list(self.procs):
            p = self.procs.pop(n, None)
            if p:
                p.stop()
