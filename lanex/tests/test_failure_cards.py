"""Failure cards for the errors users hit most, each on the text LibreLane
3.0.4 really printed.

``goldens/failures/librelane-3.0.4.json`` holds, per case, what a forced
failure on a scratch copy of LibreLane's spm example left behind (made with
LibreLane 3.0.4 in its own container image, 2026-11-22): the step folders, the
stopped step's log tail, error.log, warning.log's codes and the config. Each
test rebuilds that run folder and reads the card Flow would show."""

import json
from pathlib import Path

import pytest

from lanex.controller import problems, runexplain

FIX = json.loads((Path(__file__).parent / "goldens" / "failures" / "librelane-3.0.4.json").read_text(encoding="utf-8"))["cases"]


def _run(tmp: Path, case: str, *, cfg_extra=None, outcome=None) -> Path:
    rec = FIX[case]
    run = tmp / "proj" / "runs" / case
    run.mkdir(parents=True)
    for d in rec["steps"]:
        sd = run / d
        sd.mkdir()
        (sd / "state_in.json").write_text("{}")
        if d != rec["stopped"] and d not in rec.get("deferred", []):
            (sd / "state_out.json").write_text(json.dumps({"metrics": rec.get("metrics") or {}}))
        else:
            if rec.get("step_log_name") and rec.get("step_log_tail"):
                (sd / rec["step_log_name"]).write_text("\n".join(rec["step_log_tail"]) + "\n", encoding="utf-8")
            if rec.get("process_stats"):
                (sd / (d.split("-", 1)[1] + ".process_stats.json")).write_text(json.dumps(rec["process_stats"]))
    if rec.get("lint_log_tail"):
        (run / "01-verilator-lint" / "verilator-lint.log").write_text("\n".join(rec["lint_log_tail"]) + "\n", encoding="utf-8")
    if rec["final"]:
        (run / "final").mkdir()
    cfg = dict(rec["cfg"], DESIGN_DIR=rec["design_dir"], **(cfg_extra or {}))
    (run / "resolved.json").write_text(json.dumps(cfg))
    (run / "error.log").write_text(rec["error_log"] or "", encoding="utf-8")
    (run / "warning.log").write_text("\n".join(rec.get("warnings", {}).values()) + "\n", encoding="utf-8")
    if outcome:
        (run / "gui-run.json").write_text(json.dumps({"outcome": {"result": "error", "error": outcome}}))
    return run


def _card(tmp, case, **kw):
    rx = runexplain.explain_run(_run(tmp, case, **kw))
    assert rx["kind"] == "halted", rx["kind"]
    return rx, rx["card"]


def _sets(card):
    return {f["var"]: f for f in card["fixes"] if f["kind"] == "setting"}


@pytest.fixture(autouse=True)
def _known_vars(monkeypatch):
    # finish() keeps only settings this LibreLane knows: the real list when
    # librelane is importable, else every name passes.
    from lanex.controller import introspect
    names = introspect.known_var_names() or set()
    if not names:
        monkeypatch.setattr(introspect, "known_var_names", lambda: set())


def test_every_case_in_the_fixture_is_covered():
    assert set(FIX) == {"verilator_syntax", "yosys_syntax", "verilator_top", "yosys_top", "clock_port", "no_clock",
                        "core_negative", "core_outside", "core_bigger", "pins", "pdn_small_die", "gpl_util", "dpl_cts",
                        "route_drc", "cts_buffer", "rsz_no_buffers", "oom_openroad", "oom_container", "disk_full",
                        "lvs_crash",
                        # forced 2026-11-26 with scripts/failure_goldens.py
                        "latch", "modmissing", "modmissing_yosys", "include_missing", "implicit_wire", "dup_module",
                        "dup_module_lint", "sv_import", "bad_type", "pdk_missing", "unknown_key", "margin_negative",
                        "pdn_pitch", "pin_order", "rsz_slew", "corner_wildcard",
                        # the SRAM template, one thing broken in its macro overlay
                        "macro_inst", "macro_unplaced", "macro_orient_e", "macro_nopower", "macro_channel",
                        "macro_ihp_halo"}


# ---------------------------------------------------------------- macros (forced 2026-11-26)
# LanEx's SRAM template on sky130A, LibreLane 3.0.4, the overlay LanEx writes
# with one thing broken (scripts/failure_goldens.py macro_*); macro_ihp_halo
# is lane 7's real IHP run with the grid an earlier LanEx wrote.

