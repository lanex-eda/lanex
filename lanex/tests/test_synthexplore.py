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
"""LibreLane's SynthesisExploration read from its folder (synthexplore.py):
the folder layout and numbers as a real 3.0.4 run writes them on spm, and
the all-failed case LibreLane itself crashes on."""
from __future__ import annotations

import json
from pathlib import Path

from lanex.controller import history, synthexplore

# spm, observed: AREA 0-2 the same netlist, DELAY 0-3 the same.
SPM = {"area-0": (222, 3294.4096, 5.3297), "area-1": (222, 3294.4096, 5.3297), "area-2": (222, 3294.4096, 5.3297),
       "area-3": (410, 5054.2, 2.57), "delay-0": (346, 4031.37, 4.28), "delay-1": (346, 4031.37, 4.28),
       "delay-2": (346, 4031.37, 4.28), "delay-3": (346, 4031.37, 4.28), "delay-4": (224, 4212.0, 5.30)}


def _explore(root: Path, *, fail: bool = False) -> Path:
    run = root / "runs" / "synthx-1"
    run.mkdir(parents=True)
    (run / "resolved.json").write_text(json.dumps({"meta": {"flow": None, "librelane_version": "3.0.4"}}))
    for sl, (gates, area, ws) in SPM.items():
        syn = run / f"1-synthesis-{sl}"
        syn.mkdir()
        if fail:
            (syn / f"synthesis-{sl}.log").write_text("…\nERROR: Module `no_such_top' not found!\n")
            continue
        (syn / "state_out.json").write_text("{}")
        (run / f"1-sdc-{sl}").mkdir()
        (run / f"1-sdc-{sl}" / "state_out.json").write_text("{}")
        (run / f"1-sta-{sl}").mkdir()
        (run / f"1-sta-{sl}" / "state_out.json").write_text(json.dumps({"metrics": {
            "design__instance__count": gates, "design__instance__area": area, "timing__setup__ws": ws,
            "timing__setup__tns": 0, "timing__setup_r2r__ws": "Infinity", "timing__hold__ws": 0.06, "power__total": 0.00098}}))
    return run


def test_reads_every_recipe_and_folds_identical_netlists(tmp_path: Path) -> None:
    run = _explore(tmp_path)
    assert synthexplore.is_exploration(run)
    r = synthexplore.read(run)
    assert r["ok_count"] == 9 and not r["all_failed"]
    groups = [row["strategies"] for row in r["rows"]]
    assert ["AREA 0", "AREA 1", "AREA 2"] in groups
    assert ["DELAY 0", "DELAY 1", "DELAY 2", "DELAY 3"] in groups
    assert len(r["rows"]) == 4
    assert r["best_area"] == "AREA 0" and r["best_slack"] == "AREA 0"
    assert r["recipes"][0]["metrics"]["timing__setup_r2r__ws"] == float("inf")


def test_all_failed_is_read_not_crashed_on(tmp_path: Path) -> None:
    run = _explore(tmp_path, fail=True)
    r = synthexplore.read(run)
    assert r["all_failed"] and r["ok_count"] == 0
    assert r["common_failure"]["step"] == "synthesis"
    assert "no_such_top" in r["common_failure"]["reason"]
    assert len(r["rows"]) == 1 and len(r["rows"][0]["strategies"]) == 9
    v = synthexplore.verdict(run)
    assert v["state"] == "failed" and "No recipe synthesised" in v["label"]


def test_the_runs_list_names_it_and_shows_no_final_numbers(tmp_path: Path) -> None:
    _explore(tmp_path)
    row = history.list_runs(tmp_path)[0]
    assert row["flow"] == "SynthesisExploration"
    assert row["verdict"]["state"] == "passed" and row["verdict"]["exploration"] is True
    assert row["key_metrics"] == {}


def test_verify_and_the_status_bar_say_what_the_runs_list_says(tmp_path: Path) -> None:
    """The status bar's end-of-run words come from Verify's run verdict
    (runend.js withVerdict), which read an exploration as "Stopped after
    step 1" while Runs said "Explored 9 synthesis recipes"."""
    from lanex.controller import verify
    run = _explore(tmp_path)
    row = history.list_runs(tmp_path)[0]
    v = verify.run_verdict(run)
    assert (v["state"], v["label"]) == (row["verdict"]["state"], row["verdict"]["label"])
    assert v["exploration"] is True and "Stopped" not in v["label"]
    assert verify.verify_report(run)["run_verdict"]["label"] == row["verdict"]["label"]
    bad = _explore(tmp_path / "b", fail=True)
    assert verify.run_verdict(bad)["state"] == "failed"
