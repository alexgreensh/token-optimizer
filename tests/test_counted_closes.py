"""Stable savings ledgers, using only disposable fixture databases."""
import json
import random
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import measure
from _measure import counted_closes as closes, counted_headline as headline, savings_headline


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db = tmp_path / "trends.db"
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(measure._SCHEMA)
        yield conn


def add(conn, key, day, first, repeat=0, *, version=None, session=None):
    detail = dict.fromkeys(measure._COUNTED_COVERAGE_KEYS, 0)
    detail.update(version=version or measure._COUNTED_PRICING_VERSION,
                  priced_oneshot_events=1, priced_reread_turns=int(bool(repeat)))
    with conn:
        conn.execute(
            "INSERT INTO counted_reread(event_key, source, session_uuid, event_ts, event_month, "
            "tokens, reread_tokens, oneshot_usd, reread_usd, pricing_detail) "
            "VALUES(?, 'mk', ?, ?, ?, 2000, ?, ?, ?, ?)",
            (key, session, day + "T10:31:00Z", day[:7], int(repeat * 2000),
             first, repeat, json.dumps(detail)))


def test_daily_sum_uses_the_lifetime_selection(ledger):
    add(ledger, "mk:a", "2026-03-24", 15.125, 22.75)
    add(ledger, "mk:b", "2026-03-25", 3.25, 5.125)
    with ledger:
        ledger.execute("INSERT INTO savings_events(id, timestamp, event_type, tokens_saved) "
                       "VALUES(701, '2026-03-25T10:31:00', 'checkpoint_restore', 12345)")
        add(ledger, "se:701", "2026-03-25", 1000, 500)
        ledger.execute("UPDATE counted_reread SET source='se' WHERE event_key='se:701'")
    days = closes.per_day(measure, ledger)
    expected = measure._counted_cumulative_summary()
    assert days == {"2026-03-24": (37.875, 47500), "2026-03-25": (8.375, 12250)}
    assert round(sum(usd for usd, _ in days.values()), 2) == round(expected["total_usd"], 2)
    assert sum(tokens for _, tokens in days.values()) == expected["removed_tokens"] + expected["reread_tokens"]


def test_roll_off_and_later_repeat_reads(ledger):
    now = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2026-03-24", 50, 10)
    before = closes.refresh(measure, ledger, now)
    assert before["usd"] == 60
    assert closes.refresh(measure, ledger, now + timedelta(days=40)) == before
    with ledger:
        ledger.execute("UPDATE counted_reread SET reread_usd=20, reread_tokens=40000")
    after = closes.refresh(measure, ledger, now + timedelta(days=41))
    assert after == {"usd": 70, "tokens": 42000, "since": "2026-03-24",
                     "held_days": 0, "corrections_written": 0}


def test_lower_day_is_held_then_recounted(ledger):
    today = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2026-03-24", 100, 40)
    closes.refresh(measure, ledger, today - timedelta(days=8))
    with ledger:
        ledger.execute("UPDATE counted_reread SET reread_usd=9")
    closes.refresh(measure, ledger, today - timedelta(days=7))
    held = headline.build(measure, ledger)
    assert held["usd"] == 140 and held["rechecking"] and held["last_correction"] is None
    assert closes.refresh(measure, ledger, today - timedelta(days=1))["usd"] == 140
    assert closes.refresh(measure, ledger, today)["corrections_written"] == 1
    h = headline.build(measure, ledger)
    assert h["usd"] == 109 and not h["rechecking"]
    assert h["last_correction"]["delta_usd"] == -31
    assert h["display"]["short"] == "Saved $109 since Mar 2026 (corrected -$31)"
    assert ledger.execute("SELECT reason_code, from_usd, to_usd FROM counted_corrections").fetchall() == [
        ("recount", 140, 109)]


