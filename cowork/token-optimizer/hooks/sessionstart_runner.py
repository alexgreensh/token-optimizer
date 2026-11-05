#!/usr/bin/env python3
"""Single-import SessionStart dispatcher.

Replaces the FIVE separate ``SessionStart`` hook commands in hooks.json that
each spawned ``python-launcher.sh -> run.py -> module_runner.py ->
runpy(measure.py)`` and repeated the interpreter and module startup work:

  1. ``measure.py ensure-health --once-mark``                       (timeout 15, async)
  2. ``measure.py quality-cache --force --quiet --once-mark``       (timeout 20)
  3. ``measure.py compact-restore --compact``       [matcher compact] (timeout 20)
  4. ``read_cache.py --clear-compacted --quiet``    [source compact|clear] (timeout 10)
  5. ``measure.py compact-restore --new-session-only --once-mark``  (timeout 20)

This runner is invoked ONCE per session start, imports ``measure.py`` ONCE, and
runs all five subcommands in-process under ONE shared deadline. That removes
repeated startup work while keeping the host-specific timeout as a backstop.

This is the same consolidation the ``UserPromptSubmit`` group received in
``hooks/userpromptsubmit_runner.py``; SessionStart never got it. The
structure here deliberately mirrors that file.

Key properties:
  - ONE shared ``HookDeadline`` (18s, 2s margin under the hooks.json timeout of
    20, and 7s under Codex's hard 25s SessionStart ceiling) replaces the five
    independent per-entry timeouts. Remaining time is budgeted fairly across the
    subcommands still pending (``_runner_budget``); the shared deadline's
    ``os._exit(0)`` is the ONLY kill switch in the process, so an early
    subcommand hang can never preemptively kill later ones.
  - ``--once-mark`` latching semantics are preserved EXACTLY: subcommands 1, 2
    and 5 call ``measure._mark_ran_this_session`` (WRITE, never check), so the
    SessionStart work always runs -- including on the second SessionStart of a
    session (resume / post-compaction keep the same session_id) --
    while the ``--once-per-session`` UserPromptSubmit copies stay latched out.
    No unlink-on-failure here: the ``--once-mark`` semantics differ from the
    retryable ``--once-per-session`` flow and must remain distinct.
  - The ``matcher: "compact"`` gate on subcommands 3 and 4 is replicated
    in-process by ``_is_compact_start`` (the SessionStart ``source`` field the
    host matches on), so a non-compact start skips them exactly as the matcher
    did.
  - The consent gate that run.py used to apply per-entry is applied per
    subcommand HERE: ensure-health was consent-EXEMPT (it bootstraps the consent
    flags), the other four were consent-gated. run.py exempts this runner path
    wholesale, same as the UserPromptSubmit runner.
  - stdout from the two compact-restore subcommands is captured and emitted in
    dispatch order at the end of ``main()`` as ONE host-valid JSON envelope
    (raw text, systemMessage JSON, additionalContext JSON). ensure-health and
    quality-cache ``{"systemMessage": ...}`` JSON lines are user-facing (shown
    to the USER's terminal, NOT injected into the model's context) and also
    feed the envelope; their plain-text diagnostics are routed to a log file.
    All subcommand stderr is also routed to the log file -- the host captures
    both stdout and stderr into the model's session context, so diagnostics
    must reach neither stream.
  - One subcommand throwing/aborting never aborts the others (each is wrapped in
    ``_run_safely``); the hook always exits 0. Failure notices and tracebacks
    go to the diagnostics log file, not stderr.
  - ``run._check_consent`` is imported by explicit path so a future
    ``skills/.../run.py`` on ``sys.path`` cannot shadow the real gate.

The runner calls the same module-level entrypoints used by the ``__main__``
dispatch, preserving their arguments and behavior while only changing how
they are scheduled.

Run: ``hooks/sessionstart_runner.py`` (via run.py -> module_runner.py).
"""
from __future__ import annotations

import importlib.util as _importlib_util
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
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


# --------------------------------------------------------------------------- #
# Diagnostics log: the host captures BOTH stdout and stderr from SessionStart
# hooks into the model's session context, where they are re-billed on every
# turn. The only correct destination for hook diagnostics is therefore a log
# file, never stdout and never stderr.
# --------------------------------------------------------------------------- #

