"""The chat redraws only the messages whose content changed, in a real browser (Playwright, from the
Browser toolchain).  Skipped where Node or Playwright isn't installed: the panel itself needs neither."""

import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "cilly", "static")
FILES = ["app.js", "md.js", "agent.js", "ask.js", "inbox.js", "viewer.js", "pages.js", "projects.js", "mobile.js"]


def playwright_ok() -> bool:
    """Node, Playwright and a browser it can launch.  The module alone isn't enough."""
    if not shutil.which("node"):
        return False
    try:
        r = subprocess.run(["node", "-e", "require('playwright').chromium.launch().then((b) => b.close())"],
                           capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


SCRIPT = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch(), page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("http://cilly.test/**", (r) => r.fulfill({ contentType: "text/html", body: "<div id=app></div>" }));
  await page.goto("http://cilly.test/");
  for (const f of JSON.parse(process.argv[2])) await page.addScriptTag({ content: require("fs").readFileSync(f, "utf8") });
  const out = await page.evaluate(() => {
    const at = (m) => `2026-10-15T01:${String(m).padStart(2, "0")}:00Z`;
    const a = { id: "abcd1234", name: "t", state: "busy", running: true, mode: "persistent", queue: [], requests: [], tasks: [
      { id: "t1", state: "done", instructions: "first", created: at(1), started: at(1), finished: at(2) },
      { id: "t2", state: "running", instructions: "second", created: at(3), started: at(3) }] };
    STATE = { agents_list: [a], projects: [] };
    UI.agentId = a.id;
    AV = { id: a.id, body: el("div", { class: "page" }) };
    AV.chat = el("div", { class: "chat" });
    AV.body.append(AV.chat);
    document.body.append(AV.body);
    CHAT[a.id] = { events: [{ seq: 1, task: "t1", k: "tool", id: "c1", tool: "Bash", verb: "Ran", target: "make" },
      { seq: 2, task: "t1", k: "result", id: "c1", ok: true, summary: "ok", out: "built" },
      { seq: 3, task: "t2", k: "text", text: "Working on it." }], last: 3 };
    const block = (id) => AV.chat.querySelector(`[data-ck="t${id}"]`);
    const draw = () => { SIGS.chat = null; renderChat(agentById(a.id)); return [block("t1"), block("t2")]; };
    const r = {}, [d1, r1] = draw();
    CHAT[a.id].events.push({ seq: 4, task: "t2", k: "text", text: "Found it." });
    CHAT[a.id].last = 4;
    const [d2, r2] = draw();
    r.newOutput = [d2 === d1, r2 === r1, r2.textContent.includes("Found it.")];
    a.tasks.push({ id: "t3", state: "queued", instructions: "third", created: at(5) });
    a.queue = ["t3"];
    const [d3, r3] = draw();
    r.queued = [d3 === d2, r3 === r2, !block("t3")];  // it waits in a card above the message box
    toggleOpen("t1");  // expand the tool line in the finished task
    const [d4, r4] = draw();
    r.expand = [d4 === d3, r4 === r3, !!d4.querySelector(".tool-out")];
    a.tasks.find((t) => t.id === "t3").state = "cancelled";  // taken back before it ran
    a.queue = [];
    draw();
    r.cancelled = [!block("t3"), AV.chat.childElementCount];
    // a background command with no steps of its own: its card and the tool row inside need different keys
    CHAT[a.id].events.push({ seq: 5, task: "t2", k: "tool", id: "b1", tool: "Bash", verb: "Ran", target: "./gradlew test", bg: true },
      { seq: 6, task: "t2", k: "sub", id: "b1", state: "running", desc: "./gradlew test" });
    CHAT[a.id].last = 6;
    toggleOpen("a5");
    draw();
    const cks = [...AV.chat.querySelectorAll("[data-ck]")].map((n) => n.dataset.ck);
    r.keys = [cks.length === new Set(cks).size, cks.includes("a5"), cks.includes("e5")];
    // what a reused block shows but its task record doesn't: the research agent's name, and today's date
    STATE.agents_list.push({ id: "beefbeef", name: "scout", tasks: [] });
    EXTRA[a.id] = { deliveries: [{ id: "d1", research_agent: "beefbeef", forwarded_to: [a.id], created: at(2) }] };
    draw();
    const withName = block("t1");
    STATE.agents_list[1].name = "scout-2";
    const [renamed] = draw();
    r.names = [renamed !== withName, renamed.textContent.includes("scout-2")];
    a.tasks[0].finished = new Date().toISOString();  // ended today: its time shows without a date
    const [today] = draw();
    const RealDate = Date, later = RealDate.now() + 2 * 86400000;  // two days on, the same time needs its date
    Date = class extends RealDate { constructor(...v) { super(...(v.length ? v : [later])); } static now() { return later; } };
    const [nextDay] = draw();
    Date = RealDate;
    r.day = [nextDay !== today];
    return r;
  });
  console.log(JSON.stringify({ out, errors }));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@unittest.skipUnless(playwright_ok(), "node with Playwright not installed")
class ChatBlockTests(unittest.TestCase):
    def test_only_changed_messages_are_rebuilt(self):
        r = subprocess.run(["node", "-", json.dumps([os.path.join(STATIC, f) for f in FILES])], input=SCRIPT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        got = json.loads(r.stdout)
        self.assertEqual(got["errors"], [])
        out = got["out"]
        self.assertEqual(out["newOutput"], [True, False, True])  # new output rebuilds only the running task
        self.assertEqual(out["queued"], [True, True, True])  # a queued message isn't in the chat until it runs
        self.assertEqual(out["expand"], [False, True, True])  # opening a tool line rebuilds its own task only
        self.assertEqual(out["cancelled"], [True, 2])
        self.assertEqual(out["keys"], [True, True, True])  # card "a5", its tool row "e5", nothing shared
        self.assertEqual(out["names"], [True, True])
        self.assertEqual(out["day"], [True])


TABLE = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch(), page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.route("http://cilly.test/**", (r) => r.fulfill({ contentType: "text/html", body: "<div id=app></div>" }));
  await page.goto("http://cilly.test/");
  const [css, ...js] = JSON.parse(process.argv[2]);
  await page.addStyleTag({ content: require("fs").readFileSync(css, "utf8") });
  for (const f of js) await page.addScriptTag({ content: require("fs").readFileSync(f, "utf8") });
  const out = await page.evaluate(() => {
    const md = "| Area | What the docs say | What it means for your client |\n|---|---|---|\n"
      + "| Rides, Farefeed API | `POST /farefeed/v1/estimate` returns services with ETA and a `directDeepLink` such as "
      + "`grab://open?screenType=BOOKING&taxiTypeId=302`. | Fare estimates are possible, but only through a partner OAuth token. |";
    const chat = el("div", { class: "msg-claude" }, renderMarkdown(md));
    const box = el("div", { class: "chat" }, chat);
    box.style.width = "350px";
    document.body.append(box);
    const cells = [...chat.querySelectorAll("th, td")].map((c) => c.getBoundingClientRect().width);
    const wrap = chat.querySelector(".md-table-wrap");
    return { narrowest: Math.min(...cells), fontPx: parseFloat(getComputedStyle(chat.querySelector("th")).fontSize),
      scrolls: wrap.scrollWidth > wrap.clientWidth, pageWide: document.documentElement.scrollWidth > 390 };
  });
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@unittest.skipUnless(playwright_ok(), "node with Playwright not installed")
class TableTests(unittest.TestCase):
    def test_a_table_keeps_readable_columns_on_a_phone(self):
        """Chat text may break anywhere, which let a table column shrink to one letter on a phone.
        A column stays a few words wide, and the table scrolls in its own box, not the page."""
        files = [os.path.join(STATIC, "style.css")] + [os.path.join(STATIC, f) for f in ("app.js", "md.js")]
        r = subprocess.run(["node", "-", json.dumps(files)], input=TABLE, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout)
        self.assertGreaterEqual(out["narrowest"], 7 * out["fontPx"], out)
        self.assertTrue(out["scrolls"], out)
        self.assertFalse(out["pageWide"], out)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SkipTests(unittest.TestCase):
    def test_skips_when_the_browser_is_missing(self):
        env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=os.path.join(ROOT, "no-browsers-here"))
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_chat_blocks.ChatBlockTests"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("OK (skipped=1)", r.stderr)


if __name__ == "__main__":
    unittest.main()
