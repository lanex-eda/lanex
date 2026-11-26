// compare.js — run against run (Analytics › Compare), 2 to 12 runs.
//
// One column per run, metrics and settings down the side (the long lists
// scroll down, never sideways). The first column is the reference, tinted;
// every other number says how far it moved from it: Δ, and Δ% where a ratio
// means something, green or red by the metric's own direction from
// LibreLane's registry (metricfmt.delta, the one rule Runs and Analytics use).
// Each run's controls are in its column head (make it the reference, the
// project's baseline, show it on every page, open it in Runs, take it out).
// Then: a layout row at one scale, the headline numbers, the settings that
// differ (paths normalised by the server), charts, and every metric folded by
// base name (corners under it) with Changed / Worse / All.
import { api, fmt } from "./api.js";
import { state } from "./state.js";
import { toCsv, downloadCsv } from "./csvutil.js";
import { gatherRunsScoped, designLabel, contextRun, contextBaseline, contextDesign, normDir, setRun, projectBaseline, setProjectBaseline, ensureProjectMeta } from "./context.js";
import { icon } from "./icons.js";
import { localeNum } from "./intlcache.js";
import { verdictOfRun } from "./verdict.js";
import { openLayer, closeLayer, isLayerOpen } from "./overlay.js";
import { toast } from "./toast.js";
import * as MF from "./metricfmt.js";

const esc = (s) => fmt.escape(s == null ? "" : String(s));
// Up to twelve runs: every point of a typical sweep. At 1366 px twelve
// columns of two-line cells fit beside the label column.
const MAX = 12;

let _runs = [];              // every run of every design opened (newest first)
let _sel = [];               // the run_dirs being compared; the first is the reference
let _data = null;            // the last /api/compare answer
let _filter = "changed";     // every metric: changed | worse | all
const _openBases = new Set();// base metric names opened to their corners
let _seq = 0;

// Runs picked on the Runs page (by run_dir): the next render compares them
// straight away, so Runs' "Compare" lands on the answer.
let _preselect = null;
export function preselectCompare(runDirs) { _preselect = (runDirs || []).slice(0, MAX); }

const rowOf = (dir) => _runs.find((r) => r.run_dir === dir) || null;

export async function renderCompare() {
  const root = document.getElementById("compare-body");
  if (!root) return;
  // Compare is cross-design: every known design's runs, each carrying its own
  // absolute run_dir, which the backend resolves directly. Archived runs stay
  // comparable when picked from Runs, but are left out of "Add a run".
  try { _runs = await gatherRunsScoped("all"); } catch (_e) { _runs = state.runs || []; }
  _runs = _runs.filter((r) => r.run_dir);
  if (!_runs.length) {
    root.innerHTML = "<div class='empty'><h3>No runs to compare</h3><p>Finish at least two runs, then come back.</p></div>";
    return;
  }
  await Promise.all([...new Set(_runs.map((r) => normDir(r._design)))].map((d) => ensureProjectMeta(d)));
  if (_preselect && _preselect.length) { _sel = _preselect; _preselect = null; }
  _sel = _sel.filter((d) => rowOf(d));
  // Nothing picked yet: the run in view against its baseline, when there are both.
  if (!_sel.length) {
    const d = normDir(contextDesign());
    const find = (tag) => tag && _runs.find((r) => r.tag === tag && normDir(r._design) === d);
    const a = find(contextBaseline()), b = find(contextRun());
    if (a && b && a !== b) _sel = [a.run_dir, b.run_dir];
    else if (b) _sel = [b.run_dir];
  }
  root.innerHTML =
    "<div class='cmp-bar'><div class='cmp-line' id='cmp-line' role='status'></div>" +
    "<div class='cmp-add-wrap'><button class='btn btn-sm' id='cmp-add' aria-haspopup='true' aria-expanded='false'>" + icon("plus", { size: 13 }) + " Add a run</button>" +
    "<div class='cmp-pop' id='cmp-pop' role='dialog' aria-label='Add a run to the comparison' hidden></div></div>" +
    "<span class='cmp-spacer'></span>" +
    "<button class='btn btn-ghost btn-sm' id='cmp-export-csv' disabled title='Every setting and metric, one column per run, for Excel or LibreOffice'>" + icon("download", { size: 13 }) + " Export CSV</button></div>" +
    "<div class='menu cmp-colmenu' id='cmp-colmenu' role='menu' hidden></div>" +
    "<div id='cmp-out'></div>";
  wirePicker(root);
  paintLine(root);
  compare(root);
}

// ------------------------------------------------------------------ the line above
function paintLine(root) {
  const host = root.querySelector("#cmp-line");
  const ref = rowOf(_sel[0]);
  const base = ref && projectBaseline(ref._design) === ref.tag;
  host.innerHTML = _sel.length
    ? "<b>" + _sel.length + " run" + (_sel.length === 1 ? "" : "s") + "</b>" + (ref ? " · reference <code>" + esc(ref.tag) + "</code>" +
      (base ? " <span class='rn-basetag' title='The baseline of " + esc(designLabel(ref._design)) + "'>baseline</span>" : "") : "") +
      (_sel.length >= 2 ? " <span class='muted'>· the menu on each run's column makes it the reference</span>" : "")
    : "<span class='muted'>No runs picked yet.</span>";
  const add = root.querySelector("#cmp-add");
  add.disabled = _sel.length >= MAX;
  add.title = _sel.length >= MAX ? "Compare takes up to " + MAX + " runs: take one out to add another" : "";
}

