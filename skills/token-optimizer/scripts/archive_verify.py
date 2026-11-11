"""Check at the source that an archived tool result was really replaced.

The archive hook returns a trimmed replacement and logs "replaced" right
away. Whether the host honoured the replacement only shows later, in the
session transcript: the tool_result there is either the pointer or the full
output. Some host versions ignored the replacement for months while the
ledger kept claiming the saving, and nothing warned.

``record`` notes each replacement as pending. ``verify`` runs on a later hook
call in the same session, reads back the tool_result for pending calls from
the transcript tail, and stores a verdict: ``pointer`` (the saving is real) or
``full`` (it is not; the counted view leaves that ledger row out). ``health``
raises a warning when the newest MIN_STREAK verdicts are all ``full``.

Hook hot path: stdlib only, bounded reads, never raises.
"""
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

TABLE = "archive_verifications"
MIN_STREAK = 3
TAIL_BYTES = 16 * 1024 * 1024
MARKER = "Full result archived"
_SCHEMA = ("CREATE TABLE IF NOT EXISTS " + TABLE + " (tool_use_id TEXT, session_id TEXT, event_id INTEGER, "
           "tool_name TEXT, original_chars INTEGER, pointer_chars INTEGER, transcript_path TEXT, "
           "logged_at TEXT, verdict TEXT, checked_at TEXT, PRIMARY KEY(session_id, tool_use_id))")


def _connect(trends_db, timeout=0.5):
    conn = sqlite3.connect(str(trends_db), timeout=timeout)
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    conn.execute(_SCHEMA)
    return conn


def record(trends_db, session_id, tool_use_id, tool_name, original_chars, pointer_chars, transcript_path, detail):
    """Note a logged replacement as pending verification. Never raises."""
    if not (session_id and tool_use_id and transcript_path):
        return
    conn = None
    try:
        conn = _connect(trends_db)
        row = conn.execute("SELECT max(id) FROM savings_events WHERE event_type='tool_archive' AND session_id=? AND detail=?",
                           (session_id, detail)).fetchone()
        conn.execute("INSERT OR IGNORE INTO " + TABLE + " (tool_use_id, session_id, event_id, tool_name, original_chars, "
                     "pointer_chars, transcript_path, logged_at) VALUES (?,?,?,?,?,?,?,?)",
                     (tool_use_id, session_id, row[0] if row else None, tool_name, int(original_chars),
                      int(pointer_chars), str(transcript_path), datetime.now().isoformat()))
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def _tail_results(path, wanted):
    """{tool_use_id: result text length, has pointer marker} from the transcript tail."""
    found = {}
    with open(path, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > TAIL_BYTES:
            stream.seek(size - TAIL_BYTES)
            stream.readline()
        for line in stream:
            if b'"tool_result"' not in line or not any(t.encode() in line for t in wanted):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result" or block.get("tool_use_id") not in wanted:
                    continue
                value = block.get("content")
                if isinstance(value, list):
                    value = "\n".join(b.get("text", "") for b in value if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(value, str):
                    found[block["tool_use_id"]] = (len(value), MARKER in value)
    return found


def verdict_for(length, has_marker, original_chars, pointer_chars):
    if has_marker or abs(length - pointer_chars) <= max(100, pointer_chars * 0.1):
        return "pointer"
    if length >= 0.9 * original_chars:
        return "full"
    return None


def verify(trends_db, session_id, limit=50):
    """Judge pending replacements of this session from its transcript. Never raises."""
    if not session_id:
        return 0
    conn = None
    judged = 0
    try:
        conn = _connect(trends_db)
        rows = conn.execute("SELECT tool_use_id, original_chars, pointer_chars, transcript_path FROM " + TABLE +
                            " WHERE session_id=? AND verdict IS NULL ORDER BY logged_at LIMIT ?", (session_id, limit)).fetchall()
        if not rows:
            return 0
        found = _tail_results(rows[0][3], {r[0] for r in rows})
        now = datetime.now().isoformat()
        for tool_use_id, original, pointer, _path in rows:
            if tool_use_id not in found:
                continue
            verdict = verdict_for(found[tool_use_id][0], found[tool_use_id][1], original, pointer)
            if verdict:
                conn.execute("UPDATE " + TABLE + " SET verdict=?, checked_at=? WHERE session_id=? AND tool_use_id=?",
                             (verdict, now, session_id, tool_use_id))
                judged += 1
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return judged


def health(conn, min_streak=MIN_STREAK):
    """{'status': ok|warning|unknown, ...} from the newest verdicts."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return {"status": "unknown", "verified": 0, "full": 0, "full_streak": 0}
        rows = conn.execute("SELECT verdict, checked_at FROM " + TABLE + " WHERE verdict IS NOT NULL "
                            "ORDER BY checked_at DESC, rowid DESC LIMIT 200").fetchall()
    except sqlite3.Error:
        return {"status": "unknown", "verified": 0, "full": 0, "full_streak": 0}
    streak = 0
    for verdict, _at in rows:
        if verdict != "full":
            break
        streak += 1
    full = sum(1 for v, _ in rows if v == "full")
    status = "unknown" if not rows else ("warning" if streak >= min_streak else "ok")
    out = {"status": status, "verified": len(rows), "full": full, "full_streak": streak,
           "last_checked": rows[0][1] if rows else None}
    if status == "warning":
        out["message"] = ("The last %d archived tool results stayed in full in the transcript: the host is not "
                          "applying archive replacements, and those savings are not counted." % streak)
    return out


def view_filter(alias):
    """SQL clause leaving out archive rows whose transcript kept the full result."""
    return ("NOT EXISTS (SELECT 1 FROM main." + TABLE + " w WHERE w.event_id=" + alias
            + ".id AND w.verdict='full')")


def replacement_check(trends_db):
    """Health of archive replacements for the dashboard and audit. Never raises."""
    try:
        conn = sqlite3.connect(Path(trends_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            return health(conn)
        finally:
            conn.close()
    except Exception:
        return {"status": "unknown", "verified": 0, "full": 0, "full_streak": 0}
