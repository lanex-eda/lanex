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
"""Project-wide search and replace for the RTL IDE.

Search reads the texts the index already holds (HDL) plus the project's
other editable files (constraints, config, notes), never ``runs/``,
``sim_build/``, ``obj_dir/``, ``.git`` or hidden folders, never a file over
2 MB or one with a NUL byte. Plain and whole-word searches are escaped
patterns, so they are always fast; a regular expression the user typed runs
in a child process with a time limit, because Python's ``re`` can backtrack
for minutes on a pattern like ``(a+)+$`` and a thread cannot be stopped.

Replace never overwrites newer work: each file is re-read, its hash must be
the one the search saw, each ticked match must still be where it was, and
the write goes through :func:`editor.write_text` with that hash (atomic, line
endings and mode kept). The old texts come back so the IDE can offer Undo.
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import editor, hdlindex

MAX_SEARCH_BYTES = 2 * 1024 * 1024
MAX_MATCHES = 2000
LINE_CUT = 200
REGEX_TIMEOUT_S = 10.0


class SearchError(ValueError):
    """The search can't run (a bad pattern): the message is for the user."""


def _globs(v: Any) -> List[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = v.split(",")
    out = []
    for g in v:
        g = str(g).strip()
        if g:
            out.append(g)
    return out


def _glob_match(rel: str, pats: Sequence[str]) -> bool:
    """VS Code style: ``src/**``, ``*.sv``, ``verify`` (a folder), ``**/tb_*``.
    A pattern without a slash matches the file name or any folder name."""
    for p in pats:
        p = p.replace("\\", "/").lstrip("./") if p not in (".", "./") else "*"
        if "/" not in p.rstrip("/"):
            name = rel.rsplit("/", 1)[-1]
            if fnmatch.fnmatchcase(name, p) or any(fnmatch.fnmatchcase(part, p.rstrip("/")) for part in rel.split("/")[:-1]):
                return True
            continue
        q = p.rstrip("/")
        if q.endswith("/**"):
            base = q[:-3]
            if rel.startswith(base + "/") or fnmatch.fnmatchcase(rel, q[:-3] + "/*"):
                return True
        if fnmatch.fnmatchcase(rel, q) or fnmatch.fnmatchcase(rel, q + "/*") or rel.startswith(q + "/"):
            return True
        if q.startswith("**/") and (fnmatch.fnmatchcase(rel, q[3:]) or fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], q[3:])):
            return True
    return False


def compile_query(query: str, *, regex: bool = False, case: bool = False, word: bool = False) -> "re.Pattern[str]":
    if not query:
        raise SearchError("Type something to search for.")
    pat = query if regex else re.escape(query)
    if word:
        pat = r"(?<![\w$])(?:" + pat + r")(?![\w$])"
    try:
        return re.compile(pat, 0 if case else re.IGNORECASE)
    except re.error as ex:
        raise SearchError("That isn't a valid regular expression: " + str(ex) + ".") from None


def _texts(design_dir: Path, dirty: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], List[Dict[str, str]], int]:
    """Every searchable file's text (rel -> text, newlines as LF)."""
    from . import projectfiles
    idx = hdlindex.get(design_dir, wait=3.0)
    files = idx.snapshot()
    out: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = list(idx.skipped)
    listing = projectfiles.list_files(design_dir, max_files=hdlindex.MAX_FILES)
    n = 0
    for f in listing.get("files") or []:
        rel = f["rel"]
        n += 1
        if rel in files:
            if files[rel].size <= MAX_SEARCH_BYTES:
                out[rel] = files[rel].text
            else:
                skipped.append({"rel": rel, "why": "over 2 MB"})
            continue
        if str(f.get("ext", "")).lower() in hdlindex.HDL_EXTS:
            continue         # the index already said why
        size = f.get("size") or 0
        if size > MAX_SEARCH_BYTES:
            skipped.append({"rel": rel, "why": "over 2 MB"})
            continue
        try:
            raw = (design_dir / rel).read_bytes()
        except OSError as ex:
            skipped.append({"rel": rel, "why": str(ex)})
            continue
        if b"\0" in raw[:8192]:
            skipped.append({"rel": rel, "why": "binary file"})
            continue
        out[rel] = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    for rel, text in (dirty or {}).items():
        if isinstance(text, str) and rel in out:
            out[rel] = text.replace("\r\n", "\n")
    return out, skipped, n


def _cut(line: str, a: int, b: int) -> Tuple[str, int]:
    """The line cut to about LINE_CUT characters around the match: (text, shift)."""
    if len(line) <= LINE_CUT:
        return line, 0
    start = max(0, a - 60)
    end = min(len(line), max(b + 60, start + LINE_CUT))
    pre = "…" if start > 0 else ""
    post = "…" if end < len(line) else ""
    return pre + line[start:end] + post, start - len(pre)


