// cilly UI: questions an agent asks you (its ~/ask/<name>.json, checked by the panel), answered in a
// picker docked above the message box: one tab per question, then Submit. The answers go back as one
// message in the same conversation. On phones the picker is a bottom sheet.
"use strict";

const PICKS = {};  // agent id -> what you've picked so far in its open questions (kept across redraws)

function openAsk(a) { return [...(a.asks || [])].reverse().find((r) => r.state === "open") || null; }
function pickState(a, rec) {
  let st = PICKS[a.id];
  if (!st || st.id !== rec.id) {
    st = PICKS[a.id] = { id: rec.id, tab: 0, focus: rec.questions.map(() => 0), picked: rec.questions.map(() => []),
      other: rec.questions.map(() => ""), typing: null, sending: false, node: null };
  }
  return st;
}
const answered = (st, i) => st.picked[i].length > 0 || !!st.other[i].trim();
function answerText(q, st, i) {
  const own = st.other[i].trim();
  return [...st.picked[i].map((p) => q.options[p].label), ...(own ? [`“${own}”`] : [])].join(", ");
}

// The picker for an agent's open questions, the same node across redraws so you never lose your place
function askPicker(a, rec) {
  const st = pickState(a, rec);
  if (!st.node) {
    st.node = el("div", { class: "ask-picker", tabindex: 0, role: "group", "aria-label": `Questions from ${a.name}` });
    st.node.addEventListener("keydown", (e) => askKey(a.id, e));
  }
  drawPicker(a, rec, st);
  return st.node;
}
function redrawPicker(aid) {
  const a = agentById(aid), rec = a && openAsk(a), st = PICKS[aid];
  if (rec && st?.node) drawPicker(a, rec, st);
}
function drawPicker(a, rec, st) {
  const n = rec.questions.length, done = rec.questions.every((_, i) => answered(st, i)), phone = isPhone();
  const tab = (label, i, on, check) => el("button", { class: "ask-tab" + (on ? " on" : ""), type: "button", tabindex: -1,
    onclick: () => { st.tab = i; st.typing = null; redrawPicker(a.id); st.node.focus(); } },
    check ? el("span", { class: "ok" }, "✓ ") : "", label);
  const tabs = el("div", { class: "ask-tabs" }, rec.questions.map((q, i) => tab(q.header, i, st.tab === i, answered(st, i))),
    tab("Submit", n, st.tab === n, false));
  const head = el("div", { class: "ask-head" }, el("span", { class: "grow ellipsis" }, `${a.name} is asking`),
    el("button", { class: "link", type: "button", onclick: () => chatInstead(a, rec) }, "Answer in chat instead"));
  if (st.tab >= n) {  // Submit: what you picked, then send it
    const send = el("button", { class: "btn primary", type: "button", disabled: !done || st.sending, onclick: () => submitAsk(a, rec) },
      st.sending ? "Sending…" : "Submit answers");
    st.node.replaceChildren(head, tabs, el("div", { class: "ask-q" }, "Your answers"),
      el("div", { class: "ask-review" }, rec.questions.map((q, i) => el("div", { class: "row nowrap" },
        el("span", { class: "muted ask-h" }, q.header), answered(st, i) ? el("span", { class: "grow" }, answerText(q, st, i))
          : el("button", { class: "link warn", type: "button", onclick: () => { st.tab = i; redrawPicker(a.id); } }, "Not answered yet")))),
      el("div", { class: "row nowrap ask-foot" }, el("span", { class: "grow" }), send));
    return;
  }
  const q = rec.questions[st.tab], rows = q.options.length + 1;
  const opt = (i, label, desc, on) => el("button", { class: "ask-opt" + (st.focus[st.tab] === i ? " focus" : "") + (on ? " on" : ""),
    type: "button", tabindex: -1, role: q.multi ? "checkbox" : "radio", "aria-checked": String(on), onclick: () => pick(a.id, i) },
    el("span", { class: "ask-num" }, q.multi ? (on ? "☑" : "☐") : `${i + 1}.`),
    el("span", { class: "grow" }, el("span", { class: "ask-label" }, label), desc ? el("span", { class: "ask-desc" }, desc) : ""));
  const own = st.other[st.tab].trim();
  const list = el("div", { class: "ask-opts" }, q.options.map((o, i) => opt(i, o.label, o.description, st.picked[st.tab].includes(i))),
    opt(rows - 1, "Type something", own && st.typing !== st.tab ? `“${own}”` : "", !!own));
  if (st.typing === st.tab) {
    const field = el("input", { class: "input ask-own", value: st.other[st.tab], placeholder: "Your answer, then Enter", maxlength: 2000,
      "aria-label": "Your own answer" });
    field.addEventListener("input", () => { st.other[st.tab] = field.value; });
    field.addEventListener("keydown", (e) => {
      e.stopPropagation();  // arrows and number keys type here
      if (e.key === "Enter") { e.preventDefault(); ownDone(a.id); }
      else if (e.key === "Escape") { e.preventDefault(); st.typing = null; redrawPicker(a.id); st.node.focus(); }
    });
    list.append(field);
    setTimeout(() => field.focus());
  }
  const next = q.multi ? el("button", { class: "btn small", type: "button", onclick: () => { st.tab++; st.typing = null; redrawPicker(a.id); st.node.focus(); } }, "Next") : "";
  const keys = phone ? "" : el("span", { class: "muted small" }, q.multi ? "Space or Enter ticks, ← → move" : "↑ ↓ and Enter, or a number");
  st.node.replaceChildren(head, tabs, el("div", { class: "ask-q" }, q.question), list,
    keys || next ? el("div", { class: "row nowrap ask-foot" }, el("span", { class: "grow" }), keys, next) : "");
}

