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
"""Putting a value into a generated Tcl script (Magic, OpenROAD, Netgen, a
PDN file LibreLane sources) as exactly one word, whatever it holds.

Inside Tcl double quotes ``[...]`` runs a command and ``$x`` reads a
variable; inside braces a ``}`` in the value ends the word early. So a value
is never put in double quotes raw: ``tcl_word`` braces it only when it holds
no brace, backslash or control character, and otherwise escapes every
character that means something to Tcl (a control character, such as a
newline that would end the command, becomes ``\\uXXXX``). ``tcl_escape``
always escapes, for text that sits inside double quotes. Both read back as
the same string, also inside a braced script body that Tcl evaluates later
(``catch {...}``, ``if {...}``)."""
from __future__ import annotations

import re
from typing import Any

_TCL_PLAIN = re.compile(r"[A-Za-z0-9_./:+=,@%-]")


def _esc(ch: str) -> str:
    o = ord(ch)
    if _TCL_PLAIN.fullmatch(ch) or o >= 0xA0:
        return ch
    if o < 0x20 or 0x7F <= o < 0xA0:
        return "\\u%04x" % o
    return "\\" + ch


def file_arg(path: Any) -> str:
    """A file name as a command argument that can't be read as an option: a
    project file called ``-o`` or ``-f`` would otherwise make iverilog,
    Verilator or GHDL write or read somewhere else."""
    s = str(path)
    return "./" + s if s.startswith("-") else s


def tcl_escape(value: Any) -> str:
    """*value* backslash-escaped: literal as a bare word, and also inside
    "double quotes" (a message around ``$err``) or a list in quotes."""
    return "".join(_esc(c) for c in ("" if value is None else str(value)))


_BRACE_UNSAFE = re.compile(r"[{}\\\x00-\x1f\x7f-\x9f]")


def tcl_word(value: Any) -> str:
    """*value* as one literal Tcl word: in braces when it holds no brace,
    backslash or control character (``{/pdk/sky130A/x.lef}``, as a person
    would write it), else backslash-escaped. Not for use inside double
    quotes: that is ``tcl_escape``."""
    s = "" if value is None else str(value)
    if not _BRACE_UNSAFE.search(s):
        return "{" + s + "}"
    return tcl_escape(s)
