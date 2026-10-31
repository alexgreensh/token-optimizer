"""Unknown rates never become priced reread claims or erase stored evidence."""
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from test_counted_cumulative import m, _db, _transcript, _add_event, _local_iso, _utc_now
from test_savings_render import render


@pytest.mark.parametrize("change", ["unchanged", "replacement", "signed_rereads", "signed_removal"])
def test_legacy_refresh_requires_same_source_and_signed_counts(m, tmp_path, monkeypatch, change):
    db = _db(m, tmp_path, monkeypatch)
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=3, model="claude-opus-5-5")
    _add_event(db, "tool_archive", 1_000_000, _local_iso(start - timedelta(seconds=30)))
    with sqlite3.connect(db) as conn:
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
        conn.execute("UPDATE counted_reread SET pricing_detail=NULL,oneshot_usd=999,reread_usd=999")
        if change == "replacement":
            path.write_bytes(path.read_bytes().replace(b"claude-opus-5-5", b"claude-sonnet-4-5"))
            os.utime(path, (path.stat().st_mtime + 5, path.stat().st_mtime + 5))
        elif change == "signed_rereads":
            conn.execute("UPDATE counted_reread SET reread_tokens=-reread_tokens")
        elif change == "signed_removal":
            conn.execute("UPDATE counted_reread SET tokens=-tokens")
        conn.commit()
        result = m._update_counted_cumulative(conn)
        summary = m._counted_cumulative_summary()
        assert result["rows_written"] == (1 if change == "unchanged" else 0)
        if change == "unchanged":
            assert 0 < summary["total_usd"] < 999
            assert m._counted_pricing_detail(conn.execute("SELECT pricing_detail FROM counted_reread").fetchone()[0])["file_sha256"]
        else:
            assert summary["total_usd"] == 0
            assert summary["legacy_unverified_events"] == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("failure", ["replacement", "unreadable"])
def test_marker_reindex_cannot_admit_duplicate_and_unverifiable_drain_finishes(m, tmp_path, monkeypatch, legacy, failure):
    db = _db(m, tmp_path, monkeypatch)
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=5, start=datetime(2026, 3, 10, 12))
    marker = json.dumps({"type": "user", "message": {"content": "[Full result archived (40,000 chars) - saved]"}})
    lines = path.read_text().splitlines()
    lines.insert(4, marker)
    path.write_text("\n".join(lines) + "\n")
    old = datetime(2026, 3, 11).timestamp()
    os.utime(path, (old, old))
    with sqlite3.connect(db) as conn:
        assert m._counted_marker_backfill(conn)["rows_written"] == 1
        before = conn.execute("SELECT event_key,tokens,reread_tokens,oneshot_usd,reread_usd FROM counted_reread").fetchall()
        if legacy:
            conn.execute("UPDATE counted_reread SET pricing_detail=NULL,transcript_mtime=NULL")
            conn.commit()
        lines.insert(2, marker)
        path.write_text("\n".join(lines) + "\n")
        os.utime(path, (old + 5, old + 5))
        if failure == "unreadable":
            def unreadable(*args):
                raise PermissionError("fixture unreadable source")
            monkeypatch.setattr(m, "_counted_file_proof", unreadable)
        result = m._counted_marker_backfill(conn)
        assert result["done"] is True
        assert result["rows_written"] == 0
        assert conn.execute("SELECT event_key,tokens,reread_tokens,oneshot_usd,reread_usd FROM counted_reread").fetchall() == before
        second = m._counted_marker_backfill(conn)
        assert second["done"] is True
        assert second["files_scanned"] == second["retryable_rows"] == 0
        summary = m._counted_cumulative_summary()
        assert summary["refresh_incomplete_events"] == 1
        assert summary["legacy_unverified_events"] == int(legacy)


@pytest.mark.parametrize("model", [None, "future-model", "claude-opus-99", "claude-9-sonnet",
                                   "claude-opus-5-99", "gpt-5.4-future", "gemini-2.5-flash-future"])
