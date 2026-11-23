"""Tests for SPEC-local-adapt Part C: independent replay critical vectors."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import replay_critical_vectors as rcv  # noqa: E402

FORBIDDEN_IMPORTS = {
    "emit_safety_gate",
    "visibility_rule",
    "large_output_policy",
    "bash_compress",
    "bash_compress_hook",
    "build_output_compress",
    "pipe_file_compress",
    "builtin_tool_compress",
    "command_compress_extra",
}


def test_import_graph_is_independent_of_compressors():
    src = (REPO / "scripts" / "replay_critical_vectors.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & FORBIDDEN_IMPORTS), f"Forbidden imports: {imported & FORBIDDEN_IMPORTS}"


def test_seeded_fault_self_test_50_of_50():
    res = rcv.seeded_fault_self_test(seed=42, count=50)
    assert res["passed"] is True
    assert res["detected"] == 50
    assert res["total"] == 50


def test_evidence_hidden_flagged_when_context_lines_dropped():
    raw = "\n".join([
        "line 1 ok",
        "ctx before 2",
        "ctx before 1",
        "FAILED tests/test_math.py::test_add",
        "  Expected: 42",
        "  Received: 41",
        "line 7 ok",
    ])
    # Keeps FAILED, Expected, Received, but drops ctx before 1 and ctx before 2
    emitted = "\n".join([
        "line 1 ok",
        "... 2 lines omitted ...",
        "FAILED tests/test_math.py::test_add",
        "  Expected: 42",
        "  Received: 41",
        "line 7 ok",
    ])
    res = rcv.check_event(raw, emitted, exit_code=1)
    assert res["EVIDENCE_HIDDEN"] > 0


def test_dedupe_replay_rows_by_message_and_tool_id():
    rows = [
        {"timestamp": "2026-11-10T01:00:00Z", "message_id": "m1", "tool_use_id": "t1"},
        {"timestamp": "2026-11-10T01:00:00Z", "message_id": "m1", "tool_use_id": "t1"},
        {"timestamp": "2026-09-01T01:00:00Z", "message_id": "m_old", "tool_use_id": "t_old"},
        {"timestamp": "2026-11-12T01:00:00Z", "message_id": "m2", "tool_use_id": "t2"},
    ]
    kept = rcv.dedupe_replay_rows(rows, cutoff_iso="2026-10-25T00:00:00Z")
    assert [r["message_id"] for r in kept] == ["m1", "m2"]
