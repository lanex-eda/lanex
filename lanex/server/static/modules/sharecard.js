// sharecard.js — the share card: a picture of the chip with the numbers
// students post when a design is done (die, clock, cells, utilisation, setup
// and hold slack, Fmax per process corner, DRC, LVS, antenna), in the LanEx
// look: the near-black blue substrate with its faint grid, Barlow for words,
// JetBrains Mono for numbers, spaced mono caps labels, the layer-colour
// hairline, and the chip lit in the app's own layer colours.
//
//   cardData(summary, verdict, { showRun, showDate }) -> data (pure; tests)
//   drawCard(canvas, data, { layout: "wide" | "square", scale: 2, chip, logo })
//   altText(data) -> one sentence for screen readers and posts
//
// Drawn in the page on a canvas: the fonts are the app's (vendored), the chip
// image is same-origin, so nothing leaves the PC and the canvas stays exportable.

import { parseCorner } from "./cornergrid.js";

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
export const SIZES = { wide: [1200, 630], square: [1080, 1080] };

// The LanEx dark tokens (styles.css :root); a card is always dark.
const C = {
  bg: "#06080d", surface: "#0e131c", surface2: "#131b28", border: "#1f2a3b", borderStrong: "#33445e",
  text: "#c2cee0", strong: "#f2f7ff", muted: "#7e8ea7", met1: "#4da3ff", glow: "rgba(77, 163, 255, 0.42)",
  pass: "#36d399", warn: "#f2c14e", fail: "#ff6b70",
  layers: ["#d6446f", "#8ea6c4", "#4da3ff", "#e879c9", "#36d399", "#f2c14e", "#ff7a45"],
};

function slack(v) {
  if (!isNum(v) || Math.abs(v) >= 1e12) return "—";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2);
}

const fmtInt = (v) => (isNum(v) ? Math.round(v).toLocaleString("en-US") : "—");
const trimNum = (v, d = 1) => (isNum(v) ? (+v.toFixed(d)).toString() : "—");

