"""The cap-contradiction predicate only looks at mcp_cap rows, and costs O(1) per other row."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure import counted_cap  # noqa: E402


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE savings_events (id INTEGER PRIMARY KEY, event_type TEXT, detail TEXT, "
                 "session_id TEXT, timestamp TEXT)")
    conn.executemany("INSERT INTO savings_events(event_type, detail, session_id, timestamp) VALUES (?,?,?,?)", rows)
    return conn


def _flags(conn):
    sql = "SELECT e.id, COALESCE(%s, 0) FROM savings_events e ORDER BY e.id" % counted_cap.contradicted("e")
    # The predicate reads main.savings_events; an in-memory db is "main".
    return [r[1] for r in conn.execute(sql)]


def test_mcp_cap_row_with_a_matching_archive_row_is_contradicted_and_others_are_not():
    conn = _db([
        ("tool_archive", "replaced mcp__docs__fetch (40,000 chars -> 1,500 chars)", "s1", "2026-01-01T00:00:00"),
        ("mcp_cap", "cap event: mcp__docs__fetch result at cap", "s1", "2026-01-01T00:00:01"),   # contradicted
        ("mcp_cap", "cap event: mcp__other__tool result at cap", "s1", "2026-01-01T00:00:01"),   # no archive for it
        ("mcp_cap", "cap event: mcp__docs__fetch result at cap", "s1", "2026-01-01T01:00:00"),   # far in time
        ("bash", "replaced mcp__docs__fetch (x)", "s1", "2026-01-01T00:00:01"),
    ])
    assert _flags(conn) == [0, 1, 0, 0, 0]


def test_the_scan_runs_only_for_mcp_cap_rows():
    # Guard on the SQL shape: the archive scan sits inside a CASE on the event type, so engines that
    # evaluate AND terms eagerly cannot run it for every row.
    sql = counted_cap.contradicted("e")
    assert sql.startswith("(CASE WHEN e.event_type = 'mcp_cap' THEN ")
    assert sql.index("EXISTS") > sql.index("THEN ") and sql.endswith("ELSE 0 END)")
