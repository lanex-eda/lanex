// analytics-model.js — what each LibreLane metric means, in which unit, and how
// to show it. Pure (no DOM), so node tests can import it.
//
// LibreLane names metrics METRICS2.1-style: `category__name__stat` plus
// optional `__modifier:value` parts (`__corner:max_ss_100C_1v60`,
// `__net:VPWR`, `__class:fill_cell`, `__iter:3`). Everything here works on the
// tool's own keys and values; LanEx only converts units (W -> mW, V -> mV,
// ratio -> %) and never derives a new silicon number.

import * as FS from "./flowstages.js";

// Key anatomy, numbers and units live in metricfmt.js, the one formatter
// Runs, Compare and the sweep views share with this page; they are
// re-exported here so everything that reads them from this module keeps working.
import { parseKey, isNum, nonFinite, num, si, unitOf, show, signed, LANEX_DIRECTIONS, fixedDirection, METRIC_NAMES } from "./metricfmt.js";
import { cornerWords } from "./cornergrid.js";
export { parseKey, isNum, nonFinite, num, si, unitOf, show, signed };

// ---------- labels ---------------------------------------------------------------

// The names live in metricfmt.js (one list for every page).
export const LABELS = METRIC_NAMES;

export function labelOf(key) {
  const { base, mods } = parseKey(key);
  let l = LABELS[base];
  if (!l) return null;
  if (mods.class) l += " · " + mods.class.replace(/_/g, " ");
  if (mods.net) l += " · " + mods.net;
  if (mods.iter !== undefined) l += " · iteration " + mods.iter;
  if (mods.code) l += " · " + mods.code;
  if (mods.corner) l += " · " + mods.corner;
  return l;
}

// ---------- categories for the full table --------------------------------------------

export const CATEGORIES = [
  ["timing", "Timing", (b) => /^timing__|^clock__|^design__max_(slew|cap|fanout)/.test(b)],
  ["area", "Area & cells", (b) => /^design__(die|core|instance|rows|sites|io)|^floorplan__/.test(b)],
  ["power", "Power & IR drop", (b) => /^power__|^ir__|^design_powergrid__|^design__power_grid/.test(b)],
  ["route", "Routing & antenna", (b) => /^route__|^global_route__|^antenna/.test(b)],
  ["checks", "Physical & netlist checks", (b) => /^magic__|^klayout__|^design__(lvs|xor|lint|inferred|instance_unmapped|disconnected|critical_disconnected|violations)|^synthesis__/.test(b)],
  ["flow", "Flow", (b) => /^flow__/.test(b)],
];

export function categoryOf(key) {
  const { base } = parseKey(key);
  for (const [id, , test] of CATEGORIES) if (test(base)) return id;
  return "other";
}

// ---------- PVT corners ----------------------------------------------------------


// "max_ss_100C_1v60" -> { rc: "max", proc: "ss", temp: "100 °C", volt: "1.60 V" }
// Unknown shapes return { raw } only; the UI then shows the raw name.
export function parseCorner(name) {
  const out = { raw: name };
  for (const t of String(name).split("_")) {
    if (/^(min|nom|max)$/.test(t)) out.rc = t;
    else if (/^(ss|ff|tt|sf|fs)$/i.test(t)) out.proc = t.toLowerCase();
    else if (/^n?\d+C$/.test(t)) out.temp = (t.startsWith("n") ? "−" : "") + parseInt(t.replace(/^n/, ""), 10) + " °C";
    else if (/^\d+v\d+$/i.test(t)) out.volt = t.replace(/v/i, ".") + " V";
    else if (/^\d+p\d+V$/.test(t)) out.volt = t.replace("p", ".").replace(/V$/, " V");
  }
  return out;
}

// A corner in words: one namer for every page (cornergrid.js cornerWords):
// "slow process, 100 °C, 1.60 V, max wire RC"; an unknown name stays raw.
export function cornerShort(name) { return cornerWords(name) || String(name); }
export function cornerLabel(name) { return cornerWords(name) || String(name); }

// All corners that carry `base`, with values: [{corner, value}] in metric order.
export function byCorner(values, base) {
  const out = [];
  const pre = base + "__corner:";
  for (const [k, v] of Object.entries(values || {})) {
    if (k.startsWith(pre)) out.push({ corner: k.slice(pre.length), value: v });
  }
  return out;
}

