#!/usr/bin/python3 -I
"""cilly guest runner: a root daemon inside every agent VM.

On boot:
1. Reads boot.json (+ optional inject/ and memory/ trees) from the read-only
   boot disk (/dev/disk/by-id/virtio-cilly-task, a tar stream).
2. Opens the root-only virtio-serial control port (org.cilly.control) and
   waits for the panel's "hello" with the Claude Code policy, which it
   installs root-owned under /etc/claude-code (the agent user can't edit it).
3. Sets up /inject read-only, restores stored memory (memory mode), runs the
   network self-test, and reports "ready".

Then it serves control messages until shutdown:
  task      run Claude Code non-interactively as the unprivileged `agent` user
            in the repo checkout (optionally continuing the last conversation),
            commit leftovers, push the agent branch, open/keep a draft PR.
            The task's secrets live only in that run's process environment.
            With "attach", turn ~/scratch into a checkout of a repo first.
  export    stream ~/scratch, ~/outbox or the repo's files to the panel
  list_files / read_file   one folder listing or one file, for viewing in the panel
            (only when you press Export; the panel picks where they land).
  policy    re-install the policy live (Claude Code reloads managed settings).
  interrupt end the running task's Claude Code run; the task finishes up and the VM stays on.
  deliver   give a message you sent to the running Claude Code session, which reads it now
            (only when that Claude Code takes messages mid-run; "ready" says the runner can).
  shutdown  export memory to the memout disk (memory mode), then power off.

A boot with "move" in boot.json only moves the agent's home folder to a new VM image, then powers
off: "export" packs it onto the transfer disk, "import" unpacks it into a fresh disk.  Both as the
agent user.

Status goes back over the control port; human-readable output goes to the
log port (org.cilly.log), with agent output prefixed by "| ".
"""

import base64
import difflib
import hashlib
import json
import os
import pwd
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib

def _device(path: str) -> str:
    """A disk or port the panel gave this VM.  An image from before the rename to cilly is started under
    vmpanel's names, only to move its home folder to a new image: its own runner finds the boot disk
    there and hands over to this one."""
    old = path.replace("cilly", "vmpanel")
    return old if not os.path.exists(path) and os.path.exists(old) else path


TASK_DISK = _device("/dev/disk/by-id/virtio-cilly-task")
MEMOUT_DISK = _device("/dev/disk/by-id/virtio-cilly-memout")
TRANSFER_DISK = _device("/dev/disk/by-id/virtio-cilly-transfer")  # the home folder, on its way to a new image
TRANSFER_MAGIC = b"CILLY-HOME-1\n"
TRANSFER_HEADER = 4096  # the tar starts here; the header says how long it is and its SHA-256
CONTROL_PORT = _device("/dev/virtio-ports/org.cilly.control")
LOG_PORT = _device("/dev/virtio-ports/org.cilly.log")
PROXY = "http://10.0.2.100:3128"
INJECT = "/inject"
AGENT = "agent"
POLICY_DIR = "/etc/claude-code"
GLOBAL_GITCONFIG = "/etc/cilly/gitconfig"   # root-owned; the agent can't edit it
STATE_FILE = "cilly-state.json"             # in ~agent/.claude, travels with memory
MEMORY_TOP = ("CLAUDE.md", "projects", STATE_FILE)
INBOX = "/inbox"
ATTACH_DIR = "/attachments"                   # files you attached to messages, read-only for the agent
ATTACH_STAGING = "/var/lib/cilly/attach"    # root-only while a file arrives
PLUGINS_FILE = "/var/lib/cilly/plugins.json" # root-only: the plugins cilly installed here, name -> plugin id
PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SKILLS_FILE = "/var/lib/cilly/skills.json"   # root-only: the skills cilly unpacked here, name -> sha256 of its tar
SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
ATTACH_MAX = 100 * 1024 * 1024
OUTBOX_MAX = 2 * 1024 * 1024      # host limit for one research delivery
OUTBOX_FILE_MAX = 1024 * 1024
OUTBOX_FILES_MAX = 200            # the panel's limits for findings (cilly/handoff.py), so normal ones get through
OUTBOX_NAME = re.compile(r"^[A-Za-z0-9._@+=,-][A-Za-z0-9._@+=, -]{0,120}$")
REQUEST_MAX = 64 * 1024
ASK_MAX, ASK_FILES = 16 * 1024, 4  # ~/ask/*.json: read this much of so many files (the panel checks them)
# Must stay within the host's import limits (cilly/memory.py).
MEMOUT_MAX = 380 * 1024 * 1024
MEMOUT_FILE_MAX = 64 * 1024 * 1024
MEMOUT_FILES_MAX = 4500
MEMOUT_ENTRIES_MAX = 9000  # files and folders; the host counts both (cilly/memory.py MAX_ENTRIES)
MEMOUT_PATH_MAX, MEMOUT_DEPTH_MAX = 1024, 32  # the host refuses a memory export with a longer or deeper path
EXPORT_CHUNK = 512 * 1024         # raw bytes per control message (base64 stays under the host's 1 MiB line cap)
OUTBOX_ACK_TIMEOUT = 60           # seconds to wait for the panel to confirm it stored the findings
EXPORT_SOURCES = {"scratch": "scratch", "outbox": "outbox"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._@+=,-][A-Za-z0-9._@+=, -]{0,254}$")

_log_fh = None


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%SZ ", time.gmtime()) + msg.rstrip("\n") + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if _log_fh:
        try:
            _log_fh.write(line.encode(errors="replace"))
            _log_fh.flush()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# control channel
# ---------------------------------------------------------------------------

