"""Quality-filter audit tests: the _SESSION_QUALITY_FILTER is silently shrinking
every card that uses it.

The filter (input_tokens >= 1000 AND duration_minutes >= 1.0) was written when a
session meant a person typing. Token Optimizer's workflow now creates large numbers
of short, legitimate, genuinely-billed sessions (delegated subagents, background
tasks, one-shot automation). On the real ledger:

  June: 58% of parent sessions pass. August: 15.1% pass.

Seven sites use the filter. Six of them are broken by it. This test file proves
each break with a synthetic DB where the filter's inclusion/exclusion changes the
metric, and verifies the fix (removing the filter from sites where it doesn't
belong) produces the correct number.

The filter is KEPT only at site 6 (_short_session_pool_savings) where the NEGATED
filter is the deliberate complement that defines the short-session pool.

Run: python3 -m pytest tests/test_quality_filter_audit.py -v
"""
from _activity_fixtures import stamp_activity
import importlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"

_SCHEMA = """
CREATE TABLE session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jsonl_path TEXT UNIQUE, date TEXT NOT NULL, project TEXT,
    duration_minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
    message_count INTEGER, api_calls INTEGER, cache_hit_rate REAL,
    cache_create_1h_tokens INTEGER DEFAULT 0, cache_create_5m_tokens INTEGER DEFAULT 0,
    cache_ttl_scanned INTEGER DEFAULT 0, avg_call_gap_seconds REAL,
    max_call_gap_seconds REAL, p95_call_gap_seconds REAL, skills_json TEXT,
    subagents_json TEXT, tool_calls_json TEXT, model_usage_json TEXT,
    all_model_usage_json TEXT, model_usage_breakdown_json TEXT, version TEXT,
    slug TEXT, topic TEXT, collected_at TEXT, quality_score REAL, quality_grade TEXT,
    stale_waste_tokens INTEGER DEFAULT 0, session_uuid TEXT, is_sidechain INTEGER DEFAULT 0
);
CREATE TABLE model_daily (
    date TEXT, model TEXT, total_tokens INTEGER,
    PRIMARY KEY (date, model)
);
CREATE TABLE savings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
    tokens_saved INTEGER DEFAULT 0, cost_saved_usd REAL DEFAULT 0.0,
    session_id TEXT, detail TEXT, model TEXT, session_uuid TEXT,
    unjoinable INTEGER DEFAULT 0, pause_key TEXT
);
CREATE TABLE compression_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
    tokens_saved INTEGER DEFAULT 0, cost_saved_usd REAL DEFAULT 0.0,
    session_id TEXT, detail TEXT, model TEXT, session_uuid TEXT
);
"""


@pytest.fixture
def measure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-qfa-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    # These tests exercise the Anthropic-billing estimator. Runtime detection is
    # memoized per process, so pin it explicitly: a test that ran earlier under a
    # Cursor/Copilot environment would otherwise leave the estimator in its
    # "unsupported billing" branch and every count here reads zero.
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    sys.path.insert(0, str(SCRIPTS))
    import runtime_env as _runtime_env
    _runtime_env.detect_runtime.cache_clear()
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod, tmp
    _runtime_env.detect_runtime.cache_clear()
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _seed_db(tmp, sessions, baseline=None):
    """Create a trends.db with the given sessions.

    sessions = list of dicts with keys:
      input_tokens, output_tokens, cache_hit_rate, cache_create_5m_tokens,
      duration_minutes, subagents_json, model_usage_json,
      model_usage_breakdown_json, max_call_gap_seconds, date, is_sidechain
    """
    snap = Path(tmp)
    snap.mkdir(parents=True, exist_ok=True)
    if baseline:
        (snap / "baseline_state.json").write_text(json.dumps(baseline))
    db = snap / "trends.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    today = datetime.now().strftime("%Y-%m-%d")
    for i, s in enumerate(sessions):
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, "
            "cache_hit_rate, cache_create_5m_tokens, cache_create_1h_tokens, "
            "model_usage_json, all_model_usage_json, model_usage_breakdown_json, "
            "is_sidechain, duration_minutes, max_call_gap_seconds, subagents_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"/s/{tmp}/{i}.jsonl",
                s.get("date", today),
                s.get("input_tokens", 0),
                s.get("output_tokens", 0),
                s.get("cache_hit_rate", 0.0),
                s.get("cache_create_5m_tokens", 0),
                s.get("cache_create_1h_tokens", 0),
                json.dumps(s.get("model_usage_json", {})),
                json.dumps(s.get("all_model_usage_json", {})),
                json.dumps(s.get("model_usage_breakdown_json", {})),
                s.get("is_sidechain", 0),
                s.get("duration_minutes", 0.0),
                s.get("max_call_gap_seconds", 0),
                json.dumps(s.get("subagents_json", {})),
            ),
        )
    stamp_activity(conn)
    conn.commit()
    conn.close()
    return snap


