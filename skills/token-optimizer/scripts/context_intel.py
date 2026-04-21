#!/usr/bin/env python3
"""Token Optimizer v5.6 - Context Intelligence + Activity Tracking + Decision Extraction.

PostToolUse handler that generates heuristic summaries for large tool outputs
and logs them to the Session Knowledge Store. Shadow mode only: no
systemMessage injection, no additionalContext, zero cache impact.

The accumulated summaries feed into Dynamic Compact Instructions (measure.py)
so the model gets session-aware guidance at compaction time.

Summary generation is heuristic (<30ms), not LLM-based. Extracts:
  - File paths mentioned in output
  - Error/warning lines
  - Line counts and size
  - First/last N lines for context

Cooldown: max 3 summaries per 5 minutes to avoid write contention.

Hook registration: PostToolUse on Bash|Read|Grep|Glob|mcp__.*
"""

from __future__ import annotations

import json
import re
import time

from hook_io import read_stdin_hook_input
from session_store import SessionStore

_OUTPUT_THRESHOLD = 8192  # Only summarize outputs >= 8K chars
_SUMMARY_CAP = 600  # Max chars per summary
_COOLDOWN_WINDOW = 300  # 5 minutes
_COOLDOWN_MAX = 3  # Max summaries per window
_STDIN_MAX_BYTES = 524_288  # 512KB: sufficient for 50KB extraction limit

_PATH_RE = re.compile(
    r"(?:^|[\s\"':=])(/[\w./-]{3,120}(?:\.\w{1,10})?)",
    re.MULTILINE,
)
_ERROR_RE = re.compile(
    r"^.*(?:error|Error|ERROR|FAIL|FAILED|panic|exception|Exception"
    r"|TypeError|ValueError|KeyError|ImportError|ModuleNotFoundError"
    r"|SyntaxError|RuntimeError|AttributeError|NameError|OSError"
    r"|FileNotFoundError|PermissionError|ConnectionError"
    r"|traceback|Traceback).*$",
    re.MULTILINE,
)
_WARNING_RE = re.compile(
    r"^.*(?:warning|Warning|WARNING|WARN|deprecated|DEPRECATED).*$",
    re.MULTILINE,
)
_ERROR_TYPE_RE = re.compile(
    r"(TypeError|ValueError|KeyError|ImportError|ModuleNotFoundError"
    r"|SyntaxError|RuntimeError|AttributeError|NameError|OSError"
    r"|FileNotFoundError|PermissionError|ConnectionError"
    r"|Error|FAIL|FAILED|panic|exception|Exception"
    r"|warning|Warning|WARNING|WARN|deprecated)",
)
_SAFE_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9._:/-]")




def _check_cooldown(store: SessionStore) -> bool:
    """Check cooldown via SQLite (persists across subprocess invocations)."""
    try:
        cutoff = time.time() - _COOLDOWN_WINDOW
        conn = store._connect()
        row = conn.execute(
            "SELECT COUNT(*) FROM context_intel_events WHERE timestamp > ?",
            (cutoff,),
        ).fetchone()
        return (row[0] if row else 0) < _COOLDOWN_MAX
    except Exception:
        return True


def _extract_paths(text: str) -> list[str]:
    matches = _PATH_RE.findall(text[:50_000])
    seen: set[str] = set()
    paths: list[str] = []
    for m in matches:
        if m not in seen and not m.startswith("/dev/") and not m.startswith("/proc/"):
            seen.add(m)
            paths.append(m)
            if len(paths) >= 10:
                break
    return paths


def _sanitize_signal(raw_line: str, prefix: str) -> str:
    """Extract structural digest from an error/warning line.

    Returns e.g. "ERR: TypeError in /src/auth.py" rather than raw text,
    to prevent tool output content from flowing verbatim into compaction
    prompts.
    """
    error_type = _ERROR_TYPE_RE.search(raw_line)
    type_str = error_type.group(0) if error_type else "unknown"
    paths = _PATH_RE.findall(raw_line)
    path_str = f" in {paths[0]}" if paths else ""
    return f"{prefix}: {type_str}{path_str}"


def _extract_signals(text: str) -> list[str]:
    signals: list[str] = []
    seen: set[str] = set()

    errors = _ERROR_RE.findall(text[:50_000])
    for e in errors:
        sanitized = _sanitize_signal(e, "ERR")
        if sanitized not in seen:
            seen.add(sanitized)
            signals.append(sanitized)
            if len(signals) >= 5:
                break

    warnings = _WARNING_RE.findall(text[:50_000])
    for w in warnings:
        sanitized = _sanitize_signal(w, "WARN")
        if sanitized not in seen:
            seen.add(sanitized)
            signals.append(sanitized)
            if len(signals) >= 8:
                break

    return signals


