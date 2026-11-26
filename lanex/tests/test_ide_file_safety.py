# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The RTL IDE's saves never lose work on disk (audit G3, G4), keep the
file's line endings and mode, and the syntax check and simulation read what
the flow reads (plan 1e), with a Verilator timescale and a chosen time limit."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from lanex.controller import editor, lint, simulate


# ---------------------------------------------------------------- G3: hashes

def test_save_with_the_read_hash_writes(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("DESIGN_NAME: top\nCLOCK_PERIOD: 10\n")
    h = editor.disk_hash(tmp_path / "config.yaml")
    r = editor.write_text(tmp_path, "config.yaml", "DESIGN_NAME: top\nCLOCK_PERIOD: 12\n", base_hash=h)
    assert r["ok"], r
    assert r["hash"] == editor.disk_hash(tmp_path / "config.yaml")


def test_save_over_a_newer_file_is_refused(tmp_path: Path):
    """config.yaml open in a tab; Setup changes the clock; Ctrl+S in the IDE
    must not put the old clock back."""
    p = tmp_path / "config.yaml"
    p.write_text("DESIGN_NAME: top\nCLOCK_PERIOD: 10\n")
    opened = editor.disk_hash(p)
    p.write_text("DESIGN_NAME: top\nCLOCK_PERIOD: 25\n")        # Setup's edit
    r = editor.write_text(tmp_path, "config.yaml", "DESIGN_NAME: top\nCLOCK_PERIOD: 10\n# note\n", base_hash=opened)
    assert r["ok"] is False and r["conflict"] is True
    assert r["hash"] == editor.disk_hash(p)
    assert "CLOCK_PERIOD: 25" in p.read_text(), "the newer file is untouched"
    # Keep mine: the person chose to overwrite.
    r2 = editor.write_text(tmp_path, "config.yaml", "DESIGN_NAME: top\nCLOCK_PERIOD: 10\n", base_hash=opened, force=True)
    assert r2["ok"] and "CLOCK_PERIOD: 10" in p.read_text()


def test_hash_matches_what_the_read_endpoint_returns(tmp_path: Path):
    from lanex.controller import fsbrowser
    p = tmp_path / "a.v"
    p.write_bytes(b"module a;\r\nendmodule\r\n")
    res = fsbrowser.read_text(str(p))
    assert editor.text_hash(res["text"]) == editor.disk_hash(p)


# ---------------------------------------------------------------- CRLF + mode

def test_save_keeps_crlf_line_endings(tmp_path: Path):
    p = tmp_path / "src" / "a.v"
    p.parent.mkdir()
    p.write_bytes(b"module a;\r\n  wire x;\r\nendmodule\r\n")
    h = editor.disk_hash(p)
    # The editor works in LF; the file stays CRLF.
    r = editor.write_text(tmp_path, "src/a.v", "module a;\n  wire y;\nendmodule\n", base_hash=h)
    assert r["ok"] and r["eol"] == "crlf"
    assert p.read_bytes() == b"module a;\r\n  wire y;\r\nendmodule\r\n"
    # And the next save based on the hash it returned goes through.
    assert editor.write_text(tmp_path, "src/a.v", "module a;\nendmodule\n", base_hash=r["hash"])["ok"]


def test_save_keeps_lf_files_lf(tmp_path: Path):
    p = tmp_path / "a.v"
    p.write_bytes(b"module a;\nendmodule\n")
    editor.write_text(tmp_path, "a.v", "module b;\nendmodule\n")
    assert b"\r" not in p.read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_save_keeps_the_file_mode(tmp_path: Path):
    p = tmp_path / "run.tcl"
    p.write_text("puts hi\n")
    os.chmod(p, 0o755)
    assert editor.write_text(tmp_path, "run.tcl", "puts bye\n")["ok"]
    assert stat.S_IMODE(p.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_new_file_gets_the_umask_mode_not_0600(tmp_path: Path):
    old = os.umask(0o022)
    try:
        assert editor.write_text(tmp_path, "src/new.v", "module n; endmodule\n")["ok"]
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "src" / "new.v").stat().st_mode) == 0o644


