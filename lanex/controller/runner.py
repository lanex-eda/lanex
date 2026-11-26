# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Drive a flow in a worker thread; stream real per-step events to a queue.

The :class:`FlowRunner` bridges LibreLane's synchronous ``flow.start()`` and
the SSE world used by the SPA. It exposes:

* a ``start()`` that returns immediately once the worker thread is up,
* an in-memory event queue the SSE handler drains,
* **cooperative cancel** that aborts cleanly at the next step boundary (and
  kills any EDA subprocess that is currently executing), and
* **live per-step status** by wrapping LibreLane's own ``FlowProgressBar``,
  so the pipeline view lights up green/red exactly as the engine advances.

The per-step status is obtained without parsing logs: LibreLane's
``SequentialFlow.run`` calls ``progress_bar.start_stage()`` /
``end_stage()`` once per step in ``flow.Steps`` order. We wrap those methods
for the duration of a run and correlate each call with the corresponding
``Step.id`` by ordinal.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .container_run import (
    ContainerLogParser,
    build_dockerized_argv,
    is_progress_bar,
    strip_ansi,
)
from . import safefs
from .events import bus
from .models import EventType, StepStatus

_log = logging.getLogger("librelane.gui.runner")


class _Cancelled(Exception):
    """Raised at a step boundary to abort a flow that the user cancelled."""


# The currently-active runner, referenced by the FlowProgressBar wrappers.
# Only one flow runs per server process at a time (``start`` guards this), so a
# module global is safe and avoids depending on librelane internals threading a
# reference through to the progress bar.
_ACTIVE_RUNNER: "Optional[FlowRunner]" = None
_PATCH_LOCK = threading.Lock()
_ORIG_PROGRESS: Dict[str, Any] = {}


def _safe_jsonable(value: Any) -> Any:
    from .models import to_json

    return to_json(value)


def _load_state(path: str) -> Any:
    """A ``state_out.json`` as LibreLane's own ``-i`` reads it
    (``flows/cli.py`` ``initial_state_cb``): every path in it must exist."""
    import json as _json

    from librelane.state import State
    with open(path, encoding="utf8") as f:
        return State.load(_json.load(f), validate_path=True)


_REC_RX = None


def error_block(lines: Sequence[str], returncode: Optional[int] = None, max_lines: int = 60) -> Dict[str, Any]:
    """The error a failing LibreLane run printed on its console, whole.

    LibreLane exits 1 when it can't load the config (it lists every problem)
    and 2 for a design failure (a FlowError, deferred checker errors). Some of
    these reach only the console, never ``flow.log`` or ``error.log`` (it removes
    its file handlers before printing the error), e.g. "One or more wildcards
    specified in SETUP_VIOLATION_CORNERS did not match any corners". Returns
    ``{text, first, kind, exit_code}``: the block from the first ERROR or
    CRITICAL record (or "The following error…", a traceback) to the end, Rich's
    80-column wrapping joined back and its source-path column dropped. Pure."""
    import re as _re
    # A Rich record: "[07:24:04] ERROR    text   file.py:12"; within the same
    # second the time is left out and the level is indented instead.
    rec = _re.compile(r"^(?:\[\d\d:\d\d:\d\d\]\s+|\s{6,14})(?P<lvl>DEBUG|VERBOSE|INFO|WARNING|ERROR|CRITICAL)\s+"
                      r"(?P<msg>.*?)\s*(?:\S+\.py:\d+)?\s*$")
    start = None
    for i, ln in enumerate(lines):
        m = rec.match(ln)
        if (m and m.group("lvl") in ("ERROR", "CRITICAL")) or "The following error was encountered" in ln \
                or ln.startswith("Traceback (most recent call last)") or _re.match(r"^\s*\[(ERROR|CRITICAL)\b", ln):
            start = i
            break
    out: List[str] = []
    if start is not None:
        for ln in lines[start:]:
            m = rec.match(ln)
            if m:
                out.append(m.group("msg"))
            elif ln.startswith(" " * 20) and out:
                # Rich wraps a record at 80 columns: a line indented 20 spaces continues it.
                out[-1] = (out[-1] + " " + ln.strip()).strip()
            elif ln.strip():
                out.append(ln.rstrip())
            if len(out) >= max_lines:
                break
    text = "\n".join(out)[:6000]
    first = next((x for x in out if x.strip() and not x.startswith("Traceback")), "")
    kind = "config" if returncode == 1 and any("config" in x.lower() for x in out[:6]) else (
        "flow" if returncode == 2 else ("error" if returncode else None))
    return {"text": text, "first": first[:400], "kind": kind, "exit_code": returncode}


def latest_state_out(run_dir: Optional[str]) -> Optional[str]:
    """The ``state_out.json`` of the highest-numbered finished step folder of
    a run (the state the next step starts from), or None."""
    if not run_dir:
        return None
    import re as _re
    best = None
    try:
        for e in os.scandir(run_dir):
            m = _re.match(r"(\d+)-", e.name)
            if not m or not e.is_dir():
                continue
            so = os.path.join(e.path, "state_out.json")
            if os.path.isfile(so) and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), so)
    except OSError:
        return None
    return best[1] if best else None


def _toolchain_provenance(run_mode: Optional[str]) -> Dict[str, Any]:
    """Identity of the toolchain that produced a run, recorded into gui-run.json.

    Answers the one question a metric alone cannot: *would I get the same numbers
    if I ran this manually?* — which depends entirely on the versions of LanEx,
    LibreLane, and (in container mode) the exact image the flow executed in.
    Without this stamp an exported/reproduced run is un-auditable across time or
    machines. Best-effort: every field degrades to ``"unknown"`` and this never
    raises (a failure here must not derail persisting the rest of the meta)."""
    tc: Dict[str, Any] = {"run_mode": run_mode or "local"}
    try:
        from . import compat
        tc["librelane_version"] = compat.get_version()
    except Exception:
        tc["librelane_version"] = "unknown"
    try:
        from .. import _version as _lanex_version
        tc["lanex_version"] = _lanex_version.get_version()
    except Exception:
        tc["lanex_version"] = "unknown"
    if (run_mode or "local") == "container":
        # The image tag is version-pinned to the installed librelane, so it names
        # the exact tool set the flow ran inside (container_run.image_ref()).
        try:
            from . import container_run
            tc["image"] = container_run.image_ref()
        except Exception:
            tc["image"] = "unknown"
    return tc


def _relativize_to(paths: Optional[Sequence[str]], base: Path) -> List[str]:
    """Return each path relative to *base* when it lives under it; otherwise keep
    it unchanged. Used for VERILOG_FILES/EXTRA_FILES so they resolve inside the
    container (cwd = mounted design dir) and don't break on a space in *base*."""
    out: List[str] = []
    for p in (paths or []):
        if not p:
            continue
        try:
            rp = Path(p)
            if rp.is_absolute():
                rp = rp.resolve()
                rel = rp.relative_to(base)
                out.append(str(rel))
                continue
        except Exception:
            pass
        out.append(str(p))
    return out


def new_container_name() -> str:
    """A name for one LibreLane container of a run, unique per invocation.

    LibreLane 3.0.4 names its container with a fresh ``uuid4`` and never
    prints it (the echo is lost in the buffered stdout before ``exec``), so
    Stop used to look for "any container that mounts the design folder" and
    removed viewer and simulation containers of the same project with it.
    LanEx now picks the name itself (``LANEX_CONTAINER_NAME``, applied by
    ``librelane_entry``) and Stop removes exactly that container."""
    import uuid

    return "lanex-flow-" + uuid.uuid4().hex[:16]


# Container names Docker and Podman accept (and nothing a shell would split).
CONTAINER_NAME_RX = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}"


def container_labels() -> str:
    """The labels a run container carries, as ``key=value`` lines: the LanEx
    home and server boot that started it, and the server's process id."""
    from . import platform_env
    from .events import BOOT_ID
    try:
        home = str(platform_env.home().expanduser().resolve())
    except Exception:
        home = os.environ.get("LANEX_HOME") or ""
    labels = {"lanex.home": home, "lanex.boot": BOOT_ID, "lanex.pid": str(os.getpid())}
    return "\n".join(f"{k}={v}" for k, v in labels.items() if v and "\n" not in v)


