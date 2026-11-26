// runs.js — the Runs page: one sortable table of every run, where runs are
// managed, compared and swept. A row is one run; a group (a sweep, or runs
// that share a setting's value) is one parent row that lines up with the
// columns (the best number of its clean runs in each) and opens to its
// summary and its runs (sweeps.js draws the sweep sheet and summary).
//
//   click a row        it becomes the run every results page shows
//   open a row (▸)     the run panel beside the table: how it ended, its
//                      numbers against the baseline, tags, note and actions
//   ★                  the project's one baseline (context.js): every page
//                      measures against it
//   tick rows          Compare (2–12), Archive, Tag, Export, Delete: any number;
//                      Shift+click ticks a range, the group box a whole sweep
//   View               saved views per project (filters, sort, columns, group)
//
// The table state (filters, sort, columns, grouping) is kept per project, so
// a reload or a project switch and back finds it as left. Long lists draw
// only the rows near the view (fixed-height rows inside the table's own
// scroll box).

import { api, fmt } from "./api.js";
import { state, safeStorage } from "./state.js";
import { confirmDialog, alertDialog, checklistDialog, choiceDialog, customDialog, promptDialog } from "./dialog.js";
import { openLayer, closeLayer, isLayerOpen, layerCount } from "./overlay.js";
import { toast } from "./toast.js";
import {
  gatherRunsScoped, normDir, getRunScope, setRunScope, setRun, contextRun, contextDesign, onContext, designLabel,
  projectBaseline, setProjectBaseline, ensureProjectMeta, projectMeta, saveProjectViews,
} from "./context.js";
import { go } from "./nav.js";
import * as FS from "./flowstages.js";
import { icon } from "./icons.js";
import { verdictOf as cardVerdict, warningsHtml, fmtDur } from "./reportcard.js";
import { localeTime, localeDate } from "./intlcache.js";
import { loadingStart, pageSkeleton } from "./loading.js";
import * as MF from "./metricfmt.js";
import { toCsv, downloadCsv, downloadText } from "./csvutil.js";
import { singleKeysOn } from "./prefs.js";
import * as SM from "./studymodel.js";
import * as SQ from "./studyqueue.js";

// The last step of a stage in the Classic flow (matches setup.js's --to).
function lastStepOfStage(stageId) {
  const flow = (state.flows || []).find((f) => f.name === "Classic");
  const ids = (flow && flow.steps) || [];
  const st = FS.stagesOf(ids.map((id) => ({ id })));
  let last = "";
  st.forEach((s, i) => { if (s === stageId) last = ids[i]; });
  return last;
}

// Lightweight "make this run's design active" — needed before any run-scoped
// call (files, note, pin) since they resolve against the server's active
// design dir. It does not switch tabs.
async function focusDesign(designDir) {
  if (designDir && normDir(designDir) !== normDir(state.designDir)) {
    try { await api.setDesignDir(designDir); state.designDir = designDir; } catch (_e) {}
  }
}

const esc = (s) => fmt.escape(s == null ? "" : String(s));
const num = (v) => (typeof v === "number" && Number.isFinite(v) && Math.abs(v) < 1e20 ? v : null);
const km = (r, k) => num((r.key_metrics || {})[k]);
// "|" joins them: a NUL would not survive an HTML attribute (data-key).
// Memoised on the row: every render and every tick asks for it, a thousand times.
const keyOf = (r) => r._k || (r._k = normDir(r._design) + "|" + r.tag);
const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");

// A run whose registers no clock reached (Verify's verdict, no_clock): OpenSTA
// timed no path, so its slack numbers say nothing. Timing cells read
// "not timed", never 0.00.
const untimed = (r) => !!(r.verdict && r.verdict.no_clock);
const UNTIMED_TIP = "Timing not checked: no clock reached the registers, so OpenSTA timed no path. Verify says which clock port the config names.";

// ------------------------------------------------------------------ columns
// dir: +1 higher is better, -1 lower is better, 0 no direction (no colour).
// Directions follow LibreLane's metric registry (metricfmt.direction).
function checksOf(r) {
  const m = r.key_metrics || {};
  const drc = ["magic__drc_error__count", "klayout__drc_error__count", "route__drc_errors"]
    .map((k) => num(m[k])).filter((v) => v != null);
  const lvs = num(m.design__lvs_error__count), ant = num(m.antenna__violating__nets);
  if (!drc.length && lvs == null && ant == null) return null;
  const d = drc.length ? Math.max(...drc) : null;
  return { drc: d, lvs, ant, total: (d || 0) + (lvs || 0) + (ant || 0) };
}
const COLS = [
  { id: "name", label: "Run", fixed: true, sort: (r) => r.tag.toLowerCase() },
  { id: "verdict", label: "Result", fixed: true, sort: (r) => VERDICT_ORDER[verdictOf(r).state] ?? 9 },
  { id: "when", label: "When", sort: (r) => r.started_at || "", def: true },
  { id: "origin", label: "Origin", sort: (r) => originText(r), def: 2000 },
  { id: "design", label: "Design", sort: (r) => designLabel(r._design), allOnly: true, def: true },
  { id: "pdk", label: "PDK", sort: (r) => r.pdk || "", def: false },
  // The metric columns show LibreLane's values through the one formatter
  // (metricfmt.js) Compare, the sweep views and Analytics use: slack signed in
  // ns, areas in µm², utilisation in %, power in the prefix that suits the
  // runs shown (one prefix down the column).
  metricCol("wns", "Setup WNS", "timing__setup__ws", true, "Worst setup slack in any corner (ns). Positive: the design meets its clock."),
  metricCol("tns", "Setup TNS", "timing__setup__tns", 1500, "Total negative setup slack (ns). 0 when every path meets timing."),
  metricCol("hold", "Hold WNS", "timing__hold__ws", 1500, "Worst hold slack in any corner (ns)."),
  metricCol("die", "Die area", "design__die__area", 1500, "design__die__area: the whole die, in µm²."),
  metricCol("cells", "Std-cell area", "design__instance__area__stdcell", true,
    "design__instance__area__stdcell: the standard cells' area in µm², without fill, tap or decap cells."),
  metricCol("util", "Util", "design__instance__utilization", true, "How full the core is with cells at the end (design__instance__utilization)."),
  metricCol("power", "Power", "power__total", true, "Total power OpenROAD estimated (power__total)."),
  { id: "checks", label: "DRC · LVS · Ant", dir: -1, get: (r) => { const c = checksOf(r); return c ? c.total : null; }, def: true,
    tip: "Design-rule errors (worst of Magic, KLayout and the router), LVS errors and antenna violations. Not checked: blank." },
  { id: "runtime", label: "Runtime", key: "runtime", dir: -1, get: (r) => num(r.wall_time_s), f: (v) => fmtDur(v), def: 1300, tip: "The sum of every step's time." },
  { id: "changed", label: "Changed vs baseline", sort: (r) => (CHANGES.get(keyOf(r)) || []).length, def: true },
  { id: "tags", label: "Tags", sort: (r) => (r.tags || []).join(" ").toLowerCase(), def: false },
  { id: "note", label: "Note", sort: (r) => r.note || "", def: 2100 },
];
// The column heads: one line each, short enough that none wraps.
const HEADS = { die: "Die", cells: "Std cells", checks: "Checks", changed: "Changed" };
const VERDICT_ORDER = { running: 0, passed: 1, warning: 2, incomplete: 3, stopped: 4, failed: 5 };
const TIMING = new Set(["wns", "tns", "hold"]);
// One prefix per power column, from the runs shown (renderRuns).
const _scales = new Map();
function metricCol(id, label, key, def, tip) {
  return {
    id, label, key, def, tip, dir: MF.direction(key, null),
    get: (r) => km(r, key),
    f: (v) => (MF.unitOf(MF.parseKey(key).base) === "W" ? MF.showAt(key, v, _scales.get(id)) : MF.show(key, v, { compact: true })).text,
    get unit() { const u = MF.unitOf(MF.parseKey(key).base); return u === "W" ? (_scales.get(id) ? _scales.get(id).unit : "W") : MF.show(key, 1).unit; },
  };
}
// Any metric (m:) or setting (s:) as a column: values from /api/runs-values.
const XV = new Map();          // run_dir -> { "m:key": value, "s:KEY": value }
let _keysDirs = {};            // LibreLane's directions for the metrics Columns lists
function extraCol(xk) {
  const isM = xk.startsWith("m:"), key = xk.slice(2);
  const id = "x:" + xk;
  const base = MF.parseKey(key).base;
  const u = isM ? MF.unitOf(base) : "";
  const timing = isM && u === "ns";
  return {
    id, extra: xk, key: isM ? key : null, setting: !isM,
    label: key, tip: (isM ? "LibreLane metric " : "Setting ") + key + (isM ? " (final/metrics.json)" : " (the run's resolved.json)"),
    dir: isM ? MF.direction(key, _keysDirs) : 0, timing,
    get: (r) => { const v = (XV.get(r.run_dir) || {})[xk]; return isM ? num(v) : v; },
    f: (v) => (isM ? (u === "W" ? MF.showAt(key, v, _scales.get(id)) : MF.show(key, v, { compact: true })).text : showSetting(v)),
    get unit() { return isM ? (u === "W" ? (_scales.get(id) ? _scales.get(id).unit : "W") : MF.show(key, 1).unit) : ""; },
    sort: isM ? null : (r) => { const v = (XV.get(r.run_dir) || {})[xk]; return v == null ? null : typeof v === "number" ? v : showSetting(v).toLowerCase(); },
  };
}
const showSetting = (v) => (v == null ? "" : Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v));
function allCols() { return COLS.concat((T.extra || []).map(extraCol)); }

// A difference too small to mean anything, and the words for a hover: the
// one rule every page uses (metricfmt.delta).
function deltaOfCol(c, v, b) {
  const key = c.key || c.id;
  return MF.delta(key, v, b, null, { scale: _scales.get(c.id) || null, dir: c.dir || 0 });
}
// The exact value, for a cell's hover: more digits than the cell, in its unit.
function exactOf(c, v) {
  if (c.id === "runtime") return fmtDur(v);
  if (c.setting) return showSetting(v);
  const u = c.key ? MF.unitOf(MF.parseKey(c.key).base) : c.unit || "";
  if (u === "ratio") return String(Number((v * 100).toPrecision(6))) + " %";
  return String(Number(v.toPrecision(6))) + (u ? " " + u : "");
}

// ------------------------------------------------------------------ table state (per project)
// What the table shows: filters, sort, columns, grouping, and the view it came
// from. Kept per project (and once for "All designs") in localStorage, so it
// survives a reload; a named view keeps a copy on the server.
const STATE_KEY = "ll.runsTable", OPEN_KEY = "ll.runsOpenSweeps";
const defState = () => ({ q: "", status: "", origin: "", pdk: "", tags: [], archived: "hide",
  sort: { col: "when", dir: -1 }, cols: {}, extra: [], group: "sweeps", groupKey: "", view: "all" });
let T = defState();
let _slot = null;
function slotOf() { return getRunScope() === "all" ? "*all" : normDir(state.designDir) || ""; }
function loadState() {
  const k = slotOf();
  if (k === _slot) return;
  _slot = k;
  const all = safeStorage.getJSON(STATE_KEY, {}) || {};
  if (all[k]) T = { ...defState(), ...all[k] };
  else {
    // First time here: the project's default view, else the filters and
    // columns the page kept before views existed (adopted once).
    T = defState();
    const old = (safeStorage.getJSON("ll.runsFilters", {}) || {})[k];
    if (old) for (const x of ["q", "status", "origin", "pdk"]) if (typeof old[x] === "string") T[x] = old[x];
    const oldCols = safeStorage.getJSON("ll.runsCols", null);
    if (oldCols) T.cols = { ...oldCols };
    const oldSort = safeStorage.getJSON("ll.runsSort", null);
    if (oldSort && oldSort.col) T.sort = oldSort;
    const dv = k !== "*all" && projectMeta(state.designDir).default_view;
    const v = dv && viewById(dv);
    if (v) T = { ...stateOfView(v), view: v.id };
  }
  const fi = document.getElementById("runs-filter");
  if (fi && fi.value !== T.q) fi.value = T.q;
}
function saveState() {
  const all = safeStorage.getJSON(STATE_KEY, {}) || {};
  delete all[_slot];
  all[_slot] = T;
  const keys = Object.keys(all);
  for (const old of keys.slice(0, Math.max(0, keys.length - 40))) delete all[old];
  safeStorage.setJSON(STATE_KEY, all);
  paintViewBtn();
}
function setT(patch, { render = true } = {}) {
  Object.assign(T, patch);
  saveState();
  if (render) renderRuns({ cached: true });
}

// ------------------------------------------------------------------ views
const BUILTIN = [
  { id: "all", name: "All runs", builtin: true, state: {} },
  { id: "clean", name: "Clean runs", builtin: true, state: { status: "passed" } },
  { id: "archived", name: "Archived", builtin: true, state: { archived: "only" } },
];
const VIEW_KEYS = ["q", "status", "origin", "pdk", "tags", "archived", "sort", "cols", "extra", "group", "groupKey"];
function savedViews() { return getRunScope() === "all" ? [] : (projectMeta(state.designDir).views || []); }
function viewById(id) { return BUILTIN.find((v) => v.id === id) || savedViews().find((v) => v.id === id) || null; }
// A view as table state: built-ins over the defaults; saved views as stored.
function stateOfView(v) {
  const d = defState();
  if (v.builtin) return { ...d, ...v.state };
  const f = v.filters || {}, c = v.columns || {};
  return { ...d, q: f.q || "", status: f.status || "", origin: f.origin || "", pdk: f.pdk || "", tags: f.tags || [],
    archived: f.archived || "hide", sort: v.sort && v.sort.col ? v.sort : d.sort, cols: c.map || {}, extra: c.extra || [],
    group: (v.group && v.group.by) || "sweeps", groupKey: (v.group && v.group.key) || "" };
}
function viewOfState(id, name) {
  return { id, name, filters: { q: T.q, status: T.status, origin: T.origin, pdk: T.pdk, tags: T.tags, archived: T.archived },
    sort: T.sort, columns: { map: T.cols, extra: T.extra }, group: { by: T.group, key: T.groupKey } };
}
const pick = (s) => JSON.stringify(VIEW_KEYS.map((k) => s[k]));
function viewDirty() {
  const v = viewById(T.view);
  return !v || pick(stateOfView(v)) !== pick(T);
}
function paintViewBtn() {
  const b = document.getElementById("rn-view-btn");
  if (!b) return;
  const v = viewById(T.view) || BUILTIN[0];
  const dirty = viewDirty();
  b.innerHTML = "<span class='rn-view-k'>View</span><span class='rn-view-n'>" + esc(v.name) + "</span>" +
    (dirty ? "<i class='rn-view-dot' title='Changed since you chose this view'></i>" : "") + icon("chevronDown", { size: 13 });
  b.title = dirty ? "The view " + v.name + ", changed: save it, or save it as a new view" : "Saved views of this project";
}

function wireViews() {
  const btn = document.getElementById("rn-view-btn"), pop = document.getElementById("rn-views");
  if (!btn || !pop || btn._wired) return;
  btn._wired = true;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isLayerOpen(pop)) { closeLayer(pop); return; }
    paintViewsMenu();
    pop.hidden = false;
    openLayer(pop, { kind: "menu", owner: btn, onClose: () => { pop.hidden = true; } });
  });
  pop.addEventListener("click", async (e) => {
    const it = e.target.closest("[data-view], [data-vact]");
    if (!it) return;
    closeLayer(pop, "api");
    if (it.dataset.view) { const v = viewById(it.dataset.view); if (v) setT({ ...stateOfView(v), view: v.id }); return; }
    await viewAction(it.dataset.vact);
  });
}
function paintViewsMenu() {
  const pop = document.getElementById("rn-views");
  const runs = _shown.runs || [];
  const arch = runs.filter((r) => r.archived).length;
  const cur = T.view, dirty = viewDirty();
  const item = (v, extra = "") => "<button class='menu-item rn-vi" + (v.id === cur ? " is-cur" : "") + "' role='menuitem' data-view='" + esc(v.id) + "'>" +
    "<span>" + esc(v.name) + (v.id === cur && dirty ? " <i class='rn-view-dot'></i>" : "") + "</span><span class='rn-vi-x'>" + extra + "</span></button>";
  const dv = projectMeta(state.designDir).default_view || "all";
  const saved = savedViews();
  const curSaved = saved.find((v) => v.id === cur);
  const scopeAll = getRunScope() === "all";
  pop.innerHTML = "<div class='menu-lane'>Built in</div>" +
    BUILTIN.map((v) => item(v, v.id === "archived" ? (arch ? String(arch) : "") : v.id === dv && !scopeAll ? "default" : "")).join("") +
    (saved.length ? "<div class='menu-lane'>This project</div>" + saved.map((v) => item(v, v.id === dv ? "default" : "")).join("") : "") +
    "<div class='menu-sep'></div>" +
    (scopeAll ? "<div class='menu-note'>Saved views belong to a project: switch to This design to save one.</div>" :
      (curSaved && dirty ? "<button class='menu-item' role='menuitem' data-vact='save'>Save changes to “" + esc(curSaved.name) + "”</button>" : "") +
      "<button class='menu-item' role='menuitem' data-vact='saveas'>Save as a new view…</button>" +
      (dirty ? "<button class='menu-item' role='menuitem' data-vact='discard'>Discard changes</button>" : "") +
      (curSaved ? "<button class='menu-item' role='menuitem' data-vact='rename'>Rename “" + esc(curSaved.name) + "”…</button>" +
        "<button class='menu-item' role='menuitem' data-vact='delete'>Delete view “" + esc(curSaved.name) + "”</button>" : "") +
      (cur !== dv && viewById(cur) ? "<button class='menu-item' role='menuitem' data-vact='default'>Open " + esc(designLabel(state.designDir)) + " on this view</button>" : ""));
}
async function viewAction(a) {
  const d = state.designDir;
  const views = savedViews().slice();
  const cur = views.find((v) => v.id === T.view);
  try {
    if (a === "discard") { const v = viewById(T.view) || BUILTIN[0]; setT({ ...stateOfView(v), view: v.id }); return; }
    if (a === "save" && cur) {
      const i = views.indexOf(cur);
      views[i] = viewOfState(cur.id, cur.name);
      await saveProjectViews(d, views);
      toast.show("Saved the view “" + cur.name + "”.", "success", 2500);
    } else if (a === "saveas") {
      const name = await promptDialog({ title: "Save as a new view", label: "Its name (the filters, sort, columns and grouping you have now)", defaultValue: "" });
      if (!name || !name.trim()) return;
      const id = "v" + Date.now().toString(36);
      views.push(viewOfState(id, name.trim().slice(0, 60)));
      await saveProjectViews(d, views);
      T.view = id; saveState();
      toast.show("Saved the view “" + name.trim() + "”. It is kept with the project.", "success", 3000);
    } else if (a === "rename" && cur) {
      const name = await promptDialog({ title: "Rename the view", label: "New name", defaultValue: cur.name });
      if (!name || !name.trim()) return;
      cur.name = name.trim().slice(0, 60);
      await saveProjectViews(d, views);
    } else if (a === "delete" && cur) {
      if (!(await confirmDialog({ title: "Delete the view “" + cur.name + "”?", danger: true, confirmText: "Delete view",
        body: "Only the saved view goes: no run is touched. The table keeps what it shows now." }))) return;
      const left = views.filter((v) => v !== cur);
      await saveProjectViews(d, left, projectMeta(d).default_view === cur.id ? null : undefined);
      T.view = "all"; saveState();
    } else if (a === "default") {
      await saveProjectViews(d, views, T.view === "all" ? null : T.view);
      toast.show(designLabel(d) + " now opens on “" + (viewById(T.view) || {}).name + "”.", "success", 3000);
    }
  } catch (ex) {
    toast.show("Couldn't save the views of " + designLabel(d) + ": " + (ex.message || ex) + ". They're kept in .gui-runs.json in the project folder.", "error");
  }
  paintViewBtn();
}

