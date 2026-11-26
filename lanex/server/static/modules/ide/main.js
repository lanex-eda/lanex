// main.js — the RTL IDE. Runs both as the standalone /ide page and embedded as
// the "RTL IDE" tab in the cockpit. The layout follows the editors and FPGA
// tools people already know: an explorer with the project's files sorted by
// what they are for (design, simulation, constraints, project), open-file tabs
// over a syntax-highlighting editor, and a full-width bottom panel with the
// Verilator syntax check (Problems), the simulator's output and the waveform.
// The day-to-day loop is write → Check syntax → Simulate, before the long
// RTL-to-GDS run. Shares api.js + SSE; no bundler.
import { api, sse, fmt } from "../api.js";
import { Editor } from "./editor.js";
import { readVCD, formatTime, parseTimescale, valueAt, plainTicks } from "./vcd.js";
import { explain, explainMessage, TESTBENCH_STYLE } from "./lintinfo.js";
import { WaveView } from "./waves.js";
import { setupFullscreen } from "../fullscreen.js";
import { applyZoom, currentZoom, isAutoZoom } from "../zoom.js";
import { getPref } from "../prefs.js";
import { when as MFwhen } from "../metricfmt.js";
import { emptyHtml, EMPTY_WORDS } from "../empty.js";
import { openLayer, closeLayer, isLayerOpen } from "../overlay.js";
import { refreshAction, conflictKind, lineDiff, isConfigFile } from "./filesync.js";
import { addProjectFiles } from "../addfiles.js";
import { initNav, noteRecent, declHover, refreshIndexStatus, quickOpen, pushHere } from "./nav.js";
import { initSearch, searchPaneHtml, focusSearch } from "./search.js";
import { initSimRun, settingsHtml, simPaneExtras, payloadFor, benchChanged, resetBatch, batchRunning, timeWords } from "./simrun.js";
import { initSchematic, schematicPaneHtml, markStale } from "./schematic.js";
import { isPickerOpen } from "./picker.js";

const state = {
  designDir: null, files: [], top: null, topAbs: null,
  tabs: [], active: null, explicitOpen: false, diags: [],
  runMode: "container", wave: null, lastWavePath: null, gtkwaveInstallArmed: false,
};
let _inited = false;

// The standalone /ide page hardcodes `theme-dark` and doesn't run the main
// app's setupTheme(), so a pop-out always opened dark even when the user picked
// light. Apply the persisted `ll.theme` here, and live-sync if it changes in the
// main window (cross-window `storage` event). Idempotent for the embedded tab.
function applyTheme() {
  let light = false;
  try { light = localStorage.getItem("ll.theme") === "light"; } catch (_e) {}
  document.body.classList.toggle("theme-light", light);
  document.body.classList.toggle("theme-dark", !light);
}
function setupThemeSync() {
  applyTheme();
  applyZoom(currentZoom());            // pop-out matches the cockpit's UI zoom
  window.addEventListener("storage", (e) => {
    if (!e || e.key === "ll.theme") {
      applyTheme();
      document.dispatchEvent(new CustomEvent("g:theme_changed", { detail: { dark: !document.body.classList.contains("theme-light") } }));
    }
    if (!e || e.key === "ll.zoom") applyZoom(currentZoom());
  });
  // The automatic size follows this window's own size.
  let t = 0;
  window.addEventListener("resize", () => {
    if (!isAutoZoom()) return;
    clearTimeout(t);
    t = setTimeout(() => applyZoom(currentZoom()), 120);
  });
}

// Entry point. opts.designDir/runMode override discovery (the embedded tab knows
// them from the main app); the standalone page discovers them itself.
export async function initIde(opts = {}) {
  setupThemeSync();
  if (_inited) {
    // Re-activated tab: another project means its own set of tabs; the same
    // project just picks up files made elsewhere (Setup's New file, Add files).
    if (opts.runMode) state.runMode = opts.runMode;
    if (opts.designDir && opts.designDir !== state.designDir) switchProject(opts.designDir);
    await loadTree();
    await loadTestbenches();
    autoOpen();
    // Files may have changed while another tab was showing (Setup writes the config).
    refreshFromDisk();
    refreshIndexStatus({ refresh: true });
    if (!state.vhdlCaps) loadCaps();
    return;
  }
  _inited = true;
  ensureIdeChrome();

  if (opts.designDir) {
    state.designDir = opts.designDir;
  } else {
    try { const dd = await api.designDir(); state.designDir = dd && dd.design_dir; } catch (_e) {}
    const qd = new URLSearchParams(location.search).get("design_dir");
    if (qd) { try { await api.setDesignDir(qd); state.designDir = qd; } catch (_e) {} }
  }
  try { state.runMode = opts.runMode || localStorage.getItem("ll.runMode") || "container"; } catch (_e) {}

  setDesignLabel();
  setupFullscreen();   // idempotent; needed on the standalone /ide page too
  window.ideEditor = new Editor(document.getElementById("ide-editor"), {
    onChange: onEditorChange, onCursor: showCursor, onSave: saveFile,
  });
  window.ideEditor.hoverProvider = hover;
  wireNavigation();
  byId("ide-save", (b) => b.addEventListener("click", saveFile));
  byId("ide-lint", (b) => b.addEventListener("click", lint));
  setupAutoLint();
  byId("ide-sim", (b) => b.addEventListener("click", simulate));
  byId("ide-sim-stop", (b) => b.addEventListener("click", stopSim));
  byId("ide-find", (b) => b.addEventListener("click", () => window.ideEditor.openFind()));
  byId("ide-wave-host", (host) => {
    state.wave = new WaveView(host, {
      idPrefix: "ide-wave-",
      // One saved signal list per project and testbench.
      layoutKey: () => "ll.wave:" + (state.designDir || "") + "|" + (state.waveTb || ""),
      onGoto: gotoDeclaration,
      onGtkwave: openInGtkwave,
      onSimulate: simulate,
      onNote: note,
    });
  });
  byId("ide-new-file", (b) => b.addEventListener("click", () => newFile()));
  wireUpload();
  wireTree();
  wireTabs();
  wirePanel();
  byId("ide-tb", (sel) => sel.addEventListener("change", () => { byId("ide-top", (x) => { x.value = ""; }); deriveTopFromTb(); paintSimPick(); }));
  wireSimPick();
  wireLog();
  wireProblems();
  renderProblems();
  wireKeys();
  window.addEventListener("beforeunload", (e) => {
    if (!unsavedTabs().length) return;
    e.preventDefault();
    e.returnValue = "";
  });

  wireDiskSync();
  sse.open();
  sse.on(onEvent);
  await loadTree();
  await loadTestbenches();
  autoOpen();
  refreshIndexStatus();
  loadCaps();
}

function byId(id, fn) { const el = document.getElementById(id); if (el) fn(el); }

// ------------------------------------------------------------------ navigation, search, run all, schematic
// The panes and controls these add are made here, so the cockpit's embedded
// IDE (index.html) and the /ide pop-out (ide.html) get the same ones.

const IC_GO = "<svg viewBox='0 0 24 24' width='15' height='15' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M14 4h6v6M20 4l-8 8M10 6H5v13h13v-5'/></svg>";
const IC_FIND = "<svg viewBox='0 0 24 24' width='15' height='15' fill='none' stroke='currentColor' stroke-width='1.7' stroke-linecap='round' aria-hidden='true'><circle cx='11' cy='11' r='6.5'/><path d='M16 16l4.5 4.5'/></svg>";

function ensureIdeChrome() {
  const ptabs = document.querySelector(".ide-ptabs");
  if (!ptabs || ptabs.querySelector("[data-ptab='search']")) return;
  const tab = (name, label, extra = "") => {
    const b = document.createElement("button");
    b.className = "ide-ptab";
    b.setAttribute("role", "tab");
    b.dataset.ptab = name;
    b.innerHTML = label + extra;
    return b;
  };
  const probs = ptabs.querySelector("[data-ptab='problems']");
  probs.after(tab("search", "Search"), tab("refs", "References", " <span class='pcount' id='ide-refs-count'></span>"));
  ptabs.querySelector("[data-ptab='wave']").after(tab("schem", "Schematic"));
  const panel = document.getElementById("ide-panel");
  const pane = (name, id, html) => {
    const d = document.createElement("div");
    d.className = "ide-pane";
    d.dataset.pane = name;
    if (id) d.id = id;
    d.innerHTML = html;
    return d;
  };
  panel.querySelector("[data-pane='problems']").after(pane("search", "ide-search", searchPaneHtml()),
    pane("refs", null, "<div id='ide-refs' class='ide-diags ide-refs'></div>"));
  panel.querySelector("[data-pane='wave']").after(pane("schem", "ide-schem", schematicPaneHtml()));
  // Simulation: Run all next to the bench, run settings under the bar, the results table above the output
  const bar = document.querySelector(".ide-sim-bar");
  if (bar) {
    const stop = document.getElementById("ide-sim-stop");
    stop.insertAdjacentHTML("beforebegin", simPaneExtras());
    bar.insertAdjacentHTML("afterend", settingsHtml());
    document.getElementById("ide-sim-status")?.insertAdjacentHTML("afterend", "<div class='sr-results' id='sr-results' hidden></div>");
  }
  // Toolbar: go to a file or symbol, search every file (next to Find, which
  // is this file only); the index's state at the explorer's foot.
  const find = document.getElementById("ide-find");
  if (find) {
    find.insertAdjacentHTML("beforebegin",
      "<button class='iconbtn iconbtn-sm ide-tbi' id='ide-goto' aria-label='Go to a file or symbol (Ctrl P)' title='Go to a file or symbol (Ctrl P)'>" + IC_GO + "</button>" +
      "<button class='iconbtn iconbtn-sm ide-tbi' id='ide-search-btn' aria-label='Search every file (Ctrl Shift F)' title='Search every file in the project (Ctrl Shift F)'>" + IC_FIND + "</button>");
    find.title = "Find and replace in this file (Ctrl F)";
  }
  document.querySelector(".ide-side")?.insertAdjacentHTML("beforeend",
    "<button class='ide-index' id='ide-index-status' hidden title=''></button>");
}

// Unsaved tabs, for the index's answers to match the screen.
function dirtyTexts() {
  stash();
  const out = {};
  let total = 0;
  for (const t of state.tabs) {
    if (t.text === t.saved || !/\.(s?v|vh|svh|vhdl?)$/i.test(t.rel)) continue;
    total += t.text.length;
    if (total > 8 * 1024 * 1024) break;
    out[t.rel] = t.text;
  }
  return out;
}

// Open a project file and put the cursor on (line, col), selecting len chars.
async function openAt(rel, line, col, len) {
  const f = projectFile(rel) || state.files.find((x) => x.relpath === rel);
  if (!f) { note(rel + " isn't one of this project's files.", true); return; }
  if (state.active !== f.abspath) await openFile(f.abspath, f.relpath);
  if (line > 0 && window.ideEditor && window.ideEditor.relPath === f.relpath) window.ideEditor.selectAt(line, col || 1, len || 0);
}

let _nav = null;
function wireNavigation() {
  _nav = initNav({
    editor: () => window.ideEditor,
    activeRel: () => { const t = activeTab(); return t ? t.rel : null; },
    dirty: dirtyTexts,
    files: () => state.files,
    topRel: () => { const f = state.topAbs && state.files.find((x) => x.abspath === state.topAbs); return f ? f.relpath : null; },
    openAt, setPanel, note, designDir: () => state.designDir,
  });
  initSearch({
    dirty: dirtyTexts,
    dirtyRels: () => { stash(); return state.tabs.filter((t) => t.text !== t.saved).map((t) => t.rel); },
    note, openAt, setPanel,
    selection: () => {
      const v = window.ideEditor && window.ideEditor.view;
      if (!v) return "";
      const s = v.state.selection.main;
      return s.empty ? "" : v.state.sliceDoc(s.from, s.to);
    },
    afterWrite: async (rels) => {
      await refreshFromDisk();
      if (rels.some((r) => isConfigFile(r))) document.dispatchEvent(new CustomEvent("g:config_changed", { detail: { from: "ide" } }));
      if (rels.some((r) => /\.(s?v|vh|svh|vhdl?)$/i.test(r))) markStale();
      if (state.explorerView === "hier") renderHierarchy();
    },
  });
  initSimRun({
    designDir: () => state.designDir,
    tb: () => (document.getElementById("ide-tb") || {}).value || "",
    benches: () => state.testbenches || [],
    base: () => simBase(""),
    saveAll,
    setPanel, note, appendLog, grow: () => growPanelFor(),
    scrollLogEnd: () => { const el = document.getElementById("ide-log"); if (el) el.scrollTop = el.scrollHeight; },
    showWave: (vcd, failTime, tb) => showWaveAt(vcd, failTime, tb),
    setBusy: (on) => { byId("ide-sim", (b) => { b.disabled = !!on || !!state.simRunning || !state.designDir; }); byId("ide-sim-pick", (b) => { b.disabled = !!on || !!state.simRunning || !state.designDir; }); byId("ide-sim-stop", (b) => { b.hidden = !on && !state.simRunning; }); },
    onBatchDone: (rows) => {
      state.lastBatch = { at: Date.now(), rows: rows.map((r) => ({ tb: r.tb, state: r.state })) };
      recordRtlStatus();
    },
    defaultLimit: () => simTimeLimit(),
    configDefines: () => state.configDefines || [],
  });
  initSchematic({ openAt, note, setPanel, top: () => state.configTop || state.top });
  byId("ide-goto", (b) => b.addEventListener("click", () => quickOpen("")));
  byId("ide-search-btn", (b) => b.addEventListener("click", () => focusSearch({})));
  byId("ide-index-status", (b) => b.addEventListener("click", () => refreshIndexStatus({ refresh: true })));
  byId("ide-tb", (sel) => sel.addEventListener("change", () => benchChanged()));
}

// The hover over a name: its declaration, then its value in the loaded
// simulation (one tooltip, not two).
async function hover(word, rel, pos) {
  const [decl, value] = await Promise.all([declHover(word, rel, pos).catch(() => null), valueHover(word, rel).catch(() => null)]);
  if (!decl && !value) return null;
  if (!decl) return value;
  if (!value) return decl;
  const both = document.createElement("div");
  both.className = "cm-hoverstack";
  both.append(decl, value);
  return both;
}

// What the image can run here: VHDL through GHDL (experimental), Yosys.
async function loadCaps() {
  if (!state.files.some((f) => /\.vhdl?$/i.test(f.relpath))) { state.vhdlCaps = null; return; }
  try {
    let c = await api.ideCaps(false);
    if (c.vhdl && c.vhdl.state === "checking") c = await api.ideCaps(true);
    state.vhdlCaps = c.vhdl || null;
  } catch (_e) { state.vhdlCaps = null; }
  renderTree();
}

// The shared part of a simulation request (Simulate and Run all).
function simBase(tb) {
  const benches = new Set(state.testbenches || []);
  const sources = state.files.filter((f) => /\.s?v$/i.test(f.relpath) && f.relpath !== tb &&
      (f.role !== "testbench" || !benches.has(f.relpath))).map((f) => f.relpath);
  const helpers = state.files.filter((f) => f.role === "testbench" && /\.s?v$/i.test(f.relpath) &&
      f.relpath !== tb && !benches.has(f.relpath)).map((f) => f.relpath);
  return { sources, trace: "vcd", run_mode: state.runMode,
    sim_engine: (document.getElementById("ide-sim-engine") || {}).value || "auto",
    include_dirs: includeDirs(), from_config: true, extra_sources: helpers, testbenches: [...benches] };
}

// Open a dump and put the cursor (and a FAIL line) at the failure.
async function showWaveAt(vcd, failTime, tb) {
  if (!vcd) { note("That run wrote no waveform.", true); return; }
  if (tb) state.waveTb = tb;
  await loadWaveform(vcd, false);
  if (failTime && state.wave && state.wave.vcd) {
    const t = failTicks(failTime, state.wave.vcd);
    if (t !== null) {
      state.wave.showTime(t, { fail: true });
      note("Waveform at the failure, " + formatTime(Math.round(t), state.wave.vcd.tick) + ".");
    }
  }
}

// A failure time from the server (simulate.judge) in the dump's own ticks.
const UNIT_EXP = { s: 0, ms: -3, us: -6, "µs": -6, ns: -9, ps: -12, fs: -15 };
export function failTicks(ft, vcd) {
  if (!ft || !vcd) return null;
  if (ft.end) return vcd.end;
  if (ft.ticks !== undefined && ft.ticks !== null) {
    // ticks in the simulator's precision ("1ps"), normally the dump's own unit
    const m = String(ft.unit || "").match(/^(\d+)\s*([munpf]?s)$/);
    const tick = vcd.tick || { mult: 1, exp: 0 };
    if (!m || UNIT_EXP[m[2]] === undefined) return Math.min(vcd.end, ft.ticks);
    const t = ft.ticks * (+m[1]) * Math.pow(10, UNIT_EXP[m[2]] - tick.exp) / (tick.mult || 1);
    return Math.min(vcd.end, Math.round(t));
  }
  if (ft.value !== undefined && UNIT_EXP[ft.unit] !== undefined) {
    const tick = vcd.tick || { mult: 1, exp: 0 };
    const t = ft.value * Math.pow(10, UNIT_EXP[ft.unit] - tick.exp) / (tick.mult || 1);
    return Math.min(vcd.end, Math.round(t));
  }
  return null;
}

// Open tabs per project, so going to another project and back finds them as
// they were, unsaved edits included (the server only writes into the open
// project, so they can't be saved from elsewhere).
const _tabsByProject = new Map();

