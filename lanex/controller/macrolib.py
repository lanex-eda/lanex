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
"""Where hard macros come from, and whether one is wired up right.

Three sources, matching how people actually get a macro:

* **The PDK.** sky130, gf180mcu and IHP all ship ready SRAM blocks under
  ``libs.ref`` (``sky130_sram_macros``, ``gf180mcu_fd_ip_sram``, ``sg13g2_sram``).
  They are referenced in place with ``pdk_dir::`` paths, so nothing is copied
  and the config stays portable.
* **Another LanEx project.** The hierarchical flow: harden a block on its own,
  then drop its finished run (``final/gds``, ``lef``, ``nl``, ``spef``, ``lib``)
  into the parent. The views are copied, so deleting the old run can't break
  the parent.
* **Files** someone hands you (uploaded through :mod:`custommacros`).

And the checks that catch the usual first-macro failures before a 20-minute
run does: the module is never instantiated, the instance name is wrong (the
flattened path is ``u_core.u_sram``), the behavioural model is also in the
design's sources (so synthesis builds the SRAM out of flip-flops), or the
macro's power pins are not hooked to the grid.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import celllib

_SKIP_DIR = re.compile(r"(_sc_|stdcell|_pr$|_pr_|_io$|_io_|_ef_io|_osu_|_ml_)", re.I)


# --------------------------------------------------------------------------- #
# LEF pins -> buses, power, instantiation template
# --------------------------------------------------------------------------- #
_BIT_RX = re.compile(r"^(.*?)\[(\d+)\]$")


def group_pins(pins: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse ``din0[0]..din0[31]`` into one bus ``din0[31:0]``. Keeps LEF order."""
    out: List[Dict[str, Any]] = []
    buses: Dict[str, Dict[str, Any]] = {}
    for p in pins:
        m = _BIT_RX.match(p["name"])
        if m:
            base, bit = m.group(1), int(m.group(2))
            b = buses.get(base)
            if b is None:
                b = buses[base] = {"name": base, "dir": p.get("dir") or "", "use": p.get("use") or "signal",
                                   "lo": bit, "hi": bit, "bus": True}
                out.append(b)
            b["lo"], b["hi"] = min(b["lo"], bit), max(b["hi"], bit)
        else:
            out.append({"name": p["name"], "dir": p.get("dir") or "", "use": p.get("use") or "signal",
                        "bus": False})
    for b in out:
        b["width"] = (b["hi"] - b["lo"] + 1) if b["bus"] else 1
    return out


