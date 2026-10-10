"""`measure.py status-bar --session <id> --json`: the desktop band's one read.

One spawn returns savings (this session, 30 local days by day,
the 30-day headline), the last main-thread request time and measured cache
lifetime from the transcript, and the checkpoint saved time from the freshest
quality cache. Savings are answered from a per-session JSON cache under
SNAPSHOT_DIR; a cache older than 60 s starts exactly one detached refresh.

Run: python3 -m pytest tests/test_status_bar_command.py -q
"""
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _sandbox_env(tmp_path):
    snap = tmp_path / "snap"
    home = tmp_path / "home"
    claude = home / ".claude"
    for d in (snap, claude / "projects" / "-proj", claude / "token-optimizer"):
        d.mkdir(parents=True, exist_ok=True)
    env = {
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "CLAUDE_CONFIG_DIR": str(claude),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    return env, snap, claude


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    env, snap, claude = _sandbox_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    m._sb_env = env
    m._sb_snap = snap
    m._sb_claude = claude
    yield m
    sys.modules.pop("measure", None)


def _day(offset_days, hour=12):
    d = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=offset_days))


def _seed(m, rows=None, compression=None):
    conn = m._init_trends_db()
    try:
        for ts, etype, tok, cost, sid in rows or []:
            conn.execute(
                "INSERT INTO savings_events (timestamp, event_type, tokens_saved, "
                "cost_saved_usd, session_id, session_uuid) VALUES (?,?,?,?,?,?)",
                (ts.isoformat(), etype, tok, cost, sid, sid))
        for ts, feature, orig, comp, sid, tier in compression or []:
            conn.execute(
                "INSERT INTO compression_events (timestamp, session_id, session_uuid, "
                "feature, original_tokens, compressed_tokens, model, tier) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts.isoformat(), sid, sid, feature, orig, comp, "claude-opus-4-5", tier))
        conn.commit()
    finally:
        conn.close()


def _three_day_fixture(m):
    _seed(m, rows=[
        (_day(0), "tool_archive", 1000, 0.01, SID_A),
        (_day(2), "tool_archive", 2000, 0.02, SID_A),
        (_day(5), "structure_map", 3000, 0.03, SID_A),
        (_day(1), "tool_archive", 5000, 0.05, SID_B),
        # Estimated tier: relocated out of the realized figures, like the headline.
        (_day(0), "mcp_cap", 9999, 0.99, SID_A),
        # Older than 30 days: in neither the bars nor the 30-day total.
        (_day(40), "tool_archive", 70000, 0.70, SID_B),
    ], compression=[
        (_day(0), "bash_compress_git", 600, 100, SID_A, "measured"),
        # Opportunity tier never counts.
        (_day(0), "bash_compress_git", 900, 0, SID_A, "opportunity"),
    ])


def _by_date(daily):
    return {d["date"]: d for d in daily}


# --------------------------------------------------------------------------
# Savings figures
# --------------------------------------------------------------------------

def test_three_days_two_sessions(sb):
    _three_day_fixture(sb)
    sav = sb._status_bar_savings_or_reason(SID_A)[0]
    assert sav is not None
    assert sav["unit"] == "tokens"
    # Session A: 1000 + 2000 + 3000 realized + 500 compression; mcp_cap excluded.
    assert sav["session_tokens"] == 6500
    daily = sav["daily"]
    assert len(daily) == 30
    dates = [d["date"] for d in daily]
    assert dates == sorted(dates)
    assert dates[-1] == datetime.now().date().isoformat()
    by = _by_date(daily)
    assert by[_day(0).date().isoformat()]["tokens"] == 1500
    assert by[_day(1).date().isoformat()]["tokens"] == 5000
    assert by[_day(2).date().isoformat()]["tokens"] == 2000
    assert by[_day(5).date().isoformat()]["tokens"] == 3000
    zero_days = [d for d in daily if d["date"] not in {
        _day(k).date().isoformat() for k in (0, 1, 2, 5)}]
    assert len(zero_days) == 26
    assert all(d["tokens"] == 0 and d["usd"] == 0 for d in zero_days)
    headline = sb._get_merged_savings(days=30)
    assert sav["total_30d_usd"] == headline["total_cost_usd"]
    assert sav["total_30d_measured_tokens"] == headline["total_tokens"]
    assert sav["total_30d_tokens"] == sb._dashboard_saved_tokens(headline)[0]


def test_rows_older_than_30_days_excluded(sb):
    _three_day_fixture(sb)
    sav = sb._status_bar_savings_or_reason(SID_B)[0]
    assert sum(d["tokens"] for d in sav["daily"]) == 11500
    assert _day(40).date().isoformat() not in _by_date(sav["daily"])
    # The 70,000-token row 40 days back is outside the 30-day headline too.
    assert sav["total_30d_measured_tokens"] < 70000
    assert sav["total_30d_measured_tokens"] == sb._get_merged_savings(days=30)["total_tokens"]


def test_total_equals_merged_savings_headline(sb):
    _three_day_fixture(sb)
    sav = sb._status_bar_savings_or_reason(SID_A)[0]
    assert sav["total_30d_usd"] == sb._get_merged_savings(days=30)["total_cost_usd"]


def test_savings_summary_still_matches_realized_helper(sb):
    """The window total and the bars share one netting helper."""
    _seed(sb, rows=[
        (_day(0), "tool_archive", 4000, 0.04, SID_A),
        (_day(0), "tool_archive_reexpand", 1500, 0.015, SID_A),
        (_day(0), "verbosity_steer", 800, 0.008, SID_A),
    ])
    summary = sb._get_savings_summary(days=30)
    assert summary["total_tokens"] == 2500
    sav = sb._status_bar_savings_or_reason(SID_A)[0]
    assert sav["session_tokens"] == 2500


def test_missing_trends_db_returns_null_savings_exit_0(sb, tmp_path):
    env = dict(os.environ)
    env.update(sb._sb_env)
    assert not (sb._sb_snap / "trends.db").exists()
    proc = subprocess.run(
        [sys.executable, str(MEASURE), "status-bar", "--session", SID_A, "--json"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["savings"] is None
    assert out["savings_state"] == "unavailable"
    assert out["savings_reason"]
    assert out["last_request_epoch"] is None
    assert out["cache_lifetime"] is None
    assert out["last_checkpoint_epoch"] is None
    assert not (sb._sb_snap / "trends.db").exists(), "a read must not create the DB"


# --------------------------------------------------------------------------
# Transcript: last main-thread request and measured lifetime
# --------------------------------------------------------------------------

def _assistant(ts, usage, sidechain=False, model="claude-opus-4-5"):
    rec = {"type": "assistant", "timestamp": ts,
           "message": {"model": model, "usage": usage}}
    if sidechain:
        rec["isSidechain"] = True
        rec["agentId"] = "agent-1"
    return json.dumps(rec)


def _write_transcript(sb, lines, sid=SID_A):
    p = sb._sb_claude / "projects" / "-proj" / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_subagent_last_row_returns_main_thread_time(sb):
    p = _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00.000Z",
                   {"input_tokens": 5, "cache_creation": {"ephemeral_5m_input_tokens": 900}}),
        json.dumps({"type": "user", "timestamp": "2026-10-03T10:00:30.000Z",
                    "message": {"content": "hi"}}),
        _assistant("2026-10-03T10:05:00.000Z",
                   {"input_tokens": 5, "cache_creation": {"ephemeral_1h_input_tokens": 50}},
                   sidechain=True),
    ])
    ts, lifetime, model = sb._status_bar_transcript_state(p)
    assert ts == datetime.fromisoformat("2026-10-03T10:00:00+00:00").timestamp()
    assert lifetime == "5m"
    assert model == "claude-opus-4-5"


