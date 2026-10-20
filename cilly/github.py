"""Minimal GitHub REST client (stdlib urllib) and safe artifact extraction.

The panel only ever talks to api.github.com with a *read-only* token, plus the
short-lived signed URL GitHub redirects artifact downloads to.  The token is
never sent to that redirect target: redirects are followed manually and the
second request carries no Authorization header.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .config import valid_repo

API = "https://api.github.com"
UA = "Mozilla/5.0"  # generic: GitHub knows the bot from its token, nobody else needs to know it is cilly


class GitHubError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def set_route(route: dict | None) -> None:
    """Send the panel's GitHub calls through a route (egress.route_own), or straight out (None)."""
    global _opener
    from . import routed
    _opener = routed.opener(route, _NoRedirect)


def _request(url: str, token: str | None, *, accept: str = "application/vnd.github+json",
             method: str = "GET", body: dict | None = None):
    if not url.startswith(API + "/"):
        raise GitHubError("refusing non-API URL")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", UA)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        return _opener.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e  # caller handles Location
        msg = e.read(2000).decode(errors="replace")
        raise GitHubError(f"GitHub API {e.code}: {msg[:300]}") from None
    except urllib.error.URLError as e:
        raise GitHubError(f"network error: {e.reason}") from None


def api_get(path: str, token: str | None, **params) -> dict | list:
    q = ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})) if params else ""
    with _request(API + path + q, token) as r:
        return json.load(r)


