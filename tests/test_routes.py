"""Routes: the egress proxy sends checked connections on through SOCKS5, an HTTP proxy or Tor, and
refuses them (the kill switch) whenever the route can't take them."""

import base64
import ipaddress
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

from cilly import egress_proxy as E
from cilly.egress_proxy import Proxy, dns_answers, dns_query, parse_route
from tests.test_proxy import fake_proc, fake_resolver


def _recv(c, n):
    out = b""
    while len(out) < n:
        chunk = c.recv(n - len(out))
        if not chunk:
            raise OSError("closed")
        out += chunk
    return out


class FakeSocks:
    """A SOCKS5 proxy that also answers Tor's RESOLVE, and records what it was asked."""

    def __init__(self, login=None, resolve=None, target=None, fail=None):
        self.login, self.resolve, self.target = login, resolve or {}, target
        self.fail = list(fail or [])  # reply codes for the first requests (None answers as usual), like a failing Tor exit
        self.seen, self.logins = [], []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        try:
            ver, n = _recv(c, 2)
            methods = _recv(c, n)
            want = 2 if self.login else 0
            if want not in methods:
                c.sendall(b"\x05\xff")
                return
            c.sendall(bytes([5, want]))
            if want:
                _recv(c, 1)
                u = _recv(c, _recv(c, 1)[0]).decode()
                p = _recv(c, _recv(c, 1)[0]).decode()
                self.logins.append((u, p))
                ok = self.login is True or (u, p) == self.login
                c.sendall(b"\x01\x00" if ok else b"\x01\x01")
                if not ok:
                    return
            _v, cmd, _r, atyp = _recv(c, 4)
            if atyp == 1:
                addr = str(ipaddress.IPv4Address(_recv(c, 4)))
            elif atyp == 4:
                addr = str(ipaddress.IPv6Address(_recv(c, 16)))
            else:
                addr = _recv(c, _recv(c, 1)[0]).decode()
            port = int.from_bytes(_recv(c, 2), "big")
            self.seen.append((cmd, addr, port))
            rep = self.fail.pop(0) if self.fail else None
            if rep:
                c.sendall(bytes([5, rep, 0, 1]) + bytes(6))
                return
            if cmd == 0xF0:
                ip = self.resolve.get(addr)
                if not ip:
                    c.sendall(b"\x05\x04\x00\x01" + bytes(6))
                    return
                c.sendall(b"\x05\x00\x00\x01" + ipaddress.IPv4Address(ip).packed + b"\x00\x00")
                return
            port_to = self.target.get(addr) if isinstance(self.target, dict) else self.target
            up = socket.create_connection(("127.0.0.1", port_to))
            c.sendall(b"\x05\x00\x00\x01" + bytes(6))
            E.pump(c, up, idle=5)
            up.close()
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.srv.close()


def dns_answer(query: bytes, table: dict) -> bytes:
    """An answer to `query` from `table` (name -> addresses), with the name as a compression pointer."""
    i, labels = 12, []
    while query[i]:
        labels.append(query[i + 1:i + 1 + query[i]].decode())
        i += query[i] + 1
    qtype = struct.unpack(">H", query[i + 1:i + 3])[0]
    question = query[12:i + 5]
    ips = [ipaddress.ip_address(a) for a in table.get(".".join(labels), [])]
    ans = [ip for ip in ips if (ip.version == 4) == (qtype == 1)]
    out = struct.pack(">HHHHHH", 0, 0x8180, 1, len(ans), 0, 0) + question
    for ip in ans:
        out += b"\xc0\x0c" + struct.pack(">HHIH", qtype, 1, 120, len(ip.packed)) + ip.packed
    return out


class FakeDnsTcp:
    """A WireGuard VPN's own resolver: plain DNS over TCP."""

    def __init__(self, table):
        self.table, self.asked = table, []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            q = _recv(c, struct.unpack(">H", _recv(c, 2))[0])
            self.asked.append(q)
            a = dns_answer(q, self.table)
            c.sendall(struct.pack(">H", len(a)) + a)
            c.close()

    def close(self):
        self.srv.close()