// ---------- flow stages ------------------------------------------------------------

// The seven stages every page uses (flowstages.js). Analytics groups by label.
export const STAGES = FS.STAGES.map((s) => s.label);
export const stepCore = FS.stepCore;

export function stageSteps(steps) {
  const ids = FS.stagesOf(steps);
  return steps.map((s, i) => ({ ...s, stage: FS.stageLabel(ids[i]) }));
}

// Human name for a step: "OpenROAD.DetailedRouting" -> "Detailed routing",
// "OpenROAD.STAMidPNR-3" -> "STA mid-PnR (4)" (LibreLane suffixes repeats -1, -2…).
export function stepName(s) {
  const id = s.id || "";
  if (!id) return String(s.dir || "").replace(/^\d+-[a-z]+-/i, "").replace(/-/g, " ");
  let tail = id.split(".").slice(1).join(".") || id;
  const rep = /-(\d+)$/.exec(tail);
  tail = tail.replace(/-\d+$/, "");
  const words = tail.match(/[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+/g) || [tail];
  const out = words.map((w, i) => {
    if (w === "PNR") return "PnR";
    if (w.length > 1 && w === w.toUpperCase() && /[A-Z]/.test(w)) return w;
    return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase();
  }).join(" ").replace(/^STA (Pre|Mid|Post) PnR$/i, (m, p) => "STA " + p.toLowerCase() + "-PnR");
  return rep ? out + " (" + (Number(rep[1]) + 1) + ")" : out;
}

// Rebuild the running metric state after each step from the per-step deltas
// the server sends. Returns a new array of {…step, metrics} (shared objects
// are never mutated).
export function accumulate(steps) {
  let acc = {};
  return steps.map((s) => {
    if (s.changed && Object.keys(s.changed).length) acc = { ...acc, ...s.changed };
    return { ...s, metrics: acc };
  });
}

// ---------- signoff-style checks --------------------------------------------------

// The pass/fail counters LibreLane writes. `sev` is what a non-zero count
// means: "fail" when LibreLane's registry marks the metric critical (or it is a
// DRC count, which the registry omits for KLayout), "warn" otherwise.
export const CHECKS = [
  { group: "Timing", label: "Setup", keys: ["timing__setup_vio__count"], tip: "Endpoints whose signal arrives too late for the clock edge. LibreLane adds the corners up, so one that fails in two corners counts twice." },
  { group: "Timing", label: "Hold", keys: ["timing__hold_vio__count"], tip: "Endpoints whose signal changes too soon after the clock edge. LibreLane adds the corners up, so one that fails in two corners counts twice." },
  { group: "Electrical", label: "Max slew", keys: ["design__max_slew_violation__count"], tip: "Signals whose transitions are slower than the library allows." },
  { group: "Electrical", label: "Max cap", keys: ["design__max_cap_violation__count"], tip: "Nets driving more capacitance than the driver allows." },
  { group: "Electrical", label: "Max fanout", keys: ["design__max_fanout_violation__count"], tip: "Nets driving more inputs than the fanout limit." },
  { group: "Layout", label: "DRC Magic", keys: ["magic__drc_error__count"], sev: "fail", tip: "Design-rule errors found by Magic on the final layout." },
  { group: "Layout", label: "DRC KLayout", keys: ["klayout__drc_error__count"], sev: "fail", tip: "Design-rule errors found by KLayout on the final layout." },
  { group: "Layout", label: "Route DRC", keys: ["route__drc_errors"], sev: "fail", tip: "Rule errors the detailed router could not fix." },
  { group: "Layout", label: "LVS", keys: ["design__lvs_error__count"], tip: "Layout vs. schematic: does the drawn layout match the netlist?" },
  { group: "Layout", label: "XOR", keys: ["design__xor_difference__count"], tip: "Differences between the GDS written by Magic and by KLayout." },
  { group: "Layout", label: "Antenna", keys: ["route__antenna_violation__count", "antenna__violating__nets"], tip: "Long wires that could damage gates during manufacturing." },
  { group: "Layout", label: "Overlaps", keys: ["magic__illegal_overlap__count"], tip: "Illegal overlaps between cells found by Magic." },
  { group: "Netlist", label: "Lint", keys: ["design__lint_error__count"], tip: "Errors Verilator found in your RTL." },
  { group: "Netlist", label: "Unmapped", keys: ["design__instance_unmapped__count"], tip: "Cells synthesis could not map to the standard-cell library." },
  { group: "Netlist", label: "Latches", keys: ["design__inferred_latch__count"], tip: "Latches inferred from the RTL (often an incomplete if/case)." },
  { group: "Netlist", label: "Disconnected", keys: ["design__critical_disconnected_pin__count"], tip: "Critical pins left unconnected in the layout." },
  { group: "Netlist", label: "Power grid", keys: ["design__power_grid_violation__count"], tip: "Cells not connected to the power grid." },
];

// -> [{...check, key, value, status: pass|warn|fail|absent}]
export function evalChecks(values, meta) {
  return CHECKS.map((c) => {
    const key = c.keys.find((k) => values[k] !== undefined && values[k] !== null);
    if (!key) return { ...c, key: c.keys[0], value: undefined, status: "absent" };
    const v = values[key];
    const n = typeof v === "number" ? v : Number(v);
    let status = "pass";
    if (!(n === 0)) {
      const m = meta && meta.get && meta.get(key);
      status = c.sev || (m && m.critical ? "fail" : "warn");
      if (!Number.isFinite(n)) status = "warn";
    }
    return { ...c, key, value: v, status };
  });
}

// Check label -> Verify check id (controller/verify.py).
const SIGNOFF_ID = {
  "Setup": "setup", "Hold": "hold", "Max slew": "slew", "Max cap": "cap", "Max fanout": "fanout",
  "DRC Magic": "magic_drc", "DRC KLayout": "klayout_drc", "Route DRC": "route_drc", "LVS": "lvs", "XOR": "xor",
  "Antenna": "antenna", "Overlaps": "overlap", "Lint": "lint_errors", "Unmapped": "unmapped", "Latches": "latch",
  "Disconnected": "disc_pin", "Power grid": "pdn",
};

// Replace metric-only statuses with the server's LibreLane-exact verdicts.
export function withSignoff(checks, rep) {
  if (!rep || !Array.isArray(rep.checks)) return checks;
  const by = new Map(rep.checks.map((c) => [c.id, c]));
  const map = { pass: "pass", info: "pass", warn: "warn", fail: "fail", not_run: "absent", skipped: "absent" };
  return checks.map((c) => {
    const v = by.get(SIGNOFF_ID[c.label]);
    return v ? { ...c, status: map[v.status] || c.status } : c;
  });
}

// ---------- deltas ---------------------------------------------------------------------

// Change from baseline `b` to current `a`. `dir` is +1 when higher is better,
// −1 when lower is better, 0 when LibreLane's registry doesn't say (then the
// delta is shown neutral; LanEx never guesses a direction).
// -> null | { diff, pct, verdict: better|worse|same|neutral }
export function delta(a, b, dir) {
  if (!isNum(a) || !isNum(b)) return null;
  const diff = a - b;
  const pct = b !== 0 ? (diff / Math.abs(b)) * 100 : null;
  let verdict = "neutral";
  if (Math.abs(diff) < 1e-12) verdict = "same";
  else if (dir) verdict = (diff > 0) === (dir > 0) ? "better" : "worse";
  return { diff, pct, verdict };
}

// Direction from the registry, by base metric name; the metrics LibreLane
// doesn't register read like their registered relatives (metricfmt.js
// LANEX_DIRECTIONS), so Runs, Compare and Analytics colour the same numbers.
export function directionOf(meta, key) {
  // The few registry directions that don't hold (metricfmt.fixedDirection).
  const fx = fixedDirection(key);
  if (fx != null) return fx;
  const m = meta && meta.get && (meta.get(key) || meta.get(parseKey(key).base));
  if (m && typeof m.higher_is_better === "boolean") return m.higher_is_better ? 1 : -1;
  return LANEX_DIRECTIONS[key] || LANEX_DIRECTIONS[parseKey(key).base] || 0;
}