// Pure: the words and numbers on the card, from /api/verify/summary and
// Verify's verdict words (verify-model.js verdictCopy).
export function cardData(sum, verdict, { showRun = true, showDate = true } = {}) {
  const idn = (sum && sum.identity) || {};
  const a = (sum && sum.area) || {};
  const t = (sum && sum.timing) || {};
  const ph = (sum && sum.physical) || {};
  const corners = t.corners || [];
  const minOf = (k) => { const v = corners.map((c) => c[k]).filter((x) => isNum(x) && Math.abs(x) < 1e12); return v.length ? Math.min(...v) : null; };
  const rows = [];
  rows.push(["Die", a.die_w ? trimNum(a.die_w) + " × " + trimNum(a.die_h) + " µm" : "—"]);
  rows.push(["Clock", isNum(t.clock_period) ? trimNum(t.clock_period, 2) + " ns · " + trimNum(t.mhz, 1) + " MHz" : "—"]);
  rows.push(["Logic cells", isNum(a.logic_cells) ? fmtInt(a.logic_cells) + (isNum(a.flipflops) ? " · " + fmtInt(a.flipflops) + " flip-flops" : "") : "—"]);
  rows.push(["Utilisation", isNum(a.util) ? Math.round(a.util * 100) + " %" : "—"]);
  if (t.no_clock) {
    rows.push(["Timing", "not checked: no clock"]);
  } else {
    rows.push(["Setup", "WNS " + slack(minOf("setup_ws")) + " ns · TNS " + slack(minOf("setup_tns"))]);
    rows.push(["Hold", "WNS " + slack(minOf("hold_ws")) + " ns · TNS " + slack(minOf("hold_tns"))]);
  }
  // Register-to-register Fmax per process corner (the slowest wire RC of each).
  const byProc = new Map();
  for (const c of corners) {
    if (!isNum(c.fmax)) continue;
    const p = parseCorner(c.corner).process || c.corner;
    byProc.set(p, Math.min(byProc.has(p) ? byProc.get(p) : Infinity, c.fmax));
  }
  const order = { ss: 0, slow: 0, tt: 1, typ: 1, typical: 1, ff: 2, fast: 2 };
  const fm = [...byProc.entries()].sort((x, y) => (order[x[0]] ?? 5) - (order[y[0]] ?? 5));
  if (fm.length && !t.no_clock) rows.push(["Reg→reg Fmax", fm.map(([p, v]) => Math.round(v) + " " + p).join(" · ") + " MHz"]);
  const drc = [ph.magic_drc, ph.klayout_drc, ph.route_drc].filter(isNum);
  const lvs = ph.lvs ? (/match uniquely|circuits match/i.test(ph.lvs) && !/not/i.test(ph.lvs) ? "match" : "mismatch") : "—";
  rows.push(["Checks", "DRC " + (drc.length ? fmtInt(drc.reduce((s, x) => s + x, 0)) : "—") + " · LVS " + lvs + " · antenna " + (isNum(ph.antenna_nets) ? fmtInt(ph.antenna_nets) : "—")]);
  const v = verdict || {};
  const tone = v.tone === "pass" ? "pass" : v.tone === "fail" ? "fail" : "warn";
  const word = String(v.short || v.title || (sum && sum.verdict && sum.verdict.label) || "").toUpperCase();
  const n = corners.length;
  const line = tone === "pass" && !v.warned ? "Every LibreLane check passed" + (n ? " · " + n + " timing corners" : "") : (v.title || "");
  let date = "";
  if (showDate && idn.finished) {
    const d = new Date(idn.finished);
    if (!isNaN(d)) date = d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }
  const foot = ["LibreLane " + (idn.librelane || "?"), showRun ? "run " + idn.run : "", date].filter(Boolean).join(" · ");
  return {
    design: idn.design || "design", pdk: idn.pdk || "", scl: idn.scl || "", tone, word, line, rows, foot,
    die: a.die_w ? [a.die_w, a.die_h] : null, cells: a.logic_cells, clock: t.clock_period, run: idn.run,
  };
}

export function altText(d) {
  const bits = [];
  if (d.die) bits.push("a " + trimNum(d.die[0]) + " by " + trimNum(d.die[1]) + " µm die");
  if (isNum(d.cells)) bits.push(fmtInt(d.cells) + " logic cells");
  if (isNum(d.clock)) bits.push(trimNum(d.clock, 2) + " ns clock");
  return "Layout of " + d.design + (d.pdk ? " on " + d.pdk : "") + (bits.length ? ": " + bits.join(", ") : "") + ". " +
    (d.line ? d.line.replace(/ · .*$/, "") + ". " : "") + "Made with LanEx; not a foundry sign-off.";
}

function fit(ctx, text, max) {
  if (ctx.measureText(text).width <= max) return text;
  let s = text;
  while (s.length > 1 && ctx.measureText(s + "…").width > max) s = s.slice(0, -1);
  return s + "…";
}

function grid(ctx, x, y, w, h, step, alpha) {
  ctx.save();
  ctx.strokeStyle = C.border;
  ctx.globalAlpha = alpha;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let gx = x + step; gx < x + w; gx += step) { ctx.moveTo(Math.round(gx) + 0.5, y); ctx.lineTo(Math.round(gx) + 0.5, y + h); }
  for (let gy = y + step; gy < y + h; gy += step) { ctx.moveTo(x, Math.round(gy) + 0.5); ctx.lineTo(x + w, Math.round(gy) + 0.5); }
  ctx.stroke();
  ctx.restore();
}

function hairline(ctx, x, y, w) {
  const seg = w / C.layers.length;
  ctx.save();
  ctx.globalAlpha = 0.55;
  C.layers.forEach((c, i) => { ctx.fillStyle = c; ctx.fillRect(x + i * seg, y, seg + 0.5, 2); });
  ctx.restore();
}

