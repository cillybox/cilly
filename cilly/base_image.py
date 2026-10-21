"""Build the clean, read-only base image.

You download and verify a Debian 13 genericcloud qcow2 yourself; this module:

1. creates a temporary overlay on top of it,
2. writes a cloud-init NoCloud seed ISO (xorriso) that installs git, node,
   the agent CLIs, the guest runner, an unprivileged `agent` user, disables
   SSH and cloud-init, then powers off,
3. boots it once with the same restricted network + allowlisting proxy,
4. flattens the result into images/base-<timestamp>.qcow2 (mode 0444).
"""

from __future__ import annotations

import base64
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import config as C
from .vm import (GUEST_DIR, PKG_DIR, VMError, VMPaths, _connect_unix, _LogSink, _read_into, _wait_for, sandbox_python,
                 bwrap_argv, bwrap_decision, images_dir, qemu_argv, which)

PROXY_HOST, PROXY_PORT = "10.0.2.100", 3128  # the egress proxy, as the VM sees it
PROXY = f"http://{PROXY_HOST}:{PROXY_PORT}"


ENV_FILE = "/etc/cilly/toolchains.env"  # read by the runner: JAVA_HOME, RUSTUP_HOME, extra PATH entries, ...
PLAYWRIGHT = "1.63.0"  # pinned; its Chromium build comes with it
VINEFLOWER = "1.12.0"
CFR = "0.152"
GHIDRA = "12.1.4"  # REA supports Ghidra 12.1.x
REA = "6.5.0"      # rea-agents, with its dependencies pinned in cilly/guest/rea/package-lock.json
NODE = "24.22.0"   # for REA, which needs 22.19+ (Debian has 20.19); 24 for its SQLite analysis
RIZIN = "0.9.1"