// Picking: one answer moves on to the next question; with several, you tick and go on yourself
function pick(aid, i) {
  const a = agentById(aid), rec = a && openAsk(a), st = PICKS[aid];
  if (!rec || !st) return;
  const q = rec.questions[st.tab];
  st.focus[st.tab] = i;
  if (i === q.options.length) { st.typing = st.tab; redrawPicker(aid); return; }  // Type something
  if (q.multi) {
    const p = st.picked[st.tab];
    st.picked[st.tab] = p.includes(i) ? p.filter((x) => x !== i) : [...p, i].sort();
  } else {
    st.picked[st.tab] = [i];
    st.other[st.tab] = "";
    st.tab++;
  }
  st.typing = null;
  redrawPicker(aid);
  st.node.focus();
}
function ownDone(aid) {
  const a = agentById(aid), rec = a && openAsk(a), st = PICKS[aid];
  if (!rec || !st) return;
  const q = rec.questions[st.tab];
  if (!q.multi && st.other[st.tab].trim()) { st.picked[st.tab] = []; st.tab++; }  // your own words are the answer
  st.typing = null;
  redrawPicker(aid);
  st.node.focus();
}
function askKey(aid, e) {
  const a = agentById(aid), rec = a && openAsk(a), st = PICKS[aid];
  if (!rec || !st || e.target !== st.node) return;
  const n = rec.questions.length, q = rec.questions[st.tab], rows = q ? q.options.length + 1 : 0;
  const move = () => { e.preventDefault(); redrawPicker(aid); };
  if (e.key === "ArrowLeft" && st.tab > 0) { st.tab--; move(); }
  else if (e.key === "ArrowRight" && st.tab < n) { st.tab++; move(); }
  else if (!q) { if (e.key === "Enter" && rec.questions.every((_, i) => answered(st, i))) { e.preventDefault(); submitAsk(a, rec); } }
  else if (e.key === "ArrowDown") { st.focus[st.tab] = (st.focus[st.tab] + 1) % rows; move(); }
  else if (e.key === "ArrowUp") { st.focus[st.tab] = (st.focus[st.tab] + rows - 1) % rows; move(); }
  else if (e.key === "Enter" || (e.key === " " && q.multi)) { e.preventDefault(); pick(aid, st.focus[st.tab]); }
  else if (/^[1-9]$/.test(e.key) && +e.key <= rows) { e.preventDefault(); pick(aid, +e.key - 1); }
}

