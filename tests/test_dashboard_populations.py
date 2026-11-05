from _activity_fixtures import stamp_activity
import importlib
import sqlite3
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


def test_trends_exposes_labelled_main_cache_and_excluded_population(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE session_log (date TEXT, duration_minutes REAL, input_tokens INTEGER,
        output_tokens INTEGER, message_count INTEGER, cache_hit_rate REAL,
        cache_create_1h_tokens INTEGER DEFAULT 0, cache_create_5m_tokens INTEGER DEFAULT 0,
        is_sidechain INTEGER DEFAULT 0);
    """)
    rows = [
      ("2099-01-01", 10, 100, 10, 50, .90, 0),
      ("2099-01-01", 10, 100, 10, 100, .80, 0),
      ("2099-01-01", 1, 100, 10, 1, .00, 1),
      ("2099-01-01", 1, 100, 10, 5, .10, 0),
    ]
    conn.executemany("INSERT INTO session_log (date,duration_minutes,input_tokens,output_tokens,message_count,cache_hit_rate,is_sidechain) VALUES (?,?,?,?,?,?,?)", rows)
    stamp_activity(conn)
    stats = mod._query_dashboard_population_metrics(conn, 30)
    assert stats == {
      "all_sessions": 4, "main_work_sessions": 3, "delegated_sessions": 1,
      "cache_eligible_main_sessions": 2, "cache_ineligible_sessions": 2,
      "cache_eligible_main_hit_rate": 0.85,
      "cache_ineligible_hit_rate": 0.05,
    }
