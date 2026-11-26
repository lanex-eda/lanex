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
"""Functional simulation (Phase 3.5) — Verilator or Icarus Verilog.

Two engines, picked to fit the testbench:

* **Icarus Verilog** (``iverilog`` + ``vvp``) — event-driven, full 4-state,
  honours ``#delay`` timing controls and classic ``initial`` testbenches. The
  better fit for hand-written RTL testbenches (and beginners). The host's when
  installed, else the one on the LibreLane image's PATH (3.0.4 ships it).
* **Verilator** (``--binary``) — fast, cycle-based, SV-leaning. The host's
  only: the image has Verilator but no make or C++ compiler, so ``--binary``
  can't build a simulator there.

Either way this is our own subprocess wrapping a binary (image or host) via the
public image + ``tools.resolve_engine`` only — no new dependency and no new
LibreLane API. LibreLane itself only *lints* with Verilator; this does not change
that. The build is pure/unit-testable; :class:`SimJob` does the subprocess + SSE.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import container_run, events, lint

# Wall-clock ceiling for a single simulation. A hand-written testbench with a
# free-running clock (``always #5 clk = ~clk``) and no ``$finish`` never
# terminates — the event queue never empties — so the sim process would run
# forever, the SimJob thread would stay alive, and ``job.running`` would stay
# True, blocking every later "Build & run" with "a simulation is already
# running". The timeout kills a runaway sim so the job always frees itself; the
# (partial) VCD it already wrote is still loaded. Cancel/Stop bypasses it.
DEFAULT_SIM_TIMEOUT = 120
# The time limit a person can choose (Settings > Editor), in seconds.
MIN_SIM_TIMEOUT = 5
MAX_SIM_TIMEOUT = 3600

# The time unit and precision of a file without a `timescale.
DEFAULT_TIMESCALE = "1ns/1ps"


def sim_timeout(value: Any) -> int:
    """The time limit for one simulation: *value* in seconds (the IDE sends the
    Settings choice), clamped to 5 s .. 1 h; the default when missing or junk."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_SIM_TIMEOUT
    if n <= 0:
        return DEFAULT_SIM_TIMEOUT
    return max(MIN_SIM_TIMEOUT, min(MAX_SIM_TIMEOUT, n))

# A simulation keeps at most this many output lines for its verdict.
JUDGE_LINES_MAX = 50000

# Compiled simulators (Icarus .vvp, Verilator obj_dir) live here, inside the
# project but out of the way, like cocotb's sim_build/.
SIM_BUILD = "sim_build"


def _verilator_shell(*, top: str, sources: Sequence[str], testbench: str,
                     defines: Optional[Dict[str, Any]], include_dirs: Optional[Sequence[str]],
                     trace: str, out_name: str = "sim_top", params: Optional[Dict[str, str]] = None,
                     plusargs: Sequence[str] = (), build_dir: str = SIM_BUILD) -> str:
    """The ``bash -lc`` command that builds + runs the testbench with Verilator 5.

    Uses ``--binary`` (Verilator 5 standalone executable) and ``--trace`` (VCD) /
    ``--trace-fst`` (FST). All paths are relative to the working dir (= design
    dir / container mount). Build products go to *build_dir* (``sim_build/``,
    or ``sim_build/<bench>/`` for Run all) so the project folder holds only the
    user's files (the waveform is still written where the testbench's
    ``$dumpfile`` says, relative to the project). *params* override the
    bench's own parameters (``-G``), *plusargs* go to the simulator."""
    trace_flag = "--trace-fst" if trace == "fst" else "--trace"
    # --timescale: the unit for files that have no `timescale. Every template
    # (and most hand-written projects) has one in the testbench and none in the
    # design, which Verilator reports as TIMESCALEMOD (an error in some
    # versions); Icarus and the syntax check (lint.py) assume 1ns/1ps too.
    mdir = build_dir + "/obj_dir"
    parts: List[str] = ["verilator", "--binary", trace_flag, "--trace-structs",
                        "-Wno-fatal", "--timescale", DEFAULT_TIMESCALE,
                        "--Mdir", mdir, "-o", out_name, "--top-module", top]
    for k, v in (params or {}).items():
        parts.append(f"-G{k}={v}")
    for k, v in (defines or {}).items():
        parts.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
    for inc in (include_dirs or []):
        parts.append(f"-I{inc}")
    parts.extend(str(s) for s in sources)
    parts.append(str(testbench))
    build = " ".join(_shquote(p) for p in parts)
    run = " ".join(_shquote(p) for p in [f"./{mdir}/{out_name}"] + list(plusargs))
    # Build, then run the produced simulator (which dumps the VCD/FST).
    # Verilator makes --Mdir but not its parent: a fresh project (no Icarus run
    # made sim_build/ before) failed with "Cannot write sim_build/obj_dir/...".
    return f"set -e; mkdir -p {_shquote(build_dir)}; {build}; echo {RUN_MARK}; {run}"


def _iverilog_shell(*, top: str, sources: Sequence[str], testbench: str,
                    defines: Optional[Dict[str, Any]], include_dirs: Optional[Sequence[str]],
                    out_name: str = "sim.vvp", params: Optional[Dict[str, str]] = None,
                    plusargs: Sequence[str] = (), build_dir: str = SIM_BUILD) -> str:
    """The ``bash -lc`` command that compiles + runs the testbench with Icarus.

    ``iverilog -g2012`` (Verilog-2005 + SV subset) compiles to a ``.vvp`` that
    ``vvp`` then executes; the testbench's ``$dumpfile``/``$dumpvars`` writes the
    VCD (whatever name it chooses — :class:`SimJob` detects the produced file).
    The TB module is the elaboration top (``-s``). *params* become
    ``-P<top>.<name>=<value>``: Icarus applies only that form (``-P<name>`` and
    a deeper path are ignored without a word). ``vvp -n`` makes ``$stop`` end
    the run instead of dropping into vvp's interactive prompt."""
    out = f"{build_dir}/{out_name}"
    parts: List[str] = ["iverilog", "-g2012", "-s", top, "-o", out]
    for k, v in (params or {}).items():
        parts.append(f"-P{top}.{k}={v}")
    for k, v in (defines or {}).items():
        parts.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
    for inc in (include_dirs or []):
        parts.append(f"-I{inc}")
    parts.extend(str(s) for s in sources)
    parts.append(str(testbench))
    build = " ".join(_shquote(p) for p in parts)
    run = " ".join(_shquote(p) for p in ["vvp", "-n", out] + list(plusargs))
    return f"set -e; mkdir -p {_shquote(build_dir)}; {build}; echo {RUN_MARK}; {run}"