// ------------------------------------------------------------------ columns that fit
function visibleCols(scope) {
  return allCols().filter((c) => (c.allOnly ? scope === "all" : true) &&
    (c.fixed || c.extra || (T.cols[c.id] != null ? T.cols[c.id] : defOn(c) && !_autoHide.has(c.id))));
}
// Columns that fit: when the default set is wider than the room the table
// has, the least needed ones step aside, in this order (never a column the
// person turned on themselves; Columns brings any of them back).
const DROP_ORDER = ["note", "tags", "origin", "pdk", "changed", "tns", "hold", "die", "runtime", "util", "power", "checks", "cells", "when"];
let _autoHide = new Set(), _autoKey = "";
function fitColumns(table, scope) {
  const wrap = document.getElementById("rn-wrap");
  const key = scope + "|" + (wrap ? wrap.clientWidth : 0) + "|" + JSON.stringify(T.cols) + "|" + T.extra.join(",");
  if (key !== _autoKey) {
    _autoKey = key;
    if (_autoHide.size) { _autoHide = new Set(); _model.cols = visibleCols(scope); paintTable(table, _model); }
  }
  for (let i = 0; wrap && i < DROP_ORDER.length && wrap.scrollWidth > wrap.clientWidth + 1; i++) {
    const next = DROP_ORDER.find((id) => _model.cols.some((c) => c.id === id) && T.cols[id] !== true);
    if (!next) break;
    _autoHide.add(next);
    _model.cols = visibleCols(scope);
    paintTable(table, _model);
  }
}
// A column whose default is a width shows by default only when the table has
// that much room, so the default set fits the screen (1366: the essentials).
function defOn(c) {
  if (typeof c.def !== "number") return !!c.def;
  const w = (document.querySelector("#sec-runs .rn-main")?.clientWidth || document.getElementById("sec-runs")?.clientWidth || window.innerWidth) + 30;
  return w >= c.def - 120;
}

// ------------------------------------------------------------------ verdict
const RX = new Map();          // keyOf(run) -> runExplain result (failed runs only)
function verdictOf(r) {
  if (r.verdict && r.verdict.state) return r.verdict;
  if (state.status && state.status.running && state.liveTag && r.tag === state.liveTag && normDir(r._design) === normDir(state.designDir)) {
    return { state: "running", label: "running" };
  }
  if (r.success) return { state: "passed", label: "passed" };
  const rx = RX.get(keyOf(r));
  if (rx) {
    if (rx.kind === "stopped") return { state: "stopped", label: "stopped" };
    if (rx.kind === "incomplete") return { state: "incomplete", label: "incomplete" };
    if (rx.kind === "checks-failed") return { state: "failed", label: "checks failed", stage: "signoff" };
    if (rx.stage) return { state: "failed", label: "failed at " + FS.stageLabel(rx.stage), stage: rx.stage };
  }
  return { state: "failed", label: "failed" };
}
const VERDICT_TONE = { passed: "pass", warning: "warn", failed: "fail", stopped: "warn", incomplete: "warn", running: "live" };
const finished = (r) => /passed|warning/.test(verdictOf(r).state);

// ------------------------------------------------------------------ origin
let SWEEPS = [];               // this design's sweep manifests (newest first)
let _swById = new Map(), _swByTag = new Map();
function indexSweeps() {
  _swById = new Map(SWEEPS.map((s) => [s.id, s]));
  _swByTag = new Map();
  for (const s of SWEEPS) for (const t of s.tags || []) if (!_swByTag.has(t)) _swByTag.set(t, s);
}
// run.origin (history.run_origin), else the summary's branched_from (TRUST).
function originOf(r) {
  if (r.origin && r.origin.kind) return r.origin;
  const b = r.branched_from;
  if (typeof b === "string" && b) return { kind: "fix", parent: b, changes: [] };
  if (b && b.tag) return { kind: "fix", parent: b.tag, stage: b.stage, changes: b.changes || [] };
  return {};
}
// Who started it: lane 1's started_by ("lanex" | "sweep" | "terminal"), also
// origin.source; a run with neither was started by LanEx.
function startedBy(r) { return r.started_by || (r.origin && r.origin.source) || "lanex"; }
function sweepOf(r) {
  const o = originOf(r);
  if (o.kind === "sweep") return _swById.get(o.sweep_id) || { id: o.sweep_id, axes: [], tags: [], _design: r._design };
  if (normDir(r._design) !== normDir(state.designDir)) return null;
  return _swByTag.get(r.tag) || null;
}
function pointValues(r, sw) {
  const o = originOf(r);
  if (o.kind === "sweep" && o.values && Object.keys(o.values).length) return o.values;
  if (sw) {
    const i = (sw.tags || []).indexOf(r.tag);
    if (sw.points && sw.points[i]) return sw.points[i];
    if (i >= 0 && sw.axes && sw.axes.length === 1) return { [sw.axes[0].var]: sw.axes[0].values[i] };
  }
  return null;
}
const valuesText = (v) => Object.entries(v || {}).map(([k, x]) => k + " " + x).join(", ");
function originText(r) {
  const o = originOf(r);
  if (o.kind === "fix") return "fix of " + o.parent;
  if (sweepOf(r)) return "sweep";
  if (r.imported) return "imported";
  if (startedBy(r) === "terminal") return "terminal";
  return "manual";
}

// ------------------------------------------------------------------ groups
// A grouper folds runs under one parent row: keyOf(run) -> a group key or
// null, header(group, cols) -> the row's cells, order(runs) the children's
// order, summary(el, group) what opens under the row. Sweeps are the
// default; "a setting" groups by any setting's value; studies register their
// own (registerGrouper).
const GROUPERS = new Map();
export function registerGrouper(g) { GROUPERS.set(g.id, g); }
registerGrouper({
  id: "sweeps", label: "Sweeps",
  keyOf: (r) => { const sw = sweepOf(r); return sw ? normDir(r._design) + "|sweep:" + sw.id : null; },
  make: (r) => ({ kind: "sweep", sw: sweepOf(r) }),
  order: (g) => sweepOrder(g),
  header: (g, cols, gk, open) => sweepRowHtml(g, cols, gk, open),
  summary: (el, g) => import("./sweeps.js").then((mod) => mod.paintSweepSummary(el, g.sw, g.runs, {
    verdictOf, failCode, baseline: projectBaseline(g.runs[0]._design),
    openRun: (r) => { setRun(r.tag, r._design); openSide(r); },
    selectRuns: (rs) => pickRuns(rs, true),
  })),
});
registerGrouper({
  id: "setting", label: "A setting",
  keyOf: (r) => {
    if (!T.groupKey) return null;
    const v = (XV.get(r.run_dir) || {})["s:" + T.groupKey];
    return v === undefined ? null : normDir(r._design) + "|set:" + JSON.stringify(v);
  },
  make: (r) => ({ kind: "setting", key: T.groupKey, value: (XV.get(r.run_dir) || {})["s:" + T.groupKey] }),
  order: (g) => g.runs.sort((a, b) => String(b.started_at).localeCompare(String(a.started_at))),
  header: (g, cols, gk, open) => groupRowHtml(g, cols, gk, open, {
    tag: "Setting", title: T.groupKey + " = " + showSetting(g.value), why: "" }),
});
function activeGrouper() { return T.group === "none" ? null : GROUPERS.get(T.group) || GROUPERS.get("sweeps"); }

// A sweep's points by the value they tried (the order the chart reads), then
// in the order they were planned.
function sweepOrder(g) {
  const ax = ((g.sw && g.sw.axes) || []).map((a) => a.var);
  const at = (r) => { const i = (g.sw.tags || []).indexOf(r.tag); return i < 0 ? 1e9 : i; };
  const val = (r) => { const p = pointValues(r, g.sw) || {}; return ax.map((k) => { const x = Number(p[k]); return Number.isFinite(x) ? x : String(p[k] ?? ""); }); };
  return g.runs.sort((a, b) => {
    const va = val(a), vb = val(b);
    for (let i = 0; i < va.length; i++) {
      if (va[i] === vb[i]) continue;
      if (typeof va[i] === "number" && typeof vb[i] === "number") return va[i] - vb[i];
      return String(va[i]).localeCompare(String(vb[i]));
    }
    return at(a) - at(b) || String(a.started_at).localeCompare(String(b.started_at));
  });
}

// ------------------------------------------------------------------ state
let _wired = false;
const _picked = new Set();     // keyOf(run)
let _anchor = null;            // the last ticked row (Shift+click range start)
let _open = null;              // keyOf(run) whose panel is open
let _openSweeps = new Set(safeStorage.getJSON(OPEN_KEY, []) || []);
let _shown = { key: "", runs: [] };
let _visible = [];             // the runs the filters show, in table order
let _renderSeq = 0;
const CHANGES = new Map();     // keyOf(run) -> [{var, from, to}] vs the baseline

function baselineRowFor(r) {
  const t = projectBaseline(r._design);
  if (!t || t === r.tag) return null;
  const d = normDir(r._design);
  const mk = d + "|" + t;
  if (_baseMemo.has(mk)) return _baseMemo.get(mk);
  const b = _shown.runs.find((x) => x.tag === t && normDir(x._design) === d) || null;
  _baseMemo.set(mk, b);
  return b;
}
let _baseMemo = new Map();
const isBaseline = (r) => projectBaseline(r._design) === r.tag;

function wire() {
  if (_wired) return;
  _wired = true;
  document.querySelectorAll("#sec-runs .runs-scope[data-scope]").forEach((b) =>
    b.addEventListener("click", () => { setRunScope(b.dataset.scope); renderRuns(); }));
  onContext((d) => { if (d.why === "baseline") renderRuns({ cached: true }); else paintViewing(); });
  const fi = document.getElementById("runs-filter");
  let qt = 0;
  if (fi) fi.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(() => setT({ q: fi.value }), 120); });
  document.getElementById("runs-import")?.addEventListener("click", () => importRunFlow());
  document.getElementById("runs-new-sweep")?.addEventListener("click", () => import("./sweeps.js").then((m) => m.openSweepSheet()));
  // The column picker and the filters: popovers on the overlay stack.
  wirePop("rn-cols-btn", "rn-cols", paintColPicker, () => document.querySelector("#rn-cols input"));
  wirePop("rn-filt-btn", "rn-filters", paintFilters);
  wireViews();
  // Esc closes the run panel when nothing is open above it.
  document.addEventListener("keydown", (e) => {
    if (!document.getElementById("sec-runs")?.classList.contains("section-active")) return;
    if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z") && _undo && !isTyping(e.target) && !layerCount()) {
      e.preventDefault(); const u = _undo; _undo = null; u(); return;
    }
    if (e.key !== "Escape" || e.defaultPrevented || !_open || layerCount()) return;
    const t = e.target;
    if (t && t.matches && t.matches("input[type=search]") && t.value) return;
    closeSide();
  });
  // Default columns and the table's height follow the window.
  let rz = 0;
  window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (document.getElementById("sec-runs")?.classList.contains("section-active")) renderRuns({ cached: true }); }, 250); });
  // A study's points change state in the queue (queued, running, skipped):
  // the group row's marks and its summary follow, a moment later.
  let sqT = 0;
  SQ.onStudyJobs(() => {
    clearTimeout(sqT);
    sqT = setTimeout(() => { if (document.getElementById("sec-runs")?.classList.contains("section-active")) renderRuns(); }, 1500);
  });
  document.addEventListener("g:sweep", () => {
    if (document.getElementById("sec-runs")?.classList.contains("section-active")) renderRuns();
  });
}
const isTyping = (t) => !!(t && t.closest && t.closest("input, textarea, select, [contenteditable='true']"));
function wirePop(btnId, popId, paint, focus = null) {
  const b = document.getElementById(btnId), p = document.getElementById(popId);
  if (!b || !p) return;
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isLayerOpen(p)) { closeLayer(p); return; }
    p.hidden = false;
    paint();
    openLayer(p, { kind: "popover", owner: b, onClose: () => { p.hidden = true; }, focus });
  });
}

// ------------------------------------------------------------------ Columns
let _keys = null, _keysFor = "";
async function loadKeys() {
  const d = getRunScope() === "all" ? state.designDir : state.designDir;
  if (!d || _keysFor === normDir(d)) return _keys;
  _keysFor = normDir(d);
  try { _keys = await api.runsKeys(d); _keysDirs = _keys.directions || {}; } catch (_e) { _keys = { metrics: [], settings: [] }; }
  return _keys;
}
function paintColPicker() {
  const cp = document.getElementById("rn-cols");
  const shown = new Set(visibleCols(getRunScope()).map((c) => c.id));
  const common = COLS.filter((c) => !c.fixed && !c.allOnly);
  const box = (id, label, on, extra = "", tip = "") => "<label class='rn-col-opt'" + (tip ? " title='" + esc(tip) + "'" : "") + "><input type='checkbox' data-col='" + esc(id) + "'" +
    (on ? " checked" : "") + "/> <span>" + esc(label) + "</span>" + extra + "</label>";
  cp.innerHTML = "<div class='rn-cols-top'><div class='rn-cols-h'>Columns</div><button class='btn btn-ghost btn-sm' data-cols-reset>Reset</button></div>" +
    "<input type='search' class='rn-q rn-cols-q' placeholder='Find a metric or setting' aria-label='Find a metric or setting'/>" +
    "<div class='rn-cols-body'><div class='rn-cols-g'>" + common.map((c) => box(c.id, c.label, shown.has(c.id), "", c.tip)).join("") + "</div>" +
    "<div class='rn-cols-more'><p class='muted'>Reading the metrics and settings of these runs…</p></div></div>";
  const wire = () => cp.querySelectorAll("input[data-col]").forEach((i) => i.addEventListener("change", () => {
    const id = i.dataset.col;
    if (id.startsWith("x:")) {
      const xk = id.slice(2);
      const extra = T.extra.filter((k) => k !== xk);
      if (i.checked) extra.push(xk);
      setT({ extra: extra.slice(0, 24) });
    } else setT({ cols: { ...T.cols, [id]: i.checked } });
  }));
  wire();
  cp.querySelector("[data-cols-reset]").addEventListener("click", () => {
    const v = viewById(T.view);
    const s = v ? stateOfView(v) : defState();
    setT({ cols: s.cols, extra: s.extra });
    paintColPicker();
  });
  loadKeys().then((k) => {
    const more = cp.querySelector(".rn-cols-more");
    if (!more || !k) return;
    const on = new Set(T.extra);
    const ms = (k.metrics || []).map((m) => m.key), ss = (k.settings || []).map((s) => s.key);
    const row = (pre, key) => {
      const dir = pre === "m:" ? MF.direction(key, _keysDirs) : 0;
      const u = pre === "m:" ? MF.show(key, 1).unit : "";
      return box("x:" + pre + key, key, on.has(pre + key), "<span class='rn-col-u'>" + esc(u) + (dir ? " " + MF.dirMark(dir) : "") + "</span>",
        pre === "m:" ? MF.dirWords(dir) + (u ? " · " + u : "") : "The value each run used (its resolved.json)");
    };
    more.innerHTML = "<details class='rn-cols-sec' open><summary>LibreLane metrics <span class='rn-n'>" + ms.length + "</span></summary><div class='rn-cols-g is-mono'>" +
      ms.map((m) => row("m:", m)).join("") + "</div></details>" +
      "<details class='rn-cols-sec'><summary>Settings <span class='rn-n'>" + ss.length + "</span></summary><div class='rn-cols-g is-mono'>" +
      ss.map((s) => row("s:", s)).join("") + "</div></details>" +
      "<p class='muted rn-cols-hint'>From the newest " + (k.runs_read || 0) + " runs. Folders and file paths are left out of Settings.</p>";
    wire();
    const q = cp.querySelector(".rn-cols-q");
    q.oninput = () => {
      const s = q.value.trim().toLowerCase();
      cp.querySelectorAll(".rn-col-opt").forEach((l) => { l.hidden = !!s && !l.textContent.toLowerCase().includes(s); });
      cp.querySelectorAll(".rn-cols-sec").forEach((d) => { if (s) d.open = true; });
    };
  });
}

// ------------------------------------------------------------------ Filters
function paintFilters() {
  const p = document.getElementById("rn-filters");
  const runs = _shown.runs || [];
  const cnt = (fn) => runs.filter(fn).length;
  const seg = (key, opts) => "<div class='rn-seg' role='group'>" + opts.filter(([v, , n]) => v === "" || n == null || n > 0 || T[key] === v).map(([v, label, n]) =>
    "<button class='rn-segb" + (T[key] === v ? " is-on" : "") + "' data-f='" + key + "' data-v='" + esc(v) + "' aria-pressed='" + (T[key] === v) + "'>" +
    esc(label) + (n != null ? " <span class='rn-n'>" + n + "</span>" : "") + "</button>").join("") + "</div>";
  const pdks = [...new Set(runs.map((r) => r.pdk).filter(Boolean))];
  const tagCount = new Map();
  for (const r of runs) for (const t of r.tags || []) tagCount.set(t, (tagCount.get(t) || 0) + 1);
  const tags = [...tagCount].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const lbl = (t) => "<div class='rn-f-l'>" + esc(t) + "</div>";
  p.innerHTML = "<div class='rn-cols-h'>Filters</div>" +
    lbl("Who started it") + seg("origin", [["", "Any", null], ["manual", "You", cnt((r) => originText(r) === "manual")],
      ["sweep", "Sweeps", cnt((r) => originText(r) === "sweep")], ["fix", "Fixes", cnt((r) => originOf(r).kind === "fix")],
      ["terminal", "Terminal", cnt((r) => originText(r) === "terminal")], ["imported", "Imported", cnt((r) => r.imported)], ["pinned", "Pinned", cnt((r) => r.pinned)]]) +
    (pdks.length > 1 ? lbl("PDK") + seg("pdk", [["", "Any", null], ...pdks.map((x) => [x, x, cnt((r) => r.pdk === x)])]) : "") +
    lbl("Archived runs") + seg("archived", [["hide", "Hidden", null], ["show", "Shown", null], ["only", "Only", cnt((r) => r.archived)]]) +
    (tags.length ? lbl("Tags (any of)") + "<div class='rn-f-tags'>" + tags.map(([t, n]) =>
      "<button class='rn-tagchip" + (T.tags.includes(t) ? " is-on" : "") + "' data-ftag='" + esc(t) + "' aria-pressed='" + T.tags.includes(t) + "'>" + esc(t) + " <span class='rn-n'>" + n + "</span></button>").join("") + "</div>" : "") +
    lbl("Group rows by") + "<div class='rn-seg' role='group'>" +
      [["sweeps", "Sweeps"], ["none", "Nothing"], ["setting", "A setting…"]].map(([v, l]) =>
        "<button class='rn-segb" + (T.group === v ? " is-on" : "") + "' data-grp='" + v + "'>" + esc(l) + (v === "setting" && T.group === "setting" && T.groupKey ? ": " + esc(T.groupKey) : "") + "</button>").join("") + "</div>" +
    (filterCount() ? "<button class='btn btn-ghost btn-sm rn-f-clear' data-fclear>Clear the filters</button>" : "");
  p.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { setT({ [b.dataset.f]: b.dataset.v }); paintFilters(); }));
  p.querySelectorAll("[data-ftag]").forEach((b) => b.addEventListener("click", () => {
    const t = b.dataset.ftag;
    setT({ tags: T.tags.includes(t) ? T.tags.filter((x) => x !== t) : T.tags.concat([t]) });
    paintFilters();
  }));
  p.querySelectorAll("[data-grp]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.grp !== "setting") { setT({ group: b.dataset.grp }); paintFilters(); return; }
    const k = await loadKeys();
    const names = ((k && k.settings) || []).map((s) => s.key);
    const name = await promptDialog({ title: "Group runs by a setting", label: "The setting (for example CLOCK_PERIOD). Runs with the same value fold under one row." +
      (names.length ? " This project's runs have " + names.length + " plain settings." : ""), defaultValue: T.groupKey || "CLOCK_PERIOD" });
    if (!name || !name.trim()) return;
    const key = name.trim().toUpperCase();
    const extra = T.extra.includes("s:" + key) ? T.extra : T.extra.concat(["s:" + key]);
    setT({ group: "setting", groupKey: key, extra });
  }));
  p.querySelector("[data-fclear]")?.addEventListener("click", () => { clearFilters(); paintFilters(); });
}
function filterCount() {
  return (T.origin ? 1 : 0) + (T.pdk ? 1 : 0) + (T.tags.length ? 1 : 0) + (T.archived !== "hide" ? 1 : 0);
}
function clearFilters() {
  setT({ q: "", status: "", origin: "", pdk: "", tags: [], archived: "hide" });
  const fi = document.getElementById("runs-filter"); if (fi) fi.value = "";
}

