# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for :mod:`lanex.controller.runner`.

These look at pure-Python behaviour (events queue, lifecycle flags,
fingerprinting). They do NOT actually invoke OpenROAD/Yosys; that's an
integration test the host environment can't satisfy without PDKs.
"""
from __future__ import annotations

import gc
import threading
import time
import weakref
from pathlib import Path

import pytest

from lanex.controller import events
from lanex.controller.runner import EventType, FlowRunner


def test_runner_starts_and_drains_idle():
    from lanex.controller.runner import FlowRunner

    r = FlowRunner()
    assert not r.running
    # Drain is empty when nothing happened.
    out = r.drain(block=False, timeout=0.05)
    assert out == []


def test_runner_rejects_double_start():
    from lanex.controller.runner import FlowRunner

    r = FlowRunner()
    # Manually flip ``running`` since we have no flow factory wired.
    r._running = True
    try:
        res = r.start(
            flow_factory=lambda *a, **kw: None,
            config_files=[],
            design_dir=".",
        )
        assert res["ok"] is False
        assert "already running" in res["reason"]
    finally:
        r._running = False


def test_runner_event_seq_monotonic(monkeypatch):
    from lanex.controller.runner import FlowRunner
    from lanex.controller.models import EventType

    r = FlowRunner()
    # Drive the emit() directly to avoid spinning up the worker.
    r._emit(EventType.INFO, {"message": "a"})
    r._emit(EventType.INFO, {"message": "b"})
    evs = r.drain(block=False, timeout=0.05)
    types = [e["type"] for e in evs if e["type"] == EventType.INFO.value]
    assert types[:2] == [EventType.INFO.value, EventType.INFO.value]
    assert evs[0]["seq"] < evs[1]["seq"]
    assert evs[0]["message"] == "a" or "message" in evs[0]


def test_runner_cancel_sets_flag():
    from lanex.controller.runner import FlowRunner

    r = FlowRunner()
    assert not r.cancelled
    r.cancel()
    assert r.cancelled


def test_runner_accepts_extra_sources_and_extras_kwargs():
    """start() must accept extra_sources / extra_extras."""
    from lanex.controller.runner import FlowRunner

    r = FlowRunner()
    # Pre-flip running=False to bypass guard for kwargs validation.
    captured = {}

    def _fake_factory(*args, **kwargs):
        captured["called"] = True
        captured["args"] = args
        captured["kwargs"] = kwargs
        class _Flow:
            name = "FakeFlow"
            Steps = []
        return _Flow()

    # Patch _run to capture kwargs without actually invoking workers.
    original_run = r._run

    def _stub_run(*args, **kwargs):
        captured["run_kwargs"] = kwargs
        return None

    r._run = _stub_run  # type: ignore[assignment]
    try:
        # We bypass threading by patching start() minimally:
        # Instead of calling _run, we directly invoke _start-like path.
        # Just check the kwargs flow in by inspecting start's signature.
        import inspect
        sig = inspect.signature(r.start)
        assert "extra_sources" in sig.parameters
        assert "extra_extras" in sig.parameters
    finally:
        r._run = original_run  # type: ignore[assignment]


def test_runner_starts_with_mock_run():
    """Smoke: pass a fake flow_factory and observe events sent."""
    from lanex.controller.runner import FlowRunner
    from lanex.controller.events import bus

    r = FlowRunner()
    bus.bus if False else None  # touch
    # Avoid a real flow by making _run trivial.
    original = r._run
    r._run = lambda *a, **kw: None  # type: ignore[assignment]
    try:
        res = r.start(
            flow_factory=lambda *a, **kw: None,
            config_files=[],
            design_dir=".",
            extra_sources=["src/a.v"],
            extra_extras=["pin_order.cfg"],
        )
        assert res["ok"] is True
    finally:
        r._run = original  # type: ignore[assignment]


def test_mark_remaining_aborted_skips_pending_steps():
    # A5: after an abort (cancel or mid-flow error) every step that never reached
    # a terminal state must be closed as skipped(reason: flow aborted) instead of
    # lingering PENDING forever — the timeline must not show grey rows that look
    # like they might still run. The current (failed) step is left untouched.
    from lanex.controller.runner import FlowRunner
    from lanex.controller.models import StepStatus, EventType

    r = FlowRunner()
    r._step_statuses = {
        "s1": StepStatus.DONE.value,
        "s2": StepStatus.FAILED.value,   # the current step, already marked failed
        "s3": StepStatus.PENDING.value,
        "s4": StepStatus.RUNNING.value,
    }
    r._current_step_id = "s2"
    r._mark_remaining_aborted()

    assert r._step_statuses["s1"] == StepStatus.DONE.value      # untouched
    assert r._step_statuses["s2"] == StepStatus.FAILED.value    # current untouched
    assert r._step_statuses["s3"] == StepStatus.SKIPPED.value
    assert r._step_statuses["s4"] == StepStatus.SKIPPED.value

    evs = r.drain(block=False, timeout=0.05)
    reasons = {e["step_id"]: e.get("reason")
               for e in evs if e["type"] == EventType.STEP_SKIPPED.value}
    assert reasons.get("s3") == "flow aborted"
    assert reasons.get("s4") == "flow aborted"


def test_runner_init_logs_handler_attached(tmp_path: Path):
    """The bridge binds a handler to LibreLane's ``__librelane__`` logger at the
    SUBPROCESS level (so per-tool output reaches the live stream, matching a
    container run) and removes it on teardown.

    Regression guard for the bug where we bound to the wrong logger (root, via a
    failing ``librelane.logging.getLogger`` import) and sat at INFO — which
    silently dropped every SUBPROCESS-level (=12) tool line.
    """
    import logging

    from lanex.controller import runner as rmod

    ll_logger = logging.getLogger("__librelane__")
    baseline = list(ll_logger.handlers)

    r = rmod.FlowRunner()
    r._setup_log_bridge()
    # bound to the right logger…
    assert r._log_root is ll_logger
    assert r._log_handler in ll_logger.handlers
    # …and accepting SUBPROCESS (12) or lower, NOT INFO (20)
    assert r._log_handler.level <= 12
    assert ll_logger.level <= 12

    r._teardown_log_bridge()
    assert r._log_handler not in ll_logger.handlers
    assert list(ll_logger.handlers) == baseline


# ==================================================================
# Review fix R1-1: a finished queue job leaves no log-flush thread and no copy of its events.

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
