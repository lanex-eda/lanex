# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
#      http://www.apache.org/licenses/LICENSE-2.0
"""One verdict vocabulary (audit 7c): the states and words the server gives a
run or a check (controller/verify.py, synthexplore.py) are the ones
modules/verdict.js knows, so no page shows a state it has no words for."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from lanex.controller import synthexplore, verify

JS = (Path(__file__).resolve().parents[1] / "server" / "static" / "modules" / "verdict.js").read_text(encoding="utf-8")


def _js_table(name: str) -> dict:
    body = re.search(r"const " + name + r" = \{(.*?)\n\};", JS, re.S).group(1)
    return {k: (c, w) for k, c, w in re.findall(r'(\w+):\s*\{ cls: "(\w+)",\s*word: "([^"]+)" \}', body)}


def test_run_states_are_the_same_on_both_sides():
    js = _js_table("RUN")
    assert set(js) == set(verify.RUN_STATES)
    # every state the server's verdict code writes is one of them
    src = inspect.getsource(verify.run_verdict_from) + inspect.getsource(synthexplore.verdict)
    written = set(re.findall(r'state="(\w+)"', src)) | set(re.findall(r'"state": "(\w+)"', src))
    assert written and written <= set(verify.RUN_STATES), written - set(verify.RUN_STATES)


def test_run_words_match_the_server_labels():
    js = _js_table("RUN")
    src = inspect.getsource(verify.run_verdict_from)
    assert js["passed"][1] == "Clean" and 'label="Clean"' in src
    assert js["failed"][1] == "Failed" and '"Failed"' in src
    assert js["stopped"][1] == "Stopped" and 'label="Stopped"' in src
    # colours: a pass is never amber, a fail never green, unknown is amber (verdict.js)
    assert js["passed"][0] == "pass" and js["failed"][0] == "fail"
    assert js["warning"][0] == js["stopped"][0] == js["incomplete"][0] == "warn"
    assert 'cls: "warn", word: st ?' in JS.replace("\n", " ") or "never a guessed pass" in JS


def test_check_statuses_have_words():
    js = _js_table("CHECK")
    missing = set(verify.CHECK_STATUSES) - set(js)
    # Z2: the check words are Verify's on every page ("Not checked", "Turned off");
    # a status this build doesn't know is said as "Not checked".
    assert not missing, missing
    assert js["not_run"][1] == js["absent"][1] == "Not checked" and js["skipped"][1] == "Turned off"
    assert "CHECK[status] || CHECK.absent" in JS