def _sanitize_tool_name(name: str) -> str:
    sanitized = _SAFE_TOOL_NAME_RE.sub("", name)
    return sanitized[:64]


def _summarize_output(tool_name: str, output: str) -> str:
    tool_name = _sanitize_tool_name(tool_name)
    lines = output.splitlines()
    line_count = len(lines)
    char_count = len(output)

    parts: list[str] = []
    parts.append(f"{tool_name}: {line_count} lines, {char_count} chars")

    paths = _extract_paths(output)
    if paths:
        parts.append(f"Files: {', '.join(paths[:5])}")
        if len(paths) > 5:
            parts.append(f"  +{len(paths) - 5} more paths")

    signals = _extract_signals(output)
    for s in signals[:4]:
        parts.append(s)

    if line_count > 20 and not signals:
        parts.append(f"Output: {line_count} lines, no errors detected")

    summary = "\n".join(parts)
    return summary[:_SUMMARY_CAP]


_DECISION_RE = re.compile(
    r'\b(chose|decided|because|instead of|went with|going with|switched to|'
    r'prefer|better to|should use|will use|picking|opting for|let\'s use|'
    r'using .+ over|settled on|sticking with)\b',
    re.IGNORECASE,
)
_MAX_DECISIONS = 10
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def _extract_decisions(text: str, store: SessionStore) -> None:
    """Extract decision statements from tool output and store incrementally.

    Uses split-then-filter to avoid catastrophic regex backtracking on
    large outputs without sentence-ending punctuation (build logs, JSON).
    Wraps the read-modify-write in BEGIN IMMEDIATE to prevent lost updates
    under concurrent PostToolUse subprocesses.
    """
    if len(text) < 50:
        return
    sample = text[:5000]
    if not _DECISION_RE.search(sample):
        return

    sentences = [
        s.strip() for s in _SENTENCE_SPLIT_RE.split(sample)
        if 20 <= len(s.strip()) <= 200
    ]
    new_decisions = []
    for sentence in sentences:
        if _DECISION_RE.search(sentence):
            new_decisions.append(sentence.strip()[:150])
        if len(new_decisions) >= 3:
            break

    if not new_decisions:
        return

    try:
        conn = store._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM session_meta WHERE key = ?",
                ("session_decisions",),
            ).fetchone()
            existing = json.loads(row[0]) if row else []

            if len(existing) >= _MAX_DECISIONS:
                conn.execute("COMMIT")
                return

            for d in new_decisions:
                if d not in existing:
                    existing.append(d)
                    if len(existing) >= _MAX_DECISIONS:
                        break

            conn.execute(
                "INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, ?)",
                ("session_decisions", json.dumps(existing, ensure_ascii=False)),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
    except Exception:
        pass


def _has_error_signals(text: str) -> bool:
    """Quick check for error indicators in tool output."""
    if len(text) < 10:
        return False
    sample = text[:20_000]
    return bool(_ERROR_RE.search(sample))


def handle_post_tool_use() -> None:
    hook_input = read_stdin_hook_input(_STDIN_MAX_BYTES)
    if not hook_input:
        return

    tool_name = hook_input.get("tool_name", "")
    tool_use_id = hook_input.get("tool_use_id", "")
    tool_response = hook_input.get("tool_response", "")
    session_id = hook_input.get("session_id", "")
    tool_input = hook_input.get("tool_input", {})

    if not session_id:
        return

    try:
        store = SessionStore(session_id)
        try:
            # Activity tracking runs on every tool call, regardless of output size
            try:
                from activity_tracker import log_tool_use
                command = ""
                if tool_name == "Bash" and isinstance(tool_input, dict):
                    command = tool_input.get("command", "")
                has_error = _has_error_signals(tool_response) if isinstance(tool_response, str) else False
                log_tool_use(store, tool_name, command=command, has_error=has_error)
            except Exception:
                pass

            # Decision extraction on outputs likely to contain decisions (>500 chars)
            if isinstance(tool_response, str) and len(tool_response) > 500:
                try:
                    _extract_decisions(tool_response, store)
                except Exception:
                    pass

            # Context intel summary only for large outputs
            if not tool_use_id or not isinstance(tool_response, str) or len(tool_response) < _OUTPUT_THRESHOLD:
                return

            if not _check_cooldown(store):
                return
            summary = _summarize_output(tool_name, tool_response)
            store.insert_intel_event(
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                summary=summary,
                output_chars=len(tool_response),
            )
        finally:
            store.close()
    except Exception:
        pass


if __name__ == "__main__":
    handle_post_tool_use()
