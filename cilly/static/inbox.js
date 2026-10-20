// cilly UI: the inbox (everything waiting on you, across agents) and its review page.
// One-click actions in the popover are deliberately limited: "Allow" there means this
// agent only, findings always go through the review page (they are untrusted web text),
// and a research question is approved from the list only when you can see all of it.
"use strict";

const INBOX = { items: [], count: 0, loaded: false, filter: "" };
const KIND = { questions: ["Questions", "accent"], research: ["Research request", "accent"], network: ["Network", "err"], findings: ["Findings", "warn"],
  note: ["Project note", "ok"], device: ["Device", "lavender"] };
// Who an item is from, for the line above its title
function itemFrom(it) {
  if (it.kind === "device") return "A new device";
  if (it.kind === "note") return `${it.agent ? agentName(it.agent) : "An agent"} · ${it.project_name}`;
  return agentName(it.agent);
}
// The filter: everything, or one project's items (the chips only show projects that have some)
function inboxItems() { return INBOX.filter ? INBOX.items.filter((it) => it.project === INBOX.filter) : INBOX.items; }
function inboxFilter(rerender) {
  const ids = [...new Set(INBOX.items.map((it) => it.project).filter((p) => p && projectById(p)))];
  if (INBOX.filter && !ids.includes(INBOX.filter)) INBOX.filter = "";
  if (!ids.length) return "";
  return seg([["", "All"], ...ids.map((p) => [p, projectName(p)])], INBOX.filter, (v) => { INBOX.filter = v; rerender(); }, "small wrap inbox-filter");
}
// What Allow does on a blocked-site item: this agent only, or why it isn't offered
function allowNote(it) {
  if (it.kind !== "network" || it.fixable === false) return "";
  return el("div", { class: "muted small" }, canAllow(it) ? `Allow is for ${agentName(it.agent)} only.` : PORTS_NOTE);
}
const emptyInbox = () => el("div", { class: "empty-chat" }, "Nothing waiting on you.", el("div", { class: "muted" }, "Questions, blocked sites and findings from your agents show up here."));
const SHORT_Q = 600;  // longer questions are approved from the review page, where you see all of it

async function pollInbox() {
  try {
    const r = await api("GET", "/api/inbox");
    const items = isOwner() ? r.items : r.items.filter((it) => it.kind !== "device");  // a paired device can't approve devices
    Object.assign(INBOX, { items, count: r.count - (r.items.length - items.length), loaded: true });
  } catch (e) { return; }
  renderInboxBadge();
  checkInboxNotify();
  if (POP?.inbox) renderInboxList(POP.node.querySelector(".inbox-list"));
  if (UI.view === "review") VIEWS.review.update();
  if (UI.view === "inbox") VIEWS.inbox.update();
  if (UI.view === "agent") VIEWS.agent.update();  // the allow/deny bar above the message box
}
function renderInboxBadge() {
  const b = $("sb-inbox-count");
  if (!b) return;
  b.textContent = String(INBOX.count);
  b.hidden = !INBOX.count;
  $("sb-show-dot").hidden = !INBOX.count;
  const m = $("m-inbox-count");
  if (m) { m.textContent = String(INBOX.count); m.hidden = !INBOX.count; $("m-nav-inbox")?.setAttribute("aria-label", INBOX.count ? `Inbox, ${INBOX.count} waiting` : "Inbox"); }
}
function itemTitle(it) {
  if (it.kind === "questions") return it.questions.length === 1 ? it.questions[0].question : `${it.questions.length} questions for you`;
  if (it.kind === "device") return `Pair ${it.name}?`;
  if (it.kind === "note") return `Add a note to ${it.project_name}?`;
  if (it.kind === "network" && (it.fixable === false || !canAllow(it))) return `Blocked ${it.host}:${it.port}`;  // a phone can't allow that port
  if (it.kind === "network") return `Allow ${it.host}${it.port === 443 ? "" : ":" + it.port}?`;
  if (it.kind === "findings") return it.for_agent ? `Forward findings to ${agentName(it.for_agent)}?` : `Findings from ${agentName(it.agent)}`;
  return it.text.split("\n").find((l) => l.trim())?.slice(0, 140) || it.name;
}
// In the lists: a question short enough to read is shown in full, so its title doesn't repeat it
function shownInFull(it) { return it.kind === "research" && it.text.length <= SHORT_Q; }
function listTitle(it) {
  return shownInFull(it) ? `Send this question to ${it.research_agent ? agentName(it.research_agent) : "a research agent"}?` : itemTitle(it);
}
function itemSummary(it) {
  if (it.kind === "questions") return it.questions.map((q) => q.header).join(", ");
  if (it.kind === "device") return `Check that it shows the code ${pairCode(it.code)}`;
  if (it.kind === "note") return `${it.agent ? agentName(it.agent) : "An agent"} suggests it for every agent in ${it.project_name}.`;
  if (it.kind === "network") return `Blocked ${it.count} time${it.count > 1 ? "s" : ""}${it.reason ? ` (${it.reason})` : ""}`;
  if (it.kind === "findings") return `${plural(it.files.length, "file")} answering “${it.name || it.question.slice(0, 80)}”. Untrusted text from the open web.`
    + (it.left_out ? ` ${leftOut(it.left_out)}` : "");
  const ra = it.research_agent ? agentName(it.research_agent) : null;
  return `${agentName(it.agent)} wants to ask ${ra || "a research agent"} a web question${it.error ? `. The last try failed with ${it.error}` : ""}`;
}