class Control:
    """Newline-delimited JSON over the root-only virtio-serial port."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.buf = b""
        self.wlock = threading.Lock()

    def send(self, **msg) -> None:
        data = json.dumps(msg).encode() + b"\n"
        with self.wlock:
            view = memoryview(data)
            while view:
                try:
                    n = os.write(self.fd, view)
                    view = view[n:]
                except BlockingIOError:
                    select.select([], [self.fd], [], 5)

    def recv(self, timeout=None):
        deadline = None if timeout is None else time.time() + timeout
        while b"\n" not in self.buf:
            left = None if deadline is None else max(0, deadline - time.time())
            if deadline is not None and left == 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left if left is not None else 1.0)
            if not r:
                continue
            chunk = os.read(self.fd, 65536)
            if not chunk:
                time.sleep(0.2)  # host side not connected (yet)
                continue
            self.buf += chunk
            if len(self.buf) > 4 << 20:
                raise ValueError("control message too large")
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)


# ---------------------------------------------------------------------------
# boot disk, inject, policy
# ---------------------------------------------------------------------------

def read_boot():
    for _ in range(30):
        if os.path.exists(TASK_DISK):
            break
        time.sleep(1)
    else:
        return None, None
    tf = tarfile.open(TASK_DISK, mode="r:")
    boot = None
    inject, memory = [], []
    for m in tf:
        if m.name == "task.json" and m.isfile():
            boot = json.load(tf.extractfile(m))
        elif m.name == "inject" or m.name.startswith("inject/"):
            inject.append(m)
        elif m.name == "memory" or m.name.startswith("memory/"):
            memory.append(m)
    return boot, (tf, inject, memory)


def setup_inject(boot, tarinfo) -> None:
    mode = (boot.get("inject") or {}).get("mode")
    if not mode:
        return
    os.makedirs(INJECT, exist_ok=True)
    if mode == "share":
        subprocess.run(["mount", "-t", "virtiofs", "-o", "ro,nosuid,nodev,noexec", "inject", INJECT], check=True)
        log(f"inject: live read-only share mounted at {INJECT}")
        return
    tf, members, _ = tarinfo
    staging = "/var/lib/cilly-inject"
    if os.path.ismount(INJECT):
        subprocess.run(["umount", INJECT], check=False)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, mode=0o755)
    tf.extractall(staging, members=members, filter="data")  # rejects abs paths, '..', devices, unsafe links
    src = os.path.join(staging, "inject")
    for dirpath, dirnames, filenames in os.walk(src):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                continue
            os.lchown(p, 0, 0)
            st = os.lstat(p)
            os.chmod(p, 0o555 if (os.path.isdir(p) or st.st_mode & 0o111) else 0o444)
    os.chmod(src, 0o555)
    subprocess.run(["mount", "--bind", src, INJECT], check=True)
    subprocess.run(["mount", "-o", "remount,bind,ro,nosuid,nodev", INJECT], check=True)
    log(f"inject: {len(members) - 1} entries copied, read-only at {INJECT}")


def _h_copy_inject(src: str, dst: str) -> dict:
    """(as the agent) Put a copy of the read-only /inject in ~/scratch to work on.
    Never overwrites: anything already in ~/scratch (earlier work) is kept."""
    copied = kept = 0
    os.makedirs(dst, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):  # symlinks are not followed
        target = os.path.join(dst, os.path.relpath(dirpath, src))
        if os.path.islink(target) or not os.path.isdir(target):
            if os.path.lexists(target):
                dirnames[:] = []
                kept += 1
                continue
            os.mkdir(target)
        for n in list(dirnames) + filenames:
            s, d = os.path.join(dirpath, n), os.path.join(target, n)
            if not os.path.islink(s) and os.path.isdir(s):
                continue  # walked into next
            if os.path.lexists(d):
                kept += 1
            elif os.path.islink(s):  # links inside the folder (the panel dropped the ones pointing out)
                os.symlink(os.readlink(s), d)
                copied += 1
            elif os.path.isfile(s):
                shutil.copyfile(s, d)
                os.chmod(d, 0o755 if os.stat(s).st_mode & 0o111 else 0o644)
                copied += 1
    return {"copied": copied, "kept": kept}


def _write_root_file(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chown(tmp, 0, 0)
    os.replace(tmp, path)


def apply_policy(policy: dict) -> None:
    """Install the enforced Claude Code policy, root-owned.  Managed settings
    outrank user/project settings and are hot-reloaded by Claude Code."""
    os.makedirs(POLICY_DIR, mode=0o755, exist_ok=True)
    os.chown(POLICY_DIR, 0, 0)
    os.chmod(POLICY_DIR, 0o755)
    _write_root_file(os.path.join(POLICY_DIR, "managed-settings.json"),
                     json.dumps(policy.get("managed_settings") or {}, indent=2).encode())
    _write_root_file(os.path.join(POLICY_DIR, "CLAUDE.md"), (policy.get("claude_md") or "").encode())
    # Exclusive: only these servers load (none if empty).  Secrets appear only
    # as ${VAR} placeholders, filled from each task's environment.
    _write_root_file(os.path.join(POLICY_DIR, "managed-mcp.json"),
                     json.dumps(policy.get("managed_mcp") or {"mcpServers": {}}, indent=2).encode())
    log("policy: installed /etc/claude-code/managed-settings.json and CLAUDE.md "
        f"(permission mode {policy.get('permission_mode')})")


CA_FILE = "/usr/local/share/ca-certificates/cilly-agent-vm.crt"


def grow_root(boot) -> None:
    """A disk made bigger than its image (the agent's disk size in the panel): the root partition and its
    file system grow to fill it.  As root, at boot, before anything of the agent's runs, on the VM's own
    disk only.  growpart leaves a partition that fills its disk as it is, and a file system that fills its
    partition grows by nothing, so this is safe to run at every start."""
    if not boot.get("grow_root"):
        return
    try:
        r = subprocess.run(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "/"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=10)
        src, fstype = r.stdout.split()
    except (OSError, subprocess.SubprocessError, ValueError):
        log("disk: couldn't tell which partition is root, so it stays as it is")
        return
    m = re.fullmatch(r"(/dev/vd[a-z])([0-9]{1,2})", src)
    if not m or fstype not in ("ext4", "xfs"):
        log(f"disk: root is {src[:60]} ({fstype[:20]}), and only an ext4 or xfs partition of /dev/vdX grows")
        return
    if not shutil.which("growpart"):
        log("disk: growpart isn't in this image, so the root partition stays as it is. Rebuild the image.")
        return
    g = subprocess.run(["growpart", m[1], m[2]], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    if g.returncode != 0 and "NOCHANGE" not in g.stdout:  # NOCHANGE: it fills the disk (its file system may not yet)
        log(f"disk: growpart failed: {(g.stdout + g.stderr).strip()[-300:]}")
        return
    fs = ["resize2fs", src] if fstype == "ext4" else ["xfs_growfs", "/"]
    f = subprocess.run(fs, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    if f.returncode != 0:
        log(f"disk: {fs[0]} failed: {(f.stdout + f.stderr).strip()[-300:]}")
        return
    st = os.statvfs("/")
    log(f"disk: root fills the disk, {st.f_blocks * st.f_frsize >> 30} GiB")


def install_ca(boot) -> None:
    """With "Keep tokens on this PC", this VM holds stand-ins only, and the egress proxy puts the
    real tokens in on the way out.  For that the VM trusts a CA made for this start, which may only
    vouch for the hosts that need a token, and whose key is already gone.  Without it, none."""
    pem = boot.get("ca")
    if not pem and not os.path.exists(CA_FILE):
        return
    if pem:
        if not isinstance(pem, str) or len(pem) > 8192 or not pem.startswith("-----BEGIN CERTIFICATE-----"):
            log("tokens: the panel sent a CA that isn't one, so none is trusted")
            pem = None
    if pem:
        os.makedirs(os.path.dirname(CA_FILE), mode=0o755, exist_ok=True)
        _write_root_file(CA_FILE, pem.encode())
    else:
        try:
            os.unlink(CA_FILE)
        except FileNotFoundError:
            pass
    r = subprocess.run(["update-ca-certificates", "--fresh"], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    what = "the stand-ins' CA for the token hosts is trusted" if pem else "no CA for token hosts"
    log(f"tokens: {what}" + ("" if r.returncode == 0 else
                             f" (update-ca-certificates failed: {r.stderr.decode(errors='replace')[-200:]})"))


def write_gitconfig(boot) -> None:
    ident = boot["identity"]
    cfg = (
        "[user]\n"
        f"\tname = {ident['name']}\n"
        f"\temail = {ident['email']}\n"
        "[credential \"https://github.com\"]\n"
        "\thelper = \n"
        "\thelper = \"!f() { test \\\"$1\\\" = get && echo username=x-access-token && echo \\\"password=$GH_TOKEN\\\"; }; f\"\n"
        "[init]\n\tdefaultBranch = main\n"
        "[push]\n\tautoSetupRemote = true\n"
    )
    os.makedirs(os.path.dirname(GLOBAL_GITCONFIG), mode=0o755, exist_ok=True)
    with open(GLOBAL_GITCONFIG, "w") as f:
        f.write(cfg)
    os.chmod(GLOBAL_GITCONFIG, 0o644)


# ---------------------------------------------------------------------------
# memory (ephemeral + memory mode)
# ---------------------------------------------------------------------------

def claude_home() -> str:
    return os.path.join(pwd.getpwnam(AGENT).pw_dir, ".claude")


def restore_memory(tarinfo) -> None:
    tf, _, members = tarinfo
    if not members:
        return
    staging = "/var/lib/cilly-memory"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, mode=0o700)
    tf.extractall(staging, members=members, filter="data")
    src = os.path.join(staging, "memory")
    dst = claude_home()
    pw = pwd.getpwnam(AGENT)
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        if name not in MEMORY_TOP:
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(d) and not os.path.islink(d):
            shutil.rmtree(d)
        shutil.move(s, d)
    for dirpath, dirnames, filenames in os.walk(dst):
        for n in [dirpath] + [os.path.join(dirpath, x) for x in dirnames + filenames]:
            os.lchown(n, pw.pw_uid, pw.pw_gid)
    shutil.rmtree(staging, ignore_errors=True)
    log(f"memory: restored {len(members)} entries into ~agent/.claude")


def _h_pack_memory(out, home: str) -> None:
    """(as the agent) Tar selected parts of ~/.claude to `out`.  Only regular
    files and directories; the host validates everything again."""
    entries, total = [], 0
    for top in MEMORY_TOP:
        root = os.path.join(home, top)
        if os.path.islink(root) or not os.path.exists(root):
            continue
        if os.path.isfile(root):
            entries.append((root, top))
            continue
        def fits(arc):
            return len(arc.encode()) <= MEMOUT_PATH_MAX and arc.count("/") < MEMOUT_DEPTH_MAX
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel = os.path.relpath(dirpath, home)
            # the host would reject the whole export for any of these
            dirnames[:] = [d for d in dirnames if _SAFE_NAME.match(d) and fits(os.path.join(rel, d))]
            entries.append((dirpath, rel))
            for n in filenames:
                p = os.path.join(dirpath, n)
                if not _SAFE_NAME.match(n) or not fits(os.path.join(rel, n)) or os.path.islink(p) or not os.path.isfile(p):
                    continue
                entries.append((p, os.path.join(rel, n)))
    # drop the oldest transcripts if over budget; only folders that hold a kept file go along
    files = sorted((e for e in entries if os.path.isfile(e[0])), key=lambda e: os.path.getmtime(e[0]), reverse=True)
    keep, folders = set(), set()
    for path, arc in files:
        size = os.path.getsize(path)
        parents = {os.path.dirname(arc)[:i] for i, c in enumerate(os.path.dirname(arc) + "/") if c == "/"} - {""}
        if size > MEMOUT_FILE_MAX or total + size > MEMOUT_MAX or len(keep) >= MEMOUT_FILES_MAX \
                or len(keep) + len(folders | parents) >= MEMOUT_ENTRIES_MAX:
            continue
        total += size
        keep.add(arc)
        folders |= parents
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for path, arc in entries:
            if os.path.isdir(path):
                if arc not in folders:
                    continue
                ti = tarfile.TarInfo(arc)
                ti.type, ti.mode = tarfile.DIRTYPE, 0o700
                tar.addfile(ti)
            elif arc in keep:
                try:
                    f = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
                except OSError:
                    continue
                with f:
                    st = os.fstat(f.fileno())
                    if st.st_mode & 0o170000 != 0o100000:
                        continue
                    ti = tarfile.TarInfo(arc)
                    ti.size, ti.mtime, ti.mode = st.st_size, int(st.st_mtime), 0o600
                    tar.addfile(ti, f)
    log(f"memory: exported {len(keep)} files ({total} bytes)")


def export_memory() -> None:
    """Write the agent's memory as a tar onto the memout disk.  Packed by a
    helper running as the agent; root only copies the stream to the disk."""
    if not os.path.exists(MEMOUT_DISK):
        return
    p = agent_popen("pack_memory", claude_home())
    with open(MEMOUT_DISK, "r+b") as dev:
        shutil.copyfileobj(p.stdout, dev, 1 << 20)
    agent_finish(p, "pack_memory")


def _h_pack_home(out, home: str) -> None:
    """(as the agent) The whole home folder as a tar: files, folders and links as they are, with
    their modes and times.  Links are kept as links, never followed.  What the agent can't read,
    and sockets, pipes and devices, are left out."""
    n = skipped = 0
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        def add(path):
            nonlocal n, skipped
            try:
                ti = tar.gettarinfo(path, arcname=os.path.relpath(path, home))  # lstat: a link stays a link
            except OSError:
                ti = None
            if ti is None or not (ti.isreg() or ti.isdir() or ti.issym() or ti.islnk()):
                skipped += 1
                return
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""  # whoever unpacks it owns it
            if not ti.isreg():
                tar.addfile(ti)
                n += 1
                return
            try:
                f = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
            except OSError:
                skipped += 1
                return
            with f:
                st = os.fstat(f.fileno())
                if st.st_mode & 0o170000 != 0o100000 or st.st_size != ti.size:  # swapped or changed meanwhile
                    skipped += 1
                    return
                tar.addfile(ti, f)
            n += 1
        for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
            if dirpath != home:
                add(dirpath)
            for d in [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                dirnames.remove(d)  # a link to a folder: kept as a link
                add(os.path.join(dirpath, d))
            for name in filenames:
                add(os.path.join(dirpath, name))
    log(f"move: packed {n} entries" + (f", left out {skipped} it can't move" if skipped else ""))


def _tar_member(member, dest):
    """tar's own rules (no absolute paths, nothing outside dest, no setuid bits), and a member that
    breaks them is left out instead of stopping the rest."""
    try:
        return tarfile.tar_filter(member, dest)
    except tarfile.FilterError:
        return None


def _h_unpack_home(home: str) -> dict:
    """(as the agent) Unpack the tar on stdin into the home folder."""
    n = skipped = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
        for m in tar:
            if m.isdev() or m.isfifo() or _tar_member(m, home) is None:
                skipped += 1
                continue
            try:
                tar.extract(m, home, filter=_tar_member)
                n += 1
            except (OSError, tarfile.TarError):
                skipped += 1
    return {"entries": n, "skipped": skipped}


def _h_install_skill(tar_path: str, skills_dir: str, name: str) -> dict:
    """(as the agent) Unpack a skill's tar into ~/.claude/skills/<name>/, replacing the copy cilly put there before.
    Only plain files and folders, by tar's own rules: nothing lands outside the folder."""
    os.makedirs(skills_dir, exist_ok=True)
    tmp = os.path.join(skills_dir, f".cilly-{name}-{uuid.uuid4().hex[:8]}")
    os.mkdir(tmp, 0o755)
    n = 0
    with tarfile.open(tar_path, mode="r:") as tar:
        for m in tar:
            if not (m.isreg() or m.isdir()) or _tar_member(m, tmp) is None:
                continue
            tar.extract(m, tmp, filter=_tar_member)
            n += m.isreg()
    dest = os.path.join(skills_dir, name)
    if os.path.islink(dest) or os.path.isfile(dest):
        os.unlink(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)
    os.rename(tmp, dest)
    return {"files": n}


def _h_remove_skill(skills_dir: str, name: str) -> None:
    """(as the agent) Take away a skill cilly put in ~/.claude/skills/."""
    dest = os.path.join(skills_dir, name)
    if os.path.islink(dest) or os.path.isfile(dest):
        os.unlink(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)


def export_home() -> int:
    """Pack the home folder onto the transfer disk.  The agent packs it; root only copies the stream
    after the header, and writes the header once it's all there."""
    if not os.path.exists(TRANSFER_DISK):
        raise RuntimeError("there's no transfer disk")
    h, size = hashlib.sha256(), 0
    p = agent_popen("pack_home", home_dir())
    with open(TRANSFER_DISK, "r+b") as dev:
        dev.seek(TRANSFER_HEADER)
        while chunk := p.stdout.read(1 << 20):
            dev.write(chunk)
            h.update(chunk)
            size += len(chunk)
        agent_finish(p, "pack_home")
        dev.flush()
        os.fsync(dev.fileno())
        dev.seek(0)
        dev.write(TRANSFER_MAGIC + json.dumps({"bytes": size, "sha256": h.hexdigest()}).encode() + b"\n")
        dev.flush()
        os.fsync(dev.fileno())
    return size


def import_home() -> dict:
    """Unpack the home folder from the transfer disk into this fresh disk.  It's checked against
    its SHA-256 before anything is written; the agent unpacks it."""
    with open(TRANSFER_DISK, "rb") as dev:
        head = dev.read(TRANSFER_HEADER)
        if not head.startswith(TRANSFER_MAGIC):
            raise RuntimeError("the transfer disk holds no home folder")
        try:
            meta = json.loads(head[len(TRANSFER_MAGIC):].split(b"\n", 1)[0])
            size, want = meta["bytes"], meta["sha256"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("the transfer disk's header is damaged") from None
        if type(size) is not int or size < 0 or not isinstance(want, str):
            raise RuntimeError("the transfer disk's header is damaged")

        def stream(write):
            dev.seek(TRANSFER_HEADER)
            h, left = hashlib.sha256(), size
            while left:
                chunk = dev.read(min(1 << 20, left))
                if not chunk:
                    raise RuntimeError("the transfer disk ends early")
                h.update(chunk)
                write(chunk)
                left -= len(chunk)
            return h.hexdigest()
        if stream(lambda c: None) != want:
            raise RuntimeError("the transfer disk's contents don't match its checksum")
        p = agent_popen("unpack_home", home_dir(), stdin=subprocess.PIPE)
        try:
            stream(p.stdin.write)
        finally:
            p.stdin.close()
        out = p.stdout.read()
        agent_finish(p, "unpack_home")
    res = json.loads(out or b"null")
    log(f"move: unpacked {res['entries']} entries" + (f", left out {res['skipped']}" if res["skipped"] else ""))
    return dict(res, bytes=size)


def move_home(kind: str, ctl) -> None:
    """A boot that only moves the home folder: no policy, no self-test, no task.  Nothing of the
    agent's has run on this boot, so nothing writes into its home meanwhile."""
    ctl.send(type="phase", phase="move")
    try:
        res = {"bytes": export_home()} if kind == "export" else import_home()
        ctl.send(type="moved", step=kind, ok=True, **res)
    except Exception as e:  # noqa: BLE001
        log(f"!! moving the home folder failed: {e}")
        ctl.send(type="moved", step=kind, ok=False, error=str(e)[:300])
    log("powering off")
    subprocess.run(["systemctl", "poweroff"], check=False)


def state_path() -> str:
    return os.path.join(claude_home(), STATE_FILE)


def _h_load_state(path: str) -> dict:
    """The agent can write this file, so keep only the shape the runner expects."""
    try:
        with open(path) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    conv = st.get("conversation") if isinstance(st, dict) else None
    out = {}
    for key in ("conversation", "interrupted"):  # interrupted: you stopped the last run in that folder
        d = st.get(key) if isinstance(st, dict) else None
        out[key] = {k: True for k, v in d.items() if isinstance(k, str) and v is True} if isinstance(d, dict) else {}
    return out


def _h_save_state(path: str, state: dict) -> None:
    """Whole or not at all, since a cut-off state would lose the conversation.  The agent can plant
    anything at the temp name (a link, a FIFO), so it's removed and made anew, never opened."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def load_state() -> dict:
    """The state file travels with memory, so it lives in the agent's
    ~/.claude and is read and written as the agent."""
    try:
        st = as_agent("load_state", state_path())
    except RuntimeError:
        return {}
    return st if isinstance(st, dict) else {}


def save_state(state: dict) -> None:
    as_agent("save_state", state_path(), state)


# ---------------------------------------------------------------------------
# running things as the agent
# ---------------------------------------------------------------------------
# Rule: root never opens, creates, moves or chowns anything in the agent's
# home itself.  The agent owns that tree and can plant symlinks in it, so root
# following one could hand it any file in the guest.  Those operations run in
# a child process as the agent user (the _h_* helpers, started with
# as_agent / agent_popen), where a symlink can only lead somewhere the agent
# could reach anyway.

HELPER_FLAG = "--cilly-agent-helper"
_self_src = None


def _self_source() -> str:
    """The program a helper runs: this file, compressed.  Passed with -c, because a re-exec'd runner
    lives in root-only /run/cilly, and compressed, because Linux takes at most 128 KiB in one argument."""
    global _self_src
    if _self_src is None:
        with open(__file__, "rb") as f:
            packed = base64.b85encode(zlib.compress(f.read(), 9)).decode()
        # the data on a line of its own: a traceback quotes the line that failed, and a long one would fill stderr
        _self_src = f"PACKED = {packed!r}\nimport base64, zlib\nexec(compile(zlib.decompress(base64.b85decode(PACKED)), 'cilly-runner', 'exec'))"
    return _self_src


def agent_popen(name: str, *args, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    pw = pwd.getpwnam(AGENT)
    return subprocess.Popen([sys.executable, "-I", "-c", _self_source(), HELPER_FLAG, name, json.dumps(args)],
                            cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                            user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], start_new_session=True,
                            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def agent_finish(p: subprocess.Popen, name: str) -> None:
    p.stdout.close()
    err = p.stderr.read()
    p.stderr.close()
    rc = p.wait()
    for line in err.decode(errors="replace").splitlines():
        log(line)
    if rc != 0:
        raise RuntimeError(f"{name} (as {AGENT}) failed with {rc}")


def as_agent(name: str, *args, timeout: float = 300):
    pw = pwd.getpwnam(AGENT)
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _self_source(), HELPER_FLAG, name, json.dumps(args)],
                           cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                           user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], start_new_session=True,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{name} (as {AGENT}) timed out") from None
    for line in r.stderr.decode(errors="replace").splitlines():
        log(line)
    if r.returncode != 0:
        raise RuntimeError(f"{name} (as {AGENT}) failed with {r.returncode}")
    return json.loads(r.stdout or b"null")


def _h_mkdirs(paths: list) -> None:
    for p in paths:
        os.makedirs(p, exist_ok=True)


def _h_move(src: str, dst: str, recreate: bool = False) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    if recreate:
        os.makedirs(src, exist_ok=True)


def _helper_main() -> int:
    """Entry point of a helper child (already running as the agent)."""
    global log
    out = sys.stdout.buffer
    sys.stdout = sys.stderr
    log = lambda msg: sys.stderr.write(msg.rstrip("\n") + "\n")  # noqa: E731 - the parent timestamps it
    name, args = sys.argv[2], json.loads(sys.argv[3])
    streaming = {"pack_dir": _h_pack_dir, "pack_memory": _h_pack_memory, "read_file": _h_read_file,
                 "pack_home": _h_pack_home}
    plain = {"mkdirs": _h_mkdirs, "move": _h_move, "load_state": _h_load_state, "save_state": _h_save_state,
             "collect_text_files": _h_collect_text_files, "read_requests": _h_read_requests, "read_asks": _h_read_asks,
             "sanitize_repo_config": _h_sanitize_repo_config, "selftest": _h_selftest,
             "has_session": _h_has_session, "copy_inject": _h_copy_inject, "list_dir": _h_list_dir,
             "recent_images": _h_recent_images, "archive_outbox": _h_archive_outbox, "read_pr_note": _h_read_pr_note,
             "read_summary": _h_read_summary, "unpack_home": _h_unpack_home, "install_skill": _h_install_skill,
             "remove_skill": _h_remove_skill}
    if name in streaming:
        streaming[name](out, *args)
    else:
        out.write(json.dumps(plain[name](*args)).encode())
    out.flush()
    return 0


TOOLCHAIN_ENV = "/etc/cilly/toolchains.env"


def toolchain_env(path=TOOLCHAIN_ENV) -> dict:
    """What the image build installed beyond the basics (root-owned, written at build time):
    JAVA_HOME, RUSTUP_HOME, PLAYWRIGHT_BROWSERS_PATH, NODE_PATH, GHIDRA_INSTALL_DIR, REA's tool paths
    and extra PATH entries.
    Only these keys, only plain absolute paths."""
    try:
        with open(path) as f:
            lines = f.read(65536).splitlines()
    except OSError:
        return {}
    out, extra = {}, []
    for ln in lines:
        k, _, v = ln.partition("=")
        if not re.fullmatch(r"/[A-Za-z0-9._+/-]{1,200}", v) or ".." in v.split("/"):
            continue
        if k in ("JAVA_HOME", "RUSTUP_HOME", "PLAYWRIGHT_BROWSERS_PATH", "NODE_PATH", "GHIDRA_INSTALL_DIR",
                 "REA_JADX_MCP_JAR", "REA_PWNTOOLS_PYTHON", "REA_TSHARK_COMMAND"):
            out[k] = v
        elif k == "PATH" and v not in extra:
            extra.append(v)
    if extra:
        out["PATH"] = ":".join(extra)
    return out


def agent_env(boot, task) -> dict:
    pw = pwd.getpwnam(AGENT)
    tc = toolchain_env()
    home = pw.pw_dir
    env = {
        "HOME": pw.pw_dir, "USER": AGENT, "LOGNAME": AGENT, "SHELL": "/bin/bash",
        # toolchains first, then the system, then what the agent installs itself
        "PATH": ":".join(filter(None, [tc.pop("PATH", ""), "/usr/local/bin:/usr/bin:/bin",
                                       f"{home}/.local/bin:{home}/.cargo/bin:{home}/go/bin"])),
        **tc,
        "LANG": "C.UTF-8", "TZ": "UTC",
        "HTTPS_PROXY": PROXY, "https_proxy": PROXY, "HTTP_PROXY": PROXY, "http_proxy": PROXY,
        "NO_PROXY": "", "no_proxy": "",
        "GIT_TERMINAL_PROMPT": "0", "CI": "1",
        "GIT_CONFIG_GLOBAL": GLOBAL_GITCONFIG, "GIT_CONFIG_NOSYSTEM": "1",
    }
    if os.path.exists(CA_FILE):  # Node keeps its own list of CAs: this start's one too
        env["NODE_EXTRA_CA_CERTS"] = CA_FILE
    env.update({k: str(v) for k, v in (task.get("agent_env") or {}).items()})
    env.update(task.get("env") or {})
    return env


class ChatRelay:
    """Turns Claude Code's `--output-format stream-json` lines into small chat
    events for the panel (text, tool line, diff, tool result, done) and a
    readable line for the log.  Everything is truncated here and validated
    again on the host.  Lines that aren't stream-json pass through as text."""

    TEXT_MAX, TARGET_MAX, DIFF_LINES, LINE_MAX = 20000, 500, 200, 300
    DETAIL_MAX, OUT_MAX = 4000, 2000  # the full command (or prompt) and output, shown when you expand a line
    VERBS = {"Bash": "Ran", "Read": "Read", "Grep": "Searched", "Glob": "Listed", "WebFetch": "Fetched",
             "WebSearch": "Searched", "Task": "Delegated", "Agent": "Delegated", "NotebookEdit": "Edited",
             "TodoWrite": "Planned", "LS": "Listed"}

    FLUSH_EVERY = 0.5
    CONTEXT_EVERY = 5  # seconds between context updates to the panel (the newest one waits, the last one is sent)
    USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")

    def __init__(self, ctl, task_id: str, live=None):
        self.ctl, self.task, self.live = ctl, task_id, live
        self.batch, self.size, self.last = [], 0, time.time()
        self.lock = threading.Lock()
        self.sid = None  # Claude Code's session id, so you can go back to this conversation later
        self.spoke = False  # Claude wrote or used a tool, so the run got going
        self.progress_at = {}  # subagent id -> when we last sent its progress (they come often)
        self.model = None  # the main conversation's model, whose context window counts
        self.context, self.context_sent, self.context_at = {}, {}, 0.0  # {"used", "window"}: how full it is
        # the newest event goes out even when nothing follows it, like a tool line before a long build
        self.closed = threading.Event()
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        while not self.closed.wait(self.FLUSH_EVERY):
            with self.lock:
                if self.batch and time.time() - self.last >= self.FLUSH_EVERY:
                    self._flush()
                self._send_context()

    def _send_context(self, now: bool = False) -> None:
        """The conversation's size, numbers only.  An older panel ignores the message."""
        if self.context and self.context != self.context_sent and (now or time.time() - self.context_at >= self.CONTEXT_EVERY):
            self.context_sent, self.context_at = dict(self.context), time.time()
            self.ctl.send(type="context", task=self.task, **self.context)

    def _usage(self, msg: dict) -> None:
        """What the main conversation fills of its context window: everything its latest request sent and
        got back.  A subagent's messages have their own context, so they don't count."""
        m = msg.get("message")
        usage = m.get("usage") if isinstance(m, dict) else None
        if not isinstance(usage, dict) or msg.get("parent_tool_use_id") or m.get("model") == "<synthetic>":
            return  # a synthetic message (an API error, say) carries no real usage
        n = [usage.get(k, 0) for k in self.USAGE_KEYS]
        if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in n) or not sum(n):
            return
        if isinstance(m.get("model"), str):
            self.model = m["model"]
        with self.lock:
            self.context["used"] = sum(n)
            self._send_context()

    def _window(self, msg: dict) -> None:
        """The model's context window, from the result's modelUsage (the panel guesses by model without it)."""
        per = msg.get("modelUsage")
        if not isinstance(per, dict):
            return
        entry = per.get(self.model) if self.model in per else next(iter(per.values())) if len(per) == 1 else None
        w = entry.get("contextWindow") if isinstance(entry, dict) else None
        if isinstance(w, int) and not isinstance(w, bool) and w > 0:
            with self.lock:
                self.context["window"] = w

    def _emit(self, ev: dict) -> None:
        with self.lock:
            self.batch.append(ev)
            self.size += len(json.dumps(ev))
            if len(self.batch) >= 20 or self.size >= 150_000 or time.time() - self.last > 0.5:
                self._flush()

    def _flush(self) -> None:
        if self.batch:
            self.ctl.send(type="chat", task=self.task, events=self.batch)
        self.batch, self.size, self.last = [], 0, time.time()

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            self._send_context(now=True)
        with self.lock:
            self._flush()

    @classmethod
    def _cut(cls, text, n) -> str:
        text = str(text)
        return text if len(text) <= n else text[:n - 1] + "…"

    def _diff(self, path, old, new) -> dict:
        lines, add, rem = [], 0, 0
        for ln in difflib.unified_diff(str(old).splitlines(), str(new).splitlines(), n=2, lineterm=""):
            if ln.startswith(("---", "+++")):
                continue
            sign = ln[:1] if ln[:1] in "+-@" else " "
            add += sign == "+"
            rem += sign == "-"
            if len(lines) < self.DIFF_LINES:
                lines.append([sign, self._cut(ln[1:], self.LINE_MAX)])
        return {"k": "diff", "file": self._cut(path, self.TARGET_MAX), "add": add, "del": rem, "lines": lines}

    def _tool(self, block: dict) -> tuple[dict, str]:
        name, inp, tid = str(block.get("name", "")), block.get("input") or {}, str(block.get("id", ""))[:64]
        if not isinstance(inp, dict):
            inp = {}
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if name in ("Edit", "MultiEdit", "Write"):
            if name == "Write":
                ev = self._diff(path, "", inp.get("content", ""))
            else:
                edits = inp.get("edits") if name == "MultiEdit" else [inp]
                old = "\n...\n".join(str(e.get("old_string", "")) for e in edits or [] if isinstance(e, dict))
                new = "\n...\n".join(str(e.get("new_string", "")) for e in edits or [] if isinstance(e, dict))
                ev = self._diff(path, old, new)
            ev["id"] = tid
            return ev, f"{'Wrote' if name == 'Write' else 'Edited'} {path} +{ev['add']} -{ev['del']}"
        if name.startswith("mcp__"):
            _, srv, tool = (name.split("__", 2) + ["", ""])[:3]  # mcp__<server>__<tool>
            verb, target = "MCP", f"{srv}: {tool}"
        else:
            verb = self.VERBS.get(name, name or "Tool")
            target = (inp.get("command") or path or inp.get("pattern") or inp.get("url") or inp.get("query")
                      or inp.get("description") or "")
            if name == "TodoWrite":
                target = f"{len(inp.get('todos') or [])} items"
            if not target:
                target = json.dumps(inp)[:200]
        target = self._cut(str(target).splitlines()[0] if str(target).strip() else target, self.TARGET_MAX)
        ev = {"k": "tool", "id": tid, "tool": name[:64], "verb": verb[:20], "target": target}
        detail = inp.get("command") if name == "Bash" else inp.get("prompt") if name in ("Agent", "Task") else \
            json.dumps(inp, indent=1) if inp and name != "TodoWrite" else ""
        if isinstance(detail, str) and detail.strip() and detail.strip() != target:
            ev["detail"] = self._cut(detail, self.DETAIL_MAX)
        if name in ("Agent", "Task") and isinstance(inp.get("subagent_type"), str):
            ev["sub"] = inp["subagent_type"][:64]
        if inp.get("run_in_background") is True:
            ev["bg"] = True
        return ev, f"{verb} {target}"

    def _subagent(self, msg: dict):
        """Claude Code's task_started/task_progress/task_notification: subagents and background commands."""
        sub = msg.get("subtype")
        if sub not in ("task_started", "task_progress", "task_notification"):
            return None
        tid = msg.get("tool_use_id") or msg.get("task_id")
        if not isinstance(tid, str) or not tid:
            return None
        now = time.time()
        if sub == "task_progress":
            if now - self.progress_at.get(tid, 0) < 3:
                return None
            self.progress_at[tid] = now
        state = "running" if sub != "task_notification" else msg.get("status")
        ev = {"k": "sub", "id": tid[:64], "state": state if state in ("running", "completed", "failed", "stopped") else "running"}
        for k, src, n in (("desc", "description", 200), ("sub", "subagent_type", 64), ("last", "last_tool_name", 64),
                          ("summary", "summary", 300)):
            if isinstance(msg.get(src), str) and msg[src].strip():
                ev[k] = self._cut(" ".join(msg[src].split()), n)
        if isinstance(msg.get("is_backgrounded"), bool):
            ev["bg"] = msg["is_backgrounded"]
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        for k, src in (("tools", "tool_uses"), ("tokens", "total_tokens"), ("ms", "duration_ms")):
            if isinstance(usage.get(src), int):
                ev[k] = usage[src]
        return ev

    @staticmethod
    def _status(msg: dict):
        """Progress notes worth showing, so a long pause doesn't look stuck."""
        sub = msg.get("subtype")
        if sub == "status" and msg.get("status") == "compacting":
            return "Compacting the conversation…"
        if sub == "compact_boundary":
            pre = (msg.get("compact_metadata") or {}).get("pre_tokens")
            return "Conversation compacted" + (f" (was {pre:,} tokens)" if isinstance(pre, int) else "")
        if sub == "api_retry":
            n, wait = msg.get("attempt"), msg.get("retry_delay_ms")
            return ("API request failed, retrying" + (f" (attempt {n})" if isinstance(n, int) else "")
                    + (f" in {round(wait / 1000)}s" if isinstance(wait, (int, float)) else ""))
        return None

    def feed(self, line: str):
        """Returns the line to show in the log, or None."""
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if not isinstance(msg, dict) or "type" not in msg:
            notice = re.match(r"\[claude-code:([a-z_]{1,40})\]", line)
            if notice:  # Claude Code's own notice (like an unknown local model's name), not the agent's words: the log only
                return line
            if line.strip():
                self._emit({"k": "text", "text": self._cut(line.rstrip("\n"), self.TEXT_MAX)})
            return line
        if self.live is not None:
            self.live.saw(msg)
        typ, shown = msg.get("type"), []
        content = (msg.get("message") or {}).get("content") if isinstance(msg.get("message"), dict) else None
        if isinstance(msg.get("session_id"), str) and SESSION_RE.fullmatch(msg["session_id"]):
            self.sid = msg["session_id"]
        parent = msg.get("parent_tool_use_id")
        tag = {"parent": parent[:64]} if isinstance(parent, str) and parent else {}  # done inside a subagent
        if typ == "system":
            note = self._status(msg)
            if note:
                self._emit({"k": "status", "text": note})
                shown.append(f"[{note}]")
            ev = self._subagent(msg)
            if ev:
                self._emit(ev)
                if ev["state"] != "running" or msg.get("subtype") == "task_started":
                    shown.append(f"[subagent {ev.get('desc') or ev['id']}: {ev['state']}]")
        elif typ == "assistant" and isinstance(content, list):
            self._usage(msg)
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    self.spoke = True
                    self._emit({"k": "text", "text": self._cut(b["text"], self.TEXT_MAX), **tag})
                    shown.append(str(b["text"]))
                elif b.get("type") == "tool_use":
                    self.spoke = True
                    ev, desc = self._tool(b)
                    self._emit({**ev, **tag})
                    shown.append("> " + desc)
        elif typ == "user" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                body = b.get("content")
                if isinstance(body, list):
                    body = " ".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
                first = next((ln.strip() for ln in str(body or "").splitlines() if ln.strip()), "")
                ok = not b.get("is_error")
                ev = {"k": "result", "id": str(b.get("tool_use_id", ""))[:64], "ok": ok, "summary": self._cut(first, 200), **tag}
                if str(body or "").strip() and str(body).strip() != first:
                    ev["out"] = self._cut(str(body).strip("\n"), self.OUT_MAX)
                self._emit(ev)
                if not ok:
                    shown.append("  ! " + self._cut(first, 200))
        elif typ == "result":
            self._window(msg)
            with self.lock:
                self._send_context(now=True)
            ev = {"k": "done", "ok": not msg.get("is_error") and msg.get("subtype") == "success"}
            for k, src in (("turns", "num_turns"), ("ms", "duration_ms")):
                if isinstance(msg.get(src), int):
                    ev[k] = msg[src]
            if isinstance(msg.get("total_cost_usd"), (int, float)):
                ev["cost"] = float(msg["total_cost_usd"])
            if self.sid:
                ev["sid"] = self.sid
            self._emit(ev)
            shown.append(f"[done: {msg.get('subtype')}, {msg.get('num_turns')} turns]")
            if not ev["ok"] and msg.get("result"):
                shown.append(self._cut(msg["result"], 500))
        return "\n".join(shown) if shown else None


def user_line(text: str, uid: str | None = None) -> bytes:
    """One stream-json user message, built here from plain text: json.dumps keeps it on one line,
    so nothing in the text can add a message of its own."""
    msg = {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
           "session_id": "", "origin": {"kind": "human"}}
    if uid:
        msg["uuid"] = uid
    return json.dumps(msg).encode() + b"\n"


class LiveInput:
    """Claude Code's stdin in stream-json mode: the task's prompt, then the messages you send while it
    works (Send now).  Once Claude Code has queued such a message, its own send-now request for it
    follows, which ends no background agent or command.  Messages are only taken when system/init offers what this needs.
    Stdin closes, which ends the run as before, once a turn is over (its result, or the session going
    idle once background agents are done), every message sent has been answered, and no background
    command or agent it started still runs.  While one does, the session still takes your messages, and
    the panel is told the turn is over, so a queued message goes in too.  It waits for them at most
    bg_ceiling seconds (the agent's background work limit), and otherwise until the task's time limit.
    If Claude Code never says a message was answered, stdin still closes after QUIET seconds without
    output: once the turn is over, or at any time while it never said it got the message."""

    NEEDS = ("interrupt_send_now_v1", "msg_lifecycle_v1")
    SETTLED = ("completed", "cancelled", "discarded", "refused")
    QUIET = 300

    def __init__(self, ctl, task_id: str, prompt: str, bg_ceiling: float | None = None):
        self.ctl, self.task = ctl, task_id
        self.bg: set = set()   # background commands and agents still running: started, and not yet reported done
        self.bg_ceiling = bg_ceiling
        self.over_at = 0.0     # when the last turn was over
        self.told = False      # the panel knows this turn is over with background work running
        self.lock = threading.Lock()
        self.lines: queue.Queue = queue.Queue()
        self.lines.put(user_line(prompt))
        self.live = False      # this Claude Code takes messages mid-run
        self.closed = False
        self.open: set = set()  # uuids of messages sent that aren't answered yet
        self.unsent: set = set()  # of those, the ones whose send-now request waits until Claude Code has them
        self.ids: dict = {}     # uuid -> the panel's id for that message, until it's answered or not
        self.over = False      # the last turn is over
        self.ran = False       # a turn had its result, so the session going idle means it's over
        self.heard = time.time()

    def start(self, pipe) -> None:
        threading.Thread(target=self._write, args=(pipe,), daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _write(self, pipe) -> None:
        try:
            while (line := self.lines.get()) is not None:
                pipe.write(line)
                pipe.flush()
        except OSError:
            pass  # it exited
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _watch(self) -> None:
        while not self.closed:
            time.sleep(min(15, self.QUIET))
            with self.lock:
                if (self.over and self.open or self.open & self.unsent) and time.time() - self.heard >= self.QUIET:
                    log(f"Claude Code said nothing for {self.QUIET}s with a message unanswered; ending its input")
                    self.close()
                elif self.over and self.bg and self.bg_ceiling and time.time() - self.over_at >= self.bg_ceiling:
                    log(f"background work ran past its limit of {round(self.bg_ceiling)}s after the turn; ending the input")
                    self.close()

    def close(self) -> None:
        """Call with the lock held, or once the run is over."""
        if not self.closed:
            self.closed = True
            self.lines.put(None)

    def saw(self, msg: dict) -> None:
        """Each stream-json line Claude Code writes."""
        typ, sub = msg.get("type"), msg.get("subtype")
        with self.lock:
            self.heard = time.time()
            if self.closed:
                return
            if typ == "system" and sub == "init" and not self.live:
                caps = msg.get("capabilities")
                if isinstance(caps, list) and all(c in caps for c in self.NEEDS):
                    self.live = True
                    self.ctl.send(type="live", task=self.task)
                else:
                    self.close()  # an older Claude Code: only the prompt, as before
            elif typ == "command_lifecycle":
                uid, state = msg.get("command_uuid"), msg.get("state")
                if not isinstance(uid, str):
                    return
                if uid in self.unsent:  # its first word on this message
                    self.unsent.discard(uid)
                    if state == "queued":  # Claude Code holds it now, which its send-now request needs
                        self.lines.put(json.dumps({"type": "control_request", "request_id": "send-now-" + uid,
                                                   "request": {"subtype": "interrupt", "send_now": True, "message_uuid": uid}}).encode() + b"\n")
                if state in self.SETTLED:
                    self.open.discard(uid)
                    mid = self.ids.pop(uid, None)
                    if mid and state != "completed":  # so the chat says Claude may not have read it
                        self.ctl.send(type="delivery", task=self.task, id=mid, state=state)
                elif state == "started":
                    self.over = False
                else:
                    return
            elif typ == "control_response":  # Claude Code's answer to a send-now request, for the log
                log("| send-now: " + json.dumps(msg.get("response"))[:300])
                return
            elif typ == "system" and sub in ("task_started", "task_notification"):  # a subagent or background command
                tid = msg.get("task_id") or msg.get("tool_use_id")
                if not isinstance(tid, str) or not tid:
                    return
                if sub == "task_started":
                    self.bg.add(tid[:64])
                    return
                self.bg.discard(tid[:64])
            elif typ == "system" and sub == "session_state_changed" and msg.get("state") == "idle":
                if not self.ran:
                    return  # it may say idle before its first turn, which is no turn's end
                if not self.over:
                    self.over_at = time.time()
                self.over = True
            elif typ == "result":
                waiting = msg.get("queued_turn_count")
                self.over = not (isinstance(waiting, int) and waiting > 0)
                self.ran = True
                if self.over:
                    self.over_at = time.time()
            else:
                return
            if not self.live or (self.over and not self.open and not self.bg):
                self.close()
            elif self.over and not self.open and self.bg and not self.told:
                self.told = True  # once per turn: the session waits for its background work, and takes messages
                n = len(self.bg)
                self.ctl.send(type="turn_over", task=self.task, background=n)
                self.ctl.send(type="chat", task=self.task, events=[{"k": "status", "text": (
                    f"{n} background {'job' if n == 1 else 'jobs'} still running. A message you send goes into this conversation.")}])

    def deliver(self, text: str, mid: str) -> str:
        """Your message into the running session, and Claude Code's send-now request for it."""
        with self.lock:
            if self.closed:
                return "closing"
            if not self.live:
                return "unsupported"
            uid = str(uuid.uuid4())
            self.open.add(uid)
            self.over = False
            self.told = False
            self.unsent.add(uid)
            self.ids[uid] = mid
            self.lines.put(user_line(text, uid))
        return "delivered"


AGENT_LOCK = threading.RLock()  # held while killing the agent's processes, and during the memory export
CURRENT_RUN: dict = {}         # "proc": the task's Claude Code run, the one thing Stop ends
STOPPING = threading.Event()   # Stop is under way, so no Claude Code run goes on and the rest of the task finishes up
TASK_RUN: dict = {}            # the task now running: "id", "interrupt" (you stopped it), "claude_over"
TASK_RUN_LOCK = threading.Lock()
TASK_STOP_WAIT = 90            # seconds Stop waits for a task to finish up (the panel waits 180 for the VM)


def stop_current_run() -> None:
    """End the task's Claude Code run, so the task thread finishes up on its own (saves the
    conversation, commits, pushes and opens the pull request)."""
    p = CURRENT_RUN.get("proc")
    if p is None or p.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def interrupt_task(msg, ctl) -> None:
    """Stop this task's Claude Code run but not the VM: the task finishes up as it does for Stop.
    Once Claude is over, the commit and push aren't cut off."""
    tid = msg.get("task")
    with TASK_RUN_LOCK:
        if not TASK_RUN or TASK_RUN.get("id") != tid:
            result = "idle"
        elif TASK_RUN.get("claude_over"):
            result = "finishing"
        else:
            TASK_RUN["interrupt"] = True
            result = "stopping"
    ctl.send(type="interrupt_ack", task=tid, result=result)
    if result == "stopping":
        log(f"interrupt: ending task {tid}'s Claude Code run")
        threading.Thread(target=stop_current_run, daemon=True).start()


DELIVER_MAX = 200_000  # characters: more than the panel takes for a message
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def deliver_message(msg, ctl) -> None:
    """Send now into the running session: your message goes to Claude Code as it works.  Anything
    else is answered so the panel stops the task instead ("unsupported"), or keeps the message
    first in line because the run is ending anyway ("closing")."""
    tid, mid, text = msg.get("task"), msg.get("id"), msg.get("text")
    result = "unsupported"
    if isinstance(tid, str) and isinstance(mid, str) and TASK_ID_RE.match(mid) and isinstance(text, str) \
            and 0 < len(text) <= DELIVER_MAX and text.strip():
        try:
            text.encode()
        except UnicodeEncodeError:
            text = ""
        with TASK_RUN_LOCK:
            live = TASK_RUN.get("live") if TASK_RUN.get("id") == tid and not interrupted() and not STOPPING.is_set() else None
        if live is not None and text:
            result = live.deliver(text, mid)
    ctl.send(type="deliver_ack", task=tid if isinstance(tid, str) else None, id=mid if isinstance(mid, str) else None,
             result=result)
    if result == "delivered":
        log(f"deliver: your message {mid} went to task {tid}'s Claude Code")


TASK_BUSY = threading.Lock()  # one task at a time
DROP_CACHES = "/proc/sys/vm/drop_caches"


def drop_file_cache() -> None:
    """Between tasks: free the guest's file cache, so the balloon reports that memory to the PC too."""
    try:
        with open(DROP_CACHES, "w") as f:
            f.write("1")
    except OSError as e:
        log(f"couldn't drop the file cache: {e}")


TASK_ENDED = threading.Event()  # the running task sent its final status and is only letting go
FINAL = ("done", "failed", "interrupted")


class TaskChannel:
    """The control channel as a task sees it.  The panel sends the next queued task as soon as this
    task's final status arrives, so the steps that end a task run just before that status goes out."""

    def __init__(self, ctl, tid, last_steps):
        self.ctl, self.tid, self.last_steps, self.ended = ctl, tid, last_steps, False

    def __getattr__(self, name):
        return getattr(self.ctl, name)

    def send(self, **msg) -> None:
        if msg.get("type") == "status" and msg.get("task") == self.tid and msg.get("state") in FINAL:
            self.end()
            TASK_ENDED.set()  # before the status: the next task may arrive the moment it's out
        self.ctl.send(**msg)

    def end(self) -> None:
        """The last steps, once.  None of them can keep the task from letting go."""
        if self.ended:
            return
        self.ended = True
        for step in self.last_steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001
                log(f"!! end of task: {e.__class__.__name__}: {e}")


def start_task(boot, task, ctl):
    """Run one task on its own thread.  The task is entered in TASK_RUN before the thread starts,
    so an interrupt that comes right after the dispatch finds it.  A task that is still running gets
    "busy" at once; one that already sent its final status is let go of in a moment, so it's waited for."""
    if not TASK_BUSY.acquire(blocking=False) and not (TASK_ENDED.is_set() and TASK_BUSY.acquire(timeout=5)):
        ctl.send(type="status", task=task.get("id"), state="failed", error="agent is busy")
        return None
    TASK_ENDED.clear()
    with TASK_RUN_LOCK:
        TASK_RUN.clear()
        TASK_RUN.update(id=task.get("id"), claude_over=bool(task.get("attach")))  # attaching runs no Claude
    th = threading.Thread(target=_task_worker, args=(boot, task, ctl), daemon=True)
    th.start()
    return th


def _task_worker(boot, task, ctl) -> None:
    tid = task.get("id")
    summary_done = threading.Event()
    threading.Thread(target=watch_summary, args=(ctl, summary_done), daemon=True).start()
    # still holding TASK_BUSY, so the cache is never dropped while a task runs
    chan = TaskChannel(ctl, tid, [summary_done.set, lambda: send_summary(ctl), drop_file_cache])
    try:
        if task.get("attach"):
            run_attach(boot, task, chan)
        elif boot.get("profile") == "research":
            run_research_task(boot, task, chan)
        elif not boot.get("repo"):
            run_scratch_task(boot, task, chan)
        else:
            run_task(boot, task, chan)
    except Exception as e:  # noqa: BLE001 - report everything to the host
        log(f"!! task failed: {e.__class__.__name__}: {e}")
        chan.send(type="status", task=tid, state="failed", error=str(e)[:500])
    finally:
        try:
            chan.end()  # when no final status went out
            with TASK_RUN_LOCK:
                TASK_RUN.clear()  # an interrupt from now on finds nothing running
            task.clear()  # drop secrets
        finally:
            TASK_BUSY.release()  # the panel stops the VM if it should stop after each task


def interrupted() -> bool:
    return bool(TASK_RUN.get("interrupt"))


def encoded(text: str) -> bytes:
    """Before anything starts, so a message that can't be encoded fails the task, not Claude's prompt."""
    try:
        return text.encode()
    except UnicodeEncodeError:
        raise RuntimeError("the message has text that isn't valid Unicode, so it can't be given to Claude Code") from None


# A memory limit for the task's Claude Code run and all it starts: the VM's memory minus a reserve. When
# the agent's tools need more (Ghidra and jadx at once, say), the kernel ends the biggest of them right
# away, inside the limit, instead of the whole VM crawling with no memory left, the runner too. systemd
# starts the run in a scope with the limit and switches to the agent user itself, so the runner checks
# once, at start, that it does that exactly as Popen would: the agent's uid and gid, no other groups.
# Otherwise runs start as before. The agent can't change the limit: the scope is systemd's, as root.
SYSTEMD_RUN = "/usr/bin/systemd-run"
MEMORY_LIMIT: dict = {}  # "argv": what goes before a Claude Code run, "kib": its limit, "checking": the check runs
MEMORY_CHECKED = threading.Event()
ID_CHECK = ("/bin/sh", "-c", "id -u; id -g; id -G")  # who systemd-run runs a command as (id takes one of these at a time)


def agent_memory_kib() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            total = next(int(ln.split()[1]) for ln in f if ln.startswith("MemTotal:"))
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    reserve = min(2 << 20, max(512 << 10, total // 10))  # KiB: 512 MiB to 2 GiB for the system and the runner
    return total - reserve if total - reserve >= 512 << 10 else None


def check_memory_limit() -> None:
    try:
        kib = agent_memory_kib()
        pw = pwd.getpwnam(AGENT)
        if kib is None or not os.path.exists(SYSTEMD_RUN):
            log("memory: no limit for Claude Code runs (no systemd-run, or too little memory)")
            return
        argv = [SYSTEMD_RUN, "--scope", "--quiet", "--collect", f"--uid={pw.pw_uid}", f"--gid={pw.pw_gid}",
                "-p", f"MemoryMax={kib}K", "-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue", "--"]
        r = subprocess.run(argv + [*ID_CHECK], env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        want = f"{pw.pw_uid}\n{pw.pw_gid}\n{pw.pw_gid}\n"
        if r.returncode != 0 or r.stdout.decode(errors="replace") != want:
            log(f"memory: no limit for Claude Code runs (systemd-run gave {r.returncode}, "
                f"{r.stdout.decode(errors='replace').split()!r}, {r.stderr.decode(errors='replace').strip()[:200]!r})")
            return
        MEMORY_LIMIT.update(argv=argv, kib=kib)
        log(f"memory: Claude Code runs get at most {kib >> 10} MiB, the rest stays for the VM")
    except (OSError, KeyError, subprocess.SubprocessError) as e:
        log(f"memory: no limit for Claude Code runs ({e})")
    finally:
        MEMORY_LIMIT.pop("checking", None)
        MEMORY_CHECKED.set()


def run_as_agent(argv, env, cwd, timeout=None, check=True, quiet=False, relay=None, stdin_text=None, stoppable=False,
                 live=None):
    """stoppable: this is the task's Claude Code run, which Stop ends, and which doesn't start at
    all (returns None) once Stop is under way.  Nothing else is cut off, so a git clone or push
    that Stop comes in the middle of still finishes.  live: a LiveInput that writes its stdin."""
    try:
        return _run_as_agent(argv, env, cwd, timeout, check, quiet, relay, stdin_text, stoppable, live)
    finally:
        if relay is not None:  # its flush timer stops, also when the run couldn't start
            relay.close()
        if live is not None:
            live.close()


def _run_as_agent(argv, env, cwd, timeout, check, quiet, relay, stdin_text, stoppable, live):
    if stoppable and (STOPPING.is_set() or interrupted()):  # Stop came first (after a clone, say), so Claude doesn't start
        log("stopping: Claude Code doesn't start")
        return None
    pw = pwd.getpwnam(AGENT)
    stdin_data = None if stdin_text is None else encoded(stdin_text)
    if not quiet:
        log("$ " + " ".join(a if len(a) < 120 else a[:117] + "..." for a in argv))
    if stoppable and MEMORY_LIMIT.get("checking"):  # the first task may come before the check is done
        MEMORY_CHECKED.wait(30)
    scope = MEMORY_LIMIT.get("argv") if stoppable else None
    # in the scope, systemd-run switches to the agent user itself (checked at start), then runs argv in its place
    p = subprocess.Popen([*scope, *argv] if scope else argv, cwd=cwd, env=env,
                         **({} if scope else {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}),
                         stdin=subprocess.DEVNULL if stdin_data is None and live is None else subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    if stoppable:
        CURRENT_RUN["proc"] = p
        if STOPPING.is_set() or interrupted():  # Stop came before this run registered, so it ends at once
            log("stopping: Claude Code is ended right away")
            stop_current_run()
    late = threading.Event()  # set when the run is over, after which a leftover process's output only goes to the log

    def reader():
        try:
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode(errors="replace")
                if relay is not None and not late.is_set():
                    line = relay.feed(line)
                if line is not None:
                    log("| " + line)
        finally:
            p.stdout.close()
    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    if live is not None:
        live.start(p.stdin)
    elif stdin_data is not None:
        def feed():
            try:
                p.stdin.write(stdin_data)
            except OSError:
                pass  # it exited without reading
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        threading.Thread(target=feed, daemon=True).start()
    timer = None
    if timeout:
        def _kill():
            log(f"!! timeout after {timeout}s, killing {argv[0]}")
            try:
                os.killpg(p.pid, signal.SIGKILL)  # whole process group, incl. children
            except ProcessLookupError:
                pass
        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()
    rc = p.wait()
    if stoppable:
        CURRENT_RUN.pop("proc", None)
    if timer:
        timer.cancel()
    # A leftover background process may still hold the pipe; don't wait forever.
    rt.join(10)
    late.set()
    if check and rc != 0:
        raise RuntimeError(f"{argv[0]} exited with {rc}")
    return rc


def git_out(args, env, cwd, check=True) -> str:
    pw = pwd.getpwnam(AGENT)
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env,
                       user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], check=check,
                       capture_output=True, stdin=subprocess.DEVNULL)
    return r.stdout.decode().strip()


def _pkill_agent() -> bool:
    """True if something of the agent's was still running."""
    return subprocess.run(["pkill", "-KILL", "-u", AGENT], check=False).returncode == 0


def kill_agent_processes() -> None:
    """Make sure nothing the agent started is still running before we commit/push.
    Fails closed: if something keeps respawning, the task fails.  Waits while the memory
    export runs: its packer runs as the agent too."""
    with AGENT_LOCK:
        for _ in range(20):
            if not _pkill_agent():
                return  # nothing left
            time.sleep(0.5)
    raise RuntimeError(f"processes of {AGENT} keep running; not committing or pushing")


def sanitize_repo_config(repo_dir: str, repo_url: str) -> None:
    """Replace .git/config (agent-writable) with a minimal one before the
    runner's own git operations, so agent-set url.*.pushInsteadOf,
    push.followTags, include.path, core.fsmonitor, filters etc. can't widen
    or redirect the push.  Done as the agent: it's the agent's file."""
    as_agent("sanitize_repo_config", repo_dir, repo_url)


def _h_sanitize_repo_config(repo_dir: str, repo_url: str) -> None:
    path = os.path.join(repo_dir, ".git", "config")
    exts = subprocess.run(["git", "config", "--file", path, "--get-regexp", r"^extensions\."],
                          capture_output=True, check=False).stdout.decode().splitlines()
    lines = ["[core]", "\trepositoryformatversion = 1" if exts else "\trepositoryformatversion = 0",
             "\tfilemode = true", "\tbare = false", "\tlogallrefupdates = true"]
    if exts:
        lines.append("[extensions]")
        for e in exts:
            k, _, v = e.partition(" ")
            k = k.split(".", 1)[1]
            if k.lower() in ("objectformat", "refstorage") and v.isalnum():
                lines.append(f"\t{k} = {v}")
    lines += ['[remote "origin"]', f"\turl = {repo_url}", "\tfetch = +refs/heads/*:refs/remotes/origin/*"]
    tmp = path + ".cilly"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def open_pr(repo, token, branch, base, title, body) -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls", method="POST",
                                 data=json.dumps({"title": title, "head": branch, "base": base, "draft": True,
                                                  "body": body}).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY}))
    try:
        with opener.open(req, timeout=60) as r:
            return json.load(r)["html_url"]
    except urllib.error.HTTPError as e:
        msg = e.read(500).decode(errors="replace")
        if e.code == 422 and "already exists" in msg:
            return find_pr(repo, token, branch) or ""
        raise RuntimeError(f"PR creation failed: {e.code} {msg}") from None


def update_pr(repo, token, url, title, body) -> None:
    """Give an open pull request the agent's newer title and description."""
    m = re.search(r"/pull/(\d+)$", url or "")
    if not m:
        return
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls/{m.group(1)}", method="PATCH",
                                 data=json.dumps({"title": title, "body": body}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY}))
    try:
        opener.open(req, timeout=60).close()
    except urllib.error.URLError as e:
        log(f"could not update the pull request: {e}")


