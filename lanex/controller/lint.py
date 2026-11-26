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
"""RTL lint (Phase 3.4) — a standalone Verilator ``--lint-only`` job.

"Check syntax" in the RTL IDE must be a *lint*, not a hardening run. Earlier it
drove the whole ``Classic`` flow through the shared FlowRunner, so it wrote a
``runs/`` directory, needed a PDK, and showed up in the Runs tab / pipeline /
logs as a full RTL→GDS run. This module instead runs ``verilator --lint-only``
directly on the design sources — no FlowRunner, no ``runs/`` dir, no PDK — and
emits dedicated ``lint_started`` / ``lint_done`` events so the IDE can react
without touching the cockpit's run UI.

Verilator emits diagnostics like::

    %Error: foo.v:12:7: syntax error, unexpected ...
    %Warning-WIDTH: bar.sv:30:14: Operator ... expects 8 bits ...

We turn each into ``{file, line, col, severity, code, msg}`` for editor-gutter
markers. Pure regex + stdlib subprocess; no new dependency. Verilator runs on
the host when present, else inside the LibreLane image (container mode).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Wall-clock ceiling for a single lint. ``verilator --lint-only`` returns almost
# instantly on healthy input, but a wedged tool (e.g. the *Windows* verilator
# resolved off the WSL ``/mnt/c`` PATH, which can hang under interop) would keep
# the worker thread alive forever, leaving ``job.running`` stuck True and
# refusing every later "Check syntax" with "a lint is already running" — the
# classic "works only once" bug. The watchdog kills a runaway lint so the job
# ALWAYS frees itself.
DEFAULT_LINT_TIMEOUT = 90

# %Error / %Warning-CODE: file:line:col: message
_RX = re.compile(
    r"^%(?P<sev>Error|Warning)(?:-(?P<code>[A-Z0-9_]+))?:\s*"
    r"(?P<file>[^:\s][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<msg>.*)$"
)


def parse_verilator(log_text: str) -> List[Dict[str, Any]]:
    """Extract structured diagnostics from Verilator stdout/stderr."""
    out: List[Dict[str, Any]] = []
    for raw in (log_text or "").splitlines():
        line = raw.rstrip()
        m = _RX.match(line)
        if not m:
            continue
        out.append({
            "file": m.group("file").strip(),
            "line": int(m.group("line")),
            "col": int(m.group("col")) if m.group("col") else 1,
            "severity": m.group("sev").lower(),  # "error" | "warning"
            "code": m.group("code") or "",
            "msg": m.group("msg").strip(),
        })
    return out


def summarize(diags: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count errors/warnings for a status line."""
    errors = sum(1 for d in diags if d["severity"] == "error")
    warnings = sum(1 for d in diags if d["severity"] == "warning")
    return {"errors": errors, "warnings": warnings, "total": len(diags)}


# ---------------------------------------------------------------------------
# Standalone lint job (NOT the hardening flow)
# ---------------------------------------------------------------------------

def _shquote(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./=:+-]+", s or ""):
        return s
    return "'" + str(s).replace("'", "'\\''") + "'"


