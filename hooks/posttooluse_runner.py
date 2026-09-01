#!/usr/bin/env python3
"""Single-import PostToolUse dispatcher.

Replaces the SIX separate ``PostToolUse`` hooks.json entries that each spawned
``python-launcher.sh -> run.py -> module_runner.py -> runpy(<script>)`` --
eighteen processes per tool call at worst, on the single hottest path in the
product (PostToolUse fires on EVERY tool call):

  1. ``bash_compress_hook.py --quiet``          [Bash]                    (15s, sync)
  2. ``archive_result.py --quiet``              [mcp__.*]                 (15s, async)
  3. ``archive_result.py --quiet``              [Bash|Read|Glob|Grep|Agent] (15s, async)
  4. ``context_intel.py --quiet``               [Bash|Read|Grep|Glob|mcp__.*] (15s, async)
  5. ``read_cache.py --invalidate --quiet``     [Edit|Write|MultiEdit|NotebookEdit] (10s, sync)
  6. ``measure.py quality-cache --quiet --throttle-only`` [the union matcher] (10s, sync)

Combined declared budget: 80s. Measured cost per entry with ``__pycache__``
wiped before every call (the container steady state -- module_runner.py warns
that a read-only scripts dir means bytecode is never cached):

  entry point                          warm    cold
  bash_compress_hook.py --quiet        128ms   122ms
  archive_result.py --quiet            204ms   218ms
  context_intel.py --quiet             204ms   223ms
  read_cache.py --invalidate           205ms   218ms
  measure.py quality-cache -to         226ms   798ms

Of each of those, 127ms is FIXED dispatch overhead (bash launcher -> run.py ->
a second interpreter -> module_runner) paid before any hook work happens, and
the 798ms cold outlier is the one entry that imports measure.py (682ms cold /
99ms warm on its own). Under a sustained container workload, ``PostToolUse:Bash``
was CANCELLED 372 times and succeeded 9.

This runner is invoked ONCE per tool call, pays the dispatch overhead ONCE,
imports each hook module at most once, and runs every subcommand in-process
under ONE shared deadline. It is the same consolidation the ``UserPromptSubmit``
group received in issue #139 (``hooks/userpromptsubmit_runner.py``) and the
``SessionStart`` group received in ``hooks/sessionstart_runner.py``; the
structure here deliberately mirrors those two files.

FOUR THINGS THAT ARE DIFFERENT HERE, and how each is handled:

1. MATCHERS. Unlike the other two groups, these six entries carry FIVE distinct
   tool matchers (and ``archive_result`` is registered TWICE under two of
   them). The consolidated hooks.json entry uses the UNION matcher -- which is
   byte-identical to entry 6's, because entry 6 already matched every tool any
   of the others did -- and each subcommand re-checks its OWN original matcher
   in-process via ``_matches``. See the ``_MATCHER_*`` block for the exact
   equivalence argument, including why ``re.search`` is the right replication
   whichever way the host anchors its own regex.

2. ASYNC. Entries 2, 3 and 4 carried ``"async": true``. A hook group cannot be
   half-async, and entries 1, 5 and 6 CANNOT be async (tests/
   test_async_hook_wiring.py documents why for each), so the consolidated entry
   is SYNCHRONOUS. See the "KNOWN CHANGE" block below for what that costs and
   what it buys.

3. THE HOT PATH. ``measure.py`` is NOT imported at module scope here -- the
   single deviation from the other two runners, and a deliberate one. It is
   imported lazily, only by the quality-cache subcommand, and only after the
   throttle question has been asked. That question is asked through ONE seam
   (``_throttle_tick_due``) so that an import diet which learns to answer it
   without importing measure.py plugs straight in and this consolidation does
   not undo it. See ``_throttle_tick_due``.

4. THE INVARIANT. ``tests/test_hook_runtime_parity.py::
   test_throttle_only_cache_miss_never_parses_transcript`` encodes that a
   throttle-only cache MISS must never parse a transcript. It is enforced
   INSIDE ``measure.quality_cache`` (``if pure_time_throttle and not force and
   not cache_path.exists(): return None``), and this runner calls that function
   with exactly ``pure_time_throttle=True, force=False``, so the invariant is
   preserved by construction, not by re-implementation. There is deliberately
   no bootstrap branch here: the UserPromptSubmit runner owns cache recovery
   precisely because THIS path fires on every tool call.

KNOWN CHANGE -- the three async entries become synchronous:
  * ``context_intel.py`` writes to the session store and emits NOTHING on
    stdout, so nothing it produced is gained or lost. What changes is that the
    turn now waits for it. In-process that is ~96ms of real work (223ms cold
    minus the 127ms dispatch overhead it no longer pays).
  * ``archive_result.py`` is the interesting one. Its ``mcp__.*`` registration
    prints ``{"hookSpecificOutput": {"updatedMCPToolOutput": ...}}`` to replace
    an oversized MCP result with a preview plus an archive pointer -- and as an
    ASYNC hook that stdout was DISCARDED ENTIRELY on Claude Code, so the
    replacement never happened there. It already happens on Codex, whose mirror
    strips every async flag because Codex skips async hooks outright. Making
    the group sync therefore does not invent a behaviour; it makes Claude Code
    match the Codex mirror and the code's own documented intent. Large MCP
    results will now actually be replaced by the pointer on Claude Code. That
    is a real, user-visible change and it is called out here rather than buried.
  * What could NOT be preserved: fire-and-forget. Under the old wiring a stalled
    archive/intel write could not delay the turn at all. Now it can, bounded by
    ``_RUNNER_TOTAL_BUDGET``.

Key properties (shared with the other two runners):
  - ONE shared ``HookDeadline`` replaces six independent host timeouts. Its
    ``os._exit(0)`` is the ONLY kill switch in the process, so an early
    subcommand hang can never preemptively kill later ones. Remaining time is
    budgeted fairly across the subcommands still pending, seeded from
    ``hook_runtime.resolve_entry_budget`` so the evidence-based per-entry
    numbers keep governing each subcommand after consolidation. Each handler
    gets an interruptible per-handler deadline; an overrun is reported on
    stderr and dispatch continues with the next handler.
  - stdin is read ONCE (six processes each got their own copy from the host;
    one process has one stdin) and the SAME payload is handed to every
    subcommand by patching ``hook_io.read_stdin_hook_input`` before the hook
    modules bind it. The cap is the LARGEST any of the six used.
  - stdout is buffered per subcommand and emitted as AT MOST ONE JSON document
    (see ``_emit_post_tool_use_stdout``).
  - One subcommand throwing/aborting never aborts the others (each is wrapped
    in ``_run_safely``, which catches both ``Exception`` and ``SystemExit``);
    the hook always exits 0.

No ``measure.py`` edit, and no edit to any of the four standalone hook scripts:
every call uses the real entrypoint the script's own ``__main__`` calls. The
runner only re-orchestrates them.

Run: ``hooks/posttooluse_runner.py`` (via run.py -> module_runner.py).
"""
from __future__ import annotations

