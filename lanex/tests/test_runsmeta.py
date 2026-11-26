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
"""The Runs page's store (controller/runsmeta.py): tags, archive, the one
baseline and saved views per project, bulk delete into a trash with Undo,
free columns, "changed vs baseline" without path noise, the Excel CSV."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from lanex.controller import history, runsmeta


def _run(design: Path, tag: str, *, cfg=None, metrics=None, parent=None) -> Path:
    rd = design / "runs" / tag
    (rd / "final").mkdir(parents=True)
    (rd / "1-yosys-synthesis").mkdir()
    (rd / "resolved.json").write_text(json.dumps(cfg if cfg is not None else {
        "DESIGN_DIR": str(design), "PDK_ROOT": "/pdk/root", "CLOCK_PERIOD": 10,
        "VERILOG_FILES": [str(design / "src" / "a.v")], "RUN_TAG": tag, "meta": {"librelane_version": "3.0.4"}}))
    (rd / "final" / "metrics.json").write_text(json.dumps(metrics if metrics is not None else {
        "timing__setup__ws": 1.5, "timing__setup__ws__corner:nom_tt": 1.6, "design__die__area": 8000}))
    if parent:
        (rd / "gui-run.json").write_text(json.dumps({"branched_from": {"tag": parent}}))
    return rd


# ------------------------------------------------------------------ tags
def test_tags_are_cleaned_and_deduplicated_case_blind(tmp_path: Path) -> None:
    assert runsmeta.clean_tags(["timing", "Timing", " good  area ", "", "a" * 33, "=cmd", 7, "x/y-1.2"]) == \
        ["timing", "good area", "x/y-1.2"]


def test_tags_round_trip_and_show_in_the_summary(tmp_path: Path) -> None:
    rd = _run(tmp_path, "r1")
    assert runsmeta.write_tags(rd, ["timing", "keep"]) == ["timing", "keep"]
    assert runsmeta.read_tags(rd) == ["timing", "keep"]
    rows = history.list_runs(tmp_path)
    assert rows[0]["tags"] == ["timing", "keep"] and rows[0]["archived"] is False
    runsmeta.write_tags(rd, [])
    assert not (rd / runsmeta.TAGS_FILE).exists()


def test_archive_marker_joins_both_signatures(tmp_path: Path) -> None:
    rd = _run(tmp_path, "r1")
    st = os.stat(rd)
    fast, full = history._fast_signature(rd, st), history._run_signature(rd)
    runsmeta.set_archived(rd, True)
    st2 = os.stat(rd)
    assert history._fast_signature(rd, st2) != fast
    assert history._run_signature(rd) != full
    assert history.list_runs(tmp_path)[0]["archived"] is True
    runsmeta.set_archived(rd, False)
    assert history.list_runs(tmp_path)[0]["archived"] is False


# ------------------------------------------------------------------ project file
def test_meta_baseline_and_views(tmp_path: Path) -> None:
    assert runsmeta.read_meta(tmp_path)["baseline"] is None
    doc = runsmeta.write_meta(tmp_path, baseline="r1", views=[
        {"id": "v1", "name": "Timing hunt", "filters": {"status": "passed"}, "columns": ["wns"]},
        {"id": "v1", "name": "dup"}, {"id": "bad id!", "name": "x"}, {"name": "no id"}])
    assert doc["baseline"] == "r1"
    assert [v["name"] for v in doc["views"]] == ["Timing hunt"]
    again = runsmeta.read_meta(tmp_path)
    assert again["views"][0]["filters"] == {"status": "passed"}
    assert runsmeta.write_meta(tmp_path, baseline="../etc")["baseline"] is None
    assert runsmeta.write_meta(tmp_path, baseline=None)["baseline"] is None
    # The views survive a baseline change (a patch, not a rewrite).
    assert runsmeta.read_meta(tmp_path)["views"][0]["id"] == "v1"


def test_damaged_meta_is_kept_as_bak(tmp_path: Path) -> None:
    (tmp_path / runsmeta.META_FILE).write_text("{not json")
    doc = runsmeta.read_meta(tmp_path)
    assert doc["baseline"] is None and doc["damaged"].endswith(".bak")
    assert (tmp_path / (runsmeta.META_FILE + ".bak")).read_text() == "{not json"


# ------------------------------------------------------------------ bulk
def test_bulk_archive_restore_tag(tmp_path: Path) -> None:
    a, b = _run(tmp_path, "a"), _run(tmp_path, "b")
    res, st = runsmeta.bulk("archive", [str(a), str(b)])
    assert st == 200 and len(res["done"]) == 2 and runsmeta.is_archived(a)
    res, st = runsmeta.bulk("tag", [str(a), str(b)], tags=["timing"])
    assert runsmeta.read_tags(b) == ["timing"]
    res, st = runsmeta.bulk("untag", [str(a)], tags=["TIMING"])
    assert runsmeta.read_tags(a) == [] and runsmeta.read_tags(b) == ["timing"]
    res, st = runsmeta.bulk("restore", [str(a)])
    assert not runsmeta.is_archived(a) and runsmeta.is_archived(b)


def test_bulk_refuses_what_is_not_a_run(tmp_path: Path) -> None:
    (tmp_path / "runs" / "empty").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    a = _run(tmp_path, "a")
    res, st = runsmeta.bulk("delete", [str(tmp_path / "runs" / "empty"), str(outside), str(a)],
                            live=[str(a)])
    assert st == 200 and res["done"] == []
    assert len(res["failed"]) == 3
    assert a.is_dir() and outside.is_dir()
    assert any("still running" in f["error"] for f in res["failed"])


def test_bulk_refuses_a_design_lanex_does_not_know(tmp_path: Path) -> None:
    a = _run(tmp_path, "a")
    res, _ = runsmeta.bulk("archive", [str(a)], known=[str(tmp_path / "other")])
    assert res["done"] == [] and "hasn't opened" in res["failed"][0]["error"]


def test_bulk_delete_moves_to_trash_then_undo_or_purge(tmp_path: Path) -> None:
    a, b = _run(tmp_path, "a"), _run(tmp_path, "b")
    res, st = runsmeta.bulk("delete", [str(a), str(b)])
    assert st == 200 and len(res["done"]) == 2 and res["bytes"] > 0
    assert not a.exists() and not b.exists()
    # The trash is a dot folder: never listed as a run.
    assert [r["tag"] for r in history.list_runs(tmp_path)] == []
    res2, st2 = runsmeta.bulk("undo", [], batch=res["batch"], design_dirs=[str(tmp_path)])
    assert st2 == 200 and a.is_dir() and b.is_dir()
    res3, _ = runsmeta.bulk("delete", [str(a)])
    runsmeta.bulk("purge", [], batch=res3["batch"], design_dirs=[str(tmp_path)])
    assert not (tmp_path / "runs" / runsmeta.TRASH_DIR / res3["batch"]).exists()
    late, st4 = runsmeta.bulk("undo", [], batch=res3["batch"], design_dirs=[str(tmp_path)])
    assert st4 == 410 and "Too late" in late["error"]


def test_bulk_delete_asks_once_for_resumed_children(tmp_path: Path) -> None:
    from lanex.controller import branch
    p = _run(tmp_path, "parent")
    _run(tmp_path, "kid", parent="parent")
    res, st = runsmeta.bulk("delete", [str(p)], children_of=branch.children)
    assert st == 409 and res["children"] == [{"run": "kid", "parent": "parent"}]
    assert p.is_dir()
    # Deleting the child with it is fine: nothing is left behind broken.
    res, st = runsmeta.bulk("delete", [str(p), str(tmp_path / "runs" / "kid")], children_of=branch.children)
    assert st == 200 and len(res["done"]) == 2


def test_stale_trash_is_purged(tmp_path: Path) -> None:
    a = _run(tmp_path, "a")
    res, _ = runsmeta.bulk("delete", [str(a)])
    bdir = tmp_path / "runs" / runsmeta.TRASH_DIR / res["batch"]
    old = time.time() - runsmeta.TRASH_TTL_S - 5
    os.utime(bdir, (old, old))
    runsmeta.purge_stale(tmp_path, background=False)
    assert not bdir.exists()


def test_bad_batch_ids_are_refused(tmp_path: Path) -> None:
    res, st = runsmeta.bulk("undo", [], batch="../../x", design_dirs=[str(tmp_path)])
    assert st == 400


# ------------------------------------------------------------------ values, names, changed
def test_values_of_metrics_and_settings(tmp_path: Path) -> None:
    a = _run(tmp_path, "a")
    v = runsmeta.values([str(a)], ["m:timing__setup__ws", "s:CLOCK_PERIOD", "m:missing", "x:bad"])
    assert v[str(a)] == {"m:timing__setup__ws": 1.5, "s:CLOCK_PERIOD": 10}
    # A rewritten metrics file is read again (the cache follows the file).
    time.sleep(0.01)
    (a / "final" / "metrics.json").write_text(json.dumps({"timing__setup__ws": -0.5, "x": 1}))
    os.utime(a / "final" / "metrics.json", None)
    assert runsmeta.values([str(a)], ["m:timing__setup__ws"])[str(a)]["m:timing__setup__ws"] == -0.5


def test_keys_lists_base_metric_names_and_plain_settings(tmp_path: Path) -> None:
    _run(tmp_path, "a")
    k = runsmeta.keys(tmp_path)
    names = [m["key"] for m in k["metrics"]]
    assert "timing__setup__ws" in names and not any(":" in n for n in names)
    settings = [s["key"] for s in k["settings"]]
    assert "CLOCK_PERIOD" in settings
    assert "PDK_ROOT" not in settings and "VERILOG_FILES" not in settings and "DESIGN_DIR" not in settings


def test_changed_leaves_paths_out(tmp_path: Path) -> None:
    other = tmp_path / "copy"
    other.mkdir()
    base = _run(tmp_path, "base")
    run = _run(tmp_path, "run", cfg={"DESIGN_DIR": str(other), "PDK_ROOT": "/other/pdk", "CLOCK_PERIOD": 8,
                                     "VERILOG_FILES": [str(other / "src" / "a.v")], "RUN_TAG": "run",
                                     "meta": {"librelane_version": "3.0.4"}})
    ch = runsmeta.changed(str(base), [str(run), str(base)])
    assert ch[str(run)] == [{"var": "CLOCK_PERIOD", "from": "10", "to": "8"}]
    assert ch[str(base)] == []


def test_directions_by_base_name_and_lanex_table() -> None:
    table = {"timing__setup__ws": {"higher_is_better": True}, "x": {"higher_is_better": None}}
    table.update({k: {"higher_is_better": h} for k, (h, _c, _w) in runsmeta.LANEX_DIRECTIONS.items()})
    assert runsmeta.direction_of("timing__setup__ws__corner:nom_tt_025C_1v80", table)["higher_is_better"] is True
    assert runsmeta.direction_of("design__instance__area__stdcell", table)["higher_is_better"] is False


def test_compare_colours_per_corner_rows_by_their_base_metric(tmp_path: Path, monkeypatch) -> None:
    a = _run(tmp_path, "a")
    b = _run(tmp_path, "b", metrics={"timing__setup__ws": 1.0, "timing__setup__ws__corner:nom_tt": 0.9})
    monkeypatch.setattr(runsmeta, "metric_directions", lambda: {
        "timing__setup__ws": {"higher_is_better": True, "critical": True}, "power__x": {"higher_is_better": None}})
    out = history.compare_runs([str(a), str(b)])
    meta = out["metric_meta"]["timing__setup__ws__corner:nom_tt"]
    assert meta["direction_known"] is True and meta["higher_is_better"] is True


# ------------------------------------------------------------------ CSV
def test_excel_csv_bytes() -> None:
    text = runsmeta.excel_csv([["Run", "Setup WNS (ns)", "Note"],
                               ["r-1", 2.5, '=HYPERLINK("x")'],
                               ["r-2", float("inf"), "line\nbreak, \"q\""],
                               ["r-3", -0.25, "＝SUM(A1)"]])
    assert text.startswith("﻿")
    lines = text[1:].split("\r\n")
    assert lines[0] == '"Run","Setup WNS (ns)","Note"'
    assert lines[1] == '"r-1",2.5,"\'=HYPERLINK(""x"")"'
    assert lines[2].startswith('"r-2",Infinity,"line\nbreak, ""q"""')
    assert lines[3] == '"r-3",-0.25,"\'＝SUM(A1)"'
    assert text.endswith("\r\n")
