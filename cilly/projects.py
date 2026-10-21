"""Projects: a reusable setup for agents that work on the same thing.

A project holds instructions for CLAUDE.md, extra allowed hosts, its repositories,
defaults for new agents, a research agent, files every agent gets read-only, and
notes: things worth remembering that agents suggest and you approve.

Settings live in projects.json (config dir, mode 0600, no secrets in it); files in
<data dir>/projects/<id>/files/.  Everything here is checked again on load, because
the file can be edited by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata

from .egress_proxy import normalize_allowlist

NAME_RE = re.compile(r"[A-Za-z0-9 ._()&+'-]{1,60}")
# the runner's _SAFE_NAME: a file with any other name is dropped inside the VM
VM_FILE_NAME = re.compile(r"[A-Za-z0-9._@+=,-][A-Za-z0-9._@+=, -]{0,119}")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+=, -]{0,119}")  # stored on the host too: no leading dot
OLD_FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()+,=@-]{0,119}")  # what earlier versions stored
MAX_PROJECTS = 50
MAX_INSTRUCTIONS = 20000
MAX_NOTES = 100
MAX_NOTE = 500
MAX_PENDING = 50
MAX_FILES = 50
MAX_FILE = 25 << 20
MAX_FILES_TOTAL = 100 << 20
MODES = ("ephemeral", "persistent", "memory")


class ProjectError(Exception):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clean_text(s, limit: int) -> str:
    """Plain text for CLAUDE.md: no terminal escapes or control characters (newlines and tabs stay),
    and nothing invisible or direction-changing (Unicode format characters, line and paragraph
    separators), so what you read and approve is what every agent gets.  Removing every format
    character has a cost, which is fine for CLAUDE.md: emoji joined with a zero-width joiner come
    apart, the England, Scotland and Wales flags become a plain black flag, and Persian loses its
    zero-width non-joiner."""
    s = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f\u2028\u2029]", "", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    return s.replace("\r", "")[:limit].strip()


def _site_ok(h) -> bool:
    try:
        return isinstance(h, str) and normalize_allowlist([h]) == [h]
    except ValueError:
        return False


def vm_file_name(name) -> str:
    """The name a file gets in the VM: accents come off ("Übersicht" is "Ubersicht"), other characters
    become _, so a browser's "report(1).pdf" arrives as "report_1_.pdf", and a long name is shortened
    before its extension.  Empty when nothing usable is left."""
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    name = re.sub(r"[^A-Za-z0-9._@+=, -]", "_", name)
    base, ext = os.path.splitext(name)
    name = (base[:120 - len(ext)] + ext if len(ext) <= 20 else name[:120]).strip()
    return name if VM_FILE_NAME.fullmatch(name) and name.strip(".") else ""


def _is_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def clean_name(s) -> str:
    name = " ".join(str(s or "").split())
    if not NAME_RE.fullmatch(name):
        raise ProjectError("A project name can use letters, digits, spaces and . _ ( ) & + ' - (up to 60).")
    return name


def note_lines(text: str) -> list[str]:
    """Notes an agent suggested: one per line, list bullets and blank lines dropped."""
    out = []
    for line in str(text).splitlines():
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line)
        line = clean_text(line, MAX_NOTE)
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _blank(pid: str, name: str) -> dict:
    return {"id": pid, "name": name, "created": _now(), "instructions": "", "allow": [], "repos": [],
            "defaults": {"mode": None, "auto_stop": None, "image": None, "model": None, "effort": None, "mcp": [], "plugins": [], "skills": [], "keep_outbox": None,
                         "model_server": None, "skip_global": [], "account": None, "disk_gb": None},
            "research_agent": None, "research_auto": False, "route": None,
            "notes": [], "pending": [], "seen": [], "files": []}


def _checked(p: dict) -> dict | None:
    """A project as stored, with anything malformed dropped (the file can be edited by hand)."""
    if not isinstance(p, dict) or not re.fullmatch(r"[0-9a-f]{8}", str(p.get("id", ""))):
        return None
    try:
        out = _blank(p["id"], clean_name(p.get("name")))
    except ProjectError:
        return None
    out["created"] = str(p.get("created") or out["created"])[:40]
    out["instructions"] = clean_text(p.get("instructions"), MAX_INSTRUCTIONS)
    out["allow"] = [h for h in p.get("allow") or [] if _site_ok(h)][:1000]
    out["repos"] = [r for r in p.get("repos") or [] if isinstance(r, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", r)][:100]
    d = p.get("defaults") if isinstance(p.get("defaults"), dict) else {}
    out["defaults"].update({
        "mode": d.get("mode") if d.get("mode") in MODES else None,
        "auto_stop": d.get("auto_stop") if isinstance(d.get("auto_stop"), bool) else None,
        "image": d.get("image") if isinstance(d.get("image"), str) and re.fullmatch(r"base-[\w.-]+\.qcow2", d["image"]) else None,
        "model": d.get("model") if isinstance(d.get("model"), str) and re.fullmatch(r"[\w.\[\]:/@+-]{1,200}", d["model"]) else None,
        # the model server the model is on, or None for one of Claude's (the panel checks it still exists)
        "model_server": d.get("model_server") if isinstance(d.get("model_server"), str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", d["model_server"]) else None,
        "effort": d.get("effort") if isinstance(d.get("effort"), str) and re.fullmatch(r"[a-z]{1,10}", d["effort"]) else None,
        "mcp": [m for m in d.get("mcp") or [] if isinstance(m, str) and re.fullmatch(r"[\w.-]{1,64}", m)][:50],
        "plugins": [m for m in d.get("plugins") or [] if isinstance(m, str) and re.fullmatch(r"[\w.-]{1,64}", m)][:50],
        "skills": [m for m in d.get("skills") or [] if isinstance(m, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", m)][:50],
        # GB for new agents' disks (the panel keeps it within its limit)
        "disk_gb": d.get("disk_gb") if type(d.get("disk_gb")) is int and 20 <= d["disk_gb"] <= 4096 else None,
        # the GitHub account (a token's name) for a repository more than one has ticked (the panel checks it)
        "account": d.get("account") if isinstance(d.get("account"), str) and re.fullmatch(r"[^\x00-\x1f\x7f]{1,64}", d["account"]) else None,
        # sites of the global allowlist new agents here go without (the panel keeps only ones on it)
        "skip_global": [h for h in d.get("skip_global") or [] if _site_ok(h)][:1000],
        # a folder on this PC (the panel checks it again before each use)
        "keep_outbox": d.get("keep_outbox") if isinstance(d.get("keep_outbox"), str) and d["keep_outbox"].startswith("/")
        and len(d["keep_outbox"]) <= 4096 and not any(unicodedata.category(c)[0] == "C" for c in d["keep_outbox"]) else None,
    })
    rt = p.get("route")  # None: the standard in Settings; direct, tor or a route name (the panel checks it exists)
    out["route"] = rt if isinstance(rt, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", rt) else None
    ra = p.get("research_agent")
    out["research_agent"] = ra if isinstance(ra, str) and re.fullmatch(r"[0-9a-f]{8}", ra) else None
    out["research_auto"] = p.get("research_auto") is True and bool(out["research_agent"])
    for key, limit in (("notes", MAX_NOTES), ("pending", MAX_PENDING)):
        out[key] = [{"id": n["id"], "text": clean_text(n.get("text"), MAX_NOTE), "created": str(n.get("created") or "")[:40],
                     "agent": n.get("agent") if isinstance(n.get("agent"), str) else None}
                    for n in p.get(key) or [] if isinstance(n, dict) and re.fullmatch(r"[0-9a-f]{8}", str(n.get("id", "")))
                    and clean_text(n.get("text"), MAX_NOTE)][-limit:]
    out["seen"] = [h for h in p.get("seen") or [] if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{16}", h)][-2000:]
    out["files"] = [{"name": f["name"], "size": int(f.get("size") or 0), "sha256": str(f.get("sha256") or "")[:64],
                     "added": str(f.get("added") or "")[:40]}
                    for f in p.get("files") or [] if isinstance(f, dict) and isinstance(f.get("name"), str)
                    and FILE_NAME_RE.fullmatch(f["name"])][:MAX_FILES]
    return out


class ProjectStore:
    def __init__(self, path: str, files_root: str):
        self.path, self.files_root = path, files_root
        self._lock = threading.RLock()
        self._projects: dict[str, dict] = {}
        try:
            with open(path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        renamed = False
        for p in raw.get("projects", []) if isinstance(raw, dict) else []:
            renamed |= self._rename_old_files(p)
            p = _checked(p)
            if p:
                self._projects[p["id"]] = p
        if renamed:
            self._save()

    def _rename_old_files(self, p) -> bool:
        """Files an earlier version kept under names the VM refuses, like "Guide (v2).md": renamed on
        disk to the name they'd get now, so they're neither dropped nor left behind.  Never over an
        existing file, and never a link."""
        if not isinstance(p, dict) or not re.fullmatch(r"[0-9a-f]{8}", str(p.get("id", ""))) or not isinstance(p.get("files"), list):
            return False
        d, renamed = self._files_dir(p["id"]), False
        taken = {f.get("name") for f in p["files"] if isinstance(f, dict)}
        for f in p["files"]:
            old = f.get("name") if isinstance(f, dict) else None
            if not isinstance(old, str) or FILE_NAME_RE.fullmatch(old) or not OLD_FILE_NAME_RE.fullmatch(old):
                continue
            new = vm_file_name(old)
            if not _is_file(os.path.join(d, old)):
                # renamed before, but the panel stopped before it saved the new name: adopt it
                if FILE_NAME_RE.fullmatch(new) and new not in taken and _is_file(os.path.join(d, new)):
                    f["name"], renamed = new, True
                    taken.add(new)
                continue
            base, ext = os.path.splitext(new)
            for name in [new] + [f"{base[:110]}-{n}{ext}" for n in range(2, 50)]:
                if not FILE_NAME_RE.fullmatch(name) or name in taken:
                    continue
                try:
                    os.link(os.path.join(d, old), os.path.join(d, name), follow_symlinks=False)  # fails if it exists
                except FileExistsError:
                    continue
                except OSError:
                    break
                os.unlink(os.path.join(d, old))
                f["name"], renamed = name, True
                taken.add(name)
                break
            else:
                print(f"projects: kept {old!r} under its old name, no free name for it", flush=True)
        return renamed

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"projects": list(self._projects.values())}, f, indent=1)
        os.replace(tmp, self.path)

    # -- projects ---------------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            return sorted((json.loads(json.dumps(p)) for p in self._projects.values()), key=lambda p: p["name"].lower())

    def get(self, pid) -> dict:
        with self._lock:
            p = self._projects.get(str(pid))
            if not p:
                raise ProjectError("no such project")
            return json.loads(json.dumps(p))

    def find(self, name: str) -> dict | None:
        with self._lock:
            return next((json.loads(json.dumps(p)) for p in self._projects.values() if p["name"].lower() == name.lower()), None)

    def create(self, name: str) -> dict:
        name = clean_name(name)
        with self._lock:
            if self.find(name):
                raise ProjectError(f"There is already a project called {name}.")
            if len(self._projects) >= MAX_PROJECTS:
                raise ProjectError(f"At most {MAX_PROJECTS} projects.")
            pid = secrets.token_hex(4)
            self._projects[pid] = _blank(pid, name)
            self._save()
            return self.get(pid)

    def update(self, pid: str, **change) -> dict:
        """`change` is already validated by the caller where it needs outside knowledge
        (hosts, repositories, images, models); the stored result is checked again."""
        with self._lock:
            p = self._projects.get(pid)
            if not p:
                raise ProjectError("no such project")
            if "name" in change:
                name = clean_name(change["name"])
                other = self.find(name)
                if other and other["id"] != pid:
                    raise ProjectError(f"There is already a project called {name}.")
                change["name"] = name
            if "instructions" in change:
                change["instructions"] = clean_text(change["instructions"], MAX_INSTRUCTIONS)
            if "defaults" in change:
                change["defaults"] = dict(p["defaults"], **change["defaults"])
            new = _checked(dict(p, **change))
            self._projects[pid] = new
            self._save()
            return self.get(pid)

    def delete(self, pid: str) -> None:
        with self._lock:
            if self._projects.pop(pid, None) is None:
                raise ProjectError("no such project")
            self._save()
            d = self._files_dir(pid)
            for f in os.listdir(d) if os.path.isdir(d) else []:
                os.unlink(os.path.join(d, f))
            for path in (d, os.path.dirname(d)):
                try:
                    os.rmdir(path)
                except OSError:
                    pass

    # -- files --------------------------------------------------------------------
    def _files_dir(self, pid: str) -> str:
        return os.path.join(self.files_root, pid, "files")

    @staticmethod
    def clean_file_name(name: str) -> str:
        name = vm_file_name(name)
        if not name:
            raise ProjectError("Give the file a name with letters or digits in it.")
        if not FILE_NAME_RE.fullmatch(name):
            raise ProjectError("A file name must start with a letter or digit.")
        return name

    def add_file(self, pid: str, name: str, data: bytes) -> dict:
        name = self.clean_file_name(name)
        if not data or len(data) > MAX_FILE:
            raise ProjectError(f"Project files must be 1 byte to {MAX_FILE >> 20} MiB.")
        with self._lock:
            p = self._projects.get(pid)
            if not p:
                raise ProjectError("no such project")
            others = [f for f in p["files"] if f["name"] != name]
            if len(others) >= MAX_FILES:
                raise ProjectError(f"At most {MAX_FILES} files per project.")
            if sum(f["size"] for f in others) + len(data) > MAX_FILES_TOTAL:
                raise ProjectError(f"A project's files can be {MAX_FILES_TOTAL >> 20} MiB together.")
            d = self._files_dir(pid)
            os.makedirs(d, mode=0o700, exist_ok=True)
            tmp = os.path.join(d, ".upload-" + secrets.token_hex(4))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(d, name))
            p["files"] = others + [{"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "added": _now()}]
            self._save()
            return self.get(pid)

    def remove_file(self, pid: str, name: str) -> dict:
        with self._lock:
            p = self._projects.get(pid)
            if not p or not any(f["name"] == name for f in p["files"]):
                raise ProjectError("no such file")
            p["files"] = [f for f in p["files"] if f["name"] != name]
            try:
                os.unlink(os.path.join(self._files_dir(pid), self.clean_file_name(name)))
            except FileNotFoundError:
                pass
            self._save()
            return self.get(pid)

    def file_paths(self, pid: str) -> list[dict]:
        """[{name, path}] of the files that are really there, for sending into a VM."""
        with self._lock:
            p = self._projects.get(pid)
            if not p:
                return []
            d = self._files_dir(pid)
            out = []
            for f in p["files"]:
                path = os.path.join(d, f["name"])
                if os.path.isfile(path) and not os.path.islink(path):
                    out.append({"name": f["name"], "path": path})
            return out

    # -- notes ----------------------------------------------------------------------
    def propose(self, pid: str, agent_id: str, texts: list[str]) -> list[dict]:
        """Notes an agent suggested.  Each wording is only ever proposed once, so a file the
        agent keeps adding to doesn't bring back what you already approved or dismissed."""
        with self._lock:
            p = self._projects.get(pid)
            if not p:
                return []
            new = []
            for t in texts[:20]:
                t = clean_text(t, MAX_NOTE)
                h = hashlib.sha256(" ".join(t.lower().split()).encode()).hexdigest()[:16]
                if not t or h in p["seen"] or len(p["pending"]) >= MAX_PENDING:
                    continue
                p["seen"].append(h)
                n = {"id": secrets.token_hex(4), "text": t, "created": _now(), "agent": agent_id}
                p["pending"].append(n)
                new.append(n)
            if new:
                p["seen"] = p["seen"][-2000:]
                self._save()
            return new

    def decide(self, pid: str, nid: str, approve: bool, text: str | None = None) -> dict:
        with self._lock:
            p = self._projects.get(pid)
            n = next((n for n in (p or {}).get("pending", []) if n["id"] == nid), None)
            if not n:
                raise ProjectError("That note was already handled.")
            if approve and len(p["notes"]) >= MAX_NOTES:  # before it leaves the pending list: it stays there
                raise ProjectError(f"A project keeps at most {MAX_NOTES} notes. Remove some first.")
            p["pending"] = [x for x in p["pending"] if x["id"] != nid]
            if approve:
                # cleaned first: an edit of nothing but invisible characters keeps the suggestion's text
                n = dict(n, text=clean_text(text, MAX_NOTE) or n["text"], created=_now())
                p["notes"].append(n)
            self._save()
            return self.get(pid)

    def add_note(self, pid: str, text: str) -> dict:
        text = clean_text(text, MAX_NOTE)
        if not text:
            raise ProjectError("Write the note first.")
        with self._lock:
            p = self._projects.get(pid)
            if not p:
                raise ProjectError("no such project")
            if len(p["notes"]) >= MAX_NOTES:
                raise ProjectError(f"A project keeps at most {MAX_NOTES} notes. Remove some first.")
            p["notes"].append({"id": secrets.token_hex(4), "text": text, "created": _now(), "agent": None})
            self._save()
            return self.get(pid)

    def remove_note(self, pid: str, nid: str) -> dict:
        with self._lock:
            p = self._projects.get(pid)
            if not p or not any(n["id"] == nid for n in p["notes"]):
                raise ProjectError("no such note")
            p["notes"] = [n for n in p["notes"] if n["id"] != nid]
            self._save()
            return self.get(pid)