import io
import json
import os
import re
import signal
import sys
import time
import traceback
from contextlib import contextmanager, redirect_stdout
from pathlib import Path


def _resolve_scripts_dir() -> str:
    """Locate ``skills/token-optimizer/scripts`` so the hook modules import.

    module_runner.py puts THIS file's parent (``hooks/``) on ``sys.path[0]``;
    the hook scripts live in ``skills/token-optimizer/scripts/``. Resolve it
    from ``CLAUDE_PLUGIN_ROOT`` (set by the host before hook invocation) with a
    ``__file__``-relative fallback (the plugin root is this file's
    grandparent), and insert it ahead of ``hooks/`` so the scripts and their
    sibling modules (hook_io, hook_runtime, session_store, plugin_env, ...)
    resolve. Same resolver the other two runners use.
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


_SCRIPTS_DIR = _resolve_scripts_dir()
if _SCRIPTS_DIR and _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# --------------------------------------------------------------------------- #
# NOTE: there is NO `import measure` here.
#
# The other two runners import it at module scope, which is right for them:
# UserPromptSubmit fires once per prompt and SessionStart once per session, and
# every one of their subcommands needs measure anyway. PostToolUse fires on
# EVERY tool call and exactly ONE of its six subcommands touches measure.py, so
# a module-scope import would tax every Bash, Read, Grep and Edit in the session
# with 682ms of cold import (99ms warm) to run four subcommands that never use
# it. measure is imported lazily by _measure(), from the quality-cache
# subcommand only. See _throttle_tick_due for the import-diet seam.
# --------------------------------------------------------------------------- #

_MEASURE = None


def _measure():
    """Import measure.py on first use and cache it. Raises on real failure."""
    global _MEASURE
    if _MEASURE is None:
        import measure  # noqa: PLC0415  (lazy by design -- see the note above)

        _MEASURE = measure
    return _MEASURE


# --------------------------------------------------------------------------- #
# Shared stdin.
#
# Pre-consolidation each of the six entries was its own process and the host
# handed each one its own copy of the PostToolUse payload. One process has one
# stdin, readable once, so the runner reads it once and hands the SAME payload
# to every subcommand.
#
# Mechanism: ``hook_io.read_stdin_hook_input`` is the single source every hook
# script reads through (bash_compress_hook imports it INSIDE main(); archive_
# result, context_intel and read_cache bind it at module import). Patching
# hook_io BEFORE importing any of them covers both binding styles; the module
# attribute is re-patched after each import as belt and braces in case a module
# was already in sys.modules.
#
# The cap is the LARGEST of the six (archive_result's), not the smallest. Two
# of the six read only 1 MB, and on a payload above that they used to get {}
# and silently no-op -- read_cache.py --invalidate would skip invalidating the
# cache for a large edit, and quality-cache would fall back to the legacy
# GLOBAL throttle marker instead of the per-session one. Handing them the real
# payload fixes both. That is a behaviour change, and an intended one.
# --------------------------------------------------------------------------- #

# archive_result._STDIN_MAX_BYTES == _ARCHIVE_MAX_SIZE (5 MB) + 262_144, the
# largest of the six. Hardcoded rather than imported so reading stdin never
# requires importing archive_result (sqlite3, session_store, ...) on a tool
# call that will not use it. tests/test_posttooluse_runner.py pins this value
# against the real module constants so it cannot silently drift.
_STDIN_MAX_BYTES = 5_242_880 + 262_144


def _read_hook_input() -> dict:
    """Read the hook stdin JSON once, non-blocking, shared across subcommands."""
    try:
        from hook_io import read_stdin_hook_input

        return read_stdin_hook_input(_STDIN_MAX_BYTES) or {}
    except Exception:
        return {}


def _install_shared_stdin(payload: dict) -> None:
    """Make every subcommand's stdin read return the payload we already read."""

    def _shared(max_bytes=None, *_a, **_kw):
        # A shallow copy per caller: the top-level dict cannot be mutated for
        # the next subcommand. tool_response is a str (immutable) so the 5 MB
        # body is shared by reference, not copied.
        return dict(payload)

    try:
        import hook_io

        hook_io.read_stdin_hook_input = _shared
    except Exception:
        pass


