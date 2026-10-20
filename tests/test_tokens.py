"""Keeping tokens on this PC: the egress proxy swaps a VM's stand-ins for the real tokens, only for
the token hosts, only where a token may go, and never lets the real token back into the VM."""

import base64
import ipaddress
import json
import os
import shutil
import subprocess
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import egress_proxy as E  # noqa: E402
from cilly import tlsca  # noqa: E402
from tests.test_proxy import fake_proc, fake_resolver  # noqa: E402

HOSTS = ["api.anthropic.com", "github.com", "api.github.com"]
ADDR = {"api.anthropic.com": "160.79.104.10", "github.com": "140.82.112.3", "api.github.com": "140.82.112.5",
        "example.org": "93.184.215.14"}
CLAUDE, CLAUDE_REAL = "sk-ant-oat01-cilly-standin-0001", "sk-ant-oat01-REALREALREALREALREAL"
GH, GH_REAL = "ghp_cillystandin00000000000000000000", "ghp_REALREALREALREALREALREALREALREAL1"
RULE = {"agent": "9a70b774", "create": {"kinds": ["feat", "fix", "docs", "refactor", "test", "chore"]}}  # kind/name branches


def read_request(c) -> tuple[bytes, bytes]:
    head = b""
    while b"\r\n\r\n" not in head:
        d = c.recv(4096)
        if not d:
            return head, b""
        head += d
    head, _, body = head.partition(b"\r\n\r\n")
    low = head.lower()
    if b"transfer-encoding: chunked" in low:
        out = b""
        while True:
            while b"\r\n" not in body:
                body += c.recv(4096)
            line, _, body = body.partition(b"\r\n")
            n = int(line, 16)
            while len(body) < n + 2:
                body += c.recv(4096)
            out, body = out + body[:n], body[n + 2:]
            if n == 0:
                return head, out
    n = int(low.split(b"content-length: ", 1)[1].split(b"\r\n", 1)[0]) if b"content-length: " in low else 0
    while len(body) < n:
        body += c.recv(4096)
    return head, body


class FakeSites:
    """The real sites, with their own CA: each answers with the Authorization header it got."""

    def __init__(self, folder):
        os.makedirs(folder, mode=0o700)
        openssl = tlsca.openssl_binary()
        key, self.ca = tlsca._ca(folder, ["anthropic.com", "github.com", "example.org"], openssl)
        self.ctx = {}
        for h in HOSTS + ["example.org"]:
            crt, k = tlsca._leaf(folder, h, key, self.ca, openssl)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(crt, k)
            self.ctx[h] = ctx
        base = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        base.load_cert_chain(*tlsca._leaf(folder, "github.com", key, self.ca, openssl))
        base.sni_callback = lambda sock, name, _ctx: setattr(sock, "context", self.ctx[name]) if name in self.ctx else None
        self.base, self.got = base, []
        self.answer = None  # a test's own raw reply: answer(head, body) -> bytes
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
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
            t = self.base.wrap_socket(c, server_side=True)
            head, body = read_request(t)
            self.got.append((head.decode(), body))
            auth = [ln.split(":", 1)[1].strip() for ln in head.decode().split("\r\n")[1:] if ln.lower().startswith("authorization:")]
            if self.answer:
                t.sendall(self.answer(head.decode(), body))
            else:
                reply = ("auth=" + (auth[0] if auth else "none")).encode()
                t.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(reply), reply))
            t.close()
        except (OSError, ssl.SSLError):
            c.close()

    def close(self):
        self.srv.close()


