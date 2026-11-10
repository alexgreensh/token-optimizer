"""Measured first-read removals compound; shadow opportunities do not."""
import sqlite3
from datetime import timedelta

from test_counted_cumulative import SID, _add_event, _db, _local_iso, _transcript, m


def test_first_read_measured_and_shadow_rows_have_distinct_totals(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    _, start = _transcript(m, tmp_path, monkeypatch, n_turns=5)
    timestamp = _local_iso(start + timedelta(seconds=30))
    _add_event(dbp, "first_read_skeleton", 1000, timestamp, table="compression_events")
    with sqlite3.connect(str(dbp)) as conn:
        conn.execute(
            "INSERT INTO compression_events(timestamp,feature,original_tokens,"
            "compressed_tokens,verified,tier,session_uuid) VALUES(?,?,?,0,0,'opportunity',?)",
            (timestamp, "first_read_skeleton", 9000, SID),
        )
        conn.commit()
        m._update_counted_cumulative(conn, quiet=True)
        rows = conn.execute("SELECT tokens,reread_tokens,turns_counted FROM counted_reread").fetchall()
    assert rows == [(1000, 3000, 3)]
