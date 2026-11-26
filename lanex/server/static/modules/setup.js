// setup.js — design picker, PDK picker, run button, mode switch.

import { api } from "./api.js";
import { state, safeStorage } from "./state.js";
import { fmt } from "./api.js";
import { renderConfig, switchOverridesTo, refreshEffective } from "./config.js";
import { setupFolderBrowser, loadFilesFor } from "./fs.js";
import { toast } from "./toast.js";
import { renderPreflight, freshPreflight, preflightNow, runBlocker } from "./preflight.js";
import { go } from "./nav.js";
import { currentTab } from "./tabs.js";
import { icon } from "./icons.js";
import { wireJump } from "./jumpnav.js";
import { pdkFacts, stackHtml } from "./pdkinfo.js";
import * as FS from "./flowstages.js";
import { ago as MFago } from "./metricfmt.js";
import { askNotifyOnRun } from "./runend.js";
import { runMenuChoices, setupRunMenu, onlyChoice } from "./runmenu.js";
import { followJob } from "./runfollow.js";

const RECENT_KEY = "ll.recentDesigns";

// The run queue's last word on free slots (queue.js keeps it; it loads after
// setup.js, so it is read through this hook and assumed free until then).
let _queueMod = null;
import("./queue.js").then((m) => { _queueMod = m; }).catch(() => {});
function slotFree() { return !_queueMod || _queueMod.slotFree(); }
function queuedAhead() {
  const s = _queueMod && _queueMod.queueSnapshot();
  return s ? (s.waiting || 0) : 0;
}

function getRecentDesigns() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter(Boolean);
  } catch (_e) {
    return [];
  }
}

function rememberDesign(dir) {
  if (!dir) return;
  try {
    let list = getRecentDesigns().filter((d) => d !== dir);
    list.unshift(dir);
    list = list.slice(0, 6);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch (_e) {}
}

// Recent projects live in the top-bar project switcher now; kept as a no-op so
// older callers stay valid.
export function renderRecentDesigns() {}

async function openIde(opts) {
  const { go } = await import("./nav.js");
  go("ide");
  const m = await import("./ide/main.js");
  await m.initIde({ designDir: state.designDir, runMode: state.runMode });
  await m.openInIde(opts || {});
}

function scrollToBlock(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Buttons anywhere on the Setup page that hand off to the project switcher,
// the RTL IDE or the file manager (data-open / data-step-act).
function wireSetupActions() {
  const sec = document.getElementById("sec-setup");
  if (!sec) return;
  sec.addEventListener("click", (e) => {
    const b = e.target.closest("[data-open], [data-step-act]");
    if (!b) return;
    const act = b.dataset.open || b.dataset.stepAct;
    if (act === "new-project" || (act === "project" && !state.designDir)) {
      document.getElementById("btn-new-project")?.click();
    } else if (act === "example") {
      import("./wizard.js").then((m) => m.openWizard({ template: "blinky" }));
    } else if (act === "project") {
      scrollToBlock("setup-design");
    } else if (act === "switch") {
      e.stopPropagation();
      document.getElementById("design-pill")?.click();
    } else if (act === "reveal") {
      document.getElementById("btn-reveal-project")?.click();
    } else if (act === "ide") {
      openIde({});
    } else if (act === "new-file") {
      if (!state.designDir) { toast.show("Open a project first.", "warn"); return; }
      openIde({ create: "src" });
    } else if (act === "edit") {
      openIde({ rel: b.dataset.rel });
    } else if (act === "pdk") {
      scrollToBlock("setup-pdk");
      document.getElementById("pdk-select")?.focus({ preventScroll: true });
    } else if (act === "run") {
      scrollToBlock("setup-run");
    } else if (act === "runs") {
      import("./nav.js").then((m) => m.go("runs"));
    }
  });
  // "Add files…" copies files from anywhere on the computer into the project,
  // so the project folder stays self-contained. The browser's file input opens
  // the operating system's own file dialog.
  const add = document.getElementById("btn-add-files");
  if (add) {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.hidden = true;
    add.after(input);
    add.addEventListener("click", () => {
      if (!state.designDir) { toast.show("Open a project first.", "warn"); return; }
      input.click();
    });
    input.addEventListener("change", async () => {
      const files = [...input.files];
      input.value = "";
      // A file of the same name asks first: Replace or Keep both (addfiles.js).
      const { addProjectFiles } = await import("./addfiles.js");
      const res = await addProjectFiles(files, { report: (msg, bad) => toast.show(msg, bad ? "error" : "info") });
      const ok = res.added.length;
      if (ok) {
        toast.show("Added " + ok + " file" + (ok === 1 ? "" : "s") + " to the project" +
          (res.replaced.length ? " (" + res.replaced.length + " replaced)" : "") + ".", "success");
        await loadFilesFor(state.designDir);
        const base = state.designDir.replace(/\/+$/, "") + "/";
        const design = res.forConfig.filter((rel) => rel.startsWith("src/") && /\.s?v$/i.test(rel)).map((rel) => base + rel);
        if (design.length) {
          const { includeInConfig } = await import("./fs.js");
          includeInConfig(design);
        }
      }
    });
  }
  // Files created in the RTL IDE show up here without a manual rescan.
  document.addEventListener("g:sources_changed", () => {
    if (state.designDir) loadFilesFor(state.designDir);
  });
  // Design files added from the RTL IDE join the config's file list, like
  // the ones added here.
  document.addEventListener("g:sources_added", async (e) => {
    const files = (e.detail && e.detail.files) || [];
    if (!files.length || !state.designDir) return;
    await loadFilesFor(state.designDir);
    const { includeInConfig } = await import("./fs.js");
    includeInConfig(files);
  });
}

export function setupSetup() {
  setupFolderBrowser();
  renderRecentDesigns();
  // Page header links; the Constraints categories have their own tabs inside
  // that card (config.js), so they never crowd this bar. At the bottom of the
  // page the section filling the screen is the current one ("Ready to run?"),
  // not the folded All settings bar under it.
  wireJump(document.getElementById("sec-setup"), { atBottom: "most-visible" });
  wireSetupActions();
  import("./projectcard.js").then((m) => m.wireDesignRow()).catch(() => {});
  import("./mostused.js").then((m) => m.wireSettings()).catch(() => {});
  // A change made elsewhere (the fix sheet's Apply or Undo, Cells) must show
  // here at once: the Design row and the Settings card read the config again.
  // (The Design row's own edits announce themselves without keys: it is
  // already showing them.)
  document.addEventListener("g:config_changed", (e) => {
    const d = (e && e.detail) || {};
    if (!d.keys && !d.from) return;
    if (!state.designDir) return;
    import("./projectcard.js").then((m) => m.paintDesignRow()).catch(() => {});
    import("./mostused.js").then((m) => m.paintSettings()).catch(() => {});
  });
  // The project card's "Last run" line follows a run that just ended.
  const lastRun = () => {
    if (!state.designDir) return;
    summariseFacts(state.designDir);
  };
  document.addEventListener("g:flow_done", () => setTimeout(lastRun, 1500));
  document.addEventListener("g:runs_state", lastRun);
  // The server refused a run, resume or sweep: the folder is gone (api.js).
  document.addEventListener("g:design_missing", () => {
    if (state.designDir && state.designMissing !== state.designDir) showMissingProject(state.designDir);
  });
  document.getElementById("btn-set-design")?.addEventListener("click", onLoadDesign);
  document.getElementById("design-dir-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") onLoadDesign();
  });

  document.getElementById("pdk-select")?.addEventListener("change", onPdkSelected);
  document.getElementById("scl-select")?.addEventListener("change", onSclSelected);

  // Setup's own Run flow shows the run; the top-bar Run leaves you where you are.
  document.getElementById("btn-run")?.addEventListener("click", () => onRunClick({ show: true }));
  document.getElementById("btn-cancel")?.addEventListener("click", () => requestStop());
  // Global topbar Run/Stop: start the flow, or stop a running one, from any tab —
  // no need to navigate back to Setup. Reuses the same start/stop paths.
  document.getElementById("btn-run-global")?.addEventListener("click", () => {
    if (_start || (state.status && state.status.running)) requestStop();
    // Every slot taken (other runs, other projects): the button says "Add to
    // queue" and does that; the run starts by itself when one ends.
    else onRunClick({ show: false, when: slotFree() ? "now" : "queue" });
  });
  document.addEventListener("g:queue_state", () => paintRunLabel());
  document.addEventListener("g:run_only", () => paintRunLabel());
  document.getElementById("btn-resume")?.addEventListener("click", () => api.resumeRun(state.followJob).catch(() => {}));
  document.getElementById("btn-preflight")?.addEventListener("click", () => renderPreflight());

  wireRunMenu();

  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".mode-btn").forEach((b) =>
        b.classList.toggle("mode-btn-active", b === btn),
      );
      state.mode = btn.dataset.mode;
      import("./shell.js").then((m) => m.paintRunSettings()).catch(() => {});
    });
  });
}