# ---------------------------------------------------------------- G4: Add files

def test_add_refuses_a_same_named_file_and_suggests_a_free_name(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.v").write_text("module top; endmodule // mine\n")
    r = editor.write_text(tmp_path, "src/top.v", "module top; endmodule // theirs\n", on_exists="error")
    assert r["ok"] is False and r["exists"] is True and r["suggested"] == "src/top_2.v"
    assert "mine" in (tmp_path / "src" / "top.v").read_text()


def test_add_keep_both_saves_next_to_it(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.v").write_text("mine\n")
    (tmp_path / "src" / "top_2.v").write_text("older copy\n")
    r = editor.write_text(tmp_path, "src/top.v", "theirs\n", on_exists="keep_both")
    assert r["ok"] and r["path"] == "src/top_3.v"
    assert (tmp_path / "src" / "top.v").read_text() == "mine\n"
    assert (tmp_path / "src" / "top_3.v").read_text() == "theirs\n"


def test_add_replace_overwrites(tmp_path: Path):
    (tmp_path / "notes.md").write_text("old\n")
    assert editor.write_text(tmp_path, "notes.md", "new\n", on_exists="replace")["ok"]
    assert (tmp_path / "notes.md").read_text() == "new\n"


# ---------------------------------------------------------------- the route

class _H:
    def __init__(self, body):
        self._body = body
        self.command = "POST"
        self.sent = None

    def _send_json(self, obj, code=200):
        self.sent = (code, obj)

    def _send_text(self, text, code=200):
        self.sent = (code, text)


@pytest.fixture()
def _design(tmp_path, monkeypatch):
    from lanex.server import routes
    monkeypatch.setattr(routes, "_get_active_design_dir", lambda: str(tmp_path))
    return tmp_path


def test_route_answers_409_on_a_stale_save(_design: Path):
    from lanex.server import routes
    p = _design / "config.yaml"
    p.write_text("CLOCK_PERIOD: 10\n")
    h = editor.disk_hash(p)
    p.write_text("CLOCK_PERIOD: 25\n")
    hd = _H({"rel_path": "config.yaml", "content": "CLOCK_PERIOD: 10\n", "base_hash": h})
    routes.h_file_write(hd)
    code, body = hd.sent
    assert code == 409 and body["conflict"] is True and body["ok"] is False
    assert p.read_text() == "CLOCK_PERIOD: 25\n"


def test_route_answers_409_when_add_would_replace(_design: Path):
    from lanex.server import routes
    (_design / "a.v").write_text("x\n")
    hd = _H({"rel_path": "a.v", "content": "y\n", "on_exists": "error"})
    routes.h_file_write(hd)
    code, body = hd.sent
    assert code == 409 and body["exists"] is True and body["suggested"] == "a_2.v"


def test_read_text_route_returns_the_hash(_design: Path, monkeypatch):
    from lanex.server import routes
    p = _design / "a.v"
    p.write_text("module a; endmodule\n")
    hd = _H({})
    hd.path = "/api/read-text?path=" + str(p)
    routes.h_read_text(hd)
    code, body = hd.sent
    assert code == 200 and body["data"]["hash"] == editor.disk_hash(p)


# ---------------------------------------------------------------- 1e: config

def _project(tmp_path: Path, cfg: dict) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "src" / "old").mkdir()
    (tmp_path / "src" / "old" / "top_copy.v").write_text("module top; endmodule\n")   # a stale copy
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "defs.vh").write_text("`define W 8\n")
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return tmp_path


