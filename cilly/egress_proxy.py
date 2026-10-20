"""Allowlisting HTTPS CONNECT proxy for one VM.

Runs as a separate process (no tokens, no panel state).  Listens on a Unix
socket that only QEMU's guestfwd relay connects to.  For every connection:

* accept only ``CONNECT host:port``,
* host must match the allowlist (exact name, or ``*.suffix`` for subdomains).
  An entry with a port (``host:8080``) allows just that port; one without allows
  the ports in the allowed set.  A host let through only by the open web (``*``)
  may use ports 443 and 80, if they are in the allowed set,
* IP literals are refused,
* the name is resolved here and only *global* unicast addresses are used, so a
  guest can never reach loopback/LAN services (including this panel),
* an address must also leave through the default route, so one of this PC's own
  addresses, or one inside any other route (the local network with its global
  IPv6 prefix, VPNs, bridges), is refused.  Routes are read from /proc for
  every connection, and if they can't be read nothing is let through,
* an address in the Never Reach list (your home's public address, servers you
  own) is refused too, after the lookup, so a name pointing at it doesn't help,
* we connect to the IP we checked (no second lookup → no DNS rebinding),
* every decision is logged as a JSON line.

A route (``--route-file``) sends the checked connection on through a SOCKS5 or HTTP proxy, or
Tor, instead of straight out, so sites see that proxy's address rather than yours.  Every check
above still runs first, here.  With Tor the name is looked up at the exit, never on this PC.
The route is a kill switch: if the file is missing or bad, or the proxy is down or refuses,
the connection is refused.  It never falls back to going out directly.  A SOCKS5 or HTTP route
can look names up over DNS-over-HTTPS, at a resolver you pick, through the route itself: the
resolver sees the route's address, and your provider sees no names.

Keeping tokens on this PC (``--tokens-file``): the VM holds only stand-ins.  For the few hosts
that need a token, this proxy answers the VM's TLS with a certificate from a CA made for this VM
start (tlsca.py), checks the real site's certificate on the way out, and swaps the stand-in in
the login headers for the real token.  Everything else stays a sealed tunnel.  It takes one
HTTP/1.1 request per connection, parsed strictly, and masks the real tokens in the reply (chunked
replies decoded first, compressed ones refused).  A
GitHub token goes only with the agent's own repository (and GraphQL, which gh needs), and a
push may be held to the agent's own branches: ones named after its id, ones it created (GitHub
confirmed it, and they're kept in --branches-file), and new ones named the way the panel says.

With ``--pipe HOST:PORT`` it is instead the ProxyCommand of cilly's SSH tunnels (sshtunnel.py):
one connection to HOST:PORT through the route in --route-file, relayed to stdin and stdout.  The
name is looked up at the far end.  No allowlist applies: it is your own server, set in Settings.

Usage: python3 -m cilly.egress_proxy --socket PATH --log PATH --allow HOST [--allow HOST ...]
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import selectors
import socket
import socketserver
import ssl
import struct
import sys
import urllib.parse
import threading
import time

HEAD_MAX = 8192
HEAD_TIMEOUT = 30
IDLE_TIMEOUT = 600
MAX_CONNS = 64
LOG_CAP = 32 * 1024 * 1024
LIST_MAX = 4 * 1024 * 1024  # the allowlist or Never Reach file

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


OPEN_WEB = "*"  # any public host; private/loopback addresses are still refused
OPEN_WEB_PORTS = (443, 80)  # and only these ports (and only those also in the proxy's own set)


class Refused(PermissionError):
    """A refusal, with a code the panel uses to tell whether allowing the host would help."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


# Refusals that allowing the host for an agent can't fix, because the port or the address is the problem.
NOT_FIXED_BY_ALLOWING = frozenset({"open_web_port", "address", "own_address", "local_network", "never_reach",
                                   "no_route_info", "route", "route_down"})
NEVER_REACH_MAX = 200


def parse_never_reach(items: list) -> list:
    """Addresses and networks (CIDR) no agent may connect to, whatever name leads there."""
    if not isinstance(items, list) or len(items) > NEVER_REACH_MAX:
        raise ValueError(f"egress.never_reach must be a list of at most {NEVER_REACH_MAX} addresses or networks")
    out = []
    for it in items:
        try:
            if not isinstance(it, str) or "%" in it:
                raise ValueError
            net = ipaddress.ip_network(it.strip(), strict=False)
            mapped = net.network_address.ipv4_mapped if net.version == 6 and net.prefixlen >= 96 else None
            # ::ffff:203.0.113.7 is how IPv6 writes an IPv4 address, and addresses are checked as IPv4
            out.append(ipaddress.IPv4Network(f"{mapped}/{net.prefixlen - 96}") if mapped else net)
        except ValueError:
            raise ValueError(f"egress.never_reach: {str(it)[:60]!r} isn't an IPv4 or IPv6 address or network "
                             "(like 203.0.113.7 or 2001:db8:1234::/48)") from None
    return out


def never_reach_refusal(ip, nets: list) -> tuple | None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for n in nets:
        if ip.version == n.version and ip in n:
            return "never_reach", f"{ip} is in Never reach ({n})"
    return None


def split_entry(entry: str) -> tuple[str, int | None]:
    """'host:8080' -> ('host', 8080); 'host' -> ('host', None)."""
    name, sep, port = entry.rpartition(":")
    return (name, int(port)) if sep else (entry, None)


def valid_port(port: str) -> bool:
    """1 to 65535 in ASCII digits ("²" is a digit to str.isdigit, but not to int)."""
    return port.isascii() and port.isdigit() and len(port) <= 5 and 0 < int(port) < 65536


def normalize_allowlist(items: list[str], *, allow_open: bool = False) -> list[str]:
    out = []
    for it in items:
        it = it.strip().lower()
        if not it:
            continue
        if it == OPEN_WEB:
            if not allow_open:
                raise ValueError("'*' (open web) is only allowed for research agents")
            out.append(it)
            continue
        name, sep, port = it.rpartition(":") if ":" in it else (it, "", "")
        name = name.rstrip(".")
        bare = name[2:] if name.startswith("*.") else name
        if not _HOST_RE.match(bare):
            raise ValueError(f"invalid allowlist entry: {it!r}")
        if sep:
            if not valid_port(port):
                raise ValueError(f"invalid port in allowlist entry: {it!r} (use 1 to 65535)")
            name += f":{int(port)}"
        out.append(name)
    return out


def _names(entry_name: str, host: str) -> bool:
    if entry_name.startswith("*."):
        return host.endswith(entry_name[1:]) and host != entry_name[2:]
    return host == entry_name


def site_allowed(host: str, port: int, allow: list[str], ports) -> str:
    """The entry that lets host:port through, or raise Refused.  An entry naming this exact port
    wins, then one naming the host with a port from `ports`, then the open web on OPEN_WEB_PORTS."""
    host = host.lower().rstrip(".")
    named = [(e, p) for e in allow if e != OPEN_WEB for n, p in [split_entry(e)] if _names(n, host)]
    for e, p in named:
        if p == port:
            return e
    if port in ports:
        for e, p in named:
            if p is None:
                return e
    if named:
        raise Refused("port", f"port {port} isn't allowed for {host}")
    if OPEN_WEB in allow:
        if port in OPEN_WEB_PORTS and port in ports:
            return OPEN_WEB
        ok = " and ".join(str(p) for p in OPEN_WEB_PORTS if p in ports)
        raise Refused("open_web_port", f"port {port}: the open web is only reachable on {ok}" if ok else
                      f"port {port}: the open web isn't reachable here, because egress.ports has neither 443 nor 80")
    raise Refused("host", "host not in allowlist")


def parse_connect(head: bytes) -> tuple[str, int]:
    """Parse the request head; return (host, port) or raise ValueError."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("non-ascii request") from None
    line = text.split("\r\n", 1)[0]
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        raise ValueError("only CONNECT is supported")
    target = parts[1]
    if target.startswith("["):
        raise ValueError("IP literals are not allowed")
    host, sep, port_s = target.rpartition(":")
    if not sep or not port_s.isdigit():
        raise ValueError("bad CONNECT target")
    port = int(port_s)
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are not allowed")
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or last.startswith("0x"):  # 127.1, 0x7f.1, 2130706433: the resolver reads these as IPv4
        raise ValueError("IP literals are not allowed")
    if not _HOST_RE.match(host):
        raise ValueError("bad hostname")
    if not 0 < port < 65536:
        raise ValueError("bad port")
    return host, port


_V4_EXTRA_DENY = [ipaddress.ip_network(n) for n in ("192.0.0.0/24", "192.88.99.0/24", "198.18.0.0/15")]
_V6_GLOBAL = ipaddress.ip_network("2000::/3")
_V6_DENY = [ipaddress.ip_network(n) for n in (
    "2001::/32",        # Teredo (embeds IPv4)
    "2002::/16",        # 6to4 (embeds IPv4)
    "64:ff9b::/96",     # NAT64 (embeds IPv4)
    "64:ff9b:1::/48",   # local-use NAT64
    "2001:db8::/32",    # documentation
)]


def address_allowed(ip: ipaddress._BaseAddress) -> bool:
    """Strict: global unicast IPv4, or IPv6 in 2000::/3 without embedded IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        else:
            return ip in _V6_GLOBAL and not any(ip in n for n in _V6_DENY) and ip.is_global
    return ip.is_global and not ip.is_multicast and not any(ip in n for n in _V4_EXTRA_DENY)