async function onLoadDesign() {
  const input = document.getElementById("design-dir-input");
  const designDir = (input?.value || "").trim();
  if (!designDir) {
    toast.show("Please paste the path to your design folder.", "warn");
    return;
  }
  try {
    const res = await api.setDesignDir(designDir);
    await adoptDesignDir(res.design_dir);
  } catch (ex) {
    toast.show("Could not load that folder: " + ex.message, "error");
  }
}

// Populate the UI for a design directory the server already considers active.
// Shared by manual "Load design", the SPM example, and `--design-dir` boot.
// `opts.explicit` (default true) records that the person chose this project,
// as opposed to boot re-opening the last one ({explicit:false}).
export async function adoptDesignDir(designDir, opts = {}) {
  if (!(await openDesignQuick(designDir, opts))) return;
  await finishAdopt(designDir, opts);
}

let _adopting = null;   // the project whose folder is being read (adoptDesignDir)

// The part of opening a project every page needs before it can draw: the
// server's active project, the project card, and the config's PDK and
// library (a cheap read). False when the folder is gone.
export async function openDesignQuick(designDir, opts = {}) {
  const explicit = opts.explicit !== false;
  // Another project: the last one's runs are not this one's (Setup's strip
  // ticked "Run the flow" on a new project that had never run).
  if (state.designDir !== designDir) {
    state.runs = [];
    // Nor is the last run's result in the status bar and window title.
    if (state.designDir) import("./runend.js").then((m) => m.clearRunResult()).catch(() => {});
  }
  state.designDir = designDir;
  // Nor are its one-off settings (All settings): each project keeps its own
  // (G13), and the chip next to Run shows this one's.
  switchOverridesTo(designDir);
  state.designExplicit = explicit;
  // Until its files are in (finishAdopt), a PDK pick doesn't repaint the
  // Design row: the file list does, once.
  _adopting = designDir;
  rememberDesign(designDir);
  paintOnboardChecklist();  // step 1 (project open) is now satisfied
  paintProjectBlock();
  // Keep the SERVER's active design dir in lock-step with the frontend's. Every
  // run-scoped endpoint (/api/runs, /api/runs/<tag>, cell-usage, run-images,
  // verify, …) resolves against the server's active dir. If it ever drifts from
  // state.designDir, the per-tab run dropdowns come back empty until the user
  // opens a run from the Runs window (which re-syncs it). Syncing here makes
  // every tab show the runs immediately, no manual Open needed.
  try {
    await api.setDesignDir(designDir);
    state.designMissing = null;
  } catch (ex) {
    // A folder that was moved or deleted: say so plainly instead of loading
    // a blank project, and keep Run off.
    if (/not found/i.test((ex && ex.message) || "")) { _adopting = null; showMissingProject(designDir); return false; }
  }
  paintMissing();
  paintDesignPill("loading…");
  // The results pages show what this project showed last time, else its
  // newest run (context.js).
  (await import("./context.js")).openProject(designDir);
  await readConfigPdk();
  // All settings' values come from this project's config now.
  refreshEffective();
  return true;
}

// The rest of opening a project: the folder's summary, its files and runs
// (read side by side), the PDK picker following the config, the checklist.
export async function finishAdopt(designDir, opts = {}) {
  const explicit = opts.explicit !== false;
  const stale = () => state.designDir !== designDir;
  const summaryEl = document.getElementById("design-summary");
  const summaryP = summariseDir(designDir).then((html) => {
    if (stale()) return;
    if (summaryEl) summaryEl.innerHTML = html;
    paintDesignPill(fmt.shortPath(designDir));
  });
  const runsP = api.runs(designDir).catch(() => []);
  // The PDK the project's config names is the one LibreLane builds for: the
  // picker shows it, rather than whatever was used last. (Then the files: the
  // Design row they paint reads the picked PDK's sections.)
  _adopting = designDir;
  try { await syncPdkFromConfig({ reread: false }); } finally { if (_adopting === designDir) _adopting = null; }
  await loadFilesFor(designDir);   // paints the Design row
  const runs = await runsP;
  await summaryP;
  if (stale()) return;
  state.runs = runs;
  paintOnboardChecklist();
  if (!state.selectedRunTag && state.runs && state.runs.length) state.selectedRunTag = state.runs[0].tag;
  // The Runs page lists them when it shows (it renders on every visit).
  if (currentTab() === "runs") import("./runs.js").then((m) => m.renderRuns()).catch(() => {});
  paintRunButton();
  renderPreflight();
  // Offer to auto-generate a config when the folder has sources but no config.
  try {
    const { maybeOfferAutoConfig } = await import("./autoconfig.js");
    maybeOfferAutoConfig(designDir);
  } catch (_e) { /* non-fatal */ }
  // The person opened a project (not boot re-opening the last one). After the
  // caller's own page change (the wizard goes to Setup when this returns).
  if (explicit) setTimeout(() => document.dispatchEvent(new CustomEvent("g:project_opened", { detail: { dir: designDir } })), 0);
}

// A project whose folder is gone (moved, renamed or deleted): its card says
// so, with the two ways out, and Run stays off until it is found or removed.
export function showMissingProject(dir) {
  state.designDir = dir;
  state.designMissing = dir;
  paintProjectBlock();
  paintMissing();
  paintRunButton();
  paintOnboardChecklist();
  paintDesignPill(fmt.shortPath(dir));
}

function paintMissing() {
  const card = document.getElementById("project-open");
  if (!card) return;
  const dir = state.designMissing;
  card.classList.toggle("is-missing", !!dir);
  let box = document.getElementById("proj-missing");
  if (!dir) { if (box) box.remove(); return; }
  if (!box) {
    box = document.createElement("div");
    box.id = "proj-missing";
    box.className = "proj-missing";
    box.setAttribute("role", "alert");
    card.querySelector(".proj-path")?.after(box);
    box.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-miss]");
      if (!b) return;
      const gone = state.designMissing;
      if (b.dataset.miss === "locate") {
        (await import("./shell.js")).openFolder();
        return;
      }
      try { await api.projectForget(gone); } catch (_e) { /* the local list still drops it */ }
      try {
        const list = JSON.parse(localStorage.getItem("ll.recentDesigns") || "[]").filter((d) => d !== gone);
        localStorage.setItem("ll.recentDesigns", JSON.stringify(list));
      } catch (_e) { /* storage may be off */ }
      state.designMissing = null;
      state.designDir = null;
      state.runs = [];
      switchOverridesTo(null);
      paintProjectBlock();
      paintMissing();
      paintRunButton();
      paintDesignPill("");
      toast.show("Removed it from the list. Nothing on disk was touched.", "success");
    });
  }
  box.innerHTML = "<p class='proj-missing-h'>" + icon("alert", { size: 15 }) + "<b>Project folder not found</b></p>" +
    "<p class='proj-missing-p'><code class='proj-missing-path'>" + fmt.escape(dir) + "</code></p>" +
    "<p class='muted proj-missing-p'>It may have been moved, renamed or deleted. Point LanEx at its new place, or take it off the list.</p>" +
    "<div class='picker-actions'><button class='btn btn-primary' data-miss='locate'>Locate…</button>" +
    "<button class='btn btn-ghost' data-miss='forget'>Remove from list</button></div>";
  // The path as the person's OS shows it (the card's own path line does the same).
  import("./shell.js").then((m) => { const c = box.querySelector(".proj-missing-path"); if (c) { c.textContent = m.displayPath(dir); c.title = dir; } }).catch(() => {});
  const sum = document.getElementById("design-summary");
  if (sum) sum.innerHTML = "";
}

