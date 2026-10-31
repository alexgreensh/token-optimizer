"""Counted-cumulative ledger (2026-08-30): the "Counted to date" card.

Method under test: context removals only; per removal, prevented re-reads = the deduped
API turns from the event to the next REAL compact_boundary in its own actual
session, priced per-turn at that turn's model cache-read rate; one-shot floor
= the removal once at the first post-event call's input rate. resume_lean and
opportunity/unapplied rows are excluded. Events without a real transcript are
excluded rather than estimated. Computed in the collect pass and STORED in
trends.db (counted_reread); the dashboard summary only ever SUMs.

Run: python3 -m pytest tests/test_counted_cumulative.py -v
"""
import importlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
SID = "cafe0000-1111-2222-3333-444455556666"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._apply_sonnet_intro_pricing()
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _local_iso(dt_utc):
    """The ledger stamps naive LOCAL time; convert a UTC-naive dt to that."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()


def _transcript(m, tmp_path, monkeypatch, sid=SID, n_turns=8, compact_after=None,
                model="claude-opus-5", start=None):
    """n_turns parent calls 1 minute apart (each duplicated to prove requestId
    dedup), optional compact_boundary after turn index `compact_after`."""
    proj = tmp_path / "claude" / "projects" / "-Users-owner"
    proj.mkdir(parents=True, exist_ok=True)
    start = start or (_utc_now() - timedelta(hours=2))
    lines = []
    for i in range(n_turns):
        ts = _iso_z(start + timedelta(minutes=i))
        entry = {"type": "assistant", "timestamp": ts, "requestId": f"req-{i}",
                 "message": {"model": model, "usage": {"input_tokens": 2,
                     "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 10}}}
        lines.append(json.dumps(entry))
        lines.append(json.dumps(entry))  # duplicate content-block line: must dedup
        if compact_after is not None and i == compact_after:
            lines.append(json.dumps({"type": "system", "subtype": "compact_boundary",
                                     "timestamp": _iso_z(start + timedelta(minutes=i, seconds=30))}))
    p = proj / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    return p, start


def _db(m, tmp_path, monkeypatch):
    dbp = tmp_path / "trends.db"
    monkeypatch.setattr(m, "TRENDS_DB", dbp)
    conn = sqlite3.connect(str(dbp))
    conn.executescript(m._SCHEMA)
    conn.commit()
    monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(dbp)))
    return dbp


def _add_event(dbp, et, tokens, ts, sid=SID, cost=0.0, table="savings_events"):
    conn = sqlite3.connect(str(dbp))
    if table == "savings_events":
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,"
                     "cost_saved_usd,session_id,session_uuid) VALUES(?,?,?,?,?,?)",
                     (ts, et, tokens, cost, sid, sid))
    else:
        conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,"
                     "compressed_tokens,verified,tier,session_uuid) VALUES(?,?,?,0,1,'measured',?)",
                     (ts, et, tokens, sid))
    conn.commit()
    conn.close()


def _rates(m):
    return m.PRICING_TIERS[m._load_pricing_tier()]["claude_models"]


def _verified_pricing(m):
    """Explicit current cache metadata for rollup-only fixtures."""
    return json.dumps(dict.fromkeys(m._COUNTED_COVERAGE_KEYS, 0) | {
        "version": m._COUNTED_PRICING_VERSION, "priced_oneshot_events": 1,
        "priced_reread_turns": 1})


# ---------------------------------------------------------------------------
# 1. THE COUNT: deduped turns to the next REAL compaction, per-turn cache-read
# ---------------------------------------------------------------------------

def test_rereads_run_from_second_post_event_turn_to_session_end(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=8)
    # Event lands after turn 2 (UTC start+2.5min): post-event turns = 3..7 (5),
    # first is the one-shot, so 4 re-reads.
    ev_utc = start + timedelta(minutes=2, seconds=30)
    _add_event(dbp, "tool_archive", 100_000, _local_iso(ev_utc))

    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)
    row = conn.execute("SELECT tokens, oneshot_usd, reread_tokens, reread_usd,"
                       " turns_counted FROM counted_reread").fetchone()
    conn.close()
    opus = _rates(m)["opus"]
    assert row[0] == 100_000
    assert row[1] == pytest.approx(100_000 * opus["input"] / 1e6, rel=1e-9)
    assert row[4] == 4
    assert row[2] == 400_000
    assert row[3] == pytest.approx(400_000 * opus["cache_read"] / 1e6, rel=1e-9)


def test_rereads_stop_at_the_real_compact_boundary(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=8, compact_after=4)
    ev_utc = start + timedelta(minutes=1, seconds=30)  # post-event: turns 2,3,4 then compact
    _add_event(dbp, "tool_archive", 50_000, _local_iso(ev_utc))

    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)
    row = conn.execute("SELECT reread_tokens, turns_counted FROM counted_reread").fetchone()
    conn.close()
    assert row[1] == 2          # turns 3 and 4 (turn 2 is the one-shot)
    assert row[0] == 100_000


def test_reexpand_debits_net_out_as_negative_rows(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=6)
    ev = _local_iso(start + timedelta(minutes=0, seconds=30))
    _add_event(dbp, "tool_archive", 80_000, ev)
    _add_event(dbp, "tool_archive_reexpand", 30_000, ev)

    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)
    tok, rr = conn.execute("SELECT SUM(tokens), SUM(reread_tokens) FROM counted_reread").fetchone()
    conn.close()
    assert tok == 50_000           # 80K credit - 30K debit
    assert rr == 50_000 * 4        # both ride the same 4 re-read turns


def test_resume_lean_and_verbosity_and_unapplied_are_excluded(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _transcript(m, tmp_path, monkeypatch, n_turns=4)
    ts = _local_iso(_utc_now() - timedelta(hours=1))
    _add_event(dbp, "resume_lean", 1_000_000, ts)
    _add_event(dbp, "verbosity_steer_measured", 1_000_000, ts)
    _add_event(dbp, "verbosity_steer", 1_000_000, ts)
    # Estimated-MAGNITUDE removals: _get_savings_summary
    # relocates these to the estimated tier, so the counted card must not
    # compound them either -- one tier ruling per event class.
    _add_event(dbp, "mcp_cap", 1_000_000, ts)
    _add_event(dbp, "hint_followed", 1_000_000, ts)
    # opportunity-tier compression row (verified=0 via direct insert)
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,"
                 "compressed_tokens,verified,tier,session_uuid)"
                 " VALUES(?, 'first_read_skeleton', 999999, 0, 0, 'opportunity', ?)", (ts, SID))
    conn.commit()
    m._update_counted_cumulative(conn, quiet=True)
    n = conn.execute("SELECT COUNT(*) FROM counted_reread").fetchone()[0]
    conn.close()
    assert n == 0


def test_measured_compression_events_are_counted(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=5)
    _add_event(dbp, "bash_compress_pipeline", 40_000,
               _local_iso(start + timedelta(seconds=90)), table="compression_events")
    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)
    key, tok = conn.execute("SELECT event_key, tokens FROM counted_reread").fetchone()
    conn.close()
    assert key.startswith("ce:") and tok == 40_000


# ---------------------------------------------------------------------------
# 2. INCREMENTAL: unchanged transcripts are never re-walked
# ---------------------------------------------------------------------------

def test_unchanged_session_is_not_rewalked(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=5)
    _add_event(dbp, "tool_archive", 10_000, _local_iso(start + timedelta(seconds=30)))
    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)

    calls = {"n": 0}
    real = m._counted_walk_transcript
    monkeypatch.setattr(m, "_counted_walk_transcript",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or real(*a, **k)))
    st = m._update_counted_cumulative(conn, quiet=True)
    conn.close()
    assert calls["n"] == 0
    assert st["sessions_walked"] == 0


def test_grown_transcript_is_recomputed(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    p, start = _transcript(m, tmp_path, monkeypatch, n_turns=3)
    _add_event(dbp, "tool_archive", 10_000, _local_iso(start + timedelta(seconds=30)))
    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)
    rr1 = conn.execute("SELECT reread_tokens FROM counted_reread").fetchone()[0]

    # The session keeps going: 3 more turns appended, mtime bumped forward.
    extra = [json.dumps({"type": "assistant", "timestamp": _iso_z(start + timedelta(minutes=3 + i)),
                         "requestId": f"req-x{i}", "message": {"model": "claude-opus-5",
                         "usage": {"input_tokens": 2, "cache_read_input_tokens": 1,
                                   "cache_creation_input_tokens": 0}}}) for i in range(3)]
    with open(p, "a") as f:
        f.write("\n".join(extra) + "\n")
    future = (datetime.now() + timedelta(seconds=5)).timestamp()
    os.utime(p, (future, future))
    m._update_counted_cumulative(conn, quiet=True)
    rr2 = conn.execute("SELECT reread_tokens FROM counted_reread").fetchone()[0]
    conn.close()
    assert rr2 == rr1 + 3 * 10_000


# ---------------------------------------------------------------------------
# 3. SUMMARY: SELECT-only rollup for the card
# ---------------------------------------------------------------------------

def test_summary_sums_and_never_walks(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=8)
    _add_event(dbp, "tool_archive", 100_000, _local_iso(start + timedelta(minutes=2, seconds=30)))
    conn = sqlite3.connect(str(dbp))
    m._update_counted_cumulative(conn, quiet=True)
    conn.close()

    monkeypatch.setattr(m, "_counted_walk_transcript",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("summary walked a transcript")))
    s = m._counted_cumulative_summary()
    assert s["available"] is True
    assert s["events"] == 1
    assert s["total_usd"] == pytest.approx(s["oneshot_usd"] + s["reread_usd"], abs=0.011)
    assert s["removed_tokens"] == 100_000


def test_summary_unavailable_on_empty_ledger(m, tmp_path, monkeypatch):
    _db(m, tmp_path, monkeypatch)
    s = m._counted_cumulative_summary()
    assert s == {"available": False}


# ---------------------------------------------------------------------------
# 4. PRE-LEDGER ERA: marker backfill
# ---------------------------------------------------------------------------

def test_marker_backfill_counts_old_transcripts_once(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    start = datetime(2026, 3, 10, 12, 0, 0)
    p, _ = _transcript(m, tmp_path, monkeypatch, sid="0ld00000-1111-2222-3333-444455556666",
                       n_turns=6, start=start)
    # Inject a sized marker after turn 1 (a user line, old format has no size).
    lines = p.read_text().splitlines()
    marker = json.dumps({"type": "user", "timestamp": _iso_z(start + timedelta(seconds=80)),
                         "message": {"content": "[Full result archived (40,000 chars) - saved]"}})
    lines.insert(4, marker)  # after turn-1's two duplicate lines + turn-0 pair
    p.write_text("\n".join(lines) + "\n")
    old = datetime(2026, 3, 11).timestamp()
    os.utime(p, (old, old))

    conn = sqlite3.connect(str(dbp))
    st1 = m._counted_marker_backfill(conn, quiet=True)
    rows = conn.execute("SELECT event_key, tokens, event_month FROM counted_reread").fetchall()
    assert len(rows) == 1
    key, tok, month = rows[0]
    assert key.startswith("mk:") and tok == 10_000 and month == "2026-03"
    # Done flag set -> a second call scans nothing.
    st2 = m._counted_marker_backfill(conn, quiet=True)
    conn.close()
    assert st2["done"] is True and st2["files_scanned"] == 0


def test_marker_backfill_skips_ledger_era_files(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    p, _ = _transcript(m, tmp_path, monkeypatch, n_turns=3)  # recent mtime
    conn = sqlite3.connect(str(dbp))
    m._counted_marker_backfill(conn, quiet=True)
    n = conn.execute("SELECT COUNT(*) FROM counted_reread").fetchone()[0]
    conn.close()
    assert n == 0


def test_uuidless_compression_row_stays_out_without_a_transcript(m, tmp_path, monkeypatch):
    """A NULL-uuid row has no transcript evidence and must not be estimated."""
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")  # no transcripts
    ts = _local_iso(_utc_now() - timedelta(hours=1))
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,"
                 "compressed_tokens,verified,tier,model,session_uuid)"
                 " VALUES(?, 'bash_compress_pipeline', 50000, 10000, 1, 'measured', 'opus', NULL)",
                 (ts,))
    conn.commit()
    m._update_counted_cumulative(conn, quiet=True)
    row = conn.execute("SELECT COUNT(*) FROM counted_reread").fetchone()
    conn.close()
    assert row[0] == 0


def test_period_rollup_reconciles_to_lifetime_without_rewalking(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(str(dbp)) as conn:
        for key, age, one, rereads in [('old', 40, 2, 20), ('new', 2, 3, 30), ('debit', 1, -1, -10)]:
            ts = (now - timedelta(days=age)).isoformat()
            conn.execute('INSERT INTO counted_reread(event_key,event_ts,event_month,tokens,oneshot_usd,reread_usd,pricing_detail) VALUES(?,?,?,?,?,?,?)',
                         (key, ts, ts[:7], 100, one, rereads, _verified_pricing(m)))
    monkeypatch.setattr(m, '_counted_walk_transcript', lambda *a, **k: pytest.fail('rollup must not parse transcripts'))
    recent = m._counted_cumulative_summary(days=30, now=now)
    lifetime = m._counted_cumulative_summary()
    assert recent['available'] is True
    assert recent['oneshot_usd'] == 2
    assert recent['reread_usd'] == 20
    assert recent['total_usd'] == 22
    assert lifetime['total_usd'] == 44
    assert recent['attribution'] == 'removal_time'


def test_period_boundaries_offsets_and_future_events(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    start = now - timedelta(days=30)
    times = [start.isoformat(), (start - timedelta(seconds=1)).isoformat(),
             now.isoformat(), (now + timedelta(seconds=1)).isoformat(),
             start.astimezone(timezone(timedelta(hours=3))).isoformat(), 'invalid']
    with sqlite3.connect(str(dbp)) as conn:
        for i, ts in enumerate(times):
            conn.execute('INSERT INTO counted_reread(event_key,event_ts,event_month,oneshot_usd,reread_usd,pricing_detail) VALUES(?,?,?,?,?,?)',
                         (str(i), ts, ts[:7], 1, 10, _verified_pricing(m)))
    recent = m._counted_cumulative_summary(days=30, now=now)
    assert recent['events'] == 3
    assert recent['total_usd'] == 33


def test_empty_period_is_zero_not_lifetime_fallback(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat()
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute('INSERT INTO counted_reread(event_key,event_ts,event_month,oneshot_usd,reread_usd) VALUES(?,?,?,?,?)',
                     ('old', old, old[:7], 5, 50))
    recent = m._counted_cumulative_summary(days=30)
    assert recent['available'] is True
    assert recent['total_usd'] == 0
    assert recent['events'] == 0


@pytest.mark.parametrize('runtime', ['codex', 'hermes', 'copilot', 'cursor', 'antigravity', 'grok', 'opencode', 'openclaw'])
def test_period_has_no_invented_foreign_rereads(m, monkeypatch, runtime):
    monkeypatch.setattr(m, 'detect_runtime', lambda: runtime)
    assert m._counted_cumulative_summary(days=30) == {'available': False}


def test_delta_mirror_is_removed_without_deleting_source_telemetry(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=5)
    ts = _local_iso(start + timedelta(seconds=30))
    _add_event(dbp, 'delta_read', 1000, ts)
    _add_event(dbp, 'delta_read', 1000, ts, table='compression_events')
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute("INSERT INTO counted_reread(event_key, source, session_uuid, oneshot_usd, reread_usd) VALUES('ce:1','ce',?,99,999)", (SID,))
        conn.commit()
        result = m._update_counted_cumulative(conn)
        assert result['duplicate_rows_removed'] == 1
        assert conn.execute('SELECT event_key FROM counted_reread').fetchall() == [('se:1',)]
        assert conn.execute('SELECT COUNT(*) FROM compression_events').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM savings_events').fetchone()[0] == 1


def test_transcript_rotation_preserves_previously_verified_history(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, '_counted_session_path', lambda *a: None)
    with sqlite3.connect(str(dbp)) as conn:
        conn.executemany('INSERT INTO counted_reread(event_key,session_uuid,oneshot_usd,reread_usd,transcript_mtime) VALUES(?,?,?,?,?)',
                         [('verified', SID, 5, 50, 1234.0), ('never_verified', SID, 9, 90, None)])
        conn.commit()
        assert m._purge_counted_without_transcripts(conn) == 0
        assert conn.execute('SELECT event_key,oneshot_usd,reread_usd FROM counted_reread').fetchall() == [('verified', 5, 50), ('never_verified', 9, 90)]
    summary = m._counted_cumulative_summary()
    assert summary['legacy_unverified_events'] == 2
    assert summary['total_usd'] == 0


def test_transcript_rotation_preserves_marker_backfill_history(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, '_counted_session_path', lambda *a: None)
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute(
            "INSERT INTO counted_reread(event_key,source,session_uuid,oneshot_usd,"
            "reread_usd,transcript_mtime) VALUES('mk:old:0','mk',?,?,?,NULL)",
            (SID, 5, 50),
        )
        conn.commit()
        assert m._purge_counted_without_transcripts(conn) == 0
        assert conn.execute(
            'SELECT event_key,oneshot_usd,reread_usd FROM counted_reread'
        ).fetchall() == [('mk:old:0', 5, 50)]
