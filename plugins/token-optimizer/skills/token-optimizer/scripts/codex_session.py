#!/usr/bin/env python3
"""Codex session JSONL adapter for Token Optimizer.

Codex stores session logs in a different JSONL shape than Claude Code. This
module normalizes the parts Token Optimizer needs so the mature quality,
trends, and dashboard pipeline can stay shared.
"""

from __future__ import annotations

import itertools
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_env import codex_home

CHARS_PER_TOKEN = 4
MAX_PARSE_FILE_BYTES = 96 * 1024 * 1024
MAX_JSONL_LINE_CHARS = 8 * 1024 * 1024
_UNKNOWN_MODEL = "unknown"
_DEFAULT_MODEL = "codex"
TOOL_ALIASES = {
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "write_stdin": "Bash",
    "spawn_agent": "Task",
    "wait_agent": "Task",
    "close_agent": "Task",
    "view_image": "ViewImage",
}

READ_CMD_RE = re.compile(r"\b(?:cat|sed|nl|head|tail|less|rg|grep)\b")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_ERROR_RE = re.compile(r"\b(error|failed|traceback|exception|permission denied|not found)\b", re.IGNORECASE)


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError):
        return None


def _extract_text(payload: dict[str, Any]) -> str:
    payload_type = payload.get("type")
    if payload_type in {"user_message", "agent_message"}:
        return str(payload.get("message") or "")
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _parse_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_name(name: str) -> str:
    return TOOL_ALIASES.get(name, name or "unknown")


def _extract_model(payload: dict[str, Any]) -> str | None:
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            model = settings.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


def _estimate_tokens(text: str | int) -> int:
    if isinstance(text, int):
        return max(0, text // CHARS_PER_TOKEN)
    return max(0, len(text.encode("utf-8", errors="replace")) // CHARS_PER_TOKEN)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iter_json_records(filepath: str | Path, *, skip_large_file: bool = True):
    """Yield parsed JSONL records without letting pathological logs dominate.

    Codex can occasionally leave multi-GB session files or individual records
    with enormous embedded tool output. Dashboard and hook refresh paths need
    bounded work more than perfect telemetry from those outlier transcripts.
    """
    path = Path(filepath)
    if skip_large_file:
        try:
            if path.stat().st_size > MAX_PARSE_FILE_BYTES:
                return
        except OSError:
            return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line) > MAX_JSONL_LINE_CHARS:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except (PermissionError, OSError):
        return


def _token_usage(payload: dict[str, Any], *, cumulative: bool = True) -> dict[str, int] | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage_key = "total_token_usage" if cumulative else "last_token_usage"
    fallback_key = "last_token_usage" if cumulative else "total_token_usage"
    usage = info.get(usage_key) or info.get(fallback_key)
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": _safe_int(usage.get("input_tokens")),
        "cached_input_tokens": _safe_int(usage.get("cached_input_tokens")),
        "output_tokens": _safe_int(usage.get("output_tokens")),
        "reasoning_output_tokens": _safe_int(usage.get("reasoning_output_tokens")),
        "total_tokens": _safe_int(usage.get("total_tokens")),
        "model_context_window": _safe_int(info.get("model_context_window")),
    }


_TOPIC_PREFIXES = (
    "implement the following plan:",
    "please implement",
    "can you help me",
    "i need help with",
    "help me",
    "i want to",
    "i'd like to",
)


def _extract_topic(text: str) -> str | None:
    text = " ".join(text.split())
    if not text:
        return None
    lower = text.lower()
    for prefix in _TOPIC_PREFIXES:
        if lower.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if text.startswith("# "):
        first_line = text.split("\n", 1)[0]
        text = first_line.lstrip("# ").strip()
    if not text:
        return None
    return text[:117] + "..." if len(text) > 120 else text


def _safe_session_id(value: str | None) -> str:
    if not value:
        return ""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", value)
    return sanitized if len(sanitized) >= 6 else ""


def _looks_like_error_text(text: str) -> bool:
    return bool(_ERROR_RE.search(text))


