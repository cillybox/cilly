// cilly UI: core (auth, API, DOM helpers, modals, sidebar, navigation).
// Plain DOM APIs only. All server and guest data is inserted as text (never
// innerHTML), links to sites other than https://github.com/ ask first, and the CSP forbids
// inline styles, so styling is by class (or the CSSOM for computed widths).
"use strict";

// Keys from before the rename to cilly move over once, so a paired phone stays paired.
try {
  for (const [store, key] of [[localStorage, "device-token"], [localStorage, "sidebar"], [localStorage, "notify"], [sessionStorage, "token"], [sessionStorage, "view"]]) {
    const old = store.getItem("vmpanel-" + key);
    if (old === null) continue;
    if (store.getItem("cilly-" + key) === null) store.setItem("cilly-" + key, old);
    store.removeItem("vmpanel-" + key);
  }
} catch { /* no storage here */ }

// ---- auth token: from the URL fragment once, then sessionStorage (this tab only), or
// localStorage for a paired device (your phone) or when you chose "Remember on this device" ----
const DEVICE_KEY = "cilly-device-token";
let OPEN_AGENT = null;  // #a=<id>: opened from a notification
(function grabToken() {
  const m = location.hash.match(/[#&]t=([A-Za-z0-9_-]+)/), a = location.hash.match(/[#&]a=([0-9a-f]{8})/);
  if (m) sessionStorage.setItem("cilly-token", m[1]);
  if (a) OPEN_AGENT = a[1];
  if (m || a) history.replaceState(null, "", location.pathname);
})();
function deviceToken() { try { return localStorage.getItem(DEVICE_KEY) || ""; } catch { return ""; } }
const TOKEN = sessionStorage.getItem("cilly-token") || deviceToken();

async function api(method, path, body, retried) {
  const opts = { method, headers: { "X-Panel-Token": TOKEN } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) tokenRejected();
  let data;
  try { data = await r.json(); } catch { data = { error: "bad response" }; }
  if (!r.ok && data.needs_secret && !retried && isOwner()) {
    if (await askSecret(data.needs_secret, data.error)) return api(method, path, body, true);
  }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

// ---- tiny DOM helpers ----
const $ = (id) => document.getElementById(id);
const PROPS = new Set(["value", "checked", "disabled", "readOnly", "selected", "hidden", "open", "spellcheck"]);
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (PROPS.has(k)) e[k] = v;
    else e.setAttribute(k, v);  // never "style": the CSP blocks it; use classes
  }
  append(e, kids);
  return e;
}
// An (i) that shows a longer explanation above it on hover or focus (a tap on a phone) and hides it again when you leave.
let tipSeq = 0;
function infoTip(...text) {
  const id = "tip-" + ++tipSeq;
  const tip = el("span", { class: "tip", role: "tooltip", id, "aria-hidden": "true" },  // hidden, so a label around it isn't named by it
     el("span", { class: "tip-box pretty" }, ...text));
  const btn = el("button", { class: "info-btn", type: "button", "aria-label": "More information", "aria-describedby": id }, el("span", { class: "info-i", "aria-hidden": "true" }, "i"));
  const wrap = el("span", { class: "info-tip" }, btn, tip);
  const place = () => {  // keep the box on screen: slide it sideways, or open it below when there is no room above
    wrap.classList.remove("below");
    tip.style.translate = "0px";
    const m = 8, r = tip.getBoundingClientRect(), w = document.documentElement.clientWidth;
    const dx = r.right > w - m ? w - m - r.right : r.left < m ? m - r.left : 0;
    tip.style.translate = dx + "px";
    if (r.top < m) wrap.classList.add("below");
  };
  wrap.addEventListener("pointerenter", place);
  wrap.addEventListener("focusin", place);
  return wrap;
}
// Long lists show one page at a time: the first `step` items, then a "Show more" button that
// adds the next page in place. How far you expanded a list is remembered until the page reloads.
const SHOWN = {};
function paged(key, items, render, { step = 20, wrap } = {}) {
  const batch = (from, to) => {
    const nodes = items.slice(from, to).map(render);
    const left = items.length - to;
    if (left > 0) {
      let holder = null;
      const btn = el("button", { class: "show-more", type: "button", onclick: () => { SHOWN[key] = to + step; holder.replaceWith(...batch(to, to + step)); } },
        `Show ${Math.min(step, left)} more`, el("span", { class: "muted" }, ` (${left} not shown)`));
      holder = wrap ? wrap(btn) : btn;
      nodes.push(holder);
    }
    return nodes;
  };
  return batch(0, Math.max(step, SHOWN[key] || 0));
}
function append(e, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false || k === "") continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}
function ghLink(url, text) {
  if (typeof url !== "string" || !url.startsWith("https://github.com/")) return document.createTextNode(text || "");
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text || url);
}
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
function fmtBytes(n) {
  const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0;
  n = Number(n) || 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
function fmtTime(iso) {  // "2026-10-14T05:12:03Z" -> "05:12" today, "Oct 13 05:12" otherwise (UTC)
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const hm = d.toISOString().slice(11, 16);
  return d.toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10) ? hm
    : d.toLocaleDateString("en", { month: "short", day: "numeric", timeZone: "UTC" }) + " " + hm;
}
function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (!(s >= 0)) return fmtTime(iso);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return fmtTime(iso);
}
const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;
function clean(text) { return String(text ?? "").replace(ANSI, ""); }
// Log text between color codes: every other escape or control character (but tab) becomes a visible
// mark, as the panel stores logs (cilly/vm.py, inert_log). A C1 control is ESC and a character.
function marked(text) {
  return String(text ?? "").replace(/[\x00-\x08\x0a-\x1f\x7f-\x9f]/g, (c) => {
    const n = c.charCodeAt(0);
    if (n < 0x20) return String.fromCharCode(0x2400 + n);  // the Control Pictures, such as U+241B for ESC
    return n === 0x7f ? String.fromCharCode(0x2421) : String.fromCharCode(0x241b, n - 0x40);
  });
}
// VM images and the toolchains they were built with
function toolchainText(img) {
  return img?.toolchains?.length ? img.toolchains.map((t) => STATE.recipes?.[t]?.label || t).join(", ") : "basics only";
}
function imageLabel(i) { return i.label || STATE.presets?.[i.preset]?.label || (i.toolchains.length ? "Custom" : "Basics only"); }
// An image the way you know it: the name you gave it and the day it was built, like "AIO · Oct 17".
// One that's gone keeps its file name.
function imageNamed(name) {
  const i = (STATE.images || []).find((x) => x.name === name);
  if (!i) return String(name || "?").replace(/\.qcow2$/, "");
  const day = i.built ? new Date(i.built).toLocaleDateString("en", { month: "short", day: "numeric" }) : "";
  return [imageLabel(i), day].filter(Boolean).join(" · ");
}
function imageSelect(value, onPick, profile) {  // "" = the default image for that kind of agent
  const def = (STATE.images || []).find((i) => i.name === (profile === "research" ? STATE.research_base_image : STATE.base_image));
  const s = el("select", { class: "input", "aria-label": "VM image" },
    el("option", { value: "" }, def ? `Default (${imageNamed(def.name)})` : "Default"),
    ...(STATE.images || []).map((i) => el("option", { value: i.name }, imageNamed(i.name) + (i.outdated ? " (rebuild needed)" : ""))));
  s.value = value || "";
  s.addEventListener("change", () => onPick(s.value));
  return s;
}
const isPhone = () => window.matchMedia("(max-width: 640px)").matches;
function sw(on, onToggle, cls) {  // switch button
  const b = el("button", { class: "switch " + (cls || "") + (on ? " on" : ""), type: "button", role: "switch", "aria-checked": String(!!on) });
  b.addEventListener("click", () => onToggle(!b.classList.contains("on"), b));
  return b;
}
function setSw(b, on) { b.classList.toggle("on", !!on); b.setAttribute("aria-checked", String(!!on)); }
function seg(options, value, onPick, cls) {  // segmented control: options = [[value, label], ...]
  const box = el("div", { class: "seg " + (cls || "") });
  for (const [v, label] of options) {
    box.append(el("button", { type: "button", class: v === value ? "on" : "", onclick: () => {
      box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); onPick(v);
    } }, label));
    const b = box.lastChild;
  }
  return box;
}
let SIGS = {};
function changed(key, value) {  // re-render guard so live updates don't clobber what you're doing
  const sig = JSON.stringify(value);
  if (SIGS[key] === sig) return false;
  SIGS[key] = sig;
  return true;
}

// Whether you're typing in a field inside node, so a redraw waits until you leave it
function typingIn(node) {
  const f = document.activeElement;
  return !!f && !!node?.contains(f) && (f.tagName === "TEXTAREA" || (f.tagName === "INPUT" && !["checkbox", "radio", "button"].includes(f.type)));
}

