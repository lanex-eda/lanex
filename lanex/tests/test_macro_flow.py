# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The macro flow (lane 7): the floorplan a macro is placed against, the
placement rules, the views LanEx makes (header, relaxed Liberty, per-corner
timing), the power hookup and grid, the run overlay, and the cell library's
descriptions, links and datasheets."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanex.controller import celllib, custommacros as cm, macrolib


# ------------------------------------------------------------------ geometry
def test_core_snaps_like_openroad_on_three_pdks():
    # die minus 12 sites / 4 rows, lower-left up to the grid, whole sites and
    # rows only: the IFP-0101 core of real runs (research 6.4)
    def core(die, sw, sh):
        raw = [die[0] + 12 * sw, die[1] + 4 * sh, die[2] - 12 * sw, die[3] - 4 * sh]
        return macrolib.snap_core(raw, sw, sh)
    assert core([0, 0, 700, 600], 0.46, 2.72) == [5.52, 10.88, 694.14, 587.52]
    assert core([0, 0, 400, 300], 0.48, 3.78) == [5.76, 15.12, 394.08, 283.5]
    assert core([0, 0, 650, 450], 0.56, 3.92) == [6.72, 15.68, 642.88, 431.2]


def test_orientations_from_lef_symmetry_and_transforms():
    assert macrolib.allowed_orients("X Y R90") == ["N", "S", "FN", "FS", "W", "E", "FW", "FE"]
    assert macrolib.allowed_orients("") == ["N"]
    assert macrolib.allowed_orients("X") == ["N", "FS"]
    assert macrolib.footprint(480, 400, "E") == (400, 480)
    assert macrolib.xform_rect([0, 0, 10, 5], 100, 50, "S") == [90, 45, 100, 50]
    assert macrolib.xform_point(10, 5, 100, 50, "E") == (5, 90)


def _model(**over):
    m = {
        "ok": True, "die": {"box": [0, 0, 700, 600], "from": "config"}, "core": {"box": [5.52, 10.88, 694.14, 587.52]},
        "site": {"name": "unithd", "w": 0.46, "h": 2.72}, "mfg": 0.005, "halo": {"h": 10, "v": 10}, "endcap_w": 1.38,
        "pdn": {"vlayer": "met4", "hlayer": "met5", "v": {"w": 1.6, "s": 1.7, "pitch": 153.6, "off": 16.32},
                "h": {"w": 1.6, "s": 1.7, "pitch": 153.18, "off": 16.65}},
        "macros": {
            "sram": {"ok": True, "w": 479.78, "h": 397.5, "grid": "default", "orients": ["N", "S", "FN", "FS"], "symmetry": "X Y R90",
                     "prects": [["vccd1", "power", "met4", 4.76, 0, 6.5, 397.5], ["vssd1", "ground", "met4", 1.36, 0, 3.1, 397.5],
                                ["vccd1", "power", "met4", 473.28, 0, 475.02, 397.5], ["vssd1", "ground", "met4", 476.68, 0, 478.42, 397.5]]},
            "tiny": {"ok": True, "w": 20, "h": 8, "grid": "default", "orients": ["N", "S", "FN", "FS"],
                     "prects": [["VDD", "power", "met4", 1, 0, 2, 8], ["VSS", "ground", "met4", 3, 0, 4, 8]]},
        },
        "instances": [],
    }
    m.update(over)
    return m


def _at(x, y, o="N", name="u_sram", macro="sram"):
    return {"macro": macro, "name": name, "location": [x, y], "orientation": o}


def _codes(insts):
    return sorted(p["code"] + ("!" if p["hard"] else "") for p in macrolib.placement_problems(_model(instances=insts)))


