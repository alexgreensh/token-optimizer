#!/usr/bin/env python3
"""Bounded, standalone reporting for one explicitly selected Hermes profile.

This module intentionally uses only the Python standard library.  It reads the
profile's direct-child state.db through a WAL-aware, read-only SQLite connection
and emits aggregates only.  It does not import Token Optimizer's runtime.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
OBSERVER_NAME = "token-optimizer-observer"
DB_NAME = "state.db"
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_SCAN_ROWS = 10_000
MAX_DATE_BUCKETS = 30
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TEXT_BYTES = 8 * 1024
BUSY_TIMEOUT_MS = 250
MAX_COUNTER = 10**18
MAX_COST_USD = 10**12

_REQUIRED_COLUMNS = frozenset({"id", "started_at", "input_tokens", "output_tokens"})
_SELECT_COLUMNS = (
    "started_at",
    "ended_at",
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
)


class ObserverError(Exception):
    """A safe, user-facing failure with no private path data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _walk_without_symlinks(path: Path) -> None:
    """Reject symlinks in every lexical component, including ancestors."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        if component in ("", ".", ".."):
            raise ObserverError("home_invalid", "Hermes home must be a canonical absolute directory")
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ObserverError("home_missing", "Hermes home does not exist") from exc
        except OSError as exc:
            raise ObserverError("home_invalid", "Hermes home cannot be safely inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ObserverError("home_symlink", "Hermes home must not contain symlink components")


def validate_state_db(hermes_home: str, *, max_db_bytes: int = MAX_DB_BYTES) -> Path:
    """Validate and return the direct-child state.db without following links."""
    if not isinstance(hermes_home, str) or not hermes_home:
        raise ObserverError("home_invalid", "Hermes home must be explicitly supplied")
    home = Path(hermes_home)
    if not home.is_absolute():
        raise ObserverError("home_relative", "Hermes home must be an absolute directory")
    if ".." in home.parts or "." in home.parts:
        raise ObserverError("home_invalid", "Hermes home must be a canonical absolute directory")

    _walk_without_symlinks(home)
    try:
        home_info = home.lstat()
        resolved_home = home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ObserverError("home_invalid", "Hermes home cannot be safely inspected") from exc
    if not stat.S_ISDIR(home_info.st_mode):
        raise ObserverError("home_not_directory", "Hermes home must be a directory")
    if resolved_home != home:
        raise ObserverError("home_invalid", "Hermes home must be a canonical real directory")

    db = home / DB_NAME
    try:
        db_info = db.lstat()
    except FileNotFoundError as exc:
        raise ObserverError("database_missing", "state.db is missing") from exc
    except OSError as exc:
        raise ObserverError("database_invalid", "state.db cannot be safely inspected") from exc
    if stat.S_ISLNK(db_info.st_mode):
        raise ObserverError("database_symlink", "state.db must not be a symlink")
    if not stat.S_ISREG(db_info.st_mode):
        raise ObserverError("database_not_regular", "state.db must be a regular file")
    try:
        resolved_db = db.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ObserverError("database_invalid", "state.db cannot be safely inspected") from exc
    if resolved_db.parent != resolved_home or resolved_db.name != DB_NAME:
        raise ObserverError("database_escape", "state.db must be the direct child of Hermes home")
    if max_db_bytes < 0 or db_info.st_size > max_db_bytes:
        raise ObserverError("database_oversized", "state.db exceeds the configured size limit")
    return db


def _connect_read_only(db: Path) -> sqlite3.Connection:
    """Open a WAL-visible, query-only SQLite connection."""
    try:
        # nofollow binds the leaf-symlink policy to SQLite's own open rather
        # than relying only on the preceding lstat check.
        uri = f"{db.as_uri()}?mode=ro&nofollow=1"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Prevent schema-defined functions or virtual-table surprises where the
        # host SQLite supports this defense. It is harmless on supported builds.
        try:
            conn.execute("PRAGMA trusted_schema = OFF")
        except sqlite3.Error:
            pass
        return conn
    except sqlite3.Error as exc:
        raise ObserverError("database_malformed", "state.db is not a readable SQLite database") from exc


def _schema_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        table = conn.execute(
            "SELECT type FROM sqlite_schema WHERE name = 'sessions' LIMIT 1"
        ).fetchone()
        if table is None or table[0] != "table":
            raise ObserverError("unsupported_schema", "state.db has no supported sessions table")
        rows = conn.execute('PRAGMA table_info("sessions")').fetchall()
    except ObserverError:
        raise
    except sqlite3.Error as exc:
        raise ObserverError("database_malformed", "state.db schema cannot be read") from exc
    columns = {str(row[1]) for row in rows}
    missing = _REQUIRED_COLUMNS - columns
    if missing:
        raise ObserverError(
            "unsupported_schema",
            "sessions table is missing required aggregate columns",
        )
    return columns


def _safe_counter(value: Any) -> int:
    try:
        number = int(value) if value is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(MAX_COUNTER, max(0, number))


def _sum_counter(current: int, value: Any) -> int:
    return min(MAX_COUNTER, current + _safe_counter(value))


def _safe_cost(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return min(MAX_COST_USD, number)


def _day(value: Any) -> str | None:
    """Convert a numeric Unix time or bounded ISO timestamp to a UTC date."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
        text = str(value)[:64]
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _query_rows(conn: sqlite3.Connection, columns: set[str]) -> list[sqlite3.Row]:
    selected = [name if name in columns else f"NULL AS {name}" for name in _SELECT_COLUMNS]
    sql = (
        f"SELECT {', '.join(selected)} FROM sessions "
        "ORDER BY started_at DESC LIMIT ?"
    )
    try:
        return conn.execute(sql, (MAX_SCAN_ROWS + 1,)).fetchall()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise ObserverError("database_busy", "state.db is busy; retry shortly") from exc
        raise ObserverError("database_malformed", "sessions data cannot be read") from exc
    except sqlite3.Error as exc:
        raise ObserverError("database_malformed", "sessions data cannot be read") from exc


