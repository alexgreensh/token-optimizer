"""Price transcript-observed setup reductions, without changing setup settings.

Callers provide an explicitly dated change, a pre-change transcript and a
post-change transcript. Missing observations are uncredited. Connector removals
require named removal events, not an inference from an incomplete delta.
"""
from datetime import datetime, timezone
import json
import os

from .structural_value import _price

_FIELDS = {"deferred_tools_delta": "addedLines", "mcp_instructions_delta": "addedBlocks"}


def _epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _update(state, attachment, component):
    if attachment.get("type") != component:
        return False
    if component == "skill_listing":
        text = attachment.get("content")
        if not isinstance(text, str):
            return False
        state["listing"] = text
        return True
    names = attachment.get("addedNames", [])
    contents = attachment.get(_FIELDS[component], [])
    removed = attachment.get("removedNames", [])
    if not isinstance(names, list) or not isinstance(contents, list) or len(names) != len(contents) or not isinstance(removed, list):
        return False
    for name, content in zip(names, contents):
        if isinstance(name, str) and isinstance(content, str):
            state[name] = content
    for name in removed:
        if isinstance(name, str):
            state.pop(name, None)
            state.setdefault("_removed", set()).add(name)
    for name in names:
        if isinstance(name, str):
            state.setdefault("_removed", set()).discard(name)
    return True


def measure(before_rows, after_rows, change_ts, component, estimate, card_fn, targets=(), seen_requests=None,
            window_start=None, window_end=None, confirmed_disconnect=False):
    """Return observed token reductions priced per deduplicated billed request.

    The caller is responsible for identifying the actual user change and for
    preventing overlap with other setup-trim credits. `estimate` is the same
    calibrated tokenizer used for both transcript observations.
    """
    result = {"calls": 0, "read_tokens": 0, "write_tokens": 0, "usd": 0.0,
              "read_usd": 0.0, "write_1h_usd": 0.0, "write_5m_usd": 0.0, "unpriced_calls": 0}
    change = _epoch(change_ts)
    start = _epoch(window_start) if window_start is not None else None
    end = _epoch(window_end) if window_end is not None else None
    if (change is None or component not in ("skill_listing", *_FIELDS)
            or (window_start is not None and start is None) or (window_end is not None and end is None)):
        return result
    targets = set(targets)
    if component != "skill_listing" and not targets:
        return result
    baseline = {}
    for row in before_rows:
        ts = _epoch(row.get("timestamp"))
        if ts is not None and ts < change and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            baseline.clear()
        elif ts is not None and ts < change and row.get("type") == "attachment":
            attachment = row.get("attachment") or {}
            if isinstance(attachment, dict) and attachment.get("type") == component and not _update(baseline, attachment, component):
                baseline.clear()
    if component == "skill_listing" and "listing" not in baseline:
        return result
    if component != "skill_listing" and not any(k in baseline for k in targets):
        return result
    state, observed, seen_assistant, seen_component = dict(baseline), False, False, False
    seen = seen_requests if seen_requests is not None else set()
    state.pop("_removed", None)
    for row in after_rows:
        ts = _epoch(row.get("timestamp"))
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            observed = False
            seen_assistant, seen_component = False, False
            continue
        if ts is None or ts < change:
            if row.get("type") == "assistant":
                seen_assistant = True
            attachment = row.get("attachment") or {}
            if row.get("type") == "attachment" and isinstance(attachment, dict) and attachment.get("type") == component:
                seen_component = True
            continue
        if row.get("type") == "attachment":
            attachment = row.get("attachment") or {}
            if isinstance(attachment, dict) and attachment.get("type") == component:
                initial = confirmed_disconnect and component != "skill_listing" and not seen_assistant and not seen_component
                seen_component = True
                observed = _update(state, attachment, component)
                if observed and initial:
                    absent = targets.difference(attachment.get("addedNames") or ())
                    for name in absent:
                        state.pop(name, None)
                        state.setdefault("_removed", set()).add(name)
            continue
        if row.get("type") == "assistant":
            seen_assistant = True
        if row.get("type") != "assistant" or not observed or (start is not None and ts < start) or (end is not None and ts >= end):
            continue
        message, request_id = row.get("message") or {}, row.get("requestId")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage") or {}
        if not request_id or request_id in seen or not isinstance(usage, dict) or not usage:
            continue
        seen.add(request_id)
        if component == "skill_listing":
            delta = max(0, int(estimate(baseline["listing"])) - int(estimate(state["listing"])))
        else:
            missing = targets.intersection(state.get("_removed", set()))
            delta = sum(int(estimate(baseline[name])) for name in missing if name in baseline)
        if delta <= 0:
            continue
        try:
            cr = int(usage.get("cache_read_input_tokens") or 0)
            cc = int(usage.get("cache_creation_input_tokens") or 0)
            creation = usage.get("cache_creation") or {}
            if not isinstance(creation, dict):
                raise ValueError("invalid cache creation")
            one_hour = int(creation.get("ephemeral_1h_input_tokens") or 0)
            five_min = int(creation.get("ephemeral_5m_input_tokens") or 0)
            prompt = cr + cc + int(usage.get("input_tokens") or 0)
            if min(cr, cc, one_hour, five_min, prompt) < 0:
                raise ValueError("negative usage")
            card = card_fn(message.get("model"), prompt)
        except (TypeError, ValueError, OverflowError):
            result["unpriced_calls"] += 1
            continue
        if card is None:
            result["unpriced_calls"] += 1
            continue
        read, write, ur, u1, u5 = _price(card, delta, cr, cc, one_hour, five_min)
        if read or write:
            result["calls"] += 1
            result["read_tokens"] += read
            result["write_tokens"] += write
            result["usd"] += ur + u1 + u5
            result["read_usd"] += ur
            result["write_1h_usd"] += u1
            result["write_5m_usd"] += u5
    return result


def measure_sessions(before_rows, sessions, change_ts, component, estimate, card_fn, targets=(), confirmed_disconnect=False):
    """Sum per-session results, conservatively deduplicating request IDs."""
    baseline = list(before_rows)
    totals = {"calls": 0, "read_tokens": 0, "write_tokens": 0, "usd": 0.0,
              "read_usd": 0.0, "write_1h_usd": 0.0, "write_5m_usd": 0.0, "unpriced_calls": 0}
    seen = set()
    for session in sessions:
        sid = session.get("session_id")
        if not sid:
            continue
        part = measure(baseline, session.get("rows") or [], change_ts, component,
                       estimate, card_fn, targets=targets, seen_requests=seen, confirmed_disconnect=confirmed_disconnect)
        for key in totals:
            totals[key] += part[key]
    return totals


def read_transcript(path):
    """Yield parseable JSONL records without exposing transcript contents."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def manifest_sessions(entries):
    """Never count the same transcript twice under different session labels."""
    seen = set()
    for entry in entries:
        path = os.path.realpath(entry["path"])
        if path in seen:
            continue
        seen.add(path)
        yield {"session_id": entry["session_id"], "rows": read_transcript(path)}
