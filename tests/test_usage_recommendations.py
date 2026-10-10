"""Usage recommendations: the daily-measured record both usage-based
recommendations are read from.

Contract:
  * `usage_recommendations.json` (schema 1) lives in the Token Optimizer data
    dir and carries one item per usage-based recommendation: ``compact_window``
    and ``subagent_cache``. States: recommend | keep | not_enough_data |
    not_applicable. ``command`` is empty unless the state is ``recommend``.
  * `measure.py recommendations refresh` is the ONE refresher: it reuses the
    bounded compact_advice replay and the cached subagent-cache payoff verdict
    (running the detached scan when needed), and runs detached via
    spawn_detached with an O_EXCL lock -- never inline inside a hook, never a
    console window on Windows.
  * Session start and the dashboard data collection spawn the refresh only
    when the record is missing or older than _RECS_REFRESH_SECONDS (24h).
    A failed/partial run writes complete=false and is retried the next day.
  * `usage_recommendations_history.jsonl` gets one compact line per refresh
    (measured_ts + per-item state + observed setting + measure), capped at 180
    lines and rewritten atomically when over.
  * A win is claimed only when an earlier history line said ``recommend``, the
    observed setting later changed to the recommended value, at least 7 days
    passed, the minimum data bar holds, and the measure moved the right way --
    worded "since you changed it", never causal.
  * quick/doctor/status/coach/dashboard read the record only and never scan
    themselves; not_applicable items are not rendered.
  * Token Optimizer NEVER writes either setting.

Run: python3 -m pytest tests/test_usage_recommendations.py -q
"""

from __future__ import annotations

import inspect
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"
DASHBOARD = REPO / "skills" / "token-optimizer" / "assets" / "dashboard.html"

KEY = "subagentPromptCacheTtl"

USER_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "opus",
    "env": {"MY_KEY": "keep-me"},
}


