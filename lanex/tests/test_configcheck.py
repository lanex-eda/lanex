# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The checks before a run starts (audit 1c) and the flow a run uses (G12).

LibreLane's own loader runs in a container or a subprocess (configcheck
PROBE); here it is replaced by a fake that answers like it, so the tests need
no Docker. One test runs the real probe source in this Python when LibreLane
can load configs here."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lanex.controller import configcheck as cc


def _cfg(d: Path, data: Dict[str, Any], name: str = "config.json") -> Path:
    p = d / name
    p.write_text(json.dumps(data))
    return p


def _project(tmp_path: Path, verilog: str = "module spm(input clk, input rst, output y); assign y = rst; endmodule\n",
             **cfg: Any) -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "spm.v").write_text(verilog)
    base = {"DESIGN_NAME": "spm", "VERILOG_FILES": "dir::src/*.v", "CLOCK_PORT": "clk", "CLOCK_PERIOD": 10}
    base.update(cfg)
    return _cfg(tmp_path, base)


# ---------------------------------------------------------------- the flow (G12)

def test_flow_is_classic_unless_the_config_says(tmp_path: Path):
    pytest.importorskip("librelane.flows")
    cf = _project(tmp_path)
    r = cc.resolve_flow([str(cf)])
    assert r["name"] == "Classic" and r["cli"] is None and r["source"] == "default" and not r["error"]
    assert r["cls"].__name__ == "Classic" and "Yosys.Synthesis" in r["steps"]


def test_meta_flow_is_used_like_the_cli_does(tmp_path: Path):
    pytest.importorskip("librelane.flows")
    cf = _project(tmp_path, meta={"version": 2, "flow": "Chip"})
    r = cc.resolve_flow([str(cf)])
    assert r["name"] == "Chip" and r["cls"].__name__ == "Chip" and r["source"] == "config"
    # No -f: LibreLane reads meta.flow itself, so a terminal run is the same.
    assert r["cli"] is None
    # The page sends back the name it was shown: still the config's flow.
    assert cc.resolve_flow([str(cf)], "Chip")["cli"] is None
    # Another registered flow asked for explicitly wins, as -f does.
    r2 = cc.resolve_flow([str(cf)], "Classic")
    assert r2["cls"].__name__ == "Classic" and r2["cli"] == "Classic" and r2["source"] == "request"


def test_unknown_flow_names_are_refused_with_the_real_names(tmp_path: Path):
    pytest.importorskip("librelane.flows")
    cf = _project(tmp_path, meta={"version": 2, "flow": "Fancy"})
    r = cc.resolve_flow([str(cf)])
    assert r["cls"] is None
    assert "'Fancy'" in r["error"] and "meta.flow" in r["error"] and "Classic" in r["error"] and "Chip" in r["error"]
    r = cc.resolve_flow([str(_project(tmp_path))], "Nope")
    assert r["cls"] is None and "no flow called 'Nope'" in r["error"]
    # Never a quiet fallback to the first registered flow.
    assert "Optimizing" not in (r.get("name") or "")


def test_meta_flow_as_a_step_list_and_substitutions(tmp_path: Path):
    pytest.importorskip("librelane.flows")
    cf = _project(tmp_path, meta={"version": 2, "flow": ["Yosys.Synthesis", "OpenROAD.Floorplan"]})
    r = cc.resolve_flow([str(cf)])
    assert r["name"] == "Custom (2 steps)" and r["steps"] == ["Yosys.Synthesis", "OpenROAD.Floorplan"]
    cf = _project(tmp_path, meta={"version": 2, "substituting_steps": {"OpenROAD.STAPrePNR": None}})
    assert "without meta.flow" in cc.resolve_flow([str(cf)])["error"]
    cf = _project(tmp_path, meta={"version": 2, "flow": "Classic", "substituting_steps": {"OpenROAD.STAPrePNR": None}})
    r = cc.resolve_flow([str(cf)])
    assert not r["error"] and "substituted" in r["name"] and "OpenROAD.STAPrePNR" not in r["steps"]


def test_flow_factory_never_falls_back_to_another_flow():
    pytest.importorskip("librelane.flows")
    from lanex.server import routes
    assert routes._flow_factory("Classic").__name__ == "Classic"
    with pytest.raises(ValueError, match="no flow called 'Classicc'"):
        routes._flow_factory("Classicc")


