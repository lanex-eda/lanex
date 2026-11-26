# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Review fixes R1-9 (a run's log is streamed, never read whole), R1-10 (the
old /api/run-delete is gone) and R1-15 (the queue's events carry slim jobs)."""
from __future__ import annotations

import json
from pathlib import Path

from lanex.controller import jobs
from lanex.server import routes


class _H:
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
    h = _H(f"/api/run/log?tag=r1&design_dir={d}&download=1")
    routes.h_run_log(h)
    assert h.sent["path"] == str(rd / "lanex-console.log")
    assert h.sent["headers"]["Content-Disposition"].startswith("attachment;")
    h = _H(f"/api/run/log?tag=r1&design_dir={d}&raw=1")
    routes.h_run_log(h)
    assert h.sent["type"].startswith("text/plain") and "Content-Disposition" not in h.sent["headers"]


def test_a_log_range_is_at_most_8_mb(tmp_path, monkeypatch):
    _log_run(tmp_path, 9 * 1024 * 1024)
    got = {}
    monkeypatch.setattr(routes, "_respond", lambda h, payload, code=200: got.update(payload=payload, code=code))
    routes.h_run_log(_H(f"/api/run/log?tag=r1&design_dir={tmp_path / 'p'}"))
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
    full = s.snapshot()
    slim = s.snapshot(slim=True)
    per_full = len(json.dumps(full["jobs"])) / 60
    per_slim = len(json.dumps(slim["jobs"])) / 60
    assert per_slim < 800 and per_slim < per_full / 4, (per_full, per_slim)
    j = slim["jobs"][0]
    assert "command" not in j and "attempts" not in j and j["outcome"]["ok"] is True
    assert j["spec"] == {"tag": j["spec"]["tag"]} and j["resources"] == {"rss_gb": 1.0, "need_gb": 2.0}
    # A waiting job keeps its whole spec (Undo of Remove queues it again).
    w = s.add(d, {"overrides": {"A": 1}, "tag": "w"})
    sw = next(x for x in s.snapshot(slim=True)["jobs"] if x["id"] == w["id"])
    assert sw["spec"]["overrides"] == {"A": 1}
    # The full form is still GET /api/queue's.
    assert "command" in full["jobs"][0]
