#!/usr/bin/env python3
"""Regression tests for the consolidated SessionStart dispatcher.

The five former SessionStart hooks.json commands are collapsed into ONE that
runs ``hooks/sessionstart_runner.py``, which imports ``measure.py`` once and
runs all five subcommands in-process with per-subcommand failure isolation
under ONE shared deadline.

The bug: Codex enforces a hard 25s ceiling on SessionStart and killed the whole
group ("hook timed out after 25s"). The five entries declared 15 + 20 + 20 + 10
+ 20 = 85s and each re-spawned the launcher chain and re-imported the 1.9 MB
measure.py.

These tests pin the five deliverables:
  (a) ONE process replaces five -- hooks.json declares a single SessionStart
      command pointing at the runner, and the runner holds ONE measure import.
  (b) Every subcommand still runs, and each --once-mark subcommand still WRITES
      its per-session marker (and, unlike --once-per-session, still runs even
      when the marker already exists -- the finding-8 resume/compact semantics).
  (c) One subcommand raising never aborts the others; the hook still exits 0.
  (d) The ONE shared deadline bounds total wall time: once it is exhausted the
      remaining subcommands are skipped instead of each getting a fresh timeout.
  (e) stdout ordering and shape are preserved (dispatch order; each unit intact;
      the Codex/Cowork additionalContext envelope stays a single valid JSON
      document even when both compact-restore subcommands fire).

Run: python3 -m pytest tests/test_sessionstart_runner.py -q
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
RUNNER = HOOKS / "sessionstart_runner.py"
RUN_PY = HOOKS / "run.py"

# The five legacy SessionStart commands this runner replaces. Each substring
# must be GONE from the SessionStart wiring after consolidation.
LEGACY_SUBCOMMANDS = (
    "ensure-health --once-mark",
    "quality-cache --force --quiet --once-mark",
    "compact-restore --compact",
    "read_cache.py --clear-compacted",
    "compact-restore --new-session-only --once-mark",
)


def _load_runner(monkeypatch, tmp_path):
    """Import hooks/sessionstart_runner.py with CLAUDE_PLUGIN_ROOT=REPO so its
    _resolve_measure_dir() finds skills/token-optimizer/scripts/measure.py."""
    assert RUNNER.is_file(), (
        f"the consolidated SessionStart dispatcher is missing: {RUNNER}. "
        "SessionStart is still wired as five separate hooks.json entries."
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # claude_home() honors CLAUDE_CONFIG_DIR only when the directory exists;
    # a missing dir is rejected and falls back to the host's real ~/.claude.
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("ss_runner_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sessionstart_commands():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    out = []
    for group in data["hooks"].get("SessionStart", []):
        for hook in group.get("hooks", []):
            out.append((group.get("matcher"), hook))
    return out


# --------------------------------------------------------------------------- #
# (a) ONE process replaces five
# --------------------------------------------------------------------------- #


def test_sessionstart_is_a_single_consolidated_entry():
    """hooks.json must declare exactly ONE SessionStart command, pointing at the
    runner. Five entries = five launcher chains = five measure.py imports = the
    85s declared budget Codex killed at 25s."""
    commands = _sessionstart_commands()
    assert len(commands) == 1, (
        f"SessionStart must declare exactly ONE hook command, found "
        f"{len(commands)}: " + "; ".join(
            c["command"].split("run.py\" ")[-1][:60] for _m, c in commands
        )
    )
    matcher, hook = commands[0]
    assert matcher is None, (
        "the consolidated entry must carry no matcher: the compact-only "
        "subcommands are gated in-process by the SessionStart source"
    )
    assert "hooks/sessionstart_runner.py" in hook["command"], (
        "the single SessionStart entry must dispatch the consolidated runner"
    )


def test_no_legacy_sessionstart_subcommand_survives_in_hooks_json():
    """None of the five legacy command strings may remain in SessionStart."""
    blob = " ".join(h["command"] for _m, h in _sessionstart_commands())
    still_there = [s for s in LEGACY_SUBCOMMANDS if s in blob]
    assert not still_there, (
        "these legacy SessionStart commands are still separate hooks.json "
        f"entries instead of runner subcommands: {still_there}"
    )


def test_sessionstart_timeout_is_under_the_codex_ceiling():
    """Codex hard-kills SessionStart at 25s. The declared timeout must sit under
    it with room for the launcher/import spawn chain, and the runner's internal
    shared deadline must sit under the declared timeout."""
    _matcher, hook = _sessionstart_commands()[0]
    timeout = hook.get("timeout")
    assert isinstance(timeout, int), "the consolidated entry must declare a timeout"
    assert timeout <= 20, (
        f"declared timeout {timeout}s leaves no margin under Codex's hard 25s "
        "SessionStart ceiling"
    )
    source = RUNNER.read_text(encoding="utf-8")
    assert "_RUNNER_TOTAL_BUDGET = 18.0" in source, (
        "the runner must declare its shared deadline budget explicitly"
    )


def test_runner_imports_measure_exactly_once(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    assert runner.measure is sys.modules.get("measure"), (
        "the runner must bind the single cached measure module, not re-import it"
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


def _install_fake_deadline(monkeypatch, runner, total=18.0):
    monkeypatch.setattr(runner.measure, "HookDeadline", _FakeDeadline)
    monkeypatch.setattr(runner, "_RUNNER_TOTAL_BUDGET", total)
    monkeypatch.setattr(runner, "_RUNNER_DEADLINE", None, raising=False)
    monkeypatch.setattr(runner, "_SUBCOMMANDS_PENDING", 0, raising=False)


def _install_call_recorder(monkeypatch, runner, tmp_path):
    """Stub the five subcommand entrypoints + side-effect helpers.

    ``_mark_ran_this_session`` and ``_once_per_session_marker`` stay REAL, with
    measure.QUALITY_CACHE_DIR repointed into tmp, so the once-mark latching is
    exercised against the real marker primitives.
    """
    calls = {
        "ensure_health": [],
        "quality_cache_force": [],
        "compact_restore_compact": [],
        "clear_compacted": [],
        "compact_restore_new_session": [],
    }

    def _quality_cache(**kw):
        calls["quality_cache_force"].append(kw)
        return 100

    def _ensure_health():
        calls["ensure_health"].append({})

    def _compact_restore(**kw):
        if kw.get("new_session_only"):
            calls["compact_restore_new_session"].append(kw)
        else:
            calls["compact_restore_compact"].append(kw)

    monkeypatch.setattr(runner.measure, "quality_cache", _quality_cache)
    monkeypatch.setattr(runner.measure, "run_ensure_health", _ensure_health)
    monkeypatch.setattr(runner.measure, "compact_restore", _compact_restore)
    monkeypatch.setattr(runner.measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner.measure, "_ensure_health_daemon_revive_first", lambda: None)
    monkeypatch.setattr(runner.measure, "_is_running_from_plugin_cache", lambda: True)
    monkeypatch.setattr(runner.measure, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")
    # Real markers, written into tmp.
    marker_dir = tmp_path / "quality-cache"
    marker_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner.measure, "QUALITY_CACHE_DIR", marker_dir)
    # read_cache.handle_clear_compacted, stubbed at the module the runner
    # lazily imports.
    import read_cache  # noqa: PLC0415

    monkeypatch.setattr(
        read_cache, "handle_clear_compacted",
        lambda hook_input, quiet: calls["clear_compacted"].append((hook_input, quiet)),
    )
    # Consent True: the consent gate has its own test below.
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    return calls, marker_dir


# --------------------------------------------------------------------------- #
# (b) every subcommand still runs, and each is still latched by its once-mark
# --------------------------------------------------------------------------- #


def test_all_five_subcommands_run_in_one_process(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    payload = {
        "session_id": "sess-ss-compact",
        "transcript_path": "/tmp/t.jsonl",
        "source": "compact",
        "hook_event_name": "SessionStart",
    }
    monkeypatch.setattr(runner, "_read_hook_input", lambda: payload)

    assert runner.main() == 0

    assert len(calls["ensure_health"]) == 1, "ensure-health must run"
    assert len(calls["quality_cache_force"]) == 1, "quality-cache --force must run"
    assert len(calls["compact_restore_compact"]) == 1, "compact-restore --compact must run"
    assert len(calls["clear_compacted"]) == 1, "read_cache --clear-compacted must run"
    assert len(calls["compact_restore_new_session"]) == 1, (
        "compact-restore --new-session-only must run"
    )

    # Real call shapes, pinned against the measure.py __main__ dispatch.
    qc = calls["quality_cache_force"][0]
    assert qc == {
        "throttle_seconds": 120, "warn_threshold": 70, "quiet": True,
        "session_jsonl": "/tmp/t.jsonl", "force": True,
        "pure_time_throttle": False, "session_id": "sess-ss-compact", "warn": False,
    }
    assert calls["compact_restore_compact"][0] == {
        "session_id": "sess-ss-compact", "is_compact": True,
    }
    assert calls["compact_restore_new_session"][0] == {
        "session_id": "sess-ss-compact", "new_session_only": True,
    }
    assert calls["clear_compacted"][0][1] is True, "--quiet must be preserved"


def test_once_mark_markers_are_written_for_the_three_marked_subcommands(
    monkeypatch, tmp_path,
):
    """--once-mark WRITES the per-session marker (it never checks it). The marker
    is what latches the UserPromptSubmit --once-per-session copies, so losing it
    would double-run those on every prompt in Cowork."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    sid = "sess-ss-marker"
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": sid, "source": "startup"})

    assert runner.main() == 0

    for tag in ("ensure-health", "quality-cache-force", "compact-restore-new-session"):
        marker = runner.measure._once_per_session_marker(tag, sid)
        assert marker is not None and marker.exists(), (
            f"--once-mark marker for {tag!r} was not written; the "
            "UserPromptSubmit --once-per-session copies will double-run"
        )
    assert marker_dir.exists()


