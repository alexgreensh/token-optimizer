#!/usr/bin/env python3
"""Serve a changed-file diff instead of a full re-read (delta mode), for read_cache.

Called from read_cache's PreToolUse Read path once a diff smaller than the file
exists. It refreshes the cache entry, logs the decision and answers with a deny
plus the diff. A deny is not a read the host records, so the entry is marked
"delta_served": read_cache lets the next read of that file through to the host
(#361). Without that, hosts that refuse Edit until a Read succeeds loop.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

from reset_epoch import _reset_replacement_state

DELTA_SERVED = "delta_served"


def delta_read_pending(entry: dict[str, Any], mtime_match: bool, size_match: bool) -> bool:
    """True when the last answer for this file was a delta deny and nothing changed since."""
    return bool(mtime_match and size_match and entry.get("last_replacement_type") == DELTA_SERVED)


def serve_delta(
    *,
    file_path: str,
    entry: dict[str, Any],
    store: Any,
    reset_epoch: Any,
    session_id: str,
    mode: str,
    language: Any,
    offset: int,
    limit: int,
    quiet: bool,
    save_hook_context_enabled: bool,
    current_stat: Any,
    new_hash: str,
    safe_new_content: str,
    delta_text: str,
    delta_stats: dict[str, Any],
    old_tokens: int,
    delta_tokens: int,
    max_cache_bytes: int,
    emit_fn: Callable[..., None],
    log_decision_fn: Callable[..., Any],
    log_savings_fn: Callable[..., Any],
) -> None:
    """Refresh the entry, log, and emit the delta deny."""
    entry["mtime_ns"] = current_stat.st_mtime_ns
    entry["size_bytes"] = current_stat.st_size
    entry["content_hash"] = new_hash
    entry["read_count"] = int(entry.get("read_count", 0) or 0) + 1
    entry["last_access"] = time.time()
    _reset_replacement_state(entry)
    entry["last_replacement_type"] = DELTA_SERVED
    if len(safe_new_content.encode("utf-8", errors="replace")) <= max_cache_bytes:
        store.upsert_cached_content(file_path, safe_new_content, new_hash)
        cached = store.get_cached_content(file_path)
        if not cached or cached.get("content_hash") != new_hash:
            entry["content_hash"] = None
            store.delete_cached_content(file_path)
    else:
        entry["content_hash"] = None
        store.delete_cached_content(file_path)
    store.upsert_file_entry(file_path, entry, expected_reset_epoch=reset_epoch)

    net_saved = max(0, old_tokens - delta_tokens)

    log_decision_fn(
        "block",
        file_path,
        f"delta_read_{entry['read_count']}",
        session_id,
        mode=mode,
        actual_substitution=True,
        eligible=True,
        language=language,
        reason_code="delta_diff",
        offset=offset,
        limit=limit,
        replacement_type="delta",
        file_tokens_est=old_tokens,
        replacement_tokens_est=delta_tokens,
        net_saved_tokens_est=net_saved,
        replacement_fingerprint=new_hash[:16],
        repeat_replacement_count=0,
        save_hook_additional_context_enabled=save_hook_context_enabled,
    )
    log_savings_fn("delta_read", net_saved, session_id,
                       f"{Path(file_path).name}:+{delta_stats['added']}/-{delta_stats['removed']}")
    # Log to compression_events for v5 telemetry
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from measure import _log_compression_event
        _log_compression_event(
            feature="delta_read",
            original_text=" " * (old_tokens * 4),  # proxy
            compressed_text=delta_text,
            session_id=session_id,
            command_pattern=f"Read:{Path(file_path).name}",
            quality_preserved=True,
            verified=True,
            tier="measured",  # realized: reaches headline explicitly
            detail=f"+{delta_stats['added']}/-{delta_stats['removed']}",
        )
    except Exception:
        pass
    if not quiet:
        print(
            f"[Read Cache] Delta mode: {file_path} "
            f"(+{delta_stats['added']}/-{delta_stats['removed']}, saved~{net_saved:,})",
            file=sys.stderr,
        )
    reason = (
        f"{Path(file_path).name} was edited; showing diff "
        f"(+{delta_stats['added']}/-{delta_stats['removed']} lines) "
        f"instead of full re-read. Read the file again if you "
        f"need it in full or the host asks for a fresh read."
    )

    emit_fn("deny", reason, delta_text)
