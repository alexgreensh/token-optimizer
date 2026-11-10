"""Periphery attribution must contain only maps that made the output block."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
import structure_map
import read_cache
from _measure.periphery_exposure import exposure_rows, control_rows
from _measure import periphery_exposure
from test_read_cache_first_read_retarget import _run_read_cache, SESSION
import json


def test_builder_reports_only_paths_in_budget(monkeypatch):
    monkeypatch.setattr(structure_map, "summarize_code_file", lambda path: SimpleNamespace(
        replacement_text="def example(): ...", replacement_type="skeleton"))
    shown = []
    text = structure_map.build_periphery_skeleton(
        ["/a/one.py", "/b/two.py", "/c/three.py"], limit=2, shown_paths=shown)
    assert shown == ["/a/one.py", "/b/two.py"]
    assert "one.py" in text and "two.py" in text and "three.py" not in text


def test_builder_excludes_a_path_that_exceeds_budget(monkeypatch):
    monkeypatch.setattr(structure_map, "summarize_code_file", lambda path: SimpleNamespace(
        replacement_text="x" * 100, replacement_type="skeleton"))
    shown = []
    assert structure_map.build_periphery_skeleton(["/a/one.py"], budget=10, shown_paths=shown) == ""
    assert shown == []


def test_injection_arms_only_emitted_paths(monkeypatch):
    monkeypatch.setattr(structure_map, "resolve_one_hop_relationships", lambda *a, **k: ["one", "two"])
    def build(paths, shown_paths):
        shown_paths.append(paths[0])
        return "one map"
    monkeypatch.setattr(structure_map, "build_periphery_skeleton", build)
    monkeypatch.setattr(read_cache, "_additional_context_within_cap", lambda *a: True)
    monkeypatch.setattr(read_cache, "_hook_additional_context_saved", lambda: True)
    marked = []
    monkeypatch.setattr(read_cache, "_arm_periphery_markers", lambda paths, *a: marked.extend(paths))
    monkeypatch.setattr(read_cache, "_emit_pretool_response", lambda *a: None)
    recorded = []
    monkeypatch.setattr(periphery_exposure, "record_exposures", lambda *a: recorded.append(a))
    read_cache._inject_periphery_skeletons("target", "", "python", "s", object(), True, tool_use_id="call")
    assert marked == ["one"]
    assert recorded[0][2:4] == ("call", ["one"])
    marked.clear()
    def broken_emit(*args):
        raise BrokenPipeError()
    monkeypatch.setattr(read_cache, "_emit_pretool_response", broken_emit)
    read_cache._inject_periphery_skeletons("target", "", "python", "s", object(), True)
    assert marked == []


def test_exposure_ids_are_per_call_and_shown_path_only():
    a = exposure_rows("session", "tool-1", ["/private/one.py", "/private/two.py"], "python", "16-64KB")
    b = exposure_rows("session", "tool-2", ["/private/one.py"], "python", "16-64KB")
    assert len({r["exposure_id"] for r in a + b}) == 3
    assert "private" not in str(a)
    assert exposure_rows("session", "", ["/private/one.py"], "python", "16-64KB") == []


def test_no_map_control_is_distinct_and_not_a_confirmed_read():
    control = control_rows("session", "tool-1", "/private/one.py", "python", "16-64KB")
    mapped = exposure_rows("session", "tool-1", ["/private/one.py"], "python", "16-64KB")
    assert control[0]["arm"] == "no_map" and control[0]["read_confirmed"] is False
    assert control[0]["exposure_id"] != mapped[0]["exposure_id"]
    assert "private" not in str(control)


def test_shadow_read_records_unconfirmed_no_map_control(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"items": [{"id": i, "payload": "x" * 200} for i in range(200)]}, indent=2))
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(path)},
               "session_id": SESSION, "tool_use_id": "call-1"}
    result = _run_read_cache(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in (tmp_path / "diagnostics/periphery_exposures.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["arm"] == "no_map"
    assert rows[0]["read_confirmed"] is False and "data.json" not in str(rows)