// The Project block: an empty state with the two ways in, or the open
// project's name, where it lives and what it contains.
function paintProjectBlock() {
  const open = !!state.designDir;
  const empty = document.getElementById("project-empty");
  const card = document.getElementById("project-open");
  if (empty) empty.hidden = open;
  if (card) card.hidden = !open;
  const sw = document.getElementById("hub-switch");
  if (sw) sw.hidden = !open;
  document.getElementById("hub-current-label")?.replaceChildren(open ? "Open project" : "Get started");
  // Sources and Settings belong to a project; with none open they'd only say so.
  document.getElementById("sec-setup")?.classList.toggle("no-project", !open);
  if (!open) { document.dispatchEvent(new CustomEvent("g:projects_changed")); return; }
  const dir = state.designDir;
  const name = dir.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || dir;
  const n = document.getElementById("proj-name");
  const p = document.getElementById("proj-path");
  if (n) n.textContent = name;
  import("./shell.js").then((m) => { if (p) { p.textContent = m.displayPath(dir); p.title = dir; } }).catch(() => {});
  document.dispatchEvent(new CustomEvent("g:projects_changed"));
}

// Under the Design row: one line about the newest run, and a warning line for
// anything that will stop a run (no config, spaces in the path).
// The one "how long ago" (metricfmt.ago).
const ago = (sec) => MFago(sec);

function paintLastRun(facts) {
  const el = document.getElementById("proj-lastrun");
  if (!el) return;
  const last = facts && facts.last_run;
  if (!facts || !facts.runs || !last) { el.innerHTML = "No runs yet."; return; }
  // The newest folder of a run still going has no final/ yet: that is not
  // "didn't finish".
  const going = state.status && state.status.running;
  el.innerHTML = (going ? "A run is going now" : "Last run " + (last.finished ? "finished" : "didn't finish") + " " + fmt.escape(ago(last.at))) +
    " · <button type='button' class='linklike' data-open='runs'>" +
    (facts.runs === 1 ? "View it" : "View all " + facts.runs + " runs") + "</button>";
}

// Only the "Last run" line, read again (after a run ends).
let _factsTok = 0;
async function summariseFacts(path) {
  const tok = ++_factsTok;
  try {
    const r = await fetch("/api/design-summary?path=" + encodeURIComponent(path), { headers: { "X-Requested-With": "XMLHttpRequest" } });
    const body = await r.json();
    if (tok !== _factsTok || path !== state.designDir || !r.ok || body.ok === false) return;
    paintLastRun((body.data || {}).facts || null);
  } catch (_e) { /* keep what it says */ }
}

async function summariseDir(path) {
  const src = "/api/design-summary?path=" + encodeURIComponent(path);
  try {
    const r = await fetch(src, { headers: { "X-Requested-With": "XMLHttpRequest" } });
    const body = await r.json();
    if (!r.ok || body.ok === false) throw new Error(body.error || "scan failed");
    const data = body.data || {};
    paintLastRun(data.facts || null);
    const warns = (data.pills || []).filter((x) => x.type === "warn" || x.type === "fail");
    return warns.map((x) =>
      "<p class='proj-note'>" + icon("alert", { size: 14 }) + "<span>" + fmt.escape(x.text) + "</span></p>").join("");
  } catch (_e) {
    paintLastRun(null);
    return "<p class='proj-note is-fail'>" + icon("alert", { size: 14 }) + "<span>Could not read this folder.</span></p>";
  }
}

// The top-bar project switcher names the open project (folder name; full path
// on hover). While a design is being opened it says so.
export function paintDesignPill(text) {
  if (text === "loading…") {
    const t = document.querySelector("#design-pill .text");
    if (t) t.textContent = "Opening…";
    return;
  }
  import("./shell.js").then((m) => m.paintProject()).catch(() => {});
}

export function paintPdkPill(text, kind) {
  const pill = document.getElementById("pdk-pill");
  pill.classList.remove("pill-warn", "pill-pass", "pill-fail");
  if (kind) pill.classList.add(kind);
  document.querySelector("#pdk-pill .text").textContent = text;
  // Mirror onto the inline readiness pill beside the PDK selects (D5): it used to
  // sit frozen at "checking…" because nothing ever repainted it.
  const inline = document.getElementById("pdk-readiness");
  if (inline) {
    inline.classList.remove("pill-pending", "pill-warn", "pill-pass", "pill-fail");
    if (kind) inline.classList.add(kind);
    inline.textContent = text.replace(/^PDK:\s*/, "");
  }
  paintOnboardChecklist();
}

// The hero's three steps look the same whatever the progress; only the first
// step's button follows state (New project until one is open).
export function paintOnboardChecklist() {
  const list = document.getElementById("onboard-checklist");
  if (!list) return;
  const first = list.querySelector('[data-step-act="project"]');
  if (first) first.textContent = state.designDir ? "View project" : "New project";
  first?.classList.toggle("btn-ghost", !!state.designDir);
  // Live progress for the open project: a tick on what's done, the next step
  // marked (the same progress the first-chip guide follows). The simulation
  // tick is set by guide.js when a simulation of this project runs to the end.
  // A project whose folder is gone has nothing done yet (its card says so).
  const dd = state.designMissing ? null : state.designDir;
  const done = {
    design: !!dd,
    rtl: !!dd && safeStorage.get("ll.simok." + dd, "") === "1",
    pdk: !!dd && !!document.getElementById("pdk-readiness")?.classList.contains("pill-pass"),
    run: !!dd && (state.runs || []).length > 0,
  };
  let next = null;
  list.querySelectorAll("li[data-ck]").forEach((li) => {
    const ok = !!done[li.dataset.ck];
    li.classList.toggle("is-done", ok);
    li.classList.remove("is-next");
    if (!ok && !next) next = li;
  });
  next?.classList.add("is-next");
}

// ---------- the project's PDK ---------------------------------------------------
//
// LibreLane builds for the PDK and library the config file names, whatever
// -p/-s say (librelane/config/config.py reads the config's PDK first). So the
// picker follows the config when a project opens, a change to it is saved to
// the config, and a run whose picker and config disagree is refused by the
// server with both values named (configedit.pdk_mismatch).

let _cfgPdk = null;   // {dir, pdk, scl, name, editable}: what the config names

// The usual general-purpose library of each PDK family (pdk_catalog.py).
export const USUAL_SCL = ["sky130_fd_sc_hd", "gf180mcu_fd_sc_mcu7t5v0", "sg13g2_stdcell"];

function hasOpt(sel, v) { return !!v && Array.from(sel.options).some((o) => o.value === v); }

function addOpt(sel, v, label) {
  const o = document.createElement("option");
  o.value = v;
  o.textContent = label;
  sel.appendChild(o);
}

async function readConfigPdk() {
  _cfgPdk = null;
  if (!state.designDir) { paintPdkSrc(); return null; }
  try {
    // keys_only: the config's two values, without the source scan (cheap).
    const info = await api.designInfo({ keys: ["PDK", "STD_CELL_LIBRARY"], keys_only: true });
    const s = (info && info.settings) || {};
    const str = (v) => (typeof v === "string" ? v.trim() : "");
    _cfgPdk = {
      dir: state.designDir, pdk: str(s.PDK), scl: str(s.STD_CELL_LIBRARY),
      name: (info.config && info.config.name) || "the config", editable: !!(info.config && info.config.editable),
    };
  } catch (_e) { /* no config yet: nothing to follow */ }
  paintPdkSrc();
  return _cfgPdk;
}

// The PDK and library pickers' source buttons: the config's line for each.
function paintPdkSrc() {
  const c = cfgFor();
  import("./provenance.js").then((m) => m.paintConfigSrc(document.getElementById("setup-pdk"),
    (k) => !!(c && (k === "PDK" ? c.pdk : c.scl)))).catch(() => {});
}

const cfgFor = () => (_cfgPdk && _cfgPdk.dir === state.designDir ? _cfgPdk : null);

// LibreLane's CLI default for -p/--pdk (librelane/flows/cli.py).
const LIBRELANE_DEFAULT_PDK = "sky130A";