def _q(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def list_agent_prs(repo: str, token: str | None, mine) -> list[dict]:
    """The open pull requests from agent branches: mine(ref) says which branches are your agents'."""
    prs = api_get(f"/repos/{_q(repo)}/pulls", token, state="open", per_page=100)
    out = []
    for p in prs:
        head = p.get("head") or {}
        if not mine(str(head.get("ref", ""))):
            continue
        if (head.get("repo") or {}).get("full_name") != repo:
            continue
        url = str(p.get("html_url", ""))
        if not url.startswith("https://github.com/"):
            continue
        out.append({"repo": repo, "number": p["number"], "title": p.get("title", ""),
                    "branch": head["ref"], "sha": head.get("sha", ""), "url": url,
                    "updated_at": p.get("updated_at", ""), "draft": p.get("draft", False)})
    return out


def list_repos(token: str, pages: int = 3) -> list[dict]:
    """Repositories this token can reach (first-run setup's repo picker)."""
    out = []
    for page in range(1, pages + 1):
        batch = api_get("/user/repos", token, per_page=100, page=page, sort="pushed")
        if not isinstance(batch, list):
            break
        for r in batch:
            name = r.get("full_name") if isinstance(r, dict) else None
            if isinstance(name, str) and valid_repo(name):
                perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
                out.append({"full_name": name, "private": bool(r.get("private")),
                            "language": str(r.get("language") or "")[:40], "pushed_at": str(r.get("pushed_at") or "")[:40],
                            "permissions": {k: bool(perms.get(k)) for k in ("admin", "maintain", "push", "pull")}})
        if len(batch) < 100:
            break
    return out


def repo_permissions(repo: str, token: str) -> dict:
    r = api_get(f"/repos/{_q(repo)}", token)
    return {"full_name": r.get("full_name"), "private": r.get("private"),
            "permissions": r.get("permissions", {})}


def repo_overview(repo: str, token: str) -> dict:
    """Push access, when it was made and pushed to, and how many commits it has (None: GitHub didn't say)."""
    try:
        r = api_get(f"/repos/{_q(repo)}", token)
    except ValueError:  # not JSON
        r = None
    if not isinstance(r, dict):
        raise GitHubError("unexpected reply")
    perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
    out = {"push": bool(perms.get("push")), "created_at": str(r.get("created_at") or "")[:40],
           "pushed_at": str(r.get("pushed_at") or "")[:40], "commits": None}
    try:
        # one commit per page: the last page's number is how many there are
        with _request(f"{API}/repos/{_q(repo)}/commits?per_page=1", token) as resp:
            link, batch = resp.headers.get("Link") or "", json.load(resp)
    except GitHubError as e:
        if str(e).startswith("GitHub API 409"):  # "Git Repository is empty."
            out["commits"] = 0
        return out
    except ValueError:
        return out
    m = re.search(r'[?&]page=(\d+)>; rel="last"', link)
    out["commits"] = int(m.group(1)) if m else len(batch) if isinstance(batch, list) else None
    return out


def find_artifact(repo: str, token: str | None, *, branch: str | None = None, pr: int | None = None,
                  workflow: str = "", name: str = "") -> dict:
    head_sha = None
    if pr is not None:
        p = api_get(f"/repos/{_q(repo)}/pulls/{int(pr)}", token)
        if ((p.get("head") or {}).get("repo") or {}).get("full_name") != repo:
            raise GitHubError("This PR comes from a fork, so its builds aren't fetched. That would be outside code.")
        branch, head_sha = p["head"]["ref"], p["head"]["sha"]
    if not branch:
        raise GitHubError("need a branch or PR number")
    base = (f"/repos/{_q(repo)}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs"
            if workflow else f"/repos/{_q(repo)}/actions/runs")
    runs = api_get(base, token, branch=branch, status="success", per_page=20,
                   head_sha=head_sha).get("workflow_runs", [])
    for run in runs:
        # Only builds of code pushed to this repo itself (not a fork PR that
        # happens to use the same branch name).
        if (run.get("head_repository") or {}).get("full_name") != repo:
            continue
        if run.get("event") not in ("push", "pull_request", "workflow_dispatch"):
            continue
        arts = api_get(f"/repos/{_q(repo)}/actions/runs/{int(run['id'])}/artifacts", token,
                       per_page=100).get("artifacts", [])
        arts = [a for a in arts if not a.get("expired") and (not name or a.get("name") == name)]
        if arts:
            a = arts[0]
            return {"run_id": run["id"], "run_url": run.get("html_url"), "head_sha": run.get("head_sha"),
                    "branch": branch, "artifact_id": a["id"], "name": a["name"],
                    "size": a.get("size_in_bytes", 0)}
    raise GitHubError(f"no successful run with an unexpired artifact for {branch}")


def download_artifact(repo: str, token: str | None, artifact_id: int, max_bytes: int) -> str:
    """Download the artifact zip to a private temp file; return its path."""
    r = _request(f"{API}/repos/{_q(repo)}/actions/artifacts/{int(artifact_id)}/zip", token)
    loc = r.headers.get("Location") if r.getcode() in (301, 302, 303, 307, 308) else None
    if loc:
        r.close()
        u = urllib.parse.urlparse(loc)
        if u.scheme != "https" or not u.hostname:
            raise GitHubError("unexpected redirect")
        req = urllib.request.Request(loc, headers={"User-Agent": UA})  # no token here
        try:
            r = _opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            raise GitHubError(f"artifact download failed: {e.code}") from None
    fd, path = tempfile.mkstemp(prefix="cilly-artifact-", suffix=".zip")
    total = 0
    try:
        with os.fdopen(fd, "wb") as f, r:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise GitHubError("artifact larger than configured limit")
                f.write(chunk)
    except BaseException:
        os.unlink(path)
        raise
    return path


_SAFE_COMPONENT = re.compile(r"^[^\x00/\\]+$")


def safe_extract_zip(zip_path: str, dest_root: str, subdir: str, *, max_bytes: int, max_files: int) -> dict:
    """Extract into a *new* directory dest_root/subdir.  Never overwrites, never
    follows symlinks, rejects absolute/.. paths and symlink entries, strips
    executable bits, and leaves out git repositories (exports.git_left_out)."""
    from .exports import HEAD_PEEK, MAX_DEPTH, MAX_PATH, git_left_out, is_head_name, left_out_by
    if not os.path.isabs(dest_root):
        raise GitHubError("destination must be an absolute path")
    if not re.match(r"^[A-Za-z0-9._-]{1,200}$", subdir) or subdir in (".", ".."):
        raise GitHubError("bad subfolder name")
    dfd = os.open(dest_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(subdir, 0o755, dir_fd=dfd)
        out_fd = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    finally:
        os.close(dfd)
    files, total = [], 0
    _OD = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def open_dir(components: list[str], create: bool) -> int:
        """Walk from out_fd one component at a time (O_NOFOLLOW); caller closes."""
        fd = os.dup(out_fd)
        try:
            for c in components:
                if create:
                    try:
                        os.mkdir(c, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                nfd = os.open(c, _OD, dir_fd=fd)
                os.close(fd)
                fd = nfd
        except BaseException:
            os.close(fd)
            raise
        return fd

    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            if len(infos) > max_files:
                raise GitHubError("artifact has too many files")
            entries = []
            for zi in infos:
                name = zi.filename
                mode = (zi.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise GitHubError(f"artifact contains a symlink: {name!r}")
                if name.startswith("/") or "\\" in name or "\x00" in name or len(name.encode()) > MAX_PATH:
                    raise GitHubError(f"unsafe path in artifact: {name[:80]!r}")
                parts = [p for p in name.split("/") if p not in ("",)]
                if any(p in (".", "..") or not _SAFE_COMPONENT.match(p) for p in parts) or not parts or len(parts) > MAX_DEPTH:
                    raise GitHubError(f"unsafe path in artifact: {name!r}")
                entries.append((zi, parts, name.endswith("/")))
            left = git_left_out((parts, z.open(zi).read(HEAD_PEEK) if not is_dir and is_head_name(parts[-1]) else None)
                                for zi, parts, is_dir in entries)
            used = set()
            for zi, parts, is_dir in entries:
                skip = left_out_by(parts, left)
                if skip is not None:
                    used.add(skip)
                    continue
                if is_dir:
                    os.close(open_dir(parts, create=True))
                    continue
                total += zi.file_size
                if total > max_bytes:
                    raise GitHubError("artifact expands beyond the configured limit")
                pfd = open_dir(parts[:-1], create=True)
                try:
                    ffd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o644, dir_fd=pfd)
                finally:
                    os.close(pfd)
                written = 0
                with os.fdopen(ffd, "wb") as out, z.open(zi) as src:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > zi.file_size:
                            raise GitHubError("zip entry larger than declared")
                        out.write(chunk)
                files.append("/".join(parts))
    finally:
        os.close(out_fd)
    return {"path": os.path.join(dest_root, subdir), "files": files, "bytes": total, "left_out": len(used)}


def fetch_build(repo: str, token: str | None, dest_root: str, *, branch: str | None = None,
                pr: int | None = None, workflow: str = "", name: str = "",
                max_bytes: int, max_files: int) -> dict:
    art = find_artifact(repo, token, branch=branch, pr=pr, workflow=workflow, name=name)
    if art["size"] > max_bytes:
        raise GitHubError("artifact larger than configured limit")
    zpath = download_artifact(repo, token, art["artifact_id"], max_bytes)
    try:
        sub = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{repo.replace('/', '-')}-{art['branch']}-run{art['run_id']}")[:200]
        res = safe_extract_zip(zpath, dest_root, sub, max_bytes=max_bytes, max_files=max_files)
    finally:
        os.unlink(zpath)
    res.update(art)
    return res
