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
"""Design-Space Exploration (Phase 2.B): run one design under N configs.

DSE is **a queue of normal runs** — it reuses the existing
:class:`runner.FlowRunner` (container or local), one config at a time (single
engine, storage-sane), each with a distinct ``--run-tag``. This module owns the
pure planning (sweep expansion, deterministic tags, combo cap + validation) and
a small sequential queue manager that emits ``dse_*`` progress events on the
shared bus. No new dependency; no new run engine.
"""
from __future__ import annotations

import itertools
import re
import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import events, introspect

# Hard cap on enumerated combos to avoid runaway storage (each run writes a dir).
MAX_COMBOS = 64

# Sweep manifests live as a sidecar in the design dir (travels with the project,
# matching the existing `.gui-custom-cells.json` / `gui-run.json` convention) so
# a server restart can still list past sweeps without re-deriving them from a
# fragile run-tag regex, and so a re-run never silently overwrites a prior sweep.
_SWEEPS_FILE = ".gui-dse-sweeps.json"


def _sweeps_path(design_dir: str | Path) -> Path:
    return Path(design_dir) / _SWEEPS_FILE


def _read_sweeps_file(p: Path) -> Optional[List[Dict[str, Any]]]:
    """The manifests in *p*: [] when there is no file, None when it is there
    but unreadable (so a writer can keep it aside instead of overwriting it)."""
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    sweeps = doc.get("sweeps") if isinstance(doc, dict) else doc
    if not isinstance(sweeps, list):
        return None
    return [s for s in sweeps if isinstance(s, dict)]


def _assume_zone(created_at: Any) -> tuple:
    """``(iso, assumed, epoch)`` for a manifest's ``created_at``.

    Sweeps recorded before 2026-10-28 wrote ``datetime.now().isoformat()``:
    no offset, in the LanEx machine's own clock (the distro runs on UTC). A
    browser would read such a string as ITS local time and shift it by the
    person's offset, so it is read here the way it was written (this
    machine's zone), given an offset, and flagged as assumed."""
    from datetime import datetime
    if not isinstance(created_at, str) or not created_at:
        return created_at, False, 0.0
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError:
        return created_at, False, 0.0
    assumed = dt.tzinfo is None
    if assumed:
        dt = dt.astimezone()          # naive -> this machine's local zone
    try:
        ts = dt.timestamp()
    except (OverflowError, OSError, ValueError):
        ts = 0.0
    return dt.isoformat(timespec="seconds"), assumed, ts


def load_sweeps(design_dir: str | Path) -> List[Dict[str, Any]]:
    """All recorded sweep manifests for a design, newest first (by the real
    instant: old offset-less and new offset-carrying times sort together).

    A ``created_at`` without an offset gets this machine's offset and
    ``created_at_note`` saying so (see :func:`_assume_zone`)."""
    sweeps = _read_sweeps_file(_sweeps_path(design_dir)) or []
    keyed = []
    for s in sweeps:
        iso, assumed, ts = _assume_zone(s.get("created_at"))
        if assumed:
            from datetime import datetime
            zone = datetime.now().astimezone().tzname() or "this machine's zone"
            s["created_at"] = iso
            s["created_at_note"] = (f"Recorded by an older LanEx without a time zone; read as {zone} "
                                    "(the LanEx machine's clock) and shown in your time.")
        keyed.append((ts, str(s.get("id") or ""), s))
    keyed.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s for _, _, s in keyed]


def new_sweep_id(design_dir: str | Path | None = None) -> str:
    """A sortable, collision-resistant sweep id (``YYYYMMDD-HHMMSS``), in the
    person's own time zone (the distro's clock runs on UTC). With
    ``design_dir``, an id already recorded there gets ``-2``, ``-3``..."""
    from . import platform_env
    base = platform_env.local_now().strftime("%Y%m%d-%H%M%S")
    if design_dir is None:
        return base
    taken = {s.get("id") for s in load_sweeps(design_dir)}
    sid, k = base, 1
    while sid in taken:
        k += 1
        sid = f"{base}-{k}"
    return sid


# Serialises every load-modify-write of a sweeps file: the sweep worker
# records each point while a request thread may be recording the manifest.
_sweeps_lock = threading.RLock()


