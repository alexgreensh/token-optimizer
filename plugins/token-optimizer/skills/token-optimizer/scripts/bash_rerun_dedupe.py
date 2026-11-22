#!/usr/bin/env python3
"""Session-scoped Bash rerun deduplication.

When the same normalized Bash command runs again in a session and its output is
identical or nearly identical to the last run, returns a compact reference
naming the previous run time and line count plus only the changed lines in
unified-diff format. The caller archives the full raw output behind ``expand``.

Safety invariants:
- Exactness: hashes raw output; never diffs against truncated prior text. Any
  doubt (too many changed lines, oversized output, diff cap hit, missing prior)
  returns None (pass through).
- Bounded per-session storage: each stored output is capped at
  MAX_STORED_OUTPUT_CHARS and the command_outputs table is pruned to
  MAX_STORED_COMMANDS most recent rows.
- Credential redaction: command text and output are redacted before storage,
  diffing, or inclusion in the returned reference.
- Short-output floor: outputs shorter than MIN_OUTPUT_CHARS or MIN_OUTPUT_LINES
  are left untouched.
- Fail-open: any exception returns None so original output stands.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

MIN_OUTPUT_CHARS = 200
MIN_OUTPUT_LINES = 15
RECENCY_WINDOW_SECONDS = 3600.0
MAX_STORED_OUTPUT_CHARS = 128 * 1024  # 128 KiB per entry
MAX_STORED_COMMANDS = 100             # max command_outputs rows per session
MAX_CHANGED_RATIO = 0.30              # pass through if >30% of lines changed
MAX_CHANGED_FLOOR = 20                # ...and >20 lines changed
MAX_REF_RATIO = 0.85                  # must save at least 15%


def normalize_command(command: str) -> str:
    """Normalize a Bash command for rerun matching without changing semantics.

    Strips leading/trailing whitespace, normalizes CRLF to LF, and collapses
    runs of unquoted spaces/tabs to a single space while preserving single- and
    double-quoted strings and backslash escapes verbatim. Multiline heredocs or
    unbalanced quotes fall back to ``command.strip()`` so two distinct commands
    never collide.
    """
    if not command:
        return ""
    text = command.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "<<" in text or "\n" in text:
        return text

    out: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    prev_space = False

    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            prev_space = False
            continue
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            prev_space = False
            continue
        if in_double:
            if ch == "\\":
                out.append(ch)
                escaped = True
                prev_space = False
                continue
            out.append(ch)
            if ch == '"':
                in_double = False
            prev_space = False
            continue
        # Outside quotes
        if ch == "\\":
            out.append(ch)
            escaped = True
            prev_space = False
        elif ch == "'":
            out.append(ch)
            in_single = True
            prev_space = False
        elif ch == '"':
            out.append(ch)
            in_double = True
            prev_space = False
        elif ch in (" ", "\t"):
            if not prev_space:
                out.append(" ")
                prev_space = True
        else:
            out.append(ch)
            prev_space = False

    if in_single or in_double or escaped:
        return text
    return "".join(out).strip()


def _prune_command_outputs(store, max_rows: int = MAX_STORED_COMMANDS) -> None:
    """Keep at most ``max_rows`` most recent rows in ``command_outputs``."""
    try:
        conn = store._connect()
        conn.execute(
            """DELETE FROM command_outputs
               WHERE command_hash NOT IN (
                   SELECT command_hash FROM command_outputs
                   ORDER BY timestamp DESC
                   LIMIT ?
               )""",
            (max(1, int(max_rows)),),
        )
        conn.commit()
    except Exception:
        pass


def _format_prev_time(ts: float) -> str:
    """Format a POSIX timestamp as local HH:MM:SS for the rerun banner."""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (OSError, OverflowError, ValueError, TypeError):
        return "earlier"


def dedupe_rerun(
    command: str,
    output: str,
    *,
    session_id: str,
    store_id: str,
    tool_use_id: str = "",
    legacy_twin_seconds: float = 2.0,
) -> Optional[str]:
    """Return a compact rerun reference or unified diff, else None.

    Records the current run (redacted and bounded) in the session store before
    returning so subsequent reruns always diff against the latest full output.
    """
    try:
        if not session_id or not output:
            return None
        if len(output) < MIN_OUTPUT_CHARS:
            return None
        n_lines = len(output.splitlines())
        if n_lines < MIN_OUTPUT_LINES:
            return None

        from session_store import SessionStore
        from delta_diff import content_hash, compute_delta
        from credential_patterns import redact_credentials

        norm_cmd = normalize_command(command)
        if not norm_cmd:
            return None
        cmd_h = content_hash(norm_cmd)
        raw_cmd_h = content_hash(command.strip())
        out_h = content_hash(output)

        safe_output = redact_credentials(output)
        safe_command = redact_credentials(command, command=True)[:500]
        # Bound stored text: if output exceeds MAX_STORED_OUTPUT_CHARS, keep
        # the exact SHA-256 hash (so identical reruns still match) but store
        # empty text so we never diff against truncated content.
        stored_output = safe_output if len(safe_output) <= MAX_STORED_OUTPUT_CHARS else ""

        store = SessionStore(store_id)
        try:
            prior = store.get_command_output(cmd_h)
            if prior is None and raw_cmd_h != cmd_h:
                prior = store.get_command_output(raw_cmd_h)

            store.insert_command_output(
                cmd_h,
                safe_command,
                out_h,
                len(output),
                stored_output,
                tool_use_id=tool_use_id,
            )
            _prune_command_outputs(store, MAX_STORED_COMMANDS)

            if not prior:
                return None

            # Twin-hook guard: same tool_use_id is a duplicate hook firing on
            # the same call, not a rerun.
            if tool_use_id and prior.get("last_tool_use_id") == tool_use_id:
                return None

            now = time.time()
            prior_ts = float(prior.get("timestamp") or 0.0)

            # Pre-fix twin guard (no tool_use_id recorded).
            if (
                tool_use_id
                and not prior.get("last_tool_use_id")
                and prior.get("output_hash") == out_h
                and now - prior_ts < legacy_twin_seconds
            ):
                return None

            # Recency guard: only reference runs from the last hour.
            if now - prior_ts > RECENCY_WINDOW_SECONDS:
                return None

            label = safe_command.strip()[:60]
            prev_time = _format_prev_time(prior_ts)

            if prior.get("output_hash") == out_h:
                ref = (
                    f"[Token Optimizer: `{label}`: identical to previous run "
                    f"at {prev_time}, {n_lines} lines; unchanged.]"
                )
            else:
                prior_text = prior.get("compressed_output") or ""
                # Exactness guard: never diff when either side exceeded the
                # storage cap and was not stored in full.
                if not prior_text or not stored_output:
                    return None
                delta, stats = compute_delta(prior_text, safe_output, filename=label)
                if not delta or not stats:
                    return None
                changed = int(stats.get("changed_lines", 0))
                if changed > max(MAX_CHANGED_FLOOR, int(n_lines * MAX_CHANGED_RATIO)):
                    return None
                ref = (
                    f"[Token Optimizer: `{label}`: same as previous run "
                    f"at {prev_time}, {n_lines} lines; changed lines below (except):]\n"
                    f"{delta}"
                )

            return ref if len(ref) < len(output) * MAX_REF_RATIO else None
        finally:
            store.close()
    except Exception as exc:
        try:
            sys.stderr.write(
                f"[Token Optimizer] crossturn dedup failed: {type(exc).__name__}\n"
            )
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        return None
