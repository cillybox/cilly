"""Skills you upload: your own Claude Code skills, kept private on this PC.

A skill is a folder with a SKILL.md (a name and a description at the top) and maybe more
files next to it, such as scripts or references.  You upload it as a .zip, or just its
SKILL.md.  It's checked here, never unpacked to disk: regular files only, no links, no
paths that climb out, safe names, and size limits.  What passes is packed again into a
plain tar of its own (regular files, owned by nobody, no times), stored in <data dir>/skills
as <name>.tar with skills.json.  The VM gets that tar like a project file and unpacks it
into the agent's ~/.claude/skills/<name>/ as the agent user.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import tarfile
import threading
import time
import zipfile

from .projects import clean_text

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # Claude Code's skill names
# One folder or file name inside a skill.  A leading - is fine: names never reach a shell here, and the VM
# unpacks them with tarfile.  Docs mirrors write "(" as "-28".
PART_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._@+=, -]{0,119}")
MAX_UPLOAD = 10 << 20
MAX_TOTAL = 20 << 20      # unpacked
MAX_ENTRIES = 2000  # a skill with a big reference library; the size limits are what keep it small
MAX_DEPTH = 16  # folders inside the skill: docs mirrors nest deep.  Its own folder in the zip doesn't count.
MAX_ZIP_DEPTH = MAX_DEPTH + 8  # in the zip, with the folders around the skill (a repository's skills/<name>/)
MAX_SKILLS = 50
MAX_DESCRIPTION = 1024
JUNK = ("__MACOSX/", ".DS_Store")  # what a Mac adds to a zip


class SkillError(Exception):
    pass


def _shown(name: str) -> str:
    """A path for a message: the end of a long one, which says where it is."""
    return name if len(name) <= 100 else "…" + name[-99:]


def _frontmatter(text: str) -> dict:
    """The key: value lines between the --- lines at the top of SKILL.md.  A value can go on over
    indented lines, as YAML's | and > blocks do; they're joined with spaces."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out, key = {}, None
    for line in lines[1:400]:
        if line.strip() == "---":
            return {k: " ".join(v.split()).strip("\"'") for k, v in out.items()}
        if key and line[:1] in (" ", "\t"):  # a value going on
            out[key] += " " + line.strip()
            continue
        k, colon, value = line.partition(":")
        key = k.strip() if colon and re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", k.strip()) else None
        if key:
            value = value.strip()
            out[key] = "" if re.fullmatch(r"[|>][+-]?", value) else value
    return {}  # never closed


