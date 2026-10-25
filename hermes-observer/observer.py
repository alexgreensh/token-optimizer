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
import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

SCHEMA_VERSION = 1
OBSERVER_NAME = "token-optimizer-observer"
DB_NAME = "state.db"
MAX_DB_BYTES = 4 * 1024 * 1024 * 1024
MAX_SCAN_ROWS = 10_000
MAX_CANDIDATE_ROWS = 3 * (MAX_SCAN_ROWS + 1)
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


_Identity = tuple[int, int, int]
_IdentityHook = Callable[[str, Path], None]


class _SourceCapture(NamedTuple):
    identities: dict[Path, _Identity]
    db_size: int


class _ValidatedSource(NamedTuple):
    home: Path
    db: Path
    identities: dict[Path, _Identity]


def _validate_lexical_home(hermes_home: str) -> Path:
    """Reject noncanonical syntax before ``Path`` can erase its evidence."""
    if not isinstance(hermes_home, str) or not hermes_home:
        raise ObserverError("home_invalid", "Hermes home must be explicitly supplied")
    if "\x00" in hermes_home:
        raise ObserverError("home_invalid", "Hermes home must be a canonical absolute directory")
    home = Path(hermes_home)
    if not home.is_absolute():
        raise ObserverError("home_relative", "Hermes home must be an absolute directory")
    # pathlib normalizes all of these, so inspect the caller's spelling first.
    components = hermes_home.split("/")
    if (
        any(component in (".", "..") for component in components)
        or "//" in hermes_home
        or (len(hermes_home) > 1 and hermes_home.endswith("/"))
    ):
        raise ObserverError("home_invalid", "Hermes home must be a canonical absolute directory")
    return home


def _home_components(home: Path) -> list[Path]:
    current = Path(home.anchor)
    paths = [current]
    for component in home.parts[1:]:
        current = current / component
        paths.append(current)
    return paths


def _identity(info: os.stat_result) -> _Identity:
    """Return object identity and type/mode, excluding mutable file size."""
    return (info.st_dev, info.st_ino, info.st_mode)