def build_lint_command(
    design_dir: str | Path,
    *,
    sources: Sequence[str],
    top: Optional[str] = None,
    include_dirs: Optional[Sequence[str]] = None,
    defines: Optional[Dict[str, Any]] = None,
    run_mode: str = "local",
    engine: str = "docker",
    image: Optional[str] = None,
    timing: bool = False,
    disable_warnings: Sequence[str] = (),
) -> List[str]:
    """Build the argv for ``verilator --lint-only`` over *sources*.

    *timing* adds ``--timing`` (Verilator 5): without it every ``#delay`` and
    ``@(posedge clk)`` in a testbench is an error (NEEDTIMINGOPT), so a bench
    the simulator runs fine read as a page of errors.

    Local mode returns a bare ``verilator …`` argv (caller sets
    ``cwd=design_dir``); container mode mounts the design dir at ``/work`` and
    runs the image's verilator. Pure / no system probing. All source paths are
    relative to the design dir (the cwd / mount), so they're portable."""
    # --timescale: the default for files without a `timescale, which is what
    # the simulators assume too. Without it a testbench that has one and a
    # design file that doesn't (the usual case) raise TIMESCALEMOD.
    # --Werror-LATCH / --Werror-MULTIDRIVEN: LibreLane's own lint step fails the run on these
    # (LINTER_ERROR_ON_LATCH, on by default), so the check here calls them errors too
    # instead of warnings that then stop the flow at Lint Errors Checker.
    flags: List[str] = ["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--timescale", "1ns/1ps",
                        "--Werror-LATCH", "--Werror-MULTIDRIVEN"]
    if timing:
        flags.append("--timing")
    for w in disable_warnings or ():
        flags.append("-Wno-" + str(w))
    if top:
        flags += ["--top-module", str(top)]
    for inc in (include_dirs or []):
        flags.append("-I" + str(inc))
    for k, v in (defines or {}).items():
        flags.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
    flags.extend(str(s) for s in sources)
    if run_mode == "container":
        from . import container_run

        img = image or container_run.image_ref()
        abs_design = str(Path(design_dir).resolve())
        shell = " ".join(_shquote(p) for p in flags)
        return [engine, "run", "--rm", "-v", f"{abs_design}:/work", "-w", "/work",
                img, "bash", "-lc", shell]
    return flags


_VERSION_RX = re.compile(r"Verilator\s+(\d+)\.(\d+)")
_major_cache: Dict[str, Optional[int]] = {}


def verilator_major(exe: str = "verilator") -> Optional[int]:
    """The host Verilator's major version (``verilator --version``), cached;
    None when it can't be run."""
    if exe in _major_cache:
        return _major_cache[exe]
    major: Optional[int] = None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
        m = _VERSION_RX.search(out or "")
        major = int(m.group(1)) if m else None
    except Exception:
        major = None
    _major_cache[exe] = major
    return major


def _strip_dir(entry: str, design_dir: Path) -> Optional[str]:
    """A config path entry (``dir::x``, absolute, or design-relative) as the
    path a job running in the design dir uses: design-relative when inside it,
    else absolute. None for forms LanEx can't resolve (``ref::``, ``expr::``)."""
    e = str(entry).strip()
    if not e or e.startswith(("ref::", "refg::", "expr::", "pdk_dir::")):
        return None
    s = e[len("dir::"):] if e.startswith("dir::") else e
    p = Path(s) if os.path.isabs(s) else design_dir / s
    try:
        return p.resolve().relative_to(design_dir).as_posix()
    except ValueError:
        return str(p.resolve())
    except OSError:
        return None


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None and str(x).strip()]
    return [x for x in str(v).split() if x]


def parse_defines(v: Any) -> Dict[str, Optional[str]]:
    """``VERILOG_DEFINES`` (``["A", "W=8"]`` or ``"A W=8"``) as ``{name: value}``,
    in order; a bare name has the value None."""
    out: Dict[str, Optional[str]] = {}
    for item in _as_list(v):
        name, eq, val = item.partition("=")
        name = name.strip()
        if re.fullmatch(r"[A-Za-z_][\w$]*", name):
            out[name] = val.strip() if eq else None
    return out


