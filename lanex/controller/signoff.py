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
"""A sign-off summary of one run as files: ``report.json`` (the data),
``report.md`` and a self-contained ``report.html``, written to
``<run>/lanex-report/`` with a ``SHA256SUMS`` beside them.

One data object, rendered twice, so the two files can't disagree. Every
number is one LibreLane wrote (``verify.verify_report``, the STA reports, the
metrics) and carries the file it came from. What makes it trustworthy, in the
order a reviewer reads it: "not a foundry sign-off" first; then who made it
with what (LanEx, LibreLane, PDK commit, container image digest, the config's
SHA-256, the exact command, the GDS's SHA-256); then the verdict in the same
words Verify uses; then the evidence; then the files read, each with its
SHA-256; then what the report does not cover.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import html as _html
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import history, runwarnings, timing, verify

REPORT_DIR = "lanex-report"
NOT_SIGNOFF = ("Not a foundry sign-off. LanEx repeats what LibreLane's own checks found on this run. "
               "Before fabrication, check the GDS with the foundry's or the shuttle's own rule decks and precheck.")
NOT_COVERED = [
    "Simulation of the finished netlist with real delays (gate-level simulation).",
    "A shuttle's own precheck (Tiny Tapeout, ChipFoundry) and the foundry's sign-off rule decks.",
    "Power with real switching activity, crosstalk and signal integrity, electromigration, ESD and latch-up.",
    "Anything LibreLane did not run in this flow (the \"Not checked\" and \"Turned off\" rows above).",
]
STATUS = {"pass": "Passed", "fail": "Failed", "warn": "Warning", "info": "Info",
          "skipped": "Turned off", "not_run": "Not checked"}
MARK = {"pass": "✅", "fail": "❌", "warn": "⚠️", "info": "ℹ️", "skipped": "➖", "not_run": "⬜"}
GATE = {"blocks": "fails the run", "warns": "only warns", "info": "not checked by LibreLane"}
_SENT = 1e29


# ---------------------------------------------------------------- formatting
def _isnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fmt_n(v: Any) -> str:
    """Counts with thousands separators, other numbers to 3 significant decimals."""
    if not _isnum(v):
        return "—" if v is None else str(v)
    if float(v).is_integer() and abs(v) < 1e15:
        return f"{int(v):,}"
    a = abs(v)
    d = 3 if a < 10 else 2 if a < 100 else 1
    return f"{v:,.{d}f}"


def fmt_ns(v: Any) -> str:
    """A slack: signed, 3 decimals; a failure never rounds away to 0 (6 decimals)."""
    if not _isnum(v):
        return "—"
    if abs(v) >= _SENT:
        return "no path"
    if v < 0 and abs(v) < 0.0005:
        return f"−{abs(v):.6f}"
    s = f"{abs(v):.3f}"
    return ("+" if v > 0 else "−" if v < 0 else "") + s


def value_text(c: Dict[str, Any]) -> str:
    """One check's value in the words Verify shows (verify.js valueText)."""
    st = c.get("status")
    if st == "not_run":
        return "—"
    if c.get("id") in ("setup", "hold"):
        parts = []
        if c.get("value") is not None and str(c.get("metric") or "").endswith("vio__count"):
            parts.append(fmt_n(c["value"]) + " violating")
        if _isnum(c.get("worst")):
            parts.append("worst " + fmt_ns(c["worst"]) + " ns")
        return " · ".join(parts) or "—"
    if c.get("id") == "antenna" and c.get("breakdown"):
        nets = next((b for b in c["breakdown"] if b.get("metric") == "antenna__violating__nets"), None)
        pins = next((b for b in c["breakdown"] if b.get("metric") == "antenna__violating__pins"), None)
        return f"{fmt_n(nets['value'] if nets else c.get('value'))} nets · {fmt_n(pins['value'] if pins else 0)} pins"
    v = c.get("value")
    if v is None:
        return "off" if st == "skipped" else "error raised" if st == "fail" else "—"
    if isinstance(v, str):
        return v
    return fmt_n(v) + (" " + c["unit"] if c.get("unit") else "")


