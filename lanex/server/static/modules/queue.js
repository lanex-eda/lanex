// queue.js — the run queue in the top bar: a chip left of Run on every page,
// and its popover (what runs, what waits and why, runs at once, pause, stop
// after the runs going now, reorder, remove, continue an interrupted run).
//
// The server's job queue (controller/jobs.py) is the truth: GET /api/queue and
// the queue:changed / queue:job events. Nothing here decides what runs.
//
// Reusable parts: `segBar(stages, done)` (a stage-segmented progress bar) and
// `etaWords(s)` (the one rounding for time left, the server's eta_words twin).

import { api, fmt } from "./api.js";
import { state } from "./state.js";
import { openLayer, closeLayer, isLayerOpen } from "./overlay.js";
import { toast } from "./toast.js";
import * as FS from "./flowstages.js";
import { followJob } from "./runfollow.js";

const esc = (s) => fmt.escape(String(s == null ? "" : s));
let snap = null;          // the last /api/queue answer
let _paintT = 0;
let _seenFail = new Set();   // ended jobs whose failure the popover has shown
let _announced = new Map();  // job id -> state last announced

// ---------------------------------------------------------------- pure (tested)

// "about 12 min" / "about 1 h 20" / "less than a minute"; "" when unknown.
// Rounded so it never ticks (the server's eta_words).
export function etaWords(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const s = Math.max(0, Number(seconds));
  if (s < 60) return "less than a minute";
  const m = s / 60;
  if (m < 10) return "about " + Math.round(m) + " min";
  if (m < 60) return "about " + Math.round(m / 5) * 5 + " min";
  const total = Math.round(m / 10) * 10;
  const h = Math.floor(total / 60), mm = total % 60;
  return "about " + h + " h" + (mm ? " " + String(mm).padStart(2, "0") : "");
}

const isLive = (j) => j && (j.state === "running" || j.state === "starting" || j.state === "stopping");

// What the chip says: { hidden, text, short, tone: run|warn|fail|idle, title }.
export function chipText(q, { seenFail = new Set() } = {}) {
  const out = { hidden: true, text: "", short: "", tone: "idle", title: "" };
  if (!q) return out;
  const jobs = q.jobs || [];
  const running = jobs.filter(isLive);
  const waiting = jobs.filter((j) => j.state === "queued");
  const paused = jobs.filter((j) => j.state === "paused");
  const interrupted = jobs.filter((j) => j.state === "interrupted");
  const failed = jobs.filter((j) => j.state === "failed" && !seenFail.has(j.id));
  const orphans = q.orphans || [];
  if (!running.length && !waiting.length && !paused.length && !interrupted.length && !failed.length && !orphans.length) return out;
  out.hidden = false;
  if (interrupted.length) {
    out.tone = "warn";
    out.text = interrupted.length + (interrupted.length === 1 ? " run interrupted" : " runs interrupted");
    out.short = interrupted.length + " interrupted";
    out.title = "LanEx stopped while " + (interrupted.length === 1 ? "a run was" : "runs were") + " going. Open to continue or mark as stopped.";
    if (running.length) out.text = "Running " + running.length + " · " + out.text;
    return out;
  }
  if (!running.length && !waiting.length && paused.length) {
    const p = paused[0];
    const nxt = /next: ([^.]+)/.exec(p.reason || "");
    out.tone = "run";
    out.text = (p.project || "Run") + " paused" + (nxt ? " · next: " + nxt[1] : "");
    out.short = "Paused";
    out.title = p.reason || "";
    return out;
  }
  if (running.length) {
    out.tone = "run";
    const left = (q.eta && q.eta.known) ? etaWords(q.eta.seconds) : "";
    if (running.length === 1 && !waiting.length) {
      const j = running[0];
      const st = j.step || {};
      const bits = ["Running", j.project || ""];
      if (j.remaining_s != null) bits.push(etaWords(j.remaining_s) + " left");
      else if (j.slow) bits.push("taking longer than last time");
      else if (st.total) bits.push("step " + ((st.index || 0) + 1) + " of " + st.total);
      out.text = bits.filter(Boolean).join(" · ");
      out.short = "Running · " + (j.project || "");
    } else {
      out.text = "Running " + running.length + (waiting.length ? " · " + waiting.length + " queued" : "") + (left ? " · " + left + " left" : "");
      out.short = "Running " + running.length + (waiting.length ? " · " + waiting.length + " queued" : "");
      const unknown = (q.eta && q.eta.unknown_count) || 0;
      if (!left && unknown) out.title = unknown + " of the " + (running.length + waiting.length) + " have no estimate yet.";
    }
    if (q.terminal) out.text += " · " + q.terminal + " from a terminal";
    if (failed.length) out.tone = "fail";
    return out;
  }
  if (waiting.length) {
    const why = waiting[0].reason || "";
    out.tone = "idle";
    out.text = q.paused ? "Queue paused · " + waiting.length + " waiting"
      : waiting.length + " queued" + (/memory/i.test(why) ? " · waiting for memory" : "");
    out.short = waiting.length + " queued";
    out.title = why;
    return out;
  }
  if (orphans.length) {
    out.tone = "warn";
    out.text = orphans.length === 1 ? "A run container is still going" : orphans.length + " run containers are still going";
    out.short = "Check";
    return out;
  }
  const ended = jobs.filter((j) => j.state === "done" || j.state === "failed");
  out.tone = "fail";
  out.text = "Queue done · " + ended.filter((j) => j.state === "done").length + " finished · " + failed.length + " failed";
  out.short = failed.length + " failed";
  return out;
}