def test_placement_rules_match_the_canvas():
    # the same scenarios as frontend_test.mjs "macros: …", so server and canvas agree
    assert _codes([_at(110.4, 100.64)]) == []
    c = macrolib.crossings(_model(), _at(110.4, 100.64))
    assert c["power"] >= 2 and c["ground"] >= 2
    assert _codes([_at(0, 0)]) == ["outside!"]
    msg = macrolib.placement_problems(_model(instances=[_at(0, 0)]))[0]["message"]
    assert "5.52 µm on the left (the core starts at x = 5.52 µm)" in msg
    assert "overlap!" in _codes([_at(110.4, 100.64), _at(200.1, 100.64, name="u_b")])
    assert "orient!" in _codes([_at(110.4, 100.64, "E")])
    assert _codes([{"macro": "tiny", "name": "u_t", "location": [299.92, 35.36], "orientation": "N"}]) == ["power!"]
    assert _codes([{"macro": "tiny", "name": "u_t", "location": None, "orientation": "N"}]) == ["unplaced!"]
    assert _codes([_at(110.5, 100.64)]) == ["grid"]
    ps = macrolib.placement_problems(_model(instances=[_at(200.1, 100.64)]))
    assert [p["code"] for p in ps] == ["channel"] and "4.26 µm strip" in ps[0]["message"]


# ------------------------------------------------------------------ views
LIB = """library (sram) {
    time_unit : "1ns" ;
    default_max_transition   : 0.5 ;
    bus(addr0){
        bus_type  : addr;
        direction  : input;
        max_transition       : 0.04;
        pin(addr0[7:0]){
            timing(){ rise_transition(x) { values("0.009, 0.012"); } }
        }
    }
    pin(clk0){
        max_transition       : 0.5;
    }
}
"""


def test_liberty_limits_relaxed_and_arcs_kept():
    new, changes = macrolib.relax_liberty(LIB)
    assert changes == [{"pin": "addr0", "from": "0.04", "to": "0.5", "line": 7}]
    assert "max_transition       : 0.5;" in new.splitlines()[6]
    assert 'values("0.009, 0.012")' in new and "default_max_transition   : 0.5" in new
    # no default: nothing to relax to, the file is left alone
    assert macrolib.relax_liberty(LIB.replace("default_max_transition   : 0.5 ;", ""))[1] == []


def test_blackbox_header_ports_and_power():
    pins = [{"name": f"addr0[{i}]", "dir": "input", "use": "signal"} for i in range(8)] + [
        {"name": "dout0[0]", "dir": "output", "use": "signal"}, {"name": "clk0", "dir": "input", "use": "signal"},
        {"name": "vccd1", "dir": "inout", "use": "power"}, {"name": "VDD!", "dir": "inout", "use": "power"}]
    t = macrolib.blackbox_header("sram", pins)
    assert "(* blackbox *)\nmodule sram (" in t
    assert "`ifdef USE_POWER_PINS\n    inout           vccd1,\n`endif" in t
    assert "power pin VDD! has no Verilog name" in t
    assert "input  [7:0]    addr0," in t and t.rstrip().endswith("endmodule")
    assert "clk0\n);" in t                                  # the last port has no comma


def test_lib_corner_map_per_pdk_corner():
    one, notes = macrolib.lib_corner_map([Path("a_TT_1p8V_25C.lib")], ["tt_025C_1v80", "ss_100C_1v60"])
    assert list(one) == ["*"] and "every corner" in notes[0]
    libs = [Path("r_fast_1p32V_m55C.lib"), Path("r_slow_1p08V_125C.lib"), Path("r_typ_1p20V_25C.lib")]
    m, notes = macrolib.lib_corner_map(libs, ["fast_1p32V_m40C", "slow_1p08V_125C", "typ_1p20V_25C"])
    assert {k: v.name for k, v in m.items()} == {"*_fast_1p32V_m40C": "r_fast_1p32V_m55C.lib",
                                                 "*_slow_1p08V_125C": "r_slow_1p08V_125C.lib",
                                                 "*_typ_1p20V_25C": "r_typ_1p20V_25C.lib"}
    assert notes == ["Corner fast_1p32V_m40C uses r_fast_1p32V_m55C.lib, the closest timing file it ships."]


# ------------------------------------------------------------------ power
def test_hookup_one_line_per_power_pin_and_old_format():
    pw = {"vdd_net": "VPWR", "gnd_net": "VGND", "vdd_pins": ["VDD!", "VDDARRAY!"], "gnd_pins": ["VSS!"]}
    assert cm.hookup_lines("u_sram", pw) == ["u_sram VPWR VGND VDD! VSS!", "u_sram VPWR VGND VDDARRAY! VSS!"]
    old = {"vdd_pin": "vccd1", "gnd_pin": "vssd1", "vdd_net": "VPWR", "gnd_net": "VGND"}
    assert cm.hookup_lines("u_core.u_sram", old) == [r"u_core\.u_sram VPWR VGND vccd1 vssd1"]
    assert cm.norm_power({"vdd_pins": ["a b"], "vdd_net": "V", "gnd_net": "G", "gnd_pins": ["g"]}) is None
    assert cm.norm_power({}) == {}


