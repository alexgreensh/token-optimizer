#!/usr/bin/env python3
"""Token Optimizer v5.5 - Context Intelligence (Shadow Mode).

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
import sys
import time

from session_store import SessionStore

_OUTPUT_THRESHOLD = 8192  # Only summarize outputs >= 8K chars
_SUMMARY_CAP = 600  # Max chars per summary
_COOLDOWN_WINDOW = 300  # 5 minutes
_COOLDOWN_MAX = 3  # Max summaries per window

_cooldown_timestamps: list[float] = []

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


def _read_stdin_hook_input(max_bytes: int = 1_048_576) -> dict:
    try:
        import select
        if select.select([sys.stdin], [], [], 0.1)[0]:
            data = sys.stdin.read(max_bytes)
            return json.loads(data) if data else {}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _check_cooldown() -> bool:
    now = time.time()
    cutoff = now - _COOLDOWN_WINDOW
    _cooldown_timestamps[:] = [t for t in _cooldown_timestamps if t > cutoff]
    if len(_cooldown_timestamps) >= _COOLDOWN_MAX:
        return False
    _cooldown_timestamps.append(now)
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


def _extract_signals(text: str) -> list[str]:
    signals: list[str] = []
    seen: set[str] = set()

    errors = _ERROR_RE.findall(text[:50_000])
    for e in errors:
        line = e.strip()[:120]
        if line and line not in seen:
            seen.add(line)
            signals.append(f"ERR: {line}")
            if len(signals) >= 5:
                return signals

    warnings = _WARNING_RE.findall(text[:50_000])
    for w in warnings:
        line = w.strip()[:120]
        if line and line not in seen:
            seen.add(line)
            signals.append(f"WARN: {line}")
            if len(signals) >= 8:
                break

    return signals


def _summarize_output(tool_name: str, output: str) -> str:
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
        first_lines = [ln.strip() for ln in lines[:3] if ln.strip()]
        last_lines = [ln.strip() for ln in lines[-3:] if ln.strip()]
        if first_lines:
            parts.append(f"Start: {first_lines[0][:80]}")
        if last_lines and last_lines != first_lines:
            parts.append(f"End: {last_lines[-1][:80]}")

    summary = "\n".join(parts)
    return summary[:_SUMMARY_CAP]


def handle_post_tool_use() -> None:
    hook_input = _read_stdin_hook_input()
    if not hook_input:
        return

    tool_name = hook_input.get("tool_name", "")
    tool_use_id = hook_input.get("tool_use_id", "")
    tool_response = hook_input.get("tool_response", "")
    session_id = hook_input.get("session_id", "")

    if not tool_response or len(tool_response) < _OUTPUT_THRESHOLD:
        return

    if not session_id or not tool_use_id:
        return

    if not _check_cooldown():
        return

    summary = _summarize_output(tool_name, tool_response)

    try:
        store = SessionStore(session_id)
        try:
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
