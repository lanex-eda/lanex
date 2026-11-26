// cells-macros.js — the Macros part of the Cells tab.
//
// A macro is a pre-built block (an SRAM, an IP block, or a design hardened in
// another LanEx project) that sits in the floorplan as a fixed rectangle. This
// page gets one in from where it actually comes from, checks what a first
// macro usually gets wrong before a run does (not instantiated, its
// behavioural model in the sources, no timing model, power not hooked up,
// no power strap reaching it), and places it: the Arrange panel draws the die,
// the core OpenROAD will make, the site grid and the power straps to scale
// (floorplan-canvas.js) and checks every position live (macro-place.js, the
// same rules the server's run check uses). Everything reaches LibreLane
// through the .gui-macros.json overlay; the config file is never edited.

import { api } from "./api.js";
import { state } from "./state.js";
import { toast } from "./toast.js";
import { icon } from "./icons.js";
import { confirmDialog, customDialog } from "./dialog.js";
import * as M from "./cells-model.js";
import { copyText, setMacroTile } from "./cells.js";
import { createFloorplanCanvas } from "./floorplan-canvas.js";
import * as P from "./macro-place.js";
import { uploadFile, fmtBytes } from "./uploader.js";
import { forgetPreflight } from "./preflight.js";
import { emptyHtml, EMPTY_WORDS } from "./empty.js";

const esc = M.esc;
let C = null;        // context from cells.js
let D = null;        // {macros, settings, inspect, catalog, fp}
let _my = 0;
// The Arrange panel: the floorplan model, saved and edited placements, the
// selection, undo history, snap and layer switches. Kept across repaints.
const AR = { model: null, saved: [], pl: [], sel: null, hist: [], snap: "site", show: { rows: true, power: true, keepout: true, pins: true }, fp: null };

const pdkNow = () => state.selectedPdk || (C && C.pdk) || "";
const sclNow = () => state.selectedScl || (C && C.scl) || "";

export async function renderMacros(ctx) {
  C = ctx;
  const host = document.getElementById("cl-macros-body");
  if (!host) return;
  if (!state.designDir) {
    host.innerHTML = emptyHtml({ icon: "cube", title: EMPTY_WORDS.noProject.title, body: "Macros belong to a project: they are listed in its folder and used by its runs.", action: EMPTY_WORDS.noProject.action });
    setMacroTile(0, 0);
    return;
  }
  const my = ++_my;
  const pdk = pdkNow(), scl = sclNow();
  const [list, inspect, catalog, fp] = await Promise.all([
    api.customMacros(state.designDir).catch(() => ({ macros: [] })),
    api.macroInspect(state.designDir, pdk).catch((e) => ({ ok: false, error: e.message })),
    api.macroCatalog(pdk, scl).catch(() => ({ macros: [] })),
    api.macroFloorplan(state.designDir, pdk, scl).catch((e) => ({ ok: false, error: e.message })),
  ]);
  if (my !== _my) return;
  D = { macros: list.macros || [], settings: list.settings || {}, inspect: inspect || {}, catalog: (catalog && catalog.macros) || [], fp: fp || {} };
  // Every macro change ends in this repaint: a Run check made before it
  // read the old macros, so the next Run checks again.
  const sig = state.designDir + "\u0000" + JSON.stringify([D.macros, D.settings]);
  if (_sig !== null && _sig !== sig) forgetPreflight();
  _sig = sig;
  setModel(D.fp);
  paint(host);
}
let _sig = null;

function setModel(fp) {
  AR.model = fp && fp.ok ? fp : null;
  AR.saved = AR.model ? P.placementsOf(AR.model) : [];
  AR.pl = AR.saved.map((p) => Object.assign({}, p));
  AR.hist = [];
  if (!AR.pl.some((p) => p.name === AR.sel)) AR.sel = (AR.pl.find((p) => p.location) || AR.pl[0] || {}).name || null;
}

// ------------------------------------------------------------- checks -------
function checksFor(m) {
  const ins = D.inspect || {};
  const info = (ins.macros || {})[m.name] || {};
  const scan = ins.scan || {};
  const found = (scan.instances || {})[m.name] || [];
  const views = m.views || {};
  const gen = m.generated || {};
  const g = ((D.fp || {}).macros || {})[m.name] || {};
  const out = [];
  out.push({ s: info.ok ? "pass" : "fail", t: info.ok ? "Layout views: GDS and LEF, one block of " + fmtSize(info.w, info.h) + (info.gds_has_cell ? "; the GDS holds a cell of that name" : "") : "Couldn't read its LEF: " + (info.error || "missing") });
  if (info.gds_has_cell === false) out.push({ s: "fail", t: "The GDS has no cell called " + m.name + " (it has " + (info.gds_cells || []).join(", ") + "). Magic and KLayout look it up by the module name, so the layout would come out empty. Use the GDS made for this LEF, or the name the GDS uses." });
  if (scan.ok && scan.top_found) {
    const configured = (m.instances || []).map((i) => i.name);
    if (!found.length) out.push({ s: "fail", t: "Not instantiated anywhere under " + scan.top + ". Paste the template below into your RTL.", fix: "template" });
    else {
      const missing = configured.filter((n) => !found.includes(n));
      if (!configured.length) out.push({ s: "warn", t: "Found in the RTL as " + found.join(", ") + ", but no instance is listed here yet.", fix: "use-found" });
      else if (missing.length) out.push({ s: "fail", t: "Listed as " + missing.join(", ") + ", but the RTL names it " + found.join(", ") + ". The names must match the flattened instance path.", fix: "use-found" });
      else out.push({ s: "pass", t: "Instantiated as " + found.join(", ") });
    }
  } else if (scan.ok) {
    out.push({ s: "info", t: "Couldn't find the top module " + (scan.top || "(DESIGN_NAME unset)") + " in the design files, so the instance check was skipped." });
  }
  const defined = (scan.defined_in_sources || {})[m.name];
  if (defined) out.push({ s: "fail", t: shortPath(defined) + " defines this module, so synthesis would build the memory out of flip-flops instead of using the macro. Keep that file for simulation only (Sources → Simulation)." });
  // placement, from the floorplan check
  if (m.enabled !== false) {
    const probs = AR.model ? P.problems(AR.model, AR.saved).filter((p) => p.macro === m.name) : [];
    const placed = (m.instances || []).filter((i) => Array.isArray(i.location));
    for (const p of probs) out.push({ s: p.hard ? "fail" : "warn", t: p.message, fix: p.code === "unplaced" ? "arrange" : "select:" + p.inst });
    if (placed.length && !probs.some((p) => p.hard) && AR.model) {
      out.push({ s: "pass", t: "Placed inside the core: " + placed.map((i) => i.name + " at (" + P.fmtUm(+i.location[0]) + ", " + P.fmtUm(+i.location[1]) + ") µm, " + (i.orientation || "N")).join("; ") });
    }
  }
  // power
  const pw = normPower(m.power);
  if (pw) {
    out.push({ s: "pass", t: "Power hooked up: " + pw.vdd_pins.map((p) => p + " → " + pw.vdd_net).concat(pw.gnd_pins.map((p) => p + " → " + pw.gnd_net)).join(", ") });
  } else if (info.ok && ((info.power || {}).vdd || []).length) {
    out.push({ s: "fail", t: "Its power pins (" + info.power.vdd.concat(info.power.gnd).join(", ") + ") aren't hooked up. The power step only warns (PDN-0231) and LVS fails later.", fix: "power" });
  }
  if (g.ok) {
    if (g.grid === "default") {
      const c = AR.model ? (AR.saved.filter((p) => p.macro === m.name && p.location).map((p) => P.crossings(AR.model, p))) : [];
      const n = c.reduce((s, x) => s + x.power + x.ground, 0);
      if (c.length && n) out.push({ s: "pass", t: "Power reaches it: " + n + " via site" + (n === 1 ? "" : "s") + " where the " + (AR.model.pdn || {}).hlayer + " straps cross its " + (AR.model.pdn || {}).vlayer + " power pins." });
    } else if (g.grid === "lanex" && g.plan) {
      out.push({ s: "pass", t: g.plan.why, fix: "pdn" });
    } else if (g.grid === "own") {
      out.push({ s: "info", t: "Your config sets PDN_CFG, so your power grid decides how this block is powered. LanEx adds none." });
    }
  }
  // timing
  const libs = views.lib;
  if (libs || (views.nl && views.spef)) {
    const notes = ((gen.lib || {}).notes || []);
    const corners = libs && typeof libs === "object" ? Object.keys(libs) : [];
    out.push({ s: notes.length ? "warn" : "pass", t: libs ? ("Timing: " + (corners.length > 1 ? corners.length + " corners, each with its own Liberty file" : "one Liberty file") + ". " + notes.join(" ")) : "Timing: netlist and parasitics, so STA times paths through it." });
  } else {
    out.push({ s: "warn", t: "No timing model. Synthesis treats it as a black box and STA can't check paths into or out of it." });
  }
  const relaxed = ((gen.lib || {}).relaxed || []);
  if (relaxed.length) {
    const pins = [...new Set(relaxed.flatMap((r) => r.changes.map((c) => c.pin)))];
    const c0 = relaxed[0].changes[0];
    out.push({ s: "info", t: "Its Liberty file limits " + pins.join(", ") + " to a " + c0.from + " ns edge, which no standard cell can drive (the run would stop with RSZ-0090). LanEx uses a copy with that limit at the file's own default, " + c0.to + " ns. Timing arcs are unchanged.", fix: "lib-diff" });
  }
  if ((gen.vh || {}).from === "lef") out.push({ s: "pass", t: "Synthesis reads a ports-only header LanEx made from its LEF" + (views.model ? "; the vendor's behavioural model stays for simulation." : ".") });
  if (views.maglef) out.push({ s: "pass", t: "Magic checks around it, not inside it (its maker's DRC applies inside)." });
  else if (info.ok) out.push({ s: "info", t: "No Magic abstract view (maglef), so Magic's DRC also checks inside this block; a vendor block may report errors there." });
  return out;
}

