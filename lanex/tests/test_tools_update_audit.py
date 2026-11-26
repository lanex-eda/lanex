# Copyright 2026 LanEx Contributors
# Licensed under the Apache License, Version 2.0
"""Audit 2026-11-15: image lock (G15), in-app install supply chain (G24),
PDK layer names (G17), the Tools page bugs (4.1) and the update check (5a).

Nothing here installs, removes or downloads anything: commands are recorded,
the network is faked, and every file lives under the test's LANEX_HOME.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from lanex.controller import container_run, installer, pdk as pdk_mod, platform_env, updatecheck

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LANEX_HOME", str(h))
    monkeypatch.delenv("LIBRELANE_IMAGE_OVERRIDE", raising=False)
    return h


def _lock(home: Path, name: str, data: dict) -> None:
    (home / name).write_text(json.dumps(data), encoding="utf-8")


# ============================================================ G15 image lock

def test_lock_for_the_installed_librelane_is_used(home, monkeypatch):
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.4")
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane:3.0.4@sha256:" + "a" * 64,
                               "digest": "", "librelane_version": "3.0.4"})
    assert container_run.image_ref() == "ghcr.io/librelane/librelane:3.0.4@sha256:" + "a" * 64
    assert container_run.image_provenance()["image_stale_record"] is None


def test_lock_from_before_an_upgrade_is_ignored(home, monkeypatch):
    # The audit's case: the Tools page pulled 3.0.4's image, then LanEx was
    # upgraded with LibreLane 3.0.5. Runs must use 3.0.5's image.
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.5")
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane:3.0.4@sha256:" + "a" * 64,
                               "digest": "ghcr.io/librelane/librelane@sha256:" + "a" * 64,
                               "engine": "docker"})   # an old lock: no version field
    ref = container_run.image_ref()
    assert ref.startswith("ghcr.io/librelane/librelane:3.0.5")
    stale = container_run.image_provenance()["image_stale_record"]
    assert stale and stale["librelane_version"] == "3.0.4" and stale["source"] == "image.lock"


def test_lock_version_field_wins_over_the_tag(home, monkeypatch):
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.5")
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane:custom@sha256:" + "b" * 64,
                               "librelane_version": "3.0.5"})
    assert container_run.image_ref().endswith("b" * 64)


def test_stale_runtime_json_falls_through_to_a_fresh_lock(home, monkeypatch):
    # Windows Setup's runtime.json is from 3.0.4; after the upgrade the Tools
    # page pulled 3.0.5 and wrote image.lock. The fresh lock is used, and the
    # Tools page has nothing to warn about.
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.5")
    _lock(home, "runtime.json", {"engine": "docker", "image": "ghcr.io/librelane/librelane:3.0.4",
                                 "digest": "sha256:" + "a" * 64,
                                 "imageRef": "ghcr.io/librelane/librelane:3.0.4@sha256:" + "a" * 64})
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane:3.0.5@sha256:" + "c" * 64,
                               "librelane_version": "3.0.5"})
    assert container_run.image_ref().endswith("c" * 64)
    assert container_run.image_provenance()["image_stale_record"] is None


def test_runtime_json_for_the_installed_version_still_wins(home, monkeypatch):
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.4")
    ref = "ghcr.io/librelane/librelane:3.0.4@sha256:" + "e" * 64
    _lock(home, "runtime.json", {"engine": "docker", "imageRef": ref})
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane:3.0.4@sha256:" + "f" * 64,
                               "librelane_version": "3.0.4"})
    assert container_run.image_ref() == ref


def test_versionless_lock_with_another_digest_is_stale_when_a_tested_digest_exists(home, monkeypatch):
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.4")
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane@sha256:" + "d" * 64})
    assert container_run.image_ref() == container_run.default_image_ref("3.0.4")
    assert container_run._KNOWN_IMAGE_DIGESTS["3.0.4"] in container_run.image_ref()


def test_override_still_wins(home, monkeypatch):
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.5")
    monkeypatch.setenv("LIBRELANE_IMAGE_OVERRIDE", "my/image:x")
    _lock(home, "image.lock", {"image": "ghcr.io/librelane/librelane:3.0.4"})
    assert container_run.image_ref() == "my/image:x"
    assert container_run.image_provenance()["image_stale_record"] is None


def test_record_image_digest_stores_the_librelane_version(home, monkeypatch):
    monkeypatch.setattr(container_run, "installed_librelane_version", lambda: "3.0.4")
    monkeypatch.setattr(installer, "_shell_exec_quiet",
                        lambda argv, timeout=10.0: (0, "ghcr.io/librelane/librelane@sha256:" + "a" * 64 + "\n", ""))
    installer.record_image_digest("docker", "ghcr.io/librelane/librelane:3.0.4")
    data = json.loads((home / "image.lock").read_text(encoding="utf-8"))
    assert data["librelane_version"] == "3.0.4"
    assert data["digest"].endswith("a" * 64)


# ============================================================ G24 supply chain

def _env(**kw):
    base = {"os": "linux", "pip": True, "apt": True, "curl": True, "dnf": False}
    base.update(kw)
    return base


def test_no_strategy_pipes_a_script_to_root():
    for s in installer._STRATEGY_REGISTRY:
        for key in ("docker", "podman"):
            argv = s["prepare"](_env(dnf=True, brew=True, choco=True, scoop=True, nix=True, conda=True), key)
            text = " ".join(argv or [])
            assert "get.docker.com" not in text and "| sudo sh" not in text, s["label"]
    labels = [s["label"] for s in installer._STRATEGY_REGISTRY]
    assert not any("get.docker.com" in l for l in labels)


def test_engines_come_from_distribution_packages():
    assert installer._strategy_apt(_env(), "docker")[-1] == "docker.io"
    assert installer._strategy_apt(_env(), "podman")[-1] == "podman"
    assert installer._strategy_dnf(_env(dnf=True), "podman") == ["sudo", "dnf", "install", "-y", "podman"]
    assert installer._strategy_dnf(_env(dnf=True), "docker")[-1] == "moby-engine"
    # Docker Desktop has an agreement the person accepts: no silent choco install.
    assert installer._strategy_choco(_env(os="windows"), "docker") is None
    assert installer._strategy_choco(_env(os="windows"), "podman") is None


def test_pip_installs_are_pinned_and_constrained(monkeypatch):
    argv = installer._strategy_pip(_env(), "librelane")
    assert argv[:4] == [sys.executable or "python3", "-m", "pip", "install"]
    spec = [a for a in argv if a.startswith("librelane")][0]
    assert spec.startswith("librelane=="), argv
    assert "-c" in argv and Path(argv[argv.index("-c") + 1]).is_file()
    ciel = installer._strategy_pip(_env(), "ciel")
    assert any(a.startswith("ciel==") for a in ciel)


def test_pinned_requirement_matches_pyproject():
    import re

    text = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    want = re.search(r'"librelane==([^"]+)"', text).group(1)
    assert installer.pinned_requirement("librelane") == f"librelane=={want}"


def test_shipped_constraints_equal_the_windows_ones():
    # lanex/data/constraints.txt ships in the wheel for Tools-page installs;
    # windows/setup/make-lock.sh writes both.
    a = (_REPO / "lanex" / "data" / "constraints.txt").read_text(encoding="utf-8")
    b = (_REPO / "windows" / "setup" / "constraints.txt").read_text(encoding="utf-8")
    assert a == b


def test_constraints_env_override_and_bad_file_skipped(tmp_path, monkeypatch, home):
    good = tmp_path / "c.txt"
    good.write_text("librelane==9.9.9\nciel==1.0\n", encoding="utf-8")
    monkeypatch.setenv("LANEX_PIP_CONSTRAINT", str(good))
    assert installer.pip_constraints_file() == str(good)
    bad = tmp_path / "bad.txt"
    bad.write_text("<html>404</html>", encoding="utf-8")
    monkeypatch.setenv("LANEX_PIP_CONSTRAINT", str(bad))
    assert installer.pip_constraints_file() != str(bad)


def test_macos_fallbacks_hand_over_the_vendor_page_and_never_accept_terms(monkeypatch):
    ran = []
    monkeypatch.setattr(installer, "_macos_version", lambda: (14, 0))
    monkeypatch.setattr(installer, "_run_argv", lambda argv, **kw: ran.append(argv) or {"ok": True})
    monkeypatch.setattr(installer, "_emit", lambda *a, **k: None)
    d = installer._install_docker_dmg_darwin()
    assert d["manual"] and d["url"] and d["terms_url"] and ran == []
    p = installer._install_podman_pkg_darwin()
    assert p["manual"] and p["url"] and ran == []


# ============================================================ Tools page: remove

def test_remove_refuses_lanex_own_dependencies(monkeypatch):
    ran = []
    monkeypatch.setattr(installer, "_run_argv", lambda argv, **kw: ran.append(argv) or {"ok": True, "rc": 0})
    monkeypatch.setattr(installer, "_py_module_available", lambda m: True)
    for key in ("librelane", "ciel"):
        res = installer.uninstall_tool(key)
        assert res["ok"] is False and res["own_dependency"] is True
        assert "LanEx" in res["reason"]
    assert ran == []


def test_remove_that_leaves_the_tool_installed_is_not_a_success(monkeypatch):
    # pip "Skipping X as it is not installed" exits 0; apt removes one copy
    # while another stays on PATH. Success is what the probe says.
    ran = []
    monkeypatch.setattr(installer, "detect_environment",
                        lambda: {"os": "linux", "pip": True, "apt": True})
    monkeypatch.setattr(installer, "_check_cmd", lambda n: False)
    monkeypatch.setattr(installer, "_run_argv", lambda argv, **kw: ran.append(argv) or {"ok": True, "rc": 0})
    monkeypatch.setattr(installer, "_verify_install", lambda key: True)
    monkeypatch.setattr(installer, "_installed_path", lambda key: "/home/u/.local/bin/yosys")
    res = installer.uninstall_tool("yosys")
    assert res["ok"] is False and res["still_installed"] is True
    assert "/home/u/.local/bin/yosys" in res["reason"]
    # The pip removal runs in LanEx's own interpreter, not the system python3.
    assert ran[0][0] == (sys.executable or "python3")


def test_remove_succeeds_once_the_tool_is_gone(monkeypatch):
    gone = {"n": 0}
    monkeypatch.setattr(installer, "detect_environment", lambda: {"os": "linux", "apt": True})
    monkeypatch.setattr(installer, "_check_cmd", lambda n: False)
    monkeypatch.setattr(installer, "_run_argv", lambda argv, **kw: gone.__setitem__("n", gone["n"] + 1) or {"ok": True, "rc": 0})
    monkeypatch.setattr(installer, "_verify_install", lambda key: gone["n"] < 1)
    assert installer.uninstall_tool("klayout") == {"ok": True, "method": "apt remove", "key": "klayout"}


# ============================================================ Tools page: logs

def test_install_log_on_disk_and_its_path_on_failure(home, monkeypatch):
    sent = []
    monkeypatch.setattr(installer.bus, "emit", lambda kind, payload: sent.append((kind, payload)))
    assert installer._begin_job("pdk:sky130A")
    try:
        installer._emit("installer_started", {"key": "pdk:sky130A", "argv": ["ciel", "fetch"]})
        installer._emit("installer_line", {"key": "pdk:sky130A", "line": "downloading…"})
        installer._emit("installer_error", {"key": "pdk:sky130A", "message": "disk full"})
    finally:
        installer._end_job("pdk:sky130A")
    path = home / "logs" / "install" / "pdk-sky130A.log"
    text = path.read_text(encoding="utf-8")
    assert "$ ciel fetch" in text and "downloading" in text and "ERROR: disk full" in text
    err = [p for k, p in sent if k == "installer_error"][0]
    assert err["log_path"] == str(path)
    # The next attempt starts a new log and keeps the previous one.
    assert installer._begin_job("pdk:sky130A")
    installer._end_job("pdk:sky130A")
    assert (home / "logs" / "install" / "pdk-sky130A.prev.log").is_file()


# ============================================================ Tools page: pull retry

def _pull_env(monkeypatch, results):
    calls = []
    done = []

    def fake_run(argv, **kw):
        calls.append(kw)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(installer, "_run_argv", fake_run)
    monkeypatch.setattr(installer.time, "sleep", lambda s: None)
    monkeypatch.setattr(installer, "_emit", lambda kind, p: done.append((kind, p)))
    t = {"now": 1000.0}

    def clock():
        t["now"] += 100.0
        return t["now"]

    monkeypatch.setattr(installer.time, "time", clock)
    return calls, done


def test_pull_retries_a_dropped_connection(monkeypatch):
    calls, _ = _pull_env(monkeypatch, [
        {"ok": False, "rc": 1, "output": ["net/http: TLS handshake timeout"]},
        {"ok": True, "rc": 0, "output": ["Status: Downloaded"]},
    ])
    res = installer._pull_with_retries(["docker", "pull", "x"], "docker", "container:image")
    assert res["ok"] and res["attempts"] == 2
    assert all(kw.get("emit_done") is False for kw in calls)
    assert all(kw.get("stall_s") for kw in calls)


def test_pull_stops_on_a_permanent_failure(monkeypatch):
    calls, _ = _pull_env(monkeypatch, [{"ok": False, "rc": 1, "output": ["manifest unknown"]}])
    res = installer._pull_with_retries(["docker", "pull", "x"], "docker", "container:image")
    assert not res["ok"] and len(calls) == 1


def test_pull_gives_up_after_a_bounded_number_of_attempts(monkeypatch):
    calls, _ = _pull_env(monkeypatch, [{"ok": False, "rc": 1, "output": ["connection reset"]}])
    res = installer._pull_with_retries(["docker", "pull", "x"], "docker", "container:image")
    assert not res["ok"] and len(calls) == installer._PULL_MAX_ATTEMPTS


# ============================================================ Tools page: PDK folder

def test_pdk_folder_is_kept_for_the_next_start(home, tmp_path, monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    target = tmp_path / "bigdisk" / "pdks"
    res = installer.set_pdk_root(str(target))
    assert res["ok"] and res["pdk_root"] == str(target) and res["source"] == "saved"
    assert target.is_dir()
    assert json.loads((home / "settings.json").read_text())["pdk_root"] == str(target)
    # "Restart": a fresh process has no PDK_ROOT; the saved folder comes back.
    monkeypatch.delenv("PDK_ROOT", raising=False)
    assert installer.apply_saved_pdk_root() == str(target)
    assert os.environ["PDK_ROOT"] == str(target)
    # Empty goes back to the default and forgets the choice.
    res = installer.set_pdk_root("")
    assert res["source"] == "default" and "PDK_ROOT" not in os.environ
    assert "pdk_root" not in json.loads((home / "settings.json").read_text())


def test_pdk_folder_keeps_other_settings_and_refuses_spaces(home, tmp_path, monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    (home / "settings.json").write_text(json.dumps({"thread_sizing": False}))
    assert installer.set_pdk_root(str(tmp_path / "with space"))["ok"] is False
    installer.set_pdk_root(str(tmp_path / "p"))
    assert json.loads((home / "settings.json").read_text())["thread_sizing"] is False


def test_environment_pdk_root_wins_over_the_saved_one(home, tmp_path, monkeypatch):
    (home / "settings.json").write_text(json.dumps({"pdk_root": str(tmp_path / "saved")}))
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "env"))
    assert installer.apply_saved_pdk_root() is None
    assert os.environ["PDK_ROOT"] == str(tmp_path / "env")
    assert installer.pdk_root_info()["source"] == "environment"


# ============================================================ Tools page: half-unpacked PDK

def _ciel_tree(root: Path, family: str, ver: str, variant: str, *, incomplete: bool) -> Path:
    vdir = root / "ciel" / family / "versions" / ver
    lib = vdir / variant / "libs.ref" / "sc" / "lib"
    lib.mkdir(parents=True)
    (lib / "x.lib").write_text("library", encoding="utf-8")
    if incomplete:
        Path(str(vdir) + pdk_mod.INCOMPLETE_SUFFIX).write_text("in progress")
    else:
        # ciel writes SOURCES into a variant once every archive has unpacked.
        (vdir / variant / "SOURCES").write_text(f"open_pdks {ver}\n", encoding="utf-8")
    (root / variant).symlink_to(vdir / variant)
    return vdir


@pytest.mark.skipif(os.name != "posix", reason="ciel enables variants with symlinks")
def test_half_unpacked_variant_is_not_installed(tmp_path, monkeypatch):
    root = tmp_path / "pdks"
    root.mkdir()
    _ciel_tree(root, "sky130", "abc", "sky130A", incomplete=True)
    _ciel_tree(root, "gf180mcu", "def", "gf180mcuD", incomplete=False)
    assert pdk_mod.variant_incomplete(root / "sky130A") is True
    assert pdk_mod.variant_incomplete(root / "gf180mcuD") is False
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setattr(pdk_mod, "_candidate_pdk_roots", lambda: [root])
    names = [p["name"] for p in pdk_mod.list_pdks()]
    assert "sky130A" not in names and "gf180mcuD" in names
    local = pdk_mod._check_local_ready("sky130A", "sc", [root])
    assert local["ready"] is False and any("complete" in m for m in local["missing"])


@pytest.mark.skipif(os.name != "posix", reason="ciel enables variants with symlinks")
def test_tools_page_lists_a_half_unpacked_variant_as_incomplete(tmp_path, monkeypatch):
    from lanex.controller import tools

    root = tmp_path / "pdks"
    root.mkdir()
    _ciel_tree(root, "sky130", "abc", "sky130A", incomplete=True)
    monkeypatch.setattr(tools, "_pdk_roots", lambda: [root])
    monkeypatch.setattr(tools, "build_pdk_catalog", lambda: {"sky130A": {"family": "sky130"}})
    monkeypatch.setattr(tools, "container_status", lambda force=False: {})
    monkeypatch.setattr(tools, "EDA_TOOLS", [])
    monkeypatch.setattr(tools, "_probe", lambda b, f: {"installed": False, "path": "", "version": "", "error": ""})
    out = tools._check_tools_uncached()
    assert out["pdk"]["installed_pdks"] == []
    assert out["pdk"]["incomplete_pdks"] == ["sky130A"]


def test_running_pdk_installs_are_reported_live(monkeypatch):
    from lanex.controller import tools

    with installer._in_progress_lock:
        installer._in_progress.add("pdk:ihp-sg13g2")
    try:
        out = tools._with_live_jobs({"pdk": {"installed_pdks": []}})
        assert out["pdk"]["installing_pdks"] == ["ihp-sg13g2"]
    finally:
        with installer._in_progress_lock:
            installer._in_progress.discard("pdk:ihp-sg13g2")


# ============================================================ G17 layer names

_IHP_LEF = """
PROPERTYDEFINITIONS
  LAYER contactLimit INTEGER ;