// Point the picker at the PDK the open project's config names. A PDK that
// isn't installed is still shown (and said), so the picker never pretends.
// `reread: false` uses the config read a moment ago (openDesignQuick).
export async function syncPdkFromConfig({ reread = true } = {}) {
  const c = reread || !cfgFor() ? await readConfigPdk() : cfgFor();
  const sel = document.getElementById("pdk-select");
  if (c && !c.pdk && sel && hasOpt(sel, LIBRELANE_DEFAULT_PDK)) {
    // A config that names no PDK builds for LibreLane's default (sky130A, the
    // -p default), as a terminal run of the folder would. Keeping the picker
    // from the project opened before built spm on sky130B after a sky130B
    // project: a project's target must not depend on what was open before.
    if (sel.value !== LIBRELANE_DEFAULT_PDK) { sel.value = LIBRELANE_DEFAULT_PDK; await onPdkSelected(); }
    return;
  }
  if (!c || !c.pdk || !sel) return;
  const installed = (state.pdks || []).some((p) => p.name === c.pdk);
  if (!hasOpt(sel, c.pdk)) addOpt(sel, c.pdk, c.pdk + " (not installed)");
  // The picker already shows what the config names (boot fills it after
  // reading the config): nothing to redo.
  const already = sel.value === c.pdk && state.selectedPdk === c.pdk && (!c.scl || state.selectedScl === c.scl);
  sel.value = c.pdk;
  if (!already) await onPdkSelected();
  if (!installed) {
    toast.show("This project is set up for " + c.pdk + ", which isn't installed. Install it in Tools, or pick an installed PDK here " +
      "and LanEx saves it to " + c.name + ".", "warn", 10000, { label: "Open Tools", run: () => import("./nav.js").then((m) => m.go("tools")) });
  }
}

// The picker's PDK and library for the config: what it has to write so that
// reopening the project shows this pick again. A config that names no PDK
// builds for LibreLane's default (sky130A with sky130_fd_sc_hd), so picking
// exactly that writes nothing; any other pick is saved, or the picker would
// snap back to sky130A the next time the project opens.
export function targetSets(c, pdk, scl) {
  const set = {};
  if (!c) return set;
  const names = !!(c.pdk || c.scl);
  if (pdk && pdk !== c.pdk && (names || pdk !== LIBRELANE_DEFAULT_PDK)) set.PDK = pdk;
  if (scl && scl !== c.scl && (names || set.PDK || scl !== "sky130_fd_sc_hd")) set.STD_CELL_LIBRARY = scl;
  return set;
}

// Save the picker's PDK and library to the config (a person's pick of the
// target, so the project keeps it).
async function saveTargetToConfig() {
  const c = cfgFor();
  if (!c) return;
  const set = targetSets(c, state.selectedPdk, state.selectedScl);
  if (!Object.keys(set).length) return;
  if (!c.editable) {
    toast.show(c.pdk || c.scl
      ? c.name + " names " + (c.pdk || c.scl) + " and LanEx can't edit it. Change it there too: a run stops until the two agree."
      : "LanEx can't save the PDK to " + c.name + ", so runs from LanEx use " + state.selectedPdk + " but the project opens on sky130A again.", "warn", 9000);
    return;
  }
  try {
    const r = await api.configSet(set, [], { pdk: state.selectedPdk || null, scl: state.selectedScl || null });
    if (state.varsValues) { delete state.varsValues.PDK; delete state.varsValues.STD_CELL_LIBRARY; }
    _cfgPdk = Object.assign({}, c, { pdk: set.PDK || c.pdk, scl: set.STD_CELL_LIBRARY || c.scl });
    paintPdkSrc();
    if (r.changed && r.changed.length) {
      const what = [set.PDK, set.STD_CELL_LIBRARY].filter(Boolean).join(" / ");
      const undo = r.undo && r.undo.backup ? { label: "Undo", run: () => undoTarget(r.undo, r.name) } : null;
      toast.show("Saved " + what + " to " + r.name + ".", "success", 6000, undo);
    }
    document.dispatchEvent(new CustomEvent("g:config_changed"));
  } catch (ex) {
    toast.show("Couldn't save the PDK to " + c.name + ": " + (ex.message || ex) + "." +
      (c.pdk || c.scl ? " A run stops until the two agree." : ""), "error", 9000);
  }
}

async function undoTarget(u, name) {
  try {
    await api.configUndo({ backup: u.backup, after: u.after });
    toast.show(name + " is back as it was.", "success", 4000);
    document.dispatchEvent(new CustomEvent("g:config_changed"));
  } catch (ex) {
    toast.show("Undo: " + (ex.message || ex), "warn", 8000);
  }
  await syncPdkFromConfig();
}

// The one-click fixes for a run (or the checklist) refused because the
// picker and the config disagree: save the picker's choice to the config,
// or set the picker to what the config says.
export async function resolvePdkMismatch(mm, how) {
  if (!mm) return false;
  if (how === "save") {
    try {
      const r = await api.configSet(mm.fix || {}, [], { pdk: state.selectedPdk || null, scl: state.selectedScl || null });
      const what = [mm.fix.PDK, mm.fix.STD_CELL_LIBRARY].filter(Boolean).join(" / ");
      const undo = r.undo && r.undo.backup ? { label: "Undo", run: () => undoTarget(r.undo, r.name) } : null;
      toast.show("Saved " + what + " to " + r.name + ".", "success", 6000, undo);
      await readConfigPdk();
      document.dispatchEvent(new CustomEvent("g:config_changed"));
      return true;
    } catch (ex) {
      toast.show("Couldn't save to " + (mm.name || "the config") + ": " + (ex.message || ex), "error", 9000);
      return false;
    }
  }
  await syncPdkFromConfig();
  const ss = document.getElementById("scl-select");
  if (mm.config.scl && ss) {
    if (!hasOpt(ss, mm.config.scl)) addOpt(ss, mm.config.scl, mm.config.scl + " (not installed)");
    ss.value = mm.config.scl;
    state.selectedScl = mm.config.scl;
    refreshPdkReadiness();
  }
  toast.show("The PDK picker now follows " + (mm.name || "the config") + ".", "success", 4000);
  return true;
}

async function onPdkSelected(e) {
  const user = !!(e && e.type === "change");
  const sel = document.getElementById("pdk-select");
  state.selectedPdk = sel.value;
  if (!sel.value) return;
  safeStorage.set("ll.recentPdk", sel.value);   // remember for next session
  try {
    const scls = await api.scls(sel.value);
    const sclSel = document.getElementById("scl-select");
    const keep = state.selectedScl;
    sclSel.innerHTML = "";
    for (const s of scls) {
      const opt = document.createElement("option");
      opt.value = s.variants[0]?.[1] || "";
      opt.textContent = s.variants[0]?.[0] || "(unknown)";
      sclSel.appendChild(opt);
    }
    // The config's library when it names one for this PDK; then the one in
    // use if this PDK offers it too (sky130A and sky130B share libraries), the
    // last-used one, and the family's usual general-purpose one
    // (sky130_fd_sc_hd, the 7-track gf180 cells).
    const offered = (v) => hasOpt(sclSel, v);
    const c = cfgFor();
    const cfgScl = c && c.scl && (c.pdk === sel.value || offered(c.scl)) ? c.scl : "";
    if (cfgScl && !offered(cfgScl)) addOpt(sclSel, cfgScl, cfgScl + " (not installed)");
    const wantScl = safeStorage.get("ll.recentScl", "");
    const usual = USUAL_SCL.find(offered);
    if (cfgScl) sclSel.value = cfgScl;
    else if (offered(keep)) sclSel.value = keep;
    else if (offered(wantScl)) sclSel.value = wantScl;
    else if (usual) sclSel.value = usual;
    state.selectedScl = sclSel.value || "";
    paintPdkAbout(sel.value);
    if (user) await saveTargetToConfig();
    if (user) document.dispatchEvent(new CustomEvent("g:pdk_confirmed", { detail: { pdk: sel.value } }));
    // While a project opens, its file list paints the Design row once the
    // files are in (fs.js loadFilesFor): no second read here.
    if (_adopting !== state.designDir) import("./projectcard.js").then((m) => m.paintDesignRow()).catch(() => {});
    // Which pdk::/scl:: sections apply follows the pick (All settings' values).
    refreshEffective();
    refreshPdkReadiness();
  } catch (ex) {
    paintPdkPill("error", "pill-fail");
  }
}

// One plain line about the chosen process, and its layer stack in the hero.
function paintPdkAbout(pdk) {
  const about = document.getElementById("pdk-about");
  const facts = pdkFacts(pdk);
  if (about) about.textContent = facts.about;
  const fig = document.getElementById("layer-stack");
  const html = stackHtml(pdk);
  // Unknown PDK: keep the sky130 stack the page ships with.
  if (fig && html) fig.innerHTML = html;
}

