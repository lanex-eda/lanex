# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""Regression tests for security review R2 (findings 2, 3, 5, 6, 7, 8, 9, 10),
each built from the reviewer's proof script, plus the class each one stood
for. Threat model: a project folder (its config, file names, run folders)
may come from someone else; opening or viewing it must not run its code or
write outside it."""
from __future__ import annotations

import contextlib
import inspect
import importlib
import json
import os
import re
import shutil
import socket
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from lanex.controller import (custommacros, desktop, editor, hdlsearch, history, macrolib, odbexport, quoting,
                              runwarnings, safefs, schematic, signoff)
from lanex.server import routes
from lanex.server.app import SANDBOX_CSP, is_document_type, make_server

GOLD = Path(__file__).parent / "goldens" / "sta_paths_run"
XSS = "<img src=x onerror=\"fetch('/api/about').then(r=>r.text()).then(t=>document.title='XSS:'+t.length)\">"


# ------------------------------------------------------------------ a mini Tcl reader
def tcl_words(cmd: str):
    """One Tcl command split the way Tcl's parser does (the dodekalogue, for
    the forms LanEx writes): ``(words, substitutions)``. A ``[`` or ``$`` that
    Tcl would substitute is recorded instead of run."""
    words, subs, i, n = [], [], 0, len(cmd)

    def bslash(j):
        c = cmd[j + 1]
        if c == "u":
            m = re.match(r"[0-9a-fA-F]{1,4}", cmd[j + 2:])
            return chr(int(m.group(0), 16)), j + 2 + len(m.group(0))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(c, c), j + 2

    while i < n:
        while i < n and cmd[i] in " \t":
            i += 1
        if i >= n:
            break
        if cmd[i] == "{":
            depth, j = 1, i + 1
            while depth:
                if cmd[j] == "\\":
                    j += 2
                    continue
                depth += {"{": 1, "}": -1}.get(cmd[j], 0)
                j += 1
            words.append(cmd[i + 1:j - 1])
            assert j == n or cmd[j] in " \t", "extra characters after close-brace"
            i = j
            continue
        quoted = cmd[i] == '"'
        i += quoted
        out = []
        while i < n:
            c = cmd[i]
            if quoted and c == '"':
                i += 1
                break
            if not quoted and c in " \t":
                break
            if c == "\\":
                ch, i = bslash(i)
                out.append(ch)
                continue
            if c in "[$":
                subs.append(cmd[i:i + 20])
            out.append(c)
            i += 1
        words.append("".join(out))
    return words, subs


NASTY = ["spm[exec touch PWNED].def", "a}b {c", "$env(HOME)", "x\\", "two\nlines", "q\"uote", "semi;colon",
         "#comment", "tab\there", "brace{", "}", "µm © ünï", "", "\\u0041", "plain_name-1.2/x:y"]


@pytest.mark.parametrize("v", NASTY)
def test_tcl_word_reads_back_as_the_same_text_with_nothing_run(v):
    words, subs = tcl_words("puts " + quoting.tcl_word(v))
    assert words == ["puts", v] and subs == []
    words, subs = tcl_words('puts "' + quoting.tcl_escape(v) + '"')
    assert words == ["puts", v] and subs == []
    # inside a braced body that Tcl evaluates later (catch {...})
    body, subs = tcl_words("catch {read_db " + quoting.tcl_word(v) + "} err")
    assert subs == [] and tcl_words(body[1]) == (["read_db", v], [])


# ------------------------------------------------------------------ finding 2: stored XSS
def _run(tmp_path: Path, metrics: dict | None = None) -> Path:
    """A finished-looking run: the STA golden (real OpenSTA reports), final
    metrics, resolved.json."""
    run = tmp_path / "proj" / "runs" / "baseline"
    shutil.copytree(GOLD, run)
    sta = run / "55-openroad-stapostpnr"
    (sta / "state_in.json").write_text("{}")          # a finished step
    shutil.copy(sta / "max_ss_100C_1v60" / "max.rpt", sta / "min_ff_n40C_1v95" / "max.rpt")   # two timed corners
    m = {"design__instance__count__class:sequential_cell": 2, "timing__setup__ws": 4.1, "timing__hold__ws": 0.2,
         "timing__setup_vio__count": 0, "timing__hold_vio__count": 0, "antenna__violating__nets": 0,
         "antenna__violating__pins": 0, "route__drc_errors": 0}
    for c in ("max_ss_100C_1v60", "min_ff_n40C_1v95"):
        m.update({f"timing__setup__ws__corner:{c}": 4.1, f"timing__hold__ws__corner:{c}": 0.2,
                  f"timing__setup_vio__count__corner:{c}": 0, f"timing__hold_vio__count__corner:{c}": 0})
    m.update(metrics or {})
    (run / "final").mkdir(exist_ok=True)
    (run / "final" / "metrics.json").write_text(json.dumps(m))
    (run / "resolved.json").write_text(json.dumps({"DESIGN_NAME": "spm", "PDK": "sky130A", "CLOCK_PORT": "clk",
                                                   "STA_CORNERS": ["nom_tt_025C_1v80", "max_ss_100C_1v60", "max_ff_n40C_1v95"]}))
    return run


def test_p4_metric_payload_is_text_in_report_html(tmp_path):
    """R2 p4 proof A: antenna__violating__nets = <img onerror> landed raw in
    report.html; now it is text in both files."""
    run = _run(tmp_path, {"antenna__violating__nets": XSS, "antenna_diodes_count": XSS, "route__drc_errors": XSS})
    res = signoff.write(run, verdict={"tone": "pass", "title": "Passed", "sub": ""})
    html = (run / res["files"]["html"]).read_text(encoding="utf-8")
    md = (run / res["files"]["md"]).read_text(encoding="utf-8")
    assert "<img src=x" not in html and "onerror=\"fetch" not in html
    assert "&lt;img src=x onerror=&quot;fetch" in html
    assert "<img" not in md.replace("![", "")


def _leaves_to(obj, payload):
    if isinstance(obj, dict):
        return {k: _leaves_to(v, payload) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_leaves_to(v, payload) for v in obj]
    return payload


def test_every_value_in_both_renderers_is_escaped(tmp_path):
    """The class: replace every value of a real report's data with markup and
    a newline; neither renderer may emit it raw, crash, or start a line."""
    run = _run(tmp_path)
    (run / "warning.log").write_text("[STA-1140] library already exists.\n[RSZ-0020] found 2 floating nets.\n")
    rep = signoff.build(run, verdict={"tone": "pass", "title": "Passed", "sub": ""})
    assert rep["timing"]["worst_setup"], "the golden run has a worst path, so its renderer runs"
    assert rep["timing"]["corners"] and rep["warnings"]["groups"] and rep["checks"] and rep["files"]
    bad = _leaves_to(rep, "<x-inj a=1>\n# injected `tick` [link](javascript:x) |")
    html = signoff.render_html(bad)
    assert "<x-inj" not in html
    md = signoff.render_md(bad)
    # Markup may sit only inside code spans, where Markdown shows it as text.
    prose = re.sub(r"(?<![`\\])(`+)(?!`)(.+?)(?<!`)\1(?!`)", "", md)
    assert "<x-inj" not in prose
    assert not re.search(r"(?<!\\)\]\(javascript:", prose), "a link needs an unescaped ]("
    assert not any(ln.startswith("# injected") for ln in md.splitlines())


def test_p4_newline_in_design_name_stays_on_line_one(tmp_path):
    run = _run(tmp_path)
    rep = signoff.build(run)
    rep["identity"]["design"] = "spm\n\n## Passed: tape out now\n<script>x</script>"
    md = signoff.render_md(rep)
    first = md.splitlines()[0]
    assert first.startswith("# Sign-off summary: spm") and "tape out now" in first
    assert "<script>" not in md


# ------------------------------------------------------------------ server: documents are sandboxed
def _pick_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "runs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(routes, "_get_active_design_dir", lambda: str(proj))
    httpd, port = make_server(host="127.0.0.1", port=_pick_port(), token=None)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, proj
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, body=None):
    c = HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body or {}).encode() if method == "POST" else None
    h = {"X-Requested-With": "XMLHttpRequest"}
    if data is not None:
        h.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()


def test_run_folder_html_and_svg_are_served_sandboxed(server):
    port, proj = server
    run = proj / "runs" / "baseline"
    (run / "lanex-report").mkdir(parents=True)
    (run / "lanex-report" / "report.html").write_text("<script>fetch('/api/about')</script>")
    (run / "x.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    (run / "x.htm").write_text("<b>x</b>" * 400)          # big enough to go out gzip-compressed
    (run / "m.json").write_text("{}")
    for rel in ("lanex-report/report.html", "x.svg", "x.htm"):
        st, h, _ = _req(port, "GET", f"/api/run-file?tag=baseline&path={rel}")
        assert st == 200 and h.get("content-security-policy") == SANDBOX_CSP, rel
        assert "sandbox" in SANDBOX_CSP and "allow-scripts" not in SANDBOX_CSP
        assert h.get("x-content-type-options") == "nosniff"
    st, h, _ = _req(port, "GET", "/api/run-file?tag=baseline&path=m.json")
    assert st == 200 and h.get("x-content-type-options") == "nosniff" and "content-security-policy" not in h
    st, h, _ = _req(port, "GET", "/api/run-export?tag=baseline&fmt=html")
    assert h.get("content-security-policy") == SANDBOX_CSP
    # The app's own pages are never sandboxed.
    st, h, _ = _req(port, "GET", "/")
    assert "content-security-policy" not in h or "sandbox" not in h["content-security-policy"]


def test_every_document_type_a_run_file_can_have_is_sandboxed():
    for ext, ct in routes._CONTENT_TYPE_MAP.items():
        base = ct.split(";")[0]
        if base in ("text/html", "image/svg+xml") or base.endswith("xml"):
            assert is_document_type(ct), ext
    assert not is_document_type("image/png") and not is_document_type("application/json")


# ------------------------------------------------------------------ finding 6: links in run folders
def test_p4_report_files_never_write_through_planted_links(tmp_path):
    """R2 p4 proof B: SHA256SUMS and report.md.tmp links in lanex-report
    overwrote two files outside the project."""
    run = _run(tmp_path)
    out = tmp_path / "outside"
    out.mkdir()
    (out / "precious.txt").write_text("precious")
    (out / "victim2.txt").write_text("victim2")
    rd = run / "lanex-report"
    rd.mkdir()
    os.symlink(out / "precious.txt", rd / "SHA256SUMS")
    os.symlink(out / "victim2.txt", rd / "report.md.tmp")
    os.symlink(out / "victim2.txt", rd / "report.html")
    signoff.write(run)
    assert (out / "precious.txt").read_text() == "precious"
    assert (out / "victim2.txt").read_text() == "victim2"
    assert not (rd / "SHA256SUMS").is_symlink() and "report.md" in (rd / "SHA256SUMS").read_text()
    assert not (rd / "report.html").is_symlink()


def test_report_folder_that_is_a_link_is_refused(tmp_path):
    run = _run(tmp_path)
    out = tmp_path / "outside"
    out.mkdir()
    os.symlink(out, run / "lanex-report")
    with pytest.raises(safefs.UnsafePath, match="link"):
        signoff.write(run)
    assert list(out.iterdir()) == []


def test_overlay_cache_link_is_refused_and_nothing_happens_outside(tmp_path, monkeypatch):
    """R2: a .lanex/analysis/<key> link made the export delete files there,
    mount it read-write and rmtree <target>/.cache."""
    run = _run(tmp_path)
    (run / "final" / "odb").mkdir(parents=True)
    (run / "final" / "odb" / "spm.odb").write_bytes(b"odb")
    victim = tmp_path / "home"
    (victim / ".cache").mkdir(parents=True)
    (victim / "manifest.json").write_text("keep")
    (victim / "heat-x.csv").write_text("keep")
    (run / ".lanex").mkdir()
    os.symlink(victim, run / ".lanex" / "analysis")
    with pytest.raises(safefs.UnsafePath):
        odbexport._confined_cache(run.resolve(), "final")
    db = odbexport.database_for(run, "final")
    job = odbexport._Job(run, db)
    monkeypatch.setattr("lanex.controller.tools.resolve_engine", lambda: {"ready": True, "engine": "docker"})
    monkeypatch.setattr("lanex.controller.container_tools._image_present", lambda *a, **k: True)
    started = []
    monkeypatch.setattr(odbexport.subprocess, "Popen", lambda *a, **k: started.append(a) or (_ for _ in ()).throw(OSError("no")))
    job._run()
    assert started == [], "no container is started on a linked cache"
    assert job.error and "link" in job.error
    assert sorted(p.name for p in victim.iterdir()) == [".cache", "heat-x.csv", "manifest.json"]
    assert not (victim / "final").exists() and not (victim / "failed.json").exists()


def test_viewer_script_is_always_ours_and_never_through_a_link(tmp_path, monkeypatch):
    run = _run(tmp_path)
    monkeypatch.setattr("lanex.controller.layoutview._lef_list", lambda cfg: ["/pdk/tech.lef", "/pdk/cells.lef"])
    monkeypatch.setattr("lanex.controller.layoutview._tech_files", lambda cfg: {"lyp": None, "lym": None})
    d = run / "55-openroad-stapostpnr"
    p1 = desktop.def_open_script("magic", d / "spm.def", run)
    # Someone planted a different script under the same, predictable name.
    p1.write_text("exec touch PWNED\n")
    p2 = desktop.def_open_script("magic", d / "spm.def", run)
    assert p2 == p1 and "PWNED" not in p2.read_text()
    out = tmp_path / "out"
    out.mkdir()
    shutil.rmtree(run / ".lanex")
    (run / ".lanex").mkdir()
    os.symlink(out, run / ".lanex" / "viewers")
    assert desktop.def_open_script("magic", d / "spm.def", run) is None
    assert list(out.iterdir()) == []


def test_thumbnail_cache_link_is_not_followed(tmp_path, monkeypatch):
    from lanex.controller import layoutview as lv
    run = _run(tmp_path)
    gds = run / "final" / "gds" / "spm.gds"
    gds.parent.mkdir(parents=True)
    gds.write_bytes(b"gds")
    out = tmp_path / "out"
    out.mkdir()
    (out / "final__64x64__000000__1_1_v1.png").write_bytes(b"keep")
    (run / ".lanex").mkdir()
    os.symlink(out, run / ".lanex" / "thumbs")
    monkeypatch.setattr(lv, "available", lambda: {"ok": True})
    monkeypatch.setattr(lv, "_WORKERS", None)
    monkeypatch.setattr(lv, "_thumb_png", lambda *a, **k: b"\x89PNG\r\n\x1a\nnew")
    assert lv.thumbnail(run, "final", 64, 64, "#000000") == b"\x89PNG\r\n\x1a\nnew"
    assert [p.name for p in out.iterdir()] == ["final__64x64__000000__1_1_v1.png"]


def test_notes_and_console_log_never_write_through_links(tmp_path):
    run = _run(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    os.symlink(secret, run / ".gui-note.txt")
    assert history.write_note(run, "best so far")["ok"]
    assert secret.read_text() == "secret" and (run / ".gui-note.txt").read_text() == "best so far"
    os.symlink(secret, run / "lanex-console.log")
    with pytest.raises(safefs.UnsafePath):
        safefs.open_append(run / "lanex-console.log")
    assert secret.read_text() == "secret"


def test_safefs_confines_every_part(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    d = safefs.confined_dir(root, ".lanex", "thumbs")
    assert d == root.resolve() / ".lanex" / "thumbs" and d.is_dir()
    os.symlink(tmp_path, root / "link")
    with pytest.raises(safefs.UnsafePath):
        safefs.confined_dir(root, "link", "x")
    with pytest.raises(safefs.UnsafePath):
        safefs.confined_dir(root, "..", "x")
    (root / "f").write_text("")
    with pytest.raises(safefs.UnsafePath):
        safefs.confined_dir(root, "f")
    assert safefs.remove_tree(root, "link") is True and tmp_path.is_dir()   # the link, not its target


# ------------------------------------------------------------------ finding 3: Yosys script injection
def test_p2_design_name_cannot_split_the_yosys_script(tmp_path):
    """R2 p2: DESIGN_NAME 'a; script e.ys; b' ran e.ys, whose '!' line is a
    shell command. Now the job refuses to start, in plain words."""
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "config.json").write_text(json.dumps({"DESIGN_NAME": "a; script e.ys; b", "VERILOG_FILES": "dir::src/*.v",
                                                  "CLOCK_PORT": None, "PDK": "sky130A"}))
    (proj / "src" / "t.v").write_text("module a(input x, output y); assign y = x; endmodule\n")
    (proj / "e.ys").write_text("!touch PWNED\n")
    r = schematic.job.start(proj, top=None)
    assert r["ok"] is False and "DESIGN_NAME" in r["error"] and "plain Verilog name" in r["error"]
    assert not (proj / "PWNED").exists()


@pytest.mark.parametrize("kw", [
    {"top": "a;b"}, {"top": "!sh"}, {"top": "a b"},
    {"defines": {"W": "8; script e.ys"}}, {"defines": {"W": "1 2"}}, {"defines": {"W": '"a b"'}}, {"defines": {"a;b": None}},
    {"include_dirs": ["src;script e.ys"]}, {"include_dirs": ["inc #x"]},
    {"files": ["src/a.v;script e.ys"]}, {"files": ["-p"]}, {"files": ["src/my file.v"]}, {"files": ["src/x!.v"]},
    {"files": ["src/q'.v"]}, {"files": ["src/a.v\nscript e.ys"]},
    {"params": {"W": "1; script e.ys"}}, {"params": {"W;x": "1"}},
    {"files": [], "vhdl_files": ["c.vhd; !sh"]},
])
def test_every_yosys_value_must_be_one_plain_word(kw):
    args = {"files": ["src/a.v"], "top": "top"} | kw
    with pytest.raises(schematic.InputError):
        schematic.yosys_script(**args)


def test_ordinary_values_still_reach_yosys():
    s = schematic.yosys_script(files=["src/a.v", "/abs/b.sv"], top="top_1$x", include_dirs=["src/include"],
                               defines={"W": "8'hFF", "MODE": "FAST", "F": '"mem.hex"', "R": "1.5", "N": "-2", "X": None},
                               params={"WIDTH": "16", "INIT": "4'b1010"})
    cmds = s.split("; ")
    assert len(cmds) == 5, "exactly read, hierarchy, the two rtl passes and write_json"
    assert cmds[0] == "read_verilog -sv -noautowire -DW=8'hFF -DMODE=FAST -DF=\"mem.hex\" -DR=1.5 -DN=-2 -DX -Isrc/include src/a.v /abs/b.sv"
    assert schematic.shell_command("a b").startswith("yosys -q -p 'a b'")


def test_file_names_that_look_like_options_stay_file_names():
    from lanex.controller import simulate
    sh = simulate._iverilog_shell(top="tb", sources=["-o", "src/a.v"], testbench="-ftb.v", defines={}, include_dirs=[])
    assert " ./-o " in sh and " ./-ftb.v" in sh
    assert quoting.file_arg("src/-x.v") == "src/-x.v"


def test_python_probes_never_import_from_the_project(tmp_path):
    from lanex.controller import configcheck
    argv = configcheck._container_probe_argv({}, engine="docker", image="img", design_dir=tmp_path, pdk_root=None, name="n")
    assert "PYTHONSAFEPATH=1" in argv
    assert "PYTHONSAFEPATH" in inspect.getsource(configcheck._run_probe)
    from lanex.controller import runner
    assert 'run_env["PYTHONSAFEPATH"] = "1"' in inspect.getsource(runner)


# ------------------------------------------------------------------ finding 5: Tcl injection
def test_p5_def_name_becomes_text_in_the_magic_script(tmp_path, monkeypatch):
    """R2 p5: a DEF named spm[exec touch PWNED].def ran exec through
    puts "LanEx: opened <name>"."""
    run = _run(tmp_path)
    monkeypatch.setattr("lanex.controller.layoutview._lef_list", lambda cfg: ["/pdk/a b.lef", "/pdk/x}.lef"])
    monkeypatch.setattr("lanex.controller.layoutview._tech_files", lambda cfg: {"lyp": None, "lym": None})
    name = "spm[exec touch PWNED_by_def_name].def"
    p = desktop.def_open_script("magic", run / "55-openroad-stapostpnr" / name, run)
    lines = [ln for ln in p.read_text().splitlines() if ln and not ln.startswith("#")]
    parsed = [tcl_words(ln) for ln in lines]
    assert all(subs == [] for _, subs in parsed), lines
    assert parsed[-1][0] == ["puts", "LanEx: opened " + name]
    assert ["lef", "read", "/pdk/a b.lef"] in [w for w, _ in parsed] and ["lef", "read", "/pdk/x}.lef"] in [w for w, _ in parsed]


def test_openroad_startup_script_keeps_file_names_as_text(tmp_path):
    from lanex.controller import container_tools
    run = tmp_path / "run"
    (run / "final" / "odb").mkdir(parents=True)
    odb = run / "final" / "odb" / "a} [exec touch X] {b.odb"
    odb.write_bytes(b"")
    sp = container_tools._write_startup_script("openroad", run, odb=odb, pdk=None, pdk_root=None)
    text = sp.read_text()
    first = text.split("\n", 2)[1:3]
    cond, subs = tcl_words(first[0].rstrip(" {"))
    # if {[catch {read_db ...} err]} : the catch body holds the one path word.
    inner = cond[1][1:-1]                      # [catch {...} err]
    body = tcl_words(inner)[0][1]
    assert tcl_words(body) == (["read_db", str(odb)], [])
    assert "[exec" not in text.replace(quoting.tcl_word(str(odb)), "")


def test_pdn_file_for_macros_quotes_names(tmp_path):
    plan = {"kind": "bridge", "strap_layer": "met4[exec x]", "width": 1.6, "pitch": 10, "offset": 1, "spacing": 0.3,
            "connect": [["met4", "met5$y"]]}
    t = macrolib.pdn_cfg_text([("evil\nexec touch X", ["u_a", "u[exec rm -rf ~]"], plan)])
    for ln in t.splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        words, subs = tcl_words(ln.rstrip("\\").strip())
        assert all("exec" not in s for s in subs), ln
    inst = next(ln for ln in t.splitlines() if ln.strip().startswith("-instances"))
    assert tcl_words(inst.rstrip("\\").strip()) == (["-instances", "u_a u[exec rm -rf ~]"], [])
    assert "\nexec touch X" not in t


# ------------------------------------------------------------------ finding 7: lossy Replace
def test_p6_replace_skips_a_latin1_file_and_keeps_its_bytes(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    raw = b"// (c) \xa9 2019 Acme, delay 5 \xb5s\nmodule latin1_demo(input a, output y);\n  assign y = a; // old_name\nendmodule\n"
    (proj / "src" / "latin1_demo.v").write_bytes(raw)
    (proj / "src" / "ok.v").write_text("module ok; // old_name\nendmodule\n", encoding="utf-8")
    edits = []
    for rel in ("src/latin1_demo.v", "src/ok.v"):
        text = (proj / rel).read_bytes().decode("utf-8", errors="replace")
        edits.append({"rel": rel, "hash": editor.text_hash(text), "offs": [text.index("old_name")]})
    r = hdlsearch.replace(proj, "old_name", "new_name", edits)
    by = {x["rel"]: x for x in r["results"]}
    assert by["src/latin1_demo.v"]["ok"] is False and by["src/latin1_demo.v"]["encoding"] is True
    assert "isn't UTF-8" in by["src/latin1_demo.v"]["why"] and "0xA9" in by["src/latin1_demo.v"]["why"]
    assert (proj / "src" / "latin1_demo.v").read_bytes() == raw
    assert by["src/ok.v"]["ok"] and "new_name" in (proj / "src" / "ok.v").read_text()


# ------------------------------------------------------------------ finding 8: GET side effects (G20)
def test_schematic_cancel_and_caps_are_post_only(server, monkeypatch):
    port, _ = server
    called = []
    monkeypatch.setattr(schematic.job, "cancel", lambda: called.append(1))
    _req(port, "GET", "/api/ide/schematic-result?id=x&cancel=1")
    assert called == [], "a GET never cancels"
    st, _, _ = _req(port, "POST", "/api/ide/schematic-cancel")
    assert st == 200 and called == [1]
    for path in ("/api/ide/caps?wait=1", "/api/ide/caps", "/api/render-dot?tag=x&path=a.dot"):
        assert _req(port, "GET", path)[0] == 405, path


# Side effects a GET handler (or a lanex function it calls directly) may have,
# reviewed one by one. Anything else that writes, deletes or starts a process
# behind a GET fails the test below.
REVIEWED_GET = {
    "/api/project/defaults": "makes the default projects folder (~/LanEx-Projects) so the OS folder picker opens in it",
    "/api/layout/thumb": "an <img> source: renders a stage thumbnail and caches it in <run>/.lanex/thumbs (a real folder "
                         "inside the run, never through a link), replacing older thumbnails of the same stage",
    "/api/fs/list": "runs wslpath to turn a Windows path into a Linux one; writes nothing",
    "/api/system/storage": "measures disk use in a background thread (lane 6); writes and deletes nothing",
    "/api/preflight": "read-only checks: tool versions, docker info, and LibreLane loading the config in a --rm, "
                      "--network=none container with read-only mounts; writes nothing",
}
_SIDE = re.compile(r"\.write_text\(|\.write_bytes\(|open\([^)\n]*[\"'][wax]b?[\"']|\.unlink\(|rmtree\(|\.mkdir\(|Popen\(|"
                   r"subprocess\.(run|Popen|call|check)|\.cancel\(|os\.replace\(|shutil\.(copy|move)|\.kill\(|safefs\.|"
                   r"\.touch\(|_set_active_design_dir\(|atomic_write|threading\.Thread\(|ThreadPoolExecutor|\.submit\(")


def _callees(fn):
    src = inspect.getsource(fn)
    mods = {}
    for m in re.finditer(r"from \.\.controller import ([\w, ]+)", src):
        for name in m.group(1).split(","):
            name = name.strip().split(" as ")[0]
            with contextlib.suppress(Exception):
                mods[name] = importlib.import_module("lanex.controller." + name)
    out = []
    for m in re.finditer(r"\b([a-z_]\w*)\.([a-z_]\w*)(?:\.([a-z_]\w*))?\(", src):
        base = mods.get(m.group(1)) or fn.__globals__.get(m.group(1))
        if not inspect.ismodule(base) or not base.__name__.startswith("lanex."):
            continue
        obj = getattr(base, m.group(2), None)
        if m.group(3) and obj is not None:
            obj = getattr(obj, m.group(3), None)
        if inspect.isfunction(obj) or inspect.ismethod(obj):
            out.append(obj)
    return src, out


def test_no_get_route_writes_deletes_or_starts_a_process_unreviewed():
    """Walk ROUTES: a route that answers GET must have no side-effect marker
    in its handler or in the lanex functions the handler calls, unless it
    dispatches on the method (writes only on POST) or is reviewed above."""
    found = {}
    for prefix, fn in routes.ROUTES:
        methods = routes.ROUTE_METHODS.get(prefix, ("GET", "POST"))
        if "GET" not in methods:
            continue
        src, callees = _callees(fn)
        if "POST" in methods and re.search(r"command[\"']?,?\s*[\"']?\w*[\"']?\)?\s*==\s*[\"']POST[\"']|command\s*==\s*[\"']GET[\"']", src):
            continue      # GET reads, POST writes
        hits = {m.group(0) for m in _SIDE.finditer(src.replace("wfile.write(", ""))}
        for c in callees:
            with contextlib.suppress(Exception):
                hits |= {m.group(0) for m in _SIDE.finditer(inspect.getsource(c))}
        if hits and prefix not in REVIEWED_GET:
            found[prefix] = sorted(hits)
    assert found == {}, f"GET routes with side effects (make them POST, or review them into REVIEWED_GET): {found}"
    stale = [p for p in REVIEWED_GET if p not in routes.ROUTE_METHODS or "GET" not in routes.ROUTE_METHODS[p]]
    assert stale == []


# ------------------------------------------------------------------ finding 9: macro view folders
def test_generated_macro_views_refuse_a_linked_folder(tmp_path):
    d = tmp_path / "proj"
    (d / "macros").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(out, d / "macros" / "sram")
    views, gen = custommacros.prepare_views(d, "sram", {"lef": "dir::x.lef"})
    assert gen == {} and list(out.iterdir()) == []
    assert custommacros._writable_macro_dir(d.resolve(), "sram") is None
    with pytest.raises(safefs.UnsafePath):
        custommacros._write_generated(d / "macros" / "sram", "sram.bb.v", "module sram; endmodule\n")
    assert list(out.iterdir()) == []
    shutil.rmtree(d / "macros")
    os.symlink(out, d / "macros")
    assert custommacros._writable_macro_dir(d.resolve(), "sram") is None


# ------------------------------------------------------------------ finding 10: lost waiver saves
def test_concurrent_waiver_saves_all_land(tmp_path, monkeypatch):
    real = runwarnings.load_waivers

    def slow(dd):
        r = real(dd)
        time.sleep(0.05)          # widen the read-then-write window
        return r

    monkeypatch.setattr(runwarnings, "load_waivers", slow)
    errs = []

    def save(i):
        try:
            runwarnings.save_waiver(tmp_path, {"code": f"X-{i:04d}", "note": "a long enough note", "reason": "harmless"})
        except Exception as ex:  # noqa: BLE001
            errs.append(ex)

    th = [threading.Thread(target=save, args=(i,)) for i in range(6)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert errs == []
    codes = sorted(e["code"] for e in real(tmp_path)["entries"])
    assert codes == [f"X-{i:04d}" for i in range(6)]


def test_waiver_file_changed_by_another_program_mid_save_is_not_overwritten(tmp_path, monkeypatch):
    runwarnings.save_waiver(tmp_path, {"code": "X-0001", "note": "a long enough note", "reason": "harmless"})
    real = runwarnings.load_waivers

    def edited(dd):
        r = real(dd)
        f = Path(dd) / runwarnings.WAIVER_FILE
        f.write_text(f.read_text() + "# edited elsewhere\n")
        return r

    monkeypatch.setattr(runwarnings, "load_waivers", edited)
    with pytest.raises(runwarnings.WaiverError, match="changed on disk"):
        runwarnings.save_waiver(tmp_path, {"code": "X-0002", "note": "a long enough note", "reason": "harmless"})
    assert "# edited elsewhere" in (tmp_path / runwarnings.WAIVER_FILE).read_text()


# ------------------------------------------------------------------ run routes read only known projects
def test_run_log_and_reports_read_only_known_projects(server, tmp_path, monkeypatch):
    """F1 confined /api/runs-meta to known projects; the run log, run reports,
    runs and trends routes take a folder from the URL too."""
    port, proj = server
    other = tmp_path / "someone-else"
    for d in (proj, other):
        (d / "runs" / "r1").mkdir(parents=True, exist_ok=True)
        (d / "runs" / "r1" / "flow.log").write_text("log of " + d.name + "\n")
    monkeypatch.setattr(routes, "_runs_known_designs", lambda: [str(proj.resolve())])
    monkeypatch.setattr(routes, "_runs_live_dirs", lambda: [])
    st, _, body = _req(port, "GET", f"/api/run/log?tag=r1&design_dir={proj}")
    assert st == 200 and b"log of proj" in body
    for path in (f"/api/run/log?tag=r1&design_dir={other}", f"/api/run/log?tag=r1&design_dir={other}&raw=1",
                 f"/api/run-reports?design_dir={other}&run_tag=r1", f"/api/runs?design_dir={other}",
                 f"/api/trends?design_dir={other}"):
        st, _, body = _req(port, "GET", path)
        assert st == 403 and b"log of" not in body, path
    assert _req(port, "GET", f"/api/run-reports?design_dir={proj}&run_tag=r1")[0] == 200


# ------------------------------------------------------------------ R3 and F3 follow-ups in my files
def test_power_total_is_named_after_the_last_sta_corner(tmp_path):
    rep = signoff.build(_run(tmp_path))
    assert rep["power"]["metric_corner"] == "max_ff_n40C_1v95"
    assert "max_ff_n40C_1v95" in rep["power"]["note"]


def test_violation_counts_say_they_are_summed_over_corners(tmp_path):
    """R3-5: LibreLane's timing__setup_vio__count adds every corner's (102 =
    36 + 31 + 35 on a 65-endpoint design); the report says so."""
    m = {"timing__setup_vio__count": 67, "timing__setup__ws": -0.43,
         "timing__setup_vio__count__corner:max_ss_100C_1v60": 36, "timing__setup__ws__corner:max_ss_100C_1v60": -0.43,
         "timing__setup_vio__count__corner:min_ff_n40C_1v95": 31, "timing__setup__ws__corner:min_ff_n40C_1v95": -0.2}
    run = _run(tmp_path, m)
    sta = run / "55-openroad-stapostpnr" / "state_out.json"
    st = json.loads(sta.read_text())
    st.setdefault("metrics", {}).update(m)
    sta.write_text(json.dumps(st))
    rep = signoff.build(run)
    setup = next(c for c in rep["checks"] if c["id"] == "setup")
    per = [c["setup_vio"] for c in rep["timing"]["corners"] if isinstance(c.get("setup_vio"), int)]
    assert len(per) == 2, rep["timing"]["corners"]
    assert "67 violating (summed over 2 corners; 36 in the worst corner)" in setup["value"]


def test_no_clock_words_cover_the_three_cases():
    h, m = signoff.no_clock_words({"io_paths": False, "registers": 64, "unclocked": 64})
    assert h.startswith("Timing was never checked: no clock reached the registers") and "no timed path" in m
    h, m = signoff.no_clock_words({"io_paths": True, "registers": 64, "unclocked": 64})
    assert "Only input-to-output paths were timed; no flip-flop had a clock" in m
    h, m = signoff.no_clock_words({"io_paths": True, "registers": 64, "unclocked": 12, "port_missing": True, "sdc_port": "clock"})
    assert m.startswith("12 flip-flop clock pins not timed") and "clock" in m


def test_final_stage_paths_use_the_signoff_sdc(tmp_path):
    run = _run(tmp_path)
    proj = run.parent.parent
    (proj / "src").mkdir()
    (proj / "src" / "signoff.sdc").write_text("set_clock_uncertainty 0.1 [all_clocks]\n")
    cfg = json.loads((run / "resolved.json").read_text())
    cfg.update({"DESIGN_DIR": "/elsewhere/spm", "SIGNOFF_SDC_FILE": "/elsewhere/spm/src/signoff.sdc",
                "DEFAULT_CORNER": "nom_tt_025C_1v80", "CLOCK_PERIOD": 10, "CLOCK_PORT": "clk"})
    (run / "resolved.json").write_text(json.dumps(cfg))
    (run / "final" / "odb").mkdir(parents=True)
    (run / "final" / "odb" / "spm.odb").write_bytes(b"x")
    (run / "final" / "sdc").mkdir(parents=True)
    (run / "final" / "sdc" / "spm.sdc").write_text("set_clock_uncertainty 0.25 clk\n")
    inp = odbexport._inputs(run, odbexport.database_for(run, "final"))
    assert inp["sdc"] == proj / "src" / "signoff.sdc"
    assert inp["timing"]["signoff_sdc"] and inp["timing"]["corner"] == "nom_tt_025C_1v80" and inp["timing"]["corners"] == 3
    assert inp["env"]["CLOCK_PERIOD"] == "10" and inp["env"]["CLOCK_PORT"] == "clk"
    argv = odbexport.build_argv("docker", "img", run_dir=run, cdir=run / ".lanex", script=Path("s"), odb=Path("/o.odb"),
                                libs=[], sdc=inp["sdc"], spef=None, propagate=True, name="n", timeout_s=9, mounts=[],
                                user=None, config_env={"CLOCK_PERIOD": "10", "ODB": "/evil"})
    assert "CLOCK_PERIOD=10" in argv and "ODB=/o.odb" in argv and "ODB=/evil" not in argv
