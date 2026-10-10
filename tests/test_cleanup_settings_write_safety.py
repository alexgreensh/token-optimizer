#!/usr/bin/env python3
"""Cleanup must never lie about a write, and never clobber.

Two data-safety P1s from the review of 6d2400e:

P1a -- LEASE MISS REPORTED AS SUCCESS. ``_write_settings_atomic`` returns
early and silently when the advisory settings lease is denied. ``cleanup``
ignored the return value and printed "Removed: statusLine" + "Cleanup
complete" for a write that never landed, leaving the user with exactly the
dangling statusLine the self-disabling guard exists to fix. Any settings write in the prior few
seconds (SessionStart ensure-health, quality-bar heal) triggers it.

P1b -- FAILED RE-READ CLOBBERS THE FILE. ``_read_settings_json`` returns
``{}`` for malformed JSON / permission errors, indistinguishable from a
genuinely empty file. ``cleanup`` re-read after backup and wrote the result
back, so a file that became unreadable between the two reads was replaced
with ``{}`` -- every user key destroyed.

Contract pinned here: report only what actually happened, and never write a
dict that came from a failed read.

Run: python3 -m pytest tests/test_cleanup_settings_write_safety.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

USER_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "opus",
    "theme": "dark",
    "env": {"MY_KEY": "keep-me"},
    "permissions": {"allow": ["Bash(ls:*)"]},
    "statusLine": {"type": "command", "command": "node '/gone/token-optimizer/statusline.js'"},
}


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "claude"
    home.mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    settings.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    # Daemon + manifest stages are out of scope here.
    monkeypatch.setattr(mod, "setup_daemon", lambda **kw: None)
    yield mod, settings
    sys.modules.pop("measure", None)


def _read(settings_path: Path) -> dict:
    return json.loads(settings_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# P1a: a denied lease must be reported, never claimed as a removal
# ---------------------------------------------------------------------------

def test_lease_miss_is_reported_not_silent(measure, capsys, monkeypatch):
    mod, settings = measure
    monkeypatch.setattr(mod, "_write_settings_atomic", lambda data, **kw: False)

    mod.cleanup(dry_run=False)
    out = capsys.readouterr().out

    assert "WARNING" in out and "locked by another" in out, (
        "a denied settings lease was not surfaced; cleanup silently no-opped"
    )
    assert "Removed: statusLine" not in out, (
        "cleanup claimed it removed statusLine while the write never landed"
    )
    # The file is untouched -- the user's dangling entry is still there to retry.
    assert _read(settings) == USER_SETTINGS


def test_write_atomic_returns_false_on_lease_miss(measure, monkeypatch):
    """The return contract itself (callers depend on it)."""
    mod, _settings = measure

    class _Denied:
        def __enter__(self):
            return False

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_settings_lock", lambda **kw: _Denied())
    assert mod._write_settings_atomic({"a": 1}) is False


def test_write_atomic_returns_true_on_success(measure):
    """A landing write still returns True.

    The payload must be a SUPERSET of what is on disk. Writing a bare
    ``{"a": 1}`` over the populated fixture is now refused by the
    ``_settings_write_guard`` choke point -- that refusal is the whole point of
    the fix (see tests/test_settings_never_clobbered.py), so this test asserts
    the return contract with a non-destructive payload instead.
    """
    mod, settings = measure
    payload = dict(USER_SETTINGS, a=1)
    assert mod._write_settings_atomic(payload) is True
    assert _read(settings) == payload


def test_write_atomic_refuses_a_key_dropping_payload(measure):
    """Companion to the above: the destructive shape must NOT land."""
    mod, settings = measure
    assert mod._write_settings_atomic({"a": 1}) is False
    assert _read(settings) == USER_SETTINGS


# ---------------------------------------------------------------------------
# P1b: a failed read must never be written back
# ---------------------------------------------------------------------------

def test_failed_reread_never_clobbers_settings(measure, capsys, monkeypatch):
    mod, settings = measure
    original = settings.read_text(encoding="utf-8")
    wrote: list = []
    monkeypatch.setattr(
        mod, "_write_settings_atomic", lambda data, **kw: (wrote.append(data), True)[1]
    )

    # First read (entry detection) succeeds; the re-read after backup fails.
    calls = {"n": 0}
    real = mod._read_settings_json_checked

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return real()
        return {}, settings, False

    monkeypatch.setattr(mod, "_read_settings_json_checked", flaky)

    mod.cleanup(dry_run=False)
    out = capsys.readouterr().out

    assert wrote == [], f"cleanup wrote a dict from a FAILED read: {wrote}"
    assert settings.read_text(encoding="utf-8") == original, (
        "settings.json was modified despite an unreadable re-read"
    )
    assert "WARNING" in out and "unreadable" in out
    assert "Removed: statusLine" not in out


def test_read_checked_distinguishes_malformed_from_empty(measure):
    mod, settings = measure

    settings.write_text("{ not json", encoding="utf-8")
    data, _p, ok = mod._read_settings_json_checked()
    assert (data, ok) == ({}, False), "malformed JSON must report ok=False"

    settings.unlink()
    data, _p, ok = mod._read_settings_json_checked()
    assert (data, ok) == ({}, True), "a missing file is genuinely empty (ok=True)"

    settings.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    data, _p, ok = mod._read_settings_json_checked()
    assert ok is True and data["model"] == "opus"


def test_legacy_read_helper_still_returns_two_tuple(measure):
    """Dozens of read-only callers unpack two values -- keep that contract."""
    mod, _settings = measure
    data, path = mod._read_settings_json()
    assert isinstance(data, dict) and path is not None


# ---------------------------------------------------------------------------
# Happy path still works, and user keys survive
# ---------------------------------------------------------------------------

def test_successful_cleanup_reports_and_preserves_user_keys(measure, capsys):
    mod, settings = measure

    mod.cleanup(dry_run=False)
    out = capsys.readouterr().out

    assert "Removed: statusLine" in out
    assert "WARNING" not in out
    after = _read(settings)
    assert "statusLine" not in after, "our dangling statusLine should be gone"
    for key in ("$schema", "model", "theme", "env", "permissions"):
        assert after[key] == USER_SETTINGS[key], f"user key {key} was altered"
