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
"""How much memory WSL gives Linux: the ``[wsl2] memory=`` line of ``.wslconfig``.

WSL gives its Linux VM half of the PC's memory unless ``%UserProfile%\\.wslconfig``
says otherwise. That file is the user's and is shared by every WSL distribution
(Docker Desktop's, their own Ubuntu), so LanEx edits it like a person would:

- one line: the first ``memory=`` in ``[wsl2]`` is replaced (its trailing comment
  kept), or added under the section header, or the section is added at the end;
- every other line, comment, section, the encoding (UTF-8 or UTF-16LE, with or
  without a BOM) and the line ending are kept byte for byte;
- a backup ``.wslconfig.lanex-backup-<stamp>`` is written first;
- values are whole gigabytes in uppercase (``12GB``): WSL rejects ``8gb``,
  ``7.5GB`` and ``8 GB`` and silently falls back to its default.

LanEx never runs ``wsl --shutdown``: it stops every distribution on the PC. The
new size applies the next time WSL starts.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MIN_GB = 2
SECTION = "wsl2"
_SECTION_RX = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(?:[#;].*)?$")
_MEMORY_RX = re.compile(r"^(\s*memory\s*=\s*)([^#;\r\n]*?)(\s*(?:[#;].*)?)$", re.I)
_VALUE_RX = re.compile(r"^\s*(\d+)\s*(B|K|KB|M|MB|G|GB|T|TB)?\s*$")


class WslConfigError(Exception):
    """The file could not be read or written safely; nothing was changed."""


# ---------------------------------------------------------------- encoding

def decode(raw: bytes) -> Tuple[str, str, bool]:
    """``(text, encoding, bom)``. Raises WslConfigError for anything unsure."""
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le"), "utf-16-le", True
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8", True
    if raw and len(raw) >= 2 and raw[1:2] == b"\x00" and raw[0:1] != b"\x00":
        try:
            return raw.decode("utf-16-le"), "utf-16-le", False
        except UnicodeDecodeError as ex:
            raise WslConfigError("unknown encoding") from ex
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError as ex:
        raise WslConfigError("unknown encoding") from ex


def encode(text: str, encoding: str, bom: bool) -> bytes:
    body = text.encode(encoding)
    if bom:
        body = (b"\xff\xfe" if encoding == "utf-16-le" else b"\xef\xbb\xbf") + body
    return body


# ---------------------------------------------------------------- values

def parse_size_gb(value: str) -> Optional[float]:
    """GB that WSL would read from *value*, or None when WSL would reject it."""
    m = _VALUE_RX.match(value or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "B").upper()
    mult = {"B": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20, "MB": 1 << 20,
            "G": 1 << 30, "GB": 1 << 30, "T": 1 << 40, "TB": 1 << 40}[unit]
    # Uppercase only, as WSL's own parser: a lowercase suffix is "Invalid memory string".
    if m.group(2) and m.group(2) != m.group(2).upper():
        return None
    return round(n * mult / (1 << 30), 2)


def format_gb(gb: int) -> str:
    return f"{int(gb)}GB"


# ---------------------------------------------------------------- the edit

def _lines(text: str) -> Tuple[List[str], str]:
    nl = "\r\n" if "\r\n" in text else "\n"
    parts = text.split(nl)
    return parts, nl


def find_memory(text: str) -> Dict[str, Any]:
    """``{value, line, section_line}`` for the first ``memory=`` in ``[wsl2]``."""
    lines, _nl = _lines(text)
    section = None
    sec_line = None
    for i, line in enumerate(lines):
        m = _SECTION_RX.match(line)
        if m:
            section = m.group(1).strip().lower()
            if section == SECTION and sec_line is None:
                sec_line = i
            continue
        if section == SECTION:
            mm = _MEMORY_RX.match(line)
            if mm:
                return {"value": mm.group(2).strip(), "line": i, "section_line": sec_line}
    return {"value": "", "line": None, "section_line": sec_line}


def set_memory_text(text: str, gb: Optional[int]) -> str:
    """*text* with ``[wsl2] memory`` set to *gb* (None removes the line). Pure."""
    lines, nl = _lines(text)
    found = find_memory(text)
    if gb is None:
        if found["line"] is None:
            return text
        del lines[found["line"]]
        return nl.join(lines)
    value = format_gb(gb)
    if found["line"] is not None:
        m = _MEMORY_RX.match(lines[found["line"]])
        lines[found["line"]] = m.group(1) + value + m.group(3)
        return nl.join(lines)
    if found["section_line"] is not None:
        lines.insert(found["section_line"] + 1, f"memory={value}")
        return nl.join(lines)
    # No [wsl2] section: add one at the end, after a blank line if the file has content.
    body = text
    tail = ""
    if body and not body.endswith(("\n", "\r\n")):
        tail = nl
    sep = nl if body.strip() else ""
    return body + tail + sep + f"[{SECTION}]" + nl + f"memory={value}" + nl


# ---------------------------------------------------------------- the file

def config_path() -> Optional[Path]:
    """``/mnt/c/Users/<you>/.wslconfig`` on WSL, None elsewhere."""
    try:
        from . import platform_env

        win = platform_env.wsl_windows_home()
        return Path(win) / ".wslconfig" if win else None
    except Exception:
        return None


def _state_path() -> Path:
    from . import platform_env

    return platform_env.home() / "wslconfig-lanex.json"


def _windows_total_gb() -> Optional[float]:
    """Physical memory of the PC (WSL clamps memory= to it)."""
    try:
        import subprocess

        out = subprocess.run(
            ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
            capture_output=True, text=True, timeout=12, cwd="/mnt/c")
        v = (out.stdout or "").strip().splitlines()
        return round(int(v[-1]) / (1 << 30), 1) if v and v[-1].strip().isdigit() else None
    except Exception:
        return None


def linux_total_gb() -> Optional[float]:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) * 1024 / (1 << 30), 1)
    except Exception:
        pass
    return None


def read_memory(path: Optional[Path] = None) -> Dict[str, Any]:
    """What ``.wslconfig`` says (read only): ``{path, exists, value, gb, valid}``."""
    p = path or config_path()
    out: Dict[str, Any] = {"path": str(p) if p else "", "exists": False, "value": "", "gb": None,
                           "valid": True, "readable": True}
    if not p:
        out["readable"] = False
        return out
    try:
        if not p.is_file():
            return out
        out["exists"] = True
        text, _enc, _bom = decode(p.read_bytes())
    except (OSError, WslConfigError):
        out["readable"] = False
        return out
    f = find_memory(text)
    out["value"] = f["value"]
    if f["value"]:
        out["gb"] = parse_size_gb(f["value"])
        out["valid"] = out["gb"] is not None
    return out


def status(*, total_gb: Optional[float] = None) -> Dict[str, Any]:
    """Everything the Tools Memory tile shows."""
    cur = read_memory()
    total = total_gb if total_gb is not None else _windows_total_gb()
    linux = linux_total_gb()
    avail = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                avail = round(int(line.split()[1]) * 1024 / (1 << 30), 1)
    except Exception:
        pass
    default_gb = round(total / 2, 1) if total else None
    hi = max(MIN_GB, int(total) - 4) if total else None
    lanex = {}
    try:
        lanex = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        lanex = {}
    applied = None
    if cur.get("gb") and linux:
        # WSL reports a little less than the configured size.
        applied = abs(linux - float(cur["gb"])) <= max(1.0, float(cur["gb"]) * 0.08)
    return {
        **cur, "windows_total_gb": total, "default_gb": default_gb, "linux_total_gb": linux,
        "linux_available_gb": avail, "min_gb": MIN_GB, "max_gb": hi, "applied": applied,
        "set_by_lanex": bool(lanex.get("value")) and lanex.get("value") == cur.get("value"),
        "backup": lanex.get("backup") or "",
    }


def write_memory(gb: Optional[int], *, path: Optional[Path] = None,
                 total_gb: Optional[float] = None) -> Dict[str, Any]:
    """Set (or with None, remove) ``[wsl2] memory``. Backup first; atomic write.

    Returns ``{ok, path, value, backup, changed}``; raises WslConfigError with
    words for the user when nothing could be changed safely."""
    p = path or config_path()
    if not p:
        raise WslConfigError("LanEx could not find your Windows user folder, so it didn't change .wslconfig.")
    if gb is not None:
        if not isinstance(gb, int) or gb < MIN_GB:
            raise WslConfigError(f"Choose at least {MIN_GB} GB.")
        if total_gb and gb > int(total_gb):
            raise WslConfigError(f"This PC has {total_gb:g} GB of memory; choose {int(total_gb)} GB or less.")
    raw = b""
    exists = p.is_file()
    if exists:
        try:
            raw = p.read_bytes()
            text, enc, bom = decode(raw)
        except WslConfigError:
            raise WslConfigError("LanEx couldn't read your .wslconfig safely, so it didn't change it. "
                                 f"To set it yourself, put memory={format_gb(gb or 8)} under [wsl2].")
        except OSError as ex:
            raise WslConfigError(f"LanEx couldn't read your .wslconfig ({ex.strerror or ex}).")
    else:
        text, enc, bom = "", "utf-8", False
    new = set_memory_text(text, gb)
    if new == text:
        return {"ok": True, "path": str(p), "value": format_gb(gb) if gb else "", "backup": "", "changed": False}
    backup = ""
    if exists:
        backup_path = p.with_name(".wslconfig.lanex-backup-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            backup_path.write_bytes(raw)
            backup = str(backup_path)
        except OSError as ex:
            raise WslConfigError(f"LanEx couldn't save a backup of .wslconfig ({ex.strerror or ex}), "
                                 "so it didn't change it.")
    tmp = p.with_name(".wslconfig.lanex-tmp")
    try:
        tmp.write_bytes(encode(new, enc, bom))
        tmp.replace(p)
    except OSError as ex:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise WslConfigError(f"LanEx couldn't write .wslconfig ({ex.strerror or ex}). Nothing was changed.")
    value = format_gb(gb) if gb else ""
    try:
        from . import platform_env

        platform_env.atomic_write_text(_state_path(), json.dumps(
            {"value": value, "backup": backup, "at": time.time()}) + "\n")
    except Exception:
        pass
    return {"ok": True, "path": str(p), "value": value, "backup": backup, "changed": True}
