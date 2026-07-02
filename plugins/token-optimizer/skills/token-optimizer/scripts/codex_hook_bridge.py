#!/usr/bin/env python3
"""Codex hook adapters for Token Optimizer.

Codex currently supports a smaller hook surface than Claude Code. This bridge
keeps the existing measurement engine intact and adapts the outputs Codex can
actually consume today: SessionStart continuity context and UserPromptSubmit
quality nudges.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable

import codex_session
import measure
from hook_io import read_stdin_hook_input

# Emit a sprawl nudge once this many subagents are open concurrently.
_SUBAGENT_SPRAWL_THRESHOLD = 8


def _capture_stdout(func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            func(*args, **kwargs)
    except Exception as exc:
        print(f"[Token Optimizer] Codex hook helper failed: {exc}", file=sys.stderr)
        return ""
    return buffer.getvalue()


def _emit_additional_context(event_name: str, text: str) -> None:
    text = text.strip()
    if not text:
        return
    # If stdout is already valid JSON with a hookSpecificOutput key, pass it
    # through unchanged (#81). This prevents double-wrapping when a helper
    # emits its own Codex envelope.
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "hookSpecificOutput" in parsed:
        print(json.dumps(parsed))
        return
    print(
        json.dumps(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": text,
                },
            }
        )
    )


def _collect_system_messages(raw_output: str) -> str:
    messages: list[str] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = payload.get("systemMessage")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return "\n\n".join(messages)


def _extract_prompt_text(hook_input: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "message", "input"):
        value = hook_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    messages = hook_input.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            content = item.get("content") or item.get("message")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def _has_matching_checkpoint(session_id: str | None) -> bool:
    if not session_id:
        return False
    safe_session_id = measure.sanitize_session_id(session_id)
    for checkpoint in measure.list_checkpoints(max_age_minutes=60 * 24):
        if safe_session_id in checkpoint.get("filename", ""):
            return True
    return False


def handle_session_start() -> None:
    hook_input = read_stdin_hook_input()
    session_id = hook_input.get("session_id")
    source = str(hook_input.get("source", "")).strip().lower()

    # Keep existing self-healing behavior even when no context is injected.
    _capture_stdout(measure.run_ensure_health)

    if source == "resume" and _has_matching_checkpoint(session_id):
        context = _capture_stdout(
            measure.compact_restore,
            session_id=session_id,
            is_compact=True,
        )
    elif source == "clear":
        context = _capture_stdout(
            measure.compact_restore,
            session_id=session_id,
            new_session_only=True,
        )
    else:
        context = ""

    _emit_additional_context("SessionStart", context)


def handle_user_prompt_submit() -> None:
    hook_input = read_stdin_hook_input()
    transcript_path = hook_input.get("transcript_path")
    if transcript_path and not codex_session.is_codex_session_path(transcript_path):
        transcript_path = None
    session_id = hook_input.get("session_id")
    raw_output = _capture_stdout(
        measure.quality_cache,
        quiet=True,
        session_jsonl=transcript_path,
    )
    additional_context = _collect_system_messages(raw_output)
    prompt_text = _extract_prompt_text(hook_input)
    cwd = hook_input.get("cwd")
    if not cwd and transcript_path:
        try:
            cwd = str(Path(transcript_path).parent)
        except TypeError:
            cwd = None
    try:
        hint_context = measure.codex_prompt_hints(
            prompt_text=prompt_text,
            session_id=session_id,
            cwd=cwd,
        )
    except Exception as exc:
        print(f"[Token Optimizer] Codex hint helper failed: {exc}", file=sys.stderr)
        hint_context = ""
    if hint_context:
        additional_context = "\n\n".join(part for part in (additional_context, hint_context.strip()) if part)

    # Verbosity-steer: inject a conciseness nudge when context is under pressure.
    # Mirrors the Claude Code verbosity-steer hook in measure.py.
    # run_verbosity_steer returns the JSON payload string (doesn't print to stdout),
    # so we call it directly instead of through _capture_stdout.
    try:
        verbosity_payload = measure.run_verbosity_steer(
            transcript_path=transcript_path,
            session_id=session_id,
        )
        if verbosity_payload:
            import json as _json
            parsed = _json.loads(verbosity_payload.strip())
            ctx = (
                parsed.get("hookSpecificOutput", {})
                .get("additionalContext", "")
            )
            if ctx:
                additional_context = "\n\n".join(
                    part for part in (additional_context, ctx.strip()) if part
                )
    except Exception:
        pass

    _emit_additional_context("UserPromptSubmit", additional_context)


def _subagent_log_path(session_id: str | None) -> Path:
    """Path to the per-session subagent event log.

    This log exists ONLY to drive the real-time sprawl nudge (a live open-count
    while a session runs). It is NOT a reporting source. Authoritative subagent
    counts and token costs come from ``codex_state.subagent_costs()`` (the
    ``thread_spawn_edges`` SQLite table), which is historical and complete. The
    quality scorer and audit never read this file (KTD8: DB primary, JSONL
    fallback, hooks for nudge only).
    """
    # Cap length: a crafted oversized session_id would otherwise produce a
    # filename past the OS limit, silently disabling sprawl tracking.
    safe = (measure.sanitize_session_id(session_id) if session_id else "")[:128]
    base = measure.QUALITY_CACHE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"codex-subagents-{safe or 'unknown'}.jsonl"


def _record_subagent_event(session_id: str | None, event: str) -> None:
    """Append a subagent lifecycle event.

    Append-only with ``O_APPEND`` makes each small line write atomic on local
    POSIX filesystems (the kernel holds the inode lock for the write), so
    concurrent SubagentStart/Stop hook invocations never lose an increment the
    way a read-modify-write counter would. (Not guaranteed on NFS/CIFS, which is
    not a supported location for the cache dir.)
    """
    path = _subagent_log_path(session_id)
    line = (json.dumps({"event": event, "ts": time.time()}) + "\n").encode("utf-8")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"[Token Optimizer] Codex subagent log failed: {exc}", file=sys.stderr)


def _open_subagent_count(session_id: str | None) -> int:
    path = _subagent_log_path(session_id)
    starts = stops = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "start":
                    starts += 1
                elif rec.get("event") == "stop":
                    stops += 1
    except OSError:
        return 0
    return max(0, starts - stops)


def handle_subagent_start() -> None:
    hook_input = read_stdin_hook_input()
    session_id = hook_input.get("session_id")
    # Subagent identity (v0.134) is optional; we count regardless of whether
    # the payload carries it, so a missing identity never breaks the nudge.
    _record_subagent_event(session_id, "start")
    open_count = _open_subagent_count(session_id)
    if open_count >= _SUBAGENT_SPRAWL_THRESHOLD:
        _emit_additional_context(
            "SubagentStart",
            f"[Token Optimizer] {open_count} subagents are running concurrently. "
            "Each carries its own context and token cost - consider closing finished "
            "agents or consolidating work to reduce sprawl.",
        )
    # Below threshold: stay silent so this hook adds no per-spawn noise.


def handle_subagent_stop() -> None:
    hook_input = read_stdin_hook_input()
    session_id = hook_input.get("session_id")
    _record_subagent_event(session_id, "stop")
    # Stop only updates the count log; never emits output.


def main() -> int:
    try:
        try:
            from utf8_io import enforce_utf8_io
            enforce_utf8_io()
        except Exception:
            pass
        if len(sys.argv) < 2:
            return 0

        command = sys.argv[1].strip().lower()
        if command == "session-start":
            handle_session_start()
        elif command == "user-prompt-submit":
            handle_user_prompt_submit()
        elif command == "subagent-start":
            handle_subagent_start()
        elif command == "subagent-stop":
            handle_subagent_stop()
    except Exception as exc:
        print(f"[Token Optimizer] Codex hook bridge failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
