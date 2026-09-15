"""Regression tests for the Hermes context-fill nudge (cumulative vs live prompt size).

WHY THIS EXISTS
---------------
The nudge divided the session-CUMULATIVE input tally by the model window. Every host re-sends the
whole conversation on each turn, so that sum climbs past the window no matter how small the live
context is, and the nudge eventually announced a context emergency that did not exist.

Measured on a real Hermes session (2026-09-15, 129 API calls): the plugin's cumulative figure was
1,285,803 tokens against a 1,000,000 window -> "Context ~100% full ... Grade: F" (the percentage is
capped at 100), while Hermes itself reported 278,545 / 1,000,000 = 28% for the same session.

The fill must come from the prompt the LAST call actually sent (fresh + cached prompt tokens);
the cumulative tally stays available for cost/usage reporting.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERMES_INIT = Path(__file__).resolve().parents[1] / "hermes" / "__init__.py"
WINDOW = 1_000_000


@pytest.fixture()
def plugin():
    """Load hermes/__init__.py by path, with a fixed window and clean per-session state."""
    spec = importlib.util.spec_from_file_location("to_hermes_init_under_test", HERMES_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    # Deterministic window: the real resolver reads a model catalog we do not want to couple to.
    module._context_window = lambda model: WINDOW  # noqa: SLF001 — deliberate test seam

    module._TALLY.clear()
    module._NUDGED.clear()
    module._ROLLED_UP.clear()
    yield module
    module._TALLY.clear()
    module._NUDGED.clear()
    module._ROLLED_UP.clear()


def _call(plugin, session_id: str, input_tokens: int, cache_read: int = 0) -> None:
    plugin.on_post_api_request(
        session_id=session_id,
        usage={"input_tokens": input_tokens, "output_tokens": 1, "cache_read_tokens": cache_read,
               "cache_write_tokens": 0, "reasoning_tokens": 0},
    )


def _nudge(plugin, session_id: str, history_len: int = 10):
    return plugin.on_pre_llm_call(
        session_id=session_id,
        model="any-model",
        conversation_history=[{"role": "user", "content": "x"}] * history_len,
    )


def test_cumulative_tally_never_triggers_the_nudge(plugin):
    """Many small turns: cumulative input exceeds the window, live occupancy stays tiny."""
    for _ in range(200):
        _call(plugin, "s-cumulative", input_tokens=10_000, cache_read=0)

    tally = plugin._TALLY["s-cumulative"]
    assert tally["input"] == 2_000_000 > WINDOW, "precondition: cumulative tally passed the window"
    assert tally["last_prompt"] == 10_000, "live prompt size must be the LAST call only"

    assert _nudge(plugin, "s-cumulative") is None


def test_nudge_fires_on_live_prompt_size(plugin):
    """A genuinely full live context still nudges, and reports the live number."""
    _call(plugin, "s-live", input_tokens=5_000, cache_read=0)
    _call(plugin, "s-live", input_tokens=790_000, cache_read=0)  # 79% of the window, fresh tokens

    nudge = _nudge(plugin, "s-live")
    assert nudge is not None and "context" in nudge
    text = nudge["context"]
    assert "790,000" in text and "1,000,000" in text
    assert "79%" in text
    assert "2,000,000" not in text  # never the cumulative figure


def test_cached_prompt_tokens_count_toward_fill(plugin):
    """Cached prompt tokens occupy the window too — ignoring them undersells the fill."""
    _call(plugin, "s-cache", input_tokens=100, cache_read=750_000)

    nudge = _nudge(plugin, "s-cache")
    assert nudge is not None
    assert "750,100" in nudge["context"] or "750,000" in nudge["context"]


def test_nudge_is_once_per_session(plugin):
    _call(plugin, "s-once", input_tokens=900_000)
    assert _nudge(plugin, "s-once") is not None
    assert _nudge(plugin, "s-once") is None


def test_first_turn_without_tally_estimates_from_history(plugin):
    """Before any completed call the history estimate is still the fallback of record."""
    # ~3.3 chars per token: 300k chars ~= 90k tokens -> silent.
    short = plugin.on_pre_llm_call(
        session_id="s-first",
        model="any-model",
        conversation_history=[{"role": "user", "content": "y" * 300_000}],
    )
    assert short is None

    # ~3.3M chars ~= 1M tokens -> over the window, nudges.
    long_history = plugin.on_pre_llm_call(
        session_id="s-first-long",
        model="any-model",
        conversation_history=[{"role": "user", "content": "y" * 3_300_000}],
    )
    assert long_history is not None
