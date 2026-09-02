#!/usr/bin/env python3
"""Read-only reader for VS Code Copilot debug-logs and OTel traces.

Parses ``workspaceStorage/.../GitHub.copilot-chat/debug-logs/.../main.jsonl``
to extract per-request cost (``copilotUsageNanoAiu``), token counts, model,
context breakdown, sidecar file sizes, and tool-call telemetry.

An optional fallback reads ``agent-traces.db`` (SQLite, OTel spans) when
debug-logs are not available.

Design constraints (matching hermes_state.py and codex_state.py):

- **Pure stdlib only.**  sqlite3 is OK; no third-party packages.
- **Strictly read-only.**  Never writes any file.  OTel reader uses a
  ``?mode=ro&immutable=1`` URI.  Every function degrades gracefully
  (returns ``[]`` / empty dict) on missing, locked, or corrupt input.
- **No hardcoded user paths.**  Discovery uses per-OS default bases plus
  optional ``extra_bases`` override.  All paths resolved and canonicalised
  before scanning to prevent the Windows double-count bug (lowercase vs
  uppercase drive letter) and macOS case-insensitive mount variants.
- **No network calls.**  All operations are local filesystem reads.
- **Python 3.9 compatible.**  No walrus operators on mandatory paths,
  no ``match`` statements, union-type hints use ``Optional[]``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COPILOT_CHAT_SUBDIR = "GitHub.copilot-chat"
_DEBUG_LOGS_SUBDIR = "debug-logs"
_MAIN_JSONL = "main.jsonl"

# XML tag names we extract from inputMessages blobs.
_CONTEXT_TAGS = (
    "attachments",
    "editorContext",
    "workspace_info",
    "userMemory",
    "repoMemory",
    "sessionMemory",
    "reminderInstructions",
    "context",
    "userRequest",
)

# Attachment tag pattern: <attachment filePath="..." ...>content</attachment>
_ATTACHMENT_RE = re.compile(
    r'<attachment\s[^>]*filePath="([^"]*)"[^>]*>(.*?)</attachment>',
    re.DOTALL,
)

# Pre-compiled per-tag block patterns (compiled once; the tag set is static).
_CONTEXT_TAG_RES = tuple(
    (
        tag,
        re.compile(
            r"<" + re.escape(tag) + r"(?:\s[^>]*)?>(.+?)</" + re.escape(tag) + r">",
            re.DOTALL,
        ),
    )
    for tag in _CONTEXT_TAGS
)

# Guard: blobs larger than 4 MB are not parsed for context breakdown.
_MAX_INPUT_MESSAGES_BYTES = 4 * 1024 * 1024

_DATA_SOURCE_DEBUGLOGS = "copilot_vscode_debuglogs"
_DATA_SOURCE_OTEL = "copilot_vscode_otel"


# ---------------------------------------------------------------------------
# Path discovery + canonicalisation
# ---------------------------------------------------------------------------

def _canon_key(path: Path) -> str:
    """Return a stable string key for deduplication.

    On macOS and Windows, case-fold so ``/Users/Alex/...`` and
    ``/users/alex/...`` (or ``C:\\...`` vs ``c:\\...``) map to the same key.
    On Linux, preserve case.
    """
    s = str(path.resolve())
    if sys.platform in ("darwin", "win32"):
        return s.lower()
    return s


def discover_log_dirs(
    extra_bases: Optional[List[Path]] = None,
) -> List[Path]:
    """Return all ``debug-logs`` directories found across VS Code storage roots.

    Scans each workspace-storage root for
    ``<hash>/GitHub.copilot-chat/debug-logs/`` subdirs.

    Bases checked (per-OS):

    * macOS:   ~/Library/Application Support/Code/User/workspaceStorage
               (also "Code - Insiders", "VSCodium")
    * Linux:   ~/.config/Code/User/workspaceStorage
               (also "Code - Insiders", "VSCodium")
    * Windows: %APPDATA%/Code/User/workspaceStorage
               (also "Code - Insiders", "VSCodium")

    Duplicate paths (same resolved canonical form) are deduplicated before
    scanning so the same directory is never returned twice.
    """
    raw_bases: List[Path] = list(extra_bases or [])
    raw_bases.extend(_default_bases())

    # Canonicalise and deduplicate.
    seen_keys: set = set()
    unique_bases: List[Path] = []
    for b in raw_bases:
        try:
            resolved = b.resolve()
        except OSError:
            resolved = b
        key = _canon_key(resolved)
        if key not in seen_keys:
            seen_keys.add(key)
            unique_bases.append(resolved)

    log_dirs: List[Path] = []
    seen_log_keys: set = set()

    for base in unique_bases:
        if not base.is_dir():
            continue
        # Walk all <workspace-hash> subdirs.
        try:
            hash_dirs = [d for d in base.iterdir() if d.is_dir()]
        except OSError:
            continue
        for hash_dir in hash_dirs:
            candidate = (
                hash_dir / _COPILOT_CHAT_SUBDIR / _DEBUG_LOGS_SUBDIR
            )
            if candidate.is_dir():
                key = _canon_key(candidate)
                if key not in seen_log_keys:
                    seen_log_keys.add(key)
                    log_dirs.append(candidate)

    return log_dirs


def _default_bases() -> List[Path]:
    """Return per-OS default workspaceStorage bases (may not exist)."""
    home = Path.home()
    plat = sys.platform

    if plat == "darwin":
        app_support = home / "Library" / "Application Support"
        names = ["Code", "Code - Insiders", "VSCodium"]
        return [app_support / n / "User" / "workspaceStorage" for n in names]

    if plat == "win32":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        base = Path(appdata)
        names = ["Code", "Code - Insiders", "VSCodium"]
        return [base / n / "User" / "workspaceStorage" for n in names]

    # Linux and everything else
    config = home / ".config"
    names = ["Code", "Code - Insiders", "VSCodium"]
    return [config / n / "User" / "workspaceStorage" for n in names]


# ---------------------------------------------------------------------------
# JSONL parsing helpers
# ---------------------------------------------------------------------------

def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        # Parse through float first so a float-shaped string ("1234.0",
        # common in JSON exports) doesn't collapse to the default the way
        # int("1234.0") would. int() still truncates toward zero, matching
        # prior behavior for genuine float inputs. OverflowError guards
        # against float("inf")-style values.
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _parse_turn_index(event_name: str) -> Optional[int]:
    """Parse ``turn_start:N`` → N, or return None for non-turn events."""
    if not event_name.startswith("turn_start:"):
        return None
    suffix = event_name[len("turn_start:"):]
    try:
        return int(suffix)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Context breakdown parsing
# ---------------------------------------------------------------------------

def _parse_context_breakdown(
    input_messages_str: str,
) -> Optional[Dict[str, Any]]:
    """Parse XML-tagged context blocks from an inputMessages JSON string.

    Returns a dict of ``{tag: chars}`` plus an ``attachments`` list,
    or None when the blob is too large or cannot be parsed.

    The inputMessages field is a JSON-stringified array of chat message
    objects.  We concatenate all text content, then regex-scan for
    named XML tag blocks.
    """
    raw_bytes = input_messages_str.encode("utf-8", errors="replace")
    if len(raw_bytes) > _MAX_INPUT_MESSAGES_BYTES:
        logger.debug(
            "[copilot_vscode] inputMessages blob (%d bytes) exceeds 4 MB limit; skipping.",
            len(raw_bytes),
        )
        return None

    try:
        messages = json.loads(input_messages_str)
    except (json.JSONDecodeError, ValueError):
        logger.debug("[copilot_vscode] inputMessages is not valid JSON; skipping breakdown.")
        return None

    if not isinstance(messages, list):
        return None

    # Concatenate all text content from messages.
    parts: List[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if isinstance(text, str):
                        parts.append(text)

    blob = "\n".join(parts)

    # Extract per-tag char counts.
    tag_chars: Dict[str, int] = {}
    for tag, pattern in _CONTEXT_TAG_RES:
        chars = 0
        for m in pattern.finditer(blob):
            chars += len(m.group(1))
        if chars > 0:
            tag_chars[tag] = chars

    # Extract individual attachments.
    attachment_list: List[Dict[str, Any]] = []
    for m in _ATTACHMENT_RE.finditer(blob):
        file_path = m.group(1)
        content = m.group(2)
        attachment_list.append({"path": file_path, "chars": len(content)})

    return {"tag_chars": tag_chars, "attachments": attachment_list}


# ---------------------------------------------------------------------------
# Sidecar file reader
# ---------------------------------------------------------------------------

def _sidecar_chars(session_dir: Path, rel_or_abs: str) -> Optional[int]:
    """Return character length of a sidecar file, or None if missing/unreadable.

    The field value from the JSONL event may be an absolute path or a
    relative path (relative to the session dir).
    """
    if not rel_or_abs:
        return None
    candidate = Path(rel_or_abs)
    if not candidate.is_absolute():
        candidate = session_dir / rel_or_abs
    # Confine to the session dir: legitimate sidecars are co-located. A crafted
    # debug-log event with systemPromptFile="/etc/passwd" would otherwise leak
    # arbitrary file existence + size.
    try:
        if not candidate.resolve(strict=False).is_relative_to(session_dir.resolve(strict=False)):
            return None
    except (OSError, ValueError):
        return None
    try:
        return len(candidate.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Session-level main.jsonl parser
# ---------------------------------------------------------------------------

def _parse_session(
    session_dir: Path,
    workspace_hash: str,
) -> Dict[str, Any]:
    """Parse a single ``<session_uuid>/main.jsonl`` into a session dict.

    Unknown event types are silently ignored (schema-evolution tolerance).
    Malformed JSONL lines are silently skipped.
    """
    session_id = session_dir.name
    main_jsonl = session_dir / _MAIN_JSONL

    # Per-session accumulators.
    requests: List[Dict[str, Any]] = []
    context_breakdown: Dict[str, int] = {}
    attachments: List[Dict[str, Any]] = []
    _seen_attachment_paths: set = set()  # O(1) dedup vs O(n) list scan
    tool_calls: List[Dict[str, Any]] = []
    user_messages_count = 0

    # Read title from sidecars first (authoritative source per spec).
    # Falls back to agent_response in main.jsonl only when no sidecar title exists.
    title: Optional[str] = _read_title_from_sidecars(session_dir)

    # Tracking state across lines.
    current_turn = 0
    request_index = 0
    earliest_ts: Optional[float] = None
    # For "first llm_request per user turn" guard on context breakdown.
    turn_had_context: set = set()

    try:
        with open(main_jsonl, encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    evt = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if not isinstance(evt, dict):
                    continue

                name = evt.get("name", "")
                attrs = evt.get("attrs") or {}
                if not isinstance(attrs, dict):
                    attrs = {}

                # Track the earliest event timestamp so the session buckets to
                # the day it actually happened, not the rollup/collection day.
                _ev_ts = evt.get("ts", evt.get("timestamp", attrs.get("ts")))
                if _ev_ts is not None:
                    try:
                        _ev_ts_f = float(_ev_ts)
                        if _ev_ts_f > 1e12:  # epoch ms → s
                            _ev_ts_f /= 1000.0
                        if earliest_ts is None or _ev_ts_f < earliest_ts:
                            earliest_ts = _ev_ts_f
                    except (TypeError, ValueError):
                        pass

                # -- user_message --
                if name == "user_message":
                    user_messages_count += 1
                    # (content captured implicitly; not stored in requests)
                    continue

                # -- turn_start:N --
                turn_idx = _parse_turn_index(name)
                if turn_idx is not None:
                    current_turn = turn_idx
                    continue

                # -- llm_request --
                if name == "llm_request":
                    input_tokens = _safe_int(attrs.get("inputTokens"))
                    output_tokens = _safe_int(attrs.get("outputTokens"))
                    cached_tokens = _safe_int(attrs.get("cachedTokens"))
                    # _safe_int, not int(): a float-as-string ("1234.5") would
                    # otherwise ValueError and abort the whole session parse.
                    nano_aiu_raw = attrs.get("copilotUsageNanoAiu")
                    nano_aiu = _safe_int(nano_aiu_raw) if nano_aiu_raw is not None else None
                    model = _safe_str(attrs.get("model"))
                    ttft_raw = attrs.get("ttft")
                    ttft_ms = _safe_int(ttft_raw) if ttft_raw is not None else None

                    # Sidecar sizes.
                    sp_file = attrs.get("systemPromptFile") or ""
                    tools_file = attrs.get("toolsFile") or ""
                    system_prompt_chars = _sidecar_chars(session_dir, sp_file)
                    tools_chars = _sidecar_chars(session_dir, tools_file)

                    # Context breakdown — first llm_request per turn only.
                    if current_turn not in turn_had_context:
                        input_messages_str = attrs.get("inputMessages")
                        if isinstance(input_messages_str, str) and input_messages_str:
                            parsed = _parse_context_breakdown(input_messages_str)
                            if parsed is not None:
                                turn_had_context.add(current_turn)
                                for tag, chars in parsed["tag_chars"].items():
                                    context_breakdown[tag] = (
                                        context_breakdown.get(tag, 0) + chars
                                    )
                                for att in parsed["attachments"]:
                                    # Deduplicate attachments by path (set = O(1)).
                                    if att["path"] not in _seen_attachment_paths:
                                        _seen_attachment_paths.add(att["path"])
                                        attachments.append(att)

                    req = {
                        "request_id": f"{session_id}:{current_turn}:{request_index}",
                        "turn": current_turn,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cached_tokens": cached_tokens,
                        "nano_aiu": nano_aiu,
                        "ttft_ms": ttft_ms,
                        "system_prompt_chars": system_prompt_chars,
                        "tools_chars": tools_chars,
                    }
                    requests.append(req)
                    request_index += 1
                    continue

                # -- tool_call --
                if name == "tool_call":
                    args_raw = attrs.get("args") or ""
                    result_raw = attrs.get("result") or ""
                    tool_calls.append(
                        {
                            "name": _safe_str(attrs.get("name")) or "",
                            "dur_ms": _safe_int(attrs.get("dur")),
                            "status": _safe_str(attrs.get("status")),
                            "args_chars": len(str(args_raw)),
                            "result_chars": len(str(result_raw)),
                        }
                    )
                    continue

                # -- agent_response (in main.jsonl) — fallback when no sidecar title --
                if name == "agent_response" and title is None:
                    response_raw = attrs.get("response")
                    if isinstance(response_raw, str) and response_raw.strip():
                        title = response_raw.strip()[:200]
                    continue

                # Unknown event type — silently ignored per KTD8 / schema-evolution tolerance.

    except OSError as exc:
        logger.debug(
            "[copilot_vscode] Cannot read %s: %s", main_jsonl, exc
        )
        return _empty_session(session_id, workspace_hash)

    # Compute totals.
    total_input = sum(r["input_tokens"] for r in requests)
    total_output = sum(r["output_tokens"] for r in requests)
    total_cached = sum(r["cached_tokens"] for r in requests)
    total_nano_aiu = None
    nano_values = [r["nano_aiu"] for r in requests if r["nano_aiu"] is not None]
    if nano_values:
        total_nano_aiu = sum(nano_values)

    # Fall back to the file mtime when no event carried a timestamp, so the
    # session still buckets to roughly the right day (not the rollup day).
    if earliest_ts is None:
        try:
            earliest_ts = main_jsonl.stat().st_mtime
        except OSError:
            earliest_ts = None

    return {
        "session_id": session_id,
        "workspace_hash": workspace_hash,
        "title": title,
        "first_ts_epoch": earliest_ts,
        "requests": requests,
        "totals": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cached_tokens": total_cached,
            "nano_aiu": total_nano_aiu,
            "requests": len(requests),
        },
        "context_breakdown": context_breakdown,
        "attachments": attachments,
        "tool_calls": tool_calls,
        "user_messages": user_messages_count,
        "data_source": _DATA_SOURCE_DEBUGLOGS,
    }


def _empty_session(session_id: str, workspace_hash: str) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "workspace_hash": workspace_hash,
        "title": None,
        "requests": [],
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "nano_aiu": None,
            "requests": 0,
        },
        "context_breakdown": {},
        "attachments": [],
        "tool_calls": [],
        "user_messages": 0,
        "data_source": _DATA_SOURCE_DEBUGLOGS,
    }


def _read_title_from_sidecars(session_dir: Path) -> Optional[str]:
    """Scan ``title-*.jsonl`` sidecars for an agent_response title."""
    try:
        sidecars = sorted(session_dir.glob("title-*.jsonl"))
    except OSError:
        return None
    for sidecar in sidecars:
        try:
            with open(sidecar, encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        evt = json.loads(raw_line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(evt, dict):
                        continue
                    if evt.get("name") == "agent_response":
                        attrs = evt.get("attrs") or {}
                        response = attrs.get("response") if isinstance(attrs, dict) else None
                        if isinstance(response, str) and response.strip():
                            return response.strip()[:200]
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Public API: read_sessions
# ---------------------------------------------------------------------------

def read_sessions(
    extra_bases: Optional[List[Path]] = None,
) -> List[Dict[str, Any]]:
    """Parse all VS Code Copilot debug-log sessions into dicts.

    Discovers workspace-storage roots, walks all workspace hashes, and
    parses every ``<session_uuid>/main.jsonl`` found.

    Deduplicates sessions by ``(workspace_hash, session_id)`` so re-scans
    never produce duplicates.

    Returns an empty list when no debug-logs directories exist.
    """
    log_dirs = discover_log_dirs(extra_bases=extra_bases)

    sessions: List[Dict[str, Any]] = []
    seen: set = set()

    for log_dir in log_dirs:
        # The workspace hash is the parent of GitHub.copilot-chat/debug-logs.
        # log_dir = <ws-hash>/GitHub.copilot-chat/debug-logs
        workspace_hash = log_dir.parent.parent.name

        try:
            session_dirs = [d for d in log_dir.iterdir() if d.is_dir()]
        except OSError:
            continue

        for session_dir in session_dirs:
            dedup_key = (workspace_hash, session_dir.name)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            main_jsonl = session_dir / _MAIN_JSONL
            if not main_jsonl.is_file():
                continue

            # One unparseable session must never abort the whole scan.
            try:
                sess = _parse_session(session_dir, workspace_hash)
            except Exception as exc:
                logger.debug("[copilot_vscode] failed to parse %s: %s", session_dir, exc)
                continue
            sessions.append(sess)

    return sessions


# ---------------------------------------------------------------------------
# OTel reader: read_otel_sessions
# ---------------------------------------------------------------------------

@contextmanager
def _ro_connect_otel(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an OTel db read-only; always close even on error."""
    conn = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1",
        uri=True,
        timeout=0.25,
    )
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _otel_default_db_path() -> Optional[Path]:
    """Return the default OTel DB path (may not exist)."""
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        base = home / "Library" / "Application Support" / "Code"
    elif plat == "win32":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        base = Path(appdata) / "Code"
    else:
        base = home / ".config" / "Code"
    return base / "User" / "globalStorage" / "github.copilot-chat" / "otel" / "agent-traces.db"


