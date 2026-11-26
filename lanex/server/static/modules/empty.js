// empty.js — one empty state for every page (consistency plan 1.6): an icon,
// a title, one sentence and at most one button, the block Cells drew first.
// The titles and buttons come from one word table, so "no project" and "no
// runs" read the same on Flow, Runs, Compare, Analytics, Verify, Layout and
// the IDE:
//
//   no project   "Open a project first"        [Go to Setup]
//   no runs      "No runs yet"                  [Run the flow] (or Go to Setup)
//   run gone     "This run's folder is gone"    [Show the runs]
//
//   el.innerHTML = emptyHtml({ icon: "chart", title, body, action: { label, go } });
//   el.innerHTML = pageEmpty(state.designDir ? "noRuns" : "noProject", { icon: "chart", body: { noRuns: "…", noProject: "…" } });
//
// A results page also turns its run-bar actions off while it is empty
// (setRunBarActions): there is no run for Export or Settings used to act on.

import { escapeHtml } from "./html.js";
import { icon as iconSvg } from "./icons.js";

const esc = escapeHtml;

export const EMPTY_WORDS = {
  noProject: { title: "Open a project first", action: { label: "Go to Setup", go: "setup" } },
  noRuns: { title: "No runs yet", action: { label: "Run the flow", run: true } },
  runGone: { title: "This run's folder is gone", action: { label: "Show the runs", go: "runs" } },
};

// action: { label, go } (a page), { label, run: true } (the Run button), or
// { label, attrs } (the caller wires it). A run action falls back to Setup
// while Run can't start (no project, or its folder is missing).
// What goes inside an .empty block (for a block that is already one).
export function emptyInner({ icon = "", title = "", body = "", action = null } = {}) {
  let btn = "";
  if (action) {
    const canRun = action.run && runPossible();
    if (action.run && !canRun) action = EMPTY_WORDS.noProject.action;
    const attrs = action.run ? "data-empty-run" : action.go ? "data-go='" + esc(action.go) + "'" : (action.attrs || "");
    btn = "<button class='btn btn-primary' " + attrs + ">" + (action.run ? iconSvg("play", { size: 14 }) + " " : "") + esc(action.label) + "</button>";
  }
  return (icon ? "<span class='ico'>" + iconSvg(icon, { size: 40 }) + "</span>" : "") +
    "<h3>" + esc(title) + "</h3>" + (body ? "<p>" + esc(body) + "</p>" : "") + btn;
}
export function emptyHtml(opts = {}) {
  return "<div class='empty" + (opts.cls ? " " + opts.cls : "") + "'>" + emptyInner(opts) + "</div>";
}

// The empty state of a page, from the word table. `body`: a sentence, or one
// per kind ({ noProject, noRuns, runGone }). `inner`: without the .empty box.
export function pageEmpty(kind, { icon = "", body = "", inner = false } = {}) {
  const w = EMPTY_WORDS[kind] || EMPTY_WORDS.noRuns;
  const b = typeof body === "string" ? body : (body && body[kind]) || "";
  const o = { icon, title: w.title, body: b, action: w.action };
  return inner ? emptyInner(o) : emptyHtml(o);
}

// Fill a static empty block (#an-empty, #vf-empty, #lx-empty) and show it.
export function showEmpty(el, kind, opts = {}) {
  if (!el) return;
  const key = kind + "|" + opts.icon + "|" + runPossible();
  if (el._emptyKey !== key) { el.innerHTML = pageEmpty(kind, { ...opts, inner: true }); el._emptyKey = key; }
  el.hidden = false;
}

// The run-bar buttons of a results page (Export, Settings used, CSV, Refresh,
// 3D view, Open in…) act on the run in view: off while there is none. The run
// pickers turn themselves off (runbar.js).
export function setRunBarActions(bar, on) {
  if (!bar) return;
  for (const b of bar.querySelectorAll(".btn")) {
    if (b.closest(".rb")) continue;
    if (on) {
      if (b.dataset.emptyOff === "1") { b.disabled = false; delete b.dataset.emptyOff; }
    } else if (!b.disabled) {
      b.disabled = true; b.dataset.emptyOff = "1";
    }
  }
  bar.classList.toggle("is-empty", !on);
}

function runPossible() {
  if (typeof document === "undefined") return false;
  const g = document.getElementById("btn-run-global");
  return !!(g && !g.disabled && !g.classList.contains("is-stop"));
}

// One listener for every "Run the flow" an empty state draws: the Run button's own click.
if (typeof document !== "undefined" && !document._emptyRunWired) {
  document._emptyRunWired = true;
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-empty-run]");
    if (!b) return;
    const g = document.getElementById("btn-run-global");
    if (g && !g.disabled) g.click();
  });
}
