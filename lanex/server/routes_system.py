# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""System routes for the Tools page (audit phase 5): the doctor, storage and
clean-up, per-variant PDK removal, WSL memory, the network the distro uses,
and "skip this version". Wired into ``routes.ROUTES``; each handler answers in
the house envelope (``{ok, data}`` / ``{ok: false, error}``).

Slow work (sizes, docker queries) never runs on a request thread for long:
storage is measured in the background and cached; the doctor's network part
has its own 12 s budget.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

_log = logging.getLogger("librelane.lanex.routes_system")

_doctor_cache: Dict[str, Any] = {"t": 0.0, "v": None, "offline": None}
_doctor_lock = threading.Lock()


def _r():
    from . import routes

    return routes


def _body(handler: Any) -> Dict[str, Any]:
    b = getattr(handler, "_body", {}) or {}
    return b if isinstance(b, dict) else {}


def h_doctor(handler: Any) -> None:
    """``lanex doctor`` for the app. ``?offline=1`` skips the network;
    ``?share=1&text=1`` is the plain-text share form "Copy diagnostics" uses.
    Answers within 30 s; a repeat within 20 s is served from the last report."""
    from ..controller import doctor

    r = _r()
    offline = r._query_param(handler.path, "offline") == "1"
    fresh = r._query_param(handler.path, "fresh") == "1"
    with _doctor_lock:
        c = _doctor_cache
        hit = (c["v"] is not None and not fresh and (time.time() - c["t"]) < 20
               and (c["offline"] == offline or not c["offline"]))
        if hit:
            report = c["v"]
        else:
            report = doctor.run(offline=offline, in_server=True)
            c.update(t=time.time(), v=report, offline=offline)
    if r._query_param(handler.path, "text") == "1":
        share = r._query_param(handler.path, "share") == "1"
        r._respond(handler, {"text": doctor.to_text(report, share=share, verbose=True),
                             "summary": report.get("summary")})
        return
    r._respond(handler, report)


def h_storage(handler: Any) -> None:
    from ..controller import syscleanup

    r = _r()
    r._respond(handler, syscleanup.storage(force=r._query_param(handler.path, "fresh") == "1"))


def h_cleanup(handler: Any) -> None:
    from ..controller import syscleanup

    r = _r()
    try:
        r._respond(handler, syscleanup.cleanup_items())
    except Exception as ex:  # noqa: BLE001
        _log.exception("cleanup_items failed")
        r._respond(handler, f"LanEx could not list what can be cleaned up: {ex}", 500)


def h_cleanup_image(handler: Any) -> None:
    from ..controller import syscleanup

    r = _r()
    image_id = str(_body(handler).get("id") or "")
    try:
        r._respond(handler, syscleanup.remove_image(image_id))
    except syscleanup.CleanupError as ex:
        r._respond(handler, str(ex), 409)
    except Exception as ex:  # noqa: BLE001
        _log.exception("remove_image failed")
        r._respond(handler, f"The image was not removed: {ex}", 500)


def h_pdk_inventory(handler: Any) -> None:
    from ..controller import syscleanup

    r = _r()
    r._respond(handler, syscleanup.pdk_inventory())


def h_pdk_remove_variant(handler: Any) -> None:
    from ..controller import syscleanup, tools

    r = _r()
    variant = str(_body(handler).get("variant") or "")
    try:
        res = syscleanup.remove_pdk_variant(variant)
        tools._check_tools_cache.clear()
        r._respond(handler, res)
    except syscleanup.CleanupError as ex:
        r._respond(handler, str(ex), 409)
    except Exception as ex:  # noqa: BLE001
        _log.exception("remove_pdk_variant failed")
        r._respond(handler, f"{variant} was not removed: {ex}", 500)


