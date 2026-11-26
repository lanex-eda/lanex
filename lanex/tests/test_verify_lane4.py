# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Verify: timing paths per corner, the no-clock verdict, warnings with
waivers, and the sign-off report (audit 6a, 6e, 6f, 4.9).

The STA golden (``goldens/sta_paths_run``) is cut from a real LibreLane 3.0.4
spm run: two setup paths and a reset recovery path of ``max_ss_100C_1v60``,
two hold paths of ``min_ff_n40C_1v95``, as OpenSTA wrote them."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lanex.controller import runwarnings, signoff, timing, verify

GOLD = Path(__file__).parent / "goldens" / "sta_paths_run"


@pytest.fixture()
def sta_run(tmp_path: Path) -> Path:
    run = tmp_path / "proj" / "runs" / "r1"
    shutil.copytree(GOLD, run)
    return run


# ------------------------------------------------------------------ timing
def test_index_lists_every_path_with_its_kind(sta_run: Path) -> None:
    rows = timing.index_report(sta_run / "55-openroad-stapostpnr" / "max_ss_100C_1v60" / "max.rpt")
    assert [r["kind"] for r in rows] == ["in-reg", "in-reg", "reset"]
    assert rows[0]["start"] == "x" and rows[0]["end"] == "_278_"
    assert rows[0]["corner"] == "max_ss_100C_1v60"
    assert rows[0]["slack"] == pytest.approx(4.10026)
    assert rows[2]["group"] == "asynchronous"


def test_path_detail_matches_opensta_numbers(sta_run: Path) -> None:
    lst = timing.path_list(sta_run, kind="setup", corner="max_ss_100C_1v60")
    assert lst["ok"] and lst["total"] == 3 and lst["violating"] == 0
    assert lst["rows"][0]["slack"] == pytest.approx(4.10026)
    p = timing.path_detail(sta_run, kind="setup", corner="max_ss_100C_1v60", i=lst["rows"][0]["i"])
    assert p["ok"] and p["check"] == "setup" and p["met"] is True
    assert p["arrival"] == pytest.approx(6.16794) and p["required"] == pytest.approx(10.2682)
    # The required time written out sums to OpenSTA's own number.
    assert sum(t["v"] for t in p["required_terms"]) == pytest.approx(p["required"], abs=1e-5)
    assert sum(t["v"] for t in p["arrival_terms"]) == pytest.approx(p["arrival"], abs=1e-5)
    words = [t["word"] for t in p["required_terms"]]
    assert words[:3] == ["clock edge", "capture clock", "uncertainty"]
    assert "setup time (dfrtp_2)" in words
    # Every pin with its cell, fanout, cap (pF), slew, incremental and total delay.
    pin = next(r for r in p["data"]["rows"] if r.get("pin") == "input34/X")
    assert pin["cell"] == "clkdlybuf4s25_1" and pin["fanout"] == 2
    assert pin["cap"] == pytest.approx(0.01153) and pin["delay"] == pytest.approx(0.5464) and pin["time"] == pytest.approx(2.567238)
    assert pin["net"] == "net34" and pin["edge"] == "r"
    load = next(r for r in p["data"]["rows"] if r.get("pin") == "fanout47/A")
    assert load["driver"] is False and load["net_in"] == "net34"
    # The clock trees are their own sections; the capture clock reaches _278_/CLK.
    assert p["capture"]["to"] == "_278_/CLK" and p["capture"]["cells"] == 2
    assert p["launch"]["cells"] == 0
    assert p["line"] >= 1 and "Startpoint: x" in p["text"]


def test_highlight_names_for_layout(sta_run: Path) -> None:
    p = timing.path_detail(sta_run, kind="setup", corner="max_ss_100C_1v60", i=0)
    h = p["highlight"]
    assert h["ports"] == ["x"]
    assert h["insts"][0] == "input34" and h["insts"][-1] == "_278_"
    assert "dsa[13].y_out_next" in h["nets"]
    assert h["pins"][-1] == "_278_/D"
    assert "clkbuf_0_clk" in h["clock"]["capture"]["insts"] and "clk" in h["clock"]["capture"]["nets"]


