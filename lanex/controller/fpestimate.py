# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""What Setup's floorplan preview needs, before a run: the numbers LibreLane
3.0.4 sizes the die with, read from where LibreLane reads them.

The preview (``static/modules/fppreview.js``) draws the die and the core from
the size settings and checks, ahead of the run, what would stop it at the
floorplan or the power grid. Its arithmetic is OpenROAD's own
(``ifp/InitFloorplan.cc``, LibreLane's ``scripts/openroad/floorplan.tcl``):

* margins: left/right = ``*_MARGIN_MULT`` × site width, top/bottom =
  ``*_MARGIN_MULT`` × site height (defaults 12 and 4, ``steps/openroad.py``);
* relative sizing: core width = floor(sqrt(cell area / utilization / aspect
  ratio)), height = round(width × aspect ratio), the die = core + margins;
* absolute sizing: the die is ``DIE_AREA``, the core the die inset by the
  margins (or ``CORE_AREA``);
* the power straps fit when the core is at least offset + 2 × width +
  spacing across (``pdn/straps.cpp``; forced: PDN-0185 on a 30 µm die and on
  a 4-bit counter, both matched exactly).

Nothing here is a silicon number: the cell area is LibreLane's own synthesis
metric from the project's latest run, and the preview says which run.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# LibreLane 3.0.4's defaults for the core margins (steps/openroad.py,
# Floorplan's config_vars): in sites left and right, in rows top and bottom.
MARGIN_DEFAULTS = {"LEFT_MARGIN_MULT": 12, "RIGHT_MARGIN_MULT": 12, "TOP_MARGIN_MULT": 4, "BOTTOM_MARGIN_MULT": 4}
_PDN_KEYS = ("PDN_VOFFSET", "PDN_VWIDTH", "PDN_VSPACING", "PDN_VPITCH", "PDN_HOFFSET", "PDN_HWIDTH", "PDN_HSPACING",
             "PDN_HPITCH", "PDN_MULTILAYER", "PDN_CORE_RING")
_KEYS = ("FP_SIZING", "FP_CORE_UTIL", "DIE_AREA", "CORE_AREA", "FP_ASPECT_RATIO", "PL_TARGET_DENSITY_PCT",
         "DESIGN_NAME", "VERILOG_FILES") + tuple(MARGIN_DEFAULTS) + _PDN_KEYS


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _site(pdk: str, scl: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The placement site (name, width, height in µm) from the library's LEFs."""
    try:
        from . import celllib
        name = cfg.get("PLACE_SITE") or celllib.pdk_config(pdk, scl).get("PLACE_SITE")
        sites = celllib._lef(pdk, scl).get("sites") or {}
    except Exception:
        return None
    s = sites.get(str(name)) if name else None
    if not s and sites:
        # The site the library's cells sit on: the one with a width.
        s = next((v for v in sites.values() if v.get("w")), None)
        name = next((k for k, v in sites.items() if v is s), name)
    if not s or not s.get("w") or not s.get("h"):
        return None
    return {"name": str(name), "w": float(s["w"]), "h": float(s["h"])}


def _tracks(pdk: str, scl: str, cfg: Dict[str, Any]) -> Dict[str, float]:
    """``{layer: pitch}`` from the library's tracks.info (``met2 X 0.23 0.46``)."""
    path = cfg.get("TRACKS_INFO_FILE")
    if not path:
        try:
            from . import celllib
            path = celllib.pdk_config(pdk, scl).get("TRACKS_INFO_FILE")
        except Exception:
            path = None
    out: Dict[str, float] = {}
    try:
        for ln in Path(str(path)).read_text(encoding="utf-8", errors="replace").splitlines():
            t = ln.split()
            if len(t) >= 4 and _num(t[3]):
                out.setdefault(t[0], float(t[3]))
    except (OSError, TypeError):
        pass
    return out


def _latest_synthesis(design_dir: Path, pdk: str) -> Optional[Dict[str, Any]]:
    """The newest run of the project that finished synthesis for *pdk*: its
    cell area (LibreLane's ``design__instance__area`` after Yosys.Synthesis),
    and the standard-cell area it ended with when it got that far."""
    runs = design_dir / "runs"
    if not runs.is_dir():
        return None
    try:
        dirs = sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: -p.stat().st_mtime)[:40]
    except OSError:
        return None
    for rd in dirs:
        st = next(iter(sorted(rd.glob("*-yosys-synthesis/state_out.json"))), None)
        if st is None:
            continue
        try:
            doc = json.loads(st.read_text(encoding="utf-8"))
            res = json.loads((rd / "resolved.json").read_text(encoding="utf-8")) if (rd / "resolved.json").is_file() else {}
        except (OSError, ValueError):
            continue
        if pdk and res.get("PDK") and res.get("PDK") != pdk:
            continue
        area = _num((doc.get("metrics") or {}).get("design__instance__area"))
        if not area:
            continue
        final = {}
        try:
            final = json.loads((rd / "final" / "metrics.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {"tag": rd.name, "area": area, "at": st.stat().st_mtime,
                "placed_area": _num(final.get("design__instance__area__stdcell")),
                "die": final.get("design__die__bbox"), "core": final.get("design__core__bbox")}
    return None


def _bound(expr: str, params: Dict[str, str]) -> Optional[int]:
    """A range bound such as ``WIDTH-1`` or ``2*W-1``: parameters replaced by
    their values, then + - * and brackets only (no names, no powers)."""
    s = re.sub(r"[A-Za-z_]\w*", lambda w: params.get(w.group(0), "?"), expr.strip())
    if not re.fullmatch(r"[\d+\-*() ]{1,40}", s) or "**" in s:
        return None
    try:
        import ast
        node = ast.parse(s, mode="eval").body

        def ev(n: Any) -> int:
            if isinstance(n, ast.Constant) and isinstance(n.value, int):
                return n.value
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
                return -ev(n.operand)
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult)):
                a, b = ev(n.left), ev(n.right)
                return a + b if isinstance(n.op, ast.Add) else a - b if isinstance(n.op, ast.Sub) else a * b
            raise ValueError(s)
        v = ev(node)
        return v if abs(v) < 1_000_000 else None
    except Exception:
        return None


def last_timing(design_dir: Path) -> Optional[Dict[str, Any]]:
    """The newest finished run's clock and worst setup slack (its own
    final/metrics.json and resolved.json), for the clock helper's line."""
    runs = design_dir / "runs"
    if not runs.is_dir():
        return None
    try:
        dirs = sorted((p for p in runs.iterdir() if (p / "final" / "metrics.json").is_file()),
                      key=lambda p: -(p / "final" / "metrics.json").stat().st_mtime)[:5]
    except OSError:
        return None
    for rd in dirs:
        try:
            m = json.loads((rd / "final" / "metrics.json").read_text(encoding="utf-8"))
            res = json.loads((rd / "resolved.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ws, per = _num(m.get("timing__setup__ws")), _num(res.get("CLOCK_PERIOD"))
        if ws is not None and per:
            return {"tag": rd.name, "clock_period": per, "setup_ws": ws, "pdk": res.get("PDK")}
    return None



def port_bits(text: str, top: str) -> Optional[int]:
    """How many pins the top module has: its port bits, from the Verilog
    (``input [7:0] a, b`` is 16). None when the module isn't found."""
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text or "", flags=re.S)
    m = re.search(r"\bmodule\s+" + re.escape(top) + r"\b(.*?)\bendmodule\b", text, re.S)
    if not m:
        return None
    body = m.group(1)
    params = dict(re.findall(r"\b(?:parameter|localparam)\s+(?:\w+\s+)?(\w+)\s*=\s*(\d+)", body))
    total = 0
    parts = re.split(r"\b(?:input|output|inout)\b", body)
    for chunk in parts[1:]:
        # One declaration: up to its ';' or the ')' that closes the port list.
        depth, end = 0, len(chunk)
        for i, ch in enumerate(chunk):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif ch == ";" and depth == 0:
                end = i
                break
        decl = chunk[:end]
        mm = re.match(r"\s*(?:(?:wire|reg|logic|signed|unsigned|var|tri)\s+)*(\[[^\]]+\])?\s*(.*)", decl, re.S)
        rng, names = (mm.group(1), mm.group(2)) if mm else (None, decl)
        width = 1
        if rng:
            ab = rng.strip("[]").split(":")
            if len(ab) == 2:
                a, b = _bound(ab[0], params), _bound(ab[1], params)
                if a is not None and b is not None:
                    width = abs(a - b) + 1
        n = len([x for x in re.findall(r"[A-Za-z_]\w*", re.sub(r"=[^,]*", "", names))
                 if x not in ("wire", "reg", "logic", "signed", "unsigned", "var", "tri")])
        total += width * max(1, n)
    return total


def estimate_inputs(design_dir: Optional[str | Path], pdk: str, scl: str) -> Dict[str, Any]:
    """Everything the preview needs that the Setup page doesn't already have:
    the site, the margins, the power straps, the pin tracks, the cell area of
    the latest synthesis, the top module's pin count. With no project (the
    new-project wizard) only the PDK's numbers."""
    from . import configedit
    d = Path(design_dir) if design_dir else None
    out: Dict[str, Any] = {"pdk": pdk, "scl": scl}
    data: Dict[str, Any] = {}
    eff: Dict[str, Any] = {}
    if d is not None:
        try:
            info = configedit.read_config(d)
            data = info.get("data") or {}
            eff = configedit.effective_values(data, pdk, scl) if isinstance(data, dict) else {}
        except Exception:
            data, eff = {}, {}
    cfg = {k: (eff.get(k) or {}).get("value") for k in _KEYS if k in eff}
    try:
        from . import celllib
        pcfg = celllib.pdk_config(pdk, scl) if pdk and scl else {}
    except Exception:
        pcfg = {}
    out["installed"] = bool(pcfg)
    out["site"] = _site(pdk, scl, pcfg) if pcfg else None
    out["margins"] = {k: _num(cfg.get(k)) if _num(cfg.get(k)) is not None else (_num(pcfg.get(k)) or v)
                      for k, v in MARGIN_DEFAULTS.items()}
    pdn = {}
    for k in _PDN_KEYS:
        v = cfg.get(k, pcfg.get(k, pcfg.get("FP_" + k)))   # sky130, gf180: the old FP_PDN_* names
        pdn[k] = v if isinstance(v, bool) else _num(v) if _num(v) is not None else v
    if pdn.get("PDN_MULTILAYER") in (None, ""):
        pdn["PDN_MULTILAYER"] = True
    out["pdn"] = pdn
    out["aspect"] = _num(cfg.get("FP_ASPECT_RATIO")) or 1.0
    tr = _tracks(pdk, scl, pcfg) if pcfg else {}
    hl, vl = pcfg.get("FP_IO_HLAYER") or pcfg.get("IO_PIN_H_LAYER"), pcfg.get("FP_IO_VLAYER") or pcfg.get("IO_PIN_V_LAYER")
    out["io"] = {"h_layer": hl, "v_layer": vl, "h_pitch": tr.get(str(hl)), "v_pitch": tr.get(str(vl))}
    out["synth"] = _latest_synthesis(d, pdk) if d is not None else None
    out["timing"] = last_timing(d) if d is not None else None
    out["pins"] = None
    if d is None:
        return out
    # The pin count, from the top module in the design's own sources.
    top = cfg.get("DESIGN_NAME") or data.get("DESIGN_NAME")
    pins = None
    if top:
        try:
            srcs = configedit.resolve_sources(d, data)
            for f in (srcs.get("files") or [])[:200]:
                p = Path(f) if Path(f).is_absolute() else d / f
                try:
                    n = port_bits(p.read_text(encoding="utf-8", errors="replace"), str(top))
                except OSError:
                    continue
                if n is not None:
                    pins = n
                    break
        except Exception:
            pins = None
    out["pins"] = pins
    # Sources newer than that synthesis: the size will move with them.
    syn = out["synth"]
    if syn:
        try:
            srcs = configedit.resolve_sources(d, data)
            newest = max(((d / f) if not Path(f).is_absolute() else Path(f)).stat().st_mtime
                         for f in (srcs.get("files") or [])[:200])
            syn["stale"] = newest > syn["at"]
        except Exception:
            syn["stale"] = False
    return out
