# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""The server boundary (audit 2026-11-15, 3.3 and 3.2).

G20 routes answer only their method: side effects are POST only.
G21 every /api/* request carries the LanEx key (header or cookie).
G14 event ids carry the server's boot id; a stale one is a gap.
G16 a raw upload route streams big files to disk.
One error shape for every /api/* error.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from lanex.controller import events
from lanex.server import auth, routes
from lanex.server.app import make_server

KEY = "k" * 43


def _pick_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(token):
    httpd, port = make_server(host="127.0.0.1", port=_pick_port(), token=token)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    for _ in range(100):
        try:
            c = HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/health")
            if c.getresponse().status == 200:
                break
        except Exception:
            time.sleep(0.05)
    return httpd, port


@pytest.fixture(scope="module")
def keyed():
    httpd, port = _serve(KEY)
    yield port
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def open_server():
    httpd, port = _serve(None)
    yield port
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, *, headers=None, body=None, raw=None):
    c = HTTPConnection("127.0.0.1", port, timeout=30)
    h = dict(headers or {})
    data = raw
    if data is None and method == "POST":
        data = json.dumps(body or {}).encode()
        h.setdefault("Content-Type", "application/json")
    if data is not None:
        h["Content-Length"] = str(len(data))
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


POST_HDRS = {"X-Requested-With": "XMLHttpRequest"}


# -------------------------------------------------------------------- G20

def test_every_route_declares_its_method():
    """A new route must say whether it is a read (GET) or an action (POST):
    anything left out falls back to answering both, which this test reports."""
    unlisted = [p for p, _fn in routes.ROUTES
                if p not in routes._POST_ROUTES | routes._GET_ROUTES | routes._BOTH_ROUTES]
    assert unlisted == [], "add these to _GET_ROUTES / _POST_ROUTES / _BOTH_ROUTES: " + ", ".join(unlisted)
    stale = (routes._POST_ROUTES | routes._GET_ROUTES | routes._BOTH_ROUTES) - {p for p, _ in routes.ROUTES}
    assert stale == set(), f"listed but not routed: {sorted(stale)}"
    assert not (routes._POST_ROUTES & routes._GET_ROUTES)


def test_frontend_calls_use_each_routes_method():
    """Every /api/* call in the page's JS uses a method its route answers, so
    making a route POST-only cannot silently break a screen."""
    import re
    static = Path(routes.__file__).parent / "static"
    by_len = sorted(routes.ROUTE_METHODS, key=len, reverse=True)

    def route_of(path):
        for p in by_len:
            if path == p or (p.endswith("/") and path.startswith(p)):
                return p
        return None

    bad, checked = [], 0
    for f in sorted(static.rglob("*.js")):
        if "vendor" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        for m in re.finditer(r'["\'](/api/[A-Za-z0-9_\-/]+)', text):
            path = m.group(1)
            r = route_of(path) or (route_of(path + "/") if not path.endswith("/") else None)
            if r is None:
                continue
            ln = lines[text.count("\n", 0, m.start())].strip()
            if ln.startswith(("//", "*")) or "===" in ln or "new Set([" in ln or ln.startswith('"/api/'):
                continue      # comments, the journal's path list, comparisons
            tail = text[m.end(): m.end() + 400]
            stop = re.search(r"\),\s*\n\s*[A-Za-z_]+\s*:|;\s*\n|\n\s*\w+:\s*\(", tail)
            seg = tail[: stop.start() if stop else 400]
            want = "POST" if re.search(r"method:\s*[\"']POST[\"']", seg) else "GET"
            checked += 1
            if want not in routes.ROUTE_METHODS[r]:
                bad.append(f"{f.name}:{ln[:80]} uses {want} on {r}")
    assert checked > 100
    assert bad == [], bad


@pytest.mark.parametrize("path", [
    "/api/layout/open?tag=x&source=final",   # starts a KLayout session
    "/api/bye?id=page",                      # feeds close-to-quit
    "/api/fs/native-pick",                   # pops a dialog
    "/api/celllib/view?pdk=a&scl=b&cell=c",  # opens a viewer session
    "/api/run/cancel", "/api/runs-bulk", "/api/project/delete", "/api/manual/run",
    "/api/file/write", "/api/upload?filename=x",
])
def test_side_effect_routes_refuse_get(open_server, path):
    st, hdrs, body = _req(open_server, "GET", path)
    assert st == 405, (path, st, body[:200])
    data = json.loads(body)
    assert data["ok"] is False and data["code"] == "method"
    assert "POST" in hdrs.get("Allow", "")


def test_read_routes_refuse_post(open_server):
    st, hdrs, body = _req(open_server, "POST", "/api/runs", headers=POST_HDRS)
    assert st == 405 and json.loads(body)["code"] == "method"
    assert hdrs.get("Allow") == "GET"


def test_both_routes_answer_both(open_server):
    assert _req(open_server, "GET", "/api/settings/pdk-root")[0] == 200
    st, _h, _b = _req(open_server, "POST", "/api/settings/pdk-root", headers=POST_HDRS, body={})
    assert st == 400   # reached the handler: "missing pdk_root"


def test_head_follows_get(open_server):
    assert _req(open_server, "HEAD", "/api/health")[0] == 200
    assert _req(open_server, "HEAD", "/api/run/cancel")[0] == 405


# -------------------------------------------------------------------- G21

def test_health_and_presence_need_no_key(keyed):
    assert _req(keyed, "GET", "/api/health")[0] == 200
    assert _req(keyed, "GET", "/api/presence")[0] == 200


@pytest.mark.parametrize("path", ["/api/about", "/api/runs", "/api/events", "/api/run-file?tag=x&path=y",
                                  "/api/views/x/y", "/api/layout/thumb?tag=x"])
def test_api_without_the_key_is_refused(keyed, path):
    st, _h, body = _req(keyed, "GET", path)
    assert st == 401
    data = json.loads(body)
    assert data == {"ok": False, "error": data["error"], "code": "key"}
    assert "open LanEx again" in data["error"]


def test_post_without_the_key_is_refused_even_from_a_script(keyed):
    # No Origin header (curl, another user's script): the old checks passed it.
    st, _h, body = _req(keyed, "POST", "/api/manual/run", headers=POST_HDRS, body={"command": "yosys -V"})
    assert st == 401 and json.loads(body)["code"] == "key"


def test_wrong_key_is_refused(keyed):
    assert _req(keyed, "GET", "/api/about", headers={auth.HEADER: "x" * 43})[0] == 401


def test_key_in_header_or_cookie_is_accepted(keyed):
    assert _req(keyed, "GET", "/api/about", headers={auth.HEADER: KEY})[0] == 200
    cookie = f"other=1; {auth.cookie_name(keyed)}={KEY}; theme=dark"
    assert _req(keyed, "GET", "/api/about", headers={"Cookie": cookie})[0] == 200
    # another port's cookie is not this server's key
    assert _req(keyed, "GET", "/api/about", headers={"Cookie": f"{auth.cookie_name(keyed + 1)}={KEY}"})[0] == 401


def test_page_without_the_key_explains_how_to_open_lanex(keyed):
    st, hdrs, body = _req(keyed, "GET", "/")
    assert st == 401 and hdrs["Content-Type"].startswith("text/html")
    text = body.decode()
    assert "Open LanEx" in text and "lanex" in text and KEY not in text
    assert _req(keyed, "GET", "/ide")[0] == 401


def test_key_in_the_address_becomes_a_strict_cookie(keyed):
    st, hdrs, _b = _req(keyed, "GET", f"/?{auth.QUERY_PARAM}={KEY}&tab=runs")
    assert st == 303
    assert hdrs["Location"] == "/?tab=runs", "the key must leave the address"
    ck = hdrs["Set-Cookie"]
    assert ck.startswith(f"{auth.cookie_name(keyed)}={KEY};")
    for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age="):
        assert part in ck
    st, _h, body = _req(keyed, "GET", "/", headers={"Cookie": f"{auth.cookie_name(keyed)}={KEY}"})
    assert st == 200 and b"<html" in body.lower()


def test_wrong_key_in_the_address_sets_nothing(keyed):
    st, hdrs, _b = _req(keyed, "GET", f"/?{auth.QUERY_PARAM}={'z' * 43}")
    assert st == 401 and "Set-Cookie" not in hdrs


def test_static_files_need_no_key(keyed):
    assert _req(keyed, "GET", "/static/modules/api.js")[0] == 200
    assert _req(keyed, "GET", "/static/fonts.css")[0] == 200


def test_event_stream_with_the_key(keyed):
    c = HTTPConnection("127.0.0.1", keyed, timeout=5)
    c.request("GET", "/api/events", headers={"Cookie": f"{auth.cookie_name(keyed)}={KEY}"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    first = r.fp.readline().decode()
    assert first.startswith("event: hello")
    c.close()


def test_allow_remote_still_needs_the_key(monkeypatch):
    """--allow-remote lifts the loopback Host check, never the key."""
    from lanex.server import app as app_mod
    httpd, port = make_server(host="127.0.0.1", port=_pick_port(), allow_remote=True, token=KEY)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        lan = {"Host": f"192.168.1.20:{port}"}
        assert _req(port, "GET", "/api/about", headers=lan)[0] == 401
        assert _req(port, "GET", "/", headers=lan)[0] == 401
        assert _req(port, "GET", "/api/about", headers={**lan, auth.HEADER: KEY})[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        app_mod._ALLOW_REMOTE = False


def test_server_without_a_key_stays_open(open_server):
    """make_server() with no token (tests, embedding) keeps the old behaviour."""
    assert _req(open_server, "GET", "/api/about")[0] == 200
    assert _req(open_server, "GET", "/")[0] == 200


# --- the key file

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_key_file_is_private_and_reused(tmp_path):
    p = tmp_path / "auth-token"
    a = auth.load_or_create(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert auth.load_or_create(p) == a, "the key must survive a restart"
    os.chmod(p, 0o644)
    b = auth.load_or_create(p)
    assert b != a, "a key others could read is replaced"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_malformed_key_file_is_replaced(tmp_path):
    p = tmp_path / "auth-token"
    p.write_text("short\n")
    if os.name != "nt":
        os.chmod(p, 0o600)
    tok = auth.load_or_create(p)
    assert len(tok) >= 32 and auth.read_existing(p) == tok


def test_with_key_keeps_the_rest_of_the_address():
    assert auth.with_key("http://127.0.0.1:8765/", "K" * 40) == "http://127.0.0.1:8765/?lanex_key=" + "K" * 40
    assert auth.with_key("http://h/x?a=1#f", "K" * 40) == "http://h/x?a=1&lanex_key=" + "K" * 40 + "#f"
    assert auth.with_key("http://h/", None) == "http://h/"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_cli_server_record_carries_the_key_privately(tmp_path, monkeypatch):
    from lanex import cli
    rec = tmp_path / "server.json"
    monkeypatch.setattr(cli, "_server_record_path", lambda: rec)
    cli._write_server_record("http://127.0.0.1:8765/", 8765, KEY)
    assert json.loads(rec.read_text())["key"] == KEY
    assert stat.S_IMODE(rec.stat().st_mode) == 0o600
    assert cli._with_running_key("http://127.0.0.1:8765/") == "http://127.0.0.1:8765/?lanex_key=" + KEY


def test_app_shortcut_recognises_a_running_lanex(keyed, tmp_path, monkeypatch):
    """`lanex --from-shortcut` reopens the running LanEx (with its key) instead
    of starting a second server: /api/health's {"ok", "data"} envelope counts."""
    from lanex import cli
    rec = tmp_path / "server.json"
    cli_url = f"http://127.0.0.1:{keyed}/"
    rec.write_text(json.dumps({"url": cli_url, "key": KEY}))
    monkeypatch.setattr(cli, "_server_record_path", lambda: rec)
    assert cli._running_instance_url() == cli_url
    assert cli._with_running_key(cli_url) == cli_url + "?lanex_key=" + KEY


def test_cli_starts_the_server_with_a_key_and_opens_the_keyed_address(monkeypatch, tmp_path):
    from lanex import cli
    from lanex.server import app as server_app
    seen = {}

    class _FakeHTTPD:
        def shutdown(self):
            pass

    def fake_make_server(**kw):
        seen.update(kw)
        return _FakeHTTPD(), 8799

    opened = []

    class _Timer:
        def __init__(self, _delay, fn, args=()):
            self.fn, self.args = fn, args

        def start(self):
            opened.append(self.args[0])

    monkeypatch.setenv("LANEX_HOME", str(tmp_path))
    monkeypatch.setattr(server_app, "make_server", fake_make_server)
    monkeypatch.setattr(server_app, "serve_forever", lambda httpd, **kw: None)
    monkeypatch.setattr(cli.threading, "Timer", _Timer)
    assert cli.main(["--no-browser"]) == 0
    assert seen["token"] and seen["token"] == auth.read_existing(tmp_path / "auth-token")
    assert opened == ["http://127.0.0.1:8799/?lanex_key=" + seen["token"]]


# ---------------------------------------------------------- one error shape

def test_unknown_api_path_is_json(open_server):
    st, hdrs, body = _req(open_server, "GET", "/api/nope")
    assert st == 404 and hdrs["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"ok": False, "error": "Not found"}


def test_csrf_refusal_is_json(open_server):
    st, _h, body = _req(open_server, "POST", "/api/run/cancel", body={})
    assert st == 403 and json.loads(body)["code"] == "csrf"


def test_bad_json_body_is_json(open_server):
    st, _h, body = _req(open_server, "POST", "/api/run-note", headers={**POST_HDRS, "Content-Type": "application/json"},
                        raw=b"{not json")
    assert st == 400 and json.loads(body)["ok"] is False


def test_handler_crash_is_json(open_server, monkeypatch):
    def boom(_h):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(routes, "ROUTES", [("/api/test-boom", boom)] + list(routes.ROUTES))
    from lanex.server import app as app_mod
    monkeypatch.setattr(app_mod, "ROUTES", routes.ROUTES)
    st, _h, body = _req(open_server, "GET", "/api/test-boom")
    assert st == 500 and json.loads(body) == {"ok": False, "error": "kaboom"}


def test_page_paths_keep_plain_text_errors(open_server):
    st, hdrs, body = _req(open_server, "GET", "/nope")
    assert st == 404 and hdrs["Content-Type"].startswith("text/plain") and body == b"Not found"


# --------------------------------------------------------------------- G16

def test_raw_upload_streams_a_big_file_and_a_macro_save_uses_it(open_server, tmp_path):
    from lanex.controller import custommacros
    d = tmp_path / "chip"
    d.mkdir()
    blob = os.urandom(1 << 20) * 24           # 24 MiB: far past the old ~7.5 MB ceiling
    q = "/api/upload?filename=sram%201rw.gds&design_dir=" + str(d).replace(" ", "%20")
    st, _h, body = _req(open_server, "POST", q, headers={**POST_HDRS, "Content-Type": "application/octet-stream"},
                        raw=blob)
    assert st == 200, body[:300]
    up = json.loads(body)["data"]
    assert up["size"] == len(blob) and up["filename"] == "sram_1rw.gds"
    st, _h, body = _req(open_server, "POST", "/api/upload?filename=m.lef&design_dir=" + str(d),
                        headers=POST_HDRS, raw=b"MACRO sram\nEND sram\n")
    lef = json.loads(body)["data"]
    st, _h, body = _req(open_server, "POST", "/api/custom-macros/save", headers=POST_HDRS, body={
        "design_dir": str(d), "name": "sram", "instances": [],
        "views": {"gds": {"filename": "sram 1rw.gds", "upload_id": up["upload_id"]},
                  "lef": {"filename": "m.lef", "upload_id": lef["upload_id"]}}})
    assert st == 200, body[:300]
    assert (d / "macros" / "sram" / "sram_1rw.gds").read_bytes() == blob
    assert custommacros.list_macros(d)["macros"][0]["views"]["gds"] == "dir::macros/sram/sram_1rw.gds"


def test_upload_over_the_cap_is_refused(open_server, tmp_path):
    from lanex.controller import uploads
    d = tmp_path / "chip"
    d.mkdir()
    # 413 before a byte is stored; the connection is closed rather than drained.
    c = HTTPConnection("127.0.0.1", open_server, timeout=10)
    c.putrequest("POST", "/api/upload?filename=x.gds&design_dir=" + str(d))
    c.putheader("X-Requested-With", "XMLHttpRequest")
    c.putheader("Content-Length", str(3 * 1024 ** 3))
    c.endheaders()
    r = c.getresponse()
    assert r.status == 413
    assert json.loads(r.read())["ok"] is False
    assert not any((d / uploads.UPLOAD_SUBDIR).glob("*/*"))


