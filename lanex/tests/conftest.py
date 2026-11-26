# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Pytest configuration.

If LibreLane isn't installed in the active Python but is cloned at
``./librelane`` or ``/tmp/librelane``, prepend that path so ``import librelane``
works for the controller tests. This makes offline builds possible.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Candidate roots, in priority order.
_candidates: list[Path] = []
for var in ("LIBRELANE_ROOT", "LIBRELANE_SRC"):
    if v := os.environ.get(var):
        _candidates.append(Path(v))

# Common relative and absolute locations.
for guess in ("librelane", "../librelane", "/tmp/librelane"):
    p = Path(guess).resolve()
    if p.is_dir() and (p / "librelane" / "__init__.py").is_file():
        _candidates.append(p)

sys.path[:0] = [str(p) for p in _candidates if str(p) not in sys.path]


# Isolate the LanEx home for the WHOLE test session so nothing writes into the real
# ``~/.lanex``. Set at import time (before any fixture — incl. module-scoped
# server fixtures — runs) so e.g. set-design-dir's known-designs recording can't
# leak test paths into the user's cross-design picker. conftest is only imported
# under pytest, so this never affects a real run. Both the new (LANEX_HOME) and the
# deprecated (LIBRELANE_GUI_HOME) env are set during the transition.
import tempfile  # noqa: E402
_test_home = tempfile.mkdtemp(prefix="lanex-test-home-")
os.environ["LANEX_HOME"] = _test_home
os.environ["LIBRELANE_GUI_HOME"] = _test_home


# The job queue (controller/jobs.py) is process-wide. Each test gets a fresh
# one that keeps nothing on disk, and a run a test starts uses the runner the
# test put in place (tests swap ``app.get_runner``): the queue makes one
# runner per job through ``app.make_runner``.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_job_queue(monkeypatch):
    try:
        from lanex.controller import jobs
        from lanex.server import app as _app
        from lanex.server import routes
    except Exception:
        yield
        return
    sched = jobs.Scheduler(persist=False)
    sched._facts_override = {"cores": 32, "free_gb": 40.0, "total_gb": 46.0}
    monkeypatch.setattr(jobs, "scheduler", sched)
    monkeypatch.setattr(_app, "make_runner", lambda job=None: _app.get_runner())
    routes.wire_jobs()
    yield
