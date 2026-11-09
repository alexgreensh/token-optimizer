#!/usr/bin/env python3
"""Unit tests for the Cursor-native Token Coach data collector (cursor_coach.py).

Slice D / U7 coach gap. The coach's data under Cursor must be REAL: tallies,
compaction snapshots, recorded failures, terminal turn_ended evidence and the
rollup's trends rows - never a doctor/summary bypass, never ~/.claude scans,
never dollar figures (Cursor exposes no local billing data).

Honesty rules pinned here:
- cost surfaces say "no local billing data"; no $0.00-as-free, no pricing.
- token provenance is quoted with every figure (vscdb / estimate / unknown).
- compaction fill is historical pre-compaction occupancy, labeled as such.
- corrupt or missing data skips cleanly; absence is unknown, never healthy.

Run: python3 -m pytest tests/test_cursor_coach.py -v
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_coach  # noqa: E402
import cursor_quality  # noqa: E402


def _tally(conversation_id, **overrides):
    now = time.time()
    base = {
        "conversation_id": conversation_id,
        "first_ts": now - 3600,
        "updated_at": now,
        "final": True,
        "end_reason": "sessionEnd",
        "turns": 20,
        "tool_calls": 15,
        "tool_names": {"Shell": 10, "Read": 5},
        "models": {"claude-sonnet": 20},
        "compactions": [],
        "cwd": "/repo/demo",
        "cursor_version": "2026.11.05-86047df",
    }
    base.update(overrides)
    return base


def _write_tally(home: Path, tally: dict) -> Path:
    sessions = home / "token-optimizer" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{tally['conversation_id']}.json"
    path.write_text(json.dumps(tally), encoding="utf-8")
    return path


@pytest.fixture()
def cursor_home(tmp_path, monkeypatch):
    home = tmp_path / ".cursor"
    home.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(home))
    return home


def test_empty_cursor_home_is_clean(cursor_home):
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    assert data["runtime"] == "cursor"
    assert data["snapshot"]["sessions_analyzed"] == 0
    assert data["health_score"] is None
    assert data["patterns_good"] == []
    assert data["patterns_bad"] == []
    assert data["questions"]
    # Honest cost posture even with zero sessions.
    assert data["snapshot"]["cost"]["status"] == "cursor_no_cost_data"


def test_patterns_from_real_session_evidence(cursor_home):
    _write_tally(cursor_home, _tally(
        "conv-hot",
        turns=120,
        tool_calls=40,
        failures={"Shell": 5},
        compactions=[{
            "trigger": "auto", "context_tokens": 184000, "context_window_size": 200000,
            "context_usage_percent": 92.0, "ts": 1700003000.0,
        }],
    ))
    _write_tally(cursor_home, _tally(
        "conv-churn",
        turns=150,
        compactions=[{
            "trigger": "auto", "context_tokens": 150000, "context_window_size": 200000,
            "context_usage_percent": 75.0, "ts": 1700000000.0 + i * 900,
        } for i in range(4)],
    ))
    _write_tally(cursor_home, _tally("conv-calm", turns=12, tool_calls=25))

    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    names_bad = {p["name"] for p in data["patterns_bad"]}
    assert any("Compaction" in n for n in names_bad)          # hot pre-compaction fill
    assert any("Compaction" in n or "Churn" in n for n in names_bad)
    assert any("Failure" in n for n in names_bad)             # Shell failing 5/40
    assert data["snapshot"]["sessions_analyzed"] == 3
    # Every pattern carries the coach contract fields.
    for p in data["patterns_bad"] + data["patterns_good"]:
        assert p["detail"] and p["fix"]
    # No ~/.claude anywhere; no dollars anywhere.
    blob = json.dumps(data)
    assert ".claude" not in blob
    assert "$" not in blob
    assert data["snapshot"]["cost"]["status"] == "cursor_no_cost_data"


def test_health_score_is_mean_of_real_session_scores(cursor_home):
    tallies = [
        _tally("conv-a", turns=10, tool_calls=5),
        _tally("conv-b", turns=200, tool_calls=150, failures={"Shell": 30}),
    ]
    for t in tallies:
        _write_tally(cursor_home, t)
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    expected = [
        cursor_quality.compute_cursor_quality(t, cursor_home=cursor_home)["score"]
        for t in tallies
    ]
    assert data["health_score"] == round(sum(expected) / len(expected))
    assert data["snapshot"]["quality"]["scored_sessions"] == 2


def test_corrupt_tally_is_skipped_not_fatal(cursor_home):
    _write_tally(cursor_home, _tally("conv-good"))
    broken = cursor_home / "token-optimizer" / "sessions" / "conv-broken.json"
    broken.write_text("{not json", encoding="utf-8")
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    assert data["snapshot"]["sessions_analyzed"] == 1
    assert any("conv-broken" in n for n in data["coverage"]["skipped"])


def test_unknown_token_footprint_is_named(cursor_home):
    # No vscdb, no transcript: the coach must SAY token figures are unknown
    # rather than implying zero usage.
    _write_tally(cursor_home, _tally("conv-mystery"))
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    prov = data["snapshot"]["token_provenance"]
    assert prov.get("cursor_tally_only", 0) == 1
    blob = json.dumps(data)
    assert "unknown" in blob.lower()


def test_compaction_fill_labeled_historical(cursor_home):
    _write_tally(cursor_home, _tally(
        "conv-hist",
        compactions=[{
            "trigger": "auto", "context_tokens": 190000, "context_window_size": 200000,
            "context_usage_percent": 95.0, "ts": 1700003000.0,
        }],
    ))
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    hot = [p for p in data["patterns_bad"] if "Compaction" in p["name"] or "Hot" in p["name"]]
    assert hot
    # The detail must not present the pre-compaction peak as current fill.
    assert "compaction" in hot[0]["detail"].lower()
    assert "current" not in hot[0]["detail"].lower()


def test_history_from_trends_rows(cursor_home):
    _write_tally(cursor_home, _tally("conv-trended"))
    db_dir = cursor_home / "_backups" / "token-optimizer"
    db_dir.mkdir(parents=True)
    db = db_dir / "trends.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE session_log (jsonl_path TEXT, date TEXT, platform TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, quality_score REAL, "
        "quality_grade TEXT, cost_source TEXT)"
    )
    conn.execute(
        "INSERT INTO session_log VALUES ('conv-trended', '2026-11-08', 'cursor', "
        "12000, 3000, 71.0, 'B', 'cursor_no_cost_data')"
    )
    conn.commit()
    conn.close()
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    assert data["history"]["trended_sessions"] == 1
    assert data["history"]["tokens_resolved_sessions"] == 1


def test_questions_and_cursor_terminology(cursor_home):
    _write_tally(cursor_home, _tally("conv-terms"))
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    blob = json.dumps(data)
    # Cursor surfaces only: .cursor/rules or AGENTS.md, never CLAUDE.md.
    assert "CLAUDE.md" not in blob
    assert ".cursor/rules" in blob or "AGENTS.md" in blob


def test_coach_resolves_token_provenance_live(cursor_home, monkeypatch):
    """F11: the coach must resolve state.vscdb/transcript tokens itself — raw
    tallies never carry them, so without resolution every session scored
    cursor_tally_only and output_input_ratio could never activate."""
    import cursor_state as cst

    _write_tally(cursor_home, _tally("conv-tokens"))
    monkeypatch.setattr(
        cst, "read_state_vscdb_tokens",
        lambda ids, **kwargs: {"conv-tokens": {"input_tokens": 8000, "output_tokens": 1200,
                                     "model": "claude-sonnet"}},
    )
    monkeypatch.setattr(cst, "transcript_estimate", lambda p, home: None)
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    prov = data["snapshot"]["token_provenance"]
    assert prov.get("cursor_state_vscdb", 0) == 1
    assert prov.get("cursor_tally_only", 0) == 0


def test_fallback_snapshot_has_all_cli_keys(monkeypatch):
    """F14: when _generate fails, the fallback snapshot must still carry every
    key the human-readable CLI path reads (it crashed with KeyError)."""
    def _boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(cursor_coach, "_generate", _boom)
    data = cursor_coach.generate_cursor_coach_data()
    snap = data["snapshot"]
    for key in ("sessions_analyzed", "window_days", "total_turns",
                "total_tool_calls", "total_compactions", "token_provenance",
                "quality", "cost"):
        assert key in snap, f"fallback snapshot missing {key}"


def test_default_cursor_home_validates_override(tmp_path, monkeypatch):
    """F16: TOKEN_OPTIMIZER_CURSOR_HOME gets the same safe-home validation the
    hook bridge applies — an override outside $HOME is rejected, not read."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    safe = home / ".cursor-alt"
    safe.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(safe))
    assert cursor_coach._default_cursor_home().resolve() == safe.resolve()
    assert cursor_quality._default_cursor_home().resolve() == safe.resolve()

    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(outside))
    expected = (home / ".cursor").resolve()
    assert cursor_coach._default_cursor_home().resolve() == expected
    assert cursor_quality._default_cursor_home().resolve() == expected