def _capture_source(home: Path, db: Path) -> _SourceCapture:
    """Capture every ancestor, the home, and the database without following links."""
    identities: dict[Path, _Identity] = {}
    db_size: int | None = None
    home_paths = _home_components(home)
    for path in [*home_paths, db]:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            code = "database_missing" if path == db else "home_missing"
            message = "state.db is missing" if path == db else "Hermes home does not exist"
            raise ObserverError(code, message) from exc
        except OSError as exc:
            raise ObserverError("source_changed", "Hermes source cannot be safely inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            code = "database_symlink" if path == db else "home_symlink"
            message = (
                "state.db must not be a symlink"
                if path == db
                else "Hermes home must not contain symlink components"
            )
            raise ObserverError(code, message)
        if path in home_paths and not stat.S_ISDIR(info.st_mode):
            code = "home_not_directory" if path == home else "home_invalid"
            raise ObserverError(code, "Hermes home must be a directory")
        if path == db and not stat.S_ISREG(info.st_mode):
            raise ObserverError("database_not_regular", "state.db must be a regular file")
        identities[path] = _identity(info)
        if path == db:
            db_size = info.st_size
    if db_size is None:
        raise ObserverError("source_changed", "Hermes source cannot be safely inspected")
    return _SourceCapture(identities, db_size)


def _open_guard(
    path: Path,
    expected: _Identity,
    *,
    directory: bool,
    max_db_bytes: int,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        actual = _identity(info)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ObserverError("source_changed", "Hermes source changed during inspection") from exc
    if actual != expected:
        os.close(descriptor)
        raise ObserverError("source_changed", "Hermes source changed during inspection")
    if not directory and (max_db_bytes < 0 or info.st_size > max_db_bytes):
        os.close(descriptor)
        raise ObserverError("database_oversized", "state.db exceeds the configured size limit")
    return descriptor


def _assert_source_stable(
    home: Path,
    db: Path,
    expected: dict[Path, _Identity],
    home_fd: int,
    db_fd: int,
    max_db_bytes: int,
) -> None:
    try:
        current = _capture_source(home, db)
        home_info = os.fstat(home_fd)
        db_info = os.fstat(db_fd)
        descriptors = (_identity(home_info), _identity(db_info))
    except ObserverError as exc:
        raise ObserverError("source_changed", "Hermes source changed during inspection") from exc
    except OSError as exc:
        raise ObserverError("source_changed", "Hermes source changed during inspection") from exc
    if current.identities != expected or descriptors != (expected[home], expected[db]):
        raise ObserverError("source_changed", "Hermes source changed during inspection")
    if max_db_bytes < 0 or current.db_size > max_db_bytes or db_info.st_size > max_db_bytes:
        raise ObserverError("database_oversized", "state.db exceeds the configured size limit")


def _validate_source(hermes_home: str, *, max_db_bytes: int) -> _ValidatedSource:
    """Validate a source and retain the identity captured by that first validation."""
    home = _validate_lexical_home(hermes_home)
    db = home / DB_NAME
    captured = _capture_source(home, db)
    try:
        resolved_home = home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ObserverError("home_invalid", "Hermes home cannot be safely inspected") from exc
    if resolved_home != home:
        raise ObserverError("home_invalid", "Hermes home must be a canonical real directory")
    try:
        resolved_db = db.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ObserverError("database_invalid", "state.db cannot be safely inspected") from exc
    if resolved_db.parent != resolved_home or resolved_db.name != DB_NAME:
        raise ObserverError("database_escape", "state.db must be the direct child of Hermes home")
    if max_db_bytes < 0 or captured.db_size > max_db_bytes:
        raise ObserverError("database_oversized", "state.db exceeds the configured size limit")
    return _ValidatedSource(home, db, captured.identities)


def validate_state_db(hermes_home: str, *, max_db_bytes: int = MAX_DB_BYTES) -> Path:
    """Validate and return the direct-child state.db without following links."""
    return _validate_source(hermes_home, max_db_bytes=max_db_bytes).db


def _connect_read_only(db: Path) -> sqlite3.Connection:
    """Open a WAL-visible, read-only SQLite connection."""
    try:
        # nofollow binds the leaf-symlink policy to SQLite's own open rather
        # than relying only on the preceding lstat check.
        uri = f"{db.as_uri()}?mode=ro&nofollow=1"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise ObserverError("database_malformed", "state.db is not a readable SQLite database") from exc


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply defensive settings after the caller verifies post-open identity."""
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Prevent schema-defined functions or virtual-table surprises where the
        # host SQLite supports this defense. It is harmless on supported builds.
        try:
            conn.execute("PRAGMA trusted_schema = OFF")
        except sqlite3.Error:
            pass
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
    timestamp = _timestamp(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _timestamp(value: Any) -> float | None:
    """Normalize supported Unix REAL/INTEGER and ISO TEXT timestamps."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timestamp = float(value)
            return timestamp if math.isfinite(timestamp) else None
        if not isinstance(value, str) or len(value.encode("utf-8")) > 64:
            return None
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.astimezone(timezone.utc).timestamp()
        return timestamp if math.isfinite(timestamp) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _id_sort_key(value: Any) -> tuple[str, str]:
    """Deterministic internal tie-breaker; IDs are never copied to output."""
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    return (type(value).__name__, str(value))


def _row_sort_key(row: sqlite3.Row) -> tuple[float, tuple[str, str]]:
    timestamp = _timestamp(row["started_at"])
    return (-(timestamp if timestamp is not None else -math.inf), _id_sort_key(row["id"]))


def _query_rows(conn: sqlite3.Connection, columns: set[str]) -> list[sqlite3.Row]:
    selected = ["id", *[name if name in columns else f"NULL AS {name}" for name in _SELECT_COLUMNS]]
    select_sql = f"SELECT {', '.join(selected)} FROM sessions"
    queries = (
        select_sql
        + " WHERE typeof(started_at) IN ('integer','real')"
        + " ORDER BY CAST(started_at AS REAL) DESC, CAST(id AS TEXT) ASC LIMIT ?",
        select_sql
        + " WHERE typeof(started_at) = 'text'"
        + " ORDER BY julianday(started_at) DESC, CAST(id AS TEXT) ASC LIMIT ?",
        select_sql
        + " WHERE typeof(started_at) NOT IN ('integer','real','text')"
        + " ORDER BY CAST(id AS TEXT) ASC LIMIT ?",
    )
    try:
        candidates: list[sqlite3.Row] = []
        for sql in queries:
            candidates.extend(conn.execute(sql, (MAX_SCAN_ROWS + 1,)).fetchall())
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise ObserverError("database_busy", "state.db is busy; retry shortly") from exc
        raise ObserverError("database_malformed", "sessions data cannot be read") from exc
    except sqlite3.Error as exc:
        raise ObserverError("database_malformed", "sessions data cannot be read") from exc
    if len(candidates) > MAX_CANDIDATE_ROWS:
        raise ObserverError("internal_error", "candidate row bound was exceeded")
    candidates.sort(key=_row_sort_key)
    return candidates[: MAX_SCAN_ROWS + 1]


def inspect_database(
    hermes_home: str,
    *,
    max_db_bytes: int = MAX_DB_BYTES,
    _identity_hook: _IdentityHook | None = None,
) -> dict[str, Any]:
    """Return a deterministic, bounded aggregate report for one profile."""
    validated = _validate_source(hermes_home, max_db_bytes=max_db_bytes)
    home, db, expected = validated
    if _identity_hook is not None:
        _identity_hook("after_validation", db)
    home_fd = _open_guard(
        home, expected[home], directory=True, max_db_bytes=max_db_bytes
    )
    try:
        db_fd = _open_guard(
            db, expected[db], directory=False, max_db_bytes=max_db_bytes
        )
    except Exception:
        os.close(home_fd)
        raise
    conn: sqlite3.Connection | None = None
    try:
        if _identity_hook is not None:
            _identity_hook("before_connect", db)
        conn = _connect_read_only(db)
        if _identity_hook is not None:
            _identity_hook("after_connect", db)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
        database_rows = conn.execute("PRAGMA database_list").fetchall()
        main_paths = [str(row[2]) for row in database_rows if str(row[1]) == "main"]
        if main_paths != [str(db)]:
            raise ObserverError("source_changed", "SQLite opened an unexpected database object")
        _configure_connection(conn)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
        if _identity_hook is not None:
            _identity_hook("before_schema", db)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
        columns = _schema_columns(conn)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
        if _identity_hook is not None:
            _identity_hook("before_query", db)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
        rows = _query_rows(conn, columns)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
        if _identity_hook is not None:
            _identity_hook("after_query", db)
        _assert_source_stable(home, db, expected, home_fd, db_fd, max_db_bytes)
    finally:
        if conn is not None:
            conn.close()
        os.close(db_fd)
        os.close(home_fd)

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
        row_total = min(MAX_COUNTER, values["input"] + values["output"])
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