def test_once_mark_still_runs_when_the_marker_already_exists(monkeypatch, tmp_path):
    """finding 8: --once-mark is a WRITE, not a check-then-skip. A resume or a
    post-compaction SessionStart keeps the same session_id, so a check-then-skip
    guard would suppress the SECOND SessionStart of a session."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    sid = "sess-ss-resume"
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": sid, "source": "resume"})

    assert runner.main() == 0
    assert len(calls["ensure_health"]) == 1
    assert len(calls["quality_cache_force"]) == 1
    assert len(calls["compact_restore_new_session"]) == 1

    # Second SessionStart of the SAME session: markers already exist.
    _install_fake_deadline(monkeypatch, runner)
    assert runner.main() == 0
    assert len(calls["ensure_health"]) == 2, (
        "the second SessionStart of a session must still run ensure-health"
    )
    assert len(calls["quality_cache_force"]) == 2, (
        "a resume/compact SessionStart must still re-warm the quality cache"
    )
    assert len(calls["compact_restore_new_session"]) == 2, (
        "a resume/compact SessionStart must still re-emit the checkpoint pointer"
    )


def test_quality_cache_marker_is_claimed_only_after_a_result(monkeypatch, tmp_path):
    """A failed SessionStart warm must leave the shared recovery marker clear.

    The next UserPromptSubmit is the recovery path when the first warm timed
    out or could not write its cache. A successful later warm may claim it.
    """
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    _calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)
    sid = "sess-ss-quality-retry"
    hook_input = {"session_id": sid, "transcript_path": "/tmp/t.jsonl"}
    outcomes = iter((None, 87))
    monkeypatch.setattr(runner.measure, "quality_cache", lambda **kw: next(outcomes))

    runner._sub_quality_cache_force(hook_input)
    marker = runner.measure._once_per_session_marker("quality-cache-force", sid)
    assert marker is not None and not marker.exists(), (
        "a None quality-cache result must not latch UserPromptSubmit recovery"
    )

    runner._sub_quality_cache_force(hook_input)
    assert marker.exists(), "a successful quality-cache result must claim the marker"


def test_compact_matcher_gate_skips_the_compact_only_subcommands(monkeypatch, tmp_path):
    """hooks.json gated entries 3 and 4 behind matcher "compact". A non-compact
    start must skip both, exactly as the matcher did."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-startup", "source": "startup"})

    assert runner.main() == 0
    assert calls["compact_restore_compact"] == [], (
        "compact-restore --compact must not run on a non-compact SessionStart"
    )
    assert calls["clear_compacted"] == [], (
        "read_cache --clear-compacted must not run on a non-compact SessionStart"
    )
    # The three unmatched subcommands still run.
    assert len(calls["ensure_health"]) == 1
    assert len(calls["quality_cache_force"]) == 1
    assert len(calls["compact_restore_new_session"]) == 1


