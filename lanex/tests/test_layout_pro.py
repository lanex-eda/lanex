# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Layout pro analysis (audit 4.10, 6b, 6c, 6d): net names on the layout, the
OpenROAD analysis export's parsing and cache, worker processes, on-disk
thumbnails, viewer launch logs and stage-DEF open scripts."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from lanex.controller import desktop, layoutview as lv, odbexport as ox

klayout_ok = lv.available()["ok"]

TECH_LEF = """VERSION 5.8 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
SITE unit
  CLASS CORE ;
  SIZE 0.46 BY 2.72 ;
END unit
LAYER met1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.34 ;
  WIDTH 0.14 ;
END met1
LAYER via
  TYPE CUT ;
END via
LAYER met2
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  PITCH 0.46 ;
  WIDTH 0.14 ;
END met2
END LIBRARY
"""

CELL_LEF = """VERSION 5.8 ;
MACRO lib__inv_1
  CLASS CORE ;
  ORIGIN 0 0 ;
  SIZE 1.38 BY 2.72 ;
  SITE unit ;
  PIN A
    DIRECTION INPUT ;
    PORT
      LAYER met1 ;
        RECT 0.1 1.0 0.4 1.4 ;
    END
  END A
  PIN Y
    DIRECTION OUTPUT ;
    PORT
      LAYER met1 ;
        RECT 0.9 1.0 1.2 1.4 ;
    END
  END Y
END lib__inv_1
END LIBRARY
"""

DEF = """VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 6000 ) ;
COMPONENTS 2 ;
    - u1 lib__inv_1 + PLACED ( 1000 1000 ) N ;
    - u2 lib__inv_1 + PLACED ( 6000 1000 ) N ;
END COMPONENTS
PINS 1 ;
    - a + NET a + DIRECTION INPUT + USE SIGNAL
      + PORT
        + LAYER met2 ( -70 0 ) ( 70 140 )
        + PLACED ( 1250 0 ) N ;
END PINS
SPECIALNETS 1 ;
    - VPWR ( * VPWR ) + USE POWER ;
END SPECIALNETS
NETS 2 ;
    - a ( PIN a ) ( u1 A ) + USE SIGNAL ;
    - mid\\[0\\] ( u1 Y ) ( u2 A ) + USE SIGNAL
      + ROUTED met1 ( 2050 2200 ) ( 7250 * ) ;
END NETS
END DESIGN
"""