def _rebind_shared_stdin(module) -> None:
    """Re-patch a module that bound read_stdin_hook_input at import time."""
    try:
        if hasattr(module, "read_stdin_hook_input"):
            import hook_io

            module.read_stdin_hook_input = hook_io.read_stdin_hook_input
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Matcher replication.
#
# The host still applies the UNION matcher to the consolidated entry, so the
# runner only ever sees a tool that at least one of the six would have seen.
# Each subcommand then re-checks its own original matcher here.
#
# WHY re.search AND WHY THAT IS EXACT EITHER WAY:
#
# The host's own regex anchoring is not something this file should have to
# guess at, and it does not have to. Take the two possibilities:
#
#   * Host uses an UNANCHORED test (regex.test / re.search). Then re.search
#     here is literally the same operation on the same pattern, and every
#     subcommand fires on exactly the tools its own entry fired on. Exact.
#
#   * Host ANCHORS (fullmatch). Then the union gate admits exactly
#     {Bash, Read, Glob, Grep, Agent, Edit, Write, MultiEdit, NotebookEdit}
#     plus mcp__*, and re.search here can only over-match INSIDE that set.
#     Enumerated, it does not:
#       - _MATCHER_ARCHIVE searches for Bash|Read|Glob|Grep|Agent|mcp__.* --
#         no admitted tool contains one of those as a substring without also
#         matching it under fullmatch (MultiEdit/NotebookEdit/Edit/Write
#         contain none of them).
#       - _MATCHER_CONTEXT_INTEL likewise.
#       - _MATCHER_BASH_COMPRESS ("Bash") and _MATCHER_READ_CACHE_INV are the
#         two where an mcp tool could contain the substring (e.g.
#         "mcp__x__Bash"), and BOTH of those hook functions re-check the tool
#         name themselves -- bash_compress_hook.main() returns unless
#         tool_name == "Bash", read_cache.handle_invalidate returns unless
#         tool_name is in ("Edit","Write","MultiEdit","NotebookEdit") -- so an
#         over-match is a no-op, not a misfire.
#       - _MATCHER_QUALITY_CACHE IS the union, so it is true whenever we run.
#
# archive_result was registered TWICE (matchers "mcp__.*" and
# "Bash|Read|Glob|Grep|Agent") with a byte-identical command. The two matchers
# are disjoint, so it ran at most once per tool call; the alternation of the
# two below is exactly equivalent under search, and it runs once here.
# --------------------------------------------------------------------------- #

_MATCHER_BASH_COMPRESS = "Bash"
_MATCHER_ARCHIVE = "Bash|Read|Glob|Grep|Agent|mcp__.*"
_MATCHER_CONTEXT_INTEL = "Bash|Read|Grep|Glob|mcp__.*"
_MATCHER_READ_CACHE_INVALIDATE = "Edit|Write|MultiEdit|NotebookEdit"
_MATCHER_QUALITY_CACHE = (
    "Bash|Read|Glob|Grep|Agent|Edit|Write|MultiEdit|NotebookEdit|mcp__.*"
)

# Entry 6's matcher IS the union, and the union is what the consolidated
# hooks.json entry declares. So if the host invoked this runner at all, entry 6
# matched -- by definition, under whatever anchoring the host uses. Gating the
# quality-cache subcommand on `None` (always run) rather than re-matching the
# string is therefore both simpler and strictly MORE faithful: a payload that
# arrives with a missing or unexpected tool_name still gets the tick it used to
# get, instead of being silently dropped by a re-derivation of a decision the
# host already made.
_MATCHER_UNION = None


def _matches(pattern, tool_name: str) -> bool:
    """Replicate the host's tool-name matcher gate. Fail CLOSED on a bad regex.

    ``pattern is None`` means "the consolidated entry's own union matcher",
    which the host already evaluated: always True. Otherwise case-sensitive
    ``re.search``, like the host. A pattern that will not compile means this
    file is broken, and the safe answer for a hook that must not fire on the
    wrong tool is "do not fire".
    """
    if pattern is None:
        return True
    if not tool_name:
        return False
    try:
        return bool(re.search(pattern, tool_name))
    except re.error:
        return False


class _HandlerBudgetExceeded(BaseException):
    """Raised by a per-handler alarm so the dispatcher can continue."""

    def __init__(self, seconds: float):
        super().__init__(seconds)
        self.seconds = seconds


