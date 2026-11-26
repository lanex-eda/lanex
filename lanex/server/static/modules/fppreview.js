// fppreview.js — the floorplan preview: the die and the core drawn from the
// size settings, before a run, with what would stop the run at the floorplan
// or the power grid.
//
// The arithmetic is OpenROAD's (LibreLane 3.0.4, scripts/openroad/floorplan.tcl
// and ifp/InitFloorplan.cc), with LibreLane's own numbers from the server
// (controller/fpestimate.py): the site, the margins, the power straps, the pin
// tracks and the cell area of the project's latest synthesis. Nothing here is a
// silicon number: relative sizing needs that synthesis area, and the preview
// names the run it came from and says it is an estimate.
//
//   computeFloorplan(values, inputs)  pure: die, core, rows, checks (tested)
//   floorplanSvg(fp)                  pure: the drawing (tested)
//   mountFloorplan(el, {values, inputs, compact})  draws it into el

import { escapeHtml } from "./html.js";

const esc = escapeHtml;
const num = (v) => { const n = typeof v === "number" ? v : parseFloat(v); return Number.isFinite(n) ? n : null; };
const GRID = 0.005;   // the manufacturing grid LibreLane snaps the die to (sky130, gf180, IHP)
const snap = (v) => Math.floor(v / GRID + 1e-6) * GRID;
const um = (v) => (v == null ? "—" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 1 : 2) + " mm" : (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2)).replace(/\.?0+$/, "") + " µm");
const n1 = (v) => (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2)).replace(/\.?0+$/, "");

function area(v) {
  const a = Array.isArray(v) ? v.map(num) : String(v || "").trim().split(/[\s,]+/).filter(Boolean).map(num);
  return a.length === 4 && a.every((x) => x != null) && a[2] > a[0] && a[3] > a[1] ? a : null;
}

// The die, core and rows the settings give, and the checks. `v`: the size
// settings as the page shows them ({FP_SIZING, FP_CORE_UTIL, DIE_AREA,
// CORE_AREA}); `k`: the server's inputs (fpestimate.estimate_inputs).
export function computeFloorplan(v, k) {
  const out = { mode: v.FP_SIZING === "absolute" ? "absolute" : "relative", die: null, core: null, rows: 0,
    checks: [], estimate: false, state: "ok" };
  const site = k && k.site;
  if (!site) { out.state = "no-pdk"; return out; }
  const m = (k.margins || {});
  const L = (num(m.LEFT_MARGIN_MULT) ?? 12) * site.w, R = (num(m.RIGHT_MARGIN_MULT) ?? 12) * site.w;
  const B = (num(m.BOTTOM_MARGIN_MULT) ?? 4) * site.h, T = (num(m.TOP_MARGIN_MULT) ?? 4) * site.h;
  out.margins = { L, R, B, T };
  const syn = k.synth || null;
  if (out.mode === "absolute") {
    const d = area(v.DIE_AREA);
    if (!d) { out.state = "no-area"; return out; }
    out.die = d;
    const ca = area(v.CORE_AREA);
    out.core = ca || [d[0] + L, d[1] + B, d[2] - R, d[3] - T];
    if (ca && (ca[0] < d[0] || ca[1] < d[1] || ca[2] > d[2] || ca[3] > d[3])) {
      out.checks.push({ level: "fail", code: "IFP-0055", text: "CORE_AREA reaches outside the die. LibreLane stops at the floorplan." });
    }
  } else {
    const U = num(v.FP_CORE_UTIL) ?? 50;
    if (!syn || !syn.area) { out.state = "needs-synthesis"; return out; }
    if (!(U > 0)) { out.state = "bad-util"; return out; }
    const AR = num(k.aspect) || 1;
    const cw = snap(Math.sqrt(syn.area / (U / 100) / AR));
    const ch = snap(cw * AR);
    out.die = [0, 0, snap(L + cw + R), snap(B + ch + T)];
    out.core = [L, B, L + cw, B + ch];
    out.estimate = true;
  }
  const [cx0, cy0, cx1, cy1] = out.core;
  const cw = cx1 - cx0, ch = cy1 - cy0;
  const dw = out.die[2] - out.die[0], dh = out.die[3] - out.die[1];
  if (cw <= 0 || ch <= 0) {
    out.checks.push({ level: "fail", code: "STA-0572", text: "The margins (" + n1(L) + " + " + n1(R) + " µm across, " + n1(B) + " + " + n1(T) +
      " µm up) leave no core in a " + n1(dw) + " × " + n1(dh) + " µm die. LibreLane stops at the floorplan." });
    out.state = "no-core";
    return out;
  }
  out.rows = Math.floor(ch / site.h + 1e-6);
  // The power straps: offset + two straps + the gap between them, across the
  // core (vertical straps) and up it (horizontal ones, unless PDN_MULTILAYER
  // is off). pdn/straps.cpp, PDN-0185.
  const p = k.pdn || {};
  const vNeed = (num(p.PDN_VOFFSET) ?? 0) + 2 * (num(p.PDN_VWIDTH) ?? 0) + (num(p.PDN_VSPACING) ?? 0);
  const hNeed = (num(p.PDN_HOFFSET) ?? 0) + 2 * (num(p.PDN_HWIDTH) ?? 0) + (num(p.PDN_HSPACING) ?? 0);
  if (vNeed > 0 && cw < vNeed) {
    out.checks.push({ level: "fail", code: "PDN-0185", text: "The power grid needs " + n1(vNeed) + " µm of core width for its straps; this core has " + n1(cw) + ". LibreLane stops at the power grid." });
  } else if (hNeed > 0 && p.PDN_MULTILAYER !== false && ch < hNeed) {
    out.checks.push({ level: "fail", code: "PDN-0185", text: "The power grid needs " + n1(hNeed) + " µm of core height for its straps; this core has " + n1(ch) + ". LibreLane stops at the power grid." });
  } else if (vNeed > 0) {
    out.checks.push({ level: "ok", text: "The power straps fit." });
  }
  // The pins: one every other track along each edge (OpenROAD's default
  // spacing), pins on the vertical layer along the top and bottom edges.
  const io = k.io || {};
  const pv = num(io.v_pitch), ph = num(io.h_pitch);
  if (k.pins && pv && ph) {
    const room = 2 * Math.floor(dw / (2 * pv)) + 2 * Math.floor(dh / (2 * ph));
    out.pinRoom = room;
    const ratio = k.pins / room;
    if (ratio > 1) out.checks.push({ level: "fail", code: "PPL-0024", text: k.pins + " pins, and this die's edge has room for about " + room + ". LibreLane stops at pin placement." });
    else if (ratio > 0.9) out.checks.push({ level: "warn", code: "PPL-0024", text: k.pins + " pins, room for about " + room + ": they may not fit." });
    else out.checks.push({ level: "ok", text: k.pins + " pins, room for about " + room + " on the edge." });
  }
  if (out.mode === "absolute" && syn && syn.area) {
    const u = syn.area / (cw * ch) * 100;
    out.util = u;
    out.checks.push(u >= 100
      ? { level: "fail", code: "GPL-0301", text: "The cells from " + syn.tag + "'s synthesis need " + u.toFixed(0) + " % of this core: they don't fit." }
      : { level: u > 80 ? "warn" : "ok", text: "The cells from " + syn.tag + "'s synthesis fill about " + u.toFixed(0) + " % of this core" +
          (u > 80 ? ", which leaves little room for the buffers and the clock tree." : ".") });
  }
  if (out.checks.some((c) => c.level === "fail")) out.state = "problem";
  return out;
}

