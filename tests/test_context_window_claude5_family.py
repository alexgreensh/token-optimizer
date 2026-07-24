"""Regression tests for issue #97 — Claude 5 family context-window resolution.

`_MODEL_CONTEXT_WINDOWS` in hermes_session.py resolves versioned model ids by
prefix match, but the Claude 5 family (claude-fable-5, claude-sonnet-5, ...)
shares no prefix with any 4.x key, so it fell through to the 200K
_DEFAULT_CONTEXT_WINDOW instead of its real 1M window. Every fill/quality calc
for those models overstated context usage 5x, firing a false 100%/CRITICAL nag
each turn via the UserPromptSubmit hook.

The fix adds explicit 5-family keys plus a durable "modern non-haiku Claude ->
1M" fallback so future families (opus-6, sonnet-6, ...) can't silently regress.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from hermes_session import context_window_for_model  # noqa: E402

_1M = 1_000_000
_200K = 200_000


@pytest.mark.parametrize(
    "model,expected",
    [
        # --- issue #97 repro (the exact ids from the bug report) ---
        ("claude-fable-5", _1M),
        ("claude-sonnet-5", _1M),
        ("claude-opus-4-8", _1M),  # was already correct via prefix match
        # --- other explicit 5-family / new keys ---
        ("claude-mythos-5", _1M),
        ("claude-opus-5", _1M),
        ("claude-haiku-4-5", _200K),
        ("claude-haiku-4-5-20251001", _200K),  # prefix onto the new key
        ("claude-sonnet-5-20260101", _1M),     # snapshot suffix, prefix match
        # --- provider-qualified ids must resolve like their bare form ---
        ("anthropic/claude-fable-5", _1M),
        ("anthropic/claude-sonnet-5", _1M),
        ("openrouter/anthropic/claude-opus-5", _1M),
        ("anthropic/claude-haiku-4-5", _200K),
        # --- durable fallback: families we have not enumerated yet ---
        ("claude-opus-6", _1M),
        ("claude-sonnet-6-20270101", _1M),
        ("claude-haiku-5", _200K),  # unknown haiku stays 200K (fallback excludes haiku)
        # --- legacy Claude must stay 200K ---
        ("claude-3-opus-20240229", _200K),
        ("claude-3-5-sonnet-20241022", _200K),
        ("claude-2.1", _200K),  # unknown 2.x guarded to the 200K default
        # --- unknown / empty falls to the conservative default ---
        ("unknown", _200K),
        ("", _200K),
    ],
)
def test_context_window_resolution(model, expected):
    assert context_window_for_model(model) == expected


def test_no_claude5_model_resolves_to_the_bare_default():
    """The bug signature: a real 1M model resolving to exactly 200K by fallthrough."""
    for model in ("claude-fable-5", "claude-sonnet-5", "claude-mythos-5", "claude-opus-5"):
        assert context_window_for_model(model) == _1M, (
            f"{model} regressed to the default window (bug #97)"
        )
