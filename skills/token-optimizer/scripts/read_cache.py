#!/usr/bin/env python3
"""Token Optimizer - PreToolUse Read Cache (standalone entry point).

Conservatively intercepts Read tool calls to detect redundant file reads.
Default behavior is production-safe structure substitution for a narrow slice:
unchanged whole-file supported code rereads that can be replaced with a bounded
code map.

Default ON. Opt out via TOKEN_OPTIMIZER_READ_CACHE=0 or config.json
{"read_cache_enabled": false}.

Modes:
  soft_block (default) - deny eligible redundant rereads and inject structure map
  warn                - log redundant rereads but allow the read
  shadow              - measure-only, allow the read without warning noise
  block               - deny all redundant rereads; only inject structure map for
                        eligible supported code files, reason-only otherwise
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Optional

from plugin_env import is_v5_flag_enabled, resolve_snapshot_dir
from session_store import SessionStore, cleanup_old_stores
from structure_map import (
    StructureMapResult,
    detect_structure_language,
    is_structure_supported_file,
    summarize_code_source,
)


def _is_v5_delta_enabled():
    """Check if delta mode is enabled. Default ON in v5.1."""
    return is_v5_flag_enabled("v5_delta_mode", "TOKEN_OPTIMIZER_READ_CACHE_DELTA", default=True)


def _is_v5_structure_map_beta():
    """Check if structure map beta telemetry is enabled."""
    return is_v5_flag_enabled(
        "v5_structure_map_beta",
        "TOKEN_OPTIMIZER_STRUCTURE_MAP",
        default=False,
        env_truthy_value="beta",
    )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SNAPSHOT_DIR = resolve_snapshot_dir()
CACHE_DIR = SNAPSHOT_DIR / "read-cache"
TRENDS_DB = SNAPSHOT_DIR / "trends.db"
MAX_CONTEXTIGNORE_PATTERNS = 200
READ_CACHE_MODES = frozenset({"shadow", "warn", "soft_block", "block"})
DEFAULT_MODE = "soft_block"

MIN_STRUCTURE_CONFIDENCE = 0.84
REASON_ONLY_TOKENS_EST = 10
STRICT_CONTEXT_CAPS = {
    "signatures": 350,
    "top_level": 500,
    "skeleton": 850,
    "digest": 500,
}
MAX_ADDITIONAL_CONTEXT_CHARS = 1500
STRICT_ADDITIONAL_CONTEXT_CHARS = 1000

# All binary extensions (early-return path). Token-cost warnings use the
# narrower EXPENSIVE_BINARY subset in detectors/pdf_ingestion.py.
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".wasm", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".jar",
    ".sqlite", ".db", ".sqlite3",
})


# ---------------------------------------------------------------------------
# .contextignore
# ---------------------------------------------------------------------------

_contextignore_cache: dict[str, list[str]] = {}


def _load_contextignore_patterns() -> list[str]:
    """Load .contextignore patterns from project root and global config."""

    cache_key = "patterns"
    if cache_key in _contextignore_cache:
        return _contextignore_cache[cache_key]

    patterns: list[str] = []

    project_ignore = Path(".contextignore")
    if project_ignore.exists():
        try:
            for line in project_ignore.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass

    global_ignore = Path.home() / ".claude" / ".contextignore"
    if global_ignore.exists():
        try:
            for line in global_ignore.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass

    patterns = patterns[:MAX_CONTEXTIGNORE_PATTERNS]
    _contextignore_cache[cache_key] = patterns
    return patterns


def _is_contextignored(file_path: str) -> bool:
    """Check if file matches any .contextignore pattern."""

    patterns = _load_contextignore_patterns()
    if not patterns:
        return False
    for pattern in patterns:
        if fnmatch(file_path, pattern) or fnmatch(Path(file_path).name, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Cache operations
# ---------------------------------------------------------------------------

_store_cache: dict[str, SessionStore] = {}


def _get_store(session_id: str) -> SessionStore:
    if session_id not in _store_cache:
        _store_cache[session_id] = SessionStore(session_id)
    return _store_cache[session_id]


def _cache_path(session_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id) or "unknown"
    return CACHE_DIR / f"{safe_id}.json"


def _decisions_log_path(session_id: str = "unknown") -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id) or "unknown"
    decisions_dir = CACHE_DIR / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return decisions_dir / f"{safe_id}.jsonl"



def _reset_replacement_state(entry: dict[str, Any]) -> None:
    entry["last_replacement_fingerprint"] = ""
    entry["last_replacement_type"] = ""
    entry["repeat_replacement_count"] = 0
    entry["last_structure_reason"] = ""
    entry["last_structure_confidence"] = 0.0


def _ensure_entry_defaults(entry: dict[str, Any]) -> None:
    entry.setdefault("mtime_ns", 0)
    entry.setdefault("size_bytes", 0)
    entry.setdefault("tokens_est", 0)
    entry.setdefault("read_count", 0)
    entry.setdefault("last_access", 0.0)
    entry.setdefault("last_replacement_fingerprint", "")
    entry.setdefault("last_replacement_type", "")
    entry["repeat_replacement_count"] = int(entry.get("repeat_replacement_count", 0) or 0)
    entry["last_structure_reason"] = entry.get("last_structure_reason", "")
    entry["last_structure_confidence"] = float(entry.get("last_structure_confidence", 0.0) or 0.0)
    if "ranges_seen" not in entry:
        old_off = int(entry.get("offset", 0) or 0)
        old_lim = int(entry.get("limit", 0) or 0)
        entry["ranges_seen"] = [[old_off, old_lim]]


def _log_decision(
    decision: str,
    file_path: str,
    reason: str,
    session_id: str,
    **extra: Any,
) -> None:
    entry = {
        "ts": time.time(),
        "decision": decision,
        "file": file_path,
        "reason": reason,
        "session": session_id,
    }
    entry.update(extra)
    log_path = _decisions_log_path(session_id)
    try:
        if not log_path.exists():
            fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hook response helpers
# ---------------------------------------------------------------------------

def _emit_pretool_response(
    permission_decision: Optional[str],
    reason: Optional[str],
    additional_context: Optional[str] = None,
) -> None:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
        }
    }
    hook_output = payload["hookSpecificOutput"]
    if permission_decision:
        hook_output["permissionDecision"] = permission_decision
    if reason:
        hook_output["permissionDecisionReason"] = reason
    if additional_context:
        hook_output["additionalContext"] = additional_context
    print(json.dumps(payload))


def _build_structure_message(
    file_path: str,
    summary: StructureMapResult,
    net_saved_tokens_est: int,
) -> str:
    return "\n".join(
        [
            f"[Token Optimizer] {Path(file_path).name} is unchanged (already read this session).",
            f"Using {summary.replacement_type} view. Edit the file or request a specific range for full content.",
            "",
            summary.replacement_text,
        ]
    )


def _build_reason_only_message(file_path: str) -> str:
    return (
        f"[Token Optimizer] {Path(file_path).name} is unchanged, already in context, and was already "
        "summarized in this session."
    )


def _hook_additional_context_saved() -> bool:
    value = os.environ.get("CLAUDE_CODE_SAVE_HOOK_ADDITIONAL_CONTEXT", "").strip().lower()
    return value not in ("", "0", "false", "no")


def _additional_context_within_cap(text: str, strict: bool) -> bool:
    cap = STRICT_ADDITIONAL_CONTEXT_CHARS if strict else MAX_ADDITIONAL_CONTEXT_CHARS
    return len(text) <= cap


# ---------------------------------------------------------------------------
# Savings tracking
# ---------------------------------------------------------------------------

_SAVINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS savings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tokens_saved INTEGER DEFAULT 0,
    cost_saved_usd REAL DEFAULT 0.0,
    session_id TEXT,
    detail TEXT
);
"""


