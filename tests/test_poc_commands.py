"""Tests for SPEC-0 scaffold: POC savings command dispatch, hooks and lists."""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402,F401
from _measure import poc_commands  # noqa: E402

NEW_MODULES = (
    "poc_commands.py", "counted_closes.py", "counted_headline.py",
    "apply_safe.py", "autocompact_advice.py", "cold_guard.py",
    "keepwarm_auto.py", "next_actions.py",
)

REGISTRATION_LISTS = (
    "hooks/module_runner.py",
    "scripts/check_measure_split_payload.py",
    "skills/token-optimizer/scripts/_measure/__init__.py",
    "skills/token-optimizer/scripts/hermes_doctor.py",
    "skills/token-optimizer/scripts/hermes_install.py",
    "skills/token-optimizer/scripts/codex_doctor.py",
    "skills/token-optimizer/scripts/cloud_kit.py",
)


def test_each_command_imports_and_has_callable_main():
    for command, name in poc_commands.COMMANDS.items():
        module = importlib.import_module("." + name, poc_commands.__package__)
        assert callable(getattr(module, "main", None)), command


def test_run_dispatches_to_the_built_command(monkeypatch):
    module = importlib.import_module(".next_actions", poc_commands.__package__)
    seen = []
    monkeypatch.setattr(module, "main", lambda facade, args: seen.append(list(args)) or 0)
    assert poc_commands.run(None, ["next", "--json"]) == 0
    assert seen == [["--json"]]


def test_run_returns_1_and_never_raises_when_main_raises(monkeypatch, capsys):
    module = importlib.import_module(".next_actions", poc_commands.__package__)

    def boom(facade, args):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "main", boom)
    assert poc_commands.run(None, ["next"]) == 1
    assert capsys.readouterr().out == "Token Optimizer: next failed (RuntimeError).\n"


@pytest.mark.parametrize("relative", REGISTRATION_LISTS)
def test_every_registration_list_names_all_new_modules(relative):
    text = (REPO / relative).read_text(encoding="utf-8")
    for name in NEW_MODULES:
        assert name in text, f"{name} missing from {relative}"


def test_on_savings_refresh_hooks_isolated(monkeypatch):
    cold_guard = importlib.import_module(".cold_guard", poc_commands.__package__)
    next_actions = importlib.import_module(".next_actions", poc_commands.__package__)
    calls = []
    facade, summary = object(), {"total_tokens": 1}

    monkeypatch.setattr(cold_guard, "on_savings_refresh",
                        lambda f, s: calls.append(("cold_guard", f, s)),
                        raising=False)

    def boom(facade, summary):
        calls.append(("next_actions",))
        raise RuntimeError("boom")

    monkeypatch.setattr(next_actions, "on_savings_refresh", boom, raising=False)
    poc_commands.on_savings_refresh(facade, summary)
    # counted_closes and keepwarm_auto have no hook: skipped. A raising hook is
    # swallowed and does not stop or poison the calls around it.
    assert calls == [("cold_guard", facade, summary), ("next_actions",)]


def test_status_extras_returns_none_keys_with_stubs():
    assert poc_commands.status_extras(None, "session-id") == {
        "counted_headline": None, "next_action": None}


def _facade(directory, frozen):
    return types.SimpleNamespace(
        os=os, json=json,
        time=types.SimpleNamespace(time=lambda: frozen),
        _STATUS_BAR_SCHEMA=5,
        _STATUS_BAR_CACHE_RETENTION_S=30 * 86400,
        _status_bar_dir=lambda: directory,
        _status_bar_read_cache=lambda session_id: None,
        _status_bar_cache_path=lambda session_id: directory / "global.json",
    )


def test_write_cache_adds_status_keys_and_nothing_else(tmp_path, monkeypatch):
    from _measure import status_bar_refresh
    savings = {"session_tokens": 10, "session_usd": 0.01, "total_30d_tokens": 100}
    frozen = time.time()

    after_dir = tmp_path / "after"
    after_dir.mkdir()
    status_bar_refresh.write_cache(_facade(after_dir, frozen), "sid", savings, "counted")
    written = json.loads((after_dir / "global.json").read_text(encoding="utf-8"))
    assert written["counted_headline"] is None
    assert written["next_action"] is None

    # Same inputs and the same frozen clock with the new hook reporting no
    # extras must reproduce the pre-change bytes exactly.
    before_dir = tmp_path / "before"
    before_dir.mkdir()
    monkeypatch.setattr(poc_commands, "status_extras", lambda *a, **k: {})
    status_bar_refresh.write_cache(_facade(before_dir, frozen), "sid", savings, "counted")
    before_text = (before_dir / "global.json").read_text(encoding="utf-8")
    del written["counted_headline"], written["next_action"]
    assert json.dumps(written) == before_text
