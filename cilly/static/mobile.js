// cilly UI: phones (640px and narrower). One screen at a time: a bottom bar for the
// top-level views (Agents, Inbox, Pull requests, More) instead of the sidebar, and the
// inbox as a page instead of a popover. Everything else is the same views, restyled.
"use strict";

const NAV_VIEWS = new Set(["agents", "inbox", "prs", "more", "policy", "settings", "project"]);
const NAV_TAB = { agents: "agents", inbox: "inbox", prs: "prs", more: "more", policy: "more", settings: "more", project: "agents" };

// The bottom bar's line icons, built as SVG elements (no markup strings).
function navIcon(kind) {
  const s = svg("svg", { width: 22, height: 22, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8,
    "stroke-linejoin": "round", "stroke-linecap": "round", "aria-hidden": "true" });
  const add = (tag, attrs) => s.appendChild(svg(tag, attrs));
  if (kind === "agents") for (const [x, y] of [[3.5, 3.5], [13.5, 3.5], [3.5, 13.5], [13.5, 13.5]]) add("rect", { x, y, width: 7, height: 7, rx: 2 });
  else if (kind === "inbox") { add("path", { d: "M3.5 13.5 6 5.5h12l2.5 8v5h-17z" }); add("path", { d: "M3.5 13.5h5l1 2h5l1-2h5" }); }
  else if (kind === "prs") {
    for (const [cx, cy] of [[6.5, 5.5], [6.5, 18.5], [17.5, 18.5]]) add("circle", { cx, cy, r: 2 });
    add("path", { d: "M6.5 7.5v9" }); add("path", { d: "M17.5 16.5V10a3 3 0 0 0-3-3H11" }); add("path", { d: "m13 5-2 2 2 2" });
  } else for (const cx of [5.5, 12, 18.5]) add("circle", { cx, cy: 12, r: 1.7, fill: "currentColor", stroke: "none" });
  return s;
}
function searchIcon() {
  const s = svg("svg", { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "aria-hidden": "true" });
  s.append(svg("circle", { cx: 11, cy: 11, r: 6.5 }), svg("path", { d: "m16 16 4.5 4.5" }));
  return s;
}
function mountMobileNav() {
  const item = (key, label) => el("button", { class: "m-nav-item", id: "m-nav-" + key, onclick: () => go(key) },
    el("span", { class: "m-ico" }, navIcon(key), key === "inbox" ? el("span", { class: "count-badge", id: "m-inbox-count", hidden: true }) : ""), label);
  $("app").append(el("nav", { class: "mobile-nav", "aria-label": "Main" },
    item("agents", "Agents"), item("inbox", "Inbox"), item("prs", "Pull requests"), item("more", "More")));
}
function renderMobileNav() {
  $("app").classList.toggle("m-nav", NAV_VIEWS.has(UI.view) && !UI.sec);  // a project's or Settings' own screens have ← instead, like an agent
  for (const k of ["agents", "inbox", "prs", "more"]) {
    const b = $("m-nav-" + k);
    if (b) { b.classList.toggle("on", NAV_TAB[UI.view] === k); b.setAttribute("aria-current", NAV_TAB[UI.view] === k ? "page" : "false"); }
  }
}

function hostStatus() {
  const [dot, text] = hostSummary();
  return [el("div", { class: "row nowrap" }, el("span", { class: "dot " + dot }), el("span", {}, text)), buildLine()];
}
const mRow = (a) => sidebarRow(a);