// Another project was opened: nothing of the last one stays on screen (its
// files, problems, simulation output and waveform belong to it).
function switchProject(dir) {
  stash();
  if (state.designDir) _tabsByProject.set(state.designDir, { tabs: state.tabs, active: state.active });
  const ed = window.ideEditor;
  if (ed) { ed.load(null, ""); ed.states.clear(); }
  state.designDir = dir;
  const kept = _tabsByProject.get(dir);
  state.tabs = kept ? kept.tabs : [];
  state.active = null;
  state.explicitOpen = false;
  state.diags = [];
  state.simDiags = [];
  state.lintChecked = false;
  state.lintAt = 0;
  state.lintFailed = null;
  state.scopeMap = null;
  state.lastWavePath = null;
  state.waveTb = null;
  state.gtkwaveInstallArmed = false;
  if (state.simRun && !state.simRun.done) state.simRun.orphan = true;
  else state.simRun = null;
  state.vhdlCaps = null;
  state.lastBatch = null;
  resetBatch();
  if (state.wave) state.wave.clear();
  setWavePartial(false);
  note("");
  byId("ide-log", (el) => { el.innerHTML = ""; });
  byId("ide-top", (x) => { x.value = ""; });
  byId("ide-sim-status", (el) => { el.hidden = true; });
  byId("ide-sim-count", (el) => { el.textContent = ""; el.className = "pcount"; });
  byId("ide-open", (el) => { el.textContent = "No file open"; el.title = ""; });
  setDesignLabel();
  renderProblems();
  renderTabs();
  const back = kept && state.tabs.find((t) => t.abs === kept.active);
  if (back) activate(back);
}

// The IDE is on screen: the /ide page, or the cockpit's RTL IDE tab while it
// is the one showing.
function ideShowing() {
  const embed = document.querySelector(".ide-embed");
  return !!embed && embed.getClientRects().length > 0;
}

// Ctrl+S saves the open file wherever the focus is in the IDE (the explorer,
// the waveform, a panel), not only inside the editor, instead of the
// browser's "Save page as".
// A dialog is on screen (its backdrop stays in the page, hidden, between uses).
function dialogOpen() {
  return [...document.querySelectorAll(".dlg-backdrop")].some((el) => !el.hidden && el.getClientRects().length > 0);
}

function wireKeys() {
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || (e.key !== "s" && e.key !== "S")) return;
    if (e.defaultPrevented || !ideShowing() || dialogOpen()) return;
    if (e.target && e.target.closest && e.target.closest(".cm-editor")) return;   // the editor's own keymap has it
    e.preventDefault();
    if (activeTab()) saveFile();
  });
  // The code editors' keys for getting around (VS Code's): Ctrl+P a file,
  // Ctrl+Shift+O a symbol here, Ctrl+Shift+F / H search and replace in every
  // file. Ctrl+T stays the browser's (a new tab), so project symbols are # in
  // Ctrl+P. Only while the IDE is on screen and no dialog is open.
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.defaultPrevented || !ideShowing()) return;
    if (dialogOpen() || isPickerOpen()) return;
    const k = e.key.toLowerCase();
    if (k === "p" && !e.shiftKey) { e.preventDefault(); quickOpen(""); }
    else if (k === "o" && e.shiftKey) { e.preventDefault(); quickOpen("@"); }
    else if (k === "f" && e.shiftKey) { e.preventDefault(); focusSearch({}); }
    else if (k === "h" && e.shiftKey) { e.preventDefault(); focusSearch({ replace: true }); }
  }, true);
}

// Unsaved edits in any project's tabs: the browser asks before the page goes.
function unsavedTabs() {
  const all = state.tabs.concat(...[..._tabsByProject.entries()].filter(([d]) => d !== state.designDir).map(([, v]) => v.tabs));
  stash();
  return all.filter((t) => t.text !== t.saved);
}

// /mnt/c/Users/... is C:\Users\... to a Windows user.
function shownPath(p) {
  const m = String(p || "").match(/^\/mnt\/([a-z])(\/.*)?$/);
  return m ? m[1].toUpperCase() + ":" + (m[2] || "/").replace(/\//g, "\\") : String(p || "");
}

function setDesignLabel() {
  byId("ide-design", (el) => {
    el.textContent = state.designDir ? shownPath(state.designDir) : "No project open";
    el.title = state.designDir || "";
  });
}

// ------------------------------------------------------------------ new files

// Every kind of file a LibreLane project uses, most-used first. The server
// writes the starter content (controller/projectfiles.py) and, for the kinds
// the flow reads through a config variable, points the config at the new file.
// Verilog or SystemVerilog is a separate choice, so the list stays short.
const KINDS = [
  { id: "module", label: "Design module", dir: "src/", hdl: true, ph: "alu",
    help: "Logic that goes into the chip. Joins the config's file list." },
  { id: "tb", label: "Testbench", dir: "verify/", hdl: true, ph: "alu_tb",
    help: "Drives and checks a module in simulation. Never built into the chip." },
  { id: "sdc", label: "Timing constraints", dir: "src/", ext: ".sdc", ph: "timing", use: true,
    help: "Only for I/O delays, extra clocks or false paths. The clock period alone needs none." },
  { id: "pins", label: "Pin order", dir: "", ext: ".cfg", ph: "pin_order", use: true,
    help: "Which chip edge each port sits on. Without it the flow places pins itself." },
  { id: "header", label: "Include file", dir: "src/", hdl: true, ph: "defs",
    help: "Shared `define macros and parameters, pulled in with `include." },
  { id: "memory", label: "Memory contents", dir: "src/", ext: ".hex", ph: "rom",
    help: "Starting values for a small ROM, loaded with $readmemh." },
  { id: "other", label: "Other text file", dir: "", ext: "", ph: "notes.md",
    help: "Notes, scripts or settings: .md, .txt, .tcl, .json, .yaml." },
];
// The server's kind for (what, language).
function serverKind(id, sv) {
  if (id === "module") return sv ? "module_sv" : "module_v";
  if (id === "tb") return sv ? "tb_sv" : "tb_v";
  if (id === "header") return sv ? "header_svh" : "header_vh";
  return id;
}
const LEGACY = { src: "module", tb: "tb", module_v: "module", module_sv: "module", tb_v: "tb", tb_sv: "tb",
  header_vh: "header", header_svh: "header" };

// The project's language: the top module's file decides, else whichever
// extension the design files use most. New projects start in Verilog.
function projectIsSv() {
  if (state.topAbs) return /\.sv$/i.test(state.topAbs);
  const design = state.files.filter((f) => f.role === "design" || f.role === "testbench");
  const sv = design.filter((f) => /\.sv$/i.test(f.relpath)).length;
  return sv > design.length - sv;
}

export async function newFile(kind) {
  if (!state.designDir) { note("Open a project first (Setup tab).", true); return; }
  let id = LEGACY[kind] || kind || "module";
  if (!KINDS.some((k) => k.id === id)) id = "module";
  let sv = /_sv$|_svh$/.test(kind || "") ? true : projectIsSv();
  const { customDialog } = await import("../dialog.js");
  let picked = null;
  const list = KINDS.map((k, i) =>
    (i === 2 ? "<div class='nf-sep' aria-hidden='true'></div>" : "") +
    "<label class='nf-kind'><input type='radio' name='nf-kind' value='" + k.id + "'" + (k.id === id ? " checked" : "") + "/>" +
    "<span class='nf-kind-t'>" + fmt.escape(k.label) + "</span><span class='nf-kind-d'>" + fmt.escape(k.help) + "</span></label>").join("");
  const res = await customDialog({
    title: "New file",
    bodyHtml:
      "<div class='newfile'>" +
      "<div class='nf-kinds' role='radiogroup' aria-label='Kind of file'>" + list + "</div>" +
      "<div class='nf-row'>" +
      "<label class='nf-field nf-grow'><span id='nf-name-label'>Name</span><input id='nf-name' type='text' spellcheck='false' autocomplete='off'/></label>" +
      "<span class='nf-field' id='nf-lang-field'><span>Language</span><span class='nf-seg' role='group' aria-label='Language'>" +
      "<button type='button' data-sv='0'>Verilog .v</button><button type='button' data-sv='1'>SystemVerilog .sv</button></span></span>" +
      "</div>" +
      "<p class='nf-path'>Creates <code id='nf-path'></code></p>" +
      "<label class='nf-use' id='nf-use-row'><input type='checkbox' id='nf-use' checked/> <span id='nf-use-text'></span></label>" +
      "</div>",
    buttons: [
      { label: "Cancel", value: null, cls: "btn-ghost" },
      { label: "Create", value: "ok", cls: "btn-primary" },
    ],
    onMount: (back) => {
      const input = back.querySelector("#nf-name");
      const create = back.querySelector(".btn-primary");
      const cur = () => KINDS.find((k) => k.id === back.querySelector("input[name='nf-kind']:checked").value);
      const ext = (k) => (k.id === "header" ? (sv ? ".svh" : ".vh") : k.hdl ? (sv ? ".sv" : ".v") : k.ext);
      const paint = () => {
        const k = cur();
        back.querySelector("#nf-name-label").textContent = k.id === "module" || k.id === "tb" ? "Module name" : "File name";
        if (!input.dataset.touched) input.value = k.id === "tb" && state.top ? state.top + "_tb" : k.ph;
        back.querySelector("#nf-lang-field").hidden = !k.hdl;
        back.querySelectorAll(".nf-seg button").forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.sv === "1") === sv)));
        const name = (input.value || "").trim() || k.ph;
        const e = ext(k);
        back.querySelector("#nf-path").textContent = k.dir + (e && !name.toLowerCase().endsWith(e) ? name + e : name);
        back.querySelector("#nf-use-row").hidden = !k.use;
        back.querySelector("#nf-use-text").textContent = k.id === "sdc"
          ? "Use it for this project's runs (sets PNR_SDC_FILE and SIGNOFF_SDC_FILE in the config)"
          : "Use it for this project's runs (sets IO_PIN_ORDER_CFG in the config)";
      };
      back.querySelectorAll("input[name='nf-kind']").forEach((r) => r.addEventListener("change", paint));
      back.querySelectorAll(".nf-seg button").forEach((b) => b.addEventListener("click", () => { sv = b.dataset.sv === "1"; paint(); }));
      input.addEventListener("input", () => { input.dataset.touched = "1"; paint(); });
      paint();
      // Read the fields before the dialog closes and removes them.
      create?.addEventListener("click", () => {
        picked = { kind: serverKind(cur().id, sv), name: (input.value || "").trim(), use: back.querySelector("#nf-use").checked };
      }, true);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); create?.click(); }
      });
      setTimeout(() => { input.focus(); input.select(); }, 0);
    },
  });
  if (res !== "ok" || !picked) return;
  if (!picked.name) { note("Give the file a name.", true); return; }
  // SDC and pin files are written for the top module named in the config.
  let top = null, clock = null;
  if (picked.kind === "sdc" || picked.kind === "pins" || picked.kind.startsWith("tb")) {
    try {
      const info = await api.designInfo({ design_dir: state.designDir });
      top = (info.values && info.values.DESIGN_NAME) || info.suggested_top || null;
      const cp = info.values && info.values.CLOCK_PORT;
      clock = Array.isArray(cp) ? cp[0] : cp || null;
    } catch (_e) { /* the server says what's missing */ }
  }
  try {
    const r = await api.newProjectFile({ design_dir: state.designDir, kind: picked.kind, name: picked.name,
      top, clock, use: picked.use });
    await loadTree();
    await loadTestbenches();
    await openFile(r.abs, r.rel);
    const cfg = r.config && r.config.changed && r.config.changed.length
      ? " and updated " + r.config.name + " (" + r.config.changed.join(", ") + ")" : "";
    note(r.config_error ? "Created " + r.rel + ", but the config wasn't updated: " + r.config_error
      : "Created " + r.rel + cfg + ".", !!r.config_error);
    document.dispatchEvent(new CustomEvent("g:sources_changed"));
  } catch (ex) {
    note("Could not create the file: " + (ex.message || ex), true);
  }
}

// Called from the Setup tab: open a project file (or start a new one) here.
export async function openInIde({ rel = "", create = "" } = {}) {
  if (rel && state.designDir) {
    state.explicitOpen = true;
    await loadTree();
    await openFile(state.designDir.replace(/\/+$/, "") + "/" + rel, rel);
  } else if (create) {
    state.explicitOpen = true;
    await newFile(create);
  }
}

// ------------------------------------------------------------------ explorer

// The explorer's groups, in the order a design is built: what goes into the
// chip, what only simulates it, what constrains it, and the project itself.
const GROUPS = [
  { name: "Design", roles: ["design", "header", "memory", "vhdl"], add: "module_v", addLabel: "New design module" },
  { name: "Simulation", roles: ["testbench"], add: "tb_v", addLabel: "New testbench",
    empty: "No testbench yet. <button class='linklike' data-new='tb_v'>Write one</button> to simulate the design before the long run." },
  { name: "Constraints", roles: ["timing", "pins"], add: "sdc", addLabel: "New constraints file",
    empty: "Timing comes from the clock period in the config. <button class='linklike' data-new='sdc'>Create an SDC file</button> for I/O delays, false paths or more clocks." },
  { name: "Project", roles: ["config", "other"], add: "other", addLabel: "New text file" },
];
const PLUS = "<svg viewBox='0 0 24 24' width='13' height='13' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M12 5v14M5 12h14'/></svg>";
const XMARK = "<svg viewBox='0 0 24 24' width='12' height='12' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><path d='M6 6l12 12M18 6L6 18'/></svg>";

async function loadTree() {
  const tree = document.getElementById("ide-tree");
  if (!tree) return;
  if (!state.designDir) {
    state.files = [];
    tree.innerHTML = "<p class='muted'>Open a project first: its files appear here.</p>";
    renderStart();
    return;
  }
  let res;
  try { res = await api.projectFiles(state.designDir); } catch (_e) { return; }
  const dir = state.designDir;
  state.files = (res.files || []).map((f) => ({ relpath: f.rel, abspath: f.abs, role: f.role, built: !!f.in_verilog_files }));
  // The files show at once; the top module's tag follows. The module list
  // comes from the project index (one request, milliseconds) rather than the
  // Setup page's full design scan, which reads every file's ports (seconds
  // on a 300-file project).
  renderTree();
  try {
    const [info, hier] = await Promise.all([
      api.designInfo({ keys_only: true, keys: ["DESIGN_NAME", "VERILOG_DEFINES"] }),
      api.ideHierarchy({}).catch(() => ({ modules: {} })),
    ]);
    if (state.designDir !== dir) return;
    const s = (info && info.settings) || {};
    const mods = (hier && hier.modules) || {};
    state.moduleFiles = Object.fromEntries(Object.entries(mods).filter(([, m]) => m.role !== "testbench")
      .map(([n, m]) => [n, (state.files.find((f) => f.relpath === m.rel) || {}).abspath || null]).filter(([, a]) => a));
    state.configTop = typeof s.DESIGN_NAME === "string" && s.DESIGN_NAME ? s.DESIGN_NAME : null;
    state.top = state.configTop || suggestTop(mods);
    state.topAbs = (state.top && state.moduleFiles[state.top]) || null;
    const vd = s.VERILOG_DEFINES;
    state.configDefines = Array.isArray(vd) ? vd.map(String) : typeof vd === "string" ? vd.split(/\s+/).filter(Boolean) : [];
  } catch (_e) { state.top = null; state.configTop = null; state.topAbs = null; state.moduleFiles = {}; state.configDefines = []; }
  renderTree();
}

// With no DESIGN_NAME: the built design module nothing else instantiates.
function suggestTop(mods) {
  const used = new Set(Object.values(mods).flatMap((m) => (m.children || []).map((c) => c.type)));
  const cands = Object.values(mods).filter((m) => m.role !== "testbench" && !used.has(m.name));
  const built = cands.filter((m) => m.built);
  return ((built.length ? built : cands)[0] || {}).name || null;
}

function fileRow(f) {
  const cut = f.relpath.lastIndexOf("/");
  const dir = cut >= 0 ? f.relpath.slice(0, cut + 1) : "";
  const base = f.relpath.slice(cut + 1);
  const out = f.role === "design" && !f.built && state.files.some((x) => x.built);
  const tags = (f.abspath === state.topAbs ? "<span class='tag is-top'>top</span>" : "") +
    (out ? "<span class='tag' title='Not in the config&#39;s file list, so it isn&#39;t built into the chip. Right-click it and choose Build into the chip, or tick it under Sources on the Setup tab.'>not built</span>" : "");
  return "<div class='ide-file-row" + (f.abspath === state.active ? " is-active" : "") + (out ? " is-out" : "") +
    "' data-abs='" + fmt.escape(f.abspath) + "'>" +
    "<button class='ide-file' data-abs='" + fmt.escape(f.abspath) + "' data-rel='" + fmt.escape(f.relpath) + "' title='" + fmt.escape(f.relpath) + "'>" +
    "<span class='fn'><span class='dir'>" + fmt.escape(dir) + "</span>" + fmt.escape(base) + "</span>" + tags + "</button>" +
    "<button class='ide-file-more' title='More for " + fmt.escape(f.relpath) + "' aria-label='More for " + fmt.escape(f.relpath) +
    "' aria-haspopup='menu' data-rel='" + fmt.escape(f.relpath) + "'><svg viewBox='0 0 24 24' width='14' height='14' fill='currentColor' aria-hidden='true'><circle cx='6' cy='12' r='1.6'/><circle cx='12' cy='12' r='1.6'/><circle cx='18' cy='12' r='1.6'/></svg></button></div>";
}

function renderTree() {
  const tree = document.getElementById("ide-tree");
  if (!tree) return;
  if (state.explorerView === "hier") { renderHierarchy(); return; }
  tree.innerHTML = GROUPS.map((g) => {
    const files = state.files.filter((f) => g.roles.includes(f.role));
    if (!files.length && !g.empty && g.name !== "Design") return "";
    const vhdlNote = g.name === "Design" && files.some((f) => f.role === "vhdl") ? vhdlTag() : "";
    const body = (g.name === "Design" ? topWarning() : "") + (files.length ? files.map(fileRow).join("")
      : "<p class='ide-group-empty'>" + (g.empty || "No design files yet. <button class='linklike' data-new='module_v'>Write a module</button>.") + "</p>");
    return "<div class='ide-group'><span class='ide-group-name'>" + g.name + "</span>" + vhdlNote +
      "<button class='ide-group-add' data-new='" + g.add + "' title='" + g.addLabel + "' aria-label='" + g.addLabel + "'>" + PLUS + "</button></div>" + body;
  }).join("");
}

