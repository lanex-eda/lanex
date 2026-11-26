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
"""Parse OpenSTA ``report_checks`` output into structured timing paths.

The single most-requested "switch from CLI to GUI" feature is being able to *see*
timing closure: the worst paths, their slack, and the slack distribution — not
just the scalar ``timing__setup__ws``. LibreLane already writes these reports as
part of every STA step (``openroad -path_delay max/min`` →
``<run>/<NN-OpenROAD-STA…>/max.rpt`` for setup, ``min.rpt`` for hold), so this is
pure parsing of existing run output: **no new dependency, no new LibreLane API,
no extra tool run.** Cross-platform (text + ``pathlib`` only).

Report shape (real sky130 output)::

    ======================= nom_tt_025C_1v80 Corner ====================
    Startpoint: y (input port clocked by clk)
    Endpoint: _419_ (rising edge-triggered flip-flop clocked by clk)
    Path Group: clk
    Path Type: max
    Fanout  Cap  Slew  Delay  Time  Description
    ... (the path rows) ...
                       9.995111   data required time
                      -3.960954   data arrival time
    -------------------------------------------------------------------
                       6.034157   slack (MET)
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Time unit for slack/arrival numbers. OpenSTA reports are unitless — the value
# is in the liberty's ``time_unit``, which is **1 ns** for every PDK LanEx
# supports (sky130, gf180, ihp). We surface this ONE constant (never hardcode
# "ns" in the frontend) so the label always tracks a single source of truth; if
# a ps-unit PDK is ever added, change it here (or source it per-run) and every
# display follows. A ps liberty shown as ns would misstate slack ×1000 (Fear G),
# so ``test_supported_pdks_use_ns_time_unit`` canaries the assumption.
TIME_UNIT = "ns"

# A very large magnitude OpenSTA prints for "no path" slacks (e.g. 1e30); treat
# as "no real path" rather than a meaningful number.
_SENTINEL = 1e29

_CORNER_RE = re.compile(r"^=+\s*(\S+)\s+Corner\s*=+\s*$")
_NUM = r"(-?\d+(?:\.\d+)?)"
_SLACK_RE = re.compile(r"^\s*" + _NUM + r"\s+slack\s+\((MET|VIOLATED)\)")
_ARRIVAL_RE = re.compile(r"^\s*" + _NUM + r"\s+data arrival time\s*$")
_REQUIRED_RE = re.compile(r"^\s*" + _NUM + r"\s+data required time\s*$")
_PATH_MAX_LINES = 400  # cap stored path text so a pathological report can't bloat


def _after(label: str, line: str) -> str:
    """``'Startpoint: y (input ...)'`` → ``'y'`` (drop the parenthetical)."""
    rest = line.split(label, 1)[1].strip()
    return rest.split("(", 1)[0].strip()


def parse_report_checks(text: str) -> List[Dict[str, Any]]:
    """Parse every ``report_checks`` path block in *text*.

    Returns a list of ``{startpoint, endpoint, group, type, corner, slack, met,
    arrival, required, path_text}``. Robust to missing fields (any may be None).
    """
    lines = text.splitlines()
    paths: List[Dict[str, Any]] = []
    corner: Optional[str] = None
    cur: Optional[Dict[str, Any]] = None
    buf: List[str] = []

    def _flush() -> None:
        nonlocal cur, buf
        if cur is not None:
            cur["path_text"] = "\n".join(buf[:_PATH_MAX_LINES])
            paths.append(cur)
        cur = None
        buf = []

    for line in lines:
        m = _CORNER_RE.match(line)
        if m:
            corner = m.group(1)
            continue
        if line.startswith("Startpoint:"):
            _flush()
            cur = {
                "startpoint": _after("Startpoint:", line),
                "endpoint": None, "group": None, "type": None,
                "corner": corner, "slack": None, "met": None,
                "arrival": None, "required": None, "path_text": "",
            }
            buf = [line]
            continue
        if cur is None:
            continue
        buf.append(line)
        if line.startswith("Endpoint:"):
            cur["endpoint"] = _after("Endpoint:", line)
        elif line.startswith("Path Group:"):
            cur["group"] = line.split(":", 1)[1].strip()
        elif line.startswith("Path Type:"):
            cur["type"] = line.split(":", 1)[1].strip()
        else:
            ma = _ARRIVAL_RE.match(line)
            if ma:
                cur["arrival"] = float(ma.group(1))
                continue
            mr = _REQUIRED_RE.match(line)
            if mr:
                cur["required"] = float(mr.group(1))
                continue
            ms = _SLACK_RE.match(line)
            if ms:
                cur["slack"] = float(ms.group(1))
                cur["met"] = (ms.group(2) == "MET")
                _flush()  # slack line ends the block
    _flush()
    return paths


def _sta_dirs(run_dir: Path) -> List[Path]:
    """STA step dirs a run's results stand on, in flow order.

    A run resumed from another one (``--from`` a later step) keeps its
    earlier STA folders in the run it was resumed from: follow the lineage
    (``history.lineage_steps``), as Verify does, or a branch run started
    after STAPostPNR would show "no timing report"."""
    try:
        from . import history
        lin = [s["path"] for s in history.lineage_steps(run_dir)
               if "sta" in s["dir"].partition("-")[2].lower() and s["path"].is_dir()]
        if lin:
            return lin
    except Exception:
        pass
    out: List[Tuple[int, Path]] = []
    for entry in run_dir.iterdir():
        if not entry.is_dir():
            continue
        prefix, _, rest = entry.name.partition("-")
        if not prefix.isdigit():
            continue
        if "sta" in rest.lower():
            out.append((int(prefix), entry))
    out.sort(key=lambda t: t[0])
    return [p for _, p in out]


# Setup paths come from the ``-path_delay max`` report, hold from ``min``.
_REPORT_FOR = {"setup": "max.rpt", "hold": "min.rpt"}


def _pick_reports(run_dir: Path, kind: str) -> Tuple[List[Tuple[Path, Optional[str]]], Optional[Path]]:
    """All report files for *kind* from the most-final STA step.

    Multi-corner STA steps (``OpenROAD.STAPostPNR``) write one report per
    corner in per-corner SUBDIRS (``55-openroad-stapostpnr/max_ss_100C_1v60/
    max.rpt``); single-corner mid-PnR steps write a top-level ``max.rpt``.
    Only checking the top level silently skipped the final signoff STA and fell
    back to a mid-PnR report — pre-route numbers presented as if final (a run
    failing setup in its slow corner showed MET). So: for each STA dir in
    finality order, take the top-level report OR every per-corner one; the
    subdir name rides along as the corner label (per-corner files have no
    corner banner for the parser to find).

    Returns ``([(report_file, corner_hint), …], step_dir)``.
    """
    fname = _REPORT_FOR.get(kind, "max.rpt")
    dirs = _sta_dirs(run_dir)
    post = [d for d in dirs if "post" in d.name.lower()]
    ordered = (post[::-1] + dirs[::-1]) if post else dirs[::-1]
    for d in ordered:
        f = d / fname
        if f.is_file():
            return [(f, None)], d
        corner_files: List[Tuple[Path, Optional[str]]] = []
        try:
            for sub in sorted(d.iterdir(), key=lambda p: p.name):
                cf = sub / fname
                if sub.is_dir() and cf.is_file():
                    corner_files.append((cf, sub.name))
        except OSError:
            continue
        if corner_files:
            return corner_files, d
    return [], None


def _histogram(slacks: List[float], bins: int = 12) -> Dict[str, Any]:
    """Bucket slack values into a small histogram for a bar chart."""
    vals = [s for s in slacks if s is not None and abs(s) < _SENTINEL]
    if not vals:
        return {"bins": [], "counts": [], "edges": []}
    lo, hi = min(vals), max(vals)
    if lo == hi:
        return {"bins": [f"{lo:.3f}"], "counts": [len(vals)], "edges": [lo, hi]}
    width = (hi - lo) / bins
    counts = [0] * bins
    edges = [lo + i * width for i in range(bins + 1)]
    for v in vals:
        idx = int((v - lo) / width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    labels = [f"{edges[i]:.3f}" for i in range(bins)]
    return {"bins": labels, "counts": counts, "edges": edges}


# Parsed reports by file, re-read when the file's mtime or size moves: a big
# design's max.rpt is megabytes and the Verify and Analytics pages ask for it
# again on every visit. A few files at most (the newest ones asked for).
_REPORT_CACHE: "OrderedDict[str, Tuple[Tuple[int, int], List[Dict[str, Any]]]]" = OrderedDict()
_REPORT_CACHE_MAX = 6
_REPORT_LOCK = threading.Lock()


def _parsed_report(report: Path) -> List[Dict[str, Any]]:
    """``parse_report_checks`` of *report*, cached (never mutate the list)."""
    st = report.stat()
    key, stamp = str(report), (st.st_mtime_ns, st.st_size)
    with _REPORT_LOCK:
        hit = _REPORT_CACHE.get(key)
        if hit and hit[0] == stamp:
            _REPORT_CACHE.move_to_end(key)
            return hit[1]
    parsed = parse_report_checks(report.read_text(encoding="utf-8", errors="replace"))
    with _REPORT_LOCK:
        _REPORT_CACHE[key] = (stamp, parsed)
        _REPORT_CACHE.move_to_end(key)
        while len(_REPORT_CACHE) > _REPORT_CACHE_MAX:
            _REPORT_CACHE.popitem(last=False)
    return parsed


def timing_paths(run_dir: str | Path, *, kind: str = "setup", limit: int = 100) -> Dict[str, Any]:
    """Structured timing paths for the worst-paths table + slack histogram.

    ``kind`` ∈ {setup, hold}. Returns ``{ok, kind, source, corner(s), total,
    violating, worst_slack, paths[], histogram}``. ``paths`` are sorted
    worst-slack-first and capped to ``limit``. Degrades to ``{ok: False, …}`` when
    no STA report is present (e.g. a partial run), so the UI shows an honest
    empty state instead of an error.
    """
    run_dir = Path(run_dir)
    if kind not in _REPORT_FOR:
        kind = "setup"
    reports, step_dir = _pick_reports(run_dir, kind)
    if not reports:
        return {"ok": False, "kind": kind,
                "error": f"no {kind} timing report (run an STA step / complete the flow first)"}
    paths: List[Dict[str, Any]] = []
    sources: List[str] = []
    for report, corner_hint in reports:
        try:
            parsed = _parsed_report(report)
        except Exception as ex:
            if len(reports) == 1:
                return {"ok": False, "kind": kind, "error": str(ex)}
            continue
        if corner_hint:
            parsed = [p if p.get("corner") else dict(p, corner=corner_hint) for p in parsed]
        paths.extend(parsed)
        try:
            sources.append(str(report.relative_to(run_dir)))
        except Exception:
            sources.append(report.name)
    if not paths and not sources:
        return {"ok": False, "kind": kind,
                "error": f"no readable {kind} timing report in {step_dir.name if step_dir else 'run'}"}
    # Drop sentinel-slack "no real path" blocks from the table/stats.
    real = [p for p in paths if p.get("slack") is not None and abs(p["slack"]) < _SENTINEL]
    real.sort(key=lambda p: p["slack"])  # worst (most negative) first
    violating = sum(1 for p in real if p.get("met") is False)
    worst = real[0]["slack"] if real else None
    corners = sorted({p["corner"] for p in real if p.get("corner")})
    step_name = step_dir.name if step_dir is not None else ""
    fname = _REPORT_FOR[kind]
    source = sources[0] if len(sources) == 1 else f"{step_name}/*/{fname} ({len(sources)} corners)"
    return {
        "ok": True,
        "kind": kind,
        "source": source,
        "sources": sources,
        "step": step_name,
        "corners": corners,
        "total": len(real),
        "violating": violating,
        "worst_slack": worst,
        "unit": TIME_UNIT,
        "paths": real[:max(1, limit)],
        "histogram": _histogram([p["slack"] for p in real]),
    }


# ---------------------------------------------------------------- where it failed
# LibreLane times the design several times (Classic, 3.0.4): before placement
# (STAPrePNR: ideal clocks, no wires, every STA corner), during place and route
# (STAMidPNR, four times, the default corner only) and after routing
# (STAPostPNR: every corner, with extracted parasitics). A corner a step didn't
# time keeps the value an earlier step left in the metrics (LibreLane issue
# #1040), so each step is read only for the corners it actually timed: the
# banners in its max.rpt, or its per-corner folders.

_STA_STEPS = ("OpenROAD.STAPrePNR", "OpenROAD.STAMidPNR", "OpenROAD.STAPostPNR")
_AFTER = {  # the step before each STA, in plain words
    "OpenROAD.STAPrePNR": "before placement",
    "OpenROAD.GlobalPlacement": "after global placement",
    "OpenROAD.RepairDesignPostGPL": "after placement",
    "OpenROAD.DetailedPlacement": "after placement",
    "OpenROAD.CTS": "after clock tree synthesis",
    "OpenROAD.ResizerTimingPostCTS": "after timing repair",
    "OpenROAD.GlobalRouting": "after global routing",
    "OpenROAD.CheckAntennas": "after global routing",
    "OpenROAD.RepairAntennas": "after global routing",
    "OpenROAD.ResizerTimingPostGRT": "after global routing",
    "OpenROAD.STAPostPNR": "after routing",
}
_VIO_RE = re.compile(r"^\[(setup|hold) (in|reg)-(reg|out)\]")


def _base_id(sid: Optional[str]) -> str:
    return re.sub(r"-\d+$", "", sid or "")


def _corners_timed(d: Path) -> List[str]:
    subs = sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "max.rpt").is_file()) if d.is_dir() else []
    if subs:
        return subs
    rpt = d / "max.rpt"
    if not rpt.is_file():
        return []
    seen: List[str] = []
    with rpt.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            m = _CORNER_RE.match(line)
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
            if i > 4000:
                break
    return seen


def _violators(path: Path) -> Dict[str, Dict[str, int]]:
    """``violator_list.rpt``: LibreLane's own split of every failing endpoint
    into in-reg / reg-reg / reg-out / in-out, for setup and hold."""
    out: Dict[str, Dict[str, int]] = {"setup": {}, "hold": {}}
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _VIO_RE.match(line)
            if m:
                k = m.group(2) + "-" + m.group(3)
                out[m.group(1)][k] = out[m.group(1)].get(k, 0) + 1
    return out


def sta_history(run_dir: str | Path) -> List[Dict[str, Any]]:
    """Each timing analysis of the run in flow order (a resumed run's earlier
    ones come from its parent): ``[{id, dir, run, when, signoff, corners:
    {corner: {setup_ws, setup_tns, setup_vio, hold_ws, hold_tns, hold_vio,
    kinds}}}]``, values from the step's own ``state_out.json`` for the corners
    it timed, ``kinds`` from its violator lists."""
    import json as _json

    from . import history
    out: List[Dict[str, Any]] = []
    prev_id = None
    for s in history.lineage_steps(run_dir):
        sid = s["id"] or ""
        base = _base_id(sid)
        if base in _STA_STEPS and (s["path"] / "state_out.json").is_file():
            try:
                metrics = _json.loads((s["path"] / "state_out.json").read_text(encoding="utf-8")).get("metrics") or {}
            except Exception:
                metrics = {}
            corners: Dict[str, Any] = {}
            for c in _corners_timed(s["path"]):
                def g(k: str) -> Any:
                    return metrics.get(k + "__corner:" + c)
                vdir = s["path"] / c if (s["path"] / c).is_dir() else s["path"]
                corners[c] = {
                    "setup_ws": g("timing__setup__ws"), "setup_tns": g("timing__setup__tns"),
                    "setup_vio": g("timing__setup_vio__count"),
                    "hold_ws": g("timing__hold__ws"), "hold_tns": g("timing__hold__tns"),
                    "hold_vio": g("timing__hold_vio__count"),
                    "kinds": _violators(vdir / "violator_list.rpt"),
                }
            if base in ("OpenROAD.STAPrePNR", "OpenROAD.STAPostPNR"):
                when = _AFTER[base]
            else:
                pb = _base_id(prev_id)
                when = _AFTER.get(pb, "after " + (pb.split(".")[-1] or "the step before"))
            out.append({"id": sid, "dir": s["dir"], "run": s["run"], "path": str(s["path"]), "when": when,
                        "signoff": base == "OpenROAD.STAPostPNR", "ideal_clocks": base == "OpenROAD.STAPrePNR",
                        "corners": corners})
        # Name an STA by the step that changed the design before it, not by
        # the checks and bookkeeping in between.
        if base not in _STA_STEPS and not sid.startswith(("Checker.", "Odb.")):
            prev_id = sid
    return out


# ---------------------------------------------------------------- one path's budget
_ROW_RE = re.compile(r"^\s*((?:-?\d+(?:\.\d+)?(?:e-?\d+)?\s+)*)([\^v]\s+)?(.*?)\s*$")


def path_budget(path_text: str) -> Optional[Dict[str, Any]]:
    """Where the time of one setup path goes, from its ``report_checks`` rows
    (``-fields {slew cap input net fanout} -format full_clock_expanded``, as
    LibreLane writes them). Driver pins carry five numbers (fanout, cap, slew,
    delay, time) and their delay is the cell's; load pins carry three and
    their delay is the wire's. Returns ``{period, launch, capture, io_in,
    io_out, drive, clk_q, cells, wires, setup, uncertainty, crpr, levels,
    fanout: {pin, n, delay}, start, end, arrival, required, slack}`` in ns, or
    None when the text isn't a whole path."""
    lines = path_text.splitlines()
    start = next((ln for ln in lines[:6] if ln.startswith("Startpoint:")), None)
    end = next((ln for ln in lines[:6] if ln.startswith("Endpoint:")), "")
    if start is None:
        return None
    start_in = "input port" in start
    start_name = _after("Startpoint:", start)
    b: Dict[str, Any] = {
        "period": None, "launch": 0.0, "capture": 0.0, "io_in": 0.0, "io_out": 0.0, "drive": 0.0,
        "clk_q": 0.0, "cells": 0.0, "wires": 0.0, "setup": 0.0, "uncertainty": 0.0, "crpr": 0.0,
        "levels": 0, "fanout": None, "start": "in" if start_in else "reg",
        "end": "out" if "output port" in end else "reg", "arrival": None, "required": None, "slack": None}
    edges: List[float] = []
    required = False          # past "data arrival time"
    started = start_in        # past the start register's clock pin
    seen_driver = False

    def rel(nums: List[float]) -> float:
        return nums[-2] if len(nums) >= 2 else 0.0

    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(("Startpoint", "Endpoint", "Path ", "Fanout", "---")):
            continue
        m = _ROW_RE.match(ln)
        nums = [float(x) for x in m.group(1).split()] if m and m.group(1) else []
        desc = m.group(3) if m else s
        if desc.startswith("data arrival time"):
            if b["arrival"] is None and nums:   # the summary repeats it, negated
                b["arrival"] = nums[-1]
            required = True
        elif desc.startswith("data required time"):
            if b["required"] is None and nums:
                b["required"] = nums[-1]
        elif desc.startswith("slack"):
            b["slack"] = nums[-1] if nums else None
            break
        elif desc.startswith("clock ") and "edge)" in desc:
            if nums:
                edges.append(nums[-1])
        elif desc.startswith(("clock network delay", "clock source latency")):
            b["capture" if required else "launch"] += rel(nums)
        elif desc.startswith("input external delay"):
            b["io_in"] += rel(nums)
        elif desc.startswith("output external delay"):
            b["io_out"] -= rel(nums)
        elif desc.startswith("clock uncertainty"):
            b["uncertainty"] -= rel(nums)
        elif desc.startswith("clock reconvergence pessimism"):
            b["crpr"] += rel(nums)
        elif desc.startswith("library setup time"):
            b["setup"] -= rel(nums)
        elif m and m.group(2) and not desc.endswith("(net)"):
            driver = len(nums) >= 5
            delay = rel(nums)
            pin = desc.split(" (")[0]
            if required:
                b["capture"] += delay          # the capture clock's tree
            elif not started:
                b["launch"] += delay           # the launch clock's tree, up to the register
                if pin.startswith(start_name + "/"):
                    started = True
            elif driver:
                if desc.endswith("(in)"):
                    b["drive"] += delay        # the input port's driver
                elif not seen_driver and not start_in:
                    b["clk_q"] += delay        # the start register, clock to Q
                else:
                    b["cells"] += delay
                    b["levels"] += 1
                seen_driver = True
                n_out = int(nums[0])
                if b["fanout"] is None or n_out > b["fanout"]["n"]:
                    b["fanout"] = {"pin": pin, "n": n_out, "delay": round(delay, 4)}
            else:
                b["wires"] += delay
    if len(edges) >= 2:
        b["period"] = round(edges[1] - edges[0], 6)
    for k in ("launch", "capture", "io_in", "io_out", "drive", "clk_q", "cells", "wires", "setup", "uncertainty", "crpr"):
        b[k] = round(b[k], 6)
    return b if b["slack"] is not None else None


