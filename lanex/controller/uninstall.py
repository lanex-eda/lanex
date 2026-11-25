# Copyright 2026 LanEx Contributors
# Licensed under the Apache License, Version 2.0
"""Remove LanEx from a macOS or Linux computer: ``lanex --uninstall``.

This module is the one list of everything LanEx puts on a computer outside
the Python package itself. A tester who followed the old three-step manual
removal (2026-11-24) was left with 3.5 GB: the app window's browser profile,
the PDK download cache in ``~/.cache/lanex`` and the projects folder. Anything
new that LanEx writes outside its home directory belongs in :func:`footprint`;
``test_uninstall.py`` runs a simulated session and fails when something is
left behind.

What happens by default:

* removed: LanEx's home (``~/.lanex``: settings, logs, browser profile, tools
  it built, the venv of a venv-fallback install), its download cache, the
  desktop shortcut and a GDS3D link that points into LanEx's home;
* kept, and said where: the projects folder (your designs);
* asked about (or ``--remove-pdks`` / ``--remove-image`` / ``--all``): the PDKs
  (``~/.ciel`` is shared with any other LibreLane on the computer) and the
  LibreLane container image (about 10 GB).

The Python package, the ``lanex`` command link and the PATH line are removed by
``install.sh --uninstall``, which runs this first. The Windows app has its own
uninstaller (Start menu: Uninstall or remove LanEx); inside its environment
this refuses and points there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Never delete a folder whole unless its own name says what it is: LanEx's
# folders, and ciel's default PDK store. A PDK folder chosen elsewhere (say
# /data/pdks) is reported, never deleted whole.
_SAFE_NAMES = ("lanex", ".lanex", ".librelane-gui", "lanex-projects", ".ciel", "ciel")


def _size(path: Path) -> int:
    total = 0
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        for f in path.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n = n / 1024
    return f"{n} B"


def is_windows_appliance() -> bool:
    return Path("/etc/lanex/appliance.json").is_file()


def _safe_dir(path: Path) -> bool:
    """A folder this module may delete whole: named like LanEx's, not $HOME or /."""
    try:
        real = path.resolve()
    except OSError:
        return False
    home = Path.home().resolve()
    if real in (home, Path("/"), home.parent) or len(real.parts) < 3:
        return False
    return real.name.lower() in _SAFE_NAMES


def _home_dirs() -> List[Path]:
    from . import platform_env

    dirs = [platform_env.home()]
    legacy = Path.home() / ".librelane-gui"
    if legacy not in dirs and legacy.exists():
        dirs.append(legacy)
    return dirs


def _cache_dir() -> Path:
    from . import pdkfeed

    return pdkfeed.cache_dir("x", "y").parent.parent   # ~/.cache/lanex


def _gds3d_link(lanex_homes: List[Path]) -> Optional[Path]:
    link = Path.home() / ".local" / "bin" / "gds3d"
    if not link.is_symlink():
        return None
    try:
        target = Path(os.readlink(link))
    except OSError:
        return None
    if not target.is_absolute():
        target = link.parent / target
    for h in lanex_homes:
        try:
            target.resolve().relative_to(h.resolve())
            return link
        except (ValueError, OSError):
            continue
    return None


def footprint() -> List[Dict[str, Any]]:
    """Everything LanEx can have put on this computer, with its default action."""
    from . import platform_env, shortcut

    items: List[Dict[str, Any]] = []
    homes = _home_dirs()
    for h in homes:
        items.append({"key": "home", "path": str(h), "label": "LanEx settings, logs, app-window "
                      "profile and tools it built", "action": "remove"})
    items.append({"key": "cache", "path": str(_cache_dir()), "label": "LanEx download cache",
                  "action": "remove"})
    link = _gds3d_link(homes)
    if link:
        items.append({"key": "gds3d", "path": str(link), "label": "GDS3D command link",
                      "action": "remove"})
    for p in shortcut.existing_paths():
        items.append({"key": "shortcut", "path": str(p), "label": "desktop shortcut",
                      "action": "remove"})
    try:
        projects = platform_env.projects_dir()
    except Exception:
        projects = Path.home() / "lanex-projects"
    items.append({"key": "projects", "path": str(projects), "label": "your projects",
                  "action": "keep"})
    try:
        from .installer import ciel_home

        pdks = Path(ciel_home())
    except Exception:
        pdks = Path.home() / ".ciel"
    items.append({"key": "pdks", "path": str(pdks), "label": "PDKs (shared with any other "
                  "LibreLane on this computer)", "action": "ask"})
    items.append({"key": "image", "path": "", "label": "LibreLane container image",
                  "action": "ask"})
    return [i for i in items if i["key"] == "image" or Path(i["path"]).exists()
            or Path(i["path"]).is_symlink()]


