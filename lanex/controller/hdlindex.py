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
"""The RTL IDE's project index: go to definition, find references, symbols,
the module hierarchy and project search all answer from it.

Why a tokenizer and not a tool's parse: a container start costs 0.6 to 0.8 s
before Verilator or Yosys does anything, and an elaborating tool only sees the
active ``generate`` branch and ``ifdef`` arm (Yosys finds none of picorv32's
three sub-instances at its default parameters). Navigation has to reach all of
the text, in well under 200 ms, so the index reads the text itself:

* every file is tokenized once (identifiers, system tasks, directives,
  numbers, strings and comments skipped) and re-read only when its size or
  modification time changes;
* a light pass over the tokens finds the design units (``module``,
  ``interface``, ``package`` ...; VHDL ``entity``, ``architecture``,
  ``package``), their ports, parameters, nets and variables, functions and
  tasks, named blocks, ``typedef``/``enum`` names, ```define`` macros and
  instances (``type #(...) name (...)`` with their ``.port(...)``
  connections);
* queries never touch the disk: unsaved editor tabs come with the request and
  are indexed into a throw-away overlay, so answers match the screen.

What it does not do, said where the user meets it: code a macro generates is
not seen (the code is read as written, not as expanded), and both arms of an
``ifdef`` are indexed. Pure standard library.
"""
from __future__ import annotations

import bisect
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HDL_EXTS = (".v", ".sv", ".vh", ".svh", ".verilog", ".vhd", ".vhdl")
VHDL_EXTS = (".vhd", ".vhdl")
# A file bigger than this is indexed for its definitions but not searched
# (project search keeps texts up to this size in memory).
MAX_FILE_BYTES = 4 * 1024 * 1024
# The whole project's texts held in memory, at most.
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000

# ------------------------------------------------------------------ tokens

# One pass: comments and strings are matched (and skipped) so nothing inside
# them reads as a name. Escaped identifiers (\foo ) are names too.
_TOK_V = re.compile(
    r"//[^\n]*"                       # line comment
    r"|/\*.*?(?:\*/|\Z)"              # block comment (an unclosed one runs to the end)
    r"|\"(?:\\.|[^\"\\\n])*\"?"       # string
    r"|(?P<dir>`[A-Za-z_][\w$]*)"     # `define, `include, `MACRO
    r"|(?P<sys>\$[A-Za-z_][\w$]*)"    # $display
    r"|(?P<id>[A-Za-z_][\w$]*|\\\S+)" # identifier
    r"|(?P<num>\d[\d_]*(?:\.\d+)?|'[sS]?[bBoOdDhH][\w?_]*)"
    r"|(?P<op><=|>=|==|!=|::|\.\*|[()\[\]{};,.:=#@<>?+\-*/&|^~!%])",
    re.S,
)
_TOK_VHDL = re.compile(
    r"--[^\n]*"
    r"|/\*.*?(?:\*/|\Z)"
    r"|\"(?:\"\"|[^\"\n])*\"?"
    r"|'.'(?=[^A-Za-z_]|$)"            # character literal '0'
    r"|(?P<id>[A-Za-z][\w]*|\\[^\\\n]*\\)"
    r"|(?P<num>\d[\d_]*(?:\.\d+)?(?:#[\w.]+#)?)"
    r"|(?P<op><=|:=|=>|>=|/=|[()\[\];,.:=<>'&|+\-*/])",
    re.S,
)

V_KEYWORDS = frozenset("""
accept_on alias always always_comb always_ff always_latch and assert assign assume automatic before begin
bind bins binsof bit break buf bufif0 bufif1 byte case casex casez cell chandle checker class clocking cmos
config const constraint context continue cover covergroup coverpoint cross deassign default defparam design
disable dist do edge else end endcase endchecker endclass endclocking endconfig endfunction endgenerate
endgroup endinterface endmodule endpackage endprimitive endprogram endproperty endspecify endsequence
endtable endtask enum event eventually expect export extends extern final first_match for force foreach
forever fork forkjoin function generate genvar global highz0 highz1 if iff ifnone ignore_bins illegal_bins
implements implies import incdir include initial inout input inside instance int integer interconnect
interface intersect join join_any join_none large let liblist library local localparam logic longint
macromodule matches medium modport module nand negedge nettype new nexttime nmos nor noshowcancelled not
notif0 notif1 null or output package packed parameter pmos posedge primitive priority program property
protected pull0 pull1 pulldown pullup pulsestyle_ondetect pulsestyle_onevent pure rand randc randcase
randsequence rcmos real realtime ref reg reject_on release repeat restrict return rnmos rpmos rtran
rtranif0 rtranif1 s_always s_eventually s_nexttime s_until s_until_with scalared sequence shortint
shortreal showcancelled signed small soft solve specify specparam static string strong strong0 strong1
struct super supply0 supply1 sync_accept_on sync_reject_on table tagged task this throughout time
timeprecision timeunit tran tranif0 tranif1 tri tri0 tri1 triand trior trireg type typedef union unique
unique0 unsigned until until_with untyped use uwire var vectored virtual void wait wait_order wand weak
weak0 weak1 while wildcard wire with within wor xnor xor
""".split())

VHDL_KEYWORDS = frozenset("""
abs access after alias all and architecture array assert assume attribute begin block body buffer bus case
component configuration constant context cover default disconnect downto else elsif end entity exit fairness
file for force function generate generic group guarded if impure in inertial inout is label library linkage
literal loop map mod nand new next nor not null of on open or others out package parameter port postponed
procedure process property protected pure range record register reject release rem report restrict return
rol ror select sequence severity shared signal sla sll sra srl strong subtype then to transport type
unaffected units until use variable vmode vprop vunit wait when while with xnor xor
""".split())

# Words that start a declaration of names in a module body.
_V_DECL = frozenset(("input", "output", "inout", "ref", "wire", "reg", "logic", "tri", "tri0", "tri1",
                     "triand", "trior", "trireg", "wand", "wor", "uwire", "supply0", "supply1", "bit",
                     "byte", "shortint", "int", "longint", "integer", "time", "real", "realtime",
                     "shortreal", "string", "genvar", "parameter", "localparam", "specparam", "var",
                     "event", "chandle"))
_V_DIR = {"input": "input", "output": "output", "inout": "inout", "ref": "ref"}
_V_UNITS = {"module": "endmodule", "macromodule": "endmodule", "interface": "endinterface",
            "program": "endprogram", "package": "endpackage", "class": "endclass",
            "checker": "endchecker", "primitive": "endprimitive"}
_V_UNIT_END = frozenset(_V_UNITS.values())
# Where a statement can start: an instance or an assignment target follows these.
_STMT_START = frozenset((";", "begin", "end", "else", "generate", "endgenerate", ")", ":", "fork", "join",
                         "join_any", "join_none", "do", "default", "initial", "final", "always", "always_ff",
                         "always_comb", "always_latch", "assign", "endcase", "endfunction", "endtask"))
SYSTEM_TASKS_NOTE = "is a built-in system task or function."


class Tok(tuple):
    """(kind, text, offset). kind: "id", "dir", "sys", "num", "op"."""
    __slots__ = ()
    kind = property(lambda s: s[0])
    text = property(lambda s: s[1])
    off = property(lambda s: s[2])


def tokenize(text: str, vhdl: bool = False) -> List[Tuple[str, str, int]]:
    """The significant tokens of *text*: ``[(kind, text, offset)]``."""
    rx = _TOK_VHDL if vhdl else _TOK_V
    out: List[Tuple[str, str, int]] = []
    append = out.append
    for m in rx.finditer(text):
        kind = m.lastgroup
        if kind is None:
            continue          # a comment or a string
        append((kind, m.group(kind), m.start()))
    return out