def _install_subprocess_patch() -> None:
    """Record every process the in-process flow starts.

    Every EDA tool of a local run is started by LibreLane's
    ``Step.run_subprocess`` (with a ``_popen_callable``, psutil.Popen by
    default). Wrapping it lets Stop end exactly the flow's own process trees:
    the server's other children (KLayout or GTKWave windows, a simulation, a
    Console command, a Tools install) are never touched."""
    with _PATCH_LOCK:
        if "run_subprocess" in _ORIG_PROGRESS:
            return
        try:
            import psutil
            from librelane.steps.step import Step
        except Exception:  # pragma: no cover - librelane missing
            return
        orig = Step.run_subprocess
        _ORIG_PROGRESS["run_subprocess"] = orig

        def run_subprocess(self: Any, cmd: Any, *args: Any, **kwargs: Any):
            r = _ACTIVE_RUNNER
            if r is None:
                return orig(self, cmd, *args, **kwargs)
            base = kwargs.pop("_popen_callable", None) or psutil.Popen

            def tracked(*pa: Any, **pk: Any):
                p = base(*pa, **pk)
                r._track_flow_proc(p)
                return p

            return orig(self, cmd, *args, _popen_callable=tracked, **kwargs)

        Step.run_subprocess = run_subprocess  # type: ignore[assignment]


def _process_tree(pid: int) -> List[Any]:
    """*pid* and every process under it, as psutil objects ([] if gone)."""
    try:
        import psutil

        p = psutil.Process(pid)
        return [p] + p.children(recursive=True)
    except Exception:
        return []


def _install_progress_patch() -> None:
    """Wrap FlowProgressBar methods so step transitions emit SSE events.

    Idempotent and reference-counted via ``_ORIG_PROGRESS``. The wrappers call
    the active runner (if any) and then defer to the original implementation so
    LibreLane's own terminal progress bar keeps working.
    """
    with _PATCH_LOCK:
        if _ORIG_PROGRESS:
            return
        try:
            from librelane.flows.flow import FlowProgressBar
        except Exception:  # pragma: no cover - librelane missing
            return

        orig_set_max = FlowProgressBar.set_max_stage_count
        orig_start = FlowProgressBar.start_stage
        orig_end = FlowProgressBar.end_stage
        _ORIG_PROGRESS.update(
            {"set_max": orig_set_max, "start": orig_start, "end": orig_end, "cls": FlowProgressBar}
        )

        def set_max_stage_count(self: Any, count: int):
            r = _ACTIVE_RUNNER
            if r is not None:
                r._on_set_max(count)
            return orig_set_max(self, count)

        def start_stage(self: Any, name: str):
            r = _ACTIVE_RUNNER
            if r is not None:
                r._on_stage_start(name)  # may raise _Cancelled to abort the flow
            return orig_start(self, name)

        def end_stage(self: Any, *args: Any, **kwargs: Any):
            r = _ACTIVE_RUNNER
            if r is not None:
                r._on_stage_end(kwargs.get("increment_ordinal", True))
            return orig_end(self, *args, **kwargs)

        FlowProgressBar.set_max_stage_count = set_max_stage_count  # type: ignore[assignment]
        FlowProgressBar.start_stage = start_stage  # type: ignore[assignment]
        FlowProgressBar.end_stage = end_stage  # type: ignore[assignment]


