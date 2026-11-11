"""Context-reset epoch guard shared by session_store and read_cache (U2).

A writer that judged a Read BEFORE a context reset (/clear, compaction, or the
subagent-ledger clear) but commits its ``file_reads`` upsert AFTER the reset's
transaction would resurrect a row claiming a file is in context when the model
no longer holds it. ``reset_context_state`` / ``clear_file_entries`` bump
``session_meta.reset_epoch`` inside their own transaction; guarded writers pass
the epoch they snapshotted and the check-and-insert runs in one IMMEDIATE
transaction. Kept in its own module so the already-over-limit session_store.py
and read_cache.py do not grow (scripts/check_file_size.py).
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional


def bump_reset_epoch(conn) -> None:
    """Increment the reset epoch inside the caller's open transaction."""
    conn.execute(
        "INSERT INTO session_meta (key, value) VALUES ('reset_epoch', '1') "
        "ON CONFLICT(key) DO UPDATE SET "
        "value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def snapshot_reset_epoch(store: Any) -> Any:
    """Epoch to hand to guarded writes, or an invalid sentinel on failure.

    Call AFTER any compaction scan that may itself reset the session. Never
    raises: a store that cannot report an epoch must not resurrect stale state.
    """
    try:
        return store.get_reset_epoch()
    except Exception:
        return "unavailable"


def _reset_replacement_state(entry: dict[str, Any]) -> None:
    entry["last_replacement_fingerprint"] = ""
    entry["last_replacement_type"] = ""
    entry["repeat_replacement_count"] = 0
    entry["consecutive_denials"] = 0
    entry["last_structure_reason"] = ""
    entry["last_structure_confidence"] = 0.0


class ResetEpochMixin:
    """SessionStore methods that read or honor the reset epoch.

    Requires ``self._connect()`` and ``self.get_meta()`` from SessionStore.
    Public signatures are unchanged (``upsert_file_entry`` gains the optional
    ``expected_reset_epoch``).
    """

    def get_reset_epoch(self) -> int:
        """Current context-reset epoch (0 before any reset). U2 race guard."""
        v = self.get_meta("reset_epoch")
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def upsert_file_entry(
        self,
        file_path: str,
        entry: dict[str, Any],
        expected_reset_epoch: Optional[int] = None,
    ) -> bool:
        """Record a file read; returns False when the write was dropped.

        When ``expected_reset_epoch`` is given, the write is conditional: if a
        context reset (/clear, compaction, subagent ledger clear) committed
        after the caller snapshotted the epoch at judgment time, the write is
        dropped instead of resurrecting a pre-clear ``file_reads`` row. A
        resurrected row would make the next Read of a file the model no longer
        holds be judged "unchanged and already in context" and denied or
        outline-served -- silent context loss (U2). The check and the insert
        run in one IMMEDIATE transaction so the reset cannot slip between them.
        ``None`` keeps the legacy unconditional write.
        """
        conn = self._connect()
        if expected_reset_epoch is None:
            self._insert_file_entry(conn, file_path, entry)
            conn.commit()
            return True
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM session_meta WHERE key = 'reset_epoch'"
            ).fetchone()
            # A corrupt stored epoch must DROP, never be normalized to 0:
            # "no reset since the corruption" and "a real reset landed, THEN the
            # value was corrupted" are indistinguishable from the value alone,
            # and normalizing to 0 would accept a stale epoch-0 writer in the
            # second case -- resurrecting the pre-reset row this guard exists
            # to drop. Dropping is fail-open (the read/edit proceeds; only the
            # ledger write is skipped), and the next reset bump CASTs the value
            # to a clean integer, re-engaging the guard. An unparseable
            # expected epoch drops too: a writer that cannot state its epoch
            # has no business writing conditionally.
            try:
                current = int(row[0]) if row and row[0] is not None else 0
                expected = int(expected_reset_epoch)
            except (TypeError, ValueError):
                conn.rollback()
                return False
            if current != expected:
                conn.rollback()
                return False
            self._insert_file_entry(conn, file_path, entry)
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def _insert_file_entry(self, conn, file_path: str, entry: dict[str, Any]) -> None:
        ranges_json = json.dumps(entry.get("ranges_seen", []))
        conn.execute(
            """INSERT INTO file_reads
               (file_path, mtime_ns, size_bytes, ranges_seen, tokens_est,
                read_count, content_hash, last_access,
                last_replacement_fingerprint, last_replacement_type,
                repeat_replacement_count, last_structure_reason,
                last_structure_confidence, last_tool_use_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 mtime_ns=excluded.mtime_ns,
                 size_bytes=excluded.size_bytes,
                 ranges_seen=excluded.ranges_seen,
                 tokens_est=excluded.tokens_est,
                 read_count=excluded.read_count,
                 content_hash=excluded.content_hash,
                 last_access=excluded.last_access,
                 last_replacement_fingerprint=excluded.last_replacement_fingerprint,
                 last_replacement_type=excluded.last_replacement_type,
                 repeat_replacement_count=excluded.repeat_replacement_count,
                 last_structure_reason=excluded.last_structure_reason,
                 last_structure_confidence=excluded.last_structure_confidence,
                 last_tool_use_id=excluded.last_tool_use_id
            """,
            (
                file_path,
                int(entry.get("mtime_ns", 0)),
                int(entry.get("size_bytes", 0)),
                ranges_json,
                int(entry.get("tokens_est", 0)),
                int(entry.get("read_count", 1)),
                entry.get("content_hash"),
                float(entry.get("last_access", time.time())),
                entry.get("last_replacement_fingerprint", ""),
                entry.get("last_replacement_type", ""),
                int(entry.get("repeat_replacement_count", 0)),
                entry.get("last_structure_reason", ""),
                float(entry.get("last_structure_confidence", 0.0)),
                entry.get("last_tool_use_id", ""),
            ),
        )


def refresh_message(file_path: str, written: Any) -> str:
    """Stderr line for a post-edit cache refresh, truthful about the guarded write.

    ``upsert_file_entry`` returns False only when the reset-epoch guard dropped
    the write: a context reset committed after the snapshot, or the stored or
    expected epoch was unreadable. Both mean the ledger row was NOT refreshed.
    Any other value (True, or None from a legacy store) keeps the old wording.
    """
    if written is False:
        return (f"[Read Cache] Refresh skipped (reset-epoch guard dropped the write: "
                f"context reset or unreadable epoch since the edit): {file_path}")
    return f"[Read Cache] Refreshed after edit: {file_path}"
