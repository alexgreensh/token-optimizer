#!/usr/bin/env python3
"""Read-only reader for Grok Build session data.

Grok Build persists every session under ``$GROK_HOME/sessions/<encoded-cwd>/<uuid>/``
(17-sessions.md). The files this adapter reads, all optional / missing-tolerant:

  summary.json   — ``Summary``: ``info.id``, ``info.cwd``, the session title,
                   created/updated timestamps, message counts, current model id
                   and agent name. Upstream currently writes it with plain
                   serde_json (snake_case field names); older builds used
                   camelCase, so both spellings are accepted on read.
                   Source: crates/codegen/xai-grok-shell/src/session/persistence.rs.
  signals.json   — ``SessionSignals`` (camelCase): ``turnCount``, ``toolCallCount``,
                   ``toolsUsed``, ``modelsUsed``, ``primaryModelId``,
                   ``contextTokensUsed``, ``contextWindowTokens``,
                   ``contextWindowUsage``, ``compactionCount``,
                   ``sessionDurationSeconds``.
                   Source: crates/codegen/xai-grok-shell/src/session/signals.rs.
  updates.jsonl  — newline-delimited ``SessionNotification`` (camelCase) lines;
                   each line's ``update`` is a ``SessionUpdate`` tagged by
                   ``sessionUpdate`` (snake_case variant name). The
                   ``turn_completed`` variant carries ``usage`` (``PromptUsage``:
                   flattened camelCase ``inputTokens``/``outputTokens``/
                   ``cachedReadTokens``/``cacheCreationTokens``/``reasoningTokens``/
                   ``modelCalls``/``costUsdTicks``/``costIsPartial`` plus
                   ``modelUsage`` + ``numTurns`` + ``usageIsIncomplete``) — the
                   authoritative per-turn token totals.
                   Source: crates/codegen/xai-grok-shell/src/extensions/notification.rs
                   (``SessionNotification``, ``SessionUpdate``, ``PromptUsage``,
                   ``PromptUsageModel``) and src/session/turn_completion.rs.

Design constraints (mirroring copilot_state.py / cursor_state.py):

- **Pure stdlib only.** No Grok imports.
- **Strictly read-only.** Never writes any file.
- **No hardcoded user paths.** The Grok home comes from ``runtime_env.grok_home()``;
  callers may pass an explicit ``home`` override.
- **Schema-defensive.** Every reader degrades to a safe default and never raises.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SESSIONS_DIR = "sessions"
_SUMMARY_FILE = "summary.json"
_SIGNALS_FILE = "signals.json"
_UPDATES_FILE = "updates.jsonl"

# Bounded reads: a maliciously-crafted or pathological file must never stall a
# detached rollup (mirrors cursor_state.py / copilot_state.py caps).
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024
_MAX_EVENTS_PER_FILE = 500_000

# updates.jsonl SessionUpdate tag value for the authoritative per-turn usage.
_TURN_COMPLETED_TAG = "turn_completed"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _read_json_file(path: Path) -> Optional[dict]:
    """Read a bounded JSON file, returning the dict or None when unreadable."""
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("[grok_state] cannot read %s: %s", path, exc)
        return None


def find_session_dirs(home: Optional[Path] = None) -> list:
    """Return session directories under ``<home>/sessions/<group>/<uuid>/``.

    The store is two levels deep: the group dir is the URL-encoded cwd (or a
    slug+hash with a ``.cwd`` file when the encoded name exceeds 255 bytes), and
    each group holds one directory per session UUID. Returns [] when the
    sessions dir does not exist. Never raises.
    """
    if home is None:
        try:
            from runtime_env import grok_home

            home = grok_home()
        except Exception as exc:
            logger.debug("[grok_state] cannot import runtime_env: %s", exc)
            return []
    sessions_dir = home / _SESSIONS_DIR
    if not sessions_dir.is_dir():
        return []
    try:
        result = []
        for group in sessions_dir.iterdir():
            if not group.is_dir():
                continue
            for session_dir in group.iterdir():
                if session_dir.is_dir():
                    result.append(session_dir)
        return sorted(result, key=lambda p: str(p))
    except OSError as exc:
        logger.debug("[grok_state] cannot list %s: %s", sessions_dir, exc)
        return []


def read_summary(session_dir: Path) -> Optional[dict]:
    """Read ``summary.json``. None when absent/unreadable/invalid."""
    return _read_json_file(session_dir / _SUMMARY_FILE)


def read_signals(session_dir: Path) -> Optional[dict]:
    """Read ``signals.json``. None when absent/unreadable/invalid."""
    return _read_json_file(session_dir / _SIGNALS_FILE)


def _stream_updates(updates_path: Path):
    """Yield parsed dicts from updates.jsonl, one per valid line.

    Skips malformed JSON, oversized lines, lines missing an ``update`` object,
    and lines beyond ``_MAX_EVENTS_PER_FILE``. Never raises.
    """
    try:
        count = 0
        with updates_path.open("rb") as fh:
            for raw_line in fh:
                if count >= _MAX_EVENTS_PER_FILE:
                    logger.debug(
                        "[grok_state] %s: hit %d-event cap, stopping",
                        updates_path,
                        _MAX_EVENTS_PER_FILE,
                    )
                    break
                if len(raw_line) > _MAX_LINE_BYTES:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                count += 1
                yield obj
    except OSError as exc:
        logger.debug("[grok_state] cannot read %s: %s", updates_path, exc)


def read_usage_totals(session_dir: Path) -> dict:
    """Sum the authoritative per-turn usage across updates.jsonl.

    Only ``turn_completed`` records contribute. Returns a dict with:
      input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
      reasoning_tokens, model_calls, turns, cost_usd_ticks (summed only from
      records whose usage is NOT incomplete and NOT cost-partial — the scrub
      rule in notification.rs), usage_incomplete (True when any contributing
      record was incomplete), model_usage ({model: {input, output, cache_read,
      cache_create, model_calls}}).

    Missing/unreadable updates.jsonl yields a zeroed dict. Never raises.
    """
    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "reasoning_tokens": 0,
        "model_calls": 0,
        "turns": 0,
        "cost_usd_ticks": 0,
        "usage_incomplete": False,
        "model_usage": {},
    }
    updates_path = session_dir / _UPDATES_FILE

    for line in _stream_updates(updates_path):
        update = line.get("update")
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != _TURN_COMPLETED_TAG:
            continue
        usage = update.get("usage")
        if not isinstance(usage, dict):
            continue

        out["turns"] += 1
        out["input_tokens"] += _safe_int(usage.get("inputTokens"))
        out["output_tokens"] += _safe_int(usage.get("outputTokens"))
        out["cache_read_tokens"] += _safe_int(usage.get("cachedReadTokens"))
        out["cache_create_tokens"] += _safe_int(usage.get("cacheCreationTokens"))
        out["reasoning_tokens"] += _safe_int(usage.get("reasoningTokens"))
        out["model_calls"] += _safe_int(usage.get("modelCalls"))

        incomplete = bool(usage.get("usageIsIncomplete"))
        partial = bool(usage.get("costIsPartial"))
        if incomplete:
            out["usage_incomplete"] = True
        # Scrub rule (notification.rs): cost is trustworthy only when NOT
        # incomplete and NOT partial. Absence of cost means unknown, not free.
        if not incomplete and not partial:
            out["cost_usd_ticks"] += _safe_int(usage.get("costUsdTicks"))

        model_usage = usage.get("modelUsage")
        if isinstance(model_usage, dict):
            for model, row in model_usage.items():
                if not isinstance(row, dict):
                    continue
                model_name = str(model) if model is not None and str(model) != "" else "unknown"
                slot = out["model_usage"].setdefault(
                    model_name,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "cache_create_tokens": 0,
                        "model_calls": 0,
                    },
                )
                slot["input_tokens"] += _safe_int(row.get("inputTokens"))
                slot["output_tokens"] += _safe_int(row.get("outputTokens"))
                slot["cache_read_tokens"] += _safe_int(row.get("cacheReadInputTokens"))
                slot["cache_create_tokens"] += _safe_int(row.get("cacheCreationInputTokens"))
                slot["model_calls"] += _safe_int(row.get("modelCalls"))
    return out


def read_session(session_dir: Path) -> dict:
    """Parse one Grok session directory into a raw session dict.

    Combines summary.json (identity/cwd/title/timestamps), signals.json
    (tool/turn/model/context counters), and updates.jsonl (authoritative
    per-turn token totals). Returns a dict with keys:
      session_id, cwd, title, created_at, updated_at, num_messages,
      num_chat_messages, model_id, agent_name, signals, usage,
      data_source ("grok_session_store").
    """
    session_id = session_dir.name
    summary = read_summary(session_dir) or {}
    signals = read_signals(session_dir) or {}
    usage = read_usage_totals(session_dir)

    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    cwd = info.get("cwd") if isinstance(info.get("cwd"), str) else None

    def field(camel, snake):
        # Upstream serializes Summary with plain serde_json today (snake_case
        # names) and earlier builds used camelCase; accept either so a rename
        # on their side never silently zeroes the session record.
        value = summary.get(camel)
        return value if value is not None else summary.get(snake)

    return {
        "session_id": session_id,
        "cwd": cwd,
        "title": field("sessionSummary", "session_summary"),
        "created_at": field("createdAt", "created_at"),
        "updated_at": field("updatedAt", "updated_at"),
        "num_messages": _safe_int(field("numMessages", "num_messages")),
        "num_chat_messages": _safe_int(field("numChatMessages", "num_chat_messages")),
        "model_id": field("currentModelId", "current_model_id"),
        "agent_name": field("agentName", "agent_name"),
        "signals": signals,
        "usage": usage,
        "data_source": "grok_session_store",
    }


def read_all_sessions(home: Optional[Path] = None) -> list:
    """Parse all Grok sessions, deduped by session_id (first wins)."""
    seen: set = set()
    results: list = []
    for sd in find_session_dirs(home=home):
        sid = sd.name
        if sid in seen:
            continue
        seen.add(sid)
        try:
            results.append(read_session(sd))
        except Exception as exc:
            logger.debug("[grok_state] error reading session %s: %s", sid, exc)
    return results


if __name__ == "__main__":  # pragma: no cover - debug helper
    import json as _json

    print(_json.dumps(read_all_sessions(), indent=2, default=str))
