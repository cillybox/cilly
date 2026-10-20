// cilly UI: the agent view (header, sandbox/start/menu popovers, tabs, chat, composer).
"use strict";

const CHAT = {};      // agent id -> { events: [], last: 0 }
const COMPOSER = {};  // agent id -> { draft, cont, resume, newBranch, prTitle, attach: [] }
const EXTRA = {};     // agent id -> { deliveries, denials }
const BOOTCON = {};   // agent id -> last serial console lines while booting
// The last console lines for the boot preview: the stored marks (inert_log) turned back into the codes
// they stand for, and those cleaned away, so the preview reads as plain text.
function bootLines(text) {
  const codes = String(text ?? "").replace(/[\u2400-\u241f\u2421]/g, (c) => {
    const n = c.charCodeAt(0);
    return String.fromCharCode(n === 0x2421 ? 0x7f : n - 0x2400);
  });
  return clean(codes).split("\n").map((s) => s.trim()).filter(Boolean).slice(-3);
}
const OPEN_DIFFS = new Set();
let AV = null;        // the mounted agent view's nodes and timers

const isStopped = (a) => !a.running && !["booting", "stopping"].includes(a.state);
const isResearch = (a) => a.profile === "research";
function composerState(aid) {
  return COMPOSER[aid] || (COMPOSER[aid] = { draft: "", cont: true, resume: null, newBranch: false, prTitle: "", attach: [] });
}
function selftestState(a) {
  if (!a.selftest) return null;
  return Object.values(a.selftest).every(Boolean);
}
function researchCount(a) {
  const ex = EXTRA[a.id] || { deliveries: [] };
  // answers to your own questions are already in the chat: only a coding agent's waiting findings count
  if (isResearch(a)) return ex.deliveries.filter((d) => d.for_agent && !(d.forwarded_to || []).length && !d.dismissed).length;
  const pending = (a.requests || []).filter((r) => ["pending", "failed"].includes(r.state)).length;
  return pending + ex.deliveries.filter((d) => !(d.forwarded_to || []).includes(a.id) && !d.dismissed).length;
}

VIEWS.agent = {
  mount(main) {
    const a = agentById(UI.agentId);
    if (!a) { setTimeout(() => go("new")); return; }
    AV = { id: a.id, tick: 0, logOffset: 0, logWhich: AV?.logWhich || "agent", follow: true };
    AV.head = el("div", { class: "agent-head" });
    AV.sub = el("div", { class: "agent-sub" });
    AV.tabs = el("div", { class: "tabs", role: "tablist" });
    AV.banner = el("div");
    AV.body = el("div", { class: "page" });
    AV.body.addEventListener("scroll", () => updateJump(), { passive: true });
    AV.composer = el("div", { class: "composer-wrap" });
    main.append(el("div", { class: "agent-top" }, AV.head, AV.sub), AV.tabs, AV.banner, AV.body, AV.composer);
    mountTab(a);
    pollAgent(true);
    AV.timer = setInterval(() => pollAgent(false), 1500);
  },
  unmount() { if (AV) clearInterval(AV.timer); AV = null; },
  update() {
    const a = agentById(UI.agentId);
    if (!a) { go("new"); return; }
    if (!AV || AV.id !== a.id) return;
    renderHead(a);
    renderTabs(a);
    renderBanner(a);
    updateTab(a);
  },
};

function switchTab(tab) {
  UI.tab = tab;
  try { sessionStorage.setItem("cilly-view", JSON.stringify({ view: UI.view, agentId: UI.agentId, tab })); } catch { /* fine */ }
  SIGS = {};
  const a = agentById(UI.agentId);
  mountTab(a);
  VIEWS.agent.update();
  pollAgent(true);
}
function mountTab(a) {
  AV.body.replaceChildren();
  AV.composer.replaceChildren();
  AV.composer.hidden = UI.tab !== "chat";
  if (UI.tab === "chat") { AV.chat = el("div", { class: "chat" }); AV.blocks = null; AV.body.append(AV.chat); AV.composer.append(...composer(a)); AV.newBelow = false; AV.seenLast = null; }
  else if (UI.tab === "logs") mountLogs();
  else if (UI.tab === "files") AV.pane = AV.body.appendChild(el("div", { class: "files-wrap" }));
  else AV.pane = AV.body.appendChild(el("div", { class: "page-inner " + (["sandbox", "research"].includes(UI.tab) ? "widew" : "chatw") }));
}
function updateTab(a) {
  if (UI.tab === "chat") { renderChat(a); updateComposer(a); }
  else if (UI.tab === "tasks") renderTasks(a);
  else if (UI.tab === "files") renderFiles(a);
  else if (UI.tab === "sandbox") renderSandbox(a);
  else if (UI.tab === "research") isResearch(a) ? renderFindings(a) : renderResearch(a);
}

// ---- polling: chat events, boot console, logs, deliveries/denials ----
async function pollAgent(force) {
  if (!AV) return;
  const id = AV.id, a = agentById(id);
  if (!a) return;
  AV.tick++;
  try {
    if (UI.tab === "chat") {
      const ch = CHAT[id] || (CHAT[id] = { events: [], last: 0 });
      const r = await api("GET", `/api/agents/${id}/chat?after=${ch.last}`);
      const fresh = r.events.filter((e) => e.seq > ch.last);  // two polls can overlap: never add an event twice
      if (fresh.length) { ch.events.push(...fresh); ch.last = Math.max(ch.last, r.last); if (ch.events.length > 8000) ch.events.splice(0, ch.events.length - 8000); }
      if (a.state === "booting") {
        const c = await api("GET", `/api/agents/${id}/log?which=console&offset=-4000`);
        BOOTCON[id] = bootLines(c.text);
      }
    }
    if (UI.tab === "logs") await pollLog();
    if (force || AV.tick % 3 === 0) {
      const [d, n] = await Promise.all([api("GET", `/api/agents/${id}/deliveries`), api("GET", `/api/agents/${id}/denials`)]);
      EXTRA[id] = { deliveries: d.deliveries, denials: n.denials };
    }
  } catch (e) { /* the agent may be gone; loadState handles that */ }
  if (AV && AV.id === id) VIEWS.agent.update();
}

// ---- context meter: how full the current conversation is (from newer runners; none from older ones) ----
const kTokens = (n) => n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`;
function contextOf(a) {
  const c = a.context;
  if (!c || !(c.window > 0) || !(c.used >= 0)) return null;
  const pct = Math.min(100, Math.round(c.used / c.window * 100));
  return { ...c, pct, level: pct >= 90 ? "full" : pct >= 70 ? "high" : "", text: `${kTokens(c.used)} of ${kTokens(c.window)} tokens` };
}
function contextBar(c, cls) {
  const fill = el("span", { class: "fill" });
  fill.style.width = `${c.pct}%`;
  return el("span", { class: `ctx-bar ${cls || ""} ${c.level}` }, fill);
}
function contextPill(a, now) {
  const c = contextOf(a);
  if (!c) return "";
  const pill = el("button", { class: `ctx-pill ${c.level}`, title: c.text, "aria-label": `Context ${c.pct}% used, ${c.text}` },
    el("span", { class: "word" }, "Context"), el("span", { class: "pct" }, `${c.pct}%`), contextBar(c));
  pill.addEventListener("click", () => openPop(pill, contextPop(now()), { width: 300 }));
  return pill;
}
function contextPop(a) {
  const c = contextOf(a) || { pct: 0, used: 0, window: 0, level: "", text: "" };
  const note = isStopped(a) ? "From the last task. The next message continues this conversation unless you start a new chat."
    : c.pct >= 90 ? "Nearly full. Claude compacts on its own at the limit, which can drop details. A new chat starts fresh."
    : c.pct >= 70 ? "Getting full. Claude compacts on its own at the limit." : "Plenty of room.";
  const fresh = el("button", { class: "btn wide" }, "Start a new chat");
  fresh.addEventListener("click", () => {
    Object.assign(composerState(a.id), { cont: false, resume: null });
    SIGS.composer = null;
    closePop();
    VIEWS.agent.update();
    AV?.c?.ta.focus();
  });
  return el("div", { class: "ctx-pop" },
    el("div", { class: "row nowrap" }, el("span", { class: "grow title" }, "Context"), el("span", { class: `pct ${c.level}` }, `${c.pct}%`)),
    contextBar(c, "wide"),
    el("div", { class: "small" }, `${kTokens(c.used)} of ${kTokens(c.window)} tokens used. ${kTokens(Math.max(0, c.window - c.used))} free.`),
    el("div", { class: `small ${isStopped(a) ? "muted" : c.level === "full" ? "err-text" : "muted"}` }, note),
    fresh);
}

// ---- header ----
// What an agent waits on, from the inbox: its first item, and a few words for the header
const WAIT_WORDS = { questions: (n) => (n > 1 ? `${n} questions` : "a question"),
  research: (n) => (n > 1 ? `${n} requests` : "a research request"), network: (n) => (n > 1 ? `${n} blocked sites` : "a blocked site"),
  findings: () => "findings to review", note: (n) => (n > 1 ? `${n} project notes` : "a project note") };
function waitingOn(a) {
  const items = INBOX.items.filter((it) => it.agent === a.id), n = items.length;
  if (!n) return [null, ""];
  const say = WAIT_WORDS[items[0].kind] || (() => "an item"), same = items.every((it) => it.kind === items[0].kind);
  return [items[0], same ? say(n) : `${say(1)} and ${n - 1} more`];
}
// The state pill leads to what it's about: the item waiting on you, the logs of a failure, or else the tasks
function pillAction(a) {
  const k = statusKey(a), [first] = waitingOn(a);
  if (k === "waiting") return ["Open it", () => (!first ? openInbox() : first.kind === "questions" ? answerAsk(first) : review(first.key))];
  if (k === "failed") return ["Open the logs", () => switchTab("logs")];
  return ["Open Tasks", () => switchTab("tasks")];
}
function statePill(a, now) {
  const k = statusKey(a), words = waitingOn(a)[1], full = k === "waiting" && words ? `Waiting for you: ${words}` : statusText(a);
  const [title] = pillAction(a);
  return el("button", { class: `pill state-pill st-${k}`, type: "button", title: `${full}. ${title}`, onclick: () => pillAction(now())[1]() },
    stateIcon(k), el("span", { class: "full-words" }, full), el("span", { class: "short-words" }, statusShort(a)));
}
function prLink(url, n) {  // "Open PR #7", or "PR #7" in a narrow header
  const link = ghLink(url, `PR #${n}`);
  if (link.nodeType === 1) { link.prepend(el("span", { class: "full-words" }, "Open ")); link.title = `Open PR #${n}`; }
  return link;
}
function renderHead(a) {
  const ok = selftestState(a);
  const blocked = blockedOf(a).length;
  if (editingIn(AV.head) || !changed("head", [a.id, a.name, a.state, statusText(a), statusShort(a), waitingOn(a)[1], ownLine(a), a.repo, a.branch, a.pr, a.profile,
    ok, blocked, a.running, isPhone(), a.context?.used, a.context?.window])) return;
  const where = isResearch(a) ? "open web, no repository" : a.repo ? `${a.repo} · ${a.branch || "?"}` : "no repository, scratch folder";
  const prn = (a.pr || "").match(/\/pull\/(\d+)$/);
  const badge = el("button", { class: "sandbox-badge" + (ok === false ? " bad" : ""), "aria-haspopup": "dialog", title: "Sandbox" },
    el("span", { class: "dot " + (ok === false ? "s-failed" : ok ? "s-ready" : "s-stopped") }),
    ok === false ? (isPhone() ? "Check failed" : "Network check failed") : isResearch(a) ? "Open web" : "Isolated",
    blocked ? el("span", { class: "count-badge err", title: plural(blocked, "blocked request") }, String(blocked)) : "",
    isPhone() ? "" : el("span", { class: "dim" }, foldMark(true)));
  const now = () => agentById(a.id) || a;  // the header isn't redrawn for every change: use what's current
  const branch = !isResearch(a) && a.repo && a.branch;
  badge.addEventListener("click", () => openPop(badge, sandboxPop(now()), { width: 320 }));
  const stopped = isStopped(a);
  const ss = el("button", { class: "btn small head-ss " + (stopped ? "start" : "stop") }, stopped ? "Start" : "Stop");
  ss.disabled = a.state === "stopping";
  ss.addEventListener("click", () => stopped ? openPop(ss, startPop(now(), ss), { width: 420 }) : stopAgent(now()));
  const more = el("button", { class: "icon-btn", title: "More", "aria-label": "More actions" }, "···");
  more.addEventListener("click", () => openPop(more, agentMenu(now()), { width: 270 }));
  if (isPhone()) {  // one row: back, name with state and repo, badge, ···  (Start/Stop lives in ···)
    AV.name = el("span", { class: "name ellipsis", title: a.id }, a.name);
    AV.head.replaceChildren(
      el("button", { class: "icon-btn m-back", "aria-label": "Back to agents", onclick: () => go("agents") }, "←"),
      el("div", { class: "m-title grow" }, AV.name,
        el("span", { class: "m-sub" }, phoneState(a, now),
          el("span", { class: "muted" }, a.repo ? `· ${a.repo}` : isResearch(a) ? "· open web" : ""))),
      badge, more);
    AV.sub.replaceChildren(...phoneMeta(a, now, prn, branch));
    return;
  }
  AV.name = el("button", { class: "name", type: "button", title: `Click to rename. ID ${a.id}`, onclick: () => renameInHeader(now()) }, a.name);
  AV.head.replaceChildren(
    AV.name,
    statePill(a, now),
    a.status?.stops_at ? el("button", { class: "btn small ghost", title: "Start the idle clock again", onclick: () => keepRunning(now()) }, "Restart the clock") : "",
    prn ? prLink(a.pr, prn[1]) : "",
    el("span", { class: "grow" }), contextPill(a, now), badge, ss, more);
  // repository and branch only: the agent's own line is under its name in the sidebar
  AV.sub.replaceChildren(branch ? el("button", { class: "where copy ellipsis", type: "button", title: "Click to copy the branch", onclick: () => copyBranch(branch) }, where)
    : el("span", { class: "where ellipsis", title: where }, where));
}
// A phone's state words lead to the same place as the desktop pill
function phoneState(a, now) {
  const [title] = pillAction(a);
  return el("button", { class: "m-state", type: "button", "aria-label": `${statusText(a)}. ${title}`, onclick: () => pillAction(now())[1]() },
    stateIcon(statusKey(a)), stateWords(a, true));
}
// The line under a phone's header: what the desktop shows as the branch, the PR link and a context warning
function phoneMeta(a, now, prn, branch) {
  const c = contextOf(a);
  return [branch ? el("button", { class: "where copy ellipsis", type: "button", "aria-label": `Copy the branch ${branch}`, onclick: () => copyBranch(branch) }, branch) : "",
    prn ? el("span", { class: "where-pr" }, ghLink(a.pr, `PR #${prn[1]}`)) : "",
    c?.level ? el("button", { class: `tag ctx-tag ${c.level === "full" ? "err" : "warn"}`, type: "button", "aria-label": `Context ${c.pct}% used. ${c.text}`,
      onclick: (e) => openPop(e.currentTarget, contextPop(now()), { width: 300 }) }, `Context ${c.pct}%`) : ""];
}
function renameInHeader(a) {
  if (!AV?.name?.isConnected) return;
  const field = editName(AV.name, a.name, (name) => renameAgent(a, name), { cls: "head-rename" });
  if (isPhone()) return;  // there it takes the name's whole line
  const fit = () => { field.style.width = Math.max(8, field.value.length + 2) + "ch"; };
  field.addEventListener("input", fit);
  fit();
}
async function copyBranch(branch) {
  if (await copyText(branch)) toast(`Copied ${branch}`);
  else toast(`Couldn't copy. The branch is ${branch}`, true);
}
async function stopAgent(a) {
  const what = a.mode === "persistent" ? "Its disk is kept." : a.mode === "memory" ? "Its memory is exported first, then its VM disk is deleted." : "Its VM disk is deleted.";
  if (!(await confirmModal(`Stop ${a.name}?`, what, "Stop"))) return;
  agentAction(a, "stop");
}
async function agentAction(a, action, body) {
  try { await api("POST", `/api/agents/${a.id}/${action}`, body || {}); }
  catch (e) { toast(e.message, true); }
  if (action === "delete") { delete CHAT[a.id]; delete COMPOSER[a.id]; await loadState(); go(STATE.agents_list.length ? "agent" : "new", { agentId: STATE.agents_list[0]?.id, tab: "chat" }); return; }
  loadState();
}

// ---- sandbox: a summary popover (a bottom sheet on phones) and the Sandbox tab ----
// a research agent's list only has refusals no allowing can fix (its own network, Never reach, a port)
function blockedOf(a) { return (EXTRA[a.id] || {}).denials || []; }
const portsText = (p) => !p.length ? "no port" : p.length > 1 ? `ports ${p.slice(0, -1).join(", ")} and ${p[p.length - 1]}` : `port ${p[0]}`;
function openWebLine() {  // what a research agent really reaches, from the ports cilly lets through
  const p = STATE.open_web_ports || [443];
  return !p.length ? "No public site, because egress.ports has neither 443 nor 80."
    : `Any public site on ${portsText(p)}.`;
}
function selftestList(a) {  // one list for the header and the rows, failures first
  const checks = Object.entries(a.selftest || {}).map(([name, ok]) => ({ name, ok: !!ok }));
  return checks.filter((c) => !c.ok).concat(checks.filter((c) => c.ok));
}
function secretNames(a) {
  const lib = STATE.mcp_servers || {};
  const names = new Set(isResearch(a) ? STATE.secret_names.research : STATE.secret_names.agents[a.agent] || []);
  if (!isResearch(a) && a.repo) { names.add("GH_TOKEN"); names.add("GITHUB_TOKEN"); }
  for (const n of a.mcp || []) for (const v of lib[n]?.secrets || []) names.add(v);
  return [...names].sort();
}
function extraHosts(a) {  // [host, mcp server name or null, project or null]: where each one comes from
  const lib = STATE.mcp_servers || {}, p = projectById(a.project);
  return [...(a.mcp || []).flatMap((n) => (lib[n]?.hosts || []).map((h) => [h, n, null])), ...(p?.allow || []).map((h) => [h, null, p]),
    ...(a.egress_extra || []).map((h) => [h, null, null])];
}
// The global allowlist's sites, each with a box to leave it out for one agent, and what it's for when cilly knows
const SITE_USE = { "github.com": "Cloning and pushing its repository", "api.github.com": "Pull requests and the gh command",
  "codeload.github.com": "Downloading archives from GitHub", "objects.githubusercontent.com": "Release files and Git LFS from GitHub",
  "deb.debian.org": "Installing Debian packages with apt", "security.debian.org": "Debian security updates with apt",
  "registry.npmjs.org": "Installing npm packages", "pypi.org": "Installing Python packages with pip", "files.pythonhosted.org": "Downloading Python packages with pip" };
const REPO_SITES = ["github.com", "api.github.com"];
function globalSites(skip, onChange, repo) {
  const all = STATE.setup?.allow || [];
  if (!all.length) return [el("div", { class: "muted small" }, "The global allowlist in Settings is empty.")];
  const rows = all.map((h) => el("label", { class: "list-line site-pick" },
    el("input", { type: "checkbox", checked: !skip.includes(h), "aria-label": `Allow ${h}`,
      onchange: (e) => onChange(e.target.checked ? skip.filter((x) => x !== h) : [...skip, h]) }),
    el("div", { class: "grow stack0" }, el("code", { class: "ellipsis" }, h), SITE_USE[h] ? el("span", { class: "muted small" }, SITE_USE[h]) : "")));
  const needed = repo ? REPO_SITES.filter((h) => all.includes(h) && skip.includes(h)) : [];
  return [el("div", { class: "list-card" }, rows),
    needed.length ? el("div", { class: "note-box warn" }, `It works on ${repo}, so it needs ${needed.join(" and ")} to clone, push and open its pull request.`) : ""];
}
const MODE_SHORT = { ephemeral: "Ephemeral", persistent: "Persistent", memory: "Ephemeral + memory" };
const labelOf = (list, v) => list.find(([x]) => x === v)?.[1] || v;
const modelLabel = (a) => [a.model_server ? `${a.model} · ${a.model_server}` : a.model ? labelOf(MODELS, a.model) : "Default model",
  a.effort ? labelOf(EFFORTS, a.effort) : ""].filter(Boolean).join(" · ");
const policyLine = (a) => `${STATE.permission_mode} · v${a.policy_version || STATE.policy_version}`;
function openSandboxTab() { closePop(); if (UI.tab !== "sandbox") switchTab("sandbox"); }

