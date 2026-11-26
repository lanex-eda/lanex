# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The job queue and the multi-runner (controller/jobs.py), keep-awake
(power.py) and the terminal-run follower (termwatch.py)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from lanex.controller import dse, jobs, power, termwatch
from lanex.server import routes


# ------------------------------------------------------------------ pure parts

def test_reserve_tag_never_shares_a_folder(tmp_path):
    a = jobs.reserve_tag(tmp_path, "glui-x")
    b = jobs.reserve_tag(tmp_path, "glui-x")
    c = jobs.reserve_tag(tmp_path, "glui-x")
    assert (a, b, c) == ("glui-x", "glui-x-2", "glui-x-3")
    assert all((tmp_path / "runs" / t).is_dir() for t in (a, b, c))
    jobs.unreserve(tmp_path, b)
    assert not (tmp_path / "runs" / b).exists()
    # An unnamed run gets LanEx's glui- name, reserved too.
    t = jobs.reserve_tag(tmp_path)
    assert t.startswith("glui-") and (tmp_path / "runs" / t).is_dir()


def test_reserve_tag_from_many_threads_gives_distinct_folders(tmp_path):
    got: List[str] = []
    lock = threading.Lock()

    def go():
        t = jobs.reserve_tag(tmp_path, "same")
        with lock:
            got.append(t)
    ths = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(set(got)) == 12


@pytest.mark.parametrize("cores,free,total,need,want", [
    (32, 46.0, 46.9, 2.1, 8),     # this PC: cores and the cap of 8 decide
    (8, 6.5, 8.0, 2.1, 2),        # a student laptop, WSL with 8 GB
    (8, 6.5, 8.0, 3.0, 1),        # the same laptop, a bigger design
    (4, 30.0, 32.0, 2.0, 2),      # 4 cores: 2 each
    (2, 1.0, 4.0, 2.0, 1),        # never 0
])
def test_auto_parallel(cores, free, total, need, want):
    assert jobs.auto_parallel(cores=cores, free_gb=free, total_gb=total, need=need)["n"] == want


def test_need_and_reserve():
    assert jobs.need_gb(None) == 2.0
    assert jobs.need_gb(1.3) == pytest.approx(2.125, abs=0.01)
    assert jobs.reserve_gb(8) == 2.0 and jobs.reserve_gb(64) == 6.4


@pytest.mark.parametrize("s,words", [
    (None, ""), (20, "less than a minute"), (61, "about 1 min"), (9 * 60 + 40, "about 10 min"),
    (23 * 60, "about 25 min"), (80 * 60, "about 1 h 20"), (120 * 60, "about 2 h"),
])
def test_eta_words(s, words):
    assert jobs.eta_words(s) == words


def test_list_schedule():
    # Two slots, one running with 60 s left, three waiting of 100 s.
    assert jobs.list_schedule([60.0], [100.0, 100.0, 100.0], 2) == 200.0
    assert jobs.list_schedule([], [50.0, 50.0], 1) == 100.0
    assert jobs.list_schedule([None], [10.0], 2) is None
    assert jobs.list_schedule([10.0], [None], 2) is None


def _clean_run(d: Path, tag: str, steps: List[tuple], complete: bool = True, mtime: float = 0):
    rd = d / "runs" / tag
    rd.mkdir(parents=True, exist_ok=True)
    for n, sl, secs in steps:
        s = rd / f"{n}-{sl}"
        s.mkdir()
        (s / "state_in.json").write_text("{}")
        if secs is not None:
            (s / "state_out.json").write_text("{}")
            (s / "runtime.txt").write_text(f"00:00:{secs:06.3f}")
    (rd / "flow.log").write_text("Starting...\n" + ("Flow complete.\n" if complete else ""))
    return rd


def test_step_times_and_estimates(tmp_path):
    steps = [(1, "verilator-lint", 2.0), (2, "yosys-synthesis", 10.0), (3, "openroad-floorplan", 4.0),
             (4, "openroad-detailedrouting", 30.0)]
    _clean_run(tmp_path, "a", steps)
    _clean_run(tmp_path, "b", [(1, "verilator-lint", 4.0), (2, "yosys-synthesis", 12.0),
                               (3, "openroad-floorplan", 6.0), (4, "openroad-detailedrouting", 40.0)])
    _clean_run(tmp_path, "broken", [(1, "verilator-lint", 99.0)], complete=False)
    t = jobs.step_times(tmp_path)
    assert t["order"][0] == "verilator-lint" and t["secs"]["yosys-synthesis"] == 11.0
    assert "broken" not in t["runs"] and len(t["runs"]) == 2
    assert jobs.estimate_for(t) == pytest.approx(3 + 11 + 5 + 35)
    assert jobs.estimate_for(t, to="OpenROAD.Floorplan") == pytest.approx(3 + 11 + 5)
    # Running: two steps done, 20 s into routing's 35.
    left = jobs.estimate_for(t, done=["Verilator.Lint", "Yosys.Synthesis", "OpenROAD.Floorplan"],
                             current="OpenROAD.DetailedRouting", current_elapsed=20)
    assert left == pytest.approx(15)
    assert jobs.estimate_for({"order": [], "secs": {}}) is None


def test_run_progress_and_step_after(tmp_path):
    rd = _clean_run(tmp_path, "r", [(1, "verilator-lint", 1.0), (2, "yosys-synthesis", 2.0),
                                     (3, "openroad-floorplan", None)], complete=False)
    p = jobs.run_progress(rd)
    assert p["last_done"][1] == "yosys-synthesis" and p["cut"][1] == "openroad-floorplan"
    assert p["state_out"].endswith("2-yosys-synthesis/state_out.json") and not p["complete"]
    ids = ["Verilator.Lint", "Yosys.Synthesis", "OpenROAD.Floorplan", "OpenROAD.PDN"]
    assert jobs.step_after(ids, "yosys-synthesis") == "OpenROAD.Floorplan"
    assert jobs.step_after(ids, None) == "Verilator.Lint"
    assert jobs.step_after(ids, "openroad-pdn") is None
    # A continued run: the cut step ran again in a later folder.
    s = rd / "4-openroad-floorplan"
    s.mkdir()
    for f in ("state_in.json", "state_out.json"):
        (s / f).write_text("{}")
    (s / "runtime.txt").write_text("1")
    p = jobs.run_progress(rd)
    assert p["last_done"][2] == "4-openroad-floorplan" and p["cut"] is None