def test_unknown_models_keep_tokens_without_fallback(m, model):
    start = _utc_now()
    turns = [(start + timedelta(minutes=i), model) for i in range(3)]
    actual = m._counted_score_event(turns, [], start - timedelta(seconds=1), 1_000_000,
                                    m.PRICING_TIERS["anthropic"])
    assert actual == (0, 2_000_000, 0, 2)


@pytest.mark.parametrize("model,card", [
    ("claude-opus-5-5", "opus_5_5"),
    ("anthropic/claude-opus-5-5-20260901", "opus_5_5"),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "sonnet_4_5"),
    ("claude-3-5-sonnet-20241022", "sonnet_legacy"),
])
def test_supported_claude_aliases_use_exact_card(m, model, card):
    start = _utc_now()
    tier = m.PRICING_TIERS["anthropic"]
    rates = tier["claude_models"][card]
    actual = m._counted_score_event([(start, model), (start + timedelta(minutes=1), model)],
                                    [], start - timedelta(seconds=1), 1_000_000, tier)
    assert actual == (rates["input"], 1_000_000, rates["cache_read"], 1)


def test_native_and_known_zero_cards_are_priced(m, monkeypatch):
    monkeypatch.setitem(m.OPENAI_MODEL_PRICING, "gpt-test-zero", {"input": 0, "cache_read": 0})
    start = _utc_now()
    score = m._counted_score_detail([(start, "openai/gpt-test-zero"),
                                    (start + timedelta(minutes=1), "gpt-5.4-2026-03-05", 1000)],
                                   [], start - timedelta(seconds=1), 1_000_000,
                                   m.PRICING_TIERS["anthropic"])
    assert score["oneshot_usd"] == 0
    assert score["reread_usd"] == m.OPENAI_MODEL_PRICING["gpt-5.4"]["cache_read"]
    assert score["pricing_complete"] is True


@pytest.mark.parametrize("model,base,long,threshold", [
    ("claude-haiku-5-5", "claude", "haiku_5_5", 100_000),
    ("gpt-5.4", "openai", "gpt-5.4", None),
    ("gemini-2.5-pro", "gemini", "gemini-2.5-pro", None),
])
def test_exact_prompt_class_uses_per_request_evidence(m, model, base, long, threshold):
    tier = m.PRICING_TIERS["anthropic"]
    tables = {"claude": (tier["claude_models"], tier["claude_models_lc"], m.ANTHROPIC_LONG_CONTEXT_INPUT_THRESHOLD),
              "openai": (m.OPENAI_MODEL_PRICING, m.OPENAI_LONG_CONTEXT_PRICING, m.OPENAI_LONG_CONTEXT_INPUT_THRESHOLD),
              "gemini": (m.GEMINI_MODEL_PRICING, m.GEMINI_LONG_CONTEXT_PRICING, m.GEMINI_LONG_CONTEXT_INPUT_THRESHOLD)}
    normal, extended, threshold = tables[base]
    start = _utc_now()
    score = m._counted_score_detail([(start, model, threshold),
        (start + timedelta(minutes=1), model, threshold + 1),
        (start + timedelta(minutes=2), model, None)], [], start - timedelta(seconds=1),
        1_000_000, tier)
    assert score["oneshot_usd"] == normal[long]["input"]
    assert score["reread_usd"] == extended[long]["cache_read"]
    assert score["reread_unpriced_tokens"] == 1_000_000


def test_walker_retains_full_prompt_class(m, tmp_path, monkeypatch):
    path, _ = _transcript(m, tmp_path, monkeypatch, model="claude-haiku-5-5")
    turns, _, _ = m._counted_walk_transcript(path)
    assert turns[0][2] == 1012