function panel(ctx, x, y, w, h) {
  ctx.save();
  const g = ctx.createLinearGradient(0, y, 0, y + h);
  g.addColorStop(0, C.surface2);
  g.addColorStop(1, C.surface);
  ctx.fillStyle = g;
  ctx.fillRect(x, y, w, h);
  grid(ctx, x, y, w, h, 24, 0.55);
  ctx.strokeStyle = C.borderStrong;
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  // The lit top edge of an .an-card.
  const top = ctx.createLinearGradient(x, 0, x + w, 0);
  top.addColorStop(0, "rgba(77,163,255,0)");
  top.addColorStop(0.5, C.met1);
  top.addColorStop(1, "rgba(77,163,255,0)");
  ctx.fillStyle = top;
  ctx.fillRect(x, y, w, 2);
  ctx.restore();
}

function chipInto(ctx, img, x, y, w, h, dieLabel) {
  if (!img) {
    ctx.save();
    ctx.fillStyle = C.muted;
    ctx.font = "500 15px Barlow, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Couldn't draw the layout; the numbers are on the right.", x + w / 2, y + h / 2);
    ctx.restore();
    return;
  }
  const pad = 28;
  const iw = img.naturalWidth || img.width, ih = img.naturalHeight || img.height;
  const k = Math.min((w - 2 * pad) / iw, (h - 2 * pad - 18) / ih);
  const dw = iw * k, dh = ih * k;
  const dx = x + (w - dw) / 2, dy = y + (h - 18 - dh) / 2;
  ctx.save();
  ctx.shadowColor = C.glow;
  ctx.shadowBlur = 28;
  ctx.fillStyle = C.bg;
  ctx.fillRect(dx, dy, dw, dh);
  ctx.restore();
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, dx, dy, dw, dh);
  ctx.strokeStyle = "rgba(77,163,255,0.35)";
  ctx.strokeRect(dx - 0.5, dy - 0.5, dw + 1, dh + 1);
  if (dieLabel) {
    ctx.save();
    ctx.fillStyle = C.muted;
    ctx.font = "500 12px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    ctx.fillText(dieLabel, x + w / 2, y + h - 14);
    ctx.restore();
  }
}

function caps(ctx, text, x, y, { size = 12, color = C.muted, align = "left", spacing = "0.14em" } = {}) {
  ctx.save();
  ctx.font = "500 " + size + "px 'JetBrains Mono', monospace";
  try { ctx.letterSpacing = spacing; } catch (_e) {}
  ctx.fillStyle = color;
  ctx.textAlign = align;
  ctx.fillText(text.toUpperCase(), x, y);
  ctx.restore();
}

function verdictChip(ctx, d, x, y) {
  const col = d.tone === "pass" ? C.pass : d.tone === "fail" ? C.fail : C.warn;
  ctx.save();
  ctx.fillStyle = col;
  ctx.fillRect(x, y - 13, 14, 14);
  ctx.font = "700 18px Barlow, sans-serif";
  try { ctx.letterSpacing = "0.08em"; } catch (_e) {}
  ctx.fillText(d.word, x + 24, y);
  ctx.restore();
}

export async function loadFonts() {
  if (!document.fonts || !document.fonts.load) return;
  await Promise.all([
    document.fonts.load("700 56px Barlow"), document.fonts.load("500 20px Barlow"),
    document.fonts.load("500 24px 'JetBrains Mono'"), document.fonts.load("500 12px 'JetBrains Mono'"),
  ]).catch(() => {});
}