async function onSclSelected(e) {
  const sel = document.getElementById("scl-select");
  state.selectedScl = sel.value;
  if (sel.value) safeStorage.set("ll.recentScl", sel.value);
  if (e && e.type === "change") await saveTargetToConfig();
  refreshEffective();
  refreshPdkReadiness();
}

async function refreshPdkReadiness() {
  if (!state.selectedPdk) return;
  try {
    const r = await api.pdkReady(state.selectedPdk, state.selectedScl, state.runMode);
    if (r.ready) {
      paintPdkPill("PDK ready", "pill-pass");
    } else if (r.needs_download && r.network_available) {
      paintPdkPill("PDK: will download on first run", "pill-warn");
    } else {
      paintPdkPill("PDK: " + state.selectedPdk + " (missing " + (r.missing.join(", ") || "files") + ")", "pill-warn");
    }
  } catch (_e) {
    paintPdkPill("PDK: check failed", "pill-fail");
  }
  renderPreflight();
}

// `list`: the PDK list boot already fetched (else it is fetched here).
export async function populatePdkPicker(list) {
  try {
    const pdks = Array.isArray(list) ? list : await api.pdks();
    const sel = document.getElementById("pdk-select");
    sel.innerHTML = "";
    if (!pdks.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "No PDK installed";
      sel.appendChild(o);
    }
    for (const p of pdks) {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.name + (p.ready ? "" : " (no libs)");
      sel.appendChild(o);
    }
    // The PDK the open project's config names (shown even when it isn't
    // installed), then the last one used if it's ready, then a ready sky130A,
    // then the first ready PDK: an empty leftover folder is never the default.
    const projectPdk = (cfgFor() || {}).pdk || "";
    if (projectPdk && !hasOpt(sel, projectPdk)) addOpt(sel, projectPdk, projectPdk + " (not installed)");
    const has = (v) => hasOpt(sel, v);
    const ready = (v) => pdks.some((p) => p.name === v && p.ready);
    const recent = safeStorage.get("ll.recentPdk", "");
    const firstReady = ["sky130A", "gf180mcuD", "ihp-sg13g2"].find(ready) || (pdks.find((p) => p.ready) || pdks[0] || {}).name || "";
    const wantPdk = has(projectPdk) ? projectPdk : ready(recent) ? recent : firstReady;
    if (wantPdk && Array.from(sel.options).some((o) => o.value === wantPdk)) {
      sel.value = wantPdk;
      await onPdkSelected();   // repopulates SCLs + restores the SCL + readiness
    } else if (pdks.length) {
      paintPdkPill("PDK: pick one", "pill-warn");
    } else {
      paintPdkPill("No PDKs installed", "pill-fail");
    }
  } catch (_e) {
    paintPdkPill("PDK: probe failed", "pill-fail");
  }
}

export function paintRunButton() {
  const b = document.getElementById("btn-run");
  const g = document.getElementById("btn-run-global");
  // Don't fight paintRunning while a run is live (the global button is "Stop").
  const running = state.status && state.status.running;
  const off = !state.designDir || !!state.designMissing;
  if (g && !running) g.disabled = off;
  if (!b) return;
  b.disabled = off;
}

// Errors the RTL IDE's last syntax check found in a project's design files (ide/main.js
// recordRtlStatus keeps it per project), or 0 when it wasn't checked.
export function lastLintErrors(designDir, store = safeStorage) {
  if (!designDir) return 0;
  let st = null;
  try { st = JSON.parse(store.get("ll.rtl:" + designDir, "") || "null"); } catch (_e) { st = null; }
  // Design files only: the flow never lints testbenches.
  const n = st && st.lint && Number(st.lint.design_errors);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

// ---------- Run ----------------------------------------------------------------
//
// A Run click answers at once: the button turns into Stop and Flow opens in a
// "Starting the run…" state (unless the Run menu says to stay). Setup's "Ready
// to run?" check is used when it is fresh for these picks; otherwise it runs
// first ("Checking the config…"), and the start request goes out only after
// it: a blocker it finds asks before anything starts, so there is never a run
// folder and then "Stop the run?" (audit 1c). The server checks again (the
// config through LibreLane's own loader, the top module, the clock port) and
// refuses a run whose folder is gone, whose path has spaces, whose PDK picker
// and config disagree, or while another flow runs; Flow says why with the way
// back.

// The run being started: { from, show, cancel, error } while the start
// request is out (state.runStart is what Flow paints from).
let _start = null;

function beginStart(from, show) {
  _start = { at: Date.now(), from, show, cancel: false, error: null, tag: null };
  state.runStart = _start;
  paintStarting();
  document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "starting" } }));
}

// The top-bar button as Stop the moment Run is pressed.
function paintStarting() {
  const g = document.getElementById("btn-run-global");
  if (!g) return;
  g.disabled = false;
  g.innerHTML = icon("stop", { size: 15 }) + "<span>Stop</span>";
  g.classList.add("is-stop");
  g.dataset.tip = "Stop the run that is starting";
  const b = document.getElementById("btn-run");
  if (b) b.disabled = true;
}

// Stop pressed while the start request is still out: the run is stopped the
// moment the server says it started.
export function cancelStart() {
  if (!_start) return false;
  _start.cancel = true;
  const g = document.getElementById("btn-run-global");
  if (g) { g.disabled = true; const s = g.querySelector("span"); if (s) s.textContent = "Stopping…"; }
  document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "stopping" } }));
  return true;
}

// Stop pressed before the start request went out (during the check): nothing
// started, so the page simply goes back to how it was.
function abandonStart() {
  _start = null;
  state.runStart = null;
  paintRunning(false);
  document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "cancelled" } }));
}

// A run older than this asks before it is stopped (audit G6): one stray click
// on Stop used to throw away a long run. A run that just started stops at
// once (pressing Run by mistake is undone in one click).
export const STOP_CONFIRM_AFTER_MS = 30000;

// Pure (tested). An unknown start time (a run started in another window, or
// before this page was loaded) counts as old.
export function stopNeedsConfirm(startedAt, now = Date.now()) {
  if (!startedAt) return true;
  return now - startedAt >= STOP_CONFIRM_AFTER_MS;
}

function runAge(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return s + " seconds";
  const m = Math.round(s / 60);
  return m < 90 ? m + " minutes" : (Math.round(m / 6) / 10) + " hours";
}

let _stopAsking = false;

// Every Stop goes through here: the top-bar Stop, Setup's Stop, Flow's Stop
// and the palette's "Stop the run". Resolves true when a stop was asked for.
export async function requestStop() {
  if (cancelStart()) return true;   // still starting: nothing to lose yet
  if (!(state.status && state.status.running)) return false;
  if (_stopAsking) return false;
  if (stopNeedsConfirm(state.runStartedAt)) {
    _stopAsking = true;
    let ok = false;
    try {
      const { confirmDialog } = await import("./dialog.js");
      const age = state.runStartedAt ? "It has been running for " + runAge(Date.now() - state.runStartedAt) + ". " : "";
      ok = await confirmDialog({
        title: "Stop the run?",
        body: age + "Stopping ends the step it is on now. What it finished so far stays in the run folder, and you can resume from a finished stage later.",
        confirmText: "Stop the run", cancelText: "Keep running", danger: true,
      });
    } finally { _stopAsking = false; }
    if (!ok || !(state.status && state.status.running)) return false;
  }
  api.cancelRun(state.followJob).catch((e) => toast.show(e.message || "The run could not be stopped.", "warn", 9000));
  return true;
}

// Plain words for the server's terse refusals.
export function refusalText(msg) {
  const m = String(msg || "").trim();
  if (/^already running$/i.test(m)) return "A run is already going. Stop it, or watch it here, before starting another.";
  if (!m || /^HTTP \d+$/.test(m)) return "LanEx didn't answer the start request (" + (m || "no reply") + "). Try again in a moment.";
  return m;
}

function startRefused(reason, extra = {}) {
  const s = _start;
  _start = null;
  paintRunning(false);
  if (!s) return;
  s.error = { reason: refusalText(reason), ...extra };
  state.runStart = s;
  document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "refused" } }));
  // Flow says it when it shows the run; elsewhere a toast does.
  const onFlow = document.getElementById("sec-pipeline")?.classList.contains("section-active");
  if (!onFlow && !extra.mismatch) toast.show("Run refused: " + s.error.reason, "error");
}