def test_label_for():
    assert jobs.label_for({}, "run") == "Run"
    assert jobs.label_for({"to": "OpenROAD.CTS"}, "run") == "Run to OpenROAD.CTS"
    assert jobs.label_for({"sweep": {"values": {"CLOCK_PERIOD": "8"}}}, "point") == "Sweep CLOCK_PERIOD = 8"


def test_run_row_origin(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    assert jobs.run_row_origin(a) == {"started_by": "terminal", "job_id": None}
    (a / "gui-run.json").write_text(json.dumps({"job_id": "j-1"}))
    assert jobs.run_row_origin(a) == {"started_by": "lanex", "job_id": "j-1"}
    (a / "gui-run.json").write_text(json.dumps({"job_id": "j-2", "sweep": {"id": "s1", "values": {}}}))
    assert jobs.run_row_origin(a)["started_by"] == "sweep"


# ------------------------------------------------------------------ the scheduler

class FakeRunner:
    """Starts at once, ends when the test says so (``finish``)."""

    def __init__(self, job):
        self.job_id = job["id"]
        self.design_dir = job["design_dir"]
        self.listener = None
        self.started = None
        self.running = False
        self.awaiting_next = False
        self.container_name = None
        self.step_statuses: Dict[str, str] = {}
        self.cancelled = False
        self.resumed = 0

    def set_listener(self, fn):
        self.listener = fn

    def start(self, **kw):
        self.started = kw
        self.running = True
        return {"ok": True}

    def finish(self, **done):
        self.running = False
        self.listener("flow_done", done)

    def cancel(self):
        self.cancelled = True
        self.finish(cancelled=True)

    def resume(self):
        self.resumed += 1
        self.awaiting_next = False
        self.listener("step_started", {"step_id": "B", "index": 1, "total": 2})


@pytest.fixture()
def sched(tmp_path):
    s = jobs.Scheduler(persist=True)
    s._facts_override = {"cores": 32, "free_gb": 40.0, "total_gb": 46.0}
    runners: Dict[str, FakeRunner] = {}
    launched: List[Dict[str, Any]] = []

    def factory(job):
        r = FakeRunner(job)
        runners[job["id"]] = r
        return r

    def launcher(job, runner, share, dry, **kw):
        if dry:
            return {"ok": True}, 200
        tag = jobs.reserve_tag(job["design_dir"], (job.get("spec") or {}).get("tag"))
        s.note_tag(job["id"], tag)
        launched.append({"job": job["id"], "share": share, **kw})
        # As the real runner does: the run's gui-run.json names its job.
        rd = Path(job["design_dir"]) / "runs" / tag
        if job.get("kind") != "continue" and not (rd / "gui-run.json").exists():
            (rd / "gui-run.json").write_text(json.dumps({"job_id": job["id"]}))
        runner.start(tag=tag)
        return {"ok": True, "started": True, "tag": tag}, 200

    s.set_runner_factory(factory)
    s.set_launcher(launcher)
    s.runners_ = runners
    s.launched_ = launched
    jobs.write_setting("queue_max_parallel", 2)
    yield s
    jobs.write_setting("queue_max_parallel", None)


def _proj(tmp_path, name="p"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text("{}")
    return d


def test_two_projects_run_at_once_and_a_third_waits(sched, tmp_path):
    a, b, c = _proj(tmp_path, "a"), _proj(tmp_path, "b"), _proj(tmp_path, "c")
    ra, _ = sched.submit_now(a, {"run_mode": "container"})
    rb, _ = sched.submit_now(b, {"run_mode": "container"})
    assert ra["ok"] and rb["ok"] and ra["job_id"] != rb["job_id"]
    assert sched.running_count() == 2
    # Both have their own folder and their own runner.
    assert (a / "runs" / ra["tag"]).is_dir() and (b / "runs" / rb["tag"]).is_dir()
    rc, _ = sched.submit_now(c, {"run_mode": "container"})
    assert rc["ok"] is False and rc["busy"] is True and "most at once" in rc["reason"]
    # Queued instead, it starts by itself when one ends.
    j = sched.add(c, {"run_mode": "container"})
    sched.tick()
    assert sched.job(j["id"])["state"] == "queued"
    assert "Waiting its turn" in sched.job(j["id"])["reason"]
    sched.runners_[ra["job_id"]].finish()
    sched.tick()
    assert sched.job(ra["job_id"])["state"] == "done"
    assert sched.job(j["id"])["state"] == "running"
    # The queue file is the project's own.
    q = json.loads((c / ".lanex" / "queue.json").read_text())
    assert q["jobs"][0]["id"] == j["id"]


def test_threads_are_shared_between_runs_going_at_once(sched, tmp_path):
    a, b = _proj(tmp_path, "a"), _proj(tmp_path, "b")
    sched.submit_now(a, {})
    sched.submit_now(b, {})
    shares = [x["share"] for x in sched.launched_]
    assert shares[0]["share"] == 1 and shares[1]["share"] == 2 and shares[1]["cores"] == 16


def test_queue_order_move_remove_pause(sched, tmp_path, monkeypatch):
    a = _proj(tmp_path, "a")
    monkeypatch.setattr(jobs, "design_peak", lambda d: {"gb": 1.0, "basis": "measured"})
    sched.pause(True, a)
    ids = [sched.add(a, {"tag": f"t{i}"})["id"] for i in range(3)]
    sched.tick()
    assert all(sched.job(i)["state"] == "queued" for i in ids)
    assert sched.job(ids[0])["reason"] == "Queue paused"
    sched.move(ids[2], "top")
    assert [j["id"] for j in sched._waiting_sorted()] == [ids[2], ids[0], ids[1]]
    sched.move(ids[2], "down")
    assert [j["id"] for j in sched._waiting_sorted()] == [ids[0], ids[2], ids[1]]
    sched.remove(job_id=ids[1])
    assert sched.job(ids[1]) is None
    sched.pause(False)
    sched.tick()
    assert sched.job(ids[0])["state"] == "running" and sched.job(ids[2])["state"] == "running"


def test_stop_after_current_pauses_once_the_runs_end(sched, tmp_path):
    a = _proj(tmp_path, "a")
    r, _ = sched.submit_now(a, {})
    w = sched.add(a, {})
    sched.stop_after_current(True)
    sched.tick()
    assert sched.job(w["id"])["state"] == "queued"
    sched.runners_[r["job_id"]].finish()
    sched.tick()
    assert sched.job(w["id"])["state"] == "queued"
    assert sched.queue_of(a)["paused"] is True and not sched.snapshot()["stop_after_current"]


def test_stop_a_running_job_and_a_waiting_one(sched, tmp_path):
    a = _proj(tmp_path, "a")
    r, _ = sched.submit_now(a, {})
    w = sched.add(a, {})
    sched.stop(w["id"])
    assert sched.job(w["id"]) is None
    sched.stop(r["job_id"])
    deadline = time.time() + 2
    while sched.job(r["job_id"])["state"] != "stopped" and time.time() < deadline:
        time.sleep(0.02)
    assert sched.runners_[r["job_id"]].cancelled
    assert sched.job(r["job_id"])["state"] == "stopped"


def test_step_by_step_pause_releases_the_slot(sched, tmp_path):
    a, b = _proj(tmp_path, "a"), _proj(tmp_path, "b")
    jobs.write_setting("queue_max_parallel", 1)
    r, _ = sched.submit_now(a, {"mode": "semi"})
    runner = sched.runners_[r["job_id"]]
    runner.awaiting_next = True
    runner.listener("info", {"paused": True, "next_step": "OpenROAD.Floorplan"})
    assert sched.job(r["job_id"])["state"] == "paused"
    # Paused between steps holds no slot: another project's run starts.
    w = sched.add(b, {})
    sched.tick()
    assert sched.job(w["id"])["state"] == "running"
    # Next step while that one runs: first in line, then it goes.
    res = sched.resume_step(r["job_id"])
    assert res["started"] is False and "next step" in sched.job(r["job_id"])["reason"]
    sched.runners_[w["id"]].finish()
    sched.tick()
    assert runner.resumed == 1


def test_a_launch_that_fails_is_failed_with_its_reason(sched, tmp_path):
    a = _proj(tmp_path, "a")
    sched.set_launcher(lambda job, runner, share, dry, **kw: ({"ok": False, "started": False,
                                                                "reason": "CLOCK_PORT not found"}, 200))
    reply, st = sched.submit_now(a, {})
    assert st == 200 and reply["ok"] is False
    j = sched.job(reply["job_id"])
    assert j["state"] == "failed" and "CLOCK_PORT" in j["reason"] and j["outcome"]["result"] == "not-started"


def test_queue_survives_a_restart_and_offers_continue(sched, tmp_path, monkeypatch):
    a = _proj(tmp_path, "a")
    r, _ = sched.submit_now(a, {"run_mode": "container"})
    tag = r["tag"]
    rd = a / "runs" / tag
    # It got through synthesis, then LanEx was killed.
    for n, sl in ((1, "verilator-lint"), (2, "yosys-synthesis")):
        s = rd / f"{n}-{sl}"
        s.mkdir()
        for f in ("state_in.json", "state_out.json", "runtime.txt"):
            (s / f).write_text("{}" if f != "runtime.txt" else "1")
    (rd / "3-openroad-floorplan").mkdir()
    (rd / "3-openroad-floorplan" / "state_in.json").write_text("{}")
    (rd / "flow.log").write_text("Starting...\n")
    old = time.time() - 120          # nothing has written it for two minutes
    import os
    os.utime(rd / "flow.log", (old, old))
    queued = sched.add(a, {})
    # A new server: new boot id, a scheduler that reads the file.
    monkeypatch.setattr("lanex.controller.events.BOOT_ID", "ffffffff")
    s2 = jobs.Scheduler(persist=True)
    s2._facts_override = sched._facts_override
    monkeypatch.setattr(jobs, "flow_containers", lambda: [])
    monkeypatch.setattr(jobs.Scheduler, "_flow_steps",
                        lambda self, j: ["Verilator.Lint", "Yosys.Synthesis", "OpenROAD.Floorplan", "OpenROAD.PDN"])
    found = s2.recover_all([a])
    assert found["interrupted"] == 1
    j = s2.job(r["job_id"])
    assert j["state"] == "interrupted" and j["resume"]["from_step"] == "OpenROAD.Floorplan"
    assert j["resume"]["can_continue"] and j["resume"]["state"].endswith("2-yosys-synthesis/state_out.json")
    assert "2 steps had finished" in j["reason"]
    # The queue comes back paused; what waited still waits.
    assert s2.queue_of(a)["paused"] is True
    assert s2.job(queued["id"])["state"] == "queued"
    # Continue: a job in the same folder, -i the last state, --from the next step.
    out = s2.recover(r["job_id"], "continue")
    nj = s2.job(out["job"]["id"])
    assert nj["kind"] == "continue" and nj["spec"]["tag"] == tag
    assert nj["spec"]["frm"] == "OpenROAD.Floorplan" and nj["spec"]["initial_state"].endswith("state_out.json")
    assert s2.job(r["job_id"])["state"] == "stopped"
    assert s2.queue_of(a)["paused"] is False
    assert s2._waiting_sorted()[0]["id"] == nj["id"]


def test_a_container_still_running_is_followed_again(sched, tmp_path, monkeypatch):
    a = _proj(tmp_path, "a")
    r, _ = sched.submit_now(a, {})
    with sched._lock:
        sched.job(r["job_id"])["container"] = "lanex-flow-abc"
        sched._save(jobs.design_key(a))
    monkeypatch.setattr("lanex.controller.events.BOOT_ID", "eeeeeeee")
    s2 = jobs.Scheduler(persist=True)
    import os
    home = str(Path(os.environ["LANEX_HOME"]).resolve())
    monkeypatch.setattr(jobs, "flow_containers", lambda: [
        {"name": "lanex-flow-abc", "id": "x", "workdir": str(a)},
        # no labels (from before they existed), elsewhere: not this home's
        {"name": "lanex-flow-zzz", "id": "y", "workdir": "/elsewhere"},
        # no labels, in a project of this home: a leftover
        {"name": "lanex-flow-old", "id": "z", "workdir": str(a / "runs")},
        # another LanEx home on the same engine (the integration smoke test's case)
        {"name": "lanex-flow-other", "id": "o", "workdir": str(a),
         "labels": {"lanex.home": "/home/x/.lanex-other", "lanex.boot": "11111111", "lanex.pid": "1"}},
        # this home, an earlier boot whose server is gone
        {"name": "lanex-flow-gone", "id": "g", "workdir": "/elsewhere",
         "labels": {"lanex.home": home, "lanex.boot": "22222222", "lanex.pid": "999999999"}},
        # this home, this boot: this server's own
        {"name": "lanex-flow-mine", "id": "m", "workdir": "/elsewhere",
         "labels": {"lanex.home": home, "lanex.boot": "eeeeeeee", "lanex.pid": str(os.getpid())}},
    ])
    started = []
    monkeypatch.setattr(termwatch.Follower, "start", lambda self: started.append(self.job_id))
    found = s2.recover_all([a])
    assert found["reattached"] == 1 and started == [r["job_id"]]
    assert s2.job(r["job_id"])["state"] == "running" and s2.job(r["job_id"])["reattached"]
    # Only this home's leftovers are orphans.
    assert sorted(o["name"] for o in s2.snapshot()["orphans"]) == ["lanex-flow-gone", "lanex-flow-old"]


def test_a_container_of_a_live_server_of_this_home_is_not_an_orphan(monkeypatch):
    import os
    c = {"name": "lanex-flow-q", "labels": {"lanex.home": "/h", "lanex.boot": "aaaa", "lanex.pid": str(os.getpid())}}
    assert jobs.is_orphan(c, home="/h", boot="bbbb", known_dirs=[]) is False
    c["labels"]["lanex.pid"] = "999999999"
    assert jobs.is_orphan(c, home="/h", boot="bbbb", known_dirs=[]) is True
    assert jobs.is_orphan(c, home="/other", boot="bbbb", known_dirs=[]) is False


def test_run_containers_carry_the_starting_servers_labels(monkeypatch):
    from lanex.controller import librelane_entry, runner
    from lanex.controller.events import BOOT_ID
    import os
    text = runner.container_labels()
    kv = dict(line.split("=", 1) for line in text.splitlines())
    assert kv["lanex.boot"] == BOOT_ID and kv["lanex.pid"] == str(os.getpid())
    assert kv["lanex.home"] == str(Path(os.environ["LANEX_HOME"]).resolve())
    args = librelane_entry._label_args(text + "\nevil=1\nlanex.bad=a\nb")
    assert args[:2] == ["--label", "lanex.home=" + kv["lanex.home"]] and "evil=1" not in " ".join(args)
    # Inserted right after `run` in LibreLane's exec of the engine.
    import types
    calls = []
    fake = types.SimpleNamespace(os=os)
    monkeypatch.setitem(__import__("sys").modules, "librelane.container", fake)
    monkeypatch.setitem(__import__("sys").modules, "librelane", types.SimpleNamespace(container=fake))
    monkeypatch.setenv("LANEX_CONTAINER_LABELS", "lanex.boot=abcd")
    monkeypatch.setattr(os, "execlp", lambda f, *a: calls.append(a))
    librelane_entry._label_the_container()
    fake.os.execlp("docker", "docker", "run", "--rm", "--name", "n", "img")
    assert calls == [("docker", "run", "--label", "lanex.boot=abcd", "--rm", "--name", "n", "img")]


def test_a_continued_run_is_not_failed_by_its_cut_step(tmp_path):
    # Continue reruns the cut step in a new, higher-numbered folder; the cut
    # folder stays. Verify must not read it as a stopped step (it made a clean
    # continued spm run read "checks failed" live).
    from lanex.controller import verify
    rd = tmp_path / "runs" / "r1"
    steps = [(1, "verilator-lint", "Verilator.Lint", True), (2, "openroad-repairdesignpostgpl", "OpenROAD.RepairDesignPostGPL", False),
             (3, "openroad-repairdesignpostgpl", "OpenROAD.RepairDesignPostGPL", True), (4, "misc-reportmanufacturability",
             "Misc.ReportManufacturability", True)]
    for n, sl, sid, done in steps:
        s = rd / f"{n}-{sl}"
        s.mkdir(parents=True)
        (s / "config.json").write_text(json.dumps({"meta": {"step": sid}}))
        (s / "state_in.json").write_text("{}")
        if done:
            (s / "state_out.json").write_text("{}")
            (s / "runtime.txt").write_text("00:00:01.000")
    (rd / "final").mkdir()
    (rd / "flow.log").write_text("Starting...\nFlow complete.\n")
    st = verify._flow_state(verify._Run(rd))
    assert st["state"] == "complete" and st["stopped_step"] is None


def test_the_console_error_that_ends_a_run_is_kept_whole():
    # LibreLane prints some errors only on its console (Rich, 80 columns, a
    # path column): the run engine keeps the whole block for the failure card.
    from lanex.controller import runner
    lines = [
        "[07:23:58] VERBOSE  Running 'Checker.SetupViolations' at                       step.py:1139",
        "                    'runs/glui-1/74-checker-setupviolations'…",
        "[07:24:04] ERROR    The following error was encountered while running   __main__.py:187",
        "                    the flow:",
        "                    One or more deferred errors were encountered: One",
        "                    or more wildcards specified in",
        "                    SETUP_VIOLATION_CORNERS did not match any corners:",
        "                    - nomatch_*",
        "           ERROR    LibreLane will now quit.                            __main__.py:191",
    ]
    b = runner.error_block(lines, 2)
    assert b["exit_code"] == 2 and b["kind"] == "flow"
    assert "SETUP_VIOLATION_CORNERS did not match any corners: - nomatch_*" in b["text"]
    assert b["text"].splitlines()[-1] == "LibreLane will now quit."
    assert b["first"].startswith("The following error was encountered while running the flow:")
    assert "__main__.py" not in b["text"]
    cfg = ["[10:00:00] ERROR    There are 2 errors in the configuration file:    config.py:1",
           "                    * CLOCK_PERIOD: 'abc' is not a number",
           "                    * DIE_AREA: expected 4 numbers"]
    c = runner.error_block(cfg, 1)
    assert c["kind"] == "config" and "DIE_AREA: expected 4 numbers" in c["text"]
    assert runner.error_block(["all good"], 0)["text"] == ""


def test_runexplain_reads_a_console_error_on_a_finished_looking_folder(tmp_path):
    from lanex.controller import runexplain
    rd = tmp_path / "runs" / "r1"
    s = rd / "1-verilator-lint"
    s.mkdir(parents=True)
    (s / "config.json").write_text(json.dumps({"meta": {"step": "Verilator.Lint"}}))
    for f in ("state_in.json", "state_out.json"):
        (s / f).write_text("{}")
    (s / "runtime.txt").write_text("00:00:01.000")
    (rd / "final").mkdir()
    (rd / "flow.log").write_text("Starting...\nFlow complete.\n")
    assert runexplain.explain_run(rd)["kind"] == "clean"
    (rd / "gui-run.json").write_text(json.dumps({"outcome": {"result": "error", "error": "x",
                                                             "error_text": "One or more wildcards ... did not match any corners"}}))
    rx = runexplain.explain_run(rd)
    assert rx["kind"] == "halted" and any("did not match" in e for e in rx["errors"])


def test_resume_info_refuses_a_changed_config(sched, tmp_path):
    a = _proj(tmp_path, "a")
    rd = _clean_run(a, "r1", [(1, "verilator-lint", 1.0)], complete=False)
    cfg = a / "config.json"
    (rd / "gui-run.json").write_text(json.dumps({"job_id": "j-x", "config_file": str(cfg), "config_hash": "0" * 64}))
    j = {"id": "j-x", "design_dir": str(a), "tag": "r1", "run_dir": str(rd), "spec": {}}
    sched._flow_steps = lambda job: ["Verilator.Lint", "Yosys.Synthesis"]
    info = sched.resume_info(j)
    assert info["can_continue"] is False and "config.json changed" in info["reason"]


def test_snapshot_shape(sched, tmp_path):
    a = _proj(tmp_path, "a")
    sched.submit_now(a, {})
    sched.add(a, {})
    snap = sched.snapshot()
    for k in ("jobs", "running", "waiting", "paused", "stop_after_current", "max_parallel", "memory",
              "eta", "hold", "orphans", "boot"):
        assert k in snap
    assert snap["running"] == 1 and snap["waiting"] == 1
    assert snap["max_parallel"]["setting"] == 2 and "needs" in snap["max_parallel"]["why"] or \
        "doesn't know" in snap["max_parallel"]["why"]
    want, why, n = sched.hold_wanted()
    assert want and "1 run going" in why and "1 waiting" in why


def test_first_runs_of_a_design_go_one_at_a_time(sched, tmp_path):
    jobs.write_setting("queue_max_parallel", None)       # auto
    a = _proj(tmp_path, "a")
    sched.submit_now(a, {})
    w = sched.add(a, {})
    sched.tick()
    assert "First runs of a" in sched.job(w["id"])["reason"]


def test_memory_holds_a_run_back(sched, tmp_path, monkeypatch):
    a, b = _proj(tmp_path, "a"), _proj(tmp_path, "b")
    monkeypatch.setattr(jobs, "design_peak", lambda d: {"gb": 4.0, "basis": "measured"})
    sched._facts_override = {"cores": 32, "free_gb": 9.0, "total_gb": 16.0}
    sched.submit_now(a, {})
    w = sched.add(b, {})
    sched.tick()
    assert sched.job(w["id"])["reason"].startswith("Waiting for memory: needs 5.5 GB")


# ------------------------------------------------------------------ sweeps run N at a time

def test_a_six_point_sweep_runs_two_at_a_time(sched, tmp_path):
    a = _proj(tmp_path, "a")
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()

    def launcher(job, runner, share, dry, **kw):
        tag = jobs.reserve_tag(job["design_dir"], job["spec"].get("tag"))
        runner.start(tag=tag)
        with lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])

        def end():
            time.sleep(0.15)
            with lock:
                peak["now"] -= 1
            runner.finish()
        threading.Thread(target=end, daemon=True).start()
        return {"ok": True, "started": True, "tag": tag}, 200
    sched.set_launcher(launcher)
    monkeypatch_peak = jobs.design_peak
    jobs.design_peak = lambda d: {"gb": 1.0, "basis": "measured"}
    try:
        def start_one(tag, ov):
            j = sched.add(a, {"tag": tag}, kind="point", group="s1")
            fin = sched.wait_job(j["id"], timeout=10)
            return {"ok": fin["state"] == "done", "kind": "clean"}
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                sched.tick()
                time.sleep(0.02)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        job = dse.DseJob()
        job.start(start_one=start_one, overrides_list=[{"X": str(i)} for i in range(6)],
                  tags=[f"p{i}" for i in range(6)], parallel=6)
        deadline = time.time() + 15
        while job.active and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        assert not job.active
        assert len(job.done) == 6 and peak["max"] == 2
    finally:
        jobs.design_peak = monkeypatch_peak


