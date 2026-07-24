"""Mid-session dashboard-daemon self-heal (v5.11.63).

`_ensure_dashboard_daemon` self-heals the daemon only at SessionStart, so a daemon
that dies mid-session stays dead until the next session. `_daemon_midsession_pulse`
closes that gap: it runs once per turn from the UserPromptSubmit `quality-cache`
handler, with its own short cadence, and must be:

  * CHEAP on the hot path (probe-throttled to one socket check per window),
  * NON-BLOCKING (revive is a detached subprocess, never inline),
  * THROTTLED so a persistently-dead daemon can't spawn a revive every turn,
  * SAFE: a disabled/tombstoned daemon is NEVER revived; unsupported platforms and
    foreign runtimes no-op; it never raises.

Run: python3 -m pytest tests/test_daemon_midsession_pulse.py -v
"""
import importlib
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-daemon-pulse-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    # Present as a Claude runtime on a supported platform by default.
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(mod, "_normalized_platform", lambda: "Darwin")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _count_spawns(m, monkeypatch):
    """Replace Popen with a counter; return a mutable dict {n:...}."""
    calls = {"n": 0, "argv": None}

    def fake_popen(argv, *a, **k):
        calls["n"] += 1
        calls["argv"] = argv
        class _P:  # minimal stand-in
            pass
        return _P()
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    return calls


def test_dead_daemon_spawns_detached_revive(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)  # dead
    spawns = _count_spawns(m, monkeypatch)
    assert m._daemon_midsession_pulse() == "revive-spawned"
    assert spawns["n"] == 1
    # It must be a DETACHED child running the daemon-revive subcommand (off hot path).
    assert "daemon-revive" in spawns["argv"]


def test_probe_throttle_makes_second_call_a_noop(m, monkeypatch):
    probes = {"n": 0}
    def probe(**k):
        probes["n"] += 1
        return False
    monkeypatch.setattr(m, "_verify_daemon_port", probe)
    _count_spawns(m, monkeypatch)
    m._daemon_midsession_pulse()               # first: probes once
    assert m._daemon_midsession_pulse() == "pulse-throttled"  # within 60s window
    assert probes["n"] == 1, "second call within the window must not hit the socket"


def test_revive_throttle_blocks_respawn_while_dead(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)  # persistently dead
    spawns = _count_spawns(m, monkeypatch)
    assert m._daemon_midsession_pulse() == "revive-spawned"
    # Clear ONLY the probe throttle so we reach the revive-throttle check again.
    m._write_config_flag("last_daemon_midsession_probe", 0)
    assert m._daemon_midsession_pulse() == "revive-throttled"
    assert spawns["n"] == 1, "revive must not respawn within the 300s revive window"


def test_disabled_daemon_is_never_revived(m, monkeypatch):
    """NEGATIVE TEST: a tombstoned/uninstalled daemon (daemon_disabled) must never
    be revived, even when the port probe would report it dead."""
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    spawns = _count_spawns(m, monkeypatch)
    m._write_config_flag("daemon_disabled", True)
    assert m._daemon_midsession_pulse() == "noop-disabled"
    assert spawns["n"] == 0


def test_healthy_daemon_no_spawn(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: True)  # alive
    spawns = _count_spawns(m, monkeypatch)
    assert m._daemon_midsession_pulse() == "noop-healthy"
    assert spawns["n"] == 0


def test_foreign_runtime_noop(m, monkeypatch):
    monkeypatch.setattr(m, "detect_runtime", lambda: "codex")
    spawns = _count_spawns(m, monkeypatch)
    assert m._daemon_midsession_pulse() == "noop-foreign"
    assert spawns["n"] == 0


def test_unsupported_platform_noop(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m, "_normalized_platform", lambda: "SunOS")
    spawns = _count_spawns(m, monkeypatch)
    assert m._daemon_midsession_pulse() == "noop-unsupported"
    assert spawns["n"] == 0


def test_filesystem_tombstone_blocks_revive_even_with_clean_config(m, monkeypatch):
    """CORR-1: a corrupt config.json makes daemon_disabled read False, so the
    filesystem tombstone must be the authoritative opt-out. With the breadcrumb
    present (uninstall / thrash back-off) the daemon is NEVER revived, regardless
    of the config flag."""
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)  # dead
    spawns = _count_spawns(m, monkeypatch)
    m.DAEMON_THRASH_BREADCRUMB.parent.mkdir(parents=True, exist_ok=True)
    m.DAEMON_THRASH_BREADCRUMB.write_text("")  # size-0 uninstall tombstone
    # config flag intentionally left False (simulating corrupt/absent config)
    assert m._daemon_midsession_pulse() == "noop-tombstoned"
    assert spawns["n"] == 0


def test_windows_uses_creationflags_to_detach(m, monkeypatch):
    """CXP-1: start_new_session is a no-op on Windows, so the child would die with
    the hook's job object. On nt the spawn must pass detach creationflags."""
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)  # dead
    monkeypatch.setattr(m, "_normalized_platform", lambda: "Windows")
    monkeypatch.setattr(m.os, "name", "nt")
    # Provide the Windows-only flag constants the code ORs together.
    monkeypatch.setattr(m.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(m.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(m.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000, raising=False)
    captured = {}
    def fake_popen(argv, *a, **k):
        captured.update(k)
        class _P: pass
        return _P()
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    assert m._daemon_midsession_pulse() == "revive-spawned"
    assert "creationflags" in captured, "Windows spawn must pass creationflags"
    assert captured["creationflags"] == (0x8 | 0x200 | 0x1000000)
    assert "start_new_session" not in captured


def test_posix_uses_start_new_session(m, monkeypatch):
    monkeypatch.setattr(m, "_verify_daemon_port", lambda **k: False)
    monkeypatch.setattr(m.os, "name", "posix")
    captured = {}
    def fake_popen(argv, *a, **k):
        captured.update(k)
        class _P: pass
        return _P()
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    assert m._daemon_midsession_pulse() == "revive-spawned"
    assert captured.get("start_new_session") is True
    assert "creationflags" not in captured


def test_never_raises_on_internal_error(m, monkeypatch):
    """Any internal failure must degrade to a status string, never propagate into
    the UserPromptSubmit hot path."""
    def boom(**k):
        raise RuntimeError("probe blew up")
    monkeypatch.setattr(m, "_verify_daemon_port", boom)
    assert m._daemon_midsession_pulse() == "pulse-error"
