"""resume_lean lever rework (v5.13.1, 2026-08-30).

WHAT THE OLD LEVER DID WRONG (measured on the owner ledger, 370 events):
  * magnitude: credited the target session's LIFETIME cache_create_1h+5m --
    every incremental write plus every full re-write after each >1h idle --
    instead of the last-turn context a cold `claude --resume` actually
    re-sends. Median claim was 0.4x the real reload; two whale events claimed
    ~10x (a 531K context credited as 2.75M, twice).
  * rate: priced at the session input rate, while 305/370 events fired within
    the 1h cache TTL of the target's last turn -- a warm `--resume` is a cache
    HIT (cache_read), and a cold one is a 1h cache WRITE, never fresh input.
  * tier: the trigger is a counterfactual (the resume the user WOULD have
    run), so it must not sit inside the measured counted headline.
  * dedup: the 6h window let the same target be credited twice 42h apart.

WHAT THIS FILE PINS:
  1. tokens = target's last-turn context (input+cache_read+cache_creation)
     minus the ~30K shared prefix minus the lean block;
  2. warm (<60min) -> cache_read rate; cold -> cache_write_1h rate;
  3. relocation to the estimated tier (resume_lean_estimated), never in
     total_cost_usd / by_category;
  4. dedup per target FOREVER (a 10-day-old credit still blocks);
  5. the model-mix reprice leaves resume_lean rows untouched.

Run: python3 -m pytest tests/test_resume_lean_savings.py -v
"""
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

SID = "abcd1234-1111-2222-3333-444455556666"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    # Pin the pricing regime so the Sonnet-5 introductory flip on 2026-09-01
    # cannot silently change these arithmetic assertions.
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._apply_sonnet_intro_pricing()
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _rates(m, name):
    return m.PRICING_TIERS[m._load_pricing_tier()]["claude_models"][name]


def _db(m, tmp_path, monkeypatch):
    dbp = tmp_path / "trends.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE savings_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,
            tokens_saved INTEGER DEFAULT 0, cost_saved_usd REAL DEFAULT 0.0,
            session_id TEXT, detail TEXT, model TEXT, session_uuid TEXT,
            unjoinable INTEGER DEFAULT 0);
        CREATE TABLE session_log (
            id INTEGER PRIMARY KEY, date TEXT, input_tokens INTEGER,
            output_tokens INTEGER, api_calls INTEGER,
            model_usage_json TEXT, all_model_usage_json TEXT, session_uuid TEXT);
        CREATE TABLE compression_events (
            id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT, feature TEXT,
            original_tokens INTEGER DEFAULT 0, compressed_tokens INTEGER DEFAULT 0,
            compression_ratio REAL DEFAULT 0.0, quality_preserved INTEGER DEFAULT 1,
            verified INTEGER DEFAULT 0, detail TEXT, session_uuid TEXT,
            model TEXT, tier TEXT);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))
    return dbp


def _transcript(m, tmp_path, monkeypatch, ctx=500_000, model="claude-opus-5",
                age_minutes=20, sid=SID):
    """Fake Claude projects dir holding one transcript whose LAST assistant
    line carries the given context/model/age. Includes a sidechain line and a
    later non-usage line after it, so tail-scanning + filtering are exercised."""
    proj = tmp_path / "claude" / "projects" / "-Users-owner"
    proj.mkdir(parents=True)
    ts = (datetime.now(timezone.utc).replace(tzinfo=None)
          - timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    usage = {"input_tokens": 2, "cache_read_input_tokens": ctx - 1002,
             "cache_creation_input_tokens": 1000, "output_tokens": 50}
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant", "timestamp": ts, "message": {
            "model": model, "usage": usage}}),
        # A LATER sidechain call and a non-usage trailer must both be skipped.
        json.dumps({"type": "assistant", "isSidechain": True, "timestamp": ts, "message": {
            "model": "claude-haiku-4-5", "usage": {"input_tokens": 2,
            "cache_read_input_tokens": 99, "cache_creation_input_tokens": 0}}}),
        json.dumps({"type": "system", "subtype": "turn_duration"}),
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    return ctx


def _rows(dbp):
    conn = sqlite3.connect(str(dbp))
    try:
        return conn.execute(
            "SELECT event_type, tokens_saved, cost_saved_usd, model, detail "
            "FROM savings_events").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. MAGNITUDE + RATE