_DIAGNOSTICS_LOG_NAME = "sessionstart_diagnostics.log"
_DIAGNOSTICS_LOG_CAP = 256 * 1024  # keep the last ~256 KB


def _diagnostics_log_path():
    """Resolve the diagnostics log path under measure's snapshot/state dir.

    Reuses the same state directory measure.py uses for its other log files
    (e.g. the keep-warm scheduler log). Returns None if no state dir is
    resolvable, in which case callers silently drop diagnostics (devnull).
    """
    try:
        base = getattr(measure, "SNAPSHOT_DIR", None)
        if base is None:
            return None
        return Path(base) / _DIAGNOSTICS_LOG_NAME
    except Exception:
        return None


def _write_diagnostics(text: str) -> None:
    """Append diagnostics text to the capped log file. Fully fail-open.

    Any error is swallowed so logging can never block or break SessionStart.
    If no writable log dir can be resolved, silently drop (devnull) -- never
    fall back to real stderr.
    """
    if not text:
        return
    try:
        path = _diagnostics_log_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        chunk = text if text.endswith("\n") else text + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(chunk)
        # Cap: trim to the last ~256 KB so the file stays small.
        try:
            if path.stat().st_size > _DIAGNOSTICS_LOG_CAP:
                data = path.read_bytes()[-_DIAGNOSTICS_LOG_CAP:]
                path.write_bytes(data)
        except OSError:
            pass
    except Exception:
        pass


def _read_hook_input() -> dict:
    """Read the hook stdin JSON once, non-blocking, shared across subcommands.

    Uses measure's own shared reader (Windows pipe-peek + Unix select) so the
    behavior matches what each ``__main__`` handler saw individually. 1 MB cap
    is the largest any of the five handlers reads (quality-cache and
    read_cache.py --clear-compacted both read 1_000_000).
    """
    try:
        return measure._read_stdin_hook_input(max_bytes=1_000_000) or {}
    except Exception:
        return {}


def _is_compact_start(hook_input: dict) -> bool:
    """Replicate the ``"matcher": "compact"`` gate on hooks.json entries 3 and 4.

    Claude Code / Codex match a SessionStart hook group's ``matcher`` against
    the start ``source`` (``startup`` | ``resume`` | ``clear`` | ``compact``)
    carried in the stdin payload. Entries 3 and 4 only ran when that was
    ``compact``; every other start skipped them. ``is_compact`` is also honored
    because measure's own compact-restore dispatch honors it
    (``bool(hook_input.get("is_compact", False)) or source == "compact"``).
    """
    if bool(hook_input.get("is_compact", False)):
        return True
    source = str(hook_input.get("source") or "").strip().lower()
    return source == "compact"


def _is_context_reset_start(hook_input: dict) -> bool:
    """True when this SessionStart follows a /clear or a compaction.

    Both empty the model's context while the session id lives on, so the
    "already in context" records must be dropped. Only ``compact`` also restores
    state (``_is_compact_start``); ``clear`` is a deliberate fresh start and
    restores nothing.
    """
    if _is_compact_start(hook_input):
        return True
    source = str(hook_input.get("source") or "").strip().lower()
    return source == "clear"


