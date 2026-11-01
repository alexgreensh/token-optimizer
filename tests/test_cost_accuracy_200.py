"""Daily cost must equal what the requests actually cost, per calendar day (#200).

Four independent ways the dashboard's per-day cost drifted from the real bill:
  1. one session stored under two paths (worktree dir, sandboxed-config mirror)
     was counted twice;
  2. a session that crossed midnight billed all of its requests to one day;
  3. subagent spend was left out of the cost entirely;
  4. a resumed session kept its first-seen date, so new spend landed on an old
     day, often outside the window.
Plus the rate cards for Opus 5.5 ($4/$20, 0.05x cache reads) and Fable 5.1
(0.025x cache reads), which were priced as their family.

Each test drives the real collect -> dashboard-query path in a sandbox.
"""

import copy
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
SID = "11111111-2222-4333-8444-555555555555"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    (tmp_path / "claude_home" / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    # monkeypatch restores the original module objects afterwards, so later
    # tests that hold a reference to runtime_env keep patching the live one.
    for name in ("measure", "runtime_env", "plugin_env"):
        # setitem first so teardown also removes a module that was absent here.
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("measure")


def _utc(local_dt):
    """Naive local datetime -> the UTC 'Z' timestamp Claude Code writes."""
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _assistant(req, when, model="claude-opus-4-8", inp=1000, out=500, cr=0, cc=0):
    return {
        "type": "assistant",
        "timestamp": _utc(when),
        "requestId": req,
        "message": {
            "id": f"msg_{req}",
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc},
        },
    }


