# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The job queue and the multi-runner: every LibreLane run LanEx starts.

One process-wide :class:`Scheduler` (``jobs.scheduler``) owns every run. Each
project keeps its own queue in ``<project>/.lanex/queue.json`` (written
atomically, on state changes only), so a queue survives a server restart and
belongs to the project, not to the server's "active project". The scheduler
reads every known project's queue at start, so two projects can run at once,
and memory and cores are shared fairly across them.

Studies (lane S) and the Runs page (lane 2) build on this API. It is stable:
any change is recorded in ``.claude/audit-phases/design/LANE-1-design.md``.

Job (one entry of ``queue.json`` ``jobs``, and of ``GET /api/queue`` ``jobs``)
------------------------------------------------------------------------------
``id``            "j-20261126-104512-3f2a" (unique, never reused)
``design_dir``    the project folder (absolute, resolved)
``group``         a sweep or study id, or None. ``/api/queue/remove {group}``
                  removes every waiting job of the group.
``kind``          "run" (a plain run), "point" (a sweep/study point),
                  "continue" (an interrupted run continued in its own folder)
``label``         what the queue shows ("Run to Placement",
                  "Sweep CLOCK_PERIOD = 8")
``origin``        "lanex" or "sweep" (scheduled jobs); "terminal" for a run
                  started in a terminal that LanEx follows (listed, never
                  scheduled; its id starts with "term:")
``spec``          the run request, exactly what ``POST /api/run/start`` takes:
                  ``{flow, run_mode, mode, tag, frm, to, only, skip, overrides,
                  sources, extras, threads ("auto" | N), priority ("normal" |
                  "low"), keep_awake, notify, checks_ack, sweep: {id, values}}``
