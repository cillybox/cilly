"""Downloading Debian's cloud image (cilly/debian_image.py): kept only when it matches SHA512SUMS."""

import hashlib
import io
import os
import tempfile
import unittest
from unittest import mock

from cilly import debian_image as DI

IMAGE = b"QFI\xfb" + os.urandom(3_000_000)


class FakeResponse(io.BytesIO):
    def __init__(self, data, url, length=True):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data))} if length else {}

    def geturl(self):
        return self.url


class Fake:
    """Stands in for urllib's urlopen: serves SHA512SUMS and the image, and counts requests."""

    def __init__(self, image=IMAGE, sums_for=IMAGE, redirect=None):
        self.image, self.calls, self.redirect = image, [], redirect
        self.sums = (f"{'0' * 128}  debian-13-genericcloud-arm64.qcow2\n"
                     f"{hashlib.sha512(sums_for).hexdigest()}  {DI.NAME}\n").encode()

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url.rsplit("/", 1)[1])
        final = self.redirect or url
        return FakeResponse(self.sums if url.endswith("SHA512SUMS") else self.image, final)


class DebianImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.out = io.StringIO()

    def test_downloads_checks_and_reuses(self):
        fake = Fake()
        path = DI.fetch(out=self.out, urlopen=fake)
        self.assertEqual(path, DI.image_path())
        with open(path, "rb") as f:
            self.assertEqual(f.read(), IMAGE)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        self.assertEqual(fake.calls, ["SHA512SUMS", DI.NAME])
        self.assertIn("100%", self.out.getvalue())
        self.assertEqual(DI.fetch(out=self.out, urlopen=fake), path)  # still matches: not downloaded again
        self.assertEqual(fake.calls, ["SHA512SUMS", DI.NAME, "SHA512SUMS"])
        self.assertFalse(os.path.exists(path + ".part"))

    def test_a_mismatch_keeps_nothing(self):
        fake = Fake(image=IMAGE[:-1] + bytes([IMAGE[-1] ^ 1]))  # always one bit off: a random last byte could be "x" already
        with self.assertRaises(SystemExit) as cm:
            DI.fetch(out=self.out, urlopen=fake)
        self.assertIn("doesn't match Debian's SHA512SUMS, so it was damaged", str(cm.exception))
        self.assertEqual(os.listdir(DI.downloads_dir()), [])

    def test_a_newer_image_replaces_the_old_one(self):
        DI.fetch(out=self.out, urlopen=Fake())
        newer = b"QFI\xfb" + os.urandom(1000)
        DI.fetch(out=self.out, urlopen=Fake(image=newer, sums_for=newer))
        with open(DI.image_path(), "rb") as f:
            self.assertEqual(f.read(), newer)

    def test_the_file_is_its_own_temp_file_hashed_from_disk(self):
        hashed, seen = [], []
        real = DI._sha512_of

        def spy(path):
            hashed.append(path)
            return real(path)
        with mock.patch.object(DI, "_sha512_of", side_effect=spy):
            DI.fetch(out=self.out, urlopen=Fake(), progress=lambda done, total: seen.append((done, total)))
        part = hashed[0]
        self.assertEqual(os.path.dirname(part), DI.downloads_dir())
        self.assertTrue(os.path.basename(part).startswith(DI.NAME + ".") and part.endswith(".part"), part)
        self.assertNotEqual(part, DI.image_path() + ".part")  # unique, so two downloads never share one
        self.assertEqual(seen[-1], (len(IMAGE), len(IMAGE)))
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_only_cloud_debian_org_over_https(self):
        for url in ("http://cloud.debian.org/x", "https://mirror.example/debian.qcow2", "https://cloud.debian.org.evil.example/",
                    "https://cloud.debian.org:8443/x", "https://user@evil.example/"):
            self.assertFalse(DI.allowed(url), url)
            with self.assertRaises(SystemExit) as cm:
                DI.fetch(out=self.out, urlopen=Fake(redirect=url))
            self.assertIn("redirected away from https://cloud.debian.org/", str(cm.exception))
        self.assertTrue(DI.allowed("https://cloud.debian.org/images/cloud/trixie/20261001-1/x.qcow2"))
        handler = DI._OnlyDebian()
        req = DI.urllib.request.Request(DI.BASE + "SHA512SUMS")
        self.assertEqual(handler.redirect_request(req, None, 302, "Found", {}, "https://cloud.debian.org/y").full_url,
                         "https://cloud.debian.org/y")
        with self.assertRaises(SystemExit):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/y")

    def test_the_real_opener_refuses_a_redirect_elsewhere(self):
        import http.server
        import threading

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://evil.example/debian.qcow2")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(SystemExit) as cm:
            DI._urlopen(DI.urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/SHA512SUMS"), timeout=5)
        self.assertIn("redirected away from https://cloud.debian.org/", str(cm.exception))

    def test_refusals(self):
        big = Fake()
        with mock.patch.object(DI, "MAX_BYTES", 1000), self.assertRaises(SystemExit) as cm:
            DI.fetch(out=self.out, urlopen=big)
        self.assertIn("unexpectedly large", str(cm.exception))
        self.assertEqual(os.listdir(DI.downloads_dir()), [])
        with self.assertRaises(SystemExit) as cm:
            DI.fetch(out=self.out, urlopen=mock.Mock(side_effect=OSError("no network")))
        self.assertIn("Couldn't reach cloud.debian.org", str(cm.exception))

    def test_sums_parsing(self):
        h = "a" * 128
        self.assertEqual(DI.expected_sha512(f"{h} *{DI.NAME}\n"), h)
        for bad in ("", f"{h}  {DI.NAME}.tar.xz\n", f"{'z' * 128}  {DI.NAME}\n", f"{h[:-1]}  {DI.NAME}\n"):
            with self.assertRaises(SystemExit):
                DI.expected_sha512(bad)
        self.assertTrue(DI.BASE.startswith("https://cloud.debian.org/"))


class CommandLineTests(unittest.TestCase):
    def test_build_base_takes_a_file_or_downloads(self):
        from cilly.__main__ import parser
        a = parser().parse_args(["build-base", "--download", "--preset", "full"])
        self.assertEqual((a.download, a.image), (True, None))
        a = parser().parse_args(["build-base", "--image", "/x.qcow2"])
        self.assertEqual((a.download, a.image), (False, "/x.qcow2"))
        with mock.patch("sys.stderr", io.StringIO()):
            for argv in (["build-base"], ["build-base", "--image", "/x.qcow2", "--download"]):
                with self.assertRaises(SystemExit):
                    parser().parse_args(argv)


if __name__ == "__main__":
    unittest.main()
