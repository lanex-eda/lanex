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
"""Live layout viewing for the Layout tab, drawn by KLayout itself.

LibreLane already depends on the ``klayout`` Python package, and from 0.28 on
that package carries KLayout's own renderer (``klayout.lay.LayoutView``)
without Qt. So the app can show a run's layout exactly as KLayout draws it,
with the PDK's layer properties (.lyp), at any zoom, without opening a window.
This is the approach of gdsfactory's kweb and OpenROAD's web viewer: the
server draws the requested viewport, the browser shows the picture and does
the interaction.

What a run offers (:func:`sources`):

* ``final``: the signoff GDS (``final/gds``), full transistor-level detail;
* one snapshot per flow stage (floorplan, power grid, placement, clock tree,
  routing), read from the DEF that stage's step wrote, with the standard cells
  as their LEF abstracts (outline + pins), the way LibreLane's own
  ``OpenInKLayout`` flow opens a DEF;
* every other step that wrote a DEF, for anyone who wants a particular step.

Nothing here computes silicon numbers: geometry, layer names and colours come
straight from KLayout and the PDK files the run itself used (``resolved.json``).

Where the work runs. KLayout keeps Python's interpreter lock for the whole of
a render, so in the server (``enable_workers``) every KLayout call goes to a
worker process (``layoutworker``): *view* for what the user waits on (open,
render, probe, nets) and *bg* for the slow rest (thumbnails, the 3D mesh, 4K
and 8K saves). The server keeps each session's spec, so a worker that was
restarted reopens a session by itself. Without workers (library use, tests)
the same code runs in this process. KLayout objects are not thread-safe, so
in either place every call holds one lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import flowstages

_log = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SESSIONS: "OrderedDict[str, _Session]" = OrderedDict()
# The background worker keeps fewer sessions: it holds copies of layouts the
# view worker already has, only for thumbnails and 3D.
_MAX_SESSIONS = 2 if os.environ.get("LANEX_LAYOUT_ROLE") == "bg" else 4
# Sessions on one standard cell of a PDK library (the Cells tab) have their own,
# smaller cap so browsing cells never pushes a run's layout out.
_CELL_PREFIX = "cell::"
_MAX_CELL_SESSIONS = 2
_THUMBS: "OrderedDict[Tuple, bytes]" = OrderedDict()
_MAX_THUMBS = 48

# Render limits: a 4K export with 2x oversampling is the biggest legitimate ask.
_MAX_PX = 8192
_MAX_MARKERS = 4000
_MAX_INSTANCES = 30000
_MAX_LABELS = 6000
# One net's shapes sent to the page for a highlight (a power net on a big chip
# has more; the reply says how many were left out).
_MAX_NET_SHAPES = 20000
_MAX_NET_HITS = 200

# Worker processes (enable_workers); None = everything in this process.
_WORKERS: Optional[Dict[str, Any]] = None
# Session specs the server has handed out: enough to reopen one anywhere.
_SPECS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_MAX_SPECS = 64

# Flow stages that leave a picture, in flow order, named as on every other page
# (flowstages.py). Each is shown from the LAST step (by ordinal) whose name
# matches one of its patterns and that wrote a DEF. Patterns match the step dir
# name after "NN-".
_SNAPSHOT_STEPS = {
    "floorplan": ["openroad-floorplan", "openroad-cutrows", "openroad-tapendcapinsertion"],
    "pdn": ["openroad-generatepdn"],
    "placement": ["openroad-globalplacement", "openroad-repairdesignpostgpl", "openroad-detailedplacement"],
    "cts": ["openroad-cts", "openroad-resizertimingpostcts"],
    "routing": ["openroad-globalrouting", "openroad-detailedrouting"],
}
STAGES: List[Dict[str, Any]] = [
    {"id": sid, "label": flowstages.stage(sid)["label"], "blurb": flowstages.stage(sid)["blurb"], "match": m}
    for sid, m in _SNAPSHOT_STEPS.items()
]
_FINAL_BLURB = ("The signoff GDS: every cell with its real transistor-level shapes, plus fill. "
                "This is the file that goes to the foundry.")

# Report databases that carry marker geometry, newest step of each wins.
_MARKER_SOURCES = [
    {"id": "klayout-drc", "label": "KLayout DRC", "step": "klayout-drc", "glob": "reports/*.lyrdb"},
    {"id": "magic-drc", "label": "Magic DRC", "step": "magic-drc", "glob": "reports/*.lyrdb"},
    {"id": "route-drc", "label": "Routing DRC", "step": "openroad-detailedrouting", "glob": "*.drc.xml"},
    {"id": "xor", "label": "Magic vs KLayout XOR", "step": "klayout-xor", "glob": "*.xml"},
]


# --------------------------------------------------------------------------- availability
def available() -> Dict[str, Any]:
    """Is KLayout's renderer importable here? (It ships with LibreLane.)"""
    try:
        import klayout.db as kdb  # noqa: F401
        import klayout.lay as klay
        ok = hasattr(klay, "LayoutView")
        ver = ""
        try:
            import klayout
            ver = getattr(klayout, "__version__", "") or ""
        except Exception:
            pass
        if not ver:
            try:
                from importlib.metadata import version
                ver = version("klayout")
            except Exception:
                ver = ""
        return {"ok": ok, "version": ver,
                "reason": "" if ok else "this klayout package has no LayoutView (needs 0.28 or newer)"}
    except Exception as ex:
        return {"ok": False, "version": "", "reason": "the klayout Python package is not installed (" + str(ex) + ")"}


# --------------------------------------------------------------------------- run helpers
def _ordinal(name: str) -> int:
    head = name.split("-", 1)[0]
    return int(head) if head.isdigit() else -1


def _step_dirs(run_dir: Path) -> List[Path]:
    try:
        dirs = [d for d in run_dir.iterdir() if d.is_dir() and _ordinal(d.name) >= 0 and "-" in d.name]
    except Exception:
        return []
    return sorted(dirs, key=lambda d: _ordinal(d.name))


def _step_key(name: str) -> str:
    """``35-openroad-cts`` / ``36-openroad-stamidpnr-1`` -> ``openroad-cts`` / ``openroad-stamidpnr``."""
    rest = name.split("-", 1)[1] if "-" in name else name
    return re.sub(r"-\d+$", "", rest)


def _def_in(step_dir: Path) -> Optional[Path]:
    try:
        hits = sorted(step_dir.glob("*.def"))
    except Exception:
        return None
    return hits[0] if hits else None


def _final_gds(run_dir: Path) -> Optional[Path]:
    for sub in ("gds", "klayout_gds", "mag_gds"):
        d = run_dir / "final" / sub
        if d.is_dir():
            hits = sorted(d.glob("*.gds")) + sorted(d.glob("*.gds.gz"))
            if hits:
                return hits[0]
    return None


def _final_def(run_dir: Path) -> Optional[Path]:
    """``final/def/<design>.def``: the same chip as the final GDS, with names."""
    d = run_dir / "final" / "def"
    try:
        hits = sorted(d.glob("*.def")) if d.is_dir() else []
    except Exception:
        return None
    return hits[0] if hits else None


def _first_name(v: Any) -> Optional[str]:
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if isinstance(v, str):
        v = v.split()[0] if v.split() else None
    return str(v) if v else None


