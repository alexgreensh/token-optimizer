"""Shared utilities for Token Optimizer fleet and measurement tools.

Extracted from measure.py to prevent duplicate maintenance of JSONL parsing,
model normalization, SQLite initialization, and file discovery patterns.

Zero external dependencies. Python 3.10+ (for match/case and type unions).
"""

from __future__ import annotations  # PEP 604 union syntax compat for Python 3.9

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CHARS_PER_TOKEN = 4.0

# ---------------------------------------------------------------------------
# Model normalization
# ---------------------------------------------------------------------------

def normalize_model_name(model_id: str) -> str | None:
    """Collapse model IDs like 'claude-sonnet-4-6' into 'sonnet'.

    Returns None for synthetic/internal model IDs that should be skipped.
    Handles non-Claude models (gpt-4o, gemini, etc.) by returning as-is.
    """
    if not model_id or model_id.startswith("<"):
        return None
    m = model_id.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    # Non-Claude models: normalize common variants
    if "gpt-4o-mini" in m:
        return "gpt-4o-mini"
    if "gpt-4o" in m:
        return "gpt-4o"
    if "gemini" in m and "pro" in m:
        return "gemini-2.5-pro"
    return model_id


# ---------------------------------------------------------------------------
# JSONL streaming parser
# ---------------------------------------------------------------------------

def iter_jsonl(filepath: Path):
    """Yield parsed JSON objects from a JSONL file, skipping bad lines.

    Handles corrupted UTF-8, permission errors, and malformed JSON gracefully.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (PermissionError, OSError):
        return


def parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string, handling 'Z' suffix."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Claude Code JSONL file discovery
# ---------------------------------------------------------------------------

def find_claude_jsonl_files(days: int = 30) -> list[tuple[Path, float, str]]:
    """Find all Claude Code JSONL session files within a day window.

    Returns list of (filepath, mtime, project_dir_name) sorted newest-first.
    """
    projects_base = CLAUDE_DIR / "projects"
    if not projects_base.exists():
        return []

    cutoff = datetime.now().timestamp() - (days * 86400)
    results = []
    for project_dir in projects_base.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            try:
                mtime = jf.stat().st_mtime
                if mtime >= cutoff:
                    results.append((jf, mtime, project_dir.name))
            except OSError:
                continue
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def find_subagent_jsonl_files(session_jsonl_path: Path) -> list[Path]:
    """Find subagent JSONL files for a given session.

    Claude Code stores subagent logs in {session-uuid}/subagents/*.jsonl
    next to the parent {session-uuid}.jsonl file.
    """
    subagent_dir = session_jsonl_path.parent / session_jsonl_path.stem / "subagents"
    if not subagent_dir.is_dir():
        return []
    results = []
    for jf in subagent_dir.glob("*.jsonl"):
        try:
            if jf.stat().st_size > 0:
                results.append(jf)
        except OSError:
            continue
    return results


# ---------------------------------------------------------------------------
# Project name cleanup
# ---------------------------------------------------------------------------

def clean_project_name(raw_project: str) -> str:
    """Map Claude Code dashed directory names to human-readable labels.

    e.g. '-Users-jane' -> 'home'
         '-Users-jane-projects-acme-api' -> 'acme/api'
    """
    if not raw_project:
        return "unknown"
    cleaned = re.sub(r"^-Users-[^-]+-?", "", raw_project)
    if not cleaned:
        return "home"
    parts = [p for p in cleaned.split("-") if p]
    if not parts:
        return "home"
    if len(parts) > 2:
        return "/".join(parts[-2:])
    return "/".join(parts)


# ---------------------------------------------------------------------------
# SQLite initialization
# ---------------------------------------------------------------------------

def init_sqlite_db(db_path: Path, schema: str, wal: bool = True) -> sqlite3.Connection:
    """Initialize a SQLite database with schema and pragmas.

    Uses WAL mode and busy_timeout by default (same pattern as measure.py).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(schema)
    return conn


def migrate_add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]):
    """Add columns to a table if they don't already exist.

    columns: dict of {column_name: column_type} e.g. {"slug": "TEXT", "score": "REAL"}
    """
    try:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_type in columns.items():
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        conn.commit()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens_from_text(text: str) -> int:
    """Estimate token count from text content (character count / 4)."""
    return int(len(text) / CHARS_PER_TOKEN)


def estimate_tokens_from_file(filepath: Path) -> int:
    """Estimate tokens by reading file content."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return int(len(content) / CHARS_PER_TOKEN)
    except (PermissionError, OSError):
        return 0