def test_one_hour_write_then_reads_returns_1h(sb):
    p = _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_1h_input_tokens": 40000}}),
        _assistant("2026-10-03T10:01:00Z", {"cache_read_input_tokens": 40000}),
        _assistant("2026-10-03T10:02:00Z", {"cache_read_input_tokens": 40100}),
    ])
    ts, lifetime, model = sb._status_bar_transcript_state(p)
    assert lifetime == "1h"
    assert ts == datetime.fromisoformat("2026-10-03T10:02:00+00:00").timestamp()
    assert model == "claude-opus-4-5"


def test_command_reads_transcript_by_session_id(sb):
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_1h_input_tokens": 10}}),
    ])
    out = sb.status_bar_payload(SID_A)
    assert out["cache_lifetime"] == "1h"
    assert out["last_request_epoch"] == datetime.fromisoformat(
        "2026-10-03T10:00:00+00:00").timestamp()


# --------------------------------------------------------------------------
# Checkpoint saved time: freshest quality cache across storage dirs
# --------------------------------------------------------------------------

def _saved(sb, name):
    cp = sb._sb_claude / "elsewhere" / name
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("# checkpoint", encoding="utf-8")
    os.utime(cp, (10, 10))
    return str(cp)


def test_checkpoint_epoch_from_freshest_quality_cache(sb):
    old = sb._sb_claude / "token-optimizer" / f"quality-cache-{SID_A}.json"
    old.write_text(json.dumps({"last_checkpoint_epoch": 1000,
                               "last_checkpoint_path": _saved(sb, "old.md")}), encoding="utf-8")
    os.utime(old, (time.time() - 600, time.time() - 600))
    plugin_dir = (sb._sb_claude / "plugins" / "data"
                  / "token-optimizer-alexgreensh-token-optimizer" / "token-optimizer")
    plugin_dir.mkdir(parents=True)
    (plugin_dir / f"quality-cache-{SID_A}.json").write_text(
        json.dumps({"last_checkpoint_epoch": 2000,
                    "last_checkpoint_path": _saved(sb, "new.md")}), encoding="utf-8")
    assert sb._status_bar_checkpoint_epoch(SID_A) == 2000


