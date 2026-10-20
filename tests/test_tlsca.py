"""The CA made for one VM start: it vouches only for the token hosts, and its key is gone once the
host certificates are made."""

import os
import socket
import ssl
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import tlsca  # noqa: E402

HOSTS = ["api.anthropic.com", "github.com", "api.github.com"]


def handshake(cert, key, cafile, name) -> str:
    """A TLS handshake with a server holding cert/key, from a client that trusts only cafile."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert, key)

    def serve():
        c, _ = srv.accept()
        try:
            sctx.wrap_socket(c, server_side=True).close()
        except (ssl.SSLError, OSError):
            c.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cctx = ssl.create_default_context(cafile=cafile)
    try:
        with cctx.wrap_socket(socket.create_connection(srv.getsockname()), server_hostname=name):
            return "ok"
    except ssl.SSLCertVerificationError as e:
        return e.verify_message
    finally:
        t.join(5)
        srv.close()


@unittest.skipUnless(tlsca.openssl_binary(), "needs openssl")
class CaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "tls")

    def test_a_ca_for_the_token_hosts_only(self):
        out = tlsca.make(self.dir, HOSTS)
        self.assertEqual(sorted(out["hosts"]), sorted(HOSTS))
        self.assertTrue(out["ca"].startswith("-----BEGIN CERTIFICATE-----"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ca.key")))  # gone: nothing can sign again
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for h in HOSTS:
            self.assertEqual(stat.S_IMODE(os.stat(out["hosts"][h]["key"]).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["ca.crt"] + [f"{h}.{x}" for h in HOSTS for x in ("crt", "key")]))
        ca = os.path.join(self.dir, "ca.crt")
        gh = out["hosts"]["api.github.com"]
        self.assertEqual(handshake(gh["cert"], gh["key"], ca, "api.github.com"), "ok")
        self.assertIn("mismatch", handshake(gh["cert"], gh["key"], ca, "example.com"))
        self.assertIn("local issuer", handshake(gh["cert"], gh["key"], None, "api.github.com"))  # trusted only where it's added

    def test_it_cant_vouch_for_another_site(self):
        """Even a certificate it signed for another name fails: the name constraint."""
        openssl = tlsca.openssl_binary()
        os.makedirs(self.dir, mode=0o700)
        key, crt = tlsca._ca(self.dir, ["github.com"], openssl)
        cert, k = tlsca._leaf(self.dir, "example.org", key, crt, openssl)
        self.assertNotEqual(handshake(cert, k, crt, "example.org"), "ok")
        cert, k = tlsca._leaf(self.dir, "api.github.com", key, crt, openssl)  # a subdomain is within it
        self.assertEqual(handshake(cert, k, crt, "api.github.com"), "ok")

    def test_names_it_refuses(self):
        for bad in ("-subj", "github.com -x", "*.github.com", "localhost", "GitHub.com", "a..b", "x" * 300 + ".com", ""):
            with self.assertRaises(ValueError, msg=bad):
                tlsca.make(self.dir, [bad])

    def test_a_new_start_starts_clean(self):
        tlsca.make(self.dir, ["github.com"])
        out = tlsca.make(self.dir, ["api.anthropic.com"])
        self.assertEqual(sorted(os.listdir(self.dir)), ["api.anthropic.com.crt", "api.anthropic.com.key", "ca.crt"])
        self.assertEqual(list(out["hosts"]), ["api.anthropic.com"])
        tlsca.remove(self.dir)
        self.assertFalse(os.path.exists(self.dir))


if __name__ == "__main__":
    unittest.main()