function wirePicker(root) {
  const btn = root.querySelector("#cmp-add"), pop = root.querySelector("#cmp-pop");
  let q = "";
  const fill = () => {
    const left = _runs.filter((r) => !_sel.includes(r.run_dir) && !r.archived &&
      (!q || (r.tag + " " + designLabel(r._design) + " " + (r.note || "") + " " + (r.tags || []).join(" ")).toLowerCase().includes(q)));
    const multi = new Set(_runs.map((r) => r._design)).size > 1;
    let last = null;
    const rows = left.slice(0, 80).map((r) => {
      const head = multi && r._design !== last ? "<div class='rb-group'>" + esc(designLabel(r._design)) + "</div>" : "";
      last = r._design;
      const v = verdictOfRun(r);
      return head + "<button class='cmp-opt' data-dir='" + esc(r.run_dir) + "'><span class='rb-mark is-" + v.cls + "'></span>" +
        "<span class='cmp-opt-n'>" + esc(r.tag) + "</span><span class='cmp-opt-v'>" + esc(v.word) + "</span></button>";
    }).join("");
    pop.querySelector(".cmp-list").innerHTML = rows || "<div class='rb-none'>" + (q ? "No run matches." : "Every run is already in the comparison.") + "</div>";
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isLayerOpen(pop)) { closeLayer(pop); return; }
    q = "";
    pop.innerHTML = "<input class='rb-q cmp-q' type='search' placeholder='Find a run' aria-label='Find a run'/><div class='cmp-list'></div>";
    pop.hidden = false;
    fill();
    pop.querySelector(".cmp-q").addEventListener("input", (ev) => { q = ev.target.value.trim().toLowerCase(); fill(); });
    openLayer(pop, { kind: "popover", owner: btn, onClose: () => { pop.hidden = true; }, focus: () => pop.querySelector(".cmp-q") });
  });
  pop.addEventListener("click", (e) => {
    const o = e.target.closest(".cmp-opt");
    if (!o) return;
    if (_sel.length >= MAX) { toast.show("Compare takes up to " + MAX + " runs.", "info"); return; }
    _sel.push(o.dataset.dir);
    paintLine(root);
    compare(root);
    if (_sel.length >= MAX) closeLayer(pop); else fill();
  });
  root.querySelector("#cmp-export-csv").addEventListener("click", () => { if (_data) exportCompareCsv(_data, buildCols(_data.runs || [])); });
  // A run's column menu.
  const menu = root.querySelector("#cmp-colmenu");
  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-colmenu]");
    if (b) { e.stopPropagation(); openColMenu(root, b, +b.dataset.colmenu); return; }
    const s = e.target.closest("[data-show]");
    if (s) { const r = rowOf(_sel[+s.dataset.show]); if (r) setRun(r.tag, r._design).then(() => toast.show(r.tag + " is shown on every page.", "success", 2500)); }
  });
  menu.addEventListener("click", async (e) => {
    const it = e.target.closest("[data-cact]");
    if (!it) return;
    closeLayer(menu, "api");
    const i = +it.dataset.i, r = rowOf(_sel[i]);
    if (!r) return;
    const a = it.dataset.cact;
    if (a === "ref") { _sel.unshift(_sel.splice(i, 1)[0]); paintLine(root); compare(root); }
    else if (a === "rm") { _sel.splice(i, 1); paintLine(root); compare(root); }
    else if (a === "show") { await setRun(r.tag, r._design); toast.show(r.tag + " is shown on every page.", "success", 2500); }
    else if (a === "runs") { await setRun(r.tag, r._design); import("./nav.js").then((m) => m.go("runs")); }
    else if (a === "base" || a === "unbase") {
      try {
        await setProjectBaseline(r._design, a === "base" ? r.tag : "");
        toast.show(a === "base" ? r.tag + " is the baseline of " + designLabel(r._design) + ": every page measures against it." : "No baseline now for " + designLabel(r._design) + ".", "success", 3000);
      } catch (ex) { toast.show("Couldn't save the baseline: " + (ex.message || ex), "error"); }
      paintLine(root);
      if (_data) paintHeads(root);
    }
  });
}
function openColMenu(root, btn, i) {
  const menu = root.querySelector("#cmp-colmenu");
  if (isLayerOpen(menu)) closeLayer(menu);
  const r = rowOf(_sel[i]);
  if (!r) return;
  const isB = projectBaseline(r._design) === r.tag;
  menu.innerHTML = "<div class='menu-lane'>" + esc(r.tag) + "</div>" +
    (i > 0 ? "<button class='menu-item' role='menuitem' data-cact='ref' data-i='" + i + "'>Make it the reference</button>" : "") +
    "<button class='menu-item' role='menuitem' data-cact='" + (isB ? "unbase" : "base") + "' data-i='" + i + "'>" +
      (isB ? "Clear the baseline of " : "Make it the baseline of ") + esc(designLabel(r._design)) + "</button>" +
    "<button class='menu-item' role='menuitem' data-cact='show' data-i='" + i + "'>Show it on every page</button>" +
    "<button class='menu-item' role='menuitem' data-cact='runs' data-i='" + i + "'>Open it in Runs</button>" +
    "<div class='menu-sep'></div><button class='menu-item' role='menuitem' data-cact='rm' data-i='" + i + "'>Take it out of the comparison</button>" +
    "<div class='menu-note'>The reference is this comparison's only; the baseline is the project's, for every page.</div>";
  const br = btn.getBoundingClientRect(), hr = root.getBoundingClientRect();
  const z = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  menu.style.left = Math.max(0, Math.min((br.left - hr.left) / z - 200, root.clientWidth - 300)) + "px";
  menu.style.top = ((br.bottom - hr.top) / z + 4) + "px";
  menu.hidden = false;
  openLayer(menu, { kind: "menu", owner: btn, onClose: () => { menu.hidden = true; } });
}