def test_repricing_waits_for_rescore(ledger, monkeypatch):
    now = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2026-03-24", 50, session="test-session")
    closes.refresh(measure, ledger, now)
    monkeypatch.setattr(measure, "_COUNTED_PRICING_VERSION", "new")
    assert measure._counted_cumulative_summary()["remeasuring"]
    held = closes.refresh(measure, ledger, now)
    assert held["usd"] == 50 and held["held_days"] == 1 and held["corrections_written"] == 0
    with ledger:
        ledger.execute("DELETE FROM counted_reread")
    add(ledger, "mk:a", "2026-03-24", 30, session="test-session")
    assert not measure._counted_cumulative_summary()["remeasuring"]
    corrected = closes.refresh(measure, ledger, now)
    assert corrected["usd"] == 30 and corrected["corrections_written"] == 1
    assert ledger.execute("SELECT reason_code FROM counted_corrections").fetchone() == ("repricing",)


def test_pruning_keeps_closed_days_and_corrections(ledger, monkeypatch):
    now = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2020-03-24", 50, session="old-session")
    closes.refresh(measure, ledger, now)
    closes.register_correction(ledger, "2020-03-24", 40, "manual", "Verified removal size.")
    monkeypatch.setattr(measure, "_TRENDS_RETENTION_DAYS", 1)
    measure._prune_trends_db()
    assert ledger.execute("SELECT COUNT(*) FROM counted_reread").fetchone() == (0,)
    assert ledger.execute("SELECT COUNT(*) FROM counted_corrections").fetchone() == (1,)
    for days in (0, 8, 40):
        result = closes.refresh(measure, ledger, now + timedelta(days=days))
        assert result == {"usd": 40, "tokens": 2000, "since": "2020-03-24",
                          "held_days": 0, "corrections_written": 0}
    assert ledger.execute("SELECT reason_code FROM counted_corrections").fetchall() == [("manual",)]


def test_missing_day_clears_an_old_hold_without_repricing(ledger, monkeypatch):
    now = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2026-03-24", 50)
    closes.refresh(measure, ledger, now)
    with ledger:
        ledger.execute("UPDATE counted_reread SET oneshot_usd=25")
    assert closes.refresh(measure, ledger, now)["held_days"] == 1
    with ledger:
        ledger.execute("DELETE FROM counted_reread")
    monkeypatch.setattr(measure, "_COUNTED_PRICING_VERSION", "new")
    result = closes.refresh(measure, ledger, now + timedelta(days=8))
    assert (result["usd"], result["held_days"], result["corrections_written"]) == (50, 0, 0)
    add(ledger, "mk:a", "2026-03-24", 35)
    assert closes.refresh(measure, ledger, now + timedelta(days=9))["usd"] == 35
    assert ledger.execute("SELECT reason_code FROM counted_corrections").fetchall() == [("repricing",)]


def test_readonly_and_timeout_cache(ledger):
    add(ledger, "mk:a", "2026-03-24", 25, 15)
    closes.refresh(measure, ledger)
    with closing(sqlite3.connect(measure.TRENDS_DB.as_uri() + "?mode=ro", uri=True)) as readonly:
        assert headline.build(measure, readonly)["usd"] == 40
    measure._status_bar_write_cache("fixture-session", None, "timeout")
    payload = json.loads(measure._status_bar_cache_path("fixture-session").read_text())
    assert payload["savings"] is None
    assert payload["counted_headline"]["display"]["long"] == "$40 counted since Mar 24, 2026"


def test_rolling_display_is_labeled_and_counts_one_routing_estimate():
    payload = savings_headline.build({
        "action_savings": {"total_usd": 80, "routing_usd": 70},
        "model_routing": {"realized_cost_usd": 90},
    })
    assert payload["usd_30d"] == 170
    assert "30 days" in payload["label"] and "30 days" in payload["display"]["short"]


