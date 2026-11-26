// palette.js — command palette (Ctrl K): search everything (spec 5.8).
//
//   >  actions (default)   !  projects   @  runs   :  flow steps
//   #  LibreLane settings  /  project files        ?  help and glossary
//   Verify checks, tools and PDKs by name.
//
// Each row says where it goes (the tab) and its key if it has one. Fuzzy,
// recents first (palettemodel.js holds the ranking, tested). A run row opens
// its actions with → or Tab (Layout, Verify, Copy command, Export, Delete…);
// Backspace on an empty box or Esc goes back. ARIA combobox: the input owns
// the listbox and points at the active row with aria-activedescendant, so
// screen readers follow the arrow keys.

import { go, recentPlaces } from "./nav.js";
import { isDown, probeNow } from "./connection.js";
import { toggleTheme } from "./theme.js";
import { toggleDensity } from "./density.js";
import { toggleRunMode } from "./runmode.js";
import { icon } from "./icons.js";
import { api } from "./api.js";
import { openLayer, closeLayer } from "./overlay.js";
import { state, safeStorage } from "./state.js";
import { contextRun, contextRuns, contextBaseline, contextDesign, setRun, loadRuns } from "./context.js";
import { parseQuery, rank, oneLine, pushRecent, PREFIXES, whenLocal, keepSelection } from "./palettemodel.js";
import { help as pageHelp } from "./help.js";
import { escapeHtml } from "./html.js";

const RECENT_KEY = "ll.paletteRecent";
let _el = null;
let _input = null;
let _list = null;
let _items = [];
let _filtered = [];
let _sel = 0;
let _glossary = null;
let _files = null;      // [{ rel, role }] for the open project
let _filesFor = "";
let _projects = null;   // every project LanEx knows (shell.knownProjects)
let _steps = null;      // the flow's steps [{id, label}]
let _stepLabel = null;  // flow.js stepLabel, once loaded
let _loading = false;   // data still arriving: "Searching…", not "Nothing matches"
let _page = null;       // a run's actions: { title, items, back: the query before }

const esc = escapeHtml;
const click = (id) => document.getElementById(id)?.click();
const tab = (name, params = {}) => go(name, params, { link: false });
const guide = (action) => document.dispatchEvent(new CustomEvent("g:guide", { detail: { action } }));
const consoleTab = (pane) => import("./console.js").then((m) => m.openConsole(pane));

// The rail's number key for a tab ("3"), read from the rail so it can't drift.
function railKey(t) {
  const tabs = Array.from(document.querySelectorAll(".side-tabs .side-tab")).slice(0, 10).map((b) => b.dataset.tab);
  const i = tabs.indexOf(t);
  return i < 0 ? "" : "1234567890"[i];
}