// VHDL files: what the IDE can do with them here, on the Design group.
function vhdlTag() {
  const c = state.vhdlCaps;
  if (c && c.state === "ready") {
    return "<span class='ide-xtag' title='" + fmt.escape("VHDL: " + c.note + " It works with LibreLane 3.0.4's image; another image may not have it.") + "'>VHDL experimental</span>";
  }
  if (c && (c.state === "missing" || c.state === "no-engine" || c.state === "error")) {
    return "<span class='ide-xtag is-off' title='" + fmt.escape(c.note || "") + "'>VHDL edit only</span>";
  }
  return "";
}

// The config names a top module that no design file defines (the starter
// top.v was replaced by a pasted design, say): the run would stop at synthesis
// with "module not found". Say so where the files are, with the one-click fix.
function topWarning() {
  const want = state.configTop;
  const mods = state.moduleFiles || {};
  if (!want || mods[want]) return "";
  const designAbs = new Set(state.files.filter((f) => f.role === "design").map((f) => f.abspath));
  const cands = Object.keys(mods).filter((m) => designAbs.has(mods[m])).slice(0, 3);
  if (!cands.length) return "";
  return "<div class='ide-top-warn' role='alert'><b>No module named <code>" + fmt.escape(want) + "</code>.</b> " +
    "The config builds <code>" + fmt.escape(want) + "</code> as the top, but no design file defines it, so a run would stop at synthesis. " +
    cands.map((m) => "<button class='btn btn-sm' data-maketop='" + fmt.escape(m) + "'>Make " + fmt.escape(m) + " the top</button>").join("") + "</div>";
}

// ------------------------------------------------------------------ hierarchy

const IC_CHIP = "<svg viewBox='0 0 24 24' width='13' height='13' fill='none' stroke='currentColor' stroke-width='1.6' aria-hidden='true'><rect x='6' y='6' width='12' height='12' rx='1.5'/><path d='M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3'/></svg>";

// The module tree, like Vivado's or Quartus's Hierarchy: the chip's top with
// everything it instantiates, and each testbench with the design under it.
async function renderHierarchy() {
  const tree = document.getElementById("ide-tree");
  if (!tree) return;
  // From the project index in one request (it used to read every file over
  // HTTP each time, which a 300-file project felt).
  let mods = {};
  try { mods = (await api.ideHierarchy({ dirty: dirtyTexts() })).modules || {}; } catch (_e) { mods = {}; }
  if (state.explorerView !== "hier") return;
  const absOf = (rel) => { const f = state.files.find((x) => x.relpath === rel); return f ? f.abspath : (state.designDir || "") + "/" + rel; };
  const defs = new Map(Object.entries(mods).map(([n, m]) => [n, { rel: m.rel, abs: absOf(m.rel), line: m.line }]));
  const kids = new Map(Object.entries(mods).map(([n, m]) => [n, (m.children || []).filter((c) => defs.has(c.type))]));
  const used = new Set([...kids.values()].flat().map((i) => i.type));
  const isTb = (n) => isTestbenchFile(defs.get(n).rel);
  const chip = state.top && defs.has(state.top) ? [state.top]
    : [...defs.keys()].filter((n) => !used.has(n) && !isTb(n));
  const benches = [...defs.keys()].filter((n) => isTb(n) && !used.has(n));
  const row = (type, inst, depth, seen) => {
    const d = defs.get(type);
    const sub = depth < 12 && !seen.has(type) ? kids.get(type) || [] : [];
    const next = new Set(seen).add(type);
    const line = d.line;
    return "<div class='ih-node'><button class='ih-row' style='padding-left:" + (8 + depth * 14) + "px' data-abs='" + fmt.escape(d.abs) +
      "' data-rel='" + fmt.escape(d.rel) + "' data-line='" + line + "' title='" + fmt.escape(type + " in " + d.rel + ", line " + line) + "'>" +
      (depth === 0 ? "<span class='ih-ic'>" + IC_CHIP + "</span>" : "") +
      (inst ? "<span class='ih-inst'>" + fmt.escape(inst) + "</span><span class='ih-type'>" + fmt.escape(type) + "</span>"
        : "<span class='ih-root'>" + fmt.escape(type) + "</span>") +
      (type === state.top && depth === 0 ? "<span class='tag is-top'>top</span>" : "") + "</button>" +
      sub.map((k) => row(k.type, k.inst, depth + 1, next)).join("") + "</div>";
  };
  const group = (name, roots, empty) =>
    "<div class='ide-group'><span class='ide-group-name'>" + name + "</span></div>" +
    (roots.length ? roots.map((r) => row(r, null, 0, new Set())).join("") : "<p class='ide-group-empty'>" + empty + "</p>");
  tree.innerHTML = !defs.size
    ? "<p class='ide-group-empty'>No modules yet. Write one, and its instances show here as a tree.</p>"
    : group("Chip", chip, "No top module found in the design files.") +
      group("Simulation", benches, "No testbench yet.");
}

function setExplorerView(v) {
  state.explorerView = v === "hier" ? "hier" : "files";
  try { localStorage.setItem("ll.ideView", state.explorerView); } catch (_e) {}
  document.querySelectorAll(".ide-view-seg button").forEach((b) => {
    const on = b.dataset.view === state.explorerView;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  renderTree();
}

function wireTree() {
  const tree = document.getElementById("ide-tree");
  if (!tree) return;
  try { state.explorerView = localStorage.getItem("ll.ideView") === "hier" ? "hier" : "files"; } catch (_e) {}
  document.querySelectorAll(".ide-view-seg button").forEach((b) => {
    b.addEventListener("click", () => setExplorerView(b.dataset.view));
    const on = b.dataset.view === (state.explorerView || "files");
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  tree.addEventListener("click", async (e) => {
    const ih = e.target.closest(".ih-row");
    if (!ih) return;
    await openFile(ih.dataset.abs, ih.dataset.rel);
    window.ideEditor.gotoLine(+ih.dataset.line);
  });
  tree.addEventListener("click", (e) => {
    const n = e.target.closest("[data-new]");
    if (n) { newFile(n.dataset.new); return; }
    const mt = e.target.closest("[data-maketop]");
    if (mt) { makeTop(mt.dataset.maketop); return; }
    const more = e.target.closest(".ide-file-more");
    if (more) { e.stopPropagation(); fileMenu(more.dataset.rel, more); return; }
    const f = e.target.closest(".ide-file");
    if (f) openFile(f.dataset.abs, f.dataset.rel);
  });
  // Keyboard, as in any file explorer: F2 renames, Delete deletes (Undo in the toast).
  tree.addEventListener("keydown", (e) => {
    const f = e.target.closest(".ide-file");
    if (!f || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === "F2") { e.preventDefault(); renameFile(f.dataset.rel); }
    else if (e.key === "Delete") { e.preventDefault(); deleteFile(f.dataset.rel); }
  });
  tree.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".ide-file-row");
    if (!row) return;
    e.preventDefault();
    const b = row.querySelector(".ide-file-more");
    if (b) fileMenu(b.dataset.rel, b, e);
  });
}

// The file's menu: make one of its modules the top, put it in or out of the
// chip build, simulate a testbench, copy the path, delete.
function fileMenu(rel, anchor, ev) {
  const f = state.files.find((x) => x.relpath === rel);
  if (!f) return;
  let menu = document.getElementById("ide-file-menu");
  if (!menu) {
    menu = document.createElement("div");
    menu.id = "ide-file-menu";
    menu.className = "ide-sim-menu ide-file-menu";
    menu.setAttribute("role", "menu");
    document.querySelector(".ide-side")?.appendChild(menu);
    // Esc, arrows, an outside press and focus return: the overlay stack's (overlay.js).
  }
  const mods = Object.entries(state.moduleFiles || {}).filter(([, abs]) => abs === f.abspath).map(([m]) => m);
  const hdl = /\.s?v$/i.test(rel);
  const items = [];
  if (f.role === "design" && hdl) {
    for (const m of mods) if (m !== state.top) items.push(["top:" + m, "Make " + m + " the top module"]);
    if (state.files.some((x) => x.built) || f.built) {
      // The top module's file, or the last file built, can't leave: the
      // flow would have nothing (or no top) to build.
      const last = f.built && !state.files.some((x) => x !== f && x.built && x.role === "design" && /\.s?v$/i.test(x.relpath));
      const why = !f.built ? "" : f.abspath === state.topAbs ? "it holds the top module" : last ? "it's the only file built" : "";
      items.push(!f.built ? ["in", "Build into the chip"] : why ? ["-", "Leave out of the chip (" + why + ")"] : ["out", "Leave out of the chip"]);
    }
  }
  if (f.role === "testbench") items.push(["sim", "Simulate this testbench"]);
  items.push(["rename", "Rename…", "F2"]);
  items.push(["copy", "Copy path"]);
  items.push(["delete", "Delete", "Del"]);
  menu.innerHTML = "<div class='ism-h'>" + fmt.escape(rel) + "</div>" +
    items.map(([k, label, key]) => (k === "rename" ? "<div class='ism-sep'></div>" : "") +
      "<button role='menuitem' data-fm='" + fmt.escape(k) + "'" + (k === "delete" ? " class='is-danger'" : "") + (k === "-" ? " disabled" : "") + ">" + fmt.escape(label) +
      (key ? "<span class='ism-key'>" + key + "</span>" : "") + "</button>").join("");
  menu.hidden = false;
  const side = menu.parentElement.getBoundingClientRect();
  const a = anchor.getBoundingClientRect();
  const z = side.width && menu.parentElement.clientWidth ? side.width / menu.parentElement.clientWidth : 1;
  const y = ((ev ? ev.clientY : a.bottom) - side.top) / z;
  menu.style.left = "8px";
  menu.style.right = "8px";
  menu.style.top = Math.min(y + 2, menu.parentElement.clientHeight - menu.offsetHeight - 8) + "px";
  if (isLayerOpen(menu)) closeLayer(menu, "replaced");
  menu.hidden = false;
  openLayer(menu, { kind: "menu", owner: anchor, onClose: () => { menu.hidden = true; } });
  menu.onclick = async (e) => {
    const b = e.target.closest("[data-fm]");
    if (!b || b.disabled) return;
    closeLayer(menu);
    const k = b.dataset.fm;
    if (k.startsWith("top:")) await makeTop(k.slice(4));
    else if (k === "in" || k === "out") await setBuilt(f, k === "in");
    else if (k === "sim") {
      byId("ide-tb", (sel) => { sel.value = rel; byId("ide-top", (x) => { x.value = ""; }); deriveTopFromTb(); });
      paintSimPick();
      simulate();
    } else if (k === "copy") {
      try { await navigator.clipboard.writeText(shownPath(f.abspath)); note("Copied " + shownPath(f.abspath)); }
      catch (_e) { note(shownPath(f.abspath)); }
    } else if (k === "rename") renameFile(rel);
    else if (k === "delete") deleteFile(rel);
  };
}

// The config's DESIGN_NAME, as the Setup page's Design row sets it.
async function makeTop(mod) {
  try {
    const r = await api.configSet({ DESIGN_NAME: mod }, []);
    note("The top module is now " + mod + (r && r.name ? " (saved in " + r.name + ")." : "."));
    await loadTree();
    document.dispatchEvent(new CustomEvent("g:config_changed"));
    document.dispatchEvent(new CustomEvent("g:sources_changed"));
  } catch (ex) {
    note("Couldn't set the top module: " + (ex.message || ex), true);
  }
}

// In or out of the config's file list (VERILOG_FILES), the same write the
// Setup page's Sources ticks make.
async function setBuilt(f, on) {
  const now = state.files.filter((x) => x.role === "design" && x.built && /\.s?v$/i.test(x.relpath)).map((x) => x.abspath);
  const files = on ? now.concat([f.abspath]) : now.filter((x) => x !== f.abspath);
  try {
    const r = await api.configSources(files);
    note((on ? "Added " : "Left out ") + f.relpath + (r && r.name ? (on ? " to " : " of ") + r.name + "." : "."));
    await loadTree();
    document.dispatchEvent(new CustomEvent("g:config_changed"));
    document.dispatchEvent(new CustomEvent("g:sources_changed"));
  } catch (ex) {
    note("Couldn't change the file list: " + (ex.message || ex), true);
  }
}

// Remove a file from the project. No "are you sure": the text is kept in
// memory and the toast offers Undo, which writes it back (like a mail
// client's delete). A file that can't be read first still asks.
async function deleteFile(rel) {
  if (!rel) return;
  const f = state.files.find((x) => x.relpath === rel);
  stash();
  const t = state.tabs.find((x) => x.rel === rel);
  let text = t ? t.text : null;
  if (text === null && f) {
    try { const r = await api.readText(f.abspath); if (typeof r.text === "string") text = r.text; } catch (_e) { /* asks below */ }
  }
  if (text === null) {
    const { confirmDialog } = await import("../dialog.js");
    const ok = await confirmDialog({
      title: "Delete file", danger: true, confirmText: "Delete",
      body: "Delete " + rel + " from the project folder? This cannot be undone.",
    });
    if (!ok) return;
  }
  try {
    const r = await api.fileDelete(rel);
    if (!r.ok) throw new Error(r.error || "refused");
    if (t) { t.saved = t.text; closeTab(t.abs, { force: true }); }
    await afterFilesChanged();
    note("Deleted " + rel);
    if (text !== null) {
      const { toast } = await import("../toast.js");
      toast.show("Deleted " + rel + ".", "info", 12000, { label: "Undo", run: () => undoDelete(rel, text) });
    }
  } catch (ex) {
    note("Delete failed: " + (ex.message || ex), true);
  }
}

async function undoDelete(rel, text) {
  try {
    const r = await api.fileWrite(rel, text, { on_exists: "error" });
    if (r && r.ok === false) throw new Error(r.error || "refused");
    await afterFilesChanged();
    const f = state.files.find((x) => x.relpath === rel);
    if (f) await openFile(f.abspath, rel);
    note("Restored " + rel);
  } catch (ex) {
    note(conflictKind(ex) === "exists" ? "Couldn't restore " + rel + ": a file of that name was made since. Nothing was overwritten."
      : "Couldn't restore " + rel + ": " + (ex.message || ex), true);
  }
}

async function afterFilesChanged() {
  await loadTree();
  await loadTestbenches();
  document.dispatchEvent(new CustomEvent("g:sources_changed"));
}

// Rename in place (same folder). The server moves the file and points the
// config at the new name: the file list, an SDC or pin-order setting.
async function renameFile(rel) {
  const f = state.files.find((x) => x.relpath === rel);
  if (!f) return;
  const cut = rel.lastIndexOf("/");
  const dir = cut >= 0 ? rel.slice(0, cut + 1) : "";
  const base = rel.slice(cut + 1);
  const dot = base.lastIndexOf(".");
  const ext = dot > 0 ? base.slice(dot) : "";
  const { customDialog } = await import("../dialog.js");
  let name = null;
  const res = await customDialog({
    title: "Rename " + base,
    bodyHtml: "<div class='newfile'><label class='nf-field'><span>New name</span>" +
      "<input id='rn-name' type='text' spellcheck='false' autocomplete='off' value='" + fmt.escape(base) + "'/></label>" +
      "<p class='nf-path'>In <code>" + fmt.escape(dir || "the project folder") + "</code>. " +
      "Only the file is renamed; a module inside keeps its name.</p></div>",
    buttons: [
      { label: "Cancel", value: null, cls: "btn-ghost" },
      { label: "Rename", value: "ok", cls: "btn-primary" },
    ],
    onMount: (back) => {
      const input = back.querySelector("#rn-name");
      const ok = back.querySelector(".btn-primary");
      ok?.addEventListener("click", () => { name = (input.value || "").trim(); }, true);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ok?.click(); } });
      setTimeout(() => { input.focus(); input.setSelectionRange(0, dot > 0 ? dot : base.length); }, 0);
    },
  });
  if (res !== "ok" || !name || name === base) return;
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.\-]*$/.test(name)) { note("Use letters, digits, - _ and . in a file name.", true); return; }
  if (ext && !name.includes(".")) name += ext;
  // The renamed tab must not lose unsaved edits: save it first.
  stash();
  const t = state.tabs.find((x) => x.rel === rel);
  try {
    if (t && t.text !== t.saved) {
      const how = await saveTab(t, t.text);
      if (how === "cancel") { note("Not renamed: " + rel + " changed on disk and wasn't saved.", true); return; }
    }
    const r = await api.fileRename(rel, dir + name);
    if (!r.ok) throw new Error(r.error || "refused");
    const wasActive = t && t.abs === state.active;
    if (t) closeTab(t.abs, { force: true });
    await afterFilesChanged();
    if (r.config && r.config.changed && r.config.changed.length) document.dispatchEvent(new CustomEvent("g:config_changed"));
    const nf = state.files.find((x) => x.relpath === r.rel);
    if (nf && (wasActive || !state.active)) await openFile(nf.abspath, r.rel);
    const cfg = r.config && r.config.changed && r.config.changed.length
      ? " and updated " + r.config.name + " (" + r.config.changed.join(", ") + ")" : "";
    note(r.config_error ? "Renamed to " + r.rel + ", but the config still names the old file: " + r.config_error
      : "Renamed to " + r.rel + cfg + ".", !!r.config_error);
  } catch (ex) {
    note("Rename failed: " + (ex.message || ex), true);
  }
}

// ------------------------------------------------------------------ tabs

function activeTab() { return state.tabs.find((t) => t.abs === state.active) || null; }

// Keep the editor's text in its tab before showing another file.
function stash() {
  const t = activeTab();
  const ed = window.ideEditor;
  if (t && ed && ed.relPath === t.rel) t.text = ed.getValue();
}