# Toolchains you can add to an image (`build-base --with java,rust`).  Each is a
# fixed recipe: Debian packages, or for Rust the official rustup with its
# checksum.  They run as root during the build only; agents never get root.
# needs: toolchains built first.  downloads: fixed versions, each checked against
# its SHA-256 before use.  files: programs from cilly/guest/ put into the image.
# optional: large, so left out of the full preset.
RECIPES = {
    "java": {
        "size_mb": 350,  # about this much more on disk, for Settings → VM images
        "label": "Java 25",
        "about": "OpenJDK 25 (Debian) and Maven, both set to use the egress proxy. Gradle projects use their own wrapper.",
        "apt": ["openjdk-25-jdk-headless", "maven"],
        "script": f"""javac -version 2>&1 | grep -q '^javac 25' || {{ echo "cilly: javac is not version 25"; exit 1; }}
jh=$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")
echo "JAVA_HOME=$jh" >> {ENV_FILE}
# Java ignores HTTPS_PROXY: every JVM (Gradle too) reads the egress proxy from here, and Maven from its settings
printf '%s\\n' http.proxyHost={PROXY_HOST} http.proxyPort={PROXY_PORT} https.proxyHost={PROXY_HOST} https.proxyPort={PROXY_PORT} >> "$jh/conf/net.properties"
printf '%s\\n' '<settings><proxies>' \\
  '<proxy><id>cilly-https</id><active>true</active><protocol>https</protocol><host>{PROXY_HOST}</host><port>{PROXY_PORT}</port></proxy>' \\
  '<proxy><id>cilly-http</id><active>true</active><protocol>http</protocol><host>{PROXY_HOST}</host><port>{PROXY_PORT}</port></proxy>' \\
  '</proxies></settings>' > /etc/maven/settings.xml
""",
        "check": ["java -version", "mvn -v"],
        "build_hosts": [],
    },
    "rust": {
        "size_mb": 1300,  # about this much more on disk, for Settings → VM images
        "label": "Rust",
        "about": "Stable Rust with cargo, clippy and rustfmt (rustup, system-wide in /opt/rust).",
        "apt": [],
        "script": f"""install -d -m 0755 /opt/rust
d=$(mktemp -d) && cd "$d"
u=https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init
curl -fsSL --proto '=https' "$u" -o rustup-init
curl -fsSL --proto '=https' "$u.sha256" -o rustup-init.sha256
echo "$(cut -d' ' -f1 rustup-init.sha256)  rustup-init" | sha256sum -c -
chmod +x rustup-init
export RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo
./rustup-init -y --no-modify-path --profile minimal --default-toolchain stable -c clippy -c rustfmt
unset RUSTUP_HOME CARGO_HOME
cd / && rm -rf "$d"
chmod -R a+rX /opt/rust
echo "RUSTUP_HOME=/opt/rust/rustup" >> {ENV_FILE}
echo "PATH=/opt/rust/cargo/bin" >> {ENV_FILE}
""",
        "check": ["RUSTUP_HOME=/opt/rust/rustup /opt/rust/cargo/bin/cargo --version"],
        "build_hosts": ["static.rust-lang.org"],
    },
    "cpp": {
        "size_mb": 600,  # about this much more on disk, for Settings → VM images
        "label": "C and C++ extras",
        "about": "CMake, Ninja, Clang, clangd, clang-format and gdb (gcc, g++ and make are always there).",
        "apt": ["cmake", "ninja-build", "clang", "clangd", "clang-format", "gdb", "pkg-config"],
        "script": "",
        "check": ["cmake --version", "clang --version"],
        "build_hosts": [],
    },
    "python": {
        "size_mb": 80,  # about this much more on disk, for Settings → VM images
        "label": "Python extras",
        "about": "venv, pip, pipx and headers for building packages (Python 3 itself is always there).",
        "apt": ["python3-venv", "python3-pip", "python3-dev", "pipx"],
        "script": "",
        "check": ["python3 -m venv --help"],
        "build_hosts": [],
    },
    "browser": {
        "size_mb": 700,  # about this much more on disk, for Settings → VM images
        "label": "Browser",
        "about": f"Headless Chromium with Playwright {PLAYWRIGHT}, so agents can open pages and take screenshots.",
        "apt": [],
        # Playwright installs the Chromium build it was tested with, and the system libraries it needs (apt).
        "script": f"""export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
npm install -g --no-audit --no-fund playwright@{PLAYWRIGHT}
playwright install --with-deps --only-shell chromium
chmod -R a+rX /opt/ms-playwright
echo "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" >> {ENV_FILE}
echo "NODE_PATH=$(npm root -g)" >> {ENV_FILE}
printf '<p>cilly</p>' > /tmp/pw-check.html
""",
        "check": ["PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright playwright screenshot file:///tmp/pw-check.html /tmp/pw-check.png",
                  "test -s /tmp/pw-check.png", "rm -f /tmp/pw-check.html /tmp/pw-check.png"],
        # cdn.playwright.dev forwards the Chromium download to Google's storage (Chrome for Testing builds)
        "build_hosts": ["cdn.playwright.dev", "playwright.download.prss.microsoft.com", "storage.googleapis.com"],
    },
    "go": {
        "size_mb": 450,  # about this much more on disk, for Settings → VM images
        "label": "Go",
        "about": "Go from Debian.",
        "apt": ["golang-go"],
        "script": "",
        "check": ["go version"],
        "build_hosts": [],
    },
    "decompilers": {
        "size_mb": 15,  # about this much more on disk, for Settings → VM images
        "label": "Decompilers",
        "about": f"Vineflower {VINEFLOWER} and CFR {CFR} for Java jars and class files, jar-diff to compare two versions "
                 "of a plugin, and unzip, zip, file, xxd and binutils.",
        "needs": ["java"],
        "apt": ["unzip", "zip", "file", "xxd", "binutils", "python3-yaml"],
        "downloads": [{"to": "/opt/decompilers/vineflower.jar", "version": VINEFLOWER,
                       "url": f"https://repo1.maven.org/maven2/org/vineflower/vineflower/{VINEFLOWER}/vineflower-{VINEFLOWER}.jar",
                       "sha256": "1dfcfe974395734fa467ce620661c7623d05ba83670de0529b1fbd63ff548b9d"},
                      {"to": "/opt/decompilers/cfr.jar", "version": CFR,
                       "url": f"https://repo1.maven.org/maven2/org/benf/cfr/{CFR}/cfr-{CFR}.jar",
                       "sha256": "f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2"}],
        "files": {"/opt/decompilers/jar-diff": "jar-diff.py"},
        "script": """cat > /usr/local/bin/vineflower <<'END'
#!/bin/sh
# vineflower <jar or class> <outdir> [options]: Java sources, one per class.  Safe: it decompiles a jar
# rebuilt from the checked .class files, so a ../ or absolute entry name can't write outside <outdir>.
exec /opt/decompilers/jar-diff --decompile vineflower "$@"
END
cat > /usr/local/bin/cfr <<'END'
#!/bin/sh
# cfr <jar or class> <outdir> [options]: Java sources, the second opinion when Vineflower can't (safe too)
exec /opt/decompilers/jar-diff --decompile cfr "$@"
END
chmod 0755 /usr/local/bin/vineflower /usr/local/bin/cfr
ln -sf /opt/decompilers/jar-diff /usr/local/bin/jar-diff
chmod -R a+rX /opt/decompilers
d=/tmp/cilly-decompilers && mkdir -p $d/v1 $d/v2
printf 'public class Hello {\\n  public static String greet() { return "Hello"; }\\n}\\n' > $d/v1/Hello.java
printf 'public class Hello {\\n  public static String greet() { return "Hello, " + System.getProperty("user.name"); }\\n}\\n' > $d/v2/Hello.java
printf 'messages:\\n  no-permission: "Not allowed."\\n' > $d/v1/messages.yml
printf 'messages:\\n  errors:\\n    no-permission: "Not allowed."\\n' > $d/v2/messages.yml
for v in v1 v2; do (cd $d/$v && javac --release 17 Hello.java && zip -q ../$v.jar Hello.class messages.yml); done
""",
        "check": ["vineflower /tmp/cilly-decompilers/v1.jar /tmp/cilly-decompilers/vf -s",
                  "grep -q 'return \"Hello\";' /tmp/cilly-decompilers/vf/Hello.java",
                  "cfr /tmp/cilly-decompilers/v1.jar /tmp/cilly-decompilers/cfr --silent true",
                  "grep -q 'return \"Hello\";' /tmp/cilly-decompilers/cfr/Hello.java",
                  "jar-diff /tmp/cilly-decompilers/v1.jar /tmp/cilly-decompilers/v2.jar /tmp/cilly-decompilers/diff",
                  "grep -q 'renamed  messages.no-permission  ->  messages.errors.no-permission' /tmp/cilly-decompilers/diff/summary.txt",
                  "grep -A1 '^Changed classes' /tmp/cilly-decompilers/diff/summary.txt | grep -qx '  Hello'",
                  "rm -rf /tmp/cilly-decompilers"],
        "build_hosts": ["repo1.maven.org"],
    },
    "ghidra": {
        "size_mb": 900,  # about this much more on disk, for Settings → VM images
        "label": "Ghidra",
        "about": f"Ghidra {GHIDRA} for native programs, with ghidra-decompile to export decompiled C headless. "
                 "Agents that use it need 6 to 8 GB of memory.",
        "needs": ["java"],
        "optional": True,
        "apt": ["unzip"],
        "downloads": [{"to": "/opt/ghidra.zip", "version": GHIDRA,
                       "url": f"https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_{GHIDRA}_build/"
                              f"ghidra_{GHIDRA}_PUBLIC_20260921.zip",
                       "sha256": "ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db"}],
        "files": {"/opt/ghidra-scripts/ExportDecompiled.java": "ExportDecompiled.java",
                  "/opt/ghidra-scripts/ghidra-decompile": "ghidra-decompile"},
        "script": f"""unzip -q /opt/ghidra.zip -d /opt && rm /opt/ghidra.zip
mv /opt/ghidra_{GHIDRA}_PUBLIC /opt/ghidra
chmod -R a+rX,go-w /opt/ghidra /opt/ghidra-scripts
ln -sf /opt/ghidra-scripts/ghidra-decompile /usr/local/bin/ghidra-decompile
echo "GHIDRA_INSTALL_DIR=/opt/ghidra" >> {ENV_FILE}
d=/tmp/cilly-ghidra && mkdir -p $d
printf 'int cilly_answer(int x) {{ return x * 6 + 7; }}\\nint main(void) {{ return cilly_answer(5); }}\\n' > $d/answer.c
gcc -O0 -o $d/answer $d/answer.c
""",
        "check": ["HOME=/tmp/cilly-ghidra ghidra-decompile /tmp/cilly-ghidra/answer /tmp/cilly-ghidra/out",
                  "grep -q 'cilly_answer' /tmp/cilly-ghidra/out/answer.c",
                  "rm -rf /tmp/cilly-ghidra"],
        # github.com forwards release downloads to release-assets.githubusercontent.com
        "build_hosts": ["github.com", "release-assets.githubusercontent.com"],
    },
    "rea": {
        "size_mb": 1050,  # about this much more on disk, for Settings → VM images
        "label": "Reverse engineering",
        "about": f"REA {REA} as an MCP server and CLI, with rizin {RIZIN}, JADX, Apktool, TShark, GDB, binutils and pwntools. "
                 "It uses Ghidra too when that toolchain is in the image.",
        "needs": ["java"],
        "optional": True,  # large, so left out of the full preset, like Ghidra
        "apt": ["apktool", "tshark", "gdb", "binutils", "procps", "python3-venv"],
        "downloads": [
            # SHA-256 from nodejs.org's SHASUMS256.txt, whose signature is by the Node release key
            # C82FA3AE 1CBEDC6B E46B9360 C43CEC45 C17AB93C (Richard Lau, in nodejs/release-keys).
            {"to": "/opt/rea/node.tar.xz", "version": NODE, "url": f"https://nodejs.org/dist/v{NODE}/node-v{NODE}-linux-x64.tar.xz",
             "sha256": "cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008"},
            {"to": "/opt/rea/rizin.tar.xz", "version": RIZIN,
             "url": f"https://github.com/rizinorg/rizin/releases/download/v{RIZIN}/rizin-v{RIZIN}-static-x86_64.tar.xz",
             "sha256": "9102249a9f0b6319c5334a2e5cf8d9cc3f2035e1d3def027c41f6a90f647e8cf"},
            {"to": "/opt/rea/jadx-headless-mcp.jar", "version": "0.7.1",  # the JADX build REA's Android analysis uses
             "url": "https://github.com/1013503897/jadx-headless-mcp/releases/download/v0.7.1/jadx-headless-mcp-0.7.1-all.jar",
             "sha256": "6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75"},
        ],
        # rea-agents and its whole dependency tree, pinned by the lockfile; pwntools by wheel hashes
        "files": {"/opt/rea/app/package.json": "rea/package.json", "/opt/rea/app/package-lock.json": "rea/package-lock.json",
                  "/opt/rea/pwntools.txt": "rea/pwntools.txt", "/usr/local/bin/rea": "rea/rea"},
        # Node stays off PATH, so the agent CLI and the browser toolchain keep Debian's.  --ignore-scripts:
        # only frida has an install script, which would download binaries.  Of rizin's tools, which are
        # 58 MB each, only rizin (all REA uses), rz-bin and rz-diff are kept.
        "script": f"""install -d -m 0755 /opt/rea/node /opt/rizin
tar xJf /opt/rea/node.tar.xz -C /opt/rea/node --strip-components=1 && rm /opt/rea/node.tar.xz
tar xJf /opt/rea/rizin.tar.xz -C /opt/rizin bin/rizin bin/rz-bin bin/rz-diff share && rm /opt/rea/rizin.tar.xz
(cd /opt/rea/app && PATH=/opt/rea/node/bin:$PATH npm ci --ignore-scripts --no-audit --no-fund)
PATH=/opt/rea/node/bin:$PATH npm cache clean --force 2>/dev/null || true
python3 -m venv /opt/rea/pwntools
/opt/rea/pwntools/bin/pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r /opt/rea/pwntools.txt
printf '[update]\ninterval=never\n' > /etc/pwn.conf  # pwntools never checks for updates
chmod -R a+rX,go-w /opt/rea /opt/rizin
echo "PATH=/opt/rizin/bin" >> {ENV_FILE}
echo "REA_JADX_MCP_JAR=/opt/rea/jadx-headless-mcp.jar" >> {ENV_FILE}
echo "REA_PWNTOOLS_PYTHON=/opt/rea/pwntools/bin/python" >> {ENV_FILE}
echo "REA_TSHARK_COMMAND=/usr/bin/tshark" >> {ENV_FILE}
d=/tmp/cilly-rea && mkdir -p $d
printf 'int cilly_answer(int x) {{ return x * 6 + 7; }}\\nint main(void) {{ return cilly_answer(5); }}\\n' > $d/answer.c
gcc -O0 -o $d/answer $d/answer.c
""",
        "check": ["rea --help > /dev/null",
                  "rea inspect-with-objdump /tmp/cilly-rea/answer symbols --json | grep -q cilly_answer",
                  "PATH=/opt/rizin/bin:$PATH rea execute-rizin-command /tmp/cilly-rea/answer 'aa;afl' --json | grep -q sym.cilly_answer",
                  "/opt/rea/pwntools/bin/python -c 'import pwnlib.elf, elftools, unicorn'",
                  "rm -rf /tmp/cilly-rea"],
        # github.com forwards release downloads to release-assets.githubusercontent.com
        "build_hosts": ["nodejs.org", "registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "github.com",
                        "release-assets.githubusercontent.com"],
    },
}