// ---- popover ----
function openInbox() {
  if (isPhone()) { go("inbox"); return; }
  const node = el("div", { class: "inbox-pop" },
    el("div", { class: "row nowrap" }, el("strong", {}, "Inbox"), el("span", { class: "muted grow inbox-count" }), el("button", { class: "link", onclick: closePop }, "Close")),
    el("div", { class: "inbox-list" }));
  const anchor = $("sb-inbox");
  if (!openPop(anchor, node, { width: 420 })) return;
  // anchored to the right of the sidebar (or the left edge when it's hidden)
  const sb = $("sidebar"), sbVisible = sb.offsetWidth > 0;
  node.style.left = (sbVisible ? sb.getBoundingClientRect().right + 8 : window.innerWidth > 900 ? 56 : 12) + "px";
  node.style.top = "64px";
  POP.inbox = true;
  renderInboxList(node.querySelector(".inbox-list"));
  pollInbox();
}
function renderInboxList(list) {
  if (!list) return;
  const cnt = list.parentElement.querySelector(".inbox-count");
  cnt.textContent = INBOX.count ? `${INBOX.count} waiting on you` : "";
  if (!INBOX.loaded) { list.replaceChildren(el("div", { class: "muted" }, "Loading…")); return; }
  if (!INBOX.items.length) { list.replaceChildren(emptyInbox()); return; }
  const filter = inboxFilter(() => renderInboxList(list));
  list.replaceChildren(filter, ...paged("inbox", inboxItems(), (it) => {
    const [label, cls] = KIND[it.kind];
    const acts = el("div", { class: "row" }, it.kind === "questions" ? el("span", { class: "grow" })
      : el("button", { class: "link grow", onclick: () => review(it.key) }, "Review"));
    if (it.kind === "questions") {
      acts.append(el("button", { class: "btn small primary", onclick: () => answerAsk(it) }, "Answer"));
    } else if (it.kind === "research") {
      acts.append(el("button", { class: "btn small", onclick: () => dismissRequest(it) }, "Deny"));
      if (it.research_agent && it.text.length <= SHORT_Q) acts.append(el("button", { class: "btn small primary", onclick: () => approveRequest(it) }, "Approve"));
    } else if (it.kind === "device") {  // paired only from the review page, which shows the whole code
      acts.append(el("button", { class: "btn small", onclick: () => decideDevice(it, false) }, "Deny"));
    } else if (it.kind === "note") {
      if (isOwner()) acts.append(el("button", { class: "btn small", onclick: () => decideNote(it, false) }, "Deny"),
        el("button", { class: "btn small primary", onclick: () => decideNote(it, true) }, "Add note"));
      else acts.append(el("span", { class: "muted" }, "Add it from your PC"));
    } else if (it.kind === "network" && it.fixable === false) {
      acts.append(el("button", { class: "btn small", onclick: () => dismissDenial(it) }, "Dismiss"));
    } else if (it.kind === "network") {
      acts.append(el("button", { class: "btn small", onclick: () => dismissDenial(it) }, "Deny"),
        canAllow(it) ? el("button", { class: "btn small primary", onclick: () => allowHost(it, siteFor(it), "agent") }, allowLabel(it)) : "");
    } else {
      acts.append(el("button", { class: "btn small", onclick: () => dismissFindings(it) }, "Dismiss"));
    }
    return el("div", { class: "inbox-item" },
      el("div", { class: "row nowrap" }, el("span", { class: "tag " + cls }, label), el("span", { class: "muted ellipsis grow" }, itemFrom(it)),
        el("span", { class: "muted" }, ago(it.created))),
      el("div", { class: "title" }, listTitle(it)),
      allowNote(it),
      it.kind === "note" ? el("div", { class: "q-full" }, it.text)
        : it.kind === "research" && it.text.length <= SHORT_Q ? el("div", { class: "q-full" }, it.text) : el("div", { class: "muted ellipsis" }, itemSummary(it)),
      it.kind === "research" && it.text.length > SHORT_Q ? el("div", { class: "warn" }, "This question is long, so approve it from the review page, where you can read all of it.") : "",
      it.kind === "research" && !it.research_agent ? el("div", { class: "warn" }, "No research agent is linked yet. Pick one on the review page.") : "",
      acts);
  }));
}

