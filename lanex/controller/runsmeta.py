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
"""What the Runs page keeps about runs beyond LibreLane's own files.

Per run (sidecars in the run folder, like ``.gui-pin`` and ``.gui-note.txt``):

``.gui-tags.json``   the run's tags, a JSON list of strings
``.gui-archived``    present while the run is archived: hidden from Runs,
                     Compare and the run picker by default, never deleted.
                     The folder never moves (a fix run finds the run it was
                     resumed from at ``runs/<tag>``).

Per project (``<design>/.gui-runs.json``, written atomically)::

    {"version": 1, "baseline": "<tag>" | null, "default_view": "<id>" | null,
     "views": [{id, name, filters, sort, columns, group}]}

The one baseline of the project lives here: the Runs star, the run picker's
"Compared with" and Compare all set it (audit 4.8).

Deleting runs (``bulk("delete")``) moves their folders into
``runs/.gui-trash/<batch>/`` (a rename inside the same folder tree, instant
even for a 90 MB run), so the page can offer Undo while its toast is up. The
page then asks for ``purge``; batches older than :data:`TRASH_TTL_S` are
purged on the next listing or bulk call, and at start-up. ``list_runs`` skips
dot folders, so the trash never shows as a run.

Also here: values of any metric or setting for a set of runs (the Runs
table's free columns), what each run changed against the baseline (plain
settings, with paths left out), the metric and setting names a project's runs
have (the Columns picker), and the export of a set of runs as CSV or JSON.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import platform_env

TAGS_FILE = ".gui-tags.json"
ARCHIVE_MARKER = ".gui-archived"
META_FILE = ".gui-runs.json"
TRASH_DIR = ".gui-trash"
TRASH_TTL_S = 600
TAG_MAX = 32
TAGS_PER_RUN = 24
VIEWS_MAX = 40
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-/]{0,31}$")
_BATCH_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


# ---------------------------------------------------------------- tags
def clean_tag(t: Any) -> Optional[str]:
    """A tag as stored, or None when it is not a valid tag. Letters, digits,
    space and ``_ . - /``, 1 to 32 characters, starting with a letter or a
    digit; surrounding space trimmed, inner runs of space folded."""
    if not isinstance(t, str):
        return None
    s = re.sub(r"\s+", " ", t).strip()
    return s if _TAG_RE.match(s) else None


def clean_tags(tags: Iterable[Any]) -> List[str]:
    """Valid tags, de-duplicated case-blind (the first spelling wins)."""
    out: List[str] = []
    seen = set()
    for t in tags or []:
        c = clean_tag(t)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out[:TAGS_PER_RUN]


def read_tags(run_dir: str | Path) -> List[str]:
    p = Path(run_dir) / TAGS_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return clean_tags(doc) if isinstance(doc, list) else []


def write_tags(run_dir: str | Path, tags: Iterable[Any]) -> List[str]:
    rd = Path(run_dir)
    clean = clean_tags(tags)
    p = rd / TAGS_FILE
    if clean:
        platform_env.atomic_write_text(p, json.dumps(clean) + "\n")
    elif p.is_file():
        p.unlink()
    _touch(rd)
    return clean


def is_archived(run_dir: str | Path) -> bool:
    return (Path(run_dir) / ARCHIVE_MARKER).is_file()


def set_archived(run_dir: str | Path, on: bool) -> bool:
    rd = Path(run_dir)
    m = rd / ARCHIVE_MARKER
    if on:
        m.touch()
    elif m.is_file():
        m.unlink()
    _touch(rd)
    return bool(on)


def _touch(rd: Path) -> None:
    """Forget the cached answers about *rd* (history's per-run cache)."""
    try:
        from . import history
        history.touch_run(rd)
    except Exception:
        pass


# ---------------------------------------------------------------- project file
_META_LOCK = threading.Lock()


def _meta_path(design_dir: str | Path) -> Path:
    return Path(design_dir) / META_FILE


def _empty_meta() -> Dict[str, Any]:
    return {"version": 1, "baseline": None, "default_view": None, "views": []}


def read_meta(design_dir: str | Path) -> Dict[str, Any]:
    """The project's Runs file. A damaged file is kept as ``.bak`` (once) and
    read as empty, with ``damaged`` set so the page can say so."""
    p = _meta_path(design_dir)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return _empty_meta()
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError("not an object")
    except ValueError:
        bak = p.with_name(META_FILE + ".bak")
        try:
            if not bak.exists():
                bak.write_text(raw, encoding="utf-8")
        except OSError:
            pass
        out = _empty_meta()
        out["damaged"] = str(bak)
        return out
    out = _empty_meta()
    b = doc.get("baseline")
    out["baseline"] = b if isinstance(b, str) and b and "/" not in b else None
    dv = doc.get("default_view")
    out["default_view"] = dv if isinstance(dv, str) and dv else None
    out["views"] = [v for v in (clean_view(x) for x in doc.get("views") or []) if v]
    return out


def clean_view(v: Any) -> Optional[Dict[str, Any]]:
    """A saved view as stored: an id, a name and the table's settings. Only
    plain JSON of a bounded size is kept (it is the page's own state)."""
    if not isinstance(v, dict):
        return None
    vid = v.get("id")
    name = v.get("name")
    if not isinstance(vid, str) or not re.match(r"^[A-Za-z0-9_-]{1,40}$", vid):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    out: Dict[str, Any] = {"id": vid, "name": name.strip()[:60]}
    for k in ("filters", "sort", "columns", "group"):
        x = v.get(k)
        if x is None:
            continue
        try:
            s = json.dumps(x)
        except (TypeError, ValueError):
            continue
        if len(s) <= 8000:
            out[k] = json.loads(s)
    return out


def write_meta(design_dir: str | Path, **patch: Any) -> Dict[str, Any]:
    """Change the project's Runs file: ``baseline`` (a tag or None),
    ``views`` (the whole list), ``default_view``. Unknown keys are ignored."""
    with _META_LOCK:
        doc = read_meta(design_dir)
        doc.pop("damaged", None)
        if "baseline" in patch:
            b = patch["baseline"]
            doc["baseline"] = b if isinstance(b, str) and b and "/" not in b and b not in (".", "..") else None
        if "views" in patch:
            views = [v for v in (clean_view(x) for x in patch["views"] or []) if v]
            seen = set()
            doc["views"] = [v for v in views if not (v["id"] in seen or seen.add(v["id"]))][:VIEWS_MAX]
        if "default_view" in patch:
            dv = patch["default_view"]
            doc["default_view"] = dv if isinstance(dv, str) and dv else None
        platform_env.atomic_write_text(_meta_path(design_dir), json.dumps(doc, indent=1) + "\n")
        return doc


# ---------------------------------------------------------------- run folders
def looks_like_run(d: Path) -> bool:
    try:
        from .history import _looks_like_run
        return _looks_like_run(d)
    except OSError:
        return False


def check_run_dir(run_dir: Any, *, known: Optional[Iterable[str]] = None) -> Path:
    """The run folder, resolved, when it is one LanEx may change: a folder
    directly under ``<design>/runs/`` that looks like a LibreLane run, of a
    design LanEx knows (``known``: resolved design folders; None = any).
    Raises ValueError with the reason otherwise."""
    if not isinstance(run_dir, str) or not run_dir:
        raise ValueError("no run folder given")
    p = Path(run_dir).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("that run is gone")
    if p.parent.name != "runs" or p.name.startswith("."):
        raise ValueError("not a run folder (it must sit directly in a design's runs/ folder)")
    if known is not None and str(p.parent.parent) not in set(known):
        raise ValueError("that run belongs to a design LanEx hasn't opened")
    if not looks_like_run(p):
        raise ValueError("that folder is not a LibreLane run")
    return p


def folder_bytes(p: Path, cap_files: int = 200000) -> int:
    """The size of a folder on disk (apparent sizes), without following links."""
    total = 0
    n = 0
    stack = [str(p)]
    while stack and n < cap_files:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _new_batch() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def _trash_root(design_dir: Path) -> Path:
    return design_dir / "runs" / TRASH_DIR


def purge_stale(design_dir: str | Path, max_age_s: float = TRASH_TTL_S, *, background: bool = True) -> None:
    """Free the space of deleted runs whose Undo window has passed."""
    root = _trash_root(Path(design_dir))
    if not root.is_dir():
        return

    def work() -> None:
        now = time.time()
        try:
            batches = list(root.iterdir())
        except OSError:
            return
        for b in batches:
            try:
                age = now - b.stat().st_mtime
            except OSError:
                continue
            if age >= max_age_s:
                shutil.rmtree(b, ignore_errors=True)
        try:
            if not any(root.iterdir()):
                root.rmdir()
        except OSError:
            pass

    if background:
        threading.Thread(target=work, name="lanex-trash", daemon=True).start()
    else:
        work()


def bulk(action: str, run_dirs: List[Any], *, tags: Optional[List[Any]] = None,
         batch: Optional[str] = None, design_dirs: Optional[List[Any]] = None,
         force: bool = False, live: Iterable[str] = (), known: Optional[Iterable[str]] = None,
         children_of: Optional[Any] = None) -> Tuple[Dict[str, Any], int]:
    """One action over many runs. Returns ``(answer, status)``.

    ``archive`` / ``restore`` / ``tag`` / ``untag`` (``tags``) change markers;
    ``delete`` moves the folders into the trash (one ``batch`` for the whole
    set) and answers 409 ``children`` once when other runs were resumed from
    any of them, unless ``force``; ``undo`` puts a batch back; ``purge`` frees
    it. ``live``: run folders being written now (refused). A run that fails
    does not stop the others; each failure is listed with its reason."""
    live_set = {str(Path(x).resolve()) for x in live if x}
    known_list = None if known is None else [str(Path(k).resolve()) for k in known]
    done: List[str] = []
    failed: List[Dict[str, str]] = []

    if action in ("undo", "purge"):
        if not isinstance(batch, str) or not _BATCH_RE.match(batch):
            return {"ok": False, "error": "no such delete to undo"}, 400
        designs = []
        for d in design_dirs or []:
            try:
                p = Path(str(d)).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if known_list is None or str(p) in known_list:
                designs.append(p)
        restored: List[str] = []
        missing = True
        for d in designs:
            src = _trash_root(d) / batch
            if not src.is_dir():
                continue
            missing = False
            if action == "purge":
                shutil.rmtree(src, ignore_errors=True)
                continue
            for r in sorted(src.iterdir()):
                if r.name.startswith("."):
                    continue
                dest = d / "runs" / r.name
                if dest.exists():
                    failed.append({"run_dir": str(dest), "error": "a run with that name exists again"})
                    continue
                try:
                    os.rename(r, dest)
                    restored.append(str(dest))
                except OSError as ex:
                    failed.append({"run_dir": str(dest), "error": str(ex)})
            shutil.rmtree(src, ignore_errors=True)
        if missing:
            if action == "purge":
                return {"ok": True, "done": []}, 200
            return {"ok": False, "error": "Too late: those runs are already gone."}, 410
        for d in designs:
            purge_stale(d)
        return {"ok": not failed, "done": restored, "failed": failed}, 200

    if action not in ("archive", "restore", "tag", "untag", "delete"):
        return {"ok": False, "error": f"unknown action {action!r}"}, 400
    if not isinstance(run_dirs, list) or not run_dirs:
        return {"ok": False, "error": "no runs given"}, 400
    if len(run_dirs) > 5000:
        return {"ok": False, "error": "too many runs in one request"}, 400

    checked: List[Path] = []
    for rd in run_dirs:
        try:
            p = check_run_dir(rd, known=known_list)
        except ValueError as ex:
            failed.append({"run_dir": str(rd), "error": str(ex)})
            continue
        if str(p) in live_set and action in ("delete", "archive"):
            failed.append({"run_dir": str(p), "error": "still running: stop it first"})
            continue
        checked.append(p)

    if action in ("tag", "untag"):
        want = clean_tags(tags or [])
        if not want:
            return {"ok": False, "error": "no valid tag given (letters, digits, space, _ . - /; up to 32)"}, 400
        low = {t.lower() for t in want}
        for p in checked:
            try:
                cur = read_tags(p)
                if action == "tag":
                    new = cur + [t for t in want if t.lower() not in {c.lower() for c in cur}]
                else:
                    new = [c for c in cur if c.lower() not in low]
                write_tags(p, new)
                done.append(str(p))
            except OSError as ex:
                failed.append({"run_dir": str(p), "error": str(ex)})
        return {"ok": not failed, "done": done, "failed": failed}, 200

    if action in ("archive", "restore"):
        for p in checked:
            try:
                set_archived(p, action == "archive")
                done.append(str(p))
            except OSError as ex:
                failed.append({"run_dir": str(p), "error": str(ex)})
        return {"ok": not failed, "done": done, "failed": failed}, 200

    # delete
    names = {(str(p.parent.parent), p.name) for p in checked}
    if not force and children_of is not None:
        kids: List[Dict[str, str]] = []
        for p in checked:
            try:
                for k in children_of(p.parent.parent, p.name) or []:
                    if (str(p.parent.parent), k) not in names:
                        kids.append({"run": k, "parent": p.name})
            except Exception:
                continue
        if kids:
            return {"ok": False, "error": "resumed-from", "children": kids}, 409
    b = _new_batch()
    total = 0
    for p in checked:
        try:
            size = folder_bytes(p)
            dest_root = _trash_root(p.parent.parent) / b
            dest_root.mkdir(parents=True, exist_ok=True)
            os.rename(p, dest_root / p.name)
            total += size
            done.append(str(p))
        except OSError as ex:
            failed.append({"run_dir": str(p), "error": str(ex)})
    for d in {p.parent.parent for p in checked}:
        purge_stale(d)
    return {"ok": not failed, "done": done, "failed": failed, "batch": b, "bytes": total}, 200


# ---------------------------------------------------------------- values
def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _metrics_of(run: Path) -> Dict[str, Any]:
    m = _load(run / "final" / "metrics.json")
    if not isinstance(m, dict):
        m = _load(run / "metrics.json")
    return m if isinstance(m, dict) else {}


def _config_of(run: Path) -> Dict[str, Any]:
    c = _load(run / "resolved.json")
    if not isinstance(c, dict):
        c = _load(run / "config.json")
    return c if isinstance(c, dict) else {}


def _sig(run: Path) -> Optional[tuple]:
    out = []
    for f in (run, run / "final" / "metrics.json", run / "metrics.json", run / "resolved.json"):
        try:
            st = os.stat(f)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out) if out[0] else None


