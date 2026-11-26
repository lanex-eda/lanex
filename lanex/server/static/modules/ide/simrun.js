// simrun.js — the Simulation tab's run settings and Run all.
//
// Run settings (per testbench, kept in this browser): the bench's own
// parameters, extra defines, plusargs and a time limit, with the exact
// command underneath (what an expert would type; Copy puts it on the
// clipboard). Run all runs every testbench in turn and lists each one's
// result, decided on the server (simulate.judge): exit code and the
// simulator's own error lines first, then a PASS / FAIL line the bench
// prints. A failing row opens the waveform at the failure.
//
//   initSimRun(ctx)  ctx = { designDir(), tb(), benches(), base() (the shared
//     payload: sources, from_config, include_dirs, extra_sources, testbenches,
//     sim_engine), saveAll(), setPanel(name), note(msg, bad), appendLog(line, cls),
//     showWave(vcdRel, failTime), onBatchDone(rows), defaultLimit(), configDefines() }
import { api, fmt, sse } from "../api.js";
import { formatTime } from "./vcd.js";

const esc = (s) => fmt.escape(String(s === undefined || s === null ? "" : s));
const KEY = "ll.bench:";

let ctx = null;
const batch = { id: null, rows: [], running: false, started: 0, ms: 0, show: "all", sel: -1 };

// ------------------------------------------------------------------ settings

export function settingsFor(tb) {
  if (!tb || !ctx) return {};
  try { return JSON.parse(localStorage.getItem(KEY + ctx.designDir() + "|" + tb) || "{}") || {}; } catch (_e) { return {}; }
}
function saveSettings(tb, s) {
  try {
    const clean = { params: {}, plusargs: s.plusargs || "", defines: s.defines || "", timeout: s.timeout || "" };
    for (const [k, v] of Object.entries(s.params || {})) if (String(v).trim()) clean.params[k] = String(v).trim();
    const empty = !Object.keys(clean.params).length && !clean.plusargs && !clean.defines && !clean.timeout;
    if (empty) localStorage.removeItem(KEY + ctx.designDir() + "|" + tb);
    else localStorage.setItem(KEY + ctx.designDir() + "|" + tb, JSON.stringify(clean));
  } catch (_e) { /* storage blocked: the settings last this visit only */ }
}

// What Simulate sends for this bench's run settings.
export function payloadFor(tb) {
  const s = settingsFor(tb);
  const out = { params: s.params || {}, plusargs: s.plusargs || "", run_defines: s.defines || "" };
  if (s.timeout) out.timeout = +s.timeout;
  return out;
}

export function limitWords(sec) {
  sec = +sec;
  return sec % 3600 === 0 && sec >= 3600 ? sec / 3600 + " h" : sec % 60 === 0 && sec >= 60 ? sec / 60 + " min" : sec + " s";
}

function summaryText(tb) {
  const s = settingsFor(tb);
  const parts = [];
  for (const [k, v] of Object.entries(s.params || {})) parts.push(k + "=" + v);
  if (s.defines) parts.push(s.defines.split(/\s+/).filter(Boolean).map((d) => "-D" + d).join(" "));
  if (s.plusargs) parts.push(s.plusargs);
  if (s.timeout) parts.push(limitWords(s.timeout) + " limit");
  return parts.join(" · ");
}

export function simPaneExtras() {
  return "<button class='btn btn-sm' id='sr-runall' title='Run every testbench, one after another, and list which pass'>" +
    "<svg viewBox='0 0 24 24' width='13' height='13' fill='currentColor' aria-hidden='true'><path d='M4 5l8 7-8 7zM12 5l8 7-8 7z'/></svg> Run all benches</button>";
}

export function settingsHtml() {
  return "<details class='sr-set' id='sr-set'><summary><span class='sr-sumt'>Run settings</span><span class='sr-sum' id='sr-sum'></span></summary>" +
    "<div class='sr-grid' id='sr-grid'></div></details>";
}

