# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Why a run ended the way it did, in plain words, for the Flow page.

Reads only what LibreLane wrote: ``error.log`` and ``warning.log`` at the run
root, the stopped step's own log (for the line of the first error), and
``resolved.json`` for the value a fix would change. What an error means and
what to change come from the one knowledge base, ``problems.py``. The run's
outcome comes from the same flow state Verify uses (``verify._flow_state``),
so the two pages never disagree about whether a run halted or finished with
failing checks.

``kind``:
  ``clean``         reached the end, nothing raised
  ``checks-failed`` reached the end (``final/`` written), then LibreLane failed
                    the run on its deferred sign-off checks
  ``halted``        a step raised; nothing after it ran
  ``stopped``       the user stopped it (``gui-run.json`` ``outcome``)
  ``incomplete``    the folder ends mid-flow with no error: still running, or
                    the process was killed
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import flowstages, problems, verify

_CODE_RX = re.compile(r"\[(?:ERROR |WARNING )?([A-Z]{2,5}-\d{4})\]")
# OpenROAD "[ERROR DPL-0036]", Yosys "ERROR:" (also "f.v:67: ERROR: …"), Tcl "Error:".
_ERR_RX = re.compile(r"\[ERROR(?: [A-Z]{2,5}-\d{4})?\]|^ERROR\b|\bError:|\bERROR: ")
# A Tcl script that died (Netgen, Magic) prints its error, then "while executing".
_TCL_TRACE = re.compile(r"^\s+while executing$")
# error.log lines LibreLane adds around a tool's own (step.py run_subprocess).
_BOILER = re.compile(r"^(Subprocess had a non-zero exit\.|Last \d+ line\(s\):|Full log file: .*|\s*)$")


def _read(p: Path, limit: int = 2_000_000) -> str:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def _lines(p: Path) -> List[str]:
    return [ln.rstrip() for ln in _read(p).splitlines() if ln.strip()]


def _step_log(run_dir: Path, step_dir: str) -> Optional[Path]:
    d = run_dir / step_dir
    if not d.is_dir():
        return None
    suffix = step_dir.split("-", 1)[1] if "-" in step_dir else step_dir
    log = d / (suffix + ".log")
    if log.is_file():
        return log
    logs = sorted(d.glob("*.log"))
    return logs[0] if logs else None


