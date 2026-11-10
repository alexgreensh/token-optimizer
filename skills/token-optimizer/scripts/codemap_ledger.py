#!/usr/bin/env python3
"""Measurement ledger for code map lookups and re-read outlines.

Every lookup is logged with the files its answer covers and the tokens a full
Read of each would have cost. A Read of a covered file inside the follow-up
window means the lookup did NOT replace that read. When the window closes the
lookup is resolved: covered files that were never read count as avoided reads,
minus the tokens the answers themselves cost. The net is booked to
``savings_events`` as ``codemap_lookup``, an ESTIMATED-tier event type (the
avoided read is a counterfactual; the dashboard keeps it out of the measured
total).

Re-read outlines are different: the full re-read was actually denied and the
outline served instead, so ``codemap_outline`` is a measured removal, like
``structure_map``.

Rows carry the session id so a later matched comparison (sessions with vs
without lookups, same project and task mix) can be run from ``codemap_events``.
Everything here is best-effort and silent: a ledger failure never affects the
agent's tools.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

FOLLOWUP_WINDOW_S = 600.0
READ_TOKENS_CAP = 10_000  # credit per avoided file; long files are usually read in ranges
PENDING_MAX_PATHS = 400
_PENDING_NAME = "pending.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS codemap_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    command TEXT,
    root_hash TEXT,
    answer_tokens INTEGER DEFAULT 0,
    covered TEXT,
    status TEXT,
    reads_after INTEGER DEFAULT 0,
    avoided_tokens INTEGER DEFAULT 0,
    resolved_ts REAL
);
CREATE TABLE IF NOT EXISTS codemap_reads (
    event_id INTEGER, path TEXT, ts_epoch REAL
);
CREATE INDEX IF NOT EXISTS codemap_events_status ON codemap_events(status, ts_epoch);
"""


def _snapshot_dir() -> Path:
    from plugin_env import resolve_snapshot_dir
    return resolve_snapshot_dir()


def _trends_db() -> Path:
    return _snapshot_dir() / "trends.db"


def _pending_path() -> Path:
    return _snapshot_dir() / "codemap" / _PENDING_NAME


def session_id_from_env() -> str:
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
        val = os.environ.get(var, "").strip()
        if val:
            return val[:80]
    return "unknown"


def _connect() -> sqlite3.Connection:
    db = _trends_db()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=3.0)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(_SCHEMA)
    return conn


def _read_pending() -> Dict:
    try:
        raw = _pending_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_pending(data: Dict) -> None:
    p = _pending_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def log_lookup(
    command: str,
    answer_tokens: int,
    covered: Sequence[Tuple[str, int]],
    *,
    root_hash: str = "",
    session_id: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[int]:
    """Record one lookup. ``covered`` is [(abs_path, full_read_tokens)]."""
    now = time.time() if now is None else now
    sid = session_id or session_id_from_env()
    cov = [(str(p), int(min(max(0, t), READ_TOKENS_CAP))) for p, t in covered][:5]
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO codemap_events (timestamp, ts_epoch, session_id, kind, command, root_hash, "
                "answer_tokens, covered, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.fromtimestamp(now).isoformat(), now, sid, "lookup", command[:40], root_hash,
                 int(answer_tokens), json.dumps(cov), "pending"),
            )
            conn.commit()
            event_id = cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return None
    if event_id and cov:
        data = _read_pending()
        paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
        for path, _tok in cov:
            ids = paths.setdefault(path, [])
            if event_id not in ids:
                ids.append(event_id)
        if len(paths) > PENDING_MAX_PATHS:
            for k in list(paths)[: len(paths) - PENDING_MAX_PATHS]:
                paths.pop(k, None)
        data["paths"] = paths
        data["until"] = max(float(data.get("until") or 0), now + FOLLOWUP_WINDOW_S)
        _write_pending(data)
    return event_id


def note_read(file_path: str, *, now: Optional[float] = None) -> bool:
    """Called on every Read: records a Read of a file a pending lookup covered.

    Fast path is one failed stat/read of a tiny sidecar, so the Read hook pays
    almost nothing when no lookup is pending.
    """
    now = time.time() if now is None else now
    data = _read_pending()
    if not data:
        return False
    paths = data.get("paths") or {}
    ids = paths.get(str(file_path))
    if float(data.get("until") or 0) < now:
        try:
            _pending_path().unlink()
        except OSError:
            pass
        return False
    if not ids:
        return False
    try:
        conn = _connect()
        try:
            for eid in ids:
                conn.execute("INSERT INTO codemap_reads VALUES (?,?,?)", (int(eid), str(file_path), now))
                conn.execute("UPDATE codemap_events SET reads_after=reads_after+1 WHERE id=?", (int(eid),))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return False
    return True