def resolve_global(host: str, port: int, resolver=socket.getaddrinfo, refuse=None, private=None, private_refuse=None) -> tuple:
    """Resolve and return the first sockaddr whose address is globally routable and,
    if given, passes `refuse(ip)` (which returns (code, reason) to refuse it).  An address
    `private(ip)` says the route may reach (a VPN's private host) passes `private_refuse` instead.

    IPv4 addresses come first, then IPv6, each in the resolver's order.  Most PCs list IPv6 first,
    and a slow IPv6 route at a provider made every download through the proxy crawl, while
    browsers there hid it."""
    infos = resolver(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    first = None
    for family, _t, _p, _c, sa in sorted(infos, key=lambda i: i[0] != socket.AF_INET):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = ipaddress.ip_address(sa[0].split("%", 1)[0])
        if private and private(ip):
            why = private_refuse(ip) if private_refuse else None
        else:
            why = ("address", "no globally routable address") if not address_allowed(ip) else refuse(ip) if refuse else None
        if why is None:
            return family, sa
        first = first or why
    raise Refused(*(first or ("address", "no globally routable address")))


PROC_NET = {"fib_trie": "/proc/net/fib_trie", "ipv6_route": "/proc/net/ipv6_route", "if_inet6": "/proc/net/if_inet6"}
_HEX2, _HEX32 = (re.compile(f"[0-9A-Fa-f]{{{n}}}") for n in (2, 32))
PROC_MAX = 16 * 1024 * 1024
_TRIE_LEAF = re.compile(r"\s*\|-- (\d{1,3}(?:\.\d{1,3}){3})")
_TRIE_INFO = re.compile(r"\s*/(\d{1,2}) \S+ (.+?)(?: tos=\d+)?")
_TRIE_OTHER = re.compile(r"(\S.*:|\s*\+-- \S+/\d{1,2}( \d+)*)")  # a table name, or an inner node of the trie
# Routes this wide stand in for the default route, such as a VPN's full tunnel (OpenVPN's 0.0.0.0/1 and
# 128.0.0.0/1, its ::/3, 2000::/4 and 3000::/4) or Miredo's 2000::/3.  They lead to the internet.
DEFAULT_LIKE = {4: 4, 6: 15}  # longest prefix that still counts as the default route


def _sort_route(net, dev, routes: list, wide: list) -> None:
    if net.prefixlen > DEFAULT_LIKE[net.version] or net.is_multicast:
        routes.append((net, dev))
    elif net.prefixlen:
        wide.append((net, dev))


def _hex_field(f: str, rx) -> str:
    if not rx.fullmatch(f):
        raise ValueError(f"unexpected field {f[:40]!r}")
    return f


def _ipv6_routes(text: str, wide: list) -> list:
    out = []
    for ln in text.splitlines():
        f = ln.split()
        if not f:
            continue
        if len(f) not in (9, 10):  # 9: a route whose nexthop has no device
            raise ValueError("/proc/net/ipv6_route has an unexpected line")
        dest = ipaddress.IPv6Address(bytes.fromhex(_hex_field(f[0], _HEX32)))
        net = ipaddress.IPv6Network(f"{dest}/{int(_hex_field(f[1], _HEX2), 16)}", strict=False)
        _sort_route(net, f[9] if len(f) == 10 else "no device", out, wide)
    return out


_V6_PUBLIC = ipaddress.ip_network("2000::/3")


def _ipv6_own(text: str) -> tuple:
    """This PC's IPv6 addresses, and the /64 around each global one.  With DHCPv6 the PC can have
    a /128 and no on-link route, yet the other devices of its home are in that /64."""
    out, nets = set(), []
    for ln in text.splitlines():
        f = ln.split()
        if not f:
            continue
        if len(f) != 6:
            raise ValueError("/proc/net/if_inet6 has an unexpected line")
        ip = ipaddress.IPv6Address(bytes.fromhex(_hex_field(f[0], _HEX32)))
        out.add(ip)
        if ip in _V6_PUBLIC:
            nets.append((ipaddress.IPv6Network(f"{ip}/64", strict=False), f"the /64 of {f[5]}"))
    return out, nets


def _ipv4_trie(text: str, wide: list) -> tuple:
    """IPv4 from /proc/net/fib_trie, across every table (Main, Local, and "Id N" ones such as
    Tailscale's 52 or wg-quick's 51820, which /proc/net/route leaves out).  Returns this PC's own
    addresses, its own ranges (local ... dev lo) and the routes, each with its table."""
    own, own_nets, routes, leaf, table = set(), [], [], None, None
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if m := _TRIE_LEAF.fullmatch(ln):
            leaf = ipaddress.IPv4Address(m.group(1))
        elif m := _TRIE_INFO.fullmatch(ln):
            if leaf is None or table is None:
                raise ValueError("/proc/net/fib_trie has a route before its address")
            net, kind = ipaddress.IPv4Network(f"{leaf}/{m.group(1)}", strict=False), m.group(2)
            if kind == "LOCAL":
                (own.add(leaf) if net.prefixlen == 32 else own_nets.append(net))
            elif kind != "BROADCAST":
                _sort_route(net, f"table {table}", routes, wide)
        elif _TRIE_OTHER.fullmatch(ln):
            if not ln.startswith(" "):
                table = ln.strip().rstrip(":")
            leaf = None
        else:
            raise ValueError("/proc/net/fib_trie has an unexpected line")
    if table is None:
        raise ValueError("/proc/net/fib_trie has no tables")
    if ipaddress.IPv4Address("127.0.0.1") not in own:  # every real table has it, so without it the file is cut short
        raise ValueError("/proc/net/fib_trie has no loopback address")
    return own, own_nets, routes


class LocalNet:
    """What this PC's routing table says about an address.  Anything that doesn't leave
    through the default route (0.0.0.0/0, ::/0) is the local network, a VPN or a bridge."""

    def __init__(self, own: set, routes: list, ipv6: bool, wide: list = (), own_nets: list = ()):
        self.own, self.routes, self.ipv6 = own, routes, ipv6
        self.own_nets = list(own_nets)  # ranges this PC answers for itself (local ... dev lo)
        self.wide = list(wide)  # routes treated like the default route (DEFAULT_LIKE)

    def refusal(self, ip) -> tuple | None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.version == 6 and not self.ipv6:
            return "no_route_info", "this PC has no IPv6"
        if ip in self.own or any(ip.version == n.version and ip in n for n in self.own_nets):
            return "own_address", f"{ip} is one of this PC's own addresses"
        for net, dev in self.routes:
            if ip.version == net.version and ip in net:
                return "local_network", f"{ip} is on this PC's own network or a VPN ({net}, {dev})"
        return None


def read_local_net(paths: dict = PROC_NET) -> LocalNet:
    """Raises OSError or ValueError if anything can't be read or parsed (the caller refuses).
    A PC without IPv6 has no IPv6 files at all, and then IPv6 addresses are refused."""
    def read(key):
        with open(paths[key]) as f:
            text = f.read(PROC_MAX + 1)
        if len(text) > PROC_MAX:  # never judge by part of a table
            raise ValueError(f"{paths[key]} is too large")
        return text
    wide: list = []
    own, own_nets, routes = _ipv4_trie(read("fib_trie"), wide)
    ipv6 = os.path.exists(paths["if_inet6"]) or os.path.exists(paths["ipv6_route"])
    if ipv6:
        own6, nets6 = _ipv6_own(read("if_inet6"))
        own |= own6
        routes += _ipv6_routes(read("ipv6_route"), wide) + nets6
    return LocalNet(own, routes, ipv6, wide, own_nets)


ROUTE_KINDS = ("direct", "socks5", "http", "tor")
ROUTE_TIMEOUT = 60  # connecting and the handshake, with time for Tor to build a circuit
ROUTE_FILE_MAX = 64 * 1024  # the route file, and the tokens file


class RouteDown(OSError):
    """The route couldn't take the connection.  Nothing goes out another way."""

    def __init__(self, reason: str, socks: int | None = None):
        super().__init__(reason)
        self.code = "route_down"
        self.socks = socks  # the SOCKS5 reply code, when the route sent one


# Tor's answers that come from one exit relay (it couldn't look the name up or reach the address),
# so a new circuit, through another exit, may well work.
TOR_RETRY = {1, 3, 4, 6}


def parse_route(obj) -> dict:
    """How a checked connection leaves: {"kind": "direct"}, or through a proxy at "host" and
    "port" or a Unix socket ("unix", cilly's own Tor), with an optional "user" and "password".
    Raises ValueError, and then nothing is let through."""
    if not isinstance(obj, dict) or obj.get("kind") not in ROUTE_KINDS:
        raise ValueError(f"route: kind must be one of {', '.join(ROUTE_KINDS)}")
    if obj["kind"] == "direct":
        return {"kind": "direct"}
    out = {"kind": obj["kind"], "name": str(obj.get("name") or obj["kind"])[:60]}
    if "unix" in obj:
        path = obj["unix"]
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 200 or "\0" in path:
            raise ValueError("route: unix must be an absolute path")
        out["unix"] = path
    else:
        host, port = str(obj.get("host", "")).strip().lower(), obj.get("port")
        try:
            host = str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            if not _HOST_RE.match(host.rstrip(".")):
                raise ValueError("route: host must be a name or an IP address") from None
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("route: port must be 1 to 65535")
        out.update(host=host, port=port)
    for k in ("user", "password"):
        v = obj.get(k)
        if v is not None:
            if not isinstance(v, str) or not 0 < len(v.encode()) <= 255 or any(c in v for c in "\r\n"):
                raise ValueError(f"route: {k} must be 1 to 255 bytes on one line")
            out[k] = v
    if ("user" in out) != ("password" in out):
        raise ValueError("route: give both a user and a password, or neither")
    if obj.get("doh") is not None:
        if out["kind"] == "tor":
            raise ValueError("route: Tor looks names up itself")
        out["doh"] = parse_doh(obj["doh"])
    if obj.get("dns") is not None:  # a WireGuard VPN's own resolver, asked over TCP through the tunnel
        if out["kind"] != "socks5" or "doh" in out:
            raise ValueError("route: dns is for a WireGuard route without doh")
        try:
            if "%" in str(obj["dns"]):
                raise ValueError("a zone")
            out["dns"] = str(ipaddress.ip_address(str(obj["dns"])))  # inside the tunnel, so a private address is fine
        except ValueError:
            raise ValueError("route: dns must be an IP address") from None
    if obj.get("private") is not None:
        if out["kind"] == "tor":
            raise ValueError("route: Tor can't reach private hosts")
        out["private"] = parse_private(obj["private"])
    return out


PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")]
PRIVATE_MAX = 50


def private_address(ip) -> bool:
    """An address a VPN's private host may have: never loopback, link-local or multicast."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in PRIVATE_NETS if n.version == ip.version)


def parse_private(entries) -> dict:
    """The private hosts a route may reach: exact names, or exact private addresses.  No wildcards."""
    if not isinstance(entries, list) or len(entries) > PRIVATE_MAX:
        raise ValueError(f"route: private must be a list of at most {PRIVATE_MAX} hosts")
    names, ips = [], []
    for e in entries:
        e = str(e).strip().lower().rstrip(".")
        if "%" in e or any(c < " " for c in e):
            raise ValueError(f"route: private: {e[:60]!r} isn't a host name or an address")
        try:
            ip = ipaddress.ip_address(e.strip("[]"))
        except ValueError:
            if not _HOST_RE.match(e):
                raise ValueError(f"route: private: {e[:60]!r} isn't a host name or an address") from None
            names.append(e)
            continue
        if not private_address(ip):
            raise ValueError(f"route: private: {e} isn't a private address")
        ips.append(str(ip))
    return {"names": names, "ips": ips}


def parse_doh(obj) -> dict:
    """A DNS-over-HTTPS resolver: its https URL, and its address so reaching it needs no lookup."""
    if not isinstance(obj, dict):
        raise ValueError("route: doh must be an object")
    u = urllib.parse.urlsplit(str(obj.get("url", "")))
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not _HOST_RE.match(host) or u.username or u.password or u.port not in (None, 443) or \
            u.query or u.fragment or not re.fullmatch(r"/[A-Za-z0-9._~/-]{0,200}", u.path or "/"):
        raise ValueError("route: doh.url must look like https://dns.example/dns-query")
    out = {"url": f"https://{host}{u.path or '/'}", "host": host, "path": u.path or "/"}
    try:
        ip = ipaddress.ip_address(str(obj.get("ip", "")))
    except ValueError:
        raise ValueError("route: doh.ip must be the resolver's IP address") from None
    if not address_allowed(ip):
        raise ValueError("route: doh.ip must be a public address")
    out["ip"] = str(ip)
    return out


DOH_MAX = 64 * 1024
DOH_CACHE_MIN, DOH_CACHE_MAX = 30, 300  # seconds an answer is kept, whatever its TTL says
DOH_UA = "Mozilla/5.0"  # generic: nothing that says cilly


def dns_query(name: str, qtype: int) -> bytes:
    q = b"".join(bytes([len(p)]) + p for p in name.encode("ascii").split(b".")) + b"\x00"
    return struct.pack(">HHHHHH", 0, 0x0100, 1, 0, 0, 0) + q + struct.pack(">HH", qtype, 1)


def _skip_name(msg: bytes, i: int) -> int:
    while True:
        n = msg[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def dns_answers(msg: bytes, qtype: int) -> tuple[list[str], int]:
    """The addresses of type `qtype` in a DNS answer, and the smallest TTL.  Raises ValueError."""
    try:
        _id, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", msg[:12])
        if flags & 0x8000 == 0 or flags & 0xF not in (0, 3):  # not an answer, or an error other than "no such name"
            raise ValueError("the resolver failed")
        i = 12
        for _ in range(qd):
            i = _skip_name(msg, i) + 4
        out, ttl = [], DOH_CACHE_MAX
        for _ in range(an):
            i = _skip_name(msg, i)
            rtype, _cls, rttl, rlen = struct.unpack(">HHIH", msg[i:i + 10])
            data = msg[i + 10:i + 10 + rlen]
            i += 10 + rlen
            if rtype == qtype and rlen == (4 if qtype == 1 else 16):
                out.append(str(ipaddress.ip_address(data)))
                ttl = min(ttl, rttl)
        return out, ttl
    except (struct.error, IndexError):
        raise ValueError("the resolver's answer is malformed") from None

def _recv_exact(s: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = s.recv(n - len(out))
        if not chunk:
            raise RouteDown("the route closed the connection")
        out += chunk
    return out


SOCKS_ERRORS = {1: "general failure", 2: "not allowed by its rules", 3: "network unreachable", 4: "host unreachable",
                5: "connection refused", 6: "timed out", 7: "command not supported", 8: "address type not supported"}


def _socks5_hello(s: socket.socket, route: dict) -> None:
    login = "user" in route
    s.sendall(b"\x05\x01" + (b"\x02" if login else b"\x00"))
    if _recv_exact(s, 2) != b"\x05" + (b"\x02" if login else b"\x00"):
        raise RouteDown("the SOCKS5 proxy wants a different login" if login else "the SOCKS5 proxy wants a login")
    if login:
        u, p = route["user"].encode(), route["password"].encode()
        s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if _recv_exact(s, 2)[1] != 0:
            raise RouteDown("the SOCKS5 proxy refused the login")


def _socks5_reply(s: socket.socket) -> tuple[str, int]:
    ver, rep, _rsv, atyp = _recv_exact(s, 4)
    if ver != 5:
        raise RouteDown("the route doesn't speak SOCKS5")
    if rep:
        raise RouteDown(f"the route refused: {SOCKS_ERRORS.get(rep, f'error {rep}')}", socks=rep)
    if atyp == 1:
        addr = str(ipaddress.IPv4Address(_recv_exact(s, 4)))
    elif atyp == 4:
        addr = str(ipaddress.IPv6Address(_recv_exact(s, 16)))
    elif atyp == 3:
        addr = _recv_exact(s, _recv_exact(s, 1)[0]).decode("ascii", "replace")
    else:
        raise RouteDown("the route sent an unknown address type")
    return addr, int.from_bytes(_recv_exact(s, 2), "big")


def socks5_connect(s: socket.socket, route: dict, ip: str, port: int) -> None:
    """Ask the SOCKS5 proxy (or Tor) on `s` for a connection to the address we checked."""
    _socks5_hello(s, route)
    addr = ipaddress.ip_address(ip)
    s.sendall(b"\x05\x01\x00" + (b"\x01" if addr.version == 4 else b"\x04") + addr.packed + port.to_bytes(2, "big"))
    _socks5_reply(s)


def socks5_connect_name(s: socket.socket, route: dict, host: str, port: int) -> None:
    """Ask for a connection to a name, looked up at the far end (cilly's own downloads, which
    go only to fixed hosts such as GitHub and Debian, and have no address checks to keep)."""
    _socks5_hello(s, route)
    name = host.encode("ascii")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    _socks5_reply(s)


def tor_resolve(s: socket.socket, route: dict, host: str) -> str:
    """Tor's RESOLVE (a SOCKS5 extension): the name is looked up at the exit, not on this PC."""
    _socks5_hello(s, route)
    name = host.encode("ascii")
    s.sendall(b"\x05\xf0\x00\x03" + bytes([len(name)]) + name + b"\x00\x00")
    return _socks5_reply(s)[0]


def http_connect(s: socket.socket, route: dict, ip: str, port: int) -> None:
    target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if "user" in route:
        cred = base64.b64encode(f"{route['user']}:{route['password']}".encode()).decode()
        head += f"Proxy-Authorization: Basic {cred}\r\n"
    s.sendall((head + "\r\n").encode())
    reply = b""
    while b"\r\n\r\n" not in reply:  # nothing after the head: the site speaks first only once we send
        chunk = s.recv(1)
        if not chunk or len(reply) > HEAD_MAX:
            raise RouteDown("the HTTP proxy didn't answer")
        reply += chunk
    status = reply.split(b"\r\n", 1)[0].split(b" ")
    if len(status) < 2 or status[1] != b"200":
        raise RouteDown("the HTTP proxy refused: " + reply.split(b"\r\n", 1)[0][:100].decode("ascii", "replace"))


PROC_TCP = {4: "/proc/net/tcp", 6: "/proc/net/tcp6"}


def _proc_tcp_addr(ip: str, port: int) -> str:
    """An address as /proc/net/tcp writes it: each 32-bit word in host byte order, then the port."""
    packed = ipaddress.ip_address(ip).packed
    return b"".join(packed[i:i + 4][::-1] for i in range(0, len(packed), 4)).hex().upper() + f":{port:04X}"


def loopback_owner(s: socket.socket, paths: dict = PROC_TCP) -> int | None:
    """The account that accepted `s`, a connection on this PC's loopback, from the other end's line
    in /proc/net/tcp (or tcp6, where a listener on :: takes IPv4 too)."""
    me, peer = s.getsockname()[:2], s.getpeername()[:2]
    looks = [(6 if ":" in peer[0] else 4, peer, me)]
    if ":" not in peer[0]:
        looks.append((6, ("::ffff:" + peer[0], peer[1]), ("::ffff:" + me[0], me[1])))
    for v, local, rem in looks:
        want = (_proc_tcp_addr(*local), _proc_tcp_addr(*rem))
        try:
            with open(paths[v], encoding="ascii") as f:
                lines = f.read(PROC_MAX).splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            p = line.split()
            if len(p) > 7 and (p[1], p[2]) == want:
                return int(p[7])
    return None


def dial_route(route: dict) -> socket.socket:
    """A connection to the route's proxy itself.  One on this PC's loopback, like a WireGuard
    route's wireproxy, must be yours: another user could take its port while it restarts."""
    try:
        if "unix" in route:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(ROUTE_TIMEOUT)
            s.connect(route["unix"])
        else:
            s = socket.create_connection((route["host"], route["port"]), timeout=ROUTE_TIMEOUT)
            s.settimeout(ROUTE_TIMEOUT)
    except OSError as e:
        raise RouteDown(f"can't reach the route's proxy ({e.strerror or e})") from None
    if s.family in (socket.AF_INET, socket.AF_INET6) and ipaddress.ip_address(s.getpeername()[0]).is_loopback:
        owner = loopback_owner(s)
        if owner != os.getuid():
            s.close()
            raise RouteDown("the route's proxy on this PC isn't yours" if owner is not None
                            else "can't tell whose the route's proxy on this PC is")
    return s


def dns_tcp(s: socket.socket, query: bytes) -> bytes:
    """One DNS query over TCP on `s`, already connected to the resolver through the route."""
    s.sendall(struct.pack(">H", len(query)) + query)
    return _recv_exact(s, struct.unpack(">H", _recv_exact(s, 2))[0])


# -- keeping tokens on this PC ------------------------------------------------------------------
TOKEN_RE = re.compile(r"[!-~]{8,4096}")  # one printable word, like every token
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
PUSH_RE = re.compile(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}")
# Branch names (config.py has the same rules for the panel): kind/slug or prefix/slug
BRANCH_KINDS = ("feat", "fix", "docs", "refactor", "test", "chore")
SLUG_RE = re.compile(r"(?=.{1,60}$)[a-z0-9]+(?:-[a-z0-9]+)*")
ZERO_RE = re.compile(r"0{40}|0{64}")
MAX_OWN = 500  # branches kept in --branches-file
TAP_MAX = 256 * 1024  # of a push's answer: GitHub's report is small, progress lines come before it


def load_branches(path: str | None) -> set[str]:
    """The branches in --branches-file: written by this proxy only, still read strictly."""
    out = set()
    if not path:
        return out
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return out
    with os.fdopen(fd, "rb") as f:
        lines = f.read(1024 * 1024).splitlines()
    for ln in lines:
        try:
            name = json.loads(ln).get("branch")
        except (ValueError, AttributeError):
            continue
        if isinstance(name, str) and PUSH_RE.fullmatch(name) and len(out) < MAX_OWN:
            out.add(name)
    return out


def parse_push_rule(push):
    """None (push anywhere in the repository), or {"agent": id, "create": None | {"kinds": [...]} | {"prefix": p}}."""
    if push is None:
        return None
    if not isinstance(push, dict) or not isinstance(push.get("agent"), str) or not re.fullmatch(r"[0-9a-f]{8,32}", push["agent"]):
        raise ValueError("tokens: github.push must name the agent")
    create = push.get("create")
    if create is not None:
        if not isinstance(create, dict) or len(create) != 1:
            raise ValueError("tokens: github.push.create must be kinds or a prefix")
        if "kinds" in create:
            if not isinstance(create["kinds"], list) or not create["kinds"] or any(k not in BRANCH_KINDS for k in create["kinds"]):
                raise ValueError(f"tokens: branch kinds are {', '.join(BRANCH_KINDS)}")
            create = {"kinds": list(create["kinds"])}
        elif not isinstance(create.get("prefix"), str) or not PUSH_RE.fullmatch(create["prefix"]) or not create["prefix"].endswith("/"):
            raise ValueError("tokens: github.push.create.prefix must be a branch prefix")
    return {"agent": push["agent"], "create": create}


def push_allowed(rule: dict, ref: str, create: bool, own) -> bool:
    """May the agent change this ref?  Its own branches, yes: named after its id (under any prefix it
    had), or created by it.  A new branch only when it's named the way the rule says.  A create
    can't overwrite anything, since GitHub refuses one for a branch that already exists."""
    if not ref.startswith("refs/heads/"):
        return False  # no tags, notes and so on
    name = ref[len("refs/heads/"):]
    if not PUSH_RE.fullmatch(name):
        return False
    if name in own or re.fullmatch(r"[A-Za-z0-9_.-]+/" + re.escape(rule["agent"]) + r"(?:-\d{1,6})?", name):
        return True
    new = rule["create"]
    if not create or new is None:
        return False
    if "prefix" in new:
        return name.startswith(new["prefix"]) and bool(SLUG_RE.fullmatch(name[len(new["prefix"]):]))
    kind, _, slug = name.partition("/")
    return kind in new["kinds"] and bool(SLUG_RE.fullmatch(slug))
PUSH_DRAIN_MAX = 1024 ** 3  # the rest of a refused push that's read and dropped before answering
REQUEST_HEAD_MAX = 64 * 1024
BODY_TIMEOUT = 120
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_DROP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "expect", "accept-encoding",
         "transfer-encoding", "content-length"}