def test_a_sweep_stop_rule_skips_the_points_still_waiting(sched, tmp_path):
    a = _proj(tmp_path, "a")
    sched.pause(True, a)
    job = dse.DseJob()
    ids: List[str] = []

    def start_one(tag, ov):
        if tag == "p0":
            return {"ok": True, "kind": "clean", "good": True}
        j = sched.add(a, {"tag": tag}, kind="point", group="s1")
        ids.append(j["id"])
        fin = sched.wait_job(j["id"], timeout=5) or {}
        return {"ok": False, "kind": "skipped", "skipped": True} if fin.get("state") == "skipped" else {"ok": True}
    job.start(start_one=start_one, overrides_list=[{}] * 4, tags=["p0", "p1", "p2", "p3"], parallel=4,
              stop_when_good=True, on_halt=lambda why: sched.skip_group("s1", why))
    deadline = time.time() + 5
    while job.active and time.time() < deadline:
        time.sleep(0.05)
    st = job.status()
    assert st["stopped_early"] and st["end_reason"] == "first-clean"
    assert sorted(st["skipped"]) == ["p1", "p2", "p3"]


# ------------------------------------------------------------------ keep-awake

def test_power_plan_per_platform():
    which = lambda name: "/usr/bin/" + name  # noqa: E731
    mac = power.plan("darwin", wsl=False, pid=42, which=which)
    assert mac["argvs"] == [["caffeinate", "-i", "-w", "42"]]
    lin = power.plan("linux", wsl=False, pid=42, which=which, desktop="ubuntu:GNOME")
    a0 = lin["argvs"][0]
    assert a0[0] == "systemd-inhibit" and "--what=sleep:idle" in a0 and "--mode=block" in a0
    assert a0[-4:] == ["tail", "--pid=42", "-f", "/dev/null"]
    assert lin["argvs"][1][:3] == ["gnome-session-inhibit", "--inhibit", "suspend:idle"]
    kde = power.plan("linux", wsl=False, pid=42, which=which, desktop="KDE")
    assert len(kde["argvs"]) == 1
    wsl = power.plan("linux", wsl=True, pid=42, which=which)
    assert wsl["argvs"] == [] and wsl["how"] == "windows-launcher"
    none = power.plan("linux", wsl=False, pid=42, which=lambda n: None, desktop="")
    assert none["argvs"] == [] and "can't be held awake" in none["note"]


