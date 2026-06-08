#!/usr/bin/env python3
"""Token Optimizer - standalone compression-event logger.

Hot-path compression paths (read_cache first-read, bash unmatched-output,
structured-data compression) need to record a repriceable compression_events
row WITHOUT importing measure.py (~135ms import blows the <500ms hook budget).

This mirrors the self-contained logging pattern in archive_result.py
(_log_savings_event): own minimal schema, short DB timeout, fail-open, never
raises. It writes to the SAME compression_events table measure.py owns; the
schema here is kept in sync with measure.py's _SCHEMA literal.

Three-tier accounting (KTD5): every event written here declares a `tier` from
{"measured", "estimated", "opportunity"}. Tiers are never summed into one
headline downstream; this module only records the tag.

Phase A discipline (KTD4): every row carries a stable `session_uuid` (the join
key) and a non-null `model`. When the model cannot be resolved cheaply on the
hot path it is stored as "unknown" (non-null, backfillable from session_log via
session_uuid at reprice time) rather than NULL (which reprices inert at 1.0x).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from plugin_env import resolve_snapshot_dir

try:
    from token_estimate import estimate_tokens as _estimate_tokens
except Exception:  # pragma: no cover - fail-open if shared estimator missing
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return len(text.encode("utf-8", errors="replace")) // 4

SNAPSHOT_DIR = resolve_snapshot_dir()
TRENDS_DB = SNAPSHOT_DIR / "trends.db"

_DB_TIMEOUT_SECONDS = 0.05
_DB_BUSY_TIMEOUT_MS = 50

_VALID_TIERS = ("measured", "estimated", "opportunity")

# Kept in sync with measure.py's compression_events _SCHEMA literal.
_COMPRESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS compression_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    session_uuid TEXT,
    feature TEXT NOT NULL,
    command_pattern TEXT,
    original_tokens INTEGER DEFAULT 0,
    compressed_tokens INTEGER DEFAULT 0,
    compression_ratio REAL DEFAULT 0.0,
    quality_preserved INTEGER DEFAULT 1,
    verified INTEGER DEFAULT 0,
    detail TEXT,
    model TEXT,
    tier TEXT
);
"""

_UUID_PAT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_NON_SESSION_IDS = ("unknown", "test-123", "perf_test", "regtest", "demo")


def _extract_session_uuid(session_id: str | None) -> str | None:
    """Return the stable join UUID for a session_id, or None.

    Pure-function mirror of measure._extract_session_uuid (no I/O) so the hot
    path stays fast. A valid Claude session UUID (8-4-4-4-12 hex) is the join
    key; anything else (short agent_ids, test ids, empty) yields None.
    """
    if not session_id or session_id in _NON_SESSION_IDS:
        return None
    if _UUID_PAT.match(session_id):
        return session_id
    return None


def _resolve_model(model: str | None) -> str:
    """Resolve the event-time model cheaply (no JSONL scan).

    Order: explicit arg > env hint > "unknown" sentinel. Never NULL, so the
    repricing path is not forced inert; "unknown" is backfillable via the
    session_uuid join to session_log.
    """
    if model:
        return model
    env_model = os.environ.get("TOKEN_OPTIMIZER_SESSION_MODEL", "").strip()
    if env_model:
        return env_model
    return "unknown"


def _chmod_private(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(str(path), 0o600)
    except Exception:
        pass


def log_compression_event(
    feature: str,
    original_text: str = "",
    compressed_text: str = "",
    session_id: str | None = None,
    command_pattern: str | None = None,
    tier: str | None = None,
    model: str | None = None,
    verified: bool = False,
    quality_preserved: bool = True,
    detail: str | None = None,
    original_tokens: int | None = None,
    compressed_tokens: int | None = None,
) -> None:
    """Write one compression_events row. Fail-open: never raises.

    `tier` must be one of {"measured", "estimated", "opportunity"}; an invalid
    or missing tier is coerced to "estimated" (the conservative middle tier)
    rather than dropped, so a mis-tagged caller still produces a joinable row.

    `original_tokens`/`compressed_tokens` override the text-derived estimate when
    the caller already has accurate counts (e.g. the first-read shadow path holds
    the file's token estimate and the skeleton's `replacement_tokens_est`). This
    avoids re-encoding multi-megabyte content on the hot PreToolUse path.
    """
    conn: sqlite3.Connection | None = None
    try:
        if original_tokens is None:
            original_tokens = _estimate_tokens(original_text)
        if compressed_tokens is None:
            compressed_tokens = _estimate_tokens(compressed_text)
        ratio = 0.0
        if original_tokens > 0:
            ratio = round(1.0 - compressed_tokens / original_tokens, 4)

        safe_tier = tier if tier in _VALID_TIERS else "estimated"
        session_uuid = _extract_session_uuid(session_id)
        resolved_model = _resolve_model(model)

        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(str(TRENDS_DB), timeout=_DB_TIMEOUT_SECONDS)
        conn.execute(f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_COMPRESSION_SCHEMA)
        # Idempotent tier migration in case the table predates the tier column.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(compression_events)").fetchall()}
        if "tier" not in cols:
            conn.execute("ALTER TABLE compression_events ADD COLUMN tier TEXT")
        conn.execute(
            "INSERT INTO compression_events "
            "(timestamp, session_id, session_uuid, feature, command_pattern, original_tokens, "
            "compressed_tokens, compression_ratio, quality_preserved, verified, detail, model, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), session_id, session_uuid, feature, command_pattern,
             original_tokens, compressed_tokens, ratio,
             1 if quality_preserved else 0,
             1 if verified else 0,
             detail, resolved_model, safe_tier),
        )
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            _chmod_private(Path(str(TRENDS_DB) + suffix))
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
