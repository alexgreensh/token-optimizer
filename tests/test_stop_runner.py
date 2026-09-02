#!/usr/bin/env python3
"""Regression tests for the consolidated Stop + SessionEnd dispatcher.

The three former Stop hooks.json commands (compact-capture --trigger stop,
session-end-flush --trigger stop --quiet --defer, keepwarm-arm --quiet) are
collapsed into ONE that runs ``hooks/stop_runner.py``, which imports
``measure.py`` once and runs all three subcommands in-process with
per-subcommand failure isolation under ONE shared deadline. The SessionEnd
entry (session-end-flush --trigger end --defer) joins the same runner, branching
on ``hook_event_name`` so the trigger value ``end`` is preserved exactly.

The bug: three separate Stop entries each re-spawned the launcher chain and
re-imported the 1.9 MB measure.py (682ms cold, the container steady state
because __pycache__ never persists on a read-only scripts dir). 25s of declared
budget, 2.4s of pure import cost before any real work. A sustained container
workload recorded widespread Stop hook TIMEOUTS.

These tests pin the deliverables:
  (a) ONE process replaces three -- hooks.json declares a single Stop command
      pointing at the runner, and the runner holds ONE measure import.
  (b) Every Stop subcommand still runs with semantics intact (trigger values,
      --defer behavior, --quiet stdout suppression, keepwarm-arm's
      anti-recursion guard).
  (c) One subcommand raising OR calling sys.exit never aborts the others; the
      hook still exits 0.
  (d) The ONE shared deadline bounds total wall time: once exhausted, remaining
      subcommands are skipped instead of each getting a fresh timeout.
  (e) stdout order and shape are preserved (dispatch order; each unit intact).
  (f) SessionEnd joins the runner: a separate hooks.json entry pointing at the
      same runner file, preserving async=true, timeout=60, trigger=end, --defer.

Run: python3 -m pytest tests/test_stop_runner.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
HOOKS_JSON = HOOKS / "hooks.json"
RUNNER = HOOKS / "stop_runner.py"
RUN_PY = HOOKS / "run.py"

# The three legacy Stop commands this runner replaces. Each substring must be
# GONE from the Stop wiring after consolidation.
LEGACY_STOP_SUBCOMMANDS = (
    "compact-capture --trigger stop",
    "session-end-flush --trigger stop",
    "keepwarm-arm",
)

# The legacy SessionEnd command that joins the runner.
LEGACY_SESSIONEND_SUBCOMMAND = "session-end-flush --trigger end"


def _load_runner(monkeypatch, tmp_path):
    """Import hooks/stop_runner.py with CLAUDE_PLUGIN_ROOT=REPO so its
    _resolve_measure_dir() finds skills/token-optimizer/scripts/measure.py."""
    assert RUNNER.is_file(), (
        f"the consolidated Stop dispatcher is missing: {RUNNER}. "
        "Stop is still wired as three separate hooks.json entries."
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # claude_home() honors CLAUDE_CONFIG_DIR only when the directory exists;
    # a missing dir is rejected and falls back to the host's real ~/.claude.
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("stop_runner_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stop_commands():
    """Return [(matcher, hook), ...] for every Stop hook command in hooks.json."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    out = []
    for group in data["hooks"].get("Stop", []):
        for hook in group.get("hooks", []):
            out.append((group.get("matcher"), hook))
    return out


