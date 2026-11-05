#!/usr/bin/env python3
"""Tests for the quality-cache throttle gate (quality_cache_gate.py).

The gate is a lightweight pre-check that answers the throttle question BEFORE
importing measure.py (44k+ lines, 682ms cold import). In the common case
(throttle window active, ~97% of PostToolUse calls), it exits 0 after a single
stat() of the throttle marker. Only when the throttle has EXPIRED does it fall
through to import measure.py and run the full quality-cache computation.

The gate plugs into the consolidated PostToolUse runner
(``hooks/posttooluse_runner.py``) via its ``_delegate_to_quality_cache_gate()``
seam, which imports the gate module and calls its ``main()`` in-process. The
runner's own tests (``tests/test_posttooluse_runner.py``) already verify the
delegation wiring with mock gate modules; these tests verify the REAL gate
module's behavior.

HARD INVARIANT: tests/test_hook_runtime_parity.py::test_throttle_only_cache_miss_never_parses_transcript
must keep passing — a throttle-only cache MISS must never parse a transcript.
The gate preserves this: if the marker is missing (cache miss), the gate treats
it as "not due" and exits 0 without importing measure.py at all.
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"
GATE_SCRIPT = SCRIPTS / "quality_cache_gate.py"
MEASURE = SCRIPTS / "measure.py"


def _gate_env(tmp_path: Path) -> dict:
    """Build an env that routes QUALITY_CACHE_DIR into tmp_path.

    The gate resolves QUALITY_CACHE_DIR = runtime_home() / "token-optimizer".
    Setting CLAUDE_CONFIG_DIR to tmp_path makes claude_home() return tmp_path,
    so QUALITY_CACHE_DIR = tmp_path / "token-optimizer".
    """
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Ensure we're detected as the claude runtime, not codex/hermes/opencode/etc.
    # detect_runtime() checks TOKEN_OPTIMIZER_RUNTIME first, so setting it to
    # "claude" forces claude_home() which honors CLAUDE_CONFIG_DIR.
    env["TOKEN_OPTIMIZER_RUNTIME"] = "claude"
    env.pop("CODEX_HOME", None)
    env.pop("HERMES_HOME", None)
    env.pop("COPILOT_HOME", None)
    env.pop("TOKEN_OPTIMIZER_COPILOT_HOME", None)
    env.pop("TOKEN_OPTIMIZER_CURSOR_HOME", None)
    env.pop("CURSOR_PROJECT_DIR", None)
    env.pop("CURSOR_VERSION", None)
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env.pop("TOKEN_OPTIMIZER_PLUGIN_DATA", None)
    return env


def _quality_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "token-optimizer"


def _run_gate(tmp_path: Path, payload: dict, timeout: float = 30,
              verbose_imports: bool = False):
    """Run the gate script via module_runner (realistic dispatch path).

    With verbose_imports=True the subprocess runs with PYTHONVERBOSE=1 so its
    stderr carries one ``import '<mod>'`` line per import — the deterministic
    way to prove measure.py was never imported, on any runner speed.

    Returns (result, elapsed_seconds).
    """
    env = _gate_env(tmp_path)
    if verbose_imports:
        env["PYTHONVERBOSE"] = "1"
    module_runner = HOOKS / "module_runner.py"
    cmd = [sys.executable, str(module_runner), str(SCRIPTS), "quality_cache_gate", "--quiet"]
    stdin_data = json.dumps(payload).encode("utf-8")
    started = time.monotonic()
    result = subprocess.run(
        cmd,
        capture_output=True,
        input=stdin_data,
        env=env,
        timeout=timeout,
    )
    return result, time.monotonic() - started


def _assert_measure_never_imported(result) -> None:
    """The contract the old timing asserts approximated: on the throttled /
    cache-miss path the gate subprocess must never import measure.py at all.
    A wall-clock budget can't prove that on a fast machine (the import is a
    ~130ms warm load here, under every floor we tried); the interpreter's own
    import trace can.
    """
    assert b"import 'measure'" not in result.stderr, (
        "gate imported measure.py on a path that must stay import-free "
        "(PYTHONVERBOSE trace):\n"
        + "\n".join(l for l in result.stderr.decode("utf-8", "replace").splitlines()
                    if "measure" in l)[-2000:]
    )


def _run_measure_direct(tmp_path: Path, payload: dict, timeout: float = 30):
    """Run measure.py quality-cache --throttle-only via module_runner (baseline)."""
    env = _gate_env(tmp_path)
    module_runner = HOOKS / "module_runner.py"
    cmd = [sys.executable, str(module_runner), str(SCRIPTS), "measure", "quality-cache", "--quiet", "--throttle-only"]
    stdin_data = json.dumps(payload).encode("utf-8")
    started = time.monotonic()
    result = subprocess.run(
        cmd,
        capture_output=True,
        input=stdin_data,
        env=env,
        timeout=timeout,
    )
    return result, time.monotonic() - started


# ---------------------------------------------------------------------------
# 1. Negative test — fails before the gate script exists
# ---------------------------------------------------------------------------

def test_gate_script_exists_at_canonical_path():
    """The gate script must exist at the canonical scripts path.

    Before the optimization, this file does not exist — the PostToolUse hook
    imported measure.py directly for every tool call. This test fails on the
    un-optimized codebase and passes after the gate is added.
    """
    assert GATE_SCRIPT.is_file(), (
        f"quality_cache_gate.py not found at {GATE_SCRIPT}. "
        "The PostToolUse quality-cache hook still imports measure.py directly "
        "(682ms cold) on every tool call instead of using the lightweight gate."
    )


# ---------------------------------------------------------------------------
# 2. Gate short-circuits when throttle is active (common case)
# ---------------------------------------------------------------------------

def test_gate_exits_zero_when_throttle_marker_is_recent(tmp_path):
    """When the throttle marker is younger than the throttle window, the gate
    must exit 0 immediately WITHOUT importing measure.py.

    This is the common case (~97% of PostToolUse calls). The gate does one
    stat() of the marker and exits.
    """
    cache_dir = _quality_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = tmp_path / "session-1.jsonl"
    session_jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

    # Create a recent throttle marker (touched now = within the 120s window)
    identity = session_jsonl.stem  # "session-1"
    marker = cache_dir / f".quality-cache-throttle-{identity}"
    marker.touch()

    payload = {"transcript_path": str(session_jsonl), "session_id": "test-session-id"}
    result, elapsed = _run_gate(tmp_path, payload, timeout=30)

    assert result.returncode == 0, (
        f"Gate should exit 0 when throttle is active, got {result.returncode}. "
        f"stderr: {result.stderr.decode('utf-8', errors='replace')}"
    )
    # The gate must NOT produce any stdout (it's a no-op skip)
    assert result.stdout == b"", (
        f"Gate should produce no stdout on throttle skip, got: {result.stdout!r}"
    )


def test_gate_does_not_import_measure_when_throttle_active(tmp_path):
    """Prove the gate does not import measure.py when the throttle is active.

    Importing measure.py costs 682ms cold; a stat() + exit costs ~127ms.
    A 400ms threshold cleanly separates the two. If the gate exceeds it,
    the gate is importing measure.py on the hot path — the exact bug this guards.
    """
    cache_dir = _quality_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = tmp_path / "session-trap.jsonl"
    session_jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

    identity = session_jsonl.stem
    marker = cache_dir / f".quality-cache-throttle-{identity}"
    marker.touch()

    payload = {"transcript_path": str(session_jsonl), "session_id": "test-trap"}
    result, elapsed = _run_gate(tmp_path, payload, timeout=30,
                                verbose_imports=True)

    assert result.returncode == 0
    _assert_measure_never_imported(result)


# ---------------------------------------------------------------------------
# 3. Gate falls through when throttle expired (rare case, full work)
# ---------------------------------------------------------------------------

def test_gate_falls_through_when_throttle_expired(tmp_path):
    """When the throttle marker is older than the throttle window, the gate
    must fall through to import measure.py and run the full quality-cache
    computation. The cache file must be created/updated.
    """
    cache_dir = _quality_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = tmp_path / "session-expired.jsonl"
    # Write a minimal valid JSONL session that quality_cache can parse
    session_jsonl.write_text(
        '{"type":"user","message":{"role":"user","content":"hello"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":"hi"}}\n',
        encoding="utf-8",
    )

    # Create an OLD cache file AND marker (300s ago = well past the 120s window).
    # In real operation, _write_quality_cache creates both atomically: the cache
    # file first, then the marker touch. The gate checks the marker; measure's
    # quality_cache() checks the cache file. Both must be stale for the recompute
    # to fire. Creating only the marker (without the cache) would hit the
    # cache-miss invariant (pure_time_throttle + no cache → return None), which
    # is correct behavior but not what this test exercises.
    identity = session_jsonl.stem
    cache_path = cache_dir / f"quality-cache-{identity}.json"
    cache_path.write_text(json.dumps({"score": 50, "grade": "D", "signals": {}, "breakdown": {}}), encoding="utf-8")
    marker = cache_dir / f".quality-cache-throttle-{identity}"
    marker.touch()
    old_time = time.time() - 300
    os.utime(cache_path, (old_time, old_time))
    os.utime(marker, (old_time, old_time))

    payload = {"transcript_path": str(session_jsonl), "session_id": "test-expired"}
    result, elapsed = _run_gate(tmp_path, payload, timeout=60)

    assert result.returncode == 0, (
        f"Gate should exit 0 even on full computation, got {result.returncode}. "
        f"stderr: {result.stderr.decode('utf-8', errors='replace')}"
    )
    # The cache file should now exist with a FRESH score (the full computation ran).
    # The old score was 50; the recompute should write a new score.
    assert cache_path.exists(), (
        f"Cache file {cache_path} was not created — the gate did not fall "
        f"through to the full quality-cache computation."
    )
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "score" in cached, f"Cache file missing 'score' key: {cached}"
    # The cache file's mtime should be newer than the old timestamp (recomputed)
    assert cache_path.stat().st_mtime > old_time, (
        "Cache file was not rewritten — the full computation did not run."
    )


def test_gate_treats_missing_marker_as_not_due(tmp_path):
    """A missing throttle marker is a cache miss, NOT permission to parse.

    This mirrors _quality_cache_tick_due: OSError (missing marker) → return
    False (not due). The gate must exit 0 without importing measure.py,
    preserving the invariant that a throttle-only cache MISS never parses
    a transcript.
    """
    cache_dir = _quality_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = tmp_path / "session-missing.jsonl"
    session_jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

    # Do NOT create a throttle marker — it's a cache miss
    payload = {"transcript_path": str(session_jsonl), "session_id": "test-missing"}
    result, elapsed = _run_gate(tmp_path, payload, timeout=30,
                                verbose_imports=True)

    assert result.returncode == 0
    # No cache file should be created
    identity = session_jsonl.stem
    cache_path = cache_dir / f"quality-cache-{identity}.json"
    assert not cache_path.exists(), (
        "Cache file was created on a cache miss — the gate incorrectly fell "
        "through to the full computation."
    )
    _assert_measure_never_imported(result)


# ---------------------------------------------------------------------------
# 4. Cold-path cost comparison (real measured milliseconds)
# ---------------------------------------------------------------------------

def test_gate_cold_path_is_faster_than_measure_direct(tmp_path):
    """Measure and compare: gate (throttle active) vs measure.py direct.

    The gate's common case (throttle active) should be dramatically faster
    than measure.py's cold import path. We measure both with cold caches
    (no __pycache__) to simulate the container steady state.
    """
    cache_dir = _quality_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = tmp_path / "session-bench.jsonl"
    session_jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

    identity = session_jsonl.stem
    marker = cache_dir / f".quality-cache-throttle-{identity}"
    marker.touch()

    payload = {"transcript_path": str(session_jsonl), "session_id": "test-bench"}

    # Clear __pycache__ to simulate cold state
    for pycache in [SCRIPTS / "__pycache__", HOOKS / "__pycache__"]:
        if pycache.exists():
            for f in pycache.iterdir():
                if "quality_cache_gate" in f.name or "measure" in f.name or "hook_runtime" in f.name or "module_runner" in f.name:
                    try:
                        f.unlink()
                    except OSError:
                        pass

    # Measure the gate (throttle active = common case)
    gate_result, gate_elapsed = _run_gate(tmp_path, payload, timeout=30)
    assert gate_result.returncode == 0

    # Clear caches again for the measure.py baseline
    for pycache in [SCRIPTS / "__pycache__", HOOKS / "__pycache__"]:
        if pycache.exists():
            for f in pycache.iterdir():
                if "measure" in f.name or "hook_runtime" in f.name or "module_runner" in f.name:
                    try:
                        f.unlink()
                    except OSError:
                        pass

    # Measure measure.py direct (the old path)
    measure_result, measure_elapsed = _run_measure_direct(tmp_path, payload, timeout=30)
    assert measure_result.returncode == 0

    # The gate must beat the path it protects. A minimum RATIO flakes under
    # CPU contention — both runs inflate by their fixed spawn cost, so 4-6x
    # idle collapses toward ~1.3x under load (observed). The deterministic
    # regression check is the PYTHONVERBOSE import assert in the tests above;
    # here "strictly faster" is the stable invariant.
    assert gate_elapsed < measure_elapsed, (
        f"Gate slower than the work it skips (gate={gate_elapsed*1000:.0f}ms, "
        f"measure={measure_elapsed*1000:.0f}ms)"
    )


# ---------------------------------------------------------------------------
# 5. Runner delegation — the real gate module works through the runner
# ---------------------------------------------------------------------------

def test_runner_delegates_to_real_gate_module(monkeypatch, tmp_path):
    """The consolidated PostToolUse runner delegates to the real gate module
    (not a mock), and the gate short-circuits when the throttle is active.

    This verifies the end-to-end integration: runner reads stdin, patches
    hook_io, delegates to quality_cache_gate.main(), which reads the patched
    stdin, checks the marker, and exits 0 without importing measure.py.
    """
    import importlib.util

    cache_dir = _quality_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = tmp_path / "session-runner.jsonl"
    session_jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

    identity = session_jsonl.stem
    marker = cache_dir / f".quality-cache-throttle-{identity}"
    marker.touch()

    # Load the runner fresh
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    spec = importlib.util.spec_from_file_location(
        "ptu_runner_gate_test", HOOKS / "posttooluse_runner.py"
    )
    runner = importlib.util.module_from_spec(spec)
    _split_exec_module(runner)

    # Don't arm a real watchdog
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=None: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)

    # If delegation works, measure must NOT be imported
    monkeypatch.setattr(
        runner, "_measure", lambda: pytest.fail("delegation still imported measure")
    )
    # Stub the other subcommands so only quality-cache runs
    monkeypatch.setattr(runner, "_sub_bash_compress", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_archive_result", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_context_intel", lambda _p: None)

    # Provide the hook payload via the patched stdin
    payload = {"tool_name": "Grep", "transcript_path": str(session_jsonl), "session_id": "test-runner"}
    monkeypatch.setattr(runner, "_read_hook_input", lambda: payload)

    assert runner.main() == 0
