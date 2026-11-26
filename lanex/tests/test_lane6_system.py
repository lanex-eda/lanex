# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Audit phase 5: lanex doctor, .wslconfig memory, clean-up and per-variant PDK
removal, the update check's ETag and "skip this version", the network
settings the app loads, and the new routes' methods."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from lanex.controller import doctor, netsettings, syscleanup, updatecheck, wslconfig


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LANEX_HOME", str(h))
    return h


# ---------------------------------------------------------------- doctor

def _fake_checks(statuses):
    def make(cid, st):
        return lambda ctx: doctor.result(cid, doctor.TITLES[cid], st, f"{cid} says {st}",
                                         next_text="do this" if st in ("warn", "fail") else "")
    return {cid: make(cid, statuses.get(cid, "ok")) for cid in doctor.CHECK_ORDER}


def test_doctor_runs_every_check_in_order(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", _fake_checks({}))
    rep = doctor.run(offline=True)
    assert [c["id"] for c in rep["checks"]] == list(doctor.CHECK_ORDER)
    assert rep["schema"] == 1 and rep["summary"]["ok"] == len(doctor.CHECK_ORDER)
    assert doctor.exit_code(rep) == 0


def test_doctor_exit_codes_and_summary_counts_marks(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", _fake_checks({"image": "fail", "memory": "warn", "tools": "info"}))
    rep = doctor.run()
    assert doctor.exit_code(rep) == 1
    text = doctor.to_text(rep)
    # The summary counts exactly the marks above it (flutter #152271).
    assert text.count("[fail]") == 1 and text.count("[warn]") == 1
    assert "1 problem, 1 warning." in text
    # Every problem has its next step under it.
    assert text.count("Next: do this") == 2
    monkeypatch.setattr(doctor, "CHECKS", _fake_checks({"memory": "warn"}))
    rep = doctor.run()
    assert doctor.exit_code(rep) == 0 and doctor.exit_code(rep, strict=True) == 1
    assert "No problems found." not in doctor.to_text(rep)


def test_doctor_check_that_crashes_or_hangs_is_a_warning(monkeypatch):
    checks = _fake_checks({})

    def boom(ctx):
        raise RuntimeError("kaput")

    checks["disk"] = boom
    monkeypatch.setattr(doctor, "CHECKS", checks)
    monkeypatch.setitem(doctor.LIMITS, "files", 0.05)

    def slow(ctx):
        import time
        time.sleep(1)
        return doctor.result("files", "x", "ok", "late")

    checks["files"] = slow
    rep = doctor.run()
    by = {c["id"]: c for c in rep["checks"]}
    assert by["disk"]["status"] == "warn" and "kaput" in by["disk"]["summary"]
    assert by["files"]["status"] == "warn" and "did not finish" in by["files"]["summary"]


def test_doctor_main_json_keeps_exit_code(monkeypatch, capsys, home):
    monkeypatch.setattr(doctor, "CHECKS", _fake_checks({"engine": "fail"}))
    code = doctor.main(["--json", "--offline"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["summary"]["exit"] == 1 and out["summary"]["fail"] == 1
    assert list((home / "logs").glob("doctor-*.txt"))


def test_doctor_main_crash_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run", lambda **k: (_ for _ in ()).throw(ValueError("x")))
    assert doctor.main([]) == 2
    assert "LanEx doctor crashed" in capsys.readouterr().err


def test_cli_dispatches_doctor_before_flags(monkeypatch):
    from lanex import cli

    seen = []
    monkeypatch.setattr(doctor, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["doctor", "--json"]) == 0
    assert seen == [["--json"]]


def test_share_form_hides_user_and_proxy():
    s = doctor.share_text("proxy http://alice:pw@proxy.corp:8080 at /mnt/c/Users/alice/x and "
                          + str(Path.home()) + "/y; ghcr.io https://ghcr.io/v2/")
    assert "alice" not in s and "proxy.corp" not in s and "<proxy>" in s
    assert "/mnt/c/Users/<user>/x" in s and "~/y" in s and "https://ghcr.io/v2/" in s


def test_doctor_vhdx_drive_from_setup_state(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "windows_state",
                        lambda: {"appliance": {"basePath": r"D:\LanEx\wsl\lanex"}})
    real = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: True if str(self) == "/mnt/d" else real(self))
    assert doctor.vhdx_drive() == ("D:", Path("/mnt/d"))


def test_doctor_disk_check_words(monkeypatch):
    monkeypatch.setattr(doctor, "storage_target",
                        lambda: {"label": "C:", "path": "/mnt/c", "windows": True, "free_gb": 3.1, "total_gb": 476})
    r = doctor.check_disk({})
    assert r["status"] == "fail" and "Only 3.1 GB free on C:" in r["summary"] and r["next"]["text"]
    monkeypatch.setattr(doctor, "storage_target",
                        lambda: {"label": "C:", "path": "/mnt/c", "windows": True, "free_gb": 9.0, "total_gb": 476})
    assert doctor.check_disk({})["status"] == "warn"


def test_doctor_network_categories(monkeypatch):
    monkeypatch.setattr(doctor, "_probe_url", lambda url, timeout: (
        {"ok": False, "category": "tls", "error": "CERTIFICATE_VERIFY_FAILED"} if "ghcr" in url
        else {"ok": True, "status": 200}))
    r = doctor.check_network({})
    assert r["status"] == "fail" and "ghcr.io's certificate" in r["summary"]
    assert "5 of 6" in r["summary"]
    assert doctor.check_network({"offline": True})["status"] == "skip"


# ---------------------------------------------------------------- .wslconfig

def test_wslconfig_sizes_follow_wsl_parser():
    assert wslconfig.parse_size_gb("8GB") == 8
    assert wslconfig.parse_size_gb("8192MB") == 8
    assert wslconfig.parse_size_gb("8gb") is None      # WSL: "Invalid memory string"
    assert wslconfig.parse_size_gb("7.5GB") is None
    assert wslconfig.format_gb(12) == "12GB"


@pytest.mark.parametrize("before,gb,after", [
    ("", 12, "[wsl2]\nmemory=12GB\n"),
    ("[boot]\nsystemd=true\n", 12, "[boot]\nsystemd=true\n\n[wsl2]\nmemory=12GB\n"),
    ("# mine\n[wsl2]\nprocessors=4\n", 8, "# mine\n[wsl2]\nmemory=8GB\nprocessors=4\n"),
    ("[wsl2]\nmemory = 4GB  # small\nswap=0\n", 16, "[wsl2]\nmemory = 16GB  # small\nswap=0\n"),
    ("[WSL2]\nMemory=4GB\n[experimental]\nmemory=9GB\n", 6, "[WSL2]\nMemory=6GB\n[experimental]\nmemory=9GB\n"),
    ("[wsl2]\r\nmemory=4GB\r\nswap=0\r\n", 10, "[wsl2]\r\nmemory=10GB\r\nswap=0\r\n"),
    ("[wsl2]\nmemory=4GB\nswap=0\n", None, "[wsl2]\nswap=0\n"),
])
def test_wslconfig_edit_keeps_everything_else(before, gb, after):
    assert wslconfig.set_memory_text(before, gb) == after


def test_wslconfig_write_keeps_encoding_and_backs_up(tmp_path, home):
    p = tmp_path / ".wslconfig"
    text = "# corp\r\n[wsl2]\r\nnetworkingMode=mirrored\r\n"
    p.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    res = wslconfig.write_memory(12, path=p, total_gb=32)
    raw = p.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    assert raw[2:].decode("utf-16-le") == "# corp\r\n[wsl2]\r\nmemory=12GB\r\nnetworkingMode=mirrored\r\n"
    assert res["changed"] and Path(res["backup"]).read_bytes() == b"\xff\xfe" + text.encode("utf-16-le")
    assert wslconfig.read_memory(p)["gb"] == 12
    with pytest.raises(wslconfig.WslConfigError):
        wslconfig.write_memory(64, path=p, total_gb=32)
    with pytest.raises(wslconfig.WslConfigError):
        wslconfig.write_memory(1, path=p, total_gb=32)


def test_wslconfig_refuses_unknown_encoding(tmp_path, home):
    p = tmp_path / ".wslconfig"
    p.write_bytes(b"[wsl2]\n\xff\xfe\xfa bad")
    with pytest.raises(wslconfig.WslConfigError):
        wslconfig.write_memory(8, path=p)
    assert p.read_bytes() == b"[wsl2]\n\xff\xfe\xfa bad"


# ---------------------------------------------------------------- clean-up

def test_docker_sizes_are_1000_based():
    assert syscleanup.parse_docker_size("7.524GB") == 7_524_000_000
    assert syscleanup.parse_docker_size("512MB") == 512_000_000
    assert syscleanup.parse_docker_size("N/A") is None


def _ciel_tree(root: Path, family="sky130", version="8afc834", variants=("sky130A", "sky130B")):
    vdir = root / "ciel" / family / "versions" / version
    for v in variants:
        d = vdir / v
        (d / "libs.ref" / "lib").mkdir(parents=True)
        (d / "libs.tech").mkdir()
        (d / "libs.ref" / "lib" / "cells.lib").write_text("x" * 1000)
        (d / "SOURCES").write_text("open_pdks " + version)
        (root / v).symlink_to(os.path.relpath(d, root))
    (root / "ciel" / family / "current").write_text(version)
    return vdir


@pytest.fixture()
def pdk_home(tmp_path, monkeypatch, home):
    root = tmp_path / "ciel-home"
    root.mkdir()
    monkeypatch.setattr(syscleanup, "_ciel_homes", lambda: [root])
    monkeypatch.setattr(syscleanup, "_run_active", lambda: False)
    monkeypatch.setattr(syscleanup, "_pinned", lambda fam: "8afc834")
    return root


def test_remove_one_variant_keeps_the_other(pdk_home):
    vdir = _ciel_tree(pdk_home)
    res = syscleanup.remove_pdk_variant("sky130B")
    assert res["ok"] and res["kept"] == ["sky130A"] and not res["whole_version"]
    assert not (pdk_home / "sky130B").exists() and (pdk_home / "sky130A" / "libs.ref").is_dir()
    assert not (vdir / "sky130B").exists()
    assert syscleanup.removed_variants(vdir) == ["sky130B"]


def test_remove_variant_refuses_when_files_are_shared(pdk_home):
    vdir = _ciel_tree(pdk_home)
    # ciel optimize: sky130B links into sky130A.
    (vdir / "sky130B" / "libs.ref" / "shared.lib").symlink_to(vdir / "sky130A" / "libs.ref" / "lib" / "cells.lib")
    with pytest.raises(syscleanup.CleanupError, match="uses files stored in sky130A"):
        syscleanup.remove_pdk_variant("sky130A")
    assert (pdk_home / "sky130A").is_symlink()


def test_remove_variant_refuses_during_a_run(pdk_home, monkeypatch):
    _ciel_tree(pdk_home)
    monkeypatch.setattr(syscleanup, "_run_active", lambda: True)
    with pytest.raises(syscleanup.CleanupError, match="A run is going"):
        syscleanup.remove_pdk_variant("sky130A")


def test_staged_variant_is_adopted(pdk_home, tmp_path):
    vdir = _ciel_tree(pdk_home)
    syscleanup.remove_pdk_variant("sky130B")
    staging = pdk_home / ".lanex-staging-sky130"
    _ciel_tree(staging)
    syscleanup.adopt_staged_variant(staging, pdk_home, "sky130", "8afc834", "sky130B")
    assert (pdk_home / "sky130B" / "SOURCES").is_file()
    assert (pdk_home / "sky130B").resolve() == (vdir / "sky130B").resolve()
    assert syscleanup.removed_variants(vdir) == [] and not staging.exists()


def test_pdk_inventory_and_old_versions(pdk_home, monkeypatch):
    _ciel_tree(pdk_home)
    old = pdk_home / "ciel" / "sky130" / "versions" / "0fe599b" / "sky130A"
    (old / "libs.ref").mkdir(parents=True)
    inv = syscleanup.pdk_inventory()
    fam = inv["families"][0]
    by = {v["version"]: v for v in fam["versions"]}
    assert by["8afc834"]["enabled"] and by["8afc834"]["pinned"]
    assert not by["0fe599b"]["enabled"] and not by["0fe599b"]["variants"][0]["complete"]
    with pytest.raises(syscleanup.CleanupError, match="LibreLane uses"):
        syscleanup.remove_pdk_version("sky130", "8afc834")


def test_variant_without_sources_reads_incomplete(tmp_path):
    from lanex.controller import pdk as pdk_mod

    vdir = _ciel_tree(tmp_path)
    assert not pdk_mod.variant_incomplete(tmp_path / "sky130A")
    (vdir / "sky130A" / "SOURCES").unlink()
    assert pdk_mod.variant_incomplete(tmp_path / "sky130A")


def test_remove_image_never_forces_or_prunes(monkeypatch):
    calls = []
    imgs = {"ok": True, "current_ref": "ghcr.io/librelane/librelane:3.0.4", "images": [
        {"id": "sha256:" + "a" * 64, "short": "a" * 12, "tags": ["ghcr.io/librelane/librelane:3.0.3"],
         "digests": ["ghcr.io/librelane/librelane@sha256:" + "b" * 64], "version": "3.0.3",
         "unique_bytes": 1_200_000_000, "containers": 0, "current": False},
        {"id": "sha256:" + "c" * 64, "short": "c" * 12, "tags": ["ghcr.io/librelane/librelane:3.0.4"],
         "digests": [], "version": "3.0.4", "unique_bytes": 7_500_000_000, "containers": 0, "current": True}]}
    monkeypatch.setattr(syscleanup, "librelane_images", lambda: imgs)
    monkeypatch.setattr(syscleanup, "_engine", lambda: ("docker", False))
    monkeypatch.setattr(syscleanup, "storage_target_free", lambda: None)
    gone = {"done": False}

    def fake_run(argv, timeout=30.0):
        calls.append(argv)
        if argv[1:3] == ["image", "rm"]:
            gone["done"] = True
            return 0, "", ""
        if argv[1:3] == ["image", "inspect"]:
            return (1 if gone["done"] else 0), "", ""
        return 0, "", ""

    monkeypatch.setattr(syscleanup, "_run", fake_run)
    with pytest.raises(syscleanup.CleanupError, match="LanEx runs with this image"):
        syscleanup.remove_image("sha256:" + "c" * 64)
    res = syscleanup.remove_image("sha256:" + "a" * 64)
    assert res["ok"] and res["freed_bytes"] == 1_200_000_000
    flat = [" ".join(c) for c in calls]
    assert not any("-f" in c.split() or "prune" in c for c in flat)
    assert "docker image rm ghcr.io/librelane/librelane:3.0.3" in flat


# ---------------------------------------------------------------- update check

class _Resp(io.BytesIO):
    def __init__(self, body, etag=""):
        super().__init__(body)
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _release(tag="v1.1.0"):
    return {"tag_name": tag, "html_url": f"https://github.com/lanex-eda/lanex/releases/tag/{tag}",
            "name": tag, "published_at": "2026-11-20T00:00:00Z", "body": "## New\n<b>Faster</b> runs\x07",
            "assets": []}


def test_update_check_sends_etag_and_keeps_release_on_304(home, monkeypatch):
    import urllib.error
    import urllib.request

    monkeypatch.setattr(updatecheck, "current_version", lambda: "1.0.0")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(_release()).encode(), '"abc"'))
    s = updatecheck.check(now=10.0)
    assert s["newer"] and s["latest"]["notes"] == "## New\nFaster runs"
    sent = []

    def not_modified(req, *a, **k):
        sent.append(req.headers.get("If-none-match"))
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", not_modified)
    s = updatecheck.check(force=True, now=20.0)
    assert sent == ['"abc"'] and s["last_ok"] and s["latest"]["version"] == "1.1.0"


def test_skip_this_version(home, monkeypatch):
    import urllib.request

    monkeypatch.setattr(updatecheck, "current_version", lambda: "1.0.0")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(_release()).encode()))
    updatecheck.check(now=10.0)
    s = updatecheck.skip_version("1.1.0")
    assert s["newer"] and s["skipped"]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(_release("v1.2.0")).encode()))
    s = updatecheck.check(force=True, now=20.0)
    assert s["newer"] and not s["skipped"]   # a newer release still shows