def line_starts(text: str) -> List[int]:
    starts = [0]
    find = text.find
    i = find("\n")
    while i >= 0:
        starts.append(i + 1)
        i = find("\n", i + 1)
    return starts


# ------------------------------------------------------------------ one file

class Sym(dict):
    """A declared name: {name, kind, rel, off, end, unit, scope, detail, dir}.

    kind: module interface package program class entity architecture
    port param localparam net var genvar function task block typedef enum
    macro instance signal constant generic component."""


class FileIndex:
    """Everything one file defines and every name it uses."""

    __slots__ = ("rel", "text", "vhdl", "toks", "starts", "units", "syms", "macros", "insts",
                 "conns", "size", "mtime", "lower")

    def __init__(self, rel: str, text: str, *, size: int = 0, mtime: float = 0.0) -> None:
        self.rel = rel
        self.text = text
        self.vhdl = rel.lower().endswith(VHDL_EXTS)
        self.toks = tokenize(text, self.vhdl)
        self.starts = line_starts(text)
        self.units: List[Dict[str, Any]] = []    # design units with token ranges
        self.syms: List[Dict[str, Any]] = []     # every declaration (units included)
        self.macros: List[Dict[str, Any]] = []
        self.insts: List[Dict[str, Any]] = []    # {type, name, off, unit, ti, conns: [(port, off)], params: [...]}
        self.conns: Dict[int, Tuple[int, str]] = {}  # token index of a ".port" name -> (inst index, "port"|"param")
        self.size = size or len(text)
        self.mtime = mtime
        self.lower = self.vhdl
        if self.vhdl:
            _parse_vhdl(self)
        else:
            _parse_verilog(self)

    # positions
    def line_col(self, off: int) -> Tuple[int, int]:
        i = bisect.bisect_right(self.starts, off) - 1
        return i + 1, off - self.starts[i] + 1

    def offset(self, line: int, col: int) -> int:
        line = max(1, min(len(self.starts), int(line or 1)))
        start = self.starts[line - 1]
        end = self.starts[line] - 1 if line < len(self.starts) else len(self.text)
        return min(end, start + max(0, int(col or 1) - 1))

    def line_text(self, line: int) -> str:
        line = max(1, min(len(self.starts), line))
        s = self.starts[line - 1]
        e = self.starts[line] - 1 if line < len(self.starts) else len(self.text)
        return self.text[s:e].rstrip("\r")

    def tok_at(self, off: int) -> int:
        """Index of the identifier/directive token covering *off* (or ending
        right at it, the cursor just after a word), else -1."""
        toks = self.toks
        lo, hi = 0, len(toks)
        while lo < hi:
            mid = (lo + hi) // 2
            if toks[mid][2] <= off:
                lo = mid + 1
            else:
                hi = mid
        for i in (lo - 1, lo):
            if 0 <= i < len(toks):
                k, t, o = toks[i]
                if k in ("id", "dir", "sys") and o <= off <= o + len(t):
                    return i
        return -1

    def unit_at(self, off: int) -> Optional[Dict[str, Any]]:
        best = None
        for u in self.units:
            if u["off"] <= off <= u["end"]:
                if best is None or u["off"] >= best["off"]:
                    best = u
        return best

    def key(self, name: str) -> str:
        return name.lower() if self.lower else name


def _sym(fi: FileIndex, name: str, kind: str, off: int, **kw: Any) -> Dict[str, Any]:
    s = {"name": name, "kind": kind, "rel": fi.rel, "off": off}
    s.update(kw)
    fi.syms.append(s)
    return s


def _skip_balanced(toks: Sequence[Tuple[str, str, int]], i: int, open_: str, close: str) -> int:
    """From toks[i] == open_, the index just after its matching close."""
    depth = 0
    n = len(toks)
    while i < n:
        t = toks[i][1]
        if t == open_:
            depth += 1
        elif t == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _parse_verilog(fi: FileIndex) -> None:
    toks = fi.toks
    n = len(toks)
    unit: Optional[Dict[str, Any]] = None
    scope_stack: List[Dict[str, Any]] = []   # function/task scopes inside a unit
    i = 0
    prev = ";"
    while i < n:
        kind, t, off = toks[i]
        if kind == "dir":
            if t == "`define" and i + 1 < n and toks[i + 1][0] == "id":
                nm = toks[i + 1]
                # The macro's text runs to the end of its line (a \ continues it).
                ln, _ = fi.line_col(nm[2])
                fi.macros.append(_sym(fi, nm[1], "macro", nm[2], unit=None,
                                      detail=fi.line_text(ln).strip()[:160]))
                # Skip the rest of the definition's tokens on that line.
                end_line = ln
                while fi.line_text(end_line).rstrip().endswith("\\") and end_line < len(fi.starts):
                    end_line += 1
                stop = fi.starts[end_line] if end_line < len(fi.starts) else len(fi.text)
                i += 2
                while i < n and toks[i][2] < stop:
                    i += 1
                prev = ";"
                continue
            i += 1
            continue
        if kind != "id":
            if kind == "op":
                prev = t
            i += 1
            continue
        # ---- a design unit opens
        if t in _V_UNITS and (unit is None or t == "class") and prev not in (".", "::"):
            j = i + 1
            while j < n and toks[j][1] in ("automatic", "static", "virtual", "interface"):
                j += 1
            if j < n and toks[j][0] == "id" and toks[j][1] not in V_KEYWORDS:
                nm = toks[j]
                if unit is None:
                    unit = {"name": nm[1], "kind": "module" if t == "macromodule" else t, "off": nm[2],
                            "start": off, "end": len(fi.text), "ti": j, "end_word": _V_UNITS[t],
                            "params": [], "ports": []}
                    fi.units.append(unit)
                    s = _sym(fi, nm[1], unit["kind"], nm[2], unit=None, detail="")
                    unit["sym"] = s
                    i = _parse_header(fi, j + 1, unit)
                    prev = ";"
                    continue
                _sym(fi, nm[1], "class", nm[2], unit=unit["name"])
                i = j + 1
                continue
        if unit is not None and t == unit["end_word"]:
            unit["end"] = off + len(t)
            unit["sym"]["end"] = unit["end"]
            unit = None
            scope_stack.clear()
            i += 1
            prev = ";"
            continue
        if unit is None:
            # $unit scope: typedefs, functions, `import`
            if t == "typedef":
                i = _parse_typedef(fi, i, None)
                prev = ";"
                continue
            i += 1
            prev = t
            continue
        uname = unit["name"]
        # ---- inside a unit
        if t in ("function", "task") and prev not in ("extern", "import", "export", "pure"):
            j = i + 1
            # skip "automatic", a return type and its range up to the name before ( or ;
            last_id = None
            while j < n and toks[j][1] not in ("(", ";"):
                if toks[j][1] == "[":
                    j = _skip_balanced(toks, j, "[", "]")
                    continue
                if toks[j][0] == "id":
                    last_id = j
                j += 1
            if last_id is not None:
                nm = toks[last_id]
                end_word = "end" + t
                # find the end of the function/task
                k = j
                while k < n and toks[k][1] != end_word:
                    k += 1
                scope = {"off": off, "end": toks[k][2] if k < n else len(fi.text), "name": nm[1]}
                _sym(fi, nm[1], t, nm[2], unit=uname, scope=None,
                     detail=_decl_line(fi, nm[2]))
                scope_stack.append(scope)
                # the argument list declares names local to the scope
                if j < n and toks[j][1] == "(":
                    end = _skip_balanced(toks, j, "(", ")")
                    _parse_decl_list(fi, j + 1, end - 1, uname, scope, port_mode=True)
                    i = end
                else:
                    i = j
                prev = ";"
                continue
        if t in ("endfunction", "endtask"):
            if scope_stack:
                scope_stack.pop()
            i += 1
            prev = ";"
            continue
        scope = scope_stack[-1] if scope_stack else None
        if t == "typedef":
            i = _parse_typedef(fi, i, uname)
            prev = ";"
            continue
        if t in ("begin", "fork") and i + 2 < n and toks[i + 1][1] == ":" and toks[i + 2][0] == "id":
            nm = toks[i + 2]
            _sym(fi, nm[1], "block", nm[2], unit=uname, scope=scope, detail=_decl_line(fi, nm[2]))
            i += 3
            prev = ";"
            continue
        if t in _V_DECL and prev in _STMT_START | {"(", ",", "#"}:
            stop = i
            while stop < n and toks[stop][1] != ";":
                stop += 1
            _parse_decl_list(fi, i, stop, uname, scope, port_mode=False, unit=unit)
            i = stop + 1
            prev = ";"
            continue
        # an instance: Type [#(...)] name [range] ( ... )
        if t not in V_KEYWORDS and prev in _STMT_START and i + 1 < n:
            j = i + 1
            params_rng = None
            if toks[j][1] == "#":
                if j + 1 < n and toks[j + 1][1] == "(":
                    params_rng = (j + 1, _skip_balanced(toks, j + 1, "(", ")"))
                    j = params_rng[1]
                else:
                    j += 2   # #8 (a numeric delay is not an instance)
                    params_rng = None
            first = True
            matched = False
            while j < n and toks[j][0] == "id" and toks[j][1] not in V_KEYWORDS:
                nm = toks[j]
                k = j + 1
                if k < n and toks[k][1] == "[":
                    k = _skip_balanced(toks, k, "[", "]")
                if k < n and toks[k][1] == "(":
                    end = _skip_balanced(toks, k, "(", ")")
                    inst = {"type": t, "type_off": off, "name": nm[1], "off": nm[2], "unit": uname,
                            "conns": [], "params": [], "end": toks[end - 1][2] if end - 1 < n else len(fi.text)}
                    idx = len(fi.insts)
                    fi.insts.append(inst)
                    _sym(fi, nm[1], "instance", nm[2], unit=uname, scope=scope, detail=t,
                         type=t)
                    _named_conns(fi, k, end, idx, "port", inst["conns"])
                    if params_rng and first:
                        _named_conns(fi, params_rng[0], params_rng[1], idx, "param", inst["params"])
                    elif params_rng:
                        inst["params"] = fi.insts[idx - 1]["params"]
                    first = False
                    matched = True
                    j = end
                    if j < n and toks[j][1] == ",":
                        j += 1
                        continue
                break
            if matched:
                i = j
                prev = ";"
                continue
        prev = t
        i += 1