``state``         see STATES below
``reason``        plain words: why it waits ("Waiting for memory: needs 2.1 GB,
                  1.4 GB free"), or how it ended ("Failed at Detailed routing")
``seq``           its place in the waiting order (smaller starts first)
``queued_at``, ``started_at``, ``ended_at``   ISO 8601 with offset, or None
``tag``, ``run_dir``   the run folder, reserved (``mkdir``) when the job starts
``engine``        "container" or "local"
``container``     the run container's name ("lanex-flow-…"), container runs
``boot``          the server boot that started it (``events.BOOT_ID``)
``step``          ``{id, index, total, started}`` of the step running now
``estimate``      ``{seconds, basis}`` total run time, or None (first run)
``remaining_s``   seconds left (running jobs), None when not known
``resources``     ``{threads: {VAR: n}, need_gb, rss_gb, peak_gb}``
``outcome``       how it ended: ``{result: "ok"|"error"|"cancelled"|
                  "not-started", message, kind, stage, step, ok, good}``
``resume``        (interrupted jobs) ``{from_step, done_steps, total, state,
                  drift: [..], reason}``: where Continue starts
``attempts``      ``[{at, how: "start"|"continue", from_step}]``
``resume_pending`` (step by step) Next step was pressed and waits for a slot;
                  kept in the file, so a restart makes it ``interrupted``

The file carries ``owner`` (this LanEx home's id, :func:`home_id`). A queue
file without it, or another home's, is read paused with a notice, and the runs
it lists as going are checked (followed again only when their container really
works in this project, else ``interrupted``): a queue that came with a project
never starts or stops anything without a click.

States
------
``queued``       waiting (holds no slot)          → starting, stopped (removed)
``starting``     tag reserved, launching          → running, failed
``running``      LibreLane is working             → done, failed, stopping, paused
``paused``       step by step, between two steps (holds no slot) → running, stopped
``stopping``     Stop was asked, the step is being ended → stopped
``done``         the flow ended (``outcome.ok`` says whether checks passed)
``failed``       the flow stopped with an error, or never started
``stopped``      stopped by you
``interrupted``  LanEx stopped while it ran (server killed, PC restarted)
``skipped``      never run (a sweep stop rule dropped it)

Routes (every one takes ``design_dir`` explicitly where it needs a project;
none uses the server's active project)
------------------------------------------------------------------------------
``GET  /api/queue``  → ``snapshot()``: ``{jobs, running, waiting, paused,
       stop_after_current, max_parallel: {setting, now, why}, memory: {free_gb,
       reserve_gb, total_gb, used_by_runs_gb}, eta: {seconds, known,
       unknown_count}, hold: {hold, reason}, orphans: [...], boot}``
``POST /api/queue/add     {design_dir, spec | specs: [...], group?, kind?, label?,
       position?: "end"|"next"}`` → ``{jobs: [job, ...]}`` (a refusal from the
       config check comes back as ``{ok: false, config_check, reason}``)
``POST /api/queue/move    {id, to: "top"|"up"|"down"|"bottom"|index}``
``POST /api/queue/remove  {id} | {group} | {finished: true}``
``POST /api/queue/pause   {paused: bool}``   (every project's queue)
``POST /api/queue/stop    {id} | {all: true}``
``POST /api/queue/stop-after-current {on: bool}``
``POST /api/queue/settings {max_parallel: "auto" | 1..16}``
``POST /api/queue/recover {id, action: "continue"|"stop"}``
``POST /api/queue/resume  {id}``           (next step of a step-by-step job)
``POST /api/queue/orphan  {name, action: "stop"}``
``GET  /api/power/hold``  → ``{hold, reason, since, jobs}`` (the Windows
       launcher polls it and holds Windows awake, lane 6)
``POST /api/run/start`` is unchanged for the page and now goes through here:
       ``{..., when: "now"|"queue"}``; "now" with no free slot answers
       ``{ok: false, started: false, busy: true, reason}``.

Events (on the shared SSE bus)
------------------------------
``queue:changed``  the snapshot (as ``GET /api/queue``), at most every 0.5 s
``queue:job``      ``{job}`` one job, on each state change
Every run event (``info``, ``phase``, ``log``, ``progress``, ``step_*``,
``flow_done``) carries ``job_id``, ``design_dir`` and ``origin``, so a page
follows the run it shows and ignores the others.

Runs at once (``max_parallel``)
-------------------------------
"auto" (the default, in ``$LANEX_HOME/settings.json`` ``queue_max_parallel``)
is the smallest of: runs that fit in memory, runs that fit the cores (4 cores
each), and 8. Memory need per design comes from its measured peak (the run
container's cgroup), else its steps' ``process_stats`` corrected for flooring,
else unknown: the first run of a design runs alone until it ends. A fixed
number is a maximum; LanEx still waits when memory is short. Every start is
checked against memory free *now*. Local-tools runs go one at a time
(LibreLane runs them inside LanEx and its log handlers are process-wide).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_log = logging.getLogger("librelane.gui.jobs")

STATES = ("queued", "starting", "running", "paused", "stopping",
          "done", "failed", "stopped", "interrupted", "skipped")
ACTIVE = frozenset({"starting", "running", "stopping"})      # hold a slot
LIVE = ACTIVE | {"paused"}                                     # a process or a paused step run
ENDED = frozenset({"done", "failed", "stopped", "interrupted", "skipped"})

QUEUE_FILE = Path(".lanex") / "queue.json"
AUTO_CAP = 8
FIXED_CAP = 16
CORES_PER_RUN = 4         # cores each run gets before "fits the cores" stops counting (8+ cores)
KEEP_ENDED = 60           # ended jobs kept per project (the queue is not the run history)
CONTINUE_QUIET_S = 60     # Continue waits until a run's flow.log has been still this long
MAX_WAITING = 500
EMIT_EVERY_S = 0.5
SAMPLE_EVERY_S = 5.0
TICK_S = 1.0


# ------------------------------------------------------------------ small pure helpers

def now_iso() -> str:
    try:
        from . import platform_env
        return platform_env.local_now().isoformat(timespec="seconds")
    except Exception:  # pragma: no cover
        return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_job_id() -> str:
    return "j-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def design_key(design_dir: Any) -> str:
    try:
        return str(Path(str(design_dir)).expanduser().resolve())
    except Exception:
        return str(design_dir)


def slug(step_id: str) -> str:
    """The folder slug LibreLane gives a step ("OpenROAD.DetailedRouting" ->
    "openroad-detailedrouting", the part after "NN-")."""
    return re.sub(r"[^a-z0-9]+", "-", str(step_id or "").lower()).strip("-")


_GOOD_TAG = re.compile(r"[A-Za-z0-9._-]{1,120}")


def good_tag(tag: Any) -> bool:
    """A run name that stays one folder inside ``runs/`` (no ``/``, not
    ``.``/``..``, not an option)."""
    t = str(tag or "")
    return bool(_GOOD_TAG.fullmatch(t)) and t not in (".", "..") and not t.startswith("-")


def reserve_tag(design_dir: Any, tag: Optional[str] = None) -> str:
    """Make the run folder now (``mkdir``), so two starts in the same second
    never share one. LibreLane treats an existing empty folder as a new run
    (flow.py), so the reservation is invisible to it. A given *tag* that is
    taken gets ``-2``, ``-3``."""
    from . import platform_env
    if tag and not good_tag(tag):
        raise ValueError(f"The run name {str(tag)[:60]!r} can't be used: letters, digits and . _ - only.")
    runs = Path(str(design_dir)) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = tag or platform_env.new_run_tag(runs)
    cand, n = base, 2
    while True:
        try:
            (runs / cand).mkdir()
            return cand
        except FileExistsError:
            cand, n = f"{base}-{n}", n + 1
            if n > 999:
                raise


def unreserve(design_dir: Any, tag: Optional[str]) -> None:
    """Take back a reserved folder that stayed empty (the run never started)."""
    if not tag:
        return
    runs = Path(str(design_dir)) / "runs"
    try:
        (runs / tag).rmdir()
        runs.rmdir()            # only when nothing else is in it
    except OSError:
        pass


def need_gb(peak_gb: Optional[float]) -> float:
    """Memory one run of a design needs: its peak with 25 % room, plus the
    container and LibreLane itself. 2 GB for a design never run."""
    if not peak_gb:
        return 2.0
    return round(1.25 * float(peak_gb) + 0.5, 2)


def auto_parallel(*, cores: Optional[int], free_gb: Optional[float], total_gb: Optional[float],
                  need: float) -> Dict[str, Any]:
    """"Runs at once: Auto" for this machine and design. Pure.

    ``{n, mem_slots, cpu_slots, reserve_gb}``: the smallest of the runs that
    fit in memory (free minus a reserve, over the need of one run), the runs
    that fit the cores (4 each on 8+ cores, else 2), and 8."""
    reserve = reserve_gb(total_gb)
    mem_slots = None
    if free_gb is not None:
        mem_slots = max(1, int(math.floor(max(0.0, float(free_gb) - reserve) / max(0.5, need))))
    per = CORES_PER_RUN if (cores or 0) >= 8 else 2
    cpu_slots = max(1, int((cores or per) // per))
    n = min([x for x in (mem_slots, cpu_slots, AUTO_CAP) if x is not None])
    return {"n": max(1, n), "mem_slots": mem_slots, "cpu_slots": cpu_slots, "reserve_gb": reserve}


def reserve_gb(total_gb: Optional[float]) -> float:
    """Memory LanEx leaves alone for the system and for itself."""
    return round(max(2.0, 0.1 * float(total_gb or 0)), 1)


def share_of(cores: Optional[int], free_gb: Optional[float], k: int) -> Dict[str, Any]:
    """The machine facts one of *k* runs going at once sizes its threads to."""
    k = max(1, int(k))
    c = None if not cores else max(2, int(cores) // k)
    f = None if free_gb is None else round(float(free_gb) / k, 2)
    return {"cores": c, "available_gb": f, "total_gb": None, "share": k}


def eta_words(seconds: Optional[float]) -> str:
    """"about 12 min" / "about 1 h 20" / "less than a minute"; "" when unknown.
    Rounded so it never ticks: under 10 min to the minute, under an hour to
    5 minutes, then to 10 minutes. Pure (the page has the same rules)."""
    if seconds is None:
        return ""
    s = max(0.0, float(seconds))
    if s < 60:
        return "less than a minute"
    m = s / 60.0
    if m < 10:
        return f"about {int(round(m))} min"
    if m < 60:
        return f"about {int(round(m / 5.0) * 5)} min"
    total = int(round(m / 10.0) * 10)
    h, mm = divmod(total, 60)
    return f"about {h} h" + (f" {mm:02d}" if mm else "")


def list_schedule(running_left: List[float], waiting: List[Optional[float]], slots: int) -> Optional[float]:
    """When the last waiting job would end if *slots* run at once (list
    scheduling, each job to the first free slot). None when any is unknown."""
    if any(w is None for w in waiting) or any(r is None for r in running_left):
        return None
    slots = max(1, int(slots))
    free = sorted([max(0.0, float(r)) for r in running_left])[:slots]
    while len(free) < slots:
        free.append(0.0)
    free.sort()
    end = max(free) if running_left else 0.0
    for w in waiting:
        t = free.pop(0) + float(w)
        end = max(end, t)
        free.append(t)
        free.sort()
    return end


# ------------------------------------------------------------------ design facts

_STEP_TIMES: Dict[str, Tuple[Any, Any]] = {}


def _runtime_secs(p: Path) -> Optional[float]:
    try:
        s = p.read_text(encoding="utf-8").strip()
        if ":" in s:
            h, m, sec = s.split(":")
            return int(h) * 3600 + int(m) * 60 + float(sec)
        return float(s)
    except Exception:
        return None


def _clean_run(run: Path) -> bool:
    """A run that ended with the whole flow done: LibreLane's last line."""
    try:
        with open(run / "flow.log", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            return b"Flow complete." in f.read()
    except Exception:
        return False


def step_times(design_dir: Any, limit: int = 3) -> Dict[str, Any]:
    """Median seconds per step slug over this design's last *limit* clean
    runs, and the order of the newest one: ``{order: [slug], secs: {slug: s},
    total, runs: [tag], basis}``. Cached on the runs folder's mtime."""
    runs = Path(str(design_dir)) / "runs"
    try:
        key = runs.stat().st_mtime_ns
    except OSError:
        return {"order": [], "secs": {}, "total": None, "runs": [], "basis": ""}
    hit = _STEP_TIMES.get(str(runs))
    if hit and hit[0] == key:
        return hit[1]
    try:
        dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        dirs = []
    per: Dict[str, List[float]] = {}
    order: List[str] = []
    used: List[str] = []
    totals: List[float] = []
    for d in dirs[:12]:
        if len(used) >= limit:
            break
        if not _clean_run(d):
            continue
        steps = []
        for s in d.iterdir():
            m = re.match(r"(\d+)-(.+)$", s.name)
            if m and s.is_dir():
                steps.append((int(m.group(1)), m.group(2), s))
        steps.sort()
        seen = set()
        run_order = []
        tot = 0.0
        for _n, sl, s in steps:
            secs = _runtime_secs(s / "runtime.txt")
            if secs is None:
                continue
            if sl in seen:          # a step that ran twice (a continued run): the later one counts
                per[sl][-1] = secs
                continue
            seen.add(sl)
            run_order.append(sl)
            per.setdefault(sl, []).append(secs)
            tot += secs
        if run_order:
            used.append(d.name)
            totals.append(tot)
            if not order:
                order = run_order
    secs = {k: round(statistics.median(v), 2) for k, v in per.items() if v}
    out = {"order": order, "secs": secs, "total": round(statistics.median(totals), 1) if totals else None,
           "runs": used, "basis": (f"the last {len(used)} clean run" + ("s" if len(used) != 1 else "")
                                    + f" of {Path(str(design_dir)).name}") if used else ""}
    if len(_STEP_TIMES) > 64:
        _STEP_TIMES.clear()
    _STEP_TIMES[str(runs)] = (key, out)
    return out


def estimate_for(times: Dict[str, Any], *, frm: Optional[str] = None, to: Optional[str] = None,
                 done: Iterable[str] = (), current: Optional[str] = None,
                 current_elapsed: float = 0.0) -> Optional[float]:
    """Seconds a run of the range [frm, to] takes (or has left: *done* slugs
    are over, *current* has run *current_elapsed*). None when this design has
    no clean run to go by. Pure."""
    order = list(times.get("order") or [])
    secs = times.get("secs") or {}
    if not order:
        return None
    lo, hi = 0, len(order) - 1
    if frm and slug(frm) in order:
        lo = order.index(slug(frm))
    if to and slug(to) in order:
        hi = order.index(slug(to))
    if hi < lo:
        return None
    rng = order[lo:hi + 1]
    done_s = {slug(d) for d in done}
    cur = slug(current) if current else None
    left = 0.0
    for sl in rng:
        if sl in done_s:
            continue
        base = float(secs.get(sl) or 0.0)
        if sl == cur:
            left += max(0.0, base - float(current_elapsed or 0.0))
        else:
            left += base
    return round(left, 1)


_PEAKS: Dict[str, Tuple[float, Any]] = {}


def design_peak(design_dir: Any) -> Dict[str, Any]:
    """The most memory one run of this design used: ``{gb, basis, tag}``.

    First the measured whole-container peak LanEx recorded in ``gui-run.json``
    (``resources.peak_gb``) over the last 3 runs; else LibreLane's per-step
    ``process_stats`` (whose sizes are floored to a whole unit, so "1GiB" can
    be 1.99: one unit is added back); else ``{gb: None}``. Cached 20 s."""
    key = design_key(design_dir)
    hit = _PEAKS.get(key)
    if hit and time.monotonic() - hit[0] < 20:
        return hit[1]
    out: Dict[str, Any] = {"gb": None, "basis": "none", "tag": None}
    runs = Path(key) / "runs"
    try:
        dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        dirs = []
    best = None
    for d in dirs[:3]:
        try:
            meta = json.loads((d / "gui-run.json").read_text(encoding="utf-8"))
            gb = float(((meta.get("resources") or {}).get("peak_gb")) or 0)
        except Exception:
            gb = 0.0
        if gb > 0 and (best is None or gb > best[0]):
            best = (gb, d.name)
    if best:
        out = {"gb": round(best[0], 2), "basis": "measured", "tag": best[1]}
    else:
        try:
            from . import runresources
            pk = runresources.last_run_peak_gb(key)
        except Exception:
            pk = None
        if pk and pk.get("gb"):
            gb = float(pk["gb"])
            gb = gb + 1.0 if gb >= 1.0 else gb + 0.01
            out = {"gb": round(gb, 2), "basis": "process_stats", "tag": pk.get("tag")}
    _PEAKS[key] = (time.monotonic(), out)
    return out


def machine_facts() -> Dict[str, Any]:
    """Cores and memory now (in the Linux the containers run in)."""
    out: Dict[str, Any] = {"cores": None, "free_gb": None, "total_gb": None}
    try:
        from . import runresources
        out["cores"] = runresources.usable_cores()
    except Exception:
        pass
    try:
        import psutil
        vm = psutil.virtual_memory()
        out["free_gb"] = round(vm.available / 2 ** 30, 2)
        out["total_gb"] = round(vm.total / 2 ** 30, 2)
        if not out["cores"]:
            out["cores"] = os.cpu_count()
    except Exception:
        out["cores"] = out["cores"] or os.cpu_count()
    return out


def disk_free_gb(path: Any) -> Optional[float]:
    try:
        return round(shutil.disk_usage(str(path)).free / 2 ** 30, 2)
    except Exception:
        return None


# ------------------------------------------------------------------ run folder facts (recovery)

def run_progress(run_dir: Any) -> Dict[str, Any]:
    """How far a run folder got: ``{done: [(n, slug, dir)], last_done,
    first_unfinished, complete, state_out}``. A step folder with
    ``runtime.txt`` finished; one with ``state_in.json`` and no
    ``state_out.json`` was cut off. A step that ran twice (a continued run)
    counts by its highest-numbered folder."""
    rd = Path(str(run_dir))
    done: List[Tuple[int, str, str]] = []
    cut: Optional[Tuple[int, str, str]] = None
    try:
        entries = sorted(((int(m.group(1)), m.group(2), e) for e in rd.iterdir()
                          for m in [re.match(r"(\d+)-(.+)$", e.name)] if m and e.is_dir()),
                         key=lambda t: t[0])
    except OSError:
        entries = []
    for n, sl, e in entries:
        if (e / "runtime.txt").exists() and (e / "state_out.json").exists():
            done = [d for d in done if d[1] != sl] + [(n, sl, e.name)]
            if cut and cut[1] == sl:
                cut = None
        elif (e / "state_in.json").exists():
            cut = (n, sl, e.name)
    last = done[-1] if done else None
    return {
        "done": done,
        "last_done": last,
        "cut": cut,
        "complete": _clean_run(rd),
        "state_out": str(rd / last[2] / "state_out.json") if last else None,
    }


def step_after(step_ids: List[str], last_slug: Optional[str]) -> Optional[str]:
    """The step id that follows the step whose slug is *last_slug* (the
    first step when nothing finished). None at the end of the flow."""
    if not step_ids:
        return None
    if not last_slug:
        return step_ids[0]
    slugs = [slug(s) for s in step_ids]
    # A repeated step's folder slug carries a suffix ("-1"): match its base.
    base = re.sub(r"-\d+$", "", last_slug)
    idx = None
    for i, s in enumerate(slugs):
        if s == last_slug or s == base:
            idx = i
    if idx is None:
        return None
    return step_ids[idx + 1] if idx + 1 < len(step_ids) else None


# ------------------------------------------------------------------ engine probes

def _engine_argv(*args: str) -> Optional[List[str]]:
    try:
        from . import tools
        res = tools.resolve_engine()
        eng = res.get("engine")
        exe = shutil.which(eng) if eng else None
        if not exe:
            exe = shutil.which("docker") or shutil.which("podman")
        if not exe:
            return None
        argv = [exe, *args]
        if res.get("sg_wrap"):
            argv = tools.sg_wrap_argv(argv)
        return argv
    except Exception:
        exe = shutil.which("docker") or shutil.which("podman")
        return [exe, *args] if exe else None


def _engine_out(*args: str, timeout: float = 8.0) -> Optional[str]:
    argv = _engine_argv(*args)
    if not argv:
        return None
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def flow_containers() -> List[Dict[str, Any]]:
    """Every live LanEx flow container: ``[{name, id, workdir}]`` (empty when
    the engine can't be asked)."""
    out = _engine_out("ps", "--filter", "name=lanex-flow-", "--format", "{{.ID}} {{.Names}}")
    if not out:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("lanex-flow-"):
            found.append({"id": parts[0], "name": parts[1], "workdir": None})
    for c in found:
        wd = _engine_out("inspect", "--format", "{{.Config.WorkingDir}}", c["name"], timeout=5)
        c["workdir"] = (wd or "").strip() or None
    return found


def remove_container(name: str) -> bool:
    if not name or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
        return False
    argv = _engine_argv("rm", "-f", name)
    if not argv:
        return False
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except Exception:
        return False


def _works_in(container: Optional[Dict[str, Any]], design_dir: Any) -> bool:
    """A listed flow container (``flow_containers``) whose working folder is
    this project: LibreLane runs its container with ``-w <the project>``."""
    if not container or not container.get("workdir"):
        return False
    return design_key(container["workdir"]) == design_key(design_dir)


def job_container_alive(job: Dict[str, Any]) -> bool:
    """The container a job names is a live LanEx flow container working in
    the job's own project. A queue file can name any container, so nothing
    is stopped or followed on its name alone."""
    name = job.get("container")
    if not name:
        return False
    return any(c.get("name") == name and _works_in(c, job.get("design_dir")) for c in flow_containers())


def container_memory(name: str, cache: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """``{rss_gb, peak_gb}`` of a running container: its cgroup's
    ``memory.current`` and ``memory.peak`` (cgroup v2), else ``docker stats``."""
    cid = cache.get("id")
    if cid is None:
        out = _engine_out("inspect", "--format", "{{.Id}}", name, timeout=5)
        cid = (out or "").strip() or ""
        cache["id"] = cid
    if cid:
        for base in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope",
                     f"/sys/fs/cgroup/docker/{cid}",
                     f"/sys/fs/cgroup/machine.slice/libpod-{cid}.scope"):
            try:
                cur = int(Path(base, "memory.current").read_text().strip())
                try:
                    peak = int(Path(base, "memory.peak").read_text().strip())
                except Exception:
                    peak = cur
                cache["src"] = "cgroup"
                return {"rss_gb": round(cur / 2 ** 30, 3), "peak_gb": round(peak / 2 ** 30, 3)}
            except Exception:
                continue
    out = _engine_out("stats", "--no-stream", "--format", "{{.MemUsage}}", name, timeout=8)
    if not out:
        return None
    m = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)", out)
    if not m:
        return None
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1e3, "KIB": 2 ** 10, "MB": 1e6, "MIB": 2 ** 20, "GB": 1e9, "GIB": 2 ** 30}.get(unit, 1)
    gb = float(m.group(1)) * mult / 2 ** 30
    cache["src"] = "stats"
    prev = float(cache.get("peak") or 0)
    cache["peak"] = max(prev, gb)
    return {"rss_gb": round(gb, 3), "peak_gb": round(cache["peak"], 3)}


# ------------------------------------------------------------------ settings

def _settings_path() -> Path:
    from . import platform_env
    return platform_env.home() / "settings.json"


_SETTINGS_LOCK = threading.Lock()
_HOME_IDS: Dict[str, str] = {}
_HOME_ID_LOCK = threading.Lock()


def home_id() -> str:
    """This LanEx home's own id (``$LANEX_HOME/queue-owner-id``, made once).
    Every queue file LanEx writes carries it (``owner``): a queue that comes
    with a project from somewhere else (another PC, another LanEx, a zip) is
    recognised and never starts anything by itself."""
    from . import platform_env
    h = platform_env.home()
    key = str(h)
    with _HOME_ID_LOCK:
        hit = _HOME_IDS.get(key)
        if hit:
            return hit
        p = h / "queue-owner-id"
        val = ""
        try:
            val = p.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not re.fullmatch(r"[0-9a-f]{32}", val):
            val = secrets.token_hex(16)
            try:
                h.mkdir(parents=True, exist_ok=True)
                platform_env.atomic_write_text(p, val + "\n")
            except OSError:
                _log.debug("could not keep the queue owner id", exc_info=True)
        _HOME_IDS[key] = val
        return val


def read_setting(key: str, default: Any = None) -> Any:
    try:
        p = _settings_path()
        data = json.loads(p.read_text(encoding="utf-8") or "{}") if p.is_file() else {}
        return data.get(key, default) if isinstance(data, dict) else default
    except Exception:
        return default


def write_setting(key: str, value: Any) -> None:
    from . import platform_env
    p = _settings_path()
    # Read, change, write under one lock: two settings saved at once (Runs
    # at once and keep-awake) each kept the other's old value.
    with _SETTINGS_LOCK:
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}") if p.is_file() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        p.parent.mkdir(parents=True, exist_ok=True)
        platform_env.atomic_write_text(p, json.dumps(data, indent=2) + "\n")


def parallel_setting() -> Any:
    v = read_setting("queue_max_parallel", "auto")
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= FIXED_CAP:
        return v
    return "auto"


# ------------------------------------------------------------------ the scheduler

class Scheduler:
    """Every run LanEx starts, across every project.

    The page-facing calls (``submit``, ``move``, ``remove``, ``pause``,
    ``stop``, ``recover``, ``snapshot``) are quick and never wait on a run.
    A daemon thread (``start_service``) admits waiting jobs, samples memory
    and publishes ``queue:*`` events. Tests drive it with ``tick()``.

    The run itself is started by a *launcher* the server registers
    (``set_launcher(fn)``): ``fn(job, runner, share, dry) -> (reply, status)``
    runs the same checks and assembly a Run click does. *runner_factory*
    makes one :class:`runner.FlowRunner` per job.
    """

    def __init__(self, *, persist: bool = True) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._queues: Dict[str, Dict[str, Any]] = {}
        self._runners: Dict[str, Any] = {}
        self._followers: Dict[str, Any] = {}
        self._mem: Dict[str, Dict[str, Any]] = {}
        self._launcher: Optional[Callable[..., Tuple[Any, int]]] = None
        self._runner_factory: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._outcome_reader: Optional[Callable[[str], Dict[str, Any]]] = None
        self._end_hooks: List[Callable[[Dict[str, Any]], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._halt = threading.Event()
        self._dirty = False
        self._last_emit = 0.0
        self._last_sample = 0.0
        self._starting: set = set()
        self._latest: Dict[str, str] = {}     # design key -> id of the job its pages follow
        self._orphans: List[Dict[str, Any]] = []
        self._hold_since: Optional[str] = None
        self.persist = persist
        self._facts_override: Optional[Dict[str, Any]] = None   # tests

    # ---------------------------------------------------------------- wiring
    def set_launcher(self, fn: Callable[..., Tuple[Any, int]]) -> None:
        self._launcher = fn

    def set_runner_factory(self, fn: Callable[[Dict[str, Any]], Any]) -> None:
        self._runner_factory = fn

    def set_outcome_reader(self, fn: Callable[[str], Dict[str, Any]]) -> None:
        self._outcome_reader = fn

    def on_job_end(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        if fn not in self._end_hooks:
            self._end_hooks.append(fn)

    # ---------------------------------------------------------------- persistence
    def _qpath(self, key: str) -> Path:
        return Path(key) / QUEUE_FILE

    def queue_of(self, design_dir: Any) -> Dict[str, Any]:
        """The queue of one project (read from its file the first time)."""
        key = design_key(design_dir)
        with self._lock:
            q = self._queues.get(key)
            if q is not None:
                return q
            q = {"design_dir": key, "jobs": [], "paused": False, "stop_after_current": False, "version": 1}
            p = self._qpath(key)
            if self.persist and p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                        q["jobs"] = [j for j in data["jobs"] if isinstance(j, dict) and j.get("id")]
                        q["paused"] = bool(data.get("paused"))
                        q["stop_after_current"] = bool(data.get("stop_after_current"))
                        for j in q["jobs"]:
                            j["design_dir"] = key
                        self._adopt(q, data)
                except Exception:
                    # Kept aside, never deleted: the person may want it.
                    try:
                        p.replace(p.with_name(f"queue.unreadable-{int(time.time())}.json"))
                    except Exception:
                        pass
                    q["notice"] = "The queue file could not be read; it was kept aside and the queue starts empty."
            self._queues[key] = q
            return q

    FOREIGN_NOTICE = ("This project's queue was written by another LanEx (another PC, another user, or an "
                      "older LanEx), so it is paused and nothing in it starts by itself. Check the runs "
                      "waiting in it, then press Resume.")

    def _adopt(self, q: Dict[str, Any], data: Dict[str, Any]) -> None:
        """A queue file just read. One this LanEx home wrote (``owner``) is
        used as it is. Any other (it came with the project, or LanEx before
        queues were stamped) is paused with a notice when it holds anything
        that could start, so no run starts without a click. Runs it lists as
        going from an earlier boot are checked on the next tick: followed
        again only when their container really is working in this project,
        else interrupted (a listed "running" job never holds a slot)."""
        from .events import BOOT_ID
        own = isinstance(data.get("owner"), str) and data.get("owner") == home_id()
        pending = [j for j in q["jobs"] if j.get("state") in LIVE | {"queued"}]
        if not own and pending:
            q["paused"] = True
            q["foreign"] = True
            q["notice"] = self.FOREIGN_NOTICE
        if any(j.get("boot") != BOOT_ID and (j.get("state") in LIVE or j.get("resume_pending"))
               for j in q["jobs"]):
            q["_recover"] = True

    def _save(self, key: str) -> None:
        if not self.persist:
            return
        q = self._queues.get(key)
        if q is None:
            return
        try:
            from . import platform_env
            p = self._qpath(key)
            if not p.parent.is_dir():
                if not Path(key).is_dir():
                    return
                p.parent.mkdir(parents=True, exist_ok=True)
            doc = {"version": 1, "owner": home_id(), "paused": q.get("paused", False),
                   "stop_after_current": q.get("stop_after_current", False),
                   "jobs": [self._persistable(j) for j in q["jobs"]]}
            platform_env.atomic_write_text(p, json.dumps(doc, indent=2, default=str) + "\n")
        except Exception:
            _log.debug("could not write the queue of %s", key, exc_info=True)

    @staticmethod
    def _persistable(j: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in j.items() if not k.startswith("_")}

    # ---------------------------------------------------------------- queries
    def _all_jobs(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for q in self._queues.values():
            out.extend(q["jobs"])
        return out

    def job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for j in self._all_jobs():
                if j.get("id") == job_id:
                    return j
        f = self._followers.get(job_id)
        return f.public() if f is not None else None

    def runner(self, job_id: Optional[str]) -> Any:
        if not job_id:
            return None
        return self._runners.get(job_id)

    def jobs_of(self, design_dir: Any) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self.queue_of(design_dir)["jobs"]]

    def group_jobs(self, group: str) -> List[Dict[str, Any]]:
        """Every job of a sweep or study (copies), in queue order."""
        with self._lock:
            return [dict(j) for j in sorted(self._all_jobs(), key=lambda j: float(j.get("seq") or 0))
                    if j.get("group") == group]

    def jobs_snapshot_live(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self._all_jobs() if j.get("state") in LIVE]

    def note_tag(self, job_id: str, tag: str) -> None:
        """The launcher reserved the run folder: known before the run starts,
        so the terminal-run watcher never mistakes it for a run it should follow."""
        with self._lock:
            j = self.job(job_id)
            if j is not None and tag:
                j["tag"] = tag
                j["run_dir"] = str(Path(j["design_dir"]) / "runs" / tag)

    def live_run_dirs(self) -> List[str]:
        """Run folders being written now (LanEx runs and followed terminal runs)."""
        with self._lock:
            out = [j["run_dir"] for j in self._all_jobs() if j.get("state") in LIVE and j.get("run_dir")]
        out += [f.run_dir for f in list(self._followers.values()) if f.alive]
        return out

    def followed_job(self, design_dir: Any) -> Optional[str]:
        """The job a page of this project follows: its newest live LanEx job,
        else the newest live terminal run, else the last one that ran."""
        key = design_key(design_dir)
        with self._lock:
            jobs = self.queue_of(key)["jobs"]
            live = [j for j in jobs if j.get("state") in LIVE]
            if live:
                live.sort(key=lambda j: j.get("started_at") or "")
                return live[-1]["id"]
        for jid, f in list(self._followers.items()):
            if f.alive and design_key(f.design_dir) == key:
                return jid
        return self._latest.get(key)

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._all_jobs() if j.get("state") in ACTIVE)

    # ---------------------------------------------------------------- capacity
    def facts(self) -> Dict[str, Any]:
        return dict(self._facts_override) if self._facts_override else machine_facts()

    def _active(self) -> List[Dict[str, Any]]:
        return [j for j in self._all_jobs() if j.get("state") in ACTIVE]

    def capacity(self, facts: Optional[Dict[str, Any]] = None, *, design_dir: Any = None) -> Dict[str, Any]:
        """``{setting, now, why, need_gb, peak}``: how many may run at once now."""
        facts = facts or self.facts()
        setting = parallel_setting()
        active = self._active()
        used = sum(float((self._mem.get(j["id"]) or {}).get("rss_gb") or 0) for j in active)
        # Memory as if nothing ran: what is free plus what the runs going now use.
        idle_free = None if facts.get("free_gb") is None else float(facts["free_gb"]) + used
        dd = design_dir or (active[-1]["design_dir"] if active else None)
        peak = design_peak(dd) if dd else {"gb": None, "basis": "none"}
        need = need_gb(peak.get("gb"))
        auto = auto_parallel(cores=facts.get("cores"), free_gb=idle_free, total_gb=facts.get("total_gb"), need=need)
        n = auto["n"] if setting == "auto" else int(setting)
        name = Path(str(dd)).name if dd else "a design"
        free = facts.get("free_gb")
        bits = []
        if peak.get("gb"):
            bits.append(f"A run of {name} needs about {need:.1f} GB at its peak"
                        + (" (measured)" if peak.get("basis") == "measured" else ""))
        else:
            bits.append(f"LanEx doesn't know yet how much memory {name} needs, so it counts 2 GB a run")
        if free is not None:
            bits.append(f"{float(free):.1f} GB is free, and {auto['reserve_gb']:.1f} GB is kept for the system")
        why = ". ".join(bits) + ". More at once won't make any single run faster."
        if setting != "auto":
            why = f"At most {n} at once (your setting). LanEx still waits when memory is short. " + why
        return {"setting": setting, "now": n, "auto": auto, "why": why, "need_gb": need, "peak": peak,
                "used_gb": round(used, 2)}

    def _wait_reason(self, job: Dict[str, Any], facts: Dict[str, Any], cap: Dict[str, Any],
                     active: List[Dict[str, Any]], ahead: int) -> Optional[str]:
        """Why *job* can't start now, or None when it can."""
        key = job["design_dir"]
        q = self.queue_of(key)
        if q.get("paused"):
            return "Queue paused"
        if q.get("stop_after_current") or any(x.get("stop_after_current") for x in self._queues.values()):
            return "Stops after the runs going now"
        if not Path(key).is_dir():
            return "The project folder is not there any more"
        if len(active) >= cap["now"]:
            n = len(active)
            return f"Waiting its turn: {n} run{'s' if n != 1 else ''} going, the most at once" + \
                (f" · {ahead} ahead" if ahead else "")
        eng = (job.get("spec") or {}).get("run_mode") or job.get("engine") or "container"
        live_local = [a for a in self._all_jobs() if a.get("state") in LIVE and a is not job
                      and (a.get("spec") or {}).get("run_mode") == "local"]
        if eng == "local" and live_local:
            return "Local-tools runs go one at a time (LibreLane runs them inside LanEx)"
        peak = design_peak(key)
        same = [a for a in active if a["design_dir"] == key]
        if peak.get("gb") is None and same and not job.get("resume_pending"):
            return (f"First runs of {Path(key).name}: one at a time until LanEx knows how much "
                    "memory it needs")
        if active and facts.get("free_gb") is not None:
            need = need_gb(peak.get("gb"))
            pending = 0.0
            for a in active:
                m = self._mem.get(a["id"]) or {}
                a_need = float(((a.get("resources") or {}).get("need_gb")) or need_gb(design_peak(a["design_dir"]).get("gb")))
                pending += max(0.0, a_need - float(m.get("rss_gb") or 0))
            free_now = float(facts["free_gb"]) - cap["auto"]["reserve_gb"] - pending
            if free_now < need:
                return f"Waiting for memory: needs {need:.1f} GB, {max(0.0, free_now):.1f} GB free"
        disk = disk_free_gb(key)
        if disk is not None and disk < 1.0:
            return f"Waiting for disk space: {disk:.1f} GB free where the project is"
        return None

    # ---------------------------------------------------------------- submit
    def _new_job(self, design_dir: str, spec: Dict[str, Any], *, kind: str, group: Optional[str],
                 label: Optional[str], origin: Optional[str]) -> Dict[str, Any]:
        spec = dict(spec or {})
        return {
            "id": new_job_id(), "design_dir": design_dir, "group": group, "kind": kind,
            "label": label or label_for(spec, kind), "origin": origin or ("sweep" if kind == "point" else "lanex"),
            "spec": spec, "state": "queued", "reason": "", "seq": 0.0,
            "queued_at": now_iso(), "started_at": None, "ended_at": None,
            "tag": spec.get("tag") or None, "run_dir": None,
            "engine": "local" if spec.get("run_mode") == "local" else "container",
            "container": None, "boot": None, "step": None, "estimate": None, "remaining_s": None,
            "resources": {}, "outcome": None, "resume": None, "attempts": [],
        }

    def _next_seq(self, front: bool = False) -> float:
        seqs = [float(j.get("seq") or 0) for j in self._all_jobs() if j.get("state") == "queued"]
        if not seqs:
            return time.time()
        return (min(seqs) - 1.0) if front else (max(seqs) + 1.0)

    def add(self, design_dir: Any, spec: Dict[str, Any], *, kind: str = "run", group: Optional[str] = None,
            label: Optional[str] = None, position: str = "end", origin: Optional[str] = None) -> Dict[str, Any]:
        """Queue one job. It starts by itself when a slot and memory are free."""
        key = design_key(design_dir)
        with self._lock:
            q = self.queue_of(key)
            if sum(1 for j in q["jobs"] if j.get("state") == "queued") >= MAX_WAITING:
                raise ValueError(f"A project's queue holds at most {MAX_WAITING} waiting runs.")
            j = self._new_job(key, spec, kind=kind, group=group, label=label, origin=origin)
            j["seq"] = self._next_seq(front=(position == "next"))
            q["jobs"].append(j)
            self._estimate(j)
            self._trim(q)
            self._save(key)
            self._changed(j)
            self._cv.notify_all()
        return dict(j)

    def submit_now(self, design_dir: Any, spec: Dict[str, Any], *, kind: str = "run",
                   group: Optional[str] = None, label: Optional[str] = None,
                   initial_state: Optional[str] = None, branched_from: Optional[Dict[str, Any]] = None,
                   extra: Optional[Dict[str, Any]] = None) -> Tuple[Any, int]:
        """Start a run now in the calling thread (a Run click): the same
        reply ``_start_run`` gives, with ``job_id``. With no free slot the
        reply is ``{ok: false, started: false, busy: true, reason}`` (the page
        offers Add to queue)."""
        key = design_key(design_dir)
        facts = self.facts()
        with self._lock:
            cap = self.capacity(facts, design_dir=key)
            active = self._active()
            probe = self._new_job(key, spec, kind=kind, group=group, label=label, origin=None)
            why = self._wait_reason(probe, facts, cap, active, 0)
            if why and why != "Queue paused" and not why.startswith("Stops after"):
                return {"ok": False, "started": False, "busy": True, "reason": why,
                        "running": len(active), "max_parallel": cap["now"]}, 200
            q = self.queue_of(key)
            j = probe
            j["seq"] = self._next_seq(front=True)
            if extra:
                j.update(extra)
            q["jobs"].append(j)
            self._trim(q)
            self._estimate(j)
        reply, status = self._launch(j, initial_state=initial_state, branched_from=branched_from, share_k=len(active) + 1)
        if isinstance(reply, dict):
            reply = dict(reply)
            reply["job_id"] = j["id"]
        return reply, status

    def check_only(self, design_dir: Any, spec: Dict[str, Any], *, kind: str = "run") -> Tuple[Any, int]:
        """The config check a start runs, without starting (before queueing)."""
        if not self._launcher:
            return {"ok": True}, 200
        j = self._new_job(design_key(design_dir), spec, kind=kind, group=None, label=None, origin=None)
        return self._launcher(j, None, None, True)

    # ---------------------------------------------------------------- launch
    def _launch(self, j: Dict[str, Any], *, initial_state: Optional[str] = None,
                branched_from: Optional[Dict[str, Any]] = None, share_k: int = 1) -> Tuple[Any, int]:
        """Start *j* (``queued`` → ``starting`` → ``running``). Runs the
        launcher outside the lock: it reads the config through LibreLane."""
        key = j["design_dir"]
        with self._lock:
            if j["id"] in self._starting:
                return {"ok": False, "started": False, "reason": "already starting"}, 409
            self._starting.add(j["id"])
            j["state"] = "starting"
            j["reason"] = "Starting…"
            j["started_at"] = now_iso()
            from .events import BOOT_ID
            j["boot"] = BOOT_ID
            self._latest[key] = j["id"]
            self._save(key)
            self._changed(j)
        facts = self.facts()
        share = share_of(facts.get("cores"), facts.get("free_gb"), max(1, share_k))
        runner = self._runner_factory(j) if self._runner_factory else None
        if runner is not None:
            self._runners[j["id"]] = runner
            try:
                runner.set_listener(self._listener_for(j["id"]))
            except Exception:
                pass
        reply: Any
        status = 500
        try:
            if not self._launcher:
                raise RuntimeError("no launcher")
            reply, status = self._launcher(j, runner, share, False,
                                           initial_state=initial_state, branched_from=branched_from)
        except Exception as ex:
            _log.exception("job %s failed to start", j["id"])
            reply, status = f"The run couldn't start: {ex}", 500
        ok = status == 200 and isinstance(reply, dict) and reply.get("ok") and reply.get("started", True) is not False
        with self._lock:
            self._starting.discard(j["id"])
            if ok:
                if j["state"] == "starting":
                    j["state"] = "running"
                    j["reason"] = ""
                j["tag"] = reply.get("tag") or j.get("tag")
                if j["tag"]:
                    j["run_dir"] = str(Path(key) / "runs" / j["tag"])
                j["container"] = getattr(runner, "container_name", None) or j.get("container")
                j["resources"] = dict(j.get("resources") or {}, threads=(reply.get("resources") or {}).get("values") or {},
                                      need_gb=need_gb(design_peak(key).get("gb")))
                j["command"] = reply.get("command")
                j["attempts"] = list(j.get("attempts") or []) + [
                    {"at": j["started_at"], "how": "continue" if j["kind"] == "continue" else "start",
                     "from_step": (j.get("spec") or {}).get("frm")}]
            else:
                msg = reply if not isinstance(reply, dict) else (reply.get("reason") or reply.get("error") or "the run didn't start")
                j["state"] = "failed"
                j["reason"] = "Didn't start: " + str(msg)[:400]
                j["ended_at"] = now_iso()
                j["outcome"] = {"result": "not-started", "message": str(msg)[:2000]}
                if isinstance(reply, dict) and reply.get("config_check"):
                    j["outcome"]["config_check"] = reply.get("config_check")
                self._runners.pop(j["id"], None)
            self._save(key)
            self._changed(j)
            self._cv.notify_all()
        if not ok:
            self._fire_end(j)
        return reply, status

    def _listener_for(self, job_id: str) -> Callable[[str, Dict[str, Any]], None]:
        def listen(kind: str, body: Dict[str, Any]) -> None:
            try:
                self._on_run_event(job_id, kind, body)
            except Exception:
                _log.debug("queue listener failed", exc_info=True)
        return listen

    def _on_run_event(self, job_id: str, kind: str, body: Dict[str, Any]) -> None:
        """A run's own events move its job: steps, a step-by-step pause, the end."""
        with self._lock:
            j = self.job(job_id)
            if j is None:
                return
            changed = False
            if kind == "step_started":
                st = {"id": body.get("step_id"), "index": body.get("index"), "total": body.get("total"),
                      "started": time.time()}
                j["step"] = st
                if j["state"] in ("starting", "paused", "queued"):
                    j["state"] = "running"
                    j["reason"] = ""
                changed = True
            elif kind == "progress":
                if j.get("step") is not None:
                    j["step"]["done"] = body.get("done")
                    j["step"]["total"] = body.get("total") or j["step"].get("total")
            elif kind == "info" and body.get("paused"):
                j["state"] = "paused"
                nxt = body.get("next_step")
                j["reason"] = "Paused between steps" + (f" · next: {nxt}" if nxt else "") + ". Press Next step to go on."
                changed = True
            elif kind == "flow_done":
                cancelled = bool(body.get("cancelled"))
                err = body.get("error")
                j["state"] = "stopped" if cancelled else ("failed" if err else "done")
                j["ended_at"] = now_iso()
                j["outcome"] = {"result": "cancelled" if cancelled else ("error" if err else "ok"),
                                "message": (str(err)[:2000] if err else None)}
                j["reason"] = "Stopped by you" if cancelled else (f"Failed: {str(err)[:200]}" if err else "Finished")
                j["remaining_s"] = 0
                j["_finalize"] = True
                changed = True
            if changed:
                self._save(j["design_dir"])
                self._changed(j)
                self._cv.notify_all()

    def _finalize(self, j: Dict[str, Any]) -> None:
        """After a run ends: read how it really went from its folder, record
        the peak memory, call the end hooks. Off the lock (reads files)."""
        j.pop("_finalize", None)
        rd = j.get("run_dir")
        if rd and self._outcome_reader and j["state"] in ("done", "failed"):
            try:
                o = self._outcome_reader(rd) or {}
                j["outcome"] = {**(j.get("outcome") or {}), **{k: o.get(k) for k in
                                ("ok", "good", "kind", "stage", "step", "reason", "verdict") if k in o}}
                if j["state"] == "done":
                    j["reason"] = "Finished · " + done_words(j.get("spec") or {}, o)
                elif o.get("stage"):
                    j["reason"] = f"Failed at {o.get('stage')}" + (f": {o.get('reason')}" if o.get("reason") else "")
            except Exception:
                _log.debug("outcome reader failed", exc_info=True)
        m = self._mem.pop(j["id"], None)
        if rd and m and m.get("peak_gb"):
            j.setdefault("resources", {})["peak_gb"] = m["peak_gb"]
            _record_peak(rd, m["peak_gb"], m.get("src") or "container")
            _PEAKS.pop(design_key(j["design_dir"]), None)
        self._runners.pop(j["id"], None)
        with self._lock:
            self._save(j["design_dir"])
            self._changed(j)
        self._fire_end(j)

    def _fire_end(self, j: Dict[str, Any]) -> None:
        for fn in list(self._end_hooks):
            try:
                fn(dict(j))
            except Exception:
                _log.debug("job end hook failed", exc_info=True)
        with self._lock:
            self._cv.notify_all()

    # ---------------------------------------------------------------- admission
    def tick(self) -> None:
        """One pass: finish ended runs, start what may start, sample memory,
        publish. Called every second by the service thread."""
        with self._lock:
            ending = [j for j in self._all_jobs() if j.get("_finalize")]
            recover = [k for k, q in self._queues.items() if q.pop("_recover", False)]
        if recover:
            # A queue first read after start-up (a project opened later) that
            # lists runs going from an earlier boot.
            try:
                self.recover_all(recover)
            except Exception:
                _log.exception("queue recovery failed")
        for j in ending:
            self._finalize(j)
        self._sample()
        self._admit()
        self._auto_stop_after()
        self._estimates()
        self._publish()
        self._power()

    def _waiting_sorted(self) -> List[Dict[str, Any]]:
        return sorted((j for j in self._all_jobs() if j.get("state") == "queued"),
                      key=lambda j: (float(j.get("seq") or 0), j.get("queued_at") or ""))

    def _admit(self) -> None:
        for _ in range(FIXED_CAP):
            facts = self.facts()
            pick = None
            with self._lock:
                active = self._active()
                waiting = self._waiting_sorted()
                for i, j in enumerate(waiting):
                    cap = self.capacity(facts, design_dir=j["design_dir"])
                    why = self._wait_reason(j, facts, cap, active, i)
                    if why is None:
                        pick = j
                        break
                    if j.get("reason") != why:
                        j["reason"] = why
                        self._changed(j)
                    # Strict order: a job that can't start blocks the ones after
                    # it, except where only its own project's limits hold it.
                    if not (why.startswith("First runs") or why.startswith("Local-tools")
                            or why.startswith("The project folder") or why == "Queue paused"):
                        break
            if pick is None:
                return
            if pick.get("resume_pending"):
                self._resume_now(pick)
            else:
                self._launch(pick, share_k=len(active) + 1)

    def _auto_stop_after(self) -> None:
        with self._lock:
            if not any(q.get("stop_after_current") for q in self._queues.values()):
                return
            if self._active():
                return
            for k, q in self._queues.items():
                if q.get("stop_after_current"):
                    q["stop_after_current"] = False
                    q["paused"] = True
                    self._save(k)
            self._dirty = True

    def _sample(self) -> None:
        now = time.monotonic()
        if now - self._last_sample < SAMPLE_EVERY_S:
            return
        self._last_sample = now
        with self._lock:
            targets = [(j["id"], j.get("container") or getattr(self._runners.get(j["id"]), "container_name", None))
                       for j in self._active() if j.get("engine") == "container"]
        for jid, name in targets:
            if not name:
                continue
            cache = self._mem.setdefault(jid, {})
            m = container_memory(name, cache)
            if m:
                cache.update(m)
                cache["peak_gb"] = max(float(cache.get("peak_gb") or 0), float(m.get("peak_gb") or 0))
                with self._lock:
                    j = self.job(jid)
                    if j is not None:
                        r = dict(j.get("resources") or {})
                        r["rss_gb"] = m.get("rss_gb")
                        r["peak_gb"] = cache["peak_gb"]
                        j["resources"] = r
                        if not j.get("container"):
                            j["container"] = name
                        self._dirty = True

    def _estimate(self, j: Dict[str, Any]) -> None:
        spec = j.get("spec") or {}
        times = step_times(j["design_dir"])
        to = spec.get("to") or (spec.get("only") or None)
        frm = spec.get("frm") or (spec.get("only") or None)
        est = estimate_for(times, frm=frm, to=to)
        j["estimate"] = {"seconds": est, "basis": times.get("basis")} if est else None

    def _estimates(self) -> None:
        with self._lock:
            for j in self._active():
                st = j.get("step") or {}
                r = self._runners.get(j["id"])
                times = step_times(j["design_dir"])
                if not times.get("order"):
                    j["remaining_s"] = None
                    continue
                statuses = getattr(r, "step_statuses", {}) if r is not None else {}
                done = [sid for sid, s in statuses.items() if s in ("done", "skipped")]
                cur = st.get("id")
                el = time.time() - float(st.get("started") or time.time())
                spec = j.get("spec") or {}
                left = estimate_for(times, frm=spec.get("frm"), to=spec.get("to"), done=done,
                                    current=cur, current_elapsed=el)
                base = (times.get("secs") or {}).get(slug(cur)) if cur else None
                if base and el > 1.5 * float(base) + 30:
                    j["slow"] = True
                    left = None
                else:
                    j.pop("slow", None)
                if left is not None:
                    prev = j.get("remaining_s")
                    # Only move the number when it changed by 30 s or more:
                    # an ETA that ticks every second reads as false precision.
                    if prev is None or abs(float(prev) - left) >= 30:
                        j["remaining_s"] = left
                        self._dirty = True
                else:
                    j["remaining_s"] = None

    # ---------------------------------------------------------------- controls
    def move(self, job_id: str, to: Any) -> Dict[str, Any]:
        with self._lock:
            waiting = self._waiting_sorted()
            j = next((x for x in waiting if x["id"] == job_id), None)
            if j is None:
                raise KeyError("That run is not waiting in the queue any more.")
            order = [x for x in waiting if x["id"] != job_id]
            i = waiting.index(j)
            if to == "top":
                pos = 0
            elif to == "bottom":
                pos = len(order)
            elif to == "up":
                pos = max(0, i - 1)
            elif to == "down":
                pos = min(len(order), i + 1)
            else:
                pos = max(0, min(len(order), int(to)))
            order.insert(pos, j)
            base = float(waiting[0].get("seq") or time.time()) if waiting else time.time()
            for n, x in enumerate(order):
                x["seq"] = base + n
            for k in {x["design_dir"] for x in order}:
                self._save(k)
            self._dirty = True
            self._cv.notify_all()
            return {"position": pos}

    def remove(self, *, job_id: Optional[str] = None, group: Optional[str] = None,
               finished: bool = False, reason: str = "Removed from the queue") -> List[str]:
        """Take waiting jobs out of the queue (and ended ones, with
        *finished*). A running job is never removed: stop it first. A
        step-by-step run waiting for a slot for its next step has a run going
        (its folder, its runner): it is stopped, never just dropped."""
        gone: List[str] = []
        step_runs: List[str] = []
        with self._lock:
            for key, q in self._queues.items():
                keep = []
                for j in q["jobs"]:
                    hit = (job_id and j["id"] == job_id) or (group and j.get("group") == group)
                    if finished and j.get("state") in ENDED:
                        gone.append(j["id"])
                        continue
                    if hit and j.get("state") == "queued" and j.get("resume_pending"):
                        step_runs.append(j["id"])
                    elif hit and j.get("state") == "queued":
                        gone.append(j["id"])
                        j["state"] = "skipped"
                        j["reason"] = reason
                        j["ended_at"] = now_iso()
                        self._changed(j)
                        self._fire_end_later(j)
                        continue
                    keep.append(j)
                if len(keep) != len(q["jobs"]):
                    q["jobs"] = keep
                    self._save(key)
            self._dirty = True
            self._cv.notify_all()
        for jid in step_runs:
            gone += self.stop(jid)
        return gone

    def skip_group(self, group: str, reason: str) -> List[str]:
        """Mark every waiting job of *group* skipped (kept in the list, with
        why): a sweep's stop rule."""
        out = []
        with self._lock:
            for key, q in self._queues.items():
                hit = False
                for j in q["jobs"]:
                    if j.get("group") == group and j.get("state") == "queued":
                        j["state"] = "skipped"
                        j["reason"] = reason
                        j["ended_at"] = now_iso()
                        out.append(j["id"])
                        self._changed(j)
                        self._fire_end_later(j)
                        hit = True
                if hit:
                    self._save(key)
            self._cv.notify_all()
        return out

    def _fire_end_later(self, j: Dict[str, Any]) -> None:
        snap = dict(j)
        threading.Thread(target=self._fire_end, args=(snap,), daemon=True, name="lanex.jobs.end").start()

    def pause(self, paused: bool, design_dir: Any = None) -> None:
        with self._lock:
            qs = [self.queue_of(design_dir)] if design_dir else list(self._queues.values())
            for q in qs:
                q["paused"] = bool(paused)
                if not paused:
                    q["stop_after_current"] = False
                    if q.pop("foreign", None):       # Resume is the click a foreign queue waited for
                        q.pop("notice", None)
                self._save(q["design_dir"])
            self._dirty = True
            self._cv.notify_all()

    def stop_after_current(self, on: bool) -> None:
        with self._lock:
            for q in self._queues.values():
                q["stop_after_current"] = bool(on)
                self._save(q["design_dir"])
            self._dirty = True

    def stop(self, job_id: Optional[str] = None, *, everything: bool = False) -> List[str]:
        """Stop a job: a waiting one leaves the queue, a running one is ended
        at the step it is on (its folder keeps what it finished)."""
        out: List[str] = []
        with self._lock:
            targets = [j for j in self._all_jobs() if (everything and j.get("state") in LIVE | {"queued"})
                       or (job_id and j["id"] == job_id)]
        for j in targets:
            # A step-by-step run waiting for a slot for its next step is a run
            # between two steps (paused), not a job that never started.
            step_wait = j.get("state") == "queued" and bool(j.get("resume_pending"))
            if j.get("state") == "queued" and not step_wait:
                self.remove(job_id=j["id"], reason="Stopped before it started")
                out.append(j["id"])
                continue
            r = self._runners.get(j["id"])
            if r is not None and (j.get("state") in LIVE or step_wait):
                with self._lock:
                    j.pop("resume_pending", None)
                    if step_wait:
                        j["state"] = "paused"           # holds no slot while it ends
                    if j["state"] != "paused":
                        j["state"] = "stopping"
                        j["reason"] = "Stopping…"
                    self._save(j["design_dir"])
                    self._changed(j)
                threading.Thread(target=r.cancel, daemon=True, name="lanex.jobs.stop").start()
                out.append(j["id"])
            elif step_wait:
                # Its runner is gone (recovery turns these into interrupted
                # runs, so this is a guard): end it as stopped, folder kept.
                with self._lock:
                    j.pop("resume_pending", None)
                    j["state"] = "stopped"
                    j["reason"] = "Stopped by you"
                    j["ended_at"] = now_iso()
                    j["outcome"] = {"result": "cancelled"}
                    self._save(j["design_dir"])
                    self._changed(j)
                out.append(j["id"])
            elif j.get("state") in LIVE and j.get("container"):
                # Reattached after a restart: no runner, only its container.
                # Removed only when it is listed as a LanEx flow container
                # working in this job's project (never a name from a file).
                if job_container_alive(j):
                    remove_container(j["container"])
                f = self._followers.get(j["id"])
                if f is not None:
                    f.stop()
                with self._lock:
                    j["state"] = "stopped"
                    j["reason"] = "Stopped by you"
                    j["ended_at"] = now_iso()
                    j["outcome"] = {"result": "cancelled"}
                    self._save(j["design_dir"])
                    self._changed(j)
                out.append(j["id"])
            elif j.get("state") == "interrupted":
                self.recover(j["id"], "stop")
                out.append(j["id"])
        return out

    def resume_step(self, job_id: str) -> Dict[str, Any]:
        """Next step of a paused step-by-step job: at once when a slot is
        free, else first in line."""
        facts = self.facts()
        with self._lock:
            j = self.job(job_id)
            if j is None or j.get("state") != "paused":
                raise KeyError("That run is not paused between steps.")
            cap = self.capacity(facts, design_dir=j["design_dir"])
            active = self._active()
            j["resume_pending"] = True
            why = self._wait_reason(j, facts, cap, active, 0)
            if why and why != "Queue paused":
                j["state"] = "queued"
                j["seq"] = self._next_seq(front=True)
                j["reason"] = why + " (next step)"
                self._save(j["design_dir"])
                self._changed(j)
                return {"started": False, "reason": j["reason"]}
        self._resume_now(j)
        return {"started": True}

    def _resume_now(self, j: Dict[str, Any]) -> None:
        r = self._runners.get(j["id"])
        if r is None:
            # The run's runner is gone (LanEx restarted between two steps):
            # its next step can't be sent, so it is an interrupted run.
            with self._lock:
                j.pop("resume_pending", None)
            self._mark_interrupted(j)
            return
        with self._lock:
            j.pop("resume_pending", None)
            j["state"] = "running"
            j["reason"] = ""
            self._save(j["design_dir"])
            self._changed(j)
        if r is not None:
            threading.Thread(target=r.resume, daemon=True, name="lanex.jobs.next").start()

    def set_parallel(self, value: Any) -> Any:
        if value in ("auto", None, ""):
            write_setting("queue_max_parallel", None)
            v: Any = "auto"
        else:
            v = int(value)
            if not 1 <= v <= FIXED_CAP:
                raise ValueError(f"Runs at once is 1 to {FIXED_CAP}, or Auto.")
            write_setting("queue_max_parallel", v)
        with self._lock:
            self._dirty = True
            self._cv.notify_all()
        return v

    # ---------------------------------------------------------------- recovery
    def recover_all(self, design_dirs: Iterable[Any]) -> Dict[str, Any]:
        """At server start: every job of a known project that was going when
        LanEx stopped. A container still running is followed again; anything
        else becomes ``interrupted`` with where Continue would start, and its
        project's queue comes back paused (a crash may have been the queue)."""
        from .events import BOOT_ID
        conts = None
        found = {"reattached": 0, "interrupted": 0}
        for d in design_dirs:
            try:
                if not Path(str(d)).is_dir():
                    continue
            except Exception:
                continue
            with self._lock:
                q = self.queue_of(d)
                q.pop("_recover", None)
                stale = [j for j in q["jobs"] if j.get("boot") != BOOT_ID and
                         (j.get("state") in LIVE or (j.get("state") == "queued" and j.get("resume_pending")))]
            if not stale:
                continue
            if conts is None:
                conts = {c["name"]: c for c in flow_containers()}
            for j in stale:
                name = j.get("container")
                # Followed again only when that container really works in this
                # project: a queue file can name any container.
                if name and _works_in(conts.get(name), j["design_dir"]) and j.get("state") != "paused" \
                        and not j.get("resume_pending"):
                    self._reattach(j)
                    found["reattached"] += 1
                else:
                    self._mark_interrupted(j)
                    found["interrupted"] += 1
            with self._lock:
                q["paused"] = True if any(j.get("state") == "interrupted" for j in q["jobs"]) else q["paused"]
                self._save(q["design_dir"])
        if conts is None:
            conts = {c["name"]: c for c in flow_containers()}
        known = {j.get("container") for j in self._all_jobs() if j.get("container")}
        self._orphans = [c for n, c in conts.items() if n not in known]
        self._dirty = True
        return found

    def _reattach(self, j: Dict[str, Any]) -> None:
        from .events import BOOT_ID
        with self._lock:
            j["boot"] = BOOT_ID
            j["state"] = "running"
            j["reattached"] = True
            j["reason"] = "LanEx restarted while this run kept going in its container. It's followed again."
            self._save(j["design_dir"])
            self._changed(j)
        try:
            from . import termwatch
            f = termwatch.Follower(j["design_dir"], j.get("tag") or "", job_id=j["id"], origin=j.get("origin") or "lanex",
                                   container=j.get("container"), on_end=self._follower_end)
            self._followers[j["id"]] = f
            # Its steps move the job as a runner's would (the chip, the ETA).
            listen = self._listener_for(j["id"])
            termwatch._LISTENERS[j["id"]] = lambda kind, body: listen(kind, body) if kind != "flow_done" else None
            f.start()
        except Exception:
            _log.exception("could not follow %s again", j.get("id"))

    def _follower_end(self, job_id: str, result: Dict[str, Any]) -> None:
        f = self._followers.pop(job_id, None)
        try:
            from . import termwatch
            termwatch._LISTENERS.pop(job_id, None)
        except Exception:
            pass
        if f is not None:
            _note_outcome(f.run_dir, {"result": "ok" if result.get("result") == "ok" else "error",
                                      "error": None if result.get("result") == "ok" else result.get("message"),
                                      "how": "followed after a restart"})
        with self._lock:
            j = self.job(job_id)
            if j is None or j.get("state") not in LIVE:
                return
            ok = result.get("result") == "ok"
            j["state"] = "done" if ok else "failed"
            j["outcome"] = dict(result)
            j["ended_at"] = now_iso()
            j["reason"] = "Finished" if ok else ("Ended: " + str(result.get("message") or "the run stopped"))
            j["_finalize"] = True
            self._cv.notify_all()
        del f

    def _mark_interrupted(self, j: Dict[str, Any]) -> None:
        info = self.resume_info(j)
        with self._lock:
            j["state"] = "interrupted"
            j["ended_at"] = j.get("ended_at") or now_iso()
            j["resume"] = info
            done = len(info.get("done_steps") or [])
            at = info.get("from_step")
            j["reason"] = ("LanEx stopped while this run was going" +
                           (f": {done} steps had finished" if done else "") +
                           (f". Continue starts at {at}." if at else "."))
            self._save(j["design_dir"])
            self._changed(j)

    def resume_info(self, j: Dict[str, Any]) -> Dict[str, Any]:
        """Where an interrupted run continues, and whether it safely can."""
        rd = j.get("run_dir") or (str(Path(j["design_dir"]) / "runs" / j["tag"]) if j.get("tag") else None)
        out: Dict[str, Any] = {"run_dir": rd, "from_step": None, "done_steps": [], "state": None,
                               "drift": [], "can_continue": False, "reason": ""}
        if not rd or not Path(rd).is_dir():
            out["reason"] = "Its run folder is not there any more."
            return out
        prog = run_progress(rd)
        out["done_steps"] = [d[2] for d in prog["done"]]
        if prog["complete"]:
            out["reason"] = "It had finished: its log ends with Flow complete."
            out["complete"] = True
            return out
        meta = _gui_meta(rd)
        # The folder must be this job's own (a queue file can name any folder).
        owners = {meta.get("job_id")} | {a.get("job_id") for a in (meta.get("attempts") or [])
                                         if isinstance(a, dict)}
        spec = j.get("spec") or {}
        if j.get("id") not in owners and not (spec.get("_continue_of") and spec["_continue_of"] in owners):
            out["reason"] = ("Its run folder isn't this run's own (another run, or one LanEx didn't start, "
                             "wrote it), so LanEx won't continue in it.")
            return out
        steps = self._flow_steps(j)
        last = prog["last_done"][1] if prog["last_done"] else None
        only = spec.get("only") or None
        if only:
            # "Only one step": it continues as that same step, from its own
            # starting state, or it is done.
            if last and (slug(only) == last or re.sub(r"-\d+$", "", last) == slug(only)):
                out["reason"] = "It had finished the one step it was asked to run."
                out["complete"] = True
                return out
            nxt, state = only, spec.get("initial_state") or None
            out["keep"] = {"only": only}
        elif last is None:
            # Nothing finished in this folder yet: it starts again where the
            # job itself started (its own -i state, start and stop).
            nxt = spec.get("frm") or (steps[0] if steps else None)
            state = spec.get("initial_state") or None
        else:
            nxt = step_after(steps, last) if steps else None
            state = prog["state_out"]
        out["from_step"] = nxt
        out["state"] = state
        if spec.get("to") and nxt and steps and spec["to"] in steps and nxt in steps \
                and steps.index(nxt) > steps.index(spec["to"]):
            out["reason"] = "It had reached the stage it was asked to stop at."
            out["complete"] = True
            return out
        if state:
            try:
                from . import branch
                miss = branch.missing_paths(Path(state))
                if miss:
                    out["reason"] = "The files it needs to continue are gone (" + ", ".join(miss[:2]) + ")."
                    return out
            except Exception:
                pass
        try:
            from . import branch
            # LibreLane, the image and the PDK the run was made with, read the
            # way the fix sheet reads them (gui-run.json's toolchain and
            # resolved.json), against what a run started now would use.
            out["drift"] = branch.version_drift(branch.run_info(Path(rd))) or []
        except Exception:
            _log.debug("version check for Continue failed", exc_info=True)
        try:
            cfg = meta.get("config_file")
            if cfg and meta.get("config_hash"):
                now_hash = _hash_file(cfg)
                if now_hash and now_hash != meta.get("config_hash"):
                    out["config_changed"] = Path(cfg).name
        except Exception:
            pass
        if out["drift"]:
            what = "; ".join(f"{d.get('label')} {d.get('was')} then, {d.get('now')} now" for d in out["drift"])
            out["reason"] = (f"The tools changed since it started ({what}), so it can't continue in place: "
                             "one run would mix the two. Run it again from the start instead.")
            return out
        if out.get("config_changed"):
            out["reason"] = (f"{out['config_changed']} changed after this run started, so continuing in place "
                             "would mix two settings in one run.")
            return out
        if nxt is None:
            out["reason"] = "LanEx couldn't tell which step comes next."
            return out
        out["can_continue"] = True
        return out

    def _still_going(self, j: Dict[str, Any]) -> Optional[str]:
        """Whether an interrupted run may in fact still be going (a slow or
        failing engine at start-up marks every run interrupted): "container"
        when its own container is alive, else why Continue must wait, or None."""
        try:
            conts = flow_containers()
        except Exception:
            conts = []
        if j.get("container") and any(c.get("name") == j["container"] and _works_in(c, j["design_dir"])
                                      for c in conts):
            return "container"
        with self._lock:
            mine = {x.get("container") for x in self._all_jobs() if x.get("state") in LIVE and x.get("container")}
        other = [c for c in conts if _works_in(c, j["design_dir"]) and c.get("name") not in mine]
        if other:
            return (f"A LanEx run container ({other[0]['name']}) is still working in "
                    f"{Path(j['design_dir']).name}, so this run may still be going. Wait for it to end or "
                    "stop it from the queue, then Continue.")
        rd = j.get("run_dir") or (str(Path(j["design_dir"]) / "runs" / j["tag"]) if j.get("tag") else None)
        try:
            age = time.time() - (Path(rd) / "flow.log").stat().st_mtime if rd else None
        except OSError:
            age = None
        if age is not None and age < CONTINUE_QUIET_S:
            return (f"Its flow.log changed {int(max(0, age))} s ago, so something may still be writing this "
                    "run. Wait a minute, then Continue.")
        return None

    def _flow_steps(self, j: Dict[str, Any]) -> List[str]:
        try:
            from . import configcheck
            rd = j.get("run_dir")
            cfg = None
            if rd:
                try:
                    meta = json.loads((Path(rd) / "gui-run.json").read_text(encoding="utf-8"))
                    cfg = meta.get("config_file")
                except Exception:
                    cfg = None
            if not cfg:
                for ext in ("json", "yaml", "yml", "tcl"):
                    p = Path(j["design_dir"]) / f"config.{ext}"
                    if p.is_file():
                        cfg = str(p)
                        break
            if not cfg:
                return []
            sel = configcheck.resolve_flow([cfg], (j.get("spec") or {}).get("flow") or None)
            cls = sel.get("cls")
            return [s.id for s in (getattr(cls, "Steps", None) or [])]
        except Exception:
            return []

    def recover(self, job_id: str, action: str) -> Dict[str, Any]:
        """Continue an interrupted job in its own folder, or mark it stopped."""
        with self._lock:
            j = self.job(job_id)
            if j is None:
                raise KeyError("That run is not in the queue any more.")
            if j.get("state") != "interrupted":
                raise ValueError("That run is not interrupted.")
        if action == "stop":
            with self._lock:
                j["state"] = "stopped"
                j["reason"] = "Marked as stopped. What it finished stays in its folder."
                self._save(j["design_dir"])
                self._changed(j)
            if j.get("run_dir"):
                _note_outcome(j["run_dir"], {"result": "cancelled", "error": None, "how": "interrupted"})
            return {"ok": True, "state": "stopped"}
        if action != "continue":
            raise ValueError("action is continue or stop")
        info = self.resume_info(j)
        if not info.get("can_continue"):
            raise ValueError(info.get("reason") or "This run can't continue in place.")
        alive = self._still_going(j)
        if alive == "container":
            # Its container is still working: follow it again instead.
            self._reattach(j)
            return {"ok": True, "state": "running", "reattached": True,
                    "reason": "It is still running in its container: LanEx follows it again."}
        if alive:
            raise ValueError(alive)
        spec = dict(j.get("spec") or {})
        keep = info.get("keep") or {}
        spec.update({"tag": j.get("tag"), "frm": None if keep.get("only") else info["from_step"],
                     "checks_ack": True, "only": keep.get("only"), "_continue_of": j["id"]})
        nj = self.add(j["design_dir"], spec, kind="continue", group=j.get("group"),
                      label=f"Continue {j.get('tag')} from {info['from_step']}", position="next",
                      origin=j.get("origin"))
        with self._lock:
            k = self.job(nj["id"])
            if k is not None:
                k["_initial_state"] = info.get("state")
                k["spec"]["initial_state"] = info.get("state")
            j["state"] = "stopped"
            j["reason"] = f"Continued as a new attempt in the same folder ({nj['id']})."
            q = self.queue_of(j["design_dir"])
            q["paused"] = False
            self._save(j["design_dir"])
            self._changed(j)
            self._cv.notify_all()
        return {"ok": True, "job": nj}

    def stop_orphan(self, name: str) -> bool:
        ok = remove_container(name)
        self._orphans = [c for c in self._orphans if c.get("name") != name]
        self._dirty = True
        return ok

    # ---------------------------------------------------------------- snapshot + events
    def _trim(self, q: Dict[str, Any]) -> None:
        ended = [j for j in q["jobs"] if j.get("state") in ENDED]
        if len(ended) > KEEP_ENDED:
            drop = {j["id"] for j in sorted(ended, key=lambda j: j.get("ended_at") or "")[:len(ended) - KEEP_ENDED]}
            q["jobs"] = [j for j in q["jobs"] if j["id"] not in drop]

    def _changed(self, j: Dict[str, Any]) -> None:
        self._dirty = True
        try:
            from .events import bus
            bus.emit("queue:job", {"job": self.public(j)})
        except Exception:
            pass

    def public(self, j: Dict[str, Any]) -> Dict[str, Any]:
        out = self._persistable(j)
        out["project"] = Path(j.get("design_dir") or "").name
        return out

    def snapshot(self) -> Dict[str, Any]:
        facts = self.facts()
        with self._lock:
            jobs = [self.public(j) for j in self._all_jobs()]
            for f in list(self._followers.values()):
                if f.origin == "terminal" and f.alive:
                    jobs.append(f.public())
            active = self._active()
            cap = self.capacity(facts)
            waiting = self._waiting_sorted()
            run_left = [j.get("remaining_s") for j in active]
            wait_est = [((j.get("estimate") or {}).get("seconds")) for j in waiting]
            total = list_schedule(run_left, wait_est, cap["now"]) if (active or waiting) else None
            unknown = sum(1 for x in run_left + wait_est if x is None)
            paused = bool(self._queues) and any(q.get("paused") for q in self._queues.values()) and \
                all(q.get("paused") for q in self._queues.values() if any(j.get("state") == "queued" for j in q["jobs"])) \
                and any(j.get("state") == "queued" for j in self._all_jobs())
            snap = {
                "jobs": sorted(jobs, key=lambda x: (STATES.index(x.get("state")) if x.get("state") in STATES else 99,
                                                    float(x.get("seq") or 0))),
                "running": len(active),
                "waiting": len(waiting),
                "terminal": sum(1 for f in self._followers.values() if f.origin == "terminal" and f.alive),
                "interrupted": sum(1 for j in self._all_jobs() if j.get("state") == "interrupted"),
                "paused": paused,
                "any_paused": any(q.get("paused") for q in self._queues.values()),
                "stop_after_current": any(q.get("stop_after_current") for q in self._queues.values()),
                "max_parallel": {"setting": cap["setting"], "now": cap["now"], "why": cap["why"],
                                 "auto": cap["auto"]["n"], "cpu_slots": cap["auto"]["cpu_slots"],
                                 "mem_slots": cap["auto"]["mem_slots"]},
                "memory": {"free_gb": facts.get("free_gb"), "total_gb": facts.get("total_gb"),
                           "reserve_gb": cap["auto"]["reserve_gb"], "used_by_runs_gb": cap["used_gb"],
                           "need_gb": cap["need_gb"]},
                "cores": facts.get("cores"),
                "eta": {"seconds": total, "known": unknown == 0 and total is not None, "unknown_count": unknown},
                "orphans": list(self._orphans),
                "notices": [q["notice"] for q in self._queues.values() if q.get("notice")],
            }
        try:
            from . import power
            snap["hold"] = power.state()
        except Exception:
            snap["hold"] = {"hold": False, "reason": ""}
        from .events import BOOT_ID
        snap["boot"] = BOOT_ID
        return snap

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not (self._dirty or force) or now - self._last_emit < EMIT_EVERY_S:
            return
        self._dirty = False
        self._last_emit = now
        try:
            from .events import bus
            bus.emit("queue:changed", self.snapshot())
        except Exception:
            _log.debug("queue publish failed", exc_info=True)

    def hold_wanted(self) -> Tuple[bool, str, int]:
        """Whether the PC should stay awake: a run going, or waiting in a
        queue that isn't paused (and the run asked for it)."""
        with self._lock:
            going = [j for j in self._all_jobs() if j.get("state") in ACTIVE
                     and (j.get("spec") or {}).get("keep_awake", True) is not False]
            waiting = [j for j in self._all_jobs() if j.get("state") == "queued"
                       and not self.queue_of(j["design_dir"]).get("paused")
                       and (j.get("spec") or {}).get("keep_awake", True) is not False]
        term = [f for f in self._followers.values() if f.alive]
        n = len(going) + len(term)
        if not n and not waiting:
            return False, "", 0
        names = sorted({Path(j["design_dir"]).name for j in going} | {Path(f.design_dir).name for f in term})
        bits = []
        if n:
            bits.append(f"{n} run{'s' if n != 1 else ''} going" + (f" ({', '.join(names[:3])})" if names else ""))
        if waiting:
            bits.append(f"{len(waiting)} waiting")
        return True, ", ".join(bits), n + len(waiting)

    def _power(self) -> None:
        try:
            from . import power
            want, why, n = self.hold_wanted()
            power.update(want, why, n)
        except Exception:
            _log.debug("keep-awake update failed", exc_info=True)

    # ---------------------------------------------------------------- service
    def wait_job(self, job_id: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Block until *job_id* has ended; its final record (None on timeout)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while True:
                j = self.job(job_id)
                if j is None:
                    return None
                if j.get("state") in ENDED and not j.get("_finalize"):
                    return dict(j)
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return None
                self._cv.wait(timeout=min(1.0, left) if left is not None else 1.0)

    def start_service(self, design_dirs: Iterable[Any] = ()) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self.recover_all(list(design_dirs))
        except Exception:
            _log.exception("queue recovery failed")
        try:
            from . import termwatch
            termwatch.watcher.start(self)
        except Exception:
            _log.exception("terminal-run watcher did not start")
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lanex.jobs.scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception:
                _log.exception("scheduler tick failed")
            with self._lock:
                self._cv.wait(timeout=TICK_S)

    def stop_service(self) -> None:
        self._halt.set()
        with self._lock:
            self._cv.notify_all()

    def attach_follower(self, job_id: str, follower: Any) -> None:
        self._followers[job_id] = follower
        self._dirty = True

    def detach_follower(self, job_id: str) -> None:
        self._followers.pop(job_id, None)
        self._dirty = True

    def owns_run_dir(self, run_dir: str) -> bool:
        k = design_key(run_dir)
        with self._lock:
            for j in self._all_jobs():
                if j.get("run_dir") and design_key(j["run_dir"]) == k and j.get("state") in LIVE | {"queued"}:
                    return True
        f = [x for x in self._followers.values() if design_key(x.run_dir) == k]
        return bool(f)


def done_words(spec: Dict[str, Any], o: Dict[str, Any]) -> str:
    """How a finished job went, for the queue row: "checks passed" only for a
    whole flow Verify passed (``good``). A run of part of the flow says what
    ran; a flow that finished with problems says so. Pure."""
    if o.get("label"):
        return str(o["label"])
    part = None
    if spec.get("only"):
        part = f"only {spec['only']} ran"
    elif spec.get("to"):
        part = f"ran up to {spec['to']}"
    if part:
        return part if o.get("ok") else part + ", with problems" + (f": {o['reason']}" if o.get("reason") else "")
    if o.get("good"):
        return "checks passed"
    if o.get("ok"):
        if o.get("verdict") == "warning":
            return "with warnings (see Verify)"
        return "not every check passed (see Verify)"
    return str(o.get("reason") or "with problems")


def label_for(spec: Dict[str, Any], kind: str) -> str:
    """What the queue calls a job, from its spec."""
    sw = spec.get("sweep") if isinstance(spec.get("sweep"), dict) else None
    if kind == "point" and sw:
        vals = sw.get("values") or {}
        return "Sweep " + ", ".join(f"{k} = {v}" for k, v in vals.items()) if vals else "Sweep point"
    if spec.get("only"):
        return f"Only {spec['only']}"
    if spec.get("frm") and spec.get("to"):
        return f"Run {spec['frm']} to {spec['to']}"
    if spec.get("frm"):
        return f"Run from {spec['frm']}"
    if spec.get("to"):
        return f"Run to {spec['to']}"
    if spec.get("mode") == "semi":
        return "Run step by step"
    return "Run"


def _hash_file(path: str) -> Optional[str]:
    try:
        import hashlib
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


def _gui_meta(run_dir: str) -> Dict[str, Any]:
    try:
        d = json.loads((Path(run_dir) / "gui-run.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_meta(run_dir: str, meta: Dict[str, Any]) -> None:
    try:
        from . import platform_env
        p = Path(run_dir) / "gui-run.json"
        if not p.parent.is_dir():
            return
        platform_env.atomic_write_text(p, json.dumps(meta, indent=2, default=str) + "\n")
    except Exception:
        _log.debug("could not update gui-run.json", exc_info=True)


def _record_peak(run_dir: str, peak_gb: float, src: str) -> None:
    meta = _gui_meta(run_dir)
    if not meta:
        return
    r = dict(meta.get("resources") or {})
    r["peak_gb"] = round(float(peak_gb), 3)
    r["peak_source"] = src
    meta["resources"] = r
    _write_meta(run_dir, meta)


def _note_outcome(run_dir: str, outcome: Dict[str, Any]) -> None:
    meta = _gui_meta(run_dir)
    if not meta:
        return
    meta["outcome"] = {**outcome, "ended_at": now_iso()}
    _write_meta(run_dir, meta)


def run_row_origin(run_dir: Any) -> Dict[str, Any]:
    """``{started_by, job_id}`` for a Runs row (lane 2 shows it): "sweep" for a
    sweep point, "lanex" for any run LanEx started (it writes gui-run.json),
    "terminal" for a run folder LanEx never wrote into."""
    meta = _gui_meta(str(run_dir))
    if not meta:
        return {"started_by": "terminal", "job_id": None}
    if isinstance(meta.get("sweep"), dict) and meta["sweep"].get("id"):
        return {"started_by": "sweep", "job_id": meta.get("job_id")}
    return {"started_by": "lanex", "job_id": meta.get("job_id")}


scheduler = Scheduler()
