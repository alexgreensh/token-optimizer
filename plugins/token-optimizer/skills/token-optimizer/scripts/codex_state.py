#!/usr/bin/env python3
"""Read-only reader for Codex's runtime SQLite state.

Codex (v0.130+) moved runtime state out of in-process structures into
versioned SQLite databases under ``CODEX_HOME``:

- ``state_<N>.sqlite``  -> ``threads`` (per-thread token totals, model),
  ``thread_spawn_edges`` (subagent parent->child graph), ``stage1_outputs``
  (densified memory summaries: ``raw_memory`` + ``rollout_summary``).
- ``goals_<N>.sqlite`` -> ``thread_goals`` (objective, token_budget,
  tokens_used, status).

This module reads those databases to power Codex subagent-cost, memory-overhead,
and goal-budget measurement. It is **strictly read-only** and is designed to
never disrupt the live Codex process that owns these databases:

- Opens with ``file:...?mode=ro`` and a short busy-timeout (KTD3).
- Keeps each connection open for the minimum possible window: introspect the
  schema and run a single bounded query, then close before doing any Python-side
  aggregation. A connection lingering across a Codex WAL checkpoint would defer
  the checkpoint and grow the ``-wal`` file, degrading Codex write latency.
- Discovers databases by glob and tolerates schema-version drift, missing
  tables, renamed columns, missing files, and a freshly-migrated empty database
  (KTD2/KTD4): every public function degrades to an empty/zero result instead
  of raising.

The 117MB ``logs_<N>.sqlite`` is internal telemetry (not context tokens) and is
intentionally out of scope.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from runtime_env import codex_home, detect_runtime

CHARS_PER_TOKEN = 4
_BUSY_TIMEOUT_SECONDS = 0.25
# Subagents whose spawn edge is still "open" this long after their thread last
# updated are treated as leaked (spawned, never closed).
LEAK_STALE_MINUTES = 30
# Bound raw row reads so a pathological future state DB can't dominate work.
_MAX_ROWS = 5000
# How many detail rows to surface in the structured output.
_DETAIL_LIMIT = 20
# Whitelist of tables this module ever inspects. PRAGMA table_info cannot use
# parameter binding, so the table name is interpolated — gating on this set
# guarantees no externally-derived value can ever reach that f-string.
_ALLOWED_TABLES = frozenset({"threads", "thread_spawn_edges", "stage1_outputs", "thread_goals"})


def _is_codex() -> bool:
    return detect_runtime() == "codex"


def _find_versioned_db(prefix: str, primary_table: str) -> Path | None:
    """Return the best ``{prefix}_<N>.sqlite`` under CODEX_HOME.

    Selection rule (KTD2, migration-in-flight safe): prefer the highest numeric
    suffix whose ``primary_table`` has at least one row; if the highest-suffix
    database is empty (Codex created the new schema but has not migrated data
    yet), fall back to the next-highest with data. If none have data, return the
    highest suffix (an empty but valid result). ``None`` when no file matches.
    """
    home = codex_home()
    candidates: list[tuple[int, Path]] = []
    for path in home.glob(f"{prefix}_*.sqlite"):
        suffix = path.stem[len(prefix) + 1:]
        if suffix.isdigit():
            candidates.append((int(suffix), path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, path in candidates:
        if _table_has_rows(path, primary_table):
            return path
    # No populated database found; return the highest suffix so callers still
    # get a valid (empty) read rather than None.
    return candidates[0][1]


@contextmanager
def _ro_connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only with a short busy-timeout; always close.

    Never writes, never checkpoints, never attaches. The caller must keep the
    open window minimal: introspect + one query + fetch, then exit.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        # Defense-in-depth: `mode=ro` blocks writes to this DB but not ATTACH;
        # `query_only` also blocks ATTACH and any write through the connection,
        # so a future code change can't accidentally write via a "read-only" conn.
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _table_has_rows(path: Path, table: str) -> bool:
    try:
        with _ro_connect(path) as conn:
            if not _table_columns(conn, table):
                return False
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            return row is not None
    except (sqlite3.Error, OSError):
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table``, or empty set if it does not exist.

    Guards every read against a missing table or renamed column on a future
    schema (KTD4). ``PRAGMA table_info`` returns no rows for an unknown table,
    so this doubles as an existence check without a separate sqlite_master pass.
    """
    if table not in _ALLOWED_TABLES:
        return set()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_ms(updated_at_ms: Any, updated_at: Any) -> int | None:
    """Resolve a thread's last-update time in epoch milliseconds.

    ``threads`` carries both the original ``updated_at`` (seconds) and a later
    nullable ``updated_at_ms``. Prefer the millisecond column; fall back to the
    seconds column; ``None`` when neither is usable.

    Guards against unit drift: any epoch time after 2001 is > 1e12 ms, so a
    positive value below that in the "ms" column is almost certainly seconds
    (a migration mistake) and is rescaled — otherwise every active subagent
    would be misread as decades stale and falsely flagged as leaked.
    """
    _MS_2001 = 1_000_000_000_000  # ms epoch for ~2001; below this = seconds
    if isinstance(updated_at_ms, (int, float)) and updated_at_ms > 0:
        return int(updated_at_ms) if updated_at_ms >= _MS_2001 else int(updated_at_ms * 1000)
    if isinstance(updated_at, (int, float)) and updated_at > 0:
        return int(updated_at) if updated_at >= _MS_2001 else int(updated_at * 1000)
    return None