@unittest.skipUnless(tlsca.openssl_binary(), "needs openssl")
class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sites = FakeSites(os.path.join(self.tmp.name, "sites"))
        self.addCleanup(self.sites.close)
        self.vm = tlsca.make(os.path.join(self.tmp.name, "vm-tls"), HOSTS)  # this start's CA, as the VM trusts it
        self.vm_ca = os.path.join(self.tmp.name, "vm-ca.pem")
        with open(self.vm_ca, "w") as f:
            f.write(self.vm["ca"])
        self.proc = fake_proc(self.tmp.name)
        self.tokens_path = os.path.join(self.tmp.name, "tokens.json")
        self.write_tokens()

    def write_tokens(self, push=RULE, repo="Owner/Repo", **over):
        doc = {"hosts": self.vm["hosts"], "tokens": [
            {"stand_in": CLAUDE, "value": CLAUDE_REAL, "hosts": ["api.anthropic.com"]},
            {"stand_in": GH, "value": GH_REAL, "hosts": ["github.com", "api.github.com"], "github": {"repo": repo, "push": push}}]}
        doc.update(over)
        with open(self.tokens_path, "w") as f:
            json.dump(doc, f)

    def proxy(self, upstream_ca=None, branches_file=None):
        port = self.sites.srv.getsockname()[1]
        p = E.Proxy(HOSTS + ["example.org"], {443}, os.path.join(self.tmp.name, "egress.log"),
                    resolver=fake_resolver({h: [a] for h, a in ADDR.items()}),
                    connector=lambda fam, sa: socket.create_connection(("127.0.0.1", port)),
                    proc_net=self.proc, tokens_file=self.tokens_path, upstream_ca=upstream_ca or self.sites.ca,
                    branches_file=branches_file)
        self.addCleanup(p._log.close)
        p.maybe_reload()
        return p

    def ask(self, p, host, request: bytes, trust=None) -> bytes:
        """What the VM gets back for one request to `host`, over TLS that trusts this start's CA."""
        a, b = socket.socketpair()
        a.settimeout(10)
        t = threading.Thread(target=p.handle, args=(b,))
        t.start()
        tls = None
        try:
            a.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
            resp = a.recv(4096)
            if not resp.startswith(b"HTTP/1.1 200"):
                return resp
            tls = ssl.create_default_context(cafile=trust or self.vm_ca).wrap_socket(
                a, server_hostname=host, do_handshake_on_connect=False)
            tls.do_handshake()
            tls.sendall(request)
            out = b""
            while True:
                d = tls.recv(65536)
                if not d:
                    return out
                out += d
        except ssl.SSLError as e:
            return f"TLS: {e}".encode()
        finally:
            (tls or a).close()
            t.join(10)
            b.close()

    def req(self, method, host, path, headers=(), body=b""):
        hs = "".join(f"{n}: {v}\r\n" for n, v in [("Host", host), *headers])
        return f"{method} {path} HTTP/1.1\r\n{hs}\r\n".encode() + body

    def sent(self):
        return self.sites.got[-1][0]

    def test_the_claude_login_is_swapped_in_and_masked_on_the_way_back(self):
        out = self.ask(self.proxy(), "api.anthropic.com",
                       self.req("POST", "api.anthropic.com", "/v1/messages", [("Authorization", f"Bearer {CLAUDE}"),
                                                                             ("Content-Length", "2")], b"{}"))
        self.assertIn(f"Authorization: Bearer {CLAUDE_REAL}", self.sent())  # the real site got the real login
        self.assertIn(b"HTTP/1.1 200", out)
        self.assertNotIn(CLAUDE_REAL.encode(), out)  # the VM never sees it, even echoed back
        self.assertIn(b"auth=" + b"*" * len(f"Bearer {CLAUDE_REAL}"), out)  # what we wrote is masked whole
        self.assertEqual(self.sites.got[-1][1], b"{}")
        self.assertIn("Connection: close", self.sent())
        self.assertIn("Accept-Encoding: identity", self.sent())  # so the reply can be checked

    def test_an_mcp_servers_key_and_its_streamed_reply(self):
        """A remote MCP server's key, as server.App._stand_ins hands it over: no GitHub limits, swapped in its
        Authorization header, and the server's event stream comes back whole, with the key masked."""
        key, real = "cilly0123456789abcdef0123456789abcdef", "mcp-REAL-key-0123456789"
        self.write_tokens(tokens=[{"stand_in": key, "value": real, "hosts": ["api.anthropic.com"]}])
        events = [b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n',
                  b"data: echo " + real.encode() + b"\n\n"]
        self.sites.answer = lambda head, body: (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                                                + b"".join(b"%x\r\n%s\r\n" % (len(e), e) for e in events) + b"0\r\n\r\n")
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
        out = self.ask(self.proxy(), "api.anthropic.com",
                       self.req("POST", "api.anthropic.com", "/mcp", [("Authorization", f"Bearer {key}"),
                                                                      ("Accept", "application/json, text/event-stream"),
                                                                      ("Content-Length", str(len(body)))], body))
        self.assertIn(f"Authorization: Bearer {real}", self.sent())
        self.assertNotIn(key, self.sent())
        self.assertEqual(self.sites.got[-1][1], body)
        self.assertIn(b'"result":{"tools":[]}', out)
        self.assertNotIn(real.encode(), out)  # echoed back: masked

    def test_git_gets_the_token_only_for_its_own_repository(self):
        p = self.proxy()
        basic = base64.b64encode(f"x-access-token:{GH}".encode()).decode()
        for path, real in (("/owner/repo.git/info/refs?service=git-upload-pack", True),
                           ("/Owner/Repo/git-upload-pack", True),
                           ("/someone/else.git/info/refs?service=git-upload-pack", False),
                           ("/owner/repo.git/../../someone/else.git/info/refs", False)):
            self.ask(p, "github.com", self.req("GET", "github.com", path, [("Authorization", f"Basic {basic}")]))
            want = base64.b64encode(f"x-access-token:{GH_REAL}".encode()).decode()
            self.assertEqual(f"Authorization: Basic {want}" in self.sent(), real, path)
            self.assertNotIn(GH, self.sent())  # the stand-in never goes out either
            if not real:
                self.assertNotIn("Authorization", self.sent())  # anonymous, as for any public repository

    def test_the_api_token_only_for_its_repository_and_graphql(self):
        p = self.proxy()
        for path, real in (("/repos/owner/repo/pulls", True), ("/repos/Owner/Repo", True), ("/graphql", True),
                           ("/user", True), ("/rate_limit", True), ("/repos/owner/other/pulls", False), ("/orgs/owner/repos", False),
                           ("/repos/owner/repo/%2e%2e/x", False)):
            self.ask(p, "api.github.com", self.req("GET", "api.github.com", path, [("Authorization", f"token {GH}")]))
            self.assertEqual(f"Authorization: token {GH_REAL}" in self.sent(), real, path)
        self.ask(p, "api.github.com", self.req("GET", "api.github.com", "/repos/owner/repo",
                                               [("Authorization", f"Bearer {CLAUDE}")]))
        self.assertNotIn("Authorization", self.sent())  # the Claude login never goes to GitHub
        for method, path, real in (("POST", "/repos/owner/repo/pulls", True), ("PATCH", "/repos/owner/repo/pulls/3", True),
                                   ("POST", "/repos/owner/repo/issues/3/comments", True), ("GET", "/repos/owner/repo/git/refs/heads/main", True),
                                   ("POST", "/repos/owner/repo/git/refs", False), ("PATCH", "/repos/owner/repo/git/refs/heads/main", False),
                                   ("PUT", "/repos/owner/repo/contents/README.md", False), ("PUT", "/repos/owner/repo/pulls/3/merge", False),
                                   ("POST", "/repos/owner/repo/releases", False), ("PATCH", "/repos/owner/repo", False),
                                   ("DELETE", "/repos/owner/repo", False), ("POST", "/repos/owner/repo/merges", False),
                                   ("POST", "/repos/owner/repo/branches/main/rename", False)):
            self.ask(p, "api.github.com", self.req(method, "api.github.com", path,
                                                   [("Authorization", f"token {GH}"), ("Content-Length", "2")], b"{}"))
            self.assertEqual(f"Authorization: token {GH_REAL}" in self.sent(), real, (method, path))
        self.write_tokens(push=None)  # the switch in Settings off: the API as the token allows
        p = self.proxy()
        self.ask(p, "api.github.com", self.req("POST", "api.github.com", "/repos/owner/repo/git/refs",
                                               [("Authorization", f"token {GH}"), ("Content-Length", "2")], b"{}"))
        self.assertIn(f"Authorization: token {GH_REAL}", self.sent())
        for method in ("PATCH", "POST", "DELETE"):  # the bot account itself is read, never changed
            self.ask(p, "api.github.com", self.req(method, "api.github.com", "/user",
                                                   [("Authorization", f"token {GH}"), ("Content-Length", "2")], b"{}"))
            self.assertNotIn("Authorization", self.sent(), method)

    def test_github_never_gets_a_token_that_isnt_held_to_a_repository(self):
        """An entry without its repository (none of cilly's own has one) goes nowhere on GitHub,
        while a key without GitHub limits still goes to its other hosts."""
        self.write_tokens(tokens=[{"stand_in": GH, "value": GH_REAL, "hosts": ["github.com", "api.github.com", "api.anthropic.com"]}])
        p = self.proxy()
        basic = base64.b64encode(f"x-access-token:{GH}".encode()).decode()
        self.ask(p, "github.com", self.req("GET", "github.com", "/owner/repo.git/info/refs?service=git-upload-pack",
                                           [("Authorization", f"Basic {basic}")]))
        self.assertNotIn("Authorization", self.sent())
        for path in ("/repos/owner/repo", "/user", "/gists"):
            self.ask(p, "api.github.com", self.req("GET", "api.github.com", path, [("Authorization", f"token {GH}")]))
            self.assertNotIn("Authorization", self.sent(), path)
        self.ask(p, "api.anthropic.com", self.req("GET", "api.anthropic.com", "/v1/models", [("Authorization", f"Bearer {GH}")]))
        self.assertIn(f"Authorization: Bearer {GH_REAL}", self.sent())

    def test_a_token_goes_only_in_a_login_header(self):
        p = self.proxy()
        basic = base64.b64encode(f"x-access-token:{CLAUDE}".encode()).decode()
        self.ask(p, "api.anthropic.com", self.req("GET", "api.anthropic.com", "/v1/models", [
            ("Authorization", f"Bearer {CLAUDE}"), ("x-api-key", CLAUDE), ("X-Note", f"mine is {CLAUDE}"),
            ("Cookie", f"t={CLAUDE}"), ("X-Basic", f"Basic {basic}"), ("User-Agent", "claude-cli")]))
        sent = self.sent()
        self.assertIn(f"Authorization: Bearer {CLAUDE_REAL}", sent)
        self.assertIn(f"x-api-key: {CLAUDE_REAL}", sent)
        for gone in ("X-Note", "Cookie"):
            self.assertNotIn(gone, sent)  # a header with a stand-in that isn't a login header is dropped
        self.assertIn(f"X-Basic: Basic {basic}", sent)  # Basic logins are read only in Authorization
        self.assertIn("User-Agent: claude-cli", sent)
        self.assertEqual(sent.count(CLAUDE_REAL), 2)

    def test_an_echoed_basic_login_is_masked(self):
        """git's login is base64: a site echoing it back would hand the VM the real token encoded."""
        def answer(head, body):
            auth = head.split("Authorization: ", 1)[1].split("\r\n")[0].encode()
            return b"HTTP/1.1 200 OK\r\nX-Echo: %s\r\nContent-Length: %d\r\n\r\n%s" % (auth, len(auth), auth)
        self.sites.answer = answer
        basic = base64.b64encode(f"x-access-token:{GH}".encode()).decode()
        real = base64.b64encode(f"x-access-token:{GH_REAL}".encode())
        out = self.ask(self.proxy(), "github.com", self.req("GET", "github.com", "/owner/repo.git/info/refs?service=git-upload-pack",
                                                            [("Authorization", f"Basic {basic}")]))
        self.assertIn(real, self.sent().encode())
        self.assertNotIn(real, out)
        self.assertNotIn(real[6:-6], out)
        self.assertIn(b"X-Echo: " + b"*" * len(b"Basic " + real), out)

    def test_a_chunked_reply_cant_split_a_token_past_the_mask(self):
        def answer(head, body):
            t = CLAUDE_REAL.encode()
            pieces = [b"before ", t[:5], t[5:20], t[20:] + b" after"]
            return (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + b"".join(b"%x\r\n%s\r\n" % (len(x), x) for x in pieces) + b"0\r\n\r\n")
        self.sites.answer = answer
        out = self.ask(self.proxy(), "api.anthropic.com", self.req("GET", "api.anthropic.com", "/v1/models",
                                                                   [("Authorization", f"Bearer {CLAUDE}")]))
        head, _, body = out.partition(b"\r\n\r\n")
        self.assertIn(b"Transfer-Encoding: chunked", head)
        self.assertNotIn(CLAUDE_REAL[:12].encode(), out)
        data = b""
        while True:
            size, _, body = body.partition(b"\r\n")
            n = int(size, 16)
            data, body = data + body[:n], body[n + 2:]
            if n == 0:
                break
        self.assertEqual(data, b"before " + b"*" * len(CLAUDE_REAL) + b" after")

    def test_a_compressed_reply_is_refused(self):
        self.sites.answer = lambda head, body: b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 4\r\n\r\n\x1f\x8b\x08\x00"
        out = self.ask(self.proxy(), "api.anthropic.com", self.req("GET", "api.anthropic.com", "/v1/models",
                                                                   [("Authorization", f"Bearer {CLAUDE}")]))
        self.assertTrue(out.startswith(b"HTTP/1.1 502"), out[:60])
        self.assertNotIn(b"\x1f\x8b", out)

    def test_an_early_hint_then_the_reply(self):
        self.sites.answer = lambda head, body: (b"HTTP/1.1 103 Early Hints\r\nLink: </a>\r\n\r\n"
                                                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                                                b"%x\r\n%s\r\n0\r\n\r\n" % (len(CLAUDE_REAL), CLAUDE_REAL.encode()))
        out = self.ask(self.proxy(), "api.anthropic.com", self.req("GET", "api.anthropic.com", "/v1/models",
                                                                   [("Authorization", f"Bearer {CLAUDE}")]))
        self.assertTrue(out.startswith(b"HTTP/1.1 103 Early Hints\r\n"), out[:60])
        self.assertIn(b"HTTP/1.1 200 OK", out)
        self.assertNotIn(CLAUDE_REAL.encode(), out)
        self.assertTrue(out.endswith(b"0\r\n\r\n"))

    def push(self, ref, push=RULE, new=True, proxy=None, caps="report-status"):
        self.write_tokens(push=push)
        cmd = f"{'0' * 40 if new else '2' * 40} {'1' * 40} {ref}\0{caps}".encode()
        pkt = b"%04x" % (len(cmd) + 4) + cmd + b"0000" + b"PACK....."
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(pkt), pkt)
        basic = base64.b64encode(f"x-access-token:{GH}".encode()).decode()
        before = len(self.sites.got)
        out = self.ask(proxy or self.proxy(), "github.com", self.req("POST", "github.com", "/owner/repo.git/git-receive-pack",
                                                                     [("Authorization", f"Basic {basic}"),
                                                                      ("Transfer-Encoding", "chunked")], chunked))
        return out, self.sites.got[before:], pkt

    def test_a_push_only_to_its_own_branches(self):
        out, got, pkt = self.push("refs/heads/fix/menu")  # a new branch named kind/name
        self.assertIn(b"HTTP/1.1 200", out)
        self.assertEqual(got[0][1], pkt)  # the whole push, unchanged
        self.assertIn(b"auth=****", out)  # the site's own answer
        out, got, _ = self.push("refs/heads/main")
        self.assertIn(b"Content-Type: application/x-git-receive-pack-result", out)  # refused the way git understands
        self.assertTrue(out.endswith(b"ng refs/heads/main cilly lets this agent push only to its own branches and to a new "
                                     b"branch named kind/name, where the kind is feat, fix, docs, refactor, test, chore\n0000"), out[-160:])
        self.assertEqual(got, [])  # nothing of it reached GitHub
        with open(os.path.join(self.tmp.name, "egress.log")) as f:
            self.assertIn("test, chore, not refs/heads/main", f.read().splitlines()[-2])
        for ref, new, ok in (("refs/heads/fix/menu", False, False),  # changing a fix/ branch it didn't create: someone's
                             ("refs/heads/agent/9a70b774-2", False, True), ("refs/heads/vmpanel/9a70b774", False, True),  # by its id
                             ("refs/heads/agent/0badc0de", False, False), ("refs/heads/feature/menu", True, False),
                             ("refs/heads/fix/Menu", True, False), ("refs/heads/fix/a/b", True, False), ("refs/heads/fix/", True, False),
                             ("refs/heads/fix/" + "a" * 61, True, False), ("refs/tags/v1", True, False), ("refs/heads/cilly/menu", True, False)):
            out, got, _ = self.push(ref, new=new)
            self.assertEqual((bool(got), b"ng " + ref.encode() + b" " in out), (ok, not ok), ref)
        out, got, _ = self.push("refs/heads/main", push=None)  # the switch in Settings: anywhere
        self.assertIn(b"auth=****", out)

    def test_new_branches_under_a_prefix(self):
        rule = {"agent": "9a70b774", "create": {"prefix": "cilly/"}}
        for ref, new, ok in (("refs/heads/cilly/login-timeout", True, True), ("refs/heads/cilly/login-timeout", False, False),
                             ("refs/heads/cilly/a/b", True, False), ("refs/heads/fix/menu", True, False), ("refs/heads/cilly/9a70b774", False, True)):
            out, got, _ = self.push(ref, push=rule, new=new)
            self.assertEqual(bool(got), ok, ref)
        out, got, _ = self.push("refs/heads/cilly/9a70b774-3", push={"agent": "9a70b774", "create": None})  # names by id only
        self.assertTrue(got)
        out, got, _ = self.push("refs/heads/cilly/menu", push={"agent": "9a70b774", "create": None})
        self.assertFalse(got)

    def test_a_branch_it_created_is_its_own(self):
        """Only once GitHub says it made the branch, and still after a restart (the branches file)."""
        own = os.path.join(self.tmp.name, "branches.jsonl")
        report = lambda line: lambda head, body: (  # noqa: E731  GitHub's answer, in side band 1
            b"HTTP/1.1 200 OK\r\nContent-Type: application/x-git-receive-pack-result\r\nTransfer-Encoding: chunked\r\n\r\n"
            + b"%x\r\n%s\r\n0\r\n\r\n" % (len(pkt := band(line)), pkt))
        band = lambda line: (b"%04x" % (len(inner := b"000eunpack ok\n" + b"%04x" % (len(line) + 4) + line + b"0000") + 5)  # noqa: E731
                             + b"\x01" + inner + b"0013\x02Resolving deltas\n0000")
        self.sites.answer = report(b"ng refs/heads/fix/menu already exists\n")  # someone's: GitHub refuses the create
        p = self.proxy(branches_file=own)
        self.push("refs/heads/fix/menu", proxy=p, caps="report-status side-band-64k")
        self.assertFalse(self.push("refs/heads/fix/menu", new=False, proxy=p)[1])  # so it's not the agent's to change
        self.assertFalse(os.path.exists(own))
        self.sites.answer = report(b"ok refs/heads/fix/menu\n")
        out, got, _ = self.push("refs/heads/fix/menu", proxy=p, caps="report-status side-band-64k")
        self.assertIn(b"Resolving deltas", out)  # the answer reaches git as it was
        self.assertTrue(self.push("refs/heads/fix/menu", new=False, proxy=p)[1])  # now it is
        with open(own) as f:
            self.assertEqual(f.read(), '{"branch": "fix/menu"}\n')
        self.assertTrue(self.push("refs/heads/fix/menu", new=False, proxy=self.proxy(branches_file=own))[1])  # the next start
        self.assertFalse(self.push("refs/heads/fix/menu", new=False)[1])  # another agent's proxy, without the file

    def test_reading_githubs_answer_to_a_push(self):
        pkt = lambda b: b"%04x" % (len(b) + 4) + b  # noqa: E731
        plain = pkt(b"unpack ok\n") + pkt(b"ok refs/heads/fix/a\n") + pkt(b"ng refs/heads/fix/b already exists\n") + b"0000"
        self.assertEqual(E.receive_pack_report(plain, False), {"refs/heads/fix/a"})
        banded = b"%04x" % (len(plain) + 5) + b"\x01" + plain + b"0000"
        self.assertEqual(E.receive_pack_report(banded, True), {"refs/heads/fix/a"})
        self.assertEqual(E.receive_pack_report(b"0013\x02ok refs/heads/x\n0000", True), set())  # band 2 is progress text
        self.assertEqual(E.receive_pack_report(b"garbage", False), set())

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_itself_shows_why_a_push_was_refused(self):
        """A real git push through the proxy: git prints the reason, not a bare HTTP error."""
        def answer(head, body):
            if "info/refs?service=git-receive-pack" in head:
                line = b"0" * 40 + b" capabilities^{}\0report-status side-band-64k ofs-delta\n"
                adv = b"001f# service=git-receive-pack\n0000" + b"%04x" % (len(line) + 4) + line + b"0000"
                return (b"HTTP/1.1 200 OK\r\nContent-Type: application/x-git-receive-pack-advertisement\r\n"
                        b"Content-Length: %d\r\n\r\n%s" % (len(adv), adv))
            return b"HTTP/1.1 500 Nope\r\nContent-Length: 0\r\n\r\n"  # never reached: the proxy answers
        self.sites.answer = answer
        p = self.proxy()
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=p.handle, args=(c,), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        repo = os.path.join(self.tmp.name, "repo")
        env = dict({k: v for k, v in os.environ.items() if k in ("PATH", "LANG")}, HOME=self.tmp.name,
                   GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@a", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")
        run = lambda *a: subprocess.run(["git", "-C", repo, *a], env=env, capture_output=True, text=True, timeout=30)  # noqa: E731
        subprocess.run(["git", "init", "-q", repo], env=env, check=True)
        run("commit", "-q", "--allow-empty", "-m", "x")
        r = run("-c", f"http.proxy=http://127.0.0.1:{srv.getsockname()[1]}", "-c", f"http.sslCAInfo={self.vm_ca}",
                "-c", f"http.extraHeader=Authorization: Basic {base64.b64encode(f'x-access-token:{GH}'.encode()).decode()}",
                "push", "https://github.com/owner/repo.git", "HEAD:refs/heads/main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[remote rejected]", r.stderr)
        self.assertIn("cilly lets this agent push only to its own branches", r.stderr)
        self.assertNotIn("403", r.stderr)
        self.assertEqual([h for h, _ in self.sites.got if h.startswith("POST ")], [])  # the push never reached GitHub

    def test_strict_about_what_it_passes_on(self):
        p = self.proxy()
        auth = [("Authorization", f"Bearer {CLAUDE}")]
        bad = {
            "both framings": self.req("POST", "api.anthropic.com", "/v1/messages", auth + [("Content-Length", "2"), ("Transfer-Encoding", "chunked")], b"{}"),
            "another host": self.req("GET", "evil.example", "/v1/models", auth),
            "HTTP/1.0": b"GET /v1/models HTTP/1.0\r\nHost: api.anthropic.com\r\n\r\n",
            "absolute form": b"GET https://api.anthropic.com/v1/models HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            "a folded header": b"GET /v1/models HTTP/1.1\r\nHost: api.anthropic.com\r\n x-folded: 1\r\n\r\n",
            "an upgrade": self.req("GET", "api.anthropic.com", "/v1/models", auth + [("Upgrade", "websocket")]),
            "a bad chunk": self.req("POST", "api.anthropic.com", "/v1/messages", auth + [("Transfer-Encoding", "chunked")], b"zz\r\n"),
        }
        for what, request in bad.items():
            before = len(self.sites.got)
            out = self.ask(p, "api.anthropic.com", request)
            self.assertTrue(out.startswith(b"HTTP/1.1 400"), (what, out[:80]))
            self.assertEqual(len(self.sites.got), before, what)  # nothing went on

    def test_one_request_per_connection(self):
        two = self.req("GET", "api.anthropic.com", "/v1/models", [("Authorization", f"Bearer {CLAUDE}")]) * 2
        before = len(self.sites.got)
        self.ask(self.proxy(), "api.anthropic.com", two)
        self.assertEqual(len(self.sites.got), before + 1)  # the second never went on

    def test_expect_100_continue(self):
        out = self.ask(self.proxy(), "api.anthropic.com", self.req("POST", "api.anthropic.com", "/v1/messages",
                       [("Authorization", f"Bearer {CLAUDE}"), ("Expect", "100-continue"), ("Content-Length", "2")], b"{}"))
        self.assertTrue(out.startswith(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200"), out[:60])
        self.assertNotIn("Expect", self.sent())

    def test_other_sites_stay_sealed(self):
        """Not a token host: a plain tunnel, so this start's CA can't read it."""
        out = self.ask(self.proxy(), "example.org", self.req("GET", "example.org", "/"))
        self.assertTrue(out.startswith(b"TLS:"), out[:80])  # the real site's certificate, which the VM's CA didn't sign
        out = self.ask(self.proxy(), "example.org", self.req("GET", "example.org", "/"), trust=self.sites.ca)
        self.assertIn(b"auth=none", out)

    def test_a_site_whose_certificate_doesnt_check_out(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other, mode=0o700)
        _, other_ca = tlsca._ca(other, ["example.net"], tlsca.openssl_binary())
        out = self.ask(self.proxy(upstream_ca=other_ca), "api.anthropic.com",
                       self.req("GET", "api.anthropic.com", "/v1/models", [("Authorization", f"Bearer {CLAUDE}")]))
        self.assertTrue(out.startswith(b"HTTP/1.1 502"), out[:60])  # refused before the VM said anything

    def test_without_a_readable_tokens_file_nothing_is_decrypted(self):
        with open(self.tokens_path, "w") as f:
            f.write("{not json")
        out = self.ask(self.proxy(), "api.anthropic.com", self.req("GET", "api.anthropic.com", "/v1/models"))
        self.assertTrue(out.startswith(b"TLS:"), out[:80])  # a sealed tunnel: the stand-in alone reaches nothing

    def test_what_a_tokens_file_takes(self):
        good = {"hosts": {"github.com": {"cert": "/c", "key": "/k"}},
                "tokens": [{"stand_in": GH, "value": GH_REAL, "hosts": ["github.com"], "github": {"repo": "o/r", "push": RULE}}]}
        E.parse_tokens(good)
        for bad in ({"hosts": {"github.com": {"cert": "c", "key": "/k"}}},
                    dict(good, tokens=[dict(good["tokens"][0], hosts=["api.anthropic.com"])]),
                    dict(good, tokens=[dict(good["tokens"][0], value="has space")]),
                    dict(good, tokens=[dict(good["tokens"][0], value="x\r\nInjected: 1")]),
                    dict(good, tokens=[dict(good["tokens"][0], github={"repo": "o/r/../x", "push": None})]),
                    dict(good, tokens=[dict(good["tokens"][0], github={"repo": "o/r", "push": "agent/"})]),
                    dict(good, tokens=[dict(good["tokens"][0], github={"repo": "o/r", "push": {"agent": "../x", "create": None}})]),
                    dict(good, tokens=[dict(good["tokens"][0], github={"repo": "o/r", "push": dict(RULE, create={"kinds": ["wip"]})})]),
                    dict(good, tokens=[dict(good["tokens"][0], github={"repo": "o/r", "push": dict(RULE, create={"prefix": "../"})})])):
            with self.assertRaises(ValueError, msg=bad):
                E.parse_tokens(bad)


class FakeModelServer:
    """A model server on plain HTTP, like Ollama: answers /v1/messages with a streamed reply that
    says which headers it got, and anything else with a 404 it records."""

    def __init__(self):
        self.got = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        with c:
            head, body = read_request(c)
            if not head:
                return  # the proxy connected, then sent nothing
            self.got.append((head.decode(), body))
            first = head.decode().split("\r\n", 1)[0]
            if first.split(" ")[:2] not in (["POST", "/llm/v1/messages"], ["POST", "/llm/v1/messages?beta=true"]):
                c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            auth = [ln for ln in head.decode().split("\r\n") if ln.lower().startswith(("authorization:", "x-api-key:"))]
            events = [b'event: message_start\ndata: {"type":"message_start"}\n\n',
                      b"data: " + json.dumps({"seen": auth}).encode() + b"\n\n", b"event: message_stop\ndata: {}\n\n"]
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                      + b"".join(b"%x\r\n%s\r\n" % (len(e), e) for e in events) + b"0\r\n\r\n")

    def close(self):
        self.srv.close()


@unittest.skipUnless(tlsca.openssl_binary(), "needs openssl")
class ModelServerTests(unittest.TestCase):
    """An agent on a local model reaches your model server through its proxy, and only that."""
    HOST = "gpu.model.cilly.internal"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeModelServer()
        self.addCleanup(self.server.close)
        self.vm = tlsca.make(os.path.join(self.tmp.name, "vm-tls"), [self.HOST, "api.anthropic.com"])
        self.vm_ca = os.path.join(self.tmp.name, "vm-ca.pem")
        with open(self.vm_ca, "w") as f:
            f.write(self.vm["ca"])
        self.tokens_path = os.path.join(self.tmp.name, "tokens.json")
        self.write(key="sk-local-REALKEY-123")

    def write(self, key=None, url=None, never=None):
        port = self.server.srv.getsockname()[1]
        doc = {"hosts": self.vm["hosts"], "tokens": [
            {"stand_in": CLAUDE, "value": CLAUDE_REAL, "hosts": ["api.anthropic.com"]}],
               "models": {self.HOST: {"url": url or f"http://127.0.0.1:{port}/llm", "key": key, "model": "qwen3-coder:30b"}}}
        with open(self.tokens_path, "w") as f:
            json.dump(doc, f)

    BODY = json.dumps({"model": "qwen3-coder:30b", "max_tokens": 1, "messages": []}).encode()

    def proxy(self, never=(), connector=None):
        p = E.Proxy(["api.anthropic.com"], {443}, os.path.join(self.tmp.name, "egress.log"),
                    resolver=fake_resolver({}), connector=connector or (lambda fam, sa: socket.create_connection(sa[:2])),
                    proc_net=fake_proc(self.tmp.name), tokens_file=self.tokens_path, never_reach=list(never))
        self.addCleanup(p._log.close)
        return p

    ask = TokenTests.ask
    req = TokenTests.req

    def test_claude_code_reaches_your_model_server_with_its_key(self):
        out = self.ask(self.proxy(), self.HOST, self.req("POST", self.HOST, "/v1/messages?beta=true", [
            ("Authorization", "Bearer cilly-local"), ("x-api-key", CLAUDE), ("anthropic-version", "2023-06-01"),
            ("Transfer-Encoding", "chunked")], b"%x\r\n%s\r\n0\r\n\r\n" % (len(self.BODY), self.BODY)))
        head, body = self.server.got[-1]
        self.assertTrue(head.startswith("POST /llm/v1/messages?beta=true HTTP/1.1"), head[:80])  # the address's path, then the API's
        self.assertIn(f"Host: 127.0.0.1:{self.server.srv.getsockname()[1]}", head)
        self.assertIn("Authorization: Bearer sk-local-REALKEY-123", head)  # the server's key, put in here
        self.assertNotIn("cilly-local", head)
        self.assertNotIn(CLAUDE, head)  # nor anything the VM sent as a login
        self.assertNotIn(CLAUDE_REAL, head)
        self.assertEqual(body, self.BODY)
        self.assertIn(f"Content-Length: {len(self.BODY)}", head)  # read whole to check it, sent on with its length
        self.assertIn(b"event: message_stop", out)  # streamed back
        self.assertNotIn(b"REALKEY", out)  # the server repeating the key: masked
        self.assertIn(b"*" * len("sk-local-REALKEY-123"), out)

    def test_without_a_key_none_is_sent(self):
        self.write(key=None)
        self.ask(self.proxy(), self.HOST, self.req("POST", self.HOST, "/v1/messages", [
            ("Authorization", "Bearer cilly-local"), ("Content-Length", str(len(self.BODY)))], self.BODY))
        self.assertNotIn("Authorization", self.server.got[-1][0])

    def test_only_the_model_the_agent_picked(self):
        """The server may have others: a paid one it forwards to, or one too big for the GPU."""
        p = self.proxy()
        for body in (json.dumps({"model": "gpt-oss:120b"}).encode(), json.dumps({"model": "qwen3-coder:30b:cloud"}).encode(),
                     b"not json", b"[" * 100000, json.dumps(["qwen3-coder:30b"]).encode(), b""):
            out = self.ask(p, self.HOST, self.req("POST", self.HOST, "/v1/messages", [("Content-Length", str(len(body)))], body))
            self.assertTrue(out.startswith(b"HTTP/1.1 400"), (body[:40], out[:80]))
        self.assertEqual(self.server.got, [])  # none of them reached it

    def test_a_system_turn_later_on_gets_a_400_without_reaching_it(self):
        """Claude Code then sends those instructions inside the user's message. Many chat templates
        raise on a later system turn, and the server answers 500, which Claude Code only retries."""
        p = self.proxy()
        later = json.dumps({"model": "qwen3-coder:30b", "max_tokens": 1, "system": [{"type": "text", "text": "You are"}],
                            "messages": [{"role": "user", "content": "hi"},
                                         {"role": "system", "content": [{"type": "text", "text": "# Environment"}]}]}).encode()
        out = self.ask(p, self.HOST, self.req("POST", self.HOST, "/v1/messages?beta=true", [("Content-Length", str(len(later)))], later))
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:60])
        err = json.loads(out.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"]["type"], "invalid_request_error")
        # the words Claude Code goes by to send them again inside the user's message (checked with 2.1.303)
        self.assertIn('role "system" is not supported', err["error"]["message"])
        self.assertEqual(self.server.got, [])
        # counting tokens isn't rendered with the template, and the system prompt at the top is fine
        self.ask(p, self.HOST, self.req("POST", self.HOST, "/v1/messages/count_tokens", [("Content-Length", str(len(later)))], later))
        top = json.dumps({"model": "qwen3-coder:30b", "max_tokens": 1, "system": "You are",
                          "messages": [{"role": "user", "content": "hi"}]}).encode()
        out = self.ask(p, self.HOST, self.req("POST", self.HOST, "/v1/messages", [("Content-Length", str(len(top)))], top))
        self.assertIn(b"message_stop", out)
        self.assertEqual([h.split(" ", 2)[1] for h, _ in self.server.got], ["/llm/v1/messages/count_tokens", "/llm/v1/messages"])

    def test_a_short_key_works_and_doesnt_garble_replies(self):
        self.write(key="EMPTY")
        out = self.ask(self.proxy(), self.HOST, self.req("POST", self.HOST, "/v1/messages", [("Content-Length", str(len(self.BODY)))],
                                                         self.BODY))
        self.assertIn("Authorization: Bearer EMPTY", self.server.got[-1][0])
        self.assertIn(b"message_stop", out)  # "EMPTY" isn't masked: it would garble replies and guards nothing
        self.assertNotIn(b"Bearer EMPTY", out)  # what we wrote still is

    def test_every_address_is_tried(self):
        """localhost may be ::1 first, while the server listens on 127.0.0.1 only."""
        tried = []

        def connector(fam, sa):
            tried.append(sa[0])
            if sa[0] == "::1":
                raise ConnectionRefusedError("refused")
            return socket.create_connection(sa[:2])
        port = self.server.srv.getsockname()[1]
        real = socket.getaddrinfo
        fake = lambda host, port_, *a, **kw: ([(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port_, 0, 0)),  # noqa: E731
                                                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port_))]
                                               if host == "localhost" else real(host, port_, *a, **kw))
        self.write(url=f"http://localhost:{port}/llm")
        with unittest.mock.patch.object(E.socket, "getaddrinfo", fake):
            out = self.ask(self.proxy(connector=connector), self.HOST, self.req("POST", self.HOST, "/v1/messages", [
                ("Content-Length", str(len(self.BODY)))], self.BODY))
        self.assertEqual(tried, ["::1", "127.0.0.1"])
        self.assertIn(b"message_stop", out)

    def test_only_the_messages_api(self):
        p = self.proxy()
        for method, path in (("POST", "/api/delete"), ("POST", "/api/pull"), ("GET", "/api/tags"), ("DELETE", "/v1/models"),
                             ("GET", "/v1/messages"), ("POST", "/v1/messages/../../api/delete")):
            before = len(self.server.got)
            out = self.ask(p, self.HOST, self.req(method, self.HOST, path, [("Content-Length", "0")]))
            self.assertTrue(out.startswith(b"HTTP/1.1 400"), (method, path, out[:60]))
            self.assertEqual(len(self.server.got), before, (method, path))  # never reached it
        self.ask(p, self.HOST, self.req("GET", self.HOST, "/v1/models"))
        self.assertTrue(self.server.got[-1][0].startswith("GET /llm/v1/models "))

    def test_not_without_this_agents_model(self):
        """Another agent's proxy has no entry for it: the name isn't on the allowlist and leads nowhere."""
        with open(self.tokens_path, "w") as f:
            json.dump({"hosts": self.vm["hosts"], "tokens": []}, f)
        out = self.ask(self.proxy(), self.HOST, b"")
        self.assertTrue(out.startswith(b"HTTP/1.1 403"), out[:60])
        self.assertEqual(self.server.got, [])

    def test_never_reach_still_wins(self):
        out = self.ask(self.proxy(never=["127.0.0.0/8"]), self.HOST, b"")
        self.assertTrue(out.startswith(b"HTTP/1.1 403"), out[:60])
        self.assertEqual(self.server.got, [])

    def test_what_a_model_entry_takes(self):
        base = {"hosts": {self.HOST: {"cert": "/c", "key": "/k"}}}
        E.parse_tokens(dict(base, models={self.HOST: {"url": "http://192.168.1.50:11434", "key": None, "model": "m"}}))
        E.parse_tokens(dict(base, models={self.HOST: {"url": "http://192.168.1.50:11434", "key": "EMPTY", "model": "m"}}))
        for bad in ({"gpu.model.cilly.internal": {"url": "ftp://x", "model": "m"}},
                    {"gpu.model.cilly.internal": {"url": "http://u:p@x:1", "model": "m"}},
                    {"gpu.model.cilly.internal": {"url": "http://x:1/a b", "model": "m"}},
                    {"gpu.model.cilly.internal": {"url": "http://x:1", "key": "has space", "model": "m"}},
                    {"api.anthropic.com": {"url": "http://x:1"}},  # not a model server's name
                    {"other.model.cilly.internal": {"url": "http://x:1"}},  # no certificate
                    {"gpu.model.cilly.internal": {"url": "http://x:1", "key": None}},  # which model
                    {"gpu.model.cilly.internal": {"url": "http://x:1", "key": None, "model": "a b"}}):
            with self.assertRaises(ValueError, msg=bad):
                E.parse_tokens(dict(base, hosts=dict(base["hosts"], **{"api.anthropic.com": {"cert": "/c", "key": "/k"}}),
                                    models=bad))