// ------------------------------------------------------------------ the comparison
async function compare(root) {
  const out = root.querySelector("#cmp-out");
  const exp = root.querySelector("#cmp-export-csv");
  const my = ++_seq;
  _data = null;
  if (exp) exp.disabled = true;
  disposeCharts();
  if (_sel.length < 2) {
    out.innerHTML = "<div class='an-none cmp-hint'><b>" + (_sel.length ? "Add one more run." : "Pick two runs.") + "</b> " +
      "The first is the reference; the others show how far each number moved from it. Up to " + MAX + " runs, from any design you have opened. " +
      "Ticking runs on the Runs page and pressing Compare brings them here.</div>";
    return;
  }
  if (!out.querySelector(".cmp-sec")) out.innerHTML = "<p class='muted cmp-wait'>Comparing…</p>";
  else out.classList.add("is-loading");
  const picked = _sel.map(rowOf).filter(Boolean);
  let data;
  try { data = await api.compare(picked.map((r) => r.tag), picked.map((r) => r.run_dir)); } catch (ex) {
    if (my !== _seq) return;
    out.classList.remove("is-loading");
    out.innerHTML = "<div class='cmp-error'>Compare failed: " + esc(ex.message || ex) + " <button class='btn btn-ghost btn-sm' id='cmp-retry'>Try again</button></div>";
    out.querySelector("#cmp-retry").onclick = () => compare(root);
    return;
  }
  if (my !== _seq) return;
  out.classList.remove("is-loading");
  // The backend answers in its own order: put the runs back in the picked one.
  const order = new Map(_sel.map((d, i) => [d, i]));
  data.runs = (data.runs || []).slice().sort((a, b) => (order.get(a.run_dir) ?? 99) - (order.get(b.run_dir) ?? 99));
  _data = data;
  if (exp) exp.disabled = false;
  // Columns are keyed by the backend's per-run ``col`` (the unique run_dir),
  // never the tag: two designs can each hold a run named "baseline".
  const cols = buildCols(data.runs || []);
  out.classList.toggle("cmp-dense", cols.length >= 5);
  root.classList.toggle("cmp-many", cols.length > 4);
  out.innerHTML =
    "<section class='cmp-sec'><h3 class='cmp-h'>Layout <span class='cmp-hn'>at one scale: a smaller die looks smaller</span></h3>" + layoutHtml(data, cols) + "</section>" +
    "<div class='cmp-grid'><section class='cmp-block cmp-sec'>" + summaryHtml(data, cols) + "</section>" +
    "<section class='cmp-block cmp-sec'>" + settingsHtml(data, cols) + "</section></div>" +
    "<h3 class='cmp-h'>Charts</h3><div id='cmp-charts' class='cmp-charts'></div>" +
    "<div class='cmp-mhead'><h3 class='cmp-h'>Every metric</h3>" +
    "<div class='rn-seg' role='group' aria-label='Which metrics'>" +
    "<button class='rn-segb' data-mf='changed' title='Metrics where any run differs from the reference'>Changed <span class='rn-n' id='cmp-n-changed'></span></button>" +
    "<button class='rn-segb' data-mf='worse' title='Metrics where any run is worse than the reference, by LibreLane&#39;s direction'>Worse <span class='rn-n' id='cmp-n-worse'></span></button>" +
    "<button class='rn-segb' data-mf='all'>All <span class='rn-n' id='cmp-n-all'></span></button></div>" +
    "<input id='cmp-search' type='search' placeholder='Filter metrics' aria-label='Filter metrics'/></div>" +
    "<div class='cmp-scroll' id='cmp-metrics'></div>";
  const search = out.querySelector("#cmp-search");
  const paint = () => paintMetrics(out, data, cols, search.value.trim().toLowerCase());
  search.oninput = paint;
  out.querySelectorAll("[data-mf]").forEach((b) => b.addEventListener("click", () => { _filter = b.dataset.mf; paint(); }));
  out.querySelector("#cmp-metrics").addEventListener("click", (e) => {
    const t = e.target.closest("[data-fold]");
    if (!t) return;
    const b = t.dataset.fold;
    if (_openBases.has(b)) _openBases.delete(b); else _openBases.add(b);
    paint();
  });
  paint();
  renderCompareCharts(out.querySelector("#cmp-charts"), data, cols);
}

