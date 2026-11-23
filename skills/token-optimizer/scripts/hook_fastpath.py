"""Hook fast-path early-exit and monotonic time box (SPEC-hook-fastpath).

Pure stdlib (os, time, pathlib) so importing this module costs <1 ms and never
pulls in sqlite3, session_store, archive_result, or bash_compress.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

DEFAULT_COMPRESSION_BUDGET_S = 1.5
_QUALITY_THROTTLE_S = 120.0


class BudgetExceeded(Exception):
    """Raised when in-process compression or verification exceeds its time box."""


class MonotonicBudget:
    """In-process monotonic deadline checked at line-batch granularity."""

    __slots__ = ("deadline",)

    def __init__(self, seconds: float = DEFAULT_COMPRESSION_BUDGET_S) -> None:
        self.deadline = time.monotonic() + max(0.01, float(seconds))

    def check(self, index: int = 0, stride: int = 64) -> None:
        if stride <= 1 or (index % stride) == 0:
            if time.monotonic() > self.deadline:
                raise BudgetExceeded("compression time box exceeded")


def _quality_throttle_due(session_id: str, snapshot_dir: Path | None) -> bool:
    """True if quality-cache file exists and is older than _QUALITY_THROTTLE_S.

    If no cache file exists yet, throttle-only mode is a guaranteed no-op
    (see test_throttle_only_cache_miss_never_parses_transcript), so return False.
    """
    if not snapshot_dir or not session_id:
        return False
    try:
        qfile = snapshot_dir / f"quality-cache-{session_id}.json"
        if not qfile.is_file():
            return False
        age = time.time() - qfile.stat().st_mtime
        return age >= _QUALITY_THROTTLE_S
    except OSError:
        return False


def _resolve_snapshot_dir() -> Path | None:
    env_dir = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    pdata = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip() or os.environ.get("TOKEN_OPTIMIZER_PLUGIN_DATA", "").strip()
    if pdata:
        return Path(pdata)
    return Path.home() / ".claude" / "token-optimizer"


def can_early_exit_posttooluse(
    hook_input: dict,
    snapshot_dir: Path | None = None,
    *,
    only_throttle_matches: bool = False,
) -> bool:
    """Return True only when PostToolUse is provably a no-op.

    The runner's sole proven no-op is a tool whose only matching handler is the
    quality-cache throttle tick, with no tick due. Every tool that matches
    context_intel (Bash, Edit, Write, Read, ...) always records an activity row,
    and Edit-family tools always invalidate the read cache, so neither may skip.
    The caller states the matcher result via ``only_throttle_matches``; without
    it this returns False.
    """
    if not only_throttle_matches or not isinstance(hook_input, dict):
        return False
    if hook_input.get("hook_event_name") == "PostToolUseFailure":
        return False
    if os.environ.get("TOKEN_OPTIMIZER_COVERAGE_FUNNEL", "").strip() == "1":
        return False
    session_id = str(hook_input.get("session_id") or "")
    sdir = snapshot_dir if snapshot_dir is not None else _resolve_snapshot_dir()
    return not _quality_throttle_due(session_id, sdir)