def config_inputs(design_dir: str | Path, *, for_lint: bool = False) -> Dict[str, Any]:
    """The Verilog the flow itself reads, from the project's config, so the
    IDE's syntax check and simulation see what a run sees instead of every
    ``.v`` in the folder (a stale copy then duplicates a module):

    * ``files``: ``VERILOG_FILES`` resolved the way LibreLane does (``dir::``,
      globs), design-relative where inside the project; ``missing`` the ones
      the config names that don't exist;
    * ``defines``: ``VERILOG_DEFINES`` (for the lint, ``LINTER_DEFINES`` when
      the config sets it, as LibreLane's own lint step does);
    * ``include_dirs``: ``VERILOG_INCLUDE_DIRS``.

    ``mode`` is ``"list"`` when the config names its files, ``"none"`` when
    there is no usable config or no ``VERILOG_FILES`` (the caller falls back to
    the project's files), ``"unknown"`` for forms LanEx can't resolve.
    """
    from . import configedit

    d = Path(design_dir).resolve()
    out: Dict[str, Any] = {"mode": "none", "files": [], "missing": [], "defines": {},
                           "include_dirs": [], "name": None, "reason": None, "disable_warnings": []}
    try:
        info = configedit.read_config(d)
    except Exception as ex:  # pragma: no cover - defensive
        out["reason"] = str(ex)
        return out
    out["name"] = info.get("name")
    data = info.get("data") or {}
    if not info.get("editable") or not isinstance(data, dict):
        out["reason"] = info.get("reason")
        return out
    src = configedit.resolve_sources(d, data)
    out["mode"] = src["mode"]
    out["reason"] = src.get("reason")
    for f in src.get("files") or []:
        rel = _strip_dir(f, d)
        if rel is None:
            continue
        if not (d / rel).is_file() and not Path(rel).is_file():
            out["missing"].append(rel)
            continue
        if rel not in out["files"]:
            out["files"].append(rel)
    defs = data.get("LINTER_DEFINES") if for_lint and data.get("LINTER_DEFINES") is not None else data.get("VERILOG_DEFINES")
    out["defines"] = parse_defines(defs)
    if for_lint:
        # The defines LibreLane's own lint step always adds (steps/verilator.py),
        # so `ifdef __librelane__ / PDK_sky130A code reads the same here as in a run.
        implicit: Dict[str, Optional[str]] = {"__librelane__": None, "__pnr__": None}
        pdk, scl = data.get("PDK"), data.get("STD_CELL_LIBRARY")
        if isinstance(pdk, str) and re.fullmatch(r"[\w.]+", pdk):
            implicit["PDK_" + pdk] = None
        if isinstance(scl, str) and re.fullmatch(r"[\w.]+", scl):
            implicit["SCL_" + scl] = None
        pwr = data.get("VERILOG_POWER_DEFINE")
        if isinstance(pwr, str) and re.fullmatch(r"[A-Za-z_]\w*", pwr):
            implicit[pwr] = None
        out["defines"] = {**implicit, **out["defines"]}
        out["disable_warnings"] = [w for w in _as_list(data.get("LINTER_DISABLE_WARNINGS"))
                                   if re.fullmatch(r"[A-Z0-9_]+", w)]
    for inc in _as_list(data.get("VERILOG_INCLUDE_DIRS")):
        rel = _strip_dir(inc, d)
        if rel is not None and rel not in out["include_dirs"]:
            out["include_dirs"].append(rel)
    return out