function normPower(pw) {
  if (!pw || typeof pw !== "object") return null;
  const pins = (many, one) => {
    let v = pw[many] != null ? pw[many] : pw[one];
    if (typeof v === "string") v = v.split("|");
    return (v || []).map((x) => String(x).trim()).filter(Boolean);
  };
  const out = { vdd_net: pw.vdd_net || "", gnd_net: pw.gnd_net || "", vdd_pins: pins("vdd_pins", "vdd_pin"), gnd_pins: pins("gnd_pins", "gnd_pin") };
  return out.vdd_net && out.gnd_net && out.vdd_pins.length && out.gnd_pins.length ? out : null;
}

function hookLines(inst, pw) {
  const p = normPower(pw);
  if (!p) return [];
  const rx = inst.replace(/([.[\]])/g, "\\$1");
  const out = p.vdd_pins.map((v, k) => rx + " " + p.vdd_net + " " + p.gnd_net + " " + v + " " + p.gnd_pins[Math.min(k, p.gnd_pins.length - 1)]);
  for (const g of p.gnd_pins.slice(p.vdd_pins.length)) out.push(rx + " " + p.vdd_net + " " + p.gnd_net + " " + p.vdd_pins[0] + " " + g);
  return out;
}

function shortPath(p) {
  const d = (state.designDir || "").replace(/\/+$/, "");
  return p.startsWith(d + "/") ? p.slice(d.length + 1) : p;
}
function fmtSize(w, h) { return Number.isFinite(w) ? P.fmtUm(w) + " × " + P.fmtUm(h) + " µm" : "size unknown"; }

// -------------------------------------------------------------- paint -------
function paint(host) {
  if (AR.fp) { AR.fp.destroy(); AR.fp = null; }
  const ms = D.macros;
  const attention = ms.filter((m) => m.enabled !== false && checksFor(m).some((c) => c.s === "fail" || c.s === "warn")).length;
  setMacroTile(ms.length, attention);
  const addBtn = "<button class='btn btn-primary btn-sm' data-mc-add=''>" + icon("plus", { size: 14 }) + " Add a macro</button>";
  if (!ms.length) {
    const n = D.catalog.length;
    host.innerHTML =
      "<div class='mc-sources'>" +
      tile("pdk", "database", "Memory from the PDK", n ? n + " SRAM" + (n === 1 ? "" : "s") + " ship with " + esc(pdkNow()) + ". Pick one by size; nothing is copied." : "This PDK ships no SRAM macros.", !n) +
      tile("run", "box", "A block from another project", "Harden a sub-block on its own, then drop its finished run in here. The hierarchical flow, without the copying by hand.") +
      tile("files", "upload", "Files you were given", "A vendor or a teammate handed you GDS + LEF (+ LIB). Drop them in.") +
      "</div>";
    wireCards(host);
    return;
  }
  const inst = ms.reduce((s, m) => s + (m.instances || []).length, 0);
  host.innerHTML =
    "<div class='mc-bar'><span class='mc-count'><b>" + ms.length + "</b> macro" + (ms.length === 1 ? "" : "s") + " · <b>" + inst + "</b> instance" + (inst === 1 ? "" : "s") + " · " + esc(pdkNow()) + "</span>" +
      "<span class='bar-spacer'></span>" + addBtn + overlayBtn() + "</div>" +
    "<div class='mc-grid'><div class='mc-list'>" + ms.map(cardHtml).join("") + "</div>" +
    "<section class='cl-card mc-arr' id='mc-arrange' aria-label='Arrange the macros'>" + arrangeHtml() + "</section></div>";
  wireCards(host);
  mountArrange(host);
}

function tile(kind, ic, title, body, disabled) {
  return "<button class='mc-src' data-mc-add='" + kind + "'" + (disabled ? " disabled" : "") + "><span class='mc-src-ic'>" + icon(ic, { size: 22 }) + "</span>" +
    "<b>" + esc(title) + "</b><span>" + esc(body) + "</span></button>";
}

function overlayBtn() {
  return "<button class='btn btn-ghost btn-sm' data-mc-overlay title='The exact config file LanEx hands LibreLane for the macros'>" + icon("file", { size: 13 }) + " .gui-macros.json</button>";
}

function cardHtml(m) {
  const info = ((D.inspect || {}).macros || {})[m.name] || {};
  const checks = checksFor(m);
  const worst = checks.some((c) => c.s === "fail") ? "fail" : checks.some((c) => c.s === "warn") ? "warn" : "pass";
  const src = m.source || {};
  const cap = (D.catalog.find((x) => x.name === m.name) || {}).capacity;
  const views = Object.keys(m.views || {}).map((k) => "<span class='cc-chip'>" + esc(k) + "</span>").join(" ");
  const inst = (m.instances || []);
  const instRows = inst.map((i, k) => instRow(i, k)).join("");
  const pw = normPower(m.power);
  return "<article class='mc-card is-" + (m.enabled === false ? "off" : worst) + "' data-macro='" + esc(m.name) + "'>" +
    "<header class='mc-head'>" +
      "<div><div class='an-eyebrow'>" + esc(src.kind === "pdk" ? "PDK macro" : src.kind === "run" ? "From another project" : "Uploaded") + (src.label ? " · " + esc(src.label) : "") + "</div>" +
      "<h4 class='mc-name'>" + esc(m.name) + "</h4>" +
      "<div class='mc-meta'>" + (info.ok ? fmtSize(info.w, info.h) + " · " + M.fmtArea(info.w * info.h) : "") +
        (cap && cap.words ? " · " + cap.words + " words × " + cap.bits + " bits" + (cap.kbits ? " (" + (cap.kbits >= 8 ? (cap.kbits / 8) + " KB" : cap.kbits + " kbit") + ")" : "") : "") + "</div></div>" +
      "<div class='mc-head-act'><label class='cl-switch' title='Use this macro in the next run'><input type='checkbox' data-mc-enable" + (m.enabled !== false ? " checked" : "") + "><span></span> Use</label>" +
      "<button class='btn btn-ghost btn-sm' data-mc-remove title='Remove this macro (and the files LanEx made or copied for it)' aria-label='Remove " + esc(m.name) + "'>" + icon("trash", { size: 13 }) + "</button></div>" +
    "</header>" +
    "<ul class='mc-checks'>" + checks.map((c) => "<li class='is-" + c.s + "'><span class='vf-mark is-" + c.s + "'></span><span>" + esc(c.t) + "</span>" + fixBtn(c) + "</li>").join("") + "</ul>" +
    "<div class='mc-views'>Views " + views + "</div>" +
    "<div class='mc-inst'><div class='mc-inst-h'>Instances <span class='muted'>The instance path in your RTL. Positions are set on Arrange.</span></div>" +
      (instRows ? "<div class='mc-inst-rows'>" + instRows + "</div>" : "<p class='muted cl-p'>None yet.</p>") +
      "<div class='mc-inst-act'><button class='btn btn-ghost btn-xs' data-mc-addinst>" + icon("plus", { size: 12 }) + " Instance</button>" +
      "<button class='btn btn-sm btn-primary' data-mc-save hidden>Save names</button></div></div>" +
    (pw ? "<div class='mc-power'><div class='mc-power-h'>Power hookup <button class='btn btn-ghost btn-xs' data-mc-power>Change</button></div>" +
      "<table class='mc-pw-tbl'><tbody>" +
      pw.vdd_pins.map((p) => "<tr><td class='mono'>" + esc(p) + "</td><td class='muted'>power</td><td>→</td><td class='mono'>" + esc(pw.vdd_net) + "</td></tr>").join("") +
      pw.gnd_pins.map((p) => "<tr><td class='mono'>" + esc(p) + "</td><td class='muted'>ground</td><td>→</td><td class='mono'>" + esc(pw.gnd_net) + "</td></tr>").join("") +
      "</tbody></table>" +
      (inst.length ? "<div class='mc-pw-lines'><span class='muted'>Written as <code>PDN_MACRO_CONNECTIONS</code>:</span><pre class='mono'>" + esc(inst.flatMap((i) => hookLines(i.name, pw)).join("\n")) + "</pre></div>" : "") + "</div>" : "") +
    (info.template ? "<details class='mc-tpl'><summary>Instantiation template</summary><pre class='mono'>" + esc(info.template) + "</pre>" +
      "<div class='mc-tpl-act'><button class='btn btn-ghost btn-xs' data-mc-copytpl>Copy</button>" +
      (D.inspect.top ? "<button class='btn btn-ghost btn-xs' data-mc-opentop>Open " + esc(D.inspect.top) + " in the RTL IDE</button>" : "") +
      "<span class='muted'>Leave the power pins out of your RTL: LanEx hooks them to the grid with <code>PDN_MACRO_CONNECTIONS</code>.</span></div></details>" : "") +
    "</article>";
}

