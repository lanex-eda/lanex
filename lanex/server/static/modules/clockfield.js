// clockfield.js — the clock helper: one field that takes the clock as a
// period or a frequency, wherever the clock is typed (Setup's project card,
// the new-project wizard).
//
// The config always stores CLOCK_PERIOD in ns, as LibreLane wants. The field
// shows ns or MHz (the small unit button next to it switches, and the choice
// is remembered, prefs.js "clockUnit"); the other unit is always shown beside
// it. Typing a unit converts: "100 MHz", "100M", "2.5 GHz", "8 ns". Up and
// Down step 0.5 ns or 5 MHz (Shift: ×10).
//
//   parseClock(text, unit) -> {ns} | {error}     pure (tested)
//   periodText(ns), freqText(ns)                 pure (tested)
//   mountClockField(input, {unitEl, otherEl, onCommit})

import { safeStorage } from "./state.js";
import { mhz } from "./metricfmt.js";

const KEY = "ll.clockUnit";

export function round4(x) {
  if (!Number.isFinite(x)) return x;
  const p = Math.pow(10, 4 - Math.ceil(Math.log10(Math.abs(x) || 1)));
  return Math.round(x * p) / p;
}

// "100 MHz" -> {ns: 10}; "10" in ns mode -> {ns: 10}; "10" in MHz mode -> {ns: 100}.
export function parseClock(text, unit = "ns") {
  const s = String(text == null ? "" : text).trim().replace(",", ".").toLowerCase();
  if (!s) return { error: "empty" };
  const m = s.match(/^(\d+(?:\.\d*)?|\.\d+)\s*(ns|ps|us|µs|ghz|mhz|khz|hz|g|m|k)?$/);
  if (!m) return { error: "A clock is a number: a period such as 10 (ns), or a frequency such as 100 MHz." };
  const v = parseFloat(m[1]);
  const u = m[2] || (unit === "mhz" ? "mhz" : "ns");
  if (!(v > 0)) return { error: "The clock has to be above 0." };
  const ns = { ns: v, ps: v / 1000, us: v * 1000, "µs": v * 1000, ghz: 1 / v, g: 1 / v, mhz: 1000 / v, m: 1000 / v,
    khz: 1e6 / v, k: 1e6 / v, hz: 1e9 / v }[u];
  return { ns: round4(ns), unit: /hz|^[gmk]$/.test(u) ? "mhz" : "ns" };
}

const trim = (x) => String(round4(x)).replace(/\.0+$/, "");

// "100 MHz" / "1.25 GHz" for a period in ns.
// The one period-to-frequency formatter (metricfmt.mhz), under this module's name.
export function freqText(ns) { return mhz(ns); }

export function periodText(ns) {
  const n = parseFloat(ns);
  return n > 0 ? trim(n) + " ns" : "";
}

export function unitPref() {
  return safeStorage.get(KEY, "ns") === "mhz" ? "mhz" : "ns";
}

// The same event prefs.js setPref sends, so the Settings page and every
// clock field stay in step.
function setUnitPref(u) {
  safeStorage.set(KEY, u);
  document.dispatchEvent(new CustomEvent("g:pref", { detail: { id: "clockUnit", value: u } }));
}

// The value to show in the field for `ns` in `unit`.
export function shown(ns, unit) {
  const n = parseFloat(ns);
  if (!(n > 0)) return "";
  return unit === "mhz" ? trim(1000 / n) : trim(n);
}

// Wire one clock field. `input`: the text field; `unitEl`: the element that
// shows its unit (becomes the ns/MHz switch); `otherEl`: where the other unit
// is written; `onCommit(ns)`: called with a valid period on change or Enter.
// Returns {set(ns), get() -> ns|null, unit()}.
export function mountClockField(input, { unitEl = null, otherEl = null, onCommit = null, onInput = null } = {}) {
  if (!input || input._clock) return input && input._clock;
  let unit = unitPref();
  let ns = null;
  input.type = "text";
  input.setAttribute("inputmode", "decimal");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("spellcheck", "false");
  if (!input.getAttribute("aria-describedby") && otherEl && otherEl.id) input.setAttribute("aria-describedby", otherEl.id);
  let btn = null;
  if (unitEl) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ck-unit";
    unitEl.replaceWith(btn);
    btn.addEventListener("click", () => {
      const cur = read();
      unit = unit === "mhz" ? "ns" : "mhz";
      setUnitPref(unit);
      paint(cur != null ? cur : ns);
      input.focus();
    });
  }
  const paint = (v) => {
    if (v != null) ns = v;
    input.value = ns != null ? shown(ns, unit) : "";
    if (btn) {
      btn.textContent = unit === "mhz" ? "MHz" : "ns";
      btn.title = unit === "mhz" ? "Typing a frequency. Switch to a period in ns" : "Typing a period. Switch to a frequency in MHz";
      btn.setAttribute("aria-label", btn.title);
    }
    other(ns);
  };
  const other = (v) => {
    if (!otherEl) return;
    otherEl.textContent = v > 0 ? "= " + (unit === "mhz" ? periodText(v) : freqText(v)) : "";
  };
  const read = () => {
    const r = parseClock(input.value, unit);
    return r.error ? null : r.ns;
  };
  input.addEventListener("input", () => {
    const v = read();
    other(v);
    input.classList.toggle("is-invalid", input.value.trim() !== "" && v == null);
    if (onInput) onInput(v);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const cur = read() ?? ns;
      if (!(cur > 0)) return;
      const k = (e.shiftKey ? 10 : 1) * (e.key === "ArrowUp" ? 1 : -1);
      let next;
      if (unit === "mhz") { const f = Math.max(1, 1000 / cur + 5 * k); next = 1000 / f; }
      else next = Math.max(0.1, cur + 0.5 * k);
      input.value = shown(round4(next), unit);
      input.dispatchEvent(new Event("input"));
    } else if (e.key === "Enter") {
      input.blur();
    }
  });
  input.addEventListener("change", () => {
    const r = parseClock(input.value, unit);
    if (r.error) {
      input.classList.add("is-invalid");
      input.setAttribute("aria-invalid", "true");
      if (onCommit) onCommit(null, r.error);
      return;
    }
    input.classList.remove("is-invalid");
    input.removeAttribute("aria-invalid");
    paint(r.ns);
    if (onCommit) onCommit(r.ns);
  });
  // Another field (or Settings) switched the unit: follow it.
  document.addEventListener("g:pref", (e) => {
    const d = e.detail || {};
    if (d.id !== "clockUnit") return;
    const u = d.value === "mhz" ? "mhz" : "ns";
    if (u !== unit) { unit = u; paint(ns); }
  });
  const api = { set: (v) => paint(v > 0 ? round4(parseFloat(v)) : null), get: () => read() ?? ns, unit: () => unit, other: (v) => other(v) };
  input._clock = api;
  paint(null);
  return api;
}

// A line about the last run's timing for the clock it ran at, or "". Only
// arithmetic on LibreLane's own slack, said as an estimate: `period` the
// run's clock, `ws` its worst setup slack (ns).
export function lastRunLine(tag, period, ws) {
  const p = parseFloat(period), s = parseFloat(ws);
  if (!(p > 0) || !Number.isFinite(s)) return "";
  if (s >= 0) return tag + " met " + periodText(p) + " with " + trim(s) + " ns to spare.";
  return tag + " missed " + periodText(p) + " by " + trim(-s) + " ns: about " + periodText(Math.ceil((p - s) * 10) / 10) +
    " (" + freqText(Math.ceil((p - s) * 10) / 10) + ") would meet, an estimate (synthesis and repair change with the clock).";
}