def _write_sweeps(p: Path, sweeps: List[Dict[str, Any]]) -> None:
    # A file that is there but unreadable (hand-edited, a disk hiccup) is
    # kept aside, never overwritten with only the new sweep: its history may
    # still be recoverable by hand.
    if _read_sweeps_file(p) is None:
        try:
            p.replace(p.with_name(f"{p.name}.unreadable-{int(time.time())}"))
        except OSError:
            pass
    # A unique temp name per write, so two writers never share one file.
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps({"sweeps": sweeps}, indent=2), encoding="utf-8")
        tmp.replace(p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def record_sweep(design_dir: str | Path, sweep: Dict[str, Any]) -> None:
    """Append (or replace by id) a sweep manifest. Best-effort; never raises.
    Point results already recorded under the same id are kept when ``sweep``
    brings none of its own."""
    try:
        with _sweeps_lock:
            p = _sweeps_path(design_dir)
            existing = load_sweeps(design_dir)
            sid = sweep.get("id")
            prior = next((s for s in existing if s.get("id") == sid), None)
            sweep = dict(sweep)
            if prior and prior.get("results") and "results" not in sweep:
                sweep["results"] = prior["results"]
            existing = [s for s in existing if s.get("id") != sid]
            existing.insert(0, sweep)
            # Keep the file bounded.
            existing = existing[:200]
            _write_sweeps(p, existing)
    except Exception:
        pass


def forget_sweep(design_dir: str | Path, sweep_id: str) -> None:
    """Drop the manifest of a sweep that never started. Best-effort."""
    try:
        with _sweeps_lock:
            existing = load_sweeps(design_dir)
            kept = [s for s in existing if s.get("id") != sweep_id]
            if len(kept) != len(existing):
                _write_sweeps(_sweeps_path(design_dir), kept)
    except Exception:
        pass


def record_point(design_dir: str | Path, sweep_id: str, tag: str, result: Dict[str, Any]) -> None:
    """Note how one point of a sweep ended in its manifest (``results``), so
    the outcome survives a reload and a restart. Best-effort; never raises."""
    try:
        with _sweeps_lock:
            for s in load_sweeps(design_dir):
                if s.get("id") == sweep_id:
                    res = dict(s.get("results") or {})
                    res[tag] = dict(result)
                    s["results"] = res
                    record_sweep(design_dir, s)
                    return
    except Exception:
        pass


def stamp_run(run_dir: str | Path, sweep_id: str, values: Dict[str, Any]) -> None:
    """Mark a run folder as a sweep point (``gui-run.json`` ``sweep``), so the
    Runs table can group it and name its origin. Best-effort."""
    try:
        p = Path(run_dir) / "gui-run.json"
        if not p.parent.is_dir():
            return
        doc = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        if not isinstance(doc, dict):
            doc = {}
        doc["sweep"] = {"id": sweep_id, "values": dict(values)}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def unique_base_tag(design_dir: str | Path, base_tag: str, n: int) -> str:
    """Return a base tag whose ``dse-<base>-NN`` run dirs don't already exist.

    DSE used a deterministic ``dse-<base>-NN`` scheme with ``overwrite=True`` — a
    second sweep with the same base silently destroyed the first sweep's run
    dirs. Here we bump the base (``<base>``, ``<base>-2``, …) until none of the N
    target run dirs collide, so prior sweeps are preserved by default.
    """
    runs_root = Path(design_dir) / "runs"
    base = base_tag or "sweep"
    candidate = base
    suffix = 1
    while True:
        tags = [f"dse-{candidate}-{i:02d}" for i in range(n)]
        if not runs_root.is_dir() or not any((runs_root / t).exists() for t in tags):
            return candidate
        suffix += 1
        candidate = f"{base}-{suffix}"


def system_resources() -> Dict[str, Any]:
    """Best-effort host memory/CPU snapshot used to warn before a DSE sweep.

    Each DSE config is a *full* RTL→GDS flow; OpenROAD detailed routing + STA
    peak at several GB of RAM. On a machine with little free RAM and **no swap**,
    a single heavy run can exhaust memory and freeze the desktop session — which
    is exactly what makes an unattended N-run sweep risky. We surface the numbers
    so the GUI can warn (it never blocks the user).

    Uses ``psutil`` — already a LibreLane dependency (see ``runner.py`` /
    ``manualcmd.py``), so no new dependency is added. Degrades gracefully (numbers
    become ``None``) when it can't be read. Cross-platform.
    """
    info: Dict[str, Any] = {
        "ok": False, "total_gb": None, "available_gb": None,
        "swap_gb": None, "cores": None, "risk": "unknown", "reasons": [],
    }
    try:
        import psutil  # LibreLane dependency, not a new one

        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        info["total_gb"] = round(vm.total / 2 ** 30, 1)
        info["available_gb"] = round(vm.available / 2 ** 30, 1)
        info["swap_gb"] = round(sw.total / 2 ** 30, 1)
        info["cores"] = psutil.cpu_count(logical=True)
        info["ok"] = True
    except Exception:
        info["cores"] = os.cpu_count()
        return info

    reasons: List[str] = []
    avail = info["available_gb"]
    swap = info["swap_gb"]
    # Heuristics tuned for a real RTL→GDS flow (routing/STA peak ~2–6 GB).
    if avail is not None and avail < 4:
        reasons.append(f"only {avail} GB RAM free right now")
    if swap == 0:
        reasons.append("no swap configured (a memory spike hangs the machine "
                       "instead of slowing down)")
    if info["total_gb"] is not None and info["total_gb"] < 16:
        reasons.append(f"{info['total_gb']} GB total RAM is tight for back-to-back flows")
    if (avail is not None and avail < 3) or (swap == 0 and (info["total_gb"] or 0) < 12):
        info["risk"] = "high"
    elif reasons:
        info["risk"] = "elevated"
    else:
        info["risk"] = "ok"
    info["reasons"] = reasons
    return info


@lru_cache(maxsize=1)
def _known_var_names() -> frozenset:
    return frozenset(v["name"] for v in introspect.list_variables())


@lru_cache(maxsize=1)
def _var_info() -> Dict[str, Dict[str, Any]]:
    """introspect's description of every variable (kind, choices, default,
    units, description), by name. Empty without LibreLane."""
    try:
        return {v["name"]: v for v in introspect.list_variables()}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _variable_objects() -> Dict[str, Any]:
    """LibreLane's own ``Variable`` objects by name, gathered the way
    introspect gathers them, so a swept value is checked exactly the way
    LibreLane will read it from ``-c VAR=value``. Empty without LibreLane."""
    out: Dict[str, Any] = {}
    if not getattr(introspect, "_LIBRELANE_AVAILABLE", False):
        return out
    try:
        from librelane.config.config import (flow_common_variables, pad_variables,
                                              pdk_variables, scl_variables)
        for var in flow_common_variables + pad_variables + pdk_variables + scl_variables:
            out.setdefault(var.name, var)
    except Exception:
        pass
    try:
        steps = introspect._step_registry().values()
    except Exception:
        steps = []
    for cls in steps:
        try:
            for var in cls.get_all_config_variables():
                out.setdefault(var.name, var)
        except Exception:
            continue
    try:
        flows = introspect._flow_registry().values()
    except Exception:
        flows = []
    for cls in flows:
        for var in getattr(cls, "config_vars", None) or []:
            out.setdefault(var.name, var)
    return out


_VAR_RX = re.compile(r"[A-Z][A-Z0-9_]{0,79}")


def _format_value(v: Any) -> str:
    """Format a sweep value as a LibreLane override string (permissive)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# Kinds whose values can be checked before any run (introspect._kind_of).
# Paths, lists, dicts and free text are left to LibreLane: a path is only
# known to be right once the run resolves it.
_CHECKED_KINDS = ("int", "number", "bool", "choice", "numbers")

# Values LibreLane takes but that can only fail: checked before the first
# point so a typo does not cost a full run.
_LIMITS = {
    "FP_CORE_UTIL": (0.0, 100.0, "a percentage above 0 and at most 100"),
    "PL_TARGET_DENSITY_PCT": (0.0, 100.0, "a percentage above 0 and at most 100"),
    "CLOCK_PERIOD": (0.0, None, "a clock period in ns above 0"),
}


def _looks_like(kind: str, s: str, choices: Sequence[str]) -> bool:
    """A plain check of *s* against *kind* when LibreLane's own Variable is
    not at hand (tests without LibreLane, a variable introspect lists only
    by its description)."""
    def isnum(x: str) -> bool:
        try:
            f = float(x)
        except ValueError:
            return False
        return f == f and abs(f) != float("inf")
    if kind == "int":
        return re.fullmatch(r"[+-]?\d+", s) is not None
    if kind == "number":
        return isnum(s)
    if kind == "bool":
        return s.lower() in ("true", "false", "1", "0")
    if kind == "choice":
        return not choices or s in choices
    if kind == "numbers":
        parts = [p for p in re.split(r"[,;\s]+", s) if p]
        return bool(parts) and all(isnum(p) for p in parts)
    return True


def _type_words(var: str, s: str, kind: str, choices: Sequence[str]) -> str:
    shown = s if len(s) <= 40 else s[:37] + "..."
    if kind == "int":
        return f"{var} takes a whole number, and '{shown}' isn't one."
    if kind == "number":
        return f"{var} takes a number, and '{shown}' isn't one."
    if kind == "bool":
        return f"{var} takes true or false, not '{shown}'."
    if kind == "choice":
        opts = ", ".join(list(choices)[:8]) + (", ..." if len(choices) > 8 else "")
        return f"{var} takes one of {opts}, not '{shown}'."
    if kind == "numbers":
        return f"{var} takes numbers separated by spaces (like 0 0 200 200), not '{shown}'."
    return f"{var} can't take '{shown}'."


def check_value(var: str, value: Any) -> Optional[str]:
    """Why LibreLane would refuse *value* for *var*, in plain words, or None.

    Numbers, whole numbers, true/false, choices and number lists are checked
    with LibreLane's own ``Variable.compile`` (the reading ``-c VAR=value``
    gets), so ``FP_CORE_UTIL=abc`` is refused here instead of failing a run;
    a few settings also have a range that can only fail (``_LIMITS``)."""
    s = _format_value(value).strip()
    if not s:
        return f"{var}: one of its values is empty."
    info = _var_info().get(var) or {}
    kind = info.get("kind")
    choices = info.get("choices") or []
    if kind in _CHECKED_KINDS:
        obj = _variable_objects().get(var)
        ok = True
        if obj is not None:
            try:
                from librelane.common.generic_dict import GenericDict
                obj.compile(GenericDict({var: s}), warning_list_ref=[], permissive_typing=True)
            except Exception:
                ok = False
        else:
            ok = _looks_like(kind, s, choices)
        if not ok:
            return _type_words(var, s, kind, choices)
    lim = _LIMITS.get(var)
    if lim:
        lo, hi, words = lim
        try:
            x = float(s)
        except ValueError:
            return _type_words(var, s, "number", [])
        if not (x > lo) or (hi is not None and x > hi):
            return f"{var} = {s} can't work: it takes {words}."
    return None


def value_problems(axes: Sequence[Dict[str, Any]], limit: int = 8) -> List[Dict[str, str]]:
    """Every swept value LibreLane would refuse: ``[{var, value, message}]``."""
    out: List[Dict[str, str]] = []
    for ax in axes or []:
        var = ax.get("var") if isinstance(ax, dict) else None
        if not isinstance(var, str) or not var:
            continue
        for v in ax.get("values") or []:
            if isinstance(v, (dict, list)) or v is None:
                continue
            msg = check_value(var, v)
            if msg:
                out.append({"var": var, "value": _format_value(v), "message": msg})
                if len(out) >= limit:
                    return out
    return out


def expand_sweep(spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Expand a sweep spec into the list of per-run override dicts.

    ``spec = {"axes":[{"var":"FP_CORE_UTIL","values":[40,50,60]}, ...],
    "mode":"grid|list"}``. ``grid`` = cartesian product; ``list`` = zipped
    (all axes must share length). Validates every ``var`` against
    ``introspect.list_variables()`` and enforces :data:`MAX_COMBOS`. Raises
    ``ValueError`` on a bad spec so the endpoint returns a clear 400.
    """
    axes = spec.get("axes") or []
    mode = spec.get("mode") or "grid"
    if not axes:
        raise ValueError("The sweep has no settings to vary. Add one, then start it.")
    if not isinstance(axes, list) or not all(isinstance(ax, dict) for ax in axes):
        raise ValueError("The sweep's settings are not in the expected form (a list of {var, values}).")
    names = _known_var_names()
    seen = set()
    for ax in axes:
        var = ax.get("var")
        if not var or not isinstance(var, str):
            raise ValueError("A swept setting has no name. Pick one from the list.")
        # Every value becomes ``-c VAR=value`` on LibreLane's command line:
        # only a real variable name, never text that could read as more.
        if not _VAR_RX.fullmatch(var):
            raise ValueError(f"'{var[:40]}' is not a LibreLane setting name (capitals, digits and _).")
        if names and var not in names:
            raise ValueError(f"unknown config variable '{var}'")
        if var in seen:
            raise ValueError(f"{var} is swept twice. Keep one row for it.")
        seen.add(var)
        vals = ax.get("values")
        if not vals or not isinstance(vals, list):
            raise ValueError(f"axis '{var}' has no values")
        for v in vals:
            if isinstance(v, (dict, list)) or v is None:
                raise ValueError(f"{var}: each value must be a number or a word.")
            if any(ch in str(v) for ch in "\r\n\0") or len(str(v)) > 200:
                raise ValueError(f"{var}: a value has a line break or is too long.")

    if mode == "list":
        n = len(axes[0]["values"])
        if any(len(ax["values"]) != n for ax in axes):
            raise ValueError("list mode requires every axis to have the same number of values")
        count = n
    else:
        count = 1
        for ax in axes:
            count *= len(ax["values"])
    # Counted before anything is built: a product of long value lists would
    # otherwise be materialised (and hang the server) before being refused.
    if count > MAX_COMBOS:
        raise ValueError(f"sweep expands to {count} runs (max {MAX_COMBOS}); narrow it down")
    # Every value is checked the way LibreLane will read it, before any point
    # runs: a typo must not cost a full run (or every point of the sweep).
    bad = value_problems(axes, limit=3)
    if bad:
        raise ValueError(" ".join(b["message"] for b in bad))
    if mode == "list":
        combos = [[ax["values"][i] for ax in axes] for i in range(count)]
    else:
        combos = list(itertools.product(*[ax["values"] for ax in axes]))

    out: List[Dict[str, str]] = []
    for combo in combos:
        out.append({axes[i]["var"]: _format_value(combo[i]) for i in range(len(axes))})
    return out


