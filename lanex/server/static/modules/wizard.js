// wizard.js — New project. Two steps: pick a template, then name it and set the
// target. The folder is worked out for you (<projects dir>/<name>, inside the
// WSL home on Windows so runs stay fast); "Change…" picks another parent.
import { api, fmt } from "./api.js";
import { state } from "./state.js";
import { toast } from "./toast.js";
import { openLayer, closeLayer } from "./overlay.js";
import { mountClockField, freqText } from "./clockfield.js";
import { mhz } from "./metricfmt.js";

let _overlay = null;
let _templates = [];
let _defaults = null;
let _parent = "";      // chosen parent folder; "" = the projects dir

const X_ICON = "<svg viewBox='0 0 24 24' width='16' height='16' fill='none' stroke='currentColor' stroke-width='1.7' stroke-linecap='round' aria-hidden='true'><path d='M6 6l12 12M18 6L6 18'/></svg>";

function close() {
  if (!_overlay) return;
  closeLayer(_overlay);
  _overlay.hidden = true;
}

function overlay() {
  if (_overlay) return _overlay;
  const ov = document.createElement("div");
  ov.className = "smodal-backdrop wizard-backdrop";
  ov.hidden = true;
  ov.innerHTML =
    "<div class='smodal wizard' role='dialog' aria-modal='true' aria-labelledby='wiz-title'>" +
    "  <div class='smodal-head'><span class='smodal-title' id='wiz-title'>New project</span>" +
    "  <span class='smodal-spacer'></span><button class='iconbtn' id='wiz-close' aria-label='Close'>" + X_ICON + "</button></div>" +
    "  <div class='wizard-body' id='wiz-body'></div>" +
    "</div>";
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  ov.querySelector("#wiz-close").addEventListener("click", close);
  _overlay = ov;
  return ov;
}

function steps(n) {
  return "<div class='wiz-steps'>" +
    "<span class='wiz-step" + (n === 1 ? " is-on" : "") + "'>1 Start from</span>" +
    "<span class='wiz-step" + (n === 2 ? " is-on" : "") + "'>2 Name and target</span></div>";
}

export async function openWizard({ template = "" } = {}) {
  const ov = overlay();
  ov.hidden = false;
  // A dialog on the overlay stack: Esc closes the top layer only, so the
  // folder picker above it closes first.
  openLayer(ov, { kind: "dialog", labelledBy: "wiz-title", onClose: () => { ov.hidden = true; }, focus: false });
  _parent = "";
  _taken = new Set();
  const body = ov.querySelector("#wiz-body");
  body.innerHTML = steps(1) + "<p class='muted'>Loading templates…</p>";
  try {
    [_templates, _defaults] = await Promise.all([
      api.templates(),
      api.projectDefaults().catch(() => null),
    ]);
    _display = (await import("./shell.js")).displayPath;
  } catch (ex) {
    body.innerHTML = steps(1) + "<p class='wiz-err'>Could not load the templates: " + fmt.escape(ex.message || ex) + "</p>";
    return;
  }
  const pre = template && _templates.find((t) => t.name === template);
  if (pre) renderStep2(body, pre); else renderStep1(body);
}

function tplCard(t, cls, n = 0) {
  // A template tied to a PDK (the SRAM's block ships with sky130 only) says so
  // on its card; with no matching PDK installed it can't be picked.
  const only = pdkOnlyWords(t);
  const none = only && !(state.pdks || []).some((p) => p.ready !== false && tplPdkOk(t, p.name));
  return "<button class='wiz-tpl " + cls + "' data-name='" + fmt.escape(t.name) + "' title='" + fmt.escape(t.description) + "'" +
    (none ? " disabled aria-describedby='wiz-only-" + fmt.escape(t.name) + "'" : "") + ">" +
    "<span class='wiz-tpl-title'>" + (n ? "<span class='wiz-tpl-n'>" + String(n).padStart(2, "0") + "</span>" : "") + fmt.escape(t.title) + "</span>" +
    "<span class='wiz-tpl-desc'>" + fmt.escape(t.teaches || t.description) + "</span>" +
    (t.has_testbench ? "<span class='wiz-tpl-tag'>Self-checking testbench</span>" : "") +
    (only ? "<span class='wiz-tpl-tag wiz-tpl-only' id='wiz-only-" + fmt.escape(t.name) + "'>" + fmt.escape(only) +
      (none ? ": install it in Tools first" : "") + "</span>" : "") +
    "</button>";
}