function fixBtn(c) {
  const b = (attr, label) => " <button class='btn btn-ghost btn-xs' " + attr + ">" + esc(label) + "</button>";
  if (c.fix === "use-found") return b("data-mc-usefound", "Use the RTL's names");
  if (c.fix === "power") return b("data-mc-power", "Hook up");
  if (c.fix === "template") return b("data-mc-copytpl", "Copy template");
  if (c.fix === "arrange") return b("data-mc-arrange", "Arrange for me");
  if (c.fix === "lib-diff") return b("data-mc-libdiff", "See the change");
  if (c.fix === "pdn") return b("data-mc-pdn", "View the grid file");
  if (c.fix && c.fix.startsWith("select:")) return b("data-mc-show='" + esc(c.fix.slice(7)) + "'", "Show on Arrange");
  return "";
}

function instRow(i, k) {
  const loc = Array.isArray(i.location) ? "(" + P.fmtUm(+i.location[0]) + ", " + P.fmtUm(+i.location[1]) + ") µm · " + esc(i.orientation || "N") : "<span class='cl-warn-t'>no position</span>";
  return "<div class='mc-irow' data-k='" + k + "'>" +
    "<input class='mc-iname mono' value='" + esc(i.name) + "' aria-label='Instance path' spellcheck='false'>" +
    "<span class='mc-iloc mono'>" + loc + "</span>" +
    "<button class='btn btn-ghost btn-xs' data-mc-delinst title='Remove this instance' aria-label='Remove instance " + esc(i.name) + "'>" + icon("x", { size: 12 }) + "</button></div>";
}

// --------------------------------------------------------------- arrange ----
function arrangeHtml() {
  const fp = D.fp || {};
  const head = "<div class='cl-card-h'>Arrange <span class='muted'>to scale, on the site grid</span></div>";
  if (!fp.ok) {
    return head + "<p class='cl-p'><span class='vf-mark is-fail'></span> " + esc(fp.error || "LanEx couldn't read the floorplan.") + "</p>" +
      (/not installed/i.test(fp.error || "") ? "<button class='btn btn-sm' data-go='tools'>Open Tools</button>" : "");
  }
  const die = fp.die || {}, core = fp.core || {};
  const hh = (fp.halo || {}).h, hv = (fp.halo || {}).v;
  const keep = "<div class='mc-ko'><span class='an-eyebrow'>Keep-out</span>" +
    "<label>left / right <input id='mc-hh' inputmode='decimal' class='mono' value='" + esc(P.fmtUm(hh)) + "' aria-label='Keep-out left and right, µm'> µm</label>" +
    "<label>top / bottom <input id='mc-hv' inputmode='decimal' class='mono' value='" + esc(P.fmtUm(hv)) + "' aria-label='Keep-out top and bottom, µm'> µm</label>" +
    "<button class='btn btn-ghost btn-xs' id='mc-halo-save' hidden>Save</button>" +
    "<span class='muted has-tip' title='Sets FP_MACRO_HORIZONTAL_HALO / FP_MACRO_VERTICAL_HALO (no rows or taps) and PDN_HORIZONTAL_HALO / PDN_VERTICAL_HALO (no power straps) to the same value, so no strip of rows is left that the power grid can&#39;t reach.'>No cells, taps or power straps go there, so the pins stay reachable.</span></div>";
  if (!die.box) {
    const sd = fp.suggest_die;
    return head + "<div class='mc-arr-empty'><p class='cl-p'>The flow sizes this die from your logic (<code>FP_SIZING</code> relative), so there's no core to place against yet. Designs with macros use a fixed die.</p>" +
      (sd ? "<button class='btn btn-primary btn-sm' data-mc-setdie>Use a fixed die of " + sd[0] + " × " + sd[1] + " µm</button>" +
        "<p class='cl-foot'>Sized for the macros, their keep-out, the cells and the core margins. Saved to your config as <code>FP_SIZING: absolute</code> and <code>DIE_AREA</code>; edit it on Setup.</p>" :
        "<p class='cl-foot'>Add an instance to a macro first, so LanEx can size a die around it.</p>") + "</div>" + keep;
  }
  const W = die.box[2] - die.box[0], H = die.box[3] - die.box[1];
  const coreTip = core.from === "CORE_AREA" ? "CORE_AREA in your config, snapped to whole sites and rows the way the floorplan step does."
    : core.from === "run" ? "The core of run " + (die.tag || "") + "."
    : "The die minus " + P.fmtUm(fp.margins.LEFT_MARGIN_MULT) + " sites left and right and " + P.fmtUm(fp.margins.BOTTOM_MARGIN_MULT) + " rows top and bottom, snapped to whole sites and rows (what OpenROAD's floorplan makes).";
  const facts = "<div class='mc-arr-facts'>" +
    "<span class='mc-fact has-tip' title='" + esc(die.from === "config" ? "FP_SIZING: absolute, DIE_AREA in " + (fp.config || "your config") : "Relative sizing: the die of run " + (die.tag || "") + ". The next run can size it differently.") + "'><span class='an-eyebrow'>Die</span> <b class='mono'>" + P.fmtUm(W) + " × " + P.fmtUm(H) + "</b> µm <span class='muted'>" + (die.from === "config" ? "fixed" : "from run " + esc(die.tag || "")) + "</span></span>" +
    (core.box ? "<span class='mc-fact has-tip' title='" + esc(coreTip) + "'><span class='an-eyebrow'>Core</span> <b class='mono'>" + P.fmtUm(core.box[2] - core.box[0]) + " × " + P.fmtUm(core.box[3] - core.box[1]) + "</b> µm</span>" : "") +
    ((fp.site || {}).w ? "<span class='mc-fact has-tip' title='Rows of " + esc((fp.site || {}).name || "sites") + ". Macro corners snap to this grid.'><span class='an-eyebrow'>Site</span> <b class='mono'>" + P.fmtUm(fp.site.w) + " × " + P.fmtUm(fp.site.h) + "</b> µm</span>" : "") +
    ((fp.pdn || {}).vlayer ? "<span class='mc-fact'><span class='an-eyebrow'>Power</span> <span class='mc-sw is-v'></span>" + esc(fp.pdn.vlayer) + " <span class='mc-sw is-h'></span>" + esc(fp.pdn.hlayer) + "</span>" : "") +
    "</div>";
  const relWarn = die.from === "run" ? "<p class='mc-arr-note'><span class='vf-mark is-warn'></span> The flow sizes this die from your logic, so this is the die of the last run, and the next run may make it bigger or smaller. <button class='btn btn-ghost btn-xs' data-mc-setdie>Fix the die at " + P.fmtUm(W) + " × " + P.fmtUm(H) + " µm</button></p>" : "";
  const unplaced = AR.pl.filter((p) => !p.location).length;
  const tools = "<div class='mc-arr-tools' role='toolbar' aria-label='Arrange tools'>" +
    "<div class='rn-seg' role='group' aria-label='Zoom'>" +
      "<button class='rn-segb' data-ar='fit' title='Fit the die (0)'>" + icon("expand", { size: 13 }) + " Fit</button>" +
      "<button class='rn-segb' data-ar='out' title='Zoom out (−)' aria-label='Zoom out'>−</button>" +
      "<button class='rn-segb' data-ar='in' title='Zoom in (+)' aria-label='Zoom in'>+</button></div>" +
    "<div class='rn-seg' role='radiogroup' aria-label='Snap to'>" +
      "<button class='rn-segb" + (AR.snap === "site" ? " is-on" : "") + "' data-snap='site' role='radio' aria-checked='" + (AR.snap === "site") + "' title='Snap to the site grid (" + P.fmtUm((fp.site || {}).w) + " × " + P.fmtUm((fp.site || {}).h) + " µm)'>Sites</button>" +
      "<button class='rn-segb" + (AR.snap === "mfg" ? " is-on" : "") + "' data-snap='mfg' role='radio' aria-checked='" + (AR.snap === "mfg") + "' title='Snap to the manufacturing grid (" + P.fmtUm((fp.mfg || 0.005) * 1000) + " nm). Hold Alt while dragging for the same.'>" + P.fmtUm((fp.mfg || 0.005) * 1000) + " nm</button></div>" +
    "<div class='mc-arr-show' role='group' aria-label='Show'>" +
      ["rows:Rows", "power:Power", "keepout:Keep-out", "pins:Pins"].map((x) => {
        const [k, l] = x.split(":");
        return "<label class='mc-chk'><input type='checkbox' data-show='" + k + "'" + (AR.show[k] ? " checked" : "") + "> " + l + "</label>";
      }).join("") + "</div>" +
    "<span class='bar-spacer'></span>" +
    "<button class='btn btn-sm" + (unplaced ? " btn-primary" : "") + "' data-ar='arrange'>" + icon("layers", { size: 13 }) + (unplaced ? " Arrange for me" : " Arrange again") + "</button></div>";
  return head + facts + relWarn + tools +
    "<div class='mc-arr-body'><div class='mc-arr-canvas' id='mc-canvas'></div><aside class='mc-insp' id='mc-insp' aria-live='polite'></aside></div>" +
    "<div class='mc-arr-status'><span class='mono muted' id='mc-cursor'>&nbsp;</span><span class='mc-arr-sum' id='mc-sum'></span><span class='bar-spacer'></span>" +
      "<button class='btn btn-ghost btn-sm' data-ar='undo' id='mc-undo' disabled>Undo</button>" +
      "<button class='btn btn-primary btn-sm' data-ar='save' id='mc-save' disabled>Save positions</button></div>" + keep;
}

