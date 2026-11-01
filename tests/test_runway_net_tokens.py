"""Runway saved tokens follow the same netting and ownership rules as the savings totals."""
import importlib
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    mod._init_trends_db().close()
    monkeypatch.setattr(mod, "_keepwarm_read_meters", lambda now=None: {
        "available": True, "stale": False, "age_s": 60, "ts": time.time(),
        "five_hour_pct": 20.0, "seven_day_pct": 56.0,
        "seven_day_resets_at": time.time() + 3 * 24 * 3600})
    monkeypatch.setattr(mod, "_dashboard_spent_token_basis",
                        lambda conn, days=30: {"tokens": 1_000_000, "basis": "test", "complete": True})
    monkeypatch.setattr(mod, "_input_rate_mix_ratio", lambda days=30: 1.5)
    monkeypatch.setattr(mod, "_get_merged_savings", lambda days=30, since=None: {
        "total_cost_usd": 0.0, "model_routing": {"realized_cost_usd": 0.0},
        "resume_lean_estimated": None, "verbosity_steer_estimated": None,
        "repriced_to_session_mix": False})
    monkeypatch.setattr(mod, "_counted_window_summary", lambda conn, s, e: {"available": False})
    yield mod
    sys.modules.pop("measure", None)


def _ts(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


def _saved(m, savings=(), compression=()):
    conn = sqlite3.connect(str(m.TRENDS_DB))
    for n, (event_type, tokens) in enumerate(savings):
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,session_id,session_uuid)"
                     " VALUES(?,?,?,?,?)", (_ts(10 + n * 10), event_type, tokens, SID, SID))
    for n, (feature, orig, comp, tier) in enumerate(compression):
        conn.execute("INSERT INTO compression_events(timestamp,session_id,session_uuid,feature,"
                     "original_tokens,compressed_tokens,tier,verified) VALUES(?,?,?,?,?,?,?,1)",
                     (_ts(200 + n * 10), SID, SID, feature, orig, comp, tier))
    conn.commit()
    conn.close()
    return m.runway_snapshot(days=30)["tokens_saved"]


def test_archive_reexpansion_is_netted_and_mirrors_count_once(m):
    saved = _saved(m,
                   savings=[("tool_archive", 100), ("tool_archive_reexpand", 80), ("delta_read", 50)],
                   compression=[("delta_read", 100, 50, "measured")])
    assert saved == 70


def test_reexpansion_never_drives_the_archive_credit_below_zero(m):
    saved = _saved(m, savings=[("tool_archive", 100), ("tool_archive_reexpand", 300)])
    assert saved == 0


def test_compression_only_features_still_count(m):
    saved = _saved(m, compression=[("bash_compress_git", 1000, 400, "measured"),
                                   ("bash_compress_ls", 500, 100, "opportunity")])
    assert saved == 600


def test_structure_map_mirror_counts_once(m):
    saved = _saved(m, savings=[("structure_map", 40)],
                   compression=[("structure_map", 90, 50, "measured")])
    assert saved == 40
