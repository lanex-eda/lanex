# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The Runs trash and project file can't reach outside the project (review
fixes R2-1, R1 findings 2, 12, 13): a linked trash is never followed, only
batch folders are purged, opening a page purges nothing, Undo never deletes a
run it could not put back, a linked ``runs/`` still works."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from lanex.controller import runsmeta


def _run(design: Path, tag: str) -> Path:
    rd = design / "runs" / tag
    (rd / "final").mkdir(parents=True)
    (rd / "1-yosys-synthesis").mkdir()
    (rd / "resolved.json").write_text(json.dumps({"CLOCK_PERIOD": 10, "meta": {"librelane_version": "3.0.4"}}))
    (rd / "final" / "metrics.json").write_text(json.dumps({"timing__setup__ws": 1.5}))
    return rd


def _old(p: Path) -> None:
    t = time.time() - runsmeta.TRASH_TTL_S - 60
    os.utime(p, (t, t), follow_symlinks=False)


def _victim(tmp_path: Path) -> Path:
    v = tmp_path / "victim"
    keep = v / "20261126-101010-abcdef" / "src"       # named like a batch, and old
    keep.mkdir(parents=True)
    (keep / "keep.v").write_text("module keep; endmodule\n")
    (v / "precious_project").mkdir()
    _old(v / "20261126-101010-abcdef")
    _old(v / "precious_project")
    return v