# ---------------------------------------------------------------------------
# Helper: create sessions that pass and fail the filter
# ---------------------------------------------------------------------------

def _big_session(**kw):
    """A session that PASSES the quality filter (>=1000 input, >=1.0 min)."""
    defaults = {
        "input_tokens": 500_000,
        "output_tokens": 50_000,
        "cache_hit_rate": 0.9,
        "cache_create_5m_tokens": 50_000,
        "duration_minutes": 30.0,
        "model_usage_json": {"claude-opus-4-8": 500_000},
        "model_usage_breakdown_json": {
            "claude-opus-4-8": {
                "fresh_input": 50_000, "cache_read": 400_000,
                "cache_create": 50_000, "cache_create_5m": 50_000,
                "cache_create_1h": 0, "output": 50_000,
            }
        },
    }
    defaults.update(kw)
    return defaults


def _short_session(**kw):
    """A session that FAILS the quality filter (<1000 input OR <1.0 min)."""
    defaults = {
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_hit_rate": 0.0,
        "cache_create_5m_tokens": 0,
        "duration_minutes": 0.5,
        "model_usage_json": {"claude-haiku-4-5-20251001": 500},
        "model_usage_breakdown_json": {
            "claude-haiku-4-5-20251001": {
                "fresh_input": 500, "cache_read": 0,
                "cache_create": 0, "cache_create_5m": 0,
                "cache_create_1h": 0, "output": 100,
            }
        },
    }
    defaults.update(kw)
    return defaults


# ---------------------------------------------------------------------------
# Site 1: _estimate_uncaptured_runtime — filter overstates per_session
# ---------------------------------------------------------------------------

def test_site1_uncaptured_runtime_counts_all_sessions(measure):
    """_estimate_uncaptured_runtime divides measured_tokens by session_count.
    The filter shrinks session_count from 10 to 3, making per_session 3.3x too high.
    The fix: count ALL sessions, not just filtered ones.
    """
    mod, tmp = measure
    # 3 big sessions with subagents + 7 short sessions with subagents
    sessions = []
    for i in range(3):
        sessions.append(_big_session(subagents_json={"general-purpose": 10}))
    for i in range(7):
        sessions.append(_short_session(subagents_json={"general-purpose": 5}))
    _seed_db(tmp, sessions)

    # Pass compression/savings directly so the function doesn't bail on
    # empty compression events. The test is about the session_count
    # denominator, not the compression measurement.
    fake_compression = {"total_tokens": 100_000}
    fake_savings = {"total_tokens": 50_000}

    result = mod._estimate_uncaptured_runtime(
        days=30, compression=fake_compression, savings=fake_savings
    )
    # The subagent_dispatches should count from ALL sessions with subagents,
    # not just the 3 that pass the filter.
    # If filtered: session_count=3, dispatches=30+35=65, per_session is based on 3
    # If all: session_count=10, dispatches=65, per_session is based on 10
    # The fix should make session_count=10
    assert result["subagent_dispatches"] == 65, (
        f"Expected 65 total dispatches (30 from big + 35 from short), got {result['subagent_dispatches']}"
    )


