"""an unrecognised TOKEN_OPTIMIZER_RUNTIME must not be silently ignored."""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch):
    monkeypatch.setattr(runtime_env, "_WARNED_BAD_OVERRIDES", set(), raising=False)
    runtime_env.detect_runtime.cache_clear()
    yield
    runtime_env.detect_runtime.cache_clear()


def test_unknown_override_warns_once_on_stderr_and_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "pi")
    monkeypatch.setenv("CODEX_HOME", "/nonexistent-codex-home")
    assert runtime_env.detect_runtime() == "codex"
    err = capsys.readouterr().err
    assert "TOKEN_OPTIMIZER_RUNTIME" in err and "'pi'" in err
    assert "claude" in err and "codex" in err  # lists the accepted values
    runtime_env.detect_runtime.cache_clear()
    runtime_env.detect_runtime()
    assert capsys.readouterr().err == ""  # once per process, even past the cache


def test_pi_warning_points_at_pi_home(monkeypatch, capsys):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "pi")
    runtime_env.detect_runtime()
    assert "TOKEN_OPTIMIZER_PI_HOME" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", "  ", "claude", "CODEX"])
def test_valid_or_empty_override_is_silent(monkeypatch, capsys, value):
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", value)
    runtime_env.detect_runtime()
    assert capsys.readouterr().err == ""
