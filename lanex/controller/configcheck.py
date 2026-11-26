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
"""What a run will do, checked before it starts.

Two things live here:

* :func:`resolve_flow` picks the flow a run uses the way LibreLane's own CLI
  does (``librelane/__main__.py``): Classic unless the config's ``meta.flow``
  names another flow (or lists steps), ``meta.substituting_steps`` applied, and
  an explicitly requested flow over both. An unknown name is refused with the
  real flow names, never quietly swapped for another flow (audit G12).

* :func:`check` loads the config with LibreLane's own loader, in the place the
  run would use it (the LibreLane image for a container run, this machine's
  LibreLane for a local one), and then checks that ``DESIGN_NAME`` is a module
  the sources declare and that ``CLOCK_PORT`` is a port of it (audit 1c). The
  load is a few seconds (a container start), so its answer is cached by the
  content of everything it read: the config files, the per-run overrides, the
  PDK and the project's files.

A config LibreLane refuses blocks the run outright (the run would stop in its
first second). The top-module and clock-port findings come from reading the
Verilog, so the person can still choose to run anyway.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_log = logging.getLogger("librelane.gui.configcheck")

# Flows that open a viewer instead of building a chip.
_VIEWER_FLOWS = {"OpenInKLayout", "OpenInOpenROAD", "OpenInMagic"}


def _librelane_version() -> str:
    try:
        from . import compat

        return str(compat.get_version())
    except Exception:
        return ""


def flow_names() -> List[str]:
    """The flows the installed LibreLane registers (Classic, Chip, …)."""
    try:
        from librelane.flows import Flow

        return list(Flow.factory.list())
    except Exception:
        return []


def _names_text() -> str:
    names = [n for n in flow_names() if n not in _VIEWER_FLOWS]
    return ", ".join(names) if names else "Classic"


def _ll() -> str:
    v = _librelane_version()
    return "LibreLane " + v if v else "LibreLane"


def resolve_flow(config_files: Sequence[str], requested: Optional[str] = None) -> Dict[str, Any]:
    """The flow a run of *config_files* uses.

    Returns ``{cls, name, cli, source, steps, error}``: ``cls`` the Flow class
    the runner builds, ``name`` what to show ("Classic", "Chip", "Custom (5
    steps)"), ``cli`` the ``-f`` value to pass on (``None`` when the config's
    own ``meta`` decides, so substitutions it names are kept, as in a
    terminal), ``source`` "request" | "config" | "default". ``error`` is a plain
    refusal (and ``cls`` is None) for an unknown flow.

    A *requested* flow equal to what the config already says is the config's
    flow (the page sends back the name it was shown)."""
    out: Dict[str, Any] = {"cls": None, "name": "Classic", "cli": None, "source": "default",
                           "steps": [], "error": None, "config": None}
    try:
        from librelane.config import Config
        from librelane.flows import Flow, SequentialFlow
    except Exception as ex:  # pragma: no cover - librelane missing
        out["error"] = f"LibreLane isn't available here: {ex}"
        return out
    target = Flow.factory.get("Classic")
    name = "Classic"
    subst = False
    for cf in config_files or []:
        try:
            meta = Config.get_meta(str(cf))
        except Exception:
            meta = None   # an unreadable file: the config load says why
        if not meta:
            continue
        fname = Path(str(cf)).name
        if isinstance(meta.flow, str):
            found = Flow.factory.get(meta.flow)
            if found is None:
                out["error"] = (f"{fname} asks for the flow '{meta.flow}' (meta.flow), which {_ll()} "
                                f"doesn't have. Its flows are: {_names_text()}. Change meta.flow in {fname}.")
                return out
            target, name = found, meta.flow
            out.update(source="config", config=fname)
        elif isinstance(meta.flow, list):
            try:
                make = getattr(SequentialFlow, "Make", None) or SequentialFlow.make
                target = make([str(s) for s in meta.flow])
            except Exception as ex:
                out["error"] = (f"{fname} lists its own flow steps (meta.flow) and LibreLane can't build "
                                f"that flow: {ex}. Fix the step names in {fname}.")
                return out
            name = f"Custom ({len(meta.flow)} steps)"
            out.update(source="config", config=fname)
        if meta.substituting_steps is not None:
            if meta.flow is None:
                out["error"] = (f"{fname} sets meta.substituting_steps without meta.flow; LibreLane "
                                f"refuses that. Add meta.flow (for example Classic) to {fname}.")
                return out
            try:
                if issubclass(target, SequentialFlow):
                    target = target.Substitute(meta.substituting_steps)
                    subst = True
            except Exception as ex:
                out["error"] = f"{fname}'s meta.substituting_steps can't be applied: {ex}."
                return out
    if subst:
        name += " (with substituted steps)"
    if requested and str(requested) != name:
        found = Flow.factory.get(str(requested))
        if found is None:
            out["error"] = (f"{_ll()} has no flow called '{requested}'. "
                            f"Its flows are: {_names_text()}.")
            return out
        target, name = found, str(requested)
        out.update(cli=str(requested), source="request")
    out["cls"] = target
    out["name"] = name
    try:
        out["steps"] = [s.id for s in (getattr(target, "Steps", None) or [])]
    except Exception:
        out["steps"] = []
    return out


def flow_info(config_files: Sequence[str], requested: Optional[str] = None) -> Dict[str, Any]:
    """:func:`resolve_flow` without the class: what the page needs."""
    r = resolve_flow(config_files, requested)
    return {k: r[k] for k in ("name", "cli", "source", "steps", "error", "config")}


# ---------------------------------------------------------------- the loader probe

# Runs in the LibreLane image (container runs) or this machine's Python (local
# runs): builds the flow the way LibreLane's CLI does, which loads and checks
# the whole config, and prints one JSON line. Nothing is written anywhere.
PROBE = r'''
import json, sys
a = json.loads(sys.argv[1])
out = {"ok": False}
try:
    import librelane
    out["librelane_version"] = getattr(librelane, "__version__", None)
    from librelane.flows import Flow, SequentialFlow
    from librelane.config import Config
    # A key LibreLane doesn't know (a typo) or that this flow doesn't read is
    # only a logged warning (config/config.py): the run goes ahead and the
    # setting does nothing. Catch those lines for the check.
    import logging
    _said = []
    class _Said(logging.Handler):
        def emit(self, r):
            try:
                m = r.getMessage()
            except Exception:
                return
            if "unknown key" in m or "unused by the current flow" in m:
                _said.append(m)
    logging.getLogger("__librelane__").addHandler(_Said(level=logging.WARNING))
    out["config_warnings"] = _said
    try:
        from librelane.config import InvalidConfig
    except Exception:
        InvalidConfig = None
    target = Flow.factory.get("Classic")
    for cf in a["configs"]:
        meta = Config.get_meta(cf)
        if not meta:
            continue
        if isinstance(meta.flow, str):
            found = Flow.factory.get(meta.flow)
            if found is None:
                raise ValueError("Unknown flow '%s' in meta.flow" % meta.flow)
            target = found
        elif isinstance(meta.flow, list):
            target = (getattr(SequentialFlow, "Make", None) or SequentialFlow.make)(meta.flow)
        if meta.substituting_steps is not None and issubclass(target, SequentialFlow):
            target = target.Substitute(meta.substituting_steps)
    if a.get("flow"):
        target = Flow.factory.get(a["flow"])
        if target is None:
            raise ValueError("Unknown flow '%s'" % a["flow"])
    kw = {"design_dir": a["design_dir"], "config_override_strings": a.get("overrides") or []}
    for k in ("pdk", "scl", "pdk_root"):
        if a.get(k):
            kw[k] = a[k]
    flow = target(a["configs"], **kw)
    c = flow.config
    def val(k):
        try:
            v = c.get(k)
        except Exception:
            return None
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v]
        return str(v)
    out.update(ok=True, design_name=val("DESIGN_NAME"), clock_port=val("CLOCK_PORT"),
               verilog_files=val("VERILOG_FILES"), include_dirs=val("VERILOG_INCLUDE_DIRS"),
               vhdl_files=val("VHDL_FILES"))
except Exception as e:
    errs = list(getattr(e, "errors", None) or [])
    if InvalidConfig is not None and isinstance(e, InvalidConfig):
        out.update(kind="invalid", errors=[str(x) for x in errs] or [str(e)],
                   warnings=[str(x) for x in (getattr(e, "warnings", None) or [])])
    elif (isinstance(e, (ValueError, KeyError, TypeError))
          or type(e).__name__ in ("PassedDirectoryError", "TclError")
          or type(e).__module__.split(".")[0] == "yaml"):
        out.update(kind="config", errors=[str(e)])
    else:
        out.update(kind="crash", errors=["%s: %s" % (type(e).__name__, e)])
sys.stdout.write("\nLANEX_CHECK " + json.dumps(out, default=str) + "\n")
sys.stdout.flush()
'''

_MARK = "LANEX_CHECK "


def _parse_probe(stdout: str) -> Optional[Dict[str, Any]]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(_MARK):
            try:
                v = json.loads(line[len(_MARK):])
                return v if isinstance(v, dict) else None
            except Exception:
                return None
    return None


def _relativize(paths: Optional[Sequence[str]], base: Path) -> List[str]:
    out = []
    for p in paths or []:
        if not p:
            continue
        try:
            rp = Path(p)
            if rp.is_absolute():
                out.append(str(rp.resolve().relative_to(base)))
                continue
        except Exception:
            pass
        out.append(str(p))
    return out


def _override_strings(overrides: Optional[Dict[str, Any]], extra_sources: Optional[Sequence[str]],
                      design_dir: Path) -> List[str]:
    """The ``-c`` values the run passes, as the runner builds them."""
    out = [f"{k}={v}" for k, v in (overrides or {}).items()]
    src = _relativize(extra_sources, design_dir)
    if src:
        out.append("VERILOG_FILES=" + " ".join(src))
    return out


def _load_root(pdk: Optional[str], pdk_ready: Optional[Dict[str, Any]], run_mode: str) -> Optional[str]:
    """The PDK root LibreLane's loader needs: the folder holding ``<pdk>/``.

    A container run names the ciel home (``~/.ciel``) and LibreLane's CLI turns
    it into the pinned version's folder; the loader is handed that folder."""
    pf = pdk_ready or {}
    if not pf.get("ready"):
        return None
    if run_mode == "container" and pdk:
        for w in pf.get("where") or []:
            p = Path(str(w))
            for parent in [p] + list(p.parents):
                if (parent / pdk).is_dir():
                    return str(parent)
    return pf.get("pdk_root") or None