def _run_with_def(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "r1"
    step = run / "34-openroad-detailedplacement"
    step.mkdir(parents=True)
    (run / "tech.lef").write_text(TECH_LEF)
    (run / "cells.lef").write_text(CELL_LEF)
    (step / "top.def").write_text(DEF)
    (run / "resolved.json").write_text(json.dumps({
        "DESIGN_NAME": "top", "TECH_LEFS": {"nom_*": str(run / "tech.lef")}, "CELL_LEFS": [str(run / "cells.lef")],
        "PDN_RAIL_LAYER": "met1", "PDN_VERTICAL_LAYER": "met4", "PDN_HORIZONTAL_LAYER": "met5",
        "VDD_NETS": None, "VDD_PIN": "VPWR", "GND_PIN": "VGND"}))
    return run


# --------------------------------------------------------------------------- DEF names (no KLayout needed)
def test_parse_def_nets_reads_connections_uses_and_escapes(tmp_path):
    f = tmp_path / "x.def"
    f.write_text(DEF)
    c = lv.parse_def_nets(f)
    assert c["nets"]["a"] == [("PIN", "a"), ("u1", "A")]
    # DEF escapes the brackets; KLayout and STA do not: names are kept unescaped.
    assert c["nets"]["mid[0]"] == [("u1", "Y"), ("u2", "A")]
    assert c["pins"]["u2/A"] == "mid[0]" and c["pins"]["u1/A"] == "a"
    assert c["use"] == {"VPWR": "POWER", "a": "SIGNAL", "mid[0]": "SIGNAL"}
    # A special net on every instance's pin of that name.
    assert c["pins"]["*/VPWR"] == "VPWR"
    assert "ROUTED" not in json.dumps(c)          # routing is not a connection


def test_pdn_info_names_the_grid_layers_and_supplies():
    info = lv.pdn_info({"PDN_RAIL_LAYER": "met1", "PDN_VERTICAL_LAYER": "met4", "PDN_HORIZONTAL_LAYER": "met5",
                        "VDD_NETS": ["vccd1"], "GND_PIN": "VGND"})
    assert info == {"rail": "met1", "vertical": "met4", "horizontal": "met5", "vdd": "vccd1", "gnd": "VGND"}


def test_clean_drops_def_escapes():
    assert lv._clean("dsa\\[13\\].y_out_next") == "dsa[13].y_out_next"


@pytest.mark.skipif(not klayout_ok, reason="klayout Python package not installed")
def test_def_probe_names_net_instance_and_pin_and_search_finds_nets(tmp_path):
    run = _run_with_def(tmp_path)
    info = lv.open_session(run, "34-openroad-detailedplacement")
    assert info["ok"], info
    assert info["names"] == {"from": "def", "file": "top.def"}
    assert info["pdn"]["rail"] == "met1" and info["pdn"]["vdd"] == "VPWR"
    assert all("stipple" in L for L in info["layers"])
    sid = info["sid"]
    # The routed wire of mid[0] (DEF y 2.2 µm) names its net.
    hits = lv.probe(sid, 4.0, 2.2, 0.02)
    assert any(h.get("net") == "mid[0]" for h in hits), hits
    # u2's pin A (cell at x 6.0: pin rect 6.1-6.4, 2.0-2.4) names instance, pin and net.
    hits = lv.probe(sid, 6.25, 2.2, 0.01)
    pin = next(h for h in hits if h.get("pin"))
    assert pin["inst"] == "u2" and pin["pin"] == "A" and pin["net"] == "mid[0]"

    res = lv.nets(sid, "mid", 10)
    assert res["names"] and [n["name"] for n in res["nets"]] == ["mid[0]"]
    assert res["nets"][0]["pins"] == 2 and res["nets"][0]["use"] == "SIGNAL"
    # An escaped query finds it too.
    assert lv.nets(sid, "mid\\[0", 5)["total"] == 1

    g = lv.net(sid, "mid\\[0\\]")
    assert g["found"] and g["total"] >= 1 and g["length"] == pytest.approx(5.2, abs=0.2)
    assert {p["inst"] for p in g["pins"]} == {"u1", "u2"}

    loc = lv.locate(sid, ["u1", "nope"], ["a", "mid[0]", "gone"])
    assert loc["insts"]["u1"]["cell"] == "lib__inv_1" and loc["insts"]["u1"]["box"] == pytest.approx([1.0, 1.0, 2.38, 3.72])
    assert set(loc["nets"]) == {"a", "mid[0]"}
    assert loc["missing"] == {"insts": ["nope"], "nets": ["gone"]}
    # A net without wires (pre-route) still has its pins for flight lines.
    assert lv.net(sid, "a")["pins"]


# --------------------------------------------------------------------------- worker processes
@pytest.mark.skipif(not klayout_ok or os.name == "nt", reason="klayout or POSIX pipes not available")
def test_workers_open_render_probe_and_reopen_after_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("LANEX_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LANEX_LAYOUT_WORKERS", raising=False)
    run = _run_with_def(tmp_path)
    lv.enable_workers()
    try:
        assert lv.workers_on()
        info = lv.open_session(run, "34-openroad-detailedplacement")
        assert info["ok"], info
        sid = info["sid"]
        assert lv.render(sid, box=None, w=80, h=60)[:4] == b"\x89PNG"
        assert any(h.get("net") == "mid[0]" for h in lv.probe(sid, 4.0, 2.2, 0.02))
        # The view worker dies: the next call starts a new one, which reopens
        # the session from its spec without the page noticing.
        old = lv._WORKERS["view"]._proc
        old.kill()
        old.wait()
        assert any(h.get("net") == "mid[0]" for h in lv.probe(sid, 4.0, 2.2, 0.02))
        assert lv._WORKERS["view"]._proc is not old
        # Heavy renders go to the background worker.
        assert lv.render(sid, heavy=True, box=None, w=80, h=60)[:4] == b"\x89PNG"
        assert lv._WORKERS["bg"]._proc is not None
        assert lv.close_session(sid) and lv.render(sid, box=None, w=8, h=8) is None
    finally:
        lv.stop_workers()
    assert not lv.workers_on()


@pytest.mark.skipif(not klayout_ok, reason="klayout Python package not installed")
def test_thumbnails_are_kept_on_disk_and_served_without_klayout(tmp_path, monkeypatch):
    import klayout.db as kdb

    run = tmp_path / "runs" / "r1"
    (run / "final" / "gds").mkdir(parents=True)
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("top")
    top.shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 1000, 1000))
    ly.write(str(run / "final" / "gds" / "top.gds"))
    png = lv.thumbnail(run, "final", 64, 48, "#06080d")
    assert png[:4] == b"\x89PNG"
    files = list((run / ".lanex" / "thumbs").glob("final__64x48__06080d__*.png"))
    assert len(files) == 1 and files[0].read_bytes() == png
    # A fresh process (empty memory cache) reads the file, never KLayout.
    lv._THUMBS.clear()
    monkeypatch.setattr(lv, "_thumb_png", lambda *a, **k: pytest.fail("rendered again"))
    assert lv.thumbnail(run, "final", 64, 48, "#06080d") == png