function sandboxPop(a) {
  const checks = selftestList(a), failed = checks.filter((c) => !c.ok).length, blocked = blockedOf(a);
  const row = (label, value, cls) => el("button", { class: "sum-row", onclick: openSandboxTab },
    el("span", { class: "k" }, label), el("span", { class: "v " + (cls || "") }, value), el("span", { class: "chev" }, "›"));
  const rows = [
    row("Self-test", !checks.length ? "not run yet" : failed ? `${failed} of ${checks.length} failed` : `All ${checks.length} passed`,
      !checks.length ? "muted" : failed ? "err" : "ok"),
    row("Network", isResearch(a) ? "Open web" : `${extraHosts(a).length} extra host${extraHosts(a).length === 1 ? "" : "s"}`),
    blocked.length ? row("Blocked", `${blocked.length} waiting`, "err-text") : "",
    row("Secrets", `${secretNames(a).length} exposed`),
    row("Lifecycle", `${MODE_SHORT[a.mode]}, disk ${a.mode === "persistent" ? "kept" : "deleted"}`)];
  const head = el("div", { class: "row nowrap sum-head" }, el("span", { class: "grow title" }, "Sandbox"), el("span", { class: "muted small" }, policyLine(a)));
  if (!isPhone()) {
    return el("div", { class: "sum-pop" }, head, el("div", { class: "sum-rows" }, rows), el("div", { class: "sep" }),
      el("button", { class: "link sum-foot", onclick: openSandboxTab }, "Open sandbox details"));
  }
  // phones: blocked hosts stay right here, so you can allow one without leaving the chat
  return el("div", { class: "sum-pop" }, head, el("div", { class: "sum-rows card-group" }, rows),
    blocked.length ? [el("div", { class: "muted small" }, `Blocked (${blocked.length})`), ...blocked.slice(0, 5).map((d) => blockedRow(a, d, true)),
      blocked.length > 5 ? el("div", { class: "muted small" }, `${blocked.length - 5} more on the Sandbox tab`) : ""] : "",
    el("button", { class: "btn wide", onclick: openSandboxTab }, "Open sandbox details"));
}
function blockedRow(a, d, sheet) {
  if (d.fixable === false) {  // the address or the port is the problem: allowing the host wouldn't help
    return el("div", { class: "blocked-row" + (sheet ? " sheet" : "") },
      el("div", { class: "grow stack tight" }, el("code", { class: "err-text" }, `${d.host}:${d.port}`), el("span", { class: "muted small" }, d.reason || "")),
      el("span", { class: "muted small nowrap" }, `${d.count}×, last at ${fmtTime(d.last)}`),
      el("div", { class: "blocked-acts" }, el("button", { class: "btn small", onclick: () => dismissDenial({ ...d, agent: a.id }) }, "Dismiss")));
  }
  if (!canAllow(d)) {
    return el("div", { class: "blocked-row" + (sheet ? " sheet" : "") },
      el("div", { class: "grow stack tight" }, el("code", { class: "err-text" }, `${d.host}:${d.port}`), el("span", { class: "muted small" }, PORTS_NOTE)),
      el("span", { class: "muted small nowrap" }, `${d.count}×, last at ${fmtTime(d.last)}`),
      el("div", { class: "blocked-acts" }, el("button", { class: "btn small", onclick: () => dismissDenial({ ...d, agent: a.id }) }, "Dismiss")));
  }
  const inp = el("input", { class: "host-edit mono", value: siteFor(d), title: "Edit to a wildcard like *.example.com", spellcheck: false, "aria-label": "Site to allow" });
  const everywhere = async () => {
    if (await confirmModal(`Allow ${inp.value} for all coding agents?`, "It's added to the global egress allowlist.", "Allow everywhere"))
      allowAction(a, "allow", { host: inp.value, scope: "global" });
  };
  return el("div", { class: "blocked-row" + (sheet ? " sheet" : "") }, inp,
    el("span", { class: "muted small nowrap" }, `${d.count}×, last at ${fmtTime(d.last)}`),
    el("div", { class: "blocked-acts" },
      el("button", { class: "btn small primary", onclick: () => allowAction(a, "allow", { host: inp.value, scope: "agent" }) }, `Allow for ${a.name}`),
      isOwner() ? el("button", { class: "btn small", onclick: everywhere }, "Everywhere…") : ""));
}
function section(title, sub, ...body) {
  return el("section", { class: "sb-section" }, el("div", { class: "sec-head" }, el("span", { class: "sec-title" }, title), sub ? el("span", { class: "sec-sub" }, sub) : ""), ...body);
}
function renderSandbox(a) {
  const blocked = blockedOf(a), checks = selftestList(a);
  resetPicks(a);
  if (!changed("sandbox", [a.id, blocked, a.selftest, a.egress_extra, a.egress_skip, STATE.setup?.allow, a.mcp, projectById(a.project)?.allow, a.mode, a.has_disk, a.memory, a.image, a.base_image,
    a.cpus, a.memory_mb, isStopped(a), a.running, STATE.vm_defaults, AV.res, AV.stOpen, a.bash_limit_minutes, a.bg_limit_minutes, a.task_limit_minutes, a.limits_ignored, AV.lim,
    a.inject_source, a.repo, a.project, a.claude_md, isOwner(), STATE.mcp_servers, STATE.egress_ports, STATE.images, STATE.permission_mode, a.policy_version, STATE.policy_version, STATE.base_image, STATE.research_base_image])) {
    memUsed(a);  // changes every few seconds: updated in place, so the tab never redraws under your hands
    return;
  }
  // the text fields are kept across redraws, but moving them drops the focus: put it back where you were typing
  const f = document.activeElement, typing = [AV.instr?.ta, AV.hostIn?.input].includes(f) ? [f, f.selectionStart, f.selectionEnd, f.scrollTop] : null;
  const main = [el("div", { class: "sb-intro" }, isResearch(a)
    ? `${openWebLine()} This PC, your local network, VPNs and addresses in Never reach stay blocked.`
    : `Only allowlisted sites are reachable, through the egress proxy. A site gets ${portsText(STATE.egress_ports || [443])} unless it names its own, like example.com:8080.`)];
  if (blocked.length) main.push(section("Blocked requests", blocked.some((d) => d.fixable !== false) ? "Edit a host into a wildcard before allowing."
    : "Allowing the host wouldn't help with these.", ...paged("blocked:" + a.id, blocked, (d) => blockedRow(a, d), { step: 10 })));
  main.push(selftestSection(a, checks));
  main.push(section("Instructions for this agent", a.project && !isResearch(a) ? "Added to CLAUDE.md after your Claude policy and the project's instructions."
    : "Added to CLAUDE.md after your Claude policy.", instructionsCard(a)));
  if (!isResearch(a)) {
    const hosts = extraHosts(a);
    main.push(section("Extra hosts", "Added to the global allowlist for this agent. Applies live.", el("div", { class: "list-card" },
      hosts.length ? hosts.map(([h, mcp, p]) => el("div", { class: "list-line" },
        el("code", { class: "grow ellipsis" }, mcp ? `${h} (MCP: ${mcp})` : p ? `${h} (project: ${p.name})` : h),
        mcp ? el("button", { class: "link quiet", onclick: () => setMcp(a, (a.mcp || []).filter((x) => x !== mcp)) }, "Turn off MCP")
          : p ? el("button", { class: "link quiet", onclick: () => go("project", { projectId: p.id }) }, "Open project")
          : el("button", { class: "link quiet danger-hover", onclick: () => allowAction(a, "disallow", { host: h }) }, "Remove")))
        : el("div", { class: "list-line muted" }, "None. This agent uses only the global allowlist."),
      addHostLine(a))));
  }
  if (!isResearch(a)) {
    const skip = a.egress_skip || [];
    main.push(section("Global allowlist", skip.length ? `${skip.length} left out for this agent. Applies live.` : "From Settings. Uncheck a site to leave it out for this agent. Applies live.",
      ...globalSites(skip, async (next) => {
        const back = skip.find((h) => !next.includes(h)), out = next.find((h) => !skip.includes(h));
        try {
          await api("POST", `/api/agents/${a.id}/global-sites`, { skip: next });
          toast(out ? `${out} left out for ${a.name}` : `${back} allowed again for ${a.name}`);
        } catch (e) { toast(e.message, true); }
        await loadState(); pollAgent(true);
      }, a.repo)));
  }
  main.push(section("MCP servers", "Applied live. Remote hosts join this agent's allowlist.", el("div", { class: "card stack" }, ...mcpControls(a))));
  if (!isResearch(a)) main.push(section("Plugins", "Installed in its VM before the next task, through its own network rules.", el("div", { class: "card stack" }, ...pluginControls(a))),
    section("Skills", "Your own skills, added to ~/.claude/skills in its VM before the next task.", el("div", { class: "card stack" }, ...skillControls(a))));
  AV.pane.replaceChildren(el("div", { class: "two-cols" }, el("div", { class: "col-main" }, main), el("div", { class: "col-side" }, thisVmCard(a))));
  if (typing) { typing[0].focus({ preventScroll: true }); typing[0].setSelectionRange(typing[1], typing[2]); typing[0].scrollTop = typing[3]; }
}
function selftestSection(a, checks) {
  const failed = checks.filter((c) => !c.ok).length;
  if (!checks.length) return section("Self-test", "Runs at every boot.", el("div", { class: "muted" }, "Not started yet."));
  const open = AV.stOpen ?? failed > 0, shown = open ? checks : checks.slice(0, Math.max(4, failed));  // failures come first
  const toggle = checks.length > 4 ? el("button", { class: "link", onclick: () => { AV.stOpen = !open; renderSandbox(agentById(a.id) || a); } },
    open ? "Show fewer" : `Show all ${checks.length}`) : "";
  return el("section", { class: "sb-section" },
    el("div", { class: "sec-head" }, el("span", { class: "sec-title" }, "Self-test"),
      el("span", { class: "sec-sub " + (failed ? "err" : "ok") }, `${failed ? `${failed} of ${checks.length} failed` : `All ${checks.length} passed`}, last boot`),
      el("span", { class: "grow" }), toggle),
    el("div", { class: "check-grid" }, shown.map((c) => el("div", { class: "check" + (c.ok ? "" : " err") }, el("span", { class: "mk" }, c.ok ? "✓" : "✗"), el("span", {}, c.name)))));
}
// Kept across redraws, like the instructions, so what you type survives the tab updating
function addHostLine(a) {
  if (AV.hostIn?.aid !== a.id) {
    const input = el("input", { class: "host-add mono", placeholder: "Add a host, e.g. *.example.com", spellcheck: false, "aria-label": "Add a host for this agent" });
    const add = async () => {
      const host = input.value.trim();
      if (!host) return;
      if (!mayAllow(host)) { toast(PORTS_NOTE, true); return; }
      input.value = "";
      await allowAction(a, "allow", { host, scope: "agent" });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    AV.hostIn = { aid: a.id, input, node: el("div", { class: "list-line add-line" }, input, el("button", { class: "btn small", onclick: add }, "Add")) };
  }
  return AV.hostIn.node;
}
function vmRow(label, ...body) {
  return el("div", { class: "vm-row" }, typeof label === "string" ? el("span", { class: "vm-k" }, label) : label, ...body);
}
function thisVmCard(a) {
  const disk = a.mode === "persistent" ? (a.has_disk ? "Kept across restarts" : "None yet (the next start is clean)") : "Deleted when it stops";
  const rows = [
    vmRow("Lifecycle", el("span", {}, MODE_LABEL[a.mode]), el("span", { class: "vm-note" },
      MODE_HELP[a.mode] + (!isResearch(a) && !a.repo ? " There's no repository, so tasks run in a scratch folder with no git, GitHub token or PR." : ""))),
    vmRow("Disk", el("span", {}, disk))];
  if (a.mode === "memory") {
    const m = a.memory;
    rows.push(vmRow("Memory", el("span", {}, !m ? "Nothing saved yet. It's saved when the agent stops." : m.error ? `${m.error} (${m.at}). The previous memory was kept.`
      : m.imported ? `Saved ${plural(m.files, "file")}, ${fmtBytes(m.bytes)} at ${m.at}` + (m.left_out ? `. ${leftOut(m.left_out)}` : "") : `Nothing exported at ${m.at}`),
      m?.skipped ? el("span", { class: "vm-note" }, `${m.skipped} saved path${m.skipped === 1 ? " was" : "s were"} too long or too deep, so ${m.skipped === 1 ? "it" : "they"} didn't go into the VM at its last start.`) : ""));
  }
  rows.push(resourcesRow(a));
  rows.push(limitRow(a, "bash_limit_minutes", "Bash command limit", 10, [5, 15, 30, 60, 120, 240],
    "Claude Code gives a command 2 min unless it asks for more, up to this limit."));
  rows.push(limitRow(a, "bg_limit_minutes", "Background work limit", 10, [0, 30, 60, 120, 240],
    "How long background commands and subagents may still run after Claude's last reply. Then Claude Code stops them."));
  rows.push(limitRow(a, "task_limit_minutes", "Task time limit", STATE.vm_defaults.task_timeout_minutes, [30, 60, 120, 240, 480, 720, 1440],
    "A task that runs longer is stopped."));
  const using = (STATE.images || []).find((i) => i.name === (a.mode === "persistent" && a.has_disk && a.base_image ? a.base_image
    : a.image || (isResearch(a) ? STATE.research_base_image : STATE.base_image)));
  rows.push(vmRow("VM image", imageSelect(a.image, async (v) => {
    try { await api("POST", `/api/agents/${a.id}/image`, { image: v || null }); toast("Image saved"); } catch (e) { toast(e.message, true); }
    await loadState();
  }, a.profile), el("span", { class: "vm-note" }, (using ? toolchainText(using).replace(/^./, (c) => c.toUpperCase()) + ". " : "None built yet. ")
    + (a.mode === "persistent" && a.has_disk ? `This disk was made from ${imageNamed(a.base_image)}. To switch and keep its files, use Move to new image in the ··· menu.`
      : "Used from its next start.")
    + (a.moved?.to && a.moved.to === a.base_image ? ` Moved from ${imageNamed(a.moved.from)} on ${String(a.moved.at || "").slice(0, 10)}.` : ""))));
  rows.push(hostFolderRow(a));
  const route = a.route || inheritedRoute(a.project);
  rows.push(vmRow("Route", routeSelect(a.route, async (v) => {
    try {
      await api("POST", `/api/agents/${a.id}/route`, { route: v });
      toast(v ? `Route: ${routeLabel(v)}. It applies at once.` : "It follows its project or Settings again.");
    } catch (e) { toast(e.message, true); }
    await loadState();
  }, `Same as ${a.project ? "its project" : "Settings"} (${routeLabel(inheritedRoute(a.project))})`),
  el("span", { class: "vm-note" }, routeNote(route))));
  const names = secretNames(a);
  rows.push(vmRow("Secrets", names.length ? el("div", { class: "row tight-row" }, names.map((n) => el("code", { class: "chip-code" }, n))) : el("span", { class: "muted" }, "None"),
    el("span", { class: "vm-note" }, "Sent to the VM with each task.")));
  rows.push(vmRow("Policy", el("div", { class: "row nowrap" }, el("span", { class: "grow" }, policyLine(a)),
    isOwner() ? el("button", { class: "link", onclick: () => go("policy") }, "Edit") : el("span", { class: "muted small" }, "Edited on your PC"))));
  return el("div", { class: "vm-card" }, el("div", { class: "vm-head" }, el("span", { class: "grow sec-title" }, "This VM"), el("span", { class: "mono muted small" }, a.id)), rows);
}
const gib = (mb) => `${+(mb / 1024).toFixed(mb % 1024 ? 1 : 0)} GiB`;
const memText = (mb) => (mb >= 1024 ? gib(mb) : `${mb} MiB`);
function memUsed(a) {
  if (AV.memUsed) AV.memUsed.textContent = a.running && a.mem_used_mb ? `Using ${memText(a.mem_used_mb)} now` : "";
}
function resetPicks(a) {  // picks in This VM wait here until saved: resources only while stopped (and from the PC), limits any time
  if (AV.res?.aid !== a.id || !isOwner() || !isStopped(a)) AV.res = { aid: a.id, cpus: a.cpus, mem: a.memory_mb };
  if (AV.lim?.aid !== a.id) AV.lim = { aid: a.id };
}
function resourcesRow(a) {
  const lim = STATE.vm_defaults, may = isOwner() && isStopped(a);
  const r = AV.res, dirty = r.cpus !== a.cpus || r.mem !== a.memory_mb;
  const opts = (list, cur) => [...new Set([...list, cur])].sort((x, y) => x - y);
  const cpus = el("select", { class: "select", "aria-label": "vCPUs", disabled: !may },
    opts([1, 2, 3, 4, 6, 8, 12, 16, 24, 32].filter((n) => n <= lim.max_cpus), r.cpus).map((n) => el("option", { value: n }, `${n} vCPU`)));
  const mem = el("select", { class: "select", "aria-label": "Memory", disabled: !may },
    opts([1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64].map((g) => g * 1024).filter((m) => m <= lim.max_memory_mb), r.mem).map((m) => el("option", { value: m }, gib(m))));
  cpus.value = r.cpus; mem.value = r.mem;
  const pick = () => { Object.assign(AV.res, { cpus: +cpus.value, mem: +mem.value }); renderSandbox(agentById(a.id) || a); };
  cpus.addEventListener("change", pick); mem.addEventListener("change", pick);
  const save = async () => {
    try {
      await api("POST", `/api/agents/${a.id}/resources`, { cpus: r.cpus, memory_mb: r.mem });
      toast(`Saved. ${a.name} uses ${r.cpus} vCPU and ${gib(r.mem)} from its next start.`);
    } catch (e) { toast(e.message, true); return; }
    await loadState();
  };
  AV.memUsed = el("span", { class: "vm-note" });
  memUsed(a);
  return vmRow(el("div", { class: "row nowrap" }, el("span", { class: "vm-k grow" }, "Resources", infoTip("Idle VMs give unused memory back to your PC.")), AV.memUsed),
    el("div", { class: "row nowrap" }, cpus, mem, may && dirty ? el("button", { class: "btn small primary", onclick: save }, "Save") : ""),
    el("span", { class: "vm-note" }, !isOwner() ? "Changed on your PC." : !isStopped(a) ? "Stop the agent to change these."
      : `Applied at its next start. Limits: ${lim.max_cpus} vCPU, ${gib(lim.max_memory_mb)}.`));
}
const minText = (m) => (m === 0 ? "Until the task ends" : m % 60 ? `${m} min` : `${m / 60} h`);  // 0: no limit of its own
// One of the agent's limits, in minutes (null: the default). A pick waits in AV.lim until you save it.
function limitRow(a, key, label, def, list, help) {  // a[key] is what the server uses: an out-of-range saved value shows as the default
  const cur = a[key] ?? null, pick = key in AV.lim ? AV.lim[key] : cur;
  const sel = el("select", { class: "select", "aria-label": label },
    el("option", { value: "" }, `Default (${minText(def)})`),
    [...new Set([...list.filter((m) => m !== def), cur].filter((m) => m != null))].sort((x, y) => x - y).map((m) => el("option", { value: m }, minText(m))));
  sel.value = pick ?? "";
  sel.addEventListener("change", () => { AV.lim[key] = sel.value === "" ? null : +sel.value; renderSandbox(agentById(a.id) || a); });
  const save = async () => {
    try { await api("POST", `/api/agents/${a.id}/limits`, { [key]: pick }); toast("Saved. It applies from the next message you send."); }
    catch (e) { toast(e.message, true); return; }
    delete AV.lim[key];
    await loadState();
  };
  return vmRow(el("span", { class: "vm-k" }, label, infoTip(help + " Applies from the next message you send.")),
    el("div", { class: "row nowrap" }, sel, pick !== cur ? el("button", { class: "btn small primary", onclick: save }, "Save") : ""),
    (a.limits_ignored || []).includes(key) ? el("span", { class: "vm-note" }, "The saved value is out of range, so the default applies.") : "");
}
function hostFolderRow(a) {
  if (isResearch(a)) return vmRow("Host folder", el("span", { class: "muted" }, "None"), el("span", { class: "vm-note" }, "Research agents never get host folders."));
  const src = a.inject_source, owner = isOwner() && !isPhone();  // the folder browser needs your PC
  if (!src) {
    return vmRow("Host folder", el("div", { class: "row nowrap" }, el("span", { class: "muted grow" }, "None"),
      owner ? el("button", { class: "btn small", onclick: () => hostFolderDialog(a) }, "Add a folder…") : ""),
      el("span", { class: "vm-note" }, owner ? "Read-only at /inject. Takes effect at the next start." : "Host folders are given from the panel on your PC."));
  }
  const now = a.inject;  // what the running VM got at its start
  const pending = a.running && (!now?.root || now.root !== src.root || now.mode !== src.mode);
  const remove = async () => {
    try { await api("POST", `/api/agents/${a.id}/organize`, { forget_inject: true }); toast(a.running ? "Folder removed. Gone from /inject at the next start." : "Folder removed"); }
    catch (e) { toast(e.message, true); }
    loadState();
  };
  return vmRow("Host folder", el("code", { class: "ellipsis vm-path", title: src.root }, src.root),
    el("span", { class: "vm-note" }, [src.mode === "share" ? "Live share" : "One-time copy",
      src.files != null ? `${src.files} file${src.files === 1 ? "" : "s"}, ${fmtBytes(src.bytes)}` : "", "read-only at /inject"].filter(Boolean).join(", ")
      + (src.to_scratch ? ", with a working copy in ~/scratch." : ".")),
    pending ? el("span", { class: "vm-note warn" }, "Takes effect at the next start") : "",
    el("div", { class: "row vm-links" },
      owner ? el("button", { class: "link", onclick: () => hostFolderDialog(a) }, "Change") : "",
      owner ? el("button", { class: "link quiet", onclick: () => hostFolderDialog(a, true) }, "Rescan") : "",
      el("button", { class: "link quiet danger-hover", onclick: remove }, "Remove")));
}
// Add, change or rescan the folder an agent gets at every start. It's saved now and injected
// when the agent next starts; the panel scans it again then, as for Start.
function hostFolderDialog(a, rescan) {
  const src = a.inject_source;
  const foot = el("span", { class: "grow muted small pretty" });
  const save = el("button", { class: "btn primary", disabled: true }, src ? "Save folder" : "Add folder");
  const update = () => {
    const now = agentById(a.id) || a;
    save.disabled = !inj.ready();
    foot.textContent = !inj.scanned() ? "Scan first: it lists what goes in and flags anything that looks like a secret."
      : now.running ? `${now.name} is running. It gets the folder at its next start.` : `Injected when ${now.name} next starts.`;
  };
  const inj = makeInjectWidget(() => !a.repo, { onChange: () => update(), mode: src?.mode || "copy", toScratch: src ? !!src.to_scratch : true });
  const close = () => { back.remove(); document.removeEventListener("keydown", esc, true); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  save.addEventListener("click", async () => {
    if (!inj.ready()) return;
    save.disabled = true;
    try {
      const r = await api("POST", `/api/agents/${a.id}/inject`, inj.spec());
      toast(r.running ? `Folder saved. ${r.name} gets it at its next start.` : "Folder saved. Injected at the next start.");
      close();
    } catch (e) { toast(e.message, true); save.disabled = false; }
    loadState();
  });
  const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } },
    el("div", { class: "modal xwide", role: "dialog", "aria-modal": "true", "aria-label": `Host folder for ${a.name}` },
      el("h3", {}, src ? `Host folder for ${a.name}` : `Add a host folder to ${a.name}`),
      el("div", { class: "muted pretty" }, "It appears read-only at /inject inside the VM. The agent can read it but never change your copy."),
      inj.node,
      el("div", { class: "row nowrap modal-sep" }, foot, el("button", { class: "btn ghost", onclick: close }, "Cancel"), save)));
  document.addEventListener("keydown", esc, true);
  document.body.append(back);
  update();
  if (src) inj.scan(src.root);
  if (!rescan) back.querySelector("input")?.focus();
}
// Kept across redraws, so a draft survives the Sandbox tab updating under it
function instructionsCard(a) {
  if (AV.instr?.aid !== a.id) {
    const ta = el("textarea", { class: "editor prose", rows: 4, spellcheck: true, "aria-label": "Instructions for this agent",
      placeholder: "What only this agent should know, such as its part of the work or how it should answer." });
    const note = el("span", { class: "muted small grow" });
    const dirty = () => {
      save.disabled = ta.value === (agentById(a.id)?.claude_md || "");
      note.textContent = save.disabled ? "Used from its next task." : "Unsaved changes";
    };
    const save = el("button", { class: "btn small primary", disabled: true, onclick: async () => {
      save.disabled = true;
      try { await api("POST", `/api/agents/${a.id}/claude-md`, { text: ta.value }); toast("Saved. The agent gets it at its next task."); await loadState(); }
      catch (e) { toast(e.message, true); save.disabled = false; }
      dirty();
    } }, "Save");
    ta.addEventListener("input", dirty);
    AV.instr = { aid: a.id, ta, save, note, node: el("div", { class: "stack tight" }, ta, el("div", { class: "row" }, note, save)) };
  }
  const { ta, save, note } = AV.instr, owner = isOwner();
  if (save.disabled) ta.value = a.claude_md || "";
  ta.readOnly = !owner; save.hidden = !owner;
  note.textContent = !owner ? "Edited on your PC." : save.disabled ? "Used from its next task." : "Unsaved changes";
  return AV.instr.node;
}
// This agent's own sites, on top of the global allowlist: paste a list (URLs, bullets and
// commas are fine), and the panel keeps the host names. Applies live.
function egressEditor(a) {
  const ta = el("textarea", { class: "editor", rows: 14, spellcheck: false, "aria-label": "Sites for this agent",
    placeholder: "api.example.com\ncdn.example.net\n*.googleapis.com   (all subdomains)\nnode.example.com:8080   (only this port)\nhttps://docs.example.org/guide   (just the host is kept)" });
  ta.value = (a.egress_extra || []).join("\n");
  const count = el("span", { class: "muted" });
  const recount = () => { const n = ta.value.split("\n").flatMap((l) => l.split("#")[0].split(/[\s,;]+/)).filter((t) => t && !/^[-*+•]$/.test(t)).length; count.textContent = `about ${n} entr${n === 1 ? "y" : "ies"}`; };
  ta.addEventListener("input", recount); recount();
  const close = () => { back.remove(); document.removeEventListener("keydown", esc, true); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const save = el("button", { class: "btn primary", onclick: async () => {
    save.disabled = true;
    try {
      const r = await api("POST", `/api/agents/${a.id}/egress`, { text: ta.value });
      close();
      toast(`Saved ${r.saved} site${r.saved === 1 ? "" : "s"} for ${a.name}` + (r.skipped.length ? `. Skipped ` + r.skipped.slice(0, 4).map(([w, why]) => `${w} (${why})`).join(", ") + (r.skipped.length > 4 ? ` and ${r.skipped.length - 4} more` : "") : ""), r.skipped.length > 0);
      await loadState(); pollAgent(true);
    } catch (e) { toast(e.message, true); save.disabled = false; }
  } }, "Save and apply");
  const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } },
    el("div", { class: "modal xwide", role: "dialog", "aria-modal": "true" },
      el("h3", {}, `Allowed sites for ${a.name}`),
      el("div", { class: "muted pretty" }, `Added to the global allowlist for this agent only. One site per line or comma-separated. *.example.com covers subdomains, and example.com:8080 allows just that port.`),
      ta, el("div", { class: "row" }, count, el("span", { class: "grow" }), el("button", { class: "btn ghost", onclick: close }, "Cancel"), save)));
  document.addEventListener("keydown", esc, true);
  document.body.append(back);
  ta.focus();
}
async function allowAction(a, action, body) {
  try { await api("POST", `/api/agents/${a.id}/${action}`, body); toast(action === "allow" ? `Allowed ${body.host}` : `Removed ${body.host}`); }
  catch (e) { toast(e.message, true); }
  closePop(); await loadState(); pollAgent(true); pollInbox();
}
async function setMcp(a, servers) {
  try { await api("POST", `/api/agents/${a.id}/mcp`, { servers }); toast("MCP servers saved (applied live)"); }
  catch (e) { toast(e.message, true); }
  await loadState();
}