// Tabs are keyed by the project-relative path: the server's resolved path
// and one built from the design folder can differ, the relative one can't.
// Two opens of the same file in flight share one read.
const _opening = new Map();
async function openFile(abs, rel) {
  let t = state.tabs.find((x) => x.rel === rel);
  if (!t) {
    if (!_opening.has(rel)) {
      _opening.set(rel, api.readText(abs).finally(() => _opening.delete(rel)));
    }
    try {
      const r = await _opening.get(rel);
      if (r.text === undefined) throw new Error(r.error || "unreadable");
      t = state.tabs.find((x) => x.rel === rel);
      if (!t) {
        const f = state.files.find((x) => x.relpath === rel);
        t = { abs: f ? f.abspath : abs, rel, text: r.text, saved: r.text, hash: r.hash || null };
        state.tabs.push(t);
        await restoreDraft(t);
      }
    } catch (ex) {
      note("Could not open: " + (ex.message || ex), true);
      return;
    }
  }
  activate(t);
}

let _idxT = 0;
function refreshIndexStatusSoon() { clearTimeout(_idxT); _idxT = setTimeout(() => refreshIndexStatus(), 400); }

function activate(t) {
  noteRecent(state.designDir, t.rel);
  stash();
  if (document.querySelector(".ide-center.panel-max")) setPanelMax(false);
  state.active = t.abs;
  const ed = window.ideEditor;
  ed.load(t.rel, t.text);
  applyMarkers();
  byId("ide-open", (el) => { el.textContent = t.rel; el.title = shownPath(t.abs); });
  renderTabs();
  renderStart();
  document.querySelectorAll("#ide-tree .ide-file-row").forEach((r) => r.classList.toggle("is-active", r.dataset.abs === t.abs));
  // Opening a testbench makes it the one Simulate runs.
  const f = state.files.find((x) => x.abspath === t.abs);
  if (f && f.role === "testbench") {
    byId("ide-tb", (sel) => {
      if ([...sel.options].some((o) => o.value === f.relpath)) { sel.value = f.relpath; byId("ide-top", (x) => { x.value = ""; }); deriveTopFromTb(); paintSimPick(); }
    });
  }
}

async function closeTab(abs, { force = false } = {}) {
  stash();
  const i = state.tabs.findIndex((x) => x.abs === abs);
  if (i < 0) return;
  const t = state.tabs[i];
  if (!force && t.text !== t.saved) {
    const { confirmDialog } = await import("../dialog.js");
    const ok = await confirmDialog({ title: "Close without saving?", danger: true, confirmText: "Close",
      body: t.rel + " has changes that aren't saved. Close it and lose them?" });
    if (!ok) return;
  }
  state.tabs.splice(i, 1);
  dropDraft(t.abs);
  window.ideEditor.forget(t.rel);
  if (state.active === abs) {
    state.active = null;
    const next = state.tabs[i] || state.tabs[i - 1];
    if (next) activate(next);
    else {
      // Closed on purpose: coming back to the tab doesn't reopen the top file.
      state.explicitOpen = true;
      window.ideEditor.load(null, "");
      byId("ide-open", (el) => { el.textContent = "No file open"; el.title = ""; });
      document.querySelectorAll("#ide-tree .ide-file-row").forEach((r) => r.classList.remove("is-active"));
    }
  }
  renderTabs();
  renderStart();
}

function renderTabs() {
  const host = document.getElementById("ide-tabs");
  if (!host) return;
  host.innerHTML = state.tabs.map((t) => {
    const name = t.rel.split("/").pop();
    return "<div class='ide-tab" + (t.abs === state.active ? " is-on" : "") + (t.text !== t.saved ? " is-dirty" : "") +
      "' role='tab' aria-selected='" + (t.abs === state.active) + "' tabindex='0' data-abs='" + fmt.escape(t.abs) + "' title='" + fmt.escape(t.rel) + "'>" +
      "<span class='fn'>" + fmt.escape(name) + "</span>" +
      "<span class='x' role='button' aria-label='Close " + fmt.escape(name) + "' data-close='" + fmt.escape(t.abs) + "'>" + XMARK + "</span></div>";
  }).join("");
}

function wireTabs() {
  const host = document.getElementById("ide-tabs");
  if (!host) return;
  host.addEventListener("click", (e) => {
    const x = e.target.closest("[data-close]");
    if (x) { closeTab(x.dataset.close); return; }
    const tab = e.target.closest(".ide-tab");
    const t = tab && state.tabs.find((y) => y.abs === tab.dataset.abs);
    if (t && t.abs !== state.active) activate(t);
  });
  host.addEventListener("auxclick", (e) => {     // middle-click closes, as in every editor
    const tab = e.target.closest(".ide-tab");
    if (tab && e.button === 1) closeTab(tab.dataset.abs);
  });
  host.addEventListener("keydown", (e) => {
    const tab = e.target.closest(".ide-tab");
    if (tab && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); tab.click(); }
  });
}

// Typing marks the tab unsaved (Ctrl+S saves, from the editor's keymap).
function onEditorChange(text) {
  const t = activeTab();
  if (!t || window.ideEditor.relPath !== t.rel) return;
  t.text = text;
  const el = document.querySelector("#ide-tabs .ide-tab.is-on");
  if (el) el.classList.toggle("is-dirty", t.text !== t.saved);
  keepDraftSoon(t);
}

// ------------------------------------------------------------------ drafts
// Unsaved edits are also kept in the browser's storage as you type, so they
// survive what the page itself cannot: LanEx's server going away (a laptop
// that slept, a crash) followed by a reload or a fresh window. Opening the
// file again offers them back. Cleared on save or on a deliberate discard.

const DRAFT_PREFIX = "ll.draft:";
const DRAFT_MAX = 2 * 1024 * 1024;
const _draftTimers = new Map();

function textHash(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return s.length + ":" + (h >>> 0).toString(16);
}

function keepDraftSoon(t) {
  clearTimeout(_draftTimers.get(t.abs));
  _draftTimers.set(t.abs, setTimeout(() => { _draftTimers.delete(t.abs); keepDraft(t); }, 700));
}

function keepDraft(t) {
  try {
    if (t.text === t.saved) { localStorage.removeItem(DRAFT_PREFIX + t.abs); return; }
    if (t.text.length > DRAFT_MAX) return;
    localStorage.setItem(DRAFT_PREFIX + t.abs, JSON.stringify({ text: t.text, base: textHash(t.saved), at: Date.now() }));
  } catch (_e) { /* storage full or blocked: the beforeunload prompt still guards */ }
}

function dropDraft(abs) {
  clearTimeout(_draftTimers.get(abs));
  _draftTimers.delete(abs);
  try { localStorage.removeItem(DRAFT_PREFIX + abs); } catch (_e) { /* nothing kept */ }
}

// A freshly opened tab: bring back edits that never reached the disk.
async function restoreDraft(t) {
  let d = null;
  try { d = JSON.parse(localStorage.getItem(DRAFT_PREFIX + t.abs) || "null"); } catch (_e) { d = null; }
  if (!d || typeof d.text !== "string" || d.text === t.saved) { if (d) dropDraft(t.abs); return; }
  const when = MFwhen(d.at || Date.now());
  if (d.base === textHash(t.saved)) {
    // The file is exactly as it was when the edits were made: nothing to lose.
    t.text = d.text;
    note("Brought back unsaved edits to " + t.rel + " from " + when + ". Save to keep them.");
    return;
  }
  const { confirmDialog } = await import("../dialog.js");
  const ok = await confirmDialog({ title: "Bring back unsaved edits?", confirmText: "Bring them back",
    body: t.rel + " has edits from " + when + " that were never saved, but the file has changed since. " +
      "Bring the edits back (they replace what is on disk once you save), or keep the file as it is now?" });
  if (ok) t.text = d.text;
  else dropDraft(t.abs);
}

function showCursor({ line, col, selected, cursors }) {
  byId("ide-cursor", (c) => {
    c.textContent = "Ln " + line + ", Col " + col +
      (cursors > 1 ? " · " + cursors + " cursors" : selected ? " · " + selected + " selected" : "");
  });
}

// Open the top module (or the first design file) when nothing is open yet.
function autoOpen() {
  if (state.tabs.length || state.explicitOpen || !state.designDir) { renderStart(); return; }
  const first = state.topAbs ? state.files.find((f) => f.abspath === state.topAbs)
    : state.files.find((f) => f.role === "design");
  if (first) openFile(first.abspath, first.relpath);
  else renderStart();
}

// With no file open, the editor area shows the loop this IDE is for.
function renderStart() {
  const el = document.getElementById("ide-start");
  if (!el) return;
  // Check syntax and Simulate act on a project: off while none is open.
  const none = !state.designDir;
  byId("ide-lint", (b) => { if (!state.linting) b.disabled = none; });
  document.querySelector("#ide-diags [data-pb='lint']")?.toggleAttribute("disabled", none && !state.linting);
  for (const id of ["ide-sim", "ide-sim-pick"]) byId(id, (b) => { if (!state.simRunning) b.disabled = none; });
  el.hidden = state.tabs.length > 0;
  byId("ide-cursor", (c) => { c.hidden = !state.tabs.length; });
  if (!el.hidden) {
    const top = state.topAbs && state.files.find((f) => f.abspath === state.topAbs);
    const hasTb = state.files.some((f) => f.role === "testbench");
    el.innerHTML = !state.designDir
      ? emptyHtml({ icon: "code", title: EMPTY_WORDS.noProject.title, body: "The IDE edits, checks and simulates the Verilog of the project you have open. Open or create one on Setup and its files appear here.", action: EMPTY_WORDS.noProject.action })
      : "<h3>Nothing open</h3><p>Pick a file on the left, or start here. The usual loop: write the design, check its syntax, " +
        "simulate it with a testbench, and only then run the RTL-to-GDS flow, which takes minutes rather than seconds.</p>" +
        "<div class='ide-start-acts'>" +
        (top ? "<button class='btn btn-primary btn-sm' data-start='open-top'>Open " + fmt.escape(top.relpath) + "</button>" : "") +
        "<button class='btn btn-sm" + (top ? "" : " btn-primary") + "' data-new='module_v'>New design module</button>" +
        "<button class='btn btn-sm' data-new='tb_v'>" + (hasTb ? "New testbench" : "Write a testbench") + "</button></div>" +
        "<ol class='ide-path'>" +
        "<li><b><span>1</span>Write</b><small>Design modules go in <code>src/</code> and into the chip.</small></li>" +
        "<li><b><span>2</span>Check syntax</b><small>Verilator reads the files the config builds and the testbenches; errors land in Problems with their line.</small></li>" +
        "<li><b><span>3</span>Simulate</b><small>A testbench in <code>verify/</code> drives the design; the waveform shows below.</small></li>" +
        "<li><b><span>4</span>Run the flow</b><small>Setup checks the project, PDK and engine, then Run builds the GDS.</small></li></ol>";
  }
  if (!el._wired) {
    el._wired = true;
    el.addEventListener("click", (e) => {
      const n = e.target.closest("[data-new]");
      if (n) { newFile(n.dataset.new); return; }
      if (e.target.closest("[data-start='open-top']")) {
        const f = state.files.find((x) => x.abspath === state.topAbs);
        if (f) openFile(f.abspath, f.relpath);
      }
    });
  }
}

// ------------------------------------------------------------------ save

async function saveFile({ quiet = false } = {}) {
  const ed = window.ideEditor;
  const t = activeTab();
  if (!t || !ed.relPath) { note("Open a file first.", true); return; }
  const text = ed.getValue();
  try {
    const how = await saveTab(t, text);
    renderTabs();
    if (how === "cancel") {
      note(t.rel + " wasn't saved: it changed on disk. Save again to choose which version to keep.", true);
      return;
    }
    if (how === "reload") {
      note("Reloaded " + t.rel + " from disk. Your edits are not in the file; Ctrl+Z in the editor brings them back.");
      return;
    }
    t.text = text;
    dropSimDiags([t.rel]);
    note((how === "mine" ? "Saved your version of " : "Saved ") + t.rel);
    if (state.explorerView === "hier") renderHierarchy();
    if (/\.(s?v|vh|svh|vhdl?)$/i.test(t.rel)) { markStale(); refreshIndexStatusSoon(); }
    // The Setup page reads the config and the file list; let it catch up.
    document.dispatchEvent(new CustomEvent("g:sources_changed"));
    if (!quiet && lintOnSave() && /\.s?v$/i.test(t.rel)) lint({ quiet: true });
  } catch (ex) {
    note("Save failed: " + (ex.message || ex), true);
  }
}

// ------------------------------------------------------------------ disk sync
// Each tab keeps the hash of the text it read (or last saved). A save sends
// it; the server refuses with 409 when the file changed on disk since (Setup
// wrote the config, a terminal, the other window), and the person chooses:
// reload the disk's version, keep theirs, or compare first. Tabs without
// edits follow the disk by themselves (on focus and on g:config_changed).

// Write a tab's text based on the version it read. Throws the server's
// error (a 409 carries {conflict} or {exists}).
async function writeTab(t, text, { force = false } = {}) {
  const r = await api.fileWrite(t.rel, text, force ? { force: true } : t.hash ? { base_hash: t.hash } : {});
  if (r && r.ok === false) throw new Error(r.error || "write refused");
  t.saved = text;
  t.hash = (r && r.hash) || t.hash;
  t.diskHash = null;
  dropDraft(t.abs);
  // The config is what Setup shows: it reads it again.
  if (isConfigFile(t.rel)) document.dispatchEvent(new CustomEvent("g:config_changed", { detail: { from: "ide" } }));
  return r;
}

// Save, and when the file changed on disk, ask. -> "saved" | "mine" (kept
// over the disk's) | "reload" (the disk's version is in the tab now) | "cancel".
async function saveTab(t, text) {
  try {
    await writeTab(t, text);
    return "saved";
  } catch (ex) {
    if (conflictKind(ex) !== "changed") throw ex;
    return resolveConflict(t, text);
  }
}

async function resolveConflict(t, mine) {
  let disk = null;
  try { disk = await api.readText(t.abs); } catch (_e) { disk = null; }
  const diskText = disk && typeof disk.text === "string" ? disk.text : null;
  const name = t.rel.split("/").pop();
  const { customDialog } = await import("../dialog.js");
  let compare = false;
  for (;;) {
    let body = "<p>" + fmt.escape(t.rel) + " was changed outside this editor since you opened it" +
      (isConfigFile(t.rel) ? " (Setup writes this file when you change a setting there)" : " (Setup, a terminal or another window)") +
      ". Your edits are still here. Keeping yours overwrites that change.</p>";
    if (compare && diskText !== null) {
      const d = lineDiff(diskText, mine);
      body += !d ? "<p class='muted'>The texts are the same.</p>"
        : "<div class='fx-diff ide-cmp' aria-label='How your version differs from the file on disk'>" +
          "<div class='fx-diff-h'>− on disk · + yours</div>" +
          d.slice(0, 400).map((x) => "<div class='fx-dl is-" + (x.op === "+" ? "add" : "del") + "'><span class='fx-ln'>" + x.line +
            "</span><span class='fx-op'>" + x.op + "</span><code>" + fmt.escape(x.text) + "</code></div>").join("") +
          (d.length > 400 ? "<div class='fx-diff-h'>" + (d.length - 400) + " more lines differ</div>" : "") + "</div>";
    }
    const choice = await customDialog({
      title: name + " changed on disk",
      bodyHtml: body,
      wide: compare,
      buttons: [
        { label: "Cancel", value: null, cls: "btn-ghost" },
        ...(compare || diskText === null ? [] : [{ label: "Compare", value: "compare", cls: "btn-ghost" }]),
        { label: "Reload from disk", value: "reload", cls: "btn-ghost" },
        // Overwrites the other change: a danger button, so Cancel has the focus.
        { label: "Keep mine", value: "mine", cls: "btn-warn" },
      ],
    });
    if (choice === "compare") { compare = true; continue; }
    if (choice === "reload") {
      if (diskText === null) return "cancel";
      reloadTab(t, diskText, disk.hash);
      return "reload";
    }
    if (choice === "mine") {
      await writeTab(t, mine, { force: true });
      return "mine";
    }
    return "cancel";
  }
}

// Show the disk's text in a tab (its own edits, if any, are dropped).
function reloadTab(t, text, hash) {
  t.text = text;
  t.saved = text;
  t.hash = hash || null;
  t.diskHash = null;
  dropDraft(t.abs);
  const ed = window.ideEditor;
  if (ed && state.active === t.abs && ed.relPath === t.rel) {
    ed.setText(text);
    applyMarkers();
  } else if (ed) {
    ed.forget(t.rel);
  }
}

// Bring tabs without edits up to date with the disk; say which tabs with
// edits now differ from it (their next save asks).
let _refreshing = null;
function refreshFromDisk() {
  if (_refreshing || !state.designDir || !state.tabs.length) return _refreshing;
  _refreshing = (async () => {
    const dir = state.designDir;
    const reloaded = [], flagged = [];
    for (const t of state.tabs.slice()) {
      if (!t.hash) continue;
      let r = null;
      try { r = await api.readText(t.abs); } catch (_e) { r = null; }
      if (state.designDir !== dir) return;
      if (!state.tabs.includes(t)) continue;
      stash();
      const act = refreshAction(t, r && typeof r.text === "string" ? r.hash : null);
      if (act === "reload") { reloadTab(t, r.text, r.hash); reloaded.push(t.rel); }
      else if (act === "flag" && t.diskHash !== r.hash) { t.diskHash = r.hash; flagged.push(t.rel); }
    }
    if (reloaded.length || flagged.length) renderTabs();
    if (flagged.length) note(flagged.join(", ") + (flagged.length === 1 ? " was" : " were") +
      " changed on disk. Your edits are kept here; saving asks which version to keep.", true);
    else if (reloaded.length) note("Reloaded " + reloaded.join(", ") + ": it changed on disk.");
  })().finally(() => { _refreshing = null; });
  return _refreshing;
}

