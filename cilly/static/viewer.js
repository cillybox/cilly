// cilly UI: browse an agent's files inside its VM and look at one.
// The panel checks each file by its bytes first (filecheck.py) and says what it really is.
// Nothing an agent wrote is ever opened as a page: text is shown as text (invisible and
// direction-changing characters marked), and images are decoded by the browser and redrawn
// on a canvas from their pixels, so the original bytes are never displayed. Downloads are
// plain byte blobs that the browser saves.
"use strict";

const IMG_EXT = /\.(png|jpe?g|gif|webp)$/i;
const CACHE_MAX = 5 << 20;
const FB = { key: "", what: "", path: "", sel: null, q: "" };
const FILE_CACHE = new Map();  // small files already previewed, by path, time and size: a rewritten file is fetched again
const FILES_POLL = 5000;  // the open list follows what the agent writes

function fileSources(a) {
  return isResearch(a) ? [["outbox", "Outbox"]] : a.repo ? [["repo", "Repository"], ["outbox", "Outbox"]] : [["scratch", "Scratch folder"], ["outbox", "Outbox"]];
}
const fileUrl = (a, what, path, download) => `/api/agents/${a.id}/file?what=${encodeURIComponent(what)}&path=${encodeURIComponent(path)}${download ? "&download=1" : ""}`;
const b64bytes = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

