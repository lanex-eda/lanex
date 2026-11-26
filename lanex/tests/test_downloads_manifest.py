# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""One manifest for the app's own downloads (audit plan 5b): lanex/data/downloads.json
pins the LibreLane image by digest, GDS3D by commit and every PDK file by SHA-256,
and stays in step with what Windows Setup pins."""
from __future__ import annotations

import json
import re
from pathlib import Path

from lanex.controller import container_run, pdkfeed

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "lanex" / "data" / "downloads.json"


def _m():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_pdk_pins_match_windows_setup():
    setup = json.loads((REPO / "windows" / "setup" / "pdk-assets.json").read_text(encoding="utf-8"))
    pins = _m()["pdkAssets"]
    assert pins["families"] == setup["families"]
    assert pins["mirrorBase"] == setup["mirrorBase"]
    for fam in pins["families"].values():
        for a in fam["assets"]:
            assert re.fullmatch(r"[0-9a-f]{64}", a["sha256"]), a


def test_gds3d_commit_matches_the_release_build():
    build = (REPO / "scripts" / "build_windows_release.py").read_text(encoding="utf-8")
    commit = _m()["gds3d"]["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert f"Gds3dCommit = '{commit}'" in build


def test_image_digest_matches_the_known_table():
    m = _m()["image"]["digests"]
    for ver, digest in container_run._KNOWN_IMAGE_DIGESTS.items():
        assert m.get(ver) == digest
    build = (REPO / "scripts" / "build_windows_release.py").read_text(encoding="utf-8")
    for digest in m.values():
        assert digest in build


def test_linux_pdk_installs_use_the_packaged_pins(tmp_path, monkeypatch):
    monkeypatch.setenv("LANEX_HOME", str(tmp_path))      # no ~/.lanex/pdk-assets.json
    fam = _m()["pdkAssets"]["families"]["sky130"]
    pins = pdkfeed.load_pins("sky130", fam["version"])
    assert pins and pins["assets"] and pins["mirrorBase"]
    assert pdkfeed.load_pins("sky130", "0" * 40) is None   # another version: no pins, never wrong ones


def test_no_downloaded_install_scripts_in_the_app():
    src = (REPO / "lanex" / "controller" / "installer.py").read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert not [ln for ln in code if "get.docker.com" in ln]
    assert '"gds3d"' in MANIFEST.read_text(encoding="utf-8")