// Stage segments of a flow: [{ id, label, n }] for the steps in order.
export function stageSegments(stepIds) {
  const stages = FS.stagesOf((stepIds || []).map((id) => ({ id })));
  const out = [];
  stages.forEach((sid) => {
    const last = out[out.length - 1];
    if (last && last.id === sid) last.n += 1;
    else out.push({ id: sid, label: FS.stageLabel(sid), n: 1 });
  });
  return out;
}

// A stage-segmented progress bar (HTML). `done` steps are over, the next one is
// running. Segments are sized by their share of the steps.
export function segBar(segs, done, { label = "" } = {}) {
  const total = segs.reduce((a, s) => a + s.n, 0) || 1;
  let left = Math.max(0, done || 0);
  const cells = segs.map((s) => {
    const fill = Math.max(0, Math.min(s.n, left));
    left -= s.n;
    const cur = fill < s.n && fill >= 0 && left < 0 && (left + s.n) >= 0 && (done || 0) < total;
    const pct = Math.round((fill / s.n) * 100);
    return "<span class='q-seg" + (fill >= s.n ? " is-done" : cur ? " is-now" : "") + "' style='flex:" + s.n +
      "' title='" + esc(s.label + ": " + fill + " of " + s.n + " steps") + "'><i style='width:" + pct + "%'></i></span>";
  });
  return "<span class='q-bar' role='progressbar' aria-valuemin='0' aria-valuemax='" + total + "' aria-valuenow='" + (done || 0) +
    "' aria-valuetext='" + esc(label) + "'>" + cells.join("") + "</span>";
}

function stepsOf(job) {
  const name = (job.spec && job.spec.flow) || "Classic";
  const f = (state.flows || []).find((x) => x.name === name) || (state.flows || []).find((x) => x.name === "Classic");
  return (f && f.steps) || [];
}

// ---------------------------------------------------------------- the chip

function chip() { return document.getElementById("queue-chip"); }
function pop() { return document.getElementById("queue-pop"); }

function paintChip() {
  const c = chip();
  if (!c) return;
  const t = chipText(snap, { seenFail: _seenFail });
  c.hidden = t.hidden;
  if (t.hidden) return;
  c.dataset.tone = t.tone;
  c.querySelector(".q-text").textContent = t.text;
  c.querySelector(".q-short").textContent = t.short || t.text;
  // No tooltip over the open popover (it would sit on its header).
  if (isLayerOpen(pop())) delete c.dataset.tip;
  else c.dataset.tip = (t.title ? t.title + " · " : "") + t.text;
  c.setAttribute("aria-label", t.text + ". Open the run queue");
}