# GHDL is in the LibreLane 3.0.4 image's Nix store but not on its PATH, and it
# needs the store's gcc wrapper, binutils and zlib to elaborate. This finds
# them; the probe (vhdl.caps) runs the same lines.
GHDL_PRELUDE = (
    "G=$(ls -d /nix/store/*-ghdl-bin-*/bin 2>/dev/null | head -1); "
    "CC=$(ls -d /nix/store/*-gcc-wrapper-*/bin 2>/dev/null | head -1); "
    "BU=$(ls -d /nix/store/*-binutils-wrapper-*/bin 2>/dev/null | head -1); "
    "Z=$(dirname \"$(ls /nix/store/*-zlib-1*/lib/libz.so 2>/dev/null | head -1)\" 2>/dev/null); "
    "[ -n \"$G\" ] || { echo 'LanEx: GHDL is not in this LibreLane image.'; exit 127; }; "
    "export PATH=\"$G:$CC:$BU:$PATH\"; "
)


def _ghdl_shell(*, top: str, sources: Sequence[str], testbench: str, generics: Optional[Dict[str, str]] = None,
                build_dir: str = SIM_BUILD, stop_time: Optional[str] = None, vcd: str = "dump.vcd") -> str:
    """Analyse, elaborate and run a VHDL testbench with GHDL (VHDL-2008).
    ``--assert-level=error`` makes a failed ``assert ... severity error`` end
    the run with a non-zero exit, so a self-checking bench fails loudly."""
    work = build_dir + "/ghdl"
    files = [str(x) for x in sources] + [str(testbench)]
    ana = " ".join(_shquote(p) for p in ["ghdl", "-a", "--std=08", f"--workdir={work}"] + files)
    exe = f"{work}/{top.lower()}"
    ela = " ".join(_shquote(p) for p in ["ghdl", "-e", "--std=08", f"--workdir={work}", "-o", exe]) + \
        ' -Wl,-L"$Z" -Wl,-Wl,-rpath="$Z" ' + _shquote(top)
    run_parts = ["./" + exe, f"--vcd={vcd}", "--assert-level=error"]
    if stop_time:
        run_parts.append(f"--stop-time={stop_time}")
    for k, v in (generics or {}).items():
        run_parts.append(f"-g{k}={v}")
    run = " ".join(_shquote(p) for p in run_parts)
    return f"set -e; {GHDL_PRELUDE}mkdir -p {_shquote(work)}; {ana}; {ela}; echo {RUN_MARK}; {run}"


# Printed between the build and the run, so a failure can be told apart:
# no marker means it didn't build.
RUN_MARK = "@@LANEX-RUN"


def _shquote(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./=:+-]+", s or ""):
        return s
    return "'" + str(s).replace("'", "'\\''") + "'"


def build_sim_command(
    design_dir: str | Path,
    *,
    top: str,
    sources: Sequence[str],
    testbench: str,
    defines: Optional[Dict[str, Any]] = None,
    include_dirs: Optional[Sequence[str]] = None,
    trace: str = "vcd",
    run_mode: str = "container",
    engine: str = "docker",
    image: Optional[str] = None,
    container_name: Optional[str] = None,
    sim_engine: str = "verilator",
    params: Optional[Dict[str, str]] = None,
    plusargs: Sequence[str] = (),
    build_dir: str = SIM_BUILD,
    user_args: Sequence[str] = (),
) -> List[str]:
    """Build the full argv to simulate the testbench.

    ``sim_engine`` selects ``"iverilog"``, ``"verilator"`` or ``"ghdl"`` (a
    VHDL bench, in the image). Container mode wraps the build+run in
    ``<engine> run --rm -v <design>:/work -w /work <image> bash -lc '…'``
    (mount only the design dir; *user_args* is ``--user uid:gid`` where
    rootful Docker would leave root-owned files in the project); local mode
    is a bare ``bash -lc '…'`` (the caller sets ``cwd=design_dir``).
    Verilator is host-only (the image can't compile its C++); the caller
    enforces that. Pure."""
    if sim_engine == "iverilog":
        shell = _iverilog_shell(top=top, sources=sources, testbench=testbench,
                                defines=defines, include_dirs=include_dirs, params=params,
                                plusargs=plusargs, build_dir=build_dir)
    elif sim_engine == "ghdl":
        shell = _ghdl_shell(top=top, sources=sources, testbench=testbench, generics=params, build_dir=build_dir)
    else:
        shell = _verilator_shell(top=top, sources=sources, testbench=testbench,
                                 defines=defines, include_dirs=include_dirs, trace=trace, params=params,
                                 plusargs=plusargs, build_dir=build_dir)
    if run_mode == "container":
        img = image or container_run.image_ref()
        name = container_name or ("ll-sim-" + uuid.uuid4().hex[:12])
        abs_design = str(Path(design_dir).resolve())
        return [engine, "run", "--rm", "--name", name, *user_args, "-e", "HOME=/tmp", "-v", f"{abs_design}:/work",
                "-w", "/work", img, "bash", "-lc", shell]
    return ["bash", "-lc", shell]


def bench_stem(testbench: str) -> str:
    """A testbench's own folder name under sim_build/ (``verify/fifo_tb.v`` ->
    ``fifo_tb``; unusual characters become ``_``)."""
    stem = Path(str(testbench)).stem
    return re.sub(r"[^A-Za-z0-9_.-]", "_", stem) or "bench"


_PARAM_VALUE_RX = re.compile(
    r"^(?:[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"            # 300, -2, 1.5, 1e3
    r"|\d*\s*'[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ_?]+"         # 8'hFF, 'd5
    r"|\"[^\"\\\n]*\")$")                                  # "a string"
_NAME_RX = re.compile(r"^[A-Za-z_][\w$]*$")


