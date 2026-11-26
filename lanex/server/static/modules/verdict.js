// verdict.js — the one vocabulary for how a run or a check came out.
//
// The state and the words are the server's (verdict.state and verdict.label
// on /api/runs, the same judgement Flow and Verify show); this module only
// says them, so the run picker, Runs and Compare never disagree. The words
// below stand in when a run row carries no label.
//
//   run:    Clean · Warnings · Failed · Stopped · Running · Incomplete
//   check:  Passed · Warning · Failed · Info · Off

import { escapeHtml } from "./html.js";

const RUN = {
  passed:     { cls: "pass", word: "Clean" },
  warning:    { cls: "warn", word: "Warnings" },
  failed:     { cls: "fail", word: "Failed" },
  stopped:    { cls: "warn", word: "Stopped" },
  incomplete: { cls: "warn", word: "Incomplete" },
  running:    { cls: "run",  word: "Running" },
};

const CHECK = {
  pass:    { cls: "pass", word: "Passed" },
  warn:    { cls: "warn", word: "Warning" },
  fail:    { cls: "fail", word: "Failed" },
  info:    { cls: "none", word: "Info" },
  skipped: { cls: "none", word: "Off" },
  absent:  { cls: "none", word: "Not run" },
};

const esc = escapeHtml;

// { state, cls, word } for a verdict state ("passed", "failed", …).
export function runVerdict(st) {
  const v = RUN[st];
  if (v) return { state: st, ...v };
  // A state this build does not know: amber, never a guessed pass or fail.
  return { state: st || "incomplete", cls: "warn", word: st ? st[0].toUpperCase() + st.slice(1) : "Incomplete" };
}

// The verdict of a run row from /api/runs. `live` marks the run going now.
export function verdictOfRun(r, { live = false } = {}) {
  if (live) return runVerdict("running");
  const st = r && r.verdict && r.verdict.state;
  if (st) {
    const v = runVerdict(st);
    // The server's own words win ("Clean", "Stopped after step 37").
    return r.verdict.label ? { ...v, word: String(r.verdict.label) } : v;
  }
  return runVerdict(r && r.success ? "passed" : "failed");
}

export function checkVerdict(status) {
  const v = CHECK[status] || CHECK.absent;
  return { state: status, ...v };
}

// The mark and the word, as Runs draws a result: <span class="rn-v is-pass"><i></i>Clean</span>
export function verdictHtml(v, { word = "" } = {}) {
  return "<span class='rn-v is-" + v.cls + "'><i aria-hidden='true'></i>" + esc(word || v.word) + "</span>";
}

export const RUN_STATES = Object.keys(RUN);
