"""Child stdio fallbacks for the Windows detached child (final-r3 findings 5, 6, 10h, 10i).

Windows-only behaviour is checked with fakes here (the real-handle case needs a
Windows host: see tests/test_windows_detached_child_e2e.py). Nothing here touches
the real ~/.claude.
"""
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"
sys.path.insert(0, str(SCRIPTS))

_DETACHED_PROCESS = 0x8


def _load_run_py():
    spec = importlib.util.spec_from_file_location("run_under_test", HOOKS / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Fd:
    """Stream stand-in with a working fileno()."""

    def __init__(self, fd, tty=False):
        self._fd, self._tty = fd, tty

    def fileno(self):
        return self._fd

    def isatty(self):
        return self._tty

    def flush(self):
        pass


class _NoFileno:
    def flush(self):
        pass


# --- finding 5: unusable stream gets DEVNULL when another one is usable -----

def test_run_py_stdio_kwargs_devnull_for_missing_stream(monkeypatch):
    run = _load_run_py()
    out, err = _Fd(1), _Fd(2)
    monkeypatch.setattr(run, "sys", types.SimpleNamespace(stdin=None, stdout=out, stderr=err))
    assert run._windows_stdio_kwargs() == {
        "stdin": subprocess.DEVNULL, "stdout": out, "stderr": err,
    }


def test_run_py_stdio_kwargs_devnull_for_fileno_less_stream(monkeypatch):
    run = _load_run_py()
    inp, err = _Fd(0), _Fd(2)
    monkeypatch.setattr(
        run, "sys", types.SimpleNamespace(stdin=inp, stdout=_NoFileno(), stderr=err))
    assert run._windows_stdio_kwargs() == {
        "stdin": inp, "stdout": subprocess.DEVNULL, "stderr": err,
    }


def test_run_py_stdio_kwargs_empty_when_nothing_usable(monkeypatch):
    run = _load_run_py()
    monkeypatch.setattr(
        run, "sys", types.SimpleNamespace(stdin=None, stdout=None, stderr=_NoFileno()))
    assert run._windows_stdio_kwargs() == {}


def test_run_py_stdio_kwargs_all_usable_unchanged(monkeypatch):
    run = _load_run_py()
    s = (_Fd(0), _Fd(1), _Fd(2))
    monkeypatch.setattr(run, "sys", types.SimpleNamespace(stdin=s[0], stdout=s[1], stderr=s[2]))
    assert run._windows_stdio_kwargs() == {"stdin": s[0], "stdout": s[1], "stderr": s[2]}


def _utf8_io_nt_reexec(monkeypatch, streams):
    """Run utf8_io.reexec_in_utf8_mode on a faked nt; return the Popen kwargs."""
    sys.modules.pop("utf8_io", None)
    import utf8_io as u

    fake_os = types.ModuleType("os_nt")
    fake_os.__dict__.update(os.__dict__)
    fake_os.name = "nt"
    monkeypatch.setattr(u, "os", fake_os)

    fake_sub = types.ModuleType("subprocess")
    fake_sub.DETACHED_PROCESS = _DETACHED_PROCESS
    fake_sub.DEVNULL = subprocess.DEVNULL
    fake_sub.SubprocessError = subprocess.SubprocessError
    cap = {}

    class _P:
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(argv, **k):
        cap.update(k)
        return _P()

    fake_sub.Popen = fake_popen
    monkeypatch.setitem(sys.modules, "subprocess", fake_sub)
    monkeypatch.setattr(u, "sys", types.SimpleNamespace(
        platform="win32", flags=types.SimpleNamespace(utf8_mode=0), argv=["x.py"],
        executable=sys.executable, **streams))
    monkeypatch.delenv(u._REEXEC_FLAG, raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    import locale
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a: "cp1252")
    monkeypatch.setattr(u.os, "_exit", lambda code=0: None)
    u.reexec_in_utf8_mode()
    return cap


def test_utf8_io_reexec_devnull_for_missing_stream(monkeypatch):
    out, err = _Fd(1), _Fd(2)
    cap = _utf8_io_nt_reexec(monkeypatch, {"stdin": None, "stdout": out, "stderr": err})
    assert cap["stdin"] == subprocess.DEVNULL
    assert cap["stdout"] is out and cap["stderr"] is err


def test_utf8_io_reexec_nothing_passed_when_nothing_usable(monkeypatch):
    cap = _utf8_io_nt_reexec(
        monkeypatch, {"stdin": None, "stdout": None, "stderr": _NoFileno()})
    assert not {"stdin", "stdout", "stderr"} & set(cap)


# --- finding 5: a failed Popen leaves a trace in the existing diagnostics log

def test_run_py_logs_when_popen_raises(monkeypatch, tmp_path):
    run = _load_run_py()
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "h.py").write_text("print('x')\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "argv", ["run.py", "scripts/h.py"])
    logged = []
    monkeypatch.setattr(run, "_consent_log_diagnostics", logged.append)

    def boom(*a, **k):
        raise OSError(6, "The handle is invalid")

    monkeypatch.setattr(run.subprocess, "Popen", boom)
    monkeypatch.setattr(run.signal, "signal", lambda *a: None)
    assert run.main() == 0
    assert len(logged) == 1
    assert logged[0].count("\n") == 1 and logged[0].endswith("\n")
    assert "Popen" in logged[0] and "handle is invalid" in logged[0]


# --- finding 6: no creation flag when a console is already attached ----------

def test_utf8_io_reexec_drops_flag_when_stdout_is_tty(monkeypatch):
    cap = _utf8_io_nt_reexec(
        monkeypatch, {"stdin": _Fd(0, tty=True), "stdout": _Fd(1, tty=True), "stderr": _Fd(2)})
    assert "creationflags" not in cap


def test_utf8_io_reexec_keeps_detached_when_stdout_not_tty(monkeypatch):
    cap = _utf8_io_nt_reexec(
        monkeypatch, {"stdin": _Fd(0), "stdout": _Fd(1, tty=False), "stderr": _Fd(2)})
    assert cap["creationflags"] == _DETACHED_PROCESS


def test_utf8_io_reexec_keeps_detached_when_stdout_missing(monkeypatch):
    cap = _utf8_io_nt_reexec(
        monkeypatch, {"stdin": _Fd(0, tty=True), "stdout": None, "stderr": _Fd(2)})
    assert cap["creationflags"] == _DETACHED_PROCESS


# --- finding 10i: echoed override value is capped ------------------------------

def test_unknown_runtime_value_is_capped_and_repr(monkeypatch, capsys):
    import runtime_env
    monkeypatch.setattr(runtime_env, "_WARNED_BAD_OVERRIDES", set())
    runtime_env._warn_bad_override("x" * 500)
    err = capsys.readouterr().err
    assert "x" * 41 not in err
    assert repr("x" * 40) in err
    assert err.count("\n") == 1


def test_unknown_runtime_value_with_control_chars_is_repr(monkeypatch, capsys):
    import runtime_env
    monkeypatch.setattr(runtime_env, "_WARNED_BAD_OVERRIDES", set())
    runtime_env._warn_bad_override("a\nb\x1b[31m")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "\x1b" not in err