def _log_savings_event(event_type: str, tokens_saved: int, session_id: str, detail: str) -> None:
    if tokens_saved <= 0:
        return
    conn: Optional[sqlite3.Connection] = None
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(TRENDS_DB))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SAVINGS_SCHEMA)
        # Price at active session model. Import lazily to avoid circular imports
        # and keep this hook fast when measure.py isn't already loaded.
        cost_per_mtok = 3.0
        try:
            from measure import _estimate_compression_cost_per_mtok  # type: ignore
            cost_per_mtok = _estimate_compression_cost_per_mtok()
        except Exception:
            pass
        cost_saved = tokens_saved * cost_per_mtok / 1_000_000
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), event_type, tokens_saved, cost_saved, session_id, detail),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Structure summarization
# ---------------------------------------------------------------------------

def _summarize_redundant_read(
    file_path: str,
    *,
    offset: int,
    limit: int,
    file_tokens_est: int,
) -> tuple[Optional[StructureMapResult], str]:
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "unreadable"

    summary = summarize_code_source(
        content,
        file_path=file_path,
        offset=offset,
        limit=limit,
        file_tokens_est=file_tokens_est,
        file_size_bytes=len(content.encode("utf-8", errors="ignore")),
    )
    reason_code = summary.reason

    if not summary.eligible:
        return summary, reason_code

    if summary.confidence < MIN_STRUCTURE_CONFIDENCE:
        return summary, "low_confidence"

    if _hook_additional_context_saved():
        strict_cap = STRICT_CONTEXT_CAPS.get(summary.replacement_type, len(summary.replacement_text))
        if len(summary.replacement_text) > strict_cap:
            return summary, "hook_context_cap"

    return summary, "ok"
