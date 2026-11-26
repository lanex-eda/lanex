# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The RTL IDE's schematic: Yosys reads the design the way LibreLane's
synthesis does (the config's files, defines, include dirs and top), turns
processes into cells (``proc``) and writes a JSON netlist; the page lays it
out and draws one module at a time.

Yosys runs in the LibreLane image (the host usually has none) with the
project mounted read-only and the JSON on stdout, so nothing is written into
the project. One job at a time, cancellable, cached by the inputs' sizes
and times. Cells carry ``src`` attributes (``top.v:21.13-21.16``), which is
how a click on the drawing finds its line.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import container_run, events

LEVELS = {
    # word-level cells ($add, $mux, $dff): what the code says
    "rtl": "proc; opt_clean",
    # constant and width clean-up: fewer, smaller cells (DigitalJS's choice)
    "simple": "proc; opt; wreduce; opt_clean",
    # Yosys's generic gates before the PDK's cells: big
    "gates": "synth -run begin:fine; techmap; opt_clean",
}
TIMEOUT_S = 120
MAX_JSON_BYTES = 20 * 1024 * 1024
_CACHE: Dict[str, str] = {}
_CACHE_ORDER: List[str] = []


def _q(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./=:+-]+", s or ""):
        return s
    return "'" + str(s).replace("'", "'\\''") + "'"


def yosys_script(*, files: Sequence[str], vhdl_files: Sequence[str] = (), top: str,
                 defines: Optional[Dict[str, Any]] = None, include_dirs: Sequence[str] = (),
                 level: str = "rtl", params: Optional[Dict[str, Any]] = None) -> str:
    """The Yosys commands, mirroring LibreLane's read (scripts/pyosys/synthesize.py):
    ``read_verilog -sv`` with the defines and include dirs, VHDL through the
    GHDL plugin, ``hierarchy -top`` with the config's parameters, then the
    level's passes, then ``write_json`` to stdout."""
    cmds: List[str] = []
    if files:
        parts = ["read_verilog", "-sv", "-noautowire"]
        for k, v in (defines or {}).items():
            parts.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
        for inc in include_dirs:
            parts.append("-I" + str(inc))
        parts += [str(f) for f in files]
        cmds.append(" ".join(_q(p) for p in parts))
    if vhdl_files:
        cmds.append("plugin -i ghdl")
        cmds.append("ghdl --std=08 " + " ".join(_q(str(f)) for f in vhdl_files) + " -e " + _q(top))
    hier = ["hierarchy", "-check", "-top", top]
    for k, v in (params or {}).items():
        if re.fullmatch(r"[A-Za-z_]\w*", str(k)):
            hier += ["-chparam", str(k), str(v)]
    cmds.append(" ".join(_q(p) for p in hier))
    cmds.append(LEVELS.get(level, LEVELS["rtl"]))
    cmds.append("write_json")
    return "; ".join(cmds)


def inputs(design_dir: str | Path, top: Optional[str] = None) -> Dict[str, Any]:
    """The design as a run builds it: the config's files, defines, include
    dirs, top and SYNTH_PARAMETERS; VHDL_FILES for a VHDL design."""
    from . import configedit, lint
    d = Path(design_dir).resolve()
    ci = lint.config_inputs(d)
    data: Dict[str, Any] = {}
    try:
        data = configedit.read_config(d).get("data") or {}
    except Exception:
        data = {}
    files = list(ci.get("files") or [])
    vhdl: List[str] = []
    vf = data.get("VHDL_FILES")
    if vf:
        for e in lint._as_list(vf):
            s = e[len("dir::"):] if e.startswith("dir::") else e
            for p in (sorted(d.glob(s)) if any(c in s for c in "*?[") else [d / s]):
                rel = lint._strip_dir(str(p), d)
                if rel and (d / rel).is_file() and rel not in vhdl:
                    vhdl.append(rel)
    if not files and not vhdl:
        from . import projectfiles
        for f in projectfiles.list_files(d).get("files") or []:
            if f.get("role") == "design" and f["rel"].lower().endswith((".v", ".sv")):
                files.append(f["rel"])
    params: Dict[str, Any] = {}
    sp = data.get("SYNTH_PARAMETERS")
    for item in lint._as_list(sp):
        k, eq, v = str(item).partition("=")
        if eq and re.fullmatch(r"[A-Za-z_]\w*", k.strip()):
            params[k.strip()] = v.strip()
    return {"files": files, "vhdl_files": vhdl, "defines": ci.get("defines") or {},
            "include_dirs": ci.get("include_dirs") or [], "top": top or data.get("DESIGN_NAME"),
            "params": params, "config": ci.get("name"), "mode": ci.get("mode"), "missing": ci.get("missing") or []}


def _cache_key(design_dir: Path, script: str, files: Sequence[str]) -> str:
    h = hashlib.sha256(script.encode())
    for f in files:
        try:
            st = (design_dir / f).stat()
            h.update(f"{f}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            h.update(f"{f}:missing".encode())
    return h.hexdigest()


def _keep(key: str, text: str) -> None:
    _CACHE[key] = text
    _CACHE_ORDER.append(key)
    while len(_CACHE_ORDER) > 4:
        _CACHE.pop(_CACHE_ORDER.pop(0), None)


def _first_error(out: str) -> str:
    for line in out.splitlines():
        if "ERROR" in line or ":error:" in line:
            return line.strip()
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else "Yosys stopped without a message."


class SchematicJob:
    """One Yosys read at a time. ``start`` returns at once; ``result(id)``
    says ``running`` / ``done`` (with the JSON text) / ``failed``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancel = threading.Event()
        self.jobs: Dict[str, Dict[str, Any]] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, design_dir: str | Path, *, top: Optional[str], level: str = "rtl") -> Dict[str, Any]:
        from . import tools
        d = Path(design_dir).resolve()
        inp = inputs(d, top)
        if not inp["top"]:
            return {"ok": False, "error": "The config names no top module (DESIGN_NAME), so there is nothing to draw yet."}
        if not inp["files"] and not inp["vhdl_files"]:
            return {"ok": False, "error": "No design files to read. Add a Verilog file under src/ first."}
        script = yosys_script(files=inp["files"], vhdl_files=inp["vhdl_files"], top=inp["top"],
                              defines=inp["defines"], include_dirs=inp["include_dirs"], level=level,
                              params=inp["params"])
        key = _cache_key(d, script, list(inp["files"]) + list(inp["vhdl_files"]))
        jid = uuid.uuid4().hex[:10]
        command = "yosys -q -p " + _q(script)
        if key in _CACHE:
            self.jobs = {jid: {"state": "done", "json": _CACHE[key], "ms": 0, "cached": True, "command": command,
                               "top": inp["top"], "level": level, "inputs": inp}}
            return {"ok": True, "id": jid, "cached": True, "command": command}
        eng = tools.resolve_engine()
        host = None
        try:
            from . import platform_env
            host = platform_env.usable_which("yosys")
        except Exception:
            host = None
        if not host and not eng.get("ready"):
            return {"ok": False, "error": "Drawing a schematic needs Yosys: the LibreLane image's, through Docker or "
                                          "Podman, or one installed on this machine. Set up a container engine on the Tools tab."}
        with self._lock:
            if self.running:
                self.cancel()
                if self._thread is not None:
                    self._thread.join(5)
            self._cancel.clear()
            argv: List[str]
            if host and not inp["vhdl_files"]:
                argv = [host, "-q", "-p", script]
                cwd = str(d)
            else:
                from .configcheck import _permission_args
                engine = eng["engine"]
                argv = [engine, "run", "--rm", *_permission_args(engine), "--network", "none",
                        "-v", f"{d}:/work:ro", "-w", "/work", container_run.image_ref(), "yosys", "-q", "-p", script]
                if eng.get("sg_wrap"):
                    argv = tools.sg_wrap_argv(argv)
                cwd = str(d)
            self.jobs = {jid: {"state": "running", "started": time.time(), "command": command, "top": inp["top"],
                               "level": level, "inputs": inp}}
            self._thread = threading.Thread(target=self._run, args=(jid, argv, cwd, key), daemon=True,
                                            name="lanex.schematic")
            self._thread.start()
        return {"ok": True, "id": jid, "command": command}

    def cancel(self) -> None:
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def result(self, jid: str) -> Dict[str, Any]:
        j = self.jobs.get(jid)
        if not j:
            return {"state": "gone"}
        return {k: v for k, v in j.items() if k != "inputs"} | {"inputs": {k: j["inputs"][k] for k in ("files", "vhdl_files", "top", "config")}}

    def _run(self, jid: str, argv: List[str], cwd: str, key: str) -> None:
        t0 = time.monotonic()
        job = self.jobs.get(jid)
        try:
            self._proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          stdin=subprocess.DEVNULL)
            try:
                out, err = self._proc.communicate(timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.communicate()
                job.update(state="failed", error=f"Yosys took longer than {TIMEOUT_S} s and was stopped. "
                                                 "Try the RTL level, or draw a smaller module.")
                return
            rc = self._proc.returncode
            if self._cancel.is_set():
                job.update(state="cancelled")
                return
            text = out.decode("utf-8", errors="replace")
            errs = err.decode("utf-8", errors="replace")
            start = text.find("{")
            if rc != 0 or start < 0:
                job.update(state="failed", error=_first_error(errs + "\n" + text), log=(errs + text)[-4000:])
                return
            body = text[start:]
            if len(body) > MAX_JSON_BYTES:
                job.update(state="failed", error="This design is too big to draw (its netlist is over 20 MB). "
                                                 "Use the RTL level, or trace one signal.")
                return
            try:
                json.loads(body)
            except ValueError:
                job.update(state="failed", error="Yosys wrote something LanEx couldn't read as a netlist.",
                           log=(errs + text)[-4000:])
                return
            _keep(key, body)
            job.update(state="done", json=body, ms=int((time.monotonic() - t0) * 1000))
        except FileNotFoundError as ex:
            job.update(state="failed", error=f"Couldn't start Yosys: {ex}")
        except Exception as ex:  # pragma: no cover - environment dependent
            job.update(state="failed", error=str(ex))
        finally:
            self._proc = None
            events.publish("schematic_done", {"id": jid, "state": job.get("state") if job else "gone"})


job = SchematicJob()