// ---- actions ----
async function act(fn, ok) {
  try { await fn(); toast(ok); } catch (e) { toast(e.message, true); return false; }
  await Promise.all([loadState(), pollInbox()]);
  return true;
}
const approveRequest = (it, text) => act(() => api("POST", `/api/agents/${it.agent}/requests/${it.id}/approve`, text ? { text } : {}),
  `Approved and sent to ${it.research_agent ? agentName(it.research_agent) : "the research agent"}`);
const dismissRequest = (it) => act(() => api("POST", `/api/agents/${it.agent}/requests/${it.id}/dismiss`, {}), "Denied");
const dismissDenial = (it) => act(() => api("POST", `/api/agents/${it.agent}/denials/dismiss`, { host: it.host, port: it.port }),
  it.fixable === false ? `Dismissed ${it.host}` : `Denied ${it.host}`);
const decideNote = (it, add, text) => act(() => api("POST", `/api/projects/${it.project}/notes/${it.id}/${add ? "approve" : "dismiss"}`, text ? { text } : {}),
  add ? `Added to the ${it.project_name} notes` : "Note dismissed");
// only the owner gets device items (the server leaves them out for a paired device, and refuses its approve)
const decideDevice = (it, ok) => act(() => api("POST", `/api/devices/pending/${it.id}/${ok ? "approve" : "deny"}`, {}), ok ? `Paired ${it.name}` : "Pairing denied")
  .then((r) => { refreshPaired(); return r; });
const dismissFindings = (it) => act(() => api("POST", `/api/agents/${it.agent}/deliveries/${it.id}/dismiss`, {}), "Findings dismissed");
async function allowHost(it, host, scope) {
  if (scope === "global" && !(await confirmModal(`Allow ${host} for all coding agents?`, "It's added to the global egress allowlist.", "Allow everywhere"))) return false;
  return act(() => api("POST", `/api/agents/${it.agent}/allow`, { host, scope }), scope === "global" ? `Allowed ${host} everywhere` : `Allowed ${host} for ${agentName(it.agent)}`);
}