// ---- Agents: the sidebar's groups as a page ----
VIEWS.agents = {
  mount(main) {
    const search = el("input", { class: "input m-search", placeholder: "Search agents", "aria-label": "Search agents", spellcheck: false, value: UI.mSearch || "" });
    search.addEventListener("input", () => { UI.mSearch = search.value.trim().toLowerCase(); SIGS.mAgents = null; VIEWS.agents.update(); });
    const list = el("div", { class: "m-list" }), host = el("div", { class: "sb-host m-host" });
    main.append(el("div", { class: "m-page" },
      el("div", { class: "m-top-row" }, el("img", { class: "logo", src: "/logo.png", alt: "" }), el("span", { class: "sb-name grow" }, "cilly"),
        el("button", { class: "icon-btn m-find", title: "Go to an agent, project or page", "aria-label": "Go to an agent, project or page", onclick: () => STATE && openSwitcher() }, searchIcon()),
        el("button", { class: "btn primary m-new", onclick: () => go("new") }, el("span", { class: "plus" }, "+"), "New agent")),
      search, list, host));
    VIEWS.agents.p = { list, host };
  },
  update() {
    const P = VIEWS.agents.p;
    if (!P || !STATE) return;
    const q = UI.mSearch || "";
    const all = STATE.agents_list.filter((a) => !q || a.name.toLowerCase().includes(q) || projectName(a.project).toLowerCase().includes(q));
    const projects = STATE.projects || [];
    if (!editingIn(P.list) && changed("mAgents", [all.map((a) => [a.id, a.name, statusText(a), statusShort(a), agentLine(a), a.pinned, a.project, a.archived]),
      projects.map((p) => [p.id, p.name, p.pending.length]), q, UI.showArchived, [...UI.closedGroups], isOwner()])) {  // not while you rename one
      // the sidebar's groups: pinned, each project (its row opens it), the rest, then archived when you ask
      const live = all.filter((a) => !a.archived), archived = all.filter((a) => a.archived);
      const pinned = live.filter((a) => a.pinned), rest = live.filter((a) => !a.pinned && !projectById(a.project));
      const shown = projects.filter((p) => !q || p.name.toLowerCase().includes(q) || live.some((a) => a.project === p.id));
      const parts = [];
      if (!STATE.agents_list.length) parts.push(el("div", { class: "sb-empty" }, "No agents yet. Tap New agent to make one."));
      else if (!all.length) parts.push(el("div", { class: "sb-empty" }, "No agent matches."));
      if (pinned.length) parts.push(el("div", { class: "sb-caption" }, "Pinned"), ...pinned.map(mRow));
      if (shown.length || isOwner()) parts.push(el("div", { class: "sb-caption row nowrap" }, el("span", { class: "grow" }, "Projects"),
        isOwner() ? el("button", { class: "icon-btn sb-add", title: "New project", "aria-label": "New project", onclick: () => newProject() }, "+") : ""));
      for (const p of shown) {
        const members = live.filter((a) => !a.pinned && a.project === p.id);
        const count = STATE.agents_list.filter((a) => a.project === p.id && !a.archived).length;
        const row = el("button", { class: "m-project", onclick: () => go("project", { projectId: p.id }) },
          el("span", { class: "grow stack0" }, el("span", { class: "ellipsis" }, p.name),
            p.pending.length ? el("span", { class: "m-proj-note" }, `${plural(p.pending.length, "suggested note")}`) : ""),
          count ? el("span", { class: "muted" }, plural(count, "agent")) : "");
        const key = "p:" + p.id, closed = UI.closedGroups.has(key);
        const fold = el("button", { class: "icon-btn sb-fold", "aria-expanded": String(!closed), "aria-label": `${closed ? "Show" : "Hide"} the agents in ${p.name}`, onclick: () => {
          closed ? UI.closedGroups.delete(key) : UI.closedGroups.add(key); saveSidebarPrefs(); VIEWS.agents.update();
        } }, foldMark(!closed));
        parts.push(el("div", { class: "m-project-line" }, row, members.length ? fold : newHere(p)),  // an empty project is one line, with a +
          ...(closed ? [] : members.map((a) => { const r = sidebarRow(a, true); r.className += " member"; return r; })));
      }
      if (rest.length) parts.push(el("div", { class: "sb-caption" }, pinned.length || shown.length ? "Other agents" : "Agents"), ...rest.map(mRow));
      if (archived.length) {
        parts.push(el("button", { class: "sb-caption sb-group", "aria-expanded": String(!!UI.showArchived), onclick: () => { UI.showArchived = !UI.showArchived; saveSidebarPrefs(); VIEWS.agents.update(); } },
          el("span", { class: "grow" }, `Archived (${archived.length})`), el("span", { class: "dim" }, foldMark(UI.showArchived))));
        if (UI.showArchived) parts.push(...archived.map(mRow));
      }
      P.list.replaceChildren(...parts);
    }
    if (changed("mHost", [hostProblems(), STATE.tools.bwrap, STATE.tools.bwrap_problem, STATE.tools.use_bwrap, STATE.build?.state])) P.host.replaceChildren(...hostStatus());
  },
};

