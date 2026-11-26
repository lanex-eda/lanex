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
"""The Windows network (proxy and company certificates) inside LanEx's Linux (audit 5c).

On a company or university network, Windows knows the proxy (often through an
automatic-setup PAC script) and trusts the company's TLS-inspection root, but
the Linux tools LanEx runs (apt, Docker, pip, ciel) see neither. Two halves:

- Windows side, ``windows/setup/network.ps1``: resolves the proxy per download
  site with Windows' own resolver and records the root certificate each site's
  TLS chain ends in. It writes ``%LOCALAPPDATA%\\LanEx\\state\\network.json`` and
  ``network-ca.pem``. Setup runs it, and the launcher runs it at every start.
- Linux side, ``/usr/local/lib/lanex/network-apply`` (``windows/provision/
  network-apply.py``, root): writes only LanEx-owned files (proxy environment,
  apt, Docker drop-ins, certificates) and restarts Docker only when nothing runs.

This module is the app's view: load ``/etc/lanex/proxy.env`` into the server's
own environment at start (so ciel, pip and urllib children inherit it), apply a
changed Windows snapshot in the background, and report what is in effect for
the Tools page and the doctor. It never stores or prints a password.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROXY_ENV = Path("/etc/lanex/proxy.env")
APPLIED = Path("/var/lib/lanex/network.applied.json")
PENDING = Path("/var/lib/lanex/network.pending")
APPLIER = Path("/usr/local/lib/lanex/network-apply")
CA_DIR = Path("/usr/local/share/ca-certificates/lanex")
_ENV_KEYS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy",
             "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT")
_LINE_RX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

_lock = threading.Lock()
_ours: set = set()   # variables this process took from proxy.env (a person's own stay)


def parse_env_file(text: str) -> Dict[str, str]:
    """``KEY=VALUE`` lines (optionally quoted, ``export`` allowed); known keys only."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        m = _LINE_RX.match(line)
        if not m or m.group(1) not in _ENV_KEYS:
            continue
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[m.group(1)] = val
    return out


def load_proxy_env(path: Path = PROXY_ENV, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Put LanEx's network settings into this process's environment.

    A variable the person set themselves wins (it is already in the environment)."""
    env = os.environ if environ is None else environ
    try:
        values = parse_env_file(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    added = {}
    for k, v in values.items():
        if k not in env:
            env[k] = v
            added[k] = v
            if environ is None:
                _ours.add(k)
    return added


def _redact(url: str) -> str:
    return re.sub(r"(://)[^@/]*@", r"\1", str(url or ""))


def _host(url: str) -> str:
    m = re.match(r"^[a-z0-9]+://(?:[^@/]*@)?([^/]+)", str(url or ""), re.I)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- Windows snapshot

def windows_state_dir() -> Optional[Path]:
    try:
        from . import platform_env

        win = platform_env.wsl_windows_home()
        return Path(win) / "AppData" / "Local" / "LanEx" / "state" if win else None
    except Exception:
        return None


def snapshot_path() -> Optional[Path]:
    env = os.environ.get("LANEX_WIN_NETWORK")
    if env:
        return Path(env)
    d = windows_state_dir()
    return d / "network.json" if d else None


def read_snapshot(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or snapshot_path()
    try:
        if p and p.is_file():
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) and data.get("schema") == 1 else None
    except Exception:
        pass
    return None


def snapshot_hash(path: Optional[Path] = None) -> str:
    p = path or snapshot_path()
    h = hashlib.sha256()
    try:
        h.update(p.read_bytes())
        pem = p.with_name("network-ca.pem")
        if pem.is_file():
            h.update(pem.read_bytes())
    except Exception:
        return ""
    return h.hexdigest()


def network_script() -> Optional[str]:
    """``network.ps1`` as a Windows path (the launcher's install folder)."""
    env = os.environ.get("LANEX_WIN_APPDIR")
    candidates = []
    if env:
        candidates.append(Path(env) / "network.ps1")
    try:
        from . import platform_env

        win = platform_env.wsl_windows_home()
        if win:
            candidates.append(Path(win) / "AppData" / "Local" / "Programs" / "LanEx" / "network.ps1")
    except Exception:
        pass
    for c in candidates:
        if c.is_file():
            try:
                from . import platform_env

                return platform_env.wsl_windows_path(str(c)) or None
            except Exception:
                return None
    return None


def refresh_windows_snapshot(timeout: float = 45.0) -> Dict[str, Any]:
    """Ask Windows for its network settings now (through interop)."""
    script = network_script()
    out_dir = windows_state_dir()
    if not script or not out_dir:
        return {"ok": False, "error": "LanEx's network helper for Windows is not installed (run LanEx Setup again)."}
    try:
        from . import platform_env

        win_out = platform_env.wsl_windows_path(str(out_dir)) or ""
    except Exception:
        win_out = ""
    if not win_out:
        return {"ok": False, "error": "LanEx could not find its Windows folder."}
    try:
        p = subprocess.run(
            ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", script, "-OutDir", win_out],
            capture_output=True, text=True, timeout=timeout, cwd="/mnt/c", stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Windows did not answer within {timeout:g} s."}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)}
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout or "").strip()[-300:] or f"exit {p.returncode}"}
    return {"ok": True}


# ---------------------------------------------------------------- apply

def applied_summary() -> Dict[str, Any]:
    try:
        d = json.loads(APPLIED.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def apply(path: Optional[Path] = None, *, restart_docker: bool = True, timeout: float = 120.0) -> Dict[str, Any]:
    """Run the root applier on a snapshot. ``sudo -n`` only: the appliance allows
    it; elsewhere this answers with words instead of prompting."""
    p = path or snapshot_path()
    if not p or not p.is_file():
        return {"ok": False, "error": "There is no network snapshot from Windows yet."}
    if not APPLIER.is_file():
        return {"ok": False, "error": "LanEx's network helper is missing in its Linux (run Repair LanEx)."}
    argv = ["sudo", "-n", str(APPLIER), "--input", str(p)]
    pem = p.with_name("network-ca.pem")
    if pem.is_file():
        argv += ["--certs", str(pem)]
    if not restart_docker:
        argv.append("--no-restart")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"The network helper did not finish within {timeout:g} s."}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)}
    try:
        res = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        res = {"ok": r.returncode == 0, "error": (r.stderr or "").strip()[-300:]}
    if res.get("ok"):
        # This process starts ciel and pip: give it the new settings too.
        with _lock:
            for k in list(_ours):
                os.environ.pop(k, None)
                _ours.discard(k)
            load_proxy_env()
    return res