class FlowRunner:
    """One-shot async driver for a configured flow.

    A second ``start()`` is refused while the first is running.
    """

    def __init__(self, *, job_id: Optional[str] = None, design_dir: Optional[str] = None,
                 origin: str = "lanex") -> None:
        # Which queue job this runner is (jobs.py), and its project: every
        # event carries both, so a page follows the run it shows when several
        # go at once.
        self.job_id = job_id
        self.design_dir = design_dir
        self.origin = origin
        self._listener: Any = None
        # Container runs: the console kept on disk as it comes
        # (runs/<tag>/lanex-console.log), so a reload, "Load the full log" and
        # Download read the whole output; LibreLane's flow.log has no tool output.
        self._tee: Any = None
        self._tee_buf: List[str] = []
        # The last console lines, for the error a failing run printed (error_block).
        import collections as _collections
        self._console_tail: Any = _collections.deque(maxlen=400)
        self.low_priority = False
        self._lowered = False
        self.step_started_at: Optional[float] = None
        self._lock = threading.RLock()
        self._seq = 0
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=10000)
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()  # not paused
        self._state_out: Any = None
        self._run_dir: Optional[str] = None
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._error: Optional[str] = None
        self._step_statuses: Dict[str, str] = {}
        # progress bookkeeping
        self._flow: Any = None
        self._stage_idx = -1
        self._stage_total = 0
        self._current_step_id: Optional[str] = None
        self._step_mode = False
        # ETA bookkeeping: per-step start time + this run's observed durations,
        # combined with the learned per-step history for a live "time remaining".
        self._step_start_ts: Optional[float] = None
        self._observed_durations: List[float] = []
        self._run_start_ts: Optional[float] = None
        # container run mode bookkeeping
        self._run_mode = "local"
        self._flow_name: Optional[str] = None
        self._container_proc: Optional[subprocess.Popen] = None
        # The name LanEx gave this run's current container (new_container_name),
        # and how to reach the engine that runs it (path + sg wrap).
        self._container_name: Optional[str] = None
        self._container_engine: Optional[str] = None
        self._container_sg = False
        # Local mode: the processes LibreLane started for this flow
        # (_install_subprocess_patch). Stop ends these and nothing else.
        self._flow_procs: List[Any] = []
        self._flow_procs_lock = threading.Lock()
        # container step-by-step bookkeeping: one `--only <step>` container
        # invocation per step, resuming the same run tag. ``_cstep`` holds the
        # ordered step ids + the saved invocation args; ``_awaiting_next`` is set
        # between steps (the runner stays "running" but no subprocess is live).
        self._cstep: Dict[str, Any] = {}
        self._awaiting_next = False
        # GUI run settings to persist into the run dir for reproducibility
        # (issue #4). Written once, the first time the run dir is known.
        self._gui_meta: Optional[Dict[str, Any]] = None
        self._gui_meta_written = False
        # Log lines are sent in batches (one ``log`` event carrying several
        # lines joined by "\n", which the page already splits) instead of one
        # event per line: a big run prints ~1,000-2,000 lines a second, and an
        # event each kept the server busy and the page repainting. Every other
        # event first flushes the lines before it, so the order is unchanged.
        self._emit_lock = threading.RLock()
        self._log_buf: List[str] = []
        self._log_level: Optional[str] = None
        self._log_t0 = 0.0
        self._log_cv = threading.Condition(self._emit_lock)
        self._log_flusher: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._running

    @property
    def container_name(self) -> Optional[str]:
        """The name LanEx gave this run's container (container runs)."""
        return self._container_name

    @property
    def awaiting_next(self) -> bool:
        """Step by step in a container: between two steps, no process alive."""
        return bool(self._awaiting_next)

    @property
    def current_step(self) -> Optional[str]:
        return self._current_step_id

    def set_listener(self, fn: Any) -> None:
        """``fn(kind, body)`` for every event this run sends (the queue)."""
        self._listener = fn

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def run_dir(self) -> Optional[str]:
        return self._run_dir

    @property
    def gui_meta(self) -> Optional[Dict[str, Any]]:
        """What LanEx recorded about how this run was started (the future
        ``gui-run.json``), available before the run folder exists."""
        return dict(self._gui_meta) if self._gui_meta else None

    @property
    def step_statuses(self) -> Dict[str, str]:
        return dict(self._step_statuses)

    def cancel(self) -> None:
        """Request a stop. The worker aborts at the next step boundary.

        We also terminate any EDA subprocess currently executing so a long
        OpenROAD/Magic call is interrupted instead of running to completion.

        Note: we deliberately do **not** delete the run directory — partial
        results are often exactly what the user wants to inspect.
        """
        self._cancel.set()
        self._resume.set()  # unblock a paused worker so it can see the cancel
        # The running step is killed below (its process and the container), so
        # the run ends now, not after the step.
        self._emit(EventType.INFO, {"message": "stop requested: ending the current step now"})
        self._kill_flow_processes()
        self._kill_container()
        # Container step-by-step paused between steps: no live process to kill, so
        # finalize the run here (otherwise it'd hang "running" forever).
        if self._awaiting_next:
            self._awaiting_next = False
            self._running = False
            self._emit(EventType.INFO, {"message": "flow cancelled by user"})
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "cancelled": True})

    def _kill_container(self) -> None:
        """Force-remove this run's LibreLane container, and no other.

        Killing the host-side processes is NOT enough: terminating the
        ``docker run`` client does not stop the container, and the in-container
        librelane runs as PID 1, which ignores an unhandled SIGTERM — so a
        "cancelled" containerized flow kept executing to completion (proven
        live). The container carries the name LanEx gave it
        (:func:`new_container_name`), so it is removed by that name. Containers
        of the same project that are not this flow (a KLayout or GTKWave
        viewer, a simulation) are never matched: an earlier version removed
        every container mounting the design folder, viewers included.
        """
        name = self._container_name
        if not name:
            return
        if self._container_engine:
            engines = [self._container_engine]
        else:
            engines = [e for e in (shutil.which("docker"), shutil.which("podman")) if e]
        for engine in engines:
            argv = [engine, "rm", "-f", name]
            if self._container_sg:
                try:
                    from . import tools
                    argv = tools.sg_wrap_argv(argv)
                except Exception:
                    pass
            try:
                subprocess.run(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except Exception:  # pragma: no cover - engine/platform dependent
                pass

    def _design_dir_of_run(self) -> Optional[str]:
        """The design dir this run mounts (parent of ``runs/<tag>``), resolved.

        ``_run_dir`` is assigned before the container subprocess is spawned, so
        this is available for the whole window a container could exist in.
        """
        if not self._run_dir:
            return None
        try:
            p = Path(self._run_dir).resolve()
            return str(p.parent.parent) if p.parent.name == "runs" else str(p.parent)
        except Exception:
            return None

    def _track_flow_proc(self, proc: Any) -> None:
        """Remember a process the in-process flow started (finished ones are
        dropped as new ones come, so the list stays short)."""
        with self._flow_procs_lock:
            alive = []
            for p in self._flow_procs:
                try:
                    if p.is_running():
                        alive.append(p)
                except Exception:
                    pass
            alive.append(proc)
            self._flow_procs = alive

    def _flow_process_targets(self) -> List[Any]:
        """This flow's own processes and everything under them: the tools a
        local run started, or the container client (with the ``librelane
        --dockerized`` entry and an ``sg`` wrapper) of a container run. Never
        the server's other children."""
        seen: Dict[int, Any] = {}
        with self._flow_procs_lock:
            roots = list(self._flow_procs)
        pids = []
        for p in roots:
            try:
                if p.is_running():
                    pids.append(p.pid)
            except Exception:
                pass
        proc = self._container_proc
        if proc is not None and proc.poll() is None:
            pids.append(proc.pid)
        for pid in pids:
            for q in _process_tree(pid):
                seen.setdefault(q.pid, q)
        return list(seen.values())

    def _kill_flow_processes(self) -> None:
        """End the flow's own process trees (SIGTERM, then SIGKILL after 3 s).

        Only what this run started is touched (see
        :meth:`_flow_process_targets`). An earlier version killed every child
        of the server, so a Stop also closed KLayout and GTKWave windows and
        ended simulations, Console commands and Tools installs."""
        try:
            import psutil  # librelane depends on psutil, so this is always present

            targets = self._flow_process_targets()
            for q in targets:
                try:
                    q.terminate()
                except Exception:
                    pass
            _gone, alive = psutil.wait_procs(targets, timeout=3)
            for q in alive:
                try:
                    q.kill()
                except Exception:
                    pass
        except Exception as ex:  # pragma: no cover - platform dependent
            self._emit(EventType.INFO, {"message": f"could not terminate subprocesses: {ex}"})

    def resume(self) -> None:
        # Container step-by-step: "Resume"/"Next" launches the next single-step
        # container invocation rather than releasing an in-process Event (there
        # is no live process to release between steps).
        if self._run_mode == "container" and self._step_mode and self._awaiting_next:
            self._advance_container_step()
            return
        self._resume.set()
        self._emit(EventType.INFO, {"message": "resumed"})

    @property
    def error(self) -> Optional[str]:
        """The last run's error string (``None`` on success/clean cancel).

        Public so callers like the DSE queue can tell a failed run from a
        successful one without reaching into a private attribute."""
        return self._error

    def drain(self, *, block: bool = True, timeout: float = 0.5) -> List[Dict[str, Any]]:
        """Pop the events this runner has buffered.

        SSE no longer uses this — it reads the shared :data:`events.bus` ring
        non-destructively so multiple clients can coexist. This remains for
        in-process callers/tests that want just this runner's emissions.
        """
        out: List[Dict[str, Any]] = []
        deadline = time.time() + timeout
        while True:
            try:
                ev = self._events.get(block=block, timeout=max(0.001, deadline - time.time()))
                out.append(ev)
            except queue.Empty:
                break
        return out

    # --------------------------------------------------------------- start/stop

    def start(
        self,
        *,
        flow_factory: Any,
        config_files: Sequence[str],
        design_dir: str | Path,
        pdk_root: Optional[str] = None,
        pdk: Optional[str] = None,
        scl: Optional[str] = None,
        pad: Optional[str] = None,
        tag: Optional[str] = None,
        frm: Optional[str] = None,
        to: Optional[str] = None,
        skip: Optional[Iterable[str]] = None,
        overwrite: bool = False,
        reproducible_step: Optional[str] = None,
        config_overrides: Optional[Dict[str, Any]] = None,
        extra_sources: Optional[Sequence[str]] = None,
        extra_extras: Optional[Sequence[str]] = None,
        extra_config_files: Optional[Sequence[str]] = None,
        step_mode: bool = False,
        run_mode: str = "local",
        flow_name: Optional[str] = None,
        last_run: bool = False,
        gui_meta: Optional[Dict[str, Any]] = None,
        initial_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Spawn the worker. Returns ``{"ok": bool, "reason": str}``.

        ``run_mode`` selects the engine: ``"local"`` drives LibreLane in-process
        (per-step status via the FlowProgressBar patch); ``"container"`` shells
        out to ``librelane --dockerized`` and parses its stdout. Step-by-step
        pausing is only available in local mode (there is no in-process hook
        across the container boundary).

        *initial_state* (LibreLane's ``-i``) is the ``state_out.json`` of a step
        of another run: with ``frm`` it makes a run resumed from that run's
        results under a new tag (the steps before ``frm`` are skipped).
        """
        # Source/extra paths go into VERILOG_FILES / EXTRA_FILES, which LibreLane
        # parses as WHITESPACE-separated. Host-absolute paths (a) don't exist
        # inside the container and (b) corrupt the list when the design dir has a
        # space (e.g. "…/processor codes/control.v" → two bogus paths). Both are
        # fixed by making each source relative to the design dir (= the flow base
        # / container cwd): "control.v" resolves everywhere and has no space.
        _dd = Path(design_dir).resolve()
        extra_sources = _relativize_to(extra_sources, _dd)
        extra_extras = _relativize_to(extra_extras, _dd)

        with self._lock:
            if self._running:
                return {"ok": False, "reason": "already running"}
            self._reset()
            self._running = True
            self._run_mode = "container" if run_mode == "container" else "local"
            self._flow_name = flow_name
            self._gui_meta = dict(gui_meta) if gui_meta else None
            # Step-by-step works in BOTH modes: local pauses the in-process flow
            # between steps; container runs one `--only <step>` invocation per
            # step, resuming the same run tag (see _run_container_stepwise).
            self._step_mode = bool(step_mode)
            self._cancel.clear()
            self._resume.set()
            if self._run_mode == "container" and self._step_mode:
                worker_target = self._run_container_stepwise
            elif self._run_mode == "container":
                worker_target = self._run_container
            else:
                worker_target = self._run
            self._worker = threading.Thread(
                target=worker_target,
                args=(flow_factory, list(config_files), Path(design_dir).resolve()),
                kwargs={
                    "pdk_root": pdk_root,
                    "pdk": pdk,
                    "scl": scl,
                    "pad": pad,
                    "tag": tag,
                    "frm": frm,
                    "to": to,
                    "skip": list(skip) if skip is not None else None,
                    "overwrite": overwrite,
                    "reproducible_step": reproducible_step,
                    "config_overrides": config_overrides or {},
                    "extra_sources": list(extra_sources or []),
                    "extra_extras": list(extra_extras or []),
                    "extra_config_files": list(extra_config_files or []),
                    "last_run": last_run,
                    "initial_state": initial_state,
                },
                daemon=True,
                name="librelane.gui.FlowRunner.worker",
            )
            self._worker.start()
        return {"ok": True, "reason": "started"}

    def _reset(self) -> None:
        with self._lock:
            # NOTE: deliberately do NOT reset the sequence counter. Event seq is
            # process-global + monotonic (see events.next_seq) so a reconnecting
            # browser EventSource doesn't drop the next run's events as "already
            # seen" via a stale Last-Event-ID.
            try:
                while not self._events.empty():
                    self._events.get_nowait()
            except queue.Empty:
                pass
            self._state_out = None
            self._run_dir = None
            self._error = None
            self._step_statuses = {}
            self._flow = None
            self._stage_idx = -1
            self._stage_total = 0
            self._current_step_id = None
            self._container_proc = None
            self._container_name = None
            self._container_engine = None
            self._container_sg = False
            with self._flow_procs_lock:
                self._flow_procs = []
            self._cstep = {}
            self._awaiting_next = False
            self._gui_meta_written = False

    # --------------------------------------------------------------- worker body

    # Log batching: a batch goes out after LOG_BATCH_S from its first line, at
    # LOG_BATCH_LINES lines, or before any other event, whichever comes first.
    LOG_BATCH_S = 0.15
    LOG_BATCH_LINES = 200
    _LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

    def _emit(self, kind: EventType, payload: Optional[Dict[str, Any]] = None) -> None:
        with self._emit_lock:
            if kind == EventType.LOG and payload and set(payload) <= {"message", "level"} \
                    and isinstance(payload.get("message"), str):
                self._log_buf.append(payload["message"])
                lvl = payload.get("level")
                if lvl and self._LEVELS.get(lvl, 0) > self._LEVELS.get(self._log_level or "", 0):
                    self._log_level = lvl
                if len(self._log_buf) == 1:
                    self._log_t0 = time.monotonic()
                    self._start_log_flusher()
                    self._log_cv.notify_all()
                if len(self._log_buf) >= self.LOG_BATCH_LINES:
                    self._flush_logs()
                return
            self._flush_logs()
            self._emit_now(kind, payload)

    def _flush_logs(self) -> None:
        """Send the buffered log lines as one ``log`` event (call with the lock)."""
        if not self._log_buf:
            return
        lines, self._log_buf = self._log_buf, []
        body: Dict[str, Any] = {"message": "\n".join(lines)}
        if self._log_level:
            body["level"] = self._log_level
        self._log_level = None
        self._emit_now(EventType.LOG, body)

    def _start_log_flusher(self) -> None:
        if self._log_flusher is not None and self._log_flusher.is_alive():
            return
        self._log_flusher = threading.Thread(target=self._log_flush_loop, daemon=True,
                                             name="librelane.gui.FlowRunner.logflush")
        self._log_flusher.start()

    def _log_flush_loop(self) -> None:
        """Flush a batch LOG_BATCH_S after its first line. Sleeps on the
        condition while nothing is buffered (no idle wake-ups)."""
        with self._emit_lock:
            while True:
                if not self._log_buf:
                    self._log_cv.wait()
                    continue
                left = self._log_t0 + self.LOG_BATCH_S - time.monotonic()
                if left > 0:
                    self._log_cv.wait(left)
                    continue
                self._flush_logs()

    def _emit_now(self, kind: EventType, payload: Optional[Dict[str, Any]] = None) -> None:
        seq = self._next_seq()
        body = _safe_jsonable(payload) if payload else {}
        tag = Path(self._run_dir).name if self._run_dir else None
        if self.job_id:
            body = {**body, "job_id": self.job_id, "design_dir": self.design_dir, "origin": self.origin}
        evt = {"type": kind.value, "seq": seq, "ts": time.time(), "tag": tag, **body}
        try:
            self._events.put_nowait(evt)
        except queue.Full:
            try:
                self._events.get_nowait()
                self._events.put_nowait(evt)
            except queue.Empty:
                pass
        bus.emit(kind.value, {**body, "seq": seq, "ts": evt["ts"], "tag": tag})
        if kind == EventType.FLOW_DONE:
            self._persist_outcome(body)
            self._close_tee(final=not self._awaiting_next)
        if kind == EventType.STEP_STARTED:
            self.step_started_at = time.time()
            self._lower_priority()
        fn = self._listener
        if fn is not None:
            try:
                fn(kind.value, {**body, "tag": tag})
            except Exception:
                _log.debug("run listener failed", exc_info=True)

    # ---- the console on disk (container runs)

    TEE_NAME = "lanex-console.log"
    TEE_BUF_MAX = 20000

    def _error_from_console(self, done: Dict[str, Any], returncode: int) -> None:
        """A run that exited non-zero: its console's error, whole, goes into
        the flow_done event and gui-run.json's outcome (the failure card shows
        it; some errors never reach flow.log or error.log)."""
        try:
            blk = error_block(list(self._console_tail), returncode)
        except Exception:
            return
        if blk.get("text"):
            done["error_text"] = blk["text"]
            if blk.get("first"):
                done["error"] = blk["first"]
        done["exit_code"] = returncode
        if blk.get("kind"):
            done["failure_kind"] = blk["kind"]
        if self._error is None:
            self._error = done.get("error")

    def _tee_line(self, line: str) -> None:
        self._console_tail.append(line)
        """Keep one console line: buffered until the run folder holds its
        first step (LibreLane decides new-or-existing run on an empty folder),
        then appended to ``runs/<tag>/lanex-console.log``. The name has no
        number prefix, so LibreLane's step loader ignores it."""
        if self._tee is None:
            self._tee_buf.append(line)
            if len(self._tee_buf) > self.TEE_BUF_MAX:
                del self._tee_buf[: len(self._tee_buf) - self.TEE_BUF_MAX]
            if self._current_step_id and self._run_dir and Path(self._run_dir).is_dir():
                try:
                    self._tee = safefs.open_append(Path(self._run_dir) / self.TEE_NAME, buffering=1)  # never through a link
                    self._tee.write("\n".join(self._tee_buf) + "\n")
                    self._tee_buf = []
                except Exception:
                    self._tee = False
            return
        if self._tee:
            try:
                self._tee.write(line + "\n")
            except Exception:
                self._tee = False

    def _close_tee(self, final: bool = True) -> None:
        t = self._tee
        if t is None and self._tee_buf and self._run_dir and Path(self._run_dir).is_dir():
            # A run that ended before its first step (a config error): keep
            # what it printed, the reason is in there.
            try:
                with safefs.open_append(Path(self._run_dir) / self.TEE_NAME) as f:
                    f.write("\n".join(self._tee_buf) + "\n")
            except Exception:
                pass
            self._tee_buf = []
            return
        if t and final:
            try:
                t.close()
            except Exception:
                pass
            self._tee = None

    def _lower_priority(self) -> None:
        """Low priority: give this run's container a small CPU share once it
        exists (``docker update --cpu-shares 256``): other work stays quick,
        the run takes a little longer. Nothing where the engine can't."""
        if not self.low_priority or self._lowered or not self._container_name:
            return
        self._lowered = True
        name = self._container_name
        engine = self._container_engine
        sg = self._container_sg

        def go() -> None:
            exe = engine or shutil.which("docker") or shutil.which("podman")
            if not exe:
                return
            argv = [exe, "update", "--cpu-shares", "256", name]
            if sg:
                try:
                    from . import tools
                    argv = tools.sg_wrap_argv(argv)
                except Exception:
                    pass
            try:
                r = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                if r.returncode == 0:
                    self._emit(EventType.INFO, {"message": "Low priority: this run's container gets a smaller share of the CPU."})
            except Exception:
                pass
        threading.Thread(target=go, daemon=True, name="lanex.runner.lowprio").start()

    def _persist_outcome(self, done: Dict[str, Any]) -> None:
        """Note how the run ended in ``gui-run.json`` (``outcome``), so a run
        the user stopped reads as stopped later, not as failed: the run folder
        alone can't tell a cancel from a crash. Best effort, like the rest."""
        if not self._run_dir:
            return
        try:
            import json as _json

            from . import platform_env
            path = Path(self._run_dir) / "gui-run.json"
            if not path.parent.is_dir():
                return
            meta = _json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if not isinstance(meta, dict):
                return
            result = "cancelled" if done.get("cancelled") else ("error" if done.get("error") else "ok")
            meta["outcome"] = {"result": result, "error": done.get("error") or None,
                               "ended_at": platform_env.local_now().isoformat(timespec="seconds")}
            # The console's own words when it failed (a config LibreLane can't
            # load lists every problem; some errors reach only the console).
            for k in ("error_text", "exit_code", "failure_kind"):
                if done.get(k) is not None:
                    meta["outcome"][k] = done[k]
            platform_env.atomic_write_text(path, _json.dumps(_safe_jsonable(meta), indent=2) + "\n")
        except Exception:  # pragma: no cover - fs/permission dependent
            _log.debug("could not note the run outcome", exc_info=True)

    def _next_seq(self) -> int:
        from .events import next_seq

        return next_seq()

    def _persist_gui_meta(self) -> None:
        """Write the GUI's run settings into the run dir as ``gui-run.json`` so a
        run can be reproduced from the GUI later (issue #4). Idempotent + best
        effort — a failure here must never derail the flow. LibreLane already
        writes the resolved ``config.json``; this adds the GUI-only context
        (preset/overrides as set, run mode, from/to/skip, sources, the CLI line)."""
        if self._gui_meta_written or not self._gui_meta or not self._run_dir:
            return
        try:
            run_dir = Path(self._run_dir)
            if not run_dir.is_dir():
                return
            meta = dict(self._gui_meta)
            meta.setdefault("tag", run_dir.name)
            meta["run_dir"] = str(run_dir)
            from . import platform_env as _pe
            meta["written_at"] = _pe.local_now().isoformat(timespec="seconds")
            # Stamp the toolchain identity (LanEx/LibreLane versions + container
            # image) so an export or reproduce can prove which tools produced
            # these numbers — the "would I get the same manually?" contract.
            meta.setdefault("toolchain", _toolchain_provenance(self._run_mode))
            import json as _json

            from . import platform_env
            platform_env.atomic_write_text(
                run_dir / "gui-run.json",
                _json.dumps(_safe_jsonable(meta), indent=2) + "\n",
            )
            self._gui_meta_written = True
        except Exception:  # pragma: no cover - fs/permission dependent
            _log.debug("could not persist gui-run.json", exc_info=True)
            self._gui_meta_written = True  # don't retry every step

    def _emit_resources_note(self) -> None:
        """One log line saying which thread counts LanEx sized to this PC (from
        ``gui_meta["resources"]``), so the run's own log shows it."""
        rec = (self._gui_meta or {}).get("resources") or {}
        vals = rec.get("values") or {}
        kept = rec.get("kept") or {}
        if not vals and not kept:
            return
        msg = ""
        if vals:
            msg = ("Threads sized to this PC (" + (rec.get("reason") or "?") + "): " +
                   " ".join(f"{k}={v}" for k, v in vals.items()) + ".")
        if kept:
            msg += (" " if msg else "") + "Left as set: " + ", ".join(
                f"{k} ({src})" for k, src in kept.items()) + "."
        self._emit(EventType.INFO, {"message": msg, "resources": rec})

    # ---- progress hooks (called from the patched FlowProgressBar) ----

    def _on_set_max(self, count: int) -> None:
        self._stage_total = int(count)
        self._emit(EventType.PROGRESS, {"done": 0, "total": self._stage_total, "current": ""})

    def _step_id_for_index(self, idx: int) -> Optional[str]:
        steps = list(getattr(self._flow, "Steps", []) or [])
        if 0 <= idx < len(steps):
            return steps[idx].id
        return None

    def _eta_payload(self) -> Dict[str, Any]:
        """``{eta_seconds, elapsed_seconds}`` for the live progress bar.

        ETA = estimated time for the steps not yet done (learned per-step history
        + this run's observed average). ``None`` when nothing can be estimated.
        """
        out: Dict[str, Any] = {"eta_seconds": None, "elapsed_seconds": None}
        if self._run_start_ts is not None:
            out["elapsed_seconds"] = round(time.time() - self._run_start_ts, 1)
        try:
            from . import runtimings
            all_ids = [s.id for s in (getattr(self._flow, "Steps", []) or [])]
            done = {sid for sid, st in self._step_statuses.items()
                    if st in (StepStatus.DONE.value, StepStatus.SKIPPED.value)}
            remaining = [sid for sid in all_ids if sid not in done]
            out["eta_seconds"] = runtimings.estimate_remaining(remaining, self._observed_durations)
        except Exception:
            pass
        return out

    def _on_stage_start(self, name: str) -> None:
        # Honour pause between steps, then check for cancellation.
        if not self._resume.is_set():
            self._emit(EventType.INFO, {"message": "paused — click Resume to continue"})
            self._resume.wait()
        if self._cancel.is_set():
            raise _Cancelled()

        # The flow assigns its run directory before the first step executes;
        # capture it now so the UI and cancel logic have it mid-run.
        if not self._run_dir:
            rd = getattr(self._flow, "run_dir", None)
            if rd:
                self._run_dir = str(rd)
        self._persist_gui_meta()
        if self._run_start_ts is None:
            self._run_start_ts = time.time()
        self._step_start_ts = time.time()
        self._stage_idx += 1
        step_id = self._step_id_for_index(self._stage_idx) or name
        self._current_step_id = step_id
        self._step_statuses[step_id] = StepStatus.RUNNING.value
        self._emit(
            EventType.STEP_STARTED,
            {
                "step_id": step_id,
                "name": name,
                "index": self._stage_idx,
                "total": self._stage_total,
            },
        )
        self._emit(
            EventType.PROGRESS,
            {"done": self._stage_idx, "total": self._stage_total, "current": step_id,
             **self._eta_payload()},
        )

    def _on_stage_end(self, increment_ordinal: bool) -> None:
        step_id = self._current_step_id
        if step_id is None:
            return
        # Learn this step's duration (only for genuinely executed, non-skipped
        # steps) so future runs get a sharper ETA.
        if increment_ordinal and self._step_start_ts is not None:
            dur = time.time() - self._step_start_ts
            if dur >= 0:
                self._observed_durations.append(dur)
                try:
                    from . import runtimings
                    runtimings.record(step_id, dur)
                except Exception:
                    pass
        self._step_start_ts = None
        if increment_ordinal:
            self._step_statuses[step_id] = StepStatus.DONE.value
            self._emit(EventType.STEP_DONE, {"step_id": step_id, "index": self._stage_idx})
        else:
            self._step_statuses[step_id] = StepStatus.SKIPPED.value
            self._emit(EventType.STEP_SKIPPED, {"step_id": step_id, "index": self._stage_idx})
        self._current_step_id = None
        done = sum(
            1 for s in self._step_statuses.values() if s in (StepStatus.DONE.value, StepStatus.SKIPPED.value)
        )
        self._emit(EventType.PROGRESS, {"done": done, "total": self._stage_total, "current": "",
                                        **self._eta_payload()})
        # Step-by-step mode: hold the flow after each executed step so the user
        # can inspect results, then resume to advance to the next step.
        if self._step_mode and increment_ordinal and not self._cancel.is_set():
            self._resume.clear()
            self._emit(
                EventType.INFO,
                {"message": f"step '{step_id}' complete — click Resume for the next step", "paused": True},
            )

    # ---- flow execution ----

    def _run(
        self,
        flow_factory: Any,
        config_files: Sequence[str],
        design_dir: Path,
        *,
        pdk_root: Optional[str],
        pdk: Optional[str],
        scl: Optional[str],
        pad: Optional[str],
        tag: Optional[str],
        frm: Optional[str],
        to: Optional[str],
        skip: Optional[List[str]],
        overwrite: bool,
        reproducible_step: Optional[str],
        config_overrides: Dict[str, Any],
        extra_sources: Optional[List[str]],
        extra_extras: Optional[List[str]],
        extra_config_files: Optional[List[str]] = None,
        last_run: bool = False,
        initial_state: Optional[str] = None,
    ) -> None:
        global _ACTIVE_RUNNER
        self._setup_log_bridge()
        _install_progress_patch()
        _install_subprocess_patch()
        _ACTIVE_RUNNER = self
        try:
            self._emit(EventType.INFO, {"message": f"loading flow (config={list(config_files)})"})

            # Build the Flow with construction-time options. PDK/SCL/PAD/PDK_ROOT
            # and config overrides MUST go to the constructor — the engine reads
            # them when it loads the configuration, not at flow.start().
            override_strings = [f"{k}={v}" for k, v in (config_overrides or {}).items()]
            if extra_sources:
                # LibreLane parses list overrides as whitespace-separated paths.
                override_strings.append("VERILOG_FILES=" + " ".join(extra_sources))
            if extra_extras:
                override_strings.append("EXTRA_FILES=" + " ".join(extra_extras))

            ctor_kwargs: Dict[str, Any] = {"design_dir": str(design_dir)}
            for key, val in (("pdk_root", pdk_root), ("pdk", pdk), ("scl", scl), ("pad", pad)):
                if val:
                    ctor_kwargs[key] = val
            if override_strings:
                ctor_kwargs["config_override_strings"] = override_strings

            # Append any GUI-generated overlay configs (e.g. the custom-macro
            # MACROS overlay). Config.load merges a sequence of config sources, so
            # the overlay augments the user's config without a -c string (a Dict
            # variable can't round-trip through a KEY=VALUE override).
            all_configs = list(config_files) + list(extra_config_files or [])
            flow = flow_factory(all_configs, **ctor_kwargs)
            self._flow = flow
            self._seed_step_graph(flow)
            self._emit_resources_note()

            self._emit(
                EventType.INFO,
                {
                    "message": "starting flow",
                    "flow": getattr(flow, "name", "flow"),
                    "tag": tag,
                    "frm": frm,
                    "to": to,
                    "skip": skip,
                    "reproducible": reproducible_step,
                },
            )

            start_kwargs: Dict[str, Any] = {
                "tag": tag,
                "overwrite": overwrite,
                "with_initial_state": _load_state(initial_state) if initial_state else None,
            }
            # Continue a prior run's state (targeted re-verify of a single step).
            # Without this, a local ``-F step -T step`` has no input state and
            # the step has nothing to operate on. Container mode resumes via
            # ``--run-tag`` so it doesn't need this.
            if last_run:
                start_kwargs["last_run"] = True
            # frm/to/skip/reproducible are consumed by SequentialFlow.run via
            # **kwargs; only pass the ones that were actually requested.
            if frm:
                start_kwargs["frm"] = frm
            if to:
                start_kwargs["to"] = to
            if skip:
                start_kwargs["skip"] = tuple(skip)
            if reproducible_step:
                start_kwargs["reproducible"] = reproducible_step

            state_out = flow.start(**start_kwargs)
            self._state_out = state_out
            self._run_dir = flow.run_dir
            self._persist_gui_meta()  # covers fast runs that skip the step hook
            self._emit(
                EventType.FLOW_DONE,
                {"tag": flow.run_dir, "metrics": (getattr(state_out, "metrics", {}) or {})},
            )
        except _Cancelled:
            self._run_dir = getattr(self._flow, "run_dir", None) or self._run_dir
            self._mark_current_failed("cancelled")
            self._mark_remaining_aborted()
            self._emit(EventType.INFO, {"message": "flow cancelled by user"})
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "cancelled": True})
        except Exception as ex:
            tb = traceback.format_exc()
            self._run_dir = getattr(self._flow, "run_dir", None) or self._run_dir
            self._error = f"{type(ex).__name__}: {ex}"
            self._mark_current_failed(str(ex))
            self._emit(
                EventType.STEP_FAILED,
                {
                    "step_id": self._current_step_id,
                    "message": str(ex),
                    "traceback": tb[-4000:],
                    "cancelled": self._cancel.is_set(),
                },
            )
            self._mark_remaining_aborted()
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "error": self._error})
        finally:
            self._running = False
            _ACTIVE_RUNNER = None
            self._teardown_log_bridge()

    # ---- container execution ----

    _PARSED_EVENT_TYPES = {
        "step_started": EventType.STEP_STARTED,
        "step_done": EventType.STEP_DONE,
        "step_skipped": EventType.STEP_SKIPPED,
        "step_failed": EventType.STEP_FAILED,
        "progress": EventType.PROGRESS,
        "flow_done": EventType.FLOW_DONE,
    }
    _PARSED_STATUS = {
        "step_started": StepStatus.RUNNING,
        "step_done": StepStatus.DONE,
        "step_skipped": StepStatus.SKIPPED,
        "step_failed": StepStatus.FAILED,
    }

    def _container_launch(self, argv: List[str]) -> "tuple[List[str], Dict[str, str]]":
        """The argv and environment for one ``librelane --dockerized`` call.

        Forces LibreLane to the engine the GUI found usable (so a
        present-but-unusable Docker can't shadow a working Podman), pins the
        image, activates the docker group via ``sg`` when that is how Docker
        was made usable without a re-login, and names the container
        (``LANEX_CONTAINER_NAME``, read by ``librelane_entry``) so Stop can
        remove this container and nothing else."""
        run_env = os.environ.copy()
        # The host-side `python -m lanex.controller.librelane_entry` starts in
        # the project folder: without this, a `lanex/` or `librelane/` folder
        # (or json.py) in a shared project would be imported and run here.
        run_env["PYTHONSAFEPATH"] = "1"
        self._container_name = new_container_name()
        run_env["LANEX_CONTAINER_NAME"] = self._container_name
        # Who started it (applied as --label by librelane_entry): another
        # LanEx on the same container engine leaves this one's alone, and a
        # restarted server knows which leftovers are its own (jobs.is_orphan).
        run_env["LANEX_CONTAINER_LABELS"] = container_labels()
        self._container_engine = None
        self._container_sg = False
        try:
            from . import tools, container_run

            resolved = tools.resolve_engine()
            run_env.update(resolved.get("env") or {})
            if resolved.get("engine"):
                run_env["LIBRELANE_CONTAINER_ENGINE"] = resolved["engine"]
                self._container_engine = shutil.which(resolved["engine"]) or resolved["engine"]
            img = container_run.image_ref()
            if img:
                run_env["LIBRELANE_IMAGE_OVERRIDE"] = img
            if resolved.get("sg_wrap"):
                argv = tools.sg_wrap_argv(argv)
                self._container_sg = True
        except Exception:
            pass
        return argv, run_env

    def _dispatch_parsed(self, ev: Dict[str, Any]) -> None:
        """Translate a :class:`ContainerLogParser` event into an SSE emit."""
        t = ev.get("type")
        if t == "container":
            # LibreLane echoed its `--name`: with LanEx naming the container it
            # is the same name; keep LanEx's when the echo is anything else.
            if not self._container_name and ev.get("name"):
                self._container_name = ev.get("name")
            return
        if t == "phase":
            self._emit(EventType.PHASE, {"label": ev.get("label", "")})
            return
        sid = ev.get("step_id")
        if t in self._PARSED_STATUS and sid:
            self._step_statuses[sid] = self._PARSED_STATUS[t].value
            if t == "step_started":
                self._current_step_id = sid
                # The run folder exists once a step starts: write gui-run.json
                # now, so a run resumed from another one shows its earlier
                # steps (branched_from) while it runs, not only at the end.
                self._persist_gui_meta()
            elif t in ("step_done", "step_skipped"):
                if self._current_step_id == sid:
                    self._current_step_id = None
        kind = self._PARSED_EVENT_TYPES.get(t)
        if kind is None:
            return
        payload = {k: v for k, v in ev.items() if k != "type"}
        self._emit(kind, payload)

    def _run_container(
        self,
        flow_factory: Any,
        config_files: Sequence[str],
        design_dir: Path,
        *,
        pdk_root: Optional[str],
        pdk: Optional[str],
        scl: Optional[str],
        pad: Optional[str],
        tag: Optional[str],
        frm: Optional[str],
        to: Optional[str],
        skip: Optional[List[str]],
        overwrite: bool,
        reproducible_step: Optional[str],
        config_overrides: Dict[str, Any],
        extra_sources: Optional[List[str]],
        extra_extras: Optional[List[str]],
        extra_config_files: Optional[List[str]] = None,
        last_run: bool = False,  # container resumes via --run-tag; accepted for signature parity
        initial_state: Optional[str] = None,
    ) -> None:
        """Shell out to ``librelane --dockerized`` and stream/parse its stdout."""
        try:
            if not config_files:
                raise ValueError("no config file to run")
            # We set --run-tag ourselves so the run dir is known up front.
            if not tag:
                from .platform_env import new_run_tag
                tag = new_run_tag(design_dir / "runs")
            self._run_dir = str(design_dir / "runs" / tag)

            step_ids: List[str] = []
            try:
                for cls in getattr(flow_factory, "Steps", []) or []:
                    step_ids.append(cls.id)
            except Exception:
                pass
            self._seed_step_graph(flow_factory)
            parser = ContainerLogParser(step_ids)

            argv = build_dockerized_argv(
                config_file=config_files[0],
                extra_config_files=extra_config_files,
                design_dir=design_dir,
                flow=self._flow_name,
                pdk=pdk,
                scl=scl,
                pdk_root=pdk_root,
                tag=tag,
                frm=frm,
                to=to,
                skip=skip,
                overrides=config_overrides,
                extra_sources=extra_sources,
                extra_extras=extra_extras,
                overwrite=overwrite,
                initial_state=initial_state,
                entry_module="lanex.controller.librelane_entry",
            )
            # What really runs, kept in gui-run.json: the run menu's command and
            # the recorded cli_command must equal it (tests check the builders).
            if self._gui_meta is not None:
                self._gui_meta["launched_argv"] = list(argv)
            argv, run_env = self._container_launch(argv)

            self._emit(
                EventType.INFO,
                {"message": "Container mode — running: " + " ".join(argv)},
            )
            # The console log starts with exactly what ran (VS Code's Git output does the same).
            self._tee_line("> " + " ".join(argv))
            self._emit_resources_note()
            # Seed an immediate phase so the pipeline shows "preparing" the
            # instant the run starts, before any container stdout arrives.
            self._emit(
                EventType.PHASE,
                {"label": "Starting the LibreLane container…"},
            )

            proc = subprocess.Popen(
                argv,
                cwd=str(design_dir),
                stdin=subprocess.DEVNULL,  # no controlling TTY → engine won't want -t
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=run_env,
            )
            self._container_proc = proc
            assert proc.stdout is not None
            for raw in proc.stdout:
                # Strip ANSI/control bytes first so both the parser and the log
                # see clean text. Feed the parser every line (it keys off
                # `Running '<id>'` / `--name <uuid>` / failure markers).
                line = strip_ansi(raw).rstrip()
                for parsed in parser.feed(line):
                    self._dispatch_parsed(parsed)
                # Emit real output only — drop the Rich progress-bar redraws that
                # otherwise flood the log (progress is shown on the timeline).
                if line and not is_progress_bar(line):
                    self._emit(EventType.LOG, {"message": line})
                    self._tee_line(line)
                if self._cancel.is_set():
                    break
            proc.wait()
            # The container has created the run dir by now — persist GUI settings.
            self._persist_gui_meta()

            if self._cancel.is_set():
                # Second removal sweep: the container may have come up after (or
                # raced with) cancel()'s first attempt; the flow must not keep
                # running behind a "cancelled" status.
                self._kill_container()
                self._mark_current_failed("cancelled")
                self._emit(EventType.INFO, {"message": "flow cancelled by user"})
                self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "cancelled": True})
            else:
                for parsed in parser.finish(proc.returncode):
                    if parsed.get("type") == "flow_done":
                        parsed.setdefault("tag", self._run_dir or "")
                        if proc.returncode:
                            self._error_from_console(parsed, proc.returncode)
                    self._dispatch_parsed(parsed)
        except FileNotFoundError as ex:
            self._error = f"{type(ex).__name__}: {ex}"
            self._emit(
                EventType.STEP_FAILED,
                {"step_id": self._current_step_id, "message": str(ex)},
            )
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "error": self._error})
        except Exception as ex:
            tb = traceback.format_exc()
            self._error = f"{type(ex).__name__}: {ex}"
            self._mark_current_failed(str(ex))
            self._emit(
                EventType.STEP_FAILED,
                {
                    "step_id": self._current_step_id,
                    "message": str(ex),
                    "traceback": tb[-4000:],
                    "cancelled": self._cancel.is_set(),
                },
            )
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "error": self._error})
        finally:
            self._running = False
            self._container_proc = None

    # ---- container step-by-step execution ----

    def _run_container_stepwise(
        self,
        flow_factory: Any,
        config_files: Sequence[str],
        design_dir: Path,
        *,
        pdk_root: Optional[str],
        pdk: Optional[str],
        scl: Optional[str],
        pad: Optional[str],
        tag: Optional[str],
        frm: Optional[str],
        to: Optional[str],
        skip: Optional[List[str]],
        overwrite: bool,
        reproducible_step: Optional[str],
        config_overrides: Dict[str, Any],
        extra_sources: Optional[List[str]],
        extra_extras: Optional[List[str]],
        extra_config_files: Optional[List[str]] = None,
        last_run: bool = False,
        initial_state: Optional[str] = None,
    ) -> None:
        """Set up container step-by-step: seed the step graph, save the
        invocation args, and run the first step. Each subsequent step is launched
        by :meth:`resume` (the "Next"/Resume button) via
        :meth:`_advance_container_step`."""
        try:
            if not config_files:
                raise ValueError("no config file to run")
            if not tag:
                from .platform_env import new_run_tag
                tag = new_run_tag(design_dir / "runs")
            self._run_dir = str(design_dir / "runs" / tag)

            step_ids: List[str] = []
            try:
                for cls in getattr(flow_factory, "Steps", []) or []:
                    step_ids.append(cls.id)
            except Exception:
                pass
            # Honour a From/To window if the user set one, so step-by-step can
            # also walk just a slice of the flow.
            sub = self._slice_steps(step_ids, frm, to, skip or [])
            if not sub:
                raise ValueError("no steps to run (check From/To/Skip)")
            self._seed_step_graph(flow_factory)

            self._cstep = {
                "steps": sub,
                "idx": 0,
                "tag": tag,
                "design_dir": design_dir,
                "config_file": config_files[0],
                "extra_config_files": list(extra_config_files or []),
                "pdk": pdk,
                "scl": scl,
                "pdk_root": pdk_root,
                "overrides": dict(config_overrides or {}),
                "extra_sources": list(extra_sources or []),
                "extra_extras": list(extra_extras or []),
                "overwrite": bool(overwrite),
                "initial_state": initial_state,
            }
            self._emit(
                EventType.INFO,
                {"message": f"Container step-by-step — {len(sub)} steps; running one at a time."},
            )
            self._emit_resources_note()
            self._run_container_step(0)
        except Exception as ex:
            self._error = f"{type(ex).__name__}: {ex}"
            self._emit(EventType.STEP_FAILED, {"step_id": self._current_step_id, "message": str(ex)})
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "error": self._error})
            self._running = False

    @staticmethod
    def _slice_steps(step_ids: List[str], frm: Optional[str], to: Optional[str], skip: Sequence[str]) -> List[str]:
        """Return the ordered sub-list of step ids honoring From/To/Skip."""
        if not step_ids:
            return []
        lo = step_ids.index(frm) if (frm and frm in step_ids) else 0
        hi = step_ids.index(to) if (to and to in step_ids) else len(step_ids) - 1
        if hi < lo:
            # An inverted range is a user mistake — running the FULL flow instead
            # (the old behaviour) silently ignores their intent. Return no steps
            # so the caller raises "no steps to run (check From/To/Skip)".
            return []
        skipset = set(skip or [])
        return [s for s in step_ids[lo:hi + 1] if s not in skipset]

    def _advance_container_step(self) -> None:
        """Launch the next single-step container invocation in a worker thread."""
        if not self._cstep:
            return
        idx = self._cstep.get("idx", 0)
        self._awaiting_next = False
        self._worker = threading.Thread(
            target=self._run_container_step,
            args=(idx,),
            daemon=True,
            name="librelane.gui.FlowRunner.cstep",
        )
        self._worker.start()

    def _run_container_step(self, idx: int) -> None:
        """Run exactly one flow step inside the container via ``--only <step>``,
        resuming the shared run tag. Pauses (awaiting Next) afterwards unless it
        was the last step or the run failed."""
        c = self._cstep
        steps: List[str] = c["steps"]
        sid = steps[idx]
        design_dir: Path = c["design_dir"]
        try:
            parser = ContainerLogParser([sid])
            argv = build_dockerized_argv(
                config_file=c["config_file"],
                extra_config_files=c.get("extra_config_files"),
                design_dir=design_dir,
                flow=self._flow_name,
                pdk=c["pdk"],
                scl=c["scl"],
                pdk_root=c["pdk_root"],
                tag=c["tag"],
                frm=sid,
                to=sid,
                overrides=c["overrides"],
                extra_sources=c["extra_sources"],
                extra_extras=c["extra_extras"],
                # Only the FIRST step may overwrite an existing tag; later steps
                # MUST reuse the run dir (overwrite would wipe prior step state).
                overwrite=bool(c["overwrite"]) and idx == 0,
                # A resumed run starts from the other run's state; every later
                # step starts from the state the step before it wrote, named
                # with -i. Without -i LibreLane takes the newest state_out.json
                # by time, which is the wrong one once a later step folder
                # exists (checked on 3.0.4: CTS ran on the final layout).
                initial_state=(c.get("initial_state") if idx == 0
                               else (latest_state_out(self._run_dir) or c.get("initial_state"))),
                entry_module="lanex.controller.librelane_entry",
            )
            argv, run_env = self._container_launch(argv)

            self._emit(
                EventType.INFO,
                {"message": f"Step {idx + 1}/{len(steps)} ({sid}) — running: " + " ".join(argv)},
            )
            self._tee_line("> " + " ".join(argv))
            self._emit(EventType.PHASE, {"label": f"Starting the container for step '{sid}'…"})

            proc = subprocess.Popen(
                argv, cwd=str(design_dir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                bufsize=1, env=run_env,
            )
            self._container_proc = proc
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = strip_ansi(raw).rstrip()
                for parsed in parser.feed(line):
                    # Suppress the parser's auto-progress; we drive global
                    # progress ourselves below to reflect the whole flow.
                    if parsed.get("type") != "progress":
                        self._dispatch_parsed(parsed)
                if line and not is_progress_bar(line):
                    self._emit(EventType.LOG, {"message": line})
                    self._tee_line(line)
                if self._cancel.is_set():
                    break
            proc.wait()
            self._container_proc = None
            self._persist_gui_meta()  # run dir exists once the first step ran

            if self._cancel.is_set():
                # Second removal sweep — same reasoning as the full-run worker.
                self._kill_container()
                self._mark_current_failed("cancelled")
                self._emit(EventType.INFO, {"message": "flow cancelled by user"})
                self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "cancelled": True})
                self._running = False
                return

            # Exit 2 with --only is LibreLane's "one or more deferred errors":
            # a check failed, the step did not break. A full run goes on past
            # it, and so can this one.
            checker_failed = proc.returncode == 2 and not parser.failed
            if checker_failed:
                self._emit(EventType.INFO, {"message": f"A check failed in '{sid}'. You can still go on to the next step."})
            if proc.returncode == 0 or checker_failed:
                self._step_statuses[sid] = StepStatus.DONE.value
                self._emit(EventType.STEP_DONE, {"step_id": sid, "index": idx})
                completed = idx + 1
                self._emit(EventType.PROGRESS, {"done": completed, "total": len(steps), "current": ""})
                if completed >= len(steps):
                    self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "ok": True})
                    self._running = False
                else:
                    self._cstep["idx"] = completed
                    self._awaiting_next = True
                    nxt = steps[completed]
                    self._emit(
                        EventType.INFO,
                        {"message": f"step '{sid}' complete — click Resume for the next step ('{nxt}')",
                         "paused": True, "next_step": nxt, "done_step": sid},
                    )
            else:
                self._step_statuses[sid] = StepStatus.FAILED.value
                if not parser.failed:
                    msg = (parser.pdk_download_issue and
                           "PDK download/resolve failed (offline?)") or \
                          f"step '{sid}' exited with code {proc.returncode}"
                    self._emit(EventType.STEP_FAILED, {"step_id": sid, "message": msg})
                done = {"tag": self._run_dir or "", "error": f"step '{sid}' failed"}
                self._error_from_console(done, proc.returncode)
                self._emit(EventType.FLOW_DONE, done)
                self._running = False
        except Exception as ex:
            tb = traceback.format_exc()
            self._error = f"{type(ex).__name__}: {ex}"
            self._step_statuses[sid] = StepStatus.FAILED.value
            self._emit(EventType.STEP_FAILED,
                       {"step_id": sid, "message": str(ex), "traceback": tb[-4000:]})
            self._emit(EventType.FLOW_DONE, {"tag": self._run_dir or "", "error": self._error})
            self._running = False
            self._container_proc = None

    def _mark_current_failed(self, _why: str) -> None:
        sid = self._current_step_id
        if sid:
            self._step_statuses[sid] = StepStatus.FAILED.value

    def _mark_remaining_aborted(self) -> None:
        """After a flow aborts (cancel or mid-flow error), every step that never
        reached a terminal state stays PENDING/RUNNING forever — the timeline
        then shows a tail of grey "pending" rows that look like they might still
        run, even though the run is over (audit A5). Emit ``step_skipped``
        (reason: flow aborted) for each so the timeline is honest. The current
        step is already marked FAILED by :meth:`_mark_current_failed` and is left
        untouched here."""
        for sid, st in list(self._step_statuses.items()):
            if sid == self._current_step_id:
                continue
            if st in (StepStatus.PENDING.value, StepStatus.RUNNING.value):
                self._step_statuses[sid] = StepStatus.SKIPPED.value
                self._emit(EventType.STEP_SKIPPED, {"step_id": sid, "reason": "flow aborted"})

    def _seed_step_graph(self, flow: Any) -> None:
        """Tell the SPA which steps exist before any run begins."""
        try:
            graph = []
            for cls in getattr(flow, "Steps", []) or []:
                self._step_statuses.setdefault(cls.id, StepStatus.PENDING.value)
                graph.append({"id": cls.id, "status": StepStatus.PENDING.value})
            self._stage_total = len(graph)
            self._emit(EventType.INFO, {"step_graph": graph})
        except Exception:
            pass

    # ---- log bridge ----

    def _setup_log_bridge(self) -> None:
        """Forward LibreLane's logs to the SSE stream as ``log`` events.

        Two things matter here, and getting either wrong makes the in-image
        (native-toolchain) run look far less detailed than a container run:

        * **Bind to the right logger.** LibreLane logs to a named logger,
          ``__librelane__`` (not the root logger). The old
          ``from librelane.logging import getLogger`` import silently failed
          (that name isn't exported) and we fell back to the root logger,
          catching LibreLane's records only by propagation.
        * **Accept the SUBPROCESS level.** LibreLane streams every EDA tool's
          stdout through that logger at its custom ``SUBPROCESS`` level (=12,
          *below* ``INFO``). Our handler sat at ``INFO`` (20) and so dropped
          exactly that per-tool detail — the polygon dumps / per-cell / LVS
          output a verbose container run shows live. Capturing SUBPROCESS makes
          the local/in-image stream as detailed as container mode.
        """
        try:
            from librelane.logging.logger import LogLevels  # type: ignore

            sub_level = int(LogLevels.SUBPROCESS)
        except Exception:
            sub_level = 12  # SUBPROCESS — stable, documented CLI log-level value
        root = logging.getLogger("__librelane__")
        handler = _SSEHandler(self)
        handler.setLevel(sub_level)
        root.addHandler(handler)
        # __librelane__ defaults to SUBPROCESS, but be explicit so a global/parent
        # level set elsewhere can't filter the tool detail before we see it.
        try:
            if root.level == 0 or root.level > sub_level:
                root.setLevel(sub_level)
        except Exception:
            pass
        self._log_handler = handler
        self._log_root = root

    def _teardown_log_bridge(self) -> None:
        h = getattr(self, "_log_handler", None)
        root = getattr(self, "_log_root", None)
        if h is not None and root is not None:
            try:
                root.removeHandler(h)
            except Exception:
                pass
        self._log_handler = None


class _SSEHandler(logging.Handler):
    """Forward log records to the SSE event stream (no control flow)."""

    def __init__(self, runner: FlowRunner) -> None:
        super().__init__()
        self._runner = runner

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - threading-sensitive
        try:
            msg = self.format(record).rstrip()
        except Exception:
            return
        kind = EventType.LOG
        if record.levelno >= logging.ERROR:
            kind = EventType.LOG  # keep as log; failures are emitted by the runner itself
        try:
            self._runner._emit(kind, {"level": record.levelname, "message": msg})
        except Exception:
            pass