def dse_run_tags(base_tag: str, n: int) -> List[str]:
    """Deterministic, unique per-config run tags: ``dse-<base>-<NN>``."""
    base = base_tag or "sweep"
    return [f"dse-{base}-{i:02d}" for i in range(n)]


# Short names for the variables people sweep most, so a point's run is named by
# what it tried: sweep-util-65, sweep-clk-12.5, sweep-synth-AREA_0.
_SHORT = {
    "FP_CORE_UTIL": "util",
    "CLOCK_PERIOD": "clk",
    "PL_TARGET_DENSITY_PCT": "density",
    "SYNTH_STRATEGY": "synth",
    "FP_ASPECT_RATIO": "aspect",
    "GRT_ADJUSTMENT": "grtadj",
}


def _slug(v: Any) -> str:
    out = "".join(c if (c.isalnum() or c in ".-") else "_" for c in str(v).strip())
    return out.strip("_") or "x"


def sweep_run_tags(design_dir: str | Path, overrides_list: Sequence[Dict[str, Any]],
                   taken: Sequence[str] = ()) -> List[str]:
    """One run name per sweep point, named by the values it tries
    (``sweep-util-65``; two variables: ``sweep-util-50-clk-10``). A name that
    already has a run folder, repeats within the sweep, or is in *taken* (the
    points a continued sweep already has) gets ``-2``, ``-3``… so an earlier
    run is never overwritten."""
    runs_root = Path(design_dir) / "runs"
    used: set = set(taken or ())
    tags: List[str] = []
    for ov in overrides_list:
        parts = []
        for var, val in ov.items():
            parts.append(_SHORT.get(var) or _slug(str(var).lower()))
            parts.append(_slug(val))
        base = ("sweep-" + "-".join(parts))[:80]
        tag, k = base, 1
        while tag in used or (runs_root / tag).exists():
            k += 1
            tag = f"{base}-{k}"
        used.add(tag)
        tags.append(tag)
    return tags


