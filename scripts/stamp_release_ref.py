#!/usr/bin/env python3
"""Stamp the release tag into the Unix installers, or check the stamp.

The one-liner (curl .../main/scripts/install.sh | bash) installs the ref in
LANEX_RELEASE_REF, so a release must stamp its own tag there before it is
tagged:

    python scripts/stamp_release_ref.py v1.1.0     # writes both scripts
    git commit -am "chore(release): stamp v1.1.0 into the installers"
    git tag v1.1.0

    python scripts/stamp_release_ref.py --check    # what CI and the build run

--check fails when the two scripts disagree, when the stamp is not a ref
shape, or when HEAD is exactly at a v* tag whose name is not the stamp (a
tagged build that would install main, or another release). On an untagged
commit "main" passes with a warning: that is the development line before the
first stamped release.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = (REPO_ROOT / "scripts" / "install.sh", REPO_ROOT / "scripts" / "install-wsl.sh")
STAMP_RX = re.compile(r'^LANEX_RELEASE_REF="([^"\n]*)"$', re.M)
TAG_RX = re.compile(r"^v\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?$")


def read_stamps() -> dict[Path, str]:
    stamps = {}
    for path in SCRIPTS:
        found = STAMP_RX.findall(path.read_text(encoding="utf-8"))
        if len(found) != 1:
            raise SystemExit(f"{path.name}: expected exactly one LANEX_RELEASE_REF line, found {len(found)}")
        stamps[path] = found[0]
    return stamps


def head_tags() -> list[str]:
    res = subprocess.run(["git", "tag", "--points-at", "HEAD"], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    if res.returncode != 0:
        return []
    return [t for t in res.stdout.split() if TAG_RX.match(t)]


def check() -> int:
    stamps = read_stamps()
    values = set(stamps.values())
    if len(values) != 1:
        print("install.sh and install-wsl.sh carry different LANEX_RELEASE_REF values: "
              + ", ".join(f"{p.name}={v}" for p, v in stamps.items()), file=sys.stderr)
        return 1
    stamp = values.pop()
    if stamp != "main" and not TAG_RX.match(stamp):
        print(f"LANEX_RELEASE_REF={stamp!r} is not a release tag (vX.Y.Z) or main", file=sys.stderr)
        return 1
    tags = head_tags()
    if tags and stamp not in tags:
        print(f"HEAD is tagged {', '.join(tags)} but the installers install {stamp!r}. "
              f"Run: python scripts/stamp_release_ref.py {tags[0]}", file=sys.stderr)
        return 1
    if stamp == "main":
        print("note: LANEX_RELEASE_REF is main (development line); stamp the tag before a release.")
    else:
        print(f"installers install {stamp}")
    return 0


def stamp(tag: str) -> int:
    if not TAG_RX.match(tag):
        print(f"{tag!r} is not a release tag like v1.2.3", file=sys.stderr)
        return 2
    read_stamps()  # both scripts have exactly one line
    for path in SCRIPTS:
        text = path.read_text(encoding="utf-8")
        path.write_text(STAMP_RX.sub(f'LANEX_RELEASE_REF="{tag}"', text, count=1),
                        encoding="utf-8", newline="\n")
        print(f"{path.relative_to(REPO_ROOT)}: LANEX_RELEASE_REF={tag}")
    return 0


_INSTALLER_NAMES = ("install.sh", "install-wsl.sh")


def artifact_stamps(path: Path) -> dict[str, list[str]]:
    """``{member: [LANEX_RELEASE_REF values]}`` for every Unix installer in
    *path*: the file itself, a folder (searched recursively), or a zip/wheel
    or tar archive (its members)."""
    import tarfile
    import zipfile

    found: dict[str, list[str]] = {}

    def take(name: str, text: str) -> None:
        if Path(name).name in _INSTALLER_NAMES:
            found[name] = STAMP_RX.findall(text)

    p = Path(path)
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file() and f.name in _INSTALLER_NAMES:
                take(str(f), f.read_text(encoding="utf-8", errors="replace"))
    elif zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if Path(n).name in _INSTALLER_NAMES:
                    take(n, z.read(n).decode("utf-8", errors="replace"))
    elif p.is_file() and tarfile.is_tarfile(p):
        with tarfile.open(p) as t:
            for m in t.getmembers():
                if m.isfile() and Path(m.name).name in _INSTALLER_NAMES:
                    f = t.extractfile(m)
                    if f is not None:
                        take(m.name, f.read().decode("utf-8", errors="replace"))
    elif p.is_file():
        take(str(p), p.read_text(encoding="utf-8", errors="replace"))
    return found


def check_artifact(path: Path, tag: str) -> int:
    """Fail when a release artifact would make the one-liner install ``main`` (or
    anything but *tag*): every Unix installer in it must carry exactly *tag*."""
    if not TAG_RX.match(tag or ""):
        print(f"{tag!r} is not a release tag like v1.2.3", file=sys.stderr)
        return 2
    found = artifact_stamps(path)
    if not found:
        print(f"{path}: no install.sh or install-wsl.sh in this artifact", file=sys.stderr)
        return 1
    bad = {name: refs for name, refs in found.items() if refs != [tag]}
    for name, refs in bad.items():
        print(f"{name}: LANEX_RELEASE_REF is {', '.join(refs) or 'missing'}, not {tag}. "
              f"Stamp it before the release: python scripts/stamp_release_ref.py {tag}", file=sys.stderr)
    if not bad:
        print(f"{path}: every installer carries {tag}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("tag", nargs="?", help="release tag to stamp, e.g. v1.1.0")
    group.add_argument("--check", action="store_true", help="verify the stamp")
    group.add_argument("--check-artifact", metavar="PATH",
                       help="fail unless every Unix installer in PATH (file, folder, zip, wheel or "
                            "tar) carries --tag")
    parser.add_argument("--tag", default="", help="with --check-artifact: the release tag")
    args = parser.parse_args(argv)
    if args.check_artifact:
        return check_artifact(Path(args.check_artifact), args.tag)
    return check() if args.check else stamp(args.tag)


if __name__ == "__main__":
    sys.exit(main())