def worst_setup_path(run_dir: str | Path, step_dir: str | Path, corner: Optional[str]) -> Optional[Dict[str, Any]]:
    """The worst setup path one STA step reported in *corner*, with its budget."""
    d = Path(step_dir)
    f = d / corner / "max.rpt" if corner and (d / corner / "max.rpt").is_file() else d / "max.rpt"
    if not f.is_file():
        return None
    try:
        with f.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(3_000_000)
    except Exception:
        return None
    paths = [p for p in parse_report_checks(text) if p.get("slack") is not None and abs(p["slack"]) < _SENTINEL
             and (not corner or p.get("corner") in (None, corner))]
    if not paths:
        return None
    p = min(paths, key=lambda x: x["slack"])
    out = {k: p.get(k) for k in ("startpoint", "endpoint", "slack")}
    out["corner"] = p.get("corner") or corner
    out["budget"] = path_budget(p.get("path_text") or "")
    try:
        out["report"] = f.relative_to(Path(run_dir)).as_posix()
    except Exception:
        out["report"] = None
    return out


# ---------------------------------------------------------------- the path explorer
# Verify's timing explorer reads the same reports one corner at a time: an
# index of every path in a report (its byte span, start, end, group and slack),
# built by streaming the file once and cached by its mtime and size, and one
# path parsed in full (every pin, cell, net and number) when it is opened.
# ``report_checks -group_path_count 1000`` without ``-endpoint_path_count``
# (LibreLane's corner.tcl) gives one path per endpoint, at most 1000 per path
# group, so an index row is an endpoint.