def _run_safely(name: str, fn, *args, **kwargs) -> None:
    """Run fn, swallow any failure to the diagnostics log, never propagate.

    Catches ``Exception`` and ``SystemExit`` so one subcommand's bug or internal
    ``sys.exit()`` cannot abort the others. ``_HookTimeout`` (a ``BaseException``
    raised only by tests that inject it) is caught inside each subcommand
    function; in production the ``HookDeadline`` watchdog calls ``os._exit(0)``
    directly, which is uncatchable and correctly terminates the whole hook 0.

    The failure notice and traceback go to the diagnostics log file, never to
    real stderr (the host captures stderr into the model's session context).
    """
    try:
        fn(*args, **kwargs)
    except (Exception, SystemExit):
        try:
            buf = io.StringIO()
            buf.write(f"[Token Optimizer] {name} failed, continuing\n")
            traceback.print_exc(file=buf)
            _write_diagnostics(buf.getvalue())
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Shared deadline: ONE HookDeadline for the whole runner replaces the five
# independent per-entry timeouts (15 + 20 + 20 + 10 + 20 = 85s declared).
#
# WHY 18s:
#   * Codex enforces a HARD 25s ceiling on SessionStart hooks and kills the
#     process ("hook timed out after 25s") regardless of the declared timeout.
#     That is the smallest host ceiling, so it sets the budget.
#   * The consolidated hooks.json entry declares timeout=20 (Claude honors the
#     declared value; 20 also sits 5s under Codex's ceiling, leaving room for
#     the bash -> python-launcher -> run.py -> module_runner -> measure-import
#     spawn chain, ~1-2s on a cold cache).
#   * The internal deadline is 18s: 2s of margin under the declared 20 so the
#     runner self-exits 0 with its buffered stdout emitted, rather than being
#     SIGKILLed by the host mid-write. Same 2s margin the UserPromptSubmit
#     runner uses under its own timeout=20.
# --------------------------------------------------------------------------- #

_RUNNER_DEADLINE = None  # type: measure.HookDeadline | None
_RUNNER_TOTAL_BUDGET = 18.0  # seconds, 2s margin under hooks.json timeout=20
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
# The SessionStart stdout envelope.
#
# Every subcommand's stdout is buffered and emitted as ONE JSON object in the
# documented {"hookSpecificOutput": {...}} shape
# (docs-grounding.md §1), with the event name taken from the firing hook's stdin
# payload (PR #142) so a SessionStart hook can never emit a UserPromptSubmit
# envelope (Claude Code rejects the mismatch).
#
# This is UNCONDITIONAL -- it is not gated on detect_runtime(). The old gate
# (`detect_runtime() == "codex" or is_cowork()`) was dead code on Codex: Codex
# sets neither CODEX_HOME nor TOKEN_OPTIMIZER_RUNTIME in a hook subprocess (only
# CLAUDE_PLUGIN_ROOT / CLAUDE_PLUGIN_DATA), so detect_runtime() returns "claude"
# inside a Codex plugin hook and the raw "[Token Optimizer] ..." text went out
# unwrapped -- "hook returned invalid session start JSON output". The envelope is
# valid on BOTH hosts, so there is nothing to detect.
#
# It also has to be ONE object, not one per subcommand: pre-consolidation each
# subcommand owned its own stdout stream; now they share one, and two JSON
# documents on a single stream is exactly what Codex refuses to parse. See
# tests/test_codex_sessionstart_json_contract.py for the verified contract.
# --------------------------------------------------------------------------- #


def _envelope_event(hook_input: dict) -> str:
    """Envelope hookEventName, derived from the firing hook's stdin payload."""
    event = hook_input.get("hook_event_name")
    return "SessionStart" if event != "SessionStart" else event


def _emit_session_start_stdout(parts, hook_input: dict) -> None:
    """Emit every subcommand's stdout as AT MOST ONE host-valid JSON object.

    This runner is now the ONLY SessionStart stdout producer, so it owns the
    host's output contract. Codex (verified against 0.150.0-alpha.12.2) parses
    SessionStart stdout like this: whitespace-only is a no-op; stdout that does
    NOT start with ``{`` or ``[`` is ignored as plain text; stdout that DOES
    start with ``{`` or ``[`` must be ONE JSON document that validates against
    its ``session-start.command.output`` schema, or the entire hook fails with
    "hook returned invalid session start JSON output".

    Concatenating the subcommands' raw streams breaks that three ways at once:
    every human-readable Token Optimizer line starts with ``[Token Optimizer]``
    (a ``[``, read as the start of a JSON array); two ``{"systemMessage": ...}``
    objects on one stream are two documents; and a raw line followed by a JSON
    line is neither. ``measure._collapse_hook_stdout`` folds all of it into one
    object -- systemMessages joined, plain text carried in
    ``hookSpecificOutput.additionalContext`` -- which BOTH Claude Code and Codex
    accept, so no runtime sniffing is required (and none is possible: Codex sets
    neither CODEX_HOME nor TOKEN_OPTIMIZER_RUNTIME in the hook subprocess, so
    ``detect_runtime()`` returns "claude" inside a Codex plugin hook).
    """
    combined = "\n".join(part for part in parts if part and part.strip())
    if not combined.strip():
        return
    _run_safely(
        "session-start stdout envelope",
        measure._emit_hook_stdout_envelope,
        combined,
        _envelope_event(hook_input),
    )


