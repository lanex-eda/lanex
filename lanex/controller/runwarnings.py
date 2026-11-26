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
"""A run's warnings by tool and code, with waivers and "new since baseline".

Where the warnings come from (LibreLane 3.0.4):

* ``warning.log`` at the run root: every LibreLane-level warning, including
  every OpenROAD and OpenSTA ``[WARNING CODE]`` alert, re-logged as
  ``[CODE] message`` (``steps/openroad_alerts.py``). A record can span lines:
  a line that starts with ``* `` or with whitespace continues the one before
  it ("Setup violations found in the following corners:" then ``* corner``).
* ``flow.log`` says which step each record came from: records appear after the
  ``Running '<Step.Id>' at '<dir>'…`` line of their step.
* Verilator's own ``%Warning-CODE: file:line:col: message`` lines in the lint
  step's log (LibreLane only adds "N Lint warnings found." to warning.log).
* OpenSTA's ``check_setup`` lines in the sign-off STA log ("There are N
  unclocked register/latch pins."), which LibreLane never relays.

The ``flow__warnings__count`` metrics are not used: each step overwrites them,
so they are not totals.

Waivers live in ``lanex-waivers.yaml`` at the project root: review material
that belongs next to ``config.yaml`` in version control. A waiver never hides
anything: a waived group stays on screen, marked waived with its reason, and
a waiver that matches nothing in a run is listed as unused.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WAIVER_FILE = "lanex-waivers.yaml"
REASONS = {
    "harmless": "Harmless for this design",
    "expected-pdk": "Expected with this PDK",
    "accepted-risk": "Accepted risk",
    "fix-later": "Will fix later",
}
_MAX_LOG = 8_000_000          # bytes read from one log
_MAX_VARIANTS = 20
_MAX_SAMPLES = 3

_CODE_RX = re.compile(r"^\[(?:WARNING |ERROR )?([A-Z]{2,5}-\d{4})\]\s*(.*)$")
_RUNNING_RX = re.compile(r"^Running '([^']+)' at '([^']+)'")
_VL_RX = re.compile(r"^%Warning-([A-Z0-9_]+):\s*(?:([^:\s]+):(\d+):(?:\d+:)?)?\s*(.*)$")
_UNCLK_RX = re.compile(r"There (?:are|is) (\d+) unclocked register/latch pins?")
_UNCONS_RX = re.compile(r"There (?:are|is) (\d+) unconstrained endpoints?")

# The OpenROAD tool each code prefix belongs to (the prefix is the tool's module).
_TOOLS = {
    "STA": ("OpenSTA", "timing"), "DRT": ("OpenROAD", "detailed router"), "GRT": ("OpenROAD", "global router"),
    "RSZ": ("OpenROAD", "resizer"), "GPL": ("OpenROAD", "global placer"), "DPL": ("OpenROAD", "detailed placer"),
    "CTS": ("OpenROAD", "clock tree"), "PDN": ("OpenROAD", "power grid"), "PPL": ("OpenROAD", "pin placer"),
    "IFP": ("OpenROAD", "floorplan"), "ORD": ("OpenROAD", ""), "ODB": ("OpenROAD", "database"),
    "ANT": ("OpenROAD", "antenna checker"), "MPL": ("OpenROAD", "macro placer"), "TAP": ("OpenROAD", "tap cells"),
    "EST": ("OpenROAD", "parasitics"), "RCX": ("OpenROAD", "extraction"), "PSM": ("OpenROAD", "IR drop"),
    "FIN": ("OpenROAD", "fill"), "GUI": ("OpenROAD", "GUI"), "UKN": ("OpenROAD", ""), "PAR": ("OpenROAD", "partitioning"),
}

# LanEx's reading of the warnings that actually occur (counted over 745 real
# runs, mostly sky130). worry: look | info | harmless. These are LanEx's
# words, not the tools': the tools themselves don't rank warnings.
KB: Dict[str, Dict[str, Any]] = {
    "STA-1140": {"worry": "harmless", "title": "A cell library was read twice",
                 "plain": "OpenSTA was asked to read a cell library it already had loaded. LibreLane loads each corner's library in several steps.",
                 "matters": "Never changes timing results."},
    "RSZ-0020": {"worry": "info", "title": "Nets with no driver or no load",
                 "plain": "The resizer found nets with no driver or no load. On sky130 these are almost always VPWR and VGND, the power nets, before they are connected.",
                 "matters": "Only if the step log lists signal nets after this line: then part of your logic is unconnected."},
    "DRT-0349": {"worry": "harmless", "title": "A via rule the router can't read",
                 "plain": "The detailed router can't read one kind of via-enclosure rule in the PDK's tech LEF and skips it for that layer.",
                 "matters": "Magic and KLayout check the real rule afterwards. If DRC reports via-enclosure errors, this is why the router made them."},
    "DRT-0204": {"worry": "harmless", "title": "A cut layer with no default via",
                 "plain": "A cut layer in the tech LEF has no default via, so the router won't use it.",
                 "matters": "Never on the standard flows."},
    "RSZ-0062": {"worry": "look", "title": "Timing repair gave up",
                 "plain": "Timing repair ran out of things to try and some setup violations are left.",
                 "matters": "See Timing: a slower clock, fewer logic levels, or a less dense placement."},
    "RSZ-0064": {"worry": "look", "title": "Hold repair couldn't fix every path",
                 "plain": "Hold repair couldn't fix every hold path within its margin.",
                 "matters": "See Timing, Hold: raise the hold margins or check the clock tree."},
    "GPL-0302": {"worry": "info", "title": "Target density below what the design can reach",
                 "plain": "The placer's target density is lower than the design's own utilisation, so it used a higher one.",
                 "matters": "Harmless. Set PL_TARGET_DENSITY_PCT higher to silence it."},
    "GRT-0281": {"worry": "look", "title": "One net drives a very large number of pins",
                 "plain": "One net drives a very large number of pins. On the clock net it usually means no clock tree was built for it.",
                 "matters": "If the net is the clock: check CLOCK_PORT and the clock-tree step."},
    "GRT-0273": {"worry": "info", "title": "The router dropped a wider clock rule",
                 "plain": "To fit the routing, the router dropped the wider, more spaced rule it normally uses for this clock net.",
                 "matters": "Clock wires are thinner than planned: worth a look if hold or skew got worse."},
    "GRT-0243": {"worry": "look", "title": "Antenna repair couldn't fix a net",
                 "plain": "Antenna repair couldn't fix a net that already has diodes.",
                 "matters": "See Antenna in Verify."},
    "CTS-0083": {"worry": "look", "title": "No clock net for the clock tree",
                 "plain": "Clock-tree synthesis found no clock net, so no clock tree was built.",
                 "matters": "Almost always CLOCK_PORT doesn't name the clock input. Timing is then not checked."},
    "CTS-0082": {"worry": "look", "title": "No clock net for the clock tree",
                 "plain": "Clock-tree synthesis found no clock net, so no clock tree was built.",
                 "matters": "Almost always CLOCK_PORT doesn't name the clock input. Timing is then not checked."},
    "CTS-0041": {"worry": "harmless", "title": "The clock drives one flip-flop",
                 "plain": "The clock drives only one flip-flop, so no tree is needed.",
                 "matters": "Never."},
    "STA-0366": {"worry": "look", "title": "The clock port wasn't found",
                 "plain": "The SDC names a port the netlist doesn't have, so its clock was never created.",
                 "matters": "Set CLOCK_PORT to the clock input of the top module. Until then timing is not checked."},
    "STA-0363": {"worry": "look", "title": "A pin in the SDC wasn't found",
                 "plain": "The SDC names a pin the netlist doesn't have.",
                 "matters": "Check CLOCK_PORT and any pins your own SDC names."},
    "STA-0419": {"worry": "look", "title": "The clock became a virtual clock",
                 "plain": "The SDC's clock became a virtual clock (its port wasn't found), so its transition setting was ignored.",
                 "matters": "Same cause as STA-0366: set CLOCK_PORT."},
    "STA-0450": {"worry": "look", "title": "A virtual clock can't be propagated",
                 "plain": "The clock has no port, so OpenSTA can't propagate it through a clock tree.",
                 "matters": "Same cause as STA-0366: set CLOCK_PORT."},
    "PDN-0110": {"worry": "info", "title": "No power grid straps fit here",
                 "plain": "The power-grid generator couldn't fit some straps in the area it was given.",
                 "matters": "Check Verify's power-grid check; a very small die can leave cells between straps."},
    # LanEx's own codes for warnings LibreLane prints without one.
    "LL:lint": {"worry": "info", "title": "Verilator lint warnings",
                "plain": "Verilator found lint warnings in the RTL; each one is listed under Verilator.",
                "matters": "ERROR_ON_LINTER_WARNINGS is off by default, so they don't stop the flow. Width mismatches and implicit nets are common real bugs."},
    "LL:timing-corners": {"worry": "look", "title": "Violations in corners that only warn",
                          "plain": "LibreLane only warns here: these corners are outside the list that fails the run.",
                          "matters": "The chip would still miss its clock in those corners. See Timing."},
    "LL:no-macro-lib": {"worry": "look", "title": "A macro has no timing library",
                        "plain": "A macro has no timing library for a corner, so it was a black box there: paths through it were not timed.",
                        "matters": "Add the macro's .lib for that corner."},
    "LL:wire-threshold": {"worry": "harmless", "title": "Long-wire check skipped",
                          "plain": "WIRE_LENGTH_THRESHOLD isn't set, so LibreLane skipped its long-wire check.",
                          "matters": "Set WIRE_LENGTH_THRESHOLD (µm) to have LibreLane check long wires."},
    "LL:vsrc": {"worry": "harmless", "title": "IR drop guessed where power enters",
                "plain": "VSRC_LOC_FILES isn't set, so the IR-drop analysis guessed where power enters the block.",
                "matters": "Matters only for a full chip with real power pads."},
    "LL:fallback-sdc": {"worry": "info", "title": "LibreLane used its own SDC",
                        "plain": "No SDC file was given, so LibreLane used its own: the clock from CLOCK_PERIOD, part of it reserved for inputs and outputs (IO_DELAY_CONSTRAINT), and a load on every output.",
                        "matters": "Fine for learning. A real design should have its own SDC."},
    "LL:pin-distance": {"worry": "harmless", "title": "Pins spaced wider than asked",
                        "plain": "The pin placer spaced the pins wider than asked so they don't overlap.",
                        "matters": "Never."},
    "LL:no-klayout-drc": {"worry": "info", "title": "No KLayout DRC deck",
                          "plain": "This PDK has no KLayout DRC deck in LibreLane, so only Magic checked the design rules.",
                          "matters": "Expected on gf180mcu."},
    "LL:macro-antenna": {"worry": "info", "title": "Macro pins without antenna data",
                         "plain": "A macro's LEF has no antenna data for some pins.",
                         "matters": "Antenna checks on those pins are incomplete."},
    "LL:no-cts": {"worry": "look", "title": "No clock tree was built",
                  "plain": "No CLOCK_NET or CLOCK_PORT was given, so clock-tree synthesis was skipped.",
                  "matters": "Set CLOCK_PORT to the clock input. Without a clock, timing is not checked."},
    "STA:unclocked": {"worry": "look", "title": "Flip-flops with no clock",
                      "plain": "Some flip-flop clock pins have no clock, so the paths through them were not timed.",
                      "matters": "Set CLOCK_PORT to the clock input (or fix the SDC). Verify shows timing as not checked when no path was timed."},
    "STA:unconstrained": {"worry": "info", "title": "Endpoints with no timing check",
                          "plain": "Some endpoints have no timing constraint, so OpenSTA didn't check them.",
                          "matters": "Usually outputs or resets the SDC doesn't constrain, or a missing clock."},
}

# LibreLane's own messages that carry no code: (code, pattern).
_LL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("LL:lint", re.compile(r"^\d+ Lint warnings? found")),
    ("LL:timing-corners", re.compile(r"^(Setup|Hold|Max slew|Max cap(acitance)?) violations found in the following corners")),
    ("LL:no-macro-lib", re.compile(r"(No libs found|no timing librar)", re.I)),
    ("LL:wire-threshold", re.compile(r"^Threshold for Threshold-surpassing long wires is not set")),
    ("LL:vsrc", re.compile(r"^'VSRC_LOC_FILES' was not given a value")),
    ("LL:fallback-sdc", re.compile(r"(PNR_SDC_FILE|SIGNOFF_SDC_FILE).*(not|fallback)|fallback SDC", re.I)),
    ("LL:pin-distance", re.compile(r"^Overriding minimum distance .* for pins")),
    ("LL:no-klayout-drc", re.compile(r"KLAYOUT_DRC_RUNSET")),
    ("LL:macro-antenna", re.compile(r"antenna (info|information|data)", re.I)),
    ("LL:no-cts", re.compile(r"^No CLOCK_NET \(or CLOCK_PORT\) specified")),
]

_NORM_SUBS = [
    (re.compile(r"(/[\w.+-]+)+/?"), "{path}"),
    (re.compile(r"'[^']*'"), "'{…}'"),
    (re.compile(r"\b(min|nom|max)_[A-Za-z0-9]+_[A-Za-z0-9]+_[A-Za-z0-9]+\b"), "{corner}"),
    (re.compile(r"-?\b\d+(\.\d+)?(e-?\d+)?\b"), "{n}"),
]


def pattern_of(message: str) -> str:
    """The message with paths, quoted names, corner names and numbers
    replaced by tokens: messages that differ only in those are one variant."""
    s = message
    for rx, sub in _NORM_SUBS:
        s = rx.sub(sub, s)
    return s.strip()


def _read_lines(p: Path) -> List[str]:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_LOG).splitlines()
    except OSError:
        return []


def read_records(run_dir: str | Path) -> List[Dict[str, Any]]:
    """``warning.log`` as records (a continuation line joins the record before
    it): ``[{text, lines, step_id, step_dir}]`` in order, each with the step
    that wrote it when flow.log says."""
    run_dir = Path(run_dir)
    recs: List[Dict[str, Any]] = []
    for ln in _read_lines(run_dir / "warning.log"):
        if not ln.strip():
            continue
        if recs and (ln.startswith("* ") or ln[:1].isspace()):
            recs[-1]["lines"].append(ln.strip())
            continue
        recs.append({"text": ln.strip(), "lines": [], "step_id": None, "step_dir": None})
    if not recs:
        return recs
    # Attribute each record to its step: they appear in flow.log in the same
    # order, after their step's "Running" line.
    cur: Tuple[Optional[str], Optional[str]] = (None, None)
    k = 0
    for ln in _read_lines(run_dir / "flow.log"):
        if k >= len(recs):
            break
        m = _RUNNING_RX.match(ln)
        if m:
            cur = (m.group(1), m.group(2).rstrip("/").split("/")[-1])
            continue
        if ln.strip() == recs[k]["text"]:
            recs[k]["step_id"], recs[k]["step_dir"] = cur
            k += 1
    return recs


def _classify(text: str) -> Tuple[str, str, str]:
    """``(tool, code, message)`` of one warning.log record."""
    m = _CODE_RX.match(text)
    if m:
        code, msg = m.group(1), m.group(2)
        tool = _TOOLS.get(code.split("-")[0], ("OpenROAD", ""))[0]
        return tool, code, msg
    for code, rx in _LL_PATTERNS:
        if rx.search(text):
            return "LibreLane", code, text
    return "LibreLane", "LL:other", text


def _step_n(step_dir: Optional[str]) -> Optional[int]:
    if not step_dir:
        return None
    head = step_dir.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def _verilator_records(run_dir: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        dirs = sorted(d for d in run_dir.iterdir() if d.is_dir() and d.name.split("-", 1)[-1] == "verilator-lint")
    except OSError:
        return out
    for d in dirs[-1:]:
        for log in sorted(d.glob("*.log")):
            for ln in _read_lines(log):
                m = _VL_RX.match(ln)
                if m:
                    where = (m.group(2) + ":" + m.group(3)) if m.group(2) else ""
                    out.append({"tool": "Verilator", "code": m.group(1), "msg": (where + ": " if where else "") + m.group(4).strip(),
                                "step_id": "Verilator.Lint", "step_dir": d.name, "file": m.group(2), "line": m.group(3),
                                "log": log.relative_to(run_dir).as_posix()})
    return out


def _sta_check_records(run_dir: Path) -> List[Dict[str, Any]]:
    """OpenSTA's check_setup counts in the sign-off STA's log (first corner)."""
    out: List[Dict[str, Any]] = []
    try:
        sta = sorted((d for d in run_dir.iterdir() if d.is_dir() and d.name.split("-", 1)[-1].startswith("openroad-stapostpnr")),
                     key=lambda d: d.name)
    except OSError:
        return out
    if not sta:
        return out
    d = sta[-1]
    for sub in sorted(p for p in d.iterdir() if p.is_dir() and (p / "sta.log").is_file()):
        text = "\n".join(_read_lines(sub / "sta.log"))
        for code, rx, what in (("STA:unclocked", _UNCLK_RX, "unclocked register/latch pins"),
                               ("STA:unconstrained", _UNCONS_RX, "unconstrained endpoints")):
            m = rx.search(text)
            if m and int(m.group(1)) > 0:
                out.append({"tool": "OpenSTA", "code": code, "msg": f"There are {m.group(1)} {what} (corner {sub.name}).",
                            "step_id": "OpenROAD.STAPostPNR", "step_dir": d.name,
                            "log": (sub / "sta.log").relative_to(run_dir).as_posix()})
        break
    return out


def collect(run_dir: str | Path) -> List[Dict[str, Any]]:
    """Every warning of a run as flat records ``{tool, code, msg, step_id, step_dir, …}``."""
    run_dir = Path(run_dir)
    out: List[Dict[str, Any]] = []
    for r in read_records(run_dir):
        tool, code, msg = _classify(r["text"])
        out.append({"tool": tool, "code": code, "msg": msg, "more": r["lines"][:12],
                    "step_id": r["step_id"], "step_dir": r["step_dir"]})
    out += _verilator_records(run_dir)
    out += _sta_check_records(run_dir)
    return out


def _group(records: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        key = (r["tool"], r["code"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"tool": r["tool"], "code": r["code"], "count": 0, "steps": [], "variants": {},
                               "first": {"step_id": r.get("step_id"), "step_dir": r.get("step_dir"), "msg": r["msg"],
                                         "more": r.get("more") or [], "log": r.get("log")},
                               "samples": []}
        g["count"] += 1
        sd = r.get("step_dir")
        if sd and not any(s["dir"] == sd for s in g["steps"]):
            g["steps"].append({"dir": sd, "id": r.get("step_id"), "n": _step_n(sd)})
        pat = pattern_of(r["msg"]) if r["tool"] != "Verilator" else r["msg"]
        v = g["variants"].setdefault(pat, {"text": r["msg"], "count": 0})
        v["count"] += 1
        if len(g["samples"]) < _MAX_SAMPLES and r["msg"] not in g["samples"]:
            g["samples"].append(r["msg"])
    return groups


# ---------------------------------------------------------------- waivers
class WaiverError(Exception):
    """A waiver file that can't be read or written; the message is for the user."""


