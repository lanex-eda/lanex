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
"""Custom hard-macro insertion for a single run (advanced).

A *macro* is a pre-hardened block (an SRAM, a PLL, an analog IP, or a separately
hardened sub-design) that is dropped into the floorplan as a fixed black box and
routed around — it does **not** sit in the standard-cell rows the way a custom
*cell* does (see :mod:`librelane.lanex.controller.customcells`). LibreLane models a
macro with the verified-real ``MACROS`` configuration variable, a
``Dict[str, Macro]`` keyed by the macro's Verilog **module name**; each
:class:`librelane.config.Macro` carries the block's views:

* ``gds`` / ``lef`` — the layout and the abstract (pins/obstructions/size).
  **Both are required** (LibreLane raises if either is missing).
* ``lib`` — per-corner timing; lets synthesis black-box the macro and STA time
  through it. (``Dict[corner-wildcard, [lib…]]``; we key it ``"*"`` = all corners.)
* ``nl`` — a gate-level Verilog netlist of the macro, for power/STA.
* ``spef`` — per-corner parasitics (``Dict[wildcard, [spef…]]``).
* ``spice`` — device netlist for LVS.
* ``instances`` — ``{instance_name: {location, orientation}}``; a fixed
  placement per instance. LibreLane's Classic flow has no automatic macro
  placer (only ``Odb.ManualMacroPlacement``), so every instance needs one; a
  blank location is stored but flagged (see :func:`merge_warning`).

Why an **overlay config file** and not a ``-c KEY=VALUE`` override: LibreLane
parses a *string* value for a ``Dict`` variable as a flat Tcl ``key value …``
list (``config/variable.py``), which cannot represent a nested ``Macro`` object.
But ``Config.load`` accepts a *sequence* of config sources and merges them, and a
mapping member is consumed natively. So we write the enabled macros to a small
``<design>/.gui-macros.json`` overlay and hand its path to the flow as a second
config file — works identically for the in-process (local) flow and the
``librelane`` CLI (``CONFIG_FILES`` is ``nargs=-1``), and the ``dir::`` paths
resolve against ``DESIGN_DIR`` in both. Per-run only; the user's ``config.json``
is never modified. Pure stdlib; writes confined to the design dir; cross-platform
via ``pathlib``.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SIDECAR = ".gui-custom-macros.json"
_OVERLAY = ".gui-macros.json"          # the generated LibreLane config overlay
_MACROS_SUBDIR = "macros"
_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024    # a hardened-macro GDS can be large (as uploads.py)
_PDN_FILE = ".gui-macros-pdn.tcl"      # the power grid LanEx writes for blocks the default can't reach

# A macro module name / instance name is a Verilog identifier. (We also accept a
# leading-digit-free dotted/hier form some flows use, but keep it conservative.)
_IDENT_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# Instance names are flattened paths: ``u_core.u_sram`` or ``mem[0]``.
_INST_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\[\d+\])?(\.[A-Za-z_][A-Za-z0-9_$]*(\[\d+\])?)*$")

# Orientations LibreLane's ``Orientation`` enum accepts (config/variable.py).
ORIENTATIONS = ("N", "S", "E", "W", "FN", "FS", "FE", "FW")

# Macro view kind -> how it lands in the Macro object.
#   shape "list"  -> a ``List[Path]`` field   (gds/lef/nl/spice)
#   shape "corner"-> a ``Dict[wildcard, [Path]]`` field, keyed "*" (lib/spef)
_VIEW_SPECS: Dict[str, Dict[str, Any]] = {
    "gds":   {"field": "gds",   "shape": "list",   "exts": (".gds", ".gds.gz"),      "required": True},
    "lef":   {"field": "lef",   "shape": "list",   "exts": (".lef",),                "required": True},
    "lib":   {"field": "lib",   "shape": "corner", "exts": (".lib",),                "required": False},
    "nl":    {"field": "nl",    "shape": "list",   "exts": (".v", ".sv", ".nl.v"),   "required": False},
    "spef":  {"field": "spef",  "shape": "corner", "exts": (".spef",),               "required": False},
    "spice": {"field": "spice", "shape": "list",   "exts": (".spice", ".sp", ".cir", ".cdl"), "required": False},
    "vh":    {"field": "vh",    "shape": "list",   "exts": (".v", ".vh", ".sv"),     "required": False},
    "pnl":   {"field": "pnl",   "shape": "list",   "exts": (".v", ".pnl.v"),         "required": False},
    # Not part of LibreLane's Macro: Magic's abstract view (MAGIC_DRC_MAGLEFS,
    # so Magic checks around the block and not inside it) and the vendor's
    # behavioural model (kept for simulation; synthesis reads the header).
    "maglef": {"field": None,   "shape": "list",   "exts": (".mag",),                "required": False},
    "model": {"field": None,    "shape": "list",   "exts": (".v", ".sv"),            "required": False},
}

# A view reference that is not a GUI upload: a PDK file (``pdk_dir::``) or one
# already inside the design (``dir::``). Absolute paths are copied in instead.
_REF_RX = re.compile(r"^(pdk_dir|dir)::[^\s]+$")


# --------------------------------------------------------------------------- #
# Sidecar I/O (the GUI's own record; NOT a LibreLane config).
# --------------------------------------------------------------------------- #
def _sidecar_path(design_dir: str | Path) -> Path:
    return Path(design_dir).resolve() / _SIDECAR


def _doc(design_dir: str | Path) -> Dict[str, Any]:
    f = _sidecar_path(design_dir)
    if not f.is_file():
        return {}
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _load(design_dir: str | Path) -> List[Dict[str, Any]]:
    m = _doc(design_dir).get("macros", [])
    return m if isinstance(m, list) else []


def _settings(design_dir: str | Path) -> Dict[str, Any]:
    st = _doc(design_dir).get("settings", {})
    return st if isinstance(st, dict) else {}


def _save(design_dir: str | Path, macros: List[Dict[str, Any]],
          settings: Optional[Dict[str, Any]] = None) -> None:
    from . import platform_env
    f = _sidecar_path(design_dir)
    doc: Dict[str, Any] = {"macros": macros}
    st = _settings(design_dir) if settings is None else settings
    if st:
        doc["settings"] = st
    # Atomic: a crash mid-write must not corrupt the sidecar (a corrupt sidecar
    # reads back as "no macros" and the next save persists the loss).
    platform_env.atomic_write_text(f, json.dumps(doc, indent=2) + "\n")


def _confined(design_dir: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(design_dir.resolve())
        return True
    except Exception:
        return False


def valid_name(name: Any) -> bool:
    """A macro module name: a Verilog identifier, so never ``..``, empty or a
    path."""
    return isinstance(name, str) and 0 < len(name) <= 128 and bool(_IDENT_RX.match(name))


def _macro_dir(d: Path, name: str) -> Optional[Path]:
    """``<design>/macros/<name>``, or None when that would not be a direct
    child of the project's own ``macros`` folder."""
    if not valid_name(name):
        return None
    root = d / _MACROS_SUBDIR
    try:
        root_res = root.resolve()
    except OSError:
        return None
    if root_res.parent != d:
        return None
    return root / name


