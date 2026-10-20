// cilly UI: projects. A project is a reusable setup for agents that work on the same thing:
// instructions, allowed sites, repositories, defaults for new agents, a research agent,
// files every coding agent gets read-only, and notes that agents suggest and you approve.
"use strict";

function projectById(pid) { return (STATE?.projects || []).find((p) => p.id === pid) || null; }
function projectName(pid) { return projectById(pid)?.name || ""; }

async function newProject(then) {
  const name = await promptModal("New project", "Letters, digits, spaces and . _ ( ) & + ' - (up to 60)", "", "Create");
  if (!name || !name.trim()) return null;
  try {
    const p = await api("POST", "/api/projects", { name });
    await loadState();
    if (then) then(p); else go("project", { projectId: p.id });
    return p;
  } catch (e) { toast(e.message, true); return null; }
}
async function updateProject(pid, body, ok) {
  try {
    const r = await api("POST", `/api/projects/${pid}`, body);
    if (ok) toast(ok);
    await loadState();
    return r;
  } catch (e) { toast(e.message, true); return null; }
}

const PROJECT_DRAFTS = {};  // project id -> what you typed and haven't saved, kept while you move between its screens

VIEWS.project = {
  mount(main) {
    const page = el("div", { class: "page" }), inner = el("div", { class: "page-inner project-page" });
    page.append(inner); main.append(page);
    VIEWS.project.p = { inner };
  },
  update() {
    const P = VIEWS.project.p, p = projectById(UI.projectId);
    if (!P || !STATE) return;
    if (!p) { P.inner.replaceChildren(el("div", { class: "dashed-empty" }, "This project doesn't exist anymore.")); return; }
    const members = STATE.agents_list.filter((a) => a.project === p.id);
    // a typed draft survives redraws until you save it
    if (!changed("project", [p, members.map((a) => [a.id, a.name, statusText(a)]), isOwner(), isPhone(), STATE.repos, STATE.mcp_servers, STATE.plugins, STATE.skills, STATE.images,
      STATE.agents_list.filter((a) => a.profile === "research").map((a) => [a.id, a.name])])) return;
    if (P.inner.contains(document.activeElement) && document.activeElement.matches("textarea, input:not([type=checkbox])")) { SIGS.project = null; return; }  // not while you type
    const drafts = PROJECT_DRAFTS[p.id] ||= {};
    P.inner.classList.toggle("m-proj", isPhone());
    P.inner.replaceChildren(...(!isOwner() ? projectPageReadOnly(p, members) : isPhone() ? projectPhone(p, members, drafts) : projectPage(p, members, drafts)));
  },
};

// The name (an inline field for the owner), the agents as chips, + New agent here and Delete project
function projectHeader(p, members) {
  const owner = isOwner(), title = owner ? projectTitle(p) : el("h1", { class: "page-title" }, p.name);
  const head = el("div", { class: "row nowrap project-head" },
    el("div", { class: "grow stack tight" }, title, projectChips(p, members)),
    owner ? el("button", { class: "btn ghost danger-hover", onclick: () => deleteProject(p) }, "Delete project") : "");
  return isPhone() ? el("div", { class: "stack tight" }, projectTop(() => go("agents"), "Agents"), head) : head;
}
// A phone's way up: "← Agents" on the project, "← Lantern" on one of its screens
const projectTop = (back, label, more) => el("div", { class: "row nowrap m-proj-top" },
  el("button", { class: "link m-back-link", onclick: back }, "← " + label), el("span", { class: "grow" }), more || "");
function projectChips(p, members) {
  return el("div", { class: "row tight" }, ...members.map((a) => el("button", { class: "agent-chip", type: "button", title: statusText(a), onclick: () => go("agent", { agentId: a.id, tab: "chat" }) },
    stateIcon(statusKey(a)), a.name)),
    members.length ? "" : el("span", { class: "muted small" }, "No agents yet. Move one here from its ⋮ menu, or create one here."),
    el("button", { class: "chip add", type: "button", onclick: () => { NEW.project = p.id; NEW.applied = null; go("new"); } }, "+ New agent here"));
}
async function deleteProject(p) {
  if (!(await confirmModal(`Delete ${p.name}?`, "Its agents stay, with their own settings, under Other agents. The project's instructions, sites, notes and files are deleted.", "Delete project", true))) return;
  try { await api("POST", `/api/projects/${p.id}/delete`, {}); toast(`Deleted ${p.name}`); await loadState(); go("new"); } catch (e) { toast(e.message, true); }
}
const projSection = (title, sub, ...body) => el("div", { class: "stack tight proj-sec" },
  el("div", { class: "stack0" }, el("span", { class: "section-title" }, title), sub ? el("span", { class: "muted small pretty" }, sub) : ""), ...body);