def _named_conns(fi: FileIndex, a: int, b: int, inst_idx: int, kind: str, out: List[Any]) -> None:
    """``.port(expr)`` inside toks[a:b]: record each port name's token."""
    toks = fi.toks
    depth = 0
    for k in range(a, b):
        t = toks[k][1]
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            depth -= 1
        elif t == "." and depth == 1 and k + 1 < b and toks[k + 1][0] == "id":
            fi.conns[k + 1] = (inst_idx, kind)
            out.append((toks[k + 1][1], toks[k + 1][2]))


def _decl_line(fi: FileIndex, off: int) -> str:
    ln, _ = fi.line_col(off)
    return fi.line_text(ln).strip()[:200]


def _parse_header(fi: FileIndex, i: int, unit: Dict[str, Any]) -> int:
    """After ``module name``: imports, ``#(parameters)``, ``(ports)`` up to
    the ``;``. Returns the index after the ``;``."""
    toks = fi.toks
    n = len(toks)
    while i < n and toks[i][1] != ";":
        t = toks[i][1]
        if t == "#" and i + 1 < n and toks[i + 1][1] == "(":
            end = _skip_balanced(toks, i + 1, "(", ")")
            _parse_decl_list(fi, i + 2, end - 1, unit["name"], None, port_mode=True, unit=unit,
                             default_kind="param")
            i = end
            continue
        if t == "(":
            end = _skip_balanced(toks, i, "(", ")")
            _parse_decl_list(fi, i + 1, end - 1, unit["name"], None, port_mode=True, unit=unit,
                             default_kind="port")
            i = end
            continue
        i += 1
    return i + 1


def _parse_typedef(fi: FileIndex, i: int, uname: Optional[str]) -> int:
    toks = fi.toks
    n = len(toks)
    j = i + 1
    while j < n and toks[j][1] != ";":
        if toks[j][1] == "{":
            # enum members are names too
            if toks[i + 1][1] == "enum" or any(toks[x][1] == "enum" for x in range(i + 1, j)):
                end = _skip_balanced(toks, j, "{", "}")
                expect = True
                depth = 0
                for k in range(j + 1, end - 1):
                    tk = toks[k]
                    if tk[1] in ("(", "[", "{"):
                        depth += 1
                    elif tk[1] in (")", "]", "}"):
                        depth -= 1
                    elif tk[1] == "," and depth == 0:
                        expect = True
                    elif expect and tk[0] == "id" and depth == 0:
                        _sym(fi, tk[1], "enum", tk[2], unit=uname, detail=_decl_line(fi, tk[2]))
                        expect = False
                j = end
                continue
            j = _skip_balanced(toks, j, "{", "}")
            continue
        j += 1
    # the typedef's name is the last identifier before the ;
    k = j - 1
    while k > i and toks[k][1] == "]":
        k = _back_balanced(toks, k, "[", "]") - 1
    if k > i and toks[k][0] == "id":
        _sym(fi, toks[k][1], "typedef", toks[k][2], unit=uname, detail=_decl_line(fi, toks[k][2]))
    return j + 1


def _back_balanced(toks, k: int, open_: str, close: str) -> int:
    depth = 0
    while k >= 0:
        t = toks[k][1]
        if t == close:
            depth += 1
        elif t == open_:
            depth -= 1
            if depth == 0:
                return k
        k -= 1
    return 0


def _parse_decl_list(fi: FileIndex, a: int, b: int, uname: str, scope: Optional[Dict[str, Any]], *,
                     port_mode: bool, unit: Optional[Dict[str, Any]] = None,
                     default_kind: Optional[str] = None) -> None:
    """Names declared in toks[a:b] (a port list, a parameter list or one
    declaration statement). A name is an identifier followed by ``,``, ``;``,
    ``=``, ``[`` (an unpacked range) or the end, outside brackets, and not a
    type name (``wire logic [7:0] a`` declares only ``a``)."""
    toks = fi.toks
    kind = default_kind
    direction = None
    depth = 0
    k = a
    in_init = False
    group_start = a
    while k < b:
        tk = toks[k]
        t = tk[1]
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and t == ",":
            in_init = False
            group_start = k + 1
        elif depth == 0 and t == "=":
            in_init = True
        elif depth == 0 and t == ";":
            in_init = False
            kind = default_kind
            direction = None
            group_start = k + 1
        elif depth == 0 and not in_init and tk[0] == "id":
            if t in _V_DIR:
                direction = _V_DIR[t]
                kind = "port"
            elif t in ("parameter", "localparam", "specparam"):
                kind = "param" if t == "parameter" else "localparam"
                direction = None
            elif t == "genvar":
                kind = "genvar"
            elif t in ("wire", "tri", "uwire", "wand", "wor", "supply0", "supply1", "tri0", "tri1") and kind not in ("port", "param", "localparam"):
                kind = "net"
            elif t in V_KEYWORDS:
                if kind is None:
                    kind = "var"
            else:
                nxt = toks[k + 1][1] if k + 1 < b else ","
                if k + 1 >= b or nxt in (",", ";", "=", "[", ")"):
                    sk = kind or ("port" if port_mode and default_kind == "port" else "var")
                    s = _sym(fi, t, sk, tk[2], unit=uname, scope=scope,
                             detail=_decl_text(fi, group_start, k, b))
                    if sk == "port":
                        s["dir"] = direction or "port"
                        if unit is not None and scope is None:
                            unit["ports"].append(t)
                    elif sk in ("param", "localparam"):
                        if unit is not None and scope is None and sk == "param":
                            unit["params"].append(t)
                        s["default"] = _init_text(fi, k + 1, b)
                    elif port_mode and default_kind == "param" and unit is not None:
                        # #(W = 8) without the word parameter
                        s["kind"] = "param"
                        unit["params"].append(t)
                        s["default"] = _init_text(fi, k + 1, b)
        k += 1


