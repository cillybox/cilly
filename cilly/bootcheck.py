"""`cilly bootcheck`: find which part of the VM command line stops a guest
from booting.  Boots the panel's exact QEMU command line, then variants with
one piece removed, and reports which ones reach Linux."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import config as C
from .inject import build_task_disk
from .vm import (VMError, VMPaths, _connect_unix, bwrap_argv, bwrap_decision, detect_tools, qemu_argv, sandbox_python)

_BOOTED = re.compile(rb"Linux version|Run /init|systemd\[1\]|Welcome to|login:")


def _drop(argv, flag, pred=lambda v: True):
    out, i = [], 0
    while i < len(argv):
        if argv[i] == flag and i + 1 < len(argv) and pred(argv[i + 1]):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def _drop_flag(argv, flag):
    return [a for a in argv if a != flag]


def variants(base: list[str], image: str, paths: VMPaths) -> list[tuple[str, list[str]]]:
    v = [("panel command line, unchanged", base)]
    v.append(("without QEMU seccomp (-sandbox)", _drop(base, "-sandbox")))
    v.append(("without -nodefaults/-no-user-config", _drop_flag(_drop_flag(base, "-nodefaults"), "-no-user-config")))
    # the panel starts VMs with a VGA card (-vga std): Debian's boot path needs one
    v.append(("without a VGA card (-vga none)", [("none" if a == "std" and base[i - 1] == "-vga" else a)
                                                 for i, a in enumerate(base)]))
    v.append(("with QEMU's default devices (no -nodefaults, no -vga)",
              _drop(_drop_flag(_drop_flag(base, "-nodefaults"), "-no-user-config"), "-vga")))
    no_task = _drop(_drop(base, "-drive", lambda x: "id=task" in x), "-device", lambda x: "drive=task" in x)
    v.append(("without the second (boot/seed) disk", no_task))
    no_net = _drop(_drop(base, "-netdev"), "-device", lambda x: x.startswith("virtio-net"))
    v.append(("without networking", no_net))
    no_ser = _drop(base, "-device", lambda x: x.startswith(("virtio-serial", "virtserialport")))
    no_ser = _drop(no_ser, "-chardev", lambda x: "id=ctl" in x or "id=log" in x)
    v.append(("without virtio-serial ports", no_ser))
    no_rng = _drop(_drop(base, "-object", lambda x: x.startswith("rng-")), "-device", lambda x: "rng" in x)
    v.append(("without virtio-rng", no_rng))
    v.append(("with -cpu host,pmu=off",
              [(a.replace("host", "host,pmu=off", 1) if base[i - 1] == "-cpu" else a) for i, a in enumerate(base)]))
    direct = [(f"if=none,id=root,file={image},format=qcow2,snapshot=on" if "id=root" in a else a) for a in base]
    v.append(("base image directly instead of the overlay disk", direct))
    return v


def _boot(argv: list[str], console_sock: str, timeout: float) -> tuple[str, str]:
    q = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    data = bytearray()
    try:
        try:
            s = _connect_unix(console_sock, 15)
        except OSError:
            err = q.stderr.read(2000).decode(errors="replace") if q.poll() is not None else ""
            return "QEMU did not start", err.strip()[-300:]
        s.settimeout(1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            except TimeoutError:
                pass
            except OSError:
                break
            if _BOOTED.search(data):
                return "BOOTS", ""
            if data.count(b"Booting `") >= 3:
                return "REBOOT LOOP", ""
            if q.poll() is not None:
                break
        if q.poll() is not None:
            err = q.stderr.read(2000).decode(errors="replace")
            return f"QEMU exited ({q.returncode})", err.strip()[-300:]
        return "no Linux output (timeout)", ""
    finally:
        if q.poll() is None:
            q.kill()
            q.wait()
        time.sleep(1)  # let the sandbox's processes exit


def run(image: str, *, tcg: bool = False, timeout: float = 90, all_variants: bool = False, out=sys.stdout) -> None:
    cfg = C.load()
    image = os.path.abspath(image)
    t = detect_tools(cfg)
    if not t["qemu"] or not t["qemu-img"]:
        raise SystemExit("qemu-system-x86_64 / qemu-img not found")
    try:  # the same choice as an agent's start: inside bubblewrap when it's installed, or not at all
        bwrap, note = bwrap_decision(cfg["vm"]["use_bwrap"])
    except VMError as e:
        raise SystemExit(str(e)) from None
    print(f"bootcheck: {note}", file=out)
    run_dir = tempfile.mkdtemp(prefix="cilly-bootcheck-", dir=C.ensure_private_dir(C.runtime_dir()))
    work = tempfile.mkdtemp(prefix="cilly-bootcheck-")
    try:
        paths = VMPaths(work, run_dir)
        os.mkdir(paths.sandbox_dir)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", image, paths.disk, "20G"],
                       check=True)
        build_task_disk(paths.task_disk, {"bootcheck": True}, None)
        py, py_ro = sandbox_python() if bwrap else (sys.executable, [])
        base = qemu_argv(qemu=t["qemu"], name="bootcheck", paths=paths, cpus=cfg["vm"]["cpus"],
                         memory_mb=cfg["vm"]["memory_mb"], python=py, cpu_model=cfg["vm"]["cpu_model"],
                         nested_virt=cfg["vm"]["nested_virt"])
        if tcg:  # for testing without KVM
            base = [a.replace("accel=kvm", "accel=tcg") for a in base]
            base = [(a.replace("host", "max", 1) if base[i - 1] == "-cpu" else a) for i, a in enumerate(base)]
        tests = variants(base, image, paths)
        if bwrap:
            wrap = lambda a: bwrap_argv(bwrap, a, ro=[image, os.path.dirname(os.path.abspath(__file__))] + py_ro,
                                        rw=[paths.sandbox_dir, run_dir])
            nokvm = (lambda a: _strip_kvm(a)) if tcg else (lambda a: a)
            tests = [(name, nokvm(wrap(a))) for name, a in tests]
            tests.insert(1, ("without the bwrap sandbox", base))
        print(f"bootcheck: {len(tests)} boots, up to {int(timeout)}s each (bwrap {'on' if bwrap else 'off'}, "
              f"-cpu {cfg['vm']['cpu_model']})", file=out, flush=True)
        for name, argv in tests:
            for f in os.listdir(run_dir):
                os.unlink(os.path.join(run_dir, f))
            # fresh overlay each time: a previous QEMU may still be exiting and
            # holding the old file's lock
            os.unlink(paths.disk)
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", image, paths.disk,
                            "20G"], check=True)
            result, detail = _boot(argv, paths.console_sock, timeout)
            print(f"  {result:<26} {name}" + (f"\n      {detail}" if detail else ""), file=out, flush=True)
            if name == "panel command line, unchanged" and result == "BOOTS" and not all_variants:
                print("The panel's command line boots; no need to test variants.", file=out)
                return
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)


def _strip_kvm(argv):
    i = argv.index("--dev-bind")
    return argv[:i] + argv[i + 3:]