def test_power_update_takes_and_releases(monkeypatch):
    started: List[List[str]] = []

    class P:
        def __init__(self, argv, **kw):
            started.append(argv)
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(power.subprocess, "Popen", P)
    monkeypatch.setattr(power, "plan", lambda why="": {"how": "caffeinate", "argvs": [["caffeinate", "-i"]], "note": ""})
    st = power.update(True, "1 run going", 1)
    assert st["hold"] and st["held_here"] and started == [["caffeinate", "-i"]]
    power.update(True, "1 run going", 1)
    assert len(started) == 1                       # no new holder per tick
    st = power.update(False)
    assert not st["hold"] and not power._procs
    power.set_enabled(False)
    try:
        assert power.update(True, "x", 1)["hold"] is False
    finally:
        power.set_enabled(True)


def test_power_hold_route(monkeypatch):
    from lanex.server import routes
    got = {}

    class H:
        path = "/api/power/hold"

        def _send_json(self, obj, status=200):
            got.update(obj=obj, status=status)
    monkeypatch.setattr(jobs.scheduler, "hold_wanted", lambda: (True, "2 runs going (spm), 3 waiting", 5))
    routes.h_power_hold(H())
    d = got["obj"]["data"]
    assert d["hold"] is True and d["reason"].startswith("2 runs going") and d["jobs"] == 5


