"""Bash compression rows from synthetic hook payloads earn nothing.

Signature: no session id, model ``unknown`` (the hook could not read a
transcript), and no Bash call with that command near its time in any of the
user's transcripts, while transcripts from that time still exist. Ledger rows
are never changed; the counted view leaves the row out.
"""
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
WHEN = datetime(2026, 8, 28, 9, 0, 0)          # UTC-naive event time
CMD = "git log --oneline | head -20"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._init_trends_db().close()
    yield mod, tmp_path / "projects"
    sys.modules.pop("measure", None)


def _utc(ts):
    return datetime.fromisoformat(ts)


def _row(conn, model="unknown", sid=None, cmd=CMD, when=WHEN):
    conn.execute("INSERT INTO compression_events (timestamp, session_id, feature, command_pattern, original_tokens, "
                 "compressed_tokens, verified, tier, model) VALUES (?,?,?,?,?,?,1,'measured',?)",
                 (when.isoformat(), sid, "bash_compress_pipeline", cmd, 10937, 669, model))
    conn.commit()


def _transcript(projects, start, end, bash=None, name="s1"):
    proj = projects / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "timestamp": start.isoformat() + "Z", "message": {"content": "hi"}}]
    if bash:
        command, at = bash
        lines.append({"type": "assistant", "timestamp": at.isoformat() + "Z", "message": {"content": [
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": command}}]}})
        lines.append({"type": "user", "timestamp": at.isoformat() + "Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "out"}]}})
    path = proj / (name + ".jsonl")
    path.write_text("\n".join(map(json.dumps, lines)) + "\n")
    stamp = end.replace(tzinfo=timezone.utc).timestamp()
    import os
    os.utime(path, (stamp, stamp))
    return path


def _collect(mod, projects):
    from _measure import compression_fixture_void as cfv
    conn = mod._init_trends_db()
    try:
        return cfv.collect(conn, _utc, projects)
    finally:
        conn.close()


def _counts(mod):
    conn = mod._init_trends_db()
    try:
        view = mod._counted_table(conn, "compression_events")
        return (conn.execute("SELECT count(*) FROM " + view).fetchone()[0],
                conn.execute("SELECT count(*) FROM compression_events").fetchone()[0])
    finally:
        conn.close()


def test_synthetic_row_with_no_call_in_live_transcripts_is_excluded(m):
    mod, projects = m
    _transcript(projects, WHEN - timedelta(hours=2), WHEN + timedelta(hours=2), bash=("ls", WHEN))
    conn = mod._init_trends_db(); _row(conn); conn.close()
    assert _counts(mod) == (1, 1)
    out = _collect(mod, projects)
    assert out["fixture"] == 1
    assert _counts(mod) == (0, 1)
    assert _collect(mod, projects)["candidates"] == 0        # judged once


def test_row_whose_command_ran_in_a_transcript_counts(m):
    mod, projects = m
    _transcript(projects, WHEN - timedelta(hours=2), WHEN + timedelta(hours=2),
                bash=(CMD, WHEN - timedelta(seconds=3)))
    conn = mod._init_trends_db(); _row(conn); conn.close()
    assert _collect(mod, projects)["located"] == 1
    assert _counts(mod) == (1, 1)


def test_subagent_transcript_call_counts(m):
    mod, projects = m
    _transcript(projects, WHEN - timedelta(hours=2), WHEN + timedelta(hours=2))
    sub = projects / "-proj" / "s1" / "subagents"
    sub.mkdir(parents=True)
    agent = _transcript(projects, WHEN - timedelta(hours=1), WHEN + timedelta(hours=1), bash=(CMD, WHEN), name="tmp")
    agent.rename(sub / "agent-a.jsonl")
    conn = mod._init_trends_db(); _row(conn); conn.close()
    assert _collect(mod, projects)["located"] == 1
    assert _counts(mod) == (1, 1)


def test_cleaned_up_transcripts_never_void_a_row(m):
    mod, projects = m
    _transcript(projects, WHEN + timedelta(days=3), WHEN + timedelta(days=4))   # nothing spans the event
    conn = mod._init_trends_db(); _row(conn); conn.close()
    assert _collect(mod, projects)["uncovered"] == 1
    assert _counts(mod) == (1, 1)


@pytest.mark.parametrize("model,sid", [("claude-sonnet-5", None), ("unknown", "11111111-2222-3333-4444-555555555555")])
def test_rows_with_a_model_or_session_are_never_candidates(m, model, sid):
    mod, projects = m
    _transcript(projects, WHEN - timedelta(hours=2), WHEN + timedelta(hours=2))
    conn = mod._init_trends_db(); _row(conn, model=model, sid=sid); conn.close()
    assert _collect(mod, projects)["candidates"] == 0
    assert _counts(mod) == (1, 1)
