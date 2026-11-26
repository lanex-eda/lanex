// sweeps.js — sweeps inside Runs (what the DSE tab used to be): the "New
// sweep" sheet and the summary of a sweep under its row.
//
// A sweep runs the same design with one or more settings at several values,
// one full run per point, one at a time (controller/dse.py, /api/dse/*). Each
// point is named by what it tried (sweep-util-65) and its real outcome is kept.
// Sweeps are a last resort after the RTL and constraints are right; the sheet
// keeps the default small and says so.
//
// Before Start, the server plans the sweep (/api/dse/plan): each setting's
// value in this design and where it comes from, every value checked the way
// LibreLane reads it, the one-off Setup settings that ride along on every
// point, and what would make every point build the same chip (a fixed die).

import { api, fmt } from "./api.js";
import { state } from "./state.js";
import { toast } from "./toast.js";
import { icon } from "./icons.js";
import { confirmDialog, customDialog } from "./dialog.js";
import { openLayer, closeLayer } from "./overlay.js";
import { fmtDur } from "./reportcard.js";
import * as FS from "./flowstages.js";
import * as F from "./metricfmt.js";
import { toCsv, downloadCsv } from "./csvutil.js";
import { localeNum } from "./intlcache.js";
import * as SM from "./studymodel.js";
import * as SQ from "./studyqueue.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));
const MAX_RUNS = 64;               // controller/dse.py MAX_COMBOS
export const COMPARE_MAX = 12;     // compare.js MAX

// Goals in plain words -> the settings that move them. Their values are
// filled from this design's own settings when the sheet opens (fillGoal).
// Three goals that work, and your own (studies, audit 6.2). "Placement
// density" is not a goal: it sizes nothing, and LibreLane derives it from the
// utilisation when the config leaves it unset.
const GOALS = [
  { id: "clock", title: "Fastest clock", sub: "How fast can it run? Shorter clock periods from the one you use now; every one runs.",
    axes: [{ var: "CLOCK_PERIOD", values: "" }] },
  { id: "area", title: "Smallest chip", sub: "How small can the die get? Packs the core fuller until placement or routing fails.",
    axes: [{ var: "FP_CORE_UTIL", values: "40:60:10" }] },
  { id: "synth", title: "Best synthesis recipe", sub: "Which of LibreLane's recipes (SYNTH_STRATEGY) gives the smallest or fastest chip, each run in full.",
    axes: [{ var: "SYNTH_STRATEGY", values: "AREA 0, DELAY 0, DELAY 4" }] },
  { id: "own", title: "Your own settings", sub: "Any setting LibreLane has, up to three, every combination or paired.", axes: [{ var: "", values: "" }] },
];
// What the sheet asks the server for when it opens, so the goals can start
// from this design's values (and the guards can see a fixed die, a fixed
// density, an SDC file that sets its own clock).
const CONTEXT_KEYS = ["CLOCK_PERIOD", "FP_CORE_UTIL", "FP_SIZING", "DIE_AREA", "CORE_AREA", "PL_TARGET_DENSITY_PCT", "SYNTH_STRATEGY",
  "PNR_SDC_FILE", "SIGNOFF_SDC_FILE"];

const S = { el: null, goal: "area", axes: [], mode: "grid", stop: false, vars: null, busy: false, drop: new Set(),
  plan: null, planSeq: 0, planTimer: 0, ctx: {}, touched: false, note: "" };