def _scan(rx: "re.Pattern[str]", texts: Dict[str, str], limit: int, replace: Optional[str]) -> Dict[str, Any]:
    groups: List[Dict[str, Any]] = []
    total = 0
    capped = False
    for rel in sorted(texts):
        text = texts[rel]
        matches: List[Dict[str, Any]] = []
        line_no = 1
        line_start = 0
        last = 0
        for m in rx.finditer(text):
            a, b = m.start(), m.end()
            if a == b:
                continue      # an empty match (a*) finds nothing to show
            line_no += text.count("\n", last, a)
            line_start = text.rfind("\n", 0, a) + 1
            last = a
            line_end = text.find("\n", a)
            if line_end < 0:
                line_end = len(text)
            line = text[line_start:line_end]
            col = a - line_start
            shown, shift = _cut(line, col, col + (b - a))
            hit = {"line": line_no, "col": col + 1, "len": min(b, line_end) - a, "text": shown,
                   "at": col - shift, "off": a, "end": b}
            if replace is not None:
                try:
                    hit["new"] = m.expand(replace)
                except (re.error, IndexError) as ex:
                    raise SearchError("The replacement refers to a group the pattern doesn't have: " + str(ex) + ".")
            matches.append(hit)
            total += 1
            if total >= limit:
                capped = True
                break
        if matches:
            groups.append({"rel": rel, "hash": editor.text_hash(text), "matches": matches})
        if capped:
            break
    return {"groups": groups, "total": total, "capped": capped}


# The search a user regex runs in a child process: no LanEx import, so the
# child starts in ~40 ms and the parent can kill it.
_CHILD = r"""
import json, re, sys
req = json.loads(sys.stdin.read())
rx = re.compile(req["pat"], req["flags"])
out = {}
for rel, text in req["texts"].items():
    spans = []
    for m in rx.finditer(text):
        if m.start() == m.end():
            continue
        spans.append([m.start(), m.end(), m.expand(req["rep"]) if req["rep"] is not None else None])
        if len(spans) >= req["limit"]:
            break
    if spans:
        out[rel] = spans
sys.stdout.write(json.dumps(out))
"""


