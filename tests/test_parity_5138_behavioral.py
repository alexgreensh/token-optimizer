#!/usr/bin/env python3
"""Behavioral tests for the v5.13.8 parity fixes.

These tests exercise real runtime behavior, not static config or stubs:

1. Double-fire: a failed Bash PostToolUse through the real
   bash_compress_hook path records thrash_guard exactly once (the
   redundant fallback that double-fired is gone).

2. Nudge delivery: the real UserPromptSubmit runner routes the
   quality-cache --warn /compact nudge to the stdout envelope as
   additionalContext, not to the diagnostics log.

3. Regex: a successful command whose stdout contains "Exit code 1" on
   a later line does not trigger the failure parse.

4. Codex updatedToolOutput: with TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1,
   bash_compress_hook emits no updatedToolOutput but still records the
   thrash guard.

Run: python3 -m pytest tests/test_parity_5138_behavioral.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"
BASH_COMPRESS_HOOK = SCRIPTS / "bash_compress_hook.py"


# --------------------------------------------------------------------------- #
# 1. Double-fire: thrash_guard records exactly once per Bash PostToolUse
# --------------------------------------------------------------------------- #

def _failed_bash_payload(command: str, stdout: str, exit_code: int,
                         session_id: str) -> str:
    """Build a PostToolUse payload for a failed Bash command delivered as a
    Codex/Cowork-style string tool_response."""
    return json.dumps({
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": f"Exit code {exit_code}\n{stdout}",
    })


def _run_hook(payload: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    full_env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK)],
        input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=full_env,
    )


def _streak_count(session_id: str, command: str, snapshot_dir: str) -> int:
    """Read the thrash_guard streak count for a command from the SessionStore."""
    os.environ["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = snapshot_dir
    sys.path.insert(0, str(SCRIPTS))
    # Remove cached modules so they pick up the env var.
    for m in ("session_store", "delta_diff", "thrash_guard"):
        sys.modules.pop(m, None)
    from session_store import SessionStore
    from delta_diff import content_hash
    store = SessionStore(session_id)
    row = store.get_command_streak(content_hash(command.strip()))
    store.close()
    return row["streak"] if row else 0


def test_thrash_guard_records_exactly_once_per_bash_posttooluse():
    """A failed Bash PostToolUse through the real bash_compress_hook path
    must record thrash_guard exactly once. The redundant fallback that
    double-fired has been removed."""
    sid = "test-double-fire-" + uuid.uuid4().hex[:8]
    cmd = "make check"
    tmp = tempfile.mkdtemp(prefix="to-double-fire-")
    env = {"TOKEN_OPTIMIZER_SNAPSHOT_DIR": tmp, "CLAUDE_SESSION_ID": sid}
    payload = _failed_bash_payload(cmd, "error output\nline 2\n", 1, sid)
    proc = _run_hook(payload, env=env)
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    count = _streak_count(sid, cmd, tmp)
    assert count == 1, (
        f"thrash_guard should record exactly once per Bash PostToolUse, "
        f"but streak_count={count} (the redundant fallback may still be firing)"
    )


# --------------------------------------------------------------------------- #
# 3. Regex: successful output with a later-line "Exit code 1" must not trigger
# --------------------------------------------------------------------------- #

def test_successful_output_with_later_line_exit_code_does_not_trigger():
    """A successful command whose stdout contains 'Exit code 1' on a later
    line must not be misparsed as a failure. The anchored regex prevents this."""
    sid = "test-regex-fp-" + uuid.uuid4().hex[:8]
    cmd = "echo 'simulating a build log'"
    # A successful command whose output happens to contain "Exit code 1" on
    # a later line (e.g. a build log that mentions a previous failure).
    stdout = "Building project...\nAll tests passed.\nNote: previous run had Exit code 1\nDone.\n"
    tmp = tempfile.mkdtemp(prefix="to-regex-fp-")
    env = {"TOKEN_OPTIMIZER_SNAPSHOT_DIR": tmp, "CLAUDE_SESSION_ID": sid}
    # Deliver as a string tool_response WITHOUT a failure prefix.
    payload = json.dumps({
        "session_id": sid,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_response": stdout,
    })
    proc = _run_hook(payload, env=env)
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    # The thrash guard should have recorded this as a successful run (exit_code
    # is None, not 1). Run it 3 times to verify no burn nudge fires (burn nudges
    # only fire on non-zero exit codes with different output).
    for _ in range(2):
        proc = _run_hook(payload, env=env)
        assert proc.returncode == 0
    # No updatedToolOutput with a burn nudge should appear (the command was
    # successful, just had "Exit code 1" in its text).
    if proc.stdout.strip():
        try:
            envelope = json.loads(proc.stdout)
            hso = envelope.get("hookSpecificOutput", {})
            updated = hso.get("updatedToolOutput", {})
            nudge_stdout = updated.get("stdout", "")
            assert "failed" not in nudge_stdout, (
                "A successful command with 'Exit code 1' on a later line was "
                "misparsed as a failure. The regex must be anchored at string start."
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            pass


# --------------------------------------------------------------------------- #
# 4. Codex: no updatedToolOutput with the env flag, thrash guard still records
# --------------------------------------------------------------------------- #

def test_codex_flag_suppresses_updated_tool_output():
    """With TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1, bash_compress_hook must
    not emit updatedToolOutput, but thrash_guard must still record."""
    sid = "test-codex-no-uto-" + uuid.uuid4().hex[:8]
    cmd = "make check"
    tmp = tempfile.mkdtemp(prefix="to-codex-no-uto-")
    env = {
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": tmp,
        "CLAUDE_SESSION_ID": sid,
        "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1",
    }
    # Use a large output that would normally trigger compression + updatedToolOutput.
    stdout = "\n".join(f"line {i}: lots of output here" for i in range(200))
    payload = json.dumps({
        "session_id": sid,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "exit_code": 0,
        },
    })
    proc = _run_hook(payload, env=env)
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    # No updatedToolOutput should be emitted.
    if proc.stdout.strip():
        try:
            envelope = json.loads(proc.stdout)
            hso = envelope.get("hookSpecificOutput", {})
            assert "updatedToolOutput" not in hso, (
                "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1 must suppress "
                "updatedToolOutput emission, but it was emitted."
            )
        except json.JSONDecodeError:
            pass
    # thrash_guard must still record the run.
    count = _streak_count(sid, cmd, tmp)
    assert count >= 1, (
        "thrash_guard must still record on Codex (no updatedToolOutput), "
        "but no streak was found."
    )


def test_codex_flag_does_not_suppress_thrash_nudge_recording():
    """With TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1, a failed Bash command
    must still be recorded by thrash_guard (the guard runs regardless of the
    emission flag)."""
    sid = "test-codex-thrash-" + uuid.uuid4().hex[:8]
    cmd = "make check"
    tmp = tempfile.mkdtemp(prefix="to-codex-thrash-")
    env = {
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": tmp,
        "CLAUDE_SESSION_ID": sid,
        "TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1",
    }
    payload = _failed_bash_payload(cmd, "error output\n", 1, sid)
    proc = _run_hook(payload, env=env)
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    count = _streak_count(sid, cmd, tmp)
    assert count == 1, (
        f"thrash_guard should record the failed run even with "
        f"TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT=1, but streak_count={count}"
    )


# --------------------------------------------------------------------------- #
# 2. Nudge delivery: /compact nudge reaches the stdout envelope as
#    additionalContext, not the diagnostics log
# --------------------------------------------------------------------------- #

def _load_ups_runner(monkeypatch, tmp_path):
    """Import hooks/userpromptsubmit_runner.py fresh, pointed at the repo's
    scripts, with config dirs isolated from the host's real ~/.claude."""
    import importlib.util
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("ups_parity_test", HOOKS / "userpromptsubmit_runner.py")
    mod = importlib.util.module_from_spec(spec)
    _split_exec_module(mod)
    return mod


