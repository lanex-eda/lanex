# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Review fixes in the job queue (controller/jobs.py): a queue file that came
with a project starts nothing (R2-4), settings writes are locked (R2-10), a
finished job's words (R2-12), and Continue (R1 findings 4-8) and Stop of a
step-by-step run waiting for its next step (finding 6)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lanex.controller import jobs, termwatch

STEPS = ["Verilator.Lint", "Yosys.Synthesis", "OpenROAD.Floorplan", "OpenROAD.GlobalPlacement",
         "OpenROAD.DetailedRouting", "Magic.StreamOut"]


class FakeRunner:
    def __init__(self, job):
        self.job_id = job["id"]
        self.listener = None
        self.container_name = None
        self.awaiting_next = False
        self.running = True
        self.cancelled = False
        self.step_statuses: Dict[str, str] = {}

    def set_listener(self, fn):
        self.listener = fn

    def finish(self, **done):
        self.running = False
        self.listener("flow_done", done)

    def cancel(self):
        self.cancelled = True
        self.finish(cancelled=True)

    def resume(self):
        self.awaiting_next = False


def _sched(tmp_path: Path, runners: Dict[str, FakeRunner], launched: List[str]) -> jobs.Scheduler:
    s = jobs.Scheduler(persist=True)
    s._facts_override = {"cores": 32, "free_gb": 40.0, "total_gb": 46.0}
    s.set_runner_factory(lambda job: runners.setdefault(job["id"], FakeRunner(job)))

    def launcher(job, runner, share, dry, **kw):
        if dry:
            return {"ok": True}, 200
        tag = jobs.reserve_tag(job["design_dir"], (job.get("spec") or {}).get("tag"))
        s.note_tag(job["id"], tag)
        launched.append(job["id"])
        return {"ok": True, "started": True, "tag": tag}, 200
    s.set_launcher(launcher)
    return s


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "design_peak", lambda dd: {"gb": 1.0, "basis": "measured"})
    monkeypatch.setattr(jobs, "flow_containers", lambda: [])
    monkeypatch.setattr(jobs.Scheduler, "_flow_steps", lambda self, j: list(STEPS))
    jobs.write_setting("queue_max_parallel", 1)
    runners: Dict[str, FakeRunner] = {}
    launched: List[str] = []
    yield {"s": _sched(tmp_path, runners, launched), "runners": runners, "launched": launched}
    jobs.write_setting("queue_max_parallel", None)


def _proj(tmp_path: Path, name: str = "proj") -> Path:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text("{}")
    return d


def _plant(d: Path, doc: Dict[str, Any]) -> None:
    (d / ".lanex").mkdir(exist_ok=True)
    (d / ".lanex" / "queue.json").write_text(json.dumps(doc))


# ------------------------------------------------------------------ R2-4: a queue that came with the project
def test_a_planted_queue_starts_nothing(env, tmp_path):
    s = env["s"]
    d = _proj(tmp_path)
    _plant(d, {"version": 1, "paused": False, "jobs": [
        {"id": "j-planted-0001", "kind": "run", "state": "queued", "seq": 1, "origin": "lanex",
         "spec": {"tag": "planted-by-the-project", "run_mode": "container", "checks_ack": True}},
        {"id": "j-planted-0002", "kind": "run", "state": "running", "seq": 2, "boot": "00000000",
         "container": "some-other-container", "tag": "x", "spec": {}}]})
    q = s.queue_of(d)
    assert q["paused"] is True and "another LanEx" in q["notice"]
    for _ in range(3):
        s.tick()
    assert env["launched"] == [] and not (d / "runs" / "planted-by-the-project").exists()
    # The planted "running" job holds no slot: it is interrupted, not followed.
    assert s.job("j-planted-0002")["state"] == "interrupted" and s.running_count() == 0
    assert "another LanEx" in s.snapshot()["notices"][0]
    # Resume is the person's click: only then does the waiting run start, and
    # the file is LanEx's own from then on.
    s.pause(False)
    s.tick()
    assert env["launched"] == ["j-planted-0001"]
    assert json.loads((d / ".lanex" / "queue.json").read_text())["owner"] == jobs.home_id()
    assert not s.queue_of(d).get("notice")


