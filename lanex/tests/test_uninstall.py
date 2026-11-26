# Copyright 2026 LanEx Contributors
# Licensed under the Apache License, Version 2.0
"""`lanex --uninstall` leaves nothing behind but what the person keeps.

Tester report 2026-11-24 (macOS): after the documented manual removal, 3.5 GB
stayed: ~/.lanex/app-profile, ~/.cache/lanex/pdk-assets and the projects
folder. These tests run a simulated session in a throwaway home, uninstall,
and list every file that is left. A new place LanEx writes to that the
uninstaller does not know about fails here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="macOS/Linux uninstall (Windows has its own uninstaller)")


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from lanex.controller import installer, pdkfeed, platform_env, shortcut, uninstall

    home = tmp_path / "home"
    home.mkdir()
    for var in ("LANEX_HOME", "LIBRELANE_GUI_HOME", "XDG_CACHE_HOME", "PDK_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Never a real folder: projects and PDKs live in the throwaway home.
    monkeypatch.setattr(platform_env, "projects_dir", lambda: home / "lanex-projects")
    monkeypatch.setattr(installer, "ciel_home", lambda: str(home / ".ciel"))
    monkeypatch.setattr(uninstall, "_images", lambda: [])
    monkeypatch.setattr(uninstall, "is_windows_appliance", lambda: False)
    monkeypatch.setattr(shortcut, "_desktop_dir", lambda h: None)

    def write(p: Path, n: int = 100) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * n)

    lx = platform_env.home()
    assert lx == home / ".lanex"
    write(lx / "app-profile" / "OptGuideOnDeviceModel" / "model.bin", 5000)
    write(lx / "app-profile" / "Default" / "Preferences")
    write(lx / "tools" / "GDS3D" / "linux" / "GDS3D")
    write(lx / "logs" / "lanex.log")
    write(lx / "image.lock")
    write(pdkfeed.cache_dir("sky130", "a" * 40) / "common.tar.zst", 700)
    (home / ".local" / "bin").mkdir(parents=True)
    os.symlink(lx / "tools" / "GDS3D" / "linux" / "GDS3D", home / ".local" / "bin" / "gds3d")
    if sys.platform.startswith("linux"):
        assert shortcut.install(home=home).get("ok")
    write(home / "lanex-projects" / "counter" / "src" / "counter.v")
    write(home / ".ciel" / "ciel" / "sky130" / "versions" / ("a" * 40) / "x.lib")
    write(home / "Documents" / "unrelated.txt")          # never LanEx's
    return home, uninstall


def _files(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() or p.is_symlink())


def test_default_uninstall_keeps_only_projects_pdks_and_unrelated_files(session) -> None:
    home, uninstall = session
    assert uninstall.main(yes=True) == 0
    left = _files(home)
    assert all(f.startswith(("lanex-projects", ".ciel", "Documents")) for f in left), left
    assert "Documents/unrelated.txt" in left
    assert not (home / ".lanex").exists() and not (home / ".cache" / "lanex").exists()


def test_full_uninstall_leaves_only_unrelated_files(session) -> None:
    home, uninstall = session
    assert uninstall.main(yes=True, remove_projects=True, remove_pdks=True) == 0
    assert _files(home) == ["Documents/unrelated.txt"]


def test_dry_run_removes_nothing(session) -> None:
    home, uninstall = session
    before = _files(home)
    assert uninstall.main(dry_run=True) == 0
    assert _files(home) == before


def test_never_deletes_a_folder_not_named_like_lanex(session, monkeypatch) -> None:
    home, uninstall = session
    # A LANEX_HOME pointed at the home folder itself must not wipe the home.
    monkeypatch.setenv("LANEX_HOME", str(home))
    uninstall.main(yes=True)
    assert (home / "Documents" / "unrelated.txt").exists()
    assert (home / "lanex-projects" / "counter" / "src" / "counter.v").exists()


def test_without_a_terminal_and_without_yes_nothing_is_removed(session, monkeypatch) -> None:
    home, uninstall = session
    monkeypatch.setattr(uninstall, "_tty_in", lambda: None)
    before = _files(home)
    assert uninstall.main() == uninstall.DECLINED
    after = _files(home)
    assert after == before, {"removed": sorted(set(before) - set(after)), "added": sorted(set(after) - set(before))}


REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_install_sh_uninstall_removes_program_link_and_path_line(tmp_path: Path) -> None:
    """The one command a person runs: data, venv install, PATH line; projects stay."""
    import subprocess

    home = tmp_path / "home"
    venv_bin = home / ".lanex" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    launcher = venv_bin / "lanex"
    # A venv-fallback install whose `lanex` is this checkout's LanEx.
    launcher.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={REPO} exec {sys.executable} -c "
        "'import sys; from lanex.cli import main; sys.exit(main(sys.argv[1:]))' \"$@\"\n")
    launcher.chmod(0o755)
    (home / ".lanex" / "app-profile" / "Default").mkdir(parents=True)
    (home / ".lanex" / "app-profile" / "Default" / "Preferences").write_text("{}")
    cache = home / ".cache" / "lanex" / "pdk-assets" / "sky130-x"
    cache.mkdir(parents=True)
    (cache / "common.tar.zst").write_bytes(b"x" * 10)
    (home / "lanex-projects" / "a").mkdir(parents=True)
    (home / "lanex-projects" / "a" / "a.v").write_text("module a; endmodule\n")
    rc = home / ".bashrc"
    rc.write_text("alias ll='ls -l'\n\n# Added by the LanEx installer\n"
                  f"export PATH=\"{venv_bin}:$PATH\"\nexport EDITOR=vim\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash",
           "LANEX_REF": "main"}   # no release re-fetch
    out = subprocess.run(["/bin/bash", str(REPO / "scripts" / "install.sh"), "--uninstall", "--yes"],
                         env=env, capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not (home / ".lanex").exists()
    assert not (home / ".cache" / "lanex").exists()
    assert (home / "lanex-projects" / "a" / "a.v").exists()
    text = rc.read_text()
    assert "LanEx" not in text and str(venv_bin) not in text
    assert "alias ll='ls -l'" in text and "export EDITOR=vim" in text


def test_refuses_inside_the_windows_environment(session, monkeypatch, capsys) -> None:
    home, uninstall = session
    monkeypatch.setattr(uninstall, "is_windows_appliance", lambda: True)
    assert uninstall.main(yes=True) == 2
    assert (home / ".lanex").exists()
    assert "Uninstall or remove LanEx" in capsys.readouterr().out
