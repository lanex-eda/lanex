// studyqueue.js — the one place studies (sweeps) meet the job queue (lane 1,
// controller/jobs.py). Everything a study page needs from the queue goes
// through here, so the merge wires one file:
//
//   jobsOf(group)          the queue jobs of a study ({tag -> job}), as last heard
//   onStudyJobs(fn)        fn(group) whenever a job of some study changes
//   stopAfterRunning(dir, group)   queued points become Skipped; running ones finish
//   stopNow(group)         every running point of the study is stopped too
//   queueAvailable()       true once the server answered GET /api/queue
//
// Contract (jobs.py docstring): a study's jobs have `group` = the sweep/study
// id and `kind` "point"; `queue:job` events carry {job}; `queue:changed`
// carries the snapshot {jobs}. POST /api/queue/remove {group} drops the
// waiting jobs of a group; POST /api/queue/stop {id} stops one. Without the
// queue (before lane 1 merges) every call says so and the page falls back to
// the sweep's own status (/api/dse/status).

import { api, sse } from "./api.js";

const _jobs = new Map();       // group -> Map(tag or id -> job)
const _subs = new Set();
let _wired = false, _available = null;

function note(job) {
  if (!job || !job.group) return;
  const g = _jobs.get(job.group) || new Map();
  g.set(job.tag || job.id, job);
  _jobs.set(job.group, g);
}
function wire() {
  if (_wired || typeof document === "undefined") return;
  _wired = true;
  sse.on((ev) => {
    if (!ev || !ev.type) return;
    if (ev.type === "queue:job") {
      const job = ev.job || (ev.data && ev.data.job);
      if (job && job.group) { note(job); for (const fn of _subs) fn(job.group); }
    } else if (ev.type === "queue:changed") {
      const jobs = ev.jobs || (ev.data && ev.data.jobs) || [];
      const groups = new Set();
      for (const j of jobs) if (j.group) { note(j); groups.add(j.group); }
      for (const g of groups) for (const fn of _subs) fn(g);
    }
  });
}

export function onStudyJobs(fn) { wire(); _subs.add(fn); return () => _subs.delete(fn); }

// The study's jobs, keyed by run tag (or job id before the tag is known).
export function jobsOf(group) { wire(); return _jobs.get(group) || new Map(); }

// Ask the queue once (and again after a page reload): which study jobs exist.
export async function refreshQueue() {
  wire();
  try {
    const snap = await fetchQueue();
    _available = true;
    for (const j of (snap && snap.jobs) || []) note(j);
  } catch (_e) { _available = false; }
  return _available;
}
export function queueAvailable() { return _available; }

async function fetchQueue() {
  const r = await fetch("/api/queue", { headers: { "X-Requested-With": "XMLHttpRequest" } });
  if (!r.ok) throw new Error("no queue");
  const b = await r.json();
  return b && b.data !== undefined ? b.data : b;
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" }, body: JSON.stringify(body) });
  const b = await r.json().catch(() => ({}));
  if (!r.ok || b.ok === false) { const e = new Error(b.error || "HTTP " + r.status); e.status = r.status; throw e; }
  return b.data !== undefined ? b.data : b;
}

// "Stop after the running points" (W&B's Pause): the waiting points of the
// study leave the queue (Skipped), the running ones finish.
export async function stopAfterRunning(group) {
  if (_available === false) throw new Error("the job queue isn't in this LanEx yet");
  return post("/api/queue/remove", { group });
}
// "Stop now": the waiting points leave the queue and the running ones stop.
export async function stopNow(group) {
  await stopAfterRunning(group);
  const running = [...jobsOf(group).values()].filter((j) => /running|starting|paused/.test(j.state || ""));
  for (const j of running) await post("/api/queue/stop", { id: j.id });
  return running.length;
}

// For the sweep sheet: a study's points go to the queue as one group (the
// server's /api/dse/start does this after lane 1; here for a direct caller).
export async function addPoints(designDir, group, specs, label = "") {
  return post("/api/queue/add", { design_dir: designDir, specs, group, kind: "point", label });
}

export const _test = { note, jobsOf };
// The sweep status the page used before the queue: kept for the fallback.
export const dseStatus = () => api.dseStatus();
