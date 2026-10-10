"""compression_events must not persist caller text unredacted.

command_pattern/detail were already redacted at the DB boundary, but `feature`
and `session_id` were written verbatim — a future caller forwarding tainted
text into either column would persist it raw. Redact all caller-text columns;
session_uuid derivation and joins still work because real ids are never
credential-shaped.

Secrets are assembled at runtime (push protection scans test files).
Run: python3 -m pytest tests/test_compression_event_redaction.py -q
"""
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_SECRET = "sk-ant-" + "C" * 30


def _read_rows(tmp_path):
    db = tmp_path / "snap" / "trends.db"
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT feature, session_id, session_uuid, command_pattern, detail "
            "FROM compression_events"
        ).fetchall()
    finally:
        conn.close()


def test_feature_and_session_id_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    import compression_log
    monkeypatch.setattr(compression_log, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(compression_log, "TRENDS_DB", tmp_path / "snap" / "trends.db")
    compression_log.log_compression_event(
        feature=f"probe-{_SECRET}", session_id=f"sess-{_SECRET}",
        command_pattern="x", detail="y")
    rows = _read_rows(tmp_path)
    assert rows, "no row written"
    feature, session_id, _uuid, _cp, _d = rows[0]
    assert _SECRET not in str(feature)
    assert _SECRET not in str(session_id)


def test_real_uuid_session_id_survives_and_joins(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    import compression_log
    monkeypatch.setattr(compression_log, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(compression_log, "TRENDS_DB", tmp_path / "snap" / "trends.db")
    sid = "22222222-2222-2222-2222-222222222222"
    compression_log.log_compression_event(
        feature="first_read_skeleton", session_id=sid, command_pattern="x")
    rows = _read_rows(tmp_path)
    assert rows[0][0] == "first_read_skeleton"
    assert rows[0][1] == sid
    assert rows[0][2] == sid  # session_uuid join key intact


def test_measure_variant_redacts_feature(tmp_path, monkeypatch):
    """measure.py owns a second copy of the same write path."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    import measure
    captured = {}

    class _FakeConn:
        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("INSERT"):
                captured["params"] = params
            return self

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(measure, "_init_trends_db", lambda *a, **k: _FakeConn())
    measure._log_compression_event(
        feature=f"probe-{_SECRET}", session_id=f"sess-{_SECRET}",
        command_pattern="x", detail="y")
    params = captured.get("params")
    assert params, "insert never ran"
    assert _SECRET not in str(params)


def test_redactor_failure_still_writes_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    import compression_log
    monkeypatch.setattr(compression_log, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(compression_log, "TRENDS_DB", tmp_path / "snap" / "trends.db")

    import credential_patterns as cp
    monkeypatch.setattr(cp, "redact_credentials",
                        lambda _t: (_ for _ in ()).throw(RuntimeError("no redactor")))
    compression_log.log_compression_event(
        feature=f"probe-{_SECRET}", session_id=f"sess-{_SECRET}",
        command_pattern=f"cmd-{_SECRET}", detail=f"d-{_SECRET}")
    rows = _read_rows(tmp_path)
    assert rows, "row should still be written (fail-open on counts)"
    feature, session_id, _uuid, command_pattern, detail = rows[0]
    assert _SECRET not in str(rows[0])
    assert command_pattern is None and detail is None