def h_pdk_remove_version(handler: Any) -> None:
    from ..controller import syscleanup, tools

    r = _r()
    b = _body(handler)
    try:
        res = syscleanup.remove_pdk_version(str(b.get("family") or ""), str(b.get("version") or ""))
        tools._check_tools_cache.clear()
        r._respond(handler, res)
    except syscleanup.CleanupError as ex:
        r._respond(handler, str(ex), 409)
    except Exception as ex:  # noqa: BLE001
        _log.exception("remove_pdk_version failed")
        r._respond(handler, f"The PDK version was not removed: {ex}", 500)


def h_wsl_memory(handler: Any) -> None:
    """GET: what .wslconfig says and what Linux has. POST {gb: int|null}:
    write ``[wsl2] memory=`` (null goes back to Windows' default)."""
    from ..controller import platform_env, wslconfig

    r = _r()
    if not platform_env.is_wsl():
        r._respond(handler, "WSL memory is a Windows setting; this LanEx is not running in WSL.", 400)
        return
    if handler.command == "POST":
        b = _body(handler)
        gb = b.get("gb")
        if gb is not None:
            try:
                gb = int(gb)
            except (TypeError, ValueError):
                r._respond(handler, "Memory must be a whole number of GB.", 400)
                return
        st = wslconfig.status()
        try:
            res = wslconfig.write_memory(gb, total_gb=st.get("windows_total_gb"))
        except wslconfig.WslConfigError as ex:
            r._respond(handler, str(ex), 409)
            return
        r._respond(handler, {**res, "status": wslconfig.status(total_gb=st.get("windows_total_gb"))})
        return
    r._respond(handler, wslconfig.status())


def h_network(handler: Any) -> None:
    """GET: the proxy and certificates LanEx's Linux uses. POST: ask Windows
    again and apply (the Tools "Check again" button)."""
    from ..controller import netsettings

    r = _r()
    if handler.command == "POST":
        ref = netsettings.refresh_windows_snapshot()
        if not ref.get("ok"):
            r._respond(handler, ref.get("error") or "Windows did not answer.", 409)
            return
        res = netsettings.apply()
        if not res.get("ok"):
            r._respond(handler, res.get("error") or "The network settings could not be applied.", 409)
            return
        r._respond(handler, {"applied": res, "status": netsettings.status()})
        return
    r._respond(handler, netsettings.status())


def h_update_skip(handler: Any) -> None:
    from ..controller import updatecheck

    r = _r()
    r._respond(handler, updatecheck.skip_version(str(_body(handler).get("version") or "")))


def h_power_hold(handler: Any) -> None:
    """Keep the PC awake? (The Windows launcher asks every 5 s, power.go.)

    A fallback for builds without the run engine's own endpoint (lane 1):
    hold while a flow run is going. routes.py adds it only when no other
    handler serves /api/power/hold."""
    r = _r()
    try:
        from .app import get_runner

        running = bool(getattr(get_runner(), "running", False))
    except Exception:
        running = False
    r._respond(handler, {"hold": running, "reason": "a chip design run is in progress" if running else ""})


ROUTES = [
    ("/api/doctor", h_doctor),
    ("/api/power/hold", h_power_hold),
    ("/api/system/storage", h_storage),
    ("/api/system/cleanup/image", h_cleanup_image),
    ("/api/system/cleanup", h_cleanup),
    ("/api/system/wsl-memory", h_wsl_memory),
    ("/api/system/network", h_network),
    ("/api/pdk/inventory", h_pdk_inventory),
    ("/api/pdk/remove-variant", h_pdk_remove_variant),
    ("/api/pdk/remove-version", h_pdk_remove_version),
    ("/api/update-check/skip", h_update_skip),
]
GET_ROUTES = {"/api/doctor", "/api/system/storage", "/api/system/cleanup", "/api/pdk/inventory",
              "/api/power/hold"}
POST_ROUTES = {"/api/system/cleanup/image", "/api/pdk/remove-variant", "/api/pdk/remove-version",
               "/api/update-check/skip"}
BOTH_ROUTES = {"/api/system/wsl-memory", "/api/system/network"}
