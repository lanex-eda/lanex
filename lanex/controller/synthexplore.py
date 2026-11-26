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
"""LibreLane's ``SynthesisExploration`` flow, read from its run folder.

The flow (``librelane/flows/synth_explore.py``) synthesises the design with
each of LibreLane's nine ABC recipes (``SYNTH_STRATEGY`` AREA 0-3, DELAY 0-4),
checks the SDC and times each netlist before layout (ideal clocks, no wires),
all in one run folder::

    1-synthesis-<slug>/  1-sdc-<slug>/  1-sta-<slug>/     slug: area-0 ... delay-4

It prints a table to the console and ``summary.rpt`` and picks nothing. When
every recipe fails it crashes (``min()`` of nothing, exit code 1) and writes no
``summary.rpt``. So LanEx never parses that table: it reads each recipe's
``1-sta-<slug>/state_out.json`` itself, and a recipe without one failed at the
first of its three steps that has no ``state_out.json`` (a missing folder: its
input failed first). Recipes that gave the same netlist (same gates, area and
slack) fold into one row.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

STRATEGIES = ["AREA 0", "AREA 1", "AREA 2", "AREA 3", "DELAY 0", "DELAY 1", "DELAY 2", "DELAY 3", "DELAY 4"]
STEPS = [("synthesis", "Synthesis"), ("sdc", "SDC check"), ("sta", "Timing before layout")]
METRICS = ["design__instance__count", "design__instance__area", "timing__setup__ws", "timing__setup__tns",
           "timing__setup_r2r__ws", "timing__hold__ws", "power__total"]


def slug(strategy: str) -> str:
    return strategy.lower().replace(" ", "-")


def is_exploration(run_dir: str | Path) -> bool:
    """A SynthesisExploration run: it has a ``1-synthesis-area-0`` folder."""
    return (Path(run_dir) / "1-synthesis-area-0").is_dir()


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str) and v in ("Infinity", "inf"):
            return math.inf
        return None
    return float(v)


def _last_error(step_dir: Path) -> str:
    """The last ERROR line of a step's log (its own words, paths shortened)."""
    best = ""
    try:
        logs = sorted(step_dir.glob("*.log"))
    except OSError:
        return ""
    for log in logs:
        try:
            with log.open("r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(2_000_000)
        except OSError:
            continue
        for line in text.splitlines():
            if re.search(r"\bERROR\b", line):
                best = line.strip()
    return re.sub(r"/[^\s'`\"]+/", "…/", best)[:300]


def read(run_dir: str | Path) -> Dict[str, Any]:
    """Each recipe's outcome and numbers, folded, with the best by area and
    by setup slack among the recipes that synthesised."""
    run = Path(run_dir)
    recipes: List[Dict[str, Any]] = []
    for s in STRATEGIES:
        sl = slug(s)
        sta = run / f"1-sta-{sl}" / "state_out.json"
        rec: Dict[str, Any] = {"strategy": s}
        if sta.is_file():
            try:
                m = (json.loads(sta.read_text(encoding="utf-8", errors="replace")) or {}).get("metrics") or {}
            except (OSError, ValueError):
                m = {}
            rec.update(ok=True, metrics={k: _num(m.get(k)) for k in METRICS})
        else:
            rec["ok"] = False
            for step, words in STEPS:
                d = run / f"1-{step}-{sl}"
                if not d.is_dir():
                    rec.update(step=step, step_words=words, reason="didn't start: an earlier step of this recipe failed")
                    break
                if not (d / "state_out.json").is_file():
                    rec.update(step=step, step_words=words, reason=_last_error(d))
                    break
            else:
                rec.update(step="sta", step_words="Timing before layout", reason="")
        recipes.append(rec)
    ok = [r for r in recipes if r["ok"]]
    # Fold recipes that gave the same netlist into one row.
    rows: List[Dict[str, Any]] = []
    for r in recipes:
        if r["ok"]:
            m = r["metrics"]
            sig = (m.get("design__instance__count"), _round(m.get("design__instance__area")), _round(m.get("timing__setup__ws")))
            hit = next((x for x in rows if x["ok"] and x["_sig"] == sig), None)
        else:
            sig = (r.get("step"), r.get("reason"))
            hit = next((x for x in rows if not x["ok"] and x["_sig"] == sig), None)
        if hit:
            hit["strategies"].append(r["strategy"])
            continue
        rows.append({**{k: v for k, v in r.items() if k != "strategy"}, "strategies": [r["strategy"]], "_sig": sig})
    for x in rows:
        x.pop("_sig", None)

    def best(key: str, hib: bool) -> Optional[str]:
        cands = [r for r in ok if r["metrics"].get(key) is not None and math.isfinite(r["metrics"][key])]
        if not cands:
            return None
        pick = (max if hib else min)(cands, key=lambda r: r["metrics"][key])
        return pick["strategy"]

    fails = [r for r in recipes if not r["ok"]]
    common = None
    if fails and len({(r.get("step"), r.get("reason")) for r in fails}) == 1:
        common = {"step": fails[0].get("step"), "step_words": fails[0].get("step_words"), "reason": fails[0].get("reason")}
    return {
        "run_dir": str(run), "tag": run.name,
        "recipes": recipes, "rows": rows,
        "ok_count": len(ok), "count": len(recipes),
        "all_failed": not ok,
        "common_failure": common if not ok else None,
        "best_area": best("design__instance__area", False),
        "best_slack": best("timing__setup__ws", True),
        "summary_rpt": (run / "summary.rpt").is_file(),
        "note": "Numbers before placement and wiring (ideal clocks): they rank the recipes, they don't predict the final chip.",
    }


def _round(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) and math.isfinite(v) else v


def verdict(run_dir: str | Path) -> Dict[str, Any]:
    """The Runs verdict of an exploration run (it has no final/ folder)."""
    try:
        r = read(run_dir)
    except Exception:
        return {"state": "failed", "label": "Synthesis recipes: couldn't be read"}
    if r["all_failed"]:
        c = r["common_failure"] or {}
        return {"state": "failed", "stage": "synthesis", "exploration": True,
                "label": "No recipe synthesised" + (": " + c["reason"] if c.get("reason") else "")}
    if r["ok_count"] < r["count"]:
        return {"state": "warning", "exploration": True,
                "label": f"Explored {r['count']} synthesis recipes, {r['count'] - r['ok_count']} failed"}
    return {"state": "passed", "exploration": True, "label": f"Explored {r['count']} synthesis recipes"}

