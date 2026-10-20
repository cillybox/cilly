// cilly UI: a small Markdown renderer for agent text (Claude's replies, research findings).
// It builds DOM nodes and only ever inserts text, so nothing in the input can become HTML.
// Links open on a click for https://github.com/. Other http and https links first ask, showing
// where they go, as an agent could hide data in an address. Images are never loaded (the alt text is shown).
"use strict";

function renderMarkdown(src) {
  const root = el("div", { class: "md" });
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  const isBlank = (l) => !l.trim();
  const fence = (l) => l.match(/^\s{0,3}(```+|~~~+)\s*([\w+#.-]*)\s*$/);
  const listItem = (l) => l.match(/^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/);
  const tableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  while (i < lines.length) {
    const l = lines[i];
    if (isBlank(l)) { i++; continue; }
    let m;
    if ((m = fence(l))) {  // fenced code block
      const close = m[1], lang = m[2], body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(close)) body.push(lines[i++]);
      i++;
      root.append(codeBlock(body.join("\n"), lang));
      continue;
    }
    if ((m = l.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) {
      root.append(el("div", { class: "md-h md-h" + Math.min(m[1].length, 4) }, ...inline(m[2])));
      i++; continue;
    }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(l)) { root.append(el("hr", { class: "md-hr" })); i++; continue; }
    if (/^\s{0,3}>/.test(l)) {
      const body = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) body.push(lines[i++].replace(/^\s{0,3}>\s?/, ""));
      root.append(el("blockquote", { class: "md-quote" }, renderMarkdown(body.join("\n"))));
      continue;
    }
    if (l.includes("|") && i + 1 < lines.length && tableSep(lines[i + 1])) {
      const head = cells(l), rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && !isBlank(lines[i])) rows.push(cells(lines[i++]));
      root.append(el("div", { class: "md-table-wrap" }, el("table", { class: "md-table" },
        el("thead", {}, el("tr", {}, head.map((c) => el("th", {}, el("div", { class: "md-cell" }, ...inline(c)))))),
        el("tbody", {}, rows.slice(0, 500).map((r) => el("tr", {}, head.map((_, k) => el("td", {}, el("div", { class: "md-cell" }, ...inline(r[k] || ""))))))))));
      continue;
    }
    if (listItem(l)) {
      root.append(list());
      continue;
    }
    const para = [];  // paragraph: until a blank line or another block starts
    while (i < lines.length && !isBlank(lines[i]) && !fence(lines[i]) && !listItem(lines[i]) &&
           !/^\s{0,3}(#{1,6}\s|>)/.test(lines[i])) para.push(lines[i++]);
    root.append(el("p", { class: "md-p" }, ...inline(para.join("\n"))));
  }
  return root;

  function list() {  // a list, with nested lists by indentation
    const first = listItem(lines[i]), indent = first[1].length, ordered = /\d/.test(first[2]);
    const box = el(ordered ? "ol" : "ul", { class: "md-list" });
    if (ordered && parseInt(first[2], 10) > 1) box.setAttribute("start", String(parseInt(first[2], 10)));
    while (i < lines.length) {
      const m = listItem(lines[i]);
      if (!m || m[1].length < indent) break;
      if (m[1].length > indent) { box.lastChild?.append(list()); continue; }
      const item = [m[3]];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !listItem(lines[i]) && /^\s+\S/.test(lines[i])) item.push(lines[i++].trim());
      const task = item[0].match(/^\[([ xX])\]\s+(.*)$/);
      box.append(el("li", {}, task ? (task[1] === " " ? "☐ " : "☑ ") : "", ...inline(task ? task[2] : item.join(" "))));
    }
    return box;
  }
}

function codeBlock(code, lang) {
  return el("div", { class: "md-code" },
    el("div", { class: "md-code-head" }, el("span", { class: "grow" }, lang || "code"),
      el("button", { class: "link", onclick: async (e) => {
        try { await navigator.clipboard.writeText(code); e.target.textContent = "Copied"; setTimeout(() => { e.target.textContent = "Copy"; }, 1500); }
        catch { toast("Couldn't copy. Select the text instead.", true); }
      } }, "Copy")),
    el("pre", {}, el("code", {}, ...highlight(code, lang))));
}

// Inline spans: `code`, **bold**, *italic*, ~~strike~~, [text](url), ![alt](url), bare URLs.
// Each span stays on one line and is length-bounded, so hostile input can't make the regex slow.
const INLINE = /(`+)([^\n]{0,500}?[^`\n])\1(?!`)|\*\*([^\n]{1,500}?)\*\*|__([^\n]{1,500}?)__|~~([^\n]{1,500}?)~~|(?<![\w*])\*(?!\s)([^\n]{1,500}?)(?<!\s)\*(?!\w)|(?<![\w_])_(?!\s)([^\n]{1,500}?)(?<!\s)_(?!\w)|(!?)\[([^\]\n]{0,500})\]\(\s*<?([^)\s>]{1,2000})>?(?:\s+"[^"\n]{0,200}")?\s*\)|(https?:\/\/[^\s<>()]{1,2000}[^\s<>().,;:!?'"])/g;
function inline(text) {
  const out = [];
  let last = 0, m;
  const s = String(text ?? ""), re = new RegExp(INLINE.source, "g");  // own copy: inline() recurses
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1]) out.push(el("code", { class: "md-ic" }, m[2]));
    else if (m[3] || m[4]) out.push(el("strong", {}, ...inline(m[3] || m[4])));
    else if (m[5]) out.push(el("del", {}, ...inline(m[5])));
    else if (m[6] || m[7]) out.push(el("em", {}, ...inline(m[6] || m[7])));
    else if (m[9] !== undefined) out.push(m[8] ? el("span", { class: "md-img" }, `[image: ${m[9] || "no description"}]`) : mdLink(m[10], m[9]));
    else if (m[11]) out.push(mdLink(m[11], ""));
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
function mdLink(url, text) {
  if (url.startsWith("https://github.com/")) return ghLink(url, text || url);
  let u = null;
  try { u = new URL(url); } catch { /* not an address */ }
  if (!u || !["http:", "https:"].includes(u.protocol) || u.username || u.password) {  // user@ in an address hides the host
    if (!text || text === url) return el("span", { class: "md-url" }, url);
    return el("span", {}, ...inline(text), el("span", { class: "md-url" }, ` (${url})`));
  }
  // the text can't pretend to be another site: it's plain text, and the host follows it
  const ask = (e) => { if (e.button > 1) return; e.preventDefault(); openLink(u.href); };
  return el("a", { href: u.href, class: "md-ext", rel: "noopener noreferrer", target: "_blank", title: u.href, onclick: ask, onauxclick: ask },
    !text || text === url ? url : [text, el("span", { class: "md-host" }, ` (${u.hostname})`)]);
}
function openLink(href) {
  const host = new URL(href).hostname;  // in punycode, so a look-alike name shows as xn--
  confirmModal("Open this link?", el("div", { class: "stack tight" }, el("strong", { class: "link-host" }, host),
    el("code", { class: "link-full" }, href), el("span", { class: "muted" }, "An agent wrote it. Check where it goes.")), "Open")
    .then((ok) => { if (ok) window.open(href, "_blank", "noopener,noreferrer"); });
}