# ---------------------------------------------------------------- top module and clock port

def _probs(tmp_path: Path, top: str, clock: Any, files: List[str], inc=()) -> List[Dict[str, Any]]:
    return cc.top_and_clock_problems(design_dir=tmp_path, design_name=top, clock_port=clock,
                                     verilog_files=files, include_dirs=list(inc), config_name="config.json")


def test_wrong_top_name_is_found_and_says_the_choices(tmp_path: Path):
    f = tmp_path / "a.v"
    f.write_text("// module fake;\nmodule counter #(parameter W=8) (input clk, output [W-1:0] q);\nendmodule\n"
                 "module helper; endmodule\n")
    p = _probs(tmp_path, "top", "clk", [str(f)])
    assert [x["code"] for x in p] == ["top"]
    assert "'top'" in p[0]["message"] and "counter, helper" in p[0]["fix"] and p[0]["hard"] is False
    assert "fake" not in p[0]["fix"]   # a commented-out module is not a module
    # Case only: said as such.
    p = _probs(tmp_path, "Counter", "clk", [str(f)])
    assert "case-sensitive" in p[0]["message"] and "counter" in p[0]["fix"]
    # Right name: nothing to say.
    assert _probs(tmp_path, "counter", "clk", [str(f)]) == []


def test_top_declared_in_a_file_the_run_does_not_build(tmp_path: Path):
    (tmp_path / "src").mkdir()
    a = tmp_path / "src" / "a.v"
    a.write_text("module a; endmodule\n")
    (tmp_path / "src" / "top.v").write_text("module top(input clk); endmodule\n")
    p = _probs(tmp_path, "top", "clk", [str(a)])
    assert p[0]["code"] == "top" and "src/top.v" in p[0]["message"] and "isn't one of the files" in p[0]["message"]


def test_top_from_an_included_file_or_a_macro_is_not_flagged(tmp_path: Path):
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "core.vh").write_text("module core(input clk); endmodule\n")
    w = tmp_path / "wrap.v"
    w.write_text('`include "core.vh"\n')
    assert _probs(tmp_path, "core", "clk", [str(w)], inc=[str(tmp_path / "inc")]) == []
    m = tmp_path / "m.v"
    m.write_text("`define NAME gen_top\nmodule `NAME (input clk); endmodule\n")
    assert _probs(tmp_path, "gen_top", "clk", [str(m)]) == []


def test_missing_clock_port_is_found_with_a_suggestion(tmp_path: Path):
    f = tmp_path / "t.v"
    f.write_text("module t(input wire i_clk, input rst_n, output reg q);\nalways @(posedge i_clk) q <= rst_n;\nendmodule\n")
    p = _probs(tmp_path, "t", "clk", [str(f)])
    assert [x["code"] for x in p] == ["clock"]
    assert "'clk'" in p[0]["message"] and "i_clk" in p[0]["message"] and p[0]["suggest"] == "i_clk"
    assert _probs(tmp_path, "t", "i_clk", [str(f)]) == []
    assert _probs(tmp_path, "t", ["i_clk"], [str(f)]) == []
    # No clock (a combinational design): nothing to check.
    assert _probs(tmp_path, "t", None, [str(f)]) == []


def test_clock_port_in_classic_port_style(tmp_path: Path):
    f = tmp_path / "c.v"
    f.write_text("module c(clock, d, q);\n  input clock; input d; output q;\n  assign q = d;\nendmodule\n")
    assert _probs(tmp_path, "c", "clock", [str(f)]) == []
    assert _probs(tmp_path, "c", "clk", [str(f)])[0]["suggest"] == "clock"


# ---------------------------------------------------------------- check(): the whole thing

PDK_READY = {"ready": True, "pdk_root": "/pdks", "where": []}


class _Probe:
    """Stands in for LibreLane's loader: answers what it is told, counts calls."""

    def __init__(self, answer: Dict[str, Any]):
        self.answer = answer
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, args, **kw):
        self.calls.append(args)
        return dict(self.answer)


@pytest.fixture(autouse=True)
def _fresh_cache():
    cc.clear_cache()
    yield
    cc.clear_cache()