# ------------------------------------------------------------------ terminal runs

def test_follower_turns_flow_log_into_run_events(tmp_path, monkeypatch):
    seen: List[tuple] = []
    monkeypatch.setattr(termwatch, "_emit", lambda kind, body: seen.append((kind, body)))
    f = termwatch.Follower(str(tmp_path), "RUN_1", job_id="term:x", origin="terminal")
    f.steps = ["Verilator.Lint", "Yosys.Synthesis"]
    f.feed("Starting...\nRunning 'Verilator.Lint' at 'runs/RUN_1/01-verilator-lint'...\nlint ok\n")
    f.feed("Running 'Yosys.Synth")         # a partial line waits
    assert [k for k, _ in seen].count("step_started") == 1
    f.feed("esis' at 'runs/RUN_1/02-yosys-synthesis'...\nFlow complete.\n")
    kinds = [k for k, _ in seen]
    assert kinds.count("step_started") == 2 and "step_done" in kinds and kinds[-1] == "flow_done"
    assert all(b["origin"] == "terminal" and b["job_id"] == "term:x" for _, b in seen)
    assert seen[-1][1].get("ok") is True and not f.alive


def test_follower_ends_when_the_process_is_gone(tmp_path, monkeypatch):
    seen: List[tuple] = []
    monkeypatch.setattr(termwatch, "_emit", lambda kind, body: seen.append((kind, body)))
    rd = _clean_run(tmp_path, "RUN_2", [(1, "verilator-lint", 1.0), (2, "yosys-synthesis", None)], complete=False)
    f = termwatch.Follower(str(tmp_path), "RUN_2", job_id="term:y", alive_check=lambda: False)
    f.offset = (rd / "flow.log").stat().st_size
    f.last_growth = time.time() - 120
    f.poll()
    assert seen[-1][0] == "flow_done" and "2-yosys-synthesis" in seen[-1][1]["error"]