def _tech(*names, spacing=0.28):
    return {"layers": [{"name": n, "type": "ROUTING", "spacing": spacing} for n in names], "mfg": 0.005, "dbu": 1000}


def test_grid_plans_follow_the_published_templates():
    # IHP: VDD!/VDDARRAY!/VSS! as vertical Metal4 stripes (ihp-sg13g2-librelane-template)
    ihp = {"w": 236.8, "h": 118.78, "obs_layers": ["Metal1", "Metal2", "Metal3", "Metal4"],
           "prects": [["VSS!", "ground", "Metal4", 9.88, 0, 12.69, 118.78], ["VDD!", "power", "Metal4", 4.26, 0, 7.07, 38.825],
                      ["VDDARRAY!", "power", "Metal4", 4.26, 45.465, 7.07, 118.78]]}
    p = macrolib.macro_grid_plan(ihp, _tech("Metal1", "Metal2", "Metal3", "Metal4", "Metal5", "TopMetal1", "TopMetal2"), "TopMetal1", "TopMetal2")
    assert p["kind"] == "bridge" and p["strap_layer"] == "Metal5"
    assert (p["width"], p["pitch"], p["offset"], p["spacing"]) == (2.81, 11.24, 2.81, 2.81)
    assert p["connect"] == [["Metal4", "Metal5"], ["Metal5", "TopMetal1"]]
    # gf180: power pads in columns on Metal3 at the west and east edges (wafer.space template)
    gf = {"w": 431.86, "h": 232.88, "obs_layers": ["Nwell", "Metal1", "Metal2", "Metal3"],
          "prects": [["VDD", "power", "Metal3", 0.0, y, 5.0, y + 30] for y in (10, 50, 90, 130, 170)] +
                    [["VSS", "ground", "Metal3", 426.86, y, 431.86, y + 30] for y in (10, 50, 90, 130, 170)]}
    q = macrolib.macro_grid_plan(gf, _tech("Metal1", "Metal2", "Metal3", "Metal4", "Metal5"), "Metal4", "Metal5")
    assert q["kind"] == "columns"
    assert (q["width"], q["offset"], q["pitch"], q["count"]) == (2.36, 1.18, 426.86, 2)
    t = macrolib.pdn_cfg_text([("ihp_sram", ["u_sram"], p), ("gf_sram", ["u_a", "u_b"], q)])
    assert t.startswith("# Written by LanEx") and "source $::env(SCRIPTS_DIR)/openroad/common/pdn_cfg.tcl" in t
    assert '-instances "u_a u_b"' in t and "-number_of_straps 2" in t and '-layers "Metal4 Metal5"' in t
    # sky130: the pins are on the grid's own vertical layer, no plan needed or made
    sky = {"w": 10, "h": 10, "obs_layers": ["met1", "met2", "met3", "met4"], "prects": [["v", "power", "met4", 0, 0, 1, 10]]}
    assert macrolib.macro_grid_plan(sky, _tech("met1", "met2", "met3", "met4", "met5"), "met4", "met5") is None


# ------------------------------------------------------------------ overlay
def _fake_macro(d: Path, name="m", inst=None, maglef=True):
    (d / "config.yaml").write_text("DESIGN_NAME: top\nFP_SIZING: absolute\nDIE_AREA: [0, 0, 700, 600]\n")
    views = {"gds": {"filename": "m.gds", "content_b64": "Z2Rz"},
             "lef": {"filename": "m.lef", "content_b64": "TUFDUk8gbQpFTkQgbQo="}}
    r = cm.save_macro(str(d), name, views=views, instances=inst or [],
                      power={"vdd_net": "VPWR", "gnd_net": "VGND", "vdd_pins": ["vccd1"], "gnd_pins": ["vssd1"]})
    assert r["ok"], r
    if maglef:
        doc = json.loads((d / ".gui-custom-macros.json").read_text())
        doc["macros"][0]["views"]["maglef"] = "pdk_dir::libs.ref/x/maglef/m.mag"
        (d / ".gui-custom-macros.json").write_text(json.dumps(doc))


