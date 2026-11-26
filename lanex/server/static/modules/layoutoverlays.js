// layoutoverlays.js — what the Layout tab draws over the chip besides the
// layers: highlighted nets, a timing path, the clock tree, and the models
// behind them. Pure functions of their arguments (the page passes its view,
// { w2s, W, H, box, scale }), so they are tested without a browser and the
// saved picture draws the very same thing.

import { rampCss } from "./heatmap.js";

// Eight highlight colours (OpenROAD's eight highlight groups): the app's
// teal first, then Okabe-Ito, which stays apart for colour-blind eyes.
export const NET_COLORS = ["#38d6c8", "#E69F00", "#56B4E9", "#F0E442", "#CC79A7", "#D55E00", "#009E73", "#FFFFFF"];
// Supply and ground in the Power preset: orange and blue read apart with
// red-green colour-blindness too.
export const POWER_COLORS = { vdd: "#ff7a45", gnd: "#4da3ff" };
export const PATH_COLOR = "#ffd166";
export const CLOCK_COLOR = "#a78bfa";

export const cleanName = (s) => String(s ?? "").replace(/\\/g, "").trim();
const uniq = (a) => [...new Set(a.filter(Boolean))];

// "input34/X" -> "input34"; a top-level port ("x", "dsa[3]") has no instance.
export function instOfPin(pin) {
  const p = cleanName(pin);
  const i = p.lastIndexOf("/");
  return i > 0 ? p.slice(0, i) : "";
}

// A highlight request from another page (Verify's "Show on layout"), made
// safe and complete. The contract (docs: design/LANE-3-design.md 1.5):
//   { kind: "path"|"net"|"cells", label, insts:[], nets:[], pins:[],
//     points?: [[pin, x_um, y_um, arrival_ns], ...] | [{pin, x, y, arrival, net, inst, cell}],
//     slack?, corner?, startpoint?, endpoint?, delay_type? "max"|"min" }
export function normaliseHighlight(h = {}) {
  const kind = ["path", "net", "cells"].includes(h.kind) ? h.kind : "path";
  const num = (v) => (v === null || v === undefined || v === "" || !Number.isFinite(+v) ? null : +v);
  const points = (Array.isArray(h.points) ? h.points : []).map((p) => {
    if (Array.isArray(p)) return { pin: cleanName(p[0]), x: num(p[1]), y: num(p[2]), arrival: num(p[3]), net: "", inst: instOfPin(p[0]), cell: "" };
    return { pin: cleanName(p.pin), x: num(p.x), y: num(p.y), arrival: num(p.arrival), net: cleanName(p.net), inst: cleanName(p.inst) || instOfPin(p.pin), cell: p.cell || "" };
  }).filter((p) => p.pin);
  const pins = (Array.isArray(h.pins) ? h.pins : []).map(cleanName).filter(Boolean);
  // Without points, the pins in order are the path (their places come later).
  const pts = points.length ? points : pins.map((pin) => ({ pin, x: null, y: null, arrival: null, net: "", inst: instOfPin(pin), cell: "" }));
  const insts = uniq([...(h.insts || []).map(cleanName), ...pts.map((p) => p.inst)]);
  const nets = uniq([...(h.nets || []).map(cleanName), ...pts.map((p) => p.net)]);
  const label = String(h.label || (kind === "path" ? "Timing path" : kind === "net" ? "Nets" : "Cells"));
  const slack = num(h.slack);
  return { kind, label, insts, nets, pins, points: pts, slack, corner: h.corner ? String(h.corner) : "",
    startpoint: cleanName(h.startpoint || (pts[0] && pts[0].pin) || ""),
    endpoint: cleanName(h.endpoint || (pts.length ? pts[pts.length - 1].pin : "")),
    delay_type: h.delay_type === "min" ? "min" : "max" };
}

// A path from the analysis export (odbexport.read_paths) as a highlight.
export function pathFromExport(p, { type = "setup", corner = "" } = {}) {
  return normaliseHighlight({
    kind: "path", label: (type === "hold" ? "Hold path" : "Setup path") + " · " + (p.startpoint || "?") + " → " + (p.endpoint || "?"),
    points: (p.points || []).map((q) => ({ pin: q.pin, x: q.x, y: q.y, arrival: q.arrival, net: q.net, inst: q.inst, cell: q.cell })),
    slack: p.slack, corner, startpoint: p.startpoint, endpoint: p.endpoint, delay_type: type === "hold" ? "min" : "max" });
}

