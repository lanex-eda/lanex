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
"""``lanex doctor``: is this computer ready to run LanEx, and what to do if not.

One engine for the terminal (``lanex doctor``), the app (``GET /api/doctor``),
the Tools page's attention list and every "Copy diagnostics". Twelve checks in
a fixed order, cheapest first, the network last because it is slow. Each check
has its own time limit and answers in plain words with a next step on every
problem. Nothing here changes the machine: every check only reads.

Statuses: ``ok`` works; ``warn`` works but is slower, limited or about to fail;
``fail`` stops runs or installs; ``info`` worth knowing, nothing to do; ``skip``
not checked (with the reason).

Exit codes: 0 no ``fail``; 1 at least one ``fail`` (or a ``warn`` with
``--strict``); 2 the doctor itself crashed. ``--json`` keeps the same codes,
because scripts and CI need the signal.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = 1
STATUSES = ("ok", "warn", "fail", "info", "skip")
# The order checks run and print in. Later checks may read what earlier ones found.
CHECK_ORDER = ("lanex", "files", "system", "memory", "disk", "engine", "image", "pdks",
               "tools", "server", "network", "updates")

# Download sites LanEx reaches (Setup, Tools installs, image pulls, PDKs, updates).
NETWORK_HOSTS = (
    ("snapshot.ubuntu.com", "https://snapshot.ubuntu.com/"),
    ("pypi.org", "https://pypi.org/simple/"),
    ("files.pythonhosted.org", "https://files.pythonhosted.org/"),
    ("ghcr.io", "https://ghcr.io/v2/"),
    ("github.com", "https://github.com/"),
    ("api.github.com", "https://api.github.com/"),
)
NETWORK_PROBE_S = 8.0
NETWORK_BUDGET_S = 12.0

DISK_FAIL_GB = 5.0
DISK_WARN_GB = 12.0
MEM_FAIL_GB = 2.5


# ---------------------------------------------------------------- results

def result(cid: str, title: str, status: str, summary: str, *, next_text: str = "",
           command: str = "", ui: str = "", details: Optional[List[str]] = None,
           data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One check's answer. ``next`` is present on every warn and fail."""
    if status not in STATUSES:
        status = "warn"
    out: Dict[str, Any] = {"id": cid, "title": title, "status": status,
                           "summary": summary, "details": list(details or []),
                           "data": dict(data or {}), "ms": 0}
    if next_text or command or ui:
        out["next"] = {"text": next_text, "command": command, "ui": ui}
    return out


TITLES = {
    "lanex": "LanEx", "files": "Your LanEx folder", "system": "Computer",
    "memory": "Memory", "disk": "Disk space", "engine": "Container engine",
    "image": "Tool image", "pdks": "PDKs", "tools": "Extra tools",
    "server": "LanEx server", "network": "Network", "updates": "Updates",
}


# ---------------------------------------------------------------- helpers

def _run(argv: List[str], timeout: float = 5.0, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"did not answer within {timeout:g} s"
    except FileNotFoundError:
        return 127, "", "not found"
    except Exception as ex:  # noqa: BLE001
        return 1, "", str(ex)


def _gb(n_bytes: Optional[float]) -> Optional[float]:
    return None if n_bytes is None else round(n_bytes / 1e9, 1)


def fmt_gb(gb: Optional[float]) -> str:
    if gb is None:
        return "?"
    return f"{gb:.1f} GB" if gb < 100 else f"{gb:.0f} GB"


def is_windows_appliance() -> bool:
    """The Windows app: WSL, and Windows Setup wrote runtime.json."""
    try:
        from . import platform_env

        return platform_env.is_wsl() and (platform_env.home() / "runtime.json").is_file()
    except Exception:
        return False


def _repair_next() -> str:
    if is_windows_appliance():
        return "Run Repair LanEx from the Start menu. It keeps your projects."
    return "Run the LanEx install command again; it keeps your projects."


def _meminfo() -> Dict[str, float]:
    out: Dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition(":")
            parts = v.split()
            if parts and parts[0].isdigit():
                out[k.strip()] = int(parts[0]) * 1024 / 1e9   # kB -> GB
    except Exception:
        pass
    return out


def _mac_mem() -> Dict[str, float]:
    rc, out, _ = _run(["sysctl", "-n", "hw.memsize"], 3)
    if rc == 0 and out.strip().isdigit():
        return {"MemTotal": int(out.strip()) / 1e9}
    return {}


# ---------------------------------------------------------------- Windows facts

def windows_state() -> Dict[str, Any]:
    """Setup's saved state (read through /mnt/c), or {}: where the distro's disk lives."""
    try:
        from . import platform_env

        win_home = platform_env.wsl_windows_home()
        if not win_home:
            return {}
        p = Path(win_home) / "AppData" / "Local" / "LanEx" / "installer-state.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8-sig") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def vhdx_drive() -> Optional[Tuple[str, Path]]:
    """``("C:", Path("/mnt/c"))``: the Windows drive that holds LanEx's Linux disk.

    Free space inside WSL (``df /``) is the virtual disk's size, about 1 TB, not
    what is left on the PC: the drive below it is what fills up."""
    base = ""
    try:
        base = str(((windows_state().get("appliance") or {}).get("basePath")) or "")
    except Exception:
        base = ""
    m = re.match(r"^(?:\\\\\?\\)?([A-Za-z]):", base)
    letter = (m.group(1) if m else "c").lower()
    mount = Path("/mnt") / letter
    if mount.is_dir():
        return f"{letter.upper()}:", mount
    return None