def test_checkpoint_epoch_ignores_a_save_retention_removed(sb):
    # The quality cache still names the save after retention deleted the file.
    gone = _saved(sb, "gone.md")
    Path(gone).unlink()
    (sb._sb_claude / "token-optimizer" / f"quality-cache-{SID_A}.json").write_text(
        json.dumps({"last_checkpoint_epoch": 2000, "last_checkpoint_path": gone}), encoding="utf-8")
    assert sb._status_bar_checkpoint_epoch(SID_A) is None
    (sb._sb_claude / "token-optimizer" / f"quality-cache-{SID_A}.json").write_text(
        json.dumps({"last_checkpoint_epoch": 2000}), encoding="utf-8")
    assert sb._status_bar_checkpoint_epoch(SID_A) is None


def test_checkpoint_epoch_counts_stop_checkpoint_files(sb):
    # A stop checkpoint never touches the quality cache; its file still counts.
    (sb._sb_claude / "token-optimizer" / f"quality-cache-{SID_A}.json").write_text(
        json.dumps({"last_checkpoint_epoch": 1000}), encoding="utf-8")
    cp = sb._sb_claude / "token-optimizer" / "checkpoints" / f"{SID_A}-20261003-124125-stop.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("# stop", encoding="utf-8")
    os.utime(cp, (5000, 5000))
    assert sb._status_bar_checkpoint_epoch(SID_A) == 5000


def test_earlier_checkpoint_from_resumable_flag(sb):
    cp = sb._sb_claude / "token-optimizer" / "checkpoints" / "99999999-aaaa-20261003-120001-stop.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("# checkpoint", encoding="utf-8")
    flag = sb._sb_claude / "token-optimizer" / f"resumable-{SID_A}.json"
    flag.write_text(json.dumps({"checkpoint": str(cp), "ts": 1, "relevant": True}), encoding="utf-8")
    got = sb._status_bar_earlier_checkpoint(SID_A)
    assert got is not None and got["epoch"] == int(cp.stat().st_mtime)
    assert "about" in got


def test_earlier_checkpoint_none_without_flag_or_for_own_checkpoint(sb):
    assert sb._status_bar_earlier_checkpoint(SID_A) is None
    own = sb._sb_claude / "token-optimizer" / "checkpoints" / f"{SID_A}-20261003-120001-stop.md"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("# mine", encoding="utf-8")
    (sb._sb_claude / "token-optimizer" / f"resumable-{SID_A}.json").write_text(
        json.dumps({"checkpoint": str(own), "relevant": True}), encoding="utf-8")
    assert sb._status_bar_earlier_checkpoint(SID_A) is None


def test_earlier_checkpoint_hidden_when_not_relevant(sb):
    cp = sb._sb_claude / "token-optimizer" / "checkpoints" / "88888888-bbbb-20261003-120001-stop.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("# other project", encoding="utf-8")
    (sb._sb_claude / "token-optimizer" / f"resumable-{SID_A}.json").write_text(
        json.dumps({"checkpoint": str(cp), "relevant": False}), encoding="utf-8")
    assert sb._status_bar_earlier_checkpoint(SID_A) is None


# --------------------------------------------------------------------------
# Cache and refresh
# --------------------------------------------------------------------------

