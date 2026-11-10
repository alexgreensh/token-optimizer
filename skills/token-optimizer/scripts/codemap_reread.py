#!/usr/bin/env python3
"""Safe re-read outline, called from read_cache's PreToolUse Read path.

The existing structure-map path already replaces an unchanged re-read with a
skeleton when its own summary is confident. This module covers the re-reads it
declines (summary over its cap, low confidence, generated-looking, digest-only), using the code
map's outline instead, under stricter rules:

* the earlier Read covered the WHOLE file (offset 0, no limit) and so does this one;
* the file is unchanged (read_cache already matched mtime and size) and there
  has been no compaction since (read_cache clears its ledger on compaction);
* the user's read-cache mode is soft_block (shadow/warn stay hands-off);
* at most ONE outline per file version: Reading it again serves the full file;
* the outline must cost at most a third of the file.

Any error returns None and the Read proceeds untouched (fail open).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

DECLINED_REASONS = frozenset({
    "structure_over_cap", "low_confidence", "fallback_digest",
    "low_signal_structure", "hook_context_cap", "generated_like",
})
MIN_FILE_TOKENS = 1500
MAX_OUTLINE_RATIO = 0.33
# Persisted through the read-cache store's replacement columns, which read_cache
# resets whenever the file changes.
SERVED_TYPE = "codemap_outline"


def note_read(file_path: str) -> None:
    """Measurement hook for every Read (cheap when no lookup is pending)."""
    try:
        import codemap_ledger
        codemap_ledger.note_read(str(file_path))
    except Exception:
        pass


def _full_read_seen(entry: Dict[str, Any]) -> bool:
    for rng in entry.get("ranges_seen") or []:
        try:
            if int(rng[0]) == 0 and int(rng[1]) == 0:
                return True
        except (TypeError, ValueError, IndexError):
            continue
    return False


def _span(line: int, end: int) -> str:
    return f"{line}-{end}" if end and end > line else f"{line}"


def build_outline(file_path: str, max_chars: int) -> Optional[str]:
    """Outline lines for one file from the code map (indexes just that file)."""
    import codemap_index as ci
    p = Path(file_path).resolve()
    root = ci.find_project_root(p)
    idx = ci.CodeIndex.open(root)
    try:
        if not idx.refresh_file(p):
            return None
        rel = idx.rel(p)
        row = idx.file_row(rel) if rel else None
        if row is None or not row[6]:
            return None
        rows = [r for r in idx.outline(rel) if r[2] != "variable"]
        full = [f"{'  ' * q.count('.')}{_span(ln, e)}  {sig}" for _n, q, _k, ln, e, sig in rows]
        if sum(len(x) + 1 for x in full) <= max_chars:
            return "\n".join(full) or None
        # Compact form: top-level definitions with signatures, methods by name@line.
        methods: Dict[str, list] = {}
        for n, q, k, ln, _e, _sig in rows:
            if k == "method" and "." in q:
                methods.setdefault(q.rsplit(".", 1)[0], []).append(f"{n}@{ln}")
        compact = []
        for n, q, k, ln, e, sig in rows:
            if "." in q:
                continue
            text = f"{_span(ln, e)}  {sig}"
            if q in methods:
                ms = methods[q]
                text += f"  [{len(ms)} methods: " + ", ".join(ms[:6]) + (", ..." if len(ms) > 6 else "") + "]"
            compact.append(text)
        out, size = [], 0
        for text in compact:
            if size + len(text) + 1 > max_chars:
                if len(text) > 120 and size + 125 <= max_chars:
                    out.append(text[:110] + " ...]")
                out.append("  ...")
                break
            out.append(text)
            size += len(text) + 1
        return "\n".join(out) or None
    finally:
        idx.close()


def try_outline(
    file_path: str,
    entry: Dict[str, Any],
    *,
    offset: int,
    limit: int,
    tokens_est: int,
    reason_code: str,
    mode: str,
    mtime_ns: int,
    max_context_chars: int,
) -> Optional[Dict[str, Any]]:
    """Return {'reason', 'context', 'outline_tokens'} to deny with, or None to allow."""
    try:
        import codemap_index as ci
        if mode != "soft_block" or offset or limit:
            return None
        if reason_code not in DECLINED_REASONS or tokens_est < MIN_FILE_TOKENS:
            return None
        suffix = Path(file_path).suffix.lower()
        if suffix not in ci.SUPPORTED_SUFFIXES or not _full_read_seen(entry):
            return None
        marker = f"codemap:{int(mtime_ns)}"
        if entry.get("last_replacement_fingerprint") == marker:
            return None  # outline already served for this version: give the full file
        if not ci.reread_outline_enabled():
            return None
        name = os.path.basename(file_path)
        header = (
            f"[Token Optimizer] {name} is unchanged since your full Read earlier in this session "
            "(no compaction since), so its outline is shown instead of a second full copy. "
            "Read with offset/limit for exact lines, Edit it directly, or Read it again for the full file."
        )
        body = build_outline(file_path, max(200, max_context_chars - len(header) - 40))
        if not body:
            return None
        context = f"{header}\n{body}"
        from token_estimate import estimate_tokens
        outline_tokens = int(estimate_tokens(context))
        if outline_tokens > tokens_est * MAX_OUTLINE_RATIO or len(context) > max_context_chars:
            return None
        entry["last_replacement_fingerprint"] = marker
        entry["last_replacement_type"] = SERVED_TYPE
        return {
            "reason": f"{name} is unchanged and already in context; using its code map outline.",
            "context": context,
            "outline_tokens": outline_tokens,
        }
    except Exception:
        return None


def record_served(file_path: str, tokens_est: int, outline_tokens: int, session_id: str) -> None:
    try:
        import codemap_ledger
        codemap_ledger.log_outline_served(file_path, tokens_est, outline_tokens, session_id)
    except Exception:
        pass
