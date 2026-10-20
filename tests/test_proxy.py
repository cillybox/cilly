import ipaddress
import json
import os
import socket
import tempfile
import threading
import unittest

from cilly.egress_proxy import OPEN_WEB, Proxy, Refused, pump, parse_never_reach, normalize_allowlist, parse_connect, resolve_global, site_allowed


def fake_resolver(table):
    def r(host, port, type=None, proto=None):
        if host not in table:
            raise socket.gaierror("nx")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port) if ":" not in ip else (ip, port, 0, 0)) for ip in table[host]]
    return r


def fake_proc(d, v4_routes=(), v4_own=(), v6_routes=(), v6_own=(), ipv6=True, trie_extra=()):
    """/proc/net files for a PC with default routes and the given extra routes and own addresses."""
    le = lambda a: ipaddress.IPv4Address(a).packed[::-1].hex().upper()  # noqa: E731
    route = ["Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT    ",
             f"eth0\t00000000\t{le('192.168.1.1')}\t0003\t0\t0\t100\t00000000\t0\t0\t0"]
    for net, dev in v4_routes:
        n = ipaddress.IPv4Network(net)
        route.append(f"{dev}\t{le(n.network_address)}\t00000000\t0001\t0\t0\t100\t{le(n.netmask)}\t0\t0\t0")
    trie = ["Main:", "  +-- 0.0.0.0/0 3 0 5", "     |-- 0.0.0.0", "        /0 universe UNICAST"]
    for net, _dev in v4_routes:
        n = ipaddress.IPv4Network(net)
        trie += [f"     |-- {n.network_address}", f"        /{n.prefixlen} link UNICAST"]
    trie += ["Local:", "  +-- 127.0.0.0/8 2 0 2", "     |-- 127.0.0.1", "        /32 host LOCAL"]
    for a in v4_own:
        trie += [f"     |-- {a}", "        /32 host LOCAL"]
    trie += list(trie_extra)
    zero = "0" * 32
    v6 = [f"{zero} 00 {zero} 00 fe800000000000000000000000000001 00000400 00000003 00000000 00000003     eth0",
          f"{zero} 00 {zero} 00 {zero} ffffffff 00000001 00000000 00200200       lo"]  # default, and the kernel's reject route
    for net, dev in v6_routes:
        n = ipaddress.IPv6Network(net)
        v6.append(f"{n.network_address.packed.hex()} {n.prefixlen:02x} {zero} 00 {zero} 00000100 00000001 00000000 00000001 {dev:>8}")
    inet6 = ["00000000000000000000000000000001 01 80 10 80       lo"]
    for a in v6_own:
        inet6.append(f"{ipaddress.IPv6Address(a).packed.hex()} 02 40 00 00     eth0")
    files = {"route": route, "fib_trie": trie}
    if ipv6:
        files.update(ipv6_route=v6, if_inet6=inet6)
    paths = {}
    for k in ("ipv6_route", "if_inet6", "fib_trie"):
        paths[k] = os.path.join(d, k)
        if k in files:
            with open(paths[k], "w") as f:
                f.write("\n".join(files[k]) + "\n")
    return paths


def home_proc(d, **kw):
    """A PC on a public IPv4 subnet and a global IPv6 /64, with a VPN, and a public address on ppp0."""
    return fake_proc(d, v4_routes=[("81.2.69.0/24", "eth0"), ("93.184.0.0/16", "wg0")], v4_own=["81.2.69.10", "85.1.2.3"],
                     v6_routes=[("2a02:8070:1234:5600::/64", "eth0"), ("2001:67c:1234::/48", "wg0")],
                     v6_own=["2a02:8070:1234:5600::10"], **kw)


_PROC = tempfile.TemporaryDirectory()
DEFAULT_ONLY = fake_proc(_PROC.name)  # the tests below don't depend on this machine's routes


def tearDownModule():
    _PROC.cleanup()


def host_allowed(host, allow, port=443, ports=(443,)):
    try:
        return site_allowed(host, port, allow, ports)
    except Refused:
        return None