function mountArrange(host) {
  const el = host.querySelector("#mc-canvas");
  if (!el || !AR.model || !(AR.model.die || {}).box) { wireArrange(host); return; }
  AR.fp = createFloorplanCanvas(el, {
    editable: true,
    onChange: (pl, _probs, { final }) => {
      if (final) pushHist();
      AR.pl = pl;
      refreshSide();
    },
    onSelect: (name) => { AR.sel = name; refreshSide(); },
    onEnter: () => { document.getElementById("mc-ix")?.focus(); },
    onCursor: (x, y) => {
      const c = document.getElementById("mc-cursor");
      const s = AR.model.site || {};
      if (c) c.textContent = "x " + x.toFixed(2) + "  y " + y.toFixed(2) + " µm" + (s.w && AR.model.core.box ? "  ·  row " + Math.floor((y - AR.model.core.box[1]) / s.h) + ", site " + Math.floor((x - AR.model.core.box[0]) / s.w) : "");
    },
  });
  AR.fp.setSnap(AR.snap);
  AR.fp.setShow(AR.show);
  AR.fp.set(AR.model, AR.pl);
  if (AR.sel) AR.fp.select(AR.sel);
  wireArrange(host);
  refreshSide();
}

let _histLock = false;
function pushHist() {
  if (_histLock) return;
  AR.hist.push(AR.pl.map((p) => Object.assign({}, p, { location: p.location ? p.location.slice() : null })));
  if (AR.hist.length > 100) AR.hist.shift();
}

function applyPl(pl, { hist = true } = {}) {
  if (hist) pushHist();
  AR.pl = pl;
  if (AR.fp) AR.fp.set(AR.model, AR.pl);
  refreshSide();
}

function refreshSide() {
  const insp = document.getElementById("mc-insp");
  if (!insp || !AR.model) return;
  const probs = P.problems(AR.model, AR.pl);
  const dirty = !P.samePlacements(AR.pl, AR.saved);
  const hardBlocking = probs.filter((p) => p.hard && p.code !== "unplaced");
  const save = document.getElementById("mc-save"), undo = document.getElementById("mc-undo");
  if (save) {
    save.disabled = !dirty || hardBlocking.length > 0;
    save.textContent = dirty && hardBlocking.length ? "Fix " + hardBlocking.length + " problem" + (hardBlocking.length === 1 ? "" : "s") + " to save" : "Save positions";
  }
  if (undo) undo.disabled = !AR.hist.length && !dirty;
  const sum = document.getElementById("mc-sum");
  const placed = AR.pl.filter((p) => p.location).length;
  if (sum) sum.innerHTML = placed + " of " + AR.pl.length + " placed · " +
    (probs.length ? "<span class='" + (probs.some((p) => p.hard) ? "mc-bad" : "cl-warn-t") + "'>" + probs.length + " problem" + (probs.length === 1 ? "" : "s") + "</span>" : "<span class='mc-ok'>no problems</span>") +
    (dirty ? " · <span class='cl-warn-t'>not saved</span>" : "");
  const p = AR.pl.find((q) => q.name === AR.sel);
  if (!p) {
    insp.innerHTML = "<p class='muted cl-p'>" + (AR.pl.length ? "Click a macro to see where it is and what it needs." : "No instances yet. Add the instance your RTL uses on the macro's card.") + "</p>" + unplacedList(probs);
    return;
  }
  const g = AR.model.macros[p.macro] || {};
  const mine = probs.filter((q) => q.inst === p.name);
  const site = AR.model.site || {};
  const orients = ["N", "S", "FN", "FS"];
  const allowed = g.orients || ["N"];
  const loc = p.location;
  const c = loc && g.grid === "default" ? P.crossings(AR.model, p) : null;
  insp.innerHTML =
    "<div class='an-eyebrow mc-insp-mod' title='" + esc(p.macro) + "'>" + esc(p.macro) + "</div><h4 class='mc-insp-name mono'>" + esc(p.name) + "</h4>" +
    (loc ? "<div class='mc-insp-xy'>" +
      "<label>X <input id='mc-ix' class='mono' inputmode='decimal' value='" + esc(P.fmtUm(loc[0])) + "' aria-label='X of the lower-left corner, µm'> <span class='muted'>µm</span></label>" +
      "<label>Y <input id='mc-iy' class='mono' inputmode='decimal' value='" + esc(P.fmtUm(loc[1])) + "' aria-label='Y of the lower-left corner, µm'> <span class='muted'>µm</span></label>" +
      (site.w ? "<span class='muted mc-insp-grid'>" + P.fmtUm(loc[0] / site.w) + " sites, " + P.fmtUm(loc[1] / site.h) + " rows from the origin</span>" : "") + "</div>"
      : "<p class='cl-p'><span class='vf-mark is-fail'></span> No position yet.</p><button class='btn btn-sm btn-primary' data-ar='place-one'>Place it</button>") +
    "<div class='mc-insp-h'>Orientation</div><div class='mc-or' role='radiogroup' aria-label='Orientation'>" +
      orients.map((o) => "<button class='mc-orb" + (p.orientation === o ? " is-on" : "") + "' data-or='" + o + "' role='radio' aria-checked='" + (p.orientation === o) + "'" + (allowed.includes(o) ? "" : " disabled title='Its LEF SYMMETRY doesn&#39;t allow " + o + "'") + ">" +
        "<span class='mc-or-ic is-" + o + "' aria-hidden='true'><i></i></span><b>" + esc(P.ORIENT_WORDS[o]) + "</b><span class='mono'>" + o + "</span></button>").join("") + "</div>" +
    "<p class='cl-foot mc-or-note'>" + (allowed.some((o) => P.ROTATED.has(o)) ? "90° turns (W, E, FW, FE) are allowed: your own power grid decides." :
      (g.grid === "lanex" ? "90° turns are off: the grid LanEx adds for this block is laid across its power pins as drawn, and a turned block would get none (PDN-0233)."
        : "90° turns are off: the default power grid can't reach a turned block's power pins (PDN-0233).") +
      " Keys: R turns it 180°, F mirrors it left-right, V top-bottom.") + "</p>" +
    "<div class='mc-insp-h'>Checks</div><ul class='mc-checks'>" +
      (mine.length ? mine.map((q) => "<li class='is-" + (q.hard ? "fail" : "warn") + "'><span class='vf-mark is-" + (q.hard ? "fail" : "warn") + "'></span><span>" + esc(q.message) + (q.fix ? " <span class='muted'>" + esc(q.fix) + "</span>" : "") + "</span></li>").join("")
        : "<li class='is-pass'><span class='vf-mark is-pass'></span><span>Inside the core, on the grid, no overlap.</span></li>") +
      (c ? "<li class='is-" + (c.power && c.ground ? "pass" : "fail") + "'><span class='vf-mark is-" + (c.power && c.ground ? "pass" : "fail") + "'></span><span>Power: " + (c.power + c.ground) + " via sites where " + esc(AR.model.pdn.hlayer) + " straps cross its " + esc(AR.model.pdn.vlayer) + " pins (" + c.power + " VDD, " + c.ground + " ground).</span></li>" : "") +
      (g.grid === "lanex" ? "<li class='is-pass'><span class='vf-mark is-pass'></span><span>Power: LanEx adds a small grid for this block (" + esc((g.plan || {}).kind || "") + " pattern).</span></li>" : "") +
    "</ul>" +
    (mine.some((q) => q.hard && q.code !== "unplaced") ? "<button class='btn btn-sm' data-ar='nearest'>Move to the nearest legal spot</button>" : "") +
    unplacedList(probs);
}