// A paired device sees a project's settings without the controls: changing them is the PC's
const roRow = (label, value) => el("div", { class: "note-row ro-row" }, el("span", { class: "muted small" }, label), el("span", { class: "pretty" }, value));
const LIFECYCLES = { ephemeral: "Ephemeral", persistent: "Persistent", memory: "Ephemeral + memory" };
function projectReadParts(p) {
  const d = p.defaults, none = () => el("span", { class: "muted" }, "Not set"), list = (xs) => (xs.length ? xs.join(", ") : none());
  const on = (v) => (v === true ? "On" : v === false ? "Off" : none());
  const research = (STATE.agents_list || []).find((a) => a.id === p.research_agent);
  return {
    instructions: { title: "Instructions", sub: "", body: [p.instructions ? el("div", { class: "q-full" }, p.instructions) : el("div", { class: "muted" }, "None yet.")] },
    notes: { title: "Notes", sub: "", body: [p.notes.length ? el("div", { class: "card flush" }, ...p.notes.map((n) => el("div", { class: "note-row" }, n.text))) : el("div", { class: "muted" }, "None yet.")] },
    files: { title: "Files", sub: "Read-only at /attachments/project/ for every coding agent here.",
      body: [p.files.length ? el("div", { class: "card flush" }, ...p.files.map((f) => el("div", { class: "note-row row nowrap" },
        el("code", { class: "grow ellipsis small" }, f.name), el("span", { class: "dim small" }, fmtBytes(f.size))))) : el("div", { class: "muted" }, "None yet.")] },
    defaults: { title: "Defaults for new agents", card: true, body: [el("div", { class: "proj-defaults" },
      roRow("Repositories", list(p.repos)), roRow("Lifecycle", LIFECYCLES[d.mode] || none()), roRow("Stop after each task", on(d.auto_stop)),
      roRow("Keep outboxes", d.keep_outbox ? "On" : "Off"), roRow("Model", d.model ? (d.model_server ? `${d.model} · ${d.model_server}` : d.model) : none()),
      roRow("Effort", (d.effort && EFFORTS.find(([v]) => v === d.effort)?.[1]) || none()),
      roRow("VM image", d.image ? d.image.replace(/\.qcow2$/, "") : none()), roRow("MCP servers", list(d.mcp)), roRow("Plugins", list(d.plugins || [])), roRow("Skills", list(d.skills || [])),
      roRow("Left out of the global allowlist", list(d.skip_global || [])))] },
    research: { title: "Web research", card: true, yolo: !!(p.research_agent && p.research_auto),
      body: [roRow("Answered by", research ? research.name : none()), p.research_agent ? roRow("YOLO mode", on(!!p.research_auto)) : ""] },
    sites: { title: "Allowed sites", card: true, body: [roRow("Route", routeLabel(p.route || STATE.routes?.standard)),
      el("div", { class: "note-row" }, p.allow.length ? el("pre", { class: "ro-sites" }, p.allow.join("\n")) : el("span", { class: "muted" }, "None yet."))] },
  };
}
function projectPageReadOnly(p, members) {
  return [projectHeader(p, members), el("div", { class: "muted pretty" }, "You can change a project's settings from your PC."), ...projectColumns(projectReadParts(p))];
}