def check_params(params: Any) -> Tuple[Dict[str, str], List[str]]:
    """The run settings' parameter values, cleaned: ``({name: value}, problems)``.
    A value is a number, a sized literal (``8'hFF``) or a quoted string; an
    empty field means "the bench's default" and is left out."""
    out: Dict[str, str] = {}
    bad: List[str] = []
    if not isinstance(params, dict):
        return out, bad
    for k, v in params.items():
        k = str(k).strip()
        v = "" if v is None else str(v).strip()
        if not v:
            continue
        if not _NAME_RX.match(k):
            bad.append(f"{k}: not a parameter name")
            continue
        if not _PARAM_VALUE_RX.match(v):
            bad.append(f"{k}: use a number, like 300 or 16'h1F, or a string in quotes")
            continue
        out[k] = v.replace(" ", "") if not v.startswith('"') else v
    return out, bad


def check_plusargs(plusargs: Any) -> Tuple[List[str], List[str]]:
    """``+name`` / ``+name=value`` words (a string is split on spaces)."""
    if isinstance(plusargs, str):
        plusargs = plusargs.split()
    out: List[str] = []
    bad: List[str] = []
    for a in plusargs or []:
        a = str(a).strip()
        if not a:
            continue
        if not re.match(r"^\+[A-Za-z_][\w$+]*(=\S*)?$", a):
            bad.append(f"{a}: a plusarg starts with + (like +seed=42 or +verbose)")
            continue
        out.append(a)
    return out, bad


def check_defines(defines: Any) -> Tuple[Dict[str, Optional[str]], List[str]]:
    """``NAME`` / ``NAME=value`` words from the run settings."""
    from .lint import parse_defines
    if isinstance(defines, dict):
        items = [f"{k}={v}" if v not in (None, "") else str(k) for k, v in defines.items()]
    elif isinstance(defines, str):
        items = defines.split()
    else:
        items = [str(x) for x in (defines or [])]
    bad = [x for x in items if not re.match(r"^[A-Za-z_][\w$]*(=\S*)?$", x)]
    return parse_defines([x for x in items if x not in bad]), [f"{x}: a define is NAME or NAME=value" for x in bad]


def is_vhdl(path: str) -> bool:
    return str(path).lower().endswith((".vhd", ".vhdl"))


_TB_PATTERNS = ("*_tb.v", "*_tb.sv", "tb_*.v", "tb_*.sv", "*testbench*.v", "*testbench*.sv",
                "*_tb.vhd", "tb_*.vhd", "*_tb.vhdl", "tb_*.vhdl")

# `module <name>` — used to auto-derive the testbench's top module so the user
# doesn't have to type it (typing the DUT name instead is the #1 reason iverilog
# elaborates the wrong top, runs no $dumpvars, and produces no VCD).
_MODULE_RX = re.compile(r"^\s*module\s+([A-Za-z_]\w*)", re.MULTILINE)


def top_module_of(design_dir: str | Path, testbench: str) -> Optional[str]:
    """Best-effort: the testbench's top module name (the module that has no
    ports — i.e. the bench — else the last module declared). Returns ``None`` if
    the file can't be read. Pure / stdlib."""
    try:
        text = (Path(design_dir) / testbench).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if is_vhdl(testbench):
        ents = re.findall(r"^\s*entity\s+([A-Za-z]\w*)\s+is", text, re.MULTILINE | re.IGNORECASE)
        return ents[-1] if ents else None
    names = _MODULE_RX.findall(text)
    if not names:
        return None
    # A classic testbench module declares no ports: ``module foo_tb;``. Prefer
    # the first such; otherwise fall back to the last module in the file.
    for m in re.finditer(r"^\s*module\s+([A-Za-z_]\w*)\s*([;(])", text, re.MULTILINE):
        if m.group(2) == ";":
            return m.group(1)
    return names[-1]


def find_testbenches(design_dir: str | Path) -> List[str]:
    """Heuristically locate testbench files (rel paths), searching ``verify/``
    first then the whole design dir. De-duplicated, ``runs/`` excluded."""
    base = Path(design_dir)
    found: List[str] = []
    seen = set()

    def add(p: Path) -> None:
        try:
            rel = str(p.relative_to(base))
        except ValueError:
            return
        if "runs/" in rel or rel.startswith("runs"):
            return
        if rel not in seen:
            seen.add(rel)
            found.append(rel)

    for pat in _TB_PATTERNS:
        for p in sorted(base.glob("verify/" + pat)):
            add(p)
    # One walk for every pattern, never entering a folder whose contents add()
    # would drop anyway (``runs/`` holds ~800 files a run; slow on /mnt/c):
    # anything below a folder named "...runs", or below a top-level "runs*".
    from fnmatch import fnmatch
    from .fastscan import iter_entries
    entries = list(iter_entries(base, lambda parts: parts[-1].endswith("runs")
                                or parts[0].startswith("runs")))
    for pat in _TB_PATTERNS:
        for p in sorted(e for e in entries if fnmatch(e.name, pat)):
            add(p)
    return found


def sim_verdict(rc: int, timed_out: bool, wave: Any, cancelled: bool) -> Dict[str, bool]:
    """Pure sim outcome → ``{ok, partial}``.

    A timed-out runaway bench (free-running clock / no ``$finish``) that still
    wrote a waveform is a soft SUCCESS — the button re-enables and the dump
    loads — but that dump is INCOMPLETE, so ``partial`` is set so the UI can
    carry a durable badge and no downstream consumer mistakes a truncated dump
    for a completed simulation (Fear #5 / N2). ``partial`` is never true without
    a waveform, and a cancelled run is neither ok-by-timeout nor partial.
    """
    has_wave = bool(wave)
    ok = (rc == 0) or (bool(timed_out) and has_wave)
    partial = bool(timed_out) and has_wave and not bool(cancelled)
    return {"ok": ok, "partial": partial}


# ------------------------------------------------------------------ verdict

