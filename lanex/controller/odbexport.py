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
"""The analysis export behind the Layout tab's overlays.

One OpenROAD session per stage, on demand, on the database that stage's step
kept (``*.odb``), in the run's own LibreLane container image. It measures what
the flow knows but never draws: cell density and wire demand per tile, global
route congestion per GCell (all layers and per layer), the clock tree with the
arrival time at every clock pin, and the worst setup and hold paths with the
position of every pin on them. Everything is OpenROAD's own numbers
(``gui::dump_heatmap``, ``report_checks -format json``, the ODB); LanEx only
reshapes them for drawing.

The result is cached next to the run, ``<run>/.lanex/analysis/<step>/``:
the raw tool files, ``export.log``, ``analysis.json`` (what the page draws)
and ``manifest.json``, written last, which says what it was made from (the
database's size and time, the script version, the image). A folder without a
manifest, or whose manifest names another database, is not a result.

On spm a stage takes about a second: OpenROAD's own work is 0.2 to 0.3 s,
the rest is the container starting.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import safefs

_log = logging.getLogger(__name__)

SCRIPT_VERSION = "2"   # 2: the final stage is timed with SIGNOFF_SDC_FILE
_SCRIPT = Path(__file__).resolve().parent.parent / "data" / "openroad" / "lanex_export.tcl"

_JOBS: Dict[str, "_Job"] = {}
_JOBS_LOCK = threading.Lock()
# At most this many exports at once (each is one short single-threaded
# OpenROAD; a flow run may be using the machine too).
_SEM = threading.BoundedSemaphore(2)

# Heat maps the page offers, with their words.
MAPS = {
    "Routing": {"label": "Routing congestion", "unit": "%", "what": "of routing tracks used in each tile",
                "from": "global routing"},
    "Placement": {"label": "Cell density", "unit": "%", "what": "of each tile covered by cells",
                  "from": "global placement"},
    "RUDY": {"label": "Wire demand (estimate)", "unit": "%", "what": "estimated from each net's bounding box",
             "from": "global placement"},
}


# --------------------------------------------------------------------------- where things are
def _ordinal(name: str) -> int:
    head = name.split("-", 1)[0]
    return int(head) if head.isdigit() else -1


def _step_dirs(run_dir: Path) -> List[Path]:
    try:
        dirs = [d for d in run_dir.iterdir() if d.is_dir() and _ordinal(d.name) >= 0 and "-" in d.name]
    except OSError:
        return []
    return sorted(dirs, key=lambda d: _ordinal(d.name))


def _first(d: Path, pattern: str) -> Optional[Path]:
    try:
        hits = sorted(p for p in d.glob(pattern) if p.is_file())
    except OSError:
        return None
    return hits[0] if hits else None


def database_for(run_dir: Path, source: str) -> Dict[str, Any]:
    """The OpenROAD database to measure for a Layout source: the stage's own
    step, else the nearest earlier step that kept one (said in ``note``)."""
    run_dir = Path(run_dir)
    if source in ("", "final") or source.startswith("final:"):
        odb = _first(run_dir / "final" / "odb", "*.odb")
        if odb is None:
            # Final without a final/odb: the last step's database.
            for d in reversed(_step_dirs(run_dir)):
                odb = _first(d, "*.odb")
                if odb is not None:
                    return {"odb": odb, "step": d.name, "key": "final", "note": "The run has no final database; "
                            "measured on the last step that kept one (" + d.name + ")."}
            return {"odb": None, "step": None, "key": "final", "note": ""}
        return {"odb": odb, "step": "final", "key": "final", "note": ""}
    d = (run_dir / source).resolve()
    if d.parent != run_dir.resolve() or not d.is_dir():
        return {"odb": None, "step": None, "key": None, "note": ""}
    odb = _first(d, "*.odb")
    if odb is not None:
        return {"odb": odb, "step": d.name, "key": d.name, "note": ""}
    earlier = [s for s in _step_dirs(run_dir) if _ordinal(s.name) < _ordinal(d.name)]
    for s in reversed(earlier):
        odb = _first(s, "*.odb")
        if odb is not None:
            return {"odb": odb, "step": s.name, "key": s.name,
                    "note": "This step kept no OpenROAD database; measured on " + s.name + ", the nearest earlier one."}
    return {"odb": None, "step": None, "key": None, "note": ""}


def _cache_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:100].lstrip(".") or "_"


def cache_dir(run_dir: Path, key: str) -> Path:
    return Path(run_dir) / ".lanex" / "analysis" / _cache_name(key)


def _confined_cache(run_dir: Path, key: str) -> Path:
    """The cache folder as a real folder inside the run (made when missing).
    A run may hold links planted to send the export's writes, deletes and
    its read-write mount elsewhere: any link on the way is refused."""
    return safefs.confined_dir(run_dir, ".lanex", "analysis", _cache_name(key))


def _resolved(run_dir: Path) -> Dict[str, Any]:
    for name in ("resolved.json", "config.json"):
        f = run_dir / name
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8", errors="replace")) or {}
            except Exception:
                continue
    return {}


def _remap(p: str, cfg: Dict[str, Any]) -> Optional[str]:
    """A PDK file named by an old absolute path (the run was made on another
    machine or folder): the same file under a PDK root this machine has."""
    if not p:
        return None
    if Path(p).is_file():
        return p
    pdk = cfg.get("PDK")
    if not pdk or ("/" + str(pdk) + "/") not in p:
        return None
    tail = p.split("/" + str(pdk) + "/", 1)[1]
    try:
        from . import pdk as pdk_mod
        roots = list(pdk_mod._candidate_pdk_roots())
    except Exception:
        roots = []
    for r in roots:
        c = Path(r) / str(pdk) / tail
        if c.is_file():
            return str(c)
    return None


def _libs(cfg: Dict[str, Any]) -> List[str]:
    """The Liberty files of the run's default corner (the corner STA signs off on)."""
    lib = cfg.get("LIB")
    corner = cfg.get("DEFAULT_CORNER") or ""
    picked: List[str] = []
    if isinstance(lib, dict):
        for key, vals in lib.items():
            if corner and fnmatch.fnmatch(corner, key):
                picked = list(vals) if isinstance(vals, (list, tuple)) else [vals]
                break
        if not picked and lib:
            v = next(iter(lib.values()))
            picked = list(v) if isinstance(v, (list, tuple)) else [v]
    elif isinstance(lib, (list, tuple)):
        picked = list(lib)
    elif isinstance(lib, str):
        picked = lib.split()
    out = []
    for p in picked:
        r = _remap(str(p), cfg)
        if r:
            out.append(r)
    return out