async function submitAsk(a, rec) {
  const st = PICKS[a.id];
  if (!st || st.sending) return;
  st.sending = true;
  redrawPicker(a.id);
  try {
    if (isStopped(a)) {
      toast(`Starting ${a.name}…`);
      await api("POST", `/api/agents/${a.id}/start`, { inject: null });  // the answers wait until the VM is ready
    }
    await api("POST", `/api/agents/${a.id}/asks/${rec.id}/answer`,
      { answers: rec.questions.map((_, i) => ({ picked: st.picked[i], other: st.other[i].trim() })) });
    delete PICKS[a.id];
    closePop();
    toast("Answers sent");
  } catch (e) {
    st.sending = false;
    redrawPicker(a.id);
    toast(e.message, true);
  }
  SIGS.asks = null;
  await Promise.all([loadState(), pollInbox()]);
  pollAgent(true);
}
async function chatInstead(a, rec) {
  try { await api("POST", `/api/agents/${a.id}/asks/${rec.id}/dismiss`, {}); } catch (e) { toast(e.message, true); return; }
  delete PICKS[a.id];
  closePop();
  SIGS.asks = null;
  await Promise.all([loadState(), pollInbox()]);
  AV?.c?.ta.focus();
}
// From the inbox: the agent's chat with the picker up (a sheet on phones)
function answerAsk(it) {
  closePop();
  go("agent", { agentId: it.agent, tab: "chat" });
  const a = agentById(it.agent), rec = a && openAsk(a);
  if (!rec) return;
  if (isPhone()) openAskSheet(a, rec);
  else setTimeout(() => PICKS[a.id]?.node?.focus());
}
function openAskSheet(a, rec) {
  openPop(AV?.c?.asks || document.body, askPicker(a, rec), { toggle: false });  // opened for you, not by a click
}

// Opening an agent with open questions puts the keyboard in the picker, so arrows and numbers work at once.
// Not when you've started a message, and only once per set, so it never pulls the cursor away while you type.
function focusAsk(a, rec) {
  const st = PICKS[a.id];
  if (!st?.node || isPhone() || !AV || AV.askFocused === rec.id || composerState(a.id).draft.trim()) return;
  AV.askFocused = rec.id;
  st.node.focus();
}

// The docked strip's line for it: the whole picker on a PC, one line with Answer on a phone
function askStrip(a, rec) {
  if (!isPhone()) return askPicker(a, rec);
  const n = rec.questions.length;
  return el("div", { class: "ask asking" },
    el("span", { class: "grow ellipsis" }, `${a.name} is asking ${n === 1 ? `“${rec.questions[0].header}”` : `${n} questions`}`),
    el("button", { class: "btn small primary", onclick: () => openAskSheet(a, rec) }, "Answer"));
}

// In the chat, after the task that asked: what was asked, and what you picked
function askCard(r) {
  const head = (text, cls) => el("div", { class: "top" }, el("span", { class: "accent-fg" }, "Questions"),
    el("span", { class: "muted ellipsis" }, `~/ask/${r.name}`), el("span", { class: "grow" }), el("span", { class: "tag " + (cls || "") }, text));
  if (r.state === "refused") return el("div", { class: "req-card ask-card" }, head("refused", "err"), el("div", { class: "err-text small" }, `Not shown: ${r.error}.`));
  const word = { open: ["waiting for you", "warn"], answering: ["sending", ""], answered: ["answered", "ok"], chat: ["answered in the chat", ""], replaced: ["replaced by newer ones", ""] }[r.state] || [r.state, ""];
  return el("div", { class: "req-card ask-card" }, head(...word),
    el("div", { class: "ask-review" }, r.questions.map((q, i) => el("div", { class: "row nowrap" },
      el("span", { class: "muted ask-h" }, q.header),
      el("span", { class: "grow" }, r.answers ? [...r.answers[i].picked, ...(r.answers[i].other ? [`“${r.answers[i].other}”`] : [])].join(", ") : q.question)))));
}
