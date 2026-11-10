"""Savings-truth U6: lean-resume carry stays a shadow diagnostic.

Pins the carry rule (avoided context x real parent turns to first
compact_boundary, capped when context growth passes the avoided size) and the
hard boundary that the diagnostic never enters any savings total.

Run: python3 -m pytest tests/test_lean_resume_carry.py -v
"""
import importlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
SID = "abcd1234-1111-2222-3333-444455556666"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    sys.path.insert(0, str(SCRIPTS))
    for name in ("measure", "lean_resume_carry"):
        if name in sys.modules:
            del sys.modules[name]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._apply_sonnet_intro_pricing()
    carry = importlib.import_module("lean_resume_carry")
    # Event timestamps are naive local in the ledger; transcript timestamps are
    # UTC. Pin the event conversion to UTC-naive so fixtures stay deterministic.
    monkeypatch.setattr(
        mod, "_counted_event_utc",
        lambda ts: datetime.strptime(str(ts).replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S"))
    yield mod, carry
    for name in ("measure", "lean_resume_carry"):
        if name in sys.modules:
            del sys.modules[name]


def _db(m, tmp_path, monkeypatch, rows):
    dbp = tmp_path / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE savings_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,
            tokens_saved INTEGER DEFAULT 0, cost_saved_usd REAL DEFAULT 0.0,
            session_id TEXT, detail TEXT, model TEXT, session_uuid TEXT,
            unjoinable INTEGER DEFAULT 0);
    """)
    for row in rows:
        conn.execute(
            "INSERT INTO savings_events(timestamp, event_type, tokens_saved, "
            "cost_saved_usd, session_id, session_uuid, model, detail) "
            "VALUES(?,?,?,?,?,?,?,?)",
            row)
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))
    return dbp


def _event_row(ts="2026-01-01T10:00:00", tokens=1000, sid=SID, model="opus"):
    return (ts, "resume_lean", tokens, tokens * 0.5 / 1e6, sid, sid, model,
            "lean resume vs --resume reload of last-turn context (warm cache-read, $0.5/MTok)")


def _turn(ts, rid, ctx, model="claude-opus-5"):
    # input + cache_read + cache_creation == ctx exactly.
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": rid,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": max(0, ctx - 102),
                "cache_creation_input_tokens": 100,
                "output_tokens": 10,
            },
        },
    })


def _compact(ts):
    return json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": ts})


def _transcript(m, tmp_path, monkeypatch, lines, sid=SID):
    proj = tmp_path / "claude" / "projects" / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    return path


def test_carry_is_avoided_context_times_turns_to_first_compaction(m, tmp_path, monkeypatch):
    mod, carry = m
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    _transcript(mod, tmp_path, monkeypatch, [
        _turn("2026-01-01T10:00:05Z", "r1", 5000),
        _turn("2026-01-01T10:00:10Z", "r2", 5100),
        _turn("2026-01-01T10:00:15Z", "r3", 5200),
        _compact("2026-01-01T10:00:20Z"),
        _turn("2026-01-01T10:00:25Z", "r4", 5300),
    ])

    out = carry.summary(days=30, since="2020-01-01", include_events=True)

    assert out["available"] is True
    assert out["included_in_totals"] is False
    assert out["events"] == 1
    assert out["events_priced"] == 1
    assert out["carry_tokens"] == 3000
    # claude-opus-5 exact card cache_read is $0.50/MTok.
    assert out["carry_cost_usd"] == pytest.approx(3000 * 0.5 / 1e6, rel=1e-6)
    (item,) = out["event_details"]
    assert item["turns"] == 3
    assert item["stopped_by"] == "compaction"


def test_carry_stops_at_growth_cap_before_first_compaction(m, tmp_path, monkeypatch):
    mod, carry = m
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    _transcript(mod, tmp_path, monkeypatch, [
        _turn("2026-01-01T10:00:05Z", "r1", 5000),
        _turn("2026-01-01T10:00:10Z", "r2", 5600),   # +600 <= avoided
        _turn("2026-01-01T10:00:15Z", "r3", 6200),   # +1200 > avoided -> cap
        _turn("2026-01-01T10:00:20Z", "r4", 6300),
    ])

    out = carry.summary(days=30, since="2020-01-01", include_events=True)

    assert out["carry_tokens"] == 2000
    (item,) = out["event_details"]
    assert item["turns"] == 2
    assert item["stopped_by"] == "growth_cap"


def test_missing_transcript_is_shadow_zero_not_an_error(m, tmp_path, monkeypatch):
    mod, carry = m
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path / "claude")

    out = carry.summary(days=30, since="2020-01-01", include_events=True)

    assert out["available"] is True
    assert out["carry_tokens"] == 0
    assert out["carry_cost_usd"] == 0.0
    (item,) = out["event_details"]
    assert item["stopped_by"] == "no_transcript"
    assert item["turns"] == 0


def test_request_chunks_dedup_and_sidechains_are_not_real_turns(m, tmp_path, monkeypatch):
    mod, carry = m
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    sidechain = json.loads(_turn("2026-01-01T10:00:06Z", "side", 9000))
    sidechain["isSidechain"] = True
    _transcript(mod, tmp_path, monkeypatch, [
        _turn("2026-01-01T10:00:05Z", "r1", 5000),
        # Streamed second chunk of the same request: higher ctx wins, still one turn.
        _turn("2026-01-01T10:00:05Z", "r1", 5100),
        json.dumps(sidechain),
        _turn("2026-01-01T10:00:10Z", "r2", 5200),
    ])

    out = carry.summary(days=30, since="2020-01-01", include_events=True)

    (item,) = out["event_details"]
    assert item["turns"] == 2
    assert out["carry_tokens"] == 2000


def test_shadow_field_never_writes_or_enters_savings_totals(m, tmp_path, monkeypatch):
    mod, carry = m
    dbp = _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    _transcript(mod, tmp_path, monkeypatch, [
        _turn("2026-01-01T10:00:05Z", "r1", 5000),
        _turn("2026-01-01T10:00:10Z", "r2", 5100),
    ])
    before = dbp.read_bytes()

    out = carry.summary(days=30, since="2020-01-01", include_events=False)

    assert out["carry_tokens"] == 2000
    assert out["included_in_totals"] is False
    assert "total_cost_usd" not in out and "total_tokens" not in out
    assert dbp.read_bytes() == before


def test_counterfactual_compaction_uses_lowest_observed_auto_threshold(m, tmp_path, monkeypatch):
    mod, carry = m
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    auto = lambda ts, n: json.dumps({"type": "system", "subtype": "compact_boundary",
                                     "timestamp": ts, "compactMetadata": {"trigger": "auto", "preTokens": n}})
    _transcript(mod, tmp_path, monkeypatch, [
        auto("2026-01-01T09:00:00Z", 7000), auto("2026-01-01T09:30:00Z", 6000),
        _turn("2026-01-01T10:00:05Z", "r1", 4900),
        _turn("2026-01-01T10:00:10Z", "r2", 5000),
        _turn("2026-01-01T10:00:15Z", "r3", 5100),
    ])
    result = carry.summary(days=30, since="2020-01-01")
    assert result["carry_tokens"] == 1000
    assert result["event_details"][0]["stopped_by"] == "counterfactual_compaction"
    assert result["event_details"][0]["threshold_source"] == "observed_auto"
    assert result["threshold_source_counts"]["observed_auto"] == 1


@pytest.mark.parametrize("model,context,env,settings,threshold", [
    ("claude-sonnet-4-5", 100000, {}, {}, 200000),                      # 200K model default
    ("claude-opus-5", 100000, {}, {}, 967000),                          # 1M user who never passed 200K
    ("claude-sonnet-4-5", 201000, {}, {}, 967000),                      # the session proves a 1M window
    ("claude-opus-5", 100000, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"}, {}, 300000),
    ("claude-opus-5", 100000, {}, {"autoCompactWindow": 400000}, 400000),
    ("claude-opus-5", 100000, {}, {"modelSettings": {"claude-opus-5": {"autoCompactWindow": 450000}}}, 450000),
    ("claude-opus-5", 100000, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}, {}, 483500),
])
def test_counterfactual_fallback_is_the_users_effective_window(m, tmp_path, monkeypatch, model, context, env, settings, threshold):
    mod, carry = m
    for key in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mod, "_read_settings_json", lambda: (settings, None))
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    _transcript(mod, tmp_path, monkeypatch, [
        _turn("2026-01-01T09:00:00Z", "before", context, model=model),
        _turn("2026-01-01T10:00:05Z", "r1", threshold - 1001, model=model),
        _turn("2026-01-01T10:00:10Z", "r2", threshold - 1000, model=model),
    ])
    result = carry.summary(days=30, since="2020-01-01")
    item = result["event_details"][0]
    assert item["turns"] == 1
    assert item["stopped_by"] == "counterfactual_compaction"
    assert item["threshold_source"] == "fallback"
    assert result["threshold_source_counts"]["fallback"] == 1


def test_larger_streamed_chunk_retracts_turn_at_compaction_boundary(m, tmp_path, monkeypatch):
    mod, carry = m
    _db(mod, tmp_path, monkeypatch, [_event_row(tokens=1000)])
    _transcript(mod, tmp_path, monkeypatch, [
        _turn("2026-01-01T10:00:05Z", "r1", 198000, model="claude-sonnet-4-5"),
        _turn("2026-01-01T10:00:10Z", "r2", 198999, model="claude-sonnet-4-5"),
        _turn("2026-01-01T10:00:10Z", "r2", 199000, model="claude-sonnet-4-5"),
    ])
    item = carry.summary(days=30, since="2020-01-01")["event_details"][0]
    assert item["turns"] == 1
    assert item["stopped_by"] == "counterfactual_compaction"


def test_verified_carry_follows_receiver_and_rejects_live_old_session(m, tmp_path, monkeypatch):
    mod, carry = m
    receiver = 'efab5678-1111-2222-3333-444455556666'
    old = _transcript(mod, tmp_path, monkeypatch, [_turn('2026-01-01T09:50:00Z', 'old1', 5000)])
    recv = old.parent / (receiver + '.jsonl')
    injection = json.dumps({'type': 'attachment', 'timestamp': '2026-01-01T10:00:00Z', 'attachment': {'type': 'hook_additional_context', 'content': '[Token Optimizer] Cold-resume lean (session abcd1234, 2026-01-01): context'}})
    recv.write_text('\n'.join([injection, _turn('2026-01-01T10:00:05Z', 'new1', 5000), _turn('2026-01-01T10:00:10Z', 'new2', 5100)]))
    row = {'id': 1, 'timestamp': '2026-01-01T10:00:00', 'session_id': SID, 'session_uuid': SID,
           'tokens_saved': 1000, 'cost_saved_usd': .01, 'model': 'opus', 'detail': 'warm cache-read receiver=' + receiver}
    tier = mod.PRICING_TIERS[mod._load_pricing_tier()]
    result = carry._score_verified_event(row, mod, tier)
    assert result['eligible'] and result['turns'] == 2 and result['carry_tokens'] == 2000
    old.write_text(old.read_text() + _turn('2026-01-01T10:00:20Z', 'old2', 5200) + '\n')
    assert not carry._score_verified_event(row, mod, tier)['eligible']
    row['detail'] = ''
    assert not carry._score_verified_event(row, mod, tier)['eligible']


def test_verified_summary_credits_only_delivered_stopped_session(m, tmp_path, monkeypatch):
    mod, carry = m
    receiver = 'efab5678-1111-2222-3333-444455556666'
    old = _transcript(mod, tmp_path, monkeypatch, [_turn('2026-01-01T09:50:00Z', 'old1', 5000)])
    recv = old.parent / (receiver + '.jsonl')
    injection = json.dumps({'type': 'attachment', 'timestamp': '2026-01-01T10:00:00Z', 'attachment': {'type': 'hook_additional_context', 'content': '[Token Optimizer] Cold-resume lean (session abcd1234, 2026-01-01): context'}})
    recv.write_text('\n'.join([injection, _turn('2026-01-01T10:00:05Z', 'new1', 5000)]))
    rows = [{'id': 1, 'timestamp': '2026-01-01T10:00:00', 'session_id': SID, 'session_uuid': SID,
             'tokens_saved': 1000, 'cost_saved_usd': .01, 'model': 'opus', 'detail': 'warm cache-read receiver=' + receiver}]
    monkeypatch.setattr(carry, '_events', lambda **kw: rows)
    out = carry.verified_summary(days=30)
    assert out['events'] == 1 and out['tokens_saved'] == 2000
    rate = mod._counted_exact_card('claude-opus-5', mod.PRICING_TIERS[mod._load_pricing_tier()])['cache_read']
    assert out['cost_saved_usd'] == pytest.approx(2 * 1000 * rate / 1e6)
    old.write_text(old.read_text() + _turn('2026-01-01T10:00:20Z', 'old2', 5200) + '\n')
    assert carry.verified_summary(days=30)['events'] == 0