def test_all_in_snapshot_is_shared_and_only_falls_with_a_correction(ledger, monkeypatch):
    now = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2026-03-24", 30)
    summary = {"since_install": {"install_date": "2026-03-24", "total_usd": 170,
                                "all_in": {"total_usd": 170}}}
    closes.on_savings_refresh(measure, summary)
    assert summary["counted_headline"]["usd"] == 30
    assert summary["overall_headline"]["usd"] == 170
    assert headline.cache_value(measure, "fixture-session") == summary["overall_headline"]
    summary["since_install"]["all_in"]["total_usd"] = 140
    closes.refresh(measure, ledger, now, since_install=summary["since_install"])
    assert headline.build_overall(measure)["usd"] == 170
    closes.refresh(measure, ledger, now + timedelta(days=7), since_install=summary["since_install"])
    assert headline.cache_value(measure, "fixture-session")["usd"] == 140
    assert closes.total_overall(ledger)["last_correction"]["delta_usd"] == -30
    assert closes.total(ledger)["usd"] == 30


@pytest.mark.parametrize("source", ["keepwarm", "autocompact"])
def test_all_in_books_new_net_cost_without_waiting(ledger, monkeypatch, tmp_path, source):
    now = datetime.now(timezone.utc)
    install = (now - timedelta(days=90)).date().isoformat()
    monkeypatch.setattr(measure, "_install_date", lambda: install)
    ping_log = tmp_path / "keepwarm-spend.jsonl"
    monkeypatch.setattr(measure, "_keepwarm_ledger_path", lambda: ping_log)
    with ledger:
        ledger.execute("ALTER TABLE savings_events ADD COLUMN pause_key TEXT")
        ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd) "
                       "VALUES (?, 'tool_archive', 40000, 50.0)", (now.isoformat(),))
    before = measure._savings_since_install()
    assert before["all_in"]["total_usd"] == 50
    closes.refresh(measure, ledger, now, since_install=before)
    if source == "keepwarm":
        ping_log.write_text(json.dumps({"status": "ok", "ts": now.timestamp(), "cost_usd": 8.50,
                                        "session_id": "warm-session", "proof": False}) + "\n")
    else:
        with ledger:
            ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd) "
                           "VALUES (?, 'autocompact_window_estimated', -12000, -8.50)", (now.isoformat(),))
    after = measure._savings_since_install()
    assert after["all_in"]["total_usd"] == 41.50

    for day in (1, 2, 9):
        closes.refresh(measure, ledger, now + timedelta(days=day), since_install=after)
        balance = closes.total_overall(ledger)
        assert (balance["usd"], balance["held_days"]) == (41.50, 0)
        assert balance["last_correction"]["delta_usd"] == -8.50
    assert ledger.execute("SELECT reason_code, from_usd, to_usd FROM overall_corrections").fetchall() == [
        ("net_debit", 50, 41.50)]


