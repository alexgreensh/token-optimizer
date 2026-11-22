#!/usr/bin/env python3
"""Middle-preserving cap for oversized Task/Agent subagent tool results.

When a Task/Agent tool result exceeds SUBAGENT_CAP_MIN_CHARS (~12KB), keeps:
  1. The opening section (up to HEAD_MAX_CHARS / HEAD_MAX_LINES),
  2. Every line in the middle containing error/fail/warning/TODO/FIXME or a
     file:line reference,
  3. The concluding section (from a Conclusions/Summary heading if present,
     otherwise the tail up to TAIL_MAX_CHARS / TAIL_MAX_LINES),
  4. An explicit ``expand`` archive pointer to the full raw result on disk.

Safety invariants:
- Fail-open: any exception or unrecognized tool_response shape returns None so
  the full original output stands.
- Archive-first: the caller must verify ``archive_entry_exists`` before emitting
  ``updatedToolOutput``.
- Small-savings passthrough: if the capped output does not save at least 15%,
  returns None.
- Shape preservation: preserves the incoming ``tool_response`` shape (str or
  dict with ``content``/``result``/``output``/``text``).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

SUBAGENT_CAP_MIN_CHARS = 12 * 1024  # 12 KB
MIN_LINES = 15
HEAD_MAX_CHARS = 2500
HEAD_MAX_LINES = 35
TAIL_MAX_CHARS = 1500
TAIL_MAX_LINES = 25
MIN_SAVINGS_RATIO = 0.15

_IMPORTANT_LINE_RE = re.compile(
    r"(?:"
    r"\b(?:error|errors|err|fail|failed|failing|failure|failures|exception|"
    r"traceback|fatal|panic|warning|warn|todo|fixme|xxx|hack)\b"
    r"|"
    r"(?:[\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|swift|c|cc|cpp|"
    r"h|hpp|rb|php|sh|bash|zsh|md|json|yaml|yml|toml|sql|css|scss|html|vue|"
    r"svelte|cs|ex|exs|tf):\d+)"
    r"|"
    r"\[CREDENTIAL REDACTED"
    r")",
    re.IGNORECASE,
)

_CONCLUSION_HEADING_RE = re.compile(
    r"^\s*#{1,4}\s*(?:conclusions?|summary|results?|key findings|findings|"
    r"next steps|recommendations?|verdict|tl;dr|final status|done)\b",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(r"^\s*#{1,3}\s+\S")


def _select_head_end(lines: list[str]) -> int:
    """Return exclusive index of the opening section."""
    chars = 0
    end = 0
    for idx, line in enumerate(lines):
        if idx >= HEAD_MAX_LINES or (chars + len(line) + 1 > HEAD_MAX_CHARS and idx >= 10):
            break
        # Stop cleanly before a second top-level section once we have a solid opening.
        if idx >= 12 and chars >= 600 and _HEADING_RE.match(line):
            break
        chars += len(line) + 1
        end = idx + 1
    return max(1, end)


def _select_tail_start(lines: list[str], head_end: int) -> int:
    """Return inclusive index of the concluding section."""
    n = len(lines)
    # Prefer a conclusion/summary heading in the last ~40% of the output if it fits.
    search_from = max(head_end, int(n * 0.60))
    for idx in range(n - 1, search_from - 1, -1):
        if _CONCLUSION_HEADING_RE.match(lines[idx]):
            section_text = "\n".join(lines[idx:])
            if len(section_text) <= TAIL_MAX_CHARS * 2:
                return idx
            break

    chars = 0
    start = n
    count = 0
    for idx in range(n - 1, head_end - 1, -1):
        line = lines[idx]
        if count >= TAIL_MAX_LINES or (chars + len(line) + 1 > TAIL_MAX_CHARS and count >= 5):
            break
        chars += len(line) + 1
        start = idx
        count += 1
    return start


def cap_subagent_result(
    text: str,
    *,
    expand_instruction: str = "",
    original_chars: Optional[int] = None,
) -> Optional[str]:
    """Cap an oversized subagent result while preserving head, conclusions, and key lines.

    Returns the capped string, or None when the input should pass through untouched.
    """
    try:
        if not text or len(text) <= SUBAGENT_CAP_MIN_CHARS:
            return None
        lines = text.splitlines()
        if len(lines) < MIN_LINES:
            return None

        head_end = _select_head_end(lines)
        tail_start = _select_tail_start(lines, head_end)
        if tail_start <= head_end:
            return None

        head_lines = lines[:head_end]
        middle_lines = lines[head_end:tail_start]
        tail_lines = lines[tail_start:]

        kept_middle = [ln for ln in middle_lines if _IMPORTANT_LINE_RE.search(ln)]
        omitted_count = len(middle_lines) - len(kept_middle)
        if omitted_count <= 0:
            return None

        parts: list[str] = list(head_lines)
        parts.append("")
        if kept_middle:
            parts.append(
                f"[... {omitted_count} middle lines archived; "
                f"{len(kept_middle)} error/fail/TODO/file:line lines preserved below ...]"
            )
            parts.extend(kept_middle)
            parts.append("[... end of preserved middle lines ...]")
        else:
            parts.append(f"[... {omitted_count} middle lines archived ...]")
        parts.append("")
        parts.extend(tail_lines)

        orig = original_chars if original_chars is not None else len(text)
        body = "\n".join(parts)
        pointer_lines = [
            f"\n\n[Subagent result capped ({orig:,} chars archived) — full output saved to disk, not lost."
        ]
        if expand_instruction:
            pointer_lines.append(expand_instruction)
        pointer_lines.append("]")
        capped = body + "\n".join(pointer_lines)

        if len(capped) >= len(text) * (1.0 - MIN_SAVINGS_RATIO):
            return None
        return capped
    except Exception:
        return None


def build_updated_tool_output(raw_response: Any, replacement_text: str) -> Any:
    """Wrap replacement_text in the exact structural shape of raw_response, or None."""
    if isinstance(raw_response, str):
        return replacement_text
    if isinstance(raw_response, dict):
        content = raw_response.get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            for item in content
        ):
            updated = dict(raw_response)
            updated["content"] = [{"type": "text", "text": replacement_text}]
            return updated
        for key in ("content", "result", "output", "text"):
            if isinstance(raw_response.get(key), str):
                updated = dict(raw_response)
                updated[key] = replacement_text
                return updated
        return None
    return None


def emit_if_capped(
    *,
    tool_name: str,
    raw_response: Any,
    safe_response: str,
    original_char_count: int,
    tool_use_id: str,
    raw_tool_use_id: str,
    session_id: str,
    entry_exists: bool,
    expand_instruction: str,
    chars_per_token: float,
    trends_db: Any,
    transcript_path: Any,
    log_savings_fn: Any,
) -> bool:
    """Cap and emit PostToolUse updatedToolOutput for an oversized Agent/Task result.

    Returns True if an updatedToolOutput envelope was printed, else False.
    Fail-open: any exception returns False without printing.
    """
    try:
        if os.environ.get("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", "").strip():
            return False
        if not entry_exists or original_char_count <= SUBAGENT_CAP_MIN_CHARS:
            return False

        capped = cap_subagent_result(
            safe_response,
            expand_instruction=expand_instruction,
            original_chars=original_char_count,
        )
        if not capped:
            return False

        updated_output = build_updated_tool_output(raw_response, capped)
        if updated_output is None:
            return False

        orig_tokens = int(original_char_count / chars_per_token)
        new_tokens = int(len(capped) / chars_per_token)
        tokens_saved = max(0, orig_tokens - new_tokens)
        if tokens_saved <= 0:
            return False

        detail = (
            f"capped {tool_name} ({original_char_count:,} chars -> {len(capped):,} chars)"
        )
        log_savings_fn("tool_archive", tokens_saved, session_id=session_id, detail=detail)
        try:
            import archive_verify

            archive_verify.verify(trends_db, session_id)
            archive_verify.record(
                trends_db,
                session_id,
                raw_tool_use_id,
                tool_name,
                original_char_count,
                len(capped),
                transcript_path,
                detail,
            )
        except Exception:
            pass

        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated_output,
            }
        }
        print(json.dumps(envelope))
        return True
    except Exception:
        return False