// ------------------------------------------------------------------ values
// "35, 50, 65" or "35:65:15" (start:stop:step, inclusive) -> { values, error }.
// Text that starts like a range but is not one ("35:65:x") is an error, never
// one text value; `kind` (introspect's: int, number, bool, choice, numbers,
// text...) checks each value the way LibreLane will read it.
export function parseValuesStrict(text, kind = "", choices = []) {
  const t = String(text || "").trim();
  if (!t) return { values: [], error: "" };
  let values;
  // N points between two ends, "35..65/4", evenly spaced; "1..100/5 log" in
  // equal ratios (for settings that act over decades).
  const np = /^(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)\s*\/\s*(\d+)(\s+log)?$/i.exec(t);
  if (np) {
    const [a, b, n, log] = [Number(np[1]), Number(np[2]), Number(np[3]), !!np[4]];
    if (n < 2 || n > MAX_RUNS) return { values: [], error: "Give between 2 and " + MAX_RUNS + " points, like 35..65/4." };
    if (log && (a <= 0 || b <= 0)) return { values: [], error: "A log spread needs both ends above 0." };
    const dec = Math.max(...[np[1], np[2]].map((x) => (x.split(".")[1] || "").length));
    values = [];
    for (let i = 0; i < n; i++) {
      const v = log ? a * Math.pow(b / a, i / (n - 1)) : a + (b - a) * i / (n - 1);
      const r = Number(v.toFixed(Math.max(dec, Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2)));
      if (!values.includes(String(r))) values.push(String(r));
    }
  } else if (!/::/.test(t) && /^-?[\d.]+\s*:/.test(t)) {
    const m = /^(-?\d+(?:\.\d+)?)\s*:\s*(-?\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$/.exec(t);
    if (!m) return { values: [], error: "'" + t + "' isn't a range. Write start:stop:step with numbers, like 35:65:10." };
    const [a, b, s] = [Number(m[1]), Number(m[2]), Number(m[3])];
    if (!(s > 0)) return { values: [], error: "A range's step must be above 0." };
    if (Math.abs(b - a) / s > 999) return { values: [], error: "That range has more than a thousand values." };
    const dec = Math.max(...[m[1], m[2], m[3]].map((x) => (x.split(".")[1] || "").length));
    values = [];
    for (let v = a, i = 0; (a <= b ? v <= b + 1e-9 : v >= b - 1e-9) && i < 1000; v = a + (++i) * (a <= b ? s : -s)) values.push(String(Number(v.toFixed(dec))));
  } else {
    values = t.split(",").map((x) => x.trim()).filter(Boolean);
  }
  for (const v of values) {
    const why = valueError(v, kind, choices);
    if (why) return { values, error: why };
  }
  return { values, error: "" };
}
export function parseValues(text) { return parseValuesStrict(text).values; }

function valueError(v, kind, choices) {
  const isnum = (x) => x !== "" && Number.isFinite(Number(x));
  if (kind === "int" && !/^[+-]?\d+$/.test(v)) return "'" + v + "' isn't a whole number.";
  if (kind === "number" && !isnum(v)) return "'" + v + "' isn't a number.";
  if (kind === "bool" && !/^(true|false|1|0)$/i.test(v)) return "'" + v + "': this setting takes true or false.";
  if (kind === "choice" && choices && choices.length && !choices.includes(v)) return "'" + v + "' isn't one of " + choices.slice(0, 6).join(", ") + (choices.length > 6 ? ", …" : "") + ".";
  if (kind === "numbers" && !v.split(/[;\s]+/).filter(Boolean).every(isnum)) return "'" + v + "' should be numbers separated by spaces, like 0 0 200 200.";
  return "";
}

// The planned points, as the server expands them (dse.expand_sweep): every
// combination in order, or paired value by value. [] when the axes are not
// ready.
export function planPoints(axes, mode) {
  const vals = axes.map((a) => parseValues(a.values));
  if (!axes.length || vals.some((v) => !v.length) || axes.some((a) => !a.var)) return [];
  if (mode === "list" && axes.length > 1) {
    if (!vals.every((v) => v.length === vals[0].length)) return [];
    return vals[0].map((_, i) => Object.fromEntries(axes.map((a, j) => [a.var, vals[j][i]])));
  }
  let out = [{}];
  axes.forEach((a, j) => { out = out.flatMap((p) => vals[j].map((v) => ({ ...p, [a.var]: v }))); });
  return out;
}
const pointKeyOf = (pt) => JSON.stringify(Object.keys(pt).sort().map((k) => [k, String(pt[k])]));

// How many runs the axes make: grid = product, paired = the common length.
export function countRuns(axes, mode) {
  const lens = axes.map((a) => parseValues(a.values).length);
  if (!lens.length || lens.some((n) => !n)) return 0;
  if (mode === "list") return lens.every((n) => n === lens[0]) ? lens[0] : -1;
  return lens.reduce((x, y) => x * y, 1);
}

// Clock periods from the one the design uses now, downward. With the slack a
// past run had at a known period, the steps reach about where it should stop
// meeting timing (period - slack); a design failing timing now gets periods
// above the current one instead. Round numbers, the current one first.
export function clockValues(cur, hint = null) {
  cur = Number(cur);
  if (!(cur > 0)) return [];
  const step = cur >= 20 ? 1 : cur >= 4 ? 0.5 : 0.1;
  const round = (x) => Number((Math.round(x / step) * step).toFixed(2));
  let pts = [1, 0.9, 0.8, 0.7].map((f) => cur * f);
  if (hint && Number.isFinite(hint.limit) && hint.limit > 0) {
    if (hint.limit < cur) {
      const lo = Math.max(cur * 0.3, Math.min(cur * 0.9, hint.limit));
      pts = [0, 1, 2, 3].map((i) => cur - (cur - lo) * i / 3);
    } else {
      const hi = Math.max(hint.limit, cur * 1.1) * 1.1;
      pts = [0, 1, 2, 3].map((i) => cur + (hi - cur) * i / 3);
    }
  }
  const out = [];
  for (const p of pts) { const r = p === cur ? cur : round(p); if (r > 0 && !out.includes(r)) out.push(r); }
  return out;
}

// A die area ("0 0 300 300" or [0, 0, 300, 300]) shrunk by each factor (per
// side), from its lower-left corner.
export function dieValues(die, factors = [1, 0.9, 0.8]) {
  const n = (Array.isArray(die) ? die : String(die || "").split(/[,;\s]+/)).filter((x) => x !== "").map(Number);
  if (n.length !== 4 || !n.every(Number.isFinite) || n[2] <= n[0] || n[3] <= n[1]) return [];
  const [x0, y0, x1, y1] = n;
  const r = (v) => (Math.abs(v) >= 50 ? Math.round(v) : Number(v.toFixed(1)));
  return factors.map((f) => [x0, y0, r(x0 + (x1 - x0) * f), r(y0 + (y1 - y0) * f)].join(" "));
}

// Typical full-run time of this design: the median of its passed runs.
function typicalRunSecs() {
  const t = (state.runs || []).filter((r) => r.success && r.wall_time_s).map((r) => r.wall_time_s).sort((a, b) => a - b);
  return t.length ? t[Math.floor(t.length / 2)] : null;
}

const varMeta = (name) => (S.vars || []).find((v) => v.name === name) || null;
const ctxOf = (name) => (S.plan && S.plan.context && S.plan.context[name]) || S.ctx[name] || null;
const kindOf = (name) => (ctxOf(name) && ctxOf(name).kind) || (varMeta(name) && varMeta(name).kind) || "";
const choicesOf = (name) => (ctxOf(name) && ctxOf(name).choices) || (varMeta(name) && varMeta(name).choices) || [];
const blank = (v) => v === null || v === undefined || (typeof v === "string" && !v.trim());
const asText = (v) => (Array.isArray(v) ? v.join(" ") : v == null ? "" : String(v));

// ------------------------------------------------------------------ sheet
// `goal` (a GOALS id such as "clock") opens the sheet on that preset;
// `values` fills its first setting's values (e.g. "12.5, 10, 8").
export async function openSweepSheet(goal, { values, full = false } = {}) {
  if (!state.designDir) { toast.show("Open a project first: a sweep runs the open design.", "info"); return; }
  if (!S.el) build();
  S.ctx = {};
  S.plan = null;
  S.touched = !!values;
  S.drop = new Set();
  pickGoal(GOALS.some((g) => g.id === goal) ? goal : S.goal);
  if (values && S.axes[0]) { S.axes[0].values = values; paintAxes(); }
  // From an exploration's table: run the picked recipes in full now.
  const ex = S.el.querySelector("#sw-explore");
  if (ex) ex.checked = !full;
  S.el.hidden = false;
  // A dialog on the overlay stack: Tab stays in the sheet, Esc closes it (and
  // only it), closing puts focus back.
  openLayer(S.el, { kind: "dialog", labelledBy: "sw-title", onClose: hideSheet, focus: () => S.el.querySelector(".sw-goal.is-on") });
  requestAnimationFrame(() => S.el.classList.add("is-open"));
  S.el.querySelector(".sw-goal.is-on")?.focus();
  const varsP = S.vars ? Promise.resolve() : api.variables().then((v) => { S.vars = (v && (v.data || v)) || []; }).catch(() => { S.vars = state.variables || []; })
    .then(() => {
      const dl = S.el.querySelector("#sw-vars");
      dl.innerHTML = (S.vars || []).map((v) => "<option value='" + esc(v.name) + "'>" + esc((v.description || "").slice(0, 80)) + "</option>").join("");
    });
  // This design's own values, so the goals start from them.
  const ctxP = api.dsePlan({ axes: [], keys: CONTEXT_KEYS, base_overrides: baseOverrides() })
    .then((p) => { S.ctx = (p && p.context) || {}; }).catch(() => { S.ctx = {}; });
  // How many points the queue runs at once (lane 1: memory and cores).
  S.sdcBlock = "";
  S.par = null;
  const parP = api.queue().then((q) => { const mp = (q && q.max_parallel) || {}; S.par = { n: Number(mp.now) || 1, why: mp.why || "" }; }).catch(() => { S.par = null; });
  await Promise.all([varsP, ctxP, parP]);
  sdcGuard(S.ctx.PNR_SDC_FILE, S.ctx.SIGNOFF_SDC_FILE);
  if (S.el.hidden) return;
  if (!S.touched) pickGoal(S.goal);
  else requestPlan();
}

export function closeSweepSheet() {
  if (!S.el) return;
  closeLayer(S.el);
  hideSheet();
}

function hideSheet() {
  if (!S.el) return;
  S.el.classList.remove("is-open");
  setTimeout(() => { if (S.el && !S.el.classList.contains("is-open")) S.el.hidden = true; }, 180);
}

function baseOverrides() {
  // Setup's one-off settings (the per-run layer) and the picked PDK: what
  // every point of the sweep carries.
  const o = {};
  for (const [k, v] of Object.entries(state.varsValues || {})) if (!blank(v)) o[k] = v;
  if (state.selectedPdk) o.PDK = state.selectedPdk;
  if (state.selectedScl) o.STD_CELL_LIBRARY = state.selectedScl;
  return o;
}

function build() {
  const el = document.createElement("div");
  el.className = "sw-back";
  el.hidden = true;
  el.innerHTML =
    "<aside class='sw-sheet' role='dialog' aria-modal='true' aria-labelledby='sw-title'>" +
    "<header class='sw-head'><div><div class='an-eyebrow'>New sweep</div><h2 class='an-title' id='sw-title'>Run this design at several settings</h2>" +
    "<p class='an-lede'>One full run per point; the queue runs as many at once as memory and cores allow. Fix the RTL and the constraints first: a sweep finds a better setting, it doesn't fix a design.</p></div>" +
    "<button class='iconbtn sw-x' data-sw='close' aria-label='Close'>" + icon("x", { size: 16 }) + "</button></header>" +
    "<div class='sw-body'>" +
    "<div class='sw-lbl'>What are you after?</div><div class='sw-goals' role='radiogroup'>" +
    GOALS.map((g) => "<button class='sw-goal' role='radio' data-goal='" + g.id + "'><b>" + esc(g.title) + "</b><span>" + esc(g.sub) + "</span></button>").join("") + "</div>" +
    "<div class='sw-lbl'>Settings and values <span class='muted'>A list like 35, 50, 65; start:stop:step like 35:65:15; or N points like 35..65/4 (add log for equal ratios, like 1..100/5 log).</span></div>" +
    "<div id='sw-axes'></div><datalist id='sw-vars'></datalist>" +
    "<button class='btn btn-ghost btn-sm' data-sw='add'>" + icon("plus", { size: 12 }) + " Another setting</button>" +
    "<p class='sw-note' id='sw-note' hidden></p>" +
    "<div class='sw-mode' id='sw-mode' hidden><span class='sw-lbl'>Combine</span>" +
    "<label><input type='radio' name='sw-mode' value='grid' checked/> Every combination</label>" +
    "<label><input type='radio' name='sw-mode' value='list'/> Paired, value by value</label></div>" +
    "<div class='sw-preview' id='sw-preview' aria-live='polite'></div>" +
    "<label class='sw-stop' id='sw-stop-row'><input type='checkbox' id='sw-stop'/> Stop at the first run that is clean (timing met and every check passed)</label>" +
    "<label class='sw-stop' id='sw-explore-row' hidden><input type='checkbox' id='sw-explore' checked/> <span>Explore all 9 recipes first, in seconds: LibreLane's SynthesisExploration synthesises the design with each one and times it before layout. " +
      "Then run the best one or two in full from its table in Runs. It stops before layout, so the config's layout settings are not read. <span class='muted'>Untick to run the recipes above in full now.</span></span></label>" +
    "<div class='sw-riders' id='sw-riders' aria-live='polite'></div>" +
    "</div>" +
    "<footer class='sw-foot'><div class='sw-est' id='sw-est' aria-live='polite'></div><span class='sw-spacer'></span>" +
    "<button class='btn btn-ghost' data-sw='close'>Cancel</button><button class='btn btn-primary' id='sw-start'>" + icon("play", { size: 13 }) + " Start sweep</button></footer>" +
    "</aside>";
  document.body.appendChild(el);
  S.el = el;
  el.addEventListener("click", (e) => {
    if (e.target === el) return closeSweepSheet();
    const b = e.target.closest("[data-sw], [data-goal], [data-rm]");
    if (!b) return;
    if (b.dataset.sw === "close") closeSweepSheet();
    else if (b.dataset.sw === "add") { if (S.axes.length < 3) { S.axes.push({ var: "", values: "" }); S.touched = true; paintAxes(); } }
    else if (b.dataset.goal) { S.touched = false; pickGoal(b.dataset.goal); }
    else if (b.dataset.rm != null) { S.axes.splice(+b.dataset.rm, 1); if (!S.axes.length) S.axes.push({ var: "", values: "" }); S.touched = true; paintAxes(); }
  });
  el.querySelectorAll("input[name='sw-mode']").forEach((r) => r.addEventListener("change", () => { S.mode = r.value; paintEst(); requestPlan(); }));
  el.querySelector("#sw-stop").addEventListener("change", (e) => { S.stop = e.target.checked; });
  el.querySelector("#sw-explore").addEventListener("change", () => paintEst());
  // The preview table: untick a point and it isn't run.
  el.querySelector("#sw-preview").addEventListener("change", (e) => {
    const cb = e.target.closest("input[data-pt]");
    if (!cb) return;
    if (cb.checked) S.drop.delete(cb.dataset.pt); else S.drop.add(cb.dataset.pt);
    paintEst();
  });
  el.querySelector("#sw-preview").addEventListener("click", (e) => {
    if (e.target.closest("[data-pt-all]")) { S.drop.clear(); paintEst(); }
  });
  el.querySelector("#sw-start").addEventListener("click", start);
}

function pickGoal(id) {
  S.goal = id;
  const g = GOALS.find((x) => x.id === id) || GOALS[0];
  S.axes = g.axes.map((a) => ({ ...a }));
  S.note = "";
  fillGoal(id);
  // "Fastest clock" runs every period: the headline is the fastest that met
  // timing, which a stop at the first clean run could never find.
  S.stop = false;
  S.el.querySelector("#sw-stop").checked = false;
  S.el.querySelector("#sw-stop-row").hidden = id === "clock";
  S.el.querySelector("#sw-explore-row").hidden = id !== "synth";
  S.el.querySelectorAll(".sw-goal").forEach((b) => { const on = b.dataset.goal === id; b.classList.toggle("is-on", on); b.setAttribute("aria-checked", on); });
  paintAxes();
}

// The goal's values, from this design's own settings (S.ctx, the server's
// reading of its config: a pdk:: section, a one-off setting, the default).
function fillGoal(id) {
  const c = (k) => S.ctx[k] || null;
  const src = (e) => !e ? "" : e.source === "override" ? "a one-off setting on Setup" : e.source === "config" ? (e.config_source || "the config")
    : e.source === "pdk" ? "the PDK" : "LibreLane's default";
  if (id === "clock") {
    const e = c("CLOCK_PERIOD");
    const cur = e && Number(asText(e.value));
    if (!(cur > 0)) { S.note = e ? "This design sets no clock period LanEx can read. Type the periods to try, in ns." : ""; return; }
    const hint = clockHint(cur);
    S.axes[0].values = clockValues(cur, hint).join(", ");
    S.note = "The clock now: " + cur + " ns (" + mhz(cur) + "), from " + src(e) + "." +
      (hint ? " " + hint.text : "") + " Every period runs; the summary names the fastest that met timing.";
    sdcGuard(c("PNR_SDC_FILE"), c("SIGNOFF_SDC_FILE"));
    return;
  }
  if (id === "area") {
    const sz = c("FP_SIZING"), die = c("DIE_AREA"), core = c("CORE_AREA");
    if (sz && String(asText(sz.value)).toLowerCase() === "absolute") {
      const vals = !blank(asText(core && core.value)) ? [] : dieValues(die && die.value);
      if (vals.length) {
        S.axes = [{ var: "DIE_AREA", values: vals.join(", ") }];
        S.note = "This design has a fixed die (FP_SIZING absolute, DIE_AREA " + asText(die.value) + " in " + src(die) +
          "), so FP_CORE_UTIL would change nothing: the sweep shrinks the die itself, to 100 %, 90 % and 80 % of each side.";
      }
      return;
    }
    const u = c("FP_CORE_UTIL");
    const cur = u && Number(asText(u.value));
    if (cur > 0 && cur < 90) {
      const vals = [cur, cur + 10, cur + 20].filter((x) => x <= 95).map((x) => +x.toFixed(1));
      S.axes[0].values = vals.join(", ");
      S.note = "The core is " + cur + " % full now (" + src(u) + "). Each point packs it fuller; the die shrinks with it.";
      // A density fixed in the config: the utilisation can pass it, and the
      // placer then can't fit the cells (LibreLane derives it when unset).
      const d = c("PL_TARGET_DENSITY_PCT");
      const dv = d && d.source === "config" ? Number(asText(d.value)) : NaN;
      if (dv > 0 && vals.some((x) => x + 5 > dv)) {
        S.note += " PL_TARGET_DENSITY_PCT is fixed at " + dv + " % in " + src(d) + ": past about " + Math.max(1, dv - 5) +
          " % utilisation the placer can't fit the cells into that density. Take it out of the config so LibreLane works it out from the utilisation, or sweep both under Your own settings.";
      }
    }
  }
}

// Whether an SDC sets the clock period itself: any create_clock whose
// -period is not LibreLane's CLOCK_PERIOD ($::env(CLOCK_PERIOD), directly, in
// an expr, or through a variable set from it). A number, or a variable the SDC
// sets to anything else, is the SDC's own clock. Pure.
// -> {sets: bool, value: "10" | "$clk_period" | ..., line}
export function sdcClockSource(text) {
  const src = String(text || "").replace(/\\\r?\n/g, " ");
  const lines = src.split(/\r?\n/).map((l) => l.replace(/(^|;)\s*#.*$/, "$1"));
  const usesEnv = (s) => /(::)?env\(\s*CLOCK_PERIOD\s*\)/.test(s);
  const sets = new Map();          // tcl variable -> its value text (the last set wins)
  for (const l of lines) {
    const m = /^\s*set\s+(?:::)?([A-Za-z_][\w]*)\s+(.+?)\s*;?\s*$/.exec(l);
    if (m) sets.set(m[1], m[2]);
  }
  const fromEnv = (tok, seen = new Set()) => {
    if (usesEnv(tok)) return true;
    const vars = [...tok.matchAll(/\$\{?(?:::)?([A-Za-z_]\w*)\}?/g)].map((x) => x[1]).filter((v) => v !== "env");
    return vars.some((v) => !seen.has(v) && sets.has(v) && (seen.add(v), fromEnv(sets.get(v), seen)));
  };
  for (const l of lines) {
    if (!/\bcreate_clock\b/.test(l)) continue;
    const m = /-period\s+(\[[^\]]*\]|\{[^}]*\}|"[^"]*"|\S+)/.exec(l);
    if (!m) continue;
    const tok = m[1].replace(/^[{"]|[}"]$/g, "");
    if (!fromEnv(tok)) return { sets: true, value: tok, line: l.trim() };
  }
  return { sets: false, value: null, line: "" };
}

// An SDC file that sets the clock period itself (a number, or anything but
// $::env(CLOCK_PERIOD)) makes a clock sweep change synthesis only, never the
// timing it is judged by: say so, and hold Start.
async function sdcGuard(...entries) {
  S.sdcBlock = "";
  for (const e of entries) {
    const v = e && asText(e.value);
    if (!v || !/\.sdc$/i.test(v)) continue;
    const path = v.replace(/^dir::/, (state.designDir || "").replace(/[\\/]+$/, "") + "/");
    let text = "";
    try { const r = await api.readText(path); text = (r && (r.text ?? (r.data || {}).text)) || ""; } catch (_e) { continue; }
    const clk = sdcClockSource(text);
    if (clk.sets) {
      const what = /^[0-9.]+$/.test(clk.value) ? clk.value + " ns" : "-period " + clk.value;
      S.sdcBlock = "Your SDC file (" + v.replace(/^dir::/, "") + ") sets the clock period itself (" + what + "), so changing CLOCK_PERIOD would only change synthesis, not timing. " +
        "Make the SDC use $::env(CLOCK_PERIOD) as LibreLane's own base.sdc does, or change the period in the SDC.";
      break;
    }
  }
  if (S.el && !S.el.hidden) paintEst();
}

// What a past run says about the clock: its slack at a period LanEx knows
// (a sweep point's own value, else the period the design uses now).
function clockHint(cur) {
  const runs = (state.runs || []).filter((r) => /passed|warning/.test((r.verdict && r.verdict.state) || (r.success ? "passed" : "")));
  for (const r of runs.slice().sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))) {
    const ws = r.key_metrics && r.key_metrics.timing__setup__ws;
    if (typeof ws !== "number" || !Number.isFinite(ws)) continue;
    const o = r.origin || {};
    const p = Number(o.kind === "sweep" && o.values && o.values.CLOCK_PERIOD != null ? o.values.CLOCK_PERIOD : cur);
    if (!(p > 0)) continue;
    const limit = p - ws;
    return { limit, text: "The last finished run, " + r.tag + ", had " + F.showText("timing__setup__ws", ws) + " of setup slack at " + p + " ns, so the limit looks near " + +limit.toFixed(2) + " ns." };
  }
  return null;
}
const mhz = (ns) => (ns > 0 ? localeNum(Number((1000 / ns).toPrecision(4)), "en-US", { maximumFractionDigits: 2 }) + " MHz" : "");