def test_cached_answer_is_fast_and_reports_age(sb, monkeypatch):
    _three_day_fixture(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    f0 = time.perf_counter()
    fresh = sb.status_bar_payload(SID_A, sync=True)
    fresh_elapsed = time.perf_counter() - f0
    assert fresh["savings"]["session_tokens"] == 6500
    t0 = time.perf_counter()
    out = sb.status_bar_payload(SID_A)
    elapsed = time.perf_counter() - t0
    # Bound scaled to this machine's own fresh-compute cost: a cache that is
    # secretly ignored pays the full compute again, so 0.6x the fresh call
    # sits between the two on any runner — with no absolute floor for a fast
    # machine to hide the regression under. The 5ms min only covers a
    # degenerate ~0ms fresh measurement.
    assert elapsed < max(0.005, fresh_elapsed * 0.6), (
        f"cached path took {elapsed:.3f}s (fresh compute: {fresh_elapsed:.3f}s)"
    )
    assert out["savings"]["session_tokens"] == 6500
    assert out["savings_state"] == "fresh"
    assert 0 <= out["savings_age_s"] < 60
    assert out["refresh_started"] is False
    assert spawns == []


def test_stale_cache_answers_from_cache_and_starts_exactly_one_refresh(sb, monkeypatch):
    _three_day_fixture(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    sb.status_bar_payload(SID_A, sync=True)
    cache = sb._status_bar_cache_path(SID_A)
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["computed_at"] -= 120
    cache.write_text(json.dumps(data), encoding="utf-8")

    first = sb.status_bar_payload(SID_A)
    second = sb.status_bar_payload(SID_A)
    assert first["savings"]["session_tokens"] == 6500
    assert first["savings_state"] == "stale"
    assert first["savings_age_s"] >= 120
    assert first["refresh_started"] is True
    assert second["refresh_started"] is False
    assert len(spawns) == 1
    argv = spawns[0]
    assert "status-bar" in argv and "--sync" in argv and SID_A in argv


def test_no_cache_starts_refresh_and_reports_loading(sb, monkeypatch):
    _three_day_fixture(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    out = sb.status_bar_payload(SID_A)
    assert out["savings"] is None
    assert out["savings_state"] == "loading"
    assert out["refresh_started"] is True
    assert len(spawns) == 1


def test_full_compute_within_5s_on_50k_rows(sb):
    base = datetime.now() - timedelta(days=29)
    rows = []
    for i in range(50_000):
        ts = base + timedelta(seconds=i * 50)
        sid = SID_A if i % 2 else SID_B
        rows.append((ts, "tool_archive", 100, 0.001, sid))
    # Untimed warm-up on a 5K-row slice: absorbs lazy costs so the 50K
    # measurement below is pure throughput on this machine.
    _seed(sb, rows=rows[:5000])
    sb._status_bar_savings_or_reason(SID_A)
    _seed(sb, rows=rows[5000:])

    # Bound scaled by an INDEPENDENT fixed workload — one plain aggregate over
    # the very table the compute scans, through the module's own db opener.
    # A same-function baseline would scale with the slowdown being guarded
    # against (a uniform ~5x regression is invisible to it), and the old 5s
    # absolute floor let a ~3.4s quadratic blowup stay green on this machine.
    def _probe():
        conn = sb._init_trends_db()
        try:
            return conn.execute(
                "SELECT COUNT(*), SUM(tokens_saved) FROM savings_events"
            ).fetchone()
        finally:
            conn.close()

    probe = float("inf")
    for _ in range(3):
        p0 = time.perf_counter()
        _probe()
        probe = min(probe, time.perf_counter() - p0)

    t0 = time.perf_counter()
    sav = sb._status_bar_savings_or_reason(SID_A)[0]
    elapsed = time.perf_counter() - t0
    # Healthy compute costs ~50x the aggregate probe idle, ~140x under 4xCPU
    # contention (the tiny probe absorbs less load than the full compute);
    # a quadratic blowup lands ~6700x, so 500x sits between loaded-healthy
    # and broken on any runner.
    assert elapsed < probe * 500.0, (
        f"full compute took {elapsed:.2f}s "
        f"(probe: {probe:.4f}s for one aggregate over the same table)"
    )
    assert sav["session_tokens"] == 25_000 * 100


# --------------------------------------------------------------------------
# Refresh lock: atomic stale takeover and owner token
# --------------------------------------------------------------------------

def _stale_lock(sb, content="old-owner", age_s=200):
    lock = sb._status_bar_lock_path(SID_A)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="ascii")
    old = time.time() - age_s
    os.utime(lock, (old, old))
    return lock


def test_stale_lock_taken_over_by_one_caller_only(sb, monkeypatch):
    lock = _stale_lock(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(kw) or object())
    assert sb._status_bar_start_refresh(SID_A) is True
    assert sb._status_bar_start_refresh(SID_A) is False
    assert len(spawns) == 1
    assert lock.exists()
    assert not list(lock.parent.glob(lock.name + ".*.stale")), "quarantined lock left behind"


def test_racing_takeover_of_a_stale_lock_starts_one_child(sb, monkeypatch):
    """Both callers see the stale lock before either acts. The late one must
    not delete or replace the lock the first one just took."""
    import pathlib
    lock = _stale_lock(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(kw) or object())
    real_stat = pathlib.Path.stat
    raced = {"done": False}

    def racing_stat(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if not raced["done"] and self == lock:
            raced["done"] = True
            # Caller B runs to completion between A's staleness check and A's takeover.
            assert sb._status_bar_start_refresh(SID_A) is True
        return st

    monkeypatch.setattr(pathlib.Path, "stat", racing_stat)
    assert sb._status_bar_start_refresh(SID_A) is False
    monkeypatch.setattr(pathlib.Path, "stat", real_stat)
    assert len(spawns) == 1
    assert lock.read_text(encoding="ascii") == spawns[0]["env"][sb._STATUS_BAR_LOCK_TOKEN_ENV]
    assert not list(lock.parent.glob(lock.name + ".*.stale"))


def test_child_gets_the_lock_token_in_its_environment(sb, monkeypatch):
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append((argv, kw)) or object())
    assert sb._status_bar_start_refresh(SID_A) is True
    argv, kw = spawns[0]
    token = kw["env"]["TO_STATUS_BAR_LOCK_TOKEN"]
    assert len(token) == 32
    assert sb._status_bar_lock_path(SID_A).read_text(encoding="ascii") == token
    assert not any("TOKEN" in a for a in argv), "the token travels by env, not the CLI"


def test_failed_spawn_releases_its_own_lock(sb, monkeypatch):
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: None)
    assert sb._status_bar_start_refresh(SID_A) is False
    assert not sb._status_bar_lock_path(SID_A).exists()


def _run_release(sb, env_token):
    env = dict(os.environ)
    env.update(sb._sb_env)
    env.pop("TO_STATUS_BAR_LOCK_TOKEN", None)
    if env_token is not None:
        env["TO_STATUS_BAR_LOCK_TOKEN"] = env_token
    proc = subprocess.run(
        [sys.executable, str(MEASURE), "status-bar", "--session", SID_A,
         "--sync", "--release-lock"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return sb._status_bar_lock_path(SID_A)


@pytest.mark.parametrize("env_token", ["b" * 32, None, ""])
def test_child_keeps_a_lock_it_does_not_own(sb, env_token):
    _stale_lock(sb, content="a" * 32, age_s=0)
    lock = _run_release(sb, env_token)
    assert lock.exists()
    assert lock.read_text(encoding="ascii") == "a" * 32


def test_child_releases_its_own_lock(sb):
    _stale_lock(sb, content="a" * 32, age_s=0)
    lock = _run_release(sb, "a" * 32)
    assert not lock.exists()


def test_dashboard_saved_tokens_is_measured_plus_estimated(sb):
    summary = {
        "total_tokens": 1000,
        "mcp_cap_estimated": {"tokens_saved": 10},
        "hint_followed": {"tokens_saved": 20},
        "verbosity_steer_estimated": {"tokens_saved": 30},  # fallback key, as the dashboard reads it
        "resume_lean_estimated": {"tokens_saved": 40},
        "reread_avoided": {"reread_tokens": 999999},  # not in the headline
    }
    assert sb._dashboard_saved_tokens(summary) == (1100, 1000)
    assert sb._dashboard_saved_tokens({}) == (0, 0)


def test_dashboard_headline_formula_matches_the_dashboard_source():
    # Drift guard: if the dashboard's Tokens Saved card changes its fields,
    # _dashboard_saved_tokens must change with it.
    html = (Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    card = html[html.index("function tokensSavedCardHtml"):]
    card = card[:card.index("var tsFullSaved")]
    for field in ("s.mcp_cap_estimated", "s.hint_followed || s.hint_followed_estimated",
                  "s.verbosity_steer || s.verbosity_steer_estimated", "s.resume_lean_estimated",
                  "Number(s.total_tokens)"):
        assert field in card, field
    assert card.count("pushEst(obj, label)") == 4


def test_compactions_counted_from_transcript(sb, tmp_path):
    f = tmp_path / "t.jsonl"
    rows = ['{"type":"user"}', '{"type":"system","subtype":"compact_boundary"}', '{"type":"assistant"}',
            '{"type":"system","subtype":"compact_boundary"}']
    f.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert sb._status_bar_compactions(f) == 2
    assert sb._status_bar_compactions(tmp_path / "missing.jsonl") is None


# --------------------------------------------------------------------------
# Compaction count vs Claude Code's late compact_boundary write
# --------------------------------------------------------------------------

def test_post_compact_counts_a_compaction_the_transcript_does_not_show_yet(sb):
    # PostCompact runs before Claude Code appends the compact_boundary row.
    assert sb._settled_compactions(0, {"compactions": 0}, post_compact=True, now=1000) == 1
    # Once the boundary is written, the parse agrees instead of adding another.
    assert sb._settled_compactions(1, {"compactions": 1, "_compact_bumped_at": 1000}, post_compact=False, now=1100) == 1
    # A second PostCompact for the same compaction (within a minute) is not a second one.
    assert sb._settled_compactions(0, {"compactions": 1, "_compact_bumped_at": 1000}, post_compact=True, now=1030) == 1
    # The next real compaction, later: counted again.
    assert sb._settled_compactions(1, {"compactions": 1, "_compact_bumped_at": 1000}, post_compact=True, now=5000) == 2
    # Never lower than what was known; a parse that sees more wins.
    assert sb._settled_compactions(0, {"compactions": 3}, post_compact=False) == 3
    assert sb._settled_compactions(4, {"compactions": 3}, post_compact=True, now=9000) == 4


def test_recent_compact_boundary_reads_the_tail(sb, tmp_path):
    f = tmp_path / "t.jsonl"
    now = 1_800_000_000
    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    f.write_text('{"type":"user"}\n{"type":"system","subtype":"compact_boundary","timestamp":"%s"}\n{"type":"assistant"}\n' % iso(now - 30), encoding="utf-8")
    assert sb._recent_compact_boundary(f, now=now) is True
    assert sb._recent_compact_boundary(f, now=now + 600) is False
    g = tmp_path / "none.jsonl"
    g.write_text('{"type":"user"}\n', encoding="utf-8")
    assert sb._recent_compact_boundary(g, now=now) is False
    assert sb._recent_compact_boundary(tmp_path / "missing.jsonl", now=now) is False


def test_compactions_count_resumes_where_it_stopped_and_finds_split_markers(sb, tmp_path):
    f = tmp_path / "t.jsonl"
    mark = '{"type":"system","subtype":"compact_boundary"}\n'
    f.write_text('{"type":"user"}\n' + mark, encoding="utf-8")
    assert sb._status_bar_compactions(f, "sess-inc") == 1
    # The next row arrives in two writes, the marker split between them.
    half = len('{"type":"system","subtype":"compac')
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(mark[:half])
    assert sb._status_bar_compactions(f, "sess-inc") == 1
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(mark[half:] + mark)
    assert sb._status_bar_compactions(f, "sess-inc") == 3
    # A full re-read agrees.
    assert sb._status_bar_compactions(f) == 3
    # A real row after a long line, around the 1 MB mark.
    big = tmp_path / "big.jsonl"
    row = b'{"type":"system","subtype":"compact_boundary"}\n'
    for off in (-40, -1, 0, 1):
        big.write_bytes(b'{"type":"user","x":"' + b"x" * ((1 << 20) + off) + b'"}\n' + row)
        assert sb._status_bar_compactions(big) == 1, off
    # The same key inside a structured tool result is not a compaction.
    tool = tmp_path / "tool.jsonl"
    tool.write_text(json.dumps({"type": "user", "toolUseResult": {"type": "system", "subtype": "compact_boundary"}}) + "\n", encoding="utf-8")
    assert sb._status_bar_compactions(tool) == 0


def test_dashboard_headline_is_zero_when_nothing_was_measured(sb):
    assert sb._dashboard_saved_tokens({"total_tokens": 0, "hint_followed": {"tokens_saved": 500}}) == (0, 0)
    assert sb._dashboard_saved_tokens({"total_tokens": "1e3", "mcp_cap_estimated": {"tokens_saved": "12.5"}}) == (1012, 1000)


def test_post_compact_refresh_end_to_end_counts_once(sb, tmp_path):
    # The real refresh path: lease, parse, carry-forward, write.
    sid = "e2e00000-0000-4000-8000-000000000001"
    tr = tmp_path / f"{sid}.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": "2026-10-03T10:00:00.000Z"},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "ok"}],
                                          "usage": {"input_tokens": 10, "output_tokens": 2}}, "timestamp": "2026-10-03T10:00:01.000Z"},
    ]
    tr.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    cache = sb.QUALITY_CACHE_DIR / f"quality-cache-{sid}.json"

    def run(post_compact):
        sb.quality_cache(quiet=True, session_jsonl=str(tr), force=True, session_id=sid, post_compact=post_compact)
        return json.loads(cache.read_text(encoding="utf-8"))["compactions"]

    assert run(False) == 0
    # PostCompact: Claude Code has not written the boundary yet.
    assert run(True) == 1
    # Another refresh before the boundary lands: still 1.
    assert run(False) == 1
    # The boundary lands (fresh): a parse sees it and agrees; a second PostCompact does not add.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with open(tr, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": now_iso}) + "\n")
    assert run(False) == 1
    assert run(True) == 1


