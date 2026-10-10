"""Hermes context windows follow the documented per-model Claude table.

Haiku 5.5, Sonnet 5+, Opus 4.7+, Fable: 1M. Sonnet 4.6 / Opus 4.6: 200K, 1M only
as the [1m] variant. Everything older: 200K. Non-Claude rows are untouched.
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_session  # noqa: E402


@pytest.mark.parametrize("model,window", [
    ("claude-haiku-5-5", 1_000_000),
    ("anthropic/claude-haiku-5-5", 1_000_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-haiku-4-5-20251001", 200_000),
    ("claude-sonnet-4-6", 200_000),
    ("claude-sonnet-4-6[1m]", 1_000_000),
    ("claude-opus-4-6", 200_000),
    ("claude-opus-4-6[1m]", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("openrouter/anthropic/claude-sonnet-5-5", 1_000_000),
    ("claude-opus-5-5", 1_000_000),
    ("claude-fable-5-1", 1_000_000),
    ("claude-3-5-sonnet-20241022", 200_000),
    ("claude-opus-4-1-20250805", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-5.1", 1_000_000),
    ("o3", 200_000),
    ("opus-mt-en-de", 200_000),  # not a Claude id: default window
    ("", 200_000),
    ("unknown", 200_000),
])
def test_hermes_window_table(model, window):
    assert hermes_session.context_window_for_model(model) == window