function globRx(pat) {
  return new RegExp("^" + String(pat).replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$");
}

// Whether a template builds on a PDK: every glob in its `pdks` must match
// (scaffold.pdk_refusal on the server). Pure (tested).
export function tplPdkOk(tpl, pdk) {
  const pats = (tpl && tpl.pdks) || [];
  return !pats.length || (!!pdk && pats.every((p) => globRx(p).test(pdk)));
}

// "sky130 only" for a template tied to a PDK family, "" otherwise.
export function pdkOnlyWords(tpl) {
  const pats = (tpl && tpl.pdks) || [];
  return pats.length ? pats.map((p) => String(p).replace(/\*+$/, "")).join(" and ") + " only" : "";
}

// The examples in the order they teach (template.json "order"): each one adds
// one idea to the one before. Pure (tested).
export function ladder(templates) {
  return (templates || []).filter((t) => t.kind !== "blank").slice().sort((a, b) => (a.order ?? 99) - (b.order ?? 99) || String(a.name).localeCompare(b.name));
}

function renderStep1(body) {
  // A new project usually starts blank; the examples are there to learn from
  // or to try the flow on something that is known to work.
  const blank = _templates.filter((t) => t.kind === "blank");
  const examples = ladder(_templates);
  body.innerHTML = steps(1) +
    blank.map((t) => tplCard(t, "wiz-tpl-blank")).join("") +
    (examples.length
      ? "<div class='wiz-lane'><span class='lane-label'>Or learn from an example</span>" +
        "<span class='wiz-lane-note'>Working designs in the order they teach: each runs clean to a GDS on sky130, and each bench prints PASS.</span></div>" +
        "<div class='wiz-templates'>" + examples.map((t, i) => tplCard(t, "", i + 1)).join("") + "</div>"
      : "");
  body.querySelectorAll(".wiz-tpl").forEach((b) =>
    b.addEventListener("click", () => {
      const tpl = _templates.find((t) => t.name === b.dataset.name);
      renderStep2(body, tpl);
    }),
  );
  body.querySelector(".wiz-tpl")?.focus();
}

function projectsDir() {
  return _parent || (_defaults && _defaults.projects_dir) || "";
}

function defaultName(tpl) {
  return tpl.kind === "blank" ? "my-chip" : tpl.name;
}

function shown(p) {
  return _display ? _display(p) : p;
}
let _display = null;

function joinPath(dir, name) {
  if (!dir) return name;
  return dir.replace(/[\\/]+$/, "") + "/" + name;
}

function sclNames(pdkName) {
  const pdk = (state.pdks || []).find((p) => p.name === pdkName);
  return ((pdk && pdk.variants) || []).map((v) => (Array.isArray(v) ? v[0] : v));
}

function sclOptions(pdkName) {
  return sclNames(pdkName).map((name) =>
    "<option value='" + fmt.escape(name) + "'>" + fmt.escape(name) + "</option>").join("");
}

// The usual general-purpose library of each PDK family, the same list the
// Setup picker uses; otherwise the first one installed.
const USUAL_SCL = ["sky130_fd_sc_hd", "gf180mcu_fd_sc_mcu7t5v0", "sg13g2_stdcell"];

export function defaultScl(names) {
  return USUAL_SCL.find((s) => names.includes(s)) || names[0] || "";
}

// The usual variant of each family, in the order a beginner is pointed to.
export const USUAL_PDK = ["sky130A", "gf180mcuD", "ihp-sg13g2"];

// The PDK a new project starts on: the open project's when it's ready, then
// the first ready usual one, then the first ready one. A PDK folder without its
// libraries (a leftover or a partial download) is never the default.
export function defaultPdk(pdks, current) {
  const ready = (pdks || []).filter((p) => p.ready !== false).map((p) => p.name);
  if (current && ready.includes(current)) return current;
  return USUAL_PDK.find((p) => ready.includes(p)) || ready[0] || ((pdks || [])[0] || {}).name || "";
}

// A template's clock for a PDK: its per-family clock ({"gf180mcu*": 20})
// when one matches, else its default. Globs as LibreLane's pdk:: keys.
export function clockFor(tpl, pdk) {
  for (const [pat, ns] of Object.entries(tpl.pdk_clock || {})) {
    const rx = new RegExp("^" + pat.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$");
    if (pdk && rx.test(pdk)) return ns;
  }
  return tpl.clock_period != null ? tpl.clock_period : 10;
}


// Verilog and SystemVerilog words a module can't be called (scaffold.py keeps
// the same list).
const RESERVED = new Set(("always and assign begin buf case casex casez default else end endcase endfunction endmodule " +
  "endtask for forever function if initial inout input integer localparam module nand negedge nor not or output " +
  "parameter posedge real reg repeat signed task time tri while wire xor xnor logic bit byte int always_ff " +
  "always_comb always_latch interface package class enum struct typedef string").split(" "));

// A top name as a valid Verilog module name, exactly as the server makes it
// (scaffold.verilog_identifier): "my chip" -> my_chip, "1top" -> top_1top.
export function verilogIdentifier(name, fallback = "top") {
  let s = String(name || "").trim().replace(/[^A-Za-z0-9_]+/g, "_").replace(/_+/g, "_").replace(/^_+|_+$/g, "");
  if (!s) return fallback;
  if (/^\d/.test(s)) s = "top_" + s;
  if (RESERVED.has(s.toLowerCase())) s += "_top";
  return s;
}

// The first free folder name: `name`, else name-2, name-3… (`taken`: the
// names already in the parent folder). Running the guide twice makes
// blinky-2 instead of failing on a folder that isn't empty.
export function freeName(name, taken) {
  const t = new Set([...(taken || [])].map((x) => String(x).toLowerCase()));
  if (!t.has(String(name).toLowerCase())) return name;
  let n = 2;
  while (t.has((name + "-" + n).toLowerCase())) n++;
  return name + "-" + n;
}

// Why a new project's settings can't build, in plain words, or "".
export function createProblem({ dest, clock, hasClock, pdk, pdks, tpl }) {
  if (/\s/.test(dest || "")) {
    return "The folder path has a space in it (" + dest + "). LibreLane splits paths on spaces, so every run from there " +
      "would fail. Pick a folder without spaces with Change….";
  }
  if (hasClock && !(clock > 0)) return "The clock period is a positive number of nanoseconds, such as 10 (100 MHz).";
  const p = (pdks || []).find((x) => x.name === pdk);
  if (!pdk || !p) return "No PDK is installed yet. Install one in Tools first: a run needs it.";
  if (p.ready === false) return pdk + " isn't installed: its folder has no standard-cell libraries. Install it in Tools, or pick another PDK.";
  if (tpl && !tplPdkOk(tpl, pdk)) {
    const fam = pdkOnlyWords(tpl).replace(/ only$/, "");
    const fits = (pdks || []).filter((x) => x.ready !== false && tplPdkOk(tpl, x.name)).map((x) => x.name);
    return "This example needs a " + fam + " PDK: its hard macro ships with " + fam + " only, and " + pdk +
      " has no such block. " + (fits.length ? "Pick " + fits.join(" or ") : "Install " + fam + " in Tools") +
      ", or go Back and pick another example.";
  }
  return "";
}

let _taken = new Set();   // folder names in the parent folder
async function readTaken(dir) {
  try {
    const r = await api.fsList(dir);
    _taken = new Set(((r && r.entries) || []).filter((e) => e.is_dir).map((e) => e.name));
  } catch (_e) {
    _taken = new Set();
  }
  return _taken;
}

function renderStep2(body, tpl) {
  // A template tied to a PDK family lists the others as "(no SRAM block)":
  // still visible, so the reason is on screen, and the create check says why.
  const tied = (tpl.pdks || []).length > 0;
  const pdkOpts = (state.pdks || []).map((p) =>
    "<option value='" + fmt.escape(p.name) + "'>" + fmt.escape(p.name) + (p.ready === false ? " (not installed)"
      : tied && !tplPdkOk(tpl, p.name) ? " (not for this example)" : "") + "</option>").join("");
  const fitting = (state.pdks || []).filter((p) => tplPdkOk(tpl, p.name));
  const startPdk = defaultPdk(tied ? fitting : state.pdks, state.designDir ? state.selectedPdk : "");
  const clk = clockFor(tpl, startPdk);
  body.innerHTML = steps(2) +
    "<div class='wiz-form'>" +
    "<label class='wiz-field span-2'><span>Project name</span>" +
    "<input id='wiz-name' type='text' value='" + fmt.escape(defaultName(tpl)) + "' autocomplete='off' spellcheck='false' />" +
    "<span class='wiz-help'>Letters, numbers, dashes and underscores. This becomes the folder name.</span></label>" +
    "<div class='wiz-field span-2'><span>Location</span>" +
    "<div class='wiz-loc'><code id='wiz-where'></code><button class='btn btn-ghost btn-sm' id='wiz-change'>Change…</button></div>" +
    "<span class='wiz-help' id='wiz-where-help'></span></div>" +
    "<div class='wiz-field'><label for='wiz-top'>Top module</label>" +
    "<span class='wiz-top-row'><input id='wiz-top' type='text' value='" + fmt.escape(tpl.top) + "' spellcheck='false' />" +
    "<select id='wiz-lang' aria-label='Language' title='The language of the starter files. Verilog .v is what LibreLane&#39;s own examples and Tiny Tapeout use.'>" +
    "<option value='verilog'>Verilog .v</option>" +
    "<option value='systemverilog'>SystemVerilog .sv</option></select></span>" +
    "<span class='wiz-help' id='wiz-top-help'></span></div>" +
    "<label class='wiz-field'><span>Clock</span>" +
    "<span class='wiz-unit wiz-clock'><input id='wiz-clk' type='text' value='" + fmt.escape(clk) + "' /><em>ns</em></span>" +
    "<span class='wiz-help' id='wiz-clk-help'></span></label>" +
    "<label class='wiz-field'><span>PDK</span><select id='wiz-pdk'>" + (pdkOpts || "<option value=''>No PDK installed</option>") + "</select>" +
    "<span class='wiz-help' id='wiz-pdk-help'></span></label>" +
    "<label class='wiz-field'><span>Standard-cell library</span><select id='wiz-scl'></select>" +
    "<span class='wiz-help'>The ready-made logic gates the flow builds your design from.</span></label>" +
    (tpl.kind === "blank"
      ? "<label class='nf-use span-2'><input type='checkbox' id='wiz-tb' checked/> <span>Write a testbench too, in <code>verify/</code>, " +
        "wired to the top module's ports, so you can simulate before the first run.</span></label>"
      : "") +
    "<div class='wiz-fp' id='wiz-fp' hidden></div>" +
    "</div>" +
    "<div class='wiz-actions'>" +
    "<button class='btn btn-ghost' id='wiz-back'>Back</button>" +
    "<span class='wiz-err' id='wiz-err' role='alert'></span>" +
    "<span class='grow-space'></span>" +
    "<button class='btn btn-primary' id='wiz-create'>Create project</button>" +
    "</div>";

  const $ = (id) => body.querySelector("#" + id);
  const pdkSel = $("wiz-pdk");
  pdkSel.value = startPdk;
  // The clock helper (clockfield.js): a period or a frequency, ns stored.
  const clkHelp = document.createElement("span");
  clkHelp.className = "wiz-clk-other";
  clkHelp.id = "wiz-clk-other";
  if ($("wiz-clk")) {
    const box = $("wiz-clk").parentElement;
    mountClockField($("wiz-clk"), { unitEl: box.querySelector("em"), otherEl: clkHelp,
      onInput: () => { clkTyped = true; paintClk(); } }).set(clk);
    box.appendChild(clkHelp);      // [period][ns] = 100 MHz
  }
  // The clock follows the PDK (gf180's cells are slower) until it's typed in.
  let clkTyped = false;
  const paintScl = () => {
    const names = sclNames(pdkSel.value);
    $("wiz-scl").innerHTML = sclOptions(pdkSel.value) || "<option value=''>None installed</option>";
    $("wiz-scl").value = state.selectedScl && pdkSel.value === state.selectedPdk && names.includes(state.selectedScl)
      ? state.selectedScl : defaultScl(names);
    const p = (state.pdks || []).find((x) => x.name === pdkSel.value);
    const help = $("wiz-pdk-help");
    const wrong = !!p && p.ready !== false && !tplPdkOk(tpl, p.name);
    help.classList.toggle("wiz-warn", !p || p.ready === false || wrong);
    help.textContent = !p
      ? "No PDK is installed yet. Install one in Tools first; a run needs it."
      : p.ready === false
        ? p.name + " isn't installed: its folder has no standard-cell libraries. Install it in Tools, or pick another PDK."
        : wrong
          ? createProblem({ pdk: p.name, pdks: state.pdks, tpl })
          : tied
            ? "This example's hard macro ships with the PDK, so it is " + pdkOnlyWords(tpl) + "."
            : "The foundry process to build for. sky130A is the usual first choice.";
    if (!clkTyped && $("wiz-clk")) { $("wiz-clk")._clock.set(clockFor(tpl, pdkSel.value)); paintClk(); }
    paintFp();
  };
  // A template with a fixed die draws its floorplan here (fppreview.js);
  // one sized from the design says the size comes with the first run.
  const paintFp = async () => {
    const el = $("wiz-fp");
    if (!el) return;
    const size = tpl.size || {};
    if (size.FP_SIZING !== "absolute" || !size.DIE_AREA) { el.hidden = true; return; }
    try {
      const fp = await import("./fppreview.js");
      const k = await fp.estimateInputs({ pdk: pdkSel.value, scl: $("wiz-scl").value, project: false });
      if (!el.isConnected) return;
      const margins = { ...(k.margins || {}) };
      for (const m of ["LEFT_MARGIN_MULT", "RIGHT_MARGIN_MULT", "TOP_MARGIN_MULT", "BOTTOM_MARGIN_MULT"]) if (size[m] != null) margins[m] = size[m];
      fp.mountFloorplan(el, { values: size, inputs: { ...k, margins }, compact: true });
    } catch (_e) { el.hidden = true; }
  };
  pdkSel.addEventListener("change", paintScl);

  const paintWhere = () => {
    const name = $("wiz-name").value.trim() || defaultName(tpl);
    const dest = joinPath(projectsDir(), name);
    const where = shown(dest);
    $("wiz-where").textContent = $("wiz-where").title = where;
    const help = $("wiz-where-help");
    const spaced = /\s/.test(dest);
    help.classList.toggle("wiz-warn", spaced || _taken.has(name));
    help.textContent = spaced ? "This path has a space in it, and LibreLane can't run a design from such a folder. Pick another with Change…."
      : _taken.has(name) ? "There's already a folder named " + name + " here. Pick another name, such as " + freeName(name, _taken) + "."
      : _parent ? "A new folder is created inside the folder you chose."
      : where.startsWith("\\\\wsl") ? (_defaults && _defaults.linux_home_reason === "space"
        ? "Inside the LanEx environment, as your Windows user name has a space."
        : "Inside the LanEx environment, as LanEx couldn't find your Windows user folder.")
      : _defaults && _defaults.wsl ? "Your LanEx projects folder, next to your other Windows files." : "Your LanEx projects folder.";
  };
  // The names already taken there: the suggested name is a free one, so a
  // second run of the guide makes blinky-2 rather than failing.
  let nameTyped = false;
  const readParent = () => readTaken(projectsDir()).then(() => {
    if (!body.isConnected || !$("wiz-name")) return;
    if (!nameTyped) {
      const el = $("wiz-name");
      el.value = freeName(defaultName(tpl), _taken);
      if (document.activeElement === el) el.select();
    }
    paintWhere();
  });
  const paintClk = () => {
    if (!$("wiz-clk")) return;
    const ns = $("wiz-clk")._clock ? $("wiz-clk")._clock.get() : parseFloat($("wiz-clk").value);
    const f = ns > 0 ? freqText(ns) : "";
    $("wiz-clk-help").textContent = f ? "A period in ns or a frequency such as 100 MHz. A shorter period is a faster chip and harder to meet."
      : "A positive number of ns, such as 10 (100 MHz), or a frequency such as 100 MHz.";
  };
  const paintTop = () => {
    const h = $("wiz-top-help");
    if (!h) return;
    const typed = $("wiz-top").value.trim();
    const ident = verilogIdentifier(typed || tpl.top, tpl.top);
    const n = ident + ($("wiz-lang").value === "systemverilog" ? ".sv" : ".v");
    const sv = $("wiz-lang").value === "systemverilog";
    // A name Verilog can't use is made into one, and said, before anything
    // is created.
    const fixed = typed && ident !== typed;
    h.classList.toggle("wiz-warn", !!fixed);
    h.innerHTML = (fixed ? "A module name is letters, digits and _, not starting with a digit, so it will be <code>" +
        fmt.escape(ident) + "</code>. " : "Your top-level module. ") +
      "LanEx starts <code>src/" + fmt.escape(n) + "</code> with a small module of that name. " +
      (sv ? "SystemVerilog is for code that uses <code>logic</code>, <code>always_ff</code> and the like; the open flow supports its common subset."
        : "Verilog is the open flow's native language: LibreLane's examples and Tiny Tapeout designs use it.");
  };
  if ($("wiz-top")) {
    $("wiz-top").addEventListener("input", paintTop);
    $("wiz-lang").addEventListener("change", paintTop);
    paintTop();
  }
  paintWhere();
  paintClk();
  paintScl();
  readParent();
  $("wiz-name").addEventListener("input", () => { nameTyped = true; $("wiz-err").textContent = ""; paintWhere(); });
  $("wiz-scl")?.addEventListener("change", paintFp);
  $("wiz-change").addEventListener("click", async () => {
    const { pickFolder } = await import("./fs.js");
    const dir = await pickFolder({ title: "Where should the project folder go?", use: "Put it here", seed: projectsDir() });
    if (dir) { _parent = dir; paintWhere(); readParent(); }
  });
  $("wiz-back").addEventListener("click", () => renderStep1(body));
  $("wiz-create").addEventListener("click", () => doCreate(body, tpl));
  // Enter anywhere in the form creates the project.
  body.querySelector(".wiz-form").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName !== "SELECT") { e.preventDefault(); doCreate(body, tpl); }
  });
  // An example's top module is defined by its Verilog; only a blank project
  // lets you name it (the starter module and file are renamed to match).
  if (tpl.kind !== "blank") $("wiz-top")?.closest(".wiz-field").remove();
  if (tpl.fixed_config) {
    // This example ships its own tuned config: the fields below don't apply.
    body.querySelectorAll("#wiz-top, #wiz-clk").forEach((el) => el.closest(".wiz-field")?.remove());
    const note = document.createElement("p");
    note.className = "wiz-help wiz-fixed";
    note.textContent = "This example comes with its own tuned settings (clock, floorplan and power grid) for sky130 and gf180, so there's nothing else to set.";
    body.querySelector(".wiz-form").appendChild(note);
  }
  $("wiz-name").focus();
  $("wiz-name").select();
}

