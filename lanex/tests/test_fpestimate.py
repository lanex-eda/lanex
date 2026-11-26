"""The floorplan preview's inputs (controller/fpestimate.py): the pin count
from the Verilog, the latest synthesis area and the last run's clock, read
from a project's own files; and the knowledge base's index for Help."""
import json
from pathlib import Path

from lanex.controller import fpestimate, problems


def test_port_bits_counts_every_pin_bit():
    v = """
    module top #(parameter W = 8) (
        input  wire         clk, rst,
        input  wire [W-1:0] a,
        input  wire [2*W-1:0] b,   // 16
        output reg  [7:0]   y,
        inout  wire         sda
    );
    endmodule
    module other(input x); endmodule
    """
    assert fpestimate.port_bits(v, "top") == 1 + 1 + 8 + 16 + 8 + 1
    assert fpestimate.port_bits(v, "nope") is None


def test_port_bounds_never_run_code():
    assert fpestimate._bound("W-1", {"W": "8"}) == 7
    assert fpestimate._bound("2*W-1", {"W": "4"}) == 7
    assert fpestimate._bound("__import__('os')", {}) is None
    assert fpestimate._bound("9**99999", {}) is None


def _run(root: Path, tag: str, *, area=None, final=None, cfg=None):
    rd = root / "runs" / tag
    (rd / "06-yosys-synthesis").mkdir(parents=True)
    if area is not None:
        (rd / "06-yosys-synthesis" / "state_out.json").write_text(json.dumps({"metrics": {"design__instance__area": area}}))
    (rd / "resolved.json").write_text(json.dumps(cfg or {"PDK": "sky130A", "CLOCK_PERIOD": 10}))
    if final is not None:
        (rd / "final").mkdir()
        (rd / "final" / "metrics.json").write_text(json.dumps(final))
    return rd


def test_estimate_reads_the_latest_synthesis_and_the_last_runs_clock(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.v").write_text("module top(input clk, input [3:0] a, output [3:0] y);\nendmodule\n")
    (tmp_path / "config.json").write_text(json.dumps({"DESIGN_NAME": "top", "VERILOG_FILES": ["dir::src/*.v"],
                                                       "CLOCK_PERIOD": 10, "LEFT_MARGIN_MULT": 6}))
    _run(tmp_path, "t1", area=3359.472, final={"timing__setup__ws": -0.59, "design__instance__area__stdcell": 4167},
         cfg={"PDK": "nosuchpdk", "CLOCK_PERIOD": 10})
    out = fpestimate.estimate_inputs(tmp_path, "nosuchpdk", "nosuchlib")
    assert out["synth"]["tag"] == "t1" and out["synth"]["area"] == 3359.472
    assert out["synth"]["placed_area"] == 4167
    assert out["timing"] == {"tag": "t1", "clock_period": 10.0, "setup_ws": -0.59, "pdk": "nosuchpdk"}
    assert out["pins"] == 1 + 4 + 4
    assert out["margins"]["LEFT_MARGIN_MULT"] == 6 and out["margins"]["TOP_MARGIN_MULT"] == 4
    assert out["site"] is None and out["installed"] is False, "a PDK that isn't here: no site, the page says so"
    # No project (the wizard): the PDK's numbers only.
    assert fpestimate.estimate_inputs(None, "nosuchpdk", "x")["synth"] is None


def test_a_run_for_another_pdk_is_not_the_size_source(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    _run(tmp_path, "gf", area=9000, cfg={"PDK": "gf180mcuD"})
    assert fpestimate.estimate_inputs(tmp_path, "sky130A", "sky130_fd_sc_hd")["synth"] is None


def test_kb_index_lists_every_entry_and_the_runs_that_hit_it(tmp_path):
    for tag, text in (("a", "[PDN-0175] Pitch 3.0000 is too small for, must be atleast 7.4000\n"),
                      ("b", "[GRT-0228] Horizontal edge usage exceeds the maximum allowed.\n"),
                      ("c", "")):
        rd = tmp_path / "runs" / tag
        rd.mkdir(parents=True)
        (rd / "error.log").write_text(text)
    idx = problems.kb_index(tmp_path)
    ids = {e["id"] for e in idx["entries"]}
    assert {"gpl-0301", "pdn-0175", "latch", "config-value"} <= ids
    assert idx["seen"] == {"pdn-0175": ["a"], "grt-0116": ["b"]}
    assert next(e for e in idx["entries"] if e["id"] == "pdn-0175")["forced"] is True
    assert next(e for e in idx["entries"] if e["id"] == "drt-0073")["forced"] is False
