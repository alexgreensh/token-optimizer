"""Regression and smoke coverage for #117: Windows process enumeration.

``tasklist /v /fo csv /nh`` is pathologically slow for standard (non-elevated)
users on Windows 11: the /v flag queries verbose info (incl. window titles)
for every process, hitting access-denied retries on protected/other-user
processes. The collector now uses a PowerShell ``Get-Process`` pipeline that
pre-filters by image name and never touches window titles. These tests pin:

- the spawned command is Get-Process-based and never tasklist
- errors="replace" and the _NO_WINDOW console-less spawn guard are preserved
- the strict image-name matcher still rejects claude-adjacent processes
- SessionId 0 (services session) still means "no terminal"
- an empty StartTime (access denied) falls back to the per-PID lookup
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
MEASURE_PATH = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"


def _load_measure():
    scripts = str(MEASURE_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)  # measure.py imports sibling modules (hook_io, ...)
    spec = importlib.util.spec_from_file_location("measure_under_test", MEASURE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GET_PROCESS_CSV = (
    '"Id","ProcessName","SessionId","StartTime"\r\n'
    '"1234","claude","1","2020-01-01T00:00:00Z"\r\n'
    '"5678","claudeHelper","1","2020-01-01T00:00:00Z"\r\n'
    '"90","claude","0",""\r\n'
)


def _fake_run_factory(record, stdout, returncode=0):
    def fake_run(argv, **kwargs):
        record.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return fake_run


def test_windows_sessions_use_get_process_not_tasklist(monkeypatch):
    measure = _load_measure()
    record = []
    monkeypatch.setattr(measure.subprocess, "run", _fake_run_factory(record, GET_PROCESS_CSV))
    monkeypatch.setattr(measure, "_windows_process_creation", lambda pid: {})

    sessions = measure._collect_windows_claude_sessions()

    assert record, "no subprocess spawned"
    argv, kwargs = record[0]
    joined = " ".join(str(a) for a in argv)
    assert "tasklist" not in joined
    assert "Get-Process" in joined
    # Non-ASCII safety and the console-less spawn guard (#107) preserved.
    assert kwargs.get("errors") == "replace"
    assert kwargs.get("creationflags") == measure._NO_WINDOW
    assert kwargs.get("timeout") == 10

    # Strict image-name matcher: claudeHelper must be rejected even though the
    # PowerShell-side wildcard let it through.
    assert [s["pid"] for s in sessions] == [1234, 90]

    by_pid = {s["pid"]: s for s in sessions}
    # SessionId 0 is the services session: no terminal.
    assert by_pid[1234]["has_terminal"] is True
    assert by_pid[90]["has_terminal"] is False
    # StartTime from the CSV is used (no per-PID lookup needed).
    assert by_pid[1234]["started"] != "unknown"
    assert by_pid[1234]["elapsed_seconds"] > 0
    # Empty StartTime (access denied) fell back to the mocked per-PID lookup.
    assert by_pid[90]["started"] == "unknown"


def test_windows_sessions_empty_starttime_invokes_per_pid_fallback(monkeypatch):
    measure = _load_measure()
    record = []
    csv_one_row = (
        '"Id","ProcessName","SessionId","StartTime"\r\n'
        '"4321","claude","1",""\r\n'
    )
    monkeypatch.setattr(measure.subprocess, "run", _fake_run_factory(record, csv_one_row))
    fallback_calls = []
    monkeypatch.setattr(
        measure,
        "_windows_process_creation",
        lambda pid: fallback_calls.append(pid) or {"started": "Wed Aug  5 10:00:00 2026", "elapsed_seconds": 60},
    )

    sessions = measure._collect_windows_claude_sessions()

    assert fallback_calls == [4321]
    assert sessions[0]["elapsed_seconds"] == 60
    assert sessions[0]["elapsed_human"] == "1m"


def test_windows_sessions_spawn_failure_returns_empty_list(monkeypatch):
    measure = _load_measure()

    def raising_run(argv, **kwargs):
        raise FileNotFoundError("powershell not found")

    monkeypatch.setattr(measure.subprocess, "run", raising_run)

    assert measure._collect_windows_claude_sessions() == []


def test_windows_sessions_nonzero_exit_returns_empty_list(monkeypatch):
    measure = _load_measure()
    record = []
    monkeypatch.setattr(
        measure.subprocess, "run", _fake_run_factory(record, "", returncode=1)
    )

    assert measure._collect_windows_claude_sessions() == []


def test_windows_sessions_spawn_hardening_flags(monkeypatch):
    """LOW-2 + MED-3: the collector must pass -ExecutionPolicy Bypass (so an
    AllSigned host doesn't block the inline -Command) and use the server-side
    -Name 'claude*' filter. Both can regress silently without an argv assert."""
    measure = _load_measure()
    record = []
    monkeypatch.setattr(measure.subprocess, "run", _fake_run_factory(record, GET_PROCESS_CSV))
    monkeypatch.setattr(measure, "_windows_process_creation", lambda pid: {})

    measure._collect_windows_claude_sessions()

    assert record, "no subprocess spawned"
    argv = [str(a) for a in record[0][0]]
    assert "-ExecutionPolicy" in argv and "Bypass" in argv
    joined = " ".join(argv)
    assert "-Name" in joined and "claude*" in joined


def test_self_check_probes_get_process_not_tasklist():
    """The Windows self-check probe (measure.py self-check) must exercise the
    new enumeration command, not the retired tasklist probe."""
    src = MEASURE_PATH.read_text(encoding="utf-8")
    assert '"tasklist", "/v"' not in src, "tasklist /v spawn still present in measure.py"
    assert "Get-Process" in src


@pytest.mark.skipif(sys.platform != "win32", reason="real Get-Process smoke only on Windows")
def test_collect_windows_sessions_real_spawn():
    """Smoke: on real Windows the collector runs end-to-end within its timeout
    and returns a list (empty or not). Catches missing-PowerShell and
    output-shape regressions that mocks cannot see."""
    measure = _load_measure()

    sessions = measure._collect_windows_claude_sessions()

    assert isinstance(sessions, list)
