# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Two review findings in the knowledge base (problems.py): a code alone no
longer picks a card whose pattern doesn't match (STA-0572 is OpenSTA's check
for every number argument), and the DRC card counts by LibreLane's metric,
says when its places come from a sample, and reads a big report off the
request thread."""
from __future__ import annotations

import json
import time
from pathlib import Path

from lanex.controller import layoutview, problems


def test_sta_0572_outside_the_floorplan_is_not_the_die_margin_card():
    c = problems.explain_error("[ERROR STA-0572] transition '-0.1' is not a positive float.", {})
    assert c["kb"] == "sta-0572", c["kb"]
    assert c["title"] == "A timing value has to be above zero: transition -0.1"
    assert "MAX_TRANSITION_CONSTRAINT" in c["plain"]
    assert any(f.get("go", {}).get("setting") == "MAX_TRANSITION_CONSTRAINT" for f in c["fixes"])


def test_sta_0572_on_core_area_is_still_the_margin_card():
    c = problems.explain_error("[ERROR STA-0572] -core_area '10 10 5 5' is not a positive float", {"DIE_AREA": [0, 0, 8, 8]})
    assert c["kb"] == "ifp-margins"


def test_sta_0572_with_an_own_sdc_points_at_the_file():
    c = problems.explain_error("[ERROR STA-0572] -period '0' is not a positive float.",
                               {"PNR_SDC_FILE": "/work/p/src/top.sdc", "DESIGN_DIR": "/work/p"})
    assert c["kb"] == "sta-0572" and "your SDC (top.sdc)" in c["plain"]
    assert c["fixes"][0]["go"] == {"tab": "ide", "rel": "src/top.sdc", "line": 1}


def test_no_rule_with_a_pattern_is_picked_by_its_code_alone():
    for rule in problems.KB:
        if rule.get("rx") is not None and rule.get("code"):
            ev = {"text": "[ERROR %s] something else entirely" % rule["code"], "cfg": {}, "step": None, "log": "", "outcome": ""}
            assert problems._match(rule, ev, rule["code"]) is None, rule["id"]


def _drc_run(tmp: Path, items: int, metric: int) -> Path:
    run = tmp / "proj" / "runs" / "R"
    rep = run / "60-magic-drc" / "reports"
    rep.mkdir(parents=True)
    body = "".join("<item><category>'m1.%d'</category><values><value>box: (%d,0;%d,1)</value></values></item>"
                   % (i % 2, i * 50, i * 50 + 1) for i in range(items))
    (rep / "drc.magic.lyrdb").write_text("<report-database><categories><category><name>m1.0</name></category>"
                                         "<category><name>m1.1</name></category></categories><items>" + body +
                                         "</items></report-database>", encoding="utf-8")
    (run / "final").mkdir()
    (run / "final" / "metrics.json").write_text(json.dumps({"magic__drc_error__count": metric,
                                                            "design__die__bbox": "0 0 500 500"}))
    return run


def _rep(value):
    return {"checks": [{"id": "magic_drc", "status": "fail", "value": value, "name": "Magic DRC"}]}


def test_the_drc_count_is_the_metric_and_a_sample_is_said(tmp_path, monkeypatch):
    run = _drc_run(tmp_path, items=6, metric=9)
    monkeypatch.setattr(layoutview, "_MAX_MARKERS", 4)
    problems._DRC_CACHE.clear()
    c = problems.drc_problems(run, _rep(9))[0]
    src = c["diag"]["sources"][0]
    assert src["total"] == 9 and src["in_file"] == 6 and src["listed"] == 4 and src["sampled"] is True
    assert c["title"].startswith("9 DRC violations")
    assert "The places are from the first 4 of the 6 markers in Magic DRC's report" in c["plain"]
    assert "the report file lists 6 markers" in c["plain"]
    assert not c["pending"]


def test_a_big_drc_report_is_read_off_the_request_thread(tmp_path, monkeypatch):
    run = _drc_run(tmp_path, items=3, metric=3)
    problems._DRC_CACHE.clear()
    real = problems.drc_groups

    def slow(rd):
        time.sleep(1.0)
        return real(rd)
    monkeypatch.setattr(problems, "drc_groups", slow)
    t0 = time.monotonic()
    c = problems.drc_problems(run, _rep(3))[0]
    assert time.monotonic() - t0 < 0.8, "the request waited for the report"
    assert c["pending"] and c["diag"]["pending"] and c["title"] == "3 DRC violations (Magic DRC)"
    assert "LanEx is reading the report" in c["plain"]
    time.sleep(1.3)
    c2 = problems.drc_problems(run, _rep(3))[0]
    assert not c2["pending"] and c2["diag"]["sources"][0]["rules"]


def test_problems_with_a_pending_drc_report_are_not_remembered(tmp_path, monkeypatch):
    calls = []

    def fake(rd):
        calls.append(1)
        return {"problems": [{"pending": True}], "pending": True}
    monkeypatch.setattr(problems, "_run_problems", fake)
    problems._CACHE.clear()
    problems.run_problems(tmp_path)
    problems.run_problems(tmp_path)
    assert len(calls) == 2
