"""Process liveness must work on Windows.

``os.kill(pid, 0)`` is a POSIX probe. On Windows, signal 0 is CTRL_C_EVENT:
Python calls GenerateConsoleCtrlEvent, which fails for every pid when the
caller has no console (hooks run detached), so a live lock owner read as dead
and two daemon installs could race; with a console it can interrupt the whole
console group. ``purge --force`` also asked POSIX ``ps`` for the daemon's name,
so on Windows it never stopped the daemon.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))


class _NtOs:
    """measure's view of ``os`` with ``os.name == "nt"``; patching the real
    module would break pathlib for the rest of the process."""

    name = "nt"

    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, attr):
        return getattr(self._real, attr)


@pytest.fixture
def m(monkeypatch, tmp_path):
    (tmp_path / "claude").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    import measure
    return measure


def test_current_process_is_running_and_an_exited_child_is_not(m):
    """Real OS check (the Windows CI jobs run the Windows branch)."""
    assert m._pid_is_running(m.os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not m._pid_is_running(child.pid)
    for bad in (0, -1, None, "x"):
        assert not m._pid_is_running(bad)


def _fake_ctypes(alive_pids, denied_pids=()):
    """Enough of ctypes/wintypes for _win_pid_is_running."""
    state = {"err": 0}

    class K32:
        class _F:
            def __init__(self, fn):
                self.fn = fn
                self.restype = None
                self.argtypes = None

            def __call__(self, *a):
                return self.fn(*a)

        def __init__(self):
            def open_process(access, inherit, pid):
                if pid in denied_pids:
                    state["err"] = 5
                    return 0
                if pid in alive_pids or pid == 4242:
                    return pid
                state["err"] = 87
                return 0

            def exit_code(handle, ref):
                ref.value = 259 if handle in alive_pids else 1
                return 1

            self.OpenProcess = self._F(open_process)
            self.GetExitCodeProcess = self._F(exit_code)
            self.CloseHandle = self._F(lambda h: 1)

    class DWORD:
        def __init__(self):
            self.value = 0

    wintypes = types.SimpleNamespace(DWORD=DWORD, HANDLE=object, BOOL=object)
    fake = types.SimpleNamespace(
        WinDLL=lambda name, use_last_error=False: K32(),
        get_last_error=lambda: state["err"],
        byref=lambda x: x,
        wintypes=wintypes,
    )
    return fake, wintypes


def test_windows_never_sends_a_signal_and_reads_the_process_table(m, monkeypatch):
    fake, wintypes = _fake_ctypes(alive_pids={100}, denied_pids={200})
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setitem(sys.modules, "ctypes.wintypes", wintypes)
    def no_signals(*a):
        raise AssertionError("os.kill(pid, 0) on Windows sends CTRL_C_EVENT")

    monkeypatch.setattr(m, "os", _NtOs(m.os, kill=no_signals))
    assert m._pid_is_running(100) is True       # running
    assert m._pid_is_running(200) is True       # exists, not ours
    assert m._pid_is_running(300) is False      # no such process
    assert m._pid_is_running(4242) is False     # exited, handle still open


def test_purge_force_on_windows_identifies_and_stops_the_daemon(m, monkeypatch, tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "daemon.pid").write_text("4321", encoding="utf-8")
    monkeypatch.setattr(m, "DAEMON_LOG_DIR", logs)
    monkeypatch.setattr(m, "_win_pid_is_running", lambda pid: pid == 4321)
    monkeypatch.setattr(m, "_windows_process_names", lambda: {4321: (1, "pythonw.exe")})
    killed = []
    monkeypatch.setattr(m, "os", _NtOs(m.os, kill=lambda pid, sig: killed.append((pid, sig))))

    def no_ps(*a, **k):
        raise FileNotFoundError("ps does not exist on Windows")

    monkeypatch.setattr(m.subprocess, "run", no_ps)
    m._purge_all_data(confirm=False, force=True)
    assert killed and killed[0][0] == 4321 and killed[0][1] != 0
    assert "Stopped dashboard daemon (PID 4321)" in capsys.readouterr().out