function unplacedList(probs) {
  const un = AR.pl.filter((p) => !p.location);
  if (!un.length) return "";
  return "<div class='mc-insp-h'>No position yet</div><ul class='mc-unplaced'>" + un.map((p) => "<li><button class='btn btn-ghost btn-xs mono' data-ar-sel='" + esc(p.name) + "'>" + esc(p.name) + "</button></li>").join("") + "</ul>" + (probs ? "" : "");
}

function wireArrange(host) {
  const sec = host.querySelector("#mc-arrange");
  if (!sec) return;
  sec.addEventListener("click", async (e) => {
    const sd = e.target.closest("[data-mc-setdie]");
    if (sd) { setDie(sd); return; }
    const sn = e.target.closest("[data-snap]");
    if (sn) {
      AR.snap = sn.dataset.snap;
      sec.querySelectorAll("[data-snap]").forEach((b) => { b.classList.toggle("is-on", b === sn); b.setAttribute("aria-checked", String(b === sn)); });
      if (AR.fp) AR.fp.setSnap(AR.snap);
      return;
    }
    const ps = e.target.closest("[data-ar-sel]");
    if (ps) { AR.sel = ps.dataset.arSel; if (AR.fp) AR.fp.select(AR.sel); refreshSide(); return; }
    const orb = e.target.closest("[data-or]");
    if (orb && !orb.disabled) {
      const pl = AR.pl.map((p) => Object.assign({}, p));
      const p = pl.find((q) => q.name === AR.sel);
      if (p) { p.orientation = orb.dataset.or; applyPl(pl); }
      return;
    }
    const a = e.target.closest("[data-ar]");
    if (!a) return;
    const act = a.dataset.ar;
    if (act === "fit") AR.fp && AR.fp.fit();
    else if (act === "in") AR.fp && AR.fp.zoomBy(1.4);
    else if (act === "out") AR.fp && AR.fp.zoomBy(1 / 1.4);
    else if (act === "arrange" || act === "place-one") arrangeNow(act === "arrange" && !AR.pl.some((p) => !p.location));
    else if (act === "nearest") {
      const n = P.nearestLegal(AR.model, AR.pl, AR.sel);
      if (!n) { toast.show("No legal spot for " + AR.sel + " in this core. Make the die bigger, shrink the keep-out, or move another macro.", "warn", 7000); return; }
      applyPl(AR.pl.map((p) => (p.name === AR.sel ? Object.assign({}, p, n) : p)));
    } else if (act === "undo") {
      _histLock = true;
      const prev = AR.hist.pop();
      AR.pl = prev || AR.saved.map((p) => Object.assign({}, p));
      if (AR.fp) AR.fp.set(AR.model, AR.pl);
      _histLock = false;
      refreshSide();
    } else if (act === "save") savePositions(a);
  });
  sec.addEventListener("change", async (e) => {
    const sh = e.target.closest("[data-show]");
    if (sh) { AR.show[sh.dataset.show] = sh.checked; if (AR.fp) AR.fp.setShow(AR.show); return; }
    if (e.target.id === "mc-ix" || e.target.id === "mc-iy") typedXY();
  });
  sec.addEventListener("keydown", (e) => {
    if ((e.target.id === "mc-ix" || e.target.id === "mc-iy") && e.key === "Enter") { e.preventDefault(); typedXY(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.target.closest("input")) {
      e.preventDefault();
      sec.querySelector("[data-ar='undo']")?.click();
    }
    if (!e.target.closest("input") && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === "0") { AR.fp && AR.fp.fit(); e.preventDefault(); }
      else if (e.key === "+" || e.key === "=") { AR.fp && AR.fp.zoomBy(1.4); e.preventDefault(); }
      else if (e.key === "-") { AR.fp && AR.fp.zoomBy(1 / 1.4); e.preventDefault(); }
    }
  });
  const hh = sec.querySelector("#mc-hh"), hv = sec.querySelector("#mc-hv"), hs = sec.querySelector("#mc-halo-save");
  const showSave = () => { if (hs) hs.hidden = false; };
  hh?.addEventListener("input", showSave);
  hv?.addEventListener("input", showSave);
  hs?.addEventListener("click", async () => {
    try {
      await api.macroSettings(state.designDir, { halo_h: hh.value, halo_v: hv.value });
      toast.show("Keep-out saved. The next run uses it for the rows and the power grid.", "success");
      renderMacros(C);
    } catch (ex) { toast.show(ex.message || String(ex), "error"); }
  });
}

function typedXY() {
  const ix = document.getElementById("mc-ix"), iy = document.getElementById("mc-iy");
  if (!ix || !iy) return;
  const x = Number(ix.value.replace(",", ".")), y = Number(iy.value.replace(",", "."));
  if (!Number.isFinite(x) || !Number.isFinite(y)) { toast.show("X and Y are numbers of micrometres.", "warn"); return; }
  const [gx, gy] = P.gridOf(AR.model, AR.snap);
  const sx = P.snap(x, gx), sy = P.snap(y, gy);
  const pl = AR.pl.map((p) => (p.name === AR.sel ? Object.assign({}, p, { location: [sx, sy] }) : p));
  applyPl(pl);
  if (Math.abs(sx - x) > 1e-9 || Math.abs(sy - y) > 1e-9) toast.show("Snapped to the " + (AR.snap === "site" ? "site grid" : "manufacturing grid") + ": (" + P.fmtUm(sx) + ", " + P.fmtUm(sy) + ") µm.", "info", 3000);
}

function arrangeNow(all) {
  if (!AR.model || !(AR.model.core || {}).box) { toast.show("Set a fixed die first: there is no core to place against.", "warn", 6000); return; }
  const res = P.arrange(AR.model, AR.pl, { all });
  applyPl(res.placements);
  if (res.failed.length) toast.show("No room in the core for " + res.failed.join(", ") + ". Make the die bigger or the keep-out smaller.", "error", 8000);
  else if (res.placed.length) toast.show("Placed " + res.placed.length + " instance" + (res.placed.length === 1 ? "" : "s") + ". This is a starting point: drag to adjust, then Save positions.", "success", 6000);
  if (res.placed.length) { AR.sel = res.placed[0]; if (AR.fp) AR.fp.select(AR.sel); refreshSide(); }
}

async function savePositions(btn) {
  btn.disabled = true;
  try {
    const fp = await api.macroPlace(state.designDir, AR.pl, pdkNow(), sclNow());
    toast.show("Macro positions saved. The next run places them there.", "success");
    D.fp = fp;
    await renderMacros(C);
  } catch (ex) { toast.show("Couldn't save: " + (ex.message || ex), "error"); btn.disabled = false; }
}

async function setDie(b) {
  const fp = D.fp || {};
  const box = (fp.die || {}).from === "run" ? fp.die.box : null;
  const size = box ? [box[2] - box[0], box[3] - box[1]] : fp.suggest_die;
  if (!size) return;
  const ok = await confirmDialog({ title: "Use a fixed die", confirmText: "Save to the config",
    body: "Writes FP_SIZING: absolute and DIE_AREA: [0, 0, " + size[0] + ", " + size[1] + "] to " + ((fp.config) || "your config") + ". The flow then keeps the die at " + size[0] + " × " + size[1] + " µm, and you can change it on Setup." });
  if (!ok) return;
  b.disabled = true;
  try {
    await api.configSet({ FP_SIZING: "absolute", DIE_AREA: [0, 0, size[0], size[1]] }, []);
    document.dispatchEvent(new CustomEvent("g:config_changed", { detail: { keys: ["FP_SIZING", "DIE_AREA"] } }));
    await renderMacros(C);
    if (AR.pl.some((p) => !p.location)) arrangeNow(false);
    toast.show("Die set to " + size[0] + " × " + size[1] + " µm (absolute sizing).", "success", 6000);
  } catch (ex) { toast.show("Couldn't save: " + (ex.message || ex), "error"); b.disabled = false; }
}

