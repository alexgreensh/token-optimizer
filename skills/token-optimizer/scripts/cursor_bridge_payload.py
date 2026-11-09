"""stdin payload decoding for the Cursor hook bridge.

Split out of cursor_hook_bridge.py under the repo's 1,000-line file-size
ratchet (scripts/check_file_size.py). Pure parsing only: no I/O beyond the
payload text, no patched seams. cursor_hook_bridge re-imports these names, so
bridge.decode_payload and friends keep working.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_MAX_STDIN_BYTES = 4 * 1024 * 1024  # refuse absurd payloads (amplification)
_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_session_id(sid):
    """Strip everything but [A-Za-z0-9_-] so a payload id can never traverse.

    Mirrors measure.sanitize_session_id / copilot_hook_bridge: a conversation_id
    like "../../../etc/passwd" would otherwise let _atomic_write_json / the lock
    path escape the data dir.
    """
    if not sid:
        return "unknown"
    cleaned = _SESSION_ID_RE.sub("", sid)[:64]
    return cleaned if len(cleaned) >= 6 else "unknown"


def _parse_tool_input(raw):
    """tool_input as a dict, or as a JSON-encoded string -> dict; {} otherwise."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError, RecursionError):
            pass
    return {}


def decode_payload(payload):
    """Normalize a Cursor hook payload into a flat, safe dict.

    ``conversation_id`` wins over ``session_id`` (sessionStart carries only
    session_id; the tool events carry conversation_id). ``workspace_roots`` is
    the list Cursor exports; ``cwd`` is capped at 1024 chars because it is
    rewritten into the tally on every tool call and into restore-context.
    Missing fields default to empty values so callers never KeyError.
    """
    out = {
        "tool_name": "",
        "tool_args": {},
        "conversation_id": "",
        "hook_event_name": "",
        "cwd": "",
        "cursor_version": "",
        "workspace_roots": [],
        "transcript_path": None,
        "model": None,
        "timestamp": None,
        # preCompact numbers (0/0.0 when absent or the payload is not a dict)
        "context_tokens": 0,
        "context_window_size": 0,
        "context_usage_percent": 0.0,
        "trigger": "",
        # U4 correlation/output fields (CLI bundle 2026.11.05 evidence)
        "generation_id": "",
        "agent_id": "",
        "cache_conversation_id": "",
        "tool_use_id": "",
        "parent_tool_call_id": "",
        "tool_output": "",
        "duration": 0.0,
    }
    if not isinstance(payload, dict):
        return out

    tool_name = payload.get("tool_name", payload.get("toolName", ""))
    if isinstance(tool_name, str):
        out["tool_name"] = tool_name.strip()
    out["tool_args"] = _parse_tool_input(
        payload.get("tool_input", payload.get("toolInput", {}))
    )

    sid = payload.get("conversation_id", payload.get("session_id", ""))
    if isinstance(sid, str):
        out["conversation_id"] = _sanitize_session_id(sid)
        out["cache_conversation_id"] = sid if len(sid) <= 1024 else ""

    event = payload.get("hook_event_name", "")
    if isinstance(event, str):
        out["hook_event_name"] = event.strip()

    cwd = payload.get("cwd", "")
    if isinstance(cwd, str):
        out["cwd"] = cwd[:1024]

    version = payload.get("cursor_version", "")
    if isinstance(version, str):
        out["cursor_version"] = version[:64]

    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        out["workspace_roots"] = [
            str(r)[:1024] for r in roots if isinstance(r, str)
        ][:8]

    tp = payload.get("transcript_path")
    if isinstance(tp, str):
        out["transcript_path"] = tp[:1024]

    agent = payload.get("agent_id") or payload.get("subagent_id")
    if isinstance(agent, str) and 0 < len(agent) <= 128:
        out["agent_id"] = agent
    for key in ("generation_id", "tool_use_id", "parent_tool_call_id"):
        v = payload.get(key)
        if isinstance(v, str):
            out[key] = v[:128]
    to = payload.get("tool_output")
    if isinstance(to, str):
        out["tool_output"] = to[:262144]
    try:
        out["duration"] = float(payload.get("duration") or 0.0)
    except (TypeError, ValueError):
        out["duration"] = 0.0

    model = payload.get("model")
    if isinstance(model, str):
        out["model"] = model[:128]

    out["timestamp"] = payload.get("timestamp")

    for key in ("context_tokens", "context_window_size"):
        try:
            out[key] = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    try:
        out["context_usage_percent"] = float(payload.get("context_usage_percent") or 0.0)
    except (TypeError, ValueError):
        out["context_usage_percent"] = 0.0
    trigger = payload.get("trigger")
    if isinstance(trigger, str):
        out["trigger"] = trigger
    return out


def _read_stdin_payload():
    """Read and decode the Cursor hook payload from stdin. None on any failure."""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        return None
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # RecursionError: a deeply nested payload ('[' * 2000 ...) must never
        # escape the fail-open envelope and break always-exit-0 (G0).
        return None
    if not isinstance(payload, dict):
        return None
    return payload