# ---------------------------------------------------------------------------
# Site 2: _session_output_fraction — filter excludes short sessions negligibly
# but should be removed for correctness (volume-weighted metric)
# ---------------------------------------------------------------------------

def test_site2_output_fraction_includes_short_sessions(measure):
    """_session_output_fraction is SUM(output)/SUM(fresh+output).
    It's volume-weighted, so the filter (which gates by count, not volume)
    should not apply. Short sessions with real output tokens must be counted.
    """
    mod, tmp = measure
    # 1 big session: 1M input, 100K output, 90% cache hit
    # 10 short sessions: 500 input each, 5000 output each (high output ratio)
    sessions = [_big_session(input_tokens=1_000_000, output_tokens=100_000,
                             cache_hit_rate=0.9, cache_create_5m_tokens=100_000)]
    for i in range(10):
        sessions.append(_short_session(input_tokens=500, output_tokens=50_000,
                                        cache_hit_rate=0.0, cache_create_5m_tokens=0))
    _seed_db(tmp, sessions)

    frac = mod._session_output_fraction(days=30)
    # With filter: only the big session counts. fresh = 1M - 100K - 900K = 0,
    # output = 100K. frac = 100K / (0 + 100K) = 1.0
    # Without filter: all 11 sessions count.
    # big: fresh=0, output=100K. short: fresh=500*10=5K, output=50K*10=500K.
    # total: fresh=5K, output=600K. frac = 600K/605K = 0.992
    # The filter overstates the output fraction by excluding high-output short sessions.
    assert frac < 1.0, (
        f"Output fraction is 1.0 — the filter excluded all short sessions. "
        f"Expected < 1.0 when short sessions with real output are included. Got {frac}"
    )


# ---------------------------------------------------------------------------
# Site 3: _estimate_cache_drop_savings — filter excludes short drop sessions
# ---------------------------------------------------------------------------

def test_site3_cache_drop_includes_short_sessions(measure):
    """_estimate_cache_drop_savings counts sessions with max_call_gap > TTL.
    A short automation session that had a cache drop is still a real cache drop.
    The filter should not exclude it.
    """
    mod, tmp = measure
    # 2 big sessions with cache drops + 3 short sessions with cache drops
    sessions = []
    for i in range(2):
        sessions.append(_big_session(max_call_gap_seconds=600))
    for i in range(3):
        sessions.append(_short_session(max_call_gap_seconds=600))
    _seed_db(tmp, sessions)

    result = mod._estimate_cache_drop_savings(days=30)
    # With filter: drop_sessions=2 (only big sessions)
    # Without filter: drop_sessions=5 (all sessions with gaps)
    assert result["drop_sessions"] == 5, (
        f"Expected 5 drop sessions (2 big + 3 short), got {result['drop_sessions']}. "
        f"The filter excluded short sessions that had real cache drops."
    )


# ---------------------------------------------------------------------------
# Site 4: _compute_baseline_state — filter inflates the frozen anchor
# ---------------------------------------------------------------------------

