"""Helpers for evaluating MCP archive re-expansion and building previews."""

import re
import sqlite3
import time
from datetime import datetime

SAMPLE_SIZE = 20
THRESHOLD = 0.5
WINDOW = 300


def _connect(db):
    conn = sqlite3.connect(str(db), timeout=0.05)
    conn.execute('CREATE TABLE IF NOT EXISTS mcp_archive_observations '
                 '(tool TEXT NOT NULL, session TEXT NOT NULL, archive_id TEXT NOT NULL, '
                 'created REAL NOT NULL, PRIMARY KEY(session, archive_id))')
    conn.execute('CREATE INDEX IF NOT EXISTS mcp_archive_observations_tool_time '
                 'ON mcp_archive_observations(tool, created DESC)')
    conn.execute('CREATE TABLE IF NOT EXISTS mcp_archive_demotions '
                 '(tool TEXT PRIMARY KEY, created REAL NOT NULL, samples INTEGER NOT NULL, '
                 'expanded INTEGER NOT NULL)')
    return conn


def record(db, tool, session, archive_id, now=None):
    """Record a replacement, never an archive that served the full result."""
    try:
        with _connect(db) as conn:
            conn.execute('INSERT OR IGNORE INTO mcp_archive_observations VALUES (?,?,?,?)',
                         (tool, session, archive_id, time.time() if now is None else now))
    except sqlite3.Error:
        pass


def decide(db, tool, active=False, now=None):
    """Recompute a sticky demotion on mature samples; shadow never blocks."""
    now = time.time() if now is None else now
    try:
        with _connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            demoted = conn.execute('SELECT 1 FROM mcp_archive_demotions WHERE tool=?', (tool,)).fetchone()
            if not demoted:
                rows = conn.execute('SELECT session, archive_id, created FROM mcp_archive_observations '
                                    'WHERE tool=? AND created<=? ORDER BY created DESC, rowid DESC LIMIT ?',
                                    (tool, now - WINDOW, SAMPLE_SIZE)).fetchall()
                expanded = 0
                for session, archive_id, created in rows:
                    start = datetime.fromtimestamp(created).isoformat()
                    end = datetime.fromtimestamp(created + WINDOW).isoformat()
                    matches = conn.execute("SELECT detail FROM savings_events WHERE event_type='tool_archive_reexpand' "
                                           'AND session_id=? AND timestamp>=? AND timestamp<=?',
                                           (session, start, end)).fetchall()
                    if any((detail or '').split(' ', 1)[0] == archive_id for (detail,) in matches):
                        expanded += 1
                if len(rows) == SAMPLE_SIZE and expanded / SAMPLE_SIZE > THRESHOLD:
                    conn.execute('INSERT OR IGNORE INTO mcp_archive_demotions VALUES (?,?,?,?)',
                                 (tool, now, len(rows), expanded))
                    demoted = True
            return bool(active and demoted)
    except (sqlite3.Error, OSError, ValueError):
        return False


def evaluate(events, sample_size=20, threshold=0.5):
    """Flag a tool when a majority of its last complete sample re-expanded."""
    recent = list(events)[-sample_size:]
    return len(recent) == sample_size and sum(bool(expanded) for _, expanded in recent) / sample_size > threshold


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