_VALUES: Dict[str, Tuple[tuple, Dict[str, Any]]] = {}
_VALUES_LOCK = threading.Lock()
_VALUES_MAX = 6000


def values(run_dirs: List[Any], keys: List[Any], *, live: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
    """``{run_dir: {key: value}}`` for keys ``m:<metric>`` (from
    ``final/metrics.json``) and ``s:<setting>`` (from ``resolved.json``).
    A key the run doesn't have is left out (the page shows a blank, never a
    made-up value). Cached per run while its files are unchanged."""
    want = [k for k in keys if isinstance(k, str) and len(k) < 200 and k[:2] in ("m:", "s:")][:64]
    live_set = {str(Path(x).resolve()) for x in live if x}
    out: Dict[str, Dict[str, Any]] = {}
    for rd in run_dirs[:5000]:
        if not isinstance(rd, str):
            continue
        run = Path(rd)
        if run.parent.name != "runs":
            continue
        sig = _sig(run)
        if sig is None:
            continue
        key = str(run)
        with _VALUES_LOCK:
            hit = _VALUES.get(key)
        have = dict(hit[1]) if hit and hit[0] == sig and key not in live_set else {}
        missing = [k for k in want if k not in have]
        if missing:
            met = _metrics_of(run) if any(k.startswith("m:") for k in missing) else {}
            cfg = _config_of(run) if any(k.startswith("s:") for k in missing) else {}
            for k in missing:
                src = met if k.startswith("m:") else cfg
                have[k] = src.get(k[2:], _ABSENT)
            with _VALUES_LOCK:
                if len(_VALUES) >= _VALUES_MAX and key not in _VALUES:
                    _VALUES.clear()
                _VALUES[key] = (sig, have)
        out[rd] = {k: have[k] for k in want if have.get(k, _ABSENT) is not _ABSENT}
    return out


class _Absent:
    def __repr__(self) -> str:  # pragma: no cover
        return "<absent>"


_ABSENT = _Absent()


# ---------------------------------------------------------------- names (Columns)
def _base(metric: str) -> str:
    """LibreLane's base metric name: the ``__name:value`` parts removed
    (``timing__setup__ws__corner:x`` -> ``timing__setup__ws``)."""
    return "__".join(p for p in metric.split("__") if ":" not in p)


def is_path_value(v: Any) -> bool:
    """A setting whose value is a file or folder (these differ between any
    two run folders, so they are left out of "what changed")."""
    if isinstance(v, str):
        return v.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", v)) or v.startswith("dir::")
    if isinstance(v, list) and v:
        return all(is_path_value(x) for x in v)
    return False


_NOISE = re.compile(r"^(meta|DESIGN_DIR|PDK_ROOT|RUN_TAG|.*_PATH|.*_DIR)$")


def plain_setting(name: str, v: Any) -> bool:
    return not _NOISE.match(name) and not is_path_value(v)


def keys(design_dir: str | Path, *, runs: int = 40) -> Dict[str, Any]:
    """The metric base names and the plain settings this project's newest
    runs have, for the Columns picker: ``{metrics: [{key, n}], settings:
    [{key, n}]}`` (``n``: how many of the runs read have it)."""
    from . import history
    try:
        dirs = history._walk_runs(Path(design_dir).resolve())[:runs]
    except Exception:
        dirs = []
    mcount: Dict[str, int] = {}
    scount: Dict[str, int] = {}
    for i, run in enumerate(dirs):
        for k in _metrics_of(run):
            if isinstance(k, str):
                b = _base(k)
                mcount[b] = mcount.get(b, 0) + 1
        if i < 8:
            for k, v in _config_of(run).items():
                if isinstance(k, str) and plain_setting(k, v):
                    scount[k] = scount.get(k, 0) + 1
    return {
        "runs_read": len(dirs),
        "metrics": [{"key": k, "n": n} for k, n in sorted(mcount.items())],
        "settings": [{"key": k, "n": n} for k, n in sorted(scount.items())],
    }


# ---------------------------------------------------------------- what changed
def _norm(v: Any, prefixes: List[Tuple[str, str]]) -> Any:
    if isinstance(v, str):
        for a, b in prefixes:
            if a and v.startswith(a):
                return b + v[len(a):]
        return v
    if isinstance(v, list):
        return [_norm(x, prefixes) for x in v]
    return v


def _prefixes(run: Path, cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    out = [(str(run), "<run>")]
    for k, label in (("PDK_ROOT", "<pdk>"), ("DESIGN_DIR", "<design>")):
        v = cfg.get(k)
        if isinstance(v, str) and v:
            out.append((v.rstrip("/"), label))
    return out


def _show(v: Any) -> str:
    if v is None:
        return "unset"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


_CHANGED: Dict[Tuple[str, str], Tuple[tuple, tuple, List[Dict[str, str]]]] = {}
_CHANGED_LOCK = threading.Lock()


def changed(base_dir: Any, run_dirs: List[Any]) -> Dict[str, List[Dict[str, str]]]:
    """What each run's resolved settings changed against the baseline's:
    ``{run_dir: [{var, from, to}]}``. Plain settings only (paths and folders
    differ between any two run folders); a run without ``resolved.json``
    (stopped before it started) is left out."""
    if not isinstance(base_dir, str):
        return {}
    base = Path(base_dir)
    bsig = _sig(base)
    bcfg = _config_of(base) if bsig else {}
    if not bcfg:
        return {}
    bpre = _prefixes(base, bcfg)
    out: Dict[str, List[Dict[str, str]]] = {}
    for rd in run_dirs[:2000]:
        if not isinstance(rd, str):
            continue
        run = Path(rd)
        if run.parent.name != "runs":
            continue
        rsig = _sig(run)
        if rsig is None:
            continue
        ck = (str(base), str(run))
        with _CHANGED_LOCK:
            hit = _CHANGED.get(ck)
        if hit and hit[0] == bsig and hit[1] == rsig:
            out[rd] = hit[2]
            continue
        cfg = _config_of(run)
        if not cfg:
            continue
        pre = _prefixes(run, cfg)
        diffs: List[Dict[str, str]] = []
        for k in sorted(set(bcfg) | set(cfg)):
            a, b = bcfg.get(k), cfg.get(k)
            if not plain_setting(k, a) or not plain_setting(k, b):
                continue
            na, nb = _norm(a, bpre), _norm(b, pre)
            if json.dumps(na, sort_keys=True, default=str) != json.dumps(nb, sort_keys=True, default=str):
                diffs.append({"var": k, "from": _show(a), "to": _show(b)})
        with _CHANGED_LOCK:
            if len(_CHANGED) > 20000:
                _CHANGED.clear()
            _CHANGED[ck] = (bsig, rsig, diffs)
        out[rd] = diffs
    return out


# ---------------------------------------------------------------- directions
# LibreLane's registry knows most metrics; these it doesn't, and LanEx reads
# them the same way as their registered relatives (each with its reason).
LANEX_DIRECTIONS = {
    "design__instance__area__stdcell": (False, False, "an area, like design__instance__area"),
    "design__instance__area__macros": (False, False, "an area, like design__instance__area"),
    "design__instance__area__padcells": (False, False, "an area, like design__instance__area"),
    "design__instance__count__stdcell": (False, False, "a count, like design__instance__count"),
    "klayout__drc_error__count": (False, True, "a DRC count, like magic__drc_error__count"),
}


def metric_directions() -> Dict[str, Dict[str, Any]]:
    """``{base name: {higher_is_better: bool|None, critical, source}}``
    from LibreLane's own registry (None kept: LibreLane has no direction for
    it), plus :data:`LANEX_DIRECTIONS`."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from librelane.common.metrics.metric import Metric  # type: ignore
        from librelane.common.metrics import library as _library  # noqa: F401
        for name, m in (getattr(Metric, "by_name", {}) or {}).items():
            hib = getattr(m, "higher_is_better", None)
            out[name] = {"higher_is_better": hib if isinstance(hib, bool) else None,
                         "critical": bool(getattr(m, "critical", False)), "source": "librelane"}
    except Exception:
        pass
    for k, (hib, crit, why) in LANEX_DIRECTIONS.items():
        out.setdefault(k, {"higher_is_better": hib, "critical": crit, "source": "lanex", "why": why})
    return out


def direction_of(metric: str, table: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The entry for *metric*: its full name first, else its base name."""
    return table.get(metric) or table.get(_base(metric))


# ---------------------------------------------------------------- export
def export_json(rows: List[Dict[str, Any]], *, design: Dict[str, Any], baseline: Optional[str],
                view: Any, all_metrics: bool) -> str:
    """The runs as ``lanex-runs/1`` JSON: every row the page sent, plus each
    run's final metrics (all of them, or the ones in the row) and provenance
    read from the run folder."""
    out_runs = []
    for r in rows:
        rd = r.get("run_dir")
        run = Path(rd) if isinstance(rd, str) else None
        extra: Dict[str, Any] = {}
        if run is not None and run.is_dir() and run.parent.name == "runs":
            cfg = _config_of(run)
            meta = cfg.get("meta") if isinstance(cfg.get("meta"), dict) else {}
            gui = _load(run / "gui-run.json")
            gui = gui if isinstance(gui, dict) else {}
            extra = {
                "librelane_version": meta.get("librelane_version"),
                "flow": meta.get("flow") or gui.get("flow"),
                "config_hash": gui.get("config_hash"),
                "overrides": gui.get("overrides") or {},
                "command": (gui.get("cli_command") or {}).get("container") if isinstance(gui.get("cli_command"), dict) else gui.get("cli_command"),
            }
            if all_metrics:
                extra["metrics"] = _metrics_of(run)
        out_runs.append({**r, **{k: v for k, v in extra.items() if v is not None or k == "metrics"}})
    doc = {
        "format": "lanex-runs/1",
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "lanex_version": _lanex_version(),
        "design": design,
        "baseline": baseline,
        "view": view,
        "runs": out_runs,
    }
    return json.dumps(doc, indent=1, default=str, allow_nan=True)


def all_metrics_table(run_dirs: List[str]) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Every final metric of the runs: (sorted names, {run_dir: metrics})."""
    names = set()
    per: Dict[str, Dict[str, Any]] = {}
    for rd in run_dirs[:5000]:
        run = Path(rd)
        if run.parent.name != "runs":
            continue
        m = _metrics_of(run)
        per[rd] = m
        names.update(k for k in m if isinstance(k, str))
    return sorted(names), per


def _lanex_version() -> str:
    try:
        from .. import __version__
        return str(__version__)
    except Exception:
        return ""


# ---------------------------------------------------------------- CSV for Excel
_FORMULA = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")
_NUMBER = re.compile(r"^-?\d*\.?\d+([eE][+-]?\d+)?$")


def csv_cell(v: Any) -> str:
    """One CSV field the way Excel and LibreOffice read it right: numbers bare
    (LibreLane's own value, "." decimals), text quoted, a text that a
    spreadsheet would run as a formula prefixed with ' (OWASP)."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return '"yes"' if v else '"no"'
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return "NaN"
        if isinstance(v, float) and v in (float("inf"), float("-inf")):
            return "Infinity" if v > 0 else "-Infinity"
        return repr(v) if isinstance(v, float) else str(v)
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    if s.startswith(_FORMULA) and not _NUMBER.match(s):
        s = "'" + s
    return '"' + s.replace('"', '""') + '"'


def excel_csv(rows: List[List[Any]]) -> str:
    """Rows as CSV text: a UTF-8 BOM (Excel reads UTF-8 only with it), commas,
    CRLF row ends (RFC 4180, as LibreLane's own metrics.csv), no ``sep=``."""
    return "\ufeff" + "".join(",".join(csv_cell(c) for c in r) + "\r\n" for r in rows)