function startPop(a, anchor) {
  const inj = isResearch(a) || isPhone() || !isOwner() ? null : makeInjectWidget(() => !a.repo);  // the folder browser needs your PC
  const go_ = el("button", { class: "btn primary", onclick: async () => {
    let inject = null;
    if (inj) {
      try { inject = await inj.get(); } catch (e) { toast(e.message, true); return; }
      if (inject === false) return;
    }
    closePop();
    try { await api("POST", `/api/agents/${a.id}/start`, { inject }); } catch (e) { toast(e.message, true); }
    loadState();
  } }, "Start");
  return el("div", { class: "stack" }, el("h4", {}, `Start ${a.name}`),
    !isResearch(a) && a.inject_source ? rememberedFolder(a) : "",
    inj ? [el("div", { class: "muted" }, a.inject_source ? "Or pick a different folder:" : "Optionally give it a host folder, read-only at /inject:"), inj.node]
      : el("div", { class: "muted" }, MODE_HELP[a.mode]),
    el("div", { class: "row" }, el("span", { class: "grow" }), el("button", { class: "btn ghost", onclick: closePop }, "Cancel"), go_));
}
function rememberedFolder(a) {  // the folder this agent gets again at every start
  const s = a.inject_source;
  return el("div", { class: "note-box" }, el("div", {}, "Gets ", el("code", {}, s.root), " at every start: ",
    s.mode === "share" ? "a live read-only share" : "a fresh read-only copy", " at /inject", s.to_scratch ? ", plus a working copy in ~/scratch." : "."),
    el("div", { class: "row" }, el("span", { class: "muted grow" }, "Scanned again at every start. New warnings stop the start."),
      el("button", { class: "link", onclick: async () => {
        try { await api("POST", `/api/agents/${a.id}/organize`, { forget_inject: true }); toast("It starts without a folder from now on"); }
        catch (e) { toast(e.message, true); }
        closePop(); loadState();
      } }, "Stop injecting it")));
}
function agentMenu(a) {
  const stopped = isStopped(a);
  const item = (label, sub, enabled, fn, cls) => el("button", { class: "menu-item " + (cls || ""), disabled: !enabled, onclick: () => { closePop(); fn(); } },
    el("span", {}, label, sub ? el("span", { class: "sub" }, sub) : ""));
  const box = el("div", { class: "menu" });
  if (isPhone()) {  // the header and composer have no room for these on a phone
    const c = composerState(a.id);
    const ctx = contextOf(a);  // the header has no room for the meter next to the state on a phone
    box.append(item(stopped ? "Start…" : "Stop", "", a.state !== "stopping", () => stopped ? openPop(AV.head, startPop(a, AV.head)) : stopAgent(a)),
      ctx ? item(`Context ${ctx.pct}%`, ctx.text, true, () => openPop(AV.head, contextPop(a))) : "",
      item("Model", modelLabel(a), true, () => openPop(AV.head, modelPop(a))));
    if (!isResearch(a) && a.repo) box.append(el("div", { class: "menu-item" }, el("span", { class: "grow" }, "New branch", el("span", { class: "sub" }, "for the next message")),
      sw(c.newBranch, (on, b) => { c.newBranch = on; setSw(b, on); SIGS.composer = null; updateComposer(agentById(a.id) || a); })));
    box.append(el("div", { class: "sep" }));
  }
  box.append(item("Stop this task", a.state === "busy" ? "The agent stays on" : "Nothing is running", a.state === "busy", () => interruptTask(a)));
  if (!isResearch(a)) {
    box.append(item("Allowed sites for this agent…", `${(a.egress_extra || []).length} on top of the global list`, true, () => egressEditor(a)));
  }
  box.append(whenIdle(a));
  if (isPhone() && a.status?.stops_at) box.append(item("Restart the clock", statusText(a), true, () => keepRunning(a)));
  box.append(item("Rename", "", true, () => renameInHeader(a)));
  if (isOwner()) box.append(
    item("Clear memory", a.mode !== "memory" ? "Only for ephemeral + memory agents" : !stopped ? "Stop the agent first" : "Forget stored conversations and memory",
      a.mode === "memory" && stopped, async () => {
        if (await confirmModal("Clear memory?", "Forget this agent's stored conversations and memory?", "Clear memory", true)) agentAction(a, "clear-memory");
      }),
    item("Move to new image…", a.mode !== "persistent" ? "Only for persistent agents" : a.move ? "Moving now" : !stopped ? "Stop the agent first"
      : !a.has_disk ? "No disk yet" : "Keeps its files and conversations",
      a.mode === "persistent" && stopped && a.has_disk && !a.move, () => moveImageDialog(a)),
    item("Reset to clean", a.mode !== "persistent" ? "Only for persistent agents" : a.move ? "Moving now" : !stopped ? "Stop the agent first" : !a.has_disk ? "No disk yet"
      : "Deletes the disk. The next start is fresh.",
      a.mode === "persistent" && stopped && a.has_disk && !a.move, async () => {
        if (await confirmModal("Reset to clean?", "Delete this agent's disk? Its checkout and conversation history are lost, and the next start is clean.", "Reset", true)) agentAction(a, "reset");
      }),
    el("div", { class: "sep" }),
    item("Delete agent", "", true, async () => {
      if (await confirmModal(`Delete ${a.name}?`, "A running VM is killed immediately (no memory export). Logs are kept.", "Delete", true)) agentAction(a, "delete");
    }, "danger"));
  return box;
}

// Move to new image: the agent's home folder goes along to a fresh disk from the image you pick (vm.Manager.move_image)
function moveImageDialog(a) {
  const others = (STATE.images || []).filter((i) => i.name !== a.base_image && !i.outdated);  // an outdated image wouldn't start
  if (!others.length) { toast("Build another VM image first, in Settings under VM images.", true); return; }
  const def = isResearch(a) ? STATE.research_base_image : STATE.base_image;
  const pick = el("select", { class: "input", "aria-label": "New VM image" },
    others.map((i) => el("option", { value: i.name }, imageNamed(i.name))));
  const has = el("div", { class: "muted small pretty" });
  const showHas = () => { has.textContent = "Has " + toolchainText(others.find((i) => i.name === pick.value)) + "."; };
  pick.addEventListener("change", showHas);
  pick.value = others.some((i) => i.name === def) ? def : others[others.length - 1].name;
  showHas();
  const done = () => { back.remove(); document.removeEventListener("keydown", esc, true); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(); } };
  const move = el("button", { class: "btn primary" }, "Move");
  move.addEventListener("click", async () => {
    move.disabled = true;
    try {
      await api("POST", `/api/agents/${a.id}/move-image`, { image: pick.value });
      done();
      toast("Moving to the new image");
    } catch (e) { move.disabled = false; toast(e.message, true); }
    await loadState();
  });
  const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) done(); } },
    el("div", { class: "modal", role: "dialog", "aria-modal": "true" },
      el("h3", { class: "title-row" }, "Move to new image", infoTip("The VM starts twice. First it packs the agent's home folder, then it unpacks it "
        + "on a fresh disk from the new image. Anything installed outside the home folder comes from the new image. "
        + "The old disk stays until the move has worked, so if anything goes wrong, the agent stays as it is. "
        + "Your PC needs free space for its files while it moves.")),
      el("div", { class: "body stack tight" },
        el("span", { class: "muted small" }, `Now on ${imageNamed(a.base_image)}`), pick, has,
        el("div", { class: "pretty" }, "Its files, conversations and settings come along.")),
      el("div", { class: "actions" }, el("button", { class: "btn ghost", onclick: done }, "Cancel"), move)));
  document.addEventListener("keydown", esc, true);
  document.body.append(back);
  syncBack();
  pick.focus();
}

