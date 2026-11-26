# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""A study's Stop buttons end it as stopped, never as failed fast (review R1-3);
a job's end is fully written before wait_job returns (R1-15); keep-awake
restarts only the holder that died, with a pause (R1-15)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List

import pytest

from lanex.controller import dse, jobs, power


class FakeRunner:
    def __init__(self, job):
        self.job_id = job["id"]
        self.listener = None
        self.container_name = None
        self.awaiting_next = False
        self.step_statuses: Dict[str, str] = {}
        self.cancelled = False

    def set_listener(self, fn):
        self.listener = fn

    def finish(self, **done):
        self.listener("flow_done", done)

    def cancel(self):
        self.cancelled = True
        self.finish(cancelled=True)


@pytest.fixture()
def study(tmp_path, monkeypatch):
    from lanex.server import routes
    s = jobs.scheduler                      # the conftest's fresh queue
    runners: Dict[str, FakeRunner] = {}
    s.set_runner_factory(lambda job: runners.setdefault(job["id"], FakeRunner(job)))

    def launcher(job, runner, share, dry, **kw):
        if dry:
            return {"ok": True}, 200
        tag = jobs.reserve_tag(job["design_dir"], (job.get("spec") or {}).get("tag"))
        s.note_tag(job["id"], tag)
        return {"ok": True, "started": True, "tag": tag}, 200
    s.set_launcher(launcher)
    s.set_outcome_reader(None)
    monkeypatch.setattr(jobs, "design_peak", lambda dd: {"gb": 1.0, "basis": "measured"})
    monkeypatch.setattr(dse, "point_outcome", lambda rd: {"ok": True, "kind": "clean", "good": True, "steps_done": 50})
    jobs.write_setting("queue_max_parallel", 1)
    d = tmp_path / "proj"
    d.mkdir()
    (d / "config.json").write_text("{}")
    sid = "20261126-120000"
    pts = [{"CLOCK_PERIOD": "10"}, {"CLOCK_PERIOD": "8"}, {"CLOCK_PERIOD": "6"}]
    tags = dse.sweep_run_tags(d, pts)
    dse.record_sweep(d, {"id": sid, "tags": tags, "points": pts, "ended": None,
                         "axes": [{"var": "CLOCK_PERIOD", "values": ["10", "8", "6"]}]})
    start_one = routes._dse_run_one_blocking(str(d), d / "config.json", "Classic", "container", {}, {}, sweep_id=sid)
    assert not dse.job.active
    dse.job.start(start_one=start_one, overrides_list=pts, tags=tags, sweep_id=sid,
                  on_point=lambda tag, res: dse.record_point(d, sid, tag, res),
                  on_done=lambda end: dse.record_end(d, sid, end), parallel=len(tags),
                  on_halt=lambda why: s.skip_group(sid, why))
    for _ in range(50):
        if len(s.group_jobs(sid)) == 3:
            break
        time.sleep(0.02)
    s.tick()
    running = [j for j in s.group_jobs(sid) if j["state"] == "running"]
    assert len(running) == 1
    yield {"s": s, "d": d, "sid": sid, "runners": runners, "running": running[0], "routes": routes}
    dse.job.cancel()
    for _ in range(200):
        if not dse.job.active:
            break
        s.tick()
        time.sleep(0.02)
    jobs.write_setting("queue_max_parallel", None)


def _end(st):
    s = st["s"]
    for _ in range(300):
        s.tick()
        if not dse.job.active:
            return dse.job.status()
        time.sleep(0.02)
    raise AssertionError("the study never ended")


def _cancel(st, body):
    out = {}

    class H:
        _body = body
    st["routes"]._respond = lambda h, payload, code=200: out.update(payload=payload, code=code)
    st["routes"].h_dse_cancel(H())
    return out


def test_stop_after_the_running_points_ends_as_stopped(study, monkeypatch):
    monkeypatch.setattr(study["routes"], "_respond", study["routes"]._respond)
    out = _cancel(study, {"sweep_id": study["sid"], "after_running": True})
    assert out["code"] == 200 and len(out["payload"]["skipped"]) == 2
    # The waiting points stay in the queue as skipped; the running one goes on.
    states = sorted(j["state"] for j in study["s"].group_jobs(study["sid"]))
    assert states == ["running", "skipped", "skipped"]
    study["runners"][study["running"]["id"]].finish()
    st = _end(study)
    assert st["end_reason"] == "stopped", st
    assert len(st["skipped"]) == 2 and len(st["done"]) == 1 and st["failed"] == []
    man = next(x for x in dse.load_sweeps(study["d"]) if x["id"] == study["sid"])
    assert man["ended"]["reason"] == "stopped"