# a path that could point somewhere else than it looks: no token goes with it
_TRICKY_PATH = re.compile(r"(^|/)\.\.?(/|$)|//|%2[ef]|%5c|\\", re.I)


def parse_tokens(obj) -> dict:
    """The tokens file: the certificate for each token host, and each stand-in with its real token
    and the hosts it may go to.  Raises ValueError."""
    if not isinstance(obj, dict):
        raise ValueError("tokens: must be an object")
    hosts = {}
    for h, files in (obj.get("hosts") or {}).items():
        if not isinstance(h, str) or not _HOST_RE.match(h) or not isinstance(files, dict):
            raise ValueError("tokens: bad host")
        cert, key = files.get("cert"), files.get("key")
        if not all(isinstance(x, str) and x.startswith("/") for x in (cert, key)):
            raise ValueError("tokens: cert and key must be absolute paths")
        hosts[h.lower()] = {"cert": cert, "key": key}
    tokens = []
    for t in obj.get("tokens") or []:
        if not isinstance(t, dict) or not all(isinstance(t.get(k), str) and TOKEN_RE.fullmatch(t[k]) for k in ("stand_in", "value")):
            raise ValueError("tokens: a stand-in and its value must be printable words")
        th = t.get("hosts")
        if not isinstance(th, list) or not th or any(h not in hosts for h in th):
            raise ValueError("tokens: a token's hosts must have certificates")
        out = {"stand_in": t["stand_in"], "value": t["value"], "hosts": list(th)}
        if t.get("github") is not None:
            g = t["github"]
            if not isinstance(g, dict) or not isinstance(g.get("repo"), str) or not REPO_RE.fullmatch(g["repo"]):
                raise ValueError("tokens: github.repo must be owner/name")
            out["github"] = {"repo": g["repo"].lower(), "push": parse_push_rule(g.get("push"))}
        tokens.append(out)
    models = {}
    for h, m in (obj.get("models") or {}).items():
        if h not in hosts or not h.endswith("." + MODEL_SUFFIX) or not isinstance(m, dict):
            raise ValueError("tokens: a model server's name must have a certificate")
        key, model = m.get("key"), m.get("model")
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,4096}", key)):
            raise ValueError("tokens: a model server's key must be a printable word")  # short ones too: "EMPTY"
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", model):
            raise ValueError("tokens: a model server's entry must name the model the agent picked")
        models[h] = dict(parse_model_url(m.get("url")), key=key, model=model, tunnel=m.get("tunnel") is True)
    return {"hosts": hosts, "tokens": tokens, "models": models}


