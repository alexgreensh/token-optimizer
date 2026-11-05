"""Definition-only implementations; mutable state belongs to measure."""
from __future__ import annotations
from . import _compat


def _safe_int(value):
    """Coerce to int, mapping non-finite/garbage input to 0.

    Claude-path equivalent of codex_session._safe_int: json.loads accepts the
    non-standard Infinity/NaN literals, so a corrupt or hostile transcript can
    put float("inf")/nan into usage fields where int() then raises
    OverflowError/ValueError — neither caught by the readers' OSError guards.
    """
    _measure_facade = _compat.facade()
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
