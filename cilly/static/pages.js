// cilly UI: New agent, Claude policy, Pull requests, Settings.
"use strict";

// ---- New agent ----
const NEW = { model: null, profile: "coding", mode: "ephemeral", autostop: true, repo: null, name: "", text: "", attach: [], prTitle: "", cpus: null, mem: null, mcp: new Set(), image: "", route: "",
  keep: false, keepDest: "", plugins: new Set(), skills: new Set(), project: null, applied: null, skip: [], account: null };
// Picking a project fills in its defaults; you can still change each of them.
function applyProjectDefaults(p) {
  const d = p.defaults, research = NEW.profile === "research";
  if (d.mode) NEW.mode = d.mode;
  NEW.autostop = d.auto_stop ?? (NEW.mode === "ephemeral" && !research);
  NEW.image = d.image || "";
  NEW.mcp = new Set(d.mcp);
  NEW.plugins = new Set(d.plugins || []);
  NEW.skills = new Set(d.skills || []);
  NEW.keep = !!d.keep_outbox; NEW.keepDest = d.keep_outbox || NEW.keepDest;
  NEW.model = d.model ? { model: d.model, server: d.model_server || undefined } : null;
  NEW.skip = [...(d.skip_global || [])];
  NEW.account = d.account || null;
  NEW.applied = p.id;
}
VIEWS.new = {
  mount(main) {
    const P = (VIEWS.new.p = {});
    const phone = isPhone();
    const mod = IS_MAC ? "⌘" : "Ctrl";
    P.banner = el("div");
    P.checklist = el("div");
    // the prompt is a task description, so Enter adds a line and the modifier creates
    P.ta = el("textarea", { rows: phone ? 3 : 5, value: NEW.text, "aria-label": "Task", placeholder: phone ? "Describe the task, or leave empty and send instructions later"
      : `Describe the task, or leave it empty and send instructions later. Enter adds a new line. ${mod}+Enter creates and starts it.` });
    P.ta.addEventListener("input", () => { NEW.text = P.ta.value; });
    P.ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); create(); } });
    P.ta.addEventListener("paste", (e) => {  // a screenshot goes along with the first message
      const f = [...(e.clipboardData?.files || [])];
      if (f.length) { e.preventDefault(); addNewAttach(f); }
    });
    P.attachRow = el("div", { class: "attach-row", hidden: true });
    P.files = el("input", { type: "file", multiple: "", hidden: true });
    P.files.addEventListener("change", () => { addNewAttach([...P.files.files]); P.files.value = ""; });
    P.attachBtn = el("button", { class: "round", type: "button", title: "Attach files (read-only in the VM)", "aria-label": "Attach files", onclick: () => P.files.click() }, "+");
    renderNewAttach();  // files you added before you left the page
    P.repo = el("select", { class: "select pill-in", "aria-label": "Repository" });
    P.repo.addEventListener("change", () => { NEW.repo = P.repo.value; VIEWS.new.update(true); });
    // which GitHub account, for a repository more than one has ticked
    P.account = el("select", { class: "select pill-in", "aria-label": "GitHub account" });
    P.account.addEventListener("change", () => { NEW.account = P.account.value; });
    P.project = el("select", { class: "select pill-in", "aria-label": "Project" });
    P.project.addEventListener("change", () => {
      NEW.project = P.project.value || null;
      const p = projectById(NEW.project);
      if (p) applyProjectDefaults(p); else NEW.applied = null;
      VIEWS.new.update(true);
    });
    P.name = el("input", { class: "input pill-in", placeholder: "Name", maxlength: 48, value: NEW.name, "aria-label": "Name" });
    P.name.addEventListener("input", () => { NEW.name = P.name.value; });
    P.create = el("button", { class: "btn primary", title: `${mod}+Enter creates and starts it. Enter adds a new line.`, onclick: create }, "Create and start",
      phone ? "" : el("span", { class: "keycap" }, mod === "⌘" ? "⌘⏎" : "Ctrl ⏎"));
    P.profileSeg = seg([["coding", "Coding"], ["research", "Research"]], NEW.profile, (v) => { NEW.profile = v; VIEWS.new.update(true); }, "light");
    P.researchNote = el("div", { class: "note-box" }, "Open web only, with its own Claude login. No GitHub token, repository or host folders.");
    P.modes = el("div", { class: "mode-list", role: "radiogroup", "aria-label": "Lifecycle" });
    P.hint = el("div", { class: "muted" });
    P.inject = makeInjectWidget(() => NEW.profile !== "research" && !NEW.repo);
    P.mic = micButton(P.ta, undefined, "new-agent");
    // the options: every field, in one card (no fold-out)
    P.cpus = el("input", { class: "input", type: "number", min: 1, "aria-label": "vCPUs" });
    P.mem = el("input", { class: "input", type: "number", min: 512, step: 256, "aria-label": "Memory (MiB)" });
    P.pr = el("input", { class: "input", maxlength: 200, placeholder: "Optional. The agent picks one otherwise.", value: NEW.prTitle, "aria-label": "PR title" });
    P.pr.addEventListener("input", () => { NEW.prTitle = P.pr.value; });
    P.auto = sw(NEW.autostop, (on, b) => { NEW.autostop = on; setSw(b, on); });
    // an agent that loses its disk can copy its ~/outbox to this PC first: a host folder, so only from the PC's panel
    P.keep = sw(NEW.keep, (on, b) => { NEW.keep = on; setSw(b, on); VIEWS.new.update(); });
    P.keepDest = el("input", { class: "input mono", placeholder: "/home/you/agent-outboxes", value: NEW.keepDest, spellcheck: false, "aria-label": "Folder for its outbox" });
    P.keepDest.addEventListener("input", () => { NEW.keepDest = P.keepDest.value; });
    P.mcp = el("div", { class: "row tight" });
    P.plugins = el("div", { class: "row tight" });
    P.skills = el("div", { class: "row tight" });
    P.imageBox = el("div");
    P.modelBox = el("div", { class: "stack tight" });
    P.routeBox = el("div", { class: "stack tight" });
    // Advanced: sites of the global allowlist this agent goes without (the Sandbox tab changes them later)
    P.sites = el("div", { class: "stack tight" });
    P.advCount = el("span", { class: "dim" });
    P.adv = el("details", { class: "adv opt-sec new-adv" }, el("summary", {}, "Advanced", P.advCount),
      el("span", { class: "muted small pretty" }, "Sites from the global allowlist in Settings. Uncheck one to leave it out for this agent. "
        + "An agent that installs packages needs them for that, so you can also leave them in for its first task and take them out later on its Sandbox tab."),
      P.sites);
    const opt = (title, sub, ...body) => el("div", { class: "opt-sec" }, el("div", { class: "stack0" }, el("span", {}, title), sub ? el("span", { class: "muted small pretty" }, sub) : ""), ...body);
    P.autoRow = el("label", { class: "opt-sec row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "Stop after each task"),
      el("span", { class: "muted small" }, "Frees the VM when it finishes")), P.auto);
    P.prBox = opt("PR title", "", P.pr);
    P.keepBox = el("div", { class: "opt-sec" }, el("label", { class: "row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "Keep its outbox when it stops"),
      el("span", { class: "muted small pretty" }, "Copies ~/outbox into a new subfolder here before its disk is deleted")), P.keep), P.keepDest);
    // no host-folder injection from a phone: the folder browser needs a desktop (Start from there to give it one)
    P.injectBox = opt("Host folder", "Copied in read-only at /inject. Optional.", P.inject.node);
    P.options = el("div", { class: "card flush new-options" }, phone ? "" : el("div", { class: "opt-head" }, "Virtual machine"),
      opt("Resources", "", el("div", { class: "row nowrap opt-pair" },
        el("label", { class: "field grow" }, el("span", { class: "label" }, "vCPUs"), P.cpus),
        el("label", { class: "field grow" }, el("span", { class: "label" }, "Memory (MiB)"), P.mem))),
      P.modelOpt = opt("Model", "Claude's, or one from your model servers", P.modelBox), opt("VM image", "Which toolchains it has", P.imageBox), opt("Route", "How its traffic leaves", P.routeBox),
      P.extras = el("details", { class: "adv opt-sec new-adv" }, el("summary", {}, "Tools and files", P.extrasCount = el("span", { class: "dim" })),
        opt("MCP servers", "", P.mcp), P.pluginsOpt = opt("Plugins", "Installed in its VM before its first task", P.plugins),
        P.skillsOpt = opt("Skills", "Your own, added in its VM before its first task", P.skills), P.prBox, phone ? "" : P.injectBox),
      P.adv);
    P.stopCard = el("div", { class: "card flush" }, P.autoRow, P.keepBox);  // what happens to the VM when it stops, next to Lifecycle
    const lifecycle = el("div", { class: "stack tight" }, el("span", { class: "section-title" }, "Lifecycle"), P.modes);
    if (phone) {  // a full-screen sheet: the same fields as a settings-style list, Create pinned at the bottom
      const page = el("div", { class: "page" }), inner = el("div", { class: "page-inner m-new-page" });
      page.append(inner); main.append(page);
      P.name.placeholder = "auto";
      P.repoRow = el("label", { class: "m-set-row" }, el("span", { class: "grow" }, "Repository"), P.repo, el("span", { class: "chev", "aria-hidden": "true" }, "›"));
      inner.append(
        el("div", { class: "m-sheet-head" }, el("button", { class: "link quiet", onclick: () => go("agents") }, "Cancel"),
          el("span", { class: "grow center-text title" }, "New agent"), el("span", { class: "m-head-pad" })),
        P.banner, el("div", { class: "m-seg-wrap" }, P.profileSeg),
        P.box = el("div", { class: "composer" }, P.attachRow, P.ta, el("div", { class: "bottom" }, el("span", { class: "grow" }), P.attachBtn, P.files, P.mic)),
        P.researchNote,
        el("div", { class: "m-set-list" }, P.projectRow = el("label", { class: "m-set-row" }, el("span", { class: "grow" }, "Project"), P.project, el("span", { class: "chev", "aria-hidden": "true" }, "›")),
          P.repoRow, P.accountRow = el("label", { class: "m-set-row" }, el("span", { class: "grow" }, "GitHub account"), P.account, el("span", { class: "chev", "aria-hidden": "true" }, "›")),
          el("label", { class: "m-set-row" }, el("span", { class: "grow" }, "Name"), P.name, el("span", { class: "chev spacer", "aria-hidden": "true" }, "›"))),
        lifecycle, P.stopCard, P.options, P.checklist,
        el("div", { class: "m-foot" }, P.create, P.hint));
      return;
    }
    P.cancel = el("button", { class: "btn ghost", onclick: cancelNew }, "Cancel");
    main.append(el("div", { class: "page" }, el("div", { class: "new-inner" }, P.banner,
      el("div", { class: "stack0 new-title" }, el("h1", { class: "hero left" }, "What should the new agent do?"),
        el("span", { class: "muted" }, "It boots in its own VM with your policy and egress allowlist.")),
      el("div", { class: "new-cols" },
        el("div", { class: "new-main" },
          P.box = el("div", { class: "composer" }, P.attachRow, P.ta,
            el("div", { class: "bottom" }, P.profileSeg, P.repo, P.name, el("div", { class: "acts" }, P.attachBtn, P.files, P.mic))),
          P.checklist,
          P.projectRow = el("div", { class: "row new-project" }, el("span", { class: "muted" }, "Project"), P.project,
            P.projectNote = el("span", { class: "muted grow ellipsis" })),
          P.accountRow = el("div", { class: "row new-project" }, el("span", { class: "muted" }, "GitHub account"), P.account,
            P.accountNote = el("span", { class: "muted grow ellipsis" })),
          P.researchNote, lifecycle, P.stopCard),
        P.options))),
    el("div", { class: "new-foot" }, el("div", { class: "new-foot-inner" }, P.hint, P.cancel, P.create)));
  },
  update(force) {
    const P = VIEWS.new.p;
    if (!P || !STATE) return;
    const research = NEW.profile === "research", miss = hostProblems();
    let proj = projectById(NEW.project);
    if (NEW.project && !proj) NEW.project = null;  // deleted meanwhile
    if (proj && NEW.applied !== proj.id) applyProjectDefaults(proj);  // came from the project's New agent button
    if (changed("newProjects", [(STATE.projects || []).map((p) => [p.id, p.name]), NEW.project]) || force) {
      P.project.replaceChildren(el("option", { value: "" }, "No project"), ...(STATE.projects || []).map((p) => el("option", { value: p.id }, p.name)));
      P.project.value = NEW.project || "";
    }
    P.projectRow.hidden = !(STATE.projects || []).length;
    if (P.projectNote) P.projectNote.textContent = proj ? (research ? "Grouped with the project. Research agents don't get its files or instructions."
      : "Gets the project's instructions, sites, files and research agent.") : "";
    if (changed("newRepos", [STATE.repos, proj?.repos]) || force) {
      // none unless you pick one, so a private session never gets a repository by accident; the project's come first
      const first = (proj?.repos || []).filter((r) => STATE.repos.includes(r)), rest = STATE.repos.filter((r) => !first.includes(r));
      const keep = STATE.repos.includes(NEW.repo) ? NEW.repo : "";
      P.repo.replaceChildren(el("option", { value: "" }, isPhone() ? "No repository" : "No repository (scratch folder)"), ...first.map((r) => el("option", { value: r }, r)),
        first.length && rest.length ? el("option", { disabled: true }, "────") : "",
        ...rest.map((r) => el("option", { value: r }, r)));
      P.repo.value = keep; NEW.repo = P.repo.value;
    }
    const accts = research || !NEW.repo ? [] : accountsFor(NEW.repo);
    P.accountRow.hidden = accts.length < 2;  // one account has it: nothing to pick
    if (changed("newAccount", [NEW.repo, accts.map((a) => [a.token, a.name]), NEW.account]) || force) {
      P.account.replaceChildren(...accts.map((a, i) => el("option", { value: a.token }, a.name + (i ? "" : " (default)"))));
      P.account.value = accountOf(NEW.repo, NEW.account)?.token || "";
      if (P.accountNote) P.accountNote.textContent = accts.length > 1 ? `${NEW.repo} is under ${accts.length} accounts` : "";
    }
    setSw(P.auto, NEW.autostop);
    setSw(P.keep, NEW.keep);
    if (document.activeElement !== P.keepDest) P.keepDest.value = NEW.keepDest;
    P.keepBox.hidden = research || NEW.mode === "persistent" || !isOwner();
    P.keepDest.hidden = !NEW.keep;
    if (!P.cpus.value) P.cpus.value = STATE.vm_defaults.cpus;
    if (!P.mem.value) P.mem.value = STATE.vm_defaults.memory_mb;
    P.cpus.max = STATE.vm_defaults.max_cpus; P.mem.max = STATE.vm_defaults.max_memory_mb;
    (P.repoRow || P.repo).hidden = research; P.researchNote.hidden = !research; P.injectBox.hidden = research || !isOwner(); P.autoRow.hidden = research;
    P.stopCard.hidden = P.autoRow.hidden && P.keepBox.hidden;
    P.extrasCount.textContent = [NEW.mcp.size + NEW.plugins.size + NEW.skills.size].map((n) => (n ? ` ${n} on` : ""))[0];
    P.prBox.hidden = research || !NEW.repo;  // only an agent with a repository opens pull requests
    if (changed("newModes", [NEW.mode, research]) || force) {
      const row = (v, title, desc, tag) => el("button", { class: "mode-row" + (NEW.mode === v ? " on" : ""), type: "button", role: "radio", "aria-checked": String(NEW.mode === v),
        onclick: () => { NEW.mode = v; NEW.autostop = v === "ephemeral" && !research; setSw(P.auto, NEW.autostop); VIEWS.new.update(true); } },
      el("span", { class: "radio" }), el("span", { class: "stack0" }, el("span", { class: "title" }, title), el("span", { class: "muted small pretty" }, desc)),
      tag ? el("span", { class: "ok-soft small" }, tag) : el("span"));
      P.modes.replaceChildren(
        row("ephemeral", "Ephemeral", "A fresh VM each start. Everything is deleted when it stops.", "Safest"),
        row("persistent", "Persistent", "Keeps its checkout, tools and conversation across restarts."),
        row("memory", "Ephemeral + memory", "A fresh VM each start, but conversations and memory are restored."));
    }
    if (changed("newModel", [STATE.model_servers, NEW.model, research]) || force) P.modelBox.replaceChildren(newModelSelect());
    if (changed("newImages", [STATE.images, STATE.base_image, STATE.research_base_image, NEW.profile]) || force) {
      if (NEW.image && !(STATE.images || []).some((i) => i.name === NEW.image)) NEW.image = "";
      P.imageBox.replaceChildren(imageSelect(NEW.image, (v) => { NEW.image = v; VIEWS.new.update(true); }, NEW.profile));
      P.routeBox.replaceChildren(routeSelect(NEW.route, (v) => { NEW.route = v || ""; VIEWS.new.update(true); },
        `Same as ${NEW.project ? "the project" : "Settings"} (${routeLabel(inheritedRoute(NEW.project))})`),
      el("span", { class: "dim small pretty" }, routeNote(NEW.route || inheritedRoute(NEW.project))));
    }
    const img = (STATE.images || []).find((i) => i.name === (NEW.image || (research ? STATE.research_base_image : STATE.base_image)));
    const age = imageAge(img);
    P.adv.hidden = research;  // research agents reach the open web
    if (changed("newSites", [STATE.setup?.allow, NEW.skip, NEW.repo]) || force) {
      const left = NEW.skip.filter((h) => (STATE.setup?.allow || []).includes(h)).length;
      P.advCount.textContent = left ? `${left} site${left === 1 ? "" : "s"} left out` : "";
      P.sites.replaceChildren(...globalSites(NEW.skip, (next) => { NEW.skip = next; VIEWS.new.update(true); }, NEW.repo));
    }
    P.pluginsOpt.hidden = research || !Object.keys(STATE.plugins || {}).length;
    P.skillsOpt.hidden = research || !Object.keys(STATE.skills || {}).length;
    if (changed("newSkills", [STATE.skills, [...NEW.skills]]) || force) {
      P.skills.replaceChildren(...Object.keys(STATE.skills || {}).map((n) => el("button", { class: "chip" + (NEW.skills.has(n) ? " on" : ""), type: "button",
        title: STATE.skills[n].description, onclick: () => { NEW.skills.has(n) ? NEW.skills.delete(n) : NEW.skills.add(n); VIEWS.new.update(true); } }, n)));
    }
    if (changed("newPlugins", [STATE.plugins, [...NEW.plugins]]) || force) {
      P.plugins.replaceChildren(...Object.keys(STATE.plugins || {}).map((n) => el("button", { class: "chip" + (NEW.plugins.has(n) ? " on" : ""), type: "button",
        onclick: () => { NEW.plugins.has(n) ? NEW.plugins.delete(n) : NEW.plugins.add(n); VIEWS.new.update(true); } }, n)));
    }
    if (changed("newMcp", [STATE.mcp_servers, [...NEW.mcp], isOwner()]) || force) {
      const lib = Object.keys(STATE.mcp_servers || {});
      P.mcp.replaceChildren(...lib.map((n) => el("button", { class: "chip" + (NEW.mcp.has(n) ? " on" : ""), type: "button",
        onclick: () => { NEW.mcp.has(n) ? NEW.mcp.delete(n) : NEW.mcp.add(n); VIEWS.new.update(true); } }, n)),
      isOwner() ? el("button", { class: "chip add", type: "button", onclick: () => mcpAddDialog("Saved to your MCP library and turned on for the new agent.",
        (n) => { NEW.mcp.add(n); VIEWS.new.update(true); }) }, "+ Add")
        : !lib.length ? el("span", { class: "muted" }, "None in the library yet.") : "");
    }
    P.banner.replaceChildren(miss.length ? el("div", { class: "note-box err" }, "Host not ready: " + miss.join(", ")) : "");
    if (changed("checklist", [STATE.setup, miss, STATE.tools.bwrap, STATE.tools.bwrap_problem, STATE.tools.use_bwrap]) || force) {
      const list = isOwner() ? setupChecklist() : "";
      P.checklist.replaceChildren(list);
      P.checklist.hidden = !list;  // no gap under the composer when there's nothing left to set up
    }
    P.create.disabled = miss.length > 0 || P.busy;
    P.mic.hidden = !STATE.speech?.enabled;
    if (P.cancel) P.cancel.hidden = !newBackTo();
    P.hint.textContent = miss.length ? "Create is blocked until the host is ready."
      : age > IMAGE_OLD_DAYS ? `Its VM image is ${age} days old. Rebuild it in Settings for updates.`
      : `Policy v${STATE.policy_version} and ${STATE.allow_count - (research ? 0 : NEW.skip.filter((h) => (STATE.setup?.allow || []).includes(h)).length)} allowlisted hosts apply on boot.`;
    P.hint.className = (age > IMAGE_OLD_DAYS && !miss.length ? "warn" : "muted") + (P.cancel ? " grow small" : " center-text");
  },
};
// Cancel goes back to where you came from (the draft is kept), or to the first agent
function newBackTo() {
  const l = UI.last;
  if (l?.view === "agent" && agentById(l.agentId)) return l;
  if (l && ["project", "prs", "policy", "settings"].includes(l.view)) return l;
  const a = STATE?.agents_list?.[0];
  return a ? { view: "agent", agentId: a.id, tab: "chat" } : null;
}
function cancelNew() { const b = newBackTo(); if (b) go(b.view, b); }
VIEWS.home = VIEWS.new;
// Files for the first message: kept here until the agent exists, then sent like any message's
function addNewAttach(list) {
  takeFiles(list, NEW.attach);
  renderNewAttach();
}
function renderNewAttach() {
  const P = VIEWS.new.p;
  if (!P?.attachRow) return;
  P.attachRow.replaceChildren(...NEW.attach.map((f, i) => fileChip(f, () => { NEW.attach.splice(i, 1); renderNewAttach(); })));
  P.attachRow.hidden = !NEW.attach.length;
}
async function create() {
  const P = VIEWS.new.p, research = NEW.profile === "research";
  if (P.busy) return;
  let inject = null;
  if (!research) {
    try { inject = await P.inject.get(); } catch (e) { toast(e.message, true); return; }
    if (inject === false) return;
  }
  P.busy = true; VIEWS.new.update();
  try {
    const files = [...NEW.attach], text = NEW.text, prTitle = NEW.prTitle;
    const a = await api("POST", "/api/agents", {
      profile: NEW.profile, name: NEW.name.trim(), repo: research ? null : (NEW.repo || null), agent: "claude",
      // with files, the first message goes after it's made, through the message box's own path (uploads first)
      mode: NEW.mode, auto_stop: research ? false : NEW.autostop, instructions: files.length ? "" : text, pr_title: prTitle,
      cpus: +P.cpus.value, memory_mb: +P.mem.value, inject, mcp: [...NEW.mcp], image: NEW.image || null,
      plugins: research ? [] : [...NEW.plugins].filter((n) => n in (STATE.plugins || {})),
      skip_global: research ? [] : NEW.skip.filter((h) => (STATE.setup?.allow || []).includes(h)),
      skills: research ? [] : [...NEW.skills].filter((n) => n in (STATE.skills || {})),
      project: NEW.project || null, route: NEW.route || null,
      account: research || P.accountRow?.hidden !== false ? null : P.account.value || null,  // only when there was one to pick
      ...(P.keepBox.hidden ? {} : { keep_outbox: NEW.keep ? NEW.keepDest.trim() : null }),
      ...(NEW.model ? { model: NEW.model.model, server: NEW.model.server || null } : {}),
    });
    Object.assign(NEW, { text: "", name: "", prTitle: "", repo: null, attach: [] });  // the next one starts without a repository again
    P.inject.clear();
    await loadState();
    go("agent", { agentId: a.id, tab: "chat" });
    if (a.note) toast(a.note);  // such as where its outbox is kept
    if (files.length) {  // a new conversation, as a first message always is; a failed upload stays in its message box
      Object.assign(composerState(a.id), { draft: text, attach: files, cont: false, prTitle });
      await sendMessage();
    }
  } catch (e) { toast(e.message, true); }
  P.busy = false;
  if (UI.view === "new") VIEWS.new.update();
}