def apply_if_changed_async() -> None:
    """At server start: apply a Windows snapshot that differs from the applied one."""
    def _go() -> None:
        try:
            # The launcher writes a fresh snapshot while the server starts.
            time.sleep(20)
            p = snapshot_path()
            if not p or not p.is_file() or not APPLIER.is_file():
                return
            h = snapshot_hash(p)
            if h and h != applied_summary().get("input_hash"):
                apply(p)
        except Exception:
            pass

    threading.Thread(target=_go, name="lanex-network-apply", daemon=True).start()


# ---------------------------------------------------------------- status

def docker_proxy() -> Dict[str, Any]:
    try:
        from . import tools

        r = tools.resolve_engine()
        if not r.get("ready") or r.get("engine") != "docker":
            return {}
        argv = ["docker", "info", "--format", "{{json .HTTPProxy}} {{json .HTTPSProxy}} {{json .NoProxy}}"]
        if r.get("sg_wrap"):
            argv = tools.sg_wrap_argv(argv)
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        parts = (p.stdout or "").strip().split(" ")
        vals = [json.loads(x) if x else "" for x in parts[:3]]
        return {"http": _host(vals[0]) if vals else "", "https": _host(vals[1]) if len(vals) > 1 else "",
                "no_proxy": vals[2] if len(vals) > 2 else ""}
    except Exception:
        return {}


def status() -> Dict[str, Any]:
    """What the Tools Network tile shows. No credentials, ever."""
    env = parse_env_file(PROXY_ENV.read_text(encoding="utf-8")) if PROXY_ENV.is_file() else {}
    snap = read_snapshot() or {}
    applied = applied_summary()
    certs = sorted(p.name for p in CA_DIR.glob("*.crt")) if CA_DIR.is_dir() else []
    proxy = env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or ""
    try:
        from . import platform_env

        windows = platform_env.is_wsl() and (platform_env.home() / "runtime.json").is_file()
    except Exception:
        windows = False
    if not windows:
        proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or \
            os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    dp = docker_proxy()
    snap_hash = snapshot_hash() if snap else ""
    return {
        "windows": windows,
        "mode": snap.get("mode") or ("env" if proxy else "none"),
        "proxy_host": _host(proxy),
        "proxy_source": ("Windows" if env else ("environment" if proxy else "")),
        "no_proxy": env.get("NO_PROXY") or "",
        "certificates": [{"subject": c.get("subject", ""), "sha256": str(c.get("sha256", ""))[:16]}
                         for c in (snap.get("certificates") or []) if isinstance(c, dict)],
        "certificates_added": len(certs),
        "docker": dp,
        "docker_uses_proxy": bool(dp.get("https") or dp.get("http")) if proxy else None,
        "applied_at": applied.get("applied_at") or 0,
        "snapshot_at": snap.get("generated") or "",
        "pending": PENDING.exists(),
        "current": bool(snap_hash) and snap_hash == applied.get("input_hash"),
        "auth": snap.get("auth") or "",
        "problems": [p for p in (snap.get("problems") or []) if isinstance(p, str)][:5],
        "helper": APPLIER.is_file(),
    }
