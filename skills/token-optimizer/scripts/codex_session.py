#!/usr/bin/env python3
"""Codex session JSONL adapter for Token Optimizer.

Codex stores session logs in a different JSONL shape than Claude Code. This
module normalizes the parts Token Optimizer needs so the mature quality,
trends, and dashboard pipeline can stay shared.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime_env import codex_home

CHARS_PER_TOKEN = 4
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


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
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
    chars = text if isinstance(text, int) else len(text)
    return max(0, int(chars / CHARS_PER_TOKEN))


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _extract_topic(text: str) -> str | None:
    text = " ".join(text.split())
    if not text:
        return None
    return text[:117] + "..." if len(text) > 120 else text


def _safe_session_id(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-zA-Z0-9_-]", "", value)


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


def find_all_jsonl_files(days: int = 30) -> list[tuple[Path, float, str]]:
    cutoff = datetime.now().timestamp() - (days * 86400)
    results: list[tuple[Path, float, str]] = []
    for root in session_roots():
        if not root.exists():
            continue
        for jf in root.rglob("*.jsonl"):
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
    files = find_all_jsonl_files(days=3650)
    return files[0][0] if files else None


def find_session_jsonl_by_id(session_id: str) -> Path | None:
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        return None
    exact_matches: list[Path] = []
    for root in session_roots():
        if not root.exists():
            continue
        for jf in root.rglob(f"*{safe_id}*.jsonl"):
            meta_id = _session_meta_id(jf)
            if jf.stem == safe_id or safe_id in jf.stem or meta_id == safe_id or (meta_id and meta_id.startswith(safe_id)):
                exact_matches.append(jf)
    if not exact_matches:
        return None
    exact_matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return exact_matches[0]


def _session_meta_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                value = _payload(record).get("id")
                return _safe_session_id(str(value)) if value else None
    except OSError:
        return None
    return None


def _project_name_from_file(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                cwd = _payload(record).get("cwd")
                if cwd:
                    return Path(str(cwd)).name or str(cwd)
                break
    except OSError:
        pass
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
    current_model = "unknown"
    per_model_usage: dict[str, dict[str, int]] = {}

    try:
        with Path(filepath).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = _payload(record)
                payload_type = payload.get("type")

                ts = _parse_ts(record.get("timestamp") or payload.get("timestamp"))
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts

                if record.get("type") == "session_meta":
                    version = version or payload.get("cli_version")
                    slug = slug or payload.get("id")

                model = _extract_model(payload)
                if model:
                    current_model = model

                if payload_type == "token_count":
                    usage = _token_usage(payload, cumulative=True)
                    if usage:
                        last_usage = usage
                    turn_usage = _token_usage(payload, cumulative=False)
                    if turn_usage:
                        model_key = current_model if current_model != "unknown" else "codex"
                        bucket = per_model_usage.setdefault(
                            model_key,
                            {"fresh_input": 0, "cache_read": 0, "cache_create": 0, "output": 0},
                        )
                        bucket["fresh_input"] += turn_usage["input_tokens"]
                        bucket["cache_read"] += turn_usage["cached_input_tokens"]
                        bucket["output"] += turn_usage["output_tokens"] + turn_usage["reasoning_output_tokens"]

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

    except (PermissionError, OSError):
        return None

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
        model = current_model if current_model != "unknown" else "codex"
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
    }


def parse_session_turns(filepath: str | Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_tools: list[str] = []
    pending_usage: dict[str, int] | None = None
    current_model = "unknown"
    turn_index = 0
    try:
        with Path(filepath).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
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
                        "model": current_model if current_model != "unknown" else "codex",
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
    except (PermissionError, OSError):
        pass
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
    last_usage: dict[str, int] | None = None
    idx = 0

    try:
        with Path(filepath).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = _payload(record)
                payload_type = payload.get("type")
                ts = str(record.get("timestamp") or "")

                if record.get("type") == "compacted" or payload_type == "context_compacted":
                    compactions += 1
                    reads = []
                    writes = []
                    tool_results = []
                    system_reminders = []
                    messages = []
                    agent_dispatches = []
                    decisions = []
                    idx += 1
                    continue

                if payload_type == "token_count":
                    usage = _token_usage(payload, cumulative=True)
                    if usage:
                        last_usage = usage

                elif payload_type in {"user_message", "message", "agent_message"}:
                    text = _extract_text(payload)
                    role = "assistant" if payload_type == "agent_message" else str(payload.get("role") or "user")
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
    except (PermissionError, OSError):
        return None

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
        "total_entries": idx,
        "estimated": True,
        "context_tokens": last_usage["total_tokens"] if last_usage else None,
        "model_context_window": last_usage["model_context_window"] if last_usage else None,
    }


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

    records: list[dict[str, Any]] = []
    try:
        with Path(filepath).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(records) > tail_lines:
                    records.pop(0)
    except (PermissionError, OSError):
        return None

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
            if _looks_like_error(text):
                recent_errors.append(text[:300].strip())
        elif payload_type in {"exec_command_end", "patch_apply_end"}:
            text = _event_output_text(payload)
            exit_code = payload.get("exit_code")
            status = str(payload.get("status") or "").lower()
            if exit_code not in (None, 0) or "error" in status or _looks_like_error(text):
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


def _looks_like_error(text: str) -> bool:
    return bool(re.search(r"\b(error|failed|traceback|exception|permission denied|not found)\b", text, re.IGNORECASE))


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
