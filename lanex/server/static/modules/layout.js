// layout.js — the Layout tab: see the chip.
//
// One page replaces the old Preview + Layout pair:
//   * the run's layout drawn live by KLayout (PDK colours, any zoom), with a
//     stage strip to step through floorplan → power grid → placement → clock
//     tree → routing → final GDS (the way a P&R tool shows the database at
//     each step);
//   * a side panel shaped after KLayout / Virtuoso: layers (visibility, solo,
//     presets), the cells placed (counts by kind, highlight every instance),
//     find (pins, labels, cell types) and checks (DRC / XOR markers on the
//     layout, as Calibre RVE cross-probes into the layout);
//   * click anything to see what it is, in plain words;
//   * the desktop viewers (KLayout, OpenROAD GUI, Magic, GDS3D) one click away,
//     each saying what it is for, plus the exact terminal commands.
//   * the analysis on the chip (audit 6b, 6c, 6a): net names on click, net
//     search and highlight, the key of what each colour is, OpenROAD's
//     density, congestion and clock tree drawn over the layout, and a timing
//     path from Verify or the export highlighted cell by cell.
// The engine is controller/layoutview.py (in worker processes) and
// odbexport.py; the camera is layoutcanvas.js; the overlays layoutoverlays.js
// and heatmap.js; the key layoutkey.js; the lesson coach.js.

import { api, fmt } from "./api.js";
import { state } from "./state.js";
import { toast } from "./toast.js";
import { openLayer, closeLayer } from "./overlay.js";
import { icon } from "./icons.js";
import * as ctx from "./context.js";
import { mountRunBar } from "./runbar.js";
import { STAGES as FLOW_STAGES } from "./flowstages.js";
import { LayoutCanvas, fmtUm, niceLength } from "./layoutcanvas.js";
import { Layout3D, fmtHeight, fmtSpread } from "./layout3d.js";
import { setRunFilesTag } from "./runfiles.js";
import { toolRoutes, launchTool } from "./layouttools.js";
import { CATEGORY_ORDER, CATEGORY_TIPS } from "./cells-model.js";
import { buildHeat, drawHeat, heatValueAt, histogram, rampCss, rampGradient, scaleTop } from "./heatmap.js";
import { keyRows, keyRowsHtml, paintSwatches, paintHistogram } from "./layoutkey.js";
import { NET_COLORS, POWER_COLORS, CLOCK_COLOR, PATH_COLOR, cleanName, normaliseHighlight, pathFromExport,
  placePoints, drawNets, drawPath, drawClock, flattenMarkers, stepMarker } from "./layoutoverlays.js";
import { startCoach } from "./coach.js";
import { getPref, setPref as setAppPref } from "./prefs.js";

const BG = { dark: "#06080d", light: "#ffffff" };
// The census uses the Cells tab's categories (one taxonomy: the server's
// cells.census, from the Liberty file): its keys, its order, its tips; the
// label comes with each row (kind_label). Only the colours are Layout's own.
const KINDS = {
  logic: { label: "Logic gates", color: "#e879c9" },
  inverter: { label: "Inverters", color: "#d6446f" },
  buffer: { label: "Buffers", color: "#f2c14e" },
  flipflop: { label: "Flip-flops", color: "#4da3ff" },
  latch: { label: "Latches", color: "#7cc4ff" },
  mux: { label: "Multiplexers", color: "#38d6c8" },
  arith: { label: "Adders", color: "#ff7a45" },
  clock: { label: "Clock", color: "#a78bfa" },
  delay: { label: "Delay", color: "#d9a441" },
  tristate: { label: "Tri-state", color: "#c084fc" },
  tie: { label: "Tie cells", color: "#9fbf6a" },
  power: { label: "Power / isolation", color: "#5fbf8f" },
  physical: { label: "Physical only", color: "#6d7d94" },
  other: { label: "Other", color: "#8ea6c4" },
};
const KIND_ORDER = CATEGORY_ORDER.filter((k) => KINDS[k]);
const kindOf = (c) => (KINDS[c.kind] ? c.kind : "other");

const S = {
  wired: false,
  tag: "", design: "",  // the run on screen (the app's, context.js)
  src: null,          // sources() reply for the run
  source: "",         // current source id
  info: null,         // open_session() reply
  vis: new Set(),     // visible layer ids
  labels: pref("labels", "1") === "1",
  outlines: false,
  bg: pref("bg", "dark"),
  hl: null,           // { cell, boxes, total }
  sel: null,          // { hits, x, y }
  pin: null,          // { x, y, t } a found label
  markers: null,      // markers() reply
  showMarkers: true,
  activeMarker: null,
  rulers: 0,
  canvas: null,
  playing: 0,
  three: null,        // Layout3D, made on first use
  mode3d: false,
  meshKey: "",
  commands: null,
  openToken: 0,
  openP: null,        // the openRun in flight
  // ---- analysis on the chip
  nets: [],           // highlighted nets: [{ name, color, g (net geometry), preset }]
  netQ: "", netRes: null, netTok: 0,
  ovl: "",            // the overlay on: "" | "Routing" | "Placement" | "RUDY" | "clock" | "paths"
  ovlLast: "Routing",
  ovlLayer: "",       // routing congestion of one layer ("" = all)
  an: null,           // the OpenROAD analysis of the stage on screen: status + data
  heat: null,         // { key, built }
  heatOpacity: 0.7,
  heatFloor: 0,
  path: null,         // the timing path shown: normaliseHighlight() + cells, netGeom, placed
  pathActive: -1,
  pathList: "setup",
  focus: "",          // the side panel's temporary view: "" | "path" | "clock"
  clockFocus: "",
  flash: null,        // { box, t } an instance found
  visited: new Set(),
  markerPage: {},
  keySolo: "", keySaved: null,
  coach: null,
};

function pref(k, d) { try { return localStorage.getItem("ll.lx." + k) ?? d; } catch (_e) { return d; } }
function setPref(k, v) { try { localStorage.setItem("ll.lx." + k, v); } catch (_e) {} }
const $ = (id) => document.getElementById(id);
const STAGE_NO = Object.fromEntries(FLOW_STAGES.map((s, i) => [s.id, i + 1]));
const esc = (s) => fmt.escape(String(s ?? ""));

// ------------------------------------------------------------------ entry points
let _loading = null;
export async function renderLayout() {
  wireOnce();
  _loading = loadRuns();
  await _loading;
}

// A link to one stage or step of the run on screen (nav.js go("layout", { source })),
// e.g. Flow's "Show in Layout".
export async function revealSource(source) {
  await new Promise((r) => setTimeout(r, 0));   // let the tab's own render start first
  if (_loading) await _loading; else await renderLayout();
  if (!source || !S.src) return;
  const all = [...(S.src.stages || []), ...(S.src.steps || []), ...(S.src.variants || [])];
  if (all.some((s) => s.source === source) && S.source !== source) { stopPlay(); await openSource(source); }
}

// A link to a place on the chip (nav.js go("layout", { box: "x0,y0,x1,y1" }) in
// µm), e.g. one of Verify's DRC places: the final layout, its check markers on,
// zoomed there, with the Checks pane open.
export async function revealBox(box) {
  await new Promise((r) => setTimeout(r, 0));
  if (_loading) await _loading; else await renderLayout();
  const b = String(box || "").split(",").map(Number);
  if (b.length !== 4 || !b.every(Number.isFinite) || !S.src) return;
  const final = (S.src.stages || []).find((s) => s.id === "final");
  if (final && S.source !== final.source) { stopPlay(); await openSource(final.source, { keepView: false }); }
  if (!S.canvas) return;
  if (!S.markers) await loadMarkers(S.tag);
  S.showMarkers = true;
  document.querySelector("#lx-side [data-lx-pane='checks']")?.click();
  const pad = Math.max(b[2] - b[0], b[3] - b[1], 2) * 0.6;
  S.canvas.zoomToBox([b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad], 0);
  S.pin = { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, t: performance.now() };
  S.canvas.paintOverlay();
}

// A highlight from another page: go("layout", { run, highlight }) (nav.js).
// The contract with Verify (lane 4), in full in layoutoverlays.normaliseHighlight:
//   { kind: "path"|"net"|"cells", label, insts: [...], nets: [...], pins: [...],
//     points?: [[pin, x_um, y_um, arrival_ns], ...], slack?, corner?,
//     startpoint?, endpoint?, delay_type?: "max"|"min" }
// A path opens on the Final stage (it comes from signoff STA) unless the
// stage on screen already has its cells; nets and cells stay on the stage.
export async function revealHighlight(h, run) {
  await new Promise((r) => setTimeout(r, 0));
  if (_loading) await _loading; else await renderLayout();
  if (run && run !== S.tag) {
    await ctx.setRun(run);
    if (S.tag !== run) await loadRuns();
  }
  if (S.openP) await S.openP;
  if (!S.src || !S.info) return;
  const hl = normaliseHighlight(h || {});
  if (hl.kind === "net") {
    for (const n of hl.nets) await addNet(n, { zoom: hl.nets.length === 1 });
    showPane("find");
    return;
  }
  const final = (S.src.stages || []).find((s) => s.id === "final");
  if (hl.kind === "path" && final && S.source !== final.source) { stopPlay(); await openSource(final.source, { keepView: false }); }
  await showPath(hl, { from: "link" });
}

// ------------------------------------------------------------------ runs
// The run is the app's (context.js): Runs, the run bar on any results page and
// a finished run all change it, and this page follows.
async function loadRuns() {
  const runs = await ctx.loadRuns();
  // The list could not be loaded at all (LanEx busy or asleep): leave the page
  // as it is; context.js retries and the page repaints when runs arrive.
  if (!runs.length && ctx.runsLoadFailed()) return;
  const tag = ctx.contextRun();
  const empty = !runs.length || !tag;
  $("lx-empty").hidden = !empty;
  $("lx-page").hidden = empty;
  for (const id of ["lx-openin", "lx-save", "lx-cmd"]) { const el = $(id); if (el) el.closest(".lx-split").hidden = empty; }
  if (empty) {
    S.tag = ""; S.design = "";
    setRunFilesTag("");
    return;
  }
  if (tag !== S.tag || ctx.contextDesign() !== S.design || !S.info) { stopPlay(); await openRun(tag); }
}

function openRun(tag) {
  const p = openRunNow(tag);
  S.openP = p;
  p.finally(() => { if (S.openP === p) S.openP = null; });
  return p;
}

async function openRunNow(tag) {
  const tok = ++S.openToken;
  S.tag = tag;
  S.design = ctx.contextDesign();
  S.commands = null;
  S.markers = null;
  S.hl = null; S.sel = null; S.pin = null; S.activeMarker = null;
  // Highlights, the analysis and a shown path belong to the run they came from.
  S.nets = []; S.an = null; S.heat = null; S.path = null; S.flash = null;
  S.visited = new Set(); S.markerPage = {};
  if (S.coach) S.coach.leave();
  closeFocus({ quiet: true });
  hideInspector();
  setRunFilesTag(tag);
  renderDesk();
  let src = null;
  try { src = await api.layoutSources(tag); } catch (ex) { src = { error: ex.message || String(ex), status: ex.status, stages: [] }; }
  if (tok !== S.openToken) return;
  S.src = src;
  if (src.status === 404 && !/run not found/i.test(src.error || "")) {
    // The page is newer than the server process (the UI files update live,
    // the Python server only on restart).
    renderStages();
    showMsg("<h3>Restart LanEx to finish the update</h3><p>This page is newer than the running LanEx server, which " +
      "doesn't know the layout viewer yet. Close LanEx and open it again.</p>");
    return;
  }
  const stages = (src && src.stages) || [];
  if (!stages.length) {
    S.source = "";
    S.info = null;
    renderStages();
    showMsg("<h3>Nothing to draw yet</h3><p>This run stopped before the floorplan was written, so it has no layout. " +
      "Flow shows where it stopped and why.</p>" +
      "<button class='btn btn-ghost btn-sm' data-go='pipeline'>Open Flow</button>");
    setNote("");
    return;
  }
  const final = stages.find((s) => s.id === "final");
  const pick = final || stages[stages.length - 1];
  renderStages();
  await openSource(pick.source, { keepView: false });
  if (!final) setNote("This run stopped before the final GDS. Showing the last stage it reached: <b>" + esc(pick.label) + "</b>.");
  loadMarkers(tag);
}

// ------------------------------------------------------------------ sources
async function openSource(source, { keepView = true } = {}) {
  const tag = S.tag;
  const eng = (S.src && S.src.engine) || {};
  S.source = source;
  paintStageSelection();
  if (!eng.ok) {
    renderFallback(eng.reason);
    return;
  }
  setBusy(true);
  let info;
  try { info = await api.layoutOpen(tag, source); }
  catch (ex) { info = { ok: false, error: ex.message || String(ex) }; }
  setBusy(false);
  if (tag !== S.tag || source !== S.source) return;
  if (!info || info.ok === false) {
    S.info = null;
    showMsg("<h3>KLayout could not draw this</h3><p>" + esc((info && info.error) || "unknown error") + "</p>" +
      "<p class='muted'>The desktop viewers below may still open it.</p>");
    return;
  }
  hideMsg();
  const prevLayers = S.info ? S.info.layers : null;
  S.info = info;
  // What was clicked belongs to the previous picture.
  S.sel = null;
  hideInspector();
  S.vis = carryVisibility(prevLayers, info.layers);
  S.hl = S.hl && info.cells.some((c) => c.name === S.hl.cell) ? S.hl : null;
  if (S.hl) highlightCell(S.hl.cell, { quiet: true });
  ensureCanvas();
  S.canvas.setLayout(info.bbox, { keepView });
  renderLayers();
  renderCells();
  renderFind();
  paintBadge();
  if (S.mode3d) load3d();
  const stage = (S.src.stages || []).find((s) => s.source === source);
  const other = !stage && [...(S.src.steps || []), ...(S.src.variants || [])].find((s) => s.source === source);
  const notes = (info.notes || []).map(esc).join(" ");
  setNote((stage ? "<b class='lx-note-k'>" + esc(stage.label) + "</b>" + esc(stage.blurb) : other ? "<b class='lx-note-k'>Snapshot</b>Step snapshot <code>" + esc(other.path) + "</code>." : "") +
    (info.kind === "def" ? " <span class='muted'>Cells are drawn as their LEF outlines and pins, as LibreLane's <code>OpenInKLayout</code> shows a DEF.</span>" : "") +
    (notes ? " <span class='muted'>" + notes + "</span>" : ""));
  afterSourceOpened();
}

// A new stage on screen: highlights follow it (the same net or path drawn
// from this stage's own shapes), and an overlay that was on is measured for
// it (the export is about a second per stage, then cached).
function afterSourceOpened() {
  S.heat = null;
  S.an = S.an && S.an.key === anKey() ? S.an : null;
  if (S.nets.length) refreshNets();
  if (S.path) relocatePath();
  if (S.ovl) ensureAnalysis({ start: true });
  else peekAnalysis();
  paintKey();
  paintOvlButton();
  paintStepper();
}

// Keep the user's layer choices when switching stage: match by layer name.
function carryVisibility(prev, next) {
  const out = new Set();
  if (!prev || S._visTouched !== true) {
    for (const L of next) if (L.visible) out.add(L.id);
    return out;
  }
  const was = new Map(prev.map((L) => [L.label, S.vis.has(L.id)]));
  for (const L of next) {
    const v = was.has(L.label) ? was.get(L.label) : L.visible;
    if (v) out.add(L.id);
  }
  return out;
}

function renderFallback(reason) {
  const r = S.src && S.src.render;
  showMsg("<h3>The live viewer isn't available</h3><p>" + esc(reason || "KLayout's Python package could not be loaded.") +
    " Install LibreLane's Python packages (they include KLayout) to draw layouts here.</p>" +
    (r ? "<img class='lx-static' src='" + api.runFileUrl(S.tag, r) + "' alt='Rendered layout'/>" : "") +
    "<p class='muted'>The desktop viewers below still work.</p>");
}

// ------------------------------------------------------------------ canvas
function ensureCanvas() {
  if (S.canvas) return;
  const view = $("lx-view");
  S.canvas = new LayoutCanvas(view, {
    renderUrl: (box, w, h, dpr, o = {}) => renderUrl(box, w, h, o.backdrop ? 1 : 1 / dpr) + (o.os > 1 ? "&os=" + o.os : ""),
    bg: () => BG[S.bg] || BG.dark,
    onView: (v) => { paintHud(v); paintMini(v); },
    onHover: (x, y) => paintXY(x, y),
    onClick: (x, y, p) => probeAt(x, y, p),
    onBusy: (b) => setBusy(b),
    onError: (ex) => {
      if (!ex) return;
      // The server dropped the session (restart, cache): reopen once, quietly.
      if (ex.status === 410 && S.tag && Date.now() - (S._reopened || 0) > 5000) {
        S._reopened = Date.now();
        openSource(S.source);
        return;
      }
      toast.show("Layout render failed: " + (ex.message || ex), "error");
    },
    onRulers: (r) => { S.rulers = r.length; },
    onKey: (k) => keyCmd(k),
  });
  S.canvas.drawOverlay = drawOverlay;
  view.classList.toggle("is-light", S.bg === "light");
  // First visit: the gesture hint shows on hover until the first interaction.
  if (pref("hinted", "0") !== "1") {
    $("lx-hint").classList.add("is-first");
    S.canvas.over.addEventListener("pointerdown", () => { $("lx-hint").classList.remove("is-first"); setPref("hinted", "1"); }, { once: true });
  }
}

function renderUrl(box, w, h, res) {
  const q = new URLSearchParams();
  q.set("sid", S.info.sid);
  if (box) q.set("box", box.map((v) => +v.toFixed(5)).join(","));
  q.set("w", String(w));
  q.set("h", String(h));
  q.set("vis", [...S.vis].join(","));
  if (S.outlines) q.set("depth", "1");
  q.set("labels", S.labels ? "1" : "0");
  q.set("bg", BG[S.bg] || BG.dark);
  if (res && res !== 1) q.set("res", String(+res.toFixed(4)));
  return "/api/layout/render?" + q.toString();
}

function restyle() {
  if (S.canvas && S.info) S.canvas.restyle();
  if (S.three) S.three.setVisible(S.vis);
}

function keyCmd(k) {
  if (k === "measure") { toggleMeasure(); return true; }
  if (k === "escape") {
    const had = S.sel || S.pin || S.activeMarker || S.flash;
    if (S.canvas.mode === "measure") toggleMeasure(false);
    if (had) { clearSelection(); return true; }
    // Then the highlights, one press each: the path, the last net.
    if (S.path) { clearPath(); return true; }
    const mine = S.nets.filter((n) => !n.preset);
    if (mine.length) { removeNet(mine[mine.length - 1].name); return true; }
    return true;
  }
  if (k === "n" || k === "N") { stepMarkers(1, k === "N"); return true; }
  if (k === "p" || k === "P") { stepMarkers(-1, k === "P"); return true; }
  if (k === "/") { showPane("find"); const q = $("lx-find-q"); if (q) { q.focus(); q.select(); } return true; }
  if (k === "v" || k === "V") { pickOverlay(S.ovl ? "" : (S.ovlLast || "Routing")); return true; }
  if (k === "l" || k === "L") { toggleLabels(); return true; }
  if (k === "o" || k === "O") { toggleOutlines(); return true; }
  if (k === "[" || k === "]") { stepStage(k === "]" ? 1 : -1); return true; }
  if (k === "3") { toggle3d(); return true; }
  return false;
}

