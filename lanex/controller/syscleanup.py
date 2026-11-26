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
"""Where the disk went, and getting it back (audit plan 5a).

- :func:`storage`: free space on the drive that matters (on Windows the drive
  holding LanEx's Linux disk, not WSL's virtual 1 TB) and what LanEx uses, by
  part. Measured on a background thread and cached, never on a request thread.
- :func:`cleanup_items`: what can be reclaimed: LibreLane images other than the
  one in use (Docker's own ``UniqueSize``) and PDK versions LibreLane no longer
  uses.
- :func:`remove_image`: one image, by ID: ``docker image rm`` of each of its
  tags and digests. Never ``-f``, never ``prune``: those would also take the
  user's other images.
- :func:`remove_pdk_variant` / :func:`remove_pdk_version`: per variant and per
  version, where removing sky130A used to take sky130B with it.

ciel facts this relies on (ciel 2.6.1): a version lives in
``<home>/ciel/<family>/versions/<hash>/<variant>/``; ``<home>/<variant>`` is a
relative symlink into it; ``<home>/ciel/<family>/current`` names the enabled
hash; ciel's own ``rm`` removes a whole version. ciel decides that the shared
files are missing only from ``variants[0]``, so a variant LanEx deletes is
recorded in ``<hash>.lanex-variants.json`` and reinstalled through a staging
fetch (:mod:`installer`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LIBRELANE_REPO = "ghcr.io/librelane/librelane"
REMOVING_SUFFIX = ".lanex-removing"
VARIANTS_FILE_SUFFIX = ".lanex-variants.json"
CACHE_TTL_S = 600.0
FAMILY_VARIANTS = {
    "sky130": ["sky130A", "sky130B"],
    "gf180mcu": ["gf180mcuA", "gf180mcuB", "gf180mcuC", "gf180mcuD"],
    "ihp-sg13g2": ["ihp-sg13g2"],
}


class CleanupError(Exception):
    """Refused or failed, with words for the user; nothing (more) was removed."""


# ---------------------------------------------------------------- sizes

_SIZE_RX = re.compile(r"^\s*([\d.]+)\s*([kKMGTP]?B)\s*$")
_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}


def parse_docker_size(text: str) -> Optional[int]:
    """Docker's ``7.524GB`` (1000-based) in bytes; None for ``N/A``."""
    m = _SIZE_RX.match(str(text or ""))
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS.get(m.group(2), 1))


def tree_bytes(path: Path, *, deadline: Optional[float] = None) -> Optional[int]:
    """Bytes under *path*, symlinks not followed. None if the deadline passed."""
    total = 0
    stack = [str(path)]
    while stack:
        if deadline and time.time() > deadline:
            return None
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


# ---------------------------------------------------------------- engine

def _engine() -> Tuple[Optional[str], bool]:
    try:
        from . import tools

        r = tools.resolve_engine()
        return (r.get("engine") if r.get("ready") else None), bool(r.get("sg_wrap"))
    except Exception:
        return None, False


def _engine_argv(engine: str, sg: bool, args: List[str]) -> List[str]:
    if sg:
        from . import tools

        return tools.sg_wrap_argv([engine] + args)
    return [engine] + args


def _run(argv: List[str], timeout: float = 30.0) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {timeout:g} s"
    except Exception as ex:  # noqa: BLE001
        return 1, "", str(ex)