def test_hold_and_recovery_terms(sta_run: Path) -> None:
    hold = timing.path_detail(sta_run, kind="hold", corner="min_ff_n40C_1v95", i=0)
    assert hold["check"] == "hold" and hold["kind"] == "reg-reg"
    words = [t["word"] for t in hold["arrival_terms"]]
    assert words[:3] == ["clock edge", "launch clock", "clock to Q (dfrtp_2)"]
    assert any(t["word"].startswith("hold time") for t in hold["required_terms"])
    assert sum(t["v"] for t in hold["required_terms"]) == pytest.approx(hold["required"], abs=1e-5)
    rec = timing.path_detail(sta_run, kind="setup", corner="max_ss_100C_1v60", i=2)
    assert rec["check"] == "recovery" and rec["kind"] == "reset"
    assert any(t["word"].startswith("recovery time") for t in rec["required_terms"])


def test_list_search_kind_filter_and_baseline(sta_run: Path, tmp_path: Path) -> None:
    assert timing.path_list(sta_run, kind="setup", corner="max_ss_100C_1v60", q="net44")["shown"] >= 1
    assert timing.path_list(sta_run, kind="setup", corner="max_ss_100C_1v60", q="nothing-like-this")["shown"] == 0
    assert timing.path_list(sta_run, kind="setup", corner="max_ss_100C_1v60", only="reset")["shown"] == 1
    base = tmp_path / "proj" / "runs" / "r0"
    shutil.copytree(GOLD, base)
    rows = timing.path_list(sta_run, kind="setup", corner="max_ss_100C_1v60", base_dir=base)["rows"]
    assert rows[0]["base"] == pytest.approx(rows[0]["slack"])