// ---- tabs + error banner ----
function renderTabs(a) {
  const n = researchCount(a), blocked = blockedOf(a).length;
  if (!changed("tabs", [a.id, UI.tab, a.profile, n, blocked])) return;
  const tabs = [["chat", "Chat"], ["tasks", "Tasks"], ["files", "Files"], ["logs", "Logs"], ["sandbox", "Sandbox"], ["research", isResearch(a) ? "Findings" : "Research"]];
  AV.tabs.replaceChildren(...tabs.map(([k, label]) => el("button", { class: "tab" + (UI.tab === k ? " on" : ""), role: "tab", onclick: () => switchTab(k) },
    label, k === "research" && n ? el("span", { class: "count-badge" }, String(n)) : "",
    k === "sandbox" && blocked ? el("span", { class: "count-badge err" }, String(blocked)) : "")));
  AV.tabs.querySelector(".tab.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}
function renderBanner(a) {  // also problems while it runs, such as findings the panel had to drop
  if (!changed("banner", [a.error])) return;
  AV.banner.replaceChildren(a.error ? el("div", { class: "banner note-box err" }, el("span", { class: "grow" }, a.error),
    el("button", { class: "link", onclick: () => switchTab("logs") }, "View logs"),
    el("button", { class: "link", onclick: async () => {
      try { await api("POST", `/api/agents/${a.id}/organize`, { clear_error: true }); } catch (e) { toast(e.message, true); }
      loadState();
    } }, "Dismiss")) : "");
}

// ---- chat ----
function bootSteps(a) {
  const steps = [["allocate", "Allocating VM", `${a.cpus} vCPU · ${a.memory_mb} MiB`],
    ["disk", a.mode === "persistent" && a.has_disk ? "Attaching disk" : "Creating fresh disk", a.mode === "persistent" ? "kept on stop" : "deleted on stop"],
    ["boot", "Booting guest", ""], ["policy", `Installing Claude policy v${STATE.policy_version}`, ""]];
  if (a.mode === "memory") steps.push(["memory", "Restoring memory", ""]);
  steps.push(["selftest", "Network self-test", ""]);
  return steps;
}
const bootStep = (a, steps) => Math.max(0, steps.findIndex((s) => s[0] === a.boot_phase));
function bootCard(a) {
  const steps = bootSteps(a), cur = bootStep(a, steps);
  const secs = a.boot_started ? Math.max(0, Math.round(Date.now() / 1000 - a.boot_started)) : 0;
  const bar = el("div", {});
  bar.style.width = Math.round((cur / steps.length) * 100) + "%";
  return el("div", { class: "boot-card" },
    el("div", { class: "card-head" }, el("span", { class: "pulse" }), el("span", { class: "title" }, `Starting ${a.name}`), el("span", { class: "grow" }),
      el("span", { class: "muted" }, `Step ${cur + 1} of ${steps.length}, ${fmtSecs(secs)}`)),
    el("div", { class: "bar" }, bar),
    el("div", { class: "stack tight" }, steps.map(([, label, det], i) => el("div", { class: "boot-step " + (i < cur ? "done" : i === cur ? "now" : "pending") },
      el("span", { class: "mk" }, i < cur ? "✓" : ""), el("span", { class: "lbl" }, label), i <= cur && det ? el("span", { class: "det" }, det) : ""))),
    (BOOTCON[a.id] || []).length ? el("div", { class: "boot-con" }, BOOTCON[a.id].join("\n")) : "");
}
function chatEventsByTask(a) {
  const by = {};
  for (const e of (CHAT[a.id] || { events: [] }).events) (by[e.task] || (by[e.task] = [])).push(e);
  return by;
}
// A message over 2,000 characters: the task record keeps the start, and Show more fetches the rest.
const MSG_OPEN = {};  // task id -> its whole message while you have it open (kept across redraws)
function cutMessage(text) {  // the kept start, cut at a line or word boundary
  const nl = text.lastIndexOf("\n"), sp = text.lastIndexOf(" ");
  if (nl > text.length - 400) return text.slice(0, nl).trimEnd() + "\n…";
  return text.slice(0, sp > text.length - 200 ? sp : text.length).trimEnd() + "…";
}
function chevron(up) {
  const s = svg("svg", { width: 12, height: 12, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2.4,
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
  s.append(svg("path", { d: up ? "m6 15 6-6 6 6" : "m6 9 6 6 6-6" }));
  return s;
}
function stopIcon() {
  const s = svg("svg", { width: 12, height: 12, viewBox: "0 0 24 24", "aria-hidden": "true" });
  s.append(svg("rect", { x: 5, y: 5, width: 14, height: 14, rx: 2.5, fill: "currentColor" }));
  return s;
}
// Ends the task's Claude Code run, not the VM: the task still commits, pushes and keeps what it did.
async function interruptTask(a) {
  if (AV) AV.interrupting = true;
  SIGS.composer = null; updateComposer(agentById(a.id) || a);
  try {
    const r = await api("POST", `/api/agents/${a.id}/interrupt`, {});
    toast(r.result === "finishing" ? "It's already finishing up, so what it did is kept." : "Stopping this task. The agent stays on.");
  } catch (e) { toast(e.message, true); }
  if (AV) AV.interrupting = false;
  SIGS.composer = null;
  await loadState(); pollAgent(true);
}
// Send now: into the running session when the agent takes messages while it works (nothing stops), else it stops the task.
const goesIn = (a, t) => a.state === "busy" && (a.deliver || []).includes(t.id);
function sendNowTip(a, t) {
  if (goesIn(a, t)) return "Claude reads it now. Background work keeps running.";
  return a.state === "busy" ? "This stops the current task, then sends this message." : "Send it before the other messages";
}
const SENDING_NOW = new Set();  // task ids: it can take a few seconds for the running task to take one
async function sendNow(a, t, btn) {
  if (SENDING_NOW.has(t.id)) return;
  const ask = a.state === "busy" && !goesIn(a, t);
  if (ask && !(await confirmModal("Stop the current task and send this now?",
    "This stops the current task, and its background work too. What it did so far is kept. Your message runs next, in the same conversation.", "Send now"))) return;
  SENDING_NOW.add(t.id);
  if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  try {
    // not asked, so the server must never stop a task for it, whatever changed meanwhile
    const r = await api("POST", `/api/agents/${a.id}/tasks/${t.id}/send-now`, ask ? {} : { deliver_only: true });
    toast({ delivered: "Sent. Claude reads it now. Background work keeps running.", sent: "Sent",
      not_delivered: "It couldn't go into the running task, so nothing was stopped. It runs next.",
      moved: "Moved to the front. It runs when the agent is ready.", finishing: "The current task is finishing up. Yours runs next." }[r.result]
      || "Stopping the current task. Yours runs next.");
  } catch (e) { toast(e.message, true); }
  finally { SENDING_NOW.delete(t.id); SIGS.queued = null; }
  await loadState(); pollAgent(true);
}
function messageBubble(a, t) {  // Markdown like the agent's replies (md.js only ever inserts text)
  if (!t.full_message) return el("div", { class: "bubble" }, renderMarkdown(t.instructions));
  const open = MSG_OPEN[t.id] !== undefined;
  const toggle = async () => {
    if (open) delete MSG_OPEN[t.id];
    else {
      try { MSG_OPEN[t.id] = (await api("GET", `/api/agents/${a.id}/tasks/${t.id}/message`)).text; }
      catch (e) { toast(e.message, true); return; }
    }
    SIGS.chat = null;
    renderChat(agentById(a.id) || a);
  };
  return el("div", { class: "bubble" }, renderMarkdown(open ? MSG_OPEN[t.id] : cutMessage(t.instructions)),
    el("div", { class: "more-row" }, el("button", { class: "link more-toggle", type: "button", "aria-expanded": String(open), onclick: toggle },
      open ? "Show less" : "Show more", chevron(open))));
}
// A message waiting its turn sits in a card above the message box, where you can still change it.
// Once it runs it is a normal message in the chat.
const SENT_FILES = {};  // task id -> the files you attached, while this page is open (Edit puts them back)
const queuedMessages = (a) => a.tasks.filter((t) => t.state === "queued" && !t.attach)
  .sort((x, y) => (a.queue || []).indexOf(x.id) - (a.queue || []).indexOf(y.id));
function renderQueued(a) {
  const list = queuedMessages(a), max = isPhone() ? 1 : 2, shown = list.slice(0, max);
  if (!changed("queued", [a.id, a.state, list.length, shown.map((t) => [t.id, t.instructions, (t.attachments || []).length, goesIn(a, t), SENDING_NOW.has(t.id)]), isPhone()])) return;
  AV.c.queued.hidden = !list.length;
  AV.c.queued.replaceChildren(...shown.map((t, i) => queuedCard(a, t, i)),
    list.length > max ? el("button", { class: "link", onclick: () => switchTab("tasks") }, `${list.length - max} more queued`) : "");
}
function queuedCard(a, t, i) {
  const when = i ? "Queued. It runs after the one above." : a.state === "booting" ? "Queued. Sends when the VM is ready." : "Queued. It runs after the current task.";
  const n = (t.attachments || []).length;
  return el("div", { class: "queued-card" },
    el("div", { class: "grow" }, el("div", { class: "when" }, when),
      el("div", { class: "row nowrap" }, el("span", { class: "ellipsis", title: t.instructions.slice(0, 600) }, t.instructions.replace(/\s+/g, " ")),
        n ? el("span", { class: "muted nowrap" }, `${n} file${n === 1 ? "" : "s"}`) : "")),
    el("button", { class: "q-act", onclick: () => takeBack(a, t, true) }, "Edit"),
    el("button", { class: "q-act danger", onclick: () => takeBack(a, t, false) }, "Cancel"),
    el("button", { class: "btn small", title: sendNowTip(a, t), disabled: SENDING_NOW.has(t.id),
      onclick: (e) => sendNow(agentById(a.id) || a, t, e.currentTarget) }, SENDING_NOW.has(t.id) ? "Sending…" : "Send now"),
    onTouch() || isPhone() ? el("div", { class: "muted small q-tip" }, sendNowTip(a, t)) : "");
}
async function takeBack(a, t, intoBox) {
  try { await api("POST", `/api/agents/${a.id}/tasks/${t.id}/cancel`, {}); }
  catch (e) { toast(e.message, true); return; }  // "It already started."
  if (intoBox) {
    let text = t.instructions;
    if (t.full_message) {
      try { text = (await api("GET", `/api/agents/${a.id}/tasks/${t.id}/message`)).text; } catch { /* keep the start */ }
    }
    const c = composerState(a.id), files = SENT_FILES[t.id];
    c.draft = c.draft.trim() ? `${text}\n\n${c.draft}` : text;
    if (files) c.attach = [...files, ...c.attach].slice(0, 10);
    if (AV?.c && UI.agentId === a.id) { AV.c.ta.value = c.draft; AV.c.ta.dispatchEvent(new Event("input")); AV.c.ta.focus(); }
    toast(t.attachments?.length && !files ? "Cancelled. Attach the files again, because they only come back on the page you sent them from." : "Cancelled. Change it and send it again.", t.attachments?.length && !files);
  } else toast("Cancelled");
  SIGS.composer = null;
  await loadState();
  pollAgent(true);
}
function extOf(name) { const m = String(name).match(/\.([A-Za-z0-9]{1,5})$/); return m ? m[1] : "file"; }
const CHAT_PAGE = 20, CHAT_TASKS = {};  // messages drawn per agent (more after "Show earlier messages")
function renderChat(a) {
  const ch = CHAT[a.id] || { events: [], last: 0 };
  const dels = ((EXTRA[a.id] || {}).deliveries || []).filter((d) => !isResearch(a) && (d.forwarded_to || []).includes(a.id));
  const resume = composerState(a.id).resume;
  const w = AV.working;  // the timer ticks in place: a redraw would pull a message out from under your finger
  if (w?.node.isConnected) w.node.textContent = workingText(w.t, w.evs);
  if (!changed("chat", [a.id, a.tasks.map((t) => [t.id, t.state, t.error, t.pr, t.session]), ch.last, (a.requests || []).map((r) => [r.id, r.state]),
    (a.asks || []).map((r) => [r.id, r.state]),
    resume, Object.keys(MSG_OPEN), a.queue, isPhone(),
    a.state, a.boot_phase, a.state === "booting" ? [BOOTCON[a.id], Math.floor(Date.now() / 1000)] : 0, dels.map((d) => d.id), [...OPEN_DIFFS], [...OPEN_TOOLS],
    (a.denied_hosts || []).length, a.running, a.boot_secs])) return;
  const scroller = AV.body, nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160 || !AV.chat.childElementCount;
  const before = ch.last;
  const by = chatEventsByTask(a), out = [];
  // in the order they ran (Send now can change it), then a repository waiting to be attached.
  // Your messages wait in cards above the message box, so one you took back never shows here.
  const ran = (t) => t.started || t.created, queue = a.queue || [];
  const byRun = (x, y) => (ran(x) < ran(y) ? -1 : ran(x) > ran(y) ? 1 : (x.order || 0) - (y.order || 0) || (x.created < y.created ? -1 : 1));
  // a message sent into a running task shows inside that task's block
  const tasks = [...a.tasks.filter((t) => !["queued", "delivered"].includes(t.state) && (t.state !== "cancelled" || t.attach)).sort(byRun),
    ...a.tasks.filter((t) => t.state === "queued" && t.attach).sort((x, y) => queue.indexOf(x.id) - queue.indexOf(y.id))];
  const reqs = [...(a.requests || [])].sort((x, y) => (x.created < y.created ? -1 : 1));
  const later = (list, t, next) => t.state === "queued" ? [] : list.filter((r) => r.created >= ran(t) && (!next || next.state === "queued" || r.created < ran(next)));
  const denied = (a.denied_hosts || []).map((d) => ({ ...d, created: d.at }));
  // Continued tasks share one Claude session, so you can go back to a conversation as it
  // ended: the link sits under the last reply of each earlier conversation.
  const ends = {};
  for (const t of tasks) if (t.session) ends[t.session] = t.id;
  const current = [...tasks].reverse().find((t) => t.session)?.session;  // what "Continue conversation" continues
  const put = (key, node) => { node.dataset.ck = key; out.push(node); };
  if (!tasks.length && a.state !== "booting") out.push(el("div", { class: "empty-chat" }, isResearch(a) ? "Ask a research question below." : "No messages yet. Write the first instructions below."));
  // only the newest messages are drawn; earlier ones come back a page at a time
  const keep = CHAT_TASKS[a.id] || CHAT_PAGE, hidden = Math.max(0, tasks.length - keep);
  if (hidden) out.push(el("button", { class: "show-more chat-earlier", type: "button", onclick: () => {
    CHAT_TASKS[a.id] = keep + CHAT_PAGE;
    SIGS.chat = null;
    const h = AV.body.scrollHeight, top = AV.body.scrollTop;
    renderChat(agentById(a.id) || a);
    AV.body.scrollTop = top + (AV.body.scrollHeight - h);  // stay where you were reading
  } }, `Show ${Math.min(CHAT_PAGE, hidden)} earlier messages`, el("span", { class: "muted" }, ` (${hidden} not shown)`)));
  // each message with what came of it is one block, rebuilt only when what it shows changed:
  // most new output only touches the running task, so the rest of the chat stays as it is
  const blocks = new Map();
  const startedAt = startedLineAt(a, tasks), started = () => put("started", startedLine(a));
  tasks.forEach((t, i) => {
    if (i === startedAt) started();
    if (i < hidden) return;
    const next = tasks[i + 1], evs = by[t.id] || [];
    // the conversation lives in the VM: an ephemeral VM only has the ones since it last booted
    const kept = a.mode !== "ephemeral" || (a.running && a.boot_started && new Date(t.created) / 1000 >= a.boot_started);
    const x = { evs, reqs: later(reqs, t, next), dels: later(dels, t, next), denied: later(denied, t, next), resume: resume === t.id,
      asks: (a.asks || []).filter((r) => r.task === t.id),
      back: !!(t.session && t.session !== current && ends[t.session] === t.id && kept) };
    const mine = new Set(evs.flatMap((e) => [String(e.seq), e.id]));
    // also what it shows that its records don't say: the end time's date part and the research agent's current name
    const sig = JSON.stringify([t, evs.length, evs[0]?.seq, evs.at(-1)?.seq, x.reqs.map((r) => [r.id, r.state]), x.asks,
      x.dels.map((d) => [d.id, agentName(d.research_agent)]), fmtTime(t.finished || t.created),
      x.denied, x.resume, x.back, MSG_OPEN[t.id] !== undefined, [...OPEN_TOOLS, ...OPEN_DIFFS].filter((k) => mine.has(k.replace(/^[-ta]?/, ""))),
      isPhone(), a.state === "booting", a.running && ["ready", "busy"].includes(a.state)]);
    const had = AV.blocks?.get(t.id);
    const b = had?.sig === sig ? had : { sig, node: el("div", { class: "chat-task", "data-ck": "t" + t.id }, taskBlock(a, t, x)) };
    blocks.set(t.id, b);
    out.push(b.node);
  });
  AV.blocks = blocks;
  if (startedAt === tasks.length) started();
  if (a.state === "booting") put("boot", bootCard(a));
  const view = readingPlace(nearBottom);
  placeChildren(AV.chat, out);
  restorePlace(view);
  if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  else if (AV.seenLast != null && before > AV.seenLast) AV.newBelow = true;  // something arrived while you were reading back
  AV.seenLast = before;
  updateJump();
}
// "Started in 6 s": where the time went shows on hover, and on a tap for touch screens
function startedLine(a) {
  const text = `Started in ${fmtSecs(a.boot_secs)}`, detail = bootTimesText(a);
  if (!detail) return el("div", { class: "meta-line" }, text);
  const words = el("button", { class: "link", type: "button", title: detail }), more = el("div", { class: "boot-times muted small" }, detail);
  words.textContent = text;
  const sync = () => { more.hidden = !AV.bootOpen; words.setAttribute("aria-expanded", String(!!AV.bootOpen)); };
  words.addEventListener("click", () => { AV.bootOpen = !AV.bootOpen; sync(); });
  sync();
  return el("div", { class: "started" }, el("div", { class: "meta-line" }, words), more);
}
// Where the start's time went, from the panel's own clock: "QEMU and firmware 1.2 s · Self-test 3.1 s · …"
function bootTimesText(a) {
  const t = Array.isArray(a.boot_times) ? a.boot_times.filter((x) => Array.isArray(x) && typeof x[0] === "string" && typeof x[1] === "number") : [];
  return t.length ? t.map(([what, secs]) => `${what} ${secs.toFixed(1)} s`).join(" · ") : "";
}
function startedLineAt(a, tasks) {  // "Started in 6 s" goes before the first message that ran or waits after this boot, -1 for none
  if (!a.running || a.state === "booting" || typeof a.boot_secs !== "number") return -1;
  const i = tasks.findIndex((t) => t.state === "queued" || new Date(t.started || t.created) / 1000 >= Math.floor(a.boot_started));  // task times are whole seconds
  return i < 0 ? tasks.length : i;
}
function taskBlock(a, t, x) {  // your message, what the agent did and how it ended
  const out = [], put = (key, node) => { node.dataset.ck = key; out.push(node); };
  if (t.attach) put("m" + t.id, el("div", { class: "meta-line" }, `Attaching ${t.attach}`));
  else put("m" + t.id, el("div", { class: "msg-you" },
    t.attachments?.length ? el("div", { class: "files" }, t.attachments.map((n) => t.attachment_set && IMG_EXT.test(n)
      ? chatThumb(a, "attachments", `${t.attachment_set}/${n}`)
      : el("span", { class: "file-chip" }, el("span", { class: "ext" }, extOf(n)), el("span", { class: "ellipsis" }, n)))) : "",
    messageBubble(a, t)));
  if (t.attachments?.length && t.state !== "cancelled") put("f" + t.id, toolLine("Attached", `/attachments/ (${t.attachments.length} file${t.attachments.length > 1 ? "s" : ""}, read-only)`, ""));
  const evs = x.evs, { subs, results } = subagentsOf(evs, t.state === "running");
  let done = null;
  // each image goes right after the step that made it (a tool line that names the file), else above the end line
  const imgAfter = {}, imgLeft = [];
  for (const p of t.images || []) {
    const base = p.split("/").pop();
    const maker = [...evs].reverse().find((e) => e.k === "tool" && !(e.parent && subs[e.parent]) && `${e.target || ""} ${e.detail || ""}`.includes(base));
    if (maker) (imgAfter[maker.id] = imgAfter[maker.id] || []).push(p); else imgLeft.push(p);
  }
  for (const e of evs) {
    if (e.parent && subs[e.parent]) continue;  // shown inside its agent's card
    if (e.k === "text") out.push(claudeText(e));
    else if (e.k === "you") put("y" + e.seq, el("div", { class: "msg-you" },
      messageBubble(a, a.tasks.find((r) => r.id === e.msg) || { id: e.msg, instructions: e.text })));
    else if (e.k === "status") put("e" + e.seq, el("div", { class: "status-line" }, e.text));
    else if (e.k === "tool" && subs[e.id] && (isAgentTool(e) || subs[e.id].reported)) out.push(agentCard(subs[e.id], results, t.state === "running"));
    else if (e.k === "tool") out.push(toolRow(e, results[e.id], !results[e.id] && t.state === "running"));
    if (e.k === "tool" && imgAfter[e.id]) put("i" + e.seq, el("div", { class: "chat-images" }, imgAfter[e.id].map((p) => chatThumb(a, "outbox", p))));
    else if (e.k === "diff") out.push(diffCard(e));
    else if (e.k === "done") done = e;
  }
  for (const r of x.reqs) put("r" + r.id, el("div", { class: "req-card" },
    el("div", { class: "top" }, el("span", { class: "accent-fg" }, "Research request"), el("span", { class: "muted" }, r.name),
      el("span", { class: "grow" }), el("span", { class: "tag " + ({ delivered: "ok", pending: "warn", failed: "err" }[r.state] || "") }, r.state)),
    el("div", { class: "q" }, r.text.slice(0, 2000))));
  for (const r of x.asks) put("a" + r.id, askCard(r));
  for (const d of x.dels) put("d" + d.id, el("div", { class: "meta-line" }, `Findings ${d.id} from ${agentName(d.research_agent)} delivered to /inbox/${d.id}`));
  for (const d of x.denied) put(`x${d.host}:${d.port}`, el("div", { class: "meta-line err" }, `You denied network access to ${d.host}${d.port === 443 ? "" : ":" + d.port}`));
  if (t.state === "running") put("w" + t.id, workingLine(t, evs));
  else if (t.state === "queued") put("q" + t.id, el("div", { class: "meta-line" }, a.state === "booting" ? "Queued. Sends when the VM is ready." : "Queued. It runs after the current task."));
  else if (t.state === "cancelled") put("q" + t.id, el("div", { class: "meta-line" }, "Cancelled"));
  else {
    const when = fmtTime(t.finished || t.created);
    const bits = t.time_limit_stop ? ["Not sent: the time limit stopped the VM", when]
      : t.state === "failed" || killed(t) ? [taskStateText(t), when]
      : [t.state !== "interrupted" ? `Task ${t.id} ${t.state} at ${when}` : t.by_you ? `Stopped by you at ${when}` : `Cut off when the VM stopped at ${when}`];
    if (done?.turns) bits.push(`${done.turns} turn${done.turns === 1 ? "" : "s"}`);
    if (t.model || t.effort) bits.push([t.model, t.effort ? `${t.effort} effort` : ""].filter(Boolean).join(", "));
    if (t.resumed_from) bits.push(`went back to task ${t.resumed_from}`);
    if (t.attach && t.state === "done" && t.pr) bits.splice(0, 1, `Attached ${t.attach} and opened draft PR #${(t.pr.match(/\d+$/) || [""])[0]}`);
    if (imgLeft.length) put("i" + t.id, el("div", { class: "chat-images" }, imgLeft.map((p) => chatThumb(a, "outbox", p))));
    put("q" + t.id, el("div", { class: "meta-line" + (t.state === "failed" || killed(t) ? " err" : "") }, bits.filter(Boolean).join(" · "),
      x.resume ? el("span", { class: "accent-fg" }, "← your next message goes into this conversation") :
      x.back ? el("button", { class: "link", title: "Send your next message into this earlier conversation instead of the current one",
        onclick: () => { composerState(a.id).resume = t.id; SIGS.composer = null; SIGS.chat = null; VIEWS.agent.update(); AV?.c?.ta.focus(); } }, "Back to this conversation") : ""));
    if (errorBelow(t)) put("x" + t.id, el("div", { class: "note-box err" }, t.error));
  }
  return out;
}
function placeChildren(box, nodes) {  // like replaceChildren, but a node already in place isn't touched
  const keep = new Set(nodes);
  for (const n of [...box.children]) if (!keep.has(n)) n.remove();
  let at = box.firstElementChild;
  for (const n of nodes) {
    if (n === at) at = at.nextElementSibling;
    else box.insertBefore(n, at);
  }
}
const claudeText = (e) => el("div", { class: "msg-claude", "data-ck": "e" + e.seq }, renderMarkdown(e.text));
// Where you are reading, so a redraw puts it back: the scroll of each code view (a tool's output, a diff,
// a code block), a text selection, and the message at the top of the view.  Everything is keyed by the
// chat node it's in (data-ck), never by position on the page.
const SCROLL_BOXES = "pre, .agent-prompt, .diff-body";
function nodeKey(n) {
  const host = (n.nodeType === 1 ? n : n.parentElement)?.closest("[data-ck]");
  return host && AV.chat.contains(host) ? host : null;
}
function pathIn(host, n) {
  const path = [];
  for (; n !== host; n = n.parentNode) path.unshift([...n.parentNode.childNodes].indexOf(n));
  return path;
}
function findIn(ck, path) {
  let n = AV.chat.querySelector(`[data-ck="${CSS.escape(ck)}"]`);
  for (const i of path || []) n = n?.childNodes[i];
  return n || null;
}
function readingPlace(nearBottom) {
  const boxes = [];
  for (const b of AV.chat.querySelectorAll(SCROLL_BOXES)) {
    const host = (b.scrollTop || b.scrollLeft) && nodeKey(b);
    if (host) boxes.push([host.dataset.ck, pathIn(host, b), b.scrollTop, b.scrollLeft]);
  }
  let sel = null;
  const s = document.getSelection();
  if (s?.rangeCount && !s.isCollapsed) {
    const ah = nodeKey(s.anchorNode), fh = nodeKey(s.focusNode);
    if (ah && fh) sel = [ah.dataset.ck, pathIn(ah, s.anchorNode), s.anchorOffset, fh.dataset.ck, pathIn(fh, s.focusNode), s.focusOffset];
  }
  let anchor = null;
  if (!nearBottom) {  // the node closest to the top of the view, and how far from it
    const top = AV.body.getBoundingClientRect().top;
    let best = Infinity;
    for (const n of AV.chat.querySelectorAll("[data-ck]")) {
      const r = n.getBoundingClientRect();
      if (r.bottom > top && r.height && Math.abs(r.top - top) < best) { best = Math.abs(r.top - top); anchor = [n.dataset.ck, r.top - top]; }
    }
  }
  return { boxes, sel, anchor };
}
function restorePlace({ boxes, sel, anchor }) {
  for (const [ck, path, top, left] of boxes) {
    const b = findIn(ck, path);
    if (b) { b.scrollTop = top; b.scrollLeft = left; }
  }
  const n = anchor && findIn(anchor[0]);
  if (n) AV.body.scrollTop += n.getBoundingClientRect().top - AV.body.getBoundingClientRect().top - anchor[1];
  if (sel) {
    const an = findIn(sel[0], sel[1]), fn = findIn(sel[3], sel[4]);
    const len = (x) => (x.nodeType === 3 ? x.length : x.childNodes.length);
    try { if (an && fn) document.getSelection().setBaseAndExtent(an, Math.min(sel[2], len(an)), fn, Math.min(sel[5], len(fn))); }
    catch { /* the text changed under it */ }
  }
}
// Scrolled up to read back: a button to jump to the latest message (it says so when there's new output).
function updateJump() {
  if (!AV?.c?.jump || !AV.chat) return;
  const away = AV.body.scrollHeight - AV.body.scrollTop - AV.body.clientHeight;
  if (away < 160) AV.newBelow = false;
  AV.c.jump.hidden = away < 400;
  AV.c.jump.textContent = AV.newBelow ? "↓ New messages" : "↓";
  AV.c.jump.classList.toggle("wide", !!AV.newBelow);
}
function jumpDown() {
  AV.newBelow = false;
  AV.body.scrollTo({ top: AV.body.scrollHeight, behavior: "smooth" });
}
function toolLine(verb, target, result, bad, live, bg) {
  return el("div", { class: "tool-line" }, el("span", { class: "verb", "data-verb": verb }, verb), el("span", { class: "target ellipsis", title: target }, target),
    bg ? el("span", { class: "tag" }, "background") : "",
    result ? el("span", { class: "res ellipsis " + (live ? "live" : bad ? "bad" : "ok"), title: result }, result) : "");
}
// ---- tool lines, subagents ----
const OPEN_TOOLS = new Set();  // expanded tool lines and agent cards (by chat event seq)
const isAgentTool = (e) => e.tool === "Agent" || e.tool === "Task";
function toggleOpen(key) {
  if (OPEN_TOOLS.has(key)) OPEN_TOOLS.delete(key); else OPEN_TOOLS.add(key);
  SIGS.chat = null; renderChat(agentById(UI.agentId));
}
function expandable(node, key, label) {  // a row you can click (or Enter) to show more
  Object.assign(node, { tabIndex: 0, onclick: () => toggleOpen(key), onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleOpen(key); } } });
  node.setAttribute("role", "button");
  node.setAttribute("aria-expanded", String(OPEN_TOOLS.has(key)));
  node.setAttribute("aria-label", label);
  node.classList.add("clickable");
  return node;
}
// Each subagent or background job, merged from its tool call, task_started/progress/notification
// events and its own steps (events tagged with its id as `parent`).
function subagentsOf(evs, taskRunning) {
  const subs = {}, results = {}, calls = {};
  for (const e of evs) if (e.k === "result") results[e.id] = e; else if (e.k === "tool") calls[e.id] = e;
  for (const e of evs) {
    if (e.k === "tool" && (isAgentTool(e) || e.bg)) subs[e.id] = { id: e.id, call: e, kids: [], since: e.ts, ...(subs[e.id] || {}) };
    else if (e.k === "sub") {  // also a command that Claude Code moved to the background itself: it keeps its call
      const s = subs[e.id] || (subs[e.id] = { id: e.id, call: calls[e.id], kids: [], since: calls[e.id]?.ts ?? e.ts });
      Object.assign(s, { ...e, call: s.call, kids: s.kids, since: s.since }); s.reported = true;
    }
  }
  for (const e of evs) if (e.parent && subs[e.parent] && e.k !== "sub") subs[e.parent].kids.push(e);
  for (const s of Object.values(subs)) {
    const r = results[s.id];
    if (!s.reported) s.state = r && !s.call?.bg ? (r.ok ? "completed" : "failed") : "running";
    if (s.state === "running" && !taskRunning) s.state = "stopped";  // the run ended, so did its helpers
    s.isAgent = !s.call || isAgentTool(s.call);
    s.desc = s.desc || s.call?.target || "subagent";
    s.type = s.sub || s.call?.sub || "";
    s.bg = !!(s.bg || s.call?.bg);
    const lastKid = [...s.kids].reverse().find((k) => k.k === "tool");
    s.now = lastKid ? `${lastKid.verb} ${lastKid.target}` : s.last ? `using ${s.last}` : "";
    s.result = results[s.id];
  }
  return { subs, results };
}
function fmtTokens(n) { return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n); }
function subStats(s) {
  return [s.tools != null ? `${s.tools} tool${s.tools === 1 ? "" : "s"}` : "", s.tokens ? `${fmtTokens(s.tokens)} tokens` : "",
    s.ms ? fmtSecs(Math.round(s.ms / 1000)) : ""].filter(Boolean).join(", ");
}
const SUB_LABEL = { running: "running", completed: "done", failed: "failed", stopped: "stopped" };
function toolRow(e, r, live) {
  const key = "t" + e.seq, open = OPEN_TOOLS.has(key);
  const row = expandable(toolLine(e.verb, e.target, r ? r.summary || (r.ok ? "ok" : "failed") : live ? "running…" : "", r && !r.ok, live, e.bg),
    key, `${e.verb} ${e.target}: show details`);
  row.dataset.ck = "e" + e.seq;
  if (!open) return row;
  const full = e.detail || e.target, lang = e.tool === "Bash" ? "sh" : e.detail && e.detail.startsWith("{") ? "json" : "";
  delete row.dataset.ck;
  return el("div", { class: "tool-wrap", "data-ck": "e" + e.seq }, row, el("div", { class: "tool-detail" },
    el("pre", { class: "tool-cmd" }, ...highlight(full, lang)),
    r?.out || r?.summary ? el("pre", { class: "tool-out" + (r.ok ? "" : " bad") }, r.out || r.summary) : live ? el("div", { class: "muted" }, "Still running…") : ""));
}
function agentCard(s, results, live) {
  const key = "a" + (s.call?.seq ?? s.id), open = OPEN_TOOLS.has(key);
  const head = expandable(el("div", { class: "agent-card-head" },
    el("span", { class: "verb" }, s.isAgent ? "Agent" : "Background"), s.type ? el("span", { class: "mono muted" }, s.type) : "",
    el("span", { class: "grow ellipsis", title: s.desc }, s.desc),
    s.bg && s.isAgent ? el("span", { class: "tag" }, "background") : "",
    el("span", { class: "muted" }, subStats(s)),
    el("span", { class: "sub-state " + s.state }, SUB_LABEL[s.state] || s.state), el("span", { class: "dim" }, open ? "▲" : "▼")),
  key, `${s.desc}: ${open ? "hide" : "show"} its steps`);
  const body = [];
  if (!open && s.state === "running" && s.now) body.push(el("div", { class: "agent-now ellipsis" }, "→ " + s.now));
  if (open) {
    if (s.call?.detail) body.push(el("div", { class: "agent-prompt" }, s.call.detail));
    for (const k of s.kids) {
      if (k.k === "text") body.push(claudeText(k));
      else if (k.k === "tool") body.push(toolRow(k, results[k.id], live && !results[k.id]));
      else if (k.k === "diff") body.push(diffCard(k));
    }
    if (!s.kids.length && !s.isAgent && s.call) body.push(toolRow(s.call, s.result, false));
    const res = s.summary || (s.isAgent && s.result && !s.bg ? s.result.out || s.result.summary : "");
    if (res) body.push(el("div", { class: "agent-result" }, el("div", { class: "label" }, "Result"), renderMarkdown(res)));
  }
  return el("div", { class: "agent-card " + s.state, "data-key": key, "data-ck": key }, head, ...body);  // not "e…": its own tool row has that
}
// Above the message box while anything runs alongside Claude: subagents and background commands.
// One line with their names; open, it shows what each is doing and for how long.
const RUNNING_OPEN = new Set();  // agents whose strip you opened
const clock = (secs) => secs < 3600 ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`
  : `${Math.floor(secs / 3600)}:${String(Math.floor(secs % 3600 / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
const runningFor = (since) => since ? clock(Math.max(0, Math.round((Date.now() - new Date(since)) / 1000))) : "";
function runningLabel(list) {
  const agents = list.filter((s) => s.isAgent).length, cmds = list.length - agents;
  const what = [agents ? `${agents} subagent${agents > 1 ? "s" : ""}` : "", cmds ? `${cmds} background command${cmds > 1 ? "s" : ""}` : ""];
  return `${what.filter(Boolean).join(" and ")} running: ${list.map((s) => s.isAgent ? s.type || s.desc : s.desc).join(", ")}`;
}
function renderRunning(a) {
  const t = a.tasks.find((x) => x.state === "running");
  const list = t ? Object.values(subagentsOf(chatEventsByTask(a)[t.id] || [], true).subs).filter((s) => s.state === "running") : [];
  const open = RUNNING_OPEN.has(a.id);
  for (const [node, since] of AV.c.runningTimes || []) node.textContent = runningFor(since);  // ticks in place
  if (!changed("running", [a.id, open, list.map((s) => [s.id, s.now, s.desc, s.type, s.since])])) return;
  AV.c.running.hidden = !list.length;
  AV.c.runningTimes = [];
  if (!list.length) { AV.c.running.replaceChildren(); return; }
  const toggle = () => { if (open) RUNNING_OPEN.delete(a.id); else RUNNING_OPEN.add(a.id); renderRunning(agentById(a.id) || a); };
  const row = (s) => {
    const time = el("span", { class: "mono dim" }, runningFor(s.since));
    AV.c.runningTimes.push([time, s.since]);
    return el("button", { class: "running-row", title: "Show it in the chat", onclick: () => {
      const key = "a" + (s.call?.seq ?? s.id);
      OPEN_TOOLS.add(key); SIGS.chat = null; renderChat(agentById(UI.agentId));
      AV.chat.querySelector(`[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
    } }, el("span", { class: "mono muted" }, s.type || (s.isAgent ? "agent" : "shell")), el("span", { class: "ellipsis" }, s.desc),
      el("span", { class: "grow ellipsis dim" }, s.now), s.bg && s.isAgent ? el("span", { class: "dim" }, "background") : "", time);
  };
  AV.c.running.replaceChildren(
    el("button", { class: "running-head", "aria-expanded": String(open), onclick: toggle }, stateIcon("running"),
      el("span", { class: "grow ellipsis" }, runningLabel(list)), el("span", { class: "dim" }, open ? "▲" : "▼")),
    open ? el("div", { class: "running-rows" }, list.map(row)) : "");
}

function fmtSecs(s) { return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min`; }
function workingText(t, evs) {  // so a long step (a build, compacting) doesn't look stuck
  const secs = (iso) => Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  const since = secs(t.started || t.created), quiet = evs.length ? secs(evs[evs.length - 1].ts) : since;
  return `Working for ${fmtSecs(since)}` + (quiet >= 30 && evs.length ? `, last update ${fmtSecs(quiet)} ago` : "");
}
function workingLine(t, evs) {
  const node = el("span", { class: "muted" }, workingText(t, evs));
  AV.working = { node, t, evs };
  return el("div", { class: "working-row" }, stateIcon("running"), node);
}
function diffCard(e) {
  const key = `${e.seq}`, open = OPEN_DIFFS.has(key) || (!OPEN_DIFFS.has("-" + key) && e.lines.length <= 14);
  const head = el("button", { class: "diff-head", "aria-expanded": String(open), onclick: () => {
    if (open) { OPEN_DIFFS.delete(key); OPEN_DIFFS.add("-" + key); } else { OPEN_DIFFS.add(key); OPEN_DIFFS.delete("-" + key); }
    SIGS.chat = null; renderChat(agentById(UI.agentId));
  } }, el("span", {}, e.add && !e.del && e.lines.every((l) => l[0] !== "-") ? "Wrote" : "Edited"), el("span", { class: "file ellipsis" }, e.file),
    el("span", { class: "add" }, `+${e.add}`), el("span", { class: "del" }, `−${e.del}`), el("span", { class: "dim" }, open ? "▲" : "▼"));
  const lang = langOfFile(e.file);
  return el("div", { class: "diff", "data-ck": "e" + e.seq }, head, open ? el("div", { class: "diff-body" },
    e.lines.map(([s, t]) => s === "@" ? el("div", { class: "h" }, "@" + t)
      : el("div", { class: { "+": "p", "-": "m" }[s] || "c" }, el("span", { class: "sg" }, s === " " ? "  " : s + " "), ...highlight(t, lang)))) : "");
}

// ---- composer ----
const onTouch = () => window.matchMedia("(pointer: coarse)").matches;
// a soft keyboard has no Shift+Enter, so there Enter is a new line and only the button sends
const enterSends = (e) => e.key === "Enter" && !e.shiftKey && !e.isComposing && !onTouch();
const composerHint = (a) => a.state === "booting" ? `Type now. It sends once ${a.name} is ready.` : `Message ${a.name}…`;
function composer(a) {
  const c = composerState(a.id);
  const ta = el("textarea", { rows: 1, placeholder: composerHint(a), "aria-label": "Message", value: c.draft, spellcheck: true });
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + "px"; };
  // a box no longer on the page (dictation that ended after you left) only keeps its draft
  ta.addEventListener("input", () => { c.draft = ta.value; if (AV?.c?.ta !== ta) return; grow(); updateComposer(agentById(a.id)); });
  ta.addEventListener("keydown", (e) => {
    if (enterSends(e)) { e.preventDefault(); sendMessage(); }
  });
  ta.addEventListener("paste", (e) => {
    const f = [...(e.clipboardData?.files || [])];
    if (f.length) { e.preventDefault(); addAttach(f); return; }
    // text copied out of a chat can hold whole commands and their output: past the limit, it goes along as a file
    const text = e.clipboardData?.getData("text/plain") || "";
    if (ta.value.length + text.length > MAX_MESSAGE) {
      e.preventDefault();
      addAttach([new File([text], "pasted-text.txt", { type: "text/plain" })]);
      toast(`The pasted text has ${text.length.toLocaleString("en")} characters, so it's attached as pasted-text.txt`);
    }
  });
  const box = el("div", { class: "composer" });
  const files = el("input", { type: "file", multiple: "", hidden: true });
  files.addEventListener("change", () => { addAttach([...files.files]); files.value = ""; });
  const voiceChanged = () => { if (AV?.c?.ta !== ta) return; SIGS.composer = null; updateComposer(agentById(a.id)); };
  AV.c = { ta, box, attachRow: el("div", { class: "attach-row" }), chips: el("div", { class: "chips" }), send: el("span"),
           mic: micButton(ta, voiceChanged, "agent:" + a.id), resumeRow: el("div", { class: "resume-row" }), asks: el("div", { class: "asks" }),
           running: el("div", { class: "running-strip" }), queued: el("div", { class: "queued-cards", hidden: true }),
           jump: el("button", { class: "jump", hidden: true, "aria-label": "Jump to the latest message", onclick: jumpDown }, "↓") };
  // two groups, so the buttons stay together on the right however many chips there are
  box.append(AV.c.resumeRow, AV.c.attachRow, ta, el("div", { class: "bottom" }, AV.c.chips, el("div", { class: "acts" },
    el("button", { class: "round", title: "Attach files (read-only in the VM)", "aria-label": "Attach files", onclick: () => files.click() }, "+"), files,
    AV.c.mic, AV.c.send)));
  setTimeout(grow);
  return [AV.c.jump, AV.c.asks, AV.c.running, AV.c.queued, box, AV.c.caption = el("div", { class: "caption", hidden: true })];
}
// What this agent is waiting on you for (blocked sites, research questions), right above
// the message box. Same rules as the inbox: Allow means this agent only, and a long
// question is approved from the review page where you can read all of it.
function renderAsks(a) {
  const mine = INBOX.items.filter((it) => it.agent === a.id && (it.kind === "network" || it.kind === "research"));
  const phone = isPhone(), rec = openAsk(a);
  if (!changed("asks", [a.id, rec?.id, mine.map((it) => it.key), phone, STATE.egress_ports])) return;
  const shown = mine.slice(0, phone || rec ? 1 : 2);  // the chat needs the room; the rest wait in the inbox
  AV.c.asks.replaceChildren(...(rec ? [askStrip(a, rec)] : []), ...shown.map((it) => {
    const net = it.kind === "network", short = !net && it.text.length <= SHORT_Q && it.research_agent;
    const acts = (...btns) => el("div", { class: "ask-acts" }, ...btns);
    if (net && (it.fixable === false || !canAllow(it))) {
      return el("div", { class: "ask net" }, el("span", { class: "tag err" }, "Blocked"),
        el("span", { class: "grow ellipsis", title: itemSummary(it) }, `${a.name} tried to reach ${it.host}:${it.port}. ${it.fixable === false ? it.reason || "" : PORTS_NOTE}`),
        acts(el("button", { class: "btn small", onclick: () => dismissDenial(it) }, "Dismiss")));
    }
    return el("div", { class: "ask " + (net ? "net" : "req") },
      el("span", { class: "tag " + (net ? "err" : "accent") }, net ? "Blocked" : "Research"),
      el("span", { class: "grow ellipsis", title: net ? itemSummary(it) : it.text.slice(0, 600) },
        net ? `${a.name} tried to reach ${it.host}${it.port === 443 ? "" : ":" + it.port}` + (it.count > 1 ? ` (${it.count}×)` : "")
          : `Wants to ask “${itemTitle(it)}”`),
      acts(el("button", { class: "btn small", onclick: () => net ? dismissDenial(it) : dismissRequest(it) }, "Deny"),
        net ? el("button", { class: "btn small primary", title: `Allow ${siteFor(it)} for ${a.name} only`, onclick: () => allowHost(it, siteFor(it), "agent") }, allowLabel(it))
          : short ? el("button", { class: "btn small primary", onclick: () => approveRequest(it) }, "Approve")
          : el("button", { class: "btn small primary", onclick: () => review(it.key) }, "Review")));
  }), mine.length > shown.length ? el("button", { class: "link", onclick: openInbox }, `${mine.length - shown.length} more in the inbox`) : "");
  AV.c.asks.hidden = !mine.length && !rec;
  if (rec) focusAsk(a, rec);
}
const MAX_MESSAGE = 50000;  // characters, as the panel checks it
const tooLong = (n) => `This message has ${n.toLocaleString("en")} characters, and one can have at most ${MAX_MESSAGE.toLocaleString("en")}. `
  + "Put the long part in a file and attach it with +.";
// Files for a message go into `into` (a composer's attach list), within the limits the panel checks
function takeFiles(list, into) {
  const lim = STATE.attachments || { max_file_bytes: 25 * 1024 ** 2, max_files: 10 };
  for (const f of list) {
    if (into.length >= lim.max_files) { toast(`At most ${lim.max_files} files per message`, true); break; }
    if (f.size > lim.max_file_bytes) {  // checked before the browser reads it: a big file would only fail there
      toast(`${f.name || "That file"} is ${fmtBytes(f.size)}. Attached files can be at most ${fmtBytes(lim.max_file_bytes)}. `
        + "For bigger ones, put them in a folder and give it to the agent as its Host folder on the Sandbox tab.", true);
      continue;
    }
    into.push(f);
  }
}
function addAttach(list) {
  takeFiles(list, composerState(UI.agentId).attach);
  SIGS.composer = null;
  updateComposer(agentById(UI.agentId));
}
// One attached file, with its type, name, size and a way to take it off again
function fileChip(f, remove) {
  return el("span", { class: "file-chip" }, el("span", { class: "ext" }, extOf(f.name)),
    el("span", { class: "ellipsis" }, f.name || "pasted"), el("span", { class: "dim size" }, fmtBytes(f.size)),
    el("button", { class: "x", title: "Remove", "aria-label": "Remove " + f.name, onclick: remove }, "×"));
}
// Files dragged over the window go to the message box on the page, which gets an outline meanwhile
const DROP = { depth: 0 };
function dropTarget() {
  if (UI.view === "agent" && AV?.c?.box?.isConnected) return { box: AV.c.box, add: addAttach };
  const P = VIEWS.new?.p;
  if (UI.view === "new" && P?.box?.isConnected) return { box: P.box, add: addNewAttach };
  return null;
}
const draggingFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => {
  const t = draggingFiles(e) && dropTarget();
  if (!t) return;
  DROP.depth++;
  t.box.classList.add("drop");
});
document.addEventListener("dragover", (e) => {
  if (!draggingFiles(e) || !dropTarget()) return;
  e.preventDefault();  // or the browser opens the file instead
  e.dataTransfer.dropEffect = "copy";
});
document.addEventListener("dragleave", (e) => {
  if (!draggingFiles(e) || --DROP.depth > 0) return;
  DROP.depth = 0;
  dropTarget()?.box.classList.remove("drop");
});
document.addEventListener("drop", (e) => {
  DROP.depth = 0;
  const t = draggingFiles(e) && dropTarget();
  if (!t) return;
  e.preventDefault();
  t.box.classList.remove("drop");
  const f = [...(e.dataTransfer.files || [])];
  if (f.length) t.add(f);
});
function updateComposer(a) {
  if (!AV?.c || !a) return;
  renderAsks(a);
  renderRunning(a);
  renderQueued(a);
  const c = composerState(a.id), stopped = isStopped(a), recording = recordingInto(AV.c.ta);
  const talking = VOICE.area === AV.c.ta;  // recording, or still turning it into text: a message is coming
  const canBranch = !isResearch(a) && !!a.repo;
  // an ephemeral VM that isn't running starts fresh: nothing of the earlier conversation is left to continue
  const fresh = a.mode === "ephemeral" && stopped;
  AV.c.ta.placeholder = recording ? "Listening…" : composerHint(a);
  if (changed("composer", [a.id, a.state, a.mode, a.auto_stop, c.attach.map((f) => f.name + f.size), c.cont, c.resume, c.newBranch, canBranch, (a.mcp || []).length, !!c.draft.trim(), AV.sending, AV.interrupting, STATE.speech, a.model, a.effort, isPhone(), recording, talking])) {
    const from = c.resume && a.tasks.find((t) => t.id === c.resume);
    if (c.resume && !from) c.resume = null;
    AV.c.resumeRow.replaceChildren(...(from ? [el("span", { class: "accent-fg" }, "↩"),
      el("span", { class: "grow ellipsis" }, `Going back to an earlier conversation (it ended with “${from.instructions.slice(0, 120)}”)`),
      el("button", { class: "link", onclick: () => { c.resume = null; SIGS.composer = null; SIGS.chat = null; VIEWS.agent.update(); } }, "Cancel")] : []));
    AV.c.resumeRow.hidden = !from;
    AV.c.attachRow.replaceChildren(...c.attach.map((f, i) => fileChip(f, () => { c.attach.splice(i, 1); SIGS.composer = null; updateComposer(a); })));
    AV.c.attachRow.hidden = !c.attach.length;
    let convChip;
    const chip = (label, on, fn, title) => el("button", { class: "chip" + (on ? " on" : ""), type: "button", title: title || "", onclick: fn }, label);
    // a local model's name can be long (hf.co/org/repo:quant): its last part, cut short if need be, and all of it on hover
    const short = a.model_server ? modelLabel({ ...a, model: a.model.split("/").pop() }) : modelLabel(a);
    const modelChip = el("button", { class: "chip model-chip" + (a.model || a.effort ? " on" : ""), type: "button",
      title: `Model and effort for the next message: ${modelLabel(a)}`, onclick: () => openPop(modelChip, modelPop(a), { width: 370, above: true, align: "left" }) },
      el("span", { class: "ellipsis" }, short), " ", foldMark(true));
    const tools = (a.mcp || []).length + (a.plugins || []).length + (a.skills || []).length;
    const mcpChip = chip(tools ? `MCP (${tools})` : "MCP", tools > 0, () => openPop(mcpChip, mcpPop(a), { width: 400, above: true, align: "left" }),
      "MCP servers, plugins and skills");
    const pr = el("input", { class: "input pill-in", placeholder: "PR title (optional)", maxlength: 200, value: c.prTitle });
    pr.addEventListener("input", () => { c.prTitle = pr.value; });
    AV.c.chips.replaceChildren(
      fresh ? el("span", { class: "chip static", title: "The VM starts fresh, so the earlier conversation is gone" }, "New conversation")
        // teal only when it isn't the current conversation, like the model chip off its default
        : (convChip = chip([c.resume ? "Earlier conversation" : c.cont ? (isPhone() ? "Continue" : "Continue conversation") : "New conversation", " ", foldMark(true)],
          !c.cont || !!c.resume, () => openPop(convChip, convPop(a), { width: 400, above: true, align: "left" }), "Which conversation your next message goes into")),
      isPhone() ? "" : modelChip,  // on a phone, Model and New branch are in ···
      Object.keys(STATE.mcp_servers || {}).length ? mcpChip : "",
      canBranch && !isPhone() ? chip("New branch", c.newBranch, () => { c.newBranch = !c.newBranch; SIGS.composer = null; updateComposer(a); }) : "",
      canBranch && c.newBranch ? pr : "");
    const has = !!c.draft.trim() || c.attach.length > 0;  // a file alone is a message too
    const label = stopped ? "Start and send ↑" : ["busy", "booting"].includes(a.state) ? "Queue ↑" : null;
    // while a task runs, an empty box offers to stop it; once you type, or while you talk, the button queues your message
    const stop = a.state === "busy" && !has && !c.attach.length && !talking;
    const send = stop ? el("button", { class: "round send stop", title: "Stop this task", "aria-label": "Stop this task", onclick: () => interruptTask(a) }, stopIcon())
      : label ? el("button", { class: "send-pill", onclick: sendMessage }, label) : el("button", { class: "round send", title: onTouch() ? "Send" : "Send (Enter)", "aria-label": "Send", onclick: sendMessage }, "↑");
    send.disabled = stop ? !!AV.interrupting : !has || AV.sending || a.state === "stopping";
    AV.c.send.replaceChildren(send);
    AV.c.mic.hidden = !STATE.speech?.enabled;
    AV.c.caption.textContent = composerCaption(recording, c.attach.length);
    AV.c.caption.hidden = !AV.c.caption.textContent;
  }
}
// Only what matters for this message: where your voice goes, or where your files end up
function composerCaption(recording, files) {
  return recording ? `Audio goes to whisper on ${STATE.speech?.host}, nowhere else.` : files ? "Files are copied in read-only at /attachments." : "";
}
async function sendMessage() {
  const a = agentById(UI.agentId);
  if (!a || AV?.sending) return;
  const c = composerState(a.id), text = c.draft.trim();
  if (!text && !c.attach.length) { toast("Write some instructions first.", true); return; }
  if (text.length > MAX_MESSAGE) { toast(tooLong(text.length), true); return; }
  AV.sending = true; SIGS.composer = null; updateComposer(a);
  if (/^\/plugins?(\s|$)/i.test(text)) {  // cilly's own command: it changes the agent's plugins, and starts nothing
    try {
      toast((await api("POST", `/api/agents/${a.id}/message`, { instructions: text })).plugin);
      c.draft = "";
      if (AV?.c) { AV.c.ta.value = ""; AV.c.ta.style.height = "auto"; }
    } catch (e) { toast(e.message, true); }
    if (AV) AV.sending = false;
    SIGS.composer = null;
    await loadState();
    return;
  }
  try {
    if (isStopped(a)) {
      toast(`Starting ${a.name}…`);
      await api("POST", `/api/agents/${a.id}/start`, { inject: null });  // the message queues until the VM is ready
    }
    const attachments = [];
    for (const f of c.attach) {
      toast(`Uploading ${f.name || "file"}…`);
      attachments.push((await api("POST", `/api/agents/${a.id}/uploads`, { name: f.name || "pasted.png", data: await fileToBase64(f) })).id);
    }
    const sent = await api("POST", `/api/agents/${a.id}/message`, { instructions: text, continue: c.cont, resume: c.resume, new_branch: c.newBranch, pr_title: c.prTitle, attachments, queue: true });
    if (c.attach.length) SENT_FILES[sent.task] = [...c.attach];
    Object.assign(c, { draft: "", newBranch: false, prTitle: "", attach: [], resume: null, cont: true });
    if (AV?.c) { AV.c.ta.value = ""; AV.c.ta.style.height = "auto"; }
  } catch (e) { toast(e.message, true); }
  if (AV) AV.sending = false;
  SIGS.composer = null;
  await loadState();
  pollAgent(true);
}
function fileToBase64(f) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(new Error("could not read " + f.name));
    r.readAsDataURL(f);
  });
}
// Findings files: Markdown is rendered (safely, see md.js); anything else stays plain text.
function fileBody(f) {
  return /\.(md|markdown)$/i.test(f.path) ? el("div", { class: "card-body" }, renderMarkdown(f.content)) : el("pre", { class: "block flat" }, f.content);
}
const MODELS = [["", "Default", "What your plan gets"], ["opus", "Opus", "Most capable"], ["fable", "Fable", "If your plan includes it"],
  ["sonnet", "Sonnet", "Fast and capable"], ["haiku", "Haiku", "Fastest"]];
const EFFORTS = [["", "Default"], ["low", "Low"], ["medium", "Medium"], ["high", "High"], ["xhigh", "X-high"], ["max", "Max"]];
// Which conversation the next message goes into: the current one, a new one, or an earlier one.
// Continued tasks share Claude's session id, so a conversation is the tasks with the same one.
function conversationsOf(a) {
  const by = new Map();
  for (const t of a.tasks || []) {
    if (t.attach || !t.session) continue;
    const c = by.get(t.session) || { sid: t.session, first: t, last: t, n: 0 };
    c.last = t; c.n++;
    by.set(t.session, c);
  }
  return [...by.values()];
}
function convPop(a) {
  const c = composerState(a.id), all = conversationsOf(a), current = all[all.length - 1];
  // an ephemeral VM only has the conversations since it last booted
  const kept = (t) => a.mode !== "ephemeral" || (a.running && a.boot_started && new Date(t.created) / 1000 >= a.boot_started);
  const pick = (cont, resume) => {
    Object.assign(c, { cont, resume });
    closePop(); SIGS.composer = null; SIGS.chat = null; VIEWS.agent.update(); AV?.c?.ta.focus();
  };
  const row = (label, sub, on, fn, enabled = true) => el("button", { class: "menu-item" + (on ? " on" : ""), disabled: !enabled, onclick: fn },
    el("span", { class: "grow" }, label, sub ? el("span", { class: "sub" }, sub) : ""), on ? "✓" : "");
  const said = (cv) => `“${cv.first.instructions.split("\n")[0].slice(0, 80)}”`;
  const when = (cv) => `${cv.n} message${cv.n === 1 ? "" : "s"}, last at ${fmtTime(cv.last.finished || cv.last.created)}`;
  const earlier = all.slice(0, -1).reverse();
  return el("div", { class: "stack tight" }, el("h4", {}, "Conversation"),
    el("div", { class: "muted" }, "Which conversation your next message goes into."),
    current ? row("Continue the current one", `${said(current)}, ${when(current)}`, c.cont && !c.resume, () => pick(true, null), kept(current.last))
      : row("Continue the current one", "There's no conversation yet.", c.cont && !c.resume, () => pick(true, null)),
    row("Start a new one", "The agent won't see earlier messages. Files in the VM stay.", !c.cont && !c.resume, () => pick(false, null)),
    earlier.length ? el("div", { class: "sep" }) : "",
    earlier.length ? el("div", { class: "row" }, el("strong", {}, "Go back to")) : "",
    ...paged("conversations-" + a.id, earlier, (cv) => row(said(cv), kept(cv.last) ? when(cv) : "Gone with the VM it ran in",
      c.resume === cv.last.id, () => pick(true, cv.last.id), kept(cv.last)), { step: 8 }));
}
// When it has nothing to do: one choice for Stop after each task ("task") and Stop when idle (minutes),
// so the two never disagree. Research agents have no "task": they stay up between requests.
const whenIdleOf = (a) => (a.auto_stop && !isResearch(a) ? "task" : a.idle_stop || "keep");
function whenIdleHelp(a, v) {
  if (v === "keep") return "Stays on between tasks so the next message starts right away. Uses memory while idle.";
  if (v === "task") return "Stops after the last task and queued message. " + (a.keep_outbox ? "Ephemeral agents lose their disk, after their outbox is copied to your PC."
    : "Ephemeral agents lose their disk.");
  return `Stops after ${idleStopLabel(v)} with no task, no queued message and nothing in the inbox. `
    + (a.mode === "persistent" ? "Its disk is kept." : a.mode === "memory" ? "Its memory is saved first." : "Everything in it is deleted.")
    + (a.keep_outbox && a.mode !== "persistent" ? " Its outbox is copied to your PC before that." : "");
}
const whenIdleName = (v) => v === "keep" ? "Keep running" : v === "task" ? "Stop right away" : `Stop after ${idleStopLabel(v)}`;
function whenIdle(a) {
  let now = whenIdleOf(a);
  const opts = [["keep", "Keep running"], ...(isResearch(a) ? [] : [["task", "Stop right away"]]), ...IDLE_STOP.slice(1)];
  const help = el("div", { class: "help" }, whenIdleHelp(a, now));
  const show = (v) => { help.textContent = whenIdleHelp(a, v); choice.querySelectorAll("button").forEach((b, i) => b.classList.toggle("on", opts[i][0] === v)); };
  const choice = seg(opts, now, async (v) => {
    if (v === now) return;
    const loss = IDLE_STOP_LOSS[a.mode];
    if (typeof v === "number" && now === "keep" && loss && !(await confirmModal(`Stop ${a.name} when idle?`, loss, "Turn on"))) { show(now); return; }
    show(v);
    try {
      await api("POST", `/api/agents/${a.id}/when-idle`, { value: v });
      now = v; value.textContent = whenIdleName(v);
      toast(v === "keep" ? `${a.name} keeps running until you stop it` : v === "task" ? `${a.name} stops when it has nothing left to do`
        : `${a.name} stops after ${idleStopLabel(v)} with nothing to do`);
    } catch (e) { show(now); toast(e.message, true); }
    loadState();
  }, "when-idle" + (isResearch(a) ? " no-task" : ""));
  const body = el("div", { class: "menu-block-body" }, choice, help);
  const value = el("span", { class: "sub" }, whenIdleName(now));
  const mark = el("span", {}, foldMark(false));
  const head = el("button", { class: "menu-item", "aria-expanded": "false", onclick: () => {
    const open = body.hidden;
    body.hidden = !open;
    head.setAttribute("aria-expanded", String(open));
    mark.replaceChildren(foldMark(open));
  } }, el("span", { class: "grow" }, "When it has nothing to do", value), mark);
  body.hidden = true;
  return el("div", { class: "menu-block" }, head, body);
}
async function keepRunning(a) {
  try { await api("POST", `/api/agents/${a.id}/keep-running`, {}); toast("The idle clock starts again"); }
  catch (e) { toast(e.message, true); }
  SIGS.head = null; await loadState();
}
const foldedInModelPop = new Set();  // kept until the page reloads, so a section you folded stays folded
function modelSection(key, head, ...body) {
  const box = el("div", { class: "stack tight" }, ...body);
  const mark = el("span", { class: "mp-fold" });
  const toggle = el("button", { class: "mp-head", onclick: () => {
    if (!foldedInModelPop.delete(key)) foldedInModelPop.add(key);
    show();
  } }, head, mark);
  const show = () => {
    const open = !foldedInModelPop.has(key);
    box.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    mark.replaceChildren(foldMark(open));
  };
  show();
  return el("div", { class: "stack tight" }, toggle, box);
}
function modelPop(a) {
  const local = !!a.model_server, claudeModel = local ? "" : (a.model || "");
  const pick = async (v, server) => {
    if (!server && !(await leavingLocal(a))) return;
    try {
      await api("POST", `/api/agents/${a.id}/model`, { model: v || null, server: server || null });
      toast(v ? `${v} is used from the next message` : "Back to the default model");
    } catch (e) { toast(e.message, true); return; }
    closePop(); await loadState();
  };
  const row = (on, name, onclick, ...rest) => el("button", { class: "menu-item mp-row" + (on ? " on" : ""), onclick },
    el("span", { class: "mp-name" }, name), ...rest, el("span", { class: "mp-check" }, on ? "✓" : ""));
  // a model id of your own, behind a link: rarely needed
  const custom = el("input", { class: "input mono grow", placeholder: "a model id, e.g. claude-opus-5-5", spellcheck: false,
    value: local || MODELS.some(([v]) => v === claudeModel) ? "" : claudeModel });
  custom.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(custom.value.trim()); });
  const other = el("div", { class: "row nowrap", hidden: !custom.value }, custom, el("button", { class: "btn small", onclick: () => pick(custom.value.trim()) }, "Use"));
  const effort = seg(EFFORTS, a.effort || "", async (v) => {
    try { await api("POST", `/api/agents/${a.id}/model`, { effort: v || null }); toast(v ? `${v[0].toUpperCase() + v.slice(1)} effort from the next message` : "Back to the default effort"); }
    catch (e) { toast(e.message, true); }
    await loadState();
  }, "small wrap");
  const servers = STATE.model_servers || [];
  return el("div", { class: "stack tight model-pop" },
    el("div", { class: "row tight" }, el("strong", { class: "mp-title" }, "Model"), el("span", { class: "muted" }, a.name)),
    modelSection("cloud", el("span", { class: "mp-sec label grow" }, "Cloud"),
      ...MODELS.map(([v, label, desc]) => row(!local && claudeModel === v, label, () => pick(v), el("span", { class: "muted small grow ellipsis" }, desc))),
      other.hidden ? el("button", { class: "link small mp-other", onclick: (e) => { other.hidden = false; e.target.remove(); custom.focus(); } }, "Other model id…") : "",
      other),
    servers.length ? localModels(a, servers, row, pick) : isOwner() ? el("button", { class: "link small mp-other", onclick: () => { closePop(); openSettings("models"); } },
      "Use a model you run yourself: add its server in Settings → Local models") : "",
    // Claude Code loads MCP tools only when needed on Claude's models, but sends all of them with every message on yours
    local && (a.mcp || []).length ? el("div", { class: "note-box warn small pretty" },
      `On your own models, every MCP server sends all its tools with every message (${a.mcp.join(", ")}). `
      + "One with many tools can fill the context by itself. Turn off the ones this agent doesn't need under MCP.") : "",
    el("div", { class: "sep" }),
    modelSection("effort", el("span", { class: "row tight grow" }, el("strong", {}, "Effort"), el("span", { class: "muted small" }, "how much it thinks first")), effort,
      local ? el("div", { class: "muted small hint" }, "Claude Code sends high unless you pick one. Test in Settings → Local models says which ones your model takes.") : ""),
    el("div", { class: "muted small hint" }, "Applies from the next message. A running task keeps its model."));
}
// From a local model to Claude's: continuing the conversation sends all of it, and what the agent read
// in your files with it, to Anthropic.  Asked once, when the conversation the next message goes into
// ran on a model of yours.  False: leave the model as it is.
async function leavingLocal(a) {
  const c = composerState(a.id), all = conversationsOf(a), current = all[all.length - 1];
  const conv = c.resume ? (a.tasks || []).filter((t) => t.session === (a.tasks || []).find((x) => x.id === c.resume)?.session)
    : c.cont && current ? (a.tasks || []).filter((t) => t.session === current.sid) : [];
  const ran = conv.find((t) => typeof t.model === "string" && t.model.includes("/"));  // "server/model": one of yours
  if (!ran) return true;
  const how = await choiceModal("Send this conversation to Claude?",
    `It ran on ${ran.model.split("/").slice(1).join("/")}, a model on your own server. Continuing it on one of Claude's models sends `
    + "everything said so far to Anthropic, including what the agent read in your files.",
    [["continue", "Continue and send it"], ["new", "Start a new conversation", true]]);
  if (how === "new") { Object.assign(c, { cont: false, resume: null }); SIGS.composer = null; }
  return how !== null;
}
// The models of your model servers, asked when the menu opens (the panel keeps the list a minute).
// One "Local" group, each model with its server's name. A server that's offline is left out, unless
// the agent's model is on it.
function localModels(a, servers, row, pick) {
  const box = el("div", { class: "stack tight" }, el("span", { class: "muted small" }, "Asking your model servers…"));
  api("GET", "/api/models").then((r) => {
    const rows = servers.flatMap((name) => {
      const got = r.servers?.[name] || { models: [], error: "not in Settings any more" }, small = got.small || {};
      if (got.error) {
        return a.model_server === name ? [row(true, a.model, () => {}, el("span", { class: "dim small grow" }, name), el("span", { class: "err small", title: `Can't list its models: ${got.error}` }, "Offline"))] : [];
      }
      return got.models.map((m) => row(a.model_server === name && a.model === m, m, () => pick(m, name), el("span", { class: "dim small grow" }, name),
        small[m] ? el("span", { class: "warn small", title: "Too little for an agent. See Settings → Local models." }, `${Math.round(small[m] / 1024)}k context`) : ""));
    });
    box.replaceChildren(modelSection("local", el("span", { class: "mp-sec label grow" }, "Local"),
      rows.length ? el("div", { class: "stack0 model-list" }, ...rows) : el("span", { class: "muted small" }, "None of your model servers answer right now.")));
  }).catch((e) => box.replaceChildren(el("span", { class: "err" }, e.message)));
  return box;
}
function mcpPop(a) {
  return el("div", { class: "stack" }, el("h4", {}, `MCP servers for ${a.name}`),
    el("div", { class: "muted" }, "Changes apply live. Remote hosts are added to this agent's allowlist."), ...mcpControls(a, closePop),
    ...(isResearch(a) ? [] : [el("div", { class: "sep" }), el("h4", {}, "Plugins"),
      el("div", { class: "muted pretty" }, "Its VM installs them before the next task. " + (isOwner() ? "Type /plugin add owner/repo in the message box to add one." : "Add plugins on your PC.")),
      ...pluginControls(a), el("div", { class: "sep" }), el("h4", {}, "Skills"),
      el("div", { class: "muted pretty" }, "Your own skills. Its VM adds them before the next task."), ...skillControls(a)]));
}
async function setSkills(a, skills) {
  try { await api("POST", `/api/agents/${a.id}/skills`, { skills }); toast("Skills saved. Its VM adds or removes them before the next task."); }
  catch (e) { toast(e.message, true); }
  await loadState();
}
// Your skills, each with a switch
function skillControls(a) {
  const lib = STATE.skills || {}, box = [];
  if (!Object.keys(lib).length) box.push(el("div", { class: "muted" }, isOwner() ? "No skills yet. Upload one on Claude policy." : "No skills yet. Upload them on your PC."));
  for (const [n, info] of Object.entries(lib)) {
    const on = (a.skills || []).includes(n);
    box.push(el("div", { class: "row nowrap plugin-row" },
      el("div", { class: "grow stack0" }, el("strong", {}, n), el("span", { class: "muted small clamp2", title: info.description }, info.description)),
      sw(on, (v) => setSkills(a, v ? [...(a.skills || []), n] : (a.skills || []).filter((x) => x !== n)))));
  }
  if (isOwner()) box.push(el("button", { class: "link", onclick: () => { closePop(); go("policy"); } }, Object.keys(lib).length ? "Edit your skills" : "Upload a skill"));
  return box;
}
async function setPlugins(a, plugins) {
  try { await api("POST", `/api/agents/${a.id}/plugins`, { plugins }); toast("Plugins saved. Its VM installs or removes them before the next task."); }
  catch (e) { toast(e.message, true); }
  await loadState();
}
// The library's plugins, each with a switch and what happened when the VM installed it
function pluginControls(a) {
  const lib = STATE.plugins || {}, box = [];
  if (!Object.keys(lib).length) box.push(el("div", { class: "muted" }, "The library has no plugins yet."));
  for (const [n, info] of Object.entries(lib)) {
    const on = (a.plugins || []).includes(n), st = (a.plugin_state || {})[n];
    const status = !on ? "" : !st ? el("span", { class: "muted" }, "Installs before the next task")
      : st.state === "installed" ? el("span", { class: "ok" }, "Installed")
      : st.state === "failed" ? el("span", { class: "err pretty" }, `Failed: ${st.message || "no reason given"}`)
      : info.accepted ? el("span", { class: "muted" }, "Its install command is allowed. It installs before the next task.")
      : el("div", { class: "stack tight" }, el("span", { class: "warn pretty" }, "It installs by running this command in the VM:"), el("pre", { class: "block" }, st.command || "(not shown)"),
        isOwner() ? el("button", { class: "btn small", onclick: () => acceptPluginCommand(n, st.sha256) }, "Allow this command") : el("span", { class: "muted" }, "Allow it on your PC."));
    box.push(el("div", { class: "row nowrap plugin-row" },
      el("div", { class: "grow stack tight" }, el("strong", {}, n), el("span", { class: "muted mono small ellipsis" }, info.marketplace), status),
      sw(on, (v) => setPlugins(a, v ? [...(a.plugins || []), n] : (a.plugins || []).filter((x) => x !== n)))));
  }
  if (isOwner()) box.push(el("button", { class: "link", onclick: () => { closePop(); go("policy"); } }, "Edit the plugin library"));
  return box;
}
async function acceptPluginCommand(name, sha256) {
  try { await api("POST", `/api/plugins/${name}/accept`, { sha256 }); toast(`Allowed. ${name} installs before the next task.`); }
  catch (e) { toast(e.message, true); }
  await loadState();
}
// the same switches in the MCP popover by the message box and on the Sandbox tab
function mcpControls(a, after = () => {}) {
  const lib = STATE.mcp_servers || {}, box = [];
  if (isResearch(a)) box.push(el("div", { class: "note-box warn" }, "This agent browses the open web, so any MCP key you give it is exposed to that risk."));
  if (!Object.keys(lib).length) box.push(el("div", { class: "muted" }, "The library has no MCP servers yet."));
  const statusBox = {};
  for (const [n, info] of Object.entries(lib)) {
    const on = (a.mcp || []).includes(n);
    const st = el("span", { class: "muted" });
    statusBox[n] = st;
    if (!isOwner() && info.secrets.length) st.textContent = "Keys are set on your PC";
    box.push(el("div", { class: "row nowrap" },
      el("div", { class: "grow stack tight" }, el("strong", {}, n),
        el("span", { class: "muted" }, [info.type + (info.hosts.length ? " to " + info.hosts.join(", ") : ""), info.secrets.length ? "needs " + info.secrets.join(", ") : ""].filter(Boolean).join(", ")), st),
      sw(on, (v) => setMcp(a, v ? [...(a.mcp || []), n] : (a.mcp || []).filter((x) => x !== n)).then(after))));
  }
  // which keys are already set (status only, never values), fetched once per view, not on every redraw
  // (a paired device can't read settings, so it gets a calm line instead)
  const settings = !isOwner() ? Promise.resolve(null) : AV ? (AV.settings ||= api("GET", "/api/settings").catch(() => null)) : Promise.resolve(null);
  settings.then((s) => {
    if (!s) return;
    for (const [n, info] of Object.entries(lib)) {
      const missing = info.secrets.filter((v) => (s.secrets["mcp:" + v] || "missing").includes("missing"));
      statusBox[n].textContent = !info.secrets.length ? "" : missing.length ? `Will ask for ${missing.join(", ")}` : `${info.secrets.join(", ")} set`;
      statusBox[n].className = !missing.length ? "ok" : "warn";
    }
  }).catch(() => {});
  // adding writes the policy, so a paired device doesn't get it (the server refuses it too)
  if (isOwner()) box.push(el("div", { class: "row" }, el("button", { class: "link", onclick: () => {
    closePop();
    mcpAddDialog(`Saved to your MCP library and turned on for ${a.name}. Applies live.`, (n) => setMcp(a, [...(agentById(a.id)?.mcp || []), n]));
  } }, "+ Add a server"), el("button", { class: "link", onclick: () => { closePop(); go("policy"); } }, "Edit the MCP library")));
  return box;
}

// ---- tasks: what runs now, what runs next, and what ran, by day ----
const FAIL_MAX = 160;
const failReason = (t) => (t.error || "").trim() || (t.agent_rc ? `exit code ${t.agent_rc}` : "");
// SIGKILL: the runner's time limit or the kernel's out-of-memory killer; the runner can't tell which
const killed = (t) => t.timed_out || (t.agent_rc === -9 && !t.by_you && !["running", "queued", "cancelled"].includes(t.state));
function taskStateText(t) {  // "Failed: {reason}" with the reason's first line, cut short
  if (t.timed_out) return "Stopped: the time limit ended it";  // the panel ended it, the VM didn't
  if (killed(t)) return "Stopped (killed: time limit or out of memory)";
  if (t.state !== "failed") return t.state[0].toUpperCase() + t.state.slice(1) + (t.agent_rc ? ` (exit code ${t.agent_rc})` : "");
  const why = failReason(t), line = why.split("\n")[0];
  return !why ? "Failed" : "Failed: " + (line.length > FAIL_MAX ? line.slice(0, FAIL_MAX - 1).trimEnd() + "…" : line);
}
const errorBelow = (t) => !!t.error && (t.state !== "failed" || taskStateText(t) !== "Failed: " + failReason(t));  // not all of it fits in the state
const TASK_OPEN = new Set();  // tasks whose whole text is shown
function taskStatus(t) {  // [words, class]
  if (t.state === "queued") return ["Queued", "muted"];
  if (t.state === "running") return ["Running…", "accent-fg"];
  if (t.state === "cancelled") return ["Cancelled", "muted"];
  if (t.state === "delivered") return [`Sent into task ${t.into} while it ran` + ({ refused: ". Claude Code refused it.",
    cancelled: ". Claude Code cancelled it.", discarded: ". The session ended before Claude read it." }[t.lost] || ""), t.lost ? "warn" : "ok"];
  if (t.time_limit_stop) return ["Not sent: the time limit stopped the VM", "warn"];
  if (t.timed_out) return ["Stopped: the time limit ended it", "warn"];  // the panel ended it, the VM didn't
  if (t.state === "interrupted") return [t.by_you ? "Stopped by you" : "Cut off when the VM stopped", "muted"];
  if (t.agent_rc === -9) return ["Stopped (killed: time limit or out of memory)", "warn"];
  if (t.state === "done" && !t.error && !t.agent_rc) return ["Done", "ok"];
  return [t.error ? `Failed: ${t.error}` : t.agent_rc != null ? `Failed (exit code ${t.agent_rc})` : "Failed", "err"];
}
const utcDay = (iso) => new Date(iso).toISOString().slice(0, 10);
function dayLabel(day, now = new Date()) {
  const today = now.toISOString().slice(0, 10), yesterday = new Date(now - 864e5).toISOString().slice(0, 10);
  if (day === today) return "Today";
  if (day === yesterday) return "Yesterday";
  return new Date(day + "T00:00:00Z").toLocaleDateString("en", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC",
    ...(day.slice(0, 4) !== today.slice(0, 4) ? { year: "numeric" } : {}) });
}
// Running, Up next (in queue order) and the rest by the day you sent them, newest first
function taskGroups(a) {
  const queue = a.queue || [], at = (t) => (queue.includes(t.id) ? queue.indexOf(t.id) : queue.length);
  const running = a.tasks.filter((t) => t.state === "running");
  const queued = a.tasks.filter((t) => t.state === "queued").sort((x, y) => at(x) - at(y));
  const days = [];
  for (const t of [...a.tasks].reverse()) {
    if (t.state === "running" || t.state === "queued") continue;
    const d = utcDay(t.created);
    if (days.at(-1)?.day !== d) days.push({ day: d, tasks: [] });
    days.at(-1).tasks.push(t);
  }
  return { running, queued, days };
}
async function wholeText(a, t) {  // the task record keeps the first 2,000 characters
  if (!t.full_message) return t.instructions;
  if (MSG_OPEN[t.id] === undefined) MSG_OPEN[t.id] = (await api("GET", `/api/agents/${a.id}/tasks/${t.id}/message`)).text;
  return MSG_OPEN[t.id];
}
async function runAgain(a, t) {
  let text;
  try { text = await wholeText(a, t); } catch (e) { toast(e.message, true); return; }
  const c = composerState(a.id);
  c.draft = c.draft.trim() ? `${text}\n\n${c.draft}` : text;
  switchTab("chat");
  AV?.c?.ta.focus();
  toast("Copied into the message box");
}
async function removeQueued(a, t) {
  try { await api("POST", `/api/agents/${a.id}/tasks/${t.id}/cancel`, {}); toast("Removed from the queue"); }
  catch (e) { toast(e.message, true); }
  await loadState();
}
async function moveUp(a, t) {
  try { await api("POST", `/api/agents/${a.id}/tasks/${t.id}/move-up`, {}); }
  catch (e) { toast(e.message, true); }
  await loadState();
}
function taskRow(a, t, first) {
  const open = TASK_OPEN.has(t.id), [label, cls] = taskStatus(t);
  const text = open && MSG_OPEN[t.id] !== undefined ? MSG_OPEN[t.id] : t.instructions;
  const long = t.full_message || text.length > 180 || text.split("\n").length > 3;
  const toggle = async () => {
    if (!open) { try { await wholeText(a, t); } catch (e) { toast(e.message, true); return; } }
    if (open) TASK_OPEN.delete(t.id); else TASK_OPEN.add(t.id);
    SIGS.tasks = null; renderTasks(agentById(a.id) || a);
  };
  const prn = (t.pr || "").match(/\/pull\/(\d+)$/);
  return el("div", { class: "task-row" }, el("span", { class: "when" }, (t.created || "").slice(11, 16)),
    el("div", { class: "stack tight task-body" },
      el("div", { class: "task-text" + (open ? "" : " clamp") }, text),
      el("div", { class: "task-meta" },
        el("span", { class: cls + " task-state" }, label),
        t.attachments?.length ? el("span", { class: "muted ellipsis" }, "Attached " + t.attachments.join(", ")) : "",
        t.branch && a.branch && t.branch !== a.branch ? el("span", { class: "mono muted" }, t.branch) : "",
        t.pr ? ghLink(t.pr, prn ? `PR #${prn[1]}` : "PR") : "",
        long ? el("button", { class: "link", "aria-expanded": String(open), onclick: toggle }, open ? "Show less" : "Show more") : "",
        el("span", { class: "grow" }),
        t.state === "queued" ? el("span", { class: "task-acts" },
          isOwner() ? el("button", { class: "link quiet", disabled: first, onclick: () => moveUp(a, t) }, "Move up") : "",
          el("button", { class: "link quiet danger-hover", onclick: () => removeQueued(a, t) }, "Remove"))
        : t.state !== "running" && !t.attach ? el("button", { class: "link quiet", onclick: () => runAgain(a, t) }, "Run again") : "")));
}
function taskGroup(title, sub, rows) {
  return el("section", { class: "task-group" }, el("div", { class: "sec-head" }, el("span", { class: "sec-title" }, title), sub ? el("span", { class: "sec-sub" }, sub) : ""),
    el("div", { class: "task-list" }, rows));
}
function renderTasks(a) {
  const today = new Date().toISOString().slice(0, 10);
  if (!changed("tasks", [a.id, a.tasks, a.queue, a.branch, a.repo, isOwner(), [...TASK_OPEN], today])) return;
  if (!a.tasks.length) { AV.pane.replaceChildren(el("div", { class: "dashed-empty" }, "No tasks yet. Send a message in Chat to start one.")); return; }
  const { running, queued, days } = taskGroups(a), parts = [];
  parts.push(el("div", { class: "note-line" }, a.repo && a.branch ? `Times in UTC. Branch ${a.branch} unless noted.` : "Times in UTC."));
  if (running.length) parts.push(taskGroup("Running", running[0].started ? `since ${running[0].started.slice(11, 16)}` : "", running.map((t) => taskRow(a, t))));
  if (queued.length) parts.push(taskGroup("Up next", `${queued.length} message${queued.length === 1 ? " runs" : "s run"} in this order after the current task`,
    queued.map((t, i) => taskRow(a, t, i === 0))));
  for (const d of days) {
    const nKilled = d.tasks.filter((t) => t.agent_rc === -9 || t.timed_out).length;
    parts.push(taskGroup(dayLabel(d.day), `${d.tasks.length} task${d.tasks.length === 1 ? "" : "s"}` + (nKilled ? `, ${nKilled} killed` : ""),
      paged(`tasks:${a.id}:${d.day}`, d.tasks, (t) => taskRow(a, t), { step: 20 })));
  }
  AV.pane.replaceChildren(...parts);
}

// ---- files: the browser, Export… and Attach repository… ----
const EXPORTS_SEEN = {};  // export id -> state last shown
// git runs commands named in a repository's config, so the panel never copies one out of a VM
const leftOut = (n) => `Left out ${n} Git folder${n === 1 ? "" : "s"}.`;
const EXPORT_WHAT = { repo: "The repository checkout, without .git,", scratch: "The scratch folder, without Git folders,", outbox: "The outbox" };
function renderFiles(a) {
  const up = a.running && ["ready", "busy"].includes(a.state);
  if (changed("files", [a.id, a.profile, a.repo, up, STATE.repos, isPhone()])) {
    const exp = !isOwner() ? el("span", { class: "muted small nowrap" }, "Export: only on your PC")
      : el("button", { class: "btn small primary", "aria-haspopup": "dialog", onclick: () => openPop(exp, exportPop(agentById(a.id) || a), { width: 360 }) },
        isPhone() ? "Export" : "Export…");
    const attach = el("button", { class: "btn small accent", onclick: () => up ? attachDialog(agentById(a.id) || a, attach) : toast("Start the agent first. The files are inside its VM.", true) },
      isPhone() ? "Attach…" : "Attach repository…");
    AV.pane.replaceChildren(fileBrowser(a, up, exp, !isResearch(a) && !a.repo ? el("div", { class: "fb-strip" },
      isPhone() ? el("span", { class: "grow muted" }, "No repository yet. Attach one to put these files on a branch.")
        : el("div", { class: "grow stack none-gap" }, el("span", {}, "No repository yet"),
          el("span", { class: "muted" }, "These files live in ~/scratch. Attach a repository to put them on a branch with a draft PR. The conversation carries on.")),
      attach) : ""));
  }
  if (!changed("exports", [a.id, a.exports])) return;
  for (const e of a.exports || []) {  // say once when an export you watched finishes, and what was left out
    if (EXPORTS_SEEN[e.id] === "running" && e.state === "done") toast(`Exported ${plural(e.files, "file")}.` + (e.left_out ? ` ${leftOut(e.left_out)}` : ""));
    EXPORTS_SEEN[e.id] = e.state;
  }
}
// Copies what the browser shows (its source) into a new folder on this PC
function exportPop(a) {
  const up = a.running && ["ready", "busy"].includes(a.state), what = FB.what;
  const parts = [el("div", { class: "stack none-gap" }, el("span", { class: "title" }, "Copy to this machine"),
    el("span", { class: "muted small" }, `${EXPORT_WHAT[what]} into a new subfolder.`))];
  if (isOwner()) {
    const dest = el("input", { class: "input mono", placeholder: "/home/you/agent-exports", value: AV.dest || a.export_dest || "", spellcheck: false, "aria-label": "Host folder" });
    dest.addEventListener("input", () => { AV.dest = dest.value; });
    const go_ = el("button", { class: "btn primary", disabled: !up, onclick: async () => {
      try {
        const r = await api("POST", `/api/agents/${a.id}/export`, { what, dest: dest.value.trim() });
        EXPORTS_SEEN[r.export.id] = "running";  // a quick export may be done before the next refresh
        toast("Export started");
        closePop();
      } catch (e) { toast(e.message, true); }
      loadState();
    } }, "Export");
    parts.push(dest, el("div", { class: "small pretty " + (up ? "muted" : "warn") }, up
      ? "Copies into a new subfolder. Nothing is overwritten, links are dropped and nothing is marked executable. The files are agent-made: look before you run any of them."
      : "Start the agent to export. The files are inside its VM."), go_);
    if (["ephemeral", "memory"].includes(a.mode) && !isResearch(a)) {  // its disk goes when it stops
      const keep = sw(!!a.keep_outbox, async (on, b) => {
        try {
          await api("POST", `/api/agents/${a.id}/keep-outbox`, { dest: on ? dest.value.trim() : null });
          setSw(b, on);
          toast(on ? "Its outbox is kept when it stops" : "Its outbox goes with its disk again");
        } catch (e) { toast(e.message, true); }
        loadState();
      });
      parts.push(el("div", { class: "row nowrap keep-outbox" }, el("div", { class: "grow stack0" }, el("span", {}, "Keep its outbox when it stops"),
        el("span", { class: "muted small pretty" }, a.keep_outbox ? `Each stop copies ~/outbox into a new subfolder of ${a.keep_outbox}.`
          : "Before its disk is deleted, ~/outbox is copied into the folder above.")), keep));
    }
  }
  const list = [...(a.exports || [])].reverse();
  if (list.length) parts.push(el("div", { class: "muted small" }, "Recent exports"), el("div", { class: "list-card" },
    paged("exports:" + a.id, list, (e) => el("div", { class: "export-row" },
      el("div", { class: "row nowrap" }, el("span", { class: { running: "s-booting", done: "s-ready", failed: "s-failed" }[e.state] }, e.state === "running" ? "copying" : e.state),
        el("span", { class: "muted small grow ellipsis" }, `${fmtTime(e.created)} · ${e.what}`),
        el("span", { class: "muted small nowrap" }, e.files !== undefined ? `${plural(e.files, "file")}, ${fmtBytes(e.bytes)}` : "")),
      el("span", { class: "mono small fg-3 ellipsis", title: e.path || e.dest }, e.path || e.dest),
      e.error ? el("span", { class: "err small" }, e.error) : "", e.left_out ? el("span", { class: "warn small" }, leftOut(e.left_out)) : ""), { step: 5 })));
  return el("div", { class: "stack tight export-pop" }, parts);
}
// Pick an empty repository the token can push to. GitHub says which ones are (from your PC);
// a paired phone gets the configured list unchecked, and the panel checks the pick either way.
// A dialog on the PC, a bottom sheet on phones.
function attachDialog(a, anchor) {
  let pick = null;
  const rows = el("div", { class: "radio-list" }, el("div", { class: "muted pad" }, isOwner() ? "Checking the repositories on GitHub…" : ""));
  const title = el("input", { class: "input", placeholder: "PR title (optional)", maxlength: 200, "aria-label": "PR title" });
  const sheet = isPhone();
  let back = null;
  const close = () => { if (sheet) { closePop(); return; } back.remove(); document.removeEventListener("keydown", esc, true); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const go_ = el("button", { class: "btn primary", disabled: true, onclick: async () => {
    go_.disabled = true;
    try { await api("POST", `/api/agents/${a.id}/attach-repo`, { repo: pick, pr_title: title.value }); toast(`Attaching ${pick}`); close(); }
    catch (e) { toast(e.message, true); go_.disabled = false; }
    loadState();
  } }, "Attach and open draft PR");
  const draw = (repos) => {
    rows.replaceChildren(...(repos.length ? repos.map((r) => {
      const b = el("button", { class: "radio-row" + (pick === r.repo ? " on" : ""), role: "radio", "aria-checked": String(pick === r.repo), disabled: !r.ok,
        onclick: () => { pick = r.repo; go_.disabled = false; draw(repos); } },
        el("span", { class: "radio" }), el("span", { class: "stack none-gap grow" }, el("span", { class: "mono ellipsis" }, r.repo),
          r.reason ? el("span", { class: "small " + (r.ok ? "muted" : "warn") }, r.reason) : ""),
        el("span", { class: "muted small nowrap" }, r.when || ""));
      return b;
    }) : [el("div", { class: "muted pad" }, "No repositories are set up yet.")]));
  };
  const parts = [el("div", { class: "modal-top" }, el("h3", {}, `Attach a repository to ${a.name}`),
      el("div", { class: "muted small pretty" }, "The scratch files go onto a new branch on top of the default branch, with a draft PR. Use an empty repository with a README.")),
    rows,
    el("div", { class: "modal-pad muted small" }, "Don't see it? Give the bot token access on GitHub, then ",
      isOwner() ? el("button", { class: "link", onclick: () => { close(); openSettings("github"); } }, "refresh in Settings → GitHub access") : "refresh in Settings → GitHub access on your PC", "."),
    el("div", { class: "modal-pad" }, title),
    el("div", { class: "actions modal-foot" }, el("button", { class: "btn ghost", onclick: close }, "Cancel"), go_)];
  if (sheet) openPop(anchor, el("div", { class: "stack tight attach-sheet", role: "dialog", "aria-label": `Attach a repository to ${a.name}` }, parts));
  else {
    back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } },
      el("div", { class: "modal wide flush-modal", role: "dialog", "aria-modal": "true", "aria-label": `Attach a repository to ${a.name}` }, parts));
    document.addEventListener("keydown", esc, true);
    document.body.append(back);
  }
  const plain = () => (STATE.repos || []).map((repo) => ({ repo, ok: true, reason: "" }));
  if (!isOwner()) { draw(plain()); return; }
  api("GET", "/api/repos/attachable").then((r) => draw(r.repos.map((x) => ({ ...x, when: x.commits > 1 ? updatedText(x.pushed_at) : createdText(x.created_at) }))))
    .catch((e) => { draw(plain()); toast(`GitHub couldn't be asked: ${e.message}`, true); });
}
const shortDay = (iso) => {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const w = dayLabel(d.toISOString().slice(0, 10));
  return /^(Today|Yesterday)$/.test(w) ? w.toLowerCase() : d.toLocaleDateString("en", { month: "short", day: "numeric", timeZone: "UTC" });
};
const createdText = (iso) => (shortDay(iso) ? `created ${shortDay(iso)}` : "");
const updatedText = (iso) => (shortDay(iso) ? `updated ${shortDay(iso)}` : "");

