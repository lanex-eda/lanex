#!/usr/bin/env python3
# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""network-apply: the Windows network (proxy, company certificates) inside LanEx's Linux.

Runs as root inside the LanEx WSL distro. Installed as
/usr/local/lib/lanex/network-apply by provision.sh; Setup, the finalizer and the
app (``sudo -n``) call it with the snapshot Windows wrote
(windows/setup/network.ps1: ``network.json`` and ``network-ca.pem``).

It writes only files LanEx owns, each marked "Managed by LanEx", and removes all
of them when Windows uses no proxy and no company certificate:

  /etc/lanex/proxy.env                                   proxy for LanEx and its tools
  /etc/apt/apt.conf.d/90lanex-proxy                      apt
  /etc/systemd/system/docker.service.d/90-lanex-proxy.conf    dockerd (image pulls)
  /etc/systemd/system/containerd.service.d/90-lanex-proxy.conf
  /etc/profile.d/90-lanex-proxy.sh                       "Open LanEx shell"
  /usr/local/share/ca-certificates/lanex/<sha256>.crt    company roots, then update-ca-certificates

Never: a password (a proxy URL with user:pass@ is refused), TLS verification
turned off, /etc/docker/daemon.json, .wslconfig, or Windows' own settings. Docker
is restarted only when no container is running (a restart would kill a run);
otherwise /etc/lanex/network.pending records it and ``--restart-if-idle``
finishes it later. The last line of output is a JSON result.

stdlib only: it runs before LanEx's own Python environment exists.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HEADER = "# Managed by LanEx (network-apply): the Windows proxy and company certificates.\n"
MODES = {"none", "manual", "pac", "wpad", "env"}
_HOST_RX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_URL_RX = re.compile(r"^(https?)://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::(\d{1,5}))?/?$")
_NOPROXY_ITEM_RX = re.compile(r"^[A-Za-z0-9.*:/_\[\]-]{1,253}$")
_PEM_RX = re.compile(r"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----", re.S)


class Refused(Exception):
    pass


# ---------------------------------------------------------------- validation

def check_proxy_url(url: str) -> str:
    """'' or ``http(s)://host[:port]/``. Userinfo and other schemes are refused."""
    u = str(url or "").strip()
    if not u:
        return ""
    if "@" in u:
        raise Refused("the proxy address carries a user name or password; LanEx never stores those")
    m = _URL_RX.match(u)
    if not m:
        raise Refused(f"the proxy address is not an http:// or https:// address LanEx can use")
    host = m.group(2)
    if not host.startswith("[") and not _HOST_RX.match(host):
        raise Refused("the proxy host name has characters LanEx does not accept")
    if m.group(3) and not (1 <= int(m.group(3)) <= 65535):
        raise Refused("the proxy port is out of range")
    return f"{m.group(1)}://{host}" + (f":{m.group(3)}" if m.group(3) else "") + "/"


def check_no_proxy(value: str) -> str:
    items = []
    for raw in re.split(r"[,;\s]+", str(value or "")):
        if not raw:
            continue
        if not _NOPROXY_ITEM_RX.match(raw):
            continue
        if raw not in items:
            items.append(raw)
    for must in ("localhost", "127.0.0.1", "::1"):
        if must not in items:
            items.append(must)
    return ",".join(items)


def validate(snap: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snap, dict) or snap.get("schema") != 1:
        raise Refused("the network snapshot is not one LanEx wrote (schema 1)")
    mode = str(snap.get("mode") or "none")
    if mode not in MODES:
        raise Refused(f"unknown proxy mode {mode!r}")
    proxy = snap.get("proxy") or {}
    http = check_proxy_url(proxy.get("http", ""))
    https = check_proxy_url(proxy.get("https", "")) or http
    http = http or https
    per_host: Dict[str, str] = {}
    for host, val in (snap.get("perHost") or {}).items():
        if not _HOST_RX.match(str(host)):
            continue
        per_host[str(host)] = "DIRECT" if str(val).upper() == "DIRECT" else check_proxy_url(val)
    return {
        "mode": mode if (http or mode == "none") else "none",
        "http": http, "https": https,
        "no_proxy": check_no_proxy(snap.get("noProxy", "")),
        "per_host": per_host,
        "networking_mode": str(snap.get("networkingMode") or "nat").lower(),
    }


# ---------------------------------------------------------------- loopback proxies

def default_gateway() -> str:
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"default via (\S+)", out)
        if m:
            ipaddress.ip_address(m.group(1))
            return m.group(1)
    except Exception:
        pass
    return ""


def rewrite_loopback(url: str, networking_mode: str, gateway: str) -> Tuple[str, bool]:
    """A proxy on Windows' own 127.0.0.1 (px, cntlm, Clash) is the Windows host as
    seen from WSL's NAT network: its gateway. In mirrored mode 127.0.0.1 works."""
    m = _URL_RX.match(url or "")
    if not m or networking_mode == "mirrored":
        return url, False
    host = m.group(2).strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return url, False
    if not gateway:
        return url, False
    return f"{m.group(1)}://{gateway}" + (f":{m.group(3)}" if m.group(3) else "") + "/", True


