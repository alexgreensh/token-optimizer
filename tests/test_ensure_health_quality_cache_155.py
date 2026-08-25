#!/usr/bin/env python3
"""Regression: ensure-health must not re-add the legacy quality-cache hook (#155).

Since #139 (a299bf7, v5.11.93) the shipped ``UserPromptSubmit`` hook is a single
in-process dispatcher whose command execs ``hooks/userpromptsubmit_runner.py``
and runs ``quality-cache`` INSIDE the runner. The literal ``quality-cache``
substring no longer appears in the hook command.

Before the fix, the three detection sites in measure.py tested only for the
substring ``"quality-cache"``, so they treated the consolidated dispatcher as
"hook missing". ensure-health then called ``setup_quality_bar`` which appended a
fresh legacy ``python3 '<mp>' quality-cache --quiet`` group -- running
quality-cache twice per prompt and re-introducing the per-prompt blocking cost
#139 removed. The duplicate returned on every SessionStart.

These tests pin the fix: with the canonical #139 dispatcher already present,
the quality-cache hook is recognized as installed and setup_quality_bar does
NOT append a legacy hook.

Run: python3 -m pytest tests/test_ensure_health_quality_cache_155.py -v
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


# The exact canonical UserPromptSubmit command shipped since #139 (hooks.json).
# It runs quality-cache inside userpromptsubmit_runner.py and, crucially, does
# NOT contain the literal substring "quality-cache".
_DISPATCHER_CMD = (
    'for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash /opt/homebrew/bin/bash; do '
    'command -v "$b" >/dev/null 2>&1 && exec "$b" '
    '"${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh" '
    '"${CLAUDE_PLUGIN_ROOT}/hooks/run.py" hooks/userpromptsubmit_runner.py; done; exit 0'
)


def _dispatcher_group():
    return {"hooks": [{"type": "command", "command": _DISPATCHER_CMD}]}


def _legacy_group():
    return {"hooks": [{"type": "command", "command": "python3 '/x/measure.py' quality-cache --quiet"}]}


def _count_legacy_hooks(settings):
    """Number of standalone legacy `quality-cache --quiet` UserPromptSubmit hooks."""
    n = 0
    for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
        for hook in group.get("hooks", []):
            cmd = hook.get("command", "")
            if "quality-cache" in cmd and "userpromptsubmit_runner.py" not in cmd:
                n += 1
    return n


# --- premise: the dispatcher command really has no "quality-cache" substring ---

def test_dispatcher_command_has_no_literal_quality_cache_substring():
    # Guards the whole point of #155: the naive substring check silently misses
    # the consolidated dispatcher.
    assert "quality-cache" not in _DISPATCHER_CMD


# --- unit: the detection helpers recognize both hook shapes ---

def test_command_helper_recognizes_both_shapes():
    assert measure._command_drives_quality_cache(_DISPATCHER_CMD) is True
    assert measure._command_drives_quality_cache("python3 '/x/measure.py' quality-cache --quiet") is True
    assert measure._command_drives_quality_cache("echo hello") is False
    assert measure._command_drives_quality_cache("") is False
    assert measure._command_drives_quality_cache(None) is False


def test_group_helper_recognizes_dispatcher():
    assert measure._quality_cache_hook_present([_dispatcher_group()]) is True
    assert measure._quality_cache_hook_present([_legacy_group()]) is True
    assert measure._quality_cache_hook_present([]) is False
    assert measure._quality_cache_hook_present([{"hooks": [{"command": "echo hi"}]}]) is False


def test_is_quality_bar_installed_sees_dispatcher():
    settings = {"hooks": {"UserPromptSubmit": [_dispatcher_group()]}}
    assert measure._is_quality_bar_installed(settings)["hook"] is True


# --- integration: setup_quality_bar (what ensure-health calls) is a no-op ---

def test_setup_quality_bar_does_not_readd_legacy_hook(monkeypatch, tmp_path):
    """With the #139 dispatcher + our statusline present, setup_quality_bar must
    not append a second, legacy quality-cache hook."""
    statusline_cmd = "node '/install/skills/token-optimizer/statusline.js'"
    settings = {
        "statusLine": {"type": "command", "command": statusline_cmd},
        "hooks": {"UserPromptSubmit": [_dispatcher_group()]},
    }
    settings_path = tmp_path / "settings.json"

    # Force a script-install view of the world and capture any write.
    monkeypatch.setattr(measure, "_is_running_from_plugin_cache", lambda: False)
    monkeypatch.setattr(measure, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(measure, "_read_settings_json", lambda: (settings, settings_path))

    written = {}
    monkeypatch.setattr(measure, "_write_settings_atomic", lambda data: written.update({"data": data}))
    monkeypatch.setattr(measure, "_set_quality_bar_disabled", lambda *a, **k: None)

    assert _count_legacy_hooks(settings) == 0, "precondition: no legacy hook yet"

    measure.setup_quality_bar(quiet=True)

    # Whether or not a write happened, no legacy quality-cache hook may exist.
    final = written.get("data", settings)
    assert _count_legacy_hooks(final) == 0, (
        "ensure-health/setup_quality_bar re-added the legacy quality-cache hook "
        "despite the #139 dispatcher already being installed (regression of #155)"
    )
    # The canonical dispatcher must still be present and untouched.
    cmds = [
        h.get("command", "")
        for g in final.get("hooks", {}).get("UserPromptSubmit", [])
        for h in g.get("hooks", [])
    ]
    assert any("userpromptsubmit_runner.py" in c for c in cmds), "dispatcher must be preserved"


def test_plugin_cache_fallback_recognizes_shipped_hook(monkeypatch, tmp_path):
    """The plugin-cache fallback in _is_quality_bar_installed must recognize the
    shipped canonical hooks.json dispatcher too (issue #155 notes it would not)."""
    import json

    plugin_hooks_dir = tmp_path / "plugins" / "cache" / "mkt" / "token-optimizer" / "1.0" / "hooks"
    plugin_hooks_dir.mkdir(parents=True)
    (plugin_hooks_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [_dispatcher_group()]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path)

    # settings.json has no UserPromptSubmit hook -> forces the plugin-cache fallback.
    result = measure._is_quality_bar_installed({"hooks": {}})
    assert result["hook"] is True
