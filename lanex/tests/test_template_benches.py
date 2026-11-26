# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Every template's testbench checks its design and passes, under the
simulators the RTL IDE uses (Icarus and Verilator, built with LanEx's own
command lines), and fails loudly when the design is wrong (audit 4e)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lanex.controller import lint, scaffold, simulate

TEMPLATES = [t["name"] for t in scaffold.list_templates() if t.get("has_testbench")]
BENCH = {t["name"]: t["testbench"] for t in scaffold.list_templates() if t.get("testbench")}


def _have(*tools: str) -> bool:
    return all(shutil.which(t) for t in tools)


ENGINES = [
    pytest.param("iverilog", marks=pytest.mark.skipif(not _have("iverilog", "vvp"), reason="Icarus not installed")),
    pytest.param("verilator", marks=pytest.mark.skipif(not _have("verilator", "make", "g++"), reason="Verilator not installed")),
]


def _make(tmp_path: Path, name: str) -> Path:
    res = scaffold.create_project(tmp_path / name, name, top="", pdk="sky130A", scl="sky130_fd_sc_hd")
    if not res.get("ok"):
        pytest.skip(res.get("error"))
    return Path(res["design_dir"])


def _simulate(d: Path, tb: str, engine: str):
    ci = lint.config_inputs(d)
    assert ci["mode"] == "list" and ci["files"], ci
    m = lint.merge_inputs(ci, exclude=[tb])
    top = simulate.top_module_of(d, tb)
    argv = simulate.build_sim_command(d, top=top, sources=m["sources"], testbench=tb,
                                      defines=m["defines"], include_dirs=m["include_dirs"],
                                      run_mode="local", sim_engine=engine)
    # No core dump from a bench that aborts (a failing Verilator bench), as the
    # IDE runs it: WSL would write ~260 MB to the Windows temp folder each time.
    p = subprocess.run(argv, cwd=d, capture_output=True, text=True, timeout=600, preexec_fn=simulate._limit_core)
    return p.returncode, p.stdout + p.stderr


def test_every_example_has_a_bench():
    # The learning ladder (audit section 7, item 6): every rung has a bench.
    assert set(TEMPLATES) >= {"blinky", "counter", "fifo", "multiplier", "spm", "seg7", "traffic", "pwm",
                              "uart_tx", "timing", "tt_tile"}
    for t in scaffold.list_templates():
        if t["kind"] != "blank":
            assert t["has_testbench"] and t["teaches"], t["name"]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", TEMPLATES)
def test_template_bench_passes(tmp_path: Path, name: str, engine: str):
    d = _make(tmp_path, name)
    tb = BENCH[name]
    assert (d / tb).is_file(), tb
    rc, out = _simulate(d, tb, engine)
    assert rc == 0, out[-3000:]
    assert re.search(r"^PASS\b", out, re.M), out[-3000:]
    assert not re.search(r"\bFAIL\b", out), out[-3000:]
    # Verilator: one timescale for every file, so no TIMESCALEMOD.
    assert "TIMESCALEMOD" not in out, out[-3000:]


# A design that is wrong makes its bench say FAIL and exit non-zero.
BREAK = {
    "counter": ("src/counter.v", "count <= count + 1'b1;", "count <= count + 2'd2;"),
    "multiplier": ("src/mult.v", "p <= a * b;", "p <= a * b + 1'b1;"),
    "fifo": ("src/fifo.v", "dout <= mem[rptr];", "dout <= ~mem[rptr];"),
    "blinky": ("src/blinky.v", "assign led = ctr[WIDTH-1];", "assign led = ctr[0];"),
    "seg7": ("src/seg7.v", "4'h7: seg = 7'b0000111;", "4'h7: seg = 7'b0000110;"),
    "traffic": ("src/traffic.v", "S_YELLOW: if (timer == YELLOW_T - 1) next = S_RED;",
                "S_YELLOW: if (timer == YELLOW_T) next = S_RED;"),
    "pwm": ("src/pwm_core.v", "out   <= (count < duty);", "out   <= (count <= duty);"),
    "uart_tx": ("src/uart_tx.sv", "? shreg[0] : 1'b1;", "? shreg[7] : 1'b1;"),
    "timing": ("src/mult12.v", "p  <= ra * rb;", "p  <= ra * rb + 1'b1;"),
    "tt_tile": ("src/project.v", "assign uio_oe  = 8'b1111_0000;", "assign uio_oe  = 8'b1111_1111;"),
}


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", sorted(BREAK))
def test_template_bench_catches_a_wrong_design(tmp_path: Path, name: str, engine: str):
    d = _make(tmp_path, name)
    rel, old, new = BREAK[name]
    src = (d / rel).read_text()
    assert old in src
    (d / rel).write_text(src.replace(old, new))
    tb = BENCH[name]
    rc, out = _simulate(d, tb, engine)
    assert rc != 0, out[-3000:]
    assert re.search(r"\bFAIL\b", out), out[-3000:]
    assert not re.search(r"^PASS\b", out, re.M), out[-3000:]
    # A time in a FAIL line is in ns ($timeformat), never picoseconds labelled ns.
    for t in re.findall(r"FAIL at ([^:]+):", out):
        assert re.fullmatch(r"\s*\d+\.\d{3} ns", t), (t, out[-3000:])
        assert float(t.split()[0]) < 1e6, t