def test_config_inputs_follow_the_config(tmp_path: Path):
    d = _project(tmp_path, {"DESIGN_NAME": "top", "VERILOG_FILES": "dir::src/*.v",
                            "VERILOG_DEFINES": ["SYNTH", "W=8"], "VERILOG_INCLUDE_DIRS": ["dir::inc"]})
    ci = lint.config_inputs(d)
    assert ci["mode"] == "list"
    assert ci["files"] == ["src/top.v"], "the stale copy in src/old/ isn't read by the flow"
    assert ci["defines"] == {"SYNTH": None, "W": "8"}
    assert ci["include_dirs"] == ["inc"]
    argv = lint.build_lint_command(d, sources=ci["files"], include_dirs=ci["include_dirs"], defines=ci["defines"])
    assert "-DSYNTH" in argv and "-DW=8" in argv and "-Iinc" in argv
    assert "src/old/top_copy.v" not in argv


def test_linter_defines_win_for_the_lint(tmp_path: Path):
    d = _project(tmp_path, {"VERILOG_FILES": ["dir::src/top.v"], "VERILOG_DEFINES": ["A"], "LINTER_DEFINES": ["B"]})
    # The lint also gets the defines LibreLane's own lint step always adds.
    assert lint.config_inputs(d, for_lint=True)["defines"] == {"__librelane__": None, "__pnr__": None, "B": None}
    assert lint.config_inputs(d)["defines"] == {"A": None}


def test_lint_reads_like_the_flows_lint(tmp_path: Path):
    """PDK_/SCL_ defines and LINTER_DISABLE_WARNINGS, as steps/verilator.py passes them."""
    d = _project(tmp_path, {"VERILOG_FILES": ["dir::src/top.v"], "PDK": "sky130A", "STD_CELL_LIBRARY": "sky130_fd_sc_hd",
                            "LINTER_DISABLE_WARNINGS": ["UNUSEDSIGNAL", "bad rule"]})
    ci = lint.config_inputs(d, for_lint=True)
    assert {"PDK_sky130A", "SCL_sky130_fd_sc_hd", "__librelane__"} <= set(ci["defines"])
    assert ci["disable_warnings"] == ["UNUSEDSIGNAL"]
    argv = lint.build_lint_command(d, sources=ci["files"], defines=ci["defines"], disable_warnings=ci["disable_warnings"])
    assert "-Wno-UNUSEDSIGNAL" in argv and "-DPDK_sky130A" in argv


def test_config_inputs_report_missing_files(tmp_path: Path):
    d = _project(tmp_path, {"VERILOG_FILES": ["dir::src/top.v", "dir::src/gone.v"]})
    ci = lint.config_inputs(d)
    assert ci["files"] == ["src/top.v"] and ci["missing"] == ["src/gone.v"]


def test_config_inputs_without_a_file_list_fall_back(tmp_path: Path):
    d = _project(tmp_path, {"DESIGN_NAME": "top"})
    assert lint.config_inputs(d)["mode"] == "none"
    (d / "config.json").unlink()
    assert lint.config_inputs(d)["mode"] == "none"


def test_merge_adds_benches_and_drops_the_testbench_itself(tmp_path: Path):
    ci = {"files": ["src/top.v", "verify/top_tb.v"], "include_dirs": ["inc"], "defines": {"A": None}}
    m = lint.merge_inputs(ci, extra_sources=["verify/bus_model.v", "./src/top.v"], exclude=["verify/top_tb.v"],
                          include_dirs=[".", "inc"], defines={"SIM": "1"})
    assert m["sources"] == ["src/top.v", "verify/bus_model.v"]
    assert m["include_dirs"] == ["inc", "."]
    assert m["defines"] == {"A": None, "SIM": "1"}


