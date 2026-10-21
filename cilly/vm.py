"""VM lifecycle: overlays, QEMU command line, bwrap wrapper, logs, secrets.

Every VM is one plain `qemu-system-x86_64` process run as you.  Nothing here
needs root.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import ctypes
import hashlib
import io
import json
import os
import re
import secrets as _secrets
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass, field

from . import config as C
from . import host as H
from . import tlsca
from .egress_proxy import load_branches
from .inject import Manifest, build_task_disk
from .projects import clean_text

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
GUEST_DIR = os.path.join(PKG_DIR, "guest")  # what goes into the VM: the runner and toolchain helpers
RELAY = os.path.join(PKG_DIR, "relay.py")
PROXY_ADDR = "10.0.2.100"
PROXY_PORT = 3128
LOG_CAP = 64 * 1024 * 1024  # per log file
# How long Stop waits for a VM to shut itself down: the runner lets a task finish up (90 s), then exports
# the memory.  Quitting cilly waits as long, and contrib/cilly.service's TimeoutStopSec leaves room for it.
STOP_WAIT = 180
SEND_TIMEOUT = 10  # a VM that stops reading its control channel counts as unreachable
BOOT_WAIT = 600  # the hello waits for the guest to boot and start reading: a big policy doesn't fit the socket buffer
LOST_CONTACT = "Lost contact with the VM. Stop it and start it again."
MOVE_WAIT = 3600  # one boot that packs or unpacks an agent's home folder, at most
INTERRUPT_WAIT = 5  # an older runner never answers an interrupt
DELIVER_WAIT = 10  # for the runner's answer to Send now into a running session (only runners that take it are asked)
DELIVERY_LOST = {  # what Claude Code said about a message sent into a running task, other than answering it
    "refused": "Claude Code refused your message \"{}\", so Claude won't read it. Send it again if you still need it.",
    "cancelled": "Claude Code cancelled your message \"{}\", so Claude may not have answered it. Send it again if you still need it.",
    "discarded": "The session ended before Claude read your message \"{}\". Send it again if you still need it.",
}
# The runner ends a task at its time limit.  Should a task still run this long after it, the host
# interrupts it, and stops the VM if it hasn't ended BACKSTOP_WAIT seconds later.
BACKSTOP_GRACE = 10 * 60  # the runner's own end of the task: commit, push and pull request
BACKSTOP_WAIT = 60
# An agent that loses its disk when it stops can keep its ~/outbox first (keep_outbox): Stop waits this
# long for the copy, then stops either way.
KEEP_OUTBOX_WAIT = 600


class VMError(Exception):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def which(name: str) -> str | None:
    return shutil.which(name)


def _no_commas(*paths: str) -> None:
    for p in paths:
        if "," in p:
            raise VMError(f"path contains a comma, which QEMU options cannot express safely: {p}")


_libc = None


def _pdeathsig():
    """preexec_fn: kill the child if the panel thread that spawned it dies."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(None, use_errno=True)
        _libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass
    os.setsid()


def detect_tools(cfg: dict) -> dict:
    mode = cfg["vm"]["use_bwrap"]
    b = which("bwrap")
    # installed, but it can't build a sandbox here (Ubuntu's AppArmor, a kernel setting): VMs don't start
    problem = H.bwrap_check(b) if b and mode != "off" else None
    return {
        "qemu": which(cfg["vm"]["qemu_binary"]),
        "qemu-img": which("qemu-img"),
        "bwrap": None if problem else b,
        "bwrap_problem": problem,
        "use_bwrap": mode,
        "virtiofsd": H.find_virtiofsd(),
        "xorriso": which("xorriso"),
        "kvm": os.access("/dev/kvm", os.R_OK | os.W_OK),
        "openssl": which("openssl"),  # makes each start's certificates when tokens stay on this PC
        "keep_tokens": cfg.get("egress", {}).get("keep_tokens", True) or bool(cfg.get("model_servers")),  # openssl needed
    }


def bwrap_decision(mode: str) -> tuple[str | None, str]:
    """The bubblewrap to run QEMU in for vm.use_bwrap, and a line for the agent's log that says so.

    It fails closed: when bubblewrap is installed, QEMU runs inside it or not at all.  Only "off",
    or bubblewrap not being installed in "auto", runs QEMU without it.  Every call checks a
    bubblewrap that failed before again."""
    if mode == "off":
        return None, "QEMU runs without bubblewrap: vm.use_bwrap is off."
    b = which("bwrap")
    if not b:
        if mode == "required":
            raise VMError("vm.use_bwrap is 'required' but bwrap is not installed")
        return None, "QEMU runs without bubblewrap: it isn't installed."
    problem = H.bwrap_check(b, fresh=True)
    if problem:
        raise VMError(H.bwrap_refusal(problem))
    return b, f"QEMU runs inside bubblewrap ({b})."


def bwrap_blocker(tools: dict) -> str | None:
    """Why no VM can start with bubblewrap as it is (detect_tools' last check), or None."""
    if tools.get("bwrap_problem"):
        return H.bwrap_refusal(tools["bwrap_problem"])
    if tools.get("use_bwrap") == "required" and not tools.get("bwrap"):
        return "vm.use_bwrap is 'required' but bwrap is not installed"
    return None


def images_dir() -> str:
    return C.ensure_private_dir(os.path.join(C.data_dir(), "images"))


def logs_dir() -> str:
    return C.ensure_private_dir(os.path.join(C.data_dir(), "logs"))


def list_base_images() -> list[str]:
    try:
        return sorted(f for f in os.listdir(images_dir()) if f.startswith("base-") and f.endswith(".qcow2"))
    except FileNotFoundError:
        return []


def image_info(name: str) -> dict:
    """What an image was built with, from the note the build left next to it (on the host).  "outdated":
    built before the rename to cilly, so the runner inside listens under the old names and never answers."""
    info, noted = {}, False
    try:
        with open(os.path.join(images_dir(), name[:-len(".qcow2")] + ".json")) as f:
            info, noted = json.load(f), True
    except (OSError, ValueError):
        pass
    tc = info.get("toolchains") if isinstance(info, dict) else None
    ok = isinstance(info, dict)
    label = " ".join(str(info.get("label") or "").split())[:40] if ok else ""
    return {"name": name, "toolchains": [t for t in tc if isinstance(t, str)] if isinstance(tc, list) else [],
            "built": str(info.get("built") or "") if ok else "", "preset": str(info.get("preset") or "") if ok else "",
            "label": "".join(c for c in label if c.isprintable()),
            "outdated": noted and not (ok and info.get("runner") == "cilly")}


def default_image(cfg: dict, profile: str = "coding") -> str:
    if profile == "research" and cfg.get("research_base_image") in list_base_images():
        return cfg["research_base_image"]
    return cfg.get("base_image") or (list_base_images() or [""])[-1]


def base_image_path(cfg: dict, name: str | None = None, profile: str = "coding") -> str:
    name = name or default_image(cfg, profile)
    if not name or "/" in name:
        raise VMError("No VM image yet. Build one in Settings, under VM images, or run `" + H.cli() + " build-base --download`.")
    p = os.path.join(images_dir(), name)
    if not os.path.isfile(p):
        raise VMError(f"base image {name} not found")
    return p


# ---------------------------------------------------------------------------
# command lines (pure functions, unit-tested)
# ---------------------------------------------------------------------------

@dataclass
class VMPaths:
    vm_dir: str
    run_dir: str

    # Only `sandbox_dir` and `run_dir` are visible inside QEMU's bwrap sandbox.
    # meta.json, logs and the egress policy live elsewhere, out of reach of an escaped QEMU.
    @property
    def sandbox_dir(self): return os.path.join(self.vm_dir, "sandbox")
    @property
    def disk(self): return os.path.join(self.sandbox_dir, "disk.qcow2")
    @property
    def task_disk(self): return os.path.join(self.sandbox_dir, "task.tar")
    @property
    def meta(self): return os.path.join(self.vm_dir, "meta.json")
    @property
    def agent_log(self): return os.path.join(self.vm_dir, "agent.log")
    @property
    def console_log(self): return os.path.join(self.vm_dir, "console.log")
    @property
    def egress_log(self): return os.path.join(self.vm_dir, "egress.log")
    @property
    def branches_file(self): return os.path.join(self.vm_dir, "branches.jsonl")  # written by the egress proxy
    @property
    def qmp(self): return os.path.join(self.run_dir, "qmp.sock")
    @property
    def memout(self): return os.path.join(self.sandbox_dir, "memout.img")
    @property
    def transfer(self): return os.path.join(self.sandbox_dir, "transfer.img")  # a home folder on its way to a new image
    @property
    def disk_old(self): return os.path.join(self.sandbox_dir, "disk-old.qcow2")  # kept until a move has worked
    @property
    def allow_file(self): return os.path.join(self.vm_dir, "allow.txt")
    @property
    def never_file(self): return os.path.join(self.vm_dir, "never-reach.txt")
    @property
    def route_file(self):
        """In memory, as it can hold a proxy login, but next to `run_dir`, not in it: an escaped QEMU
        must neither read the login nor change where the proxy sends traffic."""
        return os.path.join(os.path.dirname(self.run_dir), "routes", os.path.basename(self.run_dir) + ".json")
    @property
    def tokens_file(self):
        """The real tokens for this agent's proxy: like the route file, in memory and out of QEMU's sandbox."""
        return os.path.join(os.path.dirname(self.run_dir), "tokens", os.path.basename(self.run_dir) + ".json")
    @property
    def tls_dir(self):  # this start's host certificates and keys
        return os.path.join(os.path.dirname(self.run_dir), "tls", os.path.basename(self.run_dir))
    @property
    def control_sock(self): return os.path.join(self.run_dir, "control.sock")
    @property
    def log_sock(self): return os.path.join(self.run_dir, "log.sock")
    @property
    def console_sock(self): return os.path.join(self.run_dir, "console.sock")
    @property
    def proxy_sock(self): return os.path.join(self.run_dir, "proxy.sock")
    @property
    def vfs_sock(self): return os.path.join(self.run_dir, "vfs.sock")


def qemu_argv(*, qemu: str, name: str, paths: VMPaths, cpus: int, memory_mb: int,
              task_disk_format: str = "raw", live_share: bool = False, memout: bool = False,
              python: str = sys.executable, cpu_model: str = "host", nested_virt: bool = False,
              transfer: str | None = None, old_names: bool = False) -> list[str]:
    """old_names: the disks and ports under vmpanel's names, for an image built before the rename.  Its
    runner then finds the boot disk and hands over to the current runner, which knows both names."""
    _no_commas(paths.disk, paths.task_disk, paths.run_dir, RELAY, python)
    n = "vmpanel" if old_names else "cilly"
    relay_cmd = " ".join(shlex.quote(x) for x in (python, "-I", RELAY, paths.proxy_sock))
    argv = [
        qemu,
        "-name", f"cilly-{name}",
        "-machine", "q35,accel=kvm",
        # no VMX/SVM for the guest unless asked: nested virtualization is extra host-KVM attack surface
        "-cpu", cpu_model if nested_virt else cpu_model + ",vmx=off,svm=off",
        "-smp", str(cpus),
        "-m", f"{memory_mb}M",
        "-nodefaults", "-no-user-config",
        # An emulated VGA card is required even though nothing is displayed:
        # Debian's boot path queries the VGA BIOS before Linux starts, and with
        # no card the guest resets in a loop.  `-display none` keeps it headless.
        "-display", "none", "-vga", "std",
        # QEMU's own seccomp filter. `spawn` and `elevateprivileges` must stay
        # allowed: guestfwd's cmd is forked once per guest connection, and
        # libslirp calls setsid() in the child, which elevateprivileges=deny
        # kills.  QEMU still can't gain privileges: it runs as you, inside bwrap
        # (no_new_privs).
        "-sandbox", "on,obsolete=deny,resourcecontrol=deny",
        "-drive", f"if=none,id=root,file={paths.disk},format=qcow2,discard=unmap",
        "-device", f"virtio-blk-pci,drive=root,serial={n}-root",
        "-drive", f"if=none,id=task,file={paths.task_disk},format={task_disk_format},readonly=on",
        "-device", f"virtio-blk-pci,drive=task,serial={n}-task",
        # restrict=on: the guest cannot reach the host or route anywhere.
        # The only way out is the guestfwd below, into the allowlisting proxy.
        "-netdev", f"user,id=n0,restrict=on,ipv6=off,guestfwd=tcp:{PROXY_ADDR}:{PROXY_PORT}-cmd:{relay_cmd}",
        "-device", "virtio-net-pci,netdev=n0",
        "-device", "virtio-serial-pci,id=vser",
        # control: root-only port in the guest; carries policy, tasks (with
        # their secrets) and status.  The agent user cannot open it.
        "-chardev", f"socket,id=ctl,path={paths.control_sock},server=on,wait=off",
        "-device", f"virtserialport,bus=vser.0,chardev=ctl,name=org.{n}.control",
        "-chardev", f"socket,id=log,path={paths.log_sock},server=on,wait=off",
        "-device", f"virtserialport,bus=vser.0,chardev=log,name=org.{n}.log",
        "-chardev", f"socket,id=con,path={paths.console_sock},server=on,wait=off",
        "-serial", "chardev:con",
        "-qmp", f"unix:{paths.qmp},server=on,wait=off",
        "-object", "rng-random,id=rng,filename=/dev/urandom",
        "-device", "virtio-rng-pci,rng=rng",
        # The guest hands its free memory back to the PC on its own.  QEMU discards the pages with
        # madvise, or punches holes with fallocate for the memfd backend of a live share; -sandbox
        # allows both.  No target size and no stats: nothing else is used.
        "-device", "virtio-balloon-pci,free-page-reporting=on",
    ]
    if memout:
        # writable scratch disk the guest writes its memory export to (tar);
        # the host validates it after QEMU exits (cilly/memory.py)
        _no_commas(paths.memout)
        argv += ["-drive", f"if=none,id=memout,file={paths.memout},format=raw",
                 "-device", f"virtio-blk-pci,drive=memout,serial={n}-memout"]
    if transfer:
        # the home folder moving to a new image: written by the first VM ("rw"), read by the second
        # ("ro").  Always format=raw, so QEMU never reads a header the VM wrote as an image format.
        _no_commas(paths.transfer)
        argv += ["-drive", f"if=none,id=transfer,file={paths.transfer},format=raw" + (",readonly=on" if transfer == "ro" else ""),
                 "-device", f"virtio-blk-pci,drive=transfer,serial={n}-transfer"]
    if live_share:
        argv += [
            "-object", f"memory-backend-memfd,id=mem,size={memory_mb}M,share=on",
            "-numa", "node,memdev=mem",
            "-chardev", f"socket,id=vfs,path={paths.vfs_sock}",
            "-device", "vhost-user-fs-pci,chardev=vfs,tag=inject",
        ]
    return argv


