"""Tests for SPEC-limit-units: limit_units(row), plan-limits currency, expand_round_trip,
per-feature signed net, deduped transcript rows, and C6 feature labels/nets.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
from _measure import limit_units as lu  # noqa: E402


def test_limit_units_exact_model_cards_no_hardcoded_0_1():
    # Opus 5.5: input=4, output=20 (5x), cache_read=0.2 (0.05x), cw5=1.25x, cw1=2.0x
    r_opus = lu.limit_units({
        "model": "claude-opus-5-5",
        "input": 1000,
        "cache_write_5m": 1000,
        "cache_write_1h": 1000,
        "cache_read": 10000,
        "output": 1000,
    }, facade=measure)
    assert r_opus["priced"] is True
    assert r_opus["unpriced_tokens"] == 0
    assert r_opus["by_class"]["input"] == pytest.approx(1000.0)
    assert r_opus["by_class"]["cache_write_5m"] == pytest.approx(1250.0)
    assert r_opus["by_class"]["cache_write_1h"] == pytest.approx(2000.0)
    assert r_opus["by_class"]["cache_read"] == pytest.approx(500.0)  # 0.05x, NOT 0.1x
    assert r_opus["by_class"]["output"] == pytest.approx(5000.0)
    assert r_opus["units"] == pytest.approx(9750.0)

    # Fable 5.2: input=8, output=40 (5x), cache_read=0.2 (0.025x)
    r_fable = lu.limit_units({
        "model": "claude-fable-5-2",
        "input": 0,
        "cache_read": 10000,
        "output": 0,
    }, facade=measure)
    assert r_fable["priced"] is True
    assert r_fable["by_class"]["cache_read"] == pytest.approx(250.0)  # 0.025x
    assert r_fable["units"] == pytest.approx(250.0)


def test_limit_units_unknown_model_is_unpriced_r10():
    r = lu.limit_units({
        "model": "mystery-model-99",
        "input": 500,
        "cache_read": 4000,
        "output": 200,
    }, facade=measure)
    assert r["priced"] is False
    assert r["units"] == 0.0
    assert r["unpriced_tokens"] == 4700


def test_plan_limits_cache_only_saving_moves_percentage_and_same_currency():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE session_log (session_id TEXT, is_sidechain INT, parent_daily_usage_json TEXT)")
    today = datetime.now().date().isoformat()
    usage = {
        today: {
            "claude-opus-5-5": {
                "fresh_input": 100,
                "output": 1000,
                "cache_read": 200000,
                "cache_create_1h": 50000,
                "cache_create_5m": 0,
                "cache_create": 50000,
            }
        }
    }
    conn.execute("INSERT INTO session_log VALUES ('s1', 0, ?)", (json.dumps(usage),))

    # Zero savings
    res0 = lu.plan_limits_summary(measure, conn, 30, summary={"total_tokens": 0}, counted={"reread_tokens": 0})
    assert res0["label"] == "share of window, modeled at API-rate weights"
    assert res0["diagnostic_fresh_plus_output_tokens"] == 1100
    assert res0["share_pct"] == 0.0
    expected_denom = lu.limit_units({"model": "claude-opus-5-5", **usage[today]["claude-opus-5-5"]}, facade=measure)["units"]
    assert res0["denominator_units"] == pytest.approx(expected_denom, abs=0.01)

    # Cache-only saving (repeat reads only, zero fresh input/output saved) MUST move share_pct
    res_cache = lu.plan_limits_summary(measure, conn, 30, summary={"total_tokens": 0}, counted={"reread_tokens": 100000})
    expected_num = lu.limit_units({"model": "claude-opus-5-5", "cache_read": 100000}, facade=measure)["units"]
    assert res_cache["saved_units"] == pytest.approx(expected_num, abs=0.01)
    assert res_cache["share_pct"] > 0.0
    assert res_cache["share_pct"] == pytest.approx(expected_num / expected_denom * 100.0, abs=0.001)


def test_expand_round_trip_and_negative_feature_net():
    summary = {
        "total_tokens": 1000 + 20000,  # includes raw expand_round_trip before pop
        "total_cost_usd": 0.01 + 0.004,
        "total_events": 2,
        "by_category": {
            "tool_archive": {"events": 1, "tokens_saved": 1000, "cost_saved_usd": 0.01},
            "expand_round_trip": {"events": 1, "tokens_saved": 20000, "cost_saved_usd": 0.004},
        },
    }
    out = lu.apply_round_trip_and_feature_net(summary, counted={}, labels=measure._SAVINGS_CATEGORY_LABELS)
    # expand_round_trip must NEVER appear as its own saving line
    assert "expand_round_trip" not in out["by_category"]
    # Negative net must show negative (no floor at zero)
    ta_net = out["feature_net"]["tool_archive"]
    assert ta_net["net_tokens"] == 1000 - 20000
    assert ta_net["net_tokens"] < 0
    assert ta_net["round_trip_tokens"] == 20000


def test_log_expand_round_trip_unknown_when_no_next_request(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "trends.db")
    logged = []
    monkeypatch.setattr(measure, "_log_savings_event", lambda et, tok, **kw: logged.append((et, tok, kw)))
    res = lu.log_expand_round_trip(measure, "sess-1", "toolu_1", feature="tool_archive", next_request=None)
    assert res["round_trip_unknown"] is True
    assert res["tokens"] == 0
    assert len(logged) == 1
    assert logged[0][0] == "expand_round_trip"
    assert logged[0][1] == 0
    assert "round_trip_unknown" in logged[0][2]["detail"]

    # With a next request on record: debits at that model's cache_read rate
    res2 = lu.log_expand_round_trip(measure, "sess-1", "toolu_2", feature="tool_archive",
                                    next_request=(200000, "claude-opus-5-5"))
    assert res2["round_trip_unknown"] is False
    assert res2["tokens"] == 200000
    assert res2["cost_usd"] == pytest.approx(200000 * 0.2 / 1e6, abs=1e-6)


def test_dedupe_transcript_rows_resumed_session_and_old_row():
    rows = [
        # File 1: in-window assistant + tool_result
        {"timestamp": "2026-11-10T10:00:00Z", "message": {"id": "msg_1", "role": "assistant", "usage": {"cache_read_input_tokens": 50000}}},
        {"timestamp": "2026-11-10T10:00:01Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1"}]}},
        # File 2 (resumed session, recently touched): duplicates msg_1 and tu_1 + has an old row outside window
        {"timestamp": "2026-11-10T10:00:00Z", "message": {"id": "msg_1", "role": "assistant", "usage": {"cache_read_input_tokens": 50000}}},
        {"timestamp": "2026-11-10T10:00:01Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1"}]}},
        {"timestamp": "2026-09-01T08:00:00Z", "message": {"id": "msg_old", "role": "assistant", "usage": {"cache_read_input_tokens": 999999}}},
        {"timestamp": "2026-11-11T10:00:00Z", "message": {"id": "msg_2", "role": "assistant", "usage": {"cache_read_input_tokens": 60000}}},
    ]
    kept = lu.dedupe_transcript_rows(rows, cutoff_iso="2026-10-25T00:00:00Z")
    ids = [r["message"].get("id") or r["message"]["content"][0]["tool_use_id"] for r in kept]
    assert ids == ["msg_1", "tu_1", "msg_2"]


def test_all_c6_features_have_human_label_and_net():
    c6_features = [
        "bash_compress_pipeline", "bash_search_noise_fold", "build_output_compress",
        "pipe_compress", "bash_reread_unchanged", "large_output_policy",
        "builtin_grep", "builtin_glob", "big_read_preview",
        "cmd_git_diff_stat", "cmd_git_ls", "cmd_gh_log", "cmd_gh_list",
        "cmd_terraform", "cmd_kubectl_describe", "cmd_pkg_install", "cmd_mypy",
    ]
    by_cat = {f: {"events": 1, "tokens_saved": 500, "cost_saved_usd": 0.002} for f in c6_features}
    summary = {"total_tokens": 500 * len(c6_features), "total_cost_usd": round(0.002 * len(c6_features), 4),
               "total_events": len(c6_features), "by_category": by_cat}
    out = lu.apply_round_trip_and_feature_net(summary, counted={}, labels=measure._SAVINGS_CATEGORY_LABELS)
    for f in c6_features:
        assert f in out["feature_net"]
        entry = out["feature_net"][f]
        assert entry["label"] and entry["label"] != f
        assert entry["net_tokens"] == 500
        assert entry["net_usd"] == pytest.approx(0.002)
    # R2: parts add to total to the cent
    assert round(sum(v["net_usd"] for v in out["feature_net"].values()), 2) == round(out["total_cost_usd"], 2)