def pdn_info(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The run's power grid as LibreLane built it: the layers of the rails and
    straps, and the supply and ground net names (for the Power preset)."""
    return {
        "rail": cfg.get("PDN_RAIL_LAYER") or None,
        "vertical": cfg.get("PDN_VERTICAL_LAYER") or None,
        "horizontal": cfg.get("PDN_HORIZONTAL_LAYER") or None,
        "vdd": _first_name(cfg.get("VDD_NETS")) or _first_name(cfg.get("VDD_PIN")) or None,
        "gnd": _first_name(cfg.get("GND_NETS")) or _first_name(cfg.get("GND_PIN")) or None,
    }


def _resolved(run_dir: Path) -> Dict[str, Any]:
    for name in ("resolved.json", "config.json"):
        f = run_dir / name
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8", errors="replace")) or {}
            except Exception:
                continue
    return {}


def _pdk_tech_dir(cfg: Dict[str, Any]) -> Optional[Path]:
    """``<PDK_ROOT>/<PDK>/libs.tech/klayout/tech`` when the run's own paths moved."""
    pdk = cfg.get("PDK")
    if not pdk:
        return None
    roots: List[Path] = []
    if cfg.get("PDK_ROOT"):
        roots.append(Path(str(cfg["PDK_ROOT"])))
    try:
        from . import pdk as pdk_mod
        roots.extend(pdk_mod._candidate_pdk_roots())
    except Exception:
        pass
    for r in roots:
        d = r / str(pdk) / "libs.tech" / "klayout" / "tech"
        if d.is_dir():
            return d
    return None


def _existing(p: Any) -> Optional[str]:
    if not p:
        return None
    try:
        return str(p) if Path(str(p)).is_file() else None
    except Exception:
        return None


def _tech_files(cfg: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """The run's KLayout .lyp / .map (falling back to the PDK's klayout/tech dir)."""
    lyp = _existing(cfg.get("KLAYOUT_PROPERTIES"))
    lym = _existing(cfg.get("KLAYOUT_DEF_LAYER_MAP"))
    if not (lyp and lym):
        d = _pdk_tech_dir(cfg)
        if d is not None:
            lyp = lyp or next((str(p) for p in sorted(d.glob("*.lyp"))), None)
            lym = lym or next((str(p) for p in sorted(d.glob("*.map"))), None)
    return {"lyp": lyp, "lym": lym}


def _lef_list(cfg: Dict[str, Any]) -> List[str]:
    """Tech LEF first, then cell LEFs and any macro LEFs (as LibreLane passes them)."""
    out: List[str] = []
    tl = cfg.get("TECH_LEFS")
    if isinstance(tl, dict):
        pick = tl.get("nom_*") or next(iter(tl.values()), None)
        if pick:
            out.append(str(pick))
    elif isinstance(tl, (list, tuple)) and tl:
        out.append(str(tl[0]))
    elif isinstance(tl, str):
        out.append(tl)
    for key in ("CELL_LEFS", "EXTRA_LEFS"):
        v = cfg.get(key)
        if isinstance(v, str):
            out.extend(v.split())
        elif isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v)
    macros = cfg.get("MACROS")
    if isinstance(macros, dict):
        for m in macros.values():
            if isinstance(m, dict):
                for lef in m.get("lef") or []:
                    out.append(str(lef))
    return [p for p in out if Path(p).is_file()]


def _odb_in(step_dir: Path, run_dir: Path) -> Optional[str]:
    """The step's own OpenROAD database, when the run kept it on disk."""
    try:
        hits = sorted(step_dir.glob("*.odb"))
    except Exception:
        return None
    return hits[0].relative_to(run_dir).as_posix() if hits else None


def sources(run_dir: Path) -> Dict[str, Any]:
    """What can be viewed for a run: the final GDS and one snapshot per stage."""
    run_dir = Path(run_dir)
    steps = _step_dirs(run_dir)
    with_def = [(d, _def_in(d)) for d in steps]
    with_def = [(d, p) for d, p in with_def if p is not None]
    stages: List[Dict[str, Any]] = []
    for st in STAGES:
        pick = None
        for d, p in with_def:
            if _step_key(d.name) in st["match"]:
                pick = (d, p)
        if pick is not None:
            d, p = pick
            stages.append({"id": st["id"], "label": st["label"], "blurb": st["blurb"], "kind": "def",
                           "source": d.name, "step": d.name, "path": p.relative_to(run_dir).as_posix(),
                           "odb": _odb_in(d, run_dir)})
    gds = _final_gds(run_dir)
    if gds is not None:
        stages.append({"id": "final", "label": "Final GDS", "blurb": _FINAL_BLURB, "kind": "gds",
                       "source": "final", "step": "final", "path": gds.relative_to(run_dir).as_posix()})
    all_steps = [{"source": d.name, "step": d.name, "path": p.relative_to(run_dir).as_posix(), "kind": "def",
                  "odb": _odb_in(d, run_dir)} for d, p in with_def]
    variants = []
    for sub, label in (("gds", "Signoff GDS"), ("klayout_gds", "KLayout stream-out"), ("mag_gds", "Magic stream-out")):
        dd = run_dir / "final" / sub
        if dd.is_dir():
            for f in sorted(dd.glob("*.gds")):
                variants.append({"source": "final:" + sub, "label": label, "path": f.relative_to(run_dir).as_posix(), "kind": "gds"})
    render = next((p.relative_to(run_dir).as_posix() for p in sorted((run_dir / "final" / "render").glob("*.png"))), None) \
        if (run_dir / "final" / "render").is_dir() else None
    cfg = _resolved(run_dir)
    return {"stages": stages, "steps": all_steps, "variants": variants, "render": render,
            "pdk": cfg.get("PDK"), "scl": cfg.get("STD_CELL_LIBRARY"), "design": cfg.get("DESIGN_NAME"),
            "pdn": pdn_info(cfg)}


def _source_path(run_dir: Path, source: str) -> Tuple[Optional[Path], str]:
    """(file, kind) for a source id from :func:`sources`."""
    if source in ("", "final"):
        return _final_gds(run_dir), "gds"
    if source.startswith("final:"):
        sub = source.split(":", 1)[1]
        if sub not in ("gds", "klayout_gds", "mag_gds"):
            return None, ""
        d = run_dir / "final" / sub
        hits = sorted(d.glob("*.gds")) if d.is_dir() else []
        return (hits[0] if hits else None), "gds"
    # A step dir: must be a direct child of the run dir.
    d = (run_dir / source).resolve()
    if d.parent != run_dir.resolve() or not d.is_dir():
        return None, ""
    return _def_in(d), "def"


# --------------------------------------------------------------------------- layer naming
_GROUPS = [
    ("metal", "Metal and vias"),
    # gf180 (Contact) and IHP (Cont) have contacts here but no local interconnect.
    ("local", "Contacts and local interconnect"),
    ("device", "Transistor layers"),
    ("well", "Wells and implants"),
    ("pin", "Pins and labels"),
    ("misc", "Boundaries and markers"),
]

# Layer base names across the PDKs LanEx supports: sky130 (met1, via, li1,
# licon1, mcon, diff, poly), gf180mcu (Metal1..Metal5, MetalTop, Via1..Via5,
# Contact, Poly2, COMP, Nplus/Pplus) and ihp-sg13g2 (Metal1..Metal5,
# TopMetal1/2, TopVia1/2, Cont, GatPoly, Activ, nSD/pSD).
_RX_METAL = re.compile(r"^(met\d|metal\d|m\d|topmetal\d?|metaltop|tm\d|rdl)$", re.I)
_RX_VIA = re.compile(r"^(via\d*|v\d|topvia\d?|tv\d|pad)$", re.I)
_RX_LOCAL = re.compile(r"^(li1?|licon\d?|mcon|cont|contact|co)$", re.I)
_RX_DEVICE = re.compile(r"^(diff|tap|poly|poly2|gatpoly|activ|comp|active|od|npc|hvi|hvntm)$", re.I)
_RX_WELL = re.compile(r"(well|implant|^nsdm$|^psdm$|^nsd$|^psd$|^nplus$|^pplus$|^hvtp$|^hvtr$|^lvtn$|^nimp$|^pimp$|^dnwell$|^nwell$|^pwell$|^dualgate$|^thkox$|^sab$)", re.I)
# Purposes that mark a region rather than draw silicon (gf180's metal slots and
# routing blockages), listed with the boundaries and markers.
_MARKER_PURPOSES = ("slot", "blk")

# One plain-English line per common layer, shown in the layer list's tooltip.
_LAYER_WORDS = {
    "met": "metal wiring layer {n}; higher numbers sit higher in the stack and carry longer wires",
    "metal": "metal wiring layer {n}",
    "via": "via: a vertical plug that joins the metal layer below to the one above",
    "mcon": "contact from local interconnect (li1) up to metal 1",
    "licon": "contact from diffusion or poly up to local interconnect (li1)",
    "li": "local interconnect: a thin wiring layer right above the transistors",
    "poly": "polysilicon: where poly crosses diffusion, a transistor gate is formed",
    "diff": "diffusion: the active silicon where transistors are made",
    "tap": "well tap: ties the wells to the power rails",
    "nwell": "n-well: the region where PMOS transistors are built",
    "pwell": "p-well: the region where NMOS transistors are built",
    "dnwell": "deep n-well: isolates a region from the substrate",
    "nsdm": "n+ implant: marks n-type source/drain regions",
    "psdm": "p+ implant: marks p-type source/drain regions",
    "npc": "nitride poly cut: opening for poly contacts",
    "hvtp": "high-Vt PMOS implant",
    "areaid": "area ID: a marker layer that tells rule decks what a region is",
    "prboundary": "placement boundary: the outline each cell or block reserves",
    "boundary": "design boundary",
    "contact": "contact: a plug from diffusion or poly up to metal 1",
    "topmetal": "thick top metal: carries power and long wires",
    "nplus": "n+ implant: marks n-type source/drain regions",
    "pplus": "p+ implant: marks p-type source/drain regions",
}
# Other PDKs' names for the same layers.
_WORD_ALIAS = {"poly2": "poly", "gatpoly": "poly", "comp": "diff", "activ": "diff", "active": "diff",
               "cont": "contact", "metaltop": "topmetal", "lvpwell": "pwell",
               "nsd": "nplus", "psd": "pplus"}

_RX_LD = re.compile(r"(\d+)/(\d+)")
# gf180mcu marks the purpose with a suffix ("Metal1_Label") where sky130 uses
# ".label"; a .lyp that names a layer only in its source carries it there.
_RX_SUFFIX_PURPOSE = re.compile(r"^(.+?)_(label|dummy|slot|blk|res|text|pin)$", re.I)


def _split_name(name: str) -> Tuple[str, str, str]:
    """``met1.drawing - 68/20`` -> (``met1``, ``drawing``, ``68/20``)."""
    src = ""
    m = re.search(r"\s-\s*(\d+/\d+)\s*$", name or "")
    if m:
        src = m.group(1)
        name = name[:m.start()]
    base, _, purpose = (name or "").strip().partition(".")
    return base.strip(), purpose.strip(), src


def _layer_ident(name: str, source: str) -> Tuple[str, str, str, str]:
    """(base, purpose, ``"l/d"``, label) of one layer-properties entry.

    sky130's .lyp names layers ``met1.drawing - 68/20`` (source ``68/20@1``);
    ihp-sg13g2's ``Activ.drawing`` (source ``1/0``); gf180mcu's leaves the name
    empty and puts it in the source (``Metal1_Label 34/10@1``). ``"l/d"`` is
    what the 3D stack is keyed by, so it must never keep the name text; the
    label is what the layer list shows, in the PDK's own spelling."""
    base, purpose, src = _split_name(name or "")
    if not base:
        spec = (source or "").split("@", 1)[0].strip()
        m = _RX_LD.search(spec)
        if m:
            src = src or m.group(0)
            spec = (spec[:m.start()] + spec[m.end():]).strip()
        base, _, purpose = spec.partition(".")
        base, purpose = base.strip(), purpose.strip()
        if not base:
            base = src
    if not src:
        m = _RX_LD.search(source or "")
        src = m.group(0) if m else ""
    label = (base + ("." + purpose if purpose else "")) or src
    if not purpose:
        m = _RX_SUFFIX_PURPOSE.match(base)
        if m:
            base, purpose = m.group(1), m.group(2).lower()
    return base, purpose, src, label


def _group_of(base: str, purpose: str) -> str:
    p = purpose.lower()
    if p in ("pin", "label", "text", "net", "port"):
        return "pin"
    if p in _MARKER_PURPOSES:
        return "misc"
    b = base.lower()
    if _RX_LOCAL.match(b):
        return "local"
    if _RX_METAL.match(b) or _RX_VIA.match(b):
        return "metal"
    if _RX_WELL.search(b):
        return "well"
    if _RX_DEVICE.match(b):
        return "device"
    if b.startswith(("met", "metal", "via")):
        return "metal"
    return "misc"


def _layer_words(base: str) -> str:
    b = base.lower()
    m = re.match(r"^(met|metal)(\d)$", b)
    if m:
        return _LAYER_WORDS[m.group(1)].format(n=m.group(2))
    if re.match(r"^(?:top)?via(\d*)$", b):
        return _LAYER_WORDS["via"]
    if re.match(r"^topmetal\d?$", b):
        return _LAYER_WORDS["topmetal"]
    b = _WORD_ALIAS.get(b, b)
    for k, v in _LAYER_WORDS.items():
        if b == k or (k in ("licon", "li") and b.startswith(k)):
            return v
    return ""


_RX_Z_METAL = re.compile(r"^(?:met|metal|m)(\d)$")
_RX_Z_VIA = re.compile(r"^(?:via|v)(\d*)$")
_RX_Z_TOPMETAL = re.compile(r"^(?:topmetal|tm)(\d?)$")
_RX_Z_TOPVIA = re.compile(r"^(?:topvia|tv)(\d?)$")


def _z_rank(base: str, purpose: str) -> float:
    """Rough stacking order, top first, so a click reports the top shape first.

    Metal n sits at 10 + 2n with via n just above it (via1 between metal1 and
    metal2); IHP's TopVia1 < TopMetal1 < TopVia2 < TopMetal2 above the thin
    metals; contacts (licon, Cont, Contact) under li/metal1, poly above
    diffusion, wells at the bottom."""
    b = base.lower()
    m = _RX_Z_METAL.match(b)
    mv = _RX_Z_VIA.match(b)
    mt = _RX_Z_TOPMETAL.match(b)
    mtv = _RX_Z_TOPVIA.match(b)
    if m:
        z = 10 + 2 * int(m.group(1))
    elif mv:
        z = 11 + 2 * int(mv.group(1) or 1)
    elif mt:
        z = 28 + 2 * int(mt.group(1) or 1)      # TopMetal1 30, TopMetal2 32
    elif mtv:
        z = 27 + 2 * int(mtv.group(1) or 1)     # TopVia1 29, TopVia2 31
    elif b == "metaltop":
        z = 30
    elif b == "rdl":
        z = 34
    elif b == "pad":
        z = 36
    elif b == "mcon":
        z = 9
    elif b.startswith("licon") or b in ("cont", "contact", "co"):
        z = 7
    elif b.startswith("li"):
        z = 8
    elif b in ("poly", "poly2", "gatpoly"):
        z = 5
    elif b in ("diff", "tap", "activ", "active", "comp", "od"):
        z = 4
    elif _RX_WELL.search(b):
        z = 1
    else:
        z = 0
    if purpose.lower() in ("pin", "label", "text"):
        z += 0.5
    return z


def _hex(c: int) -> str:
    return "#%06x" % (int(c) & 0xFFFFFF)


# --------------------------------------------------------------------------- names
def _prop(obj: Any, key: int) -> Optional[str]:
    """A KLayout shape's or instance's property *key* as text, or None."""
    try:
        if not obj.prop_id:
            return None
        v = obj.property(key)
    except Exception:
        return None
    return str(v) if v not in (None, "") else None


def _clean(name: Any) -> str:
    """A net or instance name as KLayout and STA spell it: DEF escapes the
    brackets (``dsa\\[0\\].x``), the others do not."""
    return str(name or "").replace("\\", "").strip()


def _box_key(b: List[float]) -> Tuple[int, int, int, int]:
    return tuple(int(round(v * 1000)) for v in b[:4])  # type: ignore[return-value]


_RX_DEF_TOK = re.compile(r"\(|\)|;|[^\s();]+")


def parse_def_nets(path: Path) -> Dict[str, Any]:
    """The connections of a DEF's NETS and SPECIALNETS sections, streamed:
    ``nets: name -> [(inst, pin)]`` (``("PIN", name)`` for a top-level pin),
    ``pins: "inst/pin" -> net`` and ``use: name -> SIGNAL|CLOCK|POWER|GROUND``.
    Only what comes before a statement's first ``+`` is a connection; the
    routing after it is read by KLayout, not here."""
    nets: Dict[str, List[Tuple[str, str]]] = {}
    pins: Dict[str, str] = {}
    use: Dict[str, str] = {}
    section = ""
    cur: Optional[str] = None
    conns: List[Tuple[str, str]] = []
    in_conn = False
    grp: List[str] = []
    in_grp = False
    want_use = False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not section:
                if s.startswith("NETS ") or s.startswith("SPECIALNETS "):
                    section = s.split()[0]
                continue
            if s.startswith("END ") and s.split()[1:2] == [section]:
                section = ""
                continue
            for tok in _RX_DEF_TOK.findall(s):
                if cur is None:
                    if tok == "-":
                        cur = ""
                    continue
                if cur == "":
                    cur = _clean(tok)
                    conns, in_conn, in_grp, want_use = [], True, False, False
                    continue
                if tok == ";":
                    if cur:
                        nets.setdefault(cur, []).extend(conns)
                        for inst, pin in conns:
                            if inst == "*":
                                # A special net on every instance's pin of that
                                # name (``( * VPWR )``).
                                pins.setdefault("*/" + pin, cur)
                            elif inst != "PIN":
                                pins.setdefault(inst + "/" + pin, cur)
                    cur = None
                    continue
                if want_use:
                    use[cur] = tok.upper()
                    want_use = False
                    continue
                if tok == "+":
                    in_conn = False
                    continue
                if tok == "USE" and not in_conn:
                    want_use = True
                    continue
                if not in_conn:
                    continue
                if tok == "(":
                    grp, in_grp = [], True
                elif tok == ")":
                    if in_grp and len(grp) >= 2:
                        conns.append((_clean(grp[0]), _clean(grp[1])))
                    in_grp = False
                elif in_grp:
                    grp.append(tok)
    return {"nets": nets, "pins": pins, "use": use}


# --------------------------------------------------------------------------- sessions
class _Session:
    """One loaded layout (a run's GDS or a stage DEF) in a headless LayoutView."""

    def __init__(self, sid: str, path: Path, kind: str, cfg: Dict[str, Any], top: Optional[str] = None,
                 names_def: Optional[str] = None):
        import klayout.db as kdb
        import klayout.lay as klay

        self.sid = sid
        self.path = path
        self.kind = kind
        self.mtime = path.stat().st_mtime
        self.used = time.time()
        self.notes: List[str] = []
        # A cell session has no run folder to find its PDK from later (3D).
        self.cfg: Optional[Dict[str, Any]] = cfg if top else None
        self._cfg: Dict[str, Any] = cfg if isinstance(cfg, dict) else {}
        # A GDS has no net or instance names; the run's final DEF (the same
        # chip, same coordinates) answers those questions for it.
        self.names_def = names_def if kind == "gds" else None
        self._companion: Any = None
        self._nets: Optional[Dict[str, Any]] = None
        self._insts: Optional[Dict[str, Any]] = None
        self._conn: Optional[Dict[str, Any]] = None
        # The run's PDK and library: the census uses the Cells tab's taxonomy.
        self.lib = ((cfg or {}).get("PDK"), (cfg or {}).get("STD_CELL_LIBRARY")) if isinstance(cfg, dict) else (None, None)
        tech = _tech_files(cfg)
        v = klay.LayoutView()
        if kind == "def":
            lefs = _lef_list(cfg)
            if not lefs:
                raise RuntimeError("the run's tech and cell LEF files are missing, so this DEF can't be drawn")
            opt = kdb.LoadLayoutOptions()
            c = opt.lefdef_config
            c.macro_resolution_mode = 1          # LEF abstracts for cells (as OpenInKLayout)
            c.read_lef_with_def = False
            c.lef_files = lefs
            if tech["lym"]:
                c.map_file = tech["lym"]
            else:
                self.notes.append("No LEF/DEF layer map found, so layers keep KLayout's generic names.")
            c.produce_cell_outlines = True
            # Names on the shapes: routed shapes carry their net and placed
            # cells their instance under property 1 (KLayout's default); pin
            # shapes inside a cell carry the pin's name under property 2.
            try:
                c.net_property_name = 1
                c.instance_property_name = 1
                c.pin_property_name = 2
            except Exception:
                pass
            v.load_layout(str(path), opt, True)
        else:
            v.load_layout(str(path), True)
        if top:
            ly = v.cellview(0).layout()
            if not ly.has_cell(top):
                raise LookupError(top + " is not in " + path.name)
            v.select_cell(ly.cell_by_name(top), 0)
        if tech["lyp"]:
            try:
                v.load_layer_props(tech["lyp"])
            except Exception:
                self.notes.append("The PDK layer properties could not be loaded; showing KLayout's default colours.")
        else:
            self.notes.append("No PDK layer properties (.lyp) found; showing KLayout's default colours.")
        for k, val in (("grid-visible", "false"), ("grid-show-ruler", "false"),
                       ("background-color", "#06080d"), ("text-visible", "true"),
                       ("cell-box-text-transform", "true"), ("min-inst-label-size", "120"),
                       ("cell-box-color", "#8ea6c4"),
                       # A vector font scales with the zoom: labels stay out of
                       # the way at fit and become readable as you zoom in (the
                       # page draws the chip's I/O names itself, at a fixed size).
                       ("text-font", "2"),
                       ("draw-array-border-instances", "false")):
            try:
                v.set_config(k, val)
            except Exception:
                pass
        v.max_hier()
        v.zoom_fit()
        self.view = v
        self.cv = v.cellview(0)
        self.layout = self.cv.layout()
        self.top = self.cv.cell
        self.dbu = self.layout.dbu
        self.applied: Dict[str, Any] = {}
        self._layers = self._scan_layers()

    # -- layers
    def _stipple(self, idx: int, cache: Dict[int, List[str]]) -> List[str]:
        """KLayout's stipple bitmap for a dither index, as rows of ``*``/``.``
        (sky130 gives several layers one colour; only the stipple tells them
        apart, so the page's key draws the real one)."""
        if idx in cache:
            return cache[idx]
        rows: List[str] = []
        try:
            raw = self.view.get_stipple(idx) if idx >= 0 else ""
            rows = [r.strip() for r in str(raw or "").splitlines() if r.strip() and set(r.strip()) <= {"*", "."}]
            rows = [r[:32] for r in rows[:32]]
        except Exception:
            rows = []
        cache[idx] = rows
        return rows

    def _scan_layers(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        it = self.view.begin_layers()
        pos = 0
        stipples: Dict[int, List[str]] = {}
        while not it.at_end():
            lp = it.current()
            li = lp.layer_index()
            empty = True
            if li >= 0:
                try:
                    empty = self.top.dbbox_per_layer(li).empty()
                except Exception:
                    empty = True
            if not empty:
                base, purpose, src, label = _layer_ident(lp.name or "", lp.source or "")
                try:
                    dither = int(lp.eff_dither_pattern(True))
                except Exception:
                    dither = int(lp.dither_pattern)
                out.append({
                    "id": pos, "name": base, "purpose": purpose, "src": src,
                    "label": label,
                    "fill": _hex(lp.fill_color), "frame": _hex(lp.frame_color),
                    "dither": int(lp.dither_pattern), "visible": bool(lp.visible),
                    "stipple": self._stipple(dither, stipples),
                    "group": _group_of(base, purpose), "z": _z_rank(base, purpose),
                    "about": _layer_words(base), "li": li,
                })
            it.next()
            pos += 1
        return out

    def info(self) -> Dict[str, Any]:
        bb = self.top.dbbox()
        groups = [{"id": g, "label": lbl} for g, lbl in _GROUPS]
        names = None
        if self.kind == "def":
            names = {"from": "def", "file": self.path.name}
        elif self.names_def:
            names = {"from": "companion", "file": Path(self.names_def).name}
        return {
            "sid": self.sid, "kind": self.kind, "file": self.path.name,
            "top": self.top.name, "dbu": self.dbu,
            "bbox": [bb.left, bb.bottom, bb.right, bb.top],
            "layers": [{k: v for k, v in L.items() if k != "li"} for L in self._layers],
            "groups": groups, "notes": self.notes,
            "cells": self.cell_stats(), "labels": self.labels(),
            "names": names, "pdn": pdn_info(self._cfg) if self._cfg else None,
        }

    def _apply(self, visible: Optional[List[int]], depth: Optional[int], labels: bool,
               bg: str, boxes: bool) -> None:
        v = self.view
        # None keeps what is there (the .lyp defaults until a client sets it).
        want_vis = None if visible is None else set(visible)
        if want_vis is not None and want_vis != self.applied.get("visible", "unset"):
            ids = {L["id"] for L in self._layers}
            it = v.begin_layers()
            pos = 0
            while not it.at_end():
                if pos in ids:
                    on = pos in want_vis
                    node = it.current()
                    if node.visible != on:
                        node.visible = on
                it.next()
                pos += 1
            self.applied["visible"] = want_vis
        if depth != self.applied.get("depth", "unset"):
            v.min_hier_levels = 0
            if depth is None or depth < 0:
                v.max_hier()
            else:
                v.max_hier_levels = int(depth)
            self.applied["depth"] = depth
        if labels != self.applied.get("labels"):
            v.set_config("text-visible", "true" if labels else "false")
            self.applied["labels"] = labels
        if bg != self.applied.get("bg"):
            v.set_config("background-color", bg)
            self.applied["bg"] = bg
        if boxes != self.applied.get("boxes"):
            try:
                v.set_config("cell-box-visible", "true" if boxes else "false")
            except Exception:
                pass
            self.applied["boxes"] = boxes

    def render(self, box: Optional[List[float]], w: int, h: int, *, visible: Optional[List[int]] = None,
               depth: Optional[int] = None, labels: bool = True, bg: str = "#06080d",
               boxes: bool = True, oversampling: int = 1, resolution: float = 0.0) -> bytes:
        import klayout.db as kdb

        self.used = time.time()
        w = max(16, min(_MAX_PX, int(w)))
        h = max(16, min(_MAX_PX, int(h)))
        self._apply(visible, depth, labels, bg, boxes)
        target = kdb.DBox(box[0], box[1], box[2], box[3]) if box else self.top.dbbox()
        os_ = max(1, min(3, int(oversampling)))
        pb = self.view.get_pixels_with_options(w, h, 1, os_, float(resolution or 0.0), target)
        return bytes(pb.to_png_data())

    # -- content
    def cell_stats(self) -> List[Dict[str, Any]]:
        """Cells placed directly in the top cell, with counts (most used first)."""
        counts: Dict[int, int] = {}
        for inst in self.top.each_inst():
            n = inst.size() if inst.is_regular_array() else 1
            counts[inst.cell_index] = counts.get(inst.cell_index, 0) + n
        names = [self.layout.cell(ci).name for ci in counts]
        try:
            from .cells import census
            kinds = census(names, *getattr(self, "lib", (None, None)))
        except Exception:  # pragma: no cover - cells module always ships
            kinds = {}
        out = []
        for ci, n in counts.items():
            c = self.layout.cell(ci)
            bb = c.dbbox()
            k = kinds.get(c.name) or {"kind": "other", "label": "Other", "desc": ""}
            out.append({"name": c.name, "count": n, "kind": k["kind"], "kind_label": k["label"], "desc": k["desc"],
                        "w": round(bb.width(), 4), "h": round(bb.height(), 4),
                        "area": round(bb.width() * bb.height() * n, 3),
                        "children": c.child_cells()})
        out.sort(key=lambda r: (-r["count"], r["name"]))
        return out

    def instances(self, cell_name: str) -> Dict[str, Any]:
        """Bounding boxes (top-cell µm) of every placement of one cell type."""
        ci = self.layout.cell_by_name(cell_name) if self.layout.has_cell(cell_name) else None
        boxes: List[List[float]] = []
        total = 0
        if ci is not None:
            for inst in self.top.each_inst():
                if inst.cell_index != ci:
                    continue
                total += 1
                if len(boxes) < _MAX_INSTANCES:
                    bb = inst.dbbox()
                    boxes.append([round(bb.left, 4), round(bb.bottom, 4), round(bb.right, 4), round(bb.top, 4)])
        return {"cell": cell_name, "total": total, "boxes": boxes, "truncated": total > len(boxes)}

    def labels(self) -> List[Dict[str, Any]]:
        """Text labels in the top cell (pin names), for the search box."""
        import klayout.db as kdb

        out: List[Dict[str, Any]] = []
        by_li = {L["li"]: L for L in self._layers}
        for li in self.layout.layer_indexes():
            L = by_li.get(li)
            for s in self.top.shapes(li).each(kdb.Shapes.STexts):
                t = s.dtext
                out.append({"text": t.string, "x": round(t.x, 4), "y": round(t.y, 4),
                            "layer": (L or {}).get("label") or self.layout.get_info(li).to_s()})
                if len(out) >= _MAX_LABELS:
                    return out
        return out

    def probe(self, x: float, y: float, r: float, layers: Optional[List[str]] = None,
              all_layers: bool = False) -> List[Dict[str, Any]]:
        """Shapes under a click on the visible layers, top of the stack first.

        On a DEF each hit names its net (a routed shape) or its instance and
        pin (a shape inside a placed cell); on a final GDS the run's final DEF
        answers for the top hits (see ``names_def``)."""
        import klayout.db as kdb

        box = kdb.DBox(x - r, y - r, x + r, y + r)
        vis = None if all_layers else self.applied.get("visible")
        hits: List[Dict[str, Any]] = []
        want = set(layers) if layers else None
        for L in self._layers:
            if vis is not None and L["id"] not in vis:
                continue
            if want is not None and L["src"] not in want:
                continue
            it = self.top.begin_shapes_rec_touching(L["li"], box)
            n = 0
            while not it.at_end() and n < 6:
                shape = it.shape()
                path = it.path()
                inst_cell = None
                inst_box = None
                inst_name = None
                pin = None
                net = None
                if path:
                    inst = path[0].inst()
                    inst_cell = self.layout.cell(inst.cell_index).name
                    ib = inst.dbbox()
                    inst_box = [ib.left, ib.bottom, ib.right, ib.top]
                    if self.kind == "def":
                        inst_name = _prop(inst, 1)
                        pin = _prop(shape, 2)
                elif self.kind == "def":
                    net = _prop(shape, 1)
                sb = shape.dbbox().transformed(it.dtrans())
                text = shape.text_string if shape.is_text() else None
                hits.append({"layer": L["label"], "src": L["src"], "fill": L["fill"], "z": L["z"],
                             "about": L["about"], "cell": inst_cell, "cell_box": inst_box,
                             "box": [sb.left, sb.bottom, sb.right, sb.top], "text": text,
                             "area": sb.width() * sb.height(),
                             "inst": inst_name, "pin": pin, "net": net})
                n += 1
                it.next()
        hits.sort(key=lambda h: (-h["z"], h["area"]))
        hits = hits[:40]
        if self.kind == "def":
            # A pin inside a cell: its net from the DEF's NETS connections.
            for h in hits[:8]:
                if h["inst"] and h["pin"] and not h["net"]:
                    h["net"] = self.conn_net(h["inst"], h["pin"])
        elif self.names_def and hits:
            comp = self.companion()
            if comp is not None:
                by_box = comp._inst_by_box()
                for h in hits[:4]:
                    h["named"] = True
                    cb = h.get("cell_box")
                    if cb:
                        h["inst"] = by_box.get(_box_key(cb))
                        continue
                    # A top-level shape: the routed shape of the DEF at that
                    # place, on the same layer, names its net.
                    try:
                        sub = comp.probe(x, y, r, layers=[h["src"]], all_layers=True)
                    except Exception:
                        sub = []
                    h["net"] = next((s["net"] for s in sub if s.get("net")), None)
        return hits

    def _inst_by_box(self) -> Dict[Tuple[int, int, int, int], str]:
        if getattr(self, "_ibox", None) is None:
            self._ibox = {_box_key(b): n for n, (_c, b) in self._inst_index().items()}
        return self._ibox

    # -- names: nets and instances (DEF property 1, the DEF's NETS section)
    def companion(self) -> Optional["_Session"]:
        """The final DEF of the run this GDS belongs to, loaded for its names."""
        if self.kind == "def":
            return self
        if not self.names_def:
            return None
        if self._companion is None:
            try:
                self._companion = _Session(self.sid + "#names", Path(self.names_def), "def", self._cfg)
            except Exception as ex:
                _log.info("companion DEF for names failed: %s", ex)
                self._companion = False
        return self._companion or None

    def _net_index(self) -> Dict[str, Any]:
        """``net -> {layers: {src: [x0,y0,x1,y1, ...]}, n, bbox, length}`` from
        the routed shapes (built once, on the first question)."""
        if self._nets is not None:
            return self._nets
        import klayout.db as kdb

        idx: Dict[str, Any] = {}
        for L in self._layers:
            if L["group"] not in ("metal", "local", "pin"):
                continue
            for s in self.top.shapes(L["li"]).each(kdb.Shapes.SBoxes | kdb.Shapes.SPolygons | kdb.Shapes.SPaths):
                name = _prop(s, 1)
                if not name:
                    continue
                b = s.dbbox()
                e = idx.get(name)
                if e is None:
                    e = idx[name] = {"layers": {}, "n": 0, "bbox": [b.left, b.bottom, b.right, b.top], "length": 0.0}
                arr = e["layers"].setdefault(L["src"] or L["label"], [])
                arr.extend((round(b.left, 4), round(b.bottom, 4), round(b.right, 4), round(b.top, 4)))
                e["n"] += 1
                bb = e["bbox"]
                bb[0] = min(bb[0], b.left); bb[1] = min(bb[1], b.bottom)
                bb[2] = max(bb[2], b.right); bb[3] = max(bb[3], b.top)
                if L["group"] != "pin":
                    e["length"] += max(b.width(), b.height())
        self._nets = idx
        return idx

    def _inst_index(self) -> Dict[str, Any]:
        """``instance -> (cell, [x0,y0,x1,y1])`` of every placed cell."""
        if self._insts is not None:
            return self._insts
        out: Dict[str, Any] = {}
        for inst in self.top.each_inst():
            name = _prop(inst, 1)
            if not name:
                continue
            b = inst.dbbox()
            out[name] = (self.layout.cell(inst.cell_index).name, [round(b.left, 4), round(b.bottom, 4), round(b.right, 4), round(b.top, 4)])
        self._insts = out
        return out

    def _connections(self) -> Dict[str, Any]:
        """The DEF's NETS and SPECIALNETS: who each net joins, and its use."""
        if self._conn is None:
            try:
                self._conn = parse_def_nets(self.path)
            except Exception as ex:
                _log.info("DEF NETS parse failed: %s", ex)
                self._conn = {"nets": {}, "pins": {}, "use": {}}
        return self._conn

    def conn_net(self, inst: str, pin: str) -> Optional[str]:
        pins = self._connections()["pins"]
        return pins.get(_clean(inst) + "/" + pin) or pins.get("*/" + pin)

    def _net_pins(self, name: str) -> List[Dict[str, Any]]:
        """Where a net's pins are: each cell pin at its instance's centre, a
        top-level pin at its label."""
        conn = self._connections()["nets"].get(name) or []
        insts = self._inst_index()
        labels = None
        out: List[Dict[str, Any]] = []
        for inst, pin in conn[:2000]:
            if inst == "PIN":
                if labels is None:
                    labels = {lb["text"]: lb for lb in self.labels()}
                lb = labels.get(pin)
                if lb:
                    out.append({"inst": None, "pin": pin, "x": lb["x"], "y": lb["y"]})
                continue
            hit = insts.get(inst)
            if hit:
                b = hit[1]
                out.append({"inst": inst, "pin": pin, "cell": hit[0], "x": round((b[0] + b[2]) / 2, 4), "y": round((b[1] + b[3]) / 2, 4)})
        return out

    def nets_search(self, q: str, limit: int = 60) -> Dict[str, Any]:
        """Nets whose name holds *q* (case-insensitive, DEF escapes ignored),
        exact and prefix matches first."""
        comp = self.companion()
        if comp is None:
            return {"nets": [], "total": 0, "names": False}
        idx = comp._net_index()
        conn = comp._connections()
        names = set(idx) | set(conn["nets"])
        qq = _clean(q).lower()
        hits = [n for n in names if qq in n.lower()] if qq else []
        hits.sort(key=lambda n: (n.lower() != qq, not n.lower().startswith(qq), len(n), n))
        out = []
        lim = max(1, min(int(limit), _MAX_NET_HITS))
        for n in hits[:lim]:
            e = idx.get(n) or {}
            out.append({"name": n, "shapes": e.get("n", 0), "layers": sorted(e.get("layers", {}).keys()),
                        "bbox": e.get("bbox"), "length": round(e.get("length", 0.0), 2),
                        "pins": len(conn["nets"].get(n) or []), "use": conn["use"].get(n, "")})
        # Placed instances by name (``_186_``, ``clkbuf_0_clk``), same rules.
        ii = comp._inst_index()
        ihits = [n for n in ii if qq in n.lower()] if qq else []
        ihits.sort(key=lambda n: (n.lower() != qq, not n.lower().startswith(qq), len(n), n))
        insts = [{"name": n, "cell": ii[n][0], "box": ii[n][1]} for n in ihits[:lim]]
        return {"nets": out, "total": len(hits), "names": True, "insts": insts, "insts_total": len(ihits)}

    def net_geom(self, name: str) -> Dict[str, Any]:
        """One net's shapes, pins and box, for a highlight."""
        comp = self.companion()
        if comp is None:
            return {"name": name, "found": False, "names": False}
        name = _clean(name)
        idx = comp._net_index()
        e = idx.get(name)
        pins = comp._net_pins(name)
        if e is None and not pins:
            return {"name": name, "found": False, "names": True}
        boxes: List[List[Any]] = []
        total = 0
        for src, arr in (e or {}).get("layers", {}).items():
            for i in range(0, len(arr), 4):
                total += 1
                if len(boxes) < _MAX_NET_SHAPES:
                    boxes.append([src, arr[i], arr[i + 1], arr[i + 2], arr[i + 3]])
        bbox = (e or {}).get("bbox")
        if bbox is None and pins:
            xs = [p["x"] for p in pins]
            ys = [p["y"] for p in pins]
            bbox = [min(xs), min(ys), max(xs), max(ys)]
        conn = comp._connections()
        return {"name": name, "found": True, "names": True, "boxes": boxes, "total": total,
                "truncated": total > len(boxes), "bbox": bbox, "pins": pins,
                "layers": sorted((e or {}).get("layers", {}).keys()),
                "length": round((e or {}).get("length", 0.0), 2), "use": conn["use"].get(name, "")}

    def locate(self, insts: List[str], nets: List[str]) -> Dict[str, Any]:
        """Where named instances and nets are (a timing path's cells and nets)."""
        comp = self.companion()
        if comp is None:
            return {"insts": {}, "nets": {}, "missing": {"insts": list(insts), "nets": list(nets)}, "names": False}
        ii = comp._inst_index()
        found_i: Dict[str, Any] = {}
        miss_i: List[str] = []
        for n in insts[:5000]:
            c = _clean(n)
            hit = ii.get(c)
            if hit:
                found_i[n] = {"cell": hit[0], "box": hit[1]}
            else:
                miss_i.append(n)
        found_n: Dict[str, Any] = {}
        miss_n: List[str] = []
        for n in nets[:400]:
            g = self.net_geom(n)
            if g.get("found"):
                found_n[n] = g
            else:
                miss_n.append(n)
        return {"insts": found_i, "nets": found_n, "missing": {"insts": miss_i, "nets": miss_n}, "names": True}


def _sid(run_dir: Path, source: str) -> str:
    return str(Path(run_dir).resolve()) + "::" + (source or "final")


def open_session(run_dir: Path, source: str) -> Dict[str, Any]:
    """Load (or reuse) a source of a run and return what the viewer needs."""
    av = available()
    if not av["ok"]:
        return {"ok": False, "error": av["reason"], "need": "klayout"}
    run_dir = Path(run_dir)
    path, kind = _source_path(run_dir, source)
    if path is None or not path.is_file():
        return {"ok": False, "error": "this run has no layout for '" + (source or "final") + "'"}
    names_def = None
    if kind == "gds" and (source in ("", "final") or source.startswith("final:")):
        d = _final_def(run_dir)
        names_def = str(d) if d is not None else None
    spec = {"sid": _sid(run_dir, source), "path": str(path), "kind": kind, "run_dir": str(run_dir),
            "top": None, "cfg": None, "names_def": names_def}
    if _WORKERS is not None:
        _remember(spec)
        try:
            info = _call("view", "open", {"spec": spec}, timeout=300)
        except Exception as ex:
            return {"ok": False, "error": _worker_words(ex, path.name)}
    else:
        info = _open_spec(spec)
    if info.get("ok"):
        info["klayout"] = av.get("version")
    return info


def _open_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    cfg = spec.get("cfg")
    run_dir = spec.get("run_dir")
    return _open(spec["sid"], Path(spec["path"]), spec["kind"],
                 (lambda: cfg) if cfg is not None else (lambda: _resolved(Path(run_dir)) if run_dir else {}),
                 top=spec.get("top"), names_def=spec.get("names_def"))


def _evict() -> None:
    """Drop the least recently used sessions past each kind's cap."""
    cells = [k for k in _SESSIONS if k.startswith(_CELL_PREFIX)]
    runs = [k for k in _SESSIONS if not k.startswith(_CELL_PREFIX)]
    for k in cells[:max(0, len(cells) - _MAX_CELL_SESSIONS)] + runs[:max(0, len(runs) - _MAX_SESSIONS)]:
        _SESSIONS.pop(k, None)


def _open(sid: str, path: Path, kind: str, cfg: Any, top: Optional[str] = None,
          names_def: Optional[str] = None) -> Dict[str, Any]:
    """Load *path* into a session (or reuse a current one) and describe it.
    *cfg* is called only when a new session has to be built."""
    with _LOCK:
        s = _SESSIONS.get(sid)
        if s is not None and s.mtime != path.stat().st_mtime:
            _SESSIONS.pop(sid, None)
            s = None
        if s is None:
            t0 = time.time()
            try:
                s = _Session(sid, path, kind, cfg(), top=top, names_def=names_def)
            except LookupError as ex:
                return {"ok": False, "error": str(ex).strip("'\"")}
            except Exception as ex:
                _log.exception("layout session open failed")
                return {"ok": False, "error": "KLayout could not read " + path.name + ": " + str(ex)}
            _log.info("layout session %s opened in %.2fs", sid, time.time() - t0)
            _SESSIONS[sid] = s
            _evict()
        else:
            _SESSIONS.move_to_end(sid)
        info = s.info()
    info["ok"] = True
    return info


def open_cell_session(pdk: str, scl: str, cell: str) -> Dict[str, Any]:
    """A viewer session on one standard cell: the library's GDS with *cell* as
    the top. The names come from a request; ``celllib.cell_gds`` only resolves
    them to a file inside the installed PDK."""
    from . import celllib

    av = available()
    if not av["ok"]:
        return {"ok": False, "error": av["reason"], "need": "klayout"}
    gds, why = celllib.cell_gds(pdk, scl, cell)
    if gds is None:
        return {"ok": False, "error": why}
    root = celllib.pdk_dir(pdk)
    cfg = {"PDK": pdk, "PDK_ROOT": str(root.parent) if root is not None else ""}
    spec = {"sid": _CELL_PREFIX + pdk + "::" + scl + "::" + cell, "path": str(gds), "kind": "gds",
            "run_dir": None, "top": cell, "cfg": cfg, "names_def": None}
    if _WORKERS is not None:
        _remember(spec)
        try:
            info = _call("view", "open", {"spec": spec}, timeout=120)
        except Exception as ex:
            return {"ok": False, "error": _worker_words(ex, Path(gds).name)}
    else:
        info = _open_spec(spec)
    if info.get("ok"):
        info["klayout"] = av.get("version")
    return info


def close_session(sid: str) -> bool:
    """Forget a session now (the page moved on). True when one was open."""
    if _WORKERS is not None:
        known = _SPECS.pop(sid, None) is not None
        for role in ("view", "bg"):     # the thumb worker keeps no sessions
            try:
                _call(role, "close", {"sid": sid}, timeout=20, spawn=False)
            except Exception:
                pass
        return known
    with _LOCK:
        return _SESSIONS.pop(sid, None) is not None


def session(sid: str) -> Optional[_Session]:
    with _LOCK:
        s = _SESSIONS.get(sid)
        if s is not None:
            _SESSIONS.move_to_end(sid)
        return s


def _with(sid: str, fn: Any, role: str = "view", op: str = "", kw: Optional[Dict[str, Any]] = None,
          timeout: float = 120.0, key: Any = None) -> Any:
    """Run *fn(session)* here, or *op* in a worker (None: no such session)."""
    if _WORKERS is not None:
        spec = _SPECS.get(sid)
        if spec is None:
            return None
        _SPECS.move_to_end(sid)
        return _call(role, op, dict(kw or {}, spec=spec), timeout=timeout, key=key)
    with _LOCK:
        s = _SESSIONS.get(sid)
        return None if s is None else fn(s)


def render(sid: str, heavy: bool = False, **kw: Any) -> Optional[bytes]:
    """PNG of a viewport. *heavy* (a 4K or 8K save) goes to the background
    worker, so the view stays live while it draws."""
    return _with(sid, lambda s: s.render(**kw), role="bg" if heavy else "view", op="render",
                 kw={"kw": kw}, timeout=600 if heavy else 120)


def probe(sid: str, x: float, y: float, r: float) -> Optional[List[Dict[str, Any]]]:
    return _with(sid, lambda s: s.probe(x, y, r), op="probe", kw={"x": x, "y": y, "r": r})


def instances(sid: str, cell: str) -> Optional[Dict[str, Any]]:
    return _with(sid, lambda s: s.instances(cell), op="instances", kw={"cell": cell})


def nets(sid: str, q: str, limit: int = 60) -> Optional[Dict[str, Any]]:
    """Nets whose name holds *q* (see ``_Session.nets_search``)."""
    return _with(sid, lambda s: s.nets_search(q, limit), op="nets", kw={"q": q, "limit": limit}, timeout=300)


def net(sid: str, name: str) -> Optional[Dict[str, Any]]:
    return _with(sid, lambda s: s.net_geom(name), op="net", kw={"name": name}, timeout=300)


def locate(sid: str, insts: List[str], nets_: List[str]) -> Optional[Dict[str, Any]]:
    return _with(sid, lambda s: s.locate(insts, nets_), op="locate", kw={"insts": insts, "nets": nets_}, timeout=300)


# Thumbnails live next to the run (``<run>/.lanex/thumbs``): made once per
# layout file and served from disk, so the stage strip fills in at once on
# every later visit and never waits on KLayout.
_THUMB_VERSION = "1"


def _thumb_file(run_dir: Path, source: str, path: Path, w: int, h: int, bg: str) -> Path:
    st = path.stat()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source or "final")[:80]
    return run_dir / ".lanex" / "thumbs" / ("%s__%dx%d__%s__%d_%d_v%s.png" % (
        safe, int(w), int(h), bg.lstrip("#"), int(st.st_mtime), st.st_size, _THUMB_VERSION))


def _thumb_png(path: str, kind: str, run_dir: str, w: int, h: int, bg: str) -> Optional[bytes]:
    with _LOCK:
        # A throwaway view: thumbnails must never push the viewer's own
        # sessions out of the cache (that made the main view expire).
        try:
            s = _Session("thumb", Path(path), kind, _resolved(Path(run_dir)))
        except Exception:
            _log.exception("layout thumbnail open failed")
            return None
        return s.render(None, w, h, visible=None, depth=None, labels=False, bg=bg, boxes=False, oversampling=3)


def thumbnail(run_dir: Path, source: str, w: int, h: int, bg: str) -> Optional[bytes]:
    """A small fit-to-window picture of one stage, cached on disk by file mtime."""
    run_dir = Path(run_dir)
    path, kind = _source_path(run_dir, source)
    if path is None or not path.is_file():
        return None
    key = (str(path), path.stat().st_mtime, int(w), int(h), bg)
    with _LOCK:
        if key in _THUMBS:
            _THUMBS.move_to_end(key)
            return _THUMBS[key]
    f = _thumb_file(run_dir, source, path, w, h, bg)
    try:
        if f.is_file():
            png = f.read_bytes()
            if png[:8] == b"\x89PNG\r\n\x1a\n":
                _keep_thumb(key, png)
                return png
    except OSError:
        pass
    if not available()["ok"]:
        return None
    if _WORKERS is not None:
        # Their own worker: a stage strip fills in while the view renders, a
        # 3D model builds or an 8K picture is saved.
        png = _call("thumb", "thumb", {"path": str(path), "kind": kind, "run_dir": str(run_dir),
                                       "w": int(w), "h": int(h), "bg": bg}, timeout=300)
    else:
        png = _thumb_png(str(path), kind, str(run_dir), int(w), int(h), bg)
    if not png:
        return None
    _keep_thumb(key, png)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        # Older pictures of the same stage at this size (the layout changed).
        for old in f.parent.glob(f.name.split("__", 1)[0] + "__%dx%d__%s__*.png" % (int(w), int(h), bg.lstrip("#"))):
            if old != f:
                try:
                    old.unlink()
                except OSError:
                    pass
        tmp = f.with_name(f.name + ".%d.tmp" % os.getpid())
        tmp.write_bytes(png)
        os.replace(tmp, f)
    except OSError:
        pass      # a read-only run folder: memory only
    return png


def _keep_thumb(key: Tuple, png: bytes) -> None:
    with _LOCK:
        _THUMBS[key] = png
        _THUMBS.move_to_end(key)
        while len(_THUMBS) > _MAX_THUMBS:
            _THUMBS.popitem(last=False)


# --------------------------------------------------------------------------- markers
_NUM = r"-?\d+(?:\.\d+)?(?:e-?\d+)?"
_RX_PT = re.compile(r"(" + _NUM + r"),(" + _NUM + r")")


def _marker_geom(text: str) -> Optional[Dict[str, Any]]:
    """A report-database ``<value>`` -> {kind, pts (list of rings), bbox} in µm."""
    t = (text or "").strip()
    kind = t.split(":", 1)[0].strip().lower() if ":" in t else ""
    if kind not in ("polygon", "box", "edge", "edge-pair", "path", "point", "text"):
        return None
    rings: List[List[List[float]]] = []
    for chunk in re.split(r"[|/]", t.split(":", 1)[1]):
        pts = [[float(a), float(b)] for a, b in _RX_PT.findall(chunk)]
        if pts:
            rings.append(pts)
    if not rings:
        return None
    if kind == "box" and len(rings[0]) == 2:
        (x1, y1), (x2, y2) = rings[0]
        rings = [[[x1, y1], [x2, y1], [x2, y2], [x1, y2]]]
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return {"kind": kind, "rings": rings, "bbox": [min(xs), min(ys), max(xs), max(ys)]}


def parse_rdb(path: Path) -> Dict[str, Any]:
    """Every category and item of a KLayout report database (streamed)."""
    cats: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    items: List[Dict[str, Any]] = []
    total = 0
    stack: List[str] = []
    cat_names: List[Optional[str]] = []
    try:
        for ev, el in ET.iterparse(str(path), events=("start", "end")):
            tag = el.tag
            if ev == "start":
                if tag == "category" and (stack[-1] if stack else "") != "item":
                    cat_names.append(None)
                stack.append(tag)
                continue
            stack.pop()
            parent = stack[-1] if stack else ""
            if tag == "name" and parent == "category" and cat_names:
                nm = (el.text or "").strip()
                outer = [c for c in cat_names[:-1] if c]
                full = ".".join(outer + [nm]) if outer else nm
                cat_names[-1] = full
                cats.setdefault(full, {"name": full, "description": "", "count": 0})
            elif tag == "description" and parent == "category" and cat_names and cat_names[-1]:
                cats[cat_names[-1]]["description"] = (el.text or "").strip()
            elif tag == "category" and parent != "item":
                if cat_names:
                    cat_names.pop()
            elif tag == "item":
                total += 1
                cat = (el.findtext("category") or "").strip().strip("'").replace("'.'", ".")
                entry = cats.setdefault(cat, {"name": cat, "description": "", "count": 0})
                entry["count"] += 1
                if len(items) < _MAX_MARKERS:
                    geoms = []
                    for val in el.iter("value"):
                        g = _marker_geom(val.text or "")
                        if g:
                            geoms.append(g)
                    if geoms:
                        xs = [g["bbox"][0] for g in geoms] + [g["bbox"][2] for g in geoms]
                        ys = [g["bbox"][1] for g in geoms] + [g["bbox"][3] for g in geoms]
                        items.append({"cat": cat, "geoms": geoms,
                                      "bbox": [min(xs), min(ys), max(xs), max(ys)]})
                el.clear()
    except Exception as ex:
        return {"ok": False, "error": "could not read " + path.name + ": " + str(ex),
                "total": 0, "categories": [], "items": []}
    cl = [c for c in cats.values() if c["count"]]
    cl.sort(key=lambda c: -c["count"])
    return {"ok": True, "total": total, "categories": cl, "items": items,
            "truncated": total > len(items)}


def markers(run_dir: Path) -> Dict[str, Any]:
    """Marker databases the run wrote (DRC, routing DRC, XOR), parsed."""
    run_dir = Path(run_dir)
    steps = _step_dirs(run_dir)
    out = []
    try:
        from . import history
        metrics = history._load_metrics(run_dir)
    except Exception:
        metrics = {}
    metrics_rel = "final/metrics.json" if (run_dir / "final" / "metrics.json").is_file() else "metrics.json"
    for src in _MARKER_SOURCES:
        hit = None
        for d in steps:
            if _step_key(d.name) == src["step"]:
                files = sorted(p for p in d.glob(src["glob"]) if p.is_file())
                if files:
                    hit = files[0]
        if hit is None:
            continue
        res = parse_rdb(hit)
        res.update({"id": src["id"], "label": src["label"], "path": hit.relative_to(run_dir).as_posix(),
                    "cap": _MAX_MARKERS})
        # The metric LibreLane judged the run on (what Verify uses). A report
        # file that disagrees with it (edited, or left over from an earlier
        # run) is shown, and the disagreement said, never silently trusted.
        mkey = _MARKER_METRIC.get(src["id"])
        mval = metrics.get(mkey) if mkey else None
        if isinstance(mval, (int, float)) and not isinstance(mval, bool):
            res["metric"] = {"key": mkey, "value": mval, "file": metrics_rel}
            # Counted as a disagreement only when one side is zero: the tools
            # may count a multi-box error differently, but "no errors" next
            # to markers (or the reverse) is a real contradiction.
            res["disagree"] = bool(res.get("ok")) and (int(mval) == 0) != (int(res.get("total") or 0) == 0)
        out.append(res)
    return {"sources": out}


_MARKER_METRIC = {
    "klayout-drc": "klayout__drc_error__count",
    "magic-drc": "magic__drc_error__count",
}


def viewer_commands(run_dir: Path, tag: str) -> Dict[str, str]:
    """Terminal commands that open this run the way LibreLane users do it."""
    run_dir = Path(run_dir)
    out: Dict[str, str] = {}
    gui: Dict[str, Any] = {}
    try:
        gui = json.loads((run_dir / "gui-run.json").read_text(encoding="utf-8")) or {}
    except Exception:
        gui = {}
    cli = gui.get("cli_command") or {}
    base = cli.get(gui.get("run_mode") or "container") or cli.get("local") or ""
    # Drop any "-f <flow>" / "--flow <flow>" and the -c overrides; the viewer flow
    # reads the run's own state, so only the config, PDK and run tag matter.
    if base:
        base = re.sub(r"\s(?:-f|--flow)\s+\S+", "", base)
        base = re.sub(r"\s-c\s+'[^']*'", "", base)
        base = re.sub(r"\s-c\s+\S+", "", base)
        out["klayout_flow"] = base + " --run-tag " + tag + " --flow OpenInKLayout"
        out["openroad_flow"] = base + " --run-tag " + tag + " --flow OpenInOpenROAD"
    cfg = _resolved(run_dir)
    tech = _tech_files(cfg)
    gds = _final_gds(run_dir)
    if gds is not None:
        parts = ["klayout"]
        lyt = _existing(cfg.get("KLAYOUT_TECH"))
        if lyt:
            parts += ["-nn", lyt]
        if tech["lyp"]:
            parts += ["-l", tech["lyp"]]
        parts.append(str(gds))
        out["klayout"] = " ".join(parts)
    out["cwd"] = str(run_dir.parent.parent)
    return out


# --------------------------------------------------------------------------- 3D
# The in-app 3D view: each layer's merged shapes as flat triangles (the top
# face) plus their outline edges (the browser extrudes the walls), stacked at
# real heights: the PDK's GDS3D process file (LanEx bundles one per supported
# PDK, desktop.gds3d_process_file), else the PDK's own KLayout 2.5D stack
# (libs.tech/klayout/tech/d25/*.lyd25), else an even, honest-looking generic
# spacing (said so in the reply).
_MAX_3D_PRIMS = 2_600_000


def _gds3d_stack(pdk: Optional[str]) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """``"l/d" -> {name, z0, z1 (µm), color}`` from the PDK's GDS3D process file."""
    try:
        from .desktop import gds3d_process_file
        path = gds3d_process_file(pdk)
    except Exception:
        path = None
    if not path:
        return {}, None
    return _parse_gds3d(path)


def _parse_gds3d(path: str) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    out: Dict[str, Dict[str, Any]] = {}
    cur: Dict[str, str] = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line != "LayerEnd" and ":" not in line:
                continue
            if line == "LayerEnd":
                try:
                    key = cur["Layer"].strip() + "/" + cur.get("Datatype", "0").strip()
                    h = float(cur.get("Height", "0")) / 1000.0
                    t = float(cur.get("Thickness", "0")) / 1000.0
                    rgb = [float(cur.get(c, "0.5")) for c in ("Red", "Green", "Blue")]
                    if cur.get("Show", "1").strip() != "0" and t > 0 and key != "255/0":
                        out[key] = {"name": cur.get("LayerStart", key).strip(), "z0": h, "z1": h + t,
                                    "color": "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c * 255)))) for c in rgb),
                                    "metal": cur.get("Metal", "0").strip() == "1"}
                except Exception:
                    pass
                cur = {}
                continue
            k, _, v = line.partition(":")
            cur[k.strip()] = v.strip()
    except Exception:
        return {}, None
    return out, path


_RX_D25_INPUT = re.compile(r"^\s*(\w+)\s*=\s*input\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*$", re.M)
_RX_D25_Z = re.compile(r"^\s*z\(\s*(?:input\(\s*(\d+)\s*,\s*(\d+)\s*\)|(\w+))\s*,(.*)\)\s*$", re.M)


def _d25_stack(cfg: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """``"l/d" -> {name, z0, z1, color}`` from the PDK's KLayout 2.5D stack.

    A ``.lyd25`` script declares ``Metal1 = input(8, 0)`` and then
    ``z(Metal1, zstart: 1.04, height: 0.42, color: 0x39bfff)`` in µm. Only
    layers drawn straight from one GDS layer are used; derived ones (boolean
    expressions such as resistor regions) have no single ``l/d`` to key by."""
    d = _pdk_tech_dir(cfg)
    files = sorted((d / "d25").glob("*.lyd25")) if d is not None and (d / "d25").is_dir() else []
    for f in files:
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            try:
                text = ET.fromstring(raw).findtext("text") or raw
            except ET.ParseError:
                text = raw
            inputs = {m.group(1): m.group(2) + "/" + m.group(3) for m in _RX_D25_INPUT.finditer(text)}
            out: Dict[str, Dict[str, Any]] = {}
            for m in _RX_D25_Z.finditer(text):
                key = (m.group(1) + "/" + m.group(2)) if m.group(1) else inputs.get(m.group(3) or "")
                if not key or key in out:
                    continue
                args = m.group(4)
                z0 = re.search(r"zstart\s*:\s*(" + _NUM + r")", args)
                h = re.search(r"height\s*:\s*(" + _NUM + r")", args)
                if not (z0 and h) or float(h.group(1)) <= 0:
                    continue
                nm = re.search(r'name\s*:\s*"([^"]*)"', args)
                col = re.search(r"color\s*:\s*0x([0-9a-fA-F]{6})", args)
                out[key] = {"name": nm.group(1) if nm else (m.group(3) or key),
                            "z0": float(z0.group(1)), "z1": float(z0.group(1)) + float(h.group(1)),
                            "color": "#" + (col.group(1).lower() if col else "8ea6c4"), "metal": True}
            if out:
                return out, str(f)
        except Exception:
            continue
    return {}, None


def _generic_stack(layers: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Even spacing by stacking order when no process file is known."""
    out: Dict[int, Dict[str, Any]] = {}
    for L in layers:
        unnamed = re.match(r"^\d+/\d+$", L["name"] or "") is not None   # no .lyp: KLayout's own names
        if L["purpose"].lower() not in ("drawing", "") or (L["group"] not in ("metal", "local", "device") and not unnamed):
            continue
        z = L["z"]
        out[L["id"]] = {"name": L["label"], "z0": z * 0.32, "z1": z * 0.32 + 0.24, "color": L["fill"], "metal": True}
    return out


_MESH_CANCEL: set = set()


class MeshCancelled(RuntimeError):
    """The 3D build was stopped by the user."""


def mesh(sid: str, box: Optional[List[float]], visible: Optional[List[int]]) -> Optional[bytes]:
    """Binary 3D mesh of a session's layout: ``u32 header length | JSON header |
    float32 data`` (triangles as x,y triples, edges as x1,y1,x2,y2, in µm
    relative to ``origin``). Built in the background worker; ``cancel_mesh``
    stops it."""
    _MESH_CANCEL.discard(sid)
    try:
        return _with(sid, lambda s: _mesh_of(s, box, visible), role="bg", op="mesh",
                     kw={"box": box, "visible": visible}, timeout=900, key=sid)
    except Exception as ex:
        if type(ex).__name__ == "Cancelled":
            raise MeshCancelled("the 3D build was cancelled") from None
        raise


def cancel_mesh(sid: str) -> bool:
    """Stop the 3D build of *sid* if one is running. True when one was."""
    if _WORKERS is not None:
        w = _WORKERS.get("bg")
        return bool(w and w.cancel("mesh", key=sid))
    _MESH_CANCEL.add(sid)
    return True


def _mesh_of(s: "_Session", box: Optional[List[float]], visible: Optional[List[int]]) -> bytes:
    import struct
    from array import array
    import klayout.db as kdb

    with _LOCK:
        if s.cfg is not None:
            cfg = s.cfg
        else:
            run_dir = s.path.parent
            while run_dir.parent != run_dir and not (run_dir / "resolved.json").is_file():
                run_dir = run_dir.parent
            cfg = _resolved(run_dir)
        stack, techfile = _gds3d_stack(cfg.get("PDK"))
        kind = "gds3d"
        if not stack:
            stack, techfile = _d25_stack(cfg)
            kind = "d25"
        generic = _generic_stack(s._layers) if not stack else {}
        vis = set(visible) if visible is not None else None
        dbu = s.dbu
        bb = s.top.dbbox()
        clip = None
        if box:
            cb = kdb.DBox(box[0], box[1], box[2], box[3]) & bb
            if not cb.empty() and (cb.width() < bb.width() * 0.98 or cb.height() < bb.height() * 0.98):
                clip = kdb.Region(cb.to_itype(dbu))
        extent = clip.bbox().to_dtype(dbu) if clip is not None else bb
        ox, oy = extent.left, extent.bottom
        layers_out: List[Dict[str, Any]] = []
        dropped: List[str] = []
        tri = array("f")
        edg = array("f")
        prims = 0
        # Top of the stack first: when a whole big chip has more geometry than
        # a browser can hold, the wiring is kept and the dense transistor-level
        # layers below it are left out (and named in the reply), instead of
        # drawing nothing. Nothing at all fitting is still "too_big".
        todo = []
        for L in s._layers:
            if vis is not None and L["id"] not in vis:
                continue
            spec = stack.get(L["src"]) if stack else generic.get(L["id"])
            if spec:
                todo.append((L, spec))
        todo.sort(key=lambda t: (-t[1]["z0"], -t[1]["z1"]))
        for L, spec in todo:
            if s.sid in _MESH_CANCEL:
                _MESH_CANCEL.discard(s.sid)
                raise MeshCancelled("the 3D build was cancelled")
            if dropped:
                dropped.append(spec["name"])
                continue
            r = kdb.Region(s.top.begin_shapes_rec(L["li"]))
            if clip is not None:
                r = r & clip
            r.merge()
            if r.is_empty():
                continue
            # A cheap size check (in KLayout's C++) before the slow Python
            # triangulation: every edge becomes a wall of two triangles.
            if prims + 2 * r.edges().count() > _MAX_3D_PRIMS:
                dropped.append(spec["name"])
                continue
            t0 = len(tri)
            e0 = len(edg)
            for p in r.decompose_trapezoids_to_region().each():
                pts = [(pt.x * dbu - ox, pt.y * dbu - oy) for pt in p.each_point_hull()]
                for i in range(1, len(pts) - 1):
                    tri.extend((pts[0][0], pts[0][1], pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))
            for e in r.edges().each():
                edg.extend((e.p1.x * dbu - ox, e.p1.y * dbu - oy, e.p2.x * dbu - ox, e.p2.y * dbu - oy))
            nt = (len(tri) - t0) // 6
            ne = (len(edg) - e0) // 4
            if prims + nt + 2 * ne > _MAX_3D_PRIMS:
                del tri[t0:]
                del edg[e0:]
                dropped.append(spec["name"])
                continue
            prims += nt + 2 * ne
            layers_out.append({"id": L["id"], "label": L["label"], "name": spec["name"], "z0": spec["z0"],
                               "z1": spec["z1"], "color": spec["color"], "metal": spec.get("metal", True),
                               "tris": nt, "edges": ne, "triOff": t0, "edgeOff": e0})
        if dropped and not layers_out:
            raise ValueError("too_big")
        head = {"origin": [ox, oy], "extent": [extent.left, extent.bottom, extent.right, extent.top],
                "clipped": clip is not None, "layers": layers_out, "triFloats": len(tri), "edgeFloats": len(edg),
                "dropped": dropped,
                "stack": (kind + ":" + Path(techfile).name) if techfile else "generic",
                "top": s.top.name}
    hb = json.dumps(head).encode("utf-8")
    hb += b" " * ((4 - (len(hb) % 4)) % 4)
    return struct.pack("<I", len(hb)) + hb + tri.tobytes() + edg.tobytes()


# --------------------------------------------------------------------------- workers
def enable_workers(on: bool = True) -> None:
    """Send KLayout work to worker processes (the server calls this once).
    Off on Windows hosts (LanEx's server runs in WSL there) and when
    ``LANEX_LAYOUT_WORKERS=0``."""
    global _WORKERS
    if not on or os.name == "nt" or os.environ.get("LANEX_LAYOUT_WORKERS") == "0":
        stop_workers()
        return
    if _WORKERS is None:
        from .layoutworker import Worker
        # view: what the user waits on; bg: the 3D model and 4K/8K saves;
        # thumb: stage thumbnails (throwaway sessions, nothing kept).
        _WORKERS = {"view": Worker("view"), "bg": Worker("bg"), "thumb": Worker("thumb")}


def stop_workers() -> None:
    global _WORKERS
    ws, _WORKERS = _WORKERS, None
    for w in (ws or {}).values():
        try:
            w.stop()
        except Exception:
            pass


def workers_on() -> bool:
    return _WORKERS is not None


def _remember(spec: Dict[str, Any]) -> None:
    _SPECS[spec["sid"]] = spec
    _SPECS.move_to_end(spec["sid"])
    while len(_SPECS) > _MAX_SPECS:
        _SPECS.popitem(last=False)


def _call(role: str, op: str, kw: Dict[str, Any], timeout: float = 120.0, key: Any = None,
          spawn: bool = True) -> Any:
    ws = _WORKERS
    if ws is None:
        return _serve(op, kw)
    w = ws[role]
    if not spawn and w._proc is None:
        return None
    return w.call(op, kw, timeout=timeout, key=key)


def _worker_words(ex: BaseException, name: str) -> str:
    """What to tell the user when a worker call failed."""
    kind = type(ex).__name__
    if kind == "TimeoutError":
        return "KLayout took too long to read " + name + " and was stopped. " + str(ex)
    if kind == "WorkerError":
        return "KLayout stopped while reading " + name + ". " + str(ex)
    return "KLayout could not read " + name + ": " + str(ex)


def _session_for(spec: Dict[str, Any]) -> "_Session":
    """The session a spec names, opened here if this process has not got it
    (a worker that was restarted, or the background worker's first use)."""
    with _LOCK:
        s = _SESSIONS.get(spec["sid"])
        if s is not None:
            try:
                if s.mtime == Path(spec["path"]).stat().st_mtime:
                    _SESSIONS.move_to_end(spec["sid"])
                    return s
            except OSError:
                pass
        info = _open_spec(spec)
        if not info.get("ok"):
            raise LookupError(info.get("error") or "the layout could not be opened")
        return _SESSIONS[spec["sid"]]


def _serve(op: str, kw: Dict[str, Any]) -> Any:
    """One call, in this process (the worker loop, or no workers at all)."""
    if op == "open":
        return _open_spec(kw["spec"])
    if op == "close":
        with _LOCK:
            return _SESSIONS.pop(kw["sid"], None) is not None
    if op == "thumb":
        return _thumb_png(kw["path"], kw["kind"], kw["run_dir"], kw["w"], kw["h"], kw["bg"])
    if op == "ping":
        return {"pid": os.getpid(), "sessions": list(_SESSIONS)}
    s = _session_for(kw["spec"])
    if op == "render":
        with _LOCK:
            return s.render(**kw["kw"])
    if op == "probe":
        with _LOCK:
            return s.probe(kw["x"], kw["y"], kw["r"])
    if op == "instances":
        with _LOCK:
            return s.instances(kw["cell"])
    if op == "nets":
        with _LOCK:
            return s.nets_search(kw["q"], kw.get("limit", 60))
    if op == "net":
        with _LOCK:
            return s.net_geom(kw["name"])
    if op == "locate":
        with _LOCK:
            return s.locate(kw["insts"], kw["nets"])
    if op == "mesh":
        return _mesh_of(s, kw.get("box"), kw.get("visible"))
    raise ValueError("unknown layout operation " + op)