// The server started it: the page shows the new run as live straight away
// (the run's first events follow a moment later).
function startAccepted(tag) {
  const s = _start;
  _start = null;
  state.runStart = null;
  // The run's own first events can arrive before this reply (app.js then
  // already holds its step graph): only a run not heard from yet is reset.
  const heard = !!tag && state.liveTag === tag && state.status.running;
  if (!heard) {
    state.pipeline = [];
    state.stepStatuses = {};
    state.stepTiming = {};
    state.prepPhase = null;
    state.prepStart = null;
    state.lastFlow = null;
    state.paused = false;
    state.status.running = true;
    if (tag) state.liveTag = tag;
    // Flow's elapsed clock starts now (app.js keeps it when this run's first event comes).
    state.runStartedAt = Date.now();
    state.runStartedFor = tag || "";
  }
  paintRunning(true);
  document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "started", tag } }));
  if (s && s.cancel) api.cancelRun(state.followJob).catch(() => {});
}

async function confirmBlockers(blockers, { quiet = false } = {}) {
  const { confirmDialog } = await import("./dialog.js");
  const ok = await confirmDialog({
    title: "Not everything is ready",
    body: "Blockers: " + blockers.join("; ").replace(/\.\s*$/, "") +
      ". Run anyway? The flow will likely stop at the first missing piece.",
    confirmText: "Run anyway", danger: true,
  });
  if (!ok && !quiet) toast.show("Run cancelled. Review the 'Ready to run?' checklist for details.", "warn");
  return ok;
}

// A blocker LibreLane's own config loader found can't be run anyway: the run
// would stop in its first second. Pure (tested).
export function hardBlockers(pf) {
  const chk = pf && pf.config_check;
  return ((chk && chk.problems) || []).filter((p) => p.hard)
    .map((p) => (p.message + (p.fix ? " " + p.fix : "")).trim());
}

// Start a run from the Run menu's own buttons: "now" or "queue".
export function startFromMenu(when) { return onRunClick({ show: false, when }); }

// A run into the queue: the server runs the config check (LibreLane's own
// loader, the top module, the clock port) before it accepts it.
function queueRequest(payload) { return api.startRun(Object.assign({}, payload, { when: "queue" })); }

// "Added to the queue: 3rd in line." Pure (tested).
export function queuedText(position) {
  const n = Number(position) || 0;
  if (n <= 1) return "Added to the queue: it starts as soon as a slot is free.";
  const ord = n + (n % 10 === 1 && n % 100 !== 11 ? "st" : n % 10 === 2 && n % 100 !== 12 ? "nd" : n % 10 === 3 && n % 100 !== 13 ? "rd" : "th");
  return "Added to the queue: " + ord + " in line. It starts by itself when a slot is free.";
}

async function onRunClick({ show = false, fixed = false, ack = false, when = "now" } = {}) {
  if (_start) return;   // one is already starting (the button is Stop)
  // "Tell me when a run finishes" is on unless turned off: the browser asks
  // for permission once, here, while the click still counts as the person's.
  askNotifyOnRun();
  if (!state.designDir) {
    toast.show("Pick a design folder first.", "warn");
    return;
  }
  if (state.designMissing) {
    toast.show("Project folder not found: " + state.designMissing + ". Locate it or remove it from the list on Setup.", "warn");
    return;
  }
  // The RTL IDE's last syntax check of this project found errors: LibreLane's
  // first step (Verilator lint) stops on them, so say so before the run.
  const rselNow = document.getElementById("run-resume-from");
  const lintErrs = rselNow && rselNow.value ? 0 : lastLintErrors(state.designDir);
  if (lintErrs > 0) {
    const { choiceDialog } = await import("./dialog.js");
    const pick = await choiceDialog({
      title: "The RTL has " + lintErrs + (lintErrs === 1 ? " error" : " errors"),
      body: "The last syntax check in the RTL IDE found " + (lintErrs === 1 ? "an error" : lintErrs + " errors") +
        " in this project's design files. The flow's first step runs the same check (Verilator lint) and stops on it.",
      choices: [{ label: "Show them", value: "show" }, { label: "Run anyway", value: "run", danger: true }],
    });
    if (pick === "show") { (await import("./nav.js")).go("ide"); return; }
    if (pick !== "run") return;
  }
  // Preflight: catch missing tools / PDK / a config LibreLane refuses before a
  // long run fails half-way. A fresh result for these picks is used as is: a
  // blocker it already knows asks before anything starts. Without one, the
  // check runs first (below), still before the start request.
  const fresh = freshPreflight();
  const known = runBlocker(fresh);
  if (known && known.kind === "mismatch" && !fixed) { await offerPdkFix(known.mismatch, show); return; }
  let acked = ack;
  if (known && known.kind === "blockers" && !ack) {
    const hard = hardBlockers(fresh);
    if (hard.length) { toast.show("Run not started: " + hard.join(" "), "error"); return; }
    if (!(await confirmBlockers(known.blockers))) return;
    acked = true;
  }
  const payload = collectRunPayload();
  payload.when = when === "queue" ? "queue" : "now";
  const rsel = document.getElementById("run-resume-from");
  const only = onlyChoice();
  if (only) {
    // Only one step (LibreLane's --only): a new run of that step on the
    // results of the run on the results pages, which it needs (-i).
    payload.branch_from = { tag: only.tag, step: only.step };
    payload.to = only.step;
  } else if (rsel && rsel.value && rsel.dataset.tag) {
    // Resume from a stage of the run on the results pages: the server plans it
    // (and starts earlier if something those stages read has changed since).
    payload.branch_from = { tag: rsel.dataset.tag, step: rsel.value };
  }
  if (payload.when === "queue") {
    // Into the queue: the server checks the config now (so a run that would
    // stop in its first second is told now, not in an hour), and the run
    // starts by itself. This page stays where it is.
    try {
      if (acked) payload.checks_ack = true;
      const res = await queueRequest(payload);
      if (res && res.queued) {
        toast.show(queuedText(queuedAhead() + 1), "success", 7000,
          { label: "Show the queue", run: () => import("./queue.js").then((q) => q.openQueue()) });
        if (_queueMod) _queueMod.refreshQueue();
        return;
      }
      const chk = res && res.config_check;
      if (chk && !chk.blocking && (chk.problems || []).length && !acked) {
        const lines = chk.problems.map((p) => (p.message + (p.fix ? " " + p.fix : "")).trim());
        if (await confirmBlockers(lines, { quiet: true })) onRunClick({ show, fixed, ack: true, when });
        return;
      }
      toast.show("Not queued: " + refusalText((res && (res.reason || res.error)) || "unknown"), "error");
    } catch (ex) {
      toast.show("Not queued: " + refusalText(ex.message), "error");
    }
    return;
  }
  // Pressing Run shows the run on Flow, unless the user turned that off in
  // the Run menu (then the status line shows it, and this says so). Setup's
  // Run flow and the very first run always show it.
  const first = !safeStorage.get("ll.ranOnce") && !(state.runs || []).length;
  const showFlow = show || first || safeStorage.get("ll.showFlowOnRun") !== "0";
  // The page first (it reads the scroll position while nothing has changed
  // yet, so no forced layout), then the button and Flow's "Starting".
  const from = currentTab();
  if (showFlow) go("pipeline", {}, { link: false });
  beginStart(from, showFlow);
  if (!fresh) {
    // No fresh check: run it now, before the start request (it reads the
    // config through LibreLane, a second or two). Stop meanwhile starts nothing.
    const s = _start;
    s.checking = true;
    document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "checking" } }));
    let pf = null;
    try { pf = await preflightNow(); } catch (_e) { pf = null; }
    s.checking = false;
    if (_start !== s) return;
    if (s.cancel) { abandonStart(); return; }
    const b = runBlocker(pf);
    if (b && b.kind === "mismatch" && !fixed) {
      startRefused("The PDK picker and the config disagree.", { mismatch: true });
      await offerPdkFix(b.mismatch, show);
      return;
    }
    if (b && b.kind === "blockers" && !acked) {
      const hard = hardBlockers(pf);
      if (hard.length) { startRefused(hard.join(" ")); return; }
      const ok = await confirmBlockers(b.blockers, { quiet: showFlow });
      if (_start !== s) return;
      if (s.cancel) { abandonStart(); return; }
      if (!ok) { startRefused("Not started. " + b.blockers.join(" ")); return; }
      acked = true;
    }
    // The flow the config names is known now.
    payload.flow = (flowOf() || {}).name || null;
    document.dispatchEvent(new CustomEvent("g:run_start", { detail: { phase: "starting" } }));
  }
  if (acked) payload.checks_ack = true;
  try {
    const res = await api.startRun(payload);
    if (res && res.busy) {
      // Every slot was taken by the time the request arrived: offer the queue.
      startRefused(res.reason || "Every slot is taken.");
      const { choiceDialog } = await import("./dialog.js");
      const pick = await choiceDialog({
        title: "Every run slot is taken",
        body: (res.reason || "Other runs are going.") + " This run can wait in the queue and start by itself when one ends.",
        choices: [{ label: "Add to queue", value: "queue" }],
      });
      if (pick === "queue") onRunClick({ show, fixed, ack: acked, when: "queue" });
      return;
    }
    if (res && res.job_id && res.ok && res.started !== false) followJob(state, res.job_id, true);
    if (!res.ok || res.started === false) {
      if (res.pdk_mismatch && !fixed) {
        startRefused(res.reason || "The PDK picker and the config disagree.", { mismatch: true });
        await offerPdkFix(res.pdk_mismatch, show);
        return;
      }
      // The server's own check found something the page's check had not seen
      // (a one-off setting, a file changed since): a hard finding stops here,
      // a top-module or clock finding asks, like the page's check does.
      const chk = res.config_check;
      if (chk && !chk.blocking && (chk.problems || []).length && !acked) {
        const lines = chk.problems.map((p) => (p.message + (p.fix ? " " + p.fix : "")).trim());
        startRefused(res.reason || lines.join(" "));
        if (await confirmBlockers(lines, { quiet: true })) onRunClick({ show, fixed, ack: true });
        return;
      }
      startRefused(res.reason || res.error || "unknown");
      return;
    }
    startAccepted(res.tag || payload.tag || null);
    safeStorage.set("ll.ranOnce", "1");
    if (payload.branch_from) {
      const r = res.resume;
      const asked = FS.stagesOf([{ id: payload.branch_from.step }])[0];
      toast.show("Resuming " + payload.branch_from.tag + " from " + (r ? FS.stageLabel(r.stage) : "the start") + " as " + res.tag +
        (r && r.reasons && r.reasons.includes("earlier") ? ", earlier than " + FS.stageLabel(asked) + ": something it reads changed since" : "") + ".", "success", 9000);
      rsel.value = "";
      paintRunLabel();
    }
    if (res.warning) toast.show(res.warning, "warn");
    // TOCTOU (N7): if the user opened the Final-settings preview and then the
    // config file changed before this run, the preview no longer matches what
    // ran. Compare the previewed config hash to the one the run actually used.
    if (state.previewedConfigHash && res.config_hash &&
        res.config_hash !== state.previewedConfigHash) {
      toast.show("The configuration file changed after your preview. Flow execution used the updated file on disk.", "warn");
    }
    state.previewedConfigHash = null;
    const tag = res.tag || payload.tag || null;
    document.dispatchEvent(new CustomEvent("g:run_started", { detail: { tag } }));
    if (!showFlow && currentTab() !== "pipeline") {
      toast.show((payload.tag ? "Run " + payload.tag + " started" : "The run started") +
        ". It keeps going in the background; the status bar shows how far it is.", "info", 7000,
        { label: "Watch it", run: () => go("pipeline") });
    }
  } catch (ex) {
    // The server refused (folder gone, spaces in the path, another flow
    // busy, PDK not ready…): its own words, on Flow.
    startRefused(ex.message);
  }
}

