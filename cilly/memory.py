"""Agent memory for "ephemeral + memory" agents.

When such an agent stops, the guest runner writes a tar of selected parts of
the agent's ~/.claude (conversation transcripts, memory notes, CLAUDE.md) onto
a small raw "memout" disk.  After QEMU has exited, the host imports it here.

Like CI artifacts and the Export button (exports.py), the import is strict:
* only regular files and directories, no links/devices,
* only paths under an allowlist of top-level names,
* no absolute paths, no "..", no odd characters,
* per-file, total-size and file-count limits,
* no git repositories (exports.git_left_out): they're left out,
* written with O_EXCL|O_NOFOLLOW relative to directory fds into a fresh
  directory, then swapped in atomically (previous copy kept as memory.prev).
The host never executes or interprets the content; it is only re-injected into
the next VM of the same agent.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile

STATE_FILE = "cilly-state.json"  # the runner's conversation state
OLD_STATE_FILE = "vmpanel-state.json"  # its name before the rename, in memory saved before it
ALLOWED_TOP = {"CLAUDE.md", "projects", STATE_FILE}
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 400 * 1024 * 1024
MAX_FILES = 5000
MAX_ENTRIES = 10000  # files and folders together, also folders only implied by a path
# A path's length and depth, so the next start can always pack it again (the guest skips longer ones)
MAX_PATH = 1024
MAX_DEPTH = 32
MEMOUT_SIZE = 512 * 1024 * 1024   # sparse raw disk the guest writes the tar to

_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SAFE = re.compile(r"^[A-Za-z0-9._@+=,-][A-Za-z0-9._@+=, -]{0,254}$")


class MemoryError_(Exception):
    pass


def create_memout(path: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        os.ftruncate(fd, MEMOUT_SIZE)
    finally:
        os.close(fd)


def _check_name(name: str) -> list[str]:
    if name.startswith("/") or "\\" in name or "\x00" in name:
        raise MemoryError_(f"unsafe path {name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or parts[0] not in ALLOWED_TOP:
        raise MemoryError_(f"path outside the memory allowlist: {name!r}")
    if len("/".join(parts).encode()) > MAX_PATH or len(parts) > MAX_DEPTH:
        raise MemoryError_(f"path longer than {MAX_PATH} bytes or deeper than {MAX_DEPTH} folders: {name[:80]!r}")
    for p in parts:
        if p == ".." or not _SAFE.fullmatch(p):
            raise MemoryError_(f"unsafe path component in {name!r}")
    return parts


def import_memout(img_path: str, agent_dir: str) -> dict:
    """Validate and import the guest's memory tar.  Returns a summary.
    Raises MemoryError_ (and leaves the current memory untouched) on any
    violation.  An all-zero disk (nothing exported) returns {"imported": False}."""
    with open(img_path, "rb") as f:
        head = f.read(512)
    if not head.strip(b"\x00"):
        return {"imported": False, "reason": "nothing exported"}
    new = os.path.join(agent_dir, "memory.new")
    shutil.rmtree(new, ignore_errors=True)
    os.mkdir(new, 0o700)
    root_fd = os.open(new, _O_DIR)
    files = total = 0
    from .exports import add_prefixes, git_left_out, left_out_by, tar_head
    try:
        try:
            tf = tarfile.open(img_path, mode="r:")
        except tarfile.TarError as e:
            raise MemoryError_(f"not a tar stream: {e}") from None
        with tf:
            members, seen = [], set()
            for m in tf:
                parts = _check_name(m.name)
                add_prefixes(seen, parts)
                if len(seen) > MAX_ENTRIES:
                    raise MemoryError_("memory export exceeds size/count limits")
                members.append((m, parts))
            left = git_left_out((parts, tar_head(tf, m, parts)) for m, parts in members)
            used = set()
            for m, parts in members:
                if (skip := left_out_by(parts, left)) is not None:
                    used.add(skip)
                    continue
                if m.isdir():
                    _open_dirs(root_fd, parts, create=True, close=True)
                    continue
                if not m.isreg():
                    raise MemoryError_(f"non-regular entry {m.name!r}")
                files += 1
                total += m.size
                if m.size > MAX_FILE or total > MAX_TOTAL or files > MAX_FILES:
                    raise MemoryError_("memory export exceeds size/count limits")
                pfd = _open_dirs(root_fd, parts[:-1], create=True, close=False)
                try:
                    ffd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600, dir_fd=pfd)
                finally:
                    os.close(pfd)
                src = tf.extractfile(m)
                written = 0
                with os.fdopen(ffd, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > m.size:
                            raise MemoryError_("entry larger than declared")
                        out.write(chunk)
    except (tarfile.TarError, EOFError) as e:  # a header that runs past the end of the disk
        os.close(root_fd)
        shutil.rmtree(new, ignore_errors=True)
        raise MemoryError_(f"broken tar stream: {e}") from None
    except BaseException:
        os.close(root_fd)
        shutil.rmtree(new, ignore_errors=True)
        raise
    os.close(root_fd)
    cur, prev = os.path.join(agent_dir, "memory"), os.path.join(agent_dir, "memory.prev")
    shutil.rmtree(prev, ignore_errors=True)
    if os.path.exists(cur):
        os.rename(cur, prev)
    os.rename(new, cur)
    return {"imported": True, "files": files, "bytes": total, **({"left_out": len(used)} if used else {})}


def _open_dirs(root_fd: int, parts: list[str], *, create: bool, close: bool) -> int:
    fd = os.dup(root_fd)
    try:
        for p in parts:
            if create:
                try:
                    os.mkdir(p, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nfd = os.open(p, _O_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    if close:
        os.close(fd)
        return -1
    return fd


def summary(agent_dir: str, limit: int = 500) -> dict:
    root = os.path.join(agent_dir, "memory")
    files, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in sorted(filenames):
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            total += st.st_size
            if len(files) < limit:
                files.append({"path": os.path.relpath(p, root), "size": st.st_size})
    return {"files": files, "bytes": total, "present": os.path.isdir(root)}


def clear(agent_dir: str) -> None:
    for n in ("memory", "memory.prev", "memory.new"):
        shutil.rmtree(os.path.join(agent_dir, n), ignore_errors=True)


def _fits(rel: str) -> bool:
    return len(rel.encode()) <= MAX_PATH and rel.count("/") < MAX_DEPTH


def add_to_tar(tar: tarfile.TarFile, agent_dir: str) -> list[str]:
    """Pack the stored memory into the boot disk under memory/.  The content
    was validated on import (regular files/dirs only), but re-check anyway.
    Returns what had to be left out: memory stored before the path limits, or a path
    the host can't open, never stops an agent from starting."""
    root = os.path.join(agent_dir, "memory")
    skipped: list[str] = []
    if not os.path.isdir(root) or os.path.islink(root):
        return skipped
    tar.addfile(_ti("memory", tarfile.DIRTYPE))
    onerror = lambda e: skipped.append(os.path.relpath(e.filename, root) if e.filename else "?")  # noqa: E731
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        dirnames.sort()
        rel = os.path.relpath(dirpath, root)
        for d in list(dirnames):
            r = os.path.normpath(os.path.join(rel, d))
            if os.path.islink(os.path.join(dirpath, d)) or not _fits(r):
                dirnames.remove(d)
                if not os.path.islink(os.path.join(dirpath, d)):
                    skipped.append(r)
                continue
            tar.addfile(_ti(os.path.join("memory", r), tarfile.DIRTYPE))
        for n in sorted(filenames):
            r = os.path.normpath(os.path.join(rel, n))
            if r == OLD_STATE_FILE:  # saved before the rename: it goes in under the new name, unless that one is there too
                if os.path.lexists(os.path.join(root, STATE_FILE)):
                    continue
                r = STATE_FILE
            if not _fits(r):
                skipped.append(r)
                continue
            try:
                fd = os.open(os.path.join(dirpath, n), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError:
                skipped.append(r)
                continue
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    continue
                tar.addfile(_ti(os.path.join("memory", r), tarfile.REGTYPE, st.st_size), f)
    return skipped


def _ti(name: str, typ, size: int = 0) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = typ
    ti.size = size if typ == tarfile.REGTYPE else 0
    ti.mode = 0o755 if typ == tarfile.DIRTYPE else 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    return ti
