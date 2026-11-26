# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""A finished queue job leaves no thread and no log behind (review R1-1):
the log-batching thread ends with the run, and a queue job's runner keeps no
copy of its events (pages read the shared bus)."""
from __future__ import annotations

import gc
import threading
import time
import weakref

from lanex.controller import events
from lanex.controller.runner import EventType, FlowRunner

FLUSH = "librelane.gui.FlowRunner.logflush"


def _flushers():
    return [t for t in threading.enumerate() if t.name == FLUSH]


def _one_job(i: int) -> "weakref.ref[FlowRunner]":
    r = FlowRunner(job_id=f"j-leak-{i}", design_dir="/tmp/leak")
    line = "[INFO] some tool output line ........................................"
    for _k in range(400):
        r._emit(EventType.LOG, {"message": "\n".join([line] * 50)})
    r._emit(EventType.FLOW_DONE, {"tag": "", "error": None})
    return weakref.ref(r)


def test_finished_jobs_leave_no_thread_and_no_runner():
    before = len(_flushers())
    refs = [_one_job(i) for i in range(6)]
    deadline = time.monotonic() + 3
    while len(_flushers()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_flushers()) <= before
    gc.collect()
    assert [w for w in refs if w() is not None] == []


def test_a_queue_jobs_runner_keeps_no_events_but_the_bus_gets_every_line():
    r = FlowRunner(job_id="j-bus", design_dir="/tmp/leak")
    start = events.bus.max_seq
    for k in range(5):
        r._emit(EventType.LOG, {"message": f"line {k}"})
    r._emit(EventType.INFO, {"message": "after"})
    assert r._events.qsize() == 0
    got = [e for e in events.bus.events_since(start) if e.get("job_id") == "j-bus"]
    assert [e["type"] for e in got] == ["log", "info"]
    assert got[0]["message"] == "\n".join(f"line {k}" for k in range(5))


def test_lines_after_a_quiet_spell_still_go_out():
    r = FlowRunner(job_id="j-quiet", design_dir="/tmp/leak")
    r.LOG_FLUSH_IDLE_S = 0.1
    r._running = True
    seen = []
    r.set_listener(lambda kind, body: seen.append((kind, body.get("message"))))
    r._emit(EventType.LOG, {"message": "first"})
    time.sleep(0.5)              # the thread flushed it, then ended for want of lines
    r._emit(EventType.LOG, {"message": "second"})
    time.sleep(0.5)
    msgs = [m for k, m in seen if k == "log"]
    assert msgs == ["first", "second"]
    r._running = False


def test_a_runner_with_no_job_still_drains_and_is_capped():
    r = FlowRunner()
    for k in range(FlowRunner.EVENTS_KEPT + 50):
        r._emit(EventType.INFO, {"message": f"m{k}"})
    evs = r.drain(block=False, timeout=0.05)
    assert len(evs) == FlowRunner.EVENTS_KEPT and evs[-1]["message"] == f"m{FlowRunner.EVENTS_KEPT + 49}"