def _check(tmp_path: Path, cf: Path, **kw):
    return cc.check(design_dir=str(tmp_path), config_files=[str(cf)], pdk="sky130A", scl="sky130_fd_sc_hd",
                    pdk_ready=PDK_READY, run_mode="local", flow={"name": "Classic", "cli": None}, **kw)


def test_broken_config_blocks_with_librelanes_words(tmp_path: Path, monkeypatch):
    cf = _project(tmp_path)
    probe = _Probe({"ok": False, "kind": "invalid", "errors": [
        "Value provided for variable 'FP_CORE_UTIL' of type Decimal is invalid: 'abc'"]})
    monkeypatch.setattr(cc, "_run_probe", probe)
    r = _check(tmp_path, cf, overrides={"FP_CORE_UTIL": "abc"})
    assert r["blocking"] is True and r["ok"] is False and r["checked"] is True
    assert r["problems"][0]["code"] == "config"
    assert "LibreLane can't load config.json" in r["problems"][0]["message"]
    assert "FP_CORE_UTIL" in r["problems"][0]["message"] and "one-off settings" in r["problems"][0]["fix"]
    # The run's own -c values reach the loader.
    assert "FP_CORE_UTIL=abc" in probe.calls[0]["overrides"]


def test_resolved_names_from_librelane_feed_the_top_check(tmp_path: Path, monkeypatch):
    cf = _project(tmp_path)
    monkeypatch.setattr(cc, "_run_probe", _Probe({"ok": True, "design_name": "spmx", "clock_port": "clk",
                                                  "verilog_files": [str(tmp_path / "src" / "spm.v")]}))
    r = _check(tmp_path, cf)
    assert r["blocking"] is False and [p["code"] for p in r["problems"]] == ["top"]
    assert r["design_name"] == "spmx"


def test_an_unknown_key_asks_first_and_names_the_nearest(tmp_path: Path, monkeypatch):
    # LibreLane 3.0.4 only warns ("An unknown key 'FP_CORE_UTL' was provided.")
    # and runs on: forced on Blinky, the run finished and the setting did nothing.
    from lanex.controller import introspect
    monkeypatch.setattr(introspect, "known_var_names", lambda: {"FP_CORE_UTIL", "FP_SIZING", "CLOCK_PERIOD"})
    cf = _project(tmp_path)
    monkeypatch.setattr(cc, "_run_probe", _Probe({
        "ok": True, "design_name": "spm", "clock_port": "clk", "verilog_files": [str(tmp_path / "src" / "spm.v")],
        "config_warnings": ["An unknown key 'FP_CORE_UTL' was provided.",
                            "Key 'PL_ROUTABILITY_DRIVEN' provided is unused by the current flow."]}))
    r = _check(tmp_path, cf)
    assert r["blocking"] is False and r["ok"] is False
    a, b = r["problems"][:2]
    assert a["code"] == "unknown-key" and a["near"] == ["FP_CORE_UTIL"] and "ignored" in a["message"]
    assert "Rename it to FP_CORE_UTIL" in a["fix"]
    assert b["key"] == "PL_ROUTABILITY_DRIVEN" and "no step of this flow reads" in b["message"]


def test_a_check_that_cannot_run_never_blocks(tmp_path: Path, monkeypatch):
    cf = _project(tmp_path)
    monkeypatch.setattr(cc, "_run_probe", _Probe({"kind": "unavailable", "why": "no usable Docker or Podman"}))
    r = _check(tmp_path, cf)
    assert r["ok"] is True and r["blocking"] is False and r["checked"] is False
    assert "no usable Docker" in r["note"]
    # The Verilog checks still run, from the config as written.
    cf = _project(tmp_path, CLOCK_PORT="clock")
    r = _check(tmp_path, cf)
    assert [p["code"] for p in r["problems"]] == ["clock"]


def test_no_pdk_yet_skips_the_load_but_not_the_reading(tmp_path: Path, monkeypatch):
    cf = _project(tmp_path, DESIGN_NAME="nope")
    probe = _Probe({"ok": True})
    monkeypatch.setattr(cc, "_run_probe", probe)
    r = cc.check(design_dir=str(tmp_path), config_files=[str(cf)], pdk="sky130A", scl=None,
                 pdk_ready={"ready": False}, run_mode="container", flow={"name": "Classic"})
    assert probe.calls == [] and "once the PDK is installed" in r["note"]
    assert [p["code"] for p in r["problems"]] == ["top"]