async function paintSettings() {
  const grid = document.getElementById("sr-grid");
  const sum = document.getElementById("sr-sum");
  const tb = ctx.tb();
  if (sum) sum.textContent = tb ? summaryText(tb) || "the bench's own defaults" : "";
  if (!grid) return;
  if (!tb) { grid.innerHTML = "<p class='sr-hint'>Write a testbench first: its parameters show here.</p>"; return; }
  const s = settingsFor(tb);
  let pv = null;
  try { pv = await api.simPreview({ ...ctx.base(), testbench: tb, ...payloadFor(tb) }); } catch (_e) { pv = null; }
  if (tb !== ctx.tb()) return;
  const top = (pv && pv.top) || tb.split("/").pop().replace(/\.[^.]+$/, "");
  const vhdl = !!(pv && pv.vhdl);
  const params = (pv && pv.params) || [];
  const cfgDefs = ctx.configDefines();
  const lim = s.timeout || "";
  const opts = [["", "Default (" + limitWords(ctx.defaultLimit()) + ")"], ["30", "30 s"], ["120", "2 min"], ["600", "10 min"], ["1800", "30 min"], ["3600", "1 h"]];
  grid.innerHTML =
    "<span class='sr-k'>" + (vhdl ? "Generics of " : "Parameters of ") + "<code>" + esc(top) + "</code></span>" +
    "<div class='sr-v'>" + (params.length ? "<div class='sr-params'>" + params.map((p) =>
      "<label class='sr-param'><span>" + esc(p.name) + "</span><input class='inp' data-param='" + esc(p.name) + "' type='text' spellcheck='false' " +
      "placeholder='" + esc(p.default === null || p.default === undefined ? "" : p.default) + "' value='" + esc((s.params || {})[p.name] || "") + "' " +
      "aria-label='" + esc(p.name) + ", default " + esc(p.default || "none") + "'/></label>").join("") + "</div>" +
      "<p class='sr-hint'>Empty means the default shown. These are " + esc(top) + "'s own " + (vhdl ? "generics" : "parameters") +
      ". To change the design's, give the testbench a parameter and pass it down: <code>counter #(.WIDTH(WIDTH)) dut (…)</code>.</p>"
      : "<p class='sr-hint'>" + esc(top) + " has no " + (vhdl ? "generics" : "parameters") + ". Add <code>parameter CYCLES = 100;</code> to the testbench to set it from here.</p>") + "</div>" +
    (vhdl ? "" :
      "<span class='sr-k'>Defines</span><div class='sr-v'><input class='inp' id='sr-defs' type='text' spellcheck='false' placeholder='FAST DEPTH=4' value='" + esc(s.defines || "") + "'/>" +
      (cfgDefs.length ? "<span class='sr-cfg'>with the config's " + esc(cfgDefs.join(" ")) + "</span>" : "") + "</div>" +
      "<span class='sr-k'>Plusargs</span><div class='sr-v'><input class='inp' id='sr-plus' type='text' spellcheck='false' placeholder='+seed=42 +verbose' value='" + esc(s.plusargs || "") + "'/>" +
      "<span class='sr-cfg'>read with $test$plusargs and $value$plusargs</span></div>") +
    "<span class='sr-k'>Time limit</span><div class='sr-v'><select id='sr-limit' aria-label='Time limit'>" +
      opts.map(([v, l]) => "<option value='" + v + "'" + (String(lim) === v ? " selected" : "") + ">" + l + "</option>").join("") + "</select>" +
      "<span class='sr-cfg'>Real time on this computer, not simulated time. Stops a bench that never reaches $finish.</span></div>" +
    "<span class='sr-k'>Command</span><div class='sr-v sr-cmdrow'><code class='sr-cmd' id='sr-cmd'>" + esc(pv && pv.command ? pv.command : "") + "</code>" +
      "<button class='btn btn-ghost btn-sm' id='sr-copy' title='Copy the command'>Copy</button></div>" +
    (pv && !pv.ok && pv.error ? "<p class='sr-bad' role='alert'>" + esc(pv.error) + "</p>" : "");
}