// The Result chips (counts from the unfiltered list) and the Filters badge.
function paintChips(runs) {
  const el = document.getElementById("rn-status");
  const vis = runs.filter((r) => archivedOk(r));
  const cnt = (fn) => vis.filter(fn).length;
  const all = [["", "All", vis.length], ["passed", "Clean", cnt((r) => verdictOf(r).state === "passed")],
    ["warning", "Warnings", cnt((r) => verdictOf(r).state === "warning")], ["failed", "Failed", cnt((r) => verdictOf(r).state === "failed")],
    ["stopped", "Stopped", cnt((r) => /stopped|incomplete/.test(verdictOf(r).state))]];
  if (el) {
    const opts = all.filter(([v, , n]) => v === "" || n > 0 || T.status === v);
    el.hidden = opts.length <= 1;
    el.innerHTML = opts.map(([v, label, n]) =>
      "<button class='rn-segb" + (T.status === v ? " is-on" : "") + "' data-v='" + esc(v) + "' aria-pressed='" + (T.status === v) + "'>" +
      esc(label) + " <span class='rn-n'>" + n + "</span></button>").join("");
    el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => setT({ status: b.dataset.v })));
  }
  const fb = document.getElementById("rn-filt-btn");
  if (fb) { const n = filterCount(); fb.innerHTML = "Filters" + (n ? " <span class='rn-badge'>" + n + "</span>" : ""); fb.classList.toggle("is-on", !!n); }
  document.querySelectorAll("#sec-runs .runs-scope[data-scope]").forEach((b) =>
    b.classList.toggle("is-on", b.dataset.scope === getRunScope()));
  paintViewBtn();
}

function archivedOk(r) {
  if (T.archived === "only") return !!r.archived;
  if (T.archived === "show") return true;
  return !r.archived;
}
function matches(r) {
  if (!archivedOk(r)) return false;
  const v = verdictOf(r).state;
  if (T.status === "passed" && v !== "passed") return false;
  if (T.status === "failed" && v !== "failed") return false;
  if (T.status === "warning" && v !== "warning") return false;
  if (T.status === "stopped" && !/stopped|incomplete/.test(v)) return false;
  if (T.origin === "pinned" && !r.pinned) return false;
  if (T.origin === "fix" && originOf(r).kind !== "fix") return false;
  if (T.origin === "imported" && !r.imported) return false;
  if (T.origin && !["fix", "pinned", "imported"].includes(T.origin) && originText(r) !== T.origin) return false;
  if (T.pdk && r.pdk !== T.pdk) return false;
  if (T.tags.length && !(r.tags || []).some((t) => T.tags.some((x) => x.toLowerCase() === t.toLowerCase()))) return false;
  const q = T.q.trim().toLowerCase();
  if (!q) return true;
  const sw = sweepOf(r);
  const hay = [r.tag, r.pdk, r.scl, r.note, originText(r), designLabel(r._design), valuesText(pointValues(r, sw)),
    (r.tags || []).join(" "), (CHANGES.get(keyOf(r)) || []).map((c) => c.var).join(" "), verdictOf(r).label || ""].join(" ").toLowerCase();
  return hay.includes(q);
}

function sortRuns(list) {
  const cols = allCols();
  const col = cols.find((c) => c.id === T.sort.col) || COLS[2];
  const val = col.sort || col.get || (() => 0);
  return list.slice().sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;          // blanks last, either way
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * T.sort.dir;
  });
}

// ------------------------------------------------------------------ render
export async function renderRuns({ cached = false } = {}) {
  const table = document.getElementById("runs-table");
  if (!table) return;
  wire();
  const scope = getRunScope();
  const key = scope + "\0" + normDir(state.designDir);
  const my = ++_renderSeq;
  let runs;
  if (cached && _shown.key === key) runs = _shown.runs;
  else {
    // A list not shown yet: the page skeleton after 300 ms, never "No runs" before the answer.
    const job = _shown.key !== key ? loadingStart(document.getElementById("sec-runs"), { skeleton: pageSkeleton("runs"), label: "Runs",
      hasContent: () => !!document.querySelector("#runs-table tbody tr") }) : null;
    try {
      const [rs, sw] = await Promise.all([gatherRunsScoped(scope, { strict: true }),
        state.designDir ? api.dseSweeps().catch(() => null) : Promise.resolve(null),
        state.designDir ? ensureProjectMeta(state.designDir) : Promise.resolve(null)]);
      runs = rs;
      if (sw) SWEEPS = (sw.data || sw).sweeps || [];
      indexSweeps();
      // Every design's baseline, for "All designs".
      await Promise.all([...new Set(runs.map((r) => normDir(r._design)))].map((d) => ensureProjectMeta(d)));
    } catch (_e) {
      if (my !== _renderSeq) return;
      if (_shown.key === key && _shown.runs.length) { note("Couldn't refresh the list. Trying again…"); setTimeout(() => { if (my === _renderSeq) renderRuns(); }, 4000); return; }
      runs = null;
    } finally { if (job) job.done(); }
  }
  if (my !== _renderSeq) return;
  loadState();
  const empty = document.getElementById("rn-empty"), wrap = document.getElementById("rn-wrap");
  const title = document.getElementById("rn-title");
  if (title) title.textContent = scope === "all" ? "Every run of every design you've opened" : (state.designDir ? "Every run of " + designLabel(state.designDir) : "Runs");
  const foot = document.getElementById("rn-foot");
  if (runs === null) {
    wrap.hidden = true; empty.hidden = false; if (foot) foot.hidden = true;
    empty.innerHTML = "<h3>Couldn't load your runs</h3><p>LanEx didn't answer in time. Trying again…</p>";
    setTimeout(() => { if (my === _renderSeq) renderRuns(); }, 4000);
    return;
  }
  note(projectMeta(state.designDir).damaged ? "This project's saved views couldn't be read; the file is backed up as " + projectMeta(state.designDir).damaged + "." : "");
  _shown = { key, runs };
  if (scope !== "all") state.runs = runs;
  // A remembered PDK filter that no run has any more would hide every run
  // with no chip left to clear it.
  if (T.pdk && !runs.some((r) => r.pdk === T.pdk)) { T.pdk = ""; saveState(); }
  paintChips(runs);
  paintLive();
  syncSide();
  if (!runs.length) {
    wrap.hidden = true; empty.hidden = false; if (foot) foot.hidden = true;
    empty.innerHTML = state.designDir || scope === "all"
      ? "<h3>No runs yet</h3><p>Run the flow once and every run appears here, one row each, with its timing, area, power and checks.</p>" +
        "<button class='btn btn-primary' data-go='setup'>Go to Setup</button>"
      : "<h3>Open a project first</h3><p>Runs lists the runs of the project you have open.</p><button class='btn btn-primary' data-go='setup'>Go to Setup</button>";
    _visible = [];
    syncSelection();
    return;
  }
  const shown = runs.filter(matches);
  _visible = shown;
  if (!shown.length) {
    wrap.hidden = true; empty.hidden = false; if (foot) foot.hidden = true;
    const arch = runs.filter((r) => r.archived && T.archived === "hide").length;
    empty.innerHTML = "<h3>No runs match</h3><p>Nothing fits the search and filters. There are " + plural(runs.length, "run") + " in all." +
      (arch ? " " + plural(arch, "archived run") + " " + (arch === 1 ? "is" : "are") + " hidden." : "") + "</p>" +
      "<button class='btn' id='rn-clear-filters'>Clear the filters</button>" +
      (arch ? " <button class='btn btn-ghost' id='rn-show-arch'>Show archived runs</button>" : "");
    empty.querySelector("#rn-clear-filters").onclick = () => clearFilters();
    empty.querySelector("#rn-show-arch")?.addEventListener("click", () => setT({ archived: "show" }));
    syncSelection();
    return;
  }
  wrap.hidden = false; empty.hidden = true;
  const cols = visibleCols(scope);
  _scales.clear();
  for (const c of cols) if (c.key && MF.unitOf(MF.parseKey(c.key).base) === "W") _scales.set(c.id, MF.scaleOf(c.key, shown.map((r) => c.get(r))));
  _baseMemo = new Map();
  _model = { items: buildItems(sortRuns(shown)), cols, a: 0, b: 0, windowed: false };
  fitHeight();
  paintTable(table, _model);
  fitColumns(table, scope);
  freezeCols(table);
  wireTable(table);
  paintFoot(runs, shown);
  syncSelection();
  paintViewing();
  explainFailed(runs);
  loadChanges(shown);
  loadExtra(shown);
}

function note(text) {
  const el = document.getElementById("runs-note");
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
}

// The table scrolls inside its own box (so the head and the first columns
// stick, and windowing reads one scroller), as tall as the window allows.
function fitHeight() {
  const wrap = document.getElementById("rn-wrap");
  const work = wrap && wrap.closest("main.work, .work");
  if (!wrap || !work || wrap.hidden) return;
  const z = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  const top = (wrap.getBoundingClientRect().top - work.getBoundingClientRect().top) / z + work.scrollTop;
  const foot = document.getElementById("rn-foot");
  const fh = foot && !foot.hidden ? foot.offsetHeight || 40 : 40;
  const h = Math.max(280, Math.floor(work.clientHeight - top - fh - 24));
  wrap.style.maxHeight = h + "px";
}

// The line under the table: how many, and what the colours mean.
function paintFoot(runs, shown) {
  const foot = document.getElementById("rn-foot");
  if (!foot) return;
  foot.hidden = false;
  const sweeps = new Set(shown.map((r) => { const sw = sweepOf(r); return sw ? normDir(r._design) + "|" + sw.id : ""; }).filter(Boolean)).size;
  const live = runs.filter((r) => archivedOk(r));
  const n = shown.length === live.length ? plural(live.length, "run") : shown.length + " of " + plural(live.length, "run");
  const arch = runs.filter((r) => r.archived).length;
  const base = getRunScope() === "all" ? "" : projectBaseline(state.designDir);
  foot.innerHTML = "<span>" + n + (sweeps ? " · " + plural(sweeps, "sweep") : "") +
    (arch && T.archived === "hide" ? " · <button class='linklike' data-foot='arch'>" + arch + " archived (hidden)</button>" : "") + "</span>" +
    (base
      ? "<span>Baseline <code>" + esc(base) + "</code></span><span class='rn-key'><i style='background:var(--pass)'></i>better than it</span><span class='rn-key'><i style='background:var(--fail)'></i>worse</span>"
      : getRunScope() === "all" ? "<span>Each design is coloured against its own baseline.</span>"
      : "<span>Star a run to make it the baseline: every number is then coloured better or worse than it, here and on every page.</span>") +
    "<span class='rn-spacer'></span><button class='linklike rn-foot-csv' data-foot='csv' title='The columns shown, one row per run shown, for Excel or LibreOffice'>" + icon("download", { size: 12 }) + " CSV</button>" +
    "<button class='linklike rn-foot-csv' data-foot='json' title='Every run shown with all its numbers and where it came from'>" + icon("download", { size: 12 }) + " JSON</button>";
  foot.onclick = (e) => {
    const b = e.target.closest("[data-foot]");
    if (!b) return;
    if (b.dataset.foot === "arch") setT({ archived: "show" });
    else exportRuns(b.dataset.foot, sortRuns(shown), T.view);
  };
}

function headHtml(cols) {
  const th = (c) => {
    const on = T.sort.col === c.id;
    const dir = c.dir ? MF.dirMark(c.dir) : "";
    const tip = (c.tip || "Sort by " + c.label) + (c.dir ? "\n" + MF.dirWords(c.dir) + " (LibreLane's metric registry)" : "");
    return "<th scope='col' class='rn-th rn-c-" + cssId(c.id) + (c.get && !c.setting ? " is-num" : "") + "'" +
      (on ? " aria-sort='" + (T.sort.dir > 0 ? "ascending" : "descending") + "'" : "") +
      "><button class='rn-sort" + (on ? " is-on" : "") + "' data-sort='" + esc(c.id) + "' title='" + esc(tip) + "'>" +
      (c.extra ? "<span class='rn-xhead'>" + esc(HEADS[c.id] || c.label) + "</span>" : esc(HEADS[c.id] || c.label)) +
      (c.unit ? " <small>" + esc(c.unit) + "</small>" : "") + (dir ? "<span class='rn-dir' aria-hidden='true'>" + dir + "</span>" : "") +
      "<span class='rn-arrow' aria-hidden='true'>" + (on ? (T.sort.dir > 0 ? "▲" : "▼") : "") + "</span></button></th>";
  };
  // The baseline stays at the top while the list scrolls: a copy of its row in
  // the table head (this design only; the real row stays in the list too).
  const b = getRunScope() === "all" ? null : _shown.runs.find((r) => isBaseline(r) && normDir(r._design) === normDir(state.designDir));
  const baseRow = b && archivedOk(b) && _visible.length > 8 ? rowHtml(b, cols, { head: true }) : "";
  return "<thead><tr><th class='rn-th rn-c-pick'><input type='checkbox' class='rn-pick rn-pick-all' aria-label='Select every run shown' title='Select every run shown'/></th>" +
    "<th class='rn-th rn-c-base' title='The baseline of the project: every page measures against it'>★</th>" + cols.map(th).join("") + "</tr>" + baseRow + "</thead>";
}
const cssId = (id) => String(id).replace(/[^A-Za-z0-9_-]/g, "_");

// One item per table row (a run, or a group row with its runs when open).
function buildItems(list) {
  const gr = activeGrouper();
  const groups = new Map();
  const order = [];
  for (const r of list) {
    const gk = gr ? gr.keyOf(r) : null;
    if (gk) {
      if (!groups.has(gk)) { groups.set(gk, { ...gr.make(r), grouper: gr, runs: [] }); order.push({ group: gk }); }
      groups.get(gk).runs.push(r);
    } else order.push({ run: r });
  }
  const items = [];
  for (const o of order) {
    if (o.run) { items.push({ key: keyOf(o.run), run: o.run }); continue; }
    const g = groups.get(o.group);
    gr.order(g);
    const open = _openSweeps.has(o.group);
    items.push({ key: o.group, group: g, gk: o.group, open });
    if (open) for (const r of g.runs) items.push({ key: keyOf(r), run: r, child: g.sw || g });
  }
  return items;
}

function itemHtml(it, cols, i) {
  const html = it.group ? it.group.grouper.header(it.group, cols, it.gk, it.open) : rowHtml(it.run, cols, { child: it.child || null });
  return html.replace(/<tr /g, "<tr data-vi='" + i + "' aria-rowindex='" + (i + 2) + "' ");
}

// ------------------------------------------------------------------ windowed rows
// Past WINDOW_MIN rows only the rows near the view are in the DOM, with a
// spacer row above and below for the rest. Rows are a fixed height (42 px, or
// the compact one), so where a row sits is arithmetic; only an open group's
// summary is measured. Every length here is in layout px (offsetHeight,
// scrollTop, clientHeight), so the app's CSS zoom never skews it. Collapsed
// "sizer" rows carry the widest cell of every column, so the columns do not
// move while scrolling. The focused row is never removed.
const WINDOW_MIN = 100, OVERSCAN = 24, MARGIN = 8;
const _heights = new Map();    // item key -> measured height (open summaries)
let _rowH = 42;                // a run row, as measured
let _model = null;             // { items, cols, a, b, windowed }
const _stash = new Map();      // item key -> its <tr>s, kept while scrolled away

function scrollerOf() { return document.getElementById("rn-wrap"); }

function estimate(it) {
  if (it.group && it.open) return _heights.get(it.key) || _rowH + 260;
  return _rowH;
}
function prefix(m) {
  const cum = new Float64Array(m.items.length + 1);
  for (let i = 0; i < m.items.length; i++) cum[i + 1] = cum[i] + estimate(m.items[i]);
  return cum;
}
// Items [i, j) under the scroll box's viewport.
function visibleRange(table, m, cum) {
  const sc = scrollerOf();
  const head = table.tHead ? table.tHead.offsetHeight : 0;
  const top = sc.scrollTop - head, bot = sc.scrollTop + sc.clientHeight - head;
  const n = m.items.length;
  let lo = 0, hi = n;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (cum[mid + 1] <= top) lo = mid + 1; else hi = mid; }
  let j = lo;
  while (j < n && cum[j] < bot) j++;
  return [Math.min(lo, n), Math.max(j, Math.min(lo + 1, n))];
}
function spacerHtml(which, cols, h) {
  return "<tr class='rn-spacer' data-sp='" + which + "' aria-hidden='true'><td colspan='" + (cols.length + 2) + "' style='height:" + h.toFixed(1) + "px'></td></tr>";
}

function paintTable(table, m) {
  const cols = m.cols, n = m.items.length;
  _stash.clear();
  table.setAttribute("aria-rowcount", String(n + 1));
  m.windowed = n > WINDOW_MIN;
  const focusKey = focusedKey();
  const hadFocus = table.contains(document.activeElement);
  if (!m.windowed) {
    m.a = 0; m.b = n;
    thawCols(table);
    table.innerHTML = headHtml(cols) + "<tbody>" + m.items.map((it, i) => itemHtml(it, cols, i)).join("") + "</tbody>";
    afterInsert(table, m, 0, n);
    restoreFocus(table, focusKey, hadFocus);
    return;
  }
  const cum = prefix(m);
  let a = 0, b = Math.min(n, 40 + OVERSCAN);
  if (table.tBodies[0] && table.offsetParent) {
    const [i, j] = visibleRange(table, m, cum);
    a = Math.max(0, i - OVERSCAN); b = Math.min(n, j + OVERSCAN);
  }
  m.a = a; m.b = b;
  let body = "";
  for (let k = a; k < b; k++) body += itemHtml(m.items[k], cols, k);
  thawCols(table);
  table.innerHTML = headHtml(cols) + "<tbody>" + sizerHtml(m) + spacerHtml("top", cols, cum[a]) + body + spacerHtml("bot", cols, cum[n] - cum[b]) + "</tbody>";
  afterInsert(table, m, a, b);
  wireWindow(table);
  restoreFocus(table, focusKey, hadFocus);
  requestAnimationFrame(() => paintWindow());
}

// A long list's columns, once laid out from the sizer rows, are frozen
// (table-layout: fixed): a scroll then lays out only the rows it adds, not
// every row of the table again (that was 35 to 50 ms a step on a slow PC).
function freezeCols(table) {
  if (!_model || !_model.windowed || !table.tHead) return;
  const ths = [...table.tHead.rows[0].cells];
  const w = ths.map((th) => th.offsetWidth);
  const total = w.reduce((a, b) => a + b, 0);
  if (!total) return;
  table.insertAdjacentHTML("afterbegin", "<colgroup class='rn-colg'>" + w.map((x) => "<col style='width:" + x + "px'>").join("") + "</colgroup>");
  table.style.tableLayout = "fixed";
  table.style.width = total + "px";
}
function thawCols(table) {
  table.style.tableLayout = "";
  table.style.width = "";
}

// The row height and the open summaries' heights, in layout px.
function measure(table, m) {
  const tb = table.tBodies[0];
  if (!tb) return;
  const acc = new Map();
  for (const tr of tb.rows) {
    const vi = tr.dataset.vi;
    if (vi == null) continue;
    acc.set(+vi, (acc.get(+vi) || 0) + tr.offsetHeight);
  }
  for (const [i, h] of acc) {
    const it = m.items[i];
    if (!it || !h) continue;
    if (it.group && it.open) _heights.set(it.key, h);
    else if (!it.group) _rowH = h;
  }
}

