#!/usr/bin/env python3
"""Single-import Stop + SessionEnd dispatcher.

Replaces the THREE separate ``Stop`` hook commands in hooks.json that each
spawned ``python-launcher.sh -> run.py -> module_runner.py -> runpy(measure.py)``
and caused each subcommand to start a separate interpreter and import
``measure.py`` independently:

  1. ``measure.py compact-capture --trigger stop --quiet``             (timeout 12)
  2. ``measure.py session-end-flush --trigger stop --quiet --defer``   (timeout 8)
  3. ``measure.py keepwarm-arm --quiet``                               (timeout 5)

The separate processes also repeat cold imports when a read-only plugin
installation cannot retain ``__pycache__``. Consolidation pays that startup
cost once, then runs the real work under one shared deadline.

The ``SessionEnd`` hook (a fourth entry: ``session-end-flush --trigger end
--defer``, async, timeout 60) joins this runner too. It is a separate hooks.json
entry (different event, ``async: true``, timeout 60) but dispatches the SAME
runner file, branching on ``hook_event_name`` so the trigger value ``end`` is
preserved exactly. This saves one more measure.py import per session end.

This is the same consolidation the ``SessionStart`` and ``UserPromptSubmit``
groups received (``hooks/sessionstart_runner.py``,
``hooks/userpromptsubmit_runner.py``); the structure deliberately mirrors those
files.

Key properties:
  - ONE shared ``HookDeadline`` (13s for Stop, 2s margin under the hooks.json
    timeout of 15; 58s for SessionEnd, 2s margin under timeout 60) replaces the
    three independent per-entry timeouts (12 + 8 + 5 = 25s). Remaining time is
    budgeted fairly across the subcommands still pending (``_runner_budget``);
    the shared deadline's ``os._exit(0)`` is the ONLY kill switch in the
    process, so an early subcommand hang can never preemptively kill later ones.
  - The ``--defer`` behaviour is preserved EXACTLY: ``session-end-flush`` calls
    ``measure._dispatch_session_end_flush`` with the same args list the
    ``__main__`` dispatch received, so the detached worker gets the same
    ``--trigger`` / ``--defer`` / ``--quiet`` flags.
  - The trigger values ``stop`` and ``end`` are preserved EXACTLY so entry
    budget rules can distinguish the Stop and SessionEnd work.
  - The ``async: true`` flag on the SessionEnd hooks.json entry is a host-level
    semantic (fire-and-forget); the runner itself is the same for both events.
  - No once-per-session latching exists in the Stop/SessionEnd chain (none of
    the three subcommands use ``_mark_ran_this_session`` or
    ``_ran_once_this_session``), so there is nothing to preserve there.
  - stdout from all subcommands is captured through one buffered emitter and
    emitted in dispatch order at the end of ``main()``. Under ``--quiet`` (the
    Stop path) all three subcommands produce no stdout; the SessionEnd path
    likewise produces none (``_dispatch_session_end_flush`` only spawns a
    detached worker). The buffer is a safety net for any unexpected output.
  - One subcommand throwing/aborting never aborts the others (each is wrapped in
    ``_run_safely``); the hook always exits 0.
  - No consent gate: the Stop and SessionEnd subcommands are all data
    collection, none bootstrap the consent flags (unlike SessionStart /
    UserPromptSubmit which contain ensure-health). run.py's consent gate
    handles them: when consent is False, run.py returns 0 and the runner never
    fires. The runner is NOT in run.py's exempt list, preserving the
    consent-gated semantics of the three legacy entries.

The runner calls the same module-level entrypoints used by the ``__main__``
dispatch, preserving their arguments and behavior while only changing how
they are scheduled.

Run: ``hooks/stop_runner.py`` (via run.py -> module_runner.py).
"""
from __future__ import annotations

import io
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path


def _resolve_measure_dir() -> str:
    """Locate the directory holding measure.py so ``import measure`` works.

    module_runner.py puts THIS file's parent (``hooks/``) on ``sys.path[0]``;
    measure.py lives in ``skills/token-optimizer/scripts/``. Resolve it from
    ``CLAUDE_PLUGIN_ROOT`` (set by the host before hook invocation) with a
    ``__file__``-relative fallback (the plugin root is this file's
    grandparent), and insert it ahead of ``hooks/`` so measure.py and its
    sibling modules (runtime_env, plugin_env, hook_io, hook_runtime) resolve.
    """
    candidates: list[Path] = []
    pr = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if pr:
        candidates.append(Path(pr) / "skills" / "token-optimizer" / "scripts")
    try:
        candidates.append(
            Path(__file__).resolve().parent.parent
            / "skills" / "token-optimizer" / "scripts"
        )
    except Exception:
        pass
    for c in candidates:
        try:
            if (c / "measure.py").is_file():
                return str(c.resolve())
        except OSError:
            continue
    # Last resort: assume CWD-relative scripts layout (manual/dev invocation).
    return str((Path.cwd() / "skills" / "token-optimizer" / "scripts").resolve())