MODEL_SUFFIX = "model.cilly.internal"  # <name>.model.cilly.internal: a model server you run (models.py)
# What an agent may ask a model server: Anthropic's Messages API, and the list of models.  Not, say,
# Ollama's own API, which has no login and can pull or delete models.
MODEL_PATHS = {("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens"), ("GET", "/v1/models")}
MODEL_BODY_MAX = 32 * 1024 * 1024  # a long conversation with images; read whole to check which model it asks for
MODEL_BODIES = threading.BoundedSemaphore(4)  # so a VM can't make the proxy hold many of them at once
SHORT_KEY = 8  # a key this short ("EMPTY", "ollama") isn't masked in replies: it would garble them, and guards nothing


SYSTEM_TURN_REFUSED = 'messages: role "system" is not supported here. cilly asks Claude Code to send it in the user message.'


def mid_system(doc: dict) -> bool:
    """Whether a Messages API request has a "system" turn among its messages, not just the system
    prompt at the top."""
    msgs = doc.get("messages")
    return isinstance(msgs, list) and any(isinstance(m, dict) and m.get("role") == "system" for m in msgs)


def parse_model_url(url) -> dict:
    u = urllib.parse.urlparse(url) if isinstance(url, str) else None
    if u is None or u.scheme not in ("http", "https") or not u.hostname or u.username or u.password \
            or u.query or u.fragment or any(c in url for c in " \t\r\n\\"):
        raise ValueError("tokens: a model server's address must be http(s)://host:port")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise ValueError("tokens: a model server's port must be a number") from None
    prefix = u.path.rstrip("/")
    if prefix and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", prefix):
        raise ValueError("tokens: a model server's path has characters cilly doesn't pass on")
    return {"scheme": u.scheme, "host": u.hostname.lower(), "port": port, "prefix": prefix}


# REST endpoints (after /repos/{repo}) that can change branches, tags or the repository itself
_CHANGES_REFS = re.compile(r"|/git/refs(?:/.*)?|/contents(?:/.*)?|/merges|/merge-upstream|/pulls/\d+/(?:merge|update-branch)"
                           r"|/releases(?:/.*)?|/branches(?:/.*)?|/tags(?:/.*)?|/rulesets(?:/.*)?|/transfer")


GITHUB_HOSTS = ("github.com", "api.github.com")


def token_allowed(token: dict, host: str, method: str, target: str) -> bool:
    """Whether this token may go with this request.  A GitHub token goes only with the agent's own
    repository (git, and the REST API), GraphQL (gh needs it), /user and /rate_limit."""
    if host not in token["hosts"]:
        return False
    g = token.get("github")
    if not g:
        return host not in GITHUB_HOSTS  # to GitHub, only a token held to the agent's repository
    path = target.split("?", 1)[0]
    if _TRICKY_PATH.search(path):
        return False
    repo = g["repo"]
    if host == "github.com":
        m = re.fullmatch(r"/([^/]+/[^/]+?)(?:\.git)?/(?:info/refs|git-upload-pack|git-receive-pack)", path)
        return bool(m) and m.group(1).lower() == repo
    if host == "api.github.com":
        p = path.lower()
        if p == "/graphql" or (p in ("/user", "/rate_limit") and method == "GET"):
            return True
        if p != f"/repos/{repo}" and not p.startswith(f"/repos/{repo}/"):
            return False
        if g["push"] is not None and method not in ("GET", "HEAD") and _CHANGES_REFS.fullmatch(p[len(f"/repos/{repo}"):]):
            return False  # held to its own branches: no changing branches, tags or the repository through the API
        return True
    return False


def parse_request(head: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    """A request head from the VM, strictly: one HTTP/1.1 request in origin form, plain header
    lines only.  Raises ValueError."""
    lines = head.decode("ascii").split("\r\n")
    m = re.fullmatch(r"([A-Z]{3,7}) (/[!-~]*) HTTP/1\.1", lines[0])
    if not m or m.group(1) not in METHODS:
        raise ValueError("not an HTTP/1.1 request cilly passes on")
    headers = []
    for ln in lines[1:]:
        name, sep, value = ln.partition(":")
        value = value.strip(" \t")
        if not sep or not _HEADER_NAME.fullmatch(name) or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value):
            raise ValueError("a header line cilly can't read")
        headers.append((name, value))
    return m.group(1), m.group(2), headers


TOKEN_HEADERS = {"authorization", "x-api-key"}  # the only headers a real token goes in


def _swap(value: str, stand_in: str, real: str | None, basic: bool = True) -> str | None:
    """A header value with the stand-in swapped for `real`, also inside a Basic login (git's) when
    `basic`.  None when the stand-in is in it and `real` is None: then the header is dropped."""
    def plain(v):
        return v if stand_in not in v else (None if real is None else v.replace(stand_in, real))
    m = re.fullmatch(r"(?i)(basic)\s+([A-Za-z0-9+/=]+)", value) if basic else None
    if m:
        try:
            inner = base64.b64decode(m.group(2), validate=True).decode("utf-8")
        except ValueError:
            return plain(value)
        if stand_in in inner:
            new = plain(inner)
            return None if new is None else f"{m.group(1)} {base64.b64encode(new.encode()).decode()}"
    return plain(value)