function announce() {
  const live = document.getElementById("queue-live");
  if (!live || !snap) return;
  const said = [];
  for (const j of snap.jobs || []) {
    const was = _announced.get(j.id);
    if (was === j.state) continue;
    _announced.set(j.id, j.state);
    if (was === undefined) continue;
    if (j.state === "running" && was === "queued") said.push((j.project || "A run") + " started");
    if (j.state === "done") said.push((j.project || "A run") + " finished");
    if (j.state === "failed") said.push((j.project || "A run") + " failed");
  }
  if (said.length) live.textContent = said.join(". ") + ".";
}

function schedulePaint() {
  if (_paintT) return;
  _paintT = setTimeout(() => {
    _paintT = 0;
    paintChip();
    announce();
    if (isLayerOpen(pop())) renderPop();
    document.dispatchEvent(new CustomEvent("g:queue_state", { detail: snap }));
  }, 250);
}

export function queueSnapshot() { return snap; }

// Is there a free slot for one more run now? (The Run button says "Add to queue" when not.)
export function slotFree() {
  if (!snap) return true;
  const mp = snap.max_parallel || {};
  return (snap.running || 0) < (mp.now || 1);
}

export async function refreshQueue() {
  try { snap = await api.queue(); } catch (_e) { return snap; }
  schedulePaint();
  return snap;
}

// ---------------------------------------------------------------- the popover

function rowRunning(j) {
  const st = j.step || {};
  const steps = stepsOf(j);
  const segs = stageSegments(steps);
  const done = st.done != null ? st.done : (st.index || 0);
  const stg = st.id ? FS.stageLabel(FS.stagesOf([{ id: st.id }])[0]) : "";
  const left = j.remaining_s != null ? etaWords(j.remaining_s) + " left" : j.slow ? "taking longer than last time" : "";
  const total = st.total || steps.length || 0;
  const where = [st.id ? (stg ? stg + " › " : "") + esc(st.id) : (j.state === "starting" ? "Starting…" : esc(j.reason || ""))];
  const count = total ? "step " + Math.min(total, (st.index || 0) + 1) + " of " + total : "";
  const term = j.origin === "terminal";
  return "<li class='q-row is-run' data-id='" + esc(j.id) + "'>" +
    "<div class='q-row-top'><span class='rn-v is-run'><i></i>" + esc(j.project || "") + "</span>" +
      "<span class='q-tag'>" + esc(j.tag || "") + "</span>" + (term ? "<span class='q-badge'>terminal</span>" : "") +
      "<span class='q-where'>" + where.join("") + "</span></div>" +
    (segs.length ? segBar(segs, done, { label: count + (stg ? ", " + stg : "") }) : "") +
    "<div class='q-row-meta'><span>" + esc([count, left].filter(Boolean).join(" · ")) + "</span>" +
      "<span class='q-acts'><button class='btn btn-ghost btn-xs' data-q='show'>Show</button>" +
      (term && !j.container ? "" : "<button class='btn btn-ghost btn-xs q-stop' data-q='stop'>" + (j.state === "stopping" ? "Stopping…" : "Stop") + "</button>") +
      "</span></div>" +
    (term && !j.container ? "<p class='q-note'>Started in a terminal: stop it there.</p>" : "") +
  "</li>";
}

function rowPaused(j) {
  return "<li class='q-row is-paused' data-id='" + esc(j.id) + "'>" +
    "<div class='q-row-top'><span class='rn-v'><i></i>" + esc(j.project || "") + "</span><span class='q-tag'>" + esc(j.tag || "") + "</span></div>" +
    "<p class='q-note'>" + esc(j.reason || "Paused between steps.") + "</p>" +
    "<div class='q-row-meta'><span></span><span class='q-acts'>" +
      "<button class='btn btn-ghost btn-xs' data-q='show'>Show</button>" +
      "<button class='btn btn-primary btn-xs' data-q='next'>Next step</button>" +
      "<button class='btn btn-ghost btn-xs' data-q='stop'>Stop</button></span></div></li>";
}

