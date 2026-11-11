#!/usr/bin/env python3
"""Token Optimizer - Bash coverage funnel (counters only, no behaviour change).

Every Bash result that reaches the PostToolUse hook ends in exactly one of five
gates. This module owns the gate vocabulary, a pure classifier that replays the
hook's own gate order on a (command, output) pair, and a tiny fail-open row
writer for the hook. It never changes what the model sees.

Gates (the order the hook applies them):
  error_or_short      empty/under 100 chars, failed command, or error words in
                      the output. These stay raw by design.
  already_compressed  carries Token Optimizer's own marker (PreToolUse got there).
  not_read_only       the command is not a read-only pipeline, so no compressor
                      was consulted. ``reason`` holds the reject category.
  under_10pct         a compressor ran and found under 10% to remove.
  compressed          the output was replaced by a smaller one.

Privacy: a row holds the gate, a fixed-vocabulary reason, the compressor pattern
name, and two token counts. No command text and no output text is stored.
The classifier is stdlib-only and imports the heavy modules lazily so the hook
path pays nothing for the replay code.
"""
from __future__ import annotations

import os
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

GATES = ("not_read_only", "under_10pct", "already_compressed", "compressed", "error_or_short")

GATE_LABELS = {
    "not_read_only": "Command shape not eligible",
    "under_10pct": "Compressor found under 10% to remove",
    "already_compressed": "Already compressed",
    "compressed": "Compressed",
    "error_or_short": "Error words, failed command, or under 100 chars",
}

# Fine-grained reasons for error_or_short, so the five-way split stays honest.
SHORT_REASONS = ("short", "failed_command", "error_words")

_MIN_CHARS = 100
_MIN_REMOVED_SHARE = 0.10
_DB_TIMEOUT_SECONDS = 0.05
_DB_BUSY_TIMEOUT_MS = 50
_RETENTION_DAYS = 120
_PRUNE_ODDS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bash_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_uuid TEXT,
    gate TEXT NOT NULL,
    reason TEXT,
    pattern TEXT,
    tokens INTEGER DEFAULT 0,
    removed_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bash_coverage_ts ON bash_coverage(timestamp);