def test_consent_false_runs_only_the_ensure_health_bootstrap(monkeypatch, tmp_path):
    """Pre-consolidation only the `ensure-health` entry matched run.py's
    exempt_commands; the other four were consent-gated. The consolidated runner
    is dispatched with no args, so it must make that decision internally."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)
    monkeypatch.setattr(runner, "_check_consent", lambda: False)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-noconsent", "source": "compact"})

    assert runner.main() == 0
    assert len(calls["ensure_health"]) == 1, (
        "ensure-health bootstraps the consent flags and must stay exempt"
    )
    assert calls["quality_cache_force"] == []
    assert calls["compact_restore_compact"] == []
    assert calls["clear_compacted"] == []
    assert calls["compact_restore_new_session"] == []


def test_run_py_exempts_the_runner_path_from_its_consent_gate():
    """run.py must let `run.py hooks/sessionstart_runner.py` through: it carries
    no exempt arg, so without an explicit exemption the ensure-health bootstrap
    could never fire and consent would stay False forever."""
    source = RUN_PY.read_text(encoding="utf-8")
    assert "hooks/sessionstart_runner.py" in source, (
        "run.py's consent gate does not exempt the consolidated SessionStart "
        "runner; the ensure-health bootstrap is deadlocked"
    )


# --------------------------------------------------------------------------- #
# (c) failure isolation
# --------------------------------------------------------------------------- #


def test_one_failing_subcommand_does_not_abort_the_others(monkeypatch, tmp_path, capsys):
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    def _boom():
        raise RuntimeError("simulated ensure-health failure")

    monkeypatch.setattr(runner.measure, "run_ensure_health", _boom)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-iso", "source": "compact"})

    assert runner.main() == 0, "a subcommand failure must never abort the hook"

    assert len(calls["quality_cache_force"]) == 1
    assert len(calls["compact_restore_compact"]) == 1
    assert len(calls["clear_compacted"]) == 1
    assert len(calls["compact_restore_new_session"]) == 1

    err = capsys.readouterr().err
    assert "ensure-health failed, continuing" in err, (
        "the failure must be logged to stderr, not swallowed silently"
    )


def test_a_subcommand_calling_sys_exit_does_not_abort_the_others(
    monkeypatch, tmp_path,
):
    """Several measure.py dispatch paths end in sys.exit(0). In-process that is
    a SystemExit that would abort the whole runner if not isolated."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    def _exiting(**kw):
        raise SystemExit(0)

    monkeypatch.setattr(runner.measure, "quality_cache", _exiting)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-exit", "source": "compact"})

    assert runner.main() == 0
    assert len(calls["compact_restore_compact"]) == 1, (
        "a SystemExit in quality-cache must not abort compact-restore"
    )
    assert len(calls["compact_restore_new_session"]) == 1


