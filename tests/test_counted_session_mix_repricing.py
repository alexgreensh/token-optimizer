"""Stored repeat-read counts without per-turn prices are priced from the session's recorded models."""
import json
import sqlite3

import pytest

from test_counted_cumulative import m, _db, SID


def _legacy_row(conn, key, reread_tokens, uuid=SID, tokens=1000, turns=7):
    conn.execute(
        "INSERT INTO counted_reread (event_key,source,session_uuid,event_ts,event_month,tokens,"
        "oneshot_usd,reread_tokens,reread_usd,turns_counted,transcript_mtime,computed_at,pricing_detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (key, "se", uuid, "2026-10-10T10:00:00", "2026-10", tokens, 999.0, reread_tokens,
         999.0, turns, None, "2026-10-10T10:00:00"))


def _session(conn, breakdown, uuid=SID):
    conn.execute(
        "INSERT INTO session_log (jsonl_path,date,session_uuid,model_usage_breakdown_json) VALUES (?,?,?,?)",
        (f"/synthetic/{uuid}.jsonl", "2026-10-10", uuid, json.dumps(breakdown)))


def _parts(cache_read):
    return {"fresh_input": 1, "cache_read": cache_read, "cache_create": 1, "output": 1}


def test_legacy_reread_priced_at_recorded_model_cache_read_rate(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    cards = m.PRICING_TIERS["anthropic"]["claude_models"]
    with sqlite3.connect(db) as conn:
        _session(conn, {"claude-opus-5-5": _parts(600), "claude-fable-5-1": _parts(400)})
        _legacy_row(conn, "se:1", 1_000_000)
        out = m._counted_rollup_rows(m._counted_stored_rows(conn).values(), conn)
    expected = 1_000_000 * (0.6 * cards["opus_5_5"]["cache_read"]
                            + 0.4 * cards["fable_5_1"]["cache_read"]) / 1e6
    assert out["reread_usd"] == pytest.approx(expected, rel=1e-9)
    assert out["reread_session_mix_priced_tokens"] == 1_000_000
    assert out["reread_unpriced_tokens"] == 0
    assert out["reread_prices_available"] is True
    assert "recorded" in out["reread_pricing_note"]
    # The one-time part and the stored dollars of an unverified row are never reused.
    assert out["oneshot_usd"] == 0
    assert out["oneshot_unpriced_credit_tokens"] == 1000


def test_unknown_model_share_stays_unpriced_and_is_not_borrowed(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    cards = m.PRICING_TIERS["anthropic"]["claude_models"]
    with sqlite3.connect(db) as conn:
        _session(conn, {"claude-opus-5-5": _parts(600), "vendor/mystery-model": _parts(400),
                        "claude-opus-5-99": _parts(0)})
        _legacy_row(conn, "se:1", 1_000_000)
        out = m._counted_rollup_rows(m._counted_stored_rows(conn).values(), conn)
    assert out["reread_usd"] == pytest.approx(0.6 * cards["opus_5_5"]["cache_read"], rel=1e-9)
    assert out["reread_session_mix_priced_tokens"] == 600_000
    assert out["reread_unpriced_credit_tokens"] == 400_000
    assert out["reread_unpriced_debit_tokens"] == 0
    assert out["reread_pricing_complete"] is False


def test_debit_counts_are_priced_with_their_sign(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    rate = m.PRICING_TIERS["anthropic"]["claude_models"]["opus_5_5"]["cache_read"]
    with sqlite3.connect(db) as conn:
        _session(conn, {"claude-opus-5-5": _parts(50)})
        _legacy_row(conn, "se:1", -2_000_000)
        out = m._counted_rollup_rows(m._counted_stored_rows(conn).values(), conn)
    assert out["reread_usd"] == pytest.approx(-2_000_000 * rate / 1e6, rel=1e-9)
    assert out["reread_unpriced_tokens"] == 0


@pytest.mark.parametrize("breakdown", [None, {}, {"vendor/mystery-model": _parts(10)},
                                       {"claude-opus-5-99": _parts(10)}])
def test_no_exact_model_record_keeps_every_token_unpriced(m, tmp_path, monkeypatch, breakdown):
    db = _db(m, tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        if breakdown is not None:
            _session(conn, breakdown)
        _legacy_row(conn, "se:1", 1_000_000)
        out = m._counted_rollup_rows(m._counted_stored_rows(conn).values(), conn)
    assert out["reread_usd"] == 0
    assert out["reread_session_mix_priced_tokens"] == 0
    assert out["reread_unpriced_credit_tokens"] == 1_000_000
    assert out["reread_prices_available"] is False


def test_current_per_turn_rows_are_never_repriced_from_the_session_mix(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    detail = dict.fromkeys(m._COUNTED_COVERAGE_KEYS, 0) | {
        "version": m._COUNTED_PRICING_VERSION, "priced_reread_turns": 3}
    with sqlite3.connect(db) as conn:
        _session(conn, {"claude-fable-5-1": _parts(10)})
        _legacy_row(conn, "se:1", 1_000_000)
        conn.execute("UPDATE counted_reread SET pricing_detail=?, reread_usd=0.5, oneshot_usd=0",
                     (json.dumps(detail),))
        out = m._counted_rollup_rows(m._counted_stored_rows(conn).values(), conn)
    assert out["reread_usd"] == 0.5
    assert out["session_mix_priced_events"] == 0


def test_summary_and_period_carry_the_session_mix_price(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    rate = m.PRICING_TIERS["anthropic"]["claude_models"]["opus_5_5"]["cache_read"]
    with sqlite3.connect(db) as conn:
        _session(conn, {"claude-opus-5-5": _parts(50)})
        _legacy_row(conn, "se:1", 3_000_000)
        conn.commit()
    summary = m._counted_cumulative_summary()
    assert summary["reread_usd"] == pytest.approx(3_000_000 * rate / 1e6, abs=1e-6)
    assert summary["reread_session_mix_priced_tokens"] == 3_000_000
    month = summary["monthly_coverage"]["2026-10"]
    assert month["reread_usd"] == summary["reread_usd"]