def test_overlay_writes_the_verified_recipe(tmp_path):
    _fake_macro(tmp_path, inst=[{"name": "u_m", "location": [110.4, 100.64]}, {"name": "u_x", "location": None}])
    cm.set_settings(str(tmp_path), {"halo_h": "12", "halo_v": "8"})
    ov = json.loads(Path(cm.write_overlay(str(tmp_path))).read_text())
    m = ov["MACROS"]["m"]
    assert list(m["instances"]) == ["u_m"]                      # never an instance without a position
    assert "maglef" not in m and "model" not in m               # not Macro fields
    assert ov["PDN_MACRO_CONNECTIONS"] == ["u_m VPWR VGND vccd1 vssd1"]
    assert (ov["FP_MACRO_HORIZONTAL_HALO"], ov["PDN_HORIZONTAL_HALO"]) == (12.0, 12.0)
    assert (ov["FP_MACRO_VERTICAL_HALO"], ov["PDN_VERTICAL_HALO"]) == (8.0, 8.0)
    assert ov["MAGIC_MACRO_STD_CELL_SOURCE"] == "PDK"
    assert ov["MAGIC_DRC_MAGLEFS"] == ["pdk_dir::libs.ref/x/maglef/m.mag"]


def test_overlay_never_overrides_the_users_own_settings(tmp_path):
    _fake_macro(tmp_path, inst=[{"name": "u_m", "location": [110.4, 100.64]}])
    (tmp_path / "config.yaml").write_text("DESIGN_NAME: top\nMAGIC_MACRO_STD_CELL_SOURCE: macro\n"
                                          "MAGIC_DRC_MAGLEFS: [dir::mine.mag]\nPDN_CFG: dir::my_pdn.tcl\n")
    ov = json.loads(Path(cm.write_overlay(str(tmp_path))).read_text())
    assert "MAGIC_MACRO_STD_CELL_SOURCE" not in ov and "PDN_CFG" not in ov
    assert ov["MAGIC_DRC_MAGLEFS"] == ["dir::mine.mag", "pdk_dir::libs.ref/x/maglef/m.mag"]
    assert not (tmp_path / ".gui-macros-pdn.tcl").exists()


