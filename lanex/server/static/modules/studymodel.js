// studymodel.js — a study's (sweep's) points and their states, in plain
// words, the same everywhere a point shows: the Runs group row's strip, the
// summary's table and charts, the CSV. Pure (no DOM): node tests import it.
//
// A point is one set of swept values. It exists before it runs (Queued), and
// ends in one of:
//   Clean · Warnings · Timing missed (finished, has numbers, a clock too fast:
//   plotted, never counted as a failure) · Failed at <stage> (no numbers) ·
//   Stopped (you) · Stopped early (the study's own rule) · Skipped (never ran,
//   with why) · Incomplete (LanEx closed while it ran).
// The state comes from the queue job while there is one (lane 1's jobs:
// queued, running, skipped, stopped, interrupted), else from the run's
// verdict and numbers.

export const POINT = {
  queued: { word: "Queued", tone: "muted", mark: "hollow" },
  running: { word: "Running", tone: "live", mark: "pulse" },
  clean: { word: "Clean", tone: "pass", mark: "fill" },
  warnings: { word: "Warnings", tone: "warn", mark: "fill" },
  missed: { word: "Timing missed", tone: "warn", mark: "bang" },
  failed: { word: "Failed", tone: "fail", mark: "cross" },
  stopped: { word: "Stopped", tone: "warn", mark: "half" },
  pruned: { word: "Stopped early", tone: "warn", mark: "half" },
  skipped: { word: "Skipped", tone: "muted", mark: "dash" },
  interrupted: { word: "Incomplete", tone: "warn", mark: "half" },
};

const fin = (v) => typeof v === "number" && Number.isFinite(v) && Math.abs(v) < 1e20;

// The state key of a point. `run`: its run row (verdict, key_metrics), or
// null; `job`: its queue job, or null; `active`: the study is still going.
export function pointKey({ run = null, job = null, active = false } = {}) {
  const js = job && job.state;
  if (js === "queued" || js === "starting") return "queued";
  if (js === "running" || js === "stopping" || js === "paused") return "running";
  if (js === "skipped") return "skipped";
  if (js === "stopped" && !run) return job.pruned ? "pruned" : "stopped";
  if (js === "interrupted") return "interrupted";
  if (!run) return active ? "queued" : "skipped";
  const v = run.verdict || {};
  const st = v.state || (run.success ? "passed" : "failed");
  const m = run.key_metrics || {};
  const setup = m.timing__setup__ws, hold = m.timing__hold__ws;
  const missed = (fin(setup) && setup < 0) || (fin(hold) && hold < 0);
  if (st === "running") return "running";
  if (st === "passed") return missed ? "missed" : "clean";
  if (st === "warning") return v.timing_miss || missed ? "missed" : "warnings";
  // LibreLane raises a timing violation after the flow has finished (a
  // deferred checker): the run "failed" but has every number. That is a
  // result, not a failure.
  // Only a run whose every step finished: one that died mid-flow can carry a
  // pre-layout slack that says nothing about the clock.
  const allDone = run.step_count > 0 && run.steps_done >= run.step_count && !run.steps_failed;
  if (st === "failed") return missed && allDone ? "missed" : "failed";
  if (st === "stopped") return job && job.pruned ? "pruned" : "stopped";
  if (st === "incomplete") return v.no_clock ? "failed" : "interrupted";
  return "failed";
}

// The words for a point: the state word ("Failed at Placement") and a
// muted detail line.
export function pointWords(key, { run = null, job = null, stageLabel = (s) => s, code = "", reason = "" } = {}) {
  const P = POINT[key] || POINT.failed;
  let word = P.word, detail = "";
  const v = (run && run.verdict) || {};
  const m = (run && run.key_metrics) || {};
  if (key === "failed" && v.stage) word = "Failed at " + stageLabel(v.stage);
  if (key === "failed" && v.no_clock) word = "Not timed: no clock";
  if (key === "failed" && code && !word.endsWith(code)) detail = code;
  if (key === "running") {
    const s = job && job.step;
    if (s && s.total) detail = "step " + (s.index || 0) + " of " + s.total;
  }
  if (key === "missed") {
    const ws = m.timing__setup__ws, hs = m.timing__hold__ws;
    detail = fin(ws) && ws < 0 ? "setup " + ws.toFixed(2) + " ns" : fin(hs) && hs < 0 ? "hold " + hs.toFixed(2) + " ns" : "";
  }
  if (key === "queued" && job && job.reason) detail = job.reason;
  if ((key === "skipped" || key === "pruned") && (reason || (job && job.reason))) detail = reason || job.reason;
  if (key === "clean" && fin(m.timing__setup__ws)) detail = (m.timing__setup__ws >= 0 ? "+" : "") + m.timing__setup__ws.toFixed(2) + " ns slack";
  return { word, detail, tone: P.tone, mark: P.mark };
}