def _files_from_zip(data: bytes) -> dict:
    """{path: (bytes, executable)} of a zip's regular files, or a SkillError saying what's wrong."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise SkillError("That zip file can't be read.") from None
    files, total = {}, 0
    for info in infos:
        name = info.filename
        if name.startswith(JUNK[0]) or name.rsplit("/", 1)[-1] == JUNK[1] or any(p.startswith(".") and p not in (".", "..") for p in name.split("/")):
            continue  # what a Mac adds, and hidden files such as .git
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SkillError(f"It holds a link, {name[:80]}. A skill can only hold plain files.")
        if name.endswith("/"):
            continue  # a folder: made from the paths of its files
        parts = name.split("/")
        if "\\" in name or not all(PART_RE.fullmatch(p) and p not in (".", "..") for p in parts):
            raise SkillError(f"It holds a name a skill can't have, {_shown(name)!r}. Use letters, digits and ._-")
        if len(parts) > MAX_ZIP_DEPTH:
            raise SkillError(f"It holds a file nested too deep, {_shown(name)!r}. A skill can have at most {MAX_DEPTH} levels of folders.")
        if info.flag_bits & 0x1:
            raise SkillError("The zip file is encrypted.")
        if len(files) >= MAX_ENTRIES:
            raise SkillError(f"A skill can have at most {MAX_ENTRIES} files.")
        total += info.file_size
        if total > MAX_TOTAL:
            raise SkillError(f"A skill can be at most {MAX_TOTAL >> 20} MiB unpacked.")
        try:
            with z.open(info) as f:
                body = f.read(info.file_size + 1)
        except (zipfile.BadZipFile, ValueError, OSError, NotImplementedError, RuntimeError):
            raise SkillError(f"Couldn't read {name[:80]} from the zip file.") from None
        if len(body) != info.file_size:
            raise SkillError("The zip file says one size and holds another.")
        files[name] = (body, bool(mode & 0o111))
    # The skill's folder is where the SKILL.md nearest the top is: the zip's top, the skill's own folder,
    # or deeper, like a repository's skills/<name>/.  What's outside it, like a README, is left out.
    found = sorted((p.count("/"), p) for p in files if p.rsplit("/", 1)[-1] == "SKILL.md")
    if found:
        same = [p for depth, p in found if depth == found[0][0]]
        if len(same) > 1:
            names = ", ".join(p.rsplit("/", 2)[-2] for p in same[:5])
            raise SkillError(f"It holds {len(same)} skills ({names}). Upload each skill's folder on its own.")
        root = same[0][:-len("SKILL.md")]
        files = {p[len(root):]: v for p, v in files.items() if p.startswith(root)}
    deep = next((p for p in files if p.count("/") > MAX_DEPTH), None)
    if deep:
        raise SkillError(f"It holds a file nested too deep, {_shown(deep)!r}. A skill can have at most {MAX_DEPTH} levels of folders.")
    return files


def check(upload_name: str, data: bytes) -> tuple[str, str, dict]:
    """(name, description, files) of an uploaded skill: a .zip, or a SKILL.md on its own."""
    if not isinstance(data, bytes) or not data:
        raise SkillError("The file is empty.")
    if len(data) > MAX_UPLOAD:
        raise SkillError(f"Upload a skill of at most {MAX_UPLOAD >> 20} MiB.")
    if data[:4] == b"PK\x03\x04":
        files = _files_from_zip(data)
    elif str(upload_name or "").lower().endswith(".md"):
        files = {"SKILL.md": (data, False)}
    else:
        raise SkillError("Upload the skill as a .zip file, or its SKILL.md.")
    if "SKILL.md" not in files:
        raise SkillError("There's no SKILL.md in it.")
    try:
        text = files["SKILL.md"][0].decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError("SKILL.md isn't UTF-8 text.") from None
    meta = _frontmatter(text)
    name = meta.get("name", "")
    if not NAME_RE.fullmatch(name):
        raise SkillError("SKILL.md needs a name: line at the top, in small letters, digits and -, like my-skill.")
    description = clean_text(meta.get("description", ""), MAX_DESCRIPTION)
    if not description:
        raise SkillError("SKILL.md needs a description: line at the top, so Claude knows when to use it.")
    return name, " ".join(description.split()), files


def pack(files: dict) -> bytes:
    """A plain tar of the checked files: regular files only, the same every time."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(files):
            body, executable = files[path]
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(body), 0o755 if executable else 0o644, 0
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class SkillStore:
    """<data dir>/skills: <name>.tar files and skills.json."""

    def __init__(self, root: str):
        self.root = root
        self.index = os.path.join(root, "skills.json")
        self._lock = threading.Lock()
        self._skills = self._load()

    def _path(self, name: str) -> str:
        return os.path.join(self.root, name + ".tar")

    def _load(self) -> dict:
        try:
            with open(self.index, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for s in raw.get("skills", []) if isinstance(raw, dict) else []:
            name = s.get("name") if isinstance(s, dict) else None
            if isinstance(name, str) and NAME_RE.fullmatch(name) and os.path.isfile(self._path(name)) \
                    and re.fullmatch(r"[0-9a-f]{64}", str(s.get("sha256", ""))):
                out[name] = {"name": name, "description": clean_text(s.get("description", ""), MAX_DESCRIPTION),
                             "files": int(s.get("files") or 0), "size": int(s.get("size") or 0),
                             "sha256": s["sha256"], "added": str(s.get("added", ""))[:40]}
        return out

    def _save(self) -> None:
        tmp = self.index + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"skills": list(self._skills.values())}, f, indent=1)
        os.replace(tmp, self.index)

    def list(self) -> dict:
        with self._lock:
            return {n: dict(s) for n, s in self._skills.items()}

    def add(self, upload_name: str, data: bytes) -> dict:
        """Check and store a skill.  One with the same name is replaced."""
        name, description, files = check(upload_name, data)
        tar = pack(files)
        with self._lock:
            replaced = name in self._skills
            if not replaced and len(self._skills) >= MAX_SKILLS:
                raise SkillError(f"At most {MAX_SKILLS} skills. Remove one first.")
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            tmp = os.path.join(self.root, ".upload-" + secrets.token_hex(4))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(tar)
            os.replace(tmp, self._path(name))
            self._skills[name] = {"name": name, "description": description, "files": len(files),
                                  "size": sum(len(b) for b, _ in files.values()), "sha256": hashlib.sha256(tar).hexdigest(),
                                  "added": time.strftime("%Y-%m-%d %H:%M")}
            self._save()
            return dict(self._skills[name], replaced=replaced)

    def remove(self, name: str) -> None:
        with self._lock:
            if name not in self._skills:
                raise SkillError("There's no skill called that.")
            del self._skills[name]
            try:
                os.unlink(self._path(name))
            except FileNotFoundError:
                pass
            self._save()

    def files(self, names) -> list[dict]:
        """[{name, path, sha256}] of these skills' tars, for sending into a VM."""
        with self._lock:
            return [{"name": n + ".tar", "skill": n, "path": self._path(n), "sha256": self._skills[n]["sha256"]}
                    for n in sorted(set(names)) if n in self._skills and os.path.isfile(self._path(n))]