# A line a self-checking bench prints: PASS / FAIL (upper case, at the start,
# after an optional "[time]" and "TEST"), as Verilator's regression and
# OpenTitan's dvsim read them. "No error found" or "pass 3 of 10" are not.
_VERDICT_LINE_RX = re.compile(r"^\s*(?:\[\s*\d+\s*\]\s*)?(?:TEST\s+|ALL TESTS\s+)?(PASS(?:ED)?|FAIL(?:ED|URE)?)\b")
_FINISH_RX = re.compile(r"\$finish called at (\d+) \((\d+\s*[munpf]?s)\)|Verilog \$finish|- Verilator: \$finish at ([\d.]+)\s*([munpf]?s)\b"
                        r"|simulation finished @([\d.]+)\s*([munpf]?s)\b")
_STOP_RX = re.compile(r"\$stop called at (\d+)|Verilog \$stop\b|simulation stopped by --stop-time @([\d.]+)\s*([munpf]?s)")
_IV_ERR_RX = re.compile(r"^(ERROR|FATAL): (?:([^:\s]+):(\d+): )?(.*)$")
_TIME_LINE_RX = re.compile(r"^\s*Time:\s*(\d+)\b")
# Verilator stamps run-time messages with the time: "[35000] %Error: ..." in
# precision units, or in the bench's $timeformat ("[46.000 ns] %Fatal: ...").
_VLT_RT_RX = re.compile(r"^\[\s*([\d.]+)\s*([munpf]?s)?\s*\]\s*%(Error|Fatal)\b(?::\s*([^:\s]+):(\d+):)?\s*(.*)$")
_GHDL_RX = re.compile(r"^([^:\s]+):(\d+):(\d+):@([\d.]+)\s*([munpf]?s):\((assertion (?:error|failure)|report (?:error|failure))\):\s*(.*)$")
_AT_TIME_RX = re.compile(r"\bat\s+(?:time\s+)?([\d.]+)\s*(fs|ps|ns|us|µs|ms|s)\b")


def _time_in(line: str) -> Optional[Dict[str, Any]]:
    m = _AT_TIME_RX.search(line)
    if m:
        unit = m.group(2).replace("µ", "u")
        return {"value": float(m.group(1)), "unit": unit}
    return None


def judge(lines: Sequence[str], *, rc: int, timed_out: bool = False, cancelled: bool = False,
          engine: str = "iverilog", built: Optional[bool] = None, started: bool = True) -> Dict[str, Any]:
    """What one simulation means, from its exit code and its output, first
    match wins (exit codes alone mislead: Icarus ``$error`` exits 0, and
    Verilator keeps running the rest of the timestep after ``$finish``):

    ``start`` couldn't start · ``build`` didn't build · ``stopped`` you
    stopped it · ``timeout`` no ``$finish`` in time · ``fail`` a non-zero
    exit or a simulator-stamped error or a FAIL line · ``pass`` a PASS line
    and nothing failed · ``stop`` it ended at ``$stop`` · ``ran`` it reached
    ``$finish`` and printed no verdict.

    Returns ``{state, line, fail_time, finish, fails, passes}``. A time is
    ``{"ticks": n}`` (the simulator's precision, the waveform's own unit) or
    ``{"value": 35.0, "unit": "ns"}``; ``{"end": true}`` means "where the
    run stopped" (the waveform's last moment)."""
    lines = [str(x) for x in lines]
    if built is None:
        built = any(l.strip() == RUN_MARK for l in lines)
    # Only the run's own output counts for the verdict (not the build's).
    if RUN_MARK in (l.strip() for l in lines):
        run_lines = lines[[l.strip() for l in lines].index(RUN_MARK) + 1:]
    else:
        run_lines = [] if not built else lines
    out: Dict[str, Any] = {"state": "ran", "line": "", "fail_time": None, "finish": None, "fails": 0, "passes": 0}
    if not started or (rc == 127 and not built):
        tail = [l for l in lines if l.strip()][-1:] or [""]
        out.update(state="start", line=tail[0])
        return out
    if cancelled:
        out["state"] = "stopped"
        return out
    if not built:
        if timed_out:
            out["state"] = "timeout"
            return out
        out["state"] = "build"
        err = next((l for l in lines if re.search(r"error|Error", l)), "")
        out["line"] = err
        return out
    # the run's messages, up to (and including) the first $finish
    finish_at = None
    for i, l in enumerate(run_lines):
        m = _FINISH_RX.search(l)
        if m:
            finish_at = i
            if m.group(1):
                out["finish"] = {"ticks": int(m.group(1)), "unit": m.group(2).replace(" ", "")}
            elif m.group(3):
                out["finish"] = {"value": float(m.group(3)), "unit": m.group(4)}
            elif m.group(5):
                out["finish"] = {"value": float(m.group(5)), "unit": m.group(6)}
            else:
                out["finish"] = {"end": True}
            break
    upto = run_lines if finish_at is None else run_lines[:finish_at + 1]
    first_fail: Optional[Tuple[str, Optional[Dict[str, Any]]]] = None
    stamped: Optional[Tuple[str, Optional[Dict[str, Any]]]] = None
    verdict_word = None
    verdict_line = ""
    for i, l in enumerate(upto):
        m = _IV_ERR_RX.match(l)
        if m and engine in ("iverilog", "auto"):
            t = None
            if i + 1 < len(upto):
                tm = _TIME_LINE_RX.match(upto[i + 1])
                if tm:
                    t = {"ticks": int(tm.group(1))}
            stamped = stamped or (l, t)
            continue
        m = _VLT_RT_RX.match(l)
        if m:
            t = {"value": float(m.group(1)), "unit": m.group(2)} if m.group(2) else {"ticks": int(float(m.group(1)))}
            stamped = stamped or (l, t)
            continue
        m = _GHDL_RX.match(l)
        if m:
            t = {"value": float(m.group(4)), "unit": m.group(5)}
            if "error" in m.group(6) or "failure" in m.group(6):
                stamped = stamped or (l, t)
            msg = m.group(7)
            vm = _VERDICT_LINE_RX.match(msg)
            if vm:
                word = vm.group(1)
                if word.startswith("FAIL"):
                    out["fails"] += 1
                    first_fail = first_fail or (l, t)
                else:
                    out["passes"] += 1
                verdict_word, verdict_line = word, l
            continue
        if l.startswith("%Error") and "Verilog $stop" not in l and engine == "verilator":
            stamped = stamped or (l, {"end": True})
            continue
        vm = _VERDICT_LINE_RX.match(l)
        if vm:
            word = vm.group(1)
            if word.startswith("FAIL"):
                out["fails"] += 1
                first_fail = first_fail or (l, _time_in(l))
            else:
                out["passes"] += 1
            verdict_word, verdict_line = word, l
        # GHDL's report lines: "tb.vhd:30:5:@95ns:(report note): PASS: ..."
        gm = re.match(r"^[^:\s]+:\d+:\d+:@([\d.]+)\s*([munpf]?s):\(report note\):\s*(.*)$", l)
        if gm:
            vm = _VERDICT_LINE_RX.match(gm.group(3))
            if vm:
                if vm.group(1).startswith("FAIL"):
                    out["fails"] += 1
                    first_fail = first_fail or (l, {"value": float(gm.group(1)), "unit": gm.group(2)})
                else:
                    out["passes"] += 1
                verdict_word, verdict_line = vm.group(1), l
    stop_hit = next((l for l in upto if _STOP_RX.search(l)), None)
    if timed_out:
        out["state"] = "timeout"
        out["line"] = (first_fail or stamped or ("", None))[0]
        return out
    if rc != 0 or stamped or first_fail:
        # Verilator ends a $stop with exit 134 and "%Error: ... Verilog $stop":
        # that alone is "ended at $stop", not a failure.
        if rc != 0 and not stamped and not first_fail and stop_hit:
            out["state"] = "stop"
            out["line"] = stop_hit
            return out
        line, t = first_fail or stamped or (next((l for l in reversed(upto) if l.strip()), ""), None)
        out["state"] = "fail"
        out["line"] = line
        out["fail_time"] = t or ({"end": True} if rc != 0 else None)
        if out["fail_time"] is None and stamped:
            out["fail_time"] = stamped[1]
        if not out["fails"]:
            out["fails"] = 1
        return out
    if verdict_word and verdict_word.startswith("PASS"):
        out["state"] = "pass"
        out["line"] = verdict_line
        return out
    if stop_hit and not out["finish"]:
        out["state"] = "stop"
        out["line"] = stop_hit
        return out
    out["state"] = "ran"
    return out


