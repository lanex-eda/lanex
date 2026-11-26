# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Follow a run LanEx did not start (audit 2e), or one it lost (a restart).

A :class:`Follower` tails a run folder's ``flow.log`` by byte offset and
turns what LibreLane writes into the same events a LanEx run sends, so Flow
follows it live. What ``flow.log`` holds (LibreLane 3.0.4, checked):

* ``Starting…`` once per invocation (a continued run writes it again);
* ``Running '<step id>' at '<dir>'…`` when a step starts (its folder then
  gets ``state_out.json`` and ``runtime.txt`` when it ends);
* ``Flow complete.`` only when the whole flow ended well.

A failure writes no last line (LibreLane removes its file handlers before it
prints the error), so "it ended" is decided by whether a process still runs
it: a ``lanex-flow-*`` container, a container working in the project folder,
or a local ``librelane`` process whose folder is the project. Never from the
log going quiet: detailed routing can be silent for many minutes.

The :class:`Watcher` looks at the open projects' run folders every 2 s
(other known projects every 15 s) and starts a follower for any run whose
``flow.log`` grew in the last 30 s and that no LanEx job owns. Its events
and its row in ``GET /api/queue`` carry ``origin: "terminal"``; LanEx never
schedules it and can stop it only when it finds its container.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger("librelane.gui.termwatch")

_RUNNING = re.compile(r"Running '(?P<id>[A-Za-z0-9_.\-:+]+)' at")
_MARKUP = re.compile(r"\[/?repr\.[a-z_]+\]")
POLL_S = 1.0
FRESH_S = 30.0
QUIET_CHECK_S = 45.0
MAX_CHUNK = 4 * 1024 * 1024


_LISTENERS: Dict[str, Callable[[str, Dict[str, Any]], None]] = {}


def _emit(kind: str, body: Dict[str, Any]) -> None:
    from .events import bus
    bus.emit(kind, body)
    fn = _LISTENERS.get(body.get("job_id") or "")
    if fn is not None:
        try:
            fn(kind, body)
        except Exception:
            _log.debug("follower listener failed", exc_info=True)


def flow_steps_for(design_dir: str, run_dir: str) -> List[str]:
    """The step ids of the flow a run folder used: its resolved config's
    ``meta.flow`` (else Classic)."""
    name = None
    try:
        res = json.loads((Path(run_dir) / "resolved.json").read_text(encoding="utf-8"))
        meta = res.get("meta") or {}
        name = meta.get("flow") if isinstance(meta.get("flow"), str) else None
    except Exception:
        name = None
    try:
        from librelane.flows import Flow
        cls = Flow.factory.get(name or "Classic") or Flow.factory.get("Classic")
        return [s.id for s in (getattr(cls, "Steps", None) or [])]
    except Exception:
        return []


