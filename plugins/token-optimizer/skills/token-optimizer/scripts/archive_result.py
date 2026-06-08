#!/usr/bin/env python3
"""Token Optimizer - PostToolUse Archive Result (standalone entry point).

Archives large tool results to disk so they survive compaction.
Standalone extraction for minimal startup overhead (~40ms vs ~135ms).

Security hardening:
  - 0o600 permissions on all written files
  - stdin capped at 1MB
  - Archive entries capped at 5MB with truncation marker
  - Session ID sanitized against path traversal
  - tool_use_id validated to alphanumeric + hyphens/underscores

SOURCE OF TRUTH for _sanitize_session_id: session_store.py.
SOURCE OF TRUTH for read_stdin_hook_input: hook_io.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import hashlib
import time

from bash_compress import _TOKEN_PATTERNS
from hook_io import read_stdin_hook_input
from plugin_env import resolve_snapshot_dir
from session_store import SessionStore, _sanitize_session_id as sanitize_sid

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4.0  # legacy proxy; retained only for the conservative MCP-cap proximity threshold
try:
    # Calibrated chars/token (~3.3 for code) — shared ratio basis for savings
    # estimates so tool_archive numbers match the rest of the pipeline.
    from token_estimate import CODE_CHARS_PER_TOKEN
except Exception:  # pragma: no cover - fail-open if shared estimator missing
    CODE_CHARS_PER_TOKEN = 3.3
_ARCHIVE_THRESHOLD = 4096       # chars: only archive results >= this size
_ARCHIVE_PREVIEW_SIZE = 1500    # chars: preview included in replacement output
_ARCHIVE_MAX_SIZE = 5_242_880   # 5MB: truncate responses beyond this
_STDIN_MAX_BYTES = _ARCHIVE_MAX_SIZE + 262_144  # 5MB response plus JSON overhead

# Plugin-data-aware paths (env > installed_plugins.json > legacy)
SNAPSHOT_DIR = resolve_snapshot_dir()
TRENDS_DB = SNAPSHOT_DIR / "trends.db"
_SAVINGS_DB_TIMEOUT_SECONDS = 0.05
_SAVINGS_DB_BUSY_TIMEOUT_MS = 50
_DEFAULT_SAVINGS_COST_PER_MTOK = 3.0  # Sonnet input rate; safe fallback for hook-only pricing.
_HOOK_INPUT_COST_PER_MTOK = {
    "gpt-5.5-pro": 30.0,
    "gpt-5.1-codex-mini": 0.25,
    "gpt-4o-mini": 0.15,
    "gpt-5-codex": 1.25,
    "gpt-5.1-codex": 1.25,
    "gpt-5.2-codex": 1.75,
    "gpt-5.3-codex": 1.75,
    "gpt-5-mini": 0.25,
    "gpt-5-nano": 0.05,
    "gpt-5": 1.25,
    "gpt-5.4": 2.5,
    "gpt-5.5": 5.0,
    "gpt-4o": 2.5,
}

_SAVINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS savings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tokens_saved INTEGER NOT NULL DEFAULT 0,
    cost_saved_usd REAL NOT NULL DEFAULT 0.0,
    session_id TEXT,
    detail TEXT
);
"""


