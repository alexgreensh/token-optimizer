"""Regression tests: microcompact and clear_tool_uses invalidate read_cache.

When Claude Code microcompacts older tool results (microcompact_boundary /
microcompactMetadata) or Anthropic's Context Editing API evicts tool results
(clear_tool_uses*), PreCompact does not fire. read_cache must still invalidate
file_reads / cached_content so the next Read is allowed in full rather than
serving a diff or structure skeleton against evicted context.
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def rc(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("TOKEN_OPTIMIZER_READ_MODE", "active")
    monkeypatch.setenv("TOKEN_OPTIMIZER_V5_DELTA", "1")
    sys.path.insert(0, str(SCRIPTS))
    for mod_name in ("compact_markers", "read_cache", "session_store"):
        sys.modules.pop(mod_name, None)
    mod = importlib.import_module("read_cache")
    yield mod
    for mod_name in ("compact_markers", "read_cache", "session_store"):
        sys.modules.pop(mod_name, None)


def _sample_file(tmp_path, name="module.py"):
    f = tmp_path / name
    lines = [f"def func_{i}(x):\n    return x + {i}\n\n" for i in range(60)]
    f.write_text("".join(lines), encoding="utf-8")
    return f


def _run_pre_read(rc, capsys, sid, fpath, transcript, tool_use_id):
    rc.handle_read(
        {
            "session_id": sid,
            "tool_name": "Read",
            "tool_use_id": tool_use_id,
            "tool_input": {"file_path": str(fpath)},
            "transcript_path": str(transcript),
        },
        mode="active",
        quiet=True,
    )
    return capsys.readouterr().out


def test_microcompact_boundary_invalidates_unchanged_and_modified_reads(rc, tmp_path, capsys):
    sid = "microcompact-sess-1"
    f = _sample_file(tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n", encoding="utf-8")

    # First read populates file_reads + cached_content.
    out1 = _run_pre_read(rc, capsys, sid, f, transcript, "tu-1")
    assert out1 == ""
    store = rc._make_store(sid)
    assert store.get_file_entry(str(f)) is not None

    # Claude Code microcompacts older Read results without firing PreCompact.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "system",
            "subtype": "microcompact_boundary",
            "uuid": "mc-uuid-001",
            "microcompactMetadata": {"trigger": "auto", "preTokens": 85000, "tokensSaved": 12000},
        }) + "\n")

    # Modify the file so delta mode would normally fire if cache survived.
    text = f.read_text(encoding="utf-8") + "\ndef func_60(x):\n    return x + 60\n"
    f.write_text(text, encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))

    # Next Read must be allowed in full (no delta diff, no skeleton/block).
    out2 = _run_pre_read(rc, capsys, sid, f, transcript, "tu-2")
    assert out2 == "", f"expected full read after microcompact, got: {out2!r}"

    # Entry was re-seeded as read_count=1 and the same boundary does not clear twice.
    entry = store.get_file_entry(str(f))
    assert entry is not None and entry["read_count"] == 1
    assert rc._scan_for_compaction(store, str(transcript)) is False


@pytest.mark.parametrize("row", [
    {
        "type": "assistant",
        "uuid": "ctu-top-1",
        "context_management": {
            "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 3, "cleared_input_tokens": 9000}],
        },
    },
    {
        "type": "assistant",
        "uuid": "ctu-msg-1",
        "message": {
            "role": "assistant",
            "context_management": {
                "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 1, "cleared_input_tokens": 4200}],
            },
        },
    },
])
def test_clear_tool_uses_edit_invalidates_read_cache(rc, tmp_path, capsys, row):
    sid = f"ctu-sess-{row['uuid']}"
    f = _sample_file(tmp_path)
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text("", encoding="utf-8")

    assert _run_pre_read(rc, capsys, sid, f, transcript, "tu-1") == ""
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

    out = _run_pre_read(rc, capsys, sid, f, transcript, "tu-2")
    assert out == "", f"expected full read after clear_tool_uses, got: {out!r}"
    store = rc._make_store(sid)
    assert store.get_file_entry(str(f))["read_count"] == 1


def test_zero_cleared_and_quoted_text_do_not_falsely_clear_cache(rc, tmp_path):
    sid = "no-false-clear-1"
    f = _sample_file(tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    store = rc._make_store(sid)
    store.upsert_file_entry(str(f), {"mtime_ns": 1, "size_bytes": 100, "read_count": 1})

    # Zero-count edit and quoted marker text in a tool_result must NOT trigger invalidation.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "uuid": "ctu-zero",
            "context_management": {
                "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 0, "cleared_input_tokens": 0}],
            },
        }) + "\n")
        fh.write(json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "microcompact_boundary clear_tool_uses_20250919"}]},
        }) + "\n")

    assert rc._scan_for_compaction(store, str(transcript)) is False
    assert store.get_file_entry(str(f)) is not None
