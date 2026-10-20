// Retakes the README's screenshots in docs/images from the harness's demo agents, at the sizes they have now,
// and records the tour GIF at the top of the README (tour.gif, which needs ffmpeg).
// Needs Playwright (the Browser toolchain has it). It starts its own harness, so nothing else needs to run:
//
//     node tools/readme_shots.js
//
// The website agent's screenshot (home-mobile.png) is a small sample page drawn here first, so the fake VM
// has a real picture to show. Exits 1 when the page has an error.
const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs"), http = require("http"), os = require("os"), path = require("path");
const REPO = path.dirname(__dirname), OUT = path.join(REPO, "docs", "images");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "cilly-shots-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PHONE = 600 / 390;  // a phone's frame is 600 px wide in phone.png and pairing.png
const IPHONE = { isMobile: true, hasTouch: true,
  userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1" };

// The page the website agent "fixed", as its screenshot in the outbox
const SAMPLE = `<!doctype html><meta charset="utf-8"><style>
@font-face { font-family: Geist; src: url("${path.join(REPO, "cilly/static/fonts/Geist-Variable.woff2")}"); font-weight: 100 900; }
@font-face { font-family: "Geist Mono"; src: url("${path.join(REPO, "cilly/static/fonts/GeistMono-Variable.woff2")}"); }
body { margin: 0; background: #12171a; color: #f2efe8; font: 15px/1.5 Geist, sans-serif; }
nav { position: sticky; top: 0; display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; background: #0e1316; }
nav b { color: #f0a456; letter-spacing: .04em; font-size: 14px; } nav i { width: 22px; height: 2px; background: #eee; box-shadow: 0 -6px #eee, 0 6px #eee; }
.hero { padding: 56px 22px 40px; background: linear-gradient(160deg, #183b3c, #13283a 70%, #12171a); }
.tag { display: inline-block; background: #7cc48a; color: #10251a; font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 99px; }
h1 { font-size: 36px; line-height: 1.12; margin: 14px 0 12px; } p { color: #c9cfcf; margin: 0 0 22px; }
.ip { display: flex; justify-content: space-between; padding: 12px 14px; border-radius: 10px; background: #0c1114; font: 14px "Geist Mono", monospace; }
.ip span { color: #f0a456; font: 600 12px Geist; } .btn { margin-top: 12px; text-align: center; padding: 12px; border-radius: 10px; background: #f0a456; color: #231307; font-weight: 600; }
.on { margin-top: 18px; color: #7cc48a; font-size: 13px; } .cards { padding: 24px 22px; display: grid; gap: 12px; }
.card { padding: 16px; border-radius: 12px; border: 1px solid #263036; background: #161d21; } .card b { display: block; margin-bottom: 4px; } .card p { font-size: 13px; margin: 0; }
</style><nav><b>LANTERN</b><i></i></nav><div class="hero"><span class="tag">Version 2 is out</span><h1>Notes that stay on your own devices</h1>
<p>Plain Markdown files, synced end to end encrypted. Works offline, on every device you own.</p><div class="ip">brew install lantern<span>Copy</span></div>
<div class="btn">Download for free</div><div class="on">● Open source and free forever</div></div><div class="cards">
<div class="card"><b>Plain Markdown</b><p>Every note is a file you can open in any editor. No lock-in.</p></div>
<div class="card"><b>Sync you control</b><p>Run the sync server yourself, or use ours. Either way, we can't read your notes.</p></div>
<div class="card"><b>Works offline</b><p>Write on a plane. Your notes sync the moment you land.</p></div></div>`;

function harness(image) {
  return new Promise((resolve, reject) => {
    const p = spawn("python3", [path.join(REPO, "tools/harness.py"), "--demo", "--port", "0"], { env: { ...process.env, FAKE_OUTBOX_IMAGE: image } });
    let out = "";
    p.stdout.on("data", (d) => { out += d; const m = /READY (http:\/\/127\.0\.0\.1:\d+)/.exec(out); if (m) resolve([p, m[1]]); });
    p.stderr.on("data", (d) => process.stderr.write(d));
    p.on("exit", (code) => reject(new Error(`the harness stopped (${code})`)));
  });
}

// The host line under the sidebar says "bwrap off" here, as the harness runs without bubblewrap. A real PC doesn't.
// Through the page's own stylesheet, as its content security policy refuses a <style> element.
const hideHost = (page) => page.evaluate(() => document.styleSheets[0].insertRule(".sb-host { visibility: hidden }", 0));

(async () => {
  const errors = [];
  const browser = await chromium.launch();
  const image = path.join(TMP, "home-mobile.png");
  const sample = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  fs.writeFileSync(path.join(TMP, "sample.html"), SAMPLE);
  await sample.goto("file://" + path.join(TMP, "sample.html"));
  await sample.screenshot({ path: image });
  const [hp, base] = await harness(image);
  const st = await (await fetch(base + "/api/state", { headers: { "X-Panel-Token": "testtoken" } })).json();
  const id = (name) => st.agents_list.find((a) => a.name === name).id;
  // a page with the startup link's full access, or signed in as a paired device (its token)
  const open = async (width, height, scale, opts = {}, device = null) => {
    const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: scale, ...opts });
    if (device) await ctx.addInitScript((t) => localStorage.setItem("cilly-device-token", t), device);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(base + (device ? "/" : "/#t=testtoken"));
    await hideHost(page);
    await sleep(1500);
    return page;
  };
  const go = async (page, view, o) => { await page.evaluate(([v, x]) => go(v, x), [view, o || {}]); await sleep(1200); };
  const shot = (page, name, clip) => page.screenshot({ path: path.join(OUT, name), clip });
  const toBottom = (page) => page.evaluate(() => { for (const n of document.querySelectorAll(".chat, .page")) n.scrollTop = n.scrollHeight; });

  // the PC: 1280 wide, at 1.25 so they come out 1600 px wide
  let page = await open(1280, 1260, 1.25);
  await go(page, "agent", { agentId: id("website"), tab: "chat" });
  await page.evaluate(() => document.querySelector(".msg-you").scrollIntoView({ block: "start" })); await sleep(400);  // from your message on
  await shot(page, "chat.png");
  await page.setViewportSize({ width: 1280, height: 820 });
  await go(page, "agent", { agentId: id("website"), tab: "sandbox" });
  await shot(page, "sandbox.png");
  await go(page, "agent", { agentId: id("website"), tab: "files" });
  await page.evaluate((aid) => openFile(agentById(aid), "outbox", "home-mobile.png"), id("website"));
  await sleep(1500);
  await shot(page, "viewer.png");
  await page.keyboard.press("Escape");
  await go(page, "review", { reviewKey: await page.evaluate(() => INBOX.items.find((i) => i.kind === "findings").key) });
  await shot(page, "research.png");
  await go(page, "new");
  await page.setViewportSize({ width: 1506, height: 821 });  // New agent's own column: 1242 wide, without the sidebar
  await page.evaluate(() => { const p = VIEWS.new.p; p.ta.value = "Add a GET /export endpoint that sends all of a user's notes as a zip of Markdown files. Write tests for it.";
    p.ta.dispatchEvent(new Event("input")); p.repo.value = "lantern/sync-server"; p.repo.dispatchEvent(new Event("change")); });
  await page.fill('input[aria-label="Name"]', "export-zip").catch(() => {});
  await sleep(500);
  const col = await page.evaluate(() => { const r = document.getElementById("main").getBoundingClientRect(); return { x: r.x, width: r.width }; });
  await shot(page, "new-agent.png", { x: col.x, y: 0, width: col.width, height: 821 });  // without the sidebar, whatever the theme's width

  // the inbox popover, at 2
  page = await open(1280, 800, 2);
  await page.evaluate(() => openInbox()); await sleep(1000);
  const pop = await page.evaluate(() => { const r = document.querySelector(".inbox-pop").getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: Math.min(530, r.height) }; });  // just the popover, wherever it opens
  await shot(page, "inbox.png", pop);

  // phones: 390 wide, framed 600 px wide, signed in as a phone paired the way the README says
  const post = (p, body, tok) => fetch(base + p, { method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", ...(tok ? { "X-Panel-Token": tok } : {}) } }).then((r) => r.json());
  const ask = await post("/api/pair/start", { name: "iPhone" });
  const pending = (await (await fetch(base + "/api/devices", { headers: { "X-Panel-Token": "testtoken" } })).json()).pending;
  await post(`/api/devices/pending/${pending.find((d) => d.code === ask.code).id}/approve`, {}, "testtoken");
  const paired = (await post("/api/pair/poll", { pair: ask.pair })).token;
  const phoneShot = async (setup) => {
    const p = await open(390, 844, PHONE, IPHONE, paired);
    await setup(p);
    return "data:image/png;base64," + (await p.screenshot()).toString("base64");
  };
  const list = await phoneShot(async (p) => go(p, "agents"));
  const chat = await phoneShot(async (p) => { await go(p, "agent", { agentId: id("website"), tab: "chat" }); await toBottom(p);
    await p.evaluate(() => document.querySelector(".chat-thumb")?.scrollIntoView({ block: "start" })); await sleep(400); });  // from the picture on
  const inbox = await phoneShot(async (p) => go(p, "inbox"));
  // pairing: the phone asks with its code, and the PC's review page shows the same code with Pair
  // The phone opens https://cilly.home, as it would through your HTTPS proxy: its requests go to the harness
  // with the headers the proxy adds, so it sees what a phone sees and not the PC's startup link.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: PHONE, serviceWorkers: "block", ...IPHONE });
  await phone.route("https://cilly.home/**", (route) => {
    const req = route.request(), u = new URL(req.url()), to = new URL(base);
    const out = http.request({ host: to.hostname, port: to.port, path: u.pathname + u.search, method: req.method(),
      headers: { ...req.headers(), host: "cilly.home", "x-forwarded-proto": "https" } }, (res) => {
      const body = [];
      res.on("data", (d) => body.push(d));
      res.on("end", () => route.fulfill({ status: res.statusCode, headers: res.headers, body: Buffer.concat(body) }));
    });
    out.on("error", () => route.abort());
    out.end(req.postDataBuffer() || undefined);
  });
  const pairPage = await phone.newPage();
  pairPage.on("pageerror", (e) => errors.push(e.message));
  await pairPage.goto("https://cilly.home/"); await sleep(1500);
  await pairPage.getByRole("button", { name: "Pair this device" }).click(); await sleep(1500);
  const asking = "data:image/png;base64," + (await pairPage.screenshot()).toString("base64");
  page = await open(1280, 820, 2);
  await page.evaluate(() => pollInbox()); await sleep(800);
  await go(page, "review", { reviewKey: await page.evaluate(() => INBOX.items.find((i) => i.kind === "device").key) });
  const card = await page.evaluate(() => { const r = document.querySelector(".page-inner.review").getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
  const pc = "data:image/png;base64," + (await page.screenshot({ clip: { x: card.x, y: card.y, width: card.width, height: Math.min(card.height, 560) } })).toString("base64");

  // put the phones in frames, on a transparent background
  const frame = (src) => `<div class="phone"><img src="${src}"></div>`;
  const compose = async (name, width, height, body) => {
    const c = await browser.newPage({ viewport: { width, height } });
    await c.setContent(`<style>body { margin: 0; display: flex; gap: 48px; align-items: center; background: transparent; }
      .phone { width: 600px; height: 1298px; border-radius: 64px; overflow: hidden; border: 3px solid #3a3a3a; box-sizing: border-box; flex: none; background: #1e1e1e; }
      .phone img { width: 100%; height: 100%; object-fit: cover; object-position: top; display: block; }
      .pc { flex: 1; border-radius: 28px; overflow: hidden; border: 3px solid #3a3a3a; } .pc img { width: 100%; display: block; }</style>${body}`);
    await c.waitForLoadState("load");
    await c.screenshot({ path: path.join(OUT, name), omitBackground: true });
  };
  await compose("phone.png", 1896, 1298, frame(list) + frame(chat) + frame(inbox));
  await compose("pairing.png", 1572, 1298, frame(asking) + `<div class="pc"><img src="${pc}"></div>`);
  for (const d of (await (await fetch(base + "/api/devices", { headers: { "X-Panel-Token": "testtoken" } })).json()).pending)
    await post(`/api/devices/pending/${d.id}/deny`, {}, "testtoken");  // so the tour doesn't show its request

  // the tour: the chat, the inbox, allowing a site, the Sandbox tab and the screenshot the agent took
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, recordVideo: { dir: TMP, size: { width: 1280, height: 800 } } });
  page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  const t0 = Date.now();
  await page.goto(base + "/#t=testtoken"); await hideHost(page);
  await go(page, "agent", { agentId: id("website"), tab: "chat" });
  await page.evaluate(() => document.querySelector(".msg-you").scrollIntoView({ block: "start" }));
  const from = (Date.now() - t0) / 1000;
  await sleep(1200);
  for (let i = 0; i < 30; i++) { await page.evaluate(() => { document.querySelector(".chat").scrollTop += 22; }); await sleep(50); }
  await sleep(1500);
  await page.evaluate(() => openInbox()); await sleep(1800);
  await page.getByRole("button", { name: "Allow" }).first().click();
  await sleep(1500);
  await page.keyboard.press("Escape"); await sleep(300);
  await go(page, "agent", { agentId: id("website"), tab: "sandbox" }); await sleep(1800);
  await go(page, "agent", { agentId: id("website"), tab: "files" });
  await page.evaluate((aid) => openFile(agentById(aid), "outbox", "home-mobile.png"), id("website"));
  await sleep(2500);
  const to = (Date.now() - t0) / 1000;
  await ctx.close();
  execFileSync("ffmpeg", ["-loglevel", "error", "-y", "-ss", String(from), "-to", String(to), "-i", await page.video().path(), "-vf",
    "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
    path.join(OUT, "tour.gif")]);

  await browser.close();
  hp.kill();
  fs.rmSync(TMP, { recursive: true, force: true });
  console.log(errors.length ? "page errors:\n" + errors.join("\n") : "no page errors");
  process.exit(errors.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