_IDX_CACHE: "OrderedDict[str, Tuple[Tuple[int, int], List[Dict[str, Any]]]]" = OrderedDict()
_IDX_ROWS_MAX = 400_000      # rows kept across every cached report
_IDX_LOCK = threading.Lock()
# A path group LibreLane's report stops at (``-group_path_count 1000``).
GROUP_CAP = 1000

_NUM_TOKEN = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_EDGE_ROW = re.compile(r"^clock (\S+) \((rise|fall) edge\)$")
_PIN_ROW = re.compile(r"^(.+)/([^/\s]+) \(([^()]+)\)$")
_PORT_ROW = re.compile(r"^(.+) \((in|out|inout)\)$")


def _name_desc(rest: str) -> Tuple[str, str]:
    """``' x (input port clocked by clk)'`` -> ``('x', 'input port clocked by clk')``."""
    rest = rest.strip()
    i = rest.find(" (")
    if i < 0:
        return rest, ""
    return rest[:i], rest[i + 2:].rstrip(")").strip()


def path_kind(start_desc: str, end_desc: str, group: Optional[str]) -> str:
    """in-reg, reg-reg, reg-out, in-out, or reset (the asynchronous group:
    recovery and removal checks on a flip-flop's reset pin)."""
    e = end_desc or ""
    if group == "asynchronous" or "recovery check" in e or "removal check" in e:
        return "reset"
    s = "in" if "input port" in (start_desc or "") else "reg"
    return s + "-" + ("out" if "output port" in e else "reg")