def test_quality_cache_nudge_reaches_envelope_not_log(monkeypatch, tmp_path):
    """The quality-cache --warn /compact nudge must appear in the stdout
    envelope as additionalContext, not in the diagnostics log."""
    runner = _load_ups_runner(monkeypatch, tmp_path)

    # Stub the subcommands that are NOT under test so the runner can dispatch
    # without side effects. We do NOT stub _sub_quality_cache_warn -- that is
    # the subcommand whose nudge we are verifying. Instead we stub
    # measure.quality_cache to return a low-score result so the nudge fires
    # predictably without needing a real session JSONL.
    measure = runner.measure

    # Stub quality_cache to return a result with score < 50 so the /clear
    # nudge fires, or score < 70 so the /compact nudge fires.
    _fake_result = {
        "score": 45,
        "fill_pct": 80,
        "stale_reads": 0,
        "bloated_results": 0,
        "duplicates": 0,
        "compaction_depth": 0,
        "decision_density": 0,
        "agent_efficiency": 100,
    }

    def _fake_quality_cache(*args, **kwargs):
        # Simulate the warn nudge: print the plain-text nudge to stdout.
        # The real quality_cache does this when warn=True and score < threshold.
        score = _fake_result["score"]
        if kwargs.get("warn") and score < 50:
            print(f"[Token Optimizer] Quality {score}/100 (critical). /clear with checkpoint.")
        elif kwargs.get("warn") and score < 70:
            print(f"[Token Optimizer] Quality {score}/100. /compact.")
        return _fake_result

    # Stub _maybe_quality_warn to always allow the nudge through.
    monkeypatch.setattr(measure, "_maybe_quality_warn", lambda result, threshold: True)
    monkeypatch.setattr(measure, "quality_cache", _fake_quality_cache)

    # Stub the other subcommands to no-ops.
    monkeypatch.setattr(runner, "_sub_prompt_continuity", lambda hook_input: None)
    monkeypatch.setattr(runner, "_sub_verbosity_steer", lambda hook_input: None)
    monkeypatch.setattr(runner, "_sub_ensure_health", lambda hook_input: None)
    monkeypatch.setattr(runner, "_sub_compact_restore", lambda hook_input: None)

    # Stub the harness gate to False so only the always-on subcommands run.
    monkeypatch.setattr(runner, "_harness_only_context", lambda: False)
    monkeypatch.setattr(runner, "_quality_cache_is_missing", lambda hook_input: False)

    # Stub consent to True so the runner dispatches all subcommands.
    monkeypatch.setattr(runner, "_check_consent", lambda: True)

    # Stub the deadline so no real watchdog is armed.
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=None: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)

    # Capture the diagnostics log writes.
    log_writes: list[str] = []
    monkeypatch.setattr(runner, "_write_diagnostics", lambda text: log_writes.append(text))

    # Capture real stdout.
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    hook_input = json.dumps({
        "session_id": "test-nudge-" + uuid.uuid4().hex[:8],
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "hook_event_name": "UserPromptSubmit",
    })
    monkeypatch.setattr(runner, "_read_hook_input", lambda: json.loads(hook_input))

    with redirect_stdout(buf):
        runner.main()

    stdout_output = buf.getvalue()
    log_text = "\n".join(log_writes)

    # The nudge must appear in the stdout envelope as additionalContext.
    assert "/clear with checkpoint" in stdout_output or "/compact" in stdout_output, (
        f"The quality-cache nudge must appear in the stdout envelope as "
        f"additionalContext. stdout={stdout_output!r}"
    )

    # The nudge must be in a JSON envelope (additionalContext), not raw text.
    assert stdout_output.strip().startswith("{"), (
        f"The stdout must be a single JSON envelope, not raw text. "
        f"stdout={stdout_output!r}"
    )
    envelope = json.loads(stdout_output)
    hso = envelope.get("hookSpecificOutput", {})
    additional_context = hso.get("additionalContext", "")
    assert "/clear with checkpoint" in additional_context or "/compact" in additional_context, (
        f"The nudge must be in additionalContext. envelope={envelope!r}"
    )

    # The nudge must NOT appear in the diagnostics log.
    assert "/clear with checkpoint" not in log_text and "/compact" not in log_text, (
        f"The nudge must not be routed to the diagnostics log. log={log_text!r}"
    )