# --------------------------------------------------------------------------- #
# (d) ONE shared deadline bounds total wall time
# --------------------------------------------------------------------------- #


def test_shared_deadline_bounds_total_wall_time(monkeypatch, tmp_path):
    """Five entries meant five independent timeouts (85s declared). One shared
    deadline means the LAST subcommand cannot start a fresh clock: once the
    shared budget is spent, the remaining subcommands are skipped and the hook
    returns, so total wall time stays bounded by the one budget."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner, total=0.5)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    def _slow_ensure_health():
        calls["ensure_health"].append({})
        time.sleep(0.7)  # burn the entire 0.5s shared budget

    monkeypatch.setattr(runner.measure, "run_ensure_health", _slow_ensure_health)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-deadline", "source": "compact"})

    started = time.monotonic()
    assert runner.main() == 0
    elapsed = time.monotonic() - started

    assert len(calls["ensure_health"]) == 1
    # The shared budget is exhausted, so every later subcommand is skipped.
    assert calls["quality_cache_force"] == [], (
        "the exhausted SHARED deadline must skip later subcommands; a "
        "per-subcommand timeout would have let each start a fresh clock"
    )
    assert calls["compact_restore_compact"] == []
    assert calls["clear_compacted"] == []
    assert calls["compact_restore_new_session"] == []
    assert elapsed < 2.0, (
        f"total wall time {elapsed:.2f}s must stay bounded by the one shared "
        "budget, not by the sum of five per-entry timeouts"
    )


def test_one_deadline_is_armed_for_the_whole_runner(monkeypatch, tmp_path):
    """Exactly ONE HookDeadline for the process. Five per-subcommand watchdogs
    would each hold their own os._exit trigger."""
    runner = _load_runner(monkeypatch, tmp_path)
    armed = []

    class _Counting(_FakeDeadline):
        def __init__(self, seconds, message=None):
            super().__init__(seconds, message)
            armed.append(seconds)

    monkeypatch.setattr(runner.measure, "HookDeadline", _Counting)
    monkeypatch.setattr(runner, "_RUNNER_DEADLINE", None, raising=False)
    monkeypatch.setattr(runner, "_SUBCOMMANDS_PENDING", 0, raising=False)
    _calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-onedl", "source": "compact"})

    assert runner.main() == 0
    assert armed == [18.0], (
        f"expected exactly one 18s shared deadline, got {armed}"
    )


def test_budget_is_shared_across_subcommands_not_reset(monkeypatch, tmp_path):
    """The fair-share budget must shrink as the shared deadline drains."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner, total=1.0)
    runner._install_runner_deadline()
    first = runner._runner_budget(8, subcommand_count_hint=5)
    time.sleep(0.4)
    second = runner._runner_budget(8)
    assert second < first, (
        "the shared deadline's remaining time must shrink across subcommands; "
        "a per-subcommand timeout would hand each the same fresh budget"
    )
    runner._clear_runner_deadline()