// ---- logs ----
function mountLogs() {
  AV.log = el("div", { class: "log-pane no-time", role: "log", "aria-live": "off", "data-empty": LOG_EMPTY[AV.logWhich] });  // no time column until a line has a time
  AV.logOffset = 0; AV.logCarry = "";
  const follow = sw(AV.follow, (on, b) => { AV.follow = on; setSw(b, on); if (on) AV.log.scrollTop = AV.log.scrollHeight; });
  AV.body.append(el("div", { class: "logs-wrap" },
    el("div", { class: "row logs-bar" }, seg([["agent", "Agent"], ["console", "Console"], ["egress", "Network"]], AV.logWhich, (v) => {
      AV.logWhich = v; AV.logOffset = 0; AV.logCarry = ""; AV.log.textContent = "";
      AV.log.classList.add("no-time");
      AV.log.classList.remove("loaded");
      AV.log.dataset.empty = LOG_EMPTY[v];
      pollLog();
    }), el("span", { class: "grow" }),
      el("button", { class: "inert-note", type: "button", title: INERT_WHY, onclick: () => toast(INERT_WHY) }, "Plain text, nothing clickable"),
      el("span", { class: "follow row nowrap" }, el("span", { class: "muted" }, "Follow"), follow)),
    AV.log));
}
const LOG_EMPTY = { agent: "Nothing logged yet.", console: "No console output yet.", egress: "No network decisions logged yet." };
const INERT_WHY = "Logs are plain text: links, escape codes and control characters are shown, never run or followed.";
// The time a line was written, from the runner's "HH:MM:SSZ " or a network decision's "ts".
// The serial console has none: its lines stay exactly as the VM wrote them.
function logTime(line, which) {
  if (which === "console") return ["", line];
  if (which === "egress") return [(/"ts":\s*"\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)/.exec(line) || [])[1] || "", line];
  const m = /^(\d\d:\d\d:\d\d)Z /.exec(line);
  return m ? [m[1], line.slice(m[0].length)] : ["", line];
}
// Colored logs: each line is tinted by what it is, and ANSI color codes (SGR only) become one of
// 16 fixed classes. Every other escape or control character is shown as a mark (marked()), as the
// file holds it; the text is only ever inserted as text.
const SGR = /\x1b\[([0-9;]*)m/g;
function lineClass(line, which) {
  if (which === "egress") return /"action":\s*"deny"/.test(line) ? "l-err" : /"action":\s*"allow"/.test(line) ? "l-ok" : "";
  if (/selftest PASS/.test(line)) return "l-ok";
  if (/selftest FAIL|^\S*\s?!!|\b(Traceback|ERROR|FAILED)\b/.test(line)) return "l-err";
  if (/^=====|^\S*\s?\[done/.test(line)) return "l-dim";
  if (/^\S*\s?(\$ |=== |> )/.test(line)) return "l-cmd";
  return "";
}
function logLine(raw, which) {
  const [time, line] = logTime(raw.replace(/\r$/, ""), which);  // and the end of a CRLF
  const box = el("span", { class: "log-text" });
  let fg = null, bold = false, last = 0, m;
  const flush = (text) => {
    text = marked(text);
    if (text) box.append(fg || bold ? el("span", { class: [fg ? "a" + fg : "", bold ? "ab" : ""].join(" ").trim() }, text) : text);
  };
  SGR.lastIndex = 0;
  while ((m = SGR.exec(line))) {
    flush(line.slice(last, m.index));
    const codes = m[1] === "" ? [0] : m[1].split(";").map(Number);
    for (let k = 0; k < codes.length; k++) {
      const c = codes[k];
      if (c === 0) { fg = null; bold = false; }
      else if (c === 1) bold = true;
      else if (c === 22) bold = false;
      else if (c === 39) fg = null;
      else if ((c >= 30 && c <= 37) || (c >= 90 && c <= 97)) fg = c;
      else if (c === 38 || c === 48) k += codes[k + 1] === 5 ? 2 : codes[k + 1] === 2 ? 4 : 0;  // 256/true color: not shown
    }
    last = SGR.lastIndex;
  }
  flush(line.slice(last));
  return el("div", { class: "log-line " + lineClass(line.replace(SGR, ""), which) }, el("span", { class: "log-t" }, time), box);
}
async function pollLog() {
  if (!AV?.log) return;
  const id = AV.id, which = AV.logWhich;
  const off = AV.logOffset === 0 ? -200000 : AV.logOffset;  // start with the tail
  const r = await api("GET", `/api/agents/${id}/log?which=${which}&offset=${off}`);
  if (!AV || AV.id !== id || AV.logWhich !== which || !AV.log) return;
  if (r.text) {
    const text = (AV.logCarry || "") + r.text, lines = text.split("\n");
    AV.logCarry = lines.pop();  // a line split across two reads waits for the rest
    const frag = document.createDocumentFragment();
    for (const ln of lines) frag.append(logLine(ln, which));
    AV.log.append(frag);
    // a log whose lines have no times (the serial console never has any) starts at the left edge
    if (lines.some((ln) => logTime(ln, which)[0])) AV.log.classList.remove("no-time");
    while (AV.log.childNodes.length > 20000) AV.log.firstChild.remove();
    if (AV.follow) AV.log.scrollTop = AV.log.scrollHeight;
  }
  AV.logOffset = Math.max(r.offset, 1);
  AV.log.classList.add("loaded");
}

// ---- research (coding agent) ----
function renderResearch(a) {
  const researchers = STATE.agents_list.filter(isResearch);
  const ex = EXTRA[a.id] || { deliveries: [] };
  if (!changed("research", [a.id, a.research_agent, a.research_auto, a.requests, ex.deliveries, researchers.map((x) => [x.id, x.name]), AV.viewer,
    a.yolo_rounds, a.yolo_paused, STATE.yolo_rounds, AV.reqView])) return;
  const f = document.activeElement, typing = f && f === AV.round?.input;
  const sel = el("select", { class: "select", "aria-label": "Answered by" }, el("option", { value: "" }, "(none)"), researchers.map((r) => el("option", { value: r.id }, r.name)));
  sel.value = a.research_agent || "";
  const save = (auto) => api("POST", `/api/agents/${a.id}/link`, { research_agent: sel.value || null, auto })
    .then(loadState).catch((e) => toast(e.message, true));
  sel.addEventListener("change", () => save(a.research_auto && !!sel.value));
  const yolo = sw(a.research_auto, async (on, b) => {
    if (on && !(await confirmModal("Turn on YOLO mode?", "Questions are approved and untrusted web findings are forwarded without you reading them first.", "Turn on YOLO", true))) return;
    setSw(b, on); save(on);
  }, "large yolo");
  yolo.disabled = !a.research_agent;
  const main = [];
  const all = [...(a.requests || [])].reverse(), closed = (r) => ["forwarded", "dismissed"].includes(r.state);
  const open = all.filter((r) => !closed(r)), finished = all.filter(closed);
  const rview = AV.reqView === "done" ? "done" : "open", reqs = rview === "done" ? finished : open;
  main.push(el("div", { class: "row" }, el("span", { class: "sec-title grow" }, "Requests"),
    all.length ? seg([["open", `Open (${open.length})`], ["done", `Done (${finished.length})`]], rview, (v) => { AV.reqView = v; SIGS.research = null; renderResearch(a); }) : ""));
  if (!reqs.length) main.push(el("div", { class: "dashed-empty" }, !all.length
    ? "No research requests yet. When this agent needs something from the web, its question appears here for you to approve."
    : rview === "done" ? "Nothing forwarded or dismissed yet." : "No open questions from this agent."));
  main.push(...paged(`requests:${a.id}:${rview}`, reqs, (r) => {
    const acts = el("div", { class: "row" });
    if (["pending", "failed"].includes(r.state)) acts.append(
      el("button", { class: "btn small", onclick: () => review(`req:${a.id}:${r.id}`) }, "Review"),
      el("button", { class: "btn small", onclick: () => reqAction(a, r.id, "dismiss") }, "Dismiss"),
      el("button", { class: "btn small accent", onclick: () => reqAction(a, r.id, "approve") }, "Approve"));
    if (r.delivery && r.research_agent && r.state === "delivered") acts.append(  // the findings came back: the same three choices
      el("button", { class: "btn small", onclick: () => review(`fin:${r.research_agent}:${r.delivery}`) }, "Review"),
      el("button", { class: "btn small", onclick: () => archiveFindings(r.research_agent, r.delivery, "Dismissed") }, "Dismiss"),
      el("button", { class: "btn small accent", onclick: () => forward(r.research_agent, r.delivery, a.id, true) }, "Forward"));
    else if (r.delivery && r.research_agent) {
      const open = viewerOpen(r.research_agent, r.delivery);
      acts.append(el("button", { class: "btn small", onclick: () => { toggleViewer(r.research_agent, r.delivery, () => { SIGS.research = null; renderResearch(a); }); } }, open ? "Hide findings" : "View findings"));
    }
    return el("div", { class: "card stack tight" },
      el("div", { class: "row" }, el("strong", {}, r.name || r.id), el("span", { class: "tag " + ({ delivered: "ok", pending: "warn", failed: "err" }[r.state] || "") }, r.state),
        el("span", { class: "grow" }), el("span", { class: "muted" }, ago(r.created))),
      el("div", { class: "pretty" }, r.text.slice(0, 4000)), r.error ? el("div", { class: "err" }, r.error) : "", acts.childElementCount ? acts : "");
  }, { step: 10 }));
  const side = [el("span", { class: "sec-title" }, "Settings"), el("div", { class: "card set-card" + (a.research_auto ? " yolo-on" : "") },
    el("div", { class: "set-row stack-row" }, el("div", { class: "stack none-gap" }, el("span", { class: "title" }, "Answered by"),
      el("span", { class: "desc" }, a.research_agent ? "Web questions from this agent are answered by a separate open-web VM." : researchers.length ? "Pick a research agent to let this agent ask for web research." : "Create a research agent (New agent → Research) to let this agent ask for web research.")), sel),
    el("div", { class: "set-row" }, el("div", { class: "grow stack none-gap" }, el("span", { class: "title" }, "YOLO mode"),
      el("span", { class: "desc" }, !a.research_agent ? "Pick an agent in Answered by first." : "Auto-approve requests and forward results without review.")), yolo),
    a.research_auto ? roundLimitRow(a) : "")];
  AV.pane.replaceChildren(el("div", { class: "two-cols" }, el("div", { class: "col-main" }, main), el("div", { class: "col-side" }, side)),
    AV.viewer ? findingsViewer(...AV.viewer) : "");
  if (typing) AV.round.input.focus({ preventScroll: true });
}
// research.yolo_rounds is a setting for every agent: it's changed here through Settings (PC only)
function roundLimitRow(a) {
  const limit = STATE.yolo_rounds, used = a.yolo_rounds || 0;
  if (AV.round?.aid !== a.id) {
    const input = el("input", { class: "input round-in", type: "number", min: 1, max: 100, placeholder: "None", "aria-label": "Round limit" });
    input.addEventListener("change", async () => {
      const v = input.value.trim() === "" ? null : Math.max(1, Math.min(100, Math.round(+input.value)));
      try {
        const s = await api("GET", "/api/settings");
        s.config.research.yolo_rounds = v;
        await api("PUT", "/api/settings", { config: s.config });
        toast(v == null ? "No round limit, for every agent" : `Round limit ${v}, for every agent`);
      } catch (e) { toast(e.message, true); }
      await loadState();
    });
    AV.round = { aid: a.id, input };
  }
  const { input } = AV.round;
  if (document.activeElement !== input) input.value = limit ?? "";
  input.disabled = !isOwner();
  return el("div", { class: "set-row" }, el("div", { class: "grow stack none-gap" }, el("span", { class: "title" }, "Round limit"),
    a.yolo_paused ? el("span", { class: "desc warn" }, `Paused after ${limit} automatic rounds. New questions wait in the inbox until you message this agent.`)
      : el("span", { class: "desc" }, limit == null ? "No limit. Set one to pause for you after that many auto-approved rounds in a row."
        : `Pauses for you after ${limit} auto-approved rounds in a row (${used} of ${limit} used).`),
    el("span", { class: "desc muted" }, isOwner() ? "Applies to every agent." : "Applies to every agent. Changed on your PC.")), input);
}
async function reqAction(a, rid, action) {
  try { await api("POST", `/api/agents/${a.id}/requests/${rid}/${action}`, {}); toast(action === "approve" ? "Approved and sent to the research agent" : "Dismissed"); }
  catch (e) { toast(e.message, true); }
  await loadState(); pollAgent(true); pollInbox();
}
// Findings you read and don't want to pass on: they move to the research agent's Archive, and the question to Done
async function archiveFindings(raid, did, ok) {
  try { await api("POST", `/api/agents/${raid}/deliveries/${did}/dismiss`, {}); toast(ok); }
  catch (e) { toast(e.message, true); }
  await loadState(); pollAgent(true); pollInbox();
}
async function forward(raid, did, to, notify) {
  if (!to) return;
  try {
    const r = await api("POST", `/api/agents/${raid}/deliveries/${did}/forward`, { to, notify });
    toast(r.delivered_now ? `Forwarded to ${agentName(to)}'s /inbox` : `Queued. It's delivered when ${agentName(to)} starts.`);
  } catch (e) { toast(e.message, true); }
  await loadState(); pollAgent(true); pollInbox();
}
function findingsViewer(raid, did) {
  const body = el("div", {}, el("div", { class: "muted" }, "loading…"));
  api("GET", `/api/agents/${raid}/deliveries/${did}`).then((r) => {
    body.replaceChildren(...r.files.map((f) => el("div", {}, el("div", { class: "card-head mono warn" }, f.path), fileBody(f))));
  }).catch((e) => body.replaceChildren(el("div", { class: "err" }, e.message)));
  return el("div", { class: "card flush warn findings-view" }, el("div", { class: "card-head" }, el("span", { class: "grow" }, `Findings ${did} from ${agentName(raid)}`),
    el("span", { class: "warn-muted" }, "untrusted"), el("button", { class: "link", onclick: () => { AV.viewer = null; SIGS.research = null; SIGS.findings = null; VIEWS.agent.update(); } }, "Close")), body);
}

// ---- findings (research agent) ----
function renderFindings(a) {
  const coders = STATE.agents_list.filter((x) => !isResearch(x));
  const ex = EXTRA[a.id] || { deliveries: [] };
  if (!changed("findings", [a.id, ex.deliveries, coders.map((x) => [x.id, x.name]), AV.viewer, AV.findView])) return;
  // new findings stay in view; forwarded or dismissed ones move to the archive
  const done = (d) => d.forwarded_to.length || d.dismissed;
  const fresh = ex.deliveries.filter((d) => !done(d)), old = ex.deliveries.filter(done);
  const view = AV.findView === "archive" ? "archive" : "new", shown = view === "archive" ? old : fresh;
  const parts = [el("div", { class: "note-box" }, el("span", {}, "The agent writes findings to ", el("code", {}, "~/outbox/"),
      ". They appear here after each task. Read them before forwarding: it's untrusted text from the open web.")),
    el("div", { class: "row" }, seg([["new", `New (${fresh.length})`], ["archive", `Archive (${old.length})`]], view,
      (v) => { AV.findView = v; SIGS.findings = null; renderFindings(a); }))];
  if (!shown.length) parts.push(el("div", { class: "dashed-empty" }, view === "archive" ? "Nothing forwarded or archived yet."
    : ex.deliveries.length ? "Nothing new. Forwarded and archived findings are in Archive." : "No findings yet."));
  parts.push(...paged(`findings:${a.id}:${view}`, shown, (d) => {
    const to = el("select", { class: "select" }, coders.map((c) => el("option", { value: c.id }, c.name)));
    if (d.for_agent) to.value = d.for_agent;
    let notify = true;
    return el("div", { class: "card stack tight" },
      el("div", { class: "row" }, el("strong", {}, d.request_name || d.instructions.slice(0, 80)), el("span", { class: "grow" }),
        el("span", { class: "muted" }, `${d.files.length} file${d.files.length === 1 ? "" : "s"}, ${ago(d.created)}`)),
      d.forwarded_to.length ? el("div", { class: "muted" }, "forwarded to " + d.forwarded_to.map(agentName).join(", ")) : "",
      d.dismissed ? el("div", { class: "muted" }, "archived without forwarding") : "",
      d.left_out ? el("div", { class: "muted" }, leftOut(d.left_out)) : "",
      el("div", { class: "row find-acts" },
        el("button", { class: "btn small", onclick: () => { toggleViewer(a.id, d.id, () => { SIGS.findings = null; renderFindings(a); }); } }, viewerOpen(a.id, d.id) ? "Hide" : "View"),
        coders.length ? to : el("span", { class: "muted" }, "No coding agents"),
        el("label", { class: "row nowrap" }, el("span", { class: "muted" }, "Tell it to continue"), sw(true, (on, b) => { notify = on; setSw(b, on); })),
        el("span", { class: "grow" }),
        done(d) ? "" : el("button", { class: "btn small", onclick: () => archiveFindings(a.id, d.id, "Archived") }, "Archive"),
        el("button", { class: "btn small " + (done(d) ? "" : "accent"), disabled: !coders.length, onclick: () => forward(a.id, d.id, to.value, notify) },
          done(d) ? "Forward again" : "Forward")));
  }, { step: 10 }));
  if (AV.viewer) parts.push(findingsViewer(...AV.viewer));
  AV.pane.replaceChildren(...parts);
}

// View opens a delivery's findings below the list, and pressing it again closes them
function viewerOpen(raid, did) { return AV.viewer?.[0] === raid && AV.viewer?.[1] === did; }
function toggleViewer(raid, did, render) {
  AV.viewer = viewerOpen(raid, did) ? null : [raid, did];
  render();
  if (AV.viewer) AV.pane.querySelector(".findings-view")?.scrollIntoView({ block: "start", behavior: "smooth" });
}

// ---- inject widget (Start popover and New agent) ----
// scratchAllowed(): may this agent get a copy in ~/scratch?  onChange: told when it becomes ready or not.
function makeInjectWidget(scratchAllowed, { onChange = () => {}, mode = "copy", toScratch = true } = {}) {
  let manifest = null;
  const path = el("input", { class: "input mono grow", placeholder: "/absolute/path/to/folder", spellcheck: false });
  const browser = el("div", { class: "browser", hidden: true });
  const result = el("div", { class: "stack tight", hidden: true });
  const scanBtn = el("button", { class: "btn small", hidden: true, onclick: scan }, "Scan");
  const clearBtn = el("button", { class: "btn small ghost", hidden: true, onclick: clear }, "Clear");
  const node = el("div", { class: "stack tight" }, el("div", { class: "row nowrap" }, path,
    el("button", { class: "btn small", onclick: () => browse(path.value.trim() || STATE.home) }, "Browse"), scanBtn, clearBtn), browser, result);
  const showPathButtons = () => { scanBtn.hidden = clearBtn.hidden = !path.value.trim(); };  // once there's a path
  path.addEventListener("input", () => { manifest = null; result.hidden = true; showPathButtons(); onChange(); });
  function clear() { manifest = null; path.value = ""; result.hidden = true; browser.hidden = true; showPathButtons(); onChange(); }
  async function browse(p) {
    try {
      const r = await api("GET", "/api/fs?path=" + encodeURIComponent(p));
      browser.hidden = false;
      browser.replaceChildren(el("div", { class: "head" }, el("button", { class: "btn small", onclick: () => browse(r.parent || "/") }, "↑ Up"),
        el("code", { class: "grow ellipsis" }, r.path), el("button", { class: "btn small primary", onclick: () => { path.value = r.path; browser.hidden = true; manifest = null; result.hidden = true; showPathButtons(); onChange(); } }, "Use this folder")),
        el("ul", {}, r.items.map((it) => it.kind === "dir"
          ? el("li", { class: "dir", onclick: () => browse(r.path.replace(/\/$/, "") + "/" + it.name) }, "▸ " + it.name)
          : el("li", { class: "nodir" }, (it.kind === "symlink" ? "↪ " : "  ") + it.name))));
    } catch (e) { toast(e.message, true); }
  }
  async function scan() {
    manifest = null; result.hidden = true;
    try { manifest = await api("POST", "/api/inject/scan", { path: path.value.trim() }); render(manifest); }
    catch (e) { toast(e.message, true); }
    onChange();
  }
  let ack = false;
  function render(m) {
    ack = false;
    const warns = m.issues.filter((i) => i.severity === "warn"), infos = m.issues.filter((i) => i.severity !== "warn");
    const ackSw = sw(false, (on, b) => { ack = on; setSw(b, on); onChange(); });
    result.hidden = false;
    result.replaceChildren(
      el("div", { class: "muted pretty" }, `${m.root}: ${plural(m.file_count, "file")}, ${plural(m.dir_count, "folder")}, ${plural(m.symlink_count, "internal symlink")}, total ${fmtBytes(m.total_bytes)}. ` +
        `Secret content scan covered ${m.content_scanned_files} text files` + (m.content_scan_truncated ? " (truncated)." : ".")),
      seg([["copy", "One-time copy"], ...(STATE.allow_live_share ? [["share", "Live share"]] : [])], mode, (v) => { mode = v; }),
      el("div", { class: "muted" }, "Appears inside the VM at /inject (read-only). The original folder is never opened by the VM."),
      scratchAllowed?.() ? el("div", { class: "row nowrap" }, sw(toScratch, (on, b) => { toScratch = on; setSw(b, on); }),
        el("span", { class: "pretty" }, "Also copy it to ~/scratch so the agent can edit it. Your folder never changes.")) : "",
      warns.length ? el("div", { class: "warn" }, `⚠ ${plural(warns.length, "warning")} to review before injecting`) : "",
      ...warns.concat(infos).map((i) => el("div", { class: "issue " + i.severity }, el("code", {}, i.rel), ": ", i.detail)),
      m.needs_ack ? el("div", { class: "row nowrap" }, ackSw, el("span", {}, "I reviewed the warnings and still want to inject this folder")) : "",
      el("details", { class: "adv" }, el("summary", {}, "File list"), el("ul", { class: "files" },
        m.entries.map((e) => el("li", {}, (e.kind === "dir" ? "▸ " : e.kind === "symlink" ? "↪ " : "  ") + e.rel + (e.kind === "file" ? `  (${fmtBytes(e.size)})` : "") + (e.target ? ` -> ${e.target}` : ""))),
        m.entries_truncated ? el("li", { class: "dim" }, "… list truncated") : "")));
  }
  return {
    node, clear,
    ready: () => !!manifest && (!manifest.needs_ack || ack),
    scanned: () => !!manifest,
    scan: (p) => { path.value = p; showPathButtons(); return scan(); },
    spec: () => ({ manifest_id: manifest.id, mode, ack_warnings: ack, to_scratch: !!scratchAllowed?.() && toScratch }),
    // null = no inject; false = cancelled; object = inject spec
    async get() {
      if (!path.value.trim()) return null;
      if (!manifest) throw new Error("Scan the folder first (or Clear it).");
      if (manifest.needs_ack && !ack) throw new Error("Acknowledge the folder warnings first.");
      const scratch = !!scratchAllowed?.() && toScratch;
      const ok = await confirmModal("Inject this folder?", `Inject ${plural(manifest.file_count, "file")} (${fmtBytes(manifest.total_bytes)}) from\n${manifest.root}\n` +
        `as ${mode === "copy" ? "a one-time read-only copy" : "a LIVE read-only share"} at /inject` + (scratch ? ", with a working copy in ~/scratch?" : "?"), "Inject");
      if (!ok) return false;
      return { manifest_id: manifest.id, mode, ack_warnings: ack, to_scratch: scratch };
    },
  };
}

// A soft keyboard covers what you type into: bring the field back into view once it has opened
const keepFieldInView = () => {
  const f = document.activeElement;
  if (isPhone() && f?.matches?.("input, textarea, select")) f.scrollIntoView({ block: "nearest" });
};
window.addEventListener("focusin", () => setTimeout(keepFieldInView, 350));
window.visualViewport?.addEventListener("resize", keepFieldInView);