def resolve_pending(*, now: Optional[float] = None, window_s: float = FOLLOWUP_WINDOW_S) -> Dict[str, int]:
    """Resolve lookups whose follow-up window closed and book the net estimate."""
    now = time.time() if now is None else now
    out = {"resolved": 0, "avoided_tokens": 0, "answer_tokens": 0, "booked": 0}
    try:
        conn = _connect()
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT id, session_id, answer_tokens, covered FROM codemap_events "
            "WHERE kind='lookup' AND status='pending' AND ts_epoch < ? LIMIT 500",
            (now - window_s,)).fetchall()
        by_session: Dict[str, List[int]] = {}
        for eid, sid, ans, cov_raw in rows:
            try:
                cov = json.loads(cov_raw or "[]")
            except ValueError:
                cov = []
            read = {r[0] for r in conn.execute("SELECT path FROM codemap_reads WHERE event_id=?", (eid,))}
            avoided = sum(int(t) for p, t in cov if p not in read)
            conn.execute(
                "UPDATE codemap_events SET status='resolved', avoided_tokens=?, resolved_ts=? WHERE id=?",
                (avoided, now, eid))
            agg = by_session.setdefault(sid or "unknown", [0, 0])
            agg[0] += avoided
            agg[1] += int(ans or 0)
            out["resolved"] += 1
            out["avoided_tokens"] += avoided
            out["answer_tokens"] += int(ans or 0)
        conn.commit()
    except Exception:
        return out
    finally:
        conn.close()
    # Net per session: lookups that did not help still cost their answers.
    for sid, (avoided, answers) in by_session.items():
        net = avoided - answers
        if net > 0 and _book_savings("codemap_lookup", net, sid, f"lookups resolved; avoided={avoided} answers={answers}"):
            out["booked"] += net
    if rows:
        try:
            if _read_pending() and float(_read_pending().get("until") or 0) < now:
                _pending_path().unlink()
        except OSError:
            pass
    return out


def log_outline_served(file_path: str, file_tokens: int, outline_tokens: int, session_id: str) -> int:
    """A full re-read was replaced by an outline: a measured removal."""
    net = max(0, int(file_tokens) - int(outline_tokens))
    now = time.time()
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO codemap_events (timestamp, ts_epoch, session_id, kind, command, answer_tokens, "
                "covered, status, avoided_tokens) VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.fromtimestamp(now).isoformat(), now, session_id, "reread_outline", "outline",
                 int(outline_tokens), json.dumps([[str(file_path), int(file_tokens)]]), "served", net))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    if net > 0:
        _book_savings("codemap_outline", net, session_id, f"{Path(file_path).name}:codemap_outline")
    return net


def _book_savings(event_type: str, tokens: int, session_id: str, detail: str) -> bool:
    """Write through read_cache's savings writer (pricing, uuid join, origin)."""
    try:
        import read_cache
        # read_cache binds its data paths at import; point them at the current
        # resolver so a long-lived importer never writes a stale trends.db.
        read_cache.SNAPSHOT_DIR = _snapshot_dir()
        read_cache.TRENDS_DB = _trends_db()
        before = _count_rows(event_type)
        read_cache._log_savings_event(event_type, int(tokens), session_id, detail)
        return _count_rows(event_type) > before
    except Exception:
        return False


def _count_rows(event_type: str) -> int:
    try:
        conn = sqlite3.connect(str(_trends_db()), timeout=3.0)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM savings_events WHERE event_type=?",
                                    (event_type,)).fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return -1


def summary(days: int = 30) -> Dict[str, int]:
    """Totals for reports: lookups, resolved, avoided, answer cost, outlines."""
    since = time.time() - days * 86400
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*), SUM(status='resolved'), COALESCE(SUM(avoided_tokens),0), "
                "COALESCE(SUM(answer_tokens),0), COALESCE(SUM(reads_after>0),0) "
                "FROM codemap_events WHERE kind='lookup' AND ts_epoch>=?", (since,)).fetchone()
            orow = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(avoided_tokens),0) FROM codemap_events "
                "WHERE kind='reread_outline' AND ts_epoch>=?", (since,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    return {
        "lookups": int(row[0] or 0), "resolved": int(row[1] or 0),
        "avoided_tokens": int(row[2] or 0), "answer_tokens": int(row[3] or 0),
        "lookups_followed_by_read": int(row[4] or 0),
        "outlines": int(orow[0] or 0), "outline_tokens_saved": int(orow[1] or 0),
    }