// MCP: Add a server.  Claude policy is the owner's, so this is too (POST /api/mcp, which a paired
// device can't call).  The server checks it like the config file: https only, keys only as ${VAR}.
// A key's value never goes in the config: it's pasted like any secret, and saved on this PC if you like.
const mcpKeyName = (name) => (/^[A-Za-z]/.test(name) ? "" : "MCP_") + name.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "") + "_API_KEY";
function mcpAddDialog(forLabel, onAdded) {
  let type = "remote", wantKey = false, remember = true, keyEdited = false, pasted = null;
  const input = (ph, label, cls = "input mono") => el("input", { class: cls, placeholder: ph, spellcheck: false, autocomplete: "off", "aria-label": label });
  const name = input("e.g. pixellab", "Name"), url = input("https://api.pixellab.ai/mcp", "URL");
  const cmd = input("npx -y @modelcontextprotocol/server-filesystem /work", "Command");
  const keyName = input("PIXELLAB_API_KEY", "Key name"), value = input("Paste it now, or when an agent first needs it", "Key");
  const paste = el("textarea", { class: "editor", rows: 4, spellcheck: false, "aria-label": "claude mcp add command",
    placeholder: 'claude mcp add pixellab https://api.pixellab.ai/mcp -t http -H "Authorization: Bearer …"' });
  value.type = "password";
  name.maxLength = 48; url.maxLength = 2000; keyName.maxLength = 64;  // the server refuses longer ones
  const header = el("select", { class: "select", "aria-label": "Sent as" },
    el("option", { value: "bearer" }, "Authorization: Bearer"), el("option", { value: "x-api-key" }, "x-api-key"));
  const field = (label, inp, help) => el("label", { class: "field" }, el("span", { class: "label" }, label), inp, help ? el("span", { class: "dim small pretty" }, help) : "");
  const keySw = sw(false, (on, b) => { setSw(b, on); wantKey = on; keyBox.hidden = !on; if (on && !keyName.value) suggest(); check(); });
  const headerField = field("Sent as", header);
  const keyBox = el("div", { class: "stack", hidden: true },
    field("Key name", keyName, "Capitals, digits and _. The config only ever has ${NAME}."), headerField,
    field("Key", value, "Leave it empty to paste it later, when an agent first needs it."),
    saveSwitch((on) => { remember = on; }));
  const remote = el("div", { class: "stack" }, field("URL", url));
  const local = el("div", { class: "stack", hidden: true }, field("Command, run inside the VM", cmd, "Its package hosts must be on the allowlist for it to install. The key goes in its environment."));
  const own = el("div", { class: "stack" }, field("Name", name), remote, local,
    el("label", { class: "row nowrap" }, keySw, el("span", {}, "It needs an API key")), keyBox);
  const pasteInfo = el("div", { class: "dim small pretty" }, "Keys in it are kept out of the config.");
  const pasteSave = el("div", { hidden: true }, saveSwitch((on) => { remember = on; }));
  const pasteBox = el("div", { class: "stack", hidden: true }, field("claude mcp add command", paste), pasteInfo, pasteSave);
  const note = el("div", { class: "note-box", hidden: true });
  const add = el("button", { class: "btn primary", disabled: true }, "Add");
  const suggest = () => { if (!keyEdited) keyName.value = name.value.trim() ? mcpKeyName(name.value.trim()) : ""; };
  const hostOf = (u) => { try { return /^https:\/\//.test(u) ? new URL(u).host : ""; } catch { return ""; } };
  const check = () => {
    let host = "", ok;
    if (type === "paste") {
      pasted = null;
      try { if (paste.value.trim()) pasted = parseMcpAdd(paste.value); pasteInfo.classList.remove("err"); }
      catch (e) { pasteInfo.textContent = e.message; pasteInfo.classList.add("err"); }
      if (pasted) {
        const keys = Object.keys(pasted.secrets);
        host = hostOf(pasted.server.url || "");
        pasteInfo.textContent = `${pasted.name}, ${pasted.server.type === "stdio" ? "runs " + pasted.server.command : "at " + (host || pasted.server.url)}.`
          + (keys.length ? ` ${keys.join(", ")} ${keys.length === 1 ? "is" : "are"} kept out of the config.` : "");
      } else if (!paste.value.trim()) pasteInfo.textContent = "Keys in it are kept out of the config.";
      pasteSave.hidden = !pasted || !Object.keys(pasted.secrets).length;
      ok = !!pasted;
    } else {
      host = type === "remote" ? hostOf(url.value.trim()) : "";
      ok = /^[A-Za-z0-9_-]{1,48}$/.test(name.value.trim()) && (type === "remote" ? !!host : !!cmd.value.trim())
        && (!wantKey || /^[A-Z][A-Z0-9_]{0,63}$/.test(keyName.value.trim()));
    }
    note.textContent = host ? `${host} is added to an agent's allowlist while this server is on for it.` : "";
    note.hidden = !host;
    add.disabled = !ok;
  };
  name.addEventListener("input", () => { suggest(); check(); });
  keyName.addEventListener("input", () => { keyEdited = !!keyName.value; check(); });
  for (const i of [url, cmd, paste]) i.addEventListener("input", check);
  const close = () => { value.value = ""; back.remove(); document.removeEventListener("keydown", esc, true); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  add.addEventListener("click", async () => {
    const keep = remember && !!STATE?.can_save_secrets;
    let body;
    try {
      if (type === "paste") body = { name: pasted.name, server: pasted.server, keys: pasted.secrets, remember: keep };
      else {
        const argv = type === "local" ? shellSplit(cmd.value.trim()) : [];
        body = type === "remote" ? { name: name.value.trim(), url: url.value.trim() } : { name: name.value.trim(), command: argv[0], args: argv.slice(1) };
        if (wantKey) body.key = { name: keyName.value.trim(), header: header.value, value: value.value, remember: keep };
      }
    } catch (e) { toast(e.message, true); return; }
    add.disabled = true;
    let r;
    try { r = await api("POST", "/api/mcp", body); }
    catch (e) { toast(e.message, true); check(); return; }
    close();
    await loadState();
    await onAdded(r.name, r.server);
    toast(`Added ${r.name}.`);
    if (r.note) toast(r.note, true);
  });
  const show = (v) => {
    type = v;
    own.hidden = v === "paste"; pasteBox.hidden = v !== "paste";
    remote.hidden = v !== "remote"; local.hidden = v !== "local"; headerField.hidden = v !== "remote";
    check();
  };
  const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } },
    el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
      el("div", { class: "stack0" }, el("h3", {}, "Add an MCP server"), el("span", { class: "muted small" }, forLabel)),
      seg([["remote", "Remote"], ["local", "Local"], ["paste", "Paste a command"]], type, show),
      own, pasteBox, note,
      el("div", { class: "actions" }, el("button", { class: "btn ghost", onclick: close }, "Cancel"), add)));
  document.addEventListener("keydown", esc, true);
  document.body.append(back);
  syncBack();
  name.focus();
}

// What a page says when its data didn't come: the reason, and a way to try again
// A page's header: full width, like an agent's, above the page in the chat's column
const pageHead = (...kids) => el("div", { class: "set-head bare" }, el("div", { class: "set-head-inner" }, ...kids));
const loadFailed = (what, e, retry) => el("div", { class: "note-box err row" },
  el("span", { class: "grow pretty" }, `Couldn't load ${what}. ${e.message}`), el("button", { class: "btn small", type: "button", onclick: retry }, "Retry"));