// ---- toast / modals / popovers ----
// Toasts stack in the corner (the top on a phone). A click or tap closes one, a swipe too. They wait while the pointer is over them.
const TOAST_MS = { ok: 4000, err: 8000 }, TOAST_MAX = 3;
function toastParts(msg) {  // "Saved. Agents get it at their next task." is a title and its detail
  const m = /^(.{3,60}?[.!?])\s+(.+)$/s.exec(String(msg));
  return m ? [m[1].replace(/\.$/, ""), m[2]] : [String(msg), ""];
}
function toastIcon(isErr) {
  const s = svg("svg", { width: 20, height: 20, viewBox: "0 0 20 20", fill: "none", stroke: "currentColor", "stroke-width": 1.7, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "toast-icon" });
  s.append(svg("circle", { cx: 10, cy: 10, r: 8 }), ...(isErr ? [svg("path", { d: "M10 5.8v5" }), svg("path", { d: "M10 13.8v.2" })] : [svg("path", { d: "m6.6 10.2 2.4 2.4 4.4-4.8" })]));
  return s;
}
function toast(msg, isErr) {
  let box = document.getElementById("toasts");
  if (!box) { box = el("div", { class: "toasts", id: "toasts" }); document.body.append(box); }
  const [title, detail] = toastParts(msg), kind = isErr ? "err" : "ok";
  const same = [...box.children].find((n) => n.dataset.msg === String(msg) && n.dataset.kind === kind);
  if (same) { same.restart(); return; }  // the same thing again: keep the one that is there
  let timer = null, from = null;
  const t = el("div", { class: "toast " + kind, role: isErr ? "alert" : "status", "data-msg": String(msg), "data-kind": kind, title: "Click to dismiss" },
    toastIcon(isErr), el("div", { class: "toast-text" }, el("div", { class: "toast-title" }, title), detail ? el("div", { class: "toast-detail" }, detail) : ""),
    el("button", { class: "toast-x", type: "button", "aria-label": "Dismiss" }, "✕"));
  const gone = () => { clearTimeout(timer); t.remove(); };
  const wait = (ms) => { clearTimeout(timer); timer = setTimeout(gone, ms); timer.unref?.(); };  // unref: a test run isn't held up by it
  t.restart = () => wait(TOAST_MS[kind]);
  t.addEventListener("click", gone);
  t.addEventListener("pointerenter", () => clearTimeout(timer));
  t.addEventListener("pointerleave", () => { if (!t.matches(":active")) wait(2000); });
  t.addEventListener("pointerdown", (e) => { from = { x: e.clientX, y: e.clientY }; });
  t.addEventListener("pointerup", (e) => { if (from && Math.hypot(e.clientX - from.x, e.clientY - from.y) > 40) gone(); from = null; });
  box.append(t);
  while (box.children.length > TOAST_MAX) box.firstElementChild.remove();
  t.restart();
}
function confirmModal(title, body, action, danger) {
  return new Promise((resolve) => {
    const done = (v) => { back.remove(); document.removeEventListener("keydown", key, true); resolve(v); };
    const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    const ok = el("button", { class: "btn " + (danger ? "danger" : "primary"), onclick: () => done(true) }, action || "OK");
    const cancel = el("button", { class: "btn ghost", onclick: () => done(false) }, "Cancel");
    const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) done(false); } },
      el("div", { class: "modal", role: "dialog", "aria-modal": "true" },
        el("h3", {}, title), body ? el("div", { class: "body" }, body) : "",
        el("div", { class: "actions" }, cancel, ok)));
    document.addEventListener("keydown", key, true);
    document.body.append(back);
    syncBack();
    (danger ? cancel : ok).focus();  // Enter must not confirm a delete
  });
}
// A question with a few answers: [[key, label, primary?], ...] -> the key, or null if cancelled
function choiceModal(title, body, choices) {
  return new Promise((resolve) => {
    const done = (v) => { back.remove(); document.removeEventListener("keydown", key, true); resolve(v); };
    const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(null); } };
    const btns = choices.map(([k, label, primary]) => el("button", { class: "btn " + (primary ? "primary" : "ghost"), onclick: () => done(k) }, label));
    const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) done(null); } },
      el("div", { class: "modal", role: "dialog", "aria-modal": "true" }, el("h3", {}, title), body ? el("div", { class: "body" }, body) : "",
        el("div", { class: "actions wrap" }, el("button", { class: "btn ghost", onclick: () => done(null) }, "Cancel"), ...btns)));
    document.addEventListener("keydown", key, true);
    document.body.append(back);
    syncBack();
    (btns.find((b) => b.classList.contains("primary")) || btns[0]).focus();
  });
}
function promptModal(title, hint, value, action) {  // -> the text, or null if cancelled
  return new Promise((resolve) => {
    const inp = el("input", { class: "input", value: value || "", spellcheck: false, "aria-label": title });
    const done = (v) => { back.remove(); document.removeEventListener("keydown", esc, true); resolve(v); };
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(null); } };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") done(inp.value); });
    const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) done(null); } },
      el("div", { class: "modal", role: "dialog", "aria-modal": "true" }, el("h3", {}, title), inp,
        hint ? el("div", { class: "muted" }, hint) : "",
        el("div", { class: "actions" }, el("button", { class: "btn ghost", onclick: () => done(null) }, "Cancel"),
          el("button", { class: "btn primary", onclick: () => done(inp.value) }, action || "OK"))));
    document.addEventListener("keydown", esc, true);
    document.body.append(back);
    syncBack();
    inp.focus(); inp.select();
  });
}
// Rename in place: `shown` makes way for a field. Enter or clicking away saves, Esc keeps the old name.
// `wrap` puts the field where `shown` was (a row keeps its icon), and redraws wait while it has focus.
function editName(shown, current, save, { max = 48, cls = "", wrap = (f) => f } = {}) {
  const field = el("input", { class: "input rename " + cls, value: current, maxlength: max, spellcheck: false, "aria-label": "Name" });
  const holder = wrap(field);
  let done = false;
  const finish = (keep) => {
    if (done) return;
    done = true;
    const name = field.value.trim();
    holder.replaceWith(shown);
    if (keep && name && name !== current) save(name);
  };
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
  });
  field.addEventListener("blur", () => finish(true));
  field.addEventListener("click", (e) => e.stopPropagation());
  shown.replaceWith(holder);
  field.focus(); field.select();
  return field;
}
const editingIn = (box) => !!box && box.contains(document.activeElement) && !!document.activeElement.classList?.contains("rename");
// The Clipboard API needs HTTPS or localhost. On plain HTTP (a phone on your network) the old way still works.
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* not available here */ }
  const ta = el("textarea", { class: "offscreen", readOnly: true, value: text, "aria-hidden": "true" });
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* nothing more to try */ }
  ta.remove();
  return ok;
}
// Paste a secret; with `remember` it's also saved on this PC (systemd-creds) and never asked for again.
async function pasteSecret(key, value, remember) {
  const r = await api("POST", "/api/secrets", { key, value, remember: !!remember && !!STATE?.can_save_secrets }, true);
  if (r.note) toast(r.note, true);
  return r;
}
function saveSwitch(onChange) {  // "Save on this PC", on by default where it's possible
  if (!STATE?.can_save_secrets) return "";
  return el("label", { class: "row nowrap" }, sw(true, (on, b) => { setSw(b, on); onChange(on); }),
    el("span", { class: "muted pretty" }, "Save on this PC, encrypted for this machine, so you're not asked again"));
}
function askSecret(key, why) {
  return new Promise((resolve) => {
    const inp = el("input", { class: "input mono", type: "password", autocomplete: "off", spellcheck: false, placeholder: "paste…" });
    let remember = true;
    const done = async (ok) => {
      if (ok && inp.value.trim()) {
        try { await pasteSecret(key, inp.value, remember); }
        catch (e) { toast(e.message, true); ok = false; }
      } else ok = false;
      inp.value = "";
      back.remove(); document.removeEventListener("keydown", esc, true);
      resolve(ok);
    };
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") done(true); });
    const back = el("div", { class: "modal-back", onclick: (e) => { if (e.target === back) done(false); } },
      el("div", { class: "modal wide", role: "dialog", "aria-modal": "true" },
        el("h3", {}, "Secret needed"),
        el("div", { class: "body" }, `${why} (`, el("code", {}, key), ")"),
        el("div", { class: "muted" }, "Sent to the VM with each task, in memory only."),
        inp, saveSwitch((on) => { remember = on; }),
        el("div", { class: "actions" }, el("button", { class: "btn ghost", onclick: () => done(false) }, "Cancel"),
          el("button", { class: "btn primary", onclick: () => done(true) }, "Use it"))));
    document.addEventListener("keydown", esc, true);
    document.body.append(back);
    syncBack();
    inp.focus();
  });
}
// One popover at a time; closes on an outside click or Esc.
let POP = null;
function closePop() { if (POP) { POP.node.remove(); POP.back?.remove(); POP.onClose?.(); POP = null; } }
// Returns false when it closed instead: a second click on what opened a popover closes it.
function openPop(anchor, node, { align = "right", above = false, width, onClose, toggle = true } = {}) {
  if (toggle && POP && POP.anchor === anchor) { closePop(); return false; }
  closePop();
  node.classList.add("pop");
  if (isPhone()) {  // a bottom sheet over a backdrop; tapping the backdrop or pulling the sheet down closes it
    node.classList.add("sheet");
    const back = el("div", { class: "backdrop sheet-back", onclick: closePop });
    document.body.append(back, node);
    POP = { node, anchor, onClose, back };
    sheetDrag(node, back);
    syncBack();
    return true;
  }
  if (width) node.style.width = width + "px";
  document.body.append(node);
  const r = anchor.getBoundingClientRect(), w = node.offsetWidth, h = node.offsetHeight;
  let left = align === "right" ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  let top = above ? r.top - h - 8 : r.bottom + 8;
  if (top + h > window.innerHeight - 8) top = Math.max(8, window.innerHeight - h - 8);
  node.style.left = left + "px"; node.style.top = Math.max(8, top) + "px";
  POP = { node, anchor, onClose };
  return true;
}
// A sheet follows your finger down and closes when you let go far enough or with a flick, like a phone's own sheets.
// It only moves when what's under the finger is scrolled to the top, so scrolling inside the sheet still works.
const SHEET_SLOP = 8;  // px before a touch counts as a drag or a scroll
const sheetGesture = (dx, dy, atTop) => (atTop && dy > 0 && dy > Math.abs(dx) ? "drag" : "scroll");
const sheetCloses = (dy, height, speed) => dy > height * 0.3 || (dy > 24 && speed > 0.5);  // speed in px per ms
function sheetDrag(node, back) {
  let t = null;
  const atTop = (n) => { for (; n && n !== node.parentNode; n = n.parentNode) if (n.scrollTop > 0) return false; return true; };
  const move = (dy) => {
    node.style.transform = dy ? `translateY(${dy}px)` : "";
    back.style.opacity = dy ? String(Math.max(0, 1 - dy / (node.offsetHeight || 1))) : "";
  };
  node.addEventListener("touchstart", (e) => {
    const p = e.touches[0];
    t = e.touches.length === 1 && !e.target.closest?.("input, textarea, select, [contenteditable]")
      ? { x: p.clientX, y: p.clientY, top: atTop(e.target), on: false, dy: 0, last: p.clientY, at: Date.now(), speed: 0 } : null;
  }, { passive: true });
  node.addEventListener("touchmove", (e) => {
    if (!t) return;
    const p = e.touches[0], dx = p.clientX - t.x, dy = p.clientY - t.y, now = Date.now();
    if (!t.on) {
      if (Math.abs(dx) < SHEET_SLOP && Math.abs(dy) < SHEET_SLOP) {  // claim a pull down early, before the browser starts its own scroll
        if (sheetGesture(dx, dy, t.top) === "drag" && e.cancelable) e.preventDefault();
        return;
      }
      if (sheetGesture(dx, dy, t.top) === "scroll") { t = null; return; }
      t.on = true;
      node.classList.add("dragging");
    }
    if (e.cancelable) e.preventDefault();  // the page doesn't scroll under the finger
    t.speed = (p.clientY - t.last) / Math.max(1, now - t.at);
    t.last = p.clientY; t.at = now; t.dy = Math.max(0, dy);
    move(t.dy);
  }, { passive: false });
  const end = () => {
    const d = t;
    t = null;
    if (!d?.on) return;
    node.classList.remove("dragging");
    if (!sheetCloses(d.dy, node.offsetHeight || 1, d.speed)) { move(0); return; }
    const mine = POP;
    node.style.transform = "translateY(100%)"; back.style.opacity = "0";
    setTimeout(() => { if (POP === mine) closePop(); }, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180);
  };
  node.addEventListener("touchend", end);
  node.addEventListener("touchcancel", end);
}
document.addEventListener("mousedown", (e) => {
  if (POP && !POP.node.contains(e.target) && !POP.anchor.contains(e.target) && !e.target.closest(".modal-back")) closePop();
});