function paintAxes() {
  const host = S.el.querySelector("#sw-axes");
  host.innerHTML = S.axes.map((a, i) =>
    "<div class='sw-axis'><input class='sw-var' list='sw-vars' data-i='" + i + "' placeholder='Setting, e.g. FP_CORE_UTIL' value='" + esc(a.var) + "' aria-label='Setting " + (i + 1) + "' spellcheck='false'/>" +
    "<input class='sw-vals' data-i='" + i + "' placeholder='" + (a.var === "CLOCK_PERIOD" ? "e.g. 10, 9, 8" : "e.g. 35, 50, 65") + "' value='" + esc(a.values) + "' aria-label='Values for setting " + (i + 1) + "' aria-describedby='sw-info-" + i + "' spellcheck='false'/>" +
    "<span class='sw-n' data-n='" + i + "'></span>" +
    (S.axes.length > 1 ? "<button class='iconbtn iconbtn-sm' data-rm='" + i + "' aria-label='Remove this setting'>" + icon("x", { size: 13 }) + "</button>" : "") +
    "<div class='sw-info' id='sw-info-" + i + "' data-info='" + i + "'></div></div>").join("");
  host.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => {
    const a = S.axes[+inp.dataset.i];
    if (inp.classList.contains("sw-var")) a.var = inp.value.trim().toUpperCase(); else a.values = inp.value;
    S.touched = true;
    paintEst();
    requestPlan();
  }));
  S.el.querySelector("#sw-mode").hidden = S.axes.length < 2;
  S.el.querySelector("[data-sw='add']").hidden = S.axes.length >= 3;
  const note = S.el.querySelector("#sw-note");
  note.hidden = !S.note;
  note.textContent = S.note;
  paintEst();
  requestPlan();
}

// The setting's description, its default and its value in this design.
function infoHtml(name) {
  if (!name) return "";
  const e = ctxOf(name), m = varMeta(name);
  if (!e && !m) return "";
  const desc = String((e && e.description) || (m && m.description) || "").replace(/\s+/g, " ").trim();
  const first = desc.split(/(?<=\.)\s/)[0];
  const unit = (e && e.units) || (m && m.units) || "";
  const u = (v) => esc(asText(v)) + (unit ? " " + esc(unit) : "");
  const kind = kindOf(name), ch = choicesOf(name);
  const takes = kind === "int" ? "a whole number" : kind === "number" ? "a number" : kind === "bool" ? "true or false"
    : kind === "choice" && ch.length ? "one of " + ch.slice(0, 6).join(", ") + (ch.length > 6 ? ", …" : "") : kind === "numbers" ? "numbers like 0 0 200 200" : "";
  const def = e ? e.default : m && m.default;
  let now = "";
  if (e) {
    now = e.source === "override" ? u(e.value) + " <span class='muted'>(a one-off setting on Setup" + (e.config_source ? "; " + esc(e.config_source) + " says " + u(e.config) : "") + ")</span>"
      : e.source === "config" ? u(e.value) + " <span class='muted'>(" + esc(e.config_source || "the config") + ")</span>"
      : e.source === "pdk" ? "<span class='muted'>set by the PDK</span>"
      : (blank(asText(e.value)) ? "<span class='muted'>not set</span>" : u(e.value) + " <span class='muted'>(LibreLane's default)</span>");
  }
  return (first ? "<span class='sw-desc'>" + esc(first.length > 160 ? first.slice(0, 157) + "…" : first) + "</span>" : "") +
    "<span class='sw-facts'>" + (takes ? "<span>Takes " + esc(takes) + "</span>" : "") +
    (def != null && def !== "" ? "<span>Default " + u(def) + "</span>" : "") +
    (now ? "<span>In this design " + now + "</span>" : "") + "</span>";
}

function clientProblem() {
  const names = new Set((S.vars || []).map((v) => v.name));
  for (const a of S.axes) {
    if (!a.var) return "Pick a setting.";
    if (names.size && !names.has(a.var)) return a.var + " isn't a LibreLane setting.";
    const p = parseValuesStrict(a.values, kindOf(a.var), choicesOf(a.var));
    if (p.error) return a.var + ": " + p.error;
    if (!p.values.length) return "Give " + a.var + " some values.";
  }
  if (new Set(S.axes.map((a) => a.var)).size < S.axes.length) return "Each setting once.";
  const n = countRuns(S.axes, S.mode);
  if (n < 0) return "Paired settings need the same number of values.";
  if (n > MAX_RUNS) return n + " runs is too many (at most " + MAX_RUNS + "). Narrow it down.";
  return "";
}

function serverProblem() {
  const p = S.plan;
  if (!p || p._for !== planKey()) return "";
  if (p.value_problems && p.value_problems.length) return p.value_problems[0].message;
  return (p.problems || [])[0] || "";
}

function problems() {
  const clockSwept = S.axes.some((a) => a.var === "CLOCK_PERIOD");
  return clientProblem() || serverProblem() || (clockSwept && S.sdcBlock) || "";
}

function planKey() { return JSON.stringify([S.axes, S.mode]); }

// Ask the server what this sweep would do (debounced): value checks the
// browser cannot make, the fixed-die guard, the riders and each setting's
// value in this design.
function requestPlan() {
  clearTimeout(S.planTimer);
  S.planTimer = setTimeout(async () => {
    const key = planKey();
    const my = ++S.planSeq;
    const axes = S.axes.filter((a) => a.var).map((a) => ({ var: a.var, values: parseValuesStrict(a.values, kindOf(a.var), choicesOf(a.var)).values }));
    let p = null;
    try { p = await api.dsePlan({ axes, mode: S.axes.length > 1 ? S.mode : "grid", base_overrides: baseOverrides(), keys: CONTEXT_KEYS }); } catch (_e) { p = null; }
    if (my !== S.planSeq || !S.el || S.el.hidden) return;
    if (p) { p._for = key; S.plan = p; }
    paintEst();
  }, 220);
}

function paintEst() {
  S.axes.forEach((a, i) => {
    const el = S.el.querySelector("[data-n='" + i + "']");
    if (el) {
      const p = parseValuesStrict(a.values, kindOf(a.var), choicesOf(a.var));
      el.classList.toggle("is-bad", !!p.error);
      el.textContent = p.error ? "check" : p.values.length ? p.values.length + " value" + (p.values.length === 1 ? "" : "s") : "";
    }
    const info = S.el.querySelector("[data-info='" + i + "']");
    if (info) info.innerHTML = infoHtml(a.var);
  });
  const n0 = countRuns(S.axes, S.mode);
  const planned = n0 > 0 && n0 <= MAX_RUNS ? planPoints(S.axes, S.axes.length > 1 ? S.mode : "grid") : [];
  for (const k of [...S.drop]) if (!planned.some((pt) => pointKeyOf(pt) === k)) S.drop.delete(k);
  const n = planned.length ? planned.length - S.drop.size : n0;
  paintPreview(planned);
  const err = problems() || (planned.length && !n ? "Every planned point is unticked: tick at least one." : "");
  const secs = typicalRunSecs();
  const est = S.el.querySelector("#sw-est");
  const warns = (S.plan && S.plan._for === planKey() && S.plan.warnings) || [];
  // "6 runs, about 5 min, 2 at a time": the time from this design's past runs
  // and the queue's runs-at-once (never a made-up number with no history).
  const par = S.par && S.par.n > 0 ? Math.min(S.par.n, Math.max(1, n)) : 1;
  const total = secs ? Math.ceil(n / par) * secs * (par > 1 ? 1.15 : 1) : null;
  const clockDown = S.axes.some((a) => a.var === "CLOCK_PERIOD");
  if (err) est.innerHTML = "<span class='sw-err'>" + esc(err) + "</span>";
  else est.innerHTML = "<b>" + n + " run" + (n === 1 ? "" : "s") + (total ? ", about " + esc(fmtDur(total)) : "") + (S.par ? ", " + par + " at a time" : "") + "</b>" +
    (total ? " <span class='muted'>from this design's past runs" + (clockDown ? " (faster clocks can take longer)" : "") + "</span>"
      : " <span class='muted'>The time is known after the first point: this design has no finished run yet.</span>") +
    (S.par && S.par.why ? "<div class='muted'>" + esc(S.par.why) + "</div>" : "") +
    (n > 8 ? "<div class='sw-warn'>That's a lot of runs. Two or three values usually show the trend.</div>" : "") +
    warns.map((w) => "<div class='sw-warn'>" + esc(w) + "</div>").join("");
  paintRiders();
  const b = S.el.querySelector("#sw-start");
  const explore = exploring();
  if (explore) {
    est.innerHTML = "<b>1 exploration of 9 recipes</b> <span class='muted'>seconds to a few minutes: synthesis and timing before layout only</span>";
    b.innerHTML = icon("play", { size: 13 }) + " Explore 9 recipes";
    b.disabled = S.busy;
    b.title = "";
    return;
  }
  b.innerHTML = icon("play", { size: 13 }) + " Start sweep";
  // The queue takes a sweep while runs are going (lane 1): it waits its turn.
  b.disabled = !!err || S.busy;
  b.title = "";
}
const exploring = () => S.goal === "synth" && !!S.el && !!S.el.querySelector("#sw-explore")?.checked;

