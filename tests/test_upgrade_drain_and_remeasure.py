"""After an upgrade the history is re-scored once, and the numbers that are
still being re-scored say so instead of moving pass to pass.

* One detached drain per counted-pricing and usage-parser version, uncapped.
* A user-run collect drains inline within a wall-clock budget.
* The counted walk commits per session, so hook collects are never locked out.
* The all-time card and model routing report "re-measuring (N sessions left)".
* A screenshot archive counts at its image token cost, only when its
  transcript shows the image left context.
"""
import json
import os
import sqlite3
import subprocess
from datetime import timedelta

import pytest

from test_counted_cumulative import m, _db, _transcript, _add_event, _local_iso, SID
from test_savings_render import render
from test_savings_card_young_install import measure  # noqa: F401  (fixture)
from test_savings_display_cents import patch as patch_savings, snapshot
from test_usage_counted_once import _load, _noon, _write, _streamed, _projects


# ---- per-session commit -----------------------------------------------------

def test_counted_walk_commits_each_session(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    sids = ["cafe0000-1111-2222-3333-44445555000%d" % i for i in range(3)]
    for sid in sids:
        _, start = _transcript(m, tmp_path, monkeypatch, sid=sid, n_turns=3, model="claude-opus-5-5")
        _add_event(db, "tool_archive", 1000, _local_iso(start - timedelta(seconds=30)), sid=sid)
    real_walk = m._counted_walk_transcript
    writes = []

    def walk(path, *a, **k):
        # While the walk is under way, another process must be able to write.
        other = sqlite3.connect(str(db), timeout=0.05)
        try:
            other.execute("CREATE TABLE IF NOT EXISTS probe (n INTEGER)")
            other.execute("INSERT INTO probe VALUES (1)")
            other.commit()
            writes.append(True)
        except sqlite3.OperationalError:
            writes.append(False)
        finally:
            other.close()
        return real_walk(path, *a, **k)

    monkeypatch.setattr(m, "_counted_walk_transcript", walk)
    with sqlite3.connect(str(db)) as conn:
        stats = m._update_counted_cumulative(conn, max_sessions=10)
    assert stats["sessions_walked"] == 3
    assert writes == [True, True, True]


# ---- detached drain -----------------------------------------------------------

def _meta(db, key):
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT value FROM token_optimizer_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def test_backlog_spawns_one_drain_per_version(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setenv("TOKEN_OPTIMIZER_TEST_ALLOW_DRAIN_SPAWN", "1")
    spawned = []
    monkeypatch.setattr(m, "_spawn_upgrade_drain", lambda: spawned.append(1) or True)
    with sqlite3.connect(str(db)) as conn:
        assert m._upgrade_drain_after_pass(conn, backlog=True) is True
        # The lock is held by the running child: no second spawn.
        assert m._upgrade_drain_after_pass(conn, backlog=True) is False
        assert spawned == [1]
        # The child finishes: the version is recorded and never drained again.
        m._upgrade_drain_mark_done(conn)
        m._upgrade_drain_release_lock()
        assert m._upgrade_drain_after_pass(conn, backlog=True) is False
    assert spawned == [1]
    assert _meta(db, m._UPGRADE_DRAIN_META) == m._upgrade_drain_key()
    # A new parser or pricing version drains again.
    monkeypatch.setattr(m, "_USAGE_PARSER_VERSION", m._USAGE_PARSER_VERSION + 1)
    with sqlite3.connect(str(db)) as conn:
        assert m._upgrade_drain_after_pass(conn, backlog=True) is True
    assert spawned == [1, 1]


def test_no_backlog_records_version_without_spawning(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setenv("TOKEN_OPTIMIZER_TEST_ALLOW_DRAIN_SPAWN", "1")
    monkeypatch.setattr(m, "_spawn_upgrade_drain", lambda: pytest.fail("spawned"))
    with sqlite3.connect(str(db)) as conn:
        assert m._upgrade_drain_after_pass(conn, backlog=False) is False
    assert _meta(db, m._UPGRADE_DRAIN_META) == m._upgrade_drain_key()


def test_stale_lock_from_dead_child_is_reclaimed(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setenv("TOKEN_OPTIMIZER_TEST_ALLOW_DRAIN_SPAWN", "1")
    monkeypatch.setattr(m, "_spawn_upgrade_drain", lambda: True)
    (tmp_path / "snap").mkdir(parents=True, exist_ok=True)
    lock = tmp_path / "snap" / m._UPGRADE_DRAIN_LOCK_NAME
    lock.write_text("1")
    old = lock.stat().st_mtime - m._UPGRADE_DRAIN_LOCK_STALE_SECONDS - 10
    os.utime(lock, (old, old))
    with sqlite3.connect(str(db)) as conn:
        assert m._upgrade_drain_after_pass(conn, backlog=True) is True


def test_drain_spawn_is_detached_and_windowless(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path / "snap")
    (tmp_path / "snap").mkdir(parents=True, exist_ok=True)
    seen = {}

    class Child:
        pid = 4242

    def popen(argv, **kw):
        seen.update(kw, argv=argv)
        return Child()

    monkeypatch.setattr(m.subprocess, "Popen", popen)
    assert m._spawn_upgrade_drain() is True
    assert seen["argv"][-2:] == ["upgrade-drain", "--quiet"]
    assert seen["env"]["TOKEN_OPTIMIZER_UPGRADE_DRAIN"] == "1"
    assert "TOKEN_OPTIMIZER_HOOK" not in seen["env"]
    assert seen["stdin"] is subprocess.DEVNULL and seen["stdout"] is subprocess.DEVNULL
    assert seen["creationflags"] == (getattr(subprocess, "DETACHED_PROCESS", 0) | m._NO_WINDOW)
    if os.name != "nt":
        assert seen["start_new_session"] is True
    assert (tmp_path / "snap" / m._UPGRADE_DRAIN_LOCK_NAME).read_text() == "4242"


def test_drain_mode_by_caller(m, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN", "1")
    assert m._collect_drain_mode() == "full"
    monkeypatch.delenv("TOKEN_OPTIMIZER_UPGRADE_DRAIN")
    assert m._collect_drain_mode() is None  # under pytest
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    assert m._collect_drain_mode() == "inline"
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)
    assert m._collect_drain_mode() is None


@pytest.mark.parametrize("drain,expected", [(None, 2), ("full", 4), ("inline", 4)])
def test_usage_reparse_is_uncapped_in_a_drain(tmp_path, monkeypatch, drain, expected):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    paths = []
    for i in range(4):
        sid = "aaaaaaaa-2222-4333-8444-55555555000%d" % i
        path = _projects(tmp_path) / "-repo" / f"{sid}.jsonl"
        _write(path, _streamed(when + timedelta(minutes=i), 2, mid=f"msg_{i}"))
        paths.append(path)
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET usage_parser_version = NULL")
    conn.commit()
    conn.close()
    monkeypatch.setattr(measure, "_COLLECT_MAX_PER_RUN", 2)
    monkeypatch.setattr(measure, "_collect_drain_mode", lambda: drain)
    monkeypatch.setattr(measure, "_upgrade_drain_after_pass", lambda conn, backlog: False)
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    current = conn.execute("SELECT COUNT(*) FROM session_log WHERE usage_parser_version = ?",
                           (measure._USAGE_PARSER_VERSION,)).fetchone()[0]
    conn.close()
    assert current == expected


def test_hook_pass_hands_capped_backlog_to_the_drain(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    for i in range(3):
        sid = "aaaaaaaa-2222-4333-8444-55555555001%d" % i
        _write(_projects(tmp_path) / "-repo" / f"{sid}.jsonl", _streamed(when, 2, mid=f"m{i}"))
    monkeypatch.setattr(measure, "_COLLECT_MAX_PER_RUN", 1)
    calls = []
    monkeypatch.setattr(measure, "_upgrade_drain_after_pass", lambda conn, backlog: calls.append(backlog))
    measure.collect_sessions(days=30, quiet=True)
    assert calls == [True]


# ---- re-measuring states ------------------------------------------------------

def test_all_time_reports_sessions_awaiting_rescore(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=3, model="claude-opus-5-5")
    _add_event(db, "tool_archive", 1000, _local_iso(start - timedelta(seconds=30)))
    with sqlite3.connect(str(db)) as conn:
        assert m._update_counted_cumulative(conn)["rows_written"] == 1
        assert m._counted_cumulative_summary()["remeasuring"] is False
        # A row priced by an older version waits for the next pass.
        conn.execute("UPDATE counted_reread SET pricing_detail = NULL")
        conn.commit()
        waiting = m._counted_cumulative_summary()
        assert waiting["remeasuring"] is True
        assert waiting["remeasuring_sessions"] == 1
        m._update_counted_cumulative(conn)
    done = m._counted_cumulative_summary()
    assert done["remeasuring"] is False and done["remeasuring_sessions"] == 0


def test_rows_without_a_session_never_hold_the_card_in_remeasuring(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("INSERT INTO counted_reread (event_key,source,session_uuid,event_ts,event_month,"
                     "tokens,oneshot_usd,reread_tokens,reread_usd,turns_counted) "
                     "VALUES ('ce:1','ce',NULL,'2026-06-01T10:00:00','2026-06',100,0.1,0,0,0)")
        conn.commit()
    assert m._counted_cumulative_summary()["remeasuring_sessions"] == 0


def _counted(**extra):
    return dict({"available": True, "reread_usd": 40.0, "reread_tokens": 5, "oneshot_usd": 1.0,
                 "total_usd": 41.0, "events": 3}, **extra)


def test_dashboard_card_labels_subtotal_in_progress(measure, monkeypatch):
    patch_savings(measure, monkeypatch)
    monkeypatch.setattr(measure, "_counted_cumulative_summary",
                        lambda **kw: _counted(remeasuring=True, remeasuring_sessions=12))
    card = render(measure._dashboard_savings_data()).split("savings-history")[1]
    assert "Re-measuring (12 sessions left)" in card
    assert "so far" in card
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: _counted())
    card = render(measure._dashboard_savings_data()).split("savings-history")[1]
    assert "Re-measuring" not in card and "so far" not in card


def test_routing_pending_counts_stale_or_unbuilt_rows_with_transcripts(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    paths = []
    for i in range(3):
        sid = "aaaaaaaa-2222-4333-8444-55555555002%d" % i
        path = _projects(tmp_path) / "-repo" / f"{sid}.jsonl"
        _write(path, _streamed(when, 2, mid=f"p{i}"))
        paths.append(path)
    measure.collect_sessions(days=30, quiet=True)
    assert measure._routing_pending_sessions(days=30) == 0
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET usage_parser_version = NULL WHERE jsonl_path = ?", (str(paths[0]),))
    conn.execute("UPDATE session_log SET daily_usage_json = NULL WHERE jsonl_path = ?", (str(paths[1]),))
    conn.execute("UPDATE session_log SET usage_parser_version = NULL, daily_usage_json = NULL "
                 "WHERE jsonl_path = ?", (str(paths[2]),))
    conn.commit()
    conn.close()
    assert measure._routing_pending_sessions(days=30) == 3
    # A row whose transcript is gone keeps its numbers and is not pending.
    paths[2].unlink()
    assert measure._routing_pending_sessions(days=30) == 2


def test_routing_returns_pending_instead_of_a_moving_number(m, monkeypatch):
    monkeypatch.setattr(m, "_model_mix_shares", lambda **kw: {"shares": {"opus": 0.8, "sonnet": 0.2},
                                                             "total_tokens": 1000})
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    monkeypatch.setattr(m, "_routing_pending_sessions", lambda **kw: 7)
    out = m._compute_model_routing_savings(days=30)
    assert out["evidence"] == "pending"
    assert out["pending_sessions"] == 7
    assert out["realized_cost_usd"] == 0.0


def _pending_snapshot():
    data = snapshot()
    data["model_routing"] = {"realized_cost_usd": 0.0, "evidence": "pending", "pending_sessions": 5}
    return data


def test_text_report_says_routing_is_remeasuring(measure, monkeypatch, capsys):
    patch_savings(measure, monkeypatch)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: _pending_snapshot())
    measure.savings_report()
    out = capsys.readouterr().out
    assert "model routing (realized): re-measuring (5 sessions left), not in ALL IN yet" in out
    assert "ALL IN leaves out model routing until its re-measurement finishes." in out


def test_dashboard_says_routing_is_remeasuring(measure, monkeypatch):
    patch_savings(measure, monkeypatch)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: _pending_snapshot())
    hero = render(measure._dashboard_savings_data()).split("savings-actions")[1].split("savings-history")[0]
    assert "Model routing: re-measuring (5 sessions left)" in hero


# ---- media archives -----------------------------------------------------------

def test_claude_image_tokens_follow_the_resize_rule(m):
    assert m._claude_image_tokens(1000, 750) == 1000
    # Over 1.15 MP: scaled down to the area limit first.
    assert m._claude_image_tokens(1446, 840) == int(1_150_000 / 750)
    # Long edge over 1568 px: scaled to 1568.
    assert m._claude_image_tokens(3136, 400) == int(1568 * 200 / 750)
    assert m._claude_image_tokens(0, 10) == 0


def _media_transcript(m, tmp_path, monkeypatch, start, archived):
    path, _ = _transcript(m, tmp_path, monkeypatch, n_turns=4, model="claude-opus-5-5", start=start)
    stamp = (start - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    use = {"type": "assistant", "timestamp": stamp, "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "mcp__claude-in-chrome__computer", "input": {}}]}}
    head = {"type": "text", "text": "Successfully captured screenshot (1000x750, jpeg)"}
    if archived:
        body = head["text"] + " " + "A" * 300 + " [Full result archived (90,000 chars)]"
    else:
        body = [head, {"type": "image", "source": {"type": "base64", "data": "A" * 90_000}}]
    result = {"type": "user", "timestamp": stamp, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": body}]}}
    path.write_text(json.dumps(use) + "\n" + json.dumps(result) + "\n" + path.read_text())
    return path, stamp


@pytest.mark.parametrize("archived", [True, False])
def test_screenshot_archive_counts_its_image_only_when_the_image_left(m, tmp_path, monkeypatch, archived):
    db = _db(m, tmp_path, monkeypatch)
    start = _transcript(m, tmp_path, monkeypatch)[1]
    _, stamp = _media_transcript(m, tmp_path, monkeypatch, start, archived)
    event_ts = _local_iso(start - timedelta(seconds=19))
    with sqlite3.connect(str(db)) as conn:
        conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,session_id,session_uuid,detail) "
                     "VALUES(?, 'tool_archive', 27000, ?, ?, "
                     "'replaced mcp__claude-in-chrome__computer (90,000 chars -> 1,800 chars)')",
                     (event_ts, SID, SID))
        conn.commit()
        m._update_counted_cumulative(conn)
        row = conn.execute("SELECT tokens, reread_tokens, turns_counted, pricing_detail FROM counted_reread "
                           "WHERE event_key LIKE 'md:%'").fetchone()
    summary = m._counted_cumulative_summary()
    assert row is not None
    detail = json.loads(row[3])
    if archived:
        marker_chars = len("Successfully captured screenshot (1000x750, jpeg) " + "A" * 300
                           + " [Full result archived (90,000 chars)]")
        assert row[0] == 1000 - (marker_chars + 1) // 2
        assert detail["magnitude_basis"] == "media_image_net"
        assert row[1] == row[0] * row[2] and row[2] > 0
        assert summary["total_usd"] > 0
        assert summary["media_priced_events"] == 1
        assert "1 of 1 screenshot and image archives" in summary["media_note"]
    else:
        assert row[0] == 0 and row[1] == 0
        assert detail["magnitude_basis"] == "media_kept_in_context"
        assert summary["total_usd"] == 0
        assert summary["media_priced_events"] == 0
    # Never the base64 length the event logged.
    assert row[0] < 27000


def test_media_initial_removal_is_priced_at_the_input_rate(m):
    start = m.datetime(2026, 9, 1, 12)
    card = m._counted_exact_card("claude-opus-5-5", m.PRICING_TIERS["anthropic"])
    turns = [(start + timedelta(minutes=i), "claude-opus-5-5", 50_000, (20_000, 0, 20_000, 30_000))
             for i in range(2)]
    floor = m._counted_score_detail(turns, [], start - timedelta(seconds=1), 1000,
                                    m.PRICING_TIERS["anthropic"], oneshot_floor_only=True)
    normal = m._counted_score_detail(turns, [], start - timedelta(seconds=1), 1000,
                                     m.PRICING_TIERS["anthropic"])
    assert floor["oneshot_usd"] == pytest.approx(1000 * card["input"] / 1e6)
    assert normal["oneshot_usd"] > floor["oneshot_usd"]
