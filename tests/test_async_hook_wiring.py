#!/usr/bin/env python3
"""Regression tests for which hooks.json entries carry "async": true.

Async hooks are fire-and-forget: Claude Code does not wait for them and
their stdout/JSON output is discarded entirely -- they cannot inject
additionalContext, set a permissionDecision, or block anything. A hook is
only safe to mark async if its entire job is a side effect nobody reads
back, AND (for Stop/StopFailure specifically) losing the write to a process
exiting right after the turn ends would be harmless.

Original classification and test scaffold contributed by danikdanik (PR #86).
This is the REDUCED-SCOPE landing: only the four hook groups whose output-free
+ race-free + exit-safe status was independently verified against source are
async here. Seven of danikdanik's original eleven flips were reverted to sync
after review found real hazards:

  - quality-cache --force / --throttle-only (SessionStart, PostCompact,
    PostToolUse): quality_cache() has an UNCONDITIONAL systemMessage print
    path (measure.py ~27982) not gated by --quiet/--warn, and it persists
    one-shot dedup flags. Async-dropping the message ALSO poisons the sync
    UserPromptSubmit --warn fallback, permanently losing the warning.
  - read_cache.py --invalidate / --clear (PostToolUse, CwdChanged, PreCompact):
    same-session read-after-write race against the sync PreToolUse/Read cache
    reader.
  - keepwarm-arm (Stop): kept sync for the same process-exit corruption-safety
    reason its sibling Stop hooks are sync.

This test pins the exact safe set so a future edit that flips the wrong one
fails loudly instead of shipping.

Run: python3 -m pytest tests/test_async_hook_wiring.py -v
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
MIRROR_HOOKS_JSON = REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json"

# (event, matcher-or-None, distinguishing substring of the command) -> expected "async" value.
# Order within an event matters when matcher is None (Stop's three hooks share no matcher).
EXPECTED_ASYNC = {
    ("PreToolUse", "Read", "read_cache.py --quiet"): False,
    ("PreToolUse", "Bash", "bash_hook.py --quiet"): False,
    ("PreToolUse", "Agent|Task", "checkpoint-trigger"): True,
    # MUST be sync: the re-fetch guard's whole job is to return a permissionDecision
    # (deny) BEFORE the duplicate MCP call runs. An async guard couldn't block it.
    ("PreToolUse", "mcp__.*", "refetch_guard.py"): False,
    ("PreCompact", None, "dynamic-compact-instructions"): False,
    ("PreCompact", None, "compact-capture --trigger auto"): True,
    ("PreCompact", None, "read_cache.py --clear"): False,
    # The five SessionStart subcommands are consolidated into a single
    # dispatcher (hooks/sessionstart_runner.py) that imports measure.py once
    # and runs all five in-process under one shared deadline. Codex enforces a
    # hard 25s SessionStart ceiling and killed the five-entry group (declared
    # 15 + 20 + 20 + 10 + 20 = 85s).
    #
    # It is sync (not async): SessionStart injects additionalContext via stdout,
    # which an async hook would discard entirely -- compact-restore's recovery
    # context and the ensure-health notices both ride that stream, and the
    # read_cache --clear-compacted must run deterministically before the next
    # PreToolUse/Read judges redundancy.
    #
    # KNOWN CHANGE: the former `ensure-health --once-mark` entry carried
    # "async": true. A group cannot be half-async, and four of the five
    # subcommands must stay sync, so ensure-health is now synchronous too. Its
    # cost is bounded by the runner's shared 18s deadline (vs. the 70s the four
    # sync entries declared between them), and its stdout -- previously
    # discarded on Claude Code, emitted on Codex where the mirror strips async
    # -- is now emitted on both.
    ("SessionStart", None, "sessionstart_runner.py"): False,
    # The three Stop subcommands (compact-capture --trigger stop, session-end-flush
    # --trigger stop --defer, keepwarm-arm) are consolidated into a single
    # dispatcher (hooks/stop_runner.py) that imports measure.py once and runs all
    # three in-process under one shared deadline. It is sync (not async): Stop
    # fires on every turn end and an async hook's fire-and-forget semantics would
    # discard any diagnostic stdout and race with the process exit that follows
    # the Stop event. Same reasoning as the three legacy sync entries.
    ("Stop", None, "stop_runner.py"): False,
    # SessionEnd joins the stop_runner (same file, different hooks.json entry).
    # It keeps async=true (host fire-and-forget): session-end-flush --trigger end
    # --defer spawns a detached worker and returns immediately; the hook's stdout
    # is never consumed and the work outlives the process.
    ("SessionEnd", None, "stop_runner.py"): True,
    ("StopFailure", None, "compact-capture --trigger stop-failure"): False,
    # The six UserPromptSubmit subcommands are consolidated into a
    # single dispatcher (hooks/userpromptsubmit_runner.py) that imports measure.py
    # once and runs all six in-process. It is sync (not async): UserPromptSubmit
    # injects additionalContext via stdout, which an async hook would discard.
    # The six former subcommand substrings no longer appear in hooks.json; the
    # runner reproduces them internally with per-subcommand failure isolation.
    ("UserPromptSubmit", None, "userpromptsubmit_runner.py"): False,
    # The six PostToolUse subcommands are consolidated into a single dispatcher
    # (hooks/posttooluse_runner.py) that runs all five in-process under one
    # shared deadline. PostToolUse fires on EVERY tool call, so six entries meant
    # six process spawns, 80s of combined declared budget, and six dispatch
    # chains on the hottest path in the product. A sustained container workload
    # CANCELLED PostToolUse:Bash 372 times and completed it 9 times.
    #
    # It is sync (not async), and it HAD to be: three of the six could never be
    # async and a hook group cannot be half-async.
    #   - bash_compress_hook returns updatedToolOutput to REPLACE the tool result
    #     before the model reads it; an async fire-and-forget hook cannot mutate
    #     output already sent to context.
    #   - read_cache.py --invalidate races the sync PreToolUse/Read cache reader
    #     in the same session (read-after-write).
    #   - quality-cache has an UNCONDITIONAL systemMessage print path not gated
    #     by --quiet/--warn, and it persists one-shot dedup flags.
    #
    # KNOWN CHANGE: the two `archive_result.py` entries and the `context_intel.py`
    # entry carried "async": true and are now synchronous.
    #   - context_intel emits NOTHING on stdout, so nothing is discarded either
    #     way; the turn now waits for its session-store write (~96ms in-process).
    #   - archive_result's mcp__.* registration prints updatedMCPToolOutput to
    #     replace an oversized MCP result with a preview plus an archive pointer.
    #     As an ASYNC hook that stdout was DISCARDED on Claude Code, so the
    #     replacement never happened there -- while it already happened on Codex,
    #     whose mirror strips every async flag. Going sync makes Claude Code match
    #     the Codex mirror and the code's documented intent. That is a real,
    #     user-visible behaviour change and it is deliberate.
    #   - What could NOT be preserved: fire-and-forget. A stalled archive/intel
    #     write can now delay the turn, bounded by the runner's shared deadline
    #     (2.5s, under hook_runtime's silent 4.5s BUDGET_POSTTOOL_RUNNER backstop).
    #
    # The five former subcommand substrings no longer appear in hooks.json; the
    # runner reproduces each one internally, re-checking its ORIGINAL tool matcher
    # in-process. See tests/test_posttooluse_runner.py.
    (
        "PostToolUse",
        "Bash|PowerShell|Read|Glob|Grep|Agent|Edit|Write|MultiEdit|NotebookEdit|mcp__.*",
        "posttooluse_runner.py",
    ): False,
    # A failed Bash call is delivered on PostToolUseFailure, not PostToolUse --
    # and the Windows PowerShell tool is the same event's other shell consumer.
    # Sync (not async): the nudge rides additionalContext, which an async
    # hook would discard, and the thrash guard's streak write must land
    # before the next run of the same command.
    ("PostToolUseFailure", "Bash|PowerShell", "posttooluse_runner.py"): False,
    ("PostCompact", None, "quality-cache --force"): False,
    ("CwdChanged", None, "read_cache.py --clear"): False,
}


def _flatten(hooks_json_path):
    """Yield (event, matcher, command, async_flag) for every hook command entry."""
    data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    for event, entries in data["hooks"].items():
        for entry in entries:
            matcher = entry.get("matcher")
            for hook in entry["hooks"]:
                yield event, matcher, hook["command"], hook.get("async", False)


def test_every_expected_hook_has_the_right_async_value():
    seen = set()
    for event, matcher, command, is_async in _flatten(HOOKS_JSON):
        matched_key = None
        for key in EXPECTED_ASYNC:
            k_event, k_matcher, k_substr = key
            if k_event == event and k_matcher == matcher and k_substr in command:
                matched_key = key
                break
        assert matched_key is not None, (
            f"unrecognized hook entry not covered by this test: "
            f"event={event!r} matcher={matcher!r} command={command!r}. "
            "Add it to EXPECTED_ASYNC with an explicit safe/unsafe classification."
        )
        assert matched_key not in seen, f"duplicate match for {matched_key}"
        seen.add(matched_key)
        expected = EXPECTED_ASYNC[matched_key]
        assert is_async == expected, (
            f"{matched_key}: expected async={expected}, got async={is_async}. "
            "If you're intentionally changing this, re-verify the hook's output "
            "contract first -- async hooks are fire-and-forget and their "
            "stdout/JSON is discarded entirely."
        )

    missing = set(EXPECTED_ASYNC) - seen
    assert not missing, f"hooks.json no longer contains expected entries: {missing}"


def test_total_async_count_is_three():
    """Was seven, then six, now three.

    The SessionStart consolidation dropped the `ensure-health --once-mark` async
    entry: its five subcommands now share ONE sync dispatcher and four of them
    need their stdout.

    The PostToolUse consolidation dropped three more (two `archive_result.py`
    registrations and `context_intel.py`): its six subcommands now share ONE
    dispatcher, and three of the six can never be async, so the group is sync.
    See the PostToolUse note in EXPECTED_ASYNC for what that changes.

    The three that remain are all genuinely output-free and race-free:
    PreToolUse `checkpoint-trigger`, PreCompact `compact-capture --trigger auto`,
    and SessionEnd `session-end-flush`."""
    count = sum(1 for *_, is_async in _flatten(HOOKS_JSON) if is_async)
    assert count == 3, (
        f"expected exactly 3 async hook entries, found {count}. "
        "If you added or removed one intentionally, update this test and "
        "EXPECTED_ASYNC together."
    )


def test_mirror_has_async_stripped_but_is_otherwise_identical():
    """plugins/token-optimizer/ (Codex mirror) must have async stripped -- Codex
    skips any hook with async: true entirely -- but be identical otherwise."""
    root = [(e, m, c) for e, m, c, _ in _flatten(HOOKS_JSON)]
    mirror = list(_flatten(MIRROR_HOOKS_JSON))

    assert all(not is_async for *_, is_async in mirror), (
        "mirror hooks.json must have every async flag stripped (Codex doesn't support async hooks)"
    )
    # The Codex mirror applies one further intentional transform beyond async-strip:
    # launcher commands are rewritten to a runtime version-resolver (mid-session
    # self-heal, since Codex pins ${CLAUDE_PLUGIN_ROOT}). Normalize that back to the
    # root's simple guard so this test still verifies "no OTHER drift".
    from _codex_mirror_norm import guard_from_resolver
    mirror_no_async = [(e, m, guard_from_resolver(c)) for e, m, c, _ in mirror]
    assert root == mirror_no_async, (
        "mirror hooks.json content (event/matcher/command) has drifted from the root -- "
        "run scripts/sync-codex-marketplace-plugin.sh"
    )