BASE_MB = 1500  # about this much on disk for the basics every image has (Debian, Node, Claude Code)
IMAGE_LABEL_MAX = 40

# One-click defaults: everything for coding agents, nothing extra for research agents
# (they only need Claude and the web, so their image stays small).
PRESETS = {
    "full": {"label": "All-in-one", "about": "every toolchain except the large optional ones; the default for coding agents",
             "toolchains": [n for n, r in RECIPES.items() if not r.get("optional")],
             "default_for": "base_image"},
    "slim": {"label": "Slim", "about": "the basics only; the default for research agents", "toolchains": [],
             "default_for": "research_base_image"},
}


def check_toolchains(names) -> list:
    """The toolchains to build, each after the ones it needs."""
    names = list(dict.fromkeys(names or []))
    bad = [n for n in names if n not in RECIPES]
    if bad:
        raise ValueError(f"unknown toolchain {', '.join(bad)}. Choose from {', '.join(RECIPES)}.")
    out: list = []

    def add(n: str, chain: tuple) -> None:
        if n in out:
            return
        if n in chain:
            raise ValueError(f"toolchain {n} needs itself: {' -> '.join(chain + (n,))}")
        for dep in RECIPES[n].get("needs", []):
            add(dep, chain + (n,))  # a needed toolchain, and anything it needs, comes first
        out.append(n)

    for n in names:
        add(n, ())
    return out


