"""When the host's fill figure disagrees with ours, our window is wrong.

live-fill.json carries used_percentage supplied by Claude Code itself, already
gated on freshness and session match. When that gate passes, the host figure
wins -- which is correct, since the host knows the real window and we only infer
it.

The subtle part: that rescue makes a misconfiguration invisible. The user sees a
correct percentage, so nothing looks broken, while the inferred window stays
wrong for every other consumer (the dashboard, trends, the savings estimate).
Recording the disagreement is what turns a silent save into a signal.

This layer is a corroborator, not a primary source. When live-fill.json is
stale, missing, or from another session it says nothing, and the disclosure and
contradiction guards still stand on their own.

Run: python3 -m pytest tests/test_live_fill_crosscheck.py -v
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

SRC = (SCRIPTS / "measure.py").read_text(encoding="utf-8")


def test_host_value_is_still_preferred():
    """The host wins. This layer observes, it does not overrule."""
    assert 'fill_pct = min(1.0, max(0.0, live["used_percentage"] / 100.0))' in SRC
    assert "host_fill_pct = fill_pct" in SRC
    # Nothing may reassign fill_pct from our own arithmetic after the host set it.
    block = SRC[SRC.index("if host_fill_pct is not None:"):][:1200]
    assert "fill_pct =" not in block, "cross-check must not overrule the host"


def test_disagreement_records_both_values_and_the_window_source():
    block = SRC[SRC.index("if host_fill_pct is not None:"):][:1200]
    for field in ("host_pct", "computed_pct", "window", "window_source"):
        assert field in block, f"disagreement must record {field}"


def test_threshold_is_wide_enough_to_ignore_rounding():
    """Denominator errors are multiples; rounding is noise. Only the first matters."""
    assert "abs(_ours - host_fill_pct) > 0.10" in SRC


def test_gate_still_requires_freshness_and_session_match():
    """An unmatched or stale host figure must not be treated as ground truth."""
    assert "if age < 10 and want_sid and live_sid == want_sid:" in SRC


def test_crosscheck_is_silent_without_a_host_figure():
    """No live-fill means no opinion, not a crash and not a default."""
    assert "host_fill_pct = None" in SRC
    assert "host_disagreement = None" in SRC
    assert "if host_fill_pct is not None:" in SRC


def test_disagreement_is_persisted_for_consumers():
    assert '"host_disagreement": host_disagreement,' in SRC


def test_crosscheck_cannot_raise_into_the_caller():
    block = SRC[SRC.index("if host_fill_pct is not None:"):][:1200]
    assert "except (TypeError, ValueError):" in block


# --- the comparison arithmetic ---------------------------------------------

def _disagrees(ours, host):
    return abs(ours - host) > 0.10


def test_the_reporters_shape_is_caught():
    """~178k tokens: 19% against a real 1M window, 89% against a wrong 200k one.

    The contradiction guard cannot catch this (178k fits inside 200k), which is
    exactly the gap this layer closes.
    """
    assert _disagrees(0.89, 0.19) is True


def test_rounding_noise_does_not_fire():
    assert _disagrees(0.190, 0.195) is False
    assert _disagrees(0.50, 0.55) is False


def test_boundary_is_exclusive():
    assert _disagrees(0.30, 0.20) is False, "exactly 10 points is not a denominator error"
    assert _disagrees(0.31, 0.20) is True