def test_malformed_prompt_usage_stays_unpriced_without_losing_turn(m, tmp_path, monkeypatch):
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=1, model="claude-haiku-5-5")
    line = json.loads(path.read_text().splitlines()[0])
    line["message"]["usage"] = ["partial"]
    path.write_text(json.dumps(line) + "\n")
    turns, compacts, _ = m._counted_walk_transcript(path)
    assert len(turns) == 1 and turns[0][2] is None
    score = m._counted_score_detail(turns, compacts, start - timedelta(seconds=1), 1000, m.PRICING_TIERS["anthropic"])
    assert score["oneshot_unpriced_tokens"] == 1000


def test_prompt_class_includes_streamed_ttl_split_cache_writes(m, tmp_path, monkeypatch):
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=1, model="claude-haiku-5-5")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    lines[0]["message"]["usage"] = {"input_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 60_000}}
    lines[1]["message"]["usage"] = {"cache_creation": {"ephemeral_5m_input_tokens": 40_000}}
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    turns, compacts, _ = m._counted_walk_transcript(path)
    assert len(turns) == 1 and turns[0][2] == 100_001
    assert m._counted_score_event(turns, compacts, start - timedelta(seconds=1), 1_000_000,
                                  m.PRICING_TIERS["anthropic"])[0] == 0.5


def test_streamed_prompt_size_crosses_threshold_without_duplicating_turn(m, tmp_path, monkeypatch):
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=2, model="claude-haiku-5-5")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    lines[0]["message"]["usage"] = {"input_tokens": 60_001}
    lines[1]["message"]["usage"] = {"cache_read_input_tokens": 40_000}
    lines[2]["message"]["usage"] = {"input_tokens": 2}
    lines[3]["message"]["usage"] = {"input_tokens": 2, "cache_read_input_tokens": 100_000}
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    turns, compacts, _ = m._counted_walk_transcript(path)
    assert len(turns) == 2
    assert [turn[2] for turn in turns] == [100_001, 100_002]
    actual = m._counted_score_event(turns, compacts, start - timedelta(seconds=1),
                                   1_000_000, m.PRICING_TIERS["anthropic"])
    assert actual == (0.5, 1_000_000, 0.05, 1)


def test_mixed_rates_and_signed_unknown_coverage_do_not_cancel(m):
    start = _utc_now()
    turns = [(start, None), (start + timedelta(minutes=1), "claude-opus-5-5"),
             (start + timedelta(minutes=2), "future-model")]
    plus = m._counted_score_detail(turns, [], start - timedelta(seconds=1), 1_000_000, m.PRICING_TIERS["anthropic"])
    minus = m._counted_score_detail(turns, [], start - timedelta(seconds=1), -1_000_000, m.PRICING_TIERS["anthropic"])
    assert plus["oneshot_usd"] == minus["oneshot_usd"] == 0
    assert plus["reread_usd"] == -minus["reread_usd"] > 0
    assert plus["oneshot_unpriced_credit_tokens"] == minus["oneshot_unpriced_debit_tokens"] == 1_000_000
    assert plus["reread_unpriced_credit_tokens"] == minus["reread_unpriced_debit_tokens"] == 1_000_000


