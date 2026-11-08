"""U1: Cursor on-disk transcript adapter.

Reads Cursor's on-disk transcript JSONL and translates it into
Claude-Code-shaped transcript records, the Hermes precedent exactly
(``hermes_session.messages_to_transcript_records`` output shape), so the
existing ``compact_capture(_trusted_transcript=...)`` pipeline consumes it
with no new dict-shaped contract.

Source schema (bundle-proven, Cursor CLI 2026.11.05 TranscriptStore,
7923.index.js): one JSON object per line, ``{"role": <role>, "message":
{"content": [...]}}`` with ``{type:"text",text}`` and ``{type:"tool_use",
name,input}`` content entries; terminal lines ``{"type":"turn_ended",
status, ...}``. Disk transcripts carry NO tool-result text - the writer
reduces tool-result content to ``{toolName}`` and drops it (U/R at
9679-11290). Callers needing failure detail must use the observed ledger /
hook payloads instead; this adapter never fabricates results.

The translated file is credential-redacted and written 0o600 under
``<cursor_home>/token-optimizer/transcripts/`` before any downstream
consumer reads it. Reads are byte-capped and confined under
``<cursor_home>/projects`` (a crafted path is refused, never followed).
Unknown or malformed lines are skipped with a count, never fatal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
_MAX_MESSAGES = 4000
_WRITER_SCHEMA = "cursor-transcriptstore-jsonl@2026.11.05"


def _confined_transcript_path(transcript_path: Any, home: Path) -> Optional[Path]:
    """Resolve a transcript path confined under <home>/projects, else None."""
    if not transcript_path:
        return None
    try:
        path = Path(str(transcript_path))
        root = (home / "projects").resolve(strict=False)
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def read_cursor_messages(transcript_path: Any, home: Path) -> tuple[list[dict], int]:
    """Read a Cursor transcript into normalized messages.

    Returns (messages, skipped_lines). Each message: {"role": str,
    "content": str, "tool_calls": [{"name": str, "input": dict}],
    "turn_ended": bool}. Malformed JSON lines and unknown shapes are
    skipped and counted. Bounded bytes and message count. Never raises.
    """
    path = _confined_transcript_path(transcript_path, home)
    if path is None:
        return [], 0
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_TRANSCRIPT_BYTES + 1)
        raw = raw[:_MAX_TRANSCRIPT_BYTES]
    except OSError:
        return [], 0
    messages: list[dict] = []
    skipped = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(rec, dict):
            skipped += 1
            continue
        if rec.get("type") == "turn_ended":
            # The ONE piece of terminal evidence on disk: preserve status and
            # error verbatim. Absence of tool results elsewhere must never be
            # read as "tools ran clean"; a turn_ended error is the only
            # disk-backed failure signal this format carries.
            if messages:
                messages[-1]["turn_ended"] = True
                status = rec.get("status")
                if isinstance(status, str) and status:
                    messages[-1]["turn_ended_status"] = status
                error = rec.get("error")
                if isinstance(error, str) and error:
                    messages[-1]["turn_ended_error"] = error
            continue
        if rec.get("type") == "conversation_meta":
            continue  # known schema line (overview/metadata), not a message
        role = rec.get("role")
        body = rec.get("message")
        if not isinstance(role, str) or not isinstance(body, dict):
            skipped += 1  # unknown shapes and malformed records
            continue
        content = body.get("content")
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text)
                elif btype == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str) and name:
                        tool_calls.append({
                            "name": name,
                            "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                        })
        if not text_parts and not tool_calls:
            continue
        messages.append({
            "role": role,
            "content": "\n\n".join(text_parts),
            "tool_calls": tool_calls,
            "turn_ended": False,
        })
        if len(messages) >= _MAX_MESSAGES:
            break
    return messages, skipped


def messages_to_cc_records(messages: list[dict]) -> list[dict]:
    """Translate normalized Cursor messages into CC-shaped transcript records.

    Same output contract as hermes_session.messages_to_transcript_records:
    user rows carry text content, assistant rows carry text + tool_use
    blocks; rows with neither are dropped. Cursor disk transcripts have no
    tool-result rows to skip (the writer never persists them): a translated
    assistant record with tool_use blocks says NOTHING about whether those
    calls succeeded - absence of results is not evidence of health, and no
    consumer may present it as error-free execution.
    """
    records: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = msg.get("content") or ""
        if not isinstance(text, str):
            text = ""
        if role == "user":
            if text.strip():
                records.append({"type": "user", "message": {"role": "user", "content": text}})
        elif role == "assistant":
            blocks: list[dict] = []
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                if isinstance(name, str) and name:
                    blocks.append({"type": "tool_use", "name": name, "input": call.get("input") or {}})
            # Carry the disk's only terminal failure evidence INTO the CC
            # records as a bracketed provenance-tagged text block, so
            # compact_capture's error-context extraction can see a
            # turn_ended error. A success line adds nothing (absence of
            # error is the disk's healthy signal, stated, not fabricated).
            status = msg.get("turn_ended_status")
            if msg.get("turn_ended") and isinstance(status, str) and status != "success":
                note = f"[cursor turn_ended status={status}"
                err = msg.get("turn_ended_error")
                if isinstance(err, str) and err.strip():
                    note += f": {err.strip()[:200]}"
                blocks.append({"type": "text", "text": note + "]"})
            if blocks:
                records.append({"type": "assistant", "message": {"role": "assistant", "content": blocks}})
    return records


def translate_transcript(transcript_path: Any, home: Path, conversation_id: str) -> Optional[Path]:
    """Translate a Cursor transcript to a redacted CC-shaped JSONL file.

    Writes ``<home>/token-optimizer/transcripts/<conversation_id>.jsonl``
    with mode 0o600 (never following a pre-existing symlink at the target)
    and returns its path, or None when there is nothing to translate.
    Credential redaction runs on every serialized record line BEFORE the
    file exists on disk. Never raises.
    """
    try:
        safe_id = "".join(c for c in str(conversation_id) if c.isalnum() or c in "_-")[:64]
        if len(safe_id) < 6:
            return None
        messages, _skipped = read_cursor_messages(transcript_path, home)
        if not messages:
            return None
        records = messages_to_cc_records(messages)
        if not records:
            return None

        from archive_result import _redact_credentials

        lines = [_redact_credentials(json.dumps(r, ensure_ascii=False)) for r in records]
        out_dir = Path(home) / "token-optimizer" / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        out_path = out_dir / f"{safe_id}.jsonl"
        if out_path.is_symlink():
            return None
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except BaseException:
            raise
        return out_path
    except Exception:
        return None