def power_pins(pins: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    pw = [p["name"] for p in pins if (p.get("use") or "") == "power"]
    gd = [p["name"] for p in pins if (p.get("use") or "") == "ground"]
    return pw, gd


def instantiation(module: str, inst: str, pins: List[Dict[str, Any]]) -> str:
    """A ready-to-paste instance with power pins behind ``USE_POWER_PINS`` (what
    LibreLane's docs ask for) and each signal commented with its direction."""
    grouped = group_pins(pins)
    pw = [p for p in grouped if p["use"] in ("power", "ground")]
    sig = [p for p in grouped if p["use"] not in ("power", "ground")]
    w = max([len(p["name"]) for p in grouped] + [4])
    lines = [f"{module} {inst} ("]
    if pw:
        lines.append("`ifdef USE_POWER_PINS")
        for p in pw:
            lines.append(f"    .{p['name'].ljust(w)} ({p['name']}),")
        lines.append("`endif")
    for k, p in enumerate(sig):
        rng = f" [{p['hi']}:{p['lo']}]" if p["bus"] else ""
        d = (p["dir"] or "inout").replace("input", "input ").replace("output", "output")
        comma = "," if k < len(sig) - 1 else ""
        lines.append(f"    .{p['name'].ljust(w)} (){comma}  // {d.strip()}{rng}")
    if not sig and pw:
        lines[-2] = lines[-2].rstrip(",")
    lines.append(");")
    return "\n".join(lines)


def _capacity(name: str, grouped: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Words x bits of a memory, from its address and data buses (falls back to
    the ``NxM`` in its name)."""
    sig = [p for p in grouped if p["use"] not in ("power", "ground")]
    addr = [p for p in sig if p["bus"] and re.search(r"addr|^a$|_a$|^adr|_addr", p["name"], re.I) and p["dir"] == "input"]
    data = [p for p in sig if p["bus"] and re.search(r"dout|^q$|_q$|dout0|rdata|_dout", p["name"], re.I)]
    words = 2 ** addr[0]["width"] if addr else None
    bits = data[0]["width"] if data else None
    m = re.search(r"(\d+)x(\d+)", name)
    if (words is None or bits is None) and m:
        a, b = int(m.group(1)), int(m.group(2))
        words, bits = (max(a, b), min(a, b)) if words is None and bits is None else (words or a, bits or b)
    ports = None
    pm = re.search(r"(\d+rw\d*r?|\dP)(?:_|$)", name, re.I)
    if pm:
        ports = pm.group(1)
    kbits = (words * bits / 1024) if words and bits else None
    return {"words": words, "bits": bits, "kbits": kbits, "ports": ports}


# --------------------------------------------------------------------------- #
# PDK catalog
# --------------------------------------------------------------------------- #
def _view_files(lib_dir: Path, stem: str) -> Dict[str, Any]:
    """The files of one macro inside a PDK ``libs.ref/<ip>`` dir."""
    def find(sub: str, exts: Tuple[str, ...]) -> List[Path]:
        d = lib_dir / sub
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            if p.is_file() and p.name.lower().endswith(exts) and (p.name.startswith(stem + ".") or p.name.startswith(stem + "_") or p.stem == stem):
                out.append(p)
        return out
    views: Dict[str, Any] = {}
    gds = find("gds", (".gds", ".gds.gz"))
    if gds:
        views["gds"] = gds[0]
    lef = find("lef", (".lef",))
    if lef:
        views["lef"] = lef[0]
    libs = find("lib", (".lib",))
    if libs:
        views["libs"] = libs
    vh = find("verilog", (".v",))
    if vh:
        views["vh"] = vh[0]
    sp = find("spice", (".spice", ".sp", ".cir")) or find("cdl", (".cdl",))
    if sp:
        views["spice"] = sp[0]
    mag = find("maglef", (".mag",))
    if mag:
        views["maglef"] = mag[0]
    return views


def pdk_macro_files(pdk: str, name: str, library: Optional[str] = None) -> Dict[str, Any]:
    """The installed files of one PDK block, found by its module name (and its
    ``libs.ref`` folder when given): ``{ok, files: {gds, lef, libs, spice,
    maglef, model}, pdk_dir, library}``."""
    d = celllib.pdk_dir(pdk or "")
    if d is None:
        return {"ok": False, "error": f"{pdk or 'This PDK'} is not installed."}
    if not valid_ident(name) or (library and not re.fullmatch(r"[A-Za-z0-9_.\-]+", library)):
        return {"ok": False, "error": "not a block name"}
    for lib_dir in sorted((d / "libs.ref").iterdir()):
        if not lib_dir.is_dir() or (library and lib_dir.name != library) or _SKIP_DIR.search(lib_dir.name):
            continue
        lef = lib_dir / "lef" / f"{name}.lef"
        if not lef.is_file():
            continue
        v = _view_files(lib_dir, name)
        if "gds" not in v:
            continue
        files = {"gds": v["gds"], "lef": lef, "libs": v.get("libs") or []}
        for k in ("spice", "maglef"):
            if k in v:
                files[k] = v[k]
        if "vh" in v:
            files["model"] = v["vh"]
        return {"ok": True, "files": files, "pdk_dir": d, "library": lib_dir.name}
    return {"ok": False, "error": f"{name} is not one of {pdk}'s blocks."}


def valid_ident(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name) <= 128 and bool(re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name))


def _typical_lib(libs: List[Path], scl_lib: Optional[str]) -> Optional[Path]:
    """The macro lib closest to the SCL's typical corner (TT, nominal voltage)."""
    if not libs:
        return None
    volt = None
    if scl_lib:
        m = re.search(r"(\d)v(\d+)", scl_lib)
        if m:
            volt = (m.group(1), m.group(2)[:1])
    def score(p: Path) -> Tuple[int, int, int]:
        n = p.name.lower()
        tt = 0 if re.search(r"(^|_)(tt|typ)", n) else 1
        v = 1
        if volt:
            if re.search(volt[0] + r"[pv]" + volt[1], n):
                v = 0
        t = 0 if re.search(r"(25c|025c|_25_)", n) else 1
        return (tt, v, t)
    return sorted(libs, key=score)[0]


def catalog(pdk: str, scl: Optional[str] = None) -> Dict[str, Any]:
    """Hard macros the installed *pdk* ships (SRAMs, mostly), with size and
    capacity so a beginner can pick by "how much memory"."""
    try:
        d = celllib.pdk_dir(pdk)
        if d is None:
            return {"ok": False, "error": f"{pdk or 'This PDK'} is not installed.", "macros": []}
        scl_lib = None
        if scl:
            lp = celllib._main_lib(pdk, scl)
            scl_lib = lp.name if lp else None
        out: List[Dict[str, Any]] = []
        for lib_dir in sorted((d / "libs.ref").iterdir()):
            if not lib_dir.is_dir() or _SKIP_DIR.search(lib_dir.name):
                continue
            lef_dir = lib_dir / "lef"
            if not lef_dir.is_dir():
                continue
            for lef in sorted(lef_dir.glob("*.lef")):
                try:
                    parsed = celllib.parse_lef(lef.read_text(encoding="utf-8", errors="replace"), shapes=False)
                except OSError:
                    continue
                for name, m in parsed["macros"].items():
                    if not str(m.get("class", "")).upper().startswith("BLOCK"):
                        continue
                    views = _view_files(lib_dir, lef.stem)
                    if "gds" not in views:
                        continue
                    grouped = group_pins(m["pins"])
                    pw, gd = power_pins(m["pins"])
                    lib = _typical_lib(views.get("libs", []), scl_lib)
                    rel = lambda p: "pdk_dir::" + str(p.relative_to(d)).replace("\\", "/")
                    out.append({
                        "name": name, "library": lib_dir.name,
                        "w": m.get("w"), "h": m.get("h"),
                        "capacity": _capacity(name, grouped),
                        "pins": len([p for p in grouped if p["use"] not in ("power", "ground")]),
                        "power": {"vdd": pw, "gnd": gd},
                        "views": {
                            "gds": rel(views["gds"]), "lef": rel(views["lef"]) if "lef" in views else rel(lef),
                            **({"lib": rel(lib)} if lib else {}),
                            **({"vh": rel(views["vh"])} if "vh" in views else {}),
                            **({"spice": rel(views["spice"])} if "spice" in views else {}),
                        },
                        "lib_corner": lib.stem if lib else None,
                        "corners": len(views.get("libs", [])),
                        "lef_bytes": lef.stat().st_size,
                        "maglef": "maglef" in views,
                    })
        out.sort(key=lambda x: (x["library"], (x["capacity"].get("kbits") or 0), x["name"]))
        return {"ok": True, "pdk": pdk, "macros": out}
    except Exception as ex:
        return {"ok": False, "error": f"could not read the PDK's macros: {ex}", "macros": []}


def resolve_view(path: str, design_dir: Optional[str], pdk: Optional[str]) -> Optional[Path]:
    """A ``dir::`` / ``pdk_dir::`` / absolute view path → a file on disk."""
    if not path:
        return None
    if path.startswith("dir::"):
        return Path(design_dir or ".") / path[5:] if design_dir else None
    if path.startswith("pdk_dir::"):
        d = celllib.pdk_dir(pdk or "")
        return d / path[9:] if d else None
    return Path(path)


def macro_pins(lef_path: Path, name: str) -> Dict[str, Any]:
    """Pins (bus-collapsed), power pins, size and an instantiation template for
    *name* as defined in *lef_path*."""
    try:
        parsed = celllib.parse_lef(lef_path.read_text(encoding="utf-8", errors="replace"), shapes=True)
    except OSError as ex:
        return {"ok": False, "error": str(ex)}
    m = parsed["macros"].get(name)
    if m is None:
        names = list(parsed["macros"])
        if len(names) == 1:
            m = parsed["macros"][names[0]]
            name = names[0]
        else:
            return {"ok": False, "error": f"{lef_path.name} has no MACRO {name}", "macros": names}
    pw, gd = power_pins(m["pins"])
    # pin centres along the outline, for the floorplan sketch
    marks = []
    for p in m["pins"]:
        if not p["rects"]:
            continue
        r = max(p["rects"], key=lambda r: (r[3] - r[1]) * (r[4] - r[2]))
        marks.append({"name": p["name"], "use": p.get("use"), "x": (r[1] + r[3]) / 2, "y": (r[2] + r[4]) / 2})
    return {"ok": True, "name": name, "w": m.get("w"), "h": m.get("h"), "class": m.get("class"),
            "pins": group_pins(m["pins"]), "power": {"vdd": pw, "gnd": gd},
            "marks": marks[:400], "template": instantiation(name, "u_" + _short(name), m["pins"])}


def _short(name: str) -> str:
    n = name.lower()
    if "sram" in n or "ram" in n:
        return "sram"
    parts = [p for p in re.split(r"_+", n) if p]
    return parts[-1] if parts and not parts[-1].isdigit() else (parts[0] if parts else "macro")


# --------------------------------------------------------------------------- #
# Another LanEx project's run as a macro
# --------------------------------------------------------------------------- #
def _final_views(run_dir: Path) -> Dict[str, Any]:
    f = run_dir / "final"
    if not f.is_dir():
        return {}

    def first(sub: str, pats: Tuple[str, ...]) -> Optional[Path]:
        d = f / sub
        if not d.is_dir():
            return None
        for pat in pats:
            got = sorted(d.glob(pat))
            if got:
                return got[0]
        return None

    def cornered(sub: str, pat: str) -> Dict[str, Path]:
        d = f / sub
        out: Dict[str, Path] = {}
        if not d.is_dir():
            return out
        for c in sorted(d.iterdir()):
            if c.is_dir():
                got = sorted(c.glob(pat))
                if got:
                    out[c.name] = got[0]
            elif c.is_file() and c.name.endswith(pat.lstrip("*")):
                out["*"] = c
        return out
    v: Dict[str, Any] = {}
    g = first("gds", ("*.gds", "*.gds.gz")) or first("klayout_gds", ("*.gds",))
    if g:
        v["gds"] = g
    lef = first("lef", ("*.lef",))
    if lef:
        v["lef"] = lef
    nl = first("nl", ("*.nl.v", "*.v"))
    if nl:
        v["nl"] = nl
    pnl = first("pnl", ("*.pnl.v", "*.v"))
    if pnl:
        v["pnl"] = pnl
    spef = cornered("spef", "*.spef")
    if spef:
        v["spef"] = spef
    lib = cornered("lib", "*.lib")
    if lib:
        v["lib"] = lib
    return v


def project_sources(exclude_dir: Optional[str] = None) -> Dict[str, Any]:
    """Every known project with a finished run that produced GDS + LEF — the
    blocks that can be dropped into another design as a macro."""
    try:
        from . import designs
    except Exception as ex:
        return {"ok": False, "error": str(ex), "projects": []}
    ex_res = None
    try:
        ex_res = str(Path(exclude_dir).resolve()) if exclude_dir else None
    except OSError:
        ex_res = None
    out = []
    for d in designs.list_designs():
        try:
            dres = str(Path(d).resolve())
        except OSError:
            continue
        if ex_res and dres == ex_res:
            continue
        # A lean walk, not history.list_runs: summarising every run (metrics,
        # step dirs) takes seconds per project on a Windows-mounted folder.
        try:
            run_dirs = sorted((p for p in (Path(d) / "runs").iterdir() if p.is_dir() and not p.name.startswith(".")),
                              key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            run_dirs = []
        usable = []
        for rd in run_dirs:
            if len(usable) >= 8:
                break
            v = _final_views(rd)
            if "gds" not in v or "lef" not in v:
                continue
            meta = _run_meta(rd)
            usable.append({
                "tag": rd.name, "success": (rd / "final" / "metrics.json").is_file(),
                "pdk": meta.get("PDK"), "scl": meta.get("STD_CELL_LIBRARY"),
                "when": _mtime_iso(rd),
                "views": sorted(v.keys()),
                "module": meta.get("DESIGN_NAME") or Path(v["lef"]).stem,
            })
        if usable:
            out.append({"design_dir": d, "name": Path(d).name, "runs": usable[:8]})
    return {"ok": True, "projects": out}


def _run_meta(run_dir: Path) -> Dict[str, Any]:
    import json
    for f in (run_dir / "resolved.json", run_dir / "config.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return {k: doc.get(k) for k in ("DESIGN_NAME", "PDK", "STD_CELL_LIBRARY") if isinstance(doc.get(k), str)}
        except Exception:
            continue
    return {}


def _mtime_iso(p: Path) -> Optional[str]:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def _design_name(run_dir: Path) -> Optional[str]:
    import json
    for f in (run_dir / "resolved.json", run_dir / "config.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("DESIGN_NAME"), str):
                return doc["DESIGN_NAME"]
        except Exception:
            continue
    return None


def run_views(design_dir: str, tag: str) -> Dict[str, Any]:
    """Absolute files of one run's final views (for the copy into the parent)."""
    if not tag or "/" in tag or "\\" in tag or tag.startswith("."):
        return {"ok": False, "error": "run not found"}
    rd = Path(design_dir) / "runs" / tag
    try:
        from . import history
        for r in history.list_runs(design_dir):
            if r.get("tag") == tag and r.get("run_dir"):
                rd = Path(r["run_dir"])
                break
    except Exception:
        pass
    if not rd.is_dir():
        return {"ok": False, "error": "run not found"}
    v = _final_views(rd)
    if "gds" not in v or "lef" not in v:
        return {"ok": False, "error": "that run has no final GDS and LEF (it may have stopped early)"}
    return {"ok": True, "module": _design_name(rd) or Path(v["lef"]).stem, "views": v, "run_dir": str(rd)}


# --------------------------------------------------------------------------- #
# RTL: is the macro instantiated, and under which flattened name?
# --------------------------------------------------------------------------- #
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_][\w$]*)")
_KEYWORDS = {"module", "endmodule", "input", "output", "inout", "wire", "reg", "logic", "assign",
             "always", "always_ff", "always_comb", "always_latch", "initial", "if", "else", "for",
             "case", "begin", "end", "function", "task", "generate", "genvar", "parameter",
             "localparam", "integer", "typedef", "struct", "enum", "return", "posedge", "negedge",
             "or", "and", "not", "default", "import", "export", "interface", "modport", "bit", "int"}


def _modules(files: List[str]) -> Dict[str, Dict[str, Any]]:
    mods: Dict[str, Dict[str, Any]] = {}
    for f in files:
        try:
            t = _COMMENT.sub(" ", Path(f).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for m in _MODULE.finditer(t):
            end = t.find("endmodule", m.end())
            body = t[m.end(): end if end != -1 else len(t)]
            mods.setdefault(m.group(1), {"file": f, "body": body})
    return mods


def _instances(body: str, wanted: set) -> List[Tuple[str, str]]:
    """``(module, instance)`` pairs in a module body, for modules in *wanted*."""
    out = []
    rx = re.compile(r"\b([A-Za-z_][\w$]*)\s*(#\s*\()?")
    i = 0
    while True:
        m = rx.search(body, i)
        if not m:
            break
        i = m.end()
        mod = m.group(1)
        if mod in _KEYWORDS or mod not in wanted:
            continue
        j = m.end()
        if m.group(2):                       # skip #( ... )
            depth = 1
            while j < len(body) and depth:
                if body[j] == "(":
                    depth += 1
                elif body[j] == ")":
                    depth -= 1
                j += 1
        im = re.match(r"\s*([A-Za-z_][\w$]*)\s*(\[[^\]]*\])?\s*\(", body[j:])
        if im:
            out.append((mod, im.group(1) + (im.group(2) or "").replace(" ", "")))
            i = j + im.end()
    return out


def find_instances(design_dir: str, top: Optional[str], macro_modules: List[str],
                   files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Flattened instance paths of each macro module under *top*, and whether a
    macro's module is *defined* in the design's own sources (its behavioural
    model sneaking into synthesis). *files*: the sources to read (a run's
    resolved VERILOG_FILES) instead of the project config's."""
    if files is not None:
        files = [str(f) for f in files if Path(str(f)).suffix.lower() in (".v", ".sv", ".vh", ".svh")]
    else:
        try:
            from . import configedit
            info = configedit.read_config(design_dir)
            data = info.get("data") or {}
            top = top or data.get("DESIGN_NAME")
            src = configedit.resolve_sources(design_dir, data)
            files = [f for f in src.get("files", []) if Path(f).suffix.lower() in (".v", ".sv", ".vh", ".svh")]
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
    mods = _modules(files)
    wanted = set(mods) | set(macro_modules)
    found: Dict[str, List[str]] = {m: [] for m in macro_modules}
    seen_paths = 0

    def walk(mod: str, prefix: str, depth: int) -> None:
        nonlocal seen_paths
        if depth > 32 or seen_paths > 5000 or mod not in mods:
            return
        for sub, inst in _instances(mods[mod]["body"], wanted):
            seen_paths += 1
            path = prefix + inst
            if sub in found:
                found[sub].append(path)
            elif sub in mods:
                walk(sub, path + ".", depth + 1)
    if top:
        walk(top, "", 0)
    defined = {m: mods[m]["file"] for m in macro_modules if m in mods}
    return {"ok": True, "top": top, "top_found": bool(top and top in mods), "instances": found,
            "top_file": mods[top]["file"] if top and top in mods else None,
            "defined_in_sources": defined, "files": len(files)}


# --------------------------------------------------------------------------- #
# A macro's own geometry, from its LEF
# --------------------------------------------------------------------------- #
ROTATED = ("E", "W", "FE", "FW")
ALL_ORIENTS = ("N", "S", "FN", "FS", "W", "E", "FW", "FE")


def _geom_of(m: Dict[str, Any]) -> Dict[str, Any]:
    pw, gd = power_pins(m["pins"])
    prects: List[List[Any]] = []
    marks: List[List[float]] = []
    sig_layers: set = set()
    for p in m["pins"]:
        use = p.get("use") or "signal"
        if use in ("power", "ground"):
            for r in p["rects"]:
                prects.append([p["name"], use, r[0], r[1], r[2], r[3], r[4]])
        elif p["rects"]:
            r = max(p["rects"], key=lambda r: (r[3] - r[1]) * (r[4] - r[2]))
            marks.append([round((r[1] + r[3]) / 2, 3), round((r[2] + r[4]) / 2, 3)])
            sig_layers.update(x[0] for x in p["rects"])
    return {"ok": True, "name": m["name"], "w": m.get("w"), "h": m.get("h"),
            "class": m.get("class") or "", "symmetry": m.get("symmetry") or "",
            "power": {"vdd": pw, "gnd": gd}, "prects": prects[:3000], "pins": marks[:600],
            "n_pins": len([p for p in m["pins"] if (p.get("use") or "signal") not in ("power", "ground")]),
            "pin_layers": sorted(sig_layers), "obs_layers": list(m.get("obs_layers") or []),
            "power_layers": sorted({r[2] for r in prects}), "raw_pins": [
                {"name": p["name"], "dir": p.get("dir") or "", "use": p.get("use") or "signal"} for p in m["pins"]]}


def macro_geometry(lef_path: Optional[Path], name: Optional[str] = None) -> Dict[str, Any]:
    """Size, symmetry, power-pin shapes, signal-pin marks and the layers its
    blockages cover, for *name* in *lef_path* (the only block when *name* is
    not in the file). Parsed once per file version."""
    if lef_path is None or not Path(lef_path).is_file():
        return {"ok": False, "error": "its LEF file is not on disk"}
    lef_path = Path(lef_path)

    def build() -> Dict[str, Any]:
        try:
            parsed = celllib.parse_lef(lef_path.read_text(encoding="utf-8", errors="replace"),
                                       shapes=True, obs_rects=False)
        except OSError as ex:
            return {"error": str(ex), "macros": {}}
        return {"macros": {n: _geom_of(m) for n, m in parsed["macros"].items()}}
    got = celllib._cached("mgeom", str(lef_path), [lef_path], build)
    ms = got.get("macros") or {}
    if got.get("error"):
        return {"ok": False, "error": got["error"]}
    if name and name in ms:
        return dict(ms[name], blocks=list(ms))
    blocks = [n for n, g in ms.items() if str(g.get("class", "")).upper().startswith("BLOCK")] or list(ms)
    if len(blocks) == 1:
        return dict(ms[blocks[0]], blocks=list(ms), renamed=bool(name and name != blocks[0]))
    if not ms:
        return {"ok": False, "error": f"{lef_path.name} has no MACRO block in it"}
    return {"ok": False, "error": f"{lef_path.name} has no MACRO {name}; it has {', '.join(list(ms)[:6])}",
            "blocks": list(ms)}


def gds_cells(path: Optional[Path]) -> Optional[List[str]]:
    """The structure names in a GDS file (None when it can't be read; a
    ``.gds.gz`` is not opened). Only the names are kept, not the file."""
    import struct
    if path is None or not Path(path).is_file() or str(path).lower().endswith(".gz"):
        return None
    path = Path(path)

    def build() -> Optional[List[str]]:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        names: List[str] = []
        i, L = 0, len(data)
        while i + 4 <= L:
            ln, rt = struct.unpack(">HB", data[i:i + 3])
            if ln < 4:
                break
            if rt == 0x06:
                names.append(data[i + 4:i + ln].rstrip(b"\0").decode("ascii", "replace"))
            elif rt == 0x04:
                break
            i += ln
        return names
    return celllib._cached("gdsnames", str(path), [path], build)


def allowed_orients(symmetry: str) -> List[str]:
    """The orientations the LEF's SYMMETRY lets a block take. No SYMMETRY
    statement means it may only sit as drawn (N)."""
    s = set(symmetry.upper().replace(";", " ").split())
    out = ["N"]
    if {"X", "Y"} <= s or "R90" in s:
        out.append("S")
    if "Y" in s:
        out.append("FN")
    if "X" in s:
        out.append("FS")
    if "R90" in s:
        out += ["W", "E"]
        if "X" in s or "Y" in s:
            out += ["FW", "FE"]
    return out


def footprint(w: float, h: float, orient: str) -> Tuple[float, float]:
    return (h, w) if orient in ROTATED else (w, h)


def xform_point(x: float, y: float, w: float, h: float, orient: str) -> Tuple[float, float]:
    """A point of a *w* x *h* block, in the block's own frame, after the block
    is placed with *orient* (its lower-left corner stays the origin)."""
    return {
        "N": (x, y), "S": (w - x, h - y), "FN": (w - x, y), "FS": (x, h - y),
        "W": (h - y, x), "E": (y, w - x), "FW": (y, x), "FE": (h - y, w - x),
    }.get(orient, (x, y))


def xform_rect(r: List[float], w: float, h: float, orient: str) -> List[float]:
    ax, ay = xform_point(r[0], r[1], w, h, orient)
    bx, by = xform_point(r[2], r[3], w, h, orient)
    return [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]


# --------------------------------------------------------------------------- #
# The PDK's grids: site, manufacturing grid, routing layers, corners
# --------------------------------------------------------------------------- #
def _tech_lef(pdk: str, scl: str, pcfg: Dict[str, Any]) -> Optional[Path]:
    p = pcfg.get("TECH_LEF")
    if isinstance(p, str) and p and Path(p).is_file():
        return Path(p)
    files = celllib._tech_lef_files(pdk, scl)
    nom = [f for f in files if "nom" in f.name and "rram" not in f.name]
    return (nom or files or [None])[0]


def tech_info(pdk: str, scl: str, pcfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Manufacturing grid, database units and routing layers (bottom up) from
    the tech LEF."""
    pcfg = pcfg if pcfg is not None else celllib.pdk_config(pdk, scl)
    tl = _tech_lef(pdk, scl, pcfg)
    if tl is None:
        return {"mfg": 0.005, "dbu": 1000, "layers": [], "file": None}

    def build() -> Dict[str, Any]:
        mfg, dbu = 0.005, 1000
        layers: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        try:
            text = tl.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"mfg": mfg, "dbu": dbu, "layers": [], "file": str(tl)}
        skip: Optional[str] = None
        for raw in text.splitlines():
            t = raw.split("#", 1)[0].replace(";", " ").split()
            if not t:
                continue
            k = t[0].upper()
            if skip is not None:
                if k == "END" and len(t) > 1 and t[1] == skip:
                    skip = None
                continue
            if cur is None:
                if k == "PROPERTYDEFINITIONS":
                    skip = "PROPERTYDEFINITIONS"
                elif k in ("VIA", "VIARULE", "SITE", "MACRO", "NONDEFAULTRULE", "SPACING") and len(t) > 1:
                    skip = t[1] if k != "SPACING" else "SPACING"
                elif k == "MANUFACTURINGGRID" and len(t) > 1:
                    mfg = float(t[1])
                elif k == "DATABASE" and len(t) > 2 and t[1].upper() == "MICRONS":
                    dbu = int(float(t[2]))
                elif k == "LAYER" and len(t) == 2:
                    cur = {"name": t[1], "type": ""}
                continue
            if k == "END" and len(t) > 1 and t[1] == cur["name"]:
                if cur.get("type") == "ROUTING":
                    layers.append(cur)
                cur = None
            elif k == "TYPE" and len(t) > 1:
                cur["type"] = t[1].upper()
            elif k == "DIRECTION" and len(t) > 1:
                cur["dir"] = t[1].upper()
            elif k in ("PITCH", "OFFSET", "WIDTH", "SPACING") and len(t) > 1 and k.lower() not in cur:
                try:
                    cur[k.lower()] = float(t[1])
                except ValueError:
                    pass
        return {"mfg": mfg, "dbu": dbu, "layers": layers, "file": str(tl)}
    return celllib._cached("techinfo", str(tl), [tl], build)


def _default_scl(pdk: str) -> Optional[str]:
    top = celllib._tech_dir(pdk)
    if top is None:
        return None
    env = celllib._parse_tcl_env(top / "config.tcl", {})
    return env.get("STD_CELL_LIBRARY") or None


_CORNER_RX = re.compile(r"\b(?:nom|min|max)_([A-Za-z0-9]+_[A-Za-z0-9]+_[A-Za-z0-9]+)\b")


def corner_suffixes(pdk: str, scl: str, pcfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """The corner names a run of this PDK times, without the RC prefix:
    ``tt_025C_1v80``. From STA_CORNERS when the PDK lists them, else from the
    typical/slowest/fastest libraries (sky130, gf180)."""
    pcfg = pcfg if pcfg is not None else celllib.pdk_config(pdk, scl)
    out: List[str] = []
    for d in (celllib._tech_dir(pdk, scl), celllib._tech_dir(pdk)):
        if d is None:
            continue
        try:
            text = (d / "config.tcl").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"STA_CORNERS\)\s*\"(.*?)\"", text, re.S)
        if m:
            for s in _CORNER_RX.findall(m.group(1)):
                if s not in out:
                    out.append(s)
    if not out:
        for key in ("LIB_TYPICAL", "LIB_SLOWEST", "LIB_FASTEST"):
            v = pcfg.get(key)
            if isinstance(v, str) and v.strip():
                stem = Path(v.split()[0]).stem
                suf = stem.rsplit("__", 1)[-1] if "__" in stem else stem[len(scl):].lstrip("_")
                if suf and suf not in out:
                    out.append(suf)
    return out


def _corner_parts(text: str) -> Tuple[str, Optional[float], Optional[float]]:
    t = text.lower()
    proc = ""
    m = re.search(r"(?:^|_)(tt|typ|ss|slow|ff|fast)(?:_|$)", t)
    if m:
        proc = {"tt": "t", "typ": "t", "ss": "s", "slow": "s", "ff": "f", "fast": "f"}[m.group(1)]
    volt = None
    m = re.search(r"(\d+)[vp](\d+)", t)
    if m:
        volt = float(m.group(1) + "." + m.group(2))
    temp = None
    m = re.search(r"(?:^|_)([nm-]?)(\d+)c(?:_|$)", t)
    if m:
        temp = float(m.group(2)) * (-1 if m.group(1) else 1)
    return proc, temp, volt


def lib_corner_map(libs: List[Path], suffixes: List[str]) -> Tuple[Dict[str, Path], List[str]]:
    """Each run corner's timing file for a block: ``{"*_tt_025C_1v80": lib}``.
    One file, or corners the names can't tell apart, map to ``"*"`` (every
    corner) with a note, so no corner is left without a model (STA-1112)."""
    libs = [Path(p) for p in libs]
    if not libs:
        return {}, []
    typ = _typical_lib(libs, None)
    if len(libs) == 1 or not suffixes:
        note = (f"Its only timing file ({libs[0].name}) is used for every corner, so slow- and "
                "fast-corner numbers through this block are its typical numbers.") if len(libs) == 1 else \
               f"LanEx couldn't tell this PDK's corners, so {typ.name} is used for every corner."
        return {"*": typ}, [note]
    out: Dict[str, Path] = {}
    notes: List[str] = []
    for suf in suffixes:
        sp, st, sv = _corner_parts(suf)

        def score(p: Path) -> Tuple[int, float, float]:
            lp, lt, lv = _corner_parts(p.stem)
            return (0 if lp == sp else 1,
                    abs((lv if lv is not None else 0) - (sv if sv is not None else 0)),
                    abs((lt if lt is not None else 0) - (st if st is not None else 0)))
        best = sorted(libs, key=score)[0]
        sc = score(best)
        out["*_" + suf] = best
        if sc[0] or sc[1] > 0.051 or sc[2] > 0.5:
            notes.append(f"Corner {suf} uses {best.name}, the closest timing file it ships.")
    return out, notes


# --------------------------------------------------------------------------- #
# Generated views: a ports-only header, and a Liberty copy a run can meet
# --------------------------------------------------------------------------- #
_VLOG_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def blackbox_header(module: str, pins: List[Dict[str, Any]]) -> str:
    """``(* blackbox *)`` module with the block's ports, from its LEF. Synthesis
    and lint read it in place of a vendor's behavioural model, which Yosys or
    Verilator may not parse (IHP's needs a define; gf180's infers a latch)."""
    grouped = group_pins(pins)
    pw = [p for p in grouped if p["use"] in ("power", "ground")]
    sig = [p for p in grouped if p["use"] not in ("power", "ground")]

    def port(p: Dict[str, Any]) -> str:
        d = (p.get("dir") or "inout").split()[0].lower()
        d = d if d in ("input", "output", "inout") else "inout"
        rng = f"[{p['hi']}:{p['lo']}]" if p.get("bus") else ""
        nm = p["name"] if _VLOG_ID.match(p["name"]) else "\\" + p["name"] + " "
        return f"    {d:<6} {rng:<8} {nm}".rstrip()
    lines = [f"// Black-box header for {module}: its ports only, read from its LEF by LanEx.",
             "// Synthesis and lint read this file; keep the vendor's behavioural model for simulation.",
             "// Power is hooked up by LanEx through PDN_MACRO_CONNECTIONS, not in your RTL.",
             "(* blackbox *)", f"module {module} ("]
    good_pw = [p for p in pw if _VLOG_ID.match(p["name"])]
    for p in pw:
        if p not in good_pw:
            lines.append(f"    // power pin {p['name']} has no Verilog name; LanEx connects it to the grid")
    if good_pw:
        lines.append("`ifdef USE_POWER_PINS")
        for p in good_pw:
            lines.append(f"    inout           {p['name']}" + ("," if sig else ""))
        lines.append("`endif")
    for k, p in enumerate(sig):
        lines.append(port(p) + ("," if k < len(sig) - 1 else ""))
    lines += [");", "endmodule", ""]
    return "\n".join(lines)


_MAXTR_RX = re.compile(r"^([ \t]*)max_transition([ \t]*:[ \t]*)([0-9.eE+-]+)", re.M)
_GROUP_RX = re.compile(r"\b(pin|bus)\s*\(\s*\"?([^\")\s]+)\"?\s*\)")


def relax_liberty(text: str, floor_ns: float = 0.1) -> Tuple[str, List[Dict[str, Any]]]:
    """*text* with every pin-level ``max_transition`` under *floor_ns* set to
    the file's own ``default_max_transition``, and the list of changes.

    The sky130 SRAMs cap addr0, addr1 and wmask0 at 0.04 ns, which no sky130
    buffer can drive, so the resizer stops with RSZ-0090. The timing arcs
    themselves are left exactly as they are."""
    unit = 1.0
    m = re.search(r"time_unit\s*:\s*\"?1(ps|ns|us)\"?", text)
    if m:
        unit = {"ps": 0.001, "ns": 1.0, "us": 1000.0}[m.group(1)]
    dm = re.search(r"default_max_transition\s*:\s*([0-9.eE+-]+)", text)
    if not dm:
        return text, []
    default = dm.group(1)
    try:
        if float(default) * unit < floor_ns:
            return text, []
    except ValueError:
        return text, []
    changes: List[Dict[str, Any]] = []
    out: List[str] = []
    last = 0
    for mm in _MAXTR_RX.finditer(text):
        try:
            v = float(mm.group(3))
        except ValueError:
            continue
        if v * unit >= floor_ns:
            continue
        owners = list(_GROUP_RX.finditer(text, 0, mm.start()))
        owner = owners[-1].group(2) if owners else "?"
        line = text.count("\n", 0, mm.start()) + 1
        changes.append({"pin": owner.split("[")[0], "from": mm.group(3), "to": default, "line": line})
        out.append(text[last:mm.start(3)])
        out.append(default)
        last = mm.end(3)
    if not changes:
        return text, []
    out.append(text[last:])
    return "".join(out), changes


# --------------------------------------------------------------------------- #
# The floorplan a macro is placed against: die, core, rows, power straps
# --------------------------------------------------------------------------- #
_MARGINS = (("LEFT_MARGIN_MULT", 12.0), ("RIGHT_MARGIN_MULT", 12.0),
            ("BOTTOM_MARGIN_MULT", 4.0), ("TOP_MARGIN_MULT", 4.0))


def _box(v: Any) -> Optional[List[float]]:
    a = v if isinstance(v, (list, tuple)) else str(v or "").replace(",", " ").split()
    try:
        n = [float(x) for x in a]
    except (TypeError, ValueError):
        return None
    return n if len(n) == 4 and n[2] > n[0] and n[3] > n[1] else None


def _num(dcfg: Dict[str, Any], pcfg: Dict[str, Any], names: Tuple[str, ...], default: Optional[float]) -> Optional[float]:
    for src in (dcfg, pcfg):
        for n in names:
            v = src.get(n)
            if v in (None, "", []):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _str(dcfg: Dict[str, Any], pcfg: Dict[str, Any], names: Tuple[str, ...]) -> Optional[str]:
    for src in (dcfg, pcfg):
        for n in names:
            v = src.get(n)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def snap_core(raw: List[float], sw: float, sh: float) -> List[float]:
    """The core OpenROAD's floorplan really makes from *raw*: the lower-left
    corner rounded up to the site grid, then whole sites and rows only
    (InitFloorplan.cc makeRows)."""
    import math
    eps = 1e-6
    cx0 = math.ceil(raw[0] / sw - eps) * sw
    cy0 = math.ceil(raw[1] / sh - eps) * sh
    n_sites = math.floor((raw[2] - cx0) / sw + eps)
    n_rows = math.floor((raw[3] - cy0) / sh + eps)
    return [round(cx0, 4), round(cy0, 4), round(cx0 + n_sites * sw, 4), round(cy0 + n_rows * sh, 4)]


def _layer_index(layers: List[Dict[str, Any]], name: Optional[str]) -> int:
    for i, l in enumerate(layers):
        if name and l["name"].lower() == name.lower():
            return i
    return -1


def macro_grid_plan(geom: Dict[str, Any], tech: Dict[str, Any], vlayer: Optional[str],
                    hlayer: Optional[str]) -> Optional[Dict[str, Any]]:
    """How LanEx powers a block whose power pins the default grid can't reach
    (LibreLane's macro grid only joins PDN_VERTICAL_LAYER to
    PDN_HORIZONTAL_LAYER). Two published patterns, sized from the LEF:

    * ``bridge`` (IHP's template): the pins are vertical stripes on a layer
      below the grid; straps on the free layer above them pick them up.
    * ``columns`` (wafer.space's gf180 template): the pins are columns on low
      layers at the block's west and east edges; vertical straps of the grid's
      own vertical layer go over them.

    None when neither fits; then the person needs their own PDN_CFG."""
    layers = tech.get("layers") or []
    prects = geom.get("prects") or []
    if not prects or not layers or not vlayer:
        return None
    obs = {x.lower() for x in geom.get("obs_layers") or []}
    vi = _layer_index(layers, vlayer)
    pl = {r[2] for r in prects}
    # bridge: every power shape on one layer L, vertical stripes, and the layer
    # above L free of blockages and below or at the grid's vertical layer
    if len(pl) == 1:
        L = next(iter(pl))
        li = _layer_index(layers, L)
        tall = [r for r in prects if (r[6] - r[4]) > 2 * (r[5] - r[3])]
        if 0 <= li and li + 1 < vi and layers[li + 1]["name"].lower() not in obs and \
                len(tall) >= 2 and {r[1] for r in tall} >= {"power", "ground"}:
            up = layers[li + 1]["name"]
            w = round(min(r[5] - r[3] for r in tall), 3)
            return {"kind": "bridge", "pin_layer": L, "strap_layer": up, "width": w, "pitch": round(4 * w, 3),
                    "offset": w, "spacing": w,
                    "connect": [[L, up]] + ([[up, vlayer]] if up.lower() != vlayer.lower() else []),
                    "why": f"Its power pins are on {L} only. LibreLane's default power grid joins {vlayer} to "
                           f"{hlayer} and would not reach them (PDN-0232). LanEx adds a small grid for it: "
                           f"{up} straps across it, joined to {L}" + (f" and {vlayer}." if up.lower() != vlayer.lower() else ".")}
    # columns: power shapes on layers under the grid's vertical layer, at the
    # west and east edges, and the vertical layer itself free over the block
    if vi >= 0 and vlayer.lower() not in obs:
        under = [r for r in prects if 0 <= _layer_index(layers, r[2]) < vi]
        if under:
            top_i = max(_layer_index(layers, r[2]) for r in under)
            top = layers[top_i]["name"]
            w_blk = geom.get("w") or 0
            h_blk = geom.get("h") or 1
            # the edge pads are many short rectangles: a column is the pads
            # along one edge, covering a good part of the block's height
            cols = [r for r in under if r[2] == top and (r[5] - r[3]) < 0.1 * w_blk]
            west = [r for r in cols if r[3] <= 0.01]
            east = [r for r in cols if r[5] >= w_blk - 0.01]
            covered = lambda rs: sum(r[6] - r[4] for r in rs)
            if west and east and covered(west) > 0.3 * h_blk and covered(east) > 0.3 * h_blk:
                cw = round(min(r[5] - r[3] for r in west), 3)
                x_w = min(r[3] for r in west)
                x_e = min(r[3] for r in east)
                sp = (layers[vi].get("spacing") or 0.28)
                return {"kind": "columns", "pin_layer": top, "strap_layer": vlayer,
                        "width": round(cw * 0.472, 2), "offset": round(x_w + cw * 0.236, 2),
                        "spacing": round(sp, 3), "pitch": round(x_e - x_w, 3), "count": 2,
                        "connect": [[vlayer, hlayer], [vlayer, top]],
                        "why": f"Its power pins are on {', '.join(sorted({r[2] for r in under}))} at its west and "
                               f"east edges. LibreLane's default power grid joins {vlayer} to {hlayer} and would "
                               f"not reach them (PDN-0232). LanEx adds two {vlayer} straps over those edge pins, "
                               f"joined to {top} and {hlayer}."}
    return None


def pdn_cfg_text(plans: List[Tuple[str, List[str], Dict[str, Any]]]) -> str:
    """A PDN_CFG that runs LibreLane's own grid, then one small grid per block
    in *plans* (``(macro, [instances], plan)``)."""
    lines = ["# Written by LanEx for the macros on the Cells & macros tab. Regenerated at",
             "# every run start; edit your own copy and set PDN_CFG to keep changes.",
             "# LibreLane's default power grid first:",
             "source $::env(SCRIPTS_DIR)/openroad/common/pdn_cfg.tcl", ""]
    for k, (macro, insts, p) in enumerate(plans):
        g = f"lanex_macro_{k}"
        inst_list = " ".join(insts)
        # The bridge grid takes no halo, as in IHP's template: with one, the
        # Metal5 straps' ground shapes came out unconnected (PSM-0038/0069 on a
        # real IHP run); the columns grid keeps wafer.space's halo.
        lines += [f"# {macro}: {p['kind']} pattern",
                  "define_pdn_grid \\", "    -macro \\", f"    -instances \"{inst_list}\" \\",
                  f"    -name {g} \\"]
        lines += (["    -starts_with POWER", ""] if p["kind"] == "bridge" else
                  ["    -starts_with POWER \\", "    -halo \"$::env(PDN_HORIZONTAL_HALO) $::env(PDN_VERTICAL_HALO)\"", ""])
        if p["kind"] == "bridge":
            lines += ["add_pdn_stripe \\", f"    -grid {g} \\", f"    -layer {p['strap_layer']} \\",
                      f"    -width {p['width']} \\", f"    -pitch {p['pitch']} \\", f"    -offset {p['offset']} \\",
                      f"    -spacing {p['spacing']} \\", "    -nets \"$::env(GND_NET) $::env(VDD_NET)\" \\",
                      "    -starts_with POWER", ""]
        else:
            lines += ["add_pdn_stripe \\", f"    -grid {g} \\", f"    -layer {p['strap_layer']} \\",
                      f"    -width {p['width']} \\", f"    -offset {p['offset']} \\", f"    -spacing {p['spacing']} \\",
                      f"    -pitch {p['pitch']} \\", "    -starts_with GROUND \\", f"    -number_of_straps {p['count']}", ""]
        for a, b in p["connect"]:
            lines += ["add_pdn_connect \\", f"    -grid {g} \\", f"    -layers \"{a} {b}\"", ""]
    return "\n".join(lines)


def _last_run_boxes(design_dir: Path) -> Dict[str, Any]:
    try:
        from . import history
        for r in history.list_runs(str(design_dir))[:6]:
            m = history._load_metrics(Path(r["run_dir"]))
            die = _box(m.get("design__die__bbox"))
            if die:
                return {"tag": r.get("tag"), "die": die, "core": _box(m.get("design__core__bbox"))}
    except Exception:
        pass
    return {}


def floorplan(design_dir: str, pdk: Optional[str] = None, scl: Optional[str] = None,
              placements: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Everything the Arrange canvas draws and checks, from the config and the
    PDK, without a run: the die, the core OpenROAD will make from it, the site
    grid, the power straps, each block's footprint and power pins, and the
    problems (:func:`placement_problems`). *placements* (``[{macro, name,
    location, orientation}]``) replaces the saved positions, for a check of
    unsaved moves."""
    from . import configedit, custommacros
    d = Path(design_dir).resolve()
    info = configedit.read_config(d)
    dcfg = info.get("data") or {}
    named = configedit.config_pdk(d)
    pdk = named.get("pdk") or pdk
    scl = named.get("scl") or scl or (_default_scl(pdk) if pdk else None)
    pcfg = celllib.pdk_config(pdk, scl) if pdk and scl else {}
    if pdk and celllib.pdk_dir(pdk) is None:
        return {"ok": False, "error": f"{pdk} is not installed. Install it on the Tools tab, then come back."}
    lef = celllib._lef(pdk, scl) if pdk and scl else {"sites": {}}
    site = _str(dcfg, pcfg, ("PLACE_SITE",))
    srec = (lef.get("sites") or {}).get(site or "") or {}
    sw = srec.get("w") or _num({}, pcfg, ("PLACE_SITE_WIDTH",), None)
    sh = srec.get("h") or _num({}, pcfg, ("PLACE_SITE_HEIGHT",), None)
    tech = tech_info(pdk, scl, pcfg) if pdk and scl else {"mfg": 0.005, "dbu": 1000, "layers": []}
    sizing = str(dcfg.get("FP_SIZING") or "relative").lower()
    die = core = None
    die_from = core_from = None
    run_tag = None
    margins = {k: _num(dcfg, pcfg, (k,), dv) for k, dv in _MARGINS}
    if sizing == "absolute" and _box(dcfg.get("DIE_AREA")):
        die, die_from = _box(dcfg["DIE_AREA"]), "config"
    else:
        last = _last_run_boxes(d)
        if last:
            die, die_from, run_tag = last["die"], "run", last["tag"]
            if last.get("core"):
                core, core_from = last["core"], "run"
    if die and core is None and sw and sh:
        raw = _box(dcfg.get("CORE_AREA")) if die_from == "config" else None
        core_from = "CORE_AREA" if raw else "computed"
        if raw is None:
            raw = [die[0] + margins["LEFT_MARGIN_MULT"] * sw, die[1] + margins["BOTTOM_MARGIN_MULT"] * sh,
                   die[2] - margins["RIGHT_MARGIN_MULT"] * sw, die[3] - margins["TOP_MARGIN_MULT"] * sh]
        core = snap_core(raw, sw, sh)
    st = custommacros._settings(d)
    halo = {"h": st.get("halo_h") if st.get("halo_h") is not None else _num(dcfg, {}, ("FP_MACRO_HORIZONTAL_HALO",), 10.0),
            "v": st.get("halo_v") if st.get("halo_v") is not None else _num(dcfg, {}, ("FP_MACRO_VERTICAL_HALO",), 10.0),
            "from": "lanex" if st else ("config" if "FP_MACRO_HORIZONTAL_HALO" in dcfg else "default")}

    def pdn_axis(a: str) -> Dict[str, Any]:
        return {"w": _num(dcfg, pcfg, (f"PDN_{a}WIDTH", f"FP_PDN_{a}WIDTH"), 1.6),
                "s": _num(dcfg, pcfg, (f"PDN_{a}SPACING", f"FP_PDN_{a}SPACING"), 1.7),
                "pitch": _num(dcfg, pcfg, (f"PDN_{a}PITCH", f"FP_PDN_{a}PITCH"), 153.6),
                "off": _num(dcfg, pcfg, (f"PDN_{a}OFFSET", f"FP_PDN_{a}OFFSET"), 16.32)}
    vlayer = _str(dcfg, pcfg, ("PDN_VERTICAL_LAYER", "FP_PDN_VERTICAL_LAYER"))
    hlayer = _str(dcfg, pcfg, ("PDN_HORIZONTAL_LAYER", "FP_PDN_HORIZONTAL_LAYER"))
    own_pdn = bool(dcfg.get("PDN_CFG") or dcfg.get("FP_PDN_CFG"))
    pdn = {"vlayer": vlayer, "hlayer": hlayer, "v": pdn_axis("V"), "h": pdn_axis("H"), "own_cfg": own_pdn}
    # endcap width, for "a strip this narrow loses its rows"
    endcap = _str(dcfg, pcfg, ("ENDCAP_CELL", "FP_ENDCAP_CELL"))
    ew = ((lef.get("macros") or {}).get(endcap or "") or {}).get("w") if endcap else None
    macros: Dict[str, Any] = {}
    saved = custommacros._load(d)
    for m in saved:
        if m.get("enabled", True) is False:
            continue
        lefv = (m.get("views") or {}).get("lef")
        p = resolve_view(lefv if isinstance(lefv, str) else "", str(d), pdk or (m.get("source") or {}).get("pdk"))
        g = macro_geometry(p, m["name"])
        if g.get("ok") and not (g.get("w") and g.get("h")):
            g = {"ok": False, "error": "its LEF gives the block no SIZE"}
        if not g.get("ok"):
            macros[m["name"]] = {"ok": False, "error": g.get("error")}
            continue
        vr = [r for r in g["prects"] if vlayer and r[2].lower() == vlayer.lower() and (r[6] - r[4]) > (r[5] - r[3])]
        plan = None
        if own_pdn:
            grid = "own"
        elif {r[1] for r in vr} >= {"power", "ground"}:
            grid = "default"
        else:
            plan = macro_grid_plan(g, tech, vlayer, hlayer)
            grid = "lanex" if plan else "none"
        orients = allowed_orients(g.get("symmetry") or "")
        if grid in ("default", "lanex"):
            orients = [o for o in orients if o not in ROTATED]
        macros[m["name"]] = {k: g[k] for k in ("ok", "w", "h", "symmetry", "power", "prects", "pins", "n_pins",
                                                 "obs_layers", "power_layers", "class")}
        macros[m["name"]].update({"grid": grid, "plan": plan, "orients": orients,
                                  "power_hookup": m.get("power")})
    if placements is not None:
        inst = [{"macro": str(p.get("macro") or ""), "name": str(p.get("name") or ""),
                 "location": _loc(p.get("location")), "orientation": str(p.get("orientation") or "N").upper()}
                for p in placements]
    else:
        inst = [{"macro": m["name"], "name": i["name"], "location": i.get("location"),
                 "orientation": i.get("orientation") or "N"}
                for m in saved if m.get("enabled", True) is not False for i in (m.get("instances") or [])]
    model = {
        "ok": True, "pdk": pdk, "scl": scl, "config": info.get("name"),
        "sizing": sizing, "die": {"box": die, "from": die_from, "tag": run_tag},
        "core": {"box": core, "from": core_from}, "margins": margins,
        "site": {"name": site, "w": sw, "h": sh}, "mfg": tech.get("mfg") or 0.005, "dbu": tech.get("dbu") or 1000,
        "halo": halo, "pdn": pdn, "endcap_w": ew, "macros": macros, "instances": inst,
    }
    model["problems"] = placement_problems(model)
    model["suggest_die"] = suggest_die(model)
    return model


def _loc(v: Any) -> Optional[List[float]]:
    if v in (None, "", []):
        return None
    try:
        a = re.split(r"[,\s]+", v.strip()) if isinstance(v, str) else list(v)
        if len(a) != 2:
            return None
        return [float(a[0]), float(a[1])]
    except (TypeError, ValueError):
        return None


def straps(model: Dict[str, Any], axis: str) -> List[Tuple[str, float, float]]:
    """``(net, lo, hi)`` bands of the core grid's straps: axis ``"h"`` gives the
    horizontal straps (their y extent), ``"v"`` the vertical ones (x). VDD
    first, ground one width plus spacing along (``-starts_with POWER``).

    pdngen's offset is the power strap's *centre line*, so each strap is
    centre ± width/2: the SRAM template's DEF has met5 VPWR at 26.73-28.33
    (core 10.88 + offset 16.65 = 27.53, its centre) and VGND at 30.03-31.63."""
    core = (model.get("core") or {}).get("box")
    p = (model.get("pdn") or {}).get(axis) or {}
    if not core or not p.get("pitch"):
        return []
    lo, hi = (core[1], core[3]) if axis == "h" else (core[0], core[2])
    half = p["w"] / 2
    out = []
    k = 0
    while k < 2000:
        c = lo + p["off"] + k * p["pitch"]
        if c > hi:
            break
        g = c + p["w"] + p["s"]
        out.append(("power", c - half, c + half))
        if g + half <= hi + 1e-6:
            out.append(("ground", g - half, g + half))
        k += 1
    return out


def crossings(model: Dict[str, Any], inst: Dict[str, Any]) -> Dict[str, int]:
    """How many horizontal straps land on the block's vertical power pins, per
    supply (``{"power": n, "ground": n}``): where they cross, pdngen drops the
    vias that power the block."""
    g = (model.get("macros") or {}).get(inst["macro"]) or {}
    loc = inst.get("location")
    out = {"power": 0, "ground": 0}
    if not g.get("ok") or not loc:
        return out
    vlayer = ((model.get("pdn") or {}).get("vlayer") or "").lower()
    hs = straps(model, "h")
    for r in g.get("prects") or []:
        if r[2].lower() != vlayer:
            continue
        x0, y0, x1, y1 = xform_rect(r[3:7], g["w"], g["h"], inst["orientation"])
        if (y1 - y0) <= (x1 - x0):
            continue
        ay0, ay1 = loc[1] + y0, loc[1] + y1
        for net, a, b in hs:
            if net == r[1] and a < ay1 and b > ay0:
                out[net] += 1
    return out


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v - round(v)) > 1e-9 else str(int(round(v)))


def placement_problems(model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every reason a macro position would fail, before a run finds it. Each is
    ``{code, hard, inst, message, fix}``. Hard ones stop the run before it
    starts; the others are warnings. The same rules run live on the canvas
    (static/modules/macro-place.js), so the two always agree."""
    out: List[Dict[str, Any]] = []
    core = (model.get("core") or {}).get("box")
    site = model.get("site") or {}
    sw, sh = site.get("w") or 0, site.get("h") or 0
    mfg = model.get("mfg") or 0.005
    halo = model.get("halo") or {"h": 10, "v": 10}
    pdn = model.get("pdn") or {}
    macros = model.get("macros") or {}
    eps = 1e-4
    placed = []

    def add(code: str, hard: bool, inst: Dict[str, Any], message: str, fix: str = "") -> None:
        out.append({"code": code, "hard": hard, "inst": inst["name"], "macro": inst["macro"],
                    "message": message, "fix": fix})
    for i in model.get("instances") or []:
        g = macros.get(i["macro"]) or {}
        if not g.get("ok"):
            continue
        if not i.get("location"):
            add("unplaced", True, i, f"{i['name']} has no position yet. LibreLane places a macro only where "
                "you put it, so the run would stop at the power grid (PDN-0235 unplaced macros).",
                "Place it on the Cells & macros tab (Macros, Arrange for me).")
            continue
        o = i["orientation"]
        w, h = footprint(g["w"], g["h"], o)
        x, y = i["location"]
        box = [x, y, x + w, y + h]
        placed.append((i, box))
        if o not in (g.get("orients") or ALL_ORIENTS):
            if o in ROTATED:
                add("orient", True, i, f"{i['name']} is turned 90° ({o}). Its {pdn.get('vlayer')} power pins then "
                    f"run beside the {pdn.get('hlayer')} straps, so no strap can reach them (the run would stop "
                    "with PDN-0233).", "Use N, S, FN or FS.")
            else:
                add("orient", True, i, f"{i['name']}'s LEF doesn't allow orientation {o} (SYMMETRY "
                    f"{g.get('symmetry') or 'none'}).", "Use " + ", ".join(g.get("orients") or ["N"]) + ".")
        if core:
            sides = []
            if box[0] < core[0] - eps:
                sides.append(f"{_fmt(core[0] - box[0])} µm on the left (the core starts at x = {_fmt(core[0])} µm)")
            if box[1] < core[1] - eps:
                sides.append(f"{_fmt(core[1] - box[1])} µm at the bottom (the core starts at y = {_fmt(core[1])} µm)")
            if box[2] > core[2] + eps:
                sides.append(f"{_fmt(box[2] - core[2])} µm on the right (the core ends at x = {_fmt(core[2])} µm)")
            if box[3] > core[3] + eps:
                sides.append(f"{_fmt(box[3] - core[3])} µm at the top (the core ends at y = {_fmt(core[3])} µm)")
            if sides:
                add("outside", True, i, f"{i['name']} is outside the core by " + " and ".join(sides) + ". "
                    "Macros must sit inside the core, where the rows and the power grid are.",
                    "Move it inside the core outline.")
        if sw and sh and (abs(x / sw - round(x / sw)) > 1e-4 or abs(y / sh - round(y / sh)) > 1e-4):
            add("grid", False, i, f"{i['name']} is off the site grid ({_fmt(sw)} × {_fmt(sh)} µm). LibreLane "
                "places it where you say, so its pins may land between routing tracks.",
                f"Snap it to x = {_fmt(round(x / sw) * sw)}, y = {_fmt(round(y / sh) * sh)} µm.")
        elif mfg and (abs(x / mfg - round(x / mfg)) > 1e-3 or abs(y / mfg - round(y / mfg)) > 1e-3):
            add("mfg", False, i, f"{i['name']} is off the {_fmt(mfg * 1000)} nm manufacturing grid.", "Snap it to the grid.")
        if g.get("grid") == "default" and o not in ROTATED:
            c = crossings(model, i)
            miss = [n for n in ("power", "ground") if c[n] == 0]
            if miss:
                add("power", True, i, f"No {pdn.get('hlayer')} power strap crosses {i['name']}'s "
                    f"{' or '.join('VDD' if n == 'power' else 'ground' for n in miss)} pins, so it would get no "
                    f"power (the run would stop with PDN-0233). The straps are {_fmt((pdn.get('h') or {}).get('pitch') or 0)} µm apart.",
                    "Move it up or down until a strap crosses it.")
        elif g.get("grid") == "none":
            add("power", True, i, f"LibreLane's default power grid can't reach {i['macro']}'s power pins "
                f"(on {', '.join(g.get('power_layers') or [])}), and LanEx doesn't know a grid for this block.",
                "Write a PDN_CFG with a grid for it and set PDN_CFG in your config.")
    # overlaps
    for a in range(len(placed)):
        for b in range(a + 1, len(placed)):
            (ia, ba), (ib, bb) = placed[a], placed[b]
            if ba[0] < bb[2] - eps and bb[0] < ba[2] - eps and ba[1] < bb[3] - eps and bb[1] < ba[3] - eps:
                add("overlap", True, ia, f"{ia['name']} overlaps {ib['name']}. Macros can't overlap.",
                    "Move one of them.")
    # narrow strips of rows: rows that survive the keep-out cut but are too
    # narrow for a vertical strap pair (PDN-0179 repair channels) or for the
    # buffers the placer puts by the block's pins (a 7 µm strip failed
    # detailed placement, DPL-0036, on a real sky130 run)
    if core and sw:
        vp = pdn.get("v") or {}
        ok_w = 2 * (vp.get("w") or 1.6) + (vp.get("s") or 1.7) + 2.0
        cell_w = max(ok_w, 20.0)
        gone_w = 2 * (model.get("endcap_w") or 3 * sw)
        for i, box in placed:
            hb = [box[0] - halo["h"], box[1] - halo["v"], box[2] + halo["h"], box[3] + halo["v"]]
            gaps = [("left", hb[0] - core[0]), ("right", core[2] - hb[2])]
            for j, ob in placed:
                if j is i:
                    continue
                ohb = [ob[0] - halo["h"], ob[1] - halo["v"], ob[2] + halo["h"], ob[3] + halo["v"]]
                if ohb[1] < hb[3] and hb[1] < ohb[3] and ohb[0] >= hb[2]:
                    gaps.append((f"{j['name']} on the right", ohb[0] - hb[2]))
            for side, gap in gaps:
                if gone_w < gap < cell_w:
                    why = ("Power straps can't reach rows that narrow, so the power grid may stop with PDN-0179."
                           if gap < ok_w else
                           "The placer puts buffers there, next to the block's pins, and a strip that narrow may "
                           "not fit them (DPL-0036 detailed placement failed).")
                    add("channel", False, i, f"{i['name']} leaves a {_fmt(gap)} µm strip of cell rows between "
                        f"its keep-out and {'the ' + side + ' core edge' if side in ('left', 'right') else side}. " + why,
                        f"Close the gap, or leave at least {_fmt(cell_w)} µm.")
    return out


def suggest_die(model: Dict[str, Any]) -> Optional[List[float]]:
    """A fixed die with room for every block, its keep-out, the cells and the
    core margins, rounded up to 10 µm."""
    import math
    halo = model.get("halo") or {"h": 10, "v": 10}
    area = 0.0
    mw = mh = 0.0
    for i in model.get("instances") or []:
        g = (model.get("macros") or {}).get(i["macro"]) or {}
        if not g.get("ok"):
            continue
        w, h = footprint(g["w"], g["h"], i.get("orientation") or "N")
        area += (w + 2 * halo["h"]) * (h + 2 * halo["v"])
        mw, mh = max(mw, w + 2 * halo["h"]), max(mh, h + 2 * halo["v"])
    if not area:
        return None
    side = math.sqrt(area * 2) + 60
    r10 = lambda v: float(math.ceil(v / 10) * 10)
    return [r10(max(side, mw + 100)), r10(max(side, mh + 100))]
