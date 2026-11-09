"""Fleet Auditor + Token Coach: Cursor runtime collection (R10/R11).

The Cursor adapter reads Token Optimizer's OWN Cursor data planes, read-only:
- hook tallies under <cursor_home>/token-optimizer/sessions/*.json (the
  authoritative record of turns, tool calls, models, compactions), and
- the rollup's trends.db session_log rows (platform='cursor') for token
  figures the rollup already resolved (state.vscdb or transcript estimate).

Honesty rules pinned here:
- cost is always 0.0 and reported as "no local billing data", never $0.00-free
  and never routed through pricing tables (Cursor exposes none);
- sessions with no trends row keep token_source='cursor_tally_only' and the
  scan says their token figures are unknown -- a silent zero is a defect;
- the collector writes nothing into the Cursor home or the trends DB.

Run: python3 -m pytest tests/test_fleet_cursor.py -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
FLEET_SCRIPTS = REPO / "skills" / "fleet-auditor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(FLEET_SCRIPTS))

import fleet  # noqa: E402

# Full session_log column set from _measure/sessions.py::_insert_normalized_session
# (33 columns). The fixture mirrors the real rollup output, not a subset.
_SESSION_LOG_COLUMNS = (
    "jsonl_path, date, project, duration_minutes, input_tokens, output_tokens, "
    "message_count, api_calls, cache_hit_rate, cache_create_1h_tokens, "
    "cache_create_5m_tokens, cache_ttl_scanned, avg_call_gap_seconds, "
    "max_call_gap_seconds, p95_call_gap_seconds, skills_json, subagents_json, "
    "tool_calls_json, model_usage_json, all_model_usage_json, "
    "model_usage_breakdown_json, version, slug, topic, collected_at, "
    "quality_score, quality_grade, stale_waste_tokens, cost_usd, cost_source, "
    "credits, platform, incomplete"
)


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    fleet._pricing_override = None
    yield
    fleet._pricing_override = None


@pytest.fixture()
def cursor_home(tmp_path, monkeypatch):
    """Point fleet.HOME at a tmp dir and create the Cursor home inside it."""
    monkeypatch.setattr(fleet, "HOME", tmp_path)
    monkeypatch.delenv("TOKEN_OPTIMIZER_CURSOR_HOME", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", raising=False)
    home = tmp_path / ".cursor"
    home.mkdir()
    return home


def _write_tally(home: Path, cid: str, **over) -> Path:
    now = datetime.now(timezone.utc).timestamp()
    tally = {
        "conversation_id": cid,
        "first_ts": now - 1800,
        "updated_at": now - 60,
        "turns": 14,
        "tool_calls": 22,
        "models": {"gpt-5.1": 9, "claude-sonnet-4-6": 5},
        "tool_names": {"Shell": 15, "Read": 7},
        "compactions": [],
        "cursor_version": "2026.11.05-abc1234",
        "cwd": "/work/project-x",
        "final": True,
        "end_reason": "sessionEnd",
    }
    tally.update(over)
    sessions = home / "token-optimizer" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{cid}.json"
    path.write_text(json.dumps(tally), encoding="utf-8")
    return path


def _write_trends_db(home: Path, rows: list[dict]) -> Path:
    """Create a rollup-shaped trends.db with platform='cursor' rows."""
    db_path = home / "_backups" / "token-optimizer" / "trends.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session_log ({_SESSION_LOG_COLUMNS})")
    for row in rows:
        base = {
            "jsonl_path": "cursor:conv-1",
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "project": "/work/project-x",
            "duration_minutes": 29.0,
            "input_tokens": 120_000,
            "output_tokens": 8_500,
            "message_count": 14,
            "api_calls": 14,
            "cache_hit_rate": 0.0,
            "cache_create_1h_tokens": 0,
            "cache_create_5m_tokens": 0,
            "cache_ttl_scanned": 0,
            "avg_call_gap_seconds": None,
            "max_call_gap_seconds": None,
            "p95_call_gap_seconds": None,
            "skills_json": "{}",
            "subagents_json": "{}",
            "tool_calls_json": '{"total": 22}',
            "model_usage_json": '{"gpt-5.1": 9}',
            "all_model_usage_json": '{"gpt-5.1": 9}',
            "model_usage_breakdown_json": "{}",
            "version": "2026.11.05-abc1234",
            "slug": "conv-1",
            "topic": None,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "quality_score": 72.0,
            "quality_grade": "C",
            "stale_waste_tokens": 0,
            "cost_usd": 0.0,
            "cost_source": "cursor_no_cost_data",
            "credits": None,
            "platform": "cursor",
            "incomplete": 0,
        }
        base.update(row)
        cols = ", ".join(base.keys())
        ph = ", ".join("?" for _ in base)
        conn.execute(f"INSERT INTO session_log ({cols}) VALUES ({ph})", tuple(base.values()))
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Registration and detection
# ---------------------------------------------------------------------------

def test_cursor_adapter_registered():
    names = [cls.name for cls in fleet.ADAPTER_REGISTRY]
    assert "cursor" in names


def test_require_known_system_accepts_cursor():
    fleet._require_known_system("cursor")  # must not exit


def test_detect_without_cursor_home(cursor_home):
    cursor_home.rmdir()
    found, confidence, _ = fleet.CursorAdapter().detect()
    assert found is False
    assert confidence == 0.0


def test_detect_cursor_home_without_tallies(cursor_home):
    found, confidence, detail = fleet.CursorAdapter().detect()
    assert found is True
    assert 0.0 < confidence < 1.0
    assert "cursor-install" in detail or "tallies" in detail.lower()


def test_detect_with_tallies(cursor_home):
    _write_tally(cursor_home, "conv-1")
    found, confidence, _ = fleet.CursorAdapter().detect()
    assert found is True
    assert confidence == 1.0


def test_cursor_home_env_override(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    _write_tally(other, "conv-9")
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(other))
    monkeypatch.setattr(fleet, "HOME", tmp_path)  # .cursor here stays empty
    found, confidence, _ = fleet.CursorAdapter().detect()
    assert found is True
    assert confidence == 1.0


def test_cursor_home_env_override_outside_home_rejected(tmp_path, monkeypatch):
    """F16: an override outside fleet's HOME root must be rejected like
    runtime_env._safe_home_from_env does, not blindly trusted."""
    outside = tmp_path / "outside"
    _write_tally(outside, "conv-x")
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(outside))
    fleet_home = tmp_path / "fleet-home"
    fleet_home.mkdir()
    monkeypatch.setattr(fleet, "HOME", fleet_home)
    # Default <fleet_home>/.cursor does not exist -> detect reports not found
    # instead of silently scanning the foreign override.
    found, confidence, _ = fleet.CursorAdapter().detect()
    assert found is False
    assert confidence == 0.0


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def test_scan_collects_tally_with_trends_tokens(cursor_home):
    _write_tally(cursor_home, "conv-1")
    _write_trends_db(cursor_home, [{"jsonl_path": "cursor:conv-1"}])
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    assert errors == []
    assert len(runs) == 1
    run = runs[0]
    assert run.system == "cursor"
    assert run.session_id == "conv-1"
    assert run.tokens.input == 120_000
    assert run.tokens.output == 8_500
    assert run.tokens.cache_read == 0  # Cursor hooks report no cache breakdown
    assert run.cost_usd == 0.0
    assert run.token_source == "cursor_rollup"
    assert run.model != ""
    assert run.project == "/work/project-x"
    assert run.message_count == 14
    assert run.outcome == "success"
    assert run.duration_seconds > 0


def test_scan_tally_without_trends_row_is_honest_unknown(cursor_home):
    _write_tally(cursor_home, "conv-1")
    _write_trends_db(cursor_home, [{"jsonl_path": "cursor:conv-other"}])
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    assert len(runs) == 1
    run = runs[0]
    assert run.tokens.total == 0
    assert run.token_source == "cursor_tally_only"
    # The absence must be SAID, never a silent zero (#149 class).
    assert any("unknown" in e.lower() and "cursor-rollup" in e for e in errors)


def test_scan_without_trends_db_still_collects_tallies(cursor_home):
    _write_tally(cursor_home, "conv-1")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    assert len(runs) == 1
    assert runs[0].tokens.total == 0
    assert runs[0].token_source == "cursor_tally_only"
    assert any("trends.db" in e for e in errors)


def test_scan_corrupt_tally_skipped_not_fatal(cursor_home):
    _write_tally(cursor_home, "conv-good")
    bad = cursor_home / "token-optimizer" / "sessions" / "conv-bad.json"
    bad.write_text("{not json", encoding="utf-8")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    assert [r.session_id for r in runs] == ["conv-good"]
    assert any("conv-bad" in e for e in errors)


def test_scan_window_uses_last_activity(cursor_home):
    now = datetime.now(timezone.utc).timestamp()
    _write_tally(cursor_home, "conv-old", first_ts=now - 40 * 86400, updated_at=now - 35 * 86400)
    _write_tally(cursor_home, "conv-longlived", first_ts=now - 40 * 86400, updated_at=now - 3600)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.CursorAdapter().scan(since)
    # A long-lived IDE chat active inside the window is collected; one whose
    # last activity predates the window is not.
    assert [r.session_id for r in runs] == ["conv-longlived"]


def test_scan_dedup_skips_already_collected(cursor_home):
    _write_tally(cursor_home, "conv-1")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE fleet_runs (id INTEGER PRIMARY KEY, source_path TEXT, "
        "system TEXT, session_id TEXT, UNIQUE(system, source_path, session_id))"
    )
    adapter = fleet.CursorAdapter()
    runs_first, _ = adapter.scan(since, conn)
    assert len(runs_first) == 1
    for r in runs_first:
        conn.execute(
            "INSERT INTO fleet_runs (source_path, system, session_id) VALUES (?, ?, ?)",
            (r.source_path, r.system, r.session_id),
        )
    conn.commit()
    runs_second, _ = adapter.scan(since, conn)
    assert runs_second == []
    conn.close()


def test_scan_missing_fields_stay_unknown(cursor_home):
    _write_tally(
        cursor_home,
        "conv-sparse",
        models={},  # type: ignore[arg-type]
        cursor_version=None,
        cwd=None,
        workspace_roots=None,
    )
    # Remove the keys entirely: a partial tally must not crash the collector.
    p = cursor_home / "token-optimizer" / "sessions" / "conv-sparse.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for key in ("models", "cursor_version", "cwd", "turns", "tool_calls"):
        data.pop(key, None)
    p.write_text(json.dumps(data), encoding="utf-8")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, _ = fleet.CursorAdapter().scan(since)
    assert len(runs) == 1
    run = runs[0]
    assert run.model == "unknown"
    assert run.project == "unknown"
    assert run.message_count == 0


def test_scan_tally_without_conversation_id_skipped(cursor_home):
    _write_tally(cursor_home, "conv-1")
    p = cursor_home / "token-optimizer" / "sessions" / "conv-1.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("conversation_id")
    p.write_text(json.dumps(data), encoding="utf-8")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    assert runs == []
    assert any("conv-1" in e for e in errors)


def test_scan_empty_outcome_when_no_activity(cursor_home):
    _write_tally(cursor_home, "conv-empty", turns=0, tool_calls=0)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, _ = fleet.CursorAdapter().scan(since)
    assert runs[0].outcome == "empty"


def test_scan_out_of_range_timestamp_skipped(cursor_home):
    _write_tally(cursor_home, "conv-huge", first_ts=1e18, updated_at=1e18 + 60)
    _write_tally(cursor_home, "conv-ok")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    # conv-huge is inside the window (last activity is "now" per its crafted
    # floats) but must not crash the scan; conv-ok still collects.
    assert [r.session_id for r in runs] == ["conv-ok"]
    assert any("conv-huge" in e for e in errors)


def test_scan_junk_trends_values_degrade_to_tally_only(cursor_home):
    _write_tally(cursor_home, "conv-1")
    db_path = cursor_home / "_backups" / "token-optimizer" / "trends.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE session_log ({_SESSION_LOG_COLUMNS})")
    conn.execute(
        "INSERT INTO session_log (jsonl_path, input_tokens, output_tokens, platform) "
        "VALUES ('cursor:conv-1', 'not-a-number', NULL, 'cursor')"
    )
    conn.commit()
    conn.close()
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs, errors = fleet.CursorAdapter().scan(since)
    assert len(runs) == 1
    assert runs[0].tokens.total == 0
    assert runs[0].token_source == "cursor_tally_only"
    assert any("unknown" in e.lower() for e in errors)


def test_report_cursor_only_total_is_no_data(cursor_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "FLEET_DB", tmp_path / "fleet.db")
    _write_tally(cursor_home, "conv-1")
    fleet.cmd_scan(["--system", "cursor", "--days", "7", "--quiet"])
    fleet.cmd_report(["--system", "cursor"])
    out = capsys.readouterr().out
    assert "Total: 1 runs, no local billing data" in out
    assert "Total: 1 runs, $0.00" not in out


# ---------------------------------------------------------------------------
# Read-only posture
# ---------------------------------------------------------------------------

def test_scan_writes_nothing_to_cursor_home_or_trends_db(cursor_home):
    _write_tally(cursor_home, "conv-1")
    db_path = _write_trends_db(cursor_home, [{"jsonl_path": "cursor:conv-1"}])
    before_db = db_path.read_bytes()
    before_tree = sorted(p.relative_to(cursor_home) for p in cursor_home.rglob("*"))
    since = datetime.now(timezone.utc) - timedelta(days=7)
    fleet.CursorAdapter().scan(since)
    after_tree = sorted(p.relative_to(cursor_home) for p in cursor_home.rglob("*"))
    assert before_tree == after_tree
    assert db_path.read_bytes() == before_db


# ---------------------------------------------------------------------------
# Cost honesty
# ---------------------------------------------------------------------------

def test_unpriced_summary_excludes_cursor():
    runs = [
        fleet.AgentRun(
            system="cursor",
            session_id="conv-1",
            model="composer-1",
            tokens=fleet.TokenBreakdown(input=100_000, output=5_000),
            cost_usd=0.0,
        ),
        fleet.AgentRun(
            system="hermes",
            session_id="sess-1",
            model="minimax-m2",
            tokens=fleet.TokenBreakdown(input=100_000, output=5_000),
            cost_usd=0.0,
        ),
    ]
    summary = fleet.unpriced_summary(runs)
    # Cursor's $0 is structural (no local billing data exists), not a missing
    # pricing row: telling Cursor users to "add rates" would fabricate cost.
    assert "composer-1" not in summary
    assert summary.get("minimax-m2") == 1


def test_report_cursor_cost_is_no_data_not_zero(cursor_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "FLEET_DB", tmp_path / "fleet.db")
    _write_tally(cursor_home, "conv-1")
    _write_trends_db(cursor_home, [{"jsonl_path": "cursor:conv-1"}])
    fleet.cmd_scan(["--system", "cursor", "--days", "7", "--quiet"])
    fleet.cmd_report(["--system", "cursor"])
    out = capsys.readouterr().out
    assert "Cursor" in out
    assert "no local billing data" in out.lower() or "no cost data" in out.lower()
    cursor_section = out.split("Cursor", 1)[1]
    assert "$0.00" not in cursor_section.split("\n\n")[0]


def test_report_json_marks_cursor_cost_note(cursor_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "FLEET_DB", tmp_path / "fleet.db")
    _write_tally(cursor_home, "conv-1")
    _write_trends_db(cursor_home, [{"jsonl_path": "cursor:conv-1"}])
    fleet.cmd_scan(["--system", "cursor", "--days", "7", "--quiet"])
    fleet.cmd_report(["--system", "cursor", "--json"])
    report = json.loads(capsys.readouterr().out)
    cursor_row = next(s for s in report["systems"] if s["system"] == "cursor")
    assert cursor_row["total_cost"] == 0
    assert cursor_row.get("cost_note")


# ---------------------------------------------------------------------------
# fleet.db migration for the provenance column
# ---------------------------------------------------------------------------

def test_fleet_db_migration_adds_token_source(tmp_path, monkeypatch):
    db_path = tmp_path / "fleet.db"
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "FLEET_DB", db_path)
    # Pre-migration database: fleet_runs WITHOUT token_source.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE fleet_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_path TEXT NOT NULL, system TEXT NOT NULL, session_id TEXT, "
        "date TEXT NOT NULL, agent_name TEXT, project TEXT, duration_seconds REAL, "
        "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
        "cache_write_tokens INTEGER, cost_usd REAL, model TEXT, "
        "context_window_size INTEGER, run_type TEXT, outcome TEXT, "
        "message_count INTEGER, error_message TEXT, exit_code INTEGER, "
        "collected_at TEXT, UNIQUE(system, source_path, session_id))"
    )
    conn.commit()
    conn.close()
    conn = fleet._init_fleet_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fleet_runs)").fetchall()}
    conn.close()
    assert "token_source" in cols


# ---------------------------------------------------------------------------
# Skill docs (R11 coach surface + fleet reference)
# ---------------------------------------------------------------------------

def test_coach_skill_gathers_cursor_data_not_claude():
    text = (REPO / "skills" / "token-coach" / "SKILL.md").read_text(encoding="utf-8")
    assert "cursor-doctor" in text
    assert "cursor-summary" in text
    # The Claude/Codex-only gather commands must be gated so a Cursor session
    # never scans ~/.claude or prints the wrong-runtime notice.
    assert '"$RUNTIME" = "cursor"' in text
    assert '"$RUNTIME" = "claude" ] || [ "$RUNTIME" = "codex"' in text


def test_coach_skill_teaches_cursor_honesty_rules():
    text = (REPO / "skills" / "token-coach" / "SKILL.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "no local billing" in lowered or "no per-session cost" in lowered or "no dollar" in lowered


def test_fleet_systems_md_documents_cursor():
    text = (REPO / "skills" / "fleet-auditor" / "references" / "fleet-systems.md").read_text(encoding="utf-8")
    assert "## Cursor" in text
    assert "token-optimizer/sessions" in text
    assert "cursor_no_cost_data" in text
