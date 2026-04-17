#!/usr/bin/env python3
"""Token Optimizer v5.5 - Session Knowledge Store.

Per-session SQLite database for tool output caching, file read tracking,
and command deduplication. Replaces the per-session JSON cache files
with a structured, ACID-safe store.

Plain SQLite with indexed columns, no FTS5. All lookups are exact match
(PRIMARY KEY), not keyword search.

Configuration:
  - WAL mode for concurrent read/write from separate hook processes
  - busy_timeout=1000 for write contention under concurrent hooks
  - synchronous=NORMAL (WAL-safe relaxation for performance)
  - 50MB cap per session DB
  - PreToolUse hooks: READ-ONLY queries only
  - PostToolUse hooks: WRITE operations
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from plugin_env import resolve_snapshot_dir

SNAPSHOT_DIR = resolve_snapshot_dir()
SESSION_STORE_DIR = SNAPSHOT_DIR / "session-store"

MAX_DB_SIZE_BYTES = 50 * 1024 * 1024  # 50MB cap
CLEANUP_AGE_HOURS = 48

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_reads (
    file_path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    ranges_seen TEXT NOT NULL DEFAULT '[]',
    tokens_est INTEGER NOT NULL DEFAULT 0,
    read_count INTEGER NOT NULL DEFAULT 1,
    content_hash TEXT,
    last_access REAL NOT NULL,
    last_replacement_fingerprint TEXT DEFAULT '',
    last_replacement_type TEXT DEFAULT '',
    repeat_replacement_count INTEGER DEFAULT 0,
    last_structure_reason TEXT DEFAULT '',
    last_structure_confidence REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS tool_outputs (
    tool_use_id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    tool_type TEXT NOT NULL,
    command_or_path TEXT,
    output_hash TEXT NOT NULL,
    output_chars INTEGER NOT NULL,
    output_tokens_est INTEGER NOT NULL,
    compressed_preview TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS command_outputs (
    command_hash TEXT PRIMARY KEY,
    command_text TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    output_chars INTEGER NOT NULL,
    compressed_output TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_content (
    file_path TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cached_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_intel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    output_chars INTEGER NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _sanitize_session_id(sid: str) -> str:
    if not sid or not re.match(r"^[a-zA-Z0-9_-]+$", sid):
        return "unknown"
    return sid


class SessionStore:
    """Per-session SQLite store for tool output caching."""

    def __init__(self, session_id: str, snapshot_dir: Optional[Path] = None):
        self.session_id = _sanitize_session_id(session_id)
        base = snapshot_dir or SNAPSHOT_DIR
        self._store_dir = base / "session-store"
        self.db_path = self._store_dir / f"{self.session_id}.db"
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn = sqlite3.connect(str(self.db_path), timeout=0.1)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=50")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn
        if conn is None:
            return
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO _meta (key, value) VALUES (?, ?)",
            ("schema_version", str(_SCHEMA_VERSION)),
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    _cap_warned = False

    def _is_over_size_cap(self) -> bool:
        try:
            over = self.db_path.stat().st_size > MAX_DB_SIZE_BYTES
        except OSError:
            return False
        if over and not SessionStore._cap_warned:
            SessionStore._cap_warned = True
            import sys as _sys
            print("[Session Store] 50MB cap reached, new writes paused for this session", file=_sys.stderr)
        return over

    # ----- file_reads -----

    def get_file_entry(self, file_path: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM file_reads WHERE file_path = ?", (file_path,)
        ).fetchone()
        if row is None:
            return None
        entry = dict(row)
        if entry.get("ranges_seen"):
            try:
                entry["ranges_seen"] = json.loads(entry["ranges_seen"])
            except (json.JSONDecodeError, TypeError):
                entry["ranges_seen"] = []
        return entry

    def upsert_file_entry(self, file_path: str, entry: dict[str, Any]) -> None:
        conn = self._connect()
        ranges_json = json.dumps(entry.get("ranges_seen", []))
        conn.execute(
            """INSERT INTO file_reads
               (file_path, mtime_ns, size_bytes, ranges_seen, tokens_est,
                read_count, content_hash, last_access,
                last_replacement_fingerprint, last_replacement_type,
                repeat_replacement_count, last_structure_reason,
                last_structure_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                 last_structure_confidence=excluded.last_structure_confidence
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
            ),
        )
        conn.commit()

    def delete_file_entry(self, file_path: str) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM file_reads WHERE file_path = ?", (file_path,))
        conn.commit()

    def get_all_file_entries(self) -> dict[str, dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM file_reads").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = dict(row)
            if entry.get("ranges_seen"):
                try:
                    entry["ranges_seen"] = json.loads(entry["ranges_seen"])
                except (json.JSONDecodeError, TypeError):
                    entry["ranges_seen"] = []
            result[entry["file_path"]] = entry
        return result

    def clear_file_entries(self) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM file_reads")
        conn.commit()

    # ----- cached_content -----

    def get_cached_content(self, file_path: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM cached_content WHERE file_path = ?", (file_path,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_cached_content(
        self, file_path: str, content: str, content_hash: str,
    ) -> None:
        if self._is_over_size_cap():
            return
        conn = self._connect()
        conn.execute(
            """INSERT INTO cached_content (file_path, content, content_hash, cached_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content=excluded.content,
                 content_hash=excluded.content_hash,
                 cached_at=excluded.cached_at
            """,
            (file_path, content, content_hash, time.time()),
        )
        conn.commit()

    def delete_cached_content(self, file_path: str) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM cached_content WHERE file_path = ?", (file_path,))
        conn.commit()

    # ----- tool_outputs -----

    def insert_tool_output(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_type: str,
        command_or_path: str,
        output_hash: str,
        output_chars: int,
        output_tokens_est: int,
        compressed_preview: Optional[str] = None,
    ) -> None:
        if self._is_over_size_cap():
            return
        conn = self._connect()
        conn.execute(
            """INSERT OR IGNORE INTO tool_outputs
               (tool_use_id, tool_name, tool_type, command_or_path,
                output_hash, output_chars, output_tokens_est,
                compressed_preview, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_use_id, tool_name, tool_type, command_or_path,
                output_hash, output_chars, output_tokens_est,
                compressed_preview, time.time(),
            ),
        )
        conn.commit()

    # ----- command_outputs -----

    def get_command_output(self, command_hash: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM command_outputs WHERE command_hash = ?", (command_hash,)
        ).fetchone()
        return dict(row) if row else None

    def insert_command_output(
        self,
        command_hash: str,
        command_text: str,
        output_hash: str,
        output_chars: int,
        compressed_output: Optional[str] = None,
    ) -> None:
        if self._is_over_size_cap():
            return
        conn = self._connect()
        conn.execute(
            """INSERT OR REPLACE INTO command_outputs
               (command_hash, command_text, output_hash, output_chars,
                compressed_output, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                command_hash, command_text, output_hash, output_chars,
                compressed_output, time.time(),
            ),
        )
        conn.commit()

    # ----- session_meta -----

    def get_meta(self, key: str) -> Optional[str]:
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM session_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()

    # ----- context_intel_events -----

    def insert_intel_event(
        self,
        tool_name: str,
        tool_use_id: str,
        summary: str,
        output_chars: int,
    ) -> None:
        if self._is_over_size_cap():
            return
        conn = self._connect()
        conn.execute(
            """INSERT INTO context_intel_events
               (tool_name, tool_use_id, summary, output_chars, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (tool_name, tool_use_id, summary, output_chars, time.time()),
        )
        conn.commit()

    def get_intel_events(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT tool_name, summary, output_chars, timestamp
               FROM context_intel_events
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ----- queries for dynamic compact instructions -----

    def get_recent_file_reads(
        self, limit: int = 10, min_read_count: int = 2,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT file_path, read_count, last_access
               FROM file_reads
               WHERE read_count >= ?
               ORDER BY last_access DESC
               LIMIT ?""",
            (min_read_count, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_one_time_reads(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT file_path, tokens_est
               FROM file_reads
               WHERE read_count = 1
               ORDER BY last_access ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_high_value_outputs(
        self, min_tokens: int = 500, limit: int = 5,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT tool_name, command_or_path, output_tokens_est
               FROM tool_outputs
               WHERE output_tokens_est >= ?
               ORDER BY output_tokens_est DESC
               LIMIT ?""",
            (min_tokens, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def cleanup_old_stores(max_age_hours: int = CLEANUP_AGE_HOURS) -> int:
    """Delete session store DBs older than max_age_hours. Returns count deleted."""
    if not SESSION_STORE_DIR.exists():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0
    for db_file in SESSION_STORE_DIR.glob("*.db"):
        try:
            if db_file.stat().st_mtime < cutoff:
                db_file.unlink()
                for wal in (db_file.with_suffix(".db-wal"), db_file.with_suffix(".db-shm")):
                    if wal.exists():
                        wal.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted
