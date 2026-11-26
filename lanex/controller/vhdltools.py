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
"""VHDL checking and simulation with what the LibreLane image carries.

The 3.0.4 image has GHDL 5.1.1 (and the Yosys GHDL plugin LibreLane's
``VHDLClassic`` flow uses) in its Nix store, off ``PATH``; with the store's
gcc wrapper, binutils and zlib on the path it analyses, elaborates and runs
VHDL-2008. That is a detail of one image, so it is probed once per image and
the IDE marks VHDL support "experimental": another image may not have it,
and then the IDE says so instead of failing in a strange way.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .quoting import file_arg
from .simulate import GHDL_PRELUDE, _shquote

_CAPS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
PROBE_TIMEOUT = 60

_GHDL_DIAG_RX = re.compile(r"^(?P<file>[^:\s][^:]*\.vhdl?):(?P<line>\d+):(?P<col>\d+):\s*(?P<sev>error|warning|note)?:?\s*(?P<msg>.*)$", re.I)


def parse_ghdl(text: str) -> List[Dict[str, Any]]:
    """``file.vhd:12:5:error: msg`` (GHDL's analysis messages) as diagnostics."""
    out: List[Dict[str, Any]] = []
    for raw in (text or "").splitlines():
        m = _GHDL_DIAG_RX.match(raw.strip())
        if not m:
            continue
        sev = (m.group("sev") or "error").lower()
        if sev == "note":
            continue
        out.append({"file": m.group("file"), "line": int(m.group("line")), "col": int(m.group("col")),
                    "severity": "warning" if sev == "warning" else "error", "code": "GHDL", "msg": m.group("msg").strip()})
    return out


def caps(engine: Optional[str], image: Optional[str], *, wait: float = 0.0) -> Dict[str, Any]:
    """``{state: "checking"|"ready"|"missing"|"no-engine", ghdl: "5.1.1"|None,
    yosys_ghdl: bool, note}``. The first call starts the probe in a thread."""
    if not engine or not image:
        return {"state": "no-engine", "ghdl": None, "yosys_ghdl": False,
                "note": "VHDL checking and simulation use GHDL from the LibreLane image, which needs Docker or Podman."}
    key = engine + "|" + image
    with _LOCK:
        c = _CAPS.get(key)
        if c is None or (c["state"] == "error" and time.time() - c.get("at", 0) > 300):
            c = {"state": "checking", "ghdl": None, "yosys_ghdl": False, "note": "Checking the image for GHDL…"}
            _CAPS[key] = c
            th = threading.Thread(target=_probe, args=(key, engine, image), daemon=True, name="lanex.vhdlprobe")
            th.start()
            c["_thread"] = th
    th = c.get("_thread")
    if wait and th is not None and th.is_alive():
        th.join(wait)
    return {k: v for k, v in _CAPS[key].items() if not k.startswith("_")}


def _probe(key: str, engine: str, image: str) -> None:
    shell = (GHDL_PRELUDE + "ghdl --version | head -1; "
             "(yosys -m ghdl -p 'help ghdl' >/dev/null 2>&1 && echo LANEX_YOSYS_GHDL) || true")
    res: Dict[str, Any] = {"state": "missing", "ghdl": None, "yosys_ghdl": False, "at": time.time()}
    try:
        cp = subprocess.run([engine, "run", "--rm", "--network", "none", image, "bash", "-lc", shell],
                            capture_output=True, text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
        out = (cp.stdout or "") + (cp.stderr or "")
        m = re.search(r"GHDL\s+(\d+\.\d+(?:\.\d+)?)", out)
        res["yosys_ghdl"] = "LANEX_YOSYS_GHDL" in out
        if m and cp.returncode == 0:
            res.update(state="ready", ghdl=m.group(1),
                       note="Checks and simulates VHDL with GHDL " + m.group(1) + " from the LibreLane image.")
        else:
            res["note"] = ("This LibreLane image has no GHDL simulator LanEx can run, so VHDL files can be edited "
                           "but not checked or simulated here. The flow still reads them (VHDL_FILES).")
    except subprocess.TimeoutExpired:
        res.update(state="error", note="Checking the image for GHDL took too long. Try again from the Simulation tab.")
    except Exception as ex:
        res.update(state="error", note="Couldn't check the image for GHDL: " + str(ex))
    with _LOCK:
        _CAPS[key] = res


def lint_shell(files: Sequence[str]) -> str:
    """GHDL's analysis of *files* (a full semantic check), with its work
    library in the container's /tmp so nothing is written into the project."""
    ana = " ".join(_shquote(p) for p in ["ghdl", "-a", "--std=08", "--workdir=/tmp/lanex-ghdl"] + [file_arg(f) for f in files])
    return GHDL_PRELUDE + "mkdir -p /tmp/lanex-ghdl; " + ana


def container_argv(engine: str, image: str, design_dir: str | Path, shell: str,
                   user_args: Sequence[str] = ()) -> List[str]:
    abs_design = str(Path(design_dir).resolve())
    return [engine, "run", "--rm", *user_args, "-e", "HOME=/tmp", "--network", "none", "-v", f"{abs_design}:/work",
            "-w", "/work", image, "bash", "-lc", shell]
