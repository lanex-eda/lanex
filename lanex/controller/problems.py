# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""LanEx's one knowledge base: what went wrong in a run, in plain words, and
what to change.

Every problem has one shape, whatever found it (workflow spec 5.6)::

    {id, severity: fail|warn, kind: halted|check, title, plain,
     where: {stage, step, check}, evidence: [lines], diag: {...},
     fixes: [{kind: "setting", var, from, to, why, resume}
             | {kind: "open", label, go, why}],
     notes: [sentences], learn: [glossary keys]}

Sources: a step that raised (an OpenROAD error code), and every sign-off check
that failed or warned (``verify.py``), with timing read from each of the run's
timing analyses (``timing.py``) and DRC grouped by rule and by place.

Rules the fixes follow (each from the spec and checked against LibreLane
3.0.4): a setting is only offered when this LibreLane knows it; hold problems
never offer a slower clock; timing leads with where it first failed, and a
path that already fails before placement gets no placement setting; I/O
constraints are named before the logic; ``resume`` is the first step that
reads the variable (``varstage.py``). Proposed values are a starting point the
fix sheet lets you change. Nothing here computes a silicon number: every
value comes from the run's own files.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_CODE_RX = re.compile(r"\[(?:ERROR |WARNING )?([A-Z]{2,5}-\d{4})\]")


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def fmt(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return " ".join(fmt(x) for x in v)
    f = _num(v)
    if f is not None and not isinstance(v, bool):
        return str(int(f)) if f == int(f) else ("%.3f" % f).rstrip("0").rstrip(".")
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def _setting(var: str, frm: Any, to: Any, why: str, label: str = "", sure: Optional[str] = None) -> Dict[str, Any]:
    """A setting to change. *sure* says how certain LanEx is that it fixes
    this failure (rustc's applicability levels, in words): ``fixes`` when the
    tool's own message gives the number that makes the error go away,
    ``likely`` when it removes the usual cause, ``try`` when it is a lever
    that helps in many cases. None when not stated."""
    out = {"kind": "setting", "var": var, "from": fmt(frm), "to": fmt(to), "why": why, "label": label}
    if sure:
        out["sure"] = sure
    return out


def _open(label: str, go: Dict[str, Any], why: str = "") -> Dict[str, Any]:
    return {"kind": "open", "label": label, "go": go, "why": why}


def _ns(v: Optional[float]) -> str:
    return "—" if v is None else ("%+.2f ns" % v).replace("-", "−")


def _round_up(v: float, step: float) -> float:
    return round(math.ceil(v / step - 1e-9) * step, 6)


# ---------------------------------------------------------------- a step raised
# One function per error code, checked against the OpenROAD LibreLane 3.0.4
# ships (dcf36133). Each returns the card's title, what happened with the
# log's own numbers, and the fixes.

def _gpl_0301(msg: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    m = re.search(r"Utilization (\d+(?:\.\d+)?) ?%", msg)
    util = _num(m.group(1)) if m else None
    sizing = str(cfg.get("FP_SIZING") or "relative")
    pad = _num(cfg.get("GPL_CELL_PADDING")) or 0
    need = ("The cells need %.0f %% of the core area, so they can't all be placed." % util) if util else \
        "The cells need more than the whole core area, so they can't all be placed."
    fixes: List[Dict[str, Any]] = []
    plain = need
    if pad > 0:
        # Global placement pads each cell by GPL_CELL_PADDING sites and counts
        # the padding as area (an OpenROAD maintainer, OpenROAD issue #4318).
        plain += (" Global placement counts each cell %s sites wider (GPL_CELL_PADDING), and the padding "
                  "counts as area." % fmt(pad))
        fixes.append(_setting("GPL_CELL_PADDING", pad, 0,
                              "No padding at all. Padding gives the router room, so add some back if routing "
                              "struggles.", "Stop padding the cells", sure="likely"))
    if sizing == "absolute":
        die = cfg.get("DIE_AREA")
        dims = [_num(x) for x in (die if isinstance(die, (list, tuple)) else str(die or "").split())]
        plain += " The die is fixed by DIE_AREA (%s µm), so LibreLane can't make room." % fmt(die)
        fixes.append(_setting("FP_SIZING", "absolute", "relative",
                              "LibreLane sizes the die from the cells' area and FP_CORE_UTIL (%s %%)."
                              % fmt(cfg.get("FP_CORE_UTIL") or 50), "Let LibreLane size the die", sure="likely"))
        if len(dims) == 4 and None not in dims and util:
            k = math.sqrt(util / 100.0 / 0.5)       # room for the cells at about half the core
            w, h = (dims[2] - dims[0]) * k, (dims[3] - dims[1]) * k
            to = "%s %s %s %s" % (fmt(dims[0]), fmt(dims[1]), fmt(dims[0] + 5 * math.ceil(w / 5)),
                                  fmt(dims[1] + 5 * math.ceil(h / 5)))
            fixes.append(_setting("DIE_AREA", die, to, "A die big enough for these cells at about half the core.",
                                  "Make the die bigger", sure="fixes"))
    else:
        core = _num(cfg.get("FP_CORE_UTIL"))
        if pad <= 0:
            plain += (" FP_CORE_UTIL asks for a core %s %% full, which leaves no room for the tap cells and "
                      "buffers placement adds." % fmt(core))
        if core:
            to = max(20, min(50, int(5 * math.floor(core * 100.0 / max(util or 100.0, 100.0) * 0.85 / 5))))
            fixes.append(_setting("FP_CORE_UTIL", core, to, "A bigger core: room for the cells and what placement adds.",
                                  "Make the core bigger", sure="fixes"))
    return {"title": "Placement ran out of room", "plain": plain, "fixes": fixes, "learn": ["utilization", "core"]}


def slug(step_id: str) -> str:
    """A step's id the way its folder spells it: ``OpenROAD.CTS`` and
    ``35-openroad-cts`` are both ``openroad-cts`` (a run without flow.log
    knows only the folder)."""
    s = str(step_id or "").lower().replace(".", "-")
    return s.split("-", 1)[1] if s[:1].isdigit() and "-" in s else s


def _dpl_0036(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ev = dict(ev or {}, cfg=cfg)
    step = str(ev.get("step") or "")
    # OpenROAD lists the cells just above the error (DPL-0034/0035).
    cells = re.findall(r"DPL-0035\]\s+(\S+)", ev.get("log") or "")
    fixes: List[Dict[str, Any]] = []
    core = _num(cfg.get("FP_CORE_UTIL"))
    if core and core > 25:
        fixes.append(_setting("FP_CORE_UTIL", core, int(max(20, core - 10)),
                              "More room around the cells the placer could not fit.", "Make the core bigger", sure="likely"))
    dens = _num(cfg.get("PL_TARGET_DENSITY_PCT"))
    if dens and dens > 30:
        fixes.append(_setting("PL_TARGET_DENSITY_PCT", dens, int(max(20, dens - 10)),
                              "Spread the cells out before they are legalised.", "Spread the cells", sure="try"))
    which = ("%d cell%s (%s)" % (len(cells), "" if len(cells) == 1 else "s", ", ".join(cells[:4]) + (" …" if len(cells) > 4 else ""))
             if cells else "some cells")
    # A macro whose keep-out leaves a narrow strip of rows by the core edge
    # (forced: the SRAM in the bottom-left corner, a 7.02 µm strip on its left;
    # the same strip with rows below it placed fine).
    strips = _macro_strips(ev) if cfg.get("MACROS") else []
    if strips:
        inst, side, gap = strips[0]
        halo = _num(cfg.get("FP_MACRO_HORIZONTAL_HALO"))
        return {"title": "The rows beside %s are too narrow" % inst,
                "plain": "%s's %s µm keep-out leaves a %s µm strip of cell rows on its %s, between the keep-out and the "
                         "core edge. The placer puts buffers there, next to the block's pins, and %s then had no legal "
                         "spot in rows that narrow. Leave at least 20 µm of rows beside a macro, or none: move it against "
                         "the core edge, so its keep-out covers the rows there."
                         % (inst, fmt(10 if halo is None else halo), fmt(gap), side, which),
                "fixes": [_open("Move %s" % inst, _MACROS_PAGE,
                                "The Arrange canvas shows the strip and its width; Arrange for me leaves wide channels."),
                          _open("Show the layout", {"tab": "layout"}, "Where the cells that didn't fit sit.")],
                "learn": ["macro", "utilization"]}
    if slug(step).startswith("openroad-cts"):
        # cts.tcl legalises after inserting the clock buffers (LibreLane 3.0.4:
        # "[INFO] Legalizing…" then detailed_placement, then DPL-0036).
        return {"title": "No room for the clock tree's buffers",
                "plain": "The clock tree step added its buffers, then had to fit every cell back into a row, and "
                         "%s had no legal spot. The placement itself had passed: the core is too full to take the "
                         "clock tree as well. Give it more room." % which,
                "fixes": fixes, "learn": ["utilization"]}
    if step and not slug(step).startswith("openroad-detailedplacement"):
        return {"title": "No room for the cells this step added",
                "plain": "%s added or resized cells, then had to fit every cell back into a row, and %s had no legal "
                         "spot. The core is too full there; look at where they sit in Layout." % (step, which),
                "fixes": fixes, "learn": ["utilization"]}
    return {"title": "Detailed placement couldn't fit every cell",
            "plain": "%s %s no legal spot in a row: the log lists them just above the error. "
                     "Usually the core is too full in that area; look at where they sit in Layout."
                     % (which[0].upper() + which[1:], "has" if len(cells) == 1 else "have"),
            "fixes": fixes, "learn": ["utilization"]}


def _die_area(cfg: Dict[str, Any]) -> Optional[List[float]]:
    die = cfg.get("DIE_AREA")
    dims = [_num(x) for x in (die if isinstance(die, (list, tuple)) else str(die or "").split())]
    return dims if len(dims) == 4 and None not in dims else None  # type: ignore[return-value]


def _bigger_die(cfg: Dict[str, Any], factor: float, why: str, label: str = "Make the die bigger") -> Optional[Dict[str, Any]]:
    """A DIE_AREA *factor* times wider and taller (rounded up to 5 µm), same origin."""
    d = _die_area(cfg)
    if not d or factor <= 1:
        return None
    w, h = (d[2] - d[0]) * factor, (d[3] - d[1]) * factor
    to = "%s %s %s %s" % (fmt(d[0]), fmt(d[1]), fmt(d[0] + 5 * math.ceil(w / 5 - 1e-9)), fmt(d[1] + 5 * math.ceil(h / 5 - 1e-9)))
    return _setting("DIE_AREA", cfg.get("DIE_AREA"), to, why, label)


def _relative(cfg: Dict[str, Any], why: str) -> Dict[str, Any]:
    return _setting("FP_SIZING", "absolute", "relative", why, "Let LibreLane size the die")


def _pdn_0185(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [PDN-0185] Insufficient width (8.16 um) to add straps on layer met5 in grid
    # "stdcell_grid" with total strap width 5.7 um and offset 5.0 um.
    m = re.search(r"Insufficient width \((\d+(?:\.\d+)?) um\) to add straps on layer (\S+) in grid \"([^\"]+)\" "
                  r"with total strap width (\d+(?:\.\d+)?) um and offset (\d+(?:\.\d+)?) um", msg)
    fixes: List[Dict[str, Any]] = []
    absolute = str(cfg.get("FP_SIZING") or "relative") == "absolute"
    if m:
        have, layer, need = float(m.group(1)), m.group(2), float(m.group(4)) + float(m.group(5))
        plain = ("The power grid needs %.1f µm across for its %s straps (%s µm of straps after an offset of %s µm), and "
                 "the core is only %.2f µm across there. " % (need, layer, m.group(4), m.group(5), have))
    else:
        have = need = 0.0
        plain = "The power straps don't fit in the core, so the grid can't be built. "
    if absolute:
        plain += "The die is fixed by DIE_AREA (%s µm). Make it bigger, or ask for smaller strap offsets (PDN_VOFFSET, " \
                 "PDN_HOFFSET) and a finer grid (PDN_VPITCH, PDN_HPITCH)." % fmt(cfg.get("DIE_AREA"))
        d = _die_area(cfg)
        if d and have > 0 and need > 0:
            # The core grows by what the die grows; keep the margins.
            side = max(d[2] - d[0], d[3] - d[1])
            big = _bigger_die(cfg, (side + (need - have) * 1.5 + 5) / side,
                              "Room for the straps: the core grows by as much as the die does.")
            if big:
                big["sure"] = "fixes"
                fixes.append(big)
        fixes.append(_relative(cfg, "LibreLane sizes the die from the cells' area and FP_CORE_UTIL (%s %%); a small "
                                    "design still gets a very small die, so check the power grid settings too."
                               % fmt(cfg.get("FP_CORE_UTIL") or 50)))
    else:
        plain += "Make the die bigger (a lower FP_CORE_UTIL), or ask for a finer grid (PDN_VPITCH, PDN_HPITCH)."
        core = _num(cfg.get("FP_CORE_UTIL"))
        if core and core > 25:
            fixes.append(_setting("FP_CORE_UTIL", core, int(max(20, core - 15)),
                                  "A bigger core has room for the power straps.", "Make the core bigger", sure="likely"))
    return {"title": "The die is too small for the power grid", "plain": plain, "fixes": fixes, "learn": ["pdn"]}


def _pdn_0179(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # "Unable to repair all channels." (the text is in OpenROAD's pdn module in
    # the LibreLane 3.0.4 image). A channel is a gap between macros, or between a
    # macro and the core edge, too narrow for the straps to pass.
    return {"title": "The power grid can't get between the macros",
            "plain": "The power grid's straps have to pass through every gap between macros (and between a macro and "
                     "the core edge). OpenROAD tried to repair the gaps that are too narrow for a strap and couldn't. "
                     "Move the macros apart, or against the core edge so no thin gap is left, or give the design a "
                     "bigger die.",
            "fixes": [_open("Show the macros", {"tab": "cells", "section": "cl-macros"},
                            "Where each macro is placed (MACROS / MACRO_PLACEMENT_CFG)."),
                      _open("Show the layout before the power grid", {"tab": "layout"},
                            "The floorplan with the macros, to see the thin gaps.")],
            "learn": ["pdn"]}


def _ppl_0024(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [PPL-0024] Number of IO pins (602) exceeds maximum number of available
    # positions (206). Increase the die perimeter from 240.00um to 818.72um.
    m = re.search(r"Number of IO pins \((\d+)\) exceeds maximum number of available positions \((\d+)\)\. "
                  r"Increase the die perimeter from (\d+(?:\.\d+)?)um to (\d+(?:\.\d+)?)um", msg)
    fixes: List[Dict[str, Any]] = []
    if not m:
        return {"title": "Too many pins for the die's edge",
                "plain": "The die's edge has fewer places for pins than the design has pins.", "fixes": [], "learn": []}
    pins, room, have, need = int(m.group(1)), int(m.group(2)), float(m.group(3)), float(m.group(4))
    plain = ("The design has %d pins and the die's edge has room for %d: it is %s µm around and needs about %s µm. "
             "Each pin needs its own place on a routing track along the edge, however few cells there are."
             % (pins, room, fmt(round(have)), fmt(math.ceil(need))))
    absolute = str(cfg.get("FP_SIZING") or "relative") == "absolute"
    if absolute:
        big = _bigger_die(cfg, need / have * 1.05, "A die whose edge has room for every pin (%s µm around)." % fmt(math.ceil(need)),
                          "Make the die bigger")
        if big:
            big["sure"] = "fixes"
            fixes.append(big)
    else:
        core = _num(cfg.get("FP_CORE_UTIL"))
        if core:
            # The die's side grows as 1/sqrt(utilisation): the perimeter needs need/have.
            to = int(core * (have / need) ** 2 / 1.1)
            if to >= 1:
                fixes.append(_setting("FP_CORE_UTIL", core, to, "LibreLane sizes the die from the cells; a lower "
                                      "utilization makes it big enough around for the pins.", "Make the die bigger",
                                      sure="likely"))
            plain += " LibreLane sizes this die from the cells' area, which is small next to the pin count."
    return {"title": "Too many pins for the die's edge", "plain": plain, "fixes": fixes, "learn": []}


def _ifp_core(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The floorplan couldn't be made: IFP-0002 (a cell bigger than the core),
    IFP-0055 (CORE_AREA outside DIE_AREA), STA-0572 on -core_area (the margins
    leave no core at all). All three forced on a scratch design, LibreLane 3.0.4."""
    fixes: List[Dict[str, Any]] = []
    absolute = str(cfg.get("FP_SIZING") or "relative") == "absolute"
    core_area = cfg.get("CORE_AREA")
    if "IFP-0055" in msg:
        title = "The core is outside the die"
        plain = ("CORE_AREA (%s µm) reaches past DIE_AREA (%s µm). The core is where cells go and has to sit inside "
                 "the die. Fix CORE_AREA, or remove it so LibreLane leaves its margins inside the die."
                 % (fmt(core_area), fmt(cfg.get("DIE_AREA"))))
    elif "IFP-0002" in msg:
        mm = re.search(r"(\S+) \((\d+(?:\.\d+)?)um, (\d+(?:\.\d+)?)um\) does not fit in the core area: "
                       r"\((\d+(?:\.\d+)?)um, (\d+(?:\.\d+)?)um\) - \((\d+(?:\.\d+)?)um, (\d+(?:\.\d+)?)um\)", msg)
        title = "The core is too small for a single cell"
        if mm:
            w = float(mm.group(6)) - float(mm.group(4))
            h = float(mm.group(7)) - float(mm.group(5))
            plain = ("Cell %s is %s × %s µm and the core is %s × %s µm, so it can't hold even one row of cells. "
                     % (mm.group(1), mm.group(2), mm.group(3), fmt(round(w, 2)), fmt(round(h, 2))))
        else:
            plain = "A cell is bigger than the whole core. "
        if core_area is not None:
            plain += ("The core comes from CORE_AREA (%s µm); check its corners are x1 y1 x2 y2 with x2 > x1 and y2 > y1."
                      % fmt(core_area))
    else:
        title = "The die is too small for its margins"
        plain = ("The die (DIE_AREA %s µm) is smaller than the margins LibreLane keeps between the die edge and the "
                 "core, so there is no core left to put cells in." % fmt(cfg.get("DIE_AREA")))
        if absolute:
            big = _bigger_die(cfg, max(2.0, 60.0 / max(1.0, max((_die_area(cfg) or [0, 0, 30, 30])[2:]))),
                              "A die big enough for the margins and a core inside them.")
            if big:
                fixes.append(big)
    if absolute and core_area is None:
        fixes.append(_relative(cfg, "LibreLane sizes the die from the cells' area and FP_CORE_UTIL (%s %%)."
                               % fmt(cfg.get("FP_CORE_UTIL") or 50)))
    fixes.append(_open("Open the die size", {"tab": "setup", "setting": "DIE_AREA"}, "Setup's die and core settings."))
    return {"title": title, "plain": plain, "fixes": fixes, "learn": ["core"]}


def _cts_0127(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [CTS-0127] No physical master cell found for buffer sky130_fd_sc_hd__nosuchbuf_4.
    m = re.search(r"No physical master cell found for buffer (\S+?)\.?$", msg.strip().splitlines()[0] if msg.strip() else "")
    cell = m.group(1) if m else "a buffer"
    keys = [k for k in ("CTS_CLK_BUFFERS", "CTS_ROOT_BUFFER") if cell in str(cfg.get(k) or "")]
    fixes: List[Dict[str, Any]] = []
    for k in keys:
        dflt = _pdk_default(cfg, k)
        if dflt not in (None, "", []) and fmt(dflt) != fmt(cfg.get(k)):
            fixes.append(_setting(k, cfg.get(k), dflt, "The PDK's own clock buffers for this library.",
                                  "Use the PDK's clock buffers"))
    fixes.append(_open("Look the cell up in the library", {"tab": "cells", "section": "cl-library"},
                       "Every cell this library has, with its exact name."))
    return {"title": "The clock tree names a cell the library doesn't have",
            "plain": "%s %s for %s, and the standard-cell library has no cell by that name, so the clock tree "
                     "can't be built. Check the spelling and the library prefix."
                     % (" and ".join(keys) or "The clock tree settings", "asks" if len(keys) == 1 else "ask", cell),
            "fixes": fixes, "learn": []}


def _rsz_0022(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [RSZ-0022] no buffers found.
    excl = [k for k in ("PNR_EXCLUDED_CELL_FILE", "EXTRA_EXCLUDED_CELLS") if cfg.get(k)]
    return {"title": "Every buffer is excluded",
            "plain": "The resizer needs buffers to repair long wires and timing, and none are allowed: every buffer of "
                     "the library is on an exclusion list%s. Take at least the plain buffers (buf_*) off the list."
                     % (" (" + ", ".join(excl) + ")" if excl else ""),
            "fixes": [_open("Show the excluded cells", {"tab": "cells", "section": "cl-rules"},
                            "Which cells the flow may not use, and which list each comes from.")],
            "learn": []}


def _pdk_default(cfg: Dict[str, Any], var: str) -> Any:
    """The PDK's value of *var* for the run's PDK and library (its config.tcl)."""
    try:
        from . import celllib
        pc = celllib.pdk_config(str(cfg.get("PDK") or ""), str(cfg.get("STD_CELL_LIBRARY") or ""))
        # sky130's and gf180's config.tcl still use the old FP_PDN_* names.
        return pc.get(var, pc.get("FP_" + var) if var.startswith("PDN_") else None)
    except Exception:
        return None


def _modules(cfg: Dict[str, Any]) -> List[str]:
    """Module names declared in the run's Verilog sources, in file order."""
    out: List[str] = []
    for f in cfg.get("VERILOG_FILES") or []:
        try:
            text = Path(str(f)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
        for name in re.findall(r"^\s*module\s+([A-Za-z_]\w*)", text, re.M):
            if name not in out:
                out.append(name)
    return out


def _top_candidates(cfg: Dict[str, Any]) -> List[str]:
    """Modules no other module instantiates: the ones that can be the top."""
    names = _modules(cfg)
    if not names:
        return []
    used = set()
    for f in cfg.get("VERILOG_FILES") or []:
        try:
            text = Path(str(f)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n in names:
            if re.search(r"^\s*" + re.escape(n) + r"\s*(#\s*\(|\w)", text, re.M):
                used.add(n)
    return [n for n in names if n not in used]


def _rel(path: str, cfg: Dict[str, Any]) -> str:
    d = str(cfg.get("DESIGN_DIR") or "").rstrip("/")
    return path[len(d) + 1:] if d and path.startswith(d + "/") else path


# Verilator: "%Error: /d/src/spm.v:67:13: syntax error, unexpected IDENTIFIER";
# Yosys: "/d/src/spm.v:67: ERROR: syntax error, unexpected TOK_ID". Errors
# only: both tools print warnings in the same shape.
_SRC = r"/?[^\s:]+\.(?:s?v|vh|svh|vhd|vhdl)"
_VERILOG_RX = re.compile(r"%Error: (?P<file>" + _SRC + r"):(?P<line>\d+):\d+: (?P<msg>[^\n]+)"
                         r"|^(?P<file2>" + _SRC + r"):(?P<line2>\d+): ERROR: (?P<msg2>[^\n]+)", re.M)


def _verilog_error(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    path, line, what = m.group("file"), int(m.group("line")), m.group("msg").strip()
    rel = _rel(path, cfg)
    tool = "Verilator" if m.group(0).startswith("%Error") else "Yosys"
    syntax = what.lower().startswith("syntax error")
    title = ("Syntax error in %s, line %d" % (rel, line)) if syntax else ("%s can't read %s, line %d" % (tool, rel, line))
    plain = ("%s stopped reading %s at line %d: %s. " % (tool, rel, line, what))
    if syntax:
        plain += ("The mistake is usually on that line or just before it: a missing semicolon, a missing end or "
                  "endmodule, or an unbalanced bracket.")
    return {"title": title, "plain": plain, "evidence": [m.group(0).strip().replace(path, rel)[:300]],
            "fixes": [_open("Open %s in the IDE" % rel, {"tab": "ide", "rel": rel, "line": line},
                            "Go to line %d; the IDE's lint lists the same error." % line)],
            "learn": []}


def _top_missing(ev: Dict[str, Any], m: "re.Match[str]") -> Optional[Dict[str, Any]]:
    cfg = ev.get("cfg") or {}
    name = m.group("top").lstrip("\\")
    if cfg.get("DESIGN_NAME") and name != cfg.get("DESIGN_NAME"):
        # A missing submodule, not the top.
        return _submodule_card(ev, name, "", 0, m.group(0).strip())
    mods = _modules(cfg)
    tops = _top_candidates(cfg)
    fixes: List[Dict[str, Any]] = []
    plain = ("DESIGN_NAME says the top module is %s, and no source file declares a module by that name." % name)
    if mods:
        plain += " The sources declare: %s." % ", ".join(mods[:8]) + (" …" if len(mods) > 8 else "")
    if len(tops) == 1:
        # One module nobody instantiates: the top, whatever it's called.
        fixes.append(_setting("DESIGN_NAME", name, tops[0], "%s is the one module no other module uses, so it is the "
                              "top. The die, pins and reports take its name." % tops[0], "Use %s as the top" % tops[0]))
    fixes.append(_open("Open the sources", {"tab": "setup", "section": "setup-sources"},
                       "Which files go into the chip, and the top module."))
    return {"title": "There is no module called %s" % name, "plain": plain, "evidence": [m.group(0).strip()[:300]],
            "fixes": fixes, "learn": []}


def _path_missing(ev: Dict[str, Any], m: "re.Match[str]") -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    var, path = m.group("var"), m.group("path")
    rel = _rel(path, cfg)
    glob = any(c in path for c in "*?[")
    if glob:
        title = "No file matches %s" % rel
        plain = ("%s asks for every file matching %s, and none does, so LibreLane stopped before the first step. "
                 "Check the folder name and the extension (.v or .sv)." % (var, rel))
    else:
        title = "A file in %s is missing" % var
        plain = ("%s lists %s, and there is no such file, so LibreLane stopped before the first step. It was moved, "
                 "renamed or deleted, or the path has a typo." % (var, rel))
    return {"title": title, "plain": plain, "evidence": [m.group(0).strip()[:300]],
            "fixes": [_open("Open the sources", {"tab": "setup", "section": "setup-sources"},
                            "Pick the design's files again, or fix the path in the config.")] if var.startswith("VERILOG")
            else [_open("Open the setting", {"tab": "setup", "setting": var}, "Where %s is set." % var)],
            "learn": []}


# ---------------------------------------------------------------- RTL the tools refused
# Each forced with LibreLane 3.0.4 on a scratch copy of the Blinky template
# (scripts/failure_goldens.py); the words are the tools' own.

def _src_name(s: str) -> str:
    """Yosys spells names as RTLIL ids: ``\\blinky``, ``$abstract\\blinky``."""
    return re.sub(r"^\$abstract", "", str(s or "")).lstrip("\\")


def _ide(rel: str, line: int, why: str) -> Dict[str, Any]:
    return _open("Open %s at line %d" % (rel, line), {"tab": "ide", "rel": rel, "line": line}, why)


_SOURCES = _open("Open the sources", {"tab": "setup", "section": "setup-sources"},
                 "Which files go into the chip, and the top module.")


def _declaring(cfg: Dict[str, Any], module: str, limit: int = 400) -> List[str]:
    """Project files (design-relative) that declare *module*: what a missing
    or doubled module is usually about. Bounded: at most *limit* HDL files,
    never inside runs/."""
    d = Path(str(cfg.get("DESIGN_DIR") or ""))
    if not module or not d.is_dir():
        return []
    rx = re.compile(r"^\s*module\s+" + re.escape(module) + r"\b", re.M)
    out: List[str] = []
    seen = 0
    for p in sorted(d.rglob("*")):
        if p.suffix.lower() not in (".v", ".sv") or "runs" in p.relative_to(d).parts[:1] or not p.is_file():
            continue
        seen += 1
        if seen > limit:
            break
        try:
            text = re.sub(r"//[^\n]*|/\*.*?\*/", "", p.read_text(encoding="utf-8", errors="replace"), flags=re.S)
        except OSError:
            continue
        if rx.search(text):
            out.append(p.relative_to(d).as_posix())
    return out


def _in_sources(cfg: Dict[str, Any], rel: str) -> bool:
    d = str(cfg.get("DESIGN_DIR") or "").rstrip("/")
    return any(str(f) in (rel, d + "/" + rel) or str(f).endswith("/" + rel) for f in cfg.get("VERILOG_FILES") or [])


def _latch(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    rel, line, sig = _rel(m.group("file"), cfg), int(m.group("line")), m.group("sig")
    short = sig.split(".")[-1]
    return {"title": "A latch was inferred for %s" % short,
            "plain": "The always block at %s line %d doesn't give %s a value on every path, so the hardware has to "
                     "remember its old value: a latch. Latches are hard to time and almost always a mistake, so "
                     "LibreLane's linter stops on them (LINTER_ERROR_ON_LATCH)." % (rel, line, short),
            "fixes": [_ide(rel, line, "Give %s a value at the top of the block (%s = 0;), or add the missing else or "
                                      "default." % (short, short))],
            "notes": ["For a latch you do want, write it as always_latch, or set LINTER_ERROR_ON_LATCH to false."],
            "learn": ["latch", "lint"]}


def _submodule_card(ev: Dict[str, Any], name: str, where: str, line: int, said: str, parent: str = "") -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    found = [f for f in _declaring(cfg, name) if not _in_sources(cfg, f)]
    plain = ("%s uses a module called %s%s, and none of the design's files (VERILOG_FILES) declares it."
             % (where or parent or "The design", name, (" (line %d)" % line) if line else ""))
    fixes: List[Dict[str, Any]] = []
    if found:
        plain += " %s declares it but isn't one of the design's files: tick it in Sources." % found[0]
    else:
        plain += " Check the spelling, or add the file that defines it."
    fixes.append(dict(_SOURCES))
    if where and line:
        fixes.append(_ide(where, line, "The line that uses %s." % name))
    return {"title": "No file defines module %s" % name, "plain": plain, "evidence": [said[:300]],
            "fixes": fixes, "learn": ["hierarchy"], "kb": "module-missing"}


def _modmissing_vl(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    card = _submodule_card(ev, m.group("mod"), _rel(m.group("file"), cfg), int(m.group("line")), m.group(0).strip())
    card.pop("evidence", None)     # the lint checker's own lines are shown
    return card


def _modmissing_ys(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    return _submodule_card(ev, _src_name(m.group("mod")), "", 0, m.group(0).strip(), parent=_src_name(m.group("parent")))


def _include_missing(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    g = _Groups(m)
    rel = _rel(g.group("file"), cfg) if g.group("file") else ""
    line = int(g.group("line") or 0)
    inc = g.group("inc")
    d = Path(str(cfg.get("DESIGN_DIR") or ""))
    hits = []
    if d.is_dir():
        for p in sorted(d.rglob(Path(inc).name))[:5]:
            if p.is_file() and "runs" not in p.relative_to(d).parts[:1]:
                hits.append(p.relative_to(d).as_posix())
    plain = ("%s includes %s, and it isn't found. Include files are looked for next to the file that includes them "
             "and in the folders VERILOG_INCLUDE_DIRS lists." % (rel or "A design file", inc))
    fixes: List[Dict[str, Any]] = []
    if hits:
        folder = str(Path(hits[0]).parent.as_posix())
        plain += " The project has it at %s." % hits[0]
        fixes.append(_open("Add its folder to VERILOG_INCLUDE_DIRS", {"tab": "setup", "setting": "VERILOG_INCLUDE_DIRS"},
                           "In the config: \"VERILOG_INCLUDE_DIRS\": [\"dir::%s\"]. Don't list the header in "
                           "VERILOG_FILES: it is only read through the `include." % folder))
    if rel and line:
        fixes.append(_ide(rel, line, "The `include line."))
    return {"title": "%s can't be found" % inc, "plain": plain, "evidence": [m.group(0).strip()[:300]],
            "fixes": fixes, "learn": []}


def _implicit(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    rel, line, name = _rel(m.group("file"), cfg), int(m.group("line")), _src_name(m.group("sig"))
    return {"title": "%s is used but never declared" % name,
            "plain": "%s line %d uses %s without declaring it. LibreLane reads Verilog with implicit wires turned off "
                     "(Yosys -noautowire), so every wire has to be declared, whatever `default_nettype says. The "
                     "linter only warned about it (Verilator's IMPLICIT), so the run got as far as synthesis."
                     % (rel, line, name),
            "evidence": [m.group(0).strip()[:300]],
            "fixes": [_ide(rel, line, "Declare it before it is used: wire %s; (with its width, if it has one)." % name)],
            "learn": ["lint"]}


def _redefined(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    rel, name = _rel(m.group("file"), cfg), _src_name(m.group("mod"))
    files = [f for f in _declaring(cfg, name) if _in_sources(cfg, f)] or _declaring(cfg, name)
    others = [f for f in files if f != rel]
    plain = ("Two of the design's files declare module %s: %s%s. Yosys reads every file in VERILOG_FILES, and a "
             "module may be defined once. Usually it is an old copy of a file, picked up by a pattern such as "
             "dir::src/*.v." % (name, rel, (" and " + ", ".join(others[:3])) if others else ""))
    return {"title": "Module %s is defined twice" % name, "plain": plain, "evidence": [m.group(0).strip()[:300]],
            "fixes": [_open("Open the sources", {"tab": "setup", "section": "setup-sources"},
                            "Leave the old copy out of the design (untick it), or delete it.")],
            "learn": ["hierarchy"]}


def _sv_frontend(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    rel, line, tok = _rel(m.group("file"), cfg), int(m.group("line")), m.group("tok").lower()
    fixes: List[Dict[str, Any]] = []
    if not cfg.get("USE_SLANG"):
        fixes.append(_setting("USE_SLANG", False, True, "Read the design with the Slang frontend, which supports "
                              "packages with import, interfaces and the rest of SystemVerilog.",
                              "Use the full SystemVerilog reader", sure="likely"))
    fixes.append(_ide(rel, line, "Or write it the way Yosys's own reader takes it: package-scoped names "
                                 "(pkg::NAME) instead of import."))
    return {"title": "Yosys's built-in reader doesn't support `%s`" % tok,
            "plain": "%s line %d uses %s, which the Verilog reader LibreLane uses by default doesn't understand. "
                     "LibreLane can read SystemVerilog with the Slang frontend instead (USE_SLANG)." % (rel, line, tok),
            "evidence": [m.group(0).strip()[:300]], "fixes": fixes, "learn": ["synthesis"]}


# ---------------------------------------------------------------- the config didn't load
# LibreLane 3.0.4 stops before the first step (exit 1, no run folder):
# "Errors have occurred while loading the design configuration file." then
# one line per error (__main__.py, config/variable.py).

def _bad_value(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    var, typ, val = m.group("var"), m.group("type"), m.group("val")
    kind = {"Decimal": "a number", "int": "a whole number", "bool": "true or false", "Path": "a file path",
            "float": "a number"}.get(typ, "a " + typ)
    return {"title": "%s has a value LibreLane can't read" % var,
            "plain": "%s is %s, and the config gives it '%s', so LibreLane stopped before the first step."
                     % (var, kind, val),
            "evidence": [m.group(0).strip()[:300]],
            "fixes": [_open("Open the setting", {"tab": "setup", "setting": var}, "Where %s is set." % var)],
            "learn": []}


def _required(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    var = m.group("var")
    words = {"DESIGN_NAME": "which module is the top", "VERILOG_FILES": "which files the design is made of",
             "CLOCK_PERIOD": "the clock period"}.get(var, var)
    return {"title": "The config doesn't say %s" % words,
            "plain": "LibreLane needs %s, and the config doesn't set it, so it stopped before the first step." % var,
            "evidence": [m.group(0).strip()[:300]],
            "fixes": [_open("Open the setting", {"tab": "setup", "setting": var}, "Where %s goes." % var)] if var else [],
            "learn": []}


def _pdk_missing(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    pdk = m.group("pdk")
    return {"title": "The PDK %s isn't installed" % pdk,
            "plain": "The run asks for %s, and the PDK folder LibreLane was given has no such PDK, so it stopped "
                     "before the first step. Install it from Tools, or pick an installed PDK in Setup." % pdk,
            "evidence": [m.group(0).strip()[:300]], "fixes": list(_TOOLS_FIX), "learn": ["pdk"]}


def nearest_names(name: str, known: Any, n: int = 3) -> List[str]:
    """Known variable names closest to *name* (a typo), nearest first."""
    import difflib
    return difflib.get_close_matches(str(name), sorted(known or []), n=n, cutoff=0.75)


def unknown_key_note(key: str, cfg_name: str = "the config", unused: bool = False) -> Dict[str, Any]:
    """LibreLane 3.0.4 only warns about a key it doesn't know ("An unknown key
    'FP_CORE_UTL' was provided.", config.py) and runs on without it: the
    setting does nothing. Forced: FP_CORE_UTL 40 on Blinky ran clean. *unused*:
    a real variable this flow never reads ("Key 'X' provided is unused by the
    current flow.")."""
    if unused:
        return {"code": "unknown-key", "key": key, "near": [], "hard": False,
                "message": "%s sets %s, which no step of this flow reads, so it changes nothing." % (cfg_name, key),
                "fix": "Remove it, or run the flow that uses it."}
    try:
        from . import introspect
        near = nearest_names(key, introspect.known_var_names())
    except Exception:
        near = []
    msg = ("%s sets %s, which this LibreLane doesn't know, so it is ignored: the run goes ahead as if it weren't "
           "there." % (cfg_name, key))
    return {"code": "unknown-key", "key": key, "near": near,
            "message": msg + (" The nearest known name is %s." % near[0] if near else ""),
            "fix": ("Rename it to %s in %s." % (near[0], cfg_name)) if near else
                   ("Check the name against LibreLane's variables, or remove it from %s." % cfg_name),
            "hard": False}


# ---------------------------------------------------------------- floorplan and pins

def _margin_negative(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [IFP-0013] Negative values not allowed for margins.  (forced: LEFT_MARGIN_MULT -1)
    defaults = {"LEFT_MARGIN_MULT": 12, "RIGHT_MARGIN_MULT": 12, "TOP_MARGIN_MULT": 4, "BOTTOM_MARGIN_MULT": 4}
    bad = [k for k in defaults if (_num(cfg.get(k)) or 0) < 0]
    fixes = [_setting(k, cfg.get(k), defaults[k], "LibreLane's default: %d %s between the die edge and the core."
                      % (defaults[k], "sites" if k.startswith(("LEFT", "RIGHT")) else "rows"),
                      "Use the default margin", sure="fixes") for k in bad]
    return {"title": "A core margin is negative",
            "plain": "%s %s below zero. The margins are the gap between the die edge and the core, counted in "
                     "placement sites (left and right) and rows (top and bottom), so they can't be negative."
                     % (" and ".join(bad) or "A *_MARGIN_MULT setting", "is" if len(bad) <= 1 else "are"),
            "fixes": fixes, "learn": ["core-margin"]}


def _pin_order(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    cfg = ev.get("cfg") or {}
    pcfg = str(cfg.get("IO_PIN_ORDER_CFG") or cfg.get("FP_PIN_ORDER_CFG") or "pin_order.cfg")
    rel = _rel(pcfg, cfg)
    g = _Groups(m)
    fixes: List[Dict[str, Any]] = [_open("Open %s" % Path(rel).name, {"tab": "ide", "rel": rel, "line": 1},
                                         "The pin order file: #N, #S, #E, #W, then one pin name or pattern per line.")]
    if g.group("pin"):
        pin = g.group("pin")
        title = "%s doesn't place %s" % (Path(rel).name, pin)
        plain = ("Every port of the top module has to match a line of %s (ERRORS_ON_UNMATCHED_IO is %s), and %s "
                 "matches none, so the pins couldn't be placed." % (rel, cfg.get("ERRORS_ON_UNMATCHED_IO") or "unmatched_design", pin))
        fixes[0]["why"] = "Add %s under the side it belongs on (#N, #S, #E or #W)." % pin
        if str(cfg.get("ERRORS_ON_UNMATCHED_IO") or "unmatched_design") in ("unmatched_design", "both"):
            fixes.append(_setting("ERRORS_ON_UNMATCHED_IO", cfg.get("ERRORS_ON_UNMATCHED_IO") or "unmatched_design",
                                  "none", "Pins the file doesn't name are then placed by OpenROAD wherever it likes.",
                                  "Place unnamed pins anywhere", sure="try"))
    elif g.group("side"):
        side = {"N": "north (top)", "S": "south (bottom)", "E": "east (right)", "W": "west (left)"}.get(g.group("side"), g.group("side"))
        title = "Too many pins on the %s edge" % side.split(" ")[0]
        plain = ("%s puts %s pins on the %s edge, which has room for %s. Move some to another side, or make the die "
                 "wider there." % (rel, g.group("pins"), side, g.group("slots")))
    else:
        title = "Two lines of %s match the same pin" % Path(rel).name
        plain = ("%s matches more than one line of %s (%s), so OpenROAD can't tell where it goes. Make the patterns "
                 "narrower." % (g.group("dup"), rel, g.group("which")))
    return {"title": title, "plain": plain, "evidence": [m.group(0).strip()[:300]], "fixes": fixes, "learn": ["pin"]}


# ---------------------------------------------------------------- power grid

def _pdn_0175(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [PDN-0175] Pitch 3.0000 is too small for, must be atleast 7.4000  (forced: PDN_VPITCH 3 on spm)
    m = re.search(r"Pitch (\d+(?:\.\d+)?) is too small for,? must be at ?least (\d+(?:\.\d+)?)", msg)
    fixes: List[Dict[str, Any]] = []
    if not m:
        return {"title": "A power-grid pitch is too small", "plain": "The pitch of the power straps is smaller than "
                "the straps it has to hold.", "fixes": [], "learn": ["pdn", "strap"]}
    have, need = float(m.group(1)), float(m.group(2))
    var = next((k for k in ("PDN_VPITCH", "PDN_HPITCH") if _num(cfg.get(k)) is not None and abs(_num(cfg.get(k)) - have) < 1e-6), None)
    names = var or "PDN_VPITCH or PDN_HPITCH"
    plain = ("%s is %s µm, and one group of straps (power and ground, each with its width and the spacing between) "
             "needs at least %s µm, so they would overlap." % (names, fmt(have), fmt(need)))
    if var:
        dflt = _num(_pdk_default(cfg, var))
        to = dflt if dflt and dflt >= need else _round_up(need, 1.0)
        fixes.append(_setting(var, have, to, ("The PDK's own pitch." if dflt and dflt >= need else
                                              "The smallest pitch the straps fit in, rounded up.") ,
                              "Widen the strap pitch", sure="fixes"))
    return {"title": "The power straps don't fit in their pitch", "plain": plain, "fixes": fixes, "learn": ["pdn", "strap"]}


def _pdn_0351(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [PDN-0351] PDN rings do not fit inside the die area by {x} um in X and {y} um in Y. … (pdn/src/rings.cpp)
    m = re.search(r"by (\d+(?:\.\d+)?) um in X and (\d+(?:\.\d+)?) um in Y", msg)
    fixes: List[Dict[str, Any]] = []
    if cfg.get("PDN_CORE_RING"):
        fixes.append(_setting("PDN_CORE_RING", True, False, "No ring around the core: the straps connect to the "
                              "rails directly, as LibreLane does by default.", "No core ring", sure="likely"))
    fixes.append(_open("Open the die size", {"tab": "setup", "setting": "DIE_AREA"}, "A bigger die, or bigger core "
                       "margins (*_MARGIN_MULT), leaves room for the ring."))
    return {"title": "The core ring doesn't fit between the core and the die edge",
            "plain": "PDN_CORE_RING draws a power ring in the margin around the core, and the ring needs %s more than "
                     "the margins leave." % (("%s µm across and %s µm up" % (m.group(1), m.group(2))) if m else "more room"),
            "fixes": fixes, "learn": ["pdn", "core-margin"]}


# ---------------------------------------------------------------- macros
# Each card below was checked against a real LibreLane 3.0.4 run of LanEx's
# SRAM template on sky130A with one thing broken in its macro overlay
# (scripts/failure_goldens.py, cases macro_*), or lane 7's IHP run.

_MACROS_PAGE = {"tab": "cells", "section": "cl-macros"}
_ROTATED = ("E", "W", "FE", "FW")


def _macro_of(cfg: Dict[str, Any], inst: str) -> tuple:
    """``(module, instance settings)`` of a MACROS instance, or ``(None, {})``."""
    macros = cfg.get("MACROS")
    for mod, v in (macros.items() if isinstance(macros, dict) else []):
        ins = (v or {}).get("instances") if isinstance(v, dict) else None
        if isinstance(ins, dict) and inst in ins:
            return mod, ins[inst] if isinstance(ins[inst], dict) else {}
    return None, {}


def _rtl_instances(cfg: Dict[str, Any], modules: List[str]) -> Optional[Dict[str, List[str]]]:
    """``{module: [instance paths]}`` in the RTL this run read (its resolved
    VERILOG_FILES, from DESIGN_NAME down), or None when it can't be read."""
    files = cfg.get("VERILOG_FILES")
    files = [str(f) for f in (files if isinstance(files, list) else [files] if files else [])]
    if not files or not modules:
        return None
    try:
        from . import macrolib
        scan = macrolib.find_instances(str(cfg.get("DESIGN_DIR") or ""), cfg.get("DESIGN_NAME"), modules, files=files)
    except Exception:
        return None
    return scan.get("instances") if scan.get("ok") and scan.get("top_found") else None


def _run_warnings(ev: Optional[Dict[str, Any]]) -> str:
    """The run's warning.log (what a warning-only problem left behind), capped."""
    rd = (ev or {}).get("run_dir")
    try:
        p = Path(rd) / "warning.log" if rd else None
        if p and p.is_file():
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(400_000)
    except OSError:
        pass
    return ""


def _pdn_macro_grid(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [WARNING PDN-0232] The grid "macro - u_sram" (Instance) does not contain any
    # shapes or vias.  then  [ERROR PDN-0233] Failed to generate full power grid.
    # (forced: u_sram turned to E on the SRAM template)
    log = (ev or {}).get("log") or ""
    g = re.search(r'PDN-0232\][^\n]*The grid "([^"]+)"', log + "\n" + msg + "\n" + _run_warnings(ev))
    grid = g.group(1) if g else None
    inst = re.sub(r"^macro\s*-\s*", "", grid) if grid else None
    mod, ins = _macro_of(cfg, inst) if inst else (None, {})
    orient = str(ins.get("orientation") or "").upper()
    vlayer = str(cfg.get("PDN_VERTICAL_LAYER") or "vertical")
    hlayer = str(cfg.get("PDN_HORIZONTAL_LAYER") or "horizontal")
    fixes = [_open("Show the macros", _MACROS_PAGE, "Each macro's orientation, power pins and how they reach the grid.")]
    if inst and orient in _ROTATED:
        return {"title": "%s is turned 90° (%s), so no power strap reaches it" % (inst, orient),
                "plain": "The power grid joins a macro through straps that cross its power pins: %s straps over its "
                         "%s pins. Turned to %s, %s's %s power pins run the same way as the %s straps, beside them "
                         "instead of under them, so its grid (\"%s\") came out with no shapes or vias (PDN-0232) and the "
                         "power step stopped. Use N, S, FN or FS." % (hlayer, vlayer, orient, inst, vlayer, hlayer, grid),
                "fixes": [_open("Turn %s back" % inst, _MACROS_PAGE,
                                "Pick N, S, FN or FS in its inspector on the Arrange canvas.")] + fixes,
                "learn": ["pdn", "macro"]}
    return {"title": "The power grid for %s came out empty" % (inst or "a macro"),
            "plain": "OpenROAD built the grid%s but nothing in it connected: no strap crosses the macro's power pins. "
                     "Check the macro's power connections (PDN_MACRO_CONNECTIONS), where it sits against the straps, and "
                     "the layers of its power pins in its LEF." % ((" \"%s\"" % grid) if grid else ""),
            "fixes": fixes, "learn": ["pdn", "macro"]}


def _pdn_unplaced(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [TAP-0032] Macro u_sram is not placed.  (CutRows and TapEndcapInsertion)
    # [WARNING PDN-0234] u_sram has not been placed and fixed.
    # [ERROR PDN-0235] Design has unplaced macros.  (forced: location null)
    said = ((ev or {}).get("log") or "") + "\n" + msg + "\n" + _run_warnings(ev)
    names: List[str] = []
    for n in re.findall(r"PDN-0234\]\s+(\S+) has not been placed", said) + re.findall(r"TAP-0032\]\s+Macro (\S+) is not placed", said):
        if n not in names:
            names.append(n)
    who = ", ".join(names[:4]) + (" …" if len(names) > 4 else "") if names else "a macro"
    tap = re.search(r"TAP-0032\]\s+(Macro \S+ is not placed)", said)
    return {"title": "%s has no position" % (names[0] if len(names) == 1 else "Some macros have" if names else "A macro"),
            "plain": "LibreLane doesn't place macros by itself: each macro instance needs a position, and %s has none.%s "
                     "The power grid can't be built around a block that isn't placed, so the run stopped here. Give it a "
                     "position: Arrange for me on Cells & macros puts it inside the core."
                     % (who, (" The tap cell step already said so (TAP-0032 \"%s\")." % tap.group(1)) if tap else ""),
            "fixes": [_open("Give %s a position" % (names[0] if len(names) == 1 else "the macros"), _MACROS_PAGE,
                            "Arrange places each block inside the core; drag it on the canvas to move it.")],
            "learn": ["macro"]}


def _macro_instance(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    # Check Macro Instances: "No macro instance u_wrong found." (forced: the
    # overlay renamed u_sram to u_wrong); placers.py: "One or more instances
    # not found. Make sure you use their Verilog and not their LEF names."
    cfg = ev.get("cfg") or {}
    log = (ev.get("log") or "") + "\n" + (ev.get("text") or "")
    names = list(dict.fromkeys(re.findall(r"^\s*\*\s+(\S+)\s*$", log, re.M)
                               or re.findall(r"No macro instance (\S+) found", log)
                               or ([m.group("inst")] if _Groups(m).group("inst") else [])))[:4]
    mods = {n: _macro_of(cfg, n)[0] for n in names}
    rtl = _rtl_instances(cfg, sorted({x for x in mods.values() if x}))
    plain = []
    for n in names:
        mod = mods.get(n)
        found = (rtl or {}).get(mod) if mod else None
        what = " (a %s)" % mod if mod else ""
        if rtl is None or not mod:
            plain.append("The macro settings place an instance named %s%s, and LibreLane found no instance by that name "
                         "in the netlist." % (n, what))
        elif found:
            plain.append("The macro settings place an instance named %s%s, and your RTL names it %s." % (n, what, " or ".join(found)))
        else:
            plain.append("The macro settings place an instance named %s%s, and your RTL doesn't instantiate %s at all: "
                         "add it to the RTL, or switch the macro off." % (n, what, mod))
    plain.append("A macro instance is named by your Verilog: the name after the module name (%s u_sram (…)), with a "
                 "dotted path when it sits inside a submodule (u_core.u_sram). It is never the macro's cell name."
                 % (next((x for x in mods.values() if x), None) or "my_macro"))
    single = names[0] if len(names) == 1 else None
    fixed = single and rtl and mods.get(single) and (rtl.get(mods[single]) or [])
    return {"title": ("%s isn't an instance in your RTL" % single) if single else "Macro settings name instances your RTL doesn't have",
            "plain": " ".join(plain),
            "evidence": [m.group(0).strip()[:300]],
            "fixes": [_open("Use the RTL's names", _MACROS_PAGE,
                            ("Rename %s to %s in the macro's instance list." % (single, fixed[0])) if fixed
                            else "Each macro's instances, checked against your RTL's names.")],
            "learn": ["macro", "hierarchy"]}


def _macro_power(ev: Dict[str, Any], m: Any) -> Optional[Dict[str, Any]]:
    # Checker.DisconnectedPins: "1 critical disconnected pins found." after
    # [WARNING PDN-0231] u_sram is not connected to any power/ground nets. and
    # [WARNING PDN-0189] Supply pin u_sram/vccd1 is not connected to any net.
    # (forced: no PDN_MACRO_CONNECTIONS; the power step only warns, the run
    # routes, then this check stops it). LibreLane counts every disconnected
    # input and power pin as critical (steps/odb.py ReportDisconnectedPins).
    said = (ev.get("log") or "") + "\n" + _run_warnings(ev)
    blocks = []
    for b in re.findall(r"PDN-0231\]\s+(\S+) is not connected to any power/ground nets", said):
        if b not in blocks:
            blocks.append(b)
    pins: Dict[str, List[str]] = {}
    for b, p in re.findall(r"PDN-0189\]\s+Supply pin (\S+?)/(\S+) is not connected to any net", said):
        if p not in pins.setdefault(b, []):
            pins[b].append(p)
    blocks += [b for b in pins if b not in blocks]
    n = m.group("n") if isinstance(m, re.Match) else None
    if not blocks:
        return {"title": "A pin is connected to nothing",
                "plain": "LibreLane's disconnected-pins check found %s critical pin%s with no wire: an input nothing "
                         "drives, a power pin no net reaches, or a block whose outputs all go nowhere. The report of the "
                         "step before it (Odb.ReportDisconnectedPins) lists them by instance."
                         % (n or "a", "" if n in (None, "1") else "s"),
                "fixes": [_open("Show the layout", {"tab": "layout"}, "Look for the instance the report names.")],
                "learn": ["disconnected-pin"]}
    b = blocks[0]
    pp = pins.get(b) or []
    return {"title": "%s's power pins aren't hooked up" % b if len(blocks) == 1 else "Macro power pins aren't hooked up",
            "plain": "Nothing connects %s's power pins%s to the design's power and ground nets: no PDN_MACRO_CONNECTIONS line "
                     "names %s. The power step only warned (PDN-0231 \"%s is not connected to any power/ground nets\"), so "
                     "the run went on through routing until LibreLane's disconnected-pins check stopped it: a block with no "
                     "power can't work. Hook up its power pins to the power and ground nets."
                     % (b, (" (" + ", ".join(pp) + ")") if pp else "", "them" if len(blocks) == 1 else "these blocks", b),
            "fixes": [_open("Hook up its power pins", _MACROS_PAGE,
                            "Each power pin with the net it joins; LanEx writes the PDN_MACRO_CONNECTIONS lines for the run.")],
            "learn": ["pdn", "macro"]}


_PSM_SHAPE_RX = re.compile(r"PSM-0038\] Unconnected shape on net (\S+) at \((-?[\d.]+)um, (-?[\d.]+)um\) - "
                           r"\((-?[\d.]+)um, (-?[\d.]+)um\), layer: (\S+?)\.?$", re.M)


def _psm_0069(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # IR drop: [PSM-0038] Unconnected shape on net VGND at (328.800um, 67.820um) -
    # (394.080um, 68.260um), layer: Metal1.  …  [PSM-0069] Check connectivity
    # failed on VGND.  (lane 7's ihp-sg13g2 SRAM run: LanEx's first macro grid
    # with a keep-out halo left the rails beside the block unconnected. LanEx no
    # longer writes that grid; a config's own PDN_CFG or halo can still do it.)
    net = (re.search(r"Check connectivity failed on (\S+?)\.?$", msg, re.M) or re.search(r"on (\S+?)\.", msg))
    net_name = net.group(1) if net else "a power net"
    log = (ev or {}).get("log") or ""
    full = log
    rd = (ev or {}).get("run_dir")
    try:
        if rd:
            for p in sorted(Path(rd).glob("*-openroad-irdropreport*/*.log")):
                with open(p, encoding="utf-8", errors="replace") as f:
                    full = f.read(4_000_000)
    except OSError:
        pass
    # The step log, and warning.log (which keeps every PSM-0038 line too).
    found = list(dict.fromkeys(_PSM_SHAPE_RX.findall(full + "\n" + _run_warnings(ev))))
    shapes = [s for s in found if s[0] == net_name] or found
    first = shapes[0] if shapes else None
    own = str(cfg.get("PDN_CFG") or "")
    halo = [k for k in ("PDN_HORIZONTAL_HALO", "PDN_VERTICAL_HALO") if _num(cfg.get(k))]
    # OpenROAD prints one message id at most 1,000 times.
    count = ("1,000 or more shapes" if len(shapes) >= 1000 else "%d shape%s" % (len(shapes), "" if len(shapes) == 1 else "s")) \
        if shapes else "part of the grid"
    plain = ("The IR-drop step checks that every piece of the power grid reaches the power pins, and %s on %s %s "
             "connected to nothing%s." % (count, net_name, "is" if len(shapes) == 1 else "are",
                                          (" (PSM-0038; the first is a %s piece at (%s, %s) µm)" % (first[5], fmt(float(first[1])), fmt(float(first[2])))) if first else ""))
    if cfg.get("MACROS"):
        plain += (" With a macro, the usual cause is a power grid for the block%s together with a keep-out halo%s: the halo "
                  "cuts the cell rows' rails beside the block off from the straps."
                  % ((" (PDN_CFG %s)" % Path(own).name) if own else "", (" (" + ", ".join(halo) + ")") if halo else ""))
        plain += (" This run used the grid an earlier LanEx wrote for the block; LanEx now writes it without a halo, so "
                  "running again fixes it." if own.endswith(".gui-macros-pdn.tcl") else
                  " LanEx's own grid for IHP and gf180 blocks uses no halo, so look at the config's own power grid and "
                  "halo settings.")
    fixes = [_open("Show the macros", _MACROS_PAGE, "The macro's keep-out and power grid.")]
    if first:
        fixes.append(_open("Show the first piece in Layout", {"tab": "layout", "box": ",".join(
            "%.2f" % float(v) for v in first[1:5])}, "The final layout, zoomed to the first unconnected shape."))
    return {"title": "Part of the %s grid isn't connected" % net_name, "plain": plain, "fixes": fixes,
            "learn": ["pdn", "macro"] if cfg.get("MACROS") else ["pdn"]}


def _macro_strips(ev: Dict[str, Any]) -> List[tuple]:
    """``(instance, side, width µm)`` for each strip of cell rows between a
    macro's keep-out and the left or right core edge that survives the row cut
    (wider than two end caps) yet is under 20 µm: where a forced sky130 run
    failed detailed placement (macrolib.placement_problems has the same rule)."""
    cfg = ev.get("cfg") or {}
    macros = cfg.get("MACROS")
    core = _core_box(ev)
    if not isinstance(macros, dict) or not core:
        return []
    halo = _num(cfg.get("FP_MACRO_HORIZONTAL_HALO"))
    halo = 10.0 if halo is None else halo
    out = []
    for mod, v in macros.items():
        size = _macro_size(cfg, mod)
        ins = (v or {}).get("instances") if isinstance(v, dict) else None
        if not size or not isinstance(ins, dict):
            continue
        for name, i in ins.items():
            loc = (i or {}).get("location") if isinstance(i, dict) else None
            if not (isinstance(loc, (list, tuple)) and len(loc) == 2):
                continue
            w = size[1] if str((i or {}).get("orientation") or "N").upper() in _ROTATED else size[0]
            x = float(loc[0])
            for side, gap in (("left", x - halo - core[0]), ("right", core[2] - (x + w + halo))):
                if 3.0 < gap < 20.0:
                    out.append((name, side, round(gap, 2)))
    return out


def _core_box(ev: Dict[str, Any]) -> Optional[List[float]]:
    """The core the floorplan made (``design__core__bbox`` in a finished
    step's state), else CORE_AREA."""
    rd = ev.get("run_dir")
    try:
        for p in sorted(Path(rd).glob("*-openroad-floorplan/state_out.json")) if rd else []:
            import json
            v = (json.loads(p.read_text(encoding="utf-8")).get("metrics") or {}).get("design__core__bbox")
            box = [float(x) for x in str(v).split()] if v else []
            if len(box) == 4:
                return box
    except (OSError, ValueError):
        pass
    ca = (ev.get("cfg") or {}).get("CORE_AREA")
    box = [_num(x) for x in (ca if isinstance(ca, (list, tuple)) else str(ca or "").split())]
    return box if len(box) == 4 and None not in box else None  # type: ignore[return-value]


def _macro_size(cfg: Dict[str, Any], module: str) -> Optional[tuple]:
    """``(w, h)`` µm of a macro from the LEF the run used."""
    lef = ((cfg.get("MACROS") or {}).get(module) or {}).get("lef")
    for f in (lef if isinstance(lef, list) else [lef] if lef else []):
        try:
            from . import macrolib
            g = macrolib.macro_geometry(Path(str(f)), module)
            if g.get("ok") and g.get("w") and g.get("h"):
                return float(g["w"]), float(g["h"])
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- resizer, routing, placement

def _rsz_0090(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [RSZ-0090] Max transition time from SDC is 0.010ns. Best achievable
    # transition time is 0.033ns with a load of 0.00pF  (forced: MAX_TRANSITION_CONSTRAINT 0.01)
    m = re.search(r"Max transition time from SDC is (\d+(?:\.\d+)?)ns\. Best achievable transition time is "
                  r"(\d+(?:\.\d+)?)ns", msg)
    want, best = (m.group(1), m.group(2)) if m else ("?", "?")
    fixes: List[Dict[str, Any]] = []
    sdc = cfg.get("PNR_SDC_FILE")
    own_sdc = False
    if sdc:
        try:
            own_sdc = "set_max_transition" in Path(str(sdc)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            own_sdc = False
    plain = ("The constraints ask every signal to switch in %s ns, and the fastest buffer in the library manages %s "
             "ns even with no load, so design repair can't meet the limit anywhere. " % (want, best))
    if own_sdc:
        rel = _rel(str(sdc), cfg)
        plain += "The limit is in the design's own SDC (%s, set_max_transition)." % rel
        fixes.append(_open("Open %s" % Path(rel).name, {"tab": "ide", "rel": rel, "line": 1},
                           "Raise set_max_transition to the PDK's limit, or remove it."))
    else:
        cur = cfg.get("MAX_TRANSITION_CONSTRAINT")
        dflt = _pdk_default(cfg, "MAX_TRANSITION_CONSTRAINT")
        plain += "The limit comes from MAX_TRANSITION_CONSTRAINT (%s ns)." % fmt(cur)
        if _num(dflt) is not None and fmt(dflt) != fmt(cur):
            fixes.append(_setting("MAX_TRANSITION_CONSTRAINT", cur, dflt, "The PDK's own limit for this library.",
                                  "Use the PDK's slew limit", sure="fixes"))
        else:
            fixes.append(_open("Open the setting", {"tab": "setup", "setting": "MAX_TRANSITION_CONSTRAINT"},
                               "Remove it to use the library's own limits."))
    return {"title": "The slew limit is tighter than any buffer can drive", "plain": plain, "fixes": fixes,
            "learn": ["slew", "resizer"]}


def _rsz_util(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [ERROR RSZ-0025] max utilization reached. (RepairSetup; 0050 RepairHold, 0125 RecoverPower)
    caps = [k for k in ("DESIGN_REPAIR_MAX_UTILIZATION", "PL_RESIZER_SETUP_MAX_UTIL_PCT", "PL_RESIZER_HOLD_MAX_UTIL_PCT",
                        "GRT_RESIZER_SETUP_MAX_UTIL_PCT", "GRT_RESIZER_HOLD_MAX_UTIL_PCT") if cfg.get(k) not in (None, "")]
    return {"title": "Timing repair hit the utilization cap",
            "plain": "The repair step stops adding and growing cells once the core is as full as a cap allows%s. It "
                     "stopped with violations still left." % ((" (" + ", ".join(caps) + ")") if caps else ""),
            "fixes": [_open("Open the setting", {"tab": "setup", "setting": caps[0]}, "Raise or remove the cap.")] if caps
            else [_open("Open the die size", {"tab": "setup", "setting": "FP_CORE_UTIL"}, "A bigger core leaves the "
                        "repair room.")],
            "learn": ["resizer", "utilization"]}


def _route_fixes(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Room for the router, in the order that usually helps: congestion
    allowed through global routing first (the detailed router inherits an
    impossible plan), then spread cells, then a bigger core, then layers."""
    fixes: List[Dict[str, Any]] = []
    if cfg.get("GRT_ALLOW_CONGESTION") is True:
        fixes.append(_setting("GRT_ALLOW_CONGESTION", True, False, "Global routing was allowed to finish congested, so "
                              "the detailed router got a plan it couldn't route. Turned off, the run stops at global "
                              "routing and shows where.", "Stop at congestion", sure="try"))
    dens = _num(cfg.get("PL_TARGET_DENSITY_PCT"))
    if dens and dens > 30:
        fixes.append(_setting("PL_TARGET_DENSITY_PCT", dens, int(max(20, dens - 10)),
                              "Spread the cells out, so fewer wires cross any one place.", "Spread the cells", sure="try"))
    core = _num(cfg.get("FP_CORE_UTIL"))
    if str(cfg.get("FP_SIZING") or "relative") != "absolute" and core and core > 25:
        fixes.append(_setting("FP_CORE_UTIL", core, int(max(20, core - 10)),
                              "A bigger core leaves more room for wires.", "Make the core bigger", sure="likely"))
    elif str(cfg.get("FP_SIZING") or "relative") == "absolute":
        big = _bigger_die(cfg, 1.2, "A die 20 % wider and taller: more room for wires.")
        if big:
            big["sure"] = "likely"
            fixes.append(big)
    return fixes


def _grt_congestion(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # GRT-0116 Global routing finished with congestion; GRT-0228/0229 edge usage over the maximum.
    return {"title": "Routing is too congested",
            "plain": "Global routing found more wires than the metal layers have room for in some places, so the "
                     "detailed router would leave shorts and opens there.",
            "fixes": _route_fixes(cfg) + [_open("Show the layout before routing", {"tab": "layout"},
                                                "Where the cells are packed; congestion follows them.")],
            "learn": ["congestion", "global-routing"]}


def _drt_access(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [ERROR DRT-0073] No access point for _123_/A (sky130_fd_sc_hd__nand2_1). (drt FlexPA_acc_point.cpp)
    m = re.search(r"No access point for (\S+)", msg)
    pin = m.group(1) if m else "a pin"
    return {"title": "The router can't reach pin %s" % pin,
            "plain": "The detailed router found no legal way onto %s. Usually a cell pressed against a macro or a "
                     "blockage, or a macro placed off the routing grid." % pin,
            "fixes": [_open("Show the layout before routing", {"tab": "layout"}, "Find the cell; look at what is next to it."),
                      _open("Show the macros", {"tab": "cells", "section": "cl-macros"},
                            "A macro moved onto the routing grid, or given a halo, usually clears it.")],
            "learn": ["detailed-routing", "macro"]}


def _drt_special(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [ERROR DRT-0305] Net {} of signal type {} is not routable by TritonRoute. Move to special nets.
    m = re.search(r"Net (\S+) of signal type (\S+) is not routable", msg)
    net = m.group(1) if m else "a net"
    return {"title": "A power net is wired as a signal (%s)" % net,
            "plain": "%s is a power or ground net, and nothing connected it to the power grid, so the signal router "
                     "was asked to route it and can't. Usually a macro's power pin with no power connection "
                     "(PDN_MACRO_CONNECTIONS)." % net,
            "fixes": [_open("Show the macros", {"tab": "cells", "section": "cl-macros"}, "How each macro's power pins are hooked up.")],
            "learn": ["pdn", "macro"]}


def _drt_offgrid(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [ERROR DRT-0416] Term {} contains offgrid pin shape. …
    return {"title": "A macro's pin is off the manufacturing grid",
            "plain": "A pin shape in a LEF isn't on the PDK's manufacturing grid, so the router can't connect to it. "
                     "The LEF is the problem, not the settings: regenerate the macro, or ask whoever made it.",
            "fixes": [_open("Show the macros", {"tab": "cells", "section": "cl-macros"}, "The macros and their LEF files.")],
            "learn": ["lef-def", "macro"]}


def _gpl_0304(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [ERROR GPL-0304] RePlAce diverged at initial iteration with steplength being {}. …
    fixes: List[Dict[str, Any]] = []
    dens = _num(cfg.get("PL_TARGET_DENSITY_PCT"))
    if dens and dens < 90:
        fixes.append(_setting("PL_TARGET_DENSITY_PCT", dens, int(min(95, dens + 10)),
                              "A little more room for the placer's first steps.", "Loosen the target density", sure="try"))
    return {"title": "Global placement diverged at the start",
            "plain": "The global placer's very first step blew up, which happens with a very small design, an odd die "
                     "shape or a density target the design can't reach.",
            "fixes": fixes, "learn": ["global-placement", "placement-density"]}


def _dpl_0015(msg: str, cfg: Dict[str, Any], ev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # [ERROR DPL-0015] instance {} does not fit inside the ROW core area.
    m = re.search(r"instance (\S+) does not fit inside the ROW core area", msg)
    return {"title": "%s doesn't fit in the core's rows" % (m.group(1) if m else "A cell"),
            "plain": "A cell or macro is bigger than the area the rows cover, so detailed placement has nowhere to put "
                     "it. Usually a macro on a die that is too small for it.",
            "fixes": [_open("Open the die size", {"tab": "setup", "setting": "DIE_AREA"}, "Setup's die and core settings.")],
            "learn": ["row-site", "core"]}


def _corner_wildcard(ev: Dict[str, Any], m: Any) -> Dict[str, Any]:
    var = m.group("var")
    base = "TIMING_VIOLATION_CORNERS" if var.endswith("VIOLATION_CORNERS") else var
    return {"title": "%s names no corner of this PDK" % var,
            "plain": "%s (or %s, which it defaults to) lists corner patterns, and one of them matches none of this "
                     "PDK's timing corners, so LibreLane failed the run at the end. The corners are named like "
                     "nom_tt_025C_1v80." % (var, base),
            "evidence": [m.group(0).strip()[:300]],
            "fixes": [_open("Open the setting", {"tab": "setup", "setting": base}, "Use patterns such as *tt*, or remove it.")],
            "learn": ["corner"]}


def _env(title: str, plain: str, fixes: Optional[List[Dict[str, Any]]] = None):
    return lambda ev, m: {"title": title, "plain": plain, "fixes": list(fixes or []), "learn": [],
                          "evidence": [m.group(0).strip()[:300]]}


_TOOLS_FIX = [_open("Open Tools", {"tab": "tools"}, "The container engine, the LibreLane image and the PDKs, with what each needs.")]


def _oom_text(ev: Dict[str, Any], m: "re.Match[str]") -> Dict[str, Any]:
    return _oom_card(ev, sure=True, said=m.group(0).strip())


def _disk_text(ev: Dict[str, Any], m: "re.Match[str]") -> Dict[str, Any]:
    return _disk_card(ev, said=m.group(0).strip())


def _code_card(fn):
    return lambda ev, m: fn(ev.get("text") or "", ev.get("cfg") or {}, ev)


_VL_LATCH_RX = re.compile(r"%Error-LATCH: (?P<file>" + _SRC + r"):(?P<line>\d+):\d+: Latch inferred for signal '(?P<sig>[^']+)'[^\n]{0,160}")
_VL_MODMISSING_RX = re.compile(r"%Error-MODMISSING: (?P<file>" + _SRC + r"):(?P<line>\d+):\d+: Cannot find file containing "
                               r"module: '(?P<mod>[^']+)'")
_YS_MODMISSING_RX = re.compile(r"ERROR: Module `(?P<mod>[^'\s]+)' referenced in module `(?P<parent>[^'\s]+)' in cell "
                               r"`[^'\s]+' is not part of the design\.")
_INCLUDE_RX = re.compile(r"%Error: (?P<file>" + _SRC + r"):(?P<line>\d+):\d+: Cannot find include file: '(?P<inc>[^']+)'"
                         r"|(?:(?P<file2>" + _SRC + r"):(?P<line2>\d+): )?ERROR: Can't open include file `(?P<inc2>[^']+)'!")
_IMPLICIT_RX = re.compile(r"^(?P<file>" + _SRC + r"):(?P<line>\d+): ERROR: Identifier `(?P<sig>[^']+)' is implicitly declared",
                          re.M)
_REDEF_RX = re.compile(r"^(?P<file>" + _SRC + r"):(?P<line>\d+): ERROR: Re-definition of module `(?P<mod>[^']+)'!", re.M)
_SV_TOK_RX = re.compile(r"^(?P<file>" + _SRC + r"):(?P<line>\d+): ERROR: syntax error, unexpected TOK_(?P<tok>IMPORT|INTERFACE)\b",
                        re.M)
_PIN_ORDER_RX = re.compile(r"(?P<pin>[^\s\]]+) not found in config but found in design\."
                           r"|The (?P<side>[NSEW]) side of the floorplan doesn't have enough slots for all the pins: "
                           r"(?P<pins>\d+) pins/(?P<slots>\d+) slots"
                           r"|Multiple regexes matched (?P<dup>\S+)\. Those are (?P<which>[^\n]{0,200})")

# What LanEx knows about a step that raised, in order: the first entry that
# matches wins. ``code``: the OpenROAD message id; ``rx``: the tool's own words
# (searched in the error text, then the step's log); ``steps``: only for these
# step ids. ``source`` says where the words were checked (LibreLane 3.0.4):
# "forced: …" is a failure made on a real run (tests/goldens/failures).
# ``about`` is the entry's one line for Help's error index, ``stage`` where it
# stops a run, ``also`` other codes that get the same card.
KB: List[Dict[str, Any]] = [
    {"id": "gpl-0301", "code": "GPL-0301", "fn": lambda ev, m: _gpl_0301(ev.get("text") or "", ev.get("cfg") or {}),
     "source": "forced: FP_CORE_UTIL 140 on spm", "stage": "placement",
     "about": "The cells, with any padding, need more area than the core has."},
    {"id": "gpl-0304", "code": "GPL-0304", "fn": _code_card(_gpl_0304), "stage": "placement",
     "source": "OpenROAD dcf36133 gpl (text)", "about": "The global placer's first step diverged."},
    {"id": "dpl-0036", "code": "DPL-0036", "fn": _code_card(_dpl_0036),
     "source": "forced: sweep point FP_CORE_UTIL 70 (in OpenROAD.CTS); the SRAM template with a 7 µm strip of rows by "
               "its keep-out (macro_channel)", "stage": "placement",
     "about": "Detailed placement found no legal row position for some cells (also: a strip of rows beside a macro "
              "that is too narrow)."},
    {"id": "dpl-0015", "code": "DPL-0015", "fn": _code_card(_dpl_0015), "stage": "placement",
     "source": "OpenROAD dcf36133 dpl (text)", "about": "A cell or macro is bigger than the rows."},
    {"id": "grt-0116", "code": "GRT-0116", "also": ["GRT-0228", "GRT-0229"], "fn": _code_card(_grt_congestion),
     "source": "OpenROAD in the 3.0.4 image", "stage": "routing",
     "about": "Global routing ran out of room on the metal layers."},
    {"id": "drt-0073", "code": "DRT-0073", "fn": _code_card(_drt_access), "stage": "routing",
     "source": "OpenROAD dcf36133 drt (text)", "about": "The detailed router can't reach a pin."},
    {"id": "drt-0305", "code": "DRT-0305", "fn": _code_card(_drt_special), "stage": "routing",
     "source": "OpenROAD dcf36133 drt (text)", "about": "A power or ground net reached the signal router."},
    {"id": "drt-0416", "code": "DRT-0416", "fn": _code_card(_drt_offgrid), "stage": "routing",
     "source": "OpenROAD dcf36133 drt (text)", "about": "A macro pin is off the manufacturing grid."},
    {"id": "pdn-0185", "code": "PDN-0185", "fn": _code_card(_pdn_0185), "source": "forced: 30 x 30 um die",
     "stage": "pdn", "about": "The die is too small for the power straps."},
    {"id": "pdn-0175", "code": "PDN-0175", "fn": _code_card(_pdn_0175), "source": "forced: PDN_VPITCH 3 on spm",
     "stage": "pdn", "about": "A strap pitch is smaller than the straps it holds."},
    {"id": "pdn-0179", "code": "PDN-0179", "rx": re.compile(r"Unable to repair all channels\."), "fn": _code_card(_pdn_0179),
     "source": "text in the 3.0.4 image's OpenROAD (pdn)", "stage": "pdn",
     "about": "The straps can't pass through a gap between macros."},
    {"id": "pdn-0233", "code": "PDN-0233", "fn": _code_card(_pdn_macro_grid), "stage": "pdn",
     "source": "forced: the SRAM template turned to E (macro_orient_e)",
     "about": "A macro's power grid came out empty, usually a block turned 90°."},
    {"id": "pdn-0235", "code": "PDN-0235", "fn": _code_card(_pdn_unplaced), "stage": "pdn",
     "source": "forced: the SRAM template with no location (macro_unplaced)", "about": "A macro has no position."},
    {"id": "psm-0069", "code": "PSM-0069", "fn": _code_card(_psm_0069), "stage": "signoff",
     "source": "forced: lane 7's ihp-sg13g2 SRAM run, a macro grid with a keep-out halo (macro_ihp_halo)",
     "about": "Part of the power grid connects to nothing (IR drop's connectivity check)."},
    {"id": "pdn-0351", "code": "PDN-0351", "fn": _code_card(_pdn_0351), "stage": "pdn",
     "source": "OpenROAD dcf36133 pdn rings.cpp (text)", "about": "The core ring doesn't fit inside the die."},
    {"id": "ppl-0024", "code": "PPL-0024", "fn": _code_card(_ppl_0024), "source": "forced: 602 pins on a 60 um die",
     "stage": "floorplan", "about": "The die's edge has fewer pin places than the design has pins."},
    {"id": "ifp-0002", "code": "IFP-0002", "fn": _code_card(_ifp_core), "source": "forced: CORE_AREA 10 10 5 5",
     "stage": "floorplan", "about": "A cell is bigger than the whole core."},
    {"id": "ifp-0013", "code": "IFP-0013", "fn": _code_card(_margin_negative), "source": "forced: LEFT_MARGIN_MULT -1 on blinky",
     "stage": "floorplan", "about": "A core margin is negative."},
    {"id": "ifp-0055", "code": "IFP-0055", "fn": _code_card(_ifp_core), "source": "forced: CORE_AREA past DIE_AREA",
     "stage": "floorplan", "about": "CORE_AREA reaches outside DIE_AREA."},
    {"id": "ifp-margins", "code": "STA-0572", "rx": re.compile(r"-core_area '[^']*' is not a positive float"),
     "fn": _code_card(_ifp_core), "source": "forced: DIE_AREA 0 0 8 8", "stage": "floorplan",
     "about": "The die is smaller than its margins: no core is left."},
    {"id": "cts-0127", "code": "CTS-0127", "also": ["CTS-0126", "CTS-0074"], "fn": _code_card(_cts_0127),
     "source": "forced: CTS_CLK_BUFFERS names no cell", "stage": "cts",
     "about": "The clock tree names a buffer the library doesn't have."},
    {"id": "rsz-0022", "code": "RSZ-0022", "fn": _code_card(_rsz_0022), "source": "forced: every buffer excluded",
     "stage": "placement", "about": "Every buffer of the library is excluded."},
    {"id": "rsz-0090", "code": "RSZ-0090", "fn": _code_card(_rsz_0090), "source": "forced: MAX_TRANSITION_CONSTRAINT 0.01 on blinky",
     "stage": "placement", "about": "The slew limit is tighter than any buffer can drive."},
    {"id": "rsz-util", "code": "RSZ-0025", "also": ["RSZ-0050", "RSZ-0125"], "fn": _code_card(_rsz_util),
     "source": "OpenROAD dcf36133 rsz (text)", "stage": "cts", "about": "Timing repair hit a utilization cap."},
    {"id": "macro-instance",
     "rx": re.compile(r"One or more instances not found\. Make sure you use their Verilog and not their LEF names\."
                      r"|No macro instance (?P<inst>\S+) found"),
     "fn": _macro_instance, "source": "forced: the SRAM template's instance renamed u_wrong (macro_inst)",
     "stage": "floorplan", "about": "The macro settings name an instance your RTL doesn't have."},
    {"id": "macro-power", "rx": re.compile(r"(?P<n>\d+) critical disconnected pins? found"), "fn": _macro_power,
     "source": "forced: the SRAM template with no PDN_MACRO_CONNECTIONS (macro_nopower)", "stage": "signoff",
     "about": "A pin is connected to nothing; with PDN-0231 warnings, a macro's power pins aren't hooked up."},
    {"id": "pin-order", "rx": _PIN_ORDER_RX, "fn": _pin_order, "source": "forced: clk left out of spm's pin_order.cfg",
     "stage": "floorplan", "about": "The pin order file doesn't match the design's ports."},
    # RTL the tools refused. The coded Verilator errors and Yosys's own
    # wording come before the general syntax rule, which would also match them.
    {"id": "latch", "rx": _VL_LATCH_RX, "fn": _latch, "source": "forced: an always @(*) without else on blinky",
     "stage": "synthesis", "about": "An always block leaves a signal unassigned on some path: a latch."},
    {"id": "module-missing", "rx": _VL_MODMISSING_RX, "fn": _modmissing_vl,
     "source": "forced: blinky instantiates a module no file declares (linter on)", "stage": "synthesis",
     "about": "The design uses a module no design file declares."},
    {"id": "module-missing-yosys", "rx": _YS_MODMISSING_RX, "fn": _modmissing_ys,
     "source": "forced: the same with RUN_LINTER false", "stage": "synthesis",
     "about": "The design uses a module no design file declares.", "hidden": True},
    {"id": "include-missing", "rx": _INCLUDE_RX, "fn": _include_missing, "source": "forced: blinky includes a missing header",
     "stage": "synthesis", "about": "An `include names a file that isn't found."},
    {"id": "implicit-wire", "rx": _IMPLICIT_RX, "fn": _implicit, "source": "forced: an undeclared wire on blinky",
     "stage": "synthesis", "about": "A wire is used without being declared; lint only warns, synthesis stops."},
    {"id": "module-twice", "rx": _REDEF_RX, "fn": _redefined, "source": "forced: an old copy of blinky.v in src/",
     "stage": "synthesis", "about": "Two design files declare the same module."},
    {"id": "sv-frontend", "rx": _SV_TOK_RX, "fn": _sv_frontend, "source": "forced: import inside a module, USE_SLANG false",
     "stage": "synthesis", "about": "SystemVerilog the default Yosys reader doesn't take (import, interfaces)."},
    # Verilator (%Error: f.v:67:13: syntax error, …) and Yosys (f.v:67: ERROR: …).
    {"id": "verilog-error", "rx": _VERILOG_RX, "fn": lambda ev, m: _verilog_error(ev, _Groups(m)),
     "source": "forced: a missing semicolon, linter on and off", "stage": "synthesis",
     "about": "A syntax error, or anything else the RTL readers refuse, with its file and line."},
    # The config doesn't load: LibreLane stops before the first step.
    {"id": "config-value",
     "rx": re.compile(r"Value provided for variable '(?P<var>[A-Z0-9_]+)(?:\[\d+\])?' of type (?P<type>\w+) is invalid: "
                      r"'(?P<val>[^'\n]{0,120})'"),
     "fn": _bad_value, "source": "forced: FP_CORE_UTIL \"abc\" on blinky", "stage": "config",
     "about": "A setting has a value of the wrong type."},
    {"id": "config-required", "rx": re.compile(r"Required variable '(?P<var>[A-Z0-9_]+)' did not get a specified value"),
     "fn": _required, "source": "LibreLane 3.0.4 config/config.py (text)", "stage": "config",
     "about": "A setting LibreLane needs isn't set."},
    {"id": "pdk-missing", "rx": re.compile(r"The PDK (?P<pdk>[^\s.]+(?:\.[^\s.]+)*) was not found\."),
     "fn": _pdk_missing, "source": "forced: PDK sky130C on blinky", "stage": "config",
     "about": "The PDK the run asks for isn't installed."},
    {"id": "corner-wildcard", "rx": re.compile(r"One or more wildcards specified in (?P<var>[A-Z_]+) did not match any corners"),
     "fn": _corner_wildcard, "source": "forced: TIMING_VIOLATION_CORNERS [*xyz*] on blinky", "stage": "signoff",
     "about": "A corner setting matches none of the PDK's timing corners."},
    {"id": "top-missing",
     "rx": re.compile(r"(?:Specified --top-module '(?P<top>[^']+)' was not found in design"
                      r"|ERROR: Module `(?P<top2>[^']+)' not found!)"),
     "fn": lambda ev, m: _top_missing(ev, _Groups(m)), "source": "forced: DESIGN_NAME spm_top, linter on and off", "stage": "synthesis",
     "about": "DESIGN_NAME names a module no design file declares."},
    {"id": "path-missing",
     "rx": re.compile(r"Path provided for variable '(?P<var>[A-Z0-9_]+)(?:\[\d+\])?' is invalid: '(?P<path>[^']+)' does not exist"),
     "fn": _path_missing, "source": "forced: VERILOG_FILES a missing file, and a glob matching nothing", "stage": "config",
     "about": "A file the config lists isn't there, or a pattern matches no file."},
    {"id": "disk-full", "rx": re.compile(r"No space left on device|\[Errno 28\][^\n]*|Disk quota exceeded"),
     "fn": _disk_text, "source": "forced: runs/ on a 4 MB tmpfs (OSError: [Errno 28])", "stage": "any",
     "about": "The disk the run writes to is full."},
    {"id": "oom", "rx": re.compile(r"std::bad_alloc|Interrupted \(SIGKILL\)|MemoryError|Cannot allocate memory|exit code 137\b"),
     "fn": _oom_text, "source": "LibreLane 3.0.4 step.py (Interrupted (signal)); forced: docker --memory 150m (exit 137)", "stage": "any",
     "about": "The system ran out of memory and stopped the tool."},
    {"id": "docker-permission",
     "rx": re.compile(r"permission denied while trying to connect to the docker[^\n]*", re.I),
     "fn": _env("This user can't use Docker",
                "Docker is running, but this user isn't allowed to talk to it: Linux lets only root and the members of "
                "the docker group use it. Add yourself to the group (sudo usermod -aG docker $USER), then log out "
                "and back in (on Windows: wsl --shutdown, then start LanEx again).", _TOOLS_FIX),
     "source": "text in the docker CLI on this PC", "stage": "engine",
     "about": "This user isn't allowed to use Docker."},
    {"id": "image-pull",
     "rx": re.compile(r"Failed to pull image '[^']+'[^\n]*|Failed to use image '[^']+'|Couldn't connect to the internet to pull "
                      r"container images|manifest unknown|pull access denied[^\n]*|toomanyrequests[^\n]*", re.I),
     "fn": _env("Couldn't get the LibreLane image",
                "Container mode runs every tool inside LibreLane's image, and it couldn't be downloaded. Check the "
                "internet connection (and any proxy), then pull it again from Tools. \"toomanyrequests\" means the "
                "registry is limiting downloads: wait a while.", _TOOLS_FIX),
     "source": "LibreLane 3.0.4 container.py; docker's own words", "stage": "engine",
     "about": "The LibreLane container image couldn't be downloaded."},
    {"id": "no-engine", "rx": re.compile(r"No compatible container engine found\."),
     "fn": _env("No container engine is installed",
                "Container mode needs Docker or Podman, and neither was found. Install one from Tools, or switch the "
                "engine to Local tools in the Run menu.", _TOOLS_FIX),
     "source": "LibreLane 3.0.4 container.py", "stage": "engine",
     "about": "Neither Docker nor Podman is installed."},
    {"id": "engine-down",
     "rx": re.compile(r"cannot connect to the docker daemon[^\n]*|is the docker daemon running|cannot connect to podman[^\n]*"
                      r"|error during connect[^\n]*", re.I),
     "fn": _env("The container engine isn't running",
                "Container mode needs Docker or Podman running. Start it (or switch the engine to Local tools in the "
                "Run menu), then run again.", _TOOLS_FIX),
     "source": "the docker CLI's words", "stage": "engine",
     "about": "The container engine isn't running."},
    {"id": "windows-docker", "rx": re.compile(r"--dockerized is not supported on windows", re.I),
     "fn": _env("Containers need WSL on Windows",
                "LibreLane's container mode needs a Linux container engine; run LanEx inside its WSL appliance."),
     "source": "LibreLane 3.0.4", "stage": "engine",
     "about": "Container mode needs LanEx's WSL environment on Windows."},
]


class _Groups:
    """A match whose named groups come in two wordings (``top``/``top2``):
    ``group("top")`` is whichever matched."""

    def __init__(self, m: "re.Match[str]") -> None:
        self._m = m

    def group(self, key: Any = 0) -> str:
        if isinstance(key, str):
            v = self._m.groupdict().get(key)
            return v if v is not None else (self._m.groupdict().get(key + "2") or "")
        return self._m.group(key)


_LOCAL_TOOLS = ("yosys", "openroad", "magic", "klayout", "netgen", "verilator")
_MISSING_RX = re.compile(r"\b(" + "|".join(_LOCAL_TOOLS) + r")\b[^\n]{0,40}(command not found|no such file or directory|not found)",
                         re.I)


def _match(rule: Dict[str, Any], ev: Dict[str, Any], code: Optional[str]) -> Any:
    if rule.get("steps") and not any(str(ev.get("step") or "").startswith(s) for s in rule["steps"]):
        return None
    if code and code in [rule.get("code")] + list(rule.get("also") or []):
        return True
    rx = rule.get("rx")
    if rx is None:
        return None
    for where in ("text", "log", "outcome"):
        m = rx.search(ev.get(where) or "")
        if m:
            return m
    return None


def explain_error(text: str, cfg: Dict[str, Any], *, step: Optional[str] = None, log: Optional[str] = None,
                  outcome: Optional[str] = None, run_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The failure card for a step that raised: ``{code, kb, title, plain,
    fixes, learn}`` (title None when LanEx has nothing to add to the tool's
    words). *text* is error.log (or the step's first error), *log* the end of
    the step's own log, *outcome* how the run's process ended."""
    m = _CODE_RX.search(text or "")
    code = m.group(1) if m else None
    ev = {"text": text or "", "cfg": cfg or {}, "step": step, "log": log or "", "outcome": outcome or "",
          "run_dir": run_dir}
    card = None
    for rule in KB:
        hit = _match(rule, ev, code)
        if hit is None:
            continue
        card = rule["fn"](ev, hit)
        if card is not None:
            card.setdefault("kb", rule["id"])
            break
    if card is None:
        tool = _MISSING_RX.search(text or "")
        if tool:
            # Local tools mode runs the tools on this machine's PATH.
            card = {"title": tool.group(1).capitalize() + " isn't installed here", "kb": "local-tool",
                    "plain": "Local tools mode runs the tools installed on this machine, and " + tool.group(1) +
                             " isn't on the PATH. Switch the engine to Container in the Run menu (the LibreLane image has "
                             "every tool at the matching version), or install it from Tools.",
                    "fixes": [], "learn": []}
    if card is None:
        card = {"title": None, "plain": None, "fixes": [], "learn": []}
    card["code"] = code
    return card


# ---------------------------------------------------------------- no message
# A step can end with nothing in error.log and no error in its log: the system
# stopped the tool (out of memory), the disk filled, or the window closed.
# Forced on scratch designs: OpenROAD in a 260 MB container (LibreLane:
# "<step> failed with an unexpected error.", error.log empty, the log just
# ends), the whole run in 150 MB (exit 137), runs/ on a 4 MB disk.

def _meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = ln.partition(":")
            n = v.strip().split()
            if n and n[0].isdigit():
                out[k.strip()] = int(n[0]) * 1024
    except OSError:
        pass
    return out


def _gb(n: Optional[float]) -> str:
    if n is None:
        return "—"
    if n >= 1 << 30:
        return "%.1f GB" % (n / (1 << 30))
    return "%d MB" % round(n / (1 << 20))


def _peak(run_dir: Optional[Path], step_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    """The stopped step's peak memory from LibreLane's own ``*.process_stats.json``."""
    if not run_dir or not step_dir:
        return None
    try:
        import json
        p = next(iter(sorted((Path(run_dir) / step_dir).glob("*.process_stats.json"))), None)
        doc = json.loads(p.read_text(encoding="utf-8")) if p else None
        rss = ((doc or {}).get("peak_resources") or {}).get("memory_rss")
        return {"rss": str(rss)} if rss else None
    except Exception:
        return None


def disk_free(path: Optional[Path]) -> Optional[Dict[str, int]]:
    import shutil
    try:
        u = shutil.disk_usage(str(path))
        return {"free": u.free, "total": u.total}
    except Exception:
        return None


def _disk_low(d: Optional[Dict[str, int]]) -> bool:
    return bool(d and (d["free"] < (1 << 30) or d["free"] < 0.02 * d["total"]))


def _disk_card(ev: Dict[str, Any], said: str = "") -> Dict[str, Any]:
    d = disk_free(ev.get("run_dir"))
    plain = ("The disk the run writes to filled up, so the step couldn't write its results%s. " %
             ((" (\"" + said[:120] + "\")") if said else ""))
    if d:
        plain += "Right now %s of %s is free there. " % (_gb(d["free"]), _gb(d["total"]))
    plain += ("Make room (old runs in Runs are the usual place: each keeps every step's layout), then run again. "
              "On Windows the WSL disk grows only up to its own limit.")
    return {"title": "The disk is full", "plain": plain, "kb": "disk-full",
            "fixes": [_open("Show the runs", {"tab": "runs"}, "Old runs, biggest first: delete the ones you don't need.")],
            "learn": [], "diag": {"disk": d}}


def _oom_card(ev: Dict[str, Any], *, sure: bool, said: str = "") -> Dict[str, Any]:
    mem = _meminfo()
    peak = ev.get("peak")
    step = str(ev.get("step") or "the step")
    parts = []
    if peak:
        parts.append("%s used up to %s before it stopped" % (step, peak["rss"].replace("MiB", " MB").replace("GiB", " GB")))
    if mem.get("MemTotal"):
        parts.append("this machine has %s, %s free right now" % (_gb(mem["MemTotal"]), _gb(mem.get("MemAvailable"))))
    numbers = ("; ".join(parts) + ". ") if parts else ""
    if sure:
        title = "Out of memory: the system stopped the run"
        plain = ("The system ran out of memory and stopped the tool%s. %s" % (
            (" (\"" + said[:80] + "\")") if said else "", numbers[0].upper() + numbers[1:] if numbers else ""))
    else:
        title = "The tool stopped without saying why"
        plain = ("%s ended without an error message: the system stopped it, most often because memory ran out, or "
                 "the run's window or container was closed. %s" % (step, numbers[0].upper() + numbers[1:] if numbers else ""))
    plain += ("Close other programs and run again. On Windows, WSL gets half of the PC's memory unless .wslconfig says "
              "otherwise; a bigger design needs more.")
    return {"title": title, "plain": plain, "kb": "oom" if sure else "silent", "fixes": [], "learn": [],
            "diag": {"memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable")}, "peak": peak}}


def explain_silent(run_dir: Path, step: Optional[Dict[str, Any]], outcome: Optional[str] = None) -> Dict[str, Any]:
    """The card for a step that stopped with no error anywhere: the disk if it
    is (nearly) full now, else memory, with the numbers this machine has."""
    ev = {"run_dir": run_dir, "step": (step or {}).get("id") or (step or {}).get("dir"),
          "peak": _peak(run_dir, (step or {}).get("dir"))}
    if _disk_low(disk_free(run_dir)):
        return _disk_card(ev)
    sure = bool(re.search(r"\b137\b|SIGKILL|bad_alloc", outcome or ""))
    return _oom_card(ev, sure=sure)


# ---------------------------------------------------------------- LVS that crashed
def lvs_crash(log_line: str) -> Dict[str, Any]:
    """Netgen.LVS stopped (no LVS verdict at all), as opposed to Checker.LVS
    failing on a mismatch. Forced: Netgen.LVS on a garbage SPICE netlist."""
    return {"title": "LVS didn't finish", "kb": "lvs-crash",
            "plain": "Netgen stopped with an error before it could compare the layout with the netlist, so there is no "
                     "LVS result at all (a mismatch would say which nets differ). Netgen's own words are below; usually "
                     "one of the two netlists it reads (the extracted SPICE or the synthesis netlist) is missing or "
                     "damaged, or a cell has no SPICE model.",
            "evidence": [log_line] if log_line else [],
            "fixes": [_open("Open LVS in Verify", {"tab": "verify", "section": "vf-lvs"},
                            "The LVS reports and the two netlists it read.")], "learn": []}


# Warnings worth a sentence: what happened, with the log's numbers.
def _note_gpl_0302(text: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    m = re.search(r"Target density (\d+(?:\.\d+)?).*?uniform density (\d+(?:\.\d+)?)", text, re.S)
    if not m:
        m2 = re.search(r"Target density (\d+(?:\.\d+)?)", text)
        return ("The placement density (PL_TARGET_DENSITY_PCT, %.0f %%) is below what this design can "
                "reach, so OpenROAD raised it. Harmless; set PL_TARGET_DENSITY_PCT higher to silence it."
                % (float(m2.group(1)) * 100)) if m2 else None
    # Often LibreLane's own default (a small design's first run), so it never
    # says "your setting" and only says how to make it go away.
    a, b = float(m.group(1)) * 100, float(m.group(2)) * 100
    return ("The placement density (PL_TARGET_DENSITY_PCT, %.0f %%) is below what this design can reach, "
            "so OpenROAD used %.0f %% instead. Harmless; set it to %d or more to silence this."
            % (a, b, math.ceil(b)))


def _note_sta_0366(text: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    # [STA-0366] port 'clock' not found.  (forced: CLOCK_PORT clock on spm;
    # CLOCK_PORT null gives port '__VIRTUAL_CLK__')
    m = re.search(r"port '([^']+)' not found", text)
    if not m:
        return None
    port, clk = m.group(1), (cfg or {}).get("CLOCK_PORT")
    if port == "__VIRTUAL_CLK__":
        if not clocked_logic(cfg or {}):
            return combinational_note(cfg or {})
        return ("No clock port is set (CLOCK_PORT is empty), so LibreLane timed the design against a made-up clock. "
                "Registers clocked by a real input were never checked. Set CLOCK_PORT if the design has a clock.")
    if clk and port in (clk if isinstance(clk, list) else [clk]):
        return ("CLOCK_PORT is '%s', and the design has no port by that name, so the clock was never created: no "
                "register-to-register path was timed, and \"timing met\" means nothing. Set CLOCK_PORT to the "
                "design's clock input." % port)
    return "The SDC names a port '%s' that the design doesn't have; that constraint did nothing." % port


_CLOCKED_RX = re.compile(r"\b(?:posedge|negedge|always_ff|always_latch)\b")


def clocked_logic(cfg: Dict[str, Any]) -> Optional[bool]:
    """Whether the run's RTL has any clocked block (an edge, always_ff or a
    latch); None when no source could be read."""
    seen = False
    for f in cfg.get("VERILOG_FILES") or []:
        try:
            text = Path(str(f)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen = True
        if _CLOCKED_RX.search(re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)):
            return True
    if seen and any(re.search(r"clk|clock", p, re.I) for p in _top_ports(cfg)["input"]):
        return None        # an input named like a clock: not sure it is pure logic
    return False if seen else None


def combinational_note(cfg: Dict[str, Any]) -> str:
    """A design with no register: its paths are still timed, from the inputs
    to the outputs, against base.sdc's virtual clock. Said as information,
    not as a problem (the 7-segment template is such a design)."""
    per, io = fmt(cfg.get("CLOCK_PERIOD") or 10), fmt(cfg.get("IO_DELAY_CONSTRAINT") or 20)
    return ("A design with no clock: LibreLane timed the paths from the inputs to the outputs against a virtual "
            "clock of %s ns, with %s %% of it given to the outside world on each side (IO_DELAY_CONSTRAINT). That "
            "is what the timing results measure." % (per, io))


def _note_cts_0083(text: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    # [WARNING CTS-0083] No clock nets have been found.  (forced: CLOCK_PORT clock)
    if not (cfg or {}).get("CLOCK_PORT") and clocked_logic(cfg or {}) is False:
        return "No clock tree was needed: the design has no registers."
    return ("The clock tree step found no clock net, so no clock tree was built: the registers' clock pins are "
            "driven straight from the pin. Usually CLOCK_PORT doesn't name the clock input.")


WARNING_NOTES = {"GPL-0302": _note_gpl_0302, "STA-0366": _note_sta_0366, "CTS-0083": _note_cts_0083,
                 "CTS-0082": _note_cts_0083}


# ---------------------------------------------------------------- timing
def _pvt(corner: str) -> str:
    """``max_ss_100C_1v60`` -> ``ss_100C_1v60``: the process, temperature and
    voltage, without the wire corner (before placement there are no wires)."""
    return re.sub(r"^(nom|min|max)_", "", corner or "")


def _io_paths(kinds: Dict[str, int]) -> int:
    return sum(n for k, n in kinds.items() if k != "reg-reg")


def _kinds_text(kinds: Dict[str, int]) -> str:
    words = {"reg-reg": "register to register", "in-reg": "from an input", "reg-out": "to an output",
             "in-out": "input straight to an output"}
    order = ["reg-reg", "in-reg", "reg-out", "in-out"]
    return ", ".join("%d %s" % (kinds[k], words[k]) for k in order if kinds.get(k))


def _sdc_reads(cfg: Dict[str, Any], var: str) -> bool:
    """Whether the run's SDC reads *var*. LibreLane's own (scripts/base.sdc)
    spends IO_DELAY_CONSTRAINT on each input and output and puts
    OUTPUT_CAP_LOAD on every output; a design's own SDC may or may not."""
    sdc = cfg.get("PNR_SDC_FILE")
    if not sdc:
        return True
    try:
        return var in Path(str(sdc)).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False


def _relax_clock(period: float, wns: float, io_pct: float, through_io: bool) -> float:
    """A clock period the worst path would meet: on an input or output path
    the I/O delay grows with the period too (IO_DELAY_CONSTRAINT %)."""
    slope = (1 - io_pct / 100.0) if through_io else 1.0
    need = period + abs(wns) / max(0.2, slope)
    return _round_up(need, 0.1 if need < 20 else 1.0)


def timing_problems(run_dir: Path, rep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Setup and hold, diagnosed from every timing analysis of the run."""
    from . import timing
    by = {c["id"]: c for c in rep.get("checks") or []}
    cfg = _cfg(run_dir)
    hist = timing.sta_history(run_dir)
    if not hist:
        return []
    final = next((h for h in reversed(hist) if h["signoff"]), None) or hist[-1]
    out = []
    s = _setup(run_dir, cfg, hist, final, by.get("setup"))
    if s:
        out.append(s)
    h = _hold(run_dir, cfg, hist, final, by.get("hold"))
    if h:
        out.append(h)
    return out


def _worst(corners: Dict[str, Any], key: str) -> Optional[str]:
    bad = [(v[key], c) for c, v in corners.items() if _num(v.get(key)) is not None and v[key] < 0]
    return min(bad)[1] if bad else None


def _setup(run_dir: Path, cfg: Dict[str, Any], hist: List[Dict[str, Any]], final: Dict[str, Any],
           check: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    from . import timing
    wc = _worst(final["corners"], "setup_ws")
    if wc is None or not check or check.get("status") not in ("fail", "warn"):
        return None
    w = final["corners"][wc]
    failing = sorted(c for c, v in final["corners"].items() if _num(v.get("setup_ws")) is not None and v["setup_ws"] < 0)
    passing = [c for c in final["corners"] if c not in failing]
    period = _num(cfg.get("CLOCK_PERIOD"))
    io_pct = _num(cfg.get("IO_DELAY_CONSTRAINT")) or 20.0
    kinds = w["kinds"].get("setup") or {}
    # Where this corner first failed: the first analysis that timed it.
    first = None
    for h in hist:
        v = next((x for c, x in h["corners"].items() if _pvt(c) == _pvt(wc)), None)
        if v and _num(v.get("setup_ws")) is not None and v["setup_ws"] < 0:
            first = (h, v, next(c for c in h["corners"] if _pvt(c) == _pvt(wc)))
            break
    before_pnr = bool(first and first[0]["ideal_clocks"])
    first_kinds = (first[1]["kinds"].get("setup") or {}) if first else {}
    # Each kind of path failing at sign-off, and whether it already failed
    # before placement (ideal clocks, no wires): a path that fails there is
    # the logic or the constraints, never placement.
    pre_k = first_kinds if before_pnr else {}
    io_n, rr_n = _io_paths(kinds), kinds.get("reg-reg", 0)
    if not kinds and before_pnr:                  # no violator list: one group
        io_n, rr_n = _io_paths(pre_k), pre_k.get("reg-reg", 0) or (0 if _io_paths(pre_k) else 1)
    early_io = bool(io_n and _io_paths(pre_k))
    early_rr = bool(rr_n and pre_k.get("reg-reg"))
    late = (io_n and not early_io) or (rr_n and not early_rr)
    # The first analysis after placement that timed this corner and failed.
    later = next((h for h in hist if not h["ideal_clocks"] and any(
        _pvt(c) == _pvt(wc) and _num(x.get("setup_ws")) is not None and x["setup_ws"] < 0 for c, x in h["corners"].items())), None)
    late_when = later["when"] if later else "after routing"
    untimed = later is not None and later["signoff"] and _pvt(wc) != _pvt(str(cfg.get("DEFAULT_CORNER") or ""))
    path = timing.worst_setup_path(run_dir, final["path"], wc)
    first_path = timing.worst_setup_path(run_dir, first[0]["path"], first[2]) if before_pnr else None
    budget = (path or {}).get("budget") or {}
    gating = bool(check and check.get("status") == "fail")
    sign = "after routing" if final["signoff"] else "in the last timing analysis (" + final["when"] + ", an estimate)"
    pre_txt = ("(%s, %d endpoints, ideal clocks and no wires)" % (_ns(first[1]["setup_ws"]), first[1].get("setup_vio") or 0)
               if before_pnr else "")

    leads = []
    if early_io:
        leads.append("Paths from inputs and to outputs already failed before placement %s: check the I/O "
                     "constraint first; placement can't help them." % pre_txt)
    if early_rr:
        leads.append("Register-to-register paths already failed before placement %s: too much logic between "
                     "registers for this clock (logic depth). Placement and routing settings can't fix that." % pre_txt)
    if late:
        if rr_n and not early_rr and (early_io or early_rr):
            what = ("A register-to-register path is the worst for %d endpoint%s %s and for none before placement"
                    % (rr_n, "" if rr_n == 1 else "s", late_when))
        else:
            what = "It passed before placement and fails %s" % late_when
        leads.append("%s: wires, clock skew or fanout, a physical problem.%s"
                     % (what, " (LibreLane times this corner only before placement and after routing.)" if untimed else ""))
    if early_io and late and not early_rr:
        title = "Setup: the I/O paths fail from the start, register paths " + late_when
    elif (early_io or early_rr) and not late:
        title = ("Setup fails before placement, on the input and output paths" if early_io and not early_rr
                 else "Setup fails before placement: too much logic for this clock")
    elif late:
        title = "Setup fails only " + late_when + ": a physical problem"
    else:
        title = "Setup fails in the slow corners" if passing else "Setup fails"
    plain = ("Worst slack %s in %s (%s): %d endpoints miss the clock there, %s short in all (TNS). "
             % (_ns(w["setup_ws"]), wc, sign, w.get("setup_vio") or 0,
                _ns(w.get("setup_tns")).replace("−", "") if _num(w.get("setup_tns")) is not None else "—")) + " ".join(leads)
    if passing and not gating:
        plain += " The corners LibreLane fails a run on pass, so this is a warning."
    evidence = []
    if kinds:
        evidence.append("Failing at sign-off in %s: %s." % (wc, _kinds_text(kinds)))
    if first_kinds and before_pnr:
        evidence.append("Failing before placement in %s: %s." % (first[2], _kinds_text(first_kinds)))

    fixes: List[Dict[str, Any]] = []
    through_io = budget.get("start") == "in" or budget.get("end") == "out"
    if period:
        to = _relax_clock(period, w["setup_ws"], io_pct, through_io)
        fixes.append(_setting("CLOCK_PERIOD", period, to,
                              "The direct fix: %s ns is about %.0f MHz. The worst path needs about %.2f ns more%s."
                              % (fmt(to), 1000.0 / to, abs(w["setup_ws"]),
                                 ", and the I/O delays grow with the clock" if through_io else ""),
                              "Slow the clock"))
    if (early_io or (through_io and io_n >= max(1, (io_n + rr_n) // 2))) and _sdc_reads(cfg, "IO_DELAY_CONSTRAINT"):
        to = max(5, int(io_pct // 2))
        fixes.append(_setting("IO_DELAY_CONSTRAINT", io_pct, to,
                              "LibreLane's SDC gives %s %% of the clock to the world outside the chip on every input and "
                              "output. Lower it only if your inputs really arrive that early." % fmt(io_pct),
                              "Check the I/O constraint"))
    load = _num(cfg.get("OUTPUT_CAP_LOAD")) if _sdc_reads(cfg, "OUTPUT_CAP_LOAD") else None
    if load and budget.get("end") == "out" and kinds.get("reg-out", 0) + kinds.get("in-out", 0) > 0:
        fixes.append(_setting("OUTPUT_CAP_LOAD", load, round(load / 2, 3),
                              "The SDC puts %s fF on every output pin, and the worst path ends at one: the cell driving "
                              "the pin is slower the bigger that load. Lower it only to what the pin really drives." % fmt(load),
                              "Check the output load"))
    if early_rr and str(cfg.get("SYNTH_STRATEGY") or "").startswith("AREA"):
        fixes.append(_setting("SYNTH_STRATEGY", cfg.get("SYNTH_STRATEGY"), "DELAY 0",
                              "Synthesis that favours speed over area. LibreLane: there is no way to know which "
                              "strategy is best before trying them.", "Synthesise for speed"))
    if late:
        if cfg.get("PL_TIMING_DRIVEN") is False:
            fixes.append(_setting("PL_TIMING_DRIVEN", False, True,
                                  "Placement that keeps the slow paths' cells close (off by default).",
                                  "Timing-driven placement"))
        if cfg.get("RUN_POST_GRT_RESIZER_TIMING") is False:
            fixes.append(_setting("RUN_POST_GRT_RESIZER_TIMING", False, True,
                                  "A second timing repair with the real routing estimates (off by default).",
                                  "Repair timing after routing too"))
        if abs(w["setup_ws"]) <= 0.1 and _num(cfg.get("PL_RESIZER_SETUP_SLACK_MARGIN")) is not None:
            m = _num(cfg.get("PL_RESIZER_SETUP_SLACK_MARGIN"))
            fixes.append(_setting("PL_RESIZER_SETUP_SLACK_MARGIN", m, round(m + 0.1, 3),
                                  "A small miss: repair aims a little past zero slack.", "Repair with more margin"))
    start = (path or {}).get("startpoint") if budget.get("start") == "reg" else None
    fixes.append(_open("Show the worst paths", {"tab": "analytics", "section": "an-timing"},
                       "Every failing path, worst first, with its cells."))
    notes = []
    if (early_io or early_rr) and late:
        notes.append("Placement settings are offered only for the paths that fail after routing. The ones that "
                     "fail before placement have no wires yet to shorten, so no placement or density setting can help them.")
    elif early_io or early_rr:
        notes.append("No placement or density setting is offered: these paths fail before placement, with no wires "
                     "yet to shorten.")
    if kinds or first_kinds:
        notes.append("Each failing endpoint is counted once, by its worst path (LibreLane's violator list).")
    if passing:
        notes.append("Passing: " + ", ".join(sorted(passing)) + ".")
    return {
        "id": "timing-setup", "severity": "fail" if gating else "warn", "kind": "check", "title": title,
        "plain": plain, "where": {"check": "setup", "stage": "signoff"}, "evidence": evidence,
        "diag": {"kind": "setup", "corner": wc, "ws": w["setup_ws"], "tns": w.get("setup_tns"),
                 "endpoints": w.get("setup_vio"), "kinds": kinds, "failing": failing,
                 "first": ({"when": first[0]["when"], "step": first[0]["id"], "corner": first[2],
                            "ws": first[1]["setup_ws"], "endpoints": first[1].get("setup_vio"),
                            "kinds": first_kinds, "run": first[0]["run"]} if first else None),
                 "before_placement": before_pnr, "io_first": early_io, "logic_depth": early_rr, "physical": bool(late),
                 "timeline": _timeline(hist, "setup_ws", wc), "path": path, "first_path": first_path,
                 "start_register": start, "period": period, "io_pct": io_pct, "out_load": load},
        "fixes": fixes, "notes": notes, "learn": ["slack", "wns", "tns"],
    }


def _timeline(hist: List[Dict[str, Any]], key: str, corner: str) -> List[Dict[str, Any]]:
    """Worst slack after each analysis: in the failing corner where it was
    timed, else in the corner the step did time (mid-PnR: the default one)."""
    out = []
    for h in hist:
        v = next(((c, x) for c, x in h["corners"].items() if _pvt(c) == _pvt(corner)), None)
        if v is None and h["corners"]:
            v = min(((c, x) for c, x in h["corners"].items() if _num(x.get(key)) is not None),
                    key=lambda t: t[1][key], default=None)
        if v is None:
            continue
        out.append({"when": h["when"], "step": h["id"], "corner": v[0], "ws": v[1].get(key),
                    "same_corner": _pvt(v[0]) == _pvt(corner), "ideal": h["ideal_clocks"], "signoff": h["signoff"]})
    return out


def _hold(run_dir: Path, cfg: Dict[str, Any], hist: List[Dict[str, Any]], final: Dict[str, Any],
          check: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    wc = _worst(final["corners"], "hold_ws")
    if wc is None or not check or check.get("status") not in ("fail", "warn"):
        return None
    w = final["corners"][wc]
    kinds = w["kinds"].get("hold") or {}
    gating = bool(check and check.get("status") == "fail")
    fixes = []
    m = _num(cfg.get("PL_RESIZER_HOLD_SLACK_MARGIN"))
    if m is not None:
        fixes.append(_setting("PL_RESIZER_HOLD_SLACK_MARGIN", m, round(m + abs(w["hold_ws"]) + 0.05, 3),
                              "Hold repair after the clock tree aims this far past zero, so it adds the buffers the "
                              "failing paths need.", "Repair hold with more margin"))
    if cfg.get("RUN_POST_GRT_RESIZER_TIMING") is False:
        fixes.append(_setting("RUN_POST_GRT_RESIZER_TIMING", False, True,
                              "A second repair with the routing estimates (off by default).", "Repair again after routing"))
    else:
        g = _num(cfg.get("GRT_RESIZER_HOLD_SLACK_MARGIN"))
        if g is not None:
            fixes.append(_setting("GRT_RESIZER_HOLD_SLACK_MARGIN", g, round(g + abs(w["hold_ws"]) + 0.05, 3),
                                  "The repair after routing aims this far past zero.", "More margin after routing"))
    plain = ("Worst hold slack %s in %s: %d endpoints. New data reaches a register too soon after the clock edge. "
             "A slower clock doesn't change hold, so none is offered; the repair steps add delay instead."
             % (_ns(w["hold_ws"]), wc, w.get("hold_vio") or 0))
    return {
        "id": "timing-hold", "severity": "fail" if gating else "warn", "kind": "check",
        "title": "Hold fails" + (" in the fast corners" if "ff" in wc else ""), "plain": plain,
        "where": {"check": "hold", "stage": "signoff"},
        "evidence": ["Failing at sign-off in %s: %s." % (wc, _kinds_text(kinds))] if kinds else [],
        "diag": {"kind": "hold", "corner": wc, "ws": w["hold_ws"], "tns": w.get("hold_tns"),
                 "endpoints": w.get("hold_vio"), "kinds": kinds},
        "fixes": fixes, "notes": ["Hold before the clock tree is built is an estimate (ideal clocks), so it isn't "
                                  "used to say where hold first failed."], "learn": ["hold"],
    }


# ---------------------------------------------------------------- DRC
_LAYERS = {"li": "local interconnect", "li1": "local interconnect", "met1": "metal 1", "met2": "metal 2",
           "met3": "metal 3", "met4": "metal 4", "met5": "metal 5", "m1": "metal 1", "m2": "metal 2", "m3": "metal 3",
           "m4": "metal 4", "m5": "metal 5", "via": "via 1", "via1": "via 1", "via2": "via 2", "via3": "via 3",
           "via4": "via 4", "mcon": "metal 1 contacts", "licon": "local interconnect contacts", "poly": "poly",
           "diff": "diffusion", "nwell": "n-well", "metal1": "metal 1", "metal2": "metal 2", "metal3": "metal 3",
           "metal4": "metal 4", "metal5": "metal 5", "local interconnect": "local interconnect"}
_KIND_WORDS = {"spacing": "shapes closer than", "width": "narrower than", "enclosure": "enclosed by less than",
               "extension": "extending less than", "area": "smaller than", "overlap": "overlapping by less than"}


def rule_in_words(name: str, description: str = "") -> str:
    """A DRC rule in plain words when its text follows the usual patterns
    (``Metal1 width < 0.14um (met1.1)``, ``m1.2 : min. met1 spacing : 0.14um``),
    else the deck's own description."""
    for text in (description, name):
        t = (text or "").strip()
        m = re.match(r"^(?:[\w.]+\s*:\s*)?min\.?\s+(\w+)\s+(spacing|width|enclosure|extension|area)\s*:?\s*([\d.]+)\s*(um2?|µm2?)?",
                     t, re.I)
        if not m:
            m2 = re.match(r"^(.+?)\s+(spacing|width|enclosure|extension|area|overlap)\b.*?<\s*([\d.]+)\s*(um2?|µm2?)?", t, re.I)
            if m2:
                m = m2
        if m:
            layer = _LAYERS.get(m.group(1).strip().lower(), m.group(1).strip())
            kind = m.group(2).lower()
            unit = "µm²" if kind == "area" or (m.group(4) or "").endswith("2") else "µm"
            return "%s %s %s %s" % (layer[0].upper() + layer[1:], _KIND_WORDS[kind], m.group(3), unit)
    return (description or name or "").strip()


def _region(cx: float, cy: float, die: Optional[List[float]]) -> str:
    if not die:
        return ""
    w, h = die[2] - die[0], die[3] - die[1]
    if w <= 0 or h <= 0:
        return ""
    fx, fy = (cx - die[0]) / w, (cy - die[1]) / h
    col = "left" if fx < 1 / 3 else "right" if fx > 2 / 3 else ""
    row = "bottom" if fy < 1 / 3 else "top" if fy > 2 / 3 else ""
    if row and col:
        return row + "-" + col + " corner"
    if row or col:
        return (row or col) + " edge"
    return "middle"


def _die(run_dir: Path) -> Optional[List[float]]:
    from . import history
    bb = str(history._load_metrics(run_dir).get("design__die__bbox") or "").split()
    try:
        d = [float(x) for x in bb]
        return d if len(d) == 4 else None
    except ValueError:
        return None


def cluster(items: List[Dict[str, Any]], radius: float) -> List[Dict[str, Any]]:
    """Group markers whose centres lie within *radius* µm of each other
    (single linkage), the way an engineer reads "two places", not "150 errors"."""
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    cs = [((it["bbox"][0] + it["bbox"][2]) / 2, (it["bbox"][1] + it["bbox"][3]) / 2) for it in items]
    order = sorted(range(n), key=lambda i: cs[i][0])
    for a_i, a in enumerate(order):          # sweep by x: only neighbours within radius in x
        for b in order[a_i + 1:]:
            if cs[b][0] - cs[a][0] > radius:
                break
            if abs(cs[b][1] - cs[a][1]) <= radius:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idx in groups.values():
        bx = [items[i]["bbox"] for i in idx]
        rules: Dict[str, int] = {}
        for i in idx:
            rules[items[i]["cat"]] = rules.get(items[i]["cat"], 0) + 1
        out.append({"count": len(idx), "bbox": [min(b[0] for b in bx), min(b[1] for b in bx),
                                                max(b[2] for b in bx), max(b[3] for b in bx)],
                    "rules": sorted(({"name": k, "count": v} for k, v in rules.items()), key=lambda r: -r["count"])})
    out.sort(key=lambda g: -g["count"])
    return out


def drc_groups(run_dir: Path) -> Dict[str, Any]:
    """Each DRC checker's violations as rules and places, plus a note when
    the checkers disagree. Markers are the run's own report databases."""
    from . import layoutview
    die = _die(run_dir)
    diag = math.hypot(die[2] - die[0], die[3] - die[1]) if die else 100.0
    radius = max(5.0, 0.04 * diag)
    out = []
    for src in layoutview.markers(run_dir).get("sources") or []:
        if src.get("id") == "xor":
            continue
        cats = src.get("categories") or []
        places = cluster(src.get("items") or [], radius)
        for p in places:
            cx, cy = (p["bbox"][0] + p["bbox"][2]) / 2, (p["bbox"][1] + p["bbox"][3]) / 2
            p["center"] = [round(cx, 2), round(cy, 2)]
            p["where"] = _region(cx, cy, die)
        out.append({"id": src["id"], "label": src["label"], "total": src.get("total") or 0, "path": src.get("path"),
                    "rules": [{"name": c["name"], "count": c["count"], "description": c.get("description") or "",
                               "plain": rule_in_words(c["name"], c.get("description") or "")} for c in cats],
                    "places": places[:12], "more_places": max(0, len(places) - 12),
                    "listed": len(src.get("items") or [])})
    return {"sources": out, "radius": round(radius, 1), "die": die}


def checker_note(rep: Dict[str, Any], groups: Dict[str, Any]) -> Optional[str]:
    """When DRC checkers disagree, say why instead of showing two numbers."""
    by = {c["id"]: c for c in rep.get("checks") or []}
    val = lambda cid: _num((by.get(cid) or {}).get("value")) if (by.get(cid) or {}).get("status") in ("pass", "fail", "warn") else None  # noqa: E731
    mg, kl, rt = val("magic_drc"), val("klayout_drc"), val("route_drc")
    cfg = rep.get("_cfg") or {}
    parts = []
    if mg is not None and kl is not None and mg != kl:
        parts.append("Magic found %d and KLayout %d. LibreLane fails the run on either (ERROR_ON_MAGIC_DRC, "
                     "ERROR_ON_KLAYOUT_DRC), so both have to be clean; neither is the tiebreaker. Each runs its own rule "
                     "deck from the PDK, so the same problem can be one marker in one and several in the other, or "
                     "only in one." % (mg, kl))
    if mg is not None and kl is None:
        # LibreLane 3.0.4 has KLayout DRC decks for sky130A/B, gf180mcuA-D,
        # ihp-sg13g2 and ihp-sg13cmos5l (steps/klayout.py); RUN_KLAYOUT_DRC
        # turns the step off.
        pdk = str(rep.get("pdk") or cfg.get("PDK") or "")
        if cfg.get("RUN_KLAYOUT_DRC") is False:
            parts.append("KLayout DRC was turned off for this run (RUN_KLAYOUT_DRC), so Magic is the only DRC here.")
        elif pdk and not re.match(r"^(sky130|gf180mcu|ihp-sg13)", pdk):
            parts.append("KLayout DRC didn't run: LibreLane 3.0.4 has KLayout rule decks for sky130, gf180mcu and "
                         "ihp-sg13g2 only, so Magic is the only DRC for %s." % pdk)
    if rt == 0 and mg and mg > 0:
        parts.append("The router finished clean, yet Magic found %d. The router checks only its own wires, against the "
                     "routing rules it knows; Magic checks every shape in the final GDS, cells and power grid included." % mg)
    if cfg.get("MAGIC_DRC_USE_GDS") is False and mg:
        parts.append("Magic checked the DEF view (MAGIC_DRC_USE_GDS is off), which LibreLane says may be less accurate.")
    return " ".join(parts) or None


def drc_problems(run_dir: Path, rep: Dict[str, Any]) -> List[Dict[str, Any]]:
    by = {c["id"]: c for c in rep.get("checks") or []}
    bad = [cid for cid in ("magic_drc", "klayout_drc", "route_drc") if (by.get(cid) or {}).get("status") in ("fail", "warn")]
    if not bad:
        return []
    groups = drc_groups(run_dir)
    srcs = sorted((s for s in groups["sources"] if s["total"]), key=lambda s: -s["total"])
    total = sum(s["total"] for s in srcs)
    rules = {r["name"] for s in srcs for r in s["rules"] if r["count"]}
    places = sum(len(s["places"]) + s["more_places"] for s in srcs)
    head = srcs[0] if srcs else None
    if head:
        title = "%d DRC violation%s: %d rule%s in %d place%s" % (
            head["total"], "" if head["total"] == 1 else "s", len([r for r in head["rules"] if r["count"]]),
            "" if len([r for r in head["rules"] if r["count"]]) == 1 else "s",
            len(head["places"]) + head["more_places"], "" if len(head["places"]) + head["more_places"] == 1 else "s")
        title += " (" + head["label"] + ")"
    else:
        title = "DRC violations"
    fixes: List[Dict[str, Any]] = []
    if head and head["places"]:
        p = head["places"][0]
        fixes.append(_open("Show the biggest place in Layout", {"tab": "layout", "box": ",".join("%.2f" % v for v in p["bbox"])},
                           "%d violation%s %s." % (p["count"], "" if p["count"] == 1 else "s",
                                                   ("near the " + p["where"]) if p["where"] else "together")))
    if (by.get("route_drc") or {}).get("status") == "fail":
        # The detailed router left violations (Checker.TrDRC, deferred): room
        # first, in the order that usually clears them, then layers, and more
        # router passes last (by 64 it has usually converged).
        cfg = _cfg(run_dir)
        fixes += _route_fixes(cfg)
        top = str(cfg.get("RT_MAX_LAYER") or "")
        pdk_top = str(_pdk_default(cfg, "RT_MAX_LAYER") or "")
        if top and pdk_top and top != pdk_top:
            fixes.append(_setting("RT_MAX_LAYER", top, pdk_top, "Routing stops at %s here; the PDK routes up to %s, so "
                                  "the router gets more layers." % (top, pdk_top), "Route on more layers", sure="likely"))
        it = _num(cfg.get("DRT_OPT_ITERS"))
        if it:
            fixes.append(_setting("DRT_OPT_ITERS", it, int(it * 2), "Let the router keep trying longer. It has usually "
                                  "settled by then, so this helps least.", "More routing passes", sure="try"))
    rep2 = dict(rep, _cfg=_cfg(run_dir))
    note = checker_note(rep2, groups)
    return [{
        "id": "drc", "severity": "fail" if any((by.get(c) or {}).get("status") == "fail" for c in bad) else "warn",
        "kind": "check", "title": title,
        "plain": "%d in all, across %d rule%s and %d place%s. Engineers read DRC this way: a few rules in a few "
                 "places, where fixing one often clears several." % (total, len(rules), "" if len(rules) == 1 else "s",
                                                                        places, "" if places == 1 else "s"),
        "where": {"check": bad[0], "stage": "signoff"}, "evidence": [],
        "diag": {"kind": "drc", **groups}, "fixes": fixes, "notes": [note] if note else [], "learn": ["drc"],
    }]


# ---------------------------------------------------------------- other checks
# Settings a failing check can point at. Each entry: (variable, proposal, why);
# a proposal is a function of the run's current value.
def _minus(step: float, floor: float):
    return lambda v: None if v is None or v - step < floor else int(v - step) if float(v).is_integer() else round(v - step, 3)


def _plus(step: float):
    return lambda v: None if v is None else (int(v + step) if float(v).is_integer() and float(step).is_integer() else round(v + step, 3))


_CHECK_FIXES: Dict[str, List[Any]] = {
    # Antenna and slew advice depends on the run's own reports: see
    # _antenna_fixes and _slew_fixes.
    "disc_pin": [("FP_CORE_UTIL", _minus(10, 20), "Usually congestion: a bigger core leaves room for every wire.")],
    "cap": [("DESIGN_REPAIR_MAX_CAP_PCT", _plus(10), "Design repair aims this much under the capacitance limit (%).")],
}


def _top_ports(cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """The top module's ports by direction, from the run's Verilog sources."""
    top = str(cfg.get("DESIGN_NAME") or "")
    out: Dict[str, List[str]] = {"input": [], "output": [], "inout": []}
    for f in cfg.get("VERILOG_FILES") or []:
        try:
            text = Path(str(f)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
        m = re.search(r"\bmodule\s+" + re.escape(top) + r"\b(.*?)\bendmodule\b", text, re.S)
        if not m:
            continue
        for d, rest in re.findall(r"\b(input|output|inout)\b([^;)]*)", m.group(1)):
            rest = re.sub(r"\[[^\]]*\]|\b(?:wire|reg|logic|signed|unsigned|var)\b", " ", rest)
            for name in re.findall(r"[A-Za-z_]\w*", rest.split("=")[0]):
                if name not in ("input", "output", "inout") and name not in out[d]:
                    out[d].append(name)
        break
    return out


def _antenna_fixes(cfg: Dict[str, Any], rep: Dict[str, Any]) -> tuple:
    """Antenna fixes that match where the violations are (OpenROAD's
    antenna_summary.rpt rows: net, pin, layer). Protection diodes on the ports
    (DIODE_ON_PORTS) only when violating nets are top-level ports."""
    rows = (((rep.get("details") or {}).get("antenna") or {}).get("rows") or [])
    nets = sorted({r.get("net") or "" for r in rows} - {""})
    ports = _top_ports(cfg)
    base = lambda n: n.split("[", 1)[0]  # noqa: E731
    on_in = [n for n in nets if base(n) in ports["input"]]
    on_out = [n for n in nets if base(n) in ports["output"]]
    fixes: List[Dict[str, Any]] = []
    notes: List[str] = []
    diode = str(cfg.get("DIODE_CELL") or _pdk_default(cfg, "DIODE_CELL") or "").split("/")[0]
    diode_w = (" (" + diode + " on " + str(cfg.get("PDK") or "this PDK") + ")") if diode else ""
    # LibreLane 3.0.4 calls it RUN_ANTENNA_REPAIR; GRT_REPAIR_ANTENNAS is its
    # deprecated name, which resolved.json never holds (flows/classic.py).
    repair = cfg.get("RUN_ANTENNA_REPAIR", cfg.get("GRT_REPAIR_ANTENNAS"))
    if repair is False:
        fixes.append(_setting("RUN_ANTENNA_REPAIR", False, True, "Antenna repair after global routing is off in this "
                              "run; it adds diodes%s and jumps to higher layers where a wire passes the limit." % diode_w,
                              "Turn antenna repair on", sure="likely"))
    else:
        it = _num(cfg.get("GRT_ANTENNA_REPAIR_ITERS"))
        if it is not None:
            fixes.append(_setting("GRT_ANTENNA_REPAIR_ITERS", it, int(it) + 3,
                                  "More repair passes after global routing (the passes after detailed routing use the "
                                  "same margin).", "More repair passes", sure="try"))
        mg = _num(cfg.get("GRT_ANTENNA_REPAIR_MARGIN"))
        if mg is not None:
            fixes.append(_setting("GRT_ANTENNA_REPAIR_MARGIN", mg, int(mg) + 10,
                                  "Repair nets that come within this much of the limit (%), not only those over it.",
                                  "Repair with more margin", sure="try"))
        # Heuristic insertion needs cell padding to leave room for the diodes
        # (its own docstring: GPL_ and DPL_CELL_PADDING above 0).
        if cfg.get("RUN_HEURISTIC_DIODE_INSERTION") is False and len(nets) >= 5:
            pad = _num(cfg.get("GPL_CELL_PADDING")) or 0
            fixes.append(_setting("RUN_HEURISTIC_DIODE_INSERTION", False, True,
                                  "A diode%s on every net longer than %s µm, before routing. It needs room next to the "
                                  "cells%s." % (diode_w, fmt(cfg.get("HEURISTIC_ANTENNA_THRESHOLD") or "the PDK's threshold"),
                                                "" if pad > 0 else ": set GPL_CELL_PADDING and DPL_CELL_PADDING to 2 as well"),
                                  "Diodes on long nets", sure="try"))
    cur = str(cfg.get("DIODE_ON_PORTS") or "none")
    want = {"in": bool(on_in) or cur in ("in", "both"), "out": bool(on_out) or cur in ("out", "both")}
    to = "both" if want["in"] and want["out"] else "in" if want["in"] else "out" if want["out"] else None
    if to and to != cur:
        hit = on_in + on_out
        fixes.append(_setting("DIODE_ON_PORTS", cur, to, "%d of the %d violating nets %s top-level %s (%s): a diode%s "
                              "on the port protects them whatever the wire outside does." % (
                                  len(hit), len(nets), "is a" if len(hit) == 1 else "are",
                                  "port" if len(hit) == 1 else "ports", ", ".join(hit[:4]), diode_w),
                              "Diodes on the ports", sure="likely"))
    elif nets and not (on_in or on_out) and ports["input"]:
        notes.append("None of the violating nets is a top-level port, so diodes on the ports (DIODE_ON_PORTS) "
                     "wouldn't help here; the repair inside the design has to.")
    if cur == "none" and ports["input"]:
        notes.append("A block that will sit inside a bigger chip (a macro, a Tiny Tapeout tile, a Caravel project) "
                     "usually wants DIODE_ON_PORTS in whatever this run reports: the long wires to its inputs are in "
                     "the parent, which this run never sees.")
    return fixes, notes


def _slew_rows(run_dir: Path) -> Dict[str, List[tuple]]:
    """``{corner: [(pin, limit, slew)]}`` from each STAPostPNR corner's
    checks.rpt "max slew" section (OpenSTA report_check_types -violators)."""
    out: Dict[str, List[tuple]] = {}
    rx = re.compile(r"^(\S+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+-\d+(?:\.\d+)? \(VIOLATED\)")
    for rpt in sorted(Path(run_dir).glob("*-openroad-stapostpnr/*/checks.rpt")):
        try:
            text = rpt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sec = re.search(r"^max slew\s*\n(.*?)(?:^max capacitance|^max fanout|^=====|\Z)", text, re.S | re.M)
        if not sec:
            continue
        rows = [(m.group(1), float(m.group(2)), float(m.group(3))) for m in map(rx.match, sec.group(1).splitlines()) if m]
        if rows:
            out[rpt.parent.name] = rows
    return out


def _slew_fixes(cfg: Dict[str, Any], run_dir: Path) -> tuple:
    """Slew fixes in the order the evidence says. Pins of one net share its
    slew to the picosecond (OpenSTA lists the driver and every load), so a big
    group with one value is one driver with many loads: lower the fanout limit
    first (seen on the counter template: 11 violations at slew margin 20 and 30,
    none at fanout 6). Scattered single pins are weak drivers or long wires: a
    bigger slew margin first, no fanout change."""
    by = _slew_rows(run_dir)
    fan = _num(cfg.get("MAX_FANOUT_CONSTRAINT"))
    pct = _num(cfg.get("DESIGN_REPAIR_MAX_SLEW_PCT"))
    fanout_fix = (_setting("MAX_FANOUT_CONSTRAINT", fan, max(4, int(fan) - 4),
                           "Fewer inputs per driver: design repair splits bigger nets with buffers, so each edge is "
                           "faster, in the slow corners too.", "Fewer loads per driver", sure="likely")
                  if fan is not None and fan > 4 else None)
    pct_fix = (_setting("DESIGN_REPAIR_MAX_SLEW_PCT", pct, int(pct) + 10,
                        "Design repair aims this much under the slew limit (%), to leave room for the wires it "
                        "can't see yet.", "Repair with more margin", sure="try") if pct is not None else None)
    wl = _num(cfg.get("DESIGN_REPAIR_MAX_WIRE_LENGTH"))
    # 0 means design repair never buffers a wire for its length alone.
    wire_fix = (_setting("DESIGN_REPAIR_MAX_WIRE_LENGTH", wl, 400,
                         "Buffer any wire longer than this (µm): a slow edge at the end of a long wire is the wire's "
                         "fault, not the driver's. A starting point; shorter adds more buffers.",
                         "Buffer long wires", sure="try") if wl == 0 else None)
    # Slew and capacitance only ever warn in LibreLane 3.0.4 (their checkers
    # match no corner), so these are worth fixing but never stop a run.
    notes: List[str] = []
    tail = ("LibreLane reports slow edges as a warning and finishes the run; the timing numbers of the nets over the "
            "limit are extrapolated from the library's tables. Raising MAX_TRANSITION_CONSTRAINT above the PDK's value "
            "only hides them.")
    if not by:
        return [f for f in (pct_fix, fanout_fix, wire_fix) if f], [tail]
    corner, rows = max(by.items(), key=lambda kv: len(kv[1]))
    groups: List[List[tuple]] = []
    for r in sorted(rows, key=lambda r: -r[2]):
        if groups and abs(groups[-1][0][2] - r[2]) < 0.0005:
            groups[-1].append(r)
        else:
            groups.append([r])
    big = max(groups, key=len)
    if len(big) >= 4:
        drivers = [p for p, _l, _s in big if re.search(r"/(X|Y|Q|Z|ZN|O)$", p) and not re.match(r"_\d+_/", p)] or [big[0][0]]
        notes.append("In %s, %d of the %d pins over the limit share one slew (%.3f ns): one net, driven by %s, with "
                     "many loads." % (corner, len(big), len(rows), big[0][2], drivers[0]))
        return [f for f in (fanout_fix, pct_fix) if f], notes + [tail]
    notes.append("In %s the %d pins over the limit are on different nets, so it isn't one driver with too many loads: "
                 "the fanout limit is left alone." % (corner, len(rows)))
    return [f for f in (pct_fix, wire_fix) if f], notes + [tail]
_CHECK_OPEN = {
    "lint_errors": ("Open the RTL IDE", {"tab": "ide"}, "Its Problems list shows each lint error with file and line."),
    "lint_warnings": ("Open the RTL IDE", {"tab": "ide"}, "Its Problems list shows each warning with file and line."),
    "lint_timing_constructs": ("Open the RTL IDE", {"tab": "ide"}, "Remove the delays or wrap them in `ifdef SIMULATION."),
    "unmapped": ("Open the RTL IDE", {"tab": "ide"}, "Usually a module that is used but never defined."),
    "synth_check": ("Open the RTL IDE", {"tab": "ide"}, "Drive every wire; break loops with a register."),
    "latch": ("Open the RTL IDE", {"tab": "ide"}, "Give every signal a value on every path of the always block."),
    "assign": ("Open the RTL IDE", {"tab": "ide"}, "Usually a port wired straight to another port or a constant."),
    "eqy": ("Open Verify", {"tab": "verify", "section": "vf-checks"}, "The equivalence report names the outputs that differ."),
    "lvs": ("Open LVS in Verify", {"tab": "verify", "section": "vf-lvs"},
            "Netgen's report: which nets or devices don't match, with their names."),
    "overlap": ("Open DRC in Verify", {"tab": "verify", "section": "vf-drc"}, "Where the overlapping cells are."),
    "antenna": ("Open reliability in Verify", {"tab": "verify", "section": "vf-rel"},
                "Every violating net, with its pin and how far over the limit it is."),
    "pdn": ("Open reliability in Verify", {"tab": "verify", "section": "vf-rel"}, "Which cells or pins have no power."),
    "wire_length": ("Open reliability in Verify", {"tab": "verify", "section": "vf-rel"}, "The longest wires, by net."),
    "disc_pin": ("Show the layout", {"tab": "layout"}, "Look for pins with no wire near the place it reports."),
    "setup": ("Show the worst paths", {"tab": "analytics", "section": "an-timing"}, "Every failing path, worst first."),
    "hold": ("Show the worst paths", {"tab": "analytics", "section": "an-timing"}, "Every failing path, worst first."),
    "slew": ("Open timing in Verify", {"tab": "verify", "section": "vf-timing"}, "The nets over the limit, per corner."),
    "cap": ("Open timing in Verify", {"tab": "verify", "section": "vf-timing"}, "The nets over the limit, per corner."),
    "fanout": ("Open timing in Verify", {"tab": "verify", "section": "vf-timing"}, "The nets over the limit, per corner."),
}

# What each check means, in plain words, for the card's lede when the check's
# own detail is only a number. Checked against LibreLane 3.0.4 (steps/checker.py,
# steps/odb.py, steps/openroad.py) and the tools' reports.
_CHECK_PLAIN = {
    "antenna": "During manufacturing, a long wire collects charge before the transistor gate it ends on is connected "
               "to anything that can drain it, and the charge can damage that gate. The antenna check counts nets whose "
               "wire area to gate area ratio is over the PDK's limit. The usual repair is a protection diode on the net, "
               "or a jump to a higher metal layer near the gate; LibreLane's global router does both when repair is on.",
    "lvs": "LVS (layout versus schematic) rebuilds the circuit from the drawn shapes and compares it with the netlist "
           "from synthesis. A mismatch means the chip that would be made is not the circuit you designed: a missing or "
           "extra connection, a short between two nets, or a cell that isn't connected the way the netlist says. "
           "Netgen's report names the nets and devices that differ. A short between power and ground, or an unconnected "
           "power pin, is the most common cause.",
    "overlap": "Two cells placed on top of each other where that's not allowed. Usually a macro or a hand-placed cell "
               "on top of standard cells.",
    "pdn": "Some cells or pins aren't connected to the power grid, so they would have no power.",
    "wire_length": "A wire longer than LibreLane's warning threshold. Long wires are slow and pick up noise; usually "
                   "the two ends sit far apart, often a pin placed far from the logic it drives.",
    "disc_pin": "Pins of cells that no wire reaches. Usually the router ran out of room; the router's log and the "
                "layout show where.",
    "slew": "Signals that change too slowly at some nets: a weak driver or too much load. Design repair resizes "
            "or buffers them.",
    "cap": "Nets driving more capacitance than their driver is rated for. Design repair splits or buffers them.",
    "fanout": "Nets driving more inputs than the limit. Design repair buffers them.",
    "setup": "Some paths take longer than the clock period: data arrives after the clock edge.",
    "hold": "Some paths are too fast: new data reaches a register too soon after the clock edge.",
    "eqy": "The formal check says the netlist doesn't do the same thing as the RTL for some outputs.",
    "assign": "The netlist still has assign statements, which some downstream tools can't read.",
    "lint_errors": "Verilator found errors in the RTL: code that won't build into hardware as written.",
    "lint_warnings": "Verilator found code that builds but often isn't what was meant, such as widths that don't match.",
    "lint_timing_constructs": "The RTL has delays (#10) or other simulation-only timing, which synthesis can't build.",
    "unmapped": "Synthesis left cells it couldn't map to the standard-cell library.",
    "synth_check": "Synthesis found undriven wires, combinational loops or other structural problems.",
    "latch": "Synthesis inferred latches: a signal keeps its old value on some path of an always block. Usually "
             "unintended, and hard to time.",
}

# Layers known to be a drawn outline, not a mask, from each PDK's KLayout layer
# properties (sky130A.lyp: "prBoundary.boundary - 235/4").
_BOUNDARY_LAYERS = {"sky130": {"235/4": "prBoundary (the cell outline)"}}
# The outline layer's name in each PDK's .lyp: sky130 prBoundary 235/4,
# gf180mcu PR_bndry 0/0, IHP sg13g2 prBoundary 189/0 and 189/4.
_OUTLINE_NAME = re.compile(r"^(prBoundary|PR_bndry)\b", re.I)


def boundary_layers(pdk: str) -> Dict[str, str]:
    """``{"layer/datatype": words}`` for the cell-outline layers of *pdk*, read
    from its KLayout layer properties; the sky130 entry when that file can't be
    read; nothing for a PDK LanEx can't tell (then no difference is called an
    outline)."""
    try:
        from . import celllib
        rows = celllib._lyp(pdk) if pdk else []
    except Exception:
        rows = []
    # IHP's outline has a label datatype too (prBoundary.label 189/1): still
    # not a mask, so it is named for what it is.
    out = {"%d/%d" % (r["layer"], r["dt"]): "%s (the cell outline%s)" % (
               r["name"].split(".")[0], "'s label" if ".label" in str(r["name"]).lower() else "")
           for r in rows if _OUTLINE_NAME.match(str(r.get("name") or ""))}
    if out:
        return out
    fam = next((f for f in _BOUNDARY_LAYERS if pdk.startswith(f)), None)
    return dict(_BOUNDARY_LAYERS.get(fam or "", {}))


def _xor_items(run_dir: Path) -> List[Dict[str, Any]]:
    """The XOR markers: ``[{layer, bbox}]`` from KLayout.XOR's xor.xml."""
    xml = next(iter(sorted(Path(run_dir).glob("*-klayout-xor/xor.xml"))), None)
    if xml is None:
        return []
    try:
        text = xml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        cat = re.search(r"<category>'?([^<']+)'?</category>", item)
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", " ".join(re.findall(r"<value>(.*?)</value>", item, re.S)))]
        xs, ys = nums[0::2], nums[1::2]
        out.append({"layer": cat.group(1) if cat else "?",
                    "bbox": [min(xs), min(ys), max(xs), max(ys)] if xs and ys else None})
    return out


def xor_problem(run_dir: Path, rep: Dict[str, Any], check: Dict[str, Any]) -> Dict[str, Any]:
    """The XOR check (LibreLane 3.0.4 KLayout.XOR in steps/klayout.py, gated by
    Checker.XOR on ``design__xor_difference__count`` in steps/checker.py): the
    GDS Magic wrote against the one KLayout wrote, shape by shape."""
    cfg = _cfg(run_dir)
    items = _xor_items(run_dir)
    n = _num(check.get("value"))
    layers = sorted({i["layer"] for i in items})
    pdk = str(cfg.get("PDK") or rep.get("pdk") or "")
    bounds = boundary_layers(pdk)
    plain = ("LibreLane writes the finished layout twice, once with Magic and once with KLayout, and XORs the two "
             "files: any shape that is in one and not the other is a difference. %s found. A difference means one of "
             "the two stream-outs got something wrong, so it isn't certain which file is right. The GDS LibreLane hands "
             "you (final/gds) is Magic's." % ("%s difference%s" % (fmt(n), "" if n == 1 else "s") if n is not None
                                                else "Differences were"))
    fixes: List[Dict[str, Any]] = []
    notes: List[str] = []
    evidence = ["On layer %s: %d marker%s." % (lay, sum(1 for i in items if i["layer"] == lay),
                                               "" if sum(1 for i in items if i["layer"] == lay) == 1 else "s")
                for lay in layers]
    title = "Mask XOR: Magic's and KLayout's GDS differ"
    die = _die(run_dir)
    area = cfg.get("DIE_AREA")
    area_v = [_num(x) for x in (area if isinstance(area, (list, tuple)) else str(area or "").split())]
    stale = (str(cfg.get("FP_SIZING") or "relative") == "relative" and len(area_v) == 4 and None not in area_v
             and die is not None and any(abs(a - b) > 0.01 for a, b in zip(area_v, die)))
    if layers and all(lay in bounds for lay in layers):
        title = "Mask XOR: only the cell outline differs"
        plain += (" Here every difference is on %s, a drawn outline, not a mask layer, so the shapes that would be "
                  "made match." % bounds[layers[0]])
        if stale:
            # Magic.StreamOut exports DIE_AREA from the config into mag_gds.tcl,
            # which draws the outline (property FIXED_BBOX) from it; KLayout's
            # stream-out takes the outline from the DEF's real die.
            plain += (" The cause: FP_SIZING is relative, so LibreLane sized the die itself (%s µm), but config.json "
                      "still sets DIE_AREA (%s µm). Magic drew the outline from DIE_AREA, KLayout from the real die."
                      % (" ".join(fmt(x) for x in die), fmt(area)))
            fixes.append(_setting("DIE_AREA", area, " ".join(fmt(x) for x in die),
                                  "The die this run really has, so both outlines agree. Removing DIE_AREA from config.json "
                                  "works too, as long as FP_SIZING stays relative.", "Match DIE_AREA to the die"))
        notes.append("Still fix it rather than turning the check off: the outline tells whatever reads the GDS next "
                     "(a parent design, a shuttle's checks) where the block ends.")
    else:
        plain += (" Open the markers to see where and on which layer. A difference on a metal or via layer is a real "
                  "ambiguity in what would be made; report it with both GDS files if your design doesn't explain it.")
    box = next((i["bbox"] for i in items if i["bbox"]), None)
    if box:
        fixes.append(_open("Show the difference in Layout", {"tab": "layout", "box": ",".join("%.2f" % v for v in box)},
                           "The final layout with the XOR markers on, zoomed to the first one."))
    fixes.append(_open("Open LVS & XOR in Verify", {"tab": "verify", "section": "vf-lvs"},
                       "The XOR report (xor.xml) opens in KLayout's marker browser from there."))
    notes.append("ERROR_ON_XOR_ERROR fails the run on any difference (LibreLane's Checker.XOR).")
    return {"id": "check-xor", "severity": check["status"], "kind": "check", "title": title, "plain": plain,
            "where": {"check": "xor", "stage": "signoff"}, "evidence": evidence,
            "diag": {"kind": "xor", "value": check.get("value"), "limit": check.get("limit"), "layers": layers,
                     "stale_die_area": stale},
            "fixes": fixes, "notes": notes, "learn": ["xor"]}


# The glossary term a check's card names (static/glossary.json).
_CHECK_TERM = {"antenna": "antenna", "lvs": "lvs", "slew": "slew", "cap": "max-cap", "fanout": "fanout",
               "latch": "latch", "lint_errors": "lint", "lint_warnings": "lint", "pdn": "pdn", "eqy": "eqy",
               "disc_pin": "disconnected-pin", "overlap": "illegal-overlap", "wire_length": "wirelength",
               "unmapped": "standard-cell", "assign": "netlist"}


def _lvs_breakdown(run_dir: Path) -> List[str]:
    """What LVS found, by kind, from the counts LibreLane computes from
    Netgen's report (steps/netgen.py): "3 pins don't match" says more than
    "48 LVS errors"."""
    from . import history
    m = history._load_metrics(run_dir)
    parts = []
    for key, one, many in (("design__lvs_unmatched_pin__count", "pin doesn't match", "pins don't match"),
                           ("design__lvs_unmatched_net__count", "net is only on one side", "nets are only on one side"),
                           ("design__lvs_unmatched_device__count", "device is only on one side", "devices are only on one side"),
                           ("design__lvs_net_difference__count", "net differs", "nets differ"),
                           ("design__lvs_device_difference__count", "device count differs", "device counts differ"),
                           ("design__lvs_property_fail__count", "property differs", "properties differ")):
        n = _num(m.get(key))
        if n:
            parts.append("%d %s" % (n, one if n == 1 else many))
    return ["Netgen: " + ", ".join(parts) + "."] if parts else []


def check_problems(run_dir: Path, rep: Dict[str, Any], skip: set) -> List[Dict[str, Any]]:
    cfg = _cfg(run_dir)
    out = []
    for c in rep.get("checks") or []:
        if c["status"] not in ("fail", "warn") or c["id"] in skip:
            continue
        if c["id"] == "xor":
            out.append(xor_problem(run_dir, rep, c))
            continue
        fixes: List[Dict[str, Any]] = []
        notes: List[str] = []
        if c["id"] == "antenna":
            fixes, notes = _antenna_fixes(cfg, rep)
        elif c["id"] == "slew":
            fixes, notes = _slew_fixes(cfg, run_dir)
        for var, prop, why in _CHECK_FIXES.get(c["id"], []):
            cur = cfg.get(var)
            num = _num(cur)
            to = prop(num if num is not None else cur)
            if to is not None and fmt(to) != fmt(cur):
                fixes.append(_setting(var, cur, to, why))
        if c["id"] in _CHECK_OPEN:
            label, go, why = _CHECK_OPEN[c["id"]]
            fixes.append(_open(label, go, why))
        detail = c.get("detail") or c.get("note") or ""
        meaning = _CHECK_PLAIN.get(c["id"], "")
        evidence: List[str] = []
        if c["id"] == "lvs":
            evidence = _lvs_breakdown(run_dir)
        out.append({"id": "check-" + c["id"], "severity": c["status"], "kind": "check", "title": c["name"],
                    "plain": (detail + " " + meaning).strip() if meaning and meaning not in detail else detail,
                    "where": {"check": c["id"]},
                    "evidence": evidence, "diag": {"kind": "check", "value": c.get("value"), "limit": c.get("limit")},
                    "fixes": fixes, "notes": notes, "learn": [_CHECK_TERM[c["id"]]] if c["id"] in _CHECK_TERM else []})
    return out


# ---------------------------------------------------------------- the run
def _cfg(run_dir: Path) -> Dict[str, Any]:
    from . import history
    return history._read_config(Path(run_dir)) or {}


def resume_for(var: str, flow: str = "Classic") -> Optional[Dict[str, Any]]:
    from . import varstage
    try:
        return varstage.resume_point([var], flow)
    except Exception:
        return None


def finish(fixes: List[Dict[str, Any]], flow: str = "Classic") -> List[Dict[str, Any]]:
    """Keep only settings this LibreLane knows (applying an unknown one would
    fail the run) and say where each one resumes."""
    from . import introspect
    names = introspect.known_var_names()
    out = []
    for f in fixes:
        if f["kind"] == "setting":
            if names and f["var"] not in names:
                continue
            f["resume"] = resume_for(f["var"], flow)
        out.append(f)
    return out


_CACHE: Dict[str, Any] = {}


def _signature(run_dir: Path) -> tuple:
    sig = []
    for p in (run_dir, run_dir / "final" / "metrics.json", run_dir / "gui-run.json", run_dir / "error.log"):
        try:
            sig.append(p.stat().st_mtime_ns)
        except OSError:
            sig.append(0)
    return tuple(sig)


_KB_SEEN: Dict[str, Any] = {}


def kb_index(design_dir: Optional[str | Path] = None, max_runs: int = 60) -> Dict[str, Any]:
    """The knowledge base as an index, LanEx's ``rustc --explain``: every
    entry's codes, stage, one line and whether its words were checked on a
    real forced failure; and, for the open project, which of its runs (the
    newest *max_runs*) stopped on each one, read from each run's error.log
    alone (a few KB each, so the request stays fast)."""
    entries = [{"id": r["id"], "code": r.get("code"), "also": list(r.get("also") or []), "stage": r.get("stage"),
                "about": r.get("about"), "forced": str(r.get("source") or "").startswith("forced"),
                "source": r.get("source")} for r in KB]
    seen: Dict[str, List[str]] = {}
    runs = Path(design_dir) / "runs" if design_dir else None
    if runs is not None and runs.is_dir():
        try:
            sig = (str(runs), runs.stat().st_mtime_ns)
        except OSError:
            sig = None
        hit = _KB_SEEN.get("v")
        if sig and hit and hit[0] == sig:
            seen = hit[1]
        else:
            dirs = sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: -p.stat().st_mtime)[:max_runs]
            for rd in dirs:
                el = rd / "error.log"
                try:
                    with el.open("r", encoding="utf-8", errors="replace") as f:
                        text = f.read(20000)
                except OSError:
                    continue
                if not text.strip():
                    continue
                m = _CODE_RX.search(text)
                code = m.group(1) if m else None
                for rule in KB:
                    if (code and code in [rule.get("code")] + list(rule.get("also") or [])) or \
                            (rule.get("rx") is not None and rule["rx"].search(text)):
                        seen.setdefault(rule["id"], []).append(rd.name)
                        break
            if sig:
                _KB_SEEN["v"] = (sig, seen)
    return {"entries": entries, "seen": seen}


def run_problems(run_dir: str | Path) -> Dict[str, Any]:
    """Every problem of one run, worst first: the step that stopped it (if
    any), then each failing or warning check, timing and DRC diagnosed.
    Remembered until the run's folder changes."""
    run_dir = Path(run_dir).resolve()
    key, sig = str(run_dir), _signature(run_dir)
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    out = _run_problems(run_dir)
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = (sig, out)
    return out


def clock_problems(run_dir: Path, rep: Dict[str, Any], rx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A clock that was never created. LibreLane 3.0.4 doesn't stop for it:
    with CLOCK_PORT naming no port (forced: CLOCK_PORT clock on spm) every STA
    warns "[STA-0366] port 'clock' not found." and CTS "[CTS-0083] No clock nets
    have been found.", and the run ends clean with nothing timed. CLOCK_PORT
    empty gives port '__VIRTUAL_CLK__' instead."""
    cfg = _cfg(run_dir)
    groups = {g["code"]: g for g in ((rx.get("warnings") or {}).get("by_code") or [])}
    sta = groups.get("STA-0366")
    if not sta:
        return []
    m = re.search(r"port '([^']+)' not found", sta.get("sample") or "")
    port = m.group(1) if m else ""
    clk = cfg.get("CLOCK_PORT")
    clks = clk if isinstance(clk, list) else [clk] if clk else []
    virtual = port == "__VIRTUAL_CLK__"
    if not virtual and port not in clks:
        return []
    if virtual and clocked_logic(cfg) is False:
        return []          # pure logic: timed against the virtual clock, said as a note
    ports = _top_ports(cfg)
    cand = [p for p in ports["input"] if re.search(r"clk|clock", p, re.I)]
    fixes: List[Dict[str, Any]] = []
    if len(cand) == 1 and cand[0] not in clks:
        fixes.append(_setting("CLOCK_PORT", clk, cand[0], "%s is the design's one input named like a clock." % cand[0],
                              "Clock the design by %s" % cand[0]))
    fixes.append(_open("Open the clock setting", {"tab": "setup", "setting": "CLOCK_PORT"}, "Where the clock port is set."))
    if virtual:
        title = "No clock is set, so nothing was timed against one"
        plain = ("CLOCK_PORT is empty, so LibreLane timed the design against a made-up clock. Registers clocked by a "
                 "real input were never checked, and the timing results don't mean the chip meets any clock.")
    else:
        title = "The clock port '%s' isn't in the design" % port
        plain = ("CLOCK_PORT is '%s', and %s has no port by that name, so the clock was never created: no "
                 "register-to-register path was timed and no clock tree was built. The run still finished, and its "
                 "timing looks clean only because nothing was checked." % (port, cfg.get("DESIGN_NAME") or "the design"))
    if cand and not virtual:
        plain += " Inputs named like a clock: %s." % ", ".join(cand)
    ev = [sta.get("sample") or ""]
    if groups.get("CTS-0083"):
        ev.append(groups["CTS-0083"].get("sample") or "")
    return [{"id": "clock-missing", "severity": "warn", "kind": "check", "title": title, "plain": plain,
             "where": {"check": "setup", "stage": "signoff"}, "evidence": [e for e in ev if e],
             "diag": {"kind": "clock", "port": port, "clock_port": clk, "candidates": cand},
             "fixes": fixes, "notes": [], "learn": ["slack"]}]


def _run_problems(run_dir: Path) -> Dict[str, Any]:
    from . import runexplain, verify
    rep = verify.verify_report(run_dir)
    flow = rep.get("flow") or "Classic"
    problems: List[Dict[str, Any]] = []
    rx = runexplain.explain_run(run_dir)
    if rx.get("kind") == "halted" and rx.get("card"):
        c = rx["card"]
        step = rx.get("step") or {}
        problems.append({"id": "halted", "severity": "fail", "kind": "halted",
                         "title": c.get("title") or ("Stopped at " + (step.get("id") or step.get("dir") or "a step")),
                         "plain": c.get("plain") or "", "where": {"stage": rx.get("stage"), "step": step, "at": rx.get("at")},
                         "evidence": (c.get("evidence") or rx.get("errors") or [])[:3],
                         "diag": {**(c.get("diag") or {}), "kind": "halted", "code": c.get("code"), "kb": c.get("kb"),
                                  "generic": bool(c.get("generic"))},
                         "help": c.get("help"),
                         "fixes": c.get("fixes") or [], "notes": [], "learn": c.get("learn") or []})
    problems += clock_problems(run_dir, rep, rx)
    skip = set()
    for p in timing_problems(run_dir, rep):
        problems.append(p)
        skip.add(p["where"]["check"])
    for p in drc_problems(run_dir, rep):
        problems.append(p)
        skip.update({"magic_drc", "klayout_drc", "route_drc"})
    problems += check_problems(run_dir, rep, skip)
    for p in problems:
        p["fixes"] = finish(p["fixes"], flow) if p["kind"] != "halted" else p["fixes"]
    problems.sort(key=lambda p: (p["severity"] != "fail", p["kind"] != "halted"))
    return {"tag": run_dir.name, "flow": flow, "verdict": (rep.get("verdict") or {}).get("state"),
            "counts": {"fail": sum(1 for p in problems if p["severity"] == "fail"),
                       "warn": sum(1 for p in problems if p["severity"] == "warn")},
            "problems": problems}