function wireDiskSync() {
  document.addEventListener("g:config_changed", () => { refreshFromDisk(); });
  let last = 0;
  const onBack = () => {
    if (!ideShowing() || Date.now() - last < 1500) return;
    last = Date.now();
    refreshFromDisk();
  };
  window.addEventListener("focus", onBack);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") onBack(); });
}

// The simulator's compile errors describe the text it compiled: once a file
// is saved again they are out of date (the next syntax check or simulation
// says what still stands).
function dropSimDiags(rels) {
  const before = (state.simDiags || []).length;
  state.simDiags = (state.simDiags || []).filter((d) => !rels.some((r) => sameFile(d.file, r)));
  if (state.simDiags.length !== before) { renderProblems(); applyMarkers(); }
}

// Save every changed tab (before a simulation, like an IDE's "save all").
// A tab whose file changed on disk asks first, as a plain save does; the
// files left unsaved that way come back, so the caller doesn't check or
// simulate text that isn't on disk.
async function saveAll() {
  stash();
  const skipped = [];
  for (const t of state.tabs.filter((x) => x.text !== x.saved)) {
    try {
      const how = await saveTab(t, t.text);
      if (how === "cancel") skipped.push(t.rel);
    } catch (_e) { /* the simulation reports what's wrong */ }
  }
  renderTabs();
  return skipped;
}

// Check on save: every save of a Verilog file re-runs the syntax check, so the
// squiggles stay current without a button press. On by default, remembered.
function setupAutoLint() {
  const cb = document.getElementById("ide-autolint");
  if (!cb) return;
  let v = null;
  try { v = localStorage.getItem("ll.lintOnSave"); } catch (_e) {}
  cb.checked = v === null ? true : v === "1";
  cb.addEventListener("change", () => {
    try { localStorage.setItem("ll.lintOnSave", cb.checked ? "1" : "0"); } catch (_e) {}
  });
}
function lintOnSave() { return !!document.getElementById("ide-autolint")?.checked; }

// Add files from the computer: Verilog to src/, testbenches to verify/,
// anything else at the top of the project. New design files join the config's
// file list (the Setup page does that, through the same path as its ticks).
function wireUpload() {
  const btn = document.getElementById("ide-upload");
  if (!btn) return;
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.accept = ".v,.sv,.vh,.svh,.vhd,.mem,.hex,.sdc,.cfg,.tcl,.json,.yaml,.yml,.txt,.md";
  input.hidden = true;
  btn.after(input);
  btn.addEventListener("click", () => {
    if (!state.designDir) { note("Open a project first (Setup tab).", true); return; }
    input.click();
  });
  input.addEventListener("change", async () => {
    const files = [...input.files];
    input.value = "";
    if (!files.length) return;
    // A file of the same name asks first: Replace or Keep both (addfiles.js).
    const res = await addProjectFiles(files, { report: (msg, bad) => note(msg, bad) });
    const added = res.added;
    if (added.length) note("Added " + added.join(", "));
    const base = state.designDir.replace(/\/+$/, "") + "/";
    const design = res.forConfig.filter((rel) => rel.startsWith("src/") && /\.s?v$/i.test(rel)).map((rel) => base + rel);
    if (design.length) document.dispatchEvent(new CustomEvent("g:sources_added", { detail: { files: design } }));
    document.dispatchEvent(new CustomEvent("g:sources_changed"));
    await loadTree();
    await loadTestbenches();
    // A replaced file that is open shows its new text.
    if (res.replaced.length) refreshFromDisk();
    if (added.length) {
      const f = state.files.find((x) => x.relpath === added[0]);
      if (f) openFile(f.abspath, f.relpath);
    }
  });
}

// ------------------------------------------------------------------ bottom panel

function setPanel(name) {
  document.querySelectorAll(".ide-ptab").forEach((b) => {
    const on = b.dataset.ptab === name;
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".ide-pane").forEach((p) => p.classList.toggle("is-on", p.dataset.pane === name));
  setPanelOpen(true);
}

function setPanelOpen(open) {
  const c = document.querySelector(".ide-center");
  if (!c) return;
  c.classList.toggle("panel-closed", !open);
  byId("ide-panel-toggle", (b) => b.setAttribute("aria-label", open ? "Hide the panel" : "Show the panel"));
  try { localStorage.setItem("ll.idePanelOpen", open ? "1" : "0"); } catch (_e) {}
}

// A waveform needs room: give the panel at least ~60% of the height unless
// the person has sized it themselves.
function growPanelFor() {
  const c = document.querySelector(".ide-center");
  if (!c) return;
  let sized = false;
  try { sized = !!localStorage.getItem("ll.idePanelH"); } catch (_e) {}
  const cur = parseFloat(getComputedStyle(c).getPropertyValue("--ide-panel-h")) || 36;
  if (!sized && cur < 58) c.style.setProperty("--ide-panel-h", "60%");
}

function setPanelMax(on) {
  const c = document.querySelector(".ide-center");
  if (!c) return;
  c.classList.toggle("panel-max", !!on);
  byId("ide-panel-max", (b) => {
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "Give the editor its space back" : "Give the panel the editor's space (the file tabs stay)";
  });
  if (on) setPanelOpen(true);
}

function wirePanel() {
  const center = document.querySelector(".ide-center");
  if (!center) return;
  byId("ide-panel-max", (b) => b.addEventListener("click", () => setPanelMax(!center.classList.contains("panel-max"))));
  document.querySelectorAll(".ide-ptab").forEach((b) => b.addEventListener("click", () => setPanel(b.dataset.ptab)));
  byId("ide-panel-toggle", (b) => b.addEventListener("click", () =>
    setPanelOpen(center.classList.contains("panel-closed"))));
  let h = null, open = "1";
  try { h = localStorage.getItem("ll.idePanelH"); open = localStorage.getItem("ll.idePanelOpen") || "1"; } catch (_e) {}
  if (h && +h >= 15 && +h <= 80) center.style.setProperty("--ide-panel-h", h + "%");
  if (open === "0") center.classList.add("panel-closed");
  // Drag the bar between the editor and the panel to share the height.
  const split = document.getElementById("ide-split");
  if (split) {
    const setPct = (pct) => {
      pct = Math.max(15, Math.min(80, pct));
      center.style.setProperty("--ide-panel-h", pct.toFixed(1) + "%");
      try { localStorage.setItem("ll.idePanelH", pct.toFixed(1)); } catch (_e) {}
    };
    split.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      split.setPointerCapture(e.pointerId);
      split.classList.add("is-drag");
      const move = (ev) => {
        const r = center.getBoundingClientRect();
        setPct(((r.bottom - ev.clientY) / r.height) * 100);
      };
      const up = () => {
        split.classList.remove("is-drag");
        split.removeEventListener("pointermove", move);
        split.removeEventListener("pointerup", up);
        split.removeEventListener("pointercancel", up);
        split.removeEventListener("lostpointercapture", up);
      };
      split.addEventListener("pointermove", move);
      split.addEventListener("pointerup", up);
      split.addEventListener("pointercancel", up);
      split.addEventListener("lostpointercapture", up);
    });
    split.addEventListener("keydown", (e) => {
      const cur = parseFloat(getComputedStyle(center).getPropertyValue("--ide-panel-h")) || 36;
      if (e.key === "ArrowUp") { e.preventDefault(); setPct(cur + 5); }
      if (e.key === "ArrowDown") { e.preventDefault(); setPct(cur - 5); }
    });
  }
}

// ------------------------------------------------------------------ syntax check

async function lint({ quiet = false } = {}) {
  if (state.linting) { if (!quiet) note("A syntax check is already running."); return; }
  // A standalone verilator --lint-only job, not the hardening flow. The server
  // checks what a run reads (the config's VERILOG_FILES, VERILOG_DEFINES and
  // VERILOG_INCLUDE_DIRS), plus the testbenches sent here; `sources` (every
  // Verilog file) is only used for a project whose config names no files.
  const sources = state.files.filter((f) => /\.s?v$/.test(f.relpath)).map((f) => f.relpath);
  // VHDL: GHDL in the image checks it (experimental, where the probe found it).
  const vhdlFiles = state.vhdlCaps && state.vhdlCaps.state === "ready"
    ? state.files.filter((f) => /\.vhdl?$/i.test(f.relpath)).map((f) => f.relpath) : [];
  if (!sources.length && !vhdlFiles.length) {
    note(state.files.some((f) => /\.vhdl?$/i.test(f.relpath))
      ? "VHDL can't be checked here: " + ((state.vhdlCaps && state.vhdlCaps.note) || "GHDL from the LibreLane image is needed.")
      : "There's no Verilog file to check yet.", true);
    return;
  }
  const benches = state.files.filter((f) => f.role === "testbench" && /\.s?v$/i.test(f.relpath)).map((f) => f.relpath);
  // Verilator reads the files on disk: an unsaved tab would be checked as it
  // was last saved (a pasted module against a bench that uses its new ports
  // reads as a page of "Pin not found"). Save everything first, as Simulate does.
  setLintBusy(true);
  const unsaved = await saveAll();
  if (unsaved.length) {
    setLintBusy(false);
    note("Not checked: " + unsaved.join(", ") + " changed on disk and your edits aren't saved. Save it to choose which version to keep.", true);
    return;
  }
  note("Checking the syntax with Verilator…");
  state.lintLines = [];
  armLintSafety();
  if (!quiet) setPanel("problems");
  try {
    const start = await api.lintStart({ run_mode: state.runMode, sources, include_dirs: includeDirs(),
      from_config: true, extra_sources: benches, vhdl_sources: vhdlFiles });
    if (!start.ok) { note("Lint refused: " + (start.reason || start.error || "?"), true); setLintBusy(false); }
    else {
      noteLintInputs(start);
      if (start.vhdl_note) appendLog(start.vhdl_note, "ll-warn");
    }
  } catch (ex) {
    // A 400 "a lint is already running" throws here too: reset so the button is
    // usable, and tell the user how to recover instead of looking frozen.
    note("Lint failed: " + (ex.message || ex), true);
    setLintBusy(false);
  }
}

// What the check read: the files (for the Problems count and the Run gate,
// which counts only errors in files a run builds) and what the config said.
function noteLintInputs(start) {
  const cfg = start && start.config;
  state.lintSources = Array.isArray(start && start.sources) ? start.sources : null;
  state.lintFlowFiles = cfg && cfg.mode === "list" && Array.isArray(cfg.files) && cfg.files.length ? cfg.files : null;
  state.lintConfigNote = configNote(cfg);
}

// A line about the config's file list when it isn't what the check could use.
function configNote(cfg) {
  if (!cfg) return "";
  if (cfg.missing && cfg.missing.length) {
    return (cfg.name || "The config") + " lists " + cfg.missing.slice(0, 3).join(", ") + (cfg.missing.length > 3 ? "…" : "") +
      ", which " + (cfg.missing.length === 1 ? "doesn't" : "don't") + " exist. A run stops on that.";
  }
  if (cfg.mode !== "list" || !(cfg.files || []).length) {
    return (cfg.reason || (cfg.name ? cfg.name + " names no Verilog files (VERILOG_FILES)" : "This project has no config yet")) +
      ", so every Verilog file in the project was used.";
  }
  return "";
}

// Is this diagnostic in a file a run reads? The config's files when known (an
// `include file the flow pulls in counts too), else any non-testbench file.
function inFlow(d) {
  if (isTestbenchFile(d.file)) return false;
  const flow = state.lintFlowFiles;
  if (!flow) return true;
  return flow.some((f) => sameFile(f, d.file)) || /\.(vh|svh)$/i.test(d.file || "");
}

function sameFile(a, b) {
  a = String(a || "").replace(/^\.\//, "");
  b = String(b || "").replace(/^\.\//, "");
  return a === b || a.endsWith("/" + b) || b.endsWith("/" + a);
}
function projectFile(file) { return state.files.find((f) => sameFile(f.relpath, file)) || null; }
function isTestbenchFile(file) {
  const f = projectFile(file);
  return f ? f.role === "testbench" : /(^|\/)(tb_[^/]*|[^/]*_tb\.s?v|[^/]*testbench[^/]*)$/i.test(file);
}
// Style warnings in simulation-only code: folded away, not counted.
function isNoise(d) {
  return d.severity === "warning" && TESTBENCH_STYLE.has(d.code) && isTestbenchFile(d.file);
}
// The lint check and a Verilator simulation build report the same problem on
// the same line: each is counted and listed once (the badge said "4 · 2"
// while the build said 2 errors).
export function mergeDiags(lint, sim, simLabel = "Simulator") {
  const key = (d) => d.file + ":" + d.line + ":" + (d.code || d.msg);
  const seen = new Set((lint || []).map(key));
  return (lint || []).map((d) => ({ ...d, src: "Verilator" }))
    .concat((sim || []).filter((d) => !seen.has(key(d))).map((d) => ({ ...d, src: simLabel })));
}

function allDiags() {
  return mergeDiags(state.diags, state.simDiags, state.simEngineLabel || "Simulator");
}

function applyMarkers() {
  const ed = window.ideEditor;
  if (!ed || !ed.relPath) return;
  const mine = allDiags().filter((d) => !isNoise(d) && sameFile(d.file, ed.relPath));
  ed.setMarkers(mine.map((d) => ({ line: d.line, col: d.col, severity: d.severity, msg: d.msg, code: d.code })));
}

// Apply diagnostics straight from the lint_done event payload (falls back to a
// fetch of the last result if called without one).
async function applyLint(diags, ev) {
  try {
    if (!Array.isArray(diags)) {
      const r = await fetch("/api/lint-result", { headers: { "X-Requested-With": "XMLHttpRequest" } });
      const body = await r.json();
      diags = (body.data && body.data.diagnostics) || [];
    }
    // Verilator that didn't run (not found, crashed, stopped at the time
    // limit) finds nothing, which is not the same as finding nothing wrong.
    const hasError = diags.some((d) => d.severity === "error");
    const broke = !!ev && (ev.timed_out || (ev.returncode !== undefined && ev.returncode !== 0 && !hasError));
    const lines = state.lintLines || [];
    state.lintLines = [];
    if (broke) {
      state.lintFailed = { timedOut: !!ev.timed_out, rc: ev.returncode,
        tail: lines.filter((l) => l.trim()).slice(-12) };
      renderProblems();
      if (!ev.timed_out) note("The syntax check couldn't run (Verilator exit code " + ev.returncode + "). See Problems.", true);
      return;
    }
    state.lintFailed = null;
    state.diags = implicitIsError(diags);
    state.lintChecked = true;
    state.lintAt = Date.now();
    applyMarkers();
    const real = diags.filter((d) => !isNoise(d));
    const errors = real.filter((d) => d.severity === "error").length;
    const warnings = real.filter((d) => d.severity === "warning").length;
    note(errors || warnings ? "Syntax check: " + plural(errors, "error") + ", " + plural(warnings, "warning") + "."
      : "Syntax check: no problems.", errors > 0);
    renderProblems();
    recordRtlStatus();
  } catch (_e) {}
}

// Verilator only warns about an implicitly declared wire, but Yosys stops
// synthesis on it ("Identifier ... is implicitly declared"), so in a file
// the run builds it is an error here, said in those words. (A testbench may
// keep one: it only simulates.)
export function implicitIsError(diags, inRun = (d) => inFlow(d)) {
  return (diags || []).map((d) => d.code === "IMPLICIT" && d.severity === "warning" && inRun(d)
    ? { ...d, severity: "error", msg: d.msg + " (synthesis stops on this: declare the wire)" } : d);
}

function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

const IC_ERR = "<svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><circle cx='12' cy='12' r='8.5'/><path d='M9 9l6 6M15 9l-6 6'/></svg>";
const IC_WARN = "<svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M12 4l9 16H3z'/><path d='M12 10v4M12 17.5v.01'/></svg>";
const IC_CARET = "<svg viewBox='0 0 24 24' width='12' height='12' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M6 9l6 6 6-6'/></svg>";

function diagRow(d) {
  const why = d.code ? explain(d.code) : d.severity === "error" ? explainMessage(d.msg) : "";
  return "<div class='pb-item pb-" + d.severity + "' role='button' tabindex='0' data-file='" + fmt.escape(d.file) +
    "' data-line='" + (d.line || 1) + "' data-col='" + (d.col || 1) + "'>" +
    "<span class='pb-ic'>" + (d.severity === "error" ? IC_ERR : IC_WARN) + "</span>" +
    "<span class='pb-msg'>" + fmt.escape(d.msg) +
      (d.code ? " <span class='pb-code' title='" + fmt.escape(why || "Verilator code " + d.code) + "'>" + fmt.escape(d.code) + "</span>" : "") +
      (d.src !== "Verilator" ? " <span class='pb-src'>" + fmt.escape(d.src) + "</span>" : "") + "</span>" +
    "<span class='pb-loc'>Ln " + (d.line || 1) + (d.col ? ", Col " + d.col : "") + "</span>" +
    (why ? "<span class='pb-why'>" + fmt.escape(why) + "</span>" : "") + "</div>";
}

function fileGroups(list) {
  const by = new Map();
  for (const d of list) {
    const k = d.file;
    if (!by.has(k)) by.set(k, []);
    by.get(k).push(d);
  }
  return [...by.entries()].sort((a, b) =>
    (b[1].some((d) => d.severity === "error") - a[1].some((d) => d.severity === "error")) || a[0].localeCompare(b[0]))
    .map(([file, ds]) => {
      ds.sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "error" ? -1 : 1) || a.line - b.line);
      const cut = file.lastIndexOf("/");
      return "<div class='pb-group'><button class='pb-fhead' aria-expanded='true'>" + IC_CARET +
        "<span class='pb-fname'><span class='pb-dir'>" + fmt.escape(file.slice(0, cut + 1)) + "</span>" + fmt.escape(file.slice(cut + 1)) + "</span>" +
        "<span class='pb-fcount'>" + ds.length + "</span></button><div class='pb-items'>" + ds.map(diagRow).join("") + "</div></div>";
    }).join("");
}