def _decl_text(fi: FileIndex, a: int, k: int, b: int) -> str:
    """The declaration as written, from its first word to the name's end."""
    toks = fi.toks
    s = toks[a][2] if a < len(toks) else toks[k][2]
    e = toks[k][2] + len(toks[k][1])
    txt = fi.text[s:e]
    return " ".join(txt.split())[:200]


def _init_text(fi: FileIndex, k: int, b: int) -> Optional[str]:
    toks = fi.toks
    if k >= b or toks[k][1] != "=":
        return None
    depth = 0
    j = k + 1
    start = toks[j][2] if j < len(toks) else None
    while j < b:
        t = toks[j][1]
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and t in (",", ";"):
            break
        j += 1
    if start is None:
        return None
    end = toks[j - 1][2] + len(toks[j - 1][1]) if j - 1 >= k + 1 else start
    return " ".join(fi.text[start:end].split())[:80]


# ------------------------------------------------------------------ VHDL

def _parse_vhdl(fi: FileIndex) -> None:
    """Entities (ports, generics), architectures (signals, constants,
    component and instance names), packages, processes. VHDL names are not
    case sensitive, so matching is done lower-cased."""
    toks = fi.toks
    n = len(toks)
    unit: Optional[Dict[str, Any]] = None
    i = 0
    prev = ";"

    def low(j: int) -> str:
        return toks[j][1].lower() if j < n else ""

    while i < n:
        kind, t, off = toks[i]
        tl = t.lower()
        if kind != "id":
            prev = t
            i += 1
            continue
        if tl in ("entity", "package") and prev in (";", "") and i + 1 < n and toks[i + 1][0] == "id" \
                and low(i + 1) != "body" and low(i + 2) == "is":
            nm = toks[i + 1]
            unit = {"name": nm[1], "kind": tl, "off": nm[2], "start": off, "end": len(fi.text), "ti": i + 1,
                    "params": [], "ports": []}
            fi.units.append(unit)
            unit["sym"] = _sym(fi, nm[1], tl, nm[2], unit=None, detail="")
            i += 3
            prev = "is"
            continue
        if tl == "architecture" and i + 3 < n and low(i + 2) == "of":
            nm = toks[i + 1]
            ent = toks[i + 3][1]
            unit = {"name": ent, "kind": "architecture", "arch": nm[1], "off": nm[2], "start": off,
                    "end": len(fi.text), "ti": i + 1, "params": [], "ports": []}
            fi.units.append(unit)
            unit["sym"] = _sym(fi, nm[1], "architecture", nm[2], unit=ent, detail="architecture of " + ent)
            i += 4
            continue
        if tl in ("generic", "port") and low(i + 1) == "(" and unit is not None and unit["kind"] in ("entity", "component"):
            end = _skip_balanced(toks, i + 1, "(", ")")
            _vhdl_iface(fi, i + 2, end - 1, unit, "param" if tl == "generic" else "port")
            i = end
            continue
        if tl == "component" and i + 1 < n and toks[i + 1][0] == "id" and unit is not None:
            nm = toks[i + 1]
            _sym(fi, nm[1], "component", nm[2], unit=unit["name"], detail=_decl_line(fi, nm[2]))
            # skip to "end component"
            j = i + 2
            while j < n and not (low(j) == "end" and low(j + 1) == "component"):
                j += 1
            i = j + 2
            continue
        if tl in ("signal", "constant", "variable", "shared", "alias", "type", "subtype") and unit is not None:
            if tl == "shared":
                i += 1
                continue
            j = i + 1
            names = []
            while j < n and toks[j][1] not in (":", ";", "is"):
                if toks[j][0] == "id":
                    names.append(toks[j])
                j += 1
            sk = {"signal": "signal", "constant": "constant", "variable": "var", "alias": "var",
                  "type": "typedef", "subtype": "typedef"}[tl]
            for nm in names:
                _sym(fi, nm[1], sk, nm[2], unit=unit["name"], detail=_decl_line(fi, nm[2]))
            # enum literals: type t is (A, B, C);
            if tl == "type" and j < n and toks[j][1] == "is" and low(j + 1) == "(":
                end = _skip_balanced(toks, j + 1, "(", ")")
                for k in range(j + 2, end - 1):
                    if toks[k][0] == "id":
                        _sym(fi, toks[k][1], "enum", toks[k][2], unit=unit["name"], detail=_decl_line(fi, toks[k][2]))
            while j < n and toks[j][1] != ";":
                j += 1
            i = j + 1
            prev = ";"
            continue
        # label : entity work.foo / label : foo port map / label : process
        if i + 2 < n and toks[i + 1][1] == ":" and prev in (";", "begin", "is", "generate", "then", "else") and unit is not None:
            lbl = toks[i]
            j = i + 2
            what = low(j)
            if what in ("process", "block", "for", "if"):
                _sym(fi, lbl[1], "block", lbl[2], unit=unit["name"], detail=_decl_line(fi, lbl[2]))
                i = j
                prev = ":"
                continue
            if what in ("entity", "component", "configuration"):
                j += 1
            if j < n and toks[j][0] == "id":
                # work.foo
                typ = toks[j]
                if j + 2 < n and toks[j + 1][1] == ".":
                    typ = toks[j + 2]
                    j += 2
                k = j + 1
                if k < n and toks[k][1] == "(":   # architecture name
                    k = _skip_balanced(toks, k, "(", ")")
                if low(k) in ("port", "generic"):
                    inst = {"type": typ[1], "type_off": typ[2], "name": lbl[1], "off": lbl[2], "unit": unit["name"],
                            "conns": [], "params": [], "end": off}
                    idx = len(fi.insts)
                    fi.insts.append(inst)
                    _sym(fi, lbl[1], "instance", lbl[2], unit=unit["name"], detail=typ[1], type=typ[1])
                    # port map (a => b, ...): the formal names
                    m = k
                    while m < n and toks[m][1] != ";":
                        if toks[m][1] == "=>" and m - 1 >= 0 and toks[m - 1][0] == "id":
                            pk = "param" if _in_generic_map(toks, m) else "port"
                            fi.conns[m - 1] = (idx, pk)
                            (inst["params"] if pk == "param" else inst["conns"]).append((toks[m - 1][1], toks[m - 1][2]))
                        m += 1
                    inst["end"] = toks[m][2] if m < n else len(fi.text)
                    i = m + 1
                    prev = ";"
                    continue
        if tl == "end" and unit is not None:
            j = i + 1
            if low(j) in ("entity", "architecture", "package") or (j < n and toks[j][1].lower() == unit.get("arch", unit["name"]).lower()) \
                    or (j < n and toks[j][1] == ";" and unit["kind"] in ("entity", "package")):
                k = j
                while k < n and toks[k][1] != ";":
                    k += 1
                if low(j) in ("entity", "architecture", "package", ";") or toks[j][1].lower() in (unit["name"].lower(), unit.get("arch", "").lower()):
                    unit["end"] = toks[k][2] if k < n else len(fi.text)
                    unit["sym"]["end"] = unit["end"]
                    unit = None
                    i = k + 1
                    prev = ";"
                    continue
        if tl in ("function", "procedure") and i + 1 < n and toks[i + 1][0] == "id":
            nm = toks[i + 1]
            _sym(fi, nm[1], "function", nm[2], unit=unit["name"] if unit else None, detail=_decl_line(fi, nm[2]))
        prev = t
        i += 1