// The project's name is a field: Enter or clicking away saves, Esc puts it back
function projectTitle(p) {
  const field = el("input", { class: "page-title title-field", value: p.name, maxlength: 60, spellcheck: false, title: "Click to rename", "aria-label": "Project name" });
  let cancel = false;
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); field.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel = true; field.blur(); }
  });
  field.addEventListener("blur", () => {
    const name = field.value.trim();
    if (cancel || !name || name === p.name) { field.value = p.name; cancel = false; return; }
    updateProject(p.id, { name }, `Renamed to ${name}`).then((r) => { if (!r) field.value = p.name; });
  });
  return field;
}
// Each part of a project: a section of the page on the PC, its own screen on a phone
function projectParts(p, drafts) {
  // instructions: Save stays grey until you change something
  const instr = el("textarea", { class: "editor prose", rows: 7, spellcheck: true, "aria-label": "Instructions",
    placeholder: "For example: We target Node 22 and use pnpm. Run the tests with pnpm test.", value: drafts.instructions ?? p.instructions });
  const instrNote = el("span", { class: "grow small" });
  const saveInstr = el("button", { class: "btn small primary", onclick: async () => {
    if (await updateProject(p.id, { instructions: instr.value }, "Saved. Agents get it at their next task.")) { delete drafts.instructions; SIGS.project = null; VIEWS.project.update(); }
  } }, "Save");
  const showDirty = () => {
    const dirty = drafts.instructions !== undefined && drafts.instructions !== p.instructions;
    saveInstr.disabled = !dirty;
    instrNote.textContent = dirty ? "Unsaved changes" : "Agents get it at their next task.";
    instrNote.className = "grow small " + (dirty ? "warn" : "dim");
  };
  instr.addEventListener("input", () => { drafts.instructions = instr.value; showDirty(); });
  showDirty();

  // suggested notes, from the agents (~/outbox/project-notes.md); you can edit one before adding it
  const pending = p.pending.map((n) => {
    const t = el("textarea", { class: "note-edit", rows: 2, value: n.text, "aria-label": "Suggested note" });
    const fit = () => { t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; };  // you read all of it before you add it
    t.addEventListener("input", fit);
    setTimeout(fit);  // once it's on the page
    return el("div", { class: "note-pending" },
      el("div", { class: "row nowrap" }, el("span", { class: "accent-fg small grow" }, `From ${n.agent ? agentName(n.agent) : "an agent"}`), el("span", { class: "dim small" }, ago(n.created))), t,
      el("div", { class: "row" }, el("span", { class: "grow" }),
        el("button", { class: "btn small ghost", onclick: () => noteAction(p, n.id, "dismiss", {}, "Dismissed") }, "Dismiss"),
        el("button", { class: "btn small primary", onclick: () => noteAction(p, n.id, "approve", { text: t.value }, "Added to the notes") }, "Add to notes")));
  });
  const noteIn = el("input", { class: "input bare grow", placeholder: "Add a note", maxlength: 500, "aria-label": "New note" });
  const addNote = async () => {
    if (!noteIn.value.trim()) return;
    try { await api("POST", `/api/projects/${p.id}/notes`, { text: noteIn.value }); toast("Note added"); noteIn.value = ""; await loadState(); } catch (e) { toast(e.message, true); }
  };
  noteIn.addEventListener("keydown", (e) => { if (e.key === "Enter") addNote(); });

  const picker = el("input", { type: "file", multiple: true, hidden: true });
  picker.addEventListener("change", () => uploadProjectFiles(p, [...picker.files]).then(() => { picker.value = ""; }));

  // allowed sites
  const sites = el("textarea", { class: "editor flat-top", rows: Math.min(12, Math.max(4, p.allow.length + 1)), spellcheck: false, "aria-label": "Allowed sites",
    placeholder: "crates.io\nstatic.crates.io\n*.example.com", value: drafts.sites ?? p.allow.join("\n") });
  const sitesOut = el("div", { class: "muted small pretty" }, drafts.skipped || "");
  const saveSites = el("button", { class: "btn small primary", disabled: drafts.sites === undefined, onclick: async () => {
    const r = await updateProject(p.id, { allow_text: sites.value }, "Saved. Running agents use them right away.");
    if (!r) return;
    delete drafts.sites;
    // kept until the next save: the page redraws after saving
    drafts.skipped = r.skipped?.length ? "Skipped " + r.skipped.map(([w, why]) => `${w} (${why})`).join(", ") + "." : "";
    SIGS.project = null; VIEWS.project.update();
  } }, "Save sites");
  sites.addEventListener("input", () => { drafts.sites = sites.value; saveSites.disabled = false; });

  // defaults for new agents
  const d = p.defaults, setD = (k, v) => updateProject(p.id, { defaults: { [k]: v } });
  const repos = (STATE.repos || []).map((r) => el("button", { class: "chip mono" + (p.repos.includes(r) ? " on" : ""), type: "button", onclick: () => {
    updateProject(p.id, { repos: p.repos.includes(r) ? p.repos.filter((x) => x !== r) : [...p.repos, r] });
  } }, r));
  const lib = Object.keys(STATE.mcp_servers || {});
  const research = (STATE.agents_list || []).filter((a) => a.profile === "research");
  const ra = el("select", { class: "input", "aria-label": "Answered by" }, el("option", { value: "" }, "None"), ...research.map((a) => el("option", { value: a.id }, a.name)));
  ra.value = p.research_agent || "";
  ra.addEventListener("change", () => updateProject(p.id, { research_agent: ra.value || null }, ra.value ? `${agentName(ra.value)} answers this project's questions` : "No research agent"));
  const keepIn = el("input", { class: "input mono", placeholder: "/home/you/agent-outboxes", value: drafts.keep ?? (d.keep_outbox || ""), spellcheck: false, "aria-label": "Keep outboxes" });
  keepIn.addEventListener("input", () => { drafts.keep = keepIn.value; });
  keepIn.addEventListener("change", async () => {
    const v = keepIn.value.trim();
    if (v !== (d.keep_outbox || "") && await updateProject(p.id, { defaults: { keep_outbox: v || null } }, v ? "New agents here keep their outbox" : "New agents here don't keep their outbox")) delete drafts.keep;
  });
  const pdRow = (...cells) => el("div", { class: "pd-row" + (cells.length > 1 ? " two" : "") }, ...cells);  // one or two settings side by side
  const optSec = (label, ...body) => el("div", { class: "opt-sec" }, el("span", { class: "muted small" }, label), ...body);

  const lines = p.instructions.split("\n").filter((l) => l.trim()).length;
  return {
    pending: pending.length ? { title: "Suggested notes", sub: "Agents suggest these by writing to ~/outbox/project-notes.md. Nothing is added until you say so.", body: pending } : null,
    instructions: { title: "Instructions", sub: "What every coding agent here should know. Added to CLAUDE.md after your Claude policy.", body: [instr],
      foot: el("div", { class: "row nowrap" }, instrNote, saveInstr), summary: lines ? plural(lines, "line") : "Empty" },
    notes: { title: "Notes", sub: "Short facts agents learned. Running agents use them right away.", summary: String(p.notes.length),
      body: [el("div", { class: "card flush" }, ...p.notes.map((n) => el("div", { class: "note-row row nowrap" }, el("span", { class: "grow pretty" }, n.text),
        el("button", { class: "btn small ghost danger-hover", onclick: () => noteAction(p, n.id, "remove", {}, "Note removed") }, "Remove"))),
      el("div", { class: "note-row row nowrap" }, noteIn, el("button", { class: "btn small", onclick: addNote }, "Add")))] },
    files: { title: "Files", sub: "Read-only for every coding agent here.", tip: "At /attachments/project/ in each coding agent's VM. Research agents don't get them. Up to 50 files, 25 MiB each.",
      summary: String(p.files.length),
      body: [el("div", { class: "card flush" }, ...p.files.map((f) => el("div", { class: "note-row row nowrap" },
        el("code", { class: "grow ellipsis small" }, f.name), el("span", { class: "dim small" }, fmtBytes(f.size)),
        el("button", { class: "btn small ghost danger-hover", onclick: async () => {
          try { await api("POST", `/api/projects/${p.id}/files`, { remove: f.name }); toast(`Removed ${f.name}`); await loadState(); } catch (e) { toast(e.message, true); }
        } }, "Remove"))),
      el("button", { class: "note-row add-row", type: "button", onclick: () => picker.click() }, "+ Upload files"), picker)] },
    defaults: { title: "Defaults for new agents", card: true, summary: p.repos.length ? plural(p.repos.length, "repo") : "Not set", body: [el("div", { class: "proj-defaults" },
      pdRow(optSec("Repositories", el("span", { class: "dim small" }, "Offered first when you create an agent here."),
        repos.length ? el("div", { class: "row tight" }, ...repos) : el("span", { class: "muted small" }, "Add repositories in Settings, GitHub access first."))),
      pdRow(optSec("Lifecycle", seg([["", "Not set"], ["ephemeral", "Ephemeral"], ["persistent", "Persistent"], ["memory", "Ephemeral + memory"]], d.mode || "",
        (v) => setD("mode", v || null), "small wrap"))),
      pdRow(optSec("Stop after each task", seg([["", "Not set"], ["on", "On"], ["off", "Off"]], d.auto_stop === true ? "on" : d.auto_stop === false ? "off" : "",
        (v) => setD("auto_stop", v === "" ? null : v === "on"), "small")),
      optSec(["Keep outboxes", infoTip("Ephemeral agents here copy ~/outbox into a new subfolder of this folder when they stop. Leave it empty to turn it off.")], keepIn)),
      pdRow(optSec("Model", modelSelect(d.model ? { model: d.model, server: d.model_server || undefined } : null,
        (m) => updateProject(p.id, { defaults: { model: m?.model || null, model_server: m?.server || null } }), () => { SIGS.project = null; VIEWS.project.update(); })),
      optSec("Effort", seg(EFFORTS, d.effort || "", (v) => setD("effort", v || null), "small wrap"))),
      pdRow(optSec("VM image", imageSelect(d.image || "", (v) => setD("image", v || null), "coding")),
      optSec("MCP servers", el("div", { class: "row tight" }, ...lib.map((n) => el("button", { class: "chip" + (d.mcp.includes(n) ? " on" : ""), type: "button",
        onclick: () => setD("mcp", d.mcp.includes(n) ? d.mcp.filter((x) => x !== n) : [...d.mcp, n]) }, n)),
      el("button", { class: "chip add", type: "button", onclick: () => mcpAddDialog(`Saved to your MCP library and turned on for new agents in ${p.name}.`,
        (n) => setD("mcp", [...(projectById(p.id)?.defaults.mcp || []), n])) }, "+ Add")))),
      Object.keys(STATE.plugins || {}).length ? pdRow(optSec("Plugins", el("div", { class: "row tight" }, ...Object.keys(STATE.plugins).map((n) => {
        const mine = d.plugins || [];
        return el("button", { class: "chip" + (mine.includes(n) ? " on" : ""), type: "button",
          onclick: () => setD("plugins", mine.includes(n) ? mine.filter((x) => x !== n) : [...mine, n]) }, n);
      })), el("span", { class: "dim small" }, "Coding agents here install them in their VM before their first task."))) : "",
      Object.keys(STATE.skills || {}).length ? pdRow(optSec("Skills", el("div", { class: "row tight" }, ...Object.keys(STATE.skills).map((n) => {
        const mine = d.skills || [];
        return el("button", { class: "chip" + (mine.includes(n) ? " on" : ""), type: "button", title: STATE.skills[n].description,
          onclick: () => setD("skills", mine.includes(n) ? mine.filter((x) => x !== n) : [...mine, n]) }, n);
      })), el("span", { class: "dim small" }, "Coding agents here get them in their VM before their first task."))) : "",
      pdRow(optSec("Global allowlist", el("details", { class: "adv", open: !!drafts.skipOpen, ontoggle: (e) => { drafts.skipOpen = e.target.open; } },
        el("summary", {}, (d.skip_global || []).length ? `${plural(d.skip_global.length, "site")} left out` : "All of it"),
        el("div", { class: "stack tight adv-body" }, el("span", { class: "dim small pretty" }, "New agents here start without the sites you uncheck. "
          + "Each agent can change it on its Sandbox tab."),
        ...globalSites(d.skip_global || [], (next) => setD("skip_global", next)))))))] },
    research: { title: "Web research", card: true, yolo: !!(p.research_agent && p.research_auto),
      tip: "Coding agents here ask this research agent. It stays in its own VM, without the project's files or instructions.",
      summary: p.research_agent ? agentName(p.research_agent) : "None", body: [
      optSec("Answered by", ra),
      p.research_agent ? el("label", { class: "opt-sec row nowrap" }, el("div", { class: "grow stack0" }, el("span", {}, "YOLO mode"),
        el("span", { class: "muted small" }, "Approve questions and forward findings without your review.")),
      sw(p.research_auto, async (on, b) => {
        if (on && !(await confirmModal("Turn on YOLO mode?", "Questions leave the sandbox and web findings reach this project's agents without you reading them first.", "Turn on"))) return;
        setSw(b, on); updateProject(p.id, { research_auto: on });
      }, "yolo")) : ""] },
    sites: { title: "Allowed sites", card: true, summary: String(p.allow.length),
      tip: "On top of the global allowlist. One per line. *.example.com covers subdomains, and example.com:8080 allows just that port.",
      body: [optSec("Route", routeSelect(p.route, (v) => updateProject(p.id, { route: v }, v ? `${p.name}'s agents take ${routeLabel(v)}` : `${p.name} follows Settings`),
        `Same as Settings (${routeLabel(STATE.routes?.standard)})`, "Project route"),
      el("span", { class: "dim small pretty" }, routeNote(p.route || STATE.routes?.standard) + " An agent's own route wins.")), sites, sitesOut], foot: el("div", { class: "row nowrap" }, el("span", { class: "dim small grow" }, "Running agents use them right away."), saveSites) },
  };
}
const PROJECT_MAIN = ["pending", "instructions", "notes", "files", "defaults"], PROJECT_SIDE = ["research", "sites"];
function projectColumns(parts) {
  const title = (x) => (x.tip ? [x.title, infoTip(x.tip)] : x.title);  // a longer explanation behind an (i)
  const section = (x) => x && projSection(title(x), x.sub, ...x.body, x.foot || "");
  const card = (x) => el("div", { class: "card flush" + (x.yolo ? " yolo-on" : "") },
    el("div", { class: "opt-head stack0" }, el("span", { class: "title-row" }, title(x)), x.sub ? el("span", { class: "muted small pretty" }, x.sub) : ""),
    ...x.body, x.foot ? el("div", { class: "opt-sec" }, x.foot) : "");
  return [el("div", { class: "proj-cols" }, el("div", { class: "proj-main" }, ...PROJECT_MAIN.map((k) => (parts[k]?.card ? card(parts[k]) : section(parts[k])) || "")),
    el("div", { class: "proj-side" }, ...PROJECT_SIDE.map((k) => card(parts[k]))))];
}
function projectPage(p, members, drafts) { return [projectHeader(p, members), ...projectColumns(projectParts(p, drafts))]; }
// A phone: the chips and suggested notes, then one row per part, each opening its own screen
function projectPhone(p, members, drafts) {
  const parts = projectParts(p, drafts), x = parts[UI.sec], top = projectTop;
  if (x && UI.sec !== "pending") {
    return [top(() => go("project", { projectId: p.id }), p.name),
      el("h1", { class: "m-h1" }, x.title), x.sub || x.tip ? el("div", { class: "muted small pretty" }, [x.sub, x.tip].filter(Boolean).join(" ")) : "",  // a phone's own screen has room
      ...(x.card ? [el("div", { class: "card flush" + (x.yolo ? " yolo-on" : "") }, ...x.body)] : x.body),
      x.foot ? el("div", { class: "m-foot" }, x.foot) : ""];
  }
  const title = projectTitle(p);
  title.classList.add("m-h1");
  const rename = () => { title.focus(); title.select(); };
  const menu = el("button", { class: "icon-btn", "aria-label": `Actions for ${p.name}`, onclick: () => openPop(menu, el("div", { class: "menu" },
    el("button", { class: "menu-item", onclick: () => { closePop(); rename(); } }, "Rename"),
    el("button", { class: "menu-item danger", onclick: () => { closePop(); deleteProject(p); } }, "Delete project"))) }, "···");
  const more = el("div", { class: "row nowrap" }, el("button", { class: "link", onclick: rename }, "Rename"), menu);
  const row = (k) => el("button", { class: "m-set-row", onclick: () => go("project", { projectId: p.id, sec: k }) },
    el("span", { class: "grow" }, parts[k].title), el("span", { class: "muted" }, parts[k].summary), el("span", { class: "chev" }, "›"));
  return [top(() => go("agents"), "Agents", more),
    title, projectChips(p, members),
    parts.pending ? projSection(`${parts.pending.title} · ${parts.pending.body.length}`, "", ...parts.pending.body) : "",
    el("div", { class: "m-set-list" }, ...["instructions", "notes", "files", "defaults", "research", "sites"].map(row))];
}
async function noteAction(p, nid, action, body, ok) {
  try { await api("POST", `/api/projects/${p.id}/notes/${nid}/${action}`, body); toast(ok); await Promise.all([loadState(), pollInbox()]); }
  catch (e) { toast(e.message, true); }
}
async function uploadProjectFiles(p, files) {
  for (const f of files) {
    if (f.size > 25 * 1024 * 1024) { toast(`${f.name} is over 25 MiB`, true); continue; }
    toast(`Uploading ${f.name}…`);
    try { await api("POST", `/api/projects/${p.id}/files`, { name: f.name, data: await fileToBase64(f) }); }
    catch (e) { toast(`${f.name}: ${e.message}`, true); return; }
  }
  toast(files.length === 1 ? `Added ${files[0].name}` : `Added ${files.length} files`);
  await loadState();
}