// The words under the drawing: where the size came from. Pure (tested).
export function sourceLine(fp, k) {
  const syn = k && k.synth;
  if (fp.state === "needs-synthesis") {
    return "LibreLane sizes this die from the design's cell area, which the first synthesis gives. Run up to Synthesis (a few seconds) to see it.";
  }
  if (!fp.estimate || !syn) return fp.mode === "absolute" ? "A fixed die (DIE_AREA); the core is the die less its margins." : "";
  const grew = syn.placed_area && syn.placed_area > syn.area
    ? " In " + syn.tag + " the cells grew to " + Math.round(syn.placed_area).toLocaleString("en-US") + " µm² once placed." : "";
  return "An estimate, sized from " + syn.tag + "'s synthesis (" + Math.round(syn.area).toLocaleString("en-US") + " µm² of cells). " +
    "Placement adds buffers, tap cells and the clock tree." + grew + (syn.stale ? " The RTL has changed since that run, so the size will move with it." : "");
}

// The drawing: die and core to scale, a few row lines. Pure (tested).
export function floorplanSvg(fp, { size = 168 } = {}) {
  if (!fp.die) return "";
  const [x0, y0, x1, y1] = fp.die;
  const w = x1 - x0, h = y1 - y0;
  const pad = Math.max(w, h) * 0.06;
  const vb = [x0 - pad, -(y1 + pad), w + 2 * pad, h + 2 * pad];   // y flipped: the die's origin is bottom-left
  const sw = Math.max(w, h) / size;                                 // one screen pixel, in µm
  let rows = "";
  if (fp.core && fp.rows > 0 && fp.core[2] > fp.core[0]) {
    const step = Math.max(1, Math.ceil(fp.rows / 12));
    const rh = (fp.core[3] - fp.core[1]) / fp.rows;
    for (let i = step; i < fp.rows; i += step) {
      const y = fp.core[1] + i * rh;
      rows += "<line class='fp-row' x1='" + fp.core[0] + "' x2='" + fp.core[2] + "' y1='" + (-y) + "' y2='" + (-y) + "' stroke-width='" + sw + "'/>";
    }
  }
  const core = fp.core && fp.core[2] > fp.core[0] && fp.core[3] > fp.core[1]
    ? "<rect class='fp-core' x='" + fp.core[0] + "' y='" + (-fp.core[3]) + "' width='" + (fp.core[2] - fp.core[0]) + "' height='" + (fp.core[3] - fp.core[1]) +
      "' stroke-width='" + sw + "'><title>Core " + n1(fp.core[2] - fp.core[0]) + " × " + n1(fp.core[3] - fp.core[1]) + " µm, " + fp.rows + " rows</title></rect>" : "";
  const label = "Die " + n1(w) + " by " + n1(h) + " µm" + (core ? ", core " + n1(fp.core[2] - fp.core[0]) + " by " + n1(fp.core[3] - fp.core[1]) + " µm with " + fp.rows + " rows" : ", no core") +
    (fp.estimate ? " (an estimate)" : "");
  return "<svg class='fp-svg" + (fp.estimate ? " is-estimate" : "") + "' viewBox='" + vb.join(" ") + "' preserveAspectRatio='xMidYMid meet' role='img' aria-label='" + esc(label) + "'>" +
    "<rect class='fp-die' x='" + x0 + "' y='" + (-y1) + "' width='" + w + "' height='" + h + "' stroke-width='" + 1.5 * sw + "'><title>Die " + n1(w) + " × " + n1(h) + " µm</title></rect>" +
    core + rows + "</svg>";
}

