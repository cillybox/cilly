// cilly UI: voice input (mic -> 16 kHz WAV in the page -> panel -> your whisper.cpp server).
"use strict";
// whisper.cpp doesn't stream. To keep it quick, speech is cut into phrases at pauses: a
// finished phrase is transcribed once and stays; only the phrase you're still saying is
// re-sent for the live preview. Silence is never sent (whisper invents text for silence).
const VAD = { pauseMs: 700, minSpeechMs: 250, preRollMs: 300, minGapMs: 400, sessionMs: 10 * 60 * 1000 };
const VOICE = { on: false, starting: false, key: "", gone: 0, btn: null, area: null, ctx: null, stream: null, rate: 48000, prefix: "", suffix: "",
                started: 0, timer: null, seg: null, pre: [], noise: 0.003, segId: 0, finals: [], texts: [],
                interim: "", busy: false, lastSend: 0 };

function voicePcm16k(frames) {
  const n = frames.reduce((s, c) => s + c.length, 0);
  const all = new Float32Array(n);
  let o = 0;
  for (const c of frames) { all.set(c, o); o += c.length; }
  const ratio = VOICE.rate / 16000, len = Math.floor(n / ratio), out = new Int16Array(len);
  for (let i = 0; i < len; i++) {  // average each window: a crude low-pass before decimating
    const a = Math.floor(i * ratio), b = Math.max(a + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = a; j < b && j < n; j++) sum += all[j];
    const v = Math.max(-1, Math.min(1, sum / (b - a)));
    out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  return out;
}
function wavBase64(pcm) {
  const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
  const tag = (o, s) => { for (let i = 0; i < 4; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  tag(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); tag(8, "WAVE"); tag(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 16000, true);
  v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  tag(36, "data"); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
function voiceShow(text) {
  const sep = VOICE.prefix && !/\s$/.test(VOICE.prefix) && text ? " " : "";
  VOICE.area.value = VOICE.prefix + sep + text + VOICE.suffix;
  return (VOICE.prefix + sep + text).length;
}
function voiceText() { return [...VOICE.texts.filter(Boolean), VOICE.interim].filter(Boolean).join(" "); }
function voiceRender() { if (VOICE.area) return voiceShow(voiceText()); }

// one audio buffer (~40 ms): energy-based voice detection, phrase cutting
function voiceFrame(buf) {
  const ms = buf.length / VOICE.rate * 1000;
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  const rms = Math.sqrt(sum / buf.length);
  const voiced = rms > Math.max(0.008, VOICE.noise * 3);
  if (!voiced) VOICE.noise = VOICE.noise * 0.95 + rms * 0.05;  // follow the room's background level
  let seg = VOICE.seg;
  if (!seg) {
    VOICE.pre.push(buf);  // keep a little lead-in so the first syllable isn't cut
    while (VOICE.pre.length * ms > VAD.preRollMs) VOICE.pre.shift();
    if (!voiced) return;
    seg = VOICE.seg = { id: ++VOICE.segId, frames: VOICE.pre, ms: VOICE.pre.length * ms, voicedMs: 0, silentMs: 0, dirty: true };
    VOICE.pre = [];
  } else {
    seg.frames.push(buf);
    seg.ms += ms;
  }
  if (voiced) { seg.voicedMs += ms; seg.silentMs = 0; seg.dirty = true; } else seg.silentMs += ms;
  const maxMs = Math.min(25, STATE.speech?.max_seconds || 120) * 1000;
  if (seg.silentMs >= VAD.pauseMs || seg.ms >= maxMs) voiceCloseSegment();
}
function voiceCloseSegment() {
  const seg = VOICE.seg;
  VOICE.seg = null;
  if (seg && seg.voicedMs >= VAD.minSpeechMs) {
    const frameMs = seg.ms / seg.frames.length;  // drop the pause that ended it, keep ~200 ms
    seg.frames.length -= Math.max(0, Math.min(seg.frames.length - 1, Math.floor((seg.silentMs - 200) / frameMs)));
    seg.idx = VOICE.texts.length;
    VOICE.texts.push(VOICE.interim);  // the preview stands in until the final text arrives
    VOICE.finals.push(seg);
  }
  VOICE.interim = "";
  voicePump();
}
async function voiceTranscribe(frames) {
  return (await api("POST", "/api/transcribe", { audio: wavBase64(voicePcm16k(frames)) })).text;
}
// one request at a time: finished phrases first, then a preview of the current one
async function voicePump() {
  if (VOICE.busy) return;
  if (VOICE.finals.length) {
    const seg = VOICE.finals.shift();
    VOICE.busy = true;
    try { VOICE.texts[seg.idx] = await voiceTranscribe(seg.frames); } catch (e) { toast(e.message, true); }
    VOICE.busy = false;
    voiceRender();
    return voicePump();
  }
  const seg = VOICE.seg;
  if (!VOICE.on || !seg || !seg.dirty || seg.voicedMs < VAD.minSpeechMs || Date.now() - VOICE.lastSend < VAD.minGapMs) return;
  seg.dirty = false;
  VOICE.busy = true; VOICE.lastSend = Date.now();
  try {
    const t = await voiceTranscribe(seg.frames.slice());
    if (VOICE.seg && VOICE.seg.id === seg.id) { VOICE.interim = t; voiceRender(); }  // still the current phrase
  } catch (e) { toast(e.message, true); }
  VOICE.busy = false;
  voicePump();
}
async function voiceStart(btn, area, label, key) {
  if (VOICE.on || VOICE.starting || VOICE.area) return;  // one microphone at a time, also on a double click
  VOICE.starting = true;
  try {
    VOICE.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (e) { toast("Microphone not available: " + e.message, true); return; }
  finally { VOICE.starting = false; }
  VOICE.ctx = new AudioContext();
  VOICE.rate = VOICE.ctx.sampleRate;
  const src = VOICE.ctx.createMediaStreamSource(VOICE.stream);
  const node = VOICE.ctx.createScriptProcessor(2048, 1, 1);
  node.onaudioprocess = (e) => { if (VOICE.on) voiceFrame(new Float32Array(e.inputBuffer.getChannelData(0))); };
  src.connect(node); node.connect(VOICE.ctx.destination);
  Object.assign(VOICE, { on: true, key: key || "", gone: 0, btn, area, label, started: Date.now(), seg: null, pre: [], noise: 0.003, finals: [],
                         texts: [], interim: "", busy: false, lastSend: 0,
                         prefix: area.value.slice(0, area.selectionStart), suffix: area.value.slice(area.selectionEnd) });
  area.readOnly = true;
  btn.classList.add("recording");
  btn.title = onTouch() ? "Stop recording" : "Stop recording (Esc)";
  btn.onVoice?.();
  const tick = () => { if (label) label.textContent = `Recording for ${Math.round((Date.now() - VOICE.started) / 1000)} s. ${onTouch() ? "Tap" : "Click"} to stop.`; };
  tick();
  VOICE.timer = setInterval(() => {
    tick();
    if (Date.now() - VOICE.started >= VAD.sessionMs) { voiceStop(); return; }
    // its message box left the page and no new one took over (another agent, say): stop, and let go of the mic
    VOICE.gone = VOICE.area.isConnected ? 0 : VOICE.gone || Date.now();
    if (VOICE.gone && Date.now() - VOICE.gone > 1000) { voiceStop(); return; }
    voicePump();
  }, 250);
}
async function voiceStop() {
  if (!VOICE.on) return;
  VOICE.on = false;
  clearInterval(VOICE.timer);
  VOICE.stream.getTracks().forEach((t) => t.stop());
  VOICE.ctx.close();
  if (VOICE.label) VOICE.label.textContent = "Transcribing…";
  VOICE.btn.disabled = true;
  VOICE.btn.onVoice?.();
  voiceCloseSegment();
  for (let i = 0; i < 1200 && (VOICE.busy || VOICE.finals.length); i++) await new Promise((r) => setTimeout(r, 50));
  const { btn, area, label } = VOICE;  // a message box drawn again meanwhile took over (voiceAdopt)
  const end = voiceRender();
  area.setSelectionRange(end, end);
  area.readOnly = false; area.focus();
  area.dispatchEvent(new Event("input"));
  btn.disabled = false; btn.classList.remove("recording"); btn.title = "Talk";
  if (label) label.textContent = "";
  VOICE.area = null;
}
function recordingInto(area) { return VOICE.on && VOICE.area === area; }
// A mic button for a textarea; `label` shows "Recording for 12 s. Click to stop.", and onVoice() runs when recording starts or stops.
function micIcon() {  // a line icon like the phone's bottom bar
  const s = svg("svg", { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
  s.append(svg("rect", { x: 9, y: 2.5, width: 6, height: 11.5, rx: 3 }), svg("path", { d: "M5.5 11a6.5 6.5 0 0 0 13 0" }),
    svg("path", { d: "M12 17.5V21" }), svg("path", { d: "M8.5 21h7" }));
  return s;
}
// `key` names the message box ("agent:<id>"), so one drawn again takes over its own recording.
function micButton(area, label, onVoice, key = "") {
  const b = el("button", { type: "button", class: "round mic", title: "Talk", "aria-label": "Talk" }, micIcon());
  b.onVoice = onVoice;
  b.addEventListener("click", () => (VOICE.on ? voiceStop() : voiceStart(b, area, label, key)));  // any mic stops the recording
  b.hidden = !STATE?.speech?.enabled;
  if (key && VOICE.area && VOICE.key === key && !VOICE.area.isConnected) voiceAdopt(b, area, label);
  return b;
}
// The same message box, drawn again while it records or transcribes (another tab and back): the new one carries on
function voiceAdopt(btn, area, label) {
  Object.assign(VOICE, { btn, area, label, gone: 0 });
  area.readOnly = true;
  voiceRender();
  if (VOICE.on) { btn.classList.add("recording"); btn.title = onTouch() ? "Stop recording" : "Stop recording (Esc)"; }
  else { btn.disabled = true; btn.classList.add("recording"); if (label) label.textContent = "Transcribing…"; }
}