def librelane_images() -> Dict[str, Any]:
    """LibreLane images in the engine: ``{ok, images: [...], current_id, error}``.

    Each image: ``{id, short, repository, tags, digests, version, size_bytes,
    unique_bytes, containers, current}``."""
    engine, sg = _engine()
    out: Dict[str, Any] = {"ok": False, "engine": engine, "images": [], "current_id": "", "error": ""}
    if not engine:
        out["error"] = "The container engine is not answering."
        return out
    from .container_run import image_ref

    current_ref = image_ref()
    rc, cur, _ = _run(_engine_argv(engine, sg, ["image", "inspect", current_ref, "--format", "{{.Id}}"]), 15)
    current_id = cur.strip() if rc == 0 else ""
    out["current_id"] = current_id
    out["current_ref"] = current_ref
    sizes: Dict[str, Dict[str, Any]] = {}
    if engine == "docker":
        rc, txt, err = _run(_engine_argv(engine, sg, ["system", "df", "-v", "--format", "json"]), 60)
        if rc == 0:
            try:
                for i in json.loads(txt or "{}").get("Images") or []:
                    sizes[str(i.get("ID") or "")] = i
            except Exception:
                pass
    fmt = "{{.Id}}\t{{json .RepoTags}}\t{{json .RepoDigests}}\t{{.Size}}\t{{.Created}}"
    rc, txt, err = _run(_engine_argv(engine, sg, ["image", "ls", "-q", "--no-trunc", "--filter",
                                                  f"reference={LIBRELANE_REPO}*"]), 30)
    if rc != 0:
        out["error"] = (err or txt).strip()[:300] or "the engine did not list its images"
        return out
    ids = []
    for line in txt.split():
        if line and line not in ids:
            ids.append(line)
    if current_id and current_id not in ids:
        ids.append(current_id)
    images = []
    for iid in ids:
        rc, info, _ = _run(_engine_argv(engine, sg, ["image", "inspect", iid, "--format", fmt]), 15)
        if rc != 0 or not info.strip():
            continue
        parts = info.strip().split("\t")
        try:
            tags = [t for t in json.loads(parts[1] or "[]") or [] if "@" not in t]
            digests = list(json.loads(parts[2] or "[]") or [])
        except Exception:
            tags, digests = [], []
        size = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        df = sizes.get(iid) or {}
        unique = parse_docker_size(df.get("UniqueSize")) if df else None
        containers = str(df.get("Containers") or "0") if df else "0"
        version = ""
        for t in tags:
            m = re.search(r":([0-9][\w.\-]*)$", t)
            if m:
                version = m.group(1)
                break
        if not version:
            version = _version_from_records(iid, digests)
        images.append({
            "id": iid, "short": iid.replace("sha256:", "")[:12], "repository": LIBRELANE_REPO,
            "tags": tags, "digests": digests, "version": version, "size_bytes": size,
            "unique_bytes": unique if unique is not None else size,
            "containers": 0 if containers in ("", "0", "N/A") else int(containers) if containers.isdigit() else 1,
            "current": iid == current_id,
        })
    out["images"] = images
    out["ok"] = True
    return out


def _version_from_records(iid: str, digests: List[str]) -> str:
    """The LibreLane version an untagged image was pulled for (Setup pulls by digest)."""
    try:
        from . import platform_env

        for name in ("runtime.json", "image.lock"):
            p = platform_env.home() / name
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                dg = str(d.get("digest") or "")
                if dg and (dg in iid or any(dg in x for x in digests)):
                    m = re.search(r":([0-9][\w.\-]*)$", str(d.get("image") or ""))
                    return m.group(1) if m else ""
    except Exception:
        pass
    return ""


def _containers_using(engine: str, sg: bool, iid: str) -> List[str]:
    rc, txt, _ = _run(_engine_argv(engine, sg, ["ps", "-a", "-q", "--filter", f"ancestor={iid}"]), 15)
    return [x for x in txt.split()] if rc == 0 else []


def remove_image(image_id: str) -> Dict[str, Any]:
    """Remove one old LibreLane image by ID. Refuses the image in use and any
    image a container still uses. Success means ``image inspect`` now fails."""
    iid = str(image_id or "").strip()
    if not re.match(r"^(sha256:)?[0-9a-f]{12,64}$", iid):
        raise CleanupError("That is not an image ID LanEx recognises.")
    try:
        from . import installer

        if installer.is_in_progress("container:image"):
            raise CleanupError("An image download is running. Wait for it to finish, then try again.")
    except CleanupError:
        raise
    except Exception:
        pass
    inv = librelane_images()
    if not inv.get("ok"):
        raise CleanupError(inv.get("error") or "The container engine is not answering.")
    img = next((i for i in inv["images"] if i["id"] == iid or i["id"].endswith(iid) or i["short"] == iid), None)
    if not img:
        raise CleanupError("That image is not a LibreLane image in LanEx's engine (it may be gone already).")
    if img["current"]:
        raise CleanupError("LanEx runs with this image, so it was not removed.")
    engine, sg = _engine()
    if not engine:
        raise CleanupError("The container engine is not answering.")
    users = _containers_using(engine, sg, img["id"])
    if users:
        raise CleanupError(f"{len(users)} container(s) still use this image, so it was not removed. "
                           "Close any viewer or run that uses it, then try again.")
    before = storage_target_free()
    errors = []
    for ref in list(img["tags"]) + list(img["digests"]):
        rc, out, err = _run(_engine_argv(engine, sg, ["image", "rm", ref]), 120)
        if rc != 0 and "No such image" not in (err + out):
            errors.append((err or out).strip()[:200])
    rc, _o, _e = _run(_engine_argv(engine, sg, ["image", "inspect", img["id"]]), 15)
    if rc == 0:
        raise CleanupError("Docker kept the image" + (f": {errors[0]}" if errors else ".") +
                           " Nothing else was removed.")
    _invalidate()
    after = storage_target_free()
    freed = (after - before) if (after is not None and before is not None and after > before) else None
    return {"ok": True, "removed": img, "freed_bytes": freed if freed else img.get("unique_bytes")}