def test_a_macro_instance_the_rtl_does_not_have_names_the_rtl_s_name(tmp_path):
    rec = FIX["macro_inst"]
    assert rec["error_log"].strip() == "No macro instance u_wrong found."
    src = tmp_path / "rtl" / "sram_demo.v"
    src.parent.mkdir()
    src.write_text((Path(problems.__file__).parent / "templates" / "sram" / "src" / "sram_demo.v").read_text())
    rx, c = _card(tmp_path, "macro_inst", cfg_extra={"VERILOG_FILES": [str(src)]})
    assert rx["step"]["dir"] == "11-openroad-checkmacroinstances"
    assert c["kb"] == "macro-instance" and c["title"] == "u_wrong isn't an instance in your RTL"
    assert "your RTL names it u_sram" in c["plain"] and "never the macro's cell name" in c["plain"]
    assert c["fixes"][0]["go"] == {"tab": "cells", "section": "cl-macros"}
    assert "Rename u_wrong to u_sram" in c["fixes"][0]["why"]


def test_a_macro_instance_card_without_the_rtl_still_says_what_to_use(tmp_path):
    _rx, c = _card(tmp_path, "macro_inst")          # the RTL path of the golden isn't on disk
    assert c["title"] == "u_wrong isn't an instance in your RTL"
    assert "LibreLane found no instance by that name" in c["plain"]


def test_an_unplaced_macro_reads_tap_and_pdn_and_says_give_it_a_position(tmp_path):
    rec = FIX["macro_unplaced"]
    assert rec["error_log"].strip() == "[PDN-0235] Design has unplaced macros."
    assert rec["warnings"]["TAP-0032"].endswith("Macro u_sram is not placed.")
    rx, c = _card(tmp_path, "macro_unplaced")
    assert rx["step"]["dir"] == "21-openroad-generatepdn"
    assert c["kb"] == "pdn-0235" and c["title"] == "u_sram has no position"
    assert 'TAP-0032 "Macro u_sram is not placed"' in c["plain"]
    assert c["fixes"][0]["label"] == "Give u_sram a position"


def test_a_macro_turned_90_degrees_says_use_n_s_fn_or_fs(tmp_path):
    rec = FIX["macro_orient_e"]
    assert "[PDN-0233] Failed to generate full power grid." in rec["error_log"]
    assert 'The grid "macro - u_sram" (Instance) does not contain any shapes or vias.' in rec["warnings"]["PDN-0232"]
    _rx, c = _card(tmp_path, "macro_orient_e")
    assert c["kb"] == "pdn-0233" and c["title"] == "u_sram is turned 90° (E), so no power strap reaches it"
    assert "Use N, S, FN or FS." in c["plain"] and "met5 straps over its met4 pins" in c["plain"]


def test_unhooked_macro_power_pins_stop_at_the_disconnected_pins_check(tmp_path):
    rec = FIX["macro_nopower"]
    # Only warnings at the power grid; the run routes, Checker.TrDRC defers its
    # error, and Checker.DisconnectedPins stops it.
    assert "PDN-0231" in rec["warnings"] and "Supply pin u_sram/" in rec["warnings"]["PDN-0189"]
    assert rec["stopped"] == "49-checker-disconnectedpins" and rec["deferred"] == ["47-checker-trdrc"]
    assert "1 critical disconnected pins found." in rec["error_log"]
    rx, c = _card(tmp_path, "macro_nopower")
    assert rx["step"]["dir"] == "49-checker-disconnectedpins", rx["step"]
    assert c["kb"] == "macro-power" and c["title"] == "u_sram's power pins aren't hooked up"
    assert "(vccd1" in c["plain"] and "PDN-0231" in c["plain"]
    assert c["fixes"][0]["label"] == "Hook up its power pins"


def test_a_disconnected_pin_without_macro_warnings_gets_the_plain_card():
    c = problems.explain_error("2 critical disconnected pins found.", {})
    assert c["kb"] == "macro-power" and c["title"] == "A pin is connected to nothing"
    assert "2 critical pins" in c["plain"]


def test_a_narrow_strip_of_rows_by_a_macro_says_twenty_microns(tmp_path, monkeypatch):
    rec = FIX["macro_channel"]
    assert rec["error_log"].strip() == "[DPL-0036] Detailed placement failed."
    assert rec["metrics"]["design__core__bbox"].split()[0] == "5.52"
    # The size the SRAM's LEF gives (read on the PC that forced the run).
    monkeypatch.setattr(problems, "_macro_size", lambda cfg, mod: (479.78, 397.5))
    rx, c = _card(tmp_path, "macro_channel")
    assert rx["step"]["dir"] == "32-openroad-repairdesignpostgpl"
    assert c["kb"] == "dpl-0036" and c["title"] == "The rows beside u_sram are too narrow"
    assert "a 7.02 µm strip of cell rows on its left" in c["plain"]
    assert "Leave at least 20 µm of rows beside a macro, or none" in c["plain"]
    assert c["fixes"][0]["label"] == "Move u_sram"