// Best synthesis recipe, step one: LibreLane's SynthesisExploration as one
// queue job (its recipes are read from the run folder: synthexplore.py).
async function startExploration() {
  S.busy = true;
  paintEst();
  try {
    const { collectRunPayload } = await import("./setup.js");
    const base = collectRunPayload();
    const d = new Date(), p = (x) => String(x).padStart(2, "0");
    const tag = "synthx-" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    // The exploration stops before layout, so the config's layout settings
    // (utilisation, pins, power grid) are not read: the config check's "this
    // flow never reads X" notes are expected here, never a reason to refuse.
    const spec = { ...base, flow: "SynthesisExploration", tag, frm: null, to: null, only: null, skip: [], checks_ack: true };
    const res = await api.queueAdd({ design_dir: state.designDir, spec, kind: "run", label: "Synthesis recipes (9)" });
    if (res && res.ok === false) throw new Error(res.reason || "the config check refused it");
    toast.show("Exploring LibreLane's 9 synthesis recipes as " + tag + ". Its table opens from the run in Runs when it ends.", "success", 8000);
    closeSweepSheet();
    document.dispatchEvent(new CustomEvent("g:sweep", { detail: { started: { run_tags: [tag] } } }));
  } catch (ex) {
    toast.show("The exploration didn't start: " + (ex.message || ex), "error", 0);
  } finally {
    S.busy = false;
    if (S.el) paintEst();
  }
}

// The planned points, one row each, in the order they will be handed to the
// queue: untick any that isn't worth a run.
function paintPreview(planned) {
  const host = S.el.querySelector("#sw-preview");
  if (!host) return;
  if (exploring() || planned.length < 2) { host.innerHTML = ""; return; }
  const axes = Object.keys(planned[0]);
  const ticked = planned.length - S.drop.size;
  host.innerHTML = "<div class='sw-lbl'>Planned points <span class='muted'>" + ticked + " of " + planned.length + " will run. Untick any that isn't worth a run.</span></div>" +
    "<div class='sw-prev-wrap'><table class='sw-res sw-prev'><thead><tr><th scope='col'><span class='sr-only'>Run it</span></th>" +
    axes.map((a) => "<th scope='col'>" + esc(a) + "</th>").join("") + "<th scope='col'></th></tr></thead><tbody>" +
    planned.map((pt) => {
      const k = pointKeyOf(pt), on = !S.drop.has(k);
      return "<tr class='" + (on ? "" : "is-off") + "'><td><input type='checkbox' data-pt='" + esc(k) + "'" + (on ? " checked" : "") +
        " aria-label='" + esc("Run " + axes.map((a) => a + " " + pt[a]).join(", ")) + "'/></td>" +
        axes.map((a) => "<td class='sw-res-v'>" + esc(pt[a]) + (a === "CLOCK_PERIOD" && Number(pt[a]) > 0 ? " <small>" + esc(mhz(Number(pt[a]))) + "</small>" : "") + "</td>").join("") +
        "<td class='muted'>" + (on ? "" : "unticked: won't run") + "</td></tr>";
    }).join("") + "</tbody></table></div>" +
    (S.drop.size ? "<button class='linklike' data-pt-all>Tick them all again</button>" : "");
}

// Setup's one-off settings go with every point: the sheet says so, by name.
function paintRiders() {
  const host = S.el.querySelector("#sw-riders");
  const riders = Object.entries((S.plan && S.plan.riders) || {});
  const cfg = (S.plan && S.plan.config) || "the config";
  host.innerHTML = riders.length
    ? "<div class='sw-lbl'>Also on every point</div><p>These one-off settings from Setup ride along on every run of the sweep, on top of " + esc(cfg) + ":</p>" +
      "<ul>" + riders.map(([k, v]) => "<li><code>" + esc(k) + " = " + esc(asText(v)) + "</code></li>").join("") + "</ul>" +
      "<p class='muted'>Clear them on Setup to sweep from the config alone.</p>"
    : "<p class='muted'>Each point is " + esc(cfg) + " with the values above, nothing else.</p>";
}

async function start() {
  if (exploring()) { if (!S.busy) await startExploration(); return; }
  if (problems() || S.busy) return;
  // The server still has the previous project active: a sweep now would run there.
  if (state.designMissing) {
    toast.show("Project folder not found: " + state.designMissing + ". Locate it or remove it from the list on Setup.", "warn");
    return;
  }
  const n = countRuns(S.axes, S.mode);
  if (!(await roomForRuns(n))) return;
  S.busy = true;
  paintEst();
  try {
    const { collectRunPayload } = await import("./setup.js");
    const base = collectRunPayload();
    const axes = S.axes.map((a) => ({ var: a.var, values: parseValuesStrict(a.values, kindOf(a.var), choicesOf(a.var)).values }));
    const r = await api.dseStart({
      axes, mode: S.axes.length > 1 ? S.mode : "grid", run_mode: state.runMode, flow_name: base.flow || null,
      base_overrides: base.overrides, sources: base.sources, extras: base.extras,
      stop_when_good: S.goal !== "clock" && S.stop, goal: S.goal,
      // Points unticked in the preview (their values); the server leaves them out.
      drop: planPoints(S.axes, S.axes.length > 1 ? S.mode : "grid").filter((pt) => S.drop.has(pointKeyOf(pt))),
    });
    const d = r && (r.data || r);
    toast.show("Sweep started: " + d.count + " run" + (d.count === 1 ? "" : "s") + ", " + (d.run_tags || []).join(", ") + ".", "success", 8000);
    closeSweepSheet();
    document.dispatchEvent(new CustomEvent("g:sweep", { detail: { started: d } }));
  } catch (ex) {
    toast.show("The sweep didn't start: " + (ex.message || ex), "error", 0);
  } finally {
    S.busy = false;
    if (S.el) paintEst();
  }
}

// Each point is a full flow: warn when the machine is short on memory.
async function roomForRuns(n) {
  let res = null;
  try { res = await api.systemResources(); res = res && (res.data || res); } catch (_e) { res = null; }
  if (res && res.ok && res.risk === "high") {
    return confirmDialog({ title: "Start " + n + " runs on a tight machine?", confirmText: "Start anyway",
      body: "This PC has " + (res.reasons || []).join("; ") + ". A run that runs out of memory can freeze Windows. Close other programs first." });
  }
  return true;
}

// ------------------------------------------------------------------ summary
// The numbers a sweep is read by: key, label, the metric (for the shared
// formatter), better direction.
const METRICS = [
  { id: "wns", key: "timing__setup__ws", label: "Setup WNS", short: "WNS", dir: 1 },
  { id: "tns", key: "timing__setup__tns", label: "Setup TNS", short: "TNS", dir: 1 },
  { id: "hold", key: "timing__hold__ws", label: "Hold WNS", short: "Hold", dir: 1 },
  { id: "die", key: "design__die__area", label: "Die area", short: "Die", dir: -1 },
  { id: "cells", key: "design__instance__area__stdcell", label: "Std-cell area", short: "Cells", dir: -1 },
  { id: "util", key: "design__instance__utilization", label: "Utilisation", short: "Util", dir: 0 },
  { id: "power", key: "power__total", label: "Power", short: "Power", dir: -1 },
  { id: "runtime", key: "", label: "Runtime", short: "Time", dir: -1 },
];
const M_BY = Object.fromEntries(METRICS.map((m) => [m.id, m]));
// The settings that size the die: a sweep of one of them is judged by the die.
const DIE_SIZERS = ["FP_CORE_UTIL", "DIE_AREA", "CORE_AREA", "FP_SIZING", "FP_ASPECT_RATIO"];
const _chartPick = new Map();     // sweep id -> the second chart's metric

function num(r, k) { const v = (r.key_metrics || {})[k]; return typeof v === "number" && Number.isFinite(v) ? v : null; }
function metricOf(p, id) { return id === "runtime" ? (typeof p.r.wall_time_s === "number" ? p.r.wall_time_s : null) : num(p.r, M_BY[id].key); }
function cellText(id, v, scale, unit = true) {
  if (v == null) return "";
  if (id === "runtime") return fmtDur(v);
  const s = id === "power" ? F.showAt("power__total", v, scale) : F.show(M_BY[id].key, v, { compact: true });
  return s.text + (unit && s.unit ? " " + s.unit : "");
}

// The points of a sweep in the order they were planned: one entry per run,
// plus each planned point that has no run (stopped, or LanEx closed first).
export function sweepPoints(sw, runs, verdictOf, jobs = null) {
  const tags = sw.tags || [];
  const reruns = sw.reruns || {};
  const byTag = new Map(runs.map((r) => [r.tag, r]));
  const idx = (t) => { const i = tags.indexOf(t); return i < 0 ? 1e9 : i; };
  const J = jobs || new Map();
  // The point's state in the study's words (studymodel.js): Queued, Running,
  // Clean, Warnings, Timing missed, Failed, Stopped, Skipped, Incomplete.
  const keyOf = (r, job) => SM.pointKey({ run: r ? { ...r, verdict: verdictOf(r) } : null, job, active: !!sw.active });
  const pts = runs.slice().sort((a, b) => idx(a.tag) - idx(b.tag) || String(a.started_at).localeCompare(String(b.started_at)))
    .map((r) => {
      const st = verdictOf(r).state;
      const key = keyOf(r, J.get(r.tag) || null);
      return { r, tag: r.tag, v: valuesOf(r, sw), state: st, key, ok: st === "passed", failed: key === "failed",
        finished: SM.hasNumbers(key), superseded: reruns[r.tag] && byTag.has(reruns[r.tag]) ? reruns[r.tag] : "" };
    });
  tags.forEach((t, i) => {
    if (byTag.has(t)) return;
    if (reruns[t] && byTag.has(reruns[t])) return;
    const key = keyOf(null, J.get(t) || null);
    pts.push({ r: null, tag: t, v: (sw.points || [])[i] || null, state: "notrun", key, ok: false, failed: false, finished: false, missing: true });
  });
  return pts;
}

// "Fastest that met timing": a finished run with no negative setup or hold slack.
const metTiming = (p) => p.r && p.finished && num(p.r, "timing__setup__ws") != null && num(p.r, "timing__setup__ws") >= 0 &&
  !(num(p.r, "timing__hold__ws") != null && num(p.r, "timing__hold__ws") < 0);

// Planned points with no run yet that still wait (or run) in the job queue:
// planned, not left. A study restarted with points queued offered "Run the N
// left" for them and queued each a second time.
const inQueue = (p) => p.key === "queued" || p.key === "running";
export function pointsLeft(pts) { return pts.filter((p) => p.missing && !inQueue(p)); }
export function pointsWaiting(pts) { return pts.filter((p) => p.missing && inQueue(p)); }