function rowWaiting(j, i, multi) {
  const est = j.estimate && j.estimate.seconds ? etaWords(j.estimate.seconds) : "no estimate yet";
  const why = j.reason && !/^Waiting its turn/.test(j.reason) ? j.reason : "";
  return "<li class='q-row is-wait' data-id='" + esc(j.id) + "' tabindex='0' aria-label='" + esc((i + 1) + ". " + (j.project || "") + " " + (j.label || "")) + "'>" +
    "<div class='q-row-top'><span class='q-n'>" + (i + 1) + "</span>" +
      "<span class='q-lbl' title='" + esc((j.project || "") + " · " + (j.label || "Run")) + "'>" + (multi ? "<b>" + esc(j.project || "") + "</b> · " : "") + esc(j.label || "Run") + "</span>" +
      "<span class='q-est' title='" + esc((j.estimate && j.estimate.basis) ? "Based on " + j.estimate.basis + "." : "") + "'>" + esc(est) + "</span>" +
      "<span class='q-acts q-move'>" +
        "<button class='iconbtn iconbtn-sm' data-q='up' aria-label='Move up' title='Move up (Alt+↑)'>↑</button>" +
        "<button class='iconbtn iconbtn-sm' data-q='down' aria-label='Move down' title='Move down (Alt+↓)'>↓</button>" +
        "<button class='iconbtn iconbtn-sm' data-q='remove' aria-label='Remove from the queue' title='Remove (Delete)'>✕</button>" +
      "</span></div>" +
    (why ? "<p class='q-note'>" + esc(why) + "</p>" : "") +
  "</li>";
}

function rowInterrupted(j) {
  const r = j.resume || {};
  const at = r.from_step || "";
  const can = !!r.can_continue;
  return "<li class='q-row is-int' data-id='" + esc(j.id) + "'>" +
    "<div class='q-row-top'><span class='rn-v is-warn'><i></i>" + esc(j.project || "") + "</span><span class='q-tag'>" + esc(j.tag || "") + "</span></div>" +
    "<p class='q-note'>" + esc(j.reason || "LanEx stopped while this run was going.") + (r.reason && !can ? " " + esc(r.reason) : "") + "</p>" +
    "<div class='q-row-meta'><span></span><span class='q-acts'>" +
      (can ? "<button class='btn btn-primary btn-xs' data-q='continue'>Continue from " + esc(at) + "</button>" : "") +
      "<button class='btn btn-ghost btn-xs' data-q='markstop'>Mark as stopped</button></span></div>" +
    (can ? "<p class='q-hint'>Continue reruns " + esc(at) + " and the steps after it, in this same run folder. Mark as stopped keeps the finished steps.</p>" : "") +
  "</li>";
}

function rowEnded(j) {
  const tone = j.state === "done" ? (j.outcome && j.outcome.ok === false ? "is-warn" : "is-pass") : j.state === "failed" ? "is-fail" : "";
  const word = j.state === "done" ? "Finished" : j.state === "failed" ? "Failed" : j.state === "skipped" ? "Not run" : "Stopped";
  return "<li class='q-row is-end' data-id='" + esc(j.id) + "'>" +
    "<div class='q-row-top'><span class='rn-v " + tone + "'><i></i>" + word + "</span>" +
    "<span class='q-lbl'>" + esc(j.project || "") + " · " + esc(j.tag || j.label || "") + "</span>" +
    (j.tag && j.state !== "skipped" ? "<span class='q-acts'><button class='btn btn-ghost btn-xs' data-q='show'>Show</button></span>" : "") + "</div>" +
    (j.state !== "done" && j.reason ? "<p class='q-note'>" + esc(j.reason) + "</p>" : "") + "</li>";
}

