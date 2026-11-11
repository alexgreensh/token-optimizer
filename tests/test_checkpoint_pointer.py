"""U1 — drop the blind recency fallback in the SessionStart pointer.

The new-session pointer must NOT surface a checkpoint that fails the relevance
threshold, even when one is recent (R1). The old code fell back to
``checkpoints[0]`` (most recent from any project) when no cwd-prefix match
existed, so an unrelated/fresh session got pointed at an irrelevant checkpoint.
Now: when no cwd-prefix match exists, candidates are scored with the U2 relevance
scorer against the cwd-derived opening context, and the pointer fires ONLY if the
best score clears ``CHECKPOINT_RELEVANCE_THRESHOLD``. The cross-session label +
"NOT your own prior work" framing and the filename-format warning path are
preserved. The age gate (24 hours) and own-session exclusion are preserved.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_OTHER_SID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _cp(tmp_path, filename, active_task, work_paths, age_seconds=60,
        trigger="stop"):
    """Write a checkpoint .md + .json sidecar with the given work paths."""
    cp = tmp_path / filename
    cp.write_text("# Session State Checkpoint\n# Generated: test\nbody\n",
                  encoding="utf-8")
    sidecar = {
        "version": 1, "generated": "test", "trigger": trigger,
        "session_id": _OTHER_SID,
        "active_task": active_task,
        "decisions": [],
        "modified_files": [{"path": p, "action": "edit", "range": None}
                           for p in work_paths],
        "recent_reads": [],
    }
    (tmp_path / cp.name.replace(".md", ".json")).write_text(
        json.dumps(sidecar), encoding="utf-8")
    return {
        "filename": filename,
        "path": str(cp),
        "created": datetime.now() - timedelta(seconds=age_seconds),
        "trigger": trigger,
    }


def _run(m, monkeypatch, tmp_path, checkpoints, cwd, session_id="live-session-id"):
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(m, "list_checkpoints", lambda: checkpoints)
    # Use the real _checkpoint_work_paths (reads sidecar) and real scorer so the
    # gate is exercised end-to-end.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.compact_restore(session_id=session_id, cwd=str(cwd),
                          new_session_only=True)
    return buf.getvalue()


# --- S1: recent checkpoint from a DIFFERENT project, unrelated opening -> EMPTY ---

def test_irrelevant_recent_checkpoint_yields_empty(m, tmp_path, monkeypatch):
    proj = tmp_path / "marketing-audit"
    proj.mkdir()
    cp = _cp(tmp_path, "a1b2c3d4-20260811-120000-checkpoint.md",
             active_task="fix checkpoint injection in token optimizer",
             work_paths=["/Users/alex/projects/token-optimizer/measure.py"])
    out = _run(m, monkeypatch, tmp_path, [cp], cwd=proj)
    assert out.strip() == "", (
        f"irrelevant recent checkpoint must NOT surface a pointer; got: {out!r}")


# --- S2: recent checkpoint whose sidecar topic matches the opening -> fires + label ---

def test_relevant_topic_match_fires_with_cross_session_label(m, tmp_path, monkeypatch):
    proj = tmp_path / "token-optimizer"
    proj.mkdir()
    # Work paths live under a DIFFERENT folder (so cwd-prefix match is None),
    # but the sidecar topic names the current project -> content relevance fires.
    cp = _cp(tmp_path, "a1b2c3d4-20260811-120000-checkpoint.md",
             active_task="fix checkpoint injection in token optimizer",
             work_paths=["/Users/alex/projects/other/token-optimizer/measure.py"])
    out = _run(m, monkeypatch, tmp_path, [cp], cwd=proj)
    assert "Cross-session checkpoint" in out, (
        f"relevant checkpoint must fire with the cross-session label; got: {out!r}")
    assert "a1b2c3d4" in out, "source-session label must be preserved"
    assert "Not your session" in out, (
        "the 'not your own prior work' framing must be preserved")


# --- S3: own-session checkpoint only -> nothing ---

def test_own_session_checkpoint_yields_nothing(m, tmp_path, monkeypatch):
    proj = tmp_path / "token-optimizer"
    proj.mkdir()
    live_sid = "bbbb5e6f-1234-4abc-8def-111122223333"
    # Filename contains the live session id -> excluded as own-session.
    cp = _cp(tmp_path, f"{live_sid}-20260811-120000-checkpoint.md",
             active_task="fix checkpoint injection in token optimizer",
             work_paths=[str(proj / "measure.py")])
    out = _run(m, monkeypatch, tmp_path, [cp], cwd=proj, session_id=live_sid)
    assert out.strip() == "", (
        f"own-session checkpoint must never surface a pointer; got: {out!r}")


# --- S4: old checkpoints do not produce a pointer ---

def test_stale_checkpoints_yield_nothing(m, tmp_path, monkeypatch):
    proj = tmp_path / "token-optimizer"
    proj.mkdir()
    cp = _cp(tmp_path, "a1b2c3d4-20260811-120000-checkpoint.md",
             active_task="fix checkpoint injection in token optimizer",
             work_paths=[str(proj / "measure.py")],
             age_seconds=25 * 60 * 60)
    out = _run(m, monkeypatch, tmp_path, [cp], cwd=proj)
    assert out.strip() == "", (
        f"stale checkpoint (>24h) must not surface a pointer; got: {out!r}")


def test_relevant_checkpoint_from_previous_evening_is_offered(m, tmp_path, monkeypatch):
    proj = tmp_path / "project"
    proj.mkdir()
    cp = _cp(tmp_path, "a1b2c3d4-20260811-120000-stop.md",
             active_task="continue project", work_paths=[str(proj / "app.py")],
             age_seconds=12 * 60 * 60)
    out = _run(m, monkeypatch, tmp_path, [cp], cwd=proj)
    assert "Cross-session checkpoint" in out and "Not your session" in out
