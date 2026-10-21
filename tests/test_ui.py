"""The UI's own logic, run in Node against a tiny stand-in for the DOM (the panel itself needs no Node)."""

import json
import os
import re
import shlex
import shutil
import subprocess
import unittest

PHONE_MQ = "(max-width: 640px), (max-height: 500px) and (pointer: coarse)"  # keep in step with static/style.css and PHONE_QUERY in app.js

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cilly", "static")
FILES = ["app.js", "agent.js", "ask.js", "inbox.js", "viewer.js", "projects.js", "mobile.js", "setup.js", "voice.js", "pages.js"]

FAKE_DOM = r"""
class Node { constructor(tag, text) { this.tag = tag; this.text = text || ""; this.kids = []; this.attrs = {}; this.className = "";
    this.style = {}; this.redraws = 0; this.ls = {}; this.parent = null;
    this.classList = { toggle() {}, add() {}, remove() {}, contains: (c) => this.className.split(" ").includes(c) }; }
  adopt(k) { for (const n of k) if (n instanceof Node) n.parent = this; return k; }
  append(...k) { this.kids.push(...this.adopt(k)); } replaceChildren(...k) { this.kids = this.adopt(k); this.redraws++; }
  prepend(...k) { this.kids.unshift(...this.adopt(k)); } get nodeType() { return this.tag === "#text" ? 3 : 1; }
  replaceWith(n) { const p = this.parent; if (p) { p.kids[p.kids.indexOf(this)] = n; n.parent = p; } this.parent = null; }
  contains(n) { return n === this || this.kids.some((k) => k instanceof Node && k.contains(n)); }
  get isConnected() { return true; }
  get children() { return this.kids.filter((k) => k instanceof Node && k.tag !== "#text"); } get firstElementChild() { return this.children[0] || null; }
  get dataset() { return Object.fromEntries(Object.entries(this.attrs).filter(([k]) => k.startsWith("data-")).map(([k, v]) => [k.slice(5), v])); } matches() { return false; }
  addEventListener(t, f) { (this.ls[t] ||= []).push(f); }
  fire(t, ev) { const e = Object.assign({ target: this, preventDefault() { e.prevented = true; }, stopPropagation() { e.stopped = true; } }, ev); for (const f of this.ls[t] || []) f(e); return e; }
  setAttribute(k, v) { this.attrs[k] = v; } toggleAttribute() {} remove() {} focus() {} select() {} blur() { this.fire("blur"); }
  querySelectorAll(sel) { return /^\.[\w-]+$/.test(sel) ? this.kids.flatMap((k) => k instanceof Node ? [...(k.classList.contains(sel.slice(1)) ? [k] : []), ...k.querySelectorAll(sel)] : []) : []; }
  set textContent(t) { this.kids = [new Node("#text", t)]; } get textContent() { return this.tag === "#text" ? this.text : this.kids.map((k) => k.textContent).join(""); } }
const IDS = {};
const store = () => ({ getItem: () => null, setItem() {}, removeItem() {} });
Object.assign(globalThis, { Node, localStorage: store(), sessionStorage: store(), location: { hash: "", pathname: "/" },
  history: { replaceState() {}, pushState() {}, back() {} }, window: { addEventListener() {}, matchMedia: () => ({ matches: false }), innerWidth: 1280 },
  document: { ls: {}, addEventListener(t, f) { (this.ls[t] ||= []).push(f); }, removeEventListener(t, f) { this.ls[t] = (this.ls[t] || []).filter((x) => x !== f); },
    createElement: (t) => new Node(t), createElementNS: (ns, t) => new Node(t), createTextNode: (t) => new Node("#text", t),
    getElementById: (id) => IDS[id] || (IDS[id] = new Node("div")), querySelector: () => null, querySelectorAll: () => [], body: new Node("body") } });
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });  // Node 21+ has a read-only one
const find = (n, cls) => n.className.split(" ").includes(cls) ? n : n.kids.map((k) => find(k, cls)).find(Boolean);
"""


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UiTests(unittest.TestCase):
    def run_js(self, body: str, prelude: str = ""):
        src = FAKE_DOM + prelude + "\n" + "".join(read(os.path.join(STATIC, f)) + "\n" for f in FILES)
        src += "\n;(async () => {\n" + body + "\n})();"
        r = subprocess.run(["node", "-"], input=src, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_enter_sends_only_without_a_touch_keyboard(self):
        got = self.run_js("""
          const key = (extra) => Object.assign({ key: "Enter", shiftKey: false, isComposing: false }, extra);
          const at = (coarse) => { window.matchMedia = (q) => ({ matches: coarse && q.includes("coarse") }); };
          at(false);
          const desk = [enterSends(key()), enterSends(key({ shiftKey: true })), enterSends(key({ isComposing: true })), enterSends(key({ key: "a" }))];
          at(true);
          console.log(JSON.stringify([desk, enterSends(key())]));
        """)
        self.assertEqual(got, [[True, False, False, False], False])

    def test_a_paired_device_gets_no_dead_ends_in_the_mcp_and_plugin_controls(self):
        got = self.run_js("""
          const calls = [];
          globalThis.api = async (m, u) => { calls.push(u); return { secrets: {} }; };
          const a = { id: "a1", name: "x", mcp: [], plugins: [] };
          const run = (role) => { STATE = { role, mcp_servers: { s: { type: "http", hosts: [], secrets: ["K"] } }, plugins: {} }; AV = { id: "a1" };
            const box = mcpControls(a); const pop = mcpPop(a); return [box.map((n) => n.textContent).join("|"), pop.textContent]; };
          const paired = run("device"), owner = run("owner");
          console.log(JSON.stringify([calls.length, paired[0].includes("Keys are set on your PC"), paired[1].includes("/plugin add"), paired[1].includes("Add plugins on your PC"),
            owner[1].includes("/plugin add")]));
        """)
        self.assertEqual(got, [1, True, False, True, True])  # only the owner's call asks for settings

    def test_a_phone_header_leads_where_the_desktop_pill_does(self):
        got = self.run_js(r"""
          const a = { id: "a1", name: "x", state: "ready", running: true, repo: "me/app", branch: "agent/1", pr: "https://github.com/me/app/pull/7",
            context: { used: 95000, window: 100000 } };
          STATE = { role: "owner", agents_list: [a] };
          INBOX.items = [];
          const [branch, pr, ctx] = phoneMeta(a, () => a, /\/pull\/(\d+)$/.exec(a.pr), "agent/1");
          const calm = phoneMeta({ ...a, context: { used: 1, window: 100 }, pr: "" }, () => a, null, "");
          console.log(JSON.stringify([branch.textContent, pr.textContent, ctx.className.includes("err"), ctx.textContent, calm.filter(Boolean).length,
            phoneState(a, () => a).tag, find(phoneState(a, () => a), "m-state") !== undefined]));
        """)
        self.assertEqual(got, ["agent/1", "PR #7", True, "Context 95%", 0, "button", True])

    def test_a_phone_ask_strip_has_deny_and_approve_like_the_desktop(self):
        got = self.run_js("""
          const a = { id: "a1", name: "x", state: "ready", asks: [] };
          STATE = { role: "owner", agents_list: [a], egress_ports: [443] };
          INBOX.items = [{ kind: "research", key: "req:a1:r1", agent: "a1", text: "How?", research_agent: "r", title: "How?" }];
          window.matchMedia = (q) => ({ matches: q.includes("640") });
          AV = { id: "a1", c: { asks: new Node("div") } };
          renderAsks(a);
          const search = (n) => n.className?.split(" ").includes("ask-acts") ? n : (n.kids || []).map(search).find(Boolean);
          console.log(JSON.stringify(search(AV.c.asks).kids.map((b) => b.textContent)));
        """)
        self.assertEqual(got, ["Deny", "Approve"])

    def test_the_started_line_opens_its_breakdown_on_a_tap(self):
        got = self.run_js("""
          AV = {};
          const a = { boot_secs: 6, boot_times: [["Disk", 0.4], ["Self-test", 3.1]] };
          const line = startedLine(a), btn = find(line, "link"), more = find(line, "boot-times");
          const before = more.hidden;
          btn.fire("click");
          const opened = more.hidden;
          btn.fire("click");
          console.log(JSON.stringify([before, opened, more.hidden, more.textContent, startedLine({ boot_secs: 2 }).kids.length]));
        """)
        self.assertEqual(got, [True, False, True, "Disk 0.4 s · Self-test 3.1 s", 1])

    def test_a_phone_keeps_the_focused_field_in_view(self):
        got = self.run_js("""
          const moved = [], field = { matches: (q) => q.includes("input"), scrollIntoView: (o) => moved.push(o.block) };
          document.activeElement = field;
          window.matchMedia = () => ({ matches: false }); keepFieldInView();
          window.matchMedia = () => ({ matches: true }); keepFieldInView();
          document.activeElement = { matches: () => false, scrollIntoView: () => moved.push("button") }; keepFieldInView();
          console.log(JSON.stringify(moved));
        """)
        self.assertEqual(got, ["nearest"])  # a phone only, and only for something you type into

    def test_image_viewer_steps_around_fits_and_keeps_the_picture_on_screen(self):
        got = self.run_js("""
          console.log(JSON.stringify([lbStep(0, -1, 3), lbStep(2, 1, 3), lbStep(1, 1, 3),
            lbFit({ w: 800, h: 600 }, 1600, 900), lbFit({ w: 800, h: 600 }, 16, 16), lbFit({ w: 800, h: 600 }, 400, 300),
            lbPan(500, 1000, 800), lbPan(-500, 1000, 800), lbPan(40, 400, 800), lbPan(-90, 1000, 800)]));
        """)
        self.assertEqual(got, [2, 0, 2, 0.5, 8, 2, 100, -100, 0, -90])

    def test_pictures_in_the_chat_know_their_place_in_the_row(self):
        got = self.run_js("""
          const a = { id: "a1", running: false, state: "stopped" };
          const b = chatThumb(a, "outbox", "shots/one.png");
          console.log(JSON.stringify([b.attrs["data-agent"], b.attrs["data-what"], b.attrs["data-path"], b.className]));
        """)
        self.assertEqual(got, ["a1", "outbox", "shots/one.png", "chat-thumb"])

    def test_a_toast_splits_into_a_title_and_its_detail(self):
        got = self.run_js("""
          console.log(JSON.stringify([toastParts("Saved. Agents get it at their next task."), toastParts("Added example.com. Applies live."),
            toastParts("Uploading a.png…"), toastParts("Couldn't reach the server!"), toastParts("Done.")]));
        """)
        self.assertEqual(got, [["Saved", "Agents get it at their next task."], ["Added example.com", "Applies live."], ["Uploading a.png…", ""],
                               ["Couldn't reach the server!", ""], ["Done.", ""]])

    def test_an_undo_toast_has_a_button_that_runs_its_action(self):
        got = self.run_js("""
          const out = [];
          toast("Archived x", false, { label: "Undo", fn: () => out.push("undone") });
          const act = find(document.getElementById("toasts").kids[0], "toast-act");
          out.push(!!act, act.textContent);
          act.fire("click", {});
          toast("Pinned", false);
          out.push(!!find(document.getElementById("toasts").kids[1], "toast-act"));
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [True, "Undo", "undone", False])

    def test_edge_swipes_need_the_edge_and_a_mostly_horizontal_pull(self):
        got = self.run_js("""
          console.log(JSON.stringify([swipedIn(5, 300, 100, 320, 200), swipedIn(40, 300, 140, 300, 200), swipedIn(5, 300, 50, 300, 200),
            swipedIn(5, 300, 100, 420, 200), swipedIn(5, 300, 100, 300, 900),
            swipedOut(200, 300, 100, 310, 200), swipedOut(200, 300, 160, 300, 200), swipedOut(200, 300, 100, 450, 200)]));
        """)
        self.assertEqual(got, [True, False, False, False, False, True, False, False])

    def test_a_screen_comes_back_at_the_place_you_left_it(self):
        got = self.run_js("""
          const page = { scrollTop: 0 };
          $("main").querySelector = () => page;
          Object.assign(UI, { view: "agents", agentId: null, projectId: null, sec: null });
          page.scrollTop = 420; saveScroll();
          page.scrollTop = 0; restoreScroll();
          const back = page.scrollTop;
          UI.view = "project"; UI.projectId = "p1"; page.scrollTop = 0; restoreScroll();
          console.log(JSON.stringify([back, page.scrollTop, scrollKey()]));
        """)
        self.assertEqual(got, [420, 0, "project|p1|"])

    def test_the_line_under_an_agent(self):
        got = self.run_js("""
          const a = (extra) => Object.assign({ id: "a1", name: "x", state: "ready", tasks: [], repo: "me/app" }, extra);
          const msg = (instructions) => a({ tasks: [{ instructions }] });
          console.log(JSON.stringify([
            agentLine(a({ summary: "Added per-site ports, tests pass, PR open", tasks: [{ instructions: "# Old" }] })),
            agentLine(msg("# PackSquash with Nexo: sha\\nmore")), agentLine(msg("\\n> **Fix** the `build`")),
            agentLine(msg("- see [the docs](https://example.com) ## now")), agentLine(a({})),
            agentLine(a({ state: "failed", error: "qemu died", summary: "s" })),
            agentLine(msg("*Fix* the _snake_case_ thing")), agentLine(msg("a\u200b\u202eb\u0007c\td")),
            agentLine(msg("x".repeat(100))), agentLine(msg("😀".repeat(90))).length,
            ownLine(a({})), ownLine(a({ profile: "research", repo: null })), ownLine(msg("Fix it"))]));
        """)
        self.assertEqual(got, ["Added per-site ports, tests pass, PR open", "PackSquash with Nexo: sha", "Fix the build",
                               "see the docs ## now", "me/app", "Failed: qemu died", "Fix the snakecase thing", "ab c d",
                               "x" * 79 + "…", 79 * 2 + 1, "", "", "Fix it"])  # the header shows where it works already

    def test_the_started_line_says_where_the_time_went(self):
        got = self.run_js("""
          console.log(JSON.stringify([
            bootTimesText({ boot_times: [["Disk", 0.4], ["Self-test", 3.12], ["bad", "x"], "junk", [5, 1]] }),
            bootTimesText({ boot_times: null }), bootTimesText({})]));
        """)
        self.assertEqual(got, ["Disk 0.4 s · Self-test 3.1 s", "", ""])

    def test_a_move_says_how_far_it_is(self):
        got = self.run_js("""
          const a = (move) => ({ id: "a1", status: { key: "starting" }, move });
          console.log(JSON.stringify([statusText(a({ step: 1 })), statusText(a({ step: 2 })), statusShort(a({ step: 1 })),
                                      statusText({ status: { key: "stopped" } })]));
        """)
        self.assertEqual(got, ["Moving to new image (1 of 2)", "Moving to new image (2 of 2)", "Moving…", "Stopped"])
        got = self.run_js("""
          const a = (move) => ({ id: "a1", state: "booting", tasks: [], move });
          console.log(JSON.stringify([ownLine(a({ step: 1 })), ownLine(a({ step: 2, to: "base-20261017.qcow2" }))]));
        """)
        self.assertEqual(got, ["Packing its home folder", "Unpacking its home folder on base-20261017"])

    def test_status_copy(self):
        got = self.run_js("""
          STATE = { policy_version: 3 };
          const a = (extra) => Object.assign({ id: "a1", name: "x", state: "ready", tasks: [], mode: "ephemeral", cpus: 2, memory_mb: 4096 }, extra);
          const boot = a({ state: "booting", boot_phase: "boot", boot_started: Date.now() / 1000 - 4 });
          const t = (extra) => Object.assign({ id: "t1", state: "failed", created: "2026-10-14T05:00:00Z" }, extra);
          const long = "x".repeat(200);
          const at = (extra, tasks) => startedLineAt(a(Object.assign({ running: true, boot_started: 1000.7, boot_secs: 6 }, extra)), tasks);
          globalThis.renderMarkdown = (text) => text;
          Object.defineProperty(Node.prototype, "dataset", { get() { return this.ds || (this.ds = {}); } });
          const end = (task) => taskBlock(a({ mode: "persistent" }), Object.assign({ instructions: "hi" }, task),
            { evs: [], reqs: [], dels: [], denied: [], asks: [] }).map((n) => n.textContent);
          const old = t({ state: "done", started: "1970-01-01T00:10:00Z" }), later = t({ state: "done", started: "1970-01-01T00:16:40Z" });  // the boot's second
          console.log(JSON.stringify([
            agentLine(boot), bootCard(boot).textContent.includes("Step 3 of 5, 4 s"),
            [fmtSecs(6), fmtSecs(125), fmtSecs(3700)],
            [taskStateText(t({ error: "tests need a Redis server" })), taskStateText(t({ agent_rc: 1 })), taskStateText(t({})),
             taskStateText(t({ error: "first\\nsecond" })), taskStateText(t({ error: long })).length, taskStateText(t({ state: "done", agent_rc: 0 }))],
            [errorBelow(t({ error: "short" })), errorBelow(t({ error: "first\\nsecond" })), errorBelow(t({ error: long })),
             errorBelow(t({ state: "interrupted", error: "x" }))],
            [at({}, [old, later]), at({}, [old]), at({}, [old, t({ state: "queued" })]), at({ running: false }, [old]),
             at({ boot_secs: undefined }, [old]), at({ state: "booting" }, [])],
            [end(t({ error: "tests need a Redis server", finished: "2020-10-14T05:12:00Z" })), end(t({ state: "done", finished: "2020-10-14T05:12:00Z" }))],
            [taskStateText(t({ agent_rc: -9 })), taskStateText(t({ state: "done", agent_rc: -9 })),
             taskStateText(t({ state: "interrupted", agent_rc: -9, by_you: true })), end(t({ state: "done", agent_rc: -9, finished: "2020-10-14T05:12:00Z" }))],
            [taskStateText(t({ state: "interrupted", agent_rc: -9, timed_out: true })),
             end(t({ state: "interrupted", timed_out: true, finished: "2020-10-14T05:12:00Z" }))],
            end(t({ state: "interrupted", time_limit_stop: true, created: "2020-10-14T05:12:00Z" })),
          ]));
        """)
        self.assertEqual(got, [
            "Starting VM (3 of 5)", True, ["6 s", "2 min 5 s", "1 h 1 min"],
            ["Failed: tests need a Redis server", "Failed: exit code 1", "Failed", "Failed: first", len("Failed: ") + 160, "Done"],
            [False, True, True, True],
            [1, 1, 1, -1, -1, -1],
            [["hi", "Failed: tests need a Redis server · Oct 14 05:12"], ["hi", "Task t1 done at Oct 14 05:12"]],
            ["Stopped (killed: time limit or out of memory)"] * 2 + ["Interrupted (exit code -9)",
             ["hi", "Stopped (killed: time limit or out of memory) · Oct 14 05:12"]],
            ["Stopped: the time limit ended it", ["hi", "Stopped: the time limit ended it · Oct 14 05:12"]],
            ["hi", "Not sent: the time limit stopped the VM · Oct 14 05:12"]])

    def test_task_status_words_and_groups(self):
        got = self.run_js("""
          const s = (t) => taskStatus(Object.assign({ state: "done" }, t));
          const words = [s({ agent_rc: 0 }), s({}), s({ agent_rc: -9 }), s({ state: "failed", agent_rc: -9 }), s({ state: "failed", error: "push refused" }),
            s({ agent_rc: 2 }), s({ state: "failed" }), s({ state: "interrupted", by_you: true }), s({ state: "queued" }), s({ state: "cancelled" }),
            s({ state: "interrupted", timed_out: true }), s({ state: "interrupted", agent_rc: -9, timed_out: true }),
            s({ state: "interrupted", time_limit_stop: true }), s({ state: "interrupted" })];
          const t = (id, state, created) => ({ id, state, created, instructions: id });
          const a = { queue: ["q2", "q1"], tasks: [t("old", "done", "2026-10-12T08:00:00Z"), t("y1", "done", "2026-10-13T08:00:00Z"),
            t("y2", "failed", "2026-10-13T09:00:00Z"), t("run", "running", "2026-10-14T05:00:00Z"), t("q1", "queued", "2026-10-14T05:01:00Z"),
            t("q2", "queued", "2026-10-14T05:02:00Z")] };
          const g = taskGroups(a), now = new Date("2026-10-14T12:00:00Z");
          console.log(JSON.stringify({ words, running: g.running.map((x) => x.id), queued: g.queued.map((x) => x.id),
            days: g.days.map((d) => [dayLabel(d.day, now), d.tasks.map((x) => x.id)]), older: dayLabel("2025-12-31", now) }));
        """)
        self.assertEqual(got["words"], [["Done", "ok"], ["Done", "ok"], ["Stopped (killed: time limit or out of memory)", "warn"],
                                        ["Stopped (killed: time limit or out of memory)", "warn"], ["Failed: push refused", "err"],
                                        ["Failed (exit code 2)", "err"], ["Failed", "err"], ["Stopped by you", "muted"],
                                        ["Queued", "muted"], ["Cancelled", "muted"],
                                        ["Stopped: the time limit ended it", "warn"], ["Stopped: the time limit ended it", "warn"],
                                        ["Not sent: the time limit stopped the VM", "warn"], ["Cut off when the VM stopped", "muted"]])
        self.assertEqual((got["running"], got["queued"]), (["run"], ["q2", "q1"]))
        self.assertEqual(got["days"], [["Yesterday", ["y2", "y1"]], ["Mon, Oct 12", ["old"]]])
        self.assertEqual(got["older"], "Wed, Dec 31, 2025")

    def test_file_list_order_ages_and_counts(self):
        got = self.run_js("""
          const e = (name, kind, size, mtime) => ({ name, kind, size, mtime });
          const list = [e("b.txt", "file", 10, 300), e("A.md", "file", 500, 100), e("src", "dir", 0, 50), e("a10.png", "file", 5, 200),
            e("a9.png", "file", 5, 400), e("docs", "dir", 0, 900)];
          const names = (key, dir) => sortEntries(list, { key, dir }).map((x) => x.name);
          const now = Date.parse("2026-10-14T12:00:00Z"), at = (s) => Date.parse(s) / 1000;
          console.log(JSON.stringify({ name: names("name", 1), nameDown: names("name", -1), size: names("size", -1), added: names("added", -1),
            ages: [fileAge(at("2026-10-14T05:03:00Z"), now), fileAge(at("2026-10-11T05:03:00Z"), now), fileAge(at("2026-09-20T00:00:00Z"), now),
              fileAge(at("2026-05-01T00:00:00Z"), now), fileAge(at("2023-01-01T00:00:00Z"), now), fileAge(0, now)],
            counts: [folderCount(list), folderCount([list[0]]), folderCount([])] }));
        """)
        self.assertEqual(got["name"], ["docs", "src", "A.md", "a9.png", "a10.png", "b.txt"])  # folders first, A to Z, numbers in order
        self.assertEqual(got["nameDown"], ["src", "docs", "b.txt", "a10.png", "a9.png", "A.md"])
        self.assertEqual(got["size"], ["docs", "src", "A.md", "b.txt", "a10.png", "a9.png"])
        self.assertEqual(got["added"], ["docs", "src", "a9.png", "b.txt", "a10.png", "A.md"])
        self.assertEqual(got["ages"], ["05:03", "3 d", "3 w", "5 mo", "3 y", ""])
        self.assertEqual(got["counts"], ["2 folders, 4 files", "1 file", "Empty"])

    def test_self_test_collapses_to_a_summary(self):
        got = self.run_js("""
          AV = { id: "a1" };
          const names = (st) => { const a = { id: "a1", selftest: st }; const s = selftestSection(a, selftestList(a));
            const grid = find(s, "check-grid"); return [s.kids[0].textContent, grid.kids.map((k) => k.textContent)]; };
          const ok = Object.fromEntries([1, 2, 3, 4, 5, 6].map((i) => [`check ${i}`, true]));
          const out = [names(ok), names({ ...ok, "check 6": false })];
          AV.stOpen = true;
          out.push(names(ok)[1].length);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got[0], ["Self-testAll 6 passed, last bootShow all 6", ["✓check 1", "✓check 2", "✓check 3", "✓check 4"]])
        self.assertEqual(got[1][0], "Self-test1 of 6 failed, last bootShow fewer")  # a failure opens the list, failures first
        self.assertEqual(got[1][1][0], "✗check 6")
        self.assertEqual(got[2], 6)

    def test_the_memory_in_use_changes_without_redrawing_the_sandbox(self):
        got = self.run_js("""
          const a = { id: "a1", name: "x", state: "ready", running: true, mode: "ephemeral", profile: "coding", repo: "me/app", tasks: [],
            cpus: 2, memory_mb: 4096, mem_used_mb: 900 };
          STATE = { role: "owner", agents_list: [a], vm_defaults: { max_cpus: 8, max_memory_mb: 16384 }, images: [], mcp_servers: {}, projects: [],
            secret_names: { agents: {}, research: [] } };
          AV = { id: "a1", pane: new Node("div") };
          document.activeElement = document.body;
          const out = [];
          for (const mb of [900, 2048, 3000]) { a.mem_used_mb = mb; renderSandbox(a); out.push([AV.pane.redraws, AV.memUsed.textContent]); }
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [[1, "Using 900 MiB now"], [1, "Using 2 GiB now"], [1, "Using 2.9 GiB now"]])

    def test_the_round_limit_line(self):
        got = self.run_js("""
          const row = (limit, a) => { STATE = { yolo_rounds: limit, role: "owner" }; AV = { id: "a1" }; return roundLimitRow(Object.assign({ id: "a1" }, a)).textContent; };
          console.log(JSON.stringify([row(3, { yolo_rounds: 1 }), row(null, {}), row(3, { yolo_rounds: 3, yolo_paused: true })]));
        """)
        self.assertEqual(got, [
            "Round limitPauses for you after 3 auto-approved rounds in a row (1 of 3 used).Applies to every agent.",
            "Round limitNo limit. Set one to pause for you after that many auto-approved rounds in a row.Applies to every agent.",
            "Round limitPaused after 3 automatic rounds. New questions wait in the inbox until you message this agent.Applies to every agent."])

    def test_the_bash_and_task_limit_rows(self):
        got = self.run_js("""
          const row = (role, a, pick) => { STATE = { role, vm_defaults: { task_timeout_minutes: 120 } }; AV = { id: "a1", lim: { aid: "a1", ...pick } };
            resetPicks(Object.assign({ id: "a1" }, a));
            const r = limitRow(Object.assign({ id: "a1" }, a), "task_limit_minutes", "Task time limit", 120, [30, 60, 120, 240, 1440], "A task that runs longer is stopped.");
            const sel = find(r, "select");
            // the explanation is behind the (i) next to the name; only an out-of-range value shows a note
            return [r.kids[0].kids[0].textContent, sel.kids.map((o) => o.textContent), sel.value, !!find(r, "primary"),
              find(r, "tip-box").textContent, (r.kids[2] && r.kids[2].textContent) || ""]; };
          console.log(JSON.stringify([row("owner", {}), row("owner", { task_limit_minutes: 90 }), row("owner", {}, { task_limit_minutes: 240 }),
            row("owner", { task_limit_minutes: 240 }, { task_limit_minutes: 240 }), row("device", { task_limit_minutes: 1440 }, { task_limit_minutes: 30 }),
            row("owner", { task_limit_minutes: null, limits_ignored: ["task_limit_minutes"] })]));
        """)
        self.assertEqual(got[0], ["Task time limit", ["Default (2 h)", "30 min", "1 h", "4 h", "24 h"], "", False,
                                  "A task that runs longer is stopped. Applies from the next message you send.", ""])
        self.assertEqual(got[1][1:3], [["Default (2 h)", "30 min", "1 h", "90 min", "4 h", "24 h"], 90])  # a value set through the API
        self.assertEqual(got[2][1:4], [["Default (2 h)", "30 min", "1 h", "4 h", "24 h"], 240, True])  # picked: Save appears
        self.assertFalse(got[3][3])  # saved
        self.assertEqual(got[4][2:4], [30, True])  # a phone may change it too
        self.assertEqual(got[5][2:4] + got[5][5:], ["", False, "The saved value is out of range, so the default applies."])

    def test_the_background_work_limit_row(self):
        got = self.run_js("""
          const row = (a) => { STATE = { role: "owner", vm_defaults: { task_timeout_minutes: 120 } }; AV = { id: "a1", lim: { aid: "a1" } };
            resetPicks(Object.assign({ id: "a1" }, a));
            const r = limitRow(Object.assign({ id: "a1" }, a), "bg_limit_minutes", "Background work limit", 10, [0, 30, 60], "x");
            const sel = find(r, "select");
            return [sel.kids.map((o) => o.textContent), sel.value]; };
          console.log(JSON.stringify([row({}), row({ bg_limit_minutes: 0 })]));
        """)
        self.assertEqual(got[0], [["Default (10 min)", "Until the task ends", "30 min", "1 h"], ""])
        self.assertEqual(got[1][1], 0)  # waiting until the task ends is a choice of its own, not the default

    def test_state_words(self):
        got = self.run_js("""
          const t = (status) => statusText({ status });
          console.log(JSON.stringify([t({ key: "running", queued: 0 }), t({ key: "running", queued: 2 }), t({ key: "waiting", items: 3 }),
            t({ key: "idle" }), t({ key: "starting" }), t({ key: "stopping" }), t({ key: "stopped" }), t({ key: "failed" }),
            t({ key: "<b>" }), t(undefined), statusKey({ status: { key: "constructor" } }),
            t({ key: "idle", stops_at: Date.now() / 1000 + 28.5 * 60 }), t({ key: "idle", stops_at: Date.now() / 1000 - 5 })]));
        """)
        self.assertEqual(got, ["Running", "Running and 2 queued", "Waiting for you", "Idle", "Starting", "Stopping",
                               "Stopped", "Failed", "Stopped", "Stopped", "stopped", "Idle, stops in 29 min", "Idle, stops in 1 min"])

    def test_short_state_words_and_boot_steps(self):
        got = self.run_js("""
          STATE = { policy_version: 3 };
          const s = (status, extra) => statusShort(Object.assign({ status }, extra));
          const boot = (phase, mode) => statusText({ status: { key: "starting" }, state: "booting", boot_phase: phase, mode });
          console.log(JSON.stringify([s({ key: "running", queued: 3 }), s({ key: "running", queued: 0 }), s({ key: "waiting", items: 2 }),
            s({ key: "starting" }), s({ key: "idle", stops_at: Date.now() / 1000 + 600 }), s({ key: "stopped" }), s({ key: "failed" }),
            s({ key: "stopping" }), boot("allocate"), boot("boot"), boot("memory", "memory"), boot("selftest", "memory"),
            boot(null), boot("from-a-newer-runner")]));
        """)
        self.assertEqual(got, ["Running +3", "Running", "Needs you", "Starting…", "Idle", "Stopped", "Failed", "Stopping",
                               "Starting (1 of 5)", "Starting (3 of 5)", "Starting (5 of 6)", "Starting (6 of 6)", "Starting", "Starting"])

    def test_state_icons(self):
        got = self.run_js("""
          const shape = (k) => { const s = stateIcon(k); return [s.attrs.class, s.attrs.width, s.kids.map((c) => c.tag + (c.attrs["stroke-dasharray"] ? " dashed" : "")).join(" ")]; };
          console.log(JSON.stringify(["running", "waiting", "idle", "starting", "stopping", "stopped", "failed"].map(shape)));
        """)
        self.assertEqual(got, [["st-icon st-running", "12", "circle path"], ["st-icon st-waiting", "12", "circle rect circle"],
                               ["st-icon st-idle", "12", "circle"], ["st-icon st-starting", "12", "circle dashed"],
                               ["st-icon st-stopping", "12", "circle dashed"], ["st-icon st-stopped", "12", "rect"],
                               ["st-icon st-failed", "12", "circle path"]])

    def test_the_header_pill_says_what_it_waits_on_and_where_it_leads(self):
        got = self.run_js("""
          const ag = (key, extra) => Object.assign({ id: "a1", name: "coder", status: { key } }, extra);
          const did = [];
          review = (k) => did.push("review " + k); answerAsk = (it) => did.push("answer " + it.key);
          switchTab = (t) => did.push("tab " + t); openInbox = () => did.push("inbox");
          const item = (kind, key, agent) => ({ kind, key, agent: agent || "a1" });
          const pill = (a) => { const p = statePill(a, () => a); pillAction(a)[1](); return [p.className, p.attrs.title, p.textContent]; };
          const out = [];
          INBOX.items = [item("research", "r1"), item("network", "n1", "other")]; out.push(pill(ag("waiting")));
          INBOX.items = [item("research", "r1"), item("research", "r2")]; out.push(waitingOn(ag("waiting"))[1]);
          INBOX.items = [item("network", "n1"), item("research", "r2"), item("findings", "f1")]; out.push(waitingOn(ag("waiting"))[1]);
          INBOX.items = [item("findings", "f1"), item("findings", "f2")]; out.push(waitingOn(ag("waiting"))[1]);
          INBOX.items = [item("questions", "q1")]; out.push(pill(ag("waiting")));
          INBOX.items = []; out.push(pill(ag("waiting")));  // the inbox hasn't caught up yet
          out.push(pill(ag("failed")), pill(ag("running", { status: { key: "running", queued: 3 } })));
          const pr = prLink("https://github.com/me/app/pull/7", "7");
          out.push([pr.textContent, pr.kids[0].className], prLink("https://evil.example/pull/7", "7").textContent);
          console.log(JSON.stringify([out, did]));
        """)
        self.assertEqual(got, [[
            ["pill state-pill st-waiting", "Waiting for you: a research request. Open it", "Waiting for you: a research requestNeeds you"],
            "2 requests", "a blocked site and 2 more", "findings to review",
            ["pill state-pill st-waiting", "Waiting for you: a question. Open it", "Waiting for you: a questionNeeds you"],
            ["pill state-pill st-waiting", "Waiting for you. Open it", "Waiting for youNeeds you"],
            ["pill state-pill st-failed", "Failed. Open the logs", "FailedFailed"],
            ["pill state-pill st-running", "Running and 3 queued. Open Tasks", "Running and 3 queuedRunning +3"],
            ["Open PR #7", "full-words"], "PR #7"],
            ["review r1", "answer q1", "inbox", "tab logs", "tab tasks"]])

    def test_the_question_picker(self):
        got = self.run_js(r"""
          const rec = { id: "q1", task: "t1", name: "plan.json", state: "open", questions: [
            { header: "Database", question: "Which one?", multi: false, options: [{ label: "SQLite", description: "One file." }, { label: "MySQL", description: "" }] },
            { header: "Features", question: "Which first?", multi: true, options: [{ label: "Shop", description: "" }, { label: "Mail", description: "" }, { label: "Bank", description: "" }] }] };
          const a = { id: "a1", name: "coder", state: "ready", running: true, tasks: [], asks: [rec] };
          STATE = { agents_list: [a] };
          const out = [];
          const node = askPicker(a, rec), st = PICKS.a1;
          out.push(node.textContent);
          pick("a1", 1);                                   // one answer: picked, and on to the next question
          out.push([st.tab, st.picked[0]]);
          pick("a1", 0); pick("a1", 2); pick("a1", 0);     // several: tick, tick, untick
          out.push([st.tab, st.picked[1]]);
          pick("a1", 3); st.other[1] = "  and a bank  "; ownDone("a1");  // Type something keeps the ticks
          out.push([st.tab, st.picked[1], answerText(rec.questions[1], st, 1)]);
          st.tab = 2; redrawPicker("a1");
          out.push(node.textContent);
          out.push(askCard(Object.assign({}, rec, { state: "answered", answers: [{ picked: ["MySQL"], other: "" }, { picked: ["Bank"], other: "and a bank" }] })).textContent);
          out.push(askCard({ id: "q2", name: "?", state: "refused", error: "the file isn't valid JSON" }).textContent);
          out.push(askCard({ id: "q3", name: "a.json", state: "refused", error: "it needs 1 to 4 questions", tell: true }).textContent);
          out.push(askCard({ id: "q4", name: "a.json", state: "refused", error: "it needs 1 to 4 questions", told: "t9" }).textContent);
          out.push([itemTitle({ kind: "questions", questions: rec.questions }), itemSummary({ kind: "questions", questions: rec.questions })]);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [
            "coder is askingAnswer in chat insteadDatabaseFeaturesSubmitWhich one?1.SQLiteOne file.2.MySQL3.Type something↑ ↓ and Enter, or a number",
            [1, [1]], [1, [2]], [1, [2], "Bank, “and a bank”"],
            "coder is askingAnswer in chat instead✓ Database✓ FeaturesSubmitYour answersDatabaseMySQLFeaturesBank, “and a bank”Submit answers",
            "Questions~/ask/plan.jsonansweredDatabaseMySQLFeaturesBank, “and a bank”",
            "Questions~/ask/?refusedNot shown: the file isn't valid JSON.",
            "Questions~/ask/a.jsonrefusedNot shown: it needs 1 to 4 questions.Your next message tells the agent why, so it can ask again.",
            "Questions~/ask/a.jsonrefusedNot shown: it needs 1 to 4 questions.Your next message told the agent why.",
            ["2 questions for you", "Database, Features"]])

    def test_the_picker_takes_the_keyboard_unless_you_are_writing(self):
        got = self.run_js(r"""
          const rec = { id: "q1", state: "open", name: "a.json", questions: [
            { header: "H", question: "Q?", multi: false, options: [{ label: "A", description: "" }, { label: "B", description: "" }] }] };
          const a = { id: "a1", name: "coder", state: "ready", running: true, tasks: [], asks: [rec] };
          STATE = { agents_list: [a] };
          const focused = [], open = () => { AV = { id: "a1" }; const n = askPicker(a, rec); n.focus = () => focused.push(a.id); focusAsk(a, rec); };
          open(); focusAsk(a, rec);                       // opened: the picker gets it, once
          composerState("a1").draft = "Actually, ";
          open();                                         // opened again while you're writing: it stays with you
          composerState("a1").draft = "";
          window.matchMedia = () => ({ matches: true }); open(); window.matchMedia = () => ({ matches: false });  // a phone: the sheet does it
          console.log(JSON.stringify(focused));
        """)
        self.assertEqual(got, ["a1"])

    def test_sidebar_redraws_only_when_something_shown_changed(self):
        got = self.run_js("""
          const ag = { id: "a1", name: "coder", state: "busy", running: true, tasks: [], repo: "me/app", queue: [],
            summary: "Fixing the build", status: { key: "running", queued: 0 } };
          STATE = { agents_list: [ag], projects: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {} };
          const box = $("sb-agents"), out = [];
          const draw = () => { renderSidebar(); out.push(box.redraws); };
          draw(); draw();                                  // the 4 s poll with nothing new
          ag.status = { key: "running", queued: 1 }; draw();
          ag.summary = "Tests pass, PR open"; draw(); draw();
          ag.tasks = [{ id: "t", instructions: "x", state: "done" }]; draw();  // not shown while it has a summary
          const row = box.kids.find((k) => find(k, "sb-agent"));
          const st = find(row, "st"), l2 = find(row, "l2");
          out.push(st.textContent, st.className, l2.textContent, l2.className);
          VIEWS.agents.mount(new Node("main")); VIEWS.agents.update(); VIEWS.agents.update();
          out.push(VIEWS.agents.p.list.redraws, find(VIEWS.agents.p.list.kids.find((k) => find(k, "sb-agent")), "st").textContent);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [1, 1, 2, 3, 3, 3, "1 queued", "st st-running", "Tests pass, PR open", "l2 ellipsis", 1,
                               "1 queued"])  # the icon says it runs

    def test_a_list_row_says_only_what_its_icon_cant(self):
        got = self.run_js("""
          document.createElementNS = (ns, t) => new Node(t);
          const d = (status, extra) => statusDetail(Object.assign({ status }, extra));
          const [icon] = rowState({ status: { key: "stopped" } });
          console.log(JSON.stringify([d({ key: "stopped" }), d({ key: "running" }), d({ key: "waiting" }), d({ key: "running", queued: 2 }),
            d({ key: "idle", stops_at: Date.now() / 1000 + 600 }), d({ key: "stopped" }, { move: { step: 2 } }),
            icon.attrs["aria-label"], icon.attrs.role]));
        """)
        self.assertEqual(got, ["", "", "", "2 queued", "Stops in 10 min", "Moving (2 of 2)", "Stopped", "img"])

    def test_a_second_click_closes_the_popover_it_opened(self):
        got = self.run_js("""
          window.matchMedia = () => ({ matches: true });  // a phone: the sheet path needs no layout
          const a = new Node("button"), b = new Node("button"), pop = () => new Node("div");
          const out = [openPop(a, pop()), openPop(a, pop()), POP === null, openPop(a, pop()), openPop(b, pop()), POP.anchor === b,
            openPop(b, pop(), { toggle: false }), POP.anchor === b];
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [True, False, True, True, True, True, True, True])

    def test_pulling_a_sheet_down_closes_it(self):
        got = self.run_js("""
          window.matchMedia = (q) => ({ matches: !q.includes("reduce") });  // a phone, with motion
          let now = 0;
          Date.now = () => now;
          const touch = (n, type, y, at, target) => { now = at; return n.fire(type, { touches: [{ clientX: 100, clientY: y }], cancelable: true, target: target || n }); };
          const pull = (moves, target) => {  // [y, ms] pairs, from y 100 at 0 ms
            const node = new Node("div");
            node.offsetHeight = 500;
            openPop(new Node("button"), node);
            touch(node, "touchstart", 100, 0, target && target(node));
            const prevented = moves.map(([y, at]) => touch(node, "touchmove", y, at).prevented || false);
            const moved = node.style.transform;
            node.fire("touchend", {});
            return { node, prevented, moved };
          };
          const wait = () => new Promise((r) => setTimeout(r, 250));
          const out = { decide: [sheetGesture(0, 20, true), sheetGesture(0, 20, false), sheetGesture(0, -20, true), sheetGesture(30, 20, true)],
            closes: [sheetCloses(200, 500, 0.1), sheetCloses(100, 500, 0.1), sheetCloses(100, 500, 0.9), sheetCloses(10, 500, 2)] };
          let r = pull([[104, 10], [160, 100], [300, 400]]);  // far: it closes
          out.far = [r.prevented, r.moved, r.node.style.transform];
          await wait(); out.farClosed = POP === null;
          r = pull([[110, 100], [140, 400]]);  // a short, slow pull: it goes back
          out.short = [r.moved, r.node.style.transform, POP?.node === r.node];
          closePop();
          r = pull([[110, 10], [180, 60]]);  // short but fast: a flick closes it
          await wait(); out.flick = POP === null;
          r = pull([[140, 50], [300, 100]], (node) => { const inner = new Node("div"); inner.scrollTop = 80; inner.parentNode = node; return inner; });
          out.scrolled = [r.prevented, r.moved, POP?.node === r.node];  // the list inside is scrolled down: it scrolls, the sheet stays
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got["decide"], ["drag", "scroll", "scroll", "scroll"])
        self.assertEqual(got["closes"], [True, False, True, False])
        self.assertEqual(got["far"], [[True, True, True], "translateY(200px)", "translateY(100%)"])
        self.assertTrue(got["farClosed"])
        self.assertEqual(got["short"], ["translateY(40px)", "", True])
        self.assertTrue(got["flick"])
        self.assertEqual(got["scrolled"], [[False, False], None, True])

    def test_an_empty_project_is_one_line_with_a_plus(self):
        got = self.run_js("""
          const ag = { id: "a1", name: "coder", state: "ready", running: true, tasks: [], repo: "me/app", project: "p2", status: { key: "idle" } };
          STATE = { agents_list: [ag], projects: [{ id: "p1", name: "Empty", pending: [] }, { id: "p2", name: "Busy", pending: [] }],
            tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {} };
          const rows = (list) => list.filter((k) => k.className).map((k) => k.className.split(" ")[0] + ":" + k.textContent);
          renderSidebar();
          const side = rows($("sb-agents").kids);
          const plus = find($("sb-agents").kids[1], "sb-here");
          const went = []; go = (v) => went.push(v, NEW.project);
          plus.fire("click");
          VIEWS.agents.mount(new Node("main")); VIEWS.agents.update();
          console.log(JSON.stringify([side, rows(VIEWS.agents.p.list.kids), plus.attrs["aria-label"], went]));
        """)
        self.assertEqual(got, [["sb-caption:Projects+", "sb-project:Empty+", "sb-project:Busy▾", "sb-row:coderme/app⋮"],
                               ["sb-caption:Projects+", "m-project-line:Empty+", "m-project-line:Busy1 agent▾", "sb-row:coderme/app⋮"],
                               "New agent in Empty", ["new", "p1"]])

    def test_the_phone_list_has_the_sidebar_groups(self):
        got = self.run_js("""
          const ag = (id, name, extra) => Object.assign({ id, name, state: "stopped", tasks: [], repo: "me/app", status: { key: "stopped" } }, extra);
          STATE = { role: "owner", agents_list: [ag("a1", "pinned", { pinned: true, project: "p1" }), ag("a2", "member", { project: "p1", status: { key: "running", queued: 3 } }),
              ag("a3", "loose"), ag("a4", "old", { archived: true, project: "p1" })],
            projects: [{ id: "p1", name: "Lantern", pending: [{}] }], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {} };
          const rows = (list) => list.map((k) => k.className + ":" + k.textContent);
          VIEWS.agents.mount(new Node("main")); VIEWS.agents.update();
          const out = [rows(VIEWS.agents.p.list.kids)];
          const member = VIEWS.agents.p.list.kids[4];
          out.push(find(member, "st").attrs.title);
          UI.showArchived = true; VIEWS.agents.update();
          out.push(rows(VIEWS.agents.p.list.kids).slice(-2));
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got[0], ["sb-caption:Pinned", "sb-row:pinnedme/app⋮", "sb-caption row nowrap:Projects+",
                                  "m-project-line:Lantern1 suggested note2 agents▾", "sb-row member:member3 queuedme/app⋮", "sb-caption:Other agents",
                                  "sb-row:looseme/app⋮", "sb-caption sb-group:Archived (1)▸"])
        self.assertEqual(got[1], "Running and 3 queued")  # the full phrase is in the title
        self.assertEqual(got[2], ["sb-caption sb-group:Archived (1)▾", "sb-row archived:oldme/app⋮"])

    def test_a_phone_can_fold_a_projects_agents_and_remembers_it(self):
        got = self.run_js("""
          const ag = (id, name, extra) => Object.assign({ id, name, state: "stopped", tasks: [], repo: "me/app", status: { key: "stopped" } }, extra);
          STATE = { role: "owner", agents_list: [ag("a1", "member", { project: "p1" })], projects: [{ id: "p1", name: "Lantern", pending: [] }],
            tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {} };
          VIEWS.agents.mount(new Node("main")); VIEWS.agents.update();
          const rows = () => VIEWS.agents.p.list.kids.map((k) => k.className);
          const before = rows();
          const fold = VIEWS.agents.p.list.kids[1].kids[1];
          fold.fire("click");
          console.log(JSON.stringify([before, rows(), [...UI.closedGroups], fold.attrs["aria-expanded"]]));
        """)
        self.assertEqual(got[0], ["sb-caption row nowrap", "m-project-line", "sb-row member"])
        self.assertEqual(got[1], ["sb-caption row nowrap", "m-project-line"])
        self.assertEqual(got[2], ["p:p1"])

    def test_the_phone_agents_page_opens_the_switcher(self):
        got = self.run_js("""
          STATE = { role: "device", agents_list: [], projects: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {} };
          VIEWS.agents.mount(new Node("main"));
          const top = VIEWS.agents.p.list.parent.kids[0], btn = top.kids.find((k) => k.attrs["aria-label"] === "Go to an agent, project or page");
          btn.fire("click");
          console.log(JSON.stringify([!!btn, !!SWITCHER, switcherItems().some((it) => it.label === "Settings")]));
        """)
        self.assertEqual(got, [True, True, False])

    def test_a_failed_poll_shows_one_calm_bar_that_clears_when_it_works(self):
        got = self.run_js("""
          Node.prototype.remove = function () { const p = this.parent; if (p) p.kids = p.kids.filter((k) => k !== this); };
          document.querySelector = (sel) => document.body.kids.find((k) => k.className.split(" ").includes(sel.slice(1))) || null;
          let up = false, toasts = 0, went = [];
          globalThis.api = async () => { if (!up) throw new TypeError("Failed to fetch"); return { role: "owner", setup: { done: true }, agents_list: [], pairing: [] }; };
          toast = () => { toasts++; }; go = (v) => { went.push(v); }; renderSidebar = () => {};
          const bars = () => document.body.kids.filter((k) => k.className === "conn-bar").length;
          await loadState(); await loadState(); await loadState();
          const down = [bars(), toasts, went.length, document.body.kids[0].textContent];
          up = true;
          await loadState();
          console.log(JSON.stringify([down, bars(), went, ROUTED]));
        """)
        self.assertEqual(got, [[1, 0, 0, "Can't reach cilly" + "Retry"], 0, ["new"], True])

    def test_the_look_bar_is_not_rebuilt_while_the_draft_is_unchanged(self):
        got = self.run_js("""
          document.querySelector = (sel) => document.body.kids.find((k) => k.className.split(" ").includes(sel.slice(1))) || null;
          LOOK.draft = {}; UI.view = "agents";
          lookBar(); const first = document.body.kids[0]; lookBar();
          console.log(JSON.stringify([document.body.kids.length, document.body.kids[0] === first]));
        """)
        self.assertEqual(got, [1, True])

    def test_a_delete_confirm_focuses_cancel_and_the_menu_has_no_letters_on_a_phone(self):
        got = self.run_js("""
          let focused = null;
          Node.prototype.focus = function () { focused = this; };
          confirmModal("Delete x?", "", "Delete", true);
          const danger = focused.textContent;
          confirmModal("Stop x?", "", "Stop");
          const plain = focused.textContent;
          STATE = { role: "owner", projects: [] };
          const a = { id: "a1", name: "x", running: false, state: "stopped", pinned: false };
          const text = () => agentListMenu(a, () => {}).kids[0].textContent;
          const other = text();
          UI.view = "agent"; UI.agentId = "a1";
          const desktop = text();
          window.matchMedia = (q) => ({ matches: q.includes("max-width") });
          console.log(JSON.stringify([danger, plain, other, desktop, text(), keyLabel("r", true)]));
        """)
        # the keys show only for the open agent, whose they are, and never on a phone
        self.assertEqual(got, ["Cancel", "Stop", "Rename", "RenameAlt+Shift+R", "Rename", "⌥⇧R"])

    def test_alt_shift_letters_act_on_the_open_agent_from_anywhere(self):
        got = self.run_js("""
          const calls = [];
          globalThis.api = async (m, u, b) => { calls.push([u, b]); return {}; };
          toast = () => {}; loadState = async () => {};
          let renamed = 0; renameInHeader = () => { renamed++; };
          STATE = { role: "owner", projects: [], agents_list: [{ id: "a1", name: "x", running: false, state: "stopped", pinned: false }] };
          UI.view = "agent"; UI.agentId = "a1";
          const press = (code, extra) => { for (const f of document.ls.keydown) f(Object.assign({ key: "x", code, altKey: true, shiftKey: true, ctrlKey: false, metaKey: false, preventDefault() {} }, extra)); };
          press("KeyP"); press("KeyR"); press("KeyA");
          press("KeyP", { altKey: false }); press("KeyP", { ctrlKey: true }); press("KeyQ");
          UI.view = "settings"; press("KeyP");
          await Promise.resolve();
          console.log(JSON.stringify([calls, renamed]));
        """)
        self.assertEqual(got, [[["/api/agents/a1/organize", {"pinned": True}], ["/api/agents/a1/organize", {"archived": True}]], 1])

    def test_changing_the_research_agent_while_approving_keeps_yolo_on_a_phone_too(self):
        got = self.run_js("""
          const calls = [];
          globalThis.api = async (m, u, b) => { calls.push([u, b]); return {}; };
          toast = () => {}; loadState = async () => {}; pollInbox = async () => {};
          VIEWS.review.p = {}; VIEWS.review.update = () => {};
          const researchers = [{ id: "r1", name: "r1", profile: "research" }, { id: "r2", name: "r2", profile: "research" }];
          const run = async (role) => {
            STATE = { role, agents_list: [{ id: "a1", name: "coder", research_agent: "r1", research_auto: true }, ...researchers] };
            calls.length = 0;
            const it = { kind: "research", agent: "a1", id: "q1", name: "q", text: "why", research_agent: "r2" };
            const acts = reviewResearch(it, []);
            await acts[1].fire("click");
            await new Promise((r) => setTimeout(r));
            return calls.map(([u, b]) => u.split("/").pop() + ":" + JSON.stringify(b?.auto));
          };
          console.log(JSON.stringify([await run("device"), await run("owner")]));
        """)
        self.assertEqual(got, [["link:true", "approve:undefined"]] * 2)  # a phone may keep YOLO on, like the PC

    def test_a_paired_inbox_never_lists_device_requests(self):
        got = self.run_js("""
          globalThis.api = async () => ({ items: [{ key: "d", kind: "device" }, { key: "n", kind: "network", agent: "a1" }], count: 2 });
          UI.view = "none"; STATE = { role: "device", agents_list: [] };
          await pollInbox();
          const paired = [INBOX.items.map((i) => i.kind), INBOX.count];
          STATE.role = "owner"; await pollInbox();
          console.log(JSON.stringify([paired, INBOX.items.length, INBOX.count]));
        """)
        self.assertEqual(got, [[["network"], 1], 2, 2])

    def test_the_shell_has_one_title_scale_and_an_ordered_z_index_ladder(self):
        css = read(os.path.join(STATIC, "style.css"))
        for sel in [".page-title", ".hero", ".review-title", ".setup-title", ".m-h1"]:
            self.assertNotRegex(css, r"(?m)^" + re.escape(sel) + r"\s*\{[^}]*font-size", sel)  # only the shared rule sizes a title
        z = lambda sel: int(re.search(re.escape(sel) + r" \{[^}]*?z-index: (\d+)", css).group(1))
        self.assertLess(z(".pop"), z(".look-bar"))
        self.assertLess(z(".look-bar"), z(".conn-bar"))
        self.assertLess(z(".conn-bar"), z(".modal-back"))
        self.assertLess(z(".modal-back"), z(".qs-back"))
        self.assertLess(z(".qs-back"), z(".toast"))
        self.assertNotIn("sb-none", css)

    def test_a_long_press_or_right_click_opens_the_menu_once(self):
        got = self.run_js("""
          const on = {}, timers = [];
          const node = { addEventListener: (t, fn) => { on[t] = fn; } };
          globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
          globalThis.clearTimeout = (i) => { if (i) timers[i - 1] = null; };
          const fire = () => timers.forEach((f, i) => { if (f) { timers[i] = null; f(); } });
          let opened = 0, clicks = 0;
          onHold(node, () => opened++);
          const ev = (extra) => Object.assign({ clientX: 0, clientY: 0, preventDefault() {}, stopPropagation() { clicks--; } }, extra);
          const click = () => { clicks++; on.click(ev()); };
          on.pointerdown(ev({ pointerType: "touch" })); fire(); click();          // long press: menu, and its tap isn't a click
          const a = [opened, clicks];
          on.pointerdown(ev({ pointerType: "touch" })); on.pointerup(ev()); fire(); click();   // a tap
          const b = [opened, clicks];
          on.pointerdown(ev({ pointerType: "touch" })); on.pointermove(ev({ clientX: 30 })); fire();  // a scroll
          const c = opened;
          on.pointerdown(ev({ pointerType: "touch" })); fire(); on.contextmenu(ev());  // Android also sends contextmenu
          const d = opened;
          on.pointerdown(ev({ pointerType: "mouse" })); on.contextmenu(ev()); fire(); // right click
          console.log(JSON.stringify([a, b, c, d, opened]));
        """)
        self.assertEqual(got, [[1, 0], [1, 1], 1, 2, 3])

    def test_the_boot_preview_drops_marks_and_codes(self):
        got = self.run_js(r"""
          const E = String.fromCharCode(0x241b), B = String.fromCharCode(0x2407), CR = String.fromCharCode(0x240d);
          console.log(JSON.stringify(bootLines(`old\n\x1b[1;32m[  OK  ]\x1b[0m Started ${E}[Kjournald\n` +
            `${E}]0;title${B}Booting${CR}\n  \nLogin ready ${E}[2J\n`)));
        """)
        self.assertEqual(got, ["[  OK  ] Started journald", "Booting", "Login ready"])

    def test_logs_show_colors_and_mark_every_other_control(self):
        got = self.run_js(r"""
          const show = (line) => { const box = logLine(line, "agent").kids[1];
            return box.kids.map((k) => typeof k === "string" ? k : [k.className, k.textContent]); };
          const text = (n) => n.kids.map((k) => typeof k === "string" ? k : k.textContent).join("");
          const row = (line, which) => { const r = logLine(line, which); return [r.className, r.kids[0].textContent, text(r.kids[1])]; };
          console.log(JSON.stringify([show("\x1b[1;31mred\x1b[0m plain"), show("\x1b]52;c;ZXZpbA==\x07 x"),
            show("\x1b]8;;https://evil.example/\x1b\\docs"), show("a\x1bb\x1bPq\x1b\\"), show("\x9b2J cr\rover"),
            show("crlf\r"), show("tab\there"),
            row("05:12:40Z selftest PASS dns", "agent"), row("===== cilly: start =====", "agent"), row("05:12:40Z", "agent"),
            row('{"ts": "2026-10-14T09:01:02Z", "action": "deny"}', "egress"), row("12:00:00Z [ OK ] Started", "console")]));
        """)
        E, B = chr(0x241B), chr(0x2407)
        self.assertEqual(got, [[["a31 ab", "red"], " plain"], [f"{E}]52;c;ZXZpbA=={B} x"],
                               [f"{E}]8;;https://evil.example/{E}\\docs"], [f"a{E}b{E}Pq{E}\\"],
                               [f"{E}[2J cr{chr(0x240D)}over"], ["crlf"], ["tab\there"],
                               ["log-line l-ok", "05:12:40", "selftest PASS dns"], ["log-line l-dim", "", "===== cilly: start ====="],
                               ["log-line ", "", "05:12:40Z"], ["log-line l-err", "09:01:02", '{"ts": "2026-10-14T09:01:02Z", "action": "deny"}'],
                               ["log-line ", "", "12:00:00Z [ OK ] Started"]])

    def test_a_blank_log_line_keeps_its_height(self):
        got = self.run_js(r"""
          const r = logLine("", "console");
          console.log(JSON.stringify([r.className, r.kids[1].className, r.kids[1].kids.length]));
        """)
        self.assertEqual(got, ["log-line ", "log-text", 0])  # empty, so the rule below gives it a line
        self.assertIn('.log-text:empty::before { content: "\\200b"; }', read(os.path.join(STATIC, "style.css")))

    def test_the_context_meter(self):
        got = self.run_js(r"""
          const a = (context, extra) => Object.assign({ id: "a1", name: "x", state: "ready", running: true, tasks: [], context }, extra);
          const pill = (x) => { const p = contextPill(x, () => x); return p ? [p.className, p.textContent, p.attrs.title] : null; };
          const pop = (x) => contextPop(x).textContent;
          const menu = (x) => { window.matchMedia = () => ({ matches: true }); const m = agentMenu(x).textContent; window.matchMedia = () => ({ matches: false }); return m; };
          const c1 = a({ used: 124000, window: 200000 });
          console.log(JSON.stringify([
            pill(c1), pill(a({ used: 150000, window: 200000 })), pill(a({ used: 190000, window: 200000 })),
            pill(a({ used: 900000, window: 1000000 })), pill(a({ used: 250000, window: 200000 })),
            pill(a(undefined)), pill(a({ used: 5, window: 0 })),  // an older runner: no meter
            pop(c1), pop(a({ used: 190000, window: 200000 })), pop(a({ used: 124000, window: 200000 }, { state: "stopped", running: false })),
            menu(c1).includes("Context 62%124k of 200k tokens"), menu(a(undefined)).includes("Context"),
          ]));
        """)
        self.assertEqual(got, [
            ["ctx-pill ", "Context62%", "124k of 200k tokens"], ["ctx-pill high", "Context75%", "150k of 200k tokens"],
            ["ctx-pill full", "Context95%", "190k of 200k tokens"], ["ctx-pill full", "Context90%", "900k of 1M tokens"],
            ["ctx-pill full", "Context100%", "250k of 200k tokens"], None, None,
            "Context62%124k of 200k tokens used. 76k free.Plenty of room.Start a new chat",
            "Context95%190k of 200k tokens used. 10k free.Nearly full. Claude compacts on its own at the limit, which can drop details. "
            "A new chat starts fresh.Start a new chat",
            "Context62%124k of 200k tokens used. 76k free.From the last task. The next message continues this conversation "
            "unless you start a new chat.Start a new chat",
            True, False])

    def test_setup_wizard_words(self):
        got = self.run_js("""
          const day = (n) => new Date(Date.now() - n * 86400000).toISOString();
          console.log(JSON.stringify([SETUP_STEPS.map((s) => s[1]), [0, 1, 2].map(reposNext),
            [0, 1, 3, 20, 100].map((n) => updatedAgo(day(n))), updatedAgo("soon")]));
        """)
        self.assertEqual(got, [["Host check", "Secrets", "Repositories", "Network", "Policy", "First agent"],
                               ["Continue", "Continue with 1 repo", "Continue with 2 repos"],
                               ["updated today", "updated yesterday", "updated 3 days ago", "updated 2 weeks ago", "updated 3 months ago"], ""])

    def test_a_first_agent_booting(self):
        got = self.run_js("""
          STATE = { policy_version: 1 };
          const a = { name: "architect", state: "booting", cpus: 2, memory_mb: 4096, mode: "ephemeral" };
          console.log(JSON.stringify([composerHint(a), composerHint({ ...a, state: "ready" }), bootSteps(a)[0][2]]));
        """)
        self.assertEqual(got, ["Type now. It sends once architect is ready.", "Message architect…", "2 vCPU · 4096 MiB"])

    def test_the_finish_setting_up_checklist(self):
        got = self.run_js("""
          const rows = (n) => n.kids.slice(1, -1).map((r) => r.textContent);  // not the bubblewrap command after them
          const state = (setup, bwrap) => ({ tools: { qemu: 1, "qemu-img": 1, kvm: true, bwrap }, base_image: "base.qcow2",
            setup: Object.assign({ steps: [], claude: "missing", github: "missing", repos: 0 }, setup) });
          STATE = state({ steps: ["host", "policy"], claude: "saved" }, null);
          const some = setupChecklist();
          STATE = state({ steps: ["host", "secrets"] }, null);
          const again = rows(setupChecklist())[0];
          STATE = state({ steps: ["network", "policy"], claude: "saved", github: "saved", repos: 2 }, "/usr/bin/bwrap");
          const all = setupChecklist();
          STATE = state({ checklist_hidden: true }, null);
          console.log(JSON.stringify([some.kids[0].textContent, rows(some), again, all, setupChecklist()]));
        """)
        self.assertEqual(got, ["Finish setting up3 of 6 doneHide",
                               ["○GitHub token and repositoriesAdd token", "○Network allowlist reviewedReview",
                                "○Install bubblewrap (optional)Show command"], "○Claude loginPaste it again", "", ""])  # only what is left

    def test_vm_images_commands_and_source(self):
        got = self.run_js("""
          const words = ["/home/me/debian.qcow2", "/home/me/My Images/d.qcow2", "/x/it's.qcow2", "/x/$(touch y).qcow2"].map(shellWord);
          const field = (extra) => {
            STATE = Object.assign({ presets: { full: { label: "All-in-one", toolchains: [], about: "everything" } }, recipes: {}, images: [],
              build: {}, host: { cli: "python3 -m cilly" }, tools: {} }, extra);
            const box = imagesSection(), out = [];
            const walk = (n) => { if (n instanceof Node) { if (n.tag === "input" && n.attrs["aria-label"] === "Debian cloud image") out.push(n.value);
              if (n.className.includes("copy") || n.tag === "code") out.push(n.textContent); n.kids.forEach(walk); } };
            walk(box);
            return out;
          };
          console.log(JSON.stringify([words, field({ build_source: "/home/me/gone.qcow2", build_source_ok: false }),
            field({ build_source: "/home/me/My Images/d.qcow2", build_source_ok: true })]));
        """)
        words, gone, kept = got
        self.assertEqual(words, ["/home/me/debian.qcow2", "'/home/me/My Images/d.qcow2'", "'/x/it'\\''s.qcow2'", "'/x/$(touch y).qcow2'"])
        self.assertEqual(gone[0], "")  # deleted since: the field doesn't offer it
        self.assertTrue(any("build-base --download --preset full" in t for t in gone[1:]), gone)
        self.assertEqual(kept[0], "/home/me/My Images/d.qcow2")
        self.assertTrue(any("build-base --image '/home/me/My Images/d.qcow2' --preset full" in t for t in kept[1:]), kept)

    def test_the_host_check_on_this_pc(self):
        got = self.run_js("""
          const text = (nodes) => nodes.map((n) => (n && n.textContent) || "").join("|");
          const base = (tools, host, extra) => Object.assign({ tools: Object.assign({ qemu: 1, "qemu-img": 1, kvm: true, xorriso: 1, use_bwrap: "auto" }, tools),
            host: Object.assign({ distro: "Ubuntu 24.04.3 LTS", cmds: {}, install_sh: "", unsupported: "" }, host), base_image: "", build: {} }, extra);
          const out = [];
          // blocked by AppArmor while the image downloads: install.sh helps, and VMs wait for it
          STATE = base({ bwrap_problem: { why: "bwrap: setting up uid map: Permission denied", kind: "apparmor",
            fix: "Ubuntu's AppArmor blocks it. cilly's install.sh adds one.", cmd: "" } }, { install_sh: "sudo sh /x/install.sh" },
            { build: { state: "running", step: 1, steps: ["Preparing", "Installing", "Saving"], download: [180e6, 400e6] } });
          out.push(text(hostChecks()), hostProblems());
          // blocked by a kernel setting: install.sh can't help, the server doesn't offer it, the page gives the sysctl
          STATE = base({ bwrap_problem: { why: "x", kind: "clone", fix: "Turn it on.", cmd: "sudo sysctl kernel.unprivileged_userns_clone=1" } }, {});
          out.push(text(hostChecks()));
          // vm.use_bwrap off: nothing to fix
          STATE = base({ use_bwrap: "off" }, {}, { base_image: "b.qcow2" });
          out.push(text(hostChecks()), hostProblems(), hostSummary());
          STATE = base({}, { distro: "Rocky Linux 9", unsupported: "RHEL, CentOS Stream, Rocky Linux and AlmaLinux aren't supported yet." });
          out.push(text(hostChecks()));
          // a file you gave once and deleted since: the setup button downloads instead
          const sent = [];
          api = async (m, path, body) => { sent.push(body); return {}; };
          loadState = async () => {};
          STATE = base({}, {}, { build_source: "/home/me/gone.qcow2", build_source_ok: false });
          let kicked = 0;
          imagesKick = () => { kicked++; };  // VM images, on the same Settings page, follows the build
          await startImageBuild();
          STATE.build_source_ok = true;
          await startImageBuild();
          out.push(sent.map((b) => b.source), kicked);
          out.push(buildStepText({ state: "running", step: 2, steps: ["Preparing", "Installing", "Saving"] }),
            buildStepText({ step: 1, steps: ["Preparing", "Installing", "Saving"], download: [5e6, 0] }));
          console.log(JSON.stringify(out));
        """)
        blocked, problems, clone, off, off_problems, off_summary, rocky, sources, kicked, step2, nolength = got
        self.assertIn("Running on Ubuntu 24.04.3 LTS", blocked)
        self.assertNotIn("This PC:", blocked)
        self.assertIn("Building it. Step 1 of 3: Downloading Debian, 45% (180 of 400 MB). You can leave this page.", blocked)
        self.assertIn("bubblewrap (bwrap) is blockedrequired", blocked)
        self.assertIn('set vm.use_bwrap to "off" in config.json yourself', blocked)
        self.assertIn("sudo sh /x/install.sh", blocked)
        self.assertIn("a working bubblewrap", problems)
        self.assertNotIn("install.sh", clone.replace("cilly's install.sh adds one", ""))
        self.assertIn("sudo sysctl kernel.unprivileged_userns_clone=1", clone)
        self.assertIn("bubblewrap (bwrap)off in config.json", off)
        self.assertEqual((off_problems, off_summary), ([], ["s-busy", "Host ready, bwrap off"]))
        self.assertIn("aren't supported yet", rocky)
        self.assertNotIn("sudo sh", rocky)
        self.assertEqual((sources, kicked), (["", "/home/me/gone.qcow2"], 2))
        self.assertEqual((step2, nolength), ("Step 2 of 3: Installing", "Step 1 of 3: Downloading Debian (5 MB)"))

    def test_this_pc_follows_a_running_build(self):
        got = self.run_js("""
          let n = 0;
          STATE = { tools: {}, host: {}, service: {}, build: { state: "running", step: 1 }, base_image: "" };
          VIEWS.settings.p = { thisPc: { isConnected: true, render: () => { n++; } }, phone: isPhone(), main: new Node("div") };
          VIEWS.settings.update(); VIEWS.settings.update();
          const once = n;
          STATE.build = { state: "running", step: 1, download: [1e6, 4e8] };
          VIEWS.settings.update();
          STATE.build = { state: "running", step: 2 };
          VIEWS.settings.update();
          console.log(JSON.stringify([once, n]));
        """)
        self.assertEqual(got, [1, 3])

    def test_narrow_screens_see_which_step(self):
        # the stepper is hidden under 900px, so the step's own page says where you are and how to leave
        got = self.run_js("""
          STATE = { tools: { qemu: 1, "qemu-img": 1, kvm: true }, base_image: "b.qcow2", setup: { steps: [] } };
          VIEWS.setup.p = { stepper: new Node("nav"), body: new Node("div") };
          UI.setupStep = 0; renderSetup();
          console.log(JSON.stringify(VIEWS.setup.p.body.kids[0].textContent));
        """)
        self.assertEqual(got, "Step 1 of 6 · Host checkFinish later")

    def test_the_recommended_claude_md_and_its_switches(self):
        # the rules with the picked style lines, and back: a text that's your own is never taken for them
        got = self.run_js("""
          const r = { claude_md_recommended: "# Working style\\n\\n- Ask.\\n", claude_md_options: [
            { key: "no_dashes", line: "- No dashes." }, { key: "ascii", line: "- ASCII." }, { key: "no_emojis", line: "- No emojis." }] };
          const both = claudeMdOf(r, ["no_emojis", "no_dashes"]);
          console.log(JSON.stringify([claudeMdOf(r, []), both, claudeMdPicked(both, r), claudeMdPicked(r.claude_md_recommended, r),
            claudeMdPicked(both + "- Mine.\\n", r), claudeMdPicked("Use tabs.", r)]));
        """)
        self.assertEqual(got, ["# Working style\n\n- Ask.\n", "# Working style\n\n- Ask.\n\n# Style\n\n- No dashes.\n- No emojis.\n",
                               ["no_dashes", "no_emojis"], [], None, None])

    def test_recommended_rules_go_after_your_own(self):
        # never in place of your text: only the lines it doesn't have, under their headings, and twice is the same as once
        got = self.run_js("""
          const rules = "# Working style\\n\\n- Ask.\\n- Be brief.\\n\\n# Git\\n\\n- Don't push.\\n";
          const mine = "# Mine\\n- Use tabs.\\n- Be brief.";
          const once = appendRules(mine, rules);
          console.log(JSON.stringify([appendRules("", rules), appendRules("  \\n", rules), once, appendRules(once, rules),
            appendRules(mine + "\\n- Ask.\\n- Don't push.", rules)]));
        """)
        self.assertEqual(got[0], "# Working style\n\n- Ask.\n- Be brief.\n\n# Git\n\n- Don't push.\n")
        self.assertEqual(got[1], got[0])
        self.assertEqual(got[2], "# Mine\n- Use tabs.\n- Be brief.\n\n# Working style\n\n- Ask.\n\n# Git\n\n- Don't push.\n")
        self.assertEqual(got[3], got[2])
        self.assertEqual(got[4], "# Mine\n- Use tabs.\n- Be brief.\n- Ask.\n- Don't push.")  # nothing new: left exactly as it was

    def test_image_names_and_build_progress(self):
        got = self.run_js("""
          STATE = { presets: { full: { label: "All-in-one" } }, recipes: { rust: { label: "Rust" } }, images: [
            { name: "base-20261017-053552.qcow2", label: "AIO", preset: "custom", toolchains: ["rust"], built: "2026-10-17T05:35:52Z" },
            { name: "base-20261018-122803.qcow2", label: "", preset: "full", toolchains: [], built: "2026-10-18T12:28:03Z" }] };
          console.log(JSON.stringify([imageNamed("base-20261017-053552.qcow2"), imageNamed("base-20261018-122803.qcow2"), imageNamed("base-gone.qcow2"),
            buildNow({ step: 2, progress: { label: "Rust", part: 3, of: 9 } }), buildNow({ step: 2, progress: {} , steps: ["Preparing", "Installing"] }),
            buildNow({ step: 3 }), buildStepText({ step: 2, steps: ["Preparing", "Installing", "Saving"], progress: { label: "Rust", part: 3, of: 9 } })]));
        """)
        self.assertEqual(got, ["AIO · Oct 17", "All-in-one · Oct 18", "base-gone", "Installing Rust (3 of 9)", "Installing",
                               "Saving the image", "Step 2 of 3: Installing Rust (3 of 9)"])

    def test_a_file_too_big_to_attach_is_refused_before_it_is_read(self):
        got = self.run_js("""
          const toasts = [];
          toast = (m, err) => toasts.push([m, !!err]);
          STATE = { agents_list: [{ id: "a1", name: "x", tasks: [] }], attachments: { max_file_bytes: 25 * 1024 * 1024, max_files: 2 } };
          UI.agentId = "a1";
          updateComposer = () => {};
          addAttach([{ name: "big.xapk", size: 263.3 * 1024 * 1024 }, { name: "a.png", size: 10 }, { name: "b.png", size: 10 }, { name: "c.png", size: 10 }]);
          console.log(JSON.stringify([composerState("a1").attach.map((f) => f.name), toasts]));
        """)
        self.assertEqual(got[0], ["a.png", "b.png"])
        self.assertEqual(got[1][0], ["big.xapk is 263.3 MiB. Attached files can be at most 25.0 MiB. For bigger ones, put them in a folder "
                                     "and give it to the agent as its Host folder on the Sandbox tab.", True])
        self.assertEqual(got[1][1], ["At most 2 files per message", True])

    def test_the_quick_switcher_lists_agents_projects_and_pages(self):
        got = self.run_js("""
          const ag = (id, name, key, extra) => Object.assign({ id, name, tasks: [], status: { key } }, extra);
          STATE = { agents_list: [ag("a1", "web-fixer", "waiting", { archived: true }), ag("a2", "coder", "running", { status: { key: "running", queued: 2 } })],
            projects: [{ id: "p1", name: "Settlers", pending: [] }] };
          const rows = (items) => items.map((it) => [it.label, it.kind, it.icon || ""]);
          const owner = switcherItems();
          const out = [rows(owner.slice(0, 4)), rows(owner.slice(4)).map((r) => r[0] + "/" + r[1]).join(", ")];
          out.push(rows(switcherMatches(owner, "SET")), rows(switcherMatches(owner, "settings net")), switcherMatches(owner, "zzz").length,
            switcherMatches(owner, "").length);
          STATE.role = "device";
          out.push(switcherItems().slice(2).map((it) => it.label).join(", "));
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [
            [["coder", "Running and 2 queued", "running"], ["web-fixer", "Waiting for you", "waiting"], ["Settlers", "Project", ""],
             ["Inbox", "Page", ""]],
            "New agent/Page, Pull requests/Page, Claude policy/Page, Settings/Page, Secrets/Settings, GitHub access/Settings, "
            "Local models/Settings, Network/Settings, VM images/Settings, VM limits/Settings, This PC/Settings, Speech to text/Settings, Devices/Settings, "
            "Appearance/Settings, Advanced/Settings",
            [["Settlers", "Project", ""], ["Settings", "Page", ""], ["Secrets", "Settings", ""], ["GitHub access", "Settings", ""],
             ["Local models", "Settings", ""], ["Network", "Settings", ""], ["VM images", "Settings", ""], ["VM limits", "Settings", ""],
             ["This PC", "Settings", ""],
             ["Speech to text", "Settings", ""], ["Devices", "Settings", ""], ["Appearance", "Settings", ""]],  # 12 at most
            [["Network", "Settings", ""]], 0, 12,
            "Settlers, Inbox, New agent, Pull requests, This device"])

    def test_leaving_a_local_model_asks_before_sending_the_conversation(self):
        got = self.run_js("""
          const t = (id, session, model) => ({ id, session, model, instructions: "hi", created: "2026-10-18T05:00:00Z" });
          const a = { id: "a1", tasks: [t("t1", "s-old", "sonnet"), t("t2", "s-now", "localmodel/qwen3.5:9b"), t("t3", "s-now", null)] };
          const asked = [], out = [];
          let answer = null;
          choiceModal = async (title, body) => { asked.push(body); return answer; };
          (async () => {
            out.push(await leavingLocal(a));                       // cancelled: the model stays
            answer = "continue"; out.push(await leavingLocal(a), composerState("a1").cont);
            answer = "new"; out.push(await leavingLocal(a), composerState("a1").cont);
            out.push(await leavingLocal(a));                       // a new conversation: nothing to ask
            composerState("a1").cont = true; composerState("a1").resume = "t1";
            out.push(await leavingLocal(a));                       // going back to one that ran on Claude
            console.log(JSON.stringify([out, asked.length, asked[0]]));
          })();
        """)
        self.assertEqual(got[0], [False, True, True, True, False, True, True])
        self.assertEqual(got[1], 3)
        self.assertIn("It ran on qwen3.5:9b, a model on your own server.", got[2])
        self.assertIn("including what the agent read in your files", got[2])

    def test_a_model_server_can_be_edited(self):
        """Edit shows a server's settings, filled in, and Save sends just those. The name stays, and so
        do its key and confirmed host key on the server's side. Going from SSH to direct asks first."""
        got = self.run_js("""
          const posts = [], toasts = [], asked = [];
          let saved = null;
          globalThis.api = async (m, u, b) => {
            if (m === "POST") posts.push([u, JSON.parse(JSON.stringify(b))]);
            if (u === "/api/models/servers") { saved = { rented: Object.assign({ api_key: false, secret: {} }, { url: b.url }, b.ssh ? { ssh: b.ssh } : {}) }; return { servers: saved, ssh: {} }; }
            return { servers: {} };
          };
          toast = (t) => toasts.push(t); confirmModal = async (title) => { asked.push(title); return true; };
          loadState = () => {}; renderSecretsSoon = () => {};
          STATE = { routes: { names: ["vpn"], kinds: { vpn: "wireguard" }, tor: {} } };
          const ssh = { host: "203.0.113.20", port: 22, user: "root", route: "direct", public_key: "ssh-ed25519 AAAA cilly-rented" };
          const T = modelsTab({ model_servers: { rented: { url: "http://127.0.0.1:11434", api_key: false, ssh } } });
          const tick = () => new Promise((r) => setTimeout(r, 0));
          const all = (n, f, out = []) => { if (n instanceof Node) { if (f(n)) out.push(n); n.kids.forEach((k) => all(k, f, out)); } return out; };
          const edit = () => find(T.node, "models-edit");
          const inEdit = (label) => all(edit(), (n) => n.attrs["aria-label"] === label)[0];
          const button = (root, text) => all(root, (n) => n.tag === "button" && n.textContent === text)[0];
          await tick();
          const before = !!edit();
          button(T.node, "Edit").fire("click");
          const filled = [inEdit("How to reach it").value, inEdit("SSH server").value, inEdit("SSH port").value, inEdit("SSH user").value, inEdit("Address").value];
          inEdit("SSH port").value = "2222"; inEdit("Address").value = "http://127.0.0.1:18000";
          button(edit(), "Save").fire("click");
          await tick(); await tick();
          const first = posts.filter(([u]) => u === "/api/models/servers").pop();
          const closed = !edit();
          button(T.node, "Edit").fire("click");
          inEdit("How to reach it").value = "direct";
          button(edit(), "Save").fire("click");
          await tick(); await tick();
          const second = posts.filter(([u]) => u === "/api/models/servers").pop();
          console.log(JSON.stringify([before, filled, first, closed, toasts, asked, second]));
        """)
        before, filled, first, closed, toasts, asked, second = got
        self.assertFalse(before)
        self.assertEqual(filled, ["ssh", "203.0.113.20", "22", "root", "http://127.0.0.1:11434"])
        self.assertEqual(first[1], {"name": "rented", "api_key": False, "url": "http://127.0.0.1:18000",
                                    "ssh": {"host": "203.0.113.20", "port": 2222, "user": "root", "route": "direct"}})
        self.assertTrue(closed)
        self.assertIn("another SSH server now", toasts[0])
        self.assertEqual(asked, ["Reach rented directly?"])
        self.assertEqual(second[1], {"name": "rented", "api_key": False, "url": "http://127.0.0.1:18000"})

    def test_the_quick_switcher_keys(self):
        got = self.run_js("""
          const ag = (id, name) => ({ id, name, tasks: [], status: { key: "idle" } });
          STATE = { agents_list: [ag("a1", "one"), ag("a2", "two"), ag("a3", "three")], projects: [] };
          const went = [], out = [];
          go = (view, opts) => went.push(view + " " + (opts?.agentId || opts?.settingsTab || ""));
          const field = () => SWITCHER.kids[0].kids[0], list = () => SWITCHER.kids[0].kids[1];
          openSwitcher();
          out.push(field().attrs.placeholder, list().kids.length, list().kids[0].attrs["aria-selected"], SWITCHER.kids[0].kids[2].textContent);
          field().fire("keydown", { key: "ArrowDown" }); field().fire("keydown", { key: "ArrowDown" });
          field().fire("keydown", { key: "Enter" });
          out.push(SWITCHER);
          openSwitcher(); field().fire("keydown", { key: "ArrowUp" }); field().fire("keydown", { key: "Enter" });  // wraps to the last
          openSwitcher(); field().value = "zzz"; field().fire("input"); out.push(list().textContent);
          field().fire("keydown", { key: "Enter" }); out.push(!!SWITCHER);   // nothing to open
          const esc = SWITCHER.kids[0].fire("keydown", { key: "Escape", target: field() }); out.push(SWITCHER, !!esc.stopped);
          openSwitcher(); field().value = "secr"; field().fire("input"); field().fire("keydown", { key: "Enter" });
          console.log(JSON.stringify([out, went]));
        """)
        self.assertEqual(got, [["Go to an agent, project or page…", 12, "true", "↑↓ to moveEnter to openEsc to close",
                                None, "Nothing matches.", True, None, True],
                               ["agent a3", "settings network", "settings secrets"]])  # the 12th item: the list shows 12

    def test_the_quick_switcher_keeps_focus_and_closes_on_esc(self):
        got = self.run_js("""
          STATE = { agents_list: [{ id: "a1", name: "one", tasks: [], status: { key: "idle" } }], projects: [] };
          const out = [], focused = [];
          go = () => {};
          const from = new Node("textarea"); from.focus = () => focused.push("from");
          document.activeElement = from;
          openSwitcher();
          const dialog = SWITCHER.kids[0], field = dialog.kids[0], row = dialog.kids[1].kids[0];
          field.focus = () => focused.push("field");
          const tab = dialog.fire("keydown", { key: "Tab", target: row, shiftKey: true });
          out.push(!!tab.prevented, focused.splice(0).join());
          for (const f of document.ls.focusin) f({ target: new Node("button") });     // focus wandered into the page
          for (const f of document.ls.focusin) f({ target: row });                    // inside is fine
          out.push(focused.splice(0).join());
          const down = dialog.fire("mousedown", { target: dialog.kids[2] });           // a click keeps focus on the field
          out.push(!!down.prevented, !!dialog.fire("mousedown", { target: field }).prevented);
          const esc = dialog.fire("keydown", { key: "Escape", target: row });
          out.push(SWITCHER, !!esc.stopped, focused.splice(0).join(), document.ls.focusin.length);
          openSwitcher();                                                              // Esc with focus outside, on the page
          for (const f of document.ls.keydown) f({ key: "Escape", preventDefault() {} });
          out.push(SWITCHER, document.ls.focusin.length, focused.splice(0).join());
          openSwitcher(); SWITCHER.kids[0].kids[1].kids[0].fire("click");             // picking one leaves focus to the page it opens
          out.push(SWITCHER, focused.splice(0).join());
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [True, "field", "field", True, False, None, True, "from", 0, None, 0, "from", None, ""])

    def test_the_switcher_key_is_cmd_k_on_a_mac_and_ctrl_k_elsewhere(self):
        got = self.run_js("""
          const keys = [{ metaKey: true }, { ctrlKey: true }, { metaKey: true, ctrlKey: true }, { metaKey: true, shiftKey: true },
            { ctrlKey: true, altKey: true }, {}];
          const on = (mac, key) => keys.map((k) => !!switcherKey(Object.assign({ key }, k), mac));
          console.log(JSON.stringify([on(true, "k"), on(false, "k"), on(true, "K"), on(false, "j")]));
        """)
        self.assertEqual(got, [[True, False, False, False, False, False], [False, True, False, False, False, False],
                               [True, False, False, False, False, False], [False] * 6])

    def test_rename_in_place(self):
        got = self.run_js("""
          const saved = [], out = [];
          const start = (name, value, ...events) => {
            const box = new Node("div"), shown = new Node("span"); box.append(shown);
            const f = editName(shown, name, (n) => saved.push(n));
            out.push(box.kids[0] === f);
            f.value = value;
            const evs = events.map(([t, key]) => f.fire(t, { key }));
            out.push(box.kids[0] === shown);
            return evs;
          };
          start("coder", "  builder ", ["keydown", "Enter"], ["blur"]);   // Enter saves, once
          const [esc] = start("coder", "other", ["keydown", "Escape"], ["blur"]);  // Esc keeps the old name
          out.push(!!esc.stopped);
          start("coder", "away", ["blur"]);                                 // clicking away saves
          start("coder", "   ", ["blur"]);                                  // empty: nothing to save
          start("coder", "coder ", ["keydown", "Enter"]);                   // unchanged
          console.log(JSON.stringify([out, saved]));
        """)
        self.assertEqual(got, [[True, True, True, True, True, True, True, True, True, True, True], ["builder", "away"]])

    def test_redraws_wait_while_you_rename(self):
        got = self.run_js("""
          const ag = { id: "a1", name: "coder", state: "ready", running: true, tasks: [], repo: "me/app", branch: "agent/x1", queue: [],
            status: { key: "idle" }, mode: "persistent" };
          STATE = { agents_list: [ag], projects: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {}, policy_version: 1 };
          const out = [], renamed = [];
          renameAgent = (a, name) => renamed.push(name);
          const box = $("sb-agents");
          renderSidebar();
          const row = box.kids.find((k) => k.className.startsWith("sb-row"));
          const f = row.rename();
          document.activeElement = f;
          ag.name = "from-elsewhere"; renderSidebar(); out.push(box.redraws, box.contains(f));
          f.value = "builder"; f.fire("keydown", { key: "Enter" }); document.activeElement = null;
          renderSidebar(); out.push(box.redraws);

          AV = { id: "a1", head: new Node("div"), sub: new Node("div") };
          renderHead(ag); out.push(AV.head.redraws, AV.name.attrs.title);
          const hf = (renameInHeader(ag), AV.head.kids[0]);
          document.activeElement = hf;
          ag.name = "again"; renderHead(ag); out.push(AV.head.redraws, hf.className);
          hf.value = "header-name"; hf.fire("blur"); document.activeElement = null;
          renderHead(ag); out.push(AV.head.redraws);
          console.log(JSON.stringify([out, renamed]));
        """)
        self.assertEqual(got, [[1, True, 2, 1, "Click to rename. ID a1", 1, "input rename head-rename", 2], ["builder", "header-name"]])

    def test_menu_rename_finds_the_row_after_a_redraw(self):
        got = self.run_js("""
          const ag = (id, name) => ({ id, name, state: "ready", running: true, tasks: [], repo: "me/app", queue: [], status: { key: "idle" }, mode: "persistent" });
          STATE = { agents_list: [ag("a1", "coder"), ag("a2", "other")], projects: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {}, policy_version: 1 };
          openPop = (anchor, node) => { POP = { node, anchor }; };
          const box = $("sb-agents"), out = [];
          const rows = () => box.kids.filter((k) => k.className.startsWith("sb-row"));
          const menuFor = (id) => { const r = rows().find((r) => r.agentId === id); r.kids[1].fire("click"); return POP.node; };
          renderSidebar();
          let menu = menuFor("a1");
          STATE.agents_list[0].status = { key: "running" }; renderSidebar();   // a poll redraws under the open menu
          menu.kids[0].fire("click");  // Rename
          const live = rows().find((r) => r.agentId === "a1");
          out.push(box.redraws, live.kids[0].className, !!find(live, "rename"));

          menu = menuFor("a2");
          STATE.agents_list.pop(); SIGS.sbAgents = null; renderSidebar();    // gone by the time you click Rename
          menu.kids[0].fire("click");  // Rename
          out.push(rows().length);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [2, "sb-agent editing", True, 1])

    def test_tapping_the_open_agent_closes_the_drawer(self):
        got = self.run_js("""
          const ag = (id, name) => ({ id, name, state: "ready", running: true, tasks: [], repo: "me/app", queue: [], status: { key: "idle" }, mode: "persistent" });
          STATE = { agents_list: [ag("a1", "coder"), ag("a2", "other")], projects: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {}, policy_version: 1 };
          Object.assign(UI, { view: "agent", agentId: "a1", tab: "chat", drawer: true });
          const went = [], out = [];
          go = (view, opts) => went.push(opts.agentId);
          renderSidebar();
          const btn = (id) => $("sb-agents").kids.find((r) => r.agentId === id).kids[0];
          btn("a1").fire("click"); out.push(UI.drawer, $("backdrop").hidden, went.length);
          UI.drawer = true; btn("a2").fire("click");
          console.log(JSON.stringify([out, went]));
        """)
        self.assertEqual(got, [[False, True, 0], ["a2"]])

    def test_the_branch_line_copies_the_branch(self):
        got = self.run_js("""
          const ag = { id: "a1", name: "coder", state: "ready", running: true, tasks: [], repo: "me/app", branch: "agent/bd856a9f",
            status: { key: "idle" }, mode: "persistent" };
          STATE = { agents_list: [ag], projects: [], policy_version: 1 };
          const out = [], copied = [];
          toast = (msg, err) => out.push([msg, !!err]);
          AV = { id: "a1", head: new Node("div"), sub: new Node("div") };
          renderHead(ag);
          const where = AV.sub.kids[0];
          out.push(where.tag, where.className, where.attrs.title, where.textContent);
          navigator.clipboard = { writeText: async (t) => { copied.push("api " + t); } };
          where.fire("click");
          await new Promise((r) => setTimeout(r));
          delete navigator.clipboard;                                     // plain HTTP: no Clipboard API
          document.execCommand = (c) => { copied.push(c + " " + document.body.kids.at(-1).value); return true; };
          where.fire("click");
          await new Promise((r) => setTimeout(r));
          document.execCommand = () => false;
          where.fire("click");
          await new Promise((r) => setTimeout(r));
          renderHead(Object.assign({}, ag, { repo: null, branch: null, id: "a1", name: "scratch" }));
          out.push(AV.sub.kids[0].tag);
          console.log(JSON.stringify([out, copied]));
        """.replace("(() => {", ""))
        self.assertEqual(got, [["button", "where copy ellipsis", "Click to copy the branch", "me/app · agent/bd856a9f",
                                ["Copied agent/bd856a9f", False], ["Copied agent/bd856a9f", False],
                                ["Couldn't copy. The branch is agent/bd856a9f", True], "span"],
                               ["api agent/bd856a9f", "copy agent/bd856a9f"]])

    def test_the_when_idle_choice_starts_folded_and_opens_on_a_tap(self):
        got = self.run_js("""
          STATE = { agents_list: [], projects: [], policy_version: 1 };
          const block = whenIdle({ id: "a1", name: "coder", mode: "persistent", auto_stop: false, idle_stop: 30, tasks: [] });
          const head = block.kids[0], body = block.kids[1], out = [[body.hidden, head.attrs["aria-expanded"], head.textContent]];
          head.fire("click"); out.push([body.hidden, head.attrs["aria-expanded"]]);
          head.fire("click"); out.push([body.hidden, head.attrs["aria-expanded"]]);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [[True, "false", "When it has nothing to do" + "Stop after 30 min" + "▸"], [False, "true"], [True, "false"]])

    def test_the_menu_says_what_it_does_when_idle(self):
        got = self.run_js("""
          const ag = (extra) => Object.assign({ id: "a1", name: "coder", state: "stopped", running: false, tasks: [], status: { key: "stopped" },
            mode: "persistent", has_disk: true, auto_stop: false, idle_stop: 0 }, extra);
          STATE = { agents_list: [], projects: [], policy_version: 1 };
          const out = [], calls = [];
          api = async (m, path, body) => { calls.push([path, body.value]); if (body.value === 120) throw new Error("no"); return {}; };
          loadState = async () => {}; toast = (msg, err) => out.push([msg, !!err]);
          let ok = false; confirmModal = async (title) => { out.push(title); return ok; };
          const click = async (b) => { b.fire("click"); await new Promise((r) => setTimeout(r)); };
          // what it shows is what applies: Stop After Each Task wins over an old timer, and research agents have no such thing
          out.push([ag({}), ag({ idle_stop: 30 }), ag({ auto_stop: true, idle_stop: 30 }), ag({ profile: "research", auto_stop: true, idle_stop: 15 })]
            .map(whenIdleOf));
          const block = whenIdle(ag({ mode: "ephemeral" })), body = block.kids[1], btn = body.kids[0].kids, help = body.kids[1];
          out.push(btn.map((b) => b.textContent).join(", "), help.textContent);
          await click(btn[1]); out.push(help.textContent);                // Stop right away
          await click(btn[1]);                                             // already on: nothing to send
          await click(btn[3]); out.push(help.textContent);                 // 30 min
          await click(btn[0]);                                             // Keep running
          await click(btn[2]);                                             // 15 min from Keep running loses files: asks, you say no
          ok = true; await click(btn[2]); out.push(help.textContent);
          await click(btn[5]); out.push(help.textContent);                 // refused: back to what it was
          const research = whenIdle(ag({ profile: "research" }));
          out.push(research.kids[1].kids[0].kids.map((b) => b.textContent).join(", "), research.kids[1].kids[0].className);
          const menu = (x) => agentMenu(ag(x)).textContent;
          out.push(menu({}).includes("When it has nothing to do"), menu({ profile: "research" }).includes("When it has nothing to do"),
            menu({}).includes("Reset to cleanDeletes the disk. The next start is fresh."), menu({ mode: "memory" }).includes("Clear memoryForget stored conversations and memory"),
            menu({ mode: "memory", running: true, state: "ready" }).includes("Clear memoryStop the agent first"),
            menu({}).includes("Clear memoryOnly for ephemeral + memory agents"), menu({}).includes("Stop after idle time"));
          console.log(JSON.stringify([out, calls]));
        """)
        self.assertEqual(got, [[
            ["keep", 30, "task", 15],
            "Keep running, Stop right away, 15 min, 30 min, 1 hour, 2 hours",
            "Stays on between tasks so the next message starts right away. Uses memory while idle.",
            ["coder stops when it has nothing left to do", False],
            "Stops after the last task and queued message. Ephemeral agents lose their disk.",
            ["coder stops after 30 min with nothing to do", False],
            "Stops after 30 min with no task, no queued message and nothing in the inbox. Everything in it is deleted.",
            ["coder keeps running until you stop it", False],
            "Stop coder when idle?", "Stop coder when idle?",
            ["coder stops after 15 min with nothing to do", False],
            "Stops after 15 min with no task, no queued message and nothing in the inbox. Everything in it is deleted.",
            ["no", True],
            "Stops after 15 min with no task, no queued message and nothing in the inbox. Everything in it is deleted.",
            "Keep running, 15 min, 30 min, 1 hour, 2 hours", "seg when-idle no-task",
            True, True, True, True, True, True, False],
            [["/api/agents/a1/when-idle", "task"], ["/api/agents/a1/when-idle", 30], ["/api/agents/a1/when-idle", "keep"],
             ["/api/agents/a1/when-idle", 15], ["/api/agents/a1/when-idle", 120]]])

    def test_a_project_name_is_a_field(self):
        got = self.run_js("""
          const p = { id: "p1", name: "Settlers" }, calls = [];
          updateProject = async (pid, body, ok) => { calls.push([pid, body, ok]); return body.name === "Bad!" ? null : {}; };
          const f = projectTitle(p);
          f.value = "Other"; const esc = f.fire("keydown", { key: "Escape" });
          const after = [f.value, !!esc.stopped];
          f.value = "  Settlers 2 "; f.fire("keydown", { key: "Enter" });
          f.value = "Settlers"; f.fire("blur");                            // unchanged
          f.value = "Bad!"; f.fire("blur");
          await new Promise((r) => setTimeout(r));
          console.log(JSON.stringify([after, calls, f.value, f.className]));
        """.replace("(() => {", ""))
        self.assertEqual(got, [["Settlers", True], [["p1", {"name": "Settlers 2"}, "Renamed to Settlers 2"],
                                                     ["p1", {"name": "Bad!"}, "Renamed to Bad!"]], "Settlers", "page-title title-field"])

    COMPOSER = r"""
      document.createElementNS = (ns, t) => new Node(t);  // the mic's icon
      const at = (secs) => new Date(Date.now() - secs * 1000).toISOString();
      const a = { id: "a1", name: "coder", state: "busy", running: true, mode: "persistent", repo: "me/app", queue: [], mcp: [], tasks: [] };
      STATE = { agents_list: [a], speech: { enabled: true, host: "gpu-box:8080" }, mcp_servers: {} };
      UI.agentId = "a1";
      AV = { id: "a1" };
      const nodes = composer(a);
      const redraw = () => { SIGS = {}; updateComposer(a); };
    """

    def test_the_composer_chips_and_hint(self):
        got = self.run_js(self.COMPOSER + r"""
          const out = [nodes.map((n) => n.className)];
          const chips = () => AV.c.chips.kids.filter((k) => k.tag).map((k) => [k.textContent, k.className]);
          const hint = () => [AV.c.caption.textContent, AV.c.caption.hidden, AV.c.ta.placeholder];
          redraw(); out.push(chips(), hint());
          const c = composerState("a1");
          c.cont = false; redraw(); out.push(chips()[0]);
          c.cont = true; a.tasks = [{ id: "t1", state: "done", instructions: "x", session: "s1" }]; c.resume = "t1"; redraw(); out.push(chips()[0]);
          c.resume = null; a.model = "opus"; a.effort = "xhigh"; redraw(); out.push(chips()[1]);
          a.model = "claude-opus-5-5"; a.effort = null; redraw(); out.push(chips()[1]);
          // a local model: the last part of its name, all of it on hover
          a.model = "hf.co/huihui-ai/Qwen-27B-GGUF:Q8_0"; a.model_server = "vast"; a.effort = "medium"; redraw();
          out.push([...chips()[1], AV.c.chips.kids.filter((k) => k.tag)[1].attrs.title]);
          a.model = null; a.model_server = null; a.effort = null;
          c.attach = [{ name: "a.txt", size: 3 }]; redraw(); out.push(hint());
          VOICE.on = true; VOICE.area = AV.c.ta; AV.c.mic.onVoice(); out.push(hint());  // recording starts: no poll needed
          VOICE.on = false; AV.c.mic.onVoice(); c.attach = []; redraw(); out.push(hint());
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [
            ["jump", "asks", "running-strip", "queued-cards", "composer", "caption"],  # ask, subagents, queued, then the box
            [["Continue conversation ▾", "chip"], ["Default model ▾", "chip model-chip"], ["New branch", "chip"]],  # nothing teal by default
            ["", True, "Message coder…"],
            ["New conversation ▾", "chip on"], ["Earlier conversation ▾", "chip on"],
            ["Opus · X-high ▾", "chip model-chip on"], ["claude-opus-5-5 ▾", "chip model-chip on"],
            ["Qwen-27B-GGUF:Q8_0 · vast · Medium ▾", "chip model-chip on",
             "Model and effort for the next message: hf.co/huihui-ai/Qwen-27B-GGUF:Q8_0 · vast · Medium"],
            ["Files are copied in read-only at /attachments.", False, "Message coder…"],
            ["Audio goes to whisper on gpu-box:8080, nowhere else.", False, "Listening…"],
            ["", True, "Message coder…"]])

    def test_talking_never_turns_the_send_button_into_stop(self):
        """While a task runs, an empty message box offers Stop this task where Send is. While you record,
        and while your words are turned into text, the box is empty but your message is coming: the
        button there must queue it, not stop the task (and its subagents) at a tap."""
        got = self.run_js(self.COMPOSER + r"""
          const btn = () => [AV.c.send.kids[0].attrs["aria-label"] || AV.c.send.kids[0].textContent, AV.c.send.kids[0].disabled];
          const c = composerState("a1"), out = [];
          redraw(); out.push(btn());                                  // nothing typed: stop offered
          VOICE.on = true; VOICE.area = AV.c.ta; AV.c.mic.onVoice(); out.push(btn());   // recording
          VOICE.on = false; AV.c.mic.onVoice(); out.push(btn());      // stopped, still transcribing
          c.draft = "fix the login timeout"; redraw(); out.push(btn());   // its text is in
          VOICE.area = null; c.draft = ""; AV.c.mic.onVoice(); out.push(btn());   // done, and you cleared it: stop again
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [["Stop this task", False], ["Queue ↑", True], ["Queue ↑", True], ["Queue ↑", False], ["Stop this task", False]])

    def test_recording_shows_no_running_text(self):
        """The red mic says it's recording. No "Recording for 2 s" line crowds the chips on a phone."""
        got = self.run_js(self.COMPOSER + r"""
          VOICE.on = true; VOICE.area = AV.c.ta; AV.c.mic.onVoice();
          const walk = (n, f) => n instanceof Node && (f(n) || n.kids.some((k) => walk(k, f)));
          const box = AV.c.box;
          console.log(JSON.stringify([walk(box, (n) => n.className.split(" ").includes("rec")), /Recording|Transcribing/.test(box.textContent), "rec" in AV.c]));
        """)
        self.assertEqual(got, [False, False, False])

    def test_a_file_alone_can_be_sent(self):
        got = self.run_js(self.COMPOSER + r"""
          const btn = () => AV.c.send.kids[0];
          const c = composerState("a1");
          a.state = "busy"; c.attach = [{ name: "shot.png", size: 3 }]; redraw(); const busy = [btn().textContent, btn().disabled];
          a.state = "idle"; c.draft = ""; c.attach = []; redraw(); const empty = btn().disabled;
          c.attach = [{ name: "shot.png", size: 3 }]; redraw(); const file = btn().disabled;
          const sent = [];
          api = async (m, path, body) => { sent.push([path, body]); return path.endsWith("/uploads") ? { id: "u1" } : { task: "t1" }; };
          fileToBase64 = async () => "eA==";
          loadState = async () => {}; pollAgent = () => {}; toast = () => {};
          await sendMessage();
          console.log(JSON.stringify([busy, empty, file, sent.map(([p, b]) => [p.split("/").pop(), b.instructions ?? null, b.attachments ?? null])]));
        """)
        busy, empty, file, sent = got
        self.assertEqual(busy, ["Queue ↑", False])  # while a task runs, a file alone queues instead of offering Stop
        self.assertEqual((empty, file), (True, False))  # nothing to send, then a screenshot alone
        self.assertEqual(sent, [["uploads", None, None], ["message", "", ["u1"]]])  # the panel words it for the agent

    def test_files_dropped_anywhere_and_in_the_first_message(self):
        got = self.run_js(r"""
          STATE = { attachments: { max_file_bytes: 100, max_files: 2 }, agents_list: [] };
          const cls = () => { const set = new Set(); return { add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c), toggle() {} }; };
          const fire = (type, files, types = ["Files"]) => { let stopped = false;
            const e = { dataTransfer: { types, files, dropEffect: "" }, preventDefault() { stopped = true; } };
            for (const f of document.ls[type] || []) f(e); return stopped; };
          const out = [];
          // New agent: dragging files over any part of the window outlines its message box, and a drop adds them
          UI.view = "new";
          VIEWS.new.p = { box: el("div"), attachRow: el("div") };
          VIEWS.new.p.box.classList = cls();
          const f1 = { name: "shot.png", size: 10 }, big = { name: "huge.bin", size: 1000 };
          fire("dragenter", []); out.push(VIEWS.new.p.box.classList.contains("drop"));
          out.push(fire("dragover", []));  // taken, or the browser would open the file
          out.push(fire("drop", [f1, big]), VIEWS.new.p.box.classList.contains("drop"), NEW.attach.map((f) => f.name));
          out.push(fire("dragover", [], ["text/plain"]));  // dragged text isn't a file: left alone
          // the agent is made without its first message, which then goes like any message, files and all
          const calls = [];
          api = async (m, path, body) => { calls.push([path, body]); return { id: "abcd1234" }; };
          loadState = async () => {}; go = () => {}; let sent = null;
          sendMessage = async () => { sent = Object.assign({}, composerState("abcd1234")); };
          VIEWS.new.p.inject = { get: async () => null, clear() {} }; VIEWS.new.p.cpus = { value: 2 }; VIEWS.new.p.mem = { value: 2048 };
          VIEWS.new.p.keepBox = { hidden: true }; VIEWS.new.update = () => {};
          NEW.text = "Fix what's in the screenshot"; NEW.name = "fixer";
          await create();
          out.push(calls[0][1].instructions, sent.draft, sent.attach.map((f) => f.name), sent.cont, NEW.attach.length);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [True, True, True, False, ["shot.png"], False,
                               "", "Fix what's in the screenshot", ["shot.png"], False, 0])

    def test_the_model_menu_warns_of_mcp_tools_on_your_own_models(self):
        got = self.run_js(self.COMPOSER + r"""
          STATE.model_servers = [];
          const text = (n) => n.textContent || "";
          const warned = (x) => text(modelPop(Object.assign({}, a, x))).includes("every MCP server sends all its tools");
          console.log(JSON.stringify([warned({ model: "qwen3:8b", model_server: "vast", mcp: ["rea"] }),
            warned({ model: "qwen3:8b", model_server: "vast", mcp: [] }), warned({ model: "opus", mcp: ["rea"] })]));
        """)
        self.assertEqual(got, [True, False, False])  # Claude's models load them only when needed

    def test_long_pasted_text_goes_along_as_a_file(self):
        """Text copied out of a chat can hold whole commands and their output, far past what a message takes."""
        got = self.run_js(self.COMPOSER + r"""
          const toasts = []; toast = (m) => toasts.push(m);
          const paste = (text) => AV.c.ta.fire("paste", { clipboardData: { files: [], getData: () => text } }).prevented || false;
          const c = composerState("a1");
          const out = [paste("short"), c.attach.length, paste("x".repeat(50001)), c.attach.map((f) => [f.name, f.size, f.type])];
          console.log(JSON.stringify([...out, toasts, tooLong(61234)]));
        """)
        self.assertEqual(got, [False, 0, True, [["pasted-text.txt", 50001, "text/plain"]],
                               ["The pasted text has 50,001 characters, so it's attached as pasted-text.txt"],
                               "This message has 61,234 characters, and one can have at most 50,000. Put the long part in a file and attach it with +."])

    def test_queued_messages_wait_in_cards(self):
        got = self.run_js(self.COMPOSER + r"""
          const q = (id, instructions, extra) => Object.assign({ id, state: "queued", instructions, created: at(5) }, extra);
          a.tasks = [{ id: "t1", state: "running", instructions: "first", created: at(60), started: at(60) },
            q("t2", "second\nwith more\tlines", { attachments: ["a.png", "b.txt"] }), q("t3", "third"), q("t4", "attach", { attach: "me/app" }),
            { id: "t5", state: "cancelled", instructions: "taken back", created: at(9) }];
          a.queue = ["t3", "t2", "t4"];
          const cards = () => [AV.c.queued.hidden, AV.c.queued.kids.filter((k) => k.tag).map((k) => k.textContent)];
          const out = [];
          redraw(); out.push(cards());
          a.state = "booting"; redraw(); out.push(cards()[1][0]);
          window.matchMedia = () => ({ matches: true }); redraw(); out.push(cards()); window.matchMedia = () => ({ matches: false });
          a.tasks = a.tasks.filter((t) => t.state !== "queued"); a.queue = []; redraw(); out.push(cards());
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [
            [False, ["Queued. It runs after the current task.thirdEditCancelSend now",  # in queue order, text on one line
                     "Queued. It runs after the one above.second with more lines2 filesEditCancelSend now"]],  # the repository attach stays in the chat
            "Queued. Sends when the VM is ready.thirdEditCancelSend now",
            [False, ["Queued. Sends when the VM is ready.thirdEditCancelSend nowSend it before the other messages", "1 more queued"]],  # a phone has no hover: the tip is shown
            [True, []]])

    def test_send_now_says_what_happens(self):
        got = self.run_js(self.COMPOSER + r"""
          const q = (id, extra) => Object.assign({ id, state: "queued", instructions: "msg " + id, created: at(5) }, extra);
          a.tasks = [{ id: "t1", state: "running", instructions: "first", created: at(60), started: at(60), live: true },
            q("t2"), q("t3", { attachments: ["a.png"] })];
          a.queue = ["t2", "t3"]; a.deliver = ["t2"];
          const all = (n) => [n, ...(n.kids || []).flatMap(all)];
          const tips = () => all(AV.c.queued).filter((n) => n.tag === "button" && n.textContent === "Send now").map((n) => n.attrs.title || n.title);
          const out = [];
          redraw(); out.push(tips());
          const asked = [], toasts = [];
          confirmModal = async (title, body) => { asked.push(body); return false; };
          const bodies = [];
          api = async (m, url, body) => { bodies.push(body); return { result: "delivered" }; };
          toast = (m) => toasts.push(m); loadState = async () => {}; pollAgent = () => {};
          (async () => {
            await sendNow(a, a.tasks[1]);  // goes in: nothing to confirm, nothing stops
            await sendNow(a, a.tasks[2]);  // with a file: it would stop the task, so it asks
            a.state = "ready"; a.deliver = []; redraw(); out.push(tips());
            out.push(asked, toasts, bodies);
            // in the chat, the delivered message sits inside the running task's block
            globalThis.renderMarkdown = (text) => new Node("#text", text);  // md.js isn't loaded here
            Object.defineProperty(Node.prototype, "dataset", { get() { return this.ds || (this.ds = {}); } });
            const block = taskBlock(a, a.tasks[0], { evs: [{ seq: 1, task: "t1", k: "you", msg: "t2", text: "cut" },
              { seq: 2, task: "t1", k: "you", msg: "gone", text: "kept start" }], reqs: [], dels: [], denied: [], asks: [] });
            out.push(block.filter((n) => n.className === "msg-you").map((n) => n.textContent));
            // the Tasks tab says where it went, and when Claude Code didn't answer it
            a.tasks[1] = Object.assign({}, a.tasks[1], { state: "delivered", into: "t1", lost: "refused" });
            a.tasks[2] = Object.assign({}, a.tasks[2], { state: "delivered", into: "t1" });
            AV.pane = new Node("div"); SIGS = {}; renderTasks(a);
            out.push(all(AV.pane).filter((n) => /\btask-state\b/.test(n.className || "") && n.textContent.startsWith("Sent")).map((n) => n.textContent));
            console.log(JSON.stringify(out));
          })();
        """)
        self.assertEqual(got, [
            ["Claude reads it now. Background work keeps running.", "This stops the current task, then sends this message."],
            ["Send it before the other messages", "Send it before the other messages"],
            ["This stops the current task, and its background work too. What it did so far is kept. "
             "Your message runs next, in the same conversation."],
            ["Sent. Claude reads it now. Background work keeps running."],
            [{"deliver_only": True}],  # not asked, so the server must not stop the task
            ["first", "msg t2", "kept start"],
            ["Sent into task t1 while it ran", "Sent into task t1 while it ran. Claude Code refused it."]])

    def test_the_subagent_strip_opens_to_what_each_is_doing(self):
        got = self.run_js(self.COMPOSER + r"""
          a.tasks = [{ id: "t1", state: "running", instructions: "go", created: at(90), started: at(90) }];
          CHAT.a1 = { last: 6, events: [
            { seq: 1, task: "t1", k: "tool", id: "ag1", tool: "Agent", verb: "Delegated", target: "Find the recipe API", sub: "Explore", ts: at(62) },
            { seq: 2, task: "t1", k: "tool", id: "c1", tool: "Grep", verb: "Searched", target: "Recipe", parent: "ag1", ts: at(50) },
            { seq: 3, task: "t1", k: "tool", id: "b1", tool: "Bash", verb: "Ran", target: "./gradlew test", bg: true, ts: at(18) },
            { seq: 4, task: "t1", k: "sub", id: "b1", state: "running", desc: "./gradlew test", ts: at(17) },
            { seq: 5, task: "t1", k: "sub", id: "x9", state: "running", desc: "Write docs", sub: "Writer", ts: at(3700) },
            { seq: 6, task: "t1", k: "sub", id: "x9", state: "running", desc: "Write docs", sub: "Writer", ts: at(1) }] };
          // a command not started in the background that Claude Code reports as a task: still a command, not a subagent
          const moved = subagentsOf([{ k: "tool", id: "s1", tool: "Bash", verb: "Ran", target: "sleep 40", ts: at(40) },
            { k: "sub", id: "s1", state: "running", desc: "sleep 40", ts: at(39) }], true).subs.s1;
          const strip = () => [AV.c.running.hidden, AV.c.running.kids.filter((k) => k.tag).map((k) => k.textContent)];
          const out = [];
          redraw(); out.push(strip());
          RUNNING_OPEN.add("a1"); renderRunning(a); out.push(strip());
          a.tasks[0].state = "done"; renderRunning(a); out.push(strip());
          out.push([moved.isAgent, moved.call?.tool, agentCard(moved, {}, true).textContent.slice(0, 18)]);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got.pop(), [False, "Bash", "Backgroundsleep 40"])
        self.assertEqual(got, [
            [False, ["2 subagents and 1 background command running: Explore, ./gradlew test, Writer▼"]],
            [False, ["2 subagents and 1 background command running: Explore, ./gradlew test, Writer▲",
                     "ExploreFind the recipe APISearched Recipe1:02shell./gradlew test0:18WriterWrite docs1:01:40"]],
            [True, []]])

    def test_new_agent_shows_its_shortcut(self):
        mount = r"""
          document.createElementNS = (ns, t) => new Node(t);
          Node.prototype.querySelector = () => new Node("div");
          STATE = { agents_list: [], projects: [], repos: [], speech: { enabled: false }, mcp_servers: {}, presets: {}, images: [], vm_defaults: {}, setup: {} };
          Object.defineProperty(globalThis, "navigator", { value: { platform: "Win32" }, configurable: true });  // read once, like the rest of the UI
          VIEWS.new.mount(new Node("main"));
          const P = VIEWS.new.p;
          console.log(JSON.stringify([P.create.textContent, P.create.attrs.title, P.ta.attrs.placeholder, modKey()]));
        """
        on = lambda platform: f'Object.defineProperty(globalThis, "navigator", {{ value: {{ platform: "{platform}" }}, configurable: true }});'
        self.assertEqual(self.run_js(mount, on("MacIntel")),
            ["Create and start⌘⏎", "⌘+Enter creates and starts it. Enter adds a new line.",
             "Describe the task, or leave it empty and send instructions later. Enter adds a new line. ⌘+Enter creates and starts it.", "⌘"])
        self.assertEqual(self.run_js(mount, on("Linux x86_64")),
            ["Create and startCtrl ⏎", "Ctrl+Enter creates and starts it. Enter adds a new line.",
             "Describe the task, or leave it empty and send instructions later. Enter adds a new line. Ctrl+Enter creates and starts it.", "Ctrl "])

    def test_new_agent_starts_without_a_repository(self):
        """Also in a project with repositories, so a private session never gets one by accident."""
        got = self.run_js(r"""
          document.createElementNS = (ns, t) => new Node(t);
          Node.prototype.querySelector = () => new Node("div");
          const proj = { id: "p1", name: "Lantern", repos: ["omni/respawn"], pending: [], defaults: { mcp: [] } };
          STATE = { agents_list: [], projects: [proj], repos: ["me/app", "omni/respawn"], speech: { enabled: false }, mcp_servers: {}, presets: {},
            images: [], vm_defaults: {}, setup: { steps: [], claude: "pasted" }, tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 } };
          Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux" }, configurable: true });
          VIEWS.new.mount(new Node("main")); VIEWS.new.update(true);
          const P = VIEWS.new.p, opts = () => P.repo.kids.filter((k) => k.tag === "option" && !k.disabled).map((k) => k.textContent);
          const out = [P.repo.value, opts()];
          NEW.project = "p1"; VIEWS.new.update(true);
          out.push(P.repo.value, opts());
          NEW.repo = "me/app"; VIEWS.new.update(true);
          out.push(P.repo.value);  // what you picked stays
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, ["", ["No repository (scratch folder)", "me/app", "omni/respawn"],
                               "", ["No repository (scratch folder)", "omni/respawn", "me/app"], "me/app"])

    def test_new_agent_offers_an_account_only_for_a_shared_repository(self):
        got = self.run_js("""
          document.createElementNS = (ns, t) => new Node(t);
          Node.prototype.querySelector = () => new Node("div");
          const proj = { id: "p1", name: "Acme", repos: ["me/app"], pending: [], defaults: { mcp: [], account: "work" } };
          STATE = { agents_list: [], projects: [proj], repos: ["me/app", "me/solo"], speech: { enabled: false }, mcp_servers: {}, presets: {},
            images: [], vm_defaults: {}, setup: { steps: [], claude: "pasted" }, tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 },
            accounts: [{ token: "bot", name: "cillybox", repos: ["me/app", "me/solo"] }, { token: "work", name: "acme-agent", repos: ["me/app"] }] };
          Object.defineProperty(globalThis, "navigator", { value: { platform: "Linux" }, configurable: true });
          VIEWS.new.mount(new Node("main"));
          const P = VIEWS.new.p, opts = () => P.account.kids.map((k) => k.textContent);
          const out = [];
          NEW.repo = "me/solo"; VIEWS.new.update(true);
          out.push(!!P.accountRow.hidden);  // one account has it
          NEW.repo = "me/app"; VIEWS.new.update(true);
          out.push(!!P.accountRow.hidden, opts(), P.account.value);
          NEW.project = "p1"; VIEWS.new.update(true);  // the project's account
          out.push(P.account.value);
          let body = null;
          api = async (m, path, b) => { body = b; return { id: "abcd1234" }; };
          loadState = async () => {}; go = () => {};
          P.inject = { get: async () => null, clear() {} };
          await create();
          out.push(body.account, body.repo);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [True, False, ["cillybox (default)", "acme-agent"], "bot", "work", "work", "me/app"])

    def test_secrets_list_order_and_names(self):
        got = self.run_js("""
          const st = { "token:bot": "pasted", "panel_token": "missing", "mcp:PIXELLAB_API_KEY": "missing",
            "agent:claude:CLAUDE_CODE_OAUTH_TOKEN": "command (not loaded yet)", "research:CLAUDE_CODE_OAUTH_TOKEN": "saved on this PC" };
          const cfg = { agents: { claude: { label: "Claude Code" } }, tokens: { bot: { repos: ["me/app"] } } };
          const list = secretList(st, cfg);
          const agents = [{ name: "coder", repo: "me/app", agent: "claude", mcp: ["pixellab"] }, { name: "r", profile: "research" }];
          const lib = { pixellab: { secrets: ["PIXELLAB_API_KEY"] } };
          console.log(JSON.stringify([list.map((s) => s.key), list.map((s) => s.name), secretSummary(list),
            list.map((s) => secretUsers(s.key, cfg, agents, lib)),
            storedText("pasted", { pasted: null }, ""), storedText("command (not loaded yet)", {}, "pass show gh"), storedText("missing", {}, "")]));
        """)
        self.assertEqual(got[0], ["mcp:PIXELLAB_API_KEY", "agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "panel_token",
                                  "research:CLAUDE_CODE_OAUTH_TOKEN", "token:bot"])  # missing, locked, then set (an optional one isn't missing)
        self.assertEqual(got[1], ["PIXELLAB_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "Panel GitHub token", "CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN"])
        self.assertEqual(got[2], "5 secrets: 1 missing, 1 locked until an agent needs it")
        self.assertEqual(got[3], ["Used by coder (MCP: pixellab)", "Used by coder", "Used by the Pull requests page", "Used by r", "Used by coder"])
        self.assertEqual(got[4:], ["Pasted, kept in memory", "From the command pass show gh, not read yet", ""])

    def test_repo_list_never_drops_a_configured_repo(self):
        got = self.run_js("""
          const listed = [{ full_name: "me/app", permissions: { push: true } }, { full_name: "me/ro", permissions: { pull: true } }];
          console.log(JSON.stringify(repoRows(listed, new Set(["me/app", "me/old"])).map((r) => [r.full_name, r.usable, !!r.missing])));
        """)
        self.assertEqual(got, [["me/app", True, False], ["me/ro", False, False], ["me/old", True, True]])

    def test_never_reach_takes_addresses_only(self):
        got = self.run_js("""
          console.log(JSON.stringify(["203.0.113.7", "2001:db8::/32", "10.0.0.0/8", "example.com", "*.ngrok.io", "1.2.3.4 x"].map(looksLikeAddress)));
        """)
        self.assertEqual(got, [True, True, True, False, False, False])

    def test_inbox_device_and_note_items(self):
        got = self.run_js("""
          const dev = { kind: "device", name: "Work laptop", code: "004213" }, note = { kind: "note", project_name: "Lantern", agent: null };
          console.log(JSON.stringify([itemTitle(dev), itemSummary(dev), itemFrom(dev), KIND.device, itemTitle(note)]));
        """)
        self.assertEqual(got, ["Pair Work laptop?", "Check that it shows the code 004 213", "A new device", ["Device", "lavender"], "Add a note to Lantern?"])

    def test_a_device_is_paired_only_from_its_review_page(self):
        got = self.run_js("""
          STATE = { role: "owner" };
          const card = inboxCard({ kind: "device", key: "dev:d1", id: "d1", name: "Work laptop", code: "004213" });
          const all = (n) => [n, ...n.kids.flatMap((k) => k.kids ? all(k) : [])];
          console.log(JSON.stringify(all(card).filter((n) => n.tag === "button").map((n) => n.textContent)));
        """)
        self.assertEqual(got, ["Deny", "Review"])
        src = "".join(read(os.path.join(STATIC, f)) for f in FILES)
        # the one place that approves: reviewDevice, next to the whole code
        self.assertEqual(src.count("decideDevice(it, true)"), 1)
        self.assertIn("done(await decideDevice(it, true))", read(os.path.join(STATIC, "inbox.js")).split("function reviewDevice")[1].split("\nfunction ")[0])
        self.assertNotIn("/approve`", read(os.path.join(STATIC, "pages.js")).split("async function refreshPaired")[1].split("\nfunction ")[0])
        self.assertNotIn("read files", src)

    def test_vm_image_rows_stack_on_phones(self):
        css = read(os.path.join(STATIC, "style.css"))
        phone = "".join(part.split("\n}\n")[0] for part in css.split("@media %s {" % PHONE_MQ)[1:])
        self.assertIn(".image-line { grid-template-columns: minmax(0, 1fr) 36px;", phone)
        self.assertIn(".image-cards { grid-template-columns: 1fr; }", phone)

    def test_only_the_owner_can_add_an_mcp_server(self):
        got = self.run_js("""
          const text = (role) => { STATE = { role, mcp_servers: {} }; return mcpControls({ id: "a1", name: "x", mcp: [] }).map((n) => n.textContent).join("|"); };
          console.log(JSON.stringify([text("owner").includes("+ Add a server"), text("device").includes("+ Add a server")]));
        """)
        self.assertEqual(got, [True, False])

    def test_add_a_server_takes_a_new_key_by_name_and_value(self):
        got = self.run_js("""
          const all = (n) => [n, ...n.kids.flatMap((k) => k.kids ? all(k) : [])];
          const sent = [];
          globalThis.api = async (m, u, body) => { sent.push([m, u, body]); return { name: body.name, server: {}, note: "" }; };
          loadState = async () => {};
          const dialog = async (fill) => {
            document.body.kids = []; STATE = { role: "owner", can_save_secrets: true };
            mcpAddDialog("x", () => {});
            const nodes = all(document.body), by = (l) => nodes.find((n) => n.attrs["aria-label"] === l);
            for (const n of nodes) if (["input", "textarea"].includes(n.tag)) n.value = "";
            by("Sent as").value = "bearer";
            const btn = (t) => nodes.find((n) => n.tag === "button" && n.textContent === t);
            const keySw = nodes.find((n) => n.className.includes("switch") && n.parent?.textContent.includes("It needs an API key"));
            fill({ by, btn, keySw, type: (l, v) => { by(l).value = v; by(l).fire("input"); } });
            const add = btn("Add");
            const ready = !add.disabled;
            if (ready) { add.fire("click"); await Promise.resolve(); }
            return [ready, nodes.find((n) => n.attrs["aria-label"] === "Key").type];
          };
          const out = [];
          out.push(await dialog(({ type, keySw, by }) => { type("Name", "pixellab"); type("URL", "https://api.pixellab.ai/mcp"); keySw.fire("click");
            out.push(by("Key name").value); by("Sent as").value = "x-api-key"; type("Key", "pk-123"); }));
          out.push(await dialog(({ type, keySw, btn }) => { btn("Local").fire("click"); type("Name", "files"); type("Command", "npx -y srv /work"); keySw.fire("click"); type("Key name", "lower"); }));
          out.push(await dialog(({ type, keySw, btn }) => { btn("Local").fire("click"); type("Name", "files"); type("Command", "npx -y srv /work"); keySw.fire("click"); type("Key name", "FILES_TOKEN"); }));
          out.push(await dialog(({ type, btn }) => { btn("Paste a command").fire("click");
            type("claude mcp add command", 'claude mcp add ref https://mcp.ref.example/mcp -t http -H "Authorization: Bearer ref-123"'); }));
          console.log(JSON.stringify([out, sent]));
        """)
        out, sent = got
        self.assertEqual(out[0], "PIXELLAB_API_KEY")  # suggested from the name
        self.assertEqual(out[1:], [[True, "password"], [False, "password"], [True, "password"], [True, "password"]])  # a key name in capitals only
        self.assertEqual(sent[0], ["POST", "/api/mcp", {"name": "pixellab", "url": "https://api.pixellab.ai/mcp",
                                                         "key": {"name": "PIXELLAB_API_KEY", "header": "x-api-key", "value": "pk-123", "remember": True}}])
        self.assertEqual(sent[1][2], {"name": "files", "command": "npx", "args": ["-y", "srv", "/work"],
                                      "key": {"name": "FILES_TOKEN", "header": "bearer", "value": "", "remember": True}})
        self.assertEqual(sent[2][2], {"name": "ref", "server": {"type": "http", "url": "https://mcp.ref.example/mcp", "headers": {"Authorization": "Bearer ${REF_API_KEY}"}},
                                      "keys": {"REF_API_KEY": "ref-123"}, "remember": True})
        self.assertEqual(len(sent), 3)

    def test_the_mcp_library_sets_keys_duplicates_and_removes(self):
        got = self.run_js("""
          const lib = { pixellab: { type: "http", url: "https://api.pixellab.ai/mcp", headers: { "x-api-key": "${PIXELLAB_API_KEY}" } },
            files: { type: "stdio", command: "npx", args: ["srv"] } };
          const calls = [];
          globalThis.api = async (m, u, body) => { calls.push([m, u, body]);
            if (u === "/api/policy" && m === "GET") return { policy: { permission_mode: "dontAsk", claude_md: "", managed_settings: {} }, mcp_servers: lib, mcp_keys: { PIXELLAB_API_KEY: "missing" } };
            if (u.endsWith("/delete")) delete lib.files;
            if (u.endsWith("/duplicate")) lib[body.name] = lib.pixellab;
            return {}; };
          loadState = async () => {};
          STATE = { role: "owner", policy_version: 3, plugins: {}, agents_list: [{ id: "a1", name: "coder", mcp: ["files"] }] };
          const main = new Node("main");
          VIEWS.policy.mount(main);
          for (let i = 0; i < 4; i++) await Promise.resolve();
          const P = VIEWS.policy.p, rows = () => P.mcpList.querySelectorAll(".mcp-item");
          const first = rows().map((r) => r.textContent);
          // the JSON has an unsaved edit: a removal follows it there, so Save doesn't bring the server back
          P.mcp.value = JSON.stringify(Object.assign(JSON.parse(P.mcp.value), { extra: { type: "stdio", command: "x" } })); P.mcp.fire("input");
          confirmModal = async () => true; promptModal = async () => "pixel2";
          openPop = (anchor, node) => { globalThis.MENU = node; return true; };
          rows()[0].kids.at(-1).fire("click");
          const items = MENU.kids.map((k) => k.textContent);
          rows()[1].kids.at(-1).fire("click");
          MENU.kids.find((k) => k.textContent === "Remove").fire("click");
          for (let i = 0; i < 8; i++) await Promise.resolve();
          rows()[0].fire("contextmenu");
          MENU.kids.find((k) => k.textContent === "Duplicate…").fire("click");
          for (let i = 0; i < 8; i++) await Promise.resolve();
          console.log(JSON.stringify([first, items, calls.filter(([m]) => m === "POST"), Object.keys(JSON.parse(P.mcp.value)), rows().map((r) => r.kids[0].kids[0].textContent)]));
        """)
        first, items, posts, json_keys, names = got
        self.assertIn("http · api.pixellab.ai", first[0])
        self.assertIn("PIXELLAB_API_KEYNot setSet key", first[0])
        self.assertIn("On for coder", first[1])
        self.assertIn("stdio · npx srv", first[1])
        self.assertEqual(items, ["Set PIXELLAB_API_KEY…", "Duplicate…", "", "Remove"])
        self.assertEqual(posts, [["POST", "/api/mcp/files/delete", {}], ["POST", "/api/mcp/pixellab/duplicate", {"name": "pixel2"}]])
        self.assertEqual(json_keys, ["pixellab", "extra", "pixel2"])
        self.assertEqual(names, ["pixellab", "pixel2"])

    def test_agent_links_open_after_showing_where_they_go(self):
        got = self.run_js("""
          const opened = [], asked = [];
          confirmModal = async (title, body) => { asked.push([title, body.textContent]); return asked.length === 1; };
          window.open = (u, t, f) => { opened.push([u, f]); };
          const all = (n) => [n, ...n.kids.flatMap((k) => k.kids ? all(k) : [])];
          const links = (text) => all(new Node("p", "")).concat(...inline(text).filter((n) => n instanceof Node).map(all)).filter((n) => n.tag === "a");
          const one = (text) => { const a = links(text)[0]; return a ? [a.href ?? a.attrs.href, a.textContent] : inline(text).map((n) => n.textContent ?? n).join(""); };
          const out = [
            one("see https://docs.example.com/setup."),
            one("the [setup guide](https://docs.example.com/setup?x=1)"),
            one("[github.com/me/app](https://evil.example/steal)"),
            one("[PR](https://github.com/me/app/pull/7)"),
            one("[x](javascript:alert(1))"),
            one("[x](https://github.com@evil.example/a)"),
            one("ftp://files.example/a"),
          ];
          const a = links("https://docs.example.com/setup")[0];
          const e1 = a.fire("click"), e2 = a.fire("click");
          a.fire("auxclick", { button: 1 }); const e3 = a.fire("auxclick", { button: 2 });
          await Promise.resolve(); await Promise.resolve();
          out.push([e1.prevented, e2.prevented, !!e3.prevented, asked.length, asked[0], opened]);
          console.log(JSON.stringify(out));
        """, prelude=read(os.path.join(STATIC, "md.js")).replace('"use strict";', ""))
        self.assertEqual(got[0], ["https://docs.example.com/setup", "https://docs.example.com/setup"])
        self.assertEqual(got[1], ["https://docs.example.com/setup?x=1", "setup guide (docs.example.com)"])
        self.assertEqual(got[2], ["https://evil.example/steal", "github.com/me/app (evil.example)"])  # the real host follows the text
        self.assertEqual(got[3], ["https://github.com/me/app/pull/7", "PR"])  # GitHub opens straight away, as before
        self.assertEqual(got[4:7], ["x (javascript:alert(1))", "x (https://github.com@evil.example/a)", "ftp://files.example/a"])  # never links
        self.assertEqual(got[7], [True, True, False, 3, ["Open this link?", "docs.example.comhttps://docs.example.com/setupAn agent wrote it. Check where it goes."],
                                  [["https://docs.example.com/setup", "noopener,noreferrer"]]])  # opened only when you say Open

    def test_skill_switches_send_the_agents_skills(self):
        got = self.run_js("""
          const calls = [];
          globalThis.api = async (m, u, b) => { calls.push([u, b]); return {}; };
          toast = () => {}; loadState = async () => {};
          const a = { id: "a1", name: "x", skills: ["pixel-eye"] };
          const run = (role, skills) => { STATE = { role, skills }; return skillControls(a); };
          const box = run("owner", { "pixel-eye": { description: "Draws eyes." }, "notes": { description: "Writes notes." } });
          const sws = box.flatMap((n) => n.kids || []).filter((k) => k.className && k.className.includes("switch"));
          sws[1].fire("click");
          await Promise.resolve();
          console.log(JSON.stringify([box.map((n) => n.textContent), calls, run("device", {}).map((n) => n.textContent)]));
        """)
        self.assertEqual(got[0], ["pixel-eyeDraws eyes.", "notesWrites notes.", "Edit your skills"])
        self.assertEqual(got[1], [["/api/agents/a1/skills", {"skills": ["pixel-eye", "notes"]}]])
        self.assertEqual(got[2], ["No skills yet. Upload them on your PC."])  # a phone can't upload

    def test_keys_from_before_the_rename_move_over_once(self):
        prelude = r"""
          const mem = (init) => { const m = new Map(Object.entries(init)); return { getItem: (k) => (m.has(k) ? m.get(k) : null),
            setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), dump: () => Object.fromEntries(m) }; };
          globalThis.LS = mem({ "vmpanel-device-token": "dev-old", "vmpanel-sidebar": "{}", "cilly-notify": "new", "vmpanel-notify": "old" });
          globalThis.SS = mem({ "vmpanel-token": "tok", "vmpanel-view": "{}" });
        """
        got = self.run_js("""
          console.log(JSON.stringify([LS.dump(), SS.dump(), TOKEN]));
        """, prelude=prelude + "Object.assign(globalThis, { localStorage: LS, sessionStorage: SS });")
        self.assertEqual(got[0], {"cilly-device-token": "dev-old", "cilly-sidebar": "{}", "cilly-notify": "new"})  # a newer value wins
        self.assertEqual(got[1], {"cilly-token": "tok", "cilly-view": "{}"})
        self.assertEqual(got[2], "tok")

    def test_project_header(self):
        got = self.run_js("""
          const p = { id: "p1", name: "Lantern" }, members = [{ id: "a1", name: "coder", status: { key: "running" } }];
          const text = (role) => { STATE = { role }; return projectHeader(p, members).textContent; };
          console.log(JSON.stringify([text("owner"), text("device")]));
        """)
        self.assertIn("coder", got[0])
        self.assertIn("+ New agent here", got[0])
        self.assertIn("Delete project", got[0])
        self.assertNotIn("Delete project", got[1])  # a paired device can't change projects

    def test_the_lifecycle_list_and_the_attach_dialog_keep_their_own_styles(self):
        css = read(os.path.join(STATIC, "style.css"))
        for rule in (".radio-list {", ".radio-row {", ".radio {", ".keycap {", "textarea.editor.prose {"):
            self.assertEqual(css.count("\n" + rule), 1, rule)
        pages = read(os.path.join(STATIC, "pages.js"))
        self.assertIn('class: "mode-list"', pages)
        self.assertNotIn("radio-row", pages)

    def test_links_into_settings_open_their_tab(self):
        src = read(os.path.join(STATIC, "app.js")) + read(os.path.join(STATIC, "agent.js"))
        self.assertIn('openSettings("images") }, el("span", { class: "pulse" }), "Building an image")', src)
        self.assertIn('openSettings("github"); } }, "refresh in Settings → GitHub access")', src)
        tabs = re.findall(r'openSettings\("(\w+)"\)', src)
        keys = re.findall(r'\["(\w+)", "[^"]+"\]', read(os.path.join(STATIC, "pages.js")).split("const SETTINGS_TABS = ", 1)[1].split(";", 1)[0])
        self.assertTrue(tabs and set(tabs) <= set(keys), (tabs, keys))

    def test_the_mic_always_lets_go(self):
        """Drawing the message box again (another tab and back) made a new mic button that couldn't stop the
        recording: the browser kept the microphone, and a new recording couldn't start."""
        got = self.run_js(r"""
          STATE = { speech: { enabled: true } };
          const tracks = [];
          let asked = 0;
          navigator.mediaDevices = { getUserMedia: async () => { asked++; const t = { live: true, stop() { this.live = false; } }; tracks.push(t); return { getTracks: () => [t] }; } };
          globalThis.AudioContext = class { constructor() { this.sampleRate = 48000; this.destination = {}; }
            createMediaStreamSource() { return { connect() {} }; } createScriptProcessor() { return { connect() {} }; } close() {} };
          const box = () => { const ta = el("textarea"); Object.assign(ta, { value: "", selectionStart: 0, selectionEnd: 0, setSelectionRange() {}, dispatchEvent() {} }); return ta; };
          const gone = (n) => Object.defineProperty(n, "isConnected", { get: () => false });
          const live = () => tracks.filter((t) => t.live).length;
          const tick = () => new Promise((r) => setTimeout(r, 20));
          const out = [];
          const ta1 = box(), b1 = micButton(ta1, null, "agent:a1");
          b1.fire("click"); b1.fire("click"); await tick();  // a double click opens the microphone once
          out.push([asked, live(), VOICE.on]);
          gone(ta1);  // the box is drawn again: the new one takes over
          const ta2 = box(), b2 = micButton(ta2, null, "agent:a1");
          out.push([VOICE.btn === b2, ta2.readOnly, b2.title]);
          b2.fire("click"); await tick(); await tick();  // and its button stops it
          out.push([live(), VOICE.on, !!VOICE.area, ta2.readOnly]);
          const b3 = micButton(box(), null, "agent:a2");
          b3.fire("click"); await tick();
          b2.fire("click"); await tick(); await tick();  // any mic button stops the one recording
          out.push([asked, live(), VOICE.on]);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [[1, 1, True], [True, True, "Stop recording (Esc)"], [0, False, False, False], [2, 0, False]])

    def test_the_new_agent_prompt_keeps_the_mic_on_its_row(self):
        pages, css = read(os.path.join(STATIC, "pages.js")), read(os.path.join(STATIC, "style.css"))
        self.assertIn('P.profileSeg, P.repo, P.name, el("div", { class: "acts" }, P.attachBtn, P.files, P.mic)', pages)
        self.assertIn(".new-main .composer .bottom { column-gap: 6px; }", css)

    def test_the_phone_review_bar_says_approve_and_send(self):
        got = self.run_js("""
          STATE = { role: "owner", agents_list: [{ id: "r1", name: "researcher", profile: "research", tasks: [] }, { id: "c1", name: "coder", tasks: [] }] };
          const it = { kind: "research", agent: "c1", text: "How?", name: "q", research_agent: "r1" };
          const label = () => reviewResearch(it, []).map((b) => b.textContent);
          const desk = label();
          window.matchMedia = () => ({ matches: true });
          console.log(JSON.stringify([desk, label()]));
        """)
        self.assertEqual(got, [["Deny", "Approve"], ["Deny", "Approve and send"]])

    def test_phone_files_footer_and_attach_sheet(self):
        got = self.run_js("""
          const e = (name, kind, size) => ({ name, kind, size, mtime: 0 });
          const sums = [folderSummary([e("a", "dir", 0), e("b.png", "file", 6348), e("c.json", "file", 412)]), folderSummary([e("a", "dir", 0)]), folderSummary([])];
          STATE = { role: "device", repos: ["me/sprites"], agents_list: [] };
          const a = { id: "a1", name: "sprite-maker" }, anchor = new Node("button");
          attachDialog(a, anchor);
          const desk = [POP === null, document.body.kids.at(-1).className];
          window.matchMedia = () => ({ matches: true });
          attachDialog(a, anchor);
          console.log(JSON.stringify({ sums, desk, sheet: POP.node.className, anchored: POP.anchor === anchor }));
        """)
        self.assertEqual(got["sums"], ["1 folder, 2 files, 6.6 KiB", "1 folder", "Empty"])
        self.assertEqual(got["desk"], [True, "modal-back"])  # a dialog on the PC
        self.assertEqual((got["sheet"], got["anchored"]), ("stack tight attach-sheet", True))  # a bottom sheet on a phone

    def test_the_project_on_a_phone_is_a_list_of_screens(self):
        got = self.run_js("""
          const p = { id: "p1", name: "Lantern", instructions: "a\\n\\nb\\nc", notes: [{ id: "n1", text: "x" }], files: [], allow: ["a.io", "b.io"],
            repos: ["me/app", "me/lib"], pending: [{ id: "n2", text: "Redis", agent: "a1" }], research_agent: "r1", research_auto: false,
            defaults: { mcp: [], auto_stop: null, mode: null, image: null, model: null, effort: null } };
          STATE = { role: "owner", repos: ["me/app", "me/lib"], mcp_servers: {}, images: [], projects: [p],
            agents_list: [{ id: "r1", name: "researcher", profile: "research", tasks: [] }, { id: "a1", name: "coder", project: "p1", tasks: [], status: { key: "idle" } }] };
          window.matchMedia = () => ({ matches: true });
          const members = STATE.agents_list.filter((a) => a.project === "p1");
          const list = projectPhone(p, members, {});
          const rows = list.at(-1).kids.map((r) => r.textContent);
          UI.sec = "instructions";
          const sub = projectPhone(p, members, {});
          const desk = projectPage(p, members, {})[1].kids.map((col) => col.kids.length);
          console.log(JSON.stringify({ rows, pending: list[3].textContent.startsWith("Suggested notes · 1"), sub: [sub[1].textContent, sub.at(-1).className], desk }));
        """)
        self.assertEqual(got["rows"], ["Instructions3 lines›", "Notes1›", "Files0›", "Defaults for new agents2 repos›", "Web researchresearcher›", "Allowed sites2›"])
        self.assertTrue(got["pending"])
        self.assertEqual(got["sub"], ["Instructions", "m-foot"])  # Save pinned at the bottom
        self.assertEqual(got["desk"], [5, 2])  # the PC keeps both columns, the defaults in the wide one

    def test_save_settings_keeps_the_themes_appearance_saved(self):
        # Save settings sends the whole config: what the Appearance tab saved goes with it, without the panel's own themes
        got = self.run_js("""
          STATE = { appearance: { theme: "0123abcd", themes: { classic: { name: "Classic", colors: {}, font: "geist", mono: "geist-mono", builtin: true },
            "0123abcd": { name: "Night shift", colors: { "--bg": "#101418" }, font: "4567cdef", mono: "geist-mono", size: 110, mono_size: 100, builtin: false } } } };
          console.log(JSON.stringify(savedAppearance()));
        """)
        self.assertEqual(got, {"theme": "0123abcd", "themes": {"0123abcd": {"name": "Night shift", "colors": {"--bg": "#101418"},
                                                                            "font": "4567cdef", "mono": "geist-mono", "size": 110,
                                                                            "mono_size": 100}}})

    def test_phone_settings_list_summaries(self):
        got = self.run_js("""
          STATE = { role: "owner", base_image: "b1", policy_version: 14, agents_list: [], images: [{ name: "b1", preset: "full", toolchains: ["java"] }], presets: { full: { label: "Everything" } },
            appearance: { theme: "0123abcd", themes: { classic: { name: "Classic" }, "0123abcd": { name: "Night shift" } } } };
          const c = { tokens: { bot: { repos: ["me/a", "me/b"] }, other: { repos: ["me/a"] } }, egress: { allow: Array(12).fill("x") },
            vm: { max_cpus: 8, max_memory_mb: 16384 }, speech: { enabled: true, whisper_url: "http://gpu-box.local:8080" }, agents: {} };
          const s = settingsSummaries(c, { "agent:claude:X": "missing", "token:bot": "pasted" });
          const off = settingsSummaries({ ...c, speech: { enabled: false, whisper_url: "" }, vm: { max_cpus: 4, max_memory_mb: 6144 } }, { "token:bot": "command (not loaded yet)" });
          console.log(JSON.stringify([s, [off.secrets, off.missing, off.vm, off.speech]]));
        """)
        self.assertEqual(got[0], {"missing": 1, "secrets": "1 missing", "github": "2 repos", "network": "12 hosts", "models": "None", "images": "Everything",
                                  "vm": "8 vCPU, 16 GiB", "notifications": "Off", "speech": "gpu-box", "policy": "v14", "host": "Ready",
                                  "appearance": "Night shift"})
        self.assertEqual(got[1], ["1 locked", 0, "4 vCPU, 6 GiB", "Off"])


    def test_devices_on_a_phone_load_when_their_screen_opens(self):
        got = self.run_js("""
          globalThis.setTimeout = () => 0;  // the list is built off the page, so its first refresh finds nothing to fill
          window.matchMedia = () => ({ matches: true });
          const calls = [];
          api = async (m, path) => { calls.push(m + " " + path); return { devices: [{ id: "d1", name: "Pixel", created: 0, last_seen: 0 }], pending: [] }; };
          STATE = { role: "owner", agents_list: [] };
          const box = pairedSection();
          VIEWS.settings.p = { phone: true, body: Object.assign(new Node("div"), { parentElement: {} }), panels: { devices: setPanel("Paired devices", "", box) } };
          VIEWS.settings.open("devices");
          await Promise.resolve(); await Promise.resolve();
          console.log(JSON.stringify([calls, box.textContent.includes("Pixel")]));
        """)
        self.assertEqual(got, [["GET /api/devices"], True])

    def test_a_suggested_note_shows_its_whole_text(self):
        got = self.run_js("""
          const timers = [];
          globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
          window.matchMedia = () => ({ matches: true });
          const p = { id: "p1", name: "Lantern", instructions: "", notes: [], files: [], allow: [], repos: [], research_agent: null, research_auto: false,
            pending: [{ id: "n2", text: "A long note. ".repeat(20), agent: "a1" }], defaults: { mcp: [], auto_stop: null, mode: null, image: null, model: null, effort: null } };
          STATE = { role: "owner", repos: [], mcp_servers: {}, images: [], projects: [p], agents_list: [{ id: "a1", name: "coder", project: "p1", tasks: [], status: { key: "idle" } }] };
          const screen = projectPhone(p, STATE.agents_list, {});
          const t = screen.map((n) => n && n.kids && find(n, "note-edit")).find(Boolean);
          t.scrollHeight = 186; timers.forEach((f) => f());
          const first = t.style.height;
          t.scrollHeight = 210; t.fire("input");
          console.log(JSON.stringify([first, t.style.height]));
        """)
        self.assertEqual(got, ["186px", "210px"])

    def test_text_stays_selectable_off_phones(self):
        css, at, phone_only = read(os.path.join(STATIC, "style.css")), [], True
        depth, media = 0, []
        for m in re.finditer(r"@media[^{]*\{|\{|\}|user-select: none", css):
            tok = m.group(0)
            if tok.startswith("@media"):
                media.append((depth, tok)); depth += 1
            elif tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
                if media and media[-1][0] == depth:
                    media.pop()
            else:
                at.append(m.start())
                phone_only &= any("max-width: 640px" in q for _, q in media)
        self.assertTrue(at)
        self.assertTrue(phone_only)

    def test_the_bottom_bar_hides_on_every_project_and_settings_screen(self):
        got = self.run_js("""
          const app = $("app"), on = {};
          app.classList.toggle = (c, v) => { on[c] = v; };
          const bar = (view, sec) => { Object.assign(UI, { view, sec }); renderMobileNav(); return on["m-nav"]; };
          console.log(JSON.stringify([bar("agents", null), bar("project", null), bar("project", "notes"), bar("project", "instructions"),
            bar("settings", null), bar("settings", "devices"), bar("settings", "vm"), bar("agent", null)]));
        """)
        self.assertEqual(got, [True, True, False, False, True, False, False, False])

    def test_back_on_a_phone_goes_up_a_level(self):
        got = self.run_js("""
          window.matchMedia = () => ({ matches: true });
          const log = [];
          history.pushState = () => log.push("push"); history.back = () => log.push("leave");
          go = (view, o) => { log.push(view + (o?.sec ? "/" + o.sec : "")); Object.assign(UI, { view, sec: null }, o || {}); syncBack(); };
          VIEWS.settings = { open: (k) => { log.push("settings/" + k); UI.sec = k; syncBack(); } };
          const at = (view, sec) => { Object.assign(UI, { view, sec, projectId: "p1" }); syncBack(); };
          at("settings", "vm"); at("settings", "vm");  // one entry, however often it's asked
          onBack(); onBack(); onBack();
          const settings = log.splice(0);
          at("project", "notes"); onBack(); onBack();
          const project = log.splice(0);
          at("review"); onBack(); at("agent"); onBack();
          const others = log.splice(0);
          at("agents"); POP = { node: new Node("div") }; closePop = () => { log.push("close"); POP = null; }; syncBack(); onBack(); onBack();
          const sheet = log.splice(0);
          at("agent"); go("agents"); onBack();  // up through the page's own ←, then Back
          const leave = log.splice(0);
          window.matchMedia = () => ({ matches: false }); at("settings", "vm");
          console.log(JSON.stringify({ settings, project, others, sheet, leave, desk: log }));
        """)
        self.assertEqual(got["settings"], ["push", "settings/null", "push", "more"])
        self.assertEqual(got["project"], ["push", "project", "push", "agents"])
        self.assertEqual(got["others"], ["push", "inbox", "push", "agents"])
        self.assertEqual(got["sheet"], ["push", "close"])
        self.assertEqual(got["leave"], ["push", "agents", "leave"])
        self.assertEqual(got["desk"], [])  # a PC keeps its history as it is

    def test_links_to_a_settings_tab_open_its_phone_screen_too(self):
        got = self.run_js("""
          const calls = [];
          go = (view, o) => calls.push([view, o]);
          STATE = { role: "owner", agents_list: [], projects: [], build: { state: "running" } };
          buildLine().fire("click");
          switcherItems().find((x) => x.label === "Network").go();
          console.log(JSON.stringify(calls));
        """)
        self.assertEqual(got, [["settings", {"settingsTab": "images", "sec": "images"}], ["settings", {"settingsTab": "network", "sec": "network"}]])

    def test_phone_rows_keep_their_menu_button(self):
        got = self.run_js("""
          window.matchMedia = () => ({ matches: true });
          STATE = { role: "owner", agents_list: [{ id: "a1", name: "coder", state: "stopped", tasks: [], status: { key: "stopped" } }], projects: [],
            tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, base_image: "b", build: {} };
          VIEWS.agents.mount(new Node("main")); VIEWS.agents.update();
          const more = find(VIEWS.agents.p.list, "sb-more");
          more.fire("click");
          console.log(JSON.stringify([more.textContent, POP?.node.className]));
        """)
        self.assertEqual(got, ["⋮", "menu"])  # the agent menu, as a sheet on a phone
        css = read(os.path.join(STATIC, "style.css"))
        self.assertNotRegex(css, r"\.m-list \.sb-more \{[^}]*display: none")

    def test_only_the_running_icon_turns_and_a_redraw_keeps_its_place(self):
        got = self.run_js("""
          let now = 763200 * 3 + 700;
          Date.now = () => now;
          const a = stateIcon("running"); now += 5000;
          const b = stateIcon("running");
          console.log(JSON.stringify([a.style.animationDelay, b.style.animationDelay, stateIcon("waiting").style.animationDelay,
            stateIcon("idle").style.animationDelay ?? null]));
        """)
        # at the clock's place in a loop that is a multiple of every animated icon's loop
        self.assertEqual(got, ["-700ms", "-5700ms", "-5700ms", None])
        for loop_ms in (1600, 900, 2120):  # Running in Classic and Terminal, and Terminal's Waiting
            self.assertEqual(763200 % loop_ms, 0)
        self.assertIn("animation: cilly-spin8 .9s", read(os.path.join(STATIC, "theme", "terminal.css")))
        self.assertIn("animation: cilly-wait9 2.12s", read(os.path.join(STATIC, "theme", "terminal.css")))
        css = read(os.path.join(STATIC, "style.css"))
        self.assertEqual(re.findall(r"animation(?:-name)?:\s*[^;}]+", css), ["animation: spin 1.6s linear infinite", "animation: none"])
        self.assertIn("@media (prefers-reduced-motion: reduce) { .st-icon.st-running { animation: none; } }", css)
        self.assertNotIn("@keyframes blink", css)
        js = "".join(read(os.path.join(STATIC, f)) for f in FILES)
        self.assertNotIn('class: "spinner"', js)
        self.assertNotIn('class: "working"', js)

    def test_findings_can_be_archived_without_forwarding(self):
        got = self.run_js("""
          Object.defineProperty(Node.prototype, "childElementCount", { get() { return this.kids.length; } });
          const calls = [];
          api = async (m, path) => { calls.push(m + " " + path); return {}; };
          loadState = async () => {}; pollAgent = () => {}; pollInbox = () => {};
          const r = { id: "r1", name: "researcher", profile: "research" }, c = { id: "c1", name: "coder", profile: "coding", research_agent: "r1",
            requests: [{ id: "q1", name: "recipes", text: "How?", state: "delivered", delivery: "d1", research_agent: "r1", created: "" }] };
          STATE = { role: "owner", agents_list: [r, c] };
          const d = (id, extra) => Object.assign({ id, request_name: id, instructions: "", files: ["a.md"], forwarded_to: [], created: "", for_agent: "c1" }, extra);
          EXTRA.r1 = { deliveries: [d("d1"), d("d2", { forwarded_to: ["c1"] })] };
          const buttons = (n) => n.tag === "button" ? [n] : (n.kids || []).flatMap(buttons);
          const labels = (n) => buttons(n).map((b) => b.textContent);
          AV = { pane: new Node("div") };
          renderFindings(r);
          const fresh = labels(AV.pane);
          AV.findView = "archive"; SIGS = {}; renderFindings(r);
          const old = labels(AV.pane);
          AV.findView = "new"; SIGS = {}; renderFindings(r);
          buttons(AV.pane).find((b) => b.textContent === "Archive").fire("click");
          AV.pane = new Node("div"); SIGS = {}; renderResearch(c);
          const coder = labels(AV.pane).filter((l) => ["Review", "Dismiss", "Forward"].includes(l));
          buttons(AV.pane).find((b) => b.textContent === "Dismiss").fire("click");
          await new Promise((res) => setTimeout(res));
          console.log(JSON.stringify([fresh.filter((l) => ["Archive", "Forward"].includes(l)), old.includes("Archive"), coder, calls]));
        """)
        self.assertEqual(got, [["Archive", "Forward"], False, ["Review", "Dismiss", "Forward"],
                               ["POST /api/agents/r1/deliveries/d1/dismiss", "POST /api/agents/r1/deliveries/d1/dismiss"]])

    def test_logs_take_the_whole_width_and_wrap_at_spaces(self):
        css = read(os.path.join(STATIC, "style.css"))
        wrap = re.search(r"\.logs-wrap \{([^}]*)\}", css).group(1)
        self.assertNotIn("max-width", wrap)
        text = re.search(r"\.log-text \{([^}]*)\}", css).group(1)
        self.assertIn("overflow-wrap: anywhere", text)
        self.assertNotIn("break-all", text)

    def test_policy_modes_are_a_list_under_a_full_width_header(self):
        got = self.run_js("""
          STATE = { role: "owner", policy_version: 3 };
          api = async () => ({ policy: { permission_mode: "dontAsk", claude_md: "", managed_settings: {} }, mcp_servers: {} });
          const main = new Node("main");
          VIEWS.policy.mount(main);
          await Promise.resolve(); await Promise.resolve();
          const P = VIEWS.policy.p, head = main.kids[0], inner = main.kids[1].kids[0];
          console.log(JSON.stringify([P.modes.className, P.modes.kids.map((k) => [k.kids[1].kids[0].textContent, k.className.includes(" on")]),
            head.className, find(head, "page-title").textContent, find(head, "tip").textContent]));
        """)
        self.assertEqual(got, ["mode-list", [["bypassPermissions", False], ["dontAsk", True], ["acceptEdits", False]], "set-head bare", "Claude policy",
                               "Enforced in every agent VM. Installed root-owned at /etc/claude-code/, so agents can't change it. Saving applies live."])
        # the title in a full-width header, like an agent's, and what the policy is behind its (i)
        css = read(os.path.join(STATIC, "style.css"))
        self.assertIn(".mode-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));", css)
        self.assertIn("@media " + PHONE_MQ + " { .mode-cards { grid-template-columns: minmax(0, 1fr); } }", css)

    def test_text_in_a_nowrap_row_still_wraps(self):
        css = read(os.path.join(STATIC, "style.css"))
        self.assertIn(".nowrap:not(.row) { white-space: nowrap; }", css)
        self.assertNotRegex(css, r"(^|\n)\.nowrap \{")

    def test_phone_layout_fixes(self):
        css = read(os.path.join(STATIC, "style.css"))
        phone = "".join(re.findall(r"@media " + re.escape(PHONE_MQ) + r" \{(.*?)\n\}", css, re.S))
        for rule in ("  .save-bar { padding: 12px 16px; }",
                     ".blocked-row > .grow.stack { flex-basis: 100%; }", ".fetch-row > .input, .fetch-row > .select { flex: 1 1 100%; }",
                     ".forward-card.row.nowrap { flex-wrap: wrap; }"):
            self.assertIn(rule, phone)
        self.assertIn(".seg.wrap button { flex: 1 1 auto; }", css)
        self.assertRegex(css, r"@media \(min-width: 641px\) and \(not \(\(max-height: 500px\) and \(pointer: coarse\)\)\) \{[^@]*\.m-page \{ width: 100%; max-width: 760px;")
        got = self.run_js("""
          STATE = { role: "owner", repos: ["me/a"], agents_list: [{ id: "c1", name: "coder", profile: "coding" }] };
          api = async () => ({ prs: [], errors: [], files: [] });
          VIEWS.prs.mount(new Node("main"));
          const fetchRow = find(VIEWS.prs.p.repo.parent, "fetch-row");
          const parts = [];
          reviewFindings({ agent: "r1", id: "d1", for_agent: "c1", question: "" }, parts);
          console.log(JSON.stringify([!!fetchRow, parts.some((n) => n.className && n.className.includes("forward-card"))]));
        """)
        self.assertEqual(got, [True, True])

    def test_settings_back_link_lines_up_with_a_projects(self):
        got = self.run_js("""
          window.matchMedia = () => ({ matches: true });
          STATE = { role: "owner", agents_list: [] };
          UI.sec = "vm";
          VIEWS.settings.p = { phone: true, body: Object.assign(new Node("div"), { parentElement: {} }), panels: { vm: setPanel("VM limits", "") } };
          VIEWS.settings.showPhone();
          console.log(JSON.stringify(VIEWS.settings.p.body.kids[0].className));
        """)
        self.assertEqual(got, "row nowrap m-proj-top")  # like the project's, so both sit at the same height

    def test_counts_say_one_file(self):
        got = self.run_js("""
          const it = (n) => ({ kind: "findings", files: Array(n).fill("a.md"), name: "q", agent: "r1" });
          console.log(JSON.stringify([itemSummary(it(1)).split(" answering")[0], itemSummary(it(2)).split(" answering")[0]]));
        """)
        self.assertEqual(got, ["1 file", "2 files"])
        for f in FILES:
            self.assertNotRegex(read(os.path.join(STATIC, f)), r" [a-z]+\(s\)(?! \{| =>)[ .`]", f)  # "file(s)", not a function's (s)

    def test_a_log_without_times_has_no_time_column(self):
        got = self.run_js("""
          document.createDocumentFragment = () => new Node("frag");
          const out = [];
          for (const text of ["===== start\\nboots on this disk: 1\\n", "===== start\\n12:00:01Z task 1\\n"]) {
            const log = new Node("div"), cls = new Set(["log-pane", "no-time"]);
            log.classList = { add: (c) => cls.add(c), remove: (c) => cls.delete(c), toggle() {}, contains: (c) => cls.has(c) };
            log.childNodes = [];
            AV = { id: "a1", logWhich: "agent", log, logOffset: 0, logCarry: "", follow: false };
            api = async () => ({ text, offset: text.length });
            await pollLog();
            out.push(cls.has("no-time"));
          }
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [True, False])

    def test_a_text_file_counts_its_lines(self):
        got = self.run_js("""
          const out = [];
          for (const lines of [1, 3]) {
            fetchFile = async () => ({ type: "text", kind: "text", preview: "none", size: 12, lines, warnings: [], name: "a.txt" });
            const v = fileView({ id: "a1" }, "outbox", "a.txt");
            await new Promise((r) => setTimeout(r));
            out.push(v.sub.textContent);
          }
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, ["Text, 12 B, 1 line", "Text, 12 B, 3 lines"])

    def test_state_icon_marks_follow_the_theme(self):
        got = self.run_js("""
          const marks = (k) => stateIcon(k).kids.slice(1).map((c) => [c.attrs.class, c.attrs.fill || "", c.attrs.stroke || ""]);
          console.log(JSON.stringify([marks("waiting"), marks("failed")]));
        """)
        self.assertEqual(got, [[["st-mark", "", ""], ["st-mark", "", ""]], [["st-mark line", "", ""]]])
        css = read(os.path.join(STATIC, "style.css"))
        self.assertIn(".st-mark { fill: var(--bg); }", css)
        self.assertIn(".st-mark.line { fill: none; stroke: var(--bg); }", css)

    def test_backgrounds_come_from_the_theme(self):
        css = read(os.path.join(STATIC, "style.css"))
        root, rest = css.split("color-scheme: dark;\n}", 1)
        self.assertIn("--surface-soft: #222;", root)
        self.assertIn("--warn-soft: #211e18;", root)
        self.assertNotRegex(rest, r"#222\b|#211e18")
        self.assertRegex(rest, r"\.running-strip \{[^}]*background: var\(--surface-soft\)")
        self.assertRegex(rest, r"\.ask-picker \{[^}]*background: var\(--warn-soft\)")

    def test_the_whisper_url_takes_a_whole_row_on_a_phone(self):
        got = self.run_js("""
          globalThis.setTimeout = () => 0;
          window.matchMedia = () => ({ matches: true });
          globalThis.Notification = { permission: "default" };
          STATE = { role: "owner", agents_list: [], images: [], presets: {}, repos: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, build: {} };
          const config = { tokens: {}, egress: { allow: [], never_reach: [], ports: [443] }, research: { yolo_rounds: 3 }, agents: {},
            vm: { cpus: 2, memory_mb: 4096, max_cpus: 8, max_memory_mb: 16384, idle_stop_minutes: 30 },
            speech: { enabled: true, whisper_url: "http://gpu-box.local:8080", max_seconds: 120 } };
          api = async () => ({ config, secrets: {}, devices: [], pending: [] });
          VIEWS.settings.mount(new Node("main"));
          await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
          const P = VIEWS.settings.p, fieldOf = (input) => input.parent.className;
          console.log(JSON.stringify([fieldOf(P.spUrl), fieldOf(P.spMax)]));
        """)
        self.assertEqual(got, ["field wide", "field"])
        css = read(os.path.join(STATIC, "style.css"))
        phone = css[css.index("/* ---- phones: one screen at a time"):]
        self.assertIn(".set-grid > .field.wide { grid-column: 1 / -1; }", phone)

    def test_a_paired_device_gets_the_same_way_back_from_a_project(self):
        got = self.run_js("""
          window.matchMedia = () => ({ matches: true });
          const p = { id: "p1", name: "Lantern", instructions: "", notes: [], files: [], allow: [], repos: [], defaults: { mcp: [] } }, members = [];
          const back = (role) => { STATE = { role }; const nodes = projectPageReadOnly(p, members);
            return [find(nodes[0], "m-back-link")?.textContent, find(nodes[0], "icon-btn")?.textContent ?? null]; };  // not a bare ←
          console.log(JSON.stringify(back("device")));
        """)
        self.assertEqual(got, ["← Agents", None])

    def test_settings_follow_the_phone_width_and_keep_your_changes(self):
        got = self.run_js("""
          let phone = false;
          window.matchMedia = () => ({ matches: phone });
          STATE = { role: "owner", agents_list: [], images: [], presets: {}, policy_version: 1, tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, build: {} };
          api = async () => ({ devices: [] });
          const main = new Node("main"), panel = (k) => setPanel(k, "", new Node("div"));
          VIEWS.settings.p = { main, panels: { speech: panel("speech"), devices: panel("devices"), notifications: panel("notifications") },
            c: { tokens: {}, egress: { allow: [] }, vm: { max_cpus: 8, max_memory_mb: 16384 }, speech: { enabled: false } }, secrets: {} };
          UI.settingsTab = "speech";
          VIEWS.settings.layout();
          const P = VIEWS.settings.p, speech = P.panels.speech, out = [[P.phone, P.body.kids.includes(speech)]];
          phone = true;
          const field = new Node("input"); field.tagName = "INPUT"; field.type = "text"; speech.append(field);
          document.activeElement = field;
          VIEWS.settings.update();  // you're typing in it: nothing moves
          out.push([P.phone, P.body.kids.includes(speech)]);
          document.activeElement = null;
          VIEWS.settings.update();
          out.push([P.phone, UI.sec, P.body.kids.includes(speech), P.panels.speech === speech]);
          UI.sec = "notifications";
          phone = false;
          VIEWS.settings.update();
          out.push([P.phone, UI.settingsTab, P.panels.devices.hidden, P.panels.notifications.hidden, P.panels.speech.hidden]);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [[False, True], [False, True], [True, "speech", True, True], [False, "devices", False, False, True]])

    def test_a_project_follows_the_phone_width(self):
        got = self.run_js("""
          let phone = false;
          window.matchMedia = () => ({ matches: phone });
          const p = { id: "p1", name: "Lantern", instructions: "", notes: [], files: [], allow: [], repos: [], pending: [], research_agent: null, research_auto: false,
            defaults: { mcp: [], auto_stop: null, mode: null, image: null, model: null, effort: null } };
          STATE = { role: "owner", repos: [], mcp_servers: {}, images: [], projects: [p], agents_list: [] };
          UI.projectId = "p1";
          VIEWS.project.mount(new Node("main"));
          VIEWS.project.update();
          const P = VIEWS.project.p, out = [P.inner.redraws];
          VIEWS.project.update();
          out.push(P.inner.redraws);  // nothing changed
          phone = true;
          VIEWS.project.update();
          out.push(P.inner.redraws, P.inner.kids.at(-1).className);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, [1, 1, 2, "m-set-list"])  # the phone's list of screens

    def test_crossing_the_phone_width_waits_while_you_type(self):
        got = self.run_js("""
          let phone = false;
          window.matchMedia = () => ({ matches: phone });
          const calls = [];
          go = (view) => calls.push("go " + view);
          VIEWS.settings.update = () => calls.push("settings in place");
          renderSidebar = () => {}; renderMobileNav = () => {}; syncBack = () => {};
          const main = $("main"), field = new Node("textarea"); field.tagName = "TEXTAREA"; main.append(field);
          UI.view = "settings";
          phoneResize();
          phone = true;
          document.activeElement = field;
          phoneResize(); phoneResize();  // while you type: only one wait for you to leave the field
          out = [calls.length, (document.ls.focusout || []).length];
          document.activeElement = null;
          globalThis.setTimeout = (f) => f();
          document.ls.focusout.forEach((f) => f());
          UI.view = "agent"; phone = false; phoneResize();
          console.log(JSON.stringify([...out, calls]));
        """)
        self.assertEqual(got, [0, 1, ["settings in place", "go agent"]])

    def test_pairing_is_one_sentence_and_the_lists_everywhere(self):
        js = "".join(read(os.path.join(STATIC, f)) for f in FILES)
        self.assertEqual(js.count("${PAIRED_SHORT}") + js.count("PAIRED_SHORT), pairedLists()") + js.count("infoTip(PAIRED_SHORT)]"), 4)  # sign-in, Settings (behind its (i)), review, More
        self.assertEqual(js.count(", pairedLists()"), 4)  # each with the short sentence
        got = self.run_js("""
          const d = pairedLists(), text = (n) => n.tag === "#text" ? n.text : n.kids.map(text).join("|");
          console.log(JSON.stringify([d.tag, d.kids[0].textContent, d.kids[1].kids.map((c) => c.kids[0].textContent),
            d.kids[1].kids.map((c) => c.kids[1].kids.length)]));
        """)
        self.assertEqual(got, ["details", "What a paired device can do", ["Can", "Can't"], [14, 7]])  # can, and can't (adding MCP servers, and plugins from other sites, among them)

    def test_on_the_pc_the_sign_in_page_leads_with_the_startup_link(self):
        got = self.run_js("""
          const texts = (n) => n.tag === "#text" ? [n.text] : (n.kids || []).flatMap((k) => k instanceof Node ? texts(k) : []);
          const page = (host) => {
            location.hostname = host;
            const app = new Node("div"); IDS.app = app;
            mountNoToken();
            const box = app.kids[0].kids[0], main = box.kids[2], other = box.kids[3];
            return [texts(main)[0], other.tag, texts(other.kids[0])[0], texts(box).includes(host === "cilly.home" ? "cilly open --print" : "cilly open"), !texts(box).some((t) => t.includes("journalctl")),
              texts(other).includes("A paired device can't open Settings, Claude policy or secrets.")];
          };
          console.log(JSON.stringify([page("127.0.0.1"), page("localhost"), page("cilly.home")]));
        """)
        self.assertEqual(got, [["Open the startup link", "details", "Pair this device", True, True, True],
                               ["Open the startup link", "details", "Pair this device", True, True, True],
                               ["Pair this device", "details", "Or use the startup link", True, True, False]])
        from cilly.__main__ import parser
        self.assertTrue(parser().parse_args(["open", "--print"]).print_link)  # the command the page names
    def test_a_paired_device_sees_a_projects_settings_without_controls(self):
        got = self.run_js("""
          const p = { id: "p1", name: "Lantern", instructions: "Use Gradle", notes: [{ id: "n", text: "Run tests" }], files: [{ name: "spec.pdf", size: 2048 }],
            allow: ["crates.io"], repos: ["me/app"], research_agent: "r1", research_auto: true, route: null,
            defaults: { mcp: ["linear"], plugins: [], mode: "persistent", auto_stop: false, keep_outbox: "/home/me/outboxes", model: null, effort: "high", image: null } };
          STATE = { role: "device", routes: { standard: "tor" }, agents_list: [{ id: "r1", name: "researcher", profile: "research" }], projects: [p] };
          const nodes = projectPageReadOnly(p, []);
          const tags = new Set(), walk = (n) => { tags.add(n.tag); n.kids?.forEach(walk); };
          nodes.slice(2).forEach(walk);
          const text = nodes.slice(2).map((n) => n.textContent).join("|");
          console.log(JSON.stringify([[...tags].filter((t) => ["input", "textarea", "select", "button"].includes(t)), text.includes("spec.pdf"), text.includes("me/app"),
            text.includes("Persistent"), text.includes("High"), text.includes("researcher"), text.includes("crates.io"), text.includes("Tor"), text.includes("/home/me")]));
        """)
        self.assertEqual(got, [[], True, True, True, True, True, True, True, False])  # all of it to read, no control, no host path

    def test_an_owner_on_a_phone_sees_a_rename_link_on_a_project(self):
        got = self.run_js("""
          const p = { id: "p1", name: "Lantern", instructions: "", notes: [], files: [], allow: [], repos: [], pending: [], defaults: { mcp: [] } };
          STATE = { role: "owner", repos: [], mcp_servers: {}, images: [], projects: [p], agents_list: [] };
          window.matchMedia = () => ({ matches: true });
          const top = projectPhone(p, [], {})[0];
          console.log(JSON.stringify(top.kids.at(-1).kids.map((k) => k.textContent)));
        """)
        self.assertEqual(got, ["Rename", "···"])  # the menu stays for Delete

    def test_a_policy_that_did_not_load_cannot_be_saved(self):
        got = self.run_js("""
          STATE = { role: "owner", policy_version: 3, plugins: {}, agents_list: [] };
          let fail = true;
          api = async () => { if (fail) throw new Error("offline"); return { policy: { permission_mode: "dontAsk", claude_md: "x", managed_settings: {} }, mcp_servers: {} }; };
          const main = new Node("main");
          VIEWS.policy.mount(main);
          await Promise.resolve(); await Promise.resolve();
          const P = VIEWS.policy.p, state = () => [P.loaded, P.failed.hidden, P.md.disabled, P.bar.kids[0].textContent, !!P.bar.kids[1].disabled];
          const bad = state(), retry = find(P.failed, "btn");
          fail = false;
          retry.fire("click");
          await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
          P.dirty = true; P.renderBar();
          console.log(JSON.stringify([bad, P.failed.textContent.includes("offline"), retry.textContent, state()]));
        """)
        self.assertEqual(got[0], [False, False, True, "Not loaded", True])
        self.assertTrue(got[1])
        self.assertEqual(got[2], "Retry")
        self.assertEqual(got[3], [True, True, False, "Unsaved changes", False])

    def test_settings_and_pull_requests_offer_a_retry_when_they_do_not_load(self):
        got = self.run_js("""
          STATE = { role: "owner", repos: [], agents_list: [], tools: { qemu: 1, "qemu-img": 1, kvm: 1, bwrap: 1 }, build: {} };
          let calls = 0;
          api = async () => { calls++; throw new Error("no answer"); };
          const main = new Node("main");
          VIEWS.settings.mount(main);
          await Promise.resolve(); await Promise.resolve();
          const P = VIEWS.settings.p, retry = find(P.body, "btn");
          retry.fire("click");
          await Promise.resolve(); await Promise.resolve();
          const settings = [retry.textContent, calls, P.body.textContent.includes("no answer"), P.body.textContent.includes("loading")];
          const page = new Node("main");
          VIEWS.prs.mount(page);
          await Promise.resolve(); await Promise.resolve();
          console.log(JSON.stringify([settings, find(VIEWS.prs.p.list, "btn")?.textContent, VIEWS.prs.p.list.textContent.includes("no answer")]));
        """)
        self.assertEqual(got, [["Retry", 2, True, False], "Retry", True])

    def test_a_paired_phone_is_not_sent_to_settings_for_the_pull_request_token(self):
        got = self.run_js("""
          const text = "No GitHub token for the panel is set, so private repositories can't be listed. Paste a token that can read them under Settings \u2192 Secrets \u2192 panel_token.";
          STATE = { role: "device" }; const paired = prError(text);
          STATE = { role: "owner" };
          console.log(JSON.stringify([paired, prError(text) === text]));
        """)
        self.assertEqual(got, ["No GitHub token for the panel is set, so private repositories can't be listed. Add one on your PC.", True])

    def test_settings_mark_what_you_edited_and_have_not_saved(self):
        got = self.run_js("""
          const field = (value) => ({ value, classList: { contains: () => false } });
          const P = VIEWS.settings.p = { phone: false, dirtyTabs: new Set(), saveNote: { hidden: true }, github: { chosen: { bot: new Set(["me/a"]) } },
            network: { allow: field("a.io"), never: [], routes: { routes: {}, route: "", own: false, doh: "", keep: true, pushOwn: true } },
            cpus: field("4"), mem: field("4096"), maxc: field("8"), maxm: field("16384"), idle: field("30"), yolo: field("3"), yoloAll: field(""),
            spOn: field(""), spUrl: field(""), spMax: field("120"), json: field("{}"), branches: { mode: "kind", prefix: field("cilly/") } };
          P.github.idents = { bot: { name: "agent-bot", email: "00000000+agent-bot@users.noreply.github.com" } };
          P.base = VIEWS.settings.signatures();
          VIEWS.settings.markDirty();
          const clean = [[...P.dirtyTabs], P.saveNote.hidden];
          P.cpus.value = "8"; P.github.chosen.bot.add("me/b");
          VIEWS.settings.markDirty();
          const edited = [[...P.dirtyTabs].sort(), P.saveNote.hidden];
          P.cpus.value = "4"; P.github.chosen.bot.delete("me/b");
          VIEWS.settings.markDirty();
          const back = [[...P.dirtyTabs], P.saveNote.hidden];
          P.branches.mode = "id";  // branch names are on GitHub access too
          VIEWS.settings.markDirty();
          const branches = [...P.dirtyTabs];
          P.branches.mode = "kind"; P.github.idents.bot = { name: "cillybox", email: "123+cillybox@users.noreply.github.com" };  // and who an account's agents commit as
          VIEWS.settings.markDirty();
          console.log(JSON.stringify([clean, edited, back, branches, [...P.dirtyTabs]]));
        """)
        self.assertEqual(got, [[[], True], [["github", "vm"], False], [[], True], ["github"], ["github"]])

    def test_github_access_shows_one_account_at_a_time(self):
        got = self.run_js("""
          const work = { name: "acme-agent", email: "98765432+acme-agent@users.noreply.github.com" };
          STATE = { repos: ["me/a", "me/b"], accounts: [
            { token: "bot", name: "agent-bot", email: "00000000+agent-bot@users.noreply.github.com", repos: ["me/a", "me/b"], placeholder: true },
            Object.assign({ token: "work", repos: ["me/a"], placeholder: false }, work)] };
          const c = { identity: { name: "agent-bot", email: "00000000+agent-bot@users.noreply.github.com" },
            tokens: { bot: { repos: ["me/a", "me/b"], secret: {} }, work: { repos: ["me/a"], secret: {}, identity: work } } };
          const G = githubAccess(c);
          const cards = G.node.kids.filter((k) => k.className === "card flush");
          const out = [G.cur, cards.map((k) => !!k.hidden), G.node.kids[0].textContent, sharedRepos(G.chosen), G.idents.bot.name, G.idents.work.name];
          G.chosen.work.delete("me/a");  // untick it: nothing is shared now
          out.push(sharedRepos(G.chosen));
          out.push([accountOf("me/a", "work").token, accountOf("me/b", "work").token, accountOf("me/a", null).token, accountOf("me/c", "work"),
            accountsFor("me/a").map((a) => a.name)]);
          console.log(JSON.stringify(out));
        """)
        self.assertEqual(got, ["bot", [False, True],
                               "me/a is under agent-bot and acme-agent. Agents get agent-bot for it, unless their project or New agent picks another account.",
                               {"me/a": ["bot", "work"]}, "agent-bot", "acme-agent", {},
                               ["work", "bot", "bot", None, ["agent-bot", "acme-agent"]]])

    def test_branch_names_in_settings(self):
        got = self.run_js("""
          const B = branchNames({ identity: { branch_names: "prefix", branch_prefix: "team/" } });
          const cards = find(B.node, "mode-cards").kids;
          console.log(JSON.stringify([B.mode, cards.map((c) => c.textContent), branchPrefix(" team "), branchPrefix("team/"), branchPrefix(""),
            branchNames({}).mode]));
        """)
        self.assertEqual(got[0], "prefix")
        self.assertEqual([c[:30] for c in got[1]], ["Kind and a namefix/login-timeo", "Prefix and a nameteam/login-ti", "Agent IDteam/9a70b774-2The age"])
        self.assertEqual(got[2:], ["team/", "team/", "", "kind"])

    def test_help_that_was_only_a_hover_is_on_the_page(self):
        src = read(os.path.join(STATIC, "pages.js"))
        self.assertIn("New coding agents start from it", src)
        self.assertIn("Over 30 days old, rebuild for updates. ", src)
        self.assertIn("Terminal has square corners, pixel icons and pixel cursors.\"", src)
        self.assertNotIn('title: "Terminal has square corners', src)


class ShownCommandTests(unittest.TestCase):
    def test_the_build_commands_the_ui_shows_parse(self):
        from cilly.__main__ import parser
        pages = read(os.path.join(STATIC, "pages.js"))
        shown = re.search(r"copyBlock\(`(\$\{cli\(\)\} build-base [^`]+)`\)", pages).group(1)
        file_arg = "${from}"  # "--image " and the path as one shell word (shellWord), or --download
        self.assertIn(file_arg, shown)
        for cli in ("python3 -m cilly", "cilly"):
            for arg, image in (("--image ~/x.qcow2", "~/x.qcow2"), ("--download", None)):
                cmd = shown.replace("${cli()}", cli).replace(file_arg, arg).replace("${preset}", "slim")
                argv = shlex.split(cmd)
                skip = 3 if cli.startswith("python") else 1
                a = parser().parse_args(argv[skip:])
                self.assertEqual((a.cmd, a.image, a.download, a.preset), ("build-base", image, image is None, "slim"), cmd)
        custom = re.findall(r"copyBlock\(`(\$\{cli\(\)\} build-base [^`]+)`\)", pages)[1]  # New image → Custom
        a = parser().parse_args(shlex.split(custom.replace("${cli()}", "cilly").replace(file_arg, "--download")
                                            .replace("${mix}", "java,ghidra"))[1:])
        self.assertEqual((a.preset, a.toolchains, a.no_default), (None, "java,ghidra", True))
        self.assertIn('(cli() === "python3 -m cilly" ? ", from the cilly folder:" : ":")', pages)
        # messages name the command the way you started cilly, never a bare `cilly ...` that may not exist
        setup = read(os.path.join(STATIC, "setup.js"))
        # (the sign-in page can't know before you sign in, so it names both: its STARTUP_LINK_CMD line)
        app = "\n".join(ln for ln in read(os.path.join(STATIC, "app.js")).splitlines() if not ln.startswith("const STARTUP_LINK_CMD = "))
        hints = [read(os.path.join(os.path.dirname(STATIC), f)) for f in ("__main__.py", "creds.py", "vm.py", "server.py", "host.py")] + [app]
        for src in hints + [setup, pages]:
            self.assertNotRegex(src, r"(?<!-m )\bcilly (build-base|secrets|serve|doctor|open|setup|service)\b")
        # Debian and Ubuntu have no `python` command, only python3
        for f in os.listdir(STATIC):
            if f.endswith(".js"):
                self.assertNotIn("python -m cilly", read(os.path.join(STATIC, f)), f)
        self.assertIn("In the cilly folder, run python3 -m ${local ? STARTUP_LINK_CMD : STARTUP_LINK_CMD + \" --print\"} instead.", read(os.path.join(STATIC, "app.js")))

    def test_the_debian_image_is_in_vm_images_everywhere(self):
        root = os.path.dirname(os.path.dirname(STATIC))
        readme, guide = read(os.path.join(root, "README.md")), read(os.path.join(root, "docs", "guide.md"))
        setup, pages = read(os.path.join(STATIC, "setup.js")), read(os.path.join(STATIC, "pages.js"))
        for src in (readme, guide, setup, pages):
            self.assertNotIn("Downloads/debian", src)
        for src in (readme, guide):  # downloading it yourself is still how the docs explain it
            self.assertIn("~/vm-images/debian-13-genericcloud-amd64.qcow2", src)
        for src in (setup, pages):  # the panel downloads it, and says how it checks it
            self.assertIn("checks that the download wasn't damaged", src)
            self.assertNotIn("SHA-512 sums", src)  # it checks for damage, not who made it
        section = re.search(r"^#{3,4} \d+\. Build a VM image.*?(?=^#{3,4} )", readme, re.S | re.M).group(0)
        self.assertIn("build-base --download", section)
        self.assertIn("curl -LO https://cloud.debian.org/", section)

class SentenceCaseTests(unittest.TestCase):
    OLD_LABELS = ("This Device", "Stop When Idle", "Stop After Each Task", "Keep Running", "New Agent",
                  "Pair This Device", "Never Reach", "Show More", "Send Now", "Save Settings", "Add to Notes")

    def test_no_title_case_labels_are_left(self):
        for f in sorted(os.listdir(STATIC)):
            if f.endswith(".js"):
                src = read(os.path.join(STATIC, f))
                for label in self.OLD_LABELS:
                    self.assertFalse(label in src, f"{f}: {label}")
        prints = re.findall(r"print\(.*", read(os.path.join(os.path.dirname(STATIC), "server.py")))
        for label in self.OLD_LABELS:
            self.assertFalse([p for p in prints if label in p], label)


class ReadmeContentsTests(unittest.TestCase):
    def test_every_link_into_the_readme_works(self):
        readme = read(os.path.join(os.path.dirname(os.path.dirname(STATIC)), "README.md"))
        # GitHub's anchors: lower case, punctuation dropped, spaces become hyphens. A folded section has <a name>.
        slug = lambda h: re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", h.strip().lower()))  # noqa: E731
        targets = {slug(m.group(1)) for m in re.finditer(r"^#{1,4} (.+)$", readme, re.M)} | set(re.findall(r'<a name="([^"]+)"></a>', readme))
        links = re.findall(r"\]\(#([^)]+)\)", readme) + re.findall(r'href="#([^"]+)"', readme)
        self.assertTrue(links)
        self.assertEqual([x for x in links if x not in targets], [])
        for name in re.findall(r'<a name="([^"]+)"></a>', readme):  # each sits right above its fold
            self.assertIn(f'<a name="{name}"></a>\n<details', readme)


class DeviceDocsTests(unittest.TestCase):
    def test_the_readme_says_a_paired_device_can_open_vm_files(self):
        from cilly.server import device_may
        self.assertTrue(device_may("GET", "/api/agents/0123abcd/file"))
        readme = read(os.path.join(os.path.dirname(os.path.dirname(STATIC)), "README.md"))
        for claim in ("not your settings or files", "can't open your files", "can't reach your files"):
            self.assertNotIn(claim, readme)
        self.assertIn("open the files in an agent's VM", readme)

    def test_the_guide_says_only_the_pc_changes_the_mcp_library(self):
        from cilly.server import device_may
        guide = read(os.path.join(os.path.dirname(os.path.dirname(STATIC)), "docs", "guide.md"))
        add = next(line for line in guide.splitlines() if "adds a server to the MCP library" in line)
        self.assertIn("Only the panel on your PC can add a server.", add)
        self.assertIn("Only the panel on your PC can change the library.", guide)
        for method, path in (("PUT", "/api/policy"), ("PUT", "/api/settings"), ("POST", "/api/mcp"),
                             ("POST", "/api/mcp/linear/delete"), ("POST", "/api/mcp/linear/duplicate")):
            self.assertFalse(device_may(method, path), path)

    def test_paired_can_says_all_that_device_routes_allow_and_no_more(self):
        from cilly.server import DEVICE_ROUTES, device_body_ok, device_may
        app = read(os.path.join(STATIC, "app.js"))
        src = app.split("const PAIRED_CAN = ")[1].split(";\n")[0]
        lists = {k: " | ".join(re.findall(r'"([^"]*)"', v)).lower() for k, v in re.findall(r"(can|cant): \[(.*?)\]", src, re.S)}
        self.assertEqual(sorted(lists), ["can", "cant"])
        self.assertIn('const PAIRED_SHORT = "Approve it once on your PC and it stays signed in. It can use your agents and the inbox, '
                      'but not your PC\'s folders, settings or secrets.";', app)
        self.assertNotIn("not settings or files", app)
        a, x = "/api/agents/0123abcd", "0123abcd"
        can = {"create agents": [("POST", "/api/agents")],
               "chat with them": [("GET", "/api/state"), ("GET", f"{a}/chat"), ("GET", f"{a}/log"), ("GET", f"{a}/memory"),
                                  ("POST", f"{a}/message"), ("POST", f"{a}/interrupt"), ("GET", f"{a}/tasks/{x}/message"),
                                  ("POST", f"{a}/tasks/{x}/cancel"), ("POST", f"{a}/tasks/{x}/send-now")],
               "send them files": [("POST", f"{a}/uploads")],
               "start and stop them": [("POST", f"{a}/start"), ("POST", f"{a}/stop")],
               "turn MCP servers, plugins and skills on and off for them": [("POST", f"{a}/mcp"), ("POST", f"{a}/plugins"), ("POST", f"{a}/skills")],
               "change an agent's model, route, VM image and when it stops": [("POST", f"{a}/model"), ("GET", "/api/models"),
                   ("POST", f"{a}/route"), ("POST", f"{a}/image"),
                   ("POST", f"{a}/auto-stop"), ("POST", f"{a}/idle-stop"), ("POST", f"{a}/when-idle"), ("POST", f"{a}/keep-running")],
               "change its Bash command and task time limits": [("POST", f"{a}/limits")],
               "rename, pin or archive it, move it into a project": [("POST", f"{a}/organize")],
               "link it to a research agent, with YOLO on or off": [("POST", f"{a}/link")],
               "attach a repository": [("POST", f"{a}/attach-repo")],
               "allow a site for one agent on the ports every site gets": [("POST", f"{a}/allow"), ("POST", f"{a}/disallow"),
                                                                          ("POST", f"{a}/egress")],
               "leave sites of the global allowlist out for one agent": [("POST", f"{a}/global-sites")],
               "open the files in an agent's VM": [("GET", f"{a}/files"), ("GET", f"{a}/file")],
               "answer agents in the inbox": [("GET", "/api/inbox"), ("GET", f"{a}/deliveries"), ("GET", f"{a}/deliveries/{x}"),
                   ("GET", f"{a}/denials"), ("POST", f"{a}/denials/dismiss"),
                   *[("POST", f"{a}/{k}/{x}/{v}") for k, vs in (("requests", ("approve", "dismiss")), ("asks", ("answer", "dismiss")),
                                                                 ("deliveries", ("forward", "dismiss"))) for v in vs]],
               "use voice input": [("POST", "/api/transcribe")],
               "see pull requests and image builds": [("GET", "/api/prs"), ("GET", "/api/images/build")],
               "see your themes and fonts": [("GET", f"/api/fonts/{x}")]}
        said = {r for routes in can.values() for r in routes} | {("POST", "/api/devices/me/forget")}  # unpairing itself
        for claim, routes in can.items():
            self.assertIn(claim.lower(), lists["can"])
            for m, p in routes:
                self.assertTrue(device_may(m, p), (claim, m, p))
        for m, rx in DEVICE_ROUTES:  # every route a device has is in the text
            pat = rx.pattern.replace("[0-9a-f]{8}", x)
            alts = re.search(r"\(([a-z|-]+)\)", pat)
            for p in ([pat.replace(alts.group(0), v) for v in alts.group(1).split("|")] if alts else [pat]):
                self.assertIn((m, p), said)
        cant = {"use folders on your PC": not device_body_ok("/api/agents", {"inject": "/home"}) and not device_may("GET", "/api/fs"),
                "add MCP servers or skills": not device_may("POST", "/api/mcp") and not device_may("POST", "/api/skills")
                and not device_may("POST", "/api/skills/x/delete"),
                "allow a site for all agents": not device_body_ok(f"{a}/allow", {"scope": "global"}),
                "delete or reset an agent": not device_may("POST", f"{a}/delete") and not device_may("POST", f"{a}/reset"),
                "add project notes": not device_may("POST", f"/api/projects/{x}/notes/{x}/approve"),
                "approve another device": not device_may("POST", f"/api/devices/pending/{x}/approve"),
                "open Settings, Claude policy or secrets": not any(device_may("GET", p) for p in ("/api/settings", "/api/policy", "/api/secrets"))}
        self.assertTrue(device_body_ok(f"{a}/link", {"research_agent": x, "auto": True}))  # YOLO from a phone, by your choice
        for claim, true in cant.items():
            self.assertIn(claim.lower(), lists["cant"])
            self.assertTrue(true, claim)



class StyleTokenTests(unittest.TestCase):
    """Colors and fonts live in :root tokens, so a theme can change them all in one place."""

    def setUp(self):
        self.css = read(os.path.join(STATIC, "style.css"))
        start = self.css.index(":root {")
        self.root, self.rest = self.css[start:self.css.index("}", start)], self.css[self.css.index("}", start):]

    def test_the_touch_block_covers_the_agent_views_small_controls(self):
        block = self.css[self.css.index("/* ---- touch:"):]
        block = block[:block.index("\n}\n")]
        for sel in [".state-pill", ".send-pill", ".q-act", ".tool-line.clickable", ".running-row", ".file-chip .x", ".m-state", ".ctx-tag",
                    ".inert-note", ".ask-opt", ".host-add"]:
            self.assertIn(sel, block, sel)
        for small in ["min-height: 36px", "min-height: 40px"]:  # a phone rule later in the file would beat the shared 44 px
            self.assertNotIn(".q-act, .queued-card .btn.small { " + small, self.css)

    def test_the_touch_block_covers_the_pages_small_controls(self):
        block = self.css[self.css.index("/* ---- touch:"):]
        block = block[:block.index("\n}\n")]
        for sel in [".add-row", "details.adv > summary", ".step-exit", '.color-row input[type="color"]', ".sec-grid .ellipsis"]:
            self.assertIn(sel, block, sel)

    def test_page_rows_keep_to_the_4_px_grid_and_one_save_look(self):
        for rule in [".note-row {", ".opt-sec {", ".check-row {", ".image-pick {", ".repo-row {"]:
            line = self.css[self.css.index(rule):].split("\n")[0] + self.css[self.css.index(rule):].split("\n")[1]
            self.assertNotRegex(line.split("}")[0], r"padding: (9|10|11|13|14)px", rule)
        self.assertIn(".toast, .look-bar, .conn-bar { box-shadow", self.css)  # one shadow for the floating bars
        self.assertNotRegex(self.css[self.css.index(".save-bar { flex: none"):].split("\n")[0], "box-shadow")  # the save bar is pinned, not floating
        self.assertIn(".setup-nav { flex-direction: column-reverse;", self.css)  # Continue on top, nothing runs off a phone

    def test_text_sizes_keep_to_whole_pixels(self):
        terminal = read(os.path.join(STATIC, "theme", "terminal.css"))
        sizes = re.findall(r"calc\(([\d.]+)px \* var\(--[a-z]+-scale\)\)", self.css + terminal)
        self.assertGreater(len(sizes), 100)
        self.assertEqual([s for s in sizes if "." in s], [])  # one scale, no 12.5 beside 12 and 13

    def test_a_long_toolchain_name_keeps_clear_of_its_note(self):
        # the name's box shows its (i) outside it, so the name itself mustn't shrink under the note ("Reverse engineering")
        self.assertIn(".image-pick > .grow.ellipsis { flex: 1 0 auto; }", self.css)
        self.assertIn(".image-pick > .dim.nowrap { white-space: normal;", self.css)

    def test_long_explanations_wait_behind_an_i(self):
        js = "".join(read(os.path.join(STATIC, f)) for f in ("pages.js", "projects.js"))
        for text in ("Enforced in every agent VM", "Secrets from a command, like pass", "The list comes from GitHub", "cilly runs none of these",
                     "How the panel looks, here and on", "One per line. *.example.com covers subdomains. A site gets",
                     "At /attachments/project/", "Ephemeral agents here copy ~/outbox", "Coding agents here ask this research agent",
                     "On top of the global allowlist"):
            at = js.index(text)
            self.assertEqual(js.count(text), 1, text)
            self.assertRegex(js[max(0, at - 420):at], r"infoTip\(|tip: ", text)
        # what you need while setting things up stays in sight
        for text in ("whisper-server has no login or HTTPS", "Ollama gives models only 4,096 tokens"):
            self.assertNotRegex(js[max(0, js.index(text) - 40):js.index(text)], r"infoTip\(", text)

    def test_the_phones_go_to_button_has_a_pixel_icon_too(self):
        terminal = read(os.path.join(STATIC, "theme", "terminal.css"))
        self.assertIn('& .m-find::before { --i: image-set(url("icons/tool-search.png")', terminal)
        self.assertIn("& .m-find svg { display: none; }", terminal)

    def test_every_token_used_is_defined(self):
        terminal = read(os.path.join(STATIC, "theme", "terminal.css"))
        defined = set(re.findall(r"(--[a-z0-9-]+):", self.root + terminal))  # with the Terminal style's own
        used = set(re.findall(r"var\((--[a-z0-9-]+)", self.css + terminal))
        self.assertEqual(used - defined, set())
        self.assertEqual(defined - used, set())

    def test_pages_share_the_chat_column(self):
        for rule in (".page-inner { max-width: 780px;", ".set-body { max-width: 780px;", ".page-inner.chatw { max-width: 780px;"):
            self.assertIn(rule, self.css)
        head = self.css[self.css.index(".set-head-inner {"):].split("\n")[0]  # a page's header runs full width, like an agent's
        self.assertNotIn("max-width", head)
        self.assertIn("padding: 16px 20px 0;", head)  # its edge lines up with the agent header's 20 px
        pages = read(os.path.join(STATIC, "pages.js"))
        for title in ('"Claude policy"),', '"Pull requests"),'):
            self.assertIn(title, pages[pages.index("const pageHead"):])

    def test_every_classic_icon_is_served(self):
        from cilly.server import _STATIC_FILES as STATIC_FILES
        css = read(os.path.join(STATIC, "theme", "classic.css"))
        urls = set(re.findall(r'url\("(phosphor/[a-z-]+\.svg)"\)', css))
        self.assertGreater(len(urls), 20)
        for u in urls:
            self.assertEqual(STATIC_FILES["/theme/" + u][1], "image/svg+xml", u)
            self.assertTrue(os.path.isfile(os.path.join(STATIC, "theme", u)), u)

    def test_the_terminal_style_never_touches_classic(self):
        css = re.sub(r"/\*.*?\*/", "", read(os.path.join(STATIC, "theme", "terminal.css")), flags=re.S)
        depth, top = 0, []
        for m in re.finditer(r"([^{}]*)([{}])", css):  # every rule at the top is the Terminal scope, a keyframes or a media query
            if m.group(2) == "{":
                if depth == 0:
                    top.append(m.group(1).strip())
                depth += 1
            else:
                depth -= 1
        self.assertEqual(depth, 0)
        for sel in top:
            self.assertTrue(sel == ':root[data-style="terminal"]' or sel.startswith(("@keyframes", "@media")), sel)
        for sel in re.findall(r"@media[^{]*\{\s*([^{]+)\{", css):
            self.assertTrue(sel.strip().startswith(':root[data-style="terminal"]'), sel)
        # colors only from the tokens (black shadows aside), and every picture is served
        self.assertEqual([c for c in re.findall(r"#[0-9a-fA-F]{3,8}\b", css) if c != "#000"], [])
        from cilly.server import _STATIC_FILES as STATIC_FILES
        urls = re.findall(r'url\("([^"]+)"\)', css)
        self.assertGreater(len(urls), 30)
        for u in urls:
            self.assertIn("/theme/" + u, STATIC_FILES)

    def test_no_color_outside_the_tokens(self):
        rest = re.sub(r"@font-face[^}]*}", "", self.rest)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", rest), [])
        self.assertEqual([c for c in re.findall(r"rgba?\([^)]*\)", rest) if not c.startswith("rgba(0, 0, 0,")], [])

    def test_touch_screens_get_44_px_targets(self):
        block = re.search(r"@media \(pointer: coarse\), \(max-width: 640px\) \{(.*?)\n\}", self.css, re.S).group(1)
        grown = re.search(r":is\(([^)]*)\)::after", block).group(1)  # one list of small controls whose target grows around them
        for sel in (".chip", ".link", ".fb-sort"):
            self.assertIn(sel, grown)
        for sel in (".btn", ".tab", ".icon-btn", ".switch::before", ".seg button", ".crumb", ".agent-chip", ".agent-sub"):
            self.assertIn(sel, block)
        self.assertIn("min-height: 44px", block)

    def test_the_image_viewer_blurs_the_page_behind_it(self):
        self.assertRegex(self.css, r"\.lightbox \{[^}]*backdrop-filter: blur")
        self.assertRegex(self.css, r"\.lb-foot \{[^}]*grid-template-columns: 1fr auto 1fr")
        self.assertIn(".lb-close {", self.css)

    def test_the_switcher_button_says_go_to_where_there_is_no_keyboard(self):
        self.assertRegex(self.css, r"@media \(hover: none\) \{ \.sb-kbd \.kbd-key \{ display: none; \} \.sb-kbd \.kbd-touch \{ display: inline; \}")

    def test_a_tall_modal_or_sign_in_page_scrolls(self):
        self.assertRegex(self.css, r"\.modal-back \{[^}]*overflow-y: auto")
        self.assertRegex(self.css, r"\.center-page \{[^}]*overflow-y: auto")

    def test_toast_keeps_clear_of_the_phone_bottom_bar(self):
        self.assertRegex(self.css, r"\.toasts \{ top: calc\(12px \+ env\(safe-area-inset-top\)\); bottom: auto;")

    def test_every_font_is_served(self):
        from cilly.server import _STATIC_FILES as STATIC_FILES
        for url in re.findall(r'url\("(/fonts/[^"]+)"\)', self.css):
            self.assertIn(url, STATIC_FILES)
            self.assertTrue(os.path.isfile(os.path.join(STATIC, STATIC_FILES[url][0])), url)


if __name__ == "__main__":
    unittest.main()