function memBar(q) {
  const m = q.memory || {};
  const total = m.total_gb || 0;
  if (!total) return "";
  const runs = (q.jobs || []).filter(isLive);
  const used = runs.reduce((a, j) => a + (Number((j.resources || {}).rss_gb) || 0), 0);
  const free = m.free_gb || 0;
  const other = Math.max(0, total - free - used);
  const pct = (x) => Math.max(0, Math.min(100, (x / total) * 100)).toFixed(1) + "%";
  return "<div class='q-mem' title='" + esc("This Linux has " + total.toFixed(1) + " GB. " + (m.reserve_gb || 0) + " GB is kept for the system.") + "'>" +
    "<span class='q-mem-bar'><i class='is-other' style='width:" + pct(other) + "'></i><i class='is-runs' style='width:" + pct(used) + "'></i>" +
    "<i class='is-res' style='width:" + pct(m.reserve_gb || 0) + "'></i></span>" +
    "<span class='q-mem-t'>" + (runs.length ? runs.length + (runs.length === 1 ? " run uses " : " runs use ") + used.toFixed(1) + " GB · " : "") +
    free.toFixed(1) + " GB free</span></div>";
}

function renderPop() {
  const el = pop();
  if (!el || !snap) return;
  const q = snap;
  const jobs = q.jobs || [];
  const running = jobs.filter(isLive);
  const paused = jobs.filter((j) => j.state === "paused");
  const waiting = jobs.filter((j) => j.state === "queued").sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const interrupted = jobs.filter((j) => j.state === "interrupted");
  const ended = jobs.filter((j) => ["done", "failed", "stopped", "skipped"].includes(j.state)).slice(-8).reverse();
  const multi = new Set(jobs.map((j) => j.design_dir)).size > 1;
  const mp = q.max_parallel || {};
  const opts = [["auto", "Auto (" + (mp.auto || mp.now || 1) + " now)"], ...[1, 2, 3, 4, 6, 8].map((n) => [String(n), String(n)])];
  const setting = String(mp.setting || "auto");
  const hold = q.hold || {};
  const keep = hold.enabled === false ? "Not keeping the PC awake (turned off)"
    : hold.hold && hold.held ? "Keeping the PC awake" : hold.hold ? (hold.note || "") : "";
  const focusId = document.activeElement && el.contains(document.activeElement) ? (document.activeElement.closest("[data-id]") || {}).dataset?.id : null;
  const focusQ = document.activeElement && el.contains(document.activeElement) ? document.activeElement.dataset.q : null;
  el.innerHTML =
    "<div class='q-head'><span class='menu-lane'>Run queue</span>" +
      "<label class='q-par'><span>Runs at once</span><select data-q='par' aria-label='Runs at once'>" +
        opts.map(([v, l]) => "<option value='" + v + "'" + (v === setting ? " selected" : "") + ">" + l + "</option>").join("") + "</select></label>" +
      "<button class='btn btn-ghost btn-xs' data-q='pause'>" + (q.any_paused && waiting.length ? "Start the queue" : "Pause") + "</button></div>" +
    "<p class='q-why'>" + esc(mp.why || "") + " Changes apply to the next run that starts.</p>" +
    memBar(q) +
    (q.notices || []).map((n) => "<p class='q-note is-warn'>" + esc(n) + "</p>").join("") +
    ((q.orphans || []).length ? "<div class='menu-lane'>Still running</div><ul class='q-list'>" + q.orphans.map((o) =>
      "<li class='q-row is-int'><p class='q-note'>A LanEx run container is still going (" + esc(o.name) + (o.workdir ? ", working in " + esc(o.workdir) : "") +
      "). No queue knows it.</p><div class='q-row-meta'><span></span><span class='q-acts'><button class='btn btn-ghost btn-xs' data-q='orphan' data-name='" + esc(o.name) + "'>Stop it</button></span></div></li>").join("") + "</ul>" : "") +
    (interrupted.length ? "<div class='menu-lane'>Interrupted</div><ul class='q-list'>" + interrupted.map(rowInterrupted).join("") + "</ul>" : "") +
    (running.length ? "<div class='menu-lane'>Running</div><ul class='q-list'>" + running.map(rowRunning).join("") + "</ul>" : "") +
    (paused.length ? "<div class='menu-lane'>Step by step</div><ul class='q-list'>" + paused.map(rowPaused).join("") + "</ul>" : "") +
    (waiting.length ? "<div class='menu-lane'>Waiting · in this order</div><ul class='q-list'>" + waiting.map((j, i) => rowWaiting(j, i, multi)).join("") + "</ul>" : "") +
    (!running.length && !waiting.length && !interrupted.length && !paused.length
      ? "<p class='q-empty'>Nothing is running or waiting. A run you start while others go waits here and starts by itself.</p>" : "") +
    (ended.length ? "<details class='q-ended'><summary>Finished here (" + ended.length + ")</summary><ul class='q-list'>" + ended.map(rowEnded).join("") + "</ul></details>" : "") +
    "<div class='q-foot'>" +
      "<label class='q-check'><input type='checkbox' data-q='after'" + (q.stop_after_current ? " checked" : "") + (running.length ? "" : " disabled") + "/> Stop after the runs going now</label>" +
      "<span class='q-keep" + (hold.hold && hold.held ? " is-on" : "") + "' title='Closing the lid or pressing the power button still puts the PC to sleep.'>" + esc(keep) + "</span>" +
      "<span class='q-foot-acts'><button class='btn btn-ghost btn-xs' data-q='runs'>Open Runs</button>" +
      (jobs.some((j) => ["done", "failed", "stopped", "skipped"].includes(j.state)) ? "<button class='btn btn-ghost btn-xs' data-q='clear'>Clear finished</button>" : "") + "</span>" +
    "</div>";
  for (const j of jobs) if (j.state === "failed") _seenFail.add(j.id);
  if (focusId) {
    const row = el.querySelector("[data-id='" + CSS.escape(focusId) + "']");
    const t = row && (focusQ ? row.querySelector("[data-q='" + focusQ + "']") : row);
    if (t) t.focus();
  }
}