// ---------------------------------------------------------- card actions ----
async function saveMacro(name, patch) {
  const m = D.macros.find((x) => x.name === name);
  const body = { design_dir: state.designDir, name, instances: m.instances || [], views: {}, enabled: m.enabled !== false };
  Object.assign(body, patch);
  try {
    await api.customMacroSave(body);
    return true;
  } catch (ex) { toast.show(ex.message || String(ex), "error"); return false; }
}

function collectNames(card, m) {
  const prev = m.instances || [];
  return [...card.querySelectorAll(".mc-irow")].map((r) => {
    const k = Number(r.dataset.k);
    const old = Number.isFinite(k) && prev[k] ? prev[k] : { location: null, orientation: "N" };
    return { name: r.querySelector(".mc-iname").value.trim(), location: old.location || null, orientation: old.orientation || "N" };
  }).filter((i) => i.name);
}

function wireCards(host) {
  if (host._mcWired) return;
  host._mcWired = true;
  host.addEventListener("input", (e) => {
    const card = e.target.closest(".mc-card");
    if (card && e.target.closest(".mc-irow")) card.querySelector("[data-mc-save]").hidden = false;
  });
  host.addEventListener("change", async (e) => {
    const card = e.target.closest(".mc-card");
    if (!card) return;
    if (e.target.matches("[data-mc-enable]")) {
      try { await api.customMacroEnable(card.dataset.macro, e.target.checked, state.designDir); renderMacros(C); }
      catch (ex) { toast.show(ex.message, "error"); e.target.checked = !e.target.checked; }
    }
  });
  host.addEventListener("click", async (e) => {
    const card = e.target.closest(".mc-card");
    const name = card && card.dataset.macro;
    const info = name ? ((D.inspect.macros || {})[name] || {}) : null;
    if (e.target.closest("[data-mc-add]") && !card) { openAdd(e.target.closest("[data-mc-add]").dataset.mcAdd || "pdk"); return; }
    if (e.target.closest("[data-mc-overlay]")) { showOverlay(); return; }
    if (!card) return;
    const m = D.macros.find((x) => x.name === name);
    if (e.target.closest("[data-mc-remove]")) {
      if (!(await confirmDialog({ title: "Remove " + name, danger: true, confirmText: "Remove",
        body: "Its next run won't include it. Files LanEx made or copied into macros/" + name + "/ are deleted; PDK files are left alone." }))) return;
      try { await api.customMacroRemove(name, state.designDir); renderMacros(C); } catch (ex) { toast.show(ex.message, "error"); }
      return;
    }
    if (e.target.closest("[data-mc-addinst]")) {
      const rows = card.querySelector(".mc-inst-rows") || (() => {
        const d = document.createElement("div"); d.className = "mc-inst-rows";
        card.querySelector(".mc-inst .cl-p")?.replaceWith(d); return d;
      })();
      const k = rows.children.length;
      rows.insertAdjacentHTML("beforeend", instRow({ name: "u_" + (k ? "macro" + k : "sram"), location: null, orientation: "N" }, "new"));
      card.querySelector("[data-mc-save]").hidden = false;
      rows.lastElementChild.querySelector(".mc-iname").select();
      return;
    }
    if (e.target.closest("[data-mc-delinst]")) {
      e.target.closest(".mc-irow").remove();
      card.querySelector("[data-mc-save]").hidden = false;
      return;
    }
    if (e.target.closest("[data-mc-save]")) {
      const inst = collectNames(card, m);
      const patch = { instances: inst };
      if (!m.power && info.ok && ((info.power || {}).vdd || []).length) patch.power = defaultPower(info);
      if (await saveMacro(name, patch)) { toast.show("Saved " + name + ". Give new instances a position on Arrange.", "success"); renderMacros(C); }
      return;
    }
    if (e.target.closest("[data-mc-usefound]")) {
      const found = ((D.inspect.scan || {}).instances || {})[name] || [];
      const prev = new Map((m.instances || []).map((i) => [i.name, i]));
      const inst = found.map((n) => prev.get(n) || { name: n, location: null, orientation: "N" });
      const patch = { instances: inst };
      if (!m.power && info.ok && (info.power.vdd || []).length) patch.power = defaultPower(info);
      if (await saveMacro(name, patch)) { toast.show("Instances set to the RTL's: " + found.join(", "), "success"); await renderMacros(C); if (AR.pl.some((p) => !p.location)) arrangeNow(false); }
      return;
    }
    if (e.target.closest("[data-mc-power]")) { editPower(name, info); return; }
    if (e.target.closest("[data-mc-arrange]")) { arrangeNow(false); document.getElementById("mc-arrange")?.scrollIntoView({ behavior: "smooth", block: "nearest" }); return; }
    const show = e.target.closest("[data-mc-show]");
    if (show) {
      AR.sel = show.dataset.mcShow;
      if (AR.fp) AR.fp.focus(AR.sel);
      refreshSide();
      document.getElementById("mc-arrange")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }
    if (e.target.closest("[data-mc-libdiff]")) { showLibChange(m); return; }
    if (e.target.closest("[data-mc-pdn]")) { showFile(".gui-macros-pdn.tcl", "The power grid LanEx adds for this block", "Written at each run start, after LibreLane's own grid (it sources pdn_cfg.tcl first). To change it, copy it into your project and set PDN_CFG in your config; LanEx then adds none.", "Written when a run starts with this block placed."); return; }
    if (e.target.closest("[data-mc-copytpl]")) { if (info.template) copyText(info.template, "Template copied"); return; }
    if (e.target.closest("[data-mc-opentop]")) {
      const f = ((D.inspect.scan || {}).top_file) || "";
      import("./nav.js").then((mm) => mm.go("ide", f ? { rel: shortPath(f) } : {}));
    }
  });
}

function defaultPower(info) {
  const p = (info && info.power) || { vdd: [], gnd: [] };
  return { vdd_pins: p.vdd || [], gnd_pins: p.gnd || [], vdd_net: D.inspect.vdd || "VPWR", gnd_net: D.inspect.gnd || "VGND" };
}

async function editPower(name, info) {
  const m = D.macros.find((x) => x.name === name);
  const cur = normPower(m.power) || defaultPower(info);
  const pins = (info && info.power) || { vdd: [], gnd: [] };
  await customDialog({
    title: "Power hookup: " + name,
    bodyHtml: "<p class='muted'>Which of the macro's pins connect to which of your design's power nets. LibreLane's power-grid step uses this to strap the macro to the grid; without it LVS reports the macro unpowered.</p>" +
      "<div class='mc-pw-form'>" +
      "<label>Macro power pins <input id='pw-vp' class='mono' value='" + esc(cur.vdd_pins.join(", ")) + "' spellcheck='false'></label>" +
      "<label>connect to net <input id='pw-vn' class='mono' value='" + esc(cur.vdd_net) + "' spellcheck='false'></label>" +
      "<label>Macro ground pins <input id='pw-gp' class='mono' value='" + esc(cur.gnd_pins.join(", ")) + "' spellcheck='false'></label>" +
      "<label>connect to net <input id='pw-gn' class='mono' value='" + esc(cur.gnd_net) + "' spellcheck='false'></label></div>" +
      "<p class='muted'>Pins its LEF marks POWER: " + esc(pins.vdd.join(", ") || "none") + "; GROUND: " + esc(pins.gnd.join(", ") || "none") +
      ". This design's nets: " + esc(D.inspect.vdd || "VPWR") + " and " + esc(D.inspect.gnd || "VGND") + " (from " + (D.inspect.vdd ? "the config or the PDK" : "the PDK") + "). Several pins: separate them with commas; each gets its own line.</p>",
    buttons: [{ label: "Cancel", value: null, cls: "btn-ghost" }, { label: "Remove hookup", value: "clear", cls: "btn-ghost" }, { label: "Save", value: "save", cls: "btn-primary" }],
    onMount: (back) => { back.querySelector(".dlg").dataset.pw = name; },
  });
}

