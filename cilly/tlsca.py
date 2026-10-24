"""A certificate authority for one VM start, so the egress proxy can put real tokens in for stand-ins.

With "Keep tokens on this PC", a VM never holds a real token, only a stand-in.  The egress proxy
opens the HTTPS connections to the few hosts that need a token, swaps the stand-in for the real
token, and sends the request on over a checked TLS connection to the real host.  For that, the
VM trusts a certificate authority made for this one start:

- It may only vouch for the token hosts (an X.509 name constraint: those names and their
  subdomains), so even inside the VM it can't stand in for any other site.
- It signs one certificate per token host at start, and then its key is deleted.  Nothing can
  make another certificate, not even on this PC.
- The host keys live in a private folder in memory, outside QEMU's sandbox, and go when the
  agent stops.

openssl, which nearly every Linux has, makes the keys and certificates.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess

DAYS = 30  # the VM is started again long before this
HOST_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def openssl_binary() -> str | None:
    return shutil.which("openssl")


def check_host(host: str) -> str:
    if not isinstance(host, str) or not HOST_RE.fullmatch(host):
        raise ValueError(f"not a host name cilly makes a certificate for: {str(host)[:60]!r}")
    return host


def _run(argv: list[str]) -> None:
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    if r.returncode:
        raise RuntimeError(f"openssl failed: {r.stderr.decode(errors='replace').strip()[-300:]}")


def _ca(folder: str, hosts: list[str], openssl: str) -> tuple[str, str]:
    key, crt = os.path.join(folder, "ca.key"), os.path.join(folder, "ca.crt")
    permitted = ",".join(f"permitted;DNS:{check_host(h)}" for h in hosts)
    _run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
          "-keyout", key, "-out", crt, "-days", str(DAYS), "-subj", "/CN=cilly agent VM",
          "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
          "-addext", "keyUsage=critical,keyCertSign",
          "-addext", f"nameConstraints=critical,{permitted}"])
    os.chmod(key, 0o600)
    return key, crt


def _leaf(folder: str, host: str, ca_key: str, ca_crt: str, openssl: str) -> tuple[str, str]:
    check_host(host)
    key, csr, crt, ext = (os.path.join(folder, f"{host}.{x}") for x in ("key", "csr", "crt", "ext"))
    _run([openssl, "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
          "-keyout", key, "-out", csr, "-subj", f"/CN={host}"])
    os.chmod(key, 0o600)
    with open(ext, "w") as f:
        f.write(f"subjectAltName=DNS:{host}\nbasicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n")
    _run([openssl, "x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key, "-out", crt, "-days", str(DAYS),
          "-set_serial", "0x" + secrets.token_hex(16), "-extfile", ext])
    os.unlink(csr)
    os.unlink(ext)
    return crt, key


def make(folder: str, hosts: list[str]) -> dict:
    """A new CA for these hosts and a certificate for each, in `folder` (made private).  Returns
    {"ca": the CA certificate (PEM, for the VM), "hosts": {host: {"cert": path, "key": path}}}."""
    openssl = openssl_binary()
    if not openssl:
        raise RuntimeError("openssl isn't installed, and keeping tokens on this PC needs it")
    hosts = sorted({check_host(h) for h in hosts})
    if not hosts:
        raise ValueError("no hosts")
    remove(folder)
    os.makedirs(folder, mode=0o700)
    ca_key, ca_crt = _ca(folder, hosts, openssl)
    try:
        out = {"hosts": {}}
        for h in hosts:
            crt, key = _leaf(folder, h, ca_key, ca_crt, openssl)
            out["hosts"][h] = {"cert": crt, "key": key}
    finally:
        os.unlink(ca_key)  # nothing can make another certificate now
    with open(ca_crt) as f:
        out["ca"] = f.read()
    return out


def remove(folder: str) -> None:
    shutil.rmtree(folder, ignore_errors=True)
