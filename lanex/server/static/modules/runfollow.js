// runfollow.js — which run a window follows when several go at once.
//
// The job queue (controller/jobs.py) runs several flows at the same time: two
// projects, or a sweep's points two or three at a time. Every run event carries
// job_id and design_dir. A window shows one run of its open project on Flow,
// the status line and the Run button: the run it was showing, until that run
// ends; then the next one of the same project that starts. The queue popover's
// "Show" picks one (followJob). Events of other runs go to the queue chip and
// the Runs list only.
//
// Pure apart from the `st` object it is handed (state.js), so it is tested.

function norm(p) {
  return String(p || "").replace(/[\\/]+$/, "");
}

// Same project folder (an empty side counts as the same: older events, a
// window with no project).
export function sameDesign(a, b) {
  if (!a || !b) return true;
  return norm(a) === norm(b);
}

// Does this window show the run *ev* belongs to? Updates st.followJob /
// st.followLive as runs start and end.
export function followsRun(ev, st) {
  if (!ev || !ev.job_id) return true;
  if (ev.design_dir && st.designDir && !sameDesign(ev.design_dir, st.designDir)) return false;
  if (st.followDesign !== (st.designDir || "")) {
    // Another project was opened: what this window followed belongs to the old one.
    st.followDesign = st.designDir || "";
    st.followJob = null;
    st.followLive = false;
  }
  const starts = ev.type === "info" && Array.isArray(ev.step_graph);
  if (!st.followJob) {
    st.followJob = ev.job_id;
    st.followLive = ev.type !== "flow_done";
    return true;
  }
  if (st.followJob === ev.job_id) {
    if (ev.type === "flow_done") st.followLive = false;
    else if (starts) st.followLive = true;
    return true;
  }
  // Another run of this project: it takes over once the one shown has ended.
  if (!st.followLive && (starts || ev.type === "step_started" || ev.type === "phase")) {
    st.followJob = ev.job_id;
    st.followLive = true;
    return true;
  }
  return false;
}

// Show this run from now on (the queue popover's Show, a page load).
export function followJob(st, jobId, live = true) {
  st.followDesign = st.designDir || "";
  st.followJob = jobId || null;
  st.followLive = !!(jobId && live);
}