def _empty_subagents() -> dict[str, Any]:
    return {
        "available": False,
        "total_subagents": 0,
        "open_subagents": 0,
        "closed_subagents": 0,
        "leaked_subagents": 0,
        "total_child_tokens": 0,
        "by_parent": [],
        "leaked": [],
    }


def subagent_costs() -> dict[str, Any]:
    """Per-subagent token cost and leak detection from the state database.

    Reads ``thread_spawn_edges`` joined to ``threads`` to attribute each child
    (subagent) thread's cumulative ``tokens_used`` to its parent, and flags
    children whose edge is still ``open`` long after their thread last updated.
    """
    if not _is_codex():
        return _empty_subagents()
    db = _find_versioned_db("state", "threads")
    if db is None:
        return _empty_subagents()

    try:
        with _ro_connect(db) as conn:
            edge_cols = _table_columns(conn, "thread_spawn_edges")
            thread_cols = _table_columns(conn, "threads")
            if not {"parent_thread_id", "child_thread_id", "status"} <= edge_cols:
                return _empty_subagents()
            if not {"id", "tokens_used"} <= thread_cols:
                return _empty_subagents()
            has_ms = "updated_at_ms" in thread_cols
            has_secs = "updated_at" in thread_cols
            updated_ms = "t.updated_at_ms" if has_ms else "NULL"
            updated_secs = "t.updated_at" if has_secs else "NULL"
            rows = conn.execute(
                f"""
                SELECT e.parent_thread_id AS parent_id,
                       e.child_thread_id  AS child_id,
                       e.status           AS status,
                       COALESCE(t.tokens_used, 0) AS child_tokens,
                       {updated_ms} AS updated_at_ms,
                       {updated_secs} AS updated_at
                FROM thread_spawn_edges e
                LEFT JOIN threads t ON t.id = e.child_thread_id
                LIMIT {_MAX_ROWS}
                """
            ).fetchall()
    except (sqlite3.Error, OSError):
        return _empty_subagents()

    # Aggregation runs after the connection is closed (KTD3).
    now_ms = _now_ms()
    stale_cutoff_ms = LEAK_STALE_MINUTES * 60 * 1000
    open_count = closed_count = leaked_count = total_child_tokens = 0
    by_parent: dict[str, dict[str, int]] = {}
    leaked: list[dict[str, Any]] = []

    for row in rows:
        status = str(row["status"] or "").lower()
        child_tokens = int(row["child_tokens"] or 0)
        total_child_tokens += child_tokens
        parent = str(row["parent_id"] or "unknown")
        bucket = by_parent.setdefault(parent, {"child_count": 0, "child_tokens": 0, "open_count": 0})
        bucket["child_count"] += 1
        bucket["child_tokens"] += child_tokens
        if status == "open":
            open_count += 1
            bucket["open_count"] += 1
            last_ms = _normalize_ms(row["updated_at_ms"], row["updated_at"])
            age_ms = (now_ms - last_ms) if last_ms is not None else None
            if age_ms is None or age_ms > stale_cutoff_ms:
                leaked_count += 1
                leaked.append({
                    "child_thread_id": str(row["child_id"] or ""),
                    "parent_thread_id": parent,
                    "tokens_used": child_tokens,
                    "age_minutes": round(age_ms / 60000, 1) if age_ms is not None else None,
                })
        elif status == "closed":
            closed_count += 1

    by_parent_list = sorted(
        ({"parent_thread_id": pid, **vals} for pid, vals in by_parent.items()),
        key=lambda item: item["child_tokens"],
        reverse=True,
    )[:_DETAIL_LIMIT]
    leaked.sort(key=lambda item: item["tokens_used"], reverse=True)

    return {
        "available": True,
        "total_subagents": len(rows),
        "open_subagents": open_count,
        "closed_subagents": closed_count,
        "leaked_subagents": leaked_count,
        "total_child_tokens": total_child_tokens,
        "by_parent": by_parent_list,
        "leaked": leaked[:_DETAIL_LIMIT],
    }