def session_roots() -> tuple[Path, ...]:
    home = codex_home()
    return (home / "sessions", home / "archived_sessions")


def is_codex_session_path(path: str | Path) -> bool:
    p = Path(path).expanduser()
    try:
        resolved = p.resolve(strict=False)
        return any(resolved.is_relative_to(root.resolve(strict=False)) for root in session_roots())
    except (OSError, ValueError):
        return False


def find_all_jsonl_files(days: int = 30, max_files: int = 500) -> list[tuple[Path, float, str]]:
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    results: list[tuple[Path, float, str]] = []
    for root in session_roots():
        if not root.exists():
            continue
        for jf in itertools.islice(root.rglob("*.jsonl"), max_files):
            try:
                mtime = jf.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            project = _project_name_from_file(jf)
            results.append((jf, mtime, project))
    results.sort(key=lambda item: item[1], reverse=True)
    return results


def find_current_session_jsonl() -> Path | None:
    files = find_all_jsonl_files(days=90, max_files=10)
    return files[0][0] if files else None


def find_session_jsonl_by_id(session_id: str) -> Path | None:
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        return None
    exact_matches: list[Path] = []
    for root in session_roots():
        if not root.exists():
            continue
        for jf in itertools.islice(root.rglob(f"*{safe_id}*.jsonl"), 50):
            meta_id = _session_meta_id(jf)
            if jf.stem == safe_id or safe_id in jf.stem or meta_id == safe_id or (meta_id and meta_id.startswith(safe_id)):
                exact_matches.append(jf)
    if not exact_matches:
        return None
    exact_matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return exact_matches[0]


def _session_meta_id(path: Path) -> str | None:
    for record in _iter_json_records(path, skip_large_file=False):
        if record.get("type") != "session_meta":
            continue
        value = _payload(record).get("id")
        return _safe_session_id(str(value)) if value else None
    return None


def _project_name_from_file(path: Path) -> str:
    for record in _iter_json_records(path, skip_large_file=False):
        if record.get("type") != "session_meta":
            continue
        cwd = _payload(record).get("cwd")
        if cwd:
            return Path(str(cwd)).name or str(cwd)
        break
    return path.parent.name