def _write_settings(path: Path, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(USER_SETTINGS if data is None else data, indent=2),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def m(tmp_path, monkeypatch):
    """Fresh measure import pinned to a temp home + snapshot dir."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(data_dir))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", MEASURE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "claude"
    home.mkdir(parents=True, exist_ok=True)
    settings = _write_settings(home / "settings.json")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "_SETTINGS_LOCK_PATH", home / ".settings.lock")
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(mod, "is_cowork", lambda: False)
    monkeypatch.setattr(mod, "_subagent_cache_claude_code_version",
                        lambda: (2, 1, 250))
    monkeypatch.setattr(mod, "keepwarm_billing_mode", lambda *a, **k: "api")
    monkeypatch.delenv("TOKEN_OPTIMIZER_SUBAGENT_CACHE_1H", raising=False)
    mod._spawn_log = []

    def _fake_spawn(argv, **kw):
        mod._spawn_log.append((list(argv), kw))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(mod, "spawn_detached", _fake_spawn)
    yield mod, settings, home
    sys.modules.pop("measure", None)


def _verdict(m, *, requests=500, saved=2.0, premium=1.0, tokens=1_234_567,
             complete=True, age=60.0, since_ts=None, now=None):
    """Write a cached verdict the way the background scan would."""
    mod, _s, _h = m
    now = time.time() if now is None else now
    payoff = mod._subagent_cache_payoff_zero(30)
    payoff.update(
        subagent_requests=requests, savings_usd_est=saved,
        extra_write_cost_usd_est=premium, net_usd_est=saved - premium,
        within_agent_tokens=tokens, missed_read_tokens=tokens)
    rec = {"version": 1, "ts": now - age, "complete": complete,
           "since_ts": since_ts, "window_days": 30,
           "payoff": payoff if complete else None,
           "reason": None if complete else "time budget exceeded"}
    p = mod._subagent_cache_verdict_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _win(window, extra, avoided, share, net, reference=False):
    return {
        "window": window,
        "label": f"{window // 1000}K",
        "is_default": reference,
        "reference": reference,
        "extra_compactions": extra,
        "affected_sessions": extra,
        "affected_share": 0.5,
        "cache_read_tokens_avoided": avoided,
        "avoided_share_pct": share,
        "compaction_cost_tokens": extra * 34000,
        "net_tokens": avoided - extra * 34000,
        "net_usd": round(net, 2),
        "net_usd_unrounded": net,
        "estimate": True,
    }


def _compact_report(*, sessions=80, days=30, total_cache_read=4_000_000,
                    current=967_000, windows=None, too_thin=False,
                    truncated=False):
    """A compact_advice() report the way measure.py builds it."""
    if windows is None:
        windows = [
            _win(current, 0, 0, 0.0, 0.0, reference=True),
            _win(400_000, 60, 1_000_000, 25.0, 2.0),
            _win(300_000, 90, 1_200_000, 30.0, 1.5),
        ]
    return {
        "days": days,
        "sessions_scanned": sessions,
        "sessions_replayed": sessions,
        "recorded_compactions": 40,
        "total_cache_read_tokens": total_cache_read,
        "too_thin": too_thin,
        "truncated": truncated,
        "default_window": {"tokens": current,
                           "source": "default (~967K for 1M-native models)",
                           "model": "claude-opus-5-5"},
        "windows": windows,
        "smallest_positive_window": None,
        "measurements": {},
        "assumptions": {},
        "quality_by_fill_band": {},
        "billing": "api",
    }


def _stub_compact(m, monkeypatch, report=None, exc=None):
    mod, _s, _h = m
    if exc is not None:
        monkeypatch.setattr(mod, "compact_advice",
                            lambda *a, **k: (_ for _ in ()).throw(exc))
    else:
        monkeypatch.setattr(mod, "compact_advice",
                            lambda *a, **k: report)


def _write_record(m, items, measured_ts=None, complete=True):
    mod, _s, _h = m
    rec = {"schema": 1, "measured_ts": measured_ts if measured_ts is not None
           else time.time(), "window_days": 30, "complete": complete,
           "items": items}
    p = mod._recs_record_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _read_record(m):
    mod, _s, _h = m
    return json.loads(mod._recs_record_path().read_text(encoding="utf-8"))


def _hist_entry(ts, compact=None, subagent=None):
    items = {}
    if compact is not None:
        items["compact_window"] = compact
    if subagent is not None:
        items["subagent_cache"] = subagent
    return {"ts": ts, "items": items}


def _write_history(m, entries):
    mod, _s, _h = m
    p = mod._recs_history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    return p


def _read_history(m):
    mod, _s, _h = m
    p = mod._recs_history_path()
    if not p.exists():
        return []
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _rec_items(m, rec=None):
    rec = _read_record(m) if rec is None else rec
    return {i["id"]: i for i in rec["items"]}


# ===========================================================================
# Regression first: the doubled "would not pay" sentence (status + doctor).
# ===========================================================================

def _live_payoff(mod, monkeypatch, *, requests=500, saved=2.0, premium=1.0):
    """subagent-cache status scans live: pin what the scan would find."""
    payoff = mod._subagent_cache_payoff_zero(30)
    payoff.update(subagent_requests=requests, savings_usd_est=saved,
                  extra_write_cost_usd_est=premium,
                  net_usd_est=saved - premium)
    monkeypatch.setattr(mod, "subagent_cache_payoff",
                        lambda *a, **k: payoff)
    return payoff


def test_status_no_doubled_verdict_sentence(m, monkeypatch, capsys):
    """verdict: would-not-pay: would not pay: ... plus the identical advice:
    line was printed twice. One verdict line, and the advice line is dropped
    when it repeats the verdict reason verbatim."""
    mod, _s, _h = m
    _live_payoff(mod, monkeypatch, saved=0.40, premium=1.0)
    mod._subagent_cache_cli(["subagent-cache", "status"])
    out = capsys.readouterr().out
    assert "would-not-pay: would not pay" not in out
    assert "would not pay: would not pay" not in out
    # The verdict reason appears exactly once across the whole output.
    assert out.count("saved $0.40 vs premium $1.00") == 1


def test_status_advice_kept_when_it_adds_a_command(m, monkeypatch, capsys):
    mod, _s, _h = m
    _live_payoff(mod, monkeypatch, saved=2.0, premium=1.0)
    mod._subagent_cache_cli(["subagent-cache", "status"])
    out = capsys.readouterr().out
    assert "advice:" in out
    assert "subagent-cache enable" in out
    # ...and still no doubled label on the verdict line.
    assert "recommend: pays:" not in out.replace("verdict: ", "")


def test_doctor_no_doubled_advice(m, capsys):
    mod, _s, _h = m
    _verdict(m, requests=500, saved=0.40, premium=1.0)
    mod.doctor()
    out = capsys.readouterr().out
    assert out.count("would not pay") <= 1


def test_quick_never_doubles(m, capsys):
    mod, _s, _h = m
    _verdict(m, requests=500, saved=0.40, premium=1.0)
    mod.quick_scan()
    out = capsys.readouterr().out
    assert "would not pay: would not pay" not in out


# ===========================================================================
# The record: schema, item states, candidate rules.
# ===========================================================================

def test_refresh_writes_schema1_record(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    now = time.time()
    rec = mod.usage_recommendations_refresh(now=now)
    assert rec is not None
    on_disk = _read_record(m)
    assert on_disk["schema"] == 1
    assert on_disk["measured_ts"] == now
    assert on_disk["window_days"] == 30
    assert on_disk["complete"] is True
    assert {i["id"] for i in on_disk["items"]} == {
        "compact_window", "subagent_cache"}
    allowed = {"recommend", "keep", "not_enough_data", "not_applicable"}
    for it in on_disk["items"]:
        for key in ("id", "state", "headline", "numbers", "command",
                    "direction", "enough_data", "reason"):
            assert key in it, key
        assert it["state"] in allowed
        if it["state"] != "recommend":
            assert it["command"] == ""


def test_compact_item_recommends_best_priced_net(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=0.40, premium=1.0)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["compact_window"]
    assert it["state"] == "recommend"
    # 400K nets +$2.00 against 300K's +$1.50: the best priced net wins.
    assert it["numbers"]["recommended"] == 400_000
    assert it["numbers"]["setting"] == 967_000
    assert it["command"] == "/autocompact 400000"
    assert "400K" in it["headline"]
    assert "Compaction can drop early instructions" in it["tradeoff"]
    assert it["direction"] == "smaller"
    assert it["enough_data"] is True


def test_compact_item_keep_below_ten_percent_share(m, monkeypatch):
    mod, _s, _h = m
    rep = _compact_report(windows=[
        _win(967_000, 0, 0, 0.0, 0.0, reference=True),
        _win(400_000, 10, 300_000, 7.5, 3.0),
    ])
    _stub_compact(m, monkeypatch, rep)
    _verdict(m)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["compact_window"]
    assert it["state"] == "keep"
    assert it["command"] == ""


def test_compact_item_keep_when_compactions_over_cap(m, monkeypatch):
    mod, _s, _h = m
    rep = _compact_report(days=30, windows=[
        _win(967_000, 0, 0, 0.0, 0.0, reference=True),
        _win(400_000, 200, 2_000_000, 50.0, 4.0),  # ~6.7/day: over the cap
    ])
    _stub_compact(m, monkeypatch, rep)
    _verdict(m)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["compact_window"]
    assert it["state"] == "keep"
    assert it["command"] == ""


def test_compact_item_not_enough_data_when_thin(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch,
                  _compact_report(sessions=2, too_thin=True, windows=[]))
    _verdict(m)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["compact_window"]
    assert it["state"] == "not_enough_data"
    assert it["enough_data"] is False
    assert it["command"] == ""


def test_subagent_item_recommends_enable(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=2.0, premium=1.0)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["subagent_cache"]
    assert it["state"] == "recommend"
    assert it["command"].endswith("subagent-cache enable")
    assert it["numbers"]["recommended"] == "1h"
    assert it["numbers"]["setting"] is None
    assert it["enough_data"] is True


def test_subagent_item_recommends_disable(m, monkeypatch):
    mod, settings, _h = m
    _write_settings(settings, dict(USER_SETTINGS, **{KEY: "1h"}))
    set_ts = time.time() - 20 * 86400
    mod._subagent_cache_write_marker(
        {"state": "set", "set_ts": set_ts, "set_by": "token-optimizer"})
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=0.50, premium=1.0,
             since_ts=set_ts)   # net -0.50
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["subagent_cache"]
    assert it["state"] == "recommend"
    assert it["command"].endswith("subagent-cache disable")
    assert it["numbers"]["recommended"] == "off"
    assert it["numbers"]["setting"] == "1h"


def test_subagent_item_user_set_key_is_manual_advice_without_a_command(m, monkeypatch):
    """A key the user set by hand cannot be undone by `subagent-cache
    disable` (no marker says we set it), so the item must not hand out that
    command; the advice is to remove the key by hand."""
    mod, settings, _h = m
    _write_settings(settings, dict(USER_SETTINGS, **{KEY: "1h"}))
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=0.50, premium=1.0)   # net -0.50
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["subagent_cache"]
    assert it["state"] == "recommend"
    assert it["command"] == ""
    assert it["direction"] == "off"
    assert it["numbers"]["recommended"] == "off"
    assert "subagent-cache disable" not in json.dumps(it)
    assert "by hand" in (it["tradeoff"] + it["headline"])
    st = mod.subagent_cache_status(use_cache=True)
    assert st["recommendation"]["action"] == "disable"
    assert "subagent-cache disable" not in st["recommendation"]["line"]
    assert "by hand" in st["recommendation"]["line"]


def test_subagent_item_keep_when_paying(m, monkeypatch):
    mod, settings, _h = m
    _write_settings(settings, dict(USER_SETTINGS, **{KEY: "1h"}))
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=3.0, premium=1.0)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["subagent_cache"]
    assert it["state"] == "keep"
    assert it["command"] == ""


def test_subagent_item_not_enough_data_when_thin(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=mod._SUBAGENT_CACHE_TRIPWIRE_MIN_REQUESTS - 1,
             saved=50.0, premium=1.0)
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["subagent_cache"]
    assert it["state"] == "not_enough_data"
    assert it["enough_data"] is False
    assert it["command"] == ""


def test_subagent_item_not_enough_data_without_verdict(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    # The scan is held by another process: the refresh never scans inline.
    monkeypatch.setattr(mod, "subagent_cache_scan_run",
                        lambda *a, **k: None)
    monkeypatch.setattr(mod, "subagent_cache_payoff",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("refresh must not scan inline")))
    rec = mod.usage_recommendations_refresh()
    it = _rec_items(m, rec)["subagent_cache"]
    assert it["state"] == "not_enough_data"


def test_items_not_applicable_on_foreign_runtime(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.setattr(mod, "detect_runtime", lambda: "codex")
    _stub_compact(m, monkeypatch, _compact_report())
    rec = mod.usage_recommendations_refresh()
    items = _rec_items(m, rec)
    assert items["compact_window"]["state"] == "not_applicable"
    assert items["subagent_cache"]["state"] == "not_applicable"
    assert all(i["command"] == "" for i in items.values())


def test_items_not_applicable_in_cowork(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.setattr(mod, "is_cowork", lambda: True)
    _stub_compact(m, monkeypatch, _compact_report())
    rec = mod.usage_recommendations_refresh()
    items = _rec_items(m, rec)
    assert all(i["state"] == "not_applicable" for i in items.values())


def test_failed_run_writes_complete_false(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, exc=RuntimeError("boom"))
    monkeypatch.setattr(mod, "subagent_cache_scan_run", lambda *a, **k: None)
    rec = mod.usage_recommendations_refresh()
    on_disk = _read_record(m)
    assert on_disk["complete"] is False
    assert _rec_items(m, rec)["compact_window"]["state"] == "not_enough_data"


def test_partial_compact_run_is_incomplete(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report(truncated=True))
    _verdict(m)
    rec = mod.usage_recommendations_refresh()
    assert rec["complete"] is False


def test_partial_compact_replay_never_recommends(m, monkeypatch):
    """A replay cut short counts only some sessions' extra compactions, so its
    per-day figure is too low and it would pick too small a window."""
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report(truncated=True))
    item = mod._recs_compact_item()
    assert item["state"] == "not_enough_data"
    assert item["command"] == ""
    assert item["enough_data"] is False
    assert item["numbers"]["recommended"] == item["numbers"]["setting"]


def test_refresh_replays_the_whole_window_not_the_coach_slice(m, monkeypatch):
    """The refresh is detached, so it takes the full replay with its own
    budget; the coach's 150-session / 4-second cap is for inline callers."""
    mod, _s, _h = m
    seen = {}

    def _advice(*a, **k):
        seen.update(k)
        return _compact_report()

    monkeypatch.setattr(mod, "compact_advice", _advice)
    mod._recs_compact_item()
    assert seen.get("max_sessions") is None
    assert seen.get("deadline_seconds") == mod._RECS_COMPACT_BUDGET_SECONDS
    assert mod._RECS_COMPACT_BUDGET_SECONDS >= 60


def test_lock_blocks_a_second_refresh(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m)
    lock = mod._recs_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held-by-another-process", encoding="utf-8")
    os.utime(lock, (time.time(), time.time()))
    assert mod.usage_recommendations_refresh() is None
    assert not mod._recs_record_path().exists()


def test_stale_lock_is_reclaimed(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m)
    lock = mod._recs_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("abandoned", encoding="utf-8")
    old = time.time() - (mod._RECS_LOCK_STALE + 60)
    os.utime(lock, (old, old))
    assert mod.usage_recommendations_refresh() is not None
    assert mod._recs_record_path().exists()


def test_wrong_token_refresh_does_nothing(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    assert mod.usage_recommendations_refresh(token="not-the-lock") is None
    assert not mod._recs_record_path().exists()


def test_refresh_releases_the_lock(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m)
    mod.usage_recommendations_refresh()
    assert not mod._recs_lock_path().exists()


# ===========================================================================
# Spawn: missing/stale record -> detached child; fresh -> nothing.
# ===========================================================================

def test_ensure_fresh_spawns_detached_when_missing(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert mod._recs_ensure_fresh() is True
    assert len(mod._spawn_log) == 1
    argv, kw = mod._spawn_log[0]
    assert Path(argv[1]).name == "measure.py"
    assert argv[-2:] == ["recommendations", "refresh"]
    assert argv[0] == mod._detached_python_exe()
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.DEVNULL
    assert kw["env"][mod._RECS_TOKEN_ENV]
    assert kw["env"]["TOKEN_OPTIMIZER_RUNTIME"] == "claude"
    # The lock is held by the spawned child: a second call does not respawn.
    assert mod._recs_ensure_fresh() is False
    assert len(mod._spawn_log) == 1


def test_ensure_fresh_no_spawn_when_fresh(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _write_record(m, [], measured_ts=time.time() - 3600)
    assert mod._recs_ensure_fresh() is False
    assert mod._spawn_log == []


def test_ensure_fresh_respawns_after_a_day(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    stale = time.time() - mod._RECS_REFRESH_SECONDS - 60
    _write_record(m, [], measured_ts=stale)
    assert mod._recs_ensure_fresh() is True
    assert len(mod._spawn_log) == 1


def test_ensure_fresh_never_spawns_under_pytest_by_default(m):
    mod, _s, _h = m
    # PYTEST_CURRENT_TEST is set by the harness: the auto path must not
    # launch a real detached child inside a test run.
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert mod._recs_ensure_fresh() is False
    assert mod._spawn_log == []


def test_ensure_fresh_never_spawns_on_foreign_runtime(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "hermes")
    assert mod._recs_ensure_fresh() is False
    assert mod._spawn_log == []


def test_spawn_goes_through_spawn_detached_only(m):
    mod, _s, _h = m
    for fn in (mod._recs_spawn_refresh, mod._recs_ensure_fresh):
        body = inspect.getsource(fn)
        assert "Popen" not in body
        assert "os.system" not in body
        assert "subprocess.run" not in body


# ===========================================================================
# History + wins.
# ===========================================================================

def test_history_line_per_refresh(m, monkeypatch):
    mod, _s, _h = m
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, saved=2.0, premium=1.0)
    mod.usage_recommendations_refresh(now=time.time())
    mod.usage_recommendations_refresh(now=time.time() + 60)
    lines = _read_history(m)
    assert len(lines) == 2
    for ln in lines:
        assert isinstance(ln["ts"], (int, float))
        entry = ln["items"]["compact_window"]
        for k in ("state", "setting", "recommended", "measure"):
            assert k in entry, k


def test_history_capped_and_atomic(m, monkeypatch):
    mod, _s, _h = m
    _write_history(m, [_hist_entry(1000 + i, compact={
        "state": "keep", "setting": 967000, "recommended": 967000,
        "measure": 100.0}) for i in range(mod._RECS_HISTORY_CAP + 10)])
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m)
    mod.usage_recommendations_refresh()
    lines = _read_history(m)
    assert len(lines) == mod._RECS_HISTORY_CAP
    # The oldest retained line is no older than the cap window allows.
    assert lines[0]["ts"] == 1000 + 11


def test_compact_win_after_adoption(m, monkeypatch):
    mod, _s, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 30 * 86400, compact={
            "state": "recommend", "setting": 967000, "recommended": 400000,
            "measure": 50000.0}),
        _hist_entry(now - 10 * 86400, compact={
            "state": "keep", "setting": 400000, "recommended": 400000,
            "measure": 40000.0}),
    ])
    # Current measurement: the user is on 400K and cache reads per session fell.
    rep = _compact_report(current=400_000, total_cache_read=2_400_000,
                          sessions=80, windows=[
        _win(400_000, 0, 0, 0.0, 0.0, reference=True)])
    _stub_compact(m, monkeypatch, rep)
    _verdict(m)
    rec = mod.usage_recommendations_refresh(now=now)
    wins = [w for w in rec["wins"] if w["id"] == "compact_window"]
    assert len(wins) == 1
    w = wins[0]
    assert w["improved"] is True
    assert w["before"] == 50000.0 and w["after"] == 30000.0
    assert "since you" in w["line"].lower()
    assert "estimate" in w["line"].lower()
    # The record carries it too.
    assert _read_record(m)["wins"][0]["id"] == "compact_window"