// ------------------------------------------------------------------ overlay
function drawOverlay(ctx, v) {
  const strokeBox = (b, style, fill, dash, minPx = 0) => {
    let [x1, y1] = v.w2s(b[0], b[3]);
    let [x2, y2] = v.w2s(b[2], b[1]);
    if (x2 - x1 < minPx) { const c = (x1 + x2) / 2; x1 = c - minPx / 2; x2 = c + minPx / 2; }
    if (y2 - y1 < minPx) { const c = (y1 + y2) / 2; y1 = c - minPx / 2; y2 = c + minPx / 2; }
    if (x2 < 0 || y2 < 0 || x1 > v.W || y1 > v.H) return;
    ctx.save();
    if (fill) { ctx.fillStyle = fill; ctx.fillRect(x1, y1, x2 - x1, y2 - y1); }
    ctx.strokeStyle = style;
    ctx.lineWidth = 1.5;
    if (dash) ctx.setLineDash(dash);
    ctx.strokeRect(Math.round(x1) + 0.5, Math.round(y1) + 0.5, Math.round(x2 - x1), Math.round(y2 - y1));
    ctx.restore();
  };
  const light = S.bg === "light";
  v.mono = _ioFont ? _ioFont.replace(/^500 11px /, "") : "monospace";
  // A heat map lies on the chip, under every mark.
  const hb = heatBuilt();
  if (hb) drawHeat(ctx, v, hb, { opacity: S.heatOpacity, light });
  if (S.labels) drawIoPins(ctx, v);
  // Highlight and dim (as P&R tools do): veil the rest once, then draw the
  // highlighted cells, nets and path over it.
  const preset = S.nets.length && S.nets.every((n) => n.preset);
  const veil = (S.hl && S.hl.boxes.length) || (S.nets.length && !preset) || S.path || (S.ovl === "clock" && clockData());
  if (veil) {
    ctx.save();
    ctx.fillStyle = light ? "rgba(255,255,255,0.62)" : "rgba(4,6,10,0.62)";
    ctx.fillRect(0, 0, v.W, v.H);
    if (S.hl) for (const b of S.hl.boxes) {
      const [x1, y1] = v.w2s(b[0], b[3]);
      const [x2, y2] = v.w2s(b[2], b[1]);
      if (x2 < 0 || y2 < 0 || x1 > v.W || y1 > v.H) continue;
      ctx.clearRect(x1, y1, Math.max(x2 - x1, 2), Math.max(y2 - y1, 2));
    }
    ctx.restore();
  }
  if (S.hl && S.hl.boxes.length) for (const b of S.hl.boxes) strokeBox(b, "#38d6c8", null, null, 3);
  if (S.ovl === "clock") drawClock(ctx, v, clockData(), { ramp: rampName(), light, focusNet: S.clockFocus });
  drawNets(ctx, v, S.nets);
  if (S.path) drawPath(ctx, v, S.path, { active: S.pathActive, light });
  if (S.flash) {
    const age = (performance.now() - S.flash.t) / 1000;
    strokeBox(S.flash.box, light ? "#0a1320" : "#ffffff", age < 1.2 ? "rgba(77,163,255,0.22)" : null, age < 1.2 ? null : [4, 3], 4);
    if (age < 1.2) requestAnimationFrame(() => S.canvas && S.canvas.paintOverlay());
  }
  // markers
  if (S.markers && S.showMarkers) {
    for (const src of S.markers.sources || []) {
      if (src.hidden) continue;
      for (const it of src.items || []) {
        const active = it === S.activeMarker;
        for (const g of it.geoms) drawGeom(ctx, v, g, active);
      }
    }
  }
  // the selection
  if (S.sel && S.sel.hits.length) {
    const h = S.sel.hits[0];
    if (h.cell_box) strokeBox(h.cell_box, "#4da3ff", "rgba(77,163,255,0.10)", null);
    strokeBox(h.box, "#ffffff", null, [4, 3]);
  }
  // a found pin / label
  if (S.pin) {
    const [x, y] = v.w2s(S.pin.x, S.pin.y);
    const age = (performance.now() - S.pin.t) / 1000;
    ctx.save();
    ctx.strokeStyle = "#f2c14e";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - 16, y); ctx.lineTo(x - 5, y); ctx.moveTo(x + 5, y); ctx.lineTo(x + 16, y);
    ctx.moveTo(x, y - 16); ctx.lineTo(x, y - 5); ctx.moveTo(x, y + 5); ctx.lineTo(x, y + 16); ctx.stroke();
    if (age < 1.6) {
      ctx.globalAlpha = 1 - age / 1.6;
      ctx.beginPath(); ctx.arc(x, y, 9 + age * 30, 0, Math.PI * 2); ctx.stroke();
      requestAnimationFrame(() => S.canvas && S.canvas.paintOverlay());
    }
    ctx.restore();
  }
}

// The chip's I/O pins, named at a readable size around the die edge (KLayout's
// own labels scale with the zoom, so at "fit" they would be specks).
const POWER = /^(VPWR|VGND|VPB|VNB|vccd\d?|vssd\d?|vdda\d?|vssa\d?|VDD|VSS|VCC|GND)$/i;
function ioPins() {
  if (!S.info) return [];
  if (S.info._io) return S.info._io;
  const bb = S.info.bbox;
  const tol = Math.max(bb[2] - bb[0], bb[3] - bb[1]) * 0.012;
  const seen = new Set();
  const out = [];
  for (const l of S.info.labels || []) {
    if (POWER.test(l.text)) continue;
    let side = "";
    if (l.x - bb[0] < tol) side = "w";
    else if (bb[2] - l.x < tol) side = "e";
    else if (l.y - bb[1] < tol) side = "s";
    else if (bb[3] - l.y < tol) side = "n";
    if (!side) continue;
    const key = l.text + "@" + side;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...l, side });
  }
  S.info._io = out;
  return out;
}

// The label font, read once: getComputedStyle on every overlay paint forced a
// style pass of the page (one 400 ms frame on a slow laptop), and --mono never
// changes while the page lives.
let _ioFont = "";
function drawIoPins(ctx, v) {
  const pins = ioPins();
  if (!pins.length) return;
  const light = S.bg === "light";
  ctx.save();
  if (!_ioFont) _ioFont = "500 11px " + (getComputedStyle(document.body).getPropertyValue("--mono") || "monospace");
  ctx.font = _ioFont;
  ctx.textBaseline = "middle";
  const placed = { n: -1e9, s: -1e9, e: -1e9, w: -1e9 };
  // Sorted once per pin set (pan and zoom only move them on screen).
  if (!pins._sorted) pins._sorted = pins.slice().sort((a, b) => (a.side === "n" || a.side === "s") ? a.x - b.x : b.y - a.y);
  const sorted = pins._sorted;
  for (const p of sorted) {
    const [x, y] = v.w2s(p.x, p.y);
    if (x < -40 || y < -40 || x > v.W + 40 || y > v.H + 40) continue;
    const along = p.side === "n" || p.side === "s" ? x : y;
    // Vertical text on n/s sides needs ~13 px between pins, horizontal on e/w too.
    if (along - placed[p.side] < 13) continue;
    placed[p.side] = along;
    ctx.fillStyle = "#38d6c8";
    ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = light ? "#0b5f58" : "#bff3ee";
    // A stroked halo, not shadowBlur: blur on every pin of every frame is
    // the most expensive thing a canvas can be asked for.
    ctx.strokeStyle = light ? "rgba(255,255,255,0.9)" : "rgba(0,0,0,0.9)";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    if (p.side === "n" || p.side === "s") {
      ctx.save();
      ctx.translate(x, y + (p.side === "n" ? -7 : 7));
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = p.side === "n" ? "left" : "right";
      ctx.strokeText(p.text, 0, 0);
      ctx.fillText(p.text, 0, 0);
      ctx.restore();
    } else {
      ctx.textAlign = p.side === "w" ? "right" : "left";
      ctx.strokeText(p.text, x + (p.side === "w" ? -7 : 7), y);
      ctx.fillText(p.text, x + (p.side === "w" ? -7 : 7), y);
    }
  }
  ctx.restore();
}