def parse_session_jsonl(filepath: str | Path) -> dict[str, Any] | None:
    skills_used: dict[str, int] = {}
    subagents_used: dict[str, int] = {}
    tool_calls: dict[str, int] = {}
    model_usage: dict[str, int] = {}
    model_usage_breakdown: dict[str, dict[str, int]] = {}
    version = None
    slug = None
    topic = None
    first_ts = None
    last_ts = None
    message_count = 0
    api_calls = 0
    input_text_chars = 0
    output_text_chars = 0
    tool_output_chars = 0
    last_usage: dict[str, int] | None = None
    current_model = _UNKNOWN_MODEL
    per_model_usage: dict[str, dict[str, int]] = {}
    rate_limits_latest: dict[str, Any] | None = None
    effort_counts: dict[str, int] = {}
    tool_durations_ms: list[float] = []
    task_durations_ms: list[float] = []
    ttft_ms: list[float] = []

    for record in _iter_json_records(filepath):
        payload = _payload(record)
        payload_type = payload.get("type")

        ts = _parse_ts(record.get("timestamp") or payload.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        if record.get("type") == "session_meta":
            version = version or payload.get("cli_version")
            slug = slug or payload.get("id")
        elif record.get("type") == "turn_context":
            effort = payload.get("effort")
            if isinstance(effort, str) and effort.strip():
                effort_counts[effort.strip()] = effort_counts.get(effort.strip(), 0) + 1

        model = _extract_model(payload)
        if model:
            current_model = model

        if payload_type == "token_count":
            usage = _token_usage(payload, cumulative=True)
            if usage:
                last_usage = usage
            rl = _extract_rate_limits(payload)
            if rl:
                rate_limits_latest = rl
            turn_usage = _token_usage(payload, cumulative=False)
            if turn_usage:
                model_key = current_model if current_model != _UNKNOWN_MODEL else _DEFAULT_MODEL
                bucket = per_model_usage.setdefault(
                    model_key,
                    {"fresh_input": 0, "cache_read": 0, "cache_create": 0, "output": 0},
                )
                bucket["fresh_input"] += turn_usage["input_tokens"]
                bucket["cache_read"] += turn_usage["cached_input_tokens"]
                bucket["output"] += turn_usage["output_tokens"] + turn_usage["reasoning_output_tokens"]

        elif payload_type == "collab_agent_spawn_end":
            # Modern subagent spawn. The legacy spawn_agent function_call path
            # below also counts subagents; a session that spans a mid-session
            # Codex upgrade (rare) could emit both and double-count.
            role = str(payload.get("new_agent_role") or payload.get("new_agent_nickname") or "subagent")
            subagents_used[role] = subagents_used.get(role, 0) + 1
            tool_calls["Task"] = tool_calls.get("Task", 0) + 1

        elif payload_type == "task_complete":
            _append_positive(task_durations_ms, payload.get("duration_ms"))
            _append_positive(ttft_ms, payload.get("time_to_first_token_ms"))

        elif payload_type in {"user_message", "message", "agent_message"}:
            text = _extract_text(payload)
            role = payload.get("role")
            if payload_type == "user_message" or role == "user":
                topic = topic or _extract_topic(text)
                input_text_chars += len(text)
            elif payload_type == "agent_message" or role == "assistant":
                output_text_chars += len(text)
            else:
                input_text_chars += len(text)
            message_count += 1

        elif payload_type in {"function_call", "custom_tool_call"}:
            raw_name = str(payload.get("name") or "unknown")
            name = _tool_name(raw_name)
            tool_calls[name] = tool_calls.get(name, 0) + 1
            api_calls += 1
            if raw_name == "spawn_agent":
                args = _parse_arguments(payload)
                agent_type = str(args.get("agent_type") or "default")
                subagents_used[agent_type] = subagents_used.get(agent_type, 0) + 1

        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            tool_output_chars += len(str(payload.get("output") or ""))
        elif payload_type in {"exec_command_end", "patch_apply_end"}:
            tool_output_chars += len(_event_output_text(payload))
            _append_duration(tool_durations_ms, payload.get("duration"))
        elif payload_type == "mcp_tool_call_end":
            _append_duration(tool_durations_ms, payload.get("duration"))

    if message_count == 0 and api_calls == 0:
        return None

    duration_minutes = 0
    if first_ts and last_ts:
        duration_minutes = max(0, (last_ts - first_ts).total_seconds() / 60)

    if last_usage:
        fresh_input = last_usage["input_tokens"]
        cache_read = last_usage["cached_input_tokens"]
        estimated_input = fresh_input + cache_read
        estimated_output = last_usage["output_tokens"] + last_usage["reasoning_output_tokens"]
        token_source = "codex_token_count"
    else:
        fresh_input = _estimate_tokens(input_text_chars + tool_output_chars)
        cache_read = 0
        estimated_input = fresh_input
        estimated_output = _estimate_tokens(output_text_chars)
        token_source = "char_estimate"

    if per_model_usage:
        model_usage_breakdown = per_model_usage
        for model, parts in model_usage_breakdown.items():
            model_usage[model] = parts["fresh_input"] + parts["cache_create"] + parts["output"]
    else:
        model = current_model if current_model != _UNKNOWN_MODEL else _DEFAULT_MODEL
        billable_estimate = fresh_input + estimated_output
        model_usage[model] = billable_estimate
        model_usage_breakdown[model] = {
            "fresh_input": fresh_input,
            "cache_read": cache_read,
            "cache_create": 0,
            "output": estimated_output,
        }

    return {
        "version": version,
        "slug": slug,
        "topic": topic,
        "duration_minutes": duration_minutes,
        "total_input_tokens": estimated_input,
        "total_output_tokens": estimated_output,
        "total_cache_read": cache_read,
        "total_cache_create": 0,
        "total_cache_create_1h": 0,
        "total_cache_create_5m": 0,
        "model_context_window": last_usage["model_context_window"] if last_usage else None,
        "cache_hit_rate": cache_read / estimated_input if estimated_input else 0.0,
        "avg_call_gap_seconds": None,
        "max_call_gap_seconds": None,
        "p95_call_gap_seconds": None,
        "model_usage": model_usage,
        "model_usage_breakdown": model_usage_breakdown,
        "skills_used": skills_used,
        "subagents_used": subagents_used,
        "tool_calls": tool_calls,
        "message_count": message_count,
        "api_calls": api_calls,
        "first_ts": first_ts.isoformat() if first_ts else None,
        "estimated": token_source != "codex_token_count",
        "runtime": "codex",
        "token_source": token_source,
        "rate_limits": rate_limits_latest,
        "effort": max(effort_counts, key=effort_counts.get) if effort_counts else None,
        "effort_breakdown": dict(effort_counts),
        "tool_duration_p90_ms": _percentile(tool_durations_ms, 90),
        "task_duration_ms_max": max(task_durations_ms) if task_durations_ms else None,
        "ttft_ms_avg": round(sum(ttft_ms) / len(ttft_ms), 1) if ttft_ms else None,
    }


def parse_session_turns(filepath: str | Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_tools: list[str] = []
    pending_usage: dict[str, int] | None = None
    current_model = _UNKNOWN_MODEL
    turn_index = 0
    for record in _iter_json_records(filepath):
        payload = _payload(record)
        payload_type = payload.get("type")
        model = _extract_model(payload)
        if model:
            current_model = model
        if payload_type in {"function_call", "custom_tool_call"}:
            pending_tools.append(_tool_name(str(payload.get("name") or "unknown")))
        elif payload_type == "token_count":
            usage = _token_usage(payload, cumulative=False)
            if usage:
                if turns:
                    turn = turns[-1]
                    turn["input_tokens"] = usage["input_tokens"] + usage["cached_input_tokens"]
                    turn["output_tokens"] = usage["output_tokens"] + usage["reasoning_output_tokens"]
                    turn["cache_read"] = usage["cached_input_tokens"]
                    turn["estimated"] = False
                else:
                    pending_usage = usage
        elif payload_type == "agent_message":
            text = _extract_text(payload)
            turn = {
                "turn_index": turn_index,
                "role": "assistant",
                "input_tokens": 0,
                "output_tokens": _estimate_tokens(text),
                "cache_read": 0,
                "cache_creation": 0,
                "cache_creation_1h": 0,
                "cache_creation_5m": 0,
                "model": current_model if current_model != _UNKNOWN_MODEL else _DEFAULT_MODEL,
                "timestamp": record.get("timestamp"),
                "gap_since_prev_seconds": None,
                "tools_used": pending_tools,
                "cost_usd": 0.0,
                "estimated": True,
            }
            if pending_usage:
                turn["input_tokens"] = pending_usage["input_tokens"] + pending_usage["cached_input_tokens"]
                turn["output_tokens"] = pending_usage["output_tokens"] + pending_usage["reasoning_output_tokens"]
                turn["cache_read"] = pending_usage["cached_input_tokens"]
                turn["estimated"] = False
                pending_usage = None
            turns.append(turn)
            pending_tools = []
            turn_index += 1
    return turns


def parse_jsonl_for_quality(filepath: str | Path) -> dict[str, Any] | None:
    reads: list[tuple[int, str, str]] = []
    writes: list[tuple[int, str, str]] = []
    tool_results: list[tuple[int, str, int, bool]] = []
    system_reminders: list[tuple[int, str, int]] = []
    messages: list[tuple[int, str, int, bool]] = []
    compactions = 0
    agent_dispatches: list[tuple[int, int, int]] = []
    decisions: list[tuple[int, str]] = []
    compaction_ratios: list[dict[str, Any]] = []
    last_usage: dict[str, int] | None = None
    current_model = _UNKNOWN_MODEL
    topic = None
    idx = 0
    prev_was_compacted = False
    last_turn_context: int | None = None

    for record in _iter_json_records(filepath):
        payload = _payload(record)
        payload_type = payload.get("type")
        ts = str(record.get("timestamp") or "")

        model = _extract_model(payload)
        if model:
            current_model = model

        # Per compaction: the `compacted` rollout record (which carries
        # replacement_history) is authoritative — count it and capture the ratio.
        # Its paired `context_compacted` event_msg fires immediately after, so
        # count that ONLY when standalone (older Codex with no `compacted`
        # record), detected by the previous record not being a `compacted`. This
        # keeps the count==len(ratios) invariant even for back-to-back compactions.
        is_compacted_record = record.get("type") == "compacted"
        if is_compacted_record or payload_type == "context_compacted":
            if is_compacted_record:
                compactions += 1
                rh = payload.get("replacement_history")
                # Codex total_token_usage is cumulative (monotonic), so it does
                # not drop at compaction. The meaningful "before" is the context
                # occupancy of the last turn (per-turn input+cache); the "after"
                # is the size of the replacement history that becomes the new
                # context. ratio < 1 means compaction shrank the context; ratio is
                # None for a degenerate empty/missing replacement_history.
                after_tokens = _replacement_content_tokens(rh) if isinstance(rh, list) else None
                ratio = round(after_tokens / last_turn_context, 3) if after_tokens and last_turn_context else None
                compaction_ratios.append({
                    "before_context_tokens": last_turn_context,
                    "after_context_tokens": after_tokens,
                    "replacement_msgs": len(rh) if isinstance(rh, list) else None,
                    "ratio": ratio,
                })
            elif not prev_was_compacted:
                compactions += 1
            prev_was_compacted = is_compacted_record
            reads = []
            writes = []
            tool_results = []
            system_reminders = []
            messages = []
            agent_dispatches = []
            decisions = []
            idx += 1
            continue
        prev_was_compacted = False

        if payload_type == "token_count":
            usage = _token_usage(payload, cumulative=True)
            if usage:
                last_usage = usage
            turn = _token_usage(payload, cumulative=False)
            if turn:
                # Context occupancy sent this turn = fresh input + cached input.
                last_turn_context = turn["input_tokens"] + turn["cached_input_tokens"]

        elif payload_type == "collab_agent_spawn_end":
            prompt = str(payload.get("prompt") or "")
            agent_dispatches.append((idx, len(prompt), 0))

        elif payload_type in {"user_message", "message", "agent_message"}:
            text = _extract_text(payload)
            role = "assistant" if payload_type == "agent_message" else str(payload.get("role") or "user")
            if role == "user":
                topic = topic or _extract_topic(text)
            substantive = len(text.split()) > (20 if role == "assistant" else 10)
            messages.append((idx, role, len(text), substantive))
            if re.search(r"\b(chose|decided|because|switched|going with)\b", text, re.IGNORECASE):
                decisions.append((idx, text[:200].strip()))

        elif payload_type in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name") or "")
            args = _parse_arguments(payload)
            if name == "exec_command":
                cmd = str(args.get("cmd") or "")
                if READ_CMD_RE.search(cmd):
                    for path in _extract_shell_paths(cmd):
                        reads.append((idx, path, ts))
            elif name == "apply_patch":
                patch = str(args.get("patch") or "")
                for path in PATCH_FILE_RE.findall(patch):
                    writes.append((idx, path.strip(), ts))
            elif name == "spawn_agent":
                prompt = str(args.get("message") or args.get("prompt") or "")
                agent_dispatches.append((idx, len(prompt), 0))

        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            text = str(payload.get("output") or "")
            call_id = str(payload.get("call_id") or idx)
            tool_results.append((idx, call_id, len(text), False))
            if agent_dispatches and agent_dispatches[-1][2] == 0:
                last = agent_dispatches[-1]
                agent_dispatches[-1] = (last[0], last[1], len(text))
        elif payload_type in {"exec_command_end", "patch_apply_end"}:
            text = _event_output_text(payload)
            if text:
                call_id = str(payload.get("call_id") or idx)
                tool_results.append((idx, call_id, len(text), False))

        idx += 1

    if not messages:
        return None

    return {
        "reads": reads,
        "writes": writes,
        "tool_results": tool_results,
        "system_reminders": system_reminders,
        "messages": messages,
        "compactions": compactions,
        "agent_dispatches": agent_dispatches,
        "decisions": decisions,
        "compaction_ratios": compaction_ratios,
        "total_entries": idx,
        "estimated": True,
        "context_tokens": last_usage["total_tokens"] if last_usage else None,
        "model_context_window": last_usage["model_context_window"] if last_usage else None,
        "model": current_model if current_model != _UNKNOWN_MODEL else _DEFAULT_MODEL,
        "topic": topic,
    }


def iter_tool_outputs(
    filepath: str | Path,
    *,
    min_chars: int = 4096,
    max_outputs: int = 20,
) -> list[dict[str, Any]]:
    """Return large/high-signal Codex tool outputs from a session JSONL.

    Balanced Codex hooks do not receive every PostToolUse payload. This
    bounded transcript pass lets the Stop worker backfill the same durable
    pointers Claude gets from PostToolUse archive hooks, without keeping tool
    hooks enabled in the noisy default profile.
    """
    call_meta: dict[str, dict[str, str]] = {}
    outputs: deque[dict[str, Any]] = deque(maxlen=max_outputs)
    synthetic_index = 0

    for record in _iter_json_records(filepath):
        payload = _payload(record)
        payload_type = payload.get("type")

        if payload_type in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or payload.get("id") or synthetic_index)
            raw_name = str(payload.get("name") or "unknown")
            args = _parse_arguments(payload)
            command_or_path = ""
            if raw_name == "exec_command":
                command_or_path = str(args.get("cmd") or args.get("command") or "")
            elif raw_name == "apply_patch":
                command_or_path = "apply_patch"
            call_meta[call_id] = {
                "tool_name": _tool_name(raw_name),
                "raw_name": raw_name,
                "command_or_path": command_or_path,
            }

        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or synthetic_index)
            text = str(payload.get("output") or "")
            meta = call_meta.get(call_id, {})
            if len(text) >= min_chars or _looks_like_error_text(text):
                outputs.append({
                    "tool_use_id": call_id,
                    "tool_name": meta.get("tool_name", "Tool"),
                    "tool_type": meta.get("raw_name", "function_call"),
                    "command_or_path": meta.get("command_or_path", ""),
                    "output": text,
                    "timestamp": record.get("timestamp") or payload.get("timestamp"),
                })

        elif payload_type in {"exec_command_end", "patch_apply_end"}:
            call_id = str(payload.get("call_id") or f"event-{synthetic_index}")
            text = _event_output_text(payload)
            exit_code = payload.get("exit_code")
            status = str(payload.get("status") or "").lower()
            high_signal = exit_code not in (None, 0) or "error" in status or _looks_like_error_text(text)
            if text and (len(text) >= min_chars or high_signal):
                outputs.append({
                    "tool_use_id": call_id,
                    "tool_name": "Bash" if payload_type == "exec_command_end" else "Edit",
                    "tool_type": payload_type,
                    "command_or_path": str(payload.get("cmd") or payload.get("command") or ""),
                    "output": text,
                    "timestamp": record.get("timestamp") or payload.get("timestamp"),
                })

        synthetic_index += 1

    return list(outputs)


