#!/usr/bin/env python3
"""Token Optimizer - PreToolUse Read Cache (standalone entry point).

Intercepts Read tool calls to detect redundant file reads.
Opt-in via TOKEN_OPTIMIZER_READ_CACHE=1 env var.

Modes:
  warn  (default) - outputs digest as suggestion, does NOT block
  block           - blocks redundant reads with outputToolResult

Security hardening:
  - Path canonicalization via Path.resolve()
  - 0o600 permissions on cache files
  - mtime re-verification on every cache hit
  - Binary file skip
  - Cache corruption recovery
  - Decision logging to decisions.jsonl
  - .contextignore support (hard block)
  - Cap at 500 entries per session, LRU prune
  - Pattern count cap at 200 for .contextignore
"""

import json
import os
import re
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PLUGIN_DATA = os.environ.get("CLAUDE_PLUGIN_DATA")
SNAPSHOT_DIR = Path(_PLUGIN_DATA) / "data" if _PLUGIN_DATA else Path.home() / ".claude" / "token-optimizer"
CACHE_DIR = SNAPSHOT_DIR / "read-cache"
MAX_CACHE_ENTRIES = 500
MAX_CONTEXTIGNORE_PATTERNS = 200

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

_contextignore_cache: dict = {}


def _load_contextignore_patterns() -> list:
    """Load .contextignore patterns from project root and global config.

    Returns list of patterns. Cached per session.
    """
    cache_key = "patterns"
    if cache_key in _contextignore_cache:
        return _contextignore_cache[cache_key]

    patterns = []

    # Project-level .contextignore
    project_ignore = Path(".contextignore")
    if project_ignore.exists():
        try:
            for line in project_ignore.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass

    # Global .contextignore
    global_ignore = Path.home() / ".claude" / ".contextignore"
    if global_ignore.exists():
        try:
            for line in global_ignore.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass

    # Cap at MAX_CONTEXTIGNORE_PATTERNS to prevent DoS
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
# Structural digests
# ---------------------------------------------------------------------------

def _digest_python(content: str) -> str:
    """Extract Python structure: classes, functions, imports with line numbers."""
    lines = content.splitlines()
    parts = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("class "):
            parts.append(f"L{i}: {stripped.split('(')[0].split(':')[0]}")
        elif stripped.startswith("def "):
            parts.append(f"L{i}: {stripped.split('(')[0]}")
        elif stripped.startswith(("import ", "from ")):
            parts.append(f"L{i}: {stripped}")
        if len(parts) >= 50:
            break
    return "\n".join(parts) if parts else f"{len(lines)} lines"


def _digest_javascript(content: str) -> str:
    """Extract JS/TS structure: classes, functions, exports with line numbers."""
    lines = content.splitlines()
    parts = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r'^(export\s+)?(class|interface|type|enum)\s+', stripped):
            parts.append(f"L{i}: {stripped.split('{')[0].strip()}")
        elif re.match(r'^(export\s+)?(async\s+)?function\s+', stripped):
            parts.append(f"L{i}: {stripped.split('{')[0].strip()}")
        elif re.match(r'^export\s+(default\s+)?(const|let|var)\s+', stripped):
            parts.append(f"L{i}: {stripped.split('=')[0].strip()}")
        if len(parts) >= 50:
            break
    return "\n".join(parts) if parts else f"{len(lines)} lines"


def _digest_fallback(content: str) -> str:
    """Fallback digest: line count + first/last 3 lines."""
    lines = content.splitlines()
    n = len(lines)
    if n <= 6:
        return f"{n} lines"
    first = "\n".join(lines[:3])
    last = "\n".join(lines[-3:])
    return f"{n} lines\nFirst 3:\n{first}\nLast 3:\n{last}"


def _generate_digest(file_path: str, content: str) -> str:
    """Generate structural digest based on file extension."""
    # Bail out on very large files to prevent hangs
    lines = content.splitlines()
    if len(lines) > 10000:
        return f"{len(lines)} lines (too large for structural digest)"

    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".py":
            return _digest_python(content)
        if ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
            return _digest_javascript(content)
        return _digest_fallback(content)
    except Exception:
        return _digest_fallback(content)


# ---------------------------------------------------------------------------
# Cache operations
# ---------------------------------------------------------------------------

