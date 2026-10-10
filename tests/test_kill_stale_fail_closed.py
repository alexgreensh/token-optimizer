"""kill-stale must fail closed, and a few Windows-only output/console leaks.

Every test replaces os.kill with a recorder: no real process is ever signalled.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"
OLD = "Wed Jan  1 00:00:00 2020"


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("measure_kill_stale_fail_closed", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _euid():
    return os.geteuid() if hasattr(os, "geteuid") else 0


def _session(pid, identity="terminal_cli", uid="mine", source="ps", elapsed=13 * 3600):
    s = {"pid": pid, "elapsed_seconds": elapsed, "elapsed_human": "13h", "flags": [],
         "started": OLD, "command": "claude", "identity_source": source}
    if identity is not None:
        s["identity"] = identity
    if source == "ps":
        s["uid"] = _euid() if uid == "mine" else uid
    return s


def _arrange(monkeypatch, measure, sessions, nt=False):
    """Hermetic kill_stale: this process is pid 9999, its parent 9998."""
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_collect_health_data", lambda: {"running_sessions": sessions})
    monkeypatch.setattr(measure.os, "getpid", lambda: 9999)
    monkeypatch.setattr(measure.os, "getppid", lambda: 9998)
    fresh = {s["pid"]: dict(s) for s in sessions}
    monkeypatch.setattr(measure, "_revalidate_terminal_cli", lambda s, identity="terminal_cli": s["pid"] in fresh)
    killed = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: killed.append(pid))
    return killed


# --- Finding 2: POSIX ancestry unknown means nothing is terminated -----------

def test_posix_ancestor_pids_is_none_when_ps_is_unreadable(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "_posix_process_names", lambda: None)
    assert measure._posix_ancestor_pids(9999) is None


def test_posix_ancestor_pids_is_none_when_this_pid_is_missing_from_the_table(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "_posix_process_names",
                        lambda: {4000: (300, "claude"), 9998: (4000, "zsh")})
    assert measure._posix_ancestor_pids(9999) is None


def test_posix_ancestor_pids_walks_the_chain_when_readable(monkeypatch):
    measure = _load_measure()
    monkeypatch.setattr(measure, "_posix_process_names",
                        lambda: {4000: (300, "claude"), 9998: (4000, "zsh"), 9999: (9998, "python3"),
                                 300: (1, "Terminal"), 1: (0, "launchd")})
    assert measure._posix_ancestor_pids(9999) == {9998, 4000, 300, 1}


@pytest.mark.skipif(os.name == "nt", reason="POSIX branch")
@pytest.mark.parametrize("table", [
    None,
    {4000: (300, "claude"), 9998: (4000, "zsh")},
], ids=["ps-unreadable", "own-pid-missing"])
def test_kill_stale_terminates_nothing_when_posix_ancestry_is_unknown(monkeypatch, capsys, table):
    measure = _load_measure()
    killed = _arrange(monkeypatch, measure, [_session(4000)])
    monkeypatch.setattr(measure, "_posix_process_names", lambda: table)
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert killed == []
    assert "Nothing was terminated" in out
    assert "Terminated 1" not in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX branch")
def test_kill_stale_still_terminates_when_posix_ancestry_is_readable(monkeypatch, capsys):
    measure = _load_measure()
    killed = _arrange(monkeypatch, measure, [_session(4000), _session(4001)])
    monkeypatch.setattr(measure, "_posix_process_names",
                        lambda: {4000: (300, "claude"), 9998: (4000, "zsh"), 9999: (9998, "python3"),
                                 4001: (300, "claude")})
    measure.kill_stale_sessions(threshold_hours=12)
    assert killed == [4001]  # 4000 is the conversation this command runs inside of


# --- Finding 10c: Windows chain with a missing intermediate pid is unknown ---

def test_windows_ancestor_pids_is_none_when_an_intermediate_parent_is_missing():
    measure = _load_measure()
    names = {9999: (9998, "python.exe"), 9998: (5000, "bash.exe"), 4000: (300, "claude.exe")}
    assert measure._windows_ancestor_pids(9999, names=names) is None


def test_windows_ancestor_pids_returns_the_full_chain_when_complete():
    measure = _load_measure()
    names = {9999: (9998, "python.exe"), 9998: (5000, "bash.exe"), 5000: (4000, "node.exe"),
             4000: (300, "claude.exe"), 300: (0, "explorer.exe")}
    assert measure._windows_ancestor_pids(9999, names=names) == {9998, 5000, 4000, 300}


def test_windows_ancestor_pids_accepts_a_system_root_whose_parent_has_exited():
    measure = _load_measure()
    # explorer.exe's parent (userinit.exe) is always gone: that is a complete chain.
    names = {9999: (9998, "python.exe"), 9998: (5000, "bash.exe"), 5000: (4000, "windowsterminal.exe"),
             4000: (777, "explorer.exe")}
    assert measure._windows_ancestor_pids(9999, names=names) == {9998, 5000, 4000, 777}


def test_windows_ancestor_pids_is_none_when_a_non_root_has_a_vanished_parent():
    measure = _load_measure()
    names = {9999: (9998, "python.exe"), 9998: (777, "bash.exe")}
    assert measure._windows_ancestor_pids(9999, names=names) is None


def test_windows_ancestor_pids_stops_quietly_at_a_dead_root_parent():
    measure = _load_measure()
    # The root's parent pid (e.g. a finished launcher) is allowed to be absent:
    # it is the end of the chain, not a gap in it.
    names = {9999: (9998, "python.exe"), 9998: (0, "bash.exe")}
    assert measure._windows_ancestor_pids(9999, names=names) == {9998}


# --- Finding 10b: os.kill raising OSError on a pid that just exited ----------

@pytest.mark.skipif(os.name == "nt", reason="POSIX branch")
def test_kill_loop_survives_oserror_and_continues(monkeypatch, capsys):
    measure = _load_measure()
    sessions = [_session(4000), _session(4001), _session(4002)]
    _arrange(monkeypatch, measure, sessions)
    monkeypatch.setattr(measure, "_posix_ancestor_pids", lambda pid: set())
    attempted = []

    def kill(pid, sig):
        attempted.append(pid)
        if pid == 4000:
            raise OSError(87, "The parameter is incorrect")  # Windows: pid just exited

    monkeypatch.setattr(measure.os, "kill", kill)
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert attempted == [4000, 4001, 4002]
    assert "Terminated 2 stale sessions" in out
    assert "PID 4000 could not be signalled" in out


# --- Finding 10d: only processes owned by the current user -------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX owner column")
def test_posix_collector_asks_ps_for_the_uid_and_records_it(monkeypatch):
    measure = _load_measure()
    seen = {}

    def fake_run(argv, **kw):
        if argv[0] == "ps" and any("lstart" in a for a in argv):
            seen["cols"] = argv[argv.index("-eo") + 1]
            row = f"  4000     1 {_euid()} ttys001 {OLD} 13:00:00 claude\n"
            return subprocess.CompletedProcess(argv, 0, stdout="hdr\n" + row, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(measure.subprocess, "run", fake_run)
    monkeypatch.setattr(measure, "_posix_read_argv", lambda pid: None)
    monkeypatch.setattr(measure, "_posix_exe_path", lambda pid, comm=None: None)
    sessions = measure._collect_posix_claude_sessions()
    assert "uid" in seen["cols"].split(",")
    assert [s["uid"] for s in sessions] == [_euid()]


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner column")
@pytest.mark.parametrize("uid", [_euid() + 1, None], ids=["other-user", "owner-unknown"])
def test_kill_stale_never_targets_a_process_it_does_not_provably_own(monkeypatch, capsys, uid):
    measure = _load_measure()
    killed = _arrange(monkeypatch, measure, [_session(4000, uid=uid), _session(4001)])
    monkeypatch.setattr(measure, "_posix_ancestor_pids", lambda pid: set())
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert killed == [4001]
    assert "PID 4000" not in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner column")
def test_kill_stale_dry_run_does_not_list_other_users_processes(monkeypatch, capsys):
    measure = _load_measure()
    _arrange(monkeypatch, measure, [_session(4000, uid=_euid() + 1)])
    monkeypatch.setattr(measure, "_posix_ancestor_pids", lambda pid: set())
    measure.kill_stale_sessions(threshold_hours=12, dry_run=True)
    assert "PID 4000" not in capsys.readouterr().out


def test_windows_closing_message_does_not_claim_an_owner_filter(monkeypatch, capsys):
    """The Windows collector has no owner column (a per-process GetOwner call would be a
    new slow CIM round trip per session), so the closing text must not promise one."""
    measure = _load_measure()
    sessions = [_session(4000, source="windows")]
    _arrange(monkeypatch, measure, sessions)
    monkeypatch.setattr(measure.os, "name", "nt")
    monkeypatch.setattr(measure, "_windows_ancestor_pids", lambda pid, names=None: set())
    monkeypatch.setattr(measure, "_revalidate_terminal_cli", lambda s, identity="terminal_cli": True)
    measure.kill_stale_sessions(threshold_hours=12)
    out = capsys.readouterr().out
    assert "Terminated 1 stale session" in out
    assert "unaffected" not in out
    assert "owner" in out.lower() or "account" in out.lower()