def plan_sim(design_dir: str | Path, body: Dict[str, Any]) -> Dict[str, Any]:
    """Everything one simulation needs, from what the IDE sent: the engine
    and where it runs (host or image), the files (the config's, plus the
    bench and its helpers), defines, include dirs, parameters, plusargs, the
    time limit and the argv. ``{ok: False, error}`` with plain words when it
    can't run. Shared by Simulate and Run all."""
    from . import lint as lint_mod
    from . import platform_env
    from . import tools as tools_mod

    testbench = str(body.get("testbench") or "").strip()
    if not testbench:
        return {"ok": False, "error": "missing testbench"}
    vhdl_bench = is_vhdl(testbench)
    top = str(body.get("top") or "").strip() or (top_module_of(design_dir, testbench) or "")
    if not top:
        return {"ok": False, "error": "could not determine the testbench top module — open the "
                                      "testbench and ensure it declares `module <name>;`"
                                      if not vhdl_bench else "could not find the testbench's entity"}
    params, bad_p = check_params(body.get("params"))
    plusargs, bad_a = check_plusargs(body.get("plusargs"))
    extra_defs, bad_d = check_defines(body.get("run_defines"))
    bad = bad_p + bad_a + bad_d
    if bad:
        return {"ok": False, "error": "Fix the run settings first: " + "; ".join(bad) + ".", "bad": bad}
    trace = "fst" if body.get("trace") == "fst" else "vcd"
    req_engine = str(body.get("sim_engine") or "auto").lower()
    eng_info = tools_mod.resolve_engine()
    has_container = bool(eng_info.get("ready"))
    if vhdl_bench:
        if not has_container:
            return {"ok": False, "error": "A VHDL simulation uses GHDL from the LibreLane image, which needs "
                                          "Docker or Podman. Set one up on the Tools tab."}
        sim_engine, run_mode = "ghdl", "container"
    else:
        has_iverilog = bool(platform_env.usable_which("iverilog") and platform_env.usable_which("vvp"))
        has_verilator = bool(platform_env.usable_which("verilator"))
        if req_engine == "iverilog":
            if has_iverilog:
                sim_engine, run_mode = "iverilog", "local"
            elif has_container:
                sim_engine, run_mode = "iverilog", "container"
            else:
                return {"ok": False, "error": "Icarus Verilog (iverilog/vvp) isn't installed on this machine and no "
                                              "container engine is available. Install Icarus from the Tools tab, then retry."}
        elif req_engine == "verilator":
            if has_verilator:
                sim_engine, run_mode = "verilator", "local"
            else:
                return {"ok": False, "error": "A Verilator simulation needs Verilator on this machine: the LibreLane "
                                              "image has no C++ build tools to compile one. Install Verilator from the "
                                              "Tools tab, or pick Icarus (Auto uses the image's Icarus)."}
        else:
            if has_iverilog:
                sim_engine, run_mode = "iverilog", "local"
            elif has_verilator:
                sim_engine, run_mode = "verilator", "local"
            elif has_container:
                sim_engine, run_mode = "iverilog", "container"
            else:
                return {"ok": False, "error": "No simulator found. Install Icarus Verilog or Verilator from the "
                                              "Tools tab, or Docker/Podman for the LibreLane image's Icarus."}
    if run_mode == "local" and not platform_env.usable_which("bash"):
        return {"ok": False, "error": "Local simulation needs a POSIX shell (bash), which isn't available here. "
                                      "Use WSL2, or switch the engine to Container mode."}
    engine = None
    env: Dict[str, str] = {}
    container_name = None
    user_args: List[str] = []
    if run_mode == "container":
        engine = eng_info["engine"]
        env = eng_info.get("env") or {}
        container_name = "ll-sim-" + uuid.uuid4().hex[:12]
        try:
            from .configcheck import _permission_args
            user_args = _permission_args(engine)
        except Exception:
            user_args = []
    sources = [s for s in (body.get("sources") or []) if s]
    defines = body.get("defines") if isinstance(body.get("defines"), dict) else {}
    include_dirs = [s for s in (body.get("include_dirs") or []) if s]
    cfg_used: Optional[Dict[str, Any]] = None
    if vhdl_bench:
        # The VHDL design: the config's VHDL_FILES when it names them, else
        # every VHDL file that isn't a testbench (and never another bench).
        sources = _vhdl_sources(design_dir, testbench, body.get("testbenches") or [])
    elif body.get("from_config"):
        ci = lint_mod.config_inputs(design_dir)
        cfg_used = {"mode": ci["mode"], "name": ci["name"], "reason": ci["reason"],
                    "missing": ci["missing"], "files": ci["files"]}
        if ci["mode"] == "list" and ci["files"]:
            m = lint_mod.merge_inputs(
                ci, extra_sources=body.get("extra_sources") or [],
                exclude=[testbench] + [t for t in (body.get("testbenches") or []) if isinstance(t, str)],
                include_dirs=include_dirs, defines=defines)
            sources, include_dirs, defines = m["sources"], m["include_dirs"], m["defines"]
    if not vhdl_bench:
        sources = [x for x in sources if not is_vhdl(x)]
    defines = dict(defines or {})
    defines.update(extra_defs)
    timeout = sim_timeout(body.get("timeout"))
    build_dir = body.get("build_dir") or SIM_BUILD
    argv = build_sim_command(
        design_dir, top=top, sources=sources, testbench=testbench,
        defines=defines, include_dirs=include_dirs,
        trace=trace, run_mode=run_mode, engine=engine or "docker", container_name=container_name,
        sim_engine=sim_engine, params=params, plusargs=plusargs, build_dir=build_dir, user_args=user_args,
    )
    if run_mode == "container" and engine and eng_info.get("sg_wrap"):
        argv = tools_mod.sg_wrap_argv(argv)
    vcd_name = "dump.fst" if (trace == "fst" and sim_engine == "verilator") else "dump.vcd"
    return {"ok": True, "argv": argv, "run_mode": run_mode, "engine": engine, "env": env,
            "container_name": container_name, "vcd_name": vcd_name, "sim_engine": sim_engine, "top": top,
            "timeout": timeout, "sources": list(sources), "config": cfg_used, "testbench": testbench,
            "params": params, "plusargs": plusargs, "run_defines": extra_defs,
            "command": command_text(argv)}