// Build the column descriptors for a comparison from the backend's run rows.
// ``col`` is the unique lookup key (run_dir); ``label`` disambiguates same-named
// runs by prefixing the design whenever a tag repeats or multiple designs are in
// play, so no two columns are ambiguous.
export function buildCols(runs) {
  const tagCount = {};
  for (const r of runs) tagCount[r.tag] = (tagCount[r.tag] || 0) + 1;
  const designs = new Set(runs.map((r) => r.design).filter(Boolean));
  const multiDesign = designs.size > 1;
  return runs.map((r) => {
    const dup = (tagCount[r.tag] || 0) > 1;
    const label = ((multiDesign || dup) && r.design) ? (r.design + " · " + r.tag) : (r.tag || r.col);
    return { col: r.col, tag: r.tag, design: r.design || "", label };
  });
}

// One head per run, the same in every table: its result mark and name (a
// press shows it on every page), the menu, REFERENCE over the first, and
// BASELINE when it is its project's baseline.
function colHeaders(cols) {
  return cols.map((c, i) => {
    const r = rowOf(_sel[i]) || {};
    const v = r.tag ? verdictOfRun(r) : { cls: "none", word: "" };
    const isB = r.tag && projectBaseline(r._design) === r.tag;
    return "<th class='cmp-colh" + (i === 0 ? " is-ref" : "") + "' data-ci='" + i + "'>" +
      (i === 0 || isB ? "<div class='cmp-chk'>" + (i === 0 ? "<span class='cmp-ref' title='Every other run is measured against this one'>Reference</span>" : "") +
        (isB ? "<span class='rn-basetag' title='The baseline of " + esc(designLabel(r._design)) + "'>baseline</span>" : "") + "</div>" : "") +
      "<div class='cmp-ch'><span class='rb-mark is-" + v.cls + "' title='" + esc(v.word) + "'></span>" +
      "<button class='cmp-cn' data-show='" + i + "' title='" + esc(c.label + (r._design ? " · " + designLabel(r._design) : "") + " · " + v.word + ". Press to show it on every page.") + "'>" + esc(shortName(c.label, cols.length)) + "</button>" +
      "<button class='cmp-cm' data-colmenu='" + i + "' aria-haspopup='menu' aria-label='" + esc("Actions for " + c.label) + "'>" + icon("chevronDown", { size: 12 }) + "</button></div></th>";
  }).join("");
}
// Run names in a narrow column keep their start and their end: sweep points
// differ at the end ("sweep-util-40", "sweep-util-45").
function shortName(s, n) {
  const max = n >= 5 ? 12 : 22;
  return s.length <= max ? s : s.slice(0, 3) + "…" + s.slice(-(max - 4));
}
function paintHeads(root) {
  if (!_data) return;
  const cols = buildCols(_data.runs || []);
  const html = colHeaders(cols);
  root.querySelectorAll("#cmp-out .cmp-table thead tr").forEach((tr) => {
    const first = tr.firstElementChild;
    tr.innerHTML = "";
    tr.appendChild(first);
    tr.insertAdjacentHTML("beforeend", html);
  });
}

// How far a number moved from the reference: the shared delta
// (metricfmt.delta). `meta` is the metric's metric_meta entry; `key` names the
// metric when known (else `unit` stands for its kind). `scale` is the row's
// (MF.scaleOf), so a power or voltage delta reads in the unit its row is
// labelled with, never in the reference value's own prefix.
export function deltaOf(v, ref, meta, unit = "", key = null, scale = null) {
  if (typeof v !== "number" || typeof ref !== "number" || !Number.isFinite(v) || !Number.isFinite(ref)) return null;
  const k = key || (unit === "ns" ? "timing__setup__ws" : unit === "µm²" ? "design__die__area" : unit === "µm" ? "route__wirelength" : unit === "mW" || unit === "W" ? "power__total" : "");
  // A summary row in mW without a metric key: its numbers are already mW.
  if (!key && unit === "mW") { v /= 1000; ref /= 1000; scale = { div: 1e-3, unit: "mW" }; }
  const known = meta && meta.direction_known !== false && typeof meta.higher_is_better === "boolean";
  const fx = key ? MF.fixedDirection(key) : null;
  const dl = MF.delta(k, v, ref, null, { scale, dir: fx != null ? fx : known ? (meta.higher_is_better ? 1 : -1) : 0 });
  if (!dl) return null;
  if (dl.same) return { text: "same", cls: "is-same", dl };
  return { text: dl.text, cls: dl.cls, dl };
}
const deltaHtml = (d) => (d ? "<span class='cmp-d " + d.cls + "' title='" + esc(d.dl ? MF.deltaWords(d.dl, "the reference") : "") + "'>" + esc(d.text) + "</span>" : "");

// Render config values readably (lists → comma list, bool/None → text).
function fmtCfg(v) {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.map((x) => String(x).split(/[/\\]/).pop()).join(", ");
  return String(v);
}

