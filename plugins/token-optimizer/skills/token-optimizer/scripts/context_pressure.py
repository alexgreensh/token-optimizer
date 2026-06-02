"""Context pressure detection for hook injection gating.

Hooks that inject content into the conversation check pressure level
before emitting. At high fill, informational injections are suppressed.
At critical fill, all non-essential injections are suppressed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from runtime_env import runtime_home

PRESSURE_NORMAL = "normal"
PRESSURE_HIGH = "high"
PRESSURE_CRITICAL = "critical"

_HIGH_THRESHOLD = 75
_CRITICAL_THRESHOLD = 90

_VALID_PRIORITIES = frozenset({"essential", "token-saving", "informational"})

_QUALITY_CACHE_DIR = runtime_home() / "token-optimizer"


def _sanitize_id(raw: str) -> str:
    """Strip unsafe characters and fall back to 'unknown' if empty."""
    safe = "".join(c for c in raw if c.isalnum() or c in "-_")
    return safe or "unknown"


def _resolve_cache_path(
    session_file: str | None = None,
    session_id: str | None = None,
) -> Path:
    """Derive quality cache path from session file or session ID."""
    if session_file:
        safe = _sanitize_id(Path(session_file).stem)
        return _QUALITY_CACHE_DIR / f"quality-cache-{safe}.json"
    if session_id:
        safe = _sanitize_id(session_id)
        return _QUALITY_CACHE_DIR / f"quality-cache-{safe}.json"
    return _QUALITY_CACHE_DIR / "quality-cache.json"


def get_pressure_level(
    session_file: str | None = None,
    session_id: str | None = None,
) -> str:
    """Return context pressure level based on quality cache fill_pct.

    Args:
        session_file: Path to the session JSONL file.
        session_id: Session UUID (used when transcript_path unavailable).
            Either parameter derives the per-session cache path.
            Falls back to global cache when neither is provided.

    Returns:
        "normal" (<75% fill), "high" (75-90%), or "critical" (>=90%).
        Defaults to "normal" on any error (fail-open).
    """
    try:
        cache_path = _resolve_cache_path(session_file, session_id)
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        fill_pct = float(data.get("fill_pct", 0) or 0)

        if fill_pct >= _CRITICAL_THRESHOLD:
            return PRESSURE_CRITICAL
        if fill_pct >= _HIGH_THRESHOLD:
            return PRESSURE_HIGH
        return PRESSURE_NORMAL
    except Exception:
        return PRESSURE_NORMAL


def should_inject(
    session_file: str | None = None,
    session_id: str | None = None,
    *,
    priority: str = "informational",
) -> bool:
    """Check whether an injection should proceed given current pressure.

    Args:
        session_file: Path to session JSONL for per-session cache lookup.
        session_id: Session UUID (alternative to session_file).
        priority: "essential" (compact guidance, checkpoint body),
                  "token-saving" (read cache, bash rewrites),
                  or "informational" (quality warnings, archive confirmations).

    Returns:
        True if the injection should proceed, False if suppressed.
    """
    if priority not in _VALID_PRIORITIES:
        return True

    pressure = get_pressure_level(session_file, session_id)

    if pressure == PRESSURE_NORMAL:
        return True

    if pressure == PRESSURE_HIGH:
        return priority in ("essential", "token-saving")

    return priority == "essential"


def log_suppression(injection_name: str, pressure: str) -> None:
    """Log to stderr when an injection is suppressed."""
    print(
        f"[Token Optimizer] Suppressed {injection_name} (context pressure: {pressure})",
        file=sys.stderr,
    )