def has_new_work(ahead: int, head: str, head_before: str, pushed_head: str) -> bool:
    """Push and open a pull request only for new work, so one you closed or merged stays that way while
    the agent just talks. Commits from this turn count, and so does a branch on GitHub that is behind
    (the last push failed). A branch deleted after its merge doesn't come back on its own."""
    if ahead == 0:
        return False
    return head != head_before or bool(pushed_head and pushed_head != head)


def leftover_subject(status_lines: list) -> str:
    """A commit message for what the agent left uncommitted, from `git diff --cached --name-status`:
    "Add banana.txt", "Update Sell.java and plugin.yml", "Update 5 files"."""
    verbs = {"A": "add", "C": "add", "M": "update", "T": "update", "D": "remove", "R": "rename"}
    groups: dict = {}
    for ln in status_lines:
        parts = ln.split("\t")
        if len(parts) >= 2 and parts[-1]:
            groups.setdefault(verbs.get(parts[0][:1], "update"), []).append(os.path.basename(parts[-1]))
    n = sum(len(v) for v in groups.values())
    if not n:
        return "Commit remaining work"
    if n > 3:
        return (next(iter(groups)).capitalize() if len(groups) == 1 else "Update") + f" {n} files"
    def names(xs):
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    s = ", ".join(f"{verb} {names(xs)}" for verb, xs in groups.items())
    s = s[0].upper() + s[1:]
    return s if len(s) <= 72 else (next(iter(groups)).capitalize() if len(groups) == 1 else "Update") + f" {n} files"