def _images() -> List[Dict[str, str]]:
    """LibreLane images held by docker or podman: [{engine, ref, size}]."""
    found: List[Dict[str, str]] = []
    for engine in ("docker", "podman"):
        exe = shutil.which(engine)
        if not exe or exe.startswith("/mnt/"):
            continue
        try:
            out = subprocess.run(
                [exe, "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}",
                 "ghcr.io/librelane/librelane"],
                capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        if out.returncode != 0:
            continue
        for line in out.stdout.splitlines():
            ref, _, size = line.partition("\t")
            if ref.strip():
                found.append({"engine": engine, "ref": ref.strip(), "size": size.strip()})
    return found


def plan(*, remove_projects: bool = False, remove_pdks: bool = False,
         remove_image: bool = False) -> List[Dict[str, Any]]:
    """The footprint with sizes and the final decision for each item."""
    out = []
    for item in footprint():
        item = dict(item)
        if item["key"] == "image":
            imgs = _images()
            if not imgs:
                continue
            item["images"] = imgs
            item["size"] = ", ".join(i["size"] for i in imgs)
            item["remove"] = remove_image
        else:
            item["bytes"] = _size(Path(item["path"]))
            item["size"] = human(item["bytes"])
            item["remove"] = (item["action"] == "remove"
                              or (item["key"] == "projects" and remove_projects)
                              or (item["key"] == "pdks" and remove_pdks))
        out.append(item)
    return out


def _remove_path(path: Path) -> Optional[str]:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            if not _safe_dir(path):
                return (f"left {path}: its name does not say it is LanEx's, so it is not "
                        "deleted whole; remove it yourself if you are sure")
            shutil.rmtree(path)
        return None
    except OSError as ex:
        return f"could not remove {path}: {ex}"


def execute(items: List[Dict[str, Any]], log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Remove every item marked ``remove``. LanEx's home goes last: a
    venv-fallback install runs from inside it."""
    from . import shortcut

    errors: List[str] = []
    freed = 0
    ordered = sorted((i for i in items if i.get("remove")), key=lambda i: i["key"] == "home")
    for item in ordered:
        if item["key"] == "image":
            for img in item.get("images", []):
                exe = shutil.which(img["engine"]) or img["engine"]
                res = subprocess.run([exe, "rmi", img["ref"]], capture_output=True, text=True)
                if res.returncode == 0:
                    log(f"removed image {img['ref']} ({img['size']})")
                else:
                    errors.append(f"could not remove image {img['ref']}: "
                                  f"{(res.stderr or res.stdout).strip()[:200]}")
            continue
        if item["key"] == "shortcut":
            res = shortcut.remove()
            for p in res.get("paths") or []:
                log(f"removed {p}")
            continue
        err = _remove_path(Path(item["path"]))
        if err:
            errors.append(err)
        else:
            freed += int(item.get("bytes") or 0)
            log(f"removed {item['path']} ({item['size']})")
    return {"ok": not errors, "freed": freed, "errors": errors}


DECLINED = 3   # exit code: the person said no, nothing was removed


def _tty_in():
    """Where answers come from: stdin when it is a terminal, else /dev/tty
    (``curl ... | bash`` feeds the script itself on stdin), else None."""
    if sys.stdin is not None and sys.stdin.isatty():
        return sys.stdin
    try:
        return open("/dev/tty", "r", encoding="utf-8")
    except OSError:
        return None


def _ask(question: str, source) -> bool:
    sys.stdout.write(f"{question} [y/N] ")
    sys.stdout.flush()
    try:
        line = source.readline()
    except (OSError, ValueError):
        return False
    return line.strip().lower() in ("y", "yes")


def main(*, dry_run: bool = False, yes: bool = False, remove_projects: bool = False,
         remove_pdks: bool = False, remove_image: bool = False) -> int:
    """``lanex --uninstall``. Returns the process exit code."""
    w = sys.stdout.write
    if is_windows_appliance():
        w("This is LanEx's environment inside Windows. Remove LanEx from Windows instead:\n"
          "Start menu > LanEx (EDA) > Uninstall or remove LanEx.\n")
        return 2
    answers = None if (yes or dry_run) else _tty_in()
    interactive = answers is not None
    items = plan(remove_projects=remove_projects, remove_pdks=remove_pdks,
                 remove_image=remove_image)
    w("LanEx uninstall\n\n")
    for i in items:
        mark = "remove" if i["remove"] else ("ask" if i["action"] == "ask" and interactive else "keep")
        where = i["path"] or ", ".join(img["ref"] for img in i.get("images", []))
        w(f"  {mark:6}  {i['size']:>9}  {i['label']}: {where}\n")
    w("\nThe LanEx program itself and the 'lanex' command are removed by\n"
      "install.sh --uninstall (or: pipx uninstall lanex).\n")
    if dry_run:
        w("\nDry run: nothing was removed.\n")
        return 0
    if not yes and not interactive:
        w("\nNo terminal to ask in, so nothing was removed. Run it again with --yes.\n")
        return DECLINED
    if interactive:
        for i in items:
            if i["action"] == "ask" and not i["remove"]:
                i["remove"] = _ask(f"Also remove the {i['label']} ({i['size']})?", answers)
        projects = next((i for i in items if i["key"] == "projects"), None)
        if projects and not projects["remove"] and projects.get("bytes", 0) == 0:
            projects["remove"] = True      # an empty projects folder is not worth keeping
        if not _ask("\nRemove the items marked remove?", answers):
            w("Nothing was removed.\n")
            return DECLINED
    result = execute(items, log=lambda s: w(s + "\n"))
    kept = [i for i in items if not i["remove"]]
    w(f"\nFreed {human(result['freed'])}.\n")
    for i in kept:
        where = i["path"] or ", ".join(img["ref"] for img in i.get("images", []))
        w(f"Kept {i['label']}: {where}\n")
    for e in result["errors"]:
        w(f"!! {e}\n")
    return 0 if result["ok"] else 1
