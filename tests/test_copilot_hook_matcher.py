"""The Copilot preToolUse matcher must be a regex string.

Copilot CLI reads `matcher` as a regex string tested against the tool name and
skips any hook entry whose matcher is not a string. copilot-doctor must flag
such a file instead of reporting it healthy.

Run: python3 -m pytest tests/test_copilot_hook_matcher.py -v
"""
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture
def mods(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("TOKEN_OPTIMIZER_COPILOT_HOME", raising=False)
    monkeypatch.setenv("COPILOT_HOME", str(home / ".copilot"))
    py = tmp_path / "bin" / "python3"
    py.parent.mkdir(mode=0o755)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(py))
    for name in ("copilot_install", "copilot_doctor"):
        sys.modules.pop(name, None)
    return importlib.import_module("copilot_install"), importlib.import_module("copilot_doctor"), home / ".copilot"


def test_pretooluse_matcher_is_a_string_that_matches_bash_only(mods):
    inst, _doc, tmp = mods
    cfg = inst._hook_config(tmp / "bridge.py")
    matcher = cfg["hooks"]["preToolUse"][0]["matcher"]
    assert isinstance(matcher, str)
    assert re.fullmatch(f"(?:{matcher})", "bash")
    assert not re.fullmatch(f"(?:{matcher})", "edit")


def test_other_events_have_no_matcher(mods):
    inst, _doc, tmp = mods
    hooks = inst._hook_config(tmp / "bridge.py")["hooks"]
    for event in ("sessionStart", "postToolUse", "sessionEnd"):
        assert all("matcher" not in e for e in hooks[event])


def test_installed_file_has_string_matcher(mods):
    inst, _doc, tmp = mods
    inst.install(home=tmp)
    data = json.loads((tmp / "hooks" / "token-optimizer.json").read_text())
    assert data["hooks"]["preToolUse"][0]["matcher"] == "bash"


def _doctor_hook_check(doc):
    return [c for c in doc._hook_config_checks() if c["name"] == "TO hook config"][0]


def test_doctor_flags_object_matcher(mods):
    inst, doc, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    data["hooks"]["preToolUse"][0]["matcher"] = {"toolName": "bash"}
    path.write_text(json.dumps(data))
    check = _doctor_hook_check(doc)
    assert check["status"] == "fail"
    assert "preToolUse" in check["detail"]
    assert "copilot-install" in check["hint"]


def test_doctor_passes_fresh_install(mods):
    inst, doc, tmp = mods
    inst.install(home=tmp)
    assert _doctor_hook_check(doc)["status"] == "ok"


def test_doctor_passes_entry_without_matcher(mods):
    inst, doc, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    del data["hooks"]["preToolUse"][0]["matcher"]
    path.write_text(json.dumps(data))
    assert _doctor_hook_check(doc)["status"] == "ok"


def test_doctor_tolerates_malformed_shapes(mods):
    _inst, doc, _tmp = mods
    assert doc._invalid_matcher_events({"preToolUse": "x", "stop": [1, None]}) == []
    assert doc._invalid_matcher_events(None) == []


@pytest.mark.parametrize("matcher", ["bash(", "", "   ", "(?=bash)bash", "(?!edit).*", "(b)\\1"])
def test_doctor_flags_string_matchers_copilot_skips(mods, matcher):
    """A string matcher can still be skipped: invalid regex, empty (matches no
    tool), or lookaround/backreference, which Copilot's Rust regex rejects."""
    inst, doc, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    data["hooks"]["preToolUse"][0]["matcher"] = matcher
    path.write_text(json.dumps(data))
    check = _doctor_hook_check(doc)
    assert check["status"] == "fail"
    assert "preToolUse" in check["detail"]


@pytest.mark.parametrize("matcher", ["bash", "bash|powershell", "(bash|shell)"])
def test_doctor_passes_valid_string_matchers(mods, matcher):
    inst, doc, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    data["hooks"]["preToolUse"][0]["matcher"] = matcher
    path.write_text(json.dumps(data))
    assert _doctor_hook_check(doc)["status"] == "ok"
