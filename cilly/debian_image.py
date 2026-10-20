"""Download the Debian 13 cloud image that VM images are built from, and check it.

Only when you ask for it (Build image with no file given, or `build-base --download`).  The
image and Debian's SHA512SUMS come from https://cloud.debian.org, and redirects are followed
only to that same host.  The image is kept only when its SHA-512 matches the one in
SHA512SUMS, which checks that the download wasn't damaged.  Both files come from the same
server, so it doesn't prove who made the image.  It's the same check as `sha512sum -c
SHA512SUMS` in the README.  A copy that still matches isn't downloaded again.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import urllib.request
from urllib.parse import urlparse

from . import config as C

HOST = "cloud.debian.org"
BASE = f"https://{HOST}/images/cloud/trixie/latest/"
NAME = "debian-13-genericcloud-amd64.qcow2"
MAX_BYTES = 2 * 1024 ** 3   # the image is about 400 MB
MAX_SUMS = 256 * 1024
TIMEOUT = 60


def allowed(url: str) -> bool:
    """Only https://cloud.debian.org/, on its usual port."""
    try:
        u = urlparse(url)
        return u.scheme == "https" and u.hostname == HOST and u.port in (None, 443)
    except ValueError:
        return False


class _OnlyDebian(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https://cloud.debian.org/ address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise SystemExit(f"refused: {req.full_url} redirected away from https://{HOST}/")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req, timeout=TIMEOUT):
    return urllib.request.build_opener(_OnlyDebian).open(req, timeout=timeout)


def downloads_dir() -> str:
    return C.ensure_private_dir(os.path.join(C.data_dir(), "downloads"))


def image_path() -> str:
    return os.path.join(downloads_dir(), NAME)


def expected_sha512(sums: str, name: str = NAME) -> str:
    """The hash Debian's SHA512SUMS lists for `name` (lines are "<hash>  <file>")."""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{128}", parts[0]):
            return parts[0]
    raise SystemExit(f"Debian's SHA512SUMS doesn't list {name}")


def _open(url: str, urlopen):
    r = urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=TIMEOUT)  # generic
    if not allowed(r.geturl()):  # also checked at each redirect
        r.close()
        raise SystemExit(f"refused: {url} redirected away from https://{HOST}/")
    return r


def _sha512_of(path: str) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch(out=sys.stdout, urlopen=_urlopen, base: str = BASE, progress=None) -> str:
    """Download and check the image, and return its path.  Raises SystemExit with the reason.
    progress(done, total) is called as it downloads (total is 0 when the server doesn't say)."""
    try:
        with _open(base + "SHA512SUMS", urlopen) as r:
            sums = r.read(MAX_SUMS + 1)
    except OSError as e:
        raise SystemExit(f"Couldn't reach {HOST}: {e}") from None
    if len(sums) > MAX_SUMS:
        raise SystemExit("Debian's SHA512SUMS is unexpectedly large")
    want = expected_sha512(sums.decode("ascii", errors="replace"))
    dest = image_path()
    if os.path.isfile(dest) and _sha512_of(dest) == want:
        print(f"Debian cloud image: already downloaded, and it matches SHA512SUMS ({dest})", file=out)
        return dest
    # a new file of its own (0600), so nothing else writes into it; it's hashed as it is on disk
    fd, part = tempfile.mkstemp(prefix=NAME + ".", suffix=".part", dir=downloads_dir())
    print(f"downloading {base}{NAME}", file=out)
    try:
        with os.fdopen(fd, "wb") as f, _open(base + NAME, urlopen) as r:
            total = int(r.headers.get("Content-Length") or 0)
            if total > MAX_BYTES:
                raise SystemExit(f"the image is unexpectedly large ({total} bytes)")
            n, shown = 0, -1
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if n > MAX_BYTES:
                    raise SystemExit("the image is unexpectedly large")
                f.write(chunk)
                if progress:
                    progress(n, total)
                pct = n * 100 // total if total else -1
                if total and pct // 10 != shown // 10:
                    print(f"  {pct}% ({n // 1_000_000} of {total // 1_000_000} MB)", file=out)
                    shown = pct
        if total and n != total:
            raise SystemExit(f"the download stopped early ({n} of {total} bytes)")
        if _sha512_of(part) != want:
            raise SystemExit("The download doesn't match Debian's SHA512SUMS, so it was damaged and was deleted. "
                             "Try again later, or download and check it yourself.")
        os.replace(part, dest)
    except OSError as e:
        raise SystemExit(f"The download failed: {e}") from None
    finally:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
    print(f"Debian cloud image: downloaded, and it matches SHA512SUMS ({dest})", file=out)
    return dest
