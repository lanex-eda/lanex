// metricfmt.js — one way to show a LibreLane metric, used by Runs, Compare,
// the sweep views and Analytics, so the same number reads the same
// everywhere: power with an SI prefix (never raw W, never a fixed mW that
// turns a few µW into 0.000), utilisation as a percentage (never the 0–1
// fraction), slack signed in ns, areas in µm². Pure (no DOM): node tests
// import it.
//
// LibreLane names metrics METRICS2.1-style: `category__name__stat` plus
// optional `__modifier:value` parts (`__corner:max_ss_100C_1v60`,
// `__net:VPWR`, `__class:fill_cell`, `__iter:3`). LanEx only converts units
// (W -> mW, V -> mV, ratio -> %) and never derives a new silicon number.

import { localeNum } from "./intlcache.js";

// ---------- key anatomy ----------------------------------------------------

// "timing__setup__ws__corner:max_ss_100C_1v60" ->
//   { base: "timing__setup__ws", mods: { corner: "max_ss_100C_1v60" } }
// Most modifiers are their own `__name:value` part. A few are glued onto the
// stat itself (`flow__warnings__count:DRT-0349`, `design__rows:unithd`): the
// stat stays in the base name and the value becomes `mods.code`.
const MOD_NAMES = ["corner", "net", "class", "iter"];
export function parseKey(key) {
  const parts = String(key).split("__");
  const base = [];
  const mods = {};
  for (const p of parts) {
    const i = p.indexOf(":");
    if (i > 0) {
      const name = p.slice(0, i);
      const val = p.slice(i + 1);
      if (MOD_NAMES.includes(name)) {
        mods[name] = val;
      } else {
        base.push(name);
        mods.code = val;
      }
    } else {
      base.push(p);
    }
  }
  return { base: base.join("__"), mods };
}

// ---------- numbers -----------------------------------------------------------

export function isNum(v) { return typeof v === "number" && Number.isFinite(v); }

// Non-finite values are real LibreLane output (e.g. reg-to-reg slack is +inf
// when a design has no register-to-register paths). Keep them distinct from
// "absent".
export function nonFinite(v) {
  return v === "Infinity" || v === "-Infinity" || v === "NaN" ||
    (typeof v === "number" && !Number.isFinite(v));
}

const US = "en-US";

// Fixed decimals, grouped, trailing zeros trimmed only when `trim`.
function fixed(n, d, trim) {
  let s = localeNum(n, US, { minimumFractionDigits: trim ? 0 : d, maximumFractionDigits: d });
  if (s === "-0") s = "0";
  return s.replace(/^-/, "−");
}

// Integer-aware general formatting: 1226 -> "1,226", 64 -> "64",
// 7214.42 -> "7,214.42", 0.0445963 -> "0.0446".
export function num(v, { digits } = {}) {
  if (v === null || v === undefined || v === "") return "—";
  if (nonFinite(v)) return v === "-Infinity" || v === -Infinity ? "−∞" : (v === "NaN" || Number.isNaN(v) ? "NaN" : "∞");
  if (typeof v !== "number") return String(v);
  // OpenSTA writes 1e30 for "no constrained path"; never spell that out.
  if (Math.abs(v) >= 1e12) return v.toExponential(2).replace(/^-/, "−");
  if (Number.isInteger(v)) return fixed(v, 0, true);
  if (digits !== undefined) return fixed(v, digits, false);
  const a = Math.abs(v);
  if (a >= 100) return fixed(v, 2, true);
  if (a >= 1) return fixed(v, 3, true);
  if (a >= 0.001) return fixed(v, 4, true);
  return v.toExponential(2).replace(/^-/, "−");
}

