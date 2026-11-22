"""Transcript boundary markers that invalidate cached Read context.

Recognizes both full compaction rows (``compact_boundary`` / ``isCompactSummary``)
and silent tool-result eviction that does not fire PreCompact:
- Claude Code microcompaction (``type="system"``, ``subtype="microcompact_boundary"``)
- Client-side tool-use cuts (``message.context_management.client_cut_tool_uses > 0``)
- Anthropic Context Editing API (``message.context_management.applied_edits`` with a
  ``clear_tool_uses*`` edit that cleared at least one tool use or input token).
"""

from __future__ import annotations

import hashlib
import json

COMPACT_MARKERS = (
    b"compact_boundary",
    b"isCompactSummary",
    b"microcompact_boundary",
    b"clear_tool_uses",
    b"client_cut_tool_uses",
)


def _nonneg_int(val: object) -> int:
    if val is None or isinstance(val, bool):
        return 0
    try:
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def context_management_totals(row: dict) -> tuple[int, int, int]:
    """Return ``(client_cut_tool_uses, cleared_tool_uses, cleared_input_tokens)``
    from ``row["message"]["context_management"]`` (all >= 0).
    """
    msg = row.get("message")
    if not isinstance(msg, dict):
        return (0, 0, 0)
    cm = msg.get("context_management")
    if not isinstance(cm, dict):
        return (0, 0, 0)
    client_cut = _nonneg_int(cm.get("client_cut_tool_uses"))
    cleared_uses = 0
    cleared_tokens = 0
    edits = cm.get("applied_edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            etype = str(edit.get("type") or "")
            if not etype.startswith("clear_tool_uses"):
                continue
            cleared_uses += _nonneg_int(edit.get("cleared_tool_uses"))
            cleared_tokens += _nonneg_int(edit.get("cleared_input_tokens"))
    return (client_cut, cleared_uses, cleared_tokens)


def compact_boundary_id(line: bytes, where: str = ""):
    """Stable id of a real compaction or tool-eviction row, or None.

    A tool result that merely quotes marker text (for example reading this
    module's source) returns None because we parse JSON and inspect structured
    fields.

    Context-management evictions return ``cm:<client_cut>:<uses>:<tokens>`` so
    callers can compare against a per-path high-water mark and avoid wiping the
    cache on every turn if the API re-reports the same ``applied_edits`` on
    subsequent assistant responses.
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
        and row.get("subtype") in ("compact_boundary", "microcompact_boundary")
    )
    if is_system_boundary or row.get("isCompactSummary") is True:
        uuid = row.get("uuid")
        if isinstance(uuid, str) and uuid:
            return "u:" + uuid[:80]
        return "h:" + hashlib.sha1(where.encode("utf-8", "replace") + b"\0" + line.strip()).hexdigest()[:20]
    client_cut, cleared_uses, cleared_tokens = context_management_totals(row)
    if client_cut > 0 or cleared_uses > 0 or cleared_tokens > 0:
        return f"cm:{client_cut}:{cleared_uses}:{cleared_tokens}"
    return None


def should_invalidate_for_id(
    bid: str,
    seen: list[str],
    new_ids: list[str],
    cm_watermark: list[int],
) -> bool:
    """Decide whether ``bid`` represents a new invalidation event and update state.

    - ``cm:<client_cut>:<uses>:<tokens>`` invalidates only when at least one
      cumulative total strictly exceeds ``cm_watermark``.
    - ``u:*`` / ``h:*`` invalidates once per distinct boundary id and resets
      ``cm_watermark`` so post-compaction tool evictions start fresh.
    """
    if bid.startswith("cm:"):
        parts = bid.split(":")
        if len(parts) != 4:
            return False
        try:
            c, u, t = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            return False
        if c > cm_watermark[0] or u > cm_watermark[1] or t > cm_watermark[2]:
            cm_watermark[0] = max(cm_watermark[0], c)
            cm_watermark[1] = max(cm_watermark[1], u)
            cm_watermark[2] = max(cm_watermark[2], t)
            return True
        return False
    if bid not in seen and bid not in new_ids:
        new_ids.append(bid)
        cm_watermark[0] = 0
        cm_watermark[1] = 0
        cm_watermark[2] = 0
        return True
    return False