# --------------------------------------------------------------------------
# Gauntlet regressions
# --------------------------------------------------------------------------

def test_compaction_memo_never_moves_backwards(sb, tmp_path):
    f = tmp_path / "t.jsonl"
    mark = '{"type":"system","subtype":"compact_boundary"}\n'
    f.write_text(mark * 2, encoding="utf-8")
    assert sb._status_bar_compactions(f, "sess-memo") == 2
    memo = sb._status_bar_dir() / "compactions-sess-memo.json"
    good = json.loads(memo.read_text(encoding="utf-8"))
    assert good["size"] == f.stat().st_size  # where the read really ended
    # A slower reader that ended earlier must not overwrite the newer memo.
    # A memo from a reader that got further (the file has grown past what this call reads).
    memo.write_text(json.dumps({"path": str(f), "size": good["size"] + 999, "count": 7}), encoding="utf-8")
    sb._status_bar_compactions(f, "sess-memo")
    assert json.loads(memo.read_text(encoding="utf-8"))["count"] == 7


def test_boundary_probe_ignores_the_words_inside_a_message(sb, tmp_path):
    f = tmp_path / "t.jsonl"
    now = 1_800_000_000
    iso = datetime.fromtimestamp(now - 10, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    f.write_text(json.dumps({"type": "user", "timestamp": iso,
                             "message": {"content": 'a pasted log: "subtype":"compact_boundary"'}}) + "\n",
                 encoding="utf-8")
    assert sb._recent_compact_boundary(f, now=now) is False


def test_status_bar_json_never_carries_infinity_or_nan(sb):
    out = sb._status_bar_finite({"a": float("inf"), "b": [float("nan"), 1.5], "c": {"d": -float("inf")}})
    assert out == {"a": None, "b": [None, 1.5], "c": {"d": None}}
    json.dumps(out, allow_nan=False)


def test_long_session_ids_get_short_file_names(sb):
    long_id = "x" * 400
    name = sb._status_bar_cache_path(long_id).name
    assert len(name) < 120 and name.endswith(".json")
    assert sb._status_bar_cache_path("short-id-1") .name == "short-id-1.json"


def test_release_never_deletes_another_owners_lock(sb):
    sid = "aaaaaaaa-1111-2222-3333-555555555555"
    lock = sb._status_bar_lock_path(sid)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("someone-elses-token", encoding="ascii")
    assert sb._status_bar_release_lock(sid, "my-token") is False
    assert lock.read_text(encoding="ascii") == "someone-elses-token"
    assert sb._status_bar_release_lock(sid, "someone-elses-token") is True
    assert not lock.exists()


def test_earlier_checkpoint_outside_token_optimizer_folders_is_not_read(sb, tmp_path):
    outside = tmp_path / "elsewhere" / "88888888-cccc-20261003-120001-stop.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# not ours", encoding="utf-8")
    (sb._sb_claude / "token-optimizer" / f"resumable-{SID_A}.json").write_text(
        json.dumps({"checkpoint": str(outside), "relevant": True}), encoding="utf-8")
    assert sb._status_bar_earlier_checkpoint(SID_A) is None


