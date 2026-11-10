"""Helpers for evaluating MCP archive re-expansion and building previews."""

import re
import math
import sqlite3
import time
from datetime import datetime

SAMPLE_SIZE = 20
WINDOW = 300


def _connect(db):
    conn = sqlite3.connect(str(db), timeout=0.05)
    conn.execute('CREATE TABLE IF NOT EXISTS mcp_archive_net_observations '
                 '(tool TEXT NOT NULL, session TEXT NOT NULL, archive_id TEXT NOT NULL, '
                 'created REAL NOT NULL, saved INTEGER NOT NULL, PRIMARY KEY(session, archive_id))')
    conn.execute('CREATE INDEX IF NOT EXISTS mcp_archive_net_observations_tool_time '
                 'ON mcp_archive_net_observations(tool, created DESC)')
    conn.execute('CREATE TABLE IF NOT EXISTS mcp_archive_expand_calls '
                 '(session TEXT NOT NULL, archive_id TEXT NOT NULL, created REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS mcp_archive_expand_calls_lookup '
                 'ON mcp_archive_expand_calls(session, archive_id, created)')
    conn.execute('CREATE TABLE IF NOT EXISTS mcp_archive_net_demotions '
                 '(tool TEXT PRIMARY KEY, created REAL NOT NULL, samples INTEGER NOT NULL, '
                 'archived INTEGER NOT NULL, reexpanded INTEGER NOT NULL, overhead INTEGER NOT NULL)')
    return conn


def record(db, tool, session, archive_id, saved, now=None):
    """Record a replacement, never an archive that served the full result."""
    try:
        with _connect(db) as conn:
            conn.execute('INSERT OR IGNORE INTO mcp_archive_net_observations VALUES (?,?,?,?,?)',
                         (tool, session, archive_id, time.time() if now is None else now, saved))
    except sqlite3.Error:
        pass


def expand_call_overhead(archive_id):
    """Conservative proxy: command characters plus 16 framing tokens."""
    return 16 + math.ceil(len('python3 measure.py expand ' + archive_id) / 3.3)


def record_expand(db, session, archive_id, now=None):
    """Count every expand call, including repeats without a fresh debit."""
    try:
        with _connect(db) as conn:
            conn.execute('INSERT INTO mcp_archive_expand_calls SELECT ?,?,? WHERE EXISTS '
                         '(SELECT 1 FROM mcp_archive_net_observations WHERE session=? AND archive_id=?)',
                         (session, archive_id, time.time() if now is None else now, session, archive_id))
    except Exception:
        pass


def decide(db, tool, active=False, now=None):
    """Recompute a sticky demotion on mature samples; shadow never blocks."""
    now = time.time() if now is None else now
    try:
        with _connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            demoted = conn.execute('SELECT 1 FROM mcp_archive_net_demotions WHERE tool=?', (tool,)).fetchone()
            if not demoted:
                rows = conn.execute('SELECT session, archive_id, created, saved FROM mcp_archive_net_observations '
                                    'WHERE tool=? AND created<=? ORDER BY created DESC, rowid DESC LIMIT ?',
                                    (tool, now - WINDOW, SAMPLE_SIZE)).fetchall()
                expanded = overhead = 0
                for session, archive_id, created, saved in rows:
                    start = datetime.fromtimestamp(created).isoformat()
                    end = datetime.fromtimestamp(now).isoformat()
                    matches = conn.execute("SELECT detail,tokens_saved FROM savings_events WHERE event_type='tool_archive_reexpand' "
                                           'AND session_id=? AND timestamp>=? AND timestamp<=?',
                                           (session, start, end)).fetchall()
                    expanded += sum(tokens or 0 for detail, tokens in matches if (detail or '').split(' ', 1)[0] == archive_id)
                    calls = conn.execute('SELECT COUNT(*) FROM mcp_archive_expand_calls WHERE session=? AND archive_id=? AND created>=? AND created<=?',
                                         (session, archive_id, created, now)).fetchone()[0]
                    overhead += calls * expand_call_overhead(archive_id)
                archived = sum(row[3] for row in rows)
                if len(rows) == SAMPLE_SIZE and expanded + overhead >= archived:
                    conn.execute('INSERT OR IGNORE INTO mcp_archive_net_demotions VALUES (?,?,?,?,?,?)',
                                 (tool, now, len(rows), archived, expanded, overhead))
                    demoted = True
            return bool(active and demoted)
    except (sqlite3.Error, OSError, ValueError):
        return False


def evaluate(events, sample_size=20):
    """Demote a complete sample when its signed net is not positive."""
    recent = list(events)[-sample_size:]
    return len(recent) == sample_size and sum(reexpanded + overhead for _, reexpanded, overhead in recent) >= sum(saved for saved, _, _ in recent)


def structured_preview(text, limit=3500, breaks_cluster=None):
    """Show both ends and a bounded outline of recognizable headings."""
    if len(text) <= limit:
        return text
    headings = []
    for line in text[:131072].splitlines():
        line = line.strip()
        if re.match(r'^#{1,6}\s+\S', line):
            headings.append(line[:120])
        else:
            key = re.match(r'^"([^"\\]{1,80})"\s*:', line)
            if key and key.group(1) not in headings:
                headings.append(key.group(1))
    outline = '\n'.join(headings)
    outline = outline[:min(1000, limit // 3)]
    prefix = '\n\n[Outline]\n' + outline if outline else ''
    separator = '\n\n[... middle omitted ...]\n\n'
    remaining = max(0, limit - len(prefix) - len(separator))
    head = remaining * 2 // 3
    tail = remaining - head
    tail_start = len(text) - tail
    if breaks_cluster:
        while head > 0 and breaks_cluster(text, head):
            head -= 1
        while tail_start < len(text) and breaks_cluster(text, tail_start):
            tail_start += 1
    return text[:head] + prefix + separator + text[tail_start:]
