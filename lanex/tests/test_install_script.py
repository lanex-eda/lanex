# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Contract lock for the universal installer (scripts/install.sh).

The behavioural proof lives in container runs (Debian 12 / Ubuntu 22.04 /
Fedora 40 / Arch, plus the venv-fallback, sudo-refusal, shim and re-run cases —
all exercised live before this suite existed). These tests lock the properties
a future edit could silently drop:

  * both scripts stay valid bash,
  * the whole installer runs through main() called on the last line (a partial
    `curl | bash` download must never execute half a command),
  * no `set -e` (fallback chains must degrade, not abort),
  * the default install source is the GitHub tarball (no git dependency, no
    PyPI-name-squat window before the real PyPI release),
  * the sudo-refusal guard and the documented env knobs exist,
  * the legacy install-wsl.sh URL keeps working as a shim.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install.sh"
SHIM = REPO / "scripts" / "install-wsl.sh"


def _bash_executable() -> str:
    for candidate in (Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
                      Path(os.environ.get("ProgramFiles", "")) / "Git" / "usr" / "bin" / "bash.exe"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash") or "bash"


def _bash_n(path: Path) -> None:
    res = subprocess.run([_bash_executable(), "-n", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, f"bash -n failed for {path.name}: {res.stderr}"


def test_install_script_is_valid_bash() -> None:
    _bash_n(INSTALL)


def test_shim_is_valid_bash() -> None:
    _bash_n(SHIM)


def test_partial_download_guard_main_on_last_line() -> None:
    lines = [ln.strip() for ln in INSTALL.read_text().splitlines() if ln.strip()]
    assert lines[-1] == 'main "$@"'
    # ... and nothing above main's definition executes stages directly.
    body = INSTALL.read_text()
    assert body.index("main() {") < body.index('\nmain "$@"')


def test_no_set_e_fallback_chains_must_degrade() -> None:
    body = INSTALL.read_text()
    assert "set -u -o pipefail" in body
    import re
    directives = [ln for ln in body.splitlines()
                  if re.match(r"^\s*set\s+-\S*e", ln.strip())]
    assert directives == [], f"set -e would abort the fallback chains: {directives}"


def test_default_source_is_github_tarball() -> None:
    body = INSTALL.read_text()
    assert "github.com/" in body and "/archive/" in body and ".tar.gz" in body
    assert "archive/refs/heads/" not in body
    assert 'github) echo "$TARBALL"' in body
    # PyPI stays an explicit opt-in until the name is actually published.
    assert 'pypi)   echo "lanex"' in body


def test_sudo_refusal_guard_present() -> None:
    body = INSTALL.read_text()
    assert "SUDO_USER" in body
    assert "Don't run this installer with sudo" in body


def test_documented_env_knobs_are_wired() -> None:
    body = INSTALL.read_text()
    for knob in ("LANEX_FROM", "LANEX_REF", "LANEX_SKIP_PULL",
                 "LANEX_SKIP_GDS3D", "LANEX_NO_PIPX", "LANEX_ASSUME_YES"):
        assert f"${{{knob}" in body, f"{knob} documented but not read"


def test_build_tools_retry_stage_present() -> None:
    # The Arch/py3.13 lesson: a dependency without a prebuilt wheel needs a
    # compiler; the installer must install build tools and retry, not die.
    body = INSTALL.read_text()
    assert "build_tools_stage" in body
    assert "base-devel" in body and "build-essential" in body


def test_every_pkg_stage_installs_git() -> None:
    # Regression (round 56): the universal installer dropped git — the old
    # install-wsl.sh had it — and the in-app GDS3D build then failed on fresh
    # WSL with "needs: git". Every Linux package stage must offer git.
    body = INSTALL.read_text()
    for stage in ("apt_stage", "dnf_stage", "pacman_stage", "zypper_stage"):
        start = body.index(f"{stage}()")
        end = body.index("\n}", start)
        assert " git" in body[start:end], f"{stage} no longer installs git"


def test_every_pkg_stage_installs_gtkwave() -> None:
    # GTKWave backs the RTL IDE's "Open in GTKWave" button. Every package
    # stage (all four Linux managers AND brew) must offer it, degradably —
    # same contract as git above.
    body = INSTALL.read_text()
    for stage in ("apt_stage", "dnf_stage", "pacman_stage", "zypper_stage",
                  "brew_stage"):
        start = body.index(f"{stage}()")
        end = body.index("\n}", start)
        assert "gtkwave" in body[start:end], f"{stage} no longer installs gtkwave"


def test_gds3d_stage_present_and_skippable() -> None:
    # GDS3D auto-installs through LanEx's OWN installer (`--install-tool gds3d`,
    # the Tools-tab code path) so all its foolproofing applies; it must be
    # best-effort (never a die) and skippable via LANEX_SKIP_GDS3D=1.
    body = INSTALL.read_text()
    start = body.index("gds3d_stage()")
    end = body.index("\n}", start)
    stage = body[start:end]
    assert "--install-tool gds3d" in stage
    assert "LANEX_SKIP_GDS3D" in stage
    assert 'die "' not in stage, "gds3d_stage must degrade, never die"
    # Wired into main(), after the launcher exists (verify_install sets it up).
    main_start = body.index("main() {")
    assert "gds3d_stage" in body[main_start:]
    assert body.index("verify_install", main_start) < body.index("gds3d_stage", main_start)


def test_shim_delegates_to_universal_installer() -> None:
    body = SHIM.read_text()
    assert "install.sh" in body
    assert "curl" in body and "wget" in body


def test_readme_points_at_universal_installer() -> None:
    readme = (REPO / "README.md").read_text()
    assert "scripts/install.sh | bash" in readme
    # The paste-broken `exec bash` pattern must not come back to the blocks.
    assert "&& exec bash" not in readme
    assert "&& exec zsh" not in readme


# --- the release the one-liner installs (G22) -------------------------------

STAMPER = REPO / "scripts" / "stamp_release_ref.py"
FAKE_CURL = """#!/bin/sh
# Test double for curl: "downloads" a tiny installer that reports how it ran.
out=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in -o) out="$2"; shift ;; -*) ;; *) url="$1" ;; esac
    shift
done
[ "${FAKE_CURL_FAIL:-0}" = 1 ] && exit 22
printf 'main() { echo "handover url=%s ref=$LANEX_REF boot=$LANEX_BOOTSTRAPPED args=$*"; }\\nmain "$@"\\n' "$url" > "$out"
"""


def _stamper():
    spec = importlib.util.spec_from_file_location("stamp_release_ref", STAMPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stamp_of(path: Path) -> str:
    found = re.findall(r'^LANEX_RELEASE_REF="([^"]*)"$', path.read_text(), re.M)
    assert len(found) == 1, path
    return found[0]


# Git for Windows' bash.exe puts its own bin folders first on PATH, so the
# fake curl is put first from inside bash, then the real command runs.
_WITH_FAKE_CURL = ('fb="$LX_FAKE_BIN"; if command -v cygpath >/dev/null 2>&1; then '
                   'fb="$(cygpath -u "$fb")"; fi; PATH="$fb:$PATH"; exec bash "$@"')


def _run_with_fake_curl(tmp_path: Path, *args: str, stdin: str | None = None,
                        fail: bool = False) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL, newline="\n")
    curl.chmod(0o755)
    env = os.environ.copy()
    for knob in ("LANEX_REF", "LANEX_FROM", "LANEX_BOOTSTRAPPED", "LANEX_REPO"):
        env.pop(knob, None)
    env["LX_FAKE_BIN"] = str(bindir)
    if fail:
        env["FAKE_CURL_FAIL"] = "1"
    return subprocess.run([_bash_executable(), "-c", _WITH_FAKE_CURL, "bash", *args],
                          input=stdin, env=env, capture_output=True, text=True, cwd=tmp_path)


def test_release_stamp_matches_in_both_scripts_and_any_head_tag() -> None:
    # Fails on a tagged checkout whose installers would install main or
    # another release: "not main in a tagged build".
    assert _stamp_of(INSTALL) == _stamp_of(SHIM)
    res = subprocess.run([sys.executable, "-I", str(STAMPER), "--check"], cwd=REPO,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + res.stdout


def test_default_ref_is_the_stamp() -> None:
    body = INSTALL.read_text()
    assert 'REF="${LANEX_REF:-$LANEX_RELEASE_REF}"' in body
    assert 'REF="${LANEX_REF:-main}"' not in body
    env = os.environ.copy()
    for knob in ("LANEX_REF", "LANEX_FROM", "LANEX_REPO"):
        env.pop(knob, None)
    res = subprocess.run([_bash_executable(), str(INSTALL), "--print-source"], env=env,
                         capture_output=True, text=True)
    assert res.stdout.strip() == f"https://github.com/lanex-eda/lanex/archive/{_stamp_of(INSTALL)}.tar.gz"


def test_stamper_writes_both_scripts_and_refuses_non_tags(tmp_path: Path, monkeypatch) -> None:
    mod = _stamper()
    copies = []
    for src in (INSTALL, SHIM):
        dst = tmp_path / src.name
        dst.write_text(src.read_text(), newline="\n")
        copies.append(dst)
    monkeypatch.setattr(mod, "SCRIPTS", tuple(copies))
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    assert mod.main(["v9.8.7"]) == 0
    assert all(_stamp_of(p) == "v9.8.7" for p in copies)
    assert mod.main(["main"]) == 2 and mod.main(["latest"]) == 2
    assert all(_stamp_of(p) == "v9.8.7" for p in copies)


def test_piped_stamped_installer_hands_over_to_the_release_copy(tmp_path: Path) -> None:
    # A stamped main copy run through a pipe runs the release's own install.sh.
    stamped = INSTALL.read_text().replace('LANEX_RELEASE_REF="main"', 'LANEX_RELEASE_REF="v9.9.9"', 1)
    assert 'LANEX_RELEASE_REF="v9.9.9"' in stamped
    res = _run_with_fake_curl(tmp_path, "-s", "--", "--help", stdin=stamped)
    assert res.returncode == 0, res.stderr
    assert "/lanex-eda/lanex/v9.9.9/scripts/install.sh" in res.stdout
    assert "ref=v9.9.9 boot=1 args=--help" in res.stdout


def test_shim_fails_loudly_when_the_download_fails(tmp_path: Path) -> None:
    shim = tmp_path / "install-wsl.sh"          # no sibling install.sh here
    shim.write_text(SHIM.read_text(), newline="\n")
    res = _run_with_fake_curl(tmp_path, str(shim), fail=True)
    assert res.returncode != 0
    assert "Could not download the LanEx installer" in res.stderr
    assert "handover" not in res.stdout


def test_shim_runs_the_downloaded_installer_with_its_arguments(tmp_path: Path) -> None:
    shim = tmp_path / "install-wsl.sh"
    shim.write_text(SHIM.read_text(), newline="\n")
    res = _run_with_fake_curl(tmp_path, str(shim), "--profile", "minimal")
    assert res.returncode == 0, res.stderr
    ref = _stamp_of(SHIM)
    assert f"/{ref}/scripts/install.sh ref={ref} boot=1 args=--profile minimal" in res.stdout


def test_every_network_fetch_checks_its_result() -> None:
    for path in (INSTALL, SHIM):
        body = path.read_text()
        # bash -c "$(curl ...)" runs an empty script and succeeds when curl fails.
        assert 'bash -c "$(curl' not in body and 'bash -c "$(wget' not in body, path.name


# --- PATH line and the /usr/local/bin link (audit 4.1) -----------------------

def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$", INSTALL.read_text(), re.M | re.S)
    assert match, name
    return match.group(0)


def test_path_line_is_added_once(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text('export PATH="/opt/lanexbin:$PATH"\n', newline="\n")   # an older run's line
    script = (_function("add_path_line")
              + "\nadd_path_line /opt/lanexbin\nadd_path_line /opt/lanexbin"
              + "\nadd_path_line /opt/other\nadd_path_line /opt/other\n")
    env = dict(os.environ, HOME=str(tmp_path), SHELL="/bin/bash")
    res = subprocess.run([_bash_executable(), "-c", script], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    text = rc.read_text()
    assert text.count('export PATH="/opt/lanexbin:$PATH"') == 1
    assert text.count('export PATH="/opt/other:$PATH"') == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Git Bash's ln -s copies instead of linking")
def test_only_our_own_link_counts_as_ours(tmp_path: Path) -> None:
    script = _function("lanex_link_is_ours") + """
LAUNCHER="$HOME/.local/bin/lanex"
mkdir -p "$HOME/.lanex/venv/bin"
ln -s "$HOME/.lanex/venv/bin/lanex" "$HOME/ours"
ln -s /opt/someone-else/lanex "$HOME/theirs"
ln -s /home/alice/.local/bin/lanex "$HOME/alices"
echo plain > "$HOME/file"
for p in ours theirs alices file; do
    if lanex_link_is_ours "$HOME/$p"; then echo "$p=ours"; else echo "$p=not"; fi
done
"""
    env = dict(os.environ, HOME=str(tmp_path))
    res = subprocess.run([_bash_executable(), "-c", script], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == ["ours=ours", "theirs=not", "alices=not", "file=not"]


def test_existing_foreign_lanex_command_is_not_overwritten() -> None:
    body = _function("expose_on_path")
    assert body.index("lanex_link_is_ours") < body.index('ln -sfn "$LAUNCHER"')
    assert 'ln -sf "$LAUNCHER" /usr/local/bin/lanex' not in INSTALL.read_text()