def list_macros(design_dir: str | Path) -> Dict[str, Any]:
    """Return the design's saved custom macros (read-only)."""
    return {"ok": True, "macros": _load(design_dir), "settings": _settings(design_dir),
            "orientations": list(ORIENTATIONS)}


def set_settings(design_dir: str | Path, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Page-wide macro settings: the keep-out halo around every macro (µm)."""
    clean: Dict[str, Any] = {}
    for k in ("halo_h", "halo_v"):
        v = (settings or {}).get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{k} must be a number of microns"}
        if f < 0 or f > 1000:
            return {"ok": False, "error": f"{k} must be between 0 and 1000 µm"}
        clean[k] = f
    _save(design_dir, _load(design_dir), clean)
    return {"ok": True, "settings": clean}


# --------------------------------------------------------------------------- #
# Instance parsing/validation.
# --------------------------------------------------------------------------- #
def _clean_instances(instances: Optional[List[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Validate the instance list; return (clean, error)."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for inst in instances or []:
        name = (str((inst or {}).get("name") or "")).strip()
        if not name:
            return [], "every instance needs a name (the instance name used in your RTL)"
        if not _INST_RX.match(name):
            return [], (f"instance name '{name}' must be the instance's path in your RTL, "
                        "e.g. u_sram or u_core.u_sram")
        if name in seen:
            return [], f"duplicate instance name '{name}'"
        seen.add(name)
        orient = (str((inst or {}).get("orientation") or "N")).strip().upper() or "N"
        if orient not in ORIENTATIONS:
            return [], f"orientation '{orient}' is not one of {', '.join(ORIENTATIONS)}"
        loc_in = (inst or {}).get("location")
        location: Optional[List[float]] = None
        if loc_in not in (None, "", []):
            try:
                if isinstance(loc_in, str):
                    parts = [p for p in re.split(r"[,\s]+", loc_in.strip()) if p]
                else:
                    parts = list(loc_in)
                if len(parts) != 2:
                    raise ValueError
                location = [float(parts[0]), float(parts[1])]
            except Exception:
                return [], f"location for '{name}' must be two numbers 'x y' (microns)"
        out.append({"name": name, "location": location, "orientation": orient})
    return out, None


# --------------------------------------------------------------------------- #
# Save / enable / remove.
# --------------------------------------------------------------------------- #
def save_macro(
    design_dir: str | Path,
    name: str,
    *,
    instances: Optional[List[Dict[str, Any]]] = None,
    views: Optional[Dict[str, Dict[str, str]]] = None,
    enabled: bool = True,
    refs: Optional[Dict[str, Any]] = None,
    copies: Optional[Dict[str, Any]] = None,
    power: Optional[Dict[str, Any]] = None,
    source: Optional[Dict[str, Any]] = None,
    generated: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Save (or replace) a custom macro for *design_dir*.

    *name* is the macro's Verilog **module name** (the ``MACROS`` dict key).
    *views* maps a kind (``gds``/``lef``/``lib``/``nl``/``spef``/``spice``/``vh``)
    to an upload ``{filename, content_b64}``; *refs* maps a kind to a path used
    in place (``pdk_dir::…`` for a PDK macro); *copies* maps a kind to an
    absolute file (or ``{corner: file}`` for lib/spef) to copy in, e.g. another
    project's finished run. GDS and LEF are required. *instances* is a list of
    ``{name, location, orientation}``. *power* is ``{vdd_pin, gnd_pin, vdd_net,
    gnd_net}`` for ``PDN_MACRO_CONNECTIONS``. Files are written under
    ``macros/<name>/`` and recorded as design-relative ``dir::`` paths. Never
    raises; returns ``{ok, macro}`` or ``{ok: False, error}``."""
    from . import uploads

    d = Path(design_dir).resolve()
    if not d.is_dir():
        return {"ok": False, "error": "design directory not found"}
    name = (name or "").strip()
    if not valid_name(name):
        return {"ok": False, "error": "macro module name must be a valid Verilog identifier (letters/digits/_/$, no leading digit)"}

    clean_inst, err = _clean_instances(instances)
    if err:
        return {"ok": False, "error": err}

    macro_dir = _macro_dir(d, name)
    if macro_dir is None or macro_dir.is_symlink() or not _confined(d, macro_dir):
        return {"ok": False, "error": "refusing to write outside the design dir"}

    # Validate/decode every uploaded view BEFORE writing anything, so a bad file
    # can't leave a half-saved macro on disk.
    decoded: Dict[str, Tuple[str, Any]] = {}
    for kind, payload in (views or {}).items():
        spec = _VIEW_SPECS.get(kind)
        if spec is None:
            return {"ok": False, "error": f"unknown macro view '{kind}'"}
        fname = Path(str((payload or {}).get("filename") or f"{name}.{kind}").replace("\\", "/")).name
        # Space-free on disk: several LibreLane steps hand view paths to Tcl
        # scripts that split lists on whitespace — don't seed that failure.
        fname = re.sub(r"\s+", "_", fname)
        if not fname or re.fullmatch(r"\.+", fname):
            return {"ok": False, "error": "invalid file name"}
        if not any(fname.lower().endswith(e) for e in spec["exts"]):
            return {"ok": False, "error": f"{kind} file should end with {' or '.join(spec['exts'])}"}
        upload_id = (payload or {}).get("upload_id")
        if upload_id:
            staged = uploads.staged(d, upload_id)
            if staged is None:
                return {"ok": False, "error": f"the uploaded {kind} file is gone; add it again"}
            decoded[kind] = (fname, staged)
            continue
        try:
            raw = base64.b64decode((payload or {}).get("content_b64") or "", validate=False)
        except Exception:
            return {"ok": False, "error": f"could not decode {kind} file"}
        if len(raw) > _MAX_FILE_BYTES:
            return {"ok": False, "error": f"{kind} file exceeds {_MAX_FILE_BYTES // (1024*1024)} MiB"}
        decoded[kind] = (fname, raw)

    clean_refs: Dict[str, Any] = {}
    for kind, ref in (refs or {}).items():
        if kind not in _VIEW_SPECS:
            return {"ok": False, "error": f"unknown macro view '{kind}'"}
        items = ref.values() if isinstance(ref, dict) else [ref]
        if not all(isinstance(r, str) and _REF_RX.match(r) for r in items):
            return {"ok": False, "error": f"{kind} must be a pdk_dir:: or dir:: path"}
        clean_refs[kind] = dict(ref) if isinstance(ref, dict) else ref
    copy_plan: Dict[str, Any] = {}
    for kind, src in (copies or {}).items():
        if kind not in _VIEW_SPECS:
            return {"ok": False, "error": f"unknown macro view '{kind}'"}
        items = src.items() if isinstance(src, dict) else [("", src)]
        planned = {}
        for corner, f in items:
            fp = Path(str(f))
            if not fp.is_absolute() or not fp.is_file():
                return {"ok": False, "error": f"{kind} file not found: {f}"}
            if fp.stat().st_size > _MAX_FILE_BYTES:
                return {"ok": False, "error": f"{kind} file exceeds {_MAX_FILE_BYTES // (1024*1024)} MiB"}
            planned[corner] = fp
        copy_plan[kind] = planned if isinstance(src, dict) else planned[""]

    # Carry forward views from a prior save when this save didn't re-upload them,
    # so editing instances/orientation doesn't force re-uploading every file.
    prior = next((m for m in _load(d) if m.get("name") == name), None)
    saved_views: Dict[str, Any] = dict((prior or {}).get("views") or {}) if prior else {}
    if copy_plan or clean_refs:
        # a new source replaces the whole set, so no stale view lingers
        saved_views = {}
    saved_views.update(clean_refs)

    macro_dir.mkdir(parents=True, exist_ok=True)
    for kind, (fname, raw) in decoded.items():
        target = macro_dir / fname
        if target.is_symlink() or target.resolve().parent != macro_dir.resolve():
            return {"ok": False, "error": "invalid file name"}
        try:
            if isinstance(raw, Path):
                uploads.move_into(raw, target)
            else:
                target.write_bytes(raw)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        saved_views[kind] = "dir::" + str(target.relative_to(d)).replace("\\", "/")

    import shutil

    def put(fp: Path, kind: str, corner: str = "") -> str:
        if corner and (not re.fullmatch(r"[A-Za-z0-9_.*\-]+", corner) or re.fullmatch(r"\.+", corner)):
            raise ValueError(f"invalid corner name {corner!r}")
        sub = macro_dir / kind / corner if corner else macro_dir
        sub.mkdir(parents=True, exist_ok=True)
        target = (sub / re.sub(r"\s+", "_", fp.name)).resolve()
        if not _confined(d, target):
            raise ValueError("invalid file name")
        shutil.copyfile(fp, target)
        return "dir::" + str(target.relative_to(d)).replace("\\", "/")
    for kind, plan in copy_plan.items():
        try:
            if isinstance(plan, dict):
                saved_views[kind] = {c: put(fp, kind, "" if c == "*" else c) for c, fp in plan.items()}
            else:
                saved_views[kind] = put(plan, kind)
        except Exception as ex:
            return {"ok": False, "error": f"could not copy the {kind} view: {ex}"}

    if not saved_views.get("gds") or not saved_views.get("lef"):
        return {"ok": False, "error": "a GDS and a LEF view are both required for a macro"}

    macros = [m for m in _load(d) if m.get("name") != name]
    entry = {
        "name": name,
        "enabled": bool(enabled),
        "instances": clean_inst,
        "views": saved_views,
    }
    pw = power if power is not None else (prior or {}).get("power")
    if pw:
        clean_pw = norm_power(pw)
        if clean_pw is None:
            return {"ok": False, "error": "power hookup needs the macro's power and ground pins and the "
                                          "design nets they connect to (names only: letters, digits, _ $ [ ] ! .)"}
        if clean_pw:
            entry["power"] = clean_pw
    src = source if source is not None else (prior or {}).get("source")
    if isinstance(src, dict):
        entry["source"] = {k: src[k] for k in ("kind", "label", "pdk", "library", "design_dir", "tag") if k in src}
    gen = generated if generated is not None else (prior or {}).get("generated")
    if isinstance(gen, dict) and gen:
        entry["generated"] = gen
    macros.append(entry)
    try:
        _save(d, macros)
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    return {"ok": True, "macro": entry}


def set_enabled(design_dir: str | Path, name: str, enabled: bool) -> Dict[str, Any]:
    macros = _load(design_dir)
    found = False
    for m in macros:
        if m.get("name") == name:
            m["enabled"] = bool(enabled)
            found = True
    if not found:
        return {"ok": False, "error": "macro not found"}
    _save(design_dir, macros)
    return {"ok": True}


def remove_macro(design_dir: str | Path, name: str) -> Dict[str, Any]:
    """Remove a custom macro and the folder LanEx stored its files in.

    Only ever deletes ``<design>/macros/<name>`` itself: ``..``, an empty name
    or a path is refused (``macros/..`` is the project folder), and a link in
    its place loses the link, never its target. A record with an unsafe name is
    dropped from the list without touching any file."""
    import shutil
    d = Path(design_dir).resolve()
    if not d.is_dir():
        return {"ok": False, "error": "design directory not found"}
    all_macros = _load(d)
    macros = [m for m in all_macros if m.get("name") != name]
    known = len(macros) != len(all_macros)
    if not valid_name(name):
        if not known:
            return {"ok": False, "error": "no macro by that name"}
        _save(d, macros)
        return {"ok": True, "files_removed": False}
    macro_dir = _macro_dir(d, name)
    if macro_dir is None:
        return {"ok": False, "error": "refusing to delete outside the project's macros folder"}
    _save(d, macros)
    removed = False
    try:
        if macro_dir.is_symlink():
            macro_dir.unlink()
            removed = True
        elif macro_dir.is_dir():
            shutil.rmtree(macro_dir)
            removed = True
    except OSError:
        pass
    return {"ok": True, "files_removed": removed}


# --------------------------------------------------------------------------- #
# Override / overlay generation.
# --------------------------------------------------------------------------- #
def _macro_to_config(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one stored macro into the ``Macro``-shaped dict LibreLane expects."""
    views = entry.get("views") or {}
    cfg: Dict[str, Any] = {}
    for kind, spec in _VIEW_SPECS.items():
        path = views.get(kind)
        if not path or spec["field"] is None:
            continue
        if isinstance(path, dict):            # {corner: path} (a run's lib/spef)
            if spec["shape"] == "list":
                first = next(iter(path.values()), None)
                if first:
                    cfg[spec["field"]] = [first]
            else:
                cfg[spec["field"]] = {_corner_key(c): [p] for c, p in path.items()}
        elif spec["shape"] == "list":
            cfg[spec["field"]] = [path]
        else:  # corner-keyed dict, "*" = all corners
            cfg[spec["field"]] = {"*": [path]}
    instances: Dict[str, Any] = {}
    for inst in entry.get("instances") or []:
        # An instance without a position is never written: LibreLane would
        # skip it and stop at the power grid (PDN-0235). The run check
        # (run_problems) refuses to start such a run instead.
        if not inst.get("location"):
            continue
        instances[inst["name"]] = {
            "location": inst.get("location"),
            "orientation": inst.get("orientation") or "N",
        }
    if instances:
        cfg["instances"] = instances
    return cfg


def _corner_key(corner: str) -> str:
    """A run's ``final/<view>/<dir>`` name as a LibreLane corner wildcard. Lib
    folders are full timing corners (``max_ss_100C_1v60``) and match as-is;
    SPEF folders are RC corners (``min``/``nom``/``max``), which must become
    ``max_*`` to match the timing corners they belong to."""
    if not corner or corner == "*":
        return "*"
    return corner if "_" in corner or "*" in corner else corner + "_*"


def _user_config_list(design_dir: Path, key: str) -> List[str]:
    for ext in ("json", "yaml", "yml"):
        f = design_dir / f"config.{ext}"
        if not f.is_file():
            continue
        try:
            if ext == "json":
                doc = json.loads(f.read_text(encoding="utf-8"))
            else:
                import yaml  # type: ignore
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            return []
        v = doc.get(key) if isinstance(doc, dict) else None
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str) and v.strip():
            return [v]
        return []
    return []


def _inst_regex(name: str) -> str:
    """An instance path as the regex PDN_MACRO_CONNECTIONS expects (dots and
    brackets escaped)."""
    return re.sub(r"([.\[\]])", r"\\\1", name)


_PIN_NAME_RX = re.compile(r"^[\w$\[\]!.*^]+$")


def norm_power(pw: Any) -> Optional[Dict[str, Any]]:
    """A power hookup as ``{vdd_net, gnd_net, vdd_pins: [...], gnd_pins: [...]}``.
    Also takes the older ``{vdd_pin, gnd_pin}`` with ``|`` between pins. ``{}``
    for an empty hookup (removed), None for a bad one."""
    if not isinstance(pw, dict):
        return {}

    def pins(many: str, one: str) -> List[str]:
        v = pw.get(many)
        if v is None:
            v = pw.get(one)
        if isinstance(v, str):
            v = v.split("|")
        return [str(x).strip() for x in (v or []) if str(x).strip()]
    out = {"vdd_net": str(pw.get("vdd_net") or "").strip(), "gnd_net": str(pw.get("gnd_net") or "").strip(),
           "vdd_pins": pins("vdd_pins", "vdd_pin"), "gnd_pins": pins("gnd_pins", "gnd_pin")}
    if not (out["vdd_net"] or out["gnd_net"] or out["vdd_pins"] or out["gnd_pins"]):
        return {}
    names = [out["vdd_net"], out["gnd_net"]] + out["vdd_pins"] + out["gnd_pins"]
    if not (out["vdd_net"] and out["gnd_net"] and out["vdd_pins"] and out["gnd_pins"]) or \
            not all(_PIN_NAME_RX.match(n) for n in names):
        return None
    return out


def hookup_lines(inst: str, pw: Dict[str, Any]) -> List[str]:
    """``PDN_MACRO_CONNECTIONS`` lines for one instance: one per power pin, as
    IHP's own template writes them (``VDD!`` and ``VDDARRAY!`` each to VPWR,
    with ``VSS!``). ``<inst_rx> <vdd_net> <gnd_net> <vdd_pin> <gnd_pin>``."""
    p = norm_power(pw) or {}
    if not p:
        return []
    vs, gs = p["vdd_pins"], p["gnd_pins"]
    rx = _inst_regex(inst)
    out = [f"{rx} {p['vdd_net']} {p['gnd_net']} {v} {gs[min(k, len(gs) - 1)]}" for k, v in enumerate(vs)]
    out += [f"{rx} {p['vdd_net']} {p['gnd_net']} {vs[0]} {g}" for g in gs[len(vs):]]
    return out


def pdn_connections(design_dir: str | Path) -> List[str]:
    """``PDN_MACRO_CONNECTIONS`` lines for the enabled, placed macros that have
    a power hookup."""
    out = []
    for m in _load(design_dir):
        pw = m.get("power")
        if not m.get("enabled", True) or not pw:
            continue
        for inst in m.get("instances") or []:
            if inst.get("location"):
                out.extend(hookup_lines(inst["name"], pw))
    return out


def _user_config_macros(design_dir: Path) -> Dict[str, Any]:
    """Best-effort read of any ``MACROS`` already in the user's JSON/YAML config,
    so an overlay augments rather than silently replaces it. Tcl/unreadable
    configs or a non-dict ``MACROS`` are skipped (the overlay then carries only
    GUI macros — see :func:`merge_warning` for the honest heads-up)."""
    for ext in ("json", "yaml", "yml"):
        f = design_dir / f"config.{ext}"
        if not f.is_file():
            continue
        try:
            if ext == "json":
                doc = json.loads(f.read_text(encoding="utf-8"))
            else:
                # YAML configs are first-class in LibreLane; parse with the same
                # library it uses (already a dependency) so a config.yaml MACROS
                # is MERGED rather than silently replaced by the overlay.
                import yaml  # type: ignore
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if isinstance(doc, dict) and isinstance(doc.get("MACROS"), dict):
            return dict(doc["MACROS"])
        return {}
    return {}


def merge_warning(design_dir: str | Path) -> Optional[str]:
    """A plain-language warning when enabled GUI macros will REPLACE (not merge
    with) macros in the user's own config — i.e. a Tcl config that mentions
    ``MACROS`` (we can't parse Tcl to merge it). ``None`` when there's nothing
    to warn about."""
    d = Path(design_dir).resolve()
    enabled = [m for m in _load(d) if m.get("enabled", True)]
    if not enabled:
        return None
    # LibreLane's Classic flow has no automatic macro placer (only
    # Odb.ManualMacroPlacement): an instance without a location is never
    # placed and the run stops at GeneratePDN with "Design has unplaced macros".
    unplaced = [i["name"] for m in enabled for i in (m.get("instances") or []) if not i.get("location")]
    if unplaced:
        return ("macro instance" + ("s " if len(unplaced) > 1 else " ") + ", ".join(unplaced[:4]) +
                (" have" if len(unplaced) > 1 else " has") + " no position. LibreLane places macros only "
                "where you put them, so the run will stop at the power grid (PDN-0235 unplaced macros). "
                "Set X/Y on the Cells tab (Macros → Arrange for me).")
    tcl = d / "config.tcl"
    if tcl.is_file() and not any((d / f"config.{e}").is_file() for e in ("json", "yaml", "yml")):
        try:
            if "MACROS" in tcl.read_text(encoding="utf-8", errors="replace"):
                return ("your config.tcl defines MACROS, which the GUI cannot merge — "
                        "for this run the GUI-managed macros will replace it. Move the "
                        "config's MACROS into config.json/yaml to combine both.")
        except Exception:
            return None
    return None


def build_macros_dict(design_dir: str | Path) -> Dict[str, Any]:
    """The merged ``{module_name: macro_cfg}`` for all enabled macros (GUI macros
    win on a name clash with the user's config). ``{}`` if none enabled."""
    d = Path(design_dir).resolve()
    enabled = [m for m in _load(d) if m.get("enabled", True)]
    if not enabled:
        return {}
    merged = _user_config_macros(d)
    for entry in enabled:
        merged[entry["name"]] = _macro_to_config(entry)
    return merged


def _user_config_doc(design_dir: Path) -> Dict[str, Any]:
    for ext in ("json", "yaml", "yml"):
        f = design_dir / f"config.{ext}"
        if not f.is_file():
            continue
        try:
            if ext == "json":
                doc = json.loads(f.read_text(encoding="utf-8"))
            else:
                import yaml  # type: ignore
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return doc if isinstance(doc, dict) else {}
    return {}


def overlay_extras(design_dir: str | Path, pdk: Optional[str] = None, scl: Optional[str] = None,
                   model: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """The settings besides ``MACROS`` a run with these macros needs, each found
    on a real LibreLane 3.0.4 run (research/LANE-7-research.md 6.1), and the
    PDN_CFG text when LanEx writes one. A config file later in the list
    replaces a key, so lists the user set are merged, and a setting the user
    chose is never overridden."""
    d = Path(design_dir).resolve()
    user = _user_config_doc(d)
    extra: Dict[str, Any] = {}
    hooks = pdn_connections(d)
    if hooks:
        mine = _user_config_list(d, "PDN_MACRO_CONNECTIONS")
        extra["PDN_MACRO_CONNECTIONS"] = mine + [h for h in hooks if h not in mine]
    # One keep-out per axis sets the rows' halo (cut_rows, tapcell) and the
    # power grid's (pdngen): a larger power halo leaves rows no strap reaches.
    st = _settings(d)
    if "halo_h" in st:
        extra["FP_MACRO_HORIZONTAL_HALO"] = st["halo_h"]
        extra["PDN_HORIZONTAL_HALO"] = st["halo_h"]
    if "halo_v" in st:
        extra["FP_MACRO_VERTICAL_HALO"] = st["halo_v"]
        extra["PDN_VERTICAL_HALO"] = st["halo_v"]
    enabled = [m for m in _load(d) if m.get("enabled", True)]
    # Magic's default stream-out of a design with macros leaves stray top
    # cells (KLayout.Render: "The layout has multiple top cells").
    if "MAGIC_MACRO_STD_CELL_SOURCE" not in user:
        extra["MAGIC_MACRO_STD_CELL_SOURCE"] = "PDK"
    # Magic checks around a block with its abstract view, not inside it (the
    # SRAM's bit cells alone report millions of errors).
    mags = [v for m in enabled for v in _as_list((m.get("views") or {}).get("maglef"))]
    if mags:
        mine = [str(x) for x in (user.get("MAGIC_DRC_MAGLEFS") or [])] if isinstance(user.get("MAGIC_DRC_MAGLEFS"), list) else []
        extra["MAGIC_DRC_MAGLEFS"] = mine + [m for m in mags if m not in mine]
    pdn_text = None
    if not (user.get("PDN_CFG") or user.get("FP_PDN_CFG")):
        try:
            from . import macrolib
            fp = model or macrolib.floorplan(str(d), pdk, scl)
            plans = []
            for name, g in (fp.get("macros") or {}).items():
                if g.get("grid") != "lanex" or not g.get("plan"):
                    continue
                insts = [i["name"] for i in fp.get("instances") or [] if i["macro"] == name and i.get("location")]
                if insts:
                    plans.append((name, insts, g["plan"]))
            if plans:
                pdn_text = macrolib.pdn_cfg_text(plans)
                extra["PDN_CFG"] = "dir::" + _PDN_FILE
        except Exception:
            pdn_text = None
    return extra, pdn_text


def _as_list(v: Any) -> List[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def write_overlay(design_dir: str | Path, pdk: Optional[str] = None, scl: Optional[str] = None) -> Optional[str]:
    """Write the ``MACROS`` overlay config and return its path, or ``None`` when
    there are no enabled macros (also removing any stale overlay then). The path
    is handed to the flow as an extra config file. Per-run; never touches
    ``config.json``."""
    d = Path(design_dir).resolve()
    overlay = d / _OVERLAY
    pdn_file = d / _PDN_FILE
    try:
        ensure_generated(d, pdk, scl)
    except Exception:
        pass
    macros = build_macros_dict(d)
    if not macros:
        for f in (overlay, pdn_file):
            try:
                f.unlink()
            except OSError:
                pass
        return None
    extra, pdn_text = overlay_extras(d, pdk, scl)
    try:
        from . import platform_env
        if pdn_text:
            platform_env.atomic_write_text(pdn_file, pdn_text)
        else:
            try:
                pdn_file.unlink()
            except OSError:
                pass
        platform_env.atomic_write_text(overlay, json.dumps(dict({"MACROS": macros}, **extra), indent=2) + "\n")
    except Exception:
        return None
    return str(overlay)


# --------------------------------------------------------------------------- #
# Views LanEx makes for a block: a ports-only header, a Liberty copy a run
# can meet, each PDK corner's timing file
# --------------------------------------------------------------------------- #
_BEHAVIOURAL_RX = re.compile(r"\b(always|assign|initial|reg)\b")


def _write_generated(macro_dir: Path, fname: str, text: str) -> str:
    from . import platform_env
    macro_dir.mkdir(parents=True, exist_ok=True)
    target = macro_dir / fname
    if target.is_symlink():
        target.unlink()
    platform_env.atomic_write_text(target, text)
    return target.name


def _relaxed_copy(macro_dir: Path, src: Path) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """A copy of *src* with too-tight input limits relaxed, in *macro_dir*,
    when it needs one: ``(file name, changes)``, or ``(None, [])``."""
    from . import macrolib
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, []
    new, changes = macrolib.relax_liberty(text)
    if not changes:
        return None, []
    name = src.name if src.parent.resolve() != macro_dir.resolve() else src.stem + ".lanex.lib"
    _write_generated(macro_dir, name, new)
    return name, changes


def prepare_views(design_dir: str | Path, name: str, views: Dict[str, Any], *,
                  libs: Optional[List[Path]] = None, pdk: Optional[str] = None,
                  scl: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The views LibreLane gets for a block, and a record of what LanEx made:

    * ``vh``: a ports-only black-box header from the LEF, unless the block
      came with one (a behavioural model becomes ``model``, for simulation);
    * ``lib``: each run corner's timing file (``*_<corner>`` keys, or ``"*"``),
      a relaxed copy where the file's input limits are under 0.1 ns.

    *views* are the saved ``dir::``/``pdk_dir::`` paths; *libs* the timing
    files to key per corner (absolute; a PDK block ships one per corner)."""
    from . import macrolib
    d = Path(design_dir).resolve()
    macro_dir = _macro_dir(d, name)
    if macro_dir is None:
        return views, {}
    out = dict(views)
    gen: Dict[str, Any] = {}
    lef = macrolib.resolve_view(out.get("lef") if isinstance(out.get("lef"), str) else "", str(d), pdk)
    # the header
    vh = out.get("vh")
    vh_file = macrolib.resolve_view(vh if isinstance(vh, str) else "", str(d), pdk) if vh else None
    if vh_file is not None and vh_file.is_file():
        try:
            if _BEHAVIOURAL_RX.search(vh_file.read_text(encoding="utf-8", errors="replace")):
                out["model"] = vh
                vh = None
        except OSError:
            pass
    if not vh and not out.get("nl") and not out.get("pnl") and lef is not None and lef.is_file():
        g = macrolib.macro_geometry(lef, name)
        if g.get("ok"):
            fname = _write_generated(macro_dir, f"{name}.bb.v", macrolib.blackbox_header(name, g["raw_pins"]))
            out["vh"] = "dir::" + str((macro_dir / fname).relative_to(d)).replace("\\", "/")
            gen["vh"] = {"from": "lef", "file": out["vh"]}
    # timing, per corner, relaxed where it must be
    if libs is None:
        cur = out.get("lib")
        if isinstance(cur, str):
            p = macrolib.resolve_view(cur, str(d), pdk)
            libs = [p] if p is not None and p.is_file() else []
    if libs:
        suffixes = macrolib.corner_suffixes(pdk, scl) if pdk and scl else []
        cmap, notes = macrolib.lib_corner_map(libs, suffixes)
        lib_views: Dict[str, str] = {}
        relaxed: List[Dict[str, Any]] = []
        done: Dict[str, str] = {}
        for key, src in cmap.items():
            if str(src) in done:
                lib_views[key] = done[str(src)]
                continue
            rname, changes = _relaxed_copy(macro_dir, src)
            if rname:
                ref = "dir::" + str((macro_dir / rname).relative_to(d)).replace("\\", "/")
                relaxed.append({"file": ref, "from": src.name, "changes": changes})
            else:
                ref = _ref_for(src, d, pdk)
            done[str(src)] = ref
            lib_views[key] = ref
        out["lib"] = lib_views if not (len(lib_views) == 1 and "*" in lib_views) else {"*": lib_views["*"]}
        gen["lib"] = {"corners": sorted(lib_views), "notes": notes, "relaxed": relaxed}
    return out, gen


def _ref_for(p: Path, d: Path, pdk: Optional[str]) -> str:
    try:
        return "dir::" + str(p.resolve().relative_to(d)).replace("\\", "/")
    except ValueError:
        pass
    from . import celllib
    pd = celllib.pdk_dir(pdk or "")
    if pd is not None:
        try:
            return "pdk_dir::" + str(p.resolve().relative_to(pd.resolve())).replace("\\", "/")
        except ValueError:
            pass
    return str(p)


def add_pdk_macro(design_dir: str | Path, pdk: str, scl: Optional[str], name: str,
                  library: Optional[str] = None, *, power: Optional[Dict[str, Any]] = None,
                  instances: Optional[List[Dict[str, Any]]] = None, enabled: bool = True) -> Dict[str, Any]:
    """Add one of the PDK's own blocks (an SRAM) by name: its GDS, LEF,
    SPICE/CDL and Magic abstract referenced in place (``pdk_dir::``), a
    ports-only header and per-corner timing (a relaxed copy where needed)
    made in ``macros/<name>/``, and the power hookup from its LEF."""
    from . import macrolib
    found = macrolib.pdk_macro_files(pdk, name, library)
    if not found.get("ok"):
        return {"ok": False, "error": found.get("error") or f"{name} is not in {pdk}"}
    f = found["files"]
    d = Path(design_dir).resolve()
    if _macro_dir(d, name) is None:
        return {"ok": False, "error": "macro module name must be a valid Verilog identifier"}
    pd = found["pdk_dir"]
    rel = lambda p: "pdk_dir::" + str(p.relative_to(pd)).replace("\\", "/")
    views: Dict[str, Any] = {"gds": rel(f["gds"]), "lef": rel(f["lef"])}
    for k in ("spice", "maglef", "model"):
        if f.get(k):
            views[k] = rel(f[k])
    views, gen = prepare_views(d, name, views, libs=f.get("libs") or [], pdk=pdk, scl=scl)
    if power is None:
        g = macrolib.macro_geometry(f["lef"], name)
        power = default_power(d, pdk, scl, g.get("power") or {}) if g.get("ok") else None
    label = f"{found['library']} ({pdk})"
    return save_macro(d, name, instances=instances or [], refs=views, power=power, enabled=enabled,
                      source={"kind": "pdk", "label": label, "pdk": pdk, "library": found["library"]},
                      generated=gen)


def default_power(design_dir: Path, pdk: Optional[str], scl: Optional[str],
                  pins: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    """Every POWER pin of the block to the design's power net, every GROUND
    pin to its ground net. Nets from the config's VDD_NETS/GND_NETS, else the
    PDK's VDD_PIN/GND_PIN (VPWR/VGND on sky130 and IHP, VDD/VSS on gf180)."""
    vdd, gnd = list(pins.get("vdd") or []), list(pins.get("gnd") or [])
    if not vdd or not gnd:
        return None
    nets = design_nets(design_dir, pdk, scl)
    return {"vdd_net": nets["vdd"], "gnd_net": nets["gnd"], "vdd_pins": vdd, "gnd_pins": gnd}


def design_nets(design_dir: Path, pdk: Optional[str], scl: Optional[str]) -> Dict[str, str]:
    from . import celllib, macrolib
    doc = _user_config_doc(Path(design_dir))

    def first(v: Any) -> Optional[str]:
        if isinstance(v, list):
            return str(v[0]) if v else None
        if isinstance(v, str) and v.strip():
            return v.split()[0]
        return None
    scl = scl or (macrolib._default_scl(pdk) if pdk else None)
    pcfg = celllib.pdk_config(pdk, scl) if pdk and scl else {}
    return {"vdd": first(doc.get("VDD_NETS")) or pcfg.get("VDD_PIN") or "VPWR",
            "gnd": first(doc.get("GND_NETS")) or pcfg.get("GND_PIN") or "VGND"}


def ensure_generated(design_dir: str | Path, pdk: Optional[str] = None, scl: Optional[str] = None) -> None:
    """Make again any file LanEx generated for a macro that is gone (a project
    copied without its macros/ folder, or a template): the header and the
    relaxed Liberty copies."""
    d = Path(design_dir).resolve()
    macros = _load(d)
    changed = False
    for m in macros:
        views = m.get("views") or {}
        missing = False
        for k in ("vh", "lib"):
            v = views.get(k)
            for p in (v.values() if isinstance(v, dict) else [v] if v else []):
                if isinstance(p, str) and p.startswith("dir::") and not (d / p[5:]).is_file():
                    missing = True
        if not missing:
            continue
        src = m.get("source") or {}
        if src.get("kind") == "pdk":
            res = add_pdk_macro(d, src.get("pdk") or pdk or "", scl, m["name"], src.get("library"),
                                power=m.get("power") or {}, instances=m.get("instances") or [],
                                enabled=m.get("enabled", True))
            changed = changed or bool(res.get("ok"))
    if changed:
        return


def finish_upload(design_dir: str | Path, name: str, pdk: Optional[str], scl: Optional[str]) -> Dict[str, Any]:
    """After an upload or a copy from another project: the generated header
    and relaxed Liberty for a saved macro, and its power hookup from the LEF
    when it has none yet."""
    from . import macrolib
    d = Path(design_dir).resolve()
    m = next((x for x in _load(d) if x.get("name") == name), None)
    if m is None:
        return {"ok": False, "error": "macro not found"}
    views, gen = prepare_views(d, name, dict(m.get("views") or {}), pdk=pdk, scl=scl)
    power = m.get("power")
    if not power:
        lef = macrolib.resolve_view(views.get("lef") or "", str(d), pdk)
        g = macrolib.macro_geometry(lef, name)
        power = default_power(d, pdk, scl, g.get("power") or {}) if g.get("ok") else None
    macros = [x for x in _load(d) if x.get("name") != name]
    m = dict(m, views=views)
    if gen:
        m["generated"] = dict(m.get("generated") or {}, **gen)
    if power:
        m["power"] = norm_power(power) or m.get("power")
    macros.append(m)
    _save(d, macros)
    return {"ok": True, "macro": m}


def set_instances(design_dir: str | Path, placements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Save every instance position at once (the Arrange canvas's Save):
    ``[{macro, name, location, orientation}]``. All or nothing."""
    d = Path(design_dir).resolve()
    macros = _load(d)
    by: Dict[str, List[Dict[str, Any]]] = {}
    for p in placements or []:
        by.setdefault(str(p.get("macro") or ""), []).append(p)
    unknown = [k for k in by if not any(m.get("name") == k for m in macros)]
    if unknown:
        return {"ok": False, "error": "no macro named " + ", ".join(unknown)}
    for m in macros:
        if m["name"] not in by:
            continue
        prev = {i["name"]: i for i in m.get("instances") or []}
        merged = []
        for p in by[m["name"]]:
            merged.append(dict(prev.get(str(p.get("name")), {}), name=p.get("name"), location=p.get("location"),
                               orientation=p.get("orientation") or "N"))
        names = {x["name"] for x in merged}
        merged += [i for i in m.get("instances") or [] if i["name"] not in names]
        clean, err = _clean_instances(merged)
        if err:
            return {"ok": False, "error": err}
        m["instances"] = clean
    _save(d, macros)
    return {"ok": True}


def run_problems(design_dir: str | Path, pdk: Optional[str] = None, scl: Optional[str] = None) -> List[Dict[str, Any]]:
    """The macro findings a run check shows, in the config check's shape
    (``{code, message, fix, hard}``): hard ones refuse the start (no position,
    outside the core, overlapping, unreachable by the power grid), the rest
    ask first. Empty without enabled macros."""
    d = Path(design_dir).resolve()
    if not any(m.get("enabled", True) for m in _load(d)):
        return []
    from . import macrolib
    try:
        fp = macrolib.floorplan(str(d), pdk, scl)
    except Exception as ex:
        return [{"code": "macro", "message": f"LanEx couldn't check the macros' positions: {ex}", "fix": "", "hard": False}]
    if not fp.get("ok"):
        return [{"code": "macro", "message": fp.get("error") or "LanEx couldn't check the macros.", "fix": "", "hard": False}]
    out = []
    if not (fp.get("core") or {}).get("box"):
        out.append({"code": "macro", "hard": False,
                    "message": "The die is sized by the flow (FP_SIZING relative), so LanEx can't check the macro "
                               "positions against the core before the run.",
                    "fix": "Use a fixed die (Cells & macros, Macros) so the positions can be checked."})
    for p in fp.get("problems") or []:
        if p["code"] == "outside" and fp["die"].get("from") == "run":
            p = dict(p, hard=False)
        out.append({"code": "macro", "hard": bool(p["hard"]), "message": p["message"], "fix": p.get("fix") or ""})
    for name, g in (fp.get("macros") or {}).items():
        if not g.get("ok"):
            out.append({"code": "macro", "hard": True, "message": f"Macro {name}: {g.get('error')}",
                        "fix": "Fix its files on the Cells & macros tab."})
        elif not any(i["macro"] == name for i in fp.get("instances") or []):
            out.append({"code": "macro", "hard": True,
                        "message": f"Macro {name} is switched on but lists no instance, so LibreLane wouldn't "
                                   "place it; if your RTL uses it, the run stops at the power grid (PDN-0235).",
                        "fix": "Add its instance (the name in your RTL) on the Cells & macros tab, or switch the macro off."})
    return out
