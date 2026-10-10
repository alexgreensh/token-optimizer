#!/usr/bin/env python3
"""Settings write safety: concurrent edits, non-regular files, undecodable bytes,
OS errors on the replace, refusal reasons, new-file modes, stale-lock reclaim.

Run: python3 -m pytest tests/test_settings_write_safety.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

BASE = {
    "model": "opus",
    "effortLevel": "high",
    "voice": "alloy",
    "env": {"MY_KEY": "keep-me", "OTHER": "1"},
    "permissions": {"allow": ["Bash(ls:*)"]},
}


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    """Load measure.py against a throwaway CLAUDE_DIR. Never touches ~/.claude."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)

    home = tmp_path / "claude"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    settings.write_text(json.dumps(BASE, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    yield mod, settings
    sys.modules.pop("measure", None)


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _inject_after_guard(mod, monkeypatch, edits):
    """Run each callable in ``edits`` right after one guard call returns.

    The guard runs after the merge's read and before os.replace, which is the
    narrowest window a real editor can hit (the merge-read-to-replace gap).
    """
    real = mod._settings_write_guard
    pending = list(edits)

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if pending:
            pending.pop(0)()
        return result

    monkeypatch.setattr(mod, "_settings_write_guard", wrapped)


# ---------------------------------------------------------------------------
# F1: an edit that lands between the merge read and os.replace is re-merged
# ---------------------------------------------------------------------------

def _human_save(settings, mutate):
    def go():
        data = _read(settings)
        mutate(data)
        settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return go


@pytest.mark.parametrize("name,mutate,check", [
    ("value_edit",
     lambda d: d.update(model="claude-human-picked"),
     lambda d: d["model"] == "claude-human-picked"),
    ("key_delete",
     lambda d: d.pop("voice"),
     lambda d: "voice" not in d),
    ("env_delete",
     lambda d: d["env"].pop("OTHER"),
     lambda d: "OTHER" not in d["env"]),
    ("env_to_string",
     lambda d: d.update(env="oops"),
     lambda d: d["env"] == "oops" or d["env"].get("TO_VAR") == "ours"),
])
def test_edit_between_merge_read_and_replace_is_remerged(measure, monkeypatch, name, mutate, check):
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok
    stale["effortLevel"] = "low"  # our change
    # A concurrent edit that lands in the file between the unrelated edit the
    # merge already saw and the replace. Force a first merge by making an
    # earlier edit, so the injected one is NOT visible to the merge read.
    settings.write_text(json.dumps({**BASE, "cleanupPeriodDays": 5}, indent=2) + "\n", encoding="utf-8")
    _inject_after_guard(mod, monkeypatch, [_human_save(settings, mutate)])

    assert mod._write_settings_atomic(stale) is True
    on_disk = _read(settings)
    assert check(on_disk), f"{name}: the concurrent edit was reverted: {on_disk}"
    assert on_disk["effortLevel"] == "low", "our own change was lost"
    assert on_disk["cleanupPeriodDays"] == 5


def test_edit_in_window_when_merge_saw_an_unchanged_file(measure, monkeypatch):
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok
    stale["effortLevel"] = "low"
    _inject_after_guard(mod, monkeypatch, [_human_save(settings, lambda d: d.update(model="human"))])

    assert mod._write_settings_atomic(stale) is True
    on_disk = _read(settings)
    assert on_disk["model"] == "human"
    assert on_disk["effortLevel"] == "low"


def test_two_changes_in_a_row_refuse_with_a_reason_and_keep_the_last_edit(measure, monkeypatch):
    mod, settings = measure
    stale, ok = mod._read_settings_for_write()
    assert ok
    stale["effortLevel"] = "low"
    _inject_after_guard(mod, monkeypatch, [
        _human_save(settings, lambda d: d.update(model="human-1")),
        _human_save(settings, lambda d: d.update(model="human-22")),
    ])

    assert mod._write_settings_atomic(stale) is False
    assert _read(settings)["model"] == "human-22", "the last human edit was reverted"
    reason = getattr(mod._SETTINGS_WRITE_READ_STATE, "last_refusal", "")
    assert "changed" in reason and "settings.json" in reason, reason
    leftovers = [p.name for p in settings.parent.iterdir() if p.name.startswith(".settings-")]
    assert leftovers == [], "temp file left behind"
