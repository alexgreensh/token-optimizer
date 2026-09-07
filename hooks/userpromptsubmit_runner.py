#!/usr/bin/env python3
"""Single-import UserPromptSubmit dispatcher.

Replaces the six separate ``UserPromptSubmit`` hooks.json entries that each
spawned ``python-launcher.sh -> run.py -> module_runner.py -> runpy(measure.py)``
(eighteen processes per prompt, with the 1.88 MB ``measure.py`` imported six
times). This runner is invoked ONCE per prompt, imports ``measure.py`` ONCE,
and runs all six subcommands in-process with per-subcommand failure isolation.

Key properties:
  - The three always-on subcommands (``quality-cache --warn --quiet``,
    ``prompt-continuity --quiet``, ``verbosity-steer --quiet``) run every prompt.
  - The three harness-only subcommands (``ensure-health --once-per-session``,
    ``quality-cache --force --quiet --once-per-session``,
    ``compact-restore --new-session-only --once-per-session``) run only when the
    harness guard passes, and each is latched by the SAME per-session marker
    ``measure._ran_once_this_session``.
  - ONE shared ``HookDeadline`` (18s, 2s margin under hooks.json timeout=20)
    replaces the six independent 8s per-subcommand deadlines.  Remaining time is
    budgeted fairly across subcommands; the shared deadline's ``os._exit(0)`` is
    the ONLY kill switch in the process, so an early subcommand hang can never
    preemptively kill later ones (including the ensure-health bootstrap).
  - stdout from all six subcommands is captured through one buffered emitter and
    emitted at the end of ``main()`` in a controlled, host-consumable way,
    preserving the per-shape contract (hookSpecificOutput JSON, systemMessage
    JSON, raw text). ensure-health and quality-cache ``{"systemMessage": ...}``
    JSON lines are user-facing (shown to the USER's terminal, NOT injected into
    the model's context) and also feed the envelope; their plain-text
    diagnostics go to a log file. All subcommand stderr is also routed to the
    log file -- the host captures both stdout and stderr into the model's
    session context, so diagnostics must reach neither stream.
  - One subcommand throwing/aborting never aborts the others (each is wrapped in
    ``_run_safely``); the hook always exits 0. Failure notices and tracebacks
    go to the diagnostics log file, not stderr.
  - ensure-health's run-once marker is unlinked on failure so a single
    transient failure never permanently deadlocks the consent gate for the
    session.
  - ``run._check_consent`` is imported by explicit path so a future
    ``skills/.../run.py`` on ``sys.path`` cannot shadow the real gate.

The runner calls the same module-level entrypoints used by the ``__main__``
dispatch, preserving their arguments and behavior while only changing how
they are scheduled.

Run: ``hooks/userpromptsubmit_runner.py`` (via run.py -> module_runner.py).
"""
from __future__ import annotations

import importlib.util as _importlib_util
import io
import json
import os
import sys
import time as _time
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
# Diagnostics log: the host captures BOTH stdout and stderr from
# UserPromptSubmit hooks into the model's session context, where they are
# re-billed on every turn. The only correct destination for hook diagnostics
# is therefore a log file, never stdout and never stderr. User-facing
# ``{"systemMessage": ...}`` JSON lines are tax-free (shown to the USER's
# terminal, not injected into the model's context) and must keep reaching the
# host on stdout.
# --------------------------------------------------------------------------- #

_DIAGNOSTICS_LOG_NAME = "userpromptsubmit_diagnostics.log"
_DIAGNOSTICS_LOG_CAP = 256 * 1024  # keep the last ~256 KB


