# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Release stamping (G22) and the Unix installer's sudo-less path (audit 5d),
plus container viewers and simulations running as the person."""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STAMPER = REPO / "scripts" / "stamp_release_ref.py"
INSTALL = REPO / "scripts" / "install.sh"


def _stamper():
    spec = importlib.util.spec_from_file_location("stamp_release_ref_l6", STAMPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _installer(ref: str) -> str:
    return re.sub(r'^LANEX_RELEASE_REF="[^"]*"$', f'LANEX_RELEASE_REF="{ref}"',
                  INSTALL.read_text(encoding="utf-8"), count=1, flags=re.M)


def test_release_artifact_that_carries_main_fails(tmp_path):
    mod = _stamper()
    art = tmp_path / "release"
    (art / "scripts").mkdir(parents=True)
    (art / "scripts" / "install.sh").write_text(_installer("main"), encoding="utf-8")
    assert mod.check_artifact(art, "v1.1.0") == 1
    (art / "scripts" / "install.sh").write_text(_installer("v1.1.0"), encoding="utf-8")
    assert mod.check_artifact(art, "v1.1.0") == 0
    # Another release's tag is as wrong as main.
    assert mod.check_artifact(art, "v1.2.0") == 1


def test_release_artifact_check_reads_archives(tmp_path):
    mod = _stamper()
    z = tmp_path / "bundle.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("lanex-1.1.0/scripts/install.sh", _installer("main"))
    assert mod.check_artifact(z, "v1.1.0") == 1
    t = tmp_path / "bundle.tar.gz"
    src = tmp_path / "install-wsl.sh"
    src.write_text('#!/bin/sh\nLANEX_RELEASE_REF="v1.1.0"\n', encoding="utf-8")
    with tarfile.open(t, "w:gz") as f:
        f.add(src, arcname="x/install-wsl.sh")
    assert mod.check_artifact(t, "v1.1.0") == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mod.check_artifact(empty, "v1.1.0") == 1     # nothing to check is not a pass
    assert mod.check_artifact(t, "main") == 2           # main is never a release tag


def test_build_script_stamps_a_release_tag():
    text = (REPO / "scripts" / "build_windows_release.py").read_text(encoding="utf-8")
    assert "--release-tag" in text and "--check-artifact" in text


def _bash():
    return shutil.which("bash")


def _function(name: str) -> str:
    text = INSTALL.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, name
    return m.group(0)


@pytest.mark.skipif(not _bash() or sys.platform == "win32", reason="needs a POSIX bash")
def test_no_sudo_installs_into_home_only(tmp_path):
    """LANEX_NO_SUDO=1: no sudo, no system packages, plain words instead of a prompt."""
    script = "\n".join([
        "set -u",
        'note() { echo "NOTE $*"; }', 'warn() { echo "WARN $*"; }', 'die() { echo "DIE $*"; exit 9; }',
        'apt_stage() { echo "APT RAN"; }',
        'OS=linux; PKG=apt; SUDO=""',
        _function("setup_privileges"),
        _function("system_deps"),
        "setup_privileges", "system_deps",
        'echo "SUDO=[$SUDO] PKG=$PKG NO_SUDO=${NO_SUDO:-0}"',
    ])
    env = {"PATH": "/usr/bin:/bin", "LANEX_NO_SUDO": "1", "HOME": str(tmp_path)}
    out = subprocess.run([_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "SUDO=[] PKG=none NO_SUDO=1" in out.stdout
    assert "APT RAN" not in out.stdout and "home folder only" in out.stdout


@pytest.mark.skipif(not _bash() or sys.platform == "win32", reason="needs a POSIX bash")
def test_sudo_that_needs_a_password_without_a_terminal_does_not_hang(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "sudo").write_text("#!/bin/sh\nexit 1\n")   # 'a password is required'
    (fake / "sudo").chmod(0o755)
    script = "\n".join([
        "set -u",
        'note() { echo "NOTE $*"; }', 'warn() { echo "WARN $*"; }', 'die() { echo "DIE $*"; exit 9; }',
        'OS=linux; PKG=apt; SUDO=""',
        _function("setup_privileges"),
        "setup_privileges",
        'echo "SUDO=[$SUDO] NO_SUDO=${NO_SUDO:-0}"',
    ])
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(["setsid", _bash(), "-c", script] if shutil.which("setsid") else [_bash(), "-c", script],
                         env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert "NO_SUDO=1" in out.stdout, out.stdout + out.stderr


def test_main_skips_engine_steps_without_sudo():
    body = _function("main")
    assert 'if [ "${NO_SUDO:-0}" = "1" ]; then' in body
    assert "lanex doctor" in body


def test_viewers_and_sims_run_as_the_person(monkeypatch):
    from lanex.controller import container_tools, simulate

    container_tools._USER_FLAGS_CACHE.clear()
    monkeypatch.setattr(container_tools.sys, "platform", "linux")
    monkeypatch.setattr(container_tools.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(container_tools.os, "getgid", lambda: 1000, raising=False)

    class R:
        stdout = "[name=seccomp,profile=builtin name=cgroupns]"

    monkeypatch.setattr(container_tools.subprocess, "run", lambda *a, **k: R())
    flags = container_tools.user_flags("docker")
    assert flags == ["--user", "1000:1000", "-e", "HOME=/tmp"]
    argv = container_tools.build_argv("docker", "img", "klayout", design_dir=Path("/d"), work_dir=Path("/d"))
    assert argv[4:6] == ["--user", "1000:1000"]
    # Podman and rootless Docker already map to the person.
    assert container_tools.user_flags("podman") == []
    container_tools._USER_FLAGS_CACHE.clear()
    R.stdout = "[name=rootless]"
    assert container_tools.user_flags("docker") == []
    container_tools._USER_FLAGS_CACHE.clear()
    R.stdout = ""
    cmd = simulate.build_sim_argv if hasattr(simulate, "build_sim_argv") else None
    text = Path(simulate.__file__).read_text(encoding="utf-8")
    assert "user_flags(engine)" in text
    container_tools._USER_FLAGS_CACHE.clear()
