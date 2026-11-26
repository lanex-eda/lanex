# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The RTL IDE's project index, project search and replace, the simulation
verdict and run settings, VHDL diagnostics and the schematic's Yosys script
(audit 4.4)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lanex.controller import editor, hdlindex, hdlsearch, schematic, simulate, vhdltools

ADDER = """`include "defs.vh"
// adder
module adder #(
    parameter W = `ACC_W
) (
    input  wire [W-1:0] a,
    input  wire [W-1:0] b,
    output wire [W:0]   s
);
    assign s = a + b;
endmodule
"""
TOP = """module acc_top #(parameter W = 8) (
    input  wire clk,
    input  wire [W-1:0] x,
    output wire [W-1:0] sum
);
    wire [W:0] next;
    reg  [W-1:0] held;
    // held is the running sum (a comment, not a use)
    adder #(.W(W)) u_add (.a(held), .b(x), .s(next));
    always @(posedge clk) held <= next[W-1:0];
    assign sum = held;
    function [W-1:0] twice(input [W-1:0] v);
        reg [W-1:0] t;
        begin
            t = v << 1;
            twice = t;
        end
    endfunction
endmodule
"""
TB = """module acc_top_tb;
    parameter CYCLES = 20;
    parameter STEP = 3;
    reg clk = 0;
    reg [7:0] x;
    wire [7:0] sum;
    acc_top dut (.clk(clk), .x(x), .sum(sum));
    initial $display("held %0d", dut.held);
endmodule
"""
VHD = """library ieee;
use ieee.std_logic_1164.all;
entity count4 is
  generic (MAXV : natural := 9);
  port (clk : in std_logic; q : out natural);
end entity count4;
architecture rtl of count4 is
  signal cnt : natural := 0;
begin
  process (clk) begin
    if rising_edge(clk) then
      if cnt = MAXV then cnt <= 0; else cnt <= cnt + 1; end if;
    end if;
  end process;
  q <= cnt;
end architecture rtl;
"""


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "verify").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({"DESIGN_NAME": "acc_top", "VERILOG_FILES": "dir::src/*.v"}))
    (tmp_path / "src" / "defs.vh").write_text("`define ACC_W 8\n")
    (tmp_path / "src" / "adder.v").write_text(ADDER)
    (tmp_path / "src" / "acc_top.v").write_text(TOP)
    (tmp_path / "verify" / "acc_top_tb.v").write_text(TB)
    (tmp_path / "src" / "count4.vhd").write_text(VHD)
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "adder.v").write_text("module adder(input a, output b); assign b = a; endmodule\n")
    hdlindex.forget_all()
    return tmp_path


def _at(idx, rel, needle, delta=0, nth=0):
    fi = idx.files[rel]
    pos = -1
    for _ in range(nth + 1):
        pos = fi.text.index(needle, pos + 1)
    return fi.line_col(pos + delta)