# ------------------------------------------------------------------ R2-1: a linked trash
def test_purge_never_follows_a_trash_link(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    _run(proj, "a")
    v = _victim(tmp_path)
    os.symlink(v, proj / "runs" / runsmeta.TRASH_DIR)
    runsmeta.purge_stale(proj, background=False)
    assert (v / "20261126-101010-abcdef" / "src" / "keep.v").is_file()
    assert (v / "precious_project").is_dir()
    # The explicit purge (the Undo toast closing) and Undo don't follow it either.
    res, st = runsmeta.bulk("purge", [], batch="20261126-101010-abcdef", design_dirs=[str(proj)])
    assert st == 200 and (v / "20261126-101010-abcdef" / "src" / "keep.v").is_file()
    res, st = runsmeta.bulk("undo", [], batch="20261126-101010-abcdef", design_dirs=[str(proj)])
    assert st == 410 and not (proj / "runs" / "src").exists()
    # Delete won't move runs into it.
    res, st = runsmeta.bulk("delete", [str(proj / "runs" / "a")])
    assert res["done"] == [] and "link" in res["failed"][0]["error"]
    assert (proj / "runs" / "a").is_dir() and sorted(p.name for p in v.iterdir()) == \
        ["20261126-101010-abcdef", "precious_project"]


def test_purge_removes_only_batch_folders_never_links(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    a = _run(proj, "a")
    res, _ = runsmeta.bulk("delete", [str(a)])
    time.sleep(0.3)
    trash = proj / "runs" / runsmeta.TRASH_DIR
    bdir = trash / res["batch"]
    _old(bdir)
    other = trash / "not-a-batch"                     # something else in the trash
    other.mkdir()
    _old(other)
    v = _victim(tmp_path)
    os.symlink(v / "precious_project", trash / "20261126-111111-abcdef")   # a batch name, but a link
    runsmeta.purge_stale(proj, background=False)
    assert not bdir.exists()
    assert other.is_dir() and (v / "precious_project").is_dir()
    assert (trash / "20261126-111111-abcdef").is_symlink()


def test_opening_runs_meta_purges_nothing(tmp_path: Path, monkeypatch) -> None:
    from lanex.server import routes
    proj = tmp_path / "proj"
    a = _run(proj, "a")
    res, _ = runsmeta.bulk("delete", [str(a)])
    time.sleep(0.3)        # the delete's own purge (a thread) has looked by now
    bdir = proj / "runs" / runsmeta.TRASH_DIR / res["batch"]
    _old(bdir)
    monkeypatch.setattr(routes, "_runs_known_designs", lambda: [str(proj.resolve())])
    out = {}
    monkeypatch.setattr(routes, "_respond", lambda h, payload, code=200: out.update(payload=payload, code=code))

    class H:
        command = "GET"
        path = "/api/runs-meta?design_dir=" + str(proj)
    routes.h_runs_meta(H())
    time.sleep(0.3)        # the old purge ran in a thread
    assert out["code"] == 200 and bdir.is_dir()
    # Server start purges it (in every known project).
    routes.purge_old_trash([str(proj)], background=False)
    assert not bdir.exists()


def test_runs_meta_writes_only_into_known_projects(tmp_path: Path, monkeypatch) -> None:
    from lanex.server import routes
    stranger = tmp_path / "stranger"
    (stranger / "runs").mkdir(parents=True)
    monkeypatch.setattr(routes, "_runs_known_designs", lambda: [str((tmp_path / "known").resolve())])
    out = {}
    monkeypatch.setattr(routes, "_respond", lambda h, payload, code=200: out.update(payload=payload, code=code))

    class H:
        command = "POST"
        path = "/api/runs-meta"
        _body = {"design_dir": str(stranger), "baseline": "x"}
    routes.h_runs_meta(H())
    assert out["code"] == 404 and not (stranger / runsmeta.META_FILE).exists()


# ------------------------------------------------------------------ finding 2: Undo keeps what it can't put back
def test_undo_keeps_a_run_it_cannot_put_back(tmp_path: Path) -> None:
    d = tmp_path / "proj"
    run = _run(d, "tuned")
    (run / "precious.gds").write_text("THE ORIGINAL LAYOUT")
    _run(d, "other")
    res, st = runsmeta.bulk("delete", [str(run), str(d / "runs" / "other")])
    assert st == 200
    batch = res["batch"]
    trashed = d / "runs" / runsmeta.TRASH_DIR / batch / "tuned"
    # A new run takes the name before Undo is pressed.
    (d / "runs" / "tuned").mkdir()
    (d / "runs" / "tuned" / "resolved.json").write_text("{}")
    res, st = runsmeta.bulk("undo", [], batch=batch, design_dirs=[str(d)])
    assert st == 200 and res["ok"] is False
    assert res["done"] == [str(d / "runs" / "other")]           # the one it could put back is back
    assert (d / "runs" / "other").is_dir()
    f = res["failed"][0]
    assert f["kept_at"] == str(trashed) and str(trashed) in f["error"] and "Nothing was lost" in f["error"]
    assert res["kept"][0]["run"] == "tuned"
    assert (trashed / "precious.gds").read_text() == "THE ORIGINAL LAYOUT"
    # Never purged: not when the toast closes, not when it is old.
    runsmeta.bulk("purge", [], batch=batch, design_dirs=[str(d)])
    _old(trashed.parent)
    runsmeta.purge_stale(d, background=False)
    assert (trashed / "precious.gds").read_text() == "THE ORIGINAL LAYOUT"
    # Once the name is free, Undo again puts it back and the batch goes.
    (d / "runs" / "tuned" / "resolved.json").unlink()
    (d / "runs" / "tuned").rmdir()
    res, st = runsmeta.bulk("undo", [], batch=batch, design_dirs=[str(d)])
    assert st == 200 and res["ok"] and (d / "runs" / "tuned" / "precious.gds").is_file()
    assert not trashed.parent.exists()


# ------------------------------------------------------------------ finding 13: a linked runs/
def test_a_linked_runs_folder_still_works(tmp_path: Path) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    big = tmp_path / "bigdisk" / "spm-runs"
    big.mkdir(parents=True)
    os.symlink(big, d / "runs")
    _run(d, "a")
    _run(d, "b")
    p = runsmeta.check_run_dir(str(d / "runs" / "a"), known=[str(d.resolve())])
    assert p == d.resolve() / "runs" / "a"
    for act, kw in (("archive", {}), ("tag", {"tags": ["keep"]})):
        res, st = runsmeta.bulk(act, [str(d / "runs" / "a")], known=[str(d)], **kw)
        assert st == 200 and res["ok"], res
    assert runsmeta.is_archived(big / "a") and runsmeta.read_tags(big / "a") == ["keep"]
    res, st = runsmeta.bulk("delete", [str(d / "runs" / "b")], known=[str(d)])
    assert st == 200 and res["ok"] and not (big / "b").exists()
    assert (big / runsmeta.TRASH_DIR / res["batch"] / "b").is_dir()
    res, st = runsmeta.bulk("undo", [], batch=res["batch"], design_dirs=[str(d)], known=[str(d)])
    assert st == 200 and res["ok"] and (big / "b").is_dir()


def test_a_run_folder_linked_out_of_runs_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "proj"
    elsewhere = _run(tmp_path / "other", "x")
    (d / "runs").mkdir(parents=True)
    os.symlink(elsewhere, d / "runs" / "x")
    with pytest.raises(ValueError):
        runsmeta.check_run_dir(str(d / "runs" / "x"), known=[str(d.resolve())])