def _first_error(log: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Line number (1-based) and text of the first error in a step log."""
    if log is None:
        return None
    lines = _read(log).splitlines()
    for i, ln in enumerate(lines, 1):
        if _ERR_RX.search(ln):
            return {"line": i, "text": ln.strip()[:400]}
    for i, ln in enumerate(lines[1:], 2):
        if _TCL_TRACE.match(ln) and lines[i - 2].strip():
            return {"line": i - 1, "text": lines[i - 2].strip()[:400]}
    return None


def _log_tail(log: Optional[Path], n: int = 80) -> str:
    if log is None:
        return ""
    try:
        with log.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-n:])


def _tool_of(step_id: str) -> str:
    head = problems.slug(step_id).split("-", 1)[0]
    return {"openroad": "OpenROAD", "odb": "OpenROAD", "yosys": "Yosys", "verilator": "Verilator", "magic": "Magic",
            "klayout": "KLayout", "netgen": "Netgen", "checker": "LibreLane's checker",
            "misc": "LibreLane"}.get(head, "The tool")


def _versions(run: "verify._Run") -> Dict[str, Any]:
    """What a helper needs to know: the versions that made this run."""
    tc = (run.gui.get("toolchain") or {}) if isinstance(run.gui, dict) else {}
    cfg = run.cfg or {}
    meta = cfg.get("meta") if isinstance(cfg.get("meta"), dict) else {}
    m = re.search(r"/versions/([0-9a-f]{7,40})", str(cfg.get("PDK_ROOT") or ""))
    try:
        from .. import _version
        lanex_v = _version.get_version()
    except Exception:
        lanex_v = "unknown"
    return {"lanex": tc.get("lanex_version") or lanex_v, "lanex_now": lanex_v,
            "librelane": tc.get("librelane_version") or (meta or {}).get("librelane_version") or "unknown",
            "image": tc.get("image"), "run_mode": tc.get("run_mode") or run.gui.get("run_mode"),
            "pdk": cfg.get("PDK"), "pdk_version": m.group(1) if m else None, "scl": cfg.get("STD_CELL_LIBRARY"),
            "flow": run.gui.get("flow") or (meta or {}).get("flow") or "Classic"}


_HELP_KEYS = ("DESIGN_NAME", "CLOCK_PORT", "CLOCK_PERIOD", "FP_SIZING", "DIE_AREA", "FP_CORE_UTIL", "PL_TARGET_DENSITY_PCT")


def help_text(run: "verify._Run", out: Dict[str, Any], card: Dict[str, Any], tail: str) -> str:
    """"Copy for help": a short plain-text block with the versions, the step,
    the error and the log's last lines, paths shown relative to the project."""
    v = _versions(run)
    cfg = run.cfg or {}
    design = str(cfg.get("DESIGN_DIR") or "").rstrip("/")
    step = out.get("step") or {}
    at = out.get("at") or {}
    err = (card.get("evidence") or [None])[0] or at.get("text") or next(
        (e for e in out.get("errors") or [] if not _BOILER.match(e)), "") or "(no error message)"
    lines = [
        "LanEx run report: " + str(out.get("tag")),
        "Stopped at step %s %s (%s)%s" % (step.get("n") or "?", step.get("id") or step.get("dir") or "?",
                                          out.get("stage") or "?", ", " + card["title"] if card.get("title") else ""),
        "Error: " + err,
        "LanEx %s · LibreLane %s · %s%s" % (v["lanex"], v["librelane"], v["run_mode"] or "local",
                                          (" · image " + v["image"]) if v.get("image") else ""),
        "PDK %s%s · library %s · flow %s" % (v["pdk"] or "?", (" (open_pdks " + v["pdk_version"][:12] + ")") if v["pdk_version"] else "",
                                            v["scl"] or "?", v["flow"]),
        "Config: " + " ".join("%s=%s" % (k, problems.fmt(cfg[k])) for k in _HELP_KEYS if cfg.get(k) not in (None, "")),
    ]
    if tail:
        lines += ["", "Last lines of " + (at.get("path") or step.get("dir") or "the step's log") + ":"]
        lines += ["  " + ln for ln in tail.splitlines()[-15:]]
    text = "\n".join(lines)
    return text.replace(design + "/", "") if design else text


def _warnings(run_dir: Path, base_dir: Optional[Path], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # One warning per record, not per line: a multi-line record ("Setup
    # violations found in the following corners:" then "* corner" lines) is
    # one warning (runwarnings.read_records, the same count Verify shows).
    from . import runwarnings
    lines = [r["text"] for r in runwarnings.read_records(run_dir)]
    base_codes = None
    if base_dir is not None:
        base_codes = {m.group(1) for ln in (r["text"] for r in runwarnings.read_records(base_dir))
                      for m in [_CODE_RX.search(ln)] if m}
    groups: Dict[str, Dict[str, Any]] = {}
    other = 0
    for ln in lines:
        m = _CODE_RX.search(ln)
        if not m:
            other += 1
            continue
        g = groups.setdefault(m.group(1), {"code": m.group(1), "count": 0, "sample": ln[:300]})
        g["count"] += 1
    # The full GPL-0302 text spans two lines in the step log; read it there.
    for code, g in groups.items():
        if code in problems.WARNING_NOTES:
            g["note"] = problems.WARNING_NOTES[code](_grep_step_logs(run_dir, code) or g["sample"], cfg or {})
        if base_codes is not None:
            g["new"] = code not in base_codes
    ordered = sorted(groups.values(), key=lambda g: (not g.get("note"), not g.get("new", False), -g["count"]))
    return {"total": len(lines), "by_code": ordered, "other": other,
            "compared_with": base_dir.name if base_dir is not None else None}


def _grep_step_logs(run_dir: Path, code: str) -> Optional[str]:
    try:
        for log in sorted(run_dir.glob("*/*.log")):
            text = _read(log, 4_000_000)
            i = text.find(code)
            if i >= 0:
                return text[i:i + 400]
    except Exception:
        pass
    return None


# A checker step that raised only says "N Lint errors found." and writes no
# log; what failed is in the tool step it checks. Each checker -> the check
# id problems.py explains, a title, and the tool step whose log has the lines.
_CHECKERS = {
    "Checker.LintErrors": ("lint_errors", "The RTL has lint errors", "Verilator.Lint"),
    "Checker.LintTimingConstructs": ("lint_timing_constructs", "The RTL has delays synthesis can't build", "Verilator.Lint"),
    "Checker.LintWarnings": ("lint_warnings", "The RTL has lint warnings, and this run fails on them", "Verilator.Lint"),
    "Checker.YosysUnmappedCells": ("unmapped", "Synthesis left cells it couldn't map", "Yosys.Synthesis"),
    "Checker.YosysSynthChecks": ("synth_check", "Synthesis found structural problems", "Yosys.Synthesis"),
    "Checker.NetlistAssignStatements": ("assign", "The netlist has assign statements", "Yosys.Synthesis"),
}
_VL_RX = re.compile(r"^%(Error|Warning)(?:-([A-Z0-9_]+))?: (.*)")
_YS_RX = re.compile(r"^\s*(?:Warning|ERROR|Error): (.*)")


def _is_step(s: Dict[str, Any], step_id: str) -> bool:
    """A step folder is *step_id*: by the id flow.log gave it, else by its
    folder name ("03-checker-linterrors" is Checker.LintErrors)."""
    if s.get("id"):
        return s["id"] == step_id
    return str(s.get("dir") or "").split("-", 1)[-1] == step_id.lower().replace(".", "-")


def _checker_of(step: Dict[str, Any]) -> Optional[tuple]:
    return next((v for k, v in _CHECKERS.items() if _is_step(step, k)), None)


def _checker_evidence(run: "verify._Run", tool_id: str, design_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    """The tool's own error lines behind a checker that raised: up to three,
    errors first, with paths relative to the design; and where the first one
    is in the tool step's log (``{lines, at}``)."""
    step = next((s for s in run.steps if _is_step(s, tool_id)), None)
    if step is None:
        return None
    log = _step_log(run.dir, step["dir"])
    if log is None:
        return None
    vl = tool_id.startswith("Verilator")
    rx = _VL_RX if vl else _YS_RX
    lines = _read(log).splitlines()
    rel = (lambda t: t.replace(str(design_dir).rstrip("/") + "/", "")) if design_dir is not None else (lambda t: t)
    hits: List[Any] = []
    seen: Dict[str, int] = {}
    for i, ln in enumerate(lines, 1):
        m = rx.match(ln)
        if not m or "Exiting due to" in ln:
            continue
        text = rel(ln.strip())
        if not vl:
            # Yosys puts where on the indented lines below ("source: f.v:11.17-11.33"):
            # name the files and lines, so a loop or undriven wire points at the RTL.
            where = []
            for nxt in lines[i:i + 12]:
                if not nxt.startswith(" "):
                    break
                for s in re.findall(r"source: (\S+?):(\d+)\.", nxt):
                    w = rel(s[0]) + ":" + s[1]
                    if w not in where:
                        where.append(w)
            if where:
                text = text.rstrip(":") + " (" + ", ".join(where[:4]) + ")"
        if text in seen:
            hits[seen[text]][3] += 1
            continue
        err = vl and m.group(1) == "Error" or ln.lstrip().upper().startswith("ERROR")
        seen[text] = len(hits)
        hits.append([0 if err else 1, i, text[:400], 1])
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], h[1]))
    first = hits[0]
    return {"lines": [h[2] + (" (×%d)" % h[3] if h[3] > 1 else "") for h in hits[:3]],
            "at": {"path": step["dir"] + "/" + log.name, "line": first[1], "text": first[2], "step": tool_id}}


