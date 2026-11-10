"""Shadow outcome evidence and matched comparisons are not edit counts."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure.first_read_outcomes import inspect, compare, analyse_sessions, main


def hashed(s):
    return hashlib.sha256(s.encode()).hexdigest()


def assistant(mid, *blocks):
    return {"type": "assistant", "message": {"id": mid, "content": list(blocks)}}


def tool(tid, name, **args):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def result(tid, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": error}]}}


def test_failed_test_revert_and_repeated_region_are_outcomes():
    rows = [assistant("m0", tool("read", "Read", file_path="/a.py")), result("read"),
            assistant("m1", tool("e1", "Edit", file_path="/a.py", old_string="a", new_string="b")), result("e1"),
            assistant("m2", tool("e2", "Edit", file_path="/a.py", old_string="b", new_string="a")), result("e2"),
            assistant("m3", tool("e3", "Edit", file_path="/a.py", old_string="a", new_string="c")), result("e3"),
            assistant("m4", tool("t1", "Bash", command="pytest tests/test_a.py")), result("t1", True),
            assistant("m5", tool("done", "Read", file_path="/a.py"))]
    observation = inspect(rows, {"tool_use_id": hashed("read"), "arm": "no_map"}, window=5)
    assert observation["confirmed"] and observation["complete"]
    assert observation["failed_test"] and observation["revert"] and observation["repeated_region"]
    assert not observation["abandoned"]


def test_missing_delivery_or_short_followup_are_never_negative_outcomes():
    rows = [assistant("m0", tool("read", "Read", file_path="/a.py")), result("read")]
    assert not inspect(rows, {"tool_use_id": hashed("read"), "arm": "map"}, window=1)["confirmed"]
    assert not inspect(rows, {"tool_use_id": hashed("read"), "arm": "no_map"}, window=1)["complete"]
    pending = rows + [assistant("m1", tool("t1", "Bash", command="pytest"))]
    assert not inspect(pending, {"tool_use_id": hashed("read"), "arm": "no_map"}, window=1)["complete"]
    unknown_result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read"}]}}
    assert not inspect([rows[0], unknown_result], {"tool_use_id": hashed("read"), "arm": "no_map"}, window=1)["confirmed"]


def test_map_requires_the_matching_visible_hook_output():
    rows = [assistant("m0", tool("read", "Read", file_path="/a.py")), result("read"),
            {"type": "attachment", "attachment": {"toolUseID": "read", "stdout": "[Token Optimizer] Periphery skeletons for a.py"}},
            assistant("m1", tool("other", "Read", file_path="/b.py"))]
    output = inspect(rows, {"tool_use_id": hashed("read"), "arm": "map"}, window=1)
    assert output["confirmed"] and output["complete"] and not output["harm"]


def test_legacy_skeleton_requires_denial_reason_and_hook_marker():
    denied = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read",
               "is_error": True, "content": "a.py: large-file first read served as a skeleton"}]}}
    hook = {"type": "attachment", "attachment": {"toolUseID": "read", "stdout": "a.py: large-file first read served as a skeleton"}}
    rows = [assistant("m0", tool("read", "Read", file_path="/a.py")), hook, denied,
            assistant("m1", tool("x", "Read", file_path="/b.py"))]
    output = inspect(rows, {"tool_use_id": hashed("read"), "arm": "legacy_skeleton"}, window=1)
    assert output["confirmed"] and output["complete"]
    assert not inspect([rows[0], denied, rows[-1]], {"tool_use_id": hashed("read"), "arm": "legacy_skeleton"}, window=1)["confirmed"]


def test_parallel_tools_in_exposure_turn_are_not_followup():
    rows = [assistant("m0", tool("t1", "Bash", command="pytest"), tool("read", "Read", file_path="/a.py")),
            result("read"), result("t1", True), assistant("m1", tool("x", "Read", file_path="/b.py"))]
    output = inspect(rows, {"tool_use_id": hashed("read"), "arm": "no_map"}, window=1)
    assert output["confirmed"] and output["complete"] and not output["failed_test"]


def test_explicit_cancellation_is_a_terminal_outcome():
    rows = [assistant("m0", tool("read", "Read", file_path="/a.py")), result("read"),
            {"type": "user", "message": {"content": "cancel this"}}]
    output = inspect(rows, {"tool_use_id": hashed("read"), "arm": "no_map"}, window=10)
    assert output["confirmed"] and output["complete"] and output["abandoned"] and output["harm"]


def test_unconfirmed_edits_do_not_become_observed_harm():
    rows = [assistant("m0", tool("read", "Read", file_path="/a.py")), result("read"),
            assistant("m1", tool("e1", "Edit", file_path="/a.py", old_string="a", new_string="b")), result("e1", True),
            assistant("m2", tool("e2", "Edit", file_path="/a.py", old_string="b", new_string="a")), result("e2", True)]
    output = inspect(rows, {"tool_use_id": hashed("read"), "arm": "no_map"}, window=2)
    assert output["confirmed"] and output["complete"] and not output["revert"] and not output["repeated_region"]


def test_matched_comparison_excludes_unknowns_and_never_reuses_controls():
    common = {"cohort": "python|16-64KB", "project": "p", "task_type": "code", "confirmed": True, "complete": True}
    rows = [dict(common, arm="map", harm=True, session_id="a", tool_use_id="1"),
            dict(common, arm="map", harm=True, session_id="a", tool_use_id="1"),
            dict(common, arm="map", harm=False, session_id="b", tool_use_id="2"),
            dict(common, arm="no_map", harm=False, session_id="c", tool_use_id="3"),
            dict(common, arm="no_map", harm=True, complete=False, session_id="d", tool_use_id="4")]
    result = compare(rows)["python|16-64KB"]
    assert result["pairs"] == 1 and result["map_harm"] == 1 and result["control_harm"] == 0
    assert len(result["map_interval_95"]) == 2 and len(result["control_interval_95"]) == 2
    assert result["difference"] == 1 and result["difference_interval_95"] == [-1.0, 1.0]


def test_each_session_and_tool_use_is_counted_at_most_once():
    base = {"cohort": "p", "project": "project", "task_type": "code", "confirmed": True, "complete": True}
    rows = [dict(base, arm="map", session_id="a", tool_use_id="1", harm=True),
            dict(base, arm="map", session_id="a", tool_use_id="1", harm=True),
            dict(base, arm="no_map", session_id="b", tool_use_id="2", harm=False),
            dict(base, arm="no_map", session_id="c", tool_use_id="3", harm=False)]
    assert compare(rows)["p"]["pairs"] == 1
    rows = [dict(base, arm="map", session_id="a", tool_use_id="1", harm=True),
            dict(base, arm="no_map", session_id="b", tool_use_id="2", harm=False),
            dict(base, arm="map", session_id="b", tool_use_id="3", harm=True),
            dict(base, arm="no_map", session_id="c", tool_use_id="4", harm=False)]
    assert compare(rows)["p"]["pairs"] == 1


def test_shadow_replay_matches_only_confirmed_complete_sessions():
    map_rows = [assistant("a0", tool("read-a", "Read", file_path="/a.py")), result("read-a"),
                {"type": "attachment", "attachment": {"toolUseID": "read-a", "stdout": "[Token Optimizer] Periphery skeletons"}},
                assistant("a1", tool("x", "Read", file_path="/b.py"))]
    control_rows = [assistant("b0", tool("read-b", "Read", file_path="/a.py")), result("read-b"),
                    assistant("b1", tool("y", "Read", file_path="/b.py"))]
    sessions = [{"session_id": "a", "project": "p", "task_type": "code", "rows": map_rows},
                {"session_id": "b", "project": "p", "task_type": "code", "rows": control_rows}]
    exposures = [{"session_id": hashed("a"), "tool_use_id": hashed("read-a"), "arm": "map", "language": "python", "band": "16-64KB"},
                 {"session_id": hashed("a"), "tool_use_id": hashed("read-a"), "arm": "map", "language": "python", "band": "16-64KB"},
                 {"session_id": hashed("b"), "tool_use_id": hashed("read-b"), "arm": "no_map", "language": "python", "band": "16-64KB"}]
    summary = analyse_sessions(sessions, exposures, window=1)
    assert summary["cohorts"]["python|16-64KB"]["pairs"] == 1
    assert summary["unique_observations"] == 2
    ambiguous = exposures + [dict(exposures[0], arm="no_map")]
    summary = analyse_sessions(sessions, ambiguous, window=1)
    assert summary["unique_observations"] == 1 and summary["cohorts"] == {}


def test_replay_cli_returns_aggregates_without_transcript_contents(tmp_path, capsys):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        assistant("a0", tool("read-a", "Read", file_path="/secret/a.py")), result("read-a"),
        assistant("a1", tool("x", "Read", file_path="/b.py"))]))
    manifest, exposures = tmp_path / "manifest.json", tmp_path / "exposures.jsonl"
    manifest.write_text(json.dumps([{"session_id": "a", "project": "private-project", "task_type": "code", "transcript_path": str(transcript)}]))
    exposures.write_text(json.dumps({"session_id": hashed("a"), "tool_use_id": hashed("read-a"), "arm": "no_map", "language": "python", "band": "16-64KB"}) + "\n")
    assert main([str(manifest), str(exposures), "--window", "1"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["unique_observations"] == 1
    assert "secret" not in output and "private-project" not in output
