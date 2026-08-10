#!/usr/bin/env python3
"""GitHub #129 — cold-resume-lean must not inject an UNRELATED session's
checkpoint with an unconditional "tell the user you reopened it" instruction.

Three fixes, one test file:
  1. build_lean_resume_context grows a ``footer_mode`` arg. The auto-inject path
     (_continuity_resume_block) passes "conditional" so the assistant VERIFIES the
     match before claiming a reopen; the explicit/CLI path stays "confident".
  2. _continuity_resume_block's vague-continue fallback (no topic named) only
     surfaces the most-recent same-project checkpoint when it is fresher than
     _RESUME_RECENCY_CAP_MIN; a stale freshest pick is declined ("" ) instead of
     blindly reopening whichever sibling session was last active in the folder.
  3. A session id NAMED in the prompt ("continue session <id>", full UUID) is
     honored strictly and never silently substituted; an id with no on-disk
     checkpoint returns "" rather than a different session.

Selection logic is exercised by calling _continuity_resume_block directly, which
bypasses the resume-intent gate and takes the checkpoint list explicitly.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

_UUID_A = "328c85e9-ada2-4bc7-8d0e-c7d03ff1313b"
_UUID_B = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


@pytest.fixture
def measure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-129-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    cp_dir = Path(tmp) / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir, raising=True)
    monkeypatch.setattr(mod, "TRENDS_DB", Path(tmp) / "trends.db", raising=True)
    # Isolate the selection logic: no savings-ledger writes, always in-project.
    monkeypatch.setattr(mod, "_log_resume_lean_savings", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(mod, "_checkpoint_in_project", lambda sc, cwd: True, raising=True)
    yield mod, cp_dir
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _sidecar(sid, task="work on the widget"):
    return {
        "session_id": sid,
        "active_task": task,
        "continuation": "left off mid-refactor",
        "decisions": ["Chose approach X"],
        "modified_files": [{"path": "/home/u/proj/src/widget.py"}],
        "recent_reads": ["/home/u/proj/README.md"],
        "git": {"branch": "main", "sha": "abc123"},
    }


def _write_checkpoint(cp_dir, sid, sidecar, age_minutes=0):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{sid}-{ts}-auto"
    md = cp_dir / f"{base}.md"
    md.write_text("# checkpoint\n", encoding="utf-8")
    (cp_dir / f"{base}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    if age_minutes:
        old = time.time() - age_minutes * 60
        os.utime(md, (old, old))
    return md


# ---------------------------------------------------------------------------
# Fix 1 — footer_mode wording
# ---------------------------------------------------------------------------

_CONFIDENT = "Tell the user you reopened the cold session"
_CONDITIONAL = "Use this only if it matches the user's current request"


def test_footer_default_is_confident(measure):
    """Default (CLI --resume-lean, no footer_mode) keeps the confident wording."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A))
    block = mod.build_lean_resume_context(_UUID_A)
    assert _CONFIDENT in block
    assert _CONDITIONAL not in block


def test_footer_conditional_mode(measure):
    """footer_mode='conditional' swaps to verify-before-claiming wording."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A))
    block = mod.build_lean_resume_context(_UUID_A, footer_mode="conditional")
    assert _CONDITIONAL in block
    assert _CONFIDENT not in block


# ---------------------------------------------------------------------------
# Fix 2 — vague fallback is staleness-capped + conditional
# ---------------------------------------------------------------------------

def test_vague_fresh_uses_conditional_footer(measure, monkeypatch):
    """Vague 'continue' + a FRESH same-project checkpoint: surfaced, but with the
    conditional footer (the plugin guessed, so the assistant must verify)."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A), age_minutes=5)
    monkeypatch.setattr(mod, "_resume_topic_score", lambda text, path: 0.0)  # below bar
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block("continue what we were doing", cps, None, "/home/u/proj")
    assert block
    assert _CONDITIONAL in block
    assert _CONFIDENT not in block


def test_vague_stale_is_declined(measure, monkeypatch):
    """Vague 'continue' + a STALE freshest checkpoint (older than the recency cap):
    declined entirely. This is the exact #129 harm — reopening whichever sibling
    session was last active in the folder — so we inject NOTHING."""
    mod, cp_dir = measure
    stale = mod._RESUME_RECENCY_CAP_MIN + 120
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A), age_minutes=stale)
    monkeypatch.setattr(mod, "_resume_topic_score", lambda text, path: 0.0)  # below bar
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block("continue what we were doing", cps, None, "/home/u/proj")
    assert block == ""


