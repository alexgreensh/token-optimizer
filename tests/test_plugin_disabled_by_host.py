"""Regression: _plugin_disabled_by_host must honor CLAUDE_CONFIG_DIR.

The self-check previously hardcoded ~/.claude/settings.json, so every
CLAUDE_CONFIG_DIR user (containers, CI, relocated config volumes) read the
wrong file and the disable was never honored. The fix routes the settings path
through _claude_settings_path(), which mirrors the consent resolver's
CLAUDE_CONFIG_DIR handling (absolute/existing/non-symlink, else ~/.claude).

Also covers the baseline: ~/.claude fallback when CLAUDE_CONFIG_DIR is unset,
fail-open on missing settings, and the explicit-disable detection itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))


def _set_home(monkeypatch, path):
    """Redirect ~ for the test on every OS. Path.home() reads HOME on POSIX but
    USERPROFILE on Windows, so setting only HOME leaves ~ pointing at the real
    profile on Windows and the ~/.claude fallback reads the wrong settings.json."""
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


@pytest.fixture()
def plugin_meta(tmp_path, monkeypatch):
    """Build a fake plugin root with .claude-plugin/{plugin,marketplace}.json
    and point CLAUDE_PLUGIN_ROOT at it. Returns the plugin root."""
    root = tmp_path / "plugin"
    meta = root / ".claude-plugin"
    meta.mkdir(parents=True)
    (meta / "plugin.json").write_text(
        json.dumps({"name": "token-optimizer"}), encoding="utf-8"
    )
    (meta / "marketplace.json").write_text(
        json.dumps({"name": "alexgreensh-token-optimizer"}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    return root


def _settings_with_disabled(disabled: bool) -> dict:
    return {
        "enabledPlugins": {
            "token-optimizer@alexgreensh-token-optimizer": (not disabled),
            "other-plugin@other-market": True,
        }
    }


def _write_settings_home(home_dir: Path, settings: dict) -> Path:
    """Write settings.json at ~/.claude/settings.json (the HOME fallback path)."""
    claude = home_dir / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    p = claude / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    return p


def _write_settings_config_dir(config_dir: Path, settings: dict) -> Path:
    """Write settings.json directly under CLAUDE_CONFIG_DIR (which IS the
    .claude equivalent, so settings.json lives at $CLAUDE_CONFIG_DIR/settings.json,
    NOT $CLAUDE_CONFIG_DIR/.claude/settings.json)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    p = config_dir / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    return p


def test_disabled_when_settings_says_false(plugin_meta, tmp_path, monkeypatch):
    """Baseline: ~/.claude/settings.json with enabledPlugins[key]=false -> True."""
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_settings_home(tmp_path, _settings_with_disabled(disabled=True))
    import run
    assert run._plugin_disabled_by_host() is True


def test_not_disabled_when_settings_says_true(plugin_meta, tmp_path, monkeypatch):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_settings_home(tmp_path, _settings_with_disabled(disabled=False))
    import run
    assert run._plugin_disabled_by_host() is False


def test_fail_open_when_no_settings(plugin_meta, tmp_path, monkeypatch):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    import run
    assert run._plugin_disabled_by_host() is False


def test_claude_config_dir_honored_when_disabled(plugin_meta, tmp_path, monkeypatch):
    """Core: CLAUDE_CONFIG_DIR points elsewhere; the disable must be read
    from $CLAUDE_CONFIG_DIR/settings.json, not ~/.claude/settings.json."""
    home = tmp_path / "home"
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    _set_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))
    # ~/.claude has an ENABLED settings.json (would mask the bug).
    _write_settings_home(home, _settings_with_disabled(disabled=False))
    # $CLAUDE_CONFIG_DIR has the DISABLED settings.json (the real intent).
    _write_settings_config_dir(relocated, _settings_with_disabled(disabled=True))
    import run
    assert run._plugin_disabled_by_host() is True, (
        "CLAUDE_CONFIG_DIR settings.json must be read, not the ~/.claude fallback"
    )


def test_claude_config_dir_honored_when_enabled(plugin_meta, tmp_path, monkeypatch):
    """Symmetric: CLAUDE_CONFIG_DIR with enabledPlugins[key]=true -> False,
    even if ~/.claude says disabled (must not read the wrong file)."""
    home = tmp_path / "home"
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    _set_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))
    _write_settings_home(home, _settings_with_disabled(disabled=True))
    _write_settings_config_dir(relocated, _settings_with_disabled(disabled=False))
    import run
    assert run._plugin_disabled_by_host() is False


def test_claude_config_dir_symlink_rejected(plugin_meta, tmp_path, monkeypatch):
    """A symlinked CLAUDE_CONFIG_DIR must be rejected (fall back to ~/.claude),
    mirroring the consent resolver and runtime_env.claude_home()."""
    home = tmp_path / "home"
    real = tmp_path / "real-claude"
    real.mkdir()
    link = tmp_path / "linked-claude"
    link.symlink_to(real)
    _set_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
    # ~/.claude says disabled; the symlink target has no settings.json.
    _write_settings_home(home, _settings_with_disabled(disabled=True))
    import run
    assert run._plugin_disabled_by_host() is True, (
        "symlinked CLAUDE_CONFIG_DIR rejected -> ~/.claude fallback used"
    )


def test_claude_config_dir_relative_rejected(plugin_meta, tmp_path, monkeypatch):
    """A relative CLAUDE_CONFIG_DIR must be rejected -> ~/.claude fallback."""
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/claude")
    _write_settings_home(home, _settings_with_disabled(disabled=True))
    import run
    assert run._plugin_disabled_by_host() is True
