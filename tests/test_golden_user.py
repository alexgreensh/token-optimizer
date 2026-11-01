"""Golden user: every headline number on a synthetic history with known answers.

The dashboard's numbers have regressed more than once without any single test
failing, because each test checked one function in isolation. This file runs
one realistic history end to end through the real collect -> savings path and
checks every headline against a value computed by hand here, then ages the
history (rebuilds, retention) and checks nothing moves.

The user:
  * installed 70 days ago; their pre-install baseline window holds 4 sessions
    of 10 calls, each call 1k fresh + 200k cache-read + 2k output (Opus 4.8);
  * since install, 4 sessions of 10 calls at 100k cache-read (lighter context);
    one of them runs on Fable 5.1 and has a 50k-token tool output archived
    after its 3rd call; one is stored twice (worktree copy).

Flat-rate cost per call (the pool's constant ruler, $5/$25/$0.50 per MTok):
  before = (1k*5 + 2k*25 + 200k*0.5) / 1e6 = $0.155
  after  = (1k*5 + 2k*25 + 100k*0.5) / 1e6 = $0.105
  -> transformation over 40 calls = (0.155 - 0.105) * 40 = $2.00
Re-reads: the archived 50k tokens stay out of calls 5..10 (6 calls) at the
Fable 5.1 cache-read rate: 6 * 50k * $0.25/MTok = $0.075.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_cost_accuracy_200 import _assistant, _load, _write  # noqa: E402

CALLS = 10
BEFORE_PER_CALL = 0.155
AFTER_PER_CALL = 0.105
POST_SESSIONS = 4
REREAD_USD = 6 * 50_000 * 0.25 / 1e6


def _sid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def _session(path, start, model, cr, prefix, mtime=None):
    recs = [_assistant(f"{prefix}-{i}", start + timedelta(minutes=i), model=model,
                       inp=1000, out=2000, cr=cr) for i in range(CALLS)]
    _write(path, recs, mtime=mtime if mtime is not None else (start + timedelta(minutes=CALLS)).timestamp())
    return recs


@pytest.fixture()
def golden(tmp_path, monkeypatch):
    return build_golden_user(tmp_path, monkeypatch)


def build_golden_user(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    snap = measure.SNAPSHOT_DIR
    snap.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(measure, "_SESSION_WEIGHT_MIN_ANCHOR_SESSIONS", 3)
    monkeypatch.setattr(measure, "_AFTER_MIN_SESSIONS", 3)
    now = datetime.now().replace(microsecond=0)
    install = now - timedelta(days=70)
    (snap / "snapshot_before.json").write_text(json.dumps({"timestamp": install.isoformat()}))
    win_start, win_end = install - timedelta(days=35), install - timedelta(days=6)
    (snap / "baseline_state.json").write_text(json.dumps({"window": {
        "start": win_start.strftime("%Y-%m-%d"), "end": win_end.strftime("%Y-%m-%d")}}))
    projects = tmp_path / "claude_home" / "projects"

    for n in range(4):  # pre-install baseline sessions
        start = (win_start + timedelta(days=3 + 5 * n)).replace(hour=11, minute=0, second=0)
        _session(projects / "-repo" / f"{_sid(n)}.jsonl", start, "claude-opus-4-8", 200_000, f"pre{n}")

    post = []
    for n in range(POST_SESSIONS):
        start = (now - timedelta(days=3 + 4 * n)).replace(hour=11, minute=0, second=0)
        model = "claude-fable-5-1" if n == 0 else "claude-opus-4-8"
        path = projects / "-repo" / f"{_sid(100 + n)}.jsonl"
        recs = _session(path, start, model, 100_000, f"post{n}")
        post.append((path, start, model, recs))
    # The same session stored twice (worktree copy): counted once everywhere.
    dup_path, dup_start, dup_model, dup_recs = post[1]
    _write(projects / "-repo-worktree" / dup_path.name, dup_recs,
           mtime=(dup_start + timedelta(minutes=CALLS)).timestamp())

    measure.collect_sessions(days=90, quiet=True)

    # A 50k-token tool output archived after the Fable session's 3rd call.
    fable_path, fable_start, _m, _r = post[0]
    ev_local = fable_start + timedelta(minutes=2, seconds=30)
    conn = sqlite3.connect(str(measure.TRENDS_DB))
    conn.execute(
        "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, "
        "session_id, session_uuid, model) VALUES (?, 'tool_archive', 50000, 0.25, ?, ?, ?)",
        (ev_local.isoformat(), fable_path.stem, fable_path.stem, "claude-fable-5-1"))
    conn.commit()
    conn.close()
    return {"measure": measure, "now": now, "install": install, "post": post,
            "window": (win_start, win_end), "tmp": tmp_path}


def _pool(measure, now):
    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    return measure._session_weight_pool_savings(cutoff, days=30)


def test_daily_cost_counts_each_session_once_at_its_own_model(golden):
    measure = golden["measure"]
    trends = measure._collect_trends_from_db(days=30)
    total = sum(d["total_cost_usd"] for d in trends["daily"])
    expected = 0.0
    for _path, _start, model, _recs in golden["post"]:
        expected += CALLS * measure._get_model_cost(model, 1000, 2000, 100_000, 0)
    assert total == pytest.approx(expected, rel=1e-3)
    assert sum(d["sessions"] for d in trends["daily"]) == POST_SESSIONS


def test_pre_install_baseline_pins_to_this_users_own_window(golden):
    measure = golden["measure"]
    assert measure._pretool_anchor_step(budget_seconds=30) == "pinned"
    pinned = measure._pinned_workload_anchor()
    m = pinned["metrics"]
    assert m["sessions"] == 4 and m["api_calls"] == 4 * CALLS
    assert m["flat_usd"] / m["api_calls"] == pytest.approx(BEFORE_PER_CALL, rel=0.01)
    # The baseline is from before install, never after it.
    win_end = golden["window"][1].strftime("%Y-%m-%d")
    assert win_end in pinned["label"] and win_end <= golden["install"].strftime("%Y-%m-%d")


def test_transformation_matches_the_hand_computed_answer(golden):
    measure = golden["measure"]
    measure._pretool_anchor_step(budget_seconds=30)
    pool = _pool(measure, golden["now"])
    assert pool["before_cost_per_unit"] == pytest.approx(BEFORE_PER_CALL, rel=0.01)
    assert pool["after_cost_per_unit"] == pytest.approx(AFTER_PER_CALL, rel=0.01)
    assert pool["now_units"] == POST_SESSIONS * CALLS
    expected = (BEFORE_PER_CALL - AFTER_PER_CALL) * POST_SESSIONS * CALLS
    assert pool["transformation_usd"] == pytest.approx(expected, rel=0.02)


def test_aging_history_never_moves_the_baseline(golden):
    """Rebuilds and retention drop the oldest ledger rows; the answer must not move."""
    measure = golden["measure"]
    measure._pretool_anchor_step(budget_seconds=30)
    first = _pool(measure, golden["now"])
    conn = sqlite3.connect(str(measure.TRENDS_DB))
    cut = (golden["now"] - timedelta(days=40)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM session_log WHERE date < ?", (cut,))
    conn.commit()
    conn.close()
    later = _pool(measure, golden["now"])
    assert later["before_cost_per_unit"] == pytest.approx(first["before_cost_per_unit"], rel=1e-9)
    assert later["transformation_usd"] == pytest.approx(first["transformation_usd"], rel=1e-9)


def test_measured_savings_include_re_reads_at_the_exact_model_rate(golden):
    measure = golden["measure"]
    rr = measure._reread_savings_for_window(30)
    assert rr["reread_tokens"] == 6 * 50_000
    assert rr["reread_usd"] == pytest.approx(REREAD_USD, rel=1e-6)


def test_cli_report_shows_every_tier_and_an_all_in_total(golden, capsys):
    """Same tiers as the dashboard's Savings tab: measured, modeled (repeat
    reads avoided), estimated, and one all-in figure that adds them up."""
    measure = golden["measure"]
    # The collector persists rereads; reports only read that counted cohort.
    with sqlite3.connect(str(measure.TRENDS_DB)) as conn:
        measure._update_counted_cumulative(conn, quiet=True)
    measure.savings_report(days=30)
    out = capsys.readouterr().out
    total_line = next(line for line in out.splitlines() if "TOTAL (measured)" in line)
    measured = float(total_line.rsplit("$", 1)[1])
    ledger = measure._get_merged_savings(days=30).get("total_cost_usd", 0.0)
    assert measured == pytest.approx(ledger, abs=0.01)
    rr_line = next(line for line in out.splitlines() if "repeat reads avoided" in line)
    assert "[modeled]" in rr_line
    all_in = float(next(line for line in out.splitlines() if "ALL IN" in line).split("$", 1)[1].split("/")[0].split(" ")[0])
    assert all_in >= measured + REREAD_USD - 0.01


def _day_totals(measure):
    trends = measure._collect_trends_from_db(days=30)
    return (round(sum(d["total_cost_usd"] for d in trends["daily"]), 6),
            sum(d["sessions"] for d in trends["daily"]))


def _ledger_totals(measure):
    conn = sqlite3.connect(str(measure.TRENDS_DB))
    try:
        return conn.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), "
                            "SUM(api_calls) FROM session_log").fetchone()
    finally:
        conn.close()


def test_collecting_again_changes_nothing(golden):
    measure = golden["measure"]
    first, ledger = _day_totals(measure), _ledger_totals(measure)
    measure.collect_sessions(days=90, quiet=True)
    measure.collect_sessions(days=90, quiet=True)
    assert _day_totals(measure) == first
    assert _ledger_totals(measure) == ledger


def test_a_growing_session_adds_exactly_its_new_requests(golden):
    """Resumed sessions and growing transcripts have double-counted before."""
    measure = golden["measure"]
    cost_before, sessions_before = _day_totals(measure)
    path, start, model, recs = golden["post"][2]
    extra = _assistant("post2-extra", start + timedelta(minutes=CALLS + 1), model=model,
                       inp=1000, out=2000, cr=100_000)
    import json as _json
    with path.open("a", encoding="utf-8") as f:  # written now, like a live session
        f.write(_json.dumps(extra) + "\n")
    ledger_before = _ledger_totals(measure)
    measure.collect_sessions(days=90, quiet=True)
    cost_after, sessions_after = _day_totals(measure)
    ledger_after = _ledger_totals(measure)
    assert ledger_after[0] == ledger_before[0]
    assert ledger_after[1] - ledger_before[1] == 1000 + 100_000
    assert ledger_after[2] - ledger_before[2] == 2000
    assert sessions_after == sessions_before
    assert cost_after - cost_before == pytest.approx(
        measure._get_model_cost(model, 1000, 2000, 100_000, 0), rel=1e-3)


def test_dashboard_savings_cards_carry_the_known_numbers(golden):
    """The Savings tab cards have gone to $0 or vanished several times; check
    the data they render and that the template still renders them."""
    measure = golden["measure"]
    conn = measure._init_trends_db()
    measure._update_counted_cumulative(conn, max_sessions=50, deadline_seconds=None)
    conn.close()
    data = measure._dashboard_savings_data(days=30)
    cp = data.get("counted_period") or {}
    assert cp.get("available") is True
    assert cp.get("reread_usd") == pytest.approx(REREAD_USD, abs=0.006)  # shown to the cent
    assert data.get("total_cost_usd", 0) > 0
    template = (measure.Path(measure.__file__).resolve().parent.parent / "assets" / "dashboard.html").read_text()
    for marker in ("function tokensSavedCardHtml", "Repeat reads avoided", 's.counted_period', 'id="hero-card"'):
        assert marker in template, f"dashboard lost {marker!r}"


def test_stored_re_read_rows_are_repriced_after_a_pricing_fix(golden):
    """Legacy prices refresh without losing the observed token history."""
    measure = golden["measure"]
    conn = measure._init_trends_db()
    measure._update_counted_cumulative(conn, max_sessions=50, deadline_seconds=None)
    history_sql = ("SELECT event_key,source,session_uuid,event_ts,event_month,tokens,"
                   "reread_tokens,turns_counted,transcript_mtime FROM counted_reread ORDER BY event_key")
    history = conn.execute(history_sql).fetchall()
    assert len(history) == 1
    ledgers = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
               for table in ("savings_events", "compression_events")}
    # Legacy rows predate per-row price evidence but retain observed token counts.
    changed = conn.execute("UPDATE counted_reread SET reread_usd = reread_usd * 4, pricing_detail = NULL")
    assert changed.rowcount == len(history)
    conn.execute("DELETE FROM token_optimizer_meta WHERE key = ?", (measure._COUNTED_META_PRICING,))
    conn.commit()
    assert conn.execute("SELECT SUM(reread_usd) FROM counted_reread").fetchone()[0] == pytest.approx(4 * REREAD_USD)
    pending = measure._update_counted_cumulative(conn, max_sessions=0, deadline_seconds=None)
    assert pending["rows_written"] == 0 and pending["pending_sessions"] == 1
    unpriced = measure._counted_cumulative_summary()
    # The stored dollars are discarded; the observed repeat-read count is priced
    # at the cache-read rate of the models the session recorded.
    assert unpriced["reread_usd"] == pytest.approx(REREAD_USD, rel=1e-6)
    assert unpriced["reread_session_mix_priced_tokens"] == unpriced["reread_tokens"] == 300_000
    assert unpriced["reread_unpriced_tokens"] == 0
    assert unpriced["legacy_unverified_events"] == 1
    # The initial removal keeps the input-rate floor of the session's models, disclosed.
    assert unpriced["oneshot_floor_events"] == 1 and unpriced["oneshot_floor_tokens"] > 0
    assert unpriced["oneshot_unpriced_tokens"] == 0
    assert unpriced["reread_pricing_complete"] is False
    assert measure._action_savings_components({}, unpriced)["total_usd"] == pytest.approx(REREAD_USD)
    assert conn.execute(history_sql).fetchall() == history

    refreshed = measure._update_counted_cumulative(conn, max_sessions=50, deadline_seconds=None)
    assert refreshed["rows_written"] == 1 and refreshed["pending_sessions"] == 0
    total = conn.execute("SELECT SUM(reread_usd) FROM counted_reread").fetchone()[0]
    assert total == pytest.approx(REREAD_USD, rel=1e-6)
    priced = measure._counted_cumulative_summary()
    assert priced["reread_usd"] == pytest.approx(REREAD_USD, rel=1e-6)
    assert priced["reread_tokens"] == 300_000
    assert priced["legacy_unverified_events"] == priced["reread_unpriced_tokens"] == 0
    assert priced["reread_pricing_complete"] is True
    assert measure._action_savings_components({}, priced)["total_usd"] == pytest.approx(REREAD_USD)
    assert measure._update_counted_cumulative(conn, max_sessions=50, deadline_seconds=None)["rows_written"] == 0
    assert conn.execute(history_sql).fetchall() == history
    for table, rows in ledgers.items():
        assert conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
    conn.close()