# --------------------------------------------------------------------------- the analysis export
def test_read_heat_builds_a_grid_with_holes_and_the_unit(tmp_path):
    f = tmp_path / "heat-Routing.csv"
    f.write_text("x0,y0,x1,y1,value (%)\n"
                 "0,0,6.9,6.9,3.2e+01\n6.9,0,13.8,6.9,95.65\n"
                 "0,6.9,6.9,10,101\n")          # the last row is a smaller GCell; one tile is missing
    g = ox.read_heat(f)
    assert g["xs"] == [0, 6.9, 13.8] and g["ys"] == [0, 6.9, 10]
    assert g["nx"] == 2 and g["ny"] == 2 and g["unit"] == "%"
    assert g["v"] == [32.0, 95.65, 101.0, None]
    assert g["max"] == 101.0 and g["over"] == 1 and g["tiles"] == 3


def test_read_paths_converts_si_units_and_survives_inf(tmp_path):
    f = tmp_path / "paths-setup.json"
    f.write_text('{"checks":[{"type":"check","path_group":"clk","startpoint":"x","endpoint":"_278_/D",'
                 '"source_path":[{"instance":"input34","cell":"buf","pin":"input34/X","net":"net34",'
                 '"x":9.63e-05,"y":4.12e-05,"arrival":2.25e-09,"slew":2e-11}],'
                 '"data_arrival_time":3.5e-09,"required_time":inf,"slack":5.836e-09}]}')
    p = ox.read_paths(f)[0]
    assert p["slack"] == 5.836 and p["required"] is None and p["endpoint"] == "_278_/D"
    pt = p["points"][0]
    assert pt["x"] == 96.3 and pt["y"] == 41.2 and pt["arrival"] == 2.25 and pt["inst"] == "input34"