let _winRaf = 0;
function paintWindow() {
  _winRaf = 0;
  const m = _model, table = document.getElementById("runs-table");
  if (!m || !m.windowed || !table || !table.tBodies[0] || !table.offsetParent) return;
  measure(table, m);
  const cum = prefix(m), n = m.items.length;
  const [i, j] = visibleRange(table, m, cum);
  const ok = (m.a === 0 || i >= m.a + MARGIN) && (m.b === n || j <= m.b - MARGIN);
  if (!ok) moveWindow(table, m, Math.max(0, i - OVERSCAN), Math.min(n, j + OVERSCAN));
  setSpacers(table, m, cum);
}
function setSpacers(table, m, cum) {
  const n = m.items.length;
  const top = table.querySelector("tr[data-sp='top'] > td"), bot = table.querySelector("tr[data-sp='bot'] > td");
  if (top) top.style.height = cum[m.a].toFixed(1) + "px";
  if (bot) bot.style.height = (cum[n] - cum[m.b]).toFixed(1) + "px";
}
function htmlOf(m, from, to) {
  let s = "";
  for (let k = from; k < to; k++) if (!_stash.has(m.items[k].key)) s += itemHtml(m.items[k], m.cols, k);
  return s;
}
// Draw items [a, b): drop the rows that left, add the ones that came in.
function moveWindow(table, m, a, b) {
  const tb = table.tBodies[0];
  const topSp = tb.querySelector("tr[data-sp='top']"), botSp = tb.querySelector("tr[data-sp='bot']");
  if (!topSp || !botSp || (a === m.a && b === m.b)) return;
  const keep = (k) => k >= a && k < b;
  const act = document.activeElement;
  for (const tr of [...tb.rows]) {
    const vi = tr.dataset.vi;
    if (vi == null || keep(+vi)) continue;
    const it = m.items[+vi];
    // An open group's summary keeps its rows, and the focused row stays drawn
    // (moved out of the way) so the keyboard never loses its place.
    if (it && ((it.group && it.open) || (act && tr.contains(act)))) {
      const list = _stash.get(it.key) || [];
      list.push(tr);
      _stash.set(it.key, list);
      if (act && tr.contains(act)) { tr.classList.add("rn-kept"); continue; }
    }
    tr.remove();
  }
  const overlap = a < m.b && b > m.a;
  const ins = (anchor, where, from, to) => {
    if (from >= to) return;
    anchor.insertAdjacentHTML(where, htmlOf(m, from, to));
    for (let k = from; k < to; k++) restore(tb, m, k);
  };
  if (overlap) {
    ins(topSp, "afterend", a, Math.min(m.a, b));
    ins(botSp, "beforebegin", Math.max(m.b, a), b);
  } else ins(botSp, "beforebegin", a, b);
  const was = [m.a, m.b];
  m.a = a; m.b = b;
  paintViewing(true);
  const fresh = [];
  for (let k = a; k < b; k++) if (!overlap || k < was[0] || k >= was[1]) fresh.push(k);
  if (fresh.length) afterInsert(table, m, fresh[0], fresh[fresh.length - 1] + 1, new Set(fresh));
}
function restore(tb, m, k) {
  const it = m.items[k];
  const list = _stash.get(it.key);
  if (!list) return;
  _stash.delete(it.key);
  let before = null;
  for (let p = k - 1; p >= 0 && !before; p--) {
    const rows = tb.querySelectorAll("tr[data-vi='" + p + "']:not(.rn-kept)");
    if (rows.length) before = rows[rows.length - 1];
  }
  if (!before) before = tb.querySelector("tr[data-sp='top']");
  let at = before;
  for (const tr of list) { tr.classList.remove("rn-kept"); at.after(tr); at = tr; }
  it._kept = true;
}

// After rows are drawn: the open groups' summaries.
function afterInsert(table, m, from, to, only = null) {
  for (let k = from; k < to; k++) {
    if (only && !only.has(k)) continue;
    const it = m.items[k];
    if (it._kept) { it._kept = false; continue; }
    if (it.group && it.open && it.group.grouper.summary) {
      const el = table.querySelector(".rn-sw-sum[data-sum='" + CSS.escape(it.gk) + "']");
      if (el && it.group.runs.length) it.group.grouper.summary(el, it.group);
    }
  }
  // Rows are drawn with their ticks; a group's dash (indeterminate) can only
  // be set from script. A scroll touches the new rows only.
  if (!only) { syncPickBoxes(); return; }
  for (const k of only) {
    const it = m.items[k];
    if (!it || !it.group) continue;
    const cb = table.querySelector("tr.rn-sweep[data-vi='" + k + "']:not(.rn-sizer) input.rn-pick-sweep");
    if (cb) markBox(cb, it.group.runs);
  }
}

// The widest cell of each column, from every row: up to three collapsed rows.
function cellWidthKey(c, it) {
  const r = it.run;
  if (c.id === "verdict") { const v = verdictOf(r); return String(v.label || v.state).length; }
  if (c.id === "when") return when(r.started_at).length;
  if (c.id === "origin") return originText(r).length;
  if (c.id === "design") return designLabel(r._design).length;
  if (c.id === "pdk") return (r.pdk || "").length;
  if (c.id === "note") return Math.min(26, (r.note || "").split("\n")[0].length);
  if (c.id === "tags") return Math.min(30, (r.tags || []).join(" · ").length);
  if (c.id === "changed" || c.id === "checks") return Math.min(c.id === "changed" ? 26 : 99, cellHtml(c, r, baselineRowFor(r)).replace(/<[^>]*>/g, "").length);
  if (TIMING.has(c.id) && untimed(r)) return 9;
  const v = c.get ? c.get(r) : null;
  if (v == null) return 0;
  const neg = typeof v === "number" && v < 0 ? 0.5 : 0;
  return Math.min(40, String(c.f ? c.f(v) : v).length) + neg;
}
function nameWidthKey(it, cols) {
  const r = it.run;
  const vals = it.child && it.child.axes ? pointValues(r, it.child) : null;
  return r.tag.length + (vals ? valuesText(vals).length * 0.9 + 1 : 0) + (r.pinned ? 2 : 0) + (r.imported ? 9 : 0) +
    (r.note && !cols.some((c) => c.id === "note") ? 2 : 0) + (isViewingRun(r) ? 7 : 0) + (vals ? 3 : 0) +
    (isBaseline(r) ? 9 : 0) + (r.archived ? 9 : 0) + (startedBy(r) === "terminal" ? 9 : 0) +
    (originOf(r).kind === "fix" ? 8 + String(originOf(r).parent || "").length * 0.6 : 0) +
    (!cols.some((c) => c.id === "tags") ? Math.min(2, (r.tags || []).length) * 8 + ((r.tags || []).length > 2 ? 3 : 0) : 0);
}
function isViewingRun(r) { return r.tag === contextRun() && normDir(r._design) === normDir(contextDesign()); }

function sizerHtml(m) {
  const cols = m.cols, N = 3;
  _viewingKey = normDir(contextDesign()) + "|" + contextRun();
  const top = new Map(cols.map((c) => [c.id, []]));
  const push = (id, score, it) => {
    const l = top.get(id);
    if (!score) return;
    if (l.length < N || score > l[l.length - 1][0]) {
      l.push([score, it]); l.sort((x, y) => y[0] - x[0]); if (l.length > N) l.pop();
    }
  };
  top.set("_group", []);
  for (const it of m.items) {
    if (it.group) { push("_group", groupNameText(it.group).length, it); continue; }
    for (const c of cols) push(c.id, c.id === "name" ? nameWidthKey(it, cols) : cellWidthKey(c, it), it);
  }
  let out = "";
  for (let k = 0; k < N; k++) {
    let any = false;
    const tds = cols.map((c) => {
      const p = top.get(c.id)[k];
      if (!p) return "<td class='rn-c-" + cssId(c.id) + "'></td>";
      any = true;
      const it = p[1];
      if (c.id === "name") return nameCellHtml(it.run, cols, it.child || null, isViewingRun(it.run));
      return cellHtml(c, it.run, baselineRowFor(it.run));
    }).join("");
    if (any) out += "<tr class='rn-sizer' aria-hidden='true'><td class='rn-c-pick'></td><td class='rn-c-base'></td>" + tds + "</tr>";
  }
  const g = top.get("_group")[0];
  if (g) out += g[1].group.grouper.header(g[1].group, cols, g[1].gk, false).replace(/<tr class='/, "<tr aria-hidden='true' class='rn-sizer ");
  return out;
}
function repaintSizer() {
  const m = _model, table = document.getElementById("runs-table");
  if (!m || !m.windowed || !table || !table.tBodies[0]) return;
  const tb = table.tBodies[0];
  tb.querySelectorAll("tr.rn-sizer").forEach((tr) => tr.remove());
  tb.insertAdjacentHTML("afterbegin", sizerHtml(m));
}

let _winWired = null;
function wireWindow(table) {
  const sc = scrollerOf();
  if (_winWired !== sc) {
    if (_winWired) _winWired.removeEventListener("scroll", onWinScroll);
    sc.addEventListener("scroll", onWinScroll, { passive: true });
    _winWired = sc;
  }
}
function onWinScroll() {
  if (!_winRaf && _model && _model.windowed) _winRaf = requestAnimationFrame(paintWindow);
}

function cellHtml(c, r, base) {
  const cls = "rn-c-" + cssId(c.id);
  if (c.id === "verdict") {
    const v = verdictOf(r);
    return "<td class='" + cls + "'><span class='rn-v is-" + (VERDICT_TONE[v.state] || "warn") + "'><i aria-hidden='true'></i>" + esc(v.label || v.state) + "</span></td>";
  }
  if (c.id === "name") return "";        // drawn by rowHtml
  if (c.id === "when") return "<td class='" + cls + "' title='" + esc(fullWhen(r.started_at)) + "'>" + esc(when(r.started_at)) + "</td>";
  if (c.id === "origin") return "<td class='" + cls + "'>" + esc(originText(r).startsWith("fix of") ? "fix" : originText(r)) + "</td>";
  if (c.id === "design") return "<td class='" + cls + "'>" + esc(designLabel(r._design)) + "</td>";
  if (c.id === "pdk") return "<td class='" + cls + "'>" + esc(r.pdk || "") + "</td>";
  if (c.id === "note") return "<td class='" + cls + "' title='" + esc(r.note || "") + "'>" + esc((r.note || "").split("\n")[0]) + "</td>";
  if (c.id === "tags") return "<td class='" + cls + "'>" + (r.tags || []).map((t) => "<span class='rn-tagchip is-sm'>" + esc(t) + "</span>").join("") + "</td>";
  if (c.id === "changed") {
    const ch = CHANGES.get(keyOf(r));
    const bt = projectBaseline(r._design);
    if (!bt) return "<td class='" + cls + " muted'></td>";
    if (bt === r.tag) return "<td class='" + cls + "'><span class='rn-basetag'>baseline</span></td>";
    if (!ch) return "<td class='" + cls + " muted'>…</td>";
    if (!ch.length) return "<td class='" + cls + " muted'>same settings</td>";
    const txt = ch.slice(0, 2).map((x) => x.var + " " + short(x.from) + "→" + short(x.to)).join(", ") + (ch.length > 2 ? " +" + (ch.length - 2) : "");
    return "<td class='" + cls + "' title='" + esc(ch.map((x) => x.var + ": " + x.from + " → " + x.to).join("\n")) + "'>" + esc(txt) + "</td>";
  }
  if (c.id === "checks") {
    const k = checksOf(r);
    if (!k) return "<td class='" + cls + " is-num muted'></td>";
    const dd = r.verdict && r.verdict.drc_disagree;
    if (!k.total && dd && dd.markers) {
      return "<td class='" + cls + " is-num'><span class='rn-clean'>clean</span> <button class='rn-drcflag' data-act='drc' title='" +
        esc("The DRC report files list " + dd.markers + " markers but LibreLane's metric says 0. Open Verify's Design rules.") + "'>· report: " + dd.markers + "</button></td>";
    }
    if (!k.total) return "<td class='" + cls + " is-num'><span class='rn-clean'>clean</span></td>";
    const parts = [k.drc ? "DRC " + k.drc : "", k.lvs ? "LVS " + k.lvs : "", k.ant ? "ant " + k.ant : ""].filter(Boolean);
    return "<td class='" + cls + " is-num'><span class='rn-bad'>" + esc(parts.join(" · ")) + "</span></td>";
  }
  // A run no clock reached: its slack says nothing (OpenSTA timed no path).
  if ((TIMING.has(c.id) || c.timing) && untimed(r)) return "<td class='" + cls + " is-num muted rn-untimed' title='" + esc(UNTIMED_TIP) + "'>not timed</td>";
  if (c.extra && c.get(r) === undefined && !XV.has(r.run_dir)) return "<td class='" + cls + (c.setting ? "" : " is-num") + " muted'>…</td>";
  const v = c.get(r);
  if (v == null) return "<td class='" + cls + (c.setting ? "" : " is-num") + " muted'></td>";
  if (c.setting) return "<td class='" + cls + " rn-setcell' title='" + esc(c.label + " = " + showSetting(v)) + "'>" + esc(short(showSetting(v), 22)) + "</td>";
  let tone = "", delta = "";
  // Only finished runs are measured against the baseline (a stopped run is "faster").
  if (c.dir && base && finished(r) && finished(base) && !untimed(base)) {
    const b = c.get(base);
    if (b != null) {
      const dl = deltaOfCol(c, v, b);
      if (dl && !dl.same) { tone = " " + dl.cls; delta = "\n" + MF.deltaWords(dl, "the baseline " + base.tag) + " (" + (c.f ? c.f(b) : b) + ")"; }
    }
  }
  const neg = (TIMING.has(c.id) || c.timing) && v < 0 ? " is-neg" : "";
  return "<td class='" + cls + " is-num" + tone + neg + "' title='" + esc(c.label + ": " + exactOf(c, v) + delta) + "'>" + esc(c.f ? c.f(v) : v) + "</td>";
}
const short = (s, n = 14) => { s = String(s ?? "—"); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

function rowHtml(r, cols, { child = null, head = false } = {}) {
  const k = keyOf(r);
  const base = baselineRowFor(r);
  const isBase = isBaseline(r);
  const name = nameCellHtml(r, cols, child);
  const cells = cols.map((c) => (c.id === "name" ? name : cellHtml(c, r, base))).join("");
  const p = _picked.has(k);
  return "<tr class='rn-row" + (child ? " is-child" : "") + (head ? " rn-baserow" : "") + (r.archived ? " is-archived" : "") + (p ? " is-picked" : "") + (_open === k ? " is-open" : "") +
    "' data-key='" + esc(k) + "' data-tag='" + esc(r.tag) + "' data-design='" + esc(r._design || "") + "'" + (head ? "" : " tabindex='-1'") + ">" +
    "<td class='rn-c-pick'>" + (head ? "" : "<input type='checkbox' class='rn-pick' tabindex='-1' aria-label='Select " + esc(r.tag) + "'" + (p ? " checked" : "") + "/>") + "</td>" +
    "<td class='rn-c-base'><button class='rn-star" + (isBase ? " is-on" : "") + "' data-act='base' tabindex='-1' aria-pressed='" + isBase + "' title='" +
      (isBase ? "The baseline of " + esc(designLabel(r._design)) + ": every page measures against it. Click to clear." : "Make " + esc(r.tag) + " the baseline: every page compares with it") + "'>" + icon("star", { size: 13 }) + "</button></td>" +
    cells + "</tr>";
}

// viewing: show the "shown" chip as the row that has it would (sizer rows only).
function nameCellHtml(r, cols, child, viewing = false) {
  const k = keyOf(r);
  const vals = child && child.axes ? pointValues(r, child) : null;
  const o = originOf(r);
  const tags = r.tags || [];
  const tagCol = cols.some((c) => c.id === "tags");
  return "<td class='rn-c-name'><div class='rn-name'>" +
    "<button class='rn-exp' data-act='expand' tabindex='-1' aria-expanded='" + (_open === k) + "' title='Numbers, settings and actions' aria-label='Details of " + esc(r.tag) + "'>" + icon("chevron", { size: 12 }) + "</button>" +
    "<span class='rn-tag'>" + esc(r.tag) + "</span>" +
    (vals ? Object.entries(vals).map(([kk, x]) => "<span class='rn-kv'>" + esc(kk) + " <b>" + esc(x) + "</b></span>").join("") : "") +
    (isBaseline(r) ? "<span class='rn-basetag' title='The baseline of this project: every page measures against it'>baseline</span>" : "") +
    (o.kind === "fix" ? "<span class='rn-chip rn-fixchip' title='" + esc("Resumed from " + o.parent + (o.stage ? " at " + FS.stageLabel(o.stage) : "") +
      ((o.changes || []).length ? " with " + o.changes.map((c) => c.var + " " + c.from + " → " + c.to).join(", ") : "")) + "'>fix of " + esc(short(o.parent, 18)) + "</span>" : "") +
    (startedBy(r) === "terminal" && !r.imported ? "<span class='rn-chip' title='Started in a terminal, not by LanEx'>terminal</span>" : "") +
    (r.imported ? "<span class='rn-chip'>imported</span>" : "") +
    (r.archived ? "<span class='rn-chip rn-archchip' title='Archived: hidden from Runs, Compare and the run picker by default. Nothing was deleted.'>archived</span>" : "") +
    (r.pinned ? "<span class='rn-pin' title='Pinned'>" + icon("pin", { size: 12 }) + "</span>" : "") +
    (!tagCol && tags.length ? tags.slice(0, 2).map((t) => "<span class='rn-tagchip is-sm'>" + esc(t) + "</span>").join("") + (tags.length > 2 ? "<span class='rn-more' title='" + esc(tags.join(", ")) + "'>+" + (tags.length - 2) + "</span>" : "") : "") +
    (r.note && !cols.some((c) => c.id === "note") ? "<span class='rn-hasnote' title='" + esc(r.note) + "'>" + icon("file", { size: 11 }) + "</span>" : "") +
    "<span class='rn-viewing' title='Flow, Layout, Analytics and Verify show this run'" + (viewing ? " style='display:inline-block'" : "") + ">shown</span></div></td>";
}

// "CLOCK_PERIOD 10, 9, 8": the values each setting tried, in the sweep's
// order; a long list keeps its first three and its last ("40, 45, 50 … 80,
// 9 values"). Axes joined by ×.
export function sweepAxesText(sw) {
  return (sw.axes || []).map((a) => {
    const vs = (a.values || []).map(String);
    const list = vs.length > 5 ? vs.slice(0, 3).join(", ") + " … " + vs[vs.length - 1] + ", " + vs.length + " values" : vs.join(", ");
    return a.var + " " + list;
  }).join(" × ") || "settings";
}
const groupNameText = (g) => (g.kind === "sweep" ? "Sweep " + sweepAxesText(g.sw) : "Setting " + (g.key || "") + " = " + showSetting(g.value)) + " 00";

// A sweep's row lines up with the columns: how many runs and how they went
// under Result, and under each number the best of its clean runs (hover says
// which run). Its runs, and the summary, show when it is open.
function sweepRowHtml(g, cols, gk, open) {
  const sw = g.sw, rs = g.runs;
  const failed = rs.filter((r) => verdictOf(r).state === "failed");
  const reasons = [...new Set(failed.map((r) => failCode(r)).filter(Boolean))];
  const passed = rs.filter((r) => verdictOf(r).state === "passed");
  const running = !!sw.active || rs.some((r) => verdictOf(r).state === "running");
  const reruns = sw.reruns || {};
  const have = new Set(rs.map((r) => r.tag));
  const planned = Number.isFinite(sw.count) ? sw.count - Object.keys(reruns).length : NaN;
  const ran = rs.length - rs.filter((r) => reruns[r.tag] && have.has(reruns[r.tag])).length;
  const isShort = Number.isFinite(planned) && ran < planned;
  const stopped = rs.some((r) => verdictOf(r).state === "stopped");
  const end = (sw.ended && sw.ended.reason) || "";
  const cut = end === "interrupted" && rs.some((r) => verdictOf(r).state === "incomplete");
  const [why, word] = running ? ["Running now", ""] : !isShort ? (cut ? ["LanEx was closed or restarted while a point ran", "interrupted"] : ["", ""])
    : end === "stopped" ? ["You stopped it", "stopped"]
    : end === "first-clean" ? ["Stopped at the first clean run, as asked", "first clean"]
    : end === "failed-fast" ? [/^(the first point|two of the first)/.test(sw.ended.detail || "") ? "Stopped early: " + sw.ended.detail
      : "Stopped: two points failed the same way before any step ran (" + (sw.ended.detail || "") + "), so the rest would too", "failed early"]
    : end === "done" ? ["Some of its runs were deleted", "runs deleted"]
    : end === "interrupted" && !sw.ended.assumed ? ["LanEx was closed or restarted while it ran", "interrupted"]
    : stopped ? ["It was stopped", "stopped"]
    : sw.stop_when_good && passed.length ? ["Stopped at the first clean run, as asked", "first clean"]
    : ["LanEx was closed or restarted while it ran", "interrupted"];
  const tone = running ? "live" : failed.length && !passed.length ? "fail" : failed.length || isShort ? "warn" : passed.length ? "pass" : "warn";
  const count = ran + (isShort ? " of " + planned : "") + " run" + ((isShort ? planned : ran) === 1 ? "" : "s");
  // One mark per point, in value order, planned points included: the study's
  // strip (studymodel.js), the same marks as its summary.
  const jobs = SQ.jobsOf(sw.id);
  const marks = rs.map((r) => ({ tag: r.tag, v: pointValues(r, sw), key: SM.pointKey({ run: { ...r, verdict: verdictOf(r) }, job: jobs.get(r.tag) || null, active: !!sw.active }) }));
  (sw.tags || []).forEach((t, i) => { if (!have.has(t) && !(reruns[t] && have.has(reruns[t]))) marks.push({ tag: t, v: (sw.points || [])[i] || null, key: SM.pointKey({ run: null, job: jobs.get(t) || null, active: !!sw.active }) }); });
  const ax = (sw.axes || []).map((a) => a.var);
  const nv = (p) => ax.map((k) => (p.v ? Number(p.v[k]) : NaN));
  marks.sort((a, b) => { const x = nv(a), y = nv(b); for (let i = 0; i < x.length; i++) if (x[i] !== y[i] && Number.isFinite(x[i]) && Number.isFinite(y[i])) return x[i] - y[i]; return 0; });
  for (const p of marks) p.label = p.v ? Object.values(p.v).join(" × ") : p.tag;
  const strip = marks.length > 1 ? SM.stripHtml(marks, esc) : "";
  const verdictCell = strip + "<span class='rn-v is-" + tone + "'><i aria-hidden='true'></i>" + esc(count) +
    (failed.length ? "<span class='rn-sw-n'>· <span class='rn-bad'>" + failed.length + " failed</span></span>" : word ? "<span class='rn-sw-n'>· " + esc(word) + "</span>" : "") + "</span>";
  return groupRowHtml(g, cols, gk, open, {
    tag: "Sweep", title: sweepAxesText(sw), why, verdictCell,
    when: "<span class='rn-sw-when" + (sw.created_at_note ? " is-assumed" : "") + "'" + (sw.created_at_note ? " title='" + esc(sw.created_at_note) + "'" : "") + ">" + esc(when(sw.created_at)) + "</span>",
    origin: "sweep", reasons,
  });
}

// One group row (any grouper): a tag and title in the name cell, how its runs
// went under Result, the best clean value under each number.
function groupRowHtml(g, cols, gk, open, { tag, title, why = "", verdictCell = "", when: whenCell = "", origin = "", reasons = null }) {
  const rs = g.runs;
  const failed = rs.filter((r) => verdictOf(r).state === "failed");
  const passed = rs.filter((r) => verdictOf(r).state === "passed");
  const rsn = reasons || [...new Set(failed.map((r) => failCode(r)).filter(Boolean))];
  const picked = rs.filter((r) => _picked.has(keyOf(r))).length;
  const vcell = verdictCell || "<span class='rn-v is-" + (failed.length && !passed.length ? "fail" : failed.length ? "warn" : "pass") + "'><i aria-hidden='true'></i>" +
    esc(plural(rs.length, "run")) + (failed.length ? "<span class='rn-sw-n'>· <span class='rn-bad'>" + failed.length + " failed</span></span>" : "") + "</span>";
  const cell = (c) => {
    const cls = "rn-c-" + cssId(c.id);
    if (c.id === "name") {
      return "<td class='" + cls + "'><div class='rn-name'>" +
        "<button class='rn-exp' data-act='group' tabindex='-1' aria-expanded='" + open + "' aria-label='" + (open ? "Hide" : "Show") + " the runs of this group'>" + icon("chevron", { size: 12 }) + "</button>" +
        "<span class='rn-swtag'>" + esc(tag) + "</span><span class='rn-sw-axes'>" + esc(title) + "</span></div></td>";
    }
    if (c.id === "verdict") return "<td class='" + cls + "'" + (why ? " title='" + esc(why) + "'" : "") + ">" + vcell + "</td>";
    if (c.id === "when") {
      const newest = rs.map((r) => r.started_at || "").sort().pop();
      return "<td class='" + cls + "'>" + (whenCell || esc(when(newest))) + "</td>";
    }
    if (c.id === "origin") return "<td class='" + cls + "'>" + esc(origin) + "</td>";
    if (c.id === "design") return "<td class='" + cls + "'>" + esc(designLabel(rs[0] && rs[0]._design)) + "</td>";
    if (c.id === "checks") {
      if (failed.length) return "<td class='" + cls + " is-num'><span class='rn-bad'>" + esc(rsn.length ? "failed: " + rsn.join(", ") : failed.length + " failed") + "</span></td>";
      const ks = passed.map(checksOf).filter(Boolean);
      return "<td class='" + cls + " is-num'>" + (ks.length && ks.every((k) => !k.total) ? "<span class='rn-clean'>clean</span>" : "") + "</td>";
    }
    if (c.id === "runtime") {
      const t = rs.map((r) => num(r.wall_time_s)).filter((v) => v != null);
      return "<td class='" + cls + " is-num rn-best'" + (t.length ? " title='" + esc("All " + rs.length + " runs together") + "'" : "") + ">" + (t.length ? esc(fmtDur(t.reduce((a, b) => a + b, 0))) : "") + "</td>";
    }
    if (c.dir && c.get) {
      let best = null;
      for (const r of passed) {
        if ((TIMING.has(c.id) || c.timing) && untimed(r)) continue;
        const v = c.get(r); if (v != null && (!best || (c.dir > 0 ? v > best.v : v < best.v))) best = { v, r };
      }
      if (!best) return "<td class='" + cls + " is-num'></td>";
      return "<td class='" + cls + " is-num rn-best' title='" + esc("Best of this group's clean runs: " + best.r.tag) + "'>" + esc(c.f ? c.f(best.v) : best.v) + "</td>";
    }
    return "<td class='" + cls + (c.get && !c.setting ? " is-num" : "") + "'></td>";
  };
  const n = cols.length + 2;
  return "<tr class='rn-sweep" + (open ? " is-open" : "") + "' data-group='" + esc(gk) + "' tabindex='-1'><td class='rn-c-pick'>" +
    "<input type='checkbox' class='rn-pick rn-pick-sweep' tabindex='-1' aria-label='Select the runs of this group'" + (picked && picked === rs.length ? " checked" : "") + "/></td><td class='rn-c-base'></td>" +
    cols.map(cell).join("") + "</tr>" +
    (open && g.grouper.summary ? "<tr class='rn-sweep-sum'><td colspan='" + n + "'><div class='rn-sw-sum' data-sum='" + esc(gk) + "'></div></td></tr>" : "");
}

// "DPL-0036": the error code in the failed run's reason, else its stage.
function failCode(r) {
  const sw = sweepOf(r);
  const res = sw && sw.results && sw.results[r.tag];
  const rx = RX.get(keyOf(r));
  const text = (res && res.reason) || (rx && ((rx.card && rx.card.title) || (rx.errors || [])[0])) || "";
  const m = /\b([A-Z]{2,4}-\d{4})\b/.exec(text) || /\b([A-Z]{2,4}-\d{4})\b/.exec(((rx && rx.errors) || []).join(" "));
  if (m) return m[1];
  const st = (res && res.stage) || (rx && rx.stage) || (r.verdict && r.verdict.stage);
  return st ? FS.stageLabel(st) : "";
}

// "today 19:22", "yesterday 19:22", then the date and time ("11 Nov 19:22"):
// relative words past yesterday make a long history read all the same.
const _whenMemo = new Map();
function when(iso) {
  if (!iso) return "";
  // A day's words change at midnight: the memo is per day.
  const day = new Date().toDateString();
  if (_whenMemo.get("") !== day) { _whenMemo.clear(); _whenMemo.set("", day); }
  let w = _whenMemo.get(iso);
  if (w === undefined) { w = whenNow(iso); if (_whenMemo.size < 20000) _whenMemo.set(iso, w); }
  return w;
}
function whenNow(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const hm = localeTime(d, [], { hour: "2-digit", minute: "2-digit" });
  const days = Math.floor((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
  if (days === 0) return "today " + hm;
  if (days === 1) return "yesterday " + hm;
  if (d.getFullYear() !== now.getFullYear()) return localeDate(d, [], { day: "numeric", month: "short", year: "numeric" });
  return localeDate(d, [], { day: "numeric", month: "short" }) + " " + hm;
}
function fullWhen(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(d)) return iso || "";
  return localeDate(d, [], { weekday: "short", day: "numeric", month: "short", year: "numeric" }) + ", " + localeTime(d, [], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ------------------------------------------------------------------ table events
function runOfRow(tr) {
  if (!tr || !tr.dataset.key) return null;
  return _shown.runs.find((r) => keyOf(r) === tr.dataset.key) || null;
}
function itemOfRow(tr) {
  if (!tr || !_model) return null;
  const vi = tr.dataset.vi;
  if (vi != null && _model.items[+vi]) return _model.items[+vi];
  if (tr.dataset.group) return _model.items.find((x) => x.group && x.gk === tr.dataset.group) || null;
  return _model.items.find((x) => !x.group && x.key === tr.dataset.key) || null;
}
// The runs in display order (a group's runs where the group sits), for ranges.
function orderedRuns() {
  const out = [];
  for (const it of (_model ? _model.items : [])) {
    if (it.group) { if (!it.open) out.push(...it.group.runs); }
    else out.push(it.run);
  }
  return out;
}
function tickRange(toKey) {
  const list = orderedRuns();
  const a = list.findIndex((r) => keyOf(r) === _anchor), b = list.findIndex((r) => keyOf(r) === toKey);
  if (a < 0 || b < 0) return false;
  const [lo, hi] = a < b ? [a, b] : [b, a];
  for (let i = lo; i <= hi; i++) _picked.add(keyOf(list[i]));
  return true;
}

function wireTable(table) {
  table.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.sort;
    const c = allCols().find((x) => x.id === id);
    const textual = ["name", "pdk", "origin", "design", "note", "tags"].includes(id) || (c && c.setting);
    setT({ sort: T.sort.col === id ? { col: id, dir: -T.sort.dir } : { col: id, dir: textual ? 1 : -1 } });
  }));
  if (table._wiredRows) return;
  table._wiredRows = true;
  table.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("tr.rn-sweep-sum")) return;
    if (t.classList && t.classList.contains("rn-pick-all")) { pickRuns(_visible, t.checked); return; }
    if (t.classList && t.classList.contains("rn-pick-sweep")) {
      const it = itemOfRow(t.closest("tr.rn-sweep"));
      if (it) pickRuns(it.group.runs, t.checked);
      return;
    }
    const grp = t.closest("tr.rn-sweep");
    if (grp) { toggleGroup(grp.dataset.group); return; }
    const tr = t.closest("tr[data-key]");
    const r = runOfRow(tr);
    if (!r) return;
    if (t.closest(".rn-pick")) {
      const k = keyOf(r);
      // Shift+click ticks every run between the last ticked one and this one.
      if (e.shiftKey && _anchor && tickRange(k)) { t.checked = true; }
      else if (t.checked) _picked.add(k); else _picked.delete(k);
      _anchor = k;
      syncSelection();
      return;
    }
    const act = t.closest("[data-act]")?.dataset.act;
    if (act === "base") { toggleBaseline(r); return; }
    if (act === "expand") { toggleOpen(r); return; }
    if (act === "drc") { e.stopPropagation(); Promise.resolve(setRun(r.tag, r._design)).then(() => go("verify", { section: "vf-drc" })); return; }
    focusRow(tr);
    setRun(r.tag, r._design);
    if (_open && _open !== keyOf(r)) openSide(r);
  });
  table.addEventListener("dblclick", (e) => {
    const r = runOfRow(e.target.closest("tr[data-key]"));
    if (r && !e.target.closest(".rn-pick, [data-act]")) toggleOpen(r);
  });
  table.addEventListener("keydown", onTableKey);
  table.addEventListener("focusin", (e) => {
    const tr = e.target.closest && e.target.closest("tr[data-vi]");
    if (tr) setRoving(tr);
  });
}

