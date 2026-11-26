// heatmap.js — a binned map (a value per tile of a 2D grid) drawn over a
// canvas view, and its colour scale. Reusable: the Layout tab's density and
// congestion overlays use it, and any page with a grid of values can.
//
//   const heat = buildHeat(map, { ramp: "lanex", floor: 20, light: false });
//   drawHeat(ctx, view, heat, { opacity: 0.7 });       // every frame: one drawImage
//   heatValueAt(map, x, y)                            // the tile under a point
//   histogram(map.v, 48, 0, 100)                      // counts for the legend
//
// A map is { xs: [tile edges], ys: [tile edges], nx, ny, v: [row by row from
// the bottom, null = no tile], min, max } in world units (µm on the layout).
// The tiles are painted once into a small offscreen canvas (one to four
// pixels per tile, edges at their real positions, so a smaller last tile
// stays right) and then scaled with smoothing off, so pan and zoom cost one
// drawImage whatever the number of tiles. Values over 100 (more demand than
// room) get a hatch on top, never just a hue.

// Inferno from 15 % to 100 %: lightness rises with the value, so on the dark
// canvas more means brighter, it survives greyscale and the common colour
// deficiencies, and it never ends in the red the DRC markers use.
const INFERNO = ["#2c0b50", "#420a68", "#6a176e", "#932667", "#bc3754", "#dd513a", "#f37819", "#fca50a", "#f6d746", "#fcffa4"];

export const RAMPS = {
  lanex: { label: "LanEx", note: "Brighter is more. Reads in greyscale and with colour-blindness." },
  turbo: { label: "OpenROAD", note: "The rainbow the OpenROAD GUI uses (Turbo)." },
};

function hexRgb(h) { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
const INF = INFERNO.map(hexRgb);

function lerp(stops, t) {
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
}

// Google's Turbo, as their published polynomial fit.
function turbo(t) {
  t = Math.min(1, Math.max(0, t));
  const r = 0.13572138 + t * (4.61539260 + t * (-42.66032258 + t * (132.13108234 + t * (-152.94239396 + t * 59.28637943))));
  const g = 0.09140261 + t * (2.19418839 + t * (4.84296658 + t * (-14.18503333 + t * (4.27729857 + t * 2.82956604))));
  const b = 0.10667330 + t * (12.64194608 + t * (-60.58204836 + t * (110.36276771 + t * (-89.90310912 + t * 27.34824973))));
  return [r, g, b].map((c) => Math.round(Math.min(1, Math.max(0, c)) * 255));
}

// [r, g, b] for t in 0..1. On a white canvas the LanEx ramp runs the other
// way (more is darker) over 10-85 % of inferno, so the faint end never
// vanishes into the white.
export function rampColor(ramp, t, light = false) {
  if (ramp === "turbo") return turbo(t);
  if (light) return lerp(INF, 0.85 - Math.min(1, Math.max(0, t)) * 0.75);
  return lerp(INF, t);
}

export function rampCss(ramp, t, light = false) {
  const [r, g, b] = rampColor(ramp, t, light);
  return "rgb(" + r + "," + g + "," + b + ")";
}

// A CSS gradient of the ramp (the legend bar).
export function rampGradient(ramp, light = false, steps = 10) {
  const stops = [];
  for (let i = 0; i <= steps; i++) stops.push(rampCss(ramp, i / steps, light) + " " + (i * 100 / steps).toFixed(0) + "%");
  return "linear-gradient(90deg," + stops.join(",") + ")";
}

// Counts of values in n equal buckets between lo and hi (values above hi go
// in the last bucket, below lo in the first).
export function histogram(values, n, lo, hi) {
  const out = new Array(n).fill(0);
  const span = hi - lo || 1;
  for (const v of values) {
    if (v == null || !Number.isFinite(v)) continue;
    const i = Math.min(n - 1, Math.max(0, Math.floor(((v - lo) / span) * n)));
    out[i] += 1;
  }
  return out;
}

function bisect(edges, x) {
  let lo = 0, hi = edges.length - 1;
  if (x < edges[0] || x > edges[hi]) return -1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (edges[mid] <= x) lo = mid; else hi = mid;
  }
  return lo;
}

// The tile under (x, y): { v, box } or null.
export function heatValueAt(map, x, y) {
  if (!map || !map.xs || !map.ys) return null;
  const i = bisect(map.xs, x), j = bisect(map.ys, y);
  if (i < 0 || j < 0 || i >= map.nx || j >= map.ny) return null;
  const v = map.v[j * map.nx + i];
  if (v == null) return null;
  return { v, box: [map.xs[i], map.ys[j], map.xs[i + 1], map.ys[j + 1]] };
}