// "Met timing": finished, setup and hold slack not negative.
export function metTiming(key, run) {
  if (!run || !(key === "clean" || key === "warnings")) return false;
  const m = run.key_metrics || {};
  return fin(m.timing__setup__ws) && m.timing__setup__ws >= 0 && !(fin(m.timing__hold__ws) && m.timing__hold__ws < 0);
}
// Points that have numbers to plot (finished, or finished but too fast).
export const hasNumbers = (key) => key === "clean" || key === "warnings" || key === "missed";

// The failure's fingerprint: the step and the tool's code (GRT-0116), or
// the first error words with numbers and paths taken out. Two points with
// the same fingerprint failed the same way.
export function fingerprint(text, stage = "") {
  const s = String(text || "");
  const code = /\b([A-Z]{2,4}-\d{4})\b/.exec(s);
  if (code) return (stage || "") + "|" + code[1];
  const line = (s.split("\n").find((l) => /error/i.test(l)) || s.split("\n")[0] || "").toLowerCase()
    .replace(/\/[^\s'"`]+/g, "<path>").replace(/[`'"][^`'"]*[`'"]/g, "<name>").replace(/\d+(\.\d+)?/g, "<n>").trim();
  return line ? (stage || "") + "|" + line.slice(0, 120) : "";
}

// Fail fast, the evidence rule: two of the first three points that ended
// failed with the same fingerprint (never "Timing missed": a clock too fast
// is a result). -> { fingerprint, points: [tag, tag] } or null.
export function sameFailure(ended) {
  const first = ended.slice(0, 3).filter((p) => p.key === "failed" && p.fp);
  const by = new Map();
  for (const p of first) {
    const l = by.get(p.fp) || [];
    l.push(p.tag);
    by.set(p.fp, l);
    if (l.length >= 2) return { fingerprint: p.fp, points: l };
  }
  return null;
}

// "8 ns (125 MHz)": a clock period with its frequency.
export function clockText(ns) {
  const n = Number(ns);
  if (!(n > 0)) return String(ns);
  const mhz = 1000 / n;
  return n + " ns (" + (mhz >= 100 ? Math.round(mhz) : mhz.toPrecision(3)) + " MHz)";
}

// Strip of squares, one per point in value order: the mark says the state,
// so colour is never the only signal.
export function stripHtml(points, esc = (s) => String(s)) {
  const many = points.length > 24;
  if (many) {
    const n = {};
    for (const p of points) n[p.key] = (n[p.key] || 0) + 1;
    return "<span class='sv-strip is-bar' role='img' aria-label='" + esc(Object.entries(n).map(([k, c]) => c + " " + POINT[k].word.toLowerCase()).join(", ")) + "'>" +
      Object.entries(n).map(([k, c]) => "<i class='sv-seg is-" + POINT[k].tone + "' style='flex:" + c + "' title='" + esc(c + " " + POINT[k].word.toLowerCase()) + "'></i>").join("") + "</span>";
  }
  return "<span class='sv-strip' role='img' aria-label='" + esc(points.map((p) => (p.label || p.tag) + ": " + POINT[p.key].word).join("; ")) + "'>" +
    points.map((p) => "<i class='sv-pt is-" + POINT[p.key].tone + " m-" + POINT[p.key].mark + "' title='" + esc((p.label || p.tag) + ": " + (p.word || POINT[p.key].word) + (p.detail ? ", " + p.detail : "")) + "'></i>").join("") + "</span>";
}