_PERM_CACHE: Dict[str, List[str]] = {}


def _permission_args(engine: str) -> List[str]:
    """``--user uid:gid`` where LibreLane adds it (rootful Docker on Linux)."""
    if engine in _PERM_CACHE:
        return _PERM_CACHE[engine]
    args: List[str] = []
    if sys.platform.startswith("linux") and "docker" in Path(engine).name and hasattr(os, "getuid"):
        rootless = False
        try:
            r = subprocess.run([engine, "info", "--format", "{{.SecurityOptions}}"],
                               capture_output=True, text=True, timeout=10)
            rootless = "rootless" in (r.stdout or "")
        except Exception:
            pass
        if not rootless:
            args = ["--user", f"{os.getuid()}:{os.getgid()}"]
    _PERM_CACHE[engine] = args
    return args


def _container_probe_argv(args: Dict[str, Any], *, engine: str, image: str, design_dir: Path,
                          pdk_root: Optional[str], name: str) -> List[str]:
    """``<engine> run`` for the probe, with the mounts LibreLane's own
    ``run_in_container`` makes (home, the PDK store, the design folder when it
    is outside home), read-only, no network, never pulling an image."""
    home = str(Path.home())
    mounts: List[str] = []
    seen = set()

    def mount(p: Optional[str]) -> None:
        if not p:
            return
        p = os.path.abspath(p)
        if p in seen or any(p == s or p.startswith(s.rstrip("/") + "/") for s in seen):
            return
        seen.add(p)
        mounts.extend(["-v", f"{p}:{p}:ro"])

    mount(home)
    mount(pdk_root)
    mount(str(design_dir))
    return ([engine, "run", "--rm", "--pull=never", "--network=none", "--name", name]
            + _permission_args(engine) + mounts
            + ["-e", "HOME=/tmp", "-w", str(design_dir), image, "python3", "-c", PROBE, json.dumps(args)])