def index_report(report: Path) -> List[Dict[str, Any]]:
    """Every path block in one ``report_checks`` file: ``[{i, off, len, start,
    start_desc, end, end_desc, group, type, slack, met, corner, kind}]``.
    Cached by the file's mtime and size; never mutate the list."""
    st = report.stat()
    key, stamp = str(report), (st.st_mtime_ns, st.st_size)
    with _IDX_LOCK:
        hit = _IDX_CACHE.get(key)
        if hit and hit[0] == stamp:
            _IDX_CACHE.move_to_end(key)
            return hit[1]
    rows: List[Dict[str, Any]] = []
    corner: Optional[str] = None
    cur: Optional[Dict[str, Any]] = None
    off = 0
    with report.open("rb") as f:
        for raw in f:
            ln = raw.decode("utf-8", "replace").rstrip("\r\n")
            if ln.startswith("Startpoint:"):
                name, desc = _name_desc(ln[len("Startpoint:"):])
                cur = {"off": off, "len": 0, "start": name, "start_desc": desc, "end": None, "end_desc": "",
                       "group": None, "type": None, "slack": None, "met": None, "corner": corner}
            elif cur is not None:
                if ln.startswith("Endpoint:"):
                    cur["end"], cur["end_desc"] = _name_desc(ln[len("Endpoint:"):])
                elif ln.startswith("Path Group:"):
                    cur["group"] = ln.split(":", 1)[1].strip()
                elif ln.startswith("Path Type:"):
                    cur["type"] = ln.split(":", 1)[1].strip()
                else:
                    m = _SLACK_RE.match(ln)
                    if m:
                        cur["slack"] = float(m.group(1))
                        cur["met"] = m.group(2) == "MET"
                        cur["len"] = off + len(raw) - cur["off"]
                        rows.append(cur)
                        cur = None
            else:
                m = _CORNER_RE.match(ln)
                if m:
                    corner = m.group(1)
            off += len(raw)
    for i, r in enumerate(rows):
        r["i"] = i
        r["kind"] = path_kind(r["start_desc"], r["end_desc"], r["group"])
    with _IDX_LOCK:
        _IDX_CACHE[key] = (stamp, rows)
        _IDX_CACHE.move_to_end(key)
        total = sum(len(v[1]) for v in _IDX_CACHE.values())
        while len(_IDX_CACHE) > 1 and total > _IDX_ROWS_MAX:
            _k, (_s, old) = _IDX_CACHE.popitem(last=False)
            total -= len(old)
    return rows