def _signoff_sdc(run_dir: Path, cfg: Dict[str, Any]) -> Optional[Path]:
    """The SIGNOFF_SDC_FILE sign-off STA reads (steps/openroad.py, STAPostPNR),
    found again when the run was made in another folder: the same path under
    this project instead of the DESIGN_DIR the run recorded."""
    p = cfg.get("SIGNOFF_SDC_FILE")
    if not isinstance(p, str) or not p:
        return None
    # This project's own copy first (a run copied in from another folder),
    # then the path as the run recorded it.
    dd = str(cfg.get("DESIGN_DIR") or "").rstrip("/")
    if dd and p.startswith(dd + "/"):
        cand = Path(run_dir).parent.parent / p[len(dd) + 1:]
        if cand.is_file():
            return cand
    return Path(p) if Path(p).is_file() else None


def _config_env(cfg: Dict[str, Any]) -> Dict[str, str]:
    """The config as LibreLane hands it to its Tcl (``$::env(CLOCK_PERIOD)``…):
    a sign-off SDC reads those. Scalars and flat lists only."""
    out: Dict[str, str] = {}

    def one(v: Any) -> Optional[str]:
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float, str)):
            return str(v)
        return None

    for k, v in cfg.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(k)) or v is None:
            continue
        if isinstance(v, (list, tuple)):
            parts = [one(x) for x in v]
            if all(x is not None for x in parts):
                out[k] = " ".join(parts)  # type: ignore[arg-type]
            continue
        s = one(v)
        if s is not None and "\n" not in s and "\x00" not in s:
            out[k] = s
    return out