// ---------------------------------------------------------------- actions
function A(id, title, where, ico, run, kbd = "") {
  return { id: "a:" + id, group: "action", title, where, icon: icon(ico), kbd, run };
}
function actions() {
  const goTo = (t, label, ico) => A("go-" + t, "Go to " + label, label, ico, () => tab(t), railKey(t));
  const list = [
    goTo("setup", "Setup", "folder"),
    goTo("ide", "RTL IDE", "code"),
    goTo("cells", "Cells", "layers"),
    goTo("pipeline", "Flow", "flow"),
    goTo("runs", "Runs", "clock"),
    goTo("layout", "Layout", "grid"),
    goTo("analytics", "Analytics", "chart"),
    goTo("verify", "Verify", "check"),
    goTo("tools", "Tools", "tools"),
    goTo("settings", "Settings", "gear"),
    A("run", "Run the flow", "Run", "play", () => click("btn-run-global"), "Ctrl Enter"),
    A("stop", "Stop the run", "Run", "stop", () => click("btn-cancel")),
    A("resume", "Resume / next step", "Run", "play", () => click("btn-resume")),
    A("preflight", "Check my setup (preflight)", "Setup", "check", () => click("btn-preflight")),
    A("runmode", "Switch run engine (Container / Local tools)", "Run menu", "refresh", () => toggleRunMode()),
    A("sweep", "Sweep settings (design-space exploration)", "Runs", "beaker", () => tab("dse")),
    A("console", "Open the Console", "Console", "command", () => consoleTab(), "Ctrl `"),
    A("console-cmd", "Show the command the next run will execute", "Console › Command", "command", () => consoleTab("command")),
    A("console-run", "Run a command (librelane, openroad, yosys…)", "Console › Run a command", "command", () => consoleTab("run")),
    A("console-journal", "What LanEx ran this session (Journal, copy as script)", "Console › Journal", "file", () => consoleTab("journal")),
    A("console-log", "Show the flow log", "Console › Log", "file", () => consoleTab("log")),
    A("spm", "New project from the SPM example", "Project", "plus", () => import("./wizard.js").then((m) => m.openWizard({ template: "spm" }))),
    A("new-project", "New project from a template", "Project", "plus", () => click("btn-new-project")),
    A("open-project", "Open a project folder", "Project", "folder", () => click("btn-open-project")),
    A("reveal-project", "Show project in file manager", "Project", "popout", () => click("btn-reveal-project")),
    A("new-file", "New file in the RTL IDE", "RTL IDE", "plus", () => tab("ide", { newFile: "src/new.v" })),
    A("all-settings", "All LibreLane settings", "Setup › Settings", "grid", () => tab("setup", { section: "cfg-card" })),
    A("theme", "Toggle dark / light theme", "View", "moon", () => toggleTheme(), "t"),
    A("density", "Compact spacing on or off", "View", "grid", () => toggleDensity(), "d"),
    A("logs", "Show install and tool logs", "Status line › Logs", "file", () => click("logs-btn")),
    A("import-run", "Import a run… (a run folder or a bundle .zip)", "Runs", "plus",
      () => import("./runs.js").then((m) => m.importRunFlow())),
    // Reloading while LanEx is away lands on the browser's error page: check
    // the connection instead (connection.js).
    A("reload", "Reload LanEx", "View", "refresh", () => (isDown() ? probeNow() : location.reload())),
    A("tour", "Take the tour", "Help", "bulb", () => guide("tour")),
    A("first-chip", "First-chip guide", "Help", "bulb", () => guide("first-chip")),
    A("reset-hints", "Reset hints", "Help", "refresh", () => guide("reset")),
    A("glossary", "Glossary and help for this page", "Help", "book", () => import("./shell.js").then((m) => m.openHelp("learn"))),
    A("shortcuts", "Keyboard shortcuts", "Help", "command", () => import("./shell.js").then((m) => m.openHelp("keys")), "?"),
    A("license", "License and notices", "Help", "book", () => import("./shell.js").then((m) => m.openHelp("about"))),
    A("docs", "Open LibreLane docs", "Web", "arrowUp", () => window.open("https://librelane.readthedocs.io/", "_blank", "noopener")),
  ];
  // The context run: what most people want next with it.
  const run = contextRun();
  if (run) {
    const base = contextBaseline();
    if (base) list.push(A("ctx-compare", "Compare " + run + " with " + base, "Analytics", "diff", () => tab("analytics", { section: "analytics-compare" })));
    list.push(A("ctx-gds", "Open " + run + " in Layout", "Layout", "grid", () => tab("layout")));
    list.push(A("ctx-read", "How to read a chip (a five-step lesson on " + run + ")", "Layout", "bulb",
      () => { tab("layout"); import("./layout.js").then((m) => m.startReadingLesson()); }));
    list.push(A("ctx-cmd", "Copy the command of " + run, "Console › Command", "command", () => copyRunCommand(run)));
    list.push(A("ctx-verify", "Sign-off checks of " + run + " (DRC, LVS)", "Verify", "check", () => tab("verify")));
  }
  // Not connected: checking again is the first thing to offer.
  if (isDown()) list.unshift(A("reconnect", "Reconnect to LanEx now", "Connection", "refresh", () => probeNow()));
  return list;
}

