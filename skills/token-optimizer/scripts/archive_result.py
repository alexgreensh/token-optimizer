#!/usr/bin/env python3
"""Token Optimizer - PostToolUse Archive Result (standalone entry point).

Archives large tool results to disk so they survive compaction.
Standalone extraction for minimal startup overhead (~40ms vs ~135ms).

Security hardening:
  - 0o600 permissions on all written files
  - stdin capped at 1MB
  - Archive entries capped at 5MB with truncation marker
  - Session ID sanitized against path traversal
  - tool_use_id validated to alphanumeric + hyphens/underscores

SOURCE OF TRUTH for _sanitize_session_id, _read_stdin_hook_input,
_archive_dir_for_session: measure.py. Keep in sync.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from plugin_env import resolve_snapshot_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4.0
_ARCHIVE_THRESHOLD = 4096       # chars: only archive results >= this size
_ARCHIVE_PREVIEW_SIZE = 1000    # chars: preview included in replacement output
_ARCHIVE_MAX_SIZE = 5_242_880   # 5MB: truncate responses beyond this
_STDIN_MAX_BYTES = 1_048_576    # 1MB: cap stdin reads

# Plugin-data-aware paths (env > installed_plugins.json > legacy)
SNAPSHOT_DIR = resolve_snapshot_dir()


# ---------------------------------------------------------------------------
# Helpers (SOURCE OF TRUTH: measure.py — keep in sync)
# ---------------------------------------------------------------------------

def _sanitize_session_id(sid: str | None) -> str:
    """Sanitize session ID for safe use in filenames. Prevents path traversal."""
    if not sid or not re.match(r'^[a-zA-Z0-9_-]+$', sid):
        return "unknown"
    return sid


def _read_stdin_hook_input(max_bytes: int = _STDIN_MAX_BYTES) -> dict:
    """Read JSON hook input from stdin non-blocking. Returns dict or empty dict.

    Bounds read size to max_bytes. Uses 1MB cap (vs measure.py's 64KB default)
    because PostToolUse payloads include tool_response which can be large.
    Works on Unix; returns empty dict on Windows.
    """
    try:
        import select
        if select.select([sys.stdin], [], [], 0.1)[0]:
            data = sys.stdin.read(max_bytes)
            return json.loads(data) if data else {}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _archive_dir_for_session(session_id: str) -> Path:
    """Return the archive directory for a given session."""
    sid = _sanitize_session_id(session_id)
    return SNAPSHOT_DIR / "tool-archive" / sid


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def archive_result(quiet: bool = False) -> None:
    """PostToolUse hook handler: archive large tool results to disk.

    Reads hook JSON from stdin. If tool_response >= _ARCHIVE_THRESHOLD chars,
    saves the full result to disk and (for MCP tools) outputs a trimmed
    replacement via stdout with updatedMCPToolOutput.

    NO _log_savings_event: SessionEnd `collect` derives savings from manifest.jsonl.
    """
    hook_input = _read_stdin_hook_input()
    if not hook_input:
        return

    tool_name = hook_input.get("tool_name", "")
    tool_use_id = hook_input.get("tool_use_id", "")
    tool_response = hook_input.get("tool_response", "")
    session_id = hook_input.get("session_id", "")

    if not tool_response or len(tool_response) < _ARCHIVE_THRESHOLD:
        return

    if not tool_use_id or not session_id:
        if not quiet:
            print("[Tool Archive] Missing tool_use_id or session_id, skipping.", file=sys.stderr)
        return

    # Sanitize tool_use_id
    if not re.match(r'^[a-zA-Z0-9_-]+$', tool_use_id):
        if not quiet:
            print("[Tool Archive] Invalid tool_use_id, skipping", file=sys.stderr)
        return

    archive_dir = _archive_dir_for_session(session_id)
    archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    original_char_count = len(tool_response)
    truncated = original_char_count > _ARCHIVE_MAX_SIZE

    # Truncate oversized responses
    if truncated:
        tool_response = tool_response[:_ARCHIVE_MAX_SIZE] + (
            f"\n\n[TRUNCATED at 5MB. Original size: {original_char_count} chars]"
        )

    # chars = content size before truncation marker (consistent metric)
    char_count = _ARCHIVE_MAX_SIZE if truncated else original_char_count
    token_est = int(char_count / CHARS_PER_TOKEN)

    # Save full result with 0o600 permissions
    entry_data = {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "chars": char_count,
        "original_chars": original_char_count,
        "tokens_est": token_est,
        "truncated": truncated,
        "timestamp": now.isoformat(),
        "archived_from": "PostToolUse",
        "response": tool_response,
    }
    entry_path = archive_dir / f"{tool_use_id}.json"
    fd = os.open(str(entry_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(entry_data, f)

    # Update manifest (append-only JSONL for crash safety) with 0o600
    manifest_path = archive_dir / "manifest.jsonl"
    manifest_entry = {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "chars": char_count,
        "original_chars": original_char_count,
        "tokens_est": token_est,
        "truncated": truncated,
        "timestamp": now.isoformat(),
        "archived_from": "PostToolUse",
    }

    fd = os.open(str(manifest_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(manifest_entry) + "\n")

    if not quiet:
        print(f"[Tool Archive] Archived {tool_name} result ({char_count:,} chars, ~{token_est:,} tokens): {tool_use_id}", file=sys.stderr)

    # For MCP tools (tool_name contains "__"): output replacement via stdout
    if "__" in tool_name:
        preview = tool_response[:_ARCHIVE_PREVIEW_SIZE]
        if original_char_count > _ARCHIVE_MAX_SIZE:
            replacement = preview + f"\n\n[Full result archived ({original_char_count:,} chars, truncated to 5MB). Use 'expand {tool_use_id}' to retrieve.]"
        else:
            replacement = preview + f"\n\n[Full result archived ({char_count:,} chars). Use 'expand {tool_use_id}' to retrieve.]"
        output = json.dumps({"updatedMCPToolOutput": replacement})
        print(output)


if __name__ == "__main__":
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    archive_result(quiet=quiet)