// ---- state ----
let STATE = null;
const UI = { view: "home", agentId: null, tab: "chat", sbHidden: false, drawer: false, search: "", closedGroups: new Set(), showArchived: false };
try {
  const sp = JSON.parse(localStorage.getItem("cilly-sidebar") || "null");
  if (sp) { UI.closedGroups = new Set(sp.closed || []); UI.showArchived = !!sp.archived; }
} catch { /* defaults */ }
const MODE_LABEL = { ephemeral: "Ephemeral", persistent: "Persistent", memory: "Ephemeral + memory" };
const MODE_HELP = {
  ephemeral: "A fresh VM at every start, deleted when it stops.",
  persistent: "The disk, checkout and conversation history survive restarts. Reset to clean deletes them.",
  memory: "A fresh VM at every start, but conversations and memory notes carry over.",
};
// A paired device (your phone) gets a smaller role: no host folders, Settings, policy or secrets.
const isOwner = () => STATE?.role !== "device";
// What DEVICE_ROUTES and device_body_ok in server.py let a paired device do
const PAIRED_CAN = { can: ["Create agents, chat with them and send them files", "Start and stop them, and turn MCP servers, plugins and skills on and off for them", "Add a plugin from GitHub with /plugin add",
  "Change an agent's model, route, VM image and when it stops", "Change its Bash command and task time limits", "Rename, pin or archive it, move it into a project",
  "Link it to a research agent, with YOLO on or off, and attach a repository", "Allow a site for one agent on the ports every site gets",
  "Leave sites of the global allowlist out for one agent",
  "Open the files in an agent's VM", "Answer agents in the inbox", "Use voice input", "See pull requests and image builds",
  "See your themes and fonts"],
cant: ["Use folders on your PC", "Add MCP servers or skills, or plugins from other sites", "Allow a site for all agents", "Delete or reset an agent", "Add project notes",
  "Approve another device", "Open Settings, Claude policy or secrets"] };
const PAIRED_SHORT = "Approve it once on your PC and it stays signed in. It can use your agents and the inbox, but not your PC's folders, settings or secrets.";
// The two lists, collapsed under the short sentence wherever pairing is explained
function pairedLists() {
  const list = (title, items) => el("div", { class: "stack tight" }, el("span", { class: "title" }, title),
    el("ul", { class: "paired-list" }, items.map((t) => el("li", {}, t))));
  return el("details", { class: "adv" }, el("summary", {}, "What a paired device can do"),
    el("div", { class: "paired-cols adv-body" }, list("Can", PAIRED_CAN.can), list("Can't", PAIRED_CAN.cant)));
}
// what to allow for a blocked request: the host, or host:port when the port isn't one every site gets
function siteFor(d) { return (STATE?.egress_ports || [443]).includes(d.port) ? d.host : `${d.host}:${d.port}`; }
const siteLabel = (site) => (/:\d+$/.test(site) ? `Allow ${site}` : "Allow");
const allowLabel = (d) => siteLabel(siteFor(d));
// A paired device may only allow the ports every site gets. The server checks it too.
const PORTS_NOTE = "Other ports can only be allowed from the panel on your PC.";
function mayAllow(site) { const m = /:(\d+)$/.exec(site); return isOwner() || !m || (STATE?.egress_ports || [443]).includes(+m[1]); }
const canAllow = (d) => mayAllow(siteFor(d));
function agentById(id) { return (STATE?.agents_list || []).find((a) => a.id === id); }
function agentName(id) { const x = agentById(id); return x ? x.name : id; }
function hostProblems() {
  if (!STATE?.tools) return [];
  const t = STATE.tools, miss = ["qemu", "qemu-img"].filter((k) => !t[k]);
  if (!t.kvm) miss.push("/dev/kvm access");
  if (!STATE.base_image) miss.push("VM image");
  if (t.bwrap_problem) miss.push("a working bubblewrap");  // installed but blocked: VMs don't start (vm.use_bwrap off clears it)
  else if (t.use_bwrap === "required" && !t.bwrap) miss.push("bubblewrap, which config.json requires");
  return miss;
}
// One line about the host for the sidebar and the phone's Settings: [dot class, text]
async function checkHostAgain() {
  try { await api("POST", "/api/host/check"); } catch {}  // a paired device can't, and only reloads
  await loadState();
}
function hostSummary() {
  const miss = hostProblems(), t = STATE.tools;
  // with vm.use_bwrap off, bwrap may be installed but QEMU runs without it
  return miss.length ? ["s-failed", "Not ready, missing " + miss.join(", ")] : t.use_bwrap === "off" ? ["s-busy", "Host ready, bwrap off"]
    : t.bwrap ? ["s-ready", "Host ready"] : ["s-busy", "Host ready, bwrap not installed"];
}

