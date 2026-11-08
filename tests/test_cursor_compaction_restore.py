#!/usr/bin/env python3
"""U3: Cursor compaction capture + post-compact restore.

Contracts guarded here:
1. Happy path: preCompact with a transcript captures a redacted checkpoint
   (through the U1 adapter + compact_capture's trusted path), marks the
   tally restore-pending, and emits the probe-gated user_message note;
   the next postToolUse emits exactly ONE bounded additional_context
   restore and clears the flag.
2. Consecutive compactions without a tool call coalesce to one restore.
3. Capture failure (no transcript_path, unconfinable path, capture raising)
   leaves today's numbers-only recording and never raises into Cursor.
4. Credentials in the transcript never reach the checkpoint or the restore
   payload.
5. Observed-events order: compactCapture is ledgered at preCompact,
   compactRestore at the restoring postToolUse.

All fixtures are built under tmp_path; no network, no writes outside
tmp_path, and checkpoint state is redirected away from any real home.

Run: python3 -m pytest tests/test_cursor_compaction_restore.py -v
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_hook_bridge as bridge  # noqa: E402

SECRET = "hunter2-compact-secret"


@pytest.fixture()
def env(monkeypatch, tmp_path, capsys):
    """Cursor home under tmp + measure redirected to tmp checkpoint dirs."""
    cur = tmp_path / ".cursor"
    cur.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bridge, "cursor_home", lambda: cur)

    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-cfg"))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    measure = importlib.import_module("measure")
    importlib.reload(measure)
    # Checkpoints live under the confined data dir, matching production
    # (SNAPSHOT_DIR is pinned to <cursor_home>/token-optimizer): the G1
    # confinement in _read_checkpoint_bounded refuses anything outside it.
    cp_dir = cur / "token-optimizer" / "snapshot" / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("CHECKPOINT_DIR", cp_dir),
        ("CLAUDE_DIR", tmp_path / "claude-cfg"),
        ("QUALITY_CACHE_DIR", tmp_path / "qc"),
        ("SNAPSHOT_DIR", tmp_path / "snap"),
        ("TRENDS_DB", tmp_path / "snap" / "trends.db"),
    ):
        monkeypatch.setattr(measure, name, value, raising=True)
    monkeypatch.setattr(measure, "_capture_git_state", lambda cwd=None: (None, None), raising=True)

    yield cur, cp_dir, capsys

    if "measure" in sys.modules:
        del sys.modules["measure"]


def _write_transcript(cur: Path, conv: str = "conv-1") -> Path:
    tdir = cur / "projects" / "-repo" / "agent-transcripts" / conv
    tdir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": f"debug the deploy with password={SECRET}"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "I decided to rerun the migration first."},
            {"type": "tool_use", "name": "Shell", "input": {"command": "make migrate"}},
        ]}},
        {"type": "turn_ended", "status": "success"},
    ]
    path = tdir / "transcript.jsonl"
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    return path


def _precompact_payload(cur: Path, sid: str = "conv-1") -> dict:
    return {
        "hook_event_name": "preCompact",
        "conversation_id": sid,
        "trigger": "auto",
        "context_tokens": 190_000,
        "context_window_size": 200_000,
        "context_usage_percent": 95.0,
        "transcript_path": str(_write_transcript(cur, sid)),
        "workspace_roots": ["/repo/demo"],
    }


def _tool_payload(sid: str = "conv-1") -> dict:
    return {
        "hook_event_name": "postToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/repo/demo/a.py"},
        "conversation_id": sid,
        "workspace_roots": ["/repo/demo"],
    }


def _tally(cur: Path, sid: str = "conv-1") -> dict:
    return json.loads(
        (cur / "token-optimizer" / "sessions" / f"{sid}.json").read_text(encoding="utf-8")
    )


def _emitted(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]


class TestCapture:
    def test_precompact_captures_checkpoint_and_marks_pending(self, env):
        cur, cp_dir, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        tally = _tally(cur)
        assert tally.get("restore_pending") is True
        cp = tally.get("compact_checkpoint")
        assert cp and Path(cp).is_file()
        assert str(cp_dir) in cp, "checkpoint must live in the redirected dir"
        body = Path(cp).read_text(encoding="utf-8")
        assert SECRET not in body, "credential leaked into checkpoint"
        assert "migration" in body, "decision content missing from checkpoint"
        # numbers-only recording still happened alongside the capture
        assert tally["compactions"][-1]["context_tokens"] == 190_000
        # probe-gated note emitted, claimed only as emitted
        assert any("checkpoint captured" in e.get("user_message", "")
                   for e in _emitted(capsys))

    def test_observed_order(self, env):
        cur, _, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        bridge.handle_post_tool_use(_tool_payload())
        ledger = (cur / "token-optimizer" / "observed-events.jsonl").read_text(
            encoding="utf-8").splitlines()
        events = [json.loads(l)["event"] for l in ledger]
        assert events.index("compactCapture") < events.index("compactRestore")


class TestRestore:
    def test_next_tool_use_restores_exactly_once(self, env):
        cur, _, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        capsys.readouterr()
        bridge.handle_post_tool_use(_tool_payload())
        first = _emitted(capsys)
        restores = [e for e in first if "checkpoint captured before compaction"
                    in e.get("additional_context", "")]
        assert len(restores) == 1
        assert _tally(cur).get("restore_pending") is False
        assert SECRET not in json.dumps(first)
        # a second tool call restores nothing
        bridge.handle_post_tool_use(_tool_payload())
        second = _emitted(capsys)
        assert not any("checkpoint captured before compaction"
                       in e.get("additional_context", "") for e in second)

    def test_consecutive_compactions_coalesce(self, env):
        cur, _, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        bridge.handle_pre_compact(_precompact_payload(cur))
        capsys.readouterr()
        bridge.handle_post_tool_use(_tool_payload())
        restores = [e for e in _emitted(capsys) if "checkpoint captured before compaction"
                    in e.get("additional_context", "")]
        assert len(restores) == 1

    def test_restore_payload_bounded(self, env, monkeypatch):
        cur, cp_dir, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        cp = Path(_tally(cur)["compact_checkpoint"])
        cp.write_text("x" * 50_000, encoding="utf-8")
        capsys.readouterr()
        bridge.handle_post_tool_use(_tool_payload())
        emitted = _emitted(capsys)
        restores = [e["additional_context"] for e in emitted
                    if "checkpoint captured before compaction"
                    in e.get("additional_context", "")]
        # 50KB file exceeds the read cap: no restore at all (never truncate-
        # blind a huge file into the rebuilt context)
        assert restores == []

    def test_restore_budget_bounds_multibyte_checkpoint_in_utf8_bytes(self, env):
        # G3 P2: a character cut let multibyte checkpoints inflate the restore
        # inject past the 8KB contract (repro: 10048 bytes). The budget bounds
        # the whole emitted string - provenance prefix + body + marker - in
        # UTF-8 bytes.
        cur, cp_dir, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        cp = Path(_tally(cur)["compact_checkpoint"])
        # ~12KB of 3-byte UTF-8 content: inside the 16KB read cap, outside the
        # 8KB restore budget.
        cp.write_text("\u20ac" * 4000, encoding="utf-8")
        capsys.readouterr()
        bridge.handle_post_tool_use(_tool_payload())
        restores = [e["additional_context"] for e in _emitted(capsys)
                    if "checkpoint captured before compaction"
                    in e.get("additional_context", "")]
        assert len(restores) == 1
        emitted = restores[0]
        assert len(emitted.encode("utf-8")) <= 8000
        assert emitted.endswith("[truncated: checkpoint exceeds restore budget]")
        assert not emit_contains_replacement_char(emitted)

    def test_restore_budget_unchanged_for_ascii_checkpoint(self, env):
        cur, cp_dir, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        cp = Path(_tally(cur)["compact_checkpoint"])
        cp.write_text("y" * 9000, encoding="utf-8")
        capsys.readouterr()
        bridge.handle_post_tool_use(_tool_payload())
        restores = [e["additional_context"] for e in _emitted(capsys)
                    if "checkpoint captured before compaction"
                    in e.get("additional_context", "")]
        assert len(restores) == 1
        assert len(restores[0].encode("utf-8")) <= 8000
        assert restores[0].endswith("[truncated: checkpoint exceeds restore budget]")


def emit_contains_replacement_char(text):
    return "\ufffd" in text


class TestFailureModes:
    def test_no_transcript_path_records_numbers_only(self, env):
        cur, _, capsys = env
        payload = _precompact_payload(cur)
        payload.pop("transcript_path")
        bridge.handle_pre_compact(payload)
        tally = _tally(cur)
        assert tally["compactions"][-1]["context_tokens"] == 190_000
        assert not tally.get("restore_pending")
        assert not any("user_message" in e for e in _emitted(capsys))

    def test_unconfinable_transcript_path_is_refused(self, env, tmp_path):
        cur, _, capsys = env
        outside = tmp_path / "elsewhere.jsonl"
        outside.write_text('{"role":"user","message":{"content":[]}}\n')
        payload = _precompact_payload(cur)
        payload["transcript_path"] = str(outside)
        bridge.handle_pre_compact(payload)
        assert not _tally(cur).get("restore_pending")

    def test_capture_exception_degrades_silently(self, env, monkeypatch):
        cur, _, _ = env
        monkeypatch.setattr(bridge, "_capture_compact_checkpoint",
                            lambda fields: (_ for _ in ()).throw(RuntimeError("boom")))
        # the handler itself must still not raise into Cursor
        try:
            bridge.handle_pre_compact(_precompact_payload(cur))
        except RuntimeError:
            # a raising helper would propagate; the contract is fail-open,
            # so if it raises here the handler needs its own guard
            pytest.fail("capture exception escaped handle_pre_compact")

    def test_missing_checkpoint_file_clears_pending_without_emit(self, env):
        cur, _, capsys = env
        bridge.handle_pre_compact(_precompact_payload(cur))
        Path(_tally(cur)["compact_checkpoint"]).unlink()
        capsys.readouterr()
        bridge.handle_post_tool_use(_tool_payload())
        assert _tally(cur).get("restore_pending") is False
        assert not any("checkpoint captured before compaction"
                       in e.get("additional_context", "") for e in _emitted(capsys))
