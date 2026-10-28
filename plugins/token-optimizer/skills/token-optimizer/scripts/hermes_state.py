#!/usr/bin/env python3
"""Read-only reader for Hermes agent's runtime SQLite state.

NousResearch/hermes-agent stores per-session token + cost data in a clean
SQLite schema at ``~/.hermes/state.db``.  This module reads that database to
power cost pass-through, quality scoring, and dashboard rollup in Token
Optimizer's Hermes adapter.

Design constraints (mirroring codex_state.py):

- **Pure stdlib only.**  No import of Hermes modules (``agent.*``, etc.).
  Hermes's own ``hermes_state.py`` transitively imports ``agent.memory_manager``
  and other heavy dependencies; coupling TO to those would break standalone CLI
  and fixture tests and couple us to Hermes internals.  We open the DB
  read-only with plain ``sqlite3``.
- **Strictly read-only.**  Opens with ``file:...?mode=ro`` URI + sets
  ``PRAGMA query_only = ON`` as defence-in-depth.  Sets a short busy-timeout
  so a locked DB degrades gracefully instead of hanging.
- **Tolerates schema drift.**  Missing columns fall back to safe defaults; a
  missing table returns an empty/zero result; a missing DB file returns an
  empty result (no exception).  Every public function degrades rather than
  raises so hook callbacks that call us never crash the host agent.
- **Minimal open window.**  Each function opens, queries, closes.  No
  long-lived connections that would defer WAL checkpoints inside Hermes.

Path resolution: ``~/.hermes/state.db`` by default; override via
``HERMES_HOME`` env var (mirrors how Hermes itself uses
``hermes_constants.get_hermes_home()``).  Unsafe/relative values are rejected
with a warning, matching the ``codex_home()`` / ``_safe_home_from_env``
pattern in ``runtime_env.py``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HERMES_HOME_ENV = "HERMES_HOME"
_DEFAULT_HERMES_DIR = ".hermes"
_DB_NAME = "state.db"
_BUSY_TIMEOUT_SECONDS = 0.25
_MAX_ROWS = 2000

# Columns we select from sessions. Ordered to match ``_SESSION_DEFAULTS``.
_SESSION_COLUMNS = (
    "id",
    "model",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "cost_source",
    "archived",
    "title",
    "cwd",
    "billing_provider",
    "billing_mode",
    "compression_failure_cooldown_until",
    "compression_failure_error",
    "compression_fallback_streak",
    "compression_ineffective_count",
    "compression_recovery_deadline",
)

_SESSION_DEFAULTS: dict[str, Any] = {
    "id": "",
    "model": "unknown",
    "started_at": None,
    "ended_at": None,
    "end_reason": None,
    "message_count": 0,
    "tool_call_count": 0,
    "api_call_count": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "estimated_cost_usd": None,
    "actual_cost_usd": None,
    "cost_status": "unknown",
    "cost_source": None,
    "archived": 0,
    "title": "",
    "cwd": None,
    "billing_provider": None,
    "billing_mode": None,
    "compression_failure_cooldown_until": None,
    "compression_failure_error": None,
    "compression_fallback_streak": 0,
    "compression_ineffective_count": 0,
    "compression_recovery_deadline": None,
}

# Whitelist of tables this module ever inspects.
_ALLOWED_TABLES = frozenset({"sessions", "messages", "schema_version", "state_meta"})


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _home_root() -> Path:
    return Path.home().resolve(strict=False)


def _is_safe_home_dir(path: Path) -> bool:
    """True when path is a non-symlink directory under the user's home."""
    try:
        if not path.is_absolute():
            return False
        resolved = path.resolve(strict=False)
        home = _home_root()
        if resolved == home or not resolved.is_relative_to(home):
            return False
        if path.exists():
            return path.is_dir() and not path.is_symlink()
        return False
    except (OSError, ValueError):
        return False