@contextmanager
def _handler_deadline(seconds: float):
    """Bound one handler without killing the shared runner.

    POSIX hook processes can interrupt Python sleeps and other Python-level
    waits with SIGALRM. On platforms without interval timers, the completion
    check still makes an overrun visible, while the shared process deadline
    remains the hard backstop.
    """
    if _RUNNER_DEADLINE is None or seconds <= 0:
        yield
        return

    setitimer = getattr(signal, "setitimer", None)
    timer_kind = getattr(signal, "ITIMER_REAL", None)
    alarm_signal = getattr(signal, "SIGALRM", None)
    if setitimer is None or timer_kind is None or alarm_signal is None:
        started = time.monotonic()
        completed = False
        try:
            yield
            completed = True
        finally:
            if completed and time.monotonic() - started >= seconds:
                raise _HandlerBudgetExceeded(seconds)
        return

    try:
        previous_handler = signal.getsignal(alarm_signal)
        previous_timer = signal.getitimer(timer_kind)
    except (AttributeError, OSError, ValueError):
        yield
        return

    started = time.monotonic()

    def _raise_timeout(_signum, _frame):
        raise _HandlerBudgetExceeded(seconds)

    try:
        signal.signal(alarm_signal, _raise_timeout)
        setitimer(timer_kind, seconds)
        yield
    finally:
        elapsed = time.monotonic() - started
        try:
            setitimer(timer_kind, 0)
            signal.signal(alarm_signal, previous_handler)
            previous_remaining, previous_interval = previous_timer
            if previous_remaining > 0:
                outer_remaining = previous_remaining - elapsed
                if outer_remaining > 0:
                    # Outer deadline still has budget: re-arm it for what's left.
                    setitimer(timer_kind, outer_remaining, previous_interval)
                elif callable(previous_handler):
                    # Outer deadline already elapsed while the inner block ran.
                    # Passing max(0.0, ...) -> 0.0 to setitimer would DISARM it,
                    # silently dropping the outer budget. Deliver it now instead
                    # via the just-restored outer handler. (Skipped when the
                    # previous handler is SIG_DFL/SIG_IGN, so we never turn an
                    # expired timer into a process-killing default SIGALRM.)
                    os.kill(os.getpid(), alarm_signal)
        except (OSError, ValueError):
            pass
        if elapsed >= seconds:
            raise _HandlerBudgetExceeded(seconds)


def _run_with_handler_deadline(seconds: float, fn, *args, **kwargs) -> None:
    """Run one handler under its fair-share deadline."""
    with _handler_deadline(seconds):
        fn(*args, **kwargs)


def _report_budget_exhausted(name: str, seconds: float, *, skipped: bool) -> None:
    action = "skipping" if skipped else "continuing"
    detail = "was exhausted before dispatch" if skipped else f"exceeded {seconds:.3f}s"
    try:
        sys.stderr.write(
            f"[Token Optimizer] {name} budget {detail}; {action}\n"
        )
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _run_safely(name: str, fn, *args, **kwargs) -> None:
    """Run fn, swallow any failure to stderr, never propagate.

    Catches handler budget exhaustion, ``Exception`` and ``SystemExit`` so one
    subcommand's overrun, bug or internal ``sys.exit()`` cannot abort the
    others. In production the shared ``HookDeadline`` watchdog remains the
    uncatchable hard process backstop.
    """
    try:
        fn(*args, **kwargs)
    except _HandlerBudgetExceeded as exc:
        _report_budget_exhausted(name, exc.seconds, skipped=False)
    except (Exception, SystemExit):
        try:
            sys.stderr.write(f"[Token Optimizer] {name} failed, continuing\n")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Shared deadline.
#
# ONE HookDeadline for the whole runner replaces the six independent host
# timeouts (15 + 15 + 15 + 15 + 10 + 10 = 80s declared).
#
# WHY 2.5s, when the other two runners use 18s:
#   * They fire once per prompt / once per session. This fires on EVERY tool
#     call, so its ceiling is a latency budget, not a safety net, and
#     hook_runtime's per-entry table sizes this event in HUNDREDS of
#     milliseconds (BUDGET_POSTTOOL 0.75s, BUDGET_POSTTOOL_MEASURE 2.0s), from
#     measured cold cost.
#   * The consolidated worst case is measurably smaller than the six-entry sum:
#     one 127ms dispatch instead of up to five, and one measure import instead
#     of one-plus-four-modules. Cold worst case ~1.1s
#     (127 dispatch + ~91 archive + ~96 intel + ~91 invalidate + ~671
#     quality-cache-with-measure-import), warm ~0.3s. 2.5s is ~2.3x the cold
#     worst case.
#   * module_runner arms hook_runtime's own SILENT backstop for this entry
#     (BUDGET_POSTTOOL_RUNNER, 4.5s). 2.5s leaves 2.0s of margin under it -- the
#     same 2s margin the other two runners keep under their declared timeout --
#     so this deadline always wins the race and the runner self-exits 0 with its
#     buffered stdout emitted, rather than being hard-killed mid-write. The
#     hooks.json declared timeout (10s) is a third-tier backstop above both.
# --------------------------------------------------------------------------- #