# --------------------------------------------------------------------------- #
# Subcommand handlers -- each mirrors its measure.py __main__ dispatch block.
# --------------------------------------------------------------------------- #


def _sub_ensure_health(hook_input: dict) -> None:
    """``measure.py ensure-health --once-mark`` (hooks.json entry 1).

    Mirrors the ``ensure-health`` dispatch (measure.py ~L43205):
      --once-mark -> _mark_ran_this_session (WRITE, never check), so the
      SessionStart copy ALWAYS runs and refreshes the marker that latches the
      UserPromptSubmit --once-per-session copy;
      then _ensure_health_daemon_revive_first() under its own guard before any
      health budget is consumed;
      then run_ensure_health().
    """
    sid = hook_input.get("session_id")
    measure._mark_ran_this_session("ensure-health", sid)
    budget = _runner_budget(8)
    if budget < 0.1:
        sys.stderr.write(
            "[Token Optimizer] insufficient time budget; skipping ensure-health\n"
        )
        return
    measure._ensure_health_daemon_revive_first()
    measure.run_ensure_health()


def _sub_quality_cache_force(hook_input: dict) -> None:
    """``measure.py quality-cache --force --quiet --once-mark`` (entry 2).

    Mirrors the ``quality-cache`` dispatch (measure.py ~L42760) for this exact
    flag set: daemon pulse, then the settings self-heal block, then the
    --once-mark WRITE, then quality_cache(force=True, quiet=True, warn=False).
    """
    budget = _runner_budget(8)
    if budget < 0.1:
        return
    quiet = True
    warn = False
    force = True
    throttle_only = False
    throttle = 120
    warn_threshold = 70
    session_jsonl = hook_input.get("transcript_path")
    session_id = hook_input.get("session_id")
    try:
        measure._daemon_midsession_pulse()
    except Exception:
        pass
    _quality_cache_self_heal()
    score = measure.quality_cache(
        throttle_seconds=throttle,
        warn_threshold=warn_threshold,
        quiet=quiet,
        session_jsonl=session_jsonl,
        force=force,
        pure_time_throttle=throttle_only,
        session_id=session_id,
        warn=warn,
    )
    # Claim the shared once-per-session marker only after the cache work
    # produced a result. A timeout or failed write must leave UserPromptSubmit
    # free to run its recovery path for this session.
    if score is not None:
        measure._mark_ran_this_session("quality-cache-force", session_id)


def _sub_compact_restore_compact(hook_input: dict, sink: list) -> None:
    """``measure.py compact-restore --compact`` (entry 3, matcher ``compact``).

    Mirrors the ``compact-restore`` dispatch (measure.py ~L42582) with
    ``--compact``: no marker (only the --new-session-only pointer is guarded --
    the in-place restore must run on EVERY compaction), is_compact=True, and the
    Codex/Cowork additionalContext wrap. The wrapped text is appended to
    ``sink`` instead of being emitted here, so both compact-restore subcommands
    share ONE envelope (see the envelope note above).
    """
    budget = _runner_budget(measure._int_env("TOKEN_OPTIMIZER_COMPACT_RESTORE_BUDGET", 8))
    if budget < 0.1:
        return
    sid = hook_input.get("session_id")
    buf = io.StringIO()
    with redirect_stdout(buf):
        measure.compact_restore(session_id=sid, is_compact=True)
    sink.append(buf.getvalue())


