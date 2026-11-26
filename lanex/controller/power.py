# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Keep the PC awake while runs go or wait (audit 2b).

The scheduler (``jobs.py``) says whether a hold is wanted; this module takes
or releases it on the machine the server runs on:

* **macOS**: ``caffeinate -i -w <server pid>``. ``-i`` stops idle sleep, on
  battery too (``-s`` works on mains power only); ``-w`` ends the assertion
  if the server dies, so a crash never leaves the Mac pinned awake.
* **Linux** (not WSL), with logind: ``systemd-inhibit --what=sleep:idle
  --who=LanEx --why=… --mode=block tail --pid=<server pid> -f /dev/null``.
  ``tail --pid`` ties the lock to the server's life. On GNOME the session's
  own idle suspend reads gnome-session's inhibitors, not logind's, so
  ``gnome-session-inhibit --inhibit suspend:idle`` is taken as well when it
  is there. ``systemd-inhibit`` exits at once without a system bus; that is
  noticed and reported, never read as held.
* **WSL** (Windows): a lock inside WSL does nothing for Windows
  (microsoft/WSL#42121), so nothing is run here. The Windows launcher polls
  ``GET /api/power/hold`` and holds Windows awake with
  ``PowerSetRequest(PowerRequestSystemRequired)`` (lane 6).

Closing the lid or pressing the power button still sleeps the PC: no API
stops that, and the words on screen say so.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_log = logging.getLogger("librelane.gui.power")

_lock = threading.Lock()
_procs: List[subprocess.Popen] = []
_state: Dict[str, Any] = {"hold": False, "reason": "", "since": None, "jobs": 0,
                          "how": "", "held_here": False, "note": ""}
_ENABLED_KEY = "keep_awake"


def is_wsl() -> bool:
    try:
        from . import platform_env
        if hasattr(platform_env, "is_wsl"):
            return bool(platform_env.is_wsl())
    except Exception:
        pass
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except Exception:
        return False


def enabled() -> bool:
    """The machine-wide switch (Settings, the run menu): on unless turned off."""
    try:
        from . import jobs
        return jobs.read_setting(_ENABLED_KEY, True) is not False
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    from . import jobs
    jobs.write_setting(_ENABLED_KEY, None if on else False)


def plan(platform: Optional[str] = None, *, wsl: Optional[bool] = None, pid: Optional[int] = None,
         why: str = "A chip flow is running", which: Any = shutil.which,
         desktop: Optional[str] = None) -> Dict[str, Any]:
    """The commands that hold this machine awake: ``{how, argvs, note}``.
    Pure (the platform, PATH lookup and desktop are passed in for tests)."""
    platform = platform or sys.platform
    pid = int(pid or os.getpid())
    wsl = is_wsl() if wsl is None else wsl
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") if desktop is None else desktop
    if platform == "darwin":
        if which("caffeinate"):
            return {"how": "caffeinate", "argvs": [["caffeinate", "-i", "-w", str(pid)]], "note": ""}
        return {"how": "", "argvs": [], "note": "This Mac can't be held awake from here (caffeinate is missing)."}
    if platform.startswith("linux"):
        if wsl:
            return {"how": "windows-launcher", "argvs": [],
                    "note": "Windows is held awake by LanEx's launcher while it is open."}
        argvs: List[List[str]] = []
        tail = ["tail", f"--pid={pid}", "-f", "/dev/null"]
        if which("systemd-inhibit"):
            argvs.append(["systemd-inhibit", "--what=sleep:idle", "--who=LanEx", f"--why={why}",
                          "--mode=block", *tail])
        elif which("elogind-inhibit"):
            argvs.append(["elogind-inhibit", "--what=sleep:idle", "--who=LanEx", f"--why={why}",
                          "--mode=block", *tail])
        if "gnome" in desktop.lower() and which("gnome-session-inhibit"):
            argvs.append(["gnome-session-inhibit", "--inhibit", "suspend:idle", "--reason", why, *tail])
        if not argvs:
            return {"how": "", "argvs": [], "note": "This PC can't be held awake from here (no power manager found)."}
        return {"how": "+".join(a[0] for a in argvs), "argvs": argvs, "note": ""}
    if platform.startswith("win"):
        return {"how": "windows-launcher", "argvs": [], "note": ""}
    return {"how": "", "argvs": [], "note": "This system can't be held awake from here."}


def _release_locked() -> None:
    for p in _procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in _procs:
        try:
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _procs.clear()


def update(want: bool, reason: str = "", jobs: int = 0) -> Dict[str, Any]:
    """Take or release the hold. Cheap to call every second: it acts only
    on a change (or when a holder process died)."""
    want = bool(want) and enabled()
    with _lock:
        if want:
            alive = [p for p in _procs if p.poll() is None]
            if not _state["hold"] or (_state.get("held_here") and len(alive) != len(_procs)):
                _release_locked()
                pl = plan(why=("LanEx: " + reason) if reason else "LanEx: a chip flow is running")
                started = 0
                for argv in pl["argvs"]:
                    try:
                        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, start_new_session=True)
                        _procs.append(p)
                        started += 1
                    except Exception:
                        _log.debug("could not start %s", argv[0], exc_info=True)
                note = pl["note"]
                if pl["argvs"]:
                    time.sleep(0.15)
                    alive = [p for p in _procs if p.poll() is None]
                    if not alive:
                        note = "This PC can't be held awake from here (the power manager refused)."
                _state.update({"how": pl["how"], "held_here": bool([p for p in _procs if p.poll() is None]),
                               "note": note})
                if not _state["hold"]:
                    _state["since"] = _iso()
            _state.update({"hold": True, "reason": reason, "jobs": int(jobs)})
        else:
            if _state["hold"]:
                _release_locked()
            _state.update({"hold": False, "reason": "", "since": None, "jobs": 0, "held_here": False})
            if not enabled():
                _state["note"] = "Keeping the PC awake is turned off."
            else:
                _state["note"] = ""
        return dict(_state)


_last_poll = [0.0]
POLL_FRESH_S = 30.0


def note_poll() -> None:
    """The Windows launcher asked ``/api/power/hold`` (it holds Windows awake)."""
    _last_poll[0] = time.monotonic()


def launcher_polling() -> bool:
    return _last_poll[0] > 0 and time.monotonic() - _last_poll[0] < POLL_FRESH_S


def state() -> Dict[str, Any]:
    """The hold as the page shows it. ``held`` says whether something really
    holds the PC: a process here, or the Windows launcher polling."""
    with _lock:
        out = dict(_state)
    out["enabled"] = enabled()
    if out.get("how") == "windows-launcher" or (not out.get("how") and is_wsl()):
        out["how"] = "windows-launcher"
        out["held"] = bool(out.get("hold")) and launcher_polling()
        if out.get("hold") and not out["held"]:
            out["note"] = "Windows isn't being held awake: LanEx's own window (the launcher) holds it while it is open."
    else:
        out["held"] = bool(out.get("hold")) and bool(out.get("held_here"))
    return out


def _iso() -> str:
    try:
        from . import platform_env
        return platform_env.local_now().isoformat(timespec="seconds")
    except Exception:  # pragma: no cover
        return time.strftime("%Y-%m-%dT%H:%M:%S")


def release_all() -> None:
    with _lock:
        _release_locked()
        _state.update({"hold": False, "held_here": False})