def inspect_database(hermes_home: str, *, max_db_bytes: int = MAX_DB_BYTES) -> dict[str, Any]:
    """Return a deterministic, bounded aggregate report for one profile."""
    db = validate_state_db(hermes_home, max_db_bytes=max_db_bytes)
    conn = _connect_read_only(db)
    try:
        columns = _schema_columns(conn)
        rows = _query_rows(conn, columns)
    finally:
        conn.close()

    truncated = len(rows) > MAX_SCAN_ROWS
    if truncated:
        rows = rows[:MAX_SCAN_ROWS]

    sessions = {
        "total": 0,
        "active": 0,
        "completed": 0,
        "messages": 0,
        "tool_calls": 0,
        "api_calls": 0,
    }
    tokens = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
        "total": 0,
    }
    estimated = 0.0
    actual = 0.0
    effective = 0.0
    daily: dict[str, dict[str, Any]] = {}

    for row in rows:
        sessions["total"] += 1
        if row["ended_at"] is None:
            sessions["active"] += 1
        else:
            sessions["completed"] += 1
        sessions["messages"] = _sum_counter(sessions["messages"], row["message_count"])
        sessions["tool_calls"] = _sum_counter(sessions["tool_calls"], row["tool_call_count"])
        sessions["api_calls"] = _sum_counter(sessions["api_calls"], row["api_call_count"])

        values = {
            "input": _safe_counter(row["input_tokens"]),
            "output": _safe_counter(row["output_tokens"]),
            "cache_read": _safe_counter(row["cache_read_tokens"]),
            "cache_write": _safe_counter(row["cache_write_tokens"]),
            "reasoning": _safe_counter(row["reasoning_tokens"]),
        }
        for key, value in values.items():
            tokens[key] = min(MAX_COUNTER, tokens[key] + value)
        row_total = min(MAX_COUNTER, sum(values.values()))
        tokens["total"] = min(MAX_COUNTER, tokens["total"] + row_total)

        row_estimated = _safe_cost(row["estimated_cost_usd"])
        row_actual = _safe_cost(row["actual_cost_usd"])
        if row_estimated is not None:
            estimated = min(MAX_COST_USD, estimated + row_estimated)
        if row_actual is not None:
            actual = min(MAX_COST_USD, actual + row_actual)
        selected_cost = row_actual if row_actual is not None else row_estimated
        if selected_cost is not None:
            effective = min(MAX_COST_USD, effective + selected_cost)

        day = _day(row["started_at"])
        if day is not None:
            bucket = daily.setdefault(day, {"date": day, "sessions": 0, "tokens": 0, "cost_usd": 0.0})
            bucket["sessions"] += 1
            bucket["tokens"] = min(MAX_COUNTER, bucket["tokens"] + row_total)
            if selected_cost is not None:
                bucket["cost_usd"] = min(MAX_COST_USD, bucket["cost_usd"] + selected_cost)

    chosen_days = sorted(daily)[-MAX_DATE_BUCKETS:]
    daily_rows = []
    for day in chosen_days:
        bucket = daily[day]
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
        daily_rows.append(bucket)

    return {
        "context": {
            "occupancy_available": False,
            "reason": "supported sessions schema stores cumulative usage, not live occupancy",
        },
        "cost_usd": {
            "actual": round(actual, 6),
            "effective": round(effective, 6),
            "estimated": round(estimated, 6),
        },
        "daily": daily_rows,
        "observer": OBSERVER_NAME,
        "schema_version": SCHEMA_VERSION,
        "sessions": sessions,
        "source": {"sessions_scanned": len(rows), "truncated": truncated},
        "status": "ok",
        "tokens": tokens,
    }