def _process_alive(design_dir: str, container: Optional[str]) -> Optional[bool]:
    """Whether something still runs this project's flow. None: can't tell."""
    from . import jobs
    try:
        conts = jobs.flow_containers()
    except Exception:
        conts = []
    if container:
        if any(c.get("name") == container for c in conts):
            return True
        out = jobs._engine_out("ps", "-q", "--filter", f"name=^{container}$")
        if out is not None:
            return bool(out.strip())
    dd = jobs.design_key(design_dir)
    # A container working in the project folder (LibreLane's own -w <cwd>).
    all_out = jobs._engine_out("ps", "--format", "{{.Names}}")
    if all_out:
        for name in all_out.split():
            wd = jobs._engine_out("inspect", "--format", "{{.Config.WorkingDir}}", name, timeout=4)
            if wd and jobs.design_key(wd.strip()) == dd:
                return True
    try:
        import psutil
        for p in psutil.process_iter(["cmdline", "cwd"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                if "librelane" in cmd and p.info.get("cwd") and jobs.design_key(p.info["cwd"]) == dd \
                        and p.pid != os.getpid():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return None


class Follower:
    """Turn one run folder's ``flow.log`` into live run events."""

    def __init__(self, design_dir: str, tag: str, *, job_id: str, origin: str = "terminal",
                 container: Optional[str] = None,
                 on_end: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 alive_check: Optional[Callable[[], Optional[bool]]] = None) -> None:
        self.design_dir = str(design_dir)
        self.tag = tag
        self.run_dir = str(Path(design_dir) / "runs" / tag)
        self.job_id = job_id
        self.origin = origin
        self.container = container
        self.on_end = on_end
        self._alive_check = alive_check
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.alive = True
        self.offset = 0
        self._partial = ""
        self.steps: List[str] = []
        self.current: Optional[str] = None
        self.index = -1
        self.started = time.time()
        self.last_growth = time.time()
        self._last_check = 0.0
        self.done_ids: List[str] = []

    # ---- events
    def _base(self) -> Dict[str, Any]:
        return {"tag": self.tag, "job_id": self.job_id, "design_dir": self.design_dir, "origin": self.origin}

    def public(self) -> Dict[str, Any]:
        return {"id": self.job_id, "design_dir": self.design_dir, "project": Path(self.design_dir).name,
                "origin": self.origin, "kind": "run", "label": "Started in a terminal", "state": "running",
                "tag": self.tag, "run_dir": self.run_dir, "engine": "container" if self.container else None,
                "container": self.container,
                "step": {"id": self.current, "index": self.index, "total": len(self.steps) or None},
                "reason": "Started outside LanEx. LanEx follows its log." +
                          ("" if self.container else " Stop it where you started it."),
                "started_at": None, "seq": 0, "estimate": None, "remaining_s": None}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"lanex.follow.{self.tag}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def seed(self) -> None:
        """Tell the pages what exists: the flow's steps, the ones already done."""
        self.steps = flow_steps_for(self.design_dir, self.run_dir)
        from . import jobs
        prog = jobs.run_progress(self.run_dir)
        done_slugs = {d[1] for d in prog["done"]}
        graph = []
        for sid in self.steps:
            st = "done" if jobs.slug(sid) in done_slugs else "pending"
            if st == "done":
                self.done_ids.append(sid)
            graph.append({"id": sid, "status": st})
        _emit("info", {**self._base(), "step_graph": graph,
                       "message": f"Following {self.tag}, a run started outside LanEx." if self.origin == "terminal"
                       else f"Following {self.tag} again after LanEx restarted."})

    def feed(self, text: str) -> None:
        """Lines of ``flow.log`` as they arrive (a partial last line waits)."""
        text = self._partial + text
        lines = text.split("\n")
        self._partial = lines.pop()
        out: List[str] = []
        for raw in lines:
            line = _MARKUP.sub("", raw.rstrip("\r"))
            m = _RUNNING.search(line)
            if m:
                if out:
                    _emit("log", {**self._base(), "message": "\n".join(out)})
                    out = []
                self._step(m.group("id"))
            if line.strip() == "Flow complete.":
                if out:
                    _emit("log", {**self._base(), "message": "\n".join(out + [line])})
                    out = []
                self._finish({"result": "ok"})
                return
            out.append(line)
        if out:
            _emit("log", {**self._base(), "message": "\n".join(out)})

    def _step(self, sid: str) -> None:
        if self.current and self.current != sid:
            _emit("step_done", {**self._base(), "step_id": self.current, "index": self.index})
            self.done_ids.append(self.current)
        self.current = sid
        try:
            self.index = self.steps.index(sid)
        except ValueError:
            self.index += 1
        total = len(self.steps) or None
        _emit("step_started", {**self._base(), "step_id": sid, "index": self.index, "total": total})
        _emit("progress", {**self._base(), "done": len(set(self.done_ids)), "total": total or 0, "current": sid})

    def _finish(self, result: Dict[str, Any]) -> None:
        if not self.alive:
            return
        self.alive = False
        if result.get("result") == "ok":
            if self.current:
                _emit("step_done", {**self._base(), "step_id": self.current, "index": self.index})
            _emit("flow_done", {**self._base(), "ok": True})
        else:
            if self.current:
                _emit("step_failed", {**self._base(), "step_id": self.current,
                                      "message": result.get("message") or "the run stopped"})
            _emit("flow_done", {**self._base(), "error": result.get("message") or "the run stopped"})
        self._stop.set()
        if self.on_end:
            try:
                self.on_end(self.job_id, result)
            except Exception:
                _log.debug("follower end hook failed", exc_info=True)

    def poll(self) -> None:
        log = Path(self.run_dir) / "flow.log"
        try:
            size = log.stat().st_size
        except OSError:
            size = 0
        if size < self.offset:          # rewritten (--overwrite): start over
            self.offset = 0
            self._partial = ""
        if size > self.offset:
            with open(log, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(min(MAX_CHUNK, size - self.offset))
            self.offset += len(chunk)
            self.last_growth = time.time()
            self.feed(chunk.decode("utf-8", "replace"))
            if not self.alive:
                return
        now = time.time()
        quiet = now - self.last_growth
        if quiet > QUIET_CHECK_S and now - self._last_check > QUIET_CHECK_S:
            self._last_check = now
            alive = self._alive_check() if self._alive_check else _process_alive(self.design_dir, self.container)
            if alive is False:
                from . import jobs
                prog = jobs.run_progress(self.run_dir)
                if prog["complete"]:
                    self._finish({"result": "ok"})
                else:
                    at = prog["cut"][2] if prog["cut"] else (self.current or "")
                    self._finish({"result": "error", "message": "The run ended without finishing" +
                                  (f" (it was in {at})" if at else "") + ". Its process is gone."})

    def _loop(self) -> None:
        try:
            self.seed()
        except Exception:
            _log.debug("follower seed failed", exc_info=True)
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                _log.debug("follower poll failed", exc_info=True)
            self._stop.wait(POLL_S)
        self.alive = False


class Watcher:
    """Find runs started outside LanEx in the projects LanEx knows."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._halt = threading.Event()
        self._sched: Any = None
        self._seen: Dict[str, float] = {}
        self._last_all = 0.0

    def start(self, scheduler: Any) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._sched = scheduler
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lanex.termwatch")
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()

    def designs_now(self, every: bool) -> List[str]:
        out: List[str] = []
        try:
            from ..server import routes
            a = routes._get_active_design_dir()
            if a:
                out.append(a)
        except Exception:
            pass
        if every:
            try:
                from . import designs
                out += designs.list_designs()[:30]
            except Exception:
                pass
        seen, uniq = set(), []
        for d in out:
            from . import jobs
            k = jobs.design_key(d)
            if k not in seen:
                seen.add(k)
                uniq.append(k)
        return uniq

    def scan(self, design_dir: str, now: Optional[float] = None) -> List[str]:
        """Run folders of *design_dir* written in the last 30 s that nobody
        follows yet (their tags)."""
        now = now or time.time()
        runs = Path(design_dir) / "runs"
        try:
            dirs = sorted((e for e in os.scandir(runs) if e.is_dir()), key=lambda e: e.stat().st_mtime, reverse=True)[:8]
        except OSError:
            return []
        found = []
        for e in dirs:
            log = Path(e.path) / "flow.log"
            try:
                mt = log.stat().st_mtime
            except OSError:
                continue
            if now - mt > FRESH_S:
                continue
            if self._sched is not None and self._sched.owns_run_dir(e.path):
                continue
            if (Path(e.path) / "gui-run.json").exists():
                continue        # LanEx's own run (its job may be from an older server)
            try:
                with open(log, "rb") as f:
                    f.seek(max(0, log.stat().st_size - 2048))
                    if b"Flow complete." in f.read():
                        continue
            except OSError:
                continue
            found.append(e.name)
        return found

    def _loop(self) -> None:
        from . import jobs
        while not self._halt.is_set():
            try:
                now = time.time()
                every = now - self._last_all > 15
                if every:
                    self._last_all = now
                for d in self.designs_now(every):
                    for tag in self.scan(d, now):
                        jid = "term:" + jobs.design_key(d) + ":" + tag
                        if self._sched.job(jid) is not None:
                            continue
                        f = Follower(d, tag, job_id=jid, origin="terminal",
                                     on_end=lambda i, r: self._sched.detach_follower(i))
                        self._sched.attach_follower(jid, f)
                        f.start()
            except Exception:
                _log.debug("terminal-run scan failed", exc_info=True)
            self._halt.wait(2.0)


watcher = Watcher()
