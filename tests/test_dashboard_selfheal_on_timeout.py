"""A hook-run dashboard rebuild that hits its wall-clock budget must hand the
work to the detached self-heal child.

The watchdog ends the process with os._exit, so nothing after the kill runs:
the handoff has to happen on the watchdog's own timeout path. These tests
drive that path for real (a subprocess whose deadline fires) instead of
injecting an exception the production code no longer raises.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_deadline_runs_on_timeout_callbacks_before_exiting(tmp_path):
    marker = tmp_path / "fired.txt"
    child = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(SCRIPTS)!r})
        from hook_runtime import HookDeadline
        d = HookDeadline(0.3, on_timeout=lambda: open({str(marker)!r}, "w").write("a"))
        d.add_on_timeout(lambda: open({str(marker)!r}, "a").write("b"))
        d.start()
        time.sleep(5)
        print("should never get here")
        """
    )
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, timeout=30)
    elapsed = time.perf_counter() - t0
    assert proc.returncode == 0, proc.stderr
    assert b"never" not in proc.stdout
    assert elapsed < 3.0, f"watchdog did not fire promptly ({elapsed:.1f}s)"
    assert marker.read_text() == "ab", "both timeout callbacks must run, in order"
    assert b"hook budget exceeded" in proc.stderr


def test_hanging_callback_cannot_delay_the_exit(tmp_path):
    child = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(SCRIPTS)!r})
        from hook_runtime import HookDeadline
        HookDeadline(0.2, on_timeout=lambda: time.sleep(60)).start()
        time.sleep(30)
        """
    )
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert time.perf_counter() - t0 < 5.0


def test_normal_completion_never_runs_callbacks(tmp_path):
    from hook_runtime import HookDeadline

    calls = []
    with HookDeadline(5.0, on_timeout=lambda: calls.append("fired")):
        time.sleep(0.05)
    time.sleep(0.2)
    assert calls == []


@pytest.fixture()
def measure(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_HOOK", "1")
    monkeypatch.delenv("TOKEN_OPTIMIZER_INTERACTIVE", raising=False)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def test_hook_run_dashboard_arms_the_selfheal_on_the_active_deadline(measure, monkeypatch):
    from hook_runtime import HookDeadline

    spawned = []
    monkeypatch.setattr(measure, "_spawn_detached_dashboard_selfheal", lambda **kw: spawned.append(kw))
    monkeypatch.setattr(measure, "_dashboard_heal_spawn_due", lambda: True)
    deadline = HookDeadline(60.0).start()
    try:
        measure.generate_standalone_dashboard(days=7, quiet=True, force=True)
        assert deadline._on_timeout, "the rebuild did not register a timeout handoff"
        deadline._run_on_timeout()
    finally:
        deadline.cancel()
    assert spawned and spawned[0]["force"] is True and spawned[0]["days"] == 7


def test_interactive_dashboard_does_not_arm_a_handoff(measure, monkeypatch):
    from hook_runtime import HookDeadline

    monkeypatch.setenv("TOKEN_OPTIMIZER_INTERACTIVE", "1")
    deadline = HookDeadline(60.0).start()
    try:
        measure.generate_standalone_dashboard(days=7, quiet=True, force=True)
        assert deadline._on_timeout == []
    finally:
        deadline.cancel()


def test_dashboard_hook_budget_env_knob(measure, monkeypatch):
    monkeypatch.delenv("TOKEN_OPTIMIZER_DASHBOARD_HOOK_BUDGET", raising=False)
    assert measure._dashboard_hook_budget_seconds() == 20.0
    monkeypatch.setenv("TOKEN_OPTIMIZER_DASHBOARD_HOOK_BUDGET", "90")
    assert measure._dashboard_hook_budget_seconds() == 90.0
    monkeypatch.setenv("TOKEN_OPTIMIZER_DASHBOARD_HOOK_BUDGET", "nope")
    assert measure._dashboard_hook_budget_seconds() == 20.0
    monkeypatch.setenv("TOKEN_OPTIMIZER_DASHBOARD_HOOK_BUDGET", "0")
    assert measure._dashboard_hook_budget_seconds() == 1.0