def sandbox_python() -> tuple[str, list[str]]:
    """The python that runs the guestfwd relay inside bwrap: its real path
    (Debian's /usr/bin/python3 goes through /etc/alternatives, which the
    sandbox doesn't have) plus any directory to bind if it lives outside /usr."""
    exe = os.path.realpath(shutil.which("python3") or "/usr/bin/python3")
    if exe.startswith("/usr/"):
        return exe, []
    prefix = os.path.dirname(os.path.dirname(exe))  # e.g. /opt/python3.13
    return exe, [prefix]


def bwrap_argv(bwrap: str, inner: list[str], *, ro: list[str], rw: list[str]) -> list[str]:
    """Wrap QEMU in bubblewrap: no network namespace, read-only /usr, only the
    VM's own directories visible.  Slirp with restrict=on needs no host network;
    the guestfwd relay reaches the proxy through a Unix socket."""
    argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr"]
    # Mirror the host's top-level layout (Arch: /lib64 -> usr/lib; Debian and
    # Ubuntu: /lib64 -> usr/lib64) so the dynamic loader is found.
    for d in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(d):
            argv += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    argv += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            "--proc", "/proc", "--dev", "/dev",
            "--dev-bind", "/dev/kvm", "/dev/kvm",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    for p in ro:
        argv += ["--ro-bind", p, p]
    for p in rw:
        argv += ["--bind", p, p]
    return argv + ["--"] + inner


def virtiofsd_argv(virtiofsd: str, sock: str, shared_dir: str) -> list[str]:
    return [virtiofsd, f"--socket-path={sock}", f"--shared-dir={shared_dir}",
            "--readonly", "--sandbox=namespace", "--cache=never"]