def test_big_reports_in_other_corners_wait_until_opened(sta_run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Indexing several very big reports would hold the request for seconds.
    monkeypatch.setattr(timing, "_OTHERS_MAX_BYTES", 10)
    with timing._IDX_LOCK:
        timing._IDX_CACHE.clear()
    sta = sta_run / "55-openroad-stapostpnr"
    shutil.copytree(sta / "max_ss_100C_1v60", sta / "max_tt_025C_1v80")
    p = timing.path_detail(sta_run, kind="setup", corner="max_ss_100C_1v60", i=0)
    by = {o["corner"]: o for o in p["others"]}
    assert by["max_ss_100C_1v60"]["slack"] == pytest.approx(4.10026)
    assert by["max_tt_025C_1v80"].get("unread") is True and by["max_tt_025C_1v80"]["slack"] is None
    timing.path_list(sta_run, kind="setup", corner="max_tt_025C_1v80")   # opening it reads it
    p = timing.path_detail(sta_run, kind="setup", corner="max_ss_100C_1v60", i=0)
    assert {o["corner"]: o for o in p["others"]}["max_tt_025C_1v80"]["slack"] == pytest.approx(4.10026)


def test_histogram_bins_sit_on_zero() -> None:
    h = timing.histogram0([-0.53, -0.2, 0.05, 0.4, 1.2])
    assert 0.0 in h["edges"]
    assert sum(h["counts"]) == 5
    w = h["width"]
    assert all(abs((e / w) - round(e / w)) < 1e-6 for e in h["edges"])
    assert timing.histogram0([])["counts"] == []


def test_no_timing_report_is_an_honest_empty(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "r"
    run.mkdir(parents=True)
    out = timing.path_list(run, kind="setup")
    assert out["ok"] is False and "no setup timing report" in out["error"]


# ------------------------------------------------------------------ no clock
def _noclk_run(tmp_path: Path, *, ws: float = 1e30) -> Path:
    run = tmp_path / "runs" / "noclk"
    sta = run / "55-openroad-stapostpnr"
    corners = ["max_ss_100C_1v60", "nom_tt_025C_1v80"]
    m = {"design__instance__count__class:sequential_cell": 64, "timing__setup__ws": ws, "timing__hold__ws": ws,
         "timing__setup_vio__count": 0, "timing__hold_vio__count": 0}
    for c in corners:
        (sta / c).mkdir(parents=True)
        if ws >= 1e29:
            (sta / c / "max.rpt").write_text("=== " + c + " Corner ===\n\nNo paths found.\n")
        else:
            shutil.copy(GOLD / "55-openroad-stapostpnr" / "max_ss_100C_1v60" / "max.rpt", sta / c / "max.rpt")
        (sta / c / "sta.log").write_text("Warning: There are 64 unclocked register/latch pins.\n" if ws >= 1e29 else "")
        for k in ("timing__setup__ws", "timing__hold__ws", "timing__setup_vio__count", "timing__hold_vio__count"):
            m[k + "__corner:" + c] = m[k]
    (sta / "state_out.json").write_text(json.dumps({"metrics": m}))
    (sta / "state_in.json").write_text("{}")
    (run / "final").mkdir()
    (run / "final" / "metrics.json").write_text(json.dumps(m))
    (run / "resolved.json").write_text(json.dumps({"DESIGN_NAME": "spm", "CLOCK_PORT": None, "PDK": "sky130A",
                                                    "TIMING_VIOLATION_CORNERS": ["*tt*"]}))
    return run


def test_missing_clock_is_never_clean(tmp_path: Path) -> None:
    rep = verify.verify_report(_noclk_run(tmp_path))
    by = {c["id"]: c for c in rep["checks"]}
    assert by["setup"]["status"] == "not_run" and by["hold"]["status"] == "not_run"
    assert by["setup"]["no_clock"]["registers"] == 64 and by["setup"]["no_clock"]["unclocked"] == 64
    assert "No clock reached the registers" in by["setup"]["detail"]
    assert rep["verdict"]["state"] == "incomplete"
    assert rep["run_verdict"]["state"] == "incomplete"
    assert rep["run_verdict"]["label"] == "Timing not checked: no clock"
    assert rep["details"]["timing"]["no_clock"]


def test_a_timed_design_is_not_flagged_no_clock(tmp_path: Path) -> None:
    rep = verify.verify_report(_noclk_run(tmp_path, ws=4.1))
    by = {c["id"]: c for c in rep["checks"]}
    assert by["setup"]["status"] == "pass" and not by["setup"].get("no_clock")


# ------------------------------------------------------------------ warnings
_WARN_LOG = """Overriding minimum distance 0.1 with 0.42 for pins on side N to avoid overlap.
[STA-1140] /pdk/sky130A/lib/sky130_fd_sc_hd__tt_025C_1v80.lib line 1, library sky130_fd_sc_hd__tt_025C_1v80 already exists.
[STA-1140] /pdk/sky130A/lib/sky130_fd_sc_hd__ss_100C_1v60.lib line 1, library sky130_fd_sc_hd__ss_100C_1v60 already exists.
[RSZ-0020] found 2 floating nets.
Setup violations found in the following corners:
* max_ss_100C_1v60
* nom_ss_100C_1v60
[DRT-0349] LEF58_ENCLOSURE with no CUTCLASS is not supported. Skipping for layer mcon
"""
_FLOW_LOG = """Running 'Odb.CustomIOPlacement' at 'runs/x/26-odb-customioplacement'…
Overriding minimum distance 0.1 with 0.42 for pins on side N to avoid overlap.
Running 'OpenROAD.RepairDesignPostGPL' at 'runs/x/32-openroad-repairdesignpostgpl'…
[STA-1140] /pdk/sky130A/lib/sky130_fd_sc_hd__tt_025C_1v80.lib line 1, library sky130_fd_sc_hd__tt_025C_1v80 already exists.
[STA-1140] /pdk/sky130A/lib/sky130_fd_sc_hd__ss_100C_1v60.lib line 1, library sky130_fd_sc_hd__ss_100C_1v60 already exists.
[RSZ-0020] found 2 floating nets.
Running 'Checker.SetupViolations' at 'runs/x/72-checker-setupviolations'…
Setup violations found in the following corners:
* max_ss_100C_1v60
* nom_ss_100C_1v60
Running 'OpenROAD.DetailedRouting' at 'runs/x/44-openroad-detailedrouting'…
[DRT-0349] LEF58_ENCLOSURE with no CUTCLASS is not supported. Skipping for layer mcon
"""


def _warn_run(root: Path, name: str, log: str = _WARN_LOG, flow: str = _FLOW_LOG) -> Path:
    run = root / "runs" / name
    run.mkdir(parents=True)
    (run / "warning.log").write_text(log)
    (run / "flow.log").write_text(flow)
    return run


def test_multiline_records_are_one_warning_with_their_step(tmp_path: Path) -> None:
    recs = runwarnings.read_records(_warn_run(tmp_path, "a"))
    assert len(recs) == 6
    setup = next(r for r in recs if r["text"].startswith("Setup violations"))
    assert setup["lines"] == ["* max_ss_100C_1v60", "* nom_ss_100C_1v60"]
    assert setup["step_id"] == "Checker.SetupViolations" and setup["step_dir"] == "72-checker-setupviolations"


def test_groups_by_code_with_explanations(tmp_path: Path) -> None:
    w = runwarnings.run_warnings(_warn_run(tmp_path, "a"))
    by = {g["code"]: g for g in w["groups"]}
    assert w["total"] == 6
    assert by["STA-1140"]["count"] == 2 and by["STA-1140"]["worry"] == "harmless" and by["STA-1140"]["variants_total"] == 2
    assert by["LL:timing-corners"]["worry"] == "look" and by["LL:timing-corners"]["first"]["more"] == ["* max_ss_100C_1v60", "* nom_ss_100C_1v60"]
    assert by["RSZ-0020"]["steps"][0]["n"] == 32
    assert w["groups"][0]["worry"] == "look", "groups that need a look come first"


def test_new_since_baseline_and_gone(tmp_path: Path) -> None:
    base = _warn_run(tmp_path, "base", log="[RSZ-0020] found 2 floating nets.\n[GRT-0281] net clk has 64 pins.\n",
                     flow="")
    w = runwarnings.run_warnings(_warn_run(tmp_path, "a"), base)
    by = {g["code"]: g for g in w["groups"]}
    assert by["RSZ-0020"]["new"] is False and by["STA-1140"]["new"] is True
    assert w["new_count"] == len(w["groups"]) - 1
    assert [g["code"] for g in w["gone"]] == ["GRT-0281"]
    assert w["base_tag"] == "base"


def test_waiver_round_trip_stays_visible(tmp_path: Path) -> None:
    proj = tmp_path
    run = _warn_run(proj, "a")
    saved = runwarnings.save_waiver(proj, {"tool": "OpenSTA", "code": "STA-1140", "reason": "harmless",
                                           "note": "Read twice by LibreLane; no effect.", "by": "t",
                                           "expect_count": "auto"}, run_dir=run)
    assert saved["saved"] == "W1" and saved["entries"][0]["expect_count"] == 2
    text = (proj / runwarnings.WAIVER_FILE).read_text()
    assert "hides nothing" in text and 'code: "STA-1140"' in text
    w = runwarnings.run_warnings(run, None, proj)
    sta = next(g for g in w["groups"] if g["code"] == "STA-1140")
    assert sta["waived_count"] == 2 and sta["waivers"][0]["note"].startswith("Read twice")
    assert w["groups"][-1]["code"] == "STA-1140", "a waived group stays listed, last"
    assert w["waived_groups"] == 1 and not w["waivers"]["unused"]


def test_waiver_glob_step_and_unused(tmp_path: Path) -> None:
    run = _warn_run(tmp_path, "a")
    runwarnings.save_waiver(tmp_path, {"code": "STA-1140", "tool": "OpenSTA", "match": "*tt_025C*", "reason": "harmless",
                                       "note": "Only the typical library.", "expect_count": "auto"}, run_dir=run)
    runwarnings.save_waiver(tmp_path, {"code": "PDN-0179", "tool": "OpenROAD", "reason": "fix-later",
                                       "note": "Not in this run at all."})
    w = runwarnings.run_warnings(run, None, tmp_path)
    sta = next(g for g in w["groups"] if g["code"] == "STA-1140")
    assert sta["waived_count"] == 1
    assert [u["code"] for u in w["waivers"]["unused"]] == ["PDN-0179"]
    assert runwarnings.load_waivers(tmp_path)["entries"][0]["expect_count"] == 1


def test_waiver_refusals_and_broken_file(tmp_path: Path) -> None:
    with pytest.raises(runwarnings.WaiverError, match="at least 10 characters"):
        runwarnings.save_waiver(tmp_path, {"code": "X-0001", "note": "short", "reason": "harmless"})
    saved = runwarnings.save_waiver(tmp_path, {"code": "X-0001", "note": "a long enough note", "reason": "harmless"})
    with pytest.raises(runwarnings.WaiverError, match="changed on disk"):
        runwarnings.save_waiver(tmp_path, {"code": "X-0002", "note": "a long enough note", "reason": "harmless"},
                                mtime=saved["mtime"] - 1)
    (tmp_path / runwarnings.WAIVER_FILE).write_text("version: 1\nwaivers:\n  - id: [oops\n")
    bad = runwarnings.load_waivers(tmp_path)
    assert bad["error"] and "line" in bad["error"]
    run = _warn_run(tmp_path, "a")
    w = runwarnings.run_warnings(run, None, tmp_path)
    assert w["waivers"]["error"] and w["waived_groups"] == 0, "a broken file never breaks the panel"
    with pytest.raises(runwarnings.WaiverError, match="Fix the file first"):
        runwarnings.save_waiver(tmp_path, {"code": "X-0003", "note": "a long enough note", "reason": "harmless"})


def test_remove_waiver(tmp_path: Path) -> None:
    s = runwarnings.save_waiver(tmp_path, {"code": "X-0001", "note": "a long enough note", "reason": "harmless"})
    r = runwarnings.remove_waiver(tmp_path, "W1", mtime=s["mtime"])
    assert r["entries"] == []
    with pytest.raises(runwarnings.WaiverError):
        runwarnings.remove_waiver(tmp_path, "W1")


# ------------------------------------------------------------------ report
def test_report_files_say_not_signoff_first_and_match(tmp_path: Path) -> None:
    run = _noclk_run(tmp_path)
    (run / "warning.log").write_text(_WARN_LOG)
    res = signoff.write(run, verdict={"tone": "warn", "title": "Timing was never checked", "sub": "No clock."})
    assert set(res["files"]) == {"json", "md", "html"}
    md = (run / res["files"]["md"]).read_text(encoding="utf-8")
    html = (run / res["files"]["html"]).read_text(encoding="utf-8")
    data = json.loads((run / res["files"]["json"]).read_text(encoding="utf-8"))
    assert md.splitlines()[2].startswith("> **Not a foundry sign-off.**")
    assert html.index("Not a foundry sign-off") < html.index("Timing was never checked")
    assert "**Result: Timing was never checked.**" in md
    assert "Timing was never checked: no clock reached the registers" in md
    assert data["verdict"]["title"] == "Timing was never checked"
    # warning.log's six plus OpenSTA's "unclocked register pins" from sta.log.
    assert data["warnings"]["total"] == 7
    assert any(g["code"] == "STA:unclocked" for g in data["warnings"]["groups"])
    sums = (run / "lanex-report" / "SHA256SUMS").read_text()
    assert "report.md" in sums and "report.html" in sums
    for row in data["checks"]:
        assert row["result"] in md, "every check's result is in the Markdown"


def test_slack_never_rounds_a_failure_away() -> None:
    assert signoff.fmt_ns(-0.0002) == "−0.000200"
    assert signoff.fmt_ns(4.10026) == "+4.100"
    assert signoff.fmt_ns(1e30) == "no path"
    assert signoff.value_text({"id": "setup", "status": "fail", "value": 3, "metric": "timing__setup_vio__count",
                               "worst": -0.43}) == "3 violating · worst −0.430 ns"