function toggleGroup(gk) {
  if (_openSweeps.has(gk)) _openSweeps.delete(gk); else _openSweeps.add(gk);
  safeStorage.setJSON(OPEN_KEY, [..._openSweeps].slice(-60));
  renderRuns({ cached: true });
}

async function toggleBaseline(r) {
  const was = isBaseline(r);
  try {
    await setProjectBaseline(r._design, was ? "" : r.tag);
    toast.show(was ? "No baseline now: each page compares with the last clean run before the one it shows."
      : r.tag + " is the baseline of " + designLabel(r._design) + ": Runs, Analytics, Verify and Compare measure against it.", "success", 3500);
  } catch (ex) {
    toast.show("Couldn't save the baseline: " + (ex.message || ex), "error");
  }
  CHANGES.clear(); _changesKey = "";
  renderRuns({ cached: true });
}

// ---- keyboard: the table is one Tab stop; arrows move, Space ticks.
let _focusKey = null;
function focusedKey() {
  const a = document.activeElement;
  const tr = a && a.closest && a.closest("#runs-table tr[data-vi]");
  return tr ? (tr.dataset.group || tr.dataset.key) : _focusKey;
}
function setRoving(tr) {
  const table = document.getElementById("runs-table");
  table.querySelectorAll("tr[tabindex='0']").forEach((x) => { if (x !== tr) x.setAttribute("tabindex", "-1"); });
  tr.setAttribute("tabindex", "0");
  _focusKey = tr.dataset.group || tr.dataset.key;
}
function focusRow(tr) { if (tr) { setRoving(tr); if (document.activeElement !== tr) tr.focus({ preventScroll: true }); } }
// After a redraw: the same row keeps the table's one Tab stop, and the focus
// when the table had it (a tick, a sort by keyboard).
function restoreFocus(table, key, hadFocus) {
  const rows = table.tBodies[0] ? [...table.tBodies[0].querySelectorAll("tr[data-vi]:not(.rn-sizer):not(.rn-sweep-sum)")] : [];
  const want = key && rows.find((tr) => (tr.dataset.group || tr.dataset.key) === key);
  const tr = want || rows[0];
  if (!tr) return;
  tr.setAttribute("tabindex", "0");
  if (want && hadFocus) tr.focus({ preventScroll: true });
}
let _kbd = false;
function rowAt(vi) { return document.querySelector("#runs-table tr[data-vi='" + vi + "']:not(.rn-sizer):not(.rn-sweep-sum)"); }
function goTo(vi) {
  const m = _model;
  if (!m) return;
  vi = Math.max(0, Math.min(m.items.length - 1, vi));
  const sc = scrollerOf();
  const cum = prefix(m);
  const head = document.getElementById("runs-table").tHead.offsetHeight;
  const top = cum[vi] + head, bot = cum[vi] + estimate(m.items[vi]) + head;
  if (top < sc.scrollTop + head) sc.scrollTop = Math.max(0, top - head - 4);
  else if (bot > sc.scrollTop + sc.clientHeight) sc.scrollTop = bot - sc.clientHeight + 4;
  if (m.windowed) paintWindow();
  const tr = rowAt(vi);
  if (tr) { _kbd = true; focusRow(tr); _kbd = false; }
}
function onTableKey(e) {
  const tr = e.target.closest && e.target.closest("tr[data-vi]");
  if (!tr || e.target.closest("input:not(.rn-pick), textarea, select")) return;
  if (e.target !== tr && !e.target.classList.contains("rn-pick") && e.key !== "Escape") {
    // A button inside the row keeps its own Enter/Space.
    if (e.key === "Enter" || e.key === " ") return;
  }
  const vi = +tr.dataset.vi;
  const it = itemOfRow(tr);
  const r = it && !it.group ? it.run : null;
  const k = e.key;
  if (k === "ArrowDown" || k === "ArrowUp") {
    e.preventDefault();
    const next = vi + (k === "ArrowDown" ? 1 : -1);
    if (e.shiftKey && r) {
      const nit = _model.items[next];
      if (nit && !nit.group) { if (!_anchor) _anchor = keyOf(r); _picked.add(keyOf(r)); _picked.add(nit.key); syncSelection(); }
    }
    goTo(next);
  } else if (k === "Home" || k === "End") { e.preventDefault(); goTo(k === "Home" ? 0 : _model.items.length - 1); }
  else if (k === "PageDown" || k === "PageUp") { e.preventDefault(); const step = Math.max(1, Math.floor(scrollerOf().clientHeight / _rowH) - 1); goTo(vi + (k === "PageDown" ? step : -step)); }
  else if (k === " " || (k.toLowerCase() === "x" && singleKeysOn() && !e.ctrlKey && !e.metaKey)) {
    e.preventDefault();
    if (it && it.group) { const all = it.group.runs.every((x) => _picked.has(keyOf(x))); pickRuns(it.group.runs, !all); return; }
    if (!r) return;
    const kk = keyOf(r);
    if (e.shiftKey && _anchor) tickRange(kk);
    else if (_picked.has(kk)) _picked.delete(kk); else _picked.add(kk);
    _anchor = kk;
    syncSelection();
  } else if (k === "Enter") {
    e.preventDefault();
    if (it && it.group) toggleGroup(it.gk); else if (r) toggleOpen(r);
  } else if ((k === "a" || k === "A") && (e.ctrlKey || e.metaKey)) { e.preventDefault(); pickRuns(_visible, true); }
  else if (k === "Escape" && !_open && _picked.size && !layerCount()) { e.preventDefault(); _picked.clear(); syncSelection(); }
  else if (k === "Delete") {
    e.preventDefault();
    const sel = pickedRuns().length ? pickedRuns() : r ? [r] : [];
    if (!sel.length) return;
    if (e.shiftKey) deleteRuns(sel); else archiveRuns(sel, !sel.every((x) => x.archived));
  } else if (k === "f" && (e.ctrlKey || e.metaKey)) {
    // The browser's find can't see rows that aren't drawn: the search can.
    e.preventDefault(); document.getElementById("runs-filter")?.focus();
  }
}

function toggleOpen(r) {
  if (_open === keyOf(r)) { closeSide(); return; }
  setRun(r.tag, r._design);
  openSide(r);
}

let _viewingKey = "";
function paintViewing(scrolled = false) {
  const run = contextRun(), design = normDir(contextDesign());
  // A scroll only needs to look at the rows of the run in view.
  if (scrolled) document.querySelectorAll("#runs-table tr.rn-row.is-viewing").forEach((tr) => tr.classList.remove("is-viewing"));
  const rows = scrolled ? document.querySelectorAll("#runs-table tr.rn-row[data-tag='" + CSS.escape(run || "") + "']") : document.querySelectorAll("#runs-table tr.rn-row");
  rows.forEach((tr) => tr.classList.toggle("is-viewing", tr.dataset.tag === run && normDir(tr.dataset.design) === design));
  const vk = design + "|" + run;
  if (vk !== _viewingKey) { _viewingKey = vk; repaintSizer(); }
}

