# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Results that must read the way the tools' own reports do (review R3).

The no-clock cases are cut from a real LibreLane 3.0.4 run (Blinky with one
input-to-output path, ``assign led2 = ~en``, and ``CLOCK_PORT: "clock"``,
a port the design doesn't have): OpenSTA times ``en -> led2`` against the
virtual clock the base SDC makes, so every corner has a finite slack, while
its sta.log says "There are 24 unclocked register/latch pins"."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import pytest

from lanex.controller import history, lint, macrolib, simulate, verify

GOLD = Path(__file__).parent / "goldens" / "sta_paths_run"

# The first lines of a real max.rpt of that run (max_ss_100C_1v60).
_IO_MAX_RPT = """
===========================================================================
report_checks -path_delay max (Setup)
============================================================================
======================= max_ss_100C_1v60 Corner ===================================

Startpoint: en (input port clocked by clock)
Endpoint: led2 (output port clocked by clock)
Path Group: clock
Path Type: max
"""
# Its sta.log, the lines that matter.
_PORT_MISSING = ("Warning: base.sdc line 15, port '{port}' not found.\n"
                 "Warning: base.sdc line 32, port '{port}' not found.\n"
                 "Warning: base.sdc line 69, transition time can not be specified for virtual clocks.\n")
_UNCLK = "Warning: There are {n} unclocked register/latch pins.\n"

_CLEAN = {"design__lint_error__count": 0, "magic__drc_error__count": 0, "klayout__drc_error__count": 0,
          "design__lvs_error__count": 0, "route__antenna_violation__count": 0}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LANEX_HOME", str(tmp_path / "home"))
    history._SUMMARY_CACHE.clear()


def _sta_run(tmp_path: Path, *, seq: int = 24, unclocked: Optional[int] = 24, port: Optional[object] = "clock",
             missing: Optional[str] = "clock", ws: float = 4.39, io_path: bool = True, tag: str = "pc1") -> Path:
    run = tmp_path / "proj" / "runs" / tag
    sta = run / "55-openroad-stapostpnr"
    corners = ["max_ss_100C_1v60", "nom_tt_025C_1v80", "min_ff_n40C_1v95"]
    m = {**_CLEAN, "design__instance__count__class:sequential_cell": seq,
         "timing__setup__ws": ws, "timing__hold__ws": ws, "timing__setup_vio__count": 0, "timing__hold_vio__count": 0}
    log = (_PORT_MISSING.format(port=missing) if missing else "") + (_UNCLK.format(n=unclocked) if unclocked else "")
    for c in corners:
        (sta / c).mkdir(parents=True)
        (sta / c / "max.rpt").write_text(_IO_MAX_RPT if io_path else "=== " + c + " Corner ===\n\nNo paths found.\n")
        if unclocked is not None:
            (sta / c / "sta.log").write_text(log)
        for k in ("timing__setup__ws", "timing__hold__ws", "timing__setup_vio__count", "timing__hold_vio__count"):
            m[k + "__corner:" + c] = m[k]
    (sta / "state_in.json").write_text("{}")
    (sta / "state_out.json").write_text(json.dumps({"metrics": m}))
    (run / "final").mkdir()
    (run / "final" / "metrics.json").write_text(json.dumps(m))
    (run / "resolved.json").write_text(json.dumps({"DESIGN_NAME": "blinky", "CLOCK_PORT": port, "PDK": "sky130A",
                                                    "TIMING_VIOLATION_CORNERS": ["*tt*"]}))
    return run


def _by(rep):
    return {c["id"]: c for c in rep["checks"]}


# ------------------------------------------------------------------ finding 1
def test_an_io_path_and_unclocked_registers_is_never_clean(tmp_path: Path) -> None:
    # The reviewer's repro: one input-to-output path timed, 24 flip-flops not.
    rep = verify.verify_report(_sta_run(tmp_path))
    by = _by(rep)
    assert by["setup"]["status"] == "not_run" and by["hold"]["status"] == "not_run"
    nc = by["setup"]["no_clock"]
    assert nc["registers"] == 24 and nc["unclocked"] == 24 and nc["io_paths"] is True and nc["port_missing"] is True
    assert "only the paths from an input straight to an output" in by["setup"]["detail"]
    assert "has no port by that name" in by["setup"]["detail"]
    assert rep["verdict"]["state"] == "incomplete"
    assert rep["run_verdict"]["state"] == "incomplete" and rep["run_verdict"]["label"] == "Timing not checked: no clock"
    assert rep["run_verdict"]["no_clock"] is True
    assert rep["details"]["timing"]["no_clock"]["io_paths"] is True
    # The run list reads the same verdict, and it is not a success.
    s = history.list_runs(tmp_path / "proj")[0]
    assert s["verdict"]["state"] == "incomplete" and s["verdict"]["no_clock"] and s["success"] is False


def test_clock_port_null_with_registers_is_no_clock(tmp_path: Path) -> None:
    # CLOCK_PORT null: the base SDC makes __VIRTUAL_CLK__, no register is clocked.
    rep = verify.verify_report(_sta_run(tmp_path, port=None, missing="__VIRTUAL_CLK__"))
    nc = _by(rep)["setup"]["no_clock"]
    assert nc and nc["sdc_port"] == "__VIRTUAL_CLK__" and nc["port_missing"] is True
    assert "CLOCK_PORT is not set" in _by(rep)["setup"]["detail"]
    assert rep["run_verdict"]["label"] == "Timing not checked: no clock"


def test_unclocked_pins_covering_the_flipflops_is_no_clock_without_sta0366(tmp_path: Path) -> None:
    rep = verify.verify_report(_sta_run(tmp_path, missing=None))
    assert _by(rep)["setup"]["no_clock"]["port_missing"] is False
    assert rep["run_verdict"]["state"] == "incomplete"


def test_a_missing_port_with_every_register_clocked_is_timed(tmp_path: Path) -> None:
    # STA-0366 about the port, but no unclocked register pin: the registers
    # were clocked (a user SDC), so the run was timed.
    rep = verify.verify_report(_sta_run(tmp_path, unclocked=0))
    by = _by(rep)
    assert by["setup"]["status"] == "pass" and not by["setup"].get("no_clock")
    assert rep["run_verdict"]["state"] == "passed"


def test_some_unclocked_flipflops_are_a_warning_with_the_count(tmp_path: Path) -> None:
    rep = verify.verify_report(_sta_run(tmp_path, unclocked=8, missing=None))
    by = _by(rep)
    assert not by["setup"].get("no_clock")
    assert by["setup"]["status"] == "warn" and by["setup"]["unclocked"] == 8 and by["setup"]["unclocked_warn"]
    assert "8 flip-flop clock pins have no clock" in by["setup"]["note"]
    assert by["hold"]["status"] == "pass" and by["hold"]["unclocked"] == 8
    rv = rep["run_verdict"]
    assert rv["state"] == "warning" and rv["unclocked"] == 8
    assert rv["label"] == "Finished: 8 flip-flop clock pins not timed"
    assert verify.run_verdict(_sta_run(tmp_path, unclocked=1, missing=None, tag="one"))["label"] == \
        "Finished: 1 flip-flop clock pin not timed"


def test_nothing_timed_at_all_is_still_no_clock(tmp_path: Path) -> None:
    rep = verify.verify_report(_sta_run(tmp_path, ws=1e30, io_path=False, missing=None, unclocked=24))
    nc = _by(rep)["setup"]["no_clock"]
    assert nc and nc["io_paths"] is False
    assert "OpenSTA timed no path in any corner" in _by(rep)["setup"]["detail"]


def test_a_real_spm_run_stays_clean(tmp_path: Path) -> None:
    # The golden cut from a real, clocked spm run: no unclocked pins.
    run = tmp_path / "proj" / "runs" / "spm"
    shutil.copytree(GOLD, run)
    (run / "final").mkdir(exist_ok=True)
    m = json.loads((run / "55-openroad-stapostpnr" / "state_out.json").read_text()).get("metrics") or {}
    m = {**_CLEAN, "timing__setup__ws": 4.10026, "timing__hold__ws": 0.31, "timing__setup_vio__count": 0,
         "timing__hold_vio__count": 0, **m, "design__instance__count__class:sequential_cell": 64}
    (run / "final" / "metrics.json").write_text(json.dumps(m))
    if not (run / "resolved.json").is_file():
        (run / "resolved.json").write_text(json.dumps({"DESIGN_NAME": "spm", "CLOCK_PORT": "clk"}))
    rep = verify.verify_report(run)
    by = _by(rep)
    assert not by["setup"].get("no_clock") and not by["setup"].get("unclocked")
    assert by["setup"]["status"] == "pass"


# ------------------------------------------------------------------ finding 6
def _sram_model():
    # The SRAM template run (sram_demo, sky130A, 700 x 600 die): core and PDN
    # as LibreLane resolved them (PDN_HOFFSET 16.65, PDN_VOFFSET 16.32, 1.6 wide,
    # 1.7 apart, pitch 153.18 / 153.6).
    return {"core": {"box": [5.52, 10.88, 694.14, 587.52]},
            "pdn": {"vlayer": "met4", "hlayer": "met5", "v": {"w": 1.6, "s": 1.7, "pitch": 153.6, "off": 16.32},
                    "h": {"w": 1.6, "s": 1.7, "pitch": 153.18, "off": 16.65}},
            "macros": {"tiny": {"ok": True, "w": 20, "h": 8,
                                "prects": [["VPWR", "power", "met4", 1, 0, 2, 8], ["VGND", "ground", "met4", 3, 0, 4, 8]]}}}


def test_power_straps_sit_on_their_centre_line_like_the_real_def():
    # final/def/sram_demo.def of that run: met5 VPWR centred at y 27.53 and
    # 180.71, VGND at 30.83 and 184.01; met4 VPWR at x 21.84, VGND at 25.14.
    h = macrolib.straps(_sram_model(), "h")
    assert h[0][0] == "power" and h[0][1:] == pytest.approx((26.73, 28.33))
    assert h[1][0] == "ground" and h[1][1:] == pytest.approx((30.03, 31.63))
    assert h[2][0] == "power" and sum(h[2][1:]) / 2 == pytest.approx(180.71)
    assert h[3][0] == "ground" and sum(h[3][1:]) / 2 == pytest.approx(184.01)
    v = macrolib.straps(_sram_model(), "v")
    assert sum(v[0][1:]) / 2 == pytest.approx(21.84) and sum(v[1][1:]) / 2 == pytest.approx(25.14)
    assert v[0][2] - v[0][1] == pytest.approx(1.6)


def test_a_pin_just_above_the_power_strap_does_not_cross_it():
    # A block whose pins start at y 28.35 clears the real VPWR strap (top edge
    # 28.33) but meets VGND (30.03-31.63); the old model put VPWR at 27.53-29.13.
    c = macrolib.crossings(_sram_model(), {"macro": "tiny", "name": "u", "location": [300.0, 28.35], "orientation": "N"})
    assert c == {"power": 0, "ground": 1}


# ------------------------------------------------------------------ finding 10
def test_lint_that_exits_non_zero_with_no_errors_is_not_ok():
    no_err = lint.summarize([])
    v = lint.lint_verdict(1, no_err, ["%Error: Internal Error: V3Ast.cpp:123: broken", "Aborting..."])
    assert v["ok"] is False and "exit code 1" in v["message"] and "Internal Error" in v["message"]
    v = lint.lint_verdict(127, no_err, ["bash: verilator: command not found"])
    assert v["ok"] is False and v["message"].startswith("Verilator wasn't found")
    assert lint.lint_verdict(0, no_err, []) == {"ok": True}
    # Parsed errors are the lint's own failure: no raw message on top.
    errs = lint.summarize(lint.parse_verilator("%Error: src/a.v:3:1: syntax error, unexpected endmodule"))
    assert errs["errors"] == 1
    assert lint.lint_verdict(1, errs, ["%Error: src/a.v:3:1: syntax error"]) == {"ok": False}
    assert lint.lint_verdict(0, no_err, [], timed_out=True)["ok"] is False


@pytest.mark.parametrize("line,state", [
    ("FAIL: 0", "pass"),
    ("0 FAIL", "pass"),
    ("PASS: 12 FAIL: 0", "pass"),
    ("FAILED: 0 of 10", "pass"),
    ("FAIL: 3", "fail"),
    ("3 FAILED", "fail"),
    ("PASS: 9 FAIL: 1", "fail"),
    ("FAIL: 0 != 1", "fail"),           # a mismatch report, not a count
    ("FAIL at 35 ns: sum = 4", "fail"),
    ("PASS: adder, 2 sums checked", "pass"),
])
def test_only_a_fail_marker_that_reports_a_failure_counts(line, state):
    j = simulate.judge(["@@LANEX-RUN", line, "tb.v:9: $finish called at 10 (1ns)"], rc=0, engine="iverilog")
    assert j["state"] == state, j