class AllowlistTests(unittest.TestCase):
    def test_exact_and_wildcard(self):
        allow = normalize_allowlist(["github.com", "*.githubusercontent.com"])
        self.assertTrue(host_allowed("github.com", allow))
        self.assertTrue(host_allowed("GitHub.com.", allow))
        self.assertFalse(host_allowed("evil-github.com", allow))
        self.assertFalse(host_allowed("api.github.com", allow))
        self.assertTrue(host_allowed("objects.githubusercontent.com", allow))
        self.assertFalse(host_allowed("githubusercontent.com", allow))
        self.assertFalse(host_allowed("xgithubusercontent.com", allow))

    def test_names_win_over_the_open_web(self):
        allow = normalize_allowlist(["*", "crates.io", "*.example.com"], allow_open=True)
        self.assertEqual(host_allowed("crates.io", allow), "crates.io")
        self.assertEqual(host_allowed("a.example.com", allow), "*.example.com")
        self.assertEqual(host_allowed("other.org", allow), OPEN_WEB)
        self.assertIsNone(host_allowed("other.org", ["crates.io"]))

    def test_bad_entries(self):
        for bad in ["*", "*.com/x", "http://x", "a b", "-x.com", "x.com:0", "x.com:65536", "x.com:123456", "x.com:http",
                    "x.com:-1", "*:8080", "x.com:443:443", "[::1]:443", "x.com:\u00b2", "x.com:\uff18\uff10", "x.com:"]:
            with self.assertRaises(ValueError, msg=bad):
                normalize_allowlist([bad])

    def test_a_site_can_carry_its_port(self):
        self.assertEqual(normalize_allowlist(["Sync.Example.net:8080", "*.example.com:22", "x.org.:08443", "y.org:65535"]),
                         ["sync.example.net:8080", "*.example.com:22", "x.org:8443", "y.org:65535"])
        allow = normalize_allowlist(["sync.example.net:8080", "*.example.com:22", "plain.org"])
        ports = (443,)
        self.assertEqual(host_allowed("sync.example.net", allow, 8080, ports), "sync.example.net:8080")
        self.assertIsNone(host_allowed("sync.example.net", allow, 443, ports))  # it names a port, so only that one
        self.assertIsNone(host_allowed("sync.example.net", allow, 8081, ports))
        self.assertEqual(host_allowed("a.example.com", allow, 22, ports), "*.example.com:22")
        self.assertIsNone(host_allowed("example.com", allow, 22, ports))
        self.assertEqual(host_allowed("plain.org", allow, 443, ports), "plain.org")
        self.assertIsNone(host_allowed("plain.org", allow, 8080, ports))  # no port named: egress.ports
        self.assertEqual(host_allowed("plain.org", allow, 8080, (443, 8080)), "plain.org")
        self.assertEqual(host_allowed("both.org", ["both.org", "both.org:8080"], 443, ports), "both.org")
        self.assertEqual(host_allowed("both.org", ["both.org", "both.org:8080"], 8080, ports), "both.org:8080")

    def test_refusal_codes_for_ports(self):
        for allow, host, port, code in ((["named.org"], "named.org", 8080, "port"),
                                        (["named.org:22"], "named.org", 8080, "port"),
                                        (["named.org"], "other.org", 8080, "host"),
                                        (["*", "named.org:8080"], "other.org", 8080, "open_web_port"),
                                        (["*", "named.org"], "named.org", 8080, "port")):
            with self.assertRaises(Refused, msg=(allow, host, port)) as cm:
                site_allowed(host, port, allow, (443, 80))
            self.assertEqual(cm.exception.code, code, (allow, host, port))


class ParseTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(parse_connect(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: x"), ("api.github.com", 443))
        for ok in ("1password.com", "web3.storage", "0xproject.org", "x.y2k"):  # digits elsewhere are fine
            self.assertEqual(parse_connect(f"CONNECT {ok}:443 HTTP/1.1".encode())[0], ok)

    def test_rejects(self):
        for head in [b"GET http://github.com/ HTTP/1.1", b"CONNECT 1.2.3.4:443 HTTP/1.1",
                     b"CONNECT [::1]:443 HTTP/1.1", b"CONNECT github.com HTTP/1.1",
                     b"CONNECT github.com:0 HTTP/1.1", b"CONNECT git_hub.com:443 HTTP/1.1",
                     b"CONNECT github.com:443", "CONNECT gíthub.com:443 HTTP/1.1".encode(),
                     # shorthand IPv4 that getaddrinfo would still turn into an address
                     b"CONNECT 127.1:443 HTTP/1.1", b"CONNECT 0x7f.1:443 HTTP/1.1", b"CONNECT 2130706433:443 HTTP/1.1",
                     b"CONNECT 0x7f000001:443 HTTP/1.1", b"CONNECT 10.1.1:443 HTTP/1.1", b"CONNECT 0177.0.0.1:443 HTTP/1.1"]:
            with self.assertRaises(ValueError, msg=head):
                parse_connect(head)


class ResolveTests(unittest.TestCase):
    def test_private_addresses_refused(self):
        for ip in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fc00::1",
                   "::ffff:127.0.0.1", "100.64.0.1", "0.0.0.0", "::7f00:1", "64:ff9b::a00:1",
                   "64:ff9b::7f00:1", "fec0::1", "192.0.0.9", "192.88.99.1", "2002:7f00:1::1", "2001:0:4136::1"]:
            with self.assertRaises(PermissionError, msg=ip):
                resolve_global("x.example", 443, fake_resolver({"x.example": [ip]}))

    def test_global_ipv6_ok(self):
        fam, sa = resolve_global("x.example", 443, fake_resolver({"x.example": ["2606:4700::1"]}))
        self.assertEqual(sa[0], "2606:4700::1")

    def test_ipv4_comes_first(self):
        """The PC lists IPv6 first, but a slow IPv6 route at one provider made every download crawl.
        Each address still passes the same checks: IPv6 is used when there's no usable IPv4."""
        both = ["2a04:4e42:5c::644", "199.232.114.132"]
        fam, sa = resolve_global("x.example", 443, fake_resolver({"x.example": both}))
        self.assertEqual((fam, sa[0]), (socket.AF_INET, "199.232.114.132"))
        refuse_v4 = lambda ip: ("never_reach", "no") if ip.version == 4 else None  # noqa: E731
        fam, sa = resolve_global("x.example", 443, fake_resolver({"x.example": both}), refuse_v4)
        self.assertEqual((fam, sa[0]), (socket.AF_INET6, "2a04:4e42:5c::644"))
        with self.assertRaises(PermissionError):  # a refused IPv4 address never lets a refused IPv6 one through
            resolve_global("x.example", 443, fake_resolver({"x.example": ["fc00::1", "10.0.0.1"]}))

    def test_picks_global(self):
        fam, sa = resolve_global("x.example", 443, fake_resolver({"x.example": ["10.0.0.1", "140.82.112.3"]}))
        self.assertEqual(sa[0], "140.82.112.3")


class PumpTests(unittest.TestCase):
    def test_a_side_that_drops_keeps_the_count(self):
        """The VM closing a download half way: the egress log keeps what got through, not 0."""
        vm, vm_peer = socket.socketpair()
        site, site_peer = socket.socketpair()
        site.settimeout(2)
        got = []
        t = threading.Thread(target=lambda: got.append(pump(vm_peer, site_peer)), daemon=True)
        t.start()
        site.sendall(b"x" * 100_000)
        n = 0
        while n < 100_000:
            n += len(vm.recv(65536))
        vm.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\0\0\0\0\0\0\0")
        vm.close()  # a reset, like a VM that's killed
        for _ in range(50):  # the next write into the gone VM fails
            try:
                site.sendall(b"y" * 65536)
            except OSError:  # a full buffer (timeout) once the pump stopped reading counts too
                break
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(got), 1, "the pump raised instead of returning its counts")
        self.assertGreaterEqual(got[0][1], 100_000)
        for x in (vm_peer, site, site_peer):
            x.close()


class OpenWebPortTests(unittest.TestCase):
    def proxy(self, allow):
        names = ["site.org", "crates.io", "a.example.com"]
        return Proxy(allow, {22, 80, 443, 8080}, None, resolver=fake_resolver({n: ["140.82.112.3"] for n in names}),
                     proc_net=DEFAULT_ONLY)

    def test_open_web_only_on_443_and_80(self):
        p = self.proxy(["*"])
        for port in (443, 80):
            self.assertEqual(p.decide(f"CONNECT site.org:{port} HTTP/1.1".encode())[:2], ("site.org", port))
        for port in (22, 8080):
            with self.assertRaises(Refused) as cm:
                p.decide(f"CONNECT site.org:{port} HTTP/1.1".encode())
            self.assertEqual(cm.exception.code, "open_web_port")
            self.assertIn("reachable on 443 and 80", str(cm.exception))

    def test_named_hosts_keep_their_ports(self):
        p = self.proxy(["*", "crates.io", "*.example.com"])
        self.assertEqual(p.decide(b"CONNECT crates.io:8080 HTTP/1.1")[:2], ("crates.io", 8080))
        self.assertEqual(p.decide(b"CONNECT a.example.com:22 HTTP/1.1")[:2], ("a.example.com", 22))
        with self.assertRaises(Refused):
            p.decide(b"CONNECT site.org:22 HTTP/1.1")

    def test_the_proxy_port_list_still_applies(self):
        p = Proxy(["*"], {443}, None, resolver=fake_resolver({"site.org": ["140.82.112.3"]}), proc_net=DEFAULT_ONLY)
        with self.assertRaises(Refused) as cm:
            p.decide(b"CONNECT site.org:80 HTTP/1.1")
        self.assertEqual(cm.exception.code, "open_web_port")
        p = Proxy(["*"], {22, 443}, None, resolver=fake_resolver({"site.org": ["140.82.112.3"]}), proc_net=DEFAULT_ONLY)
        with self.assertRaises(Refused) as cm:
            p.decide(b"CONNECT site.org:22 HTTP/1.1")
        self.assertTrue(str(cm.exception).endswith("reachable on 443"), str(cm.exception))  # names what really works
        p = Proxy(["*"], {8443}, None, resolver=fake_resolver({"site.org": ["140.82.112.3"]}), proc_net=DEFAULT_ONLY)
        with self.assertRaises(Refused) as cm:
            p.decide(b"CONNECT site.org:8443 HTTP/1.1")
        self.assertIn("egress.ports has neither 443 nor 80", str(cm.exception))  # never "reachable on " and nothing