async function doCreate(body, tpl) {
  const $ = (id) => body.querySelector("#" + id);
  const name = $("wiz-name").value.trim();
  const err = $("wiz-err");
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(name)) {
    err.textContent = "Use letters, numbers, - and _ for the name, starting with a letter or number.";
    $("wiz-name").focus();
    return;
  }
  const dest = joinPath(projectsDir(), name);
  // The server makes the same valid Verilog name (scaffold.verilog_identifier).
  const top = verilogIdentifier(($("wiz-top") ? $("wiz-top").value.trim() : "") || tpl.top, tpl.top);
  const pdk = $("wiz-pdk").value || "";
  const scl = $("wiz-scl").value || "";
  const clkRaw = $("wiz-clk") ? $("wiz-clk").value.trim() : "";
  // The clock helper reads "100 MHz" as 10 ns; the server gets ns.
  const clkNs = $("wiz-clk") && $("wiz-clk")._clock ? $("wiz-clk")._clock.get() : NaN;
  const clk = clkRaw === "" ? NaN : (clkNs > 0 ? clkNs : NaN);
  const sv = $("wiz-lang") && $("wiz-lang").value === "systemverilog";
  const withTb = !!($("wiz-tb") && $("wiz-tb").checked);
  // Settings that can't build are refused here, in plain words, before any
  // folder is made.
  const problem = createProblem({ dest, clock: clk, hasClock: !!$("wiz-clk"), pdk, pdks: state.pdks, tpl });
  if (problem) {
    err.textContent = problem;
    if (/Tools/.test(problem)) {
      const b = document.createElement("button");
      b.className = "btn btn-sm btn-ghost";
      b.textContent = "Open Tools";
      b.addEventListener("click", () => { close(); import("./nav.js").then((m) => m.go("tools")); });
      err.append(" ", b);
    }
    (/clock/.test(problem) ? $("wiz-clk") : /space/.test(problem) ? $("wiz-change") : $("wiz-pdk"))?.focus();
    return;
  }
  if (_taken.has(name)) {
    const free = freeName(name, _taken);
    $("wiz-name").value = free;
    $("wiz-name").dispatchEvent(new Event("input"));   // repaints the location line
    err.textContent = "There's already a folder named " + name + " there, so the name is now " + free + ". Press Create again, or change it.";
    $("wiz-name").focus();
    $("wiz-name").select();
    return;
  }
  const btn = $("wiz-create");
  btn.disabled = true; btn.textContent = "Creating…";
  try {
    const res = await api.projectNew({
      dest_dir: dest, template: tpl.name, top, pdk, scl,
      clock_period: isNaN(clk) ? null : clk,
      language: sv ? "systemverilog" : "verilog",
    });
    close();
    const setup = await import("./setup.js");
    const input = document.getElementById("design-dir-input");
    if (input) input.value = res.design_dir;
    const topName = res.top || "top";
    const ext = sv ? ".sv" : ".v";
    // The usual first pair: the design and a bench that drives it.
    let tbRel = null;
    if (tpl.kind === "blank" && withTb) {
      try {
        const tb = await api.newProjectFile({ design_dir: res.design_dir, kind: sv ? "tb_sv" : "tb_v",
          name: topName + "_tb", top: topName, clock: "clk", use: false });
        tbRel = tb.rel;
      } catch (ex) {
        toast.show("The project is ready, but the testbench wasn't written: " + (ex.message || ex), "warn", 6000);
      }
    }
    await setup.adoptDesignDir(res.design_dir);
    if (tpl.kind === "blank") {
      toast.show("Created " + name + ". src/" + topName + ext + (tbRel ? " and " + tbRel : "") +
        " are open in the RTL IDE.", "success");
      (await import("./nav.js")).go("ide", {}, { link: false });
      const ide = await import("./ide/main.js");
      await ide.initIde({ designDir: res.design_dir, runMode: state.runMode });
      if (tbRel) await ide.openInIde({ rel: tbRel });
      await ide.openInIde({ rel: "src/" + topName + ext });
    } else {
      const { go } = await import("./nav.js");
      go("setup", {}, { link: false });
      // Say what comes next (the first-chip guide points at it itself).
      if (document.body.classList.contains("guide-on")) toast.show("Created " + name + ".", "success");
      else toast.show("Created " + name + ". It's ready to run.", "success", 10000, [
        tpl.has_testbench ? { label: "Simulate first", run: () => go("ide") } : null,
        { label: "Go to Run", run: () => {
          go("setup", { section: "setup-preflight" }, { link: false });
          setTimeout(() => document.getElementById("btn-run")?.focus({ preventScroll: true }), 400);
        } },
      ]);
    }
  } catch (ex) {
    err.textContent = ex.message || String(ex);
    btn.disabled = false; btn.textContent = "Create project";
  }
}

export function setupWizard() {
  document.getElementById("btn-new-project")?.addEventListener("click", openWizard);
}