def test_skill_detects_default_cursor_install_before_fallback(tmp_path):
    import os
    import subprocess
    from helpers.install_sparse import install_bash

    try:
        bash = install_bash()
    except RuntimeError:
        pytest.skip("Git Bash is unavailable; the WSL launcher cannot run this probe")
    skill = (REPO / "skills" / "token-coach" / "SKILL.md").read_text(encoding="utf-8")
    assert len(skill.splitlines()) < 150
    detection = skill.split('RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"', 1)[1].split(
        '# Resolve measure.py', 1)[0]
    (tmp_path / ".cursor" / "token-optimizer").mkdir(parents=True)
    (tmp_path / ".codex").mkdir()
    env = {key: os.environ[key] for key in
           ("PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE")
           if key in os.environ}
    env["HOME"] = tmp_path.as_posix()
    result = subprocess.run([bash, "-c", 'RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"' + detection + '\nprintf "%s" "$RUNTIME"'],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "cursor"


def test_coach_uses_selected_home_database(cursor_home, tmp_path, monkeypatch):
    import cursor_state as cst
    _write_tally(cursor_home, _tally('conv-shared'))
    def write_db(path, count):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute('CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('INSERT INTO cursorDiskKV VALUES (?, ?)', ('bubbleId:conv-shared:1', json.dumps({'tokenCount': {'inputTokens': count, 'outputTokens': 2}})))
        conn.commit(); conn.close()
    write_db(tmp_path / 'global.vscdb', 999999)
    write_db(cursor_home / 'User/globalStorage/state.vscdb', 321)
    monkeypatch.setattr(cst, 'state_vscdb_path', lambda: tmp_path / 'global.vscdb')
    actual = cst.read_state_vscdb_tokens
    observed = []
    def read(ids, **kwargs):
        result = actual(ids, **kwargs)
        observed.append(result)
        return result
    monkeypatch.setattr(cst, 'read_state_vscdb_tokens', read)
    data = cursor_coach.generate_cursor_coach_data(cursor_home=cursor_home)
    assert data['snapshot']['token_provenance'].get('cursor_state_vscdb') == 1
    assert observed[0]['conv-shared']['input_tokens'] == 321
