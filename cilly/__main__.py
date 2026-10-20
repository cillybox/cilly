"""Command line: `cilly` when installed, or `python3 -m cilly` from its folder.

setup | serve | open | doctor | build-base | secrets | service | init-config
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config as C


def doctor() -> int:
    from . import host as H
    from .vm import detect_tools, list_base_images, virtiofsd_supports_readonly
    ok = True
    if os.geteuid() == 0:
        print("✗ running as root: cilly must run as your normal user")
        ok = False
    try:
        cfg = C.load()
        print(f"✓ config: {C.config_path()} ({'exists' if os.path.exists(C.config_path()) else 'defaults'})")
    except Exception as e:  # noqa: BLE001
        print(f"✗ config invalid: {e}")
        return 1
    try:
        print(f"✓ runtime dir: {C.runtime_dir()}")
    except RuntimeError as e:
        print(f"✗ {e}")
        ok = False
    t = detect_tools(cfg)
    info = H.info(t)
    print(f"  host: {info['distro']}")
    for k, need in (("qemu", True), ("qemu-img", True), ("kvm", True), ("bwrap", False),
                    ("xorriso", False), ("virtiofsd", False)):
        mark = "✓" if t[k] else ("✗" if need else "·")
        ok &= bool(t[k]) or not need
        extra = {"bwrap": "optional, recommended (QEMU runs without network namespace access)",
                 "xorriso": "needed only for build-base", "virtiofsd": "needed only for live shares"}.get(k, "")
        if k == "bwrap" and t["bwrap_problem"]:  # installed but blocked: VMs don't start until it's fixed
            ok = False
            print(f"✗ bwrap: {H.bwrap_refusal(t['bwrap_problem'])}")
            continue
        if k == "bwrap" and t["use_bwrap"] == "off":
            print("· bwrap: not used (vm.use_bwrap is off)")
            continue
        print(f"{mark} {k}: {t[k] or 'missing'} {extra}")
    if t["virtiofsd"]:
        print(f"  virtiofsd --readonly supported: {virtiofsd_supports_readonly(t['virtiofsd'])}")
    imgs = list_base_images()
    print(f"{'✓' if imgs else '✗'} base images: {', '.join(imgs) or f'none (run {H.cli()} build-base --download)'}")
    ok &= bool(imgs)
    print(f"  egress allowlist: {', '.join(cfg['egress']['allow'])}")
    print(f"  repos: {', '.join(C.repos(cfg)) or 'none configured'}")
    root_note(info)
    return 0 if ok else 1


def root_note(info: dict) -> None:
    """What install.sh would do here, where it helps (host.info decides)."""
    if info["unsupported"]:
        print(f"\n{info['unsupported']}")
    elif info["install_sh"]:
        print(f"\nSome one-time steps need root. Read the script first, then run: {info['install_sh']}")


def setup(port: int | None, open_browser: bool) -> int:
    """setup: everything that doesn't need root, then the panel, where the rest happens."""
    from . import host as H
    from .server import serve
    from .vm import detect_tools
    if not os.path.exists(C.config_path()):
        C.save(C.load())
        print(f"Wrote {C.config_path()}. It holds no secrets.")
    t = detect_tools(C.load())
    info = H.info(t)
    print(f"cilly on {info['distro']}")
    root_note(info)  # the setup page lists it too
    serve(port, open_browser=open_browser)
    return 0


