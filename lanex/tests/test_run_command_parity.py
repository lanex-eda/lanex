# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The command the run menu shows is the command that runs: the run menu's
COMMAND block (manualcmd.cli_command_for, also written to gui-run.json) must
equal the argv the runner launches (container_run.build_dockerized_argv)."""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from lanex.controller import container_run, manualcmd


@pytest.mark.parametrize("extra", [
    {},
    {"tag": "glui-1", "to": "OpenROAD.DetailedPlacement"},
    {"tag": "r2", "frm": "OpenROAD.CTS", "initial_state": "runs/r1/35-openroad-cts/state_out.json"},
    {"skip": ["OpenROAD.IRDropReport", "Magic.DRC"], "flow": "Chip"},
    {"pdk_root": "/home/u/.ciel/versions/abc", "extra_sources": ["src/a.v", "src/b.v"]},
])
def test_menu_command_equals_the_launched_argv(tmp_path, extra):
    d = tmp_path / "proj"
    d.mkdir()
    cfg = d / "config.json"
    cfg.write_text("{}")
    overrides = {"CLOCK_PERIOD": "8", "DRT_THREADS": 8, "KLAYOUT_DRC_THREADS": 8}
    kw = dict(design_dir=str(d), config_file=str(cfg), pdk="sky130A", scl="sky130_fd_sc_hd",
              overrides=overrides, **{k: v for k, v in extra.items() if k != "initial_state"})
    state_rel = extra.get("initial_state")
    menu = manualcmd.cli_command_for(run_mode="container", initial_state=state_rel, **kw)["container"]
    launched = container_run.build_dockerized_argv(
        design_dir=d, config_file=cfg, pdk="sky130A", scl="sky130_fd_sc_hd", overrides=overrides,
        flow=extra.get("flow"), pdk_root=extra.get("pdk_root"), tag=extra.get("tag"), frm=extra.get("frm"),
        to=extra.get("to"), skip=extra.get("skip"), extra_sources=extra.get("extra_sources"),
        initial_state=str(d / state_rel) if state_rel else None,
        python_exe="python3", entry_module="lanex.controller.librelane_entry")
    # The runner starts LibreLane through python -m <entry>; a terminal types librelane.
    assert launched[:3] == ["python3", "-m", "lanex.controller.librelane_entry"]
    assert shlex.split(menu) == ["librelane"] + launched[3:]