# --------------------------------------------------------------------------- #
# (e) stdout ordering and shape
# --------------------------------------------------------------------------- #


def test_stdout_is_emitted_in_dispatch_order(monkeypatch, tmp_path):
    """The host consumed five separate stdout streams in hooks.json order.
    Consolidated, the buffered emitter must reproduce that order.

    The stream is now ONE JSON object (see
    tests/test_codex_sessionstart_json_contract.py: Codex rejects a SessionStart
    stdout that starts with ``[``/``{`` and is not a single valid document), so
    "order" is the order of the plain-text units INSIDE
    ``hookSpecificOutput.additionalContext``. The systemMessage unit is carried
    in the object's own ``systemMessage`` field, which is a sibling of
    ``additionalContext`` and therefore has no position in that text.
    """
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    _calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    monkeypatch.setattr(runner.measure, "run_ensure_health",
                        lambda: print("MARK-ensure-health"))
    monkeypatch.setattr(
        runner.measure, "quality_cache",
        lambda **kw: print(json.dumps({"systemMessage": "MARK-quality-cache"})),
    )

    def _compact_restore(**kw):
        if kw.get("new_session_only"):
            print("MARK-new-session")
        else:
            print("MARK-compact")

    monkeypatch.setattr(runner.measure, "compact_restore", _compact_restore)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-order", "source": "compact"})

    import io as _io
    from contextlib import redirect_stdout as _redirect

    cap = _io.StringIO()
    with _redirect(cap):
        assert runner.main() == 0
    out = cap.getvalue()

    for mark in ("MARK-ensure-health", "MARK-quality-cache", "MARK-compact",
                 "MARK-new-session"):
        assert mark in out, f"{mark} missing from consolidated stdout"

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    positions = [
        context.index("MARK-ensure-health"),
        context.index("MARK-compact"),
        context.index("MARK-new-session"),
    ]
    assert positions == sorted(positions), (
        f"stdout order must match hooks.json dispatch order, got {positions}"
    )

    # Shape preserved: the quality-cache systemMessage content survives as the
    # object's systemMessage, not folded into the context text.
    assert payload["systemMessage"] == "MARK-quality-cache", (
        "the systemMessage unit must survive consolidation intact"
    )