def test_no_compact_win_when_measure_worsened(m, monkeypatch):
    mod, _s, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 30 * 86400, compact={
            "state": "recommend", "setting": 967000, "recommended": 400000,
            "measure": 50000.0}),
        _hist_entry(now - 10 * 86400, compact={
            "state": "keep", "setting": 400000, "recommended": 400000,
            "measure": 60000.0}),
    ])
    rep = _compact_report(current=400_000, total_cache_read=4_800_000,
                          sessions=80, windows=[
        _win(400_000, 0, 0, 0.0, 0.0, reference=True)])
    _stub_compact(m, monkeypatch, rep)
    _verdict(m)
    rec = mod.usage_recommendations_refresh(now=now)
    wins = [w for w in rec["wins"] if w["id"] == "compact_window"]
    # No win is claimed; the numbers are still stated plainly.
    if wins:
        assert wins[0]["improved"] is False
        assert "50,000" in wins[0]["line"] or "50000" in wins[0]["line"]
        assert "60,000" in wins[0]["line"] or "60000" in wins[0]["line"]


def test_no_win_before_seven_days(m, monkeypatch):
    mod, _s, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 10 * 86400, compact={
            "state": "recommend", "setting": 967000, "recommended": 400000,
            "measure": 50000.0}),
        _hist_entry(now - 3 * 86400, compact={
            "state": "keep", "setting": 400000, "recommended": 400000,
            "measure": 30000.0}),
    ])
    rep = _compact_report(current=400_000, windows=[
        _win(400_000, 0, 0, 0.0, 0.0, reference=True)])
    _stub_compact(m, monkeypatch, rep)
    _verdict(m)
    rec = mod.usage_recommendations_refresh(now=now)
    assert [w for w in rec["wins"] if w["id"] == "compact_window"] == []