def test_stop_now_ends_as_stopped(study, monkeypatch):
    monkeypatch.setattr(study["routes"], "_respond", study["routes"]._respond)
    out = _cancel(study, {"sweep_id": study["sid"]})
    assert out["code"] == 200 and out["payload"]["stopped"] == [study["running"]["id"]]
    st = _end(study)
    assert st["end_reason"] == "stopped" and len(st["skipped"]) == 2
    assert study["runners"][study["running"]["id"]].cancelled


def test_points_taken_out_of_the_queue_are_skipped_not_failed(study):
    s = study["s"]
    s.remove(group=study["sid"])           # the queue popover's Remove on each waiting point
    study["runners"][study["running"]["id"]].finish()
    st = _end(study)
    assert st["end_reason"] != "failed-fast", st
    assert len(st["skipped"]) == 2 and st["failed"] == []


# ------------------------------------------------------------------ wait_job and the peak
def test_wait_job_returns_after_the_peak_is_written(tmp_path, monkeypatch):
    s = jobs.Scheduler(persist=False)
    s._facts_override = {"cores": 32, "free_gb": 40.0, "total_gb": 46.0}
    runners: Dict[str, FakeRunner] = {}
    s.set_runner_factory(lambda job: runners.setdefault(job["id"], FakeRunner(job)))
    d = tmp_path / "p"
    (d / "runs").mkdir(parents=True)

    def launcher(job, runner, share, dry, **kw):
        tag = jobs.reserve_tag(job["design_dir"], None)
        (Path(job["design_dir"]) / "runs" / tag / "gui-run.json").write_text(json.dumps({"job_id": job["id"]}))
        return {"ok": True, "started": True, "tag": tag}, 200
    s.set_launcher(launcher)
    slow = []

    def reader(rd):
        time.sleep(1.3)                 # reading the folder takes a while (past wait_job's 1 s wake-up)
        slow.append(rd)
        return {"ok": True, "good": True}
    s.set_outcome_reader(reader)
    r, _ = s.submit_now(d, {})
    jid = r["job_id"]
    s._mem[jid] = {"peak_gb": 1.7, "src": "cgroup"}
    runners[jid].finish()
    import threading
    got = {}
    gui = d / "runs" / r["tag"] / "gui-run.json"

    def waiter():
        got["j"] = s.wait_job(jid, timeout=10)
        got["meta"] = json.loads(gui.read_text())      # what the sweep reads the moment it returns
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    tk = threading.Thread(target=s.tick)              # the scheduler's own thread
    tk.start()
    t.join(8)
    tk.join(8)
    assert got["j"]["state"] == "done" and slow
    assert (got["meta"].get("resources") or {}).get("peak_gb") == 1.7, "the peak is in gui-run.json when wait_job returns"
    # The sweep's stamp then keeps it.
    dse.stamp_run(d / "runs" / r["tag"], "s1", {"X": "1"})
    meta = json.loads((d / "runs" / r["tag"] / "gui-run.json").read_text())
    assert meta["resources"]["peak_gb"] == 1.7 and meta["sweep"]["id"] == "s1"
    assert not list((d / "runs" / r["tag"]).glob("*.tmp"))


# ------------------------------------------------------------------ keep-awake holders
def test_one_dead_holder_is_restarted_alone_and_with_a_pause(monkeypatch):
    started: List[List[str]] = []
    procs = []

    class P:
        def __init__(self, argv, **kw):
            started.append(argv)
            self.argv = argv
            self.alive = True
            procs.append(self)

        def poll(self):
            return None if self.alive else 1

        def terminate(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0
    clock = [1000.0]
    monkeypatch.setattr(power.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(power.time, "sleep", lambda s: None)
    monkeypatch.setattr(power.subprocess, "Popen", P)
    monkeypatch.setattr(power, "plan", lambda why="": {"how": "a+b", "argvs": [["a"], ["b"]], "note": ""})
    power.release_all()
    try:
        power.update(True, "1 run going", 1)
        assert started == [["a"], ["b"]]
        procs[1].alive = False            # "b" dies at once, every time
        for _ in range(10):               # ten ticks inside its first pause
            clock[0] += 0.4
            power.update(True, "1 run going", 1)
        assert started == [["a"], ["b"]], "no restart storm"
        clock[0] += 5
        power.update(True, "1 run going", 1)
        assert started == [["a"], ["b"], ["b"]], "only the dead one is started again"
        procs[-1].alive = False
        clock[0] += 5.1                   # its pause doubled: 10 s now
        power.update(True, "1 run going", 1)
        assert len(started) == 3
        clock[0] += 5
        power.update(True, "1 run going", 1)
        assert started[-1] == ["b"] and len(started) == 4
        assert procs[0].alive, "the holder that worked was never touched"
        assert power.state()["held_here"] in (True, False)
    finally:
        power.update(False)
