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
"""Is there a newer LanEx release? (audit plan 5a)

An announced, opt-out check: the Settings page says what it does and has the
switch ("Check for updates", on by default). While it is on, the browser asks
this module once per page load; this module goes online at most once a day.

What goes over the network: one plain HTTPS GET of GitHub's public
``releases/latest`` for the LanEx repository, with a generic User-Agent
(``LanEx-update-check``) and nothing else: no version, no machine, no project,
no identifier. GitHub sees the request's IP address, as with any web request.
The answer is cached in ``$LANEX_HOME/cache/update-check.json``. Offline, or
when GitHub answers badly, the check fails silently and the last good answer
stays.

Version comparison handles ``v1.0.0`` tags, ``1.1.0-beta.2`` pre-releases
and a development copy (``1.0.1.dev3+g1234``: older than ``1.0.1``). A copy
that does not know its own version (``0.0.0+unknown``, a bare checkout) never
shows the banner.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO = "lanex-eda/lanex"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
USER_AGENT = "LanEx-update-check"
INTERVAL_S = 24 * 3600
TIMEOUT_S = 6.0
_MAX_BODY = 512 * 1024

_lock = threading.Lock()


# ---------------------------------------------------------------- versions

_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "preview": 3,
             "c": 3, "rc": 3}


def parse_version(text: Any) -> Optional[Tuple[Any, ...]]:
    """A comparable key for a version or release tag, or None.

    ``v1.2`` == ``1.2.0``; ``1.2.0-rc.1`` < ``1.2.0``; ``1.2.0.dev4`` <
    ``1.2.0-alpha``; ``1.2.0.post1`` > ``1.2.0``; a ``+local`` part is ignored.
    """
    s = str(text or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    s = s.split("+", 1)[0]
    m = re.match(r"^(\d+(?:\.\d+){0,3})(.*)$", s)
    if not m:
        return None
    core = [int(x) for x in m.group(1).split(".")]
    while len(core) < 3:
        core.append(0)
    rest = m.group(2).strip().lstrip("-._").lower()
    if not rest:
        return (tuple(core[:4]), 1, (0, 0))
    pm = re.match(r"^(dev|alpha|beta|preview|pre|rc|a|b|c|post)[.\-_]?(\d*)", rest)
    if not pm:
        return None
    word, num = pm.group(1), int(pm.group(2) or 0)
    if word == "post":
        return (tuple(core[:4]), 2, (num, 0))
    return (tuple(core[:4]), 0, (_PRE_RANK[word], num))


def is_newer(latest: Any, current: Any) -> bool:
    """True when release *latest* is newer than the running *current*."""
    if "+unknown" in str(current or "") or str(current or "").startswith("0.0.0"):
        return False   # a copy that doesn't know its version: say nothing
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def current_version() -> str:
    try:
        from .._version import get_version

        return get_version()
    except Exception:
        return "0.0.0+unknown"


# ---------------------------------------------------------------- cache

def _cache_path() -> Path:
    from . import platform_env

    return platform_env.home() / "cache" / "update-check.json"


def _read_cache() -> Dict[str, Any]:
    try:
        p = _cache_path()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _write_cache(data: Dict[str, Any]) -> None:
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


# ---------------------------------------------------------------- fetch

_TAG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


def _release_from_json(data: Any) -> Optional[Dict[str, Any]]:
    """The few fields LanEx shows, validated (GitHub's answer is data)."""
    if not isinstance(data, dict):
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    if not _TAG_RX.match(tag) or parse_version(tag) is None:
        return None
    url = str(data.get("html_url") or "")
    prefix = f"https://github.com/{REPO}/releases/"
    if not url.startswith(prefix):
        url = f"{RELEASES_URL}/tag/{tag}"
    installer = ""
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        link = str(a.get("browser_download_url") or "")
        if name.lower().endswith(".exe") and link.startswith(f"https://github.com/{REPO}/releases/download/"):
            installer = link
            break
    name = str(data.get("name") or tag)[:120]
    return {
        "notes": _clean_notes(data.get("body")),
        "tag": tag,
        "version": tag[1:] if tag[:1] in ("v", "V") else tag,
        "name": name,
        "url": url,
        "published_at": str(data.get("published_at") or "")[:40],
        "windows_installer": installer,
    }


_NOTES_MAX = 16 * 1024
_CTRL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_RX = re.compile(r"<[^>]{0,200}>")


def _clean_notes(body: Any) -> str:
    """The release text, as plain text: no HTML, no control characters, 16 KB at most.

    It is shown as text (the browser escapes it), so this is about keeping it
    readable and bounded, not about safety."""
    s = str(body or "")
    s = _CTRL_RX.sub("", s.replace("\r\n", "\n"))
    s = _HTML_RX.sub("", s)
    if len(s) > _NOTES_MAX:
        s = s[:_NOTES_MAX].rsplit("\n", 1)[0] + "\n…"
    return s.strip()


def fetch_latest(timeout: float = TIMEOUT_S, etag: str = "") -> Dict[str, Any]:
    """One GET of ``releases/latest``. Never raises: ``{"ok", "release"|"error"}``.

    With *etag* (the last answer's), GitHub answers 304 when nothing changed;
    that keeps the cached release and does not count against the 60 requests
    an hour GitHub allows a client without a token: ``{"ok", "not_modified"}``."""
    import urllib.error
    import urllib.request

    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if etag and re.match(r'^(W/)?"[\x21\x23-\x7e]{1,200}"$', etag):
        headers["If-None-Match"] = etag
    req = urllib.request.Request(API_URL, headers=headers)
    try:
        from . import tlsctx

        ctx = tlsctx.https_context()
    except Exception:
        ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read(_MAX_BODY + 1)
            hdrs = getattr(r, "headers", None)
            new_etag = str((hdrs.get("ETag") if hdrs is not None else "") or "")
        if len(body) > _MAX_BODY:
            return {"ok": False, "error": "answer too large"}
        rel = _release_from_json(json.loads(body.decode("utf-8", errors="replace")))
        if rel is None:
            return {"ok": False, "error": "no usable release in the answer"}
        return {"ok": True, "release": rel, "etag": new_etag}
    except urllib.error.HTTPError as ex:
        if ex.code == 304:
            return {"ok": True, "not_modified": True}
        if ex.code in (403, 429):
            return {"ok": False, "error": "GitHub's limit for update checks was reached; LanEx tries again tomorrow"}
        return {"ok": False, "error": f"GitHub answered {ex.code}"}
    except Exception as ex:  # offline, DNS, TLS, timeout: silent
        return {"ok": False, "error": type(ex).__name__}


def skip_version(version: str) -> Dict[str, Any]:
    """"Skip this version": the banner and the Tools tile stay quiet about
    *version* (an empty string shows it again). A newer release still shows."""
    v = str(version or "").strip()
    if v and parse_version(v) is None:
        return status()
    with _lock:
        cache = _read_cache()
        cache["skipped"] = v
        _write_cache(cache)
    return status()


# ---------------------------------------------------------------- answers

def how_to_update(release: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The update step for this install: Windows Setup's EXE, or the one-liner."""
    tag = (release or {}).get("tag") or ""
    windows = False
    try:
        from . import platform_env

        # The Windows appliance: WSL, and Windows Setup wrote runtime.json.
        windows = platform_env.is_wsl() and (platform_env.home() / "runtime.json").is_file()
    except Exception:
        windows = False
    if windows:
        return {
            "platform": "windows",
            "text": "Download the new LanEx Setup and run it. It updates LanEx in place; "
                    "your projects, runs and PDKs are kept.",
            "download": (release or {}).get("windows_installer") or (release or {}).get("url") or RELEASES_URL,
        }
    ref = tag if _TAG_RX.match(tag) else "main"
    cmd = (f"curl -fsSL https://raw.githubusercontent.com/{REPO}/{ref}/scripts/install.sh"
           f" | LANEX_REF={ref} bash")
    return {
        "platform": "unix",
        "text": "Run this in a terminal, then start LanEx again. Your projects, runs and PDKs are kept.",
        "command": cmd,
    }


def status() -> Dict[str, Any]:
    """What is known now, from the cache only (no network)."""
    cache = _read_cache()
    cur = current_version()
    rel = cache.get("release") if isinstance(cache.get("release"), dict) else None
    newer = bool(rel and is_newer(rel.get("tag"), cur))
    out: Dict[str, Any] = {
        "current": cur,
        "checked_at": cache.get("checked_at") or 0,
        "last_ok": bool(cache.get("last_ok")),
        "error": cache.get("error") or "",
        "latest": rel,
        "newer": newer,
        # "Skip this version": the banner stays quiet for exactly this release.
        "skipped": bool(newer and cache.get("skipped") and rel
                        and parse_version(cache.get("skipped")) == parse_version(rel.get("tag"))),
        "skipped_version": str(cache.get("skipped") or ""),
        "releases_url": RELEASES_URL,
        "interval_s": INTERVAL_S,
    }
    if newer:
        out["how"] = how_to_update(rel)
    return out


def check(force: bool = False, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Ask GitHub when the last attempt is a day old (``force``: now).

    The attempt time is recorded whether or not it worked, so an offline PC
    does not retry on every page load; a failure keeps the last good answer."""
    t = time.time() if now is None else now
    with _lock:
        cache = _read_cache()
        last = float(cache.get("checked_at") or 0)
        # Never checked, a day old, or a clock that moved back: ask.
        due = force or not last or (t - last) >= INTERVAL_S or t < last
        if due:
            # The ETag only while a release is cached: a 304 must have something to keep.
            etag = str(cache.get("etag") or "") if isinstance(cache.get("release"), dict) else ""
            res = fetch_latest(etag=etag) if etag else fetch_latest()
            cache["checked_at"] = t
            cache["last_ok"] = bool(res.get("ok"))
            if res.get("ok") and res.get("not_modified"):
                cache["error"] = ""
            elif res.get("ok"):
                cache["release"] = res["release"]
                cache["etag"] = str(res.get("etag") or "")
                cache["error"] = ""
            else:
                cache["error"] = str(res.get("error") or "")
            _write_cache(cache)
    return status()