// ------------------------------------------------------------------ selection
const COMPARE_MAX = 12;        // compare.js MAX
function pickedRuns() { return _visible.filter((r) => _picked.has(keyOf(r))); }
function pickRuns(runs, on) {
  for (const r of runs) { if (on) _picked.add(keyOf(r)); else _picked.delete(keyOf(r)); }
  syncSelection();
}
// The ticks follow the selection without redrawing the table.
function syncPickBoxes() {
  const table = document.getElementById("runs-table");
  if (!table) return;
  table.querySelectorAll("tr.rn-row").forEach((tr) => {
    const on = _picked.has(tr.dataset.key);
    tr.classList.toggle("is-picked", on);
    const cb = tr.querySelector("input.rn-pick");
    if (cb) cb.checked = on;
  });
  table.querySelectorAll("tr.rn-sweep:not(.rn-sizer) input.rn-pick-sweep").forEach((cb) => {
    const it = itemOfRow(cb.closest("tr"));
    if (it) markBox(cb, it.group.runs);
  });
  const all = table.querySelector("input.rn-pick-all");
  if (all) markBox(all, _visible);
}
// A group's box: ticked when all its runs are, a dash when some are.
function markBox(cb, runs) {
  let n = 0;
  for (const r of runs) if (_picked.has(keyOf(r))) n++;
  cb.checked = !!runs.length && n === runs.length;
  cb.indeterminate = n > 0 && n < runs.length;
}
// The Tag and Export popovers live beside the bar, so the bar can redraw.
let _barSig = "";
function ensureBarPops(bar) {
  if (document.getElementById("rn-tagpop")) return;
  bar.insertAdjacentHTML("afterend",
    "<div class='rn-barpops'><div class='rn-pop rn-tagpop' id='rn-tagpop' role='dialog' aria-label='Tag the selected runs' hidden></div>" +
    "<div class='menu rn-pop' id='rn-exportpop' role='menu' hidden></div></div>");
}
function syncSelection() {
  const bar = document.getElementById("rn-selbar");
  if (!bar) return;
  // Runs the filters hide leave the selection: an action never reaches a run
  // you can't see.
  const live = new Set(_visible.map(keyOf));
  for (const k of [..._picked]) if (!live.has(k)) _picked.delete(k);
  if (_anchor && !live.has(_anchor)) _anchor = null;
  syncPickBoxes();
  const sel = pickedRuns();
  const n = sel.length;
  bar.hidden = n === 0;
  ensureBarPops(bar);
  if (!n) { _barSig = ""; return; }
  const many = n > COMPARE_MAX;
  const arch = sel.filter((r) => r.archived).length;
  // A redraw in the background (settings read, failures explained) must not
  // rebuild the bar under an open popover: only when what it says changed.
  const sig = n + "|" + arch;
  if (sig === _barSig && bar.querySelector("[data-sel]")) return;
  _barSig = sig;
  bar.innerHTML = "<b>" + plural(n, "run") + " selected</b>" +
    "<button class='btn btn-primary btn-sm' data-sel='compare'" + (n < 2 ? " disabled title='Tick at least two'" : many ? " disabled title='Compare takes up to " + COMPARE_MAX + " runs'" : "") + ">" + icon("diff", { size: 13 }) + " Compare</button>" +
    (arch < n ? "<button class='btn btn-ghost btn-sm' data-sel='archive' title='Hide them from Runs, Compare and the run picker. Nothing is deleted. (Delete)'>" + icon("archive", { size: 13 }) + " Archive</button>" : "") +
    (arch ? "<button class='btn btn-ghost btn-sm' data-sel='restore' title='Show them again everywhere'>" + icon("archive", { size: 13 }) + " Restore" + (arch < n ? " " + arch : "") + "</button>" : "") +
    "<button class='btn btn-ghost btn-sm' data-sel='tag' aria-haspopup='dialog'>" + icon("tag", { size: 13 }) + " Tag…</button>" +
    "<button class='btn btn-ghost btn-sm' data-sel='export' aria-haspopup='menu'>" + icon("download", { size: 13 }) + " Export " + icon("chevronDown", { size: 12 }) + "</button>" +
    "<button class='btn btn-ghost btn-sm rn-del' data-sel='delete' title='Remove their folders from disk (Shift+Delete)'>" + icon("trash", { size: 13 }) + " Delete…</button>" +
    (n < 2 ? "<span class='muted'>Tick one more to compare.</span>" : many ? "<span class='muted'>Compare takes up to " + COMPARE_MAX + ": untick " + (n - COMPARE_MAX) + ".</span>" : "") +
    "<span class='rn-spacer'></span><button class='btn btn-ghost btn-sm' data-sel='clear' title='Untick every run (Esc)'>Clear</button>";
  bar.onclick = (e) => {
    const b = e.target.closest("[data-sel]");
    if (!b) return;
    const s = b.dataset.sel, rs = pickedRuns();
    if (s === "compare") compareRuns(rs);
    else if (s === "archive") archiveRuns(rs.filter((r) => !r.archived), true);
    else if (s === "restore") archiveRuns(rs.filter((r) => r.archived), false);
    else if (s === "tag") openTagPop(b, rs);
    else if (s === "export") openExportPop(b, rs);
    else if (s === "delete") deleteRuns(rs);
    else if (s === "clear") { _picked.clear(); syncSelection(); }
  };
}

// Compare these runs (up to COMPARE_MAX of them; more are cut, and it says
// so). The project's baseline, when it is among them, is the reference.
export async function compareRuns(runs) {
  if (runs.length < 2) return;
  if (runs.length > COMPARE_MAX) {
    toast.show("Compare takes up to " + COMPARE_MAX + " runs: it shows the first " + COMPARE_MAX + " of " + runs.length +
      ". Swap any of them there with Add a run.", "info", 9000);
  }
  const list = runs.slice();
  const bi = list.findIndex((r) => isBaseline(r));
  if (bi > 0) list.unshift(list.splice(bi, 1)[0]);
  const m = await import("./compare.js");
  m.preselectCompare(list.slice(0, COMPARE_MAX).map((r) => r.run_dir).filter(Boolean));
  go("analytics", { section: "analytics-compare" });
}

// ------------------------------------------------------------------ bulk actions
let _undo = null;              // the Undo of the toast showing now (Ctrl+Z)
function undoToast(message, run, { ttl = 10000, onGone = null } = {}) {
  let done = false;
  const act = { label: "Undo", run: () => { done = true; _undo = null; run(); } };
  toast.show(message, "success", ttl, [act]);
  const el = toast.container && toast.container.lastElementChild;
  _undo = act.run;
  if (!el) return;
  // When the toast goes (timed out, closed, pushed out), Undo goes with it.
  const mo = new MutationObserver(() => {
    if (el.isConnected) return;
    mo.disconnect();
    if (_undo === act.run) _undo = null;
    if (!done && onGone) onGone();
  });
  mo.observe(toast.container, { childList: true });
}
function bulkFailures(res) {
  const f = (res && res.failed) || [];
  if (!f.length) return;
  alertDialog({ title: plural(f.length, "run") + " couldn't be changed", body: f.slice(0, 12).map((x) => (x.run_dir || "").split(/[\\/]/).pop() + ": " + x.error).join("\n") +
    (f.length > 12 ? "\n…and " + (f.length - 12) + " more." : "") });
}

async function archiveRuns(rs, on) {
  if (!rs.length) return;
  try {
    const res = await api.runsBulk({ action: on ? "archive" : "restore", run_dirs: rs.map((r) => r.run_dir) });
    const done = new Set(res.done || []);
    for (const r of rs) if (done.has(r.run_dir)) r.archived = on;
    if (on) for (const r of rs) _picked.delete(keyOf(r));
    renderRuns({ cached: true });
    bulkFailures(res);
    if (!done.size) return;
    undoToast(on ? "Archived " + plural(done.size, "run") + ". They're hidden from Runs, Compare and the run picker; nothing was deleted."
      : "Restored " + plural(done.size, "run") + ".", () => archiveRuns(rs.filter((r) => done.has(r.run_dir)), !on));
  } catch (ex) {
    toast.show("Couldn't " + (on ? "archive" : "restore") + " the runs: " + (ex.message || ex), "error");
  }
}

// Delete: one dialog for the whole set, then the folders go into the
// project's trash and an Undo stays up; the space is freed when it closes.
async function deleteRuns(rs, { force = false } = {}) {
  if (!rs.length) return;
  const n = rs.length;
  const base = rs.filter((r) => isBaseline(r));
  const names = rs.slice(0, 8).map((r) => r.tag).join(", ") + (n > 8 ? " and " + (n - 8) + " more" : "");
  if (!force) {
    const pick = await customDialog({
      title: "Delete " + plural(n, "run") + "?",
      bodyHtml: "<p>" + esc(names) + "</p><p>This removes " + (n === 1 ? "its folder" : "their folders") + " from disk: the numbers, layouts and logs go with " + (n === 1 ? "it" : "them") +
        ". You can undo it for a few seconds after. To hide runs and keep everything, archive them instead.</p>" +
        (base.length ? "<p><b>" + esc(base.map((r) => r.tag).join(", ")) + "</b> " + (base.length === 1 ? "is a baseline" : "are baselines") +
          ": pages then compare each run with the last clean run before it.</p>" : ""),
      buttons: [{ label: "Cancel", value: null, cls: "btn-ghost" }, { label: "Archive instead", value: "archive", cls: "btn-ghost" },
        { label: "Delete " + plural(n, "run"), value: "delete", cls: "btn-warn" }],
    });
    if (pick === "archive") return archiveRuns(rs.filter((r) => !r.archived), true);
    if (pick !== "delete") return;
  }
  let res;
  try {
    res = await api.runsBulk({ action: "delete", run_dirs: rs.map((r) => r.run_dir), force });
  } catch (ex) {
    const kids = ex && ex.status === 409 && ex.body && ex.body.children;
    if (!kids || !kids.length) { toast.show("Couldn't delete: " + (ex.message || ex), "error"); return; }
    const ok = await confirmDialog({
      title: "Other runs were resumed from these", danger: true, confirmText: "Delete anyway",
      body: kids.slice(0, 10).map((k) => k.run + " (from " + k.parent + ")").join(", ") + (kids.length > 10 ? " and " + (kids.length - 10) + " more" : "") +
        " read their earlier steps from these runs' folders. Deleted, those runs keep their own results and final GDS, but their earlier steps, pictures and checks go.",
    });
    if (!ok) return;
    return deleteRuns(rs, { force: true });
  }
  const done = new Set(res.done || []);
  for (const r of rs) if (done.has(r.run_dir)) { _picked.delete(keyOf(r)); if (state.selectedRunTag === r.tag) state.selectedRunTag = null; }
  await renderRuns();
  bulkFailures(res);
  if (!done.size) return;
  const designs = [...new Set(rs.filter((r) => done.has(r.run_dir)).map((r) => r._design))];
  const gb = res.bytes ? (res.bytes >= 1e9 ? (res.bytes / 1e9).toFixed(1) + " GB" : Math.max(1, Math.round(res.bytes / 1e6)) + " MB") : "";
  undoToast("Deleted " + plural(done.size, "run") + "." + (gb ? " The space (" + gb + ") is freed when this message closes." : ""), async () => {
    try {
      const u = await api.runsBulk({ action: "undo", batch: res.batch, design_dirs: designs });
      toast.show("Put back " + plural((u.done || []).length, "run") + ".", "success", 3000);
      bulkFailures(u);
    } catch (ex) { toast.show(ex.status === 410 ? "Too late: those runs are already gone." : "Couldn't put them back: " + (ex.message || ex), "error"); }
    renderRuns();
  }, { onGone: () => api.runsBulk({ action: "purge", batch: res.batch, design_dirs: designs }).catch(() => {}) });
}

// Tag…: the tags of the selected runs (ticked: all have it; a dash: some), a
// field to add one, and rename / remove across the project.
function openTagPop(btn, rs) {
  const pop = document.getElementById("rn-tagpop");
  if (!pop) return;
  if (isLayerOpen(pop)) { closeLayer(pop); return; }
  const paint = () => {
    const counts = new Map();
    for (const r of _shown.runs) for (const t of r.tags || []) counts.set(t, (counts.get(t) || 0) + 1);
    const sel = rs.map((r) => _shown.runs.find((x) => keyOf(x) === keyOf(r)) || r);
    const all = [...counts.keys()].sort((a, b) => a.localeCompare(b));
    pop.innerHTML = "<div class='rn-cols-h'>Tag " + plural(sel.length, "run") + "</div>" +
      "<input class='rn-q' type='text' maxlength='32' placeholder='Type a tag and press Enter' aria-label='New tag'/>" +
      "<div class='rn-taglist'>" + (all.length ? all.map((t) => {
        const have = sel.filter((r) => (r.tags || []).some((x) => x.toLowerCase() === t.toLowerCase())).length;
        const st = have === sel.length ? "all" : have ? "some" : "none";
        return "<div class='rn-tagrow'><label><input type='checkbox' data-t='" + esc(t) + "'" + (st === "all" ? " checked" : "") + (st === "some" ? " data-mixed='1'" : "") + "/> " +
          "<span class='rn-tagchip is-sm'>" + esc(t) + "</span> <span class='rn-n'>" + counts.get(t) + "</span></label>" +
          "<button class='linklike' data-ren='" + esc(t) + "' title='Rename this tag on every run'>Rename</button>" +
          "<button class='linklike' data-rm='" + esc(t) + "' title='Take this tag off every run that has it'>Remove</button></div>";
      }).join("") : "<p class='muted'>No tags yet. A tag is a word or two (letters, digits, - _ . /), up to 32 characters.</p>") + "</div>";
    pop.querySelectorAll("input[data-mixed]").forEach((i) => { i.indeterminate = true; });
    const inp = pop.querySelector("input.rn-q");
    inp.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" || !inp.value.trim()) return;
      e.preventDefault();
      await tagRuns(sel, [inp.value.trim()], true);
      paint(); pop.querySelector("input.rn-q")?.focus();
    });
    pop.querySelectorAll("input[data-t]").forEach((i) => i.addEventListener("change", async () => { await tagRuns(sel, [i.dataset.t], i.checked); paint(); }));
    pop.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
      const t = b.dataset.rm;
      const have = _shown.runs.filter((r) => (r.tags || []).includes(t));
      if (!(await confirmDialog({ title: "Remove the tag “" + t + "”?", body: "It comes off " + plural(have.length, "run") + ". The runs themselves are not touched.", confirmText: "Remove tag" }))) return;
      await tagRuns(have, [t], false); paint();
    }));
    pop.querySelectorAll("[data-ren]").forEach((b) => b.addEventListener("click", async () => {
      const t = b.dataset.ren;
      const nn = await promptDialog({ title: "Rename the tag “" + t + "”", label: "New name", defaultValue: t });
      if (!nn || !nn.trim() || nn.trim() === t) return;
      const have = _shown.runs.filter((r) => (r.tags || []).includes(t));
      await tagRuns(have, [nn.trim()], true);
      await tagRuns(have, [t], false);
      paint();
    }));
  };
  paint();
  pop.hidden = false;
  openLayer(pop, { kind: "popover", owner: btn, onClose: () => { pop.hidden = true; }, focus: () => pop.querySelector("input.rn-q") });
}
async function tagRuns(rs, tags, on) {
  if (!rs.length) return;
  try {
    const res = await api.runsBulk({ action: on ? "tag" : "untag", run_dirs: rs.map((r) => r.run_dir), tags });
    const done = new Set(res.done || []);
    const low = tags.map((t) => t.toLowerCase());
    for (const r of _shown.runs) {
      if (!done.has(r.run_dir)) continue;
      const cur = r.tags || [];
      r.tags = on ? cur.concat(tags.filter((t) => !cur.some((c) => c.toLowerCase() === t.toLowerCase()))) : cur.filter((c) => !low.includes(c.toLowerCase()));
    }
    renderRuns({ cached: true });
    if (_sideRow) paintSideTags(_sideRow);
    bulkFailures(res);
  } catch (ex) { toast.show("Couldn't change the tags: " + (ex.message || ex), "error"); }
}

function openExportPop(btn, rs) {
  const pop = document.getElementById("rn-exportpop");
  if (!pop) return;
  if (isLayerOpen(pop)) { closeLayer(pop); return; }
  pop.innerHTML = "<button class='menu-item' role='menuitem' data-x='csv'>CSV · the columns shown</button>" +
    "<button class='menu-item' role='menuitem' data-x='csvall'>CSV · every metric</button>" +
    "<button class='menu-item' role='menuitem' data-x='json'>JSON · everything</button>" +
    "<div class='menu-note'>Opens in Excel and LibreOffice. With a comma decimal, use Data › From Text/CSV, UTF-8 and comma. JSON keeps every value exactly.</div>";
  pop.onclick = (e) => { const b = e.target.closest("[data-x]"); if (!b) return; closeLayer(pop, "api"); exportRuns(b.dataset.x, rs, "selected"); };
  pop.hidden = false;
  openLayer(pop, { kind: "menu", owner: btn, onClose: () => { pop.hidden = true; } });
}

// ------------------------------------------------------------------ export
function stamp() {
  const d = new Date(), p = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes());
}
function exportName(what, ext) {
  const name = (getRunScope() === "all" ? "all-designs" : designLabel(state.designDir) || "runs").replace(/[^A-Za-z0-9._-]+/g, "-");
  const v = what === "selected" ? "selected" : String((viewById(what) || {}).name || "runs").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return name + "-runs-" + v + "-" + stamp() + "." + ext;
}
const localStamp = (iso) => { const d = iso ? new Date(iso) : null; if (!d || isNaN(d)) return ""; const p = (x) => String(x).padStart(2, "0"); return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()); };
async function exportRuns(fmt_, runs, what) {
  if (!runs.length) return;
  try {
    if (fmt_ === "csv") {
      const name = exportName(what, "csv");
      downloadCsv(name, runsCsv(runs));
      toast.show("Saved " + name + " (" + plural(runs.length, "run") + ").", "success", 3000);
      return;
    }
    const rows = runs.map((r) => ({
      tag: r.tag, run_dir: r.run_dir, design: designLabel(r._design), started_at: r.started_at, started: localStamp(r.started_at),
      result: { state: verdictOf(r).state, label: verdictOf(r).label, stage: verdictOf(r).stage || null }, origin: r.origin || {},
      started_by: startedBy(r), job_id: r.job_id || null, pdk: r.pdk, scl: r.scl, tags: r.tags || [], note: r.note || "",
      archived: !!r.archived, pinned: !!r.pinned, is_baseline: isBaseline(r), wall_time_s: r.wall_time_s,
      key_metrics: r.key_metrics || {}, changed_vs_baseline: CHANGES.get(keyOf(r)) || null,
    }));
    const body = { fmt: fmt_ === "json" ? "json" : "csv", rows, baseline: getRunScope() === "all" ? null : projectBaseline(state.designDir) || null,
      design: { name: designLabel(state.designDir), dir: state.designDir || "" }, view: { ...viewOfState(T.view, ""), name: (viewById(T.view) || {}).name || "" } };
    toast.show("Reading " + plural(runs.length, "run") + " for the export…", "info", 2500);
    const res = await api.runsExport(body);
    const ext = fmt_ === "json" ? "json" : "csv";
    const name = exportName(what, ext);
    downloadText(name, res.text, res.mime || (ext === "json" ? "application/json" : "text/csv"));
    toast.show("Saved " + name + " (" + plural(runs.length, "run") + ").", "success", 3000);
  } catch (ex) {
    toast.show("Couldn't export: " + (ex.message || ex), "error");
  }
}
// The table as a CSV for Excel and LibreOffice: the columns shown, the
// numbers exact as LibreLane wrote them (in its units), then where each run
// came from.
export function runsCsv(runs) {
  const cols = visibleCols(getRunScope());
  const rawUnit = (c) => (c.id === "runtime" ? "s" : c.key ? (MF.unitOf(MF.parseKey(c.key).base) === "ratio" ? "fraction" : MF.unitOf(MF.parseKey(c.key).base)) : "");
  const head = [];
  for (const c of cols) {
    if (c.id === "note" || c.id === "tags") continue;   // written once, at the end
    if (c.id === "checks") head.push("DRC errors", "LVS errors", "Antenna violations");
    else if (c.id === "when") head.push("Started", "Started (ISO)");
    else head.push(c.label + (rawUnit(c) ? " (" + rawUnit(c) + ")" : ""));
    if (c.id === "name") head.push("Sweep point");
  }
  head.push("Tags", "Note", "Archived", "Baseline", "PDK", "SCL", "Run folder");
  const rows = [head];
  for (const r of runs) {
    const row = [];
    for (const c of cols) {
      if (c.id === "name") { row.push(r.tag, valuesText(pointValues(r, sweepOf(r)))); continue; }
      if (c.id === "verdict") { const v = verdictOf(r); row.push(v.label || v.state); continue; }
      if (c.id === "when") { row.push(localStamp(r.started_at), r.started_at || ""); continue; }
      if (c.id === "origin") { row.push(originText(r)); continue; }
      if (c.id === "design") { row.push(designLabel(r._design)); continue; }
      if (c.id === "pdk") { row.push(r.pdk || ""); continue; }
      if (c.id === "note" || c.id === "tags") continue;
      if (c.id === "changed") { row.push((CHANGES.get(keyOf(r)) || []).map((x) => x.var + ": " + x.from + " -> " + x.to).join("; ")); continue; }
      if (c.id === "checks") { const k = checksOf(r); row.push(k ? k.drc ?? "" : "", k ? k.lvs ?? "" : "", k ? k.ant ?? "" : ""); continue; }
      if ((TIMING.has(c.id) || c.timing) && untimed(r)) { row.push("not timed"); continue; }
      const v = c.get ? c.get(r) : null;
      row.push(v == null ? "" : c.setting ? showSetting(v) : v);
    }
    row.push((r.tags || []).join("; "), r.note || "", r.archived ? "yes" : "", isBaseline(r) ? "yes" : "", r.pdk || "", r.scl || "", r.run_dir || "");
    rows.push(row);
  }
  return toCsv(rows, { excel: true });
}