def _sessionend_commands():
    """Return [(matcher, hook), ...] for every SessionEnd hook command."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    out = []
    for group in data["hooks"].get("SessionEnd", []):
        for hook in group.get("hooks", []):
            out.append((group.get("matcher"), hook))
    return out


# --------------------------------------------------------------------------- #
# (a) ONE process replaces three
# --------------------------------------------------------------------------- #


def test_stop_is_a_single_consolidated_entry():
    """hooks.json must declare exactly ONE Stop hook command, pointing at the
    runner. Three entries = three launcher chains = three measure.py imports =
    the 25s declared budget that timed out under container workloads."""
    commands = _stop_commands()
    assert len(commands) == 1, (
        f"Stop must declare exactly ONE hook command, found "
        f"{len(commands)}: " + "; ".join(
            c["command"].split("run.py\" ")[-1][:60] for _m, c in commands
        )
    )
    _matcher, hook = commands[0]
    assert "hooks/stop_runner.py" in hook["command"], (
        "the single Stop entry must dispatch the consolidated runner"
    )


def test_no_legacy_stop_subcommand_survives_in_hooks_json():
    """None of the three legacy Stop command strings may remain in Stop."""
    blob = " ".join(h["command"] for _m, h in _stop_commands())
    still_there = [s for s in LEGACY_STOP_SUBCOMMANDS if s in blob]
    assert not still_there, (
        "these legacy Stop commands are still separate hooks.json "
        f"entries instead of runner subcommands: {still_there}"
    )


def test_stop_timeout_is_consolidated():
    """The three legacy entries declared 12 + 8 + 5 = 25s. The consolidated
    entry must declare a single timeout, and the runner's internal shared
    deadline must sit under it with margin."""
    _matcher, hook = _stop_commands()[0]
    timeout = hook.get("timeout")
    assert isinstance(timeout, int), "the consolidated entry must declare a timeout"
    assert timeout <= 15, (
        f"declared timeout {timeout}s is too generous for a single-process "
        "consolidated runner; the whole point is to cut the 25s declared budget"
    )
    source = RUNNER.read_text(encoding="utf-8")
    assert "_RUNNER_TOTAL_BUDGET" in source, (
        "the runner must declare its shared deadline budget explicitly"
    )


def test_runner_imports_measure_exactly_once(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    assert runner.measure is sys.modules.get("measure"), (
        "the runner must bind the single cached measure module, not re-import it"
    )


# --------------------------------------------------------------------------- #
# (f) SessionEnd joins the runner
# --------------------------------------------------------------------------- #


def test_sessionend_points_at_the_same_runner():
    """SessionEnd is a separate hooks.json entry (different event, async=true,
    timeout=60) but dispatches the SAME runner file, branching on
    hook_event_name. This saves one measure.py import per session end."""
    commands = _sessionend_commands()
    assert len(commands) == 1, (
        f"SessionEnd must declare exactly ONE hook command, found {len(commands)}"
    )
    _matcher, hook = commands[0]
    assert "hooks/stop_runner.py" in hook["command"], (
        "SessionEnd must dispatch the consolidated stop_runner.py"
    )
    # The async flag and 60s timeout are host-level semantics that must be
    # preserved on the hooks.json entry itself.
    assert hook.get("async") is True, (
        "SessionEnd must keep async=true (host fire-and-forget semantics)"
    )
    assert hook.get("timeout") == 60, (
        "SessionEnd must keep timeout=60 (the 60s async budget)"
    )


def test_no_legacy_sessionend_subcommand_survives_in_hooks_json():
    """The legacy session-end-flush --trigger end command string must be gone
    from SessionEnd (replaced by the runner)."""
    blob = " ".join(h["command"] for _m, h in _sessionend_commands())
    assert LEGACY_SESSIONEND_SUBCOMMAND not in blob, (
        "the legacy SessionEnd command is still a direct measure.py invocation "
        "instead of dispatching the runner"
    )


# --------------------------------------------------------------------------- #
# Shared harness for the behavioural tests
# --------------------------------------------------------------------------- #


class _FakeDeadline:
    """A HookDeadline stand-in that reports real remaining time but never calls
    os._exit (which would kill the pytest process)."""

    def __init__(self, seconds, message=None):
        self.seconds = float(seconds)
        self.end = time.monotonic() + self.seconds
        self.cancelled = False

    def start(self):
        return self

    def remaining(self):
        return max(0.0, self.end - time.monotonic())

    def cancel(self):
        self.cancelled = True


def _install_fake_deadline(monkeypatch, runner, total=13.0):
    monkeypatch.setattr(runner.measure, "HookDeadline", _FakeDeadline)
    monkeypatch.setattr(runner, "_RUNNER_TOTAL_BUDGET", total)
    monkeypatch.setattr(runner, "_RUNNER_DEADLINE", None, raising=False)
    monkeypatch.setattr(runner, "_SUBCOMMANDS_PENDING", 0, raising=False)


def _install_call_recorder(monkeypatch, runner):
    """Stub the three Stop subcommand entrypoints + side-effect helpers to
    record calls. Returns a dict of call logs keyed by subcommand."""
    calls = {
        "compact_capture": [],
        "session_end_flush": [],
        "keepwarm_arm": [],
    }

    def _compact_capture(**kw):
        calls["compact_capture"].append(kw)

    def _dispatch_session_end_flush(args):
        calls["session_end_flush"].append(list(args))

    def _write_keepwarm_arm(session_id, transcript_path, now=None):
        calls["keepwarm_arm"].append(
            {"session_id": session_id, "transcript_path": transcript_path}
        )
        return None

    monkeypatch.setattr(runner.measure, "compact_capture", _compact_capture)
    monkeypatch.setattr(
        runner.measure, "_dispatch_session_end_flush", _dispatch_session_end_flush
    )
    monkeypatch.setattr(
        runner.measure, "write_keepwarm_arm_record", _write_keepwarm_arm
    )
    # Side-effect helpers: must not raise, must not touch the real filesystem.
    monkeypatch.setattr(runner.measure, "_install_hook_budget", lambda seconds=8: object())
    monkeypatch.setattr(runner.measure, "_clear_hook_budget", lambda deadline: None)
    return calls


# --------------------------------------------------------------------------- #
# (b) every Stop subcommand still runs, with semantics intact
# --------------------------------------------------------------------------- #


def test_all_three_stop_subcommands_run_in_one_process(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    payload = {
        "session_id": "sess-stop-abc",
        "transcript_path": "/tmp/t.jsonl",
        "hook_event_name": "Stop",
    }
    monkeypatch.setattr(runner, "_read_hook_input", lambda: payload)

    assert runner.main() == 0

    assert len(calls["compact_capture"]) == 1, "compact-capture must run"
    assert len(calls["session_end_flush"]) == 1, "session-end-flush must run"
    assert len(calls["keepwarm_arm"]) == 1, "keepwarm-arm must run"

    # Verify the REAL call shapes, pinned against the measure.py __main__
    # dispatch.
    cc_kw = calls["compact_capture"][0]
    assert cc_kw == {
        "transcript_path": "/tmp/t.jsonl",
        "session_id": "sess-stop-abc",
        "trigger": "stop",
    }, "compact-capture must be called with trigger=stop and the stdin fields"

    # session-end-flush must receive the exact args list the __main__ dispatch
    # got, preserving --trigger stop --quiet --defer.
    sef_args = calls["session_end_flush"][0]
    assert sef_args[0] == "session-end-flush", "first arg must be the subcommand name"
    assert "--trigger" in sef_args and "stop" in sef_args, (
        "trigger value 'stop' must be preserved (latency budget table matches on it)"
    )
    assert "--defer" in sef_args, "--defer flag must be preserved"
    assert "--quiet" in sef_args, "--quiet flag must be preserved"

    # keepwarm-arm must receive session_id and transcript_path from stdin.
    kw = calls["keepwarm_arm"][0]
    assert kw["session_id"] == "sess-stop-abc"
    assert kw["transcript_path"] == "/tmp/t.jsonl"


def test_sessionend_runs_only_session_end_flush_with_trigger_end(monkeypatch, tmp_path):
    """When hook_event_name is SessionEnd, the runner runs ONLY
    session-end-flush --trigger end --defer. compact-capture and keepwarm-arm
    must NOT run (they are Stop-only)."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    payload = {
        "session_id": "sess-end-xyz",
        "transcript_path": "/tmp/t.jsonl",
        "hook_event_name": "SessionEnd",
    }
    monkeypatch.setattr(runner, "_read_hook_input", lambda: payload)

    assert runner.main() == 0

    assert calls["compact_capture"] == [], (
        "compact-capture must NOT run on SessionEnd (Stop-only subcommand)"
    )
    assert calls["keepwarm_arm"] == [], (
        "keepwarm-arm must NOT run on SessionEnd (Stop-only subcommand)"
    )
    assert len(calls["session_end_flush"]) == 1, (
        "session-end-flush must run on SessionEnd"
    )

    sef_args = calls["session_end_flush"][0]
    assert "--trigger" in sef_args and "end" in sef_args, (
        "trigger value 'end' must be preserved for SessionEnd"
    )
    assert "--defer" in sef_args, "--defer flag must be preserved for SessionEnd"