// ---- Claude policy ----
VIEWS.policy = {
  mount(main) {
    const P = (VIEWS.policy.p = { dirty: false, loaded: false });
    const page = el("div", { class: "page" }), inner = el("div", { class: "page-inner under-head" });
    page.append(inner); main.append(pageHead(el("div", { class: "row nowrap" }, el("h1", { class: "page-title" }, "Claude policy"),
      infoTip("Enforced in every agent VM. Installed root-owned at /etc/claude-code/, so agents can't change it. Saving applies live."))), page);
    P.md = el("textarea", { class: "editor", rows: 14, spellcheck: false, "aria-label": "CLAUDE.md", placeholder: "Your instructions and code style for every agent. Or start from the recommended rules below." });
    P.ms = el("textarea", { class: "editor", rows: 16, spellcheck: false, "aria-label": "Managed settings JSON" });
    P.mcp = el("textarea", { class: "editor", rows: 10, spellcheck: false, "aria-label": "MCP servers JSON",
      placeholder: '{"pixellab": {"type": "http", "url": "https://api.pixellab.ai/mcp", "headers": {"Authorization": "Bearer ${PIXELLAB_API_KEY}"}}}' });
    P.mcpList = el("div", { class: "stack tight" });
    P.lib = {}; P.keys = {}; P.mcpEdited = false;
    P.modes = el("div", { class: "mode-list", role: "radiogroup", "aria-label": "Permission mode" });
    P.notes = sw(true, (on, b) => { setSw(b, on); P.dirty = true; renderBar(); });
    P.dash = sw(false, (on, b) => { setSw(b, on); P.dirty = true; renderBar(); });
    P.plan = sw(false, (on, b) => { setSw(b, on); P.dirty = true; renderBar(); });
    P.useRec = el("button", { class: "btn small", type: "button", disabled: true, onclick: () => addRecommended() }, "Add recommended rules…");
    P.bar = el("div", { class: "save-bar" });
    P.failed = el("div", { hidden: true });
    const dirty = () => { P.dirty = true; renderBar(); };
    for (const t of [P.md, P.ms, P.mcp]) { t.addEventListener("input", dirty); t.disabled = true; }  // until the policy is loaded: saving blanks would wipe it
    P.mcp.addEventListener("input", () => { P.mcpEdited = true; });
    // Four panels behind tabs, one Save for all of them
    const toggle = (sw, title, desc, tip) => el("div", { class: "opt-sec row nowrap" }, el("label", { class: "grow stack0" }, el("span", {}, title), el("span", { class: "muted small pretty" }, desc)), tip || "", sw);
    P.panels = {
      behavior: el("div", { class: "stack" }, el("div", { class: "section-title" }, "Permission mode"), P.modes,
        el("div", { class: "card flush" },
          toggle(P.notes, "Tell each agent about its VM", "Read-only folders, the outbox and blocked sites."),
          toggle(P.dash, "Check for em and en dashes", "Each file Claude writes is checked, and it is asked to fix them."),
          toggle(P.plan, "Keep the plan through compaction", "Agents keep their plan in ~/plan.md, and it comes back each time a long conversation is compacted.",
            infoTip("Adds rules to CLAUDE.md on keeping ~/plan.md and briefing subagents, and a managed hook that puts the plan back into the "
              + "conversation after each compaction. Useful for long tasks, and on local models, which compact often.")))),
      claudemd: el("div", { class: "stack" },
      el("div", { class: "section-title" }, "CLAUDE.md ", el("span", { class: "muted" }, "Instructions and code style for every agent")), P.md,
      el("div", { class: "row" }, P.useRec)),
      managed: el("div", { class: "stack" },
      el("div", { class: "section-title" }, "Managed settings ", el("span", { class: "muted" }, "Permissions, env, hooks")), P.ms),
      extensions: el("div", { class: "stack" },
      el("div", { class: "section-title" }, "MCP servers ", el("span", { class: "muted" }, "Turn them on per agent from the message box"), infoTip("Keys never go in the config. It has ${NAME}, and the key itself is pasted here or "
        + "when an agent first needs it. Remote servers' hosts are added to an agent's allowlist while the server is on for it. Adding, removing and keys are saved right away.")),
      P.mcpList,
      el("details", { class: "adv" }, el("summary", {}, "Edit as JSON"), el("div", { class: "stack adv-body" },
        el("div", { class: "muted pretty" }, "Same format as .mcp.json entries. Write keys as ${NAME}. Changes here apply when you save."), P.mcp)),
      el("div", { class: "section-title" }, "Plugins ", el("span", { class: "muted" }, "Turn them on per agent from the message box"), infoTip("Claude Code plugins, like skills, commands and agents. Each agent's VM installs the ones it has before its next task, "
        + "through its own allowlist and route. Their hooks don't run while your managed settings have allowManagedHooksOnly, as they do by default. Saved right away.")),
      P.plugins = el("div", { class: "stack tight" }),
      el("div", { class: "section-title" }, "Skills ", el("span", { class: "muted" }, "Turn them on per agent from the message box"), infoTip("Your own Claude Code skills. Upload a skill's folder as a .zip, "
        + "or just its SKILL.md, with a name and a description at the top. It stays on this PC, never on GitHub. Each agent you turn it on for gets a copy in ~/.claude/skills "
        + "in its VM before its next task. Research agents never get them. Uploading one with the same name replaces it.")),
      P.skillsBox = el("div", { class: "stack tight" })),
    };
    P.tabs = el("div", { class: "set-tabs", role: "tablist" });
    const POLICY_TABS = [["behavior", "Behavior"], ["claudemd", "CLAUDE.md"], ["managed", "Managed settings"], ["extensions", "MCP, plugins, skills"]];
    P.showTab = (key) => {
      P.tab = key;
      for (const [k, node] of Object.entries(P.panels)) node.hidden = k !== key;
      P.tabs.replaceChildren(...POLICY_TABS.map(([k, label]) => el("button", { class: "tab" + (k === key ? " on" : ""), type: "button", role: "tab", "aria-selected": String(k === key), onclick: () => P.showTab(k) }, label)));
    };
    inner.append(P.tabs, P.failed, ...Object.values(P.panels));
    P.showTab(P.tab || "behavior");
    renderPlugins(); renderMcp(); renderSkills();
    main.append(P.bar);
    function renderBar() {
      const words = !P.loaded ? ["muted", P.failed.hidden ? "Loading…" : "Not loaded"] : P.dirty ? ["warn", "Unsaved changes"] : ["muted", `Up to date (v${STATE?.policy_version ?? ""})`];
      P.bar.replaceChildren(el("span", { class: words[0] }, words[1]),
        el("button", { class: "btn primary", disabled: !P.loaded || !P.dirty, onclick: () => VIEWS.policy.save() }, "Save and apply live"));
    }
    // The saved library, one row a server: its keys and who has it on.  Add, remove and keys save right away,
    // and the JSON below follows, so a later Save doesn't bring a removed server back.
    function renderMcp() {
      const lib = P.lib, users = (n) => (STATE.agents_list || []).filter((a) => (a.mcp || []).includes(n)).map((a) => a.name);
      const vars = (s) => [...new Set([...JSON.stringify(s).matchAll(/\$\{([A-Z][A-Z0-9_]*)/g)].map((m) => m[1]))];
      const where = (s) => { try { return s.url ? new URL(s.url).host : [s.command, ...(s.args || [])].join(" "); } catch { return s.url; } };
      const setKey = async (v, n) => { if (await askSecret("mcp:" + v, `The key for ${n}.`)) await P.refreshMcp(); };
      const menu = (n, anchor) => {
        const item = (label, fn, cls) => el("button", { class: "menu-item " + (cls || ""), onclick: () => { closePop(); fn(); } }, label);
        openPop(anchor, el("div", { class: "menu" }, ...vars(lib[n]).map((v) => item(`Set ${v}…`, () => setKey(v, n))),
          item("Duplicate…", () => duplicate(n)), el("div", { class: "sep" }), item("Remove", () => remove(n), "danger")), { width: 230 });
      };
      const duplicate = async (n) => {
        const to = (await promptModal(`Duplicate ${n}`, "A name for the copy. It uses the same keys, and isn't on for any agent yet.", n + "-copy", "Duplicate"))?.trim();
        if (!to) return;
        try { await api("POST", `/api/mcp/${n}/duplicate`, { name: to }); toast(`Added ${to}. Turn it on for an agent from its message box.`); }
        catch (e) { toast(e.message, true); return; }
        await P.refreshMcp((j) => { if (j[n]) j[to] = j[n]; });
      };
      const remove = async (n) => {
        const on = users(n);
        if (!(await confirmModal(`Remove ${n}?`, on.length ? `It's turned off for ${on.join(", ")}, and for every project. Applies live.` : "It isn't on for any agent.", "Remove", true))) return;
        try { await api("POST", `/api/mcp/${n}/delete`, {}); toast(`Removed ${n}`); } catch (e) { toast(e.message, true); return; }
        await P.refreshMcp((j) => { delete j[n]; });
      };
      const add = el("button", { class: "btn", type: "button", disabled: !P.loaded,
        onclick: () => mcpAddDialog("Saved to your MCP library. Turn it on for an agent from its message box.", (n, s) => P.refreshMcp((j) => { j[n] = s; })) }, "+ Add a server");
      P.mcpList.replaceChildren(
        Object.keys(lib).length ? el("div", { class: "card flush" }, ...Object.entries(lib).map(([n, s]) => {
          const more = el("button", { class: "icon-btn", type: "button", "aria-label": `Actions for ${n}`, title: "Set a key, duplicate, remove", onclick: () => menu(n, more) }, "···");
          const row = el("div", { class: "note-row row nowrap mcp-item" },
            el("div", { class: "grow stack0" }, el("strong", {}, n), el("span", { class: "muted mono small ellipsis" }, `${s.type || "stdio"} · ${where(s)}`),
              ...vars(s).map((v) => {
                const st = P.keys[v] || "missing";
                return el("span", { class: "row tight mcp-key" }, el("code", { class: "small" }, v), el("span", { class: "small " + (st === "missing" ? "warn" : "dim") }, st === "missing" ? "Not set" : st[0].toUpperCase() + st.slice(1)),
                  el("button", { class: "link small", type: "button", onclick: () => setKey(v, n) }, st === "missing" ? "Set key" : "Replace"));
              }),
              el("span", { class: "dim small" }, users(n).length ? `On for ${users(n).join(", ")}` : "Not on for any agent")),
            more);
          onHold(row, () => menu(n, more));
          return row;
        })) : el("div", { class: "dashed-empty" }, P.loaded ? "No MCP servers yet." : "Loading…"),
        el("div", { class: "row" }, add));
    }
    // After a change that's already saved: the list from the server, and the same change in the JSON
    P.refreshMcp = async (change) => {
      try {
        const r = await api("GET", "/api/policy");
        P.lib = r.mcp_servers || {}; P.keys = r.mcp_keys || {};
      } catch (e) { toast(e.message, true); return; }
      if (!P.mcpEdited) P.mcp.value = Object.keys(P.lib).length ? JSON.stringify(P.lib, null, 2) : "";
      else if (change) {
        try { const j = JSON.parse(P.mcp.value.trim() || "{}"); change(j); P.mcp.value = JSON.stringify(j, null, 2); }
        catch { toast("Your MCP JSON has unsaved changes that aren't valid JSON. Fix them before you save, or the change above is undone.", true); }
      }
      await loadState(); renderMcp();
    };
    // the library: add by name and marketplace, or with /plugin add in an agent's message box
    function renderPlugins() {
      const lib = STATE?.plugins || {};
      const name = el("input", { class: "input mono", placeholder: "impeccable", spellcheck: false, "aria-label": "Plugin name" });
      const src = el("input", { class: "input mono grow", placeholder: "pbakaus/impeccable", spellcheck: false, "aria-label": "Marketplace" });
      const add = async () => {
        const m = src.value.trim(), n = name.value.trim() || m.replace(/\/+$/, "").split("/").pop().replace(/\.git$/, "");
        try { await api("POST", "/api/plugins", { name: n, marketplace: m }); toast(`Added ${n}. Turn it on for an agent from its message box.`); }
        catch (e) { toast(e.message, true); return; }
        await loadState(); renderPlugins();
      };
      src.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
      const remove = async (n) => {
        if (!(await confirmModal(`Remove ${n}?`, "It's turned off for every agent and project, and their VMs remove it before their next task.", "Remove"))) return;
        try { await api("POST", `/api/plugins/${n}/delete`, {}); toast(`Removed ${n}`); } catch (e) { toast(e.message, true); }
        await loadState(); renderPlugins();
      };
      const users = (n) => (STATE.agents_list || []).filter((a) => (a.plugins || []).includes(n)).map((a) => a.name);
      P.plugins.replaceChildren(
        Object.keys(lib).length ? el("div", { class: "card flush" }, ...Object.entries(lib).map(([n, info]) => el("div", { class: "note-row row nowrap plugin-item" },
          el("div", { class: "grow stack0" }, el("strong", {}, n), el("span", { class: "muted mono small ellipsis" }, info.marketplace),
            el("span", { class: "dim small" }, [users(n).length ? `On for ${users(n).join(", ")}` : "Not on for any agent", info.accepted ? "its install command is allowed" : ""].filter(Boolean).join(" · "))),
          info.accepted ? el("button", { class: "btn small ghost", onclick: async () => {
            try { await api("POST", `/api/plugins/${n}/accept`, { sha256: "" }); toast(`${n}'s install command isn't allowed any more`); } catch (e) { toast(e.message, true); }
            await loadState(); renderPlugins();
          } }, "Take back its command") : "",
          el("button", { class: "btn small ghost danger-hover", onclick: () => remove(n) }, "Remove"))))
          : el("div", { class: "dashed-empty" }, "No plugins yet. Add one below."),
        el("div", { class: "row nowrap plugin-add" }, name, src, el("button", { class: "btn", onclick: add }, "Add")),
        el("div", { class: "muted pretty" }, "The name it has in its marketplace, and the marketplace: a GitHub owner/repo or an https URL."));
    }
    // your skills: upload a .zip or a SKILL.md, checked and kept on this PC
    function renderSkills() {
      const lib = STATE?.skills || {};
      const users = (n) => (STATE.agents_list || []).filter((a) => (a.skills || []).includes(n)).map((a) => a.name);
      const pick = el("input", { type: "file", accept: ".zip,.md", multiple: true, hidden: true, "aria-label": "Skill file" });
      pick.addEventListener("change", async () => {
        const files = [...pick.files];
        pick.value = "";
        for (const f of files) {
          if (f.size > 10 * 1024 * 1024) { toast(`${f.name} is over 10 MiB`, true); continue; }
          try {
            const r = await api("POST", "/api/skills", { name: f.name, data: await fileToBase64(f) });
            toast(`${r.skill.replaced ? "Updated" : "Added"} ${r.skill.name}. ${r.skill.replaced ? "Agents that have it get the new one." : "Turn it on for an agent from its message box."}`);
          } catch (e) { toast(`${f.name}: ${e.message}`, true); }
        }
        await loadState(); renderSkills();
      });
      const remove = async (n) => {
        if (!(await confirmModal(`Remove ${n}?`, "It's turned off for every agent and project, and their VMs remove it before their next task.", "Remove", true))) return;
        try { await api("POST", `/api/skills/${n}/delete`, {}); toast(`Removed ${n}`); } catch (e) { toast(e.message, true); }
        await loadState(); renderSkills();
      };
      P.skillsBox.replaceChildren(
        Object.keys(lib).length ? el("div", { class: "card flush" }, ...Object.entries(lib).map(([n, s]) => el("div", { class: "note-row row nowrap plugin-item" },
          el("div", { class: "grow stack0" }, el("strong", {}, n), el("span", { class: "muted small clamp2", title: s.description }, s.description),
            el("span", { class: "dim small" }, [plural(s.files, "file"), fmtBytes(s.size), users(n).length ? `On for ${users(n).join(", ")}` : "Not on for any agent"].join(" · "))),
          el("button", { class: "btn small ghost danger-hover", onclick: () => remove(n) }, "Remove"))))
          : el("div", { class: "dashed-empty" }, "No skills yet. Upload one below."),
        el("div", { class: "row" }, pick, el("button", { class: "btn", type: "button", onclick: () => pick.click() }, "Upload a skill"),
          el("span", { class: "muted small pretty" }, "A .zip of the skill's folder, or its SKILL.md. Up to 10 MiB.")));
    }
    P.renderSkills = renderSkills;
    P.renderPlugins = renderPlugins;
    async function addRecommended() {
      const had = P.md.value, lines = new Set(had.split("\n").map((l) => l.trim()));
      const [box, picked] = claudeMdChoices(P.rec, P.rec.claude_md_options.filter((o) => lines.has(o.line)).map((o) => o.key));
      const body = el("div", { class: "stack" }, el("div", { class: "pretty" }, had.trim()
        ? "They go after what you wrote, which stays as it is. Lines it already has are skipped. Nothing changes until you save." : "Nothing changes until you save."), box);
      if (!await confirmModal("Add recommended rules", body, "Add these rules")) return;
      P.md.value = appendRules(had, claudeMdOf(P.rec, picked()));
      if (picked().includes("no_dashes")) setSw(P.dash, true);
      if (P.md.value === had) toast("Your CLAUDE.md already has these rules.");
      else dirty();
    }
    P.renderBar = renderBar; P.renderMcp = renderMcp;
    renderBar();
    VIEWS.policy.load();
  },
  async load() {
    const P = VIEWS.policy.p;
    P.failed.hidden = true; P.renderBar();
    try {
      const r = await api("GET", "/api/policy"), { policy, mcp_servers, mcp_keys } = r;
      P.rec = r; P.useRec.disabled = !r.claude_md_recommended;
      P.mode = policy.permission_mode;
      P.md.value = policy.claude_md || "";
      setSw(P.notes, policy.vm_notes !== false);
      setSw(P.dash, !!policy.dash_check);
      setSw(P.plan, !!policy.keep_plan);
      P.ms.value = JSON.stringify(policy.managed_settings || {}, null, 2);
      P.mcp.value = Object.keys(mcp_servers || {}).length ? JSON.stringify(mcp_servers, null, 2) : "";
      P.lib = mcp_servers || {}; P.keys = mcp_keys || {}; P.mcpEdited = false;
      P.dirty = false; P.loaded = true;
      for (const t of [P.md, P.ms, P.mcp]) t.disabled = false;
      renderModes(); P.renderBar(); P.renderMcp();
    } catch (e) {
      P.failed.replaceChildren(loadFailed("the policy", e, VIEWS.policy.load));
      P.failed.hidden = false; P.renderBar();
    }
    function renderModes() {
      const opts = [["bypassPermissions", "Everything is allowed except your deny rules. The VM and egress proxy do the isolating."],
        ["dontAsk", "Only allow rules run, and everything else is denied."],
        ["acceptEdits", "File edits run. Shell commands and other tools need allow rules."]];
      P.modes.replaceChildren(...opts.map(([m, d]) => el("button", { class: "mode-row" + (P.mode === m ? " on" : ""), type: "button", role: "radio", "aria-checked": String(P.mode === m),
        onclick: () => { P.mode = m; P.dirty = true; renderModes(); P.renderBar(); } },
        el("span", { class: "radio" }), el("span", { class: "stack0" }, el("span", { class: "title mono" }, m), el("span", { class: "muted small pretty" }, d)), el("span"))));
    }
  },
  async save() {
    const P = VIEWS.policy.p;
    if (!P.loaded) return;
    let ms, mcp;
    try { ms = JSON.parse(P.ms.value || "{}"); } catch { toast("Managed settings are not valid JSON.", true); return; }
    try { mcp = JSON.parse(P.mcp.value.trim() || "{}"); } catch { toast("MCP servers are not valid JSON.", true); return; }
    try {
      const r = await api("PUT", "/api/policy", { policy: { permission_mode: P.mode, claude_md: P.md.value, vm_notes: P.notes.classList.contains("on"),
        dash_check: P.dash.classList.contains("on"), keep_plan: P.plan.classList.contains("on"), managed_settings: ms }, mcp_servers: mcp });
      P.dirty = false;
      await P.refreshMcp(); P.mcpEdited = false; P.renderBar();
      toast(`Saved. Applied live to ${plural(r.applied_to_running_agents, "running agent")}. Others get it at their next start.`);
    } catch (e) { toast(e.message, true); }
  },
  update() { VIEWS.policy.p?.renderBar?.(); },
};

// ---- Pull requests ----
// The panel's answer points at Settings, which a paired device can't open
const prError = (text) => (isOwner() ? text : text.replace(/ Paste a token.*$/, " Add one on your PC."));
VIEWS.prs = {
  mount(main) {
    const P = (VIEWS.prs.p = {});
    const page = el("div", { class: "page" }), inner = el("div", { class: "page-inner under-head" });
    page.append(inner);
    main.append(pageHead(el("div", { class: "row nowrap" }, el("div", { class: "grow stack0" }, el("h1", { class: "page-title" }, "Pull requests"),
      el("span", { class: "muted" }, "Open PRs from agent branches.")),
      el("button", { class: "btn", onclick: VIEWS.prs.load }, "Refresh"))), page);
    P.dest = el("input", { class: "input mono", placeholder: "/home/you/agent-builds", value: UI.buildDest || "", spellcheck: false });
    P.dest.addEventListener("input", () => { UI.buildDest = P.dest.value; });
    P.list = el("div", { class: "card flush" }, el("div", { class: "list-row muted" }, "loading…"));
    P.errors = el("div", { class: "err" });
    P.repo = el("select", { class: "select" });
    P.ref = el("input", { class: "input", placeholder: "Branch or PR number", "aria-label": "Branch or PR number" });
    P.art = el("input", { class: "input", placeholder: "Artifact (optional)", "aria-label": "Artifact" });
    P.result = el("pre", { class: "block muted" }, "No build fetched yet.");
    inner.append(isOwner() ? el("label", { class: "field" }, el("span", { class: "label" }, "Save builds to"), P.dest) : "",
      P.list, P.errors,
      ...(isOwner() ? [el("div", { class: "section-title" }, "Fetch a build manually"),
        el("div", { class: "row fetch-row" }, P.repo, P.ref, P.art, el("button", { class: "btn", onclick: () => {
          const ref = P.ref.value.trim();
          fetchBuild(P.repo.value, /^\d+$/.test(ref) ? { pr: +ref } : { branch: ref });
        } }, "Fetch")),
        P.result] : [el("div", { class: "muted" }, "Builds are fetched to a folder on your PC, so that's done from the PC.")]),
      el("div", { class: "note-box warn" }, "Builds come from agent-written code. Review the PR first, or run them in a VM."));
    VIEWS.prs.load();
  },
  update() {
    const P = VIEWS.prs.p;
    if (P && changed("prRepos", [STATE.repos])) { const k = P.repo.value; P.repo.replaceChildren(...STATE.repos.map((r) => el("option", { value: r }, r))); if (k) P.repo.value = k; }
  },
  async load() {
    const P = VIEWS.prs.p;
    try {
      const r = await api("GET", "/api/prs");
      P.list.replaceChildren(...(r.prs.length ? paged("prs", r.prs, (p) => el("div", { class: "list-row" },
        el("div", { class: "grow stack tight" }, el("div", { class: "row" }, el("span", { class: "title" }, p.title), p.draft ? el("span", { class: "tag" }, "Draft") : ""),
          el("span", { class: "muted mono ellipsis" }, `${p.repo} #${p.number} · ${p.branch} · ${fmtTime(p.updated_at)}`)),
        ghLink(p.url, "Open"),
        isOwner() ? el("button", { class: "btn small", onclick: () => fetchBuild(p.repo, { pr: p.number }) }, "Fetch build") : ""))
        : [el("div", { class: "list-row muted" }, "No open agent PRs. They show up here once an agent opens one.")]));
      P.errors.textContent = r.errors.map(prError).join("\n");
    } catch (e) { P.list.replaceChildren(loadFailed("the pull requests", e, VIEWS.prs.load)); P.errors.textContent = ""; }
  },
};
async function fetchBuild(repo, ref) {
  const P = VIEWS.prs.p, dest = P.dest.value.trim();
  if (!dest) { toast("Set “Save builds to” first.", true); return; }
  P.result.className = "block"; P.result.textContent = "Fetching…";
  try {
    const r = await api("POST", "/api/fetch-build", { repo, dest, artifact: P.art.value.trim(), ...ref });
    P.result.textContent = `Saved ${plural(r.files.length, "file")}, ${fmtBytes(r.bytes)} to\n${r.path}\n` +
      `artifact "${r.name}" from run ${r.run_id} (${String(r.head_sha).slice(0, 7)})\nExecutable bits were removed.` + (r.left_out ? ` ${leftOut(r.left_out)}` : "")
      + "\n\n" + r.files.join("\n");
  } catch (e) { P.result.textContent = "Error: " + e.message; }
}

// ---- Settings: one tab at a time; edits in every tab are kept until Save settings ----
const SETTINGS_TABS = [["secrets", "Secrets"], ["github", "GitHub access"], ["models", "Local models"],
  ["network", "Network"], ["images", "VM images"], ["vm", "VM limits"], ["host", "This PC"],
  ["speech", "Speech to text"], ["devices", "Devices"], ["appearance", "Appearance"], ["advanced", "Advanced"]];
const TAB_GROUP_STARTS = new Set(["network", "speech", "advanced"]);  // Connections, Runtime, Interface, Advanced: a divider before each after the first
const openSettings = (tab) => go("settings", { settingsTab: tab, sec: tab });  // a tab on the PC, its own screen on a phone
VIEWS.settings = {
  mount(main) {
    VIEWS.settings.p = { panels: {}, main, dirtyTabs: new Set() };
    VIEWS.settings.layout();
    VIEWS.settings.load();
    for (const t of ["input", "change", "click"]) main.addEventListener(t, () => setTimeout(VIEWS.settings.markDirty));  // after the handler that made the edit
  },
  // A list of the tabs on a phone, each opening its own screen, and tabs on the PC.  Crossing the
  // phone width moves the same panels into the other layout, so what you changed and haven't saved stays.
  layout() {
    const P = VIEWS.settings.p;
    P.phone = isPhone();
    if (P.phone) {
      P.body = el("div", { class: "page-inner m-set-page" }, VIEWS.settings.pending());
      P.main.replaceChildren(el("div", { class: "page" }, P.body));
    } else {
      if (!SETTINGS_TABS.some(([k]) => k === UI.settingsTab)) UI.settingsTab = "secrets";
      P.tabs = el("div", { class: "set-tabs", role: "tablist" });
      P.body = el("div", { class: "set-body" }, VIEWS.settings.pending());
      P.saveNote = el("span", { class: "warn small", hidden: true }, "Unsaved changes");
      P.main.replaceChildren(el("div", { class: "set-head" }, el("div", { class: "set-head-inner" },
        el("h1", { class: "page-title" }, "Settings"), P.tabs)),
      el("div", { class: "page" }, P.body),
      el("div", { class: "save-bar" }, P.saveNote, el("button", { class: "btn primary", onclick: VIEWS.settings.save }, "Save settings")));
    }
    if (P.c) VIEWS.settings.place();
  },
  pending() {
    const P = VIEWS.settings.p;
    return P.error ? loadFailed("the settings", P.error, VIEWS.settings.load) : el("div", { class: "muted" }, "loading…");
  },
  // What each saved-by-button screen holds, to tell which ones you edited since the last load or save
  signatures() {
    const P = VIEWS.settings.p, R = P.network.routes;
    return { github: JSON.stringify([Object.entries(P.github.chosen).map(([n, set]) => [n, [...set].sort()]), P.branches.mode, P.branches.prefix.value, P.github.idents]),
      network: JSON.stringify([P.network.allow.value, P.network.never, R.routes, R.route, R.own, R.doh, R.keep, R.pushOwn]),
      vm: JSON.stringify([P.cpus.value, P.mem.value, P.maxc.value, P.maxm.value, P.idle.value, P.yolo.value, P.yoloAll.classList.contains("on")]),
      speech: JSON.stringify([P.spOn.classList.contains("on"), P.spUrl.value, P.spMax.value]), advanced: P.json.value };
  },
  markDirty() {
    const P = VIEWS.settings.p;
    if (!P?.base) return;
    const now = VIEWS.settings.signatures();
    const before = [...P.dirtyTabs].join();
    P.dirtyTabs = new Set(Object.keys(now).filter((k) => now[k] !== P.base[k]));
    if (P.saveNote) P.saveNote.hidden = !P.dirtyTabs.size;
    if (P.footNote) P.footNote.textContent = P.dirtyTabs.has(UI.sec) ? "Unsaved changes" : "";
    if (!P.phone && P.tabs && [...P.dirtyTabs].join() !== before) VIEWS.settings.renderTabs();
  },
  update() {
    const P = VIEWS.settings.p;
    // This PC follows the host and a running build (it has nothing to type in)
    if (P?.thisPc?.isConnected && changed("thisPc", [STATE.tools, STATE.host, STATE.service, STATE.build, STATE.base_image])) P.thisPc.render();
    if (P?.look && !P.look.editing && changed("lookTab", [STATE.appearance])) P.look.render();  // changed elsewhere
    if (!P || P.phone === isPhone() || typingIn(P.main)) return;  // never under your hands
    if (P.phone) UI.settingsTab = UI.sec === "notifications" ? "devices" : UI.sec || UI.settingsTab;  // the tab whose screen was open
    else UI.sec = UI.settingsTab;
    VIEWS.settings.layout();
    renderMobileNav();
    syncBack();
  },
  place() {
    const P = VIEWS.settings.p;
    if (P.phone) { VIEWS.settings.showPhone(); return; }
    P.body.replaceChildren(...Object.values(P.panels));
    VIEWS.settings.showTab(UI.settingsTab);
  },
  showTab(key) {
    const P = VIEWS.settings.p;
    UI.settingsTab = key;
    for (const [k, node] of Object.entries(P.panels)) node.hidden = k !== key && !(k === "notifications" && key === "devices");  // one tab on the PC
    if (key === "github") P.github?.fetch();
    VIEWS.settings.renderTabs();
    lookBar();
  },
  // One line of tabs: the ones that don't fit go under More, but the open one always shows
  renderTabs() {
    if (VIEWS.settings.p.phone) return;  // a phone has the list instead
    const P = VIEWS.settings.p, missing = secretList(P.secrets || {}, P.c).some((s) => s.state.missing);
    const tab = ([k, label]) => el("button", { class: "tab" + (k === UI.settingsTab ? " on" : ""), role: "tab",
      "aria-selected": String(k === UI.settingsTab), onclick: () => VIEWS.settings.showTab(k) },
    label, k === "secrets" && missing ? el("span", { class: "tab-dot", title: "A secret is missing" }) : "", P.dirtyTabs?.has(k) ? el("span", { class: "tab-dot warn", title: "Unsaved changes" }) : "");
    const fits = (shown, rest) => {
      const more = rest.length ? el("button", { class: "tab", "aria-haspopup": "menu", onclick: () => openPop(more, el("div", { class: "menu" },
        ...rest.map(([k, label]) => el("button", { class: "menu-item", onclick: () => { closePop(); VIEWS.settings.showTab(k); } }, label)))) }, "More ", foldMark(true)) : "";
      P.tabs.replaceChildren(...shown.flatMap((t, i) => (i && TAB_GROUP_STARTS.has(t[0]) ? [el("span", { class: "tab-sep", "aria-hidden": "true" }), tab(t)] : [tab(t)])), more);
      return P.tabs.scrollWidth <= P.tabs.clientWidth + 1;
    };
    let n = SETTINGS_TABS.length;
    while (!fits(...VIEWS.settings.splitTabs(n)) && n > 1) n--;
    if (!P.tabsObserved && window.ResizeObserver) {  // a wider or narrower window: split them again
      P.tabsObserved = true;
      let w = 0;
      new ResizeObserver(() => { if (P.tabs.isConnected && P.tabs.clientWidth !== w) { w = P.tabs.clientWidth; VIEWS.settings.renderTabs(); } }).observe(P.tabs);
    }
  },
  splitTabs(n) {  // the first n tabs, with the open one swapped in for the last when it's further along
    const shown = SETTINGS_TABS.slice(0, n), open = SETTINGS_TABS.findIndex(([k]) => k === UI.settingsTab);
    if (open >= n && n > 0) shown[n - 1] = SETTINGS_TABS[open];
    return [shown, SETTINGS_TABS.filter((t) => !shown.includes(t))];
  },
  async load() {
    const P = VIEWS.settings.p;
    let S;
    P.error = null; P.base = null;
    if (P.body && !P.c) P.body.replaceChildren(VIEWS.settings.pending());
    try { S = await api("GET", "/api/settings"); } catch (e) { P.error = e; P.body.replaceChildren(VIEWS.settings.pending()); return; }
    const c = S.config;
    P.c = c; P.secrets = S.secrets; P.times = S.secret_times || {}; P.tokens = S.tokens || {}; P.dohResolvers = S.doh_resolvers || {};
    const num = (v, min, step) => el("input", { class: "input", type: "number", min, step: step || 1, value: v });
    P.secretsBox = el("div", { class: "stack" });
    P.github = githubAccess(c);
    P.branches = branchNames(c);
    P.network = networkTab(c);
    P.models = modelsTab(c);
    // speech
    P.spOn = sw(c.speech.enabled, (on, b) => setSw(b, on), "large");
    P.spUrl = el("input", { class: "input mono", placeholder: "http://192.168.1.50:8080", value: c.speech.whisper_url, spellcheck: false });
    P.spMax = num(c.speech.max_seconds, 5); P.spMax.max = 600;
    const spOut = el("span", { class: "muted" });
    // vm
    P.cpus = num(c.vm.cpus, 1); P.mem = num(c.vm.memory_mb, 512, 256); P.maxc = num(c.vm.max_cpus, 1); P.maxm = num(c.vm.max_memory_mb, 512, 256);
    P.idle = el("select", { class: "input", "aria-label": "Stop when idle" }, IDLE_STOP.map(([v, label]) => el("option", { value: v }, label)));
    P.idle.value = String(c.vm.idle_stop_minutes);
    // YOLO: how many questions in a row an agent may send without you (null = no limit)
    const yr = c.research.yolo_rounds;
    P.yolo = num(yr ?? 3, 1); P.yolo.max = 100; P.yolo.disabled = yr === null;
    P.yoloAll = sw(yr === null, (on, b) => { setSw(b, on); P.yolo.disabled = on; });
    P.json = el("textarea", { class: "editor", rows: 24, spellcheck: false, value: JSON.stringify(c, null, 2), "aria-label": "Full configuration" });
    const setRow = (label, desc, control, tip) => el("div", { class: "opt-sec row set-row" }, el("div", { class: "grow stack0" }, el("span", {}, label, tip ? infoTip(tip) : ""),
      desc ? el("span", { class: "muted small pretty" }, desc) : ""), control);  // a label and what it does on the left, its control on the right
    const field = (label, input, cls, tip) => el("label", { class: "field" + (cls ? " " + cls : "") }, el("span", { class: "label" }, label, tip ? infoTip(tip) : ""), input);
    P.panels = {
      secrets: setPanel(["Secrets", infoTip((STATE.can_save_secrets ? "Pasted values are saved encrypted on this PC while Save on this PC is on, else kept in the panel's memory until it stops. "
        : "Pasted values stay in the panel's memory until it stops. Saving them on this PC needs systemd 256 or newer. ")
        + "Secrets from a command, like pass or secret-tool, are read the first time an agent needs them. Unlock now reads them all, so any password prompt comes while you're here.")],
        "API keys and tokens agents use. Values are never shown here.", P.secretsBox),
      github: setPanel(["GitHub access", infoTip("The list comes from GitHub: these are the repositories the token can reach. "
        + "Ticked ones show up when you create an agent. To add one that isn't listed, give the token access on GitHub, then refresh.")], "Pick which repositories agents may work in. Agents get the token only for the ticked ones.",
        P.github.node, P.branches.node),
      network: setPanel("Network", "", P.network.node),
      models: setPanel(["Local models", infoTip("cilly runs none of these. Agents reach a server only through their egress proxy, and only while their model is on it. "
        + "Only Anthropic's Messages API gets through, and only for the model you picked, so an agent can't pull or delete models. "
        + "The server must speak it: Ollama 0.14 or later, llama.cpp, vLLM and LM Studio do.")], "Model servers you already run, like Ollama, llama.cpp, vLLM or LM Studio. "
        + "An agent set to one of their models uses it instead of Claude's. Changes here are saved right away.", P.models.node),
      images: setPanel("VM images", "Agents start from these. Changes apply at an agent's next start.", imagesSection()),
      vm: setPanel("VM limits", "Defaults for new agents, and the most any agent may ask for.",
        el("div", { class: "card flush" },
          setRow("Default vCPUs", "For a new agent", P.cpus), setRow("Max vCPUs", "The most an agent may ask for", P.maxc),
          setRow("Default memory (MiB)", "For a new agent", P.mem), setRow("Max memory (MiB)", "The most an agent may ask for", P.maxm),
          setRow("Stop when idle", "For persistent agents without their own setting", P.idle, "Stop when idle is for persistent agents that don't have their own setting. "
            + "Ephemeral, ephemeral + memory and research agents start with it off. Change it per agent in its ··· menu."),
          setRow("YOLO round limit", "Questions in a row an agent in YOLO mode may send without review. After that, new ones wait in the inbox until you message the agent.",
            el("div", { class: "row nowrap" }, P.yolo, el("label", { class: "row nowrap" }, P.yoloAll, el("span", {}, "No limit"))),
            "For agents in YOLO mode, where research questions go out without your review. After this many in a row, new questions wait in the inbox until you message the agent."))),
      host: setPanel("This PC", "What cilly needs on this PC, and starting it when you log in.", (P.thisPc = thisPcSection())),
      speech: setPanel("Speech to text", "Your own whisper.cpp server.",
        el("div", { class: "card stack" },
          el("label", { class: "row nowrap" }, el("div", { class: "grow stack tight" }, el("span", {}, "Show the microphone"),
            el("span", { class: "muted small pretty" }, "Audio goes from your browser to the panel, then to the server below. Nothing is sent anywhere else.")), P.spOn),
          el("div", { class: "set-grid" }, field("whisper-server URL", P.spUrl, "wide"), field("Max recording (seconds)", P.spMax)),
          el("div", { class: "row" }, el("button", { class: "btn small", onclick: async () => {
            spOut.textContent = "testing…"; spOut.className = "muted";
            try {
              const r = await api("POST", "/api/speech/test", { url: P.spUrl.value.trim() });
              spOut.textContent = r.ok ? `Connected, answered in ${r.ms} ms` : r.error; spOut.className = r.ok ? "ok" : "err";
            } catch (e) { spOut.textContent = e.message; spOut.className = "err"; }
          } }, "Test connection"), spOut),
          el("div", { class: "dim small pretty" }, "whisper-server has no login or HTTPS, so only reach it over a network you trust, like Tailscale, WireGuard or an SSH tunnel."))),
      devices: setPanel(["Paired devices", infoTip(PAIRED_SHORT)], "Phones and other browsers you paired.", pairedLists(), pairedSection()),
      notifications: setPanel(["Notifications", infoTip(NOTIFY_ABOUT)], "", deviceSection()),
      appearance: setPanel(["Appearance", infoTip("How the panel looks, here and on your paired devices. A click on a theme changes it for all of them.")], "",
        (P.look = appearanceSection(P.tokens)).node),
      advanced: setPanel("Full configuration", "Everything on the other tabs, as JSON. Secret values are never included. "
        + "When you save, values you changed on the other tabs win over edits here.", P.json,
      el("div", { class: "muted" }, "Commands that run on this PC can only be changed in the config file.")),
    };
    renderSecrets();
    VIEWS.settings.place();
    P.base = VIEWS.settings.signatures();
    VIEWS.settings.markDirty();
  },
  open(key) {  // a phone: one panel, or the list (null)
    UI.sec = key;
    VIEWS.settings.showPhone();
    lookBar();
    renderMobileNav();
    syncBack();
    VIEWS.settings.p.body.parentElement.scrollTop = 0;
  },
  showPhone() {
    const P = VIEWS.settings.p, panel = P.panels[UI.sec];
    if (panel) {
      panel.hidden = false;
      if (UI.sec === "github") P.github.fetch();
      P.footNote = el("span", { class: "grow small warn" });
      P.body.replaceChildren(el("div", { class: "row nowrap m-proj-top" }, el("button", { class: "link m-back-link", onclick: () => VIEWS.settings.open(null) }, "← Settings")), panel,
        SAVED_BY_BUTTON.has(UI.sec) ? el("div", { class: "m-foot" }, el("div", { class: "row nowrap" }, P.footNote, el("button", { class: "btn primary", onclick: VIEWS.settings.save }, "Save settings"))) : "");
      VIEWS.settings.markDirty();
      if (UI.sec === "devices") refreshPaired();  // its list loads only once it's on the page
      if (UI.sec === "images") imagesKick?.();  // the build's progress too
      return;
    }
    UI.sec = null; P.footNote = null;
    const s = settingsSummaries(P.c, P.secrets);
    const row = (key, label, value, cls, dot) => el("button", { class: "m-set-row", onclick: () => (key === "policy" ? go("policy") : VIEWS.settings.open(key)) },
      dot ? el("span", { class: "dot s-failed" }) : "", el("span", { class: "grow" }, label), P.dirtyTabs?.has(key) ? el("span", { class: "warn small" }, "Unsaved") : "",
      el("span", { class: cls || "muted" }, value), el("span", { class: "chev", "aria-hidden": "true" }, "›"));
    const paired = el("span");  // filled in when the list of devices comes
    P.body.replaceChildren(el("h1", { class: "m-h1" }, "Settings"),
      el("div", { class: "m-set-list" }, row("secrets", "Secrets", s.secrets, s.missing ? "err" : "", s.missing),
        row("github", "GitHub access", s.github), row("network", "Network", s.network), row("models", "Local models", s.models),
        row("images", "VM images", s.images), row("vm", "VM limits", s.vm),
        row("host", "This PC", s.host, hostProblems().length ? "err" : "")),
      el("div", { class: "m-set-list" }, row("devices", "Devices", paired), row("notifications", "Notifications", s.notifications), row("speech", "Speech to text", s.speech),
        row("appearance", "Appearance", s.appearance)),
      el("div", { class: "m-set-list" }, row("policy", "Claude policy", s.policy), row("advanced", "Advanced", "JSON")),
      el("div", { class: "sb-host m-host" }, ...hostStatus()));
    api("GET", "/api/devices").then((r) => { paired.textContent = `${r.devices.length} paired`; }).catch(() => {});
  },
  async save() {
    const P = VIEWS.settings.p;
    if (!P.c) return;
    let c;
    try { c = JSON.parse(P.json.value); } catch { VIEWS.settings.showTab("advanced"); toast("The full configuration is not valid JSON.", true); return; }
    // the other tabs win over the raw JSON for the fields they show
    c.egress.allow = lines(P.network.allow.value);
    c.egress.never_reach = [...P.network.never];
    c.model_servers = P.models.servers;
    const R = P.network.routes;  // the Routes part of the Network tab
    Object.assign(c.egress, { routes: R.routes, route: R.route, route_own: R.own, doh: R.doh,
      keep_tokens: R.keep, push_own_branches: R.pushOwn });
    c.base_image = STATE.base_image || c.base_image;  // Make default saved it already; keep it
    c.research_base_image = STATE.research_base_image || c.research_base_image;
    c.research.yolo_rounds = P.yoloAll.classList.contains("on") ? null : Math.max(1, Math.min(100, Math.round(+P.yolo.value || 3)));
    c.vm.cpus = +P.cpus.value; c.vm.memory_mb = +P.mem.value; c.vm.max_cpus = +P.maxc.value; c.vm.max_memory_mb = +P.maxm.value;
    c.vm.idle_stop_minutes = +P.idle.value;
    c.speech = Object.assign(c.speech || {}, { enabled: P.spOn.classList.contains("on"), whisper_url: P.spUrl.value.trim(), max_seconds: +P.spMax.value });
    for (const [name, chosen] of Object.entries(P.github.chosen)) if (c.tokens[name]) c.tokens[name].repos = [...chosen].sort();
    for (const [name, id] of Object.entries(P.github.idents)) if (c.tokens[name]) c.tokens[name].identity = { name: id.name, email: id.email };
    c.identity = Object.assign(c.identity || {}, { branch_names: P.branches.mode, branch_prefix: branchPrefix(P.branches.prefix.value) });
    c.appearance = savedAppearance();  // the Appearance tab saves right away: keep what it saved
    try { await api("PUT", "/api/settings", { config: c }); toast("Saved."); await loadState(); VIEWS.settings.load(); }
    catch (e) { toast(e.message, true); }
  },
};
const NOTIFY_ABOUT = "Shown by this browser while the panel is open in it, even in a background tab. They show the agent's state, never your message.";
const SAVED_BY_BUTTON = new Set(["github", "network", "vm", "speech", "advanced"]);  // the rest act right away
// The phone's Settings list: one short line per tab
function settingsSummaries(c, secrets) {
  const list = secretList(secrets, c), missing = list.filter((x) => x.state.missing).length, locked = list.filter((x) => x.state.locked).length;
  const repos = new Set(Object.values(c.tokens || {}).flatMap((t) => t.repos || []));
  const img = (STATE.images || []).find((i) => i.name === STATE.base_image);
  const gib = (mb) => `${+(mb / 1024).toFixed(1)} GiB`;
  let host = "";
  try { host = new URL(c.speech.whisper_url).hostname; } catch { /* not set */ }
  return {
    missing, secrets: missing ? `${missing} missing` : locked ? `${locked} locked` : list.length ? "All set" : "None",
    github: plural(repos.size, "repo"), network: plural(c.egress.allow.length, "host"),
    models: Object.keys(c.model_servers || {}).length ? plural(Object.keys(c.model_servers).length, "server") : "None", images: img ? imageLabel(img) : "Default",
    vm: `${c.vm.max_cpus} vCPU, ${gib(c.vm.max_memory_mb)}`, notifications: notePrefs().on ? "On" : "Off",
    host: hostProblems().length ? "Not ready" : "Ready",
    speech: c.speech.enabled ? host.replace(/\.local$/, "") || "On" : "Off", policy: `v${STATE.policy_version}`,
    appearance: STATE.appearance?.themes[STATE.appearance.theme]?.name || "Dystopian",
  };
}
// Settings → Appearance: the themes (a click makes one the standard), an editor that previews on the
// whole panel as you go, and fonts you added.  Everything here saves right away.
const COLOR_GROUPS = [["Accent", /accent|lavender/], ["Backgrounds", /bg|surface|hover/], ["Text", /^--(fg|on-|chat-fg|sidebar-fg)/],
  ["Lines", /line|grabber/], ["Status", /^--(ok|warn|err|danger|yolo)/], ["Code", /^--(hl|ansi)-/], ["Other", /./]];
const THEME_FIELDS = ["name", "style", "colors", "font", "heading", "prose", "mono", "size", "prose_size", "mono_size"];
function savedAppearance() {
  const a = STATE.appearance;
  return { theme: a.theme, themes: Object.fromEntries(Object.entries(a.themes).filter(([, t]) => !t.builtin)
    .map(([id, t]) => [id, Object.fromEntries(THEME_FIELDS.map((k) => [k, t[k]]))])) };
}
function fontFamily(key) {
  const a = STATE.appearance;
  if (a.builtin_fonts[key]) return a.builtin_fonts[key].family;
  loadFont(key);
  return `"cilly-${key}", ${a.builtin_fonts.geist.family}`;
}
function appearanceSection(tokens) {
  const S = { node: el("div", { class: "stack" }), editing: LOOK.draft };  // a draft you left to look around in is still here
  const color = (t, k) => t.colors[k] || tokens[k];
  const preview = (t) => {
    const box = el("span", { class: "theme-prev" }), side = el("span", { class: "side" }), main = el("span", { class: "main" });
    const line = (k, cls) => { const x = el("span", { class: "ln " + (cls || "") }); x.style.background = color(t, k); return x; };
    const bubble = el("span", { class: "bub" }), dot = el("span", { class: "dot" });
    box.style.background = color(t, "--bg"); side.style.background = color(t, "--bg-sidebar");
    bubble.style.background = color(t, "--surface"); dot.style.background = color(t, "--accent");
    side.append(line("--fg-3", "short"), line("--fg-5"), line("--fg-5", "short"));
    main.append(bubble, line("--fg"), line("--fg-2", "short"), el("span", { class: "row nowrap" }, dot, line("--ok", "short")));
    box.append(side, main);
    return box;
  };
  const use = async (id) => {
    try { await api("PUT", "/api/appearance", { theme: id }); await loadState(); S.render(); } catch (e) { toast(e.message, true); }
  };
  const remove = async (id, t) => {
    if (!await confirmModal(`Delete ${t.name}?`, "If it's the theme in use, the panel goes back to Dystopian.", "Delete", true)) return;
    try { await api("POST", `/api/themes/${id}/delete`, {}); await loadState(); S.render(); } catch (e) { toast(e.message, true); }
  };
  S.edit = (id) => {
    const t = STATE.appearance.themes[id];
    const copy = Object.fromEntries(THEME_FIELDS.map((k) => [k, t[k]]));
    S.editing = LOOK.draft = { id: t.builtin ? null : id, theme: { ...copy, name: t.builtin ? `${t.name} copy` : t.name, colors: { ...t.colors } } };
    S.render();
  };
  S.render = () => {
    if (S.editing) return S.renderEditor();
    const a = STATE.appearance;
    const cards = Object.entries(a.themes).map(([id, t]) => {
      const on = id === a.theme, name = el("span", { class: "grow" }, t.name);
      name.style.fontFamily = fontFamily(t.font);
      return el("div", { class: "theme-card" + (on ? " on" : "") },
        el("button", { class: "theme-pick", type: "button", "aria-pressed": String(on), title: on ? "In use" : "Use this theme", onclick: () => on || use(id) },
          preview(t), el("span", { class: "theme-name" }, name, on ? el("span", { class: "tag accent" }, "In use") : "")),
        el("div", { class: "row nowrap theme-acts" },
          el("button", { class: "link", type: "button", onclick: () => S.edit(id) }, t.builtin ? "Customize" : "Edit"),
          t.builtin ? "" : el("button", { class: "link danger", type: "button", onclick: () => remove(id, t) }, "Delete")));
    });
    S.node.replaceChildren(el("div", { class: "theme-grid" }, ...cards), fontsSection());
  };
  const fontsSection = () => {
    const a = STATE.appearance, file = el("input", { type: "file", hidden: true, accept: ".woff2,.woff,.ttf,.otf" });
    file.addEventListener("change", async () => {
      const f = file.files[0];
      file.value = "";
      if (!f) return;
      try {
        const name = f.name.replace(/\.(woff2?|ttf|otf)$/i, "").replace(/[-_]+/g, " ").trim();
        await api("POST", "/api/fonts", { name, data: await fileToBase64(f) });
        toast(`Added ${name}.`); await loadState(); S.render();
      } catch (e) { toast(e.message, true); }
    });
    const rows = a.fonts.map((f) => {
      const sample = el("span", { class: "grow font-sample" }, f.name);
      sample.style.fontFamily = fontFamily(f.id);
      return el("div", { class: "list-row" }, sample, el("button", { class: "link danger", type: "button", onclick: async () => {
        if (!await confirmModal(`Remove ${f.name}?`, "Themes that use it go back to Geist.", "Remove", true)) return;
        try { await api("POST", `/api/fonts/${f.id}/delete`, {}); await loadState(); S.render(); } catch (e) { toast(e.message, true); }
      } }, "Remove"));
    });
    return el("div", { class: "stack" },
      setSubTip("Fonts", "Add WOFF2, WOFF, TTF or OTF files to use them in your themes. They stay on this PC, "
          + "and paired devices load them from the panel. Fonts you add aren't part of cilly, so check their license allows this."),
      rows.length ? el("div", { class: "card flush" }, ...rows) : "",
      el("div", { class: "row" }, el("button", { class: "btn small", type: "button", onclick: () => file.click() }, "Add font"), file));
  };
  S.renderEditor = () => {
    const E = S.editing, t = E.theme, a = STATE.appearance;
    const show = () => { LOOK.preview = { ...t, colors: { ...t.colors } }; applyLook(); };
    const fonts = [...Object.entries(a.builtin_fonts).map(([k, f]) => [k, f.name]), ...a.fonts.map((f) => [f.id, f.name])];
    const pick = (key, label, same) => {
      const s = el("select", { class: "input", "aria-label": label }, (same ? [["", "Same as font"], ...fonts] : fonts).map(([k, n]) => el("option", { value: k }, n)));
      s.value = t[key];
      s.addEventListener("change", () => { t[key] = s.value; show(); });
      return el("label", { class: "field" }, el("span", { class: "label" }, label), s);
    };
    const size = (key, label) => {
      const s = el("select", { class: "input", "aria-label": label }, Array.from({ length: 16 }, (_, i) => 75 + i * 5).map((v) => el("option", { value: v }, `${v}%`)));
      s.value = String(t[key]);
      s.addEventListener("change", () => { t[key] = +s.value; show(); });
      return el("label", { class: "field" }, el("span", { class: "label" }, label), s);
    };
    const name = el("input", { class: "input", value: t.name, maxlength: 40, "aria-label": "Name" });
    const styleSel = el("select", { class: "input", "aria-label": "Style" }, [["classic", "Classic"], ["terminal", "Terminal"]].map(([k, n]) => el("option", { value: k }, n)));
    styleSel.value = t.style || "classic";
    styleSel.addEventListener("change", () => { t.style = styleSel.value; show(); });
    const look = el("label", { class: "field" }, el("span", { class: "label" }, "Style"), styleSel,
      el("span", { class: "dim small pretty" }, "Terminal has square corners, pixel icons and pixel cursors."));
    name.addEventListener("input", () => { t.name = name.value; });
    const groups = new Map(COLOR_GROUPS.map(([g]) => [g, []]));
    for (const k of Object.keys(tokens)) groups.get(COLOR_GROUPS.find(([, rx]) => rx.test(k))[0]).push(k);
    const row = (k) => {
      const swatch = el("input", { type: "color", value: color(t, k), "aria-label": k.slice(2) });
      const hex = el("input", { class: "input mono hex", value: color(t, k), spellcheck: false, maxlength: 7, "aria-label": k.slice(2) + " value" });
      const set = (v) => { t.colors[k] = v; show(); };
      swatch.addEventListener("input", () => { hex.value = swatch.value; set(swatch.value); });
      hex.addEventListener("input", () => { const v = hex.value.trim().toLowerCase(); if (/^#[0-9a-f]{6}$/.test(v)) { swatch.value = v; set(v); } });
      return el("div", { class: "color-row" }, swatch, el("span", { class: "name" }, k.slice(2)), hex);
    };
    const save = async () => {
      try {
        const r = await api("POST", E.id ? `/api/themes/${E.id}` : "/api/themes", t);
        await api("PUT", "/api/appearance", { theme: r.id });
        S.editing = LOOK.draft = LOOK.preview = null;
        await loadState(); S.render(); toast("Saved. The panel uses it now.");
      } catch (e) { toast(e.message, true); }
    };
    const cancel = () => { S.editing = LOOK.draft = LOOK.preview = null; applyLook(); S.render(); };
    S.node.replaceChildren(
      el("div", { class: "muted pretty" }, "The whole panel shows your changes as you make them, also on other pages. "
        + "Nothing is saved until you press Save and use."),
      el("div", { class: "set-grid one" }, el("label", { class: "field" }, el("span", { class: "label" }, "Name"), name)),
      el("div", { class: "set-grid" }, pick("font", "Font"), size("size", "Text size"), pick("heading", "Heading font", true), look),
      el("div", { class: "set-grid" }, pick("prose", "Chat font", true), size("prose_size", "Chat text size"), pick("mono", "Code font"), size("mono_size", "Code text size")),
      ...[...groups].filter(([, ks]) => ks.length).map(([g, ks], i) => el("details", { class: "adv", open: i < 2 },
        el("summary", {}, g, el("span", { class: "dim" }, String(ks.length))), el("div", { class: "color-grid" }, ...ks.map(row)))),
      el("div", { class: "row" }, el("button", { class: "btn primary", type: "button", onclick: save }, "Save and use"),
        el("button", { class: "btn", type: "button", onclick: cancel }, "Cancel")));
    show();
  };
  if (STATE.appearance) S.render();
  return S;
}
const setSubTip = (title, ...text) => el("div", { class: "set-sub" }, el("span", { class: "title-row" }, el("span", { class: "title" }, title), infoTip(...text)));
function setPanel(title, sub, ...body) {
  return el("section", { class: "set-panel", role: "tabpanel", hidden: true },
    el("div", { class: "set-sub" }, el("span", { class: "title big" }, title), sub ? el("span", { class: "muted pretty" }, sub) : ""), ...body);
}
function lines(s) { return s.split("\n").map((x) => x.trim()).filter(Boolean); }

// Settings → Secrets.  Statuses only: a value never comes back from the server, not even a part of it.
function secretState(key, st) {
  if (st === "missing") return key === "panel_token" ? { word: "Not set", cls: "muted", rank: 2 } : { word: "Missing", cls: "err", rank: 0, missing: true };
  if (st === "command (not loaded yet)") return { word: "Locked", cls: "warn", rank: 1, locked: true };
  if (st === "command failed, using pasted") return { word: "Pasted, its command failed", cls: "warn", rank: 2 };
  if (st.startsWith("saved on this PC")) return { word: "Saved on this PC", cls: "ok", rank: 2 };
  if (st === "command (loaded)") return { word: "From command", cls: "ok", rank: 2 };
  return { word: "Pasted", cls: "ok", rank: 2 };
}
// The name agents know it by, what it's for, and who uses it now (from the agents' settings)
function secretAbout(key, cfg) {
  const [kind, a, b] = key.split(":");
  if (kind === "agent") return { name: b, about: `${cfg?.agents?.[a]?.label || a} in coding agents` };
  if (kind === "research") return { name: a, about: "Research agents" };
  if (kind === "token") return { name: "GH_TOKEN", about: `GitHub token “${a}”` };
  if (kind === "mcp") return { name: a, about: "MCP server key" };
  if (kind === "route") return cfg?.egress?.routes?.[a]?.kind === "wireguard"
    ? { name: `Keys for ${a}`, about: "WireGuard private key, then its preshared key if it has one" }
    : { name: `Password for ${a}`, about: "Route login" };
  if (kind === "model") return { name: `API key for ${a}`, about: "Model server login. Agents never see it" };
  if (kind === "model-ssh") return { name: `SSH key for ${a}`, about: "Made by cilly for its tunnel. The public key is under Local models" };
  if (key === "panel_token") return { name: "Panel GitHub token", about: "Optional. Public repositories work without it" };
  return { name: key, about: "" };
}
function secretUsers(key, cfg, agents, lib) {
  const [kind, a] = key.split(":");
  let users;
  if (key === "panel_token") return "Used by the Pull requests page";
  if (kind === "route") {
    users = agents.filter((x) => (x.route || inheritedRoute(x.project)) === a);
    return users.length ? usedBy(users) : "Not used by any agent yet";
  }
  if (kind === "model" || kind === "model-ssh") {
    users = agents.filter((x) => x.model_server === a);
    return users.length ? usedBy(users) : "Not used by any agent yet";
  }
  if (kind === "agent") users = agents.filter((x) => x.profile !== "research" && (x.agent || "claude") === a);
  else if (kind === "research") users = agents.filter((x) => x.profile === "research");
  else if (kind === "token") users = agents.filter((x) => x.repo && (cfg?.tokens?.[a]?.repos || []).includes(x.repo));
  else if (kind === "mcp") {
    const servers = Object.entries(lib || {}).filter(([, s]) => s.secrets.includes(a)).map(([n]) => n);
    users = agents.filter((x) => (x.mcp || []).some((n) => servers.includes(n)));
    const via = servers.length ? ` (MCP: ${servers.join(", ")})` : "";
    return (users.length ? usedBy(users) : "Not used by any agent yet") + via;
  } else users = [];
  return users.length ? usedBy(users) : "Not used by any agent yet";
}
const usedBy = (agents) => (agents.length > 3 ? `Used by ${agents.length} agents` : `Used by ${agents.map((x) => x.name).join(", ")}`);
// missing, then locked, then the ones that are set
function secretList(statuses, cfg) {
  return Object.entries(statuses).map(([key, st]) => ({ key, st, state: secretState(key, st), ...secretAbout(key, cfg) }))
    .sort((x, y) => x.state.rank - y.state.rank || x.key.localeCompare(y.key));
}
function secretSummary(list) {
  const miss = list.filter((s) => s.state.missing).length, locked = list.filter((s) => s.state.locked).length;
  const parts = [miss ? `${miss} missing` : "", locked ? `${locked} locked until an agent needs ${locked === 1 ? "it" : "them"}` : ""].filter(Boolean);
  return `${list.length} secret${list.length === 1 ? "" : "s"}` + (parts.length ? ": " + parts.join(", ") : ", all available");
}
function renderSecrets() {
  const P = VIEWS.settings.p;
  if (!P?.secretsBox) return;
  const list = secretList(P.secrets, P.c), locked = list.some((s) => s.state.locked);
  const cmdOf = (key) => {
    const ref = key === "panel_token" ? P.c.panel_token?.secret : key.startsWith("token:") ? P.c.tokens[key.slice(6)]?.secret
      : key.startsWith("mcp:") ? P.c.mcp_secrets?.[key.slice(4)] : key.startsWith("research:") ? P.c.research.secrets?.[key.split(":")[1]]
        : key.startsWith("route:") ? P.c.egress.routes?.[key.slice(6)]?.secret
        : key.startsWith("model:") ? P.c.model_servers?.[key.slice(6)]?.secret
        : key.startsWith("model-ssh:") ? P.c.model_servers?.[key.slice(10)]?.ssh_secret
        : P.c.agents[key.split(":")[1]]?.secrets?.[key.split(":")[2]];
    return ref?.command?.[0] === "systemd-creds" ? "" : (ref?.command || []).join(" ");
  };
  const rows = list.map((s) => {
    const fromCmd = s.st.startsWith("command"), cmd = cmdOf(s.key);
    const sub = [secretUsers(s.key, P.c, STATE.agents_list, STATE.mcp_servers), storedText(s.st, P.times[s.key], cmd)].filter(Boolean).join(" · ");
    const editor = el("div", { class: "sec-edit row nowrap", hidden: true });
    const row = el("div", { class: "sec-row" + (s.state.missing ? " missing" : "") },
      el("div", { class: "sec-grid" }, el("span", { class: "dot " + s.state.cls }),
        el("div", { class: "stack0" }, el("span", { class: "ellipsis", title: s.key }, el("span", { class: /^[A-Z0-9_]+$/.test(s.name) ? "mono" : "" }, s.name), el("span", { class: "dim small" }, "  " + s.about)),
          el("span", { class: "muted small ellipsis", title: sub }, sub)),
        el("div", { class: "row nowrap sec-acts" }, el("span", { class: "small " + s.state.cls }, s.state.word),
          !fromCmd ? el("button", { class: "btn small ghost", onclick: () => openEditor() }, s.st === "missing" ? "Set value" : "Replace") : "",
          s.st !== "missing" ? el("button", { class: "btn small ghost", onclick: () => forgetSecret(s, fromCmd) }, "Forget") : "")),
      editor);
    const openEditor = () => {
      const inp = el("input", { class: "input mono grow", type: "password", autocomplete: "off", spellcheck: false, placeholder: "Paste the value", "aria-label": `Value for ${s.name}` });
      const close = () => { inp.value = ""; editor.hidden = true; editor.replaceChildren(); };
      const save = async () => { if (inp.value.trim() && await putSecret(s.key, inp.value)) close(); };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); else if (e.key === "Escape") { e.stopPropagation(); close(); } });
      editor.replaceChildren(inp, el("button", { class: "btn small ghost", onclick: close }, "Cancel"), el("button", { class: "btn small primary", onclick: save }, "Save"));
      editor.hidden = false;
      inp.focus();
    };
    return row;
  });
  const unlock = el("button", { class: "btn small accent", onclick: async () => {
    unlock.disabled = true; unlock.textContent = "Unlocking…";
    try {
      const r = await api("POST", "/api/secrets/load", {});
      const bad = Object.entries(r.results).filter(([, v]) => v !== "loaded");
      toast(bad.length ? bad.map(([k, v]) => `${k}: ${v}`).join("; ") : `Unlocked ${plural(Object.keys(r.results).length, "secret")}.`, bad.length > 0);
    } catch (e) { toast(e.message, true); }
    refreshSecrets();
  } }, "Unlock now");
  P.secretsBox.replaceChildren(
    el("div", { class: "card flush" }, el("div", { class: "sec-summary row nowrap" }, el("span", { class: "grow muted" }, secretSummary(list)), locked ? unlock : ""), rows,
      list.length ? "" : el("div", { class: "list-row muted" }, "No secrets yet. They show up here once a token, a route or an MCP server needs one.")),
    saveSwitch((on) => { P.saveSecrets = on; }));
  VIEWS.settings.renderTabs();
}
// How it's stored and when it was set (times from the host; never any part of the value)
function storedText(st, t, cmd) {
  const when = (sec) => (sec ? ago(new Date(sec * 1000).toISOString()) : "");
  if (st === "missing") return "";
  if (st.startsWith("saved on this PC")) return `Saved on this PC ${when(t?.saved)}`.trim();
  if (st.startsWith("pasted") || st === "command failed, using pasted") return ["Pasted", when(t?.pasted)].filter(Boolean).join(" ") + ", kept in memory";
  return `From ${cmd ? "the command " + cmd : "a command"}` + (t?.read ? `, read ${when(t.read)}` : ", not read yet");
}
async function refreshSecrets() {
  try { Object.assign(VIEWS.settings.p, (({ secrets, secret_times: times }) => ({ secrets, times }))(await api("GET", "/api/settings"))); } catch (e) { toast(e.message, true); }
  await loadState();
  renderSecrets();
}
async function putSecret(key, value) {
  try {
    const r = await pasteSecret(key, value, VIEWS.settings.p.saveSecrets !== false);
    if (!r.note) toast(r.status.startsWith("saved") ? "Saved on this PC." : "Kept in memory until the panel stops.");
  } catch (e) { toast(e.message, true); return false; }
  await refreshSecrets();
  return true;
}
async function forgetSecret(s, fromCmd) {
  if (!(await confirmModal(`Forget ${s.name}?`, fromCmd ? "The value is dropped from memory. It's read from its command again the next time an agent needs it."
    : "The value is dropped from memory, and from this PC if it was saved here. Agents that use it are asked for it again.", "Forget", true))) return;
  try { await api("POST", "/api/secrets", { key: s.key, forget: true }); toast(`Forgot ${s.name}`); } catch (e) { toast(e.message, true); }
  await refreshSecrets();
}

// Settings → GitHub access: per token, a checkbox list of the repositories GitHub says it reaches.
// Ticked ones are the token's repos in the config.  One that's ticked but missing from GitHub's list
// (access taken away, or past the first 300) stays, shown as such, until you untick it.
function repoRows(listed, chosen) {
  const rows = listed.map((r) => ({ ...r, usable: !!r.permissions?.push }));
  for (const name of chosen) if (!rows.some((r) => r.full_name === name)) rows.push({ full_name: name, missing: true, usable: true });
  return rows;
}
// Each token is a bot account.  The tab shows one account at a time, picked at the top: who its agents
// commit as, and its repositories.  A repository more than one account has ticked goes to the first of
// them, unless an agent's project or New agent picks another.
function sharedRepos(chosen) {  // repo -> the accounts that have it ticked, in the config's order, for the ones with more than one
  const by = {};
  for (const [name, set] of Object.entries(chosen)) for (const r of set) (by[r] ||= []).push(name);
  return Object.fromEntries(Object.entries(by).filter(([, names]) => names.length > 1));
}
const acctAvatar = (login) => el("span", { class: "acct-avatar", "aria-hidden": "true" }, (login || "?")[0].toUpperCase());
function githubAccess(c) {
  const chosen = {}, idents = {}, cards = {}, draws = {}, G = { node: el("div", { class: "stack" }), chosen, idents };
  const names = Object.keys(c.tokens);
  const shared = { name: c.identity?.name || "", email: c.identity?.email || "" };
  G.cur = names.includes(UI.ghAccount) ? UI.ghAccount : names[0];
  const login = (n) => idents[n]?.name || n;
  const note = el("div", { class: "note-box" }), pick = el("div", { class: "stack tight" });
  const drawNote = () => {
    const sh = Object.entries(sharedRepos(chosen));
    note.hidden = !sh.length;
    if (sh.length === 1) {
      const [repo, holders] = sh[0];
      note.textContent = `${repo} is under ${holders.map(login).join(" and ")}. Agents get ${login(holders[0])} for it, `
        + `unless their project or New agent picks another account.`;
    } else if (sh.length) {
      note.textContent = `${sh.length} repositories are under more than one account. Agents get the first account in the list for each, `
        + `unless their project or New agent picks another.`;
    }
  };
  const drawPick = () => {
    if (!G.cur) { pick.replaceChildren(); return; }
    const id = idents[G.cur];
    const btn = el("button", { class: "acct-pick", type: "button", "aria-haspopup": "menu", "aria-label": `Account ${login(G.cur)}. Switch or add an account` },
      acctAvatar(login(G.cur)), el("span", { class: "stack0 grow" }, el("span", { class: "ellipsis" }, login(G.cur)),
        el("span", { class: "muted small ellipsis" }, [id.email, `token ${G.cur}`].join(" · "))),
      names.length > 1 ? el("span", { class: "muted small nowrap" }, plural(names.length, "account")) : "", foldMark(true));
    btn.onclick = () => openPop(btn, el("div", { class: "menu acct-menu", role: "menu" },
      ...names.map((n) => el("button", { class: "menu-item" + (n === G.cur ? " on" : ""), role: "menuitemradio", "aria-checked": String(n === G.cur),
        onclick: () => { closePop(); show(n); } },
        acctAvatar(login(n)), el("span", { class: "grow stack0" }, el("span", { class: "ellipsis" }, login(n)), el("span", { class: "sub" }, `token ${n}`)),
        n === G.cur ? "✓" : "")),
      el("div", { class: "sep" }),
      el("button", { class: "menu-item", role: "menuitem", onclick: () => { closePop(); editIdentity(G.cur); } }, "Edit name and email…"),
      el("button", { class: "menu-item", role: "menuitem", onclick: () => { closePop(); addAccount(); } }, "Add account…")),
    { align: "left", width: 320 });
    const warn = placeholderEmail(id.email) ? el("div", { class: "note-box warn" }, `Agents of ${login(G.cur)} commit under a made-up name for now, so GitHub `
      + "links their commits to no account. Open the menu above and edit the name and email, or fill them in from the token.") : "";
    pick.replaceChildren(el("span", { class: "section-title" }, "Account"), btn, warn);
  };
  const show = (n) => {
    G.cur = n; UI.ghAccount = n;
    for (const [k, card] of Object.entries(cards)) card.hidden = k !== n;
    drawPick();
    if (G.started) draws[n]?.fetch();  // the one shown is asked about when the tab opens, others when you switch to them
  };
  const editIdentity = (n) => {
    const name = el("input", { class: "input mono", value: idents[n].name, spellcheck: false, "aria-label": "Name" });
    const email = el("input", { class: "input mono", value: idents[n].email, spellcheck: false, "aria-label": "Email" });
    const fill = el("button", { class: "btn small", type: "button" }, "Fill in from the token");
    fill.onclick = async () => {
      fill.disabled = true;
      try { const r = await api("POST", "/api/identity/lookup", { token: n }); name.value = r.name; email.value = r.email; }
      catch (e) { toast(e.message, true); }
      fill.disabled = false;
    };
    formModal("Edit name and email", [
      el("div", { class: "muted small pretty" }, `Who ${login(n)}'s agents commit as: the bot account's handle and its noreply address, so GitHub shows the commits as the bot's `
        + "and your own address never appears. It applies from each agent's next start."),
      el("label", { class: "field" }, el("span", { class: "label" }, "Name"), name),
      el("label", { class: "field" }, el("span", { class: "label" }, "Email"), email),
      el("div", { class: "row" }, fill, el("span", { class: "muted small pretty" }, "Asks GitHub which account the token belongs to."))],
    "Done", () => {
      const v = { name: name.value.trim(), email: email.value.trim() };
      if (!/^[A-Za-z0-9_.-]{1,64}$/.test(v.name)) throw new Error("The name is a GitHub handle, like my-agent-bot");
      if (!/^[0-9]+\+[A-Za-z0-9_.-]+@users\.noreply\.github\.com$/.test(v.email)) throw new Error("Use the account's noreply address, like 123456789+my-agent-bot@users.noreply.github.com");
      idents[n] = v; drawPick(); drawNote(); VIEWS.settings.markDirty?.();
      toast("Save settings to keep it.");
    }, name);
  };
  const addAccount = () => {
    const tok = el("input", { class: "input mono", type: "password", spellcheck: false, autocomplete: "off", placeholder: "github_pat_…", "aria-label": "Token" });
    const keep = sw(STATE.can_save_secrets !== false, (on, b) => setSw(b, on));
    formModal("Add account", [
      el("div", { class: "muted small pretty" }, "A fine-grained token of another bot account, never your own. cilly asks GitHub which account it belongs to, "
        + "and agents of that account commit as it. Then tick the repositories it may work in."),
      el("label", { class: "field" }, el("span", { class: "label" }, "Token"), tok),
      el("label", { class: "row nowrap" }, keep, el("span", { class: "pretty" }, "Save on this PC"))],
    "Add account", async () => {
      if (P_dirty()) throw new Error("Save settings or undo your changes first");
      const r = await api("POST", "/api/accounts", { token: tok.value, remember: keep.classList.contains("on") });
      toast(`Added ${r.name}. Tick the repositories it may work in.` + (r.note ? ` Its token is ${r.note}.` : ""));
      UI.ghAccount = r.token;
      await loadState(); VIEWS.settings.load();
    }, tok);
  };
  const P_dirty = () => VIEWS.settings.p?.dirtyTabs?.size > 0;
  for (const [name, t] of Object.entries(c.tokens)) {
    idents[name] = { ...(t.identity || shared) };
    const set = (chosen[name] = new Set(t.repos || []));
    const s = { listed: null, error: "", busy: false, checked: "" };
    const summary = el("span", { class: "muted small" }), list = el("div", { class: "repo-list" });
    const refresh = el("button", { class: "btn small", onclick: () => fetchOne() }, "Refresh from GitHub");
    const filter = el("input", { class: "input bare grow", placeholder: "Filter repositories", spellcheck: false, "aria-label": "Filter repositories" });
    const all = el("button", { class: "link small" });
    const manual = el("input", { class: "input bare mono grow", placeholder: "owner/name, to add one by hand", spellcheck: false, "aria-label": "Add a repository by hand" });
    const addManual = () => {
      const v = manual.value.trim();
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(v)) { toast("Use owner/name", true); return; }
      set.add(v); manual.value = ""; draw();
    };
    manual.addEventListener("keydown", (e) => { if (e.key === "Enter") addManual(); });
    filter.addEventListener("input", () => draw());
    const draw = () => {
      const rows = repoRows(s.listed || [], set), q = (filter.value || "").trim().toLowerCase(), sh = sharedRepos(chosen);
      const shown = rows.filter((r) => r.full_name.toLowerCase().includes(q));
      const usable = shown.filter((r) => r.usable), allOn = usable.length > 0 && usable.every((r) => set.has(r.full_name));
      summary.textContent = `${set.size} of ${s.listed ? rows.length : set.size} repositories allowed` + (s.checked ? `, checked ${ago(s.checked)}` : "");
      refresh.disabled = s.busy; refresh.textContent = s.busy ? "Checking…" : "Refresh from GitHub";
      all.textContent = allOn ? "Clear all" : "Select all"; all.disabled = !usable.length;
      all.onclick = () => { for (const r of usable) allOn ? set.delete(r.full_name) : set.add(r.full_name); draw(); drawNote(); };
      const also = (repo) => {  // ticked under another account too: which one agents get
        const others = (sh[repo] || []).filter((n) => n !== name);
        if (!set.has(repo) || !others.length) return "";
        return el("span", { class: "small accent-fg" }, sh[repo][0] === name ? `Also under ${others.map(login).join(", ")}. Agents get this account by default.`
          : `Also under ${others.map(login).join(", ")}, which agents get by default`);
      };
      list.replaceChildren(
        s.listed === null && !s.error ? el("div", { class: "list-row muted" }, s.busy ? "Asking GitHub which repositories the token reaches…" : "Not checked with GitHub yet.") : "",
        s.error ? el("div", { class: "list-row err" }, s.error) : "",
        ...shown.map((r) => repoRow(r, set.has(r.full_name), () => { set.has(r.full_name) ? set.delete(r.full_name) : set.add(r.full_name); draw(); drawNote(); }, also(r.full_name))),
        !shown.length && s.listed !== null ? el("div", { class: "list-row muted" }, rows.length ? "No repositories match." : "No repositories found for this token.") : "");
    };
    const fetchOne = async () => {
      s.busy = true; draw();
      try { s.listed = (await api("POST", "/api/tokens/repos", { name })).repos; s.error = ""; s.checked = new Date().toISOString(); }
      catch (e) { s.error = e.message; }
      s.busy = false; draw();
    };
    let fetched = false;
    draws[name] = { draw, fetch: () => { if (!fetched) { fetched = true; fetchOne(); } } };
    draw();
    cards[name] = el("div", { class: "card flush" },
      el("div", { class: "sec-summary row" }, el("div", { class: "grow stack0" }, summary, t.description ? el("span", { class: "dim small" }, t.description) : ""), refresh),
      el("div", { class: "list-row tight" }, filter, all), list,
      el("div", { class: "list-row tight" }, manual, el("button", { class: "btn small", onclick: addManual }, "Add")));
  }
  G.node.append(note, pick, ...Object.values(cards));
  if (!names.length) {
    G.node.append(el("div", { class: "dashed-empty stack tight" }, "No GitHub account yet.",
      el("div", { class: "row" }, el("button", { class: "btn small", type: "button", onclick: () => addAccount() }, "Add account"),
        el("button", { class: "btn small ghost", type: "button", onclick: () => go("setup", { setupStep: 1 }) }, "Open setup"))));
  }
  drawNote();
  if (G.cur) show(G.cur);
  G.fetch = () => { if (!G.started) { G.started = true; if (G.cur) draws[G.cur].fetch(); } };
  return G;
}
// Settings → GitHub access → Branch names.  Saved with Save settings.
const branchPrefix = (v) => { const t = v.trim(); return t && !t.endsWith("/") ? t + "/" : t; };
function branchNames(c) {
  const id = c.identity || {}, B = { mode: id.branch_names || "kind", node: el("div", { class: "stack" }) };
  B.prefix = el("input", { class: "input mono", value: id.branch_prefix || "cilly/", spellcheck: false, "aria-label": "Prefix" });
  const cards = el("div", { class: "mode-cards", role: "radiogroup", "aria-label": "Branch names" });
  const prefixRow = el("label", { class: "field" }, el("span", { class: "label" }, "Prefix"), B.prefix);
  const render = () => {
    const pre = branchPrefix(B.prefix.value) || "cilly/";
    const opts = [["kind", "Kind and a name", "fix/login-timeout", "The kind of work, like feat, fix or docs, then a few words."],
      ["prefix", "Prefix and a name", pre + "login-timeout", "Your prefix, then a few words."],
      ["id", "Agent ID", pre + "9a70b774-2", "The agent's ID, as cilly named branches before."]];
    cards.replaceChildren(...opts.map(([m, title, example, desc]) => el("div", { class: "card select-card" + (B.mode === m ? " on" : ""), role: "radio",
      "aria-checked": String(B.mode === m), tabindex: 0, onclick: () => { B.mode = m; render(); VIEWS.settings.markDirty?.(); } },
      el("div", { class: "row" }, el("span", { class: "title" }, title)),
      el("span", { class: "mono small" }, example), el("span", { class: "desc" }, desc))));
    prefixRow.hidden = B.mode === "kind";
  };
  B.prefix.addEventListener("input", () => { render(); VIEWS.settings.markDirty?.(); });
  render();
  B.node.append(setSubTip("Branch names", "With a name, the agent names its branch to fit the work before it's first pushed, or cilly makes one from the task. "
      + "Branch names are public on GitHub, and stay on a pull request's page after the branch is deleted."),
    cards, prefixRow);
  return B;
}
const placeholderEmail = (email) => /^0+\+/.test(email || "");
function repoRow(r, on, toggle, extra) {
  const p = r.permissions || {};
  const sub = r.missing ? "Not in GitHub's list for this token. It may have lost access, or it's past the first 300."
    : !r.usable ? "Read only: agents need write access to push" : p.admin ? "Admin access. Agents never need that, so a narrower token is safer." : "Read and write";
  const meta = [r.language, r.pushed_at ? "pushed " + ago(r.pushed_at) : "", r.private === undefined ? "" : r.private ? "private" : "public"].filter(Boolean).join(" · ");
  // read-only: can't be ticked, but one that's already ticked can still be unticked
  return el("button", { class: "repo-row", type: "button", role: "checkbox", "aria-checked": String(on), disabled: !r.usable && !on, onclick: toggle },
    el("span", { class: "checkbox" + (on ? " on" : "") }, on ? "✓" : ""),
    el("span", { class: "stack0" }, el("code", { class: "ellipsis" }, r.full_name), el("span", { class: "small " + (r.missing || !r.usable || p.admin ? "warn" : "muted") }, sub), extra || ""),
    el("span", { class: "dim small repo-meta" }, meta));
}