// One calm bar while the panel can't be reached, instead of an error toast at every poll
function connectionBar(down) {
  const bar = document.querySelector(".conn-bar");
  if (!down) { bar?.remove(); return; }
  if (bar) return;
  document.body.append(el("div", { class: "conn-bar", role: "alert" }, el("span", { class: "grow" }, "Can't reach cilly"),
    el("button", { class: "link", type: "button", onclick: () => loadState() }, "Retry")));
}
async function loadState() {
  try {
    STATE = await api("GET", "/api/state");
  } catch (e) { if (!REJECTED) connectionBar(true); return; }
  connectionBar(false);
  if (!ROUTED) { route(); return; }
  applyLook();
  renderSidebar();
  VIEWS[UI.view]?.update?.();
  checkNotify();
  checkPairing();
}
// ---- the look: the standard theme (themes.py), or the one you're editing in Settings, as values for
// style.css's tokens.  An unsaved draft shows on every page until you save or discard it, with a bar
// that says so.  This browser remembers the last look, so a reload doesn't flash Classic first. ----
const LOOK = { set: [], fonts: {}, preview: null, draft: null };
function lookVars(a, t) {
  const scale = (n) => String((n || 100) / 100);
  const vars = { ...(t.colors || {}), "--text-scale": scale(t.size), "--prose-scale": scale(t.prose_size), "--mono-scale": scale(t.mono_size) };
  // heading and prose left empty are the font itself
  for (const [key, token, fallback] of [[t.font, "--font", "geist"], [t.heading || t.font, "--font-heading", "geist"],
    [t.prose || t.font, "--font-prose", "geist"], [t.mono, "--mono", "geist-mono"]]) {
    if (a.builtin_fonts[key]) vars[token] = a.builtin_fonts[key].family;
    else if ((a.fonts || []).some((f) => f.id === key)) { vars[token] = `"cilly-${key}", ${a.builtin_fonts[fallback].family}`; loadFont(key); }
  }
  return vars;
}
// a style changes shapes, icons and cursors as well (static/theme/terminal.css)
function setStyle(style) { document.documentElement?.setAttribute?.("data-style", style === "terminal" ? "terminal" : "classic"); }
function setLook(vars) {
  const root = document.documentElement;
  if (!root) return;
  for (const k of LOOK.set) if (!(k in vars)) root.style.removeProperty(k);
  for (const [k, v] of Object.entries(vars)) if (/^--[a-z0-9-]+$/.test(k) && typeof v === "string") root.style.setProperty(k, v);
  LOOK.set = Object.keys(vars);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", getComputedStyle(root).getPropertyValue("--bg-sidebar").trim());
}
function applyLook() {
  const a = STATE?.appearance;
  if (!a) return;
  lookBar();
  const t = LOOK.preview || a.themes[a.theme] || a.themes.classic;
  if (!changed("look", [t, a.fonts])) return;
  const vars = lookVars(a, t);
  setLook(vars);
  setStyle(t.style);
  if (t !== LOOK.preview) try { localStorage.setItem("cilly-look", JSON.stringify({ vars, style: t.style })); } catch { /* this load only */ }
}
function lookBar() {
  const show = LOOK.draft && !(UI.view === "settings" && (isPhone() ? UI.sec : UI.settingsTab) === "appearance");
  const bar = document.querySelector(".look-bar");
  if (!show) { bar?.remove(); return; }
  if (bar) return;  // already there: rebuilding it every poll drops focus from its buttons
  document.body.append(el("div", { class: "look-bar", role: "status" }, el("span", { class: "grow" }, "Previewing a theme you haven't saved"),
    el("button", { class: "link", type: "button", onclick: () => openSettings("appearance") }, "Back to editor"),
    el("button", { class: "link", type: "button", onclick: () => { LOOK.draft = LOOK.preview = null; applyLook(); } }, "Discard")));
}
// A font you added comes from the panel with the token, so it's loaded here rather than by a URL in the CSS
async function loadFont(id) {
  if (LOOK.fonts[id]) return LOOK.fonts[id];
  LOOK.fonts[id] = (async () => {
    const r = await api("GET", `/api/fonts/${id}`);
    const face = new FontFace(`cilly-${id}`, Uint8Array.from(atob(r.data), (c) => c.charCodeAt(0)).buffer);
    document.fonts.add(await face.load());
  })().catch(() => { delete LOOK.fonts[id]; });  // the fallback font stays
  return LOOK.fonts[id];
}
try {
  const v = JSON.parse(localStorage.getItem("cilly-look") || "null");
  if (v?.vars && typeof v.vars === "object") { setLook(v.vars); setStyle(v.style); }
} catch { /* Classic */ }

// A device asks to pair: it waits in the inbox, where you check its code and approve it.
// Tell you right away rather than at the next inbox poll, since the request expires after 5 minutes.
const PAIR_SEEN = new Set();
function checkPairing() {
  const fresh = (STATE.pairing || []).filter((p) => !PAIR_SEEN.has(p.id));
  for (const p of fresh) PAIR_SEEN.add(p.id);
  if (!fresh.length) return;
  pollInbox();
  toast(fresh.length === 1 ? `${fresh[0].name} wants to pair. Check its code in the inbox.` : `${fresh.length} devices want to pair. Check them in the inbox.`);
  refreshPaired();
}

// ---- notifications: shown by this page (through the service worker, so phones can show
// them too) when a task finishes or an agent needs you. Nothing goes through a push service,
// so they arrive while the panel is open somewhere, even in a background tab. ----
const NOTE = { tasks: null, inbox: null, failed: null };
function notePrefs() {
  let p = null;
  try { p = JSON.parse(localStorage.getItem("cilly-notify") || "null"); } catch { /* defaults */ }
  return Object.assign({ on: false, done: true, fail: true, needs: true }, p || {});
}
function saveNotePrefs(p) { try { localStorage.setItem("cilly-notify", JSON.stringify(p)); } catch { /* this tab only */ } }
const canNotify = () => typeof Notification !== "undefined" && !!navigator.serviceWorker && window.isSecureContext;
async function enableNotifications() {
  if (!canNotify()) { toast("This browser can only show notifications on HTTPS or localhost", true); return false; }
  if (await Notification.requestPermission() !== "granted") { toast("Notifications are blocked for this site in your browser settings", true); return false; }
  await navigator.serviceWorker.register("/sw.js");
  return true;
}
async function notify(title, body, agentId) {
  if (!canNotify() || Notification.permission !== "granted") return;
  try {
    const reg = await navigator.serviceWorker.getRegistration() || await navigator.serviceWorker.register("/sw.js");
    await reg.showNotification(title, { body: String(body || "").slice(0, 200), icon: "/icon-192.png", tag: agentId ? "a-" + agentId : undefined, data: { agentId } });
  } catch { /* not allowed here */ }
}
const watching = (aid) => !document.hidden && UI.view === "agent" && UI.agentId === aid;
// never your own message: it may be private, and it's no news to you
const doneText = (a, t) => t.state === "failed" ? t.error || "" : t.attach ? `Attached ${t.attach}`
  : a.auto_stop ? "It stopped after the task." : "Ready for your next message.";
function checkNotify() {  // after each refresh: tasks that finished since the last one
  if (!STATE) return;
  const fin = [];
  for (const a of STATE.agents_list) for (const t of a.tasks) if (t.state === "done" || t.state === "failed") fin.push([a, t]);
  const failed = STATE.agents_list.filter((a) => statusKey(a) === "failed");
  if (NOTE.tasks === null) { NOTE.tasks = new Set(fin.map(([, t]) => t.id)); NOTE.failed = new Set(failed.map((a) => a.id)); return; }  // not the history
  const p = notePrefs();
  for (const id of NOTE.failed) if (!failed.some((a) => a.id === id)) NOTE.failed.delete(id);
  for (const a of failed) {  // the agent itself, like a VM that didn't start
    if (NOTE.failed.has(a.id)) continue;
    NOTE.failed.add(a.id);
    if (p.on && p.fail && !watching(a.id)) notify(`${a.name} failed`, a.error || "", a.id);
  }
  for (const [a, t] of fin) {
    if (NOTE.tasks.has(t.id)) continue;
    NOTE.tasks.add(t.id);
    if (p.on && p[t.state === "done" ? "done" : "fail"] && !watching(a.id))
      notify(t.state === "done" ? `${a.name} finished` : `${a.name}: task failed`, doneText(a, t), a.id);
  }
}
function checkInboxNotify() {  // new things waiting on you: a blocked site, a research question, findings
  if (NOTE.inbox === null) { NOTE.inbox = new Set(INBOX.items.map((it) => it.key)); return; }
  const p = notePrefs();
  for (const it of INBOX.items) {
    if (NOTE.inbox.has(it.key)) continue;
    NOTE.inbox.add(it.key);
    if (!p.on || !p.needs || (it.agent && watching(it.agent))) continue;
    if (it.kind === "device") notify("A device wants to use cilly", `${it.name} shows the code ${pairCode(it.code)}`);
    else notify(`${agentName(it.agent)} needs you`, itemTitle(it), it.agent);
  }
}

// ---- navigation ----
function go(view, opts) {
  closePop();
  if (view === "home" && isPhone()) view = "agents";  // a phone's home screen is the agent list
  if (!isOwner() && ["settings", "policy", "setup"].includes(view)) view = "more";  // PC only
  VIEWS[UI.view]?.unmount?.();
  if (view !== UI.view) UI.last = { view: UI.view, agentId: UI.agentId, tab: UI.tab, projectId: UI.projectId };  // for Cancel
  Object.assign(UI, { view, sec: null }, opts || {});  // sec: a project's or Settings' own screen on a phone
  if (view !== "agent") UI.agentId = null;
  try { sessionStorage.setItem("cilly-view", JSON.stringify({ view: UI.view, agentId: UI.agentId, tab: UI.tab, projectId: UI.projectId })); } catch { /* fine */ }
  closeDrawer();
  const main = $("main");
  SIGS = {};
  main.replaceChildren();
  VIEWS[view]?.mount?.(main);
  renderSidebar();
  renderMobileNav();
  VIEWS[view]?.update?.();
  syncBack();
  lookBar();
}
const VIEWS = {
  home: { mount: (m) => VIEWS.new.mount(m), update: () => VIEWS.new.update() },
};