def _in_generic_map(toks, m: int) -> bool:
    k = m
    depth = 0
    while k >= 0:
        t = toks[k][1].lower()
        if t == ")":
            depth += 1
        elif t == "(":
            if depth == 0:
                return k >= 1 and toks[k - 1][1].lower() == "map" and k >= 2 and toks[k - 2][1].lower() == "generic"
            depth -= 1
        k -= 1
    return False


def _vhdl_iface(fi: FileIndex, a: int, b: int, unit: Dict[str, Any], kind: str) -> None:
    """``a, b : in std_logic; c : out ...`` inside a port or generic clause."""
    toks = fi.toks
    names: List[Tuple[str, str, int]] = []
    k = a
    depth = 0
    while k < b:
        tk = toks[k]
        if tk[1] == "(":
            depth += 1
        elif tk[1] == ")":
            depth -= 1
        elif depth == 0 and tk[1] == ":":
            direction = toks[k + 1][1].lower() if k + 1 < b else ""
            if direction not in ("in", "out", "inout", "buffer", "linkage"):
                direction = "in" if kind == "port" else ""
            for nm in names:
                s = _sym(fi, nm[1], kind, nm[2], unit=unit["name"], detail=_decl_line(fi, nm[2]))
                if kind == "port":
                    s["dir"] = {"in": "input", "out": "output"}.get(direction, direction)
                    unit["ports"].append(nm[1])
                else:
                    unit["params"].append(nm[1])
                    # := default
                    j = k + 1
                    while j < b and toks[j][1] not in (";", ":="):
                        j += 1
                    if j < b and toks[j][1] == ":=":
                        st = toks[j + 1][2] if j + 1 < b else None
                        e = j + 1
                        while e < b and toks[e][1] != ";":
                            e += 1
                        if st is not None:
                            end = toks[e - 1][2] + len(toks[e - 1][1])
                            s["default"] = " ".join(fi.text[st:end].split())[:80]
            names = []
            # skip to the ; that ends this group
            while k < b and toks[k][1] != ";":
                if toks[k][1] == "(":
                    k = _skip_balanced(toks, k, "(", ")")
                    continue
                k += 1
        elif depth == 0 and tk[0] == "id" and tk[1] != ",":
            names.append(tk)
        k += 1


# ------------------------------------------------------------------ the project

class ProjectIndex:
    """The index of one design folder. Thread-safe: a refresh replaces whole
    FileIndex objects, queries read a snapshot of the dict."""

    def __init__(self, design_dir: str | Path) -> None:
        self.root = Path(design_dir).resolve()
        self.files: Dict[str, FileIndex] = {}
        self.meta: Dict[str, Dict[str, Any]] = {}   # rel -> {role, built, abs, size}
        self.skipped: List[Dict[str, str]] = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.building = False
        self.built_at = 0.0
        self.build_ms = 0
        self.checked_at = 0.0
        self.truncated = False
        self.error: Optional[str] = None
        self.progress = (0, 0)
        self._defs_cache: Optional[Tuple[int, Dict[str, List[Dict[str, Any]]]]] = None
        self._dirty: Dict[Tuple[str, int, int], FileIndex] = {}
        self.version = 0

    # -------------------------------------------------- building
    def refresh(self, *, force: bool = False) -> None:
        """Bring the index up to date with the disk (new, changed and removed
        files). Reads only files whose size or mtime changed."""
        from . import projectfiles
        t0 = time.monotonic()
        self.building = True
        try:
            try:
                listing = projectfiles.list_files(self.root, max_files=MAX_FILES)
            except Exception as ex:  # an unreadable folder: say so, keep what we had
                self.error = str(ex)
                return
            self.error = None
            self.truncated = bool(listing.get("truncated"))
            want: Dict[str, Dict[str, Any]] = {}
            for f in listing.get("files") or []:
                if str(f.get("ext", "")).lower() in HDL_EXTS:
                    want[f["rel"]] = {"role": f.get("role"), "built": bool(f.get("in_verilog_files")),
                                      "abs": f.get("abs"), "size": f.get("size") or 0}
            total = 0
            new_files: Dict[str, FileIndex] = {}
            skipped: List[Dict[str, str]] = []
            items = list(want.items())
            self.progress = (0, len(items))
            for n_done, (rel, m) in enumerate(items, 1):
                p = self.root / rel
                try:
                    st = p.stat()
                except OSError:
                    continue
                old = self.files.get(rel)
                if old is not None and not force and old.size == st.st_size and old.mtime == st.st_mtime:
                    new_files[rel] = old
                    total += old.size
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    skipped.append({"rel": rel, "why": "over 4 MB"})
                    continue
                if total + st.st_size > MAX_TOTAL_BYTES:
                    skipped.append({"rel": rel, "why": "the project's HDL is over 64 MB"})
                    continue
                try:
                    raw = p.read_bytes()
                except OSError as ex:
                    skipped.append({"rel": rel, "why": str(ex)})
                    continue
                if b"\0" in raw[:8192]:
                    skipped.append({"rel": rel, "why": "binary file"})
                    continue
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                new_files[rel] = FileIndex(rel, text, size=st.st_size, mtime=st.st_mtime)
                total += st.st_size
                if n_done % 25 == 0:
                    self.progress = (n_done, len(items))
            with self.lock:
                changed = set(new_files) != set(self.files) or any(new_files[r] is not self.files.get(r) for r in new_files)
                self.files = new_files
                self.meta = want
                self.skipped = skipped
                if changed:
                    self.version += 1
                    self._defs_cache = None
            self.progress = (len(items), len(items))
            self.build_ms = int((time.monotonic() - t0) * 1000)
            self.built_at = time.time()
            self.checked_at = time.monotonic()
        finally:
            self.building = False
            self.ready.set()

    def touch(self, rel: str, text: Optional[str] = None) -> None:
        """A file was saved through LanEx: re-read it now (cheap, one file)."""
        rel = rel.replace("\\", "/")
        if not rel.lower().endswith(HDL_EXTS):
            return
        p = self.root / rel
        try:
            st = p.stat()
            if text is None:
                text = p.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            with self.lock:
                if self.files.pop(rel, None) is not None:
                    self.version += 1
                    self._defs_cache = None
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        fi = FileIndex(rel, text, size=st.st_size, mtime=st.st_mtime)
        with self.lock:
            self.files[rel] = fi
            if rel not in self.meta:
                from . import projectfiles
                ext = Path(rel).suffix.lower()
                self.meta[rel] = {"role": projectfiles._role(rel, ext), "built": False, "abs": str(p), "size": st.st_size}
            self.version += 1
            self._defs_cache = None

    # -------------------------------------------------- views
    def snapshot(self, dirty: Optional[Dict[str, str]] = None) -> Dict[str, FileIndex]:
        """The files to answer from: the index, with unsaved tabs swapped in."""
        with self.lock:
            files = dict(self.files)
        for rel, text in (dirty or {}).items():
            if not isinstance(text, str) or not str(rel).lower().endswith(HDL_EXTS):
                continue
            rel = str(rel).replace("\\", "/")
            if len(text) > MAX_FILE_BYTES:
                continue
            text = text.replace("\r\n", "\n")
            old = files.get(rel)
            if old is not None and old.text == text:
                continue
            # The same unsaved text comes with every hover and F12 until the
            # next keystroke: tokenize it once (a 20k-line file takes ~80 ms).
            key = (rel, len(text), hash(text))
            fi = self._dirty.get(key)
            if fi is None:
                fi = FileIndex(rel, text)
                self._dirty[key] = fi
                while len(self._dirty) > 8:
                    self._dirty.pop(next(iter(self._dirty)))
            files[rel] = fi
        return files

    def status(self) -> Dict[str, Any]:
        with self.lock:
            nfiles = len(self.files)
            nsyms = sum(len(f.syms) for f in self.files.values())
            lines = sum(len(f.starts) for f in self.files.values())
        done, total = self.progress
        return {"ready": self.ready.is_set(), "building": self.building, "files": nfiles, "symbols": nsyms,
                "lines": lines, "ms": self.build_ms, "skipped": list(self.skipped), "truncated": self.truncated,
                "error": self.error, "progress": {"done": done, "total": total}, "version": self.version}

    def is_built(self, rel: str) -> bool:
        m = self.meta.get(rel)
        return bool(m and m.get("built"))

    def role(self, rel: str) -> str:
        m = self.meta.get(rel)
        return (m or {}).get("role") or ""