def _run_probe(args: Dict[str, Any], *, run_mode: str, design_dir: Path, pdk_root: Optional[str],
               timeout: float) -> Dict[str, Any]:
    """Run :data:`PROBE` where the run would load its config. Returns the
    probe's own answer, or ``{"kind": "unavailable", "why": …}`` when the
    check itself could not run (that never blocks a run)."""
    if run_mode == "container":
        try:
            from . import container_run, tools

            resolved = tools.resolve_engine()
            eng = resolved.get("engine")
            engine = shutil.which(eng) if eng else None
            image = container_run.image_ref()
        except Exception as ex:
            return {"kind": "unavailable", "why": f"the container engine couldn't be asked ({ex})"}
        if not engine or not image:
            return {"kind": "unavailable", "why": "no usable Docker or Podman"}
        name = "lanex-check-" + uuid.uuid4().hex[:12]
        argv = _container_probe_argv(args, engine=engine, image=image, design_dir=design_dir,
                                     pdk_root=pdk_root, name=name)
        if resolved.get("sg_wrap"):
            argv = ["sg", "docker", "-c", shlex.join(argv)]
        where = "the LibreLane image"
    else:
        engine, name = None, None
        argv = [sys.executable, "-c", PROBE, json.dumps(args)]
        where = "this machine's LibreLane"
    try:
        r = subprocess.run(argv, cwd=str(design_dir), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        if engine and name:
            try:
                subprocess.run([engine, "rm", "-f", name], capture_output=True, timeout=15)
            except Exception:
                pass
        return {"kind": "unavailable", "why": f"it took longer than {int(timeout)} s", "where": where}
    except Exception as ex:
        return {"kind": "unavailable", "why": str(ex), "where": where}
    got = _parse_probe(r.stdout)
    if got is None:
        tail = " ".join(((r.stderr or "") + " " + (r.stdout or "")).split())[-300:]
        if re.search(r"No such image|image not known|not found locally|Unable to find image", tail, re.I):
            return {"kind": "unavailable", "why": "the LibreLane image isn't pulled yet", "where": where}
        return {"kind": "unavailable", "why": tail or f"exit code {r.returncode}", "where": where}
    got["where"] = where
    return got


# ---------------------------------------------------------------- cache

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_MAX = 48
_LOCK = threading.Lock()
_INFLIGHT: Dict[str, threading.Event] = {}
_SKIP_DIRS = {"runs", "sim_build", "__pycache__", "node_modules", "obj_dir"}


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _tree_print(design_dir: Path, cap: int = 3000) -> List[Tuple[str, int, int]]:
    """Names, sizes and times of the project's files (``runs/`` and hidden
    folders left out): a file added, edited or deleted changes the key, so a
    cached answer never outlives what it read (a glob, an SDC path)."""
    out: List[Tuple[str, int, int]] = []
    try:
        for root, dirs, files in os.walk(design_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
            for f in sorted(files):
                p = os.path.join(root, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((os.path.relpath(p, design_dir), st.st_size, st.st_mtime_ns))
                if len(out) >= cap:
                    return out
    except Exception:
        pass
    return out


def _file_hash(p: str) -> str:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except Exception:
        return "missing"


def _key(args: Dict[str, Any], *, run_mode: str, design_dir: Path) -> str:
    image = ""
    if run_mode == "container":
        try:
            from . import container_run

            image = container_run.image_ref()
        except Exception:
            pass
    blob = json.dumps({
        "args": args, "mode": run_mode, "image": image, "ll": _librelane_version(),
        "configs": [(c, _file_hash(c)) for c in args.get("configs") or []],
        "tree": _tree_print(design_dir),
    }, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _probe_cached(args: Dict[str, Any], *, run_mode: str, design_dir: Path, pdk_root: Optional[str],
                  reuse_failures: bool, timeout: float) -> Tuple[Dict[str, Any], bool]:
    """The probe's answer for *args*, from the cache when the same inputs were
    checked before. A cached refusal is only reused when *reuse_failures*
    (Run checks again, so a stale "no" can never block a run). Two callers
    asking at once share one probe."""
    key = _key(args, run_mode=run_mode, design_dir=design_dir)
    while True:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None and (hit.get("ok") or reuse_failures):
                return hit, True
            ev = _INFLIGHT.get(key)
            if ev is None:
                ev = threading.Event()
                _INFLIGHT[key] = ev
                mine = True
            else:
                mine = False
        if not mine:
            ev.wait(timeout + 30)
            with _LOCK:
                hit = _CACHE.get(key)
            if hit is not None:
                return hit, True
            continue
        try:
            got = _run_probe(args, run_mode=run_mode, design_dir=design_dir, pdk_root=pdk_root, timeout=timeout)
            if got.get("kind") != "unavailable":
                with _LOCK:
                    _CACHE[key] = got
                    while len(_CACHE) > _CACHE_MAX:
                        _CACHE.pop(next(iter(_CACHE)))
            return got, False
        finally:
            with _LOCK:
                _INFLIGHT.pop(key, None)
            ev.set()


# ---------------------------------------------------------------- Verilog reading

_MOD_RX = re.compile(r"\b(?:macro)?module\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z0-9_$]*)")
_INC_RX = re.compile(r"`include\s+\"([^\"]+)\"")
_HDL_EXT = (".v", ".sv", ".vh", ".svh", ".vlib")


def _strip(text: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.S)


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _modules_in(files: Sequence[Path], include_dirs: Sequence[str]) -> Tuple[Dict[str, Path], bool]:
    """Modules declared in *files* and the files they `include (three levels
    deep), and whether any name is made by a macro (then nothing is certain)."""
    mods: Dict[str, Path] = {}
    macro = False
    seen: set = set()
    todo = [(Path(f), 0) for f in files]
    while todo:
        f, depth = todo.pop(0)
        try:
            key = f.resolve()
        except OSError:
            key = f
        if key in seen:
            continue
        seen.add(key)
        text = _read(f)
        if text is None:
            continue
        t = _strip(text)
        for m in _MOD_RX.finditer(t):
            mods.setdefault(m.group(1), f)
        if re.search(r"\bmodule\s+`", t):
            macro = True
        if depth < 3:
            for inc in _INC_RX.findall(t):
                for base in [f.parent] + [Path(d) for d in include_dirs]:
                    cand = base / inc
                    if cand.is_file():
                        todo.append((cand, depth + 1))
                        break
    return mods, macro


def _header(text: str, module: str) -> Optional[str]:
    """A module's header: from ``module <name>`` to the ``;`` that ends it."""
    t = _strip(text)
    m = re.search(r"\bmodule\s+(?:(?:automatic|static)\s+)?" + re.escape(module) + r"\b", t)
    if not m:
        return None
    depth, i = 0, m.end()
    while i < len(t):
        ch = t[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return t[m.end():i]
        i += 1
    return t[m.end():]


def _rel(p: Path, design_dir: Path) -> str:
    try:
        return p.resolve().relative_to(design_dir.resolve()).as_posix()
    except Exception:
        return str(p)


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [x for x in str(v).split() if x]


def top_and_clock_problems(*, design_dir: Path, design_name: Optional[str], clock_port: Any,
                           verilog_files: Sequence[str], include_dirs: Sequence[str] = (),
                           config_name: str = "the config") -> List[Dict[str, Any]]:
    """The top-module and clock-port findings for a resolved config.

    Cautious on purpose: a name made by a macro, a design with no Verilog
    files, or a module this reader can't parse gives no finding rather than a
    wrong one."""
    out: List[Dict[str, Any]] = []
    files = [Path(f) for f in verilog_files if str(f).lower().endswith(_HDL_EXT)]
    if not design_name or not files:
        return out
    mods, macro = _modules_in(files, include_dirs)
    top = str(design_name)
    if top not in mods:
        if macro:
            return out
        others: Dict[str, Path] = {}
        try:
            from . import projectfiles

            proj = projectfiles._project_verilog(design_dir)
            others, _m = _modules_in([p for p in proj if p.resolve() not in {f.resolve() for f in files}], [])
        except Exception:
            pass
        names = sorted(mods)
        listed = ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
        near = [n for n in names if n.lower() == top.lower()]
        if near:
            msg = (f"The top module (DESIGN_NAME) is '{top}', but the sources declare '{near[0]}'. "
                   "Verilog names are case-sensitive.")
            fix = f"Set the top module to {near[0]} in {config_name}, or rename the module."
        elif top in others:
            where = _rel(others[top], design_dir)
            msg = (f"The top module (DESIGN_NAME) is '{top}', and {where} declares it, but {where} "
                   f"isn't one of the files the run builds (VERILOG_FILES in {config_name}).")
            fix = f"Tick {where} under Sources on Setup, or set the top module to one the run builds: {listed}."
        else:
            msg = (f"The top module (DESIGN_NAME) is '{top}', but no module of that name is declared in "
                   f"the {len(files)} file{'s' if len(files) != 1 else ''} the run builds.")
            fix = (f"Set the top module to one of: {listed} on Setup's project card, or rename the module in your Verilog."
                   if names else "Check that the design's Verilog files declare a module.")
        out.append({"code": "top", "message": msg, "fix": fix, "hard": False,
                    "module": top, "modules": names})
        return out
    clocks = [c.split("[", 1)[0].strip() for c in _as_list(clock_port)]
    clocks = [c for c in clocks if c]
    if not clocks:
        return out
    text = _read(mods[top]) or ""
    head = _header(text, top)
    if head is None:
        return out
    ports: List[str] = []
    try:
        from . import projectfiles

        ports = [p["name"] for p in projectfiles.module_ports(text, top)]
    except Exception:
        pass
    for clk in clocks:
        if re.search(r"(?<![A-Za-z0-9_$])" + re.escape(clk) + r"(?![A-Za-z0-9_$])", head) or clk in ports:
            continue
        guess = next((p for p in ports if re.search(r"cl(oc)?k", p, re.I)), None)
        plist = ", ".join(ports[:10]) + (" …" if len(ports) > 10 else "")
        msg = (f"The clock port (CLOCK_PORT) is '{clk}', but {top} has no port called '{clk}'"
               + (f". Its ports: {plist}." if ports else "."))
        fix = (f"Set the clock port to {guess} on Setup's project card." if guess else
               "Set the clock port to the name of the clock input, or clear it for a design without a clock.")
        out.append({"code": "clock", "message": msg, "fix": fix, "hard": False,
                    "port": clk, "ports": ports, "suggest": guess})
    return out


# ---------------------------------------------------------------- the check

def check(*, design_dir: str, config_files: Sequence[str], pdk: Optional[str], scl: Optional[str],
          pdk_ready: Optional[Dict[str, Any]], run_mode: str = "container",
          overrides: Optional[Dict[str, Any]] = None, extra_sources: Optional[Sequence[str]] = None,
          flow: Optional[Dict[str, Any]] = None, reuse_failures: bool = True,
          timeout: float = 120.0) -> Dict[str, Any]:
    """Check a run's config before it starts.

    Returns ``{ok, blocking, problems, note, checked, cached, where, flow,
    design_name, clock_port, seconds}``. ``problems`` are
    ``{code, message, fix, hard}``: ``hard`` ones ("config", "flow") block the
    run, the others ("top", "clock") ask first. ``checked`` says LibreLane's
    loader ran (``note`` says why not, for instance no PDK yet); a check that
    could not run never blocks."""
    t0 = time.time()
    d = Path(design_dir).resolve()
    cfg_name = Path(str(config_files[0])).name if config_files else "the config"
    res: Dict[str, Any] = {"ok": True, "blocking": False, "problems": [], "note": None,
                           "checked": False, "cached": False, "where": None,
                           "flow": (flow or {}).get("name"), "design_name": None, "clock_port": None}
    if flow and flow.get("error"):
        res["problems"].append({"code": "flow", "message": flow["error"],
                                "fix": "", "hard": True})
    probe: Optional[Dict[str, Any]] = None
    pdk_root = _load_root(pdk, pdk_ready, run_mode)
    if not config_files:
        res["note"] = "there is no config file"
    elif flow and flow.get("error"):
        pass
    elif not pdk_root:
        res["note"] = "the config is checked once the PDK is installed"
    else:
        args = {"configs": [str(Path(c).resolve()) for c in config_files], "design_dir": str(d),
                "pdk": pdk, "scl": scl, "pdk_root": pdk_root,
                "overrides": _override_strings(overrides, extra_sources, d),
                "flow": (flow or {}).get("cli")}
        probe, cached = _probe_cached(args, run_mode=run_mode, design_dir=d, pdk_root=pdk_root,
                                      reuse_failures=reuse_failures, timeout=timeout)
        res["cached"] = cached
        res["where"] = probe.get("where")
        kind = probe.get("kind")
        if probe.get("ok"):
            res["checked"] = True
            res["problems"] += key_warnings(probe.get("config_warnings") or [], cfg_name)
        elif kind in ("invalid", "config"):
            res["checked"] = True
            errs = [" ".join(str(e).split()) for e in (probe.get("errors") or []) if str(e).strip()]
            shown = "; ".join(errs[:4]) + (f" (and {len(errs) - 4} more)" if len(errs) > 4 else "")
            # The knowledge base's plain words for the first one (a wrong
            # type, a missing PDK, a missing file), after LibreLane's own.
            plain = ""
            try:
                from . import problems
                card = problems.explain_error(errs[0], {}) if errs else {}
                if card.get("title") and card.get("kb") in ("config-value", "config-required", "pdk-missing", "path-missing"):
                    plain = " " + str(card.get("plain") or "").replace(", so LibreLane stopped before the first step", "")
            except Exception:
                plain = ""
            res["problems"].append({
                "code": "config",
                "message": f"LibreLane can't load {cfg_name}: {shown or 'it gave no reason'}.{plain}".replace("..", "."),
                "fix": (f"Correct {'that setting' if len(errs) == 1 else 'these settings'} in {cfg_name}"
                        + (" or in the one-off settings next to Run" if overrides else "") + ", then run again."),
                "hard": True, "errors": errs})
        elif kind == "unavailable":
            res["note"] = f"LanEx couldn't run LibreLane's config check ({probe.get('why')})"
        else:
            errs = probe.get("errors") or []
            res["note"] = "LanEx couldn't run LibreLane's config check (" + "; ".join(str(e) for e in errs[:2]) + ")"
    # The top module and clock port, from what LibreLane resolved when it
    # could load the config, else from the config file as written.
    names: Dict[str, Any] = {}
    if probe and probe.get("ok"):
        names = {"design_name": probe.get("design_name"), "clock_port": probe.get("clock_port"),
                 "files": probe.get("verilog_files") or [], "inc": _as_list(probe.get("include_dirs"))}
    elif not any(p["hard"] for p in res["problems"]):
        try:
            from . import configedit

            info = configedit.read_config(d)
            data = info.get("data") or {}
            srcs = configedit.resolve_sources(d, data) if info.get("editable") else None
            files = list(srcs["files"]) if srcs and srcs.get("mode") == "list" else []
            ov = overrides or {}
            names = {"design_name": ov.get("DESIGN_NAME") or data.get("DESIGN_NAME"),
                     "clock_port": ov.get("CLOCK_PORT", data.get("CLOCK_PORT")),
                     "files": [str(d / s) if not os.path.isabs(s) else s
                               for s in _relativize(extra_sources, d)] or files, "inc": []}
        except Exception:
            names = {}
    if names:
        res["design_name"] = names.get("design_name")
        res["clock_port"] = names.get("clock_port")
        if isinstance(res["design_name"], str):
            try:
                res["problems"] += top_and_clock_problems(
                    design_dir=d, design_name=res["design_name"], clock_port=res["clock_port"],
                    verilog_files=names.get("files") or [], include_dirs=names.get("inc") or [],
                    config_name=cfg_name)
            except Exception:
                _log.debug("top/clock check failed", exc_info=True)
    res["blocking"] = any(p["hard"] for p in res["problems"])
    res["ok"] = not res["problems"]
    res["seconds"] = round(time.time() - t0, 2)
    return res


def key_warnings(lines: Sequence[str], cfg_name: str) -> List[Dict[str, Any]]:
    """LibreLane's "An unknown key 'X' was provided." and "Key 'X' provided is
    unused by the current flow." as findings that ask before the run (they
    don't block it: LibreLane itself runs on, and the setting does nothing).
    The words, and the nearest known name for a typo, are the knowledge
    base's (problems.unknown_key_note)."""
    from . import problems

    out: List[Dict[str, Any]] = []
    seen = set()
    for ln in lines or []:
        m = re.search(r"unknown key '([^']+)'", str(ln))
        u = re.search(r"Key '([^']+)' provided is unused by the current flow", str(ln))
        key = (m or u).group(1) if (m or u) else None
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(problems.unknown_key_note(key, cfg_name, unused=bool(u)))
    return out[:6]


def blockers_text(res: Dict[str, Any]) -> List[str]:
    """Each finding as one line: what is wrong, then what to do."""
    return [(p["message"] + (" " + p["fix"] if p.get("fix") else "")).strip()
            for p in (res or {}).get("problems") or []]