_MEASURE_DIR = _resolve_measure_dir()
if _MEASURE_DIR and _MEASURE_DIR not in sys.path:
    sys.path.insert(0, _MEASURE_DIR)

import measure  # noqa: E402  (path bootstrapped above)


def _read_hook_input() -> dict:
    """Read the hook stdin JSON once, non-blocking, shared across subcommands.

    Uses measure's own shared reader (Windows pipe-peek + Unix select) so the
    behavior matches what each ``__main__`` handler saw individually. 1 MB cap
    is generous: the largest handler (compact-capture) reads the default 65536,
    and none of the three Stop handlers reads more.
    """
    try:
        return measure._read_stdin_hook_input(max_bytes=1_000_000) or {}
    except Exception:
        return {}


def _record_payload_shape(hook_input: dict) -> None:
    """Remember WHICH identity fields the host sent this Stop hook (never their
    values), so the doctor can tell "host sent nothing" from "we lost it"
    (issue #278). Best effort."""
    try:
        def has(key):
            return "yes" if isinstance(hook_input.get(key), str) and hook_input.get(key) else "no"
        measure._record_hook_status(
            "stop_payload",
            f"stdin fields: {len(hook_input)}; session_id={has('session_id')}; "
            f"transcript_path={has('transcript_path')}; "
            f"CODEX_THREAD_ID env={'yes' if os.environ.get('CODEX_THREAD_ID') else 'no'}",
        )
    except Exception:
        pass


def _is_session_end(hook_input: dict) -> bool:
    """True when the firing hook is SessionEnd, not Stop.

    The host sends ``hook_event_name`` in the stdin payload. The runner is
    registered under both ``Stop`` and ``SessionEnd`` in hooks.json; this check
    routes to the right subcommand set. Case-insensitive to tolerate host
    variations. When the field is missing, default to the Stop path (the
    primary use case -- running all three subcommands when only
    session-end-flush was needed is harmless and fail-open; the reverse would
    miss the checkpoint and keepwarm arm).
    """
    event = str(hook_input.get("hook_event_name") or "").strip().lower()
    return event == "sessionend"


