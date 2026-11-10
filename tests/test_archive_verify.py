"""The archive hook verifies at the source that its replacement was applied.

A later hook call in the same session reads the earlier call's tool_result from
the transcript. Pointer there: the saving is confirmed. Full output there: the
host ignored the replacement, the ledger row leaves the counted totals (the row
itself is untouched), and after MIN_STREAK such results in a row the dashboard
and doctor warn.
"""
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))
SID = "11111111-1111-1111-1111-111111111111"
BODY = "payload\n" * 1000


def _env(tmp_path):
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"), "TOKEN_OPTIMIZER_RUNTIME": "claude"})
    return env


def _hook(tmp_path, tid, transcript, body=BODY):
    payload = {"tool_name": "mcp__demo__dump", "session_id": SID, "tool_use_id": tid, "tool_input": {},
               "tool_response": body, "transcript_path": str(transcript)}
    p = subprocess.run([sys.executable, str(SCRIPTS / "archive_result.py")], input=json.dumps(payload),
                       text=True, capture_output=True, env=_env(tmp_path), timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"]["updatedMCPToolOutput"]


def _result_line(tid, content):
    return json.dumps({"type": "user", "timestamp": "2026-08-01T10:00:00Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}}) + "\n"


def _verdicts(tmp_path):
    with sqlite3.connect(tmp_path / "snap" / "trends.db") as conn:
        return dict(conn.execute("SELECT tool_use_id, verdict FROM archive_verifications").fetchall())


def test_host_that_kept_the_full_result_is_caught_on_the_next_call(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")
    _hook(tmp_path, "toolu_a", transcript)
    assert _verdicts(tmp_path) == {"toolu_a": None}
    with transcript.open("a") as f:
        f.write(_result_line("toolu_a", BODY))                      # the host ignored the replacement
    replacement = _hook(tmp_path, "toolu_b", transcript)
    assert _verdicts(tmp_path) == {"toolu_a": "full", "toolu_b": None}
    with transcript.open("a") as f:
        f.write(_result_line("toolu_b", replacement))               # this time it was applied
    _hook(tmp_path, "toolu_c", transcript)
    assert _verdicts(tmp_path)["toolu_b"] == "pointer"


def test_full_verdict_leaves_counted_totals_and_keeps_the_ledger_row(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")
    _hook(tmp_path, "toolu_a", transcript)
    replacement = _hook(tmp_path, "toolu_b", transcript, body=BODY + "x" * 500)   # distinct row, not a twin
    with transcript.open("a") as f:
        f.write(_result_line("toolu_a", BODY) + _result_line("toolu_b", replacement))
    import archive_verify
    archive_verify.verify(tmp_path / "snap" / "trends.db", SID)
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    importlib.reload(m)
    conn = m._init_trends_db()
    try:
        assert conn.execute("SELECT count(*) FROM savings_events WHERE event_type='tool_archive'").fetchone()[0] == 2
        view = m._counted_table(conn, "savings_events")
        assert conn.execute("SELECT count(*) FROM " + view + " WHERE event_type='tool_archive'").fetchone()[0] == 1
    finally:
        conn.close()
        sys.modules.pop("measure", None)


def test_health_warns_after_a_streak_of_full_results():
    import archive_verify
    conn = sqlite3.connect(":memory:")
    conn.execute(archive_verify._SCHEMA)
    assert archive_verify.health(conn)["status"] == "unknown"
    for i, verdict in enumerate(["pointer", "full", "full", "full"]):
        conn.execute("INSERT INTO archive_verifications (tool_use_id, session_id, verdict, checked_at) VALUES (?,?,?,?)",
                     ("t%d" % i, SID, verdict, "2026-08-01T10:00:0%d" % i))
    out = archive_verify.health(conn)
    assert out["status"] == "warning" and out["full_streak"] == 3 and "not applying" in out["message"]
    conn.execute("INSERT INTO archive_verifications (tool_use_id, session_id, verdict, checked_at) VALUES ('t9',?, 'pointer', '2026-08-01T10:00:09')", (SID,))
    assert archive_verify.health(conn)["status"] == "ok"


@pytest.mark.parametrize("length,marker,expected", [(8000, False, "full"), (300, False, "pointer"),
                                                     (5000, True, "pointer"), (4000, False, None)])
def test_verdict_rules(length, marker, expected):
    import archive_verify
    assert archive_verify.verdict_for(length, marker, 8000, 300) == expected