// Syntax highlighting: a small tokenizer (comments, strings, numbers, keywords) that only
// wraps text in <span class="hl-…">. Every pattern consumes at least one character and
// unterminated strings/comments run to the end, so any input is scanned in linear time.
const HL_KW = {
  c: "abstract as async await break case catch class const continue default defer delete do else enum export extends false final finally fn for func function go if impl implements import in instanceof interface let loop match mod module mut namespace new nil null object override package private protected pub public return self static struct super switch this throw throws trait true try type typeof undefined use val var void when where while yield",
  py: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return self True try while with yield",
  rb: "alias and begin break case class def defined do else elsif end ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield require",
  sh: "case do done elif else esac export fi for function if in local readonly return select then until while echo cd exit set unset source sudo",
  sql: "add all alter and as asc between by case create delete desc distinct drop else end exists false from group having in index inner insert into is join key left like limit not null on or order outer primary references right select set table then true union unique update values when where with",
  yaml: "true false null yes no on off",
};
const HL_LANG = {};
const CHARS = new Set("java kotlin kt kts c h cpp cc hpp cxx cs csharp go golang rust rs scala".split(" "));  // 'x' is one char ('a in Rust is a lifetime)
for (const l of "js javascript mjs cjs ts typescript jsx tsx java kotlin kt kts c h cpp cc hpp cxx cs csharp go golang rust rs swift scala dart php groovy gradle json jsonc".split(" "))
  HL_LANG[l] = { kw: "c", line: "//", block: true, bt: !CHARS.has(l), chars: CHARS.has(l) };