def test_dpl_0036_without_a_narrow_strip_keeps_the_placement_card(tmp_path, monkeypatch):
    monkeypatch.setattr(problems, "_macro_size", lambda cfg, mod: (479.78, 397.5))
    _rx, c = _card(tmp_path, "macro_channel", cfg_extra={"FP_MACRO_HORIZONTAL_HALO": 30})
    assert c["title"] != "The rows beside u_sram are too narrow"


def test_an_unconnected_power_grid_names_the_net_and_the_halo(tmp_path):
    rec = FIX["macro_ihp_halo"]
    assert rec["error_log"].startswith("[PSM-0069] Check connectivity failed on VGND.")
    rx, c = _card(tmp_path, "macro_ihp_halo")
    assert rx["step"]["dir"] == "56-openroad-irdropreport"
    assert c["kb"] == "psm-0069" and c["title"] == "Part of the VGND grid isn't connected"
    assert "PSM-0038; the first is a Metal1 piece at (328.8, 67.82) µm" in c["plain"]
    assert "keep-out halo" in c["plain"] and "running again fixes it" in c["plain"]
    assert any(f["go"].get("box") == "328.80,67.82,394.08,68.26" for f in c["fixes"])


def test_the_macro_cards_are_marked_checked_on_a_real_run():
    by = {e["id"]: e for e in problems.KB}
    for k in ("macro-instance", "pdn-0235", "pdn-0233", "macro-power", "psm-0069", "dpl-0036"):
        assert by[k]["source"].startswith("forced:"), k


# ---------------------------------------------------------------- forced 2026-11-26
# Each on a scratch copy of the Blinky template or LibreLane's spm, LibreLane
# 3.0.4 in its image (scripts/failure_goldens.py makes them again).

def test_a_latch_names_the_signal_and_the_line(tmp_path):
    rx, c = _card(tmp_path, "latch")
    assert c["kb"] == "latch" and c["title"] == "A latch was inferred for q"
    assert "src/blinky.v line 17" in c["plain"] and "LINTER_ERROR_ON_LATCH" in c["plain"]
    assert c["fixes"][0]["go"] == {"tab": "ide", "rel": "src/blinky.v", "line": 17}
    assert rx["at"]["step"] == "Verilator.Lint"


@pytest.mark.parametrize("case", ["modmissing", "modmissing_yosys"])
def test_a_module_nobody_declares(tmp_path, case):
    _rx, c = _card(tmp_path, case)
    assert c["kb"] in ("module-missing", "module-missing-yosys")
    assert c["title"] == "No file defines module blink_sub"
    assert any(f.get("go", {}).get("section") == "setup-sources" for f in c["fixes"])


def test_a_module_in_an_unticked_file_says_which(tmp_path):
    d = tmp_path / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "blink_sub.v").write_text("module blink_sub(input clk, output q);\nendmodule\n")
    c = problems.explain_error("1 Lint errors found.", {"DESIGN_DIR": str(d), "VERILOG_FILES": [str(d / "src" / "blinky.v")]},
                               log="%Error-MODMISSING: src/blinky.v:17:5: Cannot find file containing module: 'blink_sub'")
    assert "src/blink_sub.v declares it but isn't one of the design's files" in c["plain"]


def test_a_missing_include(tmp_path):
    _rx, c = _card(tmp_path, "include_missing")
    assert c["kb"] == "include-missing" and c["title"] == "blink_defs.vh can't be found"
    assert "VERILOG_INCLUDE_DIRS" in c["plain"]


def test_an_undeclared_wire_passes_lint_and_stops_synthesis(tmp_path):
    rx, c = _card(tmp_path, "implicit_wire")
    assert rx["step"]["dir"] == "05-yosys-jsonheader"
    assert c["kb"] == "implicit-wire" and c["title"] == "tmp is used but never declared"
    assert "-noautowire" in c["plain"]
    assert c["fixes"][0]["go"]["line"] == 16


@pytest.mark.parametrize("case", ["dup_module", "dup_module_lint"])
def test_a_module_defined_twice(tmp_path, case):
    _rx, c = _card(tmp_path, case)
    assert c["kb"] == "module-twice" and c["title"] == "Module blinky is defined twice"
    assert "src/blinky_old.v" in c["plain"]


def test_systemverilog_import_offers_the_slang_reader(tmp_path):
    _rx, c = _card(tmp_path, "sv_import")
    assert c["kb"] == "sv-frontend" and "import" in c["title"]
    f = _sets(c)["USE_SLANG"]
    assert (f["from"], f["to"], f["sure"]) == ("false", "true", "likely")