def test_no_win_without_min_data(m, monkeypatch):
    mod, _s, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 30 * 86400, compact={
            "state": "recommend", "setting": 967000, "recommended": 400000,
            "measure": 50000.0}),
        _hist_entry(now - 10 * 86400, compact={
            "state": "keep", "setting": 400000, "recommended": 400000,
            "measure": 30000.0}),
    ])
    # Current measurement is too thin: enough_data False -> no win claim.
    _stub_compact(m, monkeypatch,
                  _compact_report(sessions=2, too_thin=True, windows=[]))
    _verdict(m)
    rec = mod.usage_recommendations_refresh(now=now)
    assert [w for w in rec["wins"] if w["id"] == "compact_window"] == []


def test_no_win_after_revert(m, monkeypatch):
    mod, _s, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 30 * 86400, compact={
            "state": "recommend", "setting": 967000, "recommended": 400000,
            "measure": 50000.0}),
        _hist_entry(now - 10 * 86400, compact={
            "state": "keep", "setting": 400000, "recommended": 400000,
            "measure": 30000.0}),
    ])
    # The user went back to 967K: no adoption stands.
    _stub_compact(m, monkeypatch, _compact_report(current=967_000))
    _verdict(m)
    rec = mod.usage_recommendations_refresh(now=now)
    assert [w for w in rec["wins"] if w["id"] == "compact_window"] == []