END PROPERTYDEFINITIONS
LAYER GatPoly
  TYPE MASTERSLICE ;
END GatPoly
LAYER Metal1
  TYPE\t\tROUTING ;
END Metal1
LAYER Via1
  TYPE\tCUT ;
END Via1
LAYER Metal2
  TYPE ROUTING ;
END Metal2
LAYER TopMetal1
  TYPE ROUTING ;
END TopMetal1
VIA Via1_X DEFAULT
  LAYER Metal1 ;
  LAYER Via1 ;
  LAYER Metal2 ;
END Via1_X
"""


def test_routing_layers_read_from_the_tech_lef(tmp_path, monkeypatch):
    root = tmp_path / "pdks"
    lef_dir = root / "ihp-sg13g2" / "libs.ref" / "sg13g2_stdcell" / "lef"
    lef_dir.mkdir(parents=True)
    (lef_dir / "sg13g2_tech.lef").write_text(_IHP_LEF, encoding="utf-8")
    monkeypatch.setattr(pdk_mod, "_candidate_pdk_roots", lambda: [root])
    res = pdk_mod.routing_layers("ihp-sg13g2", "sg13g2_stdcell")
    assert res["layers"] == ["Metal1", "Metal2", "TopMetal1"]
    assert res["source"].endswith("sg13g2_tech.lef")
    # Not installed: an empty list (the frontend's table covers it).
    assert pdk_mod.routing_layers("gf180mcuD")["layers"] == []


def test_gf180_tlef_names_are_capitalised(tmp_path, monkeypatch):
    root = tmp_path / "pdks"
    d = root / "gf180mcuA" / "libs.ref" / "gf180mcu_fd_sc_mcu7t5v0" / "techlef"
    d.mkdir(parents=True)
    (d / "gf180mcu_fd_sc_mcu7t5v0__nom.tlef").write_text(
        "".join(f"LAYER Metal{i}\n  TYPE ROUTING ;\nEND Metal{i}\n" for i in (1, 2, 3)), encoding="utf-8")
    # A third-party library whose tech LEF lists five metals on every variant
    # (seen: gf180mcu_as_sc_mcu7t3v3) sorts first by name but is not the
    # variant's truth; the foundry library is read first.
    other = root / "gf180mcuA" / "libs.ref" / "gf180mcu_as_sc_mcu7t3v3" / "techlef"
    other.mkdir(parents=True)
    (other / "gf180mcu_as_sc_mcu7t3v3__nom.tlef").write_text(
        "".join(f"LAYER Metal{i}\n  TYPE ROUTING ;\nEND Metal{i}\n" for i in range(1, 6)), encoding="utf-8")
    monkeypatch.setattr(pdk_mod, "_candidate_pdk_roots", lambda: [root])
    assert pdk_mod.routing_layers("gf180mcuA")["layers"] == ["Metal1", "Metal2", "Metal3"]
    # The design's own library, when named, is the one read.
    assert len(pdk_mod.routing_layers("gf180mcuA", "gf180mcu_as_sc_mcu7t3v3")["layers"]) == 5


# ============================================================ 5a update check

@pytest.mark.parametrize("a,b", [
    ("v1.1.0", "1.0.0"), ("1.0.1", "1.0.0"), ("1.0.0", "1.0.0-rc.1"), ("1.0.0", "1.0.0.dev3"),
    ("1.0.0-rc.1", "1.0.0-beta.2"), ("1.0.0.post1", "1.0.0"), ("2.0", "1.9.9"), ("v1.0.1", "1.0.1.dev2+g1234"),
])
def test_version_order(a, b):
    assert updatecheck.parse_version(a) > updatecheck.parse_version(b)
    assert updatecheck.is_newer(a, b)
    assert not updatecheck.is_newer(b, a)


def test_versions_that_say_nothing():
    assert updatecheck.parse_version("v1.0.0") == updatecheck.parse_version("1.0")
    assert not updatecheck.is_newer("v1.0.0", "1.0.0")
    assert not updatecheck.is_newer("v9.0.0", "0.0.0+unknown")   # a bare checkout: silent
    assert not updatecheck.is_newer("nightly", "1.0.0")
    assert updatecheck.parse_version("garbage") is None


def _release(tag="v1.1.0", **kw):
    d = {"tag_name": tag, "name": "LanEx " + tag, "draft": False, "prerelease": False,
         "html_url": f"https://github.com/lanex-eda/lanex/releases/tag/{tag}",
         "published_at": "2026-12-01T10:00:00Z",
         "assets": [{"name": "LanEx-Setup-1.1.0.exe",
                     "browser_download_url": f"https://github.com/lanex-eda/lanex/releases/download/{tag}/LanEx-Setup-1.1.0.exe"}]}
    d.update(kw)
    return d


def test_release_answer_is_validated():
    r = updatecheck._release_from_json(_release())
    assert r["version"] == "1.1.0" and r["windows_installer"].endswith(".exe")
    assert updatecheck._release_from_json(_release(draft=True)) is None
    assert updatecheck._release_from_json(_release(prerelease=True)) is None
    assert updatecheck._release_from_json(_release(tag="v1; rm -rf /")) is None
    odd = updatecheck._release_from_json(_release(html_url="https://evil.example/x",
                                                  assets=[{"name": "a.exe", "browser_download_url": "https://evil.example/a.exe"}]))
    assert odd["url"].startswith("https://github.com/lanex-eda/lanex/releases/")
    assert odd["windows_installer"] == ""


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_sends_a_plain_request_once_a_day(home, monkeypatch):
    import urllib.request

    seen = []

    def fake_open(req, timeout=None, context=None):
        seen.append(req)
        return _Resp(json.dumps(_release()).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(updatecheck, "current_version", lambda: "1.0.0")
    s = updatecheck.check(now=1_000_000.0)
    assert s["newer"] and s["latest"]["version"] == "1.1.0" and s["how"]
    assert len(seen) == 1
    req = seen[0]
    assert req.full_url == "https://api.github.com/repos/lanex-eda/lanex/releases/latest"
    assert req.get_method() == "GET" and req.data is None
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers == {"user-agent": "LanEx-update-check", "accept": "application/vnd.github+json"}
    # Within the day: answered from the cache, no request.
    updatecheck.check(now=1_000_000.0 + 3600)
    assert len(seen) == 1
    # A day later: asks again. force asks now.
    updatecheck.check(now=1_000_000.0 + 86400 + 1)
    updatecheck.check(force=True, now=1_000_000.0 + 86400 + 2)
    assert len(seen) == 3


def test_offline_fails_silently_and_keeps_the_last_answer(home, monkeypatch):
    import urllib.request

    monkeypatch.setattr(updatecheck, "current_version", lambda: "1.0.0")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(_release()).encode()))
    updatecheck.check(now=10.0)

    def offline(*a, **k):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    s = updatecheck.check(force=True, now=20.0)
    assert s["last_ok"] is False and s["error"] == "OSError"
    assert s["latest"]["version"] == "1.1.0" and s["newer"]
    # The failed attempt counts: no retry on every page load.
    calls = []
    monkeypatch.setattr(updatecheck, "fetch_latest", lambda timeout=0: calls.append(1) or {"ok": False})
    updatecheck.check(now=30.0)
    assert calls == []


def test_status_never_goes_online(home, monkeypatch):
    monkeypatch.setattr(updatecheck, "fetch_latest", lambda *a, **k: pytest.fail("went online"))
    s = updatecheck.status()
    assert s["latest"] is None and s["newer"] is False and s["checked_at"] == 0


def test_how_to_update_per_platform(home, monkeypatch):
    rel = updatecheck._release_from_json(_release())
    monkeypatch.setattr(platform_env, "is_wsl", lambda: True)
    (home / "runtime.json").write_text("{}")
    win = updatecheck.how_to_update(rel)
    assert win["platform"] == "windows" and win["download"].endswith(".exe")
    (home / "runtime.json").unlink()
    unix = updatecheck.how_to_update(rel)
    assert unix["platform"] == "unix"
    assert unix["command"] == ("curl -fsSL https://raw.githubusercontent.com/lanex-eda/lanex/v1.1.0/"
                               "scripts/install.sh | LANEX_REF=v1.1.0 bash")


def test_update_check_route_get_reads_post_checks(home, monkeypatch):
    from lanex.server import routes

    sent = []

    class H:
        def __init__(self, command, body=None):
            self.command = command
            self._body = body or {}
            self.path = "/api/update-check"

        def _send_json(self, data, status):
            sent.append((data, status))

    monkeypatch.setattr(updatecheck, "fetch_latest", lambda *a, **k: pytest.fail("GET went online"))
    routes.h_update_check(H("GET"))
    assert sent[-1][1] == 200 and sent[-1][0]["data"]["newer"] is False
    asked = []
    monkeypatch.setattr(updatecheck, "check", lambda force=False: asked.append(force) or {"newer": False})
    routes.h_update_check(H("POST", {"force": True}))
    assert asked == [True]