def test_codex_compact_start_emits_one_valid_additional_context_envelope(
    monkeypatch, tmp_path,
):
    """On Codex, SessionStart stdout must be empty or valid JSON. Both
    compact-restore subcommands fire on a compact start, so their payloads must
    land in ONE additionalContext envelope, not two JSON documents."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    _calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)

    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "codex")
    monkeypatch.setattr(runner.measure, "is_cowork", lambda: False)
    # Silent on the wrapped path: only compact-restore output is enveloped.
    monkeypatch.setattr(runner.measure, "run_ensure_health", lambda: None)
    monkeypatch.setattr(runner.measure, "quality_cache", lambda **kw: None)

    def _compact_restore(**kw):
        print("RESTORE-new-session" if kw.get("new_session_only") else "RESTORE-compact")

    monkeypatch.setattr(runner.measure, "compact_restore", _compact_restore)
    monkeypatch.setattr(
        runner, "_read_hook_input",
        lambda: {"session_id": "sess-ss-codex", "source": "compact",
                 "hook_event_name": "SessionStart"},
    )

    import io as _io
    from contextlib import redirect_stdout as _redirect

    cap = _io.StringIO()
    with _redirect(cap):
        assert runner.main() == 0
    out = cap.getvalue().strip()

    objs = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(objs) == 1, (
        f"Codex needs a single JSON document on SessionStart stdout, got "
        f"{len(objs)}: {out!r}"
    )
    envelope = objs[0]["hookSpecificOutput"]
    assert envelope["hookEventName"] == "SessionStart", (
        "the envelope event must come from the firing hook's stdin payload"
    )
    ctx = envelope["additionalContext"]
    assert "RESTORE-compact" in ctx and "RESTORE-new-session" in ctx, (
        "both compact-restore payloads must survive in the single envelope"
    )
    assert ctx.index("RESTORE-compact") < ctx.index("RESTORE-new-session"), (
        "envelope content must keep dispatch order"
    )


def test_clear_compacted_never_writes_to_stdout(monkeypatch, tmp_path, capsys):
    """read_cache --clear-compacted is stderr-only by design. Sharing one stdout
    stream, a stray print from it would corrupt the SessionStart context."""
    runner = _load_runner(monkeypatch, tmp_path)
    _install_fake_deadline(monkeypatch, runner)
    calls, _marker_dir = _install_call_recorder(monkeypatch, runner, tmp_path)
    monkeypatch.setattr(runner.measure, "run_ensure_health", lambda: None)
    monkeypatch.setattr(runner.measure, "quality_cache", lambda **kw: None)
    monkeypatch.setattr(runner.measure, "compact_restore", lambda **kw: None)
    monkeypatch.setattr(runner, "_read_hook_input",
                        lambda: {"session_id": "sess-ss-quiet", "source": "compact"})

    assert runner.main() == 0
    captured = capsys.readouterr()
    assert len(calls["clear_compacted"]) == 1
    assert captured.out == "", (
        f"SessionStart stdout must stay empty when no subcommand has context to "
        f"inject, got {captured.out!r}"
    )

    # The empty-payload failure branch stays loud on stderr, never stdout.
    runner._sub_clear_compacted({})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no stdin hook input" in captured.err, (
        "the empty-payload clear-compacted failure must stay loud on stderr"
    )


# --------------------------------------------------------------------------- #
# (f) Signature-drift guard: call each _sub_* handler end-to-end against the
#     REAL measure.py / read_cache.py (NO monkeypatching of their functions).
#     A future kwarg/param rename raises TypeError out of the handler, which
#     the handlers do NOT catch, so this fails RED here instead of being
#     silently swallowed by _run_safely in production.
# --------------------------------------------------------------------------- #

_FRESH_MEASURE_MODULES = (
    "measure", "runtime_env", "plugin_env", "hook_io", "hook_runtime",
    "codex_session", "read_cache",
)


def _load_runner_fresh_measure(monkeypatch, tmp_path):
    """Load the runner against a FRESHLY imported measure.py so every
    import-time path global resolves under the tmp env. Nothing under the
    host's ~/.claude is touched: HOME and CLAUDE_CONFIG_DIR point into tmp, the
    runtime is foreign (run_ensure_health returns early at the
    _is_foreign_runtime gate) and the daemon is disabled in config."""
    saved = {k: sys.modules.get(k) for k in _FRESH_MEASURE_MODULES}
    for k in _FRESH_MEASURE_MODULES:
        sys.modules.pop(k, None)

    claude_dir = tmp_path / "claude"
    cfg_dir = claude_dir / "token-optimizer"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        '{"daemon_disabled": true, "enterprise_consent_shown": true, '
        '"v5_welcome_shown": true}',
        encoding="utf-8",
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "opencode")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CONTAINER_ID", raising=False)

    spec = importlib.util.spec_from_file_location(
        "ss_runner_integration_under_test", RUNNER
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _restore():
        for k in _FRESH_MEASURE_MODULES:
            if saved[k] is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = saved[k]

    return mod, _restore, transcript


def test_sub_handlers_call_real_measure_without_signature_error(monkeypatch, tmp_path):
    """Each _sub_* handler calls the REAL entrypoints with the exact kwargs the
    runner uses. Called directly (NOT via main()/_run_safely) precisely so a
    signature drift surfaces as a TypeError instead of a stderr line."""
    runner, restore, transcript = _load_runner_fresh_measure(monkeypatch, tmp_path)
    try:
        sid = "sess-ss-integration"
        hook_input = {
            "session_id": sid,
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "source": "compact",
            "hook_event_name": "SessionStart",
        }
        sink: list = []
        try:
            runner._sub_ensure_health(hook_input)
            runner._sub_quality_cache_force(hook_input)
            runner._sub_compact_restore_compact(hook_input, sink)
            runner._sub_clear_compacted(hook_input)
            runner._sub_compact_restore_new_session(hook_input, sink)
        except TypeError as e:
            pytest.fail(
                f"signature drift: a _sub_* handler called its entrypoint with a "
                f"renamed/removed kwarg: {type(e).__name__}: {e}"
            )

        # Positive proof the handlers reached the REAL marker primitives.
        m = runner.measure
        for tag in ("ensure-health", "quality-cache-force",
                    "compact-restore-new-session"):
            marker = m._once_per_session_marker(tag, sid)
            assert marker is not None and marker.exists(), (
                f"real --once-mark marker for {tag!r} was not written; the "
                "handler did not reach measure._mark_ran_this_session"
            )
    finally:
        restore()


def test_signature_drift_fails_red(monkeypatch, tmp_path):
    """Mutation guard for the guard: a renamed measure kwarg must propagate out
    of the handler, not be swallowed."""
    runner, restore, transcript = _load_runner_fresh_measure(monkeypatch, tmp_path)
    try:
        real_qc = runner.measure.quality_cache

        def _drifted_qc(**kw):
            if "warn_threshold" in kw:
                raise TypeError("simulated drift: warn_threshold renamed")
            return real_qc(**kw)

        runner.measure.quality_cache = _drifted_qc
        with pytest.raises(TypeError, match="warn_threshold"):
            runner._sub_quality_cache_force({
                "session_id": "sess-ss-drift",
                "transcript_path": str(transcript),
            })
    finally:
        restore()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