def test_subagent_enable_win(m, monkeypatch):
    mod, settings, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 30 * 86400, subagent={
            "state": "recommend", "setting": None, "recommended": "1h",
            "measure": 1.5}),
        _hist_entry(now - 10 * 86400, subagent={
            "state": "keep", "setting": "1h", "recommended": "1h",
            "measure": 1.8}),
    ])
    _write_settings(settings, dict(USER_SETTINGS, **{KEY: "1h"}))
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=3.0, premium=1.0)   # net +2.0 now
    rec = mod.usage_recommendations_refresh(now=now)
    wins = [w for w in rec["wins"] if w["id"] == "subagent_cache"]
    assert len(wins) == 1
    assert wins[0]["improved"] is True
    assert "since you" in wins[0]["line"].lower()
    assert "estimate" in wins[0]["line"].lower()


def test_subagent_disable_win(m, monkeypatch):
    mod, _s, _h = m
    now = time.time()
    _write_history(m, [
        _hist_entry(now - 30 * 86400, subagent={
            "state": "recommend", "setting": "1h", "recommended": "off",
            "measure": -0.5}),
        _hist_entry(now - 10 * 86400, subagent={
            "state": "keep", "setting": None, "recommended": "off",
            "measure": -0.4}),
    ])
    _stub_compact(m, monkeypatch, _compact_report())
    _verdict(m, requests=500, saved=0.4, premium=1.0)   # still would not pay
    rec = mod.usage_recommendations_refresh(now=now)
    wins = [w for w in rec["wins"] if w["id"] == "subagent_cache"]
    assert len(wins) == 1
    assert wins[0]["improved"] is True