def extract_session_state(filepath: str | Path, tail_lines: int = 500, max_files: int = 10) -> dict[str, Any] | None:
    """Extract checkpoint-ready continuity state from a Codex JSONL session."""
    question_re = re.compile(r"\?|TODO|FIXME|HACK|XXX", re.IGNORECASE)
    active_files: list[tuple[str, str, str]] = []
    recent_reads: list[str] = []
    decisions: list[str] = []
    open_questions: list[str] = []
    agent_state: list[tuple[str, str]] = []
    error_context: list[tuple[str, str]] = []
    todos: list[tuple[str, str]] = []
    active_plan = None
    last_user_msg = ""
    last_assistant_msg = ""
    seen_files: set[str] = set()
    recent_errors: list[str] = []

    records: deque[dict[str, Any]] = deque(maxlen=tail_lines)
    for record in _iter_json_records(filepath):
        records.append(record)

    if not records:
        return None

    for record in records:
        payload = _payload(record)
        payload_type = payload.get("type")

        if payload_type in {"user_message", "message", "agent_message"}:
            role = payload.get("role")
            if payload_type == "user_message":
                role = "user"
            elif payload_type == "agent_message":
                role = "assistant"
            text = _extract_text(payload).strip()
            if not text:
                continue
            if role == "user":
                last_user_msg = text
            elif role == "assistant":
                last_assistant_msg = text
                _append_decisions(text, decisions)
                if recent_errors and _looks_like_fix(text):
                    error_context.append((recent_errors[-1][:200], text[:200]))
                    recent_errors = []
            if question_re.search(text):
                _append_question(text, open_questions)

        elif payload_type in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name") or "")
            args = _parse_arguments(payload)
            if name == "exec_command":
                cmd = str(args.get("cmd") or args.get("command") or "")
                if READ_CMD_RE.search(cmd):
                    for path in _extract_shell_paths(cmd):
                        _append_file_path(path, "read", "", active_files, recent_reads, seen_files, max_files)
                        if _is_plan_path(path):
                            active_plan = path
                for path in _extract_probable_write_paths(cmd):
                    _append_file_path(path, "modified", "", active_files, recent_reads, seen_files, max_files)
                    if _is_plan_path(path):
                        active_plan = path
            elif name == "apply_patch":
                patch = str(args.get("patch") or "")
                for path in PATCH_FILE_RE.findall(patch):
                    clean_path = path.strip()
                    _append_file_path(clean_path, "modified", "", active_files, recent_reads, seen_files, max_files)
                    if _is_plan_path(clean_path):
                        active_plan = clean_path
            elif name == "spawn_agent":
                agent_type = str(args.get("agent_type") or "default")
                desc = str(args.get("message") or args.get("prompt") or "")[:100]
                agent_state.append((agent_type, desc))
            elif name == "update_plan":
                plan = args.get("plan")
                if isinstance(plan, list):
                    todos = [
                        (str(item.get("step") or item.get("content") or "")[:120], str(item.get("status") or ""))
                        for item in plan
                        if isinstance(item, dict) and (item.get("step") or item.get("content"))
                    ]

        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            text = str(payload.get("output") or "")
            if _looks_like_error_text(text):
                recent_errors.append(text[:300].strip())
        elif payload_type in {"exec_command_end", "patch_apply_end"}:
            text = _event_output_text(payload)
            exit_code = payload.get("exit_code")
            status = str(payload.get("status") or "").lower()
            if exit_code not in (None, 0) or "error" in status or _looks_like_error_text(text):
                recent_errors.append(text[:300].strip())

    return {
        "active_files": active_files[-max_files:],
        "recent_reads": recent_reads[-max_files:],
        "decisions": decisions[-10:],
        "open_questions": open_questions[-5:],
        "agent_state": agent_state[-10:],
        "error_context": error_context[-5:],
        "todos": todos,
        "active_plan": active_plan,
        "current_step": {
            "last_user": last_user_msg[:500],
            "last_assistant": last_assistant_msg[:500],
        },
    }