class PieceTests(unittest.TestCase):
    def test_masking_across_pieces(self):
        m = E._Masker([b"SECRET"])
        out = m.feed(b"abc SEC") + m.feed(b"RET def") + m.feed(b"", last=True)
        self.assertEqual(out, b"abc ****** def")
        guesses = []
        for guess in (b"SECRQ", b"SECRE", b"QQQQQ", b"S"):  # how much comes out never depends on a right guess
            m = E._Masker([b"SECRET"])
            guesses.append(len(m.feed(b"0123456789" + guess)))
        self.assertEqual(guesses, [10, 10, 10, 6])

    def test_a_huge_chunk_is_passed_on_as_it_comes(self):
        class Trickle:
            def __init__(self, parts):
                self.parts = list(parts)

            def recv(self, n):
                return self.parts.pop(0) if self.parts else b""
        body = E.Proxy._body(Trickle([b"x" * 1000, b"y" * 1000]), b"ffffffff\r\n", ["chunked"], [])
        self.assertEqual(next(body), b"x" * 1000)  # not held until the 4 GiB chunk is whole
        self.assertEqual(next(body), b"y" * 1000)
        with self.assertRaises(ValueError):
            next(body)  # and it ended early

    def test_a_trickled_push_is_read_only_a_few_times(self):
        z, o = "0" * 40, "1" * 40
        line = f"{z} {o} refs/heads/fix/x\0report-status".encode()
        data = (b"%04x" % (len(line) + 4) + line) * 200 + b"0000PACK"
        calls = []
        real = E.receive_pack_refs
        with unittest.mock.patch.object(E, "receive_pack_refs", lambda d: calls.append(1) or real(d)):
            body, refused, _, _ = E.Proxy._check_push(iter([data[i:i + 1] for i in range(len(data))]), RULE, set())
            self.assertIsNone(refused)
            self.assertEqual(b"".join(body), data)
        self.assertLess(len(calls), 30)

    def test_reading_push_commands(self):
        z, o = "0" * 40, "1" * 40
        line = f"{z} {o} refs/heads/agent/x\0report-status".encode()
        data = b"%04x" % (len(line) + 4) + line
        self.assertIsNone(E.receive_pack_refs(data))  # no flush yet
        shallow = f"shallow {o}".encode()
        data = b"%04x" % (len(shallow) + 4) + shallow + data + b"0000PACK"
        self.assertEqual(E.receive_pack_refs(data), ["refs/heads/agent/x"])
        for bad in (b"0x10" + b"x" * 12, b"0003", b"0010not a command" + b"0000"):
            with self.assertRaises(ValueError, msg=bad):
                E.receive_pack_refs(bad)


if __name__ == "__main__":
    unittest.main()