// Fill each point's place from what the layout knows: the export's own x/y,
// else the instance's centre, else a top-level pin's label. Adds each
// stage's increment (arrival - previous arrival).
export function placePoints(points, cells = {}, labels = []) {
  const byText = new Map();
  for (const l of labels || []) if (!byText.has(l.text)) byText.set(l.text, l);
  let prev = null;
  return points.map((p) => {
    let { x, y } = p;
    if (x == null || y == null) {
      const c = p.inst && cells[p.inst];
      if (c && c.box) { x = (c.box[0] + c.box[2]) / 2; y = (c.box[1] + c.box[3]) / 2; }
      else if (!p.inst && byText.has(p.pin)) { const l = byText.get(p.pin); x = l.x; y = l.y; }
    }
    const incr = p.arrival != null && prev != null ? p.arrival - prev : null;
    if (p.arrival != null) prev = p.arrival;
    const cell = p.cell || (p.inst && cells[p.inst] && cells[p.inst].cell) || "";
    return { ...p, x, y, incr, cell };
  });
}

const inView = (b, vb) => !(b[2] < vb[0] || b[0] > vb[2] || b[3] < vb[1] || b[1] > vb[3]);

function hexA(hex, a) {
  const n = parseInt(String(hex).slice(1), 16);
  return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
}

// A rectangle in world µm, filled and/or stroked, at least minPx on screen.
function rect(ctx, v, b, minPx = 1.5) {
  let [x1, y1] = v.w2s(b[0], b[3]);
  let [x2, y2] = v.w2s(b[2], b[1]);
  if (x2 - x1 < minPx) { const c = (x1 + x2) / 2; x1 = c - minPx / 2; x2 = c + minPx / 2; }
  if (y2 - y1 < minPx) { const c = (y1 + y2) / 2; y1 = c - minPx / 2; y2 = c + minPx / 2; }
  return [x1, y1, x2 - x1, y2 - y1];
}