def _scan_child(rx: "re.Pattern[str]", texts: Dict[str, str], limit: int, replace: Optional[str],
                timeout: float = REGEX_TIMEOUT_S) -> Dict[str, Any]:
    req = json.dumps({"pat": rx.pattern, "flags": rx.flags & ~re.UNICODE, "texts": texts, "limit": limit,
                      "rep": replace})
    try:
        cp = subprocess.run([sys.executable, "-I", "-c", _CHILD], input=req, capture_output=True, text=True,
                            timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SearchError("The search took too long and was stopped. Simplify the pattern "
                          "(for example avoid nested repeats like (a+)+).") from None
    if cp.returncode != 0:
        msg = (cp.stderr or "").strip().splitlines()[-1:] or ["the search stopped"]
        raise SearchError("The search couldn't run: " + msg[0])
    spans = json.loads(cp.stdout or "{}")
    groups = []
    total = 0
    capped = False
    for rel in sorted(spans):
        text = texts[rel]
        matches = []
        for a, b, new in spans[rel]:
            line_start = text.rfind("\n", 0, a) + 1
            line_end = text.find("\n", a)
            if line_end < 0:
                line_end = len(text)
            col = a - line_start
            shown, shift = _cut(text[line_start:line_end], col, col + (b - a))
            hit = {"line": text.count("\n", 0, a) + 1, "col": col + 1, "len": min(b, line_end) - a,
                   "text": shown, "at": col - shift, "off": a, "end": b}
            if new is not None:
                hit["new"] = new
            matches.append(hit)
            total += 1
            if total >= limit:
                capped = True
                break
        groups.append({"rel": rel, "hash": editor.text_hash(text), "matches": matches})
        if capped:
            break
    return {"groups": groups, "total": total, "capped": capped}


def to_python_replacement(rep: str, regex: bool) -> str:
    """VS Code's ``$1`` / ``$&`` in a replacement as Python's ``\\g<1>``;
    a plain (non-regex) replacement is literal text."""
    if not regex:
        return rep.replace("\\", "\\\\")
    out = []
    i = 0
    while i < len(rep):
        c = rep[i]
        if c == "$" and i + 1 < len(rep):
            nxt = rep[i + 1]
            if nxt.isdigit():
                j = i + 1
                while j < len(rep) and rep[j].isdigit():
                    j += 1
                out.append("\\g<" + rep[i + 1:j] + ">")
                i = j
                continue
            if nxt == "&":
                out.append("\\g<0>")
                i += 2
                continue
            if nxt == "$":
                out.append("$")
                i += 2
                continue
        if c == "\\" and i + 1 < len(rep) and rep[i + 1] in "nt\\":
            out.append({"n": "\n", "t": "\t", "\\": "\\\\"}[rep[i + 1]])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def search(design_dir: str | Path, query: str, *, regex: bool = False, case: bool = False, word: bool = False,
           include: Any = None, exclude: Any = None, replace: Optional[str] = None,
           dirty: Optional[Dict[str, str]] = None, limit: int = MAX_MATCHES) -> Dict[str, Any]:
    t0 = time.perf_counter()
    d = Path(design_dir).resolve()
    rx = compile_query(query, regex=regex, case=case, word=word)
    texts, skipped, listed = _texts(d, dirty)
    inc, exc = _globs(include), _globs(exclude)
    if inc:
        texts = {r: t for r, t in texts.items() if _glob_match(r, inc)}
    if exc:
        texts = {r: t for r, t in texts.items() if not _glob_match(r, exc)}
    rep = to_python_replacement(replace, regex) if replace is not None else None
    res = (_scan_child if regex else _scan)(rx, texts, limit, rep)
    res.update({"ok": True, "searched": len(texts), "skipped": skipped, "files": len(res["groups"]),
                "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return res


def replace(design_dir: str | Path, query: str, replacement: str, edits: Sequence[Dict[str, Any]], *,
            regex: bool = False, case: bool = False, word: bool = False) -> Dict[str, Any]:
    """Apply the ticked matches. *edits*: ``[{rel, hash, offs: [match offset, ...]}]``
    (the ``off`` of each ticked match the search returned). Per file:
    ``{rel, ok, count, before, hash}`` or ``{rel, ok: False, why}``."""
    d = Path(design_dir).resolve()
    rx = compile_query(query, regex=regex, case=case, word=word)
    rep = to_python_replacement(replacement, regex)
    results: List[Dict[str, Any]] = []
    for e in edits:
        rel = str(e.get("rel") or "")
        want = {int(x) for x in (e.get("offs") or []) if isinstance(x, (int, float))}
        target = editor._resolve_inside(d, rel)
        if target is None or not target.is_file():
            results.append({"rel": rel, "ok": False, "why": "isn't in the project any more"})
            continue
        try:
            raw = target.read_bytes()
        except OSError as ex:
            results.append({"rel": rel, "ok": False, "why": str(ex)})
            continue
        # Only text that is exactly UTF-8 is rewritten: a Latin-1 "©" or "µ"
        # would come back as U+FFFD everywhere in the file, and Undo (which
        # writes the decoded text back) couldn't restore the bytes.
        try:
            disk = raw.decode("utf-8")
        except UnicodeDecodeError as ex:
            results.append({"rel": rel, "ok": False, "encoding": True,
                            "why": f"isn't UTF-8 text (byte 0x{raw[ex.start]:02X} at offset {ex.start}), so Replace "
                                   "left it alone; edit it in an editor that knows its encoding, or save it "
                                   "as UTF-8 first"})
            continue
        text = disk.replace("\r\n", "\n").replace("\r", "\n")
        if editor.text_hash(text) != e.get("hash"):
            results.append({"rel": rel, "ok": False, "why": "changed on disk since the search", "changed": True})
            continue
        # Where each ticked match is now and what it becomes. A user regex
        # runs in the child again (its time limit holds for replace too).
        if regex:
            got = _scan_child(rx, {rel: text}, 100000, rep)["groups"]
            spans = {m["off"]: (m["off"], m["end"], m.get("new", "")) for g in got for m in g["matches"] if m["off"] in want}
        else:
            spans = {m.start(): (m.start(), m.end(), m.expand(rep)) for m in rx.finditer(text) if m.start() in want}
        if want - set(spans):
            results.append({"rel": rel, "ok": False, "why": "the text at a ticked match is different now", "changed": True})
            continue
        new = []
        last = 0
        for off in sorted(spans):
            a, b, repl = spans[off]
            new.append(text[last:a])
            new.append(repl)
            last = b
        new.append(text[last:])
        out_text = "".join(new)
        w = editor.write_text(d, rel, out_text, base_hash=e.get("hash"))
        if not w.get("ok"):
            results.append({"rel": rel, "ok": False, "why": w.get("error") or "couldn't be written",
                            "changed": bool(w.get("conflict"))})
            continue
        hdlindex.note_saved(d, rel, out_text)
        results.append({"rel": rel, "ok": True, "count": len(spans), "before": text, "hash": w.get("hash")})
    return {"ok": True, "results": results, "count": sum(r.get("count", 0) for r in results if r.get("ok")),
            "files": sum(1 for r in results if r.get("ok"))}