def _real(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("slack") is not None and abs(r["slack"]) < _SENTINEL]


def read_block(report: Path, row: Dict[str, Any]) -> Tuple[str, int]:
    """The text of one indexed path, and the line it starts on (1-based)."""
    with report.open("rb") as f:
        head = f.read(row["off"])
        body = f.read(row["len"])
    return body.decode("utf-8", "replace"), head.count(b"\n") + 1


def _row_numbers(head: str, ends: Dict[str, int]) -> Dict[str, float]:
    """The numbers in a row's left part, by the header column they end under
    (OpenSTA right-aligns each number under its column name)."""
    out: Dict[str, float] = {}
    for m in _NUM_TOKEN.finditer(head):
        col = min(ends, key=lambda k: abs(ends[k] - m.end()))
        try:
            out[col] = float(m.group(0))
        except ValueError:
            pass
    return out


_TERM_WORDS = {
    "clock uncertainty": "uncertainty",
    "clock reconvergence pessimism": "pessimism removed",
    "library setup time": "setup time",
    "library hold time": "hold time",
    "library recovery time": "recovery time",
    "library removal time": "removal time",
    "output external delay": "output delay",
    "input external delay": "input delay",
}


def _short_cell(cell: Optional[str]) -> Optional[str]:
    """``sky130_fd_sc_hd__dfrtp_2`` -> ``dfrtp_2`` (the library prefix is noise on screen)."""
    if not cell:
        return cell
    return cell.split("__", 1)[1] if "__" in cell else cell


