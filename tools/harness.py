"""Run the real panel against the fake QEMU (tests/fake_bin), for checking the UI in a browser.

    python3 tools/harness.py --seed     # sample agents; open http://127.0.0.1:8765/#t=testtoken
    python3 tools/harness.py --fresh    # no agents: the first-run setup
    python3 tools/harness.py --fresh --bare   # ...with no VM image yet, and bubblewrap blocked by AppArmor (VMs don't start)
    python3 tools/harness.py --demo     # the sample agents of the README's screenshots (tools/readme_shots.js)
    python3 tools/harness.py --port 0   # any free port (it prints the address)

Each run lives in its own temporary folder, cleaned up when it exits. No real VM starts and nothing
leaves this machine. Drive it with Playwright (the Browser toolchain has it).
"""
import atexit, json, os, shutil, signal, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
# Listen on the port before touching any files, so a second harness fails cleanly here instead of
# wiping a running one's folder.  The server gets its real handler once the panel is set up.
# Two started at the same moment can both be turned away, as each sees the other listening first.  So
# try again after a short random pause: one of them gets it, and the other is still turned away.
for attempt in range(4):
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), BaseHTTPRequestHandler)
        break
    except OSError:
        if attempt == 3:
            sys.exit(f"harness: 127.0.0.1:{PORT} is busy. Another harness is probably running, so stop it first.")
        time.sleep(0.05 + 0.25 * int.from_bytes(os.urandom(1)) / 255)
