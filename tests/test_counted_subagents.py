"""Synthetic coverage for locating and scoring sub-agent removals."""
import json
import sqlite3
import sys
from datetime import timedelta

import pytest

from test_counted_cumulative import (SCRIPTS, _add_event, _db, _iso_z, _local_iso,
                                     _transcript, m)
sys.path.insert(0, str(SCRIPTS))
from _measure import counted_subagents as sub


def _agent(parent, start, command="make test", tid="toolu_agent", name="Bash", chars=4000):
    folder = parent.with_suffix("") / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (tid + ".jsonl")
    def record(kind, seconds, content, **extra):
        return json.dumps(dict(type=kind, timestamp=_iso_z(start + timedelta(seconds=seconds)),
                               isSidechain=True, message=dict(content=content, **extra)))
    lines = [record("assistant", 10, [{"type": "tool_use", "id": tid, "name": name,
                                       "input": {"command": command}}]),
             record("user", 30, [{"type": "tool_result", "tool_use_id": tid,
                                    "content": "[Full result archived (%s chars) - saved]" % chars}])]
    for seconds in (60, 120, 180):
        lines.append(json.dumps({"type": "assistant", "timestamp": _iso_z(start + timedelta(seconds=seconds)),
                                 "isSidechain": True, "requestId": tid + str(seconds),
                                 "message": {"model": "claude-opus-5", "usage": {
                                     "input_tokens": 2, "cache_read_input_tokens": 1000}}}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_compression_scores_agent_turns_and_window_matches(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    parent, start = _transcript(m, tmp_path, monkeypatch, n_turns=8)
    _agent(parent, start)
    _add_event(db, "bash_compress_pipeline", 1000, _local_iso(start + timedelta(seconds=30)), table="compression_events")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE compression_events SET command_pattern='make test'")
        conn.commit()
        stats = m._update_counted_cumulative(conn, quiet=True)
        rereads, detail = conn.execute("SELECT reread_tokens, pricing_detail FROM counted_reread").fetchone()
        window = m._counted_window_summary(conn, start, start + timedelta(hours=1))
    assert rereads == 2000
    assert json.loads(detail)["scope"] == "subagent"
    assert window["reread_tokens"] == rereads
    assert stats["located_subagent"] == 1
    assert stats["unlocated"] == 0


@pytest.mark.parametrize("kind,detail", [("tool_archive_reexpand", "reexpanded toolu_agent"),
                                       ("tool_archive", "replaced Bash (4,000 chars")])
def test_archive_locates_by_id_or_tool_and_size(m, tmp_path, monkeypatch, kind, detail):
    db = _db(m, tmp_path, monkeypatch)
    parent, start = _transcript(m, tmp_path, monkeypatch)
    _agent(parent, start)
    ts = _local_iso(start + timedelta(seconds=30))
    _add_event(db, kind, 1000, ts)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE savings_events SET detail=?", (detail,))
        scope = sub.SessionScope(m, conn, parent, [("se:1", ts)])
    assert scope.stats["located_subagent"] == 1
    assert scope.turns_for("se:1", [], [])[2] == "subagent"


def test_tied_commands_stay_on_parent(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    parent, start = _transcript(m, tmp_path, monkeypatch)
    _agent(parent, start)
    _agent(parent, start, tid="toolu_other")
    ts = _local_iso(start + timedelta(seconds=30))
    _add_event(db, "bash_compress_pipeline", 1000, ts, table="compression_events")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE compression_events SET command_pattern='make test'")
        scope = sub.SessionScope(m, conn, parent, [("ce:1", ts)])
    assert scope.stats["ambiguous"] == 1
    assert scope.turns_for("ce:1", [1], [2]) == ([1], [2], "parent")


def test_no_subagents_keeps_parent_score(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    parent, start = _transcript(m, tmp_path, monkeypatch)
    ts = _local_iso(start + timedelta(seconds=30))
    _add_event(db, "tool_archive", 1000, ts)
    with sqlite3.connect(db) as conn:
        turns, compacts, _ = m._counted_walk_transcript(parent)
        scope = sub.SessionScope(m, conn, parent, [("se:1", ts)])
        selected, boundaries, _ = scope.turns_for("se:1", turns, compacts)
        tier = m.PRICING_TIERS[m._load_pricing_tier()]
        assert m._counted_score_detail(selected, boundaries, m._counted_event_utc(ts), 1000, tier) == \
            m._counted_score_detail(turns, compacts, m._counted_event_utc(ts), 1000, tier)