// customDialog removes its DOM before resolving, so read the fields on click.
document.addEventListener("click", (e) => {
  const dlg = e.target.closest(".dlg[data-pw]");
  if (!dlg || !e.target.closest(".dlg-actions .btn")) return;
  const label = e.target.textContent;
  const name = dlg.dataset.pw;
  const list = (id) => dlg.querySelector(id).value.split(/[,|\s]+/).map((s) => s.trim()).filter(Boolean);
  if (label === "Save") {
    const power = { vdd_pins: list("#pw-vp"), vdd_net: dlg.querySelector("#pw-vn").value.trim(), gnd_pins: list("#pw-gp"), gnd_net: dlg.querySelector("#pw-gn").value.trim() };
    saveMacro(name, { power }).then((ok) => { if (ok) { toast.show("Power hookup saved.", "success"); renderMacros(C); } });
  } else if (label === "Remove hookup") {
    saveMacro(name, { power: {} }).then((ok) => { if (ok) renderMacros(C); });
  }
}, true);

async function showLibChange(m) {
  const rel = (((m.generated || {}).lib || {}).relaxed) || [];
  await customDialog({
    title: "What LanEx changed in the timing file",
    bodyHtml: "<p class='muted'>LanEx never edits a PDK file. It keeps a copy in your project and changes only the per-pin input limits below; every delay and transition table is exactly the PDK's. The limit it uses is the file's own <code>default_max_transition</code>.</p>" +
      rel.map((r) => "<h4>" + esc(r.from) + "</h4><p class='muted mono'>" + esc(r.file.replace(/^dir::/, "")) + "</p>" +
        "<table class='data-table cl-tbl'><thead><tr><th>Pin</th><th>Line</th><th>max_transition</th><th>LanEx's copy</th></tr></thead><tbody>" +
        r.changes.map((c) => "<tr><td class='mono'>" + esc(c.pin) + "</td><td class='mono'>" + c.line + "</td><td class='mono'>" + esc(c.from) + " ns</td><td class='mono'>" + esc(c.to) + " ns</td></tr>").join("") +
        "</tbody></table>").join("") +
      "<p class='cl-foot'>Why: a 0.04 ns limit is the bottom of the characterisation table, and no buffer in the library can make an edge that fast, so the resizer stops with RSZ-0090.</p>",
  });
}

async function showFile(rel, title, note, missing) {
  const abs = state.designDir.replace(/\/+$/, "") + "/" + rel;
  let t;
  try { t = await api.readText(abs); } catch (_e) { t = null; }
  const { renderFileText } = await import("./fileview.js");
  await customDialog({
    title, wide: true,
    bodyHtml: "<p class='muted prov-note'>" + esc(note) + "</p><div class='prov-view'></div>",
    onMount: (back) => {
      const v = back.querySelector(".prov-view");
      if (t && t.ok !== false && t.text) renderFileText(v, t.text, { title: rel, abs });
      else v.innerHTML = "<p class='muted'>Not written yet: " + esc(missing) + "</p>";
    },
  });
}

function showOverlay() {
  return showFile(".gui-macros.json", "The macro overlay LanEx hands LibreLane",
    "Written at each run start from the macros switched on above, and passed to LibreLane as a second config file next to yours. Your config file is never edited. Every run keeps a copy of what it used (gui-run.json).",
    "it appears when a run starts with at least one macro switched on.");
}

// ---------------------------------------------------------- add dialog -----
async function openAdd(tab) {
  const pdk = pdkNow();
  await customDialog({
    title: "Add a macro",
    wide: true,
    bodyHtml:
      "<div class='mc-tabs' role='tablist'>" +
      "<button class='mc-tab' data-t='pdk' role='tab'>" + icon("database", { size: 14 }) + " From the PDK</button>" +
      "<button class='mc-tab' data-t='run' role='tab'>" + icon("box", { size: 14 }) + " From another project</button>" +
      "<button class='mc-tab' data-t='files' role='tab'>" + icon("upload", { size: 14 }) + " Files</button></div>" +
      "<div class='mc-pane' data-p='pdk'></div><div class='mc-pane' data-p='run' hidden></div><div class='mc-pane' data-p='files' hidden></div>",
    buttons: [{ label: "Close", value: null, cls: "btn-ghost" }],
    onMount: (back, close) => {
      const show = (t) => {
        back.querySelectorAll(".mc-tab").forEach((b) => { b.classList.toggle("is-on", b.dataset.t === t); b.setAttribute("aria-selected", String(b.dataset.t === t)); });
        back.querySelectorAll(".mc-pane").forEach((p) => { p.hidden = p.dataset.p !== t; });
        const pane = back.querySelector(".mc-pane[data-p='" + t + "']");
        if (!pane.dataset.done) { pane.dataset.done = "1"; ({ pdk: panePdk, run: paneRun, files: paneFiles })[t](pane, pdk, close); }
      };
      back.querySelector(".mc-tabs").addEventListener("click", (e) => { const b = e.target.closest(".mc-tab"); if (b) show(b.dataset.t); });
      show(["pdk", "run", "files"].includes(tab) ? tab : "pdk");
    },
  });
}

async function afterAdd(name, close) {
  close && close(null);
  await renderMacros(C);
  // Instantiated already? Then list those instances, hook up the power and
  // propose positions (unsaved until Save positions).
  const found = ((D.inspect.scan || {}).instances || {})[name] || [];
  const m = D.macros.find((x) => x.name === name);
  const info = (D.inspect.macros || {})[name] || {};
  if (m && found.length && !(m.instances || []).length) {
    const patch = { instances: found.map((n) => ({ name: n, location: null, orientation: "N" })) };
    if (!m.power && info.ok && (info.power.vdd || []).length) patch.power = defaultPower(info);
    if (await saveMacro(name, patch)) {
      await renderMacros(C);
      if (AR.model && (AR.model.core || {}).box) arrangeNow(false);
      toast.show("Added " + name + ". Found it in your RTL as " + found.join(", ") + (AR.model && (AR.model.core || {}).box ? "; Arrange proposes a position. Save positions to keep it." : ". Give it a fixed die and a position next."), "success", 8000);
    }
  } else {
    toast.show("Added " + name + ". Next: instantiate it in your RTL (template on its card).", "success", 6000);
  }
  document.getElementById("cl-macros")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function panePdk(pane, pdk, close) {
  const list = D.catalog;
  if (!list.length) { pane.innerHTML = "<p class='muted'>" + esc(pdk) + " ships no hard macros under libs.ref.</p>"; return; }
  const have = new Set(D.macros.map((m) => m.name));
  pane.innerHTML = "<p class='muted'>Ready-made blocks in the installed " + esc(pdk) + ". LanEx points at the PDK's files with <code>pdk_dir::</code> paths, so nothing is copied and the project stays portable. It makes a ports-only header for synthesis and, where the PDK's timing file needs it, a relaxed copy in your project.</p>" +
    "<div class='cl-tblwrap'><table class='data-table cl-tbl mc-cat'><thead><tr><th>Block</th><th>Capacity</th><th>Ports</th><th>Size</th><th>Timing</th><th></th></tr></thead><tbody>" +
    list.map((m, k) => {
      const c = m.capacity || {};
      const slow = m.lef_bytes > 5e6;
      return "<tr><td><div class='mono'>" + esc(m.name) + "</div><div class='muted'>" + esc(m.library) + (slow ? " · large LEF, slow to read" : "") + "</div></td>" +
        "<td>" + (c.words ? c.words + " × " + c.bits + " bit" + (c.kbits ? "<div class='muted'>" + (c.kbits >= 8 ? (c.kbits / 8) + " KB" : c.kbits + " kbit") + "</div>" : "") : "–") + "</td>" +
        "<td>" + esc(portsText(c.ports)) + "</td>" +
        "<td>" + fmtSize(m.w, m.h) + "<div class='muted'>" + M.fmtArea(m.w * m.h) + "</div></td>" +
        "<td>" + (m.lib_corner ? "<span title='" + esc(m.lib_corner) + "'>" + (m.corners > 1 ? m.corners + " files" : "1 corner") + "</span>" : "<span class='cl-no'>none</span>") + "</td>" +
        "<td>" + (have.has(m.name) ? "<span class='muted'>added</span>" : "<button class='btn btn-sm btn-primary' data-k='" + k + "'>Add</button>") + "</td></tr>";
    }).join("") + "</tbody></table></div>";
  pane.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-k]");
    if (!b) return;
    const m = list[+b.dataset.k];
    b.disabled = true; b.textContent = "Adding…";
    try {
      await api.macroAdd({ design_dir: state.designDir, name: m.name, scl: sclNow(),
        source: { kind: "pdk", pdk, library: m.library }, instances: [] });
      afterAdd(m.name, close);
    } catch (ex) { toast.show(ex.message || String(ex), "error"); b.disabled = false; b.textContent = "Add"; }
  });
}