def _chmod_private_file(path: Path) -> None:
    try:
        if path.exists() and not path.is_symlink():
            os.chmod(str(path), 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers (SOURCE OF TRUTH: measure.py — keep in sync)
# ---------------------------------------------------------------------------

def _sanitize_session_id(sid: str | None) -> str:
    return sanitize_sid(sid or "")




def _archive_dir_for_session(session_id: str) -> Path:
    """Return the archive directory for a given session."""
    sid = _sanitize_session_id(session_id)
    return SNAPSHOT_DIR / "tool-archive" / sid


def _redact_credentials(text: str) -> str:
    """Replace credential-matching substrings with [REDACTED] before archiving.

    Uses _TOKEN_PATTERNS from bash_compress — the same patterns that guard
    compression output — so both surfaces share one canonical allowlist.
    """
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def cleanup_old_archives(max_age_hours: int = 48, skip_session_id: str | None = None) -> int:
    """Delete tool-archive session directories older than max_age_hours.

    Best-effort: individual OSError is swallowed so a locked or missing
    directory never aborts the hook. Returns the count of removed dirs.
    """
    archive_root = SNAPSHOT_DIR / "tool-archive"
    if not archive_root.exists() or archive_root.is_symlink():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    skip_sid = _sanitize_session_id(skip_session_id) if skip_session_id else None
    for session_dir in archive_root.iterdir():
        if skip_sid and session_dir.name == skip_sid:
            continue
        if session_dir.is_symlink() or not session_dir.is_dir():
            continue
        try:
            if os.lstat(session_dir).st_mtime < cutoff:
                shutil.rmtree(session_dir, ignore_errors=True)
                if not session_dir.exists():
                    removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Savings event logging (mirrors read_cache.py pattern — self-contained so
# this hook stays fast without importing measure.py at startup)
# ---------------------------------------------------------------------------

def _log_savings_event(event_type: str, tokens_saved: int, session_id: str | None, detail: str) -> None:
    """Write a savings event row to the trends DB.

    This is intentionally self-contained and fail-fast: archive_result.py runs
    in the PostToolUse hot path, so it must never import measure.py or wait on
    a locked dashboard DB. Never raises.
    """
    if tokens_saved <= 0:
        return
    conn: sqlite3.Connection | None = None
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(SNAPSHOT_DIR), 0o700)
        conn = sqlite3.connect(str(TRENDS_DB), timeout=_SAVINGS_DB_TIMEOUT_SECONDS)
        _chmod_private_file(TRENDS_DB)
        conn.execute(f"PRAGMA busy_timeout={_SAVINGS_DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SAVINGS_SCHEMA)
        cost_per_mtok = _estimate_savings_cost_per_mtok()
        cost_saved = tokens_saved * cost_per_mtok / 1_000_000
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), event_type, tokens_saved, cost_saved, session_id, detail),
        )
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            _chmod_private_file(Path(str(TRENDS_DB) + suffix))
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _estimate_savings_cost_per_mtok() -> float:
    """Return a tiny hook-safe input-token price estimate.

    Full model-aware attribution lives in measure.py. This hot-path hook keeps
    the import graph small and uses only env hints, undercounting to Sonnet when
    the active model is unknown.
    """
    override = os.environ.get("TOKEN_OPTIMIZER_COST_PER_MTOK", "").strip()
    if override:
        try:
            value = float(override)
            if value > 0:
                return value
        except ValueError:
            pass

    model = (
        os.environ.get("CLAUDE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("CODEX_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("MODEL")
        or ""
    ).lower()
    if "opus" in model:
        return 5.0
    if "haiku" in model:
        return 1.0
    for alias, rate in _HOOK_INPUT_COST_PER_MTOK.items():
        if model == alias or model.startswith(alias + "-"):
            return rate
    return _DEFAULT_SAVINGS_COST_PER_MTOK


# ---------------------------------------------------------------------------
# MCP output-cap savings estimation
# ---------------------------------------------------------------------------

# Fraction of the cap we assume was saved by the native truncation.
# Rationale: we cannot know the true pre-cap size (Claude Code truncates before
# PostToolUse fires). A conservative fixed estimate of 50% of the cap is used:
# i.e., if the cap is 10 000 tokens and the result is at the cap, we assume the
# true output was at least 1.5x the cap, so the native cap saved ~0.5x the cap.
# This deliberately understates rather than overstates; Phase 2 will replace
# this with actual measurement via a PreToolUse size probe.
_MCP_CAP_ESTIMATED_RATIO = 0.5   # fraction of cap_tokens logged as saved
# How close to the cap (in chars) a result must be before we flag it as capped.
# 90% prevents false positives from MCP tools that naturally return near-cap
# amounts without being truncated.
_MCP_CAP_PROXIMITY_PCT = 0.90    # result must be >= 90% of cap_chars to flag

_MCP_CAP_CACHE_UNSET = object()
_MCP_CAP_TOKENS_CACHE = _MCP_CAP_CACHE_UNSET


def _resolve_mcp_cap_tokens() -> int | None:
    """Read explicit MAX_MCP_OUTPUT_TOKENS from env or user settings.json.

    Resolution order mirrors the Claude Code env injection chain:
      1. Process environment (set by Claude Code from settings.json at startup)
      2. ~/.claude/settings.json "env" block (direct read — for callers running
         outside the Claude Code process, e.g. in tests)

    Never raises. Returns None when no explicit cap is configured so we do not
    invent savings from an assumed platform default.
    """
    global _MCP_CAP_TOKENS_CACHE
    if _MCP_CAP_TOKENS_CACHE is not _MCP_CAP_CACHE_UNSET:
        return _MCP_CAP_TOKENS_CACHE

    # 1. Env var (most common — Claude Code injects this from settings.json)
    env_val = os.environ.get("MAX_MCP_OUTPUT_TOKENS", "").strip()
    if env_val:
        try:
            v = int(env_val)
            if v > 0:
                _MCP_CAP_TOKENS_CACHE = v
                return v
        except ValueError:
            pass

    # 2. settings.json "env" block — for out-of-process callers
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        raw = settings.get("env", {}).get("MAX_MCP_OUTPUT_TOKENS", "")
        if raw:
            v = int(str(raw).strip())
            if v > 0:
                _MCP_CAP_TOKENS_CACHE = v
                return v
    except Exception:
        pass

    _MCP_CAP_TOKENS_CACHE = None
    return None


def _maybe_log_mcp_cap_savings(tool_name: str, original_char_count: int, session_id: str | None) -> None:
    """Log estimated native MCP-cap savings independently of archive writes."""
    if "__" not in tool_name:
        return
    cap_tokens = _resolve_mcp_cap_tokens()
    if cap_tokens is None:
        return
    cap_chars = cap_tokens * CHARS_PER_TOKEN
    cap_threshold = int(cap_chars * _MCP_CAP_PROXIMITY_PCT)
    if original_char_count < cap_threshold:
        return
    estimated_saved = max(0, int(cap_tokens * _MCP_CAP_ESTIMATED_RATIO))
    if estimated_saved <= 0:
        return
    _log_savings_event(
        "mcp_cap",
        estimated_saved,
        session_id=session_id,
        detail=(
            f"estimated: {tool_name} result at cap "
            f"({original_char_count:,} chars >= "
            f"{cap_threshold:,} threshold, "
            f"cap={cap_tokens:,} tok, "
            f"assumed saved ~{estimated_saved:,} tok [estimated])"
        ),
    )


def _tool_response_to_text(value) -> str:
    """Normalize Claude PostToolUse response shapes into archiveable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_tool_response_to_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("stdout", "stderr", "error", "text", "output", "result", "content"):
            if key not in value:
                continue
            text = _tool_response_to_text(value.get(key))
            if not text:
                continue
            if key in ("stdout", "stderr", "error"):
                parts.append(f"{key}:\n{text}")
            else:
                parts.append(text)
        if parts:
            return "\n\n".join(parts)
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _ensure_private_archive_dir(archive_dir: Path) -> bool:
    try:
        if archive_dir.exists():
            st = archive_dir.lstat()
            if archive_dir.is_symlink() or not archive_dir.is_dir():
                return False
            if getattr(st, "st_nlink", 1) < 1:
                return False
        archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(archive_dir), 0o700)
        return True
    except OSError:
        return False


def _atomic_write_json(path: Path, payload: dict) -> None:
    if path.exists() and (path.is_symlink() or path.lstat().st_nlink > 1):
        raise OSError("unsafe archive entry path")
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_manifest_line(path: Path, payload: dict) -> None:
    if path.exists() and (path.is_symlink() or path.lstat().st_nlink > 1):
        raise OSError("unsafe archive manifest path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        os.chmod(path, 0o600)
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Progressive-disclosure spine (U2): reusable archive-original + pointer.
#
# Lets non-MCP / PreToolUse compressors (first-read code, bash, structured
# data) persist the FULL original and hand the model a retrievable pointer,
# exactly like the MCP eviction path. Cache-safety is structural: these run in
# arrival hooks (PreToolUse / PostToolUse) BEFORE the content's first API send,
# so they never mutate already-cached context. expand_archived locates an entry
# by filename stem, so a derived key retrieves identically to a tool_use_id --
# no expand change is needed.
# ---------------------------------------------------------------------------

def derive_archive_key(session_id: str | None, file_path: str, mtime_ns: int) -> str:
    """Stable archive key for PreToolUse paths that have no tool_use_id.

    sha256(session_id + file_path + mtime_ns)[:16]. Hex output satisfies
    expand_archived's [a-zA-Z0-9_-]+ key validation.
    """
    raw = f"{session_id or ''}|{file_path}|{mtime_ns}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_archive_pointer(preview: str, original_chars: int, key: str) -> str:
    """Standard progressive-disclosure pointer appended after a compressed preview."""
    return (
        f"{preview}\n\n"
        f"[Full result archived ({original_chars:,} chars). "
        f"Retrieve with: expand {key}]"
    )


def archive_original(content: str, session_id: str | None, key: str,
                     tool_name: str, quiet: bool = True) -> str | None:
    """Persist a full original (credential-redacted) under `key`; return the key.

    Reusable by non-MCP / PreToolUse compressors so nothing is ever lost. Mirrors
    the MCP archive path (same dir layout + manifest) so expand_archived retrieves
    it unchanged. Fail-open: returns None on any failure (caller serves raw).
    """
    try:
        if not key or not re.match(r"^[a-zA-Z0-9_-]+$", key):
            return None
        archive_dir = _archive_dir_for_session(session_id or "unknown")
        if not _ensure_private_archive_dir(archive_dir):
            return None
        safe_response = _redact_credentials(content)
        meta = {
            "tool_name": tool_name,
            "tool_use_id": key,
            "chars": len(safe_response),
            "original_chars": len(content),
            "tokens_est": int(len(safe_response) / CODE_CHARS_PER_TOKEN),
            "truncated": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "archived_from": "compress_with_preservation",
        }
        entry_path = archive_dir / f"{key}.json"
        _atomic_write_json(entry_path, {**meta, "response": safe_response})
        _append_manifest_line(archive_dir / "manifest.jsonl", meta)
        return key
    except Exception:
        if not quiet:
            print(f"[Tool Archive] archive_original failed for {tool_name}; serving raw.", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Structure-aware MCP output compression
# ---------------------------------------------------------------------------

def _detect_output_type(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped[:100_000])
            return "json"
        except (json.JSONDecodeError, RecursionError):
            pass
    lines = stripped.splitlines()[:50]
    if len(lines) > 5:
        path_like = sum(1 for ln in lines if "/" in ln or "\\" in ln)
        if path_like > len(lines) * 0.6:
            return "paths"
    if len(lines) > 5:
        sep_count = sum(1 for ln in lines if set(ln.strip()) <= set("-=| +") and ln.strip())
        if sep_count >= 1:
            return "table"
    return "text"


def _compress_mcp_preview(text: str, output_type: str) -> str:
    if output_type == "json":
        return _compress_mcp_json(text)
    if output_type == "paths":
        return _compress_mcp_paths(text)
    if output_type == "table":
        return _compress_mcp_table(text)
    return text[:_ARCHIVE_PREVIEW_SIZE]


# Value-preserving columnar compression thresholds (U3).
_TABULAR_MIN_ROWS = 3            # too few rows to be worth a schema header
_TABULAR_MAX_COLS = 40          # wider than this is not really tabular
_TABULAR_CELL_CAP = 100         # per-cell char cap (keeps values, bounds width)
_TABULAR_TOTAL_CAP = 8192       # if the columnar form exceeds this, evict instead
_TABULAR_MIN_REDUCTION = 0.40   # must be >=40% smaller than original to engage


def _compress_mcp_json_tabular(data, original_text: str):
    """Value-preserving columnar compression for homogeneous arrays-of-dicts.

    Strips the repeated per-row schema (emit keys once) while KEEPING values, so
    the model can still read any cell inline instead of paying a full re-expand.
    Returns the columnar string, or None to fall back to the eviction preview
    when the data isn't tabular enough or the result isn't a clear win.
    """
    try:
        if not isinstance(data, list) or len(data) < _TABULAR_MIN_ROWS:
            return None
        dict_rows = [r for r in data if isinstance(r, dict)]
        if len(dict_rows) < max(_TABULAR_MIN_ROWS, int(len(data) * 0.8)):
            return None  # not predominantly homogeneous dicts
        # Ordered union of keys (first-seen order), bounded width.
        cols: list[str] = []
        seen = set()
        for row in dict_rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(str(k))
        if not cols or len(cols) > _TABULAR_MAX_COLS:
            return None

        def _cell(v) -> str:
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            s = s.replace("\n", " ").replace("\t", " ")
            return s[:_TABULAR_CELL_CAP] + ("…" if len(s) > _TABULAR_CELL_CAP else "")

        lines = [
            f"JSON array ({len(data)} items), columnar (schema-stripped, values preserved):",
            "cols: " + ", ".join(cols),
        ]
        for row in data:
            if isinstance(row, dict):
                lines.append("- " + " | ".join(_cell(row.get(c, "")) for c in cols))
            else:
                lines.append("- " + _cell(row))
        result = "\n".join(lines)

        # Only engage when it's a clear win and stays within the inline budget.
        if len(result) > _TABULAR_TOTAL_CAP:
            return None
        if len(result) > len(original_text) * (1.0 - _TABULAR_MIN_REDUCTION):
            return None
        return result
    except Exception:
        return None


def _compress_mcp_json(text: str) -> str:
    try:
        data = json.loads(text[:500_000])
    except (json.JSONDecodeError, RecursionError):
        return text[:_ARCHIVE_PREVIEW_SIZE]

    # U3: value-preserving columnar path for homogeneous arrays-of-dicts.
    # Keeps values usable inline so the model rarely needs a full re-expand;
    # falls through to the eviction preview below when it isn't a clear win.
    tabular = _compress_mcp_json_tabular(data, text)
    if tabular is not None:
        return tabular

    parts: list[str] = []
    if isinstance(data, dict):
        parts.append(f"JSON object ({len(data)} keys):")
        for key in list(data.keys())[:15]:
            val = data[key]
            if isinstance(val, list):
                parts.append(f"  {key}: [{len(val)} items]")
            elif isinstance(val, dict):
                subkeys = list(val.keys())[:5]
                suffix = "..." if len(val) > 5 else ""
                parts.append(f"  {key}: {{{', '.join(subkeys)}{suffix}}}")
            elif isinstance(val, str) and len(val) > 80:
                parts.append(f'  {key}: "{val[:77]}..."')
            else:
                parts.append(f"  {key}: {json.dumps(val)[:80]}")
        if len(data) > 15:
            parts.append(f"  ... ({len(data) - 15} more keys)")
    elif isinstance(data, list):
        parts.append(f"JSON array ({len(data)} items):")
        for item in data[:5]:
            if isinstance(item, dict):
                keys = list(item.keys())[:5]
                suffix = "..." if len(item) > 5 else ""
                parts.append(f"  {{{', '.join(keys)}{suffix}}}")
            else:
                parts.append(f"  {json.dumps(item)[:80]}")
        if len(data) > 5:
            parts.append(f"  ... ({len(data) - 5} more items)")

    result = "\n".join(parts)
    return result[:_ARCHIVE_PREVIEW_SIZE] if len(result) > _ARCHIVE_PREVIEW_SIZE else result


def _compress_mcp_paths(text: str) -> str:
    lines = text.strip().splitlines()
    dirs: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if "/" in stripped:
            dir_name = stripped.rsplit("/", 1)[0] if "/" in stripped else "."
            dirs[dir_name] = dirs.get(dir_name, 0) + 1

    parts = [f"{len(lines)} paths across {len(dirs)} directories:"]
    sorted_dirs = sorted(dirs.items(), key=lambda x: -x[1])
    for dir_name, count in sorted_dirs[:10]:
        parts.append(f"  {dir_name}/ ({count} files)")
    if len(sorted_dirs) > 10:
        parts.append(f"  ... ({len(sorted_dirs) - 10} more directories)")

    result = "\n".join(parts)
    return result[:_ARCHIVE_PREVIEW_SIZE] if len(result) > _ARCHIVE_PREVIEW_SIZE else result


def _compress_mcp_table(text: str) -> str:
    lines = text.strip().splitlines()
    header = lines[:2]
    data = [ln for ln in lines[2:] if ln.strip()]
    result = header + data[:10]
    if len(data) > 10:
        result.append(f"... ({len(data) - 10} more rows, {len(data)} total)")
    return "\n".join(result)[:_ARCHIVE_PREVIEW_SIZE]


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def archive_result(quiet: bool = False) -> None:
    """PostToolUse hook handler: archive large tool results to disk.

    Reads hook JSON from stdin. If tool_response >= _ARCHIVE_THRESHOLD chars,
    saves the full result to disk and (for MCP tools) outputs a trimmed
    replacement via stdout with updatedMCPToolOutput.

    Logs a savings event only when MCP output is actually replaced. Native
    Bash/Read/etc. archives are durability metadata unless a PreToolUse path
    suppresses the original output before it enters context.
    """
    hook_input = read_stdin_hook_input(_STDIN_MAX_BYTES)
    if not hook_input:
        return

    tool_name = hook_input.get("tool_name", "")
    tool_use_id = hook_input.get("tool_use_id", "")
    tool_response = _tool_response_to_text(hook_input.get("tool_response", ""))
    session_id = hook_input.get("session_id", "")

    if not tool_response:
        return

    original_char_count = len(tool_response)
    _maybe_log_mcp_cap_savings(tool_name, original_char_count, session_id)

    if original_char_count < _ARCHIVE_THRESHOLD:
        return

    if not tool_use_id or not session_id:
        if not quiet:
            print("[Tool Archive] Missing tool_use_id or session_id, skipping.", file=sys.stderr)
        return

    # Sanitize tool_use_id
    if not re.match(r'^[a-zA-Z0-9_-]+$', tool_use_id):
        if not quiet:
            print("[Tool Archive] Invalid tool_use_id, skipping", file=sys.stderr)
        return

    now = datetime.now(timezone.utc)
    truncated = original_char_count > _ARCHIVE_MAX_SIZE

    if truncated:
        tool_response = tool_response[:_ARCHIVE_MAX_SIZE] + (
            f"\n\n[TRUNCATED at 5MB. Original size: {original_char_count} chars]"
        )

    char_count = _ARCHIVE_MAX_SIZE if truncated else original_char_count
    token_est = int(char_count / CODE_CHARS_PER_TOKEN)

    # Best-effort TTL cleanup: runs only when we're about to write (after
    # early-exit checks), not on every PostToolUse invocation.
    try:
        cleanup_old_archives(max_age_hours=48, skip_session_id=session_id)
    except Exception:
        pass

    archive_dir = _archive_dir_for_session(session_id)
    if not _ensure_private_archive_dir(archive_dir):
        if not quiet:
            print(f"[Tool Archive] Unsafe archive directory for {tool_name}; leaving output unchanged.", file=sys.stderr)
        return

    meta = {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "chars": char_count,
        "original_chars": original_char_count,
        "tokens_est": token_est,
        "truncated": truncated,
        "timestamp": now.isoformat(),
        "archived_from": "PostToolUse",
    }

    # Redact credential patterns before writing to disk.
    # Performed on the (possibly truncated) response so no plaintext secrets
    # ever reach the archive file, even transiently.
    safe_response = _redact_credentials(tool_response)

    try:
        entry_path = archive_dir / f"{tool_use_id}.json"
        _atomic_write_json(entry_path, {**meta, "response": safe_response})

        manifest_path = archive_dir / "manifest.jsonl"
        _append_manifest_line(manifest_path, meta)
    except OSError:
        if not quiet:
            print(f"[Tool Archive] Failed to archive {tool_name} result; leaving output unchanged.", file=sys.stderr)
        return

    if not quiet:
        print(f"[Tool Archive] Archived {tool_name} result ({char_count:,} chars, ~{token_est:,} tokens): {tool_use_id}", file=sys.stderr)

    store = None
    try:
        tool_type = "mcp" if "__" in tool_name else tool_name.lower()
        command_or_path = hook_input.get("tool_input", {}).get("command") or hook_input.get("tool_input", {}).get("file_path") or tool_name
        output_hash = hashlib.sha256(tool_response[:10000].encode("utf-8", errors="replace")).hexdigest()[:16]
        store = SessionStore(session_id)
        store.insert_tool_output(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_type=tool_type,
            command_or_path=str(command_or_path)[:500],
            output_hash=output_hash,
            output_chars=char_count,
            output_tokens_est=token_est,
            compressed_preview=safe_response[:1500],
        )
    except Exception:
        pass
    finally:
        if store is not None:
            store.close()

    # For MCP tools (tool_name contains "__"): output replacement via stdout
    # No pressure gate here: the compressed replacement SAVES tokens.
    # Suppressing it would cause the full uncompressed response to flow through.
    if "__" in tool_name:
        output_type = _detect_output_type(safe_response)
        preview = _compress_mcp_preview(safe_response, output_type)
        suffix = f" ({output_type})" if output_type != "text" else ""
        if original_char_count > _ARCHIVE_MAX_SIZE:
            replacement = preview + f"\n\n[Full result archived ({original_char_count:,} chars{suffix}, truncated to 5MB).]"
        else:
            replacement = preview + f"\n\n[Full result archived ({char_count:,} chars{suffix}).]"
        original_tokens = int(original_char_count / CODE_CHARS_PER_TOKEN)
        replacement_tokens = int(len(replacement) / CODE_CHARS_PER_TOKEN)
        tokens_saved = max(0, original_tokens - replacement_tokens)
        _log_savings_event(
            "tool_archive",
            tokens_saved,
            session_id=session_id,
            detail=(
                f"replaced {tool_name} "
                f"({original_char_count:,} chars -> {len(replacement):,} chars)"
            ),
        )
        output = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": replacement,
            }
        })
        print(output)


if __name__ == "__main__":
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    archive_result(quiet=quiet)