def test_config_load_errors_have_cards():
    # No run folder: LibreLane stopped while loading the config, and printed these.
    assert FIX["bad_type"]["steps"] == [] and FIX["bad_type"]["exit_code"] == 1
    said = " ".join(" ".join(FIX["bad_type"]["console"]).split())
    # (the console wraps the line around the logger's source column)
    assert "Value provided for variable 'FP_CORE_UTIL'" in said and "of type Decimal is invalid: 'abc'" in said
    c = problems.explain_error("Value provided for variable 'FP_CORE_UTIL' of type Decimal is invalid: 'abc'", {})
    assert c["kb"] == "config-value" and c["title"] == "FP_CORE_UTIL has a value LibreLane can't read"
    assert "is a number" in c["plain"]
    p = problems.explain_error("The PDK sky130C was not found.", {})
    assert p["kb"] == "pdk-missing" and p["title"] == "The PDK sky130C isn't installed"
    assert "The PDK sky130C was not found." in " ".join(FIX["pdk_missing"]["console"])


def test_an_unknown_key_is_only_a_warning_so_the_note_names_the_nearest(monkeypatch):
    # LibreLane 3.0.4 runs on: the forced run finished, the setting did nothing.
    rec = FIX["unknown_key"]
    assert rec["final"] and rec["exit_code"] == 0
    assert any("An unknown key 'FP_CORE_UTL' was provided." in ln for ln in rec["console_head"])
    from lanex.controller import introspect
    monkeypatch.setattr(introspect, "known_var_names", lambda: {"FP_CORE_UTIL", "FP_SIZING", "DIE_AREA"})
    n = problems.unknown_key_note("FP_CORE_UTL", "config.json")
    assert n["near"] == ["FP_CORE_UTIL"] and not n["hard"]
    assert "ignored" in n["message"] and "FP_CORE_UTIL" in n["fix"]


def test_a_negative_margin_goes_back_to_the_default(tmp_path):
    _rx, c = _card(tmp_path, "margin_negative")
    assert c["code"] == "IFP-0013" and c["title"] == "A core margin is negative"
    f = _sets(c)["LEFT_MARGIN_MULT"]
    assert (f["from"], f["to"], f["sure"]) == ("-1", "12", "fixes")


def test_pdn_0175_widens_the_pitch_it_names(tmp_path, monkeypatch):
    monkeypatch.setattr(problems, "_pdk_default", lambda cfg, var: None)
    _rx, c = _card(tmp_path, "pdn_pitch")
    assert c["code"] == "PDN-0175" and c["title"] == "The power straps don't fit in their pitch"
    assert "PDN_VPITCH is 3 µm" in c["plain"] and "7.4 µm" in c["plain"]
    f = _sets(c)["PDN_VPITCH"]
    assert (f["to"], f["sure"]) == ("8", "fixes")


def test_a_pin_the_pin_order_file_leaves_out(tmp_path):
    rx, c = _card(tmp_path, "pin_order")
    assert rx["step"]["dir"] == "26-odb-customioplacement"
    assert c["kb"] == "pin-order" and c["title"] == "pin_order.cfg doesn't place clk"
    assert c["fixes"][0]["go"]["rel"] == "pin_order.cfg"
    assert _sets(c)["ERRORS_ON_UNMATCHED_IO"]["to"] == "none"


def test_rsz_0090_goes_back_to_the_pdks_slew_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(problems, "_pdk_default", lambda cfg, var: 0.75 if var == "MAX_TRANSITION_CONSTRAINT" else None)
    _rx, c = _card(tmp_path, "rsz_slew")
    assert c["code"] == "RSZ-0090" and "0.01 ns" in c["plain"] and "0.033 ns" in c["plain"]
    f = _sets(c)["MAX_TRANSITION_CONSTRAINT"]
    assert (f["from"], f["to"]) == ("0.01", "0.75")


def test_a_corner_pattern_that_matches_nothing():
    c = problems.explain_error("One or more wildcards specified in SETUP_VIOLATION_CORNERS did not match any corners:", {})
    assert c["kb"] == "corner-wildcard" and "SETUP_VIOLATION_CORNERS" in c["title"]
    assert FIX["corner_wildcard"]["final"], "the run reaches final/ and fails at the end"


def test_codes_that_share_a_card():
    for code in ("CTS-0126", "CTS-0074"):
        c = problems.explain_error("[%s] No physical master cell found for buffer sky130_fd_sc_hd__x_4." % code, {})
        assert c["kb"] == "cts-0127", code
    for code in ("GRT-0228", "GRT-0229"):
        assert problems.explain_error("[%s] Horizontal edge usage exceeds the maximum allowed." % code, {})["kb"] == "grt-0116"


