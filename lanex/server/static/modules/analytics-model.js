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
import { parseKey, isNum, nonFinite, num, si, unitOf, show, signed, LANEX_DIRECTIONS } from "./metricfmt.js";
export { parseKey, isNum, nonFinite, num, si, unitOf, show, signed };

// ---------- labels ---------------------------------------------------------------

// Plain-English names for the base metrics people look at. Unknown keys fall
// back to the raw key (experts recognise those; nothing is invented).
export const LABELS = {
  "timing__setup__ws": "Worst setup slack",
  "timing__setup__wns": "Worst negative setup slack (WNS)",
  "timing__setup__tns": "Total negative setup slack (TNS)",
  "timing__setup_vio__count": "Setup violations",
  "timing__setup_r2r__ws": "Reg-to-reg setup slack",
  "timing__setup_r2r_vio__count": "Reg-to-reg setup violations",
  "timing__hold__ws": "Worst hold slack",
  "timing__hold__wns": "Worst negative hold slack (WNS)",
  "timing__hold__tns": "Total negative hold slack (TNS)",
  "timing__hold_vio__count": "Hold violations",
  "timing__hold_r2r__ws": "Reg-to-reg hold slack",
  "timing__hold_r2r_vio__count": "Reg-to-reg hold violations",
  "timing__unannotated_net__count": "Nets without parasitics",
  "timing__unannotated_net_filtered__count": "Nets without parasitics (filtered)",
  "timing__drv__floating__nets": "Floating nets",
  "timing__drv__floating__pins": "Floating pins",
  "clock__skew__worst_setup": "Worst clock skew (setup)",
  "clock__skew__worst_hold": "Worst clock skew (hold)",
  "design__max_slew_violation__count": "Max slew violations",
  "design__max_cap_violation__count": "Max capacitance violations",
  "design__max_fanout_violation__count": "Max fanout violations",
  "design__die__area": "Die area",
  "design__die__bbox": "Die box",
  "design__core__area": "Core area",
  "design__core__bbox": "Core box",
  "design__instance__utilization": "Core utilization",
  "design__instance__utilization__stdcell": "Core utilization (std cells)",
  "design__instance__count": "Instances (all)",
  "design__instance__area": "Instance area (all, incl. fill)",
  "design__instance__count__stdcell": "Standard cells",
  "design__instance__area__stdcell": "Standard-cell area",
  "design__instance__count__macros": "Macros",
  "design__instance__area__macros": "Macro area",
  "design__instance__count__padcells": "Pad cells",
  "design__instance__area__padcells": "Pad-cell area",
  "design__instance__count__cover": "Cover cells",
  "design__instance__area__cover": "Cover-cell area",
  "design__instance__count__setup_buffer": "Setup-repair buffers",
  "design__instance__count__hold_buffer": "Hold-repair buffers",
  "design__instance__displacement__max": "Max placement displacement",
  "design__instance__displacement__mean": "Mean placement displacement",
  "design__instance__displacement__total": "Total placement displacement",
  "design__io": "I/O pins",
  "design__io__hpwl": "I/O half-perimeter wirelength",
  "floorplan__design__io": "I/O pins (floorplan)",
  "design__rows": "Placement rows",
  "design__sites": "Placement sites",
  "design__lint_error__count": "Lint errors",
  "design__lint_warning__count": "Lint warnings",
  "design__lint_timing_construct__count": "Timing constructs in RTL",
  "design__inferred_latch__count": "Inferred latches",
  "design__instance_unmapped__count": "Unmapped cells",
  "synthesis__check_error__count": "Synthesis check errors",
  "design__disconnected_pin__count": "Disconnected pins",
  "design__critical_disconnected_pin__count": "Critical disconnected pins",
  "design__lvs_error__count": "LVS errors",
  "design__lvs_device_difference__count": "LVS device differences",
  "design__lvs_net_difference__count": "LVS net differences",
  "design__lvs_property_fail__count": "LVS property failures",
  "design__lvs_unmatched_device__count": "LVS unmatched devices",
  "design__lvs_unmatched_net__count": "LVS unmatched nets",
  "design__lvs_unmatched_pin__count": "LVS unmatched pins",
  "design__xor_difference__count": "XOR differences (Magic vs KLayout GDS)",
  "design__power_grid_violation__count": "Power-grid violations",
  "design__violations": "Design violations",
  "magic__drc_error__count": "DRC errors (Magic)",
  "magic__illegal_overlap__count": "Illegal overlaps (Magic)",
  "klayout__drc_error__count": "DRC errors (KLayout)",
  "route__drc_errors": "Routing DRC errors",
  "route__antenna_violation__count": "Antenna violations",
  "antenna__violating__nets": "Antenna-violating nets",
  "antenna__violating__pins": "Antenna-violating pins",
  "antenna_diodes_count": "Antenna diodes inserted",
  "route__wirelength": "Routed wirelength",
  "route__wirelength__estimated": "Estimated wirelength (placement)",
  "route__wirelength__max": "Longest net",
  "global_route__wirelength": "Global-route wirelength",
  "global_route__vias": "Global-route vias",
  "route__vias": "Vias",
  "route__vias__singlecut": "Single-cut vias",
  "route__vias__multicut": "Multi-cut vias",
  "route__net": "Routed nets",
  "route__net__special": "Special (power) nets",
  "power__total": "Total power",
  "power__internal__total": "Internal power",
  "power__switching__total": "Switching power",
  "power__leakage__total": "Leakage power",
  "ir__drop__worst": "Worst IR drop",
  "ir__drop__avg": "Average IR drop",
  "ir__voltage__worst": "Worst supply voltage",
  "design_powergrid__drop__worst": "Worst IR drop (power grid)",
  "design_powergrid__drop__average": "Average voltage (filed by the tool as drop__average)",
  "design_powergrid__voltage__worst": "Worst voltage (power grid)",
  "flow__errors__count": "Flow errors",
  "flow__warnings__count": "Flow warnings",
  "flow__warnings__type_count": "Warning types",
};

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

const PROC = { ss: "Slow-slow", ff: "Fast-fast", tt: "Typical", sf: "Slow-fast", fs: "Fast-slow" };

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

// Compact one-line form for charts: "SS 100 °C 1.60 V · max RC".
export function cornerShort(name) {
  const c = parseCorner(name);
  const bits = [c.proc ? c.proc.toUpperCase() : "", c.temp || "", c.volt || ""].filter(Boolean).join(" ");
  if (!bits) return name;
  return bits + (c.rc ? " · " + c.rc + " RC" : "");
}

// Long form for tips: "Slow-slow process · 100 °C · 1.60 V · max wire RC".
export function cornerLabel(name) {
  const c = parseCorner(name);
  const bits = [];
  if (c.proc) bits.push(PROC[c.proc] || c.proc.toUpperCase());
  if (c.temp) bits.push(c.temp);
  if (c.volt) bits.push(c.volt);
  if (c.rc) bits.push(c.rc + " RC");
  return bits.length >= 2 ? bits.join(" · ") : name;
}

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
  { group: "Timing", label: "Setup", keys: ["timing__setup_vio__count"], tip: "Paths that arrive too late for the clock edge, in any corner." },
  { group: "Timing", label: "Hold", keys: ["timing__hold_vio__count"], tip: "Paths that change too soon after the clock edge, in any corner." },
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
  const m = meta && meta.get && (meta.get(key) || meta.get(parseKey(key).base));
  if (m && typeof m.higher_is_better === "boolean") return m.higher_is_better ? 1 : -1;
  return LANEX_DIRECTIONS[key] || LANEX_DIRECTIONS[parseKey(key).base] || 0;
}
