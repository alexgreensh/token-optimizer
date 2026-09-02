"""_sub_quality_cache_force must not latch the marker before work.

The SessionStart side was fixed (marker written AFTER quality_cache succeeds),
but the UserPromptSubmit recovery path still used _ran_once_this_session
(CHECK+SET) which creates the marker BEFORE quality_cache() runs. A hard
os._exit(0) timeout during quality_cache() leaves the marker on disk and
latches the recovery dead for the whole session.

The partial edit added unlink-on-failure for score is None, but that doesn't
help when os._exit(0) bypasses the cleanup. The fix: replace CHECK+SET with
CHECK-ONLY, write the marker only after success.

Run: python3 -m pytest tests/test_gap1_marker_latch.py -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
RUNNER = HOOKS / "userpromptsubmit_runner.py"


def _load_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # claude_home() honors CLAUDE_CONFIG_DIR only when the directory exists;
    # a missing dir is rejected and falls back to the host's real ~/.claude.
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("ups_runner_gap1_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_budget(monkeypatch, runner):
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    monkeypatch.setattr(runner.measure, "_install_hook_budget", lambda seconds=8: object())
    monkeypatch.setattr(runner.measure, "_clear_hook_budget", lambda deadline: None)
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=18: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)


def test_quality_cache_force_does_not_latch_marker_on_failure(monkeypatch, tmp_path):
    """When quality_cache() returns None (failure/timeout), the marker must
    NOT be left on disk. The next prompt must be able to retry.
    """
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)

    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")

    # Point QUALITY_CACHE_DIR at tmp so markers land in a real dir
    qcd = tmp_path / "quality-cache"
    qcd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", qcd)

    sid = "sess-gap1-test-001"
    hook_input = {"session_id": sid, "transcript_path": "/tmp/t.jsonl"}

    # quality_cache returns None (simulating failure/timeout)
    call_count = {"n": 0}
    def _qc_returns_none(**kw):
        call_count["n"] += 1
        return None
    monkeypatch.setattr(runner.measure, "quality_cache", _qc_returns_none)

    # First call: should run quality_cache, get None, NOT leave marker
    runner._sub_quality_cache_force(hook_input)
    assert call_count["n"] == 1, "quality_cache should have been called once"

    # Check marker does NOT exist
    marker = runner.measure._once_per_session_marker("quality-cache-force", sid)
    assert marker is not None, "marker path should be valid"
    assert not marker.exists(), (
        f"Marker {marker} exists after a failed quality_cache(). "
        f"A hard os._exit(0) during quality_cache() would leave this marker "
        f"on disk and latch the recovery dead for the whole session."
    )

    # Second call: should retry (marker was not latched)
    runner._sub_quality_cache_force(hook_input)
    assert call_count["n"] == 2, (
        "quality_cache should have been called again on the second prompt. "
        "If it was only called once, the marker latched the recovery dead."
    )


def test_quality_cache_force_latches_marker_on_success(monkeypatch, tmp_path):
    """When quality_cache() succeeds (returns a score), the marker MUST be
    written so subsequent prompts skip the work.
    """
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)

    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")

    qcd = tmp_path / "quality-cache"
    qcd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", qcd)

    sid = "sess-gap1-test-002"
    hook_input = {"session_id": sid, "transcript_path": "/tmp/t.jsonl"}

    call_count = {"n": 0}
    def _qc_succeeds(**kw):
        call_count["n"] += 1
        return 85  # a real score
    monkeypatch.setattr(runner.measure, "quality_cache", _qc_succeeds)

    # First call: succeeds, marker should be written
    runner._sub_quality_cache_force(hook_input)
    assert call_count["n"] == 1

    marker = runner.measure._once_per_session_marker("quality-cache-force", sid)
    assert marker is not None
    assert marker.exists(), "Marker should exist after a successful quality_cache()"

    # Second call: should skip (marker exists)
    runner._sub_quality_cache_force(hook_input)
    assert call_count["n"] == 1, (
        "quality_cache should NOT have been called again after success. "
        "The marker should have latched it."
    )


def test_quality_cache_force_survives_hard_kill_simulation(monkeypatch, tmp_path):
    """Simulate a hard os._exit(0) during quality_cache() by making it
    raise SystemExit(0). The marker must NOT be on disk because it was
    never written (CHECK-ONLY, not CHECK+SET).
    """
    runner = _load_runner(monkeypatch, tmp_path)
    _stub_budget(monkeypatch, runner)

    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")

    qcd = tmp_path / "quality-cache"
    qcd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", qcd)

    sid = "sess-gap1-test-003"
    hook_input = {"session_id": sid, "transcript_path": "/tmp/t.jsonl"}

    def _qc_hard_kill(**kw):
        # Simulate os._exit(0) via SystemExit(0) -- the marker must not
        # have been written yet if the fix is correct.
        raise SystemExit(0)
    monkeypatch.setattr(runner.measure, "quality_cache", _qc_hard_kill)

    # The SystemExit will propagate. We catch it here to check the marker.
    with pytest.raises(SystemExit):
        runner._sub_quality_cache_force(hook_input)

    marker = runner.measure._once_per_session_marker("quality-cache-force", sid)
    assert marker is not None
    assert not marker.exists(), (
        f"Marker {marker} exists after a hard kill during quality_cache(). "
        f"The marker was written BEFORE the work (CHECK+SET), so a hard "
        f"os._exit(0) latches the recovery dead for the whole session. "
        f"Fix: use CHECK-ONLY, write marker only after success."
    )