def _sub_clear_compacted(hook_input: dict) -> None:
    """``read_cache.py --clear-compacted --quiet`` (entry 4, source ``compact`` or ``clear``).

    Mirrors read_cache.main()'s --clear-compacted branch: bail loudly (stderr)
    when the stdin payload is empty, else call handle_clear_compacted(payload,
    quiet=True). read_cache is imported lazily so a plain startup or resume --
    the overwhelming majority -- never pays for the import.

    Emits nothing on stdout by design (all its output, success and failure, goes
    to stderr), so it cannot perturb the SessionStart context stream.
    """
    # The context just emptied, so resume blocks this session already received
    # (prompt-continuity dedups identical ones) are no longer in it. Cheap local
    # unlink; never raises.
    if hook_input:
        measure._clear_resume_dedup_markers(hook_input.get("session_id"))
    budget = _runner_budget(10)
    if budget < 0.1:
        return
    if not hook_input:
        # C5 parity: FAILED branches stay loud even under --quiet.
        sys.stderr.write(
            "[read_cache] --clear-compacted FAILED: no stdin hook input; "
            "live session file_reads left intact (not cleared)\n"
        )
        return
    import read_cache  # noqa: PLC0415  (lazy: only compact starts pay for it)

    read_cache.handle_clear_compacted(hook_input, True)


def _sub_compact_restore_new_session(hook_input: dict, sink: list) -> None:
    """``measure.py compact-restore --new-session-only --once-mark`` (entry 5).

    Mirrors the ``compact-restore`` dispatch (measure.py ~L42582) with
    ``--new-session-only --once-mark``: _mark_ran_this_session (WRITE, never
    check -- a resume/compact SessionStart is NOT latched out), then
    compact_restore(new_session_only=True), with the same Codex/Cowork wrap
    routed into the shared ``sink``.
    """
    sid = hook_input.get("session_id")
    measure._mark_ran_this_session("compact-restore-new-session", sid)
    budget = _runner_budget(measure._int_env("TOKEN_OPTIMIZER_COMPACT_RESTORE_BUDGET", 8))
    if budget < 0.1:
        return
    buf = io.StringIO()
    with redirect_stdout(buf):
        measure.compact_restore(session_id=sid, new_session_only=True)
    sink.append(buf.getvalue())


def _quality_cache_self_heal() -> None:
    """Replicate the quality-cache dispatch's self-healing block: if the
    quality-cache hook is missing from settings.json and this is NOT a plugin
    install and quality_bar_disabled is unset, reinstall it.

    For plugin installs (the hooks.json context this runner runs in) the
    ``_is_plugin`` check is True and the block is a no-op, exactly as in the
    dispatch. Replicated verbatim so non-plugin manual installs keep the same
    self-heal behavior. Uses measure._quality_cache_hook_present
    so an install whose canonical hook is the consolidated dispatcher is
    not "healed" by appending a duplicate legacy hook. Fail-open: never raises.
    """
    try:
        _is_plugin = measure._is_running_from_plugin_cache() or measure._is_plugin_installed()
        try:
            _qb_disabled = False
            if measure.CONFIG_PATH.exists():
                _qb_cfg = json.loads(measure.CONFIG_PATH.read_text(encoding="utf-8"))
                _qb_disabled = _qb_cfg.get("quality_bar_disabled", False)
            if (
                not _is_plugin
                and not _qb_disabled
                and measure.SETTINGS_PATH.exists()
            ):
                _sh_settings = json.loads(measure.SETTINGS_PATH.read_text(encoding="utf-8"))
                _sh_hooks = _sh_settings.get("hooks", {}).get("UserPromptSubmit", [])
                if not measure._quality_cache_hook_present(_sh_hooks):
                    measure.setup_quality_bar(quiet=True)
        except Exception:
            pass
    except Exception:
        pass


def _check_consent() -> bool:
    """Consent gate for the consolidated runner, mirroring ``run._check_consent``.

    run.py exempts this script from its own consent gate (the runner is
    dispatched with no distinguishing args, so the ``ensure-health``
    exempt-command match cannot fire there, and the runner contains the
    ensure-health bootstrap itself). The per-subcommand consent decision
    therefore lives HERE.

    Imports ``run._check_consent`` by explicit path via
    ``importlib.util.spec_from_file_location`` so a future ``skills/.../run.py``
    on ``sys.path`` cannot shadow the real ``hooks/run.py`` and silently
    disable the consent gate. Fails open on any error.
    """
    try:
        run_py = Path(__file__).resolve().parent / "run.py"
        if not run_py.is_file():
            return True
        spec = _importlib_util.spec_from_file_location("_to_run_consent_ss", run_py)
        _run_mod = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(_run_mod)
        return _run_mod._check_consent()
    except Exception:
        return True