def test_watcher_finds_a_fresh_terminal_run_only(tmp_path):
    w = termwatch.Watcher()

    class S:
        def owns_run_dir(self, rd):
            return rd.endswith("mine")
    w._sched = S()
    _clean_run(tmp_path, "term", [(1, "verilator-lint", 1.0)], complete=False)
    _clean_run(tmp_path, "done", [(1, "verilator-lint", 1.0)], complete=True)
    _clean_run(tmp_path, "mine", [(1, "verilator-lint", 1.0)], complete=False)
    g = _clean_run(tmp_path, "gui", [(1, "verilator-lint", 1.0)], complete=False)
    (g / "gui-run.json").write_text("{}")
    assert w.scan(str(tmp_path)) == ["term"]
    assert w.scan(str(tmp_path), now=time.time() + 3600) == []


# ==================================================================
# Review fixes R2-4, R2-10, R2-12, R1-4..R1-8: a queue file that came with a project starts
# nothing, settings writes are locked, a finished job's words, Continue, and Stop of a
# step-by-step run waiting for its next step.

_RF_STEPS = ["Verilator.Lint", "Yosys.Synthesis", "OpenROAD.Floorplan", "OpenROAD.GlobalPlacement",
         "OpenROAD.DetailedRouting", "Magic.StreamOut"]


class _RFRunner:
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