def command_text(argv: Sequence[str]) -> str:
    """The argv as one line someone can paste into a shell (the IDE shows it)."""
    import shlex
    if len(argv) >= 3 and argv[0] == "bash" and argv[1] == "-lc":
        text = str(argv[2])
    else:
        text = shlex.join([str(a) for a in argv])
    # LanEx's own build/run marker means nothing to a person reading it.
    return text.replace("echo " + RUN_MARK + "; ", "")


def _vhdl_sources(design_dir: str | Path, testbench: str, benches: Sequence[str]) -> List[str]:
    from . import configedit
    d = Path(design_dir).resolve()
    out: List[str] = []
    try:
        data = configedit.read_config(d).get("data") or {}
    except Exception:
        data = {}
    vf = data.get("VHDL_FILES")
    if vf:
        from .lint import _as_list, _strip_dir
        for e in _as_list(vf):
            s = e[len("dir::"):] if e.startswith("dir::") else e
            for p in sorted(d.glob(s)) if any(c in s for c in "*?[") else [d / s]:
                rel = _strip_dir(str(p), d)
                if rel and rel not in out and (d / rel).is_file():
                    out.append(rel)
    else:
        from . import projectfiles
        for f in projectfiles.list_files(d).get("files") or []:
            if is_vhdl(f["rel"]) and not projectfiles.is_testbench(f["rel"]):
                out.append(f["rel"])
    skip = {testbench} | {b for b in benches if isinstance(b, str)}
    return [x for x in out if x not in skip]


def vcd_unit(path: str | Path) -> Optional[str]:
    """The ``$timescale`` of a VCD ("1ps"): the unit of the simulator's own
    time stamps, so "46000" can be read as 46 ns."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536).decode("utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"\$timescale\s+(\d+)\s*([munpf]?s)\s+\$end", head)
    return (m.group(1) + m.group(2)) if m else None


def with_units(j: Dict[str, Any], design_dir: str | Path, wave: Optional[str]) -> Dict[str, Any]:
    """Give a judged run's tick times the dump's unit (Icarus and Verilator
    stamp messages in the simulation's precision, which is the VCD's)."""
    if not j or not wave or not str(wave).lower().endswith(".vcd"):
        return j
    unit = None
    for k in ("fail_time", "finish"):
        t = j.get(k)
        if isinstance(t, dict) and t.get("ticks") is not None and not t.get("unit"):
            unit = unit or vcd_unit(Path(design_dir) / wave)
            if unit:
                t["unit"] = unit
    return j


def _limit_core() -> None:
    """In the simulator's child: no core dump. A Verilator bench that fails
    aborts (SIGABRT), and WSL pipes every core to a crash collector that
    writes ~260 MB to the Windows temp folder each time. 1 byte is the
    kernel's "no piped dump" value (0 is not, for a pipe)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (1, hard if hard != resource.RLIM_INFINITY else resource.RLIM_INFINITY))
    except Exception:
        pass