PORT = httpd.server_address[1]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = tempfile.mkdtemp(prefix="cilly-harness-")  # a fresh folder per run, so runs never clobber each other
atexit.register(shutil.rmtree, H, ignore_errors=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so kill cleans up too
RUN = os.path.join(H, "run"); os.makedirs(RUN)
FAKE = os.path.join(REPO, "tests", "fake_bin")
os.environ.update({"XDG_CONFIG_HOME": f"{H}/cfg", "XDG_DATA_HOME": f"{H}/data", "XDG_RUNTIME_DIR": RUN,
                   "PATH": FAKE + os.pathsep + os.environ["PATH"], "FAKE_REQUEST": "1", "FAKE_MEMOUT": "good", "FAKE_NOTES": "- The sync server needs SQLite 3.45 or newer, so check the version first.\n"})
os.makedirs(f"{H}/cfg/cilly")
DEMO = "--demo" in sys.argv
REPOS = ["lantern/website", "lantern/sync-server"] if DEMO else ["me/demo-repo", "me/plugin-lib", "me/sprites"]
json.dump({"tokens": {"bot": {"repos": REPOS, "secret": {}}}, "vm": {"use_bwrap": "off"},
           "mcp_servers": {"pixellab": {"type": "http", "url": "https://api.pixellab.ai/mcp", "headers": {"Authorization": "Bearer ${PIXELLAB_API_KEY}"}}},
           "speech": {"whisper_url": "http://gpu-box.local:8080"}, "research_base_image": "base-20261012-100000.qcow2",
           **({"remote_hosts": ["cilly.home"]} if DEMO else {"plugins": {"impeccable": {"marketplace": "pbakaus/impeccable"},
                                                                            "frontend-design": {"marketplace": "anthropics/claude-plugins-official"}}}),  # the phone in tools/readme_shots.js, as through an HTTPS proxy
           **({} if DEMO else {"model_servers": {"gpu": {"url": "http://gpu-box.local:11434", "api_key": False, "secret": {}},
                                                 "vast": {"url": "http://localhost:21434", "api_key": False, "secret": {}},  # vast is offline
                                                 "rented": {"url": "http://127.0.0.1:11434", "api_key": False, "secret": {}, "ssh_secret": {},
                                                            "ssh": {"host": "203.0.113.20", "port": 22, "user": "root", "route": "direct",  # over SSH, host key not confirmed yet
                                                                    "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBERERERERERERERERERERERERERERERERERERERERER cilly-rented"}}}})},
          open(f"{H}/cfg/cilly/config.json", "w"))
sys.path.insert(0, REPO)
from cilly import vm as V
from cilly.server import App, make_handler
BARE = "--bare" in sys.argv
if not BARE:
    open(os.path.join(V.images_dir(), "base-test.qcow2"), "w").close()
if "--seed" in sys.argv:  # an all-in-one image from before the rename and a slim one, for Settings → VM images (sparse files)
    for n, preset, tc, size, runner in (("base-20260801-100000", "full", ["java", "rust", "browser"], 6_300_000_000, None),
                                        ("base-20261012-100000", "slim", [], 1_900_000_000, "cilly")):
        with open(os.path.join(V.images_dir(), n + ".qcow2"), "wb") as f:
            f.truncate(size)
        json.dump({"toolchains": tc, "preset": preset, "built": n[5:9] + "-" + n[9:11] + "-" + n[11:13] + "T10:00:00Z",
                   **({"runner": runner} if runner else {})}, open(os.path.join(V.images_dir(), n + ".json"), "w"))
mock.patch.object(V, "detect_tools", lambda cfg: {"qemu": f"{FAKE}/qemu-system-x86_64", "qemu-img": f"{FAKE}/qemu-img",
    "bwrap": None, "bwrap_problem": {"why": "bwrap: setting up uid map: Permission denied", "kind": "apparmor", "cmd": "",
                                     "fix": "Ubuntu's AppArmor blocks the user namespaces it needs until bubblewrap has a profile. cilly's install.sh turns on Ubuntu's own profile for it."} if BARE else None,
    "use_bwrap": "auto" if BARE else "off", "virtiofsd": None, "xorriso": "/usr/bin/xorriso" if BARE else None, "kvm": True}).start()
from cilly import host as _HOST  # the same distribution everywhere the harness runs
mock.patch.object(_HOST, "os_release", lambda *a: {"ID": "ubuntu", "ID_LIKE": "debian", "PRETTY_NAME": "Ubuntu 24.04.3 LTS"}).start()
from cilly import github as _GH  # Verify Access With GitHub, without GitHub
mock.patch.object(_GH, "repo_permissions", lambda repo, tok: {"full_name": repo, "private": True,
    "permissions": {"push": repo != "me/plugin-lib", "pull": True}}).start()
mock.patch.object(_GH, "repo_overview", lambda repo, tok: {"push": repo != "me/plugin-lib", "commits": {"me/demo-repo": 214}.get(repo, 1),
    "created_at": "2026-10-14T05:00:00Z", "pushed_at": "2026-10-15T05:00:00Z"}).start()  # Attach repository's list
from cilly import models as _MS  # Local models, without a model server
def _list_models(url, key=None, timeout=20):
    if "21434" in url:
        raise _MS.ModelServerError("can't reach it: [Errno 111] Connection refused")
    return ["devstral:24b", "gpt-oss:20b", "qwen3-coder:30b"]
mock.patch.object(_MS, "list_models", _list_models).start()
mock.patch.object(_MS, "ollama_contexts", lambda url, key: {"qwen3-coder:30b": 4096}).start()  # loaded with Ollama's default
mock.patch.object(_MS, "test", lambda url, key, model: {"ok": True, "text": "OK", "warning": (
    "Ollama runs this model with only 4,096 tokens of context, which is too little for an agent. "
    "Set OLLAMA_CONTEXT_LENGTH to 32768 or more where Ollama runs, and restart it.")}).start()
if BARE:  # Build VM image: a pretend download and build, nothing leaves this machine
    from cilly import base_image as _BI, debian_image as _DI

    def _fetch(out, progress, **kw):
        print("downloading https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2", file=out)
        for pct in range(5, 101, 5):
            time.sleep(0.5)
            progress(pct * 4_000_000, 400_000_000)
            if pct % 10 == 0:
                print(f"  {pct}% ({pct * 4} of 400 MB)", file=out)
        return f"{H}/debian.qcow2"

    def _build(image, cfg, *, toolchains, preset, out, **kw):
        for line in ("booting build VM …", "cilly: installing agent CLIs", "flattening image …"):
            time.sleep(4)
            print(line, file=out)
        open(os.path.join(V.images_dir(), "base-20261016-100000.qcow2"), "w").close()
        return "base-20261016-100000.qcow2"
    mock.patch.object(_DI, "fetch", _fetch).start()
    mock.patch.object(_BI, "build", _build).start()
app = App(PORT)
app.token = "testtoken"
threading.Thread(target=app.idle_loop, daemon=True).start()  # Stop When Idle, as serve() starts it
if True:  # the repo pickers (setup, Settings → GitHub access), without GitHub
    from cilly import github as GH
    _api_get = GH.api_get
    mock.patch.object(GH, "api_get", lambda path, tok, **kw: {"login": "cillybox", "id": 123456789} if path == "/user"  # who the token is
                      else _api_get(path, tok, **kw)).start()
    mock.patch.object(GH, "list_repos", lambda tok: [
        {"full_name": "me/dotfiles", "private": False, "language": "Shell", "pushed_at": "2026-03-01T05:00:00Z", "permissions": {"admin": False, "push": False, "pull": True}},
        {"full_name": "me/demo-repo", "private": True, "language": "Java", "pushed_at": "2026-10-14T05:00:00Z", "permissions": {"admin": False, "push": True, "pull": True}},
        {"full_name": "me/plugin-lib", "private": True, "language": "Java", "pushed_at": "2026-10-12T05:00:00Z", "permissions": {"admin": False, "push": True, "pull": True}},
        {"full_name": "me/website", "private": False, "language": "TypeScript", "pushed_at": "2026-10-01T05:00:00Z", "permissions": {"admin": True, "push": True, "pull": True}},
    ]).start()
for k, v in () if "--fresh" in sys.argv else (("token:bot", "ghp_x"), ("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "sk-oat"), ("research:CLAUDE_CODE_OAUTH_TOKEN", "sk-oat-r"),
                                                     *((("model-ssh:rented", "b3BlbnNzaC1rZXktdjEAAAAA"),) if not DEMO else ())):
    app.secrets.set_pasted(k, v)
httpd.RequestHandlerClass = make_handler(app)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
def wait(c, t=15):
    end = time.time() + t
    while time.time() < end and not c(): time.sleep(0.1)
if "--seed" in sys.argv:
    r = app.api_create_agent({"name": "researcher", "profile": "research", "mode": "persistent"})
    c = app.api_create_agent({"name": "coder", "repo": "me/demo-repo", "mode": "persistent", "instructions": "Add a word count to the editor's status bar."})
    s = app.api_create_agent({"name": "sprite-maker", "repo": None, "mode": "persistent", "mcp": []})
    wait(lambda: app.manager.get(c["id"]).meta["state"] == "ready" and app.manager.get(r["id"]).meta["state"] == "ready")
    app.api_link(c["id"], {"research_agent": r["id"], "auto": False})
    app.api_agent_message(r["id"], {"instructions": "How do Markdown tables render in CommonMark and GitHub Flavored Markdown?"})
    app.api_agent_message(s["id"], {"instructions": "Make the sprites, ask me what you need"})  # it asks questions back
    w = app.api_create_agent({"name": "web-fixer", "repo": "me/plugin-lib", "mode": "memory", "plugins": ["impeccable", "frontend-design"]})
    app.manager.get(w["id"]).set(plugin_state={"impeccable": {"state": "installed", "at": "2026-10-18T09:00:00Z"},  # how its installs went
        "frontend-design": {"state": "needs_command", "sha256": "ab" * 32, "command": "npx -y @example/setup-frontend", "at": "2026-10-18T09:00:00Z"}})
    app.manager.get(s["id"]).set(context={"used": 152000, "window": 200000})  # a fuller conversation for the meter
    ca = app.manager.get(c["id"])
    def past(days, hm):  # a time on an earlier day, for the Tasks tab's history
        return time.strftime("%Y-%m-%dT", time.gmtime(time.time() - days * 86400)) + hm + ":00Z"
    ca.set(tasks=[
        {"id": "0a0b0c01", "state": "done", "agent_rc": 0, "created": past(2, "09:14"), "started": past(2, "09:14"), "finished": past(2, "09:31"),
         "branch": "agent/word-count", "pr": "https://github.com/me/demo-repo/pull/3", "instructions": "Look at how the editor counts words and write down what we need."},
        {"id": "0a0b0c02", "state": "done", "agent_rc": -9, "created": past(1, "16:02"), "started": past(1, "16:02"), "finished": past(1, "17:02"),
         "branch": ca.meta["branch"], "instructions": "Run the whole test suite on Node 18, 20, 22 and 24 and tell me which ones fail. "
         "Start with the oldest one, keep the logs of each run in ~/outbox, and stop at the first version where the editor tests pass, then explain why the older ones failed."},
        {"id": "0a0b0c03", "state": "failed", "agent_rc": 1, "error": "claude exited with 1 (see the agent log)", "created": past(1, "17:40"),
         "started": past(1, "17:40"), "finished": past(1, "17:44"), "branch": ca.meta["branch"], "instructions": "Fix the flaky word count test."},
    ] + ca.meta["tasks"])
    with open(ca.paths.egress_log, "a") as f:
        for i, h in enumerate(["registry.npmjs.org", "registry.npmjs.org", "registry.npmjs.org", "static.crates.io", "evil.example.com"]):
            f.write(json.dumps({"ts": f"2026-10-14T09:0{i}:00Z", "action": "deny", "host": h, "port": 443, "reason": "not allowlisted"}) + "\n")
        f.write(json.dumps({"ts": "2026-10-14T09:06:00Z", "action": "deny", "host": "github.com", "port": 22, "code": "port",
                            "reason": "port 22 isn't allowed for github.com"}) + "\n")
        f.write(json.dumps({"ts": "2026-10-14T09:07:00Z", "action": "deny", "host": "sync.example.net", "port": 8080, "code": "host",
                            "reason": "host not in allowlist"}) + "\n")
    with open(app.manager.get(r["id"]).paths.egress_log, "a") as f:
        f.write(json.dumps({"ts": "2026-10-14T09:08:00Z", "action": "deny", "host": "site.example", "port": 8080, "code": "open_web_port",
                            "reason": "port 8080: the open web is only reachable on 443"}) + "\n")
    p = app.api_project_create({"name": "Lantern"})  # the agent list's project group and the project page
    app.api_project_update(p["id"], {"instructions": "A notes app in TypeScript.\nUse pnpm.\nNo new dependencies without asking.", "allow_text": "crates.io\nstatic.crates.io",
                                     "repos": ["me/demo-repo", "me/plugin-lib"], "research_agent": r["id"]})
    app.api_project_note(p["id"], None, "add", {"text": "Run pnpm test before pushing."})
    for a in (c, w):
        app.api_agent_organize(a["id"], {"project": p["id"]})
    app.projects.propose(p["id"], c["id"], ["The test server needs Redis on localhost:6379; use the embedded-redis fixture."])
    app.api_agent_organize(r["id"], {"pinned": True})
    print("seeded", flush=True)
if DEMO:  # the README's screenshots: two coding agents on a notes app's website and sync server, a research agent, a stopped one
    from cilly import handoff as _H
    def ago(minutes):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))
    r = app.api_create_agent({"name": "researcher", "profile": "research", "mode": "persistent"})
    shop = app.api_create_agent({"name": "sync-server", "repo": "lantern/sync-server", "mode": "persistent"})
    web = app.api_create_agent({"name": "website", "repo": "lantern/website", "mode": "memory"})
    scratch = app.api_create_agent({"name": "scratch", "repo": None, "mode": "ephemeral"})
    wait(lambda: all(app.manager.get(a["id"]).meta["state"] == "ready" for a in (r, shop, web, scratch)))
    app.api_agent_action(scratch["id"], "stop", {})
    app.api_link(shop["id"], {"research_agent": r["id"], "auto": False})
    p = app.api_project_create({"name": "Lantern"})
    app.api_project_update(p["id"], {"instructions": "A notes app: an Astro website and a sync server in Rust.\nRun the tests before pushing.",
                                     "repos": ["lantern/website", "lantern/sync-server"], "research_agent": r["id"]})
    for a in (shop, web):
        app.api_agent_organize(a["id"], {"project": p["id"]})
    app.api_agent_organize(r["id"], {"pinned": True})
    ra, sa, wa = (app.manager.get(a["id"]) for a in (r, shop, web))
    for ag in (ra, sa, wa):  # started before their tasks, in a few seconds as a real VM does
        ag.set(boot_started=time.time() - 40 * 60, boot_secs=6)
    app.api_setup({"checklist_hidden": True})  # New agent without the Finish setting up list
    selftest = {n: True for n in ["direct TCP to 1.1.1.1:443 is blocked", "direct IPv6 to [2606:4700:4700::1111]:443 is blocked",
        "host via 10.0.2.2:22 is blocked", "panel port 8765 on the host (listening) is unreachable via 10.0.2.2",
        "DNS lookup of example.com is blocked", "UDP DNS to 10.0.2.3 gets no answer", "proxy allows api.github.com:443",
        "proxy denies a host that isn't allowed (selftest.invalid:443)", "proxy denies IP literal 10.0.2.2:443",
        "proxy denies port 22 (api.github.com:22)", "no nested virtualization (VMX/SVM) in the VM",
        "agent can't gain privileges (no_new_privs)", "no setuid/setgid programs the agent can run"]}
    # website: fixed the mobile menu and took a screenshot (the picture is FAKE_OUTBOX_IMAGE, when it's set)
    img = os.path.basename(os.environ.get("FAKE_OUTBOX_IMAGE", ""))
    wa.set(selftest=selftest, summary="Mobile menu fixed, screenshot in the outbox, PR open", tasks=[
        {"id": "5e1a0c01", "state": "done", "agent_rc": 0, "created": ago(9), "started": ago(9), "finished": ago(6), "branch": wa.meta["branch"],
         "pr": "https://github.com/lantern/website/pull/31", "images": [img] if img else [],
         "instructions": "The mobile menu covers the hero text on small screens. Fix it and send me a screenshot at 390 px wide."}])
    app.manager._on_chat(wa, {"task": "5e1a0c01", "events": [
        {"k": "text", "text": "Let me look at how the nav is styled."},
        {"k": "tool", "id": "w1", "tool": "Read", "verb": "Read", "target": "src/components/Nav.astro"},
        {"k": "result", "id": "w1", "ok": True, "summary": "48 lines"},
        {"k": "tool", "id": "w2", "tool": "Read", "verb": "Read", "target": "src/styles/global.css"},
        {"k": "result", "id": "w2", "ok": True, "summary": "212 lines"},
        {"k": "text", "text": "The nav is `position: fixed`, but nothing reserves space for it, so the first line of the heading sits under it "
                              "on narrow screens. Making it sticky keeps it in the layout."},
        {"k": "diff", "id": "w3", "file": "src/styles/global.css", "add": 2, "del": 3, "lines": [
            ["@", "@ -14,10 +14,9 @@"], [" ", "nav {"], ["-", "  position: fixed;"], ["+", "  position: sticky;"], [" ", "  top: 0;"],
            [" ", "  z-index: 20;"], [" ", "}"], [" ", ""], [" ", ".hero {"], ["-", "  margin-top: 0;"], ["-", "  padding: 40px 22px;"],
            ["+", "  padding: 56px 22px 40px;"], [" ", "}"]]},
        {"k": "tool", "id": "w4", "tool": "Bash", "verb": "Ran", "target": "npm run build"},
        {"k": "result", "id": "w4", "ok": True, "summary": "6 pages built in 1.9 s"},
        {"k": "tool", "id": "w5", "tool": "Bash", "verb": "Ran", "target": "npx playwright screenshot --viewport-size=390,844 http://localhost:4321 ~/outbox/home-mobile.png"},
        {"k": "result", "id": "w5", "ok": True, "summary": "Saved ~/outbox/home-mobile.png"},
        {"k": "text", "text": "Fixed. The heading now starts below the nav at 320, 390 and 768 px wide.\n\n"
                              "- `position: fixed` became `position: sticky`\n- the hero got a little more top padding instead of the old margin\n\n"
                              "The screenshot is in your outbox, and the change is in a draft pull request."},
        {"k": "done", "ok": True, "turns": 9, "ms": 168000}]})
    with open(wa.paths.egress_log, "a") as f:
        for m, h in ((5, "fonts.googleapis.com"), (5, "fonts.gstatic.com"), (4, "fonts.googleapis.com")):
            f.write(json.dumps({"ts": ago(m), "action": "deny", "host": h, "port": 443, "reason": "not allowlisted"}) + "\n")
    # sync-server: asked the research agent two questions; the first one has come back
    sa.set(selftest=selftest, summary="Waiting for research on streaming zips", tasks=[
        {"id": "5e1a0c02", "state": "done", "agent_rc": 0, "created": ago(30), "started": ago(30), "finished": ago(24), "branch": sa.meta["branch"],
         "instructions": "Add a GET /export endpoint that sends all of a user's notes as a zip of Markdown files. Write tests for it."}])
    app.manager._on_chat(sa, {"task": "5e1a0c02", "events": [
        {"k": "tool", "id": "s1", "tool": "Read", "verb": "Read", "target": "src/routes/notes.rs"},
        {"k": "result", "id": "s1", "ok": True, "summary": "142 lines"},
        {"k": "text", "text": "Some users have thousands of notes, so building the whole zip in memory before sending it could use a lot of RAM. "
                              "I asked the research agent how to stream it from axum 0.8 before I write the endpoint."},
        {"k": "done", "ok": True, "turns": 4, "ms": 41000}]})
    zip_q = ("How should an axum 0.8 handler stream a zip file to the client without building it in memory first? "
             "Which crate does that with tokio today, and how do errors halfway through reach the client?")
    app.on_requests(sa, {"task": "5e1a0c02", "requests": [
        {"name": "axum-zip-streaming", "text": zip_q},
        {"name": "sqlite-wal", "text": "Is SQLite in WAL mode enough for a sync server with a few hundred users, or should we move to Postgres? "
                                       "What do people run into?"}]})
    rid = next(q["id"] for q in sa.meta["requests"] if q["name"] == "axum-zip-streaming")
    ra.set(tasks=[{"id": "5e1a0c03", "state": "done", "agent_rc": 0, "created": ago(22), "started": ago(22), "finished": ago(18),
                   "instructions": zip_q, "for_agent": sa.id, "request": rid, "request_name": "axum-zip-streaming"}])
    app.manager._on_chat(ra, {"task": "5e1a0c03", "events": [
        {"k": "tool", "id": "r1", "tool": "WebFetch", "verb": "Fetched", "target": "https://docs.rs/axum/latest/axum/body/struct.Body.html"},
        {"k": "result", "id": "r1", "ok": True, "summary": "Body in axum::body - Rust"},
        {"k": "tool", "id": "r2", "tool": "WebFetch", "verb": "Fetched", "target": "https://docs.rs/async_zip/latest/async_zip/"},
        {"k": "result", "id": "r2", "ok": True, "summary": "async_zip - Rust"},
        {"k": "text", "text": "I wrote the answer to ~/outbox/findings.md."},
        {"k": "done", "ok": True, "turns": 6, "ms": 96000}]})
    app.on_outbox(ra, {"task": "5e1a0c03", "files": [{"path": "findings.md", "content":
        "# Streaming a zip from an axum 0.8 handler\n\nWrite the archive while you send it, with the `async_zip` crate and its tokio support, "
        "so a large export never sits in memory.\n\n## How\n\n"
        "- Make a pipe with `tokio::io::duplex`, and write the archive into one end with `ZipFileWriter` in a spawned task\n"
        "- Turn the other end into the body with `Body::from_stream(ReaderStream::new(reader))`\n"
        "- Set `Content-Type: application/zip` and a `Content-Disposition` with the file name\n\n## Errors halfway through\n\n"
        "Once the first bytes are out, the status code can't change any more. Check that the user may export before you start, "
        "and if writing fails later, end the stream with an error so the client sees a broken download, not a short zip.\n\n"
        "Sources: the axum and async_zip documentation."}]})
    with app._handoff_lock:  # the findings came in before the website's blocked fonts, so the inbox lists those first
        for d in _H.list_all(ra.paths.vm_dir):
            d["created"] = ago(18)
            _H.save_meta(ra.paths.vm_dir, d)
    print("demo", flush=True)
print(f"READY http://127.0.0.1:{PORT}/#t=testtoken", flush=True)
while True: time.sleep(3600)