def _sha256(p: Path, limit: Optional[int] = None) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with p.open("rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def _rel(run_dir: Path, p: Path) -> str:
    try:
        return p.relative_to(run_dir).as_posix()
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- facts
def _pdk_commit(pdk_root: Any) -> Optional[str]:
    m = re.search(r"/versions/([0-9a-f]{7,40})(?:/|$)", str(pdk_root or ""))
    return m.group(1) if m else None


_VERSION_RX = [
    ("Yosys", re.compile(r"\bYosys (\d+\.\d+(?:\+\d+)?)")),
    ("Magic", re.compile(r"\bMagic (\d+\.\d+) revision (\d+)")),
    ("Netgen", re.compile(r"\bNetgen (\d+\.\d+\.\d+)")),
    ("Verilator", re.compile(r"\bVerilator (\d+\.\d+)")),
    ("KLayout", re.compile(r"\bKLayout (\d+\.\d+(?:\.\d+)?)")),
]


def _tool_versions(run_dir: Path) -> List[Dict[str, str]]:
    """Versions the tools printed in this run's own logs (the ones that print
    one). OpenROAD and OpenSTA don't print theirs there: the image digest in
    the header names them."""
    found: Dict[str, str] = {}
    try:
        logs = sorted(run_dir.glob("*/*.log"))
    except OSError:
        logs = []
    for log in logs:
        if len(found) == len(_VERSION_RX):
            break
        try:
            with log.open("r", encoding="utf-8", errors="replace") as f:
                head = f.read(20000)
        except OSError:
            continue
        for name, rx in _VERSION_RX:
            if name in found:
                continue
            m = rx.search(head)
            if m:
                found[name] = m.group(1) + (f" rev {m.group(2)}" if name == "Magic" and m.lastindex and m.lastindex > 1 else "")
    return [{"tool": k, "version": v} for k, v in found.items()]


_POWER_RX = re.compile(r"^Total\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)")


def _power_by_corner(run: "verify._Run") -> List[Dict[str, Any]]:
    out = []
    for c, d in verify._sta_corner_dirs(run).items():
        try:
            for ln in (d / "power.rpt").read_text(encoding="utf-8", errors="replace").splitlines():
                m = _POWER_RX.match(ln.strip())
                if m:
                    vals = [float(x) for x in m.groups()]
                    out.append({"corner": c, "internal": vals[0], "switching": vals[1], "leakage": vals[2],
                                "total": vals[3], "source": _rel(run.dir, d / "power.rpt")})
                    break
        except (OSError, ValueError):
            continue
    return out


def _area(m: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    bb = m.get("design__die__bbox")
    if isinstance(bb, str):
        try:
            x0, y0, x1, y1 = (float(x) for x in bb.split())
            out["die_w"], out["die_h"] = round(x1 - x0, 3), round(y1 - y0, 3)
        except ValueError:
            pass
    for k, key in (("die_area", "design__die__area"), ("core_area", "design__core__area"),
                   ("util", "design__instance__utilization"), ("stdcells", "design__instance__count__stdcell"),
                   ("flipflops", "design__instance__count__class:sequential_cell"), ("wire", "route__wirelength"),
                   ("instances", "design__instance__count")):
        if _isnum(m.get(key)):
            out[k] = m[key]
    if _isnum(out.get("stdcells")):
        phys = sum(m.get(f"design__instance__count__class:{c}") or 0 for c in
                   ("tap_cell", "endcap_cell", "decap_cell", "antenna_cell", "welltap_cell", "diode_cell")
                   if _isnum(m.get(f"design__instance__count__class:{c}")))
        out["logic_cells"] = int(out["stdcells"] - phys)
    return out


def _path_summary(run_dir: Path, kind: str, base_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    try:
        lst = timing.path_list(run_dir, kind=kind)
        if not lst.get("ok") or not lst.get("rows"):
            return None
        d = timing.path_detail(run_dir, kind=kind, corner=lst["corner"], i=lst["rows"][0]["i"], base_dir=base_dir)
        if not d.get("ok"):
            return None
    except Exception:
        return None
    rows = []
    for part in ("launch", "data", "capture"):
        sec = d[part]
        if part != "data":
            rows.append({"what": f"{part} clock" + (f": {sec['cells']} cells to {sec['to']}" if sec.get("to") else ""),
                         "incr": sec.get("delay"), "time": None, "folded": True})
            continue
        for r in sec["rows"]:
            name = r.get("pin") or r.get("port") or r.get("word") or r.get("label") or ""
            rows.append({"what": name, "cell": r.get("cell") or ("port" if r.get("port") else ""),
                         "edge": {"r": "↑", "f": "↓"}.get(r.get("edge") or "", ""),
                         "fanout": r.get("fanout"), "cap_ff": (r["cap"] * 1000 if _isnum(r.get("cap")) else None),
                         "slew": r.get("slew"), "incr": r.get("delay"), "time": r.get("time")})
    return {"kind": kind, "corner": d["corner"], "start": d["start"], "start_desc": d["start_desc"], "end": d["end"],
            "end_cell": d.get("end_cell"), "slack": d["slack"], "met": d["met"], "arrival": d["arrival"],
            "required": d["required"], "arrival_terms": d["arrival_terms"], "required_terms": d["required_terms"],
            "rows": rows, "source": d["source"], "line": d.get("line"), "endpoints": lst["total"]}


def build(run_dir: str | Path, *, design_dir: Optional[str | Path] = None, base_dir: Optional[str | Path] = None,
          verdict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The report data. *verdict* is Verify's own words for this run
    (``{tone, title, sub}`` from verify-model.js), so the file says exactly
    what the screen says; without it the server's run verdict label is used."""
    run_dir = Path(run_dir).resolve()
    base = Path(base_dir).resolve() if base_dir else None
    rep = verify.verify_report(run_dir)
    run = verify._Run(run_dir)
    gui = run.gui or {}
    tc = gui.get("toolchain") or {}
    m = run.metrics or {}
    files: Dict[str, Path] = {}

    def use(p: Optional[Path]) -> None:
        if p is not None and p.is_file():
            files[_rel(run_dir, p)] = p

    final_m = run_dir / "final" / "metrics.json"
    use(final_m)
    use(run_dir / "resolved.json")
    use(run_dir / "warning.log")
    use(run_dir / "gui-run.json")
    gds = sorted((run_dir / "final" / "gds").glob("*.gds")) if (run_dir / "final" / "gds").is_dir() else []
    gds_info = None
    if gds:
        gds_info = {"path": _rel(run_dir, gds[0]), "size": gds[0].stat().st_size, "sha256": _sha256(gds[0])}
    cmd = gui.get("cli_command") or {}
    if isinstance(cmd, dict):
        cmd = cmd.get(cmd.get("recommended") or "container") or cmd.get("local") or ""
    v = verdict if isinstance(verdict, dict) and verdict.get("title") else None
    rv = rep.get("run_verdict") or {}
    if v is None:
        tone = {"passed": "pass", "failed": "fail"}.get(rv.get("state"), "warn")
        v = {"tone": tone, "title": rv.get("label") or "Unknown", "sub": ""}
    checks = []
    for g in rep.get("groups") or []:
        for cid in g["checks"]:
            c = next((x for x in rep["checks"] if x["id"] == cid), None)
            if not c:
                continue
            src = ""
            if c.get("metric") and c.get("values"):
                src = ("final/metrics.json" if final_m.is_file() else "metrics") + " " + str(c["metric"])
            gate = GATE.get(c.get("gate"), "")
            note = str(c.get("gate_note") or "")
            # A switch that is off, or a corner rule that matches no corner,
            # means LibreLane would only warn here.
            if c.get("gate") == "blocks" and (note.endswith("= false") or "only warns" in note):
                gate = GATE["warns"]
            checks.append({"id": c["id"], "group": g.get("title"), "name": c["name"], "status": c["status"],
                           "result": STATUS.get(c["status"], c["status"]), "value": value_text(c),
                           "limit": c.get("limit") or "", "gate": gate,
                           "gate_note": c.get("gate_note") or "", "source": src, "detail": c.get("detail") or ""})
    t = (rep.get("details") or {}).get("timing") or {}
    corners = []
    for r in sorted(t.get("corners") or [], key=lambda r: r["corner"]):
        g = r.get("gates") or {}
        corners.append({k: r.get(k) for k in ("corner", "setup_ws", "setup_tns", "setup_vio", "hold_ws", "hold_tns",
                                              "hold_vio", "fmax", "period_min")} |
                       {"setup_gate": g.get("setup"), "hold_gate": g.get("hold")})
    paths = {}
    for kind in ("setup", "hold"):
        ps = _path_summary(run_dir, kind, base)
        if ps:
            paths[kind] = ps
            p = run_dir / ps["source"]["rel"] if ps["source"].get("tag") == run_dir.name else None
            use(p)
    power = _power_by_corner(run)
    for pw in power[:1]:
        use(run_dir / pw["source"])
    d = rep.get("details") or {}
    for key in ("magic_drc", "klayout_drc", "route_drc", "xor", "antenna", "ir"):
        x = d.get(key) or {}
        if x.get("report"):
            use(run_dir / x["report"])
    lv = d.get("lvs") or {}
    if lv.get("report"):
        use(run_dir / lv["report"])
    by = {c["id"]: c for c in rep.get("checks") or []}
    physical = {
        "magic_drc": by.get("magic_drc", {}).get("value"), "klayout_drc": by.get("klayout_drc", {}).get("value"),
        "route_drc": by.get("route_drc", {}).get("value"),
        "klayout_rules": (d.get("klayout_drc") or {}).get("rules"),
        "lvs": lv.get("result"), "lvs_counts": {"layout": lv.get("layout"), "netlist": lv.get("netlist")} if lv.get("layout") else None,
        "xor": by.get("xor", {}).get("value"), "xor_layers": (d.get("xor") or {}).get("layers"),
        "antenna_nets": m.get("antenna__violating__nets"), "antenna_pins": m.get("antenna__violating__pins"),
        "diodes": m.get("antenna_diodes_count"),
        "pdn": by.get("pdn", {}).get("value"),
        "ir": [{"net": r.get("net"), "pct": r.get("pct"), "worst_mv": (r["worst_drop"] * 1000 if _isnum(r.get("worst_drop")) else None)}
               for r in ((d.get("ir") or {}).get("nets") or [])],
    }
    w = runwarnings.run_warnings(run_dir, base, design_dir)
    warn_rows = [{"tool": g["tool"], "code": g["code"], "count": g["count"], "worry": g.get("worry"),
                  "title": g.get("title") or (g["samples"][0] if g.get("samples") else ""),
                  "new": g.get("new"), "waived": g["waived_count"] >= g["count"],
                  "waiver": ({"reason": runwarnings.REASONS.get(g["waivers"][0]["reason"], g["waivers"][0]["reason"]),
                              "note": g["waivers"][0]["note"], "by": g["waivers"][0]["by"], "date": g["waivers"][0]["date"]}
                             if g.get("waivers") else None)}
                 for g in w["groups"]]
    file_rows = [{"path": rel, "size": p.stat().st_size, "sha256": _sha256(p)} for rel, p in sorted(files.items())]
    try:
        from .. import _version
        lanex_v = _version.get_version()
    except Exception:
        lanex_v = tc.get("lanex_version") or "unknown"
    clk = run.cfg.get("CLOCK_PERIOD")
    unc = next((tt["v"] for tt in (paths.get("setup") or {}).get("required_terms", []) if tt["word"] == "uncertainty"), None)
    try:
        from . import platform_env
        now = platform_env.local_now()
    except Exception:
        now = _dt.datetime.now().astimezone()
    return {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "not_signoff": NOT_SIGNOFF,
        "verdict": {"tone": v.get("tone"), "title": v.get("title"), "sub": v.get("sub") or "",
                    "state": rv.get("state"), "label": rv.get("label")},
        "identity": {
            "design": rep.get("design"), "run": run_dir.name, "pdk": rep.get("pdk"), "scl": rep.get("scl"),
            "pdk_commit": _pdk_commit(run.cfg.get("PDK_ROOT")), "flow": rep.get("flow") or "Classic",
            "librelane": rep.get("librelane_version") or tc.get("librelane_version"),
            "image": tc.get("image"), "engine": tc.get("run_mode") or gui.get("run_mode"),
            "config_file": Path(str(gui.get("config_file") or "")).name or None, "config_sha256": gui.get("config_hash"),
            "overrides": gui.get("overrides") or {}, "command": cmd or None,
            "finished": ((gui.get("outcome") or {}).get("ended_at") if isinstance(gui.get("outcome"), dict) else None) or gui.get("written_at"),
            "steps": (rep.get("flow_state") or {}).get("steps_total"),
            "gds": gds_info, "lanex": lanex_v,
        },
        "checks": checks,
        "timing": {"clock_period": clk, "mhz": (round(1000.0 / float(clk), 3) if _isnum(clk) and clk else None),
                   "clock_port": t.get("clock_port"), "uncertainty": unc, "source": t.get("source"),
                   "no_clock": t.get("no_clock"), "corners": corners,
                   "setup_rule": t.get("setup_rule"), "hold_rule": t.get("hold_rule"),
                   "worst_setup": paths.get("setup"), "worst_hold": paths.get("hold")},
        "physical": physical,
        "area": _area(m),
        "power": {"corners": power, "metric_total": m.get("power__total"),
                  "note": "Estimated by OpenSTA with default switching activity (no simulation data). "
                          "LibreLane's metric power__total holds the last corner it timed"
                          + (f" ({power[-1]['corner']})." if power else ".")},
        "warnings": {"summary": runwarnings.summary_line(w), "total": w["total"], "groups": warn_rows,
                     "base": w.get("base_tag"), "waiver_error": w["waivers"].get("error")},
        "tools": _tool_versions(run_dir),
        "files": file_rows,
        "not_covered": NOT_COVERED,
    }


# ---------------------------------------------------------------- Markdown
def _md_cell(s: Any) -> str:
    return str("" if s is None else s).replace("|", "\\|").replace("\n", " ")


def _table(head: List[str], rows: List[List[Any]]) -> List[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in rows]
    return out


def _gate_mark(g: Any) -> str:
    return "●" if g else "○" if g is False else ""


def _mw(v: Any) -> str:
    return f"{v * 1000:.3f} mW" if _isnum(v) else "—"


def _terms(ts: List[Dict[str, Any]]) -> str:
    out = []
    for i, t in enumerate(ts):
        v = t["v"]
        sign = "" if i == 0 else (" + " if v >= 0 else " − ")
        out.append(f"{sign}{abs(v):.3f} {t['word']}" if i else f"{v:.3f} {t['word']}")
    return "".join(out)


def render_md(r: Dict[str, Any]) -> str:
    idn = r["identity"]
    L: List[str] = [f"# Sign-off summary: {idn['design'] or 'design'}, run {idn['run']}", ""]
    L += ["> **Not a foundry sign-off.** " + r["not_signoff"].split(". ", 1)[1], ""]
    v = r["verdict"]
    L += [f"**Result: {v['title']}.** {v['sub']}".rstrip(), ""]
    ov = ", ".join(f"{k}={val}" for k, val in (idn.get("overrides") or {}).items()) or "none"
    rows = [
        ["Design", f"{idn['design']}"],
        ["Run", f"{idn['run']}" + (f" · finished {idn['finished']}" if idn.get("finished") else "") + (f" · {idn['steps']} steps" if idn.get("steps") else "")],
        ["PDK", f"{idn['pdk'] or '—'}" + (f" · open_pdks {idn['pdk_commit'][:12]}" if idn.get("pdk_commit") else "") + (f" · library {idn['scl']}" if idn.get("scl") else "")],
        ["Flow", f"LibreLane {idn['librelane'] or '?'}, {idn['flow']} flow"],
        ["Engine", (idn.get("engine") or "—") + (f" `{idn['image']}`" if idn.get("image") else "")],
        ["Configuration", (f"`{idn['config_file']}`" if idn.get("config_file") else "—") + (f", SHA-256 `{idn['config_sha256']}`" if idn.get("config_sha256") else "") + f" · overrides: {ov}"],
        ["Command", f"`{idn['command']}`" if idn.get("command") else "—"],
        ["Layout", (f"`{idn['gds']['path']}`, {fmt_n(idn['gds']['size'])} bytes, SHA-256 `{idn['gds']['sha256']}`") if idn.get("gds") else "no final GDS"],
        ["Written by", f"LanEx {idn['lanex']} on {r['generated_at']}"],
    ]
    L += _table(["", ""], rows) + [""]
    L += ["## Checks", ""]
    L += _table(["Check", "Result", "Value", "Limit", "LibreLane", "Source"],
                [[c["name"], MARK.get(c["status"], "") + " " + c["result"], c["value"], c["limit"],
                  c["gate"] + (f" ({c['gate_note']})" if c["gate_note"] else ""), f"`{c['source']}`" if c["source"] else ""]
                 for c in r["checks"]]) + [""]
    t = r["timing"]
    L += ["## Timing", "", "### Clock", ""]
    if t.get("no_clock"):
        L += ["**Timing was never checked: no clock reached the registers.** OpenSTA found no timed path in any corner, "
              "so \"0 violations\" means nothing was checked.", ""]
    L += [(f"Period {t['clock_period']} ns ({t['mhz']} MHz)" if t.get("clock_period") else "No clock period in the config.")
          + (f" · uncertainty {abs(t['uncertainty']):.3f} ns" if _isnum(t.get("uncertainty")) else ""), ""]
    if t["corners"]:
        L += ["", "### Corners", ""]
        L += _table(["Corner", "Setup WNS ns", "Setup TNS ns", "Setup viol.", "Hold WNS ns", "Hold TNS ns", "Hold viol.", "reg→reg Fmax", "Fails the run on"],
                    [[c["corner"], fmt_ns(c["setup_ws"]), fmt_ns(c["setup_tns"]), fmt_n(c["setup_vio"]),
                      fmt_ns(c["hold_ws"]), fmt_ns(c["hold_tns"]), fmt_n(c["hold_vio"]),
                      (f"{c['fmax']:.1f} MHz" if _isnum(c.get("fmax")) else "—"),
                      " · ".join(x for x in (("setup" if c["setup_gate"] else ""), ("hold" if c["hold_gate"] else "")) if x) or "warns only"]
                     for c in t["corners"]]) + [""]
        L += ["reg→reg Fmax is OpenSTA's `report_clock_min_period` for register-to-register paths (input and output paths are not in it).", ""]
    for kind in ("setup", "hold"):
        p = t.get(f"worst_{kind}")
        if not p:
            continue
        L += [f"### Worst {kind} path · {p['corner']}", "",
              f"`{p['start']}` ({p['start_desc']}) → `{p['end']}` · slack **{fmt_ns(p['slack'])} ns** "
              f"({'MET' if p['met'] else 'VIOLATED'}) · one of {p['endpoints']} endpoints in this corner", "",
              f"- {'Needed by' if kind == 'setup' else 'Must arrive after'} {p['required']:.3f} ns = {_terms(p['required_terms'])}",
              f"- Arrives at {p['arrival']:.3f} ns = {_terms(p['arrival_terms'])}", ""]
        L += _table(["Pin", "Cell", "Edge", "Fanout", "Cap fF", "Slew ns", "Incr ns", "Arrival ns"],
                    [[x["what"], x.get("cell", ""), x.get("edge", ""), fmt_n(x.get("fanout")) if x.get("fanout") is not None else "",
                      (f"{x['cap_ff']:.2f}" if _isnum(x.get("cap_ff")) else ""), (f"{x['slew']:.3f}" if _isnum(x.get("slew")) else ""),
                      (f"{x['incr']:.3f}" if _isnum(x.get("incr")) else ""), (f"{x['time']:.3f}" if _isnum(x.get("time")) else "")]
                     for x in p["rows"]])
        L += ["", f"Source: `{p['source']['rel']}` line {p['line']}.", ""]
    ph = r["physical"]
    L += ["## Physical verification", "",
          f"- Design rules (DRC): Magic {fmt_n(ph['magic_drc'])} · KLayout {fmt_n(ph['klayout_drc'])}"
          + (f" ({ph['klayout_rules']} rules in the deck)" if ph.get("klayout_rules") else "") + f" · router {fmt_n(ph['route_drc'])}",
          f"- Layout vs netlist (LVS): {ph['lvs'] or '—'}"
          + (f" · devices {fmt_n(ph['lvs_counts']['layout'].get('devices'))} / {fmt_n(ph['lvs_counts']['netlist'].get('devices'))}"
             f" · nets {fmt_n(ph['lvs_counts']['layout'].get('nets'))} / {fmt_n(ph['lvs_counts']['netlist'].get('nets'))} (layout / netlist)"
             if ph.get("lvs_counts") else ""),
          f"- XOR: {fmt_n(ph['xor'])} differences" + (f" on {ph['xor_layers']} layers" if ph.get("xor_layers") else ""),
          f"- Antenna: {fmt_n(ph['antenna_nets'])} nets · {fmt_n(ph['antenna_pins'])} pins" + (f" · {fmt_n(ph['diodes'])} diodes" if ph.get("diodes") is not None else ""),
          f"- Power grid: {fmt_n(ph['pdn'])} unconnected" + ("".join(f" · worst IR drop on {x['net']} {x['pct']} % of supply" for x in ph["ir"][:2]) if ph.get("ir") else ""),
          ""]
    a = r["area"]
    bits = []
    if a.get("die_w"):
        bits.append(f"die {a['die_w']} × {a['die_h']} µm" + (f" ({a['die_area'] / 1e6:.4f} mm²)" if _isnum(a.get("die_area")) else ""))
    if _isnum(a.get("core_area")):
        bits.append(f"core {fmt_n(a['core_area'])} µm²")
    if _isnum(a.get("util")):
        bits.append(f"core utilisation {a['util'] * 100:.1f} %")
    if _isnum(a.get("logic_cells")):
        bits.append(f"{fmt_n(a['logic_cells'])} logic cells" + (f" ({fmt_n(a['flipflops'])} flip-flops)" if _isnum(a.get("flipflops")) else ""))
    if _isnum(a.get("wire")):
        bits.append(f"wire {fmt_n(a['wire'])} µm")
    L += ["## Area", "", " · ".join(bits) or "No area metrics.", ""]
    pw = r["power"]
    L += ["## Power (estimate)", ""]
    if pw["corners"]:
        L += _table(["Corner", "Internal", "Switching", "Leakage", "Total"],
                    [[x["corner"], _mw(x["internal"]), _mw(x["switching"]), _mw(x["leakage"]), _mw(x["total"])] for x in pw["corners"]]) + [""]
    L += [pw["note"], ""]
    wr = r["warnings"]
    L += ["## Warnings", "", wr["summary"], ""]
    if wr.get("waiver_error"):
        L += [f"> {wr['waiver_error']}", ""]
    if wr["groups"]:
        L += _table(["Code", "Tool", "Count", "LanEx's reading", "What it is", "Waiver"],
                    [[g["code"] + (" (new)" if g.get("new") else ""), g["tool"], fmt_n(g["count"]), g.get("worry") or "",
                      g["title"], (f"waived: {g['waiver']['reason']}, \"{g['waiver']['note']}\" ({g['waiver']['by'] or 'unnamed'}, {g['waiver']['date']})"
                                   if g.get("waived") and g.get("waiver") else "")]
                     for g in wr["groups"]]) + [""]
    if r["tools"]:
        L += ["## Tools", "", " · ".join(f"{x['tool']} {x['version']}" for x in r["tools"])
              + " (from this run's logs; OpenROAD and OpenSTA are named by the image above)", ""]
    L += ["## Files this report read", ""]
    L += _table(["File", "Bytes", "SHA-256"], [[f"`{f['path']}`", fmt_n(f["size"]), f"`{f['sha256']}`"] for f in r["files"]]) + [""]
    L += ["## What this report does not cover", ""] + [f"- {x}" for x in r["not_covered"]] + [""]
    return "\n".join(L)


# ---------------------------------------------------------------- HTML
_CSS = """
:root{--bg:#06080d;--bg2:#0a0e16;--surface:#0e1420;--surface2:#131b2a;--border:#1f2a3b;--border2:#2c3c55;
--text:#e6edf7;--muted:#8a9bb3;--accent:#4f8cff;--pass:#3ecf8e;--warn:#f0b429;--fail:#ff5d5d;
--poly:#d4544c;--li:#9a63d8;--m1:#4f8cff;--m2:#3ecf8e;--m3:#f0b429;--m4:#ff8a3d;--m5:#e05cb0}
*{box-sizing:border-box}html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Barlow,"Segoe UI",system-ui,sans-serif;
background-image:linear-gradient(rgba(31,42,59,.28) 1px,transparent 1px),linear-gradient(90deg,rgba(31,42,59,.28) 1px,transparent 1px);background-size:24px 24px}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 64px}
.hair{height:2px;background:linear-gradient(90deg,var(--poly),var(--li),var(--m1),var(--m2),var(--m3),var(--m4),var(--m5));opacity:.55;margin:10px 0 22px}
.eyebrow,th,.k{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{font-weight:700;font-size:34px;line-height:1.15;margin:4px 0 0}h2{font-weight:700;font-size:22px;margin:34px 0 10px}
h3{font-weight:600;font-size:17px;margin:22px 0 8px}
.disc{border:1px solid var(--warn);border-left-width:4px;background:rgba(240,180,41,.08);padding:12px 16px;border-radius:6px;margin:0 0 18px}
.verdict{display:flex;gap:14px;align-items:flex-start;background:linear-gradient(180deg,var(--surface2),var(--surface));border:1px solid var(--border2);
border-top:2px solid var(--m1);border-radius:8px;padding:16px 18px;box-shadow:0 0 0 1px rgba(79,140,255,.05),0 8px 28px rgba(0,0,0,.35)}
.via{width:14px;height:14px;flex:0 0 14px;margin-top:8px;border:2px solid currentColor;background:currentColor;opacity:.9}
.t-pass{color:var(--pass)}.t-warn{color:var(--warn)}.t-fail{color:var(--fail)}
.verdict b{font-size:22px;color:var(--text)}.verdict p{margin:4px 0 0;color:var(--muted)}
table{border-collapse:collapse;width:100%;margin:6px 0 4px;background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}th{background:var(--bg2);font-size:10.5px}
td.n,th.n{text-align:right;font-family:"JetBrains Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;white-space:nowrap}
code,.mono{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12.5px}
.src{color:var(--muted);font-family:"JetBrains Mono",ui-monospace,monospace;font-size:11.5px;word-break:break-all}
.idt td:first-child{width:150px;color:var(--muted);font-family:"JetBrains Mono",monospace;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase}
.idt code{word-break:break-all}.bad{color:var(--fail)}.wn{color:var(--warn)}.ok{color:var(--pass)}.mut{color:var(--muted)}
.fold td{color:var(--muted);font-style:italic}.chip{display:inline-block;padding:1px 7px;border:1px solid var(--border2);border-radius:4px;font-size:11px;font-family:"JetBrains Mono",monospace}
ul{margin:6px 0;padding-left:20px}.foot{margin-top:40px;color:var(--muted);font-size:13px}
.chipimg{float:right;margin:0 0 12px 18px;width:220px;border:1px solid var(--border2);border-radius:6px;box-shadow:0 0 24px rgba(79,140,255,.18)}
@media print{:root{--bg:#fff;--bg2:#f2f4f8;--surface:#fff;--surface2:#fff;--border:#d5dbe5;--border2:#b9c3d3;--text:#111;--muted:#5a6677}
body{background:#fff;background-image:none;font-size:11pt}.verdict{box-shadow:none}tr{break-inside:avoid}h2{break-after:avoid}.chipimg{box-shadow:none}}
"""


def _fonts_css(fonts_dir: Optional[Path]) -> str:
    if not fonts_dir:
        return ""
    out = []
    for fam, wt, name in (("Barlow", "400", "barlow-400.woff2"), ("Barlow", "600", "barlow-600.woff2"),
                          ("Barlow", "700", "barlow-700.woff2"), ("JetBrains Mono", "100 800", "jetbrainsmono.woff2")):
        p = fonts_dir / name
        try:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        except OSError:
            continue
        out.append(f"@font-face{{font-family:\"{fam}\";font-weight:{wt};font-style:normal;"
                   f"src:url(data:font/woff2;base64,{b64}) format(\"woff2\")}}")
    return "\n".join(out)


def _e(s: Any) -> str:
    return _html.escape("" if s is None else str(s))


def _htable(head: List[Tuple[str, bool]], rows: List[List[Tuple[Any, str]]], cls: str = "") -> str:
    h = "".join(f"<th class='{'n' if num else ''}'>{_e(t)}</th>" for t, num in head)
    body = "".join("<tr>" + "".join(f"<td class='{c}'>{v}</td>" for v, c in r) + "</tr>" for r in rows)
    return f"<table class='{cls}'><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def _slack_cls(v: Any) -> str:
    return "n bad" if _isnum(v) and v < 0 else "n"


def render_html(r: Dict[str, Any], *, fonts_dir: Optional[Path] = None, chip_png: Optional[bytes] = None) -> str:
    idn = r["identity"]
    v = r["verdict"]
    tone = {"pass": "t-pass", "fail": "t-fail"}.get(v.get("tone"), "t-warn")
    title = f"Sign-off summary: {idn['design'] or 'design'}, run {idn['run']}"
    H: List[str] = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>",
                    f"<title>{_e(title)}</title><style>{_fonts_css(fonts_dir)}{_CSS}</style></head><body><div class='wrap'>"]
    H.append(f"<div class='eyebrow'>LanEx sign-off summary · {_e(idn['pdk'] or '')}</div><h1>{_e(idn['design'] or 'design')} · run {_e(idn['run'])}</h1><div class='hair'></div>")
    H.append(f"<div class='disc'><b>Not a foundry sign-off.</b> {_e(r['not_signoff'].split('. ', 1)[1])}</div>")
    if chip_png:
        H.append("<img class='chipimg' alt='The final layout' src='data:image/png;base64," + base64.b64encode(chip_png).decode("ascii") + "'>")
    H.append(f"<div class='verdict {tone}'><span class='via'></span><div><b>{_e(v['title'])}</b><p>{_e(v.get('sub') or '')}</p></div></div>")
    ov = ", ".join(f"{k}={val}" for k, val in (idn.get("overrides") or {}).items()) or "none"
    idrows = [
        ("Design", _e(idn["design"])),
        ("Run", _e(idn["run"]) + (f" · finished {_e(idn['finished'])}" if idn.get("finished") else "") + (f" · {_e(idn['steps'])} steps" if idn.get("steps") else "")),
        ("PDK", _e(idn["pdk"] or "—") + (f" · open_pdks <code>{_e(idn['pdk_commit'][:12])}</code>" if idn.get("pdk_commit") else "") + (f" · library {_e(idn['scl'])}" if idn.get("scl") else "")),
        ("Flow", f"LibreLane {_e(idn['librelane'] or '?')}, {_e(idn['flow'])} flow"),
        ("Engine", _e(idn.get("engine") or "—") + (f" <code>{_e(idn['image'])}</code>" if idn.get("image") else "")),
        ("Configuration", (f"<code>{_e(idn['config_file'])}</code>" if idn.get("config_file") else "—") + (f", SHA-256 <code>{_e(idn['config_sha256'])}</code>" if idn.get("config_sha256") else "") + f" · overrides: {_e(ov)}"),
        ("Command", f"<code>{_e(idn['command'])}</code>" if idn.get("command") else "—"),
        ("Layout", (f"<code>{_e(idn['gds']['path'])}</code>, {fmt_n(idn['gds']['size'])} bytes, SHA-256 <code>{_e(idn['gds']['sha256'])}</code>") if idn.get("gds") else "no final GDS"),
        ("Written by", f"LanEx {_e(idn['lanex'])} on {_e(r['generated_at'])}"),
    ]
    H.append("<table class='idt'><tbody>" + "".join(f"<tr><td>{k}</td><td>{val}</td></tr>" for k, val in idrows) + "</tbody></table>")
    sc = {"pass": "ok", "fail": "bad", "warn": "wn"}
    H.append("<h2>Checks</h2>" + _htable(
        [("Check", False), ("Result", False), ("Value", True), ("Limit", True), ("LibreLane", False), ("Source", False)],
        [[(_e(c["name"]), ""), (_e(c["result"]), sc.get(c["status"], "mut")), (_e(c["value"]), "n"), (_e(c["limit"]), "n"),
          (_e(c["gate"]) + (f"<br><span class='src'>{_e(c['gate_note'])}</span>" if c["gate_note"] else ""), ""),
          (f"<span class='src'>{_e(c['source'])}</span>", "")] for c in r["checks"]]))
    t = r["timing"]
    H.append("<h2>Timing</h2>")
    if t.get("no_clock"):
        H.append("<div class='disc'><b>Timing was never checked: no clock reached the registers.</b> OpenSTA found no timed path in any corner, so “0 violations” means nothing was checked.</div>")
    H.append("<p>" + (f"Clock period <b class='mono'>{_e(t['clock_period'])} ns</b> ({_e(t['mhz'])} MHz)" if t.get("clock_period") else "No clock period in the config.")
             + (f" · uncertainty <span class='mono'>{abs(t['uncertainty']):.3f} ns</span>" if _isnum(t.get("uncertainty")) else "") + "</p>")
    if t["corners"]:
        H.append(_htable(
            [("Corner", False), ("Setup WNS ns", True), ("Setup TNS ns", True), ("Viol.", True), ("Hold WNS ns", True), ("Hold TNS ns", True),
             ("Viol.", True), ("reg→reg Fmax", True), ("Fails the run on", False)],
            [[(f"<code>{_e(c['corner'])}</code>", ""), (fmt_ns(c["setup_ws"]), _slack_cls(c["setup_ws"])), (fmt_ns(c["setup_tns"]), _slack_cls(c["setup_tns"])),
              (fmt_n(c["setup_vio"]), "n"), (fmt_ns(c["hold_ws"]), _slack_cls(c["hold_ws"])), (fmt_ns(c["hold_tns"]), _slack_cls(c["hold_tns"])),
              (fmt_n(c["hold_vio"]), "n"), ((f"{c['fmax']:.1f} MHz" if _isnum(c.get("fmax")) else "—"), "n"),
              (" · ".join(x for x in (("setup" if c["setup_gate"] else ""), ("hold" if c["hold_gate"] else "")) if x) or "<span class='mut'>warns only</span>", "")]
             for c in t["corners"]]))
        H.append("<p class='mut'>reg→reg Fmax is OpenSTA’s <code>report_clock_min_period</code> for register-to-register paths; input and output paths are not in it.</p>")
    for kind in ("setup", "hold"):
        p = t.get(f"worst_{kind}")
        if not p:
            continue
        H.append(f"<h3>Worst {kind} path · <code>{_e(p['corner'])}</code></h3>")
        H.append(f"<p><code>{_e(p['start'])}</code> <span class='mut'>({_e(p['start_desc'])})</span> → <code>{_e(p['end'])}</code> · slack "
                 f"<b class='mono {'bad' if p['slack'] < 0 else 'ok'}'>{fmt_ns(p['slack'])} ns</b> · one of {p['endpoints']} endpoints in this corner</p>")
        H.append(f"<p class='mono'>{'Needed by' if kind == 'setup' else 'Must arrive after'} {p['required']:.3f} = {_e(_terms(p['required_terms']))}<br>"
                 f"Arrives at {p['arrival']:.3f} = {_e(_terms(p['arrival_terms']))}</p>")
        H.append(_htable(
            [("Pin", False), ("Cell", False), ("Edge", False), ("Fanout", True), ("Cap fF", True), ("Slew ns", True), ("Incr ns", True), ("Arrival ns", True)],
            [[(f"<code>{_e(x['what'])}</code>", ""), (_e(x.get("cell", "")), "mono"), (_e(x.get("edge", "")), ""),
              (fmt_n(x.get("fanout")) if x.get("fanout") is not None else "", "n"),
              (f"{x['cap_ff']:.2f}" if _isnum(x.get("cap_ff")) else "", "n"), (f"{x['slew']:.3f}" if _isnum(x.get("slew")) else "", "n"),
              (f"{x['incr']:.3f}" if _isnum(x.get("incr")) else "", "n"), (f"{x['time']:.3f}" if _isnum(x.get("time")) else "", "n")]
             for x in p["rows"]]))
        H.append(f"<p class='src'>Source: {_e(p['source']['rel'])} line {_e(p['line'])}</p>")
    ph = r["physical"]
    items = [
        f"Design rules (DRC): Magic <b class='mono'>{fmt_n(ph['magic_drc'])}</b> · KLayout <b class='mono'>{fmt_n(ph['klayout_drc'])}</b>"
        + (f" ({_e(ph['klayout_rules'])} rules in the deck)" if ph.get("klayout_rules") else "") + f" · router <b class='mono'>{fmt_n(ph['route_drc'])}</b>",
        f"Layout vs netlist (LVS): <b>{_e(ph['lvs'] or '—')}</b>",
        f"XOR: <b class='mono'>{fmt_n(ph['xor'])}</b> differences" + (f" on {_e(ph['xor_layers'])} layers" if ph.get("xor_layers") else ""),
        f"Antenna: <b class='mono'>{fmt_n(ph['antenna_nets'])}</b> nets · <b class='mono'>{fmt_n(ph['antenna_pins'])}</b> pins" + (f" · {fmt_n(ph['diodes'])} diodes" if ph.get("diodes") is not None else ""),
        f"Power grid: <b class='mono'>{fmt_n(ph['pdn'])}</b> unconnected" + "".join(f" · worst IR drop on {_e(x['net'])} {_e(x['pct'])} % of supply" for x in (ph.get("ir") or [])[:2]),
    ]
    H.append("<h2>Physical verification</h2><ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
    a = r["area"]
    bits = []
    if a.get("die_w"):
        bits.append(f"die <b class='mono'>{a['die_w']} × {a['die_h']} µm</b>")
    if _isnum(a.get("util")):
        bits.append(f"core utilisation <b class='mono'>{a['util'] * 100:.1f} %</b>")
    if _isnum(a.get("logic_cells")):
        bits.append(f"<b class='mono'>{fmt_n(a['logic_cells'])}</b> logic cells" + (f" ({fmt_n(a['flipflops'])} flip-flops)" if _isnum(a.get("flipflops")) else ""))
    if _isnum(a.get("wire")):
        bits.append(f"wire <b class='mono'>{fmt_n(a['wire'])} µm</b>")
    H.append("<h2>Area</h2><p>" + (" · ".join(bits) or "No area metrics.") + "</p>")
    pw = r["power"]
    H.append("<h2>Power (estimate)</h2>")
    if pw["corners"]:
        H.append(_htable([("Corner", False), ("Internal", True), ("Switching", True), ("Leakage", True), ("Total", True)],
                         [[(f"<code>{_e(x['corner'])}</code>", ""), (_mw(x["internal"]), "n"), (_mw(x["switching"]), "n"),
                           (_mw(x["leakage"]), "n"), (_mw(x["total"]), "n")] for x in pw["corners"]]))
    H.append(f"<p class='mut'>{_e(pw['note'])}</p>")
    wr = r["warnings"]
    H.append(f"<h2>Warnings</h2><p>{_e(wr['summary'])}</p>")
    if wr.get("waiver_error"):
        H.append(f"<div class='disc'>{_e(wr['waiver_error'])}</div>")
    if wr["groups"]:
        H.append(_htable([("Code", False), ("Tool", False), ("Count", True), ("LanEx's reading", False), ("What it is", False), ("Waiver", False)],
                         [[(f"<code>{_e(g['code'])}</code>" + (" <span class='chip wn'>new</span>" if g.get("new") else ""), ""), (_e(g["tool"]), ""),
                           (fmt_n(g["count"]), "n"), (_e(g.get("worry") or ""), ""), (_e(g["title"]), ""),
                           ((f"waived: {_e(g['waiver']['reason'])}, “{_e(g['waiver']['note'])}” ({_e(g['waiver']['by'] or 'unnamed')}, {_e(g['waiver']['date'])})"
                             if g.get("waived") and g.get("waiver") else ""), "mut")] for g in wr["groups"]]))
    if r["tools"]:
        H.append("<h2>Tools</h2><p>" + " · ".join(f"{_e(x['tool'])} <span class='mono'>{_e(x['version'])}</span>" for x in r["tools"])
                 + " <span class='mut'>(from this run’s logs; OpenROAD and OpenSTA are named by the image above)</span></p>")
    H.append("<h2>Files this report read</h2>" + _htable(
        [("File", False), ("Bytes", True), ("SHA-256", False)],
        [[(f"<code>{_e(f['path'])}</code>", ""), (fmt_n(f["size"]), "n"), (f"<span class='src'>{_e(f['sha256'])}</span>", "")] for f in r["files"]]))
    H.append("<h2>What this report does not cover</h2><ul>" + "".join(f"<li>{_e(x)}</li>" for x in r["not_covered"]) + "</ul>")
    H.append(f"<p class='foot'>Written by LanEx {_e(idn['lanex'])} on {_e(r['generated_at'])} from the run folder’s own files. "
             "LanEx is not affiliated with or endorsed by LibreLane.</p></div></body></html>")
    return "\n".join(H)


def write(run_dir: str | Path, *, design_dir: Optional[str | Path] = None, base_dir: Optional[str | Path] = None,
          verdict: Optional[Dict[str, Any]] = None, fonts_dir: Optional[Path] = None,
          chip_png: Optional[bytes] = None) -> Dict[str, Any]:
    """Build the report and write ``report.json``, ``report.md``, ``report.html``
    and ``SHA256SUMS`` to ``<run>/lanex-report/``: ``{dir, files, generated_at}``."""
    run_dir = Path(run_dir).resolve()
    rep = build(run_dir, design_dir=design_dir, base_dir=base_dir, verdict=verdict)
    out = run_dir / REPORT_DIR
    out.mkdir(exist_ok=True)
    texts = {"report.json": json.dumps(rep, indent=1, ensure_ascii=False, default=str) + "\n",
             "report.md": render_md(rep),
             "report.html": render_html(rep, fonts_dir=fonts_dir, chip_png=chip_png)}
    sums = []
    for name, text in texts.items():
        data = text.encode("utf-8")
        tmp = out / (name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(out / name)
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return {"dir": _rel(run_dir, out), "files": {k.split(".")[1]: f"{REPORT_DIR}/{k}" for k in texts},
            "generated_at": rep["generated_at"], "verdict": rep["verdict"]["title"],
            "sizes": {k: len(v.encode('utf-8')) for k, v in texts.items()}}