// Problems: grouped by file, errors first, each with its Verilator code and,
// on click, a plain explanation. Style notes in testbenches sit folded below.
function renderProblems() {
  const host = document.getElementById("ide-diags");
  const count = document.getElementById("ide-problems-count");
  const all = allDiags();
  const noise = all.filter(isNoise);
  const real = all.filter((d) => !isNoise(d));
  const errors = real.filter((d) => d.severity === "error").length;
  const warnings = real.length - errors;
  const checked = state.lintChecked || (state.simDiags || []).length;
  if (count) {
    count.className = "pcount" + (errors ? " pcount-err" : warnings ? " pcount-warn" : "");
    count.textContent = !checked ? "" : errors || warnings ? errors + " · " + warnings : "0";
    count.title = !checked ? "Not checked yet" : plural(errors, "error") + ", " + plural(warnings, "warning") +
      (noise.length ? " (and " + noise.length + " testbench style notes)" : "");
  }
  if (!host) return;
  if (state.lintFailed && !state.linting) {
    const lf = state.lintFailed;
    host.innerHTML = "<div class='pb-empty pb-failed'><p><b>" + (lf.timedOut ? "The syntax check took too long and was stopped."
      : "The syntax check couldn't run.") + "</b> " + (lf.timedOut
      ? "Verilator didn't finish within 90 s. On WSL, a Windows Verilator on the PATH can hang; install the Linux one from the Tools tab."
      : "Verilator stopped (exit code " + fmt.escape(String(lf.rc)) + ") without listing a problem, so nothing was checked. Its last output:") + "</p>" +
      (lf.tail.length ? "<pre class='pb-tail'>" + fmt.escape(lf.tail.join("\n")) + "</pre>" : "") +
      "<button class='btn btn-sm' data-pb='lint'>Try again</button></div>";
    return;
  }
  if (!checked) {
    host.innerHTML = "<div class='pb-empty'><p><b>Not checked yet.</b> Check syntax runs Verilator over the files the config " +
      "builds into the chip, with its defines, and over the testbenches. It takes seconds and catches most mistakes before you simulate.</p>" +
      "<button class='btn btn-sm' data-pb='lint'" + (state.designDir ? "" : " disabled title='Open a project first'") + ">Check syntax</button></div>";
    return;
  }
  const f = state.pbFilter || (state.pbFilter = { error: true, warning: true });
  const shown = real.filter((d) => f[d.severity]);
  const files = state.lintSources ? state.lintSources.length
    : new Set(state.files.filter((x) => /\.s?v$/.test(x.relpath)).map((x) => x.relpath)).size;
  const when = state.lintAt ? new Date(state.lintAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  host.innerHTML =
    "<div class='pb-bar'>" +
      "<button class='pb-filter pb-f-error' data-sev='error' aria-pressed='" + f.error + "'>" + IC_ERR + "Errors <b>" + errors + "</b></button>" +
      "<button class='pb-filter pb-f-warning' data-sev='warning' aria-pressed='" + f.warning + "'>" + IC_WARN + "Warnings <b>" + warnings + "</b></button>" +
      "<span class='pb-meta'" + (state.lintConfigNote ? " title='" + fmt.escape(state.lintConfigNote) + "'" : "") + ">" +
        (state.lintChecked ? ((state.lintSources || []).some((s) => /\.vhdl?$/i.test(s)) ? "Verilator and GHDL, " : "Verilator, ") +
          plural(files, "file") + (when ? ", checked at " + when : "") : "From the last simulation") + "</span>" +
      "<span class='ide-spacer'></span>" +
      "<button class='btn btn-ghost btn-sm' data-pb='lint'" + (state.linting ? " disabled" : "") + ">Check again</button>" +
    "</div>" +
    "<div class='pb-list'>" +
      (state.lintChecked && state.lintConfigNote ? "<p class='pb-clean'>" + fmt.escape(state.lintConfigNote) + "</p>" : "") +
      (shown.length ? fileGroups(shown)
        : "<p class='pb-clean'>" + (real.length ? "Nothing to show with these filters."
          : "No problems. Verilator read " + plural(files, "file") + " without an error or warning.") + "</p>") +
      (noise.length ? "<details class='pb-noise'" + (state.pbNoiseOpen ? " open" : "") + "><summary>" + plural(noise.length, "style note") +
        " in testbench files <span>Unused outputs, delays and initial values are normal in code that only simulates, so these aren't counted.</span></summary>" +
        fileGroups(noise) + "</details>" : "") +
    "</div>";
}

function wireProblems() {
  const host = document.getElementById("ide-diags");
  if (!host) return;
  const jump = async (item) => {
    item.classList.toggle("is-open");
    const ln = parseInt(item.dataset.line, 10), col = parseInt(item.dataset.col, 10);
    const f = projectFile(item.dataset.file);
    if (!f) { note(item.dataset.file + " isn't one of this project's files.", true); return; }
    if (f.abspath !== state.active) await openFile(f.abspath, f.relpath);
    if (ln && window.ideEditor) window.ideEditor.gotoLine(ln, col || undefined);
  };
  host.addEventListener("click", (e) => {
    if (e.target.closest("[data-pb='lint']")) { lint(); return; }
    const flt = e.target.closest(".pb-filter");
    if (flt) {
      const f = state.pbFilter || (state.pbFilter = { error: true, warning: true });
      f[flt.dataset.sev] = !f[flt.dataset.sev];
      renderProblems();
      return;
    }
    const head = e.target.closest(".pb-fhead");
    if (head) {
      const open = head.getAttribute("aria-expanded") !== "false";
      head.setAttribute("aria-expanded", open ? "false" : "true");
      return;
    }
    const item = e.target.closest(".pb-item");
    if (item) jump(item);
  });
  host.addEventListener("keydown", (e) => {
    const item = e.target.closest(".pb-item");
    if (item && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); jump(item); }
  });
  host.addEventListener("toggle", (e) => {
    if (e.target.classList && e.target.classList.contains("pb-noise")) state.pbNoiseOpen = e.target.open;
  }, true);
}

// ------------------------------------------------------------------ simulation

async function loadTestbenches() {
  const sel = document.getElementById("ide-tb");
  if (!sel) return;
  if (!state.designDir) { sel.innerHTML = "<option value=''>(no project)</option>"; paintSimPick(); return; }
  try {
    const prev = sel.value;
    const r = await api.simTestbenches(state.designDir);
    const tbs = r.testbenches || [];
    state.testbenches = tbs;
    sel.innerHTML = tbs.map((t) => "<option value='" + fmt.escape(t) + "'>" + fmt.escape(t) + "</option>").join("")
      || "<option value=''>(no testbench yet)</option>";
    if (prev && tbs.includes(prev)) sel.value = prev;
    deriveTopFromTb();
    paintSimPick();
  } catch (_e) {}
}

// The Simulate button names the testbench it runs; its arrow switches it.
function paintSimPick() {
  const sel = document.getElementById("ide-tb");
  const tb = sel ? sel.value : "";
  byId("ide-sim-tbname", (el) => { el.textContent = tb ? tb.split("/").pop().replace(/\.s?v$/i, "") : "no testbench"; });
  byId("ide-sim-pick", (b) => { b.title = tb ? "Simulate runs " + tb + ". Click to pick another testbench." : "No testbench yet"; });
  byId("ide-sim", (b) => { b.title = tb ? "Build and run " + tb + ", then show its waveform" : "Write a testbench first"; });
}

function wireSimPick() {
  const pick = document.getElementById("ide-sim-pick");
  const menu = document.getElementById("ide-sim-menu");
  if (!pick || !menu) return;
  const close = () => { menu.hidden = true; };
  pick.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isLayerOpen(menu)) { closeLayer(menu); return; }
    const sel = document.getElementById("ide-tb");
    const tbs = state.testbenches || [];
    const eng = document.getElementById("ide-sim-engine");
    menu.innerHTML = "<div class='ism-h'>Testbench</div>" +
      (tbs.length ? tbs.map((t) => "<button role='menuitemradio' aria-checked='" + (sel && sel.value === t) + "' data-tb='" + fmt.escape(t) + "'>" + fmt.escape(t) + "</button>").join("")
        : "<p class='ism-none'>No testbench yet.</p>") +
      "<div class='ism-sep'></div>" +
      "<button role='menuitem' data-ism='new'>New testbench…</button>" +
      "<button role='menuitem' data-ism='settings'>Simulator: " + fmt.escape(eng ? eng.options[eng.selectedIndex].text.replace(/ \(.*\)$/, "") : "Auto") + "…</button>";
    menu.hidden = false;
    // On the overlay stack: Esc, arrows, an outside press, focus back on the button.
    openLayer(menu, { kind: "menu", owner: pick, onClose: close });
  });
  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    closeLayer(menu);
    if (b.dataset.tb) {
      byId("ide-tb", (sel) => { sel.value = b.dataset.tb; byId("ide-top", (x) => { x.value = ""; }); deriveTopFromTb(); });
      paintSimPick();
    } else if (b.dataset.ism === "new") newFile("tb_v");
    else if (b.dataset.ism === "settings") { setPanel("sim"); document.getElementById("ide-sim-engine")?.focus(); }
  });
}

// Toggle the Simulate / Stop buttons. While a sim runs Simulate is disabled so
// a second run can't race the first (the server rejects it anyway), and Stop
// appears so a runaway bench can be killed.
function setSimBusy(busy) {
  state.simRunning = !!busy;
  const run = document.getElementById("ide-sim");
  const stop = document.getElementById("ide-sim-stop");
  if (run) {
    run.disabled = !!busy || !state.designDir;
    run.innerHTML = busy ? "Simulating…"
      : "<svg viewBox='0 0 24 24' width='13' height='13' fill='currentColor' aria-hidden='true'><path d='M7 4l13 8-13 8z'/></svg> Simulate";
  }
  byId("ide-sim-pick", (b) => { b.disabled = !!busy || !state.designDir; });
  if (stop) stop.hidden = !busy;
}

// Toggle the Check-syntax (lint) button so a wedged lint shows progress and a
// second click can't pile up before the first finishes.
function setLintBusy(busy) {
  state.linting = !!busy;
  const b = document.getElementById("ide-lint");
  if (b) { b.disabled = !!busy || !state.designDir; b.textContent = busy ? "Checking…" : "Check syntax"; }
  document.querySelector("#ide-diags [data-pb='lint']")?.toggleAttribute("disabled", !!busy);
}

// Client-side safety nets. The server always emits sim_done / lint_done (the job
// has its own watchdog), but if that event is ever missed — dropped SSE frame, a
// reconnect gap — the button must NOT stay stuck on "Simulating…"/"Checking…"
// forever (the "works only once" symptom). These timers force the busy state off
// well after the server's own watchdog would have fired, as a last resort.
let _simSafety = 0;
let _lintSafety = 0;
const LINT_SAFETY_MS = 100000;  // > server DEFAULT_LINT_TIMEOUT (90s) + slack
// The simulation's time limit, seconds (Settings > Editor; the server clamps it).
export function simTimeLimit(v = getPref("simTimeLimit")) {
  const n = parseInt(v, 10);
  return n > 0 ? Math.max(5, Math.min(3600, n)) : 120;
}
function limitText(sec) {
  return sec % 60 === 0 && sec >= 60 ? (sec / 60) + " min" : sec + " s";
}
function armSimSafety() {
  clearTimeout(_simSafety);
  // Well after the server's own watchdog (the time limit) would have fired.
  const SIM_SAFETY_MS = (simTimeLimit() + 20) * 1000;
  _simSafety = setTimeout(() => {
    if (!document.getElementById("ide-sim")?.disabled) return;
    if (state.simRun) state.simRun.done = true;
    setSimBusy(false);
    note("No result received. The simulation process may be unresponsive. Try again or check the logs.", true);
  }, SIM_SAFETY_MS);
}
function armLintSafety() {
  clearTimeout(_lintSafety);
  _lintSafety = setTimeout(() => {
    setLintBusy(false);
    note("No lint result received. The linter may be unresponsive. Please try again.", true);
  }, LINT_SAFETY_MS);
}

const ENGINE_LABEL = { iverilog: "Icarus Verilog", verilator: "Verilator", ghdl: "GHDL (VHDL, experimental)" };

// Folders that hold HDL files, for `include lookups (src/defs.vh from a
// testbench in verify/, say). Paths relative to the project.
function includeDirs() {
  const dirs = new Set(["."]);
  for (const f of state.files) {
    if (!/\.(s?v|vh|svh)$/i.test(f.relpath)) continue;
    const cut = f.relpath.lastIndexOf("/");
    if (cut > 0) dirs.add(f.relpath.slice(0, cut));
  }
  return [...dirs];
}

async function simulate() {
  // One simulation at a time: a second press (the waveform's Simulate, a
  // file menu) while one runs would only be refused and reset the buttons.
  if (state.simRunning || batchRunning()) { setPanel("sim"); note("A simulation is already running. Stop it first, or wait for it to finish."); return; }
  const tb = (document.getElementById("ide-tb") || {}).value;
  // The top is the *testbench* module; if the user left it blank the server
  // auto-derives it from the testbench (the common case), so it's optional.
  const top = (document.getElementById("ide-top")?.value || "").trim();
  const simEngine = (document.getElementById("ide-sim-engine") || {}).value || "auto";
  setPanel("sim");
  if (!tb) {
    simStatus({ verdict: "none" });
    note("There's no testbench to simulate yet. Use + next to Simulation in the explorer to write one.", true);
    return;
  }
  // Simulate what's on screen: save every changed file first.
  const unsaved = await saveAll();
  if (unsaved.length) {
    simStatus({ verdict: "none-saved", tb, error: unsaved.join(", ") });
    note("Not simulated: " + unsaved.join(", ") + " changed on disk and your edits aren't saved. Save it to choose which version to keep.", true);
    return;
  }
  // The design as a run builds it (the server takes the config's
  // VERILOG_FILES, VERILOG_DEFINES and VERILOG_INCLUDE_DIRS), plus
  // simulation-only helpers (a bus model or memory model in verify/), but
  // not the other testbenches: each of those is a top of its own. `sources`
  // (every Verilog file) is only used when the config names no files.
  const benches = new Set(state.testbenches || []);
  const sources = state.files.filter((f) => /\.s?v$/i.test(f.relpath) && f.relpath !== tb &&
      (f.role !== "testbench" || !benches.has(f.relpath)))
    .map((f) => f.relpath);
  const helpers = state.files.filter((f) => f.role === "testbench" && /\.s?v$/i.test(f.relpath) &&
      f.relpath !== tb && !benches.has(f.relpath)).map((f) => f.relpath);
  // The bench's run settings (parameters, plusargs, defines, its own limit).
  const runSet = payloadFor(tb);
  const limit = runSet.timeout || simTimeLimit();
  state.waveTb = tb;
  state.simDiags = [];
  state.simRun = { tb, started: Date.now(), lines: [], engine: null, top: null, done: false, limit };
  appendLog("── simulate " + tb + " · " + new Date().toLocaleTimeString() + " ──", "ll-head");
  note("Building and running the simulation…");
  setSimBusy(true);
  simStatus({ verdict: "running" });
  armSimSafety();
  try {
    const r = await api.simStart({
      top, testbench: tb, sources, trace: "vcd", run_mode: state.runMode, sim_engine: simEngine,
      include_dirs: includeDirs(), from_config: true, extra_sources: helpers, testbenches: [...benches],
      params: runSet.params, plusargs: runSet.plusargs, run_defines: runSet.run_defines,
      timeout: limit,
    });
    if (!r.ok) { state.simRun.done = true; clearTimeout(_simSafety); note("Sim refused: " + (r.error || "?"), true); setSimBusy(false); simStatus({ verdict: "refused", error: r.error }); }
    else {
      if (r.top) { const x = document.getElementById("ide-top"); if (x && !x.value) x.placeholder = r.top; }
      state.simRun.engine = r.sim_engine || simEngine;
      state.simRun.top = r.top || top;
      if (r.timeout) state.simRun.limit = r.timeout;
      const cn = configNote(r.config);
      if (cn) appendLog(cn, "ll-warn");
      // The exact command, so it can be read and run by hand.
      if (r.command) appendLog("$ " + r.command, "ll-dim ll-cmd");
      state.simEngineLabel = ENGINE_LABEL[state.simRun.engine] || state.simRun.engine;
      note("Simulating with " + state.simEngineLabel + " (top: " + (r.top || top) + ")…");
      simStatus({ verdict: "running" });
    }
  } catch (ex) {
    // A 400 "already running" lands here too — surface it and reset the button.
    state.simRun.done = true;
    clearTimeout(_simSafety);
    note("Sim failed: " + (ex.message || ex), true);
    setSimBusy(false);
    simStatus({ verdict: "refused", error: ex.message || String(ex) });
  }
}

async function stopSim() {
  note("Stopping simulation…");
  try { await api.simCancel(); } catch (_e) {}
  // sim_done will fire from the cancel and flip the button back; flip now too in
  // case the process was already gone.
  setSimBusy(false);
}

// Show the testbench module the server will pick, so nobody types the DUT's
// name by mistake (which gives no waveform).
function deriveTopFromTb() {
  const sel = document.getElementById("ide-tb");
  const topEl = document.getElementById("ide-top");
  if (!sel || !topEl || topEl.value.trim()) return;
  // Heuristic mirror of the server: <name>_tb.v → <name>_tb, tb_<name> → tb_<name>.
  const base = (sel.value || "").split(/[\\/]/).pop().replace(/\.s?v$/, "");
  topEl.placeholder = base || "from the testbench";
}