// ---------------------------------------------------------------- projects, steps, checks, tools
const baseOf = (p) => String(p || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;

function projects() {
  return (_projects || []).map((d) => ({
    id: "pj:" + d, group: "project", title: baseOf(d), mono: true, where: "Project", icon: icon("folder"),
    value: d === state.designDir ? "open now" : "", sub: d, keys: [d],
    run: () => import("./shell.js").then((m) => m.switchProject(d)),
  }));
}

function steps() {
  const label = (id) => (_stepLabel ? _stepLabel(id) : id);
  const stages = (FS_STAGES || []).map((s) => ({
    id: "st:" + s.id, group: "step", title: s.label + " (stage)", where: "Flow", icon: icon("flow"),
    sub: oneLine(s.blurb || ""), keys: [s.id, s.blurb || ""],
    run: () => tab("pipeline", { stage: s.id }),
  }));
  return stages.concat((_steps || []).map((s) => ({
    id: "sp:" + s.id, group: "step", title: label(s.id), where: "Flow", icon: icon("flow"),
    value: s.id, sub: oneLine(s.summary || ""), keys: [s.id, s.summary || ""],
    run: () => tab("pipeline", { step: s.id }),
  })));
}
let FS_STAGES = null;

// Verify's checks, in its own order and words (verify-model.js).
const CHECK_NAMES = {
  setup: "Setup timing", hold: "Hold timing", slew: "Max slew", cap: "Max capacitance", fanout: "Max fanout",
  magic_drc: "Magic DRC", klayout_drc: "KLayout DRC", route_drc: "Routing DRC", overlap: "Illegal overlap",
  klayout_density: "Metal density", lvs: "LVS", xor: "Mask XOR", disc_pin: "Disconnected pins",
  antenna: "Antenna", pdn: "Power grid connections", klayout_antenna: "KLayout antenna",
  lint_errors: "Lint errors", lint_warnings: "Lint warnings", lint_timing_constructs: "Timing constructs in RTL",
  unmapped: "Unmapped cells", synth_check: "Synthesis checks", assign: "Assign statements", latch: "Latches",
  eqy: "Equivalence (eqy)", wire_length: "Wire length",
};
let _short = null;       // verify-model.js SHORT, once loaded
function checks() {
  return Object.entries(CHECK_NAMES).map(([id, name]) => ({
    id: "ck:" + id, group: "check", title: name + " check", where: "Verify", icon: icon("check"),
    sub: (_short && _short[id]) || "", keys: [id, (_short && _short[id]) || "", "sign-off check verify"],
    run: () => tab("verify", { check: id }),
  }));
}

// Tools and PDKs: the Tools page's rows (tools.js revealItem finds tl-<key>,
// tl-pdk-<key>, tl-viewer-<key>, tl-helper-<key>, tl-local-<key>).
const TOOLS = [
  ["engine", "Container engine (Docker or Podman)"], ["image", "LibreLane image"], ["ciel", "ciel, the PDK manager"],
  ["klayout", "KLayout"], ["magic", "Magic"], ["openroad", "OpenROAD GUI"], ["gds3d", "GDS3D"],
  ["iverilog", "Icarus Verilog"], ["gtkwave", "GTKWave"], ["verilator", "Verilator"],
];
function tools() {
  const t = TOOLS.map(([k, name]) => ({
    id: "tl:" + k, group: "tool", title: name, where: "Tools", icon: icon("tools"), keys: [k, "tool install"],
    run: () => tab("tools", { item: k }),
  }));
  const pdks = (state.pdks || []).map((p) => ({
    id: "pk:" + p.name, group: "tool", title: p.name + " PDK", mono: true, where: "Tools", icon: icon("layers"),
    value: p.ready === false ? "not installed" : "installed", keys: [p.name, "pdk process"],
    run: () => tab("tools", { item: p.name }),
  }));
  return t.concat(pdks);
}

// A run's actions (→ or Tab on a run row): the Runs panel's own (runs.js
// runAction), so the words, dialogs and checks are the same. Ones that ask
// first end in "…"; Delete is the last row and red.
function runActions(r) {
  const act = (id, title, where, ico, extra = {}) => ({
    id: "ra:" + id + ":" + r.tag, group: "action", title, where, icon: icon(ico), ...extra,
    run: () => import("./runs.js").then((m) => m.runAction(id, r)),
  });
  return [
    act("flow", "Open " + r.tag + " in Flow", "Flow", "flow"),
    act("layout", "Open in Layout", "Layout", "grid"),
    act("verify", "Its sign-off checks", "Verify", "check"),
    act("analytics", "Its numbers", "Analytics", "chart"),
    act("baseline", "Make it the baseline (or stop)", "Runs", "pin"),
    act("cmd", "Copy its command", "Console", "command"),
    act("export", "Export a report…", "Runs", "file"),
    act("bundle", "Download a bundle…", "Runs", "file"),
    act("files", "Show its files…", "Runs", "folder"),
    act("archive", r.archived ? "Restore it from the archive" : "Archive it", "Runs", "clock"),
    act("delete", "Delete run " + r.tag + "…", "Runs", "trash", { danger: true, sub: "Asks first." }),
  ];
}

function openRunPage(it) {
  if (!it || !it.runRow) return false;
  _page = { title: "RUN " + it.runRow.tag, items: runActions(it.runRow), back: _input.value };
  _input.value = "";
  _sel = 0;
  refresh();
  return true;
}

function closePage() {
  if (!_page) return false;
  const back = _page.back || "";
  _page = null;
  _input.value = back;
  _sel = 0;
  refresh();
  return true;
}

async function copyRunCommand(tag) {
  const { toast } = await import("./toast.js");
  try {
    const r = await api.runReproduce(tag);
    if (!r.command) { toast.show(tag + " has no recorded command (it was not started from LanEx).", "warn"); return; }
    await navigator.clipboard.writeText(r.command);
    toast.show("Copied the command of " + tag, "success", 2000);
  } catch (e) { toast.show("Could not copy: " + e.message, "warn"); }
}

// ---------------------------------------------------------------- other groups
function fmtVal(v) {
  if (v === undefined || v === null || v === "") return "";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > 40 ? s.slice(0, 39) + "…" : s;
}

function settings() {
  const vals = state.varsValues || {};
  return (state.variables || []).map((v) => {
    // Only a value the config sets at its top level is shown: a pdk::/scl::
    // section can override the default, so "the default" could be untrue.
    return {
      id: "s:" + v.name, group: "setting", title: v.name, mono: true,
      where: "Setup › Settings", icon: icon("grid"),
      value: vals[v.name] !== undefined ? "= " + fmtVal(vals[v.name]) : "", valueIsSet: vals[v.name] !== undefined,
      sub: oneLine(v.description || ""), keys: [v.description || ""],
      run: () => tab("setup", { setting: v.name }),
    };
  });
}

function runs() {
  const design = contextDesign();
  return (contextRuns() || []).map((r) => {
    // The run verdict when TRUST provides it, else what the run folder says.
    const st = (r.verdict && (r.verdict.label || r.verdict.state)) ||
      (r.success ? "completed" : r.steps_failed ? "failed" : r.success === false ? "incomplete" : "");
    return {
      id: "r:" + (r._design || design) + ":" + r.tag, group: "run", title: r.tag, mono: true,
      where: "Flow", icon: icon("clock"), value: st, more: true, runRow: { ...r, _design: r._design || design },
      sub: [whenLocal(r.started_at), r._design && r._design !== design ? r._design.split("/").pop() : ""].filter(Boolean).join(" · "),
      keys: [st, ...(r.tags || [])],
      run: async () => { await setRun(r.tag, r._design || design); tab("pipeline"); },
    };
  });
}

function files() {
  return (_files || []).map((f) => ({
    id: "f:" + f.rel, group: "file", title: f.rel, mono: true, where: "RTL IDE", icon: icon("code"),
    value: f.role || "", keys: [f.role || ""],
    run: () => tab("ide", { rel: f.rel }),
  }));
}

// Each page's guide (help.js) is searchable by everything it says, so "?sweep"
// or "?baseline" finds the Runs guide, not only glossary terms. The guides of
// pages that moved (DSE into Runs, Manual into the Console) are left out.
function pageGuides() {
  const strip = (s) => String(s || "").replace(/<[^>]+>/g, "");
  return Object.entries(pageHelp.catalog || {}).filter(([k]) => k !== "dse" && k !== "manual").map(([k, def]) => ({
    id: "g:" + k, group: "help", title: def.title + " guide", where: "Help", icon: icon("book"),
    sub: oneLine(strip(def.goal)),
    keys: [strip(def.goal), ...(def.how || []).map(strip), ...(def.pitfalls || []).map(strip),
      ...(def.samples || []).map((x) => x.key + " " + x.sample)],
    run: () => pageHelp.open(k),
  }));
}

function help() {
  return pageGuides().concat(Object.entries(_glossary || {}).map(([term, text]) => ({
    id: "h:" + term, group: "help", title: term, where: "Glossary", icon: icon("book"),
    sub: oneLine(text), keys: [String(text || "")],
    run: async () => {
      const m = await import("./shell.js");
      m.openHelp("learn");
      // Point the help sheet's glossary search at the term when it has one.
      requestAnimationFrame(() => {
        const q = document.getElementById("gloss-q");
        if (q) { q.value = term; q.dispatchEvent(new Event("input", { bubbles: true })); }
      });
    },
  })));
}

function recentPlaceItems() {
  return recentPlaces().map((r, i) => ({
    id: "p:" + r.key, group: "action", title: r.label, where: "Recent place", icon: icon("clock"), recentPlace: i,
    run: () => go(r.tab, r.params, { link: false }),
  }));
}

// Data the groups need, fetched once per open (files and glossary are cached).
async function loadData() {
  const jobs = [];
  if (!_glossary) jobs.push(fetch("/static/glossary.json", { headers: { "X-Requested-With": "XMLHttpRequest" } })
    .then((r) => r.json()).then((g) => import("./term.js").then((t) => { _glossary = t.flatGlossary(g); })).catch(() => { _glossary = {}; }));
  if (state.designDir && _filesFor !== state.designDir) jobs.push(api.projectFiles()
    .then((r) => { _files = ((r && r.files) || []).filter((f) => !/^runs\//.test(f.rel)); _filesFor = state.designDir; }).catch(() => { _files = []; }));
  if (!(state.variables || []).length) jobs.push(api.variables().then((v) => { if (Array.isArray(v) && !(state.variables || []).length) state.variables = v; }).catch(() => {}));
  if (!(contextRuns() || []).length) jobs.push(loadRuns().catch(() => {}));
  // Projects every open (another window may have added one); steps, stages
  // and check words once.
  jobs.push(import("./shell.js").then((m) => m.knownProjects()).then((d) => { _projects = d || []; }).catch(() => { _projects = _projects || []; }));
  if (!_steps) {
    jobs.push(Promise.resolve(state.steps && state.steps.length ? state.steps : api.steps())
      .then((s) => { _steps = (Array.isArray(s) ? s : (s && s.steps) || []).filter((x) => x && x.id); }).catch(() => { _steps = []; }));
    jobs.push(import("./flow.js").then((m) => { _stepLabel = m.stepLabel; }).catch(() => {}));
    jobs.push(import("./flowstages.js").then((m) => { FS_STAGES = m.STAGES; }).catch(() => {}));
    jobs.push(import("./verify-model.js").then((m) => { _short = m.SHORT; }).catch(() => {}));
  }
  await Promise.all(jobs);
}

function collect() {
  if (_page) return _page.items;
  return recentPlaceItems().concat(actions(), projects(), runs(), steps(), checks(), settings(), tools(), files(), help());
}

// ---------------------------------------------------------------- UI
function build() {
  _el = document.createElement("div");
  _el.className = "cmdk";
  _el.hidden = true;
  _el.innerHTML =
    '<div class="cmdk-backdrop"></div>' +
    '<div class="cmdk-panel" role="dialog" aria-modal="true" aria-label="Search everything">' +
    '<input class="cmdk-input" type="text" role="combobox" aria-expanded="true" aria-controls="cmdk-list" aria-autocomplete="list"' +
      ' aria-label="Search pages, projects, runs, steps, checks, settings, tools, files and help" placeholder="Search pages, projects, runs, steps, checks, settings, tools…" autocomplete="off" spellcheck="false"/>' +
    '<div class="cmdk-scope" aria-live="polite"></div>' +
    '<div class="cmdk-loading" aria-hidden="true" hidden></div>' +
    '<div class="cmdk-list" id="cmdk-list" role="listbox" aria-label="Results"></div>' +
    '<div class="cmdk-foot"><kbd>↑</kbd><kbd>↓</kbd> navigate · <kbd>↵</kbd> open · <kbd>→</kbd> a run\'s actions · <kbd>esc</kbd> close · ' +
      Object.entries(PREFIXES).map(([p, v]) => "<button type='button' class='cmdk-pfx' data-pfx='" + esc(p) + "'><kbd>" + esc(p) + "</kbd> " + esc(v.label.toLowerCase()) + "</button>").join(" ") +
    "</div></div>";
  document.body.appendChild(_el);
  _input = _el.querySelector(".cmdk-input");
  _list = _el.querySelector(".cmdk-list");
  _el.querySelector(".cmdk-backdrop").addEventListener("click", close);
  _input.addEventListener("input", () => { _sel = 0; refresh(); });
  _input.addEventListener("keydown", onKey);
  // Esc in a run's actions goes back a level, before the overlay stack (which
  // listens on document, capturing) closes the whole palette.
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _el && !_el.hidden && _page) { closePage(); e.preventDefault(); e.stopPropagation(); }
  }, true);
  _el.querySelectorAll(".cmdk-pfx").forEach((b) => b.addEventListener("click", () => {
    _input.value = b.dataset.pfx + parseQuery(_input.value).q; _sel = 0; refresh(); _input.focus();
  }));
  _list.addEventListener("click", (e) => {
    const it = e.target.closest(".cmdk-item");
    if (it) { _sel = +it.dataset.i; runSel(); }
  });
  _list.addEventListener("mousemove", (e) => {
    const it = e.target.closest(".cmdk-item");
    if (it && +it.dataset.i !== _sel) { _sel = +it.dataset.i; markSel(); }
  });
}

function onKey(e) {
  if (e.key === "ArrowDown") { _sel = Math.min(_sel + 1, _filtered.length - 1); markSel(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { _sel = Math.max(_sel - 1, 0); markSel(); e.preventDefault(); }
  else if (e.key === "PageDown") { _sel = Math.min(_sel + 8, _filtered.length - 1); markSel(); e.preventDefault(); }
  else if (e.key === "PageUp") { _sel = Math.max(_sel - 8, 0); markSel(); e.preventDefault(); }
  else if (e.key === "Enter") { runSel(); e.preventDefault(); }
  // A run row's actions: → at the end of the text, or Tab.
  else if ((e.key === "Tab" && !e.shiftKey) || (e.key === "ArrowRight" && _input.selectionStart === _input.value.length)) {
    if (!_page && openRunPage(_filtered[_sel])) e.preventDefault();
    else if (e.key === "Tab") e.preventDefault();
  }
  else if (e.key === "Backspace" && !_input.value && _page) { closePage(); e.preventDefault(); }
  else if (e.key === "Escape") { if (!closePage()) close(); e.preventDefault(); e.stopPropagation(); }
}

function recentIds() {
  const places = recentPlaceItems().map((p) => p.id);
  return places.concat(safeStorage.getJSON(RECENT_KEY, []) || []);
}

function refresh({ keep = false } = {}) {
  const raw = _input.value;
  const { group, q } = parseQuery(raw);
  const prevId = keep && _filtered[_sel] ? _filtered[_sel].id : null;
  // A run's actions page lists every action in its order, filtered by what is typed.
  _filtered = _page ? (q ? rank(_page.items, q, { limit: 80 }) : _page.items.slice())
    : rank(_items, raw, { recentIds: recentIds(), limit: q || group ? 80 : 40 });
  if (keep) _sel = keepSelection(_filtered, prevId, _sel);
  const scope = _el.querySelector(".cmdk-scope");
  const label = group ? Object.values(PREFIXES).find((p) => p.group === group).label : "";
  scope.textContent = _page ? _page.title + " ›  Backspace or Esc goes back"
    : group ? "Searching " + label.toLowerCase() + (q ? "" : " (" + _filtered.length + (_filtered.length === 80 ? "+" : "") + ")") : "";
  scope.hidden = !group && !_page;
  scope.classList.toggle("is-page", !!_page);
  const ld = _el.querySelector(".cmdk-loading");
  if (ld) ld.hidden = !_loading;
  paint(q);
}

const GROUP_LABEL = { action: "Action", project: "Project", run: "Run", step: "Flow", check: "Verify", setting: "Setting",
  tool: "Tools", file: "File", help: "Help" };

function paint() {
  if (!_filtered.length) {
    _list.innerHTML = '<div class="cmdk-empty">' + esc(emptyText()) + "</div>";
    _input.removeAttribute("aria-activedescendant");
    return;
  }
  _list.innerHTML = _filtered.map((a, i) =>
    '<div class="cmdk-item' + (i === _sel ? " cmdk-item-sel" : "") + (a.danger ? " cmdk-danger" : "") + '" role="option" id="cmdk-opt-' + i + '" aria-selected="' + (i === _sel) + '" data-i="' + i + '">' +
      '<span class="cmdk-ico">' + a.icon + "</span>" +
      '<span class="cmdk-main"><span class="cmdk-title' + (a.mono ? " is-mono" : "") + '">' + esc(a.title) + "</span>" +
        (a.value ? '<span class="cmdk-val' + (a.valueIsSet ? " is-set" : "") + '">' + esc(a.value) + "</span>" : "") +
        (a.sub ? '<span class="cmdk-sub">' + esc(a.sub) + "</span>" : "") + "</span>" +
      '<span class="cmdk-where">' + esc(a.where || GROUP_LABEL[a.group]) + "</span>" +
      (a.kbd ? '<span class="cmdk-kbd">' + a.kbd.split(" ").map((k) => "<kbd>" + esc(k) + "</kbd>").join("") + "</span>" : "") +
      (a.more ? '<span class="cmdk-chev" title="Its actions: → or Tab" aria-label="Has actions: right arrow or Tab">›</span>' : "") +
    "</div>").join("");
  markSel();
}

function emptyText() {
  const { group, q } = parseQuery(_input.value);
  if (_loading) return "Searching…";
  if (group === "file" && !state.designDir) return "Open a project to search its files.";
  if (group === "run" && !(contextRuns() || []).length) return "No runs yet. Run the flow (Ctrl Enter).";
  if (group === "project" && !(_projects || []).length) return "No projects yet. New project from a template, or open a folder (> new project).";
  if (group === "step" && !(_steps || []).length) return "The flow's steps couldn't load.";
  return "Nothing matches" + (q ? " “" + q + "”" : "") + ". Try ! projects · @ runs · : steps · # settings · / files · ? help.";
}

function markSel() {
  _list.querySelectorAll(".cmdk-item").forEach((el) => {
    const on = +el.dataset.i === _sel;
    el.classList.toggle("cmdk-item-sel", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
  });
  const sel = _list.querySelector(".cmdk-item-sel");
  if (sel) { _input.setAttribute("aria-activedescendant", sel.id); sel.scrollIntoView({ block: "nearest" }); }
}

function runSel() {
  const a = _filtered[_sel];
  if (!a) return;
  // A place visited, a run's own action page and anything destructive are
  // never remembered as a recent pick.
  if (!a.id.startsWith("p:") && !a.id.startsWith("ra:") && !a.danger) {
    safeStorage.set(RECENT_KEY, JSON.stringify(pushRecent(safeStorage.getJSON(RECENT_KEY, []) || [], a.id)));
  }
  _page = null;
  // The palette closes first: a dialog the action opens (Delete asks first)
  // gets the focus, and closing it returns to the page.
  close();
  try { Promise.resolve(a.run()).catch((e) => console.error("palette action failed", e)); }
  catch (e) { console.error("palette action failed", e); }
}

export async function openPalette(initial = "") {
  if (!_el) build();
  _page = null;
  _items = collect();
  _input.value = initial;
  _sel = 0;
  _loading = true;
  refresh();
  _el.hidden = false;
  // A dialog on the overlay stack: Tab stays in the panel, Esc closes it,
  // closing puts focus back.
  openLayer(_el, { kind: "dialog", onClose: () => { _el.hidden = true; _page = null; }, focus: () => _input });
  requestAnimationFrame(() => _input.focus());
  // Projects, runs, steps, settings, files and the glossary arrive a moment
  // later: fold them in, keeping the highlighted row where it is.
  try { await loadData(); } finally { _loading = false; }
  if (_el.hidden) return;
  if (!_page) _items = collect();
  refresh({ keep: true });
}

export function close() {
  if (!_el) return;
  closeLayer(_el);
  _el.hidden = true;
}

export function setupPalette() {
  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (_el && !_el.hidden) close();
      else openPalette();
    }
  });
}