def main() -> int:
    hook_input = _read_hook_input()
    is_compact = _is_compact_start(hook_input)
    is_context_reset = _is_context_reset_start(hook_input)

    # Consent gate. Pre-consolidation, run.py's exempt_commands check matched
    # the literal "ensure-health" arg on hooks.json entry 1, so THAT entry ran
    # even with consent False (it bootstraps v5_welcome_shown /
    # enterprise_consent_shown), while entries 2-5 carried no exempt arg and
    # run.py returned 0 for them. The consolidated runner is dispatched with no
    # args, so run.py exempts the whole runner path and the per-subcommand
    # decision is made here: consent False -> ONLY ensure-health runs.
    consent = _check_consent()

    # ONE shared HookDeadline for the entire runner (see the sizing note above).
    _install_runner_deadline()

    # Raw compact-restore text destined for ONE shared additionalContext
    # envelope (see the envelope note above). compact-restore produces the
    # restored-state additionalContext, which is legitimately model-context
    # (the feature, not the tax). ensure-health and quality-cache
    # systemMessage lines are user-facing (tax-free) and also feed the
    # envelope; their bare plain-text star pitch / nudges are model-facing
    # additionalContext and feed the envelope too. All stderr is diagnostic
    # and goes to the log file.
    _wrapped: list[str] = []

    def _capture(name: str, fn, *args, **kwargs) -> None:
        # Capture BOTH stdout and stderr from each subcommand. The host
        # captures both streams into the model's session context, so
        # diagnostics must go to the log file, never to either real stream.
        buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err_buf):
            _run_safely(name, fn, *args, **kwargs)
        captured = buf.getvalue()
        err_captured = err_buf.getvalue()
        # ALL stdout -> envelope (systemMessage + bare-text + JSON all
        # collapse into one host-valid object via _emit_session_start_stdout).
        # stderr -> log.
        if captured:
            _wrapped.append(captured)
        if err_captured:
            _write_diagnostics(f"--- {name} ---\n{err_captured}")

    if not consent:
        # Bootstrap only. ensure-health writes the consent flags; every other
        # subcommand stayed dark behind run.py's gate and stays dark here.
        _runner_budget(8, subcommand_count_hint=1)
        _capture("ensure-health", _sub_ensure_health, hook_input)
        _emit_session_start_stdout(_wrapped, hook_input)
        _clear_runner_deadline()
        return 0

    # Fair-share the 18s across the subcommands that will actually run. The two
    # matcher:"compact" subcommands only count on a compact start, so a normal
    # start gives its three subcommands a third of the budget each instead of a
    # fifth. Dispatch order matches the hooks.json entry order exactly.
    pending = 3 + (1 if is_context_reset else 0) + (1 if is_compact else 0)
    _runner_budget(8, subcommand_count_hint=pending)

    _capture("ensure-health", _sub_ensure_health, hook_input)
    _capture("quality-cache --force", _sub_quality_cache_force, hook_input)
    if is_compact:
        _capture("compact-restore --compact", _sub_compact_restore_compact,
                 hook_input, _wrapped)
    if is_context_reset:
        _capture("read_cache --clear-compacted", _sub_clear_compacted, hook_input)
    _capture("compact-restore --new-session-only", _sub_compact_restore_new_session,
             hook_input, _wrapped)

    # Emit the compact-restore payloads and ensure-health/quality-cache
    # systemMessage lines as ONE host-valid JSON object. systemMessage values
    # collapse to payload["systemMessage"] (user-facing, no additionalContext,
    # zero model-context tax); compact-restore text collapses to
    # hookSpecificOutput.additionalContext (the restored-state feature).
    # See _emit_session_start_stdout for why a concatenated raw stream is not
    # an option on Codex.
    _emit_session_start_stdout(_wrapped, hook_input)

    _clear_runner_deadline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