function jobOf(id) { return ((snap && snap.jobs) || []).find((j) => j.id === id) || null; }

async function act(kind, id, btn) {
  const j = id ? jobOf(id) : null;
  try {
    if (kind === "stop" && j) {
      const { confirmDialog } = await import("./dialog.js");
      if (j.state !== "starting") {
        const ok = await confirmDialog({
          title: "Stop " + (j.project || "this run") + "?",
          body: "Stopping ends the step it is on now. What it finished stays in " + (j.tag || "its run folder") + ", and you can resume from a finished stage later.",
          confirmText: "Stop the run", cancelText: "Keep running", danger: true,
        });
        if (!ok) return;
      }
      await api.queueStop({ id });
    } else if (kind === "next" && j) {
      const r = await api.queueResume(id);
      if (r && r.started === false) toast.show(r.reason || "The next step waits for a free slot.", "info");
    } else if (kind === "up" || kind === "down") {
      await api.queueMove(id, kind);
    } else if (kind === "remove" && j) {
      await api.queueRemove({ id });
      toast.show("Removed “" + (j.label || "the run") + "” from the queue.", "info", 7000, {
        label: "Undo", run: () => api.queueAdd({ design_dir: j.design_dir, spec: j.spec, label: j.label, position: "next" })
          .then(refreshQueue).catch((e) => toast.show(e.message, "error")),
      });
    } else if (kind === "continue" && j) {
      await api.queueRecover(id, "continue");
      toast.show("Continuing " + (j.tag || "the run") + " from " + ((j.resume || {}).from_step || "where it stopped") + ".", "success");
    } else if (kind === "markstop" && j) {
      await api.queueRecover(id, "stop");
    } else if (kind === "orphan") {
      await api.queueOrphan(btn.dataset.name);
    } else if (kind === "pause") {
      const anyPaused = snap && snap.any_paused && (snap.jobs || []).some((x) => x.state === "queued");
      await api.queuePause(!anyPaused);
    } else if (kind === "clear") {
      await api.queueRemove({ finished: true });
    } else if (kind === "runs") {
      closeLayer(pop());
      (await import("./nav.js")).go("runs");
      return;
    } else if (kind === "show" && j) {
      await show(j);
      return;
    }
  } catch (e) {
    toast.show(e.message || String(e), "error");
  }
  refreshQueue();
}