def test_stop_subcommands_run_in_dispatch_order(monkeypatch, tmp_path):
    """The three subcommands must run in the same order as the hooks.json
    entries they replace: compact-capture, session-end-flush, keepwarm-arm."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    _install_call_recorder(monkeypatch, runner)

    order = []

    def _cc(**kw):
        order.append("compact-capture")

    def _sef(args):
        order.append("session-end-flush")

    def _kw(sid, tp, now=None):
        order.append("keepwarm-arm")
        return None

    monkeypatch.setattr(runner.measure, "compact_capture", _cc)
    monkeypatch.setattr(runner.measure, "_dispatch_session_end_flush", _sef)
    monkeypatch.setattr(runner.measure, "write_keepwarm_arm_record", _kw)

    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "s", "hook_event_name": "Stop"})
    assert runner.main() == 0
    assert order == ["compact-capture", "session-end-flush", "keepwarm-arm"], (
        f"dispatch order must match the legacy hooks.json entry order, got {order}"
    )


# --------------------------------------------------------------------------- #
# (c) failure isolation: exception AND SystemExit
# --------------------------------------------------------------------------- #


def test_one_failing_subcommand_does_not_abort_the_others(monkeypatch, tmp_path, capsys):
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    def _boom(**kw):
        raise RuntimeError("simulated compact-capture failure")

    monkeypatch.setattr(runner.measure, "compact_capture", _boom)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-iso", "hook_event_name": "Stop"})

    assert runner.main() == 0, "a subcommand failure must never abort the hook (exit 0)"

    # The other two subcommands still ran.
    assert len(calls["session_end_flush"]) == 1
    assert len(calls["keepwarm_arm"]) == 1

    err = capsys.readouterr().err
    assert "compact-capture failed, continuing" in err, (
        "the failure must be logged to stderr, not swallowed silently"
    )


def test_a_subcommand_calling_sys_exit_does_not_abort_the_others(monkeypatch, tmp_path):
    """Several measure.py dispatch paths end in sys.exit(0) (notably
    session-end-flush and keepwarm-arm). In-process that is a SystemExit that
    would abort the whole runner if not isolated."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls = _install_call_recorder(monkeypatch, runner)

    def _exiting(args):
        raise SystemExit(0)

    monkeypatch.setattr(runner.measure, "_dispatch_session_end_flush", _exiting)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-exit", "hook_event_name": "Stop"})

    assert runner.main() == 0
    assert len(calls["compact_capture"]) == 1, (
        "a SystemExit in session-end-flush must not abort compact-capture"
    )
    assert len(calls["keepwarm_arm"]) == 1, (
        "a SystemExit in session-end-flush must not abort keepwarm-arm"
    )