def _inputs(run_dir: Path, db: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _resolved(run_dir)
    step_dir = db["odb"].parent
    final_db = db["key"] == "final" and db["step"] == "final"
    # The final layout is timed as sign-off times it: with SIGNOFF_SDC_FILE
    # (spm: 0.1 ns uncertainty), not the place-and-route SDC final/sdc holds
    # (0.25 ns). Earlier stages keep the constraints their step used.
    signoff = _signoff_sdc(run_dir, cfg) if final_db else None
    sdc = signoff or _first(step_dir, "*.sdc") or _first(run_dir / "final" / "sdc", "*.sdc")
    spef = None
    if final_db and (run_dir / "final" / "spef").is_dir():
        corner = cfg.get("DEFAULT_CORNER") or ""
        token = corner.split("_")[0] if corner else "nom"
        spef = _first(run_dir / "final" / "spef" / token, "*.spef") or _first(run_dir / "final" / "spef" / "nom", "*.spef")
    # Clocks are propagated only once a clock tree exists (as LibreLane's STA does).
    cts = any(_ordinal(d.name) <= _ordinal(step_dir.name) and "openroad-cts" in d.name for d in _step_dirs(run_dir)) \
        if step_dir.name != "odb" else True
    corners = cfg.get("STA_CORNERS")
    return {"libs": _libs(cfg), "sdc": sdc, "spef": spef, "propagate": cts, "cfg": cfg,
            "env": _config_env(cfg) if signoff else {},
            "timing": {"corner": cfg.get("DEFAULT_CORNER") or None, "signoff_sdc": bool(signoff),
                       "sdc": sdc.name if sdc else None, "spef": bool(spef),
                       "corners": len(corners) if isinstance(corners, list) else None}}


# --------------------------------------------------------------------------- state
def _manifest_key(odb: Path) -> Dict[str, Any]:
    st = odb.stat()
    return {"odb": odb.name, "size": st.st_size, "mtime": int(st.st_mtime), "script": SCRIPT_VERSION}


def _read_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(p: Path, data: Any) -> None:
    safefs.write_text(p.parent, p.name, json.dumps(data, separators=(",", ":")))


def _jkey(run_dir: Path, key: str) -> str:
    return str(Path(run_dir).resolve()) + "::" + key


def status(run_dir: Path, source: str) -> Dict[str, Any]:
    """Where the export of one source stands: ``none`` (never measured),
    ``running``, ``ready``, ``failed``, ``stale`` (the database changed) or
    ``nodb`` (no database at or before this step)."""
    run_dir = Path(run_dir)
    db = database_for(run_dir, source)
    if db["odb"] is None:
        return {"state": "nodb", "note": "No step at or before this one kept an OpenROAD database, "
                "so there is nothing to measure. Overlays start at the floorplan."}
    cdir = cache_dir(run_dir, db["key"])
    base = {"step": db["step"], "note": db["note"], "cache": str(cdir), "odb": db["odb"].relative_to(run_dir).as_posix()}
    with _JOBS_LOCK:
        job = _JOBS.get(_jkey(run_dir, db["key"]))
    if job is not None and job.alive():
        return dict(base, state="running", elapsed=round(time.time() - job.t0, 1), phase=job.phase)
    man = _read_json(cdir / "manifest.json")
    if man and man.get("key") == _manifest_key(db["odb"]):
        return dict(base, state="ready", summary=man.get("summary") or {}, seconds=man.get("seconds"),
                    bytes=man.get("bytes"), when=man.get("when"))
    if job is not None and job.error:
        return dict(base, state="failed", error=job.error, need=job.need, log=str(cdir / "export.log"),
                    tail=job.tail)
    fail = _read_json(cdir / "failed.json")
    if fail and fail.get("key") == _manifest_key(db["odb"]):
        return dict(base, state="failed", error=fail.get("error"), need=fail.get("need"),
                    log=str(cdir / "export.log"), tail=fail.get("tail") or [])
    return dict(base, state="stale" if man else "none")


def data(run_dir: Path, source: str) -> Optional[Dict[str, Any]]:
    """What the page draws (``analysis.json``), when the export is ready."""
    st = status(run_dir, source)
    if st.get("state") != "ready":
        return None
    return _read_json(Path(st["cache"]) / "analysis.json")


def start(run_dir: Path, source: str, *, force: bool = False) -> Dict[str, Any]:
    """Start measuring *source* (or keep the one running). Returns status()."""
    run_dir = Path(run_dir)
    st = status(run_dir, source)
    if st["state"] in ("running", "nodb") or (st["state"] == "ready" and not force):
        return st
    db = database_for(run_dir, source)
    key = _jkey(run_dir, db["key"])
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job is not None and job.alive():
            return status(run_dir, source)
        job = _Job(run_dir, db)
        _JOBS[key] = job
    job.start()
    return status(run_dir, source)


def cancel(run_dir: Path, source: str) -> bool:
    db = database_for(Path(run_dir), source)
    if db["key"] is None:
        return False
    with _JOBS_LOCK:
        job = _JOBS.get(_jkey(Path(run_dir), db["key"]))
    if job is None or not job.alive():
        return False
    job.cancel()
    return True


# --------------------------------------------------------------------------- the run
def _docker_rootless(engine: str) -> bool:
    try:
        r = subprocess.run([engine, "info", "--format", "{{json .SecurityOptions}}"], capture_output=True,
                           text=True, timeout=15)
        return "rootless" in (r.stdout or "")
    except Exception:
        return False


def build_argv(engine: str, image: str, *, run_dir: Path, cdir: Path, script: Path, odb: Path,
               libs: List[str], sdc: Optional[Path], spef: Optional[Path], propagate: bool,
               name: str, timeout_s: int, mounts: List[str], user: Optional[str],
               layers: Optional[Dict[str, str]] = None, config_env: Optional[Dict[str, str]] = None) -> List[str]:
    """``<engine> run`` for one export: the run folder read-only, its cache
    folder writable, the PDK read-only, no network, OpenROAD's GUI offscreen."""
    argv = [engine, "run", "--rm", "--name", name, "--network", "none"]
    if user:
        argv += ["--user", user]
    argv += ["-v", f"{run_dir}:{run_dir}:ro", "-v", f"{cdir}:{cdir}"]
    for m in mounts:
        argv += ["-v", f"{m}:{m}:ro"]
    # The config first (a sign-off SDC reads $::env(CLOCK_PERIOD)…), then
    # the export's own inputs, which win on a clash.
    env: Dict[str, str] = dict(config_env or {})
    env.update({"QT_QPA_PLATFORM": "offscreen", "HOME": str(cdir), "ODB": str(odb), "OUT": str(cdir),
           "LIBS": " ".join(libs), "SDCF": str(sdc or ""), "SPEF": str(spef or ""),
           "PROPAGATE": "1" if propagate else "0"})
    for k, v in (layers or {}).items():
        env[k] = str(v or "")
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    argv += ["-w", str(cdir), image, "timeout", str(int(timeout_s)),
             "openroad", "-no_init", "-no_splash", "-gui", "-no_settings", str(cdir / "lanex_export.tcl")]
    return argv


def _mount_roots(paths: List[str], run_dir: Path, cfg: Dict[str, Any]) -> List[str]:
    """Folders to mount read-only so the Liberty and SDC paths resolve inside."""
    roots: List[str] = []
    pdk_root = cfg.get("PDK_ROOT")
    for p in paths:
        if not p:
            continue
        pp = Path(p)
        if str(pp).startswith(str(run_dir) + "/"):
            continue
        r = None
        if pdk_root and str(pp).startswith(str(pdk_root).rstrip("/") + "/") and Path(pdk_root).is_dir():
            r = str(pdk_root).rstrip("/")
        else:
            pdk = cfg.get("PDK")
            if pdk and ("/" + str(pdk) + "/") in str(pp):
                r = str(pp).split("/" + str(pdk) + "/", 1)[0]
            else:
                r = str(pp.parent)
        if r and r not in roots:
            roots.append(r)
    # Mounting a parent covers its children.
    roots.sort(key=len)
    out: List[str] = []
    for r in roots:
        if not any(r == o or r.startswith(o.rstrip("/") + "/") for o in out):
            out.append(r)
    return out


class _Job:
    def __init__(self, run_dir: Path, db: Dict[str, Any]):
        self.run_dir = Path(run_dir).resolve()
        self.db = db
        self.cdir = cache_dir(self.run_dir, db["key"])
        self.t0 = time.time()
        self.phase = "starting"
        self.error: Optional[str] = None
        self.need: Optional[str] = None
        self.tail: List[str] = []
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancel = False
        self._engine = "docker"
        self.name = "lanex-export-%d-%d" % (os.getpid(), int(time.time() * 1000) % 10**9)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lanex-odbexport", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel = True
        try:
            subprocess.run([self._engine, "kill", self.name], capture_output=True, timeout=15)
        except Exception:
            pass
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def _fail(self, msg: str, need: Optional[str] = None) -> None:
        self.error, self.need = msg, need
        try:
            self.cdir = _confined_cache(self.run_dir, self.db["key"])
            _write_json(self.cdir / "failed.json", {"key": _manifest_key(self.db["odb"]), "error": msg,
                                                    "need": need, "tail": self.tail, "when": int(time.time())})
        except Exception:
            pass

    def _run(self) -> None:
        with _SEM:
            try:
                self._go()
            except Exception as ex:  # noqa: BLE001 - reported to the page, logged here
                _log.exception("analysis export failed")
                self._fail("LanEx could not run the export: " + str(ex))

    def _go(self) -> None:
        from . import tools as tools_mod
        from .container_run import image_ref

        odb: Path = self.db["odb"]
        self.phase = "checking the container"
        eng = tools_mod.resolve_engine()
        if not eng.get("ready"):
            self._fail("Overlays are measured with OpenROAD in the LibreLane container, and no container "
                       "engine is ready. Set up the container in Tools, then try again.", need="engine")
            return
        engine = eng.get("engine") or "docker"
        self._engine = engine
        image = image_ref()
        try:
            from .container_tools import _image_present
            present = _image_present(engine, image, sg_wrap=bool(eng.get("sg_wrap")))
        except Exception:
            present = True
        if not present:
            self._fail("The LibreLane image (" + image + ") isn't on this computer yet. Pull it in Tools, "
                       "then try again.", need="image")
            return
        inp = _inputs(self.run_dir, self.db)
        self.cdir = _confined_cache(self.run_dir, self.db["key"])
        for old in self.cdir.glob("heat-*.csv"):
            try:
                old.unlink()
            except OSError:
                pass
        for old in ("paths-setup.json", "paths-hold.json", "clocktree.tsv", "manifest.json", "failed.json"):
            try:
                (self.cdir / old).unlink()
            except OSError:
                pass
        safefs.write_bytes(self.cdir, "lanex_export.tcl", _SCRIPT.read_bytes())
        size_mb = odb.stat().st_size / 1e6
        timeout_s = int(min(900, 90 + 30 * size_mb / 100))
        user = None
        if sys.platform.startswith("linux") and engine == "docker" and hasattr(os, "getuid") \
                and not _docker_rootless(engine):
            user = "%d:%d" % (os.getuid(), os.getgid())
        mounts = _mount_roots(inp["libs"] + [str(inp["sdc"] or ""), str(inp["spef"] or "")], self.run_dir, inp["cfg"])
        argv = build_argv(engine, image, run_dir=self.run_dir, cdir=self.cdir, script=_SCRIPT, odb=odb,
                          libs=inp["libs"], sdc=inp["sdc"], spef=inp["spef"], propagate=inp["propagate"],
                          name=self.name, timeout_s=timeout_s, mounts=mounts, user=user,
                          layers={"RT_MIN": inp["cfg"].get("RT_MIN_LAYER"), "RT_MAX": inp["cfg"].get("RT_MAX_LAYER"),
                                  "RT_CLOCK_MIN": inp["cfg"].get("RT_CLOCK_MIN_LAYER")},
                          config_env=inp["env"])
        if eng.get("sg_wrap"):
            argv = tools_mod.sg_wrap_argv(argv)
        env = os.environ.copy()
        env.update(eng.get("env") or {})
        logp = self.cdir / "export.log"
        self.phase = "loading the database"
        t0 = time.time()
        with safefs.open_new(logp) as logf:
            logf.write("$ " + " ".join(argv) + "\n")
            logf.flush()
            self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                          env=env, bufsize=1, errors="replace")
            watchdog = threading.Timer(timeout_s + 30, self.cancel)
            watchdog.daemon = True
            watchdog.start()
            lines: List[str] = []
            try:
                for line in self._proc.stdout:  # type: ignore[union-attr]
                    logf.write(line)
                    s = line.rstrip()
                    if "GUI-009" in s:          # Qt's "no display" chatter
                        continue
                    lines.append(s)
                    if len(lines) > 400:
                        del lines[:200]
                    if s.startswith("LANEX: step "):
                        self.phase = {"load": "loading the database", "heat": "measuring density and congestion",
                                      "paths": "finding the worst paths", "clock": "tracing the clock tree"}.get(
                                          s.split()[-1], s[12:])
                code = self._proc.wait()
            finally:
                watchdog.cancel()
        self.tail = [ln for ln in lines if ln.strip()][-20:]
        seconds = round(time.time() - t0, 2)
        if self._cancel:
            self._fail("Stopped." if seconds < timeout_s else
                       "OpenROAD took longer than %d s and was stopped." % timeout_s, need="cancelled")
            return
        failed = next((ln[len("LANEX: FAILED: "):] for ln in lines if ln.startswith("LANEX: FAILED: ")), None)
        if code != 0 or failed:
            first = failed or next((ln for ln in lines if re.search(r"\[ERROR|Error|error:", ln)), None) or \
                ("exit code %d" % code)
            self._fail("OpenROAD couldn't measure this stage: " + first.strip())
            return
        self.phase = "preparing the overlays"
        out = convert(self.cdir, lines)
        # Qt and fontconfig leave caches in HOME (the cache folder): not results.
        try:
            safefs.remove_tree(self.cdir, ".cache")
        except OSError:
            pass
        out["seconds"] = seconds
        out["step"] = self.db["step"]
        out["note"] = self.db["note"]
        out["timing"] = inp["timing"]
        _write_json(self.cdir / "analysis.json", out)
        size = sum(f.stat().st_size for f in self.cdir.iterdir() if f.is_file())
        _write_json(self.cdir / "manifest.json", {
            "key": _manifest_key(odb), "image": image, "seconds": seconds, "bytes": size,
            "when": int(time.time()), "summary": out.get("summary", {})})
        _log.info("analysis export %s/%s: %.2f s, %d bytes", self.run_dir.name, self.db["key"], seconds, size)


