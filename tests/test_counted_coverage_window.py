"""Regression tests for counted-to-date coverage and window accounting."""

import importlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_counted_cumulative import _verified_pricing


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snapshot"))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    mod._apply_sonnet_intro_pricing()
    yield mod
    sys.modules.pop("measure", None)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_iso(dt_utc):
    return dt_utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()


def _transcript(path, start, turns=3, model="claude-opus-5"):
    records = []
    for i in range(turns):
        records.append({
            "type": "assistant",
            "timestamp": (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "requestId": f"req-{i}",
            "message": {
                "model": model,
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _schema_db(measure, path):
    conn = sqlite3.connect(path)
    conn.executescript(measure._SCHEMA)
    conn.commit()
    return conn


def test_dashboard_counted_payload_reconciles_to_full_table(measure, tmp_path, monkeypatch):
    """The dashboard payload must replace stale counted data with the table sum."""
    db = tmp_path / "trends.db"
    conn = _schema_db(measure, db)
    conn.execute(
        "INSERT INTO counted_reread(event_key, source, event_month, oneshot_usd, reread_usd, pricing_detail) "
        "VALUES ('e:1', 'se', '2026-08', 111.21, 870.25, ?)",
        (_verified_pricing(measure),)
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(db))
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **_: {
        "total_tokens": 0, "total_cost_usd": 0.0,
        "counted_cumulative": {"total_usd": 352.0},
    })
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {})

    result = measure._dashboard_savings_data(days=30)
    counted = result["counted_cumulative"]
    assert counted["total_usd"] == pytest.approx(981.46)
    assert counted["total_usd"] == pytest.approx(counted["oneshot_usd"] + counted["reread_usd"])
    assert "removals" in counted["method"]


def test_counted_backfill_walks_all_real_transcripts_and_skips_missing(measure, tmp_path):
    """The uncapped backfill covers every event session with a transcript only."""
    db = tmp_path / "trends.db"
    conn = _schema_db(measure, db)
    measure.CLAUDE_DIR = tmp_path
    now = _utc_now()
    for i in range(9):
        sid = f"real-{i}"
        path = tmp_path / "projects" / f"{sid}.jsonl"
        _transcript(path, now - timedelta(minutes=40 + i))
        conn.execute(
            "INSERT INTO session_log(session_uuid, jsonl_path, date) VALUES (?, ?, ?)",
            (sid, str(path), now.date().isoformat()),
        )
        conn.execute(
            "INSERT INTO savings_events(event_type, timestamp, tokens_saved, cost_saved_usd, "
            "session_uuid, session_id, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("tool_archive", _local_iso(now - timedelta(minutes=39 + i)), 100_000,
             0.3, sid, sid, "opus"),
        )
    missing = "missing-transcript"
    conn.execute(
        "INSERT INTO session_log(session_uuid, jsonl_path, date) VALUES (?, ?, ?)",
        (missing, str(tmp_path / "does-not-exist.jsonl"), now.date().isoformat()),
    )
    conn.execute(
        "INSERT INTO savings_events(event_type, timestamp, tokens_saved, cost_saved_usd, "
        "session_uuid, session_id, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("tool_archive", _local_iso(now - timedelta(minutes=20)), 100_000, 0.3,
         missing, missing, "opus"),
    )
    conn.commit()

    result = measure._counted_backfill_all(conn, quiet=True)
    covered = conn.execute(
        "SELECT COUNT(DISTINCT session_uuid) FROM counted_reread WHERE session_uuid IS NOT NULL"
    ).fetchone()[0]
    missing_rows = conn.execute(
        "SELECT COUNT(*) FROM counted_reread WHERE session_uuid = ?", (missing,)
    ).fetchone()[0]
    conn.close()
    assert result["sessions_walked"] == 9
    assert covered == 9
    assert missing_rows == 0


def test_window_dollars_use_counted_compounding_not_flat_ledger(measure, tmp_path):
    """A window dollar includes the later re-reads, not only the flat one-shot cost."""
    db = tmp_path / "trends.db"
    conn = _schema_db(measure, db)
    now = _utc_now()
    sid = "window-session"
    path = tmp_path / "projects" / f"{sid}.jsonl"
    start = now - timedelta(hours=1)
    _transcript(path, start, turns=4, model="claude-sonnet-4-5")
    conn.execute(
        "INSERT INTO session_log(session_uuid, jsonl_path, date) VALUES (?, ?, ?)",
        (sid, str(path), now.date().isoformat()),
    )
    event_utc = start + timedelta(seconds=30)
    conn.execute(
        "INSERT INTO savings_events(event_type, timestamp, tokens_saved, cost_saved_usd, "
        "session_uuid, session_id, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("tool_archive", _local_iso(event_utc), 100_000, 0.3, sid, sid, "opus"),
    )
    conn.commit()

    result = measure._counted_window_summary(conn, now - timedelta(hours=2), now)
    conn.close()
    assert result["total_usd"] == pytest.approx(0.36, abs=1e-6)
    assert result["total_usd"] > 0.3
    assert "deduped turns" in result["method"]


def test_runway_window_keeps_disjoint_logged_savings(measure, tmp_path, monkeypatch):
    """The rendered weekly dollar must use the counted window result."""
    db = tmp_path / "trends.db"
    conn = _schema_db(measure, db)
    now = _utc_now()
    sid = "runway-session"
    path = tmp_path / "projects" / f"{sid}.jsonl"
    start = now - timedelta(hours=1)
    _transcript(path, start, turns=4, model="claude-sonnet-4-5")
    conn.execute(
        "INSERT INTO session_log(session_uuid, jsonl_path, date) VALUES (?, ?, ?)",
        (sid, str(path), now.date().isoformat()),
    )
    event_utc = start + timedelta(seconds=30)
    conn.execute(
        "INSERT INTO savings_events(event_type, timestamp, tokens_saved, cost_saved_usd, "
        "session_uuid, session_id, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("tool_archive", _local_iso(event_utc), 10_000_000, 30.0, sid, sid, "sonnet"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(db))
    measure.CLAUDE_DIR = tmp_path
    monkeypatch.setattr(measure, "_dashboard_spent_token_basis",
                        lambda conn, days=30: {"tokens": 100_000_000,
                                               "basis": "test", "complete": True})
    monkeypatch.setattr(measure, "_runway_routing_lever", lambda days=30: {"state": "estimated", "factor": 1.4})
    monkeypatch.setattr(measure, "_keepwarm_read_meters", lambda **_: {
        "available": True, "stale": False, "five_hour_pct": 10.0,
        "seven_day_pct": 10.0, "seven_day_resets_at": None,
        "age_s": 1.0, "ts": time.time() - 1,
    })
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **_: {
        "total_cost_usd": 40.0,  # $30 removal + $10 setup/output or unmatched events
        "model_routing": {"realized_cost_usd": 0.0},
    })

    result = measure.runway_snapshot(days=30)
    weekly = next(window for window in result["windows"] if window["key"] == "seven_day")
    assert weekly["saved_usd"] == pytest.approx(46.0, abs=1e-6)
    assert weekly["saved_usd"] > 30.0
    assert result["window_savings_basis"] == "counted transcript window"
