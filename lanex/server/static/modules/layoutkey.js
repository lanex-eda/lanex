// layoutkey.js — the key on the Layout canvas: what each colour on screen
// is, in plain words, for the layers drawn right now (audit 4d).
//
//   keyRows({ layers, vis, stage, kind, pdn })   rows for the visible layers
//   keyRowsHtml(rows)                            the rows' markup
//   paintSwatches(root, byId, light)             real swatches: fill, frame and
//                                                KLayout's own stipple bitmap
//   paintHistogram(canvas, counts, ramp, light)  the bars over a heat map's scale
//
// Rows are merged by meaning, top of the stack first, at most eight. The PDK's
// own names stay on each row (mono) and in its tooltip for the people who
// want them; the words are for everyone else. sky130 gives several layers the
// same colour, so a swatch draws the stipple too: that is what tells them apart.

import { rampCss } from "./heatmap.js";
import { escapeHtml } from "./html.js";

const esc = escapeHtml;

const RX_VIA = /^(via\d*|v\d|topvia\d?|tv\d?|mcon|cont|contact|licon\d?)$/i;
const RX_METAL = /^(?:met|metal|m)(\d)$/i;
const RX_TOP = /^(?:topmetal|tm)(\d?)$|^metaltop$/i;

// Which bucket a layer goes in.
export function bucketOf(L) {
  const name = String(L.name || "");
  if (L.group === "pin") return "pin";
  if (L.group === "misc") return /boundary|outline/i.test(name + " " + (L.purpose || "")) ? "outline" : "misc";
  if (L.group === "well") return "well";
  if (L.group === "device") return "device";
  if (L.group === "local") return /^(mcon|cont|contact|licon\d?)$/i.test(name) ? "via" : "local";
  if (L.group === "metal") {
    if (RX_VIA.test(name)) return "via";
    const m = name.match(RX_METAL);
    if (m) return "m" + m[1];
    if (RX_TOP.test(name)) return "top";
    return "m9";
  }
  return "misc";
}

const ORDER = ["top", "m9", "m8", "m7", "m6", "m5", "m4", "m3", "m2", "m1", "via", "local", "device", "well", "pin", "outline", "misc"];
const PRE_ROUTE = new Set(["floorplan", "pdn", "placement", "cts"]);

function metalWords(n, names, { stage, pdn }) {
  const has = (x) => x && names.some((nm) => nm.toLowerCase() === String(x).toLowerCase());
  const power = [];
  if (pdn && has(pdn.rail)) power.push("the power rails along each row");
  if (pdn && has(pdn.vertical)) power.push("power straps, up and down");
  if (pdn && has(pdn.horizontal)) power.push("power straps, across");
  if (stage === "pdn") return power.length ? cap(power.join("; ")) : "Wiring layer " + n;
  if (n === 1) {
    if (PRE_ROUTE.has(stage)) return "Cell pins and the power rails along each row";
    return "Short hops between neighbouring cells" + (power.length ? ", and the rails" : "");
  }
  const base = n <= 3 ? "Local wires between cells" : "Long wires across the chip";
  return power.length && n > 1 ? base + (power.some((p) => p.startsWith("power straps")) ? ", and the power straps" : "") : base;
}

function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

// The rows for what is drawn now.
export function keyRows({ layers = [], vis = new Set(), stage = "", kind = "gds", pdn = null, preset = "" } = {}) {
  const buckets = new Map();
  // Top of the stack first inside a row too (met5 before met4, via4 before licon).
  for (const L of [...layers].sort((a, b) => (b.z || 0) - (a.z || 0))) {
    if (!vis.has(L.id)) continue;
    const b = bucketOf(L);
    if (!buckets.has(b)) buckets.set(b, []);
    buckets.get(b).push(L);
  }
  // The Power preset reads the metals by their power job, as the PDN stage does.
  if (preset === "power") stage = "pdn";
  let rows = [];
  for (const b of ORDER) {
    const ls = buckets.get(b);
    if (!ls || !ls.length) continue;
    // The swatch is the drawing layer when there is one (not its pin or label twin).
    const sw = ls.find((L) => /^(drawing|drw|)$/i.test(L.purpose || "")) || ls[0];
    const names = [...new Set(ls.map((L) => L.name))];
    const tip = ls.map((L) => L.label + " " + L.src).join(" · ");
    let label, words;
    if (/^m\d$/.test(b)) {
      const n = +b.slice(1);
      label = "Metal " + n;
      words = metalWords(n, names, { stage, pdn });
    } else if (b === "top") { label = "Top metal"; words = "Thick metal on top: power and long wires"; }
    else if (b === "via") { label = "Vias"; words = "Plugs that join one layer to the next"; }
    else if (b === "local") { label = "Local interconnect"; words = "Thin wiring just above the transistors"; }
    else if (b === "device") { label = "Transistors"; words = "Diffusion and poly: where poly crosses diffusion is a gate"; }
    else if (b === "well") { label = "Wells and implants"; words = "Where the transistors' silicon is doped"; }
    else if (b === "pin") { label = "Pins"; words = "Where wires connect, with their names"; }
    else if (b === "outline") {
      label = "Outlines";
      words = kind === "def" ? (stage === "floorplan" ? "The die, and the rows cells will sit in" : "The die, and each standard cell's box")
        : "The die and cell boundaries";
    } else { label = "Other layers"; words = "Markers the rule decks read"; }
    rows.push({ key: b, label, words, ids: ls.map((L) => L.id), sw, names, tip });
  }
  // Too many rows: pair up metals that say the same thing.
  if (rows.length > 8) {
    const out = [];
    for (const r of rows) {
      const prev = out[out.length - 1];
      if (prev && /^m\d/.test(prev.key) && /^m\d/.test(r.key) && prev.words === r.words && !prev.merged) {
        prev.label = "Metal " + r.key.slice(1) + "–" + prev.key.slice(1);
        prev.ids = prev.ids.concat(r.ids);
        prev.names = prev.names.concat(r.names);
        prev.tip += " · " + r.tip;
        prev.merged = true;
        prev.key += "+" + r.key;
        continue;
      }
      out.push({ ...r });
    }
    rows = out;
  }
  return rows.slice(0, 9);
}