# Default-off explicit override: on containerized deploys (Docker/Umbrel)
# the runtime home may be a PARENT of $HOME (e.g. HERMES_HOME=/opt/data with
# HOME=/opt/data/home). This drops ONLY the under-$HOME containment policy;
# every other safety check (absolute, resolve, exists, is_dir, not symlink)
# is still enforced by _is_safe_home_dir_relaxed below. Mirrors runtime_env.py
# so the read-only state.db reader agrees with the installer.
_UNSAFE_RUNTIME_HOME_OVERRIDE_ENV = "TOKEN_OPTIMIZER_ALLOW_UNSAFE_RUNTIME_HOME"


def _truthy_env(env_var: str) -> bool:
    """Parse a truthy env var (1/true/yes/on, case-insensitive). Mirrors the
    parser style in measure.py/runtime_env.py without importing either (keeps
    hermes_state dependency-light)."""
    val = os.environ.get(env_var)
    return isinstance(val, str) and val.strip().lower() in ("1", "true", "yes", "on")


def _is_safe_home_dir_relaxed(path: Path) -> bool:
    """Like _is_safe_home_dir but DROPS the under-$HOME containment check.

    Keeps every other guard: absolute path, resolve(strict=False), must exist,
    must be a real dir, must NOT be a symlink. Used only when the explicit
    TOKEN_OPTIMIZER_ALLOW_UNSAFE_RUNTIME_HOME override is truthy, for
    containerized deploys where the runtime home is a parent of $HOME.
    """
    try:
        if not path.is_absolute():
            return False
        path.resolve(strict=False)
        if path.exists():
            return path.is_dir() and not path.is_symlink()
        return False
    except (OSError, ValueError):
        return False


