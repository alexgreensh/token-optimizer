#!/usr/bin/env python3
"""Token Optimizer v5.5 - Session Knowledge Store.

Per-session SQLite database for tool output caching, file read tracking,
and command deduplication. Replaces the per-session JSON cache files
with a structured, ACID-safe store.

Exact-match lookups use indexed PRIMARY KEY columns. The archive
(``tool_outputs``) additionally carries an EXTERNAL-CONTENT FTS5 mirror
(``tool_outputs_fts``, ``content='tool_outputs'``) kept in sync by triggers,
so full-text search is a true mirror of the base table (dedup follows the base
PRIMARY KEY, legacy rows backfilled by a one-time ``rebuild``). A bounded
``LIKE`` query is the fallback when SQLite is built without FTS5.

Configuration:
  - WAL mode for concurrent read/write from separate hook processes
  - busy_timeout=50ms: fail-fast under write contention (shadow mode
    accepts dropped writes rather than stalling the hook process)
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
import uuid
from pathlib import Path
from typing import Any, Optional

from plugin_env import resolve_snapshot_dir

SNAPSHOT_DIR = resolve_snapshot_dir()
SESSION_STORE_DIR = SNAPSHOT_DIR / "session-store"

MAX_DB_SIZE_BYTES = 50 * 1024 * 1024  # 50MB cap
CLEANUP_AGE_HOURS = 48

# v2: added tool_outputs lineage columns + the FTS5 archive index, and
# converted that index to an external-content mirror of tool_outputs. The bump
# gates the one-time FTS backfill/rebuild migration in _ensure_fts5_index.
_SCHEMA_VERSION = 2

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
    last_structure_confidence REAL DEFAULT 0.0,
    last_tool_use_id TEXT DEFAULT ''
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
    timestamp REAL NOT NULL,
    source_file_path TEXT,
    language TEXT,
    archived_from TEXT,
    output_text TEXT
);

CREATE TABLE IF NOT EXISTS command_outputs (
    command_hash TEXT PRIMARY KEY,
    command_text TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    output_chars INTEGER NOT NULL,
    compressed_output TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS command_run_streaks (
    command_hash TEXT PRIMARY KEY,
    command_text TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    streak INTEGER NOT NULL,
    nudged_streak INTEGER,
    last_ts REAL NOT NULL,
    fail_streak INTEGER NOT NULL DEFAULT 0,
    fail_nudged_streak INTEGER,
    inline_run_count INTEGER NOT NULL DEFAULT 0,
    inline_nudged_count INTEGER
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

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    tool_bucket TEXT NOT NULL,
    has_error INTEGER NOT NULL DEFAULT 0,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hint_serves (
    file_path TEXT PRIMARY KEY,
    served_at REAL NOT NULL,
    credited INTEGER NOT NULL DEFAULT 0
);

"""


def _is_real_fts_absence(exc: sqlite3.OperationalError) -> bool:
    """True only when the OperationalError means FTS5 is genuinely absent from
    this SQLite build (so it is safe to cache _fts5_available=False for the
    process). A transient ``database is locked`` / ``busy`` returns False and
    must NOT be cached -- it is retried on a later connect."""
    msg = str(exc).lower()
    return (
        "no such module" in msg
        or "unknown tokenizer" in msg
        or "no such tokenizer" in msg
        or "no such fts5" in msg
    )


def _sanitize_session_id(sid: str) -> str:
    # Generate a unique fallback instead of a static "unknown" string.
    # A static fallback would cause all invalid/missing session IDs to share
    # one SQLite database, leaking data across unrelated sessions.
    if not sid or not re.match(r"^[a-zA-Z0-9_-]+$", sid):
        return f"fallback-{uuid.uuid4().hex[:12]}"
    return sid


