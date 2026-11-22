"""Transcript boundary markers that invalidate cached Read context.

Recognizes both full compaction rows (``compact_boundary`` / ``isCompactSummary``)
and silent tool-result eviction that does not fire PreCompact:
- Claude Code microcompaction (``microcompact_boundary`` / ``microcompactMetadata``)
- Anthropic Context Editing API (``context_management.applied_edits`` with a
  ``clear_tool_uses*`` edit that cleared at least one tool use).
"""

from __future__ import annotations

import hashlib
import json

COMPACT_MARKERS = (
    b"compact_boundary",
    b"isCompactSummary",
    b"microcompact_boundary",
    b"microcompactMetadata",
    b"clear_tool_uses",
)


def _has_cleared_tool_uses(row: dict) -> bool:
    """True when a row records an applied clear_tool_uses context edit."""
    candidates = [row.get("context_management")]
    msg = row.get("message")
    if isinstance(msg, dict):
        candidates.append(msg.get("context_management"))
    for cm in candidates:
        if not isinstance(cm, dict):
            continue
        edits = cm.get("applied_edits")
        if not isinstance(edits, list):
            continue
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            etype = str(edit.get("type") or "")
            if not etype.startswith("clear_tool_uses"):
                continue
            cleared_uses = edit.get("cleared_tool_uses")
            cleared_tokens = edit.get("cleared_input_tokens")
            if cleared_uses is not None or cleared_tokens is not None:
                try:
                    u = int(cleared_uses or 0)
                    t = int(cleared_tokens or 0)
                except (TypeError, ValueError):
                    return True
                if u <= 0 and t <= 0:
                    continue
            return True
    return False


def compact_boundary_id(line: bytes, where: str = ""):
    """Stable id of a real compaction or tool-eviction row, or None.

    A tool result that merely quotes marker text (for example reading this
    module's source) returns None because we parse JSON and inspect structured
    fields.
    """
    if not any(m in line for m in COMPACT_MARKERS):
        return None
    try:
        row = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    is_system_boundary = (
        row.get("type") == "system"
        and (
            row.get("subtype") in ("compact_boundary", "microcompact_boundary")
            or "microcompactMetadata" in row
        )
    )
    if not (is_system_boundary or row.get("isCompactSummary") is True or _has_cleared_tool_uses(row)):
        return None
    uuid = row.get("uuid")
    if isinstance(uuid, str) and uuid:
        return "u:" + uuid[:80]
    return "h:" + hashlib.sha1(where.encode("utf-8", "replace") + b"\0" + line.strip()).hexdigest()[:20]
