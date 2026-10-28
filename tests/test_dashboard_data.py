"""Regression tests for the Dashboard 5.13.1 measure.py data hotfix."""

import importlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snapshot"))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    yield mod
    sys.modules.pop("measure", None)


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return str(path)


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}],
        },
    }


def test_sidechain_marker_is_first_prompt_only_and_nested_paths_are_explicit(measure, tmp_path):
    """A later echoed marker is not a delegation, while an agent path is."""
    watcher = _write_jsonl(
        tmp_path / "watcher.jsonl",
        [_user("watch the delegate"), _tool_result("OSRC::PROGRESS#abc running") , _user("OSRC::DONE#abc echoed")],
    )
    parsed = measure._parse_session_jsonl(watcher)
    assert parsed["is_sidechain"] is False
    assert parsed["sidechain_reason"] is None

    nested = _write_jsonl(
        tmp_path / "session-1" / "subagents" / "agent-1.jsonl",
        [_user("worker prompt")],
    )
    parsed = measure._parse_session_jsonl(nested)
    assert parsed["is_sidechain"] is True
    assert parsed["sidechain_reason"] == "nested_path"


def test_runway_denominator_excludes_cache_read_and_cache_write(measure, tmp_path, monkeypatch):
    """The throughput denominator must use the same non-cache basis as saved tokens."""
    db = tmp_path / "runway.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session_log (
            date TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_create_5m_tokens INTEGER, cache_create_1h_tokens INTEGER,
            cache_hit_rate REAL, reported_input_tokens INTEGER,
            reported_output_tokens INTEGER
        );
        CREATE TABLE savings_events (timestamp TEXT, event_type TEXT, tokens_saved INTEGER);
        CREATE TABLE compression_events (
            timestamp TEXT, original_tokens INTEGER, compressed_tokens INTEGER, tier TEXT
        );
        """
    )
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO session_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now().date().isoformat(), 10_000, 100, 100, 0, 0.8, 1_900, 100),
    )
    conn.execute("INSERT INTO savings_events VALUES (?, ?, ?)", (now, "archive", 1_000))
    conn.commit()
    conn.close()

    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(db))
    monkeypatch.setattr(measure, "_keepwarm_read_meters", lambda **_: {"available": False})
    monkeypatch.setattr(measure, "_input_rate_mix_ratio", lambda days=30: 1.0)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **_: {
        "total_cost_usd": 0.0,
        "model_routing": {"realized_cost_usd": 0.0},
    })

    result = measure.runway_snapshot(days=30)
    assert result["tokens_consumed"] == 2_000
    assert result["context_multiplier"] == pytest.approx(1.5)


def test_spent_basis_uses_official_compatible_streaming_usage(measure, tmp_path):
    """Spent tokens exclude cache classes and retain streamed assistant records."""
    db = tmp_path / "spent.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session_log (
            date TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_create_5m_tokens INTEGER, cache_create_1h_tokens INTEGER,
            cache_hit_rate REAL, reported_input_tokens INTEGER,
            reported_output_tokens INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO session_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now().date().isoformat(), 405_000_000, 65_000_000, 334_000_000, 0, 0.999, 5_000_000, 157_200_000),
    )
    conn.commit()

    result = measure._dashboard_spent_token_basis(conn, days=30)
    assert result["tokens"] == 162_200_000
    assert result["basis"] == "fresh_input + streamed assistant output (top-level, no streaming dedup)"
    assert result["complete"] is True



@pytest.mark.parametrize("synthetic", [
    {"isMeta": True, "message": {"content": "Your response was cut off. Resume?"}},
    {"isCompactSummary": True, "message": {"content": "Summary TODO"}},
    {"message": {"content": "<command-name>/compact</command-name>\n<command-args></command-args>"}},
    {"message": {"content": "<task-notification>Finished?"}},
    {"message": {"content": "[SYSTEM NOTIFICATION] TODO"}},
])
def test_session_topic_skips_harness_records(measure, tmp_path, synthetic):
    path = _write_jsonl(tmp_path / "topic.jsonl", [
        {"type": "user", **synthetic}, _user("fix the login bug")])
    assert measure._parse_session_jsonl(path)["topic"] == "fix the login bug"


def test_windowed_topic_skips_metadata_and_outside_human_tasks(measure, tmp_path):
    from datetime import timezone

    records = [
        {**_user("earlier task outside window"), "timestamp": "2026-10-26T23:59:59Z"},
        {**_user("synthetic resume inside window"), "isMeta": True, "timestamp": "2026-10-27T00:00:00Z"},
        {**_user("first real task inside window"), "timestamp": "2026-10-27T00:00:01Z"},
        {**_user("later real task inside window"), "timestamp": "2026-10-27T23:59:59Z"},
        {**_user("task at exclusive window end"), "timestamp": "2026-10-28T00:00:00Z"},
    ]
    path = _write_jsonl(tmp_path / "window-topic.jsonl", records)
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    end = datetime(2026, 10, 28, tzinfo=timezone.utc)
    assert measure._parse_session_jsonl(path, window_start=start, window_end=end)["topic"] == "first real task inside window"
    # Cache keys must not leak the unwindowed first topic into the windowed view.
    assert measure._parse_session_jsonl(path)["topic"] == "earlier task outside window"
    assert measure._parse_session_jsonl(path, window_start=start, window_end=end)["topic"] == "first real task inside window"