"""

_UUID_PAT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_REASON_SAFE = re.compile(r"[^a-z0-9_-]+")
_STAGE_PREFIX = re.compile(r"^stage-\d+:")


def reject_category(reason) -> str:
    """Reduce pipeline_analyzer's reject reason to a command-free category.

    ``glued-operator-in-token:;`` -> ``glued-operator-in-token``;
    ``stage-1:not-whitelisted:cd`` -> ``not-whitelisted``. Anything after the
    first colon can carry a token of the user's command, so it is dropped.
    """
    text = _STAGE_PREFIX.sub("", str(reason or "").strip()).split(":", 1)[0].strip().lower()
    text = _REASON_SAFE.sub("-", text).strip("-")
    return text[:48] or "unknown"


_SAMPLE_CHARS = 16384


def tokens_of(text: str) -> int:
    """Token estimate that stays cheap on the hook path.

    The shared estimator is instant on ASCII but walks non-ASCII text one
    character at a time (about 1 ms per 1,000 characters), which would put a
    large Hebrew or CJK output over the hook budget for a counter. Past
    ``_SAMPLE_CHARS`` such text is estimated from its head and scaled. The hook
    and the replay both use this, so their figures agree.
    """
    if not text:
        return 0
    try:
        from token_estimate import estimate_tokens
        if len(text) <= _SAMPLE_CHARS or text.isascii():
            return int(estimate_tokens(text))
        return int(estimate_tokens(text[:_SAMPLE_CHARS]) * len(text) / _SAMPLE_CHARS)
    except Exception:
        return len(text.encode("utf-8", errors="replace")) // 4


_tokens = tokens_of


def _already_compressed(output: str) -> bool:
    return "[Full result archived" in output or "[bash_compress]" in output


def classify_result(command: str, output: str, is_error: bool = False) -> dict:
    """Replay the hook's gate order on one finished Bash result. Read-only.

    Returns ``{"gate", "reason", "pattern", "tokens", "removed"}``. Nothing is
    archived, logged, or emitted. ``is_error`` stands in for a non-zero exit
    code, which transcripts record only as a flag.
    """
    output = output if isinstance(output, str) else ""
    command = command if isinstance(command, str) else ""
    tokens = _tokens(output)
    row = {"gate": "error_or_short", "reason": "short", "pattern": "",
           "tokens": tokens, "removed": 0}
    if len(output) < _MIN_CHARS:
        return row
    if _already_compressed(output):
        row.update(gate="already_compressed", reason="marker")
        return row
    try:
        from post_run_classify import unsafe_compound_output
        if unsafe_compound_output(command):
            row.update(gate="not_read_only", reason="mixed_output")
            return row
    except Exception:
        pass  # the hook fails open on this check too
    compress_command = command
    try:
        from pipeline_analyzer import is_read_only_pipeline
        is_ro, why = is_read_only_pipeline(command)
    except Exception:
        is_ro, why = False, "analyzer-error"
    if not is_ro:
        # Same step as the hook: a harmless prefix (`cd /p;`, `export X=1 &&`)
        # does not hide the command from its pattern-specific compressor.
        try:
            from post_run_classify import eligible_effective, mode as _prc_mode
            if _prc_mode() == "on":
                effective = eligible_effective(command)
                if effective is not None:
                    is_ro, compress_command = True, effective
        except Exception:
            pass
    from bash_compress import _strip_ansi
    cleaned = _strip_ansi(output)
    if not is_ro:
        built = _build_removed(command, cleaned)
        if built is not None:
            row.update(gate="compressed", reason="build_output", pattern="build_output",
                       removed=built)
            return row
        row.update(gate="not_read_only", reason=reject_category(why))
        return row
    from bash_compress import _looks_like_failure, _detect_pattern, compress
    if _looks_like_failure(1 if is_error else 0, ""):
        row.update(reason="failed_command")
        return row
    from bash_compress_hook import _stdout_has_error_patterns
    if _stdout_has_error_patterns(cleaned):
        row.update(reason="error_words")
        return row
    pattern = _safe_pattern(_detect_pattern, command)
    row["pattern"] = pattern
    try:
        packed = compress(compress_command, cleaned, returncode=0, stderr="")
        packed = packed if isinstance(packed, str) else (
            packed[0] if isinstance(packed, (tuple, list)) and packed else str(packed))
    except Exception:
        row.update(gate="under_10pct", reason="compressor_error")
        return row
    before, after = _tokens(cleaned), _tokens(packed)
    if packed and packed != cleaned and before and (1.0 - after / before) >= _MIN_REMOVED_SHARE:
        row.update(gate="compressed", reason="read_only", removed=max(0, before - after))
        return row
    row.update(gate="under_10pct", reason="read_only")
    return row


def _safe_pattern(detect, command) -> str:
    try:
        return str(detect(command) or "")[:40]
    except Exception:
        return ""


def _build_removed(command: str, cleaned: str):
    """Tokens the build/test compressor would remove, or None. No side effects."""
    if len(cleaned.splitlines()) < 25:
        return None
    try:
        from build_output_compress import classify, classify_by_shape, compress as build_compress
        if not (classify(command) or classify_by_shape(cleaned, command)):
            return None
        packed = build_compress(command, cleaned)
        if not packed or packed == cleaned:
            return None
        before, after = _tokens(cleaned), _tokens(packed)
        if before and (1.0 - after / before) >= _MIN_REMOVED_SHARE:
            return max(0, before - after)
    except Exception:
        return None
    return None


def _snapshot_dir() -> Path:
    from plugin_env import resolve_snapshot_dir
    return resolve_snapshot_dir()


def _session_uuid(session_id):
    return session_id if session_id and _UUID_PAT.match(session_id) else None


def record(gate: str, reason: str = "", pattern: str = "", tokens: int = 0,
           removed: int = 0, session_id: str | None = None,
           snapshot_dir: Path | None = None) -> None:
    """Write one counter row. Fail-open: never raises, never blocks the hook."""
    if gate not in GATES:
        return
    conn = None
    try:
        directory = Path(snapshot_dir) if snapshot_dir is not None else _snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = directory / "trends.db"
        conn = sqlite3.connect(str(db), timeout=_DB_TIMEOUT_SECONDS)
        conn.execute(f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO bash_coverage (timestamp, session_uuid, gate, reason, pattern, tokens, removed_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), _session_uuid(session_id), gate,
             reject_category(reason) if reason else "", _REASON_SAFE.sub("_", pattern.lower())[:40],
             max(0, int(tokens)), max(0, int(removed))))
        if random.randrange(_PRUNE_ODDS) == 0:
            cutoff = (datetime.now() - timedelta(days=_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM bash_coverage WHERE timestamp < ?", (cutoff,))
        conn.commit()
        try:
            os.chmod(str(db), 0o600)
        except OSError:
            pass
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