const MARK = { ok: "pass", warn: "warn", fail: "fail" };

// The whole preview: eyebrow, drawing, the sizes, the checks, the source.
export function previewHtml(fp, k, { compact = false } = {}) {
  if (fp.state === "no-pdk") {
    return compact ? "" : "<p class='fp-empty'>The preview needs the PDK's library installed (Tools).</p>";
  }
  if (fp.state === "no-area") return "<p class='fp-empty'>Enter the die area (x0 y0 x1 y1 in µm) to see the floorplan.</p>";
  if (fp.state === "bad-util") return "<p class='fp-empty'>Core utilization is a percentage above 0.</p>";
  const head = "<div class='fp-head'><span class='an-eyebrow'>Floorplan" + (fp.estimate ? " · estimate" : "") + "</span></div>";
  if (fp.state === "needs-synthesis") {
    return head + "<div class='fp-body'><div class='fp-pic fp-pic-empty' aria-hidden='true'><span></span></div>" +
      "<div class='fp-text'><p class='fp-src'>" + esc(sourceLine(fp, k)) + "</p></div></div>";
  }
  const w = fp.die[2] - fp.die[0], h = fp.die[3] - fp.die[1];
  const core = fp.core && fp.core[2] > fp.core[0] && fp.core[3] > fp.core[1];
  const dims = "<div class='fp-dims'><b>" + esc(n1(w)) + " × " + esc(n1(h)) + "</b><small>µm die</small>" +
    (core ? "<span class='fp-sep'>·</span><b>" + esc(n1(fp.core[2] - fp.core[0])) + " × " + esc(n1(fp.core[3] - fp.core[1])) + "</b><small>µm core, " + fp.rows + " rows</small>" : "") + "</div>";
  const checks = fp.checks.length ? "<ul class='fp-checks'>" + fp.checks.map((c) =>
    "<li class='is-" + c.level + "'><span class='vf-mark is-" + MARK[c.level] + "' aria-hidden='true'></span><span>" + esc(c.text) +
    (c.code && c.level !== "ok" ? " <code>" + esc(c.code) + "</code>" : "") + "</span></li>").join("") + "</ul>" : "";
  const src = sourceLine(fp, k);
  return head + "<div class='fp-body'><div class='fp-pic'>" + floorplanSvg(fp, { size: compact ? 120 : 168 }) + "</div>" +
    "<div class='fp-text'>" + dims + checks + (src ? "<p class='fp-src'>" + esc(src) + "</p>" : "") + "</div></div>";
}

// The server's inputs for the open project, PDK and library, fetched once and
// kept until the config changes or a run finishes (the project card's clock
// line and Setup's preview share them).
let _in = null, _inKey = "";
export async function estimateInputs({ pdk, scl, project = true } = {}) {
  const [{ api }, { state }] = await Promise.all([import("./api.js"), import("./state.js")]);
  const p = pdk ?? state.selectedPdk ?? "", s = scl ?? state.selectedScl ?? "";
  const key = [project ? state.designDir : "", p, s].join("|");
  if (!_in || _inKey !== key) {
    _inKey = key;
    _in = api.floorplanEstimate(p, s, { project }).catch((e) => { _in = null; throw e; });
  }
  return _in;
}
if (typeof document !== "undefined" && document.addEventListener) {
  const drop = () => { _in = null; };
  document.addEventListener("g:config_changed", drop);
  document.addEventListener("g:flow_done", drop);
}

export function mountFloorplan(el, { values, inputs, compact = false } = {}) {
  if (!el) return null;
  const fp = computeFloorplan(values || {}, inputs || {});
  el.innerHTML = previewHtml(fp, inputs || {}, { compact });
  el.hidden = !el.innerHTML;
  el.classList.toggle("is-problem", fp.state === "problem" || fp.state === "no-core");
  return fp;
}
