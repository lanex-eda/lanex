# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""windows/provision/network-apply.py: the Windows proxy and company certificates
written into LanEx's Linux, into a scratch root (never the real /etc)."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "windows" / "provision" / "network-apply.py"


@pytest.fixture(scope="module")
def na():
    spec = importlib.util.spec_from_file_location("network_apply", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snap(**kw):
    s = {"schema": 1, "mode": "pac", "proxy": {"http": "http://proxy.corp.example:8080/",
                                                "https": "http://proxy.corp.example:8080/"},
         "noProxy": "*.corp.example,10.0.0.0/8", "perHost": {"snapshot.ubuntu.com": "DIRECT"},
         "networkingMode": "nat"}
    s.update(kw)
    return s


def _pem(der: bytes) -> str:
    b = base64.b64encode(der).decode()
    return "-----BEGIN CERTIFICATE-----\n" + b + "\n-----END CERTIFICATE-----\n"


def _root(tmp_path: Path, known: bytes = b"public-root") -> Path:
    r = tmp_path / "root"
    (r / "etc" / "ssl" / "certs").mkdir(parents=True)
    (r / "etc" / "ssl" / "certs" / "ca-certificates.crt").write_text(_pem(known))
    return r


def test_proxy_reaches_apt_docker_and_the_environment(na, tmp_path):
    root = _root(tmp_path)
    res = na.apply(_snap(), "", root=str(root), restart=False, gateway="")
    assert res["ok"] and res["proxy_host"] == "proxy.corp.example:8080"
    env = (root / "etc/lanex/proxy.env").read_text()
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        assert f"{k}=" in env
    assert "localhost,127.0.0.1,::1" in env and "*.corp.example" in env
    apt = (root / "etc/apt/apt.conf.d/90lanex-proxy").read_text()
    assert 'Acquire::https::Proxy "http://proxy.corp.example:8080/";' in apt
    assert 'Acquire::https::Proxy::snapshot.ubuntu.com "DIRECT";' in apt
    dock = (root / "etc/systemd/system/docker.service.d/90-lanex-proxy.conf").read_text()
    assert dock.startswith("# Managed by LanEx") and '"HTTPS_PROXY=http://proxy.corp.example:8080/"' in dock
    assert (root / "etc/systemd/system/containerd.service.d/90-lanex-proxy.conf").is_file()
    applied = json.loads((root / "etc/lanex/network.applied.json").read_text())
    assert applied["mode"] == "pac" and "docker" in applied["changed"]
    # Same input again: nothing changes.
    assert na.apply(_snap(), "", root=str(root), restart=False, gateway="")["changed"] == []


def test_password_in_proxy_is_refused(na, tmp_path):
    with pytest.raises(na.Refused):
        na.validate(_snap(proxy={"http": "http://bob:secret@proxy:8080/", "https": ""}))
    with pytest.raises(na.Refused):
        na.validate(_snap(proxy={"http": "socks5://proxy:1080/", "https": ""}))


def test_loopback_proxy_becomes_the_windows_host_in_nat(na, tmp_path):
    root = _root(tmp_path)
    res = na.apply(_snap(proxy={"http": "http://127.0.0.1:3128/", "https": "http://127.0.0.1:3128/"}),
                   "", root=str(root), restart=False, gateway="172.29.48.1")
    assert res["proxy_host"] == "172.29.48.1:3128" and res["notes"]
    res = na.apply(_snap(proxy={"http": "http://127.0.0.1:3128/", "https": "http://127.0.0.1:3128/"},
                         networkingMode="mirrored"), "", root=str(root), restart=False, gateway="172.29.48.1")
    assert res["proxy_host"] == "127.0.0.1:3128"


def test_only_certificates_ubuntu_lacks_are_added(na, tmp_path):
    root = _root(tmp_path, known=b"public-root")
    certs = _pem(b"public-root") + _pem(b"contoso-inspection-root")
    res = na.apply(_snap(), certs, root=str(root), restart=False, gateway="")
    files = list((root / "usr/local/share/ca-certificates/lanex").glob("*.crt"))
    assert res["certificates"] == 1 and len(files) == 1
    env = (root / "etc/lanex/proxy.env").read_text()
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in env and "PIP_CERT=" in env


def test_direct_connection_removes_only_lanex_files(na, tmp_path):
    root = _root(tmp_path)
    na.apply(_snap(), _pem(b"corp"), root=str(root), restart=False, gateway="")
    foreign = root / "etc/systemd/system/docker.service.d/http-proxy.conf"
    foreign.write_text("[Service]\nEnvironment=HTTP_PROXY=http://mine:1/\n")
    res = na.apply({"schema": 1, "mode": "none", "proxy": {"http": "", "https": ""}}, "",
                   root=str(root), restart=False, gateway="")
    assert res["mode"] == "none"
    assert not (root / "etc/lanex/proxy.env").exists()
    assert not (root / "etc/apt/apt.conf.d/90lanex-proxy").exists()
    assert not (root / "etc/systemd/system/docker.service.d/90-lanex-proxy.conf").exists()
    assert not (root / "usr/local/share/ca-certificates/lanex").exists()
    assert foreign.read_text().startswith("[Service]")     # not ours: never touched


def test_cli_last_line_is_json(na, tmp_path, capsys):
    root = _root(tmp_path)
    snap = tmp_path / "network.json"
    snap.write_text(json.dumps(_snap(proxy={"http": "http://u:p@x:1/", "https": ""})))
    assert na.main(["--input", str(snap), "--root", str(root), "--no-restart"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert not out["ok"] and "never stores" in out["error"]
    assert not (root / "etc/lanex").exists()


def test_network_ps1_never_writes_credentials_or_pac_address():
    text = (REPO / "windows" / "setup" / "network.ps1").read_text(encoding="utf-8")
    # The snapshot has no field for the PAC URL, and proxy URLs lose their user info.
    assert "AutoConfigURL" in text and "pacUrl" not in text.lower().replace("pacset", "")
    assert "Remove-UserInfo" in text
    assert "ServerCertificateValidationCallback" in text and "return errors == SslPolicyErrors.None" in text