class PortPerSiteTests(unittest.TestCase):
    TABLE = {"sync.example.net": ["140.82.112.3"], "a.games.example": ["140.82.112.4"], "lan.example": ["192.168.1.5"],
             "subnet.example": ["81.2.69.20"], "home.example": ["85.10.0.7"], "site.org": ["140.82.112.5"]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def refused(self, p, target):
        with self.assertRaises(Refused, msg=target) as cm:
            p.decide(f"CONNECT {target} HTTP/1.1".encode())
        return cm.exception.code

    def test_a_named_port_works_outside_egress_ports(self):
        p = Proxy(["sync.example.net:8080", "*.games.example:7777", "site.org"], {443}, None,
                  resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY)
        self.assertEqual(p.decide(b"CONNECT sync.example.net:8080 HTTP/1.1")[:2], ("sync.example.net", 8080))
        self.assertEqual(p.decide(b"CONNECT a.games.example:7777 HTTP/1.1")[:2], ("a.games.example", 7777))
        self.assertEqual(self.refused(p, "sync.example.net:443"), "port")
        self.assertEqual(self.refused(p, "sync.example.net:8081"), "port")
        self.assertEqual(self.refused(p, "site.org:8080"), "port")
        self.assertEqual(self.refused(p, "other.example:8080"), "host")
        p.decide(b"CONNECT site.org:443 HTTP/1.1")

    def test_the_open_web_keeps_its_ports(self):
        p = Proxy(["*", "sync.example.net:8080"], {443, 80}, None, resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY)
        p.decide(b"CONNECT sync.example.net:8080 HTTP/1.1")
        self.assertEqual(self.refused(p, "site.org:8080"), "open_web_port")
        p.decide(b"CONNECT site.org:80 HTTP/1.1")
        p = Proxy(["*"], {443}, None, resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY)
        self.assertEqual(self.refused(p, "site.org:80"), "open_web_port")  # 80 only when it's in egress.ports

    def test_address_rules_apply_whatever_the_port(self):
        allow = ["lan.example:8080", "subnet.example:8080", "home.example:8080", "sync.example.net:22"]
        p = Proxy(allow, {443}, None, resolver=fake_resolver(self.TABLE), proc_net=home_proc(self.tmp.name),
                  never_reach=["85.10.0.0/24"])
        self.assertEqual(self.refused(p, "lan.example:8080"), "address")
        self.assertEqual(self.refused(p, "subnet.example:8080"), "local_network")
        self.assertEqual(self.refused(p, "home.example:8080"), "never_reach")
        p.decide(b"CONNECT sync.example.net:22 HTTP/1.1")

    def test_a_port_in_the_allowlist_file_applies_live(self):
        path = os.path.join(self.tmp.name, "allow.txt")
        with open(path, "w") as f:
            f.write("site.org\n")
        p = Proxy([], {443}, None, resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY, allow_file=path)
        self.assertEqual(self.refused(p, "sync.example.net:8080"), "host")
        with open(path, "w") as f:
            f.write("site.org\nsync.example.net:8080\n")
        p.decide(b"CONNECT sync.example.net:8080 HTTP/1.1")
        with open(path, "w") as f:
            f.write("sync.example.net:99999\n")  # a bad file keeps the previous list
        p.decide(b"CONNECT sync.example.net:8080 HTTP/1.1")


class LocalNetTests(unittest.TestCase):
    """Nothing but the default route: the local network, VPNs and this PC's own addresses are refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def proxy(self, table, paths=None):
        return Proxy(["*"], {443}, None, resolver=fake_resolver(table), proc_net=paths or home_proc(self.tmp.name))

    def check(self, ip, code, paths=None):
        with self.assertRaises(Refused, msg=ip) as cm:
            self.proxy({"x.example": [ip]}, paths).decide(b"CONNECT x.example:443 HTTP/1.1")
        self.assertEqual(cm.exception.code, code, msg=f"{ip}: {cm.exception}")

    def allowed(self, ip, paths=None):
        return self.proxy({"x.example": [ip]}, paths).decide(b"CONNECT x.example:443 HTTP/1.1")[2][1][0]

    def test_local_subnets_and_vpns_are_refused(self):
        self.check("81.2.69.20", "local_network")           # the PC's IPv4 subnet
        self.check("2a02:8070:1234:5600:abcd::5", "local_network")  # another device in the global IPv6 /64
        self.check("93.184.216.34", "local_network")         # a VPN route
        self.check("2001:67c:1234:1::1", "local_network")
        self.check("192.168.1.20", "address")                # private, as before

    def test_own_addresses_are_refused(self):
        self.check("85.1.2.3", "own_address")
        self.check("81.2.69.10", "own_address")
        self.check("2a02:8070:1234:5600::10", "own_address")

    def test_default_route_only_is_allowed(self):
        self.assertEqual(self.allowed("140.82.112.3"), "140.82.112.3")
        self.assertEqual(self.allowed("2606:4700::1"), "2606:4700::1")

    def test_ipv4_mapped_is_checked_as_ipv4(self):
        self.check("::ffff:81.2.69.20", "local_network")
        self.check("::ffff:93.184.216.34", "local_network")
        self.check("::ffff:85.1.2.3", "own_address")
        self.check("::ffff:10.0.0.1", "address")
        self.assertEqual(self.allowed("::ffff:140.82.112.3"), "::ffff:140.82.112.3")

    def test_first_address_that_passes_is_used(self):
        p = self.proxy({"x.example": ["81.2.69.20", "2a02:8070:1234:5600::5", "140.82.112.3"],
                        "y.example": ["85.1.2.3", "2a02:8070:1234:5600::5"]})
        self.assertEqual(p.decide(b"CONNECT x.example:443 HTTP/1.1")[2][1][0], "140.82.112.3")
        with self.assertRaises(Refused) as cm:
            p.decide(b"CONNECT y.example:443 HTTP/1.1")
        self.assertEqual(cm.exception.code, "own_address")

    def test_missing_or_garbled_files_refuse(self):
        for key, text in [("fib_trie", None), ("if_inet6", None), ("fib_trie", "garbage\n"),
                          ("fib_trie", "Main:\n     |-- 1.2.3.999\n        /24 link UNICAST\n"),
                          ("fib_trie", "Main:\n        /24 link UNICAST\n"), ("fib_trie", "     |-- 1.2.3.4\n        /24 link UNICAST\n"),
                          ("ipv6_route", "2a02 40\n"), ("if_inet6", "nothex 02 40 00 00 eth0\n"),
                          ("fib_trie", "Main:\n  |-- 1.2.3.4\n  something odd\n"), ("fib_trie", ""),
                          ("fib_trie", "Main:\n"), ("fib_trie", "Main:\nLocal:\n"),  # a header and nothing else
                          ("fib_trie", "Main:\n     |-- 0.0.0.0\n        /0 universe UNICAST\n")]:
            with tempfile.TemporaryDirectory() as d:
                paths = home_proc(d)
                if text is None:
                    os.unlink(paths[key])
                else:
                    with open(paths[key], "w") as f:
                        f.write(text)
                with self.assertRaises(Refused, msg=f"{key}: {text!r}") as cm:
                    self.proxy({"x.example": ["140.82.112.3"]}, paths).decide(b"CONNECT x.example:443 HTTP/1.1")
                self.assertEqual(cm.exception.code, "no_route_info")

    def test_without_ipv6_only_ipv6_is_refused(self):
        paths = home_proc(self.tmp.name, ipv6=False)
        self.check("2606:4700::1", "no_route_info", paths)
        self.assertEqual(self.allowed("140.82.112.3", paths), "140.82.112.3")

    def test_routes_are_read_for_every_connection(self):
        p = self.proxy({"x.example": ["140.82.112.3"]})
        p.decide(b"CONNECT x.example:443 HTTP/1.1")
        fake_proc(self.tmp.name, v4_routes=[("140.82.0.0/16", "tun0")])  # a VPN came up
        with self.assertRaises(Refused):
            p.decide(b"CONNECT x.example:443 HTTP/1.1")

    def test_the_64_around_a_dhcpv6_address(self):
        # a /128 from DHCPv6, no on-link route for the /64, but the home's other devices are in it
        paths = fake_proc(self.tmp.name, v6_routes=[("2a02:8070:1234:5600::10/128", "eth0")], v6_own=["2a02:8070:1234:5600::10"])
        self.check("2a02:8070:1234:5600:abcd::5", "local_network", paths)
        self.assertEqual(self.allowed("2a02:8070:1234:5601::5", paths), "2a02:8070:1234:5601::5")  # the next /64 is elsewhere

    def test_an_ipv6_route_without_a_device(self):
        paths = fake_proc(self.tmp.name)
        zero = "0" * 32
        with open(paths["ipv6_route"], "a") as f:  # a nexthop without a device prints one field less
            f.write(f"2a020000000000000000000000000000 20 {zero} 00 fe800000000000000000000000000001 00000400 00000001 00000000 00000003\n")
        self.check("2a02:0:0:1::5", "local_network", paths)
        self.assertEqual(self.allowed("2606:4700::1", paths), "2606:4700::1")  # not everything refused

    def test_every_routing_table_counts(self):
        paths = fake_proc(self.tmp.name, trie_extra=["Id 52:", "  +-- 0.0.0.0/0 2 0 2", "     |-- 93.184.0.0",
                                                     "        /16 universe UNICAST",  # Tailscale's table: a subnet route
                                                     "Local:", "  +-- 81.3.0.0/24 2 0 2", "     |-- 81.3.0.0",
                                                     "        /24 host LOCAL",  # local 81.3.0.0/24 dev lo
                                                     "     |-- 81.3.0.255", "        /32 link BROADCAST"])
        self.check("93.184.216.34", "local_network", paths)
        self.check("81.3.0.77", "own_address", paths)
        self.assertEqual(self.allowed("140.82.112.3", paths), "140.82.112.3")

    def test_a_vpn_full_tunnel_is_the_way_out_not_the_local_network(self):
        paths = fake_proc(self.tmp.name, v4_routes=[("0.0.0.0/1", "tun0"), ("128.0.0.0/1", "tun0"), ("10.8.0.0/24", "tun0"),
                                                    ("81.2.69.0/24", "eth0"), ("88.0.0.0/5", "wg0")],
                          v6_routes=[("::/3", "tun0"), ("2000::/4", "tun0"), ("3000::/4", "tun0"), ("2000::/3", "teredo"),
                                     ("2a02:8070:1234:5600::/64", "eth0")])
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log")
            p = Proxy(["*"], {443}, log, resolver=fake_resolver({"x.example": ["140.82.112.3"], "y.example": ["2606:4700::1"],
                                                                 "lan.example": ["81.2.69.20"], "vpn.example": ["90.1.2.3"]}),
                      proc_net=paths)
            for _ in range(3):
                p.decide(b"CONNECT x.example:443 HTTP/1.1")  # OpenVPN's 0.0.0.0/1 and 128.0.0.0/1 lead out
                p.decide(b"CONNECT y.example:443 HTTP/1.1")  # and so do ::/3, 2000::/4, 3000::/4 and Miredo's 2000::/3
            with self.assertRaises(Refused):
                p.decide(b"CONNECT lan.example:443 HTTP/1.1")  # the local network still doesn't
            with self.assertRaises(Refused):
                p.decide(b"CONNECT vpn.example:443 HTTP/1.1")  # nor a public /5 behind a VPN
            p._log.close()
            with open(log) as f:
                logged = [json.loads(ln) for ln in f if "route_like_default" in ln]
        self.assertEqual(sorted(e["route"] for e in logged),
                         sorted(["0.0.0.0/1", "128.0.0.0/1", "::/3", "2000::/4", "3000::/4", "2000::/3"]))  # each once

    def test_coding_agents_named_hosts_are_checked_too(self):
        p = Proxy(["github.com"], {443}, None, resolver=fake_resolver({"github.com": ["93.184.216.34"]}),
                  proc_net=home_proc(self.tmp.name))
        with self.assertRaises(Refused):
            p.decide(b"CONNECT github.com:443 HTTP/1.1")


class NeverReachTests(unittest.TestCase):
    TABLE = {"home.dyndns.example": ["81.2.69.160"], "nas.example": ["2a02:8070:99::5"], "other.example": ["140.82.112.3"],
             "mapped.example": ["::ffff:81.2.69.161"]}

    def refused(self, p, host):
        with self.assertRaises(Refused, msg=host) as cm:
            p.decide(f"CONNECT {host}:443 HTTP/1.1".encode())
        return cm.exception.code

    def test_address_and_network(self):
        p = Proxy(["*"], {443}, None, resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY,
                  never_reach=["81.2.69.160/30", "2a02:8070:99::/48"])
        self.assertEqual(self.refused(p, "home.dyndns.example"), "never_reach")  # after the lookup: names don't help
        self.assertEqual(self.refused(p, "nas.example"), "never_reach")
        self.assertEqual(self.refused(p, "mapped.example"), "never_reach")
        p.decide(b"CONNECT other.example:443 HTTP/1.1")
        p = Proxy(["*"], {443}, None, resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY, never_reach=["81.2.69.160"])
        self.assertEqual(self.refused(p, "home.dyndns.example"), "never_reach")
        p.decide(b"CONNECT mapped.example:443 HTTP/1.1")  # .161 isn't the single address

    def test_parse(self):
        self.assertEqual([str(n) for n in parse_never_reach(["203.0.113.7", " 2001:db8:1234::/48 ", "198.51.100.9/24"])],
                         ["203.0.113.7/32", "2001:db8:1234::/48", "198.51.100.0/24"])
        self.assertEqual([str(n) for n in parse_never_reach(["::ffff:203.0.113.7", "::ffff:198.51.100.0/120"])],
                         ["203.0.113.7/32", "198.51.100.0/24"])  # IPv4-mapped entries are IPv4
        p = Proxy(["*"], {443}, None, resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY, never_reach=["::ffff:81.2.69.160"])
        self.assertEqual(self.refused(p, "home.dyndns.example"), "never_reach")
        for bad in (["nope"], ["1.2.3.4/40"], [None], ["fe80::1%eth0"]):
            with self.assertRaises(ValueError):
                parse_never_reach(bad)

    def test_live_reload_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            nf = os.path.join(d, "never-reach.txt")
            p = Proxy(["*"], {443}, os.path.join(d, "log"), resolver=fake_resolver(self.TABLE), proc_net=DEFAULT_ONLY,
                      never_file=nf)
            self.assertEqual(self.refused(p, "other.example"), "no_route_info")  # not read yet: nothing goes through
            with open(nf, "w") as f:
                f.write("# from Settings\n140.82.112.0/24\n")
            self.assertEqual(self.refused(p, "other.example"), "never_reach")
            p.decide(b"CONNECT home.dyndns.example:443 HTTP/1.1")
            with open(nf, "w") as f:
                f.write("81.2.69.160\n")
            os.utime(nf, ns=(1, 2_000_000_000_000_000_000))
            p.decide(b"CONNECT other.example:443 HTTP/1.1")
            self.assertEqual(self.refused(p, "home.dyndns.example"), "never_reach")
            with open(nf, "w") as f:
                f.write("junk\n")
            os.utime(nf, ns=(1, 3_000_000_000_000_000_000))
            self.assertEqual(self.refused(p, "home.dyndns.example"), "never_reach")  # a bad file keeps the old list
            p._log.close()


class LiveReloadTests(unittest.TestCase):
    def test_allow_file_reload(self):
        with tempfile.TemporaryDirectory() as d:
            af = os.path.join(d, "allow.txt")
            with open(af, "w") as f:
                f.write("github.com\n")
            p = Proxy([], {443}, os.path.join(d, "log"), allow_file=af,
                      resolver=fake_resolver({"github.com": ["140.82.112.3"], "pypi.org": ["151.101.0.223"]}),
                      proc_net=DEFAULT_ONLY)
            p.decide(b"CONNECT github.com:443 HTTP/1.1")
            with self.assertRaises(PermissionError):
                p.decide(b"CONNECT pypi.org:443 HTTP/1.1")
            with open(af, "w") as f:
                f.write("# edited live\npypi.org\n")
            os.utime(af, ns=(1, 2_000_000_000_000_000_000))
            p.decide(b"CONNECT pypi.org:443 HTTP/1.1")
            with self.assertRaises(PermissionError):
                p.decide(b"CONNECT github.com:443 HTTP/1.1")
            with open(af, "w") as f:
                f.write("not a host!\n")
            os.utime(af, ns=(1, 3_000_000_000_000_000_000))
            p.decide(b"CONNECT pypi.org:443 HTTP/1.1")  # bad file keeps the old list
            p._log.close()


class QuickChangeTests(unittest.TestCase):
    def test_a_second_write_in_the_same_tick_counts(self):
        from cilly.vm import _atomic_write  # how the panel writes both files
        with tempfile.TemporaryDirectory() as d:
            af, nf = os.path.join(d, "allow.txt"), os.path.join(d, "never-reach.txt")
            table = {"aaa.com": ["140.82.112.3"], "bbb.com": ["140.82.112.4"]}
            _atomic_write(af, b"aaa.com\n")
            _atomic_write(nf, b"")
            p = Proxy([], {443}, None, resolver=fake_resolver(table), proc_net=DEFAULT_ONLY, allow_file=af, never_file=nf)
            p.decide(b"CONNECT aaa.com:443 HTTP/1.1")
            for path, data in ((af, b"bbb.com\n"), (nf, b"140.82.112.4\n")):  # same size, same mtime
                before = os.stat(path).st_mtime_ns
                _atomic_write(path, data)
                os.utime(path, ns=(before, before))
                self.assertEqual(os.stat(path).st_mtime_ns, before)
                if path == af:
                    p.decide(b"CONNECT bbb.com:443 HTTP/1.1")  # the second list wins
                    with self.assertRaises(Refused):
                        p.decide(b"CONNECT aaa.com:443 HTTP/1.1")
            _atomic_write(af, b"aaa.com\nbbb.com\n")
            with self.assertRaises(Refused) as cm:
                p.decide(b"CONNECT bbb.com:443 HTTP/1.1")
            self.assertEqual(cm.exception.code, "never_reach")


class ContentReloadTests(unittest.TestCase):
    def test_same_inode_size_and_mtime_still_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            af, log = os.path.join(d, "allow.txt"), os.path.join(d, "log")
            with open(af, "w") as f:
                f.write("aaa.com\n")
            st = os.stat(af)
            p = Proxy([], {443}, log, resolver=fake_resolver({"aaa.com": ["140.82.112.3"], "bbb.com": ["140.82.112.4"]}),
                      proc_net=DEFAULT_ONLY, allow_file=af)
            p.decide(b"CONNECT aaa.com:443 HTTP/1.1")
            with open(af, "r+") as f:  # rewritten in place: the same inode, size and time
                f.write("bbb.com\n")
            os.utime(af, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertEqual((os.stat(af).st_ino, os.stat(af).st_size, os.stat(af).st_mtime_ns), (st.st_ino, st.st_size, st.st_mtime_ns))
            p.decide(b"CONNECT bbb.com:443 HTTP/1.1")
            with open(af, "w") as f:
                f.write("not a host!\n")
            for _ in range(3):
                p.decide(b"CONNECT bbb.com:443 HTTP/1.1")  # a bad file keeps the old list
            p._log.close()
            with open(log) as f:
                self.assertEqual(f.read().count("reload_failed"), 1)  # once for that change, not per connection


class ProxyEndToEnd(unittest.TestCase):
    """Run the proxy against a local echo server via an injected connector."""

    def setUp(self):
        self.echo = socket.socket()
        self.echo.bind(("127.0.0.1", 0))
        self.echo.listen()
        port = self.echo.getsockname()[1]

        def echo_loop():
            while True:
                try:
                    c, _ = self.echo.accept()
                except OSError:
                    return
                data = c.recv(100)
                c.sendall(b"echo:" + data)
                c.close()
        threading.Thread(target=echo_loop, daemon=True).start()

        def connector(family, sa):
            return socket.create_connection(("127.0.0.1", port))
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "egress.log")
        self.proxy = Proxy(["api.github.com"], {443}, self.log,
                           resolver=fake_resolver({"api.github.com": ["140.82.112.5"],
                                                   "evil.github.com": ["140.82.112.6"]}),
                           connector=connector, proc_net=DEFAULT_ONLY)

    def tearDown(self):
        self.echo.close()
        self.proxy._log.close()
        self.tmp.cleanup()

    def _request(self, head: bytes, payload: bytes = b"") -> bytes:
        a, b = socket.socketpair()
        t = threading.Thread(target=self.proxy.handle, args=(b,))
        t.start()
        a.sendall(head)
        resp = a.recv(4096)
        if payload and resp.startswith(b"HTTP/1.1 200"):
            a.sendall(payload)
            a.shutdown(socket.SHUT_WR)
            resp += a.recv(4096)
        a.close()
        t.join(5)
        b.close()
        return resp

    def test_allowed(self):
        r = self._request(b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n", b"ping")
        self.assertTrue(r.startswith(b"HTTP/1.1 200"), r)
        self.assertIn(b"echo:ping", r)
        with open(self.log) as f:
            self.assertIn('"action": "allow"', f.read())

    def test_denied_host_and_port(self):
        self.assertIn(b"403", self._request(b"CONNECT evil.github.com:443 HTTP/1.1\r\n\r\n"))
        self.assertIn(b"403", self._request(b"CONNECT api.github.com:22 HTTP/1.1\r\n\r\n"))
        self.assertIn(b"403", self._request(b"GET http://api.github.com/ HTTP/1.1\r\n\r\n"))
        with open(self.log) as f:
            self.assertEqual(f.read().count('"action": "deny"'), 3)


if __name__ == "__main__":
    unittest.main()