PR_NOTE = "pull-request.md"  # in the agent's home, outside the repository


def _h_read_pr_note(path: str, since: float) -> dict | None:
    """(as the agent) The pull request title and description the agent wrote this turn, if any."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if st.st_mode & 0o170000 != 0o100000 or st.st_mtime < since or st.st_size > 64 * 1024:
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().strip().splitlines()
    if not lines:
        return None
    return {"title": lines[0], "body": "\n".join(lines[1:])}


def pr_text(note, commits: list, instructions: str, pr_title: str = "") -> tuple:
    """Title and description for the pull request: the agent's own note if it wrote one, else its
    commits, with your request folded away at the end. No tool or AI attribution."""
    def line(s):
        return re.sub(r"[\x00-\x1f\x7f]", "", str(s)).lstrip("# ").strip()
    first = next((ln.strip() for ln in instructions.splitlines()
                  if ln.strip() and not ln.startswith("Reference files provided by the user")), "Changes")
    title = (line(pr_title) or (line(note["title"]) if note else "")
             or (line(commits[0]) if len(commits) == 1 else "") or line(first[:70] + ("..." if len(first) > 70 else "")))
    parts = []
    if note and note["body"].strip():
        parts.append(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", note["body"]).strip()[:20000])
    if commits:
        parts.append("### Commits\n\n" + "\n".join(f"- {line(c)}" for c in commits[:30])
                     + (f"\n- and {len(commits) - 30} more" if len(commits) > 30 else ""))
    quoted = "\n".join("> " + ln for ln in instructions[:3000].strip().splitlines())
    parts.append(f"<details><summary>The request</summary>\n\n{quoted}\n\n</details>")
    return title[:120] or "Changes", "\n\n".join(parts)


def find_pr(repo, token, branch) -> str | None:
    owner = repo.split("/", 1)[0]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls?state=open&head={owner}:{urllib.request.quote(branch)}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY}))
    try:
        with opener.open(req, timeout=60) as r:
            prs = json.load(r)
        return prs[0]["html_url"] if prs else None
    except (urllib.error.URLError, ValueError, KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def repo_dir_for(boot) -> str:
    if boot.get("repo_in_scratch"):  # attached later, so the repo lives where the scratch work was
        return home_dir("scratch")
    return os.path.join(pwd.getpwnam(AGENT).pw_dir, "work", boot["repo"].split("/", 1)[1])


MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-\[\]]{0,63}$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def conversation_args(task, state, cwd) -> list:
    """--resume an earlier conversation you picked, --continue the last one, or start fresh."""
    sid = task.get("resume")
    if isinstance(sid, str) and SESSION_RE.fullmatch(sid):
        if not as_agent("has_session", sid):
            raise RuntimeError("that conversation is no longer in this VM (an ephemeral VM loses it when it stops)")
        log(f"resuming conversation {sid}")
        return ["--resume", sid]
    if task.get("continue") and state.get("conversation", {}).get(cwd):
        log("continuing the previous conversation")
        return task.get("continue_args") or []
    return []


def _h_has_session(sid: str) -> bool:
    if not SESSION_RE.fullmatch(sid):
        return False
    projects = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "projects")
    try:
        return any(os.path.isfile(os.path.join(projects, d, sid + ".jsonl")) for d in os.listdir(projects))
    except OSError:
        return False


def claude_argv(task) -> tuple:
    """The command and the prompt to give it on stdin.  For Claude Code's own command (claude -p),
    an argument that is just {instructions} becomes stdin, which it reads there, so a message
    starting with "-" isn't taken for an option and its length has no argument limit.  Configs
    saved with it in the command still work, and any other command keeps it as an argument."""
    raw = task["agent_argv"]
    claude = os.path.basename(raw[0]) == "claude" and ("-p" in raw or "--print" in raw)
    prompt = task["instructions"] if claude and "{instructions}" in raw else None
    argv = [a.replace("{instructions}", task["instructions"])
             .replace("{permission_mode}", task.get("permission_mode", "bypassPermissions"))
            for a in raw if not (prompt is not None and a == "{instructions}")]
    model = task.get("model")
    if isinstance(model, str) and MODEL_RE.match(model):  # chosen in the panel, per agent
        argv += ["--model", model]
    if task.get("effort") in EFFORTS:
        argv += ["--effort", task["effort"]]
    return argv, prompt


def run_attach(boot, task, ctl) -> None:
    """Turn ~/scratch into a checkout of a (new, non-empty) GitHub repo: the
    scratch files are laid on top of the default branch on a fresh agent
    branch, pushed, and a draft PR is opened.  ~/scratch stays the working
    directory, so the Claude conversation carries on."""
    tid, repo, branch = task["id"], task["repo"], task["branch"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise RuntimeError("bad repository name")
    repo_url = f"https://github.com/{repo}.git"
    env = agent_env(boot, task)
    work = home_dir("scratch")
    ensure_agent_dir(work)
    log(f"=== attaching {repo} to ~/scratch on {branch} ===")
    ctl.send(type="status", task=tid, state="running", branch=branch)
    old_git = os.path.join(work, ".git")
    if os.path.lexists(old_git):
        keep = home_dir(f"scratch-git-{tid}")
        as_agent("move", old_git, keep)
        log(f"attach: the agent's own ~/scratch/.git was moved to {keep}")
    run_as_agent(["git", "init", "-q", "-b", branch], env, work)
    sanitize_repo_config(work, repo_url)
    run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "fetch", "origin"], env, work)
    head = git_out(["ls-remote", "--symref", "origin", "HEAD"], env, work, check=False)
    m = re.match(r"ref: refs/heads/(\S+)\s+HEAD", head)
    if not m:
        raise RuntimeError(f"{repo} is empty: create it on GitHub with a README first")
    base = m.group(1)
    # Point the branch at the default branch without touching the files, then
    # bring back repo files that scratch doesn't have.  Files both have keep
    # the scratch version (they show up as changes in the PR).
    run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "reset", "-q", "--mixed", f"origin/{base}"], env, work)
    missing = [f for f in git_out(["ls-files", "--deleted", "-z"], env, work).split("\0") if f]
    if missing:
        run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "checkout", "--"] + missing, env, work, quiet=True)
    sanitize_repo_config(work, repo_url)
    exclude = [f":(exclude){p}" for p in task.get("commit_exclude") or [] if p and not p.startswith(("-", ":"))]
    run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "add", "-A", "--", "."] + exclude, env, work)
    if not git_out(["diff", "--cached", "--name-only"], env, work, check=False):
        raise RuntimeError("nothing in ~/scratch differs from the repository")
    run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "Add work from scratch"], env, work)
    run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "-c", "push.followTags=false", "push",
                  "--no-follow-tags", repo_url, f"HEAD:refs/heads/{branch}"], env, work)
    url = open_pr(repo, task["env"]["GH_TOKEN"], branch, base, task.get("pr_title") or "Add work from scratch",
                  "Work from a scratch session, attached to this repository.")
    boot["repo"], boot["repo_in_scratch"] = repo, True
    ctl.send(type="status", task=tid, state="done", agent_rc=0, branch=branch, pushed=True, pr=url)


def run_task(boot, task, ctl: Control) -> None:
    tid = task["id"]
    repo = boot["repo"]
    repo_url = f"https://github.com/{repo}.git"
    env = agent_env(boot, task)
    work = home_dir("work")
    ensure_agent_dir(work)
    ensure_agent_dir(home_dir("outbox"))  # files for you, which the Export button takes out
    repo_dir = repo_dir_for(boot)
    branch = task["branch"]
    log(f"=== task {tid} on {branch} ===")

    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        run_as_agent(["git", "clone", repo_url, repo_dir], env, work)
    else:
        sanitize_repo_config(repo_dir, repo_url)
        run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "fetch", "--prune", "origin"], env, repo_dir)
    base = git_out(["rev-parse", "--abbrev-ref", "origin/HEAD"], env, repo_dir, check=False)
    base = base.split("/", 1)[1] if base.startswith("origin/") else "main"

    if task.get("branch_fresh") and not git_out(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], env, repo_dir,
                                                check=False):
        branch = free_branch(branch, base, env, repo_dir)  # a new name GitHub already has (someone's): don't work on theirs
    current = git_out(["rev-parse", "--abbrev-ref", "HEAD"], env, repo_dir, check=False)
    if current != branch:
        if git_out(["status", "--porcelain"], env, repo_dir, check=False):
            run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "stash", "push", "-u", "-m",
                          f"cilly: before switching to {branch}"], env, repo_dir, check=False)
        local = git_out(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], env, repo_dir, check=False)
        remote = git_out(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], env, repo_dir,
                         check=False)
        if local:
            run_as_agent(["git", "checkout", branch], env, repo_dir)
        elif remote:
            run_as_agent(["git", "checkout", "-b", branch, f"origin/{branch}"], env, repo_dir)
        else:
            run_as_agent(["git", "checkout", "-b", branch, f"origin/{base}"], env, repo_dir)
    ctl.send(type="status", task=tid, state="running", branch=branch)
    head_before = git_out(["rev-parse", "HEAD"], env, repo_dir, check=False)

    started = time.time()
    agent_rc = run_claude(task, ctl, env, repo_dir)
    log(f"agent exited with {agent_rc}")

    kill_agent_processes()
    report_outbox_images(task, ctl, started)
    send_research_requests(task, ctl)
    send_questions(task, ctl)
    sanitize_repo_config(repo_dir, repo_url)
    branch = renamed_branch(task, branch, base, env, repo_dir)
    if git_out(["status", "--porcelain"], env, repo_dir, check=False):
        exclude = [f":(exclude){p}" for p in task.get("commit_exclude") or []
                   if p and not p.startswith(("-", ":"))]
        run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "add", "-A", "--", "."] + exclude, env, repo_dir)
        if git_out(["diff", "--cached", "--name-only"], env, repo_dir, check=False):
            staged = git_out(["diff", "--cached", "--name-status"], env, repo_dir, check=False).splitlines()
            run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", leftover_subject(staged)],
                         env, repo_dir, check=False)
    ahead = int(git_out(["rev-list", "--count", f"origin/{base}..HEAD"], env, repo_dir, check=False) or "0")
    head = git_out(["rev-parse", "HEAD"], env, repo_dir, check=False)
    pushed_head = git_out(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], env, repo_dir, check=False)
    try:
        note = as_agent("read_pr_note", home_dir(PR_NOTE), started, timeout=30)
    except RuntimeError:
        note = None
    commits = [c for c in git_out(["log", "--format=%s", f"origin/{base}..HEAD"], env, repo_dir, check=False).splitlines() if c]
    title, body = pr_text(note, commits, task["instructions"], task.get("pr_title") or "")
    if not has_new_work(ahead, head, head_before, pushed_head):
        url = find_pr(repo, task["env"]["GH_TOKEN"], branch) if ahead and note else None
        if url:  # nothing new, but the agent rewrote the description of the open pull request
            update_pr(repo, task["env"]["GH_TOKEN"], url, title, body)
        ctl.send(type="status", task=tid, state=ended(), agent_rc=agent_rc, branch=branch, pushed=False)
        return
    # Push only this branch, to the canonical URL.
    run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "-c", "push.followTags=false", "push",
                  "--no-follow-tags", repo_url, f"HEAD:refs/heads/{branch}"], env, repo_dir)
    url = open_pr(repo, task["env"]["GH_TOKEN"], branch, base, title, body)
    if note:  # it rewrote its note this turn, so the pull request (maybe from an earlier turn) follows
        update_pr(repo, task["env"]["GH_TOKEN"], url, title, body)
    ctl.send(type="status", task=tid, state=ended(), agent_rc=agent_rc, branch=branch, pushed=True, pr=url)


def remote_branch(name, env, repo_dir) -> str:
    """The commit GitHub has for a branch, or "" when it has no branch of that name."""
    for line in git_out(["ls-remote", "--heads", "origin", f"refs/heads/{name}"], env, repo_dir, check=False).splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{name}":
            return sha
    return ""


def free_branch(name, base, env, repo_dir) -> str:
    """name, or name-2 up to name-9 when GitHub already has that branch and it isn't this work: this
    work's commits are in HEAD and not yet in the default branch (a branch just made from it is someone's)."""
    def ancestor(sha, of):
        return git_out(["merge-base", sha, of], env, repo_dir, check=False) == sha
    for n in [name] + [f"{name}-{i}" for i in range(2, 10)]:
        sha = remote_branch(n, env, repo_dir)
        if not sha or (ancestor(sha, "HEAD") and not ancestor(sha, f"origin/{base}")):
            return n
    return name


def renamed_branch(task, branch, base, env, repo_dir) -> str:
    """The agent may rename its branch until it's first pushed (git branch -m), to a name like
    the panel says (branch_rename).  Anything else is pushed under the name the panel gave."""
    pattern = task.get("branch_rename")
    cur = git_out(["symbolic-ref", "--quiet", "--short", "HEAD"], env, repo_dir, check=False)
    if not isinstance(pattern, str) or not cur or cur == branch or not re.fullmatch(pattern, cur):
        return branch
    if remote_branch(branch, env, repo_dir):
        log(f"the agent renamed its branch to {cur}, but {branch} is already pushed, so it keeps that name")
        return branch
    name = free_branch(cur, base, env, repo_dir)
    if name != cur:
        run_as_agent(["git", "-c", "core.hooksPath=/dev/null", "branch", "-m", cur, name], env, repo_dir)
    log(f"the agent named its branch {name}")
    return name


def ended(ok: bool = True) -> str:
    return "interrupted" if interrupted() else "done" if ok else "failed"


def _plugin_run(claude: str, args: list, env: dict, cwd: str) -> dict:
    """`claude plugin ... --json` as the agent user: the result line it printed, or a failure."""
    pw = pwd.getpwnam(AGENT)
    log("$ claude plugin " + " ".join(args))
    try:
        r = subprocess.run([claude, "plugin", *args, "--json"], cwd=cwd, env=env, user=pw.pw_uid, group=pw.pw_gid,
                           extra_groups=[], stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"outcome": "failed", "message": "it took more than 5 min"}
    except OSError as e:
        return {"outcome": "failed", "message": e.__class__.__name__}
    for line in reversed(r.stdout.decode(errors="replace").splitlines()):
        try:
            out = json.loads(line)
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    err = r.stderr.decode(errors="replace").strip().splitlines()
    return {"outcome": "failed", "message": (err[-1] if err else f"it exited with {r.returncode}")}


def _load_plugins_file() -> dict:
    try:
        with open(PLUGINS_FILE, encoding="utf-8") as f:
            got = json.load(f)
        return {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_plugins_file(have: dict) -> None:
    os.makedirs(os.path.dirname(PLUGINS_FILE), mode=0o700, exist_ok=True)
    _write_root_file(PLUGINS_FILE, json.dumps(have).encode())
    os.chmod(PLUGINS_FILE, 0o600)


def ensure_plugins(task, ctl, env, cwd) -> None:
    """Install the plugins the panel ticked for this agent, for the agent user, and remove the ones it
    took away, before Claude Code starts.  The results go into the chat.  A plugin that installs by
    running a command of its marketplace's is only installed once you allowed that exact command in
    the panel (its sha256), and the panel hears about it so it can ask you."""
    raw = task.get("agent_argv") or []
    if "plugins" not in task or not raw or os.path.basename(raw[0]) != "claude":
        return  # a panel from before plugins: leave whatever is installed alone
    want = [p for p in task["plugins"] if isinstance(p, dict) and PLUGIN_NAME_RE.fullmatch(str(p.get("name", "")))
            and isinstance(p.get("marketplace"), str) and 0 < len(p["marketplace"]) <= 300 and not p["marketplace"].startswith("-")][:50]
    have = _load_plugins_file()
    if not want and not have:
        return
    secret = task.get("env") or {}
    env = {k: v for k, v in env.items() if k not in secret}  # an install needs no token
    events, results = [], []
    for name, pid in list(have.items()):
        if name not in {p["name"] for p in want}:
            _plugin_run(raw[0], ["uninstall", pid], env, cwd)
            del have[name]
            events.append({"k": "status", "text": f"Removed the plugin {name}"})
    for p in want:
        if p["name"] in have:
            continue
        args = ["install", p["name"], "--marketplace", p["marketplace"]]
        if re.fullmatch(r"[0-9a-f]{64}", str(p.get("accept", ""))):
            args += ["--accept-command", p["accept"]]
        out = _plugin_run(raw[0], args, env, cwd)
        shown = out.get("shownCommand") if isinstance(out.get("shownCommand"), dict) else {}
        if out.get("outcome") == "ok":
            have[p["name"]] = str(out.get("pluginId") or out.get("plugin") or p["name"])[:200]
            results.append({"name": p["name"], "state": "installed"})
            events.append({"k": "status", "text": f"Installed the plugin {p['name']}"})
        elif re.fullmatch(r"[0-9a-f]{64}", str(shown.get("sha256", ""))):
            cmd = next((str(shown[k]) for k in ("command", "text", "display") if isinstance(shown.get(k), (str, list))), "")
            results.append({"name": p["name"], "state": "needs_command", "sha256": shown["sha256"], "command": cmd[:500]})
            events.append({"k": "status", "text": f"The plugin {p['name']} installs by running a command. Allow it in the panel first."})
        else:
            why = str(out.get("message") or "it failed")
            results.append({"name": p["name"], "state": "failed", "message": why[:300]})
            events.append({"k": "status", "text": f"Couldn't install the plugin {p['name']}: {why[:200]}"})
    _save_plugins_file(have)
    if events:
        ctl.send(type="chat", task=task["id"], events=events)
    if results:
        ctl.send(type="plugins", task=task["id"], results=results)


def _load_skills_file() -> dict:
    try:
        with open(SKILLS_FILE, encoding="utf-8") as f:
            got = json.load(f)
        return {k: v for k, v in got.items() if isinstance(k, str) and SKILL_NAME_RE.fullmatch(k) and isinstance(v, str)} \
            if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _skill_tar(name: str, sha: str) -> str | None:
    """/attachments/skills/<name>.tar when it's a plain root-owned file with the hash the panel gave."""
    path = os.path.join(ATTACH_DIR, "skills", name + ".tar")
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if os.path.islink(path) or not os.path.isfile(path) or st.st_uid != os.geteuid():  # root's: the agent can't have swapped it
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return path if h.hexdigest() == sha else None