function portsText(p) {
  if (!p) return "–";
  if (/^1rw1r$/i.test(p)) return "1 read/write + 1 read";
  if (/^1rw$/i.test(p)) return "1 read/write";
  if (/^1P$/i.test(p)) return "single port";
  if (/^2P$/i.test(p)) return "dual port";
  return p;
}

async function paneRun(pane, pdk, close) {
  pane.innerHTML = "<p class='muted'><span class='spinner'></span> Looking through your projects…</p>";
  let res;
  try { res = await api.macroProjects(state.designDir); } catch (_ex) { res = { projects: [] }; }
  const ps = (res && res.projects) || [];
  if (!ps.length) {
    pane.innerHTML = "<div class='an-none'>No other project has a finished run with GDS and LEF yet. The hierarchical flow: open the sub-block as its own project, run it to the end, come back here.</div>";
    return;
  }
  pane.innerHTML = "<p class='muted'>A finished run of another project becomes a block in this one: its final GDS, LEF, netlist, parasitics and timing models are copied into <code>macros/&lt;module&gt;/</code>, so cleaning the other project's runs can't break this one.</p>" +
    ps.map((p, i) => "<div class='mc-proj'><div class='mc-proj-h'>" + icon("folder", { size: 14 }) + " <b>" + esc(p.name) + "</b> <span class='muted mono'>" + esc(p.design_dir) + "</span></div>" +
      p.runs.map((r, k) => {
        const other = r.pdk && pdk && r.pdk !== pdk;
        return "<div class='mc-run'><span class='vf-mark is-" + (r.success ? "pass" : "warn") + "'></span>" +
          "<span class='mono'>" + esc(r.module) + "</span><span class='muted'>" + esc(r.tag) + (r.when ? " · " + esc(String(r.when).slice(0, 16).replace("T", " ")) : "") + "</span>" +
          "<span class='mc-run-v'>" + r.views.map((v) => "<span class='cc-chip'>" + esc(v) + "</span>").join("") + "</span>" +
          (other ? "<span class='cl-warn-t'>built for " + esc(r.pdk) + "</span>" : "") +
          "<button class='btn btn-sm btn-primary' data-p='" + i + "' data-r='" + k + "'" + (other ? " disabled title='Different PDK'" : "") + ">Use</button></div>";
      }).join("") + "</div>").join("");
  pane.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-r]");
    if (!b) return;
    const p = ps[+b.dataset.p], r = p.runs[+b.dataset.r];
    b.disabled = true; b.textContent = "Copying…";
    try {
      const res2 = await api.macroAdd({ design_dir: state.designDir, pdk, scl: sclNow(), source: { kind: "run", design_dir: p.design_dir, tag: r.tag }, instances: [] });
      afterAdd((res2.macro && res2.macro.name) || r.module, close);
    } catch (ex) { toast.show(ex.message || String(ex), "error"); b.disabled = false; b.textContent = "Use"; }
  });
}

const KIND_OF = (n) => {
  const f = n.toLowerCase();
  if (/\.gds(\.gz)?$/.test(f)) return "gds";
  if (/\.lef$/.test(f)) return "lef";
  if (/\.lib$/.test(f)) return "lib";
  if (/\.spef$/.test(f)) return "spef";
  if (/\.(spice|sp|cir|cdl)$/.test(f)) return "spice";
  if (/\.mag$/.test(f)) return "maglef";
  if (/\.nl\.v$|_nl\.v$|netlist/.test(f)) return "nl";
  if (/\.(v|sv|vh)$/.test(f)) return "vh";
  return null;
};
const KIND_LABEL = { gds: "GDS layout", lef: "LEF abstract", lib: "Liberty timing", spef: "SPEF parasitics", spice: "SPICE/CDL for LVS", nl: "Gate-level netlist", vh: "Verilog (header or model)", maglef: "Magic abstract (maglef)" };

function paneFiles(pane, _pdk, close) {
  const files = {};
  let ups = [];
  pane.innerHTML = "<label class='mc-drop' tabindex='0'><input type='file' multiple hidden>" + icon("upload", { size: 22 }) +
    "<b>Drop the macro's files here, or click to pick them</b><span>GDS and LEF are required. Add the Liberty file so timing goes through the block, and SPICE or CDL so LVS passes. Files of any size up to 2 GB.</span></label>" +
    "<div class='mc-files'></div>" +
    "<label class='mc-fname'>Module name <input class='mono' id='mc-up-name' placeholder='read from the LEF' spellcheck='false'></label>" +
    "<p class='muted mc-up-lefnote' id='mc-up-lefnote'></p>" +
    "<div class='mc-up-act'><button class='btn btn-primary btn-sm' id='mc-up-save' disabled>Add macro</button><button class='btn btn-ghost btn-sm' id='mc-up-cancel' hidden>Cancel</button><span class='muted' id='mc-up-msg' role='status'></span></div>";
  const drop = pane.querySelector(".mc-drop"), input = drop.querySelector("input");
  const paintFiles = (progress) => {
    const ks = Object.keys(files);
    pane.querySelector(".mc-files").innerHTML = ks.map((k) => {
      const pr = progress && progress[k];
      return "<div class='mc-file'><span class='cc-chip'>" + k + "</span> <span class='mono'>" + esc(files[k].name) + "</span>" +
        " <span class='muted'>" + esc(KIND_LABEL[k]) + " · " + fmtBytes(files[k].size) + "</span>" +
        (pr ? "<span class='mc-up-bar' role='progressbar' aria-valuemin='0' aria-valuemax='100' aria-valuenow='" + Math.round(100 * pr[0] / (pr[1] || 1)) + "'><i style='width:" + (100 * pr[0] / (pr[1] || 1)).toFixed(1) + "%'></i></span><span class='mono muted'>" + fmtBytes(pr[0]) + " of " + fmtBytes(pr[1]) + "</span>" : "") + "</div>";
    }).join("");
    pane.querySelector("#mc-up-save").disabled = !(files.gds && files.lef && pane.querySelector("#mc-up-name").value.trim()) || ups.length > 0;
  };
  const take = async (list) => {
    for (const f of list) {
      const k = KIND_OF(f.name);
      if (!k) { toast.show("Skipped " + f.name + ": not a macro view", "warn"); continue; }
      files[k] = f;
      if (k === "lef") {
        // the first 2 MB is enough to find the block names
        const txt = await f.slice(0, 2 * 1024 * 1024).text();
        const names = [...txt.matchAll(/^\s*MACRO\s+(\S+)/gm)].map((x) => x[1]);
        const nm = pane.querySelector("#mc-up-name");
        if (names.length && !nm.value) nm.value = names[0];
        pane.querySelector("#mc-up-lefnote").textContent = names.length > 1 ? "The LEF has " + names.length + " blocks (" + names.slice(0, 5).join(", ") + (names.length > 5 ? ", …" : "") + "). The module name says which one this macro is." : (names.length ? "" : "No MACRO found in the start of this LEF; check it is the macro's abstract.");
      }
    }
    paintFiles();
  };
  drop.addEventListener("click", (e) => { if (e.target !== input) input.click(); });
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => take([...input.files]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("is-over"); take([...e.dataTransfer.files]); });
  pane.querySelector("#mc-up-name").addEventListener("input", () => paintFiles());
  const cancelBtn = pane.querySelector("#mc-up-cancel");
  cancelBtn.addEventListener("click", () => { for (const u of ups) u.cancel(); });
  pane.querySelector("#mc-up-save").addEventListener("click", async (e) => {
    const b = e.target;
    const name = pane.querySelector("#mc-up-name").value.trim();
    const msg = pane.querySelector("#mc-up-msg");
    b.disabled = true; cancelBtn.hidden = false; msg.textContent = "Uploading…";
    const progress = {};
    try {
      // Each file goes up on its own, as itself, with its progress shown;
      // then the save names what arrived.
      const views = {};
      for (const [k, f] of Object.entries(files)) {
        progress[k] = [0, f.size];
        const up = uploadFile(f, state.designDir, { onProgress: (sent, total) => { progress[k] = [sent, total]; paintFiles(progress); } });
        ups = [up];
        const res = await up.done;
        progress[k] = [f.size, f.size];
        views[k] = { filename: f.name, upload_id: res.upload_id };
      }
      ups = [];
      cancelBtn.hidden = true;
      msg.textContent = "Reading the LEF and making the header…";
      await api.customMacroSave({ design_dir: state.designDir, name, instances: [], views, enabled: true, pdk: pdkNow(), scl: sclNow() });
      afterAdd(name, close);
    } catch (ex) {
      ups = [];
      cancelBtn.hidden = true;
      msg.textContent = ex.cancelled ? "Stopped. Nothing was saved." : (ex.message || String(ex));
      b.disabled = false; b.textContent = "Add macro";
      paintFiles();
    }
  });
}