for (const l of "py python".split(" ")) HL_LANG[l] = { kw: "py", line: "#", triple: true };
for (const l of "rb ruby".split(" ")) HL_LANG[l] = { kw: "rb", line: "#" };
for (const l of "sh bash shell zsh console fish dockerfile makefile make".split(" ")) HL_LANG[l] = { kw: "sh", line: "#" };
for (const l of "yaml yml toml ini conf".split(" ")) HL_LANG[l] = { kw: "yaml", line: "#" };
HL_LANG.sql = { kw: "sql", line: "--", block: true, ci: true };
for (const l of "css scss less".split(" ")) HL_LANG[l] = { block: true };
for (const l of "html xml svg vue".split(" ")) HL_LANG[l] = { html: true };
const HL_EXT = { mjs: "js", cjs: "js", h: "c", hpp: "cpp", yml: "yaml" };
const HL_CACHE = {};
function hlSpec(lang) {
  const key = String(lang || "").toLowerCase(), spec = HL_LANG[key];
  if (!spec) return null;
  if (HL_CACHE[key]) return HL_CACHE[key];
  const R = String.raw, cmt = [], str = [R`"(?:[^"\\\n]|\\.)*"?`];
  if (spec.triple) cmt.push(R`"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)`);
  if (spec.block) cmt.push(R`\/\*[\s\S]*?(?:\*\/|$)`);
  if (spec.html) cmt.push(R`<!--[\s\S]*?(?:-->|$)`);
  if (spec.line) cmt.push(spec.line.replace(/\//g, "\\/") + R`[^\n]*`);
  str.push(spec.chars ? R`'(?:\\.|[^'\\\n])'` : R`'(?:[^'\\\n]|\\.)*'?`);
  if (spec.bt) str.push(R`\x60(?:[^\x60\\]|\\[\s\S])*\x60?`);
  const re = new RegExp(`(${cmt.join("|") || "(?!)"})|(${str.join("|")})|` +
    R`(\b0[xX][0-9a-fA-F_]+\b|\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?[a-zA-Z]{0,2}\b)|([A-Za-z_$@][\w$]*)`, "g");
  const words = new Set((HL_KW[spec.kw] || "").split(" ").filter(Boolean));
  return (HL_CACHE[key] = { re, words, ci: !!spec.ci });
}
function highlight(code, lang) {  // -> array of strings and spans
  const spec = hlSpec(lang), s = String(code);
  if (!spec || s.length > 200000) return [s];
  const out = [], re = new RegExp(spec.re.source, "g");
  let last = 0, m;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    let cls = "";
    if (m[1] !== undefined) cls = "hl-c";
    else if (m[2] !== undefined) cls = "hl-s";
    else if (m[3] !== undefined) cls = "hl-n";
    else if (m[4] !== undefined) {
      const w = m[4];
      if (spec.words.has(spec.ci ? w.toLowerCase() : w)) cls = "hl-k";
      else if (w[0] === "@") cls = "hl-t";
      else if (/^[A-Z][a-z0-9]\w*$/.test(w)) cls = "hl-t";
      else if (s[re.lastIndex] === "(") cls = "hl-f";
    }
    out.push(cls ? el("span", { class: cls }, m[0]) : m[0]);
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
function langOfFile(path) {
  const m = String(path).toLowerCase().match(/(?:^|\/)(dockerfile|makefile)$|\.([a-z0-9]{1,6})$/);
  if (!m) return "";
  const x = m[1] || m[2];
  return x in HL_EXT ? HL_EXT[x] : x;
}