def test_legacy_migration_quarantines_prices_before_refresh_and_preserves_history(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_counted_session_path", lambda *args: None)
    now = _local_iso(_utc_now() - timedelta(hours=1))
    with sqlite3.connect(db) as conn:
        for key, source, mtime in [("se:1", "se", 1234), ("mk:old:0", "mk", None), ("unknown", "se", None)]:
            conn.execute("INSERT INTO counted_reread(event_key,source,event_ts,event_month,tokens,"
                         "oneshot_usd,reread_tokens,reread_usd,turns_counted,transcript_mtime) "
                         "VALUES(?,?,?,?,1000000,5,2000000,1,2,?)", (key, source, now, now[:7], mtime))
        conn.commit()
        # Readers must quarantine old prices even before migration/collect runs.
        before = m._counted_cumulative_summary()
        assert before["total_usd"] == 0
        assert before["legacy_unverified_events"] == 3
        assert m._reprice_counted_rows_once(conn) is True
        assert m._reprice_counted_rows_once(conn) is False
        assert m._purge_counted_without_transcripts(conn) == 0
        assert conn.execute("SELECT COUNT(*),SUM(tokens),SUM(reread_tokens) FROM counted_reread").fetchone() == (3, 3_000_000, 6_000_000)
        assert conn.execute("SELECT transcript_mtime FROM counted_reread WHERE event_key='se:1'").fetchone() == (1234,)
    summary = m._counted_cumulative_summary()
    assert summary["reread_unpriced_tokens"] == 6_000_000
    assert summary["pricing_complete"] is False
    assert m._action_savings_components({}, summary)["total_usd"] == 0


@pytest.mark.parametrize("model", [None, "claude-opus-5-5", "mixed"])
def test_new_cache_survives_missing_empty_shorter_and_replaced_transcript(m, tmp_path, monkeypatch, model):
    db = _db(m, tmp_path, monkeypatch)
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=5, model="claude-opus-5-5" if model == "mixed" else model)
    if model == "mixed":
        lines = path.read_text().splitlines()
        lines[-2:] = [line.replace("claude-opus-5-5", "future-model") for line in lines[-2:]]
        path.write_text("\n".join(lines) + "\n")
    original = path.read_bytes()
    _add_event(db, "tool_archive", 1_000_000, _local_iso(start + timedelta(seconds=30)))
    with sqlite3.connect(db) as conn:
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
        first = m._counted_cumulative_summary()
        if model == "mixed":
            assert first["reread_usd"] > 0 and first["reread_unpriced_tokens"] == 1_000_000
        for contents in (b"", original.splitlines(keepends=True)[0], original.replace(b'"input_tokens": 2', b'"input_tokens": 3')):
            path.write_bytes(contents)
            bump = path.stat().st_mtime + 5
            os.utime(path, (bump, bump))
            m._update_counted_cumulative(conn)
            current = m._counted_cumulative_summary()
            for key in ("removed_tokens", "reread_tokens", "oneshot_usd", "reread_usd", "reread_unpriced_tokens"):
                assert current[key] == first[key]
            assert current["refresh_incomplete_events"] == 1
        path.write_bytes(original)
        os.utime(path, (bump + 10, bump + 10))
        m._update_counted_cumulative(conn)
        assert m._counted_cumulative_summary()["refresh_incomplete_events"] == 0
        monkeypatch.setattr(m, "_counted_session_path", lambda *args: None)
        m._update_counted_cumulative(conn)
        assert m._purge_counted_without_transcripts(conn) == 0
        last = m._counted_cumulative_summary()
        assert last["reread_usd"] == first["reread_usd"]
        assert last["reread_tokens"] == first["reread_tokens"]


def test_signed_unknown_cancellation_remains_visible_in_monthly_coverage(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=3, model=None)
    stamp = _local_iso(start - timedelta(seconds=30))
    _add_event(db, "tool_archive", 1_000_000, stamp)
    _add_event(db, "tool_archive_reexpand", 1_000_000, stamp)
    with sqlite3.connect(db) as conn:
        assert m._update_counted_cumulative(conn)["rows_written"] == 2
        assert conn.execute("SELECT COUNT(*), SUM(tokens_saved) FROM savings_events").fetchone() == (2, 2_000_000)
    summary = m._counted_cumulative_summary()
    for result in (summary, summary["monthly_coverage"][stamp[:7]]):
        assert result["removed_tokens"] == result["reread_tokens"] == result["total_usd"] == 0
        assert result["oneshot_unpriced_tokens"] == 2_000_000
        assert result["reread_unpriced_credit_tokens"] == result["reread_unpriced_debit_tokens"] == 2_000_000
        assert result["pricing_complete"] is False