// Settings → Network: the allowlist, and Never reach.  The built-in rows say what the egress proxy
// always refuses (egress_proxy.address_allowed and LocalNet); only your own rows can be removed.
const NEVER_BUILT_IN = [
  ["Private and local networks", "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7"],
  ["This PC, loopback and link-local, including cloud metadata", "127.0.0.0/8, ::1, 169.254.0.0/16, fe80::/10"],
  ["Every other address that isn't public internet", "100.64.0.0/10, multicast, reserved and documentation ranges"],
  ["IPv6 that carries an IPv4 address", "6to4, Teredo, NAT64"],
  ["This PC's own addresses, and networks it reaches through the LAN or a VPN", "Read from its routing table, so Tailscale and WireGuard count too"],
];
// a light check before the server's full one (egress_proxy.parse_never_reach)
const looksLikeAddress = (v) => /^(\d{1,3}(\.\d{1,3}){3}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*)(\/\d{1,3})?$/.test(v);
// A model dropdown: Claude's models, then the models of your model servers that answer. A server that's
// offline is left out, but the model already picked stays listed, marked offline.
const MODEL_LISTS = { lists: null, asked: 0 };
function modelSelect(cur, onChange, redraw) {
  const enc = (m) => (m?.model ? (m.server ? `s:${m.server}:${m.model}` : `c:${m.model}`) : "");
  const servers = STATE.model_servers || [], lists = MODEL_LISTS.lists;
  const local = servers.flatMap((name) => (lists?.[name]?.models || []).map((m) => [name, m]));
  const s = el("select", { class: "input", "aria-label": "Model" }, el("option", { value: "" }, "Default"),
    el("optgroup", { label: "Cloud" }, ...MODELS.filter(([v]) => v).map(([v, label]) => el("option", { value: `c:${v}` }, label))),
    servers.length ? el("optgroup", { label: "Local" }, ...local.map(([n, m]) => el("option", { value: `s:${n}:${m}` }, `${m} · ${n}`)),
      !lists ? el("option", { disabled: true }, "Asking your model servers…") : !local.length ? el("option", { disabled: true }, "None of your model servers answer") : "") : "");
  const listed = !cur?.model || (cur.server ? local.some(([n, m]) => n === cur.server && m === cur.model) : MODELS.some(([v]) => v === cur.model));
  if (!listed) s.append(el("option", { value: enc(cur) }, cur.server ? `${cur.model} · ${cur.server} (offline)` : cur.model));
  s.value = enc(cur);
  s.addEventListener("change", () => {
    const v = s.value, i = v.indexOf(":", 2);
    onChange(!v ? null : v.startsWith("c:") ? { model: v.slice(2) } : { server: v.slice(2, i), model: v.slice(i + 1) });
  });
  if (servers.length && Date.now() - MODEL_LISTS.asked > 60000) {  // the panel keeps the lists a minute too
    MODEL_LISTS.asked = Date.now();
    api("GET", "/api/models").then((r) => { MODEL_LISTS.lists = r.servers; redraw(); }).catch(() => {});
  }
  return s;
}
// New agent → Model
function newModelSelect() {
  return modelSelect(NEW.model, (m) => { NEW.model = m; }, () => VIEWS.new.p?.modelBox?.replaceChildren(newModelSelect()));
}