// Why a sweep has fewer runs than it planned, in plain words ("" when it
// ran them all or is still running).
export function endReason(sw, pts) {
  if (sw.active) return "";
  const live = pts.filter((p) => !p.missing && !p.superseded);
  const ran = live.length;
  const waiting = pointsWaiting(pts).length;
  if (waiting) return waiting + " point" + (waiting === 1 ? " still waits" : "s still wait") + " in the queue: " + (waiting === 1 ? "it runs" : "they run") + " by " + (waiting === 1 ? "itself" : "themselves") + " (the queue may be paused).";
  const left = pointsLeft(pts).length;
  const e = sw.ended || {};
  if (!left) {
    // Every point has a run, but LanEx stopped while one of them ran.
    const cut = live.filter((p) => p.state === "incomplete").length;
    return e.reason === "interrupted" && cut
      ? "LanEx was closed or restarted while it ran: " + cut + " point" + (cut === 1 ? "" : "s") + " ended before the last step." : "";
  }
  const of = ran + " of " + (ran + left) + " points ran";
  if (e.reason === "stopped") return "You stopped it: " + of + ".";
  if (e.reason === "first-clean") return "It stopped at the first clean run, as asked: " + of + ".";
  if (e.reason === "done") return of + ": the others' runs were deleted.";
  // The sweep's own stop rules (dse.py): a failure before any swept setting
  // is read (proof), or two of the first points failing the same way.
  if (e.reason === "failed-fast" && /^(the first point|two of the first)/.test(e.detail || "")) return "It stopped early: " + e.detail + ". " + of + ". Fix the cause, then run the ones left.";
  if (e.reason === "failed-fast") return "It stopped after two points failed the same way before any step ran (" + (e.detail || "the run would not start") + "): the rest would have too. " + of + ".";
  if (e.reason === "interrupted" && !e.assumed) return "LanEx was closed or restarted while it ran: " + of + ".";
  if (pts.some((p) => p.state === "stopped")) return "It was stopped: " + of + ".";
  // Recorded before LanEx noted why a sweep ended: "stop at the first clean run".
  if (sw.stop_when_good && pts.some((p) => p.ok)) return "It stopped at the first clean run, as asked: " + of + ".";
  return "LanEx was closed or restarted while it ran: " + of + ".";
}

// A sweep's summary, under its row: the headline (the fastest clock that met
// timing, or the best for timing and for area, each with "Use this
// configuration"), what failed and why, why it ended short, the results by
// value, the trend charts and its actions.
export function paintSweepSummary(el, sw, runs, { verdictOf, failCode, openRun, selectRuns } = {}) {
  const pts = sweepPoints(sw, runs, verdictOf, SQ.jobsOf(sw.id));
  // A point's words, once: the strip, the table, the charts and the CSV.
  for (const p of pts) {
    const w = SM.pointWords(p.key, { run: p.r ? { ...p.r, verdict: verdictOf(p.r) } : null, job: SQ.jobsOf(sw.id).get(p.tag) || null,
      stageLabel: FS.stageLabel, code: p.r && p.key === "failed" && failCode ? failCode(p.r) : "",
      reason: p.missing && !sw.active ? endReason(sw, pts) : "" });
    Object.assign(p, { word: w.word, detail: w.detail, label: p.v ? Object.values(p.v).join(" × ") : p.tag });
  }
  const live = pts.filter((p) => p.r && !p.superseded);
  const passed = live.filter((p) => p.ok);
  const axes = sw.axes || [];
  const one = axes.length === 1 ? axes[0].var : "";
  const scale = F.scaleOf("power__total", live.map((p) => metricOf(p, "power")));
  // The area that moves for the swept setting: the die for a die-sizing
  // sweep, else whichever of die and std-cell area moved more.
  const areaId = areaMetric(axes, passed);
  const tile = (label, p, text, extra = "") => !p ? "" :
    "<div class='sw-best'><div class='sw-best-k'>" + esc(label) + "</div><div class='sw-best-v'>" + text + "</div>" +
    "<div class='sw-best-m'><code>" + esc(p.tag) + "</code>" + extra + "</div>" +
    (p.v ? "<button class='btn btn-sm' data-use='" + esc(p.tag) + "' title='Review what it would change in the config first'>Use this configuration</button>" : "") + "</div>";
  let head = "";
  if (one === "CLOCK_PERIOD") {
    const met = live.filter(metTiming).filter((p) => p.v && Number(p.v.CLOCK_PERIOD) > 0).sort((a, b) => Number(a.v.CLOCK_PERIOD) - Number(b.v.CLOCK_PERIOD))[0];
    if (met) {
      const ns = Number(met.v.CLOCK_PERIOD);
      head = tile("Fastest that met timing", met, "<b>" + esc(ns) + " ns</b> <span class='muted'>(" + esc(mhz(ns)) + ")</span>",
        " · " + esc(F.showText("timing__setup__ws", num(met.r, "timing__setup__ws"))) + " slack");
    } else if (live.some((p) => p.finished)) {
      const slow = live.filter((p) => p.finished && p.v).sort((a, b) => Number(b.v.CLOCK_PERIOD) - Number(a.v.CLOCK_PERIOD))[0];
      head = "<div class='sw-best is-fail'><div class='sw-best-k'>None met timing</div><div class='sw-best-m'>" +
        (slow ? "The slowest tried, " + esc(slow.v.CLOCK_PERIOD) + " ns, has " + esc(F.showText("timing__setup__ws", num(slow.r, "timing__setup__ws"))) + " of setup slack. Try longer periods." : "") + "</div></div>";
    }
  } else {
    const bestT = passed.filter((p) => num(p.r, "timing__setup__ws") != null).sort((a, b) => num(b.r, "timing__setup__ws") - num(a.r, "timing__setup__ws"))[0];
    if (bestT) head = tile("Best for timing", bestT, "<b>" + esc(F.showText("timing__setup__ws", num(bestT.r, "timing__setup__ws"))) + "</b> <span class='muted'>setup WNS</span>", valsText(bestT.v));
  }
  // A "best" only when the number moved: every point with the same area has
  // no best for area.
  const areas = passed.map((p) => metricOf(p, areaId)).filter((v) => v != null);
  const moved = areas.length > 1 && Math.max(...areas) - Math.min(...areas) > Math.max(...areas.map(Math.abs)) * 1e-4;
  const bestA = !moved ? null : passed.filter((p) => metricOf(p, areaId) != null).sort((a, b) => metricOf(a, areaId) - metricOf(b, areaId))[0];
  const areaTile = bestA ? tile("Best for area", bestA, "<b>" + esc(cellText(areaId, metricOf(bestA, areaId), scale)) + "</b> <span class='muted'>" + esc(M_BY[areaId].label.toLowerCase()) + "</span>", valsText(bestA.v)) : "";
  const failed = live.filter((p) => p.failed);
  const failTile = failed.length
    ? "<div class='sw-best is-fail'><div class='sw-best-k'>" + failed.length + " failed</div><div class='sw-best-m'>" +
      failed.map((p) => "<code>" + esc(p.tag) + "</code>" + (failCode && failCode(p.r) ? " at " + esc(failCode(p.r)) : "")).join("<br>") + "</div></div>"
    : "";
  const why = endReason(sw, pts);
  const redo = live.filter((p) => !p.ok && !p.finished && p.state !== "running").map((p) => p.tag);
  const left = pointsLeft(pts).map((p) => p.tag);
  const busy = !!(state.status && state.status.running) || !!sw.active;
  const busyT = busy ? " disabled title='A run or sweep is going'" : "";
  // Compare takes up to twelve: past that, the reference (the best by the
  // sweep's goal) and the next eleven by the same goal, and the button says so.
  const toCompare = compareSet(live, one, areaId);
  const finishedN = live.length;
  const plots = chartsHtml(sw, pts);
  // The points at a glance: one mark per point in value order, how many ran.
  const ordered = byValue(pts.filter((p) => !p.superseded), sw.axes || []);
  const counts = {};
  for (const p of ordered) counts[p.key] = (counts[p.key] || 0) + 1;
  const progress = ordered.filter((p) => !["queued", "running", "skipped"].includes(p.key)).length + " of " + ordered.length +
    Object.entries(counts).filter(([k]) => k !== "clean").map(([k, n]) => " · " + n + " " + SM.POINT[k].word.toLowerCase()).join("");
  const same = sw.active ? SM.sameFailure(live.filter((p) => !p.missing).map((p) => ({ ...p, fp: p.key === "failed" ? SM.fingerprint((failCode && failCode(p.r)) || "", (verdictOf(p.r) || {}).stage || "") : "" }))) : null;
  const queued = SQ.queueAvailable() && ordered.some((p) => p.key === "queued" || p.key === "running");
  el.innerHTML = startedFromHtml(sw, runs, verdictOf, failCode) +
    "<div class='sw-pts'>" + SM.stripHtml(ordered, esc) + "<span class='sw-pts-t'>" + esc(progress) + "</span>" +
    (queued ? "<span class='rn-spacer'></span><button class='btn btn-ghost btn-sm' data-qstop='after' title='The points waiting are skipped; the ones running finish'>" + icon("stop", { size: 13 }) + " Stop after the running points</button>" +
      "<button class='btn btn-ghost btn-sm' data-qstop='now'>" + icon("stop", { size: 13 }) + " Stop now</button>" : "") + "</div>" +
    (same ? "<p class='sw-why'>" + icon("alert", { size: 14 }) + " Two of the first points failed the same way (" + esc(same.fingerprint.split("|").filter(Boolean).join(", ")) +
      "): the rest will likely fail too. Stop after the running points and fix the cause first.</p>" : "") +
    "<div class='sw-sum" + (plots ? "" : " no-plot") + "'><div class='sw-sum-l'>" +
    (why ? "<p class='sw-why'>" + icon("alert", { size: 14 }) + " " + esc(why) + "</p>" : "") +
    "<div class='sw-bests'>" + head + areaTile + failTile + "</div>" +
    (passed.length || one === "CLOCK_PERIOD" ? "" : "<p class='muted sw-none'>No point finished clean. Open a failed one for why, or fix the design before sweeping again.</p>") +
    "<div class='sw-sum-act'>" +
      "<button class='btn btn-ghost btn-sm' data-cmp" + (toCompare.length < 2 ? " disabled title='A sweep with one run has nothing to compare'" : "") +
        (finishedN > COMPARE_MAX ? " title='Compare takes up to " + COMPARE_MAX + " runs: the best by this sweep&#39;s goal, the best first'" : "") + ">" + icon("diff", { size: 13 }) + " " +
        (finishedN > COMPARE_MAX ? "Compare the " + COMPARE_MAX + " best of " + finishedN : finishedN > 2 ? "Compare all " + finishedN : "Compare these runs") + "</button>" +
      (selectRuns ? "<button class='btn btn-ghost btn-sm' data-sel-sweep>" + icon("check", { size: 13 }) + " Select its runs</button>" : "") +
      (left.length ? "<button class='btn btn-ghost btn-sm' data-redo='left'" + busyT + ">" + icon("play", { size: 13 }) + " Run the " + left.length + " left</button>" : "") +
      (redo.length ? "<button class='btn btn-ghost btn-sm' data-redo='failed'" + busyT + ">" + icon("play", { size: 13 }) + " " +
        (live.filter((p) => redo.includes(p.tag)).every((p) => p.failed) ? "Rerun " + redo.length + " failed" : "Run " + redo.length + " again") + "</button>" : "") +
      (one ? "<button class='btn btn-ghost btn-sm' data-add" + busyT + ">" + icon("plus", { size: 13 }) + " Add values</button>" : "") +
      "<button class='btn btn-ghost btn-sm' data-csv title='One row per point, for Excel or LibreOffice'>" + icon("download", { size: 13 }) + " CSV</button>" +
      "<button class='btn btn-ghost btn-sm' data-json title='The study as recorded, every point with its state, values and numbers'>" + icon("download", { size: 13 }) + " JSON</button>" +
    "</div></div>" + plots + "</div>" +
    resultsHtml(pts, sw, { scale, areaId, failCode });
  const ptOf = (tag) => pts.find((p) => p.tag === tag);
  el.querySelectorAll("[data-use]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); useConfig(ptOf(b.dataset.use), sw); }));
  el.querySelector("[data-cmp]").addEventListener("click", async (e) => {
    e.stopPropagation();
    const m = await import("./runs.js");
    m.compareRuns(toCompare);
  });
  el.querySelector("[data-sel-sweep]")?.addEventListener("click", (e) => { e.stopPropagation(); selectRuns(runs); });
  el.querySelector("[data-csv]").addEventListener("click", (e) => { e.stopPropagation(); exportSweepCsv(sw, pts, failCode); });
  el.querySelector("[data-json]").addEventListener("click", (e) => { e.stopPropagation(); exportSweepJson(sw, pts); });
  el.querySelectorAll("[data-redo]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    continueSweep(sw, { rerun: b.dataset.redo === "left" ? left : redo });
  }));
  el.querySelector("[data-add]")?.addEventListener("click", (e) => { e.stopPropagation(); addValues(sw); });
  el.querySelectorAll("tr[data-open]").forEach((tr) => tr.addEventListener("click", (e) => {
    e.stopPropagation();
    const p = ptOf(tr.dataset.open);
    if (p && p.r && openRun) openRun(p.r);
  }));
  el.querySelectorAll("[data-chart]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    _chartPick.set(sw.id, b.dataset.chart);
    paintSweepSummary(el, sw, runs, { verdictOf, failCode, openRun, selectRuns });
  }));
  el.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    _viewPick.set(sw.id, b.dataset.view);
    paintSweepSummary(el, sw, runs, { verdictOf, failCode, openRun, selectRuns });
  }));
  el.querySelectorAll("[data-qstop]").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const now = b.dataset.qstop === "now";
    if (now && !(await confirmDialog({ title: "Stop the sweep now?", danger: true, confirmText: "Stop now",
      body: "The points running now stop where they are, and the waiting ones are skipped. Finished points are kept. Resume runs the skipped ones later." }))) return;
    try {
      if (now) await SQ.stopNow(sw.id); else await SQ.stopAfterRunning(sw.id);
      toast.show(now ? "Stopped. Finished points are kept." : "The waiting points are skipped; the running ones finish.", "success", 4000);
    } catch (ex) { toast.show("Couldn't stop the sweep: " + (ex.message || ex), "error"); }
  }));
  el.querySelector("[data-from]")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const ctx = await import("./context.js");
    await ctx.setRun(e.currentTarget.dataset.from, state.designDir);
    import("./nav.js").then((m) => m.go("verify"));
  });
  drawCharts(el, sw, pts, scale);
}

