// runmenu.js — the Run menu's What / How / When and its live COMMAND line.
//
// setup.js owns starting a run (onRunClick) and the stage pickers; this module
// adds the rest of the menu: steps to skip, threads, low priority, keep the PC
// awake, the command LanEx will run (asked from the server, the same builder
// the run records in gui-run.json), and the two ways to start (Run now / Add
// to queue). Reusable part: `argSpans(cmd, opts)` turns a shell command into
// one span per argument, so a long command wraps only between arguments.

import { api, fmt } from "./api.js";
import { state, safeStorage } from "./state.js";
import { toast } from "./toast.js";
import * as FS from "./flowstages.js";
import { closeLayer, isLayerOpen } from "./overlay.js";

const esc = (s) => fmt.escape(String(s == null ? "" : s));
const $ = (id) => document.getElementById(id);
const THREAD_VARS = /^(DRT_THREADS|KLAYOUT_[A-Z]+_THREADS|STA_THREADS)=/;

// ---------------------------------------------------------------- pure (tested)

// Split a POSIX shell command into its arguments (single and double quotes).
export function shellSplit(s) {
  const out = [];
  let cur = "", q = null, any = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q) {
      if (c === q) { q = null; continue; }
      if (q === '"' && c === "\\" && i + 1 < s.length) { cur += s[++i]; continue; }
      cur += c;
      continue;
    }
    if (c === "'" || c === '"') { q = c; any = true; continue; }
    if (c === " " || c === "\t") { if (cur || any) out.push(cur); cur = ""; any = false; continue; }
    if (c === "\\" && i + 1 < s.length) { cur += s[++i]; continue; }
    cur += c;
  }
  if (cur || any) out.push(cur);
  return out;
}

function quoteArg(a) {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(a) ? a : "'" + a.replace(/'/g, "'\\''") + "'";
}

// One span per argument: the program, flags, thread counts LanEx sized, and a
// placeholder for the run name LanEx picks at start.
export function argSpans(cmd, { tagPlaceholder = true, hot = [] } = {}) {
  const args = shellSplit(String(cmd || ""));
  if (!args.length) return "";
  const hasTag = args.includes("--run-tag");
  const parts = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    let cls = "a";
    if (i === 0) cls += " is-exe";
    else if (/^-/.test(a)) cls += " is-flag";
    if (i > 0 && args[i - 1] === "-c" && THREAD_VARS.test(a)) cls += " is-sized";
    if (hot.some((h) => a === h || a.startsWith(h + "="))) cls += " is-hot";
    const title = cls.includes("is-sized") ? " title='Sized to this PC and the runs going (Threads: Auto)'" : "";
    // A flag and its value stay together on a line.
    if (/^-/.test(a) && i + 1 < args.length && !/^-/.test(args[i + 1]) && a !== "--dockerized") {
      const v = args[i + 1];
      const vs = i + 1 > 0 && a === "-c" && THREAD_VARS.test(v);
      parts.push("<span class='" + cls + (vs ? " is-sized" : "") + "'" + (vs ? " title='Sized to this PC and the runs going (Threads: Auto)'" : title) + ">" +
        esc(a) + " " + esc(quoteArg(v)) + "</span>");
      i += 1;
      continue;
    }
    parts.push("<span class='" + cls + "'" + title + ">" + esc(quoteArg(a)) + "</span>");
  }
  if (tagPlaceholder && !hasTag) parts.push("<span class='a is-ph' title='LanEx names the run folder when it starts'>--run-tag ‹named at start›</span>");
  return parts.join("");
}

// The steps a person may skip: not the checkers (they only report).
export function skippable(stepIds) {
  return (stepIds || []).filter((id) => !/^Checker\./.test(id));
}

// ---------------------------------------------------------------- state of the menu

export function runMenuChoices() {
  const threads = $("run-threads")?.value || "auto";
  const skip = [...document.querySelectorAll("#run-skip-list input:checked")].map((b) => b.value);
  return {
    threads: threads === "auto" ? "auto" : Number(threads),
    priority: $("run-lowprio")?.checked ? "low" : "normal",
    keep_awake: $("run-keepawake") ? $("run-keepawake").checked : true,
    skip,
  };
}

function flowSteps() {
  const f = state.flowInfo;
  const known = f && f.dir === state.designDir && !f.error && f.steps && f.steps.length ? f.steps : null;
  if (known) return known;
  const name = (f && f.dir === state.designDir && f.name) || "Classic";
  const fl = (state.flows || []).find((x) => x.name === name);
  return (fl && fl.steps) || [];
}

function fillSkip() {
  const box = $("run-skip-list");
  if (!box) return;
  const keep = new Set([...box.querySelectorAll("input:checked")].map((b) => b.value));
  const ids = skippable(flowSteps());
  const stages = FS.stagesOf(ids.map((id) => ({ id })));
  box.innerHTML = ids.map((id, i) => "<label><input type='checkbox' value='" + esc(id) + "'" + (keep.has(id) ? " checked" : "") + "/> " +
    esc(id) + "<small>" + esc(FS.stageLabel(stages[i])) + "</small></label>").join("") ||
    "<span class='menu-note'>The flow's steps show here once the project's config is read.</span>";
  paintSkip();
}

function paintSkip() {
  const n = document.querySelectorAll("#run-skip-list input:checked").length;
  const s = $("run-skipsteps")?.querySelector("summary");
  if (s) s.textContent = n ? n + (n === 1 ? " step skipped" : " steps skipped") : "No steps skipped";
}

// ---------------------------------------------------------------- the command

let _cmdT = 0, _cmdTok = 0, _lastCmd = "";

function scheduleCommand(collect) {
  clearTimeout(_cmdT);
  _cmdT = setTimeout(() => paintCommand(collect), 150);
}

