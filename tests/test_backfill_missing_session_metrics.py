"""Upkeep must not erase session metrics when source evidence disappears."""
import builtins
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture
def subject(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude" / "projects").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("measure", "runtime_env", "plugin_env"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    measure = importlib.import_module("measure")
    # Isolate session-metric upkeep from unrelated dedupe and daily backfills.
    monkeypatch.setattr(measure, "_dedupe_session_rows", lambda conn: 0)
    monkeypatch.setattr(measure, "_backfill_daily_usage", lambda *a, **kw: None)
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE session_log (
        jsonl_path TEXT PRIMARY KEY, date TEXT, collected_at TEXT,
        cache_create_1h_tokens INTEGER, cache_create_5m_tokens INTEGER,
        cache_ttl_scanned INTEGER, avg_call_gap_seconds REAL,
        max_call_gap_seconds REAL, p95_call_gap_seconds REAL,
        quality_score REAL, quality_grade TEXT, retained TEXT)""")
    yield measure, conn
    conn.close()


def insert(conn, path, scanned=1):
    conn.execute("INSERT INTO session_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (str(path), datetime.now().strftime("%Y-%m-%d"), "now", 1000, 2000,
                  scanned, 10, 20, 15, None, "retained-grade", "keep"))
    conn.commit()


def row(conn):
    return conn.execute("SELECT * FROM session_log").fetchone()


def test_missing_transcript_or_selector_preserves_entire_row_across_repeats(subject, tmp_path):
    measure, conn = subject
    insert(conn, tmp_path / "missing.jsonl")
    before = row(conn)
    for _ in range(2):
        measure._read_path_upkeep(conn, 30)
        assert row(conn) == before


@pytest.mark.parametrize("kind", ["unscanned_missing", "malformed", "unreadable"])
def test_unavailable_or_unusable_source_preserves_entire_row(subject, tmp_path, monkeypatch, kind):
    measure, conn = subject
    path = tmp_path / "session.jsonl"
    if kind != "unscanned_missing":
        path.write_text("not json\n", encoding="utf8")
    if kind == "unreadable":
        real_open = builtins.open
        def open_with_unreadable_source(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError("simulated unreadable transcript")
            return real_open(file, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", open_with_unreadable_source)
    insert(conn, path, scanned=0)
    before = row(conn)
    for _ in range(2):
        measure._read_path_upkeep(conn, 30)
        assert row(conn) == before


@pytest.mark.parametrize("one_hour,five_min", [(1000, 2000), (0, 0)])
def test_readable_source_backfills_metrics_including_genuine_zero(subject, tmp_path, one_hour, five_min):
    measure, conn = subject
    path = tmp_path / "session.jsonl"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    records = [{"type": "user", "timestamp": now.isoformat(), "message": {"content": "hi"}}]
    for index in range(3):
        records.append({"type": "assistant", "timestamp": (now + timedelta(seconds=index * 10)).isoformat(),
            "requestId": str(index), "message": {"id": str(index), "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_creation_input_tokens": one_hour + five_min,
                      "cache_creation": {"ephemeral_1h_input_tokens": one_hour,
                                         "ephemeral_5m_input_tokens": five_min}}}})
    path.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf8")
    insert(conn, path, scanned=0)
    measure._read_path_upkeep(conn, 30)
    first = row(conn)
    assert first[3:6] == (3 * one_hour, 3 * five_min, 1)
    assert first[6:9] == (10, 10, 10)
    assert first[9] == measure.score_session_quality(measure._parse_session_jsonl(str(path)))["score"]
    assert not conn.in_transaction
    assert first[10:] == ("retained-grade", "keep")
    measure._read_path_upkeep(conn, 30)
    assert row(conn) == first
