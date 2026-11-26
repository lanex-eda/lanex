// projectcard.js — the Design row on the Setup page's project card: top
// module, clock port and clock period. They are read from the project's config
// file and saved straight back to it (key by key, server side), so what this
// row shows is what LibreLane runs, from LanEx or from a terminal.

import { api, fmt } from "./api.js";
import { state } from "./state.js";
import { toast } from "./toast.js";
import { applyingEntry, sectionWho, writeSetting } from "./configscope.js";
import { mountClockField, lastRunLine } from "./clockfield.js";

let _info = null;
let _seq = 0;
const $ = (id) => document.getElementById(id);

function mhz(ns) {
  const n = parseFloat(ns);
  if (!(n > 0)) return "";
  const f = 1000 / n;
  return (f >= 100 ? f.toFixed(0) : f.toFixed(1).replace(/\.0$/, "")) + " MHz";
}

function option(value, label, selected) {
  return "<option value='" + fmt.escape(value) + "'" + (selected ? " selected" : "") + ">" + fmt.escape(label) + "</option>";
}

function status(html, kind) {
  const el = $("pd-status");
  if (!el) return;
  el.innerHTML = html || "";
  el.className = "pd-status" + (kind ? " is-" + kind : "");
}

const NAMES = { DESIGN_NAME: "top module", CLOCK_PORT: "clock port", CLOCK_PERIOD: "clock period" };

// The value a run builds with: the most specific pdk::/scl:: section that
// applies to the picked PDK wins over the top level, as in LibreLane. The
// field shows it, and an edit goes there (configscope.js asks where).
export function effectiveValue(top, entries) {
  const hit = applyingEntry(entries);
  return hit ? hit.value : top;
}

// Where a value comes from when a section sets it, next to the field, so an
// edit never looks ignored.
function scopedNotes(info) {
  const out = [];
  for (const [key, entries] of Object.entries(info.scoped || {})) {
    const hit = applyingEntry(entries);
    for (const e of entries) {
      if (e.applies === false) continue;
      const v = key === "CLOCK_PERIOD" ? e.value + " ns" : String(e.value);
      if (hit && e.scope === hit.scope) {
        out.push("The " + NAMES[key] + " here comes from the <code>" + fmt.escape(e.scope) + "</code> section of " +
          fmt.escape(info.config.name) + " (<b>" + fmt.escape(v) + "</b> for " + fmt.escape(sectionWho(e)) +
          "). A change asks whether it is for " + fmt.escape(sectionWho(e)) + " only or for every PDK.");
      } else if (!e.applies) {
        out.push("The <code>" + fmt.escape(e.scope) + "</code> section of " + fmt.escape(info.config.name) +
          " sets the " + NAMES[key] + " to <b>" + fmt.escape(v) + "</b> for matching PDKs.");
      }
    }
  }
  return out;
}

function overrideNotes() {
  const v = state.varsValues || {};
  const out = [];
  if (v.CLOCK_PERIOD !== undefined && v.CLOCK_PERIOD !== null && String(v.CLOCK_PERIOD).trim() !== "") {
    out.push("A one-off setting (All settings) replaces the clock period for runs from LanEx: <b>" + fmt.escape(v.CLOCK_PERIOD) + " ns</b>.");
  }
  return out;
}