# ---------------------------------------------------------------------------

def test_warm_resume_prices_last_turn_context_at_cache_read(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    ctx = _transcript(m, tmp_path, monkeypatch, ctx=500_000, age_minutes=20)
    lean = "L" * 4000  # ~1K tokens

    m._log_resume_lean_savings(SID, lean)

    rows = _rows(dbp)
    assert len(rows) == 1
    et, tok, cost, model, detail = rows[0]
    assert et == "resume_lean"
    expected_tok = ctx - m._RESUME_LEAN_SHARED_PREFIX_TOKENS - m._estimate_tokens(lean)
    assert tok == expected_tok
    cache_read = float(_rates(m, "opus")["cache_read"])
    assert cost == pytest.approx(expected_tok * cache_read / 1e6, rel=1e-9)
    assert model == "opus"
    assert "warm cache-read" in detail


def test_cold_resume_prices_at_1h_cache_write(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    ctx = _transcript(m, tmp_path, monkeypatch, ctx=400_000, age_minutes=180)

    m._log_resume_lean_savings(SID, "")

    (et, tok, cost, model, detail), = _rows(dbp)
    expected_tok = ctx - m._RESUME_LEAN_SHARED_PREFIX_TOKENS
    write_1h = float(_rates(m, "opus")["cache_write_1h"])
    assert tok == expected_tok
    assert cost == pytest.approx(expected_tok * write_1h / 1e6, rel=1e-9)
    assert "cold 1h cache-write" in detail


def test_context_smaller_than_prefix_logs_nothing(m, tmp_path, monkeypatch):
    """A tiny target (context < shared prefix + lean block) has no honest
    credit -- the old lever would have billed its lifetime cache writes."""
    dbp = _db(m, tmp_path, monkeypatch)
    _transcript(m, tmp_path, monkeypatch, ctx=25_000, age_minutes=20)

    m._log_resume_lean_savings(SID, "L" * 4000)

    assert _rows(dbp) == []


def test_lifetime_cache_create_is_no_longer_the_base(m, tmp_path, monkeypatch):
    """The regression that motivated the rework: a session_log row carrying
    2.9M lifetime cache-create must NOT inflate the credit past the real
    last-turn context."""
    dbp = _db(m, tmp_path, monkeypatch)
    conn = sqlite3.connect(str(dbp))
    conn.execute("ALTER TABLE session_log ADD COLUMN cache_create_1h_tokens INTEGER")
    conn.execute("ALTER TABLE session_log ADD COLUMN cache_create_5m_tokens INTEGER")
    conn.execute(
        "INSERT INTO session_log(date, session_uuid, cache_create_1h_tokens,"
        " cache_create_5m_tokens) VALUES(date('now'), ?, 2900000, 0)", (SID,))
    conn.commit()
    conn.close()
    ctx = _transcript(m, tmp_path, monkeypatch, ctx=531_000, age_minutes=20)

    m._log_resume_lean_savings(SID, "")

    (_, tok, _, _, _), = _rows(dbp)
    assert tok == ctx - m._RESUME_LEAN_SHARED_PREFIX_TOKENS
    assert tok < 2_900_000


# ---------------------------------------------------------------------------
# 2. DEDUP FOREVER
# ---------------------------------------------------------------------------

def test_dedup_blocks_a_second_credit_even_ten_days_later(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _transcript(m, tmp_path, monkeypatch)
    old_ts = (datetime.now() - timedelta(days=10)).isoformat()
    conn = sqlite3.connect(str(dbp))
    conn.execute(
        "INSERT INTO savings_events(timestamp, event_type, tokens_saved,"
        " cost_saved_usd, session_id, session_uuid) VALUES(?,?,?,?,?,?)",
        (old_ts, "resume_lean", 100, 0.01, SID, SID))
    conn.commit()
    conn.close()

    m._log_resume_lean_savings(SID, "")

    assert len(_rows(dbp)) == 1  # only the pre-existing row; 6h window is gone


# ---------------------------------------------------------------------------
# 3. TIER = ESTIMATED (relocated out of the counted headline)
# ---------------------------------------------------------------------------

def test_resume_lean_is_relocated_to_the_estimated_tier(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _transcript(m, tmp_path, monkeypatch, ctx=500_000, age_minutes=20)
    m._log_resume_lean_savings(SID, "")

    s = m._get_savings_summary(days=30)

    assert "resume_lean" not in s["by_category"]
    assert s["total_cost_usd"] == pytest.approx(0.0, abs=1e-9)
    assert s["total_tokens"] == 0
    est = s["resume_lean_estimated"]
    assert est and est["events"] == 1 and est["tokens_saved"] > 0

    merged_keys = None
    # The merged view must re-export the pool for the dashboard.
    merged_keys = m._get_merged_savings(days=30)
    assert (merged_keys.get("resume_lean_estimated") or {}).get("events") == 1


def test_reprice_leaves_resume_lean_rows_untouched(m, tmp_path, monkeypatch):
    """resume_lean rows price at cache rates of the avoided reload; the
    session-mix INPUT reprice must skip them (the old behavior dragged them to
    ~$5/MTok input)."""
    dbp = _db(m, tmp_path, monkeypatch)
    conn = sqlite3.connect(str(dbp))
    tokens = 1_000_000
    logged = tokens * 0.5 / 1e6  # cache_read-priced, as v5.13.1 logs it
    conn.execute(
        "INSERT INTO savings_events(timestamp, event_type, tokens_saved,"
        " cost_saved_usd, model, session_id, session_uuid) VALUES(?,?,?,?,?,?,?)",
        ((datetime.now() - timedelta(hours=1)).isoformat(), "resume_lean",
         tokens, logged, "opus", SID, SID))
    conn.execute(
        "INSERT INTO session_log(date, input_tokens, output_tokens, api_calls,"
        " model_usage_json, all_model_usage_json, session_uuid)"
        " VALUES(date('now'), 1000000, 200000, 100, ?, ?, ?)",
        (json.dumps({"claude-opus-5": 9_000_000}),
         json.dumps({"claude-opus-5": 9_000_000}), SID))
    conn.commit()
    conn.close()

    s = m._get_savings_summary(days=30)

    est = s["resume_lean_estimated"]
    assert est["cost_saved_usd"] == pytest.approx(logged, rel=1e-9)


# ---------------------------------------------------------------------------
# 4. FALLBACK (no transcript)
# ---------------------------------------------------------------------------

def test_missing_transcript_falls_back_to_working_set_at_cache_read(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")  # no projects dir
    monkeypatch.setattr(m, "_checkpoint_restore_recovery_tokens",
                        lambda sid, floor_tokens: 200_000)
    monkeypatch.setattr(m, "_resolve_session_model", lambda sid=None: "opus")

    m._log_resume_lean_savings(SID, "")

    (et, tok, cost, model, detail), = _rows(dbp)
    assert tok == 200_000
    cache_read = float(_rates(m, "opus")["cache_read"])
    assert cost == pytest.approx(tok * cache_read / 1e6, rel=1e-9)
    assert "working-set fallback" in detail


@pytest.mark.parametrize("age_minutes", [20, 180])
def test_advisor_resume_credit_uses_executor_context_not_billed_sum(m, tmp_path, monkeypatch, age_minutes):
    dbp = _db(m, tmp_path, monkeypatch)
    ctx = _transcript(m, tmp_path, monkeypatch, ctx=491_151, age_minutes=age_minutes)
    path = next((m.CLAUDE_DIR / "projects").glob(f"*/{SID}.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    usage = records[1]["message"]["usage"]
    executor = dict(usage, type="message")
    usage.update({key: value * 2 for key, value in executor.items()
                  if key.endswith("tokens")})
    usage["iterations"] = [dict(executor),
        {"type": "advisor_message", "input_tokens": 900_000}, executor]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    lean = "L" * 4000
    m._log_resume_lean_savings(SID, lean)
    (event, tokens, cost, model, detail), = _rows(dbp)
    expected = ctx - m._RESUME_LEAN_SHARED_PREFIX_TOKENS - m._estimate_tokens(lean)
    assert event == "resume_lean"
    assert tokens == expected
    rate = "cache_read" if age_minutes < 60 else "cache_write_1h"
    assert cost == pytest.approx(expected * float(_rates(m, "opus")[rate]) / 1e6)
    assert model == "opus"