let _pvT = 0;
function onSettingsInput(e) {
  const tb = ctx.tb();
  if (!tb) return;
  const s = settingsFor(tb);
  const grid = document.getElementById("sr-grid");
  s.params = {};
  grid.querySelectorAll("[data-param]").forEach((i) => { if (i.value.trim()) s.params[i.dataset.param] = i.value.trim(); });
  const d = document.getElementById("sr-defs"), p = document.getElementById("sr-plus"), l = document.getElementById("sr-limit");
  s.defines = d ? d.value.trim() : "";
  s.plusargs = p ? p.value.trim() : "";
  s.timeout = l ? l.value : "";
  saveSettings(tb, s);
  const sum = document.getElementById("sr-sum");
  if (sum) sum.textContent = summaryText(tb) || "the bench's own defaults";
  clearTimeout(_pvT);
  _pvT = setTimeout(async () => {
    let pv = null;
    try { pv = await api.simPreview({ ...ctx.base(), testbench: tb, ...payloadFor(tb) }); } catch (_e) { return; }
    const cmd = document.getElementById("sr-cmd");
    if (cmd) cmd.textContent = pv.command || "";
    let bad = grid.querySelector(".sr-bad");
    if (pv.ok) { if (bad) bad.remove(); }
    else if (pv.error) {
      if (!bad) { bad = document.createElement("p"); bad.className = "sr-bad"; bad.setAttribute("role", "alert"); grid.appendChild(bad); }
      bad.textContent = pv.error;
    }
  }, e && e.type === "change" ? 0 : 350);
}

// ------------------------------------------------------------------ run all

const STATE = {
  queued: ["QUEUED", "is-idle"], running: ["RUNNING", "is-run"], pass: ["PASS", "is-ok"], fail: ["FAIL", "is-bad"],
  build: ["DIDN'T BUILD", "is-bad"], timeout: ["TIMED OUT", "is-warn"], stop: ["ENDED AT $STOP", "is-warn"],
  ran: ["RAN", "is-idle"], stopped: ["STOPPED", "is-idle"], notrun: ["NOT RUN", "is-idle"], start: ["COULDN'T START", "is-bad"],
};
const WHY = {
  pass: "The bench printed PASS and nothing failed.",
  fail: "A check failed: the bench printed FAIL, stopped at $fatal, or the simulator reported an error.",
  build: "It didn't compile. The errors are in Problems.",
  timeout: "No $finish within the time limit. The waveform so far is kept.",
  stop: "It ended at $stop, which isn't a pass. Use $finish when the test is done.",
  ran: "Ran to the end. It printed no PASS or FAIL line, so check the output and the waveform.",
  stopped: "You stopped it.",
  notrun: "Not run: the batch was stopped before it.",
  start: "The simulator couldn't start.",
};

export function stateWord(st) { return (STATE[st] || [String(st || "").toUpperCase(), "is-idle"])[0]; }

// A simulated time as words: {ticks} in the waveform's unit (precision),
// {value, unit}, or {end}. `unit` is a finish's "(1ps)".
export function timeWords(t, fallbackTick) {
  if (!t) return "";
  if (t.value !== undefined && t.value !== null) return trimNum(t.value) + " " + String(t.unit || "").replace("us", "µs");
  if (t.ticks !== undefined && t.ticks !== null) {
    const unit = t.unit || fallbackTick;
    if (unit) {
      const m = String(unit).match(/^(\d+)\s*([munpf]?s)$/);
      if (m) return formatTime(t.ticks, { mult: +m[1], unit: m[2], exp: { s: 0, ms: -3, us: -6, ns: -9, ps: -12, fs: -15 }[m[2]] });
    }
    return t.ticks + " ticks";
  }
  return "";
}
function trimNum(v) { return String(Math.round(v * 1000) / 1000); }