def test_cache_by_content_and_run_rechecks_a_cached_refusal(tmp_path: Path, monkeypatch):
    cf = _project(tmp_path)
    probe = _Probe({"ok": True, "design_name": "spm", "clock_port": "clk",
                    "verilog_files": [str(tmp_path / "src" / "spm.v")]})
    monkeypatch.setattr(cc, "_run_probe", probe)
    assert _check(tmp_path, cf)["cached"] is False
    assert _check(tmp_path, cf)["cached"] is True and len(probe.calls) == 1
    # Any project file changing (an SDC added, a source edited) asks again.
    (tmp_path / "src" / "impl.sdc").write_text("# new\n")
    _check(tmp_path, cf)
    assert len(probe.calls) == 2
    # A refusal is shown from the cache, but Run (reuse_failures=False) asks again.
    probe.answer = {"ok": False, "kind": "invalid", "errors": ["bad"]}
    cf.write_text(cf.read_text().replace('"CLOCK_PERIOD": 10', '"CLOCK_PERIOD": 11'))
    _check(tmp_path, cf)
    _check(tmp_path, cf)
    assert len(probe.calls) == 3
    _check(tmp_path, cf, reuse_failures=False)
    assert len(probe.calls) == 4


def test_flow_error_is_a_hard_problem(tmp_path: Path, monkeypatch):
    cf = _project(tmp_path)
    probe = _Probe({"ok": True})
    monkeypatch.setattr(cc, "_run_probe", probe)
    r = cc.check(design_dir=str(tmp_path), config_files=[str(cf)], pdk="sky130A", scl=None, pdk_ready=PDK_READY,
                 run_mode="local", flow={"name": "Classic", "error": "config.json asks for the flow 'X'"})
    assert r["blocking"] and r["problems"][0]["code"] == "flow" and probe.calls == []


def test_container_probe_mounts_read_only_and_never_pulls(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cc, "_permission_args", lambda e: ["--user", "1000:1000"])
    argv = cc._container_probe_argv({"configs": []}, engine="/usr/bin/docker", image="img@sha256:1",
                                    design_dir=Path("/mnt/c/proj"), pdk_root=str(Path.home() / ".ciel"),
                                    name="lanex-check-1")
    assert argv[:8] == ["/usr/bin/docker", "run", "--rm", "--pull=never", "--network=none", "--name",
                        "lanex-check-1", "--user"]
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert all(v.endswith(":ro") for v in vols)
    assert f"{Path.home()}:{Path.home()}:ro" in vols and "/mnt/c/proj:/mnt/c/proj:ro" in vols
    assert len(vols) == 2   # the PDK store under home rides the home mount
    assert argv[argv.index("-w") + 1] == "/mnt/c/proj" and "python3" in argv


def test_probe_answer_is_read_from_its_marker_line():
    out = "LibreLane says hello\n[WARNING] x\n\nLANEX_CHECK {\"ok\": true, \"design_name\": \"spm\"}\n"
    assert cc._parse_probe(out) == {"ok": True, "design_name": "spm"}
    assert cc._parse_probe("no marker") is None


def test_real_probe_in_this_python(tmp_path: Path):
    """The PROBE source itself, run the way a local run would run it. Skipped
    where this Python can't load LibreLane configs (no tkinter, or no PDK)."""
    pytest.importorskip("librelane.flows")
    try:
        import tkinter  # noqa: F401
    except Exception:
        pytest.skip("this Python has no tkinter, which LibreLane's config loader needs")
    from lanex.controller import pdk as pdk_mod
    pf = pdk_mod.check_pdk_ready("sky130A", "sky130_fd_sc_hd", "local")
    if not pf.get("ready"):
        pytest.skip("sky130A is not installed here")
    cf = _project(tmp_path, FP_CORE_UTIL="abc")
    r = cc.check(design_dir=str(tmp_path), config_files=[str(cf)], pdk="sky130A", scl="sky130_fd_sc_hd",
                 pdk_ready=pf, run_mode="local", flow=cc.flow_info([str(cf)]))
    assert r["blocking"] and "FP_CORE_UTIL" in r["problems"][0]["message"]