def _append_file_path(
    path: str,
    action: str,
    line_range: str,
    active_files: list[tuple[str, str, str]],
    recent_reads: list[str],
    seen_files: set[str],
    max_files: int,
) -> None:
    if not path:
        return
    if path in seen_files:
        if action == "modified" and all(existing[0] != path for existing in active_files):
            if len(active_files) < max_files:
                active_files.append((path, action, line_range))
            try:
                recent_reads.remove(path)
            except ValueError:
                pass
        return
    seen_files.add(path)
    if action == "modified":
        if len(active_files) < max_files:
            active_files.append((path, action, line_range))
    else:
        recent_reads.append(path)


def _append_decisions(text: str, decisions: list[str]) -> None:
    if not re.search(r"\b(chose|decided|because|instead of|went with|going with|switched|prefer|should use|will use)\b", text, re.IGNORECASE):
        return
    for sentence in re.split(r"[.!?\n]", text):
        if re.search(r"\b(chose|decided|because|instead of|went with|going with|switched|prefer|should use|will use)\b", sentence, re.IGNORECASE):
            snippet = sentence.strip()[:200]
            if snippet and snippet not in decisions:
                decisions.append(snippet)
            return


def _append_question(text: str, open_questions: list[str]) -> None:
    for sentence in re.split(r"[.!?\n]", text):
        snippet = sentence.strip()[:200]
        if snippet and ("?" in snippet or re.search(r"\bTODO\b|\bFIXME\b", snippet, re.IGNORECASE)):
            if snippet not in open_questions:
                open_questions.append(snippet)
            return