export function keyRowsHtml(rows, { solo = "" } = {}) {
  if (!rows.length) return "<p class='lx-key-empty'>No layers are shown. Pick a preset in Layers.</p>";
  return rows.map((r) =>
    "<button class='lx-key-row' data-key-row='" + esc(r.key) + "' aria-pressed='" + (solo === r.key) + "' title='" +
    esc(r.words + ". " + r.tip + ". Click to show only this; click again for all.") + "'>" +
    "<canvas class='lx-key-sw' data-sw='" + r.sw.id + "' width='36' height='24' aria-hidden='true'></canvas>" +
    "<span class='lx-key-t'><b>" + esc(r.label) + "</b><span>" + esc(r.words) + "</span></span>" +
    "<code class='lx-key-n'>" + esc(r.names.slice(0, 2).join(" ") + (r.names.length > 2 ? " …" : "")) + "</code></button>").join("");
}

function hexRgb(h) {
  const n = parseInt(String(h || "#8ea6c4").slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// One swatch the way KLayout draws the layer: the stipple in the fill colour
// over the canvas background, framed in the frame colour.
export function drawSwatch(cv, L, light = false) {
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const bg = light ? [255, 255, 255] : [6, 8, 13];
  const fill = hexRgb(L.fill);
  const rows = Array.isArray(L.stipple) && L.stipple.length ? L.stipple : null;
  const img = ctx.createImageData(W, H);
  const k = 2;   // two device pixels per stipple bit: readable at any density
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      let on = true;
      if (rows) {
        const r = rows[Math.floor(y / k) % rows.length];
        on = r[Math.floor(x / k) % r.length] === "*";
      } else if ((L.dither | 0) === 1) on = false;
      const c = on ? fill : bg;
      const o = (y * W + x) * 4;
      img.data[o] = c[0]; img.data[o + 1] = c[1]; img.data[o + 2] = c[2]; img.data[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  ctx.strokeStyle = L.frame || L.fill;
  ctx.lineWidth = 2;
  ctx.strokeRect(1, 1, W - 2, H - 2);
}

export function paintSwatches(root, byId, light = false) {
  if (!root) return;
  root.querySelectorAll("canvas[data-sw]").forEach((cv) => {
    const L = byId.get(+cv.dataset.sw);
    if (L) drawSwatch(cv, L, light);
  });
}

// The heat map's histogram: one bar per bucket in the ramp's colour, height
// by the square root of the count (one hot tile still shows).
// `range` ({ lo, hi, unit, what }): what a bin holds, for the hover ("40 to
// 42.5 %: 12 tiles") and the canvas's accessible name.
export function paintHistogram(cv, counts, ramp = "lanex", light = false, range = null) {
  if (!cv || !counts || !counts.length) return;
  if (range) {
    const { lo = 0, hi = 1, unit = "", what = "tiles" } = range;
    const w = (hi - lo) / counts.length;
    const f = (x) => (Math.abs(x) >= 100 ? Math.round(x) : Math.round(x * 10) / 10);
    const words = (i) => f(lo + i * w) + " to " + f(lo + (i + 1) * w) + (unit ? " " + unit : "") + ": " + counts[i] + " " + what;
    cv._histWords = words;
    cv.setAttribute("role", "img");
    cv.removeAttribute("aria-hidden");
    cv.setAttribute("aria-label", "How many " + what + " have each value, " + f(lo) + " to " + f(hi) + (unit ? " " + unit : "") + "; the most in one bar: " + Math.max(...counts));
    if (!cv._histWired) {
      cv._histWired = true;
      cv.addEventListener("pointermove", (e) => {
        const r = cv.getBoundingClientRect();
        const n = (cv._histCounts || []).length;
        if (!n || !r.width) return;
        const i = Math.min(n - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * n)));
        cv.title = cv._histWords ? cv._histWords(i) : "";
      });
    }
  }
  cv._histCounts = counts;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const max = Math.max(1, ...counts);
  const bw = W / counts.length;
  counts.forEach((n, i) => {
    if (!n) return;
    const h = Math.max(2, Math.round(Math.sqrt(n / max) * (H - 1)));
    ctx.fillStyle = rampCss(ramp, (i + 0.5) / counts.length, light);
    ctx.fillRect(Math.floor(i * bw), H - h, Math.max(1, Math.ceil(bw) - 1), h);
  });
}