def _write(path, records, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    user = {"type": "user", "timestamp": records[0]["timestamp"], "message": {"content": "hi"}}
    path.write_text("\n".join(json.dumps(r) for r in [user, *records]) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _daily(measure):
    trends = measure._collect_trends_from_db(days=30)
    return {d["date"]: d for d in trends["daily"]}


def _cost(measure, model, inp=1000, out=500, cr=0):
    return measure._get_model_cost(model, inp, out, cr, 0)


def _noon(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).replace(hour=12, minute=0, second=0, microsecond=0)


def test_same_session_under_two_paths_counts_once(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    recs = [_assistant("r1", when), _assistant("r2", when + timedelta(minutes=5))]
    projects = tmp_path / "claude_home" / "projects"
    _write(projects / "-repo" / f"{SID}.jsonl", recs)
    _write(projects / "-repo-worktree" / f"{SID}.jsonl", recs)

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[when.strftime("%Y-%m-%d")]

    assert day["sessions"] == 1
    assert day["total_cost_usd"] == pytest.approx(2 * _cost(measure, "claude-opus-4-8"), abs=1e-4)


def test_session_crossing_midnight_bills_each_day(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    before, after = midnight - timedelta(minutes=30), midnight + timedelta(minutes=30)
    _write(tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl",
           [_assistant("r1", before, out=100), _assistant("r2", after, out=9000)])

    measure.collect_sessions(days=30, quiet=True)
    daily = _daily(measure)

    assert daily[before.strftime("%Y-%m-%d")]["total_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8", out=100), abs=1e-4)
    assert daily[after.strftime("%Y-%m-%d")]["total_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8", out=9000), abs=1e-4)
    # Listed on both days, but only the latest day is the primary entry, so
    # per-session stats (cache/TTL mix, coaching) count the session once.
    entries = [sd for d in daily.values() for sd in d["session_details"]]
    assert len(entries) == 2
    assert sorted(sd["continuation"] for sd in entries) == [False, True]
    primary = next(sd for sd in entries if not sd["continuation"])
    assert primary["session_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8", out=100) + _cost(measure, "claude-opus-4-8", out=9000), abs=1e-4)


def test_subagent_spend_is_in_the_daily_cost(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    parent = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    _write(parent, [_assistant("p1", when)])
    _write(parent.parent / SID / "subagents" / "agent-abc.jsonl",
           [_assistant("s1", when + timedelta(minutes=1), model="claude-sonnet-5", inp=50_000, out=4000)])

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[when.strftime("%Y-%m-%d")]

    expected = _cost(measure, "claude-opus-4-8") + _cost(measure, "claude-sonnet-5", inp=50_000, out=4000)
    assert day["total_cost_usd"] == pytest.approx(expected, abs=1e-4)


def test_request_written_to_parent_and_subagent_bills_once(tmp_path, monkeypatch):
    """Claude Code can write one API request into several of a session's files."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    parent = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    shared = _assistant("shared", when, out=3000)
    _write(parent, [_assistant("p1", when), shared])
    _write(parent.parent / SID / "subagents" / "agent-abc.jsonl", [shared])

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[when.strftime("%Y-%m-%d")]

    expected = _cost(measure, "claude-opus-4-8") + _cost(measure, "claude-opus-4-8", out=3000)
    assert day["total_cost_usd"] == pytest.approx(expected, abs=1e-4)
    # The stored token columns agree with the cost: the shared request once.
    conn = measure._init_trends_db()
    inp, out = conn.execute("SELECT input_tokens, output_tokens FROM session_log").fetchone()
    conn.close()
    assert (inp, out) == (2000, 3500)


def test_undated_request_bills_to_the_session_date(tmp_path, monkeypatch):
    """A request with no usable timestamp is still spend; it bills to the session's date."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(0)
    parent = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    undated = _assistant("u1", when, out=2000)
    undated["timestamp"] = "not-a-time"
    _write(parent, [_assistant("p1", when), undated])
    _write(parent.parent / SID / "subagents" / "agent-abc.jsonl",
           [_assistant("s1", when, inp=90_000, out=9000)])

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[datetime.now().strftime("%Y-%m-%d")]

    expected = (_cost(measure, "claude-opus-4-8") + _cost(measure, "claude-opus-4-8", out=2000)
                + _cost(measure, "claude-opus-4-8", inp=90_000, out=9000))
    assert day["total_cost_usd"] == pytest.approx(expected, abs=1e-4)


def test_undated_request_bills_to_the_last_real_day_not_the_file_mtime(tmp_path, monkeypatch):
    """File mtime can be a day the session never ran; untimestamped spend follows
    the requests, and does not make a one-day session look multi-day."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(2)
    parent = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    undated = _assistant("u1", when, out=2000)
    undated["timestamp"] = "not-a-time"
    _write(parent, [_assistant("p1", when), undated], mtime=_noon(1).timestamp())

    measure.collect_sessions(days=30, quiet=True)
    daily = _daily(measure)

    assert _noon(1).strftime("%Y-%m-%d") not in daily
    day = daily[when.strftime("%Y-%m-%d")]
    assert day["total_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8") + _cost(measure, "claude-opus-4-8", out=2000), abs=1e-4)
    assert [sd["continuation"] for sd in day["session_details"]] == [False]


def test_resumed_session_moves_to_its_latest_day(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    old = _noon(20)
    path = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    _write(path, [_assistant("r1", old)], mtime=old.timestamp())
    measure.collect_sessions(days=30, quiet=True)

    new = _noon(1)
    _write(path, [_assistant("r1", old), _assistant("r2", new, out=7000)], mtime=time.time())
    measure.collect_sessions(days=30, quiet=True)
    daily = _daily(measure)

    assert daily[new.strftime("%Y-%m-%d")]["total_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8", out=7000), abs=1e-4)
    assert daily[old.strftime("%Y-%m-%d")]["total_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8"), abs=1e-4)


def test_existing_duplicate_rows_are_collapsed(tmp_path, monkeypatch):
    """DBs written by older versions already hold the duplicate rows."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    projects = tmp_path / "claude_home" / "projects"
    _write(projects / "-repo" / f"{SID}.jsonl", [_assistant("r1", when)])
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    row = conn.execute("SELECT * FROM session_log").fetchone()
    cols = [d[0] for d in conn.execute("SELECT * FROM session_log").description]
    dup = dict(zip(cols, row))
    dup.pop("id")
    dup["jsonl_path"] = str(projects / "-mirror" / f"{SID}.jsonl")
    conn.execute(f"INSERT INTO session_log ({','.join(dup)}) VALUES ({','.join('?' * len(dup))})",
                 list(dup.values()))
    conn.commit()
    conn.close()

    day = _daily(measure)[when.strftime("%Y-%m-%d")]
    assert day["sessions"] == 1
    assert day["total_cost_usd"] == pytest.approx(_cost(measure, "claude-opus-4-8"), abs=1e-4)


def test_generation_rate_cards(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    m = 1_000_000
    # Opus 5.5: $4 in, $20 out, $0.20 cache read, $5 / $8 cache writes.
    assert measure._get_model_cost("claude-opus-5-5", m, m, m, 0) == pytest.approx(24.20)
    assert measure._get_model_cost("claude-opus-5-5[1m]", 0, 0, 0, m, cache_create_1h=m, cache_create_5m=0) == pytest.approx(8.0)
    # Fable 5.1 cache reads are $0.25; Fable 5 stays at $1.
    assert measure._get_model_cost("claude-fable-5-1", 0, 0, m, 0) == pytest.approx(0.25)
    assert measure._get_model_cost("claude-fable-5", 0, 0, m, 0) == pytest.approx(1.0)
    # The rest of the Opus family is unchanged, and labels stay on the family bucket.
    assert measure._get_model_cost("claude-opus-5", m, m, 0, 0) == pytest.approx(30.0)
    assert measure._normalize_model_name("claude-opus-5-5") == "opus"


def test_browser_opener_is_detached_from_the_terminal(tmp_path, monkeypatch):
    """#199: a TUI host (OpenCode) must never receive the browser's output."""
    measure = _load(tmp_path, monkeypatch)
    seen = {}

    class _Proc:
        def wait(self, timeout=None):
            return 0

    def _popen(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return _Proc()

    monkeypatch.setattr(measure.subprocess, "Popen", _popen)
    measure._launch_opener(["xdg-open", "http://localhost/x"])

    for stream in ("stdin", "stdout", "stderr"):
        assert seen[stream] is subprocess.DEVNULL
    if os.name == "posix":
        assert seen["start_new_session"] is True


def test_fallback_rollup_counts_a_duplicated_session_once(tmp_path, monkeypatch):
    """The JSONL fallback (no trends DB yet) must dedupe copies like the DB path."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    recs = [_assistant("r1", when)]
    projects = tmp_path / "claude_home" / "projects"
    _write(projects / "-repo" / f"{SID}.jsonl", recs)
    _write(projects / "-repo-worktree" / f"{SID}.jsonl", recs)

    trends = measure._collect_trends_from_jsonl(days=30)
    day = {d["date"]: d for d in trends["daily"]}[when.strftime("%Y-%m-%d")]
    assert day["sessions"] == 1
    assert day["total_cost_usd"] == pytest.approx(_cost(measure, "claude-opus-4-8"), abs=1e-4)


def test_worktree_copy_subagents_are_kept_after_dedupe(tmp_path, monkeypatch):
    """A subagent that only exists under the losing copy's folder is still billed."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    projects = tmp_path / "claude_home" / "projects"
    main = projects / "-repo" / f"{SID}.jsonl"
    _write(main, [_assistant("r1", when), _assistant("r2", when)])
    _write(projects / "-repo-worktree" / f"{SID}.jsonl", [_assistant("r1", when)])
    _write(projects / "-repo-worktree" / SID / "subagents" / "agent-wt.jsonl",
           [_assistant("s1", when, inp=40_000, out=4000)])

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[when.strftime("%Y-%m-%d")]
    expected = 2 * _cost(measure, "claude-opus-4-8") + _cost(measure, "claude-opus-4-8", inp=40_000, out=4000)
    assert day["sessions"] == 1
    assert day["total_cost_usd"] == pytest.approx(expected, abs=1e-4)


def test_fallback_rollup_does_not_mutate_the_parser_cache(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    parent = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    _write(parent, [_assistant("p1", when)])
    skill = _assistant("s1", when)
    skill["message"]["content"] = [{"type": "tool_use", "name": "Skill", "input": {"skill": "x"}}]
    _write(parent.parent / SID / "subagents" / "agent-a.jsonl", [skill])

    before = copy.deepcopy(measure._parse_session_jsonl(parent))
    measure._collect_trends_from_jsonl(days=30)
    assert measure._parse_session_jsonl(parent) == before


def test_append_logs_are_capped(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    log = tmp_path / "x.log"
    log.write_bytes(b"old line\n" * 50_000)
    measure._cap_append_log(log, max_bytes=10_000)
    data = log.read_bytes()
    assert len(data) <= 10_000 and data.startswith(b"old line")


def _busy_wait(tmp_path, timeout_ms):
    """How long this machine really waits for a ``busy_timeout`` of ``timeout_ms``."""
    import sqlite3
    path = tmp_path / "busy-probe.db"
    holder = sqlite3.connect(str(path))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE IF NOT EXISTS t(x)")
    holder.commit()
    waiter = sqlite3.connect(str(path))
    waiter.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        try:
            waiter.execute("INSERT INTO t VALUES (1)")
        except sqlite3.OperationalError:
            pass
        return time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
        waiter.close()


def test_dashboard_read_does_not_wait_behind_a_held_write_lock(tmp_path, monkeypatch):
    """Upkeep writes on the read path give up fast when a collector holds the lock."""
    import sqlite3

    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    _write(tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl", [_assistant("r1", when)],
           mtime=when.timestamp())
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET daily_usage_json = NULL")  # leave upkeep to do
    conn.commit()
    conn.close()

    # The same read with no lock held: how long this runner takes for the work itself.
    # (It also does the upkeep, so the timed read below has it to do again.)
    t0 = time.monotonic()
    _daily(measure)
    baseline = time.monotonic() - t0
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET daily_usage_json = NULL")
    conn.commit()
    conn.close()

    writer = sqlite3.connect(str(measure.TRENDS_DB))
    writer.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        day = _daily(measure)[when.strftime("%Y-%m-%d")]
        elapsed = time.monotonic() - t0
    finally:
        writer.rollback()
        writer.close()

    # Giving up after the 0.2 s upkeep timeout fits easily; waiting out the 5 s
    # busy timeout does not, however slow the runner. The upkeep makes two
    # retried writes of three attempts each, so six busy waits plus 0.6 s of
    # backoff sleeps. The waits are measured, not assumed: SQLite's busy
    # handler sleeps in slices that stretch under load (a 200 ms timeout waited
    # 0.86 s on a Mac at load 39), which is the machine, not the read path.
    budget = baseline + 6 * _busy_wait(tmp_path, 200) + 0.6 + 1.0
    assert elapsed < budget, f"held-lock read took {elapsed:.2f}s, budget {budget:.2f}s"
    assert day["total_cost_usd"] == pytest.approx(_cost(measure, "claude-opus-4-8"), abs=1e-4)


def test_missing_transcript_is_backfilled_when_it_returns(tmp_path, monkeypatch):
    """A transcript that is briefly missing (synced folder, unmounted drive) must
    not be settled forever; it fills in once the file is back."""
    measure = _load(tmp_path, monkeypatch)
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    before, after = midnight - timedelta(minutes=30), midnight + timedelta(minutes=30)
    path = tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl"
    _write(path, [_assistant("r1", before, out=100), _assistant("r2", after, out=9000)])
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET daily_usage_json = NULL")
    conn.commit()
    conn.close()

    saved = path.read_bytes()
    stat = path.stat()
    path.unlink()
    _daily(measure)  # upkeep runs while the file is gone
    path.write_bytes(saved)
    os.utime(path, (stat.st_atime, stat.st_mtime))  # comes back with its old mtime

    daily = _daily(measure)
    assert daily[before.strftime("%Y-%m-%d")]["total_cost_usd"] == pytest.approx(
        _cost(measure, "claude-opus-4-8", out=100), abs=1e-4)


def test_diverged_copies_of_a_session_lose_no_spend(tmp_path, monkeypatch):
    """Two copies of one session that each hold a request the other lacks."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    projects = tmp_path / "claude_home" / "projects"
    shared = _assistant("s1", when)
    _write(projects / "-repo" / f"{SID}.jsonl", [shared, _assistant("a-only", when, out=4000)])
    _write(projects / "-repo-mirror" / f"{SID}.jsonl", [shared, _assistant("b-only", when, out=8000)])

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[when.strftime("%Y-%m-%d")]

    assert day["sessions"] == 1
    expected = (_cost(measure, "claude-opus-4-8") + _cost(measure, "claude-opus-4-8", out=4000)
                + _cost(measure, "claude-opus-4-8", out=8000))
    assert day["total_cost_usd"] == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize("bad_model", [123, {"x": 1}, ["m"], True, 4.5, None])
def test_a_record_with_a_non_text_model_does_not_break_collect(tmp_path, monkeypatch, bad_model):
    measure = _load(tmp_path, monkeypatch)
    when = _noon(1)
    rec = _assistant("r1", when)
    rec["message"]["model"] = bad_model
    _write(tmp_path / "claude_home" / "projects" / "-repo" / f"{SID}.jsonl", [rec, _assistant("r2", when)])

    measure.collect_sessions(days=30, quiet=True)
    day = _daily(measure)[when.strftime("%Y-%m-%d")]
    assert day["sessions"] == 1
    assert day["total_cost_usd"] >= _cost(measure, "claude-opus-4-8") - 1e-4


def test_resumed_adapter_session_is_dated_by_its_last_activity(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    first = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    last = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    active = measure._adapter_activity_ts({"first_ts": first.isoformat(), "last_ts": last.isoformat()})
    assert active == last
    assert measure._adapter_activity_ts({"first_ts": first.isoformat(), "last_ts": None}) == first
    assert measure._adapter_activity_ts({"first_ts": "garbage"}) is None