def test_savings_that_net_to_zero_across_days_are_not_reported_busy(sb):
    # A re-expanded archive on a later day cancels the earlier day's credit:
    # the honest 30-day figure is 0 while one day still shows a saving.
    _seed(sb, rows=[
        (_day(3), "tool_archive", 500, 0.005, SID_A),
        (_day(1), "tool_archive_reexpand", 500, 0.005, SID_A),
    ])
    sav, reason = sb._status_bar_savings_or_reason(SID_A)
    assert reason is None
    assert sav["total_30d_measured_tokens"] == 0


def test_a_zero_headline_over_real_savings_is_reported_busy(sb, monkeypatch):
    _three_day_fixture(sb)
    monkeypatch.setattr(sb, "_get_merged_savings", lambda days=30, since=None: {
        "total_tokens": 0, "total_cost_usd": 0.0})
    assert sb._status_bar_savings_or_reason(SID_A) == (None, "savings database busy")


def test_put_back_without_hard_links_never_replaces_a_newer_lock(sb, monkeypatch):
    sid = "aaaaaaaa-1111-2222-3333-666666666666"
    lock = sb._status_bar_lock_path(sid)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("old-owner", encoding="ascii")

    def no_links(src, dst):
        # A newer owner takes the name the moment ours is moved aside.
        lock.write_text("newer-owner", encoding="ascii")
        raise OSError("hard links not supported")

    monkeypatch.setattr(sb.os, "link", no_links)
    assert sb._status_bar_release_lock(sid, "my-token") is False
    assert lock.read_text(encoding="ascii") == "newer-owner"
    assert not list(lock.parent.glob(f"{lock.name}.*.release"))