# --------------------------------------------------------------------------- #
# (d) ONE shared deadline bounds total wall time
# --------------------------------------------------------------------------- #


def test_shared_deadline_bounds_total_wall_time(monkeypatch, tmp_path):
    """Three entries meant three independent timeouts (25s declared). One shared
    deadline means the LAST subcommand cannot start a fresh clock: once the
    shared budget is spent, the remaining subcommands are skipped and the hook
    returns, so total wall time stays bounded by the one budget."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner, total=0.5)
    calls = _install_call_recorder(monkeypatch, runner)

    def _slow_compact_capture(**kw):
        calls["compact_capture"].append(kw)
        time.sleep(0.7)  # burn the entire 0.5s shared budget

    monkeypatch.setattr(runner.measure, "compact_capture", _slow_compact_capture)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-deadline", "hook_event_name": "Stop"})

    started = time.monotonic()
    assert runner.main() == 0
    elapsed = time.monotonic() - started

    assert len(calls["compact_capture"]) == 1
    # The shared budget is exhausted, so every later subcommand is skipped.
    assert calls["session_end_flush"] == [], (
        "the exhausted SHARED deadline must skip later subcommands; a "
        "per-subcommand timeout would have let each start a fresh clock"
    )
    assert calls["keepwarm_arm"] == [], (
        "the exhausted SHARED deadline must skip keepwarm-arm"
    )
    assert elapsed < 2.0, (
        f"total wall time must be bounded by the shared budget, took {elapsed:.1f}s"
    )


# --------------------------------------------------------------------------- #
# (e) stdout order and shape are preserved
# --------------------------------------------------------------------------- #


def test_stop_stdout_is_empty_under_quiet(monkeypatch, tmp_path, capsys):
    """All three Stop subcommands run with --quiet (compact-capture and
    keepwarm-arm suppress their print; session-end-flush --defer spawns a
    detached worker and prints nothing). The consolidated runner must produce
    no stdout, preserving the host's no-output contract for Stop."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    _install_call_recorder(monkeypatch, runner)

    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "s", "hook_event_name": "Stop"})
    assert runner.main() == 0
    out = capsys.readouterr().out
    assert out == "", (
        f"Stop runner must produce no stdout under --quiet, got: {out!r}"
    )


