"""Funnel records only identifiers, fixed stage values, and bounded reasons."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure.structure_funnel import Funnel, summarize
import hashlib
from test_read_cache_first_read_retarget import _run_read_cache, SESSION
import json


def test_stages_and_untrusted_reasons_are_bounded():
    records = []
    funnel = Funnel("session", "agent", "tool", records.append)
    funnel.state(True)
    funnel.metadata(True, False, True)
    funnel.structure(False, "secret/path.py")
    funnel.finish()
    row = records[0]
    assert len(row["session_id"]) == 64 and len(row["tool_use_id"]) == 64
    assert "session" != row["session_id"] and "tool" != row["tool_use_id"]
    assert row["state_found"] is True and row["size_match"] is False
    assert row["structure_reason"] == "other"
    assert "secret" not in str(row)
    assert row["output_emitted"] is False and row["event_written"] is False


def test_records_are_not_written_without_a_tool_id():
    records = []
    Funnel("session", "", "", records.append).finish()
    assert records == []


def test_read_hook_records_repeated_structure_decisions(tmp_path):
    path = tmp_path / "target.py"
    path.write_text((Path(__file__).parent / "test_counted_cumulative.py").read_text())
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(path)},
               "session_id": SESSION, "agent_id": "agent-synthetic", "tool_use_id": "first"}
    env = {"TOKEN_OPTIMIZER_READ_CACHE_MODE": "block", "TOKEN_OPTIMIZER_FIRST_READ_SHADOW": "0"}
    assert _run_read_cache(tmp_path, payload, env).returncode == 0
    payload["tool_use_id"] = "second"
    result = _run_read_cache(tmp_path, payload, env)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in (tmp_path / "diagnostics/structure_funnel.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["state_found"] is False
    second = records[1]
    assert second["state_found"] is True
    assert second["mtime_match"] and second["size_match"] and second["range_covered"]
    assert second["structure_eligible"] and second["structure_reason"] == "ok"
    assert second["output_emitted"] is True and second["event_written"] is True
    assert "target.py" not in json.dumps(records)


def test_funnel_replay_requires_a_matching_delivered_result():
    tid = hashlib.sha256(b"call").hexdigest()
    records = [{"session_id": "s", "tool_use_id": tid, "state_found": True,
                "mtime_match": True, "size_match": True, "range_covered": True,
                "structure_eligible": True, "output_emitted": True, "event_written": True}]
    rows = [{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call",
              "content": "file is unchanged and already in context; using outline"}]}}]
    summary = summarize(records, rows)
    assert summary["eligible_structure"] == 1 and summary["delivered"] == 1 and summary["event_written"] == 1
    assert summarize(records, [])["delivered"] == 0
    assert summarize(records + records, rows)["ambiguous_duplicates"] == 1