# ---------------------------------------------------------------- certificates

def pem_blocks(text: str) -> List[bytes]:
    out = []
    for b64 in _PEM_RX.findall(text or ""):
        try:
            out.append(base64.b64decode("".join(b64.split()), validate=True))
        except Exception:
            continue
    return out


def der_to_pem(der: bytes) -> str:
    b = base64.b64encode(der).decode("ascii")
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + \
        "\n-----END CERTIFICATE-----\n"


def bundle_fingerprints(path: str) -> set:
    try:
        with open(path, encoding="ascii", errors="ignore") as f:
            return {hashlib.sha256(d).hexdigest() for d in pem_blocks(f.read())}
    except OSError:
        return set()


# ---------------------------------------------------------------- writing

def _p(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/")) if root != "/" else path


def write_owned(path: str, text: str, mode: int = 0o644) -> bool:
    """Write *text* if it differs. True when the file changed. Atomic."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".lanex-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def remove_owned(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            if not f.read().startswith("# Managed by LanEx"):
                return False   # not ours: never touched
    except OSError:
        return False
    os.unlink(path)
    return True


def _q(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def env_lines(net: Dict[str, Any], certs: bool) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if net["http"]:
        out += [("HTTP_PROXY", net["http"]), ("http_proxy", net["http"]),
                ("HTTPS_PROXY", net["https"]), ("https_proxy", net["https"]),
                ("NO_PROXY", net["no_proxy"]), ("no_proxy", net["no_proxy"])]
    if certs:
        bundle = "/etc/ssl/certs/ca-certificates.crt"
        out += [("SSL_CERT_FILE", bundle), ("REQUESTS_CA_BUNDLE", bundle), ("PIP_CERT", bundle)]
    return out


def apt_text(net: Dict[str, Any]) -> str:
    lines = [HEADER.rstrip("\n").replace("#", "//", 1)]
    lines.append(f'Acquire::http::Proxy "{net["http"]}";')
    lines.append(f'Acquire::https::Proxy "{net["https"]}";')
    for host, val in sorted(net["per_host"].items()):
        if host.endswith("ubuntu.com"):
            v = "DIRECT" if val == "DIRECT" else val
            lines.append(f'Acquire::http::Proxy::{host} "{v}";')
            lines.append(f'Acquire::https::Proxy::{host} "{v}";')
    return "\n".join(lines) + "\n"


def systemd_text(pairs: List[Tuple[str, str]]) -> str:
    env = " ".join(f'"{k}={_q(v).replace("%", "%%")}"' for k, v in pairs if "PROXY" in k.upper())
    return HEADER + "[Service]\n" + (f"Environment={env}\n" if env else "")


def apply(snap: Dict[str, Any], cert_text: str, *, root: str = "/", restart: bool = True,
          gateway: Optional[str] = None) -> Dict[str, Any]:
    net = validate(snap)
    notes: List[str] = []
    gw = default_gateway() if gateway is None else gateway
    for key in ("http", "https"):
        if net[key]:
            new, changed = rewrite_loopback(net[key], net["networking_mode"], gw)
            if changed:
                notes.append("the proxy runs on Windows itself; LanEx reaches it at the Windows host "
                             f"address {gw} (it must accept connections from WSL)")
            net[key] = new
    changed: List[str] = []

    # Certificates: only ones Ubuntu does not already trust.
    ca_dir = _p(root, "/usr/local/share/ca-certificates/lanex")
    known = bundle_fingerprints(_p(root, "/etc/ssl/certs/ca-certificates.crt"))
    want: Dict[str, bytes] = {}
    for der in pem_blocks(cert_text):
        fp = hashlib.sha256(der).hexdigest()
        if fp not in known:
            want[fp[:16] + ".crt"] = der
    have = set(os.listdir(ca_dir)) if os.path.isdir(ca_dir) else set()
    certs_changed = False
    for name in sorted(have - set(want)):
        if name.endswith(".crt"):
            os.unlink(os.path.join(ca_dir, name))
            certs_changed = True
    for name, der in want.items():
        if write_owned(os.path.join(ca_dir, name), der_to_pem(der)):
            certs_changed = True
    if os.path.isdir(ca_dir) and not os.listdir(ca_dir):
        os.rmdir(ca_dir)
    if certs_changed:
        changed.append("certificates")
        if root == "/" and shutil.which("update-ca-certificates"):
            subprocess.run(["update-ca-certificates"], capture_output=True, text=True, timeout=120)
    lanex_certs = bool(want)

    pairs = env_lines(net, lanex_certs)
    env_path = _p(root, "/etc/lanex/proxy.env")
    apt_path = _p(root, "/etc/apt/apt.conf.d/90lanex-proxy")
    prof_path = _p(root, "/etc/profile.d/90-lanex-proxy.sh")
    units = [_p(root, f"/etc/systemd/system/{u}.service.d/90-lanex-proxy.conf") for u in ("docker", "containerd")]
    docker_changed = False
    if pairs:
        if write_owned(env_path, HEADER + "".join(f"{k}={v}\n" for k, v in pairs)):
            changed.append("proxy.env")
        if write_owned(prof_path, HEADER + "[ -r /etc/lanex/proxy.env ] && { set -a; . /etc/lanex/proxy.env; set +a; }\n"):
            changed.append("profile")
    else:
        for p in (env_path, prof_path):
            if remove_owned(p):
                changed.append(os.path.basename(p))
    if net["http"]:
        if write_owned(apt_path, apt_text(net)):
            changed.append("apt")
        for u in units:
            if write_owned(u, systemd_text(pairs)):
                docker_changed = True
    else:
        if os.path.exists(apt_path) and _owned(apt_path) and os.unlink(apt_path) is None:
            changed.append("apt")
        for u in units:
            if remove_owned(u):
                docker_changed = True
    if docker_changed:
        changed.append("docker")
    pending = False
    if (docker_changed or certs_changed) and root == "/":
        pending = not restart_docker_if_idle(reload=True, allowed=restart)
    # /etc/lanex, not /var/lib/lanex (0700): the app reads these as the appliance user.
    state_dir = _p(root, "/etc/lanex")
    os.makedirs(state_dir, exist_ok=True)
    pend = os.path.join(state_dir, "network.pending")
    if pending:
        write_owned(pend, HEADER + "Docker restarts with the new network settings when no run is using it.\n")
    elif os.path.exists(pend) and (docker_changed or certs_changed):
        os.unlink(pend)
    summary = {
        "schema": 1, "applied_at": time.time(), "mode": net["mode"],
        "proxy_host": re.sub(r"^https?://|/$", "", net["https"] or ""),
        "certificates": len(want), "pending": pending, "changed": changed, "notes": notes,
        "input_hash": snap.get("_input_hash", ""),
    }
    write_owned(os.path.join(state_dir, "network.applied.json"), json.dumps(summary, indent=1) + "\n")
    return {"ok": True, **summary}


def _owned(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(64)
        return head.startswith("# Managed by LanEx") or head.startswith("// Managed by LanEx")
    except OSError:
        return False


def restart_docker_if_idle(*, reload: bool, allowed: bool = True) -> bool:
    """daemon-reload, then restart docker (and containerd) only when no container
    runs. True when the new settings are in effect (or Docker is not running)."""
    if not shutil.which("systemctl"):
        return True
    if reload:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=60)
    active = subprocess.run(["systemctl", "is-active", "--quiet", "docker"], timeout=20).returncode == 0
    if not active:
        return True
    if not allowed:
        return False
    try:
        running = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        running = "unknown"
    if running:
        return False
    for unit in ("containerd", "docker"):
        subprocess.run(["systemctl", "restart", unit], capture_output=True, timeout=180)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="network-apply", description=__doc__.splitlines()[0])
    ap.add_argument("--input", help="network.json from windows/setup/network.ps1")
    ap.add_argument("--certs", default="", help="network-ca.pem from the same run")
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    ap.add_argument("--no-restart", action="store_true", help="never restart Docker; record it as pending")
    ap.add_argument("--install", action="store_true", help="install this script as /usr/local/lib/lanex/network-apply")
    ap.add_argument("--restart-if-idle", action="store_true", help="finish a pending Docker restart if nothing runs")
    args = ap.parse_args(argv)
    try:
        if args.install:
            dest = _p(args.root, "/usr/local/lib/lanex/network-apply")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.abspath(__file__), dest + ".tmp")
            os.chmod(dest + ".tmp", 0o755)
            os.replace(dest + ".tmp", dest)
            if not args.input:
                print(json.dumps({"ok": True, "installed": dest}))
                return 0
        if args.restart_if_idle:
            pend = _p(args.root, "/etc/lanex/network.pending")
            if not os.path.exists(pend):
                print(json.dumps({"ok": True, "pending": False}))
                return 0
            done = restart_docker_if_idle(reload=True)
            if done:
                os.unlink(pend)
            print(json.dumps({"ok": True, "pending": not done}))
            return 0
        if not args.input:
            raise Refused("no snapshot given (--input)")
        with open(args.input, "rb") as f:
            raw = f.read()
        cert_raw = b""
        if args.certs and os.path.isfile(args.certs):
            with open(args.certs, "rb") as f:
                cert_raw = f.read()
        snap = json.loads(raw.decode("utf-8-sig"))
        snap["_input_hash"] = hashlib.sha256(raw + cert_raw).hexdigest()
        res = apply(snap, cert_raw.decode("ascii", errors="ignore"), root=args.root,
                    restart=not args.no_restart)
        print(json.dumps(res))
        return 0
    except Refused as ex:
        print(json.dumps({"ok": False, "error": f"LanEx did not change the network settings: {ex}."}))
        return 1
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"LanEx could not apply the network settings: "
                                                f"{type(ex).__name__}: {ex}."}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
