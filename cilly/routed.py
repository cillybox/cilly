"""cilly's own downloads through a route (egress.route_own): the GitHub API, the Debian image and
artifact downloads.  An opener for urllib whose HTTPS connections go through the route's SOCKS5
proxy, HTTP proxy or Tor, with the name looked up at the far end and TLS checked against it.
Plain HTTP is refused, and a route that is down fails the download: never straight out instead.
"""

from __future__ import annotations

import functools
import http.client
import ssl
import urllib.request

from . import egress_proxy as E

OWN_ISOLATE = "cilly"  # Tor keeps cilly's own downloads on circuits apart from every agent's


class _RoutedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, route: dict, **kw):
        super().__init__(*args, **kw)
        self._route = route

    def connect(self):
        r = self._route
        s = E.dial_route(r)
        try:
            if r["kind"] == "http":
                target = f"{self.host}:{self.port}"
                head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                if "user" in r:
                    import base64
                    cred = base64.b64encode(f"{r['user']}:{r['password']}".encode()).decode()
                    head += f"Proxy-Authorization: Basic {cred}\r\n"
                s.sendall((head + "\r\n").encode())
                reply = b""
                while b"\r\n\r\n" not in reply:
                    chunk = s.recv(1)
                    if not chunk or len(reply) > E.HEAD_MAX:
                        raise E.RouteDown("the HTTP proxy didn't answer")
                    reply += chunk
                if reply.split(b"\r\n", 1)[0].split(b" ")[1:2] != [b"200"]:
                    raise E.RouteDown("the HTTP proxy refused the connection")
            else:
                login = dict(r, user=OWN_ISOLATE, password="cilly") if r["kind"] == "tor" and "user" not in r else r
                E.socks5_connect_name(s, login, self.host, self.port)
            s.settimeout(self.timeout if isinstance(self.timeout, (int, float)) else None)
            self.sock = self._context.wrap_socket(s, server_hostname=self.host)
        except BaseException:
            s.close()
            raise


class _RoutedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, route: dict):
        super().__init__(context=ssl.create_default_context())
        self._route = route

    def https_open(self, req):
        return self.do_open(functools.partial(_RoutedHTTPSConnection, route=self._route), req, context=self._context)


class _NoPlainHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        raise urllib.error.URLError("plain HTTP isn't sent through a route")


def check(route: dict, host: str, port: int = 443, timeout: float = 30) -> None:
    """Connect to host:port through the route and check its TLS certificate, then hang up.  Raises
    egress_proxy.RouteDown when the route fails, ssl.SSLError or OSError when the site does."""
    conn = _RoutedHTTPSConnection(host, port, timeout=timeout, route=route, context=ssl.create_default_context())
    try:
        conn.connect()
    finally:
        conn.close()


def opener(route: dict | None, *handlers) -> urllib.request.OpenerDirector:
    """urllib's opener, through `route` when it is a proxy (None or direct: straight out)."""
    if not route or route.get("kind") == "direct":
        return urllib.request.build_opener(*handlers)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RoutedHTTPSHandler(route), _NoPlainHTTP(), *handlers)