def merge_inputs(cfg: Dict[str, Any], *, extra_sources: Sequence[str] = (),
                 exclude: Sequence[str] = (), include_dirs: Sequence[str] = (),
                 defines: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The job's sources, include dirs and defines: the config's (from
    :func:`config_inputs`) plus what the IDE adds (testbenches, simulation
    helpers, the folders that hold HDL for ``\\`include``), minus *exclude*."""
    def norm(p: Any) -> str:
        s = str(p).replace("\\", "/")
        return s[2:] if s.startswith("./") else s

    skip = {norm(x) for x in exclude if x}
    sources: List[str] = []
    for s in list(cfg.get("files") or []) + [str(x) for x in extra_sources if x]:
        n = norm(s)
        if n not in skip and n not in sources:
            sources.append(n)
    incs: List[str] = []
    for i in list(cfg.get("include_dirs") or []) + [str(x) for x in include_dirs if x]:
        if i not in incs:
            incs.append(i)
    defs: Dict[str, Any] = dict(cfg.get("defines") or {})
    defs.update(defines or {})
    return {"sources": sources, "include_dirs": incs, "defines": defs}


class LintJob:
    """Run ``verilator --lint-only`` in a worker thread, streaming output to the
    event bus and emitting ``lint_started`` / ``lint_done`` (with parsed
    diagnostics). Independent of the hardening FlowRunner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._timed_out = threading.Event()
        self.last_result: Dict[str, Any] = {"diagnostics": [], "summary": summarize([])}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, argv: Sequence[str], *, design_dir: str | Path,
              env: Optional[Dict[str, str]] = None,
              timeout: int = DEFAULT_LINT_TIMEOUT) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"ok": False, "error": "a lint is already running"}
            self._timed_out.clear()
            self._thread = threading.Thread(
                target=self._run, args=(list(argv), str(design_dir), env or {}, int(timeout)),
                daemon=True, name="librelane.gui.LintJob",
            )
            self._thread.start()
        return {"ok": True}

    def _kill(self) -> None:
        """Terminate the lint process and its children (whole group on POSIX)."""
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), __import__("signal").SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _run(self, argv, design_dir, env, timeout) -> None:
        from . import container_run, events, platform_env

        events.publish("lint_started", {"argv": argv})
        # PATH must be Linux-only on WSL so a bare ``verilator`` argv resolves to
        # the real Linux build, never the Windows one on /mnt/c (which hangs /
        # fails and would wedge this job).
        full_env = {**os.environ, **env}
        full_env["PATH"] = platform_env.linux_only_path(full_env.get("PATH"))
        out_lines: List[str] = []
        rc = -1
        watchdog: Optional[threading.Timer] = None
        try:
            # Own session so the watchdog can kill the whole group on POSIX.
            popen_kw: Dict[str, Any] = {}
            if os.name == "posix":
                popen_kw["start_new_session"] = True
            self._proc = subprocess.Popen(
                argv, cwd=design_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=full_env, **popen_kw,
            )
            if timeout and timeout > 0:
                def _on_timeout() -> None:
                    self._timed_out.set()
                    events.publish("log", {"source": "lint", "message":
                        f"lint exceeded {timeout}s and was stopped (the linter wedged — on WSL a "
                        "Windows verilator on the PATH can hang; install the Linux build)."})
                    self._kill()
                watchdog = threading.Timer(timeout, _on_timeout)
                watchdog.daemon = True
                watchdog.start()
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                clean = container_run.strip_ansi(line.rstrip("\n"))
                out_lines.append(clean)
                if clean:
                    events.publish("log", {"source": "lint", "message": clean})
            rc = self._proc.wait()
        except FileNotFoundError as ex:
            events.publish("log", {"source": "lint", "message": f"lint failed to launch: {ex}"})
            rc = 127
        except Exception as ex:  # pragma: no cover - environment dependent
            events.publish("log", {"source": "lint", "message": f"lint error: {ex}"})
            rc = 1
        finally:
            if watchdog is not None:
                watchdog.cancel()
            self._proc = None
        diags = parse_verilator("\n".join(out_lines))
        try:
            from .vhdltools import parse_ghdl
            diags += parse_ghdl("\n".join(out_lines))
        except Exception:  # pragma: no cover - the Verilog check still stands
            pass
        summary = summarize(diags)
        verdict = lint_verdict(rc, summary, out_lines, timed_out=self._timed_out.is_set())
        self.last_result = {"diagnostics": diags, "summary": summary, **verdict}
        events.publish("lint_done", {
            **verdict,
            "returncode": rc,
            "diagnostics": diags,
            "summary": summary,
            "timed_out": self._timed_out.is_set(),
        })


def lint_verdict(rc: int, summary: Dict[str, Any], out_lines: Sequence[str], *,
                 timed_out: bool = False) -> Dict[str, Any]:
    """``{ok, message?}`` for one lint. Verilator runs with ``-Wno-fatal``, so
    exit 0 means it ran and found no error. A non-zero exit is never a pass:
    with parsed errors it is a lint failure; with none (127: Verilator wasn't
    found; a crash; an error line LanEx can't parse) nothing was checked, so
    the raw output is the message."""
    ok = rc == 0 and not summary.get("errors") and not timed_out
    out: Dict[str, Any] = {"ok": ok}
    if not ok and not summary.get("errors") and not timed_out:
        tail = [ln for ln in out_lines if ln.strip()][-12:]
        what = ("Verilator wasn't found" if rc == 127
                else f"Verilator stopped with exit code {rc} and no error LanEx could read")
        out["message"] = what + ((": " + "\n".join(tail)) if tail else ".")
    return out


# Process-wide singleton.
job = LintJob()