def receive_pack_refs(data: bytes) -> list[str] | None:
    """The refs a git push changes, from the pkt-lines before its pack.  None until the flush-pkt
    that ends them has arrived.  Raises ValueError for anything else."""
    cmds = receive_pack_commands(data)
    return None if cmds is None else [r for _, _, r in cmds]


def receive_pack_commands(data: bytes) -> list[tuple[str, str, str]] | None:
    """(old, new, ref) for each ref a git push changes; old is all zeros for a new branch."""
    refs, i = [], 0
    while True:
        if len(data) < i + 4:
            return None
        if not re.fullmatch(rb"[0-9a-f]{4}", data[i:i + 4]):
            raise ValueError("a bad pkt-line length")
        n = int(data[i:i + 4], 16)
        if n == 0:
            return refs
        if n < 5 or len(data) < i + n:
            if n < 5:
                raise ValueError("a bad pkt-line")
            return None
        line = data[i + 4:i + n].split(b"\0", 1)[0].rstrip(b"\n").decode("ascii")
        i += n
        if line.startswith("shallow "):
            continue
        parts = line.split(" ")
        if len(parts) != 3 or not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x) for x in parts[:2]):
            raise ValueError("a push command cilly can't read")
        refs.append((parts[0], parts[1], parts[2]))


def _push_caps(data: bytes) -> set:
    """The capabilities a push asked for, after the NUL on its first command line."""
    first = data[4:int(data[:4], 16)]
    return set(first.split(b"\0", 1)[1].split()) if b"\0" in first else set()


def _pkt_lines(data: bytes) -> list[bytes]:
    out, i = [], 0
    while i + 4 <= len(data) and re.fullmatch(rb"[0-9a-f]{4}", data[i:i + 4]):
        n = int(data[i:i + 4], 16)
        if n == 0:
            i += 4
            continue
        if n < 4 or i + n > len(data):
            break
        out.append(data[i + 4:i + n])
        i += n
    return out


def receive_pack_report(data: bytes, sideband: bool) -> set[str]:
    """The refs GitHub says it changed ("ok <ref>") in its answer to a push."""
    if sideband:
        data = b"".join(p[1:] for p in _pkt_lines(data) if p[:1] == b"\x01")
    return {ln[3:].strip().decode("ascii", "replace") for ln in (p.rstrip(b"\n") for p in _pkt_lines(data)) if ln.startswith(b"ok ")}


def receive_pack_refusal(data: bytes, refs: list[str], reason: str) -> bytes:
    """The answer git-receive-pack gives when it refuses every ref, so git shows the reason
    ("! [remote rejected] main -> main (reason)") instead of a bare HTTP error."""
    def pkt(b: bytes) -> bytes:
        return b"%04x" % (len(b) + 4) + b
    caps = _push_caps(data)
    why = reason.encode("ascii", "replace")
    report = pkt(b"unpack ok\n") + b"".join(pkt(b"ng %s %s\n" % (r.encode(), why)) for r in refs) + b"0000"
    if not ({b"report-status", b"report-status-v2"} & caps):
        report = b""
    if b"side-band-64k" in caps or b"side-band" in caps:
        size = 65515 if b"side-band-64k" in caps else 995
        out = b"".join(pkt(b"\x01" + report[i:i + size]) for i in range(0, len(report), size))
        return pkt(b"\x02" + why + b"\n") + out + b"0000"  # git shows band 2 as "remote: ..."
    return report


class _Masker:
    """Real tokens in a reply become asterisks of the same length, so the reply's own lengths
    still hold.  It always holds back as many bytes as the longest token less one, whatever they
    are: holding back only what could start a token would tell the VM how much of a guess was right."""

    def __init__(self, values: list[bytes]):
        self.values, self.carry = [v for v in values if v], b""
        self.hold = max((len(v) for v in self.values), default=1) - 1

    def feed(self, data: bytes, last: bool = False) -> bytes:
        data = self.carry + data
        for v in self.values:
            data = data.replace(v, b"*" * len(v))
        keep = 0 if last else min(self.hold, len(data))
        self.carry = data[len(data) - keep:] if keep else b""
        return data[:len(data) - keep] if keep else data


def doh_get(s: socket.socket, doh: dict, query: bytes) -> bytes:
    """One DNS query over HTTPS on `s`, already connected to the resolver through the route."""
    tls = ssl.create_default_context().wrap_socket(s, server_hostname=doh["host"])  # its certificate, by name
    q = base64.urlsafe_b64encode(query).rstrip(b"=").decode()
    tls.sendall((f"GET {doh['path']}?dns={q} HTTP/1.1\r\nHost: {doh['host']}\r\nAccept: application/dns-message\r\n"
                 f"User-Agent: {DOH_UA}\r\nConnection: close\r\n\r\n").encode())
    data = b""
    while len(data) <= DOH_MAX:
        chunk = tls.recv(16384)
        if not chunk:
            break
        data += chunk
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep or not head.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
        raise RouteDown("the DNS resolver refused the lookup")
    if b"transfer-encoding: chunked" in head.lower():
        body = _unchunk(body)
    return body


def _unchunk(body: bytes) -> bytes:
    out = b""
    while body:
        size, _, rest = body.partition(b"\r\n")
        n = int(size.split(b";")[0] or b"0", 16)
        if n == 0:
            break
        out, body = out + rest[:n], rest[n + 2:]
    return out