def test_upload_without_a_design_is_refused(open_server):
    st, _h, body = _req(open_server, "POST", "/api/upload?filename=x.gds&design_dir=/nonexistent/x",
                        headers=POST_HDRS, raw=b"abc")
    assert st == 404 and json.loads(body)["ok"] is False


# --------------------------------------------------------------------- G14

def test_event_ids_carry_the_boot_id():
    assert events.wire_id(42) == f"{events.BOOT_ID}-42"


@pytest.mark.parametrize("last, expect_gap", [
    (None, None), ("", None), ("-1", None),
    ("5000", "restart"),                     # an older LanEx's plain number
    ("deadbeef-5000", "restart"),            # another server process
    ("garbage", "restart"),
])
def test_resume_point(last, expect_gap):
    if last == "deadbeef-5000" and events.BOOT_ID == "deadbeef":
        pytest.skip("boot id collision")
    cursor, gap = events.resume_point(last, max_seq=10)
    assert gap == expect_gap and cursor == -1


def test_resume_point_same_boot():
    assert events.resume_point(events.wire_id(7), max_seq=10) == (7, None)
    assert events.resume_point(events.wire_id(99), max_seq=10) == (-1, "ahead")


class _WFile:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    def flush(self):
        pass


class _Conn:
    def settimeout(self, _t):
        pass


