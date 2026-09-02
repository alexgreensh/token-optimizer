"""U1 — runtime identity for the Google Antigravity adapter.

Covers: detect_runtime() -> "antigravity", antigravity_home() resolution,
runtime_home(), plugin_data_env_vars(), and runtime_name_for_humans().
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"))

import runtime_env  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every runtime signal so tests start from a blank slate."""
    for key in (
        "TOKEN_OPTIMIZER_RUNTIME",
        "TOKEN_OPTIMIZER_ANTIGRAVITY_HOME",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PLUGIN_DATA",
        "CODEX_HOME",
        "HERMES_HOME",
        "COPILOT_HOME",
        "TOKEN_OPTIMIZER_COPILOT_HOME",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN",
        "CLAUDECODE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    runtime_env.detect_runtime.cache_clear()
    yield monkeypatch
    runtime_env.detect_runtime.cache_clear()


def _redirect_home(monkeypatch, tmp_path):
    """Point every home signal at the fixture so Path.home() agrees on every OS.

    Path.home() reads HOME on POSIX but USERPROFILE (+ HOMEDRIVE/HOMEPATH) on
    Windows, so setting only HOME leaves ~ pinned to the real profile on a
    Windows runner and the under-$HOME confinement resolves against the wrong
    root. Set the full set so antigravity_home()'s fallback and guard see the
    same fixture directory the test asserts against.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        drive, _, tail = str(tmp_path).partition(os.sep)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        if ":" in drive:
            monkeypatch.setenv("HOMEDRIVE", drive)
            monkeypatch.setenv("HOMEPATH", os.sep + tail)


def test_runtime_override_antigravity(clean_env, tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "antigravity")
    assert runtime_env.detect_runtime() == "antigravity"
    assert str(runtime_env.runtime_home()).endswith(".gemini")
    assert runtime_env.runtime_name_for_humans() == "Google Antigravity"


def test_home_env_resolves_under_home(clean_env, tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    gemini = tmp_path / ".gemini"
    gemini.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", str(gemini))
    assert runtime_env.detect_runtime() == "antigravity"
    assert runtime_env.antigravity_home() == gemini.resolve()


def test_home_env_outside_home_falls_back(clean_env, tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", str(tmp_path.parent / "escape")
    )
    # detect_runtime still returns antigravity (env present), but the home
    # resolver falls back to the default ~/.gemini.
    home = runtime_env.antigravity_home()
    assert home == (tmp_path / ".gemini")


def test_home_env_beats_claudecode(clean_env, tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    gemini = tmp_path / ".gemini"
    gemini.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME", str(gemini))
    monkeypatch.setenv("CLAUDECODE", "1")
    assert runtime_env.detect_runtime() == "antigravity"


def test_claude_plugin_env_beats_agy_ancestor(clean_env, tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / ".claude" / "plugins"))
    monkeypatch.setattr(
        runtime_env, "_antigravity_signal", lambda: True
    )
    assert runtime_env.detect_runtime() == "claude"


def test_plugin_data_env_vars_antigravity(clean_env, tmp_path, monkeypatch):
    _redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "antigravity")
    assert runtime_env.plugin_data_env_vars() == ("TOKEN_OPTIMIZER_PLUGIN_DATA",)