def disk_usage(path: Path) -> Optional[Dict[str, float]]:
    try:
        u = shutil.disk_usage(str(path))
        return {"free_gb": _gb(u.free), "total_gb": _gb(u.total)}
    except Exception:
        return None


def storage_target() -> Dict[str, Any]:
    """Which filesystem's free space matters, in words: ``{label, path, free_gb, total_gb}``."""
    from . import platform_env

    if platform_env.is_wsl():
        drv = vhdx_drive()
        if drv:
            use = disk_usage(drv[1])
            if use:
                return {"label": drv[0], "path": str(drv[1]), "windows": True, **use}
    home = platform_env.home()
    probe = home if home.exists() else Path.home()
    use = disk_usage(probe) or {"free_gb": None, "total_gb": None}
    return {"label": "the disk with " + _share_path(str(probe)), "path": str(probe),
            "windows": False, **use}


# ---------------------------------------------------------------- share form

def _share_path(text: str) -> str:
    """$HOME -> ~ and the Windows user's name -> <user>, for anything we print."""
    s = str(text or "")
    home = str(Path.home())
    if home and home != "/":
        s = s.replace(home, "~")
    s = re.sub(r"(/mnt/[a-z]/Users/)[^/\s]+", r"\1<user>", s)
    s = re.sub(r"([A-Za-z]:\\Users\\)[^\\\s]+", r"\1<user>", s)
    return s


_PROXY_RX = re.compile(r"\b(?:https?|socks5?)://[^\s'\"]+", re.I)


def share_text(text: str) -> str:
    """The share form: paths without the user name, proxy addresses hidden."""
    s = _share_path(text)
    s = _PROXY_RX.sub(lambda m: m.group(0) if _is_download_site(m.group(0)) else "<proxy>", s)
    return s


def _is_download_site(url: str) -> bool:
    return any(h in url for h, _ in NETWORK_HOSTS) or "lanex-eda" in url or "librelane" in url


# ---------------------------------------------------------------- checks

def check_lanex(ctx: Dict[str, Any]) -> Dict[str, Any]:
    t = TITLES["lanex"]
    try:
        from .._version import get_version
        ver = get_version()
    except Exception:
        ver = "unknown"
    ctx["version"] = ver
    bits = [ver]
    try:
        import importlib.metadata as md
        ll = md.version("librelane")
        bits.append(f"LibreLane {ll}")
        ctx["librelane"] = ll
    except Exception:
        return result("lanex", t, "fail",
                      "LanEx's own Python packages are damaged (LibreLane does not load).",
                      next_text=_repair_next(), data={"version": ver})
    try:
        import importlib.metadata as md
        bits.append(f"ciel {md.version('ciel')}")
    except Exception:
        pass
    import platform
    bits.append(f"Python {platform.python_version()}")
    kind = "Windows app" if is_windows_appliance() else ("macOS" if sys.platform == "darwin" else "Linux")
    return result("lanex", t, "ok", " · ".join(bits), data={"version": ver, "install": kind})