function endedText(r) {
  if (r.state === "fail") {
    const t = timeWords(r.fail_time);
    return t ? "failed at " + t : r.fail_time && r.fail_time.end ? "failed at the end" : "failed";
  }
  if (r.finish) { const t = timeWords(r.finish); return t ? "$finish at " + t : "$finish"; }
  if (r.state === "timeout") return "time limit";
  if (r.state === "stop") return "$stop";
  return "";
}

function msText(ms) { return !ms ? "" : ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " s"; }

export function summary(rows) {
  const n = rows.length;
  const c = (s) => rows.filter((r) => r.state === s).length;
  const parts = [n + (n === 1 ? " bench" : " benches")];
  if (c("pass")) parts.push(c("pass") + " passed");
  if (c("fail")) parts.push(c("fail") + " failed");
  if (c("build")) parts.push(c("build") + " didn't build");
  if (c("timeout")) parts.push(c("timeout") + " timed out");
  if (c("stop")) parts.push(c("stop") + " ended at $stop");
  if (c("ran")) parts.push(c("ran") + " ran without a verdict");
  if (c("start")) parts.push(c("start") + " couldn't start");
  if (c("notrun") + c("stopped")) parts.push(c("notrun") + c("stopped") + " not run");
  return parts.join(" · ");
}

function renderTable() {
  const host = document.getElementById("sr-results");
  if (!host) return;
  if (!batch.rows.length) { host.hidden = true; return; }
  host.hidden = false;
  const cur = batch.rows.findIndex((r) => r.state === "running");
  const head = batch.running
    ? "Running " + (cur + 1) + " of " + batch.rows.length + (cur >= 0 ? " · " + esc(batch.rows[cur].tb.split("/").pop()) : "") + " · " + Math.round((Date.now() - batch.started) / 1000) + " s"
    : esc(summary(batch.rows)) + (batch.ms ? " · " + msText(batch.ms) : "");
  const failed = batch.rows.filter((r) => ["fail", "build", "timeout", "start", "stop"].includes(r.state)).length;
  const rows = batch.rows.map((r, i) => ({ r, i })).filter(({ r }) => batch.show === "all" || !["pass", "ran"].includes(r.state));
  host.innerHTML =
    "<div class='sr-head'><span class='an-eyebrow'>Run all</span><span class='sr-headt'>" + head + "</span><span class='ide-spacer'></span>" +
    (!batch.running && failed ? "<button class='btn btn-ghost btn-sm' data-sr='rerun'>Run the " + failed + " that didn't pass again</button>" : "") +
    "<span class='seg sr-show' role='group' aria-label='Show'><button type='button' data-sr-show='all' aria-pressed='" + (batch.show === "all") + "'>All</button>" +
      "<button type='button' data-sr-show='bad' aria-pressed='" + (batch.show === "bad") + "'>Didn't pass</button></span>" +
    (batch.running ? "<button class='btn btn-ghost btn-sm' data-sr='stop'>Stop</button>" : "<button class='btn btn-ghost btn-sm' data-sr='close' title='Hide the table'>Close</button>") +
    "</div><div class='sr-table' role='table' aria-label='Testbench results'>" +
    rows.map(({ r, i }) => {
      const [word, cls] = STATE[r.state] || [String(r.state || "").toUpperCase(), "is-idle"];
      const name = r.tb.split("/").pop();
      const waveAt = r.vcd && r.state === "fail" && r.fail_time ? "<button class='btn btn-sm' data-sr='wave' data-i='" + i + "'>Wave at " + esc(timeWords(r.fail_time) || "the failure") + "</button>"
        : r.vcd ? "<button class='btn btn-ghost btn-sm' data-sr='wave' data-i='" + i + "'>Wave</button>" : "";
      const line = r.line || (r.state !== "running" && r.state !== "queued" ? WHY[r.state] || "" : "");
      return "<div class='sr-row" + (i === batch.sel ? " is-sel" : "") + "' role='row' tabindex='0' data-i='" + i + "'>" +
        "<span class='sr-v " + cls + "' role='cell'><i aria-hidden='true'></i>" + (r.state === "running" ? "<span class='ss-spin sr-spin' aria-hidden='true'></span>" : "") + word + "</span>" +
        "<span class='sr-tb' role='cell' title='" + esc(r.tb) + "'>" + esc(name) + "</span>" +
        "<span class='sr-end' role='cell'>" + esc(endedText(r)) + "</span>" +
        "<span class='sr-ms' role='cell'>" + esc(msText(r.ms)) + "</span>" +
        "<span class='sr-line' role='cell' title='" + esc(line) + "'>" + esc(line) + "</span>" +
        "<span class='sr-acts' role='cell'>" + waveAt + "<button class='btn btn-ghost btn-sm' data-sr='log' data-i='" + i + "'>Output</button></span></div>";
    }).join("") + "</div>";
}