class Proxy:
    def __init__(self, allow: list[str], ports: set[int], log_path: str | None,
                 resolver=socket.getaddrinfo, connector=None, allow_file: str | None = None,
                 proc_net: dict = PROC_NET, never_reach: list = (), never_file: str | None = None,
                 route: dict | None = None, route_file: str | None = None, isolate: str = "",
                 tokens_file: str | None = None, upstream_ca: str | None = None, branches_file: str | None = None):
        self.allow = normalize_allowlist(allow, allow_open=True)
        self.allow_file = allow_file
        # with a file, nothing is let through until it has been read once
        self.never = None if never_file else parse_never_reach(list(never_reach))
        self.never_file = never_file
        # with a route file, nothing is let through until it has been read once
        self.route = None if route_file else parse_route(route or {"kind": "direct"})
        self.route_file = route_file
        self.isolate = isolate  # Tor keeps each agent on its own circuits (IsolateSOCKSAuth)
        self.tokens_file = tokens_file
        self.branches_file = branches_file
        self._own = load_branches(branches_file)  # branches this agent created, for the push rule
        self._own_lock = threading.Lock()
        self.tokens: dict | None = None  # with a file: stand-ins swapped for real tokens, read below
        self._server_tls: dict = {}  # host -> (cert, key, SSLContext)
        self._upstream_tls = ssl.create_default_context(cafile=upstream_ca)  # the real sites, checked
        self._upstream_tls.set_alpn_protocols(["http/1.1"])
        self._doh_cache: dict = {}  # (resolver, name) -> (until, [addresses])
        self._circuit = 0  # bumped when a Tor exit fails: this agent moves to a new circuit
        self._seen: dict = {}  # file -> its content when last read
        self._reload_lock = threading.Lock()
        self.ports = ports
        self.resolver = resolver
        self.proc_net = proc_net
        self._wide_logged: set = set()
        self.connector = connector or self._connect
        self._log_lock = threading.Lock()
        self._log = open(log_path, "a", buffering=1) if log_path else None
        self._log_bytes = self._log.tell() if self._log else 0
        self._slots = threading.BoundedSemaphore(MAX_CONNS)

    def log(self, **kw):
        kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = json.dumps(kw, sort_keys=True)
        with self._log_lock:
            if self._log:
                if self._log_bytes < LOG_CAP:
                    self._log.write(line + "\n")
                    self._log_bytes += len(line) + 1
                    if self._log_bytes >= LOG_CAP:
                        self._log.write('{"action": "log_cap_reached"}\n')
            else:
                print(line, file=sys.stderr)

    @staticmethod
    def _connect(family, sa):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(sa)
        s.settimeout(None)
        return s

    def maybe_reload(self) -> None:
        """Live policy: re-read the allowlist and Never Reach files when they change.  The
        panel rewrites them when you edit either; running VMs pick it up on their next
        connection.  A bad file keeps the previous list."""
        with self._reload_lock:
            new = self._changed("allow", self.allow_file, lambda ls: normalize_allowlist(ls, allow_open=True))
            if new is not None and new != self.allow:
                self.allow = new
                self.log(action="reload", allow=new)
            new = self._changed("never_reach", self.never_file, parse_never_reach)
            if new is not None and new != self.never:
                self.never = new
                self.log(action="reload", never_reach=[str(n) for n in new])
            new = self._changed("route", self.route_file, lambda ls: parse_route(json.loads("\n".join(ls))))
            if new is not None and new != self.route:
                self.route = new
                self.log(action="reload", route=new["kind"], via=new.get("name"))  # never the login
            new = self._changed("tokens", self.tokens_file, lambda ls: parse_tokens(json.loads("\n".join(ls))))
            if new is not None and new != self.tokens:
                self.tokens = new
                self.log(action="reload", token_hosts=sorted(new["hosts"]), tokens=len(new["tokens"]))  # never a value

    def _changed(self, key: str, path: str | None, parse):
        """The parsed file if its content changed since it was last read, else None.  Its bytes,
        not its inode, size or time, so two writes within one timestamp tick, or a reused inode,
        still count.  It's small enough to read for every connection."""
        if not path:
            return None
        try:
            with open(path, "rb") as f:
                data = f.read(LIST_MAX + 1)
        except OSError:
            return None
        if data == self._seen.get(key):
            return None
        self._seen[key] = data  # a bad version is logged once, not on every connection
        try:
            if len(data) > (ROUTE_FILE_MAX if key in ("route", "tokens") else LIST_MAX):
                raise ValueError("too large")
            text = data.decode("utf-8")
            new = parse([ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")])
        except (ValueError, UnicodeError) as e:
            self.log(action="reload_failed", file=key, reason=str(e))
            if key == "route":
                self.route = None  # a route we can't read lets nothing through
            return None
        return new

    def _tor_resolver(self, route: dict):
        """getaddrinfo's shape, with the lookup done by Tor at the exit."""
        def resolve(host, port, type=None, proto=None):
            for fresh in (False, True):
                s = dial_route(route)
                try:
                    ip = tor_resolve(s, self._login(route), host)
                    break
                except RouteDown as e:
                    if fresh or not self._new_circuit(route, e):
                        raise
                finally:
                    s.close()
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (ip, port) if fam == socket.AF_INET else (ip, port, 0, 0))]
        return resolve

    def _doh_resolver(self, route: dict):
        """The lookup done over DNS-over-HTTPS through the route."""
        doh = route["doh"]

        def ask(s, host, qtype):
            if route["kind"] == "http":
                http_connect(s, route, doh["ip"], 443)
            else:
                socks5_connect(s, route, doh["ip"], 443)
            return doh_get(s, doh, dns_query(host, qtype))
        return self._route_resolver(route, doh["url"], ask)

    def _vpn_dns_resolver(self, route: dict):
        """The lookup done by a WireGuard VPN's own resolver, over TCP through the tunnel, so the
        VPN's private names work too."""
        def ask(s, host, qtype):
            socks5_connect(s, route, route["dns"], 53)
            return dns_tcp(s, dns_query(host, qtype))
        return self._route_resolver(route, "dns:" + route["dns"], ask)

    def _route_resolver(self, route: dict, which: str, ask):
        """getaddrinfo's shape, with each question asked through the route by `ask` and the answers
        kept a while.  IPv4 first, like the direct path; IPv6 only when the name has no IPv4 address."""
        def answers(host, qtype):
            s = dial_route(route)
            try:
                return dns_answers(ask(s, host, qtype), qtype)
            except (OSError, ValueError) as e:
                raise e if isinstance(e, RouteDown) else RouteDown(f"the DNS lookup through the route failed ({e})") from None
            finally:
                s.close()

        def resolve(host, port, type=None, proto=None):
            key = (which, host)
            hit = self._doh_cache.get(key)
            if hit and hit[0] > time.time():
                ips = hit[1]
            else:
                ips, ttl = answers(host, 1)
                if not ips:
                    ips, ttl = answers(host, 28)
                self._doh_cache[key] = (time.time() + max(DOH_CACHE_MIN, min(ttl, DOH_CACHE_MAX)), ips)
                if len(self._doh_cache) > 1000:
                    self._doh_cache.clear()
            if not ips:
                raise socket.gaierror(f"{host} has no address")
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (ip, port) if ":" not in ip else (ip, port, 0, 0)) for ip in ips]
        return resolve

    def _login(self, route: dict) -> dict:
        if route["kind"] == "tor" and "user" not in route and self.isolate:  # its own circuits, not shared with other agents
            return dict(route, user=self.isolate, password=f"cilly-{self._circuit}" if self._circuit else "cilly")
        return route

    def _new_circuit(self, route: dict, e: RouteDown) -> bool:
        """Whether to try once more, on a new Tor circuit, which later connections keep too."""
        if route["kind"] != "tor" or not self.isolate or e.socks not in TOR_RETRY:
            return False
        self._circuit += 1
        self.log(action="new_circuit", reason=str(e))
        return True

    def connect_via(self, route: dict, family, sa) -> socket.socket:
        """The connection to the checked address, through the route.  Any failure refuses it."""
        if route["kind"] == "direct":
            return self.connector(family, sa)
        for fresh in (False, True):
            s = dial_route(route)
            try:
                if route["kind"] == "http":
                    http_connect(s, route, sa[0], sa[1])
                else:
                    socks5_connect(s, self._login(route), sa[0], sa[1])
                break
            except (OSError, ValueError) as e:
                s.close()
                if isinstance(e, RouteDown) and not fresh and self._new_circuit(route, e):
                    continue
                raise e if isinstance(e, RouteDown) else RouteDown(f"the route failed ({e})") from None
        s.settimeout(None)
        return s

    def decide(self, head: bytes) -> tuple[str, int, tuple, dict]:
        self.maybe_reload()
        host, port = parse_connect(head)
        entry = site_allowed(host, port, self.allow, self.ports)
        route = self.route
        if route is None:
            raise Refused("route", "can't read this agent's route, so nothing is let through")
        never = self.never
        if never is None:
            raise Refused("no_route_info", "can't read the Never reach list, so nothing is let through")
        try:
            net = read_local_net(self.proc_net)  # fresh for every connection, so a network change counts at once
        except (OSError, ValueError) as e:
            raise Refused("no_route_info", f"can't read this PC's routes, so nothing is let through ({e})") from None
        for net_, dev in net.wide:
            if (str(net_), dev) not in self._wide_logged:  # once each, or it's logged for every connection
                self._wide_logged.add((str(net_), dev))
                self.log(action="route_like_default", route=str(net_), dev=dev)
        resolver = self._tor_resolver(route) if route["kind"] == "tor" else \
            self._doh_resolver(route) if route.get("doh") else \
            self._vpn_dns_resolver(route) if route.get("dns") else self.resolver
        never_reach = lambda ip: never_reach_refusal(ip, never)  # noqa: E731
        family, sa = resolve_global(host, port, resolver, lambda ip: net.refusal(ip) or never_reach(ip),
                                    private=self._private(route, host, entry), private_refuse=never_reach)
        return host, port, (family, sa), route

    @staticmethod
    def _private(route: dict, host: str, entry: str):
        """For a route with private hosts: whether an address is one this host may have through it.
        Only for a site you allowed by name, never the open web, and only through this route."""
        private = route.get("private")
        if not private or entry == OPEN_WEB:
            return None
        named = host.lower().rstrip(".") in private["names"]
        ips = {ipaddress.ip_address(i) for i in private["ips"]}
        return lambda ip: private_address(ip) and (named or ip in ips)

    def handle(self, client: socket.socket) -> None:
        if not self._slots.acquire(blocking=False):
            client.sendall(b"HTTP/1.1 503 Too many connections\r\n\r\n")
            return
        try:
            self._handle(client)
        finally:
            self._slots.release()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(HEAD_TIMEOUT)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = client.recv(1024)
                if not chunk:
                    return
                head += chunk
                if len(head) > HEAD_MAX:
                    raise ValueError("request head too large")
        except (socket.timeout, ValueError) as e:
            self.log(action="deny", reason=str(e) or "timeout")
            return
        head, _, rest = head.partition(b"\r\n\r\n")
        if self._model_server(client, head, rest):
            return
        try:
            host, port, (family, sa), route = self.decide(head)
        except (ValueError, PermissionError, OSError) as e:
            target = head.split(b"\r\n", 1)[0][:200].decode("ascii", "replace")
            try:
                dh, dp = parse_connect(head)
            except ValueError:
                dh = dp = None
            self.log(action="deny", request=target, reason=str(e), host=dh, port=dp, code=getattr(e, "code", None))
            if isinstance(e, RouteDown):  # the route failed looking the name up: not our policy, so not 403
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            else:
                client.sendall(b"HTTP/1.1 403 Forbidden by cilly egress policy\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            upstream = self.connect_via(route, family, sa)
        except OSError as e:
            self.log(action="error", host=host, port=port, reason=str(e), code=getattr(e, "code", None))
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self.log(action="allow", host=host, port=port, ip=sa[0], **({} if route["kind"] == "direct" else {"via": route["name"]}))
        tokens = self.tokens
        if tokens and port == 443 and host.lower() in tokens["hosts"]:
            self._intercept(client, rest, upstream, host.lower(), tokens)
            return
        client.settimeout(None)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        sent = recv = 0
        try:
            if rest:
                upstream.sendall(rest)
                sent += len(rest)
            sent2, recv = pump(client, upstream)
            sent += sent2
        finally:
            upstream.close()
            self.log(action="close", host=host, port=port, bytes_out=sent, bytes_in=recv)


    # -- keeping tokens on this PC --------------------------------------------------------
    def _server_ctx(self, host: str, files: dict) -> ssl.SSLContext:
        hit = self._server_tls.get(host)
        if hit and hit[0] == files["cert"] and hit[1] == files["key"]:
            return hit[2]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(files["cert"], files["key"])
        ctx.set_alpn_protocols(["http/1.1"])
        self._server_tls[host] = (files["cert"], files["key"], ctx)
        return ctx

    def _model_server(self, client: socket.socket, head: bytes, rest: bytes) -> bool:
        """<name>.model.cilly.internal, when this agent's model is on that server: straight to the
        address you gave (no route: it's yours, usually on your own network), unless Never reach
        has it.  False for anything else, which the allowlist then decides."""
        self.maybe_reload()
        tokens = self.tokens
        try:
            host, port = parse_connect(head)
        except ValueError:
            return False
        host = host.lower()
        if not tokens or port != 443 or host not in tokens["models"]:
            return False
        m = tokens["models"][host]
        try:
            never = self.never
            if never is None:
                raise Refused("no_route_info", "can't read the Never reach list, so nothing is let through")
            addrs, why = [], None
            for family, _, _, _, sa in socket.getaddrinfo(m["host"], m["port"], type=socket.SOCK_STREAM):
                why = never_reach_refusal(ipaddress.ip_address(sa[0].split("%", 1)[0]), never) or None
                if not why:
                    addrs.append((family, sa))
            if not addrs:
                raise Refused(*why) if why else OSError("no address")
        except (OSError, ValueError) as e:
            self.log(action="deny", host=host, port=port, reason=f"model server: {e}"[:300], code=getattr(e, "code", None))
            client.sendall(b"HTTP/1.1 403 Forbidden by cilly egress policy\r\nContent-Length: 0\r\n\r\n")
            return True
        raw = err = None
        for family, sa in addrs:  # localhost may be ::1 first while the server listens on 127.0.0.1 only
            try:
                raw = self.connector(family, sa)
                break
            except OSError as e:
                err = e
        if raw is not None and m.get("tunnel"):  # cilly's SSH tunnel: another account could take its port while ssh reconnects
            try:
                owner = loopback_owner(raw) if ipaddress.ip_address(sa[0].split("%", 1)[0]).is_loopback else None
            except OSError:
                owner = None
            if owner != os.getuid():
                raw.close()
                raw, err = None, "the SSH tunnel's port isn't held by you"
        if raw is None:
            self.log(action="error", host=host, port=port, reason=f"model server: {err}"[:300])
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return True
        self.log(action="allow", host=host, port=port, ip=sa[0], model_server=True)
        self._intercept(client, rest, raw, host, tokens, model=m)
        return True

    def _intercept(self, client: socket.socket, rest: bytes, raw_up: socket.socket, host: str, tokens: dict,
                   model: dict | None = None) -> None:
        """A token host: TLS to the real site, its certificate checked, then the VM's TLS with this
        start's certificate, and one request with the stand-ins swapped.  A model server: the same,
        to the address you gave, plain or with TLS as its address says."""
        up = tls = None
        status = "?"
        try:
            if rest:
                raise ValueError("data before the TLS handshake")
            ctx = self._server_ctx(host, tokens["hosts"][host])
            raw_up.settimeout(ROUTE_TIMEOUT)
            if model is None:
                up = self._upstream_tls.wrap_socket(raw_up, server_hostname=host)
            elif model["scheme"] == "https":
                up = ssl.create_default_context().wrap_socket(raw_up, server_hostname=model["host"])
            else:
                up = raw_up
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            client.settimeout(HEAD_TIMEOUT)
            tls = ctx.wrap_socket(client, server_side=True)
            status = self._one_request(tls, up, host, tokens, model)
        except (OSError, ValueError, ssl.SSLError) as e:
            self.log(action="error", host=host, port=443, reason=f"while swapping tokens: {e}"[:300], code="tokens")
            try:
                if up is None and tls is None:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                elif tls is not None and status == "?":  # nothing answered yet: say why, as text
                    why = f"cilly: {e}\n".encode("ascii", "replace")[:300]
                    tls.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                                b"Content-Length: %d\r\n\r\n%s" % (len(why), why))
                    status = "400"
            except OSError:
                pass
        finally:
            for x in (tls, up, raw_up):
                try:
                    if x is not None:
                        x.close()
                except OSError:
                    pass
            self.log(action="close", host=host, port=443, status=status)

    def _one_request(self, tls, up, host: str, tokens: dict, model: dict | None = None) -> str:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = tls.recv(16384)
            if not chunk:
                return "closed"
            head += chunk
            if len(head) > REQUEST_HEAD_MAX:
                raise ValueError("request head too large")
        head, _, early = head.partition(b"\r\n\r\n")
        method, target, headers = parse_request(head)
        lower = [(n.lower(), v) for n, v in headers]
        te = [v for n, v in lower if n == "transfer-encoding"]
        cl = [v for n, v in lower if n == "content-length"]
        if (te and cl) or len(te) > 1 or len(cl) > 1 or (te and te[0].lower() != "chunked") \
                or (cl and not re.fullmatch(r"\d{1,15}", cl[0])) or any(n == "upgrade" for n, _ in lower):
            raise ValueError("a request cilly can't frame safely")
        if [v.lower() for n, v in lower if n == "host"] not in ([host], [f"{host}:443"]):
            raise ValueError("the Host header doesn't name the site")
        out, used, push, written = [], False, None, []
        if model is not None:
            path = target.split("?", 1)[0]
            messages = path == "/v1/messages"
            if (method, path) not in MODEL_PATHS:
                raise ValueError(f"a model server takes only Anthropic's Messages API, not {method} {path[:100]}")
            target = model["prefix"] + target
            server = model["host"] if ":" not in model["host"] else f"[{model['host']}]"
            out.append(("Host", server if model["port"] == (443 if model["scheme"] == "https" else 80)
                        else f"{server}:{model['port']}"))
            if model["key"]:
                out += [("Authorization", f"Bearer {model['key']}"), ("x-api-key", model["key"])]
                written += [f"Bearer {model['key']}"] + ([model["key"]] if len(model["key"]) >= SHORT_KEY else [])
                used = True
        for name, value in headers:
            low = name.lower()
            if low in _DROP or (model is not None and (low in TOKEN_HEADERS or low == "host")):
                continue  # to a model server: its own key, or none, never what the VM sent
            auth = low in TOKEN_HEADERS
            for t in tokens["tokens"]:
                if value is None or _swap(value, t["stand_in"], None, basic=auth) == value:
                    continue  # not in this header (in a login header: not even inside a Basic login)
                ok = auth and token_allowed(t, host, method, target)  # a real token only in a login header
                value = _swap(value, t["stand_in"], t["value"] if ok else None, basic=auth)
                used = used or ok
                if ok and value is not None:
                    written.append(value)  # what we wrote is masked too, should a site echo it back
                if ok and t.get("github") and t["github"]["push"] is not None:
                    push = t["github"]["push"]
            if value is not None:
                out.append((name, value))
        is_push = method == "POST" and target.split("?", 1)[0].endswith("/git-receive-pack")
        if is_push and any(n == "content-encoding" for n, _ in lower):
            raise ValueError("a compressed push")
        out += [("Accept-Encoding", "identity"), ("Connection", "close")]
        if te:
            out.append(("Transfer-Encoding", "chunked"))
        elif cl:
            out.append(("Content-Length", cl[0]))
        if any(n == "expect" and v.lower() == "100-continue" for n, v in lower):
            tls.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")  # we take the body ourselves
        tls.settimeout(BODY_TIMEOUT)
        body = self._body(tls, early, te, cl)
        if model is not None and method == "POST":  # only the model the agent picked
            with MODEL_BODIES:
                data, doc = self._model_body(body, model["model"])
            if messages and mid_system(doc):
                # Claude Code puts part of its instructions in a "system" turn after the first message.
                # Many chat templates raise on that, and the server answers 500.  On a 400 Claude Code
                # sends them again inside the user's message, the way servers without these turns want.  It
                # goes by the words: 'role "system" is not supported' does it, "a system message must come
                # first" doesn't.
                reply = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": SYSTEM_TURN_REFUSED}}).encode()
                tls.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nConnection: close\r\n"
                            b"Content-Length: %d\r\n\r\n%s" % (len(reply), reply))
                return "400"
            body, te = [data], []
            out = [(n, v) for n, v in out if n.lower() not in ("transfer-encoding", "content-length")] \
                + [("Content-Length", str(len(data)))]
        creates = []
        if is_push and push is not None:
            with self._own_lock:
                own = set(self._own)
            body, refused, creates, caps = self._check_push(body, push, own)
            if refused:
                reason, reply = refused
                drained = 0
                for piece in body:  # git sends the whole pack before it reads an answer
                    drained += len(piece)
                    if drained > PUSH_DRAIN_MAX:
                        raise ValueError(reason)
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-git-receive-pack-result\r\n"
                            b"Cache-Control: no-cache\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%s" % (len(reply), reply))
                self.log(action="deny", host=host, port=443, reason=reason[:200], code="push")
                return "refused push"
        up.sendall(f"{method} {target} HTTP/1.1\r\n".encode() + b"".join(f"{n}: {v}\r\n".encode("latin-1") for n, v in out) + b"\r\n")
        for piece in body:
            if piece:  # an empty chunk would end the body
                up.sendall((b"%x\r\n%s\r\n" % (len(piece), piece)) if te else piece)
        if te:
            up.sendall(b"0\r\n\r\n")
        self.log(action="request", host=host, method=method, path=target.split("?", 1)[0][:200], token=used)
        if not creates:
            return self._reply(up, tls, tokens, written, method)
        tap = bytearray()
        status = self._reply(up, tls, tokens, written, method, tap)
        done = receive_pack_report(bytes(tap), bool({b"side-band-64k", b"side-band"} & caps)) if status == "200" else set()
        self._add_own([r[len("refs/heads/"):] for r in creates if r in done])
        return status

    @staticmethod
    def _body(tls, early: bytes, te: list, cl: list):
        """The request's body, decoded, in pieces: exactly Content-Length bytes, or strict chunks."""
        buf = bytearray(early)

        def more():
            data = tls.recv(65536)
            if not data:
                raise ValueError("the request ended early")
            buf.extend(data)
        if cl:
            left = int(cl[0])
            while left:
                if not buf:
                    more()
                piece = bytes(buf[:left])
                del buf[:len(piece)]
                left -= len(piece)
                yield piece
            return
        if not te:
            return
        while True:
            while b"\r\n" not in buf:
                if len(buf) > 1024:
                    raise ValueError("a bad chunk size line")
                more()
            line, _, rest = bytes(buf).partition(b"\r\n")
            m = re.fullmatch(rb"([0-9A-Fa-f]{1,8})(;[!-~ \t]*)?", line)
            if not m:
                raise ValueError("a bad chunk size line")
            left = int(m.group(1), 16)
            buf[:] = rest
            if left == 0:
                while not buf.startswith(b"\r\n"):  # no trailers
                    if len(buf) >= 2:
                        raise ValueError("trailers cilly doesn't pass on")
                    more()
                return
            while left:  # passed on as it comes, so a huge chunk is never held whole
                if not buf:
                    more()
                piece = bytes(buf[:left])
                del buf[:len(piece)]
                left -= len(piece)
                yield piece
            while len(buf) < 2:
                more()
            if buf[:2] != b"\r\n":
                raise ValueError("a chunk without its line break")
            del buf[:2]

    @staticmethod
    def _model_body(body, want: str) -> tuple[bytes, dict]:
        """A request for a model server, whole, when it asks for the model the agent picked: not
        another one the server has (a paid one it forwards to, or a huge one that fills the GPU)."""
        data = bytearray()
        for piece in body:
            data += piece
            if len(data) > MODEL_BODY_MAX:
                raise ValueError("a request too large for a model server")
        try:
            doc = json.loads(bytes(data))
        except (ValueError, RecursionError):
            raise ValueError("a request for a model server that isn't JSON") from None
        if not isinstance(doc, dict) or doc.get("model") != want:
            got = doc.get("model") if isinstance(doc, dict) else None
            raise ValueError(f"this agent may use {want} on this server, not {str(got)[:100]!r}")
        return bytes(data), doc

    def _add_own(self, names: list[str]) -> None:
        """Branches GitHub confirmed this agent created: its own from now on, and on later starts."""
        with self._own_lock:
            names = [n for n in dict.fromkeys(names) if n not in self._own][:MAX_OWN - len(self._own)]
            if not names:
                return
            self._own.update(names)
            if self.branches_file:
                fd = os.open(self.branches_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write("".join(json.dumps({"branch": n}) + "\n" for n in names))
        self.log(action="push", created=names)

    @staticmethod
    def _check_push(body, rule: dict, own: set):
        """Read a push's commands before any of it goes on: only the agent's own branches may change,
        and new ones named the way the rule says (push_allowed)."""
        start, check_at = bytearray(), 0
        for piece in body:
            start += piece
            if len(start) >= check_at:  # read again only once it has doubled: a trickle costs no more
                refs = receive_pack_refs(bytes(start))
                if refs is not None:
                    break
                check_at = 2 * len(start)
            if len(start) > 1024 * 1024:
                raise ValueError("a push whose commands don't end")
        else:
            refs = receive_pack_refs(bytes(start))
            if refs is None:
                raise ValueError("a push that ended early")
        start = bytes(start)
        cmds = receive_pack_commands(start)
        bad = [r for old, _, r in cmds if not push_allowed(rule, r, bool(ZERO_RE.fullmatch(old)), own)]
        if bad:
            new = rule["create"]
            names = ("a new branch named kind/name, where the kind is " + ", ".join(new["kinds"]) if new and "kinds" in new
                     else f"a new branch starting with {new['prefix']}" if new else "")
            reason = "cilly lets this agent push only to its own branches" + (f" and to {names}" if names else "")
            return body, (f"{reason}, not {bad[0][:100]}", receive_pack_refusal(start, [r for _, _, r in cmds], reason)), [], set()
        creates = [r for old, _, r in cmds if ZERO_RE.fullmatch(old)]

        def rest():
            yield start
            yield from body
        return rest(), None, creates, _push_caps(start)

    @staticmethod
    def _reply(up, tls, tokens: dict, written: list, method: str, tap: bytearray | None = None) -> str:
        """The site's reply with every real token masked, also the exact header values we wrote and
        their Basic logins, should the site echo them.  A chunked body is decoded first, so a chunk
        boundary can't split a token.  A compressed one can't be checked, so it's refused."""
        secrets_ = {t["value"] for t in tokens["tokens"]} | set(written)
        for w in written:
            m = re.fullmatch(r"(?i)basic\s+(\S+)", w)
            if m:
                secrets_.add(m.group(1))
        mask = _Masker(sorted({x.encode() for x in secrets_}, key=len, reverse=True))
        up.settimeout(IDLE_TIMEOUT)
        buf = bytearray()

        def more() -> bool:
            try:
                data = up.recv(65536)
            except (ssl.SSLError, OSError):
                data = b""
            buf.extend(data)
            return bool(data)
        while True:  # an informational reply (103) goes on as it is, then the real one follows
            while b"\r\n\r\n" not in buf:
                if len(buf) > REQUEST_HEAD_MAX or not more():
                    raise ValueError("the site's reply had no proper head")
            head, _, rest = bytes(buf).partition(b"\r\n\r\n")
            buf[:] = rest
            lines = mask.feed(head, last=True).decode("latin-1").split("\r\n")
            if not re.fullmatch(r"HTTP/1\.[01] [1-5]\d\d(?: .*)?", lines[0]):
                raise ValueError("the site's reply had no proper status line")
            status = lines[0][9:12]
            fields = [(n.strip().lower(), v.strip().lower()) for n, _, v in (ln.partition(":") for ln in lines[1:])]
            if any(n == "content-encoding" and v not in ("", "identity") for n, v in fields):
                why = b"cilly: the site sent a compressed reply, which cilly can't check for tokens.\n"
                tls.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                            b"Content-Length: %d\r\n\r\n%s" % (len(why), why))
                return "502 compressed"
            tls.sendall("\r\n".join(lines).encode("latin-1") + b"\r\n\r\n")
            if not status.startswith("1"):
                break
        no_body = method == "HEAD" or status in ("204", "304")
        chunked = not no_body and any(n == "transfer-encoding" and "chunked" in v for n, v in fields)

        def send(out: bytes) -> None:
            if not out:
                return
            if tap is not None and len(tap) < TAP_MAX:  # a push's answer, read afterwards (receive_pack_report)
                tap.extend(out[:TAP_MAX - len(tap)])
            tls.sendall(b"%x\r\n%s\r\n" % (len(out), out) if chunked else out)
        if chunked:
            for piece in Proxy._chunks(buf, more):
                send(mask.feed(piece))
            send(mask.feed(b"", last=True))
            tls.sendall(b"0\r\n\r\n")
            return status
        if buf:  # a Content-Length body, or one that ends when the site closes: same-length masks keep it right
            out = mask.feed(bytes(buf))
            buf.clear()
            send(out)
        while more():
            out = mask.feed(bytes(buf))
            buf.clear()
            send(out)
        send(mask.feed(b"", last=True))
        return status

    @staticmethod
    def _chunks(buf: bytearray, more):
        """A chunked body's data, strictly: hex sizes, each chunk with its line break, no trailers."""
        while True:
            while b"\r\n" not in buf:
                if len(buf) > 1024 or not more():
                    raise ValueError("a bad chunk size line")
            line, _, rest = bytes(buf).partition(b"\r\n")
            m = re.fullmatch(rb"([0-9A-Fa-f]{1,8})(;[!-~ \t]*)?", line)
            if not m:
                raise ValueError("a bad chunk size line")
            left = int(m.group(1), 16)
            buf[:] = rest
            if left == 0:
                while not buf.startswith(b"\r\n"):
                    if len(buf) >= 2 or not more():
                        raise ValueError("trailers cilly doesn't pass on")
                return
            while left:
                if not buf and not more():
                    raise ValueError("a chunk that ended early")
                piece = bytes(buf[:left])
                del buf[:len(piece)]
                left -= len(piece)
                yield piece
            while len(buf) < 2:
                if not more():
                    raise ValueError("a chunk that ended early")
            if buf[:2] != b"\r\n":
                raise ValueError("a chunk without its line break")
            del buf[:2]