def _waiver_path(design_dir: str | Path) -> Path:
    return Path(design_dir) / WAIVER_FILE


def load_waivers(design_dir: Optional[str | Path]) -> Dict[str, Any]:
    """``{file, exists, mtime, entries, error}``. A broken file never raises:
    the page shows the error and lists the warnings without waivers."""
    out: Dict[str, Any] = {"file": WAIVER_FILE, "exists": False, "mtime": None, "entries": [], "error": None}
    if not design_dir:
        return out
    p = _waiver_path(design_dir)
    if not p.is_file():
        return out
    out["exists"] = True
    try:
        out["mtime"] = p.stat().st_mtime_ns
        import yaml
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as ex:   # yaml.YAMLError, OSError, UnicodeError
        mark = getattr(ex, "problem_mark", None)
        where = f" on line {mark.line + 1}" if mark is not None else ""
        out["error"] = (f"{WAIVER_FILE} has an error{where}: {getattr(ex, 'problem', None) or ex}. "
                        "Warnings are shown without waivers until it is fixed.")
        return out
    raw = doc.get("waivers") if isinstance(doc, dict) else None
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        out["error"] = f"{WAIVER_FILE}: 'waivers' must be a list. Warnings are shown without waivers until it is fixed."
        return out
    for i, w in enumerate(raw):
        if not isinstance(w, dict) or not w.get("code"):
            continue
        out["entries"].append({
            "id": str(w.get("id") or f"W{i + 1}"), "tool": str(w.get("tool") or ""), "code": str(w.get("code")),
            "match": (str(w["match"]) if w.get("match") else None), "step": (str(w["step"]) if w.get("step") else None),
            "reason": str(w.get("reason") or "harmless"), "note": str(w.get("note") or ""),
            "by": str(w.get("by") or ""), "date": str(w.get("date") or ""),
            "expect_count": (int(w["expect_count"]) if isinstance(w.get("expect_count"), int) else None),
        })
    return out


