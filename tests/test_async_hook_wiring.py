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
    ("SessionStart", None, "ensure-health"): True,
    ("SessionStart", None, "quality-cache --force"): False,
    ("SessionStart", "compact", "compact-restore --compact"): False,
    ("SessionStart", None, "compact-restore --new-session-only"): False,
    ("Stop", None, "compact-capture --trigger stop --quiet"): False,
    ("Stop", None, "session-end-flush --trigger stop"): False,
    ("Stop", None, "keepwarm-arm"): False,
    ("SessionEnd", None, "session-end-flush"): True,
    ("StopFailure", None, "compact-capture --trigger stop-failure"): False,
    ("UserPromptSubmit", None, "quality-cache --warn"): False,
    ("UserPromptSubmit", None, "prompt-continuity"): False,
    ("UserPromptSubmit", None, "verbosity-steer"): False,
    ("PostToolUse", "mcp__.*", "archive_result.py"): True,
    ("PostToolUse", "Bash|Read|Glob|Grep|Agent", "archive_result.py"): True,
    ("PostToolUse", "Bash|Read|Grep|Glob|mcp__.*", "context_intel.py"): True,
    ("PostToolUse", "Edit|Write|MultiEdit|NotebookEdit", "read_cache.py --invalidate"): False,
    (
        "PostToolUse",
        "Bash|Read|Glob|Grep|Agent|Edit|Write|MultiEdit|NotebookEdit|mcp__.*",
        "quality-cache --quiet --throttle-only",
    ): False,
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


def test_total_async_count_is_seven():
    count = sum(1 for *_, is_async in _flatten(HOOKS_JSON) if is_async)
    assert count == 7, (
        f"expected exactly 7 async hook entries, found {count}. "
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
    mirror_no_async = [(e, m, c) for e, m, c, _ in mirror]
    assert root == mirror_no_async, (
        "mirror hooks.json content (event/matcher/command) has drifted from the root -- "
        "run scripts/sync-codex-marketplace-plugin.sh"
    )