def test_read_clock_tells_flip_flops_from_buffers(tmp_path):
    f = tmp_path / "clocktree.tsv"
    rows = ["net\trole\tpin\tinst\tcell\tx0\ty0\tx1\ty1\tarrival_ns\tslew_ns",
            "clk\tport\tclk\t\tPORT\t0\t50\t0.2\t50.2\t\t",
            "clk\tsink\tclkbuf_0/A\tclkbuf_0\tclkbuf_16\t10\t10\t14\t12\t0.05\t0.01",
            "c0\tdriver\tclkbuf_0/X\tclkbuf_0\tclkbuf_16\t10\t10\t14\t12\t0.2\t0.02",
            "c0\tsink\tff1/CLK\tff1\tdfrtp_1\t20\t20\t30\t22\t0.37\t0.03",
            "c0\tsink\tff2/CLK\tff2\tdfrtp_1\t40\t20\t50\t22\t0.38\t0.03"]
    f.write_text("\n".join(rows) + "\n")
    c = ox.read_clock(f)
    s = c["summary"]
    assert s["nets"] == 2 and s["buffers"] == 1 and s["sinks"] == 2
    assert s["arrival_min"] == 0.37 and s["arrival_max"] == 0.38 and s["skew"] == 0.01 and s["roots"] == ["clk"]
    clk = next(n for n in c["nets"] if n["name"] == "clk")
    assert clk["port"]["pin"] == "clk" and clk["sinks"][0]["ff"] is False


def test_database_for_uses_the_nearest_earlier_database_and_says_so(tmp_path):
    run = tmp_path / "r"
    (run / "28-openroad-globalplacement").mkdir(parents=True)
    (run / "28-openroad-globalplacement" / "d.odb").write_bytes(b"x")
    (run / "29-odb-writeverilogheader").mkdir()
    (run / "final" / "odb").mkdir(parents=True)
    (run / "final" / "odb" / "d.odb").write_bytes(b"y")
    d = ox.database_for(run, "29-odb-writeverilogheader")
    assert d["step"] == "28-openroad-globalplacement" and "nearest earlier" in d["note"]
    assert ox.database_for(run, "final")["key"] == "final"
    assert ox.database_for(run, "../etc")["odb"] is None


def test_status_is_ready_only_for_the_database_it_measured(tmp_path):
    run = tmp_path / "r"
    (run / "final" / "odb").mkdir(parents=True)
    odb = run / "final" / "odb" / "d.odb"
    odb.write_bytes(b"x")
    assert ox.status(run, "final")["state"] == "none"
    cdir = ox.cache_dir(run, "final")
    cdir.mkdir(parents=True)
    (cdir / "analysis.json").write_text(json.dumps({"heat": {}, "summary": {"maps": []}}))
    (cdir / "manifest.json").write_text(json.dumps({"key": ox._manifest_key(odb), "summary": {"maps": []}, "seconds": 1.0}))
    assert ox.status(run, "final")["state"] == "ready"
    assert ox.data(run, "final")["summary"] == {"maps": []}
    time.sleep(1.1)
    odb.write_bytes(b"changed")          # the run was redone in place
    assert ox.status(run, "final")["state"] == "stale"
    assert ox.data(run, "final") is None


def test_export_command_mounts_the_run_read_only_and_only_the_cache_writable(tmp_path):
    run = tmp_path / "r"
    cdir = ox.cache_dir(run, "final")
    argv = ox.build_argv("docker", "img:1", run_dir=run, cdir=cdir, script=cdir / "s.tcl", odb=run / "final/odb/d.odb",
                         libs=["/pdk/sky130A/lib/a.lib"], sdc=None, spef=None, propagate=True, name="lanex-export-1",
                         timeout_s=90, mounts=["/pdk"], user="1000:1000", layers={"RT_MAX": "met5"})
    s = " ".join(argv)
    assert f"{run}:{run}:ro" in s and f"{cdir}:{cdir} " in s + " " and "/pdk:/pdk:ro" in s
    assert "--user 1000:1000" in s and "--network none" in s and "QT_QPA_PLATFORM=offscreen" in s
    assert argv[-6:-1] == ["openroad", "-no_init", "-no_splash", "-gui", "-no_settings"]
    assert "timeout 90" in s and "RT_MAX=met5" in s and "PROPAGATE=1" in s