function drawGeom(ctx, v, g, active) {
  ctx.save();
  ctx.strokeStyle = "#ff3b5c";
  ctx.fillStyle = active ? "rgba(255,59,92,0.42)" : "rgba(255,59,92,0.24)";
  ctx.lineWidth = active ? 2.5 : 1.5;
  const [bx1, by1] = v.w2s(g.bbox[0], g.bbox[3]);
  const [bx2, by2] = v.w2s(g.bbox[2], g.bbox[1]);
  if (bx2 - bx1 < 6 && by2 - by1 < 6) {
    // too small to see: a ring the size of a fingertip
    const cx = (bx1 + bx2) / 2, cy = (by1 + by2) / 2;
    ctx.beginPath(); ctx.arc(cx, cy, active ? 9 : 6, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  } else if (g.kind === "edge-pair" && g.rings.length === 2 && g.rings[0].length === 2 && g.rings[1].length === 2) {
    // The violation is the gap between the two edges (spacing) or across them
    // (width): shade that quad, then draw the edges themselves.
    const [a, b] = g.rings;
    const pts = [a[0], a[1], b[1], b[0]].map((p) => v.w2s(p[0], p[1]));
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.fill();
    ctx.lineWidth = active ? 3 : 2;
    for (const e of g.rings) {
      const [x1, y1] = v.w2s(e[0][0], e[0][1]);
      const [x2, y2] = v.w2s(e[1][0], e[1][1]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }
  } else {
    for (const ring of g.rings) {
      ctx.beginPath();
      ring.forEach((p, i) => { const [x, y] = v.w2s(p[0], p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      if (g.kind === "polygon" || g.kind === "box") { ctx.closePath(); ctx.fill(); }
      ctx.stroke();
    }
  }
  if (active) {
    // Corner brackets: find it at a glance, whatever the layers around it.
    const pad = 10, L = 10;
    const x1 = Math.min(bx1, bx2) - pad, y1 = Math.min(by1, by2) - pad;
    const x2 = Math.max(bx1, bx2) + pad, y2 = Math.max(by1, by2) + pad;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.shadowColor = "rgba(0,0,0,0.9)";
    ctx.shadowBlur = 4;
    ctx.beginPath();
    for (const [cx, cy, dx, dy] of [[x1, y1, 1, 1], [x2, y1, -1, 1], [x1, y2, 1, -1], [x2, y2, -1, -1]]) {
      ctx.moveTo(cx + dx * L, cy); ctx.lineTo(cx, cy); ctx.lineTo(cx, cy + dy * L);
    }
    ctx.stroke();
  }
  ctx.restore();
}

// ------------------------------------------------------------------ HUD, minimap
function paintHud(v) {
  const el = $("lx-scale");
  if (!el) return;
  const target = v.scale * 110;
  const len = niceLength(target);
  const px = len / v.scale;
  el.querySelector("i").style.width = Math.round(px) + "px";
  el.querySelector("span").textContent = fmtUm(len);
}

function paintXY(x, y) {
  const el = $("lx-xy");
  if (!el) return;
  let t = x == null ? "" : "x " + x.toFixed(3) + "  y " + y.toFixed(3) + " µm";
  // Over a heat map: the tile under the cursor and its value.
  const m = x == null ? null : heatMap();
  const hit = m ? heatValueAt(m, x, y) : null;
  if (hit) {
    const meta = OVL[S.ovl] || {};
    t += "   ·   tile " + fmtNum(hit.box[0]) + "–" + fmtNum(hit.box[2]) + " × " + fmtNum(hit.box[1]) + "–" + fmtNum(hit.box[3]) + " µm: " +
      fmtNum(hit.v, 1) + " " + (m.unit || "") + " " + (meta.short || "");
  }
  el.textContent = t;
}

function fmtNum(v, d) {
  if (v == null || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  return (d != null ? v.toFixed(d) : a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2)).replace(/\.0+$|(\.\d*[1-9])0+$/, "$1");
}

function paintMini(v) {
  const c = $("lx-mini");
  if (!c || !S.canvas || !S.info) return;
  const bb = S.info.bbox;
  const vb = v.box;
  const all = vb[0] <= bb[0] && vb[1] <= bb[1] && vb[2] >= bb[2] && vb[3] >= bb[3];
  c.classList.toggle("is-on", !all);
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H);
  const bw = bb[2] - bb[0], bh = bb[3] - bb[1];
  const k = Math.min((W - 12) / bw, (H - 12) / bh);
  const ox = (W - bw * k) / 2, oy = (H - bh * k) / 2;
  const tx = (x) => ox + (x - bb[0]) * k, ty = (y) => H - oy - (y - bb[1]) * k;
  const bd = S.canvas.backdrop;
  ctx.fillStyle = BG[S.bg] || BG.dark;
  ctx.fillRect(0, 0, W, H);
  if (bd) {
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bd.bm, tx(bd.box[0]), ty(bd.box[3]), (bd.box[2] - bd.box[0]) * k, (bd.box[3] - bd.box[1]) * k);
  }
  ctx.strokeStyle = "#4da3ff";
  ctx.lineWidth = 1.5;
  ctx.fillStyle = "rgba(77,163,255,0.14)";
  const x1 = Math.max(0, tx(vb[0])), y1 = Math.max(0, ty(vb[3]));
  const x2 = Math.min(W, tx(vb[2])), y2 = Math.min(H, ty(vb[1]));
  ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
  ctx.strokeRect(x1 + 0.5, y1 + 0.5, Math.max(2, x2 - x1 - 1), Math.max(2, y2 - y1 - 1));
  c._map = { tx, ty, k, ox, oy, bb, H };
}

function miniJump(e) {
  const c = $("lx-mini");
  const m = c._map;
  if (!m || !S.canvas) return;
  const r = c.getBoundingClientRect();
  const sx = (e.clientX - r.left) * (c.width / r.width), sy = (e.clientY - r.top) * (c.height / r.height);
  const x = m.bb[0] + (sx - m.ox) / m.k;
  const y = m.bb[1] + (m.H - m.oy - sy) / m.k;
  S.canvas.centerOn(x, y);
}

function setBusy(b) {
  const el = $("lx-busy");
  if (!el) return;
  clearTimeout(el._t);
  if (b) el._t = setTimeout(() => { el.hidden = false; }, 160);
  else el.hidden = true;
}

function paintBadge() {
  const el = $("lx-badge");
  if (!el || !S.info) return;
  const st = (S.src.stages || []).find((s) => s.source === S.source);
  const bb = S.info.bbox;
  const other = !st && [...(S.src.steps || []), ...(S.src.variants || [])].find((s) => s.source === S.source);
  const open = keyOpen();
  el.innerHTML = "<b>" + esc(st ? st.label : other ? (other.step || other.label || "Snapshot") : S.source) +
    "<button class='lx-key-fold' data-key-fold aria-expanded='" + open + "' aria-controls='lx-key' title='" +
    (open ? "Fold the key" : "Show the key: what each colour is") + "'>" + (open ? "Key" : "Key") +
    "<svg viewBox='0 0 24 24' width='12' height='12' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='" +
    (open ? "M6 15l6-6 6 6" : "M6 9l6 6 6-6") + "'/></svg></button></b><span>" + esc(S.info.top) + " · " +
    fmtUm(bb[2] - bb[0]) + " × " + fmtUm(bb[3] - bb[1]) + "</span>";
  el.title = "";
  paintKey();
}

function showMsg(html) {
  const el = $("lx-msg");
  el.innerHTML = "<div class='lx-msg-in'>" + html + "</div>";
  el.hidden = false;
}
function hideMsg() { const el = $("lx-msg"); if (el) el.hidden = true; }
function setNote(html) { const el = $("lx-note"); if (el) el.innerHTML = html; }

// ------------------------------------------------------------------ stages
function renderStages() {
  const ol = $("lx-stages");
  if (!ol) return;
  const stages = (S.src && S.src.stages) || [];
  const extra = [...((S.src && S.src.steps) || []), ...((S.src && S.src.variants) || [])];
  const thumbH = 64, thumbW = 96;
  const bg = encodeURIComponent(BG[S.bg] || BG.dark);
  const eng = (S.src && S.src.engine) || {};
  ol.innerHTML = stages.map((st, i) =>
    "<li><button class='lx-stage' data-src='" + esc(st.source) + "' title='" + esc(st.blurb) + "'>" +
    "<span class='lx-stage-pic'>" + (eng.ok ? "<img alt='' loading='lazy' src='/api/layout/thumb?tag=" + encodeURIComponent(S.tag) +
      "&source=" + encodeURIComponent(st.source) + "&w=" + thumbW * 2 + "&h=" + thumbH * 2 + "&bg=" + bg + "'/>" : "") + "</span>" +
    // The stage's number in the flow (flowstages.js), the same on every page.
    "<span class='lx-stage-txt'><span class='lx-stage-n'>" + (st.id === "final" ? "GDS" : String(STAGE_NO[st.id] || i + 1).padStart(2, "0")) + "</span>" +
    "<b>" + esc(st.label) + "</b></span></button></li>").join("") +
    stageControls(stages, extra, eng);
  ol.querySelectorAll(".lx-stage").forEach((b) => b.addEventListener("click", () => { stopPlay(); openSource(b.dataset.src); }));
  $("lx-play")?.addEventListener("click", togglePlay);
  $("lx-more")?.addEventListener("change", (e) => { const v = e.target.value; if (v) { stopPlay(); openSource(v); } });
  paintStageSelection();
}

function stageControls(stages, extra, eng) {
  if (!eng.ok) return "";
  const play = stages.length > 1
    ? "<button class='btn btn-ghost btn-sm' id='lx-play' title='Step through every stage, keeping the current view'>" +
      "<svg viewBox='0 0 24 24' width='13' height='13' fill='currentColor' aria-hidden='true'><path d='M7 4l13 8-13 8z'/></svg> <span>Play<span class='lx-play-x'> the flow</span></span></button>"
    : "";
  const more = extra.length
    ? "<select id='lx-more' class='lx-more' aria-label='Any step snapshot' title='Any step that wrote a DEF, or the separate KLayout and Magic stream-outs'>" +
      "<option value=''>Other snapshot…</option>" +
      (S.src.steps.length ? "<optgroup label='Steps'>" + S.src.steps.map((s) => "<option value='" + esc(s.source) + "'>" + esc(s.step) + "</option>").join("") + "</optgroup>" : "") +
      (S.src.variants.length ? "<optgroup label='Final GDS files'>" + S.src.variants.map((s) => "<option value='" + esc(s.source) + "'>" + esc(s.label) + " · " + esc(s.path.split("/").pop()) + "</option>").join("") + "</optgroup>" : "") +
      "</select>"
    : "";
  return play || more ? "<li class='lx-stage-ctl'>" + play + more + "</li>" : "";
}

function paintStageSelection() {
  document.querySelectorAll("#lx-stages .lx-stage").forEach((b) => {
    const on = b.dataset.src === S.source;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-current", on ? "step" : "false");
  });
  const more = $("lx-more");
  if (more) more.value = (S.src.stages || []).some((s) => s.source === S.source) ? "" : S.source;
}

function stepStage(d) {
  const st = (S.src && S.src.stages) || [];
  const i = st.findIndex((s) => s.source === S.source);
  const j = Math.max(0, Math.min(st.length - 1, (i < 0 ? st.length - 1 : i) + d));
  if (st[j] && st[j].source !== S.source) openSource(st[j].source);
}

function togglePlay() { if (S.playing) stopPlay(); else startPlay(); }
function startPlay() {
  const st = (S.src && S.src.stages) || [];
  if (st.length < 2) return;
  let i = 0;
  const btn = $("lx-play");
  if (btn) btn.innerHTML = "<svg viewBox='0 0 24 24' width='13' height='13' fill='currentColor' aria-hidden='true'><path d='M6 5h4v14H6zM14 5h4v14h-4z'/></svg> Stop";
  const tick = async () => {
    if (!S.playing) return;
    await openSource(st[i].source);
    i += 1;
    if (i >= st.length) { stopPlay(); return; }
    S.playing = setTimeout(tick, 1500);
  };
  S.playing = setTimeout(tick, 0);
}
function stopPlay() {
  clearTimeout(S.playing);
  S.playing = 0;
  const btn = $("lx-play");
  if (btn) btn.innerHTML = "<svg viewBox='0 0 24 24' width='13' height='13' fill='currentColor' aria-hidden='true'><path d='M7 4l13 8-13 8z'/></svg> <span>Play<span class='lx-play-x'> the flow</span></span>";
}

// ------------------------------------------------------------------ layers pane
function swatchStyle(L) {
  const c = L.fill, f = L.frame;
  const d = L.dither | 0;
  let bgimg;
  if (d === 0) bgimg = "linear-gradient(" + c + "," + c + ")";
  else if (d === 1) bgimg = "none";
  else {
    const ang = [45, 135, 90, 0][d % 4];
    const gap = 3 + (d % 3);
    bgimg = "repeating-linear-gradient(" + ang + "deg," + c + " 0 1.5px,transparent 1.5px " + gap + "px)";
  }
  return "background-image:" + bgimg + ";border-color:" + f;
}

function renderLayers() {
  const host = $("lx-pane-layers");
  if (!host || !S.info) return;
  const layers = S.info.layers;
  const groups = S.info.groups.filter((g) => layers.some((L) => L.group === g.id));
  const presets = PRESETS;
  // Which preset the visible set is, if any (shown pressed).
  const same = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));
  const matches = presets.map(([id]) => id).filter((id) => same(presetSet(id), S.vis));
  const active = matches.includes(S._preset) ? S._preset : matches[0] || "";   // the one clicked, when two are the same set
  // In 3D the swatches show the 3D colours; layers outside the stack say so.
  const in3d = S.mode3d && S.three && S.three.layers.length ? S.three : null;
  host.innerHTML =
    "<div class='lx-presets' role='group' aria-label='Layer presets'>" +
    presets.map(([id, l, t]) => "<button class='lx-preset' data-preset='" + id + "' aria-pressed='" + (id === active) + "' title='" + esc(t) + "'>" + l + "</button>").join("") +
    "</div>" +
    groups.map((g) => {
      const ls = layers.filter((L) => L.group === g.id);
      const on = ls.filter((L) => S.vis.has(L.id)).length;
      return "<div class='lx-lgroup' data-group='" + g.id + "'>" +
        "<div class='lx-lgroup-h'><label class='lx-check'><input type='checkbox' data-gtoggle='" + g.id + "'" +
        (on === ls.length ? " checked" : "") + (on && on < ls.length ? " data-mixed='1'" : "") + "/><span>" + esc(g.label) + "</span></label>" +
        "<span class='lx-n'>" + on + "<i>/" + ls.length + "</i></span></div>" +
        ls.map((L) => {
          const c3 = in3d ? in3d.colorOf(L.id) : null;
          const sw = c3 ? "background:" + c3 + ";border-color:" + c3 : swatchStyle(L);
          // "drawing" is what nearly every layer is: name the purpose only when it isn't.
          const purpose = L.purpose && !/^(drawing|drw)$/i.test(L.purpose) ? L.purpose : "";
          return "<div class='lx-layer" + (S.vis.has(L.id) ? "" : " is-off") + (in3d && !c3 ? " is-flat" : "") + "' data-lid='" + L.id + "' title='" +
            esc((L.about ? L.about[0].toUpperCase() + L.about.slice(1) + ". " : "") + L.label + ", GDS layer " + L.src + "." +
              (in3d && !c3 ? " Not in this 3D view: outside the PDK's 3D stack, or nothing of it here." : "") + " Alt-click shows only this layer.") + "'>" +
            "<label class='lx-check'><input type='checkbox' data-lid='" + L.id + "'" + (S.vis.has(L.id) ? " checked" : "") + "/>" +
            "<i class='lx-sw" + (c3 ? " is-3d" : "") + "' style='" + sw + "'></i><span class='lx-ln'>" + esc(L.name) + "</span>" +
            (purpose ? "<em class='lx-purpose'>" + esc(purpose) + "</em>" : "") + "</label>" +
            "<span class='lx-src'>" + esc(L.src) + "</span>" +
            "<button class='lx-solo' data-solo='" + L.id + "' title='Show only this layer' aria-label='Show only " + esc(L.label) + "'>only</button></div>";
        }).join("") +
        "</div>";
    }).join("");
  host.querySelectorAll("[data-mixed]").forEach((cb) => { cb.indeterminate = true; });
}

function onLayersClick(e) {
  const t = e.target;
  const preset = t.closest("[data-preset]");
  if (preset) { applyPreset(preset.dataset.preset); return; }
  const solo = t.closest("[data-solo]");
  if (solo) { soloLayer(+solo.dataset.solo); return; }
  const row = t.closest(".lx-layer");
  if (row && e.altKey) { e.preventDefault(); soloLayer(+row.dataset.lid); }
}

function onLayersChange(e) {
  const t = e.target;
  if (t.dataset.gtoggle) {
    for (const L of S.info.layers) if (L.group === t.dataset.gtoggle) t.checked ? S.vis.add(L.id) : S.vis.delete(L.id);
  } else if (t.dataset.lid) {
    const id = +t.dataset.lid;
    t.checked ? S.vis.add(id) : S.vis.delete(id);
  } else return;
  S._visTouched = true;
  renderLayers();
  restyle();
}

function soloLayer(id) {
  const only = S.vis.size === 1 && S.vis.has(id);
  S.vis = only ? new Set(S.info.layers.filter((L) => L.visible).map((L) => L.id)) : new Set([id]);
  S._visTouched = !only;
  renderLayers();
  restyle();
}

const PRESETS = [
  ["all", "Everything", "Every layer the PDK's layer file shows by default"],
  ["wiring", "Wiring", "Only the metal layers, vias and pins: how signals travel"],
  ["power", "Power", "The power grid: supply and ground straps and the rails along each row"],
  ["devices", "Transistors", "Only diffusion, poly, wells and local interconnect: where the transistors are"],
];

// The run's power grid (resolved.json via the server): rail, strap layers
// and the supply and ground nets.
function pdnOf() { return (S.info && S.info.pdn) || (S.src && S.src.pdn) || {}; }

function presetSet(p) {
  const Ls = S.info.layers;
  const keepMisc = (L) => L.group === "misc" && /boundary/i.test(L.name) && L.visible;
  let pick;
  if (p === "wiring") pick = Ls.filter((L) => L.group === "metal" || (L.group === "pin" && L.visible) || keepMisc(L) || (L.group === "local" && /^li/i.test(L.name)));
  else if (p === "devices") pick = Ls.filter((L) => ["device", "local", "well"].includes(L.group) || keepMisc(L));
  else if (p === "power") {
    const pd = pdnOf();
    const want = [pd.rail, pd.vertical, pd.horizontal].filter(Boolean).map((x) => String(x).toLowerCase());
    const lv = (n) => { const m = String(n).match(/(\d+)$/); return m ? +m[1] : /^via$/i.test(n) ? 1 : 0; };
    const levels = want.map(lv).filter((x) => x > 0);
    const lo = Math.min(...levels), hi = Math.max(...levels);
    pick = Ls.filter((L) => (L.group === "metal" && /^(drawing|drw|)$/i.test(L.purpose || "") &&
      (want.includes(String(L.name).toLowerCase()) ||
       // the vias that join the grid's layers (via n joins metal n and n + 1)
       (/^via\d*$/i.test(L.name) && levels.length && lv(L.name) >= lo && lv(L.name) < hi))) || keepMisc(L));
    if (!want.length) pick = Ls.filter((L) => L.group === "metal" || keepMisc(L));
  }
  else pick = Ls.filter((L) => L.visible);
  return new Set(pick.map((L) => L.id));
}

function applyPreset(p) {
  S.vis = presetSet(p);
  S._preset = p;
  S._visTouched = p !== "all";
  S.keySolo = ""; S.keySaved = null;
  // The Power preset also lights the supply and ground nets: metal 1 carries
  // both the rails and signal wires, so the layers alone would mislead.
  const had = S.nets.some((n) => n.preset);
  S.nets = S.nets.filter((n) => !n.preset);
  if (p === "power") {
    const pd = pdnOf();
    if (S.info.names) {
      if (pd.vdd) addNet(pd.vdd, { color: POWER_COLORS.vdd, preset: true, quiet: true });
      if (pd.gnd) addNet(pd.gnd, { color: POWER_COLORS.gnd, preset: true, quiet: true });
    }
  } else if (had) renderNetChips();
  renderLayers();
  restyle();
  paintKey();
  S.canvas && S.canvas.paintOverlay();
}

// ------------------------------------------------------------------ cells pane
// IHP's cells have one underscore after the library (sg13g2_inv_1), the
// others two (sky130_fd_sc_hd__inv_1).
function shortCell(n) { const i = n.indexOf("__"); return i >= 0 ? n.slice(i + 2) : n.replace(/^sg13g2_/, ""); }
function libOf(n) { const i = n.indexOf("__"); return i >= 0 ? n.slice(0, i) : /^sg13g2_/.test(n) ? "sg13g2" : ""; }

// Plain words for a standard-cell name (sky130/gf180/ihp share most stems;
// the IHP and gf180 ones follow their own Verilog's "// type:" lines and the
// Liberty functions: dlhq is a latch open while the enable is high, lgcp a
// clock gate, sighold a bus keeper).
export function describeCell(name) {
  const s = shortCell(name).toLowerCase();
  const m = s.match(/^(.*?)(?:_(\d+))?$/);
  const base = m[1], drive = m[2];
  const D = [
    [/^dfrtp/, "D flip-flop with reset"], [/^dfrtn/, "D flip-flop (falling edge) with reset"], [/^dfstp/, "D flip-flop with set"],
    [/^dfbbp|^dfbbn/, "D flip-flop with set and reset"], [/^dfxtp|^dfxbp/, "D flip-flop"], [/^edf/, "D flip-flop with enable"],
    [/^dfrbp/, "D flip-flop with reset"], [/^dffr|^dffnr/, "D flip-flop with reset"], [/^dffs|^dffns/, "D flip-flop with set"],
    [/^sdf|^sedf/, "scan flip-flop (testable)"], [/^df/, "D flip-flop"],
    [/^s?dlclkp|^s?lgcp|^icgt/, "clock gate"], [/^dlh/, "latch, open while the enable is high"], [/^dll/, "latch, open while the enable is low"],
    [/^dlx|^dlr|^dlatch|^lat/, "latch"],
    [/^ebuf|^bufz/, "tri-state buffer"], [/^einv|^invz/, "tri-state inverter"],
    [/^clkinv/, "clock inverter"], [/^inv/, "inverter"], [/^clkbuf/, "clock buffer"], [/^clkdlybuf|^dlygate|^dlymetal|^clkdly|^dly/, "delay cell (fixes hold timing)"],
    [/^tiehi|^tieh$/, "constant 1"], [/^tielo|^tiel$/, "constant 0"], [/^sighold|^hold$/, "bus keeper: holds the last value"],
    [/^addf/, "full adder"], [/^addh/, "half adder"], [/^antenna/, "antenna diode: protects gates during manufacturing"],
    [/^buf/, "buffer"], [/^xnor(\d)/, "$1-input XNOR"], [/^xor(\d)/, "$1-input XOR"], [/^nand(\d)/, "$1-input NAND"], [/^nor(\d)/, "$1-input NOR"],
    [/^and(\d)/, "$1-input AND"], [/^or(\d)/, "$1-input OR"], [/^a(\d+)oi/, "AND-OR-invert ($1)"], [/^o(\d+)ai/, "OR-AND-invert ($1)"],
    [/^a(\d+)o/, "AND-OR ($1)"], [/^o(\d+)a/, "OR-AND ($1)"], [/^mux(\d)/, "$1-to-1 multiplexer"], [/^fah|^fa/, "full adder"], [/^ha/, "half adder"],
    [/^maj/, "majority gate"], [/^decap/, "decoupling capacitor: steadies the supply"], [/^fill/, "filler: closes a gap in the row"],
    [/^tap/, "well tap: ties the wells to power"], [/^conb/, "constant 0 and 1"], [/^diode/, "antenna diode: protects gates during manufacturing"],
    [/^endcap/, "end cap: closes a row"],
  ];
  for (const [rx, words] of D) {
    const mm = base.match(rx);
    if (mm) return words.replace("$1", mm[1] || "") + (drive ? ", drive strength " + drive : "");
  }
  return "";
}

function renderCells(filter) {
  const host = $("lx-pane-cells");
  if (!host || !S.info) return;
  const cells = S.info.cells || [];
  const q = (filter ?? host.querySelector("#lx-cell-q")?.value ?? "").trim().toLowerCase();
  const total = cells.reduce((a, c) => a + c.count, 0);
  const byKind = {};
  for (const c of cells) (byKind[kindOf(c)] = byKind[kindOf(c)] || []).push(c);
  const label = (k) => (byKind[k] && byKind[k][0].kind_label) || KINDS[k].label;
  const words = (c) => describeCell(c.name) || c.desc || "";
  const logic = total - (byKind.physical || []).reduce((a, c) => a + c.count, 0);
  const bar = KIND_ORDER.filter((k) => byKind[k]).map((k) => {
    const n = byKind[k].reduce((a, c) => a + c.count, 0);
    return "<i style='flex:" + n + ";background:" + KINDS[k].color + "' title='" + esc(label(k) + ": " + n) + "'></i>";
  }).join("");
  const keepQ = host.querySelector("#lx-cell-q")?.value || "";
  host.innerHTML =
    "<div class='lx-census'><div class='lx-census-n'><b>" + total.toLocaleString() + "</b> cells placed" +
    (logic !== total ? "<span class='muted'> · " + logic.toLocaleString() + " doing logic</span>" : "") + "</div>" +
    "<div class='lx-census-bar'>" + bar + "</div></div>" +
    "<div class='lx-find'><input type='search' id='lx-cell-q' placeholder='Filter cells' value='" + esc(keepQ) + "' aria-label='Filter cells' spellcheck='false'/></div>" +
    (S.hl ? "<div class='lx-hl'><span>Highlighting <b>" + S.hl.total + "</b> × " + esc(shortCell(S.hl.cell)) + "</span><button class='btn btn-ghost btn-sm' data-hl-clear>Clear</button></div>" : "") +
    KIND_ORDER.filter((k) => byKind[k]).map((k) => {
      const list = byKind[k].filter((c) => !q || c.name.toLowerCase().includes(q) || words(c).toLowerCase().includes(q));
      if (!list.length) return "";
      const n = list.reduce((a, c) => a + c.count, 0);
      const hint = k === "other" ? "Cells this library doesn't sort, and macros." : CATEGORY_TIPS[k] || "";
      return "<details class='lx-kind'" + (q || k !== "physical" ? " open" : "") + "><summary><i style='background:" + KINDS[k].color + "'></i>" +
        "<b>" + esc(label(k)) + "</b><span class='lx-n'>" + n.toLocaleString() + "</span></summary>" +
        (hint ? "<p class='lx-kind-hint'>" + esc(hint) + "</p>" : "") +
        list.map((c) => "<button class='lx-cell" + (S.hl && S.hl.cell === c.name ? " is-on" : "") + "' data-cell='" + esc(c.name) + "' title='" +
          esc(c.name + (words(c) ? " — " + words(c) : "") + ". " + c.w + " × " + c.h + " µm. Click to highlight every one.") + "'>" +
          "<span class='lx-cell-n'>" + esc(shortCell(c.name)) + "</span><span class='lx-cell-d'>" + esc(words(c)) + "</span>" +
          "<span class='lx-n'>" + c.count + "</span></button>").join("") +
        "</details>";
    }).join("");
  const inp = host.querySelector("#lx-cell-q");
  if (keepQ && document.activeElement?.id === "lx-cell-q") { inp.focus(); inp.setSelectionRange(keepQ.length, keepQ.length); }
}

async function highlightCell(name, { quiet = false, zoom = false } = {}) {
  if (!S.info) return;
  if (S.hl && S.hl.cell === name && !quiet) { S.hl = null; renderCells(); S.canvas.paintOverlay(); return; }
  let r;
  try { r = await api.layoutInstances(S.info.sid, name); } catch (ex) { toast.show("Couldn't find the cells: " + (ex.message || ex), "warn"); return; }
  S.hl = { cell: name, boxes: r.boxes || [], total: r.total || 0 };
  renderCells();
  if (zoom === "ifHidden" && S.hl.boxes.length && S.canvas) {
    const vb = S.canvas.viewBox();
    const seen = S.hl.boxes.some((b) => b[0] < vb[2] && b[2] > vb[0] && b[1] < vb[3] && b[3] > vb[1]);
    zoom = !seen;
  }
  if (zoom && S.hl.boxes.length) {
    const b = S.hl.boxes.reduce((a, x) => [Math.min(a[0], x[0]), Math.min(a[1], x[1]), Math.max(a[2], x[2]), Math.max(a[3], x[3])], [Infinity, Infinity, -Infinity, -Infinity]);
    S.canvas.zoomToBox(b, 0.08);
  }
  S.canvas && S.canvas.paintOverlay();
  if (!quiet && r.truncated) toast.show("Showing the first " + r.boxes.length + " of " + r.total + ".", "info");
}

// ------------------------------------------------------------------ find pane
// Nets and instances come from the server (the DEF's names); pins and cell
// types from what the page already has. One box, four kinds, grouped.
let _netT = 0;
function onFindInput() {
  renderFind();
  clearTimeout(_netT);
  const q = ($("lx-find-q")?.value || "").trim();
  if (!S.info || !S.info.names || q.length < 2) { S.netRes = null; S.netQ = ""; renderFind(); return; }
  _netT = setTimeout(() => searchNets(q), 160);
}

async function searchNets(q) {
  const tok = ++S.netTok;
  const sid = S.info && S.info.sid;
  if (!sid) return;
  S.netQ = q;
  S.netRes = { busy: true };
  renderFind();
  let r;
  try { r = await api.layoutNets(sid, q, 60); } catch (ex) { r = { error: ex.message || String(ex) }; }
  if (tok !== S.netTok || !S.info || S.info.sid !== sid) return;
  S.netRes = r;
  renderFind();
}

function renderNetChips() {
  const host = $("lx-netchips");
  if (!host) return;
  const mine = S.nets.filter((n) => !n.preset);
  host.hidden = !mine.length;
  host.innerHTML = mine.length
    ? "<div class='lx-res-h'>Highlighted <span class='lx-n'>" + mine.length + " of 8</span></div><div class='lx-chips'>" +
      mine.map((n) => "<span class='lx-netchip' style='--c:" + n.color + "' title='" + esc(netTitle(n)) + "'><i></i><button class='lx-netchip-n' data-net-zoom='" + esc(n.name) + "'>" + esc(n.name) +
        "</button><button class='lx-netchip-x' data-net-x='" + esc(n.name) + "' aria-label='Stop highlighting " + esc(n.name) + "'>×</button></span>").join("") +
      (mine.length > 1 ? "<button class='linklike lx-chips-clear' data-net-clear>Clear all</button>" : "") + "</div>"
    : "";
}

function netTitle(n) {
  const g = n.g || {};
  if (!g.found) return n.name + ": not on this stage";
  return n.name + " · " + (g.pins || []).length + " pins" + (g.layers && g.layers.length ? " · " + g.layers.map(layerName).join(" ") : "") +
    (g.length ? " · " + fmtUm(g.length) + " of wire" : " · no wires on this stage yet (flight lines)") + (g.truncated ? " · first " + g.boxes.length + " of " + g.total + " shapes drawn" : "");
}

// "68/20" -> "met1" (the layer list's own name for a GDS layer/datatype).
function layerName(src) {
  const L = S.info && S.info.layers.find((x) => x.src === src && /^(drawing|drw|)$/i.test(x.purpose || ""));
  return L ? L.name : src;
}

function renderFind() {
  const res = $("lx-find-res");
  if (!res || !S.info) return;
  renderNetChips();
  const q = ($("lx-find-q")?.value || "").trim().toLowerCase();
  const labels = S.info.labels || [];
  if (!q) {
    // Without a query: the top-level pins, deduplicated (bus bits collapse).
    const seen = new Map();
    for (const l of labels) {
      const key = l.text.replace(/\[\d+\]$/, "[…]");
      if (!seen.has(key)) seen.set(key, { ...l, n: 0, key });
      seen.get(key).n += 1;
    }
    const pins = [...seen.values()].filter((l) => !/^(VPWR|VGND|VPB|VNB|vccd\d?|vssd\d?|VDD|VSS)$/i.test(l.text));
    const namesNote = S.info.names
      ? "<p class='muted lx-pad lx-find-tip'>Type at least two letters to search nets and instances too. <kbd>/</kbd> comes here from the layout.</p>"
      : "<p class='muted lx-pad lx-find-tip'>This file has no net or instance names (a GDS on its own). Pins, labels and cell types are still found.</p>";
    res.innerHTML = namesNote + (labels.length
      ? "<div class='lx-res-h'>Pins and labels <span class='lx-n'>" + labels.length + "</span></div>" +
        pins.slice(0, 200).map((l) => "<button class='lx-res' data-x='" + l.x + "' data-y='" + l.y + "' data-q='" + esc(l.key.includes("[…]") ? l.key.replace("[…]", "[") : "") + "'>" +
          "<span class='lx-res-t'>" + esc(l.key) + "</span><span class='lx-res-s'>" + (l.n > 1 ? l.n + " labels" : esc(l.layer)) + "</span></button>").join("")
      : "<p class='muted lx-pad'>This view has no text labels. Type a cell name to find cells.</p>");
    return;
  }
  const qc = cleanName(q);
  const hitsL = labels.filter((l) => l.text.toLowerCase().includes(qc)).slice(0, 300);
  const hitsC = (S.info.cells || []).filter((c) => c.name.toLowerCase().includes(qc) || describeCell(c.name).toLowerCase().includes(qc)).slice(0, 100);
  const nr = S.netRes && S.netQ && cleanName(S.netQ).toLowerCase() === qc ? S.netRes : null;
  const nets = (nr && nr.nets) || [];
  const insts = (nr && nr.insts) || [];
  const netsHtml = !S.info.names || qc.length < 2 ? "" :
    !nr || nr.busy ? "<div class='lx-res-h'>Nets and instances</div><p class='muted lx-pad'>Searching the design's names…</p>" :
    nr.error ? "<div class='lx-res-h'>Nets and instances</div><p class='lx-pad lx-err'>Couldn't search the names: " + esc(nr.error) + "</p>" :
    (nets.length ? "<div class='lx-res-h'>Nets <span class='lx-n'>" + (nr.total > nets.length ? nets.length + " of " + nr.total : nets.length) + "</span></div>" +
      nets.map((n) => {
        const on = S.nets.find((x) => x.name === n.name);
        return "<button class='lx-res lx-res-net" + (on ? " is-on" : "") + "' data-fnet='" + esc(n.name) + "'" + (on ? " style='--c:" + on.color + "'" : "") + ">" +
          "<span class='lx-res-t'>" + esc(n.name) + (n.use && n.use !== "SIGNAL" ? " <em class='lx-use'>" + esc(n.use.toLowerCase()) + "</em>" : "") + "</span>" +
          "<span class='lx-res-s'>" + n.pins + (n.pins === 1 ? " pin" : " pins") +
          (n.layers.length ? " · " + n.layers.map(layerName).join(" ") : "") + (n.length ? " · " + fmtUm(n.length) : " · not routed here") + "</span></button>";
      }).join("") : "") +
    (insts.length ? "<div class='lx-res-h'>Instances <span class='lx-n'>" + (nr.insts_total > insts.length ? insts.length + " of " + nr.insts_total : insts.length) + "</span></div>" +
      insts.map((it) => "<button class='lx-res' data-finst='" + esc(it.name) + "' data-box='" + it.box.join(",") + "'><span class='lx-res-t'>" + esc(it.name) +
        "</span><span class='lx-res-s'>" + esc(shortCell(it.cell)) + (describeCell(it.cell) ? " · " + esc(describeCell(it.cell)) : "") + "</span></button>").join("") : "");
  const none = !hitsL.length && !hitsC.length && (!nr || nr.busy || (!nets.length && !insts.length));
  res.innerHTML = netsHtml +
    (hitsL.length ? "<div class='lx-res-h'>Pins and labels <span class='lx-n'>" + hitsL.length + "</span></div>" +
      hitsL.map((l) => "<button class='lx-res' data-x='" + l.x + "' data-y='" + l.y + "'><span class='lx-res-t'>" + esc(l.text) +
        "</span><span class='lx-res-s'>" + esc(l.layer) + " · " + l.x.toFixed(2) + ", " + l.y.toFixed(2) + "</span></button>").join("") : "") +
    (hitsC.length ? "<div class='lx-res-h'>Cell types <span class='lx-n'>" + hitsC.length + "</span></div>" +
      hitsC.map((c) => "<button class='lx-res' data-fcell='" + esc(c.name) + "'><span class='lx-res-t'>" + esc(shortCell(c.name)) +
        "</span><span class='lx-res-s'>" + c.count + " placed" + (describeCell(c.name) ? " · " + esc(describeCell(c.name)) : "") + "</span></button>").join("") : "") +
    (none && !(nr && nr.busy) ? "<p class='muted lx-pad'>Nothing matches “" + esc(q) + "”.</p>" : "");
}

function onFindClick(e) {
  const chip = e.target.closest("[data-net-x], [data-net-zoom], [data-net-clear]");
  if (chip) {
    if (chip.dataset.netX) removeNet(chip.dataset.netX);
    else if (chip.dataset.netZoom) zoomToNet(chip.dataset.netZoom);
    else { S.nets = S.nets.filter((n) => n.preset); renderFind(); S.canvas && S.canvas.paintOverlay(); }
    return;
  }
  const b = e.target.closest(".lx-res");
  if (!b) return;
  if (b.dataset.fnet) {
    if (S.nets.some((n) => n.name === b.dataset.fnet && !n.preset)) zoomToNet(b.dataset.fnet);
    else addNet(b.dataset.fnet, { zoom: true });
    return;
  }
  if (b.dataset.finst) {
    const box = b.dataset.box.split(",").map(Number);
    flashBox(box);
    return;
  }
  if (b.dataset.fcell) { highlightCell(b.dataset.fcell, { zoom: true }); return; }
  if (b.dataset.q) { const inp = $("lx-find-q"); inp.value = b.dataset.q; renderFind(); return; }
  const x = +b.dataset.x, y = +b.dataset.y;
  const span = Math.max(4, Math.min(30, ((S.info.bbox[2] - S.info.bbox[0]) / 12)));
  S.canvas.zoomToBox([x - span / 2, y - span / 2, x + span / 2, y + span / 2], 0);
  S.pin = { x, y, t: performance.now() };
  S.canvas.paintOverlay();
}

// ------------------------------------------------------------------ nets
// Up to eight nets at once, each in its own colour (as the OpenROAD GUI's
// eight highlight groups); the Power preset's supply and ground ride along
// as "preset" nets that the preset owns.
function nextNetColor() {
  const used = new Set(S.nets.filter((n) => !n.preset).map((n) => n.color));
  return NET_COLORS.find((c) => !used.has(c)) || NET_COLORS[S.nets.length % NET_COLORS.length];
}

async function addNet(name, { zoom = false, color = null, preset = false, quiet = false } = {}) {
  if (!S.info || !S.info.names) {
    if (!quiet) toast.show("This layout has no net names (a GDS on its own), so nets can't be highlighted here.", "info");
    return null;
  }
  name = cleanName(name);
  const mine = S.nets.filter((n) => !n.preset);
  if (!preset && mine.some((n) => n.name === name)) { if (zoom) zoomToNet(name); return null; }
  if (!preset && mine.length >= 8) { toast.show("Eight nets are highlighted already. Remove one first (the × on its chip in Find).", "info"); return null; }
  const entry = { name, color: color || nextNetColor(), preset, g: null };
  S.nets.push(entry);
  renderNetChips();
  const sid = S.info.sid;
  let g;
  try { g = await api.layoutNet(sid, name); } catch (ex) { g = { found: false, error: ex.message || String(ex) }; }
  if (!S.info || S.info.sid !== sid || !S.nets.includes(entry)) return null;
  entry.g = g;
  if (!g.found && !quiet) {
    toast.show(g.error ? "Couldn't read the net " + name + ": " + g.error : "No net called " + name + " on this stage.", "warn");
    S.nets = S.nets.filter((n) => n !== entry);
  }
  renderFind();
  if (g.found && g.truncated && !quiet) toast.show(name + " has " + g.total.toLocaleString() + " shapes; the first " + g.boxes.length.toLocaleString() + " are drawn.", "info");
  if (zoom && g.found) zoomToNet(name);
  paintKey();
  S.canvas && S.canvas.paintOverlay();
  return entry;
}

function removeNet(name) {
  S.nets = S.nets.filter((n) => n.preset || n.name !== name);
  renderFind();
  paintKey();
  S.canvas && S.canvas.paintOverlay();
}

function zoomToNet(name) {
  const n = S.nets.find((x) => x.name === name);
  const b = n && n.g && n.g.bbox;
  if (!b || !S.canvas) return;
  zoomClear(b, 0.12);
}

// Zoom to a box in the part of the view the key card does not cover: the
// box is stretched to the right by the card's share of the width.
function zoomClear(b, pad = 0.12) {
  const card = $("lx-corner");
  const inset = card && !$("lx-key").hidden ? card.offsetWidth + 24 : 0;
  const W = S.canvas.W;
  const k = inset && W > inset + 200 ? inset / (W - inset) : 0;
  const w = Math.max(b[2] - b[0], 1e-3) * (1 + 2 * pad);
  S.canvas.zoomToBox([b[0], b[1], b[2] + w * k, b[3]], pad);
}

// The same nets drawn from the new stage's own shapes.
async function refreshNets() {
  const sid = S.info && S.info.sid;
  if (!sid) return;
  if (!S.info.names) { S.nets = []; renderFind(); return; }
  await Promise.all(S.nets.map(async (n) => {
    try { n.g = await api.layoutNet(sid, n.name); } catch (_e) { n.g = { found: false }; }
  }));
  if (!S.info || S.info.sid !== sid) return;
  renderFind();
  S.canvas && S.canvas.paintOverlay();
}

// An instance found: zoom there and ring it for a moment.
function flashBox(box) {
  if (!S.canvas) return;
  const w = box[2] - box[0], h = box[3] - box[1];
  const pad = Math.max(w, h, 1) * 2.5;
  S.canvas.zoomToBox([box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad], 0);
  S.flash = { box, t: performance.now() };
  S.canvas.paintOverlay();
}

// ------------------------------------------------------------------ checks pane
async function loadMarkers(tag) {
  const host = $("lx-pane-checks");
  if (host) host.innerHTML = "<p class='muted lx-pad'>Reading the run's check reports…</p>";
  let m;
  // A failure to read the reports is said as such: it is not "no reports".
  try { m = await api.layoutMarkers(tag); } catch (ex) { m = { sources: [], error: ex.message || String(ex) }; }
  if (tag !== S.tag) return;
  S.markers = m;
  renderChecks();
  paintKey();
  S.canvas && S.canvas.paintOverlay();
}

function renderChecks() {
  const host = $("lx-pane-checks");
  const badge = $("lx-checks-n");
  if (!host) return;
  const srcs = (S.markers && S.markers.sources) || [];
  const total = srcs.reduce((a, s) => a + (s.total || 0), 0);
  // The badge counts what Verify counts: LibreLane's metric where it has one
  // (a report file that contradicts it is listed below, and said).
  const errs = srcs.reduce((a, s) => a + (s.metric ? (s.metric.value || 0) : (s.total || 0)), 0);
  const odd = srcs.some((s) => s.disagree);
  if (badge) { badge.hidden = !errs && !odd; badge.textContent = errs > 999 ? "999+" : errs ? String(errs) : "!"; badge.title = odd ? "A report file disagrees with LibreLane's metric" : ""; }
  const disagree = (s) => s.disagree
    ? "<p class='lx-kind-hint lx-pad lx-disagree'>The report lists " + s.total + (s.total === 1 ? " marker" : " markers") + ", but LibreLane's metric <code>" + esc(s.metric.key) +
      "</code> says " + s.metric.value + ". They disagree; Verify's verdict uses the metric. The report may be edited or left from an earlier run.</p>"
    : "";
  if (S.markers && S.markers.error) {
    host.innerHTML = "<div class='lx-mk-err lx-pad'><span class='vf-mark is-warn'></span><div><b>Couldn't read the run's check reports</b>" +
      "<p>" + esc(S.markers.error) + "</p><p class='muted'>The reports are still in the run folder. Verify reads the same files.</p>" +
      "<button class='btn btn-ghost btn-sm' data-mk-retry>Try again</button></div></div>";
    if (badge) { badge.hidden = false; badge.textContent = "!"; badge.title = "The check reports could not be read"; }
    return;
  }
  if (!srcs.length) {
    host.innerHTML = "<p class='muted lx-pad'>This run has no DRC or XOR reports, so there are no markers to show. The checks run after routing; the Verify tab says which ran.</p>";
    return;
  }
  const broken = srcs.filter((s) => s.ok === false);
  const brokenHtml = broken.map((s) => "<div class='lx-mk-err lx-pad'><span class='vf-mark is-warn'></span><div><b>" + esc(s.label) + ": couldn't read the report</b>" +
    "<p>" + esc(s.error || "unknown error") + "</p><p><a class='btn btn-ghost btn-sm' href='" + api.runFileUrl(S.tag, s.path) + "' download>" +
    esc(s.path.split("/").pop()) + "</a></p></div></div>").join("");
  if (!total && broken.length) {
    host.innerHTML = brokenHtml;
    if (badge) { badge.hidden = false; badge.textContent = "!"; badge.title = "A check report could not be read"; }
    return;
  }
  if (!total) {
    host.innerHTML = "<div class='lx-clean'><span class='vf-mark is-pass'></span><div><b>No markers</b>" +
      "<p>" + srcs.map((s) => esc(s.label)).join(", ") + " found nothing to flag in this run.</p></div></div>" +
      "<p class='muted lx-pad'>When a check does flag something, each marker shows up here and on the layout; click one to zoom to it.</p>" +
      "<button class='btn btn-ghost btn-sm lx-pad' data-go='verify' data-go-section='vf-drc'>Full sign-off in Verify " + icon("chevron", { size: 13 }) + "</button>";
    return;
  }
  const PAGE = 200;
  const open = S._mkOpen || (S._mkOpen = new Set());
  host.innerHTML =
    "<label class='lx-check lx-pad'><input type='checkbox' id='lx-mk-show'" + (S.showMarkers ? " checked" : "") + "/><span>Show markers on the layout</span></label>" +
    "<p class='muted lx-pad lx-mk-keys'>Pick a marker, then <kbd>N</kbd> and <kbd>P</kbd> step through them on the layout.</p>" +
    brokenHtml +
    srcs.map((s, si) => {
      if (s.ok === false) return "";
      if (!s.total) return "<div class='lx-mk-src is-clean'><span class='vf-mark is-" + (s.disagree ? "warn" : "pass") + "'></span><b>" + esc(s.label) + "</b><span class='lx-n'>0</span></div>" + disagree(s);
      const capped = s.truncated ? "<p class='lx-kind-hint lx-pad lx-disagree'>Showing the first " + (s.items || []).length.toLocaleString() + " of " + s.total.toLocaleString() +
        " markers. The report file has them all; KLayout's marker browser opens it.</p>" : "";
      return "<div class='lx-mk-src'><label class='lx-check'><input type='checkbox' data-mk-src='" + si + "'" + (s.hidden ? "" : " checked") + "/>" +
        "<span class='vf-mark is-" + (s.disagree ? "warn" : "fail") + "'></span><b>" + esc(s.label) + "</b></label><span class='lx-n'>" + s.total + "</span></div>" + disagree(s) + capped +
        s.categories.map((c) => {
          const items = (s.items || []).filter((it) => it.cat === c.name);
          const k = si + ":" + c.name;
          const shown = S.markerPage[k] || PAGE;
          const left = items.length - shown;
          return "<details class='lx-kind' data-mk-cat='" + esc(k) + "'" + (open.has(k) ? " open" : "") + "><summary><b class='lx-mk-cat'>" + esc(c.name) + "</b><span class='lx-n'>" + c.count + "</span></summary>" +
            (c.description ? "<p class='lx-kind-hint'>" + esc(c.description) + "</p>" : "") +
            items.slice(0, shown).map((it, ii) => "<button class='lx-res" + (it === S.activeMarker ? " is-on" : "") + (S.visited.has(it) ? " is-seen" : "") +
              "' data-mk='" + si + ":" + (s.items.indexOf(it)) + "'><span class='lx-res-t'>#" + (ii + 1) +
              "</span><span class='lx-res-s'>at " + it.bbox[0].toFixed(2) + ", " + it.bbox[1].toFixed(2) + " µm</span></button>").join("") +
            (left > 0 ? "<button class='btn btn-ghost btn-sm lx-mk-more' data-mk-more='" + esc(k) + "'>Show " + Math.min(PAGE, left) + " more <span class='muted'>(" + left.toLocaleString() + " left)</span></button>" : "") +
            (items.length < c.count ? "<p class='muted lx-pad'>" + (c.count - items.length).toLocaleString() + " more of this kind are past the first " + (s.cap || 4000).toLocaleString() + " read; they are in the report file.</p>" : "") +
            "</details>";
        }).join("") +
        "<p class='lx-pad'><a class='btn btn-ghost btn-sm' href='" + api.runFileUrl(S.tag, s.path) + "' download>" + esc(s.path.split("/").pop()) + "</a></p>";
    }).join("");
  host.querySelectorAll("details[data-mk-cat]").forEach((d) => d.addEventListener("toggle", () => {
    if (d.open) open.add(d.dataset.mkCat); else open.delete(d.dataset.mkCat);
  }));
}

function onChecksClick(e) {
  if (e.target.closest("[data-mk-retry]")) { loadMarkers(S.tag); return; }
  const more = e.target.closest("[data-mk-more]");
  if (more) {
    const k = more.dataset.mkMore;
    S.markerPage[k] = (S.markerPage[k] || 200) + 200;
    renderChecks();
    return;
  }
  const b = e.target.closest("[data-mk]");
  if (!b) return;
  const [si, ii] = b.dataset.mk.split(":").map(Number);
  const it = S.markers.sources[si].items[ii];
  if (!it) return;
  goToMarker(it);
}

// One marker on screen: zoomed to, ringed, counted, and the stepper bar
// below the view says where it is in the list.
function goToMarker(it) {
  S.activeMarker = it;
  S.showMarkers = true;
  S.visited.add(it);
  const bb = it.bbox, w = bb[2] - bb[0], h = bb[3] - bb[1];
  const pad = Math.max(w, h, 0.6) * 2;
  S.canvas.zoomToBox([bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad], 0);
  // The row: in its page, its category open, scrolled into view.
  const list = flattenMarkers(S.markers);
  const at = list.find((m) => m.item === it);
  if (at) {
    const s = S.markers.sources[at.si];
    const k = at.si + ":" + at.cat;
    const idx = (s.items || []).filter((x) => x.cat === at.cat).indexOf(it);
    if (idx >= (S.markerPage[k] || 200)) S.markerPage[k] = Math.ceil((idx + 1) / 200) * 200;
    (S._mkOpen || (S._mkOpen = new Set())).add(k);
  }
  renderChecks();
  const row = document.querySelector("#lx-pane-checks .lx-res.is-on");
  if (row) row.scrollIntoView({ block: "nearest" });
  paintStepper();
}

function stepMarkers(dir, byCat = false) {
  const list = flattenMarkers(S.markers);
  if (!list.length) { flashHint("This run has no markers to step through."); return; }
  const r = stepMarker(list, S.activeMarker, dir, { byCat });
  if (r.i < 0) return;
  if (r.wrapped) toast.show(dir > 0 ? "Back to the first marker." : "Back to the last marker.", "info");
  goToMarker(list[r.i].item);
}

function paintStepper() {
  const el = $("lx-stepper");
  if (!el) return;
  const list = S.activeMarker ? flattenMarkers(S.markers) : [];
  const i = list.findIndex((m) => m.item === S.activeMarker);
  if (i < 0 || S.mode3d) { el.hidden = true; return; }
  const m = list[i];
  el.innerHTML = "<button class='lx-tb' data-step='-1' title='Previous marker (P)' aria-label='Previous marker'>" +
    "<svg viewBox='0 0 24 24' width='16' height='16' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M15 6l-6 6 6 6'/></svg></button>" +
    "<span class='lx-stepper-t'><b>Marker " + (i + 1).toLocaleString() + " of " + list.length.toLocaleString() + "</b><span>" + esc(m.label) + " · <code>" + esc(m.cat) + "</code></span></span>" +
    "<button class='lx-tb' data-step='1' title='Next marker (N)' aria-label='Next marker'>" +
    "<svg viewBox='0 0 24 24' width='16' height='16' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M9 6l6 6-6 6'/></svg></button>" +
    "<button class='lx-tb lx-stepper-x' data-step='x' title='Stop stepping (Esc)' aria-label='Stop stepping through markers'>×</button>";
  el.hidden = false;
}

function onChecksChange(e) {
  if (e.target.id === "lx-mk-show") S.showMarkers = e.target.checked;
  else if (e.target.dataset.mkSrc) S.markers.sources[+e.target.dataset.mkSrc].hidden = !e.target.checked;
  S.canvas && S.canvas.paintOverlay();
}

// ------------------------------------------------------------------ probe / inspector
async function probeAt(x, y, p) {
  if (!S.info || S.canvas.mode === "measure") return;
  const r = Math.max(S.canvas.scale * 3, 0.002);
  let res;
  try { res = await api.layoutProbe(S.info.sid, x, y, r); } catch (_e) { return; }
  const hits = (res && res.hits) || [];
  S.sel = { hits, x, y };
  S.canvas.paintOverlay();
  showInspector(p.sx, p.sy);
}

function showInspector(sx, sy) {
  const el = $("lx-inspect");
  const hits = S.sel.hits;
  S._inAt = [sx, sy];
  if (!hits.length) {
    el.innerHTML = "<button class='lx-x' aria-label='Close'>×</button><p class='muted'>Nothing on the visible layers here.</p>" +
      "<p class='lx-mono'>" + S.sel.x.toFixed(3) + ", " + S.sel.y.toFixed(3) + " µm</p>";
  } else {
    const h = hits[0];
    const others = [...new Set(hits.slice(1).map((x) => x.layer))].filter((l) => l !== h.layer).slice(0, 8);
    const cell = h.cell;
    const kind = cell ? (S.info.cells.find((c) => c.name === cell) || {}) : null;
    const desc = cell ? describeCell(cell) : "";
    const w = h.box[2] - h.box[0], hh = h.box[3] - h.box[1];
    // The net under the click: a routed shape's own net, or the net on the
    // cell pin that was clicked (the DEF's connections).
    const named = !!(S.info.names);
    const netRow = h.net
      ? "<div class='lx-in-net'><div class='lx-in-row'><span>Net</span><b title='" + esc(h.net) + "'>" + esc(h.net) + "</b></div>" +
        (h.pin ? "<p class='lx-in-about'>Pin <code>" + esc(h.pin) + "</code> of " + esc(h.inst || "this cell") + ".</p>" : "") +
        "<div class='lx-in-act'><button class='btn btn-ghost btn-sm' data-in='net'>" +
        (S.nets.some((n) => n.name === h.net && !n.preset) ? "Zoom to net" : "Highlight net") + "</button>" +
        "<button class='btn btn-ghost btn-sm' data-in='copy-net'>Copy name</button></div></div>"
      : named && h.named && !cell && !h.text ? "<p class='lx-in-also'>No net name here: this shape is only in the GDS (fill, or a shape the DEF doesn't carry).</p>"
      : "";
    el.innerHTML =
      "<button class='lx-x' aria-label='Close'>×</button>" +
      "<div class='lx-in-layer'><i class='lx-sw' style='background:" + esc(h.fill) + ";border-color:" + esc(h.fill) + "'></i><b>" + esc(h.layer) + "</b><span class='lx-src'>" + esc(h.src) + "</span></div>" +
      (h.about ? "<p class='lx-in-about'>" + esc(h.about[0].toUpperCase() + h.about.slice(1)) + ".</p>" : "") +
      (h.text ? "<p class='lx-in-row'><span>Label</span><b>" + esc(h.text) + "</b></p>" : "<p class='lx-in-row'><span>Shape</span><b>" + fmtUm(w) + " × " + fmtUm(hh) + "</b></p>") +
      netRow +
      (cell ? "<div class='lx-in-cell'><div class='lx-in-row'><span>In cell</span><b title='" + esc(cell) + "'>" + esc(shortCell(cell)) +
        (h.inst ? " <i class='lx-in-inst'>" + esc(h.inst) + "</i>" : "") + "</b></div>" +
        (desc ? "<p class='lx-in-about'>" + esc(desc[0].toUpperCase() + desc.slice(1)) + ".</p>" : "") +
        (kind && kind.count ? "<p class='lx-in-row'><span>Used</span><b>" + kind.count + " times</b></p>" : "") +
        "<div class='lx-in-act'>" +
        "<button class='btn btn-ghost btn-sm' data-in='hl'>Highlight all " + (kind && kind.count ? kind.count : "") + "</button>" +
        "<button class='btn btn-ghost btn-sm' data-in='zoom'>Zoom to cell</button>" +
        (libOf(cell) ? "<button class='btn btn-ghost btn-sm' data-in='lib' title='Pins, timing and power of this cell in the PDK library'>In Cells " + icon("chevron", { size: 12 }) + "</button>" : "") +
        "</div></div>" : "<p class='lx-in-row'><span>In</span><b>top level (" + esc(S.info.top) + ")</b></p>") +
      (others.length ? "<p class='lx-in-also'>Also here: " + others.map(esc).join(", ") + "</p>" : "");
  }
  el.hidden = false;
  const vw = $("lx-view"), view = { width: vw.clientWidth, height: vw.clientHeight };   // CSS px, like sx/sy
  const W = el.offsetWidth, H = el.offsetHeight;
  let left = sx + 16, top = sy + 16;
  if (left + W > view.width - 8) left = Math.max(8, sx - W - 16);
  if (top + H > view.height - 8) top = Math.max(8, view.height - H - 8);
  el.style.left = left + "px";
  el.style.top = top + "px";
}

function onInspectClick(e) {
  if (e.target.closest(".lx-x")) { clearSelection(); return; }
  const b = e.target.closest("[data-in]");
  if (!b || !S.sel || !S.sel.hits.length) return;
  const h = S.sel.hits[0];
  if (b.dataset.in === "net" && h.net) {
    if (S.nets.some((n) => n.name === h.net && !n.preset)) zoomToNet(h.net);
    else addNet(h.net).then(() => showInspector(S._inAt[0], S._inAt[1]));
    return;
  }
  if (b.dataset.in === "copy-net" && h.net) { copyText(h.net, "Net name copied"); return; }
  if (b.dataset.in === "hl") highlightCell(h.cell, { quiet: true });
  else if (b.dataset.in === "zoom" && h.cell_box) S.canvas.zoomToBox(h.cell_box, 0.25);
  else if (b.dataset.in === "lib") {
    const pdk = S.src && S.src.pdk, scl = S.src && (S.src.scl || libOf(h.cell));
    import("./cells.js").then((m) => m.openCell(h.cell, { pdk, scl })).catch(() => toast.show("The Cells tab couldn't open.", "warn"));
  }
}

function hideInspector() { const el = $("lx-inspect"); if (el) el.hidden = true; }
function clearSelection() {
  S.sel = null; S.pin = null; S.activeMarker = null; S.flash = null;
  hideInspector();
  paintStepper();
  document.querySelectorAll("#lx-pane-checks .lx-res.is-on").forEach((x) => x.classList.remove("is-on"));
  S.canvas && S.canvas.paintOverlay();
}

async function copyText(text, done = "Copied") {
  try { await navigator.clipboard.writeText(text); toast.show(done, "success"); }
  catch (_e) { window.prompt("Copy:", text); }
}

// ------------------------------------------------------------------ the key
// What each colour on screen is (layoutkey.js), and the overlay's scale.
// Inside the corner card under the badge; folds to the badge alone.
function keyOpen() {
  if (S._keyUser != null) return S._keyUser === "1";
  if (getPref("layoutKey") === "0") return false;
  // A narrow or short view starts folded: the card would cover the chip.
  const v = $("lx-view");
  const w = v ? v.clientWidth : 1200, h = v ? v.clientHeight : 800;
  return w >= 760 && h >= 520;
}

function toggleKey() {
  const open = !keyOpen();
  S._keyUser = open ? "1" : "0";
  S._keySelf = true;
  setAppPref("layoutKey", open ? "1" : "0");
  S._keySelf = false;
  paintBadge();
}

function stageId() {
  const st = S.src && (S.src.stages || []).find((s) => s.source === S.source);
  return st ? st.id : "";
}

function paintKey() {
  const el = $("lx-key");
  if (!el) return;
  if (!S.info || S.mode3d) { el.hidden = true; return; }
  const open = keyOpen();
  const ovl = ovlSectionHtml();
  if (!open && !ovl) { el.hidden = true; return; }
  el.hidden = false;
  const light = S.bg === "light";
  const rows = open ? keyRows({ layers: S.info.layers, vis: S.vis, stage: stageId(), kind: S.info.kind, pdn: pdnOf(), preset: S._preset }) : [];
  const extra = [];
  if (open) {
    const pre = S.nets.filter((n) => n.preset && n.g && n.g.found);
    for (const n of pre) extra.push("<span class='lx-key-mark'><i style='background:" + n.color + "'></i>" + esc(n.name) +
      " <span class='muted'>" + (n.name === pdnOf().vdd ? "supply" : "ground") + "</span></span>");
    const mine = S.nets.filter((n) => !n.preset);
    if (mine.length) extra.push("<span class='lx-key-mark'><i class='is-multi' style='background:" + mine[0].color + "'></i>" + mine.length + (mine.length === 1 ? " net" : " nets") + " highlighted</span>");
    if (S.hl) extra.push("<span class='lx-key-mark'><i class='is-ring' style='border-color:#38d6c8'></i>" + S.hl.total + " × " + esc(shortCell(S.hl.cell)) + "</span>");
    if (S.markers && S.showMarkers && (S.markers.sources || []).some((s) => s.total && !s.hidden)) extra.push("<span class='lx-key-mark'><i class='is-ring' style='border-color:#ff3b5c'></i>Check markers</span>");
  }
  // With an overlay or a path on, its scale comes first and the layer rows
  // fold to one line, so the card stays short and off the chip.
  const busy = !!(ovl || S.path);
  const rowsOpen = !busy || S._rowsOpen;
  el.innerHTML = ovl + (open
    ? (busy ? "<button class='lx-key-rowsbtn' data-key='rows' aria-expanded='" + rowsOpen + "'><span>Layers on screen</span><span class='lx-n'>" + rows.length +
        "</span><svg viewBox='0 0 24 24' width='12' height='12' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='" +
        (rowsOpen ? "M6 15l6-6 6 6" : "M6 9l6 6 6-6") + "'/></svg></button>" : "") +
      (rowsOpen ? "<div class='lx-key-rows'>" + keyRowsHtml(rows, { solo: S.keySolo }) + "</div>" : "") +
      (extra.length ? "<div class='lx-key-marks'>" + extra.join("") + "</div>" : "") +
      (rowsOpen ? "<button class='lx-key-coach' data-key='coach'>How to read a layout " + icon("chevron", { size: 12 }) + "</button>" : "")
    : "");
  const byId = new Map(S.info.layers.map((L) => [L.id, L]));
  paintSwatches(el, byId, light);
  const hc = el.querySelector("canvas.lx-heat-hist");
  if (hc) {
    const m = heatMap();
    if (m) paintHistogram(hc, histogram(m.v, 48, 0, scaleTop(m)), rampName(), light);
  }
}

function onKeyClick(e) {
  const fold = e.target.closest("[data-key-fold]");
  if (fold) { toggleKey(); return; }
  const row = e.target.closest("[data-key-row]");
  if (row) {
    const k = row.dataset.keyRow;
    const rows = keyRows({ layers: S.info.layers, vis: S.keySaved || S.vis, stage: stageId(), kind: S.info.kind, pdn: pdnOf() });
    const r = rows.find((x) => x.key === k);
    if (!r) return;
    if (S.keySolo === k) { S.vis = S.keySaved || S.vis; S.keySolo = ""; S.keySaved = null; }
    else { S.keySaved = S.keySaved || new Set(S.vis); S.vis = new Set(r.ids); S.keySolo = k; }
    renderLayers(); restyle(); paintKey();
    return;
  }
  const b = e.target.closest("[data-key], [data-ovl]");
  if (!b) return;
  if (b.dataset.key === "coach") { startReading(); return; }
  if (b.dataset.key === "rows") { S._rowsOpen = !S._rowsOpen; paintKey(); return; }
  const a = b.dataset.ovl;
  if (a === "cancel") { api.layoutAnalysisCancel(S.tag, S.source).catch(() => {}); return; }
  if (a === "retry") { ensureAnalysis({ start: true, force: true }); return; }
  if (a === "off") { pickOverlay(""); return; }
  if (a === "tools") { import("./nav.js").then((m) => m.go("tools")); return; }
  if (a === "stage") { const st = (S.src.stages || []).find((s) => s.id === b.dataset.stage); if (st) openSource(st.source); return; }
  if (a === "copylog") { copyText(b.dataset.log, "Log path copied"); return; }
  if (a === "focus") { openFocus(b.dataset.focus); return; }
}

function onKeyInput(e) {
  const t = e.target;
  if (t.id === "lx-heat-op") { S.heatOpacity = +t.value / 100; t.style.setProperty("--p", t.value + "%"); paintOpLabel(); S.canvas && S.canvas.paintOverlay(); }
  else if (t.id === "lx-heat-floor") { S.heatFloor = +t.value; S.heat = null; t.style.setProperty("--p", (+t.value / +t.max * 100) + "%"); paintOpLabel(); S.canvas && S.canvas.paintOverlay(); }
  else if (t.id === "lx-heat-layer") { S.ovlLayer = t.value; S.heat = null; paintKey(); S.canvas && S.canvas.paintOverlay(); }
}

function paintOpLabel() {
  const o = $("lx-heat-opv"), f = $("lx-heat-floorv");
  if (o) o.textContent = Math.round(S.heatOpacity * 100) + "%";
  if (f) f.textContent = S.heatFloor ? "≥ " + S.heatFloor + " %" : "everything";
}

// ------------------------------------------------------------------ overlays (OpenROAD analysis)
const OVL = {
  Routing: { label: "Routing congestion", short: "of tracks used", unit: "% of routing tracks used in each tile",
    need: "routing", needWords: "Routing congestion exists from global routing on." },
  Placement: { label: "Cell density", short: "covered by cells", unit: "% of each tile covered by cells",
    need: "placement", needWords: "Cell density exists once cells are placed." },
  RUDY: { label: "Wire demand (estimate)", short: "of tracks wanted (estimate)", unit: "% of tracks each tile's nets would need, estimated from each net's box",
    need: "placement", needWords: "Wire demand exists once cells are placed." },
  clock: { label: "Clock tree", unit: "Clock nets from each driver to its flip-flops", need: "placement", needWords: "" },
  paths: { label: "Timing paths", unit: "The worst setup and hold paths, cell by cell", need: "", needWords: "" },
};
const STAGE_ORDER = ["floorplan", "pdn", "placement", "cts", "routing", "final"];
function rampName() { return getPref("layoutRamp") === "turbo" ? "turbo" : "lanex"; }
function anKey() { return S.tag + "|" + S.source; }

// Is this overlay possible on the stage on screen? (Before measuring, from
// the stage; after, from what OpenROAD actually wrote.)
function ovlReason(kind) {
  const meta = OVL[kind];
  if (!meta) return "";
  const data = S.an && S.an.key === anKey() && S.an.data;
  if (data && ["Routing", "Placement", "RUDY"].includes(kind)) return data.heat && data.heat[kind] ? "" : meta.needWords;
  if (data && kind === "clock") return data.clock && data.clock.nets && data.clock.nets.length ? "" : "No clock nets on this stage.";
  if (data && kind === "paths") return (data.paths.setup || []).length || (data.paths.hold || []).length ? "" : "No timing paths: this stage has no constraints (SDC) or Liberty.";
  const sid = stageId();
  if (!sid || !meta.need) return "";
  return STAGE_ORDER.indexOf(sid) < STAGE_ORDER.indexOf(meta.need) ? meta.needWords : "";
}

function fillOvlMenu() {
  const menu = $("lx-ovl-menu");
  if (!menu) return;
  const item = (k) => {
    const meta = OVL[k], why = ovlReason(k);
    return "<button role='menuitemradio' aria-checked='" + (S.ovl === k) + "' data-ovl-pick='" + k + "'" + (why ? " disabled" : "") + ">" +
      "<span class='lx-ovl-mi'><i class='lx-ovl-dot" + (S.ovl === k ? " is-on" : "") + "'></i>" + esc(meta.label) + "</span>" +
      "<span>" + esc(why || meta.unit) + "</span></button>";
  };
  const an = S.an && S.an.key === anKey() ? S.an : null;
  const foot = !an || an.state === "none" ? "Measured by OpenROAD the first time you pick one (about a second), then kept with the run."
    : an.state === "ready" ? "Measured by OpenROAD" + (an.seconds ? " in " + fmtNum(an.seconds, 1) + " s" : "") + (an.step && an.step !== "final" && an.step !== S.source ? " on " + an.step : "") + "."
    : an.state === "running" ? "Measuring with OpenROAD…" : an.state === "failed" ? "The last measurement failed; pick one to try again." : "";
  menu.innerHTML = "<div class='lx-menu-h'>Overlays <span>from OpenROAD</span></div>" +
    ["Routing", "Placement", "RUDY", "clock", "paths"].map(item).join("") +
    (S.ovl ? "<button role='menuitem' data-ovl-pick=''><span class='lx-ovl-mi'>Hide the overlay</span><span>V shows it again</span></button>" : "") +
    "<p class='lx-menu-foot'>" + esc(foot) + "</p>";
}

function paintOvlButton() {
  const b = $("lx-ovl");
  if (b) b.setAttribute("aria-pressed", S.ovl ? "true" : "false");
}

function pickOverlay(kind) {
  if (kind && ovlReason(kind)) { flashHint(ovlReason(kind)); return; }
  S.ovl = kind || "";
  if (kind) S.ovlLast = kind;
  S.heat = null;
  if (kind !== "clock") S.clockFocus = "";
  paintOvlButton();
  if (kind) ensureAnalysis({ start: true });
  if (kind === "paths" || kind === "clock") { if (S.an && S.an.state === "ready") openFocus(kind); }
  else if (S.focus === "clock") closeFocus({ quiet: true });
  paintKey();
  S.canvas && S.canvas.paintOverlay();
}

// Read the export's state for this stage without starting it (the menu then
// knows what is there).
async function peekAnalysis() {
  if (!S.tag || !S.source) return;
  const key = anKey();
  let st;
  try { st = await api.layoutAnalysis(S.tag, S.source, false); } catch (_e) { return; }
  if (key !== anKey() || S.ovl) return;
  S.an = { ...st, key, data: null };
}

let _anPoll = 0;
async function ensureAnalysis({ start = false, force = false } = {}) {
  if (!S.tag || !S.source) return;
  const key = anKey();
  const have = S.an && S.an.key === key ? S.an : null;
  if (have && !force && (have.state === "running" || (have.state === "ready" && have.data))) return;
  let st;
  try {
    st = await api.layoutAnalysis(S.tag, S.source);
    if (start && (force || ["none", "stale"].includes(st.state) || (st.state === "failed" && force))) st = await api.layoutAnalysisStart(S.tag, S.source, force);
    if (st.state === "ready" && !st.data) st = await api.layoutAnalysis(S.tag, S.source);
  } catch (ex) {
    st = { state: "error", error: ex.message || String(ex) };
  }
  if (key !== anKey()) return;
  S.an = { ...st, key };
  S.heat = null;
  if (st.state === "running") {
    clearTimeout(_anPoll);
    _anPoll = setTimeout(() => pollAnalysis(key), 500);
  }
  if (st.state === "ready") onAnalysisReady();
  paintKey();
  paintOvlButton();
  S.canvas && S.canvas.paintOverlay();
}

async function pollAnalysis(key) {
  if (key !== anKey()) return;
  let st;
  try { st = await api.layoutAnalysis(S.tag, S.source); } catch (ex) { st = { state: "error", error: ex.message || String(ex) }; }
  if (key !== anKey()) return;
  S.an = { ...st, key };
  S.heat = null;
  if (st.state === "running") _anPoll = setTimeout(() => pollAnalysis(key), 500);
  else if (st.state === "ready") onAnalysisReady();
  paintKey();
  S.canvas && S.canvas.paintOverlay();
}

function onAnalysisReady() {
  // An overlay this stage cannot show: say so in the key, keep the choice.
  if ((S.ovl === "paths" || S.ovl === "clock") && !S.focus && !ovlReason(S.ovl)) openFocus(S.ovl);
  if (S.focus === "path" && S.path && S.path.from === "export") renderFocus();
}

function heatMap() {
  if (!["Routing", "Placement", "RUDY"].includes(S.ovl)) return null;
  const d = S.an && S.an.key === anKey() && S.an.data;
  const m = d && d.heat && d.heat[S.ovl];
  if (!m) return null;
  if (S.ovl === "Routing" && S.ovlLayer && m.layers && m.layers[S.ovlLayer]) return m.layers[S.ovlLayer];
  return m;
}

function heatBuilt() {
  const m = heatMap();
  if (!m) return null;
  const key = anKey() + "|" + S.ovl + "|" + S.ovlLayer + "|" + rampName() + "|" + S.heatFloor + "|" + S.bg;
  if (S.heat && S.heat.key === key) return S.heat.built;
  S.heat = { key, built: buildHeat(m, { ramp: rampName(), floor: S.heatFloor, light: S.bg === "light" }) };
  return S.heat.built;
}

function clockData() {
  const d = S.an && S.an.key === anKey() && S.an.data;
  return d && d.clock && d.clock.nets && d.clock.nets.length ? d.clock : null;
}

// The overlay's part of the key card: its state, or its scale.
function ovlSectionHtml() {
  if (!S.ovl) return "";
  const meta = OVL[S.ovl];
  const an = S.an && S.an.key === anKey() ? S.an : null;
  const head = "<div class='lx-key-sec'><div class='lx-key-h'><span>" + esc(meta.label) + "</span>" +
    "<button class='lx-x lx-key-x' data-ovl='off' aria-label='Hide the overlay' title='Hide the overlay (V)'>×</button></div>";
  const close = "</div>";
  if (!an || an.state === "none" || an.state === "stale") return head + "<p class='lx-key-p'>Getting ready to measure…</p>" + close;
  if (an.state === "running") {
    return head + "<p class='lx-key-p lx-key-busy'><span class='lx-spin' aria-hidden='true'></span>Measuring with OpenROAD: " + esc(an.phase || "starting") +
      "… <span class='lx-mono'>" + fmtNum(an.elapsed || 0, 1) + " s</span></p><button class='btn btn-ghost btn-sm' data-ovl='cancel'>Cancel</button>" + close;
  }
  if (an.state === "nodb") return head + "<p class='lx-key-p'>" + esc(an.note) + "</p>" + close;
  if (an.state === "failed" || an.state === "error") {
    const tools = an.need === "engine" || an.need === "image";
    return head + "<p class='lx-key-p lx-err'>" + esc(an.error || "OpenROAD couldn't measure this stage.") + "</p>" +
      (an.tail && an.tail.length ? "<pre class='lx-key-tail'>" + esc(an.tail.slice(-4).join("\n")) + "</pre>" : "") +
      (an.log ? "<p class='lx-key-p muted'>Log: <code>" + esc(an.log) + "</code></p>" : "") +
      "<div class='lx-key-act'>" + (tools ? "<button class='btn btn-ghost btn-sm' data-ovl='tools'>Open Tools</button>" : "<button class='btn btn-ghost btn-sm' data-ovl='retry'>Try again</button>") +
      (an.log ? "<button class='btn btn-ghost btn-sm' data-ovl='copylog' data-log='" + esc(an.log) + "'>Copy log path</button>" : "") + "</div>" + close;
  }
  if (an.state !== "ready" || !an.data) return head + "<p class='lx-key-p lx-key-busy'><span class='lx-spin' aria-hidden='true'></span>Reading the measurements…</p>" + close;
  const why = ovlReason(S.ovl);
  if (why) {
    const later = meta.need === "routing" ? "routing" : "placement";
    const st = (S.src.stages || []).find((s) => s.id === later) || (S.src.stages || []).find((s) => s.id === "final");
    return head + "<p class='lx-key-p'>" + esc(why) + "</p>" + (st ? "<button class='btn btn-ghost btn-sm' data-ovl='stage' data-stage='" + esc(st.id) + "'>Show " + esc(st.label) + "</button>" : "") + close;
  }
  const note = an.note ? "<p class='lx-key-p muted'>" + esc(an.note) + "</p>" : "";
  const light = S.bg === "light";
  if (S.ovl === "clock") {
    const c = clockData(), s = c.summary;
    return head + "<p class='lx-key-p'>" + esc(meta.unit) + ".</p>" +
      "<div class='lx-key-marks'><span class='lx-key-mark'><i style='background:" + CLOCK_COLOR + "'></i>Clock net</span>" +
      "<span class='lx-key-mark'><i class='is-ring' style='border-color:" + CLOCK_COLOR + "'></i>Clock buffer</span></div>" +
      "<div class='lx-key-scale'><span class='lx-key-k'>Flip-flops by clock arrival</span><div class='lx-ramp' style='background:" + rampGradient(rampName(), light) + "'></div>" +
      "<div class='lx-ramp-t'><span>" + fmtNum(s.arrival_min, 3) + " ns</span><span>" + fmtNum(s.arrival_max, 3) + " ns</span></div></div>" +
      "<p class='lx-key-sum'>Skew <b>" + (s.skew != null ? fmtNum(s.skew * 1000, 1) + " ps" : "–") + "</b> · " + s.sinks + " flip-flops · " + s.buffers + " buffers</p>" +
      note + "<button class='lx-key-coach' data-ovl='focus' data-focus='clock'>The clock tree, net by net " + icon("chevron", { size: 12 }) + "</button>" + close;
  }
  if (S.ovl === "paths") {
    return head + "<p class='lx-key-p'>" + esc(meta.unit) + ". Pick one in the side panel.</p>" +
      "<div class='lx-key-marks'><span class='lx-key-mark'><i class='is-ring' style='border-color:" + PATH_COLOR + "'></i>Cells on the path</span>" +
      "<span class='lx-key-mark'><i class='is-dot' style='border-color:" + PATH_COLOR + "'></i>Start</span>" +
      "<span class='lx-key-mark'><i style='background:" + PATH_COLOR + "'></i>End</span></div>" + note +
      "<button class='lx-key-coach' data-ovl='focus' data-focus='path'>The paths " + icon("chevron", { size: 12 }) + "</button>" + close;
  }
  const m = heatMap();
  const top = scaleTop(m);
  const routing = S.ovl === "Routing";
  const layers = routing ? Object.keys((an.data.heat.Routing || {}).layers || {}) : [];
  const overWords = S.ovl === "Placement" ? "Over 100 %: cells still overlap (before legalisation)" : "Over 100 %: more wires than tracks";
  return head +
    "<p class='lx-key-p'>" + esc(meta.unit) + (m.xs && m.xs.length > 1 ? ", " + fmtNum(m.xs[1] - m.xs[0], 2) + " µm tiles" : "") + ".</p>" +
    (layers.length ? "<label class='lx-key-sel'><span>Layer</span><select id='lx-heat-layer'><option value=''>All layers</option>" +
      layers.map((l) => "<option value='" + esc(l) + "'" + (l === S.ovlLayer ? " selected" : "") + ">" + esc(l) + "</option>").join("") + "</select></label>" : "") +
    "<div class='lx-key-scale'><canvas class='lx-heat-hist' width='480' height='36' aria-hidden='true'></canvas>" +
    "<div class='lx-ramp' style='background:" + rampGradient(rampName(), light) + "'></div>" +
    "<div class='lx-ramp-t'><span>0</span><span>" + fmtNum(top / 2) + "</span><span>" + fmtNum(top) + " " + esc(m.unit || "") + "</span></div></div>" +
    (m.unit === "%" ? "<p class='lx-key-over'><i class='lx-hatch'></i>" + esc(overWords) + "</p>" : "") +
    "<p class='lx-key-sum'>Max <b>" + fmtNum(m.max, 1) + " " + esc(m.unit || "") + "</b> · " + (m.over || 0) + " of " + m.tiles + " tiles over 100 %</p>" +
    "<label class='lx-slider'><span>Show from</span><output id='lx-heat-floorv'>" + (S.heatFloor ? "≥ " + S.heatFloor + " %" : "everything") + "</output>" +
    "<input type='range' id='lx-heat-floor' min='0' max='" + Math.round(top) + "' step='5' value='" + S.heatFloor + "' style='--p:" + (S.heatFloor / top * 100) + "%' aria-label='Show only tiles at or above'/></label>" +
    "<label class='lx-slider'><span>Opacity</span><output id='lx-heat-opv'>" + Math.round(S.heatOpacity * 100) + "%</output>" +
    "<input type='range' id='lx-heat-op' min='10' max='100' step='5' value='" + Math.round(S.heatOpacity * 100) + "' style='--p:" + Math.round(S.heatOpacity * 100) + "%' aria-label='Overlay opacity'/></label>" +
    note + close;
}

// ------------------------------------------------------------------ focus panel (path, clock tree)
// A temporary view over the side panel's tabs with a clear way back.
function openFocus(kind) {
  kind = kind === "paths" ? "path" : kind;
  S.focus = kind;
  if (kind === "path" && S.ovl !== "paths" && !S.path) S.ovl = "paths";
  renderFocus();
  paintOvlButton();
}

function closeFocus({ quiet = false } = {}) {
  const el = $("lx-focus");
  const was = S.focus;
  S.focus = "";
  if (el) { el.hidden = true; el.innerHTML = ""; }
  document.querySelector("#lx-side .lx-side-tabs")?.removeAttribute("hidden");
  document.querySelectorAll("#lx-side .lx-pane").forEach((p) => { p.hidden = p.id !== "lx-pane-" + pref("pane", "layers"); });
  if (!quiet && was === "clock") { S.clockFocus = ""; S.canvas && S.canvas.paintOverlay(); }
}

function renderFocus() {
  const el = $("lx-focus");
  if (!el || !S.focus) return;
  document.querySelector("#lx-side .lx-side-tabs")?.setAttribute("hidden", "");
  document.querySelectorAll("#lx-side .lx-pane").forEach((p) => { p.hidden = true; });
  el.hidden = false;
  if ($("lx-ws").classList.contains("side-off")) toggleSide();
  const back = "<button class='lx-focus-back' data-focus-back>" +
    "<svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M15 6l-6 6 6 6'/></svg>Back to " +
    esc(pref("pane", "layers")) + "</button>";
  el.innerHTML = back + (S.focus === "clock" ? clockFocusHtml() : pathFocusHtml());
  if (S.focus === "clock") paintClockChart();
}

function slackWord(s) {
  if (s == null) return "";
  return "<span class='lx-slack " + (s < 0 ? "is-neg" : "is-pos") + "'>" + (s >= 0 ? "+" : "") + s.toFixed(3) + " ns</span>";
}

function pathFocusHtml() {
  const d = S.an && S.an.key === anKey() && S.an.data;
  const list = d ? (d.paths[S.pathList] || []) : [];
  const p = S.path;
  let html = "";
  if (p) {
    const placed = p.placed || [];
    const total = placed.length ? (placed[placed.length - 1].arrival ?? 0) - (placed[0].arrival ?? 0) : 0;
    const timed = placed.some((q) => q.arrival != null);
    const miss = (p.missing && p.missing.insts) || [];
    html += "<div class='lx-focus-h'><span class='lx-focus-k'>" + esc(p.kind === "cells" ? "Cells" : p.delay_type === "min" ? "Hold path" : "Setup path") +
      (p.corner ? " · " + esc(p.corner) : "") + "</span><button class='btn btn-ghost btn-sm' data-path-clear>Clear</button></div>" +
      "<p class='lx-focus-t'><b>" + esc(p.startpoint || "?") + "</b> → <b>" + esc(p.endpoint || "?") + "</b></p>" +
      (p.slack != null ? "<p class='lx-focus-s'>Slack " + slackWord(p.slack) + " <span class='muted'>" + (p.slack >= 0 ? "meets timing" : "fails timing") + "</span></p>" : "") +
      (p.from === "link" && p.label ? "<p class='muted lx-focus-l'>" + esc(p.label) + "</p>" : "") +
      ((p.clockInsts || []).length || (p.clockNets || []).length
        ? "<p class='lx-focus-l lx-focus-clk'><i style='background:" + CLOCK_COLOR + "'></i>The clock path is drawn in violet: " +
          Object.keys(p.clockCells || {}).length + " clock cells, " + Object.keys(p.clockNetGeom || {}).length + " clock nets.</p>" : "") +
      (miss.length ? "<p class='lx-focus-miss'>" + miss.length + (miss.length === 1 ? " cell is" : " cells are") + " not on this stage" +
        (stageId() !== "final" ? " (the tools add buffers later). <button class='linklike' data-path-final>Show on Final</button>" : ": " + esc(miss.slice(0, 4).join(", ")) + (miss.length > 4 ? " …" : "")) + "</p>" : "") +
      "<table class='lx-ptab" + (timed ? "" : " is-untimed") + "' aria-label='Path, stage by stage'><thead><tr><th>#</th><th>Pin</th><th>Cell</th>" +
      (timed ? "<th class='num'>Incr</th><th class='num'>Arrival</th>" : "") + "</tr></thead><tbody>" +
      placed.map((q, i) => {
        const share = total > 0 && q.incr != null ? Math.max(0, Math.min(1, q.incr / total)) : 0;
        return "<tr data-path-row='" + i + "'" + (i === S.pathActive ? " class='is-on'" : "") + " tabindex='-1'><td class='num'>" + (i + 1) + "</td>" +
          "<td><code>" + esc(q.pin) + "</code></td><td>" + esc(q.cell ? shortCell(q.cell) : q.inst ? "" : "port") + "</td>" +
          (timed ? "<td class='num'>" + (q.incr != null ? q.incr.toFixed(3) : "") + "<i class='lx-pbar' style='--w:" + (share * 100).toFixed(1) + "%'></i></td>" +
            "<td class='num'>" + (q.arrival != null ? q.arrival.toFixed(3) : "") + "</td>" : "") + "</tr>";
      }).join("") + "</tbody></table>" +
      "<p class='muted lx-focus-l'>" + (timed ? "Times in ns. " : "The delays of each stage are in Verify's path report. ") +
      "↑ and ↓ walk the path; a row zooms to its cell.</p>";
  }
  if (d) {
    html += "<div class='lx-focus-h lx-focus-h2'><span class='lx-focus-k'>Worst paths on this stage</span>" +
      "<span class='lx-seg lx-focus-seg' role='group' aria-label='Setup or hold'>" +
      ["setup", "hold"].map((k) => "<button data-plist='" + k + "' aria-pressed='" + (S.pathList === k) + "'>" + (k === "setup" ? "Setup" : "Hold") + "</button>").join("") + "</span></div>" +
      (list.length ? "<div class='lx-plist'>" + list.slice(0, 100).map((q, i) =>
        "<button class='lx-res lx-prow" + (p && p.from === "export" && p.index === i && p.list === S.pathList ? " is-on" : "") + "' data-pick-path='" + i + "'>" +
        "<span class='lx-res-t'>" + slackWord(q.slack) + " <span class='lx-prow-se'>" + esc(q.startpoint) + " → " + esc(q.endpoint) + "</span></span>" +
        "<span class='lx-res-s'>" + (q.points || []).length + " pins · " + esc(q.group || "") + "</span></button>").join("") + "</div>"
        : "<p class='muted lx-pad'>No " + S.pathList + " paths on this stage (no constraints or Liberty here).</p>") +
      (stageId() !== "final" && d ? "<p class='muted lx-focus-l'>Before extraction (stage " + esc(stageId() || S.source) + "), wire delay is an estimate from placement.</p>" : "");
  } else if (!p) {
    html += "<p class='muted lx-pad'>Measuring with OpenROAD…</p>";
  }
  return html;
}

function clockFocusHtml() {
  const c = clockData();
  if (!c) return "<p class='muted lx-pad'>" + (S.an && S.an.state === "running" ? "Measuring with OpenROAD…" : "No clock tree on this stage.") + "</p>";
  const s = c.summary;
  const drivers = c.nets.filter((n) => n.driver || n.port);
  return "<div class='lx-focus-h'><span class='lx-focus-k'>Clock tree · " + esc((s.roots || []).join(", ") || "clock") + "</span></div>" +
    "<p class='lx-focus-t'>" + s.nets + " nets · " + s.buffers + " buffers · " + s.sinks + " flip-flops</p>" +
    "<p class='lx-focus-s'>Arrival " + fmtNum(s.arrival_min, 3) + "–" + fmtNum(s.arrival_max, 3) + " ns · skew <b>" + (s.skew != null ? fmtNum(s.skew * 1000, 1) + " ps" : "–") + "</b></p>" +
    "<div class='lx-clk-chart' id='lx-clk-chart' role='img' aria-label='Flip-flops by clock arrival time'></div>" +
    "<div class='lx-res-h'>Nets <span class='lx-n'>" + drivers.length + "</span></div>" +
    drivers.map((n) => {
      const ffs = n.sinks.filter((k) => k.ff), arr = ffs.map((k) => k.arrival).filter((x) => x != null);
      const src = n.driver ? n.driver.inst + " (" + shortCell(n.driver.cell) + ")" : "the " + (n.port && n.port.pin) + " pin";
      return "<button class='lx-res" + (S.clockFocus === n.name ? " is-on" : "") + "' data-clk-net='" + esc(n.name) + "'><span class='lx-res-t'>" + esc(n.name) + "</span>" +
        "<span class='lx-res-s'>driven by " + esc(src) + " · " + n.sinks.length + (n.sinks.length === 1 ? " sink" : " sinks") + (ffs.length ? " · " + ffs.length + " flip-flops" : "") +
        (arr.length ? " · " + fmtNum(Math.min(...arr), 3) + "–" + fmtNum(Math.max(...arr), 3) + " ns" : "") + "</span></button>";
    }).join("");
}

async function paintClockChart() {
  const el = $("lx-clk-chart");
  const c = clockData();
  if (!el || !c) return;
  const arr = c.nets.flatMap((n) => n.sinks.filter((k) => k.ff && k.arrival != null).map((k) => k.arrival));
  if (!arr.length) { el.innerHTML = "<p class='muted'>No arrival times (the stage has no constraints).</p>"; return; }
  if (typeof window.echarts === "undefined") await import("./loading.js").then((m) => m.loadECharts()).catch(() => null);
  if (typeof window.echarts === "undefined" || !el.isConnected) { el.innerHTML = "<p class='muted'>The chart needs ECharts.</p>"; return; }
  const { chartTheme, chartInk } = await import("./theme-echarts.js");
  const lo = Math.min(...arr), hi = Math.max(...arr);
  const n = Math.max(1, Math.min(24, Math.round(Math.sqrt(arr.length) * 2)));
  const w = Math.max((hi - lo) / n, 0.001);
  const counts = new Array(n).fill(0);
  for (const a of arr) counts[Math.min(n - 1, Math.floor((a - lo) / w))] += 1;
  const k = chartInk();
  const chart = window.echarts.getInstanceByDom(el) || window.echarts.init(el, null, { renderer: "canvas" });
  chart.setOption({
    ...chartTheme(),
    animation: false,
    grid: { left: 36, right: 10, top: 10, bottom: 34 },
    tooltip: { ...chartTheme().tooltip, trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (ps) => { const i = ps[0].dataIndex; return (lo + i * w).toFixed(3) + "–" + (lo + (i + 1) * w).toFixed(3) + " ns: <b>" + counts[i] + "</b> flip-flops"; } },
    xAxis: { type: "category", data: counts.map((_c, i) => (lo + (i + 0.5) * w).toFixed(3)), name: "clock arrival (ns)", nameLocation: "middle", nameGap: 22,
      axisLabel: { color: k.text, fontSize: 10.5, interval: Math.max(0, Math.floor(n / 4) - 1) }, axisTick: { show: false } },
    yAxis: { type: "value", minInterval: 1, name: "flip-flops", nameTextStyle: { color: k.text, fontSize: 10.5 }, axisLabel: { color: k.text, fontSize: 10.5 },
      splitLine: { lineStyle: { color: k.grid } } },
    series: [{ type: "bar", data: counts, barCategoryGap: "12%", itemStyle: { color: CLOCK_COLOR } }],
  }, true);
  chart.resize();
}

function onFocusClick(e) {
  if (e.target.closest("[data-focus-back]")) { closeFocus(); if (S.ovl === "paths" && !S.path) { S.ovl = ""; paintOvlButton(); paintKey(); } return; }
  if (e.target.closest("[data-path-clear]")) { clearPath(); return; }
  if (e.target.closest("[data-path-final]")) {
    const final = (S.src.stages || []).find((s) => s.id === "final");
    if (final) openSource(final.source);
    return;
  }
  const pl = e.target.closest("[data-plist]");
  if (pl) { S.pathList = pl.dataset.plist; renderFocus(); return; }
  const pp = e.target.closest("[data-pick-path]");
  if (pp) {
    const d = S.an && S.an.data;
    const q = d && d.paths[S.pathList] && d.paths[S.pathList][+pp.dataset.pickPath];
    if (q) showPath({ ...pathFromExport(q, { type: S.pathList }), index: +pp.dataset.pickPath, list: S.pathList }, { from: "export" });
    return;
  }
  const row = e.target.closest("[data-path-row]");
  if (row) { activatePathRow(+row.dataset.pathRow, { zoom: true }); return; }
  const cn = e.target.closest("[data-clk-net]");
  if (cn) {
    const name = cn.dataset.clkNet;
    S.clockFocus = S.clockFocus === name ? "" : name;
    const n = clockData().nets.find((x) => x.name === name);
    if (n && S.clockFocus) {
      const boxes = [n.driver || n.port, ...n.sinks].filter(Boolean).map((k) => k.box);
      const b = boxes.reduce((a, x) => [Math.min(a[0], x[0]), Math.min(a[1], x[1]), Math.max(a[2], x[2]), Math.max(a[3], x[3])], [Infinity, Infinity, -Infinity, -Infinity]);
      S.canvas.zoomToBox(b, 0.15);
    }
    renderFocus();
    S.canvas.paintOverlay();
  }
}

function onFocusKey(e) {
  if (S.focus !== "path" || !S.path) return;
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  if (!e.target.closest || !e.target.closest(".lx-ptab")) return;
  e.preventDefault();
  const n = (S.path.placed || []).length;
  activatePathRow(Math.max(0, Math.min(n - 1, S.pathActive + (e.key === "ArrowDown" ? 1 : -1))), { zoom: true, focus: true });
}

function activatePathRow(i, { zoom = false, focus = false } = {}) {
  const p = S.path;
  if (!p) return;
  S.pathActive = i;
  const q = (p.placed || [])[i];
  p.activeInst = q && q.inst;
  document.querySelectorAll("#lx-focus [data-path-row]").forEach((r) => r.classList.toggle("is-on", +r.dataset.pathRow === i));
  const tr = document.querySelector("#lx-focus [data-path-row='" + i + "']");
  if (tr) { tr.scrollIntoView({ block: "nearest" }); if (focus) tr.focus({ preventScroll: true }); }
  if (zoom && q) {
    const c = q.inst && p.cells[q.inst];
    if (c && c.box) { const pad = Math.max(c.box[2] - c.box[0], c.box[3] - c.box[1], 2) * 3; S.canvas.zoomToBox([c.box[0] - pad, c.box[1] - pad, c.box[2] + pad, c.box[3] + pad], 0); }
    else if (q.x != null) S.canvas.centerOn(q.x, q.y);
  }
  S.canvas.paintOverlay();
}

// Draw one path: find its cells and nets on this stage, place its pins,
// open the path view.
async function showPath(hl, { from = "link" } = {}) {
  if (!S.info) return;
  // What was clicked before belongs to another question.
  S.sel = null; S.pin = null; S.flash = null;
  hideInspector();
  S.path = { ...hl, from, cells: {}, netGeom: {}, clockCells: {}, clockNetGeom: {},
    placed: placePoints(hl.points, {}, S.info.labels), missing: { insts: [], nets: [] } };
  S.pathActive = -1;
  S.focus = "path";
  renderFocus();
  await relocatePath({ zoom: true });
}

async function relocatePath({ zoom = false } = {}) {
  const p = S.path;
  if (!p || !S.info) return;
  const sid = S.info.sid;
  const ci = p.clockInsts || [], cn = p.clockNets || [];
  let r = { insts: {}, nets: {}, missing: { insts: p.insts, nets: p.nets }, names: false };
  if (S.info.names && (p.insts.length || p.nets.length || ci.length)) {
    try { r = await api.layoutLocate(sid, p.insts.concat(ci), p.kind === "cells" ? [] : p.nets.concat(cn)); }
    catch (ex) { toast.show("Couldn't find the path on the layout: " + (ex.message || ex), "warn"); }
  }
  if (S.path !== p || !S.info || S.info.sid !== sid) return;
  const pick = (obj, names) => Object.fromEntries(names.filter((n) => obj && obj[n]).map((n) => [n, obj[n]]));
  p.cells = pick(r.insts, p.insts);
  p.netGeom = pick(r.nets, p.nets);
  p.clockCells = pick(r.insts, ci);
  p.clockNetGeom = pick(r.nets, cn);
  const miss = r.missing || { insts: [], nets: [] };
  p.missing = { insts: (miss.insts || []).filter((n) => p.insts.includes(n)), nets: (miss.nets || []).filter((n) => p.nets.includes(n)) };
  p.placed = placePoints(p.points, p.cells, S.info.labels);
  if (!S.info.names) toast.show("This layout has no instance names, so only the pin positions of the path are drawn.", "info");
  if (zoom) {
    const pts = [...Object.values(p.cells).map((c) => c.box), ...p.placed.filter((q) => q.x != null).map((q) => [q.x, q.y, q.x, q.y])];
    if (pts.length) {
      const b = pts.reduce((a, x) => [Math.min(a[0], x[0]), Math.min(a[1], x[1]), Math.max(a[2], x[2]), Math.max(a[3], x[3])], [Infinity, Infinity, -Infinity, -Infinity]);
      paintKey();
      zoomClear(b, 0.12);
    }
  }
  if (S.focus === "path") renderFocus();
  paintKey();
  S.canvas.paintOverlay();
}

function clearPath() {
  S.path = null;
  S.pathActive = -1;
  if (S.focus === "path") renderFocus();
  if (S.focus === "path" && !(S.an && S.an.data)) closeFocus();
  paintKey();
  S.canvas && S.canvas.paintOverlay();
}

// ------------------------------------------------------------------ Reading a layout
// Five short steps that change the view as they go (coach.js). Offered from
// the key; leaving puts the stage, layers and camera back as they were.
function startReading() {
  if (!S.info || S.coach) return;
  if (S.mode3d) toggle3d(false);
  const stages = S.src.stages || [];
  const final = stages.find((s) => s.id === "final") || stages[stages.length - 1];
  const by = (id) => stages.find((s) => s.id === id) || final;
  const saved = { tag: S.tag, source: S.source, vis: new Set(S.vis), preset: S._preset, box: S.canvas.viewBox(), nets: S.nets.slice(), key: S._keyUser,
    markers: S.showMarkers, ovl: S.ovl };
  // The lesson shows the chip itself: check markers and overlays wait.
  S.showMarkers = false;
  if (S.ovl) pickOverlay("");
  const go = async (st, preset) => {
    if (st && S.source !== st.source) await openSource(st.source, { keepView: false });
    if (preset) applyPreset(preset);
  };
  const steps = [
    { title: "This is your chip, from above", body: "The outer edge is the die. Cells and wires sit in the core inside it. The names round the edge are your design's inputs and outputs.",
      enter: async () => { await go(final, "all"); S.canvas.fit(); } },
    { title: "Power comes first", body: "Before any logic is placed, the tools lay a power grid: wide straps on the top metals and thin rails along every row. Orange is the supply (" +
        (pdnOf().vdd || "VPWR") + "), blue is ground (" + (pdnOf().gnd || "VGND") + ").",
      enter: async () => { await go(by("pdn"), "power"); S.canvas.fit(); } },
    { title: "Cells in rows", body: "Each box is one standard cell, a ready-made gate from the PDK's library. They all have the same height so they fit in rows. Click any box to see what it is.",
      enter: async () => {
        await go(by("placement"), "all");
        const bb = S.info.bbox, cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2, r = Math.min(12, (bb[2] - bb[0]) / 4);
        S.canvas.zoomToBox([cx - r, cy - r, cx + r, cy + r], 0);
      } },
    { title: "Wires on metal layers", body: "Wires run on stacked metal layers joined by vias. Metal 1 makes short hops; higher layers carry longer wires. The key says which colour is which.",
      enter: async () => { await go(final, "wiring"); if (!keyOpen()) { S._keyUser = "1"; paintBadge(); } S.canvas.fit(); } },
    { title: "Inside one cell", body: "Zoomed into one flip-flop you see its transistors: wherever poly (the gate) crosses diffusion there is one.",
      hint: "Press / to find any pin, net or cell. The 3D view shows the same layers stacked.",
      done: "Done", extra: { label: "Show it in 3D", run: () => toggle3d(true) },
      enter: async () => {
        await go(final, "devices");
        const ff = (S.info.cells || []).find((c) => c.kind === "flipflop") || (S.info.cells || [])[0];
        if (!ff) return;
        try {
          const r = await api.layoutInstances(S.info.sid, ff.name);
          const b = r.boxes && r.boxes[Math.floor(r.boxes.length / 2)];
          if (b) S.canvas.zoomToBox(b, 0.12);
        } catch (_e) { /* the step still reads without the zoom */ }
      } },
  ];
  S.coach = startCoach({
    host: $("lx-view"), eyebrow: "Reading a layout", label: "Reading a layout", steps,
    onEnd: async () => {
      S.coach = null;
      if (S.tag !== saved.tag) return;      // another run was opened meanwhile
      S.nets = saved.nets;
      if (S.source !== saved.source) await openSource(saved.source);
      S.vis = saved.vis; S._preset = saved.preset;
      S._keyUser = saved.key;
      S.showMarkers = saved.markers;
      if (saved.ovl) pickOverlay(saved.ovl);
      renderLayers(); restyle(); paintBadge(); renderFind();
      S.canvas.zoomToBox(saved.box, 0);
    },
  });
}

// ------------------------------------------------------------------ 3D
function toggle3d(on) {
  const want = on ?? !S.mode3d;
  if (want && !S.info) return;
  if (want && !S.three) {
    S.three = new Layout3D($("lx-view"), {
      onKey: (k) => keyCmd(k),
      onSpin: (on) => $("lx-3d-spin")?.setAttribute("aria-pressed", on ? "true" : "false"),
      // The controls card covers the view's top right: frame the chip in
      // the space left of it.
      inset: () => { const c = $("lx-corner"); return c && !$("lx-3dctl").hidden ? c.offsetWidth + 24 : 0; },
    });
    if (!S.three.ok) {
      toast.show("This browser can't draw WebGL, so the 3D view isn't available. GDS3D (below) shows the stack in 3D.", "warn");
      S.three.destroy(); S.three = null;
      return;
    }
    S.three.setBg(BG[S.bg] || BG.dark);
    S.three.setScheme(pref("scheme", "lanex"));
    S.three.setVisible(S.vis);
    wire3dControls();
  }
  S.mode3d = want;
  $("lx-view").classList.toggle("is-3d", want);
  $("lx-3dctl").hidden = !want;
  document.querySelector("[data-lx='3d']")?.setAttribute("aria-pressed", want ? "true" : "false");
  document.querySelector("[data-lx='2d']")?.setAttribute("aria-pressed", want ? "false" : "true");
  $("lx-3d-main")?.setAttribute("aria-pressed", want ? "true" : "false");
  hideInspector();
  paintKey();
  paintStepper();
  if (want) {
    S.three.show(true);
    paint3dCtl();
    load3d();
    S.three.cv.focus({ preventScroll: true });
  } else {
    S.three && S.three.show(false);
    S.canvas.over.focus({ preventScroll: true });
    paintBadge();
  }
  renderLayers();   // the swatches follow the 3D colours while it's on
}

// The 3D card: colours, height, spread, turntable, reset.
function wire3dControls() {
  const z = $("lx-3d-z"), sp = $("lx-3d-s");
  z.addEventListener("input", () => { S.three.auto = false; S.three.setZScale(+z.value); paint3dCtl(); });
  sp.addEventListener("input", () => { S.three.auto = false; S.three.setSpread(+sp.value); paint3dCtl(); });
  $("lx-3d-scheme").addEventListener("click", (e) => {
    const b = e.target.closest("[data-scheme]");
    if (!b) return;
    setPref("scheme", b.dataset.scheme);
    S.three.setScheme(b.dataset.scheme);
    paint3dCtl();
    renderLayers();
  });
  $("lx-3d-spin").addEventListener("click", () => {
    S.three.setSpin(!S.three.spin);
    if (S.three.spin) S.three.cv.focus({ preventScroll: true });
  });
  $("lx-3d-reset").addEventListener("click", () => {
    S.three.auto = true;
    const d = S.three.defaults();
    S.three.zScale = d.z; S.three.spread = d.spread;
    paint3dCtl();
    S.three.reset(true, { animate: true });
    S.three.cv.focus({ preventScroll: true });
  });
}

function paint3dCtl() {
  const t = S.three;
  if (!t) return;
  const z = $("lx-3d-z"), sp = $("lx-3d-s");
  const d = t.defaults();
  z.max = String(Math.max(d.zMax, t.zScale));
  z.value = String(t.zScale);
  sp.value = String(t.spread);
  $("lx-3d-zv").textContent = fmtHeight(t.zScale) + (t.zScale === 1 ? " true scale" : "");
  $("lx-3d-sv").textContent = t.spread === 0 ? "0% real stack" : fmtSpread(t.spread);
  // The filled part of each track.
  for (const el of [z, sp]) el.style.setProperty("--p", ((+el.value - +el.min) / (+el.max - +el.min) * 100).toFixed(1) + "%");
  document.querySelectorAll("#lx-3d-scheme [data-scheme]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.scheme === t.scheme ? "true" : "false"));
  $("lx-3d-spin").setAttribute("aria-pressed", t.spin ? "true" : "false");
}

async function load3d() {
  if (!S.three || !S.info) return;
  const bb = S.info.bbox;
  const vb = S.canvas.viewBox();
  const zoomed = (vb[2] - vb[0]) < (bb[2] - bb[0]) * 0.9 || (vb[3] - vb[1]) < (bb[3] - bb[1]) * 0.9;
  const box = zoomed ? vb.map((v) => +v.toFixed(4)) : null;
  const key = S.info.sid + "|" + (box ? box.join(",") : "all");
  if (key === S.meshKey && (S.three.layers.length || S._meshBusy === key)) { S.three.show(true); paint3dBadge(); return; }
  S.meshKey = key;
  S._meshBusy = key;
  setBusy(true);
  hideMsg();
  // A big chip takes a few seconds to build: say so where the eye is.
  const badge = $("lx-badge");
  if (badge) {
    const st = (S.src.stages || []).find((s) => s.source === S.source);
    badge.innerHTML = "<b>" + esc(st ? st.label : "Snapshot") + "<i>3D</i></b><em class='lx-badge-wait'>Building the 3D model of " + (box ? "this area" : "the whole chip") + "…</em>" +
      "<button class='lx-badge-act' data-mesh-cancel>Cancel</button>";
    badge.querySelector("[data-mesh-cancel]")?.addEventListener("click", () => {
      api.layoutMeshCancel(S.info.sid).catch(() => {});
    }, { once: true });
  }
  try {
    const r = await fetch("/api/layout/mesh?sid=" + encodeURIComponent(S.info.sid) + (box ? "&box=" + box.join(",") : ""));
    if (r.status === 409) {
      // Cancelled on purpose: back to the flat view, no error.
      S.meshKey = "";
      toast.show("Stopped building the 3D model.", "info");
      toggle3d(false);
      return;
    }
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).error || msg; } catch (_e) {}
      S.meshKey = "";
      paintBadge();
      showMsg("<h3>3D isn't drawn for this view</h3><p>" + esc(msg) + "</p><button class='btn btn-ghost btn-sm' data-back2d>Back to 2D</button>");
      $("lx-msg").querySelector("[data-back2d]")?.addEventListener("click", () => { hideMsg(); toggle3d(false); });
      return;
    }
    const buf = await r.arrayBuffer();
    if (key !== S.meshKey) return;
    // A new chip rises out of the flat picture into the 3D view.
    const head = S.three.load(buf, { animate: true });
    S.three.setVisible(S.vis);
    S._3dhead = head;
    paint3dBadge();
    paint3dCtl();
    renderLayers();
    if (!head.layers.length) showMsg("<h3>No layers to stack</h3><p>None of this view's layers are in the PDK's 3D stack.</p>");
  } catch (ex) {
    S.meshKey = "";
    paintBadge();
    toast.show("3D failed: " + (ex.message || ex), "error");
  } finally {
    if (S._meshBusy === key) S._meshBusy = "";
    setBusy(false);
  }
}

