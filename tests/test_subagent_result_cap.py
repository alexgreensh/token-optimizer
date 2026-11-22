"""Tests for oversized subagent (Task/Agent) result capping (subagent_result_cap.py)."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    snap = tmp_path / "snap"
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "subagent-cap-session")
    monkeypatch.delenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    names = ("subagent_result_cap", "archive_result", "archive_verify", "session_store")
    saved = {n: sys.modules[n] for n in names if n in sys.modules}
    for n in names:
        sys.modules.pop(n, None)
    cap_mod = importlib.import_module("subagent_result_cap")
    arch_mod = importlib.import_module("archive_result")
    yield cap_mod, arch_mod, snap
    for n in names:
        sys.modules.pop(n, None)
    sys.modules.update(saved)


def _realistic_subagent_report() -> str:
    head = [
        "# Codebase Investigation Report",
        "",
        "## Executive Summary",
        "Investigated the checkout latency regression across 18 service modules.",
        "Primary bottleneck is repeated schema validation inside the hot request loop.",
        "",
        "## Detailed File-by-File Notes",
    ]
    middle = []
    for i in range(180):
        middle.append(
            f"- Reviewed module worker_{i:03d}: checked request path, connection pooling, "
            f"and retry jitter settings; everything looks nominal under load."
        )
        if i == 42:
            middle.append("  ERROR: Timeout waiting for redis lock in checkout pipeline")
        if i == 90:
            middle.append("  See src/checkout/validator.py:148 for the redundant schema compile")
        if i == 120:
            middle.append("  TODO: cache compiled validator per worker process")
        if i == 150:
            middle.append("  FAILED assertion in tests/test_checkout.py:88 under burst traffic")
    tail = [
        "",
        "## Conclusions",
        "1. Memoize the JSON schema compiler in validator.py.",
        "2. Add a regression test for burst checkout latency.",
        "3. Re-run the full checkout test suite after the patch.",
    ]
    return "\n".join(head + middle + tail) + "\n"


def test_caps_huge_subagent_result_preserving_head_conclusions_and_key_lines(env):
    cap_mod, _, _ = env
    raw = _realistic_subagent_report()
    assert len(raw) > 12 * 1024

    capped = cap_mod.cap_subagent_result(
        raw, expand_instruction="Run: expand subagent_key_1", original_chars=len(raw)
    )
    assert capped is not None
    # Opening section preserved
    assert "# Codebase Investigation Report" in capped
    assert "Primary bottleneck is repeated schema validation" in capped
    # Every error/fail/TODO/file:line line from the middle preserved
    assert "ERROR: Timeout waiting for redis lock in checkout pipeline" in capped
    assert "src/checkout/validator.py:148" in capped
    assert "TODO: cache compiled validator per worker process" in capped
    assert "FAILED assertion in tests/test_checkout.py:88" in capped
    # Conclusions preserved
    assert "## Conclusions" in capped
    assert "Memoize the JSON schema compiler" in capped
    # Expand pointer present
    assert "expand subagent_key_1" in capped
    # Strong token reduction
    assert len(capped) < len(raw) * 0.40


def test_under_12kb_result_passes_through_untouched(env):
    cap_mod, _, _ = env
    small = "Summary line\n" + ("normal finding line with details\n" * 120)
    assert len(small) < 12 * 1024
    assert cap_mod.cap_subagent_result(small) is None


def test_shape_preservation_for_str_and_dict_responses(env):
    cap_mod, _, _ = env
    assert cap_mod.build_updated_tool_output("raw str", "capped") == "capped"

    cc_agent_dict = {
        "status": "completed",
        "agentId": "agent_abc123",
        "totalDurationMs": 4200,
        "content": [{"type": "text", "text": "huge text"}],
    }
    updated = cap_mod.build_updated_tool_output(cc_agent_dict, "capped text")
    assert updated["status"] == "completed"
    assert updated["agentId"] == "agent_abc123"
    assert updated["content"] == [{"type": "text", "text": "capped text"}]
    # Original dict was not mutated
    assert cc_agent_dict["content"][0]["text"] == "huge text"

    # Unrecognized dict shape fails open (returns None)
    assert cap_mod.build_updated_tool_output({"unknown_field": 123}, "capped") is None


def test_archive_result_end_to_end_emits_updated_tool_output_for_agent(env, monkeypatch, capsys):
    _, arch_mod, snap = env
    raw = _realistic_subagent_report()
    payload = {
        "session_id": "subagent-cap-session",
        "tool_name": "Agent",
        "tool_use_id": "toolu_agent_001",
        "tool_input": {"description": "investigate checkout latency"},
        "tool_response": {
            "status": "completed",
            "agentId": "agent_001",
            "content": [{"type": "text", "text": raw}],
        },
    }
    monkeypatch.setattr(arch_mod, "read_stdin_hook_input", lambda max_bytes=None: payload)

    arch_mod.archive_result(quiet=True)
    stdout = capsys.readouterr().out.strip()
    assert stdout, "Expected updatedToolOutput JSON on stdout for >12KB Agent result"
    envelope = json.loads(stdout)
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "updatedToolOutput" in hso
    updated = hso["updatedToolOutput"]
    assert updated["agentId"] == "agent_001"
    capped_text = updated["content"][0]["text"]
    assert "src/checkout/validator.py:148" in capped_text
    assert "ERROR: Timeout waiting for redis lock" in capped_text
    assert "TODO: cache compiled validator" in capped_text
    assert "## Conclusions" in capped_text
    assert "expand " in capped_text
    assert len(capped_text) < len(raw) * 0.45

    # Full raw output is archived on disk
    assert arch_mod.archive_entry_exists("subagent-cap-session", "toolu_agent_001")

    # Measured savings logged in trends.db
    conn = sqlite3.connect(str(snap / "trends.db"))
    try:
        row = conn.execute(
            "SELECT event_type, tokens_saved, detail FROM savings_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "tool_archive"
    assert row[1] > 0
    assert "capped Agent" in row[2]


def test_respects_no_updated_tool_output_env(env, monkeypatch, capsys):
    _, arch_mod, _ = env
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT", "1")
    raw = _realistic_subagent_report()
    payload = {
        "session_id": "subagent-cap-session",
        "tool_name": "Agent",
        "tool_use_id": "toolu_agent_002",
        "tool_input": {"description": "investigate"},
        "tool_response": raw,
    }
    monkeypatch.setattr(arch_mod, "read_stdin_hook_input", lambda max_bytes=None: payload)
    arch_mod.archive_result(quiet=True)
    assert capsys.readouterr().out.strip() == ""


def test_fails_open_when_archive_missing(env):
    cap_mod, _, _ = env
    raw = _realistic_subagent_report()
    emitted = cap_mod.emit_if_capped(
        tool_name="Agent",
        raw_response=raw,
        safe_response=raw,
        original_char_count=len(raw),
        tool_use_id="toolu_missing",
        raw_tool_use_id="toolu_missing",
        session_id="subagent-cap-session",
        entry_exists=False,
        expand_instruction="expand toolu_missing",
        chars_per_token=3.3,
        trends_db=None,
        transcript_path=None,
        log_savings_fn=lambda *a, **kw: None,
    )
    assert emitted is False


def test_benchmark_safety_assertions_on_subagent_cap(env):
    cap_mod, _, _ = env
    import benchmark

    raw = _realistic_subagent_report()
    fixture = {
        "name": "subagent_report_cap",
        "category": "subagent_result_cap",
        "command": "Agent",
        "raw_output": raw,
        "must_preserve": [
            "# Codebase Investigation Report",
            "ERROR: Timeout waiting for redis lock in checkout pipeline",
            "src/checkout/validator.py:148",
            "TODO: cache compiled validator per worker process",
            "FAILED assertion in tests/test_checkout.py:88",
            "## Conclusions",
            "expand subagent_bench",
        ],
        "must_not_contain": [
            "Reviewed module worker_050",
            "Reviewed module worker_100",
        ],
        "min_compression": 0.60,
    }
    passed, result = benchmark.run_single_fixture(
        fixture,
        compressor=lambda _cmd, text: cap_mod.cap_subagent_result(
            text, expand_instruction="Run: expand subagent_bench"
        )
        or text,
    )
    assert passed, f"Subagent cap benchmark failed: {result}"