let _tick = 0;
function ticking(on) {
  clearInterval(_tick);
  if (on) _tick = setInterval(() => { if (batch.running) renderTable(); else clearInterval(_tick); }, 1000);
}

export async function runAll(only = null) {
  const benches = only || ctx.benches();
  if (!benches.length) {
    ctx.setPanel("sim");
    ctx.note("No testbenches yet. A testbench drives your design and checks what comes out.", true);
    return;
  }
  const unsaved = await ctx.saveAll();
  if (unsaved.length) { ctx.note("Not run: " + unsaved.join(", ") + " changed on disk and your edits aren't saved. Save it to choose which version to keep.", true); return; }
  ctx.setPanel("sim");
  if (ctx.grow) ctx.grow();
  const payload = { ...ctx.base(), benches: benches.map((tb) => ({ testbench: tb, ...payloadFor(tb), timeout: payloadFor(tb).timeout || ctx.defaultLimit() })) };
  let r;
  try { r = await api.simRunAll(payload); } catch (ex) { ctx.note("Run all didn't start: " + (ex.message || ex), true); return; }
  batch.id = r.id;
  batch.running = true;
  batch.started = Date.now();
  batch.ms = 0;
  batch.sel = -1;
  batch.rows = benches.map((tb, i) => {
    const b = (r.benches || [])[i] || {};
    return { tb, state: b.ok === false ? "start" : "queued", line: b.ok === false ? b.error || "" : "", ms: 0, vcd: null };
  });
  ctx.appendLog("── run all · " + benches.length + (benches.length === 1 ? " bench" : " benches") + " · " + new Date().toLocaleTimeString() + " ──", "ll-head");
  setBusy(true);
  renderTable();
  ticking(true);
  ctx.note("Running " + benches.length + (benches.length === 1 ? " testbench" : " testbenches") + " one after another…");
}

function setBusy(on) {
  const b = document.getElementById("sr-runall");
  if (b) { b.disabled = !!on; b.lastChild.textContent = on ? " Running all…" : " Run all benches"; }
  ctx.setBusy(!!on);
}

function onEvent(ev) {
  if (!ev || !ev.type) return;
  if (ev.type === "log" && (ev.source || (ev.payload && ev.payload.source)) === "simbatch") {
    if (batch.running) ctx.appendLog(ev.message);
    return;
  }
  if (ev.type !== "sim_batch" || !batch.id || ev.id !== batch.id) return;
  if (ev.phase === "bench" && ev.row && batch.rows[ev.index]) {
    batch.rows[ev.index] = { ...batch.rows[ev.index], ...ev.row };
    renderTable();
  } else if (ev.phase === "done") {
    batch.running = false;
    batch.ms = ev.ms || 0;
    if (Array.isArray(ev.rows)) ev.rows.forEach((row, i) => { if (batch.rows[i]) batch.rows[i] = { ...batch.rows[i], ...row }; });
    ticking(false);
    setBusy(false);
    renderTable();
    const bad = batch.rows.filter((r) => !["pass", "ran"].includes(r.state)).length;
    ctx.note("Run all: " + summary(batch.rows) + ".", bad > 0);
    ctx.onBatchDone(batch.rows);
  }
}