def test_named_topic_uses_conditional_footer(measure, monkeypatch):
    """Even the keyword-winner (topic named, score >= bar) is a best guess, so it
    also carries the conditional footer."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A), age_minutes=5)
    monkeypatch.setattr(mod, "_resume_topic_score", lambda text, path: 0.9)  # above bar
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block("continue the widget refactor", cps, None, "/home/u/proj")
    assert block
    assert _CONDITIONAL in block


# ---------------------------------------------------------------------------
# Fix 3 — explicit session id in the prompt is honored strictly
# ---------------------------------------------------------------------------

def test_extract_session_id_from_prompt(measure):
    mod, _ = measure
    f = mod._extract_session_id_from_prompt
    assert f(f"continue the work of session {_UUID_A}") == _UUID_A
    assert f("resume session 328c85e9") == "328c85e9"
    assert f("session id: deadbeefcafe1234") == "deadbeefcafe1234"
    # No id-shaped token -> None. A bare hex-ish word NOT after 'session' is ignored.
    assert f("continue what we were doing") is None
    assert f("refactor the deadbeef module") is None


def test_explicit_id_scopes_strictly(measure, monkeypatch):
    """Prompt names session A explicitly while B is the FRESHER sibling. The named
    id wins (not recency), and it gets the confident footer (user named it)."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A, "the alpha task"), age_minutes=200)
    _write_checkpoint(cp_dir, _UUID_B, _sidecar(_UUID_B, "the beta task"), age_minutes=1)
    # Would-be recency winner is B; explicit id must override that.
    monkeypatch.setattr(mod, "_resume_topic_score", lambda text, path: 0.0)
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block(
        f"continue session {_UUID_A}", cps, None, "/home/u/proj")
    assert "the alpha task" in block
    assert "the beta task" not in block
    assert _CONFIDENT in block  # explicitly named -> confident reopen is warranted


def test_explicit_id_no_match_returns_empty(measure, monkeypatch):
    """Naming an id with no on-disk checkpoint returns "" — never substitutes a
    different session (the core #129 guarantee)."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_B, _sidecar(_UUID_B, "the beta task"), age_minutes=1)
    monkeypatch.setattr(mod, "_resume_topic_score", lambda text, path: 0.0)
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block(
        "continue session ffffffffffff", cps, None, "/home/u/proj")
    assert block == ""


# ---------------------------------------------------------------------------
# CE review follow-ups (P1/P2/P3): drive the REAL hook path, not the internal fn
# ---------------------------------------------------------------------------

def test_hook_path_explicit_id_triggers_without_resume_verb(measure):
    """P1: the real hook entry (_continuity_prompt_hint) must reach the strict
    explicit-id branch for 'continue session <id>' even though _resume_intent
    rejects that phrasing. The gate now also fires on a named id."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A, "the alpha task"), age_minutes=5)
    # The phrasing genuinely does NOT trip _resume_intent, so only the id-gate can fire it:
    assert mod._resume_intent(f"continue session {_UUID_A}") is False
    hint = mod._continuity_prompt_hint(
        prompt_text=f"continue session {_UUID_A}",
        session_id=_UUID_B,          # a different current session
        cwd="/home/u/proj")
    assert "the alpha task" in hint
    assert _CONFIDENT in hint        # explicitly named -> confident reopen warranted


def test_incidental_id_falls_through_to_topic(measure, monkeypatch):
    """P2: resume_intent True + an incidental id matching NO checkpoint must NOT
    suppress the good topic match -- it falls through to topic/recency."""
    mod, cp_dir = measure
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_A, "the widget refactor"), age_minutes=5)
    monkeypatch.setattr(mod, "_resume_topic_score", lambda text, path: 0.9)  # topic match
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block(
        "continue the widget refactor; see session ffffffffffff",
        cps, None, "/home/u/proj")
    assert "the widget refactor" in block   # topic match surfaced, not suppressed by the id
    assert _CONDITIONAL in block            # topic guess -> conditional footer


def test_explicit_id_uses_filename_not_sidecar(measure):
    """P3: when a checkpoint's sidecar session_id DISAGREES with its filename, the
    explicit-id match reconstructs the FILENAME's session (the one the user named),
    not the sidecar's -- else it could confidently reopen a different session."""
    mod, cp_dir = measure
    # filename carries _UUID_A; sidecar lies that it is _UUID_B
    _write_checkpoint(cp_dir, _UUID_A, _sidecar(_UUID_B, "the alpha task"), age_minutes=5)
    cps = mod.list_checkpoints()
    block = mod._continuity_resume_block(
        f"continue session {_UUID_A}", cps, None, "/home/u/proj")
    # Reconstructed via the filename id (_UUID_A) -> finds the file -> renders its task.
    # Had it used the sidecar id (_UUID_B), _best_checkpoint_for_session(_UUID_B) would
    # find no file and return "".
    assert "the alpha task" in block
    assert _CONFIDENT in block