export async function drawCard(canvas, d, { layout = "wide", scale = 2, chip = null, logo = null } = {}) {
  await loadFonts();
  const [W, H] = SIZES[layout] || SIZES.wide;
  canvas.width = W * scale;
  canvas.height = H * scale;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);
  grid(ctx, 0, 0, W, H, 24, 0.32);
  const M = 40;
  // Header: the LanEx logo, the PDK and library.
  if (logo) {
    const lh = 30, lw = lh * (logo.naturalWidth || logo.width) / (logo.naturalHeight || logo.height);
    ctx.drawImage(logo, M, M - 6, lw, lh);
  } else {
    ctx.fillStyle = C.strong;
    ctx.font = "700 24px Barlow, sans-serif";
    ctx.fillText("LanEx", M, M + 18);
  }
  caps(ctx, [d.pdk, d.scl].filter(Boolean).join(" · "), W - M, M + 14, { align: "right" });
  const dieLabel = d.die ? trimNum(d.die[0]) + " × " + trimNum(d.die[1]) + " µm" : "";
  if (layout === "square") {
    panel(ctx, M, 96, W - 2 * M, 500);
    chipInto(ctx, chip, M, 96, W - 2 * M, 500, dieLabel);
    ctx.fillStyle = C.strong;
    ctx.font = "700 52px Barlow, sans-serif";
    ctx.fillText(fit(ctx, d.design, W - 2 * M - 300), M, 666);
    verdictChip(ctx, d, W - M - 280, 660);
    ctx.fillStyle = C.muted;
    ctx.font = "500 20px Barlow, sans-serif";
    ctx.fillText(fit(ctx, d.line, W - 2 * M), M, 702);
    const colW = (W - 2 * M) / 2;
    d.rows.forEach(([k, v], i) => {
      const cx = M + (i % 2) * colW, cy = 760 + Math.floor(i / 2) * 64;
      caps(ctx, k, cx, cy);
      ctx.fillStyle = C.strong;
      ctx.font = "500 24px 'JetBrains Mono', monospace";
      ctx.fillText(fit(ctx, v, colW - 24), cx, cy + 32);
    });
    hairline(ctx, M, H - 62, W - 2 * M);
    caps(ctx, d.foot, M, H - 30, { size: 13, spacing: "0.04em" });
    caps(ctx, "Not a foundry sign-off", W - M, H - 30, { size: 13, align: "right", spacing: "0.04em" });
    return canvas;
  }
  const PW = 500, PH = H - 96 - 80;
  panel(ctx, M, 96, PW, PH);
  chipInto(ctx, chip, M, 96, PW, PH, dieLabel);
  const X = M + PW + 44, RW = W - M - X;
  ctx.fillStyle = C.strong;
  ctx.font = "700 56px Barlow, sans-serif";
  ctx.fillText(fit(ctx, d.design, RW), X, 150);
  verdictChip(ctx, d, X, 190);
  ctx.fillStyle = C.muted;
  ctx.font = "500 19px Barlow, sans-serif";
  ctx.fillText(fit(ctx, d.line, RW), X, 222);
  const top = 270, step = Math.min(40, (H - 96 - top) / Math.max(1, d.rows.length));
  d.rows.forEach(([k, v], i) => {
    const y = top + i * step;
    caps(ctx, k, X, y);
    ctx.fillStyle = C.strong;
    ctx.font = "500 21px 'JetBrains Mono', monospace";
    ctx.fillText(fit(ctx, v, RW - 168), X + 168, y + 2);
  });
  hairline(ctx, M, H - 58, W - 2 * M);
  caps(ctx, d.foot, M, H - 28, { size: 13, spacing: "0.04em" });
  caps(ctx, "Not a foundry sign-off", W - M, H - 28, { size: 13, align: "right", spacing: "0.04em" });
  return canvas;
}

export function loadImage(src, timeoutMs = 25000) {
  return new Promise((resolve) => {
    const img = new Image();
    const t = setTimeout(() => resolve(null), timeoutMs);
    img.onload = () => { clearTimeout(t); resolve(img); };
    img.onerror = () => { clearTimeout(t); resolve(null); };
    img.src = src;
  });
}