function paint3dBadge() {
  const el = $("lx-badge");
  const h = S._3dhead;
  if (!el || !h) return;
  const w = h.extent[2] - h.extent[0], hh = h.extent[3] - h.extent[1];
  // "gds3d:<file>" (a GDS3D process file, LanEx's own or GDS3D's), "d25:<file>"
  // (the PDK's KLayout 2.5D stack) or "generic".
  const real = h.stack && h.stack !== "generic";
  const file = real ? h.stack.slice(h.stack.indexOf(":") + 1) : "";
  const st = (S.src.stages || []).find((s) => s.source === S.source);
  const left = (h.dropped || []).length;
  el.innerHTML = "<b>" + esc(st ? st.label : "Snapshot") + "<i>3D · " + (h.clipped ? "this area" : "whole chip") + "</i></b>" +
    "<span>" + esc(h.top || S.info.top) + " · " + fmtUm(w) + " × " + fmtUm(hh) + "</span>" +
    "<em>" + (real ? "Real layer stack · " + esc(file.replace(/\.(txt|lyd25)$/i, "")) : "Generic heights: no stack file for this PDK") + "</em>" +
    (left ? "<em class='lx-badge-warn'>" + left + (left === 1 ? " lower layer" : " lower layers") + " left out: too detailed for the whole chip. Zoom in to see " + (left === 1 ? "it" : "them") + ".</em>" : "");
  el.title = (real ? "Layer heights and order from " + (h.stack.startsWith("d25:") ? "the PDK's 2.5D stack " : "the process file ") + file + "." :
    "No layer-stack file for this PDK was found, so layers are evenly spaced in stacking order.") +
    " Height and spread change only how the stack is drawn." + (left ? " Left out: " + h.dropped.join(", ") + "." : "");
}