class FakeDoh:
    """A DNS-over-HTTPS resolver (TLS left out: the tests swap it for the plain socket)."""

    def __init__(self, table):
        self.table, self.paths = table, []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                head += c.recv(1024)
            line = head.split(b"\r\n", 1)[0].decode()
            self.paths.append(head.decode())
            q = line.split("?dns=", 1)[1].split(" ", 1)[0]
            body = dns_answer(base64.urlsafe_b64decode(q + "=" * (-len(q) % 4)), self.table)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            c.close()

    def close(self):
        self.srv.close()


class FakeHttpProxy:
    def __init__(self, target):
        self.target, self.heads = target, []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                head += c.recv(1)
            self.heads.append(head.decode())
            up = socket.create_connection(("127.0.0.1", self.target))
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            up.sendall(c.recv(100))
            c.sendall(up.recv(100))
            up.close()
            c.close()

    def close(self):
        self.srv.close()


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = fake_proc(self.tmp.name)  # its own: test_proxy's goes away when that module is done
        self.echo = socket.socket()
        self.echo.bind(("127.0.0.1", 0))
        self.echo.listen()
        self.addCleanup(self.echo.close)
        threading.Thread(target=self._echo, daemon=True).start()
        self.direct = []  # connections that went straight out: a route must never cause one

    def _echo(self):
        while True:
            try:
                c, _ = self.echo.accept()
            except OSError:
                return
            c.sendall(b"echo:" + c.recv(100))
            c.close()

    def proxy(self, route=None, route_file=None, resolver=None, isolate="a1b2c3d4", allow=None, never=()):
        def connector(family, sa):
            self.direct.append(sa)
            return socket.create_connection(("127.0.0.1", self.echo.getsockname()[1]))
        log = os.path.join(self.tmp.name, "egress.log")
        p = Proxy(allow or ["api.github.com", "*.example.org"], {443}, log, never_reach=list(never),
                  resolver=resolver or fake_resolver({"api.github.com": ["140.82.112.5"], "lan.example.org": ["10.0.0.5"]}),
                  connector=connector, proc_net=self.proc, route=route, route_file=route_file, isolate=isolate)
        self.addCleanup(p._log.close)
        self.log = log
        return p

    def request(self, proxy, host="api.github.com", payload=b"ping"):
        a, b = socket.socketpair()
        a.settimeout(10)  # a proxy that never answers fails the test instead of hanging it
        t = threading.Thread(target=proxy.handle, args=(b,))
        t.start()
        a.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
        resp = a.recv(4096)
        if resp.startswith(b"HTTP/1.1 200"):
            a.sendall(payload)
            resp += a.recv(4096)
        a.close()
        t.join(10)
        b.close()
        return resp

    def lines(self):
        with open(self.log) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_socks5_carries_the_checked_address_never_the_name(self):
        s = FakeSocks(target=self.echo.getsockname()[1])
        self.addCleanup(s.close)
        r = self.request(self.proxy({"kind": "socks5", "name": "VPN", "host": "127.0.0.1", "port": s.port}))
        self.assertIn(b"echo:ping", r)
        self.assertEqual(s.seen, [(1, "140.82.112.5", 443)])  # the IP we checked, so no second lookup
        self.assertEqual(self.direct, [])
        allow = [ln for ln in self.lines() if ln["action"] == "allow"][0]
        self.assertEqual(allow["via"], "VPN")

    def test_a_login_is_sent_and_a_wrong_one_refuses(self):
        s = FakeSocks(login=("me", "secret"), target=self.echo.getsockname()[1])
        self.addCleanup(s.close)
        ok = self.request(self.proxy({"kind": "socks5", "host": "127.0.0.1", "port": s.port, "user": "me", "password": "secret"}))
        self.assertIn(b"echo:ping", ok)
        bad = self.request(self.proxy({"kind": "socks5", "host": "127.0.0.1", "port": s.port, "user": "me", "password": "nope"}))
        self.assertTrue(bad.startswith(b"HTTP/1.1 502"), bad)
        none = self.request(self.proxy({"kind": "socks5", "host": "127.0.0.1", "port": s.port}))
        self.assertTrue(none.startswith(b"HTTP/1.1 502"), none)
        self.assertEqual(self.direct, [])
        errs = [ln for ln in self.lines() if ln["action"] == "error"]
        self.assertTrue(errs and all(e["code"] == "route_down" for e in errs))
        with open(self.log) as f:
            self.assertNotIn("secret", f.read())  # the login is never logged

    def test_kill_switch_when_the_route_is_down(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()  # nothing listens there now
        for kind in ("socks5", "http", "tor"):
            r = self.request(self.proxy({"kind": kind, "host": "127.0.0.1", "port": port}))
            self.assertTrue(r.startswith(b"HTTP/1.1 502") or r.startswith(b"HTTP/1.1 403"), (kind, r))
        self.assertEqual(self.direct, [])  # never straight out instead

    def test_the_checks_run_first(self):
        s = FakeSocks(target=self.echo.getsockname()[1])
        self.addCleanup(s.close)
        p = self.proxy({"kind": "socks5", "host": "127.0.0.1", "port": s.port})
        self.assertIn(b"403", self.request(p, host="evil.com"))  # not in the allowlist
        self.assertIn(b"403", self.request(p, host="lan.example.org"))  # a private address
        self.assertEqual(s.seen, [])  # the route never heard of either

    def test_tor_looks_names_up_at_the_exit_and_keeps_agents_apart(self):
        def no_local_lookup(*a, **k):
            raise AssertionError("looked up on this PC")
        s = FakeSocks(login=True, resolve={"api.github.com": "140.82.112.5", "lan.example.org": "10.0.0.5"},
                      target=self.echo.getsockname()[1])
        self.addCleanup(s.close)
        p = self.proxy({"kind": "tor", "host": "127.0.0.1", "port": s.port}, resolver=no_local_lookup, isolate="a1b2c3d4")
        self.assertIn(b"echo:ping", self.request(p))
        self.assertEqual(s.seen, [(0xF0, "api.github.com", 0), (1, "140.82.112.5", 443)])
        self.assertEqual({u for u, _ in s.logins}, {"a1b2c3d4"})  # this agent's own circuits
        self.assertIn(b"403", self.request(p, host="lan.example.org"))  # an exit that answers with a private address
        self.assertEqual(s.seen[-1], (0xF0, "lan.example.org", 0))  # asked, refused, never connected
        self.assertEqual(self.direct, [])

    def tor_proxy(self, fail):
        s = FakeSocks(login=True, resolve={"api.github.com": "140.82.112.5"}, target=self.echo.getsockname()[1], fail=fail)
        self.addCleanup(s.close)
        return s, self.proxy({"kind": "tor", "host": "127.0.0.1", "port": s.port}, isolate="a1b2c3d4")

    def test_a_failed_tor_exit_gets_one_new_circuit(self):
        """A Tor exit that can't look a name up or reach the address is one relay's trouble: once
        more on a new circuit, which the agent then keeps."""
        for fail in ([4], [None, 4], [6]):  # the lookup failed, the connection failed, it timed out
            s, p = self.tor_proxy(fail)
            self.assertIn(b"echo:ping", self.request(p), fail)
            self.assertEqual(s.logins[0], ("a1b2c3d4", "cilly"))
            self.assertEqual({pw for _, pw in s.logins[len(fail):]}, {"cilly-1"}, fail)  # a new circuit
            self.assertIn(b"echo:ping", self.request(p))
            self.assertEqual(s.logins[-1], ("a1b2c3d4", "cilly-1"))  # and it stays on it
        self.assertEqual(self.direct, [])
        self.assertTrue(any(ln["action"] == "new_circuit" for ln in self.lines()))

    def test_one_new_circuit_at_most_and_never_straight_out(self):
        s, p = self.tor_proxy([4, 4])
        r = self.request(p)
        self.assertTrue(r.startswith(b"HTTP/1.1 502"), r)  # the route failed, not the allowlist, so not 403
        self.assertEqual(len(s.seen), 2)
        s, p = self.tor_proxy([None, 5])  # refused by the address itself: another exit wouldn't help
        self.assertTrue(self.request(p).startswith(b"HTTP/1.1 502"))
        self.assertEqual(len(s.seen), 2)
        v = FakeSocks(target=self.echo.getsockname()[1], fail=[4])  # a VPN's SOCKS5 has no circuits to change
        self.addCleanup(v.close)
        self.assertTrue(self.request(self.proxy({"kind": "socks5", "host": "127.0.0.1", "port": v.port})).startswith(b"HTTP/1.1 502"))
        self.assertEqual(len(v.seen), 1)
        self.assertEqual(self.direct, [])

    def vpn(self, table, private=None):
        """A WireGuard route as cilly hands it to the proxy: wireproxy's SOCKS5, the VPN's resolver."""
        dns = FakeDnsTcp(table)
        self.addCleanup(dns.close)
        s = FakeSocks(target={"10.8.0.1": dns.port, **{ip: self.echo.getsockname()[1] for ips in table.values() for ip in ips}})
        self.addCleanup(s.close)
        route = {"kind": "socks5", "name": "office", "host": "127.0.0.1", "port": s.port, "dns": "10.8.0.1"}
        if private is not None:
            route["private"] = private
        return s, dns, route

    def test_a_vpn_looks_names_up_at_its_own_resolver(self):
        def no_local_lookup(*a, **k):
            raise AssertionError("looked up on this PC")
        s, dns, route = self.vpn({"api.github.com": ["140.82.112.5"]})
        p = self.proxy(route, resolver=no_local_lookup)
        self.assertIn(b"echo:ping", self.request(p))
        self.assertEqual(s.seen, [(1, "10.8.0.1", 53), (1, "140.82.112.5", 443)])  # its resolver, through the tunnel
        self.assertEqual(len(dns.asked), 1)

    def test_private_hosts_only_when_listed_named_and_through_their_route(self):
        table = {"git.example.org": ["10.8.0.20"], "lan.example.org": ["10.0.0.5"], "loop.example.org": ["127.0.0.1"],
                 "api.github.com": ["140.82.112.5"]}
        s, _, route = self.vpn(table, private=["git.example.org", "loop.example.org"])
        p = self.proxy(route)
        self.assertIn(b"echo:ping", self.request(p, host="git.example.org"))  # listed by name
        self.assertIn((1, "10.8.0.20", 443), s.seen)
        self.assertIn(b"403", self.request(p, host="lan.example.org"))  # private, not listed
        self.assertIn(b"403", self.request(p, host="loop.example.org"))  # listed, but never loopback
        self.assertFalse({(1, "10.0.0.5", 443), (1, "127.0.0.1", 443)} & set(s.seen))  # refused before the tunnel
        self.assertIn(b"echo:ping", self.request(self.proxy(dict(route, private=["10.0.0.5"])), host="lan.example.org"))  # by address
        self.assertIn(b"403", self.request(self.proxy(route, never=["10.8.0.20"]), host="git.example.org"))  # Never reach wins
        self.assertIn(b"403", self.request(self.proxy(route, allow=["*"]), host="git.example.org"))  # not the open web
        self.assertIn(b"403", self.request(self.proxy(dict(route, private=None)), host="git.example.org"))  # not other routes
        self.assertEqual(self.direct, [])

    def test_a_route_on_this_pcs_loopback_must_be_yours(self):
        """Another user could take wireproxy's port while it restarts, and get the agents' traffic."""
        self.assertEqual(E._proc_tcp_addr("127.0.0.1", 1080), "0100007F:0438")
        self.assertEqual(E._proc_tcp_addr("::1", 1080), "00000000000000000000000001000000:0438")
        for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                srv = socket.socket(fam)
                srv.bind((host, 0))
            except OSError:
                continue  # no IPv6 here
            srv.listen()
            c = socket.create_connection((host, srv.getsockname()[1]))
            self.assertEqual(E.loopback_owner(c), os.getuid(), host)  # found in /proc/net/tcp, and it's ours
            c.close()
            srv.close()
        s = FakeSocks(target=self.echo.getsockname()[1])
        self.addCleanup(s.close)
        route = {"kind": "socks5", "host": "127.0.0.1", "port": s.port}
        self.assertIn(b"echo:ping", self.request(self.proxy(route)))
        for owner in (os.getuid() + 1, None):  # someone else's, or nobody can tell
            with mock.patch.object(E, "loopback_owner", lambda sock, owner=owner: owner):
                self.assertTrue(self.request(self.proxy(route)).startswith(b"HTTP/1.1 502"), owner)
        self.assertEqual(self.direct, [])

    def test_what_a_private_list_takes(self):
        ok = E.parse_private(["Git.Example.org.", "10.8.0.20", "fd00::5", "100.64.1.2"])
        self.assertEqual(ok, {"names": ["git.example.org"], "ips": ["10.8.0.20", "fd00::5", "100.64.1.2"]})
        for bad in (["*.example.org"], ["8.8.8.8"], ["127.0.0.1"], ["169.254.1.1"], ["::1"], ["a b"], ["x"] * 51, "git.example.org",
                    ["fd00::5%eth0"], ["fd00::5%x\ny"]):
            with self.assertRaises(ValueError, msg=bad):
                E.parse_private(bad)
        with self.assertRaises(ValueError):
            E.parse_route({"kind": "tor", "unix": "/run/t.sock", "private": ["git.example.org"]})
        with self.assertRaises(ValueError):
            E.parse_route({"kind": "http", "host": "127.0.0.1", "port": 1, "dns": "10.8.0.1"})
        with self.assertRaises(ValueError):
            E.parse_route({"kind": "socks5", "host": "127.0.0.1", "port": 1, "dns": "not-an-ip"})
        with self.assertRaises(ValueError):
            E.parse_route({"kind": "socks5", "host": "127.0.0.1", "port": 1, "dns": "fd00::1%eth0"})

    def test_names_over_dns_over_https_through_the_route(self):
        def no_local_lookup(*a, **k):
            raise AssertionError("looked up on this PC")
        doh = FakeDoh({"api.github.com": ["140.82.112.5", "2606:50c0:8000::154"], "lan.example.org": ["192.168.1.7"]})
        self.addCleanup(doh.close)
        s = FakeSocks(target={"9.9.9.9": doh.port, "140.82.112.5": self.echo.getsockname()[1]})
        self.addCleanup(s.close)
        plain = mock.MagicMock()
        plain.return_value.wrap_socket = lambda sock, server_hostname: sock  # the plain socket instead of TLS
        route = {"kind": "socks5", "host": "127.0.0.1", "port": s.port,
                 "doh": {"url": "https://dns.quad9.net/dns-query", "ip": "9.9.9.9"}}
        with mock.patch.object(E.ssl, "create_default_context", plain):
            p = self.proxy(route, resolver=no_local_lookup)
            self.assertIn(b"echo:ping", self.request(p))
            self.assertIn(b"echo:ping", self.request(p))  # the second time from the cache
            self.assertIn(b"403", self.request(p, host="lan.example.org"))  # a private answer is still refused
        self.assertEqual([x for x in s.seen if x[1] == "9.9.9.9"], [(1, "9.9.9.9", 443)] * 2)  # the resolver, by IP, through the route
        self.assertEqual(s.seen.count((1, "140.82.112.5", 443)), 2)  # IPv4 first, the address we checked
        self.assertTrue(doh.paths[0].startswith("GET /dns-query?dns="))
        self.assertIn("Host: dns.quad9.net\r\n", doh.paths[0])
        self.assertIn("User-Agent: Mozilla/5.0\r\n", doh.paths[0])  # nothing that says cilly
        self.assertEqual(self.direct, [])

    def test_dns_messages(self):
        q = dns_query("api.github.com", 28)
        self.assertEqual(q[12:28], b"\x03api\x06github\x03com\x00")
        ans = dns_answer(q, {"api.github.com": ["2606:50c0:8000::154", "140.82.112.5"]})
        self.assertEqual(dns_answers(ans, 28), (["2606:50c0:8000::154"], 120))
        self.assertEqual(dns_answers(dns_answer(dns_query("x.org", 1), {}), 1), ([], 300))
        for bad in (b"", b"\x00" * 12, ans[:20], struct.pack(">HHHHHH", 0, 0x8182, 1, 0, 0, 0)):  # cut short, or SERVFAIL
            with self.assertRaises(ValueError):
                dns_answers(bad, 1)

    def test_http_proxy(self):
        h = FakeHttpProxy(self.echo.getsockname()[1])
        self.addCleanup(h.close)
        r = self.request(self.proxy({"kind": "http", "host": "127.0.0.1", "port": h.port, "user": "me", "password": "pw"}))
        self.assertIn(b"echo:ping", r)
        self.assertTrue(h.heads[0].startswith("CONNECT 140.82.112.5:443 HTTP/1.1\r\n"))
        self.assertIn("Proxy-Authorization: Basic bWU6cHc=\r\n", h.heads[0])
        self.assertEqual(self.direct, [])

    def test_the_route_file_is_a_kill_switch_too(self):
        f = os.path.join(self.tmp.name, "route.json")
        p = self.proxy(route_file=f)
        self.assertIn(b"403", self.request(p))  # not written yet: nothing goes out
        with open(f, "w") as fh:
            json.dump({"kind": "direct"}, fh)
        self.assertIn(b"echo:ping", self.request(p))
        s = FakeSocks(target=self.echo.getsockname()[1])
        self.addCleanup(s.close)
        with open(f, "w") as fh:
            json.dump({"kind": "socks5", "host": "127.0.0.1", "port": s.port}, fh)
        n = len(self.direct)
        self.assertIn(b"echo:ping", self.request(p))  # switched while running
        self.assertEqual((len(self.direct), len(s.seen)), (n, 1))
        with open(f, "w") as fh:
            fh.write("{broken")
        self.assertIn(b"403", self.request(p))  # a bad file lets nothing through, not the last good one
        self.assertEqual(len(self.direct), n)

    def test_what_a_route_may_say(self):
        self.assertEqual(parse_route({"kind": "direct", "host": "x"}), {"kind": "direct"})
        self.assertEqual(parse_route({"kind": "tor", "unix": "/run/user/1000/cilly/tor/socks.sock"})["unix"],
                         "/run/user/1000/cilly/tor/socks.sock")
        self.assertEqual(parse_route({"kind": "socks5", "host": "[::1]", "port": 1080})["host"], "::1")
        for bad in ({"kind": "ftp"}, {"kind": "socks5", "host": "a b", "port": 1}, {"kind": "socks5", "host": "x.org", "port": 0},
                    {"kind": "socks5", "host": "x.org", "port": "1080"}, {"kind": "socks5", "unix": "relative.sock"},
                    {"kind": "socks5", "host": "x.org", "port": 1, "user": "me"},
                    {"kind": "http", "host": "x.org", "port": 1, "user": "me", "password": "a\r\nX-Evil: 1"},
                    {"kind": "socks5", "host": "x.org", "port": 1, "user": "u" * 256, "password": "p"}, [], "direct",
                    {"kind": "tor", "unix": "/x.sock", "doh": {"url": "https://dns.quad9.net/dns-query", "ip": "9.9.9.9"}},
                    {"kind": "socks5", "host": "x.org", "port": 1, "doh": {"url": "http://dns.quad9.net/dns-query", "ip": "9.9.9.9"}},
                    {"kind": "socks5", "host": "x.org", "port": 1, "doh": {"url": "https://dns.quad9.net/dns-query", "ip": "10.0.0.1"}},
                    {"kind": "socks5", "host": "x.org", "port": 1, "doh": {"url": "https://u:p@dns.quad9.net/", "ip": "9.9.9.9"}},
                    {"kind": "socks5", "host": "x.org", "port": 1, "doh": {"url": "https://dns.quad9.net/q?x=1", "ip": "9.9.9.9"}}):
            with self.assertRaises(ValueError, msg=bad):
                parse_route(bad)


class OwnDownloadTests(unittest.TestCase):
    """cilly's own downloads (GitHub API, Debian image) through the route: the name goes to the far
    end, TLS is checked against it, plain HTTP is refused, and a route that is down fails."""

    def setUp(self):
        self.web = socket.socket()
        self.web.bind(("127.0.0.1", 0))
        self.web.listen()
        self.addCleanup(self.web.close)
        self.heads = []
        threading.Thread(target=self._serve, daemon=True).start()
        from cilly import routed
        self.routed = routed
        p = mock.patch.object(routed.ssl, "create_default_context", lambda: _PlainContext())  # TLS swapped for the plain socket
        p.start()
        self.addCleanup(p.stop)

    def _serve(self):
        while True:
            try:
                c, _ = self.web.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                head += c.recv(1024)
            self.heads.append(head.decode())
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            c.close()

    def test_through_socks_by_name(self):
        s = FakeSocks(target={"api.github.com": self.web.getsockname()[1]})
        self.addCleanup(s.close)
        op = self.routed.opener({"kind": "socks5", "host": "127.0.0.1", "port": s.port})
        import urllib.request
        r = op.open(urllib.request.Request("https://api.github.com/user", headers={"Authorization": "Bearer t"}), timeout=5)
        self.assertEqual(r.read(), b"ok")
        self.assertEqual(s.seen, [(1, "api.github.com", 443)])  # the name, looked up at the far end
        self.assertIn("Authorization: Bearer t", self.heads[0])

    def test_tor_keeps_cillys_own_circuits(self):
        s = FakeSocks(login=True, target={"deb.debian.org": self.web.getsockname()[1]})
        self.addCleanup(s.close)
        import urllib.request
        self.routed.opener({"kind": "tor", "host": "127.0.0.1", "port": s.port}).open("https://deb.debian.org/x", timeout=5).read()
        self.assertEqual(s.logins, [("cilly", "cilly")])

    def test_plain_http_and_a_dead_route_fail(self):
        import urllib.error
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        op = self.routed.opener({"kind": "socks5", "host": "127.0.0.1", "port": port})
        with self.assertRaises(urllib.error.URLError):
            op.open("http://deb.debian.org/x", timeout=5)  # never in the clear
        with self.assertRaises((urllib.error.URLError, OSError)):
            op.open("https://deb.debian.org/x", timeout=5)  # the route is down: no download, not straight out
        self.assertEqual(self.heads, [])

    def test_doh_choice_in_settings(self):
        from cilly import config as C
        cfg = C._merge(C.DEFAULT_CONFIG, {})
        self.assertIsNone(C.doh_resolver(cfg))  # this PC's DNS unless you pick one
        cfg["egress"]["doh"] = "mullvad"
        C.validate(cfg)
        self.assertEqual(C.doh_resolver(cfg), {"url": "https://dns.mullvad.net/dns-query", "ip": "194.242.2.2"})
        cfg["egress"]["doh"] = {"url": "https://doh.example.org/dns-query", "ip": "185.222.222.222"}
        C.validate(cfg)
        for bad in ("google", {"url": "https://doh.example.org/dns-query", "ip": "192.168.1.1"},
                    {"url": "http://doh.example.org/", "ip": "185.222.222.222"}, {"url": "https://x.org/", "ip": "185.222.222.222", "extra": 1}):
            cfg["egress"]["doh"] = bad
            with self.assertRaises(ValueError, msg=bad):
                C.validate(cfg)
        for name, r in C.DOH_RESOLVERS.items():  # every one in the list passes the proxy's own check
            E.parse_doh(r)


class _PlainContext:
    def wrap_socket(self, sock, server_hostname=None):
        return sock


if __name__ == "__main__":
    unittest.main()
