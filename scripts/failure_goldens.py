#!/usr/bin/env python3
# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Force real LibreLane failures on scratch designs and record what they left
behind, for ``lanex/tests/goldens/failures/librelane-<version>.json``.

Every failure card LanEx shows (``controller/problems.py``) is tested against
the words a real run printed, not against words remembered. This script makes
those runs: each case copies a small design (Blinky from LanEx's template, or
LibreLane's SPM example), breaks one thing, runs LibreLane in its container the
way LanEx does (``python -m librelane --dockerized``), and turns the run folder
(or, for a config that doesn't load, the console output) into a golden record.

Run it inside the LanEx environment (the ``lanex`` WSL distro on Windows), with
this checkout on PYTHONPATH::

    python scripts/failure_goldens.py --scratch ~/scratch/force run latch pdn_pitch
    python scripts/failure_goldens.py --scratch ~/scratch/force capture latch pdn_pitch
    python scripts/failure_goldens.py list

``run`` forces the cases (two at a time with ``-j 2``); ``capture`` merges them
into the golden file. Paths in the record are rewritten: the scratch folder is
``/work`` and the PDK root ``/pdk``, as in the older cases.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "lanex" / "tests" / "goldens" / "failures" / "librelane-3.0.4.json"
PDK_ROOT = Path(os.environ.get("PDK_ROOT") or Path.home() / ".ciel")

# The config keys a golden keeps (what the cards read), on top of each case's own.
KEEP = ["DESIGN_NAME", "CLOCK_PORT", "CLOCK_PERIOD", "FP_SIZING", "DIE_AREA", "CORE_AREA", "FP_CORE_UTIL",
        "PL_TARGET_DENSITY_PCT", "PDK", "STD_CELL_LIBRARY", "VERILOG_FILES", "RUN_LINTER", "PDN_VPITCH", "PDN_HPITCH",
        "PDN_VWIDTH", "PDN_HWIDTH", "PDN_VSPACING", "PDN_HSPACING", "PDN_VOFFSET", "PDN_HOFFSET", "PDN_CORE_RING",
        "PDN_CORE_RING_VWIDTH", "PDN_CORE_RING_HWIDTH", "PDN_CORE_RING_VOFFSET", "PDN_CORE_RING_HOFFSET",
        "PDN_CORE_RING_VSPACING", "PDN_CORE_RING_HSPACING", "LEFT_MARGIN_MULT", "RIGHT_MARGIN_MULT",
        "TOP_MARGIN_MULT", "BOTTOM_MARGIN_MULT", "USE_SLANG", "VERILOG_INCLUDE_DIRS", "IO_PIN_ORDER_CFG",
        "ERRORS_ON_UNMATCHED_IO", "MAX_TRANSITION_CONSTRAINT", "MAX_FANOUT_CONSTRAINT", "PNR_SDC_FILE",
        "DESIGN_REPAIR_MAX_SLEW_PCT", "TIMING_VIOLATION_CORNERS", "LINTER_ERROR_ON_LATCH", "DIODE_ON_PORTS",
        "RUN_ANTENNA_REPAIR", "GRT_ANTENNA_REPAIR_ITERS", "GRT_ANTENNA_REPAIR_MARGIN", "CTS_CLK_BUFFERS",
        "CTS_ROOT_BUFFER"]

BLINKY = """// Free-running counter; the MSB toggles slowly -> drives an LED.
module blinky #(
    parameter WIDTH = 24
) (
    input  wire clk,
    input  wire rst,
    output wire led
);
    reg [WIDTH-1:0] ctr;
    always @(posedge clk) begin
        if (rst)
            ctr <= {WIDTH{1'b0}};
        else
            ctr <= ctr + 1'b1;
    end
%s
endmodule
"""


def _blinky(d: Path, body: str = "    assign led = ctr[WIDTH-1];", head: str = "") -> None:
    sys.path.insert(0, str(ROOT))
    from lanex.controller import scaffold
    res = scaffold.create_project(d, "blinky", top="", pdk="sky130A", scl="sky130_fd_sc_hd")
    assert res.get("ok"), res
    (d / "src" / "blinky.v").write_text(head + BLINKY % body, encoding="utf-8")


def _spm(d: Path) -> None:
    shutil.copytree(ROOT / "spm", d, ignore=shutil.ignore_patterns("runs", "verify"))


def _cfg(d: Path, **kv: Any) -> None:
    p = d / "config.json"
    cfg = json.loads(p.read_text())
    for k, v in kv.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def _yaml_add(d: Path, text: str) -> None:
    p = d / "config.yaml"
    p.write_text(p.read_text() + "\n" + text + "\n")


def _yaml_set(d: Path, key: str, value: str) -> None:
    p = d / "config.yaml"
    t = p.read_text()
    t2, n = re.subn(r"^%s:.*$" % re.escape(key), "%s: %s" % (key, value), t, flags=re.M)
    p.write_text(t2 if n else t + "\n%s: %s\n" % (key, value))


# --------------------------------------------------------------- the cases
# Each: (note, base design maker). The note is what was broken, in words.
CASES: Dict[str, Dict[str, Any]] = {}


def case(name: str, note: str):
    def deco(fn: Callable[[Path], None]):
        CASES[name] = {"note": note, "make": fn}
        return fn
    return deco


@case("latch", "blinky: an always @(*) without an else infers a latch; linter on")
def _latch(d: Path) -> None:
    _blinky(d, "    reg q;\n    always @(*) begin\n        if (rst) q = ctr[0];\n    end\n"
               "    assign led = ctr[WIDTH-1] ^ q;")


@case("modmissing", "blinky instantiates a module no file declares; linter on")
def _modmissing(d: Path) -> None:
    _blinky(d, "    wire pulse;\n    blink_sub u_sub (.clk(clk), .q(pulse));\n    assign led = ctr[WIDTH-1] ^ pulse;")


@case("modmissing_yosys", "same, RUN_LINTER false: Yosys finds it")
def _modmissing_y(d: Path) -> None:
    _modmissing(d)
    _cfg(d, RUN_LINTER=False)


@case("include_missing", "blinky includes a header that isn't there; linter on")
def _include_missing(d: Path) -> None:
    _blinky(d, head='`include "blink_defs.vh"\n')


@case("implicit_wire", "blinky assigns a wire it never declared; linter on (it only warns)")
def _implicit(d: Path) -> None:
    _blinky(d, "    assign tmp = ctr[0] & ctr[1];\n    assign led = ctr[WIDTH-1] ^ tmp;")


@case("dup_module", "an old copy of blinky.v in src/ picked up by dir::src/*.v; linter off")
def _dup(d: Path) -> None:
    _blinky(d)
    shutil.copyfile(d / "src" / "blinky.v", d / "src" / "blinky_old.v")
    _cfg(d, RUN_LINTER=False)


@case("dup_module_lint", "an old copy of blinky.v in src/ picked up by dir::src/*.v; linter on")
def _dup_lint(d: Path) -> None:
    _blinky(d)
    shutil.copyfile(d / "src" / "blinky.v", d / "src" / "blinky_old.v")


SV_PKG = "package bpkg;\n  localparam int W = 24;\nendpackage\n"
SV_TOP = """module blinky (
    input  logic clk,
    input  logic rst,
    output logic led
);
    import bpkg::*;
    logic [W-1:0] ctr;
    always_ff @(posedge clk) begin
        if (rst) ctr <= '0;
        else     ctr <= ctr + 1'b1;
    end
    assign led = ctr[W-1];
endmodule
"""


@case("sv_import", "SystemVerilog import inside the module, USE_SLANG false: Yosys's own reader refuses it")
def _sv_import(d: Path) -> None:
    _blinky(d)
    (d / "src" / "blinky.v").unlink()
    (d / "src" / "bpkg.sv").write_text(SV_PKG)
    (d / "src" / "blinky.sv").write_text(SV_TOP)
    _cfg(d, VERILOG_FILES=["dir::src/bpkg.sv", "dir::src/blinky.sv"])


@case("sv_import_slang", "the same design with USE_SLANG true: the fix the card offers")
def _sv_import_slang(d: Path) -> None:
    _sv_import(d)
    _cfg(d, USE_SLANG=True)


@case("unknown_key", "config.json sets FP_CORE_UTL (a typo): the config doesn't load")
def _unknown(d: Path) -> None:
    _blinky(d)
    _cfg(d, FP_CORE_UTL=40)


@case("bad_type", "config.json sets FP_CORE_UTIL to \"abc\": the config doesn't load")
def _bad_type(d: Path) -> None:
    _blinky(d)
    _cfg(d, FP_CORE_UTIL="abc")


@case("pin_order", "spm: the clk line taken out of pin_order.cfg")
def _pin_order(d: Path) -> None:
    _spm(d)
    p = d / "pin_order.cfg"
    p.write_text("\n".join(ln for ln in p.read_text().splitlines() if ln.strip() != "clk") + "\n")


@case("pdn_pitch", "spm: PDN_VPITCH 3 (smaller than its straps)")
def _pdn_pitch(d: Path) -> None:
    _spm(d)
    _yaml_set(d, "PDN_VPITCH", "3")


@case("pdn_ring", "blinky: PDN_CORE_RING true with the default core margins")
def _pdn_ring(d: Path) -> None:
    _blinky(d)
    _cfg(d, PDN_CORE_RING=True)


@case("pdn_ring_fixed", "blinky on a fixed 100 x 100 um die with PDN_CORE_RING true: the ring needs more than the margins")
def _pdn_ring_fixed(d: Path) -> None:
    _blinky(d)
    _cfg(d, PDN_CORE_RING=True, FP_SIZING="absolute", DIE_AREA=[0, 0, 100, 100])


@case("margin_negative", "blinky: LEFT_MARGIN_MULT -1")
def _margin(d: Path) -> None:
    _blinky(d)
    _cfg(d, LEFT_MARGIN_MULT=-1)


@case("rsz_slew", "blinky: MAX_TRANSITION_CONSTRAINT 0.01 ns, tighter than any buffer")
def _rsz(d: Path) -> None:
    _blinky(d)
    _cfg(d, MAX_TRANSITION_CONSTRAINT=0.01)


@case("corner_wildcard", "blinky: TIMING_VIOLATION_CORNERS names no corner of sky130A")
def _corner(d: Path) -> None:
    _blinky(d)
    _cfg(d, TIMING_VIOLATION_CORNERS=["*xyz*"])


@case("pdk_missing", "blinky asks for a PDK this install doesn't have (sky130C)")
def _pdk_missing(d: Path) -> None:
    _blinky(d)
    _cfg(d, PDK="sky130C")


# --------------------------------------------------------------- run
def _py() -> str:
    venv = Path.home() / ".local/share/pipx/venvs/lanex/bin/python"
    return str(venv) if venv.is_file() else sys.executable


def force(scratch: Path, name: str) -> Dict[str, Any]:
    d = scratch / name
    if d.exists():
        shutil.rmtree(d)
    d.parent.mkdir(parents=True, exist_ok=True)
    CASES[name]["make"](d)
    cfg = "config.json" if (d / "config.json").is_file() else "config.yaml"
    argv = [_py(), "-m", "librelane", "--pdk-root", str(PDK_ROOT), "--docker-no-tty", "--dockerized", cfg,
            "--run-tag", "f1", "--overwrite"]
    p = subprocess.run(argv, cwd=d, capture_output=True, text=True, timeout=1800)
    (d / "stdout.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    (d / "exit_code").write_text(str(p.returncode))
    return {"case": name, "rc": p.returncode}


# --------------------------------------------------------------- capture
def _norm(text: str, d: Path) -> str:
    return text.replace(str(d.parent), "/work").replace(str(PDK_ROOT), "/pdk")


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def capture(scratch: Path, name: str) -> Dict[str, Any]:
    d = scratch / name
    run = d / "runs" / "f1"
    out = _strip_ansi((d / "stdout.log").read_text(encoding="utf-8", errors="replace"))
    rec: Dict[str, Any] = {"note": CASES[name]["note"], "exit_code": int((d / "exit_code").read_text())}
    if not run.is_dir() or not any(p.is_dir() and p.name[:2].isdigit() for p in run.iterdir()):
        # The config didn't load: no step ran. What LanEx has is the console.
        lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        rec.update({"steps": [], "stopped": None, "final": False, "console": [_norm(x, d) for x in lines[-25:]],
                    "design_dir": "/work/" + name})
        return rec
    steps = sorted(p.name for p in run.iterdir() if p.is_dir() and p.name[:2].isdigit())
    stopped = next((s for s in steps if not (run / s / "state_out.json").is_file()), None)
    rec["steps"] = steps
    rec["stopped"] = stopped
    rec["final"] = (run / "final").is_dir()
    el = run / "error.log"
    rec["error_log"] = _norm(el.read_text(encoding="utf-8", errors="replace"), d) if el.is_file() else ""
    if stopped:
        sd = run / stopped
        logs = sorted(sd.glob("*.log"))
        if logs:
            rec["step_log_name"] = logs[0].name
            rec["step_log_tail"] = [_norm(x, d) for x in logs[0].read_text(errors="replace").splitlines()[-40:]]
        ps = sorted(sd.glob("*.process_stats.json"))
        if ps:
            rec["process_stats"] = json.loads(ps[0].read_text())
    lint = run / "01-verilator-lint" / "verilator-lint.log"
    if lint.is_file():
        rec["lint_log_tail"] = [_norm(x, d) for x in lint.read_text(errors="replace").splitlines()[-30:]]
    warn: Dict[str, str] = {}
    wl = run / "warning.log"
    if wl.is_file():
        for ln in wl.read_text(errors="replace").splitlines():
            m = re.search(r"\[(?:WARNING )?([A-Z]{2,5}-\d{4})\]", ln)
            if m and m.group(1) not in warn:
                warn[m.group(1)] = _norm(ln.strip(), d)[:300]
    rec["warnings"] = warn
    res = json.loads((run / "resolved.json").read_text()) if (run / "resolved.json").is_file() else {}
    rec["cfg"] = json.loads(_norm(json.dumps({k: res.get(k) for k in KEEP if k in res}), d))
    rec["design_dir"] = "/work/" + name
    said = [ln for ln in out.splitlines() if ln.strip()]
    rec["console_tail"] = [_norm(x, d) for x in said[-12:]]
    # What LibreLane said while loading the config (an unknown key is only a
    # warning there, and it is written nowhere else).
    rec["console_head"] = [_norm(x, d) for x in said[:8]]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scratch", type=Path, default=Path.home() / "lanex-scratch" / "force")
    ap.add_argument("-j", type=int, default=2, help="cases at a time")
    ap.add_argument("cmd", choices=["run", "capture", "list"])
    ap.add_argument("cases", nargs="*")
    a = ap.parse_args()
    names = a.cases or sorted(CASES)
    if a.cmd == "list":
        for n in sorted(CASES):
            print("%-20s %s" % (n, CASES[n]["note"]))
        return 0
    if a.cmd == "run":
        with ThreadPoolExecutor(max_workers=max(1, a.j)) as ex:
            for r in ex.map(lambda n: force(a.scratch, n), names):
                print(r["case"], "exit", r["rc"], flush=True)
        return 0
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for n in names:
        doc["cases"][n] = capture(a.scratch, n)
        print("captured", n, "stopped at", doc["cases"][n]["stopped"])
    GOLDEN.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