def pump(a: socket.socket, b: socket.socket, idle: float = IDLE_TIMEOUT) -> tuple[int, int]:
    """Copy bytes both ways until either side closes. Returns (a->b, b->a)."""
    sel = selectors.DefaultSelector()
    sel.register(a, selectors.EVENT_READ, b)
    sel.register(b, selectors.EVENT_READ, a)
    counts = {a: 0, b: 0}
    open_dirs = 2
    try:
        while open_dirs:
            events = sel.select(idle)
            if not events:
                break
            for key, _ in events:
                src, dst = key.fileobj, key.data
                try:
                    data = src.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    sel.unregister(src)
                    open_dirs -= 1
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    dst.sendall(data)
                except OSError:  # the other side is gone (a VM that was stopped): what got through still counts
                    return counts[a], counts[b]
                counts[src] += len(data)
    finally:
        sel.close()
    return counts[a], counts[b]


def serve(sock_path: str, proxy: Proxy) -> None:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            proxy.handle(self.request)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    old = os.umask(0o177)
    try:
        srv = Server(sock_path, Handler)
    finally:
        os.umask(old)
    proxy.log(action="start", allow=proxy.allow, ports=sorted(proxy.ports),
              never_reach=None if proxy.never is None else [str(n) for n in proxy.never])
    srv.serve_forever()