class SimJob:
    """Run a built sim command, streaming stdout→``log`` events and emitting
    ``sim_started`` / ``sim_done``. Cancel kills the process (and force-removes
    the container in container mode)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._timed_out = threading.Event()
        self._container_name: Optional[str] = None
        self._engine: Optional[str] = None
        self._sim_engine = "iverilog"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, argv: Sequence[str], *, design_dir: str | Path, run_mode: str,
              env: Optional[Dict[str, str]] = None, vcd_name: str = "dump.vcd",
              container_name: Optional[str] = None, engine: Optional[str] = None,
              timeout: int = DEFAULT_SIM_TIMEOUT, sim_engine: str = "iverilog") -> Dict[str, Any]:
        with self._lock:
            if self.running or batch.running:
                return {"ok": False, "error": "a simulation is already running"}
            self._sim_engine = sim_engine
            self._cancel.clear()
            self._timed_out.clear()
            self._container_name = container_name
            self._engine = engine
            self._thread = threading.Thread(
                target=self._run,
                args=(list(argv), str(design_dir), run_mode, env or {}, vcd_name, int(timeout)),
                daemon=True, name="librelane.gui.SimJob",
            )
            self._thread.start()
        return {"ok": True}

    def cancel(self) -> None:
        self._cancel.set()
        self._kill()

    def _kill(self) -> None:
        """Terminate the sim process (and its children / its container)."""
        proc = self._proc
        if proc and proc.poll() is None:
            # Local sims are `bash -lc '… ; vvp …'`: terminating bash can orphan
            # vvp/verilator. On POSIX the process is its own session leader (see
            # _run), so signal the whole group; elsewhere fall back to terminate.
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
        if self._container_name and self._engine:
            try:
                subprocess.run([self._engine, "rm", "-f", self._container_name],
                               capture_output=True, timeout=10)
            except Exception:
                pass

    def _run(self, argv, design_dir, run_mode, env, vcd_name, timeout) -> None:
        from . import platform_env

        events.publish("sim_started", {"argv": argv, "timeout": timeout})
        # Linux-only PATH on WSL: a local `bash -lc 'verilator …'` / `iverilog …`
        # must use the Linux build, never a Windows tool on /mnt/c (which fails
        # with "verilator_bin: No such file or directory").
        full_env = {**os.environ, **env}
        full_env["PATH"] = platform_env.linux_only_path(full_env.get("PATH"))
        import time as _time
        started_at = _time.time()
        rc = -1
        lines: List[str] = []
        watchdog: Optional[threading.Timer] = None
        try:
            # Own session so a runaway local sim can be killed as a group (POSIX).
            popen_kw: Dict[str, Any] = {}
            if os.name == "posix":
                # Own session (killed as a group) and no core dumps.
                popen_kw["start_new_session"] = True
                popen_kw["preexec_fn"] = _limit_core
            self._proc = subprocess.Popen(
                argv, cwd=design_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1, env=full_env, **popen_kw,
            )
            if timeout and timeout > 0:
                def _on_timeout() -> None:
                    self._timed_out.set()
                    events.publish("log", {"source": "sim", "message":
                        f"simulation exceeded {timeout}s and was stopped — a free-running "
                        "testbench clock with no $finish runs forever. Add $finish to the bench."})
                    self._kill()
                watchdog = threading.Timer(timeout, _on_timeout)
                watchdog.daemon = True
                watchdog.start()
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                clean = container_run.strip_ansi(line.rstrip("\n"))
                if clean:
                    if len(lines) < JUDGE_LINES_MAX:
                        lines.append(clean)
                    if clean.strip() == RUN_MARK:
                        continue        # LanEx's own build/run marker
                    events.publish("log", {"source": "sim", "message": clean})
            rc = self._proc.wait()
        except FileNotFoundError as ex:
            events.publish("log", {"source": "sim", "message": f"simulation failed to launch: {ex}"})
            rc = 127
        except Exception as ex:  # pragma: no cover - environment dependent
            events.publish("log", {"source": "sim", "message": f"simulation error: {ex}"})
            rc = 1
        finally:
            if watchdog is not None:
                watchdog.cancel()
            self._proc = None
        # Find the waveform the run produced. Verilator's --binary writes the name
        # we hinted, but iverilog testbenches name their own $dumpfile — so prefer
        # the expected name, else the newest *.vcd/*.fst created during this run.
        wave = self._find_waveform(Path(design_dir), vcd_name, started_at)
        timed_out = self._timed_out.is_set()
        cancelled = self._cancel.is_set()
        verdict = sim_verdict(rc, timed_out, wave, cancelled)
        try:
            judged = judge(lines, rc=rc, timed_out=timed_out, cancelled=cancelled, engine=self._sim_engine,
                           started=rc != 127 or any(l.strip() == RUN_MARK for l in lines))
            judged = with_units(judged, design_dir, wave)
        except Exception:  # the verdict is a help, never a reason to lose the result
            judged = None
        events.publish("sim_done", {
            "ok": verdict["ok"],
            "partial": verdict["partial"],
            "returncode": rc,
            "vcd": wave,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "judged": judged,
        })

    @staticmethod
    def _find_waveform(design_dir: Path, hint: str, started_at: float) -> Optional[str]:
        # Only accept the hinted file if THIS run wrote it — otherwise a stale
        # dump.vcd from a previous sim would be falsely reported as the result.
        hinted = design_dir / hint
        try:
            if hinted.is_file() and hinted.stat().st_mtime >= started_at - 1.0:
                return hint
        except OSError:
            pass
        best: Optional[Path] = None
        best_mtime = started_at - 1.0
        try:
            for pat in ("*.vcd", "*.fst", "**/*.vcd", "**/*.fst"):
                for p in design_dir.glob(pat):
                    if "runs/" in str(p.relative_to(design_dir)).replace(os.sep, "/"):
                        continue
                    try:
                        mt = p.stat().st_mtime
                    except OSError:
                        continue
                    if mt >= best_mtime:
                        best_mtime = mt
                        best = p
        except Exception:
            return None
        if best is not None:
            try:
                return str(best.relative_to(design_dir))
            except ValueError:
                return None
        return None


class SimBatch:
    """Run all testbenches, one after another (every template bench writes
    ``dump.vcd`` in the project, so two at once would overwrite each other's
    waveform). Each bench builds in ``sim_build/<bench>/`` and its dump is
    moved to ``sim_build/waves/<bench>.vcd`` before the next one starts.
    Events: ``sim_batch`` with ``phase`` = ``started`` | ``bench`` (one row
    changed) | ``done``; the output streams as ``log`` with source
    ``simbatch``. :meth:`snapshot` gives the whole table (a page that opens
    while it runs, or after a reload)."""

    LOG_KEEP = 4000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._cur_container: Optional[Tuple[str, str]] = None
        self.state: Dict[str, Any] = {"id": None, "rows": [], "done": True}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, design_dir: str | Path, plans: Sequence[Dict[str, Any]], *, rows_meta: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
        with self._lock:
            if self.running or job.running:
                return {"ok": False, "error": "a simulation is already running"}
            self._cancel.clear()
            bid = uuid.uuid4().hex[:10]
            rows = []
            for i, pl in enumerate(plans):
                meta = rows_meta[i] if i < len(rows_meta) else {}
                rows.append({"tb": pl.get("testbench") or meta.get("tb"), "state": "queued" if pl.get("ok") else "start",
                             "line": "" if pl.get("ok") else (pl.get("error") or ""), "ms": 0, "vcd": None,
                             "engine": pl.get("sim_engine"), "top": pl.get("top"), "command": pl.get("command", ""),
                             "fail_time": None, "finish": None, "fails": 0, "passes": 0, "rc": None, "log": []})
            self.state = {"id": bid, "design_dir": str(design_dir), "rows": rows, "done": False,
                          "started": time.time(), "current": None}
            self._thread = threading.Thread(target=self._run, args=(str(design_dir), list(plans)), daemon=True,
                                            name="lanex.SimBatch")
            self._thread.start()
        return {"ok": True, "id": bid}

    def cancel(self) -> None:
        self._cancel.set()
        self._kill()

    def _kill(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
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
        if self._cur_container:
            eng, name = self._cur_container
            try:
                subprocess.run([eng, "rm", "-f", name], capture_output=True, timeout=10)
            except Exception:
                pass

    def snapshot(self, *, with_logs: bool = False) -> Dict[str, Any]:
        st = dict(self.state)
        st["rows"] = [{k: v for k, v in r.items() if with_logs or k != "log"} for r in self.state.get("rows", [])]
        st["running"] = self.running
        return st

    def log(self, i: int) -> List[str]:
        rows = self.state.get("rows") or []
        return list(rows[i]["log"]) if 0 <= i < len(rows) else []

    def _publish_row(self, i: int) -> None:
        r = self.state["rows"][i]
        events.publish("sim_batch", {"phase": "bench", "id": self.state["id"], "index": i,
                                     "row": {k: v for k, v in r.items() if k != "log"}})

    def _run(self, design_dir: str, plans: List[Dict[str, Any]]) -> None:
        import shutil as _sh
        from . import platform_env
        events.publish("sim_batch", {"phase": "started", "id": self.state["id"],
                                     "rows": [{k: v for k, v in r.items() if k != "log"} for r in self.state["rows"]]})
        d = Path(design_dir)
        for i, pl in enumerate(plans):
            row = self.state["rows"][i]
            if not pl.get("ok"):
                self._publish_row(i)
                continue
            if self._cancel.is_set():
                row["state"] = "notrun"
                self._publish_row(i)
                continue
            self.state["current"] = i
            row["state"] = "running"
            self._publish_row(i)
            events.publish("log", {"source": "simbatch", "index": i,
                                   "message": "── " + str(pl.get("testbench")) + " ──"})
            full_env = {**os.environ, **(pl.get("env") or {})}
            full_env["PATH"] = platform_env.linux_only_path(full_env.get("PATH"))
            started_at = time.time()
            t0 = time.monotonic()
            lines: List[str] = []
            rc = -1
            timed = threading.Event()
            self._cur_container = (pl["engine"], pl["container_name"]) if pl.get("container_name") and pl.get("engine") else None
            watchdog = None
            try:
                popen_kw: Dict[str, Any] = {}
                if os.name == "posix":
                    popen_kw["start_new_session"] = True
                    popen_kw["preexec_fn"] = _limit_core
                self._proc = subprocess.Popen(pl["argv"], cwd=design_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, text=True, bufsize=1, env=full_env, **popen_kw)

                def _on_timeout() -> None:
                    timed.set()
                    self._kill()
                watchdog = threading.Timer(int(pl.get("timeout") or DEFAULT_SIM_TIMEOUT), _on_timeout)
                watchdog.daemon = True
                watchdog.start()
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    clean = container_run.strip_ansi(line.rstrip("\n"))
                    if not clean:
                        continue
                    if len(lines) < JUDGE_LINES_MAX:
                        lines.append(clean)
                    if clean.strip() == RUN_MARK:
                        row["state"] = "running"
                        continue
                    row["log"].append(clean)
                    if len(row["log"]) > self.LOG_KEEP:
                        del row["log"][: len(row["log"]) - self.LOG_KEEP]
                    events.publish("log", {"source": "simbatch", "index": i, "message": clean})
                rc = self._proc.wait()
            except FileNotFoundError as ex:
                lines.append(f"simulation failed to launch: {ex}")
                rc = 127
            except Exception as ex:  # pragma: no cover - environment dependent
                lines.append(f"simulation error: {ex}")
                rc = 1
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                self._proc = None
                self._cur_container = None
            cancelled = self._cancel.is_set()
            wave = SimJob._find_waveform(d, pl.get("vcd_name") or "dump.vcd", started_at)
            if wave:
                dest_rel = f"{SIM_BUILD}/waves/{bench_stem(pl['testbench'])}{Path(wave).suffix}"
                try:
                    (d / dest_rel).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(d / wave, d / dest_rel)
                    wave = dest_rel
                except OSError:
                    pass
            j = judge(lines, rc=rc, timed_out=timed.is_set(), cancelled=cancelled, engine=pl.get("sim_engine") or "iverilog",
                      started=rc != 127 or any(l.strip() == RUN_MARK for l in lines))
            j = with_units(j, d, wave)
            row.update(state=j["state"], line=j["line"], fail_time=j["fail_time"], finish=j["finish"],
                       fails=j["fails"], passes=j["passes"], rc=rc, vcd=wave,
                       ms=int((time.monotonic() - t0) * 1000), partial=bool(timed.is_set() and wave))
            self._publish_row(i)
            if cancelled:
                for k in range(i + 1, len(plans)):
                    if self.state["rows"][k]["state"] == "queued":
                        self.state["rows"][k]["state"] = "notrun"
                        self._publish_row(k)
                break
        self.state["done"] = True
        self.state["current"] = None
        self.state["ms"] = int((time.time() - self.state["started"]) * 1000)
        events.publish("sim_batch", {"phase": "done", "id": self.state["id"], "ms": self.state["ms"],
                                     "rows": [{k: v for k, v in r.items() if k != "log"} for r in self.state["rows"]]})


# Process-wide singletons.
job = SimJob()
batch = SimBatch()
