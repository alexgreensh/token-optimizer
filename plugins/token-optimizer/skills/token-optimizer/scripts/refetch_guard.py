#!/usr/bin/env python3
"""Token Optimizer - PreToolUse re-fetch guard (standalone entry point).

Self-healing loop-breaker for issue #88. When a large MCP result is archived,
its compressed replacement tells the model to `expand` the saved copy instead of
re-fetching. If the model re-issues the SAME MCP call anyway (identical tool +
arguments), this guard detects the duplicate against the session archive manifest
and DENIES the call, handing the model the exact `expand` command. That converts
an unbounded re-fetch loop (each iteration re-inflating context with a huge
result) into a single cheap, actionable redirect.

Standalone for minimal startup overhead (mirrors archive_result.py / read_cache.py).
Fail-open by construction: any error, missing input, or unreadable manifest ->
emit a neutral PreToolUse response and let the tool call proceed untouched. The
guard can only ever ADD a deny for a proven exact duplicate; it never blocks a
first-time or novel call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from pathlib import Path

_NEUTRAL = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse"}})

try:
    from hook_io import read_stdin_hook_input
    from plugin_env import resolve_snapshot_dir
    from refetch_fingerprint import ARGS_HASH_KEY, expand_command, tool_fingerprint
    from session_store import _sanitize_session_id as sanitize_sid
except Exception:
    # If our own modules can't load, never block a tool call.
    print(_NEUTRAL)
    sys.exit(0)

_STDIN_MAX_BYTES = 1_048_576  # 1MB: tool_input is small; cap defensively.
_MANIFEST_MAX_LINES = 5000    # keep the NEWEST N manifest entries (append-only file).
_DEBUG = os.environ.get("TOKEN_OPTIMIZER_DEBUG", "").strip().lower() not in ("", "0", "false", "no")


def _debug(msg: str) -> None:
    """Opt-in stderr diagnostic (TOKEN_OPTIMIZER_DEBUG=1) so a silent fail-open path
    is observable during incident diagnosis without editing hooks.json. No-op otherwise."""
    if _DEBUG:
        print(f"[refetch_guard] {msg}", file=sys.stderr)


def _emit(permission_decision: str | None = None, reason: str | None = None) -> None:
    payload: dict = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    if permission_decision:
        payload["hookSpecificOutput"]["permissionDecision"] = permission_decision
    if reason:
        payload["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(payload))


def _read_manifest_tail(manifest: Path) -> list:
    """Return up to the last _MANIFEST_MAX_LINES parsed manifest entries (newest last).

    Opens with O_NOFOLLOW to match the writer's symlink hardening (the writer uses it
    in _append_manifest_line; the reader must not be the weaker link). Streams via a
    bounded deque so a huge manifest never loads fully into memory, and — because the
    file is append-only — the tail is exactly the newest entries the guard needs.
    Never raises.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(manifest), flags)
    tail: deque = deque(maxlen=_MANIFEST_MAX_LINES)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                tail.append(line)
    parsed = []
    for line in tail:
        try:
            parsed.append(json.loads(line))
        except Exception:
            continue
    return parsed


def _lookup_archived(session_id: str, tool_name: str, fingerprint: str):
    """Return (tool_use_id, tokens_est) for a prior identical (tool_name, fingerprint),
    else (None, 0).

    Scans only the current session's manifest (a match is inherently recent — manifests
    are TTL-pruned at 48h), newest-first, so the most recent archive of an identical call
    wins and the scan stops early. Never raises.
    """
    try:
        # Gate on the RAW session id: sanitize() turns an empty/invalid id into a random
        # `fallback-<uuid>` that can't match any archive dir, so there is nothing to look up.
        if not session_id or not session_id.strip():
            return None, 0
        sid = sanitize_sid(session_id)
        manifest = resolve_snapshot_dir() / "tool-archive" / sid / "manifest.jsonl"
        if not manifest.is_file() or manifest.is_symlink():
            return None, 0
        for entry in reversed(_read_manifest_tail(manifest)):  # newest first
            if entry.get("tool_name") == tool_name and entry.get(ARGS_HASH_KEY) == fingerprint:
                return entry.get("tool_use_id"), int(entry.get("tokens_est") or 0)
        return None, 0
    except Exception as exc:
        _debug(f"manifest lookup failed ({type(exc).__name__}: {exc}); allowing call")
        return None, 0


def _log_refetch_block(session_id: str, tool_name: str, archived_id: str, saved_tokens: int) -> None:
    """Record a blocked re-fetch as a savings event — a denied duplicate is real tokens
    saved (the model would have re-pulled the full result).

    Lazy import + fail-open: the common allow path never pays for the SQLite dependency,
    and a logging failure can never turn into a wedged tool call. (Headline-savings
    categorization is deliberately left as a separate change to the compression-category
    registry; this row is the persistent metric/audit trail of guard denies.)
    """
    if saved_tokens <= 0:
        return
    try:
        from archive_result import _log_savings_event
        _log_savings_event(
            "tool_archive_refetch_block",
            saved_tokens,
            session_id=session_id or None,
            detail=f"blocked re-fetch of {tool_name} (archived {archived_id})",
        )
    except Exception as exc:
        _debug(f"deny logging failed ({type(exc).__name__}: {exc})")


def refetch_guard() -> None:
    hook_input = read_stdin_hook_input(_STDIN_MAX_BYTES)
    if not hook_input:
        _emit()
        return

    tool_name = hook_input.get("tool_name", "") or ""
    # Only MCP tools loop this way; native tools (Read/Bash/…) are out of scope.
    if "__" not in tool_name:
        _emit()
        return

    fingerprint = tool_fingerprint(tool_name, hook_input.get("tool_input", {}))
    session_id = hook_input.get("session_id", "") or ""
    archived_id, saved_tokens = _lookup_archived(session_id, tool_name, fingerprint)

    if not archived_id or not re.match(r"^[a-zA-Z0-9_-]+$", str(archived_id)):
        _emit()  # no prior identical call — allow.
        return

    _log_refetch_block(session_id, tool_name, archived_id, saved_tokens)
    _debug(f"denied re-fetch of {tool_name}; redirecting to expand {archived_id}")
    reason = (
        f"Token Optimizer: this exact {tool_name} call already ran and its full result is "
        f"archived on disk (id {archived_id}) — re-fetching would re-inflate context with data "
        f"you already have. Do NOT call {tool_name} again. Read the saved result by running this "
        f"in Bash:\n    {expand_command(archived_id)}"
    )
    _emit(permission_decision="deny", reason=reason)


if __name__ == "__main__":
    try:
        refetch_guard()
    except Exception:
        # Absolute fail-open backstop: a broken guard must never wedge tool use.
        try:
            print(_NEUTRAL)
        except Exception:
            pass
