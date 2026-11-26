// studyqueue.js — a study's (sweep's) points in the job queue (lane 1,
// controller/jobs.py; its docstring is the contract). Thin: the queue owns
// every job; this only reads a study's jobs and asks the queue to stop them.
//
//   jobsOf(group)               Map(planned run tag -> its job)
//   onStudyJobs(fn)             fn(group) when a job of a study changes
//   stopAfterRunning(group)     waiting points are Skipped (kept in the queue), running ones finish
//   stopNow(group)              the sweep's own Stop: waiting points Skipped, running ones stop
// Both go through the sweep (POST /api/dse/cancel), so its end is recorded as
// stopped by you; taking the points out of the queue made the sweep read
// them as finished points with no folder, and stop as "failed fast".
//   queueAvailable()            the queue answered at least once
//
// A sweep's points are queue jobs with `kind` "point" and `group` = the sweep
// id; a waiting job carries its planned tag in `spec.tag` (the run folder is
// reserved when it starts). Events: app.js turns the bus's `queue:changed`
// (the snapshot) and `queue:job` ({job}) into the page's `g:queue` event.

import { api } from "./api.js";

const _jobs = new Map();       // group -> Map(tag -> job)
const _subs = new Set();
let _wired = false, _available = null;

const tagOf = (j) => j.tag || (j.spec && j.spec.tag) || j.id;
function note(job) {
  if (!job || !job.group) return null;
  const g = _jobs.get(job.group) || new Map();
  g.set(tagOf(job), job);
  _jobs.set(job.group, g);
  return job.group;
}
function wire() {
  if (_wired || typeof document === "undefined") return;
  _wired = true;
  document.addEventListener("g:queue", (e) => {
    const ev = e.detail || {};
    const groups = new Set();
    if (ev.type === "queue:job") { const g = note(ev.job); if (g) groups.add(g); }
    else if (ev.type === "queue:changed") { _available = true; for (const j of ev.jobs || []) { const g = note(j); if (g) groups.add(g); } }
    for (const g of groups) for (const fn of _subs) fn(g);
  });
  refreshQueue();
}

export function onStudyJobs(fn) { wire(); _subs.add(fn); return () => _subs.delete(fn); }
export function jobsOf(group) { wire(); return _jobs.get(group) || new Map(); }
export function queueAvailable() { wire(); return _available; }

export async function refreshQueue() {
  try {
    const snap = await api.queue();
    _available = true;
    for (const j of (snap && snap.jobs) || []) note(j);
  } catch (_e) { _available = false; }
  return _available;
}

// W&B's Pause: the waiting points are skipped, the running ones finish.
export async function stopAfterRunning(group) {
  return api.dseCancel(stopBody(group, true));
}
// Stop now: the waiting points are skipped and the running ones stop.
export async function stopNow(group) {
  const res = await api.dseCancel(stopBody(group, false));
  return ((res && res.stopped) || []).length;
}
export function stopBody(group, afterRunning) {
  return { sweep_id: String(group || ""), after_running: !!afterRunning };
}

export const _test = { note, jobsOf, tagOf, stopBody };