def test_the_pdn_and_routing_codes_only_known_from_source():
    unplaced = problems.explain_error("[PDN-0235] Design has unplaced macros.", {},
                                      log="[WARNING PDN-0234] u_sram has not been placed and fixed.")
    assert unplaced["title"] == "u_sram has no position"
    empty = problems.explain_error("[PDN-0233] Failed to generate full power grid.", {},
                                   log='[WARNING PDN-0232] The grid "macro_grid" (Core) does not contain any shapes or vias.')
    assert "macro_grid" in empty["title"]
    ring = problems.explain_error("[PDN-0351] PDN rings do not fit inside the die area by 5.4 um in X and 5.4 um in Y.",
                                  {"PDN_CORE_RING": True})
    assert _sets(ring)["PDN_CORE_RING"]["to"] == "false"
    acc = problems.explain_error("[DRT-0073] No access point for _123_/A (sky130_fd_sc_hd__nand2_1).", {})
    assert acc["title"] == "The router can't reach pin _123_/A"


def test_every_kb_entry_has_its_index_line():
    for rule in problems.KB:
        assert rule.get("about") and rule.get("stage") and rule.get("source"), rule["id"]
    ids = [r["id"] for r in problems.KB]
    assert len(ids) == len(set(ids))
    # The coded Verilator and Yosys rules come before the general one.
    assert ids.index("latch") < ids.index("verilog-error") and ids.index("implicit-wire") < ids.index("verilog-error")


def test_a_syntax_error_names_the_file_and_line_from_verilator(tmp_path):
    rx, c = _card(tmp_path, "verilator_syntax")
    assert c["kb"] == "verilog-error"
    assert c["title"] == "Syntax error in src/spm.v, line 67"
    assert "unexpected IDENTIFIER" in c["plain"] and "semicolon" in c["plain"]
    assert c["evidence"] == ["%Error: src/spm.v:67:13: syntax error, unexpected IDENTIFIER"]
    # The log link is Verilator's, not the checker's (which writes none).
    assert rx["at"]["step"] == "Verilator.Lint" and rx["at"]["line"] == 1
    assert c["fixes"][0]["go"] == {"tab": "ide", "rel": "src/spm.v", "line": 67}


def test_a_syntax_error_yosys_finds_reads_the_same(tmp_path):
    rx, c = _card(tmp_path, "yosys_syntax")
    assert c["title"] == "Syntax error in src/spm.v, line 67"
    assert c["plain"].startswith("Yosys stopped reading src/spm.v at line 67: syntax error, unexpected TOK_ID")
    # error.log wraps the line in LibreLane's "Subprocess had a non-zero exit": the card shows Yosys's own.
    assert c["evidence"] == ["src/spm.v:67: ERROR: syntax error, unexpected TOK_ID"]
    assert rx["at"]["path"] == "01-yosys-jsonheader/yosys-jsonheader.log"
    assert "syntax error" in rx["at"]["text"]


@pytest.mark.parametrize("case", ["verilator_top", "yosys_top"])
def test_a_wrong_top_module_offers_the_one_module_nobody_uses(tmp_path, case):
    src = tmp_path / "spm.v"
    src.write_text("module spm(input clk, input rst, input x, output y);\n  delayed_serial_adder dsa(.clk(clk));\n"
                   "endmodule\nmodule delayed_serial_adder(input clk);\nendmodule\n")
    rx, c = _card(tmp_path, case, cfg_extra={"VERILOG_FILES": [str(src)]})
    assert c["kb"] == "top-missing"
    assert c["title"] == "There is no module called spm_top"
    assert "spm, delayed_serial_adder" in c["plain"]
    f = _sets(c)["DESIGN_NAME"]
    assert (f["from"], f["to"]) == ("spm_top", "spm")


def test_a_missing_submodule_is_not_called_a_wrong_top(tmp_path):
    c = problems.explain_error("ERROR: Module `adder' not found!", {"DESIGN_NAME": "spm"})
    assert c.get("kb") != "top-missing"


def test_missing_source_files_and_empty_globs_are_told_apart():
    # LibreLane stops while loading the config (no run folder): these are the
    # words it printed, for the run-start check and the Flow page to use.
    a = problems.explain_error("Path provided for variable 'VERILOG_FILES[0]' is invalid: "
                               "'/work/missing/src/spm_core.v' does not exist", {"DESIGN_DIR": "/work/missing"})
    assert a["kb"] == "path-missing" and a["title"] == "A file in VERILOG_FILES is missing"
    assert "src/spm_core.v" in a["plain"]
    b = problems.explain_error("Path provided for variable 'VERILOG_FILES[0]' is invalid: "
                               "'/work/glob/rtl/*.v' does not exist", {"DESIGN_DIR": "/work/glob"})
    assert b["title"] == "No file matches rtl/*.v"