// ------------------------------------------------------------------ toolbar
function toggleMeasure(on) {
  const m = on ?? S.canvas.mode !== "measure";
  S.canvas.setMode(m ? "measure" : "pan");
  const b = document.querySelector("[data-lx='measure']");
  if (b) b.setAttribute("aria-pressed", m ? "true" : "false");
  if (m) { hideInspector(); flashHint("Drag to measure. It snaps to straight and 45° lines; hold Alt for any angle. Esc clears."); }
}
function toggleLabels() {
  S.labels = !S.labels;
  setPref("labels", S.labels ? "1" : "0");
  document.querySelector("[data-lx='labels']")?.setAttribute("aria-pressed", S.labels ? "true" : "false");
  restyle();
  S.canvas && S.canvas.paintOverlay();
}
function toggleOutlines() {
  S.outlines = !S.outlines;
  document.querySelector("[data-lx='detail']")?.setAttribute("aria-pressed", S.outlines ? "true" : "false");
  restyle();
}
function toggleBg() {
  S.bg = S.bg === "dark" ? "light" : "dark";
  setPref("bg", S.bg);
  $("lx-view").classList.toggle("is-light", S.bg === "light");
  restyle();
  if (S.three) S.three.setBg(BG[S.bg] || BG.dark);
  renderStages();
}
function toggleSide() {
  const ws = $("lx-ws");
  const off = !ws.classList.contains("side-off");
  ws.classList.toggle("side-off", off);
  setPref("side", off ? "0" : "1");
  document.querySelector("[data-lx='side']")?.setAttribute("aria-pressed", off ? "false" : "true");
}
function toggleFull() {
  const ws = $("lx-ws");
  if (document.fullscreenElement) document.exitFullscreen();
  else ws.requestFullscreen?.().catch(() => ws.classList.toggle("is-max"));
}