// Compile errors from the simulator's output, as diagnostics for Problems and
// the editor. Icarus: "file.v:12: syntax error" / "file.v:12: error: …";
// Verilator: "%Error: file.v:12:5: …".
const VLT_RX = /^%(Error|Warning)(?:-([A-Z0-9_]+))?:\s*([^:\s][^:]*):(\d+):(?:(\d+):)?\s*(.*)$/;
const IV_RX = /^((?:[\w.\-]+\/)*[\w.\-]+\.(?:s?v|vh|svh)):(\d+):\s*(?:(error|warning|sorry):\s*)?(.+)$/i;
function parseSimDiags(lines) {
  const out = [];
  for (const l of lines) {
    let m = l.match(VLT_RX);
    // "%Error: tb.v:83: Verilog $stop" is Verilator ending at a $fatal/$stop, not a build error.
    if (m && /^Verilog \$(stop|finish|fatal)\b/.test(m[6])) continue;
    if (m && projectFile(m[3])) { out.push({ file: projectFile(m[3]).relpath, line: +m[4], col: m[5] ? +m[5] : 1, severity: m[1].toLowerCase(), code: m[2] || "", msg: m[6] }); continue; }
    m = l.match(IV_RX);
    if (m && projectFile(m[1]) && !/\$finish|\$stop/.test(m[4])) {
      const sev = (m[3] || "").toLowerCase() === "warning" ? "warning" : "error";
      // Runtime prints (e.g. a $display that starts with a path) aren't compile errors.
      if (!m[3] && !/syntax|error|unknown|undeclared|not declared|unable|illegal|expect|malformed|invalid/i.test(m[4])) continue;
      out.push({ file: projectFile(m[1]).relpath, line: +m[2], col: 1, severity: sev, code: "", msg: m[4] });
    }
  }
  // Icarus often repeats a message for one line; show each once.
  const seen = new Set();
  return out.filter((d) => { const k = d.file + ":" + d.line + ":" + d.msg; if (seen.has(k)) return false; seen.add(k); return true; });
}

const FAIL_RX = /\b(FAIL|FAILED|FAILURE|MISMATCH|ERROR)\b|\bassert(ion)? fail/i;
const PASS_RX = /\b(PASS|PASSED|SUCCESS)\b/i;
const TOOLISH_RX = /^(%Warning|%Error|VCD info:|- V e r i l a t|- Verilator|\s*(g\+\+|ccache|make)\b|make\[|── )/;

// Where the run ended: Icarus prints "$finish called at 242000 (1ps)".
function parseFinish(lines) {
  for (const l of lines) {
    const m = l.match(/\$finish called at (\d+) \((\d+\s*[munpf]?s)\)/);
    if (m) return { ticks: +m[1], tick: parseTimescale(m[2].replace(/\s+/g, "")) };
    if (/Verilog \$finish/.test(l)) return { ticks: null };
  }
  return null;
}

function finishSim(ev) {
  const run = state.simRun;
  // Not this window's run (the pop-out and the cockpit share the server), or
  // already finished: nothing to report here.
  if (!run || run.done) return;
  run.done = true;
  clearTimeout(_simSafety);
  setSimBusy(false);
  if (run.orphan) {
    // Started in the project that was open before; its result belongs there.
    state.simRun = null;
    note("The previous project's simulation finished. Its result belongs to that project, so it isn't shown here.");
    return;
  }
  run.ms = Date.now() - run.started;
  state.simDiags = ev.cancelled ? [] : parseSimDiags(run.lines);
  renderProblems();
  applyMarkers();
  const out = run.lines.filter((l) => !TOOLISH_RX.test(l));
  // A self-checking bench prints a FAIL line per mismatch, then $fatal
  // (Icarus "FATAL: …", Verilator "%Fatal: …"): that one isn't counted again.
  const FATAL_RX = /^FATAL:|%Fatal\b/;
  run.fails = out.filter((l) => FAIL_RX.test(l) && !FATAL_RX.test(l)).length;
  if (!run.fails && out.some((l) => FATAL_RX.test(l))) run.fails = 1;
  run.passes = out.filter((l) => PASS_RX.test(l)).length;
  run.finish = parseFinish(run.lines);
  run.vcd = ev.vcd || null;
  run.rc = ev.returncode;
  const buildErrors = state.simDiags.filter((d) => d.severity === "error").length;
  run.verdict = ev.cancelled ? "stopped"
    : ev.timed_out ? (ev.vcd ? "timeout-partial" : "timeout")
    : !ev.ok ? (buildErrors ? "build" : run.fails ? "reported-fail" : "failed")
    : run.fails ? "reported-fail"
    : !ev.vcd ? "nowave"
    : run.passes ? "passed" : "ran";
  // The server's reading (simulate.judge) is the stricter one: an upper-case
  // PASS/FAIL at a line start before $finish, the exit code and the
  // simulator's own error lines ("No error found" is not a failure).
  const j = ev.judged;
  if (j && !ev.cancelled && !ev.timed_out) {
    run.judged = j;
    run.fails = j.fails || 0;
    run.passes = j.passes || 0;
    run.verdict = j.state === "build" ? (buildErrors ? "build" : "failed")
      : j.state === "fail" ? "reported-fail"
      : j.state === "start" ? "failed"
      : !ev.vcd ? "nowave"
      : j.state === "pass" ? "passed"
      : j.state === "stop" ? "stop" : "ran";
  }
  run.buildErrors = buildErrors;
  // It ran, but the testbench doesn't connect some of the module's inputs
  // (ports added since it was written): say so, with the rewrite offer.
  run.floating = ["ran", "passed", "nowave", "reported-fail"].includes(run.verdict)
    ? floatingPorts(state.simDiags, (file) => isTestbenchFile(file)) : [];
  simStatus(run);
  if (!ev.vcd && state.wave && state.wave.vcd) setWavePartial("old");
  if (ev.cancelled) note("Simulation stopped.", true);
  else if (ev.vcd) {
    loadWaveform(ev.vcd, ev.partial);
    if (ev.timed_out)
      note("Simulation reached the time limit (free-running clock without $finish). Displaying "
           + "partial waveform. Add $finish to your testbench for a complete run.", true);
  }
  else if (ev.timed_out)
    note("Simulation reached the time limit with no waveform generated. Add $finish to your "
         + "testbench so the simulation can terminate.", true);
  else if (ev.ok)
    note("Simulation ran but produced no waveform. Verify the testbench invokes "
         + "$dumpfile and $dumpvars, and that top is set to the testbench module.", true);
  else note(buildErrors ? "The simulation didn't build: " + plural(buildErrors, "error") + " (see Problems)."
    : "Simulation finished with errors. Review the log below for details.", true);
  recordRtlStatus();
  document.dispatchEvent(new CustomEvent("g:sim_done", { detail: { verdict: run.verdict, vcd: run.vcd, tb: run.tb } }));
}

const SS_ICON = {
  ok: "<svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><circle cx='12' cy='12' r='9'/><path d='M8 12.5l2.6 2.6L16 9.5'/></svg>",
  warn: IC_WARN.replace(/width='14' height='14'/, "width='18' height='18'"),
  fail: IC_ERR.replace(/width='14' height='14'/, "width='18' height='18'"),
  info: "<svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' aria-hidden='true'><circle cx='12' cy='12' r='9'/><path d='M12 11v5M12 8v.01'/></svg>",
  run: "<span class='ss-spin' aria-hidden='true'></span>",
};

// A build that failed only because the testbench names ports the module no
// longer has (Verilator PINNOTFOUND, Icarus "is not a port of"), all in the
// testbench file.
const PORT_RX = /is not a port of|port .* not found|unknown port|PINNOTFOUND/i;
export function isPortMismatch(diags, tbRel, isTb = (f) => f === tbRel) {
  const errs = (diags || []).filter((d) => d.severity === "error");
  return errs.length > 0 && errs.every((d) => (d.code === "PINNOTFOUND" || PORT_RX.test(d.msg || "")) && isTb(d.file));
}
// The module's inputs a testbench leaves unconnected (Verilator PINMISSING
// "Cell has missing pin: 'b'", Icarus "dangling input port 3 (op) floating"),
// warnings only in testbench files: the build passes and the simulation runs,
// but those inputs float at z. -> port names, in order, each once.
const FLOAT_RX = /missing pin:?\s*'?([A-Za-z_][\w$]*)'?|dangling input port \d+ \(([A-Za-z_][\w$]*)\)/i;
export function floatingPorts(diags, isTb = () => true) {
  const out = [];
  for (const d of diags || []) {
    if (d.severity === "error" || !isTb(d.file)) continue;
    if (d.code !== "PINMISSING" && !FLOAT_RX.test(d.msg || "")) continue;
    const m = (d.msg || "").match(FLOAT_RX);
    const name = m ? (m[1] || m[2]) : "";
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}
function portMismatch(r) {
  if (r.verdict !== "build" || !r.tb) return false;
  const f = projectFile(r.tb);
  return isPortMismatch(state.simDiags, f ? f.relpath : r.tb, (file) => isTestbenchFile(file));
}

// Write the testbench again for the module's current ports; the old file is
// kept next to it as <name>.v.bak.txt (outside the testbench glob).
async function rewriteTestbench(tb) {
  const f = projectFile(tb);
  if (!f || !state.designDir) return;
  const rel = f.relpath;
  const name = rel.split("/").pop().replace(/\.s?v$/i, "");
  const backup = rel + ".bak.txt";
  const { confirmDialog } = await import("../dialog.js");
  const ok = await confirmDialog({
    title: "Rewrite " + rel.split("/").pop() + "?",
    body: "LanEx writes a new testbench for the module's current ports: every input driven, every output watched, " +
      "the clock and reset from the config. Your file is kept as " + backup + ", so nothing you wrote is lost.",
    confirmText: "Rewrite it",
  });
  if (!ok) return;
  let top = null, clock = null;
  try {
    const info = await api.designInfo({ design_dir: state.designDir });
    top = (info.values && info.values.DESIGN_NAME) || info.suggested_top || null;
    const cp = info.values && info.values.CLOCK_PORT;
    clock = Array.isArray(cp) ? cp[0] : cp || null;
  } catch (_e) { /* the server says what's missing */ }
  try {
    await api.fileRename(rel, backup);
  } catch (ex) { note("Couldn't keep a backup of " + rel + ": " + (ex.message || ex) + ". Nothing was changed.", true); return; }
  try {
    // Back in the old testbench's own folder, under its own name.
    const folder = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
    const r = await api.newProjectFile({ design_dir: state.designDir, kind: /\.sv$/i.test(rel) ? "tb_sv" : "tb_v",
      name, top, clock, use: false, folder });
    await loadTree();
    await loadTestbenches();
    await openFile(r.abs, r.rel);
    state.simDiags = [];
    renderProblems();
    applyMarkers();
    note("Rewrote " + rel + " for " + (top || "the top module") + "'s ports. Your old file is " + backup + ". Simulate to run it.");
  } catch (ex) {
    try { await api.fileRename(backup, rel); } catch (_e) { /* leave the backup where it is */ }
    note("Couldn't write the new testbench: " + (ex.message || ex) + ". Your file is back as it was.", true);
  }
}

let _ssTimer = 0;
// The result of the last simulation, in words, with what to do next.
function simStatus(run) {
  const el = document.getElementById("ide-sim-status");
  const badge = document.getElementById("ide-sim-count");
  if (!el) return;
  clearInterval(_ssTimer);
  const r = run || {};
  const tbName = (r.tb || (state.simRun && state.simRun.tb) || "").split("/").pop();
  const engine = state.simEngineLabel || "";
  const secs = !r.ms ? "" : r.ms < 1000 ? r.ms + " ms" : (r.ms < 10000 ? (r.ms / 1000).toFixed(1) : Math.round(r.ms / 1000)) + " s";
  // Icarus reports "(1s)" when no file sets a `timescale: those are plain
  // simulation time units, not seconds.
  const noScale = r.finish && r.finish.tick && r.finish.tick.unit === "s" && r.finish.tick.mult === 1;
  const jf = r.judged && r.judged.finish;
  const ended = r.finish && r.finish.ticks !== null && r.finish.ticks !== undefined
    ? (noScale ? "$finish at time " + r.finish.ticks + " (no `timescale, so plain time units)" : "$finish at " + formatTime(r.finish.ticks, r.finish.tick))
    : jf && timeWords(jf) ? "$finish at " + timeWords(jf)
    : r.finish || jf ? "$finish reached" : "";
  const facts = [ended, engine, secs].filter(Boolean).join(" · ");
  const embedded = !!document.querySelector('.side-tab[data-tab="setup"]');
  const next = embedded ? "<button class='btn btn-ghost btn-sm' data-ss='flow'>Next: run the flow</button>" : "";
  const wave = r.vcd ? "<button class='btn btn-sm' data-ss='wave'>Show the waveform</button>" : "";
  const V = {
    running: ["run", "Simulating " + (tbName || "the testbench") + "…", engine ? engine + " · building, then running" : "Starting the simulator", ""],
    passed: ["ok", "Passed", "The testbench printed PASS and ran to the end. " + facts, wave + next],
    ran: ["ok", "Ran to the end", facts + (facts ? ". " : "") + "Check the waveform and the output against what you expect.", wave + next],
    "reported-fail": ["fail", "Failed: the testbench reported " + plural(r.fails || 1, "failure"),
      (r.judged && r.judged.line ? r.judged.line.trim().slice(0, 220) + (facts ? " · " : "") : "") + facts,
      (r.vcd && r.judged && r.judged.fail_time ? "<button class='btn btn-primary btn-sm' data-ss='wave-fail'>Waveform at " + fmt.escape(timeWords(r.judged.fail_time) || "the failure") + "</button>" : "") +
      "<button class='btn btn-sm' data-ss='first-fail'>Show the first one</button>" + (r.vcd && !(r.judged && r.judged.fail_time) ? wave : "")],
    stop: ["warn", "Ended at $stop", "It ended at $stop, which isn't a pass. Use $finish when the test is done. " + facts, wave],
    build: portMismatch(r)
      ? ["fail", "Didn't build: the testbench doesn't match the module's ports",
        (tbName || "The testbench") + " connects ports the module doesn't have, or leaves some out (" + plural(r.buildErrors || 0, "error") + " in Problems). Fix the names, or LanEx can write it again for the current ports and keep yours as a backup.",
        "<button class='btn btn-primary btn-sm' data-ss='rewrite-tb'>Rewrite the testbench for these ports</button><button class='btn btn-ghost btn-sm' data-ss='first-error'>Show the first error</button>"]
      : ["fail", "Didn't build: " + plural(r.buildErrors || 0, "error"), "They're listed in Problems and underlined in the editor.", "<button class='btn btn-sm' data-ss='first-error'>Show the first error</button>"],
    failed: ["fail", "The simulator stopped with an error", "Exit code " + r.rc + ". The output below says why." + (secs ? " " + secs : ""), ""],
    "timeout-partial": ["warn", "Stopped at the time limit", "No $finish within " + limitText(r.limit || simTimeLimit()) + ", so the waveform is partial. Add $finish at the end of the testbench's initial block, or raise the limit under Run settings.", wave + "<button class='btn btn-ghost btn-sm' data-ss='open-tb'>Open the testbench</button>"],
    timeout: ["fail", "Stopped at the time limit, no waveform", "No $finish within " + limitText(r.limit || simTimeLimit()) + ". Add $finish at the end of the testbench's initial block, and $dumpfile/$dumpvars to write a waveform.", "<button class='btn btn-ghost btn-sm' data-ss='open-tb'>Open the testbench</button>"],
    "none-saved": ["warn", "Not simulated", (r.error || "A file") + " changed on disk and your edits aren't saved. Save it to choose which version to keep.", ""],
    nowave: ["warn", "Ran, but wrote no waveform", "The testbench needs $dumpfile and $dumpvars. " + facts, "<button class='btn btn-sm' data-ss='add-dump'>Add them to " + fmt.escape(tbName) + "</button>"],
    stopped: ["info", "Stopped", "You stopped the simulation.", ""],
    refused: ["fail", "Couldn't start the simulation", fmt.escape(r.error || ""), ""],
    none: ["info", "No testbench yet", "A testbench drives your design with inputs and shows what comes out.", "<button class='btn btn-primary btn-sm' data-ss='new-tb'>Write a testbench</button>"],
  }[r.verdict] || null;
  if (V && r.floating && r.floating.length) {
    const ports = r.floating.slice(0, 4).join(", ") + (r.floating.length > 4 ? "…" : "");
    V[0] = "warn";
    V[1] = "The testbench doesn't drive " + (r.floating.length === 1 ? "port " : "ports ") + ports + " (" + (r.floating.length === 1 ? "it floats" : "they float") + " at z)";
    V[2] = (r.verdict === "passed" ? "It printed PASS, but those inputs were never set, so the result doesn't test them. " : "It ran to the end, but those inputs were never set. ") +
      "The module's ports changed since " + (tbName || "the testbench") + " was written. LanEx can write it again for the current ports and keep yours as a backup." + (facts ? " " + facts : "");
    V[3] = "<button class='btn btn-primary btn-sm' data-ss='rewrite-tb'>Rewrite the testbench for these ports</button>" + wave;
  }
  if (!V) { el.hidden = true; return; }
  el.hidden = false;
  el.className = "ide-sim-status ss-" + V[0];
  el.innerHTML = "<span class='ss-ic'>" + SS_ICON[V[0]] + "</span><div class='ss-text'><b>" + fmt.escape(V[1]) + "</b><span>" +
    fmt.escape(V[2]).replace(/\$finish|\$dumpfile|\$dumpvars/g, (m) => "<code>" + m + "</code>") + "</span></div><div class='ss-acts'>" + V[3] + "</div>";
  if (badge) {
    const b = { build: ["build failed", "pcount-err"], failed: ["failed", "pcount-err"], timeout: ["timed out", "pcount-err"],
      "timeout-partial": ["partial", "pcount-warn"], "reported-fail": [plural(r.fails || 0, "fail"), "pcount-warn"],
      nowave: ["no wave", "pcount-warn"], running: ["running", ""], stop: ["$stop", "pcount-warn"], passed: ["pass", ""] }[r.verdict] ||
      (r.floating && r.floating.length ? ["inputs float", "pcount-warn"] : null);
    badge.textContent = b ? b[0] : "";
    badge.className = "pcount" + (b && b[1] ? " " + b[1] : "");
  }
  if (r.verdict === "running") {
    const t0 = (state.simRun && state.simRun.started) || Date.now();
    _ssTimer = setInterval(() => {
      const sp = el.querySelector(".ss-text span");
      if (!state.simRunning || !sp) { clearInterval(_ssTimer); return; }
      sp.textContent = (state.simEngineLabel ? state.simEngineLabel + " · " : "") + Math.round((Date.now() - t0) / 1000) + " s";
    }, 1000);
  }
  if (!el._wired) {
    el._wired = true;
    el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-ss]");
      if (!b) return;
      const act = b.dataset.ss;
      const run2 = state.simRun || {};
      if (act === "wave") setPanel("wave");
      else if (act === "wave-fail") {
        setPanel("wave");
        if (state.wave && state.wave.vcd && run2.judged) {
          const t = failTicks(run2.judged.fail_time, state.wave.vcd);
          if (t !== null) state.wave.showTime(t, { fail: true });
        }
      } else if (act === "flow") {
        import("../nav.js").then((m) => m.go("setup", { section: "setup-run" }));
      } else if (act === "first-error") {
        const d = (state.simDiags || []).find((x) => x.severity === "error");
        const f = d && projectFile(d.file);
        if (f) { await openFile(f.abspath, f.relpath); window.ideEditor.gotoLine(d.line, d.col > 1 ? d.col : undefined); }
      } else if (act === "first-fail") {
        // The first failing line of the latest run (runs are separated by a header line).
        const all = [...document.querySelectorAll("#ide-log .ll")];
        const start = all.findLastIndex((x) => x.classList.contains("ll-head"));
        const hit = all.slice(Math.max(0, start)).find((x) => x.classList.contains("ll-err"));
        if (hit) { hit.scrollIntoView({ block: "center" }); hit.classList.add("ll-flash"); setTimeout(() => hit.classList.remove("ll-flash"), 1600); }
      } else if (act === "rewrite-tb") {
        await rewriteTestbench(run2.tb);
      } else if (act === "open-tb" || act === "add-dump") {
        const f = projectFile(run2.tb);
        if (!f) return;
        await openFile(f.abspath, f.relpath);
        if (act === "add-dump") addDumpBlock(run2.top || f.relpath.split("/").pop().replace(/\.s?v$/i, ""));
      } else if (act === "new-tb") newFile("tb_v");
    });
  }
}

