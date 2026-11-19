"""Session health judges recorded activity, not process age.

Claude Code writes <config>/sessions/<pid>.json with status busy/idle and
timestamps; a 5-day orchestrator that is busy must not read as ZOMBIE, and
kill-stale must select by idle time and never touch a busy session. Sessions
with no record keep the old elapsed-based behavior.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'skills/token-optimizer/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
MODULE = SCRIPTS / '_measure/session_activity.py'
MEASURE_PATH = SCRIPTS / 'measure.py'

spec = importlib.util.spec_from_file_location('_measure_session_activity', MODULE)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_session_activity_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    from helpers.measure_split import exec_source_module as _split_exec_module
    _split_exec_module(module)
    return module


def _euid():
    return os.geteuid() if hasattr(os, "geteuid") else 0


def _write_record(config_dir, pid, *, status="idle", idle_seconds=0, tmux=None,
                  cwd="/tmp/work", session_id=None, updated_delta=None, started_delta=5 * 86400):
    """Write a fake <config>/sessions/<pid>.json; timestamps are epoch ms."""
    now_ms = int(time.time() * 1000)
    updated = now_ms - int((idle_seconds if updated_delta is None else updated_delta) * 1000)
    rec = {"pid": pid, "sessionId": session_id or f"sess-{pid}", "cwd": cwd,
           "status": status, "updatedAt": updated, "statusUpdatedAt": updated}
    if started_delta is not None:
        rec["startedAt"] = now_ms - int(started_delta * 1000)
    if tmux:
        rec["tmux"] = tmux
    sessions = Path(config_dir) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _session(pid, elapsed=5 * 86400):
    return {"pid": pid, "started": "x", "elapsed_seconds": elapsed, "elapsed_human": "5d",
            "command": "claude", "has_terminal": True, "version": None,
            "identity": "terminal_cli", "identity_source": "ps", "uid": _euid()}


def _live_session(pid, elapsed=5 * 86400):
    """A collected session dict carrying the activity snapshot kill-stale reads."""
    s = _session(pid, elapsed)
    s["activity"] = sa.activity(pid)
    s["idle_seconds"] = s["activity"]["idle_seconds"]
    s["idle_human"] = None
    return s


# --- activity() unit behavior -------------------------------------------

def test_missing_record_fails_open(tmp_path):
    act = sa.activity(424242, now=time.time(), config_dir=tmp_path)
    assert act == {"status": None, "last_active_ts": None, "idle_seconds": None,
                   "tmux": None, "source": "none", "reason": "missing", "verified": False}


def test_bad_json_fails_open(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "7.json").write_text("{not json", encoding="utf-8")
    assert sa.activity(7, now=time.time(), config_dir=tmp_path)["source"] == "none"


def test_non_dict_record_fails_open(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "7.json").write_text("[1,2]", encoding="utf-8")
    assert sa.activity(7, now=time.time(), config_dir=tmp_path)["source"] == "none"


def test_busy_record_reports_status_and_idle(tmp_path):
    _write_record(tmp_path, 11, status="busy", idle_seconds=3600)
    act = sa.activity(11, now=time.time(), config_dir=tmp_path)
    assert act["source"] == "record"
    assert act["status"] == "busy"
    assert 3500 <= act["idle_seconds"] <= 3700


def test_idle_uses_the_newest_timestamp(tmp_path):
    now = time.time()
    _write_record(tmp_path, 12, idle_seconds=100)
    rec_path = tmp_path / "sessions" / "12.json"
    rec = json.loads(rec_path.read_text())
    rec["statusUpdatedAt"] = int((now - 900) * 1000)  # status older than updatedAt
    rec_path.write_text(json.dumps(rec))
    act = sa.activity(12, now=now, config_dir=tmp_path)
    assert act["last_active_ts"] == pytest.approx(now - 100, abs=2)
    assert act["idle_seconds"] == pytest.approx(100, abs=2)


def test_transcript_mtime_counts_as_activity(tmp_path):
    now = time.time()
    rec = _write_record(tmp_path, 13, idle_seconds=10 * 86400, cwd="/tmp/some-project")
    project = "-tmp-some-project"
    tdir = tmp_path / "projects" / project
    tdir.mkdir(parents=True)
    transcript = tdir / f"{rec['sessionId']}.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    os.utime(transcript, (now - 60, now - 60))
    act = sa.activity(13, now=now, config_dir=tmp_path)
    assert act["last_active_ts"] == pytest.approx(now - 60, abs=2)
    assert act["idle_seconds"] == pytest.approx(60, abs=2)


def test_record_without_timestamps_or_transcript_has_no_idle(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "14.json").write_text(json.dumps({"pid": 14, "status": "idle"}), encoding="utf-8")
    act = sa.activity(14, now=time.time(), config_dir=tmp_path)
    assert act["source"] == "record"
    assert act["status"] == "idle"
    assert act["last_active_ts"] is None
    assert act["idle_seconds"] is None


def test_tmux_and_future_timestamp_clamps_idle(tmp_path):
    now = time.time()
    _write_record(tmp_path, 15, idle_seconds=-30, tmux="osrc-orch2:@1.%1")
    act = sa.activity(15, now=now, config_dir=tmp_path)
    assert act["tmux"] == "osrc-orch2:@1.%1"
    assert act["idle_seconds"] == 0


def test_config_dir_env_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 16, status="busy", idle_seconds=10)
    assert sa.activity(16)["status"] == "busy"


# --- _collect_health_data flags -----------------------------------------

def _health(measure, monkeypatch, sessions):
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure.platform, "system", lambda: "Linux")
    monkeypatch.setattr(measure, "_collect_posix_claude_sessions", lambda process_name=None: sessions)
    monkeypatch.setattr(measure, "_find_session_version_for_pid", lambda pid: None)

    def fake_run(argv, **kw):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.99\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(measure.subprocess, "run", fake_run)
    return measure._collect_health_data()


def test_busy_five_day_session_is_active_not_zombie(tmp_path, monkeypatch):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 501, status="busy", idle_seconds=5 * 86400)
    data = _health(measure, monkeypatch, [_session(501)])
    flags = data["running_sessions"][0]["flags"]
    assert "ACTIVE" in flags
    assert "ZOMBIE" not in flags and "STALE" not in flags
    assert data["running_sessions"][0]["idle_seconds"] > 4 * 86400


def test_idle_three_day_session_is_zombie(tmp_path, monkeypatch):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 502, status="idle", idle_seconds=3 * 86400)
    data = _health(measure, monkeypatch, [_session(502)])
    flags = data["running_sessions"][0]["flags"]
    assert "ZOMBIE" in flags
    assert "ACTIVE" not in flags


def test_idle_under_24h_has_no_stale_flag(tmp_path, monkeypatch):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 503, status="idle", idle_seconds=3600)
    data = _health(measure, monkeypatch, [_session(503, elapsed=5 * 86400)])
    flags = data["running_sessions"][0]["flags"]
    assert "STALE" not in flags and "ZOMBIE" not in flags and "ACTIVE" not in flags


def test_no_record_keeps_elapsed_flags(tmp_path, monkeypatch):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    data = _health(measure, monkeypatch, [_session(504)])
    s = data["running_sessions"][0]
    assert s["activity"]["source"] == "none"
    assert "ZOMBIE" in s["flags"]  # 5d elapsed, same as before
    assert s["idle_seconds"] is None


def test_tmux_record_adds_orchestrated(tmp_path, monkeypatch):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 505, status="busy", idle_seconds=60, tmux="osrc-orch2:@9.%9")
    data = _health(measure, monkeypatch, [_session(505)])
    flags = data["running_sessions"][0]["flags"]
    assert "ORCHESTRATED" in flags
    assert "ACTIVE" in flags


# --- kill_stale_sessions selection --------------------------------------

def _kill_stale(measure, monkeypatch, sessions):
    killed = []
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_collect_health_data", lambda: {"running_sessions": sessions})
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    monkeypatch.setattr(measure, "_posix_ancestor_pids", lambda pid: set())
    monkeypatch.setattr(measure, "_windows_ancestor_pids", lambda pid, names=None: set())
    monkeypatch.setattr(measure, "_revalidate_terminal_cli", lambda s, identity="terminal_cli": True)
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append(pid))
    return killed


def test_kill_stale_skips_busy_five_day_session(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 601, status="busy", idle_seconds=5 * 86400)
    killed = _kill_stale(measure, monkeypatch, [_live_session(601)])
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert killed == []
    assert "No stale sessions found" in out


def test_kill_stale_selects_idle_three_day_session(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 602, status="idle", idle_seconds=3 * 86400)
    killed = _kill_stale(measure, monkeypatch, [_live_session(602)])
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert killed == [602]
    assert "Terminated 1 stale session" in out


def test_kill_stale_uses_idle_not_elapsed(tmp_path, monkeypatch, capsys):
    """A 5-day-old process idle for an hour is not a kill candidate."""
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 603, status="idle", idle_seconds=3600)
    killed = _kill_stale(measure, monkeypatch, [_live_session(603)])
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_kill_stale_no_record_keeps_elapsed_behavior(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    s = _session(604)  # no record: activity source "none"
    s["activity"] = sa.activity(604)
    killed = _kill_stale(measure, monkeypatch, [s])
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == [604]


def test_kill_stale_never_kills_session_that_went_busy(tmp_path, monkeypatch, capsys):
    """Selected as idle, then the record flips to busy before the signal."""
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 605, status="idle", idle_seconds=3 * 86400)
    killed = _kill_stale(measure, monkeypatch, [_live_session(605)])
    # Flip the record to busy after collection; the kill-time re-check must skip it.
    _write_record(tmp_path, 605, status="busy", idle_seconds=0)
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert killed == []
    assert "busy now" in out


def test_kill_stale_skips_session_that_just_finished_a_turn(tmp_path, monkeypatch, capsys):
    """Selected as idle for days, then a turn completes (idle again, fresh stamp)."""
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 606, status="idle", idle_seconds=3 * 86400)
    killed = _kill_stale(measure, monkeypatch, [_live_session(606)])
    _write_record(tmp_path, 606, status="idle", idle_seconds=5)
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_kill_stale_skips_when_the_fresh_read_fails(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 607, status="idle", idle_seconds=3 * 86400)
    killed = _kill_stale(measure, monkeypatch, [_live_session(607)])
    (tmp_path / "sessions" / "607.json").unlink()
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_record_from_a_dead_session_with_a_reused_pid_is_ignored(tmp_path):
    rec = _write_record(tmp_path, 608, status="idle", idle_seconds=10 * 86400)
    rec["startedAt"] = int((time.time() - 20 * 86400) * 1000)
    (tmp_path / "sessions" / "608.json").write_text(json.dumps(rec), encoding="utf-8")
    act = sa.activity(608, config_dir=tmp_path, elapsed_seconds=3600)
    assert act["source"] == "none"


def test_record_for_another_pid_is_ignored(tmp_path):
    rec = _write_record(tmp_path, 609, status="idle", idle_seconds=10 * 86400)
    rec["pid"] = 12345
    (tmp_path / "sessions" / "609.json").write_text(json.dumps(rec), encoding="utf-8")
    assert sa.activity(609, config_dir=tmp_path)["source"] == "none"


def test_idle_never_exceeds_process_lifetime(tmp_path):
    rec = _write_record(tmp_path, 610, status="idle", idle_seconds=10 * 86400)
    rec["startedAt"] = int((time.time() - 3600) * 1000)
    (tmp_path / "sessions" / "610.json").write_text(json.dumps(rec), encoding="utf-8")
    act = sa.activity(610, config_dir=tmp_path, elapsed_seconds=3600)
    assert act["source"] == "record" and act["idle_seconds"] <= 3600


def test_project_dir_encoding_maps_every_non_alphanumeric():
    assert sa._project_dir_name("/Users/a b/my.proj_x") == "-Users-a-b-my-proj-x"


def test_kill_stale_skips_record_that_was_unreadable_then_busy(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions" / "611.json").write_text("{not json", encoding="utf-8")
    killed = _kill_stale(measure, monkeypatch, [_live_session(611)])
    _write_record(tmp_path, 611, status="busy", idle_seconds=0)
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_kill_stale_skips_unreadable_record_on_both_reads(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions" / "612.json").write_text("{not json", encoding="utf-8")
    killed = _kill_stale(measure, monkeypatch, [_live_session(612)])
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_kill_stale_requires_explicit_idle_status(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 613, status="weird", idle_seconds=3 * 86400)
    killed = _kill_stale(measure, monkeypatch, [_live_session(613)])
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_kill_stale_requires_verified_start_time(tmp_path, monkeypatch, capsys):
    measure = _load_measure()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_record(tmp_path, 614, status="idle", idle_seconds=3 * 86400, started_delta=None)
    killed = _kill_stale(measure, monkeypatch, [_live_session(614)])
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == []


def test_zero_lifetime_clamps_idle_to_zero(tmp_path):
    _write_record(tmp_path, 615, status="idle", idle_seconds=3 * 86400, started_delta=0)
    act = sa.activity(615, config_dir=tmp_path, elapsed_seconds=0)
    assert act["idle_seconds"] == 0