function flashHint(text) {
  const el = $("lx-hint");
  if (!el) return;
  el.textContent = text;
  el.classList.add("is-on");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("is-on"), 4200);
}

function onToolbar(e) {
  const b = e.target.closest("[data-lx]");
  if (!b || !S.canvas) { if (b && b.dataset.lx === "side") toggleSide(); return; }
  const a = b.dataset.lx;
  if (S.mode3d && S.three && ["fit", "zin", "zout"].includes(a)) {
    if (a === "fit") S.three.reset();
    else { S.three.cam.dist *= a === "zin" ? 1 / 1.4 : 1.4; S.three.draw(); }
    S.three.cv.focus({ preventScroll: true });
    return;
  }
  if (a === "fit") S.canvas.fit();
  else if (a === "zin") S.canvas.zoom(1.5);
  else if (a === "zout") S.canvas.zoom(1 / 1.5);
  else if (a === "measure") toggleMeasure();
  else if (a === "labels") toggleLabels();
  else if (a === "detail") toggleOutlines();
  else if (a === "bg") toggleBg();
  else if (a === "side") toggleSide();
  else if (a === "full") toggleFull();
  else if (a === "3d" || a === "2d") { if ((a === "3d") !== !!S.mode3d) toggle3d(a === "3d"); return; }
  (S.mode3d && S.three ? S.three.cv : S.canvas.over).focus({ preventScroll: true });
}