def _save_skills_file(have: dict) -> None:
    os.makedirs(os.path.dirname(SKILLS_FILE), mode=0o700, exist_ok=True)
    _write_root_file(SKILLS_FILE, json.dumps(have).encode())
    os.chmod(SKILLS_FILE, 0o600)


def ensure_skills(task, ctl) -> None:
    """Unpack the skills the panel ticked for this agent into its ~/.claude/skills/, as the agent user, and
    take away the ones cilly put there that it no longer has.  Before Claude Code starts, so it sees them."""
    raw = task.get("agent_argv") or []
    if "skills" not in task or not raw or os.path.basename(raw[0]) != "claude":
        return  # a panel from before skills: leave whatever is there alone
    want = {s["name"]: s["sha256"] for s in task["skills"][:50] if isinstance(s, dict)
            and SKILL_NAME_RE.fullmatch(str(s.get("name", ""))) and re.fullmatch(r"[0-9a-f]{64}", str(s.get("sha256", "")))}
    have = _load_skills_file()
    if not want and not have:
        return
    skills_dir, events = home_dir(".claude", "skills"), []
    for name in [n for n in have if n not in want]:
        try:
            as_agent("remove_skill", skills_dir, name, timeout=60)
            events.append({"k": "status", "text": f"Removed the skill {name}"})
        except RuntimeError as e:
            log(f"!! skills: {e}")
        del have[name]
    for name, sha in want.items():
        if have.get(name) == sha:
            continue
        tar = _skill_tar(name, sha)
        if not tar:
            events.append({"k": "status", "text": f"Couldn't add the skill {name}: its files haven't arrived. Try again in a moment."})
            continue
        try:
            as_agent("install_skill", tar, skills_dir, name, timeout=120)
        except RuntimeError as e:
            log(f"!! skills: {e}")
            events.append({"k": "status", "text": f"Couldn't add the skill {name}"})
            continue
        events.append({"k": "status", "text": f"{'Updated' if name in have else 'Added'} the skill {name}"})
        have[name] = sha
    _save_skills_file(have)
    if events:
        ctl.send(type="chat", task=task["id"], events=events)


