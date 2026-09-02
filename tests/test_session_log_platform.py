"""session_log.platform must be populated for every ingested session.

The platform column was added by a migration but only one of the three
INSERT paths (Copilot) referenced it, and even that one wrote the wrong
value (token_source instead of the platform name). The main Claude/Codex
collector and the Hermes collector omitted the column entirely, so every
row landed with platform = NULL.

These tests exercise the real collection path end-to-end (Claude JSONL)
and source-inspect the Hermes and Copilot INSERTs so all three paths are
covered.
"""

import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


def _make_minimal_claude_jsonl(path: Path):
    """Write a minimal valid Claude Code JSONL transcript (1 user + 1 assistant turn)."""
    records = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "hello world"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:05Z",
            "requestId": "req-1",
            "message": {
                "model": "claude-sonnet-4-20250514",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_claude_collect_records_platform(tmp_path, monkeypatch):
    """End-to-end: collect_sessions on a Claude JSONL must set platform='claude'."""
    # Isolate the DB
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    # Force runtime to claude
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    # Point CLAUDE_CONFIG_DIR at a fake home so _find_all_jsonl_files scans it
    fake_claude = tmp_path / "claude_home"
    projects = fake_claude / "projects" / "test-project"
    projects.mkdir(parents=True)
    jsonl = projects / "test-session.jsonl"
    _make_minimal_claude_jsonl(jsonl)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_claude))

    # Clear cached modules so env vars take effect on import
    for mod_name in list(sys.modules):
        if mod_name in ("measure", "runtime_env", "plugin_env"):
            sys.modules.pop(mod_name, None)
    sys.path.insert(0, str(SCRIPTS))
    measure = importlib.import_module("measure")

    # Run collection
    measure.collect_sessions(days=90, quiet=True)

    # Query the DB
    db = measure.TRENDS_DB
    assert db.exists(), "trends.db was not created"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT platform, jsonl_path FROM session_log"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected 1 session, got {len(rows)}"
    platform = rows[0][0]
    assert platform == "claude", (
        f"platform should be 'claude' for a Claude JSONL session, got {platform!r}"
    )


def test_hermes_insert_includes_platform():
    """Source inspection: the Hermes INSERT must include the platform column."""
    src = Path(SCRIPTS, "measure.py").read_text(encoding="utf-8")
    insert_start = src.index("def _collect_hermes_sessions")
    insert_end = src.index("\ndef ", insert_start + 1)
    hermes_section = src[insert_start:insert_end]

    # The INSERT must list platform as a column
    assert "platform" in hermes_section, (
        "Hermes collector INSERT must include the platform column"
    )
    # And must write a hermes literal, not a token_source or other field
    assert '"hermes"' in hermes_section or "'hermes'" in hermes_section, (
        "Hermes collector must write platform='hermes'"
    )


def test_copilot_insert_writes_platform_not_token_source():
    """Source inspection: the Copilot INSERT must write the platform name, not token_source."""
    src = Path(SCRIPTS, "measure.py").read_text(encoding="utf-8")
    insert_start = src.index("def _collect_copilot_sessions")
    insert_end = src.index("\ndef ", insert_start + 1)
    copilot_section = src[insert_start:insert_end]

    # Must NOT write token_source into the platform column
    assert "token_source" not in copilot_section, (
        "Copilot collector must not write token_source into the platform column; "
        "token_source is a sub-source label (e.g. 'copilot_cli_events'), not a platform name"
    )
    # Must write the copilot platform name
    assert '"copilot"' in copilot_section or "'copilot'" in copilot_section, (
        "Copilot collector must write platform='copilot'"
    )


def test_cursor_collector_writes_platform_and_cursor_dedup_key():
    """Source inspection: the Cursor path must write the platform column and a
    cursor: dedup-key prefix, never a token_source into the platform column."""
    src = Path(SCRIPTS, "measure.py").read_text(encoding="utf-8")

    # _insert_normalized_session owns the shared INSERT and must list platform.
    insert_start = src.index("def _insert_normalized_session")
    insert_end = src.index("\ndef ", insert_start + 1)
    insert_section = src[insert_start:insert_end]
    assert "platform" in insert_section, (
        "shared normalized INSERT must include the platform column"
    )

    # _collect_cursor_sessions must build `cursor:<conversation_id>` dedup keys
    # and pass the cursor platform literal into that INSERT helper.
    coll_start = src.index("def _collect_cursor_sessions")
    coll_end = src.index("\ndef ", coll_start + 1)
    cursor_section = src[coll_start:coll_end]
    assert '"cursor"' in cursor_section or "'cursor'" in cursor_section, (
        "Cursor collector must pass platform='cursor' to the normalized INSERT"
    )
    assert "cursor:" in cursor_section, (
        "Cursor collector must prefix dedup keys with 'cursor:' so backfill "
        "can infer the platform from jsonl_path"
    )
    assert "token_source" not in cursor_section, (
        "Cursor collector must not write token_source into the platform column"
    )


def test_grok_insert_writes_platform_not_token_source():
    """Source inspection: the Grok INSERT must write the platform name, not token_source."""
    src = Path(SCRIPTS, "measure.py").read_text(encoding="utf-8")
    insert_start = src.index("def _collect_grok_sessions")
    insert_end = src.index("\ndef ", insert_start + 1)
    grok_section = src[insert_start:insert_end]

    assert "token_source" not in grok_section, (
        "Grok collector must not write token_source into the platform column"
    )
    assert '"grok"' in grok_section or "'grok'" in grok_section, (
        "Grok collector must write platform='grok'"
    )