# ------------------------------------------------------------------ queries

def _unit_defs(files: Dict[str, FileIndex]) -> Dict[str, List[Dict[str, Any]]]:
    """Design units by name (lower-cased for VHDL), every definition."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for fi in files.values():
        for u in fi.units:
            if u["kind"] == "architecture":
                continue
            out.setdefault(fi.key(u["name"]), []).append({**u, "rel": fi.rel})
            if fi.vhdl and u["name"] != fi.key(u["name"]):
                out.setdefault(u["name"], []).append({**u, "rel": fi.rel})
    return out


def _loc(files: Dict[str, FileIndex], rel: str, off: int, name: str) -> Dict[str, Any]:
    fi = files[rel]
    line, col = fi.line_col(off)
    return {"rel": rel, "line": line, "col": col, "len": len(name), "text": fi.line_text(line)[:400]}


def _sym_out(files: Dict[str, FileIndex], s: Dict[str, Any], idx: Optional[ProjectIndex]) -> Dict[str, Any]:
    o = _loc(files, s["rel"], s["off"], s["name"])
    o.update({"name": s["name"], "kind": s["kind"], "unit": s.get("unit"), "detail": s.get("detail") or "",
              "dir": s.get("dir"), "default": s.get("default")})
    if idx is not None:
        o["built"] = idx.is_built(s["rel"])
        o["role"] = idx.role(s["rel"])
    return o


def _unit_syms(files: Dict[str, FileIndex], unit_name: str, name: str, *, vhdl_ok: bool = True) -> List[Dict[str, Any]]:
    """Declarations named *name* inside design unit *unit_name* (any file:
    VHDL splits an entity and its architecture across files)."""
    out = []
    for fi in files.values():
        k = fi.key(name)
        uk = fi.key(unit_name)
        for s in fi.syms:
            su = s.get("unit")
            if su is None or fi.key(su) != uk:
                continue
            if fi.key(s["name"]) == k:
                out.append(s)
    return out


def _resolve(files: Dict[str, FileIndex], fi: FileIndex, ti: int) -> Dict[str, Any]:
    """What the token at *ti* names: {target: "module"|"decl"|"macro"|"system"|
    "unknown", name, syms:[...], unit}."""
    kind, name, off = fi.toks[ti]
    key = fi.key(name)
    if kind == "sys":
        return {"target": "system", "name": name, "syms": []}
    if kind == "dir":
        mname = name[1:]
        if mname in ("define", "include", "ifdef", "ifndef", "else", "elsif", "endif", "timescale", "undef",
                     "default_nettype", "resetall", "celldefine", "endcelldefine", "line", "pragma"):
            return {"target": "keyword", "name": name, "syms": []}
        syms = [s for f in files.values() for s in f.macros if s["name"] == mname]
        return {"target": "macro", "name": mname, "syms": syms}
    # the name in a `define line is the macro itself
    for s in fi.macros:
        if s["off"] == off:
            return {"target": "macro", "name": name, "syms": [m for f in files.values() for m in f.macros if m["name"] == name]}
    kw = VHDL_KEYWORDS if fi.vhdl else V_KEYWORDS
    if key in kw:
        return {"target": "keyword", "name": name, "syms": []}
    unit = fi.unit_at(off)
    defs = _unit_defs(files)
    # an instance's type
    for inst in fi.insts:
        if inst["type_off"] == off:
            return {"target": "module", "name": name, "syms": [u["sym"] for u in defs.get(key, [])],
                    "unit": unit and unit["name"]}
    # .port( ) / .PARAM( ) of an instance, or a VHDL formal (a => b)
    if ti in fi.conns:
        inst_idx, ck = fi.conns[ti]
        inst = fi.insts[inst_idx]
        child = defs.get(fi.key(inst["type"]))
        syms = []
        for u in child or []:
            for s in _unit_syms(files, u["name"], name):
                if s.get("scope") is None and s["kind"] in (("param",) if ck == "param" else ("port",)):
                    syms.append(s)
        return {"target": "decl", "name": name, "syms": syms, "unit": inst["type"], "via": inst["name"]}
    # a.b: a hierarchical name through an instance
    if ti >= 2 and fi.toks[ti - 1][1] == "." and fi.toks[ti - 2][0] == "id" and unit is not None and not fi.vhdl:
        chain = [fi.toks[ti - 2][1]]
        k = ti - 2
        while k >= 2 and fi.toks[k - 1][1] == "." and fi.toks[k - 2][0] == "id":
            chain.insert(0, fi.toks[k - 2][1])
            k -= 2
        mod = unit["name"]
        ok = True
        for inst_name in chain:
            nxt = None
            for f in files.values():
                for inst in f.insts:
                    if inst["unit"] == mod and inst["name"] == inst_name:
                        nxt = inst["type"]
                        break
                if nxt:
                    break
            if nxt is None:
                ok = False
                break
            mod = nxt
        if ok and mod != unit["name"]:
            syms = [s for s in _unit_syms(files, mod, name) if s.get("scope") is None]
            return {"target": "decl", "name": name, "syms": syms, "unit": mod, "via": ".".join(chain)}
    # pkg::name
    if ti >= 2 and fi.toks[ti - 1][1] == "::" and fi.toks[ti - 2][0] == "id":
        pkg = fi.toks[ti - 2][1]
        syms = _unit_syms(files, pkg, name)
        return {"target": "decl", "name": name, "syms": syms, "unit": pkg}
    # a declaration in the enclosing unit: the innermost scope that covers off
    if unit is not None:
        cands = _unit_syms(files, unit["name"], name)
        # VHDL: an architecture sees its entity's ports and generics
        if fi.vhdl and unit["kind"] == "architecture":
            pass
        scoped = [s for s in cands if s.get("scope") and s["rel"] == fi.rel and s["scope"]["off"] <= off <= s["scope"]["end"]]
        if scoped:
            return {"target": "decl", "name": name, "syms": scoped, "unit": unit["name"]}
        top = [s for s in cands if not s.get("scope")]
        if top:
            return {"target": "decl", "name": name, "syms": top, "unit": unit["name"]}
        # imported packages: any package's declaration of the name
        for f in files.values():
            for u in f.units:
                if u["kind"] == "package":
                    ps = _unit_syms(files, u["name"], name)
                    if ps:
                        return {"target": "decl", "name": name, "syms": ps, "unit": u["name"]}
    # a design unit by name (a module named in a comment-free context, a package)
    if key in defs:
        return {"target": "module", "name": name, "syms": [u["sym"] for u in defs[key]]}
    # $unit: typedefs and functions outside every unit
    glob = [s for f in files.values() for s in f.syms if s.get("unit") is None and f.key(s["name"]) == key
            and s["kind"] not in ("module", "interface", "package", "program", "entity")]
    if glob:
        return {"target": "decl", "name": name, "syms": glob}
    return {"target": "unknown", "name": name, "syms": [], "unit": unit and unit["name"]}


def _explain_unknown(name: str) -> str:
    if name.startswith("$"):
        return name + " " + SYSTEM_TASKS_NOTE
    if re.match(r"^(sky130|gf180mcu|sg13g2|ihp)\w*__", name) or re.match(r"^sky130_fd_\w+", name):
        return name + " is a standard cell from the PDK, not a project file."
    return "No definition of " + name + " in this project's files."


def definition(idx: ProjectIndex, rel: str, line: int, col: int, dirty: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Where the name at (rel, line, col) is declared."""
    t0 = time.perf_counter()
    files = idx.snapshot(dirty)
    fi = files.get(rel)
    if fi is None:
        return {"ok": False, "error": rel + " isn't in the project index (only Verilog, SystemVerilog and VHDL files are)."}
    ti = fi.tok_at(fi.offset(line, col))
    if ti < 0:
        return {"ok": True, "name": "", "results": [], "note": "Put the cursor on a name first."}
    r = _resolve(files, fi, ti)
    out = [_sym_out(files, s, idx) for s in r["syms"]]
    # built files first, then the file asked from, then by path
    out.sort(key=lambda o: (not o.get("built"), o["rel"] != rel, o["rel"], o["line"]))
    note = ""
    if not out:
        note = (r["name"] + " is a Verilog keyword.") if r["target"] == "keyword" else _explain_unknown(
            r["name"] if r["target"] != "system" else r["name"])
        if r["target"] == "macro":
            note = "No `define of " + r["name"] + " in this project's files. It may come from the config's VERILOG_DEFINES or a tool."
    here = fi.toks[ti][2]
    at_def = any(s["rel"] == rel and s["off"] == here for s in r["syms"])
    return {"ok": True, "name": r["name"], "target": r["target"], "unit": r.get("unit"), "via": r.get("via"),
            "results": out, "note": note, "at_definition": at_def,
            "ms": round((time.perf_counter() - t0) * 1000, 2)}