def _file_mode(src: str) -> str:
    """A program from cilly/guest/ gets 0755, a data file 0644, by whether it starts with a shebang."""
    with open(os.path.join(GUEST_DIR, src), "rb") as f:
        return "0755" if f.read(2) == b"#!" else "0644"


def _download(d: dict) -> str:
    """Fetch one pinned file and check it before it lands where it's used.  to/url/sha256 are a fixed,
    safe character set (checked in the tests), so single quotes are enough to keep the shell literal."""
    return f"""install -d -m 0755 "$(dirname {d['to']})"
curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 '{d['url']}' -o {d['to']}.part
echo '{d['sha256']}  {d['to']}.part' | sha256sum -c -
mv {d['to']}.part {d['to']}
"""


def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def user_data(cfg: dict, toolchains=()) -> str:
    toolchains = check_toolchains(toolchains)
    pkgs = [a["npm_package"] for a in cfg["agents"].values() if a.get("npm_package")]
    for p in pkgs:
        if not all(c.isalnum() or c in "@/._-" for c in p):
            raise ValueError(f"suspicious npm package spec {p!r}")
    runner = _b64(os.path.join(GUEST_DIR, "cilly-runner.py"))
    unit = _b64(os.path.join(GUEST_DIR, "cilly-runner.service"))
    profile = base64.b64encode(
        f"export HTTPS_PROXY={PROXY} https_proxy={PROXY} HTTP_PROXY={PROXY} http_proxy={PROXY} TZ=UTC\n".encode()
    ).decode()
    log = "/dev/virtio-ports/org.cilly.log"
    npm = " ".join(pkgs)
    bins = sorted({a["argv"][0] for a in cfg["agents"].values() if a.get("argv")})
    for b in bins:
        if not all(c.isalnum() or c in "._-" for c in b):
            raise ValueError(f"suspicious agent binary name {b!r}")
    recipes = "\n".join(f"echo 'cilly: toolchain {t}'\n" + "".join(_download(d) for d in RECIPES[t].get("downloads", []))
                        + RECIPES[t]["script"]
                        + "".join(f"{c} || {{ echo 'cilly: toolchain {t} does not work'; exit 1; }}\n"
                                  for c in RECIPES[t]["check"]) for t in toolchains)
    finish = f"""#!/bin/bash
set -euo pipefail
exec > >(tee {log}) 2>&1
echo "cilly: installing agent CLIs: {npm}"
export HTTPS_PROXY={PROXY} https_proxy={PROXY}
npm install -g --no-audit --no-fund {npm}
echo "cilly: checking the image"
for t in git python3 node npm gh growpart {" ".join(bins)}; do
  command -v "$t" >/dev/null || {{ echo "cilly: missing tool: $t"; exit 1; }}
done
for t in {" ".join(bins)}; do
  "$t" --version || {{ echo "cilly: $t is installed but does not run"; exit 1; }}
done
install -d -m 0755 /etc/cilly
: > {ENV_FILE}
{recipes}
systemctl disable --now ssh.service ssh.socket 2>/dev/null || true
# Faster boots.  QEMU's user network always gives the VM 10.0.2.15, so it's set at once instead of waiting
# for DHCP.  No DNS server either: the one DHCP named never answers (the VM reaches out only through the
# proxy, which looks names up on the host), so lookups now fail at once instead of after a timeout.
# The runner waits for the network (its self-test needs it), but never longer than 20 s (the default was 2 min).
install -d /etc/systemd/network /etc/systemd/system/systemd-networkd-wait-online.service.d /etc/default/grub.d
cat > /etc/systemd/network/05-cilly.network <<'NET'
[Match]
Name=en* eth*

[Network]
Address=10.0.2.15/24
Gateway=10.0.2.2
DHCP=no
LinkLocalAddressing=no
IPv6AcceptRA=no
NET
cat > /etc/systemd/system/systemd-networkd-wait-online.service.d/cilly.conf <<'WAIT'
[Service]
ExecStart=
ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any --timeout=20
WAIT
# No boot menu to wait at, and no timers a VM without apt access or man pages needs
cat > /etc/default/grub.d/99-cilly.cfg <<'GRUB'
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB
if command -v update-grub >/dev/null; then update-grub; fi
# apt-listchanges fetches changelogs from metadata.ftp-master.debian.org, which no VM may reach
for timer in apt-daily apt-daily-upgrade apt-listchanges man-db e2scrub_all; do
  systemctl disable "$timer.timer" 2>/dev/null || true  # one at a time: a missing one doesn't stop the rest
done
systemctl mask unattended-upgrades.service 2>/dev/null || true
apt-get purge -y openssh-server 2>/dev/null || true
systemctl enable cilly-runner.service
ln -sf /usr/share/zoneinfo/UTC /etc/localtime
touch /etc/cloud/cloud-init.disabled
apt-get clean
# A smaller image, losing nothing an agent can use: apt's package lists (agents can't install packages,
# there's no root) and download caches go, then the space freed during the build is trimmed, so the
# copy of this disk leaves it out.  Man pages, docs and translations stay.
rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache /var/cache/apt/*.bin
fstrim -v / || echo "cilly: fstrim didn't run, so the image is bigger than it needs to be"
# No setuid/setgid programs: nothing in the VM switches user, and a bug in one
# (sudo, pkexec, ...) would be a way to root.  dbus-daemon-launch-helper stays:
# only the messagebus group can run it, and system D-Bus activation needs it.
keep=/usr/lib/dbus-1.0/dbus-daemon-launch-helper
find / -xdev -type f \\( -perm -4000 -o -perm -2000 \\) ! -path "$keep" -exec chmod ug-s {{}} +
left=$(find / -xdev -type f \\( -perm -4000 -o -perm -2000 \\) ! -path "$keep")
[ -z "$left" ] || {{ echo "cilly: setuid/setgid programs remain: $left"; exit 1; }}
# Nothing a toolchain installed may be world-writable: an agent could replace it and have it run next time.
if [ -d /opt ]; then
  ww=$(find /opt -xdev ! -type l -perm -0002)  # files and folders (a link's own mode means nothing)
  [ -z "$ww" ] || {{ echo "cilly: world-writable under /opt: $ww"; exit 1; }}
fi
echo CILLY-BUILD-OK
"""
    # cloud-config is YAML; JSON is valid YAML, which avoids hand-escaping.
    doc = {
        "hostname": "agent-vm",
        "fqdn": "agent-vm",
        "manage_etc_hosts": True,
        "ssh_pwauth": False,
        "disable_root": True,
        "users": [{"name": "agent", "shell": "/bin/bash", "lock_passwd": True}],
        "bootcmd": [
            ["sh", "-c", "sed -i 's#http://deb.debian.org#https://deb.debian.org#g; "
                         "s#http://security.debian.org#https://security.debian.org#g' "
                         "/etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true"],
        ],
        "apt": {
            "https_proxy": PROXY,
            "http_proxy": PROXY,
            # A dropped download is tried again at once, a few times, and a server that stops answering
            # fails after 30 s.  apt's delayed retry is off: it can loop forever on one package
            # (Debian bug #1111554), which held a build for its whole hour.
            "conf": 'Acquire::Retries "3";\nAcquire::Retries::Delay "false";\nAcquire::http::Timeout "30";\n',
            "primary": [{"arches": ["default"], "uri": "https://deb.debian.org/debian"}],
            "security": [{"arches": ["default"], "uri": "https://security.debian.org/debian-security"}],
        },
        "package_update": True,
        "package_upgrade": True,
        # cloud-guest-utils: growpart, so an agent's disk can be bigger than the image (the runner grows root at boot)
        "packages": ["git", "python3", "nodejs", "npm", "ca-certificates", "gh", "ripgrep",
                     "build-essential", "curl", "jq", "cloud-guest-utils"] + [p for t in toolchains for p in RECIPES[t]["apt"]],
        "write_files": [
            {"path": "/usr/local/sbin/cilly-runner", "encoding": "b64", "content": runner,
             "permissions": "0755", "owner": "root:root"},
            {"path": "/etc/systemd/system/cilly-runner.service", "encoding": "b64", "content": unit,
             "permissions": "0644", "owner": "root:root"},
            {"path": "/etc/profile.d/cilly-proxy.sh", "encoding": "b64", "content": profile,
             "permissions": "0644", "owner": "root:root"},
            # guest hardening: hide kernel pointers, limit ptrace to own children, no LLMNR/mDNS listeners
            {"path": "/etc/sysctl.d/90-cilly.conf", "permissions": "0644", "owner": "root:root",
             "content": "kernel.kptr_restrict = 2\nkernel.yama.ptrace_scope = 1\n"},
            # only root may schedule jobs: the agent can't leave respawning processes behind
            {"path": "/etc/cron.allow", "permissions": "0644", "owner": "root:root", "content": "root\n"},
            {"path": "/etc/at.allow", "permissions": "0644", "owner": "root:root", "content": "root\n"},
            {"path": "/etc/systemd/resolved.conf.d/90-cilly.conf", "permissions": "0644", "owner": "root:root",
             "content": "[Resolve]\nLLMNR=no\nMulticastDNS=no\nFallbackDNS=\n"},  # no built-in public servers to wait for
            {"path": "/usr/local/sbin/cilly-build-finish", "encoding": "b64",
             "content": base64.b64encode(finish.encode()).decode(), "permissions": "0700", "owner": "root:root"},
        ] + [{"path": dest, "encoding": "b64", "content": _b64(os.path.join(GUEST_DIR, src)), "permissions": _file_mode(src),
              "owner": "root:root"} for t in toolchains for dest, src in RECIPES[t].get("files", {}).items()],
        "runcmd": [
            # One fail-fast script: the build only counts as OK if every step
            # worked and every tool is actually present.
            ["bash", "/usr/local/sbin/cilly-build-finish"],
        ],
        "power_state": {"mode": "poweroff", "condition": True, "timeout": 30},
    }
    return "#cloud-config\n" + json.dumps(doc, indent=1) + "\n"


