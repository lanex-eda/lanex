// logs.js — the live output of the run this window follows, kept for Flow.
//
// Every line the run prints, with the step it belongs to (a step's lines
// follow its "Running" marker). Flow shows the running step's share in its log
// viewer as it arrives; a finished step's log is read from its file. Tool
// installs and other work outside a run go to the Logs panel (logdrawer.js).
//
// Two parallel arrays, not an object per line: a 200 000-line run keeps one
// string and one shared step-id reference per line. The newest MAX lines stay;
// the oldest fifth goes at once and is counted (droppedLines), so the viewer
// can say "Showing the last N" and read the rest from the run's log file.

const MAX = 200000;          // lines kept for one run
let texts = [];
let steps = [];
let dropped = 0;
let step = null;             // the step being written now
let pending = [];
let timer = 0;
const subs = new Set();

function flush() {
  timer = 0;
  if (!pending.length) return;
  const batch = pending;
  pending = [];
  for (const fn of subs) { try { fn(batch, { step }); } catch (e) { console.error("live log", e); } }
}

export const renderLogs = {
  append(event) {
    const msg = event.payload?.message ?? event.message;
    if (msg == null) return;
    for (const t of String(msg).replace(/\r/g, "").split("\n")) {
      texts.push(t);
      steps.push(step);
      pending.push({ t, step });
    }
    if (texts.length > MAX) {
      const drop = texts.length - Math.floor(MAX * 0.8);
      texts.splice(0, drop);
      steps.splice(0, drop);
      dropped += drop;
    }
    if (!timer) timer = setTimeout(flush, 120);
  },
  // A step started: what follows is its output.
  markStep(stepId) { if (stepId) step = stepId; },
  // A new run: start over.
  clear() { texts = []; steps = []; dropped = 0; step = null; pending = []; },
};

// The lines of one step (or of the whole run) received so far.
export function liveLines(stepId) {
  if (!stepId) return texts.slice();
  const out = [];
  for (let i = 0; i < texts.length; i++) if (steps[i] === stepId) out.push(texts[i]);
  return out;
}

// How many of the run's first lines this window no longer keeps.
export function droppedLines() { return dropped; }

// How many lines there are so far ({ length }, read only).
export function liveRows() { return { length: texts.length }; }

// fn(newLines, { step }) for each batch; returns an unsubscribe.
export function onLiveLog(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}