// The SI prefixes used for W and V, largest first.
const STEPS = [[1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"]];
function stepFor(a) {
  if (a === 0) return [1e-3, "m"];
  for (const s of STEPS) if (a >= s[0]) return s;
  return STEPS[STEPS.length - 1];
}
const digitsFor = (ax) => (ax >= 100 ? 1 : ax >= 10 ? 2 : 3);

// Pick an SI prefix for a quantity given in base units (W, V). Returns
// { text, unit } — e.g. power 0.0014772 W -> { "1.477", "mW" }.
export function si(v, unit) {
  if (!isNum(v)) return { text: num(v), unit };
  const [scale, pre] = stepFor(Math.abs(v));
  const x = v / scale;
  return { text: fixed(x, digitsFor(Math.abs(x)), false), unit: pre + unit };
}

// ---------- units per metric ------------------------------------------------------

// The unit each LibreLane metric is reported in (by its base name). LibreLane
// writes STA times in ns, power in W, IR drop/voltages in V, areas in µm²,
// lengths in µm; utilisation is a 0–1 ratio.
export function unitOf(base) {
  if (/^timing__.*__(ws|wns|tns)$/.test(base) || /^clock__skew/.test(base)) return "ns";
  if (/^power__/.test(base)) return "W";
  if (/(drop|voltage)/.test(base) && /^(ir__|design_powergrid__)/.test(base)) return "V";
  if (/__utilization/.test(base)) return "ratio";
  if (/__area/.test(base)) return "µm²";
  if (/wirelength/.test(base) || /__displacement__/.test(base) || /__hpwl$/.test(base)) return "µm";
  if (/__bbox$/.test(base)) return "µm";
  return "";
}

// A drop (IR drop, ground bounce) is millivolt-scale. `design_powergrid__
// drop__average` holds OpenROAD's *average voltage* (1.79997 on VPWR, see its
// IR report), so it is a voltage, never converted as if it were a drop.
const isDrop = (base) => /drop/.test(base) && base !== "design_powergrid__drop__average";
// OpenSTA writes about 1e30 for a slack with no path to time.
const NO_PATH = 1e12;

// One value formatted for display in its natural unit.
// -> { text, unit, title } (title = the exact raw value for hover).
// `compact` (tables): areas and lengths of 100 or more as whole numbers.
// It never changes the unit.
export function show(key, v, { compact = false } = {}) {
  const { base } = parseKey(key);
  const u = unitOf(base);
  const title = v === null || v === undefined ? "" : String(v);
  if (!isNum(v)) {
    if (u === "µm" && typeof v === "string" && /__bbox$/.test(base)) return { text: v, unit: u, title };
    return { text: num(v), unit: nonFinite(v) && u !== "ratio" ? u : "", title };
  }
  if (u === "W") { const r = si(v, "W"); return { text: r.text, unit: r.unit, title: title + " W" }; }
  if (u === "V") {
    // Drops and ground bounce are millivolt-scale; supply voltages read better in V.
    if (isDrop(base) || Math.abs(v) < 0.1) return { text: sig(v * 1000, 4), unit: "mV", title: title + " V" };
    return { text: sig(v, 6), unit: "V", title: title + " V" };
  }
  // A reg-to-reg slack of 1e30 is OpenSTA's "no path", not a number of ns.
  if (u === "ns" && Math.abs(v) >= NO_PATH) return { text: "no path", unit: "", title: title + " (OpenSTA: no path to time)" };
  if (u === "ratio") return { text: fixed(v * 100, 1, false), unit: "%", title: title + " (ratio)" };
  if (u === "ns") return { text: signed(v, compact ? 2 : 3), unit: "ns", title: title + " ns" };
  if (compact && (u === "µm²" || u === "µm") && Math.abs(v) >= 100 && Math.abs(v) < 1e12) {
    return { text: fixed(Math.round(v), 0, true), unit: u, title: title + " " + u };
  }
  return { text: num(v), unit: u, title: u ? title + " " + u : title };
}

// "1.477 mW": the value and its unit in one string.
export function showText(key, v, opts) {
  const s = show(key, v, opts);
  return s.unit && s.text !== "—" ? s.text + " " + s.unit : s.text;
}

// One prefix for a column or row of power or voltage values (a table reads
// down one unit, not mW in one row and µW in the next): the prefix of the
// largest power; mV for a drop, or for voltages all under 0.1 V, else V (as
// show() picks for one value). `null` for metrics whose unit has no prefix.
export function scaleOf(key, values) {
  const base = parseKey(key).base;
  const u = unitOf(base);
  if (u !== "W" && u !== "V") return null;
  const a = Math.max(0, ...values.filter(isNum).map(Math.abs));
  if (u === "V") return isDrop(base) || a < 0.1 ? { div: 1e-3, unit: "mV" } : { div: 1, unit: "V" };
  const [div, pre] = stepFor(a);
  return { div, unit: pre + u };
}

// A value at a scale from scaleOf (or as show() would, without one).
export function showAt(key, v, scale, opts) {
  if (!scale || !isNum(v)) return show(key, v, opts);
  const x = v / scale.div;
  if (/V$/.test(scale.unit)) return { text: sig(x, scale.unit === "mV" ? 4 : 6), unit: scale.unit, title: String(v) + " V" };
  return { text: fixed(x, digitsFor(Math.abs(x)), false), unit: scale.unit, title: String(v) + " W" };
}

// Up to `n` significant digits, trailing zeros dropped, never exponential for
// the magnitudes LibreLane reports in V/mV.
function sig(v, n) {
  if (v === 0) return "0";
  const d = Math.max(0, n - 1 - Math.floor(Math.log10(Math.abs(v))));
  return fixed(Number(v.toFixed(Math.min(d, 12))), Math.min(d, 12), true);
}

// Slack-style signed number: +4.100 / −0.250 / 0.000.
export function signed(v, d = 3) {
  if (!isNum(v) || Math.abs(v) >= 1e12) return (isNum(v) && v > 0 ? "+" : "") + num(v);
  if (v === 0) return fixed(0, d, false);
  return (v > 0 ? "+" : "−") + fixed(Math.abs(v), d, false);
}

// ---------- corner sums ---------------------------------------------------------------
// LibreLane adds the timing violation counts of every corner up (its registry's
// sum_aggregator): 102 = 36 + 31 + 35 failing endpoints in three corners of a
// 65-endpoint design. Never read the total as "how many paths fail".
const SUMMED = new Set(["timing__setup_vio__count", "timing__hold_vio__count",
  "timing__setup_r2r_vio__count", "timing__hold_r2r_vio__count"]);
export const isCornerSum = (key) => SUMMED.has(parseKey(key).base) && !parseKey(key).mods.corner;
// -> { total, n, worst, corner } from a run's metrics, or null when the
// metric isn't a corner sum or the run has no per-corner values.
export function cornerSum(metrics, key) {
  if (!metrics || !isCornerSum(key)) return null;
  const pre = key + "__corner:";
  let n = 0, worst = null, corner = "";
  for (const [k, v] of Object.entries(metrics)) {
    if (!k.startsWith(pre) || !isNum(v)) continue;
    n += 1;
    if (worst === null || v > worst) { worst = v; corner = k.slice(pre.length); }
  }
  return n ? { total: metrics[key], n, worst, corner } : null;
}
// "summed over 3 corners; 36 in the worst corner" (empty with one corner).
export function cornerSumWords(cs) {
  if (!cs || cs.n < 2) return "";
  return "summed over " + cs.n + " corners; " + num(cs.worst) + " in the worst corner";
}
// The hover words for a summed count, without the numbers.
export const CORNER_SUM_NOTE = "LibreLane adds this count up over every corner, so an endpoint that fails in two corners counts twice.";

// ---------- direction and change ----------------------------------------------------
// One reading of "better or worse" for Runs, Compare, Analytics and the sweep
// views. `meta` is the server's metric_meta (higher_is_better,
// direction_known) or LibreLane's registry entry; it is looked up by the
// full name, then by the base name (a per-corner row reads like its base
// metric, as LibreLane's own compare does). A few metrics LibreLane doesn't
// register read like their registered relatives (controller/runsmeta.py
// LANEX_DIRECTIONS says why for each).
export const LANEX_DIRECTIONS = {
  design__instance__area__stdcell: -1,
  design__instance__area__macros: -1,
  design__instance__area__padcells: -1,
  design__instance__count__stdcell: -1,
  klayout__drc_error__count: -1,
  runtime: -1,
};
// The registry's direction for the numbers every page shows, for when no
// metric_meta is at hand (LibreLane common/metrics/library.py).
const REGISTRY = {
  timing__setup__ws: 1, timing__hold__ws: 1, timing__setup__tns: 1, timing__hold__tns: 1,
  timing__setup__wns: 1, timing__hold__wns: 1, timing__setup_r2r__ws: 1, timing__hold_r2r__ws: 1,
  design__instance__utilization: 1, design__die__area: -1, design__core__area: -1,
  design__instance__area: -1, design__instance__count: -1, power__total: -1,
  route__wirelength: -1, route__drc_errors: -1, magic__drc_error__count: -1,
  design__lvs_error__count: -1, antenna__violating__nets: -1, antenna__violating__pins: -1,
};
// A direction LanEx knows better than the registry, which beats it: or null.
// `design_powergrid__drop__average` holds OpenROAD's average *voltage* of a
// net (1.79997 on VPWR, 0.0003 on VGND), which LibreLane registers as
// lower-is-better like a drop. On a power net a higher voltage is the smaller
// drop (better); on a ground net lower is better; with no net, no direction.
const POWER_NET = /^(vpwr|vdd|vcc|vccd|vdda|vccio|pwr)/i, GROUND_NET = /^(vgnd|gnd|vss|vssd|vssa|vssio)/i;
export function fixedDirection(key) {
  const { base, mods } = parseKey(key);
  if (base !== "design_powergrid__drop__average") return null;
  const net = mods.net || "";
  return POWER_NET.test(net) ? 1 : GROUND_NET.test(net) ? -1 : 0;
}

// +1 higher is better, -1 lower is better, 0 not known (no colour).
export function direction(key, meta) {
  const fx = fixedDirection(key);
  if (fx != null) return fx;
  const look = (k) => {
    const m = meta && meta[k];
    if (!m) return null;
    if (m.direction_known === false) return 0;
    if (typeof m.higher_is_better !== "boolean") return 0;
    return m.higher_is_better ? 1 : -1;
  };
  const base = parseKey(key).base;
  const d = look(key) || look(base);
  if (d) return d;
  return LANEX_DIRECTIONS[key] || LANEX_DIRECTIONS[base] || REGISTRY[key] || REGISTRY[base] || 0;
}

// How far `v` moved from `ref`, one way everywhere:
//   { abs, pct, absText, pctText, text, cls, same, dir }
// Δ always; Δ% only where a ratio means something (areas, lengths, power,
// runtime, counts of 100 or more; never slack, where a reference near zero
// makes a percentage meaningless). "same" within 0.05 % of the reference,
// and at least 5 ps of slack or a second of runtime. `cls` is is-better /
// is-worse only when the direction is known. The text carries no unit: it is
// in the unit the row or column is labelled with, `out.unit`. Pass that
// row's `scale` (from scaleOf) for power and voltages; without one, power
// takes the prefix of the larger of the two values and a voltage the unit
// show(key, 1) labels it with (mV for a drop, else V).
export function delta(key, v, ref, meta, { scale = null, dir = null } = {}) {
  if (!isNum(v) || !isNum(ref)) return null;
  const base = parseKey(key).base;
  const u = key === "runtime" ? "s" : unitOf(base);
  const dd = dir == null ? direction(key, meta) : dir;
  // OpenSTA's "no path" (1e30) is not a slack: no difference to it means anything.
  if (u === "ns" && (Math.abs(v) >= NO_PATH || Math.abs(ref) >= NO_PATH)) {
    return v === ref ? { abs: 0, pct: null, absText: "same", pctText: "", text: "same", cls: "is-same", same: true, dir: dd, unit: "" } : null;
  }
  const d = v - ref;
  const floor = u === "ns" ? 0.005 : u === "s" ? 1 : 1e-12;
  const same = Math.abs(d) <= Math.max(Math.abs(ref) * 5e-4, floor);
  const out = { abs: d, pct: null, absText: "same", pctText: "", text: "same", cls: "is-same", same, dir: dd,
    unit: u === "ratio" ? "pt" : u === "s" ? "" : u };
  if (same) return out;
  const sign = d > 0 ? "+" : "−";
  const a = Math.abs(d);
  let absText;
  if (u === "ns") absText = sign + fixed(a, a < 0.1 ? 3 : 2, false);
  else if (u === "ratio") absText = sign + fixed(a * 100, 1, false) + " pt";
  else if (u === "W") {
    const st = scale && /W$/.test(scale.unit) ? scale : scaleOf(key, [v, ref]);
    const x = a / st.div;
    absText = sign + fixed(x, digitsFor(x), false);
    out.unit = st.unit;
  } else if (u === "V") {
    const st = scale && /V$/.test(scale.unit) ? scale : isDrop(base) ? { div: 1e-3, unit: "mV" } : { div: 1, unit: "V" };
    absText = sign + sig(a / st.div, 4);
    out.unit = st.unit;
  } else if (u === "s") absText = sign + durText(a);
  else if (u === "µm²" || u === "µm") absText = sign + (a >= 100 ? fixed(Math.round(a), 0, true) : num(a));
  else absText = sign + num(Number(a.toPrecision(4)));
  const ratioOk = ref !== 0 && Math.sign(v) === Math.sign(ref) &&
    (u === "µm²" || u === "µm" || u === "W" || u === "s" || (u === "" && Math.abs(ref) >= 100));
  if (ratioOk) {
    const p = (d / Math.abs(ref)) * 100;
    out.pct = p;
    out.pctText = (p > 0 ? "+" : "−") + (Math.abs(p) >= 10 ? Math.round(Math.abs(p)) : Math.abs(p).toFixed(1)) + "%";
  }
  out.absText = absText;
  out.text = absText + (out.pctText ? " " + out.pctText : "");
  out.cls = !dd ? "" : (d > 0) === (dd > 0) ? "is-better" : "is-worse";
  return out;
}
function durText(s) {
  if (s < 60) return Math.round(s) + " s";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  if (m < 60) return m + " min" + (r ? " " + r + " s" : "");
  return Math.floor(m / 60) + " h " + (m % 60) + " min";
}
// The words for a delta's hover: "−0.95 ns, worse than the baseline (higher is better)".
export function deltaWords(dl, against = "the baseline") {
  if (!dl) return "";
  if (dl.same) return "The same as " + against;
  const how = dl.cls === "is-better" ? "better than " : dl.cls === "is-worse" ? "worse than " : "changed from ";
  const why = dl.dir > 0 ? " (higher is better)" : dl.dir < 0 ? " (lower is better)" : " (LibreLane gives this number no better or worse direction)";
  return dl.text + ", " + how + against + why;
}
// The direction mark for a column or row head: ↑ higher is better, ↓ lower.
export function dirMark(dir) { return dir > 0 ? "↑" : dir < 0 ? "↓" : ""; }
export function dirWords(dir) {
  return dir > 0 ? "Higher is better" : dir < 0 ? "Lower is better" : "No better or worse direction";
}