def test_too_many_pins_grows_a_fixed_die_to_the_perimeter_openroad_asks_for(tmp_path):
    _rx, c = _card(tmp_path, "pins")
    assert c["code"] == "PPL-0024" and c["title"] == "Too many pins for the die's edge"
    assert "602 pins" in c["plain"] and "room for 206" in c["plain"] and "819 µm" in c["plain"]
    f = _sets(c)["DIE_AREA"]
    # 818.72 um around -> about 205 um a side, with 5 % to spare -> 215.
    assert (f["from"], f["to"]) == ("0 0 60 60", "0 0 215 215")


def test_too_many_pins_on_a_relative_die_lowers_the_utilisation():
    c = problems.explain_error("[PPL-0024] Number of IO pins (602) exceeds maximum number of available positions (206). "
                               "Increase the die perimeter from 240.00um to 818.72um.",
                               {"FP_SIZING": "relative", "FP_CORE_UTIL": 50})
    f = _sets(c)["FP_CORE_UTIL"]
    assert f["from"] == "50" and 1 <= int(f["to"]) < 5


def test_a_die_smaller_than_its_margins(tmp_path):
    _rx, c = _card(tmp_path, "core_negative")
    assert c["code"] == "STA-0572" and c["kb"] == "ifp-margins"
    assert c["title"] == "The die is too small for its margins"
    s = _sets(c)
    assert s["DIE_AREA"]["to"] == "0 0 60 60" and s["FP_SIZING"]["to"] == "relative"


def test_a_core_area_with_its_corners_swapped(tmp_path):
    _rx, c = _card(tmp_path, "core_outside")
    assert c["code"] == "IFP-0002" and c["title"] == "The core is too small for a single cell"
    assert "5 × 5 µm" in c["plain"] and "CORE_AREA (10 10 5 5" in c["plain"]
    assert not _sets(c), "no setting is safe to guess for a CORE_AREA typo"


def test_a_core_area_bigger_than_the_die(tmp_path):
    _rx, c = _card(tmp_path, "core_bigger")
    assert c["code"] == "IFP-0055" and c["title"] == "The core is outside the die"


def test_pdn_0185_reads_the_numbers_and_grows_a_fixed_die(tmp_path):
    _rx, c = _card(tmp_path, "pdn_small_die")
    assert "10.7 µm" in c["plain"] and "8.16 µm" in c["plain"] and "met5" in c["plain"]
    s = _sets(c)
    assert s["DIE_AREA"]["to"] == "0 0 40 40" and s["FP_SIZING"]["to"] == "relative"


def test_pdn_0179_is_about_the_gaps_between_macros():
    c = problems.explain_error("[PDN-0179] Unable to repair all channels.", {})
    assert c["title"] == "The power grid can't get between the macros"
    assert all(f["kind"] == "open" for f in c["fixes"])


def test_detailed_placement_inside_cts_says_the_clock_tree_had_no_room(tmp_path):
    # The sweep point the audit flagged: the run's stage is cts (OpenROAD.CTS
    # raised), and the card now says it was the clock tree's legalisation.
    rx, c = _card(tmp_path, "dpl_cts")
    assert rx["stage"] == "cts" and rx["step"]["dir"] == "35-openroad-cts"
    assert c["code"] == "DPL-0036" and c["title"] == "No room for the clock tree's buffers"
    assert "2 cells (_138_, _150_)" in c["plain"]
    assert _sets(c)["FP_CORE_UTIL"]["to"] == "60"


def test_global_placement_out_of_room_still_reads_the_same(tmp_path):
    _rx, c = _card(tmp_path, "gpl_util")
    assert c["title"] == "Placement ran out of room" and "172 %" in c["plain"]


def test_a_clock_buffer_the_library_lacks_goes_back_to_the_pdks(tmp_path, monkeypatch):
    monkeypatch.setattr(problems, "_pdk_default", lambda cfg, var: {
        "CTS_CLK_BUFFERS": ["sky130_fd_sc_hd__clkbuf_8", "sky130_fd_sc_hd__clkbuf_4"],
        "CTS_ROOT_BUFFER": "sky130_fd_sc_hd__clkbuf_16"}[var])
    _rx, c = _card(tmp_path, "cts_buffer")
    assert c["code"] == "CTS-0127" and "sky130_fd_sc_hd__nosuchbuf_4" in c["plain"]
    s = _sets(c)
    assert s["CTS_ROOT_BUFFER"]["to"] == "sky130_fd_sc_hd__clkbuf_16"
    assert s["CTS_CLK_BUFFERS"]["to"] == "sky130_fd_sc_hd__clkbuf_8 sky130_fd_sc_hd__clkbuf_4"


