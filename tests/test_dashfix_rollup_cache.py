import sqlite3

from test_counted_cumulative import m, _db, SID
from test_counted_session_mix_repricing import _legacy_row, _parts, _session


def test_month_rollups_share_session_mix_without_changing_prices(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        _session(conn, {'claude-opus-5-5': _parts(600)})
        _legacy_row(conn, 'se:1', 1_000_000)
        rows = list(m._counted_stored_rows(conn).values())
        expected = m._counted_rollup_rows(rows, conn)
        calls = []
        conn.set_trace_callback(calls.append)
        memo = {}
        assert m._counted_rollup_rows(rows, conn, memo) == expected
        assert m._counted_rollup_rows(rows, conn, memo) == expected
        reads = [s for s in calls if 'model_usage_breakdown_json FROM session_log' in s]
        assert len(reads) == 2
