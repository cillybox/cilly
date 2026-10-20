// Checks that new chat output never moves what you're reading, at 1280 and 390 wide. Needs the harness
// with a stream of events and Playwright (the Browser toolchain has it):
//
//     FAKE_STREAM=150 FAKE_STREAM_DELAY=0.4 python3 tools/harness.py --seed
//     node tools/chat_scroll_check.js
//
// It sends two messages and a long run of events to two agents, then checks: an expanded tool output
// scrolled halfway stays there, the chat scrolled up stays on the same message, a text selection stays,
// and near the bottom the chat follows new output. Exits 1 when a check fails or the page has an error.
const { chromium } = require("playwright");
const base = "http://127.0.0.1:8765", H = { "X-Panel-Token": "testtoken", "Content-Type": "application/json" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function steps(page) { return page.evaluate(() => [...document.querySelectorAll(".msg-claude")].filter((n) => /^Step \d+/.test(n.textContent)).length); }
async function waitSteps(page, n) { const end = Date.now() + 60000; while (Date.now() < end) { if (await steps(page) >= n) return; await sleep(200); } throw new Error("events didn't arrive"); }
(async () => {
  const fails = [], errors = [];
  const check = (what, got, want, near = 2) => {
    const ok = typeof want === "number" ? Math.abs(got - want) <= near : got === want;
    console.log(`${ok ? "ok  " : "FAIL"} ${what}: ${JSON.stringify(got)}${ok ? "" : `, expected ${JSON.stringify(want)}`}`);
    if (!ok) fails.push(what);
  };
  const st = await (await fetch(base + "/api/state", { headers: H })).json();
  const browser = await chromium.launch();
  for (const [w, h, name, agent] of [[1280, 800, "desktop", "sprite-maker"], [390, 844, "phone", "web-fixer"]]) {
    const id = st.agents_list.find((a) => a.name === agent).id;
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    page.on("pageerror", (e) => errors.push(`${name}: ${e.message}`));
    await page.goto(`${base}/#t=testtoken&a=${id}`);
    await sleep(1500);
    await fetch(`${base}/api/agents/${id}/message`, { method: "POST", headers: H, body: JSON.stringify({ instructions: "Look at the tests first" }) });
    await sleep(2500);
    await fetch(`${base}/api/agents/${id}/message`, { method: "POST", headers: H, body: JSON.stringify({ instructions: "Look at the build first" }) });
    await sleep(2500);
    await fetch(`${base}/api/agents/${id}/message`, { method: "POST", headers: H, body: JSON.stringify({ instructions: "stream events please" }) });
    await waitSteps(page, 2);
    // A: an expanded tool output scrolled halfway
    const row = page.locator(".tool-line", { hasText: "./gradlew build" }).last();
    await row.click();
    await sleep(300);
    const half = await page.evaluate(() => {
      const pre = [...document.querySelectorAll(".tool-out")].pop();
      pre.scrollIntoView({ block: "center" });
      pre.scrollTop = Math.round((pre.scrollHeight - pre.clientHeight) / 2);
      return pre.scrollTop;
    });
    let n = await steps(page);
    await waitSteps(page, n + 20);
    const after = await page.evaluate(() => [...document.querySelectorAll(".tool-out")].pop()?.scrollTop ?? -1);
    check(`${name}: expanded tool output keeps its scroll position`, after, half);
    // B: the chat scrolled up to an earlier message
    const pos = () => page.evaluate(() => {
      const sc = document.querySelector(".page"), m = [...document.querySelectorAll(".msg-you")].at(-2);
      return Math.round(m.getBoundingClientRect().top - sc.getBoundingClientRect().top);
    });
    await page.evaluate(() => { const sc = document.querySelector(".page"), m = [...document.querySelectorAll(".msg-you")].at(-2); sc.scrollTop += m.getBoundingClientRect().top - sc.getBoundingClientRect().top - 80; });
    await sleep(200);
    const p0 = await pos();
    n = await steps(page);
    await waitSteps(page, n + 20);
    check(`${name}: scrolled up, the same message stays in place`, await pos(), p0);
    // D: a text selection in an earlier reply
    const picked = await page.evaluate(() => {
      const m = [...document.querySelectorAll(".msg-claude")].find((n) => n.textContent.startsWith("Looking at the build"));
      const t = m.querySelector("p")?.firstChild || m.firstChild.firstChild;
      document.getSelection().setBaseAndExtent(t, 0, t, 12);
      return document.getSelection().toString();
    });
    n = await steps(page);
    await waitSteps(page, n + 5);
    check(`${name}: a text selection stays`, await page.evaluate(() => document.getSelection().toString()), picked);
    // C: near the bottom it follows new output
    await page.evaluate(() => { const sc = document.querySelector(".page"); sc.scrollTop = sc.scrollHeight; });
    await sleep(300);
    n = await steps(page);
    await waitSteps(page, n + 5);
    await sleep(1600);
    check(`${name}: near the bottom, it follows new output`,
      await page.evaluate(() => { const sc = document.querySelector(".page"); return Math.round(sc.scrollHeight - sc.scrollTop - sc.clientHeight); }), 0, 5);
    await page.close();
  }
  await browser.close();
  for (const e of errors) console.log("page error: " + e);
  process.exit(fails.length || errors.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