PIPE_RE = re.compile(r"(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252}):(\d{1,5})")


def pipe(target: str, route_file: str, isolate: str = "", stdin=None, stdout=None) -> int:
    """ssh's ProxyCommand: HOST:PORT through the route, relayed to stdin and stdout.  A route
    that is missing, bad or down ends it: never straight out instead."""
    m = PIPE_RE.fullmatch(target or "")
    if not m or not 0 < int(m.group(2)) < 65536:
        print("cilly: --pipe needs host:port", file=sys.stderr)
        return 2
    host, port = m.group(1).strip("[]"), int(m.group(2))
    try:
        with open(route_file, encoding="utf-8") as f:
            route = parse_route(json.loads(f.read(64 * 1024)))
        if route["kind"] == "direct":
            raise ValueError("route: this needs a proxy or Tor")
    except (OSError, ValueError) as e:
        print(f"cilly: can't read the route: {e}", file=sys.stderr)
        return 1
    if route["kind"] == "tor" and "user" not in route and isolate:
        route = dict(route, user=isolate, password="cilly")  # its own circuits
    try:
        s = dial_route(route)
        if route["kind"] == "http":
            http_connect(s, route, host, port)
        else:
            socks5_connect_name(s, route, host, port)
        s.settimeout(None)
    except OSError as e:
        print(f"cilly: route {route.get('name', route['kind'])}: {e}", file=sys.stderr)
        return 1
    fin = (stdin or sys.stdin.buffer).fileno()
    fout = (stdout or sys.stdout.buffer).fileno()
    sel = selectors.DefaultSelector()
    sel.register(fin, selectors.EVENT_READ, "in")
    sel.register(s, selectors.EVENT_READ, "sock")
    try:
        while True:
            for key, _ in sel.select():
                if key.data == "in":
                    data = os.read(fin, 65536)
                    if not data:
                        sel.unregister(fin)
                        s.shutdown(socket.SHUT_WR)
                    else:
                        s.sendall(data)
                else:
                    data = s.recv(65536)
                    if not data:
                        return 0
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fout, view):]
    except (BrokenPipeError, ConnectionError):
        return 0
    finally:
        s.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--socket")
    ap.add_argument("--pipe", help="host:port through the route in --route-file, for ssh's ProxyCommand")
    ap.add_argument("--log")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--allow-file", help="newline-separated allowlist, re-read when it changes")
    ap.add_argument("--never-reach", action="append", default=[], help="an address or network (CIDR) to refuse")
    ap.add_argument("--never-reach-file", help="newline-separated --never-reach entries, re-read when it changes")
    ap.add_argument("--port", action="append", type=int, default=[])
    ap.add_argument("--route-file", help="the route as JSON, re-read when it changes; missing or bad lets nothing through")
    ap.add_argument("--isolate", default="", help="a name that keeps this agent on its own Tor circuits")
    ap.add_argument("--tokens-file", help="stand-ins and the real tokens for them, as JSON, re-read when it changes")
    ap.add_argument("--branches-file", help="the branches this agent created, one JSON line each, kept for its next start")
    a = ap.parse_args(argv)
    if a.pipe:
        return pipe(a.pipe, a.route_file, a.isolate)
    if not a.socket:
        ap.error("--socket is needed")
    proxy = Proxy(a.allow, set(a.port or [443]), a.log, allow_file=a.allow_file,
                  never_reach=a.never_reach, never_file=a.never_reach_file, route_file=a.route_file, isolate=a.isolate,
                  tokens_file=a.tokens_file, branches_file=a.branches_file)
    proxy.maybe_reload()
    serve(a.socket, proxy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
