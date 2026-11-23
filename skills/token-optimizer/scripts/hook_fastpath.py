"""Hook fast-path early-exit and monotonic time box (SPEC-hook-fastpath).

Pure stdlib (os, time, pathlib) so importing this module costs <1 ms and never
pulls in sqlite3, session_store, archive_result, or bash_compress.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

DEFAULT_COMPRESSION_BUDGET_S = 1.5
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
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


def _read_cache_has_entry(session_id: str, file_path: str, snapshot_dir: Path | None) -> bool:
    """Cheap file existence / substring check without importing read_cache or sqlite3."""
    if not snapshot_dir or not file_path:
        return False
    try:
        rc_dir = snapshot_dir / "read-cache"
        if not rc_dir.is_dir():
            return False
        # Check session file if present
        if session_id:
            sf = rc_dir / f"{session_id}.json"
            if sf.is_file():
                return file_path in sf.read_text(encoding="utf-8", errors="ignore")
            return False
        return any(rc_dir.iterdir())
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


def can_early_exit_posttooluse(hook_input: dict, snapshot_dir: Path | None = None) -> bool:
    """Return True when PostToolUse is guaranteed to be a no-op and can exit
    immediately before importing sqlite3, session_store, archive_result, or
    bash_compress.
    """
    if not isinstance(hook_input, dict):
        return False
    if hook_input.get("hook_event_name") == "PostToolUseFailure":
        return False
    tool_name = str(hook_input.get("tool_name") or "")
    session_id = str(hook_input.get("session_id") or "")
    sdir = snapshot_dir if snapshot_dir is not None else _resolve_snapshot_dir()

    if tool_name in _EDIT_TOOLS:
        ti = hook_input.get("tool_input") or {}
        fpath = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if not _read_cache_has_entry(session_id, fpath, sdir) and not _quality_throttle_due(session_id, sdir):
            return True
        return False

    if tool_name == "Bash":
        tr = hook_input.get("tool_response")
        if isinstance(tr, dict):
            stdout = tr.get("stdout") or ""
            stderr = tr.get("stderr") or ""
            exit_code = tr.get("exit_code", 0)
            if (
                isinstance(stdout, str)
                and isinstance(stderr, str)
                and len(stdout) + len(stderr) < 100
                and (exit_code is None or exit_code == 0)
                and not tr.get("isImage")
                and not _quality_throttle_due(session_id, sdir)
            ):
                # Do not skip if coverage funnel recording is explicitly requested
                if os.environ.get("TOKEN_OPTIMIZER_COVERAGE_FUNNEL", "").strip() == "1":
                    return False
                return True
    return False