async function paintCommand(collect) {
  const el = $("run-cmd");
  if (!el || !isLayerOpen($("run-menu"))) return;
  if (!state.designDir) { el.innerHTML = "<span class='menu-note'>Open a project to see the command.</span>"; return; }
  const tok = ++_cmdTok;
  const p = Object.assign({}, collect(), runMenuChoices());
  const rsel = $("run-resume-from");
  const resumeTag = rsel && rsel.value ? rsel.dataset.tag : "";
  if (resumeTag) p.frm = rsel.value;
  let cmd = null;
  try { cmd = await api.cliCommand(p); } catch (e) {
    if (tok === _cmdTok) el.innerHTML = "<span class='menu-note'>Couldn't build the command: " + esc(e.message) + "</span>";
    return;
  }
  if (tok !== _cmdTok) return;
  const line = (cmd && (cmd[state.runMode === "local" ? "local" : "container"] || cmd.container)) || "";
  _lastCmd = line + (/--run-tag/.test(line) ? "" : "");
  let html = argSpans(line);
  if (resumeTag) html += "<span class='a is-ph' title='The saved state of " + esc(resumeTag) + " just before that stage'>-i ‹" + esc(resumeTag) + "'s results up to there›</span>";
  el.innerHTML = html;
  const note = $("run-cmd-note");
  if (note) {
    const res = (cmd && cmd.resources) || {};
    note.textContent = "The same run from a terminal in LanEx's Linux" +
      (res.reason ? " · threads for " + res.reason : "") + ".";
  }
}

// ---------------------------------------------------------------- slots

function paintSlot() {
  const el = $("run-slot");
  if (!el) return;
  import("./queue.js").then((q) => {
    const s = q.queueSnapshot();
    if (!s) { el.textContent = ""; return; }
    const mp = s.max_parallel || {};
    const n = s.running || 0;
    const full = n >= (mp.now || 1);
    el.textContent = full
      ? n + (n === 1 ? " run is" : " runs are") + " going, the most at once: a new run waits its turn"
      : n ? n + " going · room for " + ((mp.now || 1) - n) + " more" : "Nothing else is running";
    // With every slot taken, Add to queue is the button that does something now.
    $("run-now")?.classList.toggle("btn-primary", !full);
    $("run-now")?.classList.toggle("btn-ghost", full);
    $("run-queue")?.classList.toggle("btn-primary", full);
    $("run-queue")?.classList.toggle("btn-ghost", !full);
    const ka = $("run-keepawake");
    const hold = s.hold || {};
    if (ka && !ka.matches(":focus")) ka.checked = hold.enabled !== false;
    const kn = $("run-keepawake-note");
    if (kn) kn.textContent = (hold.note && hold.how !== "windows-launcher" ? hold.note + " " : "") +
      "Closing the lid or pressing the power button still puts it to sleep. On battery, plug in for long runs.";
  }).catch(() => {});
}

// ---------------------------------------------------------------- wiring

export function setupRunMenu({ collect, start }) {
  const menu = $("run-menu");
  if (!menu || menu._rmWired) return;
  menu._rmWired = true;
  const th = $("run-threads");
  if (th) {
    const saved = safeStorage.get("ll.runThreads", "auto") || "auto";
    if ([...th.options].some((o) => o.value === saved)) th.value = saved;
    th.addEventListener("change", () => { safeStorage.set("ll.runThreads", th.value); scheduleCommand(collect); });
  }
  const lp = $("run-lowprio");
  if (lp) {
    lp.checked = safeStorage.get("ll.runLowPrio") === "1";
    lp.addEventListener("change", () => safeStorage.set("ll.runLowPrio", lp.checked ? "1" : "0"));
  }
  const ka = $("run-keepawake");
  if (ka) ka.addEventListener("change", () => {
    api.queueSettings({ keep_awake: ka.checked }).then(() => import("./queue.js").then((q) => q.refreshQueue()))
      .catch((e) => toast.show(e.message, "error"));
  });
  $("run-skip-list")?.addEventListener("change", () => { paintSkip(); scheduleCommand(collect); });
  menu.addEventListener("change", (e) => {
    if (e.target.closest("#run-skip-list") || e.target.id === "run-threads") return;
    scheduleCommand(collect);
  });
  menu.addEventListener("click", (e) => {
    if (e.target.closest(".mode-btn, .engine-btn")) {
      // Radios: one checked in each group (the buttons keep their classes).
      setTimeout(() => {
        document.querySelectorAll("#run-menu .mode-btn, #run-menu .engine-btn").forEach((b) =>
          b.setAttribute("aria-checked", b.classList.contains("mode-btn-active") || b.classList.contains("engine-btn-active") ? "true" : "false"));
        scheduleCommand(collect);
      }, 0);
    }
  });
  $("run-cmd-copy")?.addEventListener("click", async (e) => {
    const b = e.currentTarget;
    const text = _lastCmd || $("run-cmd")?.textContent || "";
    try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; } catch (_e) { b.textContent = "Copy failed"; }
    setTimeout(() => { b.textContent = "Copy"; }, 1400);
  });
  const go = (when) => { closeLayer(menu); start(when); };
  $("run-now")?.addEventListener("click", () => go("now"));
  $("run-queue")?.addEventListener("click", () => go("queue"));
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(e.shiftKey ? "queue" : "now"); }
  });
  $("run-opts-btn")?.addEventListener("click", () => {
    setTimeout(() => { fillSkip(); paintSlot(); scheduleCommand(collect); }, 0);
  });
  document.addEventListener("g:flow_known", fillSkip);
  document.addEventListener("g:queue_state", () => { if (isLayerOpen(menu)) paintSlot(); });
}