def _cache_path(session_id: str) -> Path:
    """Get cache file path for a session."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', session_id) or "unknown"
    return CACHE_DIR / f"{safe_id}.json"


def _decisions_log_path() -> Path:
    """Get decisions log path."""
    return CACHE_DIR / "decisions.jsonl"


def _load_cache(session_id: str) -> dict:
    """Load cache for session. Recovers from corruption."""
    cp = _cache_path(session_id)
    if not cp.exists():
        return {"files": {}}
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "files" not in data:
            raise ValueError("invalid cache structure")
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        # Corruption recovery: delete and recreate
        try:
            cp.unlink()
        except OSError:
            pass
        return {"files": {}}


def _save_cache(session_id: str, cache: dict) -> None:
    """Save cache with 0o600 permissions. LRU prune if over limit."""
    files = cache.get("files", {})
    if len(files) > MAX_CACHE_ENTRIES:
        # Prune LRU: keep most recently accessed entries
        sorted_entries = sorted(files.items(), key=lambda x: x[1].get("last_access", 0))
        to_remove = len(files) - MAX_CACHE_ENTRIES
        for key, _ in sorted_entries[:to_remove]:
            del files[key]

    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cp = _cache_path(session_id)
    tmp = cp.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    tmp.rename(cp)


def _log_decision(decision: str, file_path: str, reason: str, session_id: str) -> None:
    """Append decision to decisions.jsonl."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = {
        "ts": time.time(),
        "decision": decision,
        "file": file_path,
        "reason": reason,
        "session": session_id,
    }
    log_path = _decisions_log_path()
    try:
        if not log_path.exists():
            fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main hook logic
# ---------------------------------------------------------------------------

