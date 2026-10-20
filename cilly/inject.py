"""Scan a host folder and package it for one-way, read-only copy into a VM.

Security properties:

* The walk never follows symlinks on the host.  Every open is relative to an
  already-open directory fd with O_NOFOLLOW, so swapping a directory for a
  symlink mid-walk cannot redirect us outside the selected folder.
* Symlinks whose target resolves inside the folder are stored as *relative*
  links (they then resolve inside /inject in the guest).  Anything else is
  excluded and reported.
* Mount points (different st_dev), FIFOs, sockets and device nodes are skipped.
* Regular files with st_nlink > 1 are flagged: a hardlink may be the same
  inode as a sensitive file elsewhere and cannot be told apart.
* The tar builder only packs entries from the manifest the user approved and
  aborts if any file's (dev, ino, size, mtime) changed since the scan.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import stat
import tarfile
from dataclasses import dataclass, field, asdict

# Hard limits so a huge or adversarial folder can't exhaust memory or disk.
MAX_ENTRIES = 200_000
MAX_DEPTH = 128
CONTENT_SCAN_FILE_MAX = 512 * 1024          # only scan small text files
CONTENT_SCAN_TOTAL_MAX = 64 * 1024 * 1024   # stop content scanning after this much

_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# ---------------------------------------------------------------------------
# Secret heuristics
# ---------------------------------------------------------------------------

_SECRET_NAME_PATTERNS = [
    (re.compile(r"^\.env(\..*)?$"), "dotenv file"),
    (re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_sk)?$"), "SSH private key"),
    (re.compile(r".*\.(pem|key|p12|pfx|jks|keystore|kdbx|ovpn|tfstate|tfstate\.backup)$", re.I), "key/credential file"),
    (re.compile(r"^(\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.htpasswd|\.pgpass)$"), "credential file"),
    (re.compile(r"^credentials(\..*)?$", re.I), "credentials file"),
    (re.compile(r"^(secrets?|service[-_]account)(\..*)?\.(json|ya?ml|toml)$", re.I), "secrets file"),
    (re.compile(r"^kubeconfig$|^.*\.kubeconfig$", re.I), "kubeconfig"),
]
_SECRET_DIR_NAMES = {
    ".ssh": "SSH directory",
    ".gnupg": "GnuPG directory",
    ".aws": "AWS credentials directory",
    ".kube": "Kubernetes config directory",
    ".docker": "Docker config directory",
    ".password-store": "pass password store",
}
_SECRET_CONTENT_PATTERNS = [
    (re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub fine-grained token"),
    (re.compile(rb"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(rb"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(rb"\bsk-(proj-)?[A-Za-z0-9_\-]{32,}"), "OpenAI-style API key"),
    (re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
    (re.compile(rb"\bglpat-[A-Za-z0-9_\-]{20,}"), "GitLab token"),
]
_GIT_CONFIG_CRED = re.compile(
    rb"(://[^/\s:@]+:[^/\s@]+@)|(^\s*\[credential)|(extraheader\s*=)", re.M | re.I
)


def secret_name_reason(name: str) -> str | None:
    for pat, why in _SECRET_NAME_PATTERNS:
        if pat.match(name):
            return why
    return None


def secret_content_reasons(data: bytes) -> list[str]:
    return sorted({why for pat, why in _SECRET_CONTENT_PATTERNS if pat.search(data)})


# ---------------------------------------------------------------------------
# Folders that may never be injected
# ---------------------------------------------------------------------------

_BROAD_PATHS = ["/", "/home", "/root", "/etc", "/usr", "/var", "/boot", "/proc",
                "/sys", "/dev", "/run", "/tmp", "/opt", "/srv", "/mnt", "/media"]
_HOME_SENSITIVE = [".ssh", ".gnupg", ".config", ".local", ".aws", ".kube",
                   ".docker", ".password-store", ".mozilla", ".pki", ".cache",
                   ".claude"]  # your Claude Code login and conversations


class InjectError(Exception):
    pass


def _sensitive_paths(home: str) -> list[str]:
    out = []
    for s in _HOME_SENSITIVE:
        p = os.path.join(home, s)
        out += [p, os.path.realpath(p)]  # also catch stow-style symlinked dotfiles
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
                "GNUPGHOME", "PASSWORD_STORE_DIR"):
        v = os.environ.get(var)
        if v and os.path.isabs(v):
            out += [os.path.normpath(v), os.path.realpath(v)]
    out += ["/run/user"]
    return list(dict.fromkeys(out))


def check_allowed_root(path: str, home: str | None = None) -> str:
    """Return the resolved folder path, or raise InjectError if too broad."""
    if not path or not os.path.isabs(path):
        raise InjectError("folder must be an absolute path")
    real = os.path.realpath(path)
    home = os.path.realpath(home or os.path.expanduser("~"))
    if real in _BROAD_PATHS:
        raise InjectError(f"refusing broad system path {real}")
    if real == home or home.startswith(real.rstrip("/") + "/"):
        raise InjectError("refusing your home directory or one of its parents")
    for sp in _sensitive_paths(home):
        if real == sp or real.startswith(sp.rstrip("/") + "/") or sp.startswith(real.rstrip("/") + "/"):
            raise InjectError(f"refusing sensitive location {sp}")
    return real


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    rel: str            # POSIX relative path, "" never used (root is implicit)
    kind: str           # "file" | "dir" | "symlink"
    size: int = 0
    mode: int = 0
    mtime_ns: int = 0
    dev: int = 0
    ino: int = 0
    nlink: int = 1
    link_raw: str = ""      # original readlink() text (symlinks)
    link_target: str = ""   # rewritten relative target (symlinks)


@dataclass
class Issue:
    rel: str
    code: str
    detail: str
    severity: str  # "warn" (must be acknowledged) | "info" (excluded/skipped, shown)


@dataclass
class Manifest:
    id: str
    root: str
    root_dev: int
    root_ino: int
    entries: list[Entry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    total_bytes: int = 0
    file_count: int = 0
    content_scanned_files: int = 0
    content_scan_truncated: bool = False

    @property
    def needs_ack(self) -> bool:
        return any(i.severity == "warn" for i in self.issues)

    def summary(self, max_entries: int = 2000) -> dict:
        return {
            "id": self.id,
            "root": self.root,
            "file_count": self.file_count,
            "dir_count": sum(1 for e in self.entries if e.kind == "dir"),
            "symlink_count": sum(1 for e in self.entries if e.kind == "symlink"),
            "total_bytes": self.total_bytes,
            "entries": [
                {"rel": e.rel, "kind": e.kind, "size": e.size,
                 **({"target": e.link_target} if e.kind == "symlink" else {})}
                for e in self.entries[:max_entries]
            ],
            "entries_truncated": len(self.entries) > max_entries,
            "issues": [asdict(i) for i in self.issues],
            "needs_ack": self.needs_ack,
            "content_scanned_files": self.content_scanned_files,
            "content_scan_truncated": self.content_scan_truncated,
        }


def _join(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def _resolve_symlink(root: str, parent_rel: str, raw: str) -> str | None:
    """Return a relative link target if `raw` resolves inside root, else None."""
    base = os.path.join(root, parent_rel) if parent_rel else root
    candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
    real = os.path.realpath(candidate)
    if real != root and not real.startswith(root + "/"):
        return None
    if not os.path.lexists(real):
        return None
    return os.path.relpath(real, base)


def scan(path: str, *, max_bytes: int = 4 * 1024**3, home: str | None = None) -> Manifest:
    root = check_allowed_root(path, home)
    try:
        root_fd = os.open(root, _O_DIR)
    except OSError as e:
        raise InjectError(f"cannot open folder: {e.strerror}") from None
    try:
        st = os.fstat(root_fd)
        m = Manifest(id=secrets.token_hex(8), root=root, root_dev=st.st_dev, root_ino=st.st_ino)
        budget = {"content": 0}
        _walk(m, root_fd, "", 0, budget, max_bytes)
    finally:
        os.close(root_fd)
    return m


def _walk(m: Manifest, dfd: int, prefix: str, depth: int, budget: dict, max_bytes: int) -> None:
    if depth > MAX_DEPTH:
        m.issues.append(Issue(prefix, "too_deep", f"deeper than {MAX_DEPTH} levels, skipped", "info"))
        return
    try:
        with os.scandir(dfd) as it:
            names = sorted(e.name for e in it)
    except OSError as e:
        m.issues.append(Issue(prefix or ".", "unreadable", e.strerror or str(e), "info"))
        return

    for name in names:
        rel = _join(prefix, name)
        if len(m.entries) >= MAX_ENTRIES:
            raise InjectError(f"folder has more than {MAX_ENTRIES} entries")
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError as e:
            m.issues.append(Issue(rel, "unreadable", e.strerror or str(e), "info"))
            continue
        mode = st.st_mode

        if stat.S_ISLNK(mode):
            raw = os.readlink(name, dir_fd=dfd)
            target = _resolve_symlink(m.root, prefix, raw)
            if target is None:
                m.issues.append(Issue(rel, "symlink_outside",
                                      f"symlink -> {raw!s} resolves outside the folder or is dangling; excluded",
                                      "warn"))
                continue
            m.entries.append(Entry(rel, "symlink", 0, 0o777, st.st_mtime_ns, st.st_dev,
                                   st.st_ino, st.st_nlink, raw, target))
            continue

        if stat.S_ISDIR(mode):
            if st.st_dev != m.root_dev:
                m.issues.append(Issue(rel, "mountpoint", "different filesystem (mount point); skipped", "info"))
                continue
            if name in _SECRET_DIR_NAMES:
                m.issues.append(Issue(rel, "secret_dir", _SECRET_DIR_NAMES[name], "warn"))
            if name == ".git":
                _check_git_dir(m, dfd, rel)
            try:
                cfd = os.open(name, _O_DIR, dir_fd=dfd)
            except OSError as e:
                m.issues.append(Issue(rel, "unreadable", e.strerror or str(e), "info"))
                continue
            try:
                cst = os.fstat(cfd)
                if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                    raise InjectError(f"{rel} changed during scan")
                m.entries.append(Entry(rel, "dir", 0, mode & 0o7777, st.st_mtime_ns, st.st_dev, st.st_ino))
                _walk(m, cfd, rel, depth + 1, budget, max_bytes)
            finally:
                os.close(cfd)
            continue

        if not stat.S_ISREG(mode):
            m.issues.append(Issue(rel, "special_file", "FIFO/socket/device; skipped", "info"))
            continue

        if st.st_dev != m.root_dev:
            m.issues.append(Issue(rel, "mountpoint", "file on a different filesystem; skipped", "info"))
            continue

        if name == ".git":  # worktree/submodule pointer file
            m.issues.append(Issue(rel, "git_dir", ".git file (worktree/submodule pointer)", "warn"))
        why = secret_name_reason(name)
        if why:
            m.issues.append(Issue(rel, "secret_name", why, "warn"))
        if st.st_nlink > 1:
            m.issues.append(Issue(rel, "hardlink",
                                  f"file has {st.st_nlink} hard links; it may be the same file as one outside the folder",
                                  "warn"))
        _content_scan(m, dfd, name, rel, st, budget)

        m.entries.append(Entry(rel, "file", st.st_size, mode & 0o7777, st.st_mtime_ns, st.st_dev,
                               st.st_ino, st.st_nlink))
        m.file_count += 1
        m.total_bytes += st.st_size
        if m.total_bytes > max_bytes:
            raise InjectError(f"folder is larger than the configured limit ({max_bytes} bytes)")


def _content_scan(m: Manifest, dfd: int, name: str, rel: str, st: os.stat_result, budget: dict) -> None:
    if st.st_size > CONTENT_SCAN_FILE_MAX or st.st_size == 0:
        return
    if budget["content"] + st.st_size > CONTENT_SCAN_TOTAL_MAX:
        m.content_scan_truncated = True
        return
    try:
        fd = os.open(name, _O_FILE, dir_fd=dfd)
    except OSError:
        return
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_ino != st.st_ino:
            return
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(CONTENT_SCAN_FILE_MAX)
    finally:
        os.close(fd)
    budget["content"] += len(data)
    if b"\x00" in data[:8192]:
        return  # binary
    m.content_scanned_files += 1
    for why in secret_content_reasons(data):
        m.issues.append(Issue(rel, "secret_content", f"contains what looks like a {why}", "warn"))


def _check_git_dir(m: Manifest, dfd: int, rel: str) -> None:
    m.issues.append(Issue(rel, "git_dir",
                          ".git directory: full history is copied and may contain secrets", "warn"))
    try:
        gfd = os.open(".git", _O_DIR, dir_fd=dfd)
    except OSError:
        return
    try:
        try:
            cfd = os.open("config", _O_FILE, dir_fd=gfd)
        except OSError:
            return
        try:
            if not stat.S_ISREG(os.fstat(cfd).st_mode):
                return
            data = os.read(cfd, 256 * 1024)
        finally:
            os.close(cfd)
        if _GIT_CONFIG_CRED.search(data):
            m.issues.append(Issue(rel + "/config", "git_credentials",
                                  "git config contains embedded credentials or a credential section", "warn"))
    finally:
        os.close(gfd)


# ---------------------------------------------------------------------------
# Task disk (tar stream on a read-only raw virtio disk)
# ---------------------------------------------------------------------------

def _tarinfo(name: str, *, kind: str, size: int = 0, mode: int = 0o644, mtime_ns: int = 0,
             link: str = "") -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = mtime_ns // 1_000_000_000
    if kind == "dir":
        ti.type, ti.mode = tarfile.DIRTYPE, 0o755
    elif kind == "symlink":
        ti.type, ti.mode, ti.linkname = tarfile.SYMTYPE, 0o777, link
    else:
        ti.type, ti.size = tarfile.REGTYPE, size
        ti.mode = 0o755 if mode & 0o111 else 0o644
    return ti


def build_task_disk(out_path: str, task: dict, manifest: Manifest | None, extra=None) -> int:
    """Write task.json (+ inject/ tree from `manifest`) as a tar file.
    `extra(tar)` may add more members (e.g. an agent's stored memory).

    Returns the size in bytes.  The file is created with O_EXCL and 0600.
    """
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as f:
        with tarfile.open(fileobj=f, mode="w", format=tarfile.PAX_FORMAT) as tar:
            data = json.dumps(task, indent=2).encode()
            tar.addfile(_tarinfo("task.json", kind="file", size=len(data)), io.BytesIO(data))
            if manifest is not None:
                tar.addfile(_tarinfo("inject", kind="dir"))
                _pack(tar, manifest)
            if extra is not None:
                extra(tar)
        size = f.tell()
    # Raw disks must be a multiple of 512 bytes; tar records already are.
    assert size % 512 == 0
    return size


def _pack(tar: tarfile.TarFile, m: Manifest) -> None:
    root_fd = os.open(m.root, _O_DIR)
    dir_fds: dict[str, int] = {"": root_fd}
    open_stack: list[str] = []  # currently open directories, outermost first
    try:
        st = os.fstat(root_fd)
        if (st.st_dev, st.st_ino) != (m.root_dev, m.root_ino):
            raise InjectError("folder was replaced since the scan")
        for e in m.entries:
            # Entries are in pre-order; close directories we have left so we
            # never hold more fds than the tree is deep.
            while open_stack and not e.rel.startswith(open_stack[-1] + "/"):
                os.close(dir_fds.pop(open_stack.pop()))
            parent, _, name = e.rel.rpartition("/")
            pfd = dir_fds.get(parent)
            if pfd is None:
                raise InjectError(f"parent of {e.rel} missing from manifest")
            arc = "inject/" + e.rel
            if e.kind == "dir":
                cfd = os.open(name, _O_DIR, dir_fd=pfd)
                cst = os.fstat(cfd)
                if (cst.st_dev, cst.st_ino) != (e.dev, e.ino):
                    os.close(cfd)
                    raise InjectError(f"{e.rel} changed since the scan")
                dir_fds[e.rel] = cfd
                open_stack.append(e.rel)
                tar.addfile(_tarinfo(arc, kind="dir", mtime_ns=e.mtime_ns))
            elif e.kind == "symlink":
                raw = os.readlink(name, dir_fd=pfd)
                if raw != e.link_raw:
                    raise InjectError(f"{e.rel} changed since the scan")
                tar.addfile(_tarinfo(arc, kind="symlink", mtime_ns=e.mtime_ns, link=e.link_target))
            else:
                ffd = os.open(name, _O_FILE, dir_fd=pfd)
                with os.fdopen(ffd, "rb") as f:
                    fst = os.fstat(ffd)
                    if (not stat.S_ISREG(fst.st_mode)
                            or (fst.st_dev, fst.st_ino, fst.st_size, fst.st_mtime_ns)
                            != (e.dev, e.ino, e.size, e.mtime_ns)):
                        raise InjectError(f"{e.rel} changed since the scan")
                    tar.addfile(_tarinfo(arc, kind="file", size=e.size, mode=e.mode,
                                         mtime_ns=e.mtime_ns), f)
    finally:
        for fd in dir_fds.values():
            os.close(fd)