def check_files(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from . import platform_env

    t = TITLES["files"]
    home = platform_env.home()
    shown = _share_path(str(home))
    if not home.exists():
        return result("files", t, "info", f"{shown} does not exist yet; LanEx creates it on first start.",
                      data={"home": str(home)})
    if not os.access(str(home), os.W_OK | os.X_OK):
        nxt = _repair_next() if is_windows_appliance() else f"Run: sudo chown -R $USER {shown}"
        return result("files", t, "fail", f"LanEx can't write to {shown} (permission denied).",
                      next_text=nxt, command="" if is_windows_appliance() else f"sudo chown -R $USER {shown}",
                      data={"home": str(home)})
    settings = home / "settings.json"
    if settings.is_file():
        try:
            json.loads(settings.read_text(encoding="utf-8") or "{}")
        except Exception:
            return result("files", t, "warn", "settings.json is damaged, so LanEx uses its defaults.",
                          next_text=f"Delete {_share_path(str(settings))}; LanEx writes a fresh one.",
                          data={"home": str(home)})
    key = home / "auth-token"
    if key.is_file() and sys.platform != "win32":
        try:
            mode = key.stat().st_mode & 0o777
            if mode & 0o077:
                return result("files", t, "warn", "Other users on this computer can read the LanEx key file.",
                              next_text=f"Run: chmod 600 {_share_path(str(key))}",
                              command=f"chmod 600 {_share_path(str(key))}", data={"home": str(home)})
        except Exception:
            pass
    return result("files", t, "ok", f"{shown}" + (", key file private" if key.is_file() else ""),
                  data={"home": str(home)})


def check_system(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from . import platform_env

    t = TITLES["system"]
    import platform
    os_name = platform.platform()
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    data: Dict[str, Any] = {"os": os_name, "arch": platform.machine()}
    if sys.platform == "darwin":
        mac = platform.mac_ver()[0] or "?"
        data["macos"] = mac
        summary = f"macOS {mac} ({platform.machine()})"
        try:
            major = int(mac.split(".")[0])
        except Exception:
            major = 0
        if major and major < 13:
            return result("system", t, "warn", summary + "; LanEx's container engines need macOS 13 or newer.",
                          next_text="Update macOS to 13 (Ventura) or newer, or use the Local tools engine.",
                          data=data)
        if platform.machine() == "arm64":
            rosetta = _rosetta_present()
            data["rosetta"] = rosetta
            if rosetta is False:
                return result("system", t, "warn",
                              summary + "; the 3D view (GDS3D) needs Rosetta on Apple Silicon.",
                              next_text="Run: softwareupdate --install-rosetta (this accepts Apple's licence).",
                              command="softwareupdate --install-rosetta", data=data)
        return result("system", t, "ok", summary, data=data)
    if platform_env.is_wsl():
        distro = os.environ.get("WSL_DISTRO_NAME", "")
        data["distro"] = distro
        state = ""
        rc, out, _ = _run(["systemctl", "is-system-running"], 4)
        state = out.strip() or ("not running" if rc == 127 else "")
        data["systemd"] = state
        summary = f"{os_name} in WSL 2" + (f" (distro {distro})" if distro else "")
        if is_windows_appliance() and state in ("offline", "not running", ""):
            return result("system", t, "fail",
                          summary + "; its service manager (systemd) is not running, so Docker can't start.",
                          next_text="Quit LanEx from the tray and open it again; if this stays, run Repair LanEx.",
                          data=data)
        if state:
            summary += f", systemd {state}"
        return result("system", t, "ok", summary, data=data)
    return result("system", t, "ok", f"{os_name} ({platform.machine()})", data=data)


def _rosetta_present() -> Optional[bool]:
    try:
        from .installer import _rosetta_present as rp
        return rp()
    except Exception:
        pass
    if not Path("/usr/bin/pgrep").exists():
        return None
    rc, _, _ = _run(["/usr/bin/pgrep", "-q", "oahd"], 3)
    return rc == 0


def check_memory(ctx: Dict[str, Any]) -> Dict[str, Any]:
    t = TITLES["memory"]
    mem = _mac_mem() if sys.platform == "darwin" else _meminfo()
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable")
    data = {"total_gb": total and round(total, 1), "available_gb": avail and round(avail, 1)}
    try:
        from . import wslconfig
        wc = wslconfig.read_memory()
        if wc.get("value"):
            data["wslconfig_memory"] = wc.get("value")
    except Exception:
        pass
    if total is None:
        return result("memory", t, "skip", "Could not read the memory size.", data=data)
    where = "for LanEx's Linux" if is_windows_appliance() else "in total"
    summary = f"{fmt_gb(total)} {where}" + (f", {fmt_gb(avail)} free now" if avail is not None else "")
    if total < MEM_FAIL_GB:
        return result("memory", t, "fail", summary + "; LibreLane needs at least about 2.5 GB.",
                      next_text=("Give WSL more memory in Tools > Memory for Linux." if is_windows_appliance()
                                 else "Use a computer with more memory."), ui="#tools?item=tl-sys-memory", data=data)
    if avail is not None and avail < 2.0:
        return result("memory", t, "warn", summary + "; a run may slow down or stop part-way.",
                      next_text="Close apps you don't need" + (", or give WSL more memory in Tools > Memory for Linux."
                                                                 if is_windows_appliance() else "."),
                      ui="#tools?item=tl-sys-memory", data=data)
    return result("memory", t, "ok", summary, data=data)


def check_disk(ctx: Dict[str, Any]) -> Dict[str, Any]:
    t = TITLES["disk"]
    tgt = storage_target()
    free = tgt.get("free_gb")
    data = dict(tgt)
    if free is None:
        return result("disk", t, "skip", "Could not read the free disk space.", data=data)
    summary = f"{fmt_gb(free)} free on {tgt['label']}"
    nxt = (f"Free space on {tgt['label']}, or remove old images and PDK versions in Tools > Clean up."
           if tgt.get("windows") else "Free some space, or remove old images and PDK versions in Tools > Clean up.")
    if free < DISK_FAIL_GB:
        return result("disk", t, "fail", f"Only {summary}. Runs and downloads stop with 'no space left on device'.",
                      next_text=nxt, ui="#tools?item=tl-sys-storage", data=data)
    if free < DISK_WARN_GB:
        return result("disk", t, "warn", f"{summary}; updating the tool image needs about 10 GB.",
                      next_text=nxt, ui="#tools?item=tl-sys-storage", data=data)
    return result("disk", t, "ok", summary, data=data)


def check_engine(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from . import tools

    t = TITLES["engine"]
    c = tools.container_engine()
    ctx["container"] = c
    engine = c.get("engine")
    data = {k: c.get(k) for k in ("engine", "version", "min_version", "ready", "platform", "sg_wrap")}
    run_mode = _run_mode()
    data["run_mode"] = run_mode
    if not c.get("available"):
        if run_mode == "local":
            return result("engine", t, "skip", "No Docker or Podman; the run engine is Local tools.", data=data)
        if sys.platform == "darwin":
            nxt = "Tools > Container engine > Install (Podman or Docker Desktop)."
        else:
            nxt = "Tools > Container engine > Install, or run: sudo apt install docker.io"
        return result("engine", t, "fail", "No container engine (Docker or Podman), so container runs can't start.",
                      next_text=nxt, ui="#tools?item=engine", data=data)
    name = (engine or ("docker" if (c.get("docker") or {}).get("present") else "podman")).capitalize()
    if not c.get("ready"):
        msg = (c.get("daemon_msg") or "").strip().splitlines()[:1]
        if is_windows_appliance():
            nxt = "Quit LanEx from the tray and open it again; that restarts Docker inside LanEx's Linux."
        elif sys.platform == "darwin":
            nxt = "Tools > Container engine > Start (opens Docker Desktop or starts the Podman machine)."
        else:
            nxt = "Tools > Container engine > Start, or run: sudo systemctl start docker"
        return result("engine", t, "fail", f"{name} is installed but not answering.",
                      next_text=nxt, ui="#tools?item=engine", details=msg, data=data)
    if c.get("version") and not c.get("version_ok", True):
        return result("engine", t, "fail",
                      f"{name} {_short_ver(c.get('version'))} is older than LibreLane needs "
                      f"({c.get('min_version')} or newer).",
                      next_text="Update it from your system's packages.", data=data)
    bits = [f"{name} {_short_ver(c.get('version'))}".strip(), "answering"]
    if c.get("sg_wrap"):
        bits.append("through the docker group (sg)")
    if engine == "podman":
        rootless = _podman_rootless()
        data["rootless"] = rootless
        if rootless is True and not _subuid_ok():
            return result("engine", t, "warn", " · ".join(bits) + "; rootless Podman has no /etc/subuid entry for you.",
                          next_text="Ask an administrator to add you to /etc/subuid and /etc/subgid, "
                                    "then run: podman system migrate", data=data)
        if rootless is True:
            bits.append("rootless")
    return result("engine", t, "ok", ", ".join(bits), data=data)


def _short_ver(v: Any) -> str:
    m = re.search(r"\d+\.\d+(?:\.\d+)?", str(v or ""))
    return m.group(0) if m else str(v or "")[:24]


def _run_mode() -> str:
    try:
        from . import platform_env
        p = platform_env.home() / "settings.json"
        d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        mode = str(d.get("run_mode") or d.get("engine_mode") or "")
        return "local" if mode == "local" else "container"
    except Exception:
        return "container"


def _podman_rootless() -> Optional[bool]:
    rc, out, _ = _run(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], 8)
    if rc != 0:
        return None
    return out.strip().lower() == "true"


def _subuid_ok() -> bool:
    import getpass
    try:
        user = getpass.getuser()
        text = Path("/etc/subuid").read_text(encoding="utf-8")
        return any(line.split(":", 1)[0] in (user, str(os.getuid())) for line in text.splitlines())
    except Exception:
        return False


def check_image(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from .container_run import image_ref, image_record_state

    t = TITLES["image"]
    c = ctx.get("container") or {}
    ref = image_ref()
    data: Dict[str, Any] = {"image": ref}
    if not c.get("ready"):
        return result("image", t, "skip", "Not checked: the container engine is not answering.", data=data)
    if c.get("pulling"):
        return result("image", t, "info", f"{ref} is downloading now.", data=data)
    if not c.get("image_present"):
        return result("image", t, "fail", f"{ref} is not downloaded, so container runs can't start.",
                      next_text="Tools > LibreLane image > Download (about 1.5 GB download, 7.5 GB on disk).",
                      command="lanex --pull-image", ui="#tools?item=image", data=data)
    data["size_mb"] = c.get("image_size_mb")
    try:
        rec = image_record_state()
        if rec.get("stale"):
            return result("image", t, "warn",
                          f"The image record is for LibreLane {rec.get('version') or 'another version'}, "
                          f"but LanEx now runs {ctx.get('librelane') or 'a newer one'}.",
                          next_text="Tools > LibreLane image > Update image.", ui="#tools?item=image", data=data)
    except Exception:
        pass
    size = c.get("image_size_mb")
    shown = ref.split("@", 1)[0]
    if "@sha256:" in ref:
        shown += f" ({ref.split('@sha256:', 1)[1][:7]})"
    return result("image", t, "ok", shown + (f" · {fmt_gb(size * 1.048576 / 1000)} on disk" if size else ""),
                  data=data)


def check_pdks(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from . import pdk as pdk_mod
    from . import tools

    t = TITLES["pdks"]
    roots = tools._pdk_roots()
    data: Dict[str, Any] = {"pdk_root": os.environ.get("PDK_ROOT") or "", "roots": [str(r) for r in roots]}
    variants = ["sky130A", "sky130B", "gf180mcuA", "gf180mcuB", "gf180mcuC", "gf180mcuD", "ihp-sg13g2"]
    installed: List[str] = []
    incomplete: List[str] = []
    for root in roots:
        for v in variants:
            vdir = root / v
            if v in installed or v in incomplete or not (vdir / "libs.ref").is_dir():
                continue
            if pdk_mod.variant_incomplete(vdir):
                incomplete.append(v)
            else:
                installed.append(v)
    data["installed"] = installed
    data["incomplete"] = incomplete
    where = _share_path(str(roots[0])) if roots else "no PDK folder"
    if incomplete:
        v = incomplete[0]
        return result("pdks", t, "fail", f"{v} did not finish installing (the last install stopped while unpacking).",
                      next_text=f"Tools > PDKs > {v} > Repair.", ui=f"#tools?item={v}", data=data)
    if not installed:
        if ctx.get("container", {}).get("ready"):
            return result("pdks", t, "fail", "No PDK is installed, so no design can run.",
                          next_text="Tools > PDKs > sky130A > Install (about 0.4 GB download).",
                          ui="#tools?item=sky130A", data=data)
        return result("pdks", t, "warn", "No PDK is installed yet.",
                      next_text="Tools > PDKs > sky130A > Install.", ui="#tools?item=sky130A", data=data)
    try:
        from .installer import ciel_permission_status
        perm = ciel_permission_status()
        if perm.get("needs_root"):
            return result("pdks", t, "fail", "Some PDK files belong to root, so LanEx can't update them.",
                          next_text="Tools > PDKs > Fix permissions.", ui="#tools?item=pdk", data=data)
    except Exception:
        pass
    catalog = {}
    try:
        catalog = tools.build_pdk_catalog()
    except Exception:
        pass
    vers = tools.pdk_versions(installed, catalog) if catalog else {}
    off = [v for v, x in vers.items() if x.get("installed") and x.get("pinned") and x["installed"] != x["pinned"]]
    data["versions"] = vers
    if off:
        v = off[0]
        return result("pdks", t, "warn",
                      f"{v} is version {vers[v]['installed'][:7]}; LibreLane {ctx.get('librelane') or ''} "
                      f"expects {vers[v]['pinned'][:7]}.".replace("  ", " "),
                      next_text=f"Tools > PDKs > {v} > Update.", ui=f"#tools?item={v}", data=data)
    return result("pdks", t, "ok", ", ".join(installed) + f" in {where}", data=data)


def check_tools(ctx: Dict[str, Any]) -> Dict[str, Any]:
    t = TITLES["tools"]
    have = {}
    for name in ("verilator", "iverilog", "gtkwave", "dot"):
        have[name] = bool(shutil.which(name))
    try:
        from . import platform_env
        have["gds3d"] = bool(shutil.which("GDS3D") or (platform_env.home() / "tools" / "gds3d").exists())
    except Exception:
        have["gds3d"] = False
    missing = [k for k, v in have.items() if not v]
    label = {"dot": "Graphviz", "gds3d": "GDS3D", "iverilog": "Icarus Verilog", "gtkwave": "GTKWave",
             "verilator": "Verilator"}
    if not missing:
        return result("tools", t, "ok", "Verilator, Icarus Verilog, GTKWave, Graphviz, GDS3D", data=have)
    return result("tools", t, "info", "Not installed: " + ", ".join(label[m] for m in missing) +
                  " (optional; Tools > Helpers installs them)", data=have)


def check_server(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from . import platform_env

    t = TITLES["server"]
    rec_path = platform_env.home() / "server.json"
    if not rec_path.is_file():
        return result("server", t, "info", "Not running (that's fine when you run doctor from a terminal).")
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        port = int(rec.get("port") or 0)
    except Exception:
        return result("server", t, "warn", "server.json is unreadable.",
                      next_text="Open LanEx again; it replaces the old record.")
    if ctx.get("in_server"):
        return result("server", t, "ok", f"running on 127.0.0.1:{port}", data={"port": port})
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
        d = body.get("data") if isinstance(body.get("data"), dict) else body
        if d.get("service") == "lanex":
            return result("server", t, "ok", f"running on 127.0.0.1:{port}", data={"port": port})
    except Exception:
        pass
    return result("server", t, "warn", f"server.json names port {port} but nothing answers there.",
                  next_text="Open LanEx again; it replaces the old record.", data={"port": port})


def _probe_url(url: str, timeout: float) -> Dict[str, Any]:
    import urllib.error
    import urllib.request

    try:
        from . import tlsctx
        ctx = tlsctx.https_context()
    except Exception:
        ctx = None
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "LanEx-doctor"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return {"ok": True, "status": r.status, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as ex:
        # Any HTTP answer means the site was reached (405/404/401 on HEAD are normal).
        if ex.code == 407:
            return {"ok": False, "category": "proxy-auth", "error": "the proxy asks for a sign-in (HTTP 407)"}
        return {"ok": True, "status": ex.code, "ms": int((time.time() - t0) * 1000)}
    except Exception as ex:  # noqa: BLE001
        text = f"{type(ex).__name__}: {ex}"
        return {"ok": False, "category": _net_category(text), "error": text[:240]}


def _net_category(text: str) -> str:
    s = text.lower()
    if "certificate" in s or "ssl" in s or "tls" in s:
        return "tls"
    if "name or service not known" in s or "getaddrinfo" in s or "nodename" in s or "temporary failure in name" in s:
        return "dns"
    if "timed out" in s or "timeout" in s:
        return "timeout"
    if "407" in s or "proxy authentication" in s:
        return "proxy-auth"
    return "route"


def proxy_in_effect() -> Dict[str, Any]:
    """The proxy this process would use, and where it comes from (no credentials)."""
    env = {k: os.environ.get(k, "") for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")}
    url = next((v for v in env.values() if v), "")
    src = "none"
    if url:
        src = "LanEx network settings" if Path("/etc/lanex/proxy.env").is_file() else "environment"
    host = ""
    if url:
        m = re.match(r"^[a-z0-9]+://(?:[^@/]*@)?([^/:]+(?::\d+)?)", url, re.I)
        host = m.group(1) if m else ""
    return {"url_host": host, "source": src, "set": bool(url),
            "no_proxy": os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""}


def check_network(ctx: Dict[str, Any]) -> Dict[str, Any]:
    t = TITLES["network"]
    if ctx.get("offline"):
        return result("network", t, "skip", "Not checked (--offline).")
    proxy = proxy_in_effect()
    data: Dict[str, Any] = {"proxy": proxy}
    try:
        from . import netsettings
        data["applied"] = netsettings.applied_summary()
    except Exception:
        pass
    results: Dict[str, Dict[str, Any]] = {}
    deadline = time.time() + NETWORK_BUDGET_S
    with ThreadPoolExecutor(max_workers=len(NETWORK_HOSTS), thread_name_prefix="lanex-doctor-net") as pool:
        futs = {h: pool.submit(_probe_url, u, NETWORK_PROBE_S) for h, u in NETWORK_HOSTS}
        for h, f in futs.items():
            try:
                results[h] = f.result(timeout=max(0.1, deadline - time.time()))
            except Exception:
                results[h] = {"ok": False, "category": "timeout", "error": f"no answer within {NETWORK_PROBE_S:g} s"}
    data["sites"] = results
    bad = [(h, r) for h, r in results.items() if not r.get("ok")]
    ptxt = f"proxy: {proxy['url_host']} ({proxy['source']})" if proxy["set"] else "proxy: none"
    if not bad:
        return result("network", t, "ok", f"{len(results)} of {len(results)} download sites reachable · {ptxt}", data=data)
    host, r = bad[0]
    cat = r.get("category") or "route"
    windows = is_windows_appliance()
    if cat == "dns":
        try:
            from . import platform_env
            nxt = platform_env.wsl_dns_remediation() if platform_env.is_wsl() else ""
        except Exception:
            nxt = ""
        summary = f"Can't look up {host} (DNS)."
        nxt = nxt or "Check the internet connection or VPN, then run lanex doctor again."
    elif cat == "proxy-auth":
        summary = "Your network's proxy asks for a sign-in that LanEx's Linux tools can't do."
        nxt = ("Ask IT for a proxy that needs no sign-in for these sites, or run a sign-in helper "
               "such as px on Windows and use it as the proxy.")
    elif cat == "tls":
        summary = f"{host}'s certificate is not trusted by LanEx's Linux (a company network may inspect HTTPS)."
        nxt = ("Tools > Network > Check again (copies your company certificate from Windows)." if windows else
               "Put your company's root certificate in /usr/local/share/ca-certificates/ and run: "
               "sudo update-ca-certificates")
    else:
        summary = f"{host} did not answer within {NETWORK_PROBE_S:g} s."
        nxt = "Check VPN, firewall or proxy settings, or try another network."
    n_ok = len(results) - len(bad)
    status = "fail" if n_ok == 0 or host in ("ghcr.io", "pypi.org", "github.com") else "warn"
    return result("network", t, status, f"{summary} {n_ok} of {len(results)} sites reachable · {ptxt}",
                  next_text=nxt, ui="#tools?item=tl-sys-network",
                  details=[f"{h}: {x.get('error')}" for h, x in bad], data=data)


def check_updates(ctx: Dict[str, Any]) -> Dict[str, Any]:
    from . import updatecheck

    t = TITLES["updates"]
    st = updatecheck.status()
    if not st.get("checked_at"):
        return result("updates", t, "skip", "Not checked yet (LanEx checks once a day while the setting is on).")
    if st.get("newer") and st.get("latest"):
        rel = st["latest"]
        how = (st.get("how") or {}).get("text") or ""
        return result("updates", t, "info", f"LanEx {rel.get('version')} is out (you have {st.get('current')}).",
                      next_text=how, command=(st.get("how") or {}).get("command") or "",
                      ui="#tools?item=tl-sys-lanex", data={"latest": rel.get("version")})
    return result("updates", t, "ok", f"{st.get('current')} is the newest release", data={})


CHECKS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "lanex": check_lanex, "files": check_files, "system": check_system, "memory": check_memory,
    "disk": check_disk, "engine": check_engine, "image": check_image, "pdks": check_pdks,
    "tools": check_tools, "server": check_server, "network": check_network, "updates": check_updates,
}
# Per-check time limits (s). A check over its limit becomes a warn that says so.
LIMITS = {"engine": 25.0, "image": 15.0, "network": NETWORK_BUDGET_S + 3, "pdks": 15.0, "system": 8.0}


def _timed(cid: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]], ctx: Dict[str, Any]) -> Dict[str, Any]:
    box: Dict[str, Any] = {}
    t0 = time.time()

    def _go() -> None:
        try:
            box["r"] = fn(ctx)
        except Exception as ex:  # noqa: BLE001
            box["r"] = result(cid, TITLES[cid], "warn", f"This check stopped with an error: {type(ex).__name__}: {ex}",
                              next_text="Run lanex doctor again; if it repeats, report it with the saved report.")

    th = threading.Thread(target=_go, name=f"lanex-doctor-{cid}", daemon=True)
    th.start()
    limit = LIMITS.get(cid, 6.0)
    th.join(limit)
    if "r" not in box:
        r = result(cid, TITLES[cid], "warn", f"This check did not finish within {limit:g} s.",
                   next_text="Run lanex doctor again; a slow disk or a busy Docker can cause this.")
    else:
        r = box["r"]
    r["ms"] = int((time.time() - t0) * 1000)
    return r


def platform_info() -> Dict[str, Any]:
    import platform
    from . import platform_env

    kind = "windows-wsl" if is_windows_appliance() else ("wsl" if platform_env.is_wsl() else
                                                         ("macos" if sys.platform == "darwin" else "linux"))
    return {"kind": kind, "os": platform.platform(), "python": platform.python_version()}


def run(*, offline: bool = False, only: Optional[List[str]] = None, in_server: bool = False,
        on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Every check, in order. Never raises."""
    ctx: Dict[str, Any] = {"offline": offline, "in_server": in_server}
    checks: List[Dict[str, Any]] = []
    for cid in CHECK_ORDER:
        if only and cid not in only:
            continue
        if on_progress:
            on_progress(TITLES[cid])
        checks.append(_timed(cid, CHECKS[cid], ctx))
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES}
    return {
        "schema": SCHEMA, "tool": "lanex doctor", "lanex": ctx.get("version") or "",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform_info(), "summary": counts, "checks": checks,
    }


def exit_code(report: Dict[str, Any], strict: bool = False) -> int:
    s = report.get("summary") or {}
    if s.get("fail"):
        return 1
    if strict and s.get("warn"):
        return 1
    return 0


# ---------------------------------------------------------------- text

_MARK = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[fail]", "info": "[info]", "skip": "[skip]"}
_COLOR = {"ok": "32", "warn": "33", "fail": "31", "info": "34", "skip": "90"}


def summary_line(report: Dict[str, Any]) -> str:
    s = report.get("summary") or {}
    f, w = int(s.get("fail") or 0), int(s.get("warn") or 0)
    if not f and not w:
        return "No problems found."
    bits = []
    if f:
        bits.append(f"{f} problem" + ("" if f == 1 else "s"))
    if w:
        bits.append(f"{w} warning" + ("" if w == 1 else "s"))
    return ", ".join(bits) + ". Problems stop runs; warnings can slow or limit them."


def to_text(report: Dict[str, Any], *, color: bool = False, verbose: bool = False, share: bool = False,
            width: int = 100) -> str:
    """The human report: one row per check, ``Next:`` under every problem, and a
    summary that counts exactly the marks above it."""
    import textwrap

    p = report.get("platform") or {}
    head = f"LanEx doctor · lanex {report.get('lanex') or '?'} · {p.get('kind', '')} · {report.get('generated', '')}"
    lines = [head, ""]
    tw = max(60, min(int(width or 100), 120))
    for c in report.get("checks") or []:
        st = c.get("status", "info")
        mark = _MARK.get(st, "[info]")
        if color:
            mark = f"\x1b[{_COLOR.get(st, '0')}m{mark}\x1b[0m"
        title = f"{c.get('title', ''):<19}"
        summary = c.get("summary", "")
        if share:
            summary = share_text(summary)
        body = textwrap.wrap(summary, width=tw - 27) or [""]
        lines.append(f"{mark} {title} {body[0]}")
        for extra in body[1:]:
            lines.append(" " * 27 + extra)
        if verbose:
            for d in c.get("details") or []:
                lines.append(" " * 27 + "· " + (share_text(d) if share else d))
            lines.append(" " * 27 + f"· {c.get('ms', 0)} ms")
        nxt = c.get("next") or {}
        if st in ("warn", "fail") or (st == "info" and nxt.get("text")):
            text = nxt.get("text") or ""
            if nxt.get("command") and nxt["command"] not in text:
                text = (text + " " if text else "") + "Or run: " + nxt["command"]
            if text:
                lines.append(" " * 7 + "Next: " + (share_text(text) if share else text))
    lines.append("")
    lines.append(summary_line(report))
    return "\n".join(lines) + "\n"


def save_report(report: Dict[str, Any]) -> Optional[Path]:
    """Write the share form to ``$LANEX_HOME/logs/doctor-<stamp>.txt``; keep the last 10."""
    try:
        from . import platform_env
        d = platform_env.home() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / time.strftime("doctor-%Y%m%d-%H%M%S.txt")
        path.write_text(to_text(report, share=True, verbose=True), encoding="utf-8")
        old = sorted(d.glob("doctor-*.txt"))[:-10]
        for o in old:
            try:
                o.unlink()
            except OSError:
                pass
        return path
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="lanex doctor",
        description="Check that this computer can run LanEx, and say what to do about each problem. "
                    "Exit code 0: no problems; 1: a problem (or a warning with --strict); 2: the doctor crashed. "
                    "--json keeps the same exit codes.")
    ap.add_argument("--json", action="store_true", help="print the report as JSON (schema 1)")
    ap.add_argument("--offline", action="store_true", help="skip the network check")
    ap.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    ap.add_argument("--share", action="store_true",
                    help="hide your user name and proxy address, for pasting into a bug report")
    ap.add_argument("-v", "--verbose", action="store_true", help="details and timings under each check")
    args = ap.parse_args(argv)
    try:
        tty = sys.stdout.isatty() and not args.json
        progress = None
        if tty:
            def progress(name: str) -> None:
                sys.stdout.write(f"\rChecking {name}...{' ' * 20}")
                sys.stdout.flush()
        report = run(offline=args.offline, on_progress=progress)
        if tty:
            sys.stdout.write("\r" + " " * 60 + "\r")
        saved = save_report(report)
        report["report"] = _share_path(str(saved)) if saved else ""
        code = exit_code(report, args.strict)
        report["summary"]["exit"] = code
        if args.json:
            if args.share:
                report = json.loads(share_text(json.dumps(report)))
            sys.stdout.write(json.dumps(report, indent=1) + "\n")
        else:
            color = tty and not os.environ.get("NO_COLOR")
            width = shutil.get_terminal_size((100, 24)).columns
            sys.stdout.write(to_text(report, color=color, verbose=args.verbose, share=args.share, width=width))
            if saved:
                sys.stdout.write(f"Report saved to {_share_path(str(saved))}\n"
                                 "(user name and proxy address removed, so you can paste it into a bug report).\n")
        return code
    except Exception as ex:  # noqa: BLE001
        sys.stderr.write(f"LanEx doctor crashed: {type(ex).__name__}: {ex}. Please report it at "
                         "https://github.com/lanex-eda/lanex/issues with this message.\n")
        return 2