def parse_path(text: str) -> Optional[Dict[str, Any]]:
    """One ``report_checks -format full_clock_expanded -fields {slew cap input
    net fanout}`` path, structured: the launch clock, the data path and the
    capture clock as rows (pin, cell, edge, fanout, cap pF, slew, incremental
    delay, arrival), the terms the required time is made of, the sums written
    out, where the time goes, and the instance, net and pin names a layout
    highlight needs. None when *text* is not one whole path."""
    lines = text.splitlines()
    hdr: Dict[str, Any] = {"start": None, "start_desc": "", "end": None, "end_desc": "", "group": None, "type": None}
    ends: Dict[str, int] = {}
    desc_col = None
    section = "head"
    rows: List[Dict[str, Any]] = []
    arrival = required = slack = None
    met = None
    pending_net: Optional[str] = None
    last_out: Optional[Dict[str, Any]] = None
    for ln in lines:
        if section == "head":
            if ln.startswith("Startpoint:"):
                hdr["start"], hdr["start_desc"] = _name_desc(ln[len("Startpoint:"):])
            elif ln.startswith("Endpoint:"):
                hdr["end"], hdr["end_desc"] = _name_desc(ln[len("Endpoint:"):])
            elif ln.startswith("Path Group:"):
                hdr["group"] = ln.split(":", 1)[1].strip()
            elif ln.startswith("Path Type:"):
                hdr["type"] = ln.split(":", 1)[1].strip()
            elif ln.lstrip().startswith("Fanout") and "Description" in ln:
                for w in ("Fanout", "Cap", "Slew", "Delay", "Time"):
                    i = ln.find(w)
                    if i >= 0:
                        ends[w] = i + len(w)
                desc_col = ln.find("Description")
                section = "arr"
            continue
        if not ln.strip() or ln.startswith("---"):
            continue
        ms = _SLACK_RE.match(ln)
        if ms:
            slack = float(ms.group(1))
            met = ms.group(2) == "MET"
            break
        if section == "sum":
            continue
        if desc_col and len(ln) > desc_col:
            head_part, desc = ln[:desc_col], ln[desc_col:].strip()
        else:
            head_part, desc = ln, ""
        edge = None
        hp = head_part.rstrip()
        if hp.endswith((" ^", " v")):
            edge, hp = ("r" if hp[-1] == "^" else "f"), hp[:-2]
        if re.search(r"[^\d\s.eE+-]", hp):
            # Not a table row ("(Path is unconstrained)" and the like).
            continue
        nums = _row_numbers(hp, ends) if ends else {}
        if desc == "data arrival time":
            if arrival is None:
                arrival = nums.get("Time")
            section = "req"
            pending_net, last_out = None, None
            continue
        if desc == "data required time":
            if required is None:
                required = nums.get("Time")
            section = "sum"
            continue
        row: Dict[str, Any] = {"sec": section, "edge": edge, "fanout": nums.get("Fanout"), "cap": nums.get("Cap"),
                               "slew": nums.get("Slew"), "delay": nums.get("Delay"), "time": nums.get("Time")}
        if desc.endswith(" (net)"):
            name = desc[:-6]
            if last_out is not None and not last_out.get("net"):
                last_out["net"] = name
            pending_net = name
            continue
        m = _EDGE_ROW.match(desc)
        if m:
            row.update(kind="edge", clock=m.group(1), dir=m.group(2), label=desc)
        elif desc.startswith(("clock network delay", "clock source latency")):
            row.update(kind="latency", label=desc)
        elif desc in _TERM_WORDS or desc.startswith(("library ", "clock uncertainty", "clock reconvergence",
                                                     "time borrowed", "time given", "max_delay", "min_delay")):
            row.update(kind="io" if desc.endswith("external delay") else "term", label=desc,
                       word=_TERM_WORDS.get(desc, desc))
        else:
            mp = _PORT_ROW.match(desc)
            mi = _PIN_ROW.match(desc) if not mp else None
            if mp:
                row.update(kind="port", port=mp.group(1), dir=mp.group(2))
            elif mi:
                row.update(kind="pin", inst=mi.group(1), pin=mi.group(1) + "/" + mi.group(2), pin_name=mi.group(2),
                           lib=mi.group(3), cell=_short_cell(mi.group(3)))
            else:
                row.update(kind="term", label=desc, word=desc)
            if row["kind"] in ("port", "pin"):
                # A driver (output pin, input port) carries fanout and cap; a
                # load pin carries only slew/delay/time and its delay is the wire's.
                row["driver"] = row["fanout"] is not None
                if row["driver"]:
                    last_out = row
                else:
                    row["net_in"] = pending_net
        rows.append(row)
    if slack is None or not hdr["start"]:
        return None
    return _shape_path(hdr, rows, arrival, required, slack, met)