def _diagnostics_log_path():
    """Resolve the diagnostics log path under measure's snapshot/state dir.

    Reuses the same state directory measure.py uses for its other log files.
    Returns None if no state dir is resolvable, in which case callers silently
    drop diagnostics.
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

    Any error is swallowed so logging can never block or break the hook.
    If no writable log dir can be resolved, silently drop -- never fall back
    to real stderr.
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
        try:
            if path.stat().st_size > _DIAGNOSTICS_LOG_CAP:
                data = path.read_bytes()[-_DIAGNOSTICS_LOG_CAP:]
                path.write_bytes(data)
        except OSError:
            pass
    except Exception:
        pass


def _emit_stdout_envelope(bufs: list[str]) -> None:
    """Collapse all subcommand stdout into ONE host-valid JSON envelope.

    STDOUT is feature output: ``systemMessage`` JSON lines are user-facing
    (tax-free), bare plain-text nudges and star pitches are model-facing
    (``additionalContext``), and ``hookSpecificOutput`` JSON from
    prompt-continuity / verbosity-steer carries ``additionalContext`` too.
    ``measure._collapse_hook_stdout`` folds all of these into one object so
    the host sees a single parseable document. STDERR is diagnostic and goes
    to the log (handled per-subcommand in ``_capture``).
    """
    combined = "\n".join(part for part in bufs if part and part.strip())
    if not combined.strip():
        return
    _run_safely(
        "user-prompt-submit stdout envelope",
        measure._emit_hook_stdout_envelope,
        combined,
        "UserPromptSubmit",
    )


def _read_hook_input() -> dict:
    """Read the hook stdin JSON once, non-blocking, shared across subcommands.

    Uses measure's own shared reader (Windows pipe-peek + Unix select) so the
    behavior matches what each ``__main__`` handler saw individually. 1 MB cap
    is the largest any of the six handlers reads (the quality-cache handler).
    """
    try:
        return measure._read_stdin_hook_input(max_bytes=1_000_000) or {}
    except Exception:
        return {}


def _harness_only_context() -> bool:
    """Replicate the shell harness guard that gated hooks.json entries 4/5/6.

    Original shell::

        [ -n "$CLAUDE_CODE_CONTAINER_ID$CLAUDE_CODE_REMOTE" ] ||
        case "$AI_AGENT$CLAUDE_PLUGIN_ROOT" in *harness*|*/plugins/synced/*) ;; *) exit 0;; esac

    i.e. run the harness-only subcommands when a container/remote env is set
    or the plugin root is under ``/plugins/synced/``. ``AI_AGENT`` is excluded:
    native Claude Code uses a ``..._harness`` suffix too.
    """
    container_id = os.environ.get("CLAUDE_CODE_CONTAINER_ID", "").strip()
    remote = os.environ.get("CLAUDE_CODE_REMOTE", "").strip()
    if container_id or remote:
        return True
    return "/plugins/synced/" in os.environ.get("CLAUDE_PLUGIN_ROOT", "")


def _quality_cache_is_missing(hook_input: dict) -> bool:
    """True when this session has NO quality cache yet (one stat, no parsing).

    Deliberately cheap and deliberately narrow: it answers "does the file
    exist", nothing more. Any error resolving the path returns False so the
    recovery branch stays opt-in and a broken resolution can never turn every
    UserPromptSubmit into a full transcript parse.
    """
    try:
        path = measure._quality_cache_path_for(hook_input.get("transcript_path"))
        return path is not None and not Path(path).exists()
    except Exception:
        return False


def _run_safely(name: str, fn, *args, **kwargs) -> None:
    """Run fn, swallow any failure to the diagnostics log, never propagate.

    Catches ``Exception`` and ``SystemExit`` so one subcommand's bug or internal
    ``sys.exit()`` cannot abort the others. ``_HookTimeout`` (a ``BaseException``
    raised only by tests that inject it) is caught inside each subcommand
    function; in production the ``HookDeadline`` watchdog calls ``os._exit(0)``
    directly, which is uncatchable and correctly terminates the whole hook 0.

    Failure notices and tracebacks go to the diagnostics log file, not stderr
    (the host captures stderr into the model's session context).
    """
    try:
        fn(*args, **kwargs)
    except (Exception, SystemExit):
        try:
            buf = io.StringIO()
            buf.write(f"[Token Optimizer] {name} failed, continuing\n")
            traceback.print_exc(file=buf)
            _write_diagnostics(buf.getvalue())
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Shared deadline: ONE HookDeadline for the whole runner
# replaces the six independent 8s per-subcommand deadlines.  The shared
# deadline fires os._exit(0) only when the TOTAL time runs out, and the
# remaining time is budgeted across subcommands so an early subcommand cannot
# consume a later one's entire allotment.  Total budget = 18s (2s margin under
# the hooks.json 20s HOT_PATH_CEILING).
# --------------------------------------------------------------------------- #

_RUNNER_DEADLINE = None  # type: measure.HookDeadline | None
_RUNNER_TOTAL_BUDGET = 18.0  # seconds, with 2s margin under hooks.json timeout=20
_SUBCOMMANDS_PENDING = 0  # decremented by _runner_budget on each call


def _install_runner_deadline(total_seconds=_RUNNER_TOTAL_BUDGET):
    """Arm ONE shared HookDeadline watchdog for the entire runner."""
    global _RUNNER_DEADLINE
    if _RUNNER_DEADLINE is not None:
        return _RUNNER_DEADLINE
    _RUNNER_DEADLINE = measure.HookDeadline(total_seconds)
    _RUNNER_DEADLINE.start()
    return _RUNNER_DEADLINE


def _runner_budget(default_seconds, subcommand_count_hint=None):
    """Return the fair-share budget (seconds) for one subcommand.

    Divides the shared deadline's remaining time among the subcommands that
    have not yet run.  Callers check the returned value: if it is below a
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
    # Fair share: divide remaining time among the subcommands that still need
    # to run (the one we are about to run + the ones still pending).
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
# Subcommand handlers — each mirrors its measure.py __main__ dispatch block.
# --------------------------------------------------------------------------- #


def _sub_quality_cache_warn(hook_input: dict) -> None:
    """``quality-cache --warn --quiet`` (always runs). Mirrors __main__ L40696."""
    budget = _runner_budget(8)
    if budget < 0.1:
        _write_diagnostics("[Token Optimizer] insufficient time budget; skipping quality-cache --warn\n")
        return
    quiet = True
    warn = True
    force = False
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
    measure.quality_cache(
        throttle_seconds=throttle,
        warn_threshold=warn_threshold,
        quiet=quiet,
        session_jsonl=session_jsonl,
        force=force,
        pure_time_throttle=throttle_only,
        session_id=session_id,
        warn=warn,
    )


def _sub_prompt_continuity(hook_input: dict) -> None:
    """``prompt-continuity --quiet`` (always runs). Mirrors __main__ L40602."""
    budget = _runner_budget(8)
    if budget < 0.1:
        return
    prompt_text = (
        hook_input.get("prompt")
        or hook_input.get("current_prompt")
        or hook_input.get("user_prompt")
        or ""
    )
    sid = hook_input.get("session_id")
    cwd = hook_input.get("cwd")
    transcript_path = hook_input.get("transcript_path")
    if not cwd and transcript_path:
        try:
            cwd = str(Path(transcript_path).parent)
        except TypeError:
            cwd = None
    hint = ""
    try:
        hint = measure._continuity_prompt_hint(
            prompt_text=prompt_text, session_id=sid, cwd=cwd
        )
    except Exception:
        hint = ""
    hint = (hint or "").strip()
    if hint:
        print(
            json.dumps(
                {
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": hint,
                    },
                }
            )
        )


def _sub_verbosity_steer(hook_input: dict) -> None:
    """``verbosity-steer --quiet`` (always runs). Mirrors __main__ L40642.

    Note: the __main__ dispatch hardcodes ``quiet=False`` (the ``--quiet`` CLI
    flag is not parsed for this subcommand). Match the REAL call shape, not the
    flag name.
    """
    budget = _runner_budget(8)
    if budget < 0.1:
        return
    transcript_path = hook_input.get("transcript_path")
    session_id = hook_input.get("session_id")
    try:
        payload = measure.run_verbosity_steer(
            transcript_path=transcript_path,
            quiet=False,
            session_id=session_id,
        )
        if payload:
            print(payload)
    except Exception:
        pass


def _sub_ensure_health(hook_input: dict) -> None:
    """``ensure-health --once-per-session`` (harness-gated). Mirrors __main__ L41138.

    The run-once marker is set BEFORE the work by
    ``_ran_once_this_session``.  If the first call throws (caught by
    ``_run_safely``), the marker is already on disk but the consent flags
    were never written, so ensure-health no-ops for the rest of the session
    and consent stays False (all six subcommands dead).  To prevent this
    re-deadlock, we unlink the marker on any failure so the next prompt
    retries the bootstrap.
    """
    sid = hook_input.get("session_id")
    if measure._ran_once_this_session("ensure-health", sid):
        return
    budget = _runner_budget(8)
    if budget < 0.1:
        # Unlink the marker we just set so the next prompt can retry.
        marker = measure._once_per_session_marker("ensure-health", sid)
        if marker is not None:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
        _write_diagnostics(
            "[Token Optimizer] CRITICAL: insufficient time budget for "
            "ensure-health bootstrap; will retry next prompt\n"
        )
        return
    # The daemon ensure/revive runs FIRST, under its own short guard, BEFORE
    # any health budget is consumed (mirrors __main__ L41167).
    measure._ensure_health_daemon_revive_first()
    try:
        measure.run_ensure_health()
    except Exception:
        # Unlink the marker on failure so the next prompt retries the
        # bootstrap.  Without this, a single transient failure (disk full,
        # permission error, settings.json temporarily locked) permanently
        # deadlocks the consent gate for the entire session.
        marker = measure._once_per_session_marker("ensure-health", sid)
        if marker is not None:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
        _write_diagnostics(
            "[Token Optimizer] CRITICAL: ensure-health bootstrap failed; "
            "will retry next prompt.  Consent flags may not be written.\n"
        )
        raise


def _sub_quality_cache_force(hook_input: dict) -> None:
    """``quality-cache --force --quiet --once-per-session`` (harness-gated).

    Mirrors __main__ L40696 with --force --quiet --once-per-session: the daemon
    pulse + self-heal run unconditionally (as in the dispatch), THEN the
    once-per-session gate, THEN quality_cache() with force=True.

    The gate is CHECK-ONLY, never CHECK+SET.
    ``_ran_once_this_session`` atomically creates the marker BEFORE the work,
    so a hard ``os._exit(0)`` timeout during ``quality_cache()`` leaves it on
    disk and latches the recovery dead for the whole session. The unlink-on-
    failure for ``score is None`` only helps for normal failures, not hard
    kills. Instead, check the marker's existence without creating it, run the
    work, and only write the marker after success. A double-run from a TOCTOU
    race is harmless (quality_cache with force=True is idempotent), and far
    better than a permanently dead recovery.
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
    # CHECK-ONLY: do not create the marker yet. A hard os._exit(0) during
    # quality_cache() would leave a pre-written marker on disk and latch the
    # recovery dead for the whole session.
    marker = measure._once_per_session_marker("quality-cache-force", session_id)
    if marker is not None and marker.exists():
        return
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
    # Write the marker only after the cache work produced a result. A timeout,
    # missing transcript, busy lease, failed write, or hard os._exit(0) leaves
    # no marker so the next prompt can retry.
    if score is not None:
        measure._mark_ran_this_session("quality-cache-force", session_id)


def _sub_compact_restore(hook_input: dict) -> None:
    """``compact-restore --new-session-only --once-per-session`` (harness-gated).

    Mirrors __main__ L40526: the --new-session-only --once-per-session copy
    checks-then-skips the marker, then runs compact_restore(new_session_only).
    Under Codex/Cowork the raw stdout is captured and wrapped in the documented
    additionalContext envelope (docs-grounding.md §1).
    """
    sid = hook_input.get("session_id")
    if measure._ran_once_this_session("compact-restore-new-session", sid):
        return
    budget = _runner_budget(8)
    if budget < 0.1:
        return
    _cw = measure.is_cowork()
    if measure.detect_runtime() == "codex" or _cw:
        buf = io.StringIO()
        with redirect_stdout(buf):
            measure.compact_restore(session_id=sid, new_session_only=True)
        measure._emit_additional_context(
            buf.getvalue(), event=hook_input.get("hook_event_name", "UserPromptSubmit")
        )
    else:
        measure.compact_restore(session_id=sid, new_session_only=True)


def _quality_cache_self_heal() -> None:
    """Replicate the quality-cache dispatch's self-healing block (__main__
    L40743-40755): if the quality-cache hook is missing from settings.json and
    this is NOT a plugin install and quality_bar_disabled is unset, reinstall it.

    For plugin installs (the hooks.json context this runner runs in) the
    ``_is_plugin`` check is True and the block is a no-op, exactly as in the
    dispatch. Replicated verbatim so non-plugin manual installs keep the same
    self-heal behavior. Fail-open: never raises.
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
                # Use _quality_cache_hook_present
                # (the fix in sessionstart_runner.py:448) instead of the
                # naive substring check. The naive check cannot see the
                # consolidated UPS runner dispatcher as a quality-cache
                # provider, so on a script (non-plugin) install it re-appends
                # a duplicate legacy hook -- exactly the bug it was meant to prevent.
                if not measure._quality_cache_hook_present(_sh_hooks):
                    measure.setup_quality_bar(quiet=True)
        except Exception:
            pass
    except Exception:
        pass


def _check_consent() -> bool:
    """Consent gate for the consolidated runner, mirroring ``run._check_consent``.

    run.py exempts this script from its own consent gate:
    the runner is dispatched with no distinguishing args, so the
    ``ensure-health`` exempt-command match never fires there; the runner
    contains the ensure-health bootstrap itself). The per-subcommand consent
    decision therefore lives HERE.

    The consolidated runner requires importing ``run._check_consent`` by explicit path via
    ``importlib.util.spec_from_file_location`` so a future ``skills/.../run.py``
    on ``sys.path`` cannot shadow the real ``hooks/run.py`` and silently
    disable the consent gate (``import run`` fails-open on any AttributeError).
    """
    try:
        run_py = Path(__file__).resolve().parent / "run.py"
        if not run_py.is_file():
            return True
        spec = _importlib_util.spec_from_file_location("_to_run_consent", run_py)
        _run_mod = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(_run_mod)
        return _run_mod._check_consent()
    except Exception:
        return True


def main() -> int:
    hook_input = _read_hook_input()

    # Consent gate for the consolidated runner. Before consolidation, the six
    # UserPromptSubmit hooks.json entries each passed distinguishing args, so
    # the ensure-health entry was consent-exempt (it bootstraps the
    # v5_welcome_shown / enterprise_consent_shown flags) and the other five
    # were consent-gated (returned 0 when consent was False). The consolidated
    # runner is dispatched with no args, so run.py exempts the whole runner
    # path and delegates the per-subcommand decision here.
    #
    # When consent is False: ONLY ensure-health runs (it writes the consent
    # flags via _show_v5_welcome + the v5_welcome_shown write, the bootstrap),
    # and only when the harness guard passes. On Cowork (the no-SessionStart
    # host where this deadlock is fatal) the harness guard is True, so
    # ensure-health bootstraps. On native Claude Code, SessionStart already
    # bootstraps consent, so consent is True before UserPromptSubmit fires and
    # this branch is not reached. The other five subcommands skip, preserving
    # the original consent-gated semantics exactly. When consent is True: all
    # six run per their existing gates.
    if not _check_consent():
        if _harness_only_context():
            _install_runner_deadline()
            _bufs: list[str] = []

            def _capture_consent(name: str, fn, *args, **kwargs) -> None:
                buf = io.StringIO()
                err_buf = io.StringIO()
                with redirect_stdout(buf), redirect_stderr(err_buf):
                    _run_safely(name, fn, *args, **kwargs)
                captured = buf.getvalue()
                err_captured = err_buf.getvalue()
                # ALL stdout is feature output (systemMessage + bare-text
                # nudges/star-pitch) -> envelope. stderr is diagnostic -> log.
                if captured:
                    _bufs.append(captured)
                if err_captured:
                    _write_diagnostics(f"--- {name} ---\n{err_captured}")

            _capture_consent("ensure-health", _sub_ensure_health, hook_input)
            _emit_stdout_envelope(_bufs)
            _clear_runner_deadline()
        return 0

    # Install ONE shared HookDeadline for the entire
    # runner (18s, 2s margin under hooks.json timeout=20).  The per-subcommand
    # _runner_budget calls divide the remaining time fairly.  The shared
    # deadline is the ONLY os._exit(0) in the process -- no individual
    # subcommand deadline can kill later subcommands.
    _install_runner_deadline()

    # Capture each subcommand's stdout through ONE
    # buffered emitter, then emit at the end in a controlled, host-consumable
    # way.  Pre-consolidation each subcommand was its own hooks.json entry and
    # the host parsed their stdout independently; now all six share one stdout
    # stream, so we buffer to avoid mixed JSON/raw-text corruption.
    #
    # STDOUT is feature output: systemMessage JSON (user-facing, tax-free),
    # bare plain-text nudges/star-pitch (model-facing additionalContext), and
    # hookSpecificOutput JSON from prompt-continuity / verbosity-steer /
    # compact-restore (model-facing additionalContext). All of it collapses
    # into ONE envelope via ``_emit_stdout_envelope`` at the end.
    # STDERR is diagnostic -> log file (the host captures both streams into
    # the model's session context, so diagnostics must reach neither).
    _stdout_bufs: list[str] = []

    def _capture(name: str, fn, *args, **kwargs) -> None:
        buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err_buf):
            _run_safely(name, fn, *args, **kwargs)
        captured = buf.getvalue()
        err_captured = err_buf.getvalue()
        # ALL stdout -> envelope (systemMessage + bare-text + JSON all
        # collapse into one host-valid object). stderr -> log.
        if captured:
            _stdout_bufs.append(captured)
        if err_captured:
            _write_diagnostics(f"--- {name} ---\n{err_captured}")

    # 1-3: always-on subcommands. Ordering: the cheap,
    # user-visible subcommands (prompt-continuity, verbosity-steer) run BEFORE
    # the heavier quality-cache --warn.  The shared deadline's os._exit(0)
    # kills the whole process uncatchably, so a hang in an early subcommand
    # still skips later ones; running the cheap user-visible work first
    # minimizes what a quality-cache hang can suppress.  The harness-only 4-6
    # still run after the gate (gating-order semantics preserved).
    _runner_budget(8, subcommand_count_hint=6)
    _capture("prompt-continuity", _sub_prompt_continuity, hook_input)
    _capture("verbosity-steer", _sub_verbosity_steer, hook_input)
    _capture("quality-cache --warn", _sub_quality_cache_warn, hook_input)

    # 4-6: harness-only subcommands. The shell guard that used to prefix
    # hooks.json entries 4/5/6 is evaluated once here; when it fails, all three
    # are skipped exactly as the shell `exit 0` skipped each entry.
    if _harness_only_context():
        _capture("ensure-health", _sub_ensure_health, hook_input)
        _capture("quality-cache --force", _sub_quality_cache_force, hook_input)
        _capture("compact-restore", _sub_compact_restore, hook_input)
    elif _quality_cache_is_missing(hook_input):
        # RECOVERY. Non-harness sessions reach this ONLY when no cache exists.
        # SessionStart is normally the sole creator (`quality-cache --force
        # --quiet --once-mark`, timeout 20s). When it times out -- a busy
        # machine, a boot storm, or heavy workload, exactly when someone
        # looks at the statusline -- nothing else ever created it, and
        # PostToolUse deliberately refuses to (a latency invariant: it fires on
        # every tool call). The session was stuck on `ContextQ:--` with no path
        # back. Bounded: one stat() in the common case; the full computation
        # only when the file is genuinely absent, after which the normal
        # throttle governs and this branch is never taken again.
        _capture("quality-cache --force (bootstrap)", _sub_quality_cache_force,
                 hook_input)

    # Collapse all subcommand stdout into ONE host-valid JSON envelope.
    # systemMessage -> user (tax-free), bare text + additionalContext -> model.
    _emit_stdout_envelope(_stdout_bufs)

    _clear_runner_deadline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
