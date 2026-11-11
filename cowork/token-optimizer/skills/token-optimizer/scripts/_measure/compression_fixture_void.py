"""Bash compression rows that no model call ever produced.

The PostToolUse bash hook records the session and reads the model from the
call's transcript. A row with no session, model ``unknown`` (the hook could not
read a transcript) and no Bash call with that command anywhere in the user's
transcripts near its time was written by a synthetic payload (a test harness
pointed at the live data dir), not by a real call. It earns nothing.

A verdict is recorded only when transcripts from that time still exist (some
main transcript spans the event), so a user whose old transcripts were cleaned
up never loses real rows. Verdicts are stored in their own table; the ledger
row is never changed, and the counted view leaves out ``fixture`` verdicts.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TABLE = "compression_fixture_verdicts"
MATCH_SECONDS = 900
FIXTURE, LOCATED, UNCOVERED = "fixture", "located", "uncovered"


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS " + TABLE + " (event_id INTEGER PRIMARY KEY, verdict TEXT NOT NULL)")


def candidates(conn):
    _ensure(conn)
    return conn.execute(
        "SELECT c.id, c.timestamp, c.command_pattern FROM compression_events c "
        "WHERE c.feature GLOB 'bash_compress*' AND COALESCE(c.session_id,'')='' AND COALESCE(c.session_uuid,'')='' "
        "AND c.model='unknown' AND COALESCE(c.command_pattern,'')!='' AND c.original_tokens > c.compressed_tokens "
        "AND NOT EXISTS (SELECT 1 FROM " + TABLE + " v WHERE v.event_id=c.id) ORDER BY c.timestamp").fetchall()


def _first_ts(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for _index, line in zip(range(50), stream):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                stamp = record.get("timestamp") if isinstance(record, dict) else None
                try:
                    return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return None


def _utc_mtime(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(tzinfo=None)


def collect(conn, event_utc, projects_dir, deadline_seconds=None):
    """Record verdicts for unjudged candidate rows. Returns counts by verdict."""
    import time
    from . import counted_subagents as sub
    started = time.monotonic()
    out = {"candidates": 0, FIXTURE: 0, LOCATED: 0, UNCOVERED: 0, "pending": 0}
    rows = []
    for ident, stamp, command in candidates(conn):
        when = event_utc(stamp)
        if when is not None:
            rows.append((ident, when, command))
    out["candidates"] = len(rows)
    projects_dir = Path(projects_dir)
    if not rows or not projects_dir.is_dir():
        return out
    pad = timedelta(seconds=MATCH_SECONDS)
    low, high = min(r[1] for r in rows) - pad, max(r[1] for r in rows) + pad
    commands = {r[2] for r in rows}
    from . import compression_scan_checkpoint
    import os
    checkpoint = compression_scan_checkpoint.FixtureScan(conn, commands) if os.environ.get("TOKEN_OPTIMIZER_UPGRADE_DRAIN") == "1" else None
    spans, calls = [], {}
    files = [(p, False) for p in projects_dir.glob("*/*.jsonl")] + \
            [(p, True) for p in projects_dir.glob("*/*/subagents/agent-*.jsonl")]
    for path, sidechain in files:
        if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:
            out["pending"] = len(rows)
            return out
        try:
            before = path.stat()
            last = datetime.fromtimestamp(before.st_mtime, timezone.utc).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            if checkpoint:
                out["pending"] = len(rows)
                return out
            continue
        if checkpoint and last < low:
            checkpoint.track_metadata(path, before)
        elif checkpoint:
            try:
                checkpoint.track(path, before)
            except OSError:
                out["pending"] = len(rows)
                return out
        if last < low:
            continue
        first = _first_ts(path)
        if first is None or first > high:
            continue
        if not sidechain:
            spans.append((first, last))
        if checkpoint:
            try:
                found = checkpoint.calls(path, sidechain, before, commands, sub)
            except (OSError, ValueError):
                found = None
            if found is None:
                out["pending"] = len(rows)
                return out
        else:
            found = []
            for dt, _tid, name, command, _size in sub.index_results(path, sidechain):
                if name == "Bash" and command:
                    stored = sub._stored_command(command)
                    if stored in commands:
                        found.append((stored, dt))
        for stored, dt in found:
            calls.setdefault(stored, []).append(dt)
    if checkpoint:
        current = [(p, False) for p in projects_dir.glob("*/*.jsonl")] + [(p, True) for p in projects_dir.glob("*/*/subagents/agent-*.jsonl")]
        if not checkpoint.stable() or set(files) != set(current):
            out["pending"] = len(rows)
            return out
    for ident, when, command in rows:
        if any(abs((dt - when).total_seconds()) <= MATCH_SECONDS for dt in calls.get(command, ())):
            verdict = LOCATED
        elif any(first <= when <= last for first, last in spans):
            verdict = FIXTURE
        else:
            verdict = UNCOVERED
        conn.execute("INSERT OR IGNORE INTO " + TABLE + " VALUES (?,?)", (ident, verdict))
        out[verdict] += 1
    conn.commit()
    return out


def view_filter(alias):
    """SQL clause leaving out rows judged fixture output."""
    return ("NOT EXISTS (SELECT 1 FROM main." + TABLE + " v WHERE v.event_id=" + alias
            + ".id AND v.verdict='" + FIXTURE + "')")