// the Secrets tab, again, once the settings it lists from changed (a model server's key)
async function renderSecretsSoon() {
  const P = VIEWS.settings.p;
  if (!P?.secretsBox) return;
  try { const S = await api("GET", "/api/settings"); P.secrets = S.secrets; P.c.model_servers = S.config.model_servers; } catch { return; }
  renderSecrets(); VIEWS.settings.renderTabs();
}
// Settings → Local models: the model servers you run, each with its models and a test, and a form to add one
function modelsTab(c) {
  const T = { servers: JSON.parse(JSON.stringify(c.model_servers || {})), lists: {}, results: {}, ssh: {}, keys: {}, edit: {} };
  const list = el("div", { class: "stack" });
  const status = (name) => {
    const got = T.lists[name];
    if (!got) return el("span", { class: "muted small" }, "Asking it…");
    if (got.error) return el("span", { class: "row tight nowrap err small" }, el("span", { class: "dot s-failed" }), "Offline");
    return el("span", { class: "row tight nowrap s-ready small" }, el("span", { class: "dot s-ready" }), `Online · ${plural(got.models.length, "model")}`);
  };
  const card = (name, s) => {
    const got = T.lists[name] || {}, small = got.small || {}, res = T.results[name];
    const test = async () => {
      T.results[name] = { busy: true }; render();
      try { T.results[name] = await api("POST", "/api/models/test", { server: name, url: s.url }); }
      catch (e) { T.results[name] = { ok: false, error: e.message }; }
      await refresh(true);
    };
    const key = sw(!!s.api_key, async (on, b) => {
      if (await saveServer({ name, url: s.url, api_key: on, ssh: s.ssh })) toast(on ? "Saved. Now paste its key in Secrets." : "Saved. It's used without a key.");
      else setSw(b, !on);
    });
    const outcome = !res ? "" : res.busy ? el("span", { class: "muted small" }, "Sending one of its models a short message. A model that isn't loaded yet can take a minute.")
      : !res.ok ? el("span", { class: "err small" }, `Failed: ${res.error}`)
      : el("span", { class: "ok small" }, res.text ? `Works. ${res.model} answered “${res.text}”.` : `Works. ${res.model} answered, but with no text yet.`);
    return el("div", { class: "card stack" },
      el("div", { class: "row nowrap" },
        el("div", { class: "grow stack0" }, el("div", { class: "row tight" }, el("strong", {}, name), status(name)),
          el("span", { class: "mono small muted" }, s.url),
          s.ssh ? el("span", { class: "small muted" }, `Over SSH to ${s.ssh.user}@${s.ssh.host}${s.ssh.port === 22 ? "" : ":" + s.ssh.port}${s.ssh.route === "direct" ? "" : " · " + routeLabel(s.ssh.route)}`) : ""),
        el("button", { class: "btn small", disabled: !!res?.busy, onclick: test }, "Test"),
        T.edit[name] ? "" : el("button", { class: "btn small ghost", onclick: () => {
          T.edit[name] = editForm(name, s); render(); T.edit[name].F.url.focus();
        } }, "Edit"),
        el("button", { class: "btn small ghost", onclick: async () => {
          if (await confirmModal(`Remove ${name}?`, "Agents on one of its models can't reach it any more. Pick another model for them.", "Remove", true)
              && await saveServer({ name, remove: true })) toast(`Removed ${name}`);
        } }, "Remove")),
      T.edit[name] ? T.edit[name].node : "",
      got.error ? el("span", { class: "err small pretty" }, `Can't list its models: ${got.error}`) : "",
      (got.models || []).length ? el("div", { class: "row tight" }, ...got.models.map((m) =>
        el("span", { class: "tool-chip" + (small[m] ? " warn" : "") }, small[m] ? `${m} · ${Math.round(small[m] / 1024)}k context` : m))) : "",
      ...Object.entries(small).map(([m, n]) => el("div", { class: "note-box warn" },
        `Ollama gives ${m} only ${n.toLocaleString("en")} tokens of context. Agents need 32,768, so set OLLAMA_CONTEXT_LENGTH=32768 on that PC and restart Ollama.`)),
      res?.warning && !Object.keys(small).length ? el("div", { class: "note-box warn" }, res.warning) : "",
      res?.effort_warning ? el("div", { class: "note-box warn" }, res.effort_warning) : "",
      outcome,
      s.ssh ? sshSteps(name, s) : "",
      el("div", { class: "sep" }),
      el("label", { class: "row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "Needs an API key"),
        el("span", { class: "muted small pretty" }, "Turn on if the server asks for one. Agents never see the key.")), key));
  };
  // a server over SSH: the key to put on it, its host key to confirm, and how the tunnel is doing
  const sshSteps = (name, s) => {
    const v = T.lists[name]?.ssh || T.ssh[name] || {}, fetched = T.keys[name];
    const ssh = async (body, done) => {
      try {
        const r = await api("POST", "/api/models/ssh", { name, ...body });
        if (r.ok === false) { toast(`Couldn't get its host key: ${r.error}`, true); return null; }
        if (r.servers) { T.servers = r.servers; T.ssh = r.ssh || {}; }
        if (done) toast(done);
        return r;
      } catch (e) { toast(e.message, true); return null; }
    };
    const fetchKey = async (b) => {
      b.disabled = true; b.textContent = "Asking the server…";
      const r = await ssh({ action: "fetch" });
      if (r) T.keys[name] = r;
      render();
    };
    const confirmKey = async () => {
      if (await ssh({ action: "confirm", fingerprint: fetched.fingerprint }, "Host key confirmed. cilly refuses any other from now on.")) {
        delete T.keys[name]; render(); refresh(true);
      }
    };
    const newKey = async () => {
      if (!await confirmModal(`Make a new key for ${name}?`, "The old key stops working. Put the new one on the server in its place.", "Make new key", true)) return;
      if (await ssh({ action: "new_key" }, "New key made. Put it on the server.")) { render(); renderSecretsSoon(); }
    };
    const tunnel = v.tunnel;
    const hostKey = fetched ? el("div", { class: "note-box stack" },
      el("span", {}, fetched.pinned ? "It's still the host key you confirmed." : `Its host key is ${fetched.type}:`),
      el("code", { class: "mono break" }, fetched.fingerprint),
      fetched.pinned ? "" : el("span", { class: "small" }, "Check it on the server with ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub, or ask your provider. Confirm only if it matches."),
      el("div", { class: "row" }, fetched.pinned ? "" : el("button", { class: "btn small primary", onclick: confirmKey }, "It matches"),
        el("button", { class: "btn small ghost", onclick: () => { delete T.keys[name]; render(); } }, fetched.pinned ? "Close" : "Cancel")))
      : el("div", { class: "row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "Host key"),
          el("span", { class: "small muted mono break" }, v.fingerprint || "Not confirmed yet. The tunnel waits for it.")),
        el("button", { class: "btn small" + (v.fingerprint ? " ghost" : ""), onclick: (e) => fetchKey(e.target) }, v.fingerprint ? "Check again" : "Get host key"));
    return el("div", { class: "stack models-ssh" },
      el("div", { class: "sep" }),
      el("div", { class: "row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "Key for the server"),
        el("span", { class: "small muted pretty" }, `Add this line to ~${s.ssh.user}/.ssh/authorized_keys on the server. It lets cilly forward to the model server and nothing else.`)),
        el("button", { class: "btn small ghost", onclick: newKey }, "New key")),
      v.key === "missing" ? el("div", { class: "note-box warn" }, "The private key is gone. It was kept for this session only. Make a new key and put it on the server.") : "",
      v.authorized ? copyBlock(v.authorized) : "",
      hostKey,
      tunnel?.error ? el("span", { class: "err small pretty" }, `Tunnel: ${tunnel.error}`)
        : tunnel?.ready ? el("span", { class: "ok small" }, `Tunnel up on this PC's port ${tunnel.port}`) : "");
  };
  // a redraw (a model list that came in, say) keeps an edit form's fields, and the one you were typing in
  const render = () => {
    const f = document.activeElement, typing = f?.closest?.(".models-edit") ? [f, f.selectionStart, f.selectionEnd] : null;
    for (const n of Object.keys(T.edit)) if (!T.servers[n]) delete T.edit[n];  // removed meanwhile
    list.replaceChildren(...(Object.keys(T.servers).length
      ? Object.entries(T.servers).map(([n, s]) => card(n, s)) : [el("div", { class: "dashed-empty" }, "No model servers yet. Add one below, like Ollama or llama.cpp.")]));
    if (typing && document.contains(typing[0])) {
      typing[0].focus();
      try { typing[0].setSelectionRange(typing[1], typing[2]); } catch { /* a select */ }
    }
  };
  // what each server has, and whether it's up: asked when the tab opens and after each change
  const refresh = async (fresh) => {
    if (!Object.keys(T.servers).length) { render(); return; }
    try { T.lists = (await api("GET", "/api/models" + (fresh ? "?refresh=1" : ""))).servers; } catch { /* the cards say "Asking it…" */ }
    render();
  };
  // each change is saved at once, so agents and their model menus see it right away
  const saveServer = async (body) => {
    try {
      const r = await api("POST", "/api/models/servers", body);
      T.servers = r.servers; T.ssh = r.ssh || {};
      if (r.note) toast(`Its SSH key is ${r.note}.`);
    } catch (e) { toast(e.message, true); return false; }
    render(); refresh(true); loadState(); renderSecretsSoon();
    return true;
  };
  const field = (label, input, cls, tip) => el("label", { class: "field" + (cls ? " " + cls : "") }, typeof label === "string" ? el("span", { class: "label" }, label, tip ? infoTip(tip) : "") : label, input);
  // a server's fields, for adding one and for Edit: its address, and over SSH, where cilly logs in and how it gets there
  const serverForm = (s = {}) => {
    const F = { route: s.ssh?.route || "direct" };
    F.url = el("input", { class: "input mono", spellcheck: false, "aria-label": "Address", value: s.url || "" });
    F.reach = el("select", { class: "input", "aria-label": "How to reach it" },
      el("option", { value: "direct" }, "Directly"), el("option", { value: "ssh" }, "Over SSH"));
    F.reach.value = s.ssh ? "ssh" : "direct";
    F.sshHost = el("input", { class: "input mono", placeholder: "gpu.example.net", spellcheck: false, "aria-label": "SSH server", value: s.ssh?.host || "" });
    F.sshPort = el("input", { class: "input mono", value: String(s.ssh?.port || 22), inputmode: "numeric", "aria-label": "SSH port" });
    F.sshUser = el("input", { class: "input mono", placeholder: "root", spellcheck: false, "aria-label": "SSH user", value: s.ssh?.user || "" });
    const urlLabel = el("span", { class: "label" }, "Address");
    F.sshFields = el("div", { class: "set-grid" }, field("SSH server", F.sshHost, "wide"), field("Port", F.sshPort), field("User", F.sshUser),
      field("Route", el("div", {}, routeSelect(F.route, (v) => { F.route = v || "direct"; }, null, "SSH route")), "",
        "How the SSH connection leaves this PC. Through Tor, a VPN or a proxy, the server sees that address instead of yours."));
    F.urlField = el("div", { class: "set-grid" }, field(urlLabel, F.url, "wide", ""));
    const showReach = () => {
      const over = F.reach.value === "ssh";
      F.sshFields.hidden = !over;
      urlLabel.textContent = over ? "Address on that server" : "Address";
      F.url.placeholder = over ? "http://127.0.0.1:11434" : "http://192.168.1.50:11434";
    };
    F.reach.addEventListener("change", showReach);
    showReach();
    F.read = () => Object.assign({ url: F.url.value.trim() }, F.reach.value === "ssh" ? {
      ssh: { host: F.sshHost.value.trim(), port: Number(F.sshPort.value.trim() || 22), user: F.sshUser.value.trim(), route: F.route } } : {});
    return F;
  };
  const hostPort = (u) => { try { const x = new URL(u); return `${x.hostname}:${x.port || (x.protocol === "https:" ? 443 : 80)}`; } catch { return u; } };
  // Edit: the same fields, filled in.  Its key stays, and so does the host key you confirmed, unless the SSH server or its port changes
  const editForm = (n, s) => {
    const F = serverForm(s);
    const save = async () => {
      const body = F.read(), was = s.ssh, now = body.ssh;
      if (was && !now && !await confirmModal(`Reach ${n} directly?`, "Its SSH key is deleted and its tunnel stops. To go back to SSH, you'd put a new key on the server.", "Reach it directly", true)) return;
      if (!await saveServer({ name: n, api_key: !!s.api_key, ...body })) return;
      delete T.edit[n];
      render();
      const notes = [];
      if (now && !was) notes.push("Put its key on the server and confirm its host key below.");
      if (now && was && (was.host !== now.host || was.port !== now.port)) notes.push("It's another SSH server now, so get its host key and confirm it again.");
      else if (now && was && hostPort(s.url) !== hostPort(body.url)) notes.push("Its line for authorized_keys changed. Replace the old one on the server.");
      toast(notes.length ? `Saved ${n}. ${notes.join(" ")}` : `Saved ${n}.`);
    };
    const node = el("div", { class: "stack models-edit" }, el("div", { class: "sep" }),
      el("div", { class: "set-grid" }, field("Reach it", F.reach)), F.sshFields, F.urlField,
      el("div", { class: "row" }, el("button", { class: "btn small primary", onclick: save }, "Save"),
        el("button", { class: "btn small ghost", onclick: () => { delete T.edit[n]; render(); } }, "Cancel")));
    return { F, node };
  };
  const name = el("input", { class: "input", placeholder: "gpu", spellcheck: false, "aria-label": "Name" });
  const A = serverForm();
  const form = el("div", { class: "card stack", hidden: true });
  const add = async () => {
    const n = name.value.trim(), body = A.read();
    if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(n)) { toast("Name it with lowercase letters, digits and -, like gpu or studio.", true); return; }
    if (T.servers[n]) { toast(`There is already a server called ${n}.`, true); return; }
    if (await saveServer({ name: n, api_key: false, ...body })) {
      name.value = A.url.value = A.sshHost.value = A.sshUser.value = ""; form.hidden = true; opener.hidden = false;
      toast(body.ssh ? `Added ${n}. Put its key on the server and confirm its host key below.`
        : `Added ${n}. Pick one of its models in an agent's model menu, or under New agent → Options.`);
    }
  };
  form.append(el("div", { class: "set-grid" }, field("Name", name), field("Reach it", A.reach)), A.sshFields, A.urlField,
    el("div", { class: "row" }, el("button", { class: "btn small primary", onclick: add }, "Add server"),
      el("button", { class: "btn small ghost", onclick: () => { form.hidden = true; opener.hidden = false; } }, "Cancel")));
  const opener = el("button", { class: "chip add models-add", type: "button", onclick: () => { form.hidden = false; opener.hidden = true; name.focus(); } }, "+ Add a server");
  render(); refresh();
  T.node = el("div", { class: "stack" }, list, opener, form,
    el("div", { class: "dim small pretty" }, "Ollama gives models only 4,096 tokens of context unless you set OLLAMA_CONTEXT_LENGTH. Agents need 32,768 or more. "
      + "A model shows how much it has once it's loaded, for example after Test."));
  return T;
}

