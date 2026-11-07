"""doctor and health list each hook the malformed-hook healer left alone:
one plain line with what it is, where it lives, that Token Optimizer left it
alone, and that the user can delete it. Read-only; never changes the score.

Run: python3 -m pytest tests/test_heal_report.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
OWN = f"{REPO}/skills/token-optimizer/scripts/measure.py"
CLAUDE_WRITTEN = f"python3 $(which python3) {OWN} ensure-health"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-hr-"))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    import measure
    claude = tmp_path / "claude"
    claude.mkdir()
    settings = claude / "settings.json"
    monkeypatch.setattr(measure, "CONFIG_DIR", tmp_path / "to-config")
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude)
    monkeypatch.setattr(measure, "SETTINGS_PATH", settings)
    monkeypatch.setattr(measure, "_SETTINGS_LOCK_PATH", claude / ".settings.lock")
    monkeypatch.setattr(measure, "_find_plugin_hooks_json", lambda: REPO / "hooks" / "hooks.json")
    monkeypatch.setattr(measure, "_is_foreign_runtime", lambda: False)
    yield measure
    sys.modules.pop("measure", None)


def _settings(m, *commands, event="PreToolUse"):
    m.SETTINGS_PATH.write_text(json.dumps({"hooks": {event: [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": c} for c in commands]}]}}),
        encoding="utf-8")


def _old_template(m):
    import hook_provenance as hp
    return next(m._resolve_hook_command(t, REPO) for t in sorted(hp.SHIPPED_HOOK_TEMPLATES)
                if "${D%/*}" in t and hp.looks_malformed(m._resolve_hook_command(t, REPO), False))


def _doctor_text(m, capsys):
    m.doctor(as_json=False)
    return capsys.readouterr().out


def test_doctor_lists_each_left_alone_hook_in_one_plain_line(m, capsys):
    _settings(m, CLAUDE_WRITTEN, CLAUDE_WRITTEN, "echo unrelated $(date)", _old_template(m))
    before = m.SETTINGS_PATH.read_bytes()
    out = _doctor_text(m, capsys)
    lines = [line for line in out.splitlines() if "Hook left alone" in line]
    assert len(lines) == 1, out  # duplicate entry shown once; unrelated and proven-ours not shown
    line = lines[0]
    assert "PreToolUse hook `python3 $(which python3)" in line          # what it is
    assert str(m.SETTINGS_PATH) in line                                 # where it lives
    assert "not written by Token Optimizer" in line and "left it alone" in line
    assert "Delete it there" in line
    assert m.SETTINGS_PATH.read_bytes() == before  # doctor stays read-only


def test_doctor_rows_do_not_change_the_score(m, capsys):
    _settings(m, "echo fine")
    clean = m.doctor(as_json=True)
    capsys.readouterr()
    _settings(m, "echo fine", CLAUDE_WRITTEN)
    flagged = m.doctor(as_json=True)
    capsys.readouterr()
    assert (flagged["score"], flagged["total"]) == (clean["score"], clean["total"])
    rows = [c for c in flagged["checks"] if c["name"] == "Hook left alone"]
    assert len(rows) == 1 and rows[0]["status"] == "WARN"
    assert not [c for c in clean["checks"] if c["name"] == "Hook left alone"]
    assert any(c["name"] == "Settings self-heal" for c in flagged["checks"])  # moved row kept


def test_line_disappears_once_the_user_deletes_the_hook(m, capsys):
    _settings(m, CLAUDE_WRITTEN)
    assert "Hook left alone" in _doctor_text(m, capsys)
    _settings(m, "echo fine")
    assert "Hook left alone" not in _doctor_text(m, capsys)


def test_long_commands_are_shortened(m):
    import heal_report
    long_cmd = CLAUDE_WRITTEN + " --pad " + "x" * 400
    line = heal_report.describe("Stop", long_cmd, "/u/.claude/settings.json")
    assert len(line) < 330 and "..." in line and "/u/.claude/settings.json" in line


def test_health_output_has_the_section_only_when_needed(m, capsys, monkeypatch):
    monkeypatch.setattr(m, "_collect_health_data", lambda: {
        "installed_version": "2.1.0", "running_sessions": [], "automated": [], "recommendations": []})
    _settings(m, "echo fine")
    m.session_health()
    clean = capsys.readouterr().out
    assert "HOOKS LEFT ALONE" not in clean
    _settings(m, CLAUDE_WRITTEN)
    m.session_health()
    out = capsys.readouterr().out
    assert "HOOKS LEFT ALONE (1)" in out and "left it alone" in out and str(m.SETTINGS_PATH) in out
    assert out.count("\n") > clean.count("\n")


def test_heal_log_entry_says_where_the_hook_lives(m):
    _settings(m, CLAUDE_WRITTEN)
    assert m._fix_malformed_hook_commands() == 0
    log = (m._settings_heal_dir() / "heal_log.jsonl").read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in log if '"hook_unproven"' in line]
    assert len(entries) == 1 and entries[0]["settings"] == str(m.SETTINGS_PATH)


def test_installed_hooks_are_never_listed(m, capsys, monkeypatch):
    m.SETTINGS_PATH.write_text(json.dumps({"_test_marker": True}), encoding="utf-8")
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    monkeypatch.setattr(m, "_is_running_from_plugin_cache", lambda: False)
    monkeypatch.setattr(m, "_is_running_from_synced_plugin", lambda: False)
    assert m.setup_all_hooks(dry_run=False, verbose=False)["added"] > 0
    import heal_report
    assert heal_report.unproven_hooks(m) == []