// ------------------------------------------------------------------ lazy facts
// Why each failed run failed (the same reading Flow uses), for "failed at
// Placement" and the sweep's "(DPL-0036)". Active design only (the endpoint
// resolves against it); a dozen at most per render.
let _rxBusy = false;
async function explainFailed(runs) {
  if (_rxBusy) return;
  const todo = runs.filter((r) => !r.success && !(r.verdict && r.verdict.stage) && !(r.verdict && r.verdict.state && r.verdict.state !== "failed") &&
    !RX.has(keyOf(r)) && normDir(r._design) === normDir(state.designDir)).slice(0, 12);
  if (!todo.length) return;
  _rxBusy = true;
  try {
    for (const r of todo) {
      try { RX.set(keyOf(r), await api.runExplain(r.tag)); } catch (_e) { RX.set(keyOf(r), {}); }
    }
  } finally { _rxBusy = false; }
  renderRuns({ cached: true });
}

// What each run changed against its design's baseline: worked out by the
// server (plain settings, paths left out) and cached there per run, for the
// runs shown, a few hundred at a time.
let _changesKey = "";
async function loadChanges(runs) {
  const byDesign = new Map();
  for (const r of runs) {
    const b = projectBaseline(r._design);
    if (!b) continue;
    if (!byDesign.has(r._design)) byDesign.set(r._design, []);
    byDesign.get(r._design).push(r);
  }
  const todo = [];
  for (const [d, rs] of byDesign) {
    const base = _shown.runs.find((r) => r.tag === projectBaseline(d) && normDir(r._design) === normDir(d));
    if (!base) continue;
    const need = rs.filter((r) => r !== base && !CHANGES.has(keyOf(r)));
    if (need.length) todo.push([base, need]);
  }
  const key = todo.map(([b, rs]) => b.run_dir + ":" + rs.length).join("|");
  if (!todo.length || key === _changesKey) return;
  _changesKey = key;
  for (const [base, rs] of todo) {
    for (let i = 0; i < rs.length; i += 300) {
      const part = rs.slice(i, i + 300);
      try {
        const res = await api.runsChanged(base.run_dir, part.map((r) => r.run_dir));
        const ch = res.changes || {};
        for (const r of part) CHANGES.set(keyOf(r), ch[r.run_dir] || []);
      } catch (_e) { for (const r of part) CHANGES.set(keyOf(r), []); }
      if (key !== _changesKey) return;
    }
  }
  if (key === _changesKey) renderRuns({ cached: true });
}

// The values of the added columns (any metric or setting), for the runs shown.
let _extraBusy = "";
async function loadExtra(runs) {
  const keys = T.extra.slice();
  if (!keys.length) return;
  const need = runs.filter((r) => { const x = XV.get(r.run_dir); return !x || keys.some((k) => !(k in x) && !(x._asked || []).includes(k)); });
  if (!need.length) return;
  const sig = keys.join(",") + ":" + need.length + ":" + (need[0] && need[0].run_dir);
  if (sig === _extraBusy) return;
  _extraBusy = sig;
  try {
    for (let i = 0; i < need.length; i += 500) {
      const part = need.slice(i, i + 500);
      const res = await api.runsValues(part.map((r) => r.run_dir), keys);
      const vals = res.values || {};
      for (const r of part) {
        const cur = XV.get(r.run_dir) || {};
        const got = vals[r.run_dir] || {};
        const asked = new Set([...(cur._asked || []), ...keys]);
        XV.set(r.run_dir, { ...cur, ...got, _asked: [...asked] });
      }
    }
  } catch (ex) {
    toast.show("Couldn't read the added columns: " + (ex.message || ex), "error");
  } finally { _extraBusy = ""; }
  renderRuns({ cached: true });
}

// ------------------------------------------------------------------ live sweep
let _poll = null;
async function paintLive() {
  const el = document.getElementById("rn-live");
  if (!el) return;
  let st = null;
  try { st = await api.dseStatus(); st = st && (st.data || st); } catch (_e) { st = null; }
  if (!st || !st.active) {
    el.hidden = true;
    if (_poll) { clearInterval(_poll); _poll = null; renderRuns(); }
    return;
  }
  const done = (st.done || []).length + (st.failed || []).length;
  el.hidden = false;
  el.innerHTML = "<span class='rn-live-dot' aria-hidden='true'></span><b>Sweep running</b> · run " + Math.min(done + 1, st.total || done + 1) + " of " + (st.total || "?") +
    (st.running ? " · <code>" + esc(st.running) + "</code>" : "") +
    ((st.failed || []).length ? " · <span class='rn-bad'>" + st.failed.length + " failed</span>" : "") +
    "<span class='rn-spacer'></span><button class='btn btn-ghost btn-sm' data-go='pipeline'>" + icon("flow", { size: 13 }) + " Watch in Flow</button>" +
    "<button class='btn btn-ghost btn-sm' id='rn-sweep-stop'>" + icon("stop", { size: 13 }) + " Stop sweep</button>";
  el.querySelector("#rn-sweep-stop").onclick = async () => {
    if (!(await confirmDialog({ title: "Stop the sweep?", confirmText: "Stop sweep", danger: true,
      body: "The run that is going stops now and the rest are not run. Finished runs are kept." }))) return;
    try { await api.dseCancel(); toast.show("Sweep stopped.", "success"); } catch (ex) { toast.show("Couldn't stop: " + (ex.message || ex), "error"); }
    paintLive();
  };
  if (!_poll) _poll = setInterval(() => { if (document.getElementById("sec-runs")?.classList.contains("section-active")) paintLive(); }, 4000);
}

// ------------------------------------------------------------------ the run panel
// One run, beside the table: how it ended, its numbers against the baseline,
// where it came from, its tags and note, and everything you can do with it.
const SIDE_NUMS = ["wns", "tns", "hold", "die", "cells", "util", "power", "runtime"];
const sideEl = () => document.getElementById("rn-side");
let _sideRow = null, _sideSeq = 0;

function markOpen() {
  document.querySelectorAll("#runs-table tr.rn-row").forEach((tr) => {
    const on = tr.dataset.key === _open;
    tr.classList.toggle("is-open", on);
    tr.querySelector(".rn-exp")?.setAttribute("aria-expanded", on ? "true" : "false");
  });
}
function openSide(r) {
  const el = sideEl();
  if (!el || !r) return;
  const was = !!_open;
  _open = keyOf(r);
  _sideRow = r;
  el.hidden = false;
  document.getElementById("rn-split")?.classList.add("has-side");
  markOpen();
  paintSide(r);
  // The table has less room now: its columns follow on the next frame, so
  // the panel shows at once (a full redraw of a long list takes a while on a
  // slow PC).
  if (!was) requestAnimationFrame(() => { if (_open) renderRuns({ cached: true }); });
}
function closeSide() {
  const el = sideEl();
  if (!_open && (!el || el.hidden)) return;
  _open = null; _sideRow = null; _sideSeq++;
  if (el) { el.hidden = true; el.innerHTML = ""; }
  document.getElementById("rn-split")?.classList.remove("has-side");
  if (document.getElementById("sec-runs")?.classList.contains("section-active")) renderRuns({ cached: true }); else markOpen();
}
// After a render: the open run may be gone, or its row may be a fresh one;
// the baseline or the settings read may have changed.
function syncSide() {
  if (!_open) return;
  const r = _shown.runs.find((x) => keyOf(x) === _open);
  if (!r) { closeSide(); return; }
  const host = sideEl();
  if (r !== _sideRow && !(host && host.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName))) { paintSide(r); return; }
  const base = baselineRowFor(r);
  const nums = host && host.querySelector("#rn-side-nums");
  if (nums) nums.innerHTML = sideNumsHtml(r, base);
  const ch = host && host.querySelector("#rn-side-set");
  if (ch) ch.innerHTML = sideSettingsHtml(r);
}

function sideDeltaHtml(c, v, b, r, base) {
  if (b == null || !finished(r) || !finished(base)) return "<span class='rn-d'></span>";
  const dl = deltaOfCol(c, v, b);
  if (!dl) return "<span class='rn-d'></span>";
  if (dl.same) return "<span class='rn-d'>same</span>";
  return "<span class='rn-d " + dl.cls + "' title='" + esc(MF.deltaWords(dl, "the baseline " + base.tag) + " · baseline " + (c.f ? c.f(b) : b) + (c.unit ? " " + c.unit : "")) + "'>" + esc(dl.text) + "</span>";
}
function sideNumsHtml(r, base) {
  const rows = COLS.filter((c) => SIDE_NUMS.includes(c.id)).map((c) => {
    if (TIMING.has(c.id) && untimed(r)) return "<div class='rn-num'><dt title='" + esc(c.tip || "") + "'>" + esc(c.label) + "</dt><dd class='muted' title='" + esc(UNTIMED_TIP) + "'>not timed</dd><span class='rn-d'></span></div>";
    const v = c.get(r);
    if (v == null) return "";
    const neg = c.unit === "ns" && v < 0 ? " class='is-neg'" : "";
    return "<div class='rn-num'><dt title='" + esc(c.tip || "") + "'>" + esc(c.label) + "</dt><dd" + neg + " title='" + esc(exactOf(c, v)) + "'>" + esc(c.f ? c.f(v) : v) +
      (c.unit ? "<small>" + esc(c.unit) + "</small>" : "") + "</dd>" + (base && !(TIMING.has(c.id) && untimed(base)) ? sideDeltaHtml(c, v, c.get(base), r, base) : "<span class='rn-d'></span>") + "</div>";
  }).join("");
  const k = checksOf(r);
  const checks = !k ? "" : "<div class='rn-num'><dt title='Design-rule errors (worst of Magic, KLayout and the router), LVS errors and antenna violations'>DRC · LVS · Antenna</dt><dd class='" +
    (k.total ? "is-neg" : "") + "'>" + (k.total ? esc([k.drc ? "DRC " + k.drc : "", k.lvs ? "LVS " + k.lvs : "", k.ant ? "ant " + k.ant : ""].filter(Boolean).join(" · ")) : "<span class='rn-clean'>clean</span>") + "</dd><span class='rn-d'></span></div>";
  if (!rows && !checks) return "<p class='rn-side-line muted'>This run ended before it had numbers to show.</p>";
  return "<h4 class='rn-side-h'>Numbers" + (base ? " <small>against the baseline <code>" + esc(base.tag) + "</code></small>" : isBaseline(r) ? " <small>this run is the baseline</small>" : "") + "</h4><dl class='rn-nums'>" + rows + checks + "</dl>";
}
// Where the run came from and which settings it changed.
function sideSettingsHtml(r) {
  const o = originOf(r), sw = sweepOf(r);
  let from = "";
  if (o.kind === "fix") {
    const ch = (o.changes || []).map((c) => "<li>" + esc(c.var) + " <i>" + esc(c.from) + " →</i> " + esc(c.to) + "</li>").join("");
    from = "<p class='rn-side-line'>A fix of <code>" + esc(o.parent) + "</code>" + (o.stage ? ", resumed at " + esc(FS.stageLabel(o.stage)) : "") + (ch ? " with:" : ".") + "</p>" +
      (ch ? "<ul class='rn-changes'>" + ch + "</ul>" : "");
  } else if (sw) {
    const vals = pointValues(r, sw);
    from = "<p class='rn-side-line'>A point of the sweep " + esc(sweepAxesText(sw)) + (vals ? ":" : ".") + "</p>" +
      (vals ? "<ul class='rn-changes'>" + Object.entries(vals).map(([k, x]) => "<li>" + esc(k) + " <i>=</i> " + esc(x) + "</li>").join("") + "</ul>" : "");
  } else if (r.imported) from = "<p class='rn-side-line'>Imported from elsewhere.</p>";
  else if (startedBy(r) === "terminal") from = "<p class='rn-side-line'>Started in a terminal, not by LanEx: it has no saved command or one-off settings.</p>";
  const bt = projectBaseline(r._design);
  let changed = "";
  if (bt && bt !== r.tag) {
    const ch = CHANGES.get(keyOf(r));
    changed = "<h4 class='rn-side-h' style='margin-top:14px'>Changed <small>against the baseline</small></h4>" +
      (!ch ? "<p class='rn-side-line muted'>Reading the settings…</p>"
        : !ch.length ? "<p class='rn-side-line muted'>The same settings as the baseline.</p>"
        : "<ul class='rn-changes'>" + ch.map((x) => "<li>" + esc(x.var) + " <i>" + esc(x.from) + " →</i> " + esc(x.to) + "</li>").join("") + "</ul>");
  } else if (bt === r.tag) changed = "<p class='rn-side-line muted' style='margin-top:6px'>This run is the baseline: every page measures against it.</p>";
  if (!from && !changed) return "";
  return (from ? "<h4 class='rn-side-h'>Where it came from</h4>" + from : "") + changed;
}

// ---- LibreLane's SynthesisExploration: its nine recipes, read from the run
// folder (controller/synthexplore.py), folded where the netlists are the same.
const isExploration = (r) => !!((r.verdict && r.verdict.exploration) || r.flow === "SynthesisExploration");
async function paintRecipes(r, my) {
  const host = document.getElementById("rn-side-se");
  let d;
  try { d = await api.synthExplore(r.run_dir); } catch (ex) {
    if (host) host.innerHTML = "<h4 class='rn-side-h'>Synthesis recipes</h4><p class='rn-side-line'>Couldn't read them: " + esc(ex.message || ex) + "</p>";
    return;
  }
  if (my !== _sideSeq || !host || !host.isConnected) return;
  const n = (v, dd = 2) => (typeof v === "number" && Number.isFinite(v) ? (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("en-US") : v.toFixed(dd)) : v === Infinity ? "none" : "");
  const rows = (d.rows || []).map((x) => {
    const m = x.metrics || {};
    const best = [x.strategies.includes(d.best_area) ? "smallest" : "", x.strategies.includes(d.best_slack) ? "most slack" : ""].filter(Boolean).join(", ");
    return "<tr" + (best ? " class='is-best'" : "") + "><td class='sw-res-v'>" + esc(x.strategies.join(" · ")) + (best ? " <small>" + esc(best) + "</small>" : "") + "</td>" +
      (x.ok ? "<td class='sw-num'>" + esc(n(m.design__instance__count, 0)) + "</td><td class='sw-num'>" + esc(n(m.design__instance__area, 0)) + "</td>" +
        "<td class='sw-num" + (m.timing__setup__ws < 0 ? " is-neg" : "") + "'>" + esc((m.timing__setup__ws >= 0 ? "+" : "") + n(m.timing__setup__ws)) + "</td>"
        : "<td colspan='3'><span class='rn-v is-fail'><i aria-hidden='true'></i>Failed at " + esc(x.step_words || x.step || "") + "</span> <span class='muted'>" + esc(x.reason || "") + "</span></td>") + "</tr>";
  }).join("");
  // The best by area and the best by slack; when one recipe is both, the
  // runner-up by slack from a different netlist (a folded row is one netlist).
  const rowOfS = (s) => (d.rows || []).find((x) => x.strategies.includes(s));
  const pickRows = [...new Set([d.best_area, d.best_slack].filter(Boolean).map(rowOfS).filter(Boolean))];
  if (pickRows.length === 1) {
    const next = (d.rows || []).filter((x) => x.ok && x !== pickRows[0])
      .sort((a, b) => ((b.metrics || {}).timing__setup__ws ?? -1e9) - ((a.metrics || {}).timing__setup__ws ?? -1e9))[0];
    if (next) pickRows.push(next);
  }
  const picks = pickRows.map((x) => x.strategies[0]);
  host.innerHTML = "<h4 class='rn-side-h'>Synthesis recipes <small>" + esc(d.ok_count + " of " + d.count + " synthesised") + "</small></h4>" +
    (d.all_failed
      ? "<p class='rn-side-line'><b>None of the " + d.count + " recipes synthesised.</b> " +
        (d.common_failure ? "All failed at " + esc(d.common_failure.step_words || "") + (d.common_failure.reason ? ": " + esc(d.common_failure.reason) : "") + ". " : "") +
        "That's a design problem, not a recipe problem: fix it on Setup (DESIGN_NAME, or the Verilog files).</p>"
      : "<div class='sw-res-wrap rn-se'><table class='sw-res'><thead><tr><th>Recipe</th><th class='sw-num'>Gates</th><th class='sw-num'>Area <small>µm²</small></th><th class='sw-num'>Setup WNS <small>ns</small></th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
        "<p class='rn-side-line muted'>" + esc(d.note || "") + "</p>" +
        (picks.length ? "<div class='rn-actions'><button class='btn btn-primary btn-sm' data-se-run='" + esc(picks.join(", ")) + "'>" + icon("play", { size: 13 }) + " Run " + esc(picks.join(" and ")) + " in full</button></div>" : ""));
  host.querySelector("[data-se-run]")?.addEventListener("click", (e) => {
    const vals = e.currentTarget.dataset.seRun;
    import("./sweeps.js").then((m) => m.openSweepSheet("synth", { values: vals, full: true }));
  });
}

// How it ended, in the words Flow and Verify use for the same run.
function sideTopHtml(r, rx, vf) {
  const v = verdictOf(r);
  const tone = VERDICT_TONE[v.state] || "warn";
  let title = v.label || v.state, why = "", act = "", extra = "";
  const flowBtn = (label) => "<button class='btn btn-primary btn-sm' data-ra='flow'>" + icon("flow", { size: 13 }) + " " + label + "</button>";
  // An exploration has no layout: Flow's "stopped at step 1" reading of its
  // folder is not what happened. Its recipes say it (below).
  if (isExploration(r)) {
    return "<div class='rn-side-verdict is-" + tone + "'><span class='rn-v is-" + tone + "'><i aria-hidden='true'></i>" + esc(title) + "</span>" +
      "<p>LibreLane's SynthesisExploration: each synthesis recipe run through synthesis and timing before layout, in one run folder. It builds no layout.</p></div>";
  }
  if (rx && (rx.kind === "clean" || rx.kind === "checks-failed")) {
    const cv = cardVerdict(vf, rx);
    if (cv) { title = rx.kind === "checks-failed" ? "The layout is done, but " + (cv.short || "checks failed") : cv.title; why = cv.sub || ""; }
    if (!vf && /Reading the sign-off checks/.test(why)) why = "No step failed. Open Verify for the sign-off checks.";
    if (rx.kind === "checks-failed") act = "<button class='btn btn-primary btn-sm' data-ra='verify'>" + icon("alert", { size: 13 }) + " Show the problems</button>";
    if (rx.warnings && rx.warnings.total) extra = warningsHtml(rx.warnings);
  } else if (rx) {
    const c = rx.card || {};
    const cancelled = rx.kind === "stopped";
    const early = !cancelled && (v.state === "stopped" || v.state === "incomplete");
    const at = rx.step ? (rx.step.id || rx.step.dir) + (rx.step.n ? " (step " + rx.step.n + ")" : "") : "";
    title = cancelled ? "You stopped this run" : early && !untimed(r) ? "This run ended before the last step" : c.title ? c.title : title;
    why = untimed(r) ? UNTIMED_TIP : c.plain ? c.plain
      : cancelled ? (at ? "It stopped at " + at + ". " : "") + "Everything before it is kept; run again whenever you're ready."
      : early ? (at ? "Its folder ends at " + at + " with no error" : "It has no error") + ": LanEx may have been closed while it ran. Run it again to finish."
      : at ? "It stopped at " + at + "." : "";
    if (v.state === "failed") act = flowBtn("Why, and how to fix it");
    if (untimed(r)) act = "<button class='btn btn-primary btn-sm' data-ra='verify'>" + icon("alert", { size: 13 }) + " Open Verify</button>";
  } else if (v.state === "failed") act = flowBtn("Why, and how to fix it");
  return "<div class='rn-side-verdict is-" + tone + "'><span class='rn-v is-" + tone + "'><i aria-hidden='true'></i>" + esc(title) + "</span>" +
    (why ? "<p>" + esc(why) + "</p>" : "") + act + extra + "</div>";
}

