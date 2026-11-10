"""Append confirmed connector changes and value observed transcript deltas.

Only an explicitly confirmed disconnect can register an action. Targets cannot
be registered twice until a close/reconnect protocol exists.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .setup_attachment_counter import _epoch, _update, measure, read_transcript

_COMPONENTS = frozenset(("deferred_tools_delta", "mcp_instructions_delta"))
_SCHEMA = """CREATE TABLE IF NOT EXISTS setup_connector_actions (
 action_id TEXT PRIMARY KEY, project TEXT NOT NULL, component TEXT NOT NULL,
 changed_at TEXT NOT NULL, before_transcript TEXT NOT NULL, targets_json TEXT NOT NULL,
 origin TEXT NOT NULL
)"""


def register(conn, project, component, changed_at, before_transcript, targets, confirmed):
    """Append one owner-confirmed action; return True only after reading it back."""
    if confirmed is not True or component not in _COMPONENTS or not isinstance(project, str) or not project:
        return False
    if not isinstance(before_transcript, str) or not os.path.isfile(before_transcript) or _epoch(changed_at) is None:
        return False
    if not isinstance(targets, (list, tuple)) or not targets or len(targets) > 200:
        return False
    if any(not isinstance(t, str) or not t or len(t) > 512 for t in targets):
        return False
    targets = sorted(set(targets))
    baseline = {}
    try:
        for row in read_transcript(before_transcript):
            stamp = _epoch(row.get("timestamp"))
            if stamp is not None and stamp < _epoch(changed_at) and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                baseline.clear()
            elif stamp is not None and stamp < _epoch(changed_at) and row.get("type") == "attachment":
                item = row.get("attachment") or {}
                if isinstance(item, dict) and item.get("type") == component and not _update(baseline, item, component):
                    baseline.clear()
    except OSError:
        return False
    if any(not isinstance(baseline.get(t), str) or not baseline[t] for t in targets):
        return False
    identity = json.dumps([project, component, changed_at, before_transcript, targets], separators=(",", ":"))
    action_id = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()
    if conn.in_transaction:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_SCHEMA)
        existing = conn.execute("SELECT targets_json FROM setup_connector_actions WHERE project=? AND component=?", (project, component)).fetchall()
        for (raw,) in existing:
            try:
                prior = json.loads(raw)
                if not isinstance(prior, list) or set(prior).intersection(targets):
                    conn.rollback()
                    return False
            except (ValueError, TypeError):
                conn.rollback()
                return False
        cursor = conn.execute("INSERT OR IGNORE INTO setup_connector_actions VALUES(?,?,?,?,?,?,?)",
                              (action_id, project, component, changed_at, before_transcript,
                               json.dumps(targets, separators=(",", ":")), "owner_confirmed"))
        changed = cursor.rowcount == 1
        conn.commit()
        return changed and conn.execute("SELECT 1 FROM setup_connector_actions WHERE action_id=?", (action_id,)).fetchone() is not None
    except sqlite3.Error:
        conn.rollback()
        return False


def measured_values(conn, trims, estimate, card_fn, window_start, window_end):
    """Value explicitly confirmed actions; suppress possible MCP-trim overlaps."""
    zero = {"calls": 0, "tokens_saved": 0, "usd": 0.0, "read_usd": 0.0,
            "write_1h_usd": 0.0, "write_5m_usd": 0.0, "overlap_suppressed": False,
            "missing_transcripts": 0, "actions": []}
    start, end = _epoch(window_start), _epoch(window_end)
    if start is None or end is None or end <= start:
        return zero
    for trim in trims:
        if (trim.get("component") == "mcp_tools" and trim.get("tier") in ("measured", "estimated")
                and int(trim.get("tokens") or 0) > 0 and (trim.get("ts_epoch") or 0) < end
                and (trim.get("closed_epoch") is None or trim["closed_epoch"] > start)):
            return {**zero, "overlap_suppressed": True}
    try:
        actions = conn.execute("SELECT action_id,project,component,changed_at,before_transcript,targets_json "
                               "FROM setup_connector_actions WHERE origin='owner_confirmed'").fetchall()
    except sqlite3.Error:
        return zero
    total = dict(zero)
    total["actions"] = []
    for action_id, project, component, changed_at, before_path, raw_targets in actions:
        try:
            targets = json.loads(raw_targets)
            baseline = list(read_transcript(before_path))
            first = (datetime.fromtimestamp(_epoch(changed_at), timezone.utc) - timedelta(days=1)).date().isoformat()
            last = (datetime.fromtimestamp(end, timezone.utc) + timedelta(days=1)).date().isoformat()
            sessions = conn.execute("SELECT DISTINCT session_uuid,jsonl_path FROM session_log WHERE project=? "
                                    "AND COALESCE(last_activity_day,date)>=? AND COALESCE(first_activity_day,date)<=?",
                                    (project, first, last)).fetchall()
        except (OSError, ValueError, sqlite3.Error):
            total["missing_transcripts"] += 1
            continue
        seen, subtotal = set(), {"calls": 0, "read_tokens": 0, "write_tokens": 0, "usd": 0.0,
                                 "read_usd": 0.0, "write_1h_usd": 0.0, "write_5m_usd": 0.0}
        for sid, path in sessions:
            if not sid or not path:
                continue
            try:
                part = measure(baseline, read_transcript(path), changed_at, component, estimate, card_fn,
                               targets=targets, seen_requests=seen, window_start=window_start,
                               window_end=window_end, confirmed_disconnect=True)
            except OSError:
                total["missing_transcripts"] += 1
                continue
            for key in subtotal:
                subtotal[key] += part[key]
        total["calls"] += subtotal["calls"]
        total["tokens_saved"] += subtotal["read_tokens"] + subtotal["write_tokens"]
        for key in ("usd", "read_usd", "write_1h_usd", "write_5m_usd"):
            total[key] += subtotal[key]
        total["actions"].append({"id": action_id, "component": component, "changed_at": changed_at, **subtotal})
    return total