_RUNNER_DEADLINE = None
_RUNNER_TOTAL_BUDGET = 2.5  # seconds, 2s margin under hook_runtime's 4.5s backstop
_SUBCOMMANDS_PENDING = 0  # handlers still waiting for their turn
_MIN_HANDLER_BUDGET = 0.1


def _install_runner_deadline(total_seconds=None):
    """Arm ONE shared HookDeadline watchdog for the entire runner."""
    global _RUNNER_DEADLINE
    if _RUNNER_DEADLINE is not None:
        return _RUNNER_DEADLINE
    if total_seconds is None:
        total_seconds = _RUNNER_TOTAL_BUDGET
    try:
        from hook_runtime import HookDeadline

        _RUNNER_DEADLINE = HookDeadline(total_seconds)
        _RUNNER_DEADLINE.start()
    except Exception:
        _RUNNER_DEADLINE = None
    return _RUNNER_DEADLINE


def _runner_budget(default_seconds, subcommand_count_hint=None):
    """Return the fair-share budget (seconds) for one subcommand.

    Divides the shared deadline's remaining time among the subcommands that
    have not yet run. ``subcommand_count_hint`` seeds the pending count and is
    not itself a handler, so the first handler receives a true 1/N share.
    Budgets below the minimum are returned as zero for an explicit, visible
    skip rather than silently handing out time that cannot be used.
    """
    global _SUBCOMMANDS_PENDING
    if subcommand_count_hint is not None:
        _SUBCOMMANDS_PENDING = max(0, int(subcommand_count_hint))
        return 0.0
    _SUBCOMMANDS_PENDING = max(0, _SUBCOMMANDS_PENDING - 1)
    if _RUNNER_DEADLINE is None:
        return default_seconds
    remaining = _RUNNER_DEADLINE.remaining()
    if remaining <= 0:
        return 0.0
    divisor = max(1, _SUBCOMMANDS_PENDING + 1)
    fair = remaining / divisor
    if fair < _MIN_HANDLER_BUDGET:
        return 0.0
    return min(default_seconds, fair)


def _clear_runner_deadline():
    """Cancel the shared deadline (normal completion)."""
    global _RUNNER_DEADLINE
    if _RUNNER_DEADLINE is not None:
        try:
            _RUNNER_DEADLINE.cancel()
        except Exception:
            pass
        _RUNNER_DEADLINE = None


def _entry_budget(module_name, argv, fallback):
    """Per-subcommand default budget, taken from hook_runtime's own table.

    The table in ``hook_runtime._entry_budget_rules`` still carries a rule for
    each of the five original PostToolUse entry points, with the measured
    evidence behind every number. After consolidation module_runner no longer
    matches those rules (it sees module ``posttooluse_runner``), so the runner
    consumes them HERE instead: the table stays the single source of truth for
    "how long is this hook allowed to take", and a future tuning pass to those
    numbers still lands on this path. Falls back to the caller's number if
    hook_runtime or the rule is unavailable.
    """
    try:
        from hook_runtime import resolve_entry_budget

        seconds, _label = resolve_entry_budget(module_name, list(argv))
        if seconds:
            return seconds
    except Exception:
        pass
    return fallback


# --------------------------------------------------------------------------- #
# The PostToolUse stdout envelope.
#
# Pre-consolidation each subcommand owned its own stdout stream and the host
# parsed each independently. Now they share one, and two JSON documents on one
# stream -- or a JSON document with a stray plain-text line beside it -- is not
# parseable. Only two of the six ever write to stdout:
#
#   bash_compress_hook  {"hookSpecificOutput": {"hookEventName": "PostToolUse",
#                        "updatedToolOutput": {...}}}   -- only when tool == Bash
#   archive_result      {"hookSpecificOutput": {"hookEventName": "PostToolUse",
#                        "updatedMCPToolOutput": "..."}} -- only when "__" in tool
#
# Those two conditions are mutually exclusive (a tool is either exactly "Bash"
# or an mcp__ tool, never both), so in practice AT MOST ONE JSON document
# exists and it is emitted BYTE-IDENTICALLY to what the six-entry wiring
# produced. The merge path below exists so that a future change which makes
# both fire cannot corrupt the stream: the two payload keys live side by side
# in one hookSpecificOutput object, which is valid.
# --------------------------------------------------------------------------- #


def _emit_post_tool_use_stdout(parts) -> None:
    """Emit every subcommand's stdout as AT MOST ONE host-valid JSON document."""
    texts = [p for p in parts if p and p.strip()]
    if not texts:
        return

    payloads = []
    plain = []
    for text in texts:
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                plain.append(text)
                continue
            if isinstance(parsed, dict):
                payloads.append((text, parsed))
            else:
                plain.append(text)
        else:
            plain.append(text)

    if not payloads:
        # No JSON in play: the raw stream is exactly what the host used to see.
        for text in plain:
            sys.stdout.write(text)
        return

    if len(payloads) == 1 and not plain:
        # The overwhelming common case. Emit the original bytes untouched so the
        # host sees precisely what the standalone hook produced.
        sys.stdout.write(payloads[0][0])
        return

    merged: dict = {}
    hook_specific: dict = {}
    for _text, obj in payloads:
        for key, value in obj.items():
            if key == "hookSpecificOutput" and isinstance(value, dict):
                hook_specific.update(value)
            else:
                merged[key] = value
    if hook_specific:
        merged["hookSpecificOutput"] = hook_specific
    sys.stdout.write(json.dumps(merged))

    # Plain text alongside JSON would break the parse. On exit 0 a PostToolUse
    # hook's stdout and stderr are both transcript-only, so routing it to stderr
    # keeps it visible in the same place without corrupting the document.
    for text in plain:
        try:
            sys.stderr.write(text)
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Subcommand handlers -- each mirrors its hooks.json entry exactly.
# --------------------------------------------------------------------------- #


