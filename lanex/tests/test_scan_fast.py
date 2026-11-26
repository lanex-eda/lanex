# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Setup's source scan (autoconfig.scan_sources) reads each file once and
remembers it: a 300-file project took about 6 s (a regex per module over all
the text) and held other requests up meanwhile. The answers are the same as
the old whole-text scan's."""
from __future__ import annotations

import os
import random
import re
import threading
import time
from pathlib import Path

from lanex.controller import autoconfig


def _old_instantiated(texts, declared):
    """The old rule, for parity: a regex per declared module over the
    synthesisable text."""
    synth = "\n".join(texts)
    out = set()
    for nm in declared:
        rx = re.compile(r"\b" + re.escape(nm) + r"\b\s*(#\s*\(.*?\))?\s+[A-Za-z_][A-Za-z0-9_$]*\s*\(", re.S)
        for m in rx.finditer(synth):
            if re.search(r"\bmodule\s*$", synth[max(0, m.start() - 12):m.start()]):
                continue
            out.add(nm)
            break
    return out


SAMPLE = {
    "src/top.v": "module top #(parameter W = 8) (input clk, input [W-1:0] a, output [W-1:0] y);\n"
                 "  core #(.W(W), .D($clog2(W+1))) u_core (.clk(clk), .a(a), .y(y));\n"
                 "  leaf u_l [3:0] (.x(a[3:0]));\nendmodule\n",
    "src/core.v": "// core\nmodule core #(parameter W = 8, D = 2) (\n  input clk, input [W-1:0] a, output [W-1:0] y);\n"
                  "  /* leaf u_x (.x()); is a comment */\n  assign y = a;\nendmodule\n",
    "src/leaf.v": "module leaf (input [3:0] x);\nendmodule\nmodule unused_one (input i_clk);\nendmodule\n",
    "verify/top_tb.v": "`timescale 1ns/1ps\nmodule top_tb;\n  reg clk = 0;\n  top dut (.clk(clk));\n"
                       "  initial begin $display(\"hi\"); $finish; end\nendmodule\n",
}


def _write(d: Path, files):
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_the_scan_finds_tops_instances_benches_and_the_clock(tmp_path):
    _write(tmp_path, SAMPLE)
    r = autoconfig.scan_sources(tmp_path)
    assert set(r["instantiated"]) == {"core", "leaf"}
    assert r["top_candidates"] == ["unused_one", "top"] or set(r["top_candidates"]) == {"top", "unused_one"}
    assert r["testbench_modules"] == ["top_tb"] and r["testbench_files"] == ["verify/top_tb.v"]
    assert r["top"] in ("top", "unused_one")
    r2 = autoconfig.scan_sources(tmp_path, only_files=["src/top.v", "src/core.v", "src/leaf.v"])
    assert r2["clock_port"] in ("clk", "i_clk")
    texts = [autoconfig._strip_comments((tmp_path / f).read_text()) for f in SAMPLE if not f.startswith("verify")]
    # The old scan missed an array of instances (leaf u_l [3:0] (...)) and
    # offered leaf as a top; the new one counts it.
    assert _old_instantiated(texts, r["modules"]) == {"core"}


def test_a_changed_file_is_read_again(tmp_path):
    _write(tmp_path, SAMPLE)
    assert "leaf" in autoconfig.scan_sources(tmp_path)["instantiated"]
    p = tmp_path / "src" / "top.v"
    p.write_text(SAMPLE["src/top.v"].replace("  leaf u_l [3:0] (.x(a[3:0]));\n", ""))
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert "leaf" not in autoconfig.scan_sources(tmp_path)["instantiated"]


def _big(d: Path, n: int = 300):
    random.seed(7)
    for i in range(n):
        mods = []
        for k in range(3):
            subs = "".join(f"  m{random.randrange(i + 1, n)}_{s} #(.W(W), .D(D+{s})) u_{s} (.clk(clk), .a(a), .y(y{s}));\n"
                           for s in range(2)) if i + 1 < n else ""
            mods.append(f"module m{i}_{k} #(parameter W = 8, parameter D = 2) (input clk, input [W-1:0] a, output [W-1:0] y);\n"
                        + subs + "".join(f"  wire [W-1:0] t{q} = a ^ {q};  // {q}\n" for q in range(30))
                        + "  assign y = a;\nendmodule\n")
        (d / "src").mkdir(exist_ok=True)
        (d / "src" / f"f{i}.v").write_text("".join(mods))


def test_a_300_file_project_scans_fast_and_never_holds_other_threads(tmp_path):
    _big(tmp_path)
    gaps = []
    stop = threading.Event()

    def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    t0 = time.perf_counter()
    r = autoconfig.scan_sources(tmp_path)
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    autoconfig.scan_sources(tmp_path)
    again = time.perf_counter() - t0
    stop.set()
    t.join()
    assert len(r["verilog"]) == 300 and len(r["modules"]) == 900
    assert first < 2.0, first            # was ~6 s
    assert again < 0.5, again            # nothing changed: nothing read
    assert max(gaps) < 0.1, max(gaps)    # another thread never waits 100 ms


def test_the_instances_found_are_the_old_scans(tmp_path):
    _big(tmp_path, 60)
    r = autoconfig.scan_sources(tmp_path)
    texts = [autoconfig._strip_comments(p.read_text()) for p in sorted((tmp_path / "src").glob("*.v"))]
    assert set(r["instantiated"]) == _old_instantiated(texts, r["modules"])