def point_outcome(run_dir: str | Path) -> Dict[str, Any]:
    """How one sweep point really ended, from the run folder (the same reading
    the Flow page uses, runexplain): ``{ok, kind, stage, step, reason}``.
    ``ok`` only for a run that reached the end with nothing raised; a run that
    merely launched is not a pass."""
    try:
        from . import runexplain
        rx = runexplain.explain_run(run_dir)
    except Exception as ex:  # pragma: no cover - unreadable folder
        return {"ok": False, "kind": "unknown", "stage": None, "step": None,
                "reason": f"could not read the run: {ex}"}
    kind = rx.get("kind")
    card = rx.get("card") or {}
    step = rx.get("step") or {}
    reason = None
    if kind == "halted":
        reason = card.get("title") or ((rx.get("errors") or [None])[0])
    elif kind == "checks-failed":
        rc = rx.get("raised_checkers") or []
        names = [str((r.get("id") if isinstance(r, dict) else r) or "").replace("Checker.", "") for r in rc[:3]]
        reason = "sign-off checks failed" + (": " + ", ".join(n for n in names if n) if names else "")
    elif kind == "stopped":
        reason = "stopped"
    elif kind == "incomplete":
        reason = "ended before the last step"
    # "Good enough to stop the sweep" is Verify's passed verdict: a run that
    # finished with timing missed in a corner LibreLane only warns about is
    # "clean" to runexplain but not timing met.
    verdict = None
    if kind == "clean":
        try:
            from . import verify
            verdict = (verify.run_verdict(run_dir) or {}).get("state")
        except Exception:  # pragma: no cover - unreadable folder
            verdict = None
    return {"ok": kind == "clean", "kind": kind, "stage": rx.get("stage"),
            "step": step.get("id") or step.get("dir"), "reason": reason,
            "verdict": verdict, "good": kind == "clean" and verdict == "passed",
            "steps_done": rx.get("steps_done")}