// The run the study started from, pinned above it, with what a sweep can and
// can't do about its cause: a sweep is a tool, not a substitute for the cause
// (audit 6.2). The run before the sweep was created, that isn't one of its
// points (this design's runs only).
function startedFromHtml(sw, runs, verdictOf, failCode) {
  const t0 = sw.created_at ? Date.parse(sw.created_at) : NaN;
  if (!Number.isFinite(t0)) return "";
  const mine = new Set((sw.tags || []).concat(runs.map((r) => r.tag)));
  const before = (state.runs || []).filter((r) => !mine.has(r.tag) && r.started_at && Date.parse(r.started_at) < t0 && !(r.origin && r.origin.kind === "sweep"))
    .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0];
  if (!before) return "";
  const v = verdictOf(before);
  const m = before.key_metrics || {};
  const ws = m.timing__setup__ws, hs = m.timing__hold__ws;
  const fin = (x) => typeof x === "number" && Number.isFinite(x) && Math.abs(x) < 1e20;
  const code = v.state === "failed" && failCode ? failCode(before) : "";
  let cause = "", hint = "";
  if (fin(ws) && ws < 0) { cause = "Setup slack " + F.showText("timing__setup__ws", ws) + "."; hint = "A study finds the fastest clock that works; it doesn't shorten the path that misses."; }
  else if (fin(hs) && hs < 0) { cause = "Hold slack " + F.showText("timing__hold__ws", hs) + "."; hint = "A hold miss is fixed by buffering, not by a slower clock."; }
  else if (/GRT/.test(code)) { cause = "Routing was too congested (" + code + ")."; hint = "A study can try a lower utilisation; if every value fails, the design needs fewer cells or more metal layers."; }
  else if (/DPL|GPL/.test(code)) { cause = "Placement failed (" + code + ")."; hint = "A bigger die or a lower utilisation often fixes this; if not, look at the macro placement."; }
  else if (v.state === "failed") { cause = code && !String(v.label || "").includes(code) ? "(" + code + ")" : ""; hint = "Fix the cause first: a study can't fix a design that doesn't build."; }
  const tone = { passed: "pass", warning: "warn", failed: "fail" }[v.state] || "warn";
  return "<div class='sw-from is-" + tone + "'><span class='sw-from-k'>Started from</span><code>" + esc(before.tag) + "</code>" +
    "<span class='rn-v is-" + tone + "'><i aria-hidden='true'></i>" + esc(v.label || v.state) + "</span>" +
    (cause ? "<span class='sw-from-c'>" + esc(cause) + "</span>" : fin(ws) ? "<span class='sw-from-c'>" + esc(F.showText("timing__setup__ws", ws)) + " setup slack</span>" : "") +
    (hint ? "<span class='sw-from-h'>" + esc(hint) + "</span>" : "") +
    (cause || hint ? "<button class='btn btn-ghost btn-sm' data-from='" + esc(before.tag) + "'>" + icon("check", { size: 13 }) + " Open it in Verify</button>" : "") + "</div>";
}

// Which runs "Compare" sends: all of them up to twelve; past that the best by
// the sweep's goal (the fastest clock that met timing first for a clock
// sweep, the smallest area for a sizing one, else the most slack).
function compareSet(live, one, areaId) {
  const runs = live.filter((p) => p.r);
  if (runs.length <= COMPARE_MAX) return runs.map((p) => p.r);
  const ws = (p) => num(p.r, "timing__setup__ws");
  const score = one === "CLOCK_PERIOD"
    ? (p) => (SM.metTiming(p.key, p.r) ? 1e6 - Number(p.v && p.v.CLOCK_PERIOD) : p.finished ? (ws(p) ?? -1e9) : -1e12)
    : DIE_SIZERS.includes(one)
      ? (p) => (p.finished ? -(metricOf(p, areaId) ?? 1e12) : -1e15)
      : (p) => (p.finished ? (ws(p) ?? -1e9) : -1e12);
  return runs.slice().sort((a, b) => score(b) - score(a)).slice(0, COMPARE_MAX).map((p) => p.r);
}

// ------------------------------------------------------------------ charts (ECharts, studycharts.js)
const _viewPick = new Map();      // sweep id -> "value" | "trade" | "heat"
const _sumCharts = new WeakMap(); // summary element -> its chart instances
function chartViews(sw) {
  const axes = sw.axes || [];
  const nums = axes.filter((a) => (a.values || []).length > 1 && (a.values || []).every((v) => Number.isFinite(Number(v))));
  const out = [];
  if (axes.length === 1 && nums.length === 1) out.push(["value", "By value"]);
  if (axes.length === 2) out.push(["heat", "Heatmap"]);
  out.push(["trade", "Trade-off"]);
  return out;
}
function chartsHtml(sw, pts) {
  if (pts.filter((p) => p.r).length < 2 && !pts.some((p) => p.key === "queued")) return "";
  const views = chartViews(sw);
  const view = views.some(([v]) => v === _viewPick.get(sw.id)) ? _viewPick.get(sw.id) : views[0][0];
  const one = (sw.axes || []).length === 1 ? sw.axes[0].var : "";
  const pick = _chartPick.get(sw.id) || (view === "value" ? "wns" : view === "heat" ? "wns" : "die");
  const metricSeg = "<div class='rn-seg sw-mseg' role='group' aria-label='The chart shows'>" +
    ["wns", "tns", "hold", "die", "cells", "util", "power", "runtime"].map((id) =>
      "<button class='rn-segb" + (id === pick ? " is-on" : "") + "' data-chart='" + id + "' aria-pressed='" + (id === pick) + "' title='" + esc(M_BY[id].label + (view === "trade" ? " against setup slack" : one ? " against " + one : "")) + "'>" + esc(M_BY[id].short) + "</button>").join("") + "</div>";
  const viewSeg = views.length > 1 ? "<div class='rn-seg sw-mseg' role='group' aria-label='Chart'>" + views.map(([v, l]) =>
    "<button class='rn-segb" + (v === view ? " is-on" : "") + "' data-view='" + v + "' aria-pressed='" + (v === view) + "'>" + esc(l) + "</button>").join("") + "</div>" : "";
  return "<div class='sw-plots-wrap'><div class='sw-chart-bar'>" + viewSeg + metricSeg + "</div>" +
    "<figure class='sw-plot sw-echart'><figcaption></figcaption><div class='sw-chart' role='img'></div><p class='sw-chart-note muted'></p></figure></div>";
}
async function drawCharts(el, sw, pts, scale) {
  for (const c of _sumCharts.get(el) || []) { try { c.dispose(); } catch (_e) { /* gone */ } }
  _sumCharts.set(el, []);
  const host = el.querySelector(".sw-chart");
  if (!host) return;
  if (typeof window.echarts === "undefined") await import("./loading.js").then((m) => m.loadECharts()).catch(() => null);
  if (!host.isConnected || typeof window.echarts === "undefined") return;
  const SC = await import("./studycharts.js");
  const views = chartViews(sw);
  const view = views.some(([v]) => v === _viewPick.get(sw.id)) ? _viewPick.get(sw.id) : views[0][0];
  const axes = sw.axes || [];
  const pick = _chartPick.get(sw.id) || (view === "trade" ? "die" : "wns");
  const m = M_BY[pick];
  const k = pick === "power" && scale ? 1 / scale.div : pick === "util" ? 100 : 1;
  const unit = pick === "runtime" ? "s" : pick === "power" ? (scale ? scale.unit : "W") : pick === "util" ? "%" : F.show(m.key, 1).unit;
  const yOf = (p) => { const v = p.r ? metricOf(p, pick) : null; return v == null ? null : v * k; };
  const live = pts.filter((p) => !p.superseded);
  const cap = el.querySelector(".sw-echart figcaption"), note = el.querySelector(".sw-chart-note");
  let opt = null;
  if (view === "value") {
    const xv = axes[0].var;
    opt = SC.byValueOption({ xvar: xv, xunit: xv === "CLOCK_PERIOD" ? "ns" : "", clock: xv === "CLOCK_PERIOD",
      metric: { key: m.key, label: m.label, unit, dir: m.dir },
      points: live.map((p) => ({ x: p.v ? Number(p.v[xv]) : NaN, y: yOf(p), key: p.key, tag: p.tag, word: p.word, detail: p.detail })) });
    cap.innerHTML = "<b>" + esc(m.label) + "</b>" + (unit ? " (" + esc(unit) + ")" : "") + " by " + esc(xv);
  } else if (view === "heat") {
    const [ax, ay] = axes;
    const sortV = (vs) => vs.slice().sort((a, b) => (Number.isFinite(+a) && Number.isFinite(+b) ? +a - +b : String(a).localeCompare(String(b))));
    opt = SC.heatmapOption({ xvar: ax.var, yvar: ay.var, xs: sortV(ax.values || []), ys: sortV(ay.values || []),
      metric: { key: m.key, label: m.label, unit, dir: m.dir },
      points: live.map((p) => ({ xv: p.v ? p.v[ax.var] : null, yv: p.v ? p.v[ay.var] : null, y: yOf(p), key: p.key, tag: p.tag, word: p.word, detail: p.detail })) });
    cap.innerHTML = "<b>" + esc(m.label) + "</b>" + (unit ? " (" + esc(unit) + ")" : "") + " for each " + esc(ax.var) + " × " + esc(ay.var);
  } else {
    const xm = M_BY[pick === "wns" ? "die" : pick];
    const xk = xm.id === "power" && scale ? 1 / scale.div : xm.id === "util" ? 100 : 1;
    const xu = xm.id === "runtime" ? "s" : xm.id === "power" ? (scale ? scale.unit : "W") : xm.id === "util" ? "%" : F.show(xm.key, 1).unit;
    const wsU = F.show("timing__setup__ws", 1).unit;
    opt = SC.paretoOption({ x: { label: xm.label, unit: xu, dir: xm.dir || -1 }, y: { label: "Setup WNS", unit: wsU, dir: 1 },
      points: live.map((p) => ({ x: p.r ? (metricOf(p, xm.id) == null ? null : metricOf(p, xm.id) * xk) : null, y: p.r ? metricOf(p, "wns") : null,
        key: p.key, tag: p.tag, label: p.v ? Object.values(p.v).join("/") : p.tag })) });
    cap.innerHTML = "<b>Trade-off</b>: setup WNS against " + esc(xm.label.toLowerCase()) + ". The line joins the points nothing beats on both.";
  }
  const fails = live.filter((p) => p.key === "failed" || p.key === "stopped" || p.key === "interrupted");
  note.textContent = view === "trade" && fails.length ? fails.length + " point" + (fails.length === 1 ? "" : "s") + " failed and have no numbers, so they aren't on this chart: " + fails.map((p) => p.label).join(", ") + "."
    : view === "value" && fails.length ? "A cross on the axis is a point that failed (no numbers)." : "";
  if (!opt) { host.closest("figure").hidden = true; return; }
  host.style.height = (view === "heat" ? Math.max(220, 46 * ((axes[1] || {}).values || []).length + 70) : 250) + "px";
  host.setAttribute("aria-label", cap.textContent);
  const chart = window.echarts.init(host, null, { renderer: "canvas" });
  chart.setOption(opt);
  // A point on the chart opens its run (the same as its row in the table).
  chart.on("click", (e) => { const p = e.data && e.data.p; const row = p && el.querySelector("tr[data-open='" + CSS.escape(p.tag) + "']"); if (row) row.click(); });
  _sumCharts.get(el).push(chart);
  if (typeof ResizeObserver !== "undefined") {
    let t = 0;
    const ro = new ResizeObserver(() => { clearTimeout(t); t = setTimeout(() => { if (!chart.isDisposed()) chart.resize(); else ro.disconnect(); }, 80); });
    ro.observe(host);
  }
}