def run_claude(task, ctl, env, cwd):
    """Claude Code for one task, in the conversation it asked for.  When you stopped the last run here
    and continuing fails before Claude does anything, the conversation was left broken: start a new
    one and say so in the chat."""
    try:
        ensure_plugins(task, ctl, env, cwd)
    except Exception as e:  # noqa: BLE001 - a plugin never keeps the task from running
        log(f"!! plugins: {e.__class__.__name__}: {e}")
    try:
        ensure_skills(task, ctl)
    except Exception as e:  # noqa: BLE001 - nor does a skill
        log(f"!! skills: {e.__class__.__name__}: {e}")
    state = load_state()
    argv, prompt = claude_argv(task)
    conv = conversation_args(task, state, cwd)
    stream = prompt is not None and streams_input(argv, env)
    if stream:
        encoded(prompt)
        argv += ["--input-format", "stream-json"]
        env = dict(env, CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS="1")  # without it, no session_state_changed

    def run(args):
        live = LiveInput(ctl, task["id"], prompt, bg_ceiling(env)) if stream else None
        with TASK_RUN_LOCK:
            if TASK_RUN.get("id") == task["id"]:
                TASK_RUN["live"] = live
        relay = ChatRelay(ctl, task["id"], live)
        try:
            return run_as_agent(args, env, cwd, timeout=task.get("timeout_s"), check=False, relay=relay,
                                stdin_text=None if stream else prompt, stoppable=True, live=live), relay
        finally:
            with TASK_RUN_LOCK:
                TASK_RUN.pop("live", None)

    rc, relay = run(argv[:1] + conv + argv[1:])
    if rc and conv and state.get("interrupted", {}).get(cwd) and not relay.spoke and not interrupted() and not STOPPING.is_set():
        log("the conversation you stopped couldn't continue; starting a new one")
        ctl.send(type="chat", task=task["id"], events=[
            {"k": "status", "text": "The conversation you stopped couldn't continue, so this is a new one."}])
        rc, _ = run(argv)
    with TASK_RUN_LOCK:  # only now: until here an interrupt still ends a run, or keeps the next from starting
        if TASK_RUN:
            TASK_RUN["claude_over"] = True
    mark_conversation(state, cwd, rc)
    return rc


STREAM_INPUT: dict = {}  # Claude Code's command -> whether it takes --input-format stream-json