def _write_at(fi: FileIndex, ti: int) -> bool:
    """Is the name at token *ti* assigned here? (the target of =, <=, assign,
    a VHDL <= or :=). A heuristic, said so in the UI."""
    toks = fi.toks
    n = len(toks)
    j = ti + 1
    while j < n and toks[j][1] == "[":
        j = _skip_balanced(toks, j, "[", "]")
    if j >= n:
        return False
    nxt = toks[j][1]
    if fi.vhdl:
        return nxt in ("<=", ":=")
    if nxt not in ("=", "<="):
        return False
    k = ti - 1
    while k >= 0 and toks[k][1] in (",",):
        k -= 1
    prev = toks[k][1] if k >= 0 else ";"
    return prev in _STMT_START or prev in ("assign", "{", "begin", "#")


def references(idx: ProjectIndex, rel: str, line: int, col: int, dirty: Optional[Dict[str, str]] = None,
               limit: int = 5000) -> Dict[str, Any]:
    """Every place the name at (rel, line, col) is used, by the rules of
    :func:`_resolve`: a module's instances, a declaration's uses in its unit
    (and its ``.port`` connections and ``inst.name`` paths from outside), a
    macro's uses."""
    t0 = time.perf_counter()
    files = idx.snapshot(dirty)
    fi = files.get(rel)
    if fi is None:
        return {"ok": False, "error": rel + " isn't in the project index."}
    ti = fi.tok_at(fi.offset(line, col))
    if ti < 0:
        return {"ok": True, "name": "", "results": [], "note": "Put the cursor on a name first."}
    r = _resolve(files, fi, ti)
    name = r["name"]
    hits: List[Tuple[str, int, bool, str]] = []   # (rel, off, write, tag)
    decl_offs = {(s["rel"], s["off"]) for s in r["syms"]}
    tgt = r["target"]
    if tgt == "macro":
        for f in files.values():
            for k, t, o in f.toks:
                if k == "dir" and t[1:] == name:
                    hits.append((f.rel, o + 1, False, ""))
            for s in f.macros:
                if s["name"] == name:
                    hits.append((f.rel, s["off"], False, "defined"))
    elif tgt == "module" or (r["syms"] and all(s["kind"] in ("module", "interface", "package", "program", "entity", "component") for s in r["syms"])):
        for f in files.values():
            k = f.key(name)
            for inst in f.insts:
                if f.key(inst["type"]) == k:
                    hits.append((f.rel, inst["type_off"], False, "instance " + inst["name"]))
            for s in f.syms:
                if s["kind"] in ("module", "interface", "package", "program", "entity", "component") and f.key(s["name"]) == k:
                    hits.append((f.rel, s["off"], False, "defined"))
            # pkg::x and import pkg::*
            for j, (kk, t, o) in enumerate(f.toks):
                if kk == "id" and f.key(t) == k and j + 1 < len(f.toks) and f.toks[j + 1][1] == "::":
                    hits.append((f.rel, o, False, ""))
    elif tgt == "decl" and r["syms"]:
        unit_name = r.get("unit")
        scoped = [s for s in r["syms"] if s.get("scope")]
        for f in files.values():
            k = f.key(name)
            for u in f.units:
                if unit_name is None or f.key(u["name"]) != f.key(unit_name):
                    continue
                lo, hi = u["start"], u["end"]
                if scoped and f.rel == scoped[0]["rel"]:
                    lo, hi = scoped[0]["scope"]["off"], scoped[0]["scope"]["end"]
                for j, (kk, t, o) in enumerate(f.toks):
                    if o < lo:
                        continue
                    if o > hi:
                        break
                    if kk != "id" or f.key(t) != k:
                        continue
                    if j in f.conns:
                        continue            # .port( ) of a child: the child's name
                    if j >= 1 and f.toks[j - 1][1] in (".", "::") and not f.vhdl:
                        continue            # someone else's member
                    if not scoped and _in_scope_decl_shadow(f, o, name, unit_name):
                        continue
                    tag = "declared" if (f.rel, o) in decl_offs else ""
                    hits.append((f.rel, o, _write_at(f, j) and not tag, tag))
            if scoped:
                continue
            # from outside: .name( ) on instances of this unit, inst.name paths
            if unit_name is not None:
                uk = f.key(unit_name)
                for inst_i, inst in enumerate(f.insts):
                    if f.key(inst["type"]) != uk:
                        continue
                    for pn, po in inst["conns"] + inst["params"]:
                        if f.key(pn) == k:
                            hits.append((f.rel, po, False, "connection in " + inst["name"]))
                    # hierarchical inst.name in the instantiating unit
                    for j, (kk, t, o) in enumerate(f.toks):
                        if kk == "id" and f.key(t) == k and j >= 2 and f.toks[j - 1][1] == "." \
                                and f.toks[j - 2][1] == inst["name"] and j not in f.conns:
                            hits.append((f.rel, o, False, "through " + inst["name"]))
        # a testbench's dut.u.x two levels down is not followed (said in the UI)
    seen = set()
    out: List[Dict[str, Any]] = []
    for h in sorted(hits, key=lambda h: (h[0], h[1])):
        if (h[0], h[1]) in seen:
            continue
        seen.add((h[0], h[1]))
        o = _loc(files, h[0], h[1], name)
        o["write"] = h[2]
        o["tag"] = h[3]
        o["built"] = idx.is_built(h[0])
        out.append(o)
    capped = len(out) > limit
    nfiles = len({o["rel"] for o in out})
    note = ""
    if not out:
        note = _explain_unknown(name) if tgt in ("unknown", "system") else "No uses of " + name + " found."
    return {"ok": True, "name": name, "target": tgt, "unit": r.get("unit"), "results": out[:limit],
            "total": len(out), "files": nfiles, "capped": capped, "note": note,
            "definitions": [_sym_out(files, s, idx) for s in r["syms"]],
            "ms": round((time.perf_counter() - t0) * 1000, 2)}