def _outcome(run_dir: Path) -> Optional[str]:
    try:
        doc = json.loads((run_dir / "gui-run.json").read_text(encoding="utf-8"))
        out = doc.get("outcome") if isinstance(doc, dict) else None
        return (out or {}).get("result") if isinstance(out, dict) else None
    except Exception:
        return None


def explain_run(run_dir: str | Path, base_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Outcome, reason, fixes and warnings for one run (see module doc)."""
    run_dir = Path(run_dir)
    base = Path(base_dir) if base_dir else None
    run = verify._Run(run_dir)
    fs = verify._flow_state(run)
    cfg = run.cfg or {}
    outcome = _outcome(run_dir)
    state = fs.get("state")
    if state == "complete":
        kind = "clean"
    elif state in ("deferred_errors", "complete_with_errors"):
        kind = "checks-failed"
    elif outcome == "cancelled":
        kind = "stopped"
    elif state == "stopped" or fs.get("error_log"):
        kind = "halted"
    else:
        kind = "incomplete"
    step = fs.get("stopped_step") if kind in ("halted", "stopped") else None
    if kind == "incomplete" and fs.get("last_step"):
        step = fs["last_step"]
    out: Dict[str, Any] = {
        "tag": run_dir.name, "kind": kind, "state": state, "step": step,
        "stage": None, "errors": fs.get("error_log") or [],
        "raised_checkers": fs.get("raised_checkers") or [],
        "steps_done": fs.get("steps_done"), "steps_total": fs.get("steps_total"),
        "card": None, "at": None,
        "warnings": _warnings(run_dir, base if base and base.is_dir() else None, cfg),
    }
    if step:
        # Unlisted steps inherit the stage before them, so walk the run in order.
        names = [s.get("id") or s["dir"] for s in run.steps]
        stages = flowstages.stages_of(names)
        hit = next((i for i, s in enumerate(run.steps) if s["dir"] == step.get("dir")), None)
        out["stage"] = stages[hit] if hit is not None else flowstages.stages_of([step.get("id") or step.get("dir")])[0]
    if kind == "halted" and step:
        out["card"] = _halted_card(run, run_dir, step, out, cfg)
    # A run resumed from another one: where it started, and what changed.
    out["branched_from"] = run.branch
    return out


def _halted_card(run: "verify._Run", run_dir: Path, step: Dict[str, Any], out: Dict[str, Any],
                 cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The failure card of a step that raised: the knowledge base's
    (problems.KB) when it knows the error, an LVS crash, a step that stopped
    with no message (disk, memory), else the generic card. Every card carries
    "Copy for help" text."""
    log = _step_log(run_dir, step["dir"])
    first = _first_error(log)
    if first and log is not None:
        out["at"] = {"path": step["dir"] + "/" + log.name, **first}
    tail = _log_tail(log)
    errors = [e for e in out["errors"] if not _BOILER.match(e)]
    text = "\n".join(out["errors"]) or (first or {}).get("text", "")
    outcome = ""
    try:
        oc = run.gui.get("outcome") if isinstance(run.gui, dict) else None
        outcome = str((oc or {}).get("error") or "") if isinstance(oc, dict) else ""
    except Exception:
        outcome = ""
    sid = step.get("id") or step["dir"]
    ck = _checker_of(step)
    ev = None
    if ck:
        # A checker that raised names only a count: the tool step's own lines
        # (Verilator's "%Error: f.v:67:13: syntax error") are the evidence.
        dd = run_dir.parent.parent if run_dir.parent.name == "runs" else None
        ev = _checker_evidence(run, ck[2], dd)
        if ev:
            out["at"] = ev["at"]
    card = problems.explain_error(text, cfg, step=sid, outcome=outcome, run_dir=run_dir,
                                  log="\n".join((ev or {}).get("lines") or []) + "\n" + tail)
    if ck and not card.get("title"):
        key, title, _tool = ck
        card["title"] = title
        card["plain"] = problems._CHECK_PLAIN.get(key) or card.get("plain")
        if key in problems._CHECK_OPEN:
            label, go, why = problems._CHECK_OPEN[key]
            card["fixes"] = list(card.get("fixes") or []) + [problems._open(label, go, why)]
    if ev and not card.get("evidence"):
        card["evidence"] = ev["lines"]
    if not card.get("title") and (sid == "Netgen.LVS" or step["dir"].split("-", 1)[-1] == "netgen-lvs"):
        card.update(problems.lvs_crash((first or {}).get("text") or ""))
    if not card.get("title") and not errors and not first:
        card.update(problems.explain_silent(run_dir, step, outcome))
    if not card.get("title"):
        # Generic: the tool's first error, where, and a way to ask for help.
        tool = _tool_of(sid)
        card["generic"] = True
        card["plain"] = ("%s stopped the run at %s with the error below. LanEx has no explanation for this one yet. The "
                         "step's log has the lines around it; Copy for help puts the versions, the step, the error and "
                         "the log's last lines on the clipboard, for a forum post or an issue." % (tool, sid or step["dir"]))
        lead = (first or {}).get("text") or (errors[0] if errors else "")
        if lead:
            card["evidence"] = [lead] + [e for e in errors if e != lead][:2]
    if not card.get("evidence") and errors and len(errors) < len(out["errors"]):
        # error.log wrapped the tool's line in LibreLane's own: show the tool's.
        hit = [e for e in errors if problems._CODE_RX.search(e) or "ERROR" in e or "Error" in e]
        card["evidence"] = (hit or errors)[:3]
    card["fixes"] = problems.finish(card.get("fixes", []), (cfg.get("meta") or {}).get("flow") or "Classic")
    card["help"] = help_text(run, out, card, tail)
    return card