# ---------------------------------------------------------------- network settings

def test_proxy_env_parse_and_load_never_overrides(tmp_path):
    f = tmp_path / "proxy.env"
    f.write_text("# Managed by LanEx\nHTTPS_PROXY=http://proxy.corp:8080/\nexport NO_PROXY='localhost,127.0.0.1'\n"
                 "EVIL=1\nhttp_proxy=\"http://proxy.corp:8080/\"\n")
    assert netsettings.parse_env_file(f.read_text()) == {
        "HTTPS_PROXY": "http://proxy.corp:8080/", "NO_PROXY": "localhost,127.0.0.1",
        "http_proxy": "http://proxy.corp:8080/"}
    env = {"HTTPS_PROXY": "http://mine:3128/"}
    added = netsettings.load_proxy_env(f, env)
    assert env["HTTPS_PROXY"] == "http://mine:3128/" and "HTTPS_PROXY" not in added
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


# ---------------------------------------------------------------- routes

def test_new_routes_have_methods():
    from lanex.server import routes

    m = routes.ROUTE_METHODS
    assert m["/api/doctor"] == ("GET",) and m["/api/system/storage"] == ("GET",)
    assert m["/api/system/cleanup/image"] == ("POST",) and m["/api/pdk/remove-variant"] == ("POST",)
    assert m["/api/pdk/remove-version"] == ("POST",) and m["/api/update-check/skip"] == ("POST",)
    assert m["/api/system/wsl-memory"] == ("GET", "POST") and m["/api/system/network"] == ("GET", "POST")
    prefixes = [p for p, _ in routes.ROUTES]
    assert len(prefixes) == len(set(prefixes))