def _in_scope_decl_shadow(f: FileIndex, off: int, name: str, unit_name: Optional[str]) -> bool:
    """A use inside a function/task that declares its own *name*: it is
    that local one, not the unit's."""
    for s in f.syms:
        sc = s.get("scope")
        if sc and s["name"] == name and s.get("unit") == unit_name and sc["off"] <= off <= sc["end"]:
            return True
    return False


SYMBOL_ORDER = {"module": 0, "interface": 0, "package": 0, "program": 0, "entity": 0, "architecture": 1,
                "class": 1, "port": 2, "param": 3, "localparam": 3, "generic": 3, "net": 4, "signal": 4,
                "var": 4, "constant": 4, "genvar": 4, "instance": 5, "function": 6, "task": 6, "block": 7,
                "typedef": 8, "enum": 8, "macro": 9, "component": 5}


def file_symbols(idx: ProjectIndex, rel: str, dirty: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Every declaration in one file, in source order (Ctrl+Shift+O)."""
    files = idx.snapshot(dirty)
    fi = files.get(rel)
    if fi is None:
        return {"ok": True, "symbols": []}
    out = [_sym_out(files, s, None) for s in sorted(fi.syms, key=lambda s: s["off"])]
    return {"ok": True, "symbols": out}


def project_symbols(idx: ProjectIndex, dirty: Optional[Dict[str, str]] = None, limit: int = 20000) -> Dict[str, Any]:
    """Project-wide symbols for the # picker: units, ports, parameters,
    functions, tasks, typedefs, macros, instances (not every local net)."""
    files = idx.snapshot(dirty)
    keep = {"module", "interface", "package", "program", "entity", "class", "port", "param", "function",
            "task", "typedef", "macro", "instance", "component", "architecture", "net", "signal", "var", "localparam",
            "constant", "enum", "block", "genvar"}
    out = []
    for fi in files.values():
        for s in fi.syms:
            if s["kind"] in keep and not s.get("scope"):
                line, col = fi.line_col(s["off"])
                out.append({"name": s["name"], "kind": s["kind"], "rel": fi.rel, "line": line, "col": col,
                            "unit": s.get("unit"), "built": idx.is_built(fi.rel)})
                if len(out) >= limit:
                    return {"ok": True, "symbols": out, "capped": True}
    return {"ok": True, "symbols": out, "capped": False}


def hierarchy(idx: ProjectIndex, top: Optional[str] = None, dirty: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Modules and what each instantiates (for the Hierarchy view and the
    waveform's scope map), from the index in one request."""
    files = idx.snapshot(dirty)
    defs = _unit_defs(files)
    mods: Dict[str, Dict[str, Any]] = {}
    for fi in files.values():
        for u in fi.units:
            if u["kind"] in ("module", "interface", "program", "entity"):
                line, _ = fi.line_col(u["off"])
                key = u["name"]
                if key in mods:
                    mods[key].setdefault("also", []).append(fi.rel)
                    continue
                mods[key] = {"name": u["name"], "kind": u["kind"], "rel": fi.rel, "line": line,
                             "built": idx.is_built(fi.rel), "role": idx.role(fi.rel),
                             "params": list(u["params"]), "ports": list(u["ports"]), "children": []}
    for fi in files.values():
        for inst in fi.insts:
            tk = inst["type"]
            parent = inst["unit"]
            if parent in mods and (tk in mods or fi.key(tk) in defs):
                line, col = fi.line_col(inst["off"])
                mods[parent]["children"].append({"inst": inst["name"], "type": tk, "rel": fi.rel, "line": line, "col": col})
    return {"ok": True, "modules": mods, "top": top}


def unit_info(idx: ProjectIndex, name: str) -> Optional[Dict[str, Any]]:
    """One design unit's parameters (with defaults) and ports, e.g. a
    testbench's parameters for the Run settings."""
    files = idx.snapshot()
    for fi in files.values():
        for u in fi.units:
            if u["kind"] != "architecture" and fi.key(u["name"]) == fi.key(name):
                params = []
                for s in fi.syms:
                    if s.get("unit") and fi.key(s["unit"]) == fi.key(u["name"]) and s["kind"] in ("param",) and not s.get("scope"):
                        params.append({"name": s["name"], "default": s.get("default"), "detail": s.get("detail")})
                ports = [s["name"] for s in fi.syms if s.get("unit") == u["name"] and s["kind"] == "port"]
                line, _ = fi.line_col(u["off"])
                return {"name": u["name"], "kind": u["kind"], "rel": fi.rel, "line": line, "params": params,
                        "ports": ports, "vhdl": fi.vhdl}
    return None


# ------------------------------------------------------------------ registry

_INDEXES: Dict[str, ProjectIndex] = {}
_REG_LOCK = threading.Lock()
# On a Windows-drive project a stat of 300 files takes ~0.3 s, so the disk is
# re-checked at most this often (saves through LanEx update the index at once).
RECHECK_S = 2.0
RECHECK_S_WINDRIVE = 10.0


def get(design_dir: str | Path, *, wait: float = 1.5) -> ProjectIndex:
    """The index for *design_dir*, building it in a thread the first time and
    re-checking the disk now and then (never on the request thread after the
    first build). Waits up to *wait* seconds for a first build."""
    key = str(Path(design_dir).resolve())
    with _REG_LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            if len(_INDEXES) > 8:            # projects opened long ago
                for k in list(_INDEXES)[:-4]:
                    _INDEXES.pop(k, None)
            idx = ProjectIndex(key)
            _INDEXES[key] = idx
            _spawn(idx)
        else:
            interval = RECHECK_S_WINDRIVE if key.startswith("/mnt/") else RECHECK_S
            if not idx.building and time.monotonic() - idx.checked_at > interval:
                idx.checked_at = time.monotonic()
                _spawn(idx)
    if wait and not idx.ready.is_set():
        idx.ready.wait(wait)
    return idx


def _spawn(idx: ProjectIndex, force: bool = False) -> None:
    idx.building = True
    th = threading.Thread(target=idx.refresh, kwargs={"force": force}, daemon=True, name="lanex.hdlindex")
    th.start()


def peek(design_dir: str | Path) -> Optional[ProjectIndex]:
    return _INDEXES.get(str(Path(design_dir).resolve()))


def note_saved(design_dir: str | Path, rel: str, text: Optional[str] = None) -> None:
    """Called after a save, rename or delete through LanEx."""
    idx = peek(design_dir)
    if idx is not None:
        try:
            idx.touch(rel, text)
        except Exception:
            pass


def forget_all() -> None:
    with _REG_LOCK:
        _INDEXES.clear()