# ===========================================================================
# Surfaces read the record only.
# ===========================================================================

def _record_with_items(m):
    """A complete record: compact recommends, subagent keeps."""
    return _write_record(m, [
        {"id": "compact_window", "state": "recommend",
         "headline": "compacting at 400K instead of 967K would have avoided "
                     "25% of cache-read tokens",
         "numbers": {"setting": 967000, "recommended": 400000},
         "command": "/autocompact 400000", "direction": "smaller",
         "enough_data": True, "reason": "best priced net",
         "tradeoff": "Compaction can drop early instructions."},
        {"id": "subagent_cache", "state": "keep",
         "headline": "is paying: +$2.00 net (estimate) over the last 30 days",
         "numbers": {"setting": "1h", "recommended": "1h", "measure": 2.0},
         "command": "", "direction": "", "enough_data": True,
         "reason": "net positive"},
    ])


def test_quick_prints_recommend_items_only(m, capsys):
    mod, _s, _h = m
    _record_with_items(m)
    mod.quick_scan()
    out = capsys.readouterr().out
    assert "/autocompact 400000" in out
    assert "Compact window" in out
    # The keep item does not earn a quick line.
    assert "is paying" not in out


def test_quick_silent_when_no_recommend(m, capsys):
    mod, _s, _h = m
    _write_record(m, [
        {"id": "compact_window", "state": "keep",
         "headline": "your current setting fits your usage",
         "numbers": {"setting": 967000}, "command": "", "direction": "",
         "enough_data": True, "reason": "no better window"},
        {"id": "subagent_cache", "state": "keep",
         "headline": "is paying", "numbers": {"setting": "1h"},
         "command": "", "direction": "", "enough_data": True,
         "reason": "net positive"},
    ])
    mod.quick_scan()
    out = capsys.readouterr().out
    assert "USAGE RECOMMENDATIONS" not in out
    assert "/autocompact" not in out