_CODE = re.compile(r"\b([A-Z]{2,4}-\d{4})\b")


def failure_fingerprint(res: Dict[str, Any]) -> Optional[str]:
    """How a point failed, as ``step|code`` (the tool's code, GRT-0116) or
    ``step|words`` (its first words, numbers and paths taken out): two points
    with the same fingerprint failed the same way. None for anything that
    isn't a failure of the flow (clean, timing missed, stopped, skipped)."""
    if res.get("ok") or res.get("skipped") or res.get("kind") not in ("halted", "error"):
        return None
    reason = str(res.get("reason") or "")
    m = _CODE.search(reason)
    tail = m.group(1) if m else re.sub(r"\d+(\.\d+)?", "<n>", re.sub(r"/\S+", "<path>", reason.lower()))[:100].strip()
    if not tail:
        return None
    return str(res.get("step") or res.get("stage") or "") + "|" + tail


def fingerprint_words(fp: str) -> str:
    step, _, tail = fp.partition("|")
    return (step + ", " if step else "") + tail


def proof_stop(res: Dict[str, Any], swept: List[str], flow: str = "Classic") -> Optional[str]:
    """Why every other point would fail like this one, when that is certain:
    it failed at a step that runs before the first step reading any swept
    setting (varstage). None otherwise, or when LibreLane can't be asked."""
    if res.get("ok") or res.get("kind") != "halted" or not swept:
        return None
    step = str(res.get("step") or "")
    try:
        from . import introspect, varstage
        steps = introspect.flow_steps(flow) or []
        first = varstage.resume_point(swept, flow, steps=steps)
    except Exception:
        return None
    if not first or not steps:
        return None
    idx = next((i for i, s in enumerate(steps) if s.get("id") == step), None)
    if idx is None:
        m = re.match(r"^(\d+)-", step)
        idx = int(m.group(1)) - 1 if m else None
    if idx is None or idx >= first["index"]:
        return None
    reason = str(res.get("reason") or "it failed")
    return ("the first point failed at %s (%s), which runs before %s is used, so the other points would fail the same way"
            % (step, reason[:160], first.get("by") or ", ".join(swept)))


def failed_before_any_step(res: Dict[str, Any]) -> bool:
    """A point that ended before LibreLane finished a single step: the run
    never started, or its config did not load. The same reason twice in a row
    means every other point would end the same way."""
    if res.get("ok") or res.get("kind") == "stopped":
        return False
    if res.get("kind") == "error":
        return True
    return res.get("steps_done") == 0 and res.get("kind") in ("halted", "incomplete", None)


# ---------------------------------------------------------------- planning
# What the sheet needs before a sweep starts: each setting's value in this
# design (and where it comes from), every value checked, the one-off Setup
# settings that ride along, and the die-sizing guard.

_DIE_VARS = ("FP_SIZING", "DIE_AREA", "CORE_AREA")


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def effective_values(design_dir: str | Path, keys: Sequence[str],
                     base_overrides: Optional[Dict[str, Any]] = None,
                     pdk: Optional[str] = None, scl: Optional[str] = None) -> Dict[str, Any]:
    """Each key's value for a run of this design, and where it comes from.

    The order LibreLane applies: a ``-c`` override (the one-off settings a
    sweep carries) wins over the config, a ``pdk::``/``scl::`` section that
    matches the run's PDK wins over the config's top level, and the
    variable's default is used when nothing sets it (a PDK variable then
    comes from the PDK, which is not read here: ``value`` is None).

    Returns ``{"values": {key: {value, source, config, config_source,
    scoped, default, description, units, kind, choices}}, "pdk", "scl",
    "config"}`` with ``source`` one of override / config / pdk / default."""
    from . import configedit
    info = configedit.read_config(design_dir)
    data = info.get("data") or {}
    name = info.get("name") or "the config"
    ov = {k: v for k, v in (base_overrides or {}).items() if not _blank(v)}
    try:
        named = configedit.config_pdk(design_dir)
    except Exception:
        named = {}
    pdk = pdk or ov.get("PDK") or named.get("pdk") or None
    scl = scl or ov.get("STD_CELL_LIBRARY") or (named.get("scl") if pdk == named.get("pdk") else None) or None
    meta_all = _var_info()
    out: Dict[str, Any] = {}
    for k in keys:
        if not isinstance(k, str) or not _VAR_RX.fullmatch(k):
            continue
        meta = meta_all.get(k) or {}
        scoped = configedit.scoped_values(data, k)
        applying = [s for s in scoped if configedit.applies(s, pdk, scl)]
        config_value, config_source = None, None
        if applying:
            # Sections nest (pdk:: then scl:: inside it): the deepest that
            # matches is applied last, so it wins.
            config_value, config_source = applying[-1]["value"], f"{name}, {applying[-1]['scope']}"
        elif k in data:
            config_value, config_source = data.get(k), name
        if k in ov:
            value, source = ov[k], "override"
        elif config_source:
            value, source = config_value, "config"
        elif meta.get("pdk"):
            value, source = None, "pdk"
        else:
            value, source = meta.get("default"), "default"
        out[k] = {
            "value": value, "source": source,
            "config": config_value, "config_source": config_source,
            "scoped": [{"scope": s["scope"], "value": s["value"],
                        "applies": configedit.applies(s, pdk, scl)} for s in scoped],
            "default": meta.get("default"), "description": meta.get("description") or "",
            "units": meta.get("units"), "kind": meta.get("kind"),
            "choices": meta.get("choices") or [], "known": bool(meta),
        }
    return {"values": out, "pdk": pdk, "scl": scl, "config": name}