def _empty_memory() -> dict[str, Any]:
    return {
        "available": False,
        "thread_count": 0,
        "total_raw_memory_chars": 0,
        "total_rollout_summary_chars": 0,
        "total_memory_tokens": 0,
        "max_thread_memory_tokens": 0,
        "by_thread": [],
    }


def memory_overhead() -> dict[str, Any]:
    """Per-thread memory overhead from ``stage1_outputs``.

    Codex's densified memory (``raw_memory`` + ``rollout_summary``) is injected
    into context. This measures its size in tokens per thread. Returns zeros
    cleanly when the table is empty (the common case until a user accumulates
    memory).
    """
    if not _is_codex():
        return _empty_memory()
    db = _find_versioned_db("state", "stage1_outputs")
    if db is None:
        return _empty_memory()

    try:
        with _ro_connect(db) as conn:
            cols = _table_columns(conn, "stage1_outputs")
            if "thread_id" not in cols:
                return _empty_memory()
            raw_expr = "length(raw_memory)" if "raw_memory" in cols else "0"
            summary_expr = "length(rollout_summary)" if "rollout_summary" in cols else "0"
            rows = conn.execute(
                f"""
                SELECT thread_id AS thread_id,
                       COALESCE({raw_expr}, 0) AS raw_chars,
                       COALESCE({summary_expr}, 0) AS summary_chars
                FROM stage1_outputs
                LIMIT {_MAX_ROWS}
                """
            ).fetchall()
    except (sqlite3.Error, OSError):
        return _empty_memory()

    if not rows:
        return {**_empty_memory(), "available": True}

    total_raw = total_summary = max_tokens = 0
    by_thread: list[dict[str, Any]] = []
    for row in rows:
        raw_chars = int(row["raw_chars"] or 0)
        summary_chars = int(row["summary_chars"] or 0)
        total_raw += raw_chars
        total_summary += summary_chars
        thread_tokens = (raw_chars + summary_chars) // CHARS_PER_TOKEN
        max_tokens = max(max_tokens, thread_tokens)
        by_thread.append({"thread_id": str(row["thread_id"] or ""), "memory_tokens": thread_tokens})

    by_thread.sort(key=lambda item: item["memory_tokens"], reverse=True)
    return {
        "available": True,
        "thread_count": len(rows),
        "total_raw_memory_chars": total_raw,
        "total_rollout_summary_chars": total_summary,
        "total_memory_tokens": (total_raw + total_summary) // CHARS_PER_TOKEN,
        "max_thread_memory_tokens": max_tokens,
        "by_thread": by_thread[:_DETAIL_LIMIT],
    }