def test_site4_baseline_includes_all_sessions(measure):
    """_compute_baseline_state computes the frozen "typical session" anchor.
    The filter excludes short sessions, inflating the mean.
    The fix: compute the baseline from ALL human sessions, not just filtered ones.
    """
    mod, tmp = measure
    # Create sessions spanning 35 days so the early window has enough data.
    # Mix of big and short sessions in the early window.
    base_date = datetime.now() - timedelta(days=35)
    sessions = []
    # 20 big sessions in the early window (days 1-30)
    for i in range(20):
        sessions.append(_big_session(
            input_tokens=2_000_000,
            date=(base_date + timedelta(days=i % 30 + 1)).strftime("%Y-%m-%d"),
        ))
    # 20 short sessions in the early window
    for i in range(20):
        sessions.append(_short_session(
            input_tokens=500,
            date=(base_date + timedelta(days=i % 30 + 1)).strftime("%Y-%m-%d"),
        ))
    _seed_db(tmp, sessions)

    baseline = mod._compute_baseline_state()
    assert baseline is not None, "Baseline should be computable with 40 sessions"
    # With filter: only 20 big sessions, mean input ~2M
    # Without filter: 40 sessions, mean input ~1M (20*2M + 20*500) / 40
    # The frozen typical_session fresh_input should reflect ALL sessions.
    # If the filter is still applied, sessions_used will be ~20, not ~40.
    assert baseline["window"]["sessions_used"] > 25, (
        f"Baseline used only {baseline['window']['sessions_used']} sessions. "
        f"Expected > 25 (all sessions, not just filtered). The filter is excluding "
        f"short sessions from the frozen anchor."
    )


# ---------------------------------------------------------------------------
# Site 5: _mix_from_session_rows — filter shifts model mix
# ---------------------------------------------------------------------------

def test_site5_model_mix_includes_short_sessions(measure):
    """_mix_from_session_rows computes model shares for the transformation's
    "actual" arm. The filter removes short sessions (which tend to use cheaper
    models like Haiku), making the mix look more Opus-heavy than it is.
    This understates routing savings.
    """
    mod, tmp = measure
    # 1 big Opus session + 10 short Haiku sessions
    sessions = [_big_session(
        input_tokens=1_000_000,
        model_usage_json={"claude-opus-4-8": 1_000_000},
    )]
    for i in range(10):
        sessions.append(_short_session(
            input_tokens=500,
            model_usage_json={"claude-haiku-4-5-20251001": 500},
        ))
    _seed_db(tmp, sessions)

    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    mix = mod._mix_from_session_rows(cutoff)
    # With filter: only Opus session counts -> 100% Opus
    # Without filter: Opus=1M, Haiku=500*10=5K. Opus share = 1M/(1M+5K) = 99.5%
    # The filter makes it look 100% Opus when it's 99.5%.
    # More dramatic on real data (73.9% vs 79.8%).
    assert "claude-haiku-4-5-20251001" in mix, (
        f"Haiku is missing from the model mix. The filter excluded all short Haiku sessions. "
        f"Mix: {mix}"
    )


# ---------------------------------------------------------------------------
# Site 6: _short_session_pool_savings — NEGATED filter is CORRECT (no fix)
# ---------------------------------------------------------------------------

def test_site6_short_session_pool_uses_negated_filter(measure):
    """_short_session_pool_savings uses _SESSION_QUALITY_FILTER_NEGATED to
    select below-threshold sessions. This is the deliberate complement of the
    positive filter. It must KEEP the negated filter — removing it would
    double-count sessions that are already in the main pool.
    """
    mod, tmp = measure
    # 2 big + 3 short sessions with breakdown_json
    sessions = []
    for i in range(2):
        sessions.append(_big_session())
    for i in range(3):
        sessions.append(_short_session())
    _seed_db(tmp, sessions)

    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    # The pool takes a MEASURED baseline Opus share; there is no assumed default.
    result = mod._short_session_pool_savings(cutoff, 0.0)
    # The short pool should only count the 3 short sessions, not all 5.
    assert result["sessions"] == 3, (
        f"Short pool should have 3 sessions (below-threshold only), got {result['sessions']}. "
        f"If this is 5, the negated filter was removed — that would double-count."
    )


# ---------------------------------------------------------------------------
# Site 7: _baseline_progress — filter can delay "ready" signal
# ---------------------------------------------------------------------------