def read_otel_sessions(
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Parse OTel ``agent-traces.db`` into session dicts.

    Falls back to ``[]`` on missing, locked, or corrupt DB.  Callers
    pick ONE source (debug-logs OR OTel) — this module never merges both.

    If a ``sessions`` VIEW exists it is preferred; otherwise spans are
    aggregated from individual rows grouped by ``conversation_id``.
    """
    target = db_path if db_path is not None else _otel_default_db_path()
    if target is None or not Path(target).exists():
        return []

    try:
        with _ro_connect_otel(Path(target)) as conn:
            return _otel_read(conn)
    except sqlite3.Error as exc:
        logger.debug("[copilot_vscode] OTel DB read error (%s): %s", target, exc)
        return []
    except OSError as exc:
        logger.debug("[copilot_vscode] OTel DB open error (%s): %s", target, exc)
        return []


def _otel_read(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Read sessions from an open OTel connection."""
    # Prefer a `sessions` VIEW if it exists.
    try:
        view_rows = conn.execute(
            "SELECT conversation_id, turn_index, input_tokens, "
            "output_tokens, cached_tokens, request_model "
            "FROM sessions"
        ).fetchall()
        return _otel_aggregate(view_rows)
    except sqlite3.Error:
        pass

    # Fallback: aggregate spans table.
    try:
        span_rows = conn.execute(
            "SELECT conversation_id, turn_index, input_tokens, "
            "output_tokens, cached_tokens, request_model "
            "FROM spans"
        ).fetchall()
        return _otel_aggregate(span_rows)
    except sqlite3.Error as exc:
        # A genuine schema/corruption failure (vs the expected "no such view"
        # for the sessions probe above) — surface it so the doctor can explain
        # "DB present but unreadable" rather than silently reporting zero.
        logger.debug("[copilot_vscode] OTel spans query failed: %s", exc)

    return []


def _otel_aggregate(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Aggregate per-span OTel rows into per-session dicts."""
    # Group by conversation_id.
    by_conv: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        conv_id = str(row[0]) if row[0] is not None else "unknown"
        if conv_id not in by_conv:
            by_conv[conv_id] = []
        by_conv[conv_id].append(row)

    sessions: List[Dict[str, Any]] = []
    for conv_id, conv_rows in by_conv.items():
        total_input = sum(_safe_int(r[2]) for r in conv_rows)
        total_output = sum(_safe_int(r[3]) for r in conv_rows)
        total_cached = sum(_safe_int(r[4]) for r in conv_rows)
        requests_count = len(conv_rows)

        sessions.append(
            {
                "session_id": conv_id,
                "workspace_hash": "",
                "title": None,
                "requests": [
                    {
                        "request_id": f"{conv_id}:{_safe_int(r[1])}:{i}",
                        "turn": _safe_int(r[1]),
                        "model": _safe_str(r[5]),
                        "input_tokens": _safe_int(r[2]),
                        "output_tokens": _safe_int(r[3]),
                        "cached_tokens": _safe_int(r[4]),
                        "nano_aiu": None,
                        "ttft_ms": None,
                        "system_prompt_chars": None,
                        "tools_chars": None,
                    }
                    for i, r in enumerate(conv_rows)
                ],
                "totals": {
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "cached_tokens": total_cached,
                    "nano_aiu": None,
                    "requests": requests_count,
                },
                "context_breakdown": {},
                "attachments": [],
                "tool_calls": [],
                "user_messages": 0,
                "data_source": _DATA_SOURCE_OTEL,
            }
        )

    return sessions


# ---------------------------------------------------------------------------
# Enablement status
# ---------------------------------------------------------------------------

def enablement_status(
    extra_bases: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """Report which VS Code Copilot data sources are plausibly enabled.

    Checks for the presence of existing debug-logs directories and the
    default OTel DB path without reading any VS Code settings.json.
    Returns a dict with:

    * ``debug_logs_found`` (bool): at least one debug-logs dir exists.
    * ``otel_db_found`` (bool): default OTel DB path exists.
    * ``bases_scanned`` (list[str]): workspace-storage roots checked.
    """
    log_dirs = discover_log_dirs(extra_bases=extra_bases)
    debug_logs_found = len(log_dirs) > 0

    otel_path = _otel_default_db_path()
    otel_db_found = otel_path is not None and otel_path.exists()

    # Display value only — discover_log_dirs owns the canonical dedup logic;
    # repeating it here just to prettify this list invited silent divergence.
    bases_scanned = [str(b) for b in list(extra_bases or []) + _default_bases()]

    return {
        "debug_logs_found": debug_logs_found,
        "otel_db_found": otel_db_found,
        "bases_scanned": bases_scanned,
    }


# ---------------------------------------------------------------------------
# CLI entry point (for manual inspection)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import pprint

    parser = argparse.ArgumentParser(description="Read VS Code Copilot debug-logs.")
    parser.add_argument("--status", action="store_true", help="Show enablement status.")
    parser.add_argument("--otel", metavar="DB", help="Read OTel DB at path.")
    args = parser.parse_args()

    if args.status:
        pprint.pprint(enablement_status())
    elif args.otel:
        pprint.pprint(read_otel_sessions(db_path=Path(args.otel)))
    else:
        sessions = read_sessions()
        print(f"Found {len(sessions)} session(s).")
        for s in sessions:
            print(
                f"  {s['workspace_hash']}/{s['session_id']}: "
                f"requests={s['totals']['requests']} "
                f"input={s['totals']['input_tokens']} "
                f"nano_aiu={s['totals']['nano_aiu']}"
            )