// Flight lines: from the pin nearest the middle to every other pin (a net
// before routing has no wires to draw).
function flightLines(ctx, v, pins, color) {
  if (!pins || pins.length < 2) return;
  const cx = pins.reduce((a, p) => a + p.x, 0) / pins.length, cy = pins.reduce((a, p) => a + p.y, 0) / pins.length;
  let hub = pins[0], best = Infinity;
  for (const p of pins) { const d = (p.x - cx) ** 2 + (p.y - cy) ** 2; if (d < best) { best = d; hub = p; } }
  const [hx, hy] = v.w2s(hub.x, hub.y);
  ctx.save();
  ctx.strokeStyle = hexA(color, 0.85);
  ctx.lineWidth = 1.25;
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  for (const p of pins) {
    if (p === hub) continue;
    const [x, y] = v.w2s(p.x, p.y);
    ctx.moveTo(hx, hy); ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.restore();
}

// Highlighted nets: their shapes in their colour, their pins as dots; a net
// with no wires on this stage gets flight lines between its pins.
export function drawNets(ctx, v, nets) {
  const vb = v.box;
  for (const n of nets || []) {
    const g = n.g;
    if (!g || !g.found) continue;
    ctx.save();
    ctx.fillStyle = hexA(n.color, 0.82);
    ctx.strokeStyle = n.color;
    ctx.lineWidth = 1;
    for (const b of g.boxes || []) {
      const box = [b[1], b[2], b[3], b[4]];
      if (!inView(box, vb)) continue;
      const r = rect(ctx, v, box, 1.5);
      ctx.fillRect(r[0], r[1], r[2], r[3]);
    }
    if (!(g.boxes && g.boxes.length)) flightLines(ctx, v, g.pins, n.color);
    if (!n.preset) {
      ctx.fillStyle = n.color;
      for (const p of g.pins || []) {
        const [x, y] = v.w2s(p.x, p.y);
        if (x < -8 || y < -8 || x > v.W + 8 || y > v.H + 8) continue;
        ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
      }
    }
    ctx.restore();
  }
}

// A timing path: its cells outlined, its nets' wires, and a line through
// its pins in order over a dark halo; numbered stage dots when there is
// room; the start a hollow circle, the end a filled square.
export function drawPath(ctx, v, path, { active = -1, light = false } = {}) {
  if (!path) return;
  const vb = v.box;
  ctx.save();
  // nets first (under the cells)
  for (const g of Object.values(path.netGeom || {})) {
    for (const b of g.boxes || []) {
      const box = [b[1], b[2], b[3], b[4]];
      if (!inView(box, vb)) continue;
      const r = rect(ctx, v, box, 1.5);
      ctx.fillStyle = hexA(PATH_COLOR, 0.55);
      ctx.fillRect(r[0], r[1], r[2], r[3]);
    }
  }
  for (const [name, c] of Object.entries(path.cells || {})) {
    if (!c.box || !inView(c.box, vb)) continue;
    const r = rect(ctx, v, c.box, 4);
    ctx.fillStyle = hexA(PATH_COLOR, name === path.activeInst ? 0.42 : 0.18);
    ctx.fillRect(r[0], r[1], r[2], r[3]);
    ctx.strokeStyle = PATH_COLOR;
    ctx.lineWidth = name === path.activeInst ? 2.5 : 1.5;
    ctx.strokeRect(Math.round(r[0]) + 0.5, Math.round(r[1]) + 0.5, Math.round(r[2]), Math.round(r[3]));
  }
  const pts = (path.placed || []).filter((p) => p.x != null && p.y != null).map((p, i) => ({ ...p, s: v.w2s(p.x, p.y), i }));
  if (pts.length > 1) {
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.s[0], p.s[1]) : ctx.moveTo(p.s[0], p.s[1])));
    ctx.strokeStyle = light ? "rgba(255,255,255,0.85)" : "rgba(0,0,0,0.7)";
    ctx.lineWidth = 5;
    ctx.stroke();
    ctx.strokeStyle = PATH_COLOR;
    ctx.lineWidth = 2.5;
    ctx.stroke();
  }
  // stage numbers where they do not crowd each other
  ctx.font = "600 10px " + (v.mono || "monospace");
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  let lastX = -1e9, lastY = -1e9;
  pts.forEach((p, k) => {
    const [x, y] = p.s;
    if (x < -10 || y < -10 || x > v.W + 10 || y > v.H + 10) return;
    const first = k === 0, last = k === pts.length - 1;
    if (first) {
      ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle = light ? "#ffffff" : "#06080d"; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = PATH_COLOR; ctx.stroke();
    } else if (last) {
      ctx.fillStyle = PATH_COLOR; ctx.fillRect(x - 6, y - 6, 12, 12);
      ctx.strokeStyle = light ? "#ffffff" : "#06080d"; ctx.lineWidth = 1.5; ctx.strokeRect(x - 6, y - 6, 12, 12);
    } else if (Math.hypot(x - lastX, y - lastY) > 18 || p.i === active) {
      ctx.beginPath(); ctx.arc(x, y, p.i === active ? 8 : 6.5, 0, Math.PI * 2);
      ctx.fillStyle = PATH_COLOR; ctx.fill();
      ctx.fillStyle = "#1a1200";
      ctx.fillText(String(p.i + 1), x, y + 0.5);
      lastX = x; lastY = y;
    }
    if (p.i === active) {
      ctx.beginPath(); ctx.arc(x, y, 13, 0, Math.PI * 2);
      ctx.strokeStyle = light ? "#0a1320" : "#ffffff"; ctx.lineWidth = 2; ctx.stroke();
    }
  });
  ctx.restore();
}