def test_compaction_memo_off_a_line_boundary_recounts(sb, tmp_path):
    f = tmp_path / "t.jsonl"
    mark = '{"type":"system","subtype":"compact_boundary"}\n'
    f.write_text(mark * 3, encoding="utf-8")
    memo = sb._status_bar_dir() / "compactions-sess-mid.json"
    memo.parent.mkdir(parents=True, exist_ok=True)
    # A memo that stopped partway into the second row.
    memo.write_text(json.dumps({"path": str(f), "size": len(mark) + 5, "count": 1}), encoding="utf-8")
    assert sb._status_bar_compactions(f, "sess-mid") == 3


# --------------------------------------------------------------------------
# --session takes a pasted/truncated id
# --------------------------------------------------------------------------

def test_truncated_session_id_resolves_a_unique_prefix(sb):
    """`status-bar --session aaaaaaaa`: the truncated ids our own listings
    print must resolve to the real session, not degrade to an empty report."""
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_1h_input_tokens": 10}}),
    ])
    out = sb.status_bar_payload("aaaaaaaa")
    assert out["session_id"] == SID_A
    assert out["cache_lifetime"] == "1h"
    assert out["last_request_epoch"] is not None


def test_short_session_id_under_6_chars_resolves_a_unique_prefix(sb):
    """`--session aaaa` sanitizes to "unknown" today; a unique prefix match
    should still find the one session it can only mean."""
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_1h_input_tokens": 10}}),
    ])
    out = sb.status_bar_payload("aaaa")
    assert out["session_id"] == SID_A
    assert out["cache_lifetime"] == "1h"