// ---- review page ----
function review(key) { closePop(); go("review", { reviewKey: key, reviewFrom: UI.view === "review" ? UI.reviewFrom : { view: UI.view, agentId: UI.agentId, tab: UI.tab } }); }
VIEWS.review = {
  mount(main) {
    const page = el("div", { class: "page" }), inner = el("div", { class: "page-inner review" });
    page.append(inner); main.append(page);
    VIEWS.review.p = { inner, key: null };
    if (!INBOX.loaded) pollInbox();
  },
  update() {
    const P = VIEWS.review.p;
    if (!P) return;
    const it = INBOX.items.find((x) => x.key === UI.reviewKey);
    if (P.key === UI.reviewKey && (it || P.handled)) return;  // don't rebuild under your hands
    const back = isPhone() ? el("button", { class: "link", onclick: () => go("inbox") }, "← Inbox")
      : el("button", { class: "link", onclick: () => { const f = UI.reviewFrom || { view: "new" }; go(f.view, f); setTimeout(openInbox); } }, "← Back to inbox");
    if (!it) {
      if (!INBOX.loaded) return;
      P.handled = true; P.key = UI.reviewKey;
      P.inner.replaceChildren(back, el("div", { class: "empty-chat" }, "This item has been handled."));
      return;
    }
    P.key = UI.reviewKey; P.handled = false;
    const [label, cls] = KIND[it.kind];
    const parts = [back,
      el("div", { class: "row" }, el("span", { class: "tag " + cls }, label), el("span", { class: "muted" }, `from ${it.kind === "device" ? "a new device" : itemFrom(it)}, ${ago(it.created)}`)),
      el("h1", { class: "review-title" }, itemTitle(it)), el("div", { class: "muted pretty" }, itemSummary(it))];
    let actions;
    if (it.kind === "research") actions = reviewResearch(it, parts);
    else if (it.kind === "network") actions = reviewNetwork(it, parts);
    else if (it.kind === "note") actions = reviewNote(it, parts);
    else if (it.kind === "device") actions = reviewDevice(it, parts);
    else actions = reviewFindings(it, parts);
    const ctxAgent = it.kind === "findings" ? it.for_agent : it.agent;
    if (ctxAgent && agentById(ctxAgent)) parts.push(contextCard(ctxAgent));
    parts.push(el("div", { class: "row review-actions" }, el("span", { class: "grow" }), ...actions));
    P.inner.replaceChildren(...parts);
  },
};
function done(ok) { if (ok !== false) { VIEWS.review.p.handled = true; VIEWS.review.p.key = null; VIEWS.review.update(); } }