def test_main_claude_insert_includes_platform():
    """Source inspection: the main Claude/Codex INSERT must include the platform column."""
    src = Path(SCRIPTS, "measure.py").read_text(encoding="utf-8")
    # The main collector INSERT is inside collect_sessions, after the hermes/copilot dispatch
    collect_start = src.index("def collect_sessions(")
    collect_end = src.index("\ndef ", collect_start + 1)
    collect_section = src[collect_start:collect_end]

    # Find the INSERT OR IGNORE INTO session_log in the main collector
    assert "INSERT OR IGNORE INTO session_log" in collect_section, (
        "main collector must have an INSERT into session_log"
    )
    # The INSERT must include platform as a column
    assert "platform" in collect_section, (
        "main collector INSERT must include the platform column"
    )


def test_backfill_infers_platform_from_jsonl_path(tmp_path, monkeypatch):
    """_init_trends_db backfills platform from jsonl_path for pre-fix rows.

    Existing rows with platform IS NULL and a Claude projects path get
    'claude'; Codex paths get 'codex'; Hermes/Copilot dedup keys get their
    platform. Rows with ambiguous paths stay NULL. Idempotent.
    """
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    fake_claude = tmp_path / "claude_home"
    fake_claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_claude))

    for mod_name in list(sys.modules):
        if mod_name in ("measure", "runtime_env", "plugin_env"):
            sys.modules.pop(mod_name, None)
    sys.path.insert(0, str(SCRIPTS))
    measure = importlib.import_module("measure")

    # Manually create a DB with pre-fix rows (platform IS NULL)
    db = measure.TRENDS_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(measure._TRENDS_SCHEMA if hasattr(measure, "_TRENDS_SCHEMA") else """
        CREATE TABLE IF NOT EXISTS session_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jsonl_path TEXT UNIQUE,
            date TEXT, project TEXT, duration_minutes REAL,
            input_tokens INTEGER, output_tokens INTEGER,
            message_count INTEGER, api_calls INTEGER,
            cache_hit_rate REAL,
            cache_create_1h_tokens INTEGER DEFAULT 0,
            cache_create_5m_tokens INTEGER DEFAULT 0,
            cache_ttl_scanned INTEGER DEFAULT 0,
            avg_call_gap_seconds REAL, max_call_gap_seconds REAL, p95_call_gap_seconds REAL,
            skills_json TEXT, subagents_json TEXT, tool_calls_json TEXT,
            model_usage_json TEXT, all_model_usage_json TEXT, model_usage_breakdown_json TEXT,
            version TEXT, slug TEXT, topic TEXT, collected_at TEXT,
            quality_score INTEGER DEFAULT 0, quality_grade TEXT DEFAULT 'F',
            stale_waste_tokens INTEGER DEFAULT 0,
            session_uuid TEXT, cost_usd REAL, cost_source TEXT, credits REAL,
            platform TEXT, incomplete INTEGER DEFAULT 0, is_sidechain INTEGER
        );
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY, sessions INTEGER, total_tokens INTEGER,
            avg_tokens REAL, avg_duration REAL, avg_cache_hit_rate REAL
        );
        CREATE TABLE IF NOT EXISTS model_daily (
            date TEXT, model TEXT, tokens INTEGER,
            PRIMARY KEY (date, model)
        );
        CREATE TABLE IF NOT EXISTS skill_daily (
            date TEXT, skill TEXT, uses INTEGER,
            PRIMARY KEY (date, skill)
        );
        CREATE TABLE IF NOT EXISTS subagent_daily (
            date TEXT, subagent TEXT, uses INTEGER,
            PRIMARY KEY (date, subagent)
        );
    """)
    # Insert rows with NULL platform simulating pre-fix state
    test_rows = [
        ("/home/user/.claude/projects/myproj/abc.jsonl", None),
        ("/home/user/.claude/projects/other/def.jsonl", None),
        ("/home/user/.codex/sessions/rollout-123.jsonl", None),
        ("/home/user/.codex/archived_sessions/rollout-456.jsonl", None),
        ("hermes:my-slug", None),
        ("copilot:vscode-abcd-1234", None),
        ("cursor:composer-abc-1234", None),
        ("/some/ambiguous/path.jsonl", None),
    ]
    for jpath, _ in test_rows:
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, platform) VALUES (?, '2026-01-01', ?)",
            (jpath, None),
        )
    conn.commit()
    conn.close()

    # Run _init_trends_db which triggers the backfill
    measure._init_trends_db()

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT jsonl_path, platform FROM session_log ORDER BY jsonl_path"
    ).fetchall()
    conn.close()

    results = {row[0]: row[1] for row in rows}
    assert results["/home/user/.claude/projects/myproj/abc.jsonl"] == "claude"
    assert results["/home/user/.claude/projects/other/def.jsonl"] == "claude"
    assert results["/home/user/.codex/sessions/rollout-123.jsonl"] == "codex"
    assert results["/home/user/.codex/archived_sessions/rollout-456.jsonl"] == "codex"
    assert results["hermes:my-slug"] == "hermes"
    assert results["copilot:vscode-abcd-1234"] == "copilot"
    assert results["cursor:composer-abc-1234"] == "cursor"
    # Ambiguous path stays NULL
    assert results["/some/ambiguous/path.jsonl"] is None