_SUBCOMMAND_BUDGETS = {
    "bash_compress_hook": ("bash_compress_hook", ["--quiet"], 0.75),
    "archive_result": ("archive_result", ["--quiet"], 0.75),
    "context_intel": ("context_intel", ["--quiet"], 0.75),
    "read_cache --invalidate": ("read_cache", ["--invalidate", "--quiet"], 0.75),
    "quality-cache --throttle-only": (
        "measure",
        ["quality-cache", "--quiet", "--throttle-only"],
        2.0,
    ),
}


def _subcommand_budget(name: str) -> float:
    module_name, argv, fallback = _SUBCOMMAND_BUDGETS[name]
    return _entry_budget(module_name, argv, fallback)


def _sub_bash_compress(hook_input: dict) -> None:
    """``bash_compress_hook.py --quiet`` (entry 1, matcher ``Bash``).

    Calls the script's own ``main()``, which reads the shared payload through
    the patched ``hook_io.read_stdin_hook_input`` (it imports it inside main(),
    so the hook_io patch is what reaches it) and self-gates on
    ``tool_name != "Bash"``. It emits the ``updatedToolOutput`` replacement.
    """
    import bash_compress_hook  # noqa: PLC0415  (lazy: per-tool-call hot path)

    _rebind_shared_stdin(bash_compress_hook)
    bash_compress_hook.main()


def _sub_archive_result(hook_input: dict) -> None:
    """``archive_result.py --quiet`` (entries 2 AND 3 -- one call, see the
    matcher block: the two registrations are the same command under two
    disjoint matchers, so it ran at most once per tool call before too).
    """
    import archive_result  # noqa: PLC0415  (lazy: per-tool-call hot path)

    _rebind_shared_stdin(archive_result)
    archive_result.archive_result(quiet=True)


def _sub_context_intel(hook_input: dict) -> None:
    """``context_intel.py --quiet`` (entry 4). Emits nothing on stdout."""
    import context_intel  # noqa: PLC0415  (lazy: per-tool-call hot path)

    _rebind_shared_stdin(context_intel)
    context_intel.handle_post_tool_use()


def _sub_read_cache_invalidate(hook_input: dict) -> None:
    """``read_cache.py --invalidate --quiet`` (entry 5).

    Mirrors read_cache.main()'s --invalidate branch: no payload -> return, else
    ``handle_invalidate(payload, quiet=True)``. Called directly with the shared
    payload (the same shape sessionstart_runner uses for --clear-compacted).
    """
    if not hook_input:
        return
    import read_cache  # noqa: PLC0415  (lazy: per-tool-call hot path)

    read_cache.handle_invalidate(hook_input, True)


def _throttle_tick_due(hook_input: dict, throttle_seconds: int = 120) -> bool:
    """Is a quality-cache refresh due? -- asked as cheaply as the install allows.

    FALLBACK GATE. The primary route is ``_delegate_to_quality_cache_gate``,
    which hands the whole subcommand to the import diet's
    ``quality_cache_gate.py`` when that module is present. This function is what
    runs when it is not.

    The question itself costs one ``stat()`` of a marker file, but the only
    implementation of that stat in THIS tree lives in
    ``measure._quality_cache_tick_due``, so asking costs a 682ms cold measure
    import to run a syscall -- which is exactly why the throttle-only entry was
    the 798ms cold outlier of the six. A measure-free
    ``hook_runtime.quality_cache_tick_due(throttle_seconds, filepath,
    session_id)`` is honoured first if one ever appears, so the fallback path
    can shed the import too without another edit here.

    Deliberately NOT re-implemented here. The marker path is derived from
    measure's ``_STATE_BASE`` / ``QUALITY_CACHE_DIR`` resolution; a second copy
    of that logic in this file would be a second source of truth that could
    drift from the diet's, and drift here means either a stale statusline or a
    transcript parse on every tool call. One implementation, one seam.

    Fails OPEN (returns True) so a broken gate degrades to "ask the real
    quality_cache()", which enforces the throttle itself, rather than silently
    freezing the statusline forever.
    """
    gate = None
    try:
        import hook_runtime

        gate = getattr(hook_runtime, "quality_cache_tick_due", None)
    except Exception:
        gate = None
    if gate is None:
        try:
            gate = _measure()._quality_cache_tick_due
        except Exception:
            return True
    try:
        return bool(
            gate(
                throttle_seconds,
                hook_input.get("transcript_path"),
                hook_input.get("session_id"),
            )
        )
    except Exception:
        return True