def _shape_path(hdr: Dict[str, Any], rows: List[Dict[str, Any]], arrival: Optional[float],
                required: Optional[float], slack: float, met: Optional[bool]) -> Dict[str, Any]:
    kind = path_kind(hdr["start_desc"], hdr["end_desc"], hdr["group"])
    ptype = (hdr["type"] or "").lower()
    e = hdr["end_desc"] or ""
    check = ("recovery" if "recovery check" in e else "removal" if "removal check" in e
             else "hold" if ptype == "min" else "setup")
    start_in = "input port" in (hdr["start_desc"] or "")
    end_out = "output port" in e
    arr = [r for r in rows if r["sec"] == "arr"]
    req = [r for r in rows if r["sec"] == "req"]
    # The launch clock: from the clock edge up to the start register's clock
    # pin (the first pin of the start instance). A path from an input port
    # has no clock tree: its launch is the edge and the latency rows.
    n_launch = 0
    hit = None if start_in else next((i for i, r in enumerate(arr) if r["kind"] == "pin" and r.get("inst") == hdr["start"]), None)
    if hit is None:
        while n_launch < len(arr) and arr[n_launch]["kind"] in ("edge", "latency"):
            n_launch += 1
    else:
        n_launch = hit + 1
    launch, data = arr[:n_launch], arr[n_launch:]
    # The capture clock: the rows before the first requirement term.
    n_cap = next((i for i, r in enumerate(req) if r["kind"] in ("term", "io")), len(req))
    capture, terms = req[:n_cap], req[n_cap:]
    for part, name in ((launch, "launch"), (data, "data"), (capture, "capture"), (terms, "term")):
        for r in part:
            r["sec"] = name

    def clock_of(part: List[Dict[str, Any]]) -> Dict[str, Any]:
        edge = next((r for r in part if r["kind"] == "edge"), None)
        t0 = edge["time"] if edge and edge.get("time") is not None else 0.0
        t1 = next((r["time"] for r in reversed(part) if r.get("time") is not None), t0)
        cells = [r for r in part if r["kind"] == "pin" and r.get("driver")]
        return {"clock": edge.get("clock") if edge else None, "dir": edge.get("dir") if edge else None,
                "edge": round(t0, 6), "delay": round(t1 - t0, 6), "cells": len(cells),
                "to": next((r["pin"] for r in reversed(part) if r["kind"] == "pin"), None),
                "ideal": any("(ideal)" in (r.get("label") or "") for r in part)}

    lc, cc = clock_of(launch), clock_of(capture)
    # Where the data path's time goes (verify-diag.js PARTS uses these keys).
    parts = {"launch": max(0.0, lc["delay"]), "io_in": 0.0, "drive": 0.0, "clk_q": 0.0, "cells": 0.0, "wires": 0.0}
    levels = 0
    seen_driver = False
    worst_fan = None
    for r in data:
        d = r.get("delay") or 0.0
        if r["kind"] == "io":
            parts["io_in"] += d
        elif r["kind"] == "port" and r.get("driver"):
            parts["drive"] += d
            seen_driver = True
        elif r["kind"] == "pin" and r.get("driver"):
            if not seen_driver and not start_in and r.get("inst") == hdr["start"]:
                parts["clk_q"] += d
            else:
                parts["cells"] += d
                levels += 1
            seen_driver = True
            if r.get("fanout") is not None and (worst_fan is None or r["fanout"] > worst_fan["n"]):
                worst_fan = {"pin": r["pin"], "n": int(r["fanout"]), "delay": round(d, 4)}
        elif r["kind"] in ("pin", "port"):
            parts["wires"] += d
    parts = {k: round(v, 6) for k, v in parts.items()}
    end_cell = next((r.get("cell") for r in reversed(data) if r["kind"] == "pin"), None)
    # The sums, written out with every term the report has.
    arrival_terms = [{"k": "edge", "word": "clock edge", "v": lc["edge"]}]
    if lc["delay"] or not start_in:
        arrival_terms.append({"k": "launch", "word": "launch clock", "v": lc["delay"]})
    if parts["io_in"]:
        arrival_terms.append({"k": "io_in", "word": "input delay", "v": parts["io_in"]})
    if parts["clk_q"]:
        start_cell = next((r.get("cell") for r in data if r["kind"] == "pin" and r.get("inst") == hdr["start"]), None)
        arrival_terms.append({"k": "clk_q", "word": "clock to Q" + (f" ({start_cell})" if start_cell else ""),
                              "v": parts["clk_q"]})
    through =(arrival if arrival is not None else 0.0) - sum(t["v"] for t in arrival_terms)
    arrival_terms.append({"k": "data", "word": f"through {levels} cell{'s' if levels != 1 else ''}",
                          "v": round(through, 6)})
    required_terms = [{"k": "edge", "word": "clock edge", "v": cc["edge"]},
                      {"k": "capture", "word": "capture clock", "v": cc["delay"]}]
    for r in terms:
        w = r.get("word") or r.get("label")
        if w in ("setup time", "hold time", "recovery time", "removal time") and end_cell:
            w = f"{w} ({end_cell})"
        required_terms.append({"k": "term", "word": w, "v": r.get("delay") or 0.0})
    if required is not None:
        rest = required - sum(t["v"] for t in required_terms)
        if abs(rest) > 5e-6:
            required_terms.append({"k": "other", "word": "other", "v": round(rest, 6)})

    def uniq(xs: List[Optional[str]]) -> List[str]:
        out: List[str] = []
        for x in xs:
            if x and x not in out:
                out.append(x)
        return out

    def names(part: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        return {"insts": uniq([r.get("inst") for r in part if r["kind"] == "pin"]),
                "nets": uniq([r.get("net") for r in part] + [r.get("net_in") for r in part])}

    dn = names(data)
    if not start_in and hdr["start"] and hdr["start"] not in dn["insts"]:
        dn["insts"].insert(0, hdr["start"])
    highlight = {
        "insts": dn["insts"], "nets": dn["nets"],
        "pins": uniq([r.get("pin") for r in data if r["kind"] == "pin"]),
        "ports": uniq([r.get("port") for r in data if r["kind"] == "port"]),
        "clock": {"launch": names(launch), "capture": names(capture)},
    }
    return {
        "start": hdr["start"], "start_desc": hdr["start_desc"], "end": hdr["end"], "end_desc": hdr["end_desc"],
        "group": hdr["group"], "type": hdr["type"], "check": check, "kind": kind,
        "start_in": start_in, "end_out": end_out, "end_cell": end_cell,
        "arrival": arrival, "required": required, "slack": slack, "met": met,
        "launch": {**lc, "rows": launch}, "data": {"rows": data, "levels": levels},
        "capture": {**cc, "rows": capture}, "terms": terms,
        "arrival_terms": arrival_terms, "required_terms": required_terms,
        "parts": parts, "levels": levels, "fanout": worst_fan,
        "period": round(cc["edge"] - lc["edge"], 6) if check == "setup" else None,
        "highlight": highlight,
    }


def _nice_width(span: float, max_bins: int = 24) -> float:
    for w in (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100):
        if span / w <= max_bins:
            return w
    return span / max_bins


def histogram0(slacks: List[Optional[float]], max_bins: int = 24) -> Dict[str, Any]:
    """Endpoint slacks in bins whose edges sit on 0 (a bin never straddles
    pass and fail), with a round width: ``{edges, counts, width}``."""
    import math as _m
    vals = [s for s in slacks if s is not None and abs(s) < _SENTINEL]
    if not vals:
        return {"edges": [], "counts": [], "width": None}
    lo, hi = min(vals), max(vals)
    w = _nice_width(max(hi - lo, 1e-9), max_bins)
    a = _m.floor(lo / w + 1e-9) * w
    n = max(1, int(_m.floor((hi - a) / w + 1e-9)) + 1)
    edges = [round(a + k * w, 6) for k in range(n + 1)]
    counts = [0] * n
    for v in vals:
        k = int(_m.floor((v - a) / w + 1e-9))
        counts[min(max(k, 0), n - 1)] += 1
    return {"edges": edges, "counts": counts, "width": w}


def _rel_in(run_dir: Path, p: Path) -> Dict[str, Optional[str]]:
    """Where a report lives: ``{tag, rel}``, the run holding it (a resumed
    run's STA can sit in its parent) and the path inside that run."""
    try:
        return {"tag": run_dir.name, "rel": p.relative_to(run_dir).as_posix()}
    except ValueError:
        pass
    for parent in p.parents:
        if parent.parent.name == "runs":
            return {"tag": parent.name, "rel": p.relative_to(parent).as_posix()}
    return {"tag": None, "rel": p.name}


def corner_files(run_dir: str | Path, kind: str = "setup") -> Tuple[List[Tuple[str, Path]], Optional[Path]]:
    """``([(corner, report), …], step_dir)`` for *kind* from the most final STA step."""
    run_dir = Path(run_dir)
    reports, step = _pick_reports(run_dir, kind if kind in _REPORT_FOR else "setup")
    out: List[Tuple[str, Path]] = []
    for f, hint in reports:
        c = hint
        if not c:
            try:
                c = next((r["corner"] for r in index_report(f) if r.get("corner")), None)
            except OSError:
                c = None
        out.append((c or "default", f))
    return out, step


def _endpoint_key(r: Dict[str, Any]) -> Tuple[Any, ...]:
    return (r.get("end"), r.get("group"), r.get("type"))


def _worst_corner(run_dir: Path, kind: str, corners: List[str]) -> Optional[str]:
    """The corner with the worst slack for *kind*, read from the metrics (one
    small file, not every report)."""
    try:
        from . import history
        m = history._load_metrics(run_dir) or {}
    except Exception:
        m = {}
    pre = f"timing__{kind}__ws__corner:"
    best = None
    for c in corners:
        v = m.get(pre + c)
        if isinstance(v, (int, float)) and abs(v) < _SENTINEL and (best is None or v < best[0]):
            best = (v, c)
    return best[1] if best else None


def _public_row(r: Dict[str, Any]) -> Dict[str, Any]:
    return {k: r.get(k) for k in ("i", "start", "start_desc", "end", "end_desc", "group", "type", "slack", "met", "kind")}


def path_list(run_dir: str | Path, *, kind: str = "setup", corner: Optional[str] = None, q: str = "",
              only: str = "", base_dir: Optional[str | Path] = None, limit: int = GROUP_CAP * 4) -> Dict[str, Any]:
    """One corner's endpoints for the path explorer, worst first, as index
    rows (no path text): ``{ok, kind, corner, corners, total, shown,
    violating, worst, rows, histogram, groups, kinds, truncated, source,
    step, base_tag}``. *corner* None: the corner with the worst slack. *q*
    matches the start, the end, or any pin or net on the path. *only* keeps
    one path kind (in-reg, reg-reg, reg-out, in-out, reset). *base_dir*: each
    row gets the same endpoint's slack in that run (``base``)."""
    run_dir = Path(run_dir)
    kind = kind if kind in _REPORT_FOR else "setup"
    files, step = corner_files(run_dir, kind)
    if not files:
        return {"ok": False, "kind": kind,
                "error": f"This run has no {kind} timing report: static timing analysis didn't run, "
                         "or the run stopped before it."}
    names = [c for c, _f in files]
    if corner not in names:
        corner = _worst_corner(run_dir, kind, names) or names[0]
    report = dict(files)[corner]
    try:
        allrows = _real(index_report(report))
    except OSError as ex:
        return {"ok": False, "kind": kind, "corner": corner, "corners": names,
                "error": f"Couldn't read {report.name} ({ex}).", "source": _rel_in(run_dir, report)}
    rows = allrows
    if only:
        rows = [r for r in rows if r["kind"] == only]
    qq = (q or "").strip().lower()
    if qq:
        hit = [r for r in rows if qq in (r["start"] or "").lower() or qq in (r["end"] or "").lower()]
        seen = {id(r) for r in hit}
        rest = [r for r in rows if id(r) not in seen]
        if rest:
            data = report.read_bytes()
            hit += [r for r in rest if qq in data[r["off"]:r["off"] + r["len"]].decode("utf-8", "replace").lower()]
        rows = hit
    rows = sorted(rows, key=lambda r: r["slack"])
    groups: Dict[str, int] = {}
    kinds: Dict[str, int] = {}
    for r in allrows:
        groups[r["group"] or ""] = groups.get(r["group"] or "", 0) + 1
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    out_rows = [_public_row(r) for r in rows[:max(1, limit)]]
    base_tag = None
    if base_dir:
        bmap = dict(corner_files(base_dir, kind)[0])
        if corner in bmap:
            try:
                bslack = {_endpoint_key(r): r["slack"] for r in _real(index_report(bmap[corner]))}
                base_tag = Path(base_dir).name
                for r in out_rows:
                    r["base"] = bslack.get(_endpoint_key(r))
            except OSError:
                pass
    return {
        "ok": True, "kind": kind, "corner": corner, "corners": names, "unit": TIME_UNIT,
        "total": len(allrows), "shown": len(rows),
        "violating": sum(1 for r in allrows if r["slack"] < 0),
        "worst": min(r["slack"] for r in allrows) if allrows else None,
        "rows": out_rows, "histogram": histogram0([r["slack"] for r in allrows]),
        "groups": groups, "kinds": kinds, "truncated": any(v >= GROUP_CAP for v in groups.values()),
        "source": _rel_in(run_dir, report), "step": step.name if step is not None else "",
        "base_tag": base_tag,
    }


def path_detail(run_dir: str | Path, *, kind: str = "setup", corner: Optional[str] = None, i: int = 0,
                base_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Path *i* of one corner's report, parsed in full (``parse_path``), with
    the same endpoint's slack in every other corner and in *base_dir*."""
    run_dir = Path(run_dir)
    kind = kind if kind in _REPORT_FOR else "setup"
    files, _step = corner_files(run_dir, kind)
    fmap = dict(files)
    if corner not in fmap:
        return {"ok": False, "error": f"This run has no {kind} report for corner {corner}."}
    report = fmap[corner]
    rows = index_report(report)
    if not 0 <= i < len(rows):
        return {"ok": False, "error": "That path is no longer in the report (the run may have been re-run). Refresh the page."}
    row = rows[i]
    text, line = read_block(report, row)
    p = parse_path(text)
    if p is None:
        return {"ok": False, "error": f"Couldn't read path {i + 1} of {report.name}.",
                "source": _rel_in(run_dir, report), "line": line}
    key = _endpoint_key(row)
    others = []
    for c, f in files:
        try:
            hit = next((r for r in _real(index_report(f)) if _endpoint_key(r) == key), None)
        except OSError:
            hit = None
        others.append({"corner": c, "slack": hit["slack"] if hit else None, "i": hit["i"] if hit else None})
    base = None
    if base_dir:
        bmap = dict(corner_files(base_dir, kind)[0])
        if corner in bmap:
            try:
                hit = next((r for r in _real(index_report(bmap[corner])) if _endpoint_key(r) == key), None)
                base = {"tag": Path(base_dir).name, "slack": hit["slack"] if hit else None}
            except OSError:
                base = None
    p.update(ok=True, i=i, corner=corner, analysis=kind, source=_rel_in(run_dir, report), line=line,
             others=others, base=base)
    return p
