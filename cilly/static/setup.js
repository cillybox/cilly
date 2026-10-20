// cilly UI: first-run setup (host check, logins, repositories, network, policy, first agent)
// and the "Finish setting up" checklist shown on the New agent page until everything is done.
"use strict";

const SETUP_STEPS = [["host", "Host check"], ["secrets", "Secrets"], ["repos", "Repositories"],
                     ["network", "Network"], ["policy", "Policy"], ["agent", "First agent"]];
const CLAUDE_HOSTS = "api.anthropic.com, platform.claude.com, claude.ai";
// Network presets: each chip adds or removes a group of hosts from the global allowlist.
const PRESETS = [
  ["GitHub", ["github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com"]],
  ["Debian packages", ["deb.debian.org", "security.debian.org"]],
  ["npm", ["registry.npmjs.org"]],
  ["PyPI", ["pypi.org", "files.pythonhosted.org"]],
  ["Maven + Gradle", ["repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "services.gradle.org", "downloads.gradle.org"]],
  ["Paper MC", ["repo.papermc.io", "repo.codemc.io", "repo.opencollab.dev"]],
  ["Docker Hub", ["registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com"]],
  ["crates.io", ["crates.io", "index.crates.io", "static.crates.io"]],
  ["Go modules", ["proxy.golang.org", "sum.golang.org"]],
];
const CMD = {
  kvm: "sudo usermod -aG kvm $USER   # then log out and back in",
  token: "claude setup-token",
};
// The command that installs a package on this PC's distribution ("" when cilly doesn't know it),
// and how to run cilly here: `cilly`, or `python3 -m cilly` from its folder.
const hostCmd = (need) => STATE?.host?.cmds?.[need] || "";
const cli = () => STATE?.host?.cli || "python3 -m cilly";
// One word for a shell command you copy: a path with spaces or quotes in it stays one path
const shellWord = (s) => /^[A-Za-z0-9_\/.,:@%+=~-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'";

function copyBlock(cmd) {
  return el("div", { class: "copy-block" }, el("code", { class: "grow" }, cmd),
    el("button", { class: "link", onclick: async (e) => {
      try { await navigator.clipboard.writeText(cmd); e.target.textContent = "Copied"; } catch { toast("Copy it by hand", true); }
    } }, "Copy"));
}
async function markStep(step) { try { await api("POST", "/api/setup", { step }); } catch { /* not important */ } }
async function finishSetup() {
  try { await api("POST", "/api/setup", { done: true }); } catch (e) { toast(e.message, true); }
  await loadState();
}

VIEWS.setup = {
  mount(main) {
    $("app").classList.add("setup-mode");
    const P = (VIEWS.setup.p = {});
    P.stepper = el("nav", { class: "stepper", "aria-label": "Setup steps" });
    P.body = el("div", { class: "setup-body" });
    main.append(el("div", { class: "setup" }, P.stepper, el("div", { class: "page" }, P.body)));
    UI.setupStep = UI.setupStep || 0;
    renderSetup();
  },
  unmount() { $("app").classList.remove("setup-mode"); },
  update() { const P = VIEWS.setup.p; if (P && SETUP_STEPS[UI.setupStep][0] === "host") renderSetup(); },
};
function setupGo(i) { UI.setupStep = i; renderSetup(); }
const leaveSetup = async () => { await finishSetup(); go("new"); };
function renderSetup() {
  const P = VIEWS.setup.p, su = STATE.setup, cur = UI.setupStep;
  P.stepper.replaceChildren(
    el("div", { class: "sb-logo" }, el("img", { class: "logo", src: "/logo.png", alt: "" }), el("span", { class: "sb-name" }, "cilly")),
    ...SETUP_STEPS.map(([k, label], i) => {
      const done = i < cur || su.steps.includes(k);
      return el("button", { class: "step" + (i === cur ? " now" : ""), onclick: () => setupGo(i) },
        el("span", { class: "step-dot" + (done && i !== cur ? " done" : i === cur ? " now" : "") }, done && i !== cur ? "✓" : String(i + 1)), label);
    }),
    el("span", { class: "grow" }),
    el("button", { class: "link step-exit", onclick: leaveSetup }, "Finish setting up later"));
  const step = SETUP_STEPS[cur][0];
  P.body.className = "setup-body " + ({ policy: "wide", agent: "centered" }[step] || "");
  // narrow screens have no stepper, so this line says where you are
  const narrowHead = el("div", { class: "row nowrap setup-narrow-head" }, el("span", { class: "muted grow" }, `Step ${cur + 1} of ${SETUP_STEPS.length} · ${SETUP_STEPS[cur][1]}`),
    el("button", { class: "link", onclick: leaveSetup }, "Finish later"));
  P.body.replaceChildren(narrowHead, ...({ host: setupHost, secrets: setupSecrets, repos: setupRepos, network: setupNetwork, policy: setupPolicy, agent: setupAgent })[step]());
}
function setupHead(title, sub, cls = "") {
  return [el("h1", { class: "setup-title " + cls }, title), sub ? el("div", { class: "muted pretty " + cls }, sub) : ""];
}
function setupNav({ back = true, left, next = "Continue", onNext, nextDisabled, skip } = {}) {
  const cur = UI.setupStep;
  const nx = el("button", { class: "btn primary", disabled: !!nextDisabled, onclick: async () => {
    nx.disabled = true;
    if (onNext && (await onNext()) === false) { nx.disabled = false; return; }
    setupGo(cur + 1);
  } }, next);
  return el("div", { class: "row setup-nav" }, left || (back && cur > 0 ? el("button", { class: "btn ghost", onclick: () => setupGo(cur - 1) }, "Back") : ""),
    el("span", { class: "grow" }), skip ? setupSkip(skip, cur) : "", nx);
}

function setupSkip(skip, cur) {  // skip is a label, or [label, onclick] when skipping does more than move on
  const [label, onclick] = Array.isArray(skip) ? skip : [skip, () => setupGo(cur + 1)];
  return el("button", { class: "btn ghost", onclick }, label);
}

// 1. host
function setupHost() {
  const again = el("button", { class: "btn", onclick: async () => { await checkHostAgain(); renderSetup(); toast(hostProblems().length ? "Still missing: " + hostProblems().join(", ") : "Host ready"); } }, "Check again");
  const required = !hostProblems().length;
  return [...setupHead("Set up cilly", "Each agent runs in its own VM on this PC. First, let's check it can run them."),
    ...hostChecks(),
    setupNav({ left: again, nextDisabled: !required, onNext: () => markStep("host") }),
    !required ? el("div", { class: "setup-note" }, "Continue unlocks once QEMU, KVM access and a VM image are in place, and bubblewrap works if it's installed.") : ""];
}

// What this PC has, and the command for each missing piece: setup's first step, and Settings → This PC.
// The server says what install.sh would do here (host.info), and offers it only where it helps.
function startImageBuild() {
  // a file you gave once and deleted since: download Debian's image instead (there's no field here to clear it)
  return api("POST", "/api/images/build", { preset: "full", source: STATE.build_source_ok ? STATE.build_source : "" })
    .then(loadState, (e) => toast(e.message, true)).then(() => imagesKick?.());  // VM images, if it's on the page, follows it too
}
// "Step 1 of 3: Downloading Debian, 45% (180 of 400 MB)", for a running build
// What an image build is doing right now, like "Installing Rust (3 of 9)"
function buildNow(b) {
  const step = b.step || 1, d = b.download, p = b.progress || {};
  if (step === 1 && d) return d[1] ? `Downloading Debian, ${Math.floor((d[0] * 100) / d[1])}% (${Math.floor(d[0] / 1e6)} of ${Math.floor(d[1] / 1e6)} MB)`
    : `Downloading Debian (${Math.floor(d[0] / 1e6)} MB)`;
  if (step === 2 && p.label) return `Installing ${p.label} (${p.part} of ${p.of})`;
  return step === 3 ? "Saving the image" : (b.steps || [])[step - 1] || "Preparing";
}
function buildStepText(b) {
  return `Step ${b.step || 1} of ${(b.steps || []).length || 3}: ${buildNow(b)}`;
}
function hostChecks(after) {  // after: redraw once a build has started (setup redraws with every state update)
  const t = STATE.tools, h = STATE.host || {}, b = STATE.build || {}, p = t.bwrap_problem;
  const row = (ok, name, detail, level, help, cmd) => el("div", { class: "check-row " + (ok ? "" : level) },
    el("div", { class: "row nowrap" }, el("span", { class: "check-mark " + (ok ? "ok" : level === "req" ? "err" : "warn") }, ok ? "✓" : level === "req" ? "✗" : "!"),
      el("span", { class: "grow" }, name), el("span", { class: ok ? "mono muted" : level === "req" ? "err" : "warn" }, ok ? detail : level === "req" ? "required" : "recommended")),
    !ok && help ? el("div", { class: "check-help " + (typeof help === "string" ? "muted" : "") }, help) : "", !ok && cmd ? copyBlock(cmd) : "");
  const pkg = (need, what) => hostCmd(need) ? what : what + " Install it from your distribution's packages.";
  const bwrapOk = !p && !(t.use_bwrap === "required" && !t.bwrap);  // else the build stops after downloading Debian
  const building = b.state === "running", canBuild = !!(t.qemu && t["qemu-img"] && t.kvm && t.xorriso && bwrapOk);
  const image = el("div", { class: "stack tight" },
    el("span", { class: "muted" }, "The VM every agent starts from. cilly downloads Debian 13's cloud image, checks that the download wasn't damaged, "
      + "and installs Claude Code and the toolchains in it. It takes 5 to 15 minutes."),
    building ? el("div", { class: "row nowrap" }, el("span", { class: "pulse" }), el("span", {}, `Building it. ${buildStepText(b)}. You can leave this page.`))
      : b.state === "failed" ? el("div", { class: "err" }, "The build failed. " + (b.error || "Settings, under VM images, has its log.")) : "",
    el("div", { class: "row" }, el("button", { class: "btn primary small", disabled: building || !canBuild, onclick: (e) => { e.target.disabled = true; startImageBuild().then(after); } },
      building ? "Building…" : b.state === "failed" ? "Try again" : "Build VM image"),
      !canBuild && !building ? el("span", { class: "muted small" }, bwrapOk ? "QEMU, KVM access and xorriso come first."
        : "QEMU, KVM access, xorriso and a working bubblewrap come first.") : ""));
  const bwrapRow = t.use_bwrap === "off" ? row(true, "bubblewrap (bwrap)", "off in config.json", "opt")
    : p ? row(false, "bubblewrap (bwrap) is blocked", "", "req", `It's installed but can't build its sandbox: ${p.why}. ${p.fix ? p.fix + " " : ""}`
        + "VMs don't start until it works, or until you set vm.use_bwrap to \"off\" in config.json yourself.", p.cmd)
      : t.use_bwrap === "required" ? row(!!t.bwrap, t.bwrap ? "bubblewrap (bwrap)" : "bubblewrap (bwrap) not installed", "host-side sandbox", "req",
        pkg("bwrap", "config.json sets vm.use_bwrap to \"required\", so VMs don't start without it."), hostCmd("bwrap"))
      : row(!!t.bwrap, t.bwrap ? "bubblewrap (bwrap)" : "bubblewrap (bwrap) not installed", "host-side sandbox", "opt",
        pkg("bwrap", "Optional extra host-side sandboxing around QEMU. cilly works without it."), hostCmd("bwrap"));
  return [h.distro ? el("div", { class: "muted small" }, `Running on ${h.distro}`) : "",
    h.unsupported ? el("div", { class: "note-box warn" }, el("span", { class: "pretty" }, h.unsupported)) : "",
    h.install_sh ? el("div", { class: "note-box" }, el("div", { class: "stack tight grow" },
      el("span", { class: "pretty" }, "The steps that need root are in one script. It installs QEMU, bubblewrap and xorriso, gives you access to /dev/kvm and, "
        + "on Ubuntu, lets bubblewrap work. Read it first, then run it in a terminal:"), copyBlock(h.install_sh))) : "",
    el("div", { class: "card flush" },
      row(!!t.qemu, "QEMU", "qemu-system-x86_64", "req", pkg("qemu", "Runs the VMs, as you (no root)."), hostCmd("qemu")),
      row(!!t["qemu-img"], "qemu-img", "disk images", "req", pkg("qemu", "Comes with QEMU."), hostCmd("qemu")),
      row(!!t.kvm, "KVM access", "/dev/kvm", "req", h.kvm === "missing" ? "This PC has no /dev/kvm. Turn on virtualization (Intel VT-x or AMD-V) in its firmware settings."
        : "Your user needs to be in the kvm group.", h.kvm === "missing" ? "" : CMD.kvm),
      row(!!t.xorriso, "xorriso", "builds VM images", STATE.base_image ? "opt" : "req", pkg("xorriso", "Builds the setup disk for a new VM image."), hostCmd("xorriso")),
      row(!!t.openssl, "openssl", "keeps tokens on this PC", t.keep_tokens ? "req" : "opt",
        pkg("openssl", "Makes each start's certificates, so the real Claude and GitHub tokens stay on this PC and VMs get stand-ins."), hostCmd("openssl")),
      row(!!STATE.base_image, "VM image", STATE.base_image || "", "req", image, ""),
      row(!!STATE.routes?.tor?.installed, "Tor", "for Tor routes", "opt", pkg("tor", "Only for agents whose route is Tor. cilly runs it as you, inside bubblewrap."), hostCmd("tor")),
      row(!!STATE.routes?.wireguard?.installed, "wireproxy", "for WireGuard VPN routes", "opt",
        "Only for agents whose route is a WireGuard VPN. cilly runs it as you, inside bubblewrap. "
          + (wireproxyCmd() ? "" : "Arch packages it. Elsewhere, get it from github.com/windtf/wireproxy."), wireproxyCmd()),
      bwrapRow)];
}

// 2. secrets
const secretStatus = (s) => s.startsWith("saved") ? "✓ Saved on this PC" : s.startsWith("command") ? "✓ From your command" : "✓ Set for this session";
function secretField(key, label, note, placeholder, help, status, remember, aside) {
  const fromCmd = status.startsWith("command");
  const inp = el("input", { type: "password", autocomplete: "off", spellcheck: false, placeholder, disabled: fromCmd, "aria-label": label });
  const right = el("span", { class: "aside" }, status === "missing" ? aside || "" : el("span", { class: "ok" }, secretStatus(status)));
  const box = el("div", { class: "secret-box" + (status === "missing" ? "" : " set") }, inp, right);
  const save = async () => {
    if (!inp.value.trim()) return true;
    try {
      const r = await pasteSecret(key, inp.value, remember.on);
      inp.value = ""; box.classList.add("set"); right.replaceChildren(el("span", { class: "ok" }, secretStatus(r.status)));
      field.pasted = true;
      return true;
    } catch (e) { toast(e.message, true); return false; }
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  const field = { save, pasted: false, node: el("div", { class: "field" }, el("span", { class: "section-title" }, label, el("span", { class: "muted" }, note)), box, help) };
  return field;
}
function setupSecrets() {
  const su = STATE.setup, remember = { on: true };
  const claude = secretField("agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "Claude login", "Required", "Paste the token",
    el("div", { class: "stack tight" }, el("span", { class: "field-note" }, "Create a token for your Claude subscription with this command, then paste it here."),
      copyBlock(CMD.token)), su.claude, remember);
  const research = secretField("research:CLAUDE_CODE_OAUTH_TOKEN", "Research agents' login", "Optional", "Paste a second token",
    el("span", { class: "field-note" }, "Give research agents their own token, so a leak only costs you that one."), su.research, remember);
  const gh = secretField(`token:${su.token}`, "GitHub token", "Needed for repos and pull requests", "Paste a fine-grained token",
    el("span", { class: "field-note" }, "Create it signed in as the agents' own bot account. Give it read and write on Contents and Pull requests, ",
      "read on Metadata, and only the repos agents should touch."), su.github, remember,
    ghLink("https://github.com/settings/personal-access-tokens/new", "Create one on GitHub"));
  const saveAll = async (fields) => {
    for (const f of fields) if (!(await f.save())) return false;
    if (gh.pasted) {  // the bot's handle and noreply address for its agents' commits, unless you set them already
      try { const r = await api("POST", "/api/identity/lookup", { save: true }); if (r.saved) toast(`Agents will commit as ${r.name}`); }
      catch { /* Settings, GitHub access, can fill them in later */ }
    }
    await markStep("secrets"); await loadState();
  };
  // Skipping GitHub skips the repositories too: without the token there's nothing to list or clone.
  const skipGitHub = async () => { if ((await saveAll([claude, research])) !== false) setupGo(UI.setupStep + 2); };
  return [...setupHead("Add your keys", STATE.can_save_secrets ? "Saved encrypted on this PC, so you paste each one once."
      : "Secrets are held in memory only, never written to disk. You'll paste them again after cilly restarts."),
    claude.node, research.node, gh.node, saveSwitch((on) => { remember.on = on; }),
    setupNav({ skip: su.github === "missing" ? ["Skip GitHub for now", skipGitHub] : null, onNext: () => saveAll([claude, research, gh]) })];
}

// 3. repositories
const reposNext = (n) => n ? `Continue with ${n} ${n === 1 ? "repo" : "repos"}` : "Continue";
function updatedAgo(iso) {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000);
  if (!(days >= 0)) return "";
  return "updated " + (days < 1 ? "today" : days < 2 ? "yesterday" : days < 14 ? `${days} days ago`
    : days < 60 ? `${Math.floor(days / 7)} weeks ago` : `${Math.floor(days / 30)} months ago`);
}
function setupRepos() {
  const su = STATE.setup, box = el("div", { class: "card flush" }, el("div", { class: "list-row muted" }, "Asking GitHub which repositories the token reaches…"));
  const warn = el("div", { class: "stack tight", hidden: true });
  const chosen = new Set(STATE.repos);
  const manual = el("input", { class: "input mono grow", placeholder: "owner/name (if a repository doesn't show up)", spellcheck: false });
  let listed = [];
  const nav = setupNav({ skip: "Skip for now", next: reposNext(chosen.size), onNext: async () => {
    try {
      const s = await api("GET", "/api/settings");
      s.config.tokens[su.token].repos = [...chosen].sort();
      await api("PUT", "/api/settings", { config: s.config });
    } catch (e) { toast(e.message, true); return false; }
    await markStep("repos"); await loadState();
  } });
  const renderList = () => {
    const rows = [...listed];
    for (const r of chosen) if (!rows.some((x) => x.full_name === r)) rows.push({ full_name: r, permissions: {}, manual: true });
    box.replaceChildren(...(rows.length ? rows.map((r) => {
      const p = r.permissions || {}, perm = p.admin ? "admin" : p.push ? "read/write" : p.pull ? "read only" : r.manual ? "added by hand" : "";
      const on = chosen.has(r.full_name);
      return el("button", { class: "list-row check-pick", type: "button", role: "checkbox", "aria-checked": String(on), onclick: () => { on ? chosen.delete(r.full_name) : chosen.add(r.full_name); renderList(); } },
        el("span", { class: "checkbox" + (on ? " on" : "") }, on ? "✓" : ""),
        el("span", { class: "grow stack none" }, el("code", {}, r.full_name),
          el("span", { class: "muted" }, [r.private === undefined ? "" : r.private ? "Private" : "Public", r.language, r.pushed_at ? updatedAgo(r.pushed_at) : ""].filter(Boolean).join(" · "))),
        el("span", { class: "perm " + (p.admin ? "warn" : p.pull && !p.push ? "err" : "muted") }, perm));
    }) : [el("div", { class: "list-row muted" }, "No repositories found for this token.")]));
    // admin is about the token, so it's worth a word even on repositories you leave out
    const admin = listed.filter((r) => r.permissions?.admin).map((r) => r.full_name);
    const ro = listed.filter((r) => chosen.has(r.full_name) && r.permissions?.pull && !r.permissions?.push).map((r) => r.full_name);
    warn.replaceChildren(
      admin.length ? el("div", { class: "note-box warn" }, el("span", { class: "warn" }, "!"),
        el("span", {}, `The token has admin on ${admin.join(", ")}. Agents never need admin, so a narrower token is safer.`)) : "",
      ro.length ? el("div", { class: "note-box err" }, `The token can't push to ${ro.join(", ")}, so agents can't open pull requests there.`) : "");
    warn.hidden = !admin.length && !ro.length;
    nav.lastChild.textContent = reposNext(chosen.size);
  };
  if (su.github === "missing") {
    box.replaceChildren(el("div", { class: "list-row muted" }, "Add a GitHub token in the previous step to list repositories, or add them by hand below."));
  } else {
    api("POST", "/api/tokens/repos", { name: su.token }).then((r) => { listed = r.repos; renderList(); })
      .catch((e) => { box.replaceChildren(el("div", { class: "list-row err" }, e.message)); renderList(); });
  }
  return [...setupHead("Which repos can agents work on?", "Your token can reach these repositories. Agents only get a token for the ones you pick."),
    box, warn,
    el("div", { class: "row nowrap" }, manual, el("button", { class: "btn", onclick: () => {
      const v = manual.value.trim();
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(v)) { toast("Use owner/name", true); return; }
      chosen.add(v); manual.value = ""; renderList();
    } }, "Add")),
    nav];
}

// 4. network
function setupNetwork() {
  const current = STATE.setup.allow;
  const presetHosts = new Set(PRESETS.flatMap(([, h]) => h));
  const custom = current.filter((h) => !presetHosts.has(h));
  const on = new Set(PRESETS.filter(([, h]) => h.some((x) => current.includes(x))).map(([n]) => n));
  const chips = el("div", { class: "row" }), preview = el("pre", { class: "block cols" }), count = el("span", { class: "setup-note" }), diff = el("div", { class: "setup-note" });
  const hosts = () => [...new Set([...PRESETS.filter(([n]) => on.has(n)).flatMap(([, h]) => h), ...custom])];
  const render = () => {
    chips.replaceChildren(el("span", { class: "chip on static", title: `Always on for Claude agents: ${CLAUDE_HOSTS}` }, "✓ Claude", el("span", { class: "chip-note" }, "required")),
      ...PRESETS.map(([n]) => el("button", { class: "chip" + (on.has(n) ? " on" : ""), type: "button", onclick: () => { on.has(n) ? on.delete(n) : on.add(n); render(); } },
        (on.has(n) ? "✓ " : "+ ") + n)));
    const h = hosts();
    preview.textContent = h.join("\n") || "(nothing besides Claude)";
    count.textContent = `Allowlist preview (${h.length} ${h.length === 1 ? "host" : "hosts"}). You can edit this anytime in Settings.`;
    const add = h.filter((x) => !current.includes(x)), rem = current.filter((x) => !h.includes(x));
    diff.textContent = add.length || rem.length ? [add.length ? `Adds ${add.join(", ")}.` : "", rem.length ? `Removes ${rem.join(", ")}.` : ""].filter(Boolean).join(" ") : "No change to your current allowlist.";
  };
  render();
  return [...setupHead("What can agents reach?", "Everything is blocked except the hosts you allow. Blocked requests show up in your inbox so you can allow them later."),
    chips, el("div", { class: "stack tight" }, count, preview),
    el("div", { class: "stack tight" }, diff, custom.length ? el("div", { class: "setup-note" }, `Kept from your current list: ${custom.join(", ")}`) : "",
      el("div", { class: "setup-note" }, "Research agents get the open web instead, with no repositories or tokens.")),
    setupNav({ onNext: async () => {
      try {
        const s = await api("GET", "/api/settings");
        s.config.egress.allow = hosts();
        await api("PUT", "/api/settings", { config: s.config });
      } catch (e) { toast(e.message, true); return false; }
      await markStep("network"); await loadState();
    } })];
}

// 5. policy
// The recommended CLAUDE.md from /api/policy, with the style lines you picked under "# Style"
const claudeMdOf = (r, picked) => {
  const lines = r.claude_md_options.filter((o) => picked.includes(o.key)).map((o) => o.line);
  return lines.length ? r.claude_md_recommended.replace(/\n*$/, "\n\n# Style\n\n") + lines.join("\n") + "\n" : r.claude_md_recommended;
};
// Which style lines a CLAUDE.md is the recommended one with, or null when it's your own
function claudeMdPicked(text, r) {
  const keys = r.claude_md_options.map((o) => o.key);
  for (let m = 0; m < 1 << keys.length; m++) {
    const picked = keys.filter((_, i) => m & (1 << i));
    if (claudeMdOf(r, picked) === text) return picked;
  }
  return null;
}
// Rules added to the end of a CLAUDE.md, never in place of it: only the lines it doesn't have yet, under
// their headings, so adding the same rules twice changes nothing
function appendRules(text, rules) {
  if (!text.trim()) return rules;
  const have = new Set(text.split("\n").map((l) => l.trim()).filter(Boolean)), out = [];
  let head = null, lines = [];
  const flush = () => { if (lines.length) out.push(...(head ? [head, ""] : []), ...lines, ""); lines = []; };
  for (const l of rules.split("\n")) {
    if (l.startsWith("#")) { flush(); head = l; } else if (l.trim() && !have.has(l.trim())) lines.push(l);
  }
  flush();
  return out.length ? text.replace(/\n*$/, "\n\n") + out.join("\n").replace(/\n*$/, "\n") : text;
}
// A switch for each style line, and a fold that shows the rules: [box, () => the keys switched on]
function claudeMdChoices(r, picked) {
  const on = new Set(picked);
  const rules = el("pre", { class: "block" });
  const show = () => { rules.textContent = claudeMdOf(r, [...on]); };
  show();
  const box = el("div", { class: "stack tight" }, ...r.claude_md_options.map((o) => el("label", { class: "row nowrap" },
    sw(on.has(o.key), (v, b) => { setSw(b, v); if (v) on.add(o.key); else on.delete(o.key); show(); }),
    el("div", { class: "grow stack none" }, el("span", {}, o.label), el("span", { class: "muted pretty" }, o.note)))),
    el("details", { class: "adv" }, el("summary", {}, "See the rules"), el("div", { class: "adv-body" }, rules)));
  return [box, () => r.claude_md_options.map((o) => o.key).filter((k) => on.has(k))];
}
function setupPolicy() {
  let mode = STATE.permission_mode, cur = null, md = "recommended", picked = () => [];
  const opts = [["bypassPermissions", "Run freely inside the sandbox", "Everything is allowed except your deny rules. The VM and the egress proxy do the isolating.", "recommended"],
    ["acceptEdits", "Allow edits, ask for the rest", "File edits run. Shell commands and other tools need allow rules."],
    ["dontAsk", "Only what I allow", "Only allow rules run, and everything else is denied."]];
  const cards = el("div", { class: "stack tight" });
  const render = () => cards.replaceChildren(...opts.map(([m, title, desc, tag]) => el("div", { class: "card select-card" + (mode === m ? " on" : ""), role: "radio", "aria-checked": String(mode === m), tabindex: 0,
    onclick: () => { mode = m; render(); } }, el("div", { class: "row" }, el("span", { class: "title" }, title), tag ? el("span", { class: "tag ok plain" }, tag) : "", el("span", { class: "grow" }),
      el("span", { class: "mono muted" }, m)), el("span", { class: "desc" }, desc))));
  render();
  // CLAUDE.md: the recommended rules, your own, or none
  const own = el("textarea", { class: "editor", rows: 8, spellcheck: false, "aria-label": "Your CLAUDE.md", placeholder: "# Code style\n- Use tabs." });
  const mdCards = el("div", { class: "stack tight" }, el("div", { class: "muted" }, "Loading…"));
  let extra = el("div"), mine = false;  // mine: you have a CLAUDE.md of your own, which the rules go after
  const mdOpts = () => [["recommended", "Recommended rules", "Ask when unclear, keep changes focused, run the tests, ask before anything risky, plain commit messages."
    + (mine ? " Added after your own text." : ""), "recommended"],
    ["own", "Write my own", "Your own instructions and code style."],
    ["none", "None for now", "You can add some later on Claude policy."]];
  const renderMd = () => mdCards.replaceChildren(...mdOpts().flatMap(([k, title, desc, tag]) => [el("div", { class: "card select-card" + (md === k ? " on" : ""), role: "radio", "aria-checked": String(md === k), tabindex: 0,
    onclick: () => { md = k; renderMd(); } }, el("div", { class: "row" }, el("span", { class: "title" }, title), tag ? el("span", { class: "tag ok plain" }, tag) : ""),
    el("span", { class: "desc" }, desc)), ...(md !== k ? [] : k === "recommended" ? [extra] : k === "own" ? [own] : [])]));
  api("GET", "/api/policy").then((r) => {
    cur = r;
    const text = r.policy.claude_md || "", was = text.trim() ? claudeMdPicked(text, r) : [];
    if (was === null) { md = "own"; own.value = text; mine = true; }
    [extra, picked] = claudeMdChoices(r, was || []);
    renderMd();
  }).catch((e) => toast(e.message, true));
  return [...setupHead("How much should agents do on their own?", "This becomes the Claude Code policy, installed root-owned in every VM. Agents can't change it."),
    cards,
    el("div", { class: "section-title" }, "CLAUDE.md ", el("span", { class: "muted" }, "Instructions every agent reads")), mdCards,
    setupNav({ onNext: async () => {
      try {
        const now = cur || await api("GET", "/api/policy");
        const policy = { ...now.policy, permission_mode: mode };
        if (cur && md === "recommended") {
          policy.claude_md = mine ? appendRules(own.value, claudeMdOf(cur, picked())) : claudeMdOf(cur, picked());
          policy.dash_check = picked().includes("no_dashes");
        }
        else if (cur && md === "own") policy.claude_md = own.value;
        else if (cur) policy.claude_md = "";
        if (JSON.stringify(policy) !== JSON.stringify(now.policy)) await api("PUT", "/api/policy", { policy });
      } catch (e) { toast(e.message, true); return false; }
      await markStep("policy"); await loadState();
    } })];
}

// 6. first agent
function setupAgent() {
  const ta = el("textarea", { rows: 3, placeholder: "Read the codebase and write a short ARCHITECTURE.md. Open a PR.", "aria-label": "Task" });
  const repo = el("select", { class: "select pill-in", "aria-label": "Repository" }, STATE.repos.map((r) => el("option", { value: r }, r)), el("option", { value: "" }, "No repository"));
  const mode = el("select", { class: "select pill-in", "aria-label": "Lifecycle" }, ["ephemeral", "persistent", "memory"].map((m) => el("option", { value: m }, MODE_LABEL[m])));
  let profile = "coding";
  const create = el("button", { class: "btn primary", onclick: async () => {
    create.disabled = true;
    try {
      const a = await api("POST", "/api/agents", { profile, name: profile === "research" ? "researcher" : "first-agent", agent: "claude",
        repo: profile === "research" ? null : repo.value || null, mode: mode.value, auto_stop: mode.value === "ephemeral" && profile === "coding",
        instructions: ta.value.trim(), cpus: STATE.vm_defaults.cpus, memory_mb: STATE.vm_defaults.memory_mb, mcp: [] });
      await markStep("agent"); await finishSetup();
      go("agent", { agentId: a.id, tab: "chat" });
    } catch (e) { toast(e.message, true); create.disabled = false; }
  } }, "Create and start");
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !create.disabled) { e.preventDefault(); create.click(); } });
  const example = (title, desc, text, prof) => el("button", { class: "card example", type: "button", onclick: () => {
    ta.value = text; profile = prof || "coding"; repo.hidden = profile === "research"; ta.focus();
  } }, el("span", { class: "title" }, title), el("span", { class: "desc" }, desc));
  return [...setupHead("What should your first agent do?", "It boots in a fresh VM with the policy and allowlist you just set.", "center-text"),
    el("div", { class: "composer" }, ta, el("div", { class: "bottom" }, repo, mode, el("span", { class: "grow" }), create)),
    el("div", { class: "stack tight" }, el("span", { class: "setup-note examples-cap" }, "Or start from an example"),
      el("div", { class: "examples" },
        example("Fix failing tests", "Run the suite, fix what breaks, open a PR.", "Run the test suite, fix what fails, and open a PR with the fixes."),
        example("Bump dependencies", "Update minor versions and check the build.", "Update dependencies to their latest minor versions, make sure the build and tests pass, and open a PR."),
        example("Research agent", "Open-web VM that answers other agents' questions.", "", "research"))),
    el("button", { class: "link setup-skip", onclick: leaveSetup }, "Skip, I'll create one later")];
}

