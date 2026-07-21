"""The reported context-fill % must disclose the window it was divided by.

Origin: issue #95. A user on a 1M-context session saw the hook escalate to
"Context at 89% capacity" while /context reported 19%. The numerator was right;
the denominator was 200k. Nothing in the message said which window was used or
where that window came from, so a 5x-wrong reading was indistinguishable from a
correct one -- and the assistant acted on it, truncating output and recommending
a fresh session to reclaim tokens that were never scarce.

detect_context_window() already returns (size, source) where source is a
human-readable origin such as "env: CLAUDE_CODE_DISABLE_1M_CONTEXT". Thirteen
call sites discarded it with [0], including the one feeding the user-visible
percentage. These tests pin the provenance to the output.

Run: python3 -m pytest tests/test_context_window_disclosure.py -v
"""

import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


# --- the note itself -------------------------------------------------------

def test_note_names_window_and_source():
    note = measure._format_window_note(
        {"model_context_window": 1_000_000, "model_context_window_source": "default 1M"}
    )
    assert "1M window" in note
    assert "source: default 1M" in note


def test_note_renders_a_200k_window_distinctly():
    """The whole point: a 200k denominator must be visible as 200k."""
    note = measure._format_window_note(
        {
            "model_context_window": 200_000,
            "model_context_window_source": "env: CLAUDE_CODE_DISABLE_1M_CONTEXT",
        }
    )
    assert "200k window" in note
    # The reader must be able to see WHY it is 200k, which is the actionable half.
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" in note


def test_note_is_empty_for_a_cache_without_the_window():
    """An older cache predates these fields and must degrade, not raise."""
    assert measure._format_window_note({}) == ""
    assert measure._format_window_note({"fill_pct": 42}) == ""


def test_note_survives_a_malformed_window():
    for bad in ({"model_context_window": "not-a-number"},
                {"model_context_window": 0},
                {"model_context_window": -1},
                {"model_context_window": None}):
        assert measure._format_window_note(bad) == ""


def test_note_without_source_still_names_the_window():
    note = measure._format_window_note({"model_context_window": 1_000_000})
    assert "1M window" in note
    assert "source:" not in note


def test_note_drops_the_override_hint_but_keeps_the_origin():
    """The source string carries a trailing hint for humans.

    The note is injected into the very context window it reports on, so it keeps
    the identifying clause and sheds the rest.
    """
    note = measure._format_window_note(
        {
            "model_context_window": 1_000_000,
            "model_context_window_source": (
                "default (1M, Opus/Sonnet 4.6+ GA. Override: TOKEN_OPTIMIZER_CONTEXT_SIZE)"
            ),
        }
    )
    assert "Override:" not in note
    assert "TOKEN_OPTIMIZER_CONTEXT_SIZE" not in note
    assert "default" in note


# --- provenance actually reaches the note ----------------------------------

def _clear_window_cache():
    measure._context_window_cache = None


def test_detect_reports_both_size_and_source():
    _clear_window_cache()
    window, source = measure.detect_context_window()
    assert isinstance(window, int) and window > 0
    assert isinstance(source, str) and source, "provenance must never be empty"


def test_disable_1m_env_is_named_as_the_source(monkeypatch):
    """The suspected root cause of #95 must identify itself in the output.

    A user with this flag set (including via settings.json's env block, which
    _resolve_feature_env also reads) gets a 200k denominator on every Claude
    session. The number alone looks plausible; the source is what gives it away.
    """
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_1M_CONTEXT", "1")
    _clear_window_cache()
    try:
        window, source = measure.detect_context_window()
        assert window == 200_000
        assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" in source

        # And it survives the trip through the note the user actually reads.
        note = measure._format_window_note(
            {"model_context_window": window, "model_context_window_source": source}
        )
        assert "200k window" in note
        assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" in note
    finally:
        _clear_window_cache()


def test_explicit_size_override_is_named_as_the_source(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_1M_CONTEXT", raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_SIZE", "350000")
    _clear_window_cache()
    try:
        window, source = measure.detect_context_window()
        assert window == 350_000
        assert "TOKEN_OPTIMIZER_CONTEXT_SIZE" in source
    finally:
        _clear_window_cache()