def _rf_sched(tmp_path: Path, runners: Dict[str, _RFRunner], launched: List[str]) -> jobs.Scheduler:
    s = jobs.Scheduler(persist=True)
    s._facts_override = {"cores": 32, "free_gb": 40.0, "total_gb": 46.0}
    s.set_runner_factory(lambda job: runners.setdefault(job["id"], _RFRunner(job)))

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
def rf_env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "design_peak", lambda dd: {"gb": 1.0, "basis": "measured"})
    monkeypatch.setattr(jobs, "flow_containers", lambda: [])
    monkeypatch.setattr(jobs.Scheduler, "_flow_steps", lambda self, j: list(_RF_STEPS))
    jobs.write_setting("queue_max_parallel", 1)
    runners: Dict[str, _RFRunner] = {}
    launched: List[str] = []
    yield {"s": _rf_sched(tmp_path, runners, launched), "runners": runners, "launched": launched}
    jobs.write_setting("queue_max_parallel", None)





def _plant(d: Path, doc: Dict[str, Any]) -> None:
    (d / ".lanex").mkdir(exist_ok=True)
    (d / ".lanex" / "queue.json").write_text(json.dumps(doc))


# ------------------------------------------------------------------ R2-4: a queue that came with the project
def test_a_planted_queue_starts_nothing(rf_env, tmp_path):
    s = rf_env["s"]
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
    assert rf_env["launched"] == [] and not (d / "runs" / "planted-by-the-project").exists()
    # The planted "running" job holds no slot: it is interrupted, not followed.
    assert s.job("j-planted-0002")["state"] == "interrupted" and s.running_count() == 0
    assert "another LanEx" in s.snapshot()["notices"][0]
    # Resume is the person's click: only then does the waiting run start, and
    # the file is LanEx's own from then on.
    s.pause(False)
    s.tick()
    assert rf_env["launched"] == ["j-planted-0001"]
    assert json.loads((d / ".lanex" / "queue.json").read_text())["owner"] == jobs.home_id()
    assert not s.queue_of(d).get("notice")


def test_this_homes_own_queue_is_used_as_it_was(rf_env, tmp_path):
    s = rf_env["s"]
    d = _proj(tmp_path)
    s.add(d, {"run_mode": "container"})            # writes the file, stamped
    s2 = _rf_sched(tmp_path, rf_env["runners"], rf_env["launched"])
    q = s2.queue_of(d)
    assert q["paused"] is False and not q.get("notice")
    s2.tick()
    assert len(rf_env["launched"]) == 1


def test_stop_never_removes_a_container_the_queue_file_named(rf_env, tmp_path, monkeypatch):
    s = rf_env["s"]
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


def test_reattach_needs_the_container_to_work_in_this_project(rf_env, tmp_path, monkeypatch):
    s = rf_env["s"]
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


def test_continue_sees_a_librelane_change(rf_env, tmp_path, monkeypatch):
    from lanex.controller import branch
    s = rf_env["s"]
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


def test_continue_of_only_one_step_keeps_the_step_and_its_state(rf_env, tmp_path):
    s = rf_env["s"]
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


def test_an_only_run_that_finished_is_complete(rf_env, tmp_path):
    s = rf_env["s"]
    d = _proj(tmp_path)
    rd = _cut_run(d, "only-done", [("1-openroad-detailedrouting", True)], job_id="j-cut")
    j = _interrupted(s, d, "only-done", rd, {"only": "OpenROAD.DetailedRouting", "initial_state": "/x"})
    info = s.resume_info(j)
    assert info["can_continue"] is False and info.get("complete")


def test_continue_with_nothing_finished_keeps_the_jobs_own_start(rf_env, tmp_path):
    s = rf_env["s"]
    d = _proj(tmp_path)
    parent_state = str(d / "runs" / "base" / "40-grt" / "state_out.json")
    rd = _cut_run(d, "fix-grt", [("1-openroad-globalrouting", False)], job_id="j-cut")
    spec = {"frm": "OpenROAD.GlobalPlacement", "to": "OpenROAD.DetailedRouting", "initial_state": parent_state}
    j = _interrupted(s, d, "fix-grt", rd, spec)
    out = s.recover(j["id"], "continue")
    sp = s.job(out["job"]["id"])["spec"]
    assert sp["frm"] == "OpenROAD.GlobalPlacement" and sp["initial_state"] == parent_state
    assert sp["to"] == "OpenROAD.DetailedRouting"


def test_continue_refuses_a_folder_of_another_job(rf_env, tmp_path):
    s = rf_env["s"]
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


def test_a_step_by_step_run_continues_from_its_next_step(rf_env, tmp_path):
    # Each step of a step-by-step run is its own LibreLane call, which ends
    # flow.log with "Flow complete.": that is not the whole flow done.
    s = rf_env["s"]
    d = _proj(tmp_path)
    rd = _cut_run(d, "steps", [("1-verilator-lint", True)], job_id="j-cut")
    (rd / "flow.log").write_text("Starting...\nFlow complete.\n")
    t = time.time() - 300
    os.utime(rd / "flow.log", (t, t))
    j = _interrupted(s, d, "steps", rd, {"mode": "semi"})
    info = s.resume_info(j)
    assert info["can_continue"] and info["from_step"] == _RF_STEPS[1]
    assert info["state"].endswith("1-verilator-lint/state_out.json")
    out = s.recover(j["id"], "continue")
    sp = s.job(out["job"]["id"])["spec"]
    assert sp["mode"] == "semi" and sp["frm"] == _RF_STEPS[1]
    # Its last step done: complete.
    rd2 = _cut_run(d, "steps2", [(f"{i + 1}-{jobs.slug(x)}", True) for i, x in enumerate(_RF_STEPS)], job_id="j-all")
    (rd2 / "flow.log").write_text("Flow complete.\n")
    j2 = _interrupted(s, d, "steps2", rd2, {"mode": "semi"}, jid="j-all")
    assert s.resume_info(j2).get("complete")


def test_continue_waits_while_the_run_may_still_be_going(rf_env, tmp_path, monkeypatch):
    s = rf_env["s"]
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
def _step_run_waiting(rf_env, tmp_path):
    s, runners = rf_env["s"], rf_env["runners"]
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