def _delegate_to_quality_cache_gate(hook_input: dict) -> bool:
    """Route the quality-cache subcommand through ``quality_cache_gate.py``.

    THE IMPORT DIET, as it actually landed. A parallel workstream added
    ``skills/token-optimizer/scripts/quality_cache_gate.py``: a standalone hook
    script that answers the throttle question with ONE ``stat()`` of the marker
    and exits, importing measure.py only on the rare tick where the throttle has
    expired. It was built to REPLACE ``measure.py quality-cache --quiet
    --throttle-only`` as this event's hooks.json entry -- the very entry this
    consolidation folds in -- so the two changes touch the same hook.

    They compose instead of colliding, because the gate module IS the whole
    PostToolUse quality-cache entry point (gate, then the work). Delegating to
    it wholesale means the gate stays the single source of truth for the
    decision, this runner picks up the entire cold-import win the moment the
    module lands, and neither change undoes the other. The inline path below is
    the fallback for a tree where the module is not present yet, and it stays
    fully tested.

    Returns True when the gate module owned the call (so the caller must NOT
    also run the inline path and do the work twice), False when it is absent.

    TWO THINGS THE GATE ASSUMES THAT ARE NOT TRUE IN HERE, both handled below.

    1. It reads its own stdin. On POSIX that goes through
       ``hook_io.read_stdin_hook_input``, which this runner has already patched,
       so it would work by luck. On Windows ``_read_stdin_payload`` peeks and
       reads ``sys.stdin`` DIRECTLY, bypassing hook_io -- and this runner has
       already drained stdin, so the gate would get ``{}``, fall back to the
       legacy GLOBAL throttle marker instead of the per-session one, and share
       one throttle window across every concurrent session. So the payload is
       injected by replacing ``_read_stdin_payload`` for the duration of the
       call rather than relying on the hook_io patch reaching it.

    2. It parses ``sys.argv[1:]`` for ``--quiet``. Under module_runner this
       process's argv is ``["posttooluse_runner"]``, so the gate would run with
       ``quiet=False`` and could print on a path the standalone entry
       (``... quality_cache_gate.py --quiet``) kept silent. argv is set to the
       flags that entry passed, and restored afterwards.

    Both are restored in a ``finally`` so a raising gate cannot leave this
    process with a foreign argv or a stubbed reader.
    """
    try:
        import quality_cache_gate  # noqa: PLC0415  (optional: import diet)
    except Exception:
        return False
    entry = getattr(quality_cache_gate, "main", None)
    if not callable(entry):
        return False

    saved_argv = sys.argv
    saved_reader = getattr(quality_cache_gate, "_read_stdin_payload", None)
    try:
        sys.argv = ["quality_cache_gate", "--quiet"]
        if saved_reader is not None:
            quality_cache_gate._read_stdin_payload = lambda: dict(hook_input)
        _rebind_shared_stdin(quality_cache_gate)
        entry()
    finally:
        sys.argv = saved_argv
        if saved_reader is not None:
            quality_cache_gate._read_stdin_payload = saved_reader
    return True


def _quality_cache_self_heal() -> None:
    """Replicate the quality-cache dispatch's self-healing block: if the
    quality-cache hook is missing from settings.json and this is NOT a plugin
    install and quality_bar_disabled is unset, reinstall it.

    For plugin installs (the hooks.json context this runner runs in) the
    ``_is_plugin`` check is True and the block is a no-op, exactly as in the
    dispatch. Replicated verbatim so non-plugin manual installs keep the same
    self-heal behavior. Uses ``measure._quality_cache_hook_present`` (GitHub
    #155) so an install whose canonical hook is a consolidated dispatcher is not
    "healed" by appending a duplicate legacy hook. Fail-open: never raises.

    Note the ORDER: in the dispatch this runs only AFTER the throttle-only
    ``due`` check has passed (``if not due: sys.exit(0)`` comes first), so on a
    not-due tick neither settings.json nor config.json is read. That ordering is
    preserved in ``_sub_quality_cache`` and it matters -- these are two file
    reads on the hottest path in the product.
    """
    measure = _measure()
    try:
        _is_plugin = (
            measure._is_running_from_plugin_cache() or measure._is_plugin_installed()
        )
        try:
            _qb_disabled = False
            if measure.CONFIG_PATH.exists():
                _qb_cfg = json.loads(measure.CONFIG_PATH.read_text(encoding="utf-8"))
                _qb_disabled = _qb_cfg.get("quality_bar_disabled", False)
            if not _is_plugin and not _qb_disabled and measure.SETTINGS_PATH.exists():
                _sh_settings = json.loads(
                    measure.SETTINGS_PATH.read_text(encoding="utf-8")
                )
                _sh_hooks = _sh_settings.get("hooks", {}).get("UserPromptSubmit", [])
                if not measure._quality_cache_hook_present(_sh_hooks):
                    measure.setup_quality_bar(quiet=True)
        except Exception:
            pass
    except Exception:
        pass


