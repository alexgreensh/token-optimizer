r"""Checkpoint filename regex guard.

The cross-session warning relies on a regex match against the checkpoint
filename: `^([0-9a-fA-F-]{8,36})-\d{8}-\d{6}-`. If the filename format ever
changes, the match fails and the old code fell through to the unlabeled "A
recent checkpoint is available" message -- the exact cross-session hazard the
labeled warning was written to eliminate.

The fix: (a) emit a stderr warning when the regex doesn't match so a format
change is visible at dev time, and (b) keep the cross-session "DIFFERENT
session" label even without the source sid, so the fallback never silently
reverts to the old unlabeled pointer.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch, tmp_path):
    if "measure" in sys.modules:
        del sys.modules["measure"]
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _fake_checkpoint(tmp_path, filename, age_seconds=60):
    """Build a fake checkpoint dict matching list_checkpoints() shape."""
    cp_path = tmp_path / filename
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.write_text("# Session State Checkpoint\n# Generated: test\nbody\n", encoding="utf-8")
    return {
        "filename": filename,
        "path": str(cp_path),
        "created": datetime.now() - timedelta(seconds=age_seconds),
    }


def test_matching_filename_uses_labeled_warning(m, tmp_path, monkeypatch, capsys):
    """Baseline: a filename matching the regex gets the labeled cross-session
    warning with the source sid."""
    cp = _fake_checkpoint(tmp_path, "a1b2c3d4-12345678-20260808-120000-checkpoint.md")
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(m, "list_checkpoints", lambda: [cp])
    monkeypatch.setattr(m, "_checkpoint_descriptor", lambda p: "test-project")
    # U1: the pointer now fires via a cwd-prefix (same-work) match, not the
    # removed blind recency fallback. Put the checkpoint's work under cwd so
    # the label-format path is reached and the F10 guard is still exercised.
    monkeypatch.setattr(m, "_checkpoint_work_paths", lambda p: [str(tmp_path / "src" / "file.py")])
    m.compact_restore(session_id="different-session-id", cwd=str(tmp_path), new_session_only=True)
    out = capsys.readouterr().out
    assert "Cross-session checkpoint" in out
    assert "a1b2c3d4" in out


def test_non_matching_filename_still_labels_cross_session(m, tmp_path, monkeypatch, capsys):
    """A filename that does NOT match the regex must still get the
    cross-session label, NOT the old unlabeled message."""
    cp = _fake_checkpoint(tmp_path, "new-format-checkpoint-2026.md")
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(m, "list_checkpoints", lambda: [cp])
    monkeypatch.setattr(m, "_checkpoint_descriptor", lambda p: "test-project")
    monkeypatch.setattr(m, "_checkpoint_work_paths", lambda p: [str(tmp_path / "src" / "file.py")])
    m.compact_restore(session_id="different-session-id", cwd=str(tmp_path), new_session_only=True)
    captured = capsys.readouterr()
    assert "Cross-session checkpoint" in captured.out, (
        "non-matching filename must still get the cross-session label (F10)"
    )
    assert "A recent checkpoint is available" not in captured.out, (
        "the old unlabeled message must not appear for a non-matching filename (F10)"
    )


def test_non_matching_filename_emits_stderr_warning(m, tmp_path, monkeypatch, capsys):
    """A non-matching filename must emit a stderr warning so a format
    change is visible at dev time."""
    cp = _fake_checkpoint(tmp_path, "new-format-checkpoint-2026.md")
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(m, "list_checkpoints", lambda: [cp])
    monkeypatch.setattr(m, "_checkpoint_descriptor", lambda p: "")
    monkeypatch.setattr(m, "_checkpoint_work_paths", lambda p: [str(tmp_path / "src" / "file.py")])
    m.compact_restore(session_id="different-session-id", cwd=str(tmp_path), new_session_only=True)
    captured = capsys.readouterr()
    assert "did not match" in captured.err, (
        "stderr warning must fire when the filename format changes (F10)"
    )
    assert "WARNING" in captured.err