export async function paintDesignRow() {
  const box = $("proj-design");
  if (!box) return;
  // The Settings card reads the same config; keep it in step.
  import("./mostused.js").then((m) => m.paintSettings()).catch(() => {});
  if (!state.designDir) { _info = null; return; }
  const seq = ++_seq;
  let info;
  try {
    info = await api.designInfo({
      files: (state.selectedFiles || []).filter((f) => /\.(s?v|verilog)$/i.test(f)),
      pdk: state.selectedPdk || null, scl: state.selectedScl || null,
    });
  } catch (ex) {
    if (seq === _seq) status("Couldn't read the design: " + fmt.escape(ex.message || ex), "warn");
    return;
  }
  if (seq !== _seq) return;
  _info = info;
  // What a run builds with for the picked PDK (a pdk::/scl:: section wins).
  const vals = {};
  for (const k of Object.keys(NAMES)) vals[k] = effectiveValue((info.values || {})[k], (info.scoped || {})[k]);
  const editable = !!(info.config && info.config.editable);

  // Top module: the modules in the ticked files; the config's value stays
  // selectable (and flagged) even when no ticked file declares it.
  const top = typeof vals.DESIGN_NAME === "string" ? vals.DESIGN_NAME : "";
  const mods = (info.modules || []).slice();
  let topHtml = mods.map((m) => option(m, m, m === top)).join("");
  if (top && !mods.includes(top)) topHtml = option(top, top + " (not in the ticked files)", true) + topHtml;
  if (!top) topHtml = option("", mods.length ? "Choose…" : "No modules found", true) + topHtml;
  $("pd-top").innerHTML = topHtml;

  // Clock: the top module's inputs (single-bit first), or no clock at all.
  const cp = vals.CLOCK_PORT;
  const multi = Array.isArray(cp) && cp.length > 1;
  const clock = Array.isArray(cp) ? (cp[0] || "") : (cp || "");
  const inputs = (info.ports || []).filter((p) => p.dir === "input");
  inputs.sort((a, b) => Number(a.bus) - Number(b.bus));
  let clkHtml = inputs.map((p) => option(p.name, p.name + (p.bus ? " (bus)" : ""), p.name === clock)).join("");
  if (clock && !inputs.some((p) => p.name === clock)) clkHtml = option(clock, clock + " (not a port of the top)", true) + clkHtml;
  clkHtml += option("", "No clock (combinational)", !clock);
  $("pd-clock").innerHTML = clkHtml;

  const per = $("pd-period");
  // The clock helper (clockfield.js): ns or MHz, the other unit beside it.
  if (per._clock) per._clock.set(vals.CLOCK_PERIOD);
  else { per.value = vals.CLOCK_PERIOD != null ? vals.CLOCK_PERIOD : ""; $("pd-mhz").textContent = mhz(per.value); }
  paintLastClock(vals.CLOCK_PERIOD);
  // Each value's line in the config file, one click away.
  import("./provenance.js").then((m) => m.paintConfigSrc(box, (k) => vals[k] !== undefined && vals[k] !== null && vals[k] !== "")).catch(() => {});

  $("pd-top").disabled = !editable;
  $("pd-clock").disabled = !editable || multi;
  per.disabled = !editable;

  const notes = [];
  if (!editable) notes.push(fmt.escape(info.config.reason || "LanEx can't edit this config."));
  if (multi) notes.push("This design has several clocks (" + fmt.escape(cp.join(", ")) + "). Change them in " + fmt.escape(info.config.name) + ".");
  if (top && !mods.includes(top)) notes.push("No ticked file declares <code>" + fmt.escape(top) + "</code>. Tick its file below or choose another top module.");
  if (clock && !inputs.some((p) => p.name === clock) && inputs.length) notes.push("<code>" + fmt.escape(clock) + "</code> isn't an input of the top module.");
  notes.push(...scopedNotes(info), ...overrideNotes());
  status(notes.join("<br>"), notes.length ? "warn" : "");

  // The file that declares the top gets a "top" tag in Sources.
  const file = (info.module_files || {})[top] || null;
  import("./fs.js").then((m) => m.setTopFile(file)).catch(() => {});
}

// How the newest finished run did at its clock, as the period field's hover
// text and, when it missed, a line under the card (an estimate, said so).
let _lastSeq = 0;
async function paintLastClock(period) {
  const small = $("pd-mhz");
  if (!small || !state.designDir) return;
  const seq = ++_lastSeq;
  let t = null;
  try { t = (await (await import("./fppreview.js")).estimateInputs()).timing; } catch (_e) { t = null; }
  if (seq !== _lastSeq) return;
  const line = t ? lastRunLine(t.tag, t.clock_period, t.setup_ws) : "";
  small.title = line;
  let miss = document.getElementById("pd-clock-last");
  if (t && t.setup_ws < 0 && line) {
    if (!miss) {
      miss = document.createElement("p");
      miss.id = "pd-clock-last";
      miss.className = "pd-status is-warn pd-clock-last";
      $("pd-status")?.after(miss);
    }
    miss.textContent = "Last run: " + line;
  } else if (miss) miss.remove();
}

// Save one key where it takes effect for the picked PDK: the top level, or the
// pdk::/scl:: section that wins for it (asked once: that section only, or
// every PDK). A one-off setting of the same key is dropped (configscope.js).
async function save(set, remove, what) {
  const key = Object.keys(set)[0] || (remove || [])[0];
  try {
    const r = await writeSetting({
      key, value: set[key], remove: !(key in set), label: what,
      entries: ((_info && _info.scoped) || {})[key] || [], cfgName: (_info && _info.config && _info.config.name) || "the config",
    });
    if (r) document.dispatchEvent(new CustomEvent("g:config_changed"));
  } catch (ex) {
    toast.show("Couldn't save the " + what + ": " + (ex.message || ex), "error", 7000);
  }
  await paintDesignRow();
}

export function wireDesignRow() {
  $("pd-top")?.addEventListener("change", (e) => {
    if (e.target.value) save({ DESIGN_NAME: e.target.value }, [], "top module");
  });
  $("pd-clock")?.addEventListener("change", (e) => {
    const v = e.target.value;
    if (v) save({ CLOCK_PORT: v }, [], "clock port");
    else save({}, ["CLOCK_PORT"], "clock setting");
  });
  const per = $("pd-period");
  // One field for the period or the frequency (clockfield.js); the config
  // always gets ns.
  if (per) mountClockField(per, {
    unitEl: per.parentElement && per.parentElement.querySelector("em"), otherEl: $("pd-mhz"),
    onCommit: (ns, err) => {
      if (!(ns > 0)) {
        toast.show(err && err !== "empty" ? err : "The clock period is a positive number of nanoseconds, such as 10 (100 MHz).", "warn");
        paintDesignRow();
        return;
      }
      save({ CLOCK_PERIOD: ns }, [], "clock period");
    },
  });
  // Anything else that changes the config (Sources ticks, a new SDC) can
  // change the modules or the scoped values shown here.
  document.addEventListener("g:config_changed", () => {
    import("./config.js").then((m) => m.renderConfig()).catch(() => {});
    import("./preflight.js").then((m) => m.renderPreflight()).catch(() => {});
  });
}