// The run was refused because the PDK picker and the config disagree: say
// both values and offer the two ways to make them agree, then run.
async function offerPdkFix(mm, show) {
  const { choiceDialog } = await import("./dialog.js");
  const choices = [];
  // Short labels: the body already names both values.
  if (mm.editable) choices.push({ label: "Save to " + (mm.name || "the config") + " and run", value: "save" });
  if (mm.config.pdk) choices.push({ label: "Run on " + mm.config.pdk, value: "follow" });
  const how = await choiceDialog({ title: "The PDK picker and " + (mm.name || "the config") + " disagree", body: mm.message, choices });
  if (!how) return;
  if (await resolvePdkMismatch(mm, how)) onRunClick({ show, fixed: true });
}

export function collectRunPayload() {
  // Setup's "Run name", sanitised to a filesystem-safe run-dir name.
  const rawName = (document.getElementById("run-name-input")?.value || "").trim();
  const tag = rawName ? rawName.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") : "";
  // "Run up to" a stage (Run menu): LibreLane's --to, at that stage's last step.
  const to = lastStepOf(document.getElementById("run-stop-after")?.value || "");
  // Apply PDK + SCL overrides picked in setup. Drop any blank/empty value so a
  // left-empty constraint field never becomes a bare `KEY=` the engine can't
  // type-parse (issue #11) — blank means "use the default". The backend strips
  // these too; doing it here keeps the CLI-reveal/preview honest.
  const overrides = {};
  for (const [k, v] of Object.entries(state.varsValues || {})) {
    if (v === null || v === undefined) continue;
    if (typeof v === "string" && v.trim() === "") continue;
    overrides[k] = v;
  }
  if (state.selectedPdk) overrides.PDK = state.selectedPdk;
  if (state.selectedScl) overrides.STD_CELL_LIBRARY = state.selectedScl;
  // Sources: the config's VERILOG_FILES is the project's list. The ticks are
  // sent as a per-run override only when they differ from it (a Tcl config,
  // a config LanEx couldn't edit, or a run loaded from history).
  const ticked = (state.selectedFiles || []).filter(Boolean);
  const cfgSrc = state.configSources;
  const sameAsConfig = Array.isArray(cfgSrc) && cfgSrc.length > 0 && ticked.length === cfgSrc.length &&
    ticked.every((f) => cfgSrc.includes(f));
  const sources = sameAsConfig ? [] : ticked;
  const extras = (state.extrasFiles || []).filter(Boolean);
  // The Run menu's How: threads, low priority, keep awake, and steps to skip.
  const how = runMenuChoices();
  return {
    tag: tag || null,
    frm: null,
    to: to || null,
    skip: how.skip,
    threads: how.threads,
    priority: how.priority,
    keep_awake: how.keep_awake,
    overrides,
    sources,
    extras,
    mode: state.mode,
    run_mode: state.runMode,
    // The flow the config names (meta.flow, else Classic), as the check read
    // it; the server resolves it the same way and refuses an unknown name.
    flow: (flowOf() || {}).name || null,
  };
}

// The flow the open project's config names, as the last check read it
// (preflight.js keeps it per project), or null before that / on an error.
function flowOf() {
  const f = state.flowInfo;
  return f && f.dir === state.designDir && !f.error && f.name ? f : null;
}

// The last step of a stage in the flow that runs (the config's flow, as the
// check read it; Classic until then), for --to.
function lastStepOf(stageId) {
  if (!stageId) return "";
  const known = flowOf();
  const flow = (state.flows || []).find((f) => f.name === ((known && known.name) || "Classic"));
  const ids = (known && known.steps && known.steps.length ? known.steps : null) || (flow && flow.steps) || [];
  const stages = FS.stagesOf(ids.map((id) => ({ id })));
  let last = "";
  stages.forEach((s, i) => { if (s === stageId) last = ids[i]; });
  return last;
}