def _empty_goals() -> dict[str, Any]:
    return {
        "available": False,
        "total_goals": 0,
        "active_goals": 0,
        "budget_limited": 0,
        "usage_limited": 0,
        "over_budget": 0,
        "goals": [],
    }


def goal_budgets() -> dict[str, Any]:
    """Goal budget utilization from ``thread_goals``.

    Surfaces per-goal token budget vs. usage and flags goals Codex marked
    ``budget_limited`` / ``usage_limited``, plus goals that exceeded their
    explicit ``token_budget``. Empty-safe.
    """
    if not _is_codex():
        return _empty_goals()
    db = _find_versioned_db("goals", "thread_goals")
    if db is None:
        return _empty_goals()

    try:
        with _ro_connect(db) as conn:
            cols = _table_columns(conn, "thread_goals")
            if "status" not in cols:
                return _empty_goals()
            objective = "objective" if "objective" in cols else "''"
            budget = "token_budget" if "token_budget" in cols else "NULL"
            used = "tokens_used" if "tokens_used" in cols else "0"
            time_used = "time_used_seconds" if "time_used_seconds" in cols else "0"
            thread_id = "thread_id" if "thread_id" in cols else "''"
            rows = conn.execute(
                f"""
                SELECT {thread_id} AS thread_id,
                       {objective} AS objective,
                       status AS status,
                       {budget} AS token_budget,
                       COALESCE({used}, 0) AS tokens_used,
                       COALESCE({time_used}, 0) AS time_used_seconds
                FROM thread_goals
                LIMIT {_MAX_ROWS}
                """
            ).fetchall()
    except (sqlite3.Error, OSError):
        return _empty_goals()

    if not rows:
        return {**_empty_goals(), "available": True}

    active = budget_limited = usage_limited = over_budget = 0
    goals: list[dict[str, Any]] = []
    for row in rows:
        status = str(row["status"] or "").lower()
        token_budget = row["token_budget"]
        tokens_used = int(row["tokens_used"] or 0)
        utilization: float | None = None
        if isinstance(token_budget, (int, float)) and token_budget > 0:
            utilization = round(tokens_used / token_budget, 3)
            if tokens_used > token_budget:
                over_budget += 1
        if status == "active":
            active += 1
        elif status == "budget_limited":
            budget_limited += 1
        elif status == "usage_limited":
            usage_limited += 1
        objective_text = str(row["objective"] or "").strip()
        goals.append({
            "thread_id": str(row["thread_id"] or ""),
            "objective": objective_text[:117] + "..." if len(objective_text) > 120 else objective_text,
            "status": status,
            "token_budget": int(token_budget) if isinstance(token_budget, (int, float)) else None,
            "tokens_used": tokens_used,
            "utilization": utilization,
            "time_used_seconds": int(row["time_used_seconds"] or 0),
        })

    goals.sort(key=lambda item: item["utilization"] if item["utilization"] is not None else -1, reverse=True)
    return {
        "available": True,
        "total_goals": len(rows),
        "active_goals": active,
        "budget_limited": budget_limited,
        "usage_limited": usage_limited,
        "over_budget": over_budget,
        "goals": goals[:_DETAIL_LIMIT],
    }


def state_db_status() -> dict[str, Any]:
    """Lightweight presence/readability report for the doctor (U5)."""
    state_db = _find_versioned_db("state", "threads")
    goals_db = _find_versioned_db("goals", "thread_goals")
    return {
        "state_db": str(state_db) if state_db else None,
        "goals_db": str(goals_db) if goals_db else None,
        "state_readable": bool(state_db and _table_has_rows(state_db, "threads")) if state_db else False,
        "goals_present": goals_db is not None,
    }


if __name__ == "__main__":
    import json

    print(json.dumps({
        "subagent_costs": subagent_costs(),
        "memory_overhead": memory_overhead(),
        "goal_budgets": goal_budgets(),
        "state_db_status": state_db_status(),
    }, indent=2))