def _as_text(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return "" if v is None else _format_value(v)


def plan_sweep(design_dir: str | Path, *, axes: Sequence[Dict[str, Any]], mode: str = "grid",
               base_overrides: Optional[Dict[str, Any]] = None,
               keys: Sequence[str] = ()) -> Dict[str, Any]:
    """Everything the sweep sheet shows before Start, and the checks Start
    repeats: ``{ok, count, points, problems, value_problems, warnings,
    riders, context, pdk, scl, config}``. ``problems`` block the start;
    ``warnings`` do not."""
    base = {k: v for k, v in (base_overrides or {}).items() if not _blank(v)}
    swept = [a.get("var") for a in axes or [] if isinstance(a, dict) and isinstance(a.get("var"), str)]
    want = list(dict.fromkeys([v for v in swept if v] + [k for k in keys if isinstance(k, str)] + list(_DIE_VARS)))
    ctx = effective_values(design_dir, want, base)
    vals = ctx["values"]
    problems: List[str] = []
    warnings: List[str] = []
    points: List[Dict[str, str]] = []
    vp = value_problems(axes)
    if axes:
        try:
            points = expand_sweep({"axes": list(axes), "mode": mode})
        except ValueError as ex:
            problems.append(str(ex))
    # A fixed die: FP_CORE_UTIL only sizes the die when FP_SIZING is relative.
    sizing = vals.get("FP_SIZING", {})
    fixed = str(_as_text(sizing.get("value")) or "relative").strip().lower() == "absolute"
    if fixed and "FP_CORE_UTIL" in swept and "FP_SIZING" not in swept:
        where = "a one-off setting" if sizing.get("source") == "override" else (sizing.get("config_source") or "the config")
        die = _as_text(vals.get("DIE_AREA", {}).get("value"))
        problems.append(
            f"FP_SIZING is absolute in {where}: the die is fixed" + (f" at DIE_AREA {die}" if die else "") +
            ", so FP_CORE_UTIL does nothing and every point would build the same chip. "
            "Sweep DIE_AREA instead, or add FP_SIZING with the value relative.")
    if "DIE_AREA" in swept and "CORE_AREA" not in swept and not _blank(vals.get("CORE_AREA", {}).get("value")):
        problems.append(
            f"CORE_AREA is fixed at {_as_text(vals['CORE_AREA']['value'])}: a smaller DIE_AREA would cut into it. "
            "Sweep CORE_AREA with it, or take CORE_AREA out of the config.")
    if "DIE_AREA" in swept and not fixed and "FP_SIZING" not in swept:
        problems.append("DIE_AREA is only used when FP_SIZING is absolute, and it is relative here. "
                        "Add FP_SIZING with the value absolute, or sweep FP_CORE_UTIL instead.")
    # The one-off settings from Setup ride along on every point.
    riders = {k: v for k, v in base.items() if k not in ("PDK", "STD_CELL_LIBRARY") and k not in swept}
    for v in swept:
        if v in base:
            warnings.append(f"{v} is also a one-off setting on Setup ({_as_text(base[v])}): the sweep's values replace it.")
    return {"ok": True, "count": len(points), "points": points, "problems": problems,
            "value_problems": vp, "warnings": warnings, "riders": riders,
            "context": vals, "pdk": ctx["pdk"], "scl": ctx["scl"], "config": ctx["config"]}


def continuation(manifest: Dict[str, Any], *, rerun: Optional[Sequence[str]] = None,
                 values: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """The points that continue a finished sweep: its failed points again
    (``rerun``: their tags), or more values of its one setting (``values``).
    Returns ``{points, reruns: [old tags, in point order], axes}`` with the
    manifest's axes grown by the new values. Raises ``ValueError``."""
    axes = [dict(a) for a in manifest.get("axes") or [] if isinstance(a, dict)]
    tags = list(manifest.get("tags") or [])
    pts = list(manifest.get("points") or [])
    results = manifest.get("results") or {}
    if rerun:
        out, old = [], []
        for t in rerun:
            v = (results.get(t) or {}).get("values")
            if not v and t in tags and tags.index(t) < len(pts):
                v = pts[tags.index(t)]
            if not v:
                raise ValueError(f"{t} is not a point of this sweep.")
            out.append({k: _format_value(x) for k, x in v.items()})
            old.append(t)
        if not out:
            raise ValueError("No failed points to run again.")
        return {"points": out, "reruns": old, "axes": axes}
    if values is not None:
        if len(axes) != 1:
            raise ValueError("Values can be added to a sweep of one setting only.")
        var = axes[0].get("var")
        have = {_format_value(x) for x in axes[0].get("values") or []}
        new = []
        for x in values:
            s = _format_value(x).strip()
            if s and s not in have and s not in new:
                new.append(s)
        if not new:
            raise ValueError(f"Those values of {var} are already in the sweep.")
        expand_sweep({"axes": [{"var": var, "values": new}], "mode": "grid"})   # checks names and values
        if len(tags) + len(new) > MAX_COMBOS:
            raise ValueError(f"A sweep holds at most {MAX_COMBOS} runs.")
        axes[0]["values"] = list(axes[0].get("values") or []) + new
        return {"points": [{var: s} for s in new], "reruns": [], "axes": axes}
    raise ValueError("Say which points to run: failed ones again, or new values.")


def record_end(design_dir: str | Path, sweep_id: str, end: Dict[str, Any]) -> None:
    """Note how and why a sweep's queue ended (``ended``): every point ran,
    you stopped it, it stopped at the first clean run, or it stopped because
    the points were failing before any step ran. Best-effort; never raises."""
    try:
        from . import platform_env
        with _sweeps_lock:
            for s in load_sweeps(design_dir):
                if s.get("id") == sweep_id:
                    s["ended"] = {**{k: v for k, v in end.items() if k in ("reason", "detail")},
                                  "at": platform_env.local_now().isoformat(timespec="seconds")}
                    record_sweep(design_dir, s)
                    return
    except Exception:
        pass


class DseJob:
    """A sweep's points, run through ``start_one`` and followed to the end.

    ``start_one(tag, overrides)`` runs one point and blocks until it ends.
    With ``parallel=1`` (the default) the points go one after another, as
    they always did. The server passes every point at once
    (``parallel=len(points)``): each ``start_one`` puts its point in the job
    queue (``jobs.py``), which runs as many at a time as memory and cores
    allow. Emits ``dse_config_started`` / ``dse_config_done`` / ``dse_done``
    on the bus so the SPA can show queue progress. A stop rule (stop at the
    first good point; two points ending the same way before any step) calls
    ``on_halt(reason)``, which drops the points still waiting; points already
    running finish. Cancellation stops the rest. Runs in daemon threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._halt = threading.Event()
        self.queued: List[str] = []
        self.running_tag: Optional[str] = None
        self.running_tags: List[str] = []
        self.done: List[str] = []
        self.failed: List[str] = []
        self.skipped: List[str] = []
        # tag -> {ok, kind, stage, step, reason, values}: how each point ended.
        self.results: Dict[str, Dict[str, Any]] = {}
        self.sweep_id: Optional[str] = None
        self.total = 0
        self.parallel = 1
        self.started_at: Optional[float] = None
        self._on_point = None
        self._on_done = None
        self._on_halt = None
        self.stop_when_good = False
        self.stopped_early = False
        # Why the queue ended: done / stopped / first-clean / failed-fast.
        self.end_reason: Optional[str] = None
        self.end_detail: Optional[str] = None
        self._early: Dict[str, int] = {}
        self._failed_fast: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active": self.active,
                "queued": list(self.queued),
                # The first point going now (older pages read one), and all of them.
                "running": self.running_tags[0] if self.running_tags else self.running_tag,
                "running_tags": list(self.running_tags),
                "done": list(self.done),
                "failed": list(self.failed),
                "skipped": list(self.skipped),
                "results": {k: dict(v) for k, v in self.results.items()},
                "sweep_id": self.sweep_id,
                "total": self.total,
                "parallel": self.parallel,
                "started_at": self.started_at,
                "stopped_early": self.stopped_early,
                "end_reason": self.end_reason,
                "end_detail": self.end_detail,
            }

    def start(self, *, start_one, overrides_list: Sequence[Dict[str, str]],
              tags: Sequence[str], sweep_id: Optional[str] = None,
              on_point=None, stop_when_good: bool = False, on_done=None,
              parallel: int = 1, on_halt=None) -> Dict[str, Any]:
        """Begin. ``start_one(tag, overrides)`` runs one point and blocks
        until it ends; it returns the point's outcome (``{ok, kind, stage,
        step, reason}``, see :func:`point_outcome`) or a bare bool.
        ``on_point(tag, result)`` is called after each point (the route
        persists results into the sweep manifest with it), ``on_done({reason,
        detail})`` once the sweep ends, ``on_halt(reason)`` when a stop rule
        drops the points still waiting. Returns ``{ok, count, run_tags}``."""
        with self._lock:
            if self.active:
                return {"ok": False, "error": "a sweep is already running"}
            self._cancel.clear()
            self._halt.clear()
            self.queued = list(tags)
            self.done = []
            self.failed = []
            self.skipped = []
            self.results = {}
            self.sweep_id = sweep_id
            self.total = len(tags)
            self.parallel = max(1, int(parallel or 1))
            self.started_at = time.time()
            self._on_point = on_point
            self._on_done = on_done
            self._on_halt = on_halt
            self.stop_when_good = bool(stop_when_good)
            self.stopped_early = False
            self.end_reason = None
            self.end_detail = None
            self.running_tag = None
            self.running_tags = []
            self._early = {}
            self._ended_fps = []
            self._failed_fast = None
            self._thread = threading.Thread(
                target=self._run, args=(start_one, list(overrides_list), list(tags)),
                daemon=True, name="librelane.gui.DseJob",
            )
            self._thread.start()
        return {"ok": True, "count": len(tags), "run_tags": list(tags)}

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        """Stop was asked for the current queue."""
        return self._cancel.is_set()

    def _point(self, start_one, i: int, tag: str, ov: Dict[str, Any], total: int) -> None:
        with self._lock:
            self.running_tag = tag
            self.running_tags.append(tag)
            if tag in self.queued:
                self.queued.remove(tag)
        events.publish("dse_config_started", {"tag": tag, "index": i, "total": total, "overrides": ov})
        res: Dict[str, Any]
        try:
            out = start_one(tag, ov)
            # A launched run is not a passed run: the closure reports how
            # the point really ended. A bare bool is accepted for callers
            # that only know whether it ran.
            res = dict(out) if isinstance(out, dict) else {"ok": bool(out), "kind": None,
                                                           "reason": None if out else "did not start"}
        except Exception as ex:  # pragma: no cover - depends on live runner
            events.publish("log", {"message": f"Sweep: could not run {tag}: {ex}"})
            res = {"ok": False, "kind": "error", "reason": f"could not start: {ex}"}
        if self._cancel.is_set() and not res.get("ok"):
            res.setdefault("kind", "stopped")
            res["reason"] = res.get("reason") or "stopped"
        res["values"] = dict(ov)
        ok = bool(res.get("ok"))
        skipped = bool(res.get("skipped"))
        with self._lock:
            if tag in self.running_tags:
                self.running_tags.remove(tag)
            self.running_tag = self.running_tags[0] if self.running_tags else None
            if skipped:
                self.skipped.append(tag)
            else:
                (self.done if ok else self.failed).append(tag)
            self.results[tag] = res
        if self._on_point and not skipped:
            try:
                self._on_point(tag, res)
            except Exception:  # pragma: no cover - persistence is best effort
                pass
        events.publish("dse_config_done", {"tag": tag, "ok": ok, "index": i, "total": total,
                                           "kind": res.get("kind"), "stage": res.get("stage"),
                                           "reason": res.get("reason"), "skipped": skipped})
        if skipped:
            return
        good = bool(res.get("good", ok))
        if good and self.stop_when_good:
            # "Stop when one is good enough": timing met in every corner and
            # every check clean (Verify's passed verdict, not only a flow
            # that finished); the points still waiting are dropped, not run.
            self._stop_rest("first-clean", "Not run: an earlier point met the goal")
            return
        # Two points that ended the same way before any step ran (the
        # config did not load, the run would not start): the rest would too,
        # so they are not run.
        # Fail fast, with proof: the point failed at a step that runs before
        # any swept setting is read, so its value can't matter and every other
        # point would fail the same way. (Two points failing the same way at a
        # step that does read it is only evidence: a utilisation sweep reaching
        # its limit fails like that, and with points running N at a time the
        # failures end first. The study summary says so and offers Stop; it
        # never stops by itself. A clock too fast is a result, never a failure.)
        if not self._cancel.is_set() and not failed_before_any_step(res):
            why = proof_stop(res, list(ov.keys()))
            with self._lock:
                self._ended_fps.append(failure_fingerprint(res))
            if why and self._remaining():
                self._failed_fast = why
                self._stop_rest("failed-fast", "Not run: " + why)
                return
        early = str(res.get("reason") or res.get("kind") or "") if failed_before_any_step(res) else None
        if early and not self._cancel.is_set():
            with self._lock:
                n = self._early.get(early, 0) + 1
                self._early[early] = n
            if n >= 2 and self._remaining():
                self._failed_fast = early
                self._stop_rest("failed-fast", "Not run: two points failed the same way before any step")

    def _remaining(self) -> int:
        """Points not ended yet: not started here, or (all points in the job
        queue at once) started and still waiting or running there."""
        with self._lock:
            return len(self.queued) + (len(self.running_tags) if self.parallel > 1 else 0)

    def _stop_rest(self, why: str, words: str) -> None:
        if not self._remaining():
            return
        with self._lock:
            if self._halt.is_set():
                return
            self._halt.set()
            if why == "first-clean":
                self.stopped_early = True
            # Points not handed to the queue yet are not run either.
            if self.parallel > 1:
                self.skipped.extend(self.queued)
            self.queued = []
        if self._on_halt:
            try:
                self._on_halt(words)
            except Exception:  # pragma: no cover
                pass

    def _run(self, start_one, overrides_list, tags) -> None:
        import queue as _q
        work: "_q.Queue[int]" = _q.Queue()
        for i in range(len(tags)):
            work.put(i)
        total = len(tags)

        def worker() -> None:
            while not self._cancel.is_set() and not self._halt.is_set():
                try:
                    i = work.get_nowait()
                except _q.Empty:
                    return
                self._point(start_one, i, tags[i], overrides_list[i], total)

        n = min(self.parallel, max(1, total))
        if n <= 1:
            worker()
        else:
            ths = [threading.Thread(target=worker, daemon=True, name=f"librelane.gui.DseJob.{k}") for k in range(n)]
            for t in ths:
                t.start()
                # Points enter the queue in their own order.
                time.sleep(0.02)
            for t in ths:
                t.join()
        cancelled = self._cancel.is_set()
        reason = ("stopped" if cancelled else "first-clean" if self.stopped_early
                  else "failed-fast" if self._failed_fast else "done")
        with self._lock:
            self.end_reason = reason
            self.end_detail = self._failed_fast
        # Recorded before the event, so a page that re-reads the sweeps on
        # dse_done already finds why it ended.
        if self._on_done:
            try:
                self._on_done({"reason": reason, "detail": self._failed_fast})
            except Exception:  # pragma: no cover - persistence is best effort
                pass
        events.publish("dse_done", {"done": list(self.done), "failed": list(self.failed),
                                    "skipped": list(self.skipped),
                                    "cancelled": cancelled, "sweep_id": self.sweep_id,
                                    "stopped_early": self.stopped_early, "reason": reason,
                                    "detail": self._failed_fast})
        with self._lock:
            self.running_tag = None
            self.running_tags = []
            self.queued = []


# Process-wide singleton (one engine -> one queue at a time).
job = DseJob()
