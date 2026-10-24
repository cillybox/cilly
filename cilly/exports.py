"""Exports: files an agent's VM hands over when you press Export.

The guest streams a tar over the control port; the host extracts it here
into a *new* subfolder of a destination you chose.  The tar is untrusted:
* only regular files and directories (no links, devices, FIFOs),
* no absolute paths, no "..", no NULs or backslashes, at most 32 levels and 1024 bytes,
* size and entry-count limits (folders count too, also ones only implied by a path),
* written with O_EXCL|O_NOFOLLOW relative to directory fds, never
  overwriting anything, with executable bits removed,
* no git repositories: a .git (any case) or a folder git would take for a repository
  (one holding a valid HEAD) is left out, because git runs commands its config names
  (core.fsmonitor, core.pager) as soon as you, or your shell prompt, run git in that folder.
"""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import unicodedata

_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_PATH, MAX_DEPTH = 1024, 32  # one path from a VM, like memory's


class ExportError(Exception):
    pass


def check_destination(dest: str, home: str | None = None) -> str:
    """Where exports may go: an existing folder, not a symlink, and not a
    sensitive location (same rules as inject sources: no /, no home itself,
    no ~/.config, ~/.ssh, ~/.local, ...)."""
    from .inject import InjectError, check_allowed_root
    if not os.path.isabs(dest) or not os.path.isdir(dest) or os.path.islink(dest):
        raise ExportError("destination must be an existing absolute folder (not a symlink)")
    try:
        return check_allowed_root(dest, home)
    except InjectError as e:
        raise ExportError(f"not a safe destination: {e}") from None


def _fold(part: str) -> str:
    """A name as git or a case-insensitive filesystem may read it: ignorable characters and
    trailing dots and spaces gone, any case."""
    return "".join(c for c in part if unicodedata.category(c) != "Cf").rstrip(". ").lower()


def is_git_name(part: str) -> bool:
    return _fold(part) in (".git", "git~1")  # git~1: the NTFS short name


def is_head_name(part: str) -> bool:
    return _fold(part) == "head"


HEAD_PEEK = 128  # how much of a file named HEAD to read to tell whether git would take it


def looks_like_head(data: bytes) -> bool:
    """What git accepts as HEAD: a symbolic ref, or an object id (SHA-1 or SHA-256)."""
    return data.startswith(b"ref:") or re.match(rb"[0-9a-fA-F]{40}", data) is not None


def git_left_out(entries) -> dict:
    """{folder: why} to leave out of a tree from a VM, given (parts, head) for each entry, where
    head is the start of a regular file named HEAD and None otherwise: everything from a part
    named .git on, and every folder holding a HEAD git would accept.  Git takes such a folder
    for a repository and reads objects, refs and config from wherever a commondir file in it
    points, even outside it, so nothing else about the folder can be trusted.  The root is ()."""
    out = {}
    for parts, head in entries:
        parts = tuple(parts)
        g = next((i for i, p in enumerate(parts) if is_git_name(p)), None)
        if g is not None:
            out.setdefault(parts[:g + 1], "git")
        if head is not None and looks_like_head(head):
            out.setdefault(parts[:-1], "head")
    return out


def tar_head(tf: tarfile.TarFile, m: tarfile.TarInfo, parts) -> bytes | None:
    """The start of a regular file named HEAD in a tar, for git_left_out."""
    if not m.isreg() or not is_head_name(parts[-1]):
        return None
    return tf.extractfile(m).read(HEAD_PEEK)


def left_out_by(parts, left: dict):
    """The left-out folder `parts` is in, or None."""
    return next((tuple(parts[:i]) for i in range(len(parts) + 1) if tuple(parts[:i]) in left), None)


def _parts(name: str) -> list[str]:
    if name.startswith("/") or "\\" in name or "\x00" in name or len(name.encode()) > MAX_PATH:
        raise ExportError(f"unsafe path {name[:80]!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(unicodedata.category(c) == "Cc" for p in parts for c in p):
        raise ExportError(f"unsafe path {name[:80]!r}")  # ESC and newlines would drive a terminal that lists the folder
    if not parts or len(parts) > MAX_DEPTH or any(p == ".." or len(p.encode()) > 255 for p in parts):
        raise ExportError(f"unsafe path {name[:80]!r}")
    return parts


def add_prefixes(seen: set, parts) -> None:
    """Every folder on the way to parts, and parts itself, into seen (for the entry count)."""
    pre = ()
    for p in parts:
        pre += (p,)
        seen.add(pre)


def safe_extract_tar(tar_path: str, dest_root: str, subdir: str, *, max_bytes: int, max_files: int) -> dict:
    """Extract into dest_root/subdir, which must not exist yet.  On any error
    the new subfolder is removed again, so nothing half-written stays."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", subdir) or subdir in (".", ".."):
        raise ExportError("bad subfolder name")
    dfd = os.open(dest_root, _O_DIR)
    try:
        os.mkdir(subdir, 0o755, dir_fd=dfd)
        out_fd = os.open(subdir, _O_DIR, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        files, total, left = _extract(tar_path, out_fd, max_bytes, max_files)
    except BaseException:
        # only our own, just-created subfolder
        shutil.rmtree(os.path.join(dest_root, subdir), ignore_errors=True)
        raise
    finally:
        os.close(out_fd)
    return {"path": os.path.join(dest_root, subdir), "files": files, "bytes": total, "left_out": left}


def _open_dirs(out_fd: int, parts: list[str]) -> int:
    fd = os.dup(out_fd)
    try:
        for p in parts:
            try:
                os.mkdir(p, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            nfd = os.open(p, _O_DIR, dir_fd=fd)  # O_NOFOLLOW: a file or link here fails
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _extract(tar_path: str, out_fd: int, max_bytes: int, max_files: int) -> tuple[list[str], int, int]:
    """Returns (files written, bytes, how many git folders were left out)."""
    files, total = [], 0
    try:
        tf = tarfile.open(tar_path, mode="r:")
    except tarfile.TarError as e:
        raise ExportError(f"not a tar stream: {e}") from None
    with tf:
        try:
            members, seen = [], set()  # first the whole list: a repository is a folder's content
            for m in tf:
                parts = _parts(m.name)
                add_prefixes(seen, parts)
                if len(seen) > max_files:
                    raise ExportError("export exceeds the size or file-count limit")
                members.append((m, parts))
            left = git_left_out((parts, tar_head(tf, m, parts)) for m, parts in members)
            used = set()
            for m, parts in members:
                skip = left_out_by(parts, left)
                if skip is not None:
                    used.add(skip)
                    continue
                if m.isdir():
                    os.close(_open_dirs(out_fd, parts))
                    continue
                if not m.isreg():
                    raise ExportError(f"not a regular file: {m.name[:80]!r}")
                total += m.size
                if total > max_bytes:
                    raise ExportError("export exceeds the size or file-count limit")
                pfd = _open_dirs(out_fd, parts[:-1])
                try:
                    ffd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o644, dir_fd=pfd)
                finally:
                    os.close(pfd)
                src = tf.extractfile(m)
                with os.fdopen(ffd, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                files.append("/".join(parts))
        except tarfile.TarError as e:
            raise ExportError(f"broken tar stream: {e}") from None
        except FileExistsError:
            raise ExportError("the export names the same file twice") from None
        except NotADirectoryError:
            raise ExportError("the export uses a file name as a folder") from None
    return files, total, len(used)