class AptWatch:
    """Notices apt's endless retry of one download in the build VM's console, so the build stops
    then instead of when its time runs out.  Fed the console's bytes in whatever pieces they come."""
    MARK = b"Tried to start delayed item"

    def __init__(self, limit: int = 20):
        self.limit, self.seen, self.tail = limit, 0, b""

    def feed(self, data: bytes) -> bool:
        """True once apt has said it `limit` times."""
        buf = self.tail + data
        self.seen += buf.count(self.MARK)
        cut = buf.rfind(self.MARK)
        rest = buf[cut + len(self.MARK):] if cut >= 0 else buf
        self.tail = rest[-(len(self.MARK) - 1):]  # the start of a mark split across two pieces
        return self.seen >= self.limit


APT_STUCK = ("apt got stuck retrying the same download: Debian's servers stopped answering, or the "
             "connection to them broke. Nothing was built. Try again in a few minutes.")


def _print_denials(path: str, out, qemu) -> None:
    """Surface blocked hosts while the build runs, so a stuck apt/npm is obvious."""
    seen, pos = set(), 0
    while qemu.poll() is None:
        time.sleep(2)
        try:
            with open(path) as f:
                f.seek(pos)
                lines = f.readlines()
                pos = f.tell()
        except FileNotFoundError:
            continue
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("action") == "deny" and e.get("host") and e["host"] not in seen:
                seen.add(e["host"])
                print(f"\n[cilly] egress DENIED: {e['host']}:{e.get('port')}; if the build needs it, add it to "
                      "build_egress_extra in the config and rebuild", file=out, flush=True)


