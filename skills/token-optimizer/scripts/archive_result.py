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

import hashlib

from plugin_env import resolve_snapshot_dir
from session_store import SessionStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4.0
_ARCHIVE_THRESHOLD = 4096       # chars: only archive results >= this size
_ARCHIVE_PREVIEW_SIZE = 1500    # chars: preview included in replacement output
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
# Structure-aware MCP output compression
# ---------------------------------------------------------------------------

def _detect_output_type(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped[:100_000])
            return "json"
        except (json.JSONDecodeError, RecursionError):
            pass
    lines = stripped.splitlines()[:50]
    if len(lines) > 5:
        path_like = sum(1 for ln in lines if "/" in ln or "\\" in ln)
        if path_like > len(lines) * 0.6:
            return "paths"
    if len(lines) > 5:
        sep_count = sum(1 for ln in lines if set(ln.strip()) <= set("-=| +") and ln.strip())
        if sep_count >= 1:
            return "table"
    return "text"


def _compress_mcp_preview(text: str, output_type: str) -> str:
    if output_type == "json":
        return _compress_mcp_json(text)
    if output_type == "paths":
        return _compress_mcp_paths(text)
    if output_type == "table":
        return _compress_mcp_table(text)
    return text[:_ARCHIVE_PREVIEW_SIZE]


def _compress_mcp_json(text: str) -> str:
    try:
        data = json.loads(text[:500_000])
    except (json.JSONDecodeError, RecursionError):
        return text[:_ARCHIVE_PREVIEW_SIZE]

    parts: list[str] = []
    if isinstance(data, dict):
        parts.append(f"JSON object ({len(data)} keys):")
        for key in list(data.keys())[:15]:
            val = data[key]
            if isinstance(val, list):
                parts.append(f"  {key}: [{len(val)} items]")
            elif isinstance(val, dict):
                subkeys = list(val.keys())[:5]
                suffix = "..." if len(val) > 5 else ""
                parts.append(f"  {key}: {{{', '.join(subkeys)}{suffix}}}")
            elif isinstance(val, str) and len(val) > 80:
                parts.append(f'  {key}: "{val[:77]}..."')
            else:
                parts.append(f"  {key}: {json.dumps(val)[:80]}")
        if len(data) > 15:
            parts.append(f"  ... ({len(data) - 15} more keys)")
    elif isinstance(data, list):
        parts.append(f"JSON array ({len(data)} items):")
        for item in data[:5]:
            if isinstance(item, dict):
                keys = list(item.keys())[:5]
                suffix = "..." if len(item) > 5 else ""
                parts.append(f"  {{{', '.join(keys)}{suffix}}}")
            else:
                parts.append(f"  {json.dumps(item)[:80]}")
        if len(data) > 5:
            parts.append(f"  ... ({len(data) - 5} more items)")

    result = "\n".join(parts)
    return result[:_ARCHIVE_PREVIEW_SIZE] if len(result) > _ARCHIVE_PREVIEW_SIZE else result


def _compress_mcp_paths(text: str) -> str:
    lines = text.strip().splitlines()
    dirs: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if "/" in stripped:
            dir_name = stripped.rsplit("/", 1)[0] if "/" in stripped else "."
            dirs[dir_name] = dirs.get(dir_name, 0) + 1

    parts = [f"{len(lines)} paths across {len(dirs)} directories:"]
    sorted_dirs = sorted(dirs.items(), key=lambda x: -x[1])
    for dir_name, count in sorted_dirs[:10]:
        parts.append(f"  {dir_name}/ ({count} files)")
    if len(sorted_dirs) > 10:
        parts.append(f"  ... ({len(sorted_dirs) - 10} more directories)")

    result = "\n".join(parts)
    return result[:_ARCHIVE_PREVIEW_SIZE] if len(result) > _ARCHIVE_PREVIEW_SIZE else result


def _compress_mcp_table(text: str) -> str:
    lines = text.strip().splitlines()
    header = lines[:2]
    data = [ln for ln in lines[2:] if ln.strip()]
    result = header + data[:10]
    if len(data) > 10:
        result.append(f"... ({len(data) - 10} more rows, {len(data)} total)")
    return "\n".join(result)[:_ARCHIVE_PREVIEW_SIZE]


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

    try:
        tool_type = "mcp" if "__" in tool_name else tool_name.lower()
        command_or_path = hook_input.get("tool_input", {}).get("command") or hook_input.get("tool_input", {}).get("file_path") or tool_name
        output_hash = hashlib.sha256(tool_response[:10000].encode("utf-8", errors="replace")).hexdigest()[:16]
        store = SessionStore(session_id)
        store.insert_tool_output(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_type=tool_type,
            command_or_path=str(command_or_path)[:500],
            output_hash=output_hash,
            output_chars=char_count,
            output_tokens_est=token_est,
            compressed_preview=tool_response[:1500],
        )
    except Exception:
        pass

    # For MCP tools (tool_name contains "__"): output replacement via stdout
    if "__" in tool_name:
        output_type = _detect_output_type(tool_response)
        preview = _compress_mcp_preview(tool_response, output_type)
        suffix = f" ({output_type})" if output_type != "text" else ""
        if original_char_count > _ARCHIVE_MAX_SIZE:
            replacement = preview + f"\n\n[Full result archived ({original_char_count:,} chars{suffix}, truncated to 5MB).]"
        else:
            replacement = preview + f"\n\n[Full result archived ({char_count:,} chars{suffix}).]"
        output = json.dumps({"updatedMCPToolOutput": replacement})
        print(output)


if __name__ == "__main__":
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    archive_result(quiet=quiet)