def test_site7_baseline_progress_counts_all_sessions(measure):
    """_baseline_progress counts sessions in the early window to determine
    if the baseline is "ready" (>= 30 sessions). The filter can delay this
    for users with many short sessions.
    """
    mod, tmp = measure
    base_date = datetime.now() - timedelta(days=35)
    sessions = []
    # 20 big + 15 short = 35 total in window (>= 30 threshold)
    for i in range(20):
        sessions.append(_big_session(
            date=(base_date + timedelta(days=i % 30 + 1)).strftime("%Y-%m-%d"),
        ))
    for i in range(15):
        sessions.append(_short_session(
            date=(base_date + timedelta(days=i % 30 + 1)).strftime("%Y-%m-%d"),
        ))
    _seed_db(tmp, sessions)

    progress = mod._baseline_progress()
    assert progress is not None
    # With filter: only 20 sessions counted -> NOT READY (needs 30)
    # Without filter: 35 sessions counted -> READY
    assert progress["ready"] is True, (
        f"Baseline should be READY with 35 sessions, but progress says "
        f"sessions_in_window={progress['sessions_in_window']}, ready={progress['ready']}. "
        f"The filter is delaying the ready signal by excluding short sessions."
    )


# ---------------------------------------------------------------------------
# Site 8: _estimate_before_after_savings — surgical cache-hit fix
# ---------------------------------------------------------------------------

def test_site8_cache_hit_uses_unfiltered_rows(measure):
    """_estimate_before_after_savings keeps the filter on recent_rows for
    sessions_per_month (which multiplies the frozen anchor), but computes
    the cache-hit rate (cur_hit) from ALL sessions via a separate
    unfiltered query. This test verifies the cache-hit rate reflects
    short sessions' cache patterns, not just the filtered subset.
    """
    mod, tmp = measure
    # 40 big sessions with 90% pool-hit (cache_read=3.6M, fresh=400K, no cw)
    # 100 short sessions with 0% cache hit (all fresh, no cache read)
    # The short sessions pull the overall cache-hit rate below 0.9.
    sessions = []
    for i in range(40):
        sessions.append(_big_session(
            input_tokens=4_000_000,
            cache_hit_rate=0.9,
            cache_create_5m_tokens=0,
            duration_minutes=30.0,
        ))
    for i in range(100):
        sessions.append(_short_session(
            input_tokens=500,
            cache_hit_rate=0.0,
            cache_create_5m_tokens=0,
            duration_minutes=0.5,
        ))
    _seed_db(tmp, sessions, baseline={
        "version": 4,
        "activity_basis": 1,
        "typical_session": {
            "fresh_input": 400000, "cache_write": 0,
            "cache_read": 3600000, "output": 50000,
        },
        "opus_share": 0.95, "opus_share_source": "pretool_baseline",
        "model_shares": {"opus": 0.95, "sonnet": 0.05},
        "window": {"start": "2026-01-01", "end": "2026-01-31",
                   "sessions_used": 100, "sessions_total": 100, "elapsed": True},
        "method": "winsorized_mean", "winsor_pct": 0.99,
        "structural_overhead_tokens": 0,
        "captured_at": "2026-01-15T00:00:00", "source": "frozen_from_history",
    })

    result = mod._estimate_before_after_savings(days=30)
    # sessions_per_month should be 40 (filtered, multiplies frozen anchor)
    assert result["sessions_per_month"] == 40, (
        f"sessions_per_month should be 40 (filtered count), got {result['sessions_per_month']}. "
        f"The filter must be KEPT on recent_rows for the session count."
    )
    # Pool-hit = CR / (F + CR).
    # Filtered (big only): F=40*400K=16M, CR=40*3.6M=144M. hit=144/160=0.9
    # Unfiltered (all): F=16M+100*500=16.05M, CR=144M. hit=144/160.05=0.89969...
    # The short sessions add 50K fresh input with 0 cache read, pulling the
    # rate below 0.9. If the filter is still on the cache-hit query, it stays 0.9.
    assert result["after_cache_hit"] < 0.9, (
        f"after_cache_hit is {result['after_cache_hit']}, expected < 0.9. "
        f"A value of exactly 0.9 means only filtered sessions were counted. "
        f"The unfiltered query should include short sessions with 0% cache hit."
    )