// Show a job's run on Flow (switching project if it is another one's).
async function show(j) {
  closeLayer(pop());
  if (j.design_dir && state.designDir && j.design_dir.replace(/\/+$/, "") !== state.designDir.replace(/\/+$/, "")) {
    try {
      const setup = await import("./setup.js");
      await setup.openDesignQuick(j.design_dir, { explicit: true });
    } catch (e) { toast.show("Couldn't open " + j.design_dir + ": " + e.message, "error"); return; }
  }
  if (isLive(j) || j.state === "paused") {
    followJob(state, j.id, true);
    try {
      const s = await api.runStatus(j.id);
      document.dispatchEvent(new CustomEvent("g:follow", { detail: { job: j, status: s } }));
    } catch (_e) {}
  }
  const ctx = await import("./context.js");
  if (j.tag && !isLive(j)) { try { await ctx.loadRuns({ fresh: true }); await ctx.setRun(j.tag); } catch (_e) {} }
  (await import("./nav.js")).go("pipeline", {}, { link: true });
}

function onPopKey(e) {
  const row = e.target.closest && e.target.closest(".q-row.is-wait");
  if (!row) return;
  if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
    e.preventDefault();
    act(e.key === "ArrowUp" ? "up" : "down", row.dataset.id);
  } else if (e.key === "Delete" && e.target === row) {
    e.preventDefault();
    act("remove", row.dataset.id);
  }
}

export function openQueue() {
  const el = pop(), c = chip();
  if (!el) return;
  if (isLayerOpen(el)) { closeLayer(el); return; }
  el.hidden = false;
  renderPop();
  c && c.setAttribute("aria-expanded", "true");
  if (c) { delete c.dataset.tip; const tip = document.querySelector(".tip"); if (tip) tip.hidden = true; }
  openLayer(el, { kind: "popover", owner: c, label: "Run queue", onClose: () => { el.hidden = true; c && c.setAttribute("aria-expanded", "false"); paintChip(); } });
  refreshQueue();
}

export function setupQueue() {
  const c = chip(), el = pop();
  if (!c || !el || c._wired) return;
  c._wired = true;
  c.addEventListener("click", (e) => { e.stopPropagation(); openQueue(); });
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-q]");
    if (!b || b.tagName === "SELECT" || b.tagName === "INPUT") return;
    const row = b.closest("[data-id]");
    act(b.dataset.q, row ? row.dataset.id : null, b);
  });
  el.addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.dataset.q === "par") await api.queueSettings({ max_parallel: t.value === "auto" ? "auto" : Number(t.value) });
      if (t.dataset.q === "after") await api.queueStopAfter(t.checked);
    } catch (ex) { toast.show(ex.message, "error"); }
    refreshQueue();
  });
  el.addEventListener("keydown", onPopKey);
  document.addEventListener("g:queue", (e) => {
    const ev = e.detail || {};
    if (ev.type === "queue:changed") {
      snap = Object.assign({}, ev);
      delete snap.type; delete snap.seq; delete snap.ts;
    } else if (ev.type === "queue:job" && ev.job && snap) {
      const i = (snap.jobs || []).findIndex((j) => j.id === ev.job.id);
      if (i >= 0) snap.jobs[i] = ev.job; else (snap.jobs = snap.jobs || []).push(ev.job);
    }
    schedulePaint();
  });
  document.addEventListener("g:server_back", refreshQueue);
  refreshQueue().then(() => offerRecovery());
}

// After a restart that interrupted runs: one toast that stays until answered.
let _offered = false;
function offerRecovery() {
  if (_offered || !snap) return;
  const n = (snap.jobs || []).filter((j) => j.state === "interrupted").length;
  if (!n) return;
  _offered = true;
  toast.show("LanEx stopped while " + (n === 1 ? "a run was" : n + " runs were") + " going. The queue is paused until you decide.", "warn", 0,
    { label: "Review", run: () => openQueue() });
}