def _error_document(error: ObserverError) -> dict[str, Any]:
    return {
        "error": {"code": error.code, "message": error.message},
        "observer": OBSERVER_NAME,
        "ok": False,
        "schema_version": SCHEMA_VERSION,
    }


def _json_text(document: dict[str, Any]) -> str:
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ObserverError("output_limit", "aggregate report exceeds the output limit")
    return text


def _text_report(report: dict[str, Any]) -> str:
    sessions = report["sessions"]
    tokens = report["tokens"]
    costs = report["cost_usd"]
    source = report["source"]
    lines = [
        "Token Optimizer Hermes Observer (beta)",
        f"Sessions: {sessions['total']} ({sessions['active']} active, {sessions['completed']} completed)",
        f"Activity: {sessions['messages']} messages, {sessions['tool_calls']} tool calls, {sessions['api_calls']} API calls",
        (
            "Tokens: "
            f"{tokens['total']} total; {tokens['input']} input, {tokens['output']} output, "
            f"{tokens['cache_read']} cache-read, {tokens['cache_write']} cache-write, "
            f"{tokens['reasoning']} reasoning"
        ),
        (
            "Cost (USD): "
            f"{costs['effective']:.6f} effective, {costs['estimated']:.6f} estimated, "
            f"{costs['actual']:.6f} actual"
        ),
        "Context occupancy: unavailable (session counters are cumulative)",
        f"Rows scanned: {source['sessions_scanned']}; truncated: {str(source['truncated']).lower()}",
        "Daily aggregates:",
    ]
    for bucket in report["daily"]:
        lines.append(
            f"  {bucket['date']}: {bucket['sessions']} sessions, "
            f"{bucket['tokens']} tokens, ${bucket['cost_usd']:.6f}"
        )
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ObserverError("output_limit", "text report exceeds the output limit")
    return text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Hermes aggregate observer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report", help="emit a bounded aggregate report")
    report.add_argument("--hermes-home", required=True)
    report.add_argument("--format", choices=("json", "text"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = inspect_database(args.hermes_home)
        output = _json_text(report) if args.format == "json" else _text_report(report)
        sys.stdout.write(output)
        return 0
    except ObserverError as error:
        if args.format == "json":
            sys.stdout.write(_json_text(_error_document(error)))
        else:
            sys.stdout.write(f"ERROR [{error.code}]: {error.message}\n")
        return 1
    except Exception:
        error = ObserverError("internal_error", "observer failed safely")
        if args.format == "json":
            sys.stdout.write(_json_text(_error_document(error)))
        else:
            sys.stdout.write(f"ERROR [{error.code}]: {error.message}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