def _extract_probable_write_paths(command: str) -> list[str]:
    if not re.search(r">\s*|tee\s+|mv\s+|cp\s+|touch\s+|mkdir\s+-p\s+", command):
        return []
    return _extract_shell_paths(command)


def _is_plan_path(path: str) -> bool:
    return "/docs/plans/" in path and path.endswith(".md")


def _looks_like_fix(text: str) -> bool:
    return bool(re.search(r"\b(fix|fixed|instead|switched|resolved|retry|rerun|passing)\b", text, re.IGNORECASE))


def _extract_shell_paths(command: str) -> list[str]:
    paths: list[str] = []
    for token in re.findall(r"(?:[./~][^\s'\";|&<>]+|[A-Za-z0-9_.-]+/[^\s'\";|&<>]+)", command):
        if any(ch in token for ch in "*?$(){}[]"):
            continue
        paths.append(token.rstrip(":,"))
    return paths[:10]


def _event_output_text(payload: dict[str, Any]) -> str:
    parts = []
    for key in ("aggregated_output", "formatted_output", "stdout", "stderr", "output"):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def _append_positive(bucket: list[float], value: Any) -> None:
    if isinstance(value, (int, float)) and value > 0:
        bucket.append(float(value))


def _append_duration(bucket: list[float], duration: Any) -> None:
    ms = _secs_nanos_to_ms(duration)
    if ms is not None and ms > 0:  # match _append_positive: ignore zero-duration noise
        bucket.append(ms)