# --------------------------------------------------------------------------- shaping the results
def read_heat(path: Path) -> Optional[Dict[str, Any]]:
    """``gui::dump_heatmap`` CSV (``x0,y0,x1,y1,value (unit)`` per tile, µm)
    as a grid: tile edges ``xs`` and ``ys`` and the values row by row from the
    bottom (None where OpenROAD wrote no tile, outside the core)."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.reader(f))
    except OSError:
        return None
    if len(rows) < 2:
        return None
    head = [h.strip() for h in rows[0]]
    vcol = next((i for i, h in enumerate(head) if h.lower().startswith("value")), 4)
    unit = ""
    m = re.search(r"\(([^)]*)\)", head[vcol]) if vcol < len(head) else None
    if m:
        unit = m.group(1).strip()
    tiles: List[Tuple[float, float, float, float, float]] = []
    for r in rows[1:]:
        try:
            tiles.append((float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[vcol])))
        except (ValueError, IndexError):
            continue
    if not tiles:
        return None

    def edges(lo: List[float], hi: List[float]) -> List[float]:
        e = sorted(set(round(v, 4) for v in lo) | set(round(v, 4) for v in hi))
        return e

    xs = edges([t[0] for t in tiles], [t[2] for t in tiles])
    ys = edges([t[1] for t in tiles], [t[3] for t in tiles])
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: i for i, v in enumerate(ys)}
    nx, ny = len(xs) - 1, len(ys) - 1
    vals: List[Optional[float]] = [None] * (nx * ny)
    for x0, y0, x1, y1, v in tiles:
        i0, i1 = xi.get(round(x0, 4)), xi.get(round(x1, 4))
        j0, j1 = yi.get(round(y0, 4)), yi.get(round(y1, 4))
        if None in (i0, i1, j0, j1):
            continue
        for j in range(j0, j1):
            for i in range(i0, i1):
                vals[j * nx + i] = round(v, 2)
    present = [v for v in vals if v is not None]
    return {"xs": xs, "ys": ys, "nx": nx, "ny": ny, "v": vals, "unit": unit,
            "min": min(present) if present else 0, "max": max(present) if present else 0,
            "over": sum(1 for v in present if v > 100), "tiles": len(present)}


def _num(v: Any, scale: float) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f * scale


def read_paths(path: Path) -> List[Dict[str, Any]]:
    """``report_checks -format json`` (SI units: seconds, metres) as paths in
    ns and µm, each with its points in order: pin, net, instance, cell, x, y,
    arrival, slew."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # OpenSTA writes bare inf/nan for unconstrained values.
    raw = re.sub(r"(?<=[:\[,\s])-?(inf|nan)(?=[,\]\s}])", "null", raw)
    try:
        doc = json.loads(raw)
    except Exception:
        return []
    out = []
    for c in doc.get("checks") or []:
        pts = []
        for p in c.get("source_path") or []:
            pts.append({"pin": p.get("pin"), "net": p.get("net"), "inst": p.get("instance"), "cell": p.get("cell"),
                        "x": _r(_num(p.get("x"), 1e6), 3), "y": _r(_num(p.get("y"), 1e6), 3),
                        "arrival": _r(_num(p.get("arrival"), 1e9), 4), "slew": _r(_num(p.get("slew"), 1e9), 4)})
        out.append({"type": c.get("type"), "group": c.get("path_group"), "startpoint": c.get("startpoint"),
                    "endpoint": c.get("endpoint"), "slack": _r(_num(c.get("slack"), 1e9), 4),
                    "arrival": _r(_num(c.get("data_arrival_time"), 1e9), 4),
                    "required": _r(_num(c.get("required_time"), 1e9), 4),
                    "clock": c.get("source_clock") or c.get("target_clock"), "points": pts})
    out.sort(key=lambda p: (p["slack"] if p["slack"] is not None else 1e9))
    return out