// ------------------------------------------------------------------ desktop viewers
const DESK = [
  { key: "klayout", name: "KLayout", for: "Inspect and measure", what: "The standard GDS viewer: every layer, rulers, cross-sections, and the DRC marker browser with this run's reports loaded." },
  { key: "openroad", name: "OpenROAD GUI", for: "Timing and congestion", what: "The place-and-route database: critical timing paths drawn on the chip, placement and routing heat maps, the clock tree." },
  { key: "magic", name: "Magic", for: "Edit transistors", what: "Edit the layout at transistor level with design rules checked as you draw, and extract a SPICE netlist." },
  { key: "gds3d", name: "GDS3D", for: "See it in 3D", what: "Fly through the real layer stack in 3D: metals, vias and devices at their true heights." },
];

let _routes = null;
async function renderDesk() {
  const host = $("lx-desk");
  if (!host) return;
  try { _routes = await toolRoutes(); } catch (_e) { _routes = {}; }
  const last = pref("tool", "klayout");
  host.innerHTML = DESK.map((t) => {
    const r = _routes[t.key] || { where: "install" };
    const status = { container: "Ready · runs in the LanEx container", host: "Ready · installed on this machine",
      nodisplay: "No display for windows", install: "Not installed" }[r.where];
    const ok = r.where === "container" || r.where === "host";
    return "<div class='lx-dcard" + (ok ? "" : " is-off") + (t.key === last ? " is-last" : "") + "'>" +
      "<div class='lx-dcard-top'><b>" + t.name + "</b><span class='lx-dfor'>" + t.for + "</span></div>" +
      "<p>" + t.what + "</p>" +
      "<div class='lx-dcard-foot'><span class='lx-dstat lt-" + r.where + "' title='" + esc(r.title || "") + "'>" + status + "</span>" +
      "<button class='btn btn-sm " + (ok ? "btn-ghost" : "btn-ghost") + "' data-desk='" + t.key + "'>" +
      (r.where === "install" ? "Set up in Tools" : "Open") + "</button></div></div>";
  }).join("");
  paintOpenMain();
}