def test_run_check_asks_about_an_instance_the_rtl_lacks(tmp_path):
    # LibreLane 3.0.4, Check Macro Instances: "No macro instance u_wrong found." (forced on a real run)
    _fake_macro(tmp_path, inst=[{"name": "u_wrong", "location": [110.4, 100.64]}], maglef=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.v").write_text("module top(input clk);\n  m u_m (.clk(clk));\nendmodule\n")
    (tmp_path / "config.yaml").write_text("DESIGN_NAME: top\nVERILOG_FILES: dir::src/top.v\n")
    ps = cm.run_problems(str(tmp_path))
    hit = [p for p in ps if "No macro instance u_wrong found." in p["message"]]
    assert hit and not hit[0]["hard"] and "names it u_m" in hit[0]["message"]
    # a LEF without a SIZE is said, never a crash
    assert any(p["hard"] and "no SIZE" in p["message"] for p in ps)


def test_set_instances_saves_all_or_nothing(tmp_path):
    _fake_macro(tmp_path, inst=[{"name": "u_m", "location": None}])
    assert not cm.set_instances(str(tmp_path), [{"macro": "nope", "name": "u", "location": [1, 2]}])["ok"]
    assert not cm.set_instances(str(tmp_path), [{"macro": "m", "name": "u_m", "location": [1, 2], "orientation": "Q"}])["ok"]
    assert cm.set_instances(str(tmp_path), [{"macro": "m", "name": "u_m", "location": [5.52, 10.88], "orientation": "FS"}])["ok"]
    inst = cm.list_macros(str(tmp_path))["macros"][0]["instances"]
    assert inst == [{"name": "u_m", "location": [5.52, 10.88], "orientation": "FS"}]


# ------------------------------------------------------------------ library words
def test_family_text_for_ihp_and_gf180_names():
    assert celllib.family_text("a21oi") == "AND-OR-invert: a 2-input AND and one input into a 2-input NOR."
    assert celllib.family_text("aoi211") == "AND-OR-invert: a 2-input AND and two single inputs into a 3-input NOR."
    assert celllib.family_text("oai33") == "OR-AND-invert: two 3-input ORs into a 2-input NAND."
    assert celllib.family_text("nand2b") == "2-input NAND gate, with one input inverted (the _N pin)."
    assert celllib.family_text("dffrnq") == "D flip-flop, rising edge, active-low reset; output Q."
    assert celllib.family_text("sdffrsnq").startswith("Scan D flip-flop, rising edge, active-low set and reset")
    assert celllib.family_text("decap_20").startswith("Decoupling capacitor")
    assert "obsolete" in celllib.family_text("sdfbbp")
    assert celllib.family_text("zz_unknown") == ""


def test_datasheet_stays_inside_the_library(tmp_path, monkeypatch):
    lib = tmp_path / "ihpX" / "libs.ref" / "stdc"
    (lib / "doc").mkdir(parents=True)
    (lib / "doc" / "stdc_typ_1p20V_25C.pdf").write_bytes(b"%PDF-1.4")
    (lib / "doc" / "stdc_slow_1p08V_125C.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(celllib, "_pdk_roots", lambda: [tmp_path])
    assert celllib.datasheet("ihpX", "stdc", "slow_1p08V_125C").name == "stdc_slow_1p08V_125C.pdf"
    assert celllib.datasheet("ihpX", "stdc", None).name == "stdc_typ_1p20V_25C.pdf"
    assert celllib.datasheet("ihpX", "../stdc", None) is None
    assert celllib.datasheet("..", "stdc", None) is None
    links = celllib.doc_links("ihpX", "stdc", "typ_1p20V_25C")
    assert links["pdf"] == "stdc_typ_1p20V_25C" and links["family"] is None


def test_new_routes_have_methods():
    from lanex.server import routes
    assert routes.ROUTE_METHODS["/api/macros/floorplan"] == ("GET",)
    assert routes.ROUTE_METHODS["/api/macros/place"] == ("POST",)
    assert routes.ROUTE_METHODS["/api/celllib/doc"] == ("GET",)
    names = [p for p, _ in routes.ROUTES]
    assert names.index("/api/celllib/doc") < names.index("/api/celllib")


# ------------------------------------------------------------------ the installed PDKs
def _have(pdk: str) -> bool:
    return celllib.pdk_dir(pdk) is not None


@pytest.mark.skipif(not _have("sky130A"), reason="sky130A not installed")
def test_sky130_sram_gets_the_clean_recipe(tmp_path):
    (tmp_path / "config.yaml").write_text("DESIGN_NAME: sram_demo\nFP_SIZING: absolute\nDIE_AREA: [0, 0, 700, 600]\n")
    name = "sky130_sram_1kbyte_1rw1r_32x256_8"
    r = cm.add_pdk_macro(str(tmp_path), "sky130A", "sky130_fd_sc_hd", name)
    assert r["ok"], r
    v = r["macro"]["views"]
    assert v["vh"] == f"dir::macros/{name}/{name}.bb.v" and (tmp_path / v["vh"][5:]).is_file()
    assert list(v["lib"]) == ["*"] and v["lib"]["*"].startswith("dir::")          # the relaxed copy
    assert "max_transition       : 0.04" not in (tmp_path / v["lib"]["*"][5:]).read_text()
    assert v["maglef"].endswith(f"maglef/{name}.mag")
    assert r["macro"]["power"] == {"vdd_net": "VPWR", "gnd_net": "VGND", "vdd_pins": ["vccd1"], "gnd_pins": ["vssd1"]}
    cm.set_instances(str(tmp_path), [{"macro": name, "name": "u_sram", "location": [110.4, 100.64], "orientation": "N"}])
    fp = macrolib.floorplan(str(tmp_path), "sky130A", "sky130_fd_sc_hd")
    assert fp["core"]["box"] == [5.52, 10.88, 694.14, 587.52]
    assert fp["macros"][name]["grid"] == "default" and fp["problems"] == []
    assert cm.run_problems(str(tmp_path), "sky130A", "sky130_fd_sc_hd") == []
    ov = json.loads(Path(cm.write_overlay(str(tmp_path), "sky130A", "sky130_fd_sc_hd")).read_text())
    assert ov["PDN_MACRO_CONNECTIONS"] == ["u_sram VPWR VGND vccd1 vssd1"] and "PDN_CFG" not in ov
    # turned 90°, unplaced or outside: the run check refuses before the start
    cm.set_instances(str(tmp_path), [{"macro": name, "name": "u_sram", "location": [0, 0], "orientation": "N"}])
    assert any(p["hard"] and "outside the core" in p["message"] for p in cm.run_problems(str(tmp_path), "sky130A", "sky130_fd_sc_hd"))


@pytest.mark.skipif(not _have("ihp-sg13g2"), reason="ihp-sg13g2 not installed")
def test_ihp_sram_gets_corners_two_hooks_and_a_grid(tmp_path):
    (tmp_path / "config.yaml").write_text("DESIGN_NAME: t\nPDK: ihp-sg13g2\nSTD_CELL_LIBRARY: sg13g2_stdcell\n"
                                          "FP_SIZING: absolute\nDIE_AREA: [0, 0, 400, 300]\n")
    name = "RM_IHPSG13_1P_256x16_c2_bm_bist"
    r = cm.add_pdk_macro(str(tmp_path), "ihp-sg13g2", "sg13g2_stdcell", name,
                         instances=[{"name": "u_sram", "location": [81.6, 75.6]}])
    assert r["ok"], r
    assert sorted(r["macro"]["views"]["lib"]) == ["*_fast_1p32V_m40C", "*_slow_1p08V_125C", "*_typ_1p20V_25C"]
    fp = macrolib.floorplan(str(tmp_path))
    assert fp["core"]["box"] == [5.76, 15.12, 394.08, 283.5]
    assert fp["macros"][name]["grid"] == "lanex" and fp["macros"][name]["plan"]["strap_layer"] == "Metal5"
    ov = json.loads(Path(cm.write_overlay(str(tmp_path))).read_text())
    assert ov["PDN_MACRO_CONNECTIONS"] == ["u_sram VPWR VGND VDD! VSS!", "u_sram VPWR VGND VDDARRAY! VSS!"]
    assert ov["PDN_CFG"] == "dir::.gui-macros-pdn.tcl" and (tmp_path / ".gui-macros-pdn.tcl").is_file()


@pytest.mark.skipif(not _have("gf180mcuD"), reason="gf180mcuD not installed")
def test_gf180_sram_power_nets_are_vdd_vss_and_gets_column_straps(tmp_path):
    (tmp_path / "config.yaml").write_text("DESIGN_NAME: t\nPDK: gf180mcuD\nSTD_CELL_LIBRARY: gf180mcu_fd_sc_mcu7t5v0\n"
                                          "FP_SIZING: absolute\nDIE_AREA: [0, 0, 650, 450]\n")
    name = "gf180mcu_fd_ip_sram__sram64x8m8wm1"
    r = cm.add_pdk_macro(str(tmp_path), "gf180mcuD", "gf180mcu_fd_sc_mcu7t5v0", name)
    assert r["ok"], r
    assert (r["macro"]["power"]["vdd_net"], r["macro"]["power"]["gnd_net"]) == ("VDD", "VSS")
    fp = macrolib.floorplan(str(tmp_path))
    plan = fp["macros"][name]["plan"]
    assert fp["core"]["box"] == [6.72, 15.68, 642.88, 431.2]
    assert (plan["kind"], plan["width"], plan["offset"], plan["pitch"]) == ("columns", 2.36, 1.18, 426.86)


@pytest.mark.skipif(not _have("sky130A"), reason="sky130A not installed")
def test_sram_template_makes_a_placed_powered_macro(tmp_path):
    from lanex.controller import scaffold
    d = tmp_path / "sramp"
    assert scaffold.create_project(str(d), "sram", top="", pdk="sky130A")["ok"]
    cfg = json.loads((d / "config.json").read_text())
    assert cfg["FP_SIZING"] == "absolute" and cfg["DIE_AREA"] == [0, 0, 700, 600]
    res = cm.apply_template_macros(str(d), "sram", "sky130A", "sky130_fd_sc_hd")
    assert res == [{"ok": True, "name": "sky130_sram_1kbyte_1rw1r_32x256_8", "error": None}]
    # the position verified clean on real runs (the research's and LanEx's own)
    assert cm.run_problems(str(d), "sky130A", "sky130_fd_sc_hd") == []
    other = cm.apply_template_macros(str(tmp_path / "x"), "sram", "gf180mcuD", None)
    assert not other[0]["ok"] and "sky130" in other[0]["error"]