def test_all_in_keeps_verified_lean_saving_after_transcript_cleanup(ledger, monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    event = (now - timedelta(days=45)).replace(microsecond=0)
    install = (now - timedelta(days=90)).date().isoformat()
    monkeypatch.setattr(measure, "_install_date", lambda: install)
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "claude")
    project = measure.CLAUDE_DIR / "projects" / "-fixture"
    project.mkdir(parents=True)
    target = "abcd1234-1111-2222-3333-444455556666"
    receiver = "efab5678-1111-2222-3333-444455556666"

    def turn(seconds, request):
        return {"type": "assistant", "timestamp": (event + timedelta(seconds=seconds)).isoformat(),
                "requestId": request, "message": {"model": "claude-opus-5", "usage": {
                    "input_tokens": 2, "cache_read_input_tokens": 50000,
                    "cache_creation_input_tokens": 100, "output_tokens": 10}}}

    old, new = (project / f"{sid}.jsonl" for sid in (target, receiver))
    old.write_text(json.dumps(turn(-600, "old-request")) + "\n")
    delivery = {"type": "attachment", "timestamp": event.isoformat(), "attachment": {
        "type": "hook_additional_context", "content": "[Token Optimizer] Cold-resume lean (session abcd1234, restored)"}}
    new.write_text("\n".join(map(json.dumps, (delivery, turn(5, "new-request")))) + "\n")
    with ledger:
        ledger.executemany("INSERT INTO session_log(session_uuid, jsonl_path, date) VALUES (?, ?, ?)",
                           ((sid, str(path), event.date().isoformat()) for sid, path in ((target, old), (receiver, new))))
        ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd) "
                       "VALUES (?, 'tool_archive', 40000, 50.0)", (now.isoformat(),))
        ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, detail, session_id, session_uuid) "
                       "VALUES (?, 'resume_lean', 40000, ?, ?, ?)",
                       (event.isoformat(), f"warm cache-read receiver={receiver}", target, target))
    before = measure._savings_since_install()
    assert before["all_in"]["estimated_usd"] > 0
    closes.refresh(measure, ledger, now, since_install=before)

    new.unlink()
    after = measure._savings_since_install()
    assert after["all_in"]["estimated_usd"] == 0
    for days in (1, 9, 40):
        closes.refresh(measure, ledger, now + timedelta(days=days), since_install=after)
        assert closes.total_overall(ledger)["usd"] == before["all_in"]["total_usd"]
    assert ledger.execute("SELECT COUNT(*) FROM overall_corrections").fetchone() == (0,)

    with ledger:
        ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd) "
                       "VALUES (?, 'autocompact_window_estimated', -12000, -8.50)", (now.isoformat(),))
    closes.refresh(measure, ledger, now + timedelta(days=41), since_install=measure._savings_since_install())
    assert closes.total_overall(ledger)["usd"] == pytest.approx(before["all_in"]["total_usd"] - 8.50)
    assert ledger.execute("SELECT reason_code FROM overall_corrections").fetchall() == [("net_debit",)]


def test_net_debit_does_not_release_an_unexplained_decrease(ledger):
    now = datetime.now(timezone.utc)
    install = (now - timedelta(days=90)).date().isoformat()
    summary = {"install_date": install, "all_in": {"total_usd": 50}}
    closes.refresh(measure, ledger, now, since_install=summary)
    with ledger:
        ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd) "
                       "VALUES (?, 'autocompact_window_estimated', -12000, -8.5)", (now.isoformat(),))
    summary["all_in"]["total_usd"] = 34.50
    closes.refresh(measure, ledger, now + timedelta(days=1), since_install=summary)
    balance = closes.total_overall(ledger)
    assert (balance["usd"], balance["held_days"]) == (41.50, 1)
    closes.refresh(measure, ledger, now + timedelta(days=8), since_install=summary)
    assert closes.total_overall(ledger)["usd"] == 34.50
    assert ledger.execute("SELECT reason_code, from_usd, to_usd FROM overall_corrections ORDER BY id").fetchall() == [
        ("net_debit", 50, 41.50), ("recount", 41.50, 34.50)]


def test_subcent_costs_apply_when_the_running_cost_reaches_a_cent(ledger):
    now = datetime.now(timezone.utc)
    install = (now - timedelta(days=90)).date().isoformat()
    summary = {"install_date": install, "all_in": {"total_usd": 50}}
    closes.refresh(measure, ledger, now, since_install=summary)
    for day in (1, 2):
        stamp = (now + timedelta(days=day)).isoformat()
        with ledger:
            ledger.execute("INSERT INTO savings_events(timestamp, event_type, tokens_saved, cost_saved_usd) "
                           "VALUES (?, 'autocompact_window_estimated', -100, -0.003)", (stamp,))
        summary["all_in"]["total_usd"] = round(50 - day * 0.003, 2)
        closes.refresh(measure, ledger, now + timedelta(days=day), since_install=summary)
    assert closes.total_overall(ledger)["usd"] == 49.99
    assert ledger.execute("SELECT reason_code FROM overall_corrections").fetchall() == [("net_debit",)]