// Put the waveform block into the open testbench, just before its endmodule.
function addDumpBlock(tbModule) {
  const ed = window.ideEditor;
  const text = ed.getValue();
  const at = text.lastIndexOf("endmodule");
  if (at < 0) { note("Couldn't find endmodule in this file. Add $dumpfile/$dumpvars inside the testbench module.", true); return; }
  const block = "    // Write every signal to dump.vcd for the waveform viewer.\n    initial begin\n        $dumpfile(\"dump.vcd\");\n        $dumpvars(0, " + tbModule + ");\n    end\n\n";
  const line = text.slice(0, at).split("\n").length;
  ed.gotoLine(line, 1);
  ed.insert(block);
  note("Added a $dumpfile/$dumpvars block. Save, then Simulate again.");
}

// A simulation keeps at most this many output lines for its verdict and its
// Problems; a bench that prints millions would otherwise fill the page's memory.
const SIM_LINES_MAX = 50000;

function onEvent(ev) {
  if (!ev || !ev.type) return;
  if (ev.type === "log" && ev.message) {
    // Only the simulation's own output belongs in its log: never a syntax
    // check's or a flow run's that happens at the same time. (A server from
    // before the source tag sends none; then only the lint can be told apart.)
    const src = ev.source || (ev.payload && ev.payload.source) || "";
    if (src === "lint" && state.linting) { (state.lintLines || (state.lintLines = [])).push(ev.message); return; }
    const mine = src ? src === "sim" : !state.linting;
    const run = state.simRun;
    if (mine && run && !run.done && !run.orphan && state.simRunning) {
      if (run.lines.length < SIM_LINES_MAX) run.lines.push(ev.message);
      appendLog(ev.message);
    }
  }
  else if (ev.type === "lint_done") {                  // standalone lint finished
    if (!state.linting) return;                        // someone else's (another window's) check
    clearTimeout(_lintSafety);
    setLintBusy(false);
    if (ev.timed_out) {
      note("Lint exceeded the time limit and was stopped. If running on WSL with Windows Verilator on PATH, "
           + "install the Linux binary from Tools.", true);
    }
    applyLint(ev.diagnostics, ev);
  }
  else if (ev.type === "sim_done") finishSim(ev);
}

// What the IDE last found, per project, for the Setup page's "Write and
// simulate" step.
function recordRtlStatus() {
  if (!state.designDir) return;
  const key = "ll.rtl:" + state.designDir;
  let prev = {};
  try { prev = JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch (_e) {}
  const real = allDiags().filter((d) => !isNoise(d));
  const st = {
    lint: state.lintChecked ? { errors: real.filter((d) => d.severity === "error").length,
      warnings: real.filter((d) => d.severity === "warning").length, at: state.lintAt,
      // What the flow's own lint (design files only, never testbenches) will hit.
      design_errors: real.filter((d) => d.severity === "error" && inFlow(d)).length } : prev.lint,
    sim: state.simRun && state.simRun.verdict && state.simRun.verdict !== "running"
      ? { tb: state.simRun.tb, verdict: state.simRun.verdict, at: Date.now() } : prev.sim,
  };
  try { localStorage.setItem(key, JSON.stringify(st)); } catch (_e) {}
  document.dispatchEvent(new CustomEvent("g:rtl_status", { detail: { designDir: state.designDir, ...st } }));
}

// Open the last simulation's dump in the desktop GTKWave (server pre-generates a
// .gtkw save file so the signals are on screen — see /api/ide/open-wave). When
// GTKWave isn't installed, the first click explains and arms the button; the
// second click starts the install through the normal Tools machinery (progress
// streams to the Install logs / log drawer).
async function openInGtkwave(signals) {
  const path = state.lastWavePath;
  if (!path) { note("Run a simulation first. No waveform available to open.", true); return; }
  try {
    const r = await api.openWave(path, Array.isArray(signals) ? signals : []);
    if (r && r.ok) {
      state.gtkwaveInstallArmed = false;
      note("GTKWave launched on " + path + (r.signals ? " (" + r.signals + " signals preloaded)." : "."));
      return;
    }
    if (r && r.need === "gtkwave") {
      if (state.gtkwaveInstallArmed) {
        state.gtkwaveInstallArmed = false;
        note("Installing GTKWave. Monitor the install logs, then click GTKWave again when complete.");
        const ir = await api.installTool("gtkwave");
        if (ir && ir.in_progress) note("GTKWave installation in progress. Monitor the install logs for updates.");
        else if (ir && ir.ok === false) note(ir.guidance || ir.reason || "Could not install GTKWave automatically. See the Tools tab for manual setup.", true);
      } else {
        state.gtkwaveInstallArmed = true;
        note((r.error || "GTKWave is not installed.") + " Click GTKWave again to install it now.", true);
      }
      return;
    }
    note((r && r.error) || "Could not open GTKWave.", true);
  } catch (ex) {
    note("Could not open GTKWave: " + (ex.message || ex), true);
  }
}

// Show/hide the durable "partial waveform" badge on the Waveform tab. Kept
// separate from the transient note() so the disclosure survives later toasts and
// is visible in a screenshot of the pane (N2).
// The same badge says when the waveform on screen is an earlier run's (the
// latest one didn't build or wrote no dump), so it isn't read as the new result.
const WAVE_BADGE = {
  partial: ["partial", "The simulation hit its time limit before $finish, so this waveform is incomplete. Add $finish to the testbench to capture the whole run."],
  old: ["earlier run", "The last simulation produced no waveform, so this is the one before it."],
};
function setWavePartial(kind) {
  const badge = document.getElementById("ide-wave-partial");
  if (!badge) return;
  const b = WAVE_BADGE[kind === true ? "partial" : kind];
  badge.hidden = !b;
  if (b) { badge.textContent = b[0]; badge.title = b[1]; badge.classList.toggle("is-old", kind === "old"); }
}

// A dump this big would stall the page while it parses; GTKWave reads it fine.
const WAVE_MAX_BYTES = 200 * 1024 * 1024;

async function loadWaveform(vcdRel, partial) {
  state.lastWavePath = vcdRel;
  const dir = state.designDir;
  if (/\.fst$/i.test(vcdRel)) {
    setPanel("wave");
    if (state.wave) state.wave.showFst(vcdRel);
    note(vcdRel + " is an FST dump; GTKWave opens it.", true);
    return;
  }
  note("Loading waveform " + vcdRel + "…");
  try {
    const r = await fetch(api.waveformUrl(vcdRel), { headers: { "X-Requested-With": "XMLHttpRequest" } });
    if (!r.ok) throw new Error("the server answered " + r.status + " for " + vcdRel);
    if (state.designDir !== dir) return;          // another project was opened meanwhile
    const size = +(r.headers.get("Content-Length") || 0);
    if (size > WAVE_MAX_BYTES) {
      setPanel("wave");
      if (state.wave) state.wave.showTooBig(Math.round(size / 1048576));
      note("The dump is " + Math.round(size / 1048576) + " MB, too big to show here. GTKWave opens it.", true);
      return;
    }
    // Parsed in a worker when big (vcd.js readVCD), with progress in the note.
    const vcd = await readVCD(r, { onProgress: (phase, p) => {
      if (state.designDir === dir) note((phase === "parse" ? "Reading waveform " : "Loading waveform ") + vcdRel + "… " + Math.round(p * 100) + "%");
    } });
    if (state.designDir !== dir) return;
    setPanel("wave");
    growPanelFor("wave");
    if (state.wave) state.wave.load(vcd);
    // A full (non-partial) load clears any prior badge; a partial load pins it.
    setWavePartial(!!partial);
    // Count what the panel lists (its lanes, of the dump's variables), not the dump's raw ids.
    const shown = state.wave ? state.wave.rows.length : vcd.signals.length;
    const all = state.wave ? state.wave.vars.length : vcd.signals.length;
    note("Waveform: " + (shown === all ? shown + (shown === 1 ? " signal" : " signals") : shown + " of " + all + " signals shown") +
      " over " + formatTime(vcd.end, vcd.tick) + (plainTicks(vcd.tick) ? " time units (no `timescale)" : "") + ".");
  } catch (ex) {
    note("Could not load waveform: " + (ex.message || ex), true);
  }
}

// ------------------------------------------------------------------ code → waveform

// Which module each scope of the dump is an instance of, from the project's
// own instantiations (fifo_tb → dut is a fifo). Built once per waveform.
async function scopeModules() {
  const w = state.wave;
  if (!w || !w.vcd) return null;
  if (state.scopeMap && state.scopeMap.vcd === w.vcd) return state.scopeMap;
  const defs = await indexDefs();
  const map = new Map();
  const walk = (node, path, mod) => {
    for (const c of node.children) {
      const p = path.concat([c.name]);
      const m = !path.length ? (defs.has(c.name) ? c.name : null)
        : mod ? (childType(defs.get(mod), c.name.replace(/\[\d+\]$/, "")) || mod) : null;
      if (m) map.set(p.join("."), m);
      walk(c, p, m);
    }
  };
  walk(w.vcd.scopes, [], null);
  state.scopeMap = { vcd: w.vcd, map, defs };
  return state.scopeMap;
}

// Hovering a name in the code shows its value in the loaded simulation at the
// waveform's cursor (every instance of this file's module), and puts it on
// the waveform with one click, the way Verdi and SimVision annotate source.
async function valueHover(word, relPath) {
  const w = state.wave;
  if (!w || !w.vcd || !relPath) return null;
  const sm = await scopeModules();
  if (!sm) return null;
  const mods = new Set([...sm.defs.entries()].filter(([, d]) => d.rel === relPath).map(([n]) => n));
  if (!mods.size) return null;
  const hits = w.vars.filter((v) => (v.base || v.name) === word && mods.has(sm.map.get((v.scope || []).join("."))));
  if (!hits.length) return null;
  const t = w.cursor === null || w.cursor === undefined ? null : w.cursor;
  const at = t === null ? w.vcd.end : t;
  const dom = document.createElement("div");
  dom.className = "cm-wavetip";
  const head = document.createElement("div");
  head.className = "cm-wavetip-h";
  head.textContent = t === null ? "End of the simulation, " + formatTime(Math.round(at), w.vcd.tick)
    : "At the waveform cursor, " + formatTime(Math.round(at), w.vcd.tick);
  dom.appendChild(head);
  for (const v of hits.slice(0, 6)) {
    const row = w.rows.find((r) => r.path === v.path);
    const line = document.createElement("div");
    line.className = "cm-wavetip-row";
    const name = document.createElement("span");
    name.className = "cm-wavetip-n";
    name.textContent = (v.scope || []).slice(1).concat([v.name]).join(".");
    const val = document.createElement("b");
    val.textContent = w._fmt({ path: v.path, radix: row ? row.radix : null }, valueAt(w.vcd.byId[v.id], at));
    const btn = document.createElement("button");
    btn.className = "cm-wavetip-b";
    btn.textContent = row ? "Show" : "Add to waveform";
    btn.addEventListener("mousedown", (e) => {
      e.preventDefault();
      w.addSignal(v.path);
      setPanel("wave");
    });
    line.append(name, val, btn);
    dom.appendChild(line);
  }
  return dom;
}

// Double-click a signal in the waveform: open the file that declares it, on
// the declaration. The scope path in the dump names instances (tb → dut), so
// walk the instantiations down from the testbench to find the module.
async function gotoDeclaration(v) {
  if (!v) return;
  const scope = v.scope || [];
  const name = (v.base || v.name || "").replace(/\[.*$/, "");
  const defs = await indexDefs();
  let mod = defs.has(scope[0]) ? scope[0] : null;
  for (const inst of scope.slice(1)) {
    if (!mod) break;
    // Named generate blocks and begin/end scopes stay in the same module.
    mod = childType(defs.get(mod), inst.replace(/\[\d+\]$/, "")) || mod;
  }
  const def = mod && defs.get(mod);
  if (!def) { note("Couldn't find the module that declares " + v.path + " in this project's files.", true); return; }
  // The declaration from the index: the module's own symbol of that name.
  let hit = null;
  try {
    const syms = (await api.ideSymbols({ rel: def.rel, dirty: dirtyTexts() })).symbols || [];
    hit = syms.find((s) => s.name === name && s.unit === mod && !["instance", "block"].includes(s.kind)) ||
      syms.find((s) => s.name === name && s.unit === mod);
  } catch (_e) { hit = null; }
  pushHere();
  if (hit) await openAt(def.rel, hit.line, hit.col, name.length);
  else await openAt(def.rel, def.line, 1, 0);
  note(hit ? v.path + " is declared in " + def.rel + ", line " + hit.line + "."
    : "Opened module " + mod + " (" + def.rel + "); " + name + " isn't declared there by name.", !hit);
}

// module name -> { rel, line, children: Map(inst -> type) }, from the index.
async function indexDefs() {
  let mods = {};
  try { mods = (await api.ideHierarchy({ dirty: dirtyTexts() })).modules || {}; } catch (_e) { mods = {}; }
  return new Map(Object.entries(mods).map(([n, m]) => [n, { rel: m.rel, line: m.line,
    children: new Map((m.children || []).map((c) => [c.inst, c.type])) }]));
}
function childType(def, inst) { return def && def.children ? def.children.get(inst) || null : null; }

// The simulation's output, a line at a time: errors red, warnings amber, the
// tools' own chatter dimmed, and every file:line a link into the editor.
const LOG_MAX = 5000;
const LOC_RX = /((?:[\w.\-]+\/)*[\w.\-]+\.(?:s?v|vh|svh)):(\d+)(?::(\d+))?/g;
function classifyLine(s) {
  if (/^── /.test(s)) return "ll-head";
  if (/^%Error|\berror\b[:\s]|\bERROR\b|\bFATAL\b|\bFAIL(ED|URE)?\b|syntax error|I give up|MISMATCH/i.test(s)) return "ll-err";
  if (/^%Warning|\bwarning\b[:\s]|\bWARN(ING)?\b/i.test(s)) return "ll-warn";
  if (TOOLISH_RX.test(s)) return "ll-dim";
  if (/\$finish|\bPASS(ED)?\b/i.test(s)) return "ll-ok";
  return "";
}
function appendLog(line, cls) {
  const el = document.getElementById("ide-log");
  if (!el) return;
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  const div = document.createElement("div");
  div.className = "ll " + (cls || classifyLine(line));
  div.innerHTML = fmt.escape(line).replace(LOC_RX, (m, file, ln, col) =>
    projectFile(file) ? "<button class='ll-loc' data-file='" + file + "' data-line='" + ln + "'" + (col ? " data-col='" + col + "'" : "") + ">" + m + "</button>" : m);
  el.appendChild(div);
  while (el.childElementCount > LOG_MAX) el.firstElementChild.remove();
  if (stick) el.scrollTop = el.scrollHeight;
}

function wireLog() {
  const el = document.getElementById("ide-log");
  if (!el) return;
  el.addEventListener("click", async (e) => {
    const b = e.target.closest(".ll-loc");
    if (!b) return;
    const f = projectFile(b.dataset.file);
    if (!f) return;
    await openFile(f.abspath, f.relpath);
    window.ideEditor.gotoLine(+b.dataset.line, b.dataset.col ? +b.dataset.col : undefined);
  });
  byId("ide-log-clear", (b) => b.addEventListener("click", () => { el.innerHTML = ""; }));
}

function note(msg, bad) {
  const el = document.getElementById("ide-note");
  if (!el) return;
  el.textContent = msg;
  el.title = msg;
  el.className = "ide-note" + (bad ? " ide-note-bad" : "");
}

// Standalone /ide page bootstrap. The embedded tab calls initIde() itself, so
// only auto-boot when this is the dedicated IDE document.
if (document.body && document.body.classList.contains("ide-body")) {
  window.addEventListener("DOMContentLoaded", () => initIde());
}