def _r(v: Optional[float], n: int) -> Optional[float]:
    return None if v is None else round(v, n)


def read_clock(path: Path) -> Dict[str, Any]:
    """The clock tree from ``clocktree.tsv``: per clock net its driver and
    sinks with boxes (µm) and arrival (ns); flip-flops are the sinks that drive
    no clock net themselves; buffers are the instances that do."""
    nets: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rd = csv.DictReader(f, delimiter="\t")
            for r in rd:
                n = nets.setdefault(r["net"], {"name": r["net"], "driver": None, "port": None, "sinks": []})
                try:
                    box = [round(float(r["x0"]), 3), round(float(r["y0"]), 3), round(float(r["x1"]), 3), round(float(r["y1"]), 3)]
                except (ValueError, KeyError):
                    continue
                pin = {"pin": r.get("pin"), "inst": r.get("inst") or None, "cell": r.get("cell"), "box": box,
                       "arrival": _r(_num(r.get("arrival_ns"), 1.0), 4), "slew": _r(_num(r.get("slew_ns"), 1.0), 4)}
                role = r.get("role")
                if role == "port":
                    n["port"] = pin
                elif role == "driver":
                    n["driver"] = pin
                else:
                    n["sinks"].append(pin)
    except OSError:
        return {"nets": [], "summary": {}}
    drivers = {n["driver"]["inst"] for n in nets.values() if n.get("driver") and n["driver"].get("inst")}
    ffs = [s for n in nets.values() for s in n["sinks"] if s.get("inst") not in drivers]
    arr = [s["arrival"] for s in ffs if s.get("arrival") is not None]
    summary = {"nets": len(nets), "buffers": len(drivers), "sinks": len(ffs),
               "arrival_min": min(arr) if arr else None, "arrival_max": max(arr) if arr else None,
               "skew": round(max(arr) - min(arr), 4) if arr else None,
               "roots": sorted({n["port"]["pin"] for n in nets.values() if n.get("port")})}
    for n in nets.values():
        for s in n["sinks"]:
            s["ff"] = s.get("inst") not in drivers
    return {"nets": sorted(nets.values(), key=lambda n: n["name"]), "summary": summary}