def test_definition_of_a_module_lists_both_copies_built_first(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    line, col = _at(idx, "src/acc_top.v", "adder #", 1)
    r = hdlindex.definition(idx, "src/acc_top.v", line, col)
    assert r["target"] == "module"
    assert [o["rel"] for o in r["results"]] == ["src/adder.v", "old/adder.v"]
    assert r["results"][0]["built"] is True and r["results"][1]["built"] is False


def test_definition_of_a_port_connection_is_the_childs_port(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    line, col = _at(idx, "src/acc_top.v", ".a(held)", 1)
    r = hdlindex.definition(idx, "src/acc_top.v", line, col)
    hit = [o for o in r["results"] if o["rel"] == "src/adder.v"]
    assert hit and hit[0]["kind"] == "port" and hit[0]["dir"] == "input" and hit[0]["line"] == 6
    # the signal inside the parentheses is the parent's own net
    line, col = _at(idx, "src/acc_top.v", ".a(held)", 3)
    r = hdlindex.definition(idx, "src/acc_top.v", line, col)
    assert r["results"][0]["rel"] == "src/acc_top.v" and r["results"][0]["line"] == 7


def test_definition_of_a_macro_a_local_and_a_hierarchical_name(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    line, col = _at(idx, "src/adder.v", "`ACC_W", 2)
    r = hdlindex.definition(idx, "src/adder.v", line, col)
    assert r["target"] == "macro" and r["results"][0]["rel"] == "src/defs.vh"
    line, col = _at(idx, "src/acc_top.v", "t = v")
    r = hdlindex.definition(idx, "src/acc_top.v", line, col)
    assert r["results"][0]["line"] == 13 and r["results"][0]["kind"] == "var"
    line, col = _at(idx, "verify/acc_top_tb.v", "dut.held", 5)
    r = hdlindex.definition(idx, "verify/acc_top_tb.v", line, col)
    assert r["results"][0]["rel"] == "src/acc_top.v" and r["results"][0]["line"] == 7


def test_unknown_names_say_what_they_are(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    (proj / "src" / "cells.v").write_text("module c(input a, output y);\n sky130_fd_sc_hd__inv_1 u (.A(a), .Y(y));\nendmodule\n")
    idx.refresh()
    line, col = _at(idx, "src/cells.v", "sky130", 1)
    r = hdlindex.definition(idx, "src/cells.v", line, col)
    assert not r["results"] and "standard cell from the PDK" in r["note"]


def test_references_skip_comments_and_count_outside_uses(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    line, col = _at(idx, "src/acc_top.v", "held;", 0)
    r = hdlindex.references(idx, "src/acc_top.v", line, col)
    lines = [(o["rel"], o["line"]) for o in r["results"]]
    assert ("src/acc_top.v", 8) not in lines                      # the comment
    assert ("verify/acc_top_tb.v", 8) in lines                    # dut.held
    writes = [o["line"] for o in r["results"] if o["write"]]
    assert writes == [10]                                         # held <= next
    # a port: its declaration, its uses, the .a( ) connection outside
    line, col = _at(idx, "src/adder.v", "a,", 0)
    r = hdlindex.references(idx, "src/adder.v", line, col)
    assert {(o["rel"], o["tag"]) for o in r["results"]} >= {("src/acc_top.v", "connection in u_add"), ("src/adder.v", "declared")}


def test_unsaved_tabs_are_what_the_answers_read(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    edited = TOP.replace("reg  [W-1:0] held;", "reg  [W-1:0] held;\n    reg  [W-1:0] extra;")
    line, col = 8, 18
    r = hdlindex.definition(idx, "src/acc_top.v", line, col, dirty={"src/acc_top.v": edited})
    assert r["name"] == "extra" and r["results"][0]["line"] == 8


def test_vhdl_entities_generics_and_signals(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    line, col = _at(idx, "src/count4.vhd", "cnt + 1")
    r = hdlindex.definition(idx, "src/count4.vhd", line, col)
    assert r["results"][0]["kind"] == "signal" and r["results"][0]["line"] == 8
    info = hdlindex.unit_info(idx, "count4")
    assert info["params"][0]["name"] == "MAXV" and info["params"][0]["default"] == "9"
    r = hdlindex.references(idx, "src/count4.vhd", line, col)
    assert sum(1 for o in r["results"] if o["write"]) == 2


def test_hierarchy_and_bench_parameters(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    mods = hdlindex.hierarchy(idx)["modules"]
    assert [c["inst"] for c in mods["acc_top"]["children"]] == ["u_add"]
    tb = hdlindex.unit_info(idx, "acc_top_tb")
    assert [(p["name"], p["default"]) for p in tb["params"]] == [("CYCLES", "20"), ("STEP", "3")]


def test_a_save_updates_the_index_at_once(proj: Path):
    idx = hdlindex.get(proj, wait=5)
    editor.write_text(proj, "src/adder.v", ADDER.replace("assign s", "wire q = a;\n    assign s"))
    hdlindex.note_saved(proj, "src/adder.v")
    assert any(s["name"] == "q" for s in idx.files["src/adder.v"].syms)


def test_index_speed_on_a_300_file_project(tmp_path: Path):
    (tmp_path / "src").mkdir()
    for i in range(300):
        body = "\n".join(f"    wire [7:0] w{j} = din ^ {j}; reg [7:0] r{j}; always @(posedge clk) r{j} <= w{j};" for j in range(60))
        nxt = f"    m{i+1} u_next (.clk(clk), .din(r0), .dout());\n" if i < 299 else ""
        (tmp_path / "src" / f"m{i}.v").write_text(
            f"module m{i} (input clk, input [7:0] din, output reg [7:0] dout);\n{body}\n{nxt}endmodule\n")
    hdlindex.forget_all()
    idx = hdlindex.get(tmp_path, wait=30)
    assert idx.status()["files"] == 300
    line, col = _at(idx, "src/m150.v", "m151 u_next", 1)
    t = time.perf_counter()
    r = hdlindex.definition(idx, "src/m150.v", line, col)
    assert r["results"][0]["rel"] == "src/m151.v"
    line, col = _at(idx, "src/m150.v", "r0)", 0)
    hdlindex.references(idx, "src/m150.v", line, col)
    assert time.perf_counter() - t < 0.2


# ------------------------------------------------------------------ search

def test_search_plain_word_case_and_globs(proj: Path):
    r = hdlsearch.search(proj, "held")
    assert r["total"] >= 5 and all(g["rel"].endswith((".v", ".vhd", ".vh", ".json")) for g in r["groups"])
    r = hdlsearch.search(proj, "W", case=True, word=True, include="src/**", exclude="old")
    assert {g["rel"] for g in r["groups"]} <= {"src/adder.v", "src/acc_top.v"}
    assert all(m["text"][m["at"]:m["at"] + m["len"]] == "W" for g in r["groups"] for m in g["matches"])
    with pytest.raises(hdlsearch.SearchError):
        hdlsearch.search(proj, "(", regex=True)


def test_search_regex_runs_with_a_time_limit(proj: Path):
    r = hdlsearch.search(proj, r"held\s*<=", regex=True)
    assert r["total"] == 1
    (proj / "src" / "evil.v").write_text("a" * 40 + "!\n")
    hdlindex.forget_all()
    with pytest.raises(hdlsearch.SearchError, match="took too long"):
        hdlsearch._scan_child(hdlsearch.compile_query(r"(a+)+$", regex=True),
                              {"x": "a" * 40 + "!"}, 10, None, timeout=1.0)


def test_replace_only_ticked_matches_and_never_newer_text(proj: Path):
    r = hdlsearch.search(proj, "held", word=True, include="src/acc_top.v", replace="acc")
    g = r["groups"][0]
    ticked = [m["off"] for m in g["matches"] if m["line"] != 7]       # keep the declaration
    out = hdlsearch.replace(proj, "held", "acc", [{"rel": g["rel"], "hash": g["hash"], "offs": ticked}], word=True)
    assert out["results"][0]["ok"] and out["count"] == len(ticked)
    text = (proj / "src" / "acc_top.v").read_text()
    assert "reg  [W-1:0] held;" in text and "acc <= next" in text
    # the same search's hash is stale now: nothing is written
    again = hdlsearch.replace(proj, "held", "acc", [{"rel": g["rel"], "hash": g["hash"], "offs": ticked}], word=True)
    assert again["results"][0]["ok"] is False and again["results"][0]["changed"]
    # undo text comes back
    assert "held <= next" in out["results"][0]["before"]


def test_replace_keeps_crlf_and_regex_groups(proj: Path):
    (proj / "src" / "crlf.v").write_bytes(b"module crlf;\r\n  wire a_1, a_2;\r\nendmodule\r\n")
    hdlindex.forget_all()
    r = hdlsearch.search(proj, r"a_(\d)", regex=True, include="src/crlf.v", replace="b_$1")
    g = r["groups"][0]
    assert [m["new"] for m in g["matches"]] == ["b_1", "b_2"]
    hdlsearch.replace(proj, r"a_(\d)", "b_$1", [{"rel": g["rel"], "hash": g["hash"], "offs": [m["off"] for m in g["matches"]]}], regex=True)
    assert (proj / "src" / "crlf.v").read_bytes() == b"module crlf;\r\n  wire b_1, b_2;\r\nendmodule\r\n"


def test_replacement_syntax():
    assert hdlsearch.to_python_replacement("x_$1_$&_$$", True) == "x_\\g<1>_\\g<0>_$"
    assert hdlsearch.to_python_replacement("a\\b", False) == "a\\\\b"


# ------------------------------------------------------------------ simulation

IV_PASS = ["@@LANEX-RUN", "VCD info: dumpfile dump.vcd opened for output.", "PASS: adder, 2 sums checked",
           "verify/adder_tb.v:15: $finish called at 20000 (1ps)"]
IV_FATAL = ["@@LANEX-RUN", "FATAL: verify/w_tb.v:17: FAIL at 46.000 ns: sum = 255", "       Time: 46000  Scope: w_tb"]
VLT_FATAL = ["@@LANEX-RUN", "[46.000 ns] %Fatal: w_tb.v:17: Assertion failed in TOP.w_tb: FAIL", "%Error: w_tb.v:17: Verilog $stop", "Aborting..."]
GHDL_ERR = ["@@LANEX-RUN", "tb.vhd:25:9:@66ns:(assertion error): FAIL: q is not 9", "./tb:error: assertion failed"]


@pytest.mark.parametrize("lines,rc,engine,state,time_", [
    (IV_PASS, 0, "iverilog", "pass", None),
    (IV_FATAL, 1, "iverilog", "fail", {"ticks": 46000}),
    (VLT_FATAL, -6, "verilator", "fail", {"value": 46.0, "unit": "ns"}),
    (GHDL_ERR, 1, "ghdl", "fail", {"value": 66.0, "unit": "ns"}),
    (["@@LANEX-RUN", "No error found", "tb.v:9: $finish called at 10 (1ns)"], 0, "iverilog", "ran", None),
    (["@@LANEX-RUN", "checked: pass 3 of 10", "tb.v:9: $finish called at 10 (1ns)"], 0, "iverilog", "ran", None),
    (["@@LANEX-RUN", "ERROR: tb.v:29: first error", "       Time: 35000  Scope: tb", "tb.v:38: $finish called at 50000 (1ps)"], 0, "iverilog", "fail", {"ticks": 35000}),
    (["@@LANEX-RUN", "tb.v:34: $stop called at 35000 (1ps)"], 0, "iverilog", "stop", None),
    (["@@LANEX-RUN", "%Error: tb.v:34: Verilog $stop", "Aborting..."], 134, "verilator", "stop", None),
    (["@@LANEX-RUN", "- tb.v:35: Verilog $finish", "FAIL late, after finish"], 0, "verilator", "ran", None),
    (["tb.v:3: syntax error", "I give up."], 1, "iverilog", "build", None),
])
def test_verdict_rules(lines, rc, engine, state, time_):
    j = simulate.judge(lines, rc=rc, engine=engine)
    assert j["state"] == state, j
    if time_:
        assert j["fail_time"] == time_


def test_verdict_stopped_timeout_and_start():
    assert simulate.judge(["@@LANEX-RUN"], rc=-15, cancelled=True)["state"] == "stopped"
    assert simulate.judge(["@@LANEX-RUN", "x"], rc=-15, timed_out=True)["state"] == "timeout"
    assert simulate.judge(["bash: iverilog: command not found"], rc=127)["state"] == "start"


def test_run_settings_are_checked():
    p, bad = simulate.check_params({"CYCLES": "300", "W": "16'h1F", "NAME": '"abc"', "EMPTY": "", "X": "1+2"})
    assert p == {"CYCLES": "300", "W": "16'h1F", "NAME": '"abc"'} and bad and "X:" in bad[0]
    a, bad = simulate.check_plusargs("+seed=42 +verbose verbose")
    assert a == ["+seed=42", "+verbose"] and bad
    d, bad = simulate.check_defines("FAST DEPTH=4 9bad")
    assert d == {"FAST": None, "DEPTH": "4"} and bad


def test_commands_carry_parameters_plusargs_and_their_own_build_folder():
    iv = simulate.build_sim_command("/d", top="tb", sources=["a.v"], testbench="tb.v", run_mode="local",
                                    sim_engine="iverilog", params={"N": "7"}, plusargs=["+seed=1"],
                                    build_dir="sim_build/tb")[-1]
    assert "-Ptb.N=7" in iv and "vvp -n sim_build/tb/sim.vvp +seed=1" in iv and simulate.RUN_MARK in iv
    vl = simulate.build_sim_command("/d", top="tb", sources=["a.v"], testbench="tb.v", run_mode="local",
                                    sim_engine="verilator", params={"N": "7"}, plusargs=["+v"], build_dir="sim_build/tb")[-1]
    assert "-GN=7" in vl and "--Mdir sim_build/tb/obj_dir" in vl and vl.endswith("./sim_build/tb/obj_dir/sim_top +v")
    gh = simulate.build_sim_command("/d", top="count4_tb", sources=["c.vhd"], testbench="tb.vhd", run_mode="container",
                                    engine="docker", image="img", sim_engine="ghdl", params={"CYCLES": "3"},
                                    user_args=["--user", "1:1"])
    assert gh[:6] == ["docker", "run", "--rm", "--name", gh[4], "--user"] and "-gCYCLES=3" in gh[-1]
    assert "--assert-level=error" in gh[-1] and "ghdl -a --std=08" in gh[-1]


def test_vhdl_benches_are_found_and_their_entity_is_the_top(tmp_path: Path):
    (tmp_path / "verify").mkdir()
    (tmp_path / "verify" / "c_tb.vhd").write_text("entity c_tb is\nend entity;\narchitecture s of c_tb is begin end;\n")
    assert "verify/c_tb.vhd" in simulate.find_testbenches(tmp_path)
    assert simulate.top_module_of(tmp_path, "verify/c_tb.vhd") == "c_tb"


def test_ghdl_diagnostics_parse():
    d = vhdltools.parse_ghdl("src/c.vhd:5:14:error: no declaration for \"x\"\nsrc/c.vhd:9:1:warning: unused\nnoise")
    assert [(x["line"], x["severity"]) for x in d] == [(5, "error"), (9, "warning")]


def test_the_schematic_reads_the_design_like_synthesis():
    s = schematic.yosys_script(files=["src/a.v"], top="acc_top", defines={"X": None, "W": "8"},
                               include_dirs=["src"], level="rtl", params={"W": "16"})
    assert s.startswith("read_verilog -sv -noautowire -DX -DW=8 -Isrc src/a.v")
    assert "hierarchy -check -top acc_top -chparam W 16" in s and s.endswith("proc; opt_clean; write_json")
    v = schematic.yosys_script(files=[], vhdl_files=["c.vhd"], top="count4")
    assert "plugin -i ghdl" in v and "ghdl --std=08 c.vhd -e count4" in v