def bg_ceiling(env) -> float | None:
    """The agent's background work limit in seconds, which Claude Code gets as an environment variable,
    or None when it has none (0 too: it waits for them until the task's time limit)."""
    try:
        ms = int(env.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS") or 0)
    except ValueError:
        return None
    return ms / 1000 if ms > 0 else None


def streams_input(argv, env) -> bool:
    """Claude Code reads its prompt as stream-json, so you can send it messages while it works:
    only with stream-json output, and only when this Claude Code has the option (asked once)."""
    out = argv.index("--output-format") + 1 if "--output-format" in argv else 0
    if not out or argv[out:out + 1] != ["stream-json"]:
        return False
    if argv[0] not in STREAM_INPUT:
        pw = pwd.getpwnam(AGENT)
        try:
            r = subprocess.run([argv[0], "--help"], env=env, user=pw.pw_uid, group=pw.pw_gid, extra_groups=[],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            STREAM_INPUT[argv[0]] = b"--input-format" in r.stdout and b"stream-json" in r.stdout
        except (OSError, subprocess.TimeoutExpired):
            STREAM_INPUT[argv[0]] = False
        log(f"{argv[0]} {'takes' if STREAM_INPUT[argv[0]] else 'does not take'} messages while it works")
    return STREAM_INPUT[argv[0]]


def mark_conversation(state: dict, cwd: str, agent_rc) -> None:
    """The next task may continue the conversation, but only if Claude really ran: after a run that
    Stop kept from starting there's none, and --continue would fail."""
    if agent_rc is None:
        return
    state.setdefault("conversation", {})[cwd] = True
    if interrupted():
        state.setdefault("interrupted", {})[cwd] = True
    else:
        state.setdefault("interrupted", {}).pop(cwd, None)
    save_state(state)


def home_dir(*parts) -> str:
    return os.path.join(pwd.getpwnam(AGENT).pw_dir, *parts)


def ensure_agent_dir(path: str) -> None:
    as_agent("mkdirs", [path])


def collect_text_files(root: str, max_total: int, max_file: int) -> list:
    return as_agent("collect_text_files", root, max_total, max_file)


def _h_collect_text_files(root: str, max_total: int, max_file: int, max_depth: int = 3) -> list:
    """Regular UTF-8 text files under root (no symlinks), within the panel's limits."""
    out, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count("/") + 1
        dirnames[:] = sorted(d for d in dirnames if OUTBOX_NAME.match(d) and depth < max_depth
                             and not os.path.islink(os.path.join(dirpath, d)))
        for n in sorted(filenames):
            p = os.path.join(dirpath, n)
            if not OUTBOX_NAME.match(n) or os.path.islink(p) or not os.path.isfile(p):
                if not os.path.islink(p) and os.path.isfile(p):
                    log(f"outbox: skipping {n[:80]!r} (the name is too long or has characters the panel doesn't take)")
                continue
            if len(out) >= OUTBOX_FILES_MAX:
                log(f"outbox: skipping {n!r} (more than {OUTBOX_FILES_MAX} files)")
                continue
            size = os.path.getsize(p)
            if size > max_file or total + size > max_total:
                log(f"outbox: skipping {n!r} (size limit)")
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                log(f"outbox: skipping {n!r} (not UTF-8 text)")
                continue
            if "\x00" in content:
                continue
            total += size
            out.append({"path": n if rel == "." else f"{rel}/{n}", "content": content})
    return out


OUTBOX_ACKS: dict = {}  # task id -> {"event", "result"}: the panel's answer to that task's findings


def send_outbox(ctl, tid: str, files: list):
    """Findings to the panel in pieces that each stay under its line limit, however much
    escaping they need.  Returns "ok" once the panel stored them, ("refused", why), or None
    when no answer came."""
    data = json.dumps(files, ensure_ascii=False).encode()
    ack = OUTBOX_ACKS[tid] = {"event": threading.Event(), "result": None}
    try:
        chunks = [data[i:i + EXPORT_CHUNK] for i in range(0, len(data), EXPORT_CHUNK)]
        for seq, c in enumerate(chunks):
            ctl.send(type="outbox_chunk", task=tid, seq=seq, data=base64.b64encode(c).decode())
        ctl.send(type="outbox_end", task=tid, chunks=len(chunks), sha256=hashlib.sha256(data).hexdigest())
        ack["event"].wait(OUTBOX_ACK_TIMEOUT)
        return ack["result"]
    finally:
        OUTBOX_ACKS.pop(tid, None)


def outbox_answer(msg: dict) -> None:
    ack = OUTBOX_ACKS.get(str(msg.get("task", "")))
    if ack:
        ack["result"] = "ok" if msg.get("type") == "outbox_ok" else ("refused", str(msg.get("error") or "no reason given")[:300])
        ack["event"].set()


def _archive(src: str, task_id: str) -> None:
    as_agent("archive_outbox", src, home_dir("sent", task_id))


def _h_archive_outbox(src: str, dst: str) -> None:
    """(as the agent) Move what was sent out of the outbox, so it isn't sent again. Images at its top
    level stay: the chat shows them from here (they're never part of the findings)."""
    os.makedirs(dst, exist_ok=True)
    for n in os.listdir(src):
        p = os.path.join(src, n)
        if os.path.isfile(p) and not os.path.islink(p) and n.lower().endswith(IMAGE_EXT):
            continue
        shutil.move(p, os.path.join(dst, n))


def run_research_task(boot, task, ctl) -> None:
    """Open-web research: no repo, no GitHub token.  Findings in ~/outbox/
    go to the panel after the run, where you review them."""
    tid, started = task["id"], time.time()
    env = agent_env(boot, task)
    work, outbox = home_dir("research"), home_dir("outbox")
    ensure_agent_dir(work)
    ensure_agent_dir(outbox)
    ctl.send(type="status", task=tid, state="running")
    agent_rc = run_claude(task, ctl, env, work)
    kill_agent_processes()
    files = collect_text_files(outbox, OUTBOX_MAX, OUTBOX_FILE_MAX)
    # whatever the answer, these findings leave ~/outbox, so they're never sent again or with a later task
    answer = send_outbox(ctl, tid, files) if files else None
    if answer == "ok":
        _archive(outbox, tid)
        log(f"outbox: sent {len(files)} file(s) to the panel")
    elif answer:
        as_agent("archive_outbox", outbox, home_dir("sent", f"refused-{tid}"))
        log(f"outbox: the panel refused the findings ({answer[1]}). They are in ~/sent/refused-{tid}")
    elif files:
        as_agent("archive_outbox", outbox, home_dir("sent", f"unconfirmed-{tid}"))
        log(f"outbox: the panel didn't confirm the findings. They are in ~/sent/unconfirmed-{tid}")
    else:
        log("outbox: empty (the agent wrote nothing to ~/outbox/)")
    report_outbox_images(task, ctl, started)  # shown in its own chat, through the same checks as any agent's
    send_questions(task, ctl)
    ctl.send(type="status", task=tid, state=ended(), agent_rc=agent_rc, outbox_files=len(files))


def run_scratch_task(boot, task, ctl) -> None:
    """A coding agent without a repository: Claude Code in ~/scratch, no git,
    no GitHub token, no PR.  Useful for trying things out and for MCP work."""
    tid = task["id"]
    env = agent_env(boot, task)
    work = home_dir("scratch")
    ensure_agent_dir(work)
    ensure_agent_dir(home_dir("outbox"))
    ctl.send(type="status", task=tid, state="running")
    started = time.time()
    agent_rc = run_claude(task, ctl, env, work)
    kill_agent_processes()
    report_outbox_images(task, ctl, started)
    send_research_requests(task, ctl)  # agents without a repository can ask for research too
    send_questions(task, ctl)
    state = ended(agent_rc == 0)
    ctl.send(type="status", task=tid, state=state, agent_rc=agent_rc,
             **({} if state != "failed" else {"error": "stopped before Claude Code started" if agent_rc is None
                                              else f"claude exited with {agent_rc} (see the agent log)"}))


def send_research_requests(task, ctl) -> None:
    """A coding agent asks for research by writing ~/requests/*.md."""
    reqs = as_agent("read_requests", home_dir("requests"), home_dir("sent", "requests-" + task["id"]))
    if reqs:
        ctl.send(type="research_requests", task=task["id"], requests=reqs)
        log(f"requests: sent {len(reqs)} research request(s) to the panel")


def send_questions(task, ctl) -> None:
    """The agent asks the user by writing ~/ask/<name>.json.  The panel checks them; this only carries the text.
    An older panel ignores the message."""
    files = as_agent("read_asks", home_dir("ask"), home_dir("sent", "ask-" + task["id"]))
    if files:
        ctl.send(type="questions", task=task["id"], files=files)
        log(f"ask: sent {len(files)} question file(s) to the panel")


def _h_read_asks(ask_dir: str, archive_to: str) -> list:
    if not os.path.isdir(ask_dir) or os.path.islink(ask_dir):
        return []
    files = []
    for n in sorted(os.listdir(ask_dir))[:ASK_FILES]:
        p = os.path.join(ask_dir, n)
        if not n.endswith(".json") or not _SAFE_NAME.match(n) or os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read(ASK_MAX + 1)
        except OSError:
            continue
        files.append({"name": n, "text": data.decode("utf-8", "replace")})  # too big or not JSON: the panel says why
    if files:
        _h_move(ask_dir, archive_to, True)
    return files


def _h_read_requests(req_dir: str, archive_to: str) -> list:
    if not os.path.isdir(req_dir) or os.path.islink(req_dir):
        return []
    reqs = []
    for n in sorted(os.listdir(req_dir))[:10]:
        p = os.path.join(req_dir, n)
        if not n.endswith(".md") or not _SAFE_NAME.match(n) or os.path.islink(p) or not os.path.isfile(p):
            continue
        if os.path.getsize(p) > REQUEST_MAX:
            log(f"requests: {n!r} too large, skipped")
            continue
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read(REQUEST_MAX)
        except (UnicodeDecodeError, OSError):
            continue
        if text.strip():
            reqs.append({"name": n[:-3], "text": text})
    if reqs:
        _h_move(req_dir, archive_to, True)
    return reqs


class Attachments:
    """Receives files you attached to a message (host -> VM).  They arrive in
    chunks on the control port before the task, are checked against their
    hash and end up root-owned and read-only in /attachments/<set>/."""

    def __init__(self):
        self.partial = {}  # (set, name) -> {"f", "seq", "size", "sha"}

    @staticmethod
    def _key(msg):
        sid, name = str(msg.get("set", "")), str(msg.get("name", ""))
        if not sid.isalnum() or len(sid) > 16 or not _SAFE_NAME.match(name) or name in (".", ".."):
            return None
        return sid, name

    def chunk(self, msg) -> None:
        key = self._key(msg)
        if key is None:
            return
        st = self.partial.get(key)
        if st is None and msg.get("seq") == 0:
            os.makedirs(ATTACH_STAGING, mode=0o700, exist_ok=True)
            path = os.path.join(ATTACH_STAGING, f"{key[0]}-{len(self.partial)}-{time.time_ns()}")
            st = self.partial[key] = {"f": open(path, "xb"), "path": path, "seq": 0, "size": 0,
                                      "sha": hashlib.sha256()}
        if st is None or msg.get("seq") != st["seq"]:
            return self._drop(key, "out of order")
        try:
            data = base64.b64decode(str(msg.get("data", "")), validate=True)
        except ValueError:
            return self._drop(key, "bad data")
        st["seq"] += 1
        st["size"] += len(data)
        if st["size"] > ATTACH_MAX:
            return self._drop(key, "too large")
        st["sha"].update(data)
        st["f"].write(data)

    def end(self, msg) -> None:
        key = self._key(msg)
        st = self.partial.pop(key, None) if key else None
        if st is None:
            return
        st["f"].close()
        if msg.get("chunks") != st["seq"] or msg.get("sha256") != st["sha"].hexdigest():
            os.unlink(st["path"])
            log(f"attachments: {key[1]} arrived incomplete, dropped")
            return
        os.makedirs(ATTACH_DIR, mode=0o755, exist_ok=True)
        d = os.path.join(ATTACH_DIR, key[0])
        os.makedirs(d, mode=0o755, exist_ok=True)
        os.chmod(st["path"], 0o444)
        os.replace(st["path"], os.path.join(d, key[1]))
        log(f"attachments: {d}/{key[1]} ({st['size']} bytes)")

    def _drop(self, key, why) -> None:
        st = self.partial.pop(key, None)
        if st:
            st["f"].close()
            os.unlink(st["path"])
        log(f"attachments: {key[1]} dropped ({why})")


def sync_project_files(names, set_id: str = "project") -> None:
    """Drop project files (or skills) the user removed.  /attachments is root-owned, so this runs as root
    only there, on plain files, and never follows a link."""
    if not isinstance(names, list):
        return
    keep = {n for n in names if isinstance(n, str)}
    d = os.path.join(ATTACH_DIR, set_id)
    if not os.path.isdir(d) or os.path.islink(d):
        return
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if n not in keep and os.path.isfile(p) and not os.path.islink(p):
            os.unlink(p)
            log(f"{set_id} files: removed {n}")


def write_inbox(msg) -> None:
    """Research results from the panel, read-only for the agent."""
    did = str(msg.get("id", ""))
    if not did.isalnum() or len(did) > 16:
        return
    os.makedirs(INBOX, mode=0o755, exist_ok=True)
    root = os.path.join(INBOX, did)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, mode=0o755)
    for f in msg.get("files") or []:
        parts = str(f.get("path", "")).split("/")
        if not parts or any(p in ("", ".", "..") or not _SAFE_NAME.match(p) for p in parts):
            continue
        dest = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(str(f.get("content", "")))
        os.chmod(dest, 0o444)
    if msg.get("title"):
        with open(os.path.join(root, "REQUEST.md"), "w", encoding="utf-8") as fh:
            fh.write(str(msg["title"]))
    for dirpath, dirnames, _ in os.walk(root):
        os.chmod(dirpath, 0o555)
    log(f"inbox: research results delivered to {root}")


def _h_pack_dir(out, root: str, skip_git: bool) -> None:
    """(as the agent) Tar regular files and directories under root to `out`;
    links, devices and FIFOs are skipped, and so is .git (the panel leaves it out too)."""
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if skip_git:
                filenames = [n for n in filenames if n.lower() != ".git"]
                dirnames[:] = [d for d in dirnames if d.lower() != ".git"]
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
            rel = os.path.relpath(dirpath, root)
            if rel != ".":
                ti = tarfile.TarInfo(rel)
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(ti)
            for n in sorted(filenames):
                try:
                    f = os.fdopen(os.open(os.path.join(dirpath, n), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
                except OSError:
                    continue
                with f:
                    st = os.fstat(f.fileno())
                    if st.st_mode & 0o170000 != 0o100000:
                        continue
                    ti = tarfile.TarInfo(n if rel == "." else rel + "/" + n)
                    ti.size, ti.mode, ti.mtime = st.st_size, 0o644, int(st.st_mtime)
                    tar.addfile(ti, f)


FILE_LIST_MAX = 1000


def _open_under(root: str, rel: str, final_dir: bool) -> int:
    """(as the agent) Walk from root to rel one name at a time, never following a
    link, and return an fd for it.  .git is never entered."""
    parts = [p for p in rel.split("/") if p not in ("", ".")] if rel else []
    if any(p == ".." or p == ".git" or len(p.encode()) > 255 for p in parts) or len(parts) > 40:
        raise ValueError("bad path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for i, p in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if (not last or final_dir) else os.O_NONBLOCK)
            nfd = os.open(p, flags, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _h_list_dir(root: str, rel: str) -> dict:
    """(as the agent) One folder: names, kinds and sizes.  Links are shown as links, not followed."""
    try:
        fd = _open_under(root, rel, True)
    except (OSError, ValueError) as e:
        return {"error": f"cannot open {rel or '/'}: {getattr(e, 'strerror', None) or e}"}
    out = []
    try:
        with os.scandir(fd) as it:
            for e in it:
                if e.name == ".git":
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                fmt = st.st_mode & 0o170000
                kind = "dir" if fmt == 0o040000 else "file" if fmt == 0o100000 else "link" if fmt == 0o120000 else "other"
                out.append({"name": e.name, "kind": kind, "size": st.st_size if kind == "file" else 0, "mtime": int(st.st_mtime)})
                if len(out) >= FILE_LIST_MAX:
                    break
    finally:
        os.close(fd)
    out.sort(key=lambda x: (x["kind"] != "dir", x["name"].lower()))
    return {"entries": out, "truncated": len(out) >= FILE_LIST_MAX}


def _h_read_file(out, root: str, rel: str, limit: int) -> None:
    """(as the agent) Copy one regular file to `out`; refuse links, devices, FIFOs and big files."""
    fd = _open_under(root, rel, False)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if st.st_mode & 0o170000 != 0o100000:
            raise ValueError("not a regular file")
        if st.st_size > limit:
            raise ValueError(f"larger than {limit} bytes")
        left = limit + 1
        while left > 0:
            chunk = f.read(min(EXPORT_CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            out.write(chunk)


def _file_root(boot, what):
    if what == "repo" and boot.get("repo"):
        return repo_dir_for(boot)
    if what == "attachments":
        return ATTACH_DIR
    if what in EXPORT_SOURCES:
        return home_dir(EXPORT_SOURCES[what])
    return None


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _h_recent_images(root: str, since: float, limit: int = 8) -> list:
    """(as the agent) Images in root (two levels deep) written since `since`, oldest first."""
    found = []
    base = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if dirpath.count("/") - base >= 2:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d != ".git" and not os.path.islink(os.path.join(dirpath, d))]
        for n in filenames:
            if not n.lower().endswith(IMAGE_EXT):
                continue
            p = os.path.join(dirpath, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if st.st_mode & 0o170000 == 0o100000 and st.st_mtime >= since:
                found.append((st.st_mtime, os.path.relpath(p, root)))
    return [rel for _, rel in sorted(found)[-limit:]]


def report_outbox_images(task, ctl, since: float) -> None:
    """Tell the panel which images the agent put in ~/outbox during this task, so the chat can show them."""
    try:
        imgs = as_agent("recent_images", home_dir("outbox"), since, timeout=30)
    except RuntimeError:
        return
    if imgs:
        ctl.send(type="outbox_images", task=task["id"], files=imgs)


SUMMARY_FILE = "status.txt"  # in the agent's home: one line on where its work stands, shown in the panel
SUMMARY_EVERY = 30            # seconds between reads while a task runs
SUMMARY_SENT = {"text": None}
SUMMARY_LOCK = threading.Lock()


def _h_read_summary(path: str) -> str | None:
    """(as the agent) The first line of ~/status.txt, when it's a plain file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO can't hold the read up
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if os.fstat(f.fileno()).st_mode & 0o170000 != 0o100000:
            return None
        lines = f.read(1024).decode(errors="replace").strip().splitlines()
    return lines[0].strip()[:200] if lines else None


def send_summary(ctl) -> None:
    """The agent's summary to the panel when it changed (older panels ignore the message)."""
    try:
        text = as_agent("read_summary", home_dir(SUMMARY_FILE), timeout=30)
    except RuntimeError:
        return
    with SUMMARY_LOCK:
        if not text or text == SUMMARY_SENT["text"]:
            return
        SUMMARY_SENT["text"] = text
        ctl.send(type="summary", text=text)


def watch_summary(ctl, done: threading.Event) -> None:
    while not done.wait(SUMMARY_EVERY):
        try:
            send_summary(ctl)
        except Exception as e:  # noqa: BLE001 - one bad read never ends the watch
            log(f"!! reading the summary: {e.__class__.__name__}: {e}")


def list_files(boot, msg, ctl) -> None:
    rid = str(msg.get("id", ""))
    if not rid.isalnum() or len(rid) > 16:
        return
    root = _file_root(boot, msg.get("what"))
    if not root or not os.path.isdir(root) or os.path.islink(root):
        ctl.send(type="files_list", id=rid, error="that folder doesn't exist in the VM yet")
        return
    try:
        r = as_agent("list_dir", root, str(msg.get("path") or "")[:4096], timeout=30)
    except RuntimeError as e:
        r = {"error": str(e)}
    ctl.send(type="files_list", id=rid, **r)


def read_file(boot, msg, ctl) -> None:
    """Send one file to the panel, to look at or download (it never lands on the host)."""
    rid = str(msg.get("id", ""))
    if not rid.isalnum() or len(rid) > 16:
        return
    root = _file_root(boot, msg.get("what"))
    if not root or not os.path.isdir(root) or os.path.islink(root):
        ctl.send(type="file_failed", id=rid, error="that folder doesn't exist in the VM yet")
        return
    limit = max(0, min(int(msg.get("max_bytes") or 0), 64 << 20))
    p = agent_popen("read_file", root, str(msg.get("path") or "")[:4096], limit)
    h, total, seq = hashlib.sha256(), 0, 0
    while True:
        chunk = p.stdout.read(EXPORT_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            p.kill()
            break
        h.update(chunk)
        ctl.send(type="file_chunk", id=rid, seq=seq, data=base64.b64encode(chunk).decode())
        seq += 1
    try:
        agent_finish(p, "read_file")
    except RuntimeError:
        ctl.send(type="file_failed", id=rid, error=f"can't read it (a link, not a regular file, or larger than {limit} bytes)")
        return
    if total > limit:
        ctl.send(type="file_failed", id=rid, error=f"larger than {limit} bytes")
        return
    ctl.send(type="file_end", id=rid, chunks=seq, bytes=total, sha256=h.hexdigest())


def export_files(boot, msg, ctl) -> None:
    """Stream one folder to the panel because you pressed Export.  The panel
    decides where (if anywhere) the files land on the host."""
    eid = str(msg.get("id", ""))
    if not eid.isalnum() or len(eid) > 16:
        return
    what = msg.get("what")
    if what == "repo" and boot.get("repo"):
        src = repo_dir_for(boot)
    elif what in EXPORT_SOURCES:
        src = home_dir(EXPORT_SOURCES[what])
    else:
        ctl.send(type="export_failed", id=eid, error="unknown export source")
        return
    if not os.path.isdir(src) or os.path.islink(src):
        ctl.send(type="export_failed", id=eid, error=f"{src} does not exist in the VM")
        return
    limit = int(msg.get("max_bytes") or 0)
    p = agent_popen("pack_dir", src, True)
    h, total, seq = hashlib.sha256(), 0, 0
    try:
        while True:
            chunk = p.stdout.read(EXPORT_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if limit and total > limit:
                p.kill()
                ctl.send(type="export_failed", id=eid, error=f"more than {limit} bytes")
                return
            h.update(chunk)
            ctl.send(type="export_chunk", id=eid, seq=seq, data=base64.b64encode(chunk).decode())
            seq += 1
    finally:
        try:
            agent_finish(p, "pack_dir")
            failed = None
        except RuntimeError as e:
            failed = str(e)
    if failed:
        ctl.send(type="export_failed", id=eid, error=f"packing failed: {failed}")
        return
    ctl.send(type="export_end", id=eid, chunks=seq, bytes=total, sha256=h.hexdigest())
    log(f"export: sent {src} ({total} bytes)")


def run_all(funcs: list) -> list:
    """Run the functions at the same time and return their results in order.  One that fails gives
    "error (...)", which no check accepts as a pass."""
    import concurrent.futures

    def safe(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            return f"error ({type(e).__name__})"
    if not funcs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(funcs)) as ex:
        return list(ex.map(safe, funcs))


def selftest(profile: str = "coding", host_ports: list = ()) -> dict:
    """Network isolation self-test, run as the agent user at every boot."""
    try:
        return as_agent("selftest", profile, [int(x) for x in host_ports][:5], timeout=120)
    except (RuntimeError, ValueError) as e:
        log(f"selftest FAIL: could not run: {e}")
        return {"self-test ran": False}


def _no_new_privs() -> str:
    try:
        with open("/proc/self/status") as f:
            return "set" if any(ln.split() == ["NoNewPrivs:", "1"] for ln in f) else "not set"
    except OSError:
        return "unknown"


def _setuid_programs(roots=("/usr", "/bin", "/sbin", "/opt", "/etc")) -> str:
    """Setuid/setgid files the agent could execute (the image build strips them)."""
    import stat
    found, stack = [], [r for r in roots if os.path.isdir(r) and not os.path.islink(r)]
    while stack and len(found) < 5:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(e.path)
            elif stat.S_ISREG(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID) and os.access(e.path, os.X_OK):
                found.append(e.path)
    return "none" if not found else " ".join(found)


def _h_selftest(profile: str, host_ports: list) -> dict:
    """(as the agent) How to read the results: QEMU's user networking with
    restrict=on answers every guest connection it won't forward with a TCP
    reset, so "refused" on 10.0.2.x comes from QEMU itself, not your host.
    The real proof is the panel's own port: it IS listening on the host, so
    reaching it through 10.0.2.2 would mean restrict=on is not in effect."""
    import errno
    import socket

    def tcp(host, port, family=socket.AF_INET):
        s = None
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((host, port))
            return "connected"
        except socket.timeout:
            return "no answer"
        except OSError as e:
            return {errno.ECONNREFUSED: "reset", errno.ENETUNREACH: "no route",
                    errno.EHOSTUNREACH: "no route", errno.EAFNOSUPPORT: "no IPv6",
                    errno.EADDRNOTAVAIL: "no IPv6 address"}.get(e.errno, f"error {e.errno}")
        finally:
            if s:
                s.close()

    def udp_dns(server):
        q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        try:
            s.sendto(q, (server, 53))
            s.recv(512)
            return "answered"
        except OSError:
            return "no answer"
        finally:
            s.close()

    def dns(name):
        try:
            socket.getaddrinfo(name, 443)
            return "resolved"
        except OSError:
            return "blocked"

    def via_proxy(target):
        try:
            s = socket.create_connection(("10.0.2.100", 3128), timeout=10)
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            buf = b""
            while b"\r\n" not in buf and len(buf) < 200:
                chunk = s.recv(200)
                if not chunk:
                    break
                buf += chunk
            s.close()
            m = re.match(rb"HTTP/1\.[01] (\d{3})", buf)
            return f"status {m.group(1).decode()}" if m else f"bad reply {buf[:40]!r}"
        except OSError as e:
            return f"error ({e})"

    def nested_virt():
        try:
            with open("/proc/cpuinfo") as f:
                flags = {w for ln in f if ln.startswith("flags") for w in ln.split(":", 1)[1].split()}
            return "present" if flags & {"vmx", "svm"} else "absent"
        except OSError:
            return "unknown"

    # Each check is (name, how to run it, what passes).  They run all at once: several pass by waiting
    # for a timeout ("no answer"), and one after another those waits added up to seconds at every boot.
    blocked = lambda r: r != "connected"  # noqa: E731
    checks = [
        ("direct TCP to 1.1.1.1:443 is blocked", lambda: tcp("1.1.1.1", 443), blocked),
        ("direct IPv6 to [2606:4700:4700::1111]:443 is blocked",
         lambda: tcp("2606:4700:4700::1111", 443, socket.AF_INET6), blocked),
        ("host via 10.0.2.2:22 is blocked", lambda: tcp("10.0.2.2", 22), blocked),
    ]
    for port in host_ports:
        checks.append((f"panel port {port} on the host (listening) is unreachable via 10.0.2.2",
                       lambda port=port: tcp("10.0.2.2", port), blocked))
        checks.append((f"proxy address 10.0.2.100:{port} only forwards 3128", lambda port=port: tcp("10.0.2.100", port), blocked))
    checks += [
        ("DNS lookup of example.com is blocked", lambda: dns("example.com"), lambda r: r == "blocked"),
        ("UDP DNS to 10.0.2.3 gets no answer", lambda: udp_dns("10.0.2.3"), lambda r: r == "no answer"),
        ("UDP DNS to 1.1.1.1 gets no answer", lambda: udp_dns("1.1.1.1"), lambda r: r == "no answer"),
        ("proxy allows api.github.com:443", lambda: via_proxy("api.github.com:443"), lambda r: r == "status 200"),
        # a reserved name (RFC 2606) nobody would allow, so allowing example.com for an agent can't fail the check
        ("proxy denies a host that isn't allowed (selftest.invalid:443)", lambda: via_proxy("selftest.invalid:443"),
         lambda r: r == "status 403")
        if profile != "research" else
        ("proxy allows the open web (example.com:443)", lambda: via_proxy("example.com:443"), lambda r: r == "status 200"),
        ("proxy denies IP literal 10.0.2.2:443", lambda: via_proxy("10.0.2.2:443"), lambda r: r == "status 403"),
        ("proxy denies port 22 (api.github.com:22)", lambda: via_proxy("api.github.com:22"), lambda r: r == "status 403"),
        ("no nested virtualization (VMX/SVM) in the VM", nested_virt, lambda r: r == "absent"),
        ("agent can't gain privileges (no_new_privs)", _no_new_privs, lambda r: r == "set"),
        ("no setuid/setgid programs the agent can run", _setuid_programs, lambda r: r == "none"),
    ]
    results = {}
    for (name, _, ok), got in zip(checks, run_all([run for _, run, _ in checks])):
        passed = ok(got)
        log(f"selftest {'PASS' if passed else 'FAIL'}: {name} -> {got}")
        results[name] = passed
    return results


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def shutdown(boot, task_thread=None) -> None:
    """Stop: end the running task and let it finish up (save the conversation, commit and push),
    then export the memory with nothing else touching the agent's processes, then power off."""
    STOPPING.set()  # before ending the run, so one that starts a moment later sees it
    if task_thread is not None and task_thread.is_alive():
        log("stopping the running task first")
        stop_current_run()
        task_thread.join(TASK_STOP_WAIT)
        if task_thread.is_alive():
            log(f"!! the task didn't finish within {TASK_STOP_WAIT}s; shutting down anyway")
    if boot and boot.get("export_memory"):
        with AGENT_LOCK:
            try:
                kill_agent_processes()
                export_memory()
            except Exception as e:  # noqa: BLE001
                log(f"!! memory export failed: {e}")
    log("powering off")
    subprocess.run(["systemctl", "poweroff"], check=False)


def set_no_new_privs() -> None:
    """Nothing started from here can gain privileges on exec, ever: setuid
    programs (sudo, su, pkexec, ...) and file capabilities stop working for
    the agent and everything it runs.  The runner is already root and never
    needs them; inherited by every child, and it can't be turned off again."""
    import ctypes
    PR_SET_NO_NEW_PRIVS, PR_GET_NO_NEW_PRIVS = 38, 39
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise RuntimeError(f"could not set no_new_privs (errno {ctypes.get_errno()}); refusing to run agents")


def maybe_reexec_newer_runner() -> None:
    """The panel puts its current runner on the boot disk; run that one, so
    runner updates don't need a base image rebuild.  The boot disk is the
    host's read-only disk, not something the agent can write."""
    if os.environ.get("CILLY_RUNNER_REEXEC") or not os.path.exists(TASK_DISK):
        return
    try:
        with tarfile.open(TASK_DISK, mode="r:") as tf:
            m = next((x for x in tf if x.name == "runner.py" and x.isfile()), None)
            if m is None:
                return
            code = tf.extractfile(m).read()
    except (OSError, tarfile.TarError):
        return
    with open(__file__, "rb") as f:
        if f.read() == code:
            return
    os.makedirs("/run/cilly", mode=0o700, exist_ok=True)
    path = "/run/cilly/runner.py"
    with open(path, "wb") as f:
        f.write(code)
    os.chmod(path, 0o700)
    os.environ["CILLY_RUNNER_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable, "-I", path])


def main() -> int:
    global _log_fh
    try:
        _log_fh = open(LOG_PORT, "wb", buffering=0)
    except OSError:
        pass
    boot, tarinfo = read_boot()
    if not boot:
        log("no boot disk; idle VM")
        return 0
    log(f"agent {boot.get('name')} ({boot['agent_id']}, mode {boot['mode']}): repo={boot.get('repo')}")
    ctl = Control(CONTROL_PORT)
    hello = None
    while hello is None or hello.get("type") != "hello":
        hello = ctl.recv()
    if boot.get("move") in ("export", "import"):
        move_home(boot["move"], ctl)
        return 0
    ctl.send(type="phase", phase="policy")  # boot progress for the panel, in fixed names only
    apply_policy(hello.get("policy") or {})
    grow_root(boot)
    install_ca(boot)
    write_gitconfig(boot)
    setup_inject(boot, tarinfo)
    # Root writes into ~agent/.claude here, which is safe only because memory
    # mode always boots a fresh disk and no agent process has run yet.
    if boot.get("restore_memory") and boot.get("mode") == "memory":
        ctl.send(type="phase", phase="memory")
        restore_memory(tarinfo)
    inj = boot.get("inject") or {}
    if inj.get("to_scratch") and not boot.get("repo") and boot.get("profile") != "research" and os.path.isdir(INJECT):
        r = as_agent("copy_inject", INJECT, home_dir("scratch"))
        log(f"inject: copied {r['copied']} entries into ~/scratch to work on ({r['kept']} already there, kept)")
    MEMORY_LIMIT["checking"] = True
    threading.Thread(target=check_memory_limit, daemon=True).start()  # alongside the self-test
    ctl.send(type="phase", phase="selftest")
    results = selftest(boot.get("profile", "coding"), boot.get("selftest_host_ports") or [])
    ctl.send(type="ready", selftest=results, deliver=True)  # takes "deliver" (an older panel ignores this)

    attachments = Attachments()
    current = {"thread": None}
    stopping = threading.Event()

    while True:
        msg = ctl.recv()
        if not isinstance(msg, dict):
            continue
        typ = msg.get("type")
        if typ == "policy":
            apply_policy(msg.get("policy") or {})
            ctl.send(type="policy_applied")
        elif typ in ("outbox_ok", "outbox_refused"):
            outbox_answer(msg)
        elif typ == "inbox":
            write_inbox(msg)
            ctl.send(type="inbox_ok", id=msg.get("id"))
        elif typ == "attach_chunk":
            attachments.chunk(msg)
        elif typ == "attach_end":
            attachments.end(msg)
        elif typ == "project_sync":
            sync_project_files(msg.get("names"))
        elif typ == "skills_sync":
            sync_project_files(msg.get("names"), "skills")
        elif typ == "export":
            threading.Thread(target=export_files, args=(boot, msg, ctl), daemon=True).start()
        elif typ == "list_files":
            threading.Thread(target=list_files, args=(boot, msg, ctl), daemon=True).start()
        elif typ == "read_file":
            threading.Thread(target=read_file, args=(boot, msg, ctl), daemon=True).start()
        elif typ == "task":
            if stopping.is_set():
                ctl.send(type="status", task=msg.get("id"), state="failed", error="the agent is stopping")
                continue
            current["thread"] = start_task(boot, msg, ctl) or current["thread"]
        elif typ == "interrupt":
            interrupt_task(msg, ctl)
        elif typ == "deliver":
            deliver_message(msg, ctl)
        elif typ == "shutdown" and not stopping.is_set():
            # on its own thread, because the task finishing up may still need answers from the panel
            stopping.set()
            threading.Thread(target=shutdown, args=(boot, current["thread"])).start()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == HELPER_FLAG:
        sys.exit(_helper_main())
    maybe_reexec_newer_runner()
    try:
        set_no_new_privs()
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log(f"!! runner failed: {e.__class__.__name__}: {e}")
        sys.exit(1)