async function showLog(i) {
  const r = batch.rows[i];
  if (!r) return;
  batch.sel = i;
  renderTable();
  let lines = [];
  try { lines = (await api.simBatch(i)).lines || []; } catch (_e) { lines = []; }
  ctx.appendLog("── " + r.tb + " · " + stateWord(r.state) + " ──", "ll-head");
  for (const l of lines) ctx.appendLog(l);
  ctx.scrollLogEnd();
}

export function initSimRun(c) {
  ctx = c;
  const set = document.getElementById("sr-set");
  if (set && !set._wired) {
    set._wired = true;
    try { set.open = localStorage.getItem("ll.ideRunSet") === "1"; } catch (_e) { /* closed */ }
    set.addEventListener("toggle", () => {
      try { localStorage.setItem("ll.ideRunSet", set.open ? "1" : "0"); } catch (_e) { /* not kept */ }
      if (set.open) { paintSettings(); if (ctx.grow) ctx.grow(); }
    });
    set.addEventListener("input", onSettingsInput);
    set.addEventListener("change", onSettingsInput);
    set.addEventListener("click", async (e) => {
      if (!e.target.closest("#sr-copy")) return;
      const t = document.getElementById("sr-cmd")?.textContent || "";
      try { await navigator.clipboard.writeText(t); ctx.note("Copied the simulation command."); } catch (_e) { ctx.note(t); }
    });
  }
  document.getElementById("sr-runall")?.addEventListener("click", () => runAll());
  const host = document.getElementById("sr-results");
  if (host && !host._wired) {
    host._wired = true;
    host.addEventListener("click", async (e) => {
      const show = e.target.closest("[data-sr-show]");
      if (show) { batch.show = show.dataset.srShow; renderTable(); return; }
      const b = e.target.closest("[data-sr]");
      const row = e.target.closest(".sr-row");
      if (b) {
        const i = +b.dataset.i;
        if (b.dataset.sr === "stop") { try { await api.simCancel(); } catch (_e) { /* the done event follows */ } }
        else if (b.dataset.sr === "close") { batch.rows = []; renderTable(); }
        else if (b.dataset.sr === "rerun") runAll(batch.rows.filter((r) => ["fail", "build", "timeout", "start", "stop"].includes(r.state)).map((r) => r.tb));
        else if (b.dataset.sr === "log") showLog(i);
        else if (b.dataset.sr === "wave") { batch.sel = i; renderTable(); ctx.showWave(batch.rows[i].vcd, batch.rows[i].state === "fail" ? batch.rows[i].fail_time : null, batch.rows[i].tb); }
        return;
      }
      if (row) showLog(+row.dataset.i);
    });
    host.addEventListener("keydown", (e) => {
      const row = e.target.closest(".sr-row");
      if (!row) return;
      if (e.key === "Enter") { e.preventDefault(); showLog(+row.dataset.i); }
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const all = [...host.querySelectorAll(".sr-row")];
        const k = all.indexOf(row) + (e.key === "ArrowDown" ? 1 : -1);
        if (all[k]) all[k].focus();
      }
    });
  }
  sse.on(onEvent);
  // A batch that was running before a reload: pick up its table.
  api.simBatch().then((s) => {
    if (!s || !s.id || !s.rows || !s.rows.length || s.design_dir !== ctx.designDir()) return;
    batch.id = s.id;
    batch.rows = s.rows;
    batch.running = !!s.running;
    batch.started = (s.started || Date.now() / 1000) * 1000;
    batch.ms = s.ms || 0;
    if (batch.running) { setBusy(true); ticking(true); }
    renderTable();
  }).catch(() => {});
  return { paintSettings, runAll };
}

export function benchChanged() { if (document.getElementById("sr-set")?.open) paintSettings(); else { const sum = document.getElementById("sr-sum"); if (sum && ctx) sum.textContent = ctx.tb() ? summaryText(ctx.tb()) || "the bench's own defaults" : ""; } }
export function resetBatch() { batch.rows = []; batch.id = null; batch.running = false; renderTable(); }
export function batchRunning() { return batch.running; }
