"""A new explicit re-promotion produces a real, countable replacement."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from test_counted_cumulative import SID, _db, _transcript, m

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import archive_result
import compression_log
import read_cache


def test_repromotion_records_actual_replacement_and_compounds(m, tmp_path, monkeypatch, capsys):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(read_cache, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(read_cache, "_is_first_read_active_enabled", lambda: True)
    monkeypatch.setattr(read_cache, "_is_first_read_shadow_enabled", lambda: True)
    monkeypatch.setattr(compression_log, "TRENDS_DB", db)
    monkeypatch.setattr(read_cache, "_demoted_cohorts", lambda: frozenset())
    monkeypatch.setattr(archive_result, "SNAPSHOT_DIR", tmp_path)
    class Lock:
        def __init__(self, *args, **kwargs): pass
        def acquire(self): return True
        def release(self): pass
    monkeypatch.setattr(archive_result, "LeaseLock", Lock)
    source = tmp_path / "example.py"
    source.write_text("\n".join(f"def task_{i}(value):\n    result_{i} = value + {i}\n    note_{i} = 'An explanatory string of ordinary application content {i}'\n    return result_{i}, note_{i}\n" for i in range(180)))
    band = read_cache._first_read_size_band(source.stat().st_size)
    cohort = ("python", band)
    assert cohort in read_cache.FIRST_READ_ACTIVE_COHORTS
    summary = read_cache.summarize_code_file(str(source), content=source.read_text(), file_size_bytes=source.stat().st_size)
    assert summary.eligible, summary.reason
    m._write_tripwire_sidecar({cohort}, {})
    assert m.cohorts_promote(["python:" + band]) == 1
    from _measure.first_read_revive import enabled
    assert enabled(tmp_path / "cohort_tripwire.json", "python", band)
    capsys.readouterr()
    class Store:
        def set_meta(self, *args): pass
    served = read_cache._first_read_compress(str(source), source.stat(), 0, 0, "python", SID, Store(), "soft_block", tool_use_id="read-1", parent_session_id=SID)
    response = json.loads(capsys.readouterr().out)
    assert served and response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "expand " in response["hookSpecificOutput"]["additionalContext"]
    key = archive_result.derive_archive_key(SID, str(source), source.stat().st_mtime_ns)
    assert archive_result.archive_entry_exists(SID, key)
    exposure = json.loads((tmp_path / "diagnostics/periphery_exposures.jsonl").read_text())
    assert exposure["arm"] == "legacy_skeleton" and str(source) not in json.dumps(exposure)
    from _measure.first_read_outcomes import _hash
    pending = json.loads((tmp_path / "diagnostics/first_read_pending.jsonl").read_text())
    assert pending["parent_session_id"] == _hash(SID)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM compression_events WHERE feature='first_read_skeleton'").fetchone()[0] == 0
        start = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=10)
        transcript, _ = _transcript(m, tmp_path, monkeypatch, n_turns=4, start=start)
        marker = "large-file first read served as a code outline"
        prefix = [
            {"type":"assistant", "message":{"content":[{"type":"tool_use", "name":"Read", "id":"read-1"}]}},
            {"type":"attachment", "attachment":{"toolUseID":"read-1", "content":marker}},
            {"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"read-1", "content":marker}]}},
        ]
        conn.execute("INSERT INTO session_log(date,session_uuid,jsonl_path) VALUES(?,?,?)", (start.date().isoformat(), SID, str(transcript)))
        conn.commit()
        turns = transcript.read_text()
        transcript.write_text("".join(json.dumps(row) + "\n" for row in prefix[:2]) + turns)
        m._update_counted_cumulative(conn, quiet=True)
        assert conn.execute("SELECT COUNT(*) FROM compression_events WHERE feature='first_read_skeleton'").fetchone()[0] == 0
        transcript.write_text("".join(json.dumps(row) + "\n" for row in prefix) + turns)
        m._update_counted_cumulative(conn, quiet=True)
        rows = conn.execute("SELECT original_tokens,compressed_tokens,tier FROM compression_events WHERE feature='first_read_skeleton'").fetchall()
        assert len(rows) == 1 and rows[0][2] == "measured" and rows[0][0] > rows[0][1] > 0
        counted = conn.execute("SELECT tokens,reread_tokens,turns_counted FROM counted_reread").fetchone()
        m._update_counted_cumulative(conn, quiet=True)
        assert conn.execute("SELECT COUNT(*) FROM compression_events WHERE feature='first_read_skeleton'").fetchone()[0] == 1
    assert counted == (rows[0][0] - rows[0][1], (rows[0][0] - rows[0][1]) * 3, 3)
    with sqlite3.connect(db) as check:
        assert check.execute("SELECT COUNT(*) FROM first_read_confirmations").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM compression_events WHERE feature='first_read_skeleton' AND tier='measured'").fetchone()[0] == 1


def test_only_new_matching_promotion_can_enable_replacement(tmp_path):
    from _measure.first_read_revive import enabled
    sidecar = tmp_path / "sidecar.json"
    cohort = "python|16-64KB"
    sidecar.write_text(json.dumps({"demoted": [], "promoted_at": {cohort: 123}}))
    assert not enabled(sidecar, "python", "16-64KB")
    sidecar.write_text(json.dumps({"demoted": [["python", "16-64KB"]], "promoted_at": {cohort: 123, "target_skeleton_v1|" + cohort: 123}}))
    assert not enabled(sidecar, "python", "16-64KB")
    sidecar.write_text("invalid")
    assert not enabled(sidecar, "python", "16-64KB")


def test_failed_archive_or_emission_never_logs_removal(monkeypatch, tmp_path):
    from _measure.first_read_revive import serve
    source = tmp_path / "test.py"
    source.write_text("test" * 100)
    event = []
    monkeypatch.setattr(compression_log, "log_compression_event", lambda **kw: event.append(kw))
    monkeypatch.setattr(archive_result, "derive_archive_key", lambda *a: "key")
    monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: None)
    args = (str(source), source.stat(), "python", "16-64KB", SID, "original", SimpleNamespace(replacement_text="outline"), 1000, len)
    assert not serve(*args, lambda *a: None)
    monkeypatch.setattr(archive_result, "archive_original", lambda *a, **k: "key")
    monkeypatch.setattr(archive_result, "archive_entry_exists", lambda *a: True)
    monkeypatch.setattr(archive_result, "build_archive_pointer", lambda *a: "outline")
    assert not serve(*args, lambda *a: None, allow_context=lambda *a: False)
    def failed_emit(*args): raise BrokenPipeError()
    assert not serve(*args, failed_emit)
    assert event == []


def test_confirmed_subagent_saving_compounds_only_its_own_turns(m, tmp_path, monkeypatch):
    from _measure.first_read_outcomes import _hash
    from _measure.periphery_exposure import _rows
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "SNAPSHOT_DIR", tmp_path)
    start = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=10)
    parent, _ = _transcript(m, tmp_path, monkeypatch, n_turns=8, start=start)
    sub = parent.with_suffix("") / "subagents" / "agent-agent123.jsonl"
    sub.parent.mkdir(parents=True)
    stamp = (start - timedelta(seconds=5)).isoformat() + "Z"
    reason = "large-file first read served as a code outline"
    records = [
        {"type":"assistant", "isSidechain":True, "timestamp":stamp, "message":{"content":[{"type":"tool_use", "name":"Read", "id":"toolu_read1"}]}},
        {"type":"attachment", "attachment":{"toolUseID":"toolu_read1", "content":reason}},
        {"type":"user", "isSidechain":True, "timestamp":stamp, "message":{"content":[{"type":"tool_result", "tool_use_id":"toolu_read1", "content":reason}]}},
    ]
    for i in range(3):
        records.append({"type":"assistant", "isSidechain":True, "timestamp":(start + timedelta(minutes=i)).isoformat() + "Z", "requestId":f"sub-{i}", "message":{"model":"claude-opus-5", "usage":{"input_tokens":2, "cache_read_input_tokens":1000, "cache_creation_input_tokens":10}}})
    sub.write_text("".join(json.dumps(row) + "\n" for row in records))
    row = _rows("agent123", "toolu_read1", "/private/source.py", "python", "16-64KB", "legacy_skeleton")[0]
    row.update(parent_session_id=_hash(SID), timestamp_local=(datetime.now().astimezone().replace(tzinfo=None)).isoformat(), original_tokens=1000, compressed_tokens=100)
    diag = tmp_path / "diagnostics"
    diag.mkdir()
    (diag / "first_read_pending.jsonl").write_text(json.dumps(row) + "\n")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO session_log(date,session_uuid,jsonl_path) VALUES(?,?,?)", (start.date().isoformat(), SID, str(parent)))
        conn.commit()
        m._update_counted_cumulative(conn, quiet=True)
        row = conn.execute("SELECT tokens,reread_tokens,turns_counted FROM counted_reread").fetchone()
        event_id = conn.execute("SELECT event_id FROM first_read_confirmation_scopes").fetchone()[0]
        from _measure import counted_subagents
        monkeypatch.setattr(counted_subagents, "subagent_files", lambda path: [])
        scope = counted_subagents.SessionScope(m, conn, parent, [(f"ce:{event_id}", stamp)])
        assert scope.turns_for(f"ce:{event_id}", [("parent",)], []) == ([], [], "subagent")
        class MissingScope:
            def execute(self, sql, params=()):
                if "FROM first_read_confirmation_scopes" in sql:
                    raise sqlite3.OperationalError("missing table")
                return conn.execute(sql, params)
        scope = counted_subagents.SessionScope(m, MissingScope(), parent, [(f"ce:{event_id}", stamp)])
        assert scope.turns_for(f"ce:{event_id}", [("parent",)], []) == ([], [], "subagent")
        class BrokenDB:
            def execute(self, *args): raise sqlite3.DatabaseError("unreadable")
        scope = counted_subagents.SessionScope(m, BrokenDB(), parent, [(f"ce:{event_id}", stamp)])
        assert scope.turns_for(f"ce:{event_id}", [("parent",)], []) == ([], [], "subagent")
    assert row == (900, 1800, 2)