function networkTab(c) {
  const allow = el("textarea", { class: "editor", rows: 9, spellcheck: false, value: c.egress.allow.join("\n"), "aria-label": "Global allowlist" });
  const count = el("span", { class: "muted pretty" });
  const showCount = () => { count.textContent = `${lines(allow.value).length} sites on the global allowlist. Running agents pick up changes once you save.`; };
  allow.addEventListener("input", showCount); showCount();
  const never = [...(c.egress.never_reach || [])], list = el("div", { class: "card flush" });
  const input = el("input", { class: "input mono grow", placeholder: "an address or network, such as 203.0.113.7 or 2001:db8::/32", spellcheck: false, "aria-label": "Add to Never reach" });
  const add = () => {
    const v = input.value.trim();
    if (!v) return;
    if (!looksLikeAddress(v)) { toast("Never reach takes addresses and networks, such as 203.0.113.7 or 2001:db8::/32", true); return; }
    if (never.includes(v)) { toast(`${v} is already on the list`, true); return; }
    never.push(v); input.value = ""; render(); toast(`Added ${v}. Save settings to apply it.`);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  const render = () => list.replaceChildren(
    ...NEVER_BUILT_IN.map(([what, detail]) => el("div", { class: "list-row tight" }, el("div", { class: "grow stack0" }, el("span", {}, what), el("span", { class: "dim small" }, detail)),
      el("span", { class: "dim small" }, "Built in"))),
    ...never.map((v) => el("div", { class: "list-row tight" }, el("code", { class: "grow ellipsis" }, v),
      el("button", { class: "btn small ghost", onclick: () => { never.splice(never.indexOf(v), 1); render(); } }, "Remove"))),
    el("div", { class: "list-row tight" }, input, el("button", { class: "btn small", onclick: add }, "Add")));
  render();
  const R = routesSection(c);
  return { allow, never, routes: R, node: el("div", { class: "stack" },
    el("div", { class: "title-row" }, count, infoTip(`One per line. *.example.com covers subdomains. A site gets ${portsText(c.egress.ports)}, `
      + "unless it names its own, like example.com:8080, which allows just that port.")), allow,
    setSubTip("Never reach", "Always blocked, for every agent, including research agents on the open web. It beats the allowlist and any agent's own sites, "
        + "and it's checked after the name lookup, so whatever name leads there is blocked too."),
    list,
    el("span", { class: "dim small pretty" }, "Add your home's public IPv4 address, your IPv6 prefix and servers you own. Your router's status page shows the first two."),
    R.node) };
}

// Settings → Network → Routes: send agents' traffic on through a proxy or Tor.  Saved with Save settings.
function routesSection(c) {
  const R = { routes: JSON.parse(JSON.stringify(c.egress.routes || {})), route: c.egress.route || "", own: !!c.egress.route_own,
    doh: c.egress.doh || "", node: el("div", { class: "stack" }), editing: null, tests: {}, testHost: "",
    keep: c.egress.keep_tokens !== false, pushOwn: c.egress.push_own_branches !== false };
  const field = (label, input) => el("label", { class: "field" }, el("span", { class: "label" }, label), input);
  const tor = () => {
    const t = STATE.routes?.tor || {};
    return !t.installed ? `Not installed. ${hostCmd("tor") ? "Install it with: " + hostCmd("tor") : "Install tor from your distribution's packages."}`
      : t.error ? t.error : t.running ? (t.progress >= 100 ? "Running and connected" : `Connecting (${t.progress}%)`)
        : "Installed. cilly starts it, inside bubblewrap, when an agent's route needs it.";
  };
  const wgStatus = (n) => {
    const w = STATE.routes?.wireguard || {}, s = w.routes?.[n];
    if (!w.installed) return `wireproxy isn't installed. ${wireproxyCmd() ? "Install it with: " + wireproxyCmd() : "Get it from github.com/windtf/wireproxy."}`;
    return s?.error || (s?.running ? "Running" : "cilly starts it, inside bubblewrap, when an agent's route needs it.");
  };
  // Test: connect through a saved route to a site, as an agent would
  const test = async (n) => {
    R.tests[n] = { busy: true };
    render();
    try { R.tests[n] = await api("POST", "/api/routes/test", { name: n, host: R.testHost.trim() || undefined }); }
    catch (e) { R.tests[n] = { ok: false, error: e.message }; }
    if (R.node.isConnected) render();
  };
  const testBtn = (n) => el("button", { class: "btn small ghost", type: "button", disabled: !!R.tests[n]?.busy, onclick: () => test(n) }, "Test");
  const testLine = (n) => {  // under its route's row, the whole width
    const t = R.tests[n];
    if (!t) return "";
    return el("div", { class: "route-test" }, t.busy ? el("span", { class: "muted small" }, "Connecting through it…")
      : t.ok ? el("span", { class: "ok small" }, `Works. Reached ${t.host} in ${t.ms} ms.`)
        : el("span", { class: "err small pretty" }, `Failed: ${t.error}`));
  };
  const hostsField = (r) => el("textarea", { class: "input mono", rows: 3, spellcheck: false, value: (r?.private || []).join("\n"),
    placeholder: "git.office.lan\n10.8.0.20", "aria-label": "Private hosts it may reach" });
  const hostsOf = (t) => t.value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  const withHosts = (route, hosts) => { delete route.private; if (hosts.length) route.private = hosts; return route; };
  const hostsHelp = () => el("span", { class: "dim small pretty" }, "One per line: exact names or private addresses, none by default. "
    + "Agents also need each one among their allowed sites. Agents on the open web never reach them.");
  const goodName = (n) => {
    if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(n) || n === "tor" || n === "direct") { toast("Name it with lowercase letters, digits and -, like vpn or office.", true); return false; }
    return true;
  };
  const standard = () => {
    const s = el("select", { class: "input", "aria-label": "Standard route" },
      [["", "Straight out"], ["tor", "Tor"], ...Object.keys(R.routes).map((n) => [n, n])].map(([v, t]) => el("option", { value: v }, t)));
    s.value = Object.prototype.hasOwnProperty.call(R.routes, R.route) || R.route === "tor" ? R.route : "";
    s.addEventListener("change", () => { R.route = s.value; render(); });
    return s;
  };
  const editor = (name) => {
    const r = name ? R.routes[name] : { kind: "socks5", host: "", port: 1080 };
    const nm = el("input", { class: "input mono", value: name || "", placeholder: "vpn", spellcheck: false, "aria-label": "Name", disabled: !!name });
    const kind = el("select", { class: "input", "aria-label": "Type" }, [["socks5", "SOCKS5"], ["http", "HTTP proxy"]].map(([v, t]) => el("option", { value: v }, t)));
    kind.value = r.kind;
    const host = el("input", { class: "input mono", value: r.host || "", placeholder: "10.64.0.1", spellcheck: false, "aria-label": "Host" });
    const port = el("input", { class: "input", type: "number", min: 1, max: 65535, value: r.port || "", "aria-label": "Port" });
    const user = el("input", { class: "input mono", value: r.user || "", placeholder: "Optional", spellcheck: false, autocomplete: "off", "aria-label": "User" });
    const hosts = hostsField(r);
    const done = () => {
      const n = (name || nm.value.trim()).toLowerCase();
      if (!goodName(n)) return;
      if (!name && R.routes[n]) { toast(`There is already a route called ${n}.`, true); return; }
      const p = +port.value;
      if (!host.value.trim() || !(p >= 1 && p <= 65535)) { toast("Give the proxy's host and a port from 1 to 65535.", true); return; }
      R.routes[n] = withHosts(Object.assign({ kind: kind.value, host: host.value.trim(), port: p }, user.value.trim() ? { user: user.value.trim() } : {},
        r.secret && user.value.trim() ? { secret: r.secret } : {}), hostsOf(hosts));
      R.editing = null; render();
      toast(user.value.trim() ? `Save settings, then give ${n}'s password under Secrets.` : "Save settings to use it.");
    };
    return el("div", { class: "card stack" },
      el("div", { class: "set-grid" }, field("Name", nm), field("Type", kind), field("Host", host), field("Port", port), field("User", user)),
      el("span", { class: "dim small pretty" }, "A password goes under Secrets once you save, never into the config file."),
      field("Private hosts it may reach", hosts), hostsHelp(),
      el("div", { class: "row" }, el("button", { class: "btn small primary", type: "button", onclick: done }, name ? "Done" : "Add route"),
        el("button", { class: "btn small ghost", type: "button", onclick: () => { R.editing = null; render(); } }, "Cancel")));
  };
  // A WireGuard VPN: paste its .conf. cilly adds the route and puts its keys under Secrets at once,
  // so they never wait in this page. Private hosts are saved with Save settings.
  const wgEditor = (name) => {
    const r = name ? R.routes[name] : null;
    const nm = el("input", { class: "input mono", value: name || "", placeholder: "office", spellcheck: false, "aria-label": "Name", disabled: !!name });
    const conf = el("textarea", { class: "input mono", rows: 8, spellcheck: false, autocomplete: "off", "aria-label": "WireGuard config",
      placeholder: name ? "Paste a new .conf to replace its config and keys" : "[Interface]\nPrivateKey = …\nAddress = …\n\n[Peer]\nPublicKey = …\nEndpoint = …" });
    const hosts = hostsField(r);
    let remember = true;
    const done = async (b) => {
      const n = (name || nm.value.trim()).toLowerCase();
      if (!goodName(n)) return;
      if (!name && R.routes[n]) { toast(`There is already a route called ${n}.`, true); return; }
      if (!name && !conf.value.trim()) { toast("Paste the VPN's .conf file.", true); return; }
      if (conf.value.trim()) {
        b.disabled = true;
        let res;
        try {
          res = await api("POST", "/api/routes/wireguard", { name: n, conf: conf.value, remember: remember && !!STATE?.can_save_secrets });
        } catch (e) { b.disabled = false; toast(e.message, true); return; }
        conf.value = "";  // the keys leave this page
        R.routes[n] = res.route;
        const left = res.left_out.length ? ` cilly left out ${res.left_out.join(", ")}, and never runs commands from the file.` : "";
        toast(res.note ? `Added, but the keys are ${res.note}.` : `Added. Its keys are under Secrets.${left}`, !!res.note);
        loadState();
      }
      withHosts(R.routes[n], hostsOf(hosts));
      R.editing = null; render();
      if (JSON.stringify(R.routes[n].private || []) !== JSON.stringify(c.egress.routes?.[n]?.private || [])) toast("Save settings to use its private hosts.");
    };
    return el("div", { class: "card stack" },
      el("div", { class: "set-grid one" }, field("Name", nm)), field(name ? "New config (optional)" : "Its .conf file", conf),
      el("span", { class: "dim small pretty" }, "The keys go under Secrets, never into the config file. cilly never runs commands from the file, like PostUp."),
      saveSwitch((on) => { remember = on; }),
      field("Private hosts it may reach", hosts), hostsHelp(),
      el("div", { class: "row" }, el("button", { class: "btn small primary", type: "button", onclick: (e) => done(e.currentTarget) }, name ? "Done" : "Add VPN"),
        el("button", { class: "btn small ghost", type: "button", onclick: () => { R.editing = null; render(); } }, "Cancel")));
  };
  const dohSel = () => {
    const custom = typeof R.doh === "object" && R.doh;
    const s = el("select", { class: "input", "aria-label": "Look names up with" },
      [["", "This PC's DNS"], ...Object.entries(P_DOH()).map(([k, l]) => [k, l]), ["custom", "Your own resolver"]]
        .map(([v, t]) => el("option", { value: v }, t)));
    s.value = custom ? "custom" : R.doh;
    s.addEventListener("change", () => { R.doh = s.value === "custom" ? { url: "", ip: "" } : s.value; render(); });
    if (!custom) return [s];
    const url = el("input", { class: "input mono", value: custom.url, placeholder: "https://dns.example/dns-query", spellcheck: false, "aria-label": "Resolver URL" });
    const ip = el("input", { class: "input mono", value: custom.ip, placeholder: "its IP address", spellcheck: false, "aria-label": "Resolver address" });
    url.addEventListener("input", () => { custom.url = url.value.trim(); });
    ip.addEventListener("input", () => { custom.ip = ip.value.trim(); });
    return [s, el("div", { class: "set-grid" }, field("URL", url), field("Its address", ip))];
  };
  const testSite = () => {  // kept across redraws, so typing isn't lost when a test ends
    if (!R.testInput) {
      R.testInput = el("input", { class: "input mono", placeholder: "api.github.com", spellcheck: false, "aria-label": "Site to test with" });
      R.testInput.addEventListener("input", () => { R.testHost = R.testInput.value; });
    }
    return R.testInput;
  };
  const render = () => {
    const names = Object.keys(R.routes);
    R.node.replaceChildren(
      setSubTip("Routes", "Send agents' traffic on through a proxy, a VPN or Tor, so sites see that address instead of yours. "
          + "Every check above still runs first, on this PC. If a route is down, its agents reach nothing rather than going straight out."),
      el("div", { class: "set-grid one" }, field("Standard route", standard())),
      el("span", { class: "dim small pretty" }, "For every agent, unless its project or the agent picks another."),
      el("div", { class: "card flush" },
        el("div", { class: "list-row tight" + (R.tests.tor ? " joined" : "") }, el("div", { class: "grow stack0" }, el("span", {}, "Tor"), el("span", { class: "dim small pretty" }, tor())),
          el("span", { class: "dim small" }, "Built in"), STATE.routes?.tor?.installed ? testBtn("tor") : ""), testLine("tor"),
        ...names.flatMap((n) => { const r = R.routes[n], vpn = r.kind === "wireguard"; return [el("div", { class: "list-row tight" + (R.tests[n] ? " joined" : "") },
          el("div", { class: "grow stack0" }, el("code", {}, n),
            el("span", { class: "dim small" }, [ROUTE_KIND[r.kind], vpn ? r.endpoint : `${r.host}:${r.port}`, r.user ? "login" : "",
              r.private?.length ? `${r.private.length} private host${r.private.length > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ")),
            vpn ? el("span", { class: "dim small pretty" }, wgStatus(n)) : ""),
          testBtn(n),
          el("button", { class: "btn small ghost", type: "button", onclick: () => { R.editing = n; render(); } }, "Edit"),
          el("button", { class: "btn small ghost", type: "button", onclick: () => { delete R.routes[n]; if (R.route === n) R.route = ""; render(); } }, "Remove")), testLine(n)]; }),
        R.editing === null ? el("button", { class: "note-row add-row", type: "button", onclick: () => { R.editing = ""; render(); } }, "+ Add a SOCKS5 or HTTP proxy") : "",
        R.editing === null ? el("button", { class: "note-row add-row", type: "button", onclick: () => { R.editing = "wg:"; render(); } }, "+ Add a WireGuard VPN") : ""),
      el("div", { class: "set-grid one" }, field("Site to test with", testSite())),
      el("span", { class: "dim small pretty" }, "Test connects through a saved route to this site, as an agent would. It can be api.github.com or a site on the allowlist."),
      R.editing === "wg:" ? wgEditor(null) : R.editing !== null && R.routes[R.editing]?.kind === "wireguard" ? wgEditor(R.editing)
        : R.editing !== null ? editor(R.editing || null) : "",
      el("div", { class: "set-grid one" }, field("Look names up with", el("div", { class: "stack tight" }, ...dohSel()), "", "With this PC's DNS, your provider sees the names agents look up. DNS-over-HTTPS sends them through the route instead. For SOCKS5 and HTTP routes, and a VPN without its own DNS. Tor looks names up itself.")),
      el("label", { class: "row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "Also for cilly's own downloads"),
        el("span", { class: "muted small pretty" }, "The GitHub API, the Debian image and image builds take the standard route too.")),
      sw(R.own, (on, b) => { R.own = on; setSw(b, on); })),
      el("div", { class: "set-sub" }, el("span", { class: "title" }, "Tokens"),
        el("span", { class: "muted pretty" }, "Where the Claude login and the GitHub token live while agents work.")),
      el("label", { class: "row nowrap" }, el("span", { class: "grow" }, "Keep tokens on this PC", infoTip("VMs get stand-ins. cilly's proxy puts the real tokens in on the way to Anthropic and GitHub, "
          + "so an agent can use them while it works but can't take them. Each start makes its own certificate authority, "
          + "which the VM trusts for those sites only. Applies from an agent's next start.")),
      sw(R.keep, (on, b) => { R.keep = on; setSw(b, on); render(); })),
      R.keep ? el("label", { class: "row nowrap" }, el("span", { class: "grow" }, "Agents push only to their own branches", infoTip("git pushes go only to branches the agent created, and to new ones named "
          + "the way GitHub access says. A new branch never replaces one that's there. "
          + "Agents still read every branch. To be sure main can't change, also protect it on GitHub, since the API could change it if the token allows.")),
      sw(R.pushOwn, (on, b) => { R.pushOwn = on; setSw(b, on); })) : "");
  };
  const P_DOH = () => VIEWS.settings.p?.dohResolvers || {};
  render();
  return R;
}

// ---- `claude mcp add …` → an MCP library entry, with keys moved out into memory ----
function shellSplit(cmd) {
  const out = [];
  let cur = "", q = null, any = false;
  for (let i = 0; i < cmd.length; i++) {
    const ch = cmd[i];
    if (q) { if (ch === q) q = null; else if (ch === "\\" && q === '"' && i + 1 < cmd.length) cur += cmd[++i]; else cur += ch; }
    else if (ch === "'" || ch === '"') { q = ch; any = true; }
    else if (ch === "\\" && i + 1 < cmd.length) { cur += cmd[++i]; any = true; }
    else if (/\s/.test(ch)) { if (cur || any) out.push(cur); cur = ""; any = false; }
    else cur += ch;
  }
  if (q) throw new Error("Unclosed quote in the command.");
  if (cur || any) out.push(cur);
  return out;
}
function parseMcpAdd(cmd) {
  const t = shellSplit(cmd.trim().replace(/\\\n/g, " "));
  if (t[0] !== "claude" || t[1] !== "mcp" || t[2] !== "add") throw new Error("Expected a command that starts with claude mcp add");
  let transport = null;
  const headers = [], envs = [], pos = [];
  for (let i = 3; i < t.length; i++) {
    const a = t[i];
    if (a === "--") { pos.push(...t.slice(i + 1)); break; }
    if (a === "-t" || a === "--transport") transport = t[++i];
    else if (a.startsWith("--transport=")) transport = a.slice(12);
    else if (a === "-H" || a === "--header") headers.push(t[++i]);
    else if (a === "-e" || a === "--env") envs.push(t[++i]);
    else if (a === "-s" || a === "--scope") i++;
    else if (a.startsWith("-") && !pos.length) throw new Error(`Unknown option ${a}`);
    else pos.push(a);
  }
  const [name, target, ...args] = pos;
  if (!name || !/^[A-Za-z0-9_-]{1,48}$/.test(name)) throw new Error("The server name must be letters, digits, - or _");
  if (!target) throw new Error("Missing the server URL or command");
  const base = name.toUpperCase().replace(/[^A-Z0-9]/g, "_");
  const secrets = {};
  const keep = (v, varName) => {  // keep the value out of the config: memory only, as ${VAR}
    if (!v || /\$\{[A-Z][A-Z0-9_]*\}/.test(v)) return v;
    secrets[varName] = v;
    return "${" + varName + "}";
  };
  const isUrl = /^https?:\/\//.test(target);
  transport = transport || (isUrl ? "http" : "stdio");
  if (!["http", "sse", "stdio"].includes(transport)) throw new Error(`Unknown transport ${transport}`);
  const server = { type: transport };
  if (transport === "stdio") {
    server.command = target; server.args = args;
  } else {
    if (!isUrl) throw new Error("http and sse servers need an https:// URL");
    server.url = target;
  }
  if (headers.length) {
    server.headers = {};
    for (const h of headers) {
      const k = h.slice(0, h.indexOf(":")).trim(), v = h.slice(h.indexOf(":") + 1).trim();
      if (!k || h.indexOf(":") < 0) throw new Error(`The header ${h} doesn't look like "Name: value"`);
      const auth = v.match(/^(Bearer|Token|Basic)\s+(.+)$/i);
      server.headers[k] = auth ? `${auth[1]} ${keep(auth[2], base + "_API_KEY")}` : keep(v, base + "_" + k.toUpperCase().replace(/[^A-Z0-9]/g, "_"));
    }
  }
  if (envs.length) {
    server.env = {};
    for (const e of envs) {
      const i = e.indexOf("=");
      if (i < 1) throw new Error(`The variable ${e} doesn't look like KEY=value`);
      const k = e.slice(0, i);
      server.env[k] = keep(e.slice(i + 1), k.toUpperCase().replace(/[^A-Z0-9_]/g, "_"));
    }
  }
  return { name, server, secrets };
}

// Settings → This PC: the host check from setup, and starting cilly when you log in (a systemd user service).
function thisPcSection() {
  const box = el("div", { class: "stack" });
  const render = () => {
    const sv = STATE.service || {};
    const toggle = sw(sv.enabled, async (on, b) => {
      b.disabled = true;
      try { STATE.service = await api("POST", "/api/service", { enabled: on }); toast(on ? "cilly starts when you log in." : "cilly no longer starts when you log in."); }
      catch (e) { toast(e.message, true); }
      render();
    }, "large");
    toggle.disabled = !sv.available;
    toggle.setAttribute("aria-label", "Start when you log in");
    box.replaceChildren(...hostChecks(render),
      el("div", { class: "row" }, el("button", { class: "btn small", onclick: async () => { await checkHostAgain(); render(); } }, "Check again")),
      el("label", { class: "card row nowrap" }, el("div", { class: "grow stack none" }, el("span", { class: "title" }, "Start when you log in"),
        el("span", { class: "muted small pretty" }, !sv.available ? "Needs systemd running your session."
          : `A systemd user service, as you. Open it with ${cli()} open, as its link changes at every start.`)), toggle));
  };
  render();
  box.render = render;  // Settings' update redraws it as the state changes
  return box;
}

// Settings → VM images: the images you built, what each has and who uses it, the default for coding and
// for research agents, rebuilding one, deleting one, and building a new one (here, or with the command it shows).
function builtText(i) {
  const days = imageAge(i);
  return days == null ? "" : days < 1 ? "Built today" : `Built ${days} day${days === 1 ? "" : "s"} ago`;
}
function imageUsers(i) {
  const names = (i.used_by || []).map(agentName);
  return !names.length ? "Not used" : names.length > 3 ? `Used by ${names.length} agents` : `Used by ${names.join(", ")}`;
}
function imagesSection() {
  const box = el("div", { class: "stack" }), defaults = el("div", { class: "image-cards" }), table = el("div", { class: "card flush image-table" });
  const progress = el("div", { class: "stack tight" }), cmd = el("div"), aboutTip = el("span");
  const src = el("input", { class: "input mono grow", spellcheck: false, "aria-label": "Debian cloud image",
    placeholder: "Leave empty to download it", value: STATE.build_source_ok ? STATE.build_source : "" });  // not one deleted since
  let preset = "full", timer = null, build = null;
  const presets = STATE.presets || {}, recipes = STATE.recipes || {};
  const chosen = new Set(presets.full?.toolchains || []), open = new Set();
  const name = el("input", { class: "input grow", placeholder: "Plugin RE", maxlength: 40, "aria-label": "Name", spellcheck: false });
  const picker = el("div", { class: "image-picks" }), total = el("div", { class: "card image-total" });
  const hostsNote = el("span", { class: "muted small pretty" });
  const nameRow = el("label", { class: "field image-name", hidden: true }, el("span", { class: "label" }, "Name"), name);
  const options = el("div", { class: "stack tight image-options", hidden: true });
  const optionsMark = el("span", {}, foldMark(false));
  const optionsBtn = el("button", { class: "btn small ghost", type: "button", "aria-expanded": "false", onclick: () => {
    options.hidden = !options.hidden;
    optionsBtn.setAttribute("aria-expanded", String(!options.hidden));
    optionsMark.replaceChildren(foldMark(!options.hidden));
  } }, optionsMark, "Build options");
  const forWhom = () => preset === "slim" ? "research" : "coding";
  const renderCmd = () => {
    const custom = preset === "custom";
    aboutTip.replaceChildren(infoTip(presets[preset] ? `${presets[preset].about.replace(/^./, (ch) => ch.toUpperCase())}.` : "Your own mix, with a name. It doesn't replace a default until you make it one."));
    nameRow.hidden = !custom;
    const mix = [...withNeeds(chosen)].join(",") || "''";
    const from = src.value.trim() ? "--image " + shellWord(src.value.trim()) : "--download";
    cmd.replaceChildren(!custom ? copyBlock(`${cli()} build-base ${from} --preset ${preset}`)
      : copyBlock(`${cli()} build-base ${from} --with ${mix} --no-default`));
    renderPicker();
  };
  // what a build gets: the ticked toolchains and the ones they need
  const withNeeds = (set) => {
    const out = new Set();
    const add = (t) => { if (out.has(t) || !recipes[t]) return; (recipes[t].needs || []).forEach(add); out.add(t); };
    [...set].forEach(add);
    return out;
  };
  const renderPicker = () => {
    const all = withNeeds(chosen), mb = (STATE.image_base_mb || 0) + [...all].reduce((n, t) => n + (recipes[t]?.size_mb || 0), 0);
    const gb = (n) => n < 100 ? `${n} MB` : `${(n / 1000).toFixed(1)} GB`;
    const row = (key, r, on, locked, note) => el("label", { class: "image-pick" + (locked ? " locked" : "") },
      el("input", { type: "checkbox", checked: on, disabled: locked, onchange: (e) => {
        if (e.target.checked) chosen.add(key); else chosen.delete(key);
        if (preset !== "custom") { preset = "custom"; renderSeg(); }
        renderCmd();
      } }),
      el("span", { class: "grow ellipsis" }, r.label, infoTip(r.about)), note ? el("span", { class: "dim small nowrap" }, note) : "",
      el("span", { class: "muted small nowrap image-size" }, r.size_mb ? gb(r.size_mb) : ""));
    const neededBy = (t) => [...chosen].filter((c) => (recipes[c]?.needs || []).includes(t)).map((c) => recipes[c].label);
    picker.replaceChildren(
      row("base", { label: "Base", about: "Debian, git, gh, Python, Node, gcc, ripgrep, curl and Claude Code", size_mb: STATE.image_base_mb }, true, true, "Always"),
      ...Object.entries(recipes).map(([t, r]) => {
        const via = !chosen.has(t) && all.has(t) ? neededBy(t) : [];
        const needs = (r.needs || []).map((n) => recipes[n]?.label || n);
        return row(t, r, all.has(t), via.length > 0, via.length ? `needed by ${via.join(", ")}` : needs.length ? `adds ${needs.join(", ")}` : r.optional ? "large" : "");
      }));
    const hosts = [...new Set([...all].flatMap((t) => recipes[t]?.hosts || []))];
    hostsNote.textContent = "The build reaches Debian and the hosts on your allowlist" + (hosts.length ? `, plus ${hosts.join(", ")}` : "") + ". Agents never get the build's extra hosts.";
    const count = all.size;
    total.replaceChildren(el("div", { class: "grow stack0" }, el("strong", {}, `About ${gb(mb)} on disk`),
      el("span", { class: "muted small pretty" }, `Base and ${count} toolchain${count === 1 ? "" : "s"}`
        + (preset === "custom" ? " · doesn't replace a default" : ` · becomes the default for ${forWhom()} agents`))),
      el("div", { class: "row nowrap image-build" }, optionsBtn, build));
  };
  src.addEventListener("input", renderCmd);
  const act = async (fn, ok) => { try { await fn(); await loadState(); renderList(); if (ok) toast(ok); } catch (e) { toast(e.message, true); } };
  const startBuild = async (p, rebuild, mix) => {
    try {
      const st = await api("POST", "/api/images/build", { preset: p, source: src.value, rebuild, ...(mix || {}) });
      if (st.queued) toast("Added to the queue. It starts when the builds before it are done.");
      renderProgress(st);
      await loadState(); renderList(); watch();
    } catch (e) { toast(e.message, true); }
  };
  const unqueue = (j) => act(async () => renderProgress(await api("POST", "/api/images/queue", { remove: j.id })), "Taken off the queue");
  const jobLabel = (j) => j.label || presets[j.preset]?.label || j.preset;
  const buildNew = () => {
    if (preset !== "custom") { startBuild(preset); return; }
    if (!name.value.trim()) { toast("Give the image a name, like Plugin RE.", true); name.focus(); return; }
    startBuild("custom", null, { toolchains: [...withNeeds(chosen)], name: name.value.trim() });
  };
  const remove = async (i) => {
    const users = (i.used_by || []).map(agentName);
    const body = `Deletes the image file and frees ${fmtBytes(i.size)}. ` + (users.length
      ? `${users.join(", ")} ${users.length === 1 ? "goes" : "go"} back to the default image at the next start, without the tools only this one had.`
      : "No agent uses it.");
    if (!(await confirmModal(`Delete ${imageLabel(i)} (${i.name.replace(/\.qcow2$/, "")})?`, body, "Delete image", true))) return;
    act(() => api("POST", "/api/images/delete", { name: i.name }), `Deleted it. Freed ${fmtBytes(i.size)}.`);
  };
  const renderDefaults = () => {
    const card = (kind, img) => el("div", { class: "image-card " + kind },
      el("div", { class: "row nowrap" }, el("span", { class: "image-kind" }, kind === "coding" ? "Coding default" : "Research default"), el("span", { class: "grow" }),
        img ? el("span", { class: "muted small" }, `${(img.used_by || []).length} agent${(img.used_by || []).length === 1 ? "" : "s"}`) : ""),
      img ? [el("div", { class: "row tight image-card-name" }, el("span", { class: "title" }, imageLabel(img)),
          el("span", { class: "muted small" }, [fmtBytes(img.size), builtText(img).toLowerCase()].filter(Boolean).join(" · "))),
        el("div", { class: "muted small ellipsis" }, img.toolchains.length ? "Base, " + toolchainText(img) : "Git, Python, Node and the basics")]
        : el("div", { class: "muted small" }, "None yet. Build one below."));
    const by = (n) => (STATE.images || []).find((i) => i.name === n);
    defaults.replaceChildren(card("coding", by(STATE.base_image)), card("research", by(STATE.research_base_image)));
  };
  const imageMenu = (i, mine, queued, building) => {
    const coding = i.name === STATE.base_image, research = i.name === STATE.research_base_image;
    const canRebuild = !!presets[i.preset] || i.preset === "custom";
    const item = (label, sub, enabled, fn, cls) => el("button", { class: "menu-item " + (cls || ""), disabled: !enabled, onclick: () => { closePop(); fn(); } },
      el("span", {}, label, sub ? el("span", { class: "sub" }, sub) : ""));
    const menu = el("div", { class: "menu" },
      item(coding ? "Default for coding" : "Make default for coding", coding ? "" : "New coding agents start from it", !coding,
        () => act(() => api("POST", "/api/images/default", { name: i.name, for: "coding" }), `New coding agents now start from ${imageLabel(i)}`)),
      item(research ? "Default for research" : "Make default for research", research ? "" : "New research agents start from it", !research,
        () => act(() => api("POST", "/api/images/default", { name: i.name, for: "research" }), `New research agents now start from ${imageLabel(i)}`)));
    if (canRebuild) menu.append(item(mine ? "Building…" : queued ? "Queued" : building ? "Queue rebuild" : "Rebuild", (imageAge(i) > IMAGE_OLD_DAYS ? "Over 30 days old, rebuild for updates. " : "") + "Fresh copy, same toolchains. It becomes the default for its kind of agent.",
      !mine && !queued, () => startBuild(i.preset, i.name, i.preset === "custom" ? { toolchains: i.toolchains, name: imageLabel(i) } : null)));
    menu.append(el("div", { class: "sep" }), item("Delete", coding || research ? "It's a default. Pick another first." : building ? "Wait for the build to finish" : `Frees ${fmtBytes(i.size)}`,
      !coding && !research && !building, () => remove(i), "danger"));
    return menu;
  };
  const renderList = () => {
    const building = STATE.build?.state === "running", b = STATE.build || {};
    const waiting = new Set((b.queue || []).map((j) => j.rebuild).filter(Boolean));
    renderDefaults();
    const imgs = [...(STATE.images || [])].reverse();
    const unused = imgs.filter((i) => i.name !== STATE.base_image && i.name !== STATE.research_base_image && !(i.used_by || []).length);
    cleanBtn.hidden = !unused.length || building;
    cleanBtn.textContent = `Delete ${unused.length} unused`;
    cleanBtn.onclick = async () => {
      const freed = fmtBytes(unused.reduce((n, i) => n + (i.size || 0), 0));
      if (!(await confirmModal(`Delete ${unused.length} unused image${unused.length === 1 ? "" : "s"}?`, `No agent uses ${unused.length === 1 ? "it" : "them"}, and neither is a default. Frees ${freed}.`, "Delete images", true))) return;
      act(async () => { for (const i of unused) await api("POST", "/api/images/delete", { name: i.name }); }, `Deleted ${unused.length}. Freed ${freed}.`);
    };
    table.replaceChildren(el("div", { class: "image-line image-colhead" }, el("span", {}, "Image"), el("span", {}, "Size"), el("span", {}, "Built"), el("span", {}, "Used by"), el("span")),
      ...(imgs.length ? imgs.map((i) => {
        const coding = i.name === STATE.base_image, research = i.name === STATE.research_base_image, days = imageAge(i);
        const mine = building && b.rebuild === i.name, shown = open.has(i.name);
        const users = (i.used_by || []).map(agentName);
        const when = i.built ? new Date(i.built).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
        const more = el("button", { class: "icon-btn", type: "button", "aria-label": "More actions", title: `Actions for ${imageLabel(i)}`, onclick: () => openPop(more, imageMenu(i, mine, waiting.has(i.name), building)) }, "···");
        return el("div", { class: "image-row" },
          el("div", { class: "image-line" },
            el("button", { class: "image-name-cell", type: "button", "aria-expanded": String(shown), onclick: () => { if (!open.delete(i.name)) open.add(i.name); renderList(); } },
              foldMark(shown), el("span", { class: "image-mark " + (coding ? "coding" : research ? "research" : "none"), title: coding ? "Coding default" : research ? "Research default" : "Not a default" }),
              el("span", { class: "title" }, imageLabel(i)), el("span", { class: "dim small mono" }, when),
              i.outdated ? el("span", { class: "tag warn", title: "Built before cilly's rename, so agents can't start from it. Rebuild it from its ··· menu." }, "Rebuild needed") : ""),
            el("span", { class: "image-meta" },
              el("span", { class: "image-cell" }, i.size ? fmtBytes(i.size) : ""),
              mine ? el("span", { class: "image-cell accent-fg" }, `Building… (${b.step || 1} of ${(b.steps || []).length || 3})`)
                : el("span", { class: "image-cell " + (days > IMAGE_OLD_DAYS ? "warn" : "muted") },
                  days == null ? "" : days < 1 ? "Today" : `${days} day${days === 1 ? "" : "s"} ago`),
              el("span", { class: "image-cell" + (users.length ? "" : " dim") }, !users.length ? "Not used" : users.length > 3 ? `${users.length} agents` : users.join(", "))),
            more),
          shown ? el("div", { class: "image-more stack tight" }, el("div", { class: "row tight" }, ...(i.toolchains.length ? ["Base", ...i.toolchains.map((t) => STATE.recipes?.[t]?.label || t)] : ["Git, Python, Node and the basics"])
              .map((t) => el("span", { class: "tool-chip" }, t))), el("code", { class: "dim small" }, i.name.replace(/\.qcow2$/, ""))) : "");
      }) : [el("div", { class: "list-row muted" }, "No images yet. Build one below.")]));
  };
  let logOpen = false;  // Show details stays open across redraws
  const renderProgress = (st) => {
    if (build) build.textContent = st?.state === "running" ? "Add to queue" : "Build image";
    if (STATE.build && st) Object.assign(STATE.build, { queue: st.queue || [], results: st.results || [] });
    if (!st || st.state === "idle") { progress.replaceChildren(); return; }
    const name = st.label || presets[st.preset]?.label || st.preset;
    const label = { running: `Building ${name}`,
      done: presets[st.preset] ? `Built ${st.image}. It's now the default for ${st.preset === "slim" ? "research" : "coding"} agents.`
        : `Built ${st.label || st.image}. Make it the default, or pick it for an agent under New agent → Options.`,
      failed: `The build failed. ${st.error || "See the log."}` }[st.state];
    const pre = el("pre", { class: "block build-log" }, (st.lines || []).slice(-80).join("\n"));
    const details = el("details", { class: "adv build-details", open: logOpen, ontoggle: () => { logOpen = details.open; if (logOpen) pre.scrollTop = pre.scrollHeight; } },
      el("summary", {}, "Show details"), el("div", { class: "adv-body" }, pre));
    const pct = st.state === "done" ? 100 : st.progress?.percent || 0, mins = st.started ? Math.floor((Date.now() / 1000 - st.started) / 60) : null;
    const bar = el("div", { class: "progress build-progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct, "aria-label": "Build progress" },
      el("span", {}));
    bar.firstChild.style.width = `${pct}%`;
    const running = st.state === "running" ? el("div", { class: "card stack tight build-card" },
      el("div", { class: "row nowrap" }, el("span", { class: "title grow ellipsis" }, label), el("span", { class: "muted small" }, `${pct}%`)), bar,
      el("div", { class: "row nowrap muted small" }, el("span", { class: "grow ellipsis" }, buildNow(st)),
        el("span", {}, mins === null ? "" : mins < 1 ? "Just started" : `${mins} min`, infoTip("A build usually takes 5 to 15 minutes. You can leave this page."))), details) : null;
    // earlier builds that failed while others ran after them, so the next one doesn't hide it
    const failed = (st.results || []).slice(0, st.state === "running" ? undefined : -1).filter((r) => r.state === "failed");
    const queue = st.queue || [];
    progress.replaceChildren(...failed.map((r) => el("div", { class: "err small" }, `Failed: ${jobLabel(r)}. ${r.error || ""}`)),
      running || el("div", { class: st.state === "failed" ? "err" : "ok" }, label),
      queue.length ? el("div", { class: "card flush build-queue" }, el("div", { class: "list-row muted small" }, `Waiting (${queue.length})`),
        ...queue.map((j, n) => el("div", { class: "list-row row nowrap" }, el("span", { class: "muted small" }, `${n + 1}.`),
          el("span", { class: "grow ellipsis" }, j.rebuild ? `${jobLabel(j)} (rebuild)` : jobLabel(j)),
          el("button", { class: "btn small ghost", onclick: () => unqueue(j) }, "Remove")))) : "", running ? "" : details);
    if (logOpen) pre.scrollTop = pre.scrollHeight;
  };
  const poll = async () => {
    if (!box.isConnected) { clearInterval(timer); timer = null; return; }
    let st;
    try { st = await api("GET", "/api/images/build"); } catch { return; }
    renderProgress(st);
    if (STATE.build) Object.assign(STATE.build, { state: st.state, step: st.step });
    renderList();
    if (st.state !== "running") { clearInterval(timer); timer = null; await loadState(); renderList(); }
  };
  const watch = () => { if (!timer) timer = setInterval(poll, 2000); poll(); };
  build = el("button", { class: "btn primary", onclick: buildNew }, "Build image");
  const segBox = el("span");
  const renderSeg = () => segBox.replaceChildren(seg([...Object.entries(presets).map(([n, p]) => [n, p.label]), ["custom", "Custom"]], preset, (v) => {
    preset = v;
    if (presets[v]) { chosen.clear(); presets[v].toolchains.forEach((t) => chosen.add(t)); }
    renderCmd();
  }, "small"));
  const cleanBtn = el("button", { class: "btn small ghost", type: "button", hidden: true });
  renderSeg();
  renderList(); renderCmd();
  // once the section is on the page: pick up a build that's already running (you left and came back)
  imagesKick = () => (STATE.build?.state === "running" ? watch() : poll());
  setTimeout(imagesKick);
  options.append(el("label", { class: "field" }, el("span", { class: "label" }, "Debian cloud image", infoTip("Leave the file empty, and cilly downloads Debian 13's cloud image and checks that the download wasn't damaged. "
      + "Or give the full path of one you downloaded and checked yourself. Coding agents use the all-in-one image and research agents the slim one. "
      + "Rebuild monthly for updates.")), src),
    hostsNote,
    el("div", { class: "muted" }, "You can also build it in a terminal" + (cli() === "python3 -m cilly" ? ", from the cilly folder:" : ":")), cmd);
  box.append(defaults,
    el("div", { class: "set-sub" }, el("div", { class: "row" }, el("span", { class: "title grow" }, "All images"), cleanBtn)), table,
    el("div", { class: "image-legend small muted" }, el("span", {}, el("span", { class: "image-mark coding" }), "Coding default"),
      el("span", {}, el("span", { class: "image-mark research" }), "Research default"), el("span", {}, el("span", { class: "image-mark none" }), "Not a default")),
    el("div", { class: "set-sub image-new" }, el("div", { class: "row" }, el("span", { class: "title title-row grow" }, "New image", aboutTip), segBox)),
    nameRow, picker, total, options, progress);
  return box;
}
let imagesKick = null;  // shows a running build's progress once VM images is on the page (a phone shows it later)
const IMAGE_OLD_DAYS = 30;
function imageAge(img) {
  const t = Date.parse(img?.built || "");
  return isNaN(t) ? null : Math.floor((Date.now() - t) / 86400000);
}

// Settings → Devices: this browser, the devices you paired (each has its own token; revoking one
// signs it out at once) and the ones waiting for you, with their code.
let PAIRED_BOX = null;
function pairedSection() {
  PAIRED_BOX = el("div", { class: "card flush" }, el("div", { class: "list-row muted" }, "loading…"));
  setTimeout(refreshPaired);  // once it's on the page
  return PAIRED_BOX;
}
async function refreshPaired() {
  const box = PAIRED_BOX;
  if (!box || !box.isConnected || !isOwner()) return;
  let r;
  try { r = await api("GET", "/api/devices"); } catch (e) { box.replaceChildren(loadFailed("the devices", e, refreshPaired)); return; }
  const when = (t) => t ? fmtTime(new Date(t * 1000).toISOString()) : "not yet";
  const deny = async (p) => {
    try { await api("POST", `/api/devices/pending/${p.id}/deny`, {}); toast("Denied"); }
    catch (e) { toast(e.message, true); }
    refreshPaired(); pollInbox();
  };
  const revoke = async (d) => {
    if (!(await confirmModal(`Revoke ${d.name}?`, "It's signed out right away and has to be paired again to get back in.", "Revoke", true))) return;
    try { await api("POST", `/api/devices/${d.id}/revoke`, {}); toast(`Revoked ${d.name}`); } catch (e) { toast(e.message, true); }
    refreshPaired();
  };
  const host = (STATE.remote_hosts || [])[0];
  box.replaceChildren(
    el("div", { class: "list-row" }, el("div", { class: "grow stack0" }, el("span", {}, "This browser ", el("span", { class: "tag" }, "This device")),
      el("span", { class: "muted small" }, deviceToken() ? "Full access. It remembers the panel's access key." : "Full access, with the link the panel printed at startup.")),
    deviceToken() ? el("button", { class: "btn small ghost", onclick: () => { try { localStorage.removeItem(DEVICE_KEY); } catch { /* fine */ } toast("Forgotten. Next time, this device needs the link."); refreshPaired(); } }, "Forget the key") : ""),
    ...r.pending.map((p) => el("div", { class: "list-row pair-pending" },
      el("div", { class: "grow stack0" }, el("span", {}, p.name), el("span", { class: "warn small" }, "Waiting for approval. Review it to see its code.")),
      el("button", { class: "btn small", onclick: () => deny(p) }, "Deny"), el("button", { class: "btn small primary", onclick: async () => { await pollInbox(); review(`dev:${p.id}`); } }, "Review"))),
    ...r.devices.map((d) => el("div", { class: "list-row" },
      el("div", { class: "grow stack0" }, el("span", {}, d.name), el("span", { class: "muted small" }, `Paired ${when(d.created)}, last used ${when(d.last_seen)}`)),
      el("button", { class: "btn small ghost", onclick: () => revoke(d) }, "Revoke"))),
    el("div", { class: "list-row stack0 pair-help" }, el("span", {}, "Pair a new device"),
      el("span", { class: "muted small pretty" }, host ? `Open https://${host} on it and choose Pair this device. A request with a short code shows up here and in your inbox.`
        : "Set up the HTTPS proxy from the README, then open the panel on it and choose Pair this device. A request with a short code shows up here and in your inbox.")));
}
const pairCode = (code) => `${String(code).slice(0, 3)} ${String(code).slice(3)}`;
// Notifications (kept in this browser, not in the config).  Also on a paired phone's More page.
function deviceSection() {
  const box = el("div", { class: "card flush" }), p = notePrefs();
  const status = el("span", { class: "muted small" });
  const showStatus = () => {
    status.textContent = !canNotify() ? "Not available here, because notifications need HTTPS or localhost."
      : Notification.permission === "denied" ? "Blocked in this browser's site settings."
      : notePrefs().on ? "On while the panel is open in this browser, even in a background tab." : "Off";
  };
  const opt = (key, label, sub) => el("label", { class: "list-row" }, el("div", { class: "grow stack0" }, el("span", {}, label), el("span", { class: "muted small" }, sub)),
    sw(p[key], (on, b) => { p[key] = on; setSw(b, on); saveNotePrefs(p); }));
  const main = sw(p.on, async (on, b) => {
    if (on && !(await enableNotifications())) { setSw(b, false); return; }
    p.on = on; setSw(b, on); saveNotePrefs(p); showStatus();
    if (on) notify("Notifications are on", "You'll hear from cilly when an agent finishes, fails or needs you.");
  });
  showStatus();
  box.append(el("label", { class: "list-row" }, el("div", { class: "grow stack0" }, el("span", {}, "Notify me in this browser"), status), main),
    opt("done", "When an agent finishes", "“coder finished. Ready for your next message.”"),
    opt("fail", "When an agent fails", "“web-fixer: task failed”, with the reason"),
    opt("needs", "When an agent needs you", "Research questions, blocked sites, findings and devices to pair"));
  return box;
}