function paintOpenMain() {
  const last = pref("tool", "klayout");
  const t = DESK.find((d) => d.key === last) || DESK[0];
  const lbl = $("lx-open-label");
  if (lbl) lbl.textContent = "Open in " + t.name;
  const main = $("lx-open-main");
  if (main && S.info) main.title = "Open " + openWhat(t.key) + " in " + t.name;
  const menu = $("lx-open-menu");
  if (menu) menu.innerHTML = DESK.map((d) => {
    const r = (_routes || {})[d.key] || { where: "install" };
    return "<button role='menuitem' data-open='" + d.key + "'>" + d.name + "<span>" + d.for +
      (r.where === "install" ? " · not installed" : r.where === "nodisplay" ? " · no display" : " · opens " + esc(openWhat(d.key))) + "</span></button>";
  }).join("");
}

// The viewers open the stage on screen: OpenROAD its database, KLayout and
// Magic its DEF (through a startup script that loads the LEFs first) or the
// GDS file shown. GDS3D is GDS-only, so it always gets the final GDS.
function stagePath(tool, { quiet = false } = {}) {
  if (!S.source || S.source === "final" || !S.src) return undefined;
  if (S.source.startsWith("final:")) {
    const v = (S.src.variants || []).find((s) => s.source === S.source);
    return tool === "klayout" || tool === "magic" ? (v && v.path) : undefined;
  }
  const st = [...(S.src.stages || []), ...(S.src.steps || [])].find((s) => s.source === S.source);
  if (!st) return undefined;
  if (tool === "openroad") {
    if (!st.odb) {
      if (!quiet) toast.show("This step's OpenROAD database wasn't kept on disk, so OpenROAD opens the final one.", "info");
      return undefined;
    }
    return st.odb;
  }
  if (tool === "klayout" || tool === "magic") return st.path;
  return undefined;
}

// What "Open in" opens, in words (the menu and the button's tooltip).
function openWhat(tool) {
  const p = stagePath(tool, { quiet: true });
  const st = S.src && (S.src.stages || []).find((s) => s.source === S.source);
  const name = st ? st.label : S.source;
  if (!p) return tool === "gds3d" || !S.source || S.source === "final" ? "the final GDS" : tool === "openroad" ? "the final database" : "the final GDS";
  if (/\.def$/i.test(p)) return "the " + name + " stage (DEF, cells as outlines)";
  if (/\.odb$/i.test(p)) return "the " + name + " stage's database";
  return p.split("/").pop();
}

async function openTool(tool) {
  setPref("tool", tool);
  paintOpenMain();
  document.querySelectorAll(".lx-dcard").forEach((c) => c.classList.toggle("is-last", c.querySelector("[data-desk]")?.dataset.desk === tool));
  if (!S.tag) return;
  await launchTool(tool, S.tag, { path: stagePath(tool) });
}

// ------------------------------------------------------------------ menus
function wireMenu(btnId, menuId, onPick, onOpen) {
  const btn = $(btnId), menu = $(menuId);
  if (!btn || !menu) return;
  // A menu on the overlay stack (overlay.js): Esc, arrows, an outside press
  // and focus return are the same as every menu in the app.
  const hide = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!menu.hidden) { closeLayer(menu); return; }
    if (onOpen) await onOpen(menu);
    menu.hidden = false;
    openLayer(menu, { kind: "menu", owner: btn, onClose: hide });
  });
  menu.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { closeLayer(menu); onPick(b); } });
}

async function fillCommands(menu) {
  if (!S.commands && S.tag) {
    try { S.commands = await api.layoutCommands(S.tag); } catch (_e) { S.commands = {}; }
  }
  const c = S.commands || {};
  const rows = [
    ["klayout_flow", "LibreLane: open in KLayout", "Run from " + (c.cwd || "the design folder")],
    ["openroad_flow", "LibreLane: open in OpenROAD", "Same, with the OpenROAD GUI"],
    ["klayout", "KLayout directly", "The final GDS with the PDK's technology and layer colours"],
  ].filter(([k]) => c[k]);
  menu.innerHTML = rows.length
    ? rows.map(([k, t, s]) => "<button role='menuitem' data-copy='" + esc(c[k]) + "'>" + esc(t) + "<span>" + esc(s) + "</span><code>" + esc(c[k]) + "</code></button>").join("")
    : "<p class='muted lx-pad'>No commands for this run.</p>";
}

async function copy(text) { return copyText(text, "Command copied"); }

function saveImage(kind) {
  if (!S.info || !S.canvas) return;
  if (S.mode3d && S.three && kind === "view") {
    const a = document.createElement("a");
    a.href = S.three.snapshot();
    a.download = (S.info.top || "layout") + "-3d.png";
    document.body.appendChild(a); a.click(); a.remove();
    return;
  }
  const st = (S.src.stages || []).find((s) => s.source === S.source);
  const base = (S.info.top || "layout") + "-" + (st ? st.id : "view");
  let url, box, w, h, cssW;
  if (kind === "view") {
    const dpr = S.canvas.dpr;
    box = S.canvas.viewBox();
    w = Math.round(S.canvas.W * dpr); h = Math.round(S.canvas.H * dpr);
    cssW = S.canvas.W;
    url = renderUrl(box, w, h, 1 / dpr);
  } else {
    const long = kind === "chip8k" ? 7680 : 3840;
    const bb = S.info.bbox, bw = bb[2] - bb[0], bh = bb[3] - bb[1];
    const pad = Math.max(bw, bh) * 0.03;
    box = [bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad];
    const ar = (box[2] - box[0]) / (box[3] - box[1]);
    w = ar >= 1 ? long : Math.round(long * ar); h = ar >= 1 ? Math.round(long / ar) : long;
    // The overlays are drawn as if on a 1600 px wide screen, then scaled.
    cssW = 1600 * (w / long);
    // res < 1 scales line widths and stipples up, so the picture reads like the
    // screen view instead of hairlines on a huge canvas.
    url = renderUrl(box, w, h, kind === "chip8k" ? 0.25 : 0.5) + (kind === "chip4k" ? "&os=2" : "") + "&heavy=1";
  }
  const name = base + "-" + (kind === "view" ? "view" : kind === "chip8k" ? "8k" : "4k") + ".png";
  toast.show("Saving the picture, with what's drawn on it. Big ones take a few seconds.", "info");
  composeImage(url, box, w, h, cssW).then((blob) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);
  }).catch((ex) => toast.show("Couldn't save the picture: " + (ex.message || ex), "error"));
}

// The saved picture: KLayout's render of the box, then exactly what the
// canvas draws on top (heat map, highlights, path, markers), then the key
// and the scale bar in the corner.
async function composeImage(url, box, w, h, cssW) {
  const r = await fetch(url);
  if (!r.ok) {
    let msg = "HTTP " + r.status;
    try { msg = (await r.json()).error || msg; } catch (_e) {}
    throw new Error(msg);
  }
  const bm = await createImageBitmap(await r.blob());
  const cv = document.createElement("canvas");
  cv.width = w; cv.height = h;
  const c = cv.getContext("2d");
  c.drawImage(bm, 0, 0, w, h);
  const k = w / cssW;
  const W = cssW, H = h / k;
  const sx = W / (box[2] - box[0]);
  const v = {
    W, H, box, scale: 1 / sx,
    w2s: (x, y) => [(x - box[0]) * sx, H - (y - box[1]) * sx],
    s2w: (a, b) => [box[0] + a / sx, box[1] + (H - b) / sx],
  };
  c.setTransform(k, 0, 0, k, 0, 0);
  drawOverlay(c, v);
  drawImageKey(c, v);
  const blob = await new Promise((res, rej) => cv.toBlob((b) => (b ? res(b) : rej(new Error("the browser could not encode the picture"))), "image/png"));
  return blob;
}

// The key and scale bar drawn into a saved picture (bottom left).
function drawImageKey(c, v) {
  const light = S.bg === "light";
  const rows = keyRows({ layers: S.info.layers, vis: S.vis, stage: stageId(), kind: S.info.kind, pdn: pdnOf() }).slice(0, 7);
  const st = (S.src.stages || []).find((s) => s.source === S.source);
  const m = heatMap();
  const lines = rows.length + (m ? 2 : 0) + 2;
  const bw = 230, lh = 17, bh = 16 + lines * lh;
  const x0 = 12, y0 = v.H - bh - 12;
  c.save();
  c.fillStyle = light ? "rgba(255,255,255,0.88)" : "rgba(11,17,27,0.86)";
  c.strokeStyle = light ? "rgba(60,80,110,0.3)" : "rgba(126,150,186,0.3)";
  c.lineWidth = 1;
  c.fillRect(x0, y0, bw, bh);
  c.strokeRect(x0 + 0.5, y0 + 0.5, bw - 1, bh - 1);
  const ink = light ? "#0a1320" : "#f2f7ff", mute = light ? "#5f6d80" : "#8494ab";
  const mono = (_ioFont || "500 11px monospace").replace(/^500 11px /, "");
  c.textBaseline = "middle";
  let y = y0 + 14;
  c.font = "600 11px " + mono;
  c.fillStyle = ink;
  c.fillText(((st ? st.label : "Layout") + " · " + S.info.top).toUpperCase(), x0 + 10, y);
  y += lh;
  c.font = "12px " + mono;
  for (const rw of rows) {
    c.fillStyle = rw.sw.fill; c.fillRect(x0 + 10, y - 5, 14, 10);
    c.strokeStyle = rw.sw.frame || rw.sw.fill; c.strokeRect(x0 + 10.5, y - 4.5, 13, 9);
    c.fillStyle = ink; c.fillText(rw.label, x0 + 32, y);
    c.fillStyle = mute; c.fillText(rw.names.slice(0, 2).join(" "), x0 + 140, y);
    y += lh;
  }
  if (m) {
    c.fillStyle = ink; c.fillText(OVL[S.ovl].label + (S.ovlLayer ? " · " + S.ovlLayer : ""), x0 + 10, y);
    y += lh;
    const g = c.createLinearGradient(x0 + 10, 0, x0 + 170, 0);
    for (let i = 0; i <= 10; i++) g.addColorStop(i / 10, rampCssOf(i / 10, light));
    c.fillStyle = g; c.fillRect(x0 + 10, y - 5, 160, 10);
    c.fillStyle = mute; c.fillText("0–" + fmtNum(scaleTop(m)) + " " + (m.unit || ""), x0 + 176, y);
    y += lh;
  }
  // scale bar
  const len = niceLength(v.scale * 110);
  const px = len / v.scale;
  c.strokeStyle = ink; c.lineWidth = 1.5;
  c.beginPath(); c.moveTo(x0 + 10, y - 4); c.lineTo(x0 + 10, y + 2); c.lineTo(x0 + 10 + px, y + 2); c.lineTo(x0 + 10 + px, y - 4); c.stroke();
  c.fillStyle = ink; c.fillText(fmtUm(len), x0 + 18 + px, y);
  c.restore();
}

function rampCssOf(t, light) { return rampCss(rampName(), t, light); }

function showPane(name) {
  if (S.focus) closeFocus({ quiet: true });
  document.querySelectorAll("#lx-side [data-lx-pane]").forEach((t) => {
    const on = t.dataset.lxPane === name;
    t.classList.toggle("is-on", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll("#lx-side .lx-pane").forEach((p) => { p.hidden = p.id !== "lx-pane-" + name; });
  setPref("pane", name);
}

// ------------------------------------------------------------------ wiring
function wireOnce() {
  if (S.wired) return;
  S.wired = true;
  mountRunBar($("layout-runbar"));
  ctx.onContext((d) => { if ($("sec-layout")?.classList.contains("section-active") && d.why !== "loaded" && d.why !== "settled") loadRuns(); });
  document.querySelector("#sec-layout .lx-tools")?.addEventListener("click", onToolbar);
  for (const [id, b] of [["labels", S.labels], ["detail", S.outlines]]) document.querySelector("[data-lx='" + id + "']")?.setAttribute("aria-pressed", b ? "true" : "false");
  if (pref("side", "1") === "0") { $("lx-ws").classList.add("side-off"); document.querySelector("[data-lx='side']")?.setAttribute("aria-pressed", "false"); }
  // side tabs
  const tabs = document.querySelectorAll("#lx-side [data-lx-pane]");
  tabs.forEach((t) => t.addEventListener("click", () => { closeFocus({ quiet: true }); showPane(t.dataset.lxPane); }));
  showPane(pref("pane", "layers"));
  $("lx-pane-layers").addEventListener("click", onLayersClick);
  $("lx-pane-layers").addEventListener("change", onLayersChange);
  $("lx-pane-cells").addEventListener("click", (e) => {
    if (e.target.closest("[data-hl-clear]")) { S.hl = null; renderCells(); S.canvas && S.canvas.paintOverlay(); return; }
    const b = e.target.closest("[data-cell]");
    if (b) { clearSelection(); highlightCell(b.dataset.cell, { zoom: "ifHidden" }); }
  });
  $("lx-pane-cells").addEventListener("input", (e) => { if (e.target.id === "lx-cell-q") renderCells(e.target.value); });
  $("lx-find-q").addEventListener("input", onFindInput);
  $("lx-find-q").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const first = $("lx-find-res").querySelector(".lx-res");
    if (first) { e.preventDefault(); first.click(); }
  });
  $("lx-find-res").addEventListener("click", onFindClick);
  $("lx-netchips")?.addEventListener("click", onFindClick);
  $("lx-pane-checks").addEventListener("click", onChecksClick);
  $("lx-pane-checks").addEventListener("change", onChecksChange);
  $("lx-inspect").addEventListener("click", onInspectClick);
  // the key, the overlays, the focus panel, the marker stepper
  $("lx-badge").addEventListener("click", onKeyClick);
  $("lx-key").addEventListener("click", onKeyClick);
  $("lx-key").addEventListener("input", onKeyInput);
  $("lx-key").addEventListener("change", onKeyInput);
  $("lx-focus").addEventListener("click", onFocusClick);
  $("lx-focus").addEventListener("keydown", onFocusKey);
  $("lx-focus").addEventListener("pointerover", (e) => {
    const row = e.target.closest("[data-path-row]");
    if (!row || !S.path) return;
    const q = (S.path.placed || [])[+row.dataset.pathRow];
    if (q && S.path.activeInst !== q.inst) { S.path.activeInst = q.inst; S.canvas && S.canvas.paintOverlay(); }
  });
  $("lx-stepper").addEventListener("click", (e) => {
    const b = e.target.closest("[data-step]");
    if (!b) return;
    if (b.dataset.step === "x") clearSelection();
    else stepMarkers(+b.dataset.step);
  });
  wireMenu("lx-ovl", "lx-ovl-menu", (b) => { if (b.matches("[data-ovl-pick]") && !b.disabled) pickOverlay(b.dataset.ovlPick); }, () => fillOvlMenu());
  document.addEventListener("g:pref", (e) => {
    const id = e.detail && e.detail.id;
    if (id === "layoutRamp") { S.heat = null; paintKey(); S.canvas && S.canvas.paintOverlay(); }
    if (id === "layoutKey" && !S._keySelf) { S._keyUser = null; paintBadge(); }
  });
  const mini = $("lx-mini");
  let dragging = false;
  mini.addEventListener("pointerdown", (e) => { dragging = true; mini.setPointerCapture(e.pointerId); miniJump(e); });
  mini.addEventListener("pointermove", (e) => { if (dragging) miniJump(e); });
  mini.addEventListener("pointerup", () => { dragging = false; });
  // The run bar's 3D view: the same switch as the toolbar's 2D/3D.
  $("lx-3d-main").addEventListener("click", () => {
    if (!S.info) { toast.show("There's no layout on screen yet to show in 3D.", "info"); return; }
    toggle3d();
  });
  // desktop viewers
  $("lx-desk").addEventListener("click", (e) => { const b = e.target.closest("[data-desk]"); if (b) openTool(b.dataset.desk); });
  $("lx-open-main").addEventListener("click", () => openTool(pref("tool", "klayout")));
  wireMenu("lx-open-pick", "lx-open-menu", (b) => openTool(b.dataset.open), () => paintOpenMain());
  wireMenu("lx-save", "lx-save-menu", (b) => saveImage(b.dataset.save));
  wireMenu("lx-cmd", "lx-cmd-menu", (b) => { if (b.dataset.copy) copy(b.dataset.copy); }, fillCommands);
  document.addEventListener("fullscreenchange", () => $("lx-ws").classList.toggle("is-full", document.fullscreenElement === $("lx-ws")));
}

// Tests and the console reach the page state through this.
export function _layoutState() { return S; }