def test_200_sequences_fall_only_by_written_corrections(ledger):
    rng = random.Random(903)
    assert closes.refresh(measure, ledger)["usd"] == 0
    for sequence in range(200):
        with ledger:
            for table in ("counted_reread", "counted_day_close", "counted_corrections", "token_optimizer_meta"):
                ledger.execute("DELETE FROM " + table)
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        previous = last_id = 0
        for _ in range(12):
            day = f"2026-03-{rng.randint(1, 5):02d}"
            key = "mk:" + day
            action = rng.randrange(5)
            if action == 0:
                add(ledger, f"{key}:{rng.getrandbits(64)}", day, rng.randint(1, 50))
            elif action in (1, 2):
                with ledger:
                    ledger.execute("UPDATE counted_reread SET oneshot_usd=oneshot_usd*? WHERE event_ts LIKE ?",
                                   (0.5 if action == 1 else 1.5, day + "%"))
            elif action == 3:
                with ledger:
                    ledger.execute("DELETE FROM counted_reread WHERE event_ts LIKE ?", (day + "%",))
            else:
                now += timedelta(days=rng.randint(1, 10))
            result = closes.refresh(measure, ledger, now)
            corrections = ledger.execute(
                "SELECT id, from_usd-to_usd FROM counted_corrections WHERE id > ? ORDER BY id",
                (last_id,)).fetchall()
            assert len(corrections) == result["corrections_written"], sequence
            assert result["usd"] >= previous - sum(delta for _, delta in corrections) - 1e-8, sequence
            if corrections:
                last_id = corrections[-1][0]
            previous = result["usd"]


def test_new_net_debit_has_a_correction_even_on_its_first_close(ledger):
    add(ledger, "mk:a", "2026-03-24", 50)
    assert closes.refresh(measure, ledger)["usd"] == 50
    add(ledger, "mk:b", "2026-03-25", -12)
    result = closes.refresh(measure, ledger)
    assert result["usd"] == 38 and result["corrections_written"] == 1
    assert ledger.execute("SELECT reason_code, from_usd, to_usd FROM counted_corrections").fetchone() == (
        "net_debit", 0, -12)