def test_lint_route_uses_the_config(_design: Path, monkeypatch):
    from lanex.server import routes
    from lanex.controller import platform_env
    _project(_design, {"VERILOG_FILES": "dir::src/*.v", "VERILOG_DEFINES": ["SYNTH"]})
    seen = {}
    monkeypatch.setattr(platform_env, "usable_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(lint, "verilator_major", lambda exe="verilator": 5)
    monkeypatch.setattr(lint.job, "start", lambda argv, **kw: seen.setdefault("argv", argv) and {"ok": True})
    hd = _H({"run_mode": "local", "from_config": True, "sources": ["src/top.v", "src/old/top_copy.v"],
             "extra_sources": ["verify/top_tb.v"], "include_dirs": ["."]})
    routes.h_lint(hd)
    code, body = hd.sent
    assert code == 200, body
    argv = seen["argv"]
    assert "src/old/top_copy.v" not in argv and "src/top.v" in argv and "verify/top_tb.v" in argv
    assert "-DSYNTH" in argv and "--timing" in argv
    assert body["data"]["config"]["mode"] == "list"


# ---------------------------------------------------------------- simulation

def _sim_route(_design: Path, monkeypatch, body: dict, *, host: set, container: bool):
    from lanex.server import routes
    from lanex.controller import platform_env
    seen = {}
    monkeypatch.setattr(platform_env, "usable_which", lambda name: ("/usr/bin/" + name) if name in host | {"bash"} else None)
    monkeypatch.setattr(routes.tools_mod, "resolve_engine",
                        lambda: {"ready": container, "engine": "docker", "env": {}})
    monkeypatch.setattr(simulate.job, "start", lambda argv, **kw: (seen.update(argv=argv, **kw), {"ok": True})[1])
    hd = _H(dict({"testbench": "verify/top_tb.v", "top": "top_tb", "sources": ["src/top.v", "src/old/top_copy.v"]}, **body))
    routes.h_sim_start(hd)
    return hd.sent, seen


def test_sim_route_uses_the_config_and_the_time_limit(_design: Path, monkeypatch):
    _project(_design, {"VERILOG_FILES": "dir::src/*.v", "VERILOG_DEFINES": ["SIM_ME"]})
    (sent, seen) = _sim_route(_design, monkeypatch, {"from_config": True, "timeout": 600, "sim_engine": "verilator",
                                                    "extra_sources": ["verify/model.v"], "testbenches": ["verify/top_tb.v", "verify/b_tb.v"]},
                              host={"verilator"}, container=False)
    code, body = sent
    assert code == 200, body
    shell = seen["argv"][-1]
    assert "src/old/top_copy.v" not in shell and "src/top.v" in shell and "verify/model.v" in shell
    assert "-DSIM_ME" in shell and "--timescale 1ns/1ps" in shell
    assert "verify/b_tb.v" not in shell
    assert seen["timeout"] == 600 and body["data"]["timeout"] == 600


def test_sim_without_host_tools_uses_the_images_icarus(_design: Path, monkeypatch):
    _project(_design, {"VERILOG_FILES": "dir::src/*.v"})
    (sent, seen) = _sim_route(_design, monkeypatch, {"sim_engine": "auto"}, host=set(), container=True)
    assert sent[0] == 200 and sent[1]["data"]["sim_engine"] == "iverilog"
    assert seen["argv"][0] == "docker" and "iverilog" in seen["argv"][-1]
    # Verilator asked for, none on the host: a plain refusal, not a build that
    # dies inside the image for want of make.
    (sent, seen) = _sim_route(_design, monkeypatch, {"sim_engine": "verilator"}, host=set(), container=True)
    assert sent[0] == 400 and "Verilator on this machine" in sent[1]["error"]


def test_verilator_sim_sets_a_default_timescale():
    argv = simulate.build_sim_command("/d", top="tb", sources=["a.v"], testbench="tb.v", run_mode="local")
    assert "--timescale 1ns/1ps" in argv[-1]
    # Its build folder exists before Verilator writes into it.
    assert argv[-1].index("mkdir -p sim_build") < argv[-1].index("verilator")


def test_sim_time_limit_is_clamped():
    assert simulate.sim_timeout(None) == simulate.DEFAULT_SIM_TIMEOUT
    assert simulate.sim_timeout("junk") == simulate.DEFAULT_SIM_TIMEOUT
    assert simulate.sim_timeout(600) == 600
    assert simulate.sim_timeout(1) == simulate.MIN_SIM_TIMEOUT
    assert simulate.sim_timeout(10 ** 6) == simulate.MAX_SIM_TIMEOUT
