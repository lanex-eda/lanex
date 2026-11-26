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
"""Writing LanEx's own files into a project or run folder without following
links planted there.

A project can come from someone else, so any folder or file in it may be a
symbolic link to somewhere else (``~/.ssh``, another project, ``~/.cache``).
Every writer that puts LanEx's files into a project or a run (reports,
caches, logs, notes) goes through here:

- ``confined_dir(root, ".lanex", "thumbs")``: that folder as a real folder
  inside *root*, made when missing. Raises ``UnsafePath`` when any part of it
  is a link or not a folder.
- ``write_bytes(folder, name, data)`` / ``write_text``: a fresh temp file in
  *folder*, then ``os.replace``. A link at ``folder/name`` is replaced, never
  written through.
- ``open_append(path)``: append with ``O_NOFOLLOW``; a link there fails.
- ``remove_tree(root, *parts)``: deletes only a real folder inside *root*.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import IO, Iterable, Union

PathLike = Union[str, "os.PathLike[str]"]


class UnsafePath(OSError):
    """A folder or file LanEx would write is a link, or isn't what it should be."""


def _parts(parts: Iterable[PathLike]) -> list:
    out = []
    for p in parts:
        for seg in str(p).replace("\\", "/").split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                raise UnsafePath(f"{p!s} leaves the folder it belongs in.")
            out.append(seg)
    return out


def _refuse_link(p: Path) -> UnsafePath:
    return UnsafePath(f"{p} is a link to somewhere else, so LanEx won't write there. "
                      f"Remove the link (LanEx makes the folder itself) and try again.")


def confined_dir(root: PathLike, *parts: PathLike, create: bool = True) -> Path:
    """``root/parts...`` as a real folder inside *root* (whose own path is
    trusted and resolved first). Each part must be a real folder, not a link;
    missing ones are made when *create*. Raises ``UnsafePath`` (an OSError)."""
    cur = Path(os.path.realpath(os.fspath(root)))
    for seg in _parts(parts):
        cur = cur / seg
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(cur)
            except FileExistsError:
                pass
            st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode):
            raise _refuse_link(cur)
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{cur} is a file, not a folder, so LanEx can't keep its files there.")
    return cur


def check_inside(root: PathLike, path: PathLike) -> Path:
    """*path* when it is inside *root* through real folders only (the last
    part may be missing); else ``UnsafePath``."""
    base = Path(os.path.realpath(os.fspath(root)))
    p = Path(os.path.abspath(os.fspath(path)))
    try:
        rel = p.relative_to(base)
    except ValueError:
        raise UnsafePath(f"{p} is outside {base}.") from None
    parts = rel.parts
    if len(parts) > 1:
        confined_dir(base, *parts[:-1], create=False)
    if parts and os.path.islink(p):
        raise _refuse_link(p)
    return p


def _name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise UnsafePath(f"{name!r} is not a plain file name.")
    return name


def write_bytes(folder: PathLike, name: str, data: bytes, *, mode: int = 0o644) -> Path:
    """Write *data* to ``folder/name`` through a temp file and ``os.replace``.
    *folder* should come from ``confined_dir``; a link at ``folder/name`` is
    replaced by the file, its target is never touched."""
    folder = Path(folder)
    target = folder / _name(name)
    fd, tmp = tempfile.mkstemp(prefix="." + name + ".", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def write_text(folder: PathLike, name: str, text: str, *, encoding: str = "utf-8") -> Path:
    return write_bytes(folder, name, text.encode(encoding))


def open_append(path: PathLike, *, encoding: str = "utf-8", buffering: int = -1) -> IO[str]:
    """Open *path* to append text, creating it; a link there raises
    ``UnsafePath`` instead of being followed."""
    p = os.fspath(path)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW") and os.path.islink(p):
        raise _refuse_link(Path(p))
    try:
        fd = os.open(p, flags, 0o644)
    except OSError as ex:
        if os.path.islink(p):
            raise _refuse_link(Path(p)) from ex
        raise
    return os.fdopen(fd, "a", buffering=buffering, encoding=encoding, errors="replace")


def open_new(path: PathLike, *, encoding: str = "utf-8") -> IO[str]:
    """Open *path* as a new, empty text file: whatever is there (a file or a
    link, never a folder) is removed first, and the file is created
    exclusively, so a link planted there is never written through."""
    p = os.fspath(path)
    try:
        st = os.lstat(p)
        if stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{p} is a folder, not a file.")
        os.unlink(p)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(p, flags, 0o644)
    return os.fdopen(fd, "w", encoding=encoding, errors="replace")


def remove_tree(root: PathLike, *parts: PathLike) -> bool:
    """Delete ``root/parts...`` when it is a real folder inside *root*. A link
    there is unlinked (the link, not what it points at). False when absent."""
    segs = _parts(parts)
    if not segs:
        raise UnsafePath("refusing to delete the root folder itself")
    try:
        parent = confined_dir(root, *segs[:-1], create=False)
    except FileNotFoundError:
        return False
    target = parent / segs[-1]
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        os.unlink(target)
        return True
    shutil.rmtree(target)
    return True