def hermes_home(home: str | os.PathLike | None = None) -> Path:
    """Return the Hermes data directory, safely honoring HERMES_HOME when valid.

    ``home`` names a profile home explicitly (the Hermes plugin passes the
    profile whose hook is running: in a multiplexed gateway os.environ holds
    the launch profile's HERMES_HOME, not the active one). It goes through the
    same safety checks as the env var.
    """
    raw = str(home).strip() if home else os.environ.get(_HERMES_HOME_ENV, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if _is_safe_home_dir(candidate):
            return candidate.resolve(strict=False)
        # Override: on containerized deploys (Docker/Umbrel) the runtime
        # home may be a PARENT of $HOME. When the explicit override env var is
        # truthy, re-validate with the under-$HOME requirement DROPPED but all
        # other checks kept (absolute, resolve, exists, is_dir, not symlink).
        if _truthy_env(_UNSAFE_RUNTIME_HOME_OVERRIDE_ENV) and _is_safe_home_dir_relaxed(candidate):
            return candidate.resolve(strict=False)
        logger.warning(
            "[Token Optimizer] HERMES_HOME=%r rejected (not a safe directory). Using default.",
            raw,
        )
    return Path.home() / _DEFAULT_HERMES_DIR


def state_db_path(home: str | os.PathLike | None = None) -> Path:
    """Return the canonical path to Hermes's state.db (of ``home`` when given)."""
    return hermes_home(home) / _DB_NAME


# ---------------------------------------------------------------------------
# Internal connection helpers
# ---------------------------------------------------------------------------

@contextmanager
def _ro_connect(path: Path, *, immutable: bool = True) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only with a short busy-timeout; always close.

    Never writes, never checkpoints, never attaches.  Uses PRAGMA query_only
    as defence-in-depth so even future code changes cannot write via this
    connection.

    ``immutable=True`` (the default) reads the checkpointed main file only.
    Pass ``immutable=False`` for a WAL-visible view: plain ``mode=ro`` reads
    committed -wal pages, which is what a probe of the *live* session needs —
    with immutable=1 the read silently misses writes that have not checkpointed
    yet. The trade-off is that non-immutable opens may consult/create the
    -shm/-wal side files, so it is opt-in per call site.
    """
    # Q3: immutable=1 prevents SQLite from creating WAL/SHM side-effect files
    # when opening a read-only view of a DB owned by another process (Hermes).
    # H-2: build the file: URI via Path.as_uri() rather than string
    # interpolation. With uri=True, characters like ?, #, % and spaces in the
    # path are URI syntax, so a path containing '?' would start the query
    # string early and could drop or override mode=ro (opening read-write).
    # N-4 (M-3 parity): resolve() first so a relative path doesn't raise an
    # uncaught ValueError past callers that only catch (sqlite3.Error, OSError).
    db_uri = Path(path).resolve().as_uri()
    uri = f"{db_uri}?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=_BUSY_TIMEOUT_SECONDS,
    )
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names of ``table``, or empty set when it does not exist.

    Guards every read against a missing table or renamed column on a future
    Hermes schema version.  ``PRAGMA table_info`` returns no rows for an
    unknown table, so this doubles as an existence check.
    """
    if table not in _ALLOWED_TABLES:
        return set()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row: sqlite3.Row, available_cols: set[str]) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, falling back to defaults for missing columns."""
    result: dict[str, Any] = dict(_SESSION_DEFAULTS)
    for col in _SESSION_COLUMNS:
        if col in available_cols:
            try:
                val = row[col]
            except (IndexError, KeyError):
                val = _SESSION_DEFAULTS.get(col)
            result[col] = val if val is not None else _SESSION_DEFAULTS.get(col)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_session(
    session_id: str, *, live: bool = False, home: str | os.PathLike | None = None,
) -> dict[str, Any] | None:
    """Return a single session row as a dict, or None when not found.

    Absent DB or locked DB returns None gracefully.  Missing columns in the
    sessions table fall back to the corresponding entry in ``_SESSION_DEFAULTS``.

    ``live=True`` opens the DB WAL-visible (plain ``mode=ro``) so a probe
    running alongside a live Hermes session sees writes that are committed but
    not yet checkpointed. The default keeps the immutable snapshot view used
    for collecting already-ended sessions. ``home`` reads that profile's
    state.db instead of the one HERMES_HOME names.
    """
    if not session_id:
        return None
    db = state_db_path(home) if home else state_db_path()
    if not db.exists():
        return None
    try:
        with _ro_connect(db, immutable=not live) as conn:
            available = _table_columns(conn, "sessions")
            if not available:
                return None
            # Build SELECT only for columns that exist in the schema.
            select_cols = [c for c in _SESSION_COLUMNS if c in available]
            if not select_cols:
                return None
            sql = f"SELECT {', '.join(select_cols)} FROM sessions WHERE id = ? LIMIT 1"
            row = conn.execute(sql, (session_id,)).fetchone()
            if row is None:
                return None
            return _row_to_dict(row, set(select_cols))
    except (sqlite3.Error, OSError) as exc:
        logger.debug("[hermes_state] get_session(%r) error: %s", session_id, exc)
        return None


def recent_sessions(days: int = 30, max_rows: int = _MAX_ROWS) -> list[dict[str, Any]]:
    """Return sessions started within the last ``days`` days, newest first.

    Returns an empty list when the DB is absent, locked, or has no sessions
    table.  Missing columns fall back to defaults.
    """
    db = state_db_path()
    if not db.exists():
        return []
    cutoff = time.time() - (max(1, days) * 86400)
    try:
        with _ro_connect(db) as conn:
            available = _table_columns(conn, "sessions")
            if not available:
                return []
            select_cols = [c for c in _SESSION_COLUMNS if c in available]
            if not select_cols:
                return []
            # Use started_at filter only when the column exists.
            if "started_at" in available:
                sql = (
                    f"SELECT {', '.join(select_cols)} FROM sessions "
                    f"WHERE started_at >= ? "
                    f"ORDER BY started_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, (cutoff, max_rows)).fetchall()
            else:
                sql = f"SELECT {', '.join(select_cols)} FROM sessions LIMIT ?"
                rows = conn.execute(sql, (max_rows,)).fetchall()
            return [_row_to_dict(r, set(select_cols)) for r in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.debug("[hermes_state] recent_sessions() error: %s", exc)
        return []


def session_token_totals(session_id: str) -> dict[str, Any]:
    """Return token/cost columns for a session as a flat dict.

    Always returns a dict (never raises).  Zero/None defaults when the DB is
    absent, the session does not exist, or columns are missing.

    Returned keys:
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
        reasoning_tokens, message_count, tool_call_count, api_call_count,
        model, estimated_cost_usd, cost_status, started_at, ended_at,
        end_reason.
    """
    empty: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "message_count": 0,
        "tool_call_count": 0,
        "api_call_count": 0,
        "model": "unknown",
        "estimated_cost_usd": None,
        "cost_status": "unknown",
        "started_at": None,
        "ended_at": None,
        "end_reason": None,
    }
    row = get_session(session_id)
    if row is None:
        return empty
    return {
        "input_tokens": _safe_int(row.get("input_tokens")),
        "output_tokens": _safe_int(row.get("output_tokens")),
        "cache_read_tokens": _safe_int(row.get("cache_read_tokens")),
        "cache_write_tokens": _safe_int(row.get("cache_write_tokens")),
        "reasoning_tokens": _safe_int(row.get("reasoning_tokens")),
        "message_count": _safe_int(row.get("message_count")),
        "tool_call_count": _safe_int(row.get("tool_call_count")),
        "api_call_count": _safe_int(row.get("api_call_count")),
        "model": str(row.get("model") or "unknown"),
        "estimated_cost_usd": _safe_float(row.get("estimated_cost_usd")),
        "cost_status": str(row.get("cost_status") or "unknown"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "end_reason": row.get("end_reason"),
    }


# Message columns the checkpoint builder reads. Content-bearing columns are
# capped per row so one huge tool result cannot balloon a checkpoint read.
_MESSAGE_COLUMNS = ("id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp")
_MESSAGE_TEXT_CAP = 20_000
_MAX_MESSAGE_ROWS = 2_000


def session_messages(
    session_id: str, *, limit: int = 500, live: bool = True
) -> list[dict[str, Any]]:
    """Return the last ``limit`` message rows of a session, oldest first.

    Read-only, like every reader here. Rows are returned whether or not Hermes
    has since compacted them away (``active``/``compacted`` are ignored on
    purpose): a pre-compaction checkpoint must see what the model saw. Each row
    is a dict with ``role``, ``content``, ``tool_calls`` (raw JSON text),
    ``tool_name``, ``tool_call_id`` and ``timestamp``; text is capped per row.

    ``live=True`` (default) opens the WAL-visible view so a checkpoint written
    during a live session sees the latest turns. Never raises: an absent,
    locked, or unexpected DB yields an empty list.
    """
    if not session_id:
        return []
    db = state_db_path()
    if not db.exists():
        return []
    limit = max(1, min(int(limit or 1), _MAX_MESSAGE_ROWS))
    try:
        with _ro_connect(db, immutable=not live) as conn:
            available = _table_columns(conn, "messages")
            if not available or "session_id" not in available or "role" not in available:
                return []
            cols = [c for c in _MESSAGE_COLUMNS if c in available]
            order = "id" if "id" in available else ("timestamp" if "timestamp" in available else "rowid")
            sql = (
                f"SELECT {', '.join(cols)} FROM messages WHERE session_id = ? "
                f"ORDER BY {order} DESC LIMIT ?"
            )
            rows = conn.execute(sql, (session_id, limit)).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.debug("[hermes_state] session_messages(%r) error: %s", session_id, exc)
        return []
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        item: dict[str, Any] = {}
        for col in cols:
            try:
                val = row[col]
            except (IndexError, KeyError):
                val = None
            if isinstance(val, str) and len(val) > _MESSAGE_TEXT_CAP:
                val = val[:_MESSAGE_TEXT_CAP]
            item[col] = val
        out.append(item)
    return out


def db_status() -> dict[str, Any]:
    """Lightweight presence/readability report for the doctor."""
    db = state_db_path()
    readable = False
    row_count = 0
    schema_version = None
    if db.exists():
        try:
            with _ro_connect(db) as conn:
                available = _table_columns(conn, "sessions")
                if available:
                    result = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
                    row_count = int(result[0]) if result else 0
                    readable = True
                sv_cols = _table_columns(conn, "schema_version")
                if sv_cols:
                    svrow = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
                    if svrow:
                        schema_version = int(svrow[0])
        except (sqlite3.Error, OSError):
            pass
    return {
        "db_path": str(db),
        "db_exists": db.exists(),
        "readable": readable,
        "session_count": row_count,
        "schema_version": schema_version,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(db_status(), indent=2))