def open_panel(print_link: bool = False) -> int:
    """open: the running panel, in your browser, or its link."""
    from . import host as H
    from . import launch as L
    url = L.read_link()
    if not url:
        print(f"cilly isn't running. Start it with `{H.cli()} setup` or `{H.cli()} serve`.", file=sys.stderr)
        return 1
    if print_link:
        print(url)
        return 0
    why = L.open_in_browser(url)
    if why:
        print(f"Open this link in your browser ({why}):\n{url}")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cilly")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("setup", help="get ready and open the panel in your browser, where setup continues")
    st.add_argument("--port", type=int)
    st.add_argument("--no-browser", action="store_true", help="only print the link")
    s = sub.add_parser("serve", help="run the web panel on 127.0.0.1")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open it in your browser")
    op = sub.add_parser("open", help="open the running panel in your browser")
    op.add_argument("--print", dest="print_link", action="store_true",
                    help="print its link (with this start's token) instead, to paste it somewhere")
    sv = sub.add_parser("service", help="start cilly when you log in (a systemd user service)")
    sv.add_argument("action", choices=["install", "remove", "status"])
    sub.add_parser("doctor", help="check prerequisites")
    b = sub.add_parser("build-base", help="build a clean base image from a Debian cloud qcow2")
    src = b.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="verified debian-13-genericcloud-amd64.qcow2")
    src.add_argument("--download", action="store_true",
                     help="download it from cloud.debian.org and check it against Debian's SHA512SUMS")
    b.add_argument("--timeout-min", type=int, default=60)
    b.add_argument("--with", dest="toolchains", default="",
                   help="toolchains to add, comma-separated; with --preset it makes a custom image (the coding default, "
                        "never the research default), e.g. --preset full --with ghidra")
    b.add_argument("--preset", choices=["full", "slim"],
                   help="full: every toolchain, the coding agents' default; slim: basics only, the research agents' default")
    b.add_argument("--no-default", action="store_true", help="don't make it the default image for new agents")
    bc = sub.add_parser("bootcheck", help="find which QEMU option stops an image from booting")
    bc.add_argument("--image", required=True)
    bc.add_argument("--timeout", type=int, default=90)
    bc.add_argument("--all", action="store_true", help="test every variant even if the panel's command line boots")
    bc.add_argument("--tcg", action="store_true", help=argparse.SUPPRESS)
    sc = sub.add_parser("secrets", help="list secrets, or store one with systemd-creds so you never type it again")
    sc.add_argument("action", nargs="?", choices=["list", "set", "forget"], default="list")
    sc.add_argument("key", nargs="?", help="e.g. claude, research, github, token:bot, mcp:PIXELLAB_API_KEY")
    sub.add_parser("init-config", help="write the default config file if none exists")
    sub.add_parser("show-config", help="print the effective config")
    return ap


def main(argv=None) -> int:
    a = parser().parse_args(argv)

    if os.geteuid() == 0:
        print("refusing to run as root", file=sys.stderr)
        return 1
    if sys.version_info < (3, 11):
        print("cilly needs Python 3.11 or newer", file=sys.stderr)
        return 1
    if a.cmd == "setup":
        return setup(a.port, not a.no_browser)
    if a.cmd == "open":
        return open_panel(a.print_link)
    if a.cmd == "service":
        from . import launch as L
        if a.action == "install":
            print(f"Wrote {L.install_service()}. cilly starts when you log in. "
                  "To start it now instead, stop this one and run: systemctl --user start cilly")
        elif a.action == "remove":
            L.remove_service()
            print("cilly no longer starts when you log in.")
        else:
            print(json.dumps(L.service_status()))
        return 0
    if a.cmd == "serve":
        from .server import serve
        serve(a.port, open_browser=a.open)
    elif a.cmd == "doctor":
        return doctor()
    elif a.cmd == "build-base":
        from .base_image import PRESETS, build, check_toolchains
        cfg = C.load()
        try:
            tc = check_toolchains((PRESETS[a.preset]["toolchains"] if a.preset else [])
                                  + [t.strip() for t in a.toolchains.split(",") if t.strip()])
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        extra = [t.strip() for t in a.toolchains.split(",") if t.strip()]
        preset = a.preset if (a.preset and not extra) else ""  # a preset plus --with is a custom mix, not the preset
        if a.download:
            from .debian_image import fetch
            a.image = fetch()
        name = build(a.image, cfg, timeout_min=a.timeout_min, toolchains=tc, preset=preset)
        if not a.no_default:
            key = PRESETS[preset]["default_for"] if preset else "base_image"  # a custom image is only the coding default
            cfg = C.load()  # the panel may have changed it during the build
            cfg[key] = name
            C.save(cfg)
            print(f"config updated: {key} = {name}")
    elif a.cmd == "bootcheck":
        from .bootcheck import run
        run(a.image, tcg=a.tcg, timeout=a.timeout, all_variants=a.all)
    elif a.cmd == "secrets":
        from . import creds
        if a.action != "list" and not a.key:
            from .host import cli
            print(f"which secret? e.g. {cli()} secrets set claude", file=sys.stderr)
            return 2
        return creds.main(a)
    elif a.cmd == "init-config":
        if os.path.exists(C.config_path()):
            print(f"{C.config_path()} already exists")
        else:
            C.save(C.load())
            print(f"wrote {C.config_path()}")
    elif a.cmd == "show-config":
        print(json.dumps(C.load(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