// ---- sidebar ----
function closeDrawer() {
  UI.drawer = false;
  $("app").classList.remove("drawer-open");
  $("backdrop").hidden = true;
}
function toggleSidebar() {
  if (window.innerWidth <= 900) {
    UI.drawer = !UI.drawer;
    $("app").classList.toggle("drawer-open", UI.drawer);
    $("backdrop").hidden = !UI.drawer;
  } else {
    UI.sbHidden = !UI.sbHidden;
    $("app").classList.toggle("sb-hidden", UI.sbHidden);
  }
  $("sb-show").hidden = !(UI.sbHidden || window.innerWidth <= 900);
}
function mountShell() {
  const app = $("app");
  const search = el("input", { class: "sb-search", placeholder: "Search agents", "aria-label": "Search agents", spellcheck: false });
  search.addEventListener("input", () => { UI.search = search.value.trim().toLowerCase(); SIGS.sbAgents = null; renderSidebar(); });
  const sidebar = el("nav", { class: "sidebar", id: "sidebar" },
    el("div", { class: "sb-logo" }, el("img", { class: "logo", src: "/logo.png", alt: "" }), el("span", { class: "sb-name" }, "cilly"),
      el("button", { class: "icon-btn", title: "Hide sidebar (⌘\\)", "aria-label": "Hide sidebar", onclick: toggleSidebar }, el("span", { class: "panel-icon" }))),
    el("div", { class: "sb-nav" },
      el("button", { class: "sb-new", id: "sb-new", onclick: () => go("new") }, el("span", { class: "plus" }, "+"), "New agent"),
      el("button", { class: "sb-link sb-inbox", id: "sb-inbox", onclick: () => (POP?.inbox ? closePop() : openInbox()) }, "Inbox",
        el("span", { class: "count-badge", id: "sb-inbox-count", hidden: true }))),
    el("div", { class: "sb-search-wrap" }, search,
      el("button", { class: "sb-kbd", type: "button", title: "Go to an agent, project or page", "aria-label": "Go to an agent, project or page", onclick: () => STATE && openSwitcher() },
        el("span", { class: "kbd-key" }, `${modKey()}K`), el("span", { class: "kbd-touch" }, "Go to"))),
    el("div", { class: "sb-agents", id: "sb-agents" }),
    el("div", { class: "sb-foot" },
      el("button", { class: "sb-link", id: "sb-prs", onclick: () => go("prs") }, "Pull requests"),
      el("button", { class: "sb-link", id: "sb-policy", onclick: () => go("policy") }, "Claude policy"),
      el("button", { class: "sb-link", id: "sb-settings", onclick: () => go(isOwner() ? "settings" : "more") }, "Settings"),
      el("div", { class: "sb-host", id: "sb-host" })));
  app.replaceChildren(sidebar,
    el("div", { class: "backdrop", id: "backdrop", hidden: true, onclick: toggleSidebar }),
    el("button", { class: "icon-btn sb-show", id: "sb-show", hidden: true, title: "Show sidebar (⌘\\)", "aria-label": "Show sidebar", onclick: toggleSidebar },
      el("span", { class: "panel-icon" }), el("span", { class: "dot-badge", id: "sb-show-dot", hidden: true })),
    el("main", { class: "main", id: "main" }));
  mountMobileNav();
  $("sb-show").hidden = window.innerWidth > 900;
}
// The line under an agent's name: its own summary (~/status.txt in the VM), else your last message, else where it works.
function agentLine(a) {
  return ownLine(a) || (a.profile === "research" ? "Open web, no repository" : (a.repo || "No repository, scratch folder"));
}
function ownLine(a) {  // without the where-it-works fallback, for the header, which shows that already
  if (a.move) return a.move.step === 2 ? `Unpacking its home folder on ${String(a.move.to || "").replace(/\.qcow2$/, "")}` : "Packing its home folder";
  if (a.state === "booting") {
    const steps = bootSteps(a);
    return `Starting VM (${bootStep(a, steps) + 1} of ${steps.length})`;
  }
  if (a.error && a.state === "failed") return oneLine(`Failed: ${a.error}`);
  if (a.summary) return oneLine(a.summary);
  const last = a.tasks.length ? a.tasks[a.tasks.length - 1] : null;
  return last ? oneLine(plainLine(last.instructions)) : "";
}
function plainLine(text) {  // the first line of a message without its Markdown
  const line = String(text).split("\n").find((l) => l.trim()) || "";
  return line.replace(/^\s*(?:#+\s*|>\s*|[*+-]\s+)*/, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[`*_]+/g, "").trim();
}
// One short line of plain text, as the server makes the agent's summary: no control or invisible characters
function oneLine(text, max = 80) {
  const chars = [...String(text ?? "").replace(/\p{Cf}/gu, "").replace(/[\p{Cc}\u2028\u2029\s]+/gu, " ").trim()];
  return chars.length > max ? chars.slice(0, max - 1).join("").trimEnd() + "…" : chars.join("");
}
// The panel's word for where an agent is, worked out by the server (never by the agent)
const STATUS_LABEL = { starting: "Starting", stopping: "Stopping", failed: "Failed", running: "Running",
  waiting: "Waiting for you", idle: "Idle", stopped: "Stopped" };
const statusKey = (a) => (Object.hasOwn(STATUS_LABEL, a.status?.key ?? "") ? a.status.key : "stopped");
function statusText(a) {
  const k = statusKey(a), n = a.status?.queued, at = a.status?.stops_at;
  if (a.move) return `Moving to new image (${a.move.step === 2 ? 2 : 1} of 2)`;
  if (k === "running" && n > 0) return `Running and ${n} queued`;
  if (k === "idle" && typeof at === "number") return `Idle, stops in ${Math.max(1, Math.ceil((at * 1000 - Date.now()) / 60000))} min`;
  if (k === "starting" && a.boot_phase) {
    const steps = bootSteps(a), i = steps.findIndex((s) => s[0] === a.boot_phase);
    if (i >= 0) return `Starting (${i + 1} of ${steps.length})`;
  }
  return STATUS_LABEL[k];
}
function statusShort(a) {  // where space is tight: the header of a narrow window, and the phone's list
  const k = statusKey(a), n = a.status?.queued;
  if (a.move) return "Moving…";
  if (k === "running" && n > 0) return `Running +${n}`;
  return { waiting: "Needs you", starting: "Starting…", idle: "Idle" }[k] || STATUS_LABEL[k];
}
// A list row's words: only what its icon doesn't already say, so the name keeps the room
function statusDetail(a) {
  const k = statusKey(a), n = a.status?.queued, at = a.status?.stops_at;
  if (a.move) return `Moving (${a.move.step === 2 ? 2 : 1} of 2)`;
  if (n > 0) return `${n} queued`;
  if (k === "idle" && typeof at === "number") return `Stops in ${Math.max(1, Math.ceil((at * 1000 - Date.now()) / 60000))} min`;
  if (k === "starting" && a.boot_phase) {
    const steps = bootSteps(a), i = steps.findIndex((s) => s[0] === a.boot_phase);
    if (i >= 0) return `Starting (${i + 1} of ${steps.length})`;
  }
  return "";
}
function rowState(a) {  // the icon says the state, also to a screen reader, and the words add what it can't
  const icon = stateIcon(statusKey(a));
  icon.setAttribute("role", "img");
  icon.setAttribute("aria-hidden", "false");
  icon.setAttribute("aria-label", statusText(a));
  return [icon, (name) => [name, el("span", { class: "st st-" + statusKey(a), title: statusText(a) }, statusDetail(a))]];
}
function stateWords(a, short) {
  return el("span", { class: "st st-" + statusKey(a), title: statusText(a) }, short ? statusShort(a) : statusText(a));
}
// Each state has its own shape as well as its color, so it reads without color too
const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  return n;
}
// The animated icons stay in step with the clock, so a redrawn icon carries on where the old one was. This is a
// multiple of every loop: Running turns in 1.6 s (style.css), and in the Terminal style it spins in 0.9 s and
// Waiting's hourglass runs 2.12 s (theme/terminal.css).
const SPIN_MS = 763200;
// ---- routes: straight out, Tor, or a proxy you set up in Settings → Network ----
const routeLabel = (name) => !name || name === "direct" ? "Straight out" : name === "tor" ? "Tor" : name;
const ROUTE_KIND = { socks5: "SOCKS5", http: "HTTP proxy", wireguard: "WireGuard" };
// wireproxy is packaged on Arch only, so it isn't in the host check's package table (install.sh's)
const wireproxyCmd = () => STATE?.host?.family === "arch" ? "sudo pacman -S wireproxy" : "";
// what an agent takes when it has no route of its own: its project's, else the standard in Settings
const inheritedRoute = (projectId) => projectById(projectId)?.route || STATE?.routes?.standard || "direct";
function routeSelect(value, onPick, inherit, label = "Route") {
  const R = STATE?.routes || { names: [], kinds: {}, tor: {} };
  const opts = [...(inherit ? [["", inherit]] : []), ["direct", "Straight out"], ["tor", R.tor?.installed ? "Tor" : "Tor (not installed)"],
    ...R.names.map((n) => [n, `${n} (${ROUTE_KIND[R.kinds[n]] || R.kinds[n]})`])];
  const s = el("select", { class: "input", "aria-label": label }, opts.map(([v, t]) => el("option", { value: v }, t)));
  s.value = value || "";
  if (s.value !== (value || "")) s.value = "";  // a route that was deleted
  s.addEventListener("change", () => onPick(s.value || null));
  return s;
}
function routeNote(name) {
  if (!name || name === "direct") return "Sites see your address.";
  const tor = name === "tor";
  return (tor ? "Sites see a Tor exit, and names are looked up there." : `Sites see ${name}'s address.`)
    + ` If ${tor ? "Tor" : "it"} is down, it reaches nothing rather than going straight out.`;
}
// + on an empty project's line: a new agent in it
const newHere = (p) => el("button", { class: "icon-btn sb-add sb-here", title: `New agent in ${p.name}`, "aria-label": `New agent in ${p.name}`,
  onclick: () => { NEW.project = p.id; NEW.applied = null; go("new"); } }, "+");
// ▾ open, ▸ closed (a pixel chevron in the Terminal style)
const foldMark = (open) => el("span", { class: "fold-mark" + (open ? " open" : "") }, open ? "▾" : "▸");
function stateIcon(key) {
  const s = svg("svg", { width: 12, height: 12, viewBox: "0 0 12 12", class: "st-icon st-" + key, "aria-hidden": "true" });
  const add = (tag, attrs) => s.append(svg(tag, attrs));
  const ring = (more) => add("circle", { cx: 6, cy: 6, r: 4.5, fill: "none", stroke: "currentColor", "stroke-width": 1.6, ...more });
  const disc = () => add("circle", { cx: 6, cy: 6, r: 5.5, fill: "currentColor" });
  if (key === "running" || key === "waiting") s.style.animationDelay = `-${Date.now() % SPIN_MS}ms`;
  if (key === "running") {
    ring({ "stroke-opacity": 0.3 });
    add("path", { d: "M6 1.5 A4.5 4.5 0 1 1 1.5 6", fill: "none", stroke: "currentColor", "stroke-width": 1.6, "stroke-linecap": "round" });
  } else if (key === "waiting") {
    disc();
    add("rect", { x: 5.2, y: 2.6, width: 1.6, height: 4.2, rx: 0.8, class: "st-mark" });
    add("circle", { cx: 6, cy: 8.7, r: 0.9, class: "st-mark" });
  } else if (key === "failed") {
    disc();
    add("path", { d: "M4 4 L8 8 M8 4 L4 8", class: "st-mark line", "stroke-width": 1.5, "stroke-linecap": "round" });
  } else if (key === "stopped") add("rect", { x: 2, y: 2, width: 8, height: 8, rx: 2, fill: "currentColor" });
  else if (key === "idle") ring();
  else ring({ "stroke-dasharray": "2.2 1.8" });  // starting, stopping
  return s;
}
// Stop when idle: the choices, and what turning it on costs for agents whose disk goes when they stop
const IDLE_STOP = [[0, "Off"], [15, "15 min"], [30, "30 min"], [60, "1 hour"], [120, "2 hours"]];
const idleStopLabel = (m) => (IDLE_STOP.find(([v]) => v === m) || IDLE_STOP[0])[1];
const IDLE_STOP_LOSS = {
  ephemeral: "Files in the VM that you haven't exported are lost when it stops.",
  memory: "Files in the VM that you haven't exported are lost when it stops. Its conversations and memory notes are kept.",
};
function renderSidebar() {
  if (!STATE) return;
  const projects = STATE.projects || [];
  const list = STATE.agents_list.filter((a) => !UI.search || a.name.toLowerCase().includes(UI.search) || projectName(a.project).toLowerCase().includes(UI.search));
  const box = $("sb-agents");
  if (!editingIn(box) && changed("sbAgents", [list.map((a) => [a.id, a.name, statusText(a), agentLine(a), a.pinned, a.project, a.archived]), projects.map((p) => [p.id, p.name, p.pending.length]),
    UI.agentId, UI.projectId, UI.view, UI.search, UI.showArchived, [...UI.closedGroups], isOwner()])) {
    box.replaceChildren();
    if (!STATE.agents_list.length) box.append(el("div", { class: "sb-empty" }, "No agents yet. They'll show up here."));
    else if (!list.length) box.append(el("div", { class: "sb-empty" }, "No agent matches."));
    // pinned first, then each project, then the rest; archived ones only when you ask
    const live = list.filter((a) => !a.archived), archived = list.filter((a) => a.archived);
    const section = (title, agents) => {
      if (!agents.length) return;
      box.append(el("div", { class: "sb-caption" }, title), ...agents.map((a) => sidebarRow(a)));
    };
    const pinned = live.filter((a) => a.pinned);
    section("Pinned", pinned);
    // projects: the name opens the project, the arrow folds its agents away
    const shown = projects.filter((p) => !UI.search || p.name.toLowerCase().includes(UI.search) || live.some((a) => a.project === p.id));
    if (shown.length || isOwner()) box.append(el("div", { class: "sb-caption row nowrap" }, el("span", { class: "grow" }, "Projects"),
      isOwner() ? el("button", { class: "icon-btn sb-add", title: "New project", "aria-label": "New project", onclick: () => newProject() }, "+") : ""));
    for (const p of shown) {
      const agents = live.filter((a) => !a.pinned && a.project === p.id), key = "p:" + p.id, closed = UI.closedGroups.has(key);
      box.append(el("div", { class: "sb-project" + (UI.view === "project" && UI.projectId === p.id ? " active" : "") },
        el("button", { class: "sb-project-name", onclick: () => go("project", { projectId: p.id }) },
          el("span", { class: "grow ellipsis" }, p.name), p.pending.length ? el("span", { class: "count-badge", title: "Suggested notes" }, String(p.pending.length)) : ""),
        agents.length ? el("button", { class: "icon-btn sb-fold", "aria-expanded": String(!closed), title: closed ? "Show its agents" : "Hide its agents", onclick: () => {
          closed ? UI.closedGroups.delete(key) : UI.closedGroups.add(key); saveSidebarPrefs(); SIGS.sbAgents = null; renderSidebar();
        } }, closed ? `${agents.length} ` : "", foldMark(!closed)) : newHere(p)));  // an empty project is one line, with a +
      if (!closed) for (const a of agents) box.append(sidebarRow(a));
    }
    section(pinned.length || shown.length ? "Other agents" : "Agents", live.filter((a) => !a.pinned && !projectById(a.project)));
    if (archived.length) {
      box.append(el("button", { class: "sb-caption sb-group", onclick: () => { UI.showArchived = !UI.showArchived; saveSidebarPrefs(); SIGS.sbAgents = null; renderSidebar(); } },
        el("span", { class: "grow" }, `Archived (${archived.length})`), el("span", { class: "dim" }, foldMark(UI.showArchived))));
      if (UI.showArchived) for (const a of archived) box.append(sidebarRow(a));
    }
  }
  $("sb-new").classList.toggle("active", UI.view === "new" || UI.view === "home");
  for (const v of ["prs", "policy", "settings"]) $("sb-" + v).classList.toggle("active", UI.view === v || (v === "settings" && UI.view === "more"));
  $("sb-policy").hidden = !isOwner();
  $("sb-settings").textContent = isOwner() ? "Settings" : "This device";
  const miss = hostProblems();
  if (changed("sbHost", [miss, STATE.tools.bwrap, STATE.tools.bwrap_problem, STATE.tools.use_bwrap, STATE.build?.state])) {
    const [dot, text] = hostSummary();
    $("sb-host").replaceChildren(el("div", { class: "row nowrap" }, el("span", { class: "dot " + dot }), el("span", {}, text)), buildLine());
  }
}
// A running image build shows under the host status everywhere, so you can find it again.
function buildLine() {
  if (STATE.build?.state !== "running" || !isOwner()) return "";
  return el("button", { class: "link build-line", onclick: () => openSettings("images") }, el("span", { class: "pulse" }), "Building an image");
}
function sidebarRow(a) {
  const row = el("div", { class: "sb-row" + (a.id === UI.agentId ? " active" : "") + (a.archived ? " archived" : "") });
  row.agentId = a.id;
  const menu = (anchor) => {
    // the list can redraw while the menu is open, so rename the row that is there by then
    const list = [$("sb-agents"), $("main")].find((box) => box.contains(row));
    const rename = () => [...(list?.querySelectorAll(".sb-row") || [])].find((r) => r.agentId === a.id)?.rename();
    const wide = a.id === UI.agentId && UI.view === "agent";  // room for the open agent's keys
    const opened = openPop(anchor, agentListMenu(a, rename), { width: wide ? 300 : 230, align: "left", onClose: () => more.setAttribute("aria-expanded", "false") });
    if (opened) { more.setAttribute("aria-expanded", "true"); POP.node.focus(); }
  };
  const more = el("button", { class: "icon-btn sb-more", title: "Rename, pin, group, archive…", "aria-label": `Actions for ${a.name}`, "aria-haspopup": "menu", "aria-expanded": "false", onclick: (e) => {
    e.stopPropagation(); menu(more);
  } }, "⋮");
  const line = () => el("div", { class: "l2 ellipsis", title: agentLine(a) }, agentLine(a));
  // the open agent's row isn't redrawn on a click, so the second click of a double-click lands on it
  const open = () => (UI.view === "agent" && UI.agentId === a.id && UI.tab === "chat" ? closeDrawer() : go("agent", { agentId: a.id, tab: "chat" }));
  const l1 = (name) => { const [icon, rest] = rowState(a); return el("div", { class: "l1" }, icon, ...rest(name)); };
  const btn = el("button", { class: "sb-agent", onclick: open, ondblclick: () => row.rename() },
    l1(el("span", { class: "name ellipsis", title: "Double-click to rename" }, a.name)), line());
  row.rename = () => editName(btn, a.name, (name) => renameAgent(a, name), {
    wrap: (f) => el("div", { class: "sb-agent editing" }, l1(f), line()) });
  row.append(btn, more);
  onHold(row, () => menu(row));
  return row;
}
// fn() on a long press (touch or pen) or a right click. The tap that ends a long press doesn't count as a click.
function onHold(node, fn, ms = 500) {
  let timer = 0, held = false, x = 0, y = 0;
  const cancel = () => { clearTimeout(timer); timer = 0; };
  const inField = (e) => e.target?.tagName === "INPUT";  // a rename field keeps its own menu
  node.addEventListener("pointerdown", (e) => {
    held = false;
    if (e.pointerType === "mouse" || inField(e)) return;
    x = e.clientX; y = e.clientY;
    timer = setTimeout(() => { timer = 0; held = true; fn(); }, ms);
  });
  node.addEventListener("pointermove", (e) => { if (timer && Math.hypot(e.clientX - x, e.clientY - y) > 10) cancel(); });
  for (const t of ["pointerup", "pointercancel", "pointerleave"]) node.addEventListener(t, cancel);
  node.addEventListener("touchend", (e) => { if (held) e.preventDefault(); });  // no mouse events after it, they'd close the sheet
  node.addEventListener("contextmenu", (e) => {  // Android sends this for a long press too
    if (inField(e)) return;
    e.preventDefault();
    cancel();
    if (!held) fn();
  });
  node.addEventListener("click", (e) => { if (held) { held = false; e.preventDefault(); e.stopPropagation(); } }, true);
}
// The ⋮ menu on an agent in the sidebar. For the open agent it shows the keys that do the same from anywhere.
function agentListMenu(a, rename, toProjects) {
  const organize = async (body, ok) => {
    try {
      await api("POST", `/api/agents/${a.id}/organize`, body);
      if (ok) toast(ok);
    } catch (e) { toast(e.message, true); }
    closePop(); await loadState();
  };
  const stopped = !a.running && !["booting", "stopping"].includes(a.state);
  const actions = {
    r: () => { closePop(); rename(); },
    p: () => organize({ pinned: !a.pinned }, a.pinned ? "Unpinned" : "Pinned"),
    m: () => showProjects(),
    a: () => (a.archived ? organize({ archived: false }, "Back in the list") : stopped ? organize({ archived: true }, `Archived ${a.name}`) : toast("Stop it first", true)),
    d: async () => { closePop(); if (!isOwner()) return; if (await confirmModal(`Delete ${a.name}?`, "A running VM is killed immediately (no memory export). Logs are kept.", "Delete", true)) agentAction(a, "delete"); },
  };
  const keys = UI.view === "agent" && UI.agentId === a.id && !isPhone();  // a phone has no keys
  const item = (label, key, fn, cls, sub) => el("button", { class: "menu-item " + (cls || ""), onclick: fn },
    el("span", { class: "grow" }, label, sub ? el("span", { class: "sub" }, sub) : ""),
    key === "›" || key === "✓" ? el("span", { class: "dim" }, key) : key && keys ? el("kbd", { class: "menu-key" }, keyLabel(key)) : "");
  const box = el("div", { class: "menu", tabindex: -1 });
  const main = () => box.replaceChildren(
    item("Rename", "r", actions.r),
    item(a.pinned ? "Unpin" : "Pin to the top", "p", actions.p),
    item("Move to project", keys ? "m" : "›", actions.m, "", projectName(a.project)),
    el("div", { class: "sep" }),
    item(a.archived ? "Unarchive" : "Archive", "a", actions.a, "", a.archived ? "" : stopped ? "Hides it until you need it" : "Stop it first"),
    isOwner() ? item("Delete", "d", actions.d, "danger") : "");
  function showProjects() {
    // moving it in gives it the project's instructions, sites, research agent and files
    box.replaceChildren(item("‹ Back", "", main),
      ...(STATE.projects || []).map((p) => item(p.name, p.id === a.project ? "✓" : "", () => organize({ project: p.id }, `Moved to ${p.name}`))),
      a.project ? item("No project", "", () => organize({ project: null }, "Moved to Other agents")) : "",
      isOwner() ? el("div", { class: "sep" }) : "",
      isOwner() ? item("New project…", "", () => { closePop(); newProject((p) => organize({ project: p.id }, `Moved to ${p.name}`)); }) : "");
    box.focus();
  }
  box.actions = actions;
  if (toProjects) showProjects(); else main();
  return box;
}
async function renameAgent(a, name) {  // the server checks the name and says what it allows
  try { await api("POST", `/api/agents/${a.id}/organize`, { name }); toast(`Renamed to ${name}`); } catch (e) { toast(e.message, true); }
  await loadState();
}
function saveSidebarPrefs() {  // per-browser conveniences only
  try { localStorage.setItem("cilly-sidebar", JSON.stringify({ closed: [...UI.closedGroups], archived: UI.showArchived })); } catch { /* fine */ }
}

// ---- quick switcher (⌘K / Ctrl+K): any agent, project or page ----
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
const modKey = () => (IS_MAC ? "⌘" : "Ctrl ");
// Ctrl+K on a Mac stays the text fields' "delete to the end of the line"
// The open agent's shortcuts: Alt+Shift (Option+Shift on a Mac) and a letter. Browsers and desktops leave
// those alone, and they work while you type. By e.code, as Option on a Mac turns the letter into another one.
const AGENT_KEYS = ["r", "p", "m", "a", "d"];
const agentKey = (e) => (e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && AGENT_KEYS.find((k) => e.code === "Key" + k.toUpperCase())) || null;
const keyLabel = (k, mac = IS_MAC) => (mac ? "⌥⇧" : "Alt+Shift+") + k.toUpperCase();
function agentShortcut(k) {
  const a = agentById(UI.agentId);
  if (!a) return;
  const row = [...($("sb-agents")?.querySelectorAll(".sb-row") || [])].find((r) => r.agentId === a.id);
  const rename = () => renameInHeader(a);
  if (k === "m") { if (openPop(row || AV.name, agentListMenu(a, rename, true), { width: 230, align: "left", toggle: false })) POP.node.focus(); }
  else agentListMenu(a, rename).actions[k]();
}
const switcherKey = (e, mac = IS_MAC) => (e.key === "k" || e.key === "K") && !e.altKey && !e.shiftKey && (mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey);
function switcherItems() {
  const agents = [...STATE.agents_list].sort((x, y) => !!x.archived - !!y.archived);  // archived ones last
  const page = (label, fn) => ({ label, kind: "Page", go: fn });
  return [
    ...agents.map((a) => ({ label: a.name, kind: statusText(a), icon: statusKey(a), go: () => go("agent", { agentId: a.id, tab: "chat" }) })),
    ...(STATE.projects || []).map((p) => ({ label: p.name, kind: "Project", go: () => go("project", { projectId: p.id }) })),
    page("Inbox", openInbox), page("New agent", () => go("new")), page("Pull requests", () => go("prs")),
    ...(isOwner() ? [page("Claude policy", () => go("policy")), page("Settings", () => go("settings")),
      ...SETTINGS_TABS.map(([k, label]) => ({ label, kind: "Settings", go: () => openSettings(k) }))]
      : [page("This device", () => go("more"))]),
  ];
}
function switcherMatches(items, query, max = 12) {  // names that start with it first, then the ones that contain it
  const q = query.trim().toLowerCase(), key = (it) => (it.kind === "Settings" ? "settings " : "") + it.label.toLowerCase();
  if (!q) return items.slice(0, max);
  const hits = items.filter((it) => key(it).includes(q) || it.label.toLowerCase().includes(q));
  const starts = (it) => it.label.toLowerCase().startsWith(q) || key(it).startsWith(q);
  return [...hits.filter(starts), ...hits.filter((it) => !starts(it))].slice(0, max);
}
let SWITCHER = null;
function closeSwitcher(refocus = true) {
  if (!SWITCHER) return;
  const { trap, from } = SWITCHER;
  SWITCHER.remove(); SWITCHER = null;
  document.removeEventListener("focusin", trap, true);
  if (refocus && from?.isConnected) from.focus();
}
function openSwitcher() {
  closePop();
  const from = document.activeElement;
  const items = switcherItems();
  const field = el("input", { class: "qs-input", value: "", placeholder: "Go to an agent, project or page…", spellcheck: false, autocomplete: "off",
    "aria-label": "Go to an agent, project or page", role: "combobox", "aria-controls": "qs-list", "aria-expanded": "true" });
  const list = el("div", { class: "qs-list", id: "qs-list", role: "listbox" });
  let shown = [], rows = [], at = 0;
  const pick = (it) => { closeSwitcher(false); it.go(); };
  const mark = () => rows.forEach((r, i) => { r.classList.toggle("on", i === at); r.setAttribute("aria-selected", String(i === at)); if (i === at) r.scrollIntoView?.({ block: "nearest" }); });
  const draw = () => {
    shown = switcherMatches(items, field.value);
    at = 0;
    rows = shown.map((it) => el("button", { class: "qs-row", type: "button", role: "option", tabindex: -1, onclick: () => pick(it) },
      el("span", { class: "qs-icon" }, it.icon ? stateIcon(it.icon) : ""), el("span", { class: "grow ellipsis" }, it.label), el("span", { class: "qs-kind" }, it.kind)));
    list.replaceChildren(...(rows.length ? rows : [el("div", { class: "qs-none" }, "Nothing matches.")]));
    mark();
  };
  field.addEventListener("input", draw);
  field.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (rows.length) { at = (at + (e.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length; mark(); } }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[at]) pick(shown[at]); }
  });
  // a modal: focus stays on the field (the arrows move through the rows), so Tab can't wander into the page behind
  const dialog = el("div", { class: "qs", role: "dialog", "aria-modal": "true", "aria-label": "Quick switcher",
    onmousedown: (e) => { if (e.target !== field) e.preventDefault(); } }, field, list,
    el("div", { class: "qs-foot" }, el("span", {}, "↑↓ to move"), el("span", {}, "Enter to open"), el("span", {}, "Esc to close")));
  dialog.addEventListener("keydown", (e) => {
    if (e.key === "Tab") { e.preventDefault(); field.focus(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSwitcher(); }
  });
  const back = el("div", { class: "qs-back", onmousedown: (e) => { if (e.target === back) closeSwitcher(); } }, dialog);
  back.from = from;
  back.trap = (e) => { if (!back.contains(e.target)) field.focus(); };
  document.addEventListener("focusin", back.trap, true);
  SWITCHER = back;
  document.body.append(back);
  syncBack();
  draw();
  field.focus();
}

