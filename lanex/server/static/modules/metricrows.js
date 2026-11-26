// metricrows.js — the one "every metric" view (consistency plan D4, rank 9).
// Analytics' All metrics and Compare's Every metric draw the same row and the
// same filter:
//
//   - the row: the plain name from metricfmt's one list, a "critical" tag,
//     LibreLane's key in mono under it with how many corners or nets it has,
//     and the unit with LibreLane's direction (↑ higher is better);
//   - the numbers: the table precision (metricfmt.show compact: slack to
//     3 decimals, areas whole), right-aligned in mono, one unit per row;
//   - the filter: All · Problems · Changed · Worse, each with its count, the
//     Runs filter look (.rn-seg). Changed and Worse need a run to compare with.
//
// Compare keeps its column per run; Analytics its topic groups. Pure apart
// from the HTML strings: node tests import it.

import { escapeHtml } from "./html.js";
import * as MF from "./metricfmt.js";
import { cornerWords } from "./cornergrid.js";

const esc = escapeHtml;

// A count that should be 0 (violations, errors, unmatched…) and isn't, or a
// negative slack: "fail" or "warn"; "pass" for such a count at 0; "" otherwise.
// `critical(base)`: LibreLane marks the metric critical (a non-zero fails the flow).
const COUNTS = /(_vio__count|error[s]?__count|__errors$|_violation__count|drc_errors|illegal_overlap__count|unmatched_|_difference__count|disconnected_pin__count|inferred_latch__count|unmapped__count|property_fail__count|violating__)/;
export function statusOf(key, v, critical = () => false) {
  const { base } = MF.parseKey(key);
  if (COUNTS.test(base)) {
    const n = Number(v);
    if (n === 0) return "pass";
    return critical(base) || /drc_error|drc_errors/.test(base) ? "fail" : "warn";
  }
  if (/^timing__.*__(ws|wns|tns)$/.test(base) && MF.isNum(v)) return v < 0 ? "fail" : "";
  return "";
}

// The name cell. `kid`: a corner/net/iteration row under its metric.
export function nameHtml({ key = "", base = "", kid = false, critical = false, count = "", unit = "", dir = 0 } = {}) {
  base = base || MF.parseKey(key).base;
  if (kid) {
    // A corner row says its corner in words (cornergrid.js), LibreLane's name under it.
    const c = MF.parseKey(key).mods.corner, w = c ? cornerWords(c) : "";
    const raw = key.slice(base.length).replace(/^__/, "").replace(/__/g, " · ");
    return w ? "<span class='an-mlabel'>" + esc(w) + "</span><code>" + esc(raw) + "</code>" : "<span class='an-kidname'>" + esc(raw) + "</span>";
  }
  const k = key || base;
  const label = MF.metricName(k) || MF.metricName(base);
  const crit = critical ? "<span class='an-crit' data-an-tip='LibreLane marks this metric critical: a non-zero count fails the flow.' tabindex='0'>critical</span>" : "";
  const cnt = count ? "<span class='an-mcount'> · " + esc(count) + "</span>" : "";
  const ud = unit || dir ? "<small class='mr-u'" + (dir ? " title='" + esc(MF.dirWords(dir)) + "'" : "") + ">" + esc(unit) + (dir ? (unit ? " " : "") + MF.dirMark(dir) : "") + "</small>" : "";
  return label
    ? "<span class='an-mlabel'>" + esc(label) + "</span>" + crit + ud + "<code>" + esc(k) + cnt + "</code>"
    : "<code class='an-mkey'>" + esc(k) + "</code>" + crit + ud + cnt;
}

// A row's unit: one per row (power in the prefix of its largest value).
export function rowScale(key, values) { return MF.scaleOf(key, values); }
export function rowUnit(key, scale) { return scale ? scale.unit : MF.show(key, 1).unit; }
// A value at the table precision, in the row's unit (no unit in the text).
export function valueShown(key, v, scale) {
  if (v === undefined || v === null) return { text: "—", unit: "", title: "" };
  return scale ? MF.showAt(key, v, scale) : MF.show(key, v, { compact: true });
}

export const FILTERS = [
  ["all", "All", "Every metric"],
  ["problems", "Problems", "Violations, errors and negative slack"],
  ["changed", "Changed", "Metrics that differ from the run they are compared with"],
  ["worse", "Worse", "Metrics worse than the run they are compared with, by LibreLane's direction"],
];
// `counts`: { all, problems, changed, worse }; `noRef`: Changed and Worse
// have nothing to compare with.
export function filterHtml(counts, cur, { attr = "data-mf", noRef = false, label = "Show" } = {}) {
  return "<div class='rn-seg mr-filter' role='group' aria-label='" + esc(label) + "'>" + FILTERS.map(([id, l, tip]) => {
    const off = noRef && (id === "changed" || id === "worse");
    return "<button type='button' class='rn-segb" + (id === cur ? " is-on" : "") + "' " + attr + "='" + id + "' aria-pressed='" + (id === cur) + "'" +
      (off ? " disabled title='Pick a run under Compare with first'" : " title='" + esc(tip) + "'") + ">" + l +
      (off ? "" : " <span class='rn-n'>" + esc(counts[id] == null ? "" : String(counts[id])) + "</span>") + "</button>";
  }).join("") + "</div>";
}

// "9 corners", "2 nets", "3 iterations": what a metric's variants are.
export function countWords(keys) {
  if (!keys || !keys.length) return "";
  const m = MF.parseKey(keys[0]).mods, n = keys.length;
  const w = m.corner ? "corner" : m.net ? "net" : m.iter !== undefined ? "iteration" : m.class ? "cell type" : "variant";
  return n + " " + w + (n === 1 ? "" : "s");
}

// A search matches a metric's key or its plain name.
export function matches(key, q) {
  if (!q) return true;
  return key.toLowerCase().includes(q) || (MF.metricName(key) || "").toLowerCase().includes(q);
}