// ---- the browser on the Files tab: a list and a preview (one pane at a time on phones) ----
const SORTS = { name: 1, size: -1, added: -1 };  // the first direction for each: A to Z, largest, newest
function fileSorts() {
  try { return JSON.parse(localStorage.getItem("cilly-file-sort") || "{}") || {}; } catch { return {}; }
}
function sortEntries(entries, { key, dir }) {  // folders always first
  const by = { name: (x, y) => x.name.localeCompare(y.name, undefined, { sensitivity: "base", numeric: true }),
    size: (x, y) => (x.size || 0) - (y.size || 0), added: (x, y) => (x.mtime || 0) - (y.mtime || 0) }[key] || (() => 0);
  return [...entries].sort((x, y) => (y.kind === "dir") - (x.kind === "dir") || by(x, y) * dir || x.name.localeCompare(y.name));
}
function fileAge(mtime, now = Date.now()) {  // "05:03" today, else "3 d", "2 w", "4 mo", "1 y"
  if (!mtime) return "";
  const s = now / 1000 - mtime;
  if (s < 86400) return new Date(mtime * 1000).toISOString().slice(11, 16);
  const d = Math.floor(s / 86400);
  return d < 14 ? `${d} d` : d < 61 ? `${Math.floor(d / 7)} w` : d < 730 ? `${Math.floor(d / 30)} mo` : `${Math.floor(d / 365)} y`;
}
function folderSummary(entries) {
  const bytes = entries.reduce((n, e) => n + (e.kind === "file" ? e.size || 0 : 0), 0);
  return folderCount(entries) + (bytes ? `, ${fmtBytes(bytes)}` : "");
}
function folderCount(entries) {
  const dirs = entries.filter((e) => e.kind === "dir").length, files = entries.length - dirs;
  return [dirs ? `${dirs} folder${dirs === 1 ? "" : "s"}` : "", files ? `${files} file${files === 1 ? "" : "s"}` : ""].filter(Boolean).join(", ") || "Empty";
}
const SOURCE_ROOT = { repo: "repository", scratch: "~/scratch", outbox: "~/outbox" };
function fileBrowser(a, up, extras, strip) {  // extras: the toolbar's right end (Export…); strip: a line under it
  const sources = fileSources(a);
  if (FB.key !== a.id || !sources.some((s) => s[0] === FB.what)) Object.assign(FB, { key: a.id, what: sources[0][0], path: "", sel: null });
  const list = el("div", { class: "fb-list" }), crumbs = el("div", { class: "fb-crumbs" }), head = el("div", { class: "fb-head" });
  const count = el("span", { class: "grow ellipsis" }), preview = el("div", { class: "fb-preview" });
  const filter = el("input", { class: "input pill-in fb-filter", placeholder: "Filter", "aria-label": "Filter by name", spellcheck: false, value: FB.q || "" });
  let entries = null, shown = "", loading = false, latest = 0, note = null, order = [];
  const sortOf = () => fileSorts()[FB.what] || { key: "name", dir: 1 };
  const go = (path) => { FB.path = path; FB.sel = null; FB.q = filter.value = ""; showPreview(); load(); };
  const drawCrumbs = () => {
    const parts = FB.path.split("/").filter(Boolean);
    crumbs.replaceChildren(...[["", SOURCE_ROOT[FB.what]], ...parts.map((p, i) => [parts.slice(0, i + 1).join("/"), p])]
      .flatMap(([path, label], i, all) => [i ? el("span", { class: "dim" }, "/") : "",
        el("button", { class: "crumb" + (i === all.length - 1 ? " here" : ""), onclick: () => go(path) }, label)]));
  };
  const setSort = (v) => {  // remembered per source
    const all = fileSorts();
    all[FB.what] = v;
    try { localStorage.setItem("cilly-file-sort", JSON.stringify(all)); } catch { /* this page only */ }
    drawHead(); drawList();
  };
  const drawHead = () => {
    const s = sortOf();
    const btn = (key, label) => el("button", { class: "fb-sort" + (s.key === key ? " on" : ""), title: `Sort by ${label.toLowerCase()}`,
      onclick: () => setSort(s.key === key ? { key, dir: -s.dir } : { key, dir: SORTS[key] }) }, label + (s.key === key ? (s.dir === 1 ? " ↑" : " ↓") : ""));
    const keys = [["name", "Name"], ["size", "Size"], ["added", "Added"]];
    if (isPhone()) {  // one button that cycles through them
      const i = Math.max(0, keys.findIndex(([k]) => k === s.key)), next = keys[(i + 1) % keys.length][0];
      head.replaceChildren(crumbs, el("button", { class: "fb-sort on", title: "Change the order", onclick: () => setSort({ key: next, dir: SORTS[next] }) },
        keys[i][1] + (s.dir === 1 ? " ↑" : " ↓")));
      return;
    }
    head.replaceChildren(el("span"), el("span", {}, btn("name", "Name")), el("span", { class: "fb-sorts" }, btn("size", "Size"), el("span", { class: "dim" }, "·"), btn("added", "Added")));
  };
  const row = (e) => {
    const path = (FB.path ? FB.path + "/" : "") + e.name, stamp = `${e.mtime}:${e.size}`;
    const meta = [e.kind === "file" ? fmtBytes(e.size) : e.kind === "link" ? "link, not followed" : e.kind === "other" ? "not a file" : "", fileAge(e.mtime)].filter(Boolean).join(" · ");
    const open = e.kind === "dir" ? () => go(path) : e.kind === "file" ? () => pick(path, stamp) : null;
    return el("button", { class: "fb-row" + (FB.sel?.path === path ? " sel" : ""), disabled: !open, onclick: open || undefined,
      ondblclick: e.kind === "file" ? () => openFile(a, FB.what, path, stamp) : undefined },
      el("span", { class: "fb-icon" }, fileIcon(e.kind)), el("span", { class: "ellipsis", title: e.name }, e.name), el("span", { class: "fb-meta" }, meta),
      isPhone() ? el("span", { class: "chev" }, open ? "›" : "") : "");
  };
  const drawList = () => {
    if (!entries) return;
    const q = filter.value.trim().toLowerCase();
    const items = sortEntries(entries.filter((e) => !q || e.name.toLowerCase().includes(q)), sortOf());
    order = items;
    const up_ = FB.path ? [el("button", { class: "fb-row", onclick: () => go(FB.path.split("/").slice(0, -1).join("/")) },
      el("span", { class: "fb-icon dim" }, "↑"), el("span", { class: "muted" }, ".."), el("span"))] : [];
    list.replaceChildren(...up_, ...(items.length ? paged(`files:${a.id}:${FB.what}:${FB.path}`, items, row, { step: 200 })
      : [el("div", { class: "fb-empty" }, q ? `Nothing matches “${filter.value.trim()}”.` : "This folder is empty.")]), note || "");
    count.textContent = folderSummary(entries);
  };
  const pathOf = (e) => (FB.path ? FB.path + "/" : "") + e.name;
  const folderImages = () => order.filter((e) => e.kind === "file" && IMG_EXT.test(e.name)).map((e) => ({ what: FB.what, path: pathOf(e), stamp: `${e.mtime}:${e.size}` }));
  const enlarge = (path) => { const all = folderImages(); openImages(a, all, Math.max(0, all.findIndex((x) => x.path === path))); };  // the pictures of this folder, side by side
  const pick = (path, stamp) => {
    if (isPhone()) { IMG_EXT.test(path) ? enlarge(path) : openFile(a, FB.what, path, stamp); return; }
    FB.sel = { path, stamp };
    if (IMG_EXT.test(path)) enlarge(path);  // the preview pane is too small for a picture
    for (const r of list.querySelectorAll(".fb-row.sel")) r.classList.remove("sel");
    drawList();
    showPreview();
  };
  const showPreview = () => {
    if (!FB.sel) {
      preview.replaceChildren(el("div", { class: "fb-none" }, el("span", { class: "fg-3" }, "Select a file to preview it"),
        el("span", {}, "Text, code and images open here. Nothing is run.")));
      return;
    }
    const { path, stamp } = FB.sel, what = FB.what;
    const v = fileView(a, what, path, stamp, () => enlarge(path));
    preview.replaceChildren(el("div", { class: "fb-pv-head" }, el("div", { class: "row nowrap" },
      el("span", { class: "grow ellipsis mono", title: path }, path.split("/").pop()), v.copy, v.dl), v.sub), v.body);
  };
  const load = async (quiet) => {
    if (!quiet) drawCrumbs();
    if (!up) { entries = null; list.replaceChildren(el("div", { class: "fb-empty" }, "Start the agent to see its files. They're inside its VM.")); count.textContent = ""; return; }
    if (!quiet) list.replaceChildren(el("div", { class: "fb-empty" }, "Loading…"));
    const seq = ++latest;  // a slow answer from before you changed folders is dropped
    let r;
    loading = true;
    try { r = await api("GET", `/api/agents/${a.id}/files?what=${FB.what}&path=${encodeURIComponent(FB.path)}`); }
    catch (e) {
      if (!quiet && seq === latest) list.replaceChildren(el("div", { class: "fb-empty err" }, e.message, " ", el("button", { class: "link", type: "button", onclick: () => load() }, "Try again")));
      return;
    }
    finally { if (seq === latest) loading = false; }
    if (seq !== latest) return;
    const sig = JSON.stringify([FB.what, FB.path, r.entries]);
    if (!box.isConnected || (quiet && sig === shown)) return;  // redrawn only when something changed
    shown = sig;
    entries = r.entries;
    note = r.truncated ? el("div", { class: "fb-empty" }, "Only the first 1000 entries are shown.") : null;
    drawList();
  };
  filter.addEventListener("input", () => { FB.q = filter.value; drawList(); });
  const refresh = el("button", { class: "round small", title: "Refresh", "aria-label": "Refresh", disabled: !up, onclick: () => { FILE_CACHE.clear(); load(); } }, "↻");
  const find = el("button", { class: "btn small fb-find", type: "button", "aria-expanded": String(!!FB.q) }, "Filter");
  find.addEventListener("click", () => {  // a phone has no room for the box until you ask for it
    const on = box.classList.toggle("finding");
    find.setAttribute("aria-expanded", String(on));
    if (on) filter.focus(); else { FB.q = filter.value = ""; drawList(); }
  });
  const box = el("div", { class: "fb" + (FB.q ? " finding" : "") },
    el("div", { class: "fb-bar" }, sources.length > 1 ? seg(sources, FB.what, (v) => { FB.what = v; go(""); drawHead(); }) : "",
      isPhone() ? "" : crumbs, filter, find, refresh, extras || ""),  // a phone has them above the list
    strip || "",
    el("div", { class: "fb-panes" },
      el("div", { class: "fb-left" }, head, list, el("div", { class: "fb-foot" }, count, el("span", { class: "nowrap" }, "Read-only"))),
      preview));
  drawHead();
  showPreview();
  setTimeout(load);
  const timer = setInterval(() => {
    if (!box.isConnected) clearInterval(timer);
    else if (up && !document.hidden && !loading) load(true);
  }, FILES_POLL);
  return box;
}
function fileIcon(kind) {
  const s = svg("svg", { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true" });
  if (kind === "dir") s.append(svg("path", { d: "M1.5 4.5a1 1 0 0 1 1-1h3.6l1.4 1.5h6a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z", class: "ic-dir" }));
  else s.append(svg("path", { d: "M3.5 1.5h6l3 3v10h-9z", class: "ic-file" }), svg("path", { d: "M9.5 1.5v3h3", class: "ic-file" }));
  return s;
}

// Only a file the list has seen, with its time and size (stamp), comes from the cache.
async function fetchFile(a, what, path, download, stamp) {
  const key = `${a.id}:${what}:${path}:${stamp}`;
  const hit = stamp && FILE_CACHE.get(key);
  if (hit && (!download || hit.data)) return hit;
  const info = await api("GET", fileUrl(a, what, path, download));
  if (stamp && info.size <= CACHE_MAX) { FILE_CACHE.set(key, info); if (FILE_CACHE.size > 40) FILE_CACHE.delete(FILE_CACHE.keys().next().value); }
  return info;
}

// Decode with the browser and redraw from pixels. If the browser can't decode it, it isn't an image.
async function drawImage(info, maxSide) {
  const bmp = await createImageBitmap(new Blob([b64bytes(info.data)], { type: "application/octet-stream" }));
  const same = bmp.width === info.width && bmp.height === info.height, turned = bmp.width === info.height && bmp.height === info.width;
  if (info.width && !same && !turned) { bmp.close?.(); throw new Error("size mismatch"); }  // not the picture the check saw
  const scale = Math.min(1, (maxSide || 4096) / Math.max(bmp.width, bmp.height));
  const c = el("canvas", { class: "view-img", width: Math.max(1, Math.round(bmp.width * scale)), height: Math.max(1, Math.round(bmp.height * scale)) });
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close?.();
  return c;
}

// Text with the characters that hide things made visible.
const MARK_RE = /[‪-‮⁦-⁩‎‏؜​-‍⁠-⁤­᠎﻿\u0080-\u009f\u{E0000}-\u{E007F}]/u;
// the England, Scotland and Wales flags are made of tag characters too, but hide nothing
const FLAG_RE = /(\u{1F3F4}[\u{E0030}-\u{E0039}\u{E0061}-\u{E007A}]{2,7}\u{E007F})/u;
const hiddenCount = (text) => (text.split(FLAG_RE).filter((_, i) => i % 2 === 0).join("").match(new RegExp(MARK_RE.source, "gu")) || []).length;
function markedText(text, lang) {
  if (!hiddenCount(text)) return highlight(text, lang);
  const out = [];
  let buf = "";
  text.split(FLAG_RE).forEach((part, i) => {
    if (i % 2) { buf += part; return; }  // a flag
    for (const ch of part) {
      if (MARK_RE.test(ch)) {
        if (buf) { out.push(buf); buf = ""; }
        out.push(el("span", { class: "hidden-char", title: "Hidden character" }, "U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")));
      } else buf += ch;
    }
  });
  if (buf) out.push(buf);
  return out;
}

// ---- images in the chat: yours (from /attachments) and the ones the agent put in ~/outbox ----
// Thumbnails are drawn once and kept, so redrawing the chat doesn't fetch them again.
const CHAT_THUMBS = new Map(), THUMB_LOADING = new Set();
function chatThumb(a, what, path) {
  const key = `${a.id}:${what}:${path}`, name = path.split("/").pop();
  const btn = el("button", { class: "chat-thumb", type: "button", title: name, "data-thumb": key, "data-agent": a.id, "data-what": what, "data-path": path,
    onclick: () => {  // every picture in this chat, in the order they appear
      const all = [...document.querySelectorAll(".chat-thumb")].filter((b) => b.dataset.agent === a.id);
      openImages(a, all.map((b) => ({ what: b.dataset.what, path: b.dataset.path })), Math.max(0, all.indexOf(btn)));
    } });
  const have = CHAT_THUMBS.get(key);
  if (have) btn.append(copyCanvas(have));
  else {
    btn.append(el("span", { class: "chat-thumb-name ellipsis" }, name));
    const up = a.running && ["ready", "busy"].includes(a.state);
    if (up && !THUMB_LOADING.has(key)) {
      THUMB_LOADING.add(key);
      fetchFile(a, what, path).then((info) => info.preview === "image" ? drawImage(info, 480) : null).then((c) => {
        if (!c) return;
        c.classList.add("chat-thumb-img");
        CHAT_THUMBS.set(key, c);
        for (const b of document.querySelectorAll(".chat-thumb")) if (b.dataset.thumb === key) b.replaceChildren(copyCanvas(c));
      }).catch(() => {}).finally(() => THUMB_LOADING.delete(key));
    }
  }
  return btn;
}
function copyCanvas(c) {  // a canvas can only be in one place: draw a copy
  const n = el("canvas", { class: c.className, width: c.width, height: c.height });
  n.getContext("2d").drawImage(c, 0, 0);
  return n;
}

// ---- what a file is and what's in it: in the Files tab's preview, and in the viewer ----
function fileView(a, what, path, stamp, onImage) {
  const body = el("div", { class: "viewer-body" }, el("div", { class: "muted" }, "Checking the file…"));
  const sub = el("span", { class: "muted small ellipsis" });
  const dl = el("button", { class: "btn small", disabled: true }, "Download");
  const copy = el("button", { class: "btn small", hidden: true }, "Copy");
  (async () => {
    let info;
    try { info = await fetchFile(a, what, path, false, stamp); }
    catch (e) { body.replaceChildren(el("div", { class: "err" }, e.message)); return; }
    sub.textContent = [info.type.charAt(0).toUpperCase() + info.type.slice(1), fmtBytes(info.size), info.width ? `${info.width} × ${info.height}` : "", info.lines ? plural(info.lines, "line") : ""].filter(Boolean).join(", ");
    dl.disabled = false;
    dl.onclick = () => downloadFile(a, what, path, info.name);
    if (info.preview === "text") { copy.hidden = false; copy.onclick = () => copyFile(a, what, path, info, copy); }
    const verdict = info.warnings.length
      ? el("div", { class: "note-box warn stack tight" }, info.warnings.map((w) => el("div", {}, w)))
      : el("div", { class: "check-ok" }, "✓ ", info.preview === "image" ? `A real ${info.type}` : info.kind === "text" ? "Plain text" : `A ${info.type}`,
        ". Checked by its content, not its name.");
    const parts = [verdict];
    if (info.preview === "image") {
      try {
        const c = await drawImage(info);
        const wrap = el("div", { class: "view-img-wrap", title: "Click to enlarge", onclick: onImage || (() => openImages(a, [{ what, path, stamp }], 0)) }, c);
        parts.push(wrap);
      } catch { parts.push(el("div", { class: "warn" }, "Your browser couldn't decode it as an image, so it isn't shown.")); }
    } else if (info.preview === "text") {
      parts.push(el("pre", { class: "view-text" }, markedText(info.text, langOfFile(info.name))),
        info.text_truncated ? el("div", { class: "muted" }, "Only the start is shown. Download it to see all of it.") : "");
    } else parts.push(el("div", { class: "muted" }, info.kind === "empty" ? "The file is empty." : "No preview for this kind of file. You can still download it."));
    body.replaceChildren(...parts);
  })();
  return { sub, copy, dl, body };
}
// The viewer: a large overlay, full screen on phones
function openFile(a, what, path, stamp) {
  if (IMG_EXT.test(path)) { openImages(a, [{ what, path, stamp }], 0); return; }
  const name = path.split("/").pop(), v = fileView(a, what, path, stamp);
  const close = () => { back.remove(); document.removeEventListener("keydown", key, true); };
  const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const back = el("div", { class: "modal-back viewer-back", onclick: (e) => { if (e.target === back) close(); } },
    el("div", { class: "viewer", role: "dialog", "aria-modal": "true", "aria-label": name },
      el("div", { class: "viewer-head" }, isPhone() ? el("button", { class: "icon-btn m-back", "aria-label": "Back", onclick: close }, "←") : "",
        el("div", { class: "grow stack tight" }, el("span", { class: "title ellipsis" }, name), v.sub),
        v.copy, v.dl, isPhone() ? "" : el("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, "✕")),
      v.body));
  document.addEventListener("keydown", key, true);
  document.body.append(back);
  syncBack();
}

// ---- the image viewer: a blurred overlay over the page, arrows between the pictures, click to zoom, drag to move ----
// items: [{ what, path, stamp }], the pictures of the folder or of the chat. Pictures still go through
// fetchFile and drawImage, so what is shown is redrawn from decoded pixels, as everywhere else.
const LB_FIT_MAX = 8;  // a small picture is enlarged to fill the screen, up to this many times
const lbStep = (i, d, n) => (i + d + n) % n;
const lbFit = (box, w, h) => Math.min(LB_FIT_MAX, box.w / w, box.h / h);
function lbPan(t, size, box) {  // keep the picture on screen: centered when it is smaller than the box
  const m = Math.max(0, (size - box) / 2);
  return Math.min(m, Math.max(-m, t));
}
function openImages(a, items, index) {
  if (!items.length) return;
  let i = Math.min(Math.max(index || 0, 0), items.length - 1), seq = 0, drag = null;
  const V = { canvas: null, fit: 1, s: 1, tx: 0, ty: 0, zoomed: false };
  const name = el("span", { class: "title ellipsis" }), sub = el("span", { class: "muted small ellipsis" }), count = el("span", { class: "lb-count muted small" });
  const dl = el("button", { class: "btn small", type: "button", disabled: true }, "Download");
  const slot = el("div", { class: "lb-slot" }), note = el("div", { class: "lb-note" });
  const many = items.length > 1, born = Date.now();
  const nav = (label, d, glyph) => el("button", { class: "round lb-nav", type: "button", "aria-label": label, onclick: () => { i = lbStep(i, d, items.length); load(); } }, glyph);
  const stage = el("div", { class: "lb-stage" }, slot, note);
  const close = () => { back.remove(); document.removeEventListener("keydown", key, true); window.removeEventListener("resize", refit); };
  const step = (d) => { i = lbStep(i, d, items.length); load(); };
  const key = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    else if (many && (e.key === "ArrowLeft" || e.key === "ArrowRight")) { e.preventDefault(); e.stopPropagation(); step(e.key === "ArrowLeft" ? -1 : 1); }
  };
  const back = el("div", { class: "modal-back lightbox", role: "dialog", "aria-modal": "true", "aria-label": "Image viewer",
    onclick: (e) => { if ((e.target === back || e.target === stage || e.target === slot) && Date.now() - born > 400) close(); } },  // not the second click of a double click
    stage,
    el("button", { class: "icon-btn lb-close", type: "button", "aria-label": "Close", onclick: close }, "✕"),
    el("div", { class: "lb-foot" }, el("div", { class: "stack tight lb-info" }, name, sub),
      many ? el("div", { class: "lb-pager" }, nav("Previous image", -1, "‹"), count, nav("Next image", 1, "›")) : el("span"), dl));
  const box = () => { const r = stage.getBoundingClientRect(); return { w: Math.max(1, r.width - 32), h: Math.max(1, r.height - 32), full: r }; };
  const place = (animate) => {
    const c = V.canvas;
    if (!c) return;
    c.classList.toggle("lb-anim", !!animate);
    c.classList.toggle("lb-pixels", V.s > 1.5);  // a small picture stays sharp-edged when enlarged
    stage.classList.toggle("zoomed", V.zoomed);
    c.style.transform = `translate(-50%, -50%) translate(${V.tx}px, ${V.ty}px) scale(${V.s})`;
  };
  const clamp = () => { const b = box(); V.tx = lbPan(V.tx, V.canvas.width * V.s, b.full.width); V.ty = lbPan(V.ty, V.canvas.height * V.s, b.full.height); };
  const refit = () => {
    if (!V.canvas || V.zoomed) return;
    V.fit = V.s = lbFit(box(), V.canvas.width, V.canvas.height);
    place(false);
  };
  const toggleZoom = (px, py) => {  // zoom in around the point you clicked, or back out
    const b = box().full, cx = b.left + b.width / 2, cy = b.top + b.height / 2;
    if (V.zoomed) Object.assign(V, { zoomed: false, s: V.fit, tx: 0, ty: 0 });
    else {
      const s2 = Math.max(1, V.fit * 2.5), k = s2 / V.s;
      Object.assign(V, { zoomed: true, s: s2, tx: (px - cx) - (px - cx - V.tx) * k, ty: (py - cy) - (py - cy - V.ty) * k });
      clamp();
    }
    place(true);
  };
  const wire = (c) => {
    c.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, tx: V.tx, ty: V.ty, moved: false }; c.setPointerCapture?.(e.pointerId); });
    c.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.hypot(dx, dy) > 4) drag.moved = true;
      if (V.zoomed && drag.moved) { V.tx = drag.tx + dx; V.ty = drag.ty + dy; clamp(); stage.classList.add("dragging"); place(false); }
    });
    const end = (e) => {
      if (!drag) return;
      const d = drag, dx = e.clientX - d.x, dy = e.clientY - d.y;
      drag = null;
      stage.classList.remove("dragging");
      if (!d.moved) toggleZoom(e.clientX, e.clientY);
      else if (!V.zoomed && many && Math.abs(dx) > 60 && Math.abs(dx) > 1.5 * Math.abs(dy)) step(dx < 0 ? 1 : -1);  // a swipe
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", () => { drag = null; stage.classList.remove("dragging"); });
  };
  const load = async () => {
    const mine = ++seq, it = items[i];
    name.textContent = it.path.split("/").pop();
    count.textContent = many ? `${i + 1} of ${items.length}` : "";
    sub.textContent = "";
    dl.disabled = true;
    note.replaceChildren();
    V.canvas = null;
    Object.assign(V, { zoomed: false, tx: 0, ty: 0 });
    stage.classList.remove("zoomed");
    slot.replaceChildren(el("div", { class: "muted" }, "Loading…"));
    let info, c;
    try { info = await fetchFile(a, it.what, it.path, false, it.stamp); } catch (e) { if (mine === seq) slot.replaceChildren(el("div", { class: "err" }, e.message)); return; }
    if (mine !== seq) return;
    sub.textContent = [info.type.charAt(0).toUpperCase() + info.type.slice(1), fmtBytes(info.size), info.width ? `${info.width} × ${info.height}` : ""].filter(Boolean).join(", ");
    dl.disabled = false;
    dl.onclick = () => downloadFile(a, it.what, it.path, info.name);
    if (info.warnings.length) note.replaceChildren(el("div", { class: "note-box warn stack tight" }, info.warnings.map((w) => el("div", {}, w))));
    if (info.preview !== "image") { slot.replaceChildren(el("div", { class: "muted" }, "This isn't a picture the panel can show. You can still download it.")); return; }
    try { c = await drawImage(info); } catch { if (mine === seq) slot.replaceChildren(el("div", { class: "warn" }, "Your browser couldn't decode it as an image, so it isn't shown.")); return; }
    if (mine !== seq) return;
    c.className = "lb-img";
    wire(c);
    slot.replaceChildren(c);
    V.canvas = c;
    V.fit = V.s = lbFit(box(), c.width, c.height);
    place(false);
    if (many) for (const j of [lbStep(i, 1, items.length), lbStep(i, -1, items.length)]) if (items[j].stamp) fetchFile(a, items[j].what, items[j].path, false, items[j].stamp).catch(() => {});  // the next one is ready when you press the arrow
  };
  document.addEventListener("keydown", key, true);
  window.addEventListener("resize", refit);
  document.body.append(back);
  syncBack();
  load();
}