// ---- keyboard ----
document.addEventListener("keydown", (e) => {
  const mod = e.metaKey || e.ctrlKey;
  if (switcherKey(e) && STATE && !document.querySelector(".modal-back")) {
    e.preventDefault(); SWITCHER ? closeSwitcher() : openSwitcher();
  }
  else if (mod && e.key === "\\") { e.preventDefault(); toggleSidebar(); }
  else if (agentKey(e) && STATE && UI.view === "agent" && !isPhone() && !SWITCHER && !document.querySelector(".modal-back")) {
    e.preventDefault(); agentShortcut(agentKey(e));
  }
  else if (mod && (e.key === "s" || e.key === "S") && UI.view === "policy") { e.preventDefault(); VIEWS.policy.save?.(); }
  else if (e.key === "Escape") {
    if (SWITCHER) closeSwitcher();
    else if (typeof VOICE !== "undefined" && VOICE.on) voiceStop();
    else closePop();
  }
});
window.addEventListener("resize", () => {
  if (!$("sb-show")) return;  // the token page has no shell
  $("sb-show").hidden = !(UI.sbHidden || window.innerWidth <= 900);
  if (window.innerWidth > 900 && UI.drawer) closeDrawer();
});

// The panel makes a new token every time it starts, so a saved one stops working after a
// restart. Forget it and come back to the paste form (with "Remember" on if it was remembered).
let REJECTED = false;
function tokenRejected() {
  if (REJECTED) return;
  REJECTED = true;
  const was = deviceToken() === TOKEN ? "device" : "tab";
  try { localStorage.removeItem(DEVICE_KEY); } catch { /* fine */ }
  sessionStorage.removeItem("cilly-token");
  sessionStorage.setItem("cilly-stale", was);
  location.reload();
}