// ---- Inbox as a page ----
VIEWS.inbox = {
  mount(main) {
    const count = el("span", { class: "muted" }), list = el("div", { class: "m-cards" });
    main.append(el("div", { class: "m-page" }, el("div", { class: "m-title-block" }, el("h1", { class: "m-h1" }, "Inbox"), count), list));
    VIEWS.inbox.p = { count, list };
    pollInbox();
  },
  update() {
    const P = VIEWS.inbox.p;
    if (!P || !changed("mInbox", [INBOX.loaded, INBOX.items, INBOX.filter, STATE.egress_ports])) return;
    P.count.textContent = INBOX.count ? `${INBOX.count} waiting on you` : "";
    if (!INBOX.loaded) { P.list.replaceChildren(el("div", { class: "muted" }, "Loading…")); return; }
    if (!INBOX.items.length) { P.list.replaceChildren(emptyInbox()); return; }
    P.list.replaceChildren(inboxFilter(() => { SIGS.mInbox = null; VIEWS.inbox.update(); }), ...paged("inbox", inboxItems(), inboxCard));
  },
};
// ---- More ----
VIEWS.more = {
  mount(main) {
    const host = el("div", { class: "sb-host m-host" });
    const row = (label, sub, fn) => el("button", { class: "m-set-row", onclick: fn }, el("span", { class: "grow" }, label), sub ? el("span", { class: "muted" }, sub) : "", el("span", { class: "chev", "aria-hidden": "true" }, "›"));
    const page = el("div", { class: "m-page" }, el("div", { class: "m-title-block" }, el("h1", { class: "m-h1" }, isOwner() ? "More" : "This device")));
    if (isOwner()) page.append(el("div", { class: "m-set-list" }, row("New project", "", () => newProject()),
      row("Claude policy", "", () => go("policy")), row("Settings", "", () => go("settings"))));
    else page.append(el("div", { class: "m-device stack" },
      el("div", { class: "card stack tight" }, el("span", { class: "title" }, STATE.device?.name ? `Paired as ${STATE.device.name}` : "Paired"),
        el("span", { class: "muted pretty" }, PAIRED_SHORT), pairedLists(),
        el("div", {}, el("button", { class: "btn", onclick: signOutDevice }, "Sign out this device"))),
      el("div", { class: "section-title" }, "Notifications"), deviceSection()));
    page.append(host);
    main.append(page);
    VIEWS.more.p = { host };
  },
  update() {
    const P = VIEWS.more.p;
    if (P && STATE && changed("mHost", [hostProblems(), STATE.tools.bwrap, STATE.tools.bwrap_problem, STATE.tools.use_bwrap, STATE.build?.state])) P.host.replaceChildren(...hostStatus());
  },
};

async function signOutDevice() {
  if (!(await confirmModal("Sign out this device?", "It forgets its access and has to be paired again on your PC.", "Sign out", true))) return;
  try { await api("POST", "/api/devices/me/forget", {}); } catch { /* signed out below either way */ }
  try { localStorage.removeItem(DEVICE_KEY); } catch { /* fine */ }
  sessionStorage.removeItem("cilly-token");
  location.reload();
}