def _run_safely(name: str, fn, *args, **kwargs) -> None:
    """Run fn, swallow any failure to stderr, never propagate.

    Catches ``Exception``, ``SystemExit``, and ``measure._HookTimeout`` (a
    ``BaseException``) so one subcommand's bug, internal ``sys.exit()``, or
    legacy timeout signal cannot abort the others. The ``session-end-flush``
    and ``keepwarm-arm`` dispatch blocks both end in ``sys.exit(0)``; in-process
    that is a ``SystemExit`` that this guard isolates. The original
    ``__main__`` blocks explicitly catch ``_HookTimeout``; in production the
    new ``HookDeadline`` watchdog calls ``os._exit(0)`` directly (uncatchable),
    but ``_HookTimeout`` is caught here for defense-in-depth and for tests
    that inject it.
    """
    try:
        fn(*args, **kwargs)
    except (Exception, SystemExit):
        try:
            sys.stderr.write(f"[Token Optimizer] {name} failed, continuing\n")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    except BaseException:
        # _HookTimeout (a BaseException subclass in measure.py) and any other
        # BaseException that is not Exception/SystemExit. Swallow it so the
        # remaining subcommands still run; the shared HookDeadline's
        # os._exit(0) is the only uncatchable kill switch.
        try:
            sys.stderr.write(f"[Token Optimizer] {name} hit a base exception, continuing\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Shared deadline: ONE HookDeadline for the whole runner replaces the three
# independent per-entry timeouts (12 + 8 + 5 = 25s declared for Stop).
#
# WHY 13s for Stop:
#   * The three legacy entries declared 25s combined. The consolidated
#     hooks.json entry declares timeout=15 (well under the 25s original, and
#     under any plausible host ceiling).
#   * The internal deadline is 13s: 2s of margin under the declared 15 so the
#     runner self-exits 0 with its buffered stdout emitted, rather than being
#     SIGKILLed by the host mid-write. Same 2s margin the SessionStart and
#     UserPromptSubmit runners use.
#
# WHY 58s for SessionEnd:
#   * The legacy SessionEnd entry declared timeout=60 with async=true. The
#     consolidated entry preserves both. The internal deadline is 58s (2s
#     margin under 60). The work is trivial (spawn a detached worker and
#     return), so the deadline is a safety net that never fires in practice.
#   * The 58s applies to Claude Code only. The Codex
#     marketplace plugin clamps SessionEnd to timeout=3 (via
#     sync-codex-marketplace-plugin.sh CODEX_SESSION_END_TIMEOUT_CAP=3), so on
#     Codex the host kills the process at 3s and the 58s deadline never fires.
#     The work (spawn detached, return) completes well within 3s, so this is
#     not a functional issue, only a headroom claim that is wrong for one host.
# --------------------------------------------------------------------------- #

_RUNNER_DEADLINE = None  # type: measure.HookDeadline | None
_RUNNER_TOTAL_BUDGET = 13.0  # seconds, 2s margin under hooks.json timeout=15 (Stop)
_SUBCOMMANDS_PENDING = 0  # decremented by _runner_budget on each call


def _install_runner_deadline(total_seconds=None):
    """Arm ONE shared HookDeadline watchdog for the entire runner."""
    global _RUNNER_DEADLINE
    if _RUNNER_DEADLINE is not None:
        return _RUNNER_DEADLINE
    if total_seconds is None:
        total_seconds = _RUNNER_TOTAL_BUDGET
    _RUNNER_DEADLINE = measure.HookDeadline(total_seconds)
    _RUNNER_DEADLINE.start()
    return _RUNNER_DEADLINE


def _runner_budget(default_seconds, subcommand_count_hint=None):
    """Return the fair-share budget (seconds) for one subcommand.

    Divides the shared deadline's remaining time among the subcommands that
    have not yet run. Callers check the returned value: if it is below a
    minimum threshold they skip the subcommand entirely so a later subcommand
    with real work still gets a chance.
    """
    global _SUBCOMMANDS_PENDING
    if subcommand_count_hint is not None:
        _SUBCOMMANDS_PENDING = subcommand_count_hint
    _SUBCOMMANDS_PENDING = max(0, _SUBCOMMANDS_PENDING - 1)
    if _RUNNER_DEADLINE is None:
        return default_seconds
    remaining = _RUNNER_DEADLINE.remaining()
    if remaining <= 0:
        return 0.0
    divisor = max(1, _SUBCOMMANDS_PENDING + 1)
    fair = remaining / divisor
    return min(default_seconds, max(0.1, fair))


def _clear_runner_deadline():
    """Cancel the shared deadline (normal completion)."""
    global _RUNNER_DEADLINE
    if _RUNNER_DEADLINE is not None:
        _RUNNER_DEADLINE.cancel()
        _RUNNER_DEADLINE = None


# --------------------------------------------------------------------------- #
# Subcommand handlers -- each mirrors its measure.py __main__ dispatch block.
# --------------------------------------------------------------------------- #


def _sub_compact_capture_stop(hook_input: dict) -> None:
    """``measure.py compact-capture --trigger stop --quiet`` (Stop entry 1).

    Mirrors the ``compact-capture`` dispatch (measure.py ~L42447):
      --trigger stop -> trigger="stop";
      reads stdin for transcript_path and session_id;
      calls compact_capture(transcript_path=..., session_id=..., trigger="stop");
      --quiet suppresses the print.
    The dispatch's own ``_install_hook_budget(8)`` is replaced by the runner's
    shared HookDeadline; the ``_HookTimeout`` / ``Exception`` catches are
    replaced by ``_run_safely``.
    """
    budget = _runner_budget(8)
    if budget < 0.1:
        return
    transcript = hook_input.get("transcript_path")
    sid = hook_input.get("session_id")
    result = measure.compact_capture(
        transcript_path=transcript, session_id=sid, trigger="stop"
    )
    # --quiet: no print. The dispatch only prints when result is truthy AND
    # --quiet is NOT in args; the Stop path always has --quiet, so no print.
    # Non-quiet output (if ever needed) would go through the stdout buffer.


def _sub_session_end_flush_stop(hook_input: dict) -> None:
    """``measure.py session-end-flush --trigger stop --quiet --defer`` (entry 2).

    Mirrors the ``session-end-flush`` dispatch (measure.py ~L42154):
      calls _dispatch_session_end_flush(args) with the exact args list;
      _dispatch_session_end_flush defers by default (--no-defer not present),
      spawning a detached worker with args[1:] = [--trigger, stop, --quiet,
      --defer]; the worker reads --trigger for its compact_capture call.
    The dispatch's trailing ``sys.exit(0)`` is NOT part of
    _dispatch_session_end_flush; calling the function directly avoids it. Even
    if it did sys.exit, ``_run_safely`` catches SystemExit.
    """
    budget = _runner_budget(8)
    if budget < 0.1:
        return
    args = ["session-end-flush", "--trigger", "stop", "--quiet", "--defer"]
    # The deferred worker has no stdin, so hand it the session identity the
    # host gave THIS hook; without it the worker can only guess and (on Codex)
    # wrote an "unknown" placeholder checkpoint (issue #278).
    for flag, key in (("--session-id", "session_id"), ("--transcript-path", "transcript_path")):
        value = hook_input.get(key)
        if isinstance(value, str) and value and len(value) <= 4096 and "\x00" not in value:
            args += [flag, value]
    measure._dispatch_session_end_flush(args, budget_seconds=budget)


def _sub_keepwarm_arm(hook_input: dict) -> None:
    """``measure.py keepwarm-arm --quiet`` (Stop entry 3).

    Mirrors the ``keepwarm-arm`` dispatch (measure.py ~L42479):
      reads stdin for session_id and transcript_path;
      calls write_keepwarm_arm_record(sid, transcript);
      --quiet suppresses the print.
    The dispatch's own ``_install_hook_budget(2)`` is replaced by the runner's
    shared HookDeadline. The dispatch's ``sys.exit(0)`` on _HookTimeout /
    Exception is replaced by ``_run_safely``'s SystemExit catch.
    """
    budget = _runner_budget(2)
    if budget < 0.1:
        return
    sid = hook_input.get("session_id")
    transcript = hook_input.get("transcript_path")
    measure.write_keepwarm_arm_record(sid, transcript)


def _sub_session_end_flush_end(hook_input: dict) -> None:
    """``measure.py session-end-flush --trigger end --defer`` (SessionEnd entry).

    Same function as the Stop path's session-end-flush but with trigger=end and
    no --quiet (the legacy SessionEnd entry did not pass --quiet). The trigger
    value ``end`` is preserved exactly for the latency budget table that
    matches on it. ``--defer`` is preserved (default defer, but the flag is
    passed through to the detached worker's args).
    """
    budget = _runner_budget(60)
    if budget < 0.1:
        return
    measure._dispatch_session_end_flush(
        ["session-end-flush", "--trigger", "end", "--defer"],
        budget_seconds=budget,
    )


def main() -> int:
    # Wrap the entire body in a top-level guard so "exit 0 always" is truly
    # always: an unhandled exception in main() (e.g. from _install_runner_deadline
    # or _is_session_end) would otherwise propagate and yield a non-zero exit.
    # The only non-zero path is the uncatchable os._exit(0) from the HookDeadline
    # watchdog (which exits 0 anyway). Per-subcommand failures are already
    # isolated by _run_safely; this guard covers the orchestration layer.
    try:
        hook_input = _read_hook_input()
        is_session_end = _is_session_end(hook_input)
        _record_payload_shape(hook_input)

        # ONE shared HookDeadline for the entire runner. The budget differs by
        # event: 13s for Stop (under the 15s declared timeout), 58s for SessionEnd
        # (under the 60s declared timeout). The SessionEnd path runs one trivial
        # subcommand (spawn detached, return), so the 58s deadline is a safety net.
        if is_session_end:
            _install_runner_deadline(58.0)
        else:
            _install_runner_deadline()

        # Buffer every subcommand's stdout and emit in dispatch order at the end.
        # Under --quiet (the Stop path) all three subcommands produce no stdout;
        # the SessionEnd path likewise produces none. The buffer is a safety net
        # for any unexpected output, preserving the host's no-output contract.
        _stdout_bufs: list[str] = []

        def _capture(name: str, fn, *args, **kwargs) -> None:
            buf = io.StringIO()
            with redirect_stdout(buf):
                _run_safely(name, fn, *args, **kwargs)
            captured = buf.getvalue()
            if captured:
                _stdout_bufs.append(captured)

        if is_session_end:
            # SessionEnd: only session-end-flush --trigger end --defer.
            _runner_budget(60, subcommand_count_hint=1)
            _capture("session-end-flush --trigger end", _sub_session_end_flush_end,
                     hook_input)
        else:
            # Stop: all three subcommands in dispatch order (matching the legacy
            # hooks.json entry order: compact-capture, session-end-flush, keepwarm-arm).
            _runner_budget(8, subcommand_count_hint=3)
            _capture("compact-capture", _sub_compact_capture_stop, hook_input)
            _capture("session-end-flush --trigger stop", _sub_session_end_flush_stop,
                     hook_input)
            _capture("keepwarm-arm", _sub_keepwarm_arm, hook_input)

        # Emit all buffered stdout in order (preserves per-shape contract: each
        # subcommand's output is a self-contained unit, emitted in the order the
        # host expects from the consolidated dispatcher).
        for buf in _stdout_bufs:
            sys.stdout.write(buf)
    except BaseException:
        # Catch BaseException (not just Exception) so _HookTimeout and any
        # other BaseException subclass from measure.py is swallowed. The
        # HookDeadline's os._exit(0) is uncatchable and bypasses this guard,
        # which is the correct behavior (it exits 0).
        try:
            sys.stderr.write("[Token Optimizer] stop_runner top-level error, exiting 0\n")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    finally:
        _clear_runner_deadline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