def _sub_quality_cache(hook_input: dict) -> None:
    """``measure.py quality-cache --quiet --throttle-only`` (entry 6).

    Mirrors the ``quality-cache`` dispatch (measure.py ~L43065) for this exact
    flag set, in the dispatch's own order:

      1. argument-only policy: quiet=True, warn=False, force=False,
         throttle_only=True, throttle=120, warn_threshold=70.
      2. the throttle-only gate FIRST, before touching the filesystem, the
         daemon, settings, or config. Not due -> return, having done nothing.
      3. the mid-session daemon liveness pulse (swallowed).
      4. the settings.json self-heal block.
      5. quality_cache(..., pure_time_throttle=True, force=False). Neither
         --once-mark nor --once-per-session is in this entry's argv, so neither
         marker branch applies, and hook_event_name is PostToolUse so the
         SessionStart JSON-collapsing wrapper does not apply either.
      6. the cohort-tripwire piggyback, which the dispatch runs only on the
         throttle-only path.

    NO RUN-ONCE MARKER, DELIBERATELY. This entry's argv carries neither
    ``--once-mark`` nor ``--once-per-session``, so neither marker branch of the
    dispatch applies and this runner writes no session marker at all. That
    matters beyond parity: ``quality-cache-force`` is a marker path SHARED with
    the SessionStart and UserPromptSubmit runners, and a marker latched here
    before the work completed would silently disable the UserPromptSubmit
    recovery path (which opens by checking ``_ran_once_this_session(
    "quality-cache-force")``) for the rest of the session. The only marker this
    path touches at all is the 120s throttle marker, and measure touches that
    inside ``_write_quality_cache`` AFTER ``os.replace`` succeeds -- i.e. after
    the work, never before it. Keep it that way.

    THE INVARIANT (tests/test_hook_runtime_parity.py::
    test_throttle_only_cache_miss_never_parses_transcript): a throttle-only
    cache MISS must never parse a transcript. It is enforced inside
    ``quality_cache`` by ``if pure_time_throttle and not force and not
    cache_path.exists(): return None``, and the two kwargs that arm it are
    passed literally below. There is NO bootstrap branch here, deliberately:
    the UserPromptSubmit runner owns cache recovery because it fires once per
    prompt, while this fires on every tool call.
    """
    # Prefer the dedicated gate module when the import diet has landed it. See
    # _delegate_to_quality_cache_gate.
    if _delegate_to_quality_cache_gate(hook_input):
        return

    throttle = 120
    warn_threshold = 70

    # Step 2: the gate, BEFORE any filesystem/daemon/settings work.
    if not _throttle_tick_due(hook_input, throttle):
        return

    measure = _measure()
    try:
        measure._daemon_midsession_pulse()
    except Exception:
        pass
    _quality_cache_self_heal()

    measure.quality_cache(
        throttle_seconds=throttle,
        warn_threshold=warn_threshold,
        quiet=True,
        session_jsonl=hook_input.get("transcript_path"),
        force=False,
        pure_time_throttle=True,
        session_id=hook_input.get("session_id"),
        warn=False,
    )

    # Tripwire piggyback: the --throttle-only invocation fires on the PostToolUse
    # Edit/Write path (where active first-read follow-ups are resolved), so this
    # is the natural place to refresh the per-cohort live edit-rate verdict. It
    # is mtime-gated to 5 min, so the common case is a single sidecar stat().
    try:
        measure.evaluate_cohort_tripwire()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Dispatch table: (name, matcher, handler), in hooks.json entry order.
#
# A function, not a module constant, so the handler bound at dispatch time is
# whatever the module currently holds. That keeps the table honest for tests
# that substitute a single subcommand, and costs one tuple build per tool call.
# --------------------------------------------------------------------------- #


def _subcommand_table():
    return (
        ("bash_compress_hook", _MATCHER_BASH_COMPRESS, _sub_bash_compress),
        ("archive_result", _MATCHER_ARCHIVE, _sub_archive_result),
        ("context_intel", _MATCHER_CONTEXT_INTEL, _sub_context_intel),
        (
            "read_cache --invalidate",
            _MATCHER_READ_CACHE_INVALIDATE,
            _sub_read_cache_invalidate,
        ),
        ("quality-cache --throttle-only", _MATCHER_UNION, _sub_quality_cache),
    )


def main() -> int:
    hook_input = _read_hook_input()
    tool_name = str(hook_input.get("tool_name") or "")

    _install_shared_stdin(hook_input)

    # Gate FIRST, then budget: the fair share must be divided among the
    # subcommands that will actually run, not among all five. An Edit only runs
    # two of them and should get half the budget each, not a fifth.
    pending = [
        (name, handler)
        for name, matcher, handler in _subcommand_table()
        if _matches(matcher, tool_name)
    ]
    if not pending:  # pragma: no cover -- quality-cache's _MATCHER_UNION is always True
        return 0

    _install_runner_deadline()

    # Buffer every subcommand's stdout and emit in dispatch order at the end.
    _stdout_bufs: list[str] = []

    def _capture(name: str, fn, *args, **kwargs) -> None:
        budget = _runner_budget(_subcommand_budget(name))
        if budget <= 0:
            _report_budget_exhausted(name, 0.0, skipped=True)
            return
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run_safely(
                name,
                lambda: _run_with_handler_deadline(budget, fn, *args, **kwargs),
            )
        captured = buf.getvalue()
        if captured:
            _stdout_bufs.append(captured)

    _runner_budget(0.0, subcommand_count_hint=len(pending))
    for name, handler in pending:
        _capture(name, handler, hook_input)

    _emit_post_tool_use_stdout(_stdout_bufs)

    _clear_runner_deadline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
