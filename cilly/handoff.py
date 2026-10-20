"""Research deliveries: text files a research agent wrote to ~/outbox/.

They arrive over the control port and may later be forwarded into a coding
agent's /inbox.  The content is untrusted (it came from the open web), so:
* only UTF-8 text files, with safe relative names, limited size and count,
* no git repositories (exports.git_left_out): those files are left out,
* stored as plain data under the research agent's directory,
* shown in the panel as text, never rendered as HTML,
* forwarded to a coding agent only when you approve it (or when you turned
  on auto-forwarding for that coding agent).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time

MAX_FILES = 200
MAX_FILE = 1024 * 1024
_SAFE = re.compile(r"^[A-Za-z0-9._@+=,-][A-Za-z0-9._@+=, -]{0,120}$")
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class HandoffError(Exception):
    pass


def validate_files(files, max_total: int) -> tuple[list[tuple[str, str]], int]:
    """The files to store, and how many git folders were left out of them."""
    from .exports import HEAD_PEEK, git_left_out, is_head_name, left_out_by
    if not isinstance(files, list) or not files:
        raise HandoffError("no files")
    if len(files) > MAX_FILES:
        raise HandoffError("too many files")
    out, total, seen = [], 0, set()
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
            raise HandoffError("malformed file entry")
        parts = f["path"].split("/")
        if len(parts) > 4 or any(p in ("", ".", "..") or not _SAFE.match(p) for p in parts):
            raise HandoffError(f"unsafe file name {f['path'][:80]!r}")
        data = f["content"].encode("utf-8", "strict")
        if "\x00" in f["content"]:
            raise HandoffError("binary content")
        total += len(data)
        if len(data) > MAX_FILE or total > max_total:
            raise HandoffError("delivery too large")
        path = "/".join(parts)
        if path in seen:
            raise HandoffError("duplicate file")
        seen.add(path)
        out.append((path, f["content"]))
    for path, _ in out:
        if any("/".join(path.split("/")[:i]) in seen for i in range(1, path.count("/") + 1)):
            raise HandoffError(f"{path[:80]!r} is inside a file of the same name")
    left = git_left_out((p.split("/"), c.encode()[:HEAD_PEEK] if is_head_name(p.split("/")[-1]) else None) for p, c in out)
    keep = [(p, c) for p, c in out if left_out_by(p.split("/"), left) is None]
    if not keep:
        raise HandoffError("no files")
    return keep, len({left_out_by(p.split("/"), left) for p, _ in out} - {None})


def deliveries_dir(agent_dir: str) -> str:
    d = os.path.join(agent_dir, "deliveries")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def store(agent_dir: str, files: list[tuple[str, str]], info: dict) -> dict:
    did = secrets.token_hex(4)
    root = os.path.join(deliveries_dir(agent_dir), did)
    os.mkdir(root, 0o700)
    os.mkdir(os.path.join(root, "files"), 0o700)
    rfd = os.open(os.path.join(root, "files"), _O_DIR)
    try:
        _write_files(rfd, files)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)  # nothing half-written stays behind
        raise
    finally:
        os.close(rfd)
    meta = dict(info, id=did, created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                files=[{"path": p, "size": len(c.encode())} for p, c in files], forwarded_to=[])
    save_meta(agent_dir, meta)
    return meta


def _write_files(rfd: int, files: list[tuple[str, str]]) -> None:
    for path, content in files:
        *dirs, name = path.split("/")
        fd = os.dup(rfd)
        try:
            for d in dirs:
                try:
                    os.mkdir(d, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(d, _O_DIR, dir_fd=fd)
                os.close(fd)
                fd = nfd
            ffd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=fd)
        finally:
            os.close(fd)
        with os.fdopen(ffd, "w", encoding="utf-8") as fh:
            fh.write(content)


def save_meta(agent_dir: str, meta: dict) -> None:
    p = os.path.join(deliveries_dir(agent_dir), meta["id"], "delivery.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, p)


def list_all(agent_dir: str) -> list[dict]:
    out = []
    d = os.path.join(agent_dir, "deliveries")
    if not os.path.isdir(d):
        return out
    for did in os.listdir(d):
        p = os.path.join(d, did, "delivery.json")
        if re.fullmatch(r"[0-9a-f]{8}", did) and os.path.isfile(p):
            with open(p) as f:
                out.append(json.load(f))
    return sorted(out, key=lambda m: m["created"], reverse=True)


def get(agent_dir: str, did: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{8}", did):
        raise HandoffError("bad delivery id")
    p = os.path.join(agent_dir, "deliveries", did, "delivery.json")
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HandoffError("no such delivery") from None


def read_files(agent_dir: str, did: str) -> list[dict]:
    meta = get(agent_dir, did)
    root = os.path.join(agent_dir, "deliveries", did, "files")
    out = []
    for f in meta["files"]:
        fd = os.open(os.path.join(root, f["path"]), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, encoding="utf-8") as fh:
            out.append({"path": f["path"], "content": fh.read(MAX_FILE)})
    return out