class SessionStore:
    """Per-session SQLite store for tool output caching."""

    def __init__(
        self,
        session_id: str,
        snapshot_dir: Optional[Path] = None,
        busy_timeout_ms: Optional[int] = None,
    ):
        self.session_id = _sanitize_session_id(session_id)
        # Per-instance busy_timeout. Defaults to the 50ms fail-fast policy
        # documented above (shadow mode prefers dropped writes over stalling a
        # hook process). The compaction clear (read_cache.handle_clear_compacted)
        # opts into a higher value so it waits out a sibling write lock on the
        # same per-session db instead of dying at 50ms and silently no-op'ing
        # (#101 follow-up).
        self._busy_timeout_ms = (
            50 if busy_timeout_ms is None else max(0, int(busy_timeout_ms))
        )
        base = snapshot_dir or SNAPSHOT_DIR
        self._store_dir = base / "session-store"
        self.db_path = self._store_dir / f"{self.session_id}.db"
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Align the connect timeout (seconds) with the PRAGMA busy_timeout (ms)
        # so the schema init AND later writes honor the same lock-wait budget.
        timeout_s = max(0.05, self._busy_timeout_ms / 1000.0)
        self._conn = sqlite3.connect(str(self.db_path), timeout=timeout_s)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Defense-in-depth privacy (the parent dir is already 0700): the DB and
        # its WAL/SHM sidecars hold cached tool output, so restrict them to the
        # owner (0600). sqlite creates them honoring umask (commonly 0644), so
        # tighten explicitly. Best-effort; never break a connect on chmod.
        import os as _os
        for _p in (
            self.db_path,
            self.db_path.with_suffix(".db-wal"),
            self.db_path.with_suffix(".db-shm"),
        ):
            try:
                _os.chmod(_p, 0o600)
            except OSError:
                pass
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn
        if conn is None:
            return
        conn.executescript(_SCHEMA_SQL)
        # Read the PRIOR schema version BEFORE stamping the current one, so the
        # FTS migration can tell a pre-v2 DB (legacy standalone FTS or none)
        # from an already-migrated one. None => brand-new / pre-versioning DB.
        prior_version = self._read_schema_version(conn)
        # U5: idempotent in-place migration for the lineage columns on
        # pre-existing tool_outputs tables (PRAGMA-introspect then ALTER).
        # Runs BEFORE the FTS setup so the external-content mirror + triggers
        # can reference the lineage columns (output_text in particular).
        self._ensure_tool_output_columns(conn)
        # F1b: add last_tool_use_id to file_reads for double-fire idempotency.
        self._ensure_file_reads_columns(conn)
        # Burn nudge: add fail_streak / fail_nudged_streak to command_run_streaks.
        self._ensure_command_streaks_columns(conn)
        # U6/fix-1: probe + setup the external-content FTS5 mirror (LIKE
        # fallback). Backfills legacy rows once, gated on prior_version < 2.
        self._ensure_fts5_index(conn, prior_version)
        # Stamp the current schema version last (INSERT OR REPLACE so the bump
        # actually lands on pre-existing DBs, not just brand-new ones).
        conn.execute(
            "INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, ?)",
            ("_schema_version", str(_SCHEMA_VERSION)),
        )
        conn.commit()

    @staticmethod
    def _read_schema_version(conn: sqlite3.Connection) -> Optional[int]:
        """Return the stored ``_schema_version`` (int) or None when absent/
        unreadable. Never raises."""
        try:
            row = conn.execute(
                "SELECT value FROM session_meta WHERE key = '_schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        if not row:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def _ensure_tool_output_columns(self, conn: sqlite3.Connection) -> None:
        """Add source_file_path/language/archived_from/output_text to
        tool_outputs if absent.

        Mirrors the compression_log tier-migration pattern: PRAGMA table_info
        to introspect, then ALTER TABLE ADD COLUMN for each missing one. Safe
        to call on every connect; idempotent (SQLite ADD COLUMN is a no-op
        once the column exists). Never raises on a corrupt/missing table.
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_outputs)").fetchall()}
        except sqlite3.DatabaseError:
            return
        for col in ("source_file_path", "language", "archived_from", "output_text"):
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE tool_outputs ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

    def _ensure_file_reads_columns(self, conn: sqlite3.Connection) -> None:
        """Add last_tool_use_id to file_reads if absent (F1b idempotency guard).

        Mirrors the tool_outputs migration pattern: PRAGMA-introspect then
        ALTER TABLE ADD COLUMN. Idempotent and never raises on a corrupt table.
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(file_reads)").fetchall()}
        except sqlite3.DatabaseError:
            return
        if "last_tool_use_id" not in cols:
            try:
                conn.execute("ALTER TABLE file_reads ADD COLUMN last_tool_use_id TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

    def _ensure_command_streaks_columns(self, conn: sqlite3.Connection) -> None:
        """Add fail_streak and fail_nudged_streak to command_run_streaks if
        absent (burn nudge). Idempotent ALTER TABLE, same pattern as the
        tool_outputs and file_reads migrations. Never raises.
        """
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(command_run_streaks)"
            ).fetchall()}
        except sqlite3.DatabaseError:
            return
        for col, decl in (
            ("fail_streak", "INTEGER NOT NULL DEFAULT 0"),
            ("fail_nudged_streak", "INTEGER"),
            ("inline_run_count", "INTEGER NOT NULL DEFAULT 0"),
            ("inline_nudged_count", "INTEGER"),
        ):
            if col not in cols:
                try:
                    conn.execute(
                        f"ALTER TABLE command_run_streaks ADD COLUMN {col} {decl}"
                    )
                except sqlite3.OperationalError:
                    pass

    # U6/fix-1: external-content FTS5 mirror over the archive.
    _fts5_available: Optional[bool] = None

    def _ensure_fts5_index(
        self, conn: sqlite3.Connection, prior_version: Optional[int] = None
    ) -> None:
        """Probe FTS5 and set up an EXTERNAL-CONTENT mirror of ``tool_outputs``.

        External content (``content='tool_outputs'``) makes the FTS index a
        true mirror of the base table rather than a separate copy:

          * Dedup follows the base PRIMARY KEY. The AFTER INSERT trigger fires
            only when ``INSERT OR IGNORE`` actually writes a new row, so
            re-inserting the same ``tool_use_id`` (e.g. read_cache's stable
            ``fr_shadow_<sha>`` id on a re-read) never appends a duplicate FTS
            row -- search returns each match exactly once.
          * Search reads content from the base table, so every base row is
            visible, including legacy rows written before FTS existed.
          * A one-time ``'rebuild'`` (gated on a pre-v2 schema version)
            backfills the entire existing archive into the index.

        Availability is cached once per process, but ONLY a genuine absence
        (``no such module`` / ``unknown tokenizer``) disables FTS. A transient
        ``database is locked`` / ``busy`` is left uncached so a later connect
        retries -- a lock must never permanently disable FTS for the process.
        Never raises.
        """
        if SessionStore._fts5_available is False:
            return
        if SessionStore._fts5_available is None:
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
                conn.execute("DROP TABLE IF EXISTS _fts5_probe")
                SessionStore._fts5_available = True
            except sqlite3.OperationalError as exc:
                if _is_real_fts_absence(exc):
                    SessionStore._fts5_available = False
                # transient (locked/busy or unknown): leave None, retry later
                return
        # A pre-v2 DB either has no FTS or the legacy STANDALONE (non-mirror)
        # tool_outputs_fts from the first U6 cut; both must be replaced by the
        # external-content mirror and backfilled once. A brand-new DB
        # (prior_version is None) also takes this path -- rebuild over an empty
        # base is a no-op.
        needs_backfill = prior_version is None or prior_version < 2
        try:
            if needs_backfill:
                self._drop_fts_objects(conn)
            self._create_fts_objects(conn)
            if needs_backfill:
                conn.execute(
                    "INSERT INTO tool_outputs_fts(tool_outputs_fts) VALUES('rebuild')"
                )
        except sqlite3.OperationalError as exc:
            if _is_real_fts_absence(exc):
                SessionStore._fts5_available = False
            # transient: leave availability as-is; retried on the next connect

    @staticmethod
    def _drop_fts_objects(conn: sqlite3.Connection) -> None:
        """Drop the FTS mirror + its sync triggers (idempotent)."""
        for stmt in (
            "DROP TRIGGER IF EXISTS tool_outputs_ai",
            "DROP TRIGGER IF EXISTS tool_outputs_ad",
            "DROP TRIGGER IF EXISTS tool_outputs_au",
            "DROP TABLE IF EXISTS tool_outputs_fts",
        ):
            conn.execute(stmt)

    @staticmethod
    def _create_fts_objects(conn: sqlite3.Connection) -> None:
        """Create the external-content FTS mirror + AFTER INSERT/DELETE/UPDATE
        sync triggers (all IF NOT EXISTS, so safe on every connect)."""
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS tool_outputs_fts USING fts5(
                   tool_use_id UNINDEXED,
                   tool_name,
                   command_or_path,
                   source_file_path,
                   language,
                   archived_from,
                   output_text,
                   content='tool_outputs',
                   content_rowid='rowid',
                   tokenize='porter unicode61'
               )"""
        )
        # AFTER INSERT fires only on a real insert (INSERT OR IGNORE that is
        # ignored fires nothing) -> automatic dedup, no separate guard needed.
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS tool_outputs_ai
               AFTER INSERT ON tool_outputs BEGIN
                 INSERT INTO tool_outputs_fts(
                     rowid, tool_use_id, tool_name, command_or_path,
                     source_file_path, language, archived_from, output_text)
                 VALUES (
                     new.rowid, new.tool_use_id, new.tool_name, new.command_or_path,
                     new.source_file_path, new.language, new.archived_from, new.output_text);
               END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS tool_outputs_ad
               AFTER DELETE ON tool_outputs BEGIN
                 INSERT INTO tool_outputs_fts(
                     tool_outputs_fts, rowid, tool_use_id, tool_name, command_or_path,
                     source_file_path, language, archived_from, output_text)
                 VALUES (
                     'delete', old.rowid, old.tool_use_id, old.tool_name, old.command_or_path,
                     old.source_file_path, old.language, old.archived_from, old.output_text);
               END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS tool_outputs_au
               AFTER UPDATE ON tool_outputs BEGIN
                 INSERT INTO tool_outputs_fts(
                     tool_outputs_fts, rowid, tool_use_id, tool_name, command_or_path,
                     source_file_path, language, archived_from, output_text)
                 VALUES (
                     'delete', old.rowid, old.tool_use_id, old.tool_name, old.command_or_path,
                     old.source_file_path, old.language, old.archived_from, old.output_text);
                 INSERT INTO tool_outputs_fts(
                     rowid, tool_use_id, tool_name, command_or_path,
                     source_file_path, language, archived_from, output_text)
                 VALUES (
                     new.rowid, new.tool_use_id, new.tool_name, new.command_or_path,
                     new.source_file_path, new.language, new.archived_from, new.output_text);
               END"""
        )

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
        source_file_path: Optional[str] = None,
        language: Optional[str] = None,
        archived_from: Optional[str] = None,
        output_text: Optional[str] = None,
    ) -> None:
        if self._is_over_size_cap():
            return
        conn = self._connect()
        conn.execute(
            """INSERT OR IGNORE INTO tool_outputs
               (tool_use_id, tool_name, tool_type, command_or_path,
                output_hash, output_chars, output_tokens_est,
                compressed_preview, timestamp,
                source_file_path, language, archived_from, output_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_use_id, tool_name, tool_type, command_or_path,
                output_hash, output_chars, output_tokens_est,
                compressed_preview, time.time(),
                source_file_path, language, archived_from, output_text,
            ),
        )
        # fix-1: the external-content FTS mirror is maintained by the AFTER
        # INSERT trigger, which fires only when the INSERT OR IGNORE above
        # actually writes a new row. No manual FTS write here -- that was the
        # source of duplicate FTS rows on re-insert of the same tool_use_id.
        conn.commit()

    # ----- tool_outputs search (U6: FTS5 with LIKE fallback) -----

    def search_tool_outputs(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over archived tool outputs Returns rows of (tool_use_id, tool_name, command_or_path,
        source_file_path, language, archived_from, snippet) ranked by
        relevance. Uses FTS5 ``bm25()`` when available; falls back to a
        bounded ``LIKE '%term%'`` query over the lineage + output_text
        columns when FTS5 is not compiled into SQLite. Never raises: an
        empty/invalid query or a missing index returns an empty list.
        """
        if not query or not query.strip():
            return []
        conn = self._connect()
        safe_limit = max(1, min(int(limit or 20), 200))
        if SessionStore._fts5_available:
            try:
                rows = conn.execute(
                    """SELECT t.tool_use_id, t.tool_name, t.command_or_path,
                              t.source_file_path, t.language, t.archived_from,
                              snippet(tool_outputs_fts, 6, '>>', '<<', '...', 16) AS snippet
                       FROM tool_outputs_fts f
                       JOIN tool_outputs t ON t.rowid = f.rowid
                       WHERE tool_outputs_fts MATCH ?
                       ORDER BY bm25(tool_outputs_fts)
                       LIMIT ?""",
                    (query, safe_limit),
                ).fetchall()
                # fix (LOW): the external-content mirror covers every base row,
                # so a successful-but-EMPTY FTS result is authoritative -- there
                # is no match. Return it directly; only fall through to LIKE on
                # an actual OperationalError (e.g. an FTS MATCH syntax error).
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass  # FTS query failed (e.g. MATCH syntax) -> try LIKE
        # LIKE fallback: bounded substring search over lineage + output_text.
        # fix-4: escape LIKE wildcards (% and _) in the user query and declare
        # an ESCAPE char so a literal '%'/'_' matches itself, not any-run.
        try:
            escaped = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            rows = conn.execute(
                r"""SELECT tool_use_id, tool_name, command_or_path,
                          source_file_path, language, archived_from,
                          substr(COALESCE(output_text, compressed_preview, ''), 1, 200) AS snippet
                   FROM tool_outputs
                   WHERE tool_name LIKE ? ESCAPE '\' OR command_or_path LIKE ? ESCAPE '\'
                      OR source_file_path LIKE ? ESCAPE '\' OR language LIKE ? ESCAPE '\'
                      OR archived_from LIKE ? ESCAPE '\' OR output_text LIKE ? ESCAPE '\'
                      OR compressed_preview LIKE ? ESCAPE '\'
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (pattern, pattern, pattern, pattern, pattern, pattern, pattern, safe_limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

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

    # ----- command_run_streaks (thrash guard) -----

    def get_command_streak(self, command_hash: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM command_run_streaks WHERE command_hash = ?", (command_hash,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_command_streak(
        self,
        command_hash: str,
        command_text: str,
        output_hash: str,
        streak: int,
        nudged_streak: Optional[int],
        last_ts: float,
        fail_streak: int = 0,
        fail_nudged_streak: Optional[int] = None,
        inline_run_count: int = 0,
        inline_nudged_count: Optional[int] = None,
    ) -> None:
        if self._is_over_size_cap():
            # Cap hit: delete the stale row so the thrash guard fails open to
            # "no streak" (streak=1, no nudge) rather than re-reading a frozen
            # row that breaks the cooldown invariant.
            try:
                conn = self._connect()
                conn.execute(
                    "DELETE FROM command_run_streaks WHERE command_hash = ?",
                    (command_hash,),
                )
                conn.commit()
            except Exception:
                pass
            return
        conn = self._connect()
        conn.execute(
            """INSERT OR REPLACE INTO command_run_streaks
               (command_hash, command_text, output_hash, streak,
                nudged_streak, last_ts, fail_streak, fail_nudged_streak,
                inline_run_count, inline_nudged_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_hash, command_text[:500], output_hash, streak,
                nudged_streak, last_ts, fail_streak, fail_nudged_streak,
                inline_run_count, inline_nudged_count,
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

    def get_active_read_tokens(
        self, limit: int = 25, min_read_count: int = 2,
    ) -> int:
        """Sum tokens_est of the session's active (repeatedly-read) files.

        This is the working set a checkpoint restore lets a resumed session skip
        re-reading -- the grounded basis for the avoided-reconstruction credit,
        in place of the compressed checkpoint's own byte size.
        """
        conn = self._connect()
        rows = conn.execute(
            """SELECT COALESCE(tokens_est, 0) AS t
               FROM file_reads
               WHERE read_count >= ?
               ORDER BY last_access DESC
               LIMIT ?""",
            (min_read_count, limit),
        ).fetchall()
        return int(sum(int(r["t"] or 0) for r in rows))

    # ----- hint_serves (U-G: per-hint avoided-search measurement) -----

    def record_hint_serve(self, file_paths) -> None:
        """Record that a proactive prior-session hint surfaced these files to
        this session. A later read of one of them is observed evidence the hint
        spared an exploratory search (credited once via claim_hint_follow)."""
        paths = [str(p).strip() for p in (file_paths or []) if str(p or "").strip()]
        if not paths:
            return
        # Defensive cap independent of the call site (which already slices to ~5):
        # a hint never legitimately surfaces dozens of files, so bound the write.
        paths = paths[:25]
        conn = self._connect()
        now = time.time()
        conn.executemany(
            """INSERT INTO hint_serves (file_path, served_at, credited)
               VALUES (?, ?, 0)
               ON CONFLICT(file_path) DO NOTHING""",
            [(p, now) for p in paths],
        )
        conn.commit()

    # Only credit a hint follow when the read happens within this window of the
    # hint being served. Beyond it, a read of the same file is more likely a
    # coincidence than the hint doing its job -- keeps the avoided-search credit
    # causally honest (and conservative).
    HINT_FOLLOW_MAX_AGE_SECONDS = 4 * 60 * 60

    def claim_hint_follow(self, file_path: str, max_age_seconds: float = HINT_FOLLOW_MAX_AGE_SECONDS) -> bool:
        """If file_path was hinted to this session recently and not yet credited,
        mark it credited and return True (caller logs the avoided-search saving
        once). Returns False otherwise. Idempotent: a path is credited at most
        once, and only within max_age_seconds of the serve.

        This runs on every Read hook, so the common case (no matching uncredited
        hint) takes only a cheap indexed SELECT and never acquires a write lock.
        """
        if not file_path:
            return False
        conn = self._connect()
        fresh_after = time.time() - max(0.0, max_age_seconds)
        hit = conn.execute(
            "SELECT 1 FROM hint_serves "
            "WHERE file_path = ? AND credited = 0 AND served_at >= ? LIMIT 1",
            (str(file_path), fresh_after),
        ).fetchone()
        if not hit:
            return False
        cur = conn.execute(
            "UPDATE hint_serves SET credited = 1 "
            "WHERE file_path = ? AND credited = 0 AND served_at >= ?",
            (str(file_path), fresh_after),
        )
        conn.commit()
        return cur.rowcount > 0

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