// ------------------------------------------------------------------ layout row
function bboxSize(b) {
  const p = String(b || "").trim().split(/\s+/).map(Number);
  return p.length === 4 && p.every(Number.isFinite) ? [p[2] - p[0], p[3] - p[1]] : null;
}
function layoutHtml(data, cols) {
  const runs = data.runs || [];
  const sizes = runs.map((r) => bboxSize(r.die_bbox));
  const maxW = Math.max(0, ...sizes.filter(Boolean).map((s) => s[0]));
  const maxH = Math.max(0, ...sizes.filter(Boolean).map((s) => s[1]));
  return "<div class='cmp-scroll'><table class='cmp-table cmp-lay'><thead><tr><th>Run</th>" + colHeaders(cols) + "</tr></thead><tbody><tr><td class='cmp-lay-l'>Final layout" +
    (maxW ? "<small>die up to " + Math.round(maxW) + " × " + Math.round(maxH) + " µm</small>" : "") + "</td>" +
    runs.map((r, i) => {
      if (!r.render) return "<td class='cmp-lay-c" + (i === 0 ? " is-ref" : "") + "'><span class='muted'>no picture</span></td>";
      const s = sizes[i];
      // One scale: the largest die fills its cell, the others shrink with it.
      const pct = s && maxW && maxH ? Math.max(12, Math.round(Math.min(s[0] / maxW, s[1] / maxH) * 100)) : 100;
      return "<td class='cmp-lay-c" + (i === 0 ? " is-ref" : "") + "'><button class='cmp-thumb' data-show='" + i + "' title='" + esc(r.tag + (s ? " · die " + s[0].toFixed(1) + " × " + s[1].toFixed(1) + " µm" : "")) + "'>" +
        "<img loading='lazy' alt='" + esc("Final layout of " + r.tag) + "' style='width:" + pct + "%' src='/api/compare-image?run_dir=" + encodeURIComponent(r.run_dir) + "'/></button></td>";
    }).join("") + "</tr></tbody></table></div>";
}

// ------------------------------------------------------------------ headline numbers
// Side-by-side headline summary, built from each run's design_summary rows
// (already returned by /api/compare). Rows are the union of labels seen. Keyed
// by the unique per-run column, so same-named runs never merge.
function summaryHtml(data, cols) {
  const runs = data.runs || [], meta = data.metric_meta || {};
  const byCol = {};
  const order = [];
  for (const r of runs) {
    byCol[r.col] = {};
    for (const row of (r.summary || [])) {
      byCol[r.col][row.label] = row;
      if (!order.includes(row.label)) order.push(row.label);
    }
  }
  if (!order.length) return "<h3 class='cmp-h'>Headline numbers</h3><p class='muted'>These runs have no summary numbers (they ended before the layout was built).</p>";
  const ref = cols[0];
  const table = data.metric_table || {};
  return "<h3 class='cmp-h'>Headline numbers</h3>" +
    "<div class='cmp-scroll'><table class='cmp-table cmp-sum'><thead><tr><th>Metric</th>" + colHeaders(cols) + "</tr></thead><tbody>" +
    order.map((label) => {
      const base = byCol[ref.col] && byCol[ref.col][label];
      // The summary carries power in mW and utilisation in %: the row is shown
      // from LibreLane's own value through the shared formatter instead, with
      // one prefix along the row.
      const key = base && base.key || (cols.map((c) => byCol[c.col] && byCol[c.col][label]).find(Boolean) || {}).key;
      const raw = key && table[key] ? table[key] : null;
      const scale = raw ? MF.scaleOf(key, cols.map((c) => raw[c.col])) : null;
      const dir = key ? MF.direction(key, meta) : 0;
      // The unit once, in the row label (cells carry numbers only when dense).
      const unit = scale ? scale.unit : key && raw ? MF.show(key, 1).unit : (base && base.unit) || "";
      return "<tr><td>" + esc(label) + (unit || dir ? " <small class='cmp-u'" + (dir ? " title='" + esc(MF.dirWords(dir)) + "'" : "") + ">" + esc(unit) + (dir ? " " + MF.dirMark(dir) : "") + "</small>" : "") + "</td>" + cols.map((c, i) => {
        const row = byCol[c.col] && byCol[c.col][label];
        const rv = raw && row ? raw[c.col] : null;
        const bv = raw && base ? raw[ref.col] : null;
        let d = null;
        if (i && row && base) {
          d = typeof rv === "number" && typeof bv === "number" ? deltaOf(rv, bv, meta[key], "", key, scale) : deltaOf(row.value, base.value, meta[row.key], row.unit);
          if (d && d.dl && d.dl.dir === 0 && key) d.cls = "";
        }
        return summaryCellHtml(row, d, raw && row ? { key, value: rv, scale } : null, i === 0);
      }).join("") + "</tr>";
    }).join("") + "</tbody></table></div>";
}

// One side-by-side summary cell. Exported + pure so the "rounded display never
// hides the real value" invariant is directly testable. fmt.metric() rounds for
// readability; fmt.titleAttr() carries the EXACT tool value in the cell's title
// so a tape-out comparison can never rest on a number the user cannot verify.
// An absent row stays "—" (never a fabricated 0). `delta` (from deltaOf) adds
// how far the value moved from the reference run.
// `raw` ({key, value, scale}): the metric's own value from metric_table,
// shown through the shared formatter (metricfmt.js) when it is a number.
export function summaryCellHtml(row, delta = null, raw = null, isRef = false) {
  const refCls = isRef ? " is-ref" : "";
  if (!row) return isRef ? "<td class='is-ref'>—</td>" : "<td>—</td>";
  const st = row.status === "pass" ? " pass" : row.status === "fail" ? " fail" : "";
  const cls = " class='cmp-num" + st + refCls + "'";
  if (raw && typeof raw.value === "number" && Number.isFinite(raw.value)) {
    const s = metricShown(raw.key, raw.value, raw.scale);
    return "<td" + cls + fmt.titleAttr(raw.value) + "><span class='cmp-v'>" + fmt.escape(s.text) + (s.unit ? " <small>" + fmt.escape(s.unit) + "</small>" : "") + "</span>" + deltaHtml(delta) + "</td>";
  }
  const unit = row.unit ? " <small>" + fmt.escape(row.unit) + "</small>" : "";
  return "<td" + cls + fmt.titleAttr(row.value) + "><span class='cmp-v'>" + fmt.escape(shortMetric(row.value)) + unit + "</span>" + deltaHtml(delta) + "</td>";
}

// A metric through the shared formatter; power at its row's prefix; areas
// and lengths as whole numbers, as in Runs (the exact value is in the title).
function metricShown(key, v, scale) {
  return scale ? MF.showAt(key, v, scale) : MF.show(key, v, { compact: true });
}

// fmt.metric() pads to 3 decimals, which turns a count of 64 into "64.000".
// When the value has no more digits than that, show it as written (64, 66.4);
// otherwise keep the rounded display (the exact value is in the title).
function shortMetric(v) {
  const r = fmt.metric(v);
  if (typeof v === "number" && Number.isFinite(v) && Math.abs(v) < 100 && Number(r) === v) {
    return localeNum(v, "en-US", { maximumFractionDigits: 3 });
  }
  return r;
}

// ------------------------------------------------------------------ settings
// The server compared each setting with every run's own folders written as
// <run>, <design> and <pdk> (history.compare_runs), so a run imported from
// another PC or a second run folder shows no difference there. A path that
// still differs (another SDC file) is real, and folds under its own head.
const same = (per, cols) => new Set(cols.map((c) => JSON.stringify(per[c.col] ?? null))).size <= 1;
// A metric differs when the runs that have it disagree (a run that stopped
// early has no metrics at all: that is not a difference in every one of them).
const differ = (per, cols) => new Set(cols.map((c) => per[c.col]).filter((v) => v !== undefined && v !== null).map((v) => JSON.stringify(v))).size > 1;

function cfgTable(keys, table, cols, mark) {
  return "<div class='cmp-scroll'><table class='cmp-table cmp-cfg'><thead><tr><th>Setting</th>" + colHeaders(cols) + "</tr></thead><tbody>" +
    keys.map((k) => "<tr><td><code>" + esc(k) + "</code></td>" + cols.map((c, i) => {
      const v = table[k][c.col];
      const diff = mark && i && JSON.stringify(v ?? null) !== JSON.stringify(table[k][cols[0].col] ?? null);
      return "<td class='" + (diff ? "cmp-diff" : "") + (i === 0 ? " is-ref" : "") + "' title='" + esc(Array.isArray(v) ? v.join("\n") : fmtCfg(v)) + "'>" + esc(fmtCfg(v)) + "</td>";
    }).join("") + "</tr>").join("") + "</tbody></table></div>";
}

function settingsHtml(data, cols) {
  const kc = data.key_config || {}, cd = data.config_diff || {};
  const paths = new Set(data.config_paths || []);
  const diffKeys = Object.keys(cd).filter((k) => !paths.has(k) && !same(cd[k], cols));
  const pathKeys = Object.keys(cd).filter((k) => paths.has(k) && !same(cd[k], cols));
  const sameKeys = Object.keys(kc).filter((k) => !diffKeys.includes(k) && same(kc[k], cols));
  const designs = new Set((data.runs || []).map((r) => r.design).filter(Boolean));
  const versions = [...new Set((data.runs || []).map((r) => r.librelane_version).filter(Boolean))];
  const fold = (title, n, body) => "<details class='cmp-fold'><summary>" + esc(title) + " <span class='rn-n'>" + n + "</span></summary>" + body + "</details>";
  return "<h3 class='cmp-h'>Settings that differ</h3>" +
    (designs.size > 1 ? "<p class='cmp-note'>These runs are from " + designs.size + " designs: DESIGN_NAME and the sources differ by design.</p>" : "") +
    (versions.length > 1 ? "<p class='cmp-note'>These runs used different LibreLane versions (" + esc(versions.join(", ")) + "): some numbers may differ for that reason alone.</p>" : "") +
    (diffKeys.length ? cfgTable(diffKeys, cd, cols, true)
      : "<p class='cmp-same'>" + icon("check", { size: 14 }) + " These runs used the same settings" + (pathKeys.length ? " (apart from the files below)" : "") + ".</p>") +
    (pathKeys.length ? fold("Files that differ", pathKeys.length, cfgTable(pathKeys, cd, cols, true)) : "") +
    (sameKeys.length ? fold("The same in every run", sameKeys.length, cfgTable(sameKeys, kc, cols, false)) : "");
}