def test_this_homes_own_queue_is_used_as_it_was(env, tmp_path):
    s = env["s"]
    d = _proj(tmp_path)
    s.add(d, {"run_mode": "container"})            # writes the file, stamped
    s2 = _sched(tmp_path, env["runners"], env["launched"])
    q = s2.queue_of(d)
    assert q["paused"] is False and not q.get("notice")
    s2.tick()
    assert len(env["launched"]) == 1


def test_stop_never_removes_a_container_the_queue_file_named(env, tmp_path, monkeypatch):
    s = env["s"]
    d = _proj(tmp_path)
    removed: List[str] = []
    monkeypatch.setattr(jobs, "remove_container", lambda name: removed.append(name) or True)
    j = s.add(d, {})
    with s._lock:
        k = s.job(j["id"])
        k.update(state="running", container="victim-db", boot="x")
    s.stop(j["id"])
    assert removed == [] and s.job(j["id"])["state"] == "stopped"
    # A real LanEx flow container of this project is removed.
    j2 = s.add(d, {})
    with s._lock:
        s.job(j2["id"]).update(state="running", container="lanex-flow-1", boot="x")
    monkeypatch.setattr(jobs, "flow_containers", lambda: [{"name": "lanex-flow-1", "id": "i", "workdir": str(d)}])
    s.stop(j2["id"])
    assert removed == ["lanex-flow-1"]


def test_reattach_needs_the_container_to_work_in_this_project(env, tmp_path, monkeypatch):
    s = env["s"]
    d = _proj(tmp_path)
    _plant(d, {"version": 1, "owner": jobs.home_id(), "jobs": [
        {"id": "j-a", "kind": "run", "state": "running", "boot": "00000000", "container": "lanex-flow-x",
         "tag": "t", "spec": {}}]})
    monkeypatch.setattr(jobs, "flow_containers", lambda: [{"name": "lanex-flow-x", "id": "i", "workdir": "/elsewhere"}])
    started = []
    monkeypatch.setattr(termwatch.Follower, "start", lambda self: started.append(self.job_id))
    found = s.recover_all([d])
    assert found == {"reattached": 0, "interrupted": 1} and started == []


def test_bad_point_tags_never_make_folders_outside_runs(tmp_path):
    from lanex.server import routes
    d = _proj(tmp_path)
    out, st = routes._launch_point({"id": "j-1", "design_dir": str(d), "kind": "point",
                                    "spec": {"tag": "../../escape"}}, None, None, False)
    assert st == 400 and "can't be used" in out
    with pytest.raises(ValueError):
        jobs.reserve_tag(d, "../escape")
    assert not (tmp_path / "escape").exists()