// The Run menu's run options: where to stop, and what to do when a run
// starts and ends (runend.js reads the same keys).
function wireRunMenu() {
  const sel = document.getElementById("run-stop-after");
  if (sel) {
    sel.innerHTML = "<option value=''>The whole flow, to the GDS</option>" +
      FS.STAGES.filter((s) => s.id !== "signoff").map((s, i) =>
        "<option value='" + s.id + "'>" + (i + 1) + ". " + fmt.escape(s.label) + (s.id === "synthesis" ? " (cells and area in seconds)" : "") + "</option>").join("");
    sel.addEventListener("change", () => paintRunLabel());
  }
  const pref = (id, key, onChange, on = false) => {
    const box = document.getElementById(id);
    if (!box) return;
    box.checked = on ? safeStorage.get(key) !== "0" : safeStorage.get(key) === "1";
    box.addEventListener("change", async () => {
      if (onChange) {
        const r = await onChange(box.checked);
        if (r && !r.ok) { box.checked = false; toast.show(r.why, "warn", 7000); }
      } else safeStorage.set(key, box.checked ? "1" : "0");
    });
  };
  // Resume from a stage of the run on the results pages (LibreLane's -i and
  // --from, as a new run): the stages it can resume from come from the server,
  // which knows how far that run got and what changed since.
  const res = document.getElementById("run-resume-from");
  if (res) {
    res.innerHTML = "<option value=''>The start (a fresh run)</option>";
    // The note under Start speaks about a stage: it shows once one is picked.
    const syncNote = () => { const n = document.getElementById("run-resume-note"); if (n && n.textContent.startsWith("Reuses ")) n.hidden = !res.value; };
    res.addEventListener("change", () => { syncNote(); paintRunLabel(); });
    res._syncNote = syncNote;
    document.getElementById("run-opts-btn")?.addEventListener("click", () => fillResume());
    // It resumes the run on the results pages: picking another run disarms it.
    document.addEventListener("g:context", (e) => {
      if (res.value && res.dataset.tag !== (e.detail || {}).run) { res.value = ""; paintRunLabel(); }
    });
  }
  pref("run-pref-show", "ll.showFlowOnRun", null, true);
  // On unless turned off (or blocked in the browser).
  pref("run-pref-notify", "ll.notifyRuns", async (on) => (await import("./runend.js")).enableNotifications(on), true);
  const nb = document.getElementById("run-pref-notify");
  if (nb) import("./runend.js").then((m) => { nb.checked = m.notifyShownOn(); }).catch(() => {});
  pref("run-pref-chime", "ll.runChime");
  // Which flow a run uses: one small line in the menu, above "Run up to".
  const lane = sel && sel.closest(".menu-field")?.previousElementSibling;
  if (lane && !document.getElementById("run-flow-note")) {
    const note = document.createElement("p");
    note.className = "menu-note";
    note.id = "run-flow-note";
    lane.before(note);
    const hdr = document.createElement("div");
    hdr.className = "menu-lane";
    hdr.textContent = "Flow";
    note.before(hdr);
  }
  paintFlowNote();
  document.addEventListener("g:flow_known", () => { paintFlowNote(); paintRunLabel(); });
  // Skip steps, threads, low priority, keep awake, the live command, Run now / Add to queue.
  setupRunMenu({ collect: collectRunPayload, start: (when) => onRunClick({ show: false, when }) });
}

// "Classic (the config names no flow)" or "Chip, from config.yaml (meta.flow)".
export function flowNoteText(flow) {
  if (!flow || !flow.name) return "Classic, until the project's config is read.";
  if (flow.error) return flow.error;
  if (flow.source === "config") return flow.name + ", from " + (flow.config || "the config") + " (meta.flow).";
  if (flow.source === "request") return flow.name + ".";
  return flow.name + " (the config names no other flow).";
}

function paintFlowNote() {
  const el = document.getElementById("run-flow-note");
  const f = state.flowInfo;
  if (el) el.textContent = flowNoteText(f && f.dir === state.designDir ? f : null);
}

let _resumeTok = 0;
async function fillResume() {
  const sel = document.getElementById("run-resume-from");
  const note = document.getElementById("run-resume-note");
  if (!sel) return;
  const { contextRun } = await import("./context.js");
  const tag = contextRun();
  const keep = sel.value;
  const tok = ++_resumeTok;
  if (!tag) { sel.innerHTML = "<option value=''>The start (a fresh run)</option>"; if (note) note.textContent = "No run to resume from yet."; return; }
  if (note) note.textContent = "Reading " + tag + "…";
  let p = null;
  try { p = await api.fixPlan({ tag, sets: {}, start: "auto", run_mode: state.runMode }); } catch (ex) { p = { error: ex.message }; }
  if (tok !== _resumeTok) return;
  if (!p || p.error) { if (note) note.textContent = (p && p.error) || ""; return; }
  const opts = (p.stages || []).filter((s) => s.index > 0).map((s) => {
    const st = FS.stage(s.stage);
    const why = s.ok ? "" : s.index > (p.parent.stopped != null ? p.parent.stopped : p.parent.done + 1)
      ? " (" + tag + " didn't get there)" : ((p.drift || {}).versions || []).length ? " (made with other tool versions)" : " (something it reads changed)";
    return "<option value='" + fmt.escape(s.step) + "' data-stage='" + fmt.escape(s.stage) + "'" + (s.ok ? "" : " disabled") + ">" +
      (FS.STAGES.indexOf(st) + 1) + ". " + fmt.escape(st ? st.label : s.stage) + why + "</option>";
  });
  sel.innerHTML = "<option value=''>The start (a fresh run)</option>" + opts.join("");
  sel.value = [...sel.options].some((o) => o.value === keep && !o.disabled) ? keep : "";
  sel.dataset.tag = tag;
  const d = p.drift || {};
  const changed = (d.settings || []).map((x) => x.var).concat((d.files || []).map((f) => f.path));
  if (note) note.textContent = (d.versions || []).length && p.blocked ? p.blocked
    : "Reuses " + tag + "'s results up to that stage." + (changed.length ? " Changed since: " + changed.slice(0, 3).join(", ") + "." : "");
  if (sel._syncNote) sel._syncNote(); else if (note) note.hidden = false;
  paintRunLabel();
}

// Resume the run on the results pages from a stage, through the Run menu's own
// "Resume from" path (so the server plans it the same way). Returns why it
// can't, or "" once the run is asked to start.
export async function resumeFrom(stage) {
  await fillResume();
  const sel = document.getElementById("run-resume-from");
  const opt = sel && [...sel.options].find((o) => o.dataset.stage === stage);
  if (!opt) return "LanEx can't resume this run from " + FS.stageLabel(stage) + ".";
  if (opt.disabled) return opt.textContent.replace(/^\d+\.\s*/, "");
  sel.value = opt.value;
  paintRunLabel();
  document.getElementById("btn-run-global")?.click();
  return "";
}

// "Run", "Run to Placement" or "Resume from Placement": the button says what
// it will do.
function paintRunLabel() {
  const g = document.getElementById("btn-run-global");
  if (!g || (state.status && state.status.running)) return;
  const st = FS.stage(document.getElementById("run-stop-after")?.value || "");
  const rsel = document.getElementById("run-resume-from");
  const ropt = rsel && rsel.value ? rsel.options[rsel.selectedIndex] : null;
  const rst = ropt ? FS.stage(ropt.dataset.stage) : null;
  const span = g.querySelector("span");
  const full = !slotFree();
  const only = onlyChoice();
  if (span) span.textContent = full ? "Add to queue"
    : only ? "Run only " + only.step.split(".").pop()
    : rst ? "Resume from " + rst.label + (st ? " to " + st.label : "") : st ? "Run to " + st.label : "Run";
  g.classList.toggle("is-partial", !!(st || rst || only));
  g.classList.toggle("is-queue", full);
  const ic = g.querySelector("svg");
  if (ic) ic.outerHTML = icon(full ? "plus" : "play", { size: 15 });
  g.dataset.tip = full ? "Every run slot is taken. This run waits in the queue and starts by itself when one ends (r)"
    : rst ? "A new run that reuses " + rsel.dataset.tag + "'s results up to " + rst.label + " (r)"
    : "Run the " + ((flowOf() || {}).name || "Classic") + " flow on this project (r)";
}

export function paintRunning(running) {
  const b = document.getElementById("btn-run");
  const c = document.getElementById("btn-cancel");
  const r = document.getElementById("btn-resume");
  const pill = document.getElementById("run-pill");
  // Resume/Next is only meaningful in Step-by-step mode.
  const showResume = running && state.mode === "semi";
  if (r) r.hidden = !showResume;
  const g = document.getElementById("btn-run-global");
  if (running) {
    if (b) b.disabled = true;
    if (c) { c.disabled = false; c.hidden = false; }
    if (g) { g.disabled = false; g.innerHTML = icon("stop", { size: 15 }) + "<span>Stop</span>"; g.classList.add("is-stop"); g.dataset.tip = "Stop the run"; }
    pill?.classList.remove("pill-pending");
    pill?.classList.add("pill-running");
    if (pill) pill.textContent = "Running";
  } else {
    if (b) b.disabled = !state.designDir || !!state.designMissing;
    if (c) { c.disabled = true; c.hidden = true; }   // Stop shows only while a run is live
    if (g) { g.disabled = !state.designDir || !!state.designMissing; g.innerHTML = icon("play", { size: 15 }) + "<span>Run</span>"; g.classList.remove("is-stop"); paintRunLabel(); }
    pill?.classList.add("pill-pending");
    pill?.classList.remove("pill-running");
    if (pill) pill.textContent = "Idle";
  }
}