def handle_read(hook_input: dict, mode: str, quiet: bool) -> None:
    """Handle a PreToolUse Read event.

    Args:
        hook_input: The hook JSON from stdin
        mode: "warn" or "block"
        quiet: Suppress stderr output
    """
    tool_input = hook_input.get("tool_input", {})
    raw_path = tool_input.get("file_path", "")
    if not raw_path:
        return

    # Security: canonicalize path
    file_path = str(Path(raw_path).resolve())
    session_id = hook_input.get("session_id", "unknown")

    # .contextignore check (hard block, regardless of mode)
    if _is_contextignored(file_path):
        _log_decision("block", file_path, "contextignore", session_id)
        if not quiet:
            print(f"[Read Cache] Blocked by .contextignore: {file_path}", file=sys.stderr)
        output = {
            "outputToolResult": f"[Token Optimizer] File blocked by .contextignore: {Path(file_path).name}\n"
                                f"This file matches a pattern in .contextignore and will not be read.\n"
                                f"Remove the pattern from .contextignore if you need access."
        }
        print(json.dumps(output))
        return

    # Skip binary files
    ext = Path(file_path).suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return

    # Load cache
    cache = _load_cache(session_id)
    files = cache.get("files", {})
    entry = files.get(file_path)

    offset = tool_input.get("offset", 0) or 0
    limit = tool_input.get("limit", 0) or 0

    if entry is None:
        # First read: cache it
        try:
            stat = os.stat(file_path)
            mtime = stat.st_mtime
        except OSError:
            return

        # Estimate tokens from file size
        try:
            size = stat.st_size
            tokens_est = max(1, size // 4)
        except (OSError, ValueError):
            tokens_est = 0

        files[file_path] = {
            "mtime": mtime,
            "offset": offset,
            "limit": limit,
            "tokens_est": tokens_est,
            "read_count": 1,
            "last_access": time.time(),
            "digest": "",
        }
        cache["files"] = files
        _save_cache(session_id, cache)
        _log_decision("allow", file_path, "first_read", session_id)
        return

    # Subsequent read: check staleness conditions
    # All three must be true to trigger warn/block:
    # 1. Same file path (already matched by cache key)
    # 2. mtime unchanged (re-stat the real file)
    # 3. Same offset + limit

    try:
        current_mtime = os.stat(file_path).st_mtime
    except OSError:
        # File deleted or inaccessible, remove from cache
        del files[file_path]
        cache["files"] = files
        _save_cache(session_id, cache)
        _log_decision("allow", file_path, "file_changed_or_deleted", session_id)
        return

    mtime_match = abs(current_mtime - entry["mtime"]) < 0.001
    range_match = (entry.get("offset", 0) == offset and entry.get("limit", 0) == limit)

    if not (mtime_match and range_match):
        # File changed or different range, update cache
        entry["mtime"] = current_mtime
        entry["offset"] = offset
        entry["limit"] = limit
        entry["read_count"] = entry.get("read_count", 0) + 1
        entry["last_access"] = time.time()
        entry["digest"] = ""
        _save_cache(session_id, cache)
        _log_decision("allow", file_path, "file_modified_or_different_range", session_id)
        return

    # All three conditions match: this is a redundant read
    entry["read_count"] = entry.get("read_count", 0) + 1
    entry["last_access"] = time.time()

    # Generate digest if not cached
    digest = entry.get("digest", "")
    if not digest:
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
            digest = _generate_digest(file_path, content)
            entry["digest"] = digest
        except OSError:
            digest = "(unable to generate digest)"

    _save_cache(session_id, cache)

    tokens_est = entry.get("tokens_est", 0)
    read_count = entry.get("read_count", 1)

    if mode == "block":
        _log_decision("block", file_path, f"redundant_read_{read_count}", session_id)
        if not quiet:
            print(f"[Read Cache] Blocked redundant read #{read_count}: {file_path} (~{tokens_est:,} tokens saved)", file=sys.stderr)
        output = {
            "outputToolResult": (
                f"[Token Optimizer] File already in context (read #{read_count}, unchanged).\n"
                f"Structural digest of {Path(file_path).name}:\n{digest}\n\n"
                f"To re-read, edit the file first or use a different offset/limit."
            )
        }
        print(json.dumps(output))
    else:
        # Warn mode: allow the read but output suggestion
        _log_decision("warn", file_path, f"redundant_read_{read_count}", session_id)
        if not quiet:
            print(f"[Read Cache] Redundant read #{read_count}: {file_path} (~{tokens_est:,} tokens)", file=sys.stderr)


def handle_clear(session_id: str, quiet: bool) -> None:
    """Clear read cache for a session (PreCompact handler)."""
    if session_id and session_id != "all":
        cp = _cache_path(session_id)
        if cp.exists():
            cp.unlink()
            if not quiet:
                print(f"[Read Cache] Cleared cache for session {session_id}", file=sys.stderr)
    elif session_id == "all":
        if CACHE_DIR.exists():
            for f in CACHE_DIR.glob("*.json"):
                f.unlink()
            if not quiet:
                print("[Read Cache] Cleared all caches", file=sys.stderr)


def handle_invalidate(hook_input: dict, quiet: bool) -> None:
    """Invalidate cache entry when a file is edited/written (PostToolUse handler)."""
    tool_name = hook_input.get("tool_name", "")
    if tool_name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return

    tool_input = hook_input.get("tool_input", {})
    raw_path = tool_input.get("file_path", "")
    if not raw_path:
        return

    file_path = str(Path(raw_path).resolve())
    session_id = hook_input.get("session_id", "unknown")
    cache = _load_cache(session_id)
    files = cache.get("files", {})

    if file_path in files:
        del files[file_path]
        cache["files"] = files
        _save_cache(session_id, cache)
        if not quiet:
            print(f"[Read Cache] Invalidated: {file_path}", file=sys.stderr)


def handle_stats(session_id: str) -> None:
    """Print cache stats for a session."""
    cache = _load_cache(session_id)
    files = cache.get("files", {})
    total_reads = sum(e.get("read_count", 0) for e in files.values())
    total_tokens = sum(e.get("tokens_est", 0) for e in files.values())

    # Count decisions from log
    log_path = _decisions_log_path()
    warns = blocks = allows = 0
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    if d.get("session") == session_id:
                        dec = d.get("decision", "")
                        if dec == "warn":
                            warns += 1
                        elif dec == "block":
                            blocks += 1
                        elif dec == "allow":
                            allows += 1
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    result = {
        "session_id": session_id,
        "cached_files": len(files),
        "total_reads": total_reads,
        "total_tokens_cached": total_tokens,
        "decisions": {"allow": allows, "warn": warns, "block": blocks},
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args

    # Subcommands
    if "--clear" in args:
        session_id = "all"
        for i, a in enumerate(args):
            if a == "--session" and i + 1 < len(args):
                session_id = args[i + 1]
        handle_clear(session_id, quiet)
        return

    if "--stats" in args:
        session_id = "unknown"
        for i, a in enumerate(args):
            if a == "--session" and i + 1 < len(args):
                session_id = args[i + 1]
        handle_stats(session_id)
        return

    if "--invalidate" in args:
        # PostToolUse mode: read hook input from stdin
        try:
            hook_input = json.loads(sys.stdin.read(1_000_000))
        except (json.JSONDecodeError, OSError):
            return
        handle_invalidate(hook_input, quiet)
        return

    # Default: PreToolUse Read handler
    # Check opt-in
    if not os.environ.get("TOKEN_OPTIMIZER_READ_CACHE"):
        return

    mode = os.environ.get("TOKEN_OPTIMIZER_READ_CACHE_MODE", "warn").lower()
    if mode not in ("warn", "block"):
        mode = "warn"

    try:
        hook_input = json.loads(sys.stdin.read(1_000_000))
    except (json.JSONDecodeError, OSError):
        return

    handle_read(hook_input, mode, quiet)


if __name__ == "__main__":
    main()
