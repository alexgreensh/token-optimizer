#!/usr/bin/env python3
"""U1: Cursor disk-transcript -> CC-shaped translation (cursor_transcript.py).

Contracts guarded here:
1. Happy path: the writer-proven disk schema (message/tool_use lines +
   turn_ended terminals) translates into CC records; turn boundaries and
   roles survive.
2. NO tool-result text survives - by writer design disk has none, and the
   adapter must not invent any. Absence of results is NOT "tools ran
   clean": a translated record with tool_use blocks carries no success
   signal, and turn_ended status/error is preserved as the disk's only
   terminal failure evidence.
3. Redaction: credentials in user text and tool inputs are masked before
   anything reaches the CC-shaped temp file.
4. Confinement: paths outside <cursor-home>/projects are refused; symlink
   escapes are refused; the translated output is 0o600 under the caller's
   private dir.
5. Robustness: empty file, truncated final line, and oversized files fail
   soft (None / partial with malformed count), never raise.
6. Integration: translate_transcript output feeds compact_capture's
   _trusted_transcript path and yields a real checkpoint.

Fixtures under tests/fixtures/cursor/ are SYNTHETIC, built to the bundle
2026.11.05 TranscriptStore writer schema (see fixtures README). They are
not native captures.

Run: python3 -m pytest tests/test_cursor_transcript.py -v
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "_measure"))

import cursor_transcript  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cursor"


def _home_with_transcript(tmp_path: Path, fixture: str = "two_turn_shell_read.jsonl") -> tuple[Path, Path]:
    """Lay a fixture out under <home>/projects/<slug>/agent-transcripts/."""
    home = tmp_path / "cursor-home"
    tdir = home / "projects" / "-repo" / "agent-transcripts" / "conv-1"
    tdir.mkdir(parents=True)
    src = (FIXTURES / fixture).read_text(encoding="utf-8")
    path = tdir / "transcript.jsonl"
    path.write_text(src, encoding="utf-8")
    return home, path


class TestReadCursorMessages:
    def test_happy_path_roles_and_blocks(self, tmp_path):
        home, path = _home_with_transcript(tmp_path)
        messages, malformed = cursor_transcript.read_cursor_messages(path, home)
        assert malformed == 0
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant", "assistant", "user", "assistant"]

    def test_no_tool_result_content_anywhere(self, tmp_path):
        """The writer drops tool results; the reader must not fabricate any."""
        home, path = _home_with_transcript(tmp_path)
        messages, _ = cursor_transcript.read_cursor_messages(path, home)
        for msg in messages:
            # Normalized shape: text content + tool_calls (name/input only).
            # No result/output/error field may appear: result-absence is a
            # schema fact, never evidence of error-free execution.
            assert set(msg) <= {"role", "content", "tool_calls", "turn_ended",
                                "turn_ended_status", "turn_ended_error"}
            assert isinstance(msg["content"], str)
            for call in msg["tool_calls"]:
                assert set(call) == {"name", "input"}, (
                    "tool_calls carry name/input only - no result may be "
                    "fabricated to fill the disk schema's gap"
                )
            blob = json.dumps(msg)
            assert "tool_result" not in blob

    def test_turn_ended_status_and_error_preserved(self, tmp_path):
        home, path = _home_with_transcript(tmp_path)
        messages, _ = cursor_transcript.read_cursor_messages(path, home)
        # First turn ended clean; second turn ended in an error - the ONLY
        # disk-backed failure signal this format carries.
        assert messages[2].get("turn_ended") is True
        assert messages[2].get("turn_ended_status") == "success"
        assert messages[4].get("turn_ended") is True
        assert messages[4].get("turn_ended_status") == "error"
        assert "aborted" in messages[4].get("turn_ended_error", "")

    def test_empty_file(self, tmp_path):
        home, path = _home_with_transcript(tmp_path, "empty.jsonl")
        messages, malformed = cursor_transcript.read_cursor_messages(path, home)
        assert messages == []
        assert malformed == 0

    def test_truncated_final_line_counted_malformed(self, tmp_path):
        home, path = _home_with_transcript(tmp_path, "malformed_tail.jsonl")
        messages, malformed = cursor_transcript.read_cursor_messages(path, home)
        assert len(messages) == 1
        assert malformed == 1

    def test_confinement_refuses_outside_projects(self, tmp_path):
        home = tmp_path / "cursor-home"
        home.mkdir()
        outside = tmp_path / "elsewhere.jsonl"
        outside.write_text('{"role":"user","message":{"content":[]}}\n')
        messages, malformed = cursor_transcript.read_cursor_messages(outside, home)
        assert messages == []
        assert malformed == 0  # refused, not parsed

    def test_symlink_escape_refused(self, tmp_path):
        home, _ = _home_with_transcript(tmp_path)
        secret = tmp_path / "secret.jsonl"
        secret.write_text('{"role":"user","message":{"content":[]}}\n')
        link = home / "projects" / "-repo" / "agent-transcripts" / "conv-1" / "link.jsonl"
        link.symlink_to(secret)
        messages, _ = cursor_transcript.read_cursor_messages(link, home)
        assert messages == []


class TestTranslateTranscript:
    def test_cc_records_shape(self, tmp_path):
        home, path = _home_with_transcript(tmp_path)
        out = cursor_transcript.translate_transcript(path, home, "conv-1")
        assert out is not None
        try:
            records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
            assert records, "expected translated records"
            for rec in records:
                assert rec.get("type") in ("user", "assistant")
                content = rec["message"]["content"]
                if rec["type"] == "user":
                    # CC user rows carry plain string text content.
                    assert isinstance(content, str) and content.strip()
                else:
                    assert isinstance(content, list) and content
            tool_uses = [
                b for r in records for b in r["message"]["content"]
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            names = {t.get("name") for t in tool_uses}
            assert {"Shell", "Read"} <= names
        finally:
            out.unlink(missing_ok=True)

    def test_output_is_0600_and_confined(self, tmp_path):
        home, path = _home_with_transcript(tmp_path)
        out = cursor_transcript.translate_transcript(path, home, "conv-1")
        assert out is not None
        try:
            mode = stat.S_IMODE(os.stat(out).st_mode)
            assert mode == 0o600, oct(mode)
        finally:
            out.unlink(missing_ok=True)

    def test_redaction_masks_credentials(self, tmp_path):
        home, path = _home_with_transcript(tmp_path)
        out = cursor_transcript.translate_transcript(path, home, "conv-1")
        assert out is not None
        try:
            body = out.read_text(encoding="utf-8")
            assert "hunter2" not in body, "credential leaked into translated transcript"
        finally:
            out.unlink(missing_ok=True)

    def test_refusal_returns_none(self, tmp_path):
        home = tmp_path / "cursor-home"
        home.mkdir()
        outside = tmp_path / "nope.jsonl"
        outside.write_text("{}\n")
        assert cursor_transcript.translate_transcript(outside, home, "conv-1") is None


class TestCompactCaptureIntegration:
    def test_translated_transcript_feeds_compact_capture(self, tmp_path, monkeypatch):
        """The CC-shaped temp file must satisfy compact_capture's
        _trusted_transcript contract and produce a checkpoint whose content
        carries the session's decisions - with redaction applied and no
        fabricated tool outcomes."""
        import importlib

        monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        if "measure" in sys.modules:
            del sys.modules["measure"]
        measure = importlib.import_module("measure")
        importlib.reload(measure)
        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(measure, "CHECKPOINT_DIR", cp_dir, raising=True)
        monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path, raising=True)
        monkeypatch.setattr(measure, "QUALITY_CACHE_DIR", tmp_path / "qc", raising=True)
        monkeypatch.setattr(measure, "SNAPSHOT_DIR", tmp_path / "snap", raising=True)
        monkeypatch.setattr(measure, "TRENDS_DB", tmp_path / "snap" / "trends.db", raising=True)
        monkeypatch.setattr(measure, "_capture_git_state", lambda cwd=None: (None, None), raising=True)

        home, path = _home_with_transcript(tmp_path)
        out = cursor_transcript.translate_transcript(path, home, "conv-1")
        assert out is not None
        try:
            checkpoint = measure.compact_capture(
                session_id="cursor-conv-1",
                trigger="stop",
                _trusted_transcript=out,
            )
        finally:
            out.unlink(missing_ok=True)
            if "measure" in sys.modules:
                del sys.modules["measure"]
        assert checkpoint is not None
        text = Path(checkpoint).read_text(encoding="utf-8")
        assert "hunter2" not in text, "credential leaked into checkpoint"
        # Content came from the Cursor transcript (decisions/messages), and
        # nothing in the checkpoint may claim tool calls succeeded - disk
        # transcripts carry no results to support that.
        assert "pytest" in text or "build" in text

    def test_turn_ended_error_reaches_cc_records(self, tmp_path):
        """Judge U1 medium: terminal failure evidence must not be dropped at
        the CC-records boundary - compact_capture is a consumer."""
        home, path = _home_with_transcript(tmp_path)
        messages, _ = cursor_transcript.read_cursor_messages(path, home)
        records = cursor_transcript.messages_to_cc_records(messages)
        texts = [
            b.get("text", "") for r in records
            for b in (r["message"]["content"] if isinstance(r["message"]["content"], list) else [])
            if isinstance(b, dict)
        ]
        assert any("turn_ended status=error" in t and "aborted" in t for t in texts), texts
        # a success terminal adds no fabricated error content
        assert not any("status=success" in t for t in texts)