function reviewResearch(it, parts) {
  const ca = agentById(it.agent), researchers = (STATE.agents_list || []).filter((x) => x.profile === "research");
  const q = el("textarea", { class: "editor", rows: Math.min(14, Math.max(4, it.text.split("\n").length + 1)), spellcheck: true, value: it.text, "aria-label": "Question" });
  const sel = el("select", { class: "select" }, researchers.length ? researchers.map((r) => el("option", { value: r.id }, r.name)) : el("option", { value: "" }, "(no research agents)"));
  if (it.research_agent) sel.value = it.research_agent;
  parts.push(
    el("div", { class: "card flush" }, el("div", { class: "card-head" }, el("span", { class: "title grow" }, "Question that will leave the sandbox"),
      el("span", { class: "muted" }, it.name)),
      el("div", { class: "card-body stack tight" }, q, el("div", { class: "muted" }, "This goes to an open-web agent. Remove anything that shouldn't leave the sandbox."))),
    el("div", { class: "kv-grid review-kv" }, el("span", { class: "muted" }, "Answered by"), sel,
      el("span", { class: "muted" }, "Answer delivered"), el("span", {}, `Read-only to /inbox/ on ${ca ? ca.name : "the agent"}. Findings come back here for review first, unless YOLO is on.`),
      el("span", { class: "muted" }, "Request name"), el("code", {}, it.name || "(none)")));
  const approve = el("button", { class: "btn accent", disabled: !researchers.length, onclick: async () => {
    approve.disabled = true;
    if (sel.value && sel.value !== ca?.research_agent) {
      try { await api("POST", `/api/agents/${it.agent}/link`, { research_agent: sel.value, auto: !!ca?.research_auto }); }
      catch (e) { toast(e.message, true); approve.disabled = false; return; }
    }
    const text = q.value.trim();
    if (!text) { toast("The question is empty.", true); approve.disabled = false; return; }
    done(await approveRequest({ ...it, research_agent: sel.value }, text !== it.text.trim() ? text : null));
  } }, isPhone() ? "Approve and send" : "Approve");  // the phone's full-width bar has room to say it
  return [el("button", { class: "btn", onclick: async () => done(await dismissRequest(it)) }, "Deny"), approve];
}
// The code is the whole check, so it's all this page shows: no context and no note
function reviewDevice(it, parts) {
  parts.splice(3, 1, el("div", { class: "muted pretty" }, "A browser wants to use your agents and inbox."));
  parts.push(el("div", { class: "card stack pair-review" }, el("span", { class: "muted" }, "Check that the device shows this code"),
    el("span", { class: "pair-code mono" }, pairCode(it.code)),
    el("span", { class: "desc pretty" }, `If it doesn't match, deny it. ${PAIRED_SHORT} You can revoke it anytime in Settings, Devices.`), pairedLists()));
  return [el("button", { class: "btn", onclick: async () => done(await decideDevice(it, false)) }, "Deny"),
    el("button", { class: "btn accent", onclick: async () => done(await decideDevice(it, true)) }, "Pair")];
}
function reviewNote(it, parts) {
  const p = projectById(it.project);
  const t = el("textarea", { class: "editor", rows: 3, value: it.text, "aria-label": "Note", maxlength: 500 });
  parts.push(
    el("div", { class: "card flush" }, el("div", { class: "card-head" }, el("span", { class: "title grow" }, "The suggested note"),
      el("span", { class: "muted" }, "edit it before adding")), el("div", { class: "card-body stack tight" }, t,
      el("div", { class: "muted pretty" }, `Added notes go into CLAUDE.md for every coding agent in ${it.project_name}. Text from an agent, so check what it says.`))),
    p?.notes.length ? el("div", { class: "card stack tight" }, el("span", { class: "muted" }, "Notes the project has now"),
      ...p.notes.map((n) => el("div", { class: "note-row" }, n.text))) : "");
  if (!isOwner()) return [el("span", { class: "muted" }, "Add it from your PC.")];
  return [el("button", { class: "btn", onclick: async () => done(await decideNote(it, false)) }, "Deny"),
    el("button", { class: "btn accent", onclick: async () => done(await decideNote(it, true, t.value.trim() !== it.text.trim() ? t.value : null)) }, "Add note")];
}
const ROUTE_CODES = new Set(["route", "route_down"]);  // the route failed, not the allowlist
function reviewNetwork(it, parts) {
  if (it.fixable === false) {
    parts.push(el("div", { class: "card stack" },
      el("div", { class: "row" }, el("span", { class: "muted" }, `Port ${it.port}, ${it.count} attempt${it.count > 1 ? "s" : ""}, last at ${fmtTime(it.created)}`)),
      el("div", { class: "pretty" }, ROUTE_CODES.has(it.code)
        ? "The agent's route couldn't reach it, so nothing went out. With Tor, the next try goes through another exit and usually works. If it keeps failing, check the route in Settings → Network."
        : "Allowing the host wouldn't help. The address is on your own network or in Never reach, or the open web doesn't reach that port."),
      el("pre", { class: "block" }, (it.lines || []).join("\n") || "(none)")),
      el("div", { class: "muted pretty" }, "The agent's Logs tab shows what it was doing at the time."));
    return [el("button", { class: "btn", onclick: async () => done(await dismissDenial(it)) }, "Dismiss")];
  }
  let scope = "agent";
  const host = el("input", { class: "input mono", value: siteFor(it), spellcheck: false, title: "Edit to a wildcard like *.example.com", "aria-label": "Site" });
  const allow = el("button", { class: "btn accent", onclick: async () => done(await allowHost(it, host.value.trim(), scope)) });
  const portNote = el("div", { class: "muted" }, PORTS_NOTE);
  const follow = () => { const v = host.value.trim(); allow.textContent = siteLabel(v); allow.disabled = !mayAllow(v); portNote.hidden = mayAllow(v); };
  host.addEventListener("input", follow);
  follow();
  parts.push(
    el("div", { class: "card stack" },
      el("div", { class: "field" }, el("span", { class: "label" }, "Site (edit to a wildcard like *.example.com)"), host),
      el("div", { class: "row" }, el("span", { class: "muted" }, `Port ${it.port}, ${it.count} attempt${it.count > 1 ? "s" : ""}, last at ${fmtTime(it.created)}`)),
      portNote,
      isOwner() ? el("div", { class: "field" }, el("span", { class: "label" }, "Allow for"),
        seg([["agent", `Only ${agentName(it.agent)}`], ["global", "All agents (allowlist)"]], scope, (v) => { scope = v; }))
        : el("div", { class: "muted" }, `Allow is for ${agentName(it.agent)} only (all agents is on your PC).`)),
    el("div", { class: "field" }, el("span", { class: "label" }, "Denied requests (egress proxy log)"), el("pre", { class: "block" }, (it.lines || []).join("\n") || "(none)")),
    el("div", { class: "muted pretty" }, "The agent's Logs tab shows what it was doing at the time."));
  return [el("button", { class: "btn", onclick: async () => done(await dismissDenial(it)) }, "Deny"),
    allow];
}
function reviewFindings(it, parts) {
  const coders = (STATE.agents_list || []).filter((x) => x.profile !== "research");
  const to = el("select", { class: "select" }, coders.length ? coders.map((c) => el("option", { value: c.id }, c.name)) : el("option", { value: "" }, "(no coding agents)"));
  if (it.for_agent) to.value = it.for_agent;
  let notify = true;
  const body = el("div", {}, el("div", { class: "card-body muted" }, "loading…"));
  api("GET", `/api/agents/${it.agent}/deliveries/${it.id}`).then((r) => {
    body.replaceChildren(...r.files.map((f) => el("div", {}, el("div", { class: "card-head mono warn" }, f.path), fileBody(f))));
  }).catch((e) => body.replaceChildren(el("div", { class: "card-body err" }, e.message)));
  parts.push(
    it.question ? el("div", { class: "card stack tight" }, el("span", { class: "muted" }, "The question"), el("div", { class: "q-full" }, it.question)) : "",
    el("div", { class: "card flush warn" }, el("div", { class: "card-head" }, el("span", { class: "grow" }, "Untrusted findings from the open web. Review them before forwarding.")), body),
    el("div", { class: "card row nowrap forward-card" }, el("div", { class: "grow stack tight" }, el("span", { class: "title" }, "Forward to"),
      el("span", { class: "desc" }, "Delivered read-only to the agent's /inbox/.")),
      to, el("span", { class: "muted" }, "Tell it to continue"), sw(true, (on, b) => { notify = on; setSw(b, on); })));
  return [el("button", { class: "btn", onclick: async () => done(await dismissFindings(it)) }, "Dismiss"),
    el("button", { class: "btn accent", disabled: !coders.length, onclick: async () => {
      done(await act(() => api("POST", `/api/agents/${it.agent}/deliveries/${it.id}/forward`, { to: to.value, notify }), `Forwarded to ${agentName(to.value)}`));
    } }, "Forward")];
}
function contextCard(aid) {
  const a = agentById(aid), box = el("div", { class: "stack tight context-body" }, el("span", { class: "muted" }, "loading…"));
  api("GET", `/api/agents/${aid}/chat`).then((r) => {
    const last = a.tasks[a.tasks.length - 1];
    const texts = r.events.filter((e) => e.k === "text" && (!last || e.task === last.id)).slice(-2);
    box.replaceChildren(
      last ? el("div", { class: "msg-you" }, el("div", { class: "bubble" }, last.instructions.slice(0, 1500))) : el("span", { class: "muted" }, "No tasks yet."),
      ...texts.map((e) => el("div", { class: "msg-claude" }, renderMarkdown(e.text.slice(0, 1500)))));
  }).catch(() => box.replaceChildren(el("span", { class: "muted" }, "Couldn't load the conversation.")));
  return el("div", { class: "card stack" }, el("div", { class: "row" }, el("span", { class: "title grow" }, `Context from ${a.name}`),
    el("button", { class: "link", onclick: () => go("agent", { agentId: aid, tab: "chat" }) }, "Open chat")), box);
}