def test_stop_stdout_preserves_order_when_emitted(monkeypatch, tmp_path, capsys):
    """If a subcommand does emit stdout (e.g. a non-quiet invocation path), the
    runner buffers and emits in dispatch order, not interleaved."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)

    def _cc(**kw):
        print("CC-OUTPUT")

    def _sef(args):
        print("SEF-OUTPUT")

    def _kw(sid, tp, now=None):
        print("KW-OUTPUT")
        return None

    monkeypatch.setattr(runner.measure, "compact_capture", _cc)
    monkeypatch.setattr(runner.measure, "_dispatch_session_end_flush", _sef)
    monkeypatch.setattr(runner.measure, "write_keepwarm_arm_record", _kw)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "s", "hook_event_name": "Stop"})

    assert runner.main() == 0
    out = capsys.readouterr().out
    # Each subcommand's stdout is a self-contained unit, emitted in dispatch
    # order: compact-capture, session-end-flush, keepwarm-arm.
    assert "CC-OUTPUT" in out
    assert out.index("CC-OUTPUT") < out.index("SEF-OUTPUT")
    assert out.index("SEF-OUTPUT") < out.index("KW-OUTPUT")


def test_hook_always_exits_zero(monkeypatch, tmp_path):
    """The hook must exit 0 always, regardless of subcommand outcomes."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    _install_call_recorder(monkeypatch, runner)

    def _boom_cc(**kw):
        raise RuntimeError("boom")

    def _boom_sef(args):
        raise SystemExit(1)

    def _boom_kw(sid, tp, now=None):
        raise ValueError("boom")
        return None

    monkeypatch.setattr(runner.measure, "compact_capture", _boom_cc)
    monkeypatch.setattr(runner.measure, "_dispatch_session_end_flush", _boom_sef)
    monkeypatch.setattr(runner.measure, "write_keepwarm_arm_record", _boom_kw)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "s", "hook_event_name": "Stop"})

    assert runner.main() == 0, "the hook must exit 0 even if every subcommand fails"