def compression_options(tmpdir: str) -> list:
    """qemu-img options for a compressed image: zstd where this qemu-img has it (smaller and quicker to read
    than zlib), else zlib.  VMs read compressed images like any other; what they write goes to their own disk."""
    probe = os.path.join(tmpdir, "zstd-probe.qcow2")
    r = subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-o", "compression_type=zstd", probe, "1M"],
                       capture_output=True)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return ["-c", "-o", "compression_type=zstd"] if r.returncode == 0 else ["-c"]


def check_label(label) -> str:
    """A name you give an image, like Plugin RE: one short line of text."""
    label = " ".join(str(label or "").split())
    if len(label) > IMAGE_LABEL_MAX or any(not c.isprintable() for c in label):
        raise ValueError(f"an image's name is one line of up to {IMAGE_LABEL_MAX} characters")
    return label


def build(image: str, cfg: dict, *, timeout_min: int = 60, toolchains=(), preset: str = "", out=sys.stdout,
          route: dict | None = None, label: str = "") -> str:
    toolchains = check_toolchains(toolchains)
    label = check_label(label)
    image = os.path.abspath(image)
    if not os.path.isfile(image):
        raise SystemExit(f"image not found: {image}")
    for tool in ("qemu-img", cfg["vm"]["qemu_binary"], "xorriso"):
        if not which(tool):
            raise SystemExit(f"{tool} not found (see README: one-time setup)")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise SystemExit("/dev/kvm not accessible")
    info = json.loads(subprocess.run(["qemu-img", "info", "--output=json", image],
                                     check=True, capture_output=True).stdout)
    fmt = info.get("format")
    if fmt not in ("qcow2", "raw"):
        raise SystemExit(f"unexpected image format {fmt}")
    try:  # the same choice as an agent's start: inside bubblewrap when it's installed, or not at all
        bwrap, note = bwrap_decision(cfg["vm"]["use_bwrap"])
    except VMError as e:  # before any folder, disk or proxy exists, so a retry leaves nothing behind
        raise SystemExit(str(e)) from None

    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    work = os.path.join(images_dir(), f"build-{ts}")
    run = os.path.join(C.ensure_private_dir(C.runtime_dir()), f"build-{ts}")
    os.mkdir(work, 0o700)
    os.mkdir(run, 0o700)
    paths = VMPaths(work, run)
    os.mkdir(paths.sandbox_dir, 0o700)
    seed_dir = os.path.join(work, "seed")
    os.mkdir(seed_dir)
    with open(os.path.join(seed_dir, "user-data"), "w") as f:
        f.write(user_data(cfg, toolchains))
    with open(os.path.join(seed_dir, "meta-data"), "w") as f:
        f.write(f"instance-id: cilly-build-{ts}\nlocal-hostname: agent-vm\n")
    seed = paths.task_disk  # reuse the "task disk" slot for the seed ISO
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-output", seed, "-volid", "cidata",
                    "-joliet", "-rock", os.path.join(seed_dir, "user-data"), os.path.join(seed_dir, "meta-data")],
                   check=True)
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", fmt, "-b", image, paths.disk, "20G"],
                   check=True)

    allow = list(dict.fromkeys(cfg["egress"]["allow"] + cfg.get("build_egress_extra", [])
                               + [h for t in toolchains for h in RECIPES[t]["build_hosts"]]))
    # cilly's own downloads take the route when egress.route_own is on (in memory, like an agent's,
    # and like it out of the folders the build's QEMU sandbox can see)
    route_file = paths.route_file
    C.ensure_private_dir(os.path.dirname(route_file))
    fd = os.open(route_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(route or {"kind": "direct"}, f)
    proxy = subprocess.Popen([sys.executable, "-I", os.path.join(PKG_DIR, "egress_proxy.py"),
                              "--socket", paths.proxy_sock, "--log", paths.egress_log,
                              "--route-file", route_file, "--isolate", "cilly"]
                             + [x for h in allow for x in ("--allow", h)]
                             + [x for n in cfg["egress"]["never_reach"] for x in ("--never-reach", n)],
                             stdin=subprocess.DEVNULL)
    qemu = None
    try:
        _wait_for(paths.proxy_sock, 10, "egress proxy")
        print(note, file=out)
        argv = qemu_argv(qemu=which(cfg["vm"]["qemu_binary"]), name=f"build-{ts}", paths=paths,
                         cpus=cfg["vm"]["cpus"], memory_mb=max(2048, cfg["vm"]["memory_mb"]),
                         python=sandbox_python()[0] if bwrap else sys.executable,
                         cpu_model=cfg["vm"]["cpu_model"], nested_virt=cfg["vm"]["nested_virt"])
        if bwrap:
            argv = bwrap_argv(bwrap, argv, ro=[image, PKG_DIR] + sandbox_python()[1], rw=[paths.sandbox_dir, run])
        print(f"booting build VM ({work}) …", file=out)
        print("  console output follows; the apt steps can take 5-15 minutes.", file=out)
        print(f"  egress decisions: {paths.egress_log}", file=out)
        qemu = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        ok = threading.Event()
        sink = _LogSink(paths.agent_log)
        orig = sink.write

        text_of = codecs.getincrementaldecoder("utf-8")(errors="replace").decode  # a character can come in two pieces
        con_text_of = codecs.getincrementaldecoder("utf-8")(errors="replace").decode

        def tee(data: bytes):
            orig(data)
            out.write(text_of(data))
            out.flush()
            if b"CILLY-BUILD-OK" in data:
                ok.set()
        sink.write = tee
        threading.Thread(target=_read_into, args=(_connect_unix(paths.log_sock), sink), daemon=True).start()
        con = _LogSink(paths.console_log)
        con_write = con.write
        apt, stuck = AptWatch(), threading.Event()

        def con_tee(data: bytes):
            con_write(data)
            if apt.feed(data) and not stuck.is_set():
                stuck.set()
                qemu.kill()  # it would only repeat the same line until the time limit
            # the guest console is untrusted bytes: drop escape/control codes before printing
            text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]", "", con_text_of(data))
            out.write(text)
            out.flush()
        con.write = con_tee
        threading.Thread(target=_read_into, args=(_connect_unix(paths.console_sock), con), daemon=True).start()
        threading.Thread(target=_print_denials, args=(paths.egress_log, out, qemu), daemon=True).start()
        qemu.wait(timeout=timeout_min * 60)
    except subprocess.TimeoutExpired:
        qemu.kill()
        raise SystemExit(f"build timed out; logs in {work}")
    finally:
        proxy.terminate()
        if qemu and qemu.poll() is None:
            qemu.kill()
        try:
            proxy.wait(10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        shutil.rmtree(run, ignore_errors=True)  # only sockets: a failed build leaves none behind (its logs stay)
        os.unlink(route_file)
    if stuck.is_set():
        raise SystemExit(f"{APT_STUCK} Logs: {work}")
    if not ok.is_set():
        raise SystemExit(f"build did not finish cleanly; see {paths.agent_log}, {paths.console_log}, "
                         f"{paths.egress_log}")
    final = os.path.join(images_dir(), f"base-{ts}.qcow2")
    print("flattening image …", file=out)
    compress = compression_options(work)
    print(f"  compressed with {'zstd' if 'compression_type=zstd' in compress else 'zlib'}", file=out)
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", *compress, paths.disk, final], check=True)
    os.chmod(final, 0o444)
    with open(final[:-len(".qcow2")] + ".json", "w") as f:  # the panel shows which toolchains it has
        json.dump({"toolchains": toolchains, "preset": preset, "runner": "cilly", **({"label": label} if label else {}),
                   "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    shutil.rmtree(work)
    print(f"base image ready: {final}", file=out)
    return os.path.basename(final)