// The colour scale's top: 100 for a percentage map, else the map's max.
export function scaleTop(map) {
  if (!map) return 100;
  return map.unit === "%" ? 100 : (map.max || 1);
}

function makeCanvas(w, h) {
  if (typeof OffscreenCanvas !== "undefined") return new OffscreenCanvas(w, h);
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  return c;
}

// Paint the map once: { canvas, box, over: [tile boxes over 100] }.
// floor: values below it are left transparent ("show from").
export function buildHeat(map, { ramp = "lanex", floor = 0, light = false, top = null } = {}) {
  if (!map || !map.nx || !map.ny) return null;
  const n = map.nx * map.ny;
  const px = n > 40000 ? 1 : n > 4000 ? 2 : 4;
  const x0 = map.xs[0], x1 = map.xs[map.nx], y0 = map.ys[0], y1 = map.ys[map.ny];
  // Pixels per world unit, so tile edges land where they are (a smaller
  // last GCell stays smaller).
  let minStep = Infinity;
  for (let i = 0; i < map.nx; i++) minStep = Math.min(minStep, map.xs[i + 1] - map.xs[i]);
  for (let j = 0; j < map.ny; j++) minStep = Math.min(minStep, map.ys[j + 1] - map.ys[j]);
  const k = px / (minStep > 0 && Number.isFinite(minStep) ? minStep : 1);
  const W = Math.max(1, Math.min(4096, Math.round((x1 - x0) * k)));
  const H = Math.max(1, Math.min(4096, Math.round((y1 - y0) * k)));
  const kx = W / (x1 - x0), ky = H / (y1 - y0);
  const cv = makeCanvas(W, H);
  const ctx = cv.getContext("2d");
  const hi = top == null ? scaleTop(map) : top;
  const over = [];
  for (let j = 0; j < map.ny; j++) {
    const sy0 = Math.round(H - (map.ys[j + 1] - y0) * ky), sy1 = Math.round(H - (map.ys[j] - y0) * ky);
    for (let i = 0; i < map.nx; i++) {
      const v = map.v[j * map.nx + i];
      if (v == null || v < floor || v <= 0) continue;
      const sx0 = Math.round((map.xs[i] - x0) * kx), sx1 = Math.round((map.xs[i + 1] - x0) * kx);
      ctx.fillStyle = rampCss(ramp, v / hi, light);
      ctx.fillRect(sx0, sy0, Math.max(1, sx1 - sx0), Math.max(1, sy1 - sy0));
      if (map.unit === "%" && v > 100) over.push([map.xs[i], map.ys[j], map.xs[i + 1], map.ys[j + 1]]);
    }
  }
  return { canvas: cv, box: [x0, y0, x1, y1], over };
}

let _hatch = null;
function hatchPattern(ctx, light) {
  const key = light ? "l" : "d";
  if (_hatch && _hatch.key === key) return _hatch.pat;
  const c = makeCanvas(8, 8);
  const g = c.getContext("2d");
  g.strokeStyle = light ? "rgba(0,0,0,0.85)" : "rgba(255,255,255,0.9)";
  g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(-2, 10); g.lineTo(10, -2); g.moveTo(-2, 2); g.lineTo(2, -2); g.moveTo(6, 10); g.lineTo(10, 6); g.stroke();
  _hatch = { key, pat: ctx.createPattern(c, "repeat") };
  return _hatch.pat;
}

// Draw a built heat map in a view ({ w2s(x, y) -> [sx, sy], W, H }).
export function drawHeat(ctx, view, heat, { opacity = 0.7, light = false } = {}) {
  if (!heat) return;
  const [sx0, sy0] = view.w2s(heat.box[0], heat.box[3]);
  const [sx1, sy1] = view.w2s(heat.box[2], heat.box[1]);
  if (sx1 < 0 || sy1 < 0 || sx0 > view.W || sy0 > view.H) return;
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.globalAlpha = Math.min(1, Math.max(0, opacity));
  ctx.drawImage(heat.canvas, sx0, sy0, sx1 - sx0, sy1 - sy0);
  if (heat.over && heat.over.length) {
    ctx.globalAlpha = Math.min(1, opacity + 0.2);
    ctx.fillStyle = hatchPattern(ctx, light);
    for (const b of heat.over) {
      const [a0, a1] = view.w2s(b[0], b[3]);
      const [c0, c1] = view.w2s(b[2], b[1]);
      ctx.fillRect(a0, a1, c0 - a0, c1 - a1);
    }
  }
  ctx.restore();
}