def test_counted_command_is_read_only_and_lists_recent_corrections(ledger, capsys):
    add(ledger, "mk:a", "2026-03-24", 25, 15)
    closes.on_savings_refresh(measure, {
        "period_days": 30, "action_savings": {"total_usd": 26},
        "since_install": {"install_date": "2026-03-24",
                          "all_in": {"total_usd": 65, "estimated_usd": 10}},
    })
    closes.register_correction(ledger, "2026-03-24", 34, "manual", "Verified the original request.")
    stamp = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    with ledger:
        ledger.execute("INSERT INTO counted_corrections(day, applied_at, from_usd, to_usd, reason_code, reason_text) "
                       "VALUES('2026-03-24', ?, 44, 40, 'manual', 'Old correction.')", (stamp,))
    contents = measure.TRENDS_DB.read_bytes()
    assert headline.main(measure, []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "$34 counted since Mar 24, 2026. Verified the original request.",
        "Estimated on top (can move): $10.00",
        "Last 30 days (moves as days roll off): $26.00",
        f"{datetime.now(timezone.utc).date()}  -$6.00  Verified the original request.",
    ]
    assert headline.main(measure, ["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["usd"] == 34 and len(payload["corrections"]) == 1
    assert measure.TRENDS_DB.read_bytes() == contents


def test_overall_headline_is_first_in_report_and_survives_status_timeout(ledger, monkeypatch, capsys):
    add(ledger, "mk:a", "2026-03-24", 20, 10)
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {
        "install_date": "2026-03-24", "days": 240,
        "all_in": {"total_usd": 170, "estimated_usd": 40,
                   "components_usd": {"logged": 100, "routing_estimated": 30}},
    })
    summary = measure._dashboard_savings_data(days=30)
    assert summary["overall_headline"]["usd"] == 170
    assert summary["counted_headline"]["usd"] == 30

    measure.savings_report(days=30)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "$170 saved since Mar 24, 2026"
    assert "Last 30 days" in lines[2]
    assert sum("since Mar 24, 2026" in line for line in lines) == 1

    measure._status_bar_write_cache("fixture-session", None, "timeout")
    payload = json.loads(measure._status_bar_cache_path("fixture-session").read_text())
    assert payload["savings"] is None
    assert payload["counted_headline"] == summary["overall_headline"]

    def check_rolling_labels(value):
        if isinstance(value, dict):
            label = " ".join(str(value.get(key, "")) for key in ("label", "short", "long")).lower()
            if {"total_30d_usd", "usd_30d"} & value.keys() and "saved" in label:
                assert "30 days" in label
            for member in value.values():
                check_rolling_labels(member)

    check_rolling_labels(payload)
    check_rolling_labels(summary)


@pytest.mark.parametrize(("usd", "events", "alarm"), [(89, 90, True), (90, 90, False), (89, 89, False)])
def test_tripwire_uses_ten_percent_drop_and_ninety_percent_activity(ledger, usd, events, alarm):
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
    history = measure.SNAPSHOT_DIR / measure._HEADLINE_HISTORY_FILE
    history.write_text(json.dumps({"d30": [{"date": yesterday, "usd": 100, "events": 100}]}))
    assert bool(measure._headline_tripwire(usd, events, 30)) is alarm


def test_failed_correction_rolls_back_the_whole_refresh(ledger):
    today = datetime.now(timezone.utc)
    add(ledger, "mk:a", "2026-03-24", 50)
    add(ledger, "mk:b", "2026-03-25", 50)
    closes.refresh(measure, ledger, today)
    with ledger:
        ledger.execute("UPDATE counted_reread SET oneshot_usd=oneshot_usd/2")
    closes.refresh(measure, ledger, today)
    with ledger:
        ledger.execute("""CREATE TRIGGER reject_second_correction BEFORE INSERT ON counted_corrections
            WHEN NEW.day = '2026-03-25' BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
    after = closes.refresh(measure, ledger, today + timedelta(days=7))
    assert after["usd"] == 100 and after["held_days"] == 2 and after["corrections_written"] == 0
    assert ledger.execute("SELECT COUNT(*) FROM counted_corrections").fetchone() == (0,)


def test_stored_lifetime_total_leads_and_correction_is_visible():
    from test_savings_story_render import _visible, hero, render_savings, routed

    s = routed()
    s['overall_headline'] = {
        'usd': 3812, 'since': '2025-11-01', 'rechecking': True,
        'last_correction': {'date': '2026-11-19', 'delta_usd': -22,
                            'reason': 'A recount adjusted the earlier savings total.'},
        'display': {'long': '$3,812 saved since Nov 1, 2025'},
    }
    s['counted_headline'] = {'usd': 310, 'since': '2025-11-01', 'last_correction': None}
    s['before_after'] = {'transformation_state': 'ok', 'monthly_savings_usd': 300,
                         'actual_monthly_usd': 1000, 'counterfactual_monthly_usd': 1300}
    html = render_savings(s, extra={'runway': {'windows': []}})
    h = hero(html)
    vis = _visible(h)
    assert vis.index('$3,812') < vis.index('$145')
    assert 'Last 30 days (moves as days roll off)' in vis
    assert 'A recount adjusted the earlier savings total (Nov 19, -$22.00).' in vis
    assert '$3,834' not in html and 'Counted since' not in vis and '$310.00' not in vis
    assert 'Counted since Nov 1, 2025 $310.00' in h.split('<details')[1]
    assert 'Could save (not saved yet)' in html
    assert '$3,812 saved since Nov 1, 2025' in html.split('st-limits-card')[1]
    s.pop('session_effect')
    fallback = render_savings(s)
    assert '$3,812' in fallback and 'saved since you installed' in fallback
