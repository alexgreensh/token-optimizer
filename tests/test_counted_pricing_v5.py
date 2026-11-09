"""Counted pricing v5 (savings-truth U4): write-class premium in the total,
cache-miss reread turns at their write class, lean resumes on exact cards.

Synthetic fixtures only.
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

from test_resume_lean_savings import SCRIPTS, SID, _db, _rows, _transcript, m  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
from _measure import counted_pricing_v5 as v5  # noqa: E402

OPUS = "claude-opus-5-5"


def _card(m):
    return m._counted_exact_card(OPUS, m.PRICING_TIERS[m._load_pricing_tier()])


# --------------------------------------------------------------------------
# Cache-miss boundary
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cache_class,tokens,expected", [
    ((1000, 1000, 0, 0), 1000, True),      # wrote exactly the removed amount, read nothing
    ((1000, 1000, 0, 0), 1001, False),     # wrote less than was removed
    ((5000, 5000, 0, 1), 1000, False),     # any cache read means a partial hit: ambiguous
    ((0, 0, 0, 0), 1000, False),           # no cache use at all is not a miss
    ((1000, 0, 1000, 0), -1000, True),     # debits use the same size test
    (None, 1000, False),                    # no usage record
    ((1000, 1000, 0, 0), 0, False),
])
def test_miss_test_boundary(cache_class, tokens, expected):
    assert v5.is_cache_miss_turn(cache_class, tokens) is expected


def test_miss_turn_reprices_only_that_turn(m):
    tier = m.PRICING_TIERS[m._load_pricing_tier()]
    card = _card(m)
    t0 = datetime(2026, 11, 1, 12, 0, 0)
    tokens = 2000
    turns = [
        (t0 + timedelta(seconds=10), OPUS, 90_000, (500, 500, 0, 89_000)),   # first call: oneshot
        (t0 + timedelta(seconds=20), OPUS, 91_000, (300, 300, 0, 90_000)),   # hit: cache_read
        (t0 + timedelta(seconds=30), OPUS, 91_000, (91_000, 91_000, 0, 0)),  # full miss, 1h write
        (t0 + timedelta(seconds=40), OPUS, 92_000, (60_000, 60_000, 0, 32_000)),  # partial: cache_read
        (t0 + timedelta(seconds=50), OPUS, 92_000, (1_500, 0, 1_500, 0)),    # wrote < removed: cache_read
    ]
    out = m._counted_score_detail(turns, [], t0, tokens, tier)
    assert out["turns_counted"] == 4
    assert out["reread_miss_turns"] == 1
    expected = tokens * (3 * card["cache_read"] + card["cache_write_1h"]) / 1e6
    assert out["reread_usd"] == pytest.approx(expected)
    assert out["reread_miss_premium_usd"] == pytest.approx(
        tokens * (card["cache_write_1h"] - card["cache_read"]) / 1e6)
    assert out["reread_tokens"] == 4 * tokens  # token counts never change


def test_miss_turn_5m_write_uses_5m_rate(m):
    tier = m.PRICING_TIERS[m._load_pricing_tier()]
    card = _card(m)
    t0 = datetime(2026, 11, 1, 12, 0, 0)
    turns = [(t0 + timedelta(seconds=10), OPUS, 10_000, None),
             (t0 + timedelta(seconds=20), OPUS, 10_000, (10_000, 0, 10_000, 0))]
    out = m._counted_score_detail(turns, [], t0, 1000, tier)
    assert out["reread_usd"] == pytest.approx(1000 * card["cache_write"] / 1e6)


def test_pricing_version_is_5_so_old_rows_rescore(m):
    assert m._COUNTED_PRICING_VERSION == "5"


def test_rollup_sums_miss_fields_from_current_rows(m):
    detail = dict.fromkeys(m._COUNTED_COVERAGE_KEYS, 0)
    detail.update(version=m._COUNTED_PRICING_VERSION, reread_miss_turns=2,
                  reread_miss_premium_usd=0.0156, oneshot_class_premium_usd=0.01)
    row = {"tokens": 100, "reread_tokens": 400, "oneshot_usd": 0.0004, "reread_usd": 0.02,
           "pricing_detail": json.dumps(detail), "transcript_mtime": 1.0, "source": "ce",
           "session_uuid": "s", "turns_counted": 4}
    out = m._counted_rollup_rows([row, dict(row)])
    assert out["reread_miss_turns"] == 4
    assert out["reread_miss_premium_usd"] == pytest.approx(0.0312)


def test_session_mix_price_matches_the_previous_inline_formula():
    terms = [(700, 0.2), (300, None)]
    for amount in (1000, -1000, 0, 7):
        usd, priced, rest = v5.session_mix_price(amount, terms)
        assert priced == round(abs(amount) * 700 / 1000)
        assert usd == pytest.approx(amount * 700 * 0.2 / 1000 / 1e6)
        assert abs(rest) == abs(amount) - priced


# --------------------------------------------------------------------------
# Lean resumes on exact cards
# --------------------------------------------------------------------------

def _breakdown_db(m, tmp_path, monkeypatch, sessions):
    dbp = _db(m, tmp_path, monkeypatch)
    conn = sqlite3.connect(str(dbp))
    conn.execute("ALTER TABLE session_log ADD COLUMN model_usage_breakdown_json TEXT")
    for sid, breakdown in sessions.items():
        conn.execute("INSERT INTO session_log(date, session_uuid, model_usage_breakdown_json) "
                     "VALUES(date('now'), ?, ?)", (sid, json.dumps(breakdown)))
    conn.commit()
    conn.close()
    return dbp


def _resume_row(dbp, sid, tokens, cost, basis, family="opus"):
    conn = sqlite3.connect(str(dbp))
    conn.execute(
        "INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd, model,"
        " session_id, session_uuid, detail) VALUES(?,?,?,?,?,?,?,?)",
        ((datetime.now() - timedelta(hours=1)).isoformat(), "resume_lean", tokens, cost,
         family, sid, sid, f"lean resume vs --resume reload of last-turn context ({basis}, $x/MTok)"))
    conn.commit()
    conn.close()


def test_family_priced_resume_rows_reprice_on_the_exact_card(m, tmp_path, monkeypatch):
    other = "ffff0000-1111-2222-3333-444455556666"
    dbp = _breakdown_db(m, tmp_path, monkeypatch, {
        SID: {OPUS: {"cache_read": 9_000_000, "output": 10}, "<synthetic>": {"output": 0}}})
    _resume_row(dbp, SID, 1_000_000, 0.5, "warm cache-read")          # generic opus $0.50
    _resume_row(dbp, SID, 100_000, 1.0, "cold 1h cache-write")        # generic opus $10
    _resume_row(dbp, other, 1_000_000, 0.5, "warm cache-read")        # no session row: kept
    card = _card(m)
    est = m._get_savings_summary(days=30)["resume_lean_estimated"]
    expected = 1.0 * card["cache_read"] + 0.1 * card["cache_write_1h"] + 0.5
    assert est["cost_saved_usd"] == pytest.approx(expected, abs=1e-4)
    assert est["exact_card_reprice"]["repriced_events"] == 2
    assert est["exact_card_reprice"]["unresolved_events"] == 1
    # Stored rows are never rewritten.
    assert sorted(r[2] for r in _rows(dbp)) == [0.5, 0.5, 1.0]


def test_family_mismatch_is_not_guessed():
    assert v5.resume_exact_model({"claude-sonnet-5-5": {"cache_read": 5}}, "opus") is None
    assert v5.resume_exact_model({"claude-opus-5-5": {"cache_read": 5},
                                  "claude-opus-5": {"cache_read": 9}}, "opus") == "claude-opus-5"
    assert v5.resume_rate_key("something else") is None


def test_new_resume_credit_logs_at_the_exact_card(m, tmp_path, monkeypatch):
    dbp = _breakdown_db(m, tmp_path, monkeypatch, {SID: {OPUS: {"cache_read": 9_000_000}}})
    ctx = _transcript(m, tmp_path, monkeypatch, ctx=500_000, model=OPUS, age_minutes=20)
    m._log_resume_lean_savings(SID, "")
    (_et, tok, cost, _model, _detail), = _rows(dbp)
    assert tok == ctx - m._RESUME_LEAN_SHARED_PREFIX_TOKENS
    assert cost == pytest.approx(tok * _card(m)["cache_read"] / 1e6, rel=1e-9)
    # Read-time repricing of an exact-priced row is a no-op.
    est = m._get_savings_summary(days=30)["resume_lean_estimated"]
    assert est["cost_saved_usd"] == pytest.approx(round(cost, 4), abs=1e-4)


# --------------------------------------------------------------------------
# Write-class premium in the total (CLI)
# --------------------------------------------------------------------------

def test_cli_prints_premium_as_its_own_measured_line(m, monkeypatch, capsys):
    from test_compounded_headline import sample
    s = sample()
    s["oneshot_pricing"] = {"basis": "input_rate_minimum", "removal_events": 9, "removal_tokens": 9000}
    s["counted_period"].update(oneshot_class_events=4, oneshot_class_premium_usd=1.25)
    monkeypatch.setattr(m, "_get_merged_savings", lambda **kw: dict(s))
    monkeypatch.setattr(m, "_counted_cumulative_summary", lambda **kw: dict(s["counted_period"]))
    m.savings_report(days=30, as_json=False)
    text = capsys.readouterr().out
    assert "+ write-class premium: ~$1.25" in text and "4 removals" in text
    assert "not in the total" not in text
    m.savings_report(days=30, as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["action_savings"]["class_premium_usd"] == 1.25
    assert data["action_savings"]["display"]["components_cents"]["class_premium"] == 125


def test_unavailable_ledger_adds_no_premium():
    assert v5.class_premium_usd({"available": False, "oneshot_class_premium_usd": 9}) == 0.0
    assert v5.class_premium_usd(None) == 0.0


# --------------------------------------------------------------------------
# Orphaned derived rows (event type left the counted set)
# --------------------------------------------------------------------------

def test_rows_for_events_that_left_the_counted_set_are_excluded_not_deleted(m, tmp_path, monkeypatch):
    import test_counted_cumulative as tcc
    from test_counted_session_mix_repricing import _legacy_row, _parts, _session
    mod = m
    db = tcc._db(mod, tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        _session(conn, {OPUS: _parts(50)})
        conn.execute("INSERT INTO savings_events (id, timestamp, event_type, tokens_saved, session_uuid)"
                     " VALUES (1, '2026-10-10T10:00:00', 'checkpoint_restore', 1000, ?)", (tcc.SID,))
        _legacy_row(conn, "se:1", 3_000_000)        # source exists, no longer counted
        _legacy_row(conn, "se:2", 2_000_000)        # source gone: durable history, kept
        conn.commit()
    summary = mod._counted_cumulative_summary()
    assert summary["orphaned_rows_excluded"] == 1
    assert summary["reread_tokens"] == 2_000_000
    assert summary["remeasuring"] is (summary["remeasuring_events"] > 0)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM counted_reread").fetchone()[0] == 2
