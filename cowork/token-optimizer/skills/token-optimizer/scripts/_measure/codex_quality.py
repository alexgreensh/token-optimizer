"""Codex quality parsing with a validated compact-boundary checkpoint."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
from . import quality_checkpoint
from codex_session import (PATCH_FILE_RE, READ_CMD_RE, _DEFAULT_MODEL, _UNKNOWN_MODEL,
    _event_output_text, _extract_model, _extract_shell_paths, _extract_text,
    _extract_topic, _parse_arguments, _payload, _pick_topic,
    _replacement_content_tokens, _task_user_text, _token_usage)

def parse_jsonl_for_quality(filepath: str | Path, checkpoint=None) -> dict[str, Any] | None:
    reads: list[tuple[int, str, str]] = []
    writes: list[tuple[int, str, str]] = []
    tool_results: list[tuple[int, str, int, bool]] = []
    system_reminders: list[tuple[int, str, int]] = []
    messages: list[tuple[int, str, int, bool]] = []
    compactions = 0
    agent_dispatches: list[tuple[int, int, int]] = []
    decisions: list[tuple[int, str]] = []
    compaction_ratios: list[dict[str, Any]] = []
    compaction_events: list[dict[str, Any]] = []
    last_usage: dict[str, int] | None = None
    current_model = _UNKNOWN_MODEL
    topics: dict[str, str | None] = {}
    idx = 0
    prev_was_compacted = False
    last_turn_context: int | None = None
    safe_pause = False

    saved_checkpoint = None
    parsed_offset = 0
    window_offset = 0
    try:
        handle = open(filepath, 'rb')
    except OSError:
        return quality_checkpoint.ParseFailure('unreadable')
    with handle:
        restored = quality_checkpoint.restore_codex(handle, checkpoint) if checkpoint else None
        if restored is not None:
            compactions = restored['compactions']
            compaction_ratios = restored['compaction_ratios']
            compaction_events = restored['compaction_events']
            last_usage = restored['last_usage']
            current_model = restored['current_model']
            topics = restored['topics']
            idx = restored['idx']
            prev_was_compacted = restored['prev_was_compacted']
            last_turn_context = restored['last_turn_context']
            parsed_offset = checkpoint['offset']
            window_offset = parsed_offset
        for line_offset, line in quality_checkpoint.lines(handle, 16 * 1024 * 1024):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            payload = _payload(record)
            payload_type = payload.get("type")
            ts = str(record.get("timestamp") or "")
            if payload_type in {"task_complete", "turn_complete"}:
                safe_pause = True
            elif payload_type in {"task_started", "turn_started", "function_call", "custom_tool_call",
                                  "function_call_output", "custom_tool_call_output", "exec_command_end",
                                  "patch_apply_end", "agent_message"}:
                safe_pause = False

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
                window_offset = line_offset
                saved_checkpoint = quality_checkpoint.save(handle, line_offset, {
                    'compactions': compactions, 'compaction_ratios': compaction_ratios,
                    'compaction_events': compaction_events, 'last_usage': last_usage,
                    'current_model': current_model, 'topics': topics, 'idx': idx,
                    'prev_was_compacted': prev_was_compacted, 'last_turn_context': last_turn_context,
                }, kind='codex')
                safe_pause = False
                if is_compacted_record:
                    compactions += 1
                    # Minimal recovery event for the quality panel: Codex has no
                    # matched pre/post tool-call windows, so window_data=False
                    # keeps every boundary honestly "unmeasurable" rather than
                    # implying healthy recovery.
                    compaction_events.append({
                        "boundary_id": f"codex-{compactions}",
                        "ordinal": compactions,
                        "line": idx,
                        "ts": ts,
                        "trigger": "auto",
                        "pre_tokens": last_turn_context,
                        "post_tokens": None,
                        "host_version": None,
                        "model": current_model if current_model != _UNKNOWN_MODEL else None,
                        "pre_seg_calls": 0,
                        "post_seg_calls": 0,
                        "matched_calls": 0,
                        "eligible": False,
                        "window_data": False,
                        "restore_transcript": False,
                        "capture_notice": False,
                    })
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
                    last_turn_context = after_tokens
                elif not prev_was_compacted:
                    compactions += 1
                    compaction_events.append({
                        "boundary_id": f"codex-{compactions}",
                        "ordinal": compactions,
                        "line": idx,
                        "ts": ts,
                        "trigger": "auto",
                        "pre_tokens": last_turn_context,
                        "post_tokens": None,
                        "host_version": None,
                        "model": current_model if current_model != _UNKNOWN_MODEL else None,
                        "pre_seg_calls": 0,
                        "post_seg_calls": 0,
                        "matched_calls": 0,
                        "eligible": False,
                        "window_data": False,
                        "restore_transcript": False,
                        "capture_notice": False,
                    })
                    last_turn_context = None
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
                    # input already includes cached input; cumulative session
                    # usage is not the occupancy of this response's context.
                    last_turn_context = turn["input_tokens"] + turn["output_tokens"]

            elif payload_type == "collab_agent_spawn_end":
                prompt = str(payload.get("prompt") or "")
                agent_dispatches.append((idx, len(prompt), 0))

            elif payload_type in {"user_message", "message", "agent_message"}:
                text = _extract_text(payload)
                role = "assistant" if payload_type == "agent_message" else str(payload.get("role") or "user")
                safe_pause = role == "user" and bool(_task_user_text(text))
                if role == "user" and not topics.get(payload_type):
                    task = _task_user_text(text)
                    if task:
                        topics[payload_type] = _extract_topic(task)
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

        import codex_session
        if handle.tell() - window_offset > codex_session.MAX_PARSE_FILE_BYTES:
            return quality_checkpoint.ParseFailure('too_large')

    return {
        "parse_reason": None if messages else "no_messages",
        "_quality_checkpoint": saved_checkpoint,
        "_parsed_from_offset": parsed_offset,
        "reads": reads,
        "writes": writes,
        "tool_results": tool_results,
        "system_reminders": system_reminders,
        "messages": messages,
        "safe_pause": safe_pause,
        "compactions": compactions,
        "agent_dispatches": agent_dispatches,
        "decisions": decisions,
        "compaction_ratios": compaction_ratios,
        "compaction_events": compaction_events,
        "total_entries": idx,
        "estimated": True,
        "context_tokens": last_turn_context or None,
        "model_context_window": last_usage["model_context_window"] if last_usage else None,
        "model": current_model if current_model != _UNKNOWN_MODEL else _DEFAULT_MODEL,
        "topic": _pick_topic(topics),
    }