def _q(s: Any) -> str:
    """A YAML scalar: JSON strings are valid double-quoted YAML."""
    if s is None:
        return "null"
    if isinstance(s, bool):
        return "true" if s else "false"
    if isinstance(s, int):
        return str(s)
    return json.dumps(str(s), ensure_ascii=False)


def _dump(entries: List[Dict[str, Any]]) -> str:
    lines = [
        "# LanEx warning waivers for this project.",
        "# A waiver hides nothing: LanEx still lists the warning, marked as waived, with this reason.",
        "# Waivers change only how LanEx lists warnings, never the run or its checks.",
        "# Review changes to this file like code.",
        "version: 1",
        "waivers:" + ("" if entries else " []"),
    ]
    for w in entries:
        lines.append(f"  - id: {_q(w['id'])}")
        for k in ("tool", "code", "match", "step", "reason", "note", "by", "date", "expect_count"):
            lines.append(f"    {k}: {_q(w.get(k))}")
    return "\n".join(lines) + "\n"


def _write_atomic(p: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".lanex-waivers.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check_mtime(cur: Dict[str, Any], mtime: Optional[int]) -> None:
    if mtime is not None and cur.get("mtime") is not None and int(mtime) != int(cur["mtime"]):
        raise WaiverError(f"{WAIVER_FILE} changed on disk since this page read it (another window, or an edit). "
                          "Reload the warnings and try again.")
    if cur.get("error"):
        raise WaiverError(cur["error"].replace(" Warnings are shown without waivers until it is fixed.",
                                               " Fix the file first; LanEx won't overwrite it."))


def save_waiver(design_dir: str | Path, entry: Dict[str, Any], *, mtime: Optional[int] = None,
                run_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Add a waiver, or replace the one with the same ``id``. Returns the new
    :func:`load_waivers`. Raises :class:`WaiverError` with words for the user.
    ``expect_count: "auto"`` with *run_dir*: the number of that run's messages
    this waiver matches ("ask again if the count changes")."""
    if not design_dir or not Path(design_dir).is_dir():
        raise WaiverError("Open the project first: waivers are saved in the project folder.")
    cur = load_waivers(design_dir)
    _check_mtime(cur, mtime)
    code = str(entry.get("code") or "").strip()
    if not code:
        raise WaiverError("A waiver needs the warning's code.")
    note = str(entry.get("note") or "").strip()
    if len(note) < 10:
        raise WaiverError("Write a note for a reviewer (at least 10 characters): why is this warning fine?")
    reason = str(entry.get("reason") or "")
    if reason not in REASONS:
        raise WaiverError("Pick why it's fine: " + ", ".join(REASONS.values()) + ".")
    match = str(entry.get("match") or "").strip() or None
    step = str(entry.get("step") or "").strip() or None
    exp = entry.get("expect_count")
    w = {"id": str(entry.get("id") or ""), "tool": str(entry.get("tool") or "").strip(), "code": code,
         "match": match, "step": step, "reason": reason, "note": note, "by": str(entry.get("by") or "").strip()[:80],
         "date": str(entry.get("date") or _dt.date.today().isoformat()),
         "expect_count": int(exp) if isinstance(exp, int) or (isinstance(exp, str) and exp.isdigit()) else None}
    if exp == "auto":
        w["expect_count"] = (sum(1 for r in collect(run_dir) if waiver_matches(w, r))
                             if run_dir and Path(run_dir).is_dir() else None)
    entries = list(cur["entries"])
    if w["id"] and any(e["id"] == w["id"] for e in entries):
        entries = [w if e["id"] == w["id"] else e for e in entries]
    else:
        used = {e["id"] for e in entries}
        k = len(entries) + 1
        while f"W{k}" in used:
            k += 1
        w["id"] = f"W{k}"
        entries.append(w)
    _write_atomic(_waiver_path(design_dir), _dump(entries))
    res = load_waivers(design_dir)
    res["saved"] = w["id"]
    return res


def remove_waiver(design_dir: str | Path, wid: str, *, mtime: Optional[int] = None) -> Dict[str, Any]:
    cur = load_waivers(design_dir)
    _check_mtime(cur, mtime)
    entries = [e for e in cur["entries"] if e["id"] != wid]
    if len(entries) == len(cur["entries"]):
        raise WaiverError(f"No waiver {wid} in {WAIVER_FILE}; it may have been removed already. Reload the warnings.")
    _write_atomic(_waiver_path(design_dir), _dump(entries))
    return load_waivers(design_dir)


def _base_step(s: Optional[str]) -> str:
    return re.sub(r"-\d+$", "", s or "")


def waiver_matches(w: Dict[str, Any], rec: Dict[str, Any]) -> bool:
    if w["code"] != rec["code"]:
        return False
    if w.get("tool") and w["tool"] != rec["tool"]:
        return False
    if w.get("step") and _base_step(w["step"]) != _base_step(rec.get("step_id")):
        return False
    if w.get("match") and not fnmatch.fnmatchcase(rec["msg"], w["match"]):
        return False
    return True


def _specificity(w: Dict[str, Any]) -> int:
    return (2 if w.get("step") else 0) + (1 if w.get("match") else 0)


# ---------------------------------------------------------------- the panel
def run_warnings(run_dir: str | Path, base_dir: Optional[str | Path] = None,
                 design_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Verify's warnings panel: ``{total, groups, waivers, base_tag, gone, …}``.

    Groups are one per tool and code (LibreLane's uncoded messages get LanEx
    codes ``LL:…``), each with its count, the steps that wrote it, the first
    occurrence, its variants, LanEx's explanation when it has one, whether it
    is new since *base_dir*, and the waiver that covers it."""
    run_dir = Path(run_dir)
    records = collect(run_dir)
    wv = load_waivers(design_dir)
    entries = wv["entries"] if not wv["error"] else []
    used: Dict[str, int] = {}
    for r in records:
        hits = [w for w in entries if waiver_matches(w, r)]
        if hits:
            w = max(hits, key=_specificity)
            r["waiver"] = w["id"]
            used[w["id"]] = used.get(w["id"], 0) + 1
    groups = _group(records)
    base_groups: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
    if base_dir and Path(base_dir).is_dir():
        base_groups = _group(collect(base_dir))
    by_id = {w["id"]: w for w in entries}
    out_groups: List[Dict[str, Any]] = []
    for key, g in groups.items():
        recs = [r for r in records if (r["tool"], r["code"]) == key]
        waived_ids = sorted({r["waiver"] for r in recs if r.get("waiver")})
        waived_n = sum(1 for r in recs if r.get("waiver"))
        kb = KB.get(g["code"])
        variants = sorted(g["variants"].values(), key=lambda v: -v["count"])
        tool_part = _TOOLS.get(g["code"].split("-")[0], ("", ""))[1] if "-" in g["code"] and not g["code"].startswith(("LL:", "STA:")) else ""
        item = {
            "key": g["tool"] + "|" + g["code"], "tool": g["tool"], "part": tool_part, "code": g["code"],
            "count": g["count"], "steps": sorted(g["steps"], key=lambda s: (s["n"] is None, s["n"] or 0)),
            "first": g["first"], "samples": g["samples"],
            "variants": variants[:_MAX_VARIANTS], "variants_total": len(variants),
            "title": (kb or {}).get("title"), "worry": (kb or {}).get("worry"),
            "plain": (kb or {}).get("plain"), "matters": (kb or {}).get("matters"),
            "doc": (f"https://verilator.org/guide/latest/warnings.html#cmdoption-arg-{g['code']}"
                    if g["tool"] == "Verilator" else None),
            "waived_count": waived_n,
            "waivers": [by_id[i] for i in waived_ids if i in by_id],
        }
        if waived_ids:
            for w in item["waivers"]:
                if w.get("expect_count") is not None and w["expect_count"] != used.get(w["id"], 0):
                    item["recheck"] = {"id": w["id"], "was": w["expect_count"], "now": used.get(w["id"], 0)}
        if base_groups is not None:
            b = base_groups.get(key)
            item["new"] = b is None
            item["base_count"] = b["count"] if b else 0
        out_groups.append(item)
    rank = {"look": 0, "info": 1, None: 2, "harmless": 3}

    def order(g: Dict[str, Any]) -> Tuple[Any, ...]:
        waived = g["waived_count"] >= g["count"]
        return (waived, not g.get("new", False), rank.get(g["worry"], 2), -g["count"], g["code"])

    out_groups.sort(key=order)
    gone = []
    if base_groups is not None:
        for key, b in base_groups.items():
            if key not in groups:
                kb = KB.get(b["code"]) or {}
                gone.append({"tool": b["tool"], "code": b["code"], "count": b["count"], "title": kb.get("title")})
    unused = [w for w in entries if not used.get(w["id"])]
    n_waived_groups = sum(1 for g in out_groups if g["waived_count"] >= g["count"])
    return {
        "ok": True, "tag": run_dir.name, "total": len(records), "groups": out_groups,
        "group_count": len(out_groups), "waived_groups": n_waived_groups,
        "waived_messages": sum(g["waived_count"] for g in out_groups),
        "look": sum(1 for g in out_groups if g["worry"] == "look" and g["waived_count"] < g["count"]),
        "new_count": sum(1 for g in out_groups if g.get("new")) if base_groups is not None else None,
        "base_tag": Path(base_dir).name if base_groups is not None else None,
        "gone": gone,
        "waivers": {"file": WAIVER_FILE, "exists": wv["exists"], "mtime": wv["mtime"], "error": wv["error"],
                    "entries": entries, "unused": unused, "reasons": REASONS},
        "has_log": (run_dir / "warning.log").is_file(),
    }


def summary_line(w: Dict[str, Any]) -> str:
    """"32 messages in 9 groups · 1 new since tuned · 1 group waived"."""
    parts = [f"{w['total']} message{'s' if w['total'] != 1 else ''} in {w['group_count']} group{'s' if w['group_count'] != 1 else ''}"]
    if w.get("base_tag") and w.get("new_count") is not None:
        parts.append(f"{w['new_count']} new since {w['base_tag']}")
    if w.get("waived_groups"):
        parts.append(f"{w['waived_groups']} group{'s' if w['waived_groups'] != 1 else ''} waived")
    if w.get("look"):
        parts.append(f"{w['look']} need{'s' if w['look'] == 1 else ''} a look")
    return " · ".join(parts)