def _secs_nanos_to_ms(value: Any) -> float | None:
    """Convert a Codex ``{secs, nanos}`` duration to milliseconds.

    Observed Codex wire format for tool durations is always the ``{secs, nanos}``
    dict. A bare number is a defensive fallback assumed to already be in
    milliseconds; if a future Codex emits bare seconds, durations would read
    1000x low (the dict form is unaffected)."""
    if isinstance(value, dict):
        secs = value.get("secs")
        nanos = value.get("nanos")
        if isinstance(secs, (int, float)) or isinstance(nanos, (int, float)):
            return (float(secs or 0) * 1000.0) + (float(nanos or 0) / 1_000_000.0)
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def _extract_rate_limits(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Capture the rate_limits block from a token_count payload, defensively.

    Scalars are copied as-is; ``primary``/``secondary`` window objects are kept
    whole so downstream consumers can read whatever fields Codex exposes without
    this parser guessing the window schema.
    """
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in rl.items():
        if value is None or isinstance(value, (str, int, float, bool, dict)):
            out[key] = value
    return out or None


def _replacement_content_tokens(replacement_history: list[Any]) -> int:
    """Approximate the token size of a compaction's replacement_history messages."""
    total_chars = 0
    for item in replacement_history:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        total_chars += len(text)
                elif isinstance(part, str):
                    total_chars += len(part)
    return total_chars // CHARS_PER_TOKEN