def convert(cdir: Path, lines: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every raw file of an export folder into what the page draws."""
    heat: Dict[str, Any] = {}
    layers: Dict[str, Any] = {}
    for f in sorted(cdir.glob("heat-*.csv")):
        name = f.stem[len("heat-"):]
        g = read_heat(f)
        if g is None:
            continue
        if "@" in name:
            base, layer = name.split("@", 1)
            if base == "Routing" and g["tiles"]:
                layers[layer] = g
            continue
        heat[name] = g
    if "Routing" in heat:
        heat["Routing"]["layers"] = layers
    unavailable = {}
    for ln in lines or []:
        m = re.match(r"LANEX: unavailable (\w+): (.*)", ln)
        if m:
            unavailable[m.group(1)] = m.group(2)
    setup = read_paths(cdir / "paths-setup.json")
    hold = read_paths(cdir / "paths-hold.json")
    clock = read_clock(cdir / "clocktree.tsv")
    summary = {
        "maps": sorted(heat),
        "routing_max": heat.get("Routing", {}).get("max"),
        "routing_over": heat.get("Routing", {}).get("over"),
        "density_max": heat.get("Placement", {}).get("max"),
        "setup_paths": len(setup), "hold_paths": len(hold),
        "worst_setup": setup[0]["slack"] if setup else None,
        "worst_hold": hold[0]["slack"] if hold else None,
        "clock_nets": clock["summary"].get("nets", 0),
        "skew": clock["summary"].get("skew"),
    }
    return {"version": SCRIPT_VERSION, "heat": heat, "paths": {"setup": setup, "hold": hold},
            "clock": clock, "unavailable": unavailable, "summary": summary}