def virtiofsd_supports_readonly(virtiofsd: str) -> bool:
    try:
        out = subprocess.run([virtiofsd, "--help"], capture_output=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return b"--readonly" in out


# ---------------------------------------------------------------------------
# QMP
# ---------------------------------------------------------------------------

def qmp(sock_path: str, command: str, timeout: float = 5) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        f = s.makefile("rwb")
        f.readline()  # greeting
        for cmd in ("qmp_capabilities", command):
            f.write(json.dumps({"execute": cmd}).encode() + b"\n")
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    return {}
                msg = json.loads(line)
                if "return" in msg or "error" in msg:
                    break
        return msg
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
#
# An agent is a named, long-lived record with one of three lifecycle modes:
#   ephemeral   fresh VM disk on every start; the disk is deleted on stop
#   persistent  the VM disk survives stop/start (until "Reset to clean")
#   memory      fresh VM disk on every start, but the agent's Claude Code
#               conversations/memory are exported on stop and restored on the
#               next start (cilly/memory.py validates the export)
#
# While an agent runs, the panel talks to the guest runner over a root-only
# virtio-serial "control" port: policy updates, tasks (each carrying fresh
# secrets), shutdown; the runner answers with ready/status messages.

MODES = ("ephemeral", "persistent", "memory")
_ACTIVE = ("booting", "ready", "busy", "stopping")


def agents_dir() -> str:
    return C.ensure_private_dir(os.path.join(C.data_dir(), "agents"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write `path` in one step.  Each write has its own temporary file (made with O_EXCL, so never
    through a link): two threads saving the same agent at once can't take each other's."""
    d, name = os.path.split(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


CHAT_CAP = 20 * 1024 * 1024  # per agent; past this, chat stops growing (the log keeps going)
MESSAGE_KEPT = 2000          # characters of your message kept in the task record; a longer one gets its own file
TASKS_KEPT = 200             # task records per agent
CHAT_RECENT = 5000           # events kept in memory per agent for the chat view
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # Claude Code session ids
_CHAT_STR = {"text": 20000, "id": 64, "tool": 64, "verb": 20, "target": 500, "summary": 300, "file": 500,
             "detail": 4000, "out": 2000, "parent": 64, "sub": 64, "desc": 200, "last": 64}
# kind -> (fields always present, optional text fields, optional counters)
_CHAT_KINDS = {
    "text": (("text",), ("parent",), ()),
    "tool": (("id", "tool", "verb", "target"), ("detail", "parent", "sub"), ()),
    "result": (("id", "summary"), ("out", "parent"), ()),
    "diff": (("id", "file"), ("parent",), ()),
    "done": ((), (), ("turns", "ms")),
    "status": (("text",), (), ()),
    "sub": (("id", "state"), ("desc", "sub", "last", "summary"), ("tools", "tokens", "ms")),  # a subagent or background job
}


def deliverable(task: dict, live: dict) -> bool:
    """Send now can go into the running session: a message without files, for the same conversation,
    branch, model and effort.  Anything else starts its own task, as before."""
    return (not task.get("attachments") and not task.get("attach") and task.get("continue") is True
            and not task.get("resume") and not task.get("new_branch")
            and (task.get("model") or task.get("local_model")) == live.get("model")  # what the task record keeps
            and task.get("effort") == live.get("effort"))


def _clean_chat_event(e) -> dict | None:
    """Keep only known event kinds and fields, with types and sizes checked:
    the VM is untrusted, and the browser shows these as plain text."""
    if not isinstance(e, dict) or not isinstance(e.get("k"), str) or e["k"] not in _CHAT_KINDS:
        return None
    k = e["k"]
    need, opt, nums = _CHAT_KINDS[k]
    out = {"k": k}
    for f in need:
        v = e.get(f, "")
        out[f] = v[:_CHAT_STR.get(f, 200)] if isinstance(v, str) else ""
    for f in opt:
        if isinstance(e.get(f), str) and e[f].strip():
            out[f] = e[f][:_CHAT_STR[f]]
    for f in nums:
        if isinstance(e.get(f), int) and not isinstance(e.get(f), bool) and 0 <= e[f] < 10**12:
            out[f] = e[f]
    if k in ("text", "status"):
        if k == "status":
            out["text"] = " ".join(out["text"][:300].split())
        if not out["text"].strip():
            return None
    if k in ("result", "done"):
        out["ok"] = e.get("ok") is True
    if k in ("tool", "sub") and e.get("bg") is True:
        out["bg"] = True
    if k == "sub" and out["state"] not in ("running", "completed", "failed", "stopped"):
        return None
    if k == "diff":
        out["add"] = e["add"] if isinstance(e.get("add"), int) and 0 <= e["add"] < 10**7 else 0
        out["del"] = e["del"] if isinstance(e.get("del"), int) and 0 <= e["del"] < 10**7 else 0
        lines = e.get("lines") if isinstance(e.get("lines"), list) else []
        out["lines"] = [[ln[0], ln[1][:300]] for ln in lines[:200]
                        if isinstance(ln, list) and len(ln) == 2 and ln[0] in ("+", "-", " ", "@")
                        and isinstance(ln[1], str)]
    if k == "done":
        if isinstance(e.get("cost"), (int, float)) and 0 <= e["cost"] < 10**5:
            out["cost"] = round(float(e["cost"]), 4)
        if isinstance(e.get("sid"), str) and SESSION_RE.fullmatch(e["sid"]):
            out["sid"] = e["sid"]
    return out


ATTACH_CHUNK = 512 * 1024  # raw bytes per control message (the guest caps lines at 4 MiB)
LINE_MAX = 1 << 20         # one control message from the guest
OUTBOX_MAX = 16 << 20      # one research delivery as the runner encodes it (JSON, before validation)


SUMMARY_MAX = 80  # characters of the agent's own summary line (from ~/status.txt in the VM)
SUMMARY_GAP = 5   # seconds between stored summaries; a newer one waits its turn, so a VM can't flood them
CONTEXT_MAX = 10_000_000  # tokens: no model's context window comes near
CONTEXT_SAVE_GAP = 5      # seconds between saves of the context size (it's kept in memory in between)


def _tokens(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= CONTEXT_MAX


def context_window(model) -> int:
    """A model's context window, for when Claude Code didn't report one: Claude models have
    200k tokens, or 1M when picked with the [1m] option."""
    return 1_000_000 if isinstance(model, str) and model.lower().endswith("[1m]") else 200_000


def clean_summary(text, hide=lambda s: s) -> str:
    """The agent's summary as one short line of plain text: the VM is untrusted.  Secrets are hidden
    after cleaning too, as removing an invisible character can join a secret back together."""
    if not isinstance(text, str):
        return ""
    lines = clean_text(text[:4000], 4000).splitlines()
    line = " ".join(lines[0].split()) if lines else ""
    line = hide("".join(c for c in line if unicodedata.category(c) != "Cc"))  # C1 controls too
    return line if len(line) <= SUMMARY_MAX else line[:SUMMARY_MAX - 1].rstrip() + "…"


def _drop_attachments(task: dict) -> None:
    """Uploaded files live on the host only until they are sent (or dropped)."""
    for att in task.pop("attachments", None) or []:
        _rm(att["path"])


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def tree_rss_mb(pid: int, proc: str = "/proc") -> int | None:
    """How much of your PC's memory a process and its children use now, in MiB (VmRSS).
    QEMU may run under bwrap, so the process the panel started can be the wrapper."""
    total, seen, todo = 0, set(), [pid]
    while todo and len(seen) < 64:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(f"{proc}/{p}/status", encoding="ascii", errors="replace") as f:
                rss = next((line.split()[1] for line in f if line.startswith("VmRSS:")), "0")
            total += int(rss) if rss.isdigit() else 0
            for tid in os.listdir(f"{proc}/{p}/task"):
                with open(f"{proc}/{p}/task/{tid}/children", encoding="ascii", errors="replace") as f:
                    todo += [int(c) for c in f.read(4096).split() if c.isdigit()]
        except (OSError, ValueError):
            continue
    return total // 1024 if seen and total else None


MEM_EVERY = 5.0  # seconds between two reads of an agent's memory use


BOOT_STEPS = {  # what happens from one step of a start to the next
    "start": "Preparing", "disk": "Disk", "qemu": "QEMU and firmware", "console": "Boot loader, Linux and systemd",
    "runner": "Runner start", "policy": "Policy and setup", "memory": "Memory", "selftest": "Self-test",
}


def boot_times(marks: list) -> list:
    """Where a start's time went: [[what, seconds], ...] in order, from (step, monotonic time) marks."""
    marks = sorted(marks, key=lambda m: m[1])
    return [[BOOT_STEPS.get(k, k), round(max(0.0, t2 - t1), 1)] for (k, t1), (_, t2) in zip(marks, marks[1:])
            if k in BOOT_STEPS]


class Agent:
    def __init__(self, aid: str, meta: dict):
        self.id = aid
        self.meta = meta
        self.paths = VMPaths(os.path.join(agents_dir(), aid), os.path.join(C.runtime_dir(), aid))
        self.procs: dict = {}
        self.lock = threading.RLock()
        self.ctl: socket.socket | None = None
        self.ctl_lock = threading.Lock()
        self.ready = threading.Event()
        self.queue: list[dict] = []   # tasks waiting for the agent (hold secrets; RAM only)
        self.thread: threading.Thread | None = None
        self.deleted = False
        self.exports: dict = {}       # export id -> in-flight transfer state
        self.outbox_parts: dict = {}  # task id -> research findings arriving in pieces (only the running task's)
        self.fileops: dict = {}       # list/read request id -> waiting state (viewing files in the panel)
        self.interrupts: dict = {}    # task id -> waiting for the runner to answer an interrupt
        self.delivers: dict = {}      # queued task id -> waiting for the runner to answer a delivery
        self.can_deliver = False      # this VM's runner takes messages into a running session (it said so when ready)
        self.chat_lock = threading.Lock()
        self.chat_recent: list | None = None  # newest chat events (loaded from chat.jsonl on first use)
        self.chat_seq = 0
        self.hide: frozenset = frozenset()  # secret values sent to this VM (RAM only): hidden in what it sends back
        self.summary_at = 0.0         # when the last summary was stored (monotonic)
        self.summary_next: str | None = None  # a newer one, waiting for SUMMARY_GAP to pass
        self.summary_timer: threading.Timer | None = None
        self.sandbox_note = ""        # whether this start's QEMU runs inside bubblewrap, for the agent log
        self.move_step: str | None = None  # "export" or "import": this boot only moves the home folder
        self.move_result: dict | None = None  # what the VM said about it
        self.context_saved = 0.0  # when the context size was last saved (monotonic)
        self.mem_read = (0.0, None)  # (when, MiB): the memory use, read at most every MEM_EVERY seconds
        self.backstops: dict = {}  # task id -> the timer for its time limit
        self.limit_stop = False  # the time limit is stopping the VM: the messages it drops say so
        self.boot_marks: list = []  # (step, monotonic time) of this start, for where the boot time goes
        self.tls: dict | None = None  # this start's CA and host certificates, when tokens stay on this PC
        self.tokens: list = []  # stand-ins and their real tokens, for this agent's proxy (memory only)
        self.models: dict = {}  # the model server this agent's model is on: {host: {"url", "key"}} (memory only)
        self.stand_ins: dict = {}  # real token -> its stand-in, the same for every task of this agent

    @property
    def running(self) -> bool:
        p = self.procs.get("qemu")
        return p is not None and p.poll() is None

    def save_meta(self) -> None:
        if self.deleted:
            return
        try:
            _atomic_write(self.paths.meta, json.dumps(self.meta, indent=2).encode())
        except FileNotFoundError:
            pass  # agent deleted

    def set(self, **kw) -> None:
        with self.lock:
            self.meta.update(kw)
            self.save_meta()

    def update_task(self, tid: str, **kw) -> None:
        with self.lock:
            for t in self.meta.get("tasks", []):
                if t["id"] == tid:
                    t.update(kw)
            self.meta["idle_since"] = None  # Stop When Idle: activity restarts the clock
            self.save_meta()

    def public(self) -> dict:
        with self.lock:
            d = json.loads(json.dumps(self.meta))
            d["queue"] = [t["id"] for t in self.queue]  # the order queued messages run in
            live = self.live_run()  # the queued messages Send now gives to the running session (else it stops the task)
            d["deliver"] = [t["id"] for t in self.queue if live and deliverable(t, live)]
        d["running"] = self.running
        d["has_disk"] = os.path.exists(self.paths.disk)
        d["mem_used_mb"] = self.mem_used_mb() if d["running"] else None
        ctx = d.get("context")
        if ctx and not ctx.get("window"):
            last = next((t for t in reversed(d.get("tasks", [])) if t.get("model")), {})
            d["context"] = dict(ctx, window=context_window(last.get("model") or d.get("model")))
        d["tasks"] = d.get("tasks", [])[-50:]
        return d

    def live_run(self) -> dict | None:
        """The running task's record when Send now can go into it: the runner and its Claude Code said so."""
        with self.lock:
            if not self.can_deliver or self.meta.get("state") != "busy":
                return None
            rec = next((t for t in reversed(self.meta.get("tasks", [])) if t.get("state") == "running"), None)
            return rec if rec and rec.get("live") else None

    def mem_used_mb(self) -> int | None:
        at, mb = self.mem_read
        if time.monotonic() - at >= MEM_EVERY:
            p = self.procs.get("qemu")
            mb = tree_rss_mb(p.pid) if p is not None and isinstance(getattr(p, "pid", None), int) else None
            self.mem_read = (time.monotonic(), mb)
        return mb

    def send(self, msg: dict, timeout: float | None = None) -> None:
        """Waiting for the lock counts toward the deadline too, so a long send (the hello during boot)
        doesn't hold up Stop's shutdown message for longer."""
        data = json.dumps(msg).encode() + b"\n"
        end = time.monotonic() + (SEND_TIMEOUT if timeout is None else timeout)
        if not self.ctl_lock.acquire(timeout=max(0.0, end - time.monotonic())):
            raise VMError("the control channel is busy")
        try:
            if self.ctl is None:
                raise VMError("agent is not running")
            if end - time.monotonic() <= 0:  # the lock came too late: nothing was sent, so the link stays
                raise VMError("the control channel is busy")
            try:
                _send_all(self.ctl, data, end - time.monotonic())
            except TimeoutError:
                # drop the link rather than hold ctl_lock forever; Stop then powers the VM off
                try:
                    self.ctl.shutdown(socket.SHUT_RDWR)  # wakes the control reader
                except OSError:
                    pass
                self.ctl.close()
                self.ctl = None
                if self.meta.get("state") != "stopping":
                    self.set(error=LOST_CONTACT)
                raise VMError("the VM stopped reading its control channel") from None
        finally:
            self.ctl_lock.release()


def _send_all(sock: socket.socket, data: bytes, timeout: float) -> None:
    """sendall with a deadline.  Not a socket timeout: the control reader blocks on the same socket."""
    end = time.monotonic() + timeout
    p = select.poll()
    p.register(sock, select.POLLOUT)
    view = memoryview(data)
    while view:
        left = end - time.monotonic()
        if left <= 0 or not p.poll(left * 1000):
            raise TimeoutError
        try:
            view = view[sock.send(view, socket.MSG_DONTWAIT):]
        except BlockingIOError:
            pass


HIDDEN = "[hidden]"
LOG_LINE_MAX = 65536  # a log line longer than this is written out without waiting for its end
# What a reader doesn't see: terminal escapes, control characters (not tab or newline, nor a carriage
# return before one) and every Default_Ignorable_Code_Point, unassigned ones included.
_INVISIBLE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b[ -/]*[0-~]"  # a title's text (OSC) still counts
    r"|\r(?!\n)|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
    r"\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")


def hide_secrets(text: str, secrets: list, hold: bool = False) -> tuple[str, str]:
    """text with each secret replaced by HIDDEN, matched as a reader sees it: a secret split by invisible
    characters or terminal escapes is caught too, and they're hidden with it.  Everything else stays as
    it was.  With hold, the end that could still be the start of a secret comes back apart, to try again
    with what follows; without, that part is empty."""
    needles = [n for n in (_INVISIBLE.sub("", s) for s in secrets) if n]
    seen = _INVISIBLE.sub("", text)
    if not needles or not hold and not any(n in seen for n in needles):
        return text, ""
    runs, n, pos = [], 0, 0  # (index in seen, index in text) of each visible run
    for m in _INVISIBLE.finditer(text):
        if m.start() > pos:
            runs.append((n, pos))
            n += m.start() - pos
        pos = m.end()
    if pos < len(text):
        runs.append((n, pos))
    starts = [r[0] for r in runs]

    def where(i):  # a visible character's index in text
        k = bisect.bisect_right(starts, i) - 1
        return runs[k][1] + i - runs[k][0]
    spans = []
    for needle in needles:
        i = seen.find(needle)
        while i >= 0:
            spans.append((where(i), where(i + len(needle) - 1) + 1))
            i = seen.find(needle, i + len(needle))
    spans.sort()
    cut = len(text)
    if hold and seen:
        first = len(seen) - max(map(len, needles)) + 1  # the first character that could still start a secret
        if first < len(seen):
            cut = where(max(first, 0))
        for s, e in spans:
            if s < cut < e:
                cut = e  # a whole match goes out now
    merged = []  # overlapping matches of different secrets become one
    for s, e in spans:
        if s >= cut:
            break
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, at = [], 0
    for s, e in merged:
        out += [text[at:s], HIDDEN + "\x00" * ("\x00" in text[s:e])]  # binary stays binary, and is refused as such
        at = e
    out.append(text[at:cut])
    return "".join(out), text[cut:]


# Kept in a stored log: color codes (SGR) and CRLF.  Every other control character is turned into a mark.
_LOG_CONTROL = re.compile("\x1b\\[[0-9;]*m|\r\n|[\x00-\x08\x0b-\x1f\x7f-\x9f" + chr(0xDC80) + "-" + chr(0xDC9F) + "]")
# A color code, CRLF or UTF-8 character cut in two by a read: held back for the next one to finish
_CUT_IN_TWO = re.compile(rb"(?:\x1b(?:\[[0-9;]{0,64})?|\r|[\xc2-\xdf]|[\xe0-\xef][\x80-\xbf]?|[\xf0-\xf4][\x80-\xbf]{0,2})\Z")


def _mark(m) -> str:
    c = m.group()
    if len(c) > 1:
        return c
    n = ord(c) - 0xDC00 if ord(c) >= 0xDC00 else ord(c)  # a raw byte that isn't UTF-8 counts too
    if n < 0x20:
        return chr(0x2400 + n)  # the Control Pictures: ESC is shown as U+241B, NUL as U+2400
    if n == 0x7F:
        return chr(0x2421)
    return chr(0x241B) + chr(n - 0x40)  # a C1 control is ESC and a character: CSI shows as ESC [


def inert_log(data: bytes) -> bytes:
    """Log output as the panel stores it: colors stay, and every other escape or control character
    (but tab, newline and CRLF) becomes a visible mark.  So `cat` on a log can't drive the terminal
    (clipboard writes with OSC 52, disguised links with OSC 8, titles), and the log view shows what
    the file holds."""
    return _LOG_CONTROL.sub(_mark, data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")


class _LogSink:
    """Appends bytes to a file with a size cap (never follows symlinks), made inert (inert_log).  With
    `redact(data, hold)`, known secrets are replaced first, a whole line at a time so one can't slip
    through split in two.  A line too long to wait for is written in parts, holding back what could
    still be the start of a secret."""

    def __init__(self, path: str, redact=None):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.f = os.fdopen(fd, "ab", buffering=0)
        self.size = os.fstat(fd).st_size
        self.redact, self.pending = redact, b""

    def write(self, data: bytes) -> None:
        data, self.pending = self.pending + data, b""
        if self.redact:
            cut = data.rfind(b"\n")
            if cut < 0 and len(data) < LOG_LINE_MAX:
                self.pending = data  # wait for the end of the line
                return
            if cut >= 0:
                data, self.pending = self.redact(data[:cut + 1], False)[0], data[cut + 1:]
            else:
                data, self.pending = self.redact(data, True)
                if len(self.pending) >= LOG_LINE_MAX:  # a run of invisible characters: never hold it forever
                    data += self.redact(self.pending, False)[0]
                    self.pending = b""
        if m := _CUT_IN_TWO.search(data):
            data, self.pending = data[:m.start()], data[m.start():] + self.pending
        self._write(inert_log(data))

    def _write(self, data: bytes) -> None:
        if self.size < LOG_CAP:
            chunk = data[: LOG_CAP - self.size]
            self.f.write(chunk)
            self.size += len(chunk)
            if self.size >= LOG_CAP:
                self.f.write(b"\n[cilly: log size cap reached, further output dropped]\n")

    def close(self):
        if self.pending:
            self._write(inert_log(self.redact(self.pending, False)[0] if self.redact else self.pending))
            self.pending = b""
        self.f.close()


def _connect_unix(path: str, timeout: float = 15) -> socket.socket:
    deadline = time.time() + timeout
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            return s
        except OSError:
            s.close()
            if time.time() > deadline:
                raise
            time.sleep(0.1)


def _read_into(sock: socket.socket, sink: _LogSink, first=None) -> None:
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            if first is not None:  # the first output: when this part of the VM got going
                first, call = None, first
                call()
            sink.write(data)
    except OSError:
        pass
    finally:
        sock.close()
        sink.close()


def _wait_for(path: str, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while not os.path.exists(path):
        if time.time() > deadline:
            raise VMError(f"{what} did not start")
        time.sleep(0.05)


class Manager:
    def __init__(self, cfg_getter):
        self.cfg = cfg_getter
        self.agents: dict[str, Agent] = {}
        self._lock = threading.Lock()
        # Set by the server: on_ready(agent), on_outbox(agent, msg), on_requests(agent, msg)
        self.hooks: dict = {}
        self.route_of = lambda a: {"kind": "direct"}  # the panel's choice for an agent (server.App._route)
        self.token_hosts = lambda a: ()  # the hosts whose tokens stay on this PC for an agent (server.App._token_hosts)
        self.model_hosts = lambda a: ()  # the model servers' names it may use (server.App._model_hosts)
        self._tokens_lock = threading.Lock()  # a task's stand-ins and a start both write an agent's tokens file
        self.panel_port: int | None = None  # set by the server
        self.known_secrets = lambda: ()  # set by the server: every secret value the panel holds
        self.closing = False  # the panel is quitting: no new VMs
        self._load_existing()

    # -- discovery ------------------------------------------------------------
    def _load_existing(self) -> None:
        for aid in os.listdir(agents_dir()):
            p = os.path.join(agents_dir(), aid, "meta.json")
            if not re.match(r"^[0-9a-f]{8}$", aid) or not os.path.isfile(p):
                continue
            with open(p) as f:
                meta = json.load(f)
            if meta.get("state") in _ACTIVE:
                meta["state"] = "stopped"  # VMs die with the panel
            meta["idle_since"] = None  # and so does any idle clock
            if isinstance(meta.get("move"), dict):
                self._undo_move(VMPaths(os.path.join(agents_dir(), aid), ""), meta,
                                "cilly stopped while the agent moved to a new image.")
            for t in meta.get("tasks", []):
                if t.get("state") in ("queued", "running"):
                    t["state"] = "interrupted"
            for e in meta.get("exports", []):
                if e.get("state") == "running":
                    e.update(state="failed", error="the panel stopped during the export")
            shutil.rmtree(os.path.join(agents_dir(), aid, "uploads"), ignore_errors=True)  # never sent
            self.agents[aid] = Agent(aid, meta)

    def list(self) -> list[dict]:
        with self._lock:
            agents = list(self.agents.values())
        return sorted((a.public() for a in agents), key=lambda d: d.get("created", ""), reverse=True)

    def get(self, aid: str) -> Agent:
        a = self.agents.get(aid)
        if not a:
            raise VMError("no such agent")
        return a

    # -- tools ------------------------------------------------------------------
    def tools(self) -> dict:
        return detect_tools(self.cfg())


    # -- create -----------------------------------------------------------------
    def create(self, *, name: str, mode: str, repo: str | None, agent: str, cpus: int, memory_mb: int,
               auto_stop: bool, branch_prefix: str, profile: str = "coding") -> Agent:
        if mode not in MODES:
            raise VMError("unknown mode")
        if profile not in ("coding", "research"):
            raise VMError("unknown profile")
        if profile == "research":
            repo = None
        aid = _secrets.token_hex(4)
        meta = {
            "id": aid, "name": name, "mode": mode, "repo": repo, "agent": agent,
            "cpus": cpus, "memory_mb": memory_mb, "auto_stop": auto_stop,
            "created": _now(), "state": "stopped", "error": None,
            # kind and prefix names come from the first task (next_branch)
            "branch": f"{branch_prefix}{aid}" if repo and C.branch_names(self.cfg()) == "id" else None, "branch_seq": 0,
            "pr": None, "base_image": None, "inject": None, "tasks": [], "memory": None,
            "profile": profile, "egress_extra": [],
            # coding agents: linked research agent, auto mode, research requests, undelivered inbox items
            "research_agent": None, "research_auto": False, "requests": [], "inbox_pending": [],
            "mcp": [],
        }
        a = Agent(aid, meta)
        os.mkdir(a.paths.vm_dir, 0o700)
        os.mkdir(a.paths.sandbox_dir, 0o700)
        a.save_meta()
        with self._lock:
            self.agents[aid] = a
        return a

    def write_allowlist(self, a: Agent, allow: list[str]) -> None:
        """Live egress policy: the agent's proxy re-reads these files on change."""
        _atomic_write(a.paths.allow_file, ("\n".join(allow) + "\n").encode())
        never = self.cfg()["egress"]["never_reach"]
        _atomic_write(a.paths.never_file, "".join(f"{n}\n" for n in never).encode())
        a.set(egress_allow=allow)

    def write_route(self, a: Agent) -> None:
        """How the agent's checked connections leave, for its proxy to re-read (live, like the
        allowlist).  Only while its run folder exists: a stopped agent gets it at its next start."""
        if not os.path.isdir(a.paths.run_dir):
            return
        route = self.route_of(a)
        C.ensure_private_dir(os.path.dirname(a.paths.route_file))
        _atomic_write(a.paths.route_file, json.dumps(route).encode())
        a.set(route_now=None if route.get("kind") == "direct" else route.get("name") or route.get("kind"))  # for the Sandbox tab

    def stand_in(self, a: Agent, real: str) -> str:
        """The stand-in a VM gets for a real token: the same kind of prefix, so tools take it, and
        nothing of the token itself."""
        if real not in a.stand_ins:
            m = re.match(r"sk-ant-[a-z]{3}\d{2}-|github_pat_|gh[pousr]_", real)  # a known kind only, never a piece of it
            a.stand_ins[real] = (m.group(0) if m else "") + "cilly" + _secrets.token_hex(16)
        return a.stand_ins[real]

    def set_tokens(self, a: Agent, tokens: list, models: dict | None = None) -> None:
        a.tokens = tokens
        if models is not None:
            a.models = models
        self.write_tokens(a)

    def write_tokens(self, a: Agent) -> None:
        """The stand-ins and real tokens for the agent's proxy, for the hosts this start has
        certificates for.  Without certificates, no file: the proxy then swaps nothing."""
        with self._tokens_lock:
            tls = a.tls
            if not tls:
                _rm(a.paths.tokens_file)
                return
            have = set(tls["hosts"])
            tokens = [dict(t, hosts=[h for h in t["hosts"] if h in have]) for t in a.tokens]
            doc = {"hosts": tls["hosts"], "tokens": [t for t in tokens if t["hosts"]],
                   "models": {h: m for h, m in a.models.items() if h in have}}
            C.ensure_private_dir(os.path.dirname(a.paths.tokens_file))
            _atomic_write(a.paths.tokens_file, json.dumps(doc).encode())

    def set_models(self, a: Agent, models: dict) -> None:
        if models != a.models:
            a.models = models
            self.write_tokens(a)

    def _drop_tls(self, a: Agent) -> None:
        with self._tokens_lock:
            a.tls = None
            tlsca.remove(a.paths.tls_dir)
            _rm(a.paths.tokens_file)

    # -- start ------------------------------------------------------------------
    def start(self, aid: str, *, allow: list[str], policy: dict, manifest: Manifest | None = None,
              inject_mode: str | None = None, first_task: dict | None = None, inject_to_scratch: bool = False) -> Agent:
        a = self.get(aid)
        with a.lock:
            if a.running or a.meta["state"] in _ACTIVE:
                raise VMError("agent is already running")
            if a.meta.get("move") and not a.move_step:
                raise VMError("The agent is moving to a new image. Wait until it's done.")
            a.meta.update(state="booting", boot_phase="allocate", boot_started=time.time(), archived=False,  # in use again
                          boot_secs=None, boot_times=None, idle_since=None)  # Stop When Idle starts counting once this VM is up
            a.boot_marks = [("start", time.monotonic())]
            if a.meta.get("mode") == "ephemeral":
                a.meta["summary"] = None  # a fresh disk: its status.txt is gone
                if a.meta.get("context"):
                    a.meta["context"] = {"used": 0, "window": None}  # and its conversation
        try:
            argv, extra = self._prepare(a, manifest, inject_mode, allow, inject_to_scratch)
        except Exception as e:
            self._drop_tls(a)
            a.set(state="failed", error=str(e)[:500])
            raise VMError(str(e)) from None
        a.set(error=None)
        th = threading.Thread(target=self._run, args=(a, argv, extra, policy, first_task),
                              name=f"agent-{aid}", daemon=True)
        a.thread = th
        th.start()
        return a

    def _prepare(self, a: Agent, manifest, inject_mode, allow, to_scratch=False):
        cfg = self.cfg()
        t = self.tools()
        if not t["qemu"] or not t["qemu-img"]:
            raise VMError("qemu-system-x86_64 / qemu-img not found. Install QEMU: " + (H.install_command(H.family(H.os_release()), ["qemu"]) or "see the README"))
        if not t["kvm"]:
            raise VMError("/dev/kvm is not accessible to you")
        if manifest is not None and a.meta.get("profile") == "research":
            raise VMError("Research agents can't receive injected folders. They have open web access, "
                          "so anything injected could leak.")
        live = inject_mode == "share"
        if live:
            if not cfg["inject"].get("allow_live_share"):
                raise VMError("live share is disabled in settings")
            if any(i.code == "mountpoint" for i in manifest.issues):
                raise VMError("The folder contains mount points, which a live share would expose. Use copy mode.")
            try:
                st = os.lstat(manifest.root)
            except OSError as e:
                raise VMError(f"folder vanished: {e.strerror}") from None
            if not os.path.isdir(manifest.root) or os.path.islink(manifest.root) or \
                    (st.st_dev, st.st_ino) != (manifest.root_dev, manifest.root_ino):
                raise VMError("The folder changed since the scan. Scan it again.")
            if not t["virtiofsd"] or not virtiofsd_supports_readonly(t["virtiofsd"]):
                raise VMError("virtiofsd with --readonly support is required for a live share")
        bwrap, a.sandbox_note = bwrap_decision(cfg["vm"]["use_bwrap"])
        p = a.paths
        mode = a.meta["mode"]
        os.makedirs(p.sandbox_dir, mode=0o700, exist_ok=True)
        os.makedirs(C.ensure_private_dir(C.runtime_dir()), exist_ok=True)
        longest = max(len(x) for x in (p.control_sock, p.proxy_sock, p.console_sock, p.vfs_sock))
        if longest > 100:  # AF_UNIX paths are limited to 108 bytes
            raise VMError(f"Socket paths under {C.runtime_dir()} are too long for Unix sockets. "
                          "Set XDG_RUNTIME_DIR to a short path such as /run/user/<uid>.")
        shutil.rmtree(p.run_dir, ignore_errors=True)
        os.mkdir(p.run_dir, 0o700)

        a.meta["boot_phase"] = "disk"
        self._boot_mark(a, "disk")
        # Disk: persistent agents keep theirs; everything else starts clean.
        old_names = False
        if mode == "persistent" and os.path.exists(p.disk):
            base = os.path.join(images_dir(), a.meta.get("base_image") or "")
            if not a.meta.get("base_image") or not os.path.isfile(base):
                raise VMError(f"the base image this agent was created from ({a.meta.get('base_image')}) "
                              "is gone; use 'Reset to clean'")
            if image_info(a.meta["base_image"])["outdated"]:
                if a.move_step != "export":  # packing its home folder for the move is all such a disk still does
                    raise VMError("Its disk was made from an image built before cilly's rename. Rebuild the image under "
                                  "Settings → VM images, then use Move to new image in ··· to keep its files, or Reset to clean.")
                old_names = True
        else:
            _rm(p.disk)
            base = base_image_path(cfg, a.meta.get("image"), a.meta.get("profile", "coding"))  # its own pick, else the default
            if image_info(os.path.basename(base))["outdated"]:
                raise VMError(f"{os.path.basename(base)} was built before cilly's rename. Rebuild it under Settings → VM images, "
                              "then start the agent again.")
            subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, p.disk],
                           check=True, capture_output=True, timeout=60)
            a.set(base_image=os.path.basename(base))

        from . import memory as M
        _rm(p.memout)
        if mode == "memory":
            M.create_memout(p.memout)
        _rm(p.task_disk)
        a.models = {}  # a model server only from the first task that runs on it
        keep = set(self.token_hosts(a))
        hosts = sorted(keep | set(self.model_hosts(a)))
        if hosts:  # tokens stay on this PC, or a model server: a CA for this start only, which the VM trusts
            try:
                C.ensure_private_dir(os.path.dirname(p.tls_dir))
                a.tls = tlsca.make(p.tls_dir, hosts)
                a.tls["tokens"] = bool(keep)  # whether this start keeps tokens, or has certificates for model servers only
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
                self._drop_tls(a)
                raise VMError(f"Couldn't make this start's certificates: {e}. Install openssl: keeping tokens on this PC "
                              "and local models need it. Or turn off Keep tokens on this PC in Settings → Network, and "
                              "remove the model servers in Settings → Local models.") from None
        else:
            self._drop_tls(a)
        boot = {
            "agent_id": a.id, "name": a.meta["name"], "mode": mode, "repo": a.meta.get("repo"),
            # its account's, for git's config (each task also says who its commits are by)
            "identity": C.account_identity(cfg, C.token_for_repo(cfg, a.meta["repo"], a.meta.get("account")) if a.meta.get("repo")
                                           else a.meta.get("account") or next(iter(cfg["tokens"]), None)),
            # the panel stops the VM after a task (see _on_guest), so the switch works on a running agent too.
            # Kept, and always false, for images whose runner would power off on its own.
            "auto_stop": False,
            "inject": {"mode": inject_mode, "path_in_vm": "/inject", "to_scratch": bool(to_scratch)} if manifest else None,
            "export_memory": mode == "memory", "profile": a.meta.get("profile", "coding"),
            **({"move": a.move_step} if a.move_step else {}),  # only moves the home folder, then powers off
            "repo_in_scratch": bool(a.meta.get("repo_in_scratch")),
            # the panel listens here on the host: the self-test checks the VM can't reach it
            "selftest_host_ports": [self.panel_port or cfg["listen_port"]],
            # the CA this start's VM trusts, so the proxy can put the real tokens in (None: trust none)
            "ca": a.tls["ca"] if a.tls else None,
        }
        skipped: list = []

        def add_memory(tar):
            # the current guest runner, so runner updates don't need an image rebuild
            runner = os.path.join(GUEST_DIR, "cilly-runner.py")
            with open(runner, "rb") as rf:
                data = rf.read()
            ti = tarfile.TarInfo("runner.py")
            ti.size, ti.mode = len(data), 0o700
            tar.addfile(ti, io.BytesIO(data))
            if mode == "memory":
                skipped.extend(M.add_to_tar(tar, p.vm_dir))
        boot["restore_memory"] = mode == "memory" and os.path.isdir(os.path.join(p.vm_dir, "memory"))
        build_task_disk(p.task_disk, boot, manifest if inject_mode == "copy" else None, extra=add_memory)
        if skipped:  # memory from before the path limits: the agent starts without those files
            a.set(memory=dict(a.meta.get("memory") or {}, skipped=len(skipped), skipped_paths=[s[:200] for s in skipped[:10]]))
        a.set(inject=({"root": manifest.root, "mode": inject_mode, "files": manifest.file_count,
                       "bytes": manifest.total_bytes, "path_in_vm": "/inject", "to_scratch": bool(to_scratch)}
                      if manifest else None))
        self.write_allowlist(a, allow)
        self.write_route(a)
        self.write_tokens(a)

        py, py_ro = sandbox_python() if bwrap else (sys.executable, [])
        argv = qemu_argv(qemu=t["qemu"], name=a.id, paths=p, cpus=a.meta["cpus"],
                         memory_mb=a.meta["memory_mb"], live_share=live, memout=(mode == "memory"), python=py,
                         cpu_model=cfg["vm"]["cpu_model"], nested_virt=cfg["vm"]["nested_virt"],
                         transfer={"export": "rw", "import": "ro"}.get(a.move_step or ""), old_names=old_names)
        if bwrap:
            argv = bwrap_argv(bwrap, argv, ro=[base, PKG_DIR] + py_ro, rw=[p.sandbox_dir, p.run_dir])
        _atomic_write(os.path.join(p.vm_dir, "argv.json"), json.dumps(argv, indent=1).encode())
        extra = {"virtiofsd": virtiofsd_argv(t["virtiofsd"], p.vfs_sock, manifest.root)} if live else {}
        return argv, extra

    def _spawn(self, a: Agent, key: str, argv: list[str], log_path: str, **kw) -> subprocess.Popen:
        log = open(log_path, "ab")
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 preexec_fn=_pdeathsig, close_fds=True, **kw)
        finally:
            log.close()
        a.procs[key] = p
        return p

    def _run(self, a: Agent, argv, extra, policy: dict, first_task: dict | None) -> None:
        """Long-lived per-agent thread; the VM's processes die with it (PDEATHSIG)."""
        p = a.paths
        cfg = self.cfg()
        readers: list[threading.Thread] = []
        sep = f"\n===== cilly: start {_now()} ({a.meta['mode']}) =====\n".encode()
        for path in (p.agent_log, p.console_log):
            s = _LogSink(path)
            s.write(sep + (f"cilly: {a.sandbox_note}\n".encode() if path == p.agent_log and a.sandbox_note else b""))
            s.close()
        try:
            proxy_argv = [sys.executable, "-I", os.path.join(PKG_DIR, "egress_proxy.py"),
                          "--socket", p.proxy_sock, "--log", p.egress_log, "--allow-file", p.allow_file,
                          "--never-reach-file", p.never_file, "--route-file", p.route_file, "--isolate", a.id,
                          "--tokens-file", p.tokens_file, "--branches-file", p.branches_file]
            for port in cfg["egress"]["ports"]:
                proxy_argv += ["--port", str(port)]
            self._spawn(a, "proxy", proxy_argv, os.path.join(p.vm_dir, "proxy.stderr"), cwd="/")
            _wait_for(p.proxy_sock, 10, "egress proxy")
            if "virtiofsd" in extra:
                self._spawn(a, "virtiofsd", extra["virtiofsd"], os.path.join(p.vm_dir, "virtiofsd.log"))
                _wait_for(p.vfs_sock, 10, "virtiofsd")
            a.set(boot_phase="boot")
            self._boot_mark(a, "qemu")
            if self.closing:
                raise VMError("cilly was stopped while this agent started")
            qemu = self._spawn(a, "qemu", argv, os.path.join(p.vm_dir, "qemu.stderr"))
            hide = lambda data, hold, a=a: self.redact_stream(a, data, hold)  # noqa: E731
            for sock, sink, step in ((p.log_sock, _LogSink(p.agent_log, hide), "runner"),
                                     (p.console_sock, _LogSink(p.console_log, hide), "console")):
                try:
                    s = _connect_unix(sock, 15)
                except OSError:
                    sink.close()
                    raise VMError(f"QEMU did not start (see {os.path.join(p.vm_dir, 'qemu.stderr')})")
                rt = threading.Thread(target=_read_into, args=(s, sink, lambda a=a, step=step: self._boot_mark(a, step)),
                                      daemon=True)
                rt.start()
                readers.append(rt)
            ctl = _connect_unix(p.control_sock, 15)
            with a.ctl_lock:
                a.ctl = ctl
            rt = threading.Thread(target=self._control_reader, args=(a, ctl), daemon=True)
            rt.start()
            readers.append(rt)
            a.send({"type": "hello", "policy": policy}, timeout=BOOT_WAIT)
            a.set(policy_version=policy.get("version"))
            if first_task is not None:
                with a.lock:
                    a.queue.append(first_task)
            first_task = None
            qemu.wait()
        except Exception as e:  # noqa: BLE001
            # quitting cilly, or Stop, mid-start isn't the agent failing: it's just stopped
            stopped = self.closing or a.meta.get("state") == "stopping"
            a.set(**({"state": "stopped", "error": None} if stopped else {"state": "failed", "error": str(e)[:500]}))
        finally:
            for key in ("qemu", "virtiofsd", "proxy"):
                proc = a.procs.get(key)
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            with a.ctl_lock:
                if a.ctl is not None:
                    try:
                        a.ctl.close()
                    except OSError:
                        pass
                a.ctl = None
            a.ready.clear()
            a.can_deliver = False
            for rt in readers:
                rt.join(5)
            self._after_stop(a)
            shutil.rmtree(p.run_dir, ignore_errors=True)
            _rm(p.route_file)
            self._drop_tls(a)

    def _after_stop(self, a: Agent) -> None:
        p = a.paths
        mode = a.meta["mode"]
        upd = {}
        if mode == "memory" and os.path.exists(p.memout):
            from . import memory as M
            try:
                res = M.import_memout(p.memout, p.vm_dir)
                upd["memory"] = dict(res, at=_now())
            except (M.MemoryError_, OSError) as e:
                upd["memory"] = {"imported": False, "error": f"memory export rejected: {e}", "at": _now()}
        _rm(p.memout)
        _rm(p.task_disk)
        if mode != "persistent":
            _rm(p.disk)
        with a.lock:
            self._disarm_backstop(a)  # before it reads as stopped: nothing of this run is left by then
            for t in a.queue:
                (t.get("env") or {}).clear()
                _drop_attachments(t)
            a.queue.clear()
            for t in a.meta.get("tasks", []):
                if t.get("state") == "queued" and a.limit_stop:
                    t["time_limit_stop"] = True  # "Not sent: the time limit stopped the VM"
                if t.get("state") in ("queued", "running"):
                    t["state"] = "interrupted"
                    self._attach_failed(a, t["id"])
            a.limit_stop = False
            if a.meta.get("state") in _ACTIVE:
                a.meta["state"] = "stopped"
            a.meta["boot_phase"] = None
            a.meta.update(upd)
            a.save_meta()
        for eid in list(a.exports):
            self._export_fail(a, eid, "the VM stopped during the export")

    def _control_reader(self, a: Agent, sock: socket.socket) -> None:
        buf, dropping = b"", 0  # dropping: bytes of a line over the limit, skipped up to its end
        a.outbox_parts.clear()  # nothing half-sent carries over from an earlier run
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if dropping or len(line) > LINE_MAX:
                        self._dropped(a, dropping + len(line))
                        dropping = 0
                        continue
                    # one bad message is skipped, never the end of the link to this VM.  Logged by size and
                    # kind only: a secret in it can't be hidden reliably before it's parsed.
                    try:
                        msg = self.redact_obj(a, json.loads(self.redact_bytes(a, line)))  # chat, findings, requests, errors
                    except (ValueError, RecursionError) as e:  # not JSON, or nested too deep
                        print(f"agent {a.id}: skipped a message of {len(line)} bytes from its VM ({e.__class__.__name__})",
                              flush=True)
                        continue
                    try:
                        if isinstance(msg, dict):
                            self._on_guest(a, msg)
                    except Exception as e:  # noqa: BLE001 - a type the handler didn't expect, or a panel bug
                        print(f"agent {a.id}: skipped a message of {len(line)} bytes from its VM ({e.__class__.__name__})",
                              flush=True)
                        traceback.print_exc()
                if len(buf) > LINE_MAX:
                    dropping += len(buf)
                    buf = b""
        except OSError:
            return
        finally:
            a.outbox_parts.clear()

    @staticmethod
    def _dropped(a: Agent, size: int) -> None:
        """Never drop a message without saying so: older runners sent big findings in one line."""
        print(f"agent {a.id}: dropped a message of {size} bytes from its VM (the limit is {LINE_MAX})", flush=True)
        a.set(error=f"The VM sent a message larger than {LINE_MAX >> 20} MiB, which was dropped. "
                    "If it was research findings, they didn't arrive.")

    @staticmethod
    def refuse_outbox(a: Agent, tid: str, why: str) -> None:
        """Tell the runner its findings won't be stored, and why: it sets them aside for good
        instead of waiting, and never sends them again (older runners ignore this).  The task
        keeps the reason, for the question it was answering."""
        a.update_task(tid, refused=why[:300])
        try:
            a.send({"type": "outbox_refused", "task": tid, "error": why[:300]})
        except (OSError, VMError):
            pass

    def _outbox_part(self, a: Agent, typ: str, msg: dict) -> None:
        """Research findings in pieces (newer runners); the old single "outbox" message still works."""
        tid = msg.get("task")
        if not isinstance(tid, str) or not tid or len(tid) > 32:
            return
        if typ == "outbox_chunk":
            st = a.outbox_parts.get(tid)
            if msg.get("seq") == 0:  # a delivery starts (again)
                with a.lock:
                    running = any(t["id"] == tid and t.get("state") == "running" for t in a.meta.get("tasks", []))
                if not running:  # only the task that is running delivers, one delivery at a time
                    a.set(error="Research findings for a task that isn't running were dropped.")
                    self.refuse_outbox(a, tid, "the task isn't running")
                    return
                if a.outbox_parts:
                    a.set(error="Earlier research findings were cut off and dropped.")
                a.outbox_parts.clear()
                st = a.outbox_parts[tid] = {"seq": 0, "buf": bytearray(), "sha": hashlib.sha256()}
            if st is None:
                return
            try:
                if msg.get("seq") != st["seq"] or not isinstance(msg.get("data"), str):
                    raise ValueError("out of order")
                data = base64.b64decode(msg["data"], validate=True)
            except (binascii.Error, ValueError):
                a.outbox_parts.pop(tid, None)
                a.set(error="Research findings arrived out of order and were dropped.")
                self.refuse_outbox(a, tid, "they arrived out of order")
                return
            st["seq"] += 1
            st["buf"] += data
            st["sha"].update(data)
            if len(st["buf"]) > OUTBOX_MAX:
                a.outbox_parts.pop(tid, None)
                a.set(error="Research findings were larger than the panel accepts and were dropped.")
                self.refuse_outbox(a, tid, "they are larger than the panel accepts")
            return
        st = a.outbox_parts.pop(tid, None)
        if st is None:
            return
        try:
            if msg.get("chunks") != st["seq"] or msg.get("sha256") != st["sha"].hexdigest():
                raise ValueError("incomplete")
            files = self.redact_obj(a, json.loads(bytes(st["buf"]).decode("utf-8")))
        except (ValueError, UnicodeError):
            a.set(error="Research findings arrived incomplete and were dropped.")
            self.refuse_outbox(a, tid, "they arrived incomplete")
            return
        self._hook("on_outbox", a, {"task": tid, "files": files})

    def _hidden(self, a: Agent) -> list:
        """Secrets to keep out of anything stored or shown: what this VM was given and what the panel
        holds. Only values of 8 characters or more, so ordinary words are never touched."""
        try:
            known = set(self.known_secrets())
        except Exception:  # noqa: BLE001 - never let this stop the logs
            known = set()
        return sorted({v for v in a.hide | known if isinstance(v, str) and len(v) >= 8}, key=len, reverse=True)

    def redact_bytes(self, a: Agent, data: bytes) -> bytes:
        return self.redact_stream(a, data)[0]

    def redact_stream(self, a: Agent, data: bytes, hold: bool = False) -> tuple[bytes, bytes]:
        out, rest = hide_secrets(data.decode("utf-8", "surrogateescape"), self._hidden(a), hold)
        return out.encode("utf-8", "surrogateescape"), rest.encode("utf-8", "surrogateescape")

    def redact_obj(self, a: Agent, obj, hidden=None):
        """Secrets out of every string of a parsed message, keys too.  The raw line can't be
        trusted for this: in JSON a secret with a quote, a backslash or non-ASCII is escaped."""
        hidden = self._hidden(a) if hidden is None else hidden
        if isinstance(obj, str):
            return hide_secrets(obj, hidden)[0]
        if isinstance(obj, list):
            return [self.redact_obj(a, x, hidden) for x in obj]
        if isinstance(obj, dict):
            return {self.redact_obj(a, k, hidden): self.redact_obj(a, v, hidden) for k, v in obj.items()}
        return obj

    @staticmethod
    def _boot_mark(a: Agent, step: str) -> None:
        """When a step of this start began, by the panel's own clock (once per step; the VM only decides when
        its own steps arrive, which moves nothing but these numbers)."""
        with a.lock:
            if a.meta.get("state") == "booting" and a.boot_marks and all(k != step for k, _ in a.boot_marks):
                a.boot_marks.append((step, time.monotonic()))

    def _on_guest(self, a: Agent, msg: dict) -> None:
        typ = msg.get("type")
        if not isinstance(typ, str):
            return
        if typ == "moved":
            if a.move_step and msg.get("step") == a.move_step and a.move_result is None:
                num = lambda k: msg.get(k) if type(msg.get(k)) is int and msg.get(k) >= 0 else 0  # noqa: E731
                a.move_result = {"ok": msg.get("ok") is True, "error": str(msg.get("error") or "")[:300],
                                 "bytes": num("bytes"), "entries": num("entries"), "skipped": num("skipped")}
            return
        if typ == "ready" and a.move_step:  # a runner that doesn't know the move booted as usual instead
            if a.move_result is None:
                a.move_result = {"ok": False, "error": "the VM started as usual instead of moving the home folder"}
            threading.Thread(target=self.kill, args=(a,), daemon=True).start()
            return
        if typ == "ready":
            a.can_deliver = msg.get("deliver") is True and a.meta.get("profile") != "research"
            a.ready.set()
            with a.lock:
                if a.meta["state"] == "booting":
                    a.meta["state"] = "ready"
                a.meta["boot_phase"] = None
                st = msg.get("selftest")
                if isinstance(st, dict):
                    a.meta["selftest"] = {str(k)[:200]: bool(v) for k, v in list(st.items())[:20]}
                # the self-test's own refused requests (example.com and the like) happen in this window.
                # Only the first ready of a start counts: a guest that sends it again can't move either.
                start = a.meta.get("boot_started")
                if isinstance(start, (int, float)) and a.meta.get("boot_secs") is None:
                    a.meta["boot_secs"] = max(0, round(time.time() - start))  # "Started in 6 s"
                    a.boot_marks.append(("ready", time.monotonic()))  # the state is already ready here
                    a.meta["boot_times"] = boot_times(a.boot_marks)
                    print(f"agent {a.id}: started in {a.meta['boot_secs']} s ("
                          + " · ".join(f"{name} {secs} s" for name, secs in a.meta["boot_times"]) + ")", flush=True)
                    win = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1))]
                    a.meta["selftest_windows"] = (a.meta.get("selftest_windows") or [])[-19:] + [win]
                a.save_meta()
            # synchronous: pending /inbox deliveries must reach the guest before
            # a queued task that refers to them (the control port is ordered)
            fn = self.hooks.get("on_ready")
            if fn:
                self._safe_hook(fn, (a,))
            self.dispatch_next(a)
        elif typ == "status":
            if not isinstance(msg.get("task"), str):
                return
            tid = msg["task"][:32]
            state = msg.get("state")
            upd = {}
            with a.lock:
                # only the task running now: an end for any other would free the agent for the next
                # task while this one runs on, and a "running" would bring back an ended one
                rec = next((t for t in a.meta.get("tasks", []) if t["id"] == tid), None)
                if rec is None or rec.get("state") != "running":
                    return
                if state in ("running", "done", "failed", "interrupted"):
                    upd["state"] = state
                    if state == "running" and not rec.get("started"):
                        upd["started"] = _now()  # the chat shows how long it has been working
                    elif state != "running":
                        upd["finished"] = _now()
                    if state == "done":
                        rec.pop("timed_out", None)  # it ended on its own after the time limit's interrupt
                for k in ("agent_rc", "outbox_files"):  # outbox_files: findings a research task tried to deliver
                    if type(msg.get(k)) is int:
                        upd[k] = msg[k]
                if isinstance(msg.get("error"), str):
                    upd["error"] = msg["error"][:500]
                repo = a.meta.get("repo") or ""
                pr = msg.get("pr")
                if isinstance(pr, str) and repo and re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/\d+", pr):
                    upd["pr"] = pr
                    a.set(pr=pr)
                br = msg.get("branch")
                if isinstance(br, str) and self.branch_ok(a, br):
                    upd["branch"] = br
                    a.set(branch=br)
                a.update_task(tid, **upd)
            if state == "failed":
                self._attach_failed(a, tid)
            if state in ("done", "failed", "interrupted"):
                self._disarm_backstop(a, tid)
                with a.lock:
                    if a.meta["state"] == "busy":
                        a.meta["state"] = "ready"
                    a.save_meta()
                self.dispatch_next(a)
                # not on this thread: the hook may read files from the VM, whose answers arrive here
                threading.Thread(target=self._after_task, args=(a, tid), daemon=True).start()
        elif typ == "summary":
            self._on_summary(a, msg.get("text"))
        elif typ == "context":
            self._on_context(a, msg)
        elif typ == "policy_applied":
            a.set(policy_applied_at=_now())
        elif typ == "outbox" and a.meta.get("profile") == "research":
            self._hook("on_outbox", a, msg)
        elif typ in ("outbox_chunk", "outbox_end") and a.meta.get("profile") == "research":
            self._outbox_part(a, typ, msg)
        elif typ == "research_requests" and a.meta.get("profile") != "research":
            self._hook("on_requests", a, msg)
        elif typ == "questions":  # the agent's ~/ask/ files (newer runners)
            self._hook("on_questions", a, msg)
        elif typ == "inbox_ok":
            pass
        elif typ == "phase" and msg.get("phase") in ("policy", "memory", "selftest"):
            if a.meta.get("state") == "booting":
                a.set(boot_phase=msg["phase"])
                self._boot_mark(a, msg["phase"])
        elif typ == "chat":
            self._on_chat(a, msg)
        elif typ == "plugins":  # how installing the agent's plugins went (newer runners)
            self._on_plugins(a, msg)
        elif typ == "export_chunk":
            self._export_chunk(a, msg)
        elif typ == "export_end":
            self._export_end(a, msg)
        elif typ == "export_failed":
            self._export_fail(a, str(msg.get("id", "")), "the VM said: " + str(msg.get("error", ""))[:300])
        elif typ in ("files_list", "file_chunk", "file_end", "file_failed"):
            self._fileop_msg(a, typ, msg)
        elif typ == "live":  # the running task's Claude Code takes messages while it works
            with a.lock:
                rec = next((t for t in a.meta.get("tasks", []) if t["id"] == msg.get("task") and t.get("state") == "running"), None)
                if rec is not None and a.can_deliver and not rec.get("live"):  # a VM repeating it writes nothing
                    rec["live"] = True
                    a.save_meta()
        elif typ == "turn_over":  # the turn is over, but background work runs on and its session still takes messages
            if isinstance(msg.get("task"), str):
                threading.Thread(target=self._deliver_queued, args=(a, msg["task"]), daemon=True).start()
        elif typ == "deliver_ack":
            mid = str(msg.get("id", ""))
            with a.lock:  # Send now gives up waiting under the lock, so the answer counts only before that
                w = a.delivers.get(mid)
                if not w or msg.get("task") != w["task"] or msg.get("result") not in ("delivered", "closing", "unsupported"):
                    return
                w["result"] = msg["result"]
                if w["result"] == "delivered":  # here, in channel order, so a report on it that follows finds it
                    a.update_task(mid, state="delivered", into=w["task"], started=_now())
            if w["result"] == "delivered":
                self._chat_append(a, w["task"], [{"k": "you", "msg": mid, "text": w["text"][:MESSAGE_KEPT]}])
            w["event"].set()
        elif typ == "delivery":
            self._delivery_lost(a, msg)
        elif typ == "interrupt_ack":
            w = a.interrupts.get(str(msg.get("task", "")))
            if w and msg.get("result") in ("stopping", "finishing", "idle"):
                w["result"] = msg["result"]
                w["event"].set()
        elif typ == "outbox_images":  # images the agent put in ~/outbox during a task: the chat shows them
            files = msg.get("files")
            if isinstance(files, list) and isinstance(msg.get("task"), str):
                ok = [f for f in files[:8] if isinstance(f, str) and len(f) < 300 and ".." not in f.split("/")
                      and re.fullmatch(r"[A-Za-z0-9._ @+=,()-][A-Za-z0-9._ @+=,()/-]*", f)
                      and f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))]
                if ok:
                    a.update_task(str(msg.get("task", ""))[:32], images=ok)

    def _on_summary(self, a: Agent, text) -> None:
        line = clean_summary(text, lambda s: self.redact_obj(a, s))
        if not line:
            return
        with a.lock:
            a.summary_next = line
            if a.summary_timer:
                return  # the waiting one takes the newest text
            wait = a.summary_at + SUMMARY_GAP - time.monotonic()
            if wait > 0:
                a.summary_timer = threading.Timer(wait, self._store_summary, (a,))
                a.summary_timer.daemon = True
                a.summary_timer.start()
                return
        self._store_summary(a)

    @staticmethod
    def _on_context(a: Agent, msg: dict) -> None:
        """How full the running task's conversation is (newer runners): two numbers, checked, and
        nothing else from the message kept.  Saved at most every CONTEXT_SAVE_GAP seconds."""
        used, window = msg.get("used"), msg.get("window")
        if not _tokens(used) or (window is not None and not (_tokens(window) and window >= 1000)):
            return
        with a.lock:
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == msg.get("task") and t.get("state") == "running"), None)
            if rec is None:
                return
            # a model server's own word on its window beats the program's guess for a model it doesn't know
            ctx = {"used": used, "window": rec.get("context_window") or window or (a.meta.get("context") or {}).get("window")}
            rec["context"] = a.meta["context"] = ctx
            if time.monotonic() - a.context_saved >= CONTEXT_SAVE_GAP:
                a.context_saved = time.monotonic()
                a.save_meta()

    @staticmethod
    def _store_summary(a: Agent) -> None:
        with a.lock:
            a.summary_timer = None
            line, a.summary_next = a.summary_next, None
            if line is None:
                return
            a.summary_at = time.monotonic()
            if line != a.meta.get("summary"):
                a.meta["summary"] = line
                a.save_meta()

    def _after_task(self, a: Agent, tid: str) -> None:
        """After each task: the server's hook (project notes), then Stop After Each Task,
        only once nothing else is waiting, so the hook still finds the VM running."""
        fn = self.hooks.get("after_task")
        if fn:
            self._safe_hook(fn, (a, tid))
        with a.lock:
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == tid), {})
            # you stopped the task, most likely to say something else, so the VM stays on
            idle = a.meta.get("auto_stop") and a.meta["state"] == "ready" and not a.queue and not rec.get("by_you")
        if idle:
            self.stop(a.id)

    def send_project_files(self, a: Agent, files: list[dict]) -> None:
        """A project's files into the VM, read-only at /attachments/project/, through the same
        checked transfer as attachments.  The sync message lets the runner drop removed ones."""
        try:
            for f in files:
                self._send_attachment(a, "project", f)
            a.send({"type": "project_sync", "names": [f["name"] for f in files]})
        except (OSError, VMError):
            pass  # stopped meanwhile: it gets them at its next start

    def send_skill_files(self, a: Agent, files: list[dict]) -> None:
        """An agent's skills into the VM, read-only at /attachments/skills/<name>.tar, through the same checked
        transfer as attachments.  The runner unpacks them before the next task.  The sync message lets it drop
        the ones taken away (an older runner ignores it, and the task's list still decides what's installed)."""
        try:
            for f in files:
                self._send_attachment(a, "skills", f)
            a.send({"type": "skills_sync", "names": [f["name"] for f in files]})
        except (OSError, VMError):
            pass  # stopped meanwhile: it gets them at its next start

    def _hook(self, name: str, *args) -> None:
        fn = self.hooks.get(name)
        if fn:
            # never block the control reader on panel logic
            threading.Thread(target=self._safe_hook, args=(fn, args), daemon=True).start()

    @staticmethod
    def _safe_hook(fn, args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()

    # -- tasks ------------------------------------------------------------------
    def new_task(self, aid: str, task: dict, *, queue: bool = False, record: dict | None = None) -> dict:
        """`task` holds instructions, agent argv, secrets env etc.  The record
        kept in meta never includes the secrets.  With queue=True the task
        waits (in RAM) until the agent is ready."""
        a = self.get(aid)
        tid = _secrets.token_hex(4)
        task = dict(task, id=tid)
        text = task.get("summary", task["instructions"])
        rec = {"id": tid, "created": _now(), "instructions": text[:MESSAGE_KEPT],
               "state": "queued", "branch": None, "pr": None, "new_branch": bool(task.get("new_branch")),
               "continue": bool(task.get("continue")), "model": task.get("model") or task.get("local_model"),
               "effort": task.get("effort")}
        if _tokens(task.get("context_window")) and task["context_window"] >= 1000:  # what a model server said it gives
            rec["context_window"] = task["context_window"]
        rec.update(record or {})
        if len(text) > MESSAGE_KEPT:  # the whole message, for "Show More" (the agent gets all of it anyway)
            os.makedirs(self._messages_dir(a), mode=0o700, exist_ok=True)
            _atomic_write(self._message_path(a, tid), text.encode())
            rec["full_message"] = True
        with a.lock:
            a.meta.setdefault("tasks", []).append(rec)
            a.meta["idle_since"] = None  # Stop When Idle: a message restarts the clock
            dropped = a.meta["tasks"][:-TASKS_KEPT]
            a.meta["tasks"] = a.meta["tasks"][-TASKS_KEPT:]
            a.save_meta()
            if queue:
                a.queue.append(task)
        for old in dropped:  # a message goes with its task record
            if old.get("full_message"):
                _rm(self._message_path(a, old["id"]))
        if queue:
            self.dispatch_next(a)
        else:
            try:
                self._dispatch(a, task)
            except VMError:
                a.update_task(tid, state="failed", error="agent not ready")
                (task.get("env") or {}).clear()
                _drop_attachments(task)
                raise
        return task

    def cancel_task(self, aid: str, tid: str) -> dict:
        """Take a message that is still waiting out of the agent's queue.  Under the agent's lock,
        like the dispatch, so a message is either cancelled or sent, never both."""
        a = self.get(aid)
        with a.lock:
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == tid), None)
            task = next((t for t in a.queue if t["id"] == tid), None)
            if rec is None:
                raise VMError("no such task")
            if task is None:
                raise VMError("It already started." if rec.get("state") != "cancelled" else "It's already cancelled.")
            a.queue.remove(task)
            rec.update(state="cancelled", finished=_now())
            a.save_meta()
        (task.get("env") or {}).clear()
        _drop_attachments(task)
        return dict(rec)

    def interrupt(self, aid: str) -> str:
        """End the running task's Claude Code run but keep the VM on: the task finishes up (commit,
        push, pull request) as it does for Stop.  Returns "stopping", or "finishing" when Claude is
        already done and only the commit and push are left, which aren't cut off."""
        a = self.get(aid)
        with a.lock:
            rec = next((t for t in reversed(a.meta.get("tasks", [])) if t.get("state") == "running"), None)
            if a.meta.get("state") != "busy" or rec is None:
                raise VMError("Nothing is running right now.")
            w = a.interrupts.setdefault(rec["id"], {"event": threading.Event(), "result": None})
            # before asking, so the task's end (which may come first) already knows it was you
            rec["by_you"] = True
        try:
            a.send({"type": "interrupt", "task": rec["id"]})
            if not w["event"].wait(INTERRUPT_WAIT):
                raise VMError("The VM didn't answer, so the task wasn't stopped. It may be stuck, for example out of "
                              "memory, or its image too old to stop a task on its own. Stop powers it off. A persistent "
                              "agent keeps its files and conversation.")
        finally:
            with a.lock:
                a.interrupts.pop(rec["id"], None)
                if w["result"] != "stopping":
                    rec.pop("by_you", None)
                a.save_meta()
        if w["result"] == "idle":
            raise VMError("Nothing is running right now.")
        return w["result"]

    def _deliver_queued(self, a: Agent, tid: str) -> None:
        """After a turn that left a background command or agent running, the first queued message goes
        into that session, as Send now would, when it can go there.  Nothing is ever stopped for it."""
        live = a.live_run()
        with a.lock:
            nxt = a.queue[0]["id"] if live and live["id"] == tid and a.queue else None
        if nxt:
            try:
                self.send_now(a.id, nxt, deliver_only=True)
            except VMError:
                pass

    def send_now(self, aid: str, tid: str, deliver_only: bool = False) -> str:
        """A queued message goes first.  While a task runs, it goes into that run when the runner and
        its Claude Code take messages mid-run ("delivered"), so background work goes on.  Otherwise
        the running task is interrupted so it runs next, unless deliver_only (you weren't asked) or
        delivering was tried and failed: then nothing is stopped ("not_delivered")."""
        a = self.get(aid)
        with a.lock:
            task = next((t for t in a.queue if t["id"] == tid), None)
            if task is None and tid in a.delivers:  # a click before the first one's answer came
                raise VMError("It's on its way into the running task. It runs next if Claude doesn't take it there.")
            if task is None:
                raise VMError("It already started." if any(t["id"] == tid for t in a.meta.get("tasks", [])) else "no such task")
            a.queue.remove(task)
            live = a.live_run()
            if live and deliverable(task, live):  # out of the queue meanwhile, so it can't also start as a task
                w = a.delivers[tid] = {"event": threading.Event(), "result": None, "task": live["id"],
                                       "text": task.get("summary", task["instructions"])}
            else:
                live = None
                a.queue.insert(0, task)
        if live:
            result = self._deliver(a, task, live, w)
            if result == "delivered":
                return "delivered"
            with a.lock:  # Stop may have come while it waited: then nothing runs it, and its secrets go
                rec = next((t for t in a.meta.get("tasks", []) if t["id"] == tid), None)
                back = a.running and rec is not None and rec.get("state") == "queued"
                if back:
                    a.queue.insert(0, task)
            if not back:
                (task.get("env") or {}).clear()
                _drop_attachments(task)
                raise VMError("The agent stopped before your message got to it.")
            self.dispatch_next(a)  # the task may have ended while it waited
            # never a stop you weren't asked about: it runs next, and "closing" means the run is ending
            return "finishing" if result == "closing" else "not_delivered"
        with a.lock:
            busy = a.meta.get("state") == "busy"
        if not busy:
            self.dispatch_next(a)
            with a.lock:
                return "moved" if task in a.queue else "sent"  # moved: it runs when the agent is ready
        if deliver_only:
            return "not_delivered"
        try:
            return self.interrupt(aid)
        except VMError:  # an older runner, or the task just ended: it's first in line either way
            return "moved"

    def _deliver(self, a: Agent, task: dict, live: dict, w: dict) -> str | None:
        """Your message into the running session.  Delivered, it becomes part of that task: its record
        says where it went, and the chat shows it there, written by the panel (a VM can't).  Both
        happen when the runner's answer comes in."""
        try:
            a.send({"type": "deliver", "task": live["id"], "id": task["id"], "text": w["text"]})
            w["event"].wait(DELIVER_WAIT)
        except (OSError, VMError):
            pass
        finally:
            with a.lock:
                a.delivers.pop(task["id"], None)
        if w["result"] != "delivered":
            return w["result"]
        (task.get("env") or {}).clear()
        _drop_attachments(task)
        return "delivered"

    def _delivery_lost(self, a: Agent, msg: dict) -> None:
        """Claude Code didn't answer a message you sent into the running task: the chat says so, once,
        in the panel's words.  Only for a message that went into the task the VM names."""
        text = DELIVERY_LOST.get(msg.get("state")) if isinstance(msg.get("state"), str) else None
        with a.lock:
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == msg.get("id") and t.get("state") == "delivered"), None)
            if text is None or rec is None or rec.get("into") != msg.get("task") or rec.get("lost"):
                return
            rec["lost"] = msg["state"]
            a.save_meta()
        line = " ".join(rec.get("instructions", "").split())
        self._chat_append(a, rec["into"], [{"k": "status", "text": text.format(line if len(line) <= 60 else line[:59] + "…")}])

    def move_up(self, aid: str, tid: str) -> int:
        """A queued message swaps places with the one before it.  Returns its place in the queue (0 runs next)."""
        a = self.get(aid)
        with a.lock:
            i = next((i for i, t in enumerate(a.queue) if t["id"] == tid), None)
            if i is None:
                raise VMError("It already started." if any(t["id"] == tid for t in a.meta.get("tasks", [])) else "no such task")
            if i:
                a.queue[i - 1], a.queue[i] = a.queue[i], a.queue[i - 1]
            return max(0, i - 1)

    @staticmethod
    def _messages_dir(a: Agent) -> str:
        return os.path.join(a.paths.vm_dir, "messages")

    def _message_path(self, a: Agent, tid: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{8}", tid):
            raise VMError("bad task id")
        return os.path.join(self._messages_dir(a), tid + ".txt")

    def read_message(self, aid: str, tid: str) -> str:
        """The whole of your message for a task, as you wrote it."""
        a = self.get(aid)
        with a.lock:
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == tid), None)
        if rec is None:
            raise VMError("no such task")
        if not rec.get("full_message"):
            return rec.get("instructions", "")
        try:
            with os.fdopen(os.open(self._message_path(a, tid), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC),
                           encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return rec.get("instructions", "")

    def dispatch_next(self, a: Agent) -> None:
        with a.lock:
            if not a.queue or a.meta["state"] != "ready" or not a.running:
                return
            task = a.queue.pop(0)
        try:
            self._dispatch(a, task)
        except VMError as e:
            a.update_task(task["id"], state="failed", error=str(e))

    def _dispatch(self, a: Agent, task: dict) -> None:
        with a.lock:
            if a.meta["state"] != "ready" or not a.running:
                raise VMError(f"agent is {a.meta['state']}, not ready")
            a.meta["state"] = "busy"
            a.meta["dispatched"] = a.meta.get("dispatched", 0) + 1
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == task["id"]), {})
            rec["order"] = a.meta["dispatched"]  # the order tasks ran in, finer than their start time
            rec["state"] = "running"  # before it's sent: the VM's own status for it may come back at once
            a.meta["idle_since"] = None
            if a.meta.get("context"):  # the conversation this task is in: a new one starts empty
                back = next((t for t in a.meta["tasks"] if t["id"] == rec.get("resumed_from")), None)
                if back is not None:
                    a.meta["context"] = back.get("context") or {"used": 0, "window": None}
                elif not task.get("continue"):
                    a.meta["context"] = {"used": 0, "window": None}
            a.save_meta()
            a.hide = a.hide | frozenset(str(v) for v in (task.get("env") or {}).values())
        models = task.pop("proxy_models", None)
        if models is not None:  # the model server this task's model is on, now that it runs (none for Claude's)
            self.set_models(a, models)
        limit = task.get("timeout_s")
        if type(limit) is int and limit > 0:
            self._arm_backstop(a, task["id"], limit + BACKSTOP_GRACE)
        try:
            # attached files first: the control port is ordered, so they are in
            # place before the task starts
            for att in task.get("attachments") or []:
                self._send_attachment(a, task["attachment_set"], att)
            a.send(dict({k: v for k, v in task.items() if k != "attachments"}, type="task"))
        except (OSError, VMError) as e:
            self._disarm_backstop(a, task["id"])
            a.set(state="ready" if a.running else "stopped")
            raise VMError(f"could not send task: {e}") from None
        finally:
            (task.get("env") or {}).clear()  # secrets only live until sent
            _drop_attachments(task)

    def _arm_backstop(self, a: Agent, tid: str, seconds: float) -> None:
        timer = threading.Timer(seconds, self._backstop, args=(a, tid))
        timer.daemon = True
        self._disarm_backstop(a, tid)
        with a.lock:
            a.backstops[tid] = timer  # one per task: arming never drops another task's
        timer.start()

    @staticmethod
    def _disarm_backstop(a: Agent, tid: str | None = None) -> None:
        with a.lock:
            for t in [tid] if tid else list(a.backstops):
                timer = a.backstops.pop(t, None)
                if timer:
                    timer.cancel()

    @staticmethod
    def _still_running(a: Agent, tid: str) -> bool:
        with a.lock:
            return not a.deleted and a.running and \
                any(t["id"] == tid and t.get("state") == "running" for t in a.meta.get("tasks", []))

    def _backstop(self, a: Agent, tid: str) -> None:
        """The task outlived its time limit: the VM didn't end it.  End it from here."""
        with a.lock:
            if not self._still_running(a, tid):
                return
            a.update_task(tid, timed_out=True)  # the task says the time limit ended it
        try:
            a.send({"type": "interrupt", "task": tid})
        except (OSError, VMError):
            pass
        end = time.monotonic() + BACKSTOP_WAIT
        while time.monotonic() < end and self._still_running(a, tid):
            time.sleep(0.2)
        with a.lock:  # the check and the stop at once, as in stop_if_idle: a task sent meanwhile is never stopped
            if not self._still_running(a, tid):
                return
            a.limit_stop = True
            a.set(state="stopping")
        threading.Thread(target=self._stop_sync, args=(a,), daemon=True).start()

    @staticmethod
    def _send_attachment(a: Agent, set_id: str, att: dict) -> None:
        h, seq = hashlib.sha256(), 0
        with open(att["path"], "rb") as f:
            while True:
                chunk = f.read(ATTACH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                a.send({"type": "attach_chunk", "set": set_id, "name": att["name"], "seq": seq,
                        "data": base64.b64encode(chunk).decode()})
                seq += 1
        a.send({"type": "attach_end", "set": set_id, "name": att["name"], "chunks": seq, "sha256": h.hexdigest()})

    def send_inbox(self, aid: str, did: str, title: str, files: list[dict]) -> bool:
        """Deliver research results into a running agent's /inbox/<did>/.
        Returns False if the agent isn't running (the caller queues it)."""
        a = self.get(aid)
        if not a.running or not a.ready.is_set():
            return False
        a.send({"type": "inbox", "id": did, "title": title, "files": files})
        return True

    def _attach_failed(self, a: Agent, tid: str) -> None:
        with a.lock:
            rec = next((t for t in a.meta.get("tasks", []) if t["id"] == tid), None)
            if rec and rec.get("attach") and a.meta.get("repo") == rec["attach"]:
                a.meta.update(repo=None, repo_in_scratch=False, branch=None, pr=None)
                a.save_meta()

    # -- chat (Claude's events per task, from the VM) ------------------------------
    def _on_plugins(self, a: Agent, msg: dict) -> None:
        """The VM says how each plugin's install went.  Untrusted: only plugins the agent has, known states,
        short plain text with secrets hidden, and an install command only with its sha256."""
        results = msg.get("results")
        if not isinstance(results, list):
            return
        mine = set(a.meta.get("plugins") or [])
        text = lambda v, n: hide_secrets(" ".join(clean_text(str(v or "")[:2000], n).split()), list(a.hide))[0][:n]  # noqa: E731
        got = dict(a.meta.get("plugin_state") or {})
        for r in results[:50]:
            if not isinstance(r, dict) or r.get("name") not in mine or r.get("state") not in ("installed", "failed", "needs_command"):
                continue
            e = {"state": r["state"], "at": _now()}
            if r["state"] == "failed":
                e["message"] = text(r.get("message"), 300)
            elif r["state"] == "needs_command":
                if not isinstance(r.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", r["sha256"]):
                    continue
                e.update(sha256=r["sha256"], command=text(r.get("command"), 500))
            got[r["name"]] = e
        a.set(plugin_state={k: v for k, v in got.items() if k in mine})

    def _on_chat(self, a: Agent, msg: dict) -> None:
        if not isinstance(msg.get("task"), str):
            return
        tid = msg["task"][:32]
        events = msg.get("events")
        with a.lock:
            known = any(t["id"] == tid for t in a.meta.get("tasks", []))
        if not known or not isinstance(events, list):
            return
        clean = [e for e in (_clean_chat_event(x) for x in events[:200]) if e]
        if not clean:
            return
        for e in clean:
            if e["k"] == "done" and e.get("sid"):  # lets you go back to this conversation later
                a.update_task(tid, session=e["sid"])
        self._chat_append(a, tid, clean)

    def _chat_append(self, a: Agent, tid: str, clean: list) -> None:
        path = os.path.join(a.paths.vm_dir, "chat.jsonl")
        with a.chat_lock:
            self._chat_load(a)
            try:
                size = os.path.getsize(path)
            except FileNotFoundError:
                size = 0
            if size > CHAT_CAP:
                return  # full: the agent log still has everything
            ts = _now()
            lines = []
            for e in clean:
                a.chat_seq += 1
                rec = dict(e, seq=a.chat_seq, task=tid, ts=ts)
                a.chat_recent.append(rec)
                lines.append(json.dumps(rec))
            del a.chat_recent[:-CHAT_RECENT]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write("\n".join(lines) + "\n")

    @staticmethod
    def _chat_load(a: Agent) -> None:
        """Call with a.chat_lock held."""
        if a.chat_recent is not None:
            return
        recent, seq = [], 0
        try:
            with os.fdopen(os.open(os.path.join(a.paths.vm_dir, "chat.jsonl"),
                                   os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and isinstance(e.get("seq"), int):
                        recent.append(e)
                        seq = max(seq, e["seq"])
                        del recent[:-CHAT_RECENT]
        except FileNotFoundError:
            pass
        a.chat_recent, a.chat_seq = recent, seq

    def read_chat(self, aid: str, after: int = 0, limit: int = 2000) -> dict:
        """The newest events (after=0), or the ones after a given number."""
        a = self.get(aid)
        with a.chat_lock:
            self._chat_load(a)
            out = [e for e in a.chat_recent if e["seq"] > after]
        out = out[-limit:] if after == 0 else out[:limit]
        return {"events": out, "last": out[-1]["seq"] if out else max(after, a.chat_seq if after else 0)}

    # -- viewing files (VM -> panel memory -> your browser; nothing is written on the host) --
    def _file_source(self, a: Agent, what: str, path: str) -> str:
        if not a.running or not a.ready.is_set():
            raise VMError("Start the agent to see its files. They're inside its VM.")
        ok = ["outbox", "attachments"] + (["repo"] if a.meta.get("repo") else []) + ([] if a.meta.get("profile") == "research" else ["scratch"])
        if what not in ok:
            raise VMError(f"source must be one of {', '.join(ok)}")
        parts = [p for p in str(path).split("/") if p not in ("", ".")]
        if len(path) > 4096 or any(p == ".." or p == ".git" or "\x00" in p for p in parts):
            raise VMError("bad path")
        return "/".join(parts)

    def _fileop(self, a: Agent, msg: dict, timeout: float) -> dict:
        rid = _secrets.token_hex(6)
        st = {"event": threading.Event(), "chunks": [], "bytes": 0, "seq": 0, "sha": hashlib.sha256(),
              "cap": msg.get("max_bytes", 0), "result": None}
        with a.lock:
            if len(a.fileops) >= 8:
                raise VMError("Too many file requests at once. Try again.")
            a.fileops[rid] = st
        try:
            a.send({**msg, "id": rid})
            if not st["event"].wait(timeout):
                raise VMError("the VM didn't answer in time")
            return st["result"]
        finally:
            with a.lock:
                a.fileops.pop(rid, None)

    def _fileop_msg(self, a: Agent, typ: str, msg: dict) -> None:
        st = a.fileops.get(str(msg.get("id", "")))
        if not st or st["event"].is_set():
            return
        done = lambda r: (st.__setitem__("result", r), st["event"].set())  # noqa: E731
        if typ == "files_list":
            if msg.get("error"):
                return done({"error": str(msg["error"])[:300]})
            ents = msg.get("entries")
            if not isinstance(ents, list):
                return done({"error": "bad listing from the VM"})
            clean = []
            num = lambda v: v if type(v) is int and 0 <= v < 1 << 62 else 0  # noqa: E731
            for e in ents[:1000]:
                if isinstance(e, dict) and isinstance(e.get("name"), str) and e.get("kind") in ("dir", "file", "link", "other") \
                        and "/" not in e["name"] and e["name"] not in (".", ".."):
                    clean.append({"name": e["name"][:255], "kind": e["kind"], "size": num(e.get("size")),
                                  "mtime": num(e.get("mtime"))})
            return done({"entries": clean, "truncated": bool(msg.get("truncated"))})
        if typ == "file_failed":
            return done({"error": "the VM said: " + str(msg.get("error", ""))[:300]})
        if typ == "file_chunk":
            if msg.get("seq") != st["seq"] or not isinstance(msg.get("data"), str):
                return done({"error": "transfer out of order"})
            try:
                data = base64.b64decode(msg["data"], validate=True)
            except (binascii.Error, ValueError):
                return done({"error": "bad data from the VM"})
            st["seq"] += 1
            st["bytes"] += len(data)
            if st["bytes"] > st["cap"]:
                return done({"error": f"larger than {st['cap']} bytes"})
            st["sha"].update(data)
            st["chunks"].append(data)
            return None
        if msg.get("chunks") != st["seq"] or msg.get("sha256") != st["sha"].hexdigest():
            return done({"error": "transfer incomplete"})
        return done({"data": b"".join(st["chunks"]), "sha256": st["sha"].hexdigest()})

    def list_files(self, aid: str, what: str, path: str) -> dict:
        a = self.get(aid)
        rel = self._file_source(a, what, path)
        r = self._fileop(a, {"type": "list_files", "what": what, "path": rel}, 30)
        if r.get("error"):
            raise VMError(r["error"])
        return {"path": rel, **r}

    def read_file(self, aid: str, what: str, path: str, max_bytes: int) -> dict:
        a = self.get(aid)
        rel = self._file_source(a, what, path)
        if not rel:
            raise VMError("pick a file")
        r = self._fileop(a, {"type": "read_file", "what": what, "path": rel, "max_bytes": max_bytes}, 120)
        if r.get("error"):
            raise VMError(r["error"])
        return {"path": rel, **r}

    # -- exports (VM -> host, only when you press Export) -------------------------
    def start_export(self, aid: str, what: str, dest: str) -> dict:
        from . import exports as X
        a = self.get(aid)
        if what not in ("scratch", "outbox", "repo"):
            raise VMError("export source must be scratch, outbox or repo")
        if what == "repo" and not a.meta.get("repo"):
            raise VMError("this agent has no repository")
        try:
            dest = X.check_destination(dest)
        except X.ExportError as e:
            raise VMError(str(e)) from None
        if not a.running or not a.ready.is_set():
            raise VMError("Start the agent first. The files are inside its VM.")
        if a.exports:
            raise VMError("an export is already running for this agent")
        lim = self.cfg()["exports"]
        eid = _secrets.token_hex(4)
        tmp = os.path.join(a.paths.vm_dir, f"export-{eid}.tar")
        fh = os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600), "wb")
        st = {"tmp": tmp, "fh": fh, "seq": 0, "bytes": 0, "sha": hashlib.sha256(), "what": what, "dest": dest,
              "cap": lim["max_bytes"] + lim["max_files"] * 2048 + (1 << 20)}
        rec = {"id": eid, "what": what, "dest": dest, "state": "running", "created": _now()}
        with a.lock:
            a.exports[eid] = st
            a.meta.setdefault("exports", []).append(rec)
            a.meta["exports"] = a.meta["exports"][-20:]
            a.meta["export_dest"] = dest
            a.save_meta()
        try:
            a.send({"type": "export", "id": eid, "what": what, "max_bytes": st["cap"]})
        except (OSError, VMError) as e:
            self._export_fail(a, eid, f"could not ask the VM: {e}")
            raise VMError(f"could not ask the VM: {e}") from None
        return rec

    def _export_rec(self, a: Agent, eid: str, **kw) -> None:
        with a.lock:
            for r in a.meta.get("exports", []):
                if r["id"] == eid:
                    r.update(kw)
            a.save_meta()

    def _export_fail(self, a: Agent, eid: str, error: str) -> None:
        with a.lock:
            st = a.exports.pop(eid, None)
        if not st:
            return
        st["fh"].close()
        _rm(st["tmp"])
        self._export_rec(a, eid, state="failed", error=error[:500])

    def _export_chunk(self, a: Agent, msg: dict) -> None:
        eid = str(msg.get("id", ""))
        st = a.exports.get(eid)
        if not st:
            return
        if msg.get("seq") != st["seq"] or not isinstance(msg.get("data"), str):
            return self._export_fail(a, eid, "transfer out of order")
        try:
            data = base64.b64decode(msg["data"], validate=True)
        except (binascii.Error, ValueError):
            return self._export_fail(a, eid, "bad data from the VM")
        st["seq"] += 1
        st["bytes"] += len(data)
        if st["bytes"] > st["cap"]:
            return self._export_fail(a, eid, "export exceeds the size limit (settings: exports.max_bytes)")
        st["sha"].update(data)
        st["fh"].write(data)

    def _export_end(self, a: Agent, msg: dict) -> None:
        eid = str(msg.get("id", ""))
        st = a.exports.get(eid)
        if not st:
            return
        if msg.get("chunks") != st["seq"] or msg.get("sha256") != st["sha"].hexdigest():
            return self._export_fail(a, eid, "transfer incomplete")
        with a.lock:
            a.exports.pop(eid, None)  # the VM's part is done; stopping it no longer affects this export
        st["fh"].close()
        # extracting can take a while: keep the control reader free
        threading.Thread(target=self._export_extract, args=(a, eid, st), daemon=True).start()

    def _export_extract(self, a: Agent, eid: str, st: dict) -> None:
        from . import exports as X
        lim = self.cfg()["exports"]
        name = re.sub(r"[^A-Za-z0-9._-]", "_", a.meta.get("name", "agent"))[:48]
        sub = f"{name}-{st['what']}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        try:
            dest = X.check_destination(st["dest"])
            try:
                res = X.safe_extract_tar(st["tmp"], dest, sub, max_bytes=lim["max_bytes"],
                                         max_files=lim["max_files"])
            except FileExistsError:
                res = X.safe_extract_tar(st["tmp"], dest, f"{sub}-{eid}", max_bytes=lim["max_bytes"],
                                         max_files=lim["max_files"])
        except (X.ExportError, OSError) as e:
            _rm(st["tmp"])
            self._export_rec(a, eid, state="failed", error=f"rejected: {e}"[:500])
            return
        _rm(st["tmp"])
        self._export_rec(a, eid, state="done", path=res["path"], files=len(res["files"]), bytes=res["bytes"],
                         left_out=res["left_out"], finished=_now())

    def next_branch(self, aid: str, text: str = "") -> str:
        """A name for a new branch, from the task's first words (or the agent's id, as Settings say)."""
        a = self.get(aid)
        cfg = self.cfg()
        taken = self.branches(a) | {t.get("branch") for t in a.meta.get("tasks", [])} | {a.meta.get("branch")}
        with a.lock:
            a.meta["branch_seq"] = a.meta.get("branch_seq", 0) + 1
            a.save_meta()
            return C.new_branch_name(cfg, a.id, text, a.meta["branch_seq"], taken)

    def branches(self, a: Agent) -> set[str]:
        """The branches GitHub confirmed this agent created (the egress proxy keeps them)."""
        return load_branches(a.paths.branches_file)

    def branch_ok(self, a: Agent, name: str) -> bool:
        """A branch name the VM may say it's on: named the way Settings say, after its id, or its own.
        The VM is trusted with nothing here: the egress proxy decides what it may push."""
        if not re.fullmatch(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9._/-]{0,99}", name):
            return False
        cfg = self.cfg()
        pat = C.branch_pattern(cfg)
        return name.startswith(cfg["identity"]["branch_prefix"]) or bool(pat and re.fullmatch(pat, name)) \
            or C.is_id_branch(name, a.id) or name in self.branches(a)

    # -- policy -----------------------------------------------------------------
    def apply_policy(self, policy_for) -> int:
        """Push the policy to every running agent; `policy_for(agent)` builds
        each agent's own copy (MCP servers differ per agent)."""
        n = 0
        for a in list(self.agents.values()):
            if a.running and a.ready.is_set():
                try:
                    pol = policy_for(a)
                    a.send({"type": "policy", "policy": pol})
                    a.set(policy_version=pol.get("version"))
                    n += 1
                except (OSError, VMError):
                    pass
        return n

    # -- stop / reset / delete --------------------------------------------------
    def stop(self, aid: str) -> None:
        """Asynchronous: ask the runner to shut down (memory agents export
        their memory first), then fall back to ACPI powerdown and kill."""
        a = self.get(aid)
        if not a.running:
            return
        if a.move_step and a.move_result is None:  # Stop during a move: it goes back to the old disk
            a.move_result = {"ok": False, "error": "you stopped it"}
        a.set(state="stopping")
        threading.Thread(target=self._stop_sync, args=(a,), daemon=True).start()

    def stop_if_idle(self, aid: str, since: float, inbox_empty=lambda: True) -> bool:
        """Stop When Idle: stop only if, under the agent's lock, it's still in the idle stretch that ran out
        (since): up, ready, nothing queued, no transfer, no activity since (which clears idle_since)
        and still nothing in its inbox.  A message dispatched in between is never stopped."""
        a = self.get(aid)
        with a.lock:
            if not a.running or a.meta.get("state") != "ready" or a.queue or a.exports or a.fileops \
                    or a.meta.get("idle_since") != since or not inbox_empty():
                return False
            a.meta.update(state="stopping", idle_since=None)
            a.save_meta()
        threading.Thread(target=self._stop_sync, args=(a,), daemon=True).start()
        return True

    def _stop_sync(self, a: Agent) -> None:
        q = a.procs.get("qemu")
        if q is None:
            return
        if not self.closing:  # quitting cilly doesn't wait for a copy
            self.keep_outbox(a)
        try:
            a.send({"type": "shutdown"})
            q.wait(STOP_WAIT)
            return
        except (OSError, VMError, subprocess.TimeoutExpired):
            pass
        try:
            qmp(a.paths.qmp, "system_powerdown")
            q.wait(30)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        q.kill()

    def keep_outbox(self, a: Agent, wait: float = KEEP_OUTBOX_WAIT) -> str | None:
        """Before an agent that loses its disk stops (ephemeral, or ephemeral with memory): its ~/outbox
        into a new subfolder of the folder you chose for it, when there's anything in it.  The same
        checked export as the Export button.  Returns the export's id, or None when there was nothing
        to do.  The VM stops either way, once the copy is done, has failed or took too long."""
        dest = a.meta.get("keep_outbox")
        if not dest or a.meta.get("mode") not in ("ephemeral", "memory") or a.meta.get("profile") == "research" \
                or not a.ready.is_set():
            return None
        try:
            if not self.list_files(a.id, "outbox", "").get("entries"):
                return None
            eid = self.start_export(a.id, "outbox", dest)["id"]
        except VMError as e:
            with a.lock:
                a.meta.setdefault("exports", []).append({"id": _secrets.token_hex(4), "what": "outbox", "dest": dest,
                                                         "state": "failed", "created": _now(), "error": str(e)[:500]})
                a.meta["exports"] = a.meta["exports"][-20:]
                a.save_meta()
            return None
        end = time.monotonic() + wait
        while time.monotonic() < end:
            with a.lock:
                rec = next((r for r in a.meta.get("exports", []) if r["id"] == eid), {})
            if rec.get("state") != "running":
                return eid
            time.sleep(0.2)
        self._export_fail(a, eid, f"stopped after {int(wait // 60)} min, before the copy was done")
        return eid

    def kill(self, a: Agent) -> None:
        for key in ("qemu", "virtiofsd", "proxy"):
            q = a.procs.get(key)
            if q and q.poll() is None:
                q.kill()
                try:
                    q.wait(10)
                except subprocess.TimeoutExpired:
                    pass

    # -- move to a new image, keeping the home folder ---------------------------------
    def move_image(self, aid: str, image: str, *, policy: dict) -> None:
        """Move a stopped persistent agent to another VM image, keeping its home folder: its files,
        its conversations, caches and settings.  One boot packs the home folder onto a transfer disk,
        a second unpacks it into a fresh disk from the new image.  Both run as the agent user inside
        the VMs: this PC never opens what's packed.  The old disk stays until the second boot says
        it worked, and comes back if anything goes wrong.  Runs in the background."""
        a = self.get(aid)
        p = a.paths
        with a.lock:
            if a.running or a.meta["state"] in _ACTIVE or a.meta.get("move"):
                raise VMError("Stop the agent first.")
            if a.meta.get("mode") != "persistent" or not os.path.exists(p.disk):
                raise VMError("Only a persistent agent with a disk keeps its files. Others start from the new image anyway.")
            if image not in list_base_images():
                raise VMError("unknown VM image")
            if image == a.meta.get("base_image"):
                raise VMError("The agent already runs on this image.")
            need = 2 * os.stat(p.disk).st_blocks * 512 + (1 << 30)  # the packed home folder, then the new disk
            free = shutil.disk_usage(p.sandbox_dir).free
            if free < need:
                raise VMError(f"Not enough free space for the move: it needs about {need >> 30} GB, "
                              f"and {free >> 30} GB is free.")
            a.meta.update(move={"to": image, "from": a.meta.get("base_image"), "image": a.meta.get("image"), "step": 1},
                          error=None)
            a.save_meta()
        threading.Thread(target=self._move, args=(a, image, policy), name=f"move-{aid}", daemon=True).start()

    def _move(self, a: Agent, image: str, policy: dict) -> None:
        p = a.paths
        try:
            info = json.loads(subprocess.run(["qemu-img", "info", "--output=json", p.disk], check=True,
                                             capture_output=True, timeout=60).stdout)
            with open(p.transfer, "wb") as f:  # sparse: it takes only as much room as the home folder
                f.truncate(int(info.get("virtual-size") or 0) + (1 << 20))
            self._move_boot(a, "export", policy)
            os.replace(p.disk, p.disk_old)
            a.set(move=dict(a.meta["move"], step=2), image=image)  # the fresh disk comes from the new image
            self._move_boot(a, "import", policy)
            res = a.move_result or {}
            _rm(p.disk_old)
            self._move_over(a)
            a.set(move=None, moved={"from": a.meta["move"]["from"], "to": image, "at": _now(),
                                    "entries": res.get("entries", 0), "skipped": res.get("skipped", 0)})
        except Exception as e:  # noqa: BLE001
            self._move_over(a)
            with a.lock:
                self._undo_move(p, a.meta, f"Couldn't move to {image}: {e}.")
                a.save_meta()
        finally:
            _rm(p.transfer)
            a.move_step = a.move_result = None

    @staticmethod
    def _move_over(a: Agent) -> None:
        """Before the move counts as done (or undone): an agent started right after must boot as
        usual, not as a move step, and nothing should find the transfer disk still there."""
        _rm(a.paths.transfer)
        a.move_step = a.move_result = None

    def _move_boot(self, a: Agent, step: str, policy: dict) -> None:
        a.move_step, a.move_result = step, None
        self.start(a.id, allow=[], policy=policy)  # nothing to reach: this boot only moves files
        a.thread.join(MOVE_WAIT)
        if a.thread.is_alive():
            self.kill(a)
            a.thread.join(60)
            raise VMError("it took more than an hour")
        r = a.move_result
        if r is None:
            raise VMError(a.meta.get("error") or "the VM stopped before it was done")
        if not r["ok"]:
            raise VMError(r["error"] or "the VM couldn't move the files")

    @staticmethod
    def _undo_move(p: VMPaths, meta: dict, why: str) -> None:
        """Back to the old disk and image, as before the move.  Unless the move had worked and only
        its last step, forgetting the old disk, was cut short: then it's done."""
        mv = meta.get("move") or {}
        if os.path.exists(p.disk_old):
            _rm(p.disk)
            os.replace(p.disk_old, p.disk)
            meta.update(base_image=mv.get("from"), image=mv.get("image"),
                        error=f"{why} It's still on its old image, with all its files."[:500])
        elif mv.get("step") == 2 and meta.get("base_image") == mv.get("to"):
            meta["moved"] = {"from": mv.get("from"), "to": mv.get("to"), "at": _now(), "entries": 0, "skipped": 0}
        else:
            meta.update(image=mv.get("image"), error=f"{why} It's still on its old image, with all its files."[:500])
        _rm(p.transfer)
        meta["move"] = None
        if meta.get("state") in _ACTIVE:
            meta["state"] = "stopped"

    def reset_disk(self, aid: str) -> None:
        a = self.get(aid)
        if a.running:
            raise VMError("stop the agent first")
        if a.meta.get("move"):
            raise VMError("The agent is moving to a new image. Wait until it's done.")
        _rm(a.paths.disk)
        a.set(base_image=None, summary=None,  # its status.txt and conversations went with the disk
              context={"used": 0, "window": None} if a.meta.get("context") else None)

    def clear_memory(self, aid: str) -> None:
        from . import memory as M
        a = self.get(aid)
        if a.running:
            raise VMError("stop the agent first")
        M.clear(a.paths.vm_dir)
        a.set(memory=None)

    def delete(self, aid: str) -> None:
        a = self.get(aid)
        if a.meta.get("move"):
            raise VMError("The agent is moving to a new image. Wait until it's done.")
        self.kill(a)
        # Let the agent's thread finish its shutdown first, so it can't write
        # into the directory while (or after) we remove it.
        if a.thread is not None and a.thread is not threading.current_thread():
            a.thread.join(30)
        with a.lock:
            a.deleted = True
        keep = C.ensure_private_dir(os.path.join(logs_dir(), f"{aid}-{time.strftime('%Y%m%d%H%M%S')}"))
        for name in ("meta.json", "agent.log", "console.log", "egress.log", "argv.json", "qemu.stderr", "chat.jsonl"):
            src = os.path.join(a.paths.vm_dir, name)
            if os.path.exists(src) and not os.path.islink(src):
                shutil.move(src, os.path.join(keep, name))
        shutil.rmtree(a.paths.vm_dir, ignore_errors=True)
        shutil.rmtree(a.paths.run_dir, ignore_errors=True)
        _rm(a.paths.route_file)
        self._drop_tls(a)
        with self._lock:
            self.agents.pop(aid, None)

    def shutdown_all(self, wait: float = STOP_WAIT) -> None:
        """The panel is quitting: stop every running agent the way Stop does, so memory
        agents save their conversation and persistent disks are shut down cleanly.  QEMU is
        only killed if an agent takes longer than `wait`, as long as Stop waits."""
        self.closing = True  # an agent still starting stops before its VM does
        agents = list(self.agents.values())
        running = [a for a in agents if a.procs.get("qemu") and a.procs["qemu"].poll() is None]
        if running:
            try:  # the terminal may be gone (it was closed)
                print(f"stopping {len(running)} agent(s); memory agents save their conversation first. "
                      "Press Ctrl+C again to stop them at once.", flush=True)
            except OSError:
                pass
        stoppers = []
        for a in running:
            a.set(state="stopping")
            t = threading.Thread(target=self._stop_sync, args=(a,), daemon=True)
            t.start()
            stoppers.append(t)
        deadline = time.time() + wait
        for t in stoppers:
            t.join(max(0.1, deadline - time.time()))
        for a in running:
            q = a.procs.get("qemu")
            if q and q.poll() is None:
                q.terminate()
        deadline = time.time() + 15
        for a in agents:  # let each agent thread record its final state
            if a.thread is not None:
                a.thread.join(max(0.1, deadline - time.time()))

    # -- logs -------------------------------------------------------------------
    def read_log(self, aid: str, which_log: str, offset: int, limit: int = 256 * 1024) -> dict:
        a = self.get(aid)
        path = {"agent": a.paths.agent_log, "console": a.paths.console_log,
                "egress": a.paths.egress_log}.get(which_log)
        if not path:
            raise VMError("unknown log")
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if offset < 0:  # tail
                    offset = max(0, size + offset)
                offset = min(offset, size)
                f.seek(offset)
                data = f.read(limit)
        except FileNotFoundError:
            return {"offset": 0, "size": 0, "text": ""}
        return {"offset": offset + len(data), "size": size, "text": data.decode("utf-8", "replace")}