# ---------------------------------------------------------------- PDKs

def _ciel_homes() -> List[Path]:
    try:
        from . import pdk as pdk_mod

        return [Path(h) for h in pdk_mod._ciel_homes()]
    except Exception:
        return [Path.home() / ".ciel"]


def _pinned(family: str) -> str:
    try:
        from . import pdk as pdk_mod

        return pdk_mod.required_pdk_version(family) or ""
    except Exception:
        return ""


def _enabled_hash(home: Path, family: str) -> str:
    try:
        return (home / "ciel" / family / "current").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _size_cache_path() -> Path:
    from . import platform_env

    return platform_env.home() / "cache" / "pdk-sizes.json"


_size_lock = threading.Lock()
_size_measuring: Dict[str, bool] = {}


def _cached_size(path: Path) -> Optional[int]:
    """Bytes of a PDK tree, from the cache (keyed by path and mtime); measured in
    the background on a miss, so the first answer may be None ("measuring")."""
    try:
        key = str(path)
        mt = path.stat().st_mtime
    except OSError:
        return None
    try:
        cache = json.loads(_size_cache_path().read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("mtime") == mt:
        return hit.get("bytes")
    with _size_lock:
        if _size_measuring.get(key):
            return None
        _size_measuring[key] = True

    def _measure() -> None:
        try:
            n = tree_bytes(path, deadline=time.time() + 120)
            if n is None:
                return
            with _size_lock:
                try:
                    c = json.loads(_size_cache_path().read_text(encoding="utf-8"))
                except Exception:
                    c = {}
                c[key] = {"mtime": mt, "bytes": n}
                p = _size_cache_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                from . import platform_env

                platform_env.atomic_write_text(p, json.dumps(c))
        finally:
            with _size_lock:
                _size_measuring.pop(key, None)

    threading.Thread(target=_measure, name="lanex-pdk-size", daemon=True).start()
    return None


def removed_variants(vdir: Path) -> List[str]:
    try:
        d = json.loads(Path(str(vdir) + VARIANTS_FILE_SUFFIX).read_text(encoding="utf-8"))
        return [str(x) for x in d.get("removed") or []]
    except Exception:
        return []


def _write_removed(vdir: Path, removed: List[str]) -> None:
    p = Path(str(vdir) + VARIANTS_FILE_SUFFIX)
    if not removed:
        try:
            p.unlink()
        except OSError:
            pass
        return
    from . import platform_env

    platform_env.atomic_write_text(p, json.dumps({"removed": sorted(set(removed))}) + "\n")


def pdk_inventory() -> Dict[str, Any]:
    """Every installed ciel version, per family, with variants and sizes."""
    families: Dict[str, Dict[str, Any]] = {}
    for home in _ciel_homes():
        store = home / "ciel"
        if not store.is_dir():
            continue
        for family, vlist in FAMILY_VARIANTS.items():
            vroot = store / family / "versions"
            if not vroot.is_dir():
                continue
            fam = families.setdefault(family, {"family": family, "pinned": _pinned(family), "versions": []})
            enabled = _enabled_hash(home, family)
            for vdir in sorted(vroot.iterdir()):
                if not vdir.is_dir() or vdir.name.startswith(".") or vdir.name.endswith(REMOVING_SUFFIX):
                    continue
                variants = []
                for v in vlist:
                    sub = vdir / v
                    if not sub.is_dir():
                        continue
                    link = home / v
                    on = False
                    try:
                        on = link.is_symlink() and link.resolve() == sub.resolve()
                    except OSError:
                        on = False
                    variants.append({"variant": v, "enabled": on, "path": str(sub),
                                     "bytes": _cached_size(sub),
                                     "complete": (sub / "SOURCES").is_file()})
                try:
                    when = vdir.stat().st_mtime
                except OSError:
                    when = None
                fam["versions"].append({
                    "version": vdir.name, "home": str(home), "path": str(vdir),
                    "enabled": vdir.name == enabled, "pinned": vdir.name == fam["pinned"],
                    "variants": variants, "removed": removed_variants(vdir), "installed_at": when,
                    "incomplete": Path(str(vdir) + ".lanex-incomplete").exists(),
                })
    return {"families": list(families.values())}


def _run_active() -> bool:
    try:
        from ..server.app import get_runner

        r = get_runner()
        return bool(getattr(r, "running", False))
    except Exception:
        return False


def _shared_links_into(target: Path, siblings: List[Path]) -> List[str]:
    """Paths in *siblings* that are symlinks resolving into *target* (``ciel optimize``)."""
    hits: List[str] = []
    tgt = str(target.resolve())
    for sib in siblings:
        stack = [str(sib)]
        n = 0
        while stack and len(hits) < 3:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        n += 1
                        if e.is_symlink():
                            try:
                                if os.path.realpath(e.path).startswith(tgt + os.sep):
                                    hits.append(e.path)
                            except OSError:
                                pass
                        elif e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
            except OSError:
                continue
    return hits


def remove_pdk_variant(variant: str) -> Dict[str, Any]:
    """Remove one variant of the enabled version, keeping the others.

    Rename first (instant, atomic: the variant disappears from every check at
    once), then delete in the background. The last variant left goes through
    ``ciel rm`` so ciel's own ``current`` record is cleared too."""
    family = next((f for f, vs in FAMILY_VARIANTS.items() if variant in vs), None)
    if not family:
        raise CleanupError(f"{variant} is not a PDK LanEx manages.")
    if _run_active():
        raise CleanupError("A run is going. Wait for it to finish, or stop it, then remove the PDK.")
    from . import installer

    if not installer._begin_pdk_family(family):
        raise CleanupError(f"A {family} install or removal is already running. Wait for it to finish.")
    try:
        for home in _ciel_homes():
            link = home / variant
            if not link.is_symlink():
                continue
            target = link.resolve()
            vdir = target.parent
            if vdir.parent.name != "versions" or not str(target).startswith(str((home / "ciel").resolve())):
                raise CleanupError(f"{variant} is not a ciel install, so LanEx won't delete it. "
                                   f"Remove {link} yourself if you want it gone.")
            others = [vdir / v for v in FAMILY_VARIANTS[family] if v != variant and (vdir / v).is_dir()]
            shared = _shared_links_into(target, others)
            if shared:
                names = sorted({Path(s).relative_to(vdir).parts[0] for s in shared})
                raise CleanupError(f"{', '.join(names)} uses files stored in {variant} (ciel optimize linked them), "
                                   f"so {variant} can't be removed on its own. Remove the whole version, or keep both.")
            size = _cached_size(target)
            if not others:
                # The last variant of this version: ciel's own rm, for this version
                # only (it also clears ciel's "current" record).
                _ciel_rm(home, family, vdir.name)
                _write_removed(vdir, [])
                _invalidate()
                return {"ok": True, "variant": variant, "family": family, "version": vdir.name,
                        "freed_bytes": size, "whole_version": True}
            link.unlink()
            gone = vdir / ("." + variant + REMOVING_SUFFIX)
            if gone.exists():
                shutil.rmtree(gone, ignore_errors=True)
            os.rename(target, gone)
            _write_removed(vdir, removed_variants(vdir) + [variant])
            threading.Thread(target=shutil.rmtree, args=(str(gone),), kwargs={"ignore_errors": True},
                             name="lanex-pdk-remove", daemon=True).start()
            _invalidate()
            return {"ok": True, "variant": variant, "family": family, "version": vdir.name,
                    "freed_bytes": size, "whole_version": False,
                    "kept": [p.name for p in others]}
        raise CleanupError(f"{variant} is not installed.")
    finally:
        installer._end_pdk_family(family)


def _ciel_rm(home: Path, family: str, version: str) -> None:
    from . import installer

    ciel_argv = installer._ciel_argv()
    if ciel_argv is None:
        raise CleanupError("ciel (the PDK manager) is missing, so the version can't be removed.")
    vdir = home / "ciel" / family / "versions" / version
    res = installer._run_argv(ciel_argv + ["rm", "--pdk-root", str(home), "--pdk-family", family,
                                            version, "--yes"], label="ciel rm", key=f"pdk:{family}")
    if vdir.is_dir():
        raise CleanupError(f"ciel could not remove version {version[:7]}" +
                           (f" (exit {res.get('rc')})" if res.get("rc") is not None else "") + ".")


def adopt_staged_variant(staging: Path, root: Path, family: str, version: str, variant: str) -> None:
    """Move *variant* fetched into *staging* next to the installed version and
    enable it, then delete the staging root. Raises on any step that failed."""
    src = staging / "ciel" / family / "versions" / version / variant
    vdir = root / "ciel" / family / "versions" / version
    dest = vdir / variant
    if not (src / "libs.ref").is_dir():
        raise CleanupError(f"the download for {variant} has no libraries")
    if dest.exists():
        shutil.rmtree(dest)
    os.rename(src, dest)
    link = root / variant
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(os.path.relpath(dest, root), link)
    _write_removed(vdir, [v for v in removed_variants(vdir) if v != variant])
    shutil.rmtree(staging, ignore_errors=True)
    _invalidate()


def remove_pdk_version(family: str, version: str) -> Dict[str, Any]:
    """Remove one old ciel version (never the enabled or the pinned one)."""
    if family not in FAMILY_VARIANTS or not re.match(r"^[0-9a-f]{7,64}$", str(version or "")):
        raise CleanupError("That is not a PDK version LanEx knows.")
    if version == _pinned(family):
        raise CleanupError(f"LibreLane uses version {version[:7]}, so it stays.")
    from . import installer

    ciel_argv = installer._ciel_argv()
    if ciel_argv is None:
        raise CleanupError("ciel (the PDK manager) is missing, so the version can't be removed.")
    if not installer._begin_pdk_family(family):
        raise CleanupError(f"A {family} install or removal is already running. Wait for it to finish.")
    try:
        for home in _ciel_homes():
            vdir = home / "ciel" / family / "versions" / version
            if not vdir.is_dir():
                continue
            if _enabled_hash(home, family) == version:
                raise CleanupError(f"Version {version[:7]} is the one in use, so it stays.")
            size = _cached_size(vdir)
            res = installer._run_argv(ciel_argv + ["rm", "--pdk-root", str(home), "--pdk-family", family,
                                                    version, "--yes"], label="ciel rm", key=f"pdk:{family}")
            if vdir.is_dir():
                raise CleanupError(f"ciel could not remove version {version[:7]}" +
                                   (f" (exit {res.get('rc')})" if res.get("rc") is not None else "") + ".")
            _write_removed(vdir, [])
            _invalidate()
            return {"ok": True, "family": family, "version": version, "freed_bytes": size}
        raise CleanupError(f"Version {version[:7]} of {family} is not installed.")
    finally:
        installer._end_pdk_family(family)


def sweep_leftovers() -> int:
    """Delete ``.<variant>.lanex-removing`` folders a crash left behind (at start)."""
    n = 0
    for home in _ciel_homes():
        for vroot in (home / "ciel").glob("*/versions/*"):
            try:
                for child in vroot.iterdir():
                    if child.name.startswith(".") and child.name.endswith(REMOVING_SUFFIX):
                        threading.Thread(target=shutil.rmtree, args=(str(child),),
                                         kwargs={"ignore_errors": True}, daemon=True).start()
                        n += 1
            except OSError:
                continue
    return n


# ---------------------------------------------------------------- clean-up list

def cleanup_items() -> Dict[str, Any]:
    """Everything that can be reclaimed, largest first."""
    items: List[Dict[str, Any]] = []
    imgs = librelane_images()
    for i in imgs.get("images") or []:
        if i["current"]:
            continue
        items.append({
            "kind": "image", "id": i["id"], "short": i["short"], "version": i["version"],
            "name": f"LibreLane {i['version']} image" if i["version"] else f"Untagged LibreLane image ({i['short']})",
            "ref": (i["tags"] or i["digests"] or [i["id"]])[0], "bytes": i["unique_bytes"],
            "in_use": i["containers"] > 0,
        })
    inv = pdk_inventory()
    for fam in inv["families"]:
        for v in fam["versions"]:
            if v["enabled"] or v["pinned"]:
                continue
            sizes = [x["bytes"] for x in v["variants"]]
            total = sum(s for s in sizes if s) if all(s is not None for s in sizes) else None
            items.append({
                "kind": "pdk-version", "family": fam["family"], "version": v["version"],
                "name": f"Old {fam['family']} version {v['version'][:7]}",
                "variants": [x["variant"] for x in v["variants"]], "bytes": total,
                "installed_at": v["installed_at"],
            })
    items.sort(key=lambda x: -(x.get("bytes") or 0))
    known = [x["bytes"] for x in items if x.get("bytes")]
    return {"items": items, "total_bytes": sum(known) if known else 0,
            "current_image": imgs.get("current_ref") or "", "engine": imgs.get("engine"),
            "engine_error": imgs.get("error") or ""}


# ---------------------------------------------------------------- storage

_storage_cache: Dict[str, Any] = {"t": 0.0, "v": None, "measuring": False}
_storage_lock = threading.Lock()


def _invalidate() -> None:
    with _storage_lock:
        _storage_cache["t"] = 0.0


def storage_target_free() -> Optional[int]:
    try:
        from .doctor import storage_target

        t = storage_target()
        return shutil.disk_usage(t["path"]).free
    except Exception:
        return None


def _measure_storage() -> Dict[str, Any]:
    from . import platform_env
    from .doctor import storage_target

    tgt = storage_target()
    try:
        u = shutil.disk_usage(tgt["path"])
        free, total = u.free, u.total
    except Exception:
        free = total = None
    parts: Dict[str, Optional[int]] = {"image": None, "pdks": None, "runs": None, "other": None}
    imgs = librelane_images()
    if imgs.get("ok"):
        parts["image"] = sum(int(i.get("unique_bytes") or i.get("size_bytes") or 0) for i in imgs["images"])
    pdk_total = 0
    pdk_known = True
    for home in _ciel_homes():
        store = home / "ciel"
        if store.is_dir():
            n = tree_bytes(store, deadline=time.time() + 90)
            if n is None:
                pdk_known = False
            else:
                pdk_total += n
    parts["pdks"] = pdk_total if pdk_known else None
    runs = 0
    runs_known = True
    try:
        from . import designs

        for d in designs.list_designs()[:200]:
            rd = Path(d) / "runs"
            if rd.is_dir():
                n = tree_bytes(rd, deadline=time.time() + 30)
                if n is None:
                    runs_known = False
                    break
                runs += n
    except Exception:
        runs_known = False
    parts["runs"] = runs if runs_known else None
    try:
        parts["other"] = tree_bytes(platform_env.home(), deadline=time.time() + 30)
    except Exception:
        parts["other"] = None
    return {"drive": tgt.get("label"), "windows": bool(tgt.get("windows")), "free_bytes": free,
            "total_bytes": total, "parts": parts, "measured_at": time.time(),
            "note": ("Space freed inside LanEx's Linux disk stays reserved on the Windows drive "
                     "(WSL disks only grow).") if tgt.get("windows") else ""}


def storage(force: bool = False) -> Dict[str, Any]:
    """The cached storage picture, measured in the background when stale.

    Free space is always read now (cheap); the parts may be ``measuring``."""
    with _storage_lock:
        v = _storage_cache["v"]
        fresh = v is not None and (time.time() - _storage_cache["t"]) < CACHE_TTL_S and not force
        start = not fresh and not _storage_cache["measuring"]
        if start:
            _storage_cache["measuring"] = True

    def _go() -> None:
        try:
            res = _measure_storage()
            with _storage_lock:
                _storage_cache.update(v=res, t=time.time())
        finally:
            with _storage_lock:
                _storage_cache["measuring"] = False

    if start:
        threading.Thread(target=_go, name="lanex-storage", daemon=True).start()
    from .doctor import storage_target

    tgt = storage_target()
    try:
        u = shutil.disk_usage(tgt["path"])
        free, total = u.free, u.total
    except Exception:
        free = total = None
    out = dict(v or {"parts": {"image": None, "pdks": None, "runs": None, "other": None}})
    out.update(drive=tgt.get("label"), windows=bool(tgt.get("windows")), free_bytes=free, total_bytes=total,
               measuring=bool(_storage_cache["measuring"]))
    return out