// ------------------------------------------------------------------ every metric
// Folded by LibreLane's base name: one row per base (its own value when the
// run has it), its corners, nets and iterations under it. Changed: any run
// differs from the reference; Worse: any run is worse by the direction;
// critical metrics (LibreLane's "a change here matters") first.
function groupMetrics(table) {
  const groups = new Map();
  for (const k of Object.keys(table)) {
    const b = MF.parseKey(k).base;
    if (!groups.has(b)) groups.set(b, { base: b, self: null, kids: [] });
    const g = groups.get(b);
    if (k === b) g.self = k; else g.kids.push(k);
  }
  for (const g of groups.values()) g.kids.sort();
  return [...groups.values()].sort((a, b) => a.base.localeCompare(b.base));
}
function paintMetrics(out, data, cols, filter) {
  const table = data.metric_table || {}, meta = data.metric_meta || {};
  const groups = groupMetrics(table);
  const ref = cols[0];
  const worseOf = (k) => cols.slice(1).some((c) => { const d = deltaOf(table[k][c.col], table[k][ref.col], meta[k] || meta[MF.parseKey(k).base], "", k); return d && d.cls === "is-worse"; });
  const keysOf = (g) => (g.self ? [g.self] : []).concat(g.kids);
  const changed = (g) => keysOf(g).some((k) => differ(table[k], cols));
  const worse = (g) => keysOf(g).some((k) => worseOf(k));
  const crit = (g) => !!(meta[g.base] && meta[g.base].critical) || keysOf(g).some((k) => meta[k] && meta[k].critical);
  const nChanged = groups.filter(changed).length, nWorse = groups.filter(worse).length;
  out.querySelector("#cmp-n-changed").textContent = nChanged;
  out.querySelector("#cmp-n-worse").textContent = nWorse;
  out.querySelector("#cmp-n-all").textContent = groups.length;
  out.querySelectorAll("[data-mf]").forEach((b) => {
    const on = b.dataset.mf === _filter;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  let set = groups.filter((g) => (_filter === "all" ? true : _filter === "worse" ? worse(g) : changed(g)));
  if (filter) set = set.filter((g) => keysOf(g).some((k) => k.toLowerCase().includes(filter)));
  if (_filter !== "all") set.sort((a, b) => (crit(b) ? 1 : 0) - (crit(a) ? 1 : 0) || a.base.localeCompare(b.base));
  const host = out.querySelector("#cmp-metrics");
  if (!set.length) {
    host.innerHTML = "<p class='muted cmp-none'>" + (filter ? "No metric matches that filter." : _filter === "worse" ? "No run is worse than the reference in any metric." : "Every metric is the same in these runs.") + "</p>";
    return;
  }
  const row = (k, { sub = false, g = null } = {}) => {
    const per = table[k];
    const scale = MF.scaleOf(k, cols.map((c) => per[c.col]));
    const m = meta[k] || meta[MF.parseKey(k).base];
    const dir = MF.direction(k, meta);
    const unit = scale ? scale.unit : MF.show(k, 1).unit;
    const isCrit = m && m.critical;
    const n = g ? g.kids.length : 0;
    const label = sub ? "<span class='cmp-sub'>" + esc(k.slice(MF.parseKey(k).base.length).replace(/^__/, "")) + "</span>"
      : (n ? "<button class='cmp-fold-b' data-fold='" + esc(g.base) + "' aria-expanded='" + _openBases.has(g.base) + "'>" + icon("chevron", { size: 11 }) + "</button>" : "<span class='cmp-fold-sp'></span>") +
        (isCrit ? "<span class='cmp-crit' title='LibreLane treats a change here as critical'>!</span>" : "") + "<code>" + esc(g ? g.base : k) + "</code>" +
        (n ? " <span class='cmp-kids'>" + n + (g.self ? "" : " values") + "</span>" : "");
    const cells = cols.map((c, i) => {
      if (!sub && g && !g.self) return "<td class='" + (i === 0 ? "is-ref" : "") + "'></td>";
      const v = per[c.col];
      const d = i ? deltaOf(v, per[ref.col], m, "", k, scale) : null;
      if (d && d.dl && !dir) d.cls = "";
      const s = v === undefined || v === null ? null : metricShown(k, v, scale);
      return "<td class='" + (i === 0 ? "is-ref" : "") + "'" + fmt.titleAttr(v) + "><span class='cmp-v'>" + esc(!s ? "—" : s.text) + "</span>" +
        (d && d.cls !== "is-same" ? deltaHtml(d) : d ? "<span class='cmp-d is-same'>same</span>" : "") + "</td>";
    }).join("");
    return "<tr class='" + (sub ? "cmp-subrow" : "") + "'><td title='" + esc(k) + "'><div class='cmp-ml'>" + label + (unit || dir ? "<small class='cmp-u'>" + esc(unit) + (dir ? " " + MF.dirMark(dir) : "") + "</small>" : "") + "</div></td>" + cells + "</tr>";
  };
  host.innerHTML = "<table class='cmp-table cmp-all'><thead><tr><th>Metric</th>" + colHeaders(cols) + "</tr></thead><tbody>" +
    set.map((g) => {
      const head = row(g.self || g.kids[0], { g });
      const open = _openBases.has(g.base) || (filter && g.kids.some((k) => k.toLowerCase().includes(filter)));
      return head + (open ? g.kids.map((k) => row(k, { sub: true })).join("") : "");
    }).join("") + "</tbody></table>";
}

// Export the whole comparison (key config + config diffs + every metric) as one
// CSV for Excel: column 1 = field, then one column per run (headers are the
// disambiguated labels; values keyed by the unique per-run column, never the tag).
function exportCompareCsv(data, cols) {
  const rows = [["field", ...cols.map((c) => c.label)]];
  const kc = data.key_config || {};
  for (const k of Object.keys(kc)) rows.push(["config: " + k, ...cols.map((c) => fmtCfg(kc[k][c.col]))]);
  const cd = data.config_diff || {};
  for (const k of Object.keys(cd)) rows.push(["diff: " + k, ...cols.map((c) => fmtCfg(cd[k][c.col]))]);
  const mt = data.metric_table || {};
  for (const m of Object.keys(mt).sort()) rows.push([m, ...cols.map((c) => mt[m][c.col])]);
  downloadCsv("compare-" + (cols[0] ? cols[0].tag : "runs") + ".csv", toCsv(rows, { excel: true }));
}

// ------------------------------------------------------------------ charts
// Curated key metrics to chart across runs (one small bar chart each). Only the
// ones actually present in the comparison are drawn — never fabricated.
// Names from the one list (metricfmt.js), units from the shared formatter.
const _CHART_METRICS = ["design__instance__count", "design__core__area", "design__instance__utilization",
  "timing__setup__ws", "timing__hold__ws", "power__total", "route__wirelength__estimated"]
  .map((k) => [k, (MF.metricName(k) || k) + (MF.show(k, 1).unit && !/^(W|ratio)$/.test(MF.unitOf(MF.parseKey(k).base)) ? " (" + MF.show(k, 1).unit + ")" : "")]);

// The chart instances of the last render (disposed when it is replaced) and
// one ResizeObserver for all of them.
let _charts = [];
let _chartRO = null;
function disposeCharts() {
  for (const c of _charts) { try { c.dispose(); } catch (_e) { /* already gone */ } }
  _charts = [];
}

async function renderCompareCharts(host, data, cols) {
  if (!host) return;
  disposeCharts();
  if (typeof window.echarts === "undefined") await import("./loading.js").then((m) => m.loadECharts()).catch(() => null);
  if (!host.isConnected) return;
  if (typeof window.echarts === "undefined") { host.innerHTML = "<p class='muted'>Charts need ECharts.</p>"; return; }
  const { runBarsOption, runBarsHeight } = await import("./barchart.js");
  const table = data.metric_table || {};
  const num = (v) => { const n = Number(v); return isFinite(n) ? n : null; };
  const labels = cols.map((c) => c.label);
  const present = _CHART_METRICS.filter(([k]) => table[k] &&
    cols.some((c) => num(table[k][c.col]) !== null));
  if (!present.length) { host.innerHTML = "<p class='muted'>These runs have no chartable metrics in common.</p>"; return; }
  host.innerHTML = present.map(([k, label]) => {
    const um = /\(([^)]+)\)\s*$/.exec(label);
    const name = um ? label.slice(0, um.index).trim() : label;
    const scale = MF.scaleOf(k, cols.map((c) => num(table[k][c.col])));
    const u = scale ? scale.unit : MF.unitOf(MF.parseKey(k).base) === "ratio" ? "%" : um ? um[1] : "";
    return "<figure class='cmp-fig'><figcaption><b>" + fmt.escape(name) + "</b>" + (u ? " <span>" + fmt.escape(u) + "</span>" : "") +
      "<code>" + fmt.escape(k) + "</code></figcaption><div class='cmp-chart' data-k='" + fmt.escape(k) + "' style='height:" + runBarsHeight(cols.length) + "px'></div></figure>";
  }).join("");
  for (const [k, label] of present) {
    const el = host.querySelector(".cmp-chart[data-k='" + (window.CSS && CSS.escape ? CSS.escape(k) : k) + "']");
    if (!el) continue;
    const um = /\(([^)]+)\)\s*$/.exec(label);
    const name = um ? label.slice(0, um.index).trim() : label;
    const raw = cols.map((c) => num(table[k][c.col]));
    const scale = MF.scaleOf(k, raw);
    const pct = MF.unitOf(MF.parseKey(k).base) === "ratio";
    const values = raw.map((v) => (v == null ? null : scale ? v / scale.div : pct ? Number((v * 100).toFixed(4)) : v));
    const unit = scale ? scale.unit : pct ? "%" : um ? um[1] : "";
    const opt = runBarsOption({ labels, values, unit });
    if (!opt) continue;
    const chart = window.echarts.init(el, null, { renderer: "canvas" });
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", name + " comparison across selected runs, matching the values in the comparison tables above.");
    chart.setOption(opt);
    _charts.push(chart);
  }
  if (typeof ResizeObserver === "undefined") return;
  if (!_chartRO) {
    let t = 0;
    _chartRO = new ResizeObserver(() => {
      clearTimeout(t);
      t = setTimeout(() => { for (const c of _charts) { if (!c.isDisposed()) c.resize(); } }, 60);
    });
  }
  _chartRO.disconnect();
  _chartRO.observe(host);
}