# ---------------------------------------------------------------------------
# Main hook logic
# ---------------------------------------------------------------------------

def handle_read(hook_input: dict[str, Any], mode: str, quiet: bool) -> None:
    """Handle a PreToolUse Read event."""

    if hook_input.get("tool_name") not in ("", "Read"):
        return

    tool_input = hook_input.get("tool_input", {})
    raw_path = tool_input.get("file_path", "")
    if not raw_path:
        return

    file_path = str(Path(raw_path).resolve())
    session_id = str(hook_input.get("agent_id") or hook_input.get("session_id") or "unknown")
    try:
        offset = int(tool_input.get("offset", 0) or 0)
    except (ValueError, TypeError):
        offset = 0
    try:
        limit = int(tool_input.get("limit", 0) or 0)
    except (ValueError, TypeError):
        limit = 0
    ext = Path(file_path).suffix.lower()
    language = detect_structure_language(file_path)
    save_hook_context_enabled = _hook_additional_context_saved()

    if _is_contextignored(file_path):
        reason = f"Blocked by .contextignore: {Path(file_path).name}"
        _log_decision(
            "block",
            file_path,
            "contextignore",
            session_id,
            mode=mode,
            actual_substitution=False,
            eligible=False,
            language=language,
            reason_code="contextignore",
            offset=offset,
            limit=limit,
            replacement_type=None,
            file_tokens_est=0,
            replacement_tokens_est=0,
            net_saved_tokens_est=0,
            replacement_fingerprint=None,
            repeat_replacement_count=0,
            save_hook_additional_context_enabled=save_hook_context_enabled,
        )
        if not quiet:
            print(f"[Read Cache] Blocked by .contextignore: {file_path}", file=sys.stderr)
        _emit_pretool_response("deny", reason)
        return

    if ext in BINARY_EXTENSIONS:
        # Warn about expensive binary ingestion (PDF, images, Office docs)
        try:
            binary_stat = os.stat(file_path)
            binary_size = binary_stat.st_size
        except OSError:
            return

        finding = None
        try:
            from detectors.pdf_ingestion import detect_pdf_ingestion_inline
            finding = detect_pdf_ingestion_inline(file_path, binary_size, ext)
        except ImportError:
            pass

        if finding:
            size_mb = binary_size / (1024 * 1024)
            context_msg = (
                f"[Token Optimizer] {Path(file_path).name} is a {ext} file "
                f"({size_mb:.1f}MB, ~{finding['savings_tokens']:,} estimated tokens). "
                f"{finding['suggestion']}"
            )
            _log_decision(
                "allow",
                file_path,
                "binary_ingestion_warning",
                session_id,
                mode=mode,
                actual_substitution=False,
                eligible=False,
                language=language,
                reason_code="binary_ingestion",
                offset=offset,
                limit=limit,
                replacement_type=None,
                file_tokens_est=finding["savings_tokens"],
                replacement_tokens_est=0,
                net_saved_tokens_est=0,
                replacement_fingerprint=None,
                repeat_replacement_count=0,
                save_hook_additional_context_enabled=save_hook_context_enabled,
            )
            if not quiet:
                print(
                    f"[Read Cache] Binary ingestion warning: {file_path} "
                    f"(~{finding['savings_tokens']:,} tokens)",
                    file=sys.stderr,
                )
            _emit_pretool_response(None, None, context_msg)
        return

    store = _get_store(session_id)
    entry = store.get_file_entry(file_path)

    if entry is None:
        try:
            stat = os.stat(file_path)
        except OSError:
            return

        tokens_est = max(1, stat.st_size // 4) if stat.st_size else 0
        entry = {
            "mtime_ns": stat.st_mtime_ns,
            "size_bytes": stat.st_size,
            "ranges_seen": [[offset, limit]],
            "tokens_est": tokens_est,
            "read_count": 1,
            "last_access": time.time(),
        }
        if _is_v5_delta_enabled() and offset == 0 and limit == 0:
            try:
                from delta_diff import is_delta_eligible, content_hash, MAX_CONTENT_CACHE_BYTES
                if is_delta_eligible(file_path):
                    fc = Path(file_path).read_text(encoding="utf-8", errors="replace")
                    if len(fc.encode("utf-8", errors="replace")) <= MAX_CONTENT_CACHE_BYTES:
                        entry["cached_content"] = fc
                        entry["content_hash"] = content_hash(fc)
                        store.upsert_cached_content(file_path, fc, content_hash(fc))
            except Exception:
                pass
        _reset_replacement_state(entry)
        store.upsert_file_entry(file_path, entry)
        _log_decision(
            "allow",
            file_path,
            "first_read",
            session_id,
            mode=mode,
            actual_substitution=False,
            eligible=False,
            language=language,
            reason_code="first_read",
            offset=offset,
            limit=limit,
            replacement_type=None,
            file_tokens_est=tokens_est,
            replacement_tokens_est=0,
            net_saved_tokens_est=0,
            replacement_fingerprint=None,
            repeat_replacement_count=0,
            save_hook_additional_context_enabled=save_hook_context_enabled,
        )
        return

    _ensure_entry_defaults(entry)

    try:
        current_stat = os.stat(file_path)
    except OSError:
        store.delete_file_entry(file_path)
        _log_decision(
            "allow",
            file_path,
            "file_deleted",
            session_id,
            mode=mode,
            actual_substitution=False,
            eligible=False,
            language=language,
            reason_code="file_deleted",
            offset=offset,
            limit=limit,
            replacement_type=None,
            file_tokens_est=entry.get("tokens_est", 0),
            replacement_tokens_est=0,
            net_saved_tokens_est=0,
            replacement_fingerprint=None,
            repeat_replacement_count=0,
            save_hook_additional_context_enabled=save_hook_context_enabled,
        )
        return

    mtime_match = int(entry.get("mtime_ns", 0) or 0) == current_stat.st_mtime_ns
    size_match = int(entry.get("size_bytes", 0) or 0) == current_stat.st_size

    ranges_seen = entry.get("ranges_seen", [])
    if not ranges_seen:
        old_off = int(entry.get("offset", 0) or 0)
        old_lim = int(entry.get("limit", 0) or 0)
        ranges_seen = [[old_off, old_lim]]

    range_covered = False
    for cached_off, cached_lim in ranges_seen:
        if cached_off == 0 and cached_lim == 0:
            range_covered = True
            break
        if offset >= cached_off:
            if cached_lim == 0:
                range_covered = True
                break
            if limit > 0 and (offset + limit) <= (cached_off + cached_lim):
                range_covered = True
                break

    if not (mtime_match and size_match and range_covered):
        # v5.0: Delta mode -- return diff instead of allowing full re-read
        delta_enabled = _is_v5_delta_enabled()
        cached = store.get_cached_content(file_path) if delta_enabled else None
        old_content = cached.get("content") if cached else entry.get("cached_content")
        old_hash = cached.get("content_hash") if cached else entry.get("content_hash")
        if (
            delta_enabled
            and offset == 0
            and limit == 0
            and not mtime_match
            and old_hash
            and old_content
        ):
            try:
                from delta_diff import compute_delta, content_hash, is_delta_eligible, MAX_CONTENT_CACHE_BYTES
                if is_delta_eligible(file_path):
                    new_content = Path(file_path).read_text(encoding="utf-8", errors="replace")
                    new_hash = content_hash(new_content)
                    if new_hash != old_hash:
                        delta_text, delta_stats = compute_delta(old_content, new_content, Path(file_path).name)
                        if delta_text is not None:
                            entry["mtime_ns"] = current_stat.st_mtime_ns
                            entry["size_bytes"] = current_stat.st_size
                            entry["content_hash"] = new_hash
                            entry["read_count"] = int(entry.get("read_count", 0) or 0) + 1
                            entry["last_access"] = time.time()
                            _reset_replacement_state(entry)
                            store.upsert_file_entry(file_path, entry)
                            if len(new_content.encode("utf-8", errors="replace")) <= MAX_CONTENT_CACHE_BYTES:
                                store.upsert_cached_content(file_path, new_content, new_hash)

                            old_tokens = max(1, current_stat.st_size // 4)
                            delta_tokens = len(delta_text.encode("utf-8", errors="replace")) // 4
                            net_saved = max(0, old_tokens - delta_tokens)

                            _log_decision(
                                "block",
                                file_path,
                                f"delta_read_{entry['read_count']}",
                                session_id,
                                mode=mode,
                                actual_substitution=True,
                                eligible=True,
                                language=language,
                                reason_code="delta_diff",
                                offset=offset,
                                limit=limit,
                                replacement_type="delta",
                                file_tokens_est=old_tokens,
                                replacement_tokens_est=delta_tokens,
                                net_saved_tokens_est=net_saved,
                                replacement_fingerprint=new_hash[:16],
                                repeat_replacement_count=0,
                                save_hook_additional_context_enabled=save_hook_context_enabled,
                            )
                            _log_savings_event("delta_read", net_saved, session_id,
                                               f"{Path(file_path).name}:+{delta_stats['added']}/-{delta_stats['removed']}")
                            # Log to compression_events for v5 telemetry
                            try:
                                sys.path.insert(0, str(Path(__file__).resolve().parent))
                                from measure import _log_compression_event
                                _log_compression_event(
                                    feature="delta_read",
                                    original_text=" " * (old_tokens * 4),  # proxy
                                    compressed_text=delta_text,
                                    session_id=session_id,
                                    command_pattern=f"Read:{Path(file_path).name}",
                                    quality_preserved=True,
                                    verified=True,
                                    detail=f"+{delta_stats['added']}/-{delta_stats['removed']}",
                                )
                            except Exception:
                                pass
                            if not quiet:
                                print(
                                    f"[Read Cache] Delta mode: {file_path} "
                                    f"(+{delta_stats['added']}/-{delta_stats['removed']}, saved~{net_saved:,})",
                                    file=sys.stderr,
                                )
                            reason = (
                                f"{Path(file_path).name} was edited; showing diff "
                                f"(+{delta_stats['added']}/-{delta_stats['removed']} lines) "
                                f"instead of full re-read."
                            )
                            _emit_pretool_response("deny", reason, delta_text)
                            return  # EXIT: delta handled, do NOT evaluate structure map
                    else:
                        # Hash unchanged despite mtime change (e.g., touch). Allow normal read.
                        pass
            except Exception:
                pass  # Fail open: fall through to normal allow

        file_changed = not (mtime_match and size_match)
        reason_code_allow = "file_modified" if file_changed else "new_range"
        entry["mtime_ns"] = current_stat.st_mtime_ns
        entry["size_bytes"] = current_stat.st_size
        if file_changed:
            entry["ranges_seen"] = [[offset, limit]]
        else:
            ranges_seen.append([offset, limit])
            entry["ranges_seen"] = ranges_seen
        entry["tokens_est"] = max(1, current_stat.st_size // 4) if current_stat.st_size else 0
        entry["read_count"] = int(entry.get("read_count", 0) or 0) + 1
        entry["last_access"] = time.time()
        # Cache content for future delta diffs (v5.0) - only whole-file reads
        if delta_enabled and offset == 0 and limit == 0 and not entry.get("cached_content"):
            try:
                from delta_diff import is_delta_eligible, content_hash, MAX_CONTENT_CACHE_BYTES
                if is_delta_eligible(file_path):
                    fc = Path(file_path).read_text(encoding="utf-8", errors="replace")
                    if len(fc.encode("utf-8", errors="replace")) <= MAX_CONTENT_CACHE_BYTES:
                        entry["cached_content"] = fc
                        entry["content_hash"] = content_hash(fc)
                        store.upsert_cached_content(file_path, fc, content_hash(fc))
            except Exception:
                pass
        _reset_replacement_state(entry)
        store.upsert_file_entry(file_path, entry)
        _log_decision(
            "allow",
            file_path,
            reason_code_allow,
            session_id,
            mode=mode,
            actual_substitution=False,
            eligible=False,
            language=language,
            reason_code=reason_code_allow,
            offset=offset,
            limit=limit,
            replacement_type=None,
            file_tokens_est=entry.get("tokens_est", 0),
            replacement_tokens_est=0,
            net_saved_tokens_est=0,
            replacement_fingerprint=None,
            repeat_replacement_count=0,
            save_hook_additional_context_enabled=save_hook_context_enabled,
        )
        return

    entry["read_count"] = int(entry.get("read_count", 0) or 0) + 1
    entry["last_access"] = time.time()
    tokens_est = int(entry.get("tokens_est", 0) or 0)

    summary: Optional[StructureMapResult] = None
    reason_code = "unsupported_language"
    if is_structure_supported_file(file_path):
        summary, reason_code = _summarize_redundant_read(
            file_path,
            offset=offset,
            limit=limit,
            file_tokens_est=tokens_est,
        )
    eligible_structure = bool(summary and summary.eligible and reason_code == "ok")

    if mode in {"shadow", "warn"} or (mode == "soft_block" and not eligible_structure):
        decision = "warn" if mode == "warn" else "allow"
        entry["last_structure_reason"] = reason_code
        entry["last_structure_confidence"] = summary.confidence if summary else 0.0
        store.upsert_file_entry(file_path, entry)
        _log_decision(
            decision,
            file_path,
            f"redundant_read_{entry['read_count']}",
            session_id,
            mode=mode,
            actual_substitution=False,
            eligible=eligible_structure,
            language=language,
            reason_code=reason_code,
            offset=offset,
            limit=limit,
            replacement_type=summary.replacement_type if summary else None,
            file_tokens_est=tokens_est,
            replacement_tokens_est=summary.replacement_tokens_est if summary else 0,
            net_saved_tokens_est=max(0, tokens_est - (summary.replacement_tokens_est if summary else 0)),
            replacement_fingerprint=summary.fingerprint if summary else None,
            repeat_replacement_count=0,
            save_hook_additional_context_enabled=save_hook_context_enabled,
            confidence=summary.confidence if summary else 0.0,
        )
        if not quiet and mode == "warn":
            print(f"[Read Cache] Redundant read allowed in warn mode: {file_path}", file=sys.stderr)
        return

    if eligible_structure and summary is not None:
        same_fingerprint = summary.fingerprint == entry.get("last_replacement_fingerprint", "")
        repeat_count = int(entry.get("repeat_replacement_count", 0) or 0) + 1 if same_fingerprint else 1
        entry["last_replacement_fingerprint"] = summary.fingerprint
        entry["last_replacement_type"] = summary.replacement_type
        entry["repeat_replacement_count"] = repeat_count
        entry["last_structure_reason"] = reason_code
        entry["last_structure_confidence"] = summary.confidence
        store.upsert_file_entry(file_path, entry)

        if repeat_count == 1:
            replacement_tokens_est = summary.replacement_tokens_est
            additional_context = _build_structure_message(
                file_path,
                summary,
                max(0, tokens_est - summary.replacement_tokens_est),
            )
            if not _additional_context_within_cap(additional_context, save_hook_context_enabled):
                additional_context = None
                replacement_tokens_est = REASON_ONLY_TOKENS_EST
        else:
            additional_context = None
            replacement_tokens_est = REASON_ONLY_TOKENS_EST

        net_saved_tokens_est = max(0, tokens_est - replacement_tokens_est)
        if repeat_count == 1:
            reason = (
                f"{Path(file_path).name} is unchanged and already in context; "
                f"using {summary.replacement_type} view."
            )
        else:
            reason = _build_reason_only_message(file_path)

        _log_decision(
            "block",
            file_path,
            f"redundant_read_{entry['read_count']}",
            session_id,
            mode=mode,
            actual_substitution=True,
            eligible=True,
            language=language,
            reason_code=f"structure_map_repeat_{repeat_count}",
            offset=offset,
            limit=limit,
            replacement_type=summary.replacement_type,
            file_tokens_est=tokens_est,
            replacement_tokens_est=replacement_tokens_est,
            net_saved_tokens_est=net_saved_tokens_est,
            replacement_fingerprint=summary.fingerprint,
            repeat_replacement_count=repeat_count,
            save_hook_additional_context_enabled=save_hook_context_enabled,
            confidence=summary.confidence,
        )
        _log_savings_event(
            "structure_map",
            net_saved_tokens_est,
            session_id,
            f"{Path(file_path).name}:{summary.replacement_type}:repeat={repeat_count}",
        )
        # v5.0: Structure map beta telemetry
        if _is_v5_structure_map_beta():
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from measure import _log_compression_event
                _log_compression_event(
                    feature="structure_map",
                    original_text=" " * (tokens_est * 4),  # proxy
                    compressed_text=summary.replacement_text if summary.replacement_text else "",
                    session_id=session_id,
                    command_pattern=f"Read:{Path(file_path).name}",
                    quality_preserved=True,
                    verified=False,  # we don't know if AI would have used full file
                    detail=f"type={summary.replacement_type} confidence={summary.confidence:.2f} repeat={repeat_count} lang={language}",
                )
            except Exception:
                pass
        if not quiet:
            print(
                f"[Read Cache] Blocked redundant read #{entry['read_count']}: {file_path} "
                f"(mode={mode}, replacement={summary.replacement_type}, repeat={repeat_count}, "
                f"saved~{net_saved_tokens_est:,})",
                file=sys.stderr,
            )
        _emit_pretool_response("deny", reason, additional_context)
        return

    if mode == "block":
        store.upsert_file_entry(file_path, entry)
        _log_decision(
            "block",
            file_path,
            f"redundant_read_{entry['read_count']}",
            session_id,
            mode=mode,
            actual_substitution=False,
            eligible=False,
            language=language,
            reason_code=reason_code,
            offset=offset,
            limit=limit,
            replacement_type=None,
            file_tokens_est=tokens_est,
            replacement_tokens_est=REASON_ONLY_TOKENS_EST,
            net_saved_tokens_est=max(0, tokens_est - REASON_ONLY_TOKENS_EST),
            replacement_fingerprint=None,
            repeat_replacement_count=0,
            save_hook_additional_context_enabled=save_hook_context_enabled,
            confidence=summary.confidence if summary else 0.0,
        )
        reason = (
            f"{Path(file_path).name} is unchanged and already in context; "
            "redundant reread blocked."
        )
        _emit_pretool_response("deny", reason)
        return


def handle_clear(session_id: str, quiet: bool) -> None:
    """Clear read cache for a session."""

    if session_id and session_id != "all":
        store = _get_store(session_id)
        store.clear_file_entries()
        cp = _cache_path(session_id)
        if cp.exists():
            cp.unlink()
        dp = _decisions_log_path(session_id)
        if dp.exists():
            try:
                dp.unlink()
            except OSError:
                pass
        if not quiet:
            print(f"[Read Cache] Cleared cache for session {session_id}", file=sys.stderr)
    elif session_id == "all" and CACHE_DIR.exists():
        for candidate in CACHE_DIR.glob("*.json"):
            candidate.unlink()
        for candidate in CACHE_DIR.glob("*.tmp"):
            try:
                candidate.unlink()
            except OSError:
                pass
        decisions_dir = CACHE_DIR / "decisions"
        if decisions_dir.exists():
            for candidate in decisions_dir.glob("*.jsonl"):
                try:
                    candidate.unlink()
                except OSError:
                    pass
        deleted = cleanup_old_stores()
        if not quiet:
            extra = f", pruned {deleted} old session stores" if deleted else ""
            print(f"[Read Cache] Cleared all caches{extra}", file=sys.stderr)


def handle_invalidate(hook_input: dict[str, Any], quiet: bool) -> None:
    """Invalidate cache entry when a file is edited/written."""

    tool_name = hook_input.get("tool_name", "")
    if tool_name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return

    tool_input = hook_input.get("tool_input", {})
    raw_path = tool_input.get("file_path", "")
    if not raw_path:
        return

    file_path = str(Path(raw_path).resolve())
    session_id = str(hook_input.get("agent_id") or hook_input.get("session_id") or "unknown")
    store = _get_store(session_id)
    existing = store.get_file_entry(file_path)

    if existing is not None:
        store.delete_file_entry(file_path)
        store.delete_cached_content(file_path)
        if not quiet:
            print(f"[Read Cache] Invalidated: {file_path}", file=sys.stderr)


def handle_stats(session_id: str) -> None:
    """Print cache stats for a session."""

    store = _get_store(session_id)
    files = store.get_all_file_entries()
    total_reads = sum(int(entry.get("read_count", 0) or 0) for entry in files.values())
    total_tokens = sum(int(entry.get("tokens_est", 0) or 0) for entry in files.values())

    decisions: dict[str, int] = {}
    reason_codes: dict[str, int] = {}
    replacement_types: dict[str, int] = {}
    eligible_events = 0
    repeat_replacement_events = 0
    structure_tokens_avoided = 0

    log_path = _decisions_log_path(session_id)
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                decision = str(event.get("decision", "") or "")
                decisions[decision] = decisions.get(decision, 0) + 1
                reason_code = str(event.get("reason_code", "") or "")
                if reason_code:
                    reason_codes[reason_code] = reason_codes.get(reason_code, 0) + 1
                replacement_type = event.get("replacement_type")
                if replacement_type:
                    replacement_types[str(replacement_type)] = replacement_types.get(str(replacement_type), 0) + 1
                if bool(event.get("eligible")):
                    eligible_events += 1
                if int(event.get("repeat_replacement_count", 0) or 0) > 1:
                    repeat_replacement_events += 1
                if bool(event.get("actual_substitution")):
                    structure_tokens_avoided += int(event.get("net_saved_tokens_est", 0) or 0)
        except OSError:
            pass

    result = {
        "session_id": session_id,
        "cached_files": len(files),
        "total_reads": total_reads,
        "total_tokens_cached": total_tokens,
        "decisions": decisions,
        "structure": {
            "eligible_events": eligible_events,
            "repeat_replacement_events": repeat_replacement_events,
            "actual_tokens_avoided": structure_tokens_avoided,
            "replacement_types": replacement_types,
            "reason_codes": reason_codes,
        },
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Opt-out detection
# ---------------------------------------------------------------------------

def _is_read_cache_disabled() -> bool:
    """Check if user explicitly disabled read-cache via env var or config file."""

    env_val = os.environ.get("TOKEN_OPTIMIZER_READ_CACHE")
    if env_val == "0":
        return True
    if env_val is None:
        for config_dir in [SNAPSHOT_DIR, CACHE_DIR]:
            config_path = config_dir / "config.json"
            if config_path.exists():
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    if config.get("read_cache_enabled") is False:
                        return True
                except (json.JSONDecodeError, OSError, ValueError):
                    pass
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args

    if "--clear" in args:
        session_id = "all"
        for index, arg in enumerate(args):
            if arg == "--session" and index + 1 < len(args):
                session_id = args[index + 1]
        handle_clear(session_id, quiet)
        return

    if "--stats" in args:
        session_id = "unknown"
        for index, arg in enumerate(args):
            if arg == "--session" and index + 1 < len(args):
                session_id = args[index + 1]
        handle_stats(session_id)
        return

    if "--invalidate" in args:
        try:
            hook_input = json.loads(sys.stdin.read(1_000_000))
        except (json.JSONDecodeError, OSError):
            return
        handle_invalidate(hook_input, quiet)
        return

    if _is_read_cache_disabled():
        return

    mode = os.environ.get("TOKEN_OPTIMIZER_READ_CACHE_MODE", DEFAULT_MODE).lower()
    if mode not in READ_CACHE_MODES:
        mode = DEFAULT_MODE

    try:
        hook_input = json.loads(sys.stdin.read(1_000_000))
    except (json.JSONDecodeError, OSError):
        return

    handle_read(hook_input, mode, quiet)


if __name__ == "__main__":
    main()