function sideHtml(r, base) {
  const b = (id, label, ic, cls = "btn-ghost") => "<button class='btn " + cls + " btn-sm' data-ra='" + id + "'>" + (ic ? icon(ic, { size: 13 }) + " " : "") + esc(label) + "</button>";
  const d = r.started_at ? new Date(r.started_at) : null;
  const meta = [d && !isNaN(d) ? localeDate(d, [], { day: "numeric", month: "short", year: "numeric" }) + ", " + localeTime(d, [], { hour: "2-digit", minute: "2-digit" }) : "",
    [r.pdk, r.scl].filter(Boolean).join(" / "), getRunScope() === "all" ? designLabel(r._design) : ""].filter(Boolean).join(" · ");
  const isB = isBaseline(r);
  return "<div class='rn-side-head'><div><div class='an-eyebrow'>Run" + (isB ? " · baseline" : "") + (r.archived ? " · archived" : "") + "</div><h3 class='rn-side-tag'>" + esc(r.tag) + "</h3>" +
    (meta ? "<div class='rn-side-meta'>" + esc(meta) + "</div>" : "") + "</div>" +
    "<button class='iconbtn' data-side='close' aria-label='Close the run details (Esc)' title='Close (Esc)'>" + icon("x", { size: 16 }) + "</button></div>" +
    "<div class='rn-side-body'>" +
    "<div id='rn-side-top'>" + sideTopHtml(r, RX.get(keyOf(r)) || null, null) + "</div>" +
    (isExploration(r) ? "<section id='rn-side-se'><h4 class='rn-side-h'>Synthesis recipes</h4><p class='rn-side-line muted'>Reading the nine recipes…</p></section>"
      : "<section id='rn-side-nums'>" + sideNumsHtml(r, base) + "</section>") +
    "<section id='rn-side-set'>" + sideSettingsHtml(r) + "</section>" +
    "<section><h4 class='rn-side-h'>Open this run in</h4><div class='rn-open-in'>" +
      b("flow", "Flow", "flow", "") + b("layout", "Layout", "grid", "") + b("analytics", "Analytics", "chart", "") + b("verify", "Verify", "check", "") + "</div></section>" +
    "<section><h4 class='rn-side-h'>Actions</h4><div class='rn-actions'>" +
      b("baseline", isB ? "Clear baseline" : "Make baseline", "star") +
      b("again", "Run again with changes", "play") + b("cmd", "Copy command", "command") + b("pin", r.pinned ? "Unpin" : "Pin", "pin") +
      b("archive", r.archived ? "Restore" : "Archive", "archive") +
      b("bundle", "Bundle…", "download") + b("export", "Report…", "file") + b("files", "Files", "folder") + b("delete", "Delete…", "trash", "btn-ghost rn-del") + "</div></section>" +
    "<section><h4 class='rn-side-h'>Tags</h4><div id='rn-side-tags'></div></section>" +
    "<section><h4 class='rn-side-h'><label for='rn-note-ta'>Note</label></h4>" +
    "<textarea id='rn-note-ta' class='rn-note-ta' rows='2' placeholder='What you tried, what you saw…'></textarea></section>" +
    "</div>";
}
function paintSideTags(r) {
  const host = document.getElementById("rn-side-tags");
  if (!host) return;
  const fresh = _shown.runs.find((x) => keyOf(x) === keyOf(r)) || r;
  host.innerHTML = "<div class='rn-f-tags'>" + (fresh.tags || []).map((t) => "<span class='rn-tagchip'>" + esc(t) +
    "<button class='rn-tagx' data-untag='" + esc(t) + "' aria-label='Take the tag " + esc(t) + " off'>" + icon("x", { size: 11 }) + "</button></span>").join("") +
    "<input class='rn-tagadd' type='text' maxlength='32' placeholder='Add a tag' aria-label='Add a tag to " + esc(r.tag) + "'/></div>";
  const inp = host.querySelector(".rn-tagadd");
  inp.addEventListener("keydown", async (e) => { if (e.key === "Enter" && inp.value.trim()) { e.preventDefault(); await tagRuns([fresh], [inp.value.trim()], true); host.querySelector(".rn-tagadd")?.focus(); } });
  host.querySelectorAll("[data-untag]").forEach((b) => b.addEventListener("click", () => tagRuns([fresh], [b.dataset.untag], false)));
}

async function paintSide(r) {
  const host = sideEl();
  if (!host || !r) return;
  const my = ++_sideSeq;
  _sideRow = r;
  const base = baselineRowFor(r);
  const active = normDir(r._design) === normDir(state.designDir);
  host.innerHTML = sideHtml(r, base);
  paintSideTags(r);
  if (isExploration(r)) paintRecipes(r, my);
  const ta = host.querySelector("#rn-note-ta");
  ta.value = r.note || "";
  ta.addEventListener("blur", async () => {
    if (ta.value === (r.note || "")) return;
    try { await focusDesign(r._design); await api.setRunNote(r.tag, ta.value); r.note = ta.value; toast.show("Note saved.", "success", 2500); }
    catch (ex) { toast.show("Couldn't save the note: " + (ex.message || ex), "error"); }
  });
  host.querySelector("[data-side='close']").addEventListener("click", () => closeSide());
  host.onclick = (e) => {
    const b = e.target.closest("[data-ra]");
    if (b) { e.stopPropagation(); runAction(b.dataset.ra, r); }
  };
  let rx = RX.get(keyOf(r));
  const [rx2, vfr] = await Promise.all([
    rx || !active ? Promise.resolve(rx || null) : api.runExplain(r.tag, base ? base.tag : undefined).catch(() => null),
    active && (r.success || (r.verdict && r.verdict.state !== "running")) ? api.verify(r.tag).catch(() => null) : Promise.resolve(null),
  ]);
  if (my !== _sideSeq || _open !== keyOf(r)) return;
  rx = rx2;
  const vf = vfr && (vfr.verdict ? vfr : vfr.data && vfr.data.verdict ? vfr.data : null);
  const top = host.querySelector("#rn-side-top");
  if (top) top.innerHTML = sideTopHtml(r, rx, vf);
}

// The palette opens a run's actions with this same function (palette.js).
export async function runAction(id, r) {
  const d = r._design;
  if (["flow", "layout", "analytics", "verify"].includes(id)) { await setRun(r.tag, d); go(id === "flow" ? "pipeline" : id); return; }
  if (id === "baseline") return toggleBaseline(r);
  if (id === "archive") return archiveRuns([r], !r.archived);
  if (id === "delete") return deleteRuns([r]);
  await focusDesign(d);
  if (id === "again") return reproduceRun(r.tag);
  if (id === "cmd") return copyCommand(r.tag);
  if (id === "pin") return togglePin(r.tag, d, !r.pinned);
  if (id === "bundle") return bundleDownloadFlow(r.tag);
  if (id === "export") return exportRunFlow(r.tag);
  if (id === "files") return showFilesModal(r.tag);
}

async function copyCommand(tag) {
  let meta;
  try { meta = await api.runGuiMeta(tag); } catch (_e) { meta = null; }
  const cc = meta && meta.cli_command;
  const cli = String((cc && typeof cc === "object" ? (cc[cc.recommended] || cc.container || cc.local) : cc) || "").trim();
  if (!cli) { await alertDialog({ title: "No saved command", body: "LanEx didn't record the command for " + tag + " (it was started elsewhere, or by a sweep before LanEx kept it)." }); return; }
  try { await navigator.clipboard.writeText(cli); toast.show("Command copied.", "success"); }
  catch (_e) { await alertDialog({ title: "The command of " + tag, body: cli }); }
}

// Download a support bundle — the user ticks exactly what goes in. Keys match
// bundle.ALL_PARTS on the backend.
const BUNDLE_PARTS = [
  { key: "config", label: "Used config", hint: "config.json + resolved.json + GUI overrides/preset" },
  { key: "sources", label: "RTL source files" },
  { key: "metrics_csv", label: "Metrics CSV" },
  { key: "settings_csv", label: "Settings / constraints / PDK CSV" },
  { key: "analytics_csv", label: "Analytics CSV", hint: "summary + report counts + all metrics" },
  { key: "reports", label: "Signoff reports", hint: ".rpt / .drc / .lvs" },
  { key: "logs", label: "Step logs" },
  // Heavy binary deliverables — off by default (each can be tens–hundreds of MB).
  { key: "gds", label: "GDSII layout", hint: "final .gds / OASIS stream", checked: false },
  { key: "layout_views", label: "Other layout views", hint: "DEF / LEF / OpenDB / Magic", checked: false },
  { key: "netlists", label: "Netlists", hint: "gate-level + powered Verilog, SPICE/CDL, JSON header", checked: false },
  { key: "timing", label: "Timing models", hint: "Liberty .lib / SDF / SPEF / SDC", checked: false },
  { key: "images", label: "Layout images", hint: "KLayout render(s), PNG/SVG", checked: false },
  { key: "diagrams", label: "Yosys diagrams", hint: ".dot schematics + rendered .svg", checked: false },
];
async function bundleDownloadFlow(tag) {
  const picked = await checklistDialog({
    title: "Download bundle: " + tag,
    body: "Select components to include in the .zip archive. Text and report data are selected by default. " +
      "Binary deliverables like GDS, netlists, timing models, and images are large, so select only what you need.",
    items: BUNDLE_PARTS,
    confirmText: "Download .zip",
  });
  if (!picked || !picked.length) return;
  const url = api.runBundleUrl(tag, picked);
  const a = document.createElement("a");
  a.href = url; a.download = tag + "-bundle.zip";
  document.body.appendChild(a); a.click(); a.remove();
  toast.show("Preparing bundle…", "info");
}

// A formatted run report (CSV / Markdown / HTML): history.export_run.
async function exportRunFlow(tag) {
  const f = await choiceDialog({
    title: "Export run report: " + tag,
    body: "Download a formatted report for this run. (For the raw metrics table, use Analytics > Metrics CSV.)",
    choices: [
      { label: "CSV", value: "csv" },
      { label: "Markdown", value: "md" },
      { label: "HTML", value: "html" },
    ],
  });
  if (!f) return;
  const a = document.createElement("a");
  a.href = api.runExportUrl(tag, f);
  a.download = tag + "-report." + f;
  document.body.appendChild(a); a.click(); a.remove();
}

// Load a run's saved settings back into Setup so it can be re-run. Reads
// gui-run.json (persisted per run); never auto-starts the run.
async function reproduceRun(tag) {
  let meta;
  try {
    meta = await api.runGuiMeta(tag);
  } catch (ex) {
    await alertDialog({
      title: "Can't reproduce this run",
      body: (ex && ex.message) ||
        "No saved settings found for this run. It was created before reproduce support was available or generated outside the GUI.",
    });
    return;
  }
  const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ""; };
  if (meta.pdk) {
    state.selectedPdk = meta.pdk;
    const ps = document.getElementById("pdk-select");
    if (ps) { ps.value = meta.pdk; ps.dispatchEvent(new Event("change")); }
  }
  if (meta.scl) {
    state.selectedScl = meta.scl;
    setTimeout(() => { const sc = document.getElementById("scl-select"); if (sc) sc.value = meta.scl; }, 0);
  }
  if (meta.run_mode) state.runMode = meta.run_mode;
  state.varsValues = {};
  for (const [k, v] of Object.entries(meta.overrides || {})) {
    state.varsValues[k] = (typeof v === "boolean") ? v : String(v);
  }
  const stopSel = document.getElementById("run-stop-after");
  if (stopSel) {
    stopSel.value = "";
    if (meta.to) for (const o of stopSel.options) if (o.value && lastStepOfStage(o.value) === meta.to) stopSel.value = o.value;
    stopSel.dispatchEvent(new Event("change"));
  }
  const modeVal = meta.mode === "semi" ? "semi" : "auto";
  document.querySelectorAll(".mode-btn").forEach((b) =>
    b.classList.toggle("mode-btn-active", b.dataset.mode === modeVal));
  const rn = (meta.tag || tag) + "-repro";
  setVal("run-name-input", rn);
  if (Array.isArray(meta.sources) && meta.sources.length) state.selectedFiles = meta.sources.slice();
  try { const cfg = await import("./config.js"); cfg.renderConfig(); } catch (_e) { /* ignore */ }
  go("setup");
  const summary = "Loaded " + Object.keys(meta.overrides || {}).length + " override(s), " +
    (meta.sources || []).length + " source(s), " + (meta.run_mode || "container") +
    " mode, flow " + (meta.flow || "Classic") + ". Review your settings in Setup, then click Run flow when ready. " +
    "Nothing has started yet." +
    (meta.frm || (meta.skip || []).length ? " That run also used --from or --skip; LanEx runs the whole flow (or up to a stage) instead." : "");
  const cc = meta.cli_command;
  const cli = String((cc && typeof cc === "object" ? (cc[cc.recommended] || cc.container || cc.local) : cc) || "").trim();
  const choices = cli ? [{ label: "Copy the run's CLI command", value: "copy" }] : [];
  const pick = await choiceDialog({
    title: "Loaded " + tag + " into Setup",
    body: summary + (cli ? "\n\nThat run's exact command:\n" + cli : ""),
    choices,
  });
  if (pick === "copy" && cli) {
    try { await navigator.clipboard.writeText(cli); toast.show("CLI command copied.", "success"); }
    catch (_e) { toast.show("Copy failed. The command is shown above: select it and copy it.", "warn"); }
  }
}

// Pin a run so it's easy to find in a long history. A bookmark; never touches run data.
async function togglePin(tag, designDir, pinned) {
  try {
    await focusDesign(designDir);
    await api.setRunPin(tag, pinned);
    await renderRuns();
  } catch (ex) {
    toast.show("Could not " + (pinned ? "pin" : "unpin") + ": " + (ex.message || ex), "error");
  }
}

// Import a run from elsewhere: adopt a run directory, or restore a LanEx
// export bundle (.zip). Both land under the active design's runs/.
export async function importRunFlow() {
  if (!state.designDir) {
    await alertDialog({
      title: "No active design",
      body: "Set a design directory first. Imported runs are saved into its runs directory.",
    });
    return;
  }
  const kind = await choiceDialog({
    title: "Import a run",
    body: "Adopt an existing LibreLane run directory, or restore a LanEx export bundle (.zip). It is copied into this design's runs/ so it survives deletion of the source.",
    choices: [
      { label: "Run folder", value: "dir" },
      { label: "LanEx bundle (.zip)", value: "bundle" },
    ],
  });
  if (!kind) return;
  const path = await importPathDialog(kind);
  if (!path || !path.trim()) return;
  try {
    const res = kind === "dir"
      ? await api.importRunDir(path.trim())
      : await api.importRunBundle({ path: path.trim() });
    await renderRuns();
    let msg = "Imported as '" + res.tag + "'.";
    if (res.warnings && res.warnings.length) msg += " " + res.warnings.join(" ");
    toast.show(msg, "success");
  } catch (ex) {
    await alertDialog({ title: "Import failed", body: (ex && ex.message) || String(ex) });
  }
}

// Where the import comes from: Browse opens the system picker (or the built-in
// browser), and the field still takes a typed or pasted path for remote and
// headless setups where no picker can appear.
async function importPathDialog(kind) {
  const isDir = kind === "dir";
  const seed = (state.designDir || "").replace(/[\\/]+$/, "") + (isDir ? "/runs" : "");
  let inputEl = null;
  const e2 = fmt.escape;
  const v = await customDialog({
    title: isDir ? "Adopt a run directory" : "Import a bundle (.zip)",
    bodyHtml:
      "<label class='dlg-label' for='import-path'>" + e2(isDir
        ? "The run folder (a runs/<tag> folder). Browse to it, or type or paste its full path."
        : "The LanEx bundle (.zip). Browse to it, or type or paste its full path.") + "</label>" +
      "<div class='picker-row'><input class='dlg-input' id='import-path' type='text' autocomplete='off' spellcheck='false'/>" +
      "<button type='button' class='btn btn-ghost' id='import-browse'>Browse…</button></div>",
    buttons: [
      { label: "Cancel", value: null, cls: "btn-ghost" },
      { label: "Import", value: "__GO__", cls: "btn-primary" },
    ],
    onMount: (back, close) => {
      inputEl = back.querySelector("#import-path");
      inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && inputEl.value.trim()) { e.preventDefault(); close("__GO__"); }
      });
      back.querySelector("#import-browse").addEventListener("click", async () => {
        const m = await import("./fs.js");
        const start = inputEl.value.trim() || seed;
        back.hidden = true;
        const got = isDir
          ? await m.pickFolder({ title: "Choose the run folder", use: "Import this run", seed: start })
          : await m.pickFile({ title: "Choose the LanEx bundle", use: "Import this bundle", seed: start, exts: [".zip"] });
        back.hidden = false;
        if (got) inputEl.value = got;
        inputEl.focus();
      });
      setTimeout(() => inputEl.focus(), 0);
    },
  });
  return v === "__GO__" && inputEl ? inputEl.value : null;
}

let _filesOverlay = null;
function filesOverlay() {
  if (_filesOverlay) return _filesOverlay;
  const ov = document.createElement("div");
  ov.className = "smodal-backdrop";
  ov.hidden = true;
  ov.innerHTML =
    "<div class='smodal' role='dialog' aria-modal='true' aria-labelledby='files-title'>" +
    "  <div class='smodal-head'><span class='smodal-title' id='files-title'>Files</span>" +
    "  <span class='smodal-spacer'></span><button class='iconbtn' id='files-close' aria-label='Close the file list'><svg viewBox='0 0 24 24' width='16' height='16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M6 6l12 12M18 6L6 18'/></svg></button></div>" +
    "  <div class='smodal-log' id='files-body' style='white-space:normal'></div>" +
    "</div>";
  document.body.appendChild(ov);
  const hide = () => { closeLayer(ov); ov.hidden = true; };
  ov.addEventListener("click", (e) => { if (e.target === ov) hide(); });
  ov.querySelector("#files-close").addEventListener("click", hide);
  _filesOverlay = ov;
  return ov;
}

function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

async function showFilesModal(tag) {
  const ov = filesOverlay();
  ov.hidden = false;
  openLayer(ov, { kind: "dialog", labelledBy: "files-title", onClose: () => { ov.hidden = true; }, focus: () => ov.querySelector("#files-close") });
  ov.querySelector("#files-title").textContent = "Files: " + tag;
  const body = ov.querySelector("#files-body");
  body.textContent = "loading…";
  let r;
  try {
    r = await api.runFiles(tag);
  } catch (ex) {
    body.textContent = "Could not list files: " + (ex.message || ex);
    return;
  }
  const files = (r && r.files) || [];
  const rows = files
    .filter((f) => !f.dir)
    .map((f) => {
      const url = api.runFileUrl(tag, f.path);
      return (
        "<div class='files-row'><a href='" + url + "' target='_blank' rel='noopener'>" +
        fmt.escape(f.path) + "</a> <span class='muted'>" + fmtBytes(f.size) + "</span></div>"
      );
    });
  body.innerHTML =
    "<div class='muted' style='margin-bottom:8px'>" + rows.length + " files: click to open or download</div>" +
    rows.join("");
}