# ------------------------------------------------------------------ R2-10, R2-12
def test_settings_writes_keep_each_other(tmp_path):
    keys = [f"k{i}" for i in range(16)]
    ths = [threading.Thread(target=jobs.write_setting, args=(k, i)) for i, k in enumerate(keys)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert all(jobs.read_setting(k) == i for i, k in enumerate(keys))
    for k in keys:
        jobs.write_setting(k, None)


@pytest.mark.parametrize("spec,o,words", [
    ({"to": "Yosys.Synthesis"}, {"ok": True, "good": False}, "ran up to Yosys.Synthesis"),
    ({"only": "OpenROAD.CTS"}, {"ok": True, "good": False}, "only OpenROAD.CTS ran"),
    ({}, {"ok": True, "good": True, "verdict": "passed"}, "checks passed"),
    ({}, {"ok": True, "good": False, "verdict": "warning"}, "with warnings (see Verify)"),
    ({}, {"ok": True, "good": False, "verdict": "failed"}, "not every check passed (see Verify)"),
])
def test_finished_words_say_what_ran(spec, o, words):
    assert jobs.done_words(spec, o) == words


# ------------------------------------------------------------------ Continue (findings 4, 5, 7, 8)
def _cut_run(d: Path, tag: str, made, *, job_id: str, gui=None, flow_age: float = 300) -> Path:
    rd = d / "runs" / tag
    rd.mkdir(parents=True)
    for folder, finished in made:
        f = rd / folder
        f.mkdir()
        (f / "state_in.json").write_text("{}")
        if finished:
            (f / "state_out.json").write_text("{}")
            (f / "runtime.txt").write_text("1")
    (rd / "gui-run.json").write_text(json.dumps({"job_id": job_id, **(gui or {})}))
    (rd / "flow.log").write_text("Starting...\n")
    t = time.time() - flow_age
    os.utime(rd / "flow.log", (t, t))
    return rd


def _interrupted(s, d, tag, rd, spec, jid="j-cut"):
    j = {"id": jid, "design_dir": str(d), "tag": tag, "run_dir": str(rd), "spec": spec,
         "state": "interrupted", "kind": "run", "group": None, "origin": "lanex"}
    s.queue_of(d)["jobs"].append(j)
    return j


def test_continue_sees_a_librelane_change(env, tmp_path, monkeypatch):
    from lanex.controller import branch
    s = env["s"]
    d = _proj(tmp_path)
    rd = _cut_run(d, "cut", [("1-verilator-lint", True)], job_id="j-cut",
                  gui={"run_mode": "container", "config_file": str(d / "config.json"),
                       "toolchain": {"librelane_version": "2.4.0", "image": "ghcr.io/librelane/librelane:2.4.0",
                                     "run_mode": "container"}})
    (rd / "resolved.json").write_text(json.dumps({"meta": {"librelane_version": "2.4.0", "flow": "Classic"}}))
    monkeypatch.setattr(branch, "versions_now", lambda: {"librelane": "3.0.4", "image": "ghcr.io/librelane/librelane:3.0.4"})
    j = _interrupted(s, d, "cut", rd, {})
    info = s.resume_info(j)
    assert info["can_continue"] is False
    assert {x["what"] for x in info["drift"]} >= {"librelane"}
    assert "2.4.0 then, 3.0.4 now" in info["reason"]


def test_continue_of_only_one_step_keeps_the_step_and_its_state(env, tmp_path):
    s = env["s"]
    d = _proj(tmp_path)
    parent_state = str(d / "runs" / "base" / "30-openroad-cts" / "state_out.json")
    rd = _cut_run(d, "only-drt", [("1-openroad-detailedrouting", False)], job_id="j-cut")
    spec = {"only": "OpenROAD.DetailedRouting", "initial_state": parent_state}
    j = _interrupted(s, d, "only-drt", rd, spec)
    info = s.resume_info(j)
    assert info["from_step"] == "OpenROAD.DetailedRouting" and info["state"] == parent_state
    out = s.recover(j["id"], "continue")
    sp = s.job(out["job"]["id"])["spec"]
    assert sp["only"] == "OpenROAD.DetailedRouting" and sp["initial_state"] == parent_state and not sp.get("frm")


def test_an_only_run_that_finished_is_complete(env, tmp_path):
    s = env["s"]
    d = _proj(tmp_path)
    rd = _cut_run(d, "only-done", [("1-openroad-detailedrouting", True)], job_id="j-cut")
    j = _interrupted(s, d, "only-done", rd, {"only": "OpenROAD.DetailedRouting", "initial_state": "/x"})
    info = s.resume_info(j)
    assert info["can_continue"] is False and info.get("complete")


def test_continue_with_nothing_finished_keeps_the_jobs_own_start(env, tmp_path):
    s = env["s"]
    d = _proj(tmp_path)
    parent_state = str(d / "runs" / "base" / "40-grt" / "state_out.json")
    rd = _cut_run(d, "fix-grt", [("1-openroad-globalrouting", False)], job_id="j-cut")
    spec = {"frm": "OpenROAD.GlobalPlacement", "to": "OpenROAD.DetailedRouting", "initial_state": parent_state}
    j = _interrupted(s, d, "fix-grt", rd, spec)
    out = s.recover(j["id"], "continue")
    sp = s.job(out["job"]["id"])["spec"]
    assert sp["frm"] == "OpenROAD.GlobalPlacement" and sp["initial_state"] == parent_state
    assert sp["to"] == "OpenROAD.DetailedRouting"


def test_continue_refuses_a_folder_of_another_job(env, tmp_path):
    s = env["s"]
    d = _proj(tmp_path)
    rd = _cut_run(d, "theirs", [("1-verilator-lint", True)], job_id="j-someone-else")
    j = _interrupted(s, d, "theirs", rd, {})
    with pytest.raises(ValueError, match="isn't this run's own"):
        s.recover(j["id"], "continue")
    # A continued run's folder names the first job: its continuation may go on.
    rd2 = _cut_run(d, "mine", [("1-verilator-lint", True)], job_id="j-first",
                   gui={"attempts": [{"how": "continue", "job_id": "j-second"}]})
    j2 = _interrupted(s, d, "mine", rd2, {"_continue_of": "j-first"}, jid="j-second")
    assert s.resume_info(j2)["can_continue"] is True


def test_continue_waits_while_the_run_may_still_be_going(env, tmp_path, monkeypatch):
    s = env["s"]
    d = _proj(tmp_path)
    rd = _cut_run(d, "busy", [("1-verilator-lint", True)], job_id="j-cut", flow_age=5)
    j = _interrupted(s, d, "busy", rd, {})
    with pytest.raises(ValueError, match="flow.log changed"):
        s.recover(j["id"], "continue")
    # Its own container is alive (docker ps was slow at start-up): followed again.
    t = time.time() - 300
    os.utime(rd / "flow.log", (t, t))
    j["container"] = "lanex-flow-busy"
    monkeypatch.setattr(jobs, "flow_containers", lambda: [{"name": "lanex-flow-busy", "id": "i", "workdir": str(d)}])
    started = []
    monkeypatch.setattr(termwatch.Follower, "start", lambda self: started.append(self.job_id))
    out = s.recover(j["id"], "continue")
    assert out["reattached"] and s.job("j-cut")["state"] == "running" and started == ["j-cut"]
    # Another LanEx container working in the project: refused.
    j3 = _interrupted(s, d, "busy", rd, {}, jid="j-cut3")
    monkeypatch.setattr(jobs, "flow_containers", lambda: [{"name": "lanex-flow-zz", "id": "i", "workdir": str(d)}])
    rd_meta = json.loads((rd / "gui-run.json").read_text())
    rd_meta["job_id"] = "j-cut3"
    (rd / "gui-run.json").write_text(json.dumps(rd_meta))
    with pytest.raises(ValueError, match="still working"):
        s.recover(j3["id"], "continue")


# ------------------------------------------------------------------ finding 6: a step run waiting for its next step
def _step_run_waiting(env, tmp_path):
    s, runners = env["s"], env["runners"]
    a, b = _proj(tmp_path, "a"), _proj(tmp_path, "b")
    r, _ = s.submit_now(a, {"mode": "semi", "run_mode": "container"})
    ra = runners[r["job_id"]]
    ra.awaiting_next = True
    ra.listener("info", {"paused": True, "next_step": "OpenROAD.Floorplan"})
    w = s.add(b, {"run_mode": "container"})
    s.tick()
    assert s.job(w["id"])["state"] == "running"
    out = s.resume_step(r["job_id"])
    assert out["started"] is False and s.job(r["job_id"])["state"] == "queued"
    return s, r["job_id"], ra, a


def test_stop_ends_a_step_run_waiting_for_its_next_step(env, tmp_path):
    s, jid, ra, a = _step_run_waiting(env, tmp_path)
    s.stop(jid)
    time.sleep(0.2)
    assert ra.cancelled is True
    j = s.job(jid)
    assert j is not None and j["state"] == "stopped" and not j.get("resume_pending")


def test_remove_on_a_step_run_waiting_stops_it_too(env, tmp_path):
    s, jid, ra, a = _step_run_waiting(env, tmp_path)
    s.remove(job_id=jid)
    time.sleep(0.2)
    assert ra.cancelled is True and s.job(jid)["state"] == "stopped"


def test_a_step_run_waiting_survives_a_restart_as_interrupted(env, tmp_path, monkeypatch):
    s, jid, ra, a = _step_run_waiting(env, tmp_path)
    saved = json.loads((a / ".lanex" / "queue.json").read_text())
    assert next(j for j in saved["jobs"] if j["id"] == jid)["resume_pending"] is True
    monkeypatch.setattr("lanex.controller.events.BOOT_ID", "ffffffff")
    s2 = _sched(tmp_path, {}, env["launched"])
    n = len(env["launched"])
    s2.queue_of(a)
    s2.tick()
    j = s2.job(jid)
    assert j["state"] == "interrupted"            # never started over from the first step
    assert len(env["launched"]) == n
