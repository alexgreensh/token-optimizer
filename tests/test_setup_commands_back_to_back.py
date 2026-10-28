"""Explicit setup commands typed back to back must not be refused.

Every settings.json write takes a lease. A finished hook-style write keeps the
lease reserved for ~10s so a herd of hooks cannot repeat the same mutation,
and the setup functions used that hook-style lease even when the user ran them
by hand. So ``setup-quality-bar`` then ``setup-smart-compact`` then
``setup-hook`` printed "settings.json write was refused" for the second and
third. The CLI entrypoints now pass ``user_initiated=True``; the automatic heal
callers (SessionStart / UserPromptSubmit / PostToolUse runners, ensure-health,
quality_cache_gate) keep the default and still respect a fresh lease.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402

USER_SETTINGS = {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}


def _env(home: Path) -> dict:
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "TMPDIR": str(home / "tmp"),
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }


def test_setup_commands_back_to_back_all_land(tmp_path):
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    skill = home / ".claude" / "skills" / "token-optimizer"
    shutil.copytree(REPO / "skills" / "token-optimizer", skill,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    measure_py = skill / "scripts" / "measure.py"

    for verb in ("setup-quality-bar", "setup-smart-compact", "setup-hook"):
        # No lease release between commands: exactly what a user typing them does.
        proc = subprocess.run([sys.executable, str(measure_py), verb], capture_output=True,
                              text=True, env=_env(home), cwd=str(home), timeout=240)
        assert proc.returncode == 0, (verb, proc.stdout, proc.stderr)
        assert "refused" not in proc.stderr + proc.stdout, (verb, proc.stderr)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [h["command"] for groups in settings["hooks"].values()
                for g in groups for h in g["hooks"]]
    assert "statusline.js" in settings["statusLine"]["command"]           # quality bar
    assert any("quality-cache" in c for c in commands)                     # quality bar
    assert any("compact-capture" in c for c in commands)                   # smart compact
    assert any("compact-restore" in c for c in commands)                   # smart compact
    assert "compactInstructions" in settings                               # smart compact
    assert any("session-end-flush" in c for c in commands)                 # setup-hook
    assert "echo mine" in commands and settings["theme"] == "dark"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude)
    monkeypatch.setattr(measure, "SETTINGS_PATH", claude / "settings.json")
    monkeypatch.setattr(measure, "_SETTINGS_LOCK_PATH", claude / ".settings.lock")
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path / "data")
    monkeypatch.setattr(measure, "DAEMON_LOG_DIR", tmp_path / "data" / "logs")
    for name in ("_remote_lean_profile", "is_claude_code_cloud", "_cloud_kit_owns_hooks"):
        monkeypatch.setattr(measure, name, lambda: False)
    measure.SETTINGS_PATH.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    return claude


def _hook_style_write(claude: Path):
    """A heal/hook write from ANOTHER process (as real hooks are): not
    user-initiated, so it leaves the herd reservation on the lease. A
    same-process writer may reuse its own tombstone, so this must not run
    in-process."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import measure, json; "
        "s, ok = measure._read_settings_for_write(); assert ok; "
        "s['hooks'] = dict(s['hooks']); "
        "sys.exit(0 if measure._write_settings_atomic(s) else 3)"
    )
    proc = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True,
                          text=True, env=_env(claude.parent), timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert measure.SETTINGS_PATH == claude / "settings.json"
    assert (claude / ".settings.lease").exists()


def test_automatic_heal_still_respects_a_fresh_lease(sandbox):
    _hook_style_write(sandbox)
    before = measure.SETTINGS_PATH.read_text(encoding="utf-8")
    # The self-heal callers' exact call shape: no user_initiated.
    measure.setup_quality_bar(quiet=True)
    assert measure.SETTINGS_PATH.read_text(encoding="utf-8") == before, \
        "an automatic heal bypassed the herd reservation"


def test_explicit_command_takes_the_lease_after_a_fresh_write(sandbox):
    _hook_style_write(sandbox)
    measure.setup_quality_bar(quiet=True, user_initiated=True)
    settings = json.loads(measure.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "statusLine" in settings