class _FakeHandler:
    def __init__(self):
        self.wfile = _WFile()
        self.connection = _Conn()
        self.headers = {}

    def send_response(self, *_a, **_k):
        pass

    def send_header(self, *_a, **_k):
        pass

    def end_headers(self):
        pass


def _stream(last_event_id, emit_after=None):
    from lanex.server import sse
    h = _FakeHandler()
    s = sse.ISSEHandler(h)
    t = threading.Thread(target=lambda: s.stream_until_closed(None, heartbeat=999, idle=0.02,
                                                              last_event_id=last_event_id), daemon=True)
    t.start()
    time.sleep(0.15)
    if emit_after:
        events.bus.emit(*emit_after)
        time.sleep(0.2)
    s._closed = True
    t.join(2)
    return h.wfile.buf.decode()


def test_restart_reconnect_gets_a_gap_and_new_events():
    """G14: the window kept the old server's id (a much larger number). It is
    told to resync and then sees the new server's events at once."""
    for _ in range(3):
        events.bus.emit("info", {"n": 1})
    out = _stream("0badc0de-99999999", emit_after=("step_status", {"step": "after-restart"}))
    assert "event: gap" in out and '"reason": "restart"' in out
    assert "after-restart" in out, "a new event was skipped as already seen"
    assert f"id: {events.BOOT_ID}-" in out


def test_same_boot_reconnect_resumes_without_a_gap():
    events.bus.emit("info", {"n": 1})
    cursor = events.bus.max_seq
    events.bus.emit("info", {"missed": "while-away"})
    out = _stream(events.wire_id(cursor))
    assert "event: gap" not in out and "while-away" in out