// The whole file, even when the preview only shows its start. Hidden characters are copied
// too, since the copy should be the file, so say so when there are any.
async function copyFile(a, what, path, info, btn) {
  btn.disabled = true;
  const whole = !info.text_truncated ? Promise.resolve(info.text) : fetchFile(a, what, path, true).then((f) => {
    if (f.preview !== "text") throw new Error("It isn't text anymore. Open it again to see what it is now.");
    return new TextDecoder().decode(b64bytes(f.data));
  });
  try {
    // the text as a promise, so the copy still counts as your click after a long download
    if (window.ClipboardItem && navigator.clipboard.write) await navigator.clipboard.write([new ClipboardItem({ "text/plain": whole.then((t) => new Blob([t], { type: "text/plain" })) })]);
    else await navigator.clipboard.writeText(await whole);
  } catch {
    const failed = await whole.then(() => null, (e) => e);
    toast(failed ? failed.message : "This browser doesn't allow copying here. Select the text instead.", true);
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  const hidden = hiddenCount(await whole);
  const marked = info.text_truncated ? "" : ", marked in the preview,";  // past 512 KiB they aren't shown
  if (hidden) toast(hidden === 1 ? `Copied. It contains a hidden character${marked} and it's copied too.`
    : `Copied. It contains ${hidden} hidden characters${marked} and they're copied too.`, true);
  btn.textContent = "Copied";
  setTimeout(() => { btn.textContent = "Copy"; }, 1500);
}

async function downloadFile(a, what, path, name) {
  let info;
  try { info = await fetchFile(a, what, path, true); } catch (e) { toast(e.message, true); return; }
  const url = URL.createObjectURL(new Blob([b64bytes(info.data)], { type: "application/octet-stream" }));
  const link = el("a", { href: url, download: name || "file" });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