// The clock tree: each clock net as a star from its driver to its sinks,
// buffers outlined, flip-flops as dots coloured by when the clock arrives
// (the heat ramp: early dark, late bright), so skew reads as colour.
export function drawClock(ctx, v, clock, { ramp = "lanex", light = false, focusNet = "" } = {}) {
  if (!clock || !clock.nets) return;
  const s = clock.summary || {};
  const lo = s.arrival_min, hi = s.arrival_max;
  const span = lo != null && hi != null && hi > lo ? hi - lo : 0;
  const vb = v.box;
  const c = (b) => [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
  ctx.save();
  for (const n of clock.nets) {
    const src = n.driver || n.port;
    if (!src) continue;
    const dim = focusNet && n.name !== focusNet;
    const [dx, dy] = c(src.box);
    const [sx, sy] = v.w2s(dx, dy);
    ctx.strokeStyle = hexA(CLOCK_COLOR, dim ? 0.18 : 0.8);
    ctx.lineWidth = n.name === focusNet ? 2 : 1.2;
    ctx.beginPath();
    for (const k of n.sinks) {
      const [x, y] = c(k.box);
      const [tx, ty] = v.w2s(x, y);
      if ((sx < 0 && tx < 0) || (sy < 0 && ty < 0) || (sx > v.W && tx > v.W) || (sy > v.H && ty > v.H)) continue;
      ctx.moveTo(sx, sy); ctx.lineTo(tx, ty);
    }
    ctx.stroke();
    if (n.driver && inView(n.driver.box, vb)) {
      const r = rect(ctx, v, n.driver.box, 6);
      ctx.strokeStyle = CLOCK_COLOR;
      ctx.lineWidth = 2;
      ctx.fillStyle = hexA(CLOCK_COLOR, dim ? 0.1 : 0.28);
      ctx.fillRect(r[0], r[1], r[2], r[3]);
      ctx.strokeRect(Math.round(r[0]) + 0.5, Math.round(r[1]) + 0.5, Math.round(r[2]), Math.round(r[3]));
    }
  }
  for (const n of clock.nets) {
    const dim = focusNet && n.name !== focusNet;
    for (const k of n.sinks) {
      if (!k.ff) continue;
      const [x, y] = c(k.box);
      const [tx, ty] = v.w2s(x, y);
      if (tx < -6 || ty < -6 || tx > v.W + 6 || ty > v.H + 6) continue;
      const t = span && k.arrival != null ? (k.arrival - lo) / span : 0.5;
      ctx.beginPath(); ctx.arc(tx, ty, 4, 0, Math.PI * 2);
      ctx.fillStyle = dim ? "rgba(140,150,170,0.35)" : rampCss(ramp, 0.25 + 0.75 * t, light);
      ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = light ? "rgba(0,0,0,0.6)" : "rgba(0,0,0,0.85)"; ctx.stroke();
    }
  }
  ctx.restore();
}

// Every marker of the visible report sources, in the order the Checks pane
// lists them (source, then category by count), for next and previous.
export function flattenMarkers(markers) {
  const out = [];
  for (const [si, s] of ((markers && markers.sources) || []).entries()) {
    if (s.hidden || s.ok === false) continue;
    for (const c of s.categories || []) {
      for (const it of s.items || []) if (it.cat === c.name) out.push({ si, item: it, cat: c.name, label: s.label });
    }
  }
  return out;
}

// The next (+1) or previous (-1) marker, or the first of the next category
// (byCat), wrapping at the ends. Returns { i, wrapped }.
export function stepMarker(list, cur, dir, { byCat = false } = {}) {
  const n = list.length;
  if (!n) return { i: -1, wrapped: false };
  let i = list.findIndex((m) => m.item === cur);
  if (i < 0) return { i: dir > 0 ? 0 : n - 1, wrapped: false };
  if (!byCat) {
    const j = i + dir;
    return { i: (j + n) % n, wrapped: j < 0 || j >= n };
  }
  const firstOf = (j) => { const c2 = list[j].cat; while (j > 0 && list[j - 1].cat === c2) j--; return j; };
  const cat = list[i].cat;
  let j = i;
  for (let k = 0; k < n; k++) {
    j += dir;
    if (j < 0 || j >= n) {
      const w = dir > 0 ? 0 : firstOf(n - 1);
      return { i: w, wrapped: list[w].cat !== cat || w !== i };
    }
    if (list[j].cat !== cat) return { i: dir < 0 ? firstOf(j) : j, wrapped: false };
  }
  return { i, wrapped: false };
}