const valsText = (v) => (v ? " · " + esc(Object.entries(v).map(([k, x]) => k + " " + x).join(", ")) : "");

function areaMetric(axes, passed) {
  if (axes.some((a) => DIE_SIZERS.includes(a.var))) return "die";
  const spread = (id) => {
    const xs = passed.map((p) => metricOf(p, id)).filter((v) => v != null);
    if (xs.length < 2) return 0;
    const lo = Math.min(...xs), hi = Math.max(...xs);
    return hi > 0 ? (hi - lo) / hi : 0;
  };
  return spread("die") > spread("cells") ? "die" : "cells";
}

// Points sorted by the swept value (numbers as numbers), then by run order.
function byValue(pts, axes) {
  const key = (p) => axes.map((a) => (p.v ? p.v[a.var] : undefined));
  return pts.slice().sort((a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) {
      const x = ka[i], y = kb[i];
      if (x === y) continue;
      if (x === undefined) return 1;
      if (y === undefined) return -1;
      const nx = Number(x), ny = Number(y);
      const c = Number.isFinite(nx) && Number.isFinite(ny) ? nx - ny : String(x).localeCompare(String(y));
      if (c) return c;
    }
    return 0;
  });
}

const STATE_WORD = { passed: "Clean", warning: "Warnings", failed: "Failed", stopped: "Stopped", incomplete: "Incomplete", running: "Running", notrun: "Not run" };
const STATE_TONE = { passed: "pass", warning: "warn", failed: "fail", stopped: "warn", incomplete: "warn", running: "live", notrun: "warn" };

// The table a sweep exists for: the swept value(s) against how each point
// came out and its key numbers, sorted by value.
function resultsHtml(pts, sw, { scale, areaId, failCode }) {
  const axes = sw.axes || [];
  if (!pts.length || !axes.length) return "";
  const cols = ["wns", "hold", areaId === "die" ? "die" : "cells", areaId === "die" ? "cells" : "die", "util", "power", "runtime"];
  const rows = byValue(pts, axes).map((p) => {
    const P = SM.POINT[p.key] || {};
    const word = p.word || P.word || STATE_WORD[p.state] || p.state;
    const tone = P.tone === "muted" ? "none" : P.tone || STATE_TONE[p.state] || "warn";
    const sup = p.superseded ? "<span class='muted'> · rerun as " + esc(p.superseded) + "</span>" : "";
    return "<tr" + (p.r ? " data-open='" + esc(p.tag) + "' class='sw-res-row" + (p.superseded ? " is-old" : "") + "' title='" + esc("Open " + p.tag) + "'" : " class='is-missing'") + ">" +
      axes.map((a) => "<td class='sw-res-v'>" + esc(p.v ? p.v[a.var] : "") + (a.var === "CLOCK_PERIOD" && p.v && Number(p.v[a.var]) > 0 ? " <small>" + esc(mhz(Number(p.v[a.var]))) + "</small>" : "") + "</td>").join("") +
      "<td><span class='rn-v is-" + tone + "'><i aria-hidden='true'></i>" + esc(word) + "</span>" +
        (p.detail ? " <span class='muted'>" + esc(p.detail) + "</span>" : "") + sup + "</td>" +
      cols.map((id) => {
        const v = p.r ? metricOf(p, id) : null;
        const neg = (id === "wns" || id === "hold") && v != null && v < 0 ? " is-neg" : "";
        return "<td class='sw-num" + neg + "'" + (v != null && id !== "runtime" ? fmt.titleAttr(v) : "") + ">" + esc(cellText(id, v, scale, false)) + "</td>";
      }).join("") +
      "<td class='sw-res-tag'>" + esc(p.tag) + "</td></tr>";
  }).join("");
  const unit = (id) => id === "runtime" ? "" : id === "power" ? (scale ? scale.unit : "") : F.show(M_BY[id].key, 1).unit;
  return "<div class='sw-res-wrap'><table class='sw-res'><caption>Results by " + esc(axes.map((a) => a.var).join(" × ")) + "</caption><thead><tr>" +
    axes.map((a) => "<th scope='col'>" + esc(a.var) + "</th>").join("") + "<th scope='col'>Result</th>" +
    cols.map((id) => "<th scope='col' class='sw-num'>" + esc(M_BY[id].label) + (unit(id) ? " <small>" + esc(unit(id)) + "</small>" : "") + "</th>").join("") +
    "<th scope='col'>Run</th></tr></thead><tbody>" + rows + "</tbody></table></div>";
}

// The sweep's results as a CSV: the swept values, how each point ended and
// every key number, exact (as LibreLane wrote it), in sweep order.
// The study as JSON: its plan (axes, mode, goal, how it ended) and every
// point with its values, state words, run and LibreLane's raw numbers.
function exportSweepJson(sw, pts) {
  const doc = {
    format: "lanex-study/1", exported_at: new Date().toISOString(), design: state.designDir || "",
    study: { id: sw.id, created_at: sw.created_at, axes: sw.axes, mode: sw.mode, goal: sw.goal || null, count: sw.count, ended: sw.ended || null, reruns: sw.reruns || {} },
    points: byValue(pts, sw.axes || []).map((p) => ({
      values: p.v || null, run: p.r ? p.tag : null, state: p.key, words: p.word || "", detail: p.detail || "", superseded_by: p.superseded || null,
      run_dir: p.r ? p.r.run_dir : null, metrics: p.r ? p.r.key_metrics || {} : {}, wall_time_s: p.r ? p.r.wall_time_s : null,
    })),
  };
  import("./csvutil.js").then((m) => m.downloadText("study-" + (sw.id || "results") + ".json", JSON.stringify(doc, null, 1)));
}

function exportSweepCsv(sw, pts, failCode) {
  const axes = sw.axes || [];
  const head = [...axes.map((a) => a.var), "run", "result", "reason", ...METRICS.map((m) => m.key ? m.key + " (" + (F.unitOf(m.key) === "ratio" ? "fraction" : F.unitOf(m.key)) + ")" : "runtime (s)")];
  const rows = [head, ...byValue(pts, axes).map((p) => [
    ...axes.map((a) => (p.v ? p.v[a.var] : "")), p.tag, p.word || STATE_WORD[p.state] || p.state,
    p.superseded ? "rerun as " + p.superseded : p.detail || (p.r && p.failed ? (failCode && failCode(p.r)) || "" : ""),
    ...METRICS.map((m) => (p.r ? metricOf(p, m.id) ?? "" : "")),
  ])];
  downloadCsv("sweep-" + (sw.id || "results") + ".csv", toCsv(rows, { excel: true }));
}

function valuesOf(r, sw) {
  const o = r.origin || {};
  if (o.kind === "sweep" && o.values && Object.keys(o.values).length) return o.values;
  const i = (sw.tags || []).indexOf(r.tag);
  if (sw.points && sw.points[i]) return sw.points[i];
  if (i >= 0 && (sw.axes || []).length === 1) return { [sw.axes[0].var]: sw.axes[0].values[i] };
  return null;
}

// ------------------------------------------------------------------ continue a sweep
// Run its failed (or never-run) points again, or more values of its one
// setting: the same sweep, with the settings it started with.
async function continueSweep(sw, { rerun = null, values = null }) {
  const n = rerun ? rerun.length : values.length;
  if (!(await roomForRuns(n))) return;
  try {
    const r = await api.dseStart({ continue: { sweep_id: sw.id, ...(rerun ? { rerun } : { values }) } });
    const d = r && (r.data || r);
    toast.show("Sweep continued: " + d.count + " more run" + (d.count === 1 ? "" : "s") + ", " + (d.run_tags || []).join(", ") + ".", "success", 8000);
    document.dispatchEvent(new CustomEvent("g:sweep", { detail: { started: d } }));
  } catch (ex) {
    toast.show("The sweep didn't continue: " + (ex.message || ex), "error", 0);
  }
}

async function addValues(sw) {
  const ax = (sw.axes || [])[0];
  if (!ax) return;
  const have = new Set((ax.values || []).map(String));
  let input = null, out = null;
  const fresh = () => {
    const p = parseValuesStrict(input.value, kindOf(ax.var), choicesOf(ax.var));
    const nv = p.values.filter((v) => !have.has(v));
    return { p, nv };
  };
  const v = await customDialog({
    title: "Add values to the sweep of " + ax.var,
    bodyHtml: "<label class='dlg-label' for='sw-add-in'>More values of <code>" + esc(ax.var) + "</code>. It has tried " + esc([...have].join(", ")) + ".</label>" +
      "<input class='dlg-input' id='sw-add-in' type='text' spellcheck='false' autocomplete='off' placeholder='e.g. 7, 6.5'/>" +
      "<p class='sw-add-out' id='sw-add-out' aria-live='polite'></p>",
    buttons: [{ label: "Cancel", value: null, cls: "btn-ghost" }, { label: "Run them", value: "go", cls: "btn-primary" }],
    onMount: (back, close) => {
      input = back.querySelector("#sw-add-in");
      out = back.querySelector("#sw-add-out");
      const go = back.querySelector(".dlg-actions .btn-primary");
      const paint = () => {
        const { p, nv } = fresh();
        go.disabled = !!p.error || !nv.length;
        out.innerHTML = p.error ? "<span class='sw-err'>" + esc(p.error) + "</span>" : nv.length
          ? esc(nv.length + " new run" + (nv.length === 1 ? "" : "s") + (typicalRunSecs() ? ", about " + fmtDur(nv.length * typicalRunSecs()) : "")) +
            (p.values.length > nv.length ? " <span class='muted'>(" + esc(p.values.filter((x) => have.has(x)).join(", ")) + " already ran)</span>" : "")
          : p.values.length ? "<span class='muted'>Those values already ran.</span>" : "";
      };
      input.addEventListener("input", paint);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !go.disabled) { e.preventDefault(); close("go"); } });
      paint();
    },
  });
  if (v !== "go" || !input) return;
  const { p, nv } = fresh();
  if (p.error || !nv.length) return;
  continueSweep(sw, { values: nv });
}