// ---- checklist on the New agent page ----
function setupChecklist() {
  const su = STATE?.setup;
  if (!su || su.checklist_hidden) return "";
  const bwrapCmd = el("div", { class: "checklist-cmd", hidden: true }, hostCmd("bwrap") ? copyBlock(hostCmd("bwrap"))
    : el("span", { class: "muted" }, "Install bubblewrap from your distribution's packages."));
  // a blocked bubblewrap stops VMs, so the host check above has it; with vm.use_bwrap off it isn't wanted
  const bwrapDone = !!STATE.tools.bwrap || !!STATE.tools.bwrap_problem || STATE.tools.use_bwrap === "off";
  const items = [
    ["Host check", !hostProblems().length, "Open setup", () => go("setup", { setupStep: 0 })],
    ["Claude login", su.claude !== "missing", su.steps.includes("secrets") ? "Paste it again" : "Add it", () => go("setup", { setupStep: 1 })],
    ["GitHub token and repositories", su.github !== "missing" && su.repos > 0, su.github === "missing" ? "Add token" : "Pick repositories",
      () => go("setup", { setupStep: su.github === "missing" ? 1 : 2 })],
    ["Network allowlist reviewed", su.steps.includes("network"), "Review", () => go("setup", { setupStep: 3 })],
    ["Policy chosen", su.steps.includes("policy"), "Review", () => go("setup", { setupStep: 4 })],
    ["Install bubblewrap (optional)", bwrapDone, "Show command", (e) => {
      bwrapCmd.hidden = !bwrapCmd.hidden; e.target.textContent = bwrapCmd.hidden ? "Show command" : "Hide command";
    }],
  ];
  const done = items.filter((i) => i[1]).length;
  if (done === items.length) return "";
  const fill = el("span", {});
  fill.style.width = Math.round((done / items.length) * 100) + "%";
  const bar = el("span", { class: "progress" }, fill);
  return el("div", { class: "card flush checklist" },
    el("div", { class: "list-row" }, el("span", { class: "title grow" }, "Finish setting up"), el("span", { class: "muted" }, `${done} of ${items.length} done`), bar,
      el("button", { class: "link", title: "Hide this checklist", onclick: async () => { await api("POST", "/api/setup", { checklist_hidden: true }); await loadState(); VIEWS.new.update(true); } }, "Hide")),
    ...items.map(([label, ok, action, onclick]) => el("div", { class: "list-row" }, el("span", { class: ok ? "ok" : "dim" }, ok ? "✓" : "○"),
      el("span", { class: "grow " + (ok ? "muted" : "") }, label),
      ok ? "" : el("button", { class: "link", onclick }, action))),
    bwrapCmd);
}
