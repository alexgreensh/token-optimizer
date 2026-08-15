"""Regression coverage for Codex hook consent resolution."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "hooks" / "run.py"


def _load_run_py():
    spec = importlib.util.spec_from_file_location("run_py_consent_test", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_config(home: Path, runtime_dir: str, payload: dict) -> None:
    config_dir = home / runtime_dir / "token-optimizer"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_explicit_codex_runtime_uses_default_codex_home_without_codex_home_env(monkeypatch, tmp_path):
    """Codex hooks must not consult Claude consent when CODEX_HOME is unset."""
    module = _load_run_py()
    _write_config(tmp_path, ".codex", {})
    _write_config(tmp_path, ".claude", {"enterprise_consent_shown": True})

    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "codex")

    assert module._check_consent() is False


def test_explicit_codex_runtime_reads_consent_from_default_codex_home(monkeypatch, tmp_path):
    module = _load_run_py()
    _write_config(tmp_path, ".codex", {"enterprise_consent_shown": True})

    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "codex")

    assert module._check_consent() is True