def test_no_buffers_names_the_exclusion_list(tmp_path):
    _rx, c = _card(tmp_path, "rsz_no_buffers", cfg_extra={"PNR_EXCLUDED_CELL_FILE": "/work/nobuf.cells"})
    assert c["code"] == "RSZ-0022" and c["title"] == "Every buffer is excluded"
    assert "PNR_EXCLUDED_CELL_FILE" in c["plain"]


def test_an_lvs_crash_is_not_an_lvs_mismatch(tmp_path):
    rx, c = _card(tmp_path, "lvs_crash")
    assert c["kb"] == "lvs-crash" and c["title"] == "LVS didn't finish"
    # Netgen's Tcl error, found by the line before "while executing".
    assert c["evidence"] == ['wrong # args: should be "netgen::readnet ?format? file ?filenum?"']
    assert rx["at"]["line"] > 0


def test_a_tool_killed_for_memory_says_so_with_the_peak(tmp_path):
    _rx, c = _card(tmp_path, "oom_openroad")
    assert c["kb"] == "silent" and c["title"] == "The tool stopped without saying why"
    assert "260 MB" in c["plain"] and "memory" in c["plain"]
    # LanEx's runner records the container's exit code: 137 is the kernel's kill.
    _rx, c2 = _card(tmp_path / "b", "oom_container", outcome="exit code 137")
    assert c2["kb"] == "oom" and c2["title"] == "Out of memory: the system stopped the run"


def test_a_full_disk_is_told_from_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(problems, "disk_free", lambda p: {"free": 0, "total": 4 << 20})
    _rx, c = _card(tmp_path, "disk_full")
    assert c["kb"] == "disk-full" and c["title"] == "The disk is full"
    assert "0 MB of 4 MB" in c["plain"]
    # The words themselves, as local mode records them (OSError from LibreLane).
    c2 = problems.explain_error("", {}, outcome="OSError: [Errno 28] No space left on device")
    assert c2["kb"] == "disk-full"


def test_container_engine_problems_are_told_apart():
    perm = problems.explain_error("permission denied while trying to connect to the docker API at unix:///var/run/docker.sock", {})
    assert perm["title"] == "This user can't use Docker" and "usermod -aG docker" in perm["plain"]
    pull = problems.explain_error("Failed to pull image 'ghcr.io/librelane/librelane:3.0.4' from the container registries.", {})
    assert pull["title"] == "Couldn't get the LibreLane image"
    down = problems.explain_error("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?", {})
    assert down["title"] == "The container engine isn't running"
    none = problems.explain_error("No compatible container engine found.", {})
    assert none["title"] == "No container engine is installed"


def test_anything_else_gets_the_generic_card_with_copy_for_help(tmp_path):
    run = _run(tmp_path, "gpl_util")
    (run / "error.log").write_text("[ERROR ANT-0099] something LanEx has never seen.\n")
    log = run / "28-openroad-globalplacement" / "openroad-globalplacement.log"
    log.write_text("line one\n[ERROR ANT-0099] something LanEx has never seen.\nError: x.tcl, 3 ANT-0099\n")
    rx = runexplain.explain_run(run)
    c = rx["card"]
    assert c["generic"] and c["title"] is None
    assert c["evidence"][0] == "[ERROR ANT-0099] something LanEx has never seen."
    assert "OpenROAD stopped the run at" in c["plain"]
    assert rx["at"]["line"] == 2
    h = c["help"]
    # (no flow.log here, so the step is named by its folder)
    assert "Stopped at step 28 28-openroad-globalplacement (placement)" in h
    assert "Error: [ERROR ANT-0099]" in h and "LibreLane" in h and "PDK sky130A" in h
    assert "Last lines of 28-openroad-globalplacement/openroad-globalplacement.log" in h


def test_every_halted_card_carries_help_text(tmp_path):
    _rx, c = _card(tmp_path, "pins")
    assert c["help"].startswith("LanEx run report: pins")
    assert "/work/pins/" not in c["help"], "paths are relative to the project"


def test_a_clock_port_that_isnt_there_is_a_problem_on_a_clean_run(tmp_path, monkeypatch):
    src = tmp_path / "spm.v"
    src.write_text("module spm(input clk, input rst, input x, output y);\nendmodule\n")
    run = _run(tmp_path, "clock_port", cfg_extra={"VERILOG_FILES": [str(src)]})
    rx = runexplain.explain_run(run)
    assert rx["kind"] == "clean"
    notes = {g["code"]: g.get("note") for g in rx["warnings"]["by_code"]}
    assert "CLOCK_PORT is 'clock'" in notes["STA-0366"] and notes["CTS-0083"]
    p = problems.clock_problems(run, {}, rx)[0]
    assert p["title"] == "The clock port 'clock' isn't in the design" and p["severity"] == "warn"
    f = [x for x in p["fixes"] if x["kind"] == "setting"][0]
    assert (f["var"], f["from"], f["to"]) == ("CLOCK_PORT", "clock", "clk")


