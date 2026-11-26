# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""A study (sweep) stops early when the rest would fail the same way: with
proof (a failure before any swept setting is read) or evidence (two of the
first three points failing at the same step with the same tool code). A clock
too fast (timing missed) is a result and never stops it."""
from __future__ import annotations

import time

import pytest

from lanex.controller import dse


def _drain(job, n=300):
    for _ in range(n):
        if not job.active:
            return
        time.sleep(0.02)
    raise AssertionError("sweep thread never finished")


def test_points_failing_the_same_way_where_the_setting_is_read_keep_going():
    # A utilisation sweep reaching its limit: routing fails the same way for
    # the fullest points. That is the answer the sweep is for, not a reason to
    # stop (the summary shows the pattern and offers Stop).
    job = dse.DseJob()
    ran = []

    def start_one(tag, ov):
        ran.append(tag)
        return {"ok": False, "kind": "halted", "stage": "routing", "step": "OpenROAD.GlobalRouting",
                "reason": "[GRT-0116] Global routing finished with congestion at " + tag}

    job.start(start_one=start_one, overrides_list=[{"FP_CORE_UTIL": str(v)} for v in (60, 70, 80)], tags=["a", "b", "c"])
    _drain(job)
    assert ran == ["a", "b", "c"] and job.end_reason == "done"


def test_a_failure_before_any_swept_setting_is_read_stops_the_rest():
    from lanex.controller import introspect
    if not introspect.flow_steps("Classic"):
        pytest.skip("LibreLane isn't importable here")
    job = dse.DseJob()
    ran = []

    def start_one(tag, ov):
        ran.append(tag)
        return {"ok": False, "kind": "halted", "stage": "synthesis", "step": "Yosys.Synthesis", "steps_done": 3,
                "reason": "Module `no_such_top' not found"}

    job.start(start_one=start_one, overrides_list=[{"FP_CORE_UTIL": str(v)} for v in (40, 50, 60, 70)], tags=list("abcd"))
    _drain(job)
    assert ran == ["a"], "one failure is proof: the others would fail the same way"
    assert job.end_reason == "failed-fast" and "before FP_CORE_UTIL is used" in job.end_detail


def test_timing_missed_never_counts_as_failing_the_same_way():
    job = dse.DseJob()
    ran = []

    def start_one(tag, ov):
        ran.append(tag)
        return {"ok": False, "kind": "checks-failed", "stage": "signoff", "reason": "sign-off checks failed: SetupViolations"}

    job.start(start_one=start_one, overrides_list=[{"CLOCK_PERIOD": v} for v in ("6", "5", "4")], tags=["a", "b", "c"])
    _drain(job)
    assert ran == ["a", "b", "c"], "a clock too fast is a result: every period runs"
    assert job.end_reason == "done"


def test_different_failures_do_not_stop_it():
    job = dse.DseJob()
    reasons = {"a": "DPL-0036", "b": "GRT-0116", "c": "DPL-0036"}

    def start_one(tag, ov):
        return {"ok": tag not in reasons, "kind": "halted" if tag in reasons else "clean", "step": "S" + tag,
                "reason": reasons.get(tag)}

    job.start(start_one=start_one, overrides_list=[{"X": "1"}] * 4, tags=["a", "b", "c", "d"])
    _drain(job)
    assert job.end_reason == "done", "the same code at different steps is not the same failure"


def test_failure_fingerprints():
    fp = dse.failure_fingerprint({"ok": False, "kind": "halted", "step": "OpenROAD.DetailedPlacement",
                                  "reason": "Detailed placement failed (DPL-0036)"})
    assert fp == "OpenROAD.DetailedPlacement|DPL-0036"
    assert dse.failure_fingerprint({"ok": False, "kind": "checks-failed", "reason": "x"}) is None
    assert dse.failure_fingerprint({"ok": True, "kind": "clean"}) is None
    a = dse.failure_fingerprint({"ok": False, "kind": "halted", "step": "S", "reason": "ERROR: file /a/b.v line 12 bad"})
    b = dse.failure_fingerprint({"ok": False, "kind": "halted", "step": "S", "reason": "ERROR: file /c/d.v line 99 bad"})
    assert a == b, "paths and numbers don't make two failures different"


def test_a_failure_before_the_swept_setting_is_read_stops_at_once():
    from lanex.controller import introspect
    if not introspect.flow_steps("Classic"):
        pytest.skip("LibreLane isn't importable here")
    why = dse.proof_stop({"ok": False, "kind": "halted", "step": "Yosys.Synthesis",
                          "reason": "Module `no_such_top' not found"}, ["FP_CORE_UTIL"])
    assert why and "before FP_CORE_UTIL is used" in why
    # At or after the first step that reads it: proves nothing.
    assert dse.proof_stop({"ok": False, "kind": "halted", "step": "OpenROAD.GlobalRouting", "reason": "GRT-0116"},
                          ["FP_CORE_UTIL"]) is None
    assert dse.proof_stop({"ok": False, "kind": "checks-failed", "step": "Yosys.Synthesis"}, ["FP_CORE_UTIL"]) is None
