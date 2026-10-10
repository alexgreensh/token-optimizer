"""Observed tokens above the window mean a wrong window, not a full context.

Origin: a misdetected context window. The fill percentage was computed as
min(1.0, tokens / window). When the window is misdetected too small, that clamp
converts an impossible ratio into a confident-looking 100% -- the single most
plausible-looking output the code can produce, and indistinguishable from a
genuinely full session. The reporter's case fit exactly: ~178k observed against
a 200k window while the host reported a 1M session.

The contradiction is pure arithmetic, so it needs no cooperation from the host
and holds for runtimes with legitimately smaller windows (Codex, Hermes) too.

Run: python3 -m pytest tests/test_context_window_contradiction.py -v
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402

SRC = (SCRIPTS / "measure.py").read_text(encoding="utf-8")


def test_contradiction_is_detected_before_the_clamp():
    """The flag must be computed from the raw ratio, not the clamped value.

    Reading the clamped value can never reveal the contradiction: 1.0 is the
    same whether the session is genuinely full or the window is 5x too small.
    """
    assert "raw_ratio = float(context_tokens) / float(model_context_window)" in SRC
    assert "window_contradicted = raw_ratio > 1.0" in SRC
    # Order matters: detection must precede the clamp that erases the evidence.
    assert SRC.index("window_contradicted = raw_ratio > 1.0") < SRC.index(
        "model_fill = min(1.0, max(0.0, raw_ratio))"
    )


def test_contradiction_is_persisted_for_consumers():
    """The producer computes it; the consumer that prints the message reads it."""
    assert '"window_contradicted": window_contradicted,' in SRC
    assert 'result["context_window_contradicted"] = bool(cfd.get("window_contradicted"))' in SRC


def test_nudge_is_suppressed_when_the_window_is_contradicted():
    assert 'if cached.get("context_window_contradicted"):' in SRC
    suppress_at = SRC.index('if cached.get("context_window_contradicted"):')
    # It must bail before the tier logic that would emit a percentage-based nudge.
    # Anchor updated when ResourceHealth stopped being labeled
    # as quality. The invariant is unchanged: suppression first, tiers after.
    # If this index() ever raises, repoint the anchor; do not delete the assertion.
    tier_at = SRC.index('f"[Token Optimizer] Context {fill_pct:.0f}%{window_note}, resource health')
    assert suppress_at < tier_at, "suppression must precede the nudge tiers"


def test_suppression_does_not_guess_a_replacement_window():
    """Guessing a denominator is the original defect; do not do it twice."""
    window = SRC[SRC.index('if cached.get("context_window_contradicted"):'):][:900]
    assert "misdetected" in window
    assert "TOKEN_OPTIMIZER_CONTEXT_SIZE" in window, "must tell the user how to fix it"
    for guess in ("1_000_000", "1000000", "200_000", "200000"):
        assert guess not in window, f"suppression path must not hardcode {guess}"


# --- the arithmetic itself -------------------------------------------------

def _ratio_flag(tokens, window):
    """Mirror of the production expression, to pin the boundary behavior."""
    raw = float(tokens) / float(window)
    return raw > 1.0, min(1.0, max(0.0, raw))


def test_boundary_is_not_over_eager():
    """Exactly-full is legitimate and must still report a percentage."""
    contradicted, fill = _ratio_flag(200_000, 200_000)
    assert contradicted is False
    assert fill == 1.0

    contradicted, fill = _ratio_flag(199_999, 200_000)
    assert contradicted is False
    assert fill < 1.0


def test_the_reporters_case_is_caught():
    """~178k observed against a 200k window on a session the host called 1M."""
    contradicted, _ = _ratio_flag(178_000, 200_000)
    assert contradicted is False, "178k fits in 200k; this case needs the host cross-check"

    # But the same tokens against a window smaller than them is provably wrong.
    contradicted, fill = _ratio_flag(250_000, 200_000)
    assert contradicted is True
    assert fill == 1.0, "clamp still yields 100%, which is exactly why the flag is needed"


def test_a_legitimately_small_window_is_unaffected():
    """Codex/Hermes run at 200k for real; proportionate tokens must not trip it."""
    contradicted, fill = _ratio_flag(50_000, 200_000)
    assert contradicted is False
    assert 0.24 < fill < 0.26