def test_no_clock_port_at_all(tmp_path):
    src = tmp_path / "spm.v"
    src.write_text("module spm(input clk, input rst, input x, output y);\nendmodule\n")
    run = _run(tmp_path, "no_clock", cfg_extra={"VERILOG_FILES": [str(src)]})
    rx = runexplain.explain_run(run)
    p = problems.clock_problems(run, {}, rx)[0]
    assert p["title"] == "No clock is set, so nothing was timed against one"


def test_route_drc_after_one_router_pass_is_a_check_not_a_halt(tmp_path):
    rx = runexplain.explain_run(_run(tmp_path, "route_drc"))
    assert rx["kind"] == "checks-failed" and rx["card"] is None


def test_a_design_with_no_clock_is_a_note_not_a_problem(tmp_path):
    # The 7-segment template: no register, no CLOCK_PORT. Its in-to-out paths
    # are timed against the virtual clock, which is what the note says.
    src = tmp_path / "seg7.v"
    src.write_text("module seg7(input [3:0] hex, output reg [6:0] seg);\n"
                   "  always @(*) case (hex) default: seg = 0; endcase\nendmodule\n")
    run = _run(tmp_path, "no_clock", cfg_extra={"VERILOG_FILES": [str(src)], "DESIGN_NAME": "seg7", "CLOCK_PORT": None})
    rx = runexplain.explain_run(run)
    assert problems.clock_problems(run, {}, rx) == []
    note = {g["code"]: g.get("note") for g in rx["warnings"]["by_code"]}["STA-0366"]
    assert note.startswith("A design with no clock: LibreLane timed the paths from the inputs to the outputs")


def test_antenna_advice_follows_the_cause_and_names_the_pdks_diode(tmp_path, monkeypatch):
    src = tmp_path / "top.v"
    src.write_text("module top(input clk, input [7:0] a, output y);\nendmodule\n")
    cfg = {"DESIGN_NAME": "top", "VERILOG_FILES": [str(src)], "PDK": "gf180mcuD", "RUN_ANTENNA_REPAIR": True,
           "DIODE_CELL": "gf180mcu_fd_sc_mcu7t5v0__antenna/I", "GRT_ANTENNA_REPAIR_ITERS": 3,
           "GRT_ANTENNA_REPAIR_MARGIN": 10, "RUN_HEURISTIC_DIODE_INSERTION": False, "GPL_CELL_PADDING": 0,
           "HEURISTIC_ANTENNA_THRESHOLD": 130, "DIODE_ON_PORTS": "none"}
    monkeypatch.setattr(problems, "_cfg", lambda run_dir: cfg)
    rep = {"checks": [{"id": "antenna", "name": "Antenna", "status": "warn", "value": 6}],
           "details": {"antenna": {"rows": [{"net": n} for n in ("a[1]", "n1", "n2", "n3", "n4", "n5")]}}}
    p = problems.check_problems(tmp_path, rep, set())[0]
    s = {f["var"]: f for f in p["fixes"] if f["kind"] == "setting"}
    # A port net: diodes on the inputs, named for this PDK's diode cell.
    assert s["DIODE_ON_PORTS"]["to"] == "in" and "gf180mcu_fd_sc_mcu7t5v0__antenna on gf180mcuD" in s["DIODE_ON_PORTS"]["why"]
    # Many nets: heuristic diodes, with the padding they need.
    h = s["RUN_HEURISTIC_DIODE_INSERTION"]
    assert "130 µm" in h["why"] and "GPL_CELL_PADDING" in h["why"] and h["sure"] == "try"
    assert "GRT_REPAIR_ANTENNAS" not in s and "DRT_ANTENNA_REPAIR_MARGIN" not in s
    assert p["learn"] == ["antenna"]


def test_an_lvs_mismatch_says_what_differs(tmp_path, monkeypatch):
    from lanex.controller import history
    monkeypatch.setattr(problems, "_cfg", lambda run_dir: {})
    monkeypatch.setattr(history, "_load_metrics", lambda run_dir: {
        "design__lvs_error__count": 48, "design__lvs_unmatched_pin__count": 3, "design__lvs_net_difference__count": 1,
        "design__lvs_property_fail__count": 0})
    p = problems.check_problems(tmp_path, {"checks": [{"id": "lvs", "name": "LVS", "status": "fail", "value": 48}]}, set())[0]
    assert p["evidence"] == ["Netgen: 3 pins don't match, 1 net differs."]
    assert p["learn"] == ["lvs"]