def test_stop_ends_a_step_run_waiting_for_its_next_step(rf_env, tmp_path):
    s, jid, ra, a = _step_run_waiting(rf_env, tmp_path)
    s.stop(jid)
    time.sleep(0.2)
    assert ra.cancelled is True
    j = s.job(jid)
    assert j is not None and j["state"] == "stopped" and not j.get("resume_pending")


def test_remove_on_a_step_run_waiting_stops_it_too(rf_env, tmp_path):
    s, jid, ra, a = _step_run_waiting(rf_env, tmp_path)
    s.remove(job_id=jid)
    time.sleep(0.2)
    assert ra.cancelled is True and s.job(jid)["state"] == "stopped"


def test_a_step_run_waiting_survives_a_restart_as_interrupted(rf_env, tmp_path, monkeypatch):
    s, jid, ra, a = _step_run_waiting(rf_env, tmp_path)
    saved = json.loads((a / ".lanex" / "queue.json").read_text())
    assert next(j for j in saved["jobs"] if j["id"] == jid)["resume_pending"] is True
    monkeypatch.setattr("lanex.controller.events.BOOT_ID", "ffffffff")
    s2 = _rf_sched(tmp_path, {}, rf_env["launched"])
    n = len(rf_env["launched"])
    s2.queue_of(a)
    s2.tick()
    j = s2.job(jid)
    assert j["state"] == "interrupted"            # never started over from the first step
    assert not j.get("resume_pending")
    assert len(rf_env["launched"]) == n


# ==================================================================
# Review fixes R1-9, R1-10, R1-15: a run's log is streamed, never read whole; the old
# /api/run-delete is gone; the queue's events carry slim jobs.

class _LogH:
    def __init__(self, path):
        self.path = path
        self.sent = None
        self.command = "GET"

    def _send_file(self, path, content_type, status=200, headers=None):
        self.sent = {"path": path, "type": content_type, "headers": dict(headers or {})}


def _log_run(tmp_path: Path, size: int) -> Path:
    rd = tmp_path / "p" / "runs" / "r1"
    rd.mkdir(parents=True)
    line = b"[INFO] a tool printed this line ......................................\n"
    with open(rd / "lanex-console.log", "wb") as f:
        n = 0
        while n < size:
            f.write(line)
            n += len(line)
    return rd


def test_log_download_and_load_full_are_streamed(tmp_path, monkeypatch):
    rd = _log_run(tmp_path, 9 * 1024 * 1024)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("read whole")))
    d = tmp_path / "p"
    h = _LogH(f"/api/run/log?tag=r1&design_dir={d}&download=1")
    routes.h_run_log(h)
    assert h.sent["path"] == str(rd / "lanex-console.log")
    assert h.sent["headers"]["Content-Disposition"].startswith("attachment;")
    h = _LogH(f"/api/run/log?tag=r1&design_dir={d}&raw=1")
    routes.h_run_log(h)
    assert h.sent["type"].startswith("text/plain") and "Content-Disposition" not in h.sent["headers"]


def test_a_log_range_is_at_most_8_mb(tmp_path, monkeypatch):
    _log_run(tmp_path, 9 * 1024 * 1024)
    got = {}
    monkeypatch.setattr(routes, "_respond", lambda h, payload, code=200: got.update(payload=payload, code=code))
    routes.h_run_log(_LogH(f"/api/run/log?tag=r1&design_dir={tmp_path / 'p'}"))
    p = got["payload"]
    assert got["code"] == 200 and p["to"] - p["from"] <= 8 * 1024 * 1024 and p["to"] == p["size"]


def test_the_old_run_delete_route_is_gone():
    assert "/api/run-delete" not in dict(routes.ROUTES)
    assert not hasattr(routes, "h_run_delete")
    assert "/api/run-delete" not in routes.ROUTE_METHODS


def test_queue_events_carry_slim_jobs(tmp_path):
    s = jobs.Scheduler(persist=False)
    s._facts_override = {"cores": 32, "free_gb": 40.0, "total_gb": 46.0}
    d = tmp_path / "p"
    d.mkdir()
    big = {"container": "x " * 400, "local": "y " * 400}
    for i in range(60):
        j = s.add(d, {"overrides": {"CLOCK_PERIOD": i}, "tag": f"t{i}"})
        with s._lock:
            k = s.job(j["id"])
            k.update(state="done", command=big, attempts=[{"at": "x", "how": "start"}] * 5,
                     outcome={"result": "ok", "ok": True, "good": True, "message": "m" * 2000},
                     resources={"threads": {f"V{n}": n for n in range(30)}, "rss_gb": 1.0, "need_gb": 2.0})
    with s._lock:
        s.job(s.jobs_of(d)[0]["id"]).update(state="interrupted")      # always carried
    full = s.snapshot()
    slim = s.snapshot(slim=True)
    assert len(full["jobs"]) == 60
    # Every job still waiting, going or interrupted, and the newest 8 ended.
    assert len(slim["jobs"]) == 9 and slim["ended_left_out"] == 51
    assert any(x["state"] == "interrupted" for x in slim["jobs"])
    per_full = len(json.dumps(full["jobs"])) / 60
    per_slim = len(json.dumps(slim["jobs"])) / 9
    assert per_slim < 800 and per_slim < per_full / 4, (per_full, per_slim)
    assert len(json.dumps(slim)) < len(json.dumps(full)) / 20
    j = next(x for x in slim["jobs"] if x["state"] == "done")
    assert "command" not in j and "attempts" not in j and j["outcome"]["ok"] is True
    assert j["spec"] == {"tag": j["spec"]["tag"]} and j["resources"] == {"rss_gb": 1.0, "need_gb": 2.0}
    # A waiting job keeps its whole spec (Undo of Remove queues it again).
    w = s.add(d, {"overrides": {"A": 1}, "tag": "w"})
    sw = next(x for x in s.snapshot(slim=True)["jobs"] if x["id"] == w["id"])
    assert sw["spec"]["overrides"] == {"A": 1}
    # The full form is still GET /api/queue's.
    assert "command" in full["jobs"][0]