def test_append_with_preserved_mtime_refreshes_using_size_and_prefix(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    path, start = _transcript(m, tmp_path, monkeypatch, n_turns=3)
    stamp = path.stat().st_mtime
    _add_event(db, "tool_archive", 1000, _local_iso(start - timedelta(seconds=30)))
    with sqlite3.connect(db) as conn:
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
        _transcript(m, tmp_path, monkeypatch, n_turns=4, start=start)
        os.utime(path, (stamp, stamp))
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
        assert conn.execute("SELECT reread_tokens FROM counted_reread").fetchone() == (3000,)


def test_unknown_one_shot_only_is_retained_in_window_and_cache(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=1, model=None)
    _add_event(db, "tool_archive", 1000, _local_iso(start - timedelta(seconds=30)))
    with sqlite3.connect(db) as conn:
        summary = m._counted_window_summary(conn, start - timedelta(minutes=1), start + timedelta(minutes=1))
        assert summary["events"] == 1
        assert summary["oneshot_unpriced_tokens"] == 1000
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
    assert m._counted_cumulative_summary()["oneshot_unpriced_tokens"] == 1000


def test_unknown_prices_disclosed_in_cli_json_and_dashboard(m, tmp_path, monkeypatch, capsys):
    db = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=3, model=None)
    _add_event(db, "tool_archive", 1_000_000, _local_iso(start - timedelta(seconds=30)))
    with sqlite3.connect(db) as conn:
        m._update_counted_cumulative(conn)
    monkeypatch.setattr(m, "_get_merged_savings", lambda **kw: {"period_days": 30, "total_cost_usd": 2})
    monkeypatch.setattr(m, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(m, "_install_date", lambda: None)
    m.savings_report()
    text = capsys.readouterr().out
    assert "unpriced" in text.lower()
    assert "2,000,000" in text
    m.savings_report(as_json=True)
    cli = json.loads(capsys.readouterr().out)
    dashboard = m._dashboard_savings_data()
    assert cli["action_savings"] == dashboard["action_savings"]
    assert cli["action_savings"]["reread_pricing_complete"] is False
    assert cli["action_savings"]["total_usd"] == 2
    html = render(dashboard)
    assert "unpriced" in html.lower()
    assert "Context savings" in html


@pytest.mark.parametrize("known_zero", [False, True])
def test_unpriced_reread_label_is_distinct_from_verified_zero(m, monkeypatch, known_zero):
    monkeypatch.setitem(m.OPENAI_MODEL_PRICING, "gpt-test-zero", {"input": 0, "cache_read": 0})
    start = _utc_now()
    model = "gpt-test-zero" if known_zero else None
    counted = m._counted_score_detail([(start, model), (start + timedelta(minutes=1), model)],
        [], start - timedelta(seconds=1), 1000, m.PRICING_TIERS["anthropic"])
    counted["available"] = True
    summary = {"period_days": 30, "total_cost_usd": 2, "counted_period": counted}
    summary["action_savings"] = m._action_savings_components(summary, counted)
    html = render(summary)
    assert ("Repeat reads avoided <strong>~$0" if known_zero else
            "Repeat reads avoided <strong>Unpriced") in html


def test_weekly_plan_payload_carries_partial_reread_coverage(m, tmp_path, monkeypatch):
    from test_runway_resume_lean_addback import _wire
    _db(m, tmp_path, monkeypatch)
    _wire(m, monkeypatch, resume_lean_usd=0)
    monkeypatch.setattr(m, "_counted_window_summary", lambda *args: {
        "available": True, "reread_usd": 0, "reread_pricing_complete": False,
        "reread_unpriced_tokens": 1000, "reread_pricing_note": "Repeat-read tokens unpriced."})
    result = m.runway_snapshot()
    assert result["counted_pricing"] == {
        "reread_pricing_complete": False, "reread_unpriced_tokens": 1000,
        "reread_pricing_note": "Repeat-read tokens unpriced."}
