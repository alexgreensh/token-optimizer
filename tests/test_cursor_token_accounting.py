"""U4: afterAgentResponse real-token accounting + record-only event wiring.

Provenance rules under test (contract v3, KTD3/R7/R13/R14/R15):

- afterAgentResponse token fields are ingested only when present and plausible
  (non-negative ints), and are flagged ``cursor_hook_measured``; they never
  displace the vscdb source and never fabricate values when absent.
- postToolUseFailure / afterShellExecution / subagentStart / subagentStop are
  record-only: no stdout emissions, no tool-call double counting.
- Every handler fails open on malformed payloads.

All fixtures are synthetic and built under tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_hook_bridge as bridge  # noqa: E402


@pytest.fixture()
def cursor_dir(monkeypatch, tmp_path):
    cur = tmp_path / ".cursor"
    cur.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)
    return cur


def _tally(cursor_dir, sid="conv-1"):
    p = cursor_dir / "token-optimizer" / "sessions" / f"{sid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _observed(cursor_dir):
    p = cursor_dir / "token-optimizer" / "observed-events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _resp_payload(**kw):
    base = {
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "model": "claude-opus",
        "cursor_version": "3.23.23",
        "text": "done",
    }
    base.update(kw)
    return base


def test_after_agent_response_ingests_plausible_tokens(cursor_dir, capsys):
    bridge.handle_after_agent_response(_resp_payload(
        input_tokens=1200, output_tokens=300,
        cache_read_tokens=5000, cache_write_tokens=800,
    ))
    bridge.handle_after_agent_response(_resp_payload(
        generation_id="gen-2", input_tokens=1500, output_tokens=100,
    ))
    assert capsys.readouterr().out == ""
    tally = _tally(cursor_dir)
    hm = tally["hook_measured_tokens"]
    assert hm["input"] == 2700
    assert hm["output"] == 400
    assert hm["cache_read"] == 5000
    assert hm["cache_write"] == 800
    assert hm["responses"] == 2
    assert hm["source"] == "cursor_hook_measured"
    events = [e for e in _observed(cursor_dir) if e["event"] == "afterAgentResponse"]
    assert len(events) == 2
    assert events[0].get("tokens") == "measured"


def test_after_agent_response_without_fields_ingests_nothing(cursor_dir, capsys):
    bridge.handle_after_agent_response(_resp_payload())
    assert capsys.readouterr().out == ""
    assert _tally(cursor_dir) is None or "hook_measured_tokens" not in _tally(cursor_dir)
    events = [e for e in _observed(cursor_dir) if e["event"] == "afterAgentResponse"]
    assert events and events[0].get("tokens") == "absent"


def test_after_agent_response_rejects_nonintegral_floats(cursor_dir, capsys):
    """G5: 1.5 must not silently truncate to 1 - a fractional token count is
    not a plausible observation and is rejected, not rounded."""
    bridge.handle_after_agent_response(_resp_payload(input_tokens=1.5, output_tokens=10))
    assert capsys.readouterr().out == ""
    tally = _tally(cursor_dir)
    assert tally is None or "hook_measured_tokens" not in tally
    events = [e for e in _observed(cursor_dir) if e["event"] == "afterAgentResponse"]
    assert events and events[0].get("tokens") == "rejected"
    # Integral floats (1200.0) remain plausible - JSON encoders emit them.
    bridge.handle_after_agent_response(_resp_payload(input_tokens=1200.0, output_tokens=10))
    events = [e for e in _observed(cursor_dir) if e["event"] == "afterAgentResponse"]
    assert events[-1].get("tokens") == "measured"


def test_after_agent_response_rejects_implausible_values(cursor_dir, capsys):
    bridge.handle_after_agent_response(_resp_payload(input_tokens=-5, output_tokens=10))
    bridge.handle_after_agent_response(_resp_payload(input_tokens="lots"))
    bridge.handle_after_agent_response(_resp_payload(input_tokens=True))
    assert capsys.readouterr().out == ""
    tally = _tally(cursor_dir)
    assert tally is None or "hook_measured_tokens" not in tally
    events = [e for e in _observed(cursor_dir) if e["event"] == "afterAgentResponse"]
    assert all(e.get("tokens") == "rejected" for e in events)


def test_hook_measured_quarantined_from_resolution():
    """afterAgentResponse usage numbers are stored for offline correlation
    only: per-response vs cumulative semantics are unproven, so resolution
    must never consume them - even when they are the only positive source."""
    sys.path.insert(0, str(SCRIPTS))
    import cursor_session

    measured = {"input": 2700, "output": 400, "cache_read": 5000, "cache_write": 800}
    # vscdb still wins
    src = cursor_session.resolve_token_source(
        {"input_tokens": 10, "output_tokens": 5}, 99999, hook_measured=measured)
    assert src[2] == "cursor_state_vscdb"
    # hook-measured values do NOT beat the transcript estimate (quarantine)
    src = cursor_session.resolve_token_source(None, 99999, hook_measured=measured)
    assert src[2] == "cursor_transcript_estimate"
    assert src[3] is True
    # and with no transcript estimate they do not mint trusted tokens either
    src = cursor_session.resolve_token_source(None, None, hook_measured=measured)
    assert src[2] == "cursor_tally_only"
    assert src[0] == 0 and src[1] == 0
    # absent measured falls through unchanged
    src = cursor_session.resolve_token_source(None, 99999, hook_measured=None)
    assert src[2] == "cursor_transcript_estimate"


def test_post_tool_use_failure_counts_by_tool(cursor_dir, capsys):
    bridge.handle_post_tool_use_failure({
        "conversation_id": "conv-1", "tool_name": "Shell",
        "tool_input": {"command": "make build"}, "error": "exit 1",
    })
    bridge.handle_post_tool_use_failure({
        "conversation_id": "conv-1", "tool_name": "Shell",
        "tool_input": {"command": "make test"}, "error": "exit 2",
    })
    assert capsys.readouterr().out == ""
    tally = _tally(cursor_dir)
    assert tally["failures"]["Shell"] == 2
    # failures are not successful tool calls
    assert tally.get("tool_calls", 0) == 0


def test_after_shell_execution_never_double_counts(cursor_dir, capsys):
    bridge.handle_post_tool_use({
        "conversation_id": "conv-1", "tool_name": "Shell",
        "tool_input": {"command": "ls -la"}, "tool_output": "total 0",
        "tool_use_id": "tu-1",
    })
    bridge.handle_after_shell_execution({
        "conversation_id": "conv-1", "command": "ls -la", "output": "total 0",
        "tool_use_id": "tu-1",
    })
    assert capsys.readouterr().out == ""
    tally = _tally(cursor_dir)
    assert tally["tool_calls"] == 1
    events = [e for e in _observed(cursor_dir) if e["event"] == "afterShellExecution"]
    assert events and events[0].get("tool_use_id") == "tu-1"


def test_subagent_events_recorded(cursor_dir, capsys):
    bridge.handle_subagent_start({
        "conversation_id": "conv-1", "tool_use_id": "tu-9",
        "subagent_type": "delegate_task",
    })
    bridge.handle_subagent_stop({
        "conversation_id": "conv-1", "tool_use_id": "tu-9",
        "subagent_type": "delegate_task",
    })
    assert capsys.readouterr().out == ""
    events = _observed(cursor_dir)
    kinds = [e["event"] for e in events]
    assert "subagentStart" in kinds and "subagentStop" in kinds
    tally = _tally(cursor_dir)
    assert tally.get("subagents", 0) >= 1


def test_new_handlers_fail_open_on_garbage(cursor_dir, capsys):
    for handler in (
        bridge.handle_after_agent_response,
        bridge.handle_post_tool_use_failure,
        bridge.handle_after_shell_execution,
        bridge.handle_subagent_start,
        bridge.handle_subagent_stop,
    ):
        handler(None)
        handler("not a dict")
        handler({"tool_input": 12345})
    assert capsys.readouterr().out == ""