// Back on a phone goes up a level: it closes a sheet or dialog, or opens the screen above this one.
// While there is such a level, one extra history entry sits on top for Back to use up.
let BACK_ARMED = false;
function upTarget() {
  if (SWITCHER) return closeSwitcher;
  if (POP) return closePop;
  if (document.querySelector(".modal-back")) return () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));  // every dialog closes on Esc
  const v = UI.view;
  if (v === "project" && UI.sec) return () => go("project", { projectId: UI.projectId });
  if (v === "settings" && UI.sec) return () => VIEWS.settings.open(null);
  if (["agent", "project", "new"].includes(v)) return () => go("agents");
  if (v === "review") return () => go("inbox");
  if (["settings", "policy"].includes(v)) return () => go("more");
  return null;
}
function syncBack() {
  if (!isPhone() || BACK_ARMED || !upTarget()) return;
  BACK_ARMED = true;
  history.pushState({ up: true }, "");
}
function onBack() {
  if (!BACK_ARMED) return;
  BACK_ARMED = false;
  const up = upTarget();
  if (up) { up(); syncBack(); }
  else history.back();  // nothing above (you went up in the page): leave, as Back would have
}
window.addEventListener("popstate", onBack);

// Crossing the phone width (rotating, resizing): the home screen differs, Settings and a project
// switch layouts in place and keep what you changed, and the rest re-renders. Never while you type in a
// field: that waits until you leave it.
const IN_PLACE = new Set(["settings", "project"]);
let WAS_PHONE = isPhone(), RESIZE_WAITS = false;
function phoneResize() {
  if (!$("main")) return;  // the token page has no shell
  const now = isPhone();
  if (WAS_PHONE === null || WAS_PHONE === now) { WAS_PHONE = now; return; }
  if (typingIn($("main"))) {
    if (!RESIZE_WAITS) {
      RESIZE_WAITS = true;
      document.addEventListener("focusout", () => { RESIZE_WAITS = false; setTimeout(phoneResize); }, { once: true });
    }
    return;
  }
  WAS_PHONE = now;
  closePop();
  if (now && (UI.view === "home")) go("agents");
  else if (!now && ["agents", "more", "inbox"].includes(UI.view)) go("home");
  else if (IN_PLACE.has(UI.view)) { VIEWS[UI.view].update(); renderSidebar(); renderMobileNav(); syncBack(); }
  else go(UI.view, { agentId: UI.agentId, tab: UI.tab });
}
window.addEventListener("resize", phoneResize);

// Edge swipes in the installed app, where the browser's own back swipe is gone (iOS): in from the left edge goes
// up a level on a phone and opens the sidebar on a tablet, and a swipe left closes the drawer. Android has a
// system back gesture on that edge, so the phone back swipe is iOS only.
const EDGE_PX = 20, SWIPE_PX = 70, SWIPE_MS = 700;
const swipedIn = (x0, y0, x1, y1, ms) => x0 <= EDGE_PX && x1 - x0 >= SWIPE_PX && Math.abs(y1 - y0) <= (x1 - x0) / 2 && ms < SWIPE_MS;
const swipedOut = (x0, y0, x1, y1, ms) => x0 - x1 >= SWIPE_PX && Math.abs(y1 - y0) <= (x0 - x1) / 2 && ms < SWIPE_MS;
const installedApp = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const onIos = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
function edgeSwipe(from, to) {
  if (document.querySelector(".lightbox, .modal-back")) return;
  const ms = Date.now() - from.t;
  if (swipedIn(from.x, from.y, to.clientX, to.clientY, ms)) {
    if (isPhone()) {
      const up = onIos() && upTarget();
      if (up) BACK_ARMED ? history.back() : (up(), syncBack());
    } else if (window.innerWidth <= 767 ? !UI.drawer : UI.sbHidden) toggleSidebar();
  } else if (UI.drawer && from.inDrawer && swipedOut(from.x, from.y, to.clientX, to.clientY, ms)) closeDrawer();
}
if (installedApp()) {
  let from = null;
  document.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    from = e.touches.length === 1 ? { x: t.clientX, y: t.clientY, t: Date.now(), inDrawer: !!e.target.closest?.("#sidebar, #backdrop") } : null;
  }, { passive: true });
  document.addEventListener("touchend", (e) => { if (from && e.changedTouches[0]) edgeSwipe(from, e.changedTouches[0]); from = null; }, { passive: true });
}