def test_mount_roots_cover_each_pdk_once(tmp_path):
    run = tmp_path / "r"
    cfg = {"PDK": "sky130A", "PDK_ROOT": "/gone/root"}
    roots = ox._mount_roots(["/home/u/.ciel/sky130A/libs.ref/a.lib", "/home/u/.ciel/sky130A/libs.ref/b.lib",
                             str(run / "final/sdc/x.sdc")], run, cfg)
    assert roots == ["/home/u/.ciel"]


def test_convert_summarises_what_the_page_draws(tmp_path):
    (tmp_path / "heat-Routing.csv").write_text("x0,y0,x1,y1,value (%)\n0,0,1,1,50\n")
    (tmp_path / "heat-Routing@met1.csv").write_text("x0,y0,x1,y1,value (%)\n0,0,1,1,70\n")
    (tmp_path / "heat-Placement.csv").write_text("x0,y0,x1,y1,value (%)\n0,0,1,1,120\n")
    out = ox.convert(tmp_path, ["LANEX: unavailable RUDY: GUI-0072 not populated"])
    assert out["summary"]["maps"] == ["Placement", "Routing"]
    assert out["heat"]["Routing"]["layers"]["met1"]["max"] == 70
    assert out["unavailable"] == {"RUDY": "GUI-0072 not populated"}
    assert out["summary"]["density_max"] == 120


# --------------------------------------------------------------------------- viewer launches
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_a_viewer_that_dies_at_start_is_reported_with_its_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LANEX_HOME", str(tmp_path / "home"))
    launch = desktop.spawn_viewer(["sh", "-c", "echo 'cannot open display :0'; exit 3"], tool="klayout",
                                  label="KLayout", cwd=str(tmp_path))
    assert Path(launch["log"]).parent == tmp_path / "home" / "logs" / "viewers"
    launch["proc"].wait(timeout=10)
    st = desktop.viewer_status(launch["id"])
    assert st["state"] == "exited" and st["code"] == 3
    assert st["tail"] == ["cannot open display :0"]
    assert desktop.viewer_status("nope")["state"] == "unknown"
    # A crash dump: the tool's own error lines, not its memory map.
    log = tmp_path / "crash.log"
    log.write_text("$ docker run klayout\nInfo: Could not load the Qt platform plugin \"xcb\"\n"
                   "Fatal: This application failed to start\n"
                   "7e5d67317000-7e5d6731a000 r--p 00000000 08:30 213837   /nix/store/x/lib/libz.so.1\n"
                   " RIP: 0x00007e5d57a779e5 RBP: 0x00007ffe615f0f10\n")
    assert desktop._tail(str(log)) == ["Info: Could not load the Qt platform plugin \"xcb\"", "Fatal: This application failed to start"]


def test_def_open_scripts_load_the_lefs_first(tmp_path):
    run = _run_with_def(tmp_path)
    d = run / "34-openroad-detailedplacement" / "top.def"
    kl = desktop.def_open_script("klayout", d, run)
    text = kl.read_text()
    assert kl.parent == run / ".lanex" / "viewers" and kl.suffix == ".py"
    assert "macro_resolution_mode = 1" in text and str(run / "cells.lef") in text and str(d) in text
    mg = desktop.def_open_script("magic", d, run)
    lines = mg.read_text().splitlines()
    reads = [ln for ln in lines if ln.startswith(("lef read", "def read", "load"))]
    assert reads[0].startswith("lef read {" + str(run / "tech.lef"))
    assert reads[-2] == "def read {" + str(d) + "}" and reads[-1] == "load {top}"
    # No LEFs, no script (the caller says why).
    (run / "resolved.json").write_text("{}")
    assert desktop.def_open_script("klayout", d, run) is None