def test_bench_times_follow_timeformat():
    """``%0t`` prints the simulator's precision (ps under 1ns/1ps), so the old
    "FAIL at %0t ns" said 25000 ns for 25 ns. Every bench that prints a time
    sets ``$timeformat`` to ns and prints it with ``%t``."""
    root = Path(scaffold.__file__).resolve().parent / "templates"
    for tb in [p for p in root.glob("*/verify/*") if p.suffix in (".v", ".sv")]:
        text = tb.read_text()
        assert "%0t" not in text, tb
        if "%t" in text:
            assert re.search(r'\$timeformat\(-9,\s*\d+,\s*" ns"', text), tb


def test_benches_drive_inputs_away_from_the_sampling_edge():
    """The races the audit found: inputs set with blocking assignments right
    after @(posedge clk). The benches now change inputs on the falling edge."""
    root = Path(scaffold.__file__).resolve().parent / "templates"
    for tb in [p for p in root.glob("*/verify/*") if p.suffix in (".v", ".sv")]:
        text = tb.read_text()
        assert not re.search(r"@\(posedge clk\);\s*\w+\s*=", text), tb


def test_benches_fail_with_fatal_not_error():
    """Icarus exits 0 after $error, so a bench that reports with $error alone
    looks like a pass to anything that reads the exit code. Every bench ends
    in $fatal on a failure and prints PASS otherwise."""
    root = Path(scaffold.__file__).resolve().parent / "templates"
    for name, tb in BENCH.items():
        text = (root / name / tb).read_text()
        assert "$fatal(1," in text and re.search(r'\$display\("PASS: ', text), (name, tb)
        assert "$error" not in text, (name, tb)


def test_the_template_list_names_each_templates_files():
    """The first-chip guide names the files the project really gets
    (guide-steps.js guideFiles reads `sources` and `testbench`)."""
    by = {t["name"]: t for t in scaffold.list_templates()}
    assert by["blinky"]["sources"] == ["src/blinky.v"] and by["blinky"]["testbench"] == "verify/blinky_tb.v"
    assert by["uart_tx"]["sources"] == ["src/uart_pkg.sv", "src/uart_tx.sv"]
    assert by["seg7"]["clock_port"] is None and by["seg7"]["size"]["FP_SIZING"] == "absolute"
    orders = [by[n]["order"] for n in ("blinky", "seg7", "counter", "traffic", "pwm", "uart_tx", "fifo", "timing",
                                       "spm", "tt_tile", "multiplier")]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)


def test_new_rungs_render_the_config_they_teach(tmp_path: Path):
    import json
    seg = json.loads((_make(tmp_path, "seg7") / "config.json").read_text())
    assert "CLOCK_PORT" not in seg and seg["FP_SIZING"] == "absolute"
    pwm = json.loads((_make(tmp_path, "pwm") / "config.json").read_text())
    assert pwm["VERILOG_FILES"] == ["dir::src/pwm_top.v", "dir::src/pwm_core.v"]
    assert pwm["VERILOG_INCLUDE_DIRS"] == ["dir::src/include"]
    sv = json.loads((_make(tmp_path, "uart_tx") / "config.json").read_text())
    assert sv["VERILOG_FILES"][0] == "dir::src/uart_pkg.sv", "the package is read first"
    tt = json.loads((_make(tmp_path, "tt_tile") / "config.json").read_text())
    assert tt["DIE_AREA"] == [0, 0, 161.0, 111.52] and tt["RT_MAX_LAYER"] == "met4"
    assert (tmp_path / "tt_tile" / "src" / "pin_order.cfg").is_file()
    t = json.loads((_make(tmp_path, "timing") / "config.json").read_text())
    assert t["CLOCK_PERIOD"] == 5.0


def test_spm_project_gets_the_self_checking_bench(tmp_path: Path):
    d = _make(tmp_path, "spm")
    text = (d / "verify" / "spm_tb.v").read_text()
    assert "PASS: spm" in text and "$fatal" in text