// ---- sign-in page: pair this device (approved on your PC), or paste the startup link ----
// The startup link is never in the terminal or the journal: the open command finds it.  The page can't
// know how cilly was installed before you sign in, so it names both ways.
const STARTUP_LINK_CMD = "cilly open";
const onThisPC = () => ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
function mountNoToken() {
  const stale = sessionStorage.getItem("cilly-stale");
  sessionStorage.removeItem("cilly-stale");
  const local = onThisPC();
  const pairBox = el("div", { class: "card stack" });
  pairBox.titled = !local;  // on the PC it sits under its own "Pair this device"
  showPairStart(pairBox);
  const link = (cls) => el("div", { class: "stack tight " + cls },
    el("div", { class: "muted pretty" }, local ? "It changes at every start. Run this on your PC to open it:"
      : "It changes at every start. Run this on your PC, then paste the link it prints:"),
    copyBlock(local ? STARTUP_LINK_CMD : STARTUP_LINK_CMD + " --print"),
    el("div", { class: "muted small pretty" }, `In the cilly folder, run python3 -m ${local ? STARTUP_LINK_CMD : STARTUP_LINK_CMD + " --print"} instead.`),
    tokenForm(false));
  // on the PC itself the startup link is the way in, and pairing is the exception; elsewhere it's the other way round
  const main = local ? el("div", { class: "card stack" }, el("div", { class: "section-title" }, "Open the startup link"), link(""))
    : pairBox;
  const other = local
    ? el("details", { class: "adv" }, el("summary", {}, "Pair this device"), el("div", { class: "stack adv-body" },
      el("div", { class: "muted pretty" }, "A paired device can't open Settings, Claude policy or secrets."), pairBox))
    : el("details", { class: "adv" }, el("summary", {}, "Or use the startup link"), link("adv-body"));
  $("app").replaceChildren(el("div", { class: "center-page" }, el("div", { class: "center-box" },
    el("img", { class: "logo big", src: "/logo.png", alt: "" }),
    el("h1", { class: "page-title" }, stale ? "Sign in again" : "Sign in to cilly"),
    stale ? el("div", { class: "muted pretty" }, stale === "device"
      ? `The saved access stopped working, so it was forgotten. ${local ? "Open the startup link, or pair this device again." : "Pair this device again."}`
      : "This tab's link is from an earlier panel start.") : "",
    main, other)));
}
function guessDeviceName() {
  const ua = navigator.userAgent;
  return /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone"
    : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows PC" : /Linux/.test(ua) ? "Linux PC" : "Browser";
}
function showPairStart(box, note) {
  const name = el("input", { class: "input", value: guessDeviceName(), maxlength: 40, "aria-label": "Name for this device" });
  const start = async () => {
    try { showPairWait(box, await api("POST", "/api/pair/start", { name: name.value })); }
    catch (e) { toast(e.message, true); }
  };
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
  box.replaceChildren(box.titled === false ? "" : el("div", { class: "section-title" }, "Pair this device"),
    note ? el("div", { class: "warn" }, note) : "",
    el("div", { class: "muted pretty" }, PAIRED_SHORT), pairedLists(),
    el("label", { class: "field" }, el("span", { class: "label" }, "Name"), name),
    el("button", { class: "btn primary", onclick: start }, "Pair this device"));
}
function showPairWait(box, req) {
  let stop = false;
  const status = el("div", { class: "muted" }, "Waiting for you to approve it on your PC…");
  box.replaceChildren(el("div", { class: "section-title" }, "Approve it on your PC"),
    el("div", { class: "muted pretty" }, "Your PC now asks to approve this device. Check that it shows this code:"),
    el("div", { class: "pair-code mono" }, `${req.code.slice(0, 3)} ${req.code.slice(3)}`), status,
    el("button", { class: "btn", onclick: () => { stop = true; showPairStart(box); } }, "Cancel"));
  const until = Date.now() + req.expires_in * 1000;
  const tick = async () => {
    if (stop) return;
    let r;
    try { r = await api("POST", "/api/pair/poll", { pair: req.pair }); } catch { r = { state: "pending" }; }  // a blip: keep waiting
    if (r.state === "approved") {
      try { localStorage.setItem(DEVICE_KEY, r.token); } catch { sessionStorage.setItem("cilly-token", r.token); }
      location.reload();
    } else if (r.state === "denied") showPairStart(box, "It was denied on your PC.");
    else if (r.state === "expired" || Date.now() > until) showPairStart(box, "The request expired. Try again, and approve it on your PC within 5 minutes.");
    else setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
}
function tokenForm(rememberFirst) {  // e.g. a home-screen app on your phone: paste the link once
  let remember = !!rememberFirst;
  const inp = el("input", { class: "input mono", type: "password", autocomplete: "off", spellcheck: false, placeholder: "paste the link or the token",
    "aria-label": "Panel link or token" });
  const open = () => {
    const v = inp.value.trim(), m = v.match(/[#&]t=([A-Za-z0-9_-]+)/) || v.match(/^([A-Za-z0-9_-]{16,})$/);
    if (!m) { toast("That doesn't look like the panel link or token", true); return; }
    sessionStorage.setItem("cilly-token", m[1]);
    if (remember) try { localStorage.setItem(DEVICE_KEY, m[1]); } catch { /* this tab only */ }
    location.reload();
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  return el("div", { class: "stack tight" }, el("div", { class: "row nowrap" }, inp, el("button", { class: "btn primary", onclick: open }, "Open")),
    el("label", { class: "row nowrap" }, sw(remember, (on, b) => { remember = on; setSw(b, on); }),
      el("span", { class: "muted pretty" }, "Remember on this device. Anyone who can use this device can then open the panel.")));
}

// ---- boot ----
let ROUTED = false;
function route() {  // the first screen, once the state is there
  ROUTED = true;
  applyLook();
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem("cilly-view") || "null"); } catch { /* fine */ }
  if (isOwner() && !STATE.setup.done && !STATE.agents_list.length) go("setup", { setupStep: 0 });
  else if (OPEN_AGENT && agentById(OPEN_AGENT)) go("agent", { agentId: OPEN_AGENT, tab: "chat" });
  else if (saved && VIEWS[saved.view] && !["review", "setup"].includes(saved.view) && (saved.view !== "agent" || agentById(saved.agentId))) go(saved.view, { agentId: saved.agentId, tab: saved.tab || "chat", projectId: saved.projectId });
  else if (STATE.agents_list.length) isPhone() ? go("agents") : go("agent", { agentId: STATE.agents_list[0].id, tab: "chat" });
  else go("new");
  checkNotify();
  checkPairing();
}
document.addEventListener("DOMContentLoaded", async () => {
  if (!TOKEN) { mountNoToken(); return; }
  mountShell();
  try { STATE = await api("GET", "/api/state"); } catch { if (REJECTED) return; }
  if (STATE) route();
  else {  // an empty shell with the same bar, until the first load works
    connectionBar(true);
    $("main").append(el("div", { class: "center-page" }, el("div", { class: "center-box center-text" },
      el("div", { class: "muted pretty" }, "Your agents will show up here as soon as cilly can be reached."))));
  }
  pollInbox();
  // keep watching in a background tab when notifications are on (browsers slow these timers down)
  setInterval(() => { if (!document.hidden || notePrefs().on) loadState(); }, 4000);
  setInterval(() => { if (!document.hidden || notePrefs().on) pollInbox(); }, 5000);
  navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data?.open && agentById(e.data.open)) go("agent", { agentId: e.data.open, tab: "chat" }); });
  if (notePrefs().on && canNotify() && Notification.permission === "granted") navigator.serviceWorker.register("/sw.js").catch(() => {});
});
