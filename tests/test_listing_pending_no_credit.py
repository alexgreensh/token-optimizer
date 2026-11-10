"""Unobserved setup shrink must never enter a savings total."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure.structural_listing import apply_listing_check


def test_pending_listing_has_no_credited_tokens():
    trim = {"component": "skills", "scope": "global", "ts": "2026-01-01",
            "ts_epoch": 0, "tokens_before": 1000, "tokens_after": 500, "tokens": 500}
    apply_listing_check([trim], [], 4)
    assert trim["listing_check"] == "pending"
    assert trim["tokens"] == 0
    assert trim["tokens_estimated"] == 500