def test_ambiguous_session_id_prefix_says_so(sb):
    """Two sessions share the prefix: refuse to guess and say why."""
    for sid in ("aaaa1111-0000-4000-8000-000000000001",
                "aaaa2222-0000-4000-8000-000000000002"):
        _write_transcript(sb, [
            _assistant("2026-10-03T10:00:00Z",
                       {"cache_creation": {"ephemeral_1h_input_tokens": 10}}),
        ], sid=sid)
    out = sb.status_bar_payload("aaaa")
    assert out["savings"] is None
    assert out["savings_reason"]
    assert "ambiguous" in out["savings_reason"]
    assert "2" in out["savings_reason"]


def test_session_id_prefix_matching_nothing_says_so(sb):
    """Zero matches: name the prefix that matched nothing, not 'unknown'."""
    out = sb.status_bar_payload("zz")
    assert out["session_id"] == "unknown"
    assert out["savings_reason"]
    assert "zz" in out["savings_reason"]


def test_full_session_id_unaffected_by_prefix_resolution(sb):
    """A real full id with no transcript keeps the existing behaviour."""
    out = sb.status_bar_payload(SID_A)
    assert out["session_id"] == SID_A
    assert out["savings_reason"] != "no session id"


@pytest.mark.parametrize("seps", [(",", ":"), (", ", ": "), (",", ": "), (" , ", " : ")])
def test_compactions_counted_whatever_the_json_spacing(sb, tmp_path, seps):
    """the cheap prefilter must not depend on how a writer spaced the
    JSON; the parsed row decides. A message that merely quotes the marker
    does not count."""
    now = 1_800_000_000
    iso = datetime.fromtimestamp(now - 30, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = [{"type": "user", "message": {"content": 'the word "subtype": "compact_boundary" in text'}},
            {"type": "system", "subtype": "compact_boundary", "timestamp": iso},
            {"type": "assistant"},
            {"type": "system", "subtype": "compact_boundary", "timestamp": iso}]
    f = tmp_path / "spaced.jsonl"
    f.write_text("\n".join(json.dumps(r, separators=seps) for r in rows) + "\n", encoding="utf-8")
    assert sb._status_bar_compactions(f) == 2
    assert sb._recent_compact_boundary(f, now=now) is True