def test_quick_json_carries_usage_recommendations(m):
    mod, _s, _h = m
    _record_with_items(m)
    result = mod.quick_scan(as_json=True)
    blk = result.get("usage_recommendations")
    assert isinstance(blk, dict)
    assert {i["id"] for i in blk["items"]} == {
        "compact_window", "subagent_cache"}


def test_doctor_one_line_per_item_any_state(m, capsys):
    mod, _s, _h = m
    _record_with_items(m)
    mod.doctor()
    out = capsys.readouterr().out
    assert "/autocompact 400000" in out
    assert "your current setting fits your usage" in out or "is paying" in out


def test_doctor_json_carries_usage_recommendations(m):
    mod, _s, _h = m
    _record_with_items(m)
    result = mod.doctor(as_json=True)
    blk = result.get("usage_recommendations")
    assert isinstance(blk, dict) and blk.get("items")


def test_coach_json_carries_usage_recommendations(m, capsys):
    mod, _s, _h = m
    _record_with_items(m)
    mod._coach_cli(["coach", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    blk = data.get("usage_recommendations")
    assert isinstance(blk, dict) and blk.get("items")


def test_coach_text_prints_recommendation(m, capsys):
    mod, _s, _h = m
    _record_with_items(m)
    mod._coach_cli(["coach"])
    out = capsys.readouterr().out
    assert "/autocompact 400000" in out


def test_recommendations_cli_status(m, capsys):
    mod, _s, _h = m
    _record_with_items(m)
    mod._recs_cli(["recommendations"])
    out = capsys.readouterr().out
    assert "Compact window" in out
    assert "/autocompact 400000" in out


def test_recommendations_cli_json(m, capsys):
    mod, _s, _h = m
    _record_with_items(m)
    mod._recs_cli(["recommendations", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert "items" in data


def test_surfaces_never_scan(m, monkeypatch, capsys):
    """quick/doctor/coach/recommendations must answer from the record:
    no compact_advice replay, no payoff scan, no spawn."""
    mod, _s, _h = m
    _record_with_items(m)

    def _boom(*a, **k):
        raise AssertionError("surface scanned instead of reading the record")

    monkeypatch.setattr(mod, "compact_advice", _boom)
    monkeypatch.setattr(mod, "subagent_cache_payoff", _boom)
    monkeypatch.setattr(mod, "subagent_cache_scan_run", _boom)
    mod.quick_scan()
    mod.doctor()
    mod._coach_cli(["coach", "--json"])
    mod._recs_cli(["recommendations"])
    capsys.readouterr()


def test_not_applicable_items_not_rendered(m, capsys):
    mod, _s, _h = m
    _write_record(m, [
        {"id": "compact_window", "state": "not_applicable", "headline": "",
         "numbers": {}, "command": "", "direction": "", "enough_data": False,
         "reason": "not Claude Code"},
        {"id": "subagent_cache", "state": "not_applicable", "headline": "",
         "numbers": {}, "command": "", "direction": "", "enough_data": False,
         "reason": "not Claude Code"},
    ])
    mod.quick_scan()
    quick_out = capsys.readouterr().out
    assert "not applicable" not in quick_out.lower()
    mod._recs_cli(["recommendations"])
    recs_out = capsys.readouterr().out
    assert "not applicable" not in recs_out.lower()


# ===========================================================================
# The session start + dashboard data collection spawn the refresh.
# ===========================================================================

def test_session_start_spawns_refresh_when_stale(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    mod.run_ensure_health()
    assert any(a[-2:] == ["recommendations", "refresh"]
               for a, _kw in mod._spawn_log)


def test_coach_data_spawns_refresh_and_carries_block(m, monkeypatch):
    mod, _s, _h = m
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    data = mod.generate_coach_data()
    assert any(a[-2:] == ["recommendations", "refresh"]
               for a, _kw in mod._spawn_log)
    assert data.get("usage_recommendations") is not None
    assert data["usage_recommendations"]["present"] is False


# ===========================================================================
# Dashboard source assertions (same pattern as test_kill_stale_hints.py).
# ===========================================================================

def test_dashboard_has_from_your_own_usage_section():
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "From your own usage" in src


def test_dashboard_escapes_inserted_strings():
    src = DASHBOARD.read_text(encoding="utf-8")
    for needle in ("esc(it.headline", "esc(it.command)", "esc(w.line)"):
        assert needle in src, needle


def test_dashboard_missing_record_line_and_copy_pattern():
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "Measuring in the background, check back after your next session" in src
    assert "data-usage-rec-cmd" in src
    assert "copyHookCmd" in src


def test_dashboard_hides_not_applicable_and_empty_wins():
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "not_applicable" in src
    assert "urWins" in src


def test_recommendations_in_claude_target_cmds(m):
    mod, _s, _h = m
    assert "recommendations" in mod._CLAUDE_TARGET_CMDS