// ------------------------------------------------------------------ use a point's settings

// A value as the setting's own type (`kind` as LanEx reads LibreLane's
// variables), for writing into a config file: a sweep's values are text, and
// "6" written as text is a quoted string in YAML or JSON. A value that does
// not read as its kind stays as it is. Pure (tested).
export function typedValue(v, kind) {
  if (typeof v !== "string") return v;
  const s = v.trim();
  if (kind === "int" || kind === "number") {
    const n = Number(s);
    return s !== "" && Number.isFinite(n) ? n : v;
  }
  if (kind === "bool") {
    if (/^(true|yes|on|1)$/i.test(s)) return true;
    if (/^(false|no|off|0)$/i.test(s)) return false;
    return v;
  }
  if (kind === "numbers") {
    const parts = s.replace(/^[[(]\s*|\s*[\])]$/g, "").split(/[\s,]+/).filter(Boolean);
    const ns = parts.map(Number);
    return parts.length && ns.every(Number.isFinite) ? ns : v;
  }
  return v;
}

// The keys of `set` that the config file, after the write, still sets to
// another value for this PDK (`effective`, as /api/config/set answers it).
// Pure (tested).
export function stillWins(set, effective, same) {
  const out = [];
  for (const k of Object.keys(set || {})) {
    const e = (effective || {})[k];
    if (e && !same(e.value, set[k])) out.push(k);
  }
  return out;
}
// Write what a point ran with into the design's config: the sweep's one-off
// settings plus the point's values, only where they differ from the config,
// shown as a diff first. A setting that a pdk:: section of the config sets for
// this PDK is changed in that section (the top level would not take effect).
// Undo puts the config back.
async function useConfig(p, sw) {
  if (!p || !p.v) return;
  const base = sw.base_overrides || {};
  const ran = {};
  for (const [k, v] of Object.entries(base)) if (k !== "PDK" && k !== "STD_CELL_LIBRARY" && !blank(v)) ran[k] = v;
  Object.assign(ran, p.v);
  // The run's own PDK, never the one picked on Setup now.
  const pdk = base.PDK || (p.r && p.r.pdk) || null, scl = base.STD_CELL_LIBRARY || (p.r && p.r.scl) || null;
  let ctx = {}, cfg = "the config";
  try {
    const plan = await api.dsePlan({ axes: [], keys: Object.keys(ran), base_overrides: { ...(pdk ? { PDK: pdk } : {}), ...(scl ? { STD_CELL_LIBRARY: scl } : {}) } });
    ctx = (plan && plan.context) || {};
    cfg = (plan && plan.config) || cfg;
  } catch (ex) { toast.show("Couldn't read the config: " + (ex.message || ex), "error"); return; }
  const same = (a, b) => { const x = asText(a).trim(), y = asText(b).trim(); return x === y || (x !== "" && y !== "" && Number.isFinite(Number(x)) && Number(x) === Number(y)); };
  const rows = Object.entries(ran).map(([k, raw]) => {
    const e = ctx[k] || {};
    // A point's values are text ("6"): the config gets the setting's own type.
    const to = typedValue(raw, e.kind || kindOf(k));
    const scoped = (e.scoped || []).filter((s) => s.applies).pop() || null;
    const from = e.config_source ? e.config : null;
    return { k, to, from, def: e.default, scoped, rider: !(k in p.v), same: e.config_source ? same(e.config, to) : false };
  });
  const change = rows.filter((r) => !r.same);
  if (!change.length) {
    toast.show("The config already has " + p.tag + "'s settings (" + rows.map((r) => r.k + " = " + asText(r.to)).join(", ") + ").", "info", 12000,
      p.r ? { label: "Run it once to check", run: () => checkRun(p) } : null);
    return;
  }
  const v = await customDialog({
    title: "Use " + p.tag + "'s settings?",
    bodyHtml: "<p>This writes what " + esc(p.tag) + " ran with into the config, so a plain Run builds it again:</p>" +
      "<table class='sw-diff'><thead><tr><th>Setting</th><th>Now</th><th></th><th>After</th></tr></thead><tbody>" +
      change.map((r) => "<tr><td><code>" + esc(r.k) + "</code>" + (r.rider ? " <span class='muted'>one-off setting it ran with</span>" : "") +
        (r.scoped ? " <span class='muted'>in " + esc(r.scoped.scope) + "</span>" : "") + "</td>" +
        "<td class='sw-diff-from'>" + (r.from != null ? esc(asText(r.from)) : "<span class='muted'>unset" + (r.def != null && r.def !== "" ? " (default " + esc(asText(r.def)) + ")" : "") + "</span>") + "</td>" +
        "<td aria-hidden='true'>→</td><td class='sw-diff-to'>" + esc(asText(r.to)) + "</td></tr>").join("") + "</tbody></table>" +
      (rows.length > change.length ? "<p class='muted'>Already in the config: " + esc(rows.filter((r) => r.same).map((r) => r.k + " = " + asText(r.to)).join(", ")) + ".</p>" : "") +
      "<p class='muted'>You can undo it from the message that follows.</p>",
    buttons: [{ label: "Cancel", value: null, cls: "btn-ghost" }, { label: "Save to the config", value: "go", cls: "btn-primary" }],
  });
  if (v !== "go") return;
  // One write per place: the top level, then each pdk::/scl:: section.
  const groups = new Map();
  for (const r of change) {
    const g = r.scoped ? r.scoped.scope : "";
    if (!groups.has(g)) groups.set(g, {});
    groups.get(g)[r.k] = r.to;
  }
  const undos = [], said = [], stuck = [];
  try {
    for (const [scope, set] of groups) {
      const extra = { pdk, scl };
      if (scope) extra.scope = scope.split(" / ");
      const r = await api.configSet(set, [], extra);
      const d = (r && (r.data || r)) || {};
      if (d.undo && d.undo.backup) undos.push(d.undo);
      said.push(Object.entries(set).map(([k, x]) => k + " = " + asText(x)).join(", "));
      // A section that still sets the key for this PDK, to another value,
      // wins over what was written. `effective` is the file after the write
      // (`scoped` is the file before it, so it still has the old value).
      stuck.push(...stillWins(set, d.effective, same));
    }
  } catch (ex) {
    toast.show("Couldn't save: " + (ex.message || ex), "error", 0);
    if (!undos.length) return;
  }
  document.dispatchEvent(new CustomEvent("g:config_changed", { detail: { keys: change.map((r) => r.k), from: "sweep" } }));
  // A one-off setting on Setup for the same key still wins over the config.
  const ov = state.varsValues || {};
  const shadow = change.filter((r) => !blank(ov[r.k]) && !same(ov[r.k], r.to)).map((r) => r.k + " = " + asText(ov[r.k]));
  const msg = "Saved in " + cfg + ": " + said.join("; ") + "." +
    (stuck.length ? " A pdk:: section of the config still sets " + stuck.join(", ") + " for this PDK: change it there." : "") +
    (shadow.length ? " Setup still has the one-off " + shadow.join(", ") + ", which wins over the config: clear it there." : "");
  const acts = [];
  if (undos.length) acts.push({ label: "Undo", run: async () => {
    try {
      for (const u of undos.slice().reverse()) await api.configUndo({ backup: u.backup, after: u.after });
      toast.show("The config is back as it was.", "success");
      document.dispatchEvent(new CustomEvent("g:config_changed", { detail: { keys: change.map((r) => r.k), from: "sweep-undo" } }));
    } catch (ex) { toast.show("Undo: " + (ex.message || ex), "warn", 9000); }
  } });
  // The winner must reproduce (ORFS AutoTuner's best config famously did
  // not): one plain run from the saved config, its numbers held against the
  // point's when it ends.
  if (p.r && !stuck.length && !shadow.length) acts.push({ label: "Run it once to check", run: () => checkRun(p) });
  toast.show(msg, stuck.length || shadow.length ? "warn" : "success", 14000, acts.length ? acts : null);
}

// A plain run of the config as saved, through the queue; when it ends, its
// numbers against the point it should reproduce: "Same numbers as
// sweep-clk-8" or what differs, with Compare one press away.
const CHECK_KEYS = [["timing__setup__ws", "setup slack"], ["timing__hold__ws", "hold slack"], ["design__die__area", "die"],
  ["design__instance__area__stdcell", "std-cell area"], ["power__total", "power"]];
export async function checkRun(p) {
  let job;
  try {
    const { collectRunPayload } = await import("./setup.js");
    const base = collectRunPayload();
    const d = new Date(), pad2 = (x) => String(x).padStart(2, "0");
    const tag = "check-" + String(p.tag).slice(0, 40) + "-" + pad2(d.getHours()) + pad2(d.getMinutes());
    const res = await api.queueAdd({ design_dir: state.designDir, spec: { ...base, tag, frm: null, to: null, only: null, skip: [] }, kind: "run", label: "Check of " + p.tag });
    if (res && res.ok === false) throw new Error(res.reason || "the config check refused it");
    job = (res && res.jobs && res.jobs[0]) || null;
    toast.show("Running " + tag + " from the saved config, to check it gives " + p.tag + "'s numbers.", "info", 6000);
  } catch (ex) { toast.show("The check run didn't start: " + (ex.message || ex), "error", 0); return; }
  if (!job) return;
  const design = state.designDir;
  const onQ = async (e) => {
    const ev = e.detail || {};
    const j = ev.type === "queue:job" ? ev.job : (ev.jobs || []).find((x) => x.id === job.id);
    if (!j || j.id !== job.id || !/^(done|failed|stopped|skipped)$/.test(j.state || "")) return;
    document.removeEventListener("g:queue", onQ);
    if (j.state !== "done") { toast.show("The check run " + (j.tag || job.tag || "") + " didn't finish (" + j.state + "): nothing to compare.", "warn", 9000); return; }
    let runs = [];
    try { runs = await api.runs(design); } catch (_e) { runs = []; }
    const r = runs.find((x) => x.tag === j.tag);
    if (!r) return;
    const a = (p.r && p.r.key_metrics) || {}, b = r.key_metrics || {};
    const diffs = [];
    for (const [k, words] of CHECK_KEYS) {
      if (typeof a[k] !== "number" || typeof b[k] !== "number") continue;
      const dl = F.delta(k, b[k], a[k], null);
      if (dl && !dl.same) diffs.push(words + " " + F.showText(k, a[k]) + " → " + F.showText(k, b[k]));
    }
    const cmp = { label: "Compare them", run: async () => { r._design = design; p.r._design = p.r._design || design; (await import("./runs.js")).compareRuns([p.r, r]); } };
    if (!diffs.length) toast.show("Same numbers as " + p.tag + ": " + r.tag + " reproduces it (setup slack " + F.showText("timing__setup__ws", b.timing__setup__ws) + ").", "success", 12000, cmp);
    else toast.show("Different from " + p.tag + ": " + diffs.join("; ") + ". Something else changed since it ran (Compare shows what).", "warn", 0, cmp);
  };
  document.addEventListener("g:queue", onQ);
}

export const _test = { planPoints, parseValues, parseValuesStrict, countRuns, clockValues, dieValues, endReason, sweepPoints, pointsLeft, pointsWaiting, sdcClockSource, typedValue, stillWins, stageLabel: FS.stageLabel };
