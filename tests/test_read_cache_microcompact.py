"""Regression tests: microcompact, client_cut_tool_uses, and clear_tool_uses
invalidate read_cache.

When Claude Code microcompacts older tool results (``microcompact_boundary``),
cuts tool uses client-side (``message.context_management.client_cut_tool_uses``),
or Anthropic's Context Editing API evicts tool results (``clear_tool_uses*`` in
``message.context_management.applied_edits``), PreCompact does not fire.
read_cache must still invalidate file_reads / cached_content so the next Read is
allowed in full rather than serving a diff or structure skeleton against evicted
context — while not re-invalidating when the API re-reports unchanged totals.
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


def _modify_file(f: Path, suffix: str, bump_sec: int = 5):
    text = f.read_text(encoding="utf-8") + f"\ndef func_{suffix}(x):\n    return x + 1\n"
    f.write_text(text, encoding="utf-8")
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + bump_sec))


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


def test_microcompact_boundary_invalidates_modified_read(rc, tmp_path, capsys):
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
        }) + "\n")

    # Modify the file so delta mode would normally fire if cache survived.
    _modify_file(f, "60")

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
        "uuid": "ctu-msg-1",
        "message": {
            "role": "assistant",
            "context_management": {
                "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 1, "cleared_input_tokens": 4200}],
            },
        },
    },
    {
        "type": "assistant",
        "uuid": "ccut-msg-1",
        "message": {
            "role": "assistant",
            "context_management": {
                "client_cut_tool_uses": 2,
                "applied_edits": [],
            },
        },
    },
])
def test_clear_tool_uses_and_client_cut_invalidate_read_cache(rc, tmp_path, capsys, row):
    sid = f"ctu-sess-{row['uuid']}"
    f = _sample_file(tmp_path)
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text("", encoding="utf-8")

    assert _run_pre_read(rc, capsys, sid, f, transcript, "tu-1") == ""
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

    # Modify the file so delta mode would intercept if cache was NOT invalidated.
    _modify_file(f, "61")

    out = _run_pre_read(rc, capsys, sid, f, transcript, "tu-2")
    assert out == "", f"expected full read after context_management eviction, got: {out!r}"
    store = rc._make_store(sid)
    assert store.get_file_entry(str(f))["read_count"] == 1


def test_rereported_applied_edits_does_not_wipe_cache_every_turn(rc, tmp_path, capsys):
    """If the API re-reports the same applied_edits totals on subsequent
    assistant turns (each with a fresh uuid), read_cache must only invalidate
    once until cleared totals actually increase.
    """
    sid = "ctu-rereport-1"
    f = _sample_file(tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    assert _run_pre_read(rc, capsys, sid, f, transcript, "tu-1") == ""

    # Turn 1: initial clear_tool_uses (3 uses, 9000 tokens) -> invalidates.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "uuid": "asst-turn-1",
            "message": {
                "role": "assistant",
                "context_management": {
                    "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 3, "cleared_input_tokens": 9000}],
                },
            },
        }) + "\n")
    _modify_file(f, "turn1", bump_sec=5)
    out1 = _run_pre_read(rc, capsys, sid, f, transcript, "tu-2")
    assert out1 == "", f"first clear_tool_uses should allow full read, got: {out1!r}"

    # Turn 2: fresh uuid re-reports the EXACT SAME totals -> must NOT wipe cache.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "uuid": "asst-turn-2",
            "message": {
                "role": "assistant",
                "context_management": {
                    "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 3, "cleared_input_tokens": 9000}],
                },
            },
        }) + "\n")
    _modify_file(f, "turn2", bump_sec=10)
    out2 = _run_pre_read(rc, capsys, sid, f, transcript, "tu-3")
    assert out2 != "", "re-reported applied_edits with unchanged totals must not wipe read cache"

    # Turn 3: totals increase (5 uses, 15000 tokens) -> invalidates again.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "uuid": "asst-turn-3",
            "message": {
                "role": "assistant",
                "context_management": {
                    "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 5, "cleared_input_tokens": 15000}],
                },
            },
        }) + "\n")
    _modify_file(f, "turn3", bump_sec=15)
    out3 = _run_pre_read(rc, capsys, sid, f, transcript, "tu-4")
    assert out3 == "", f"increased applied_edits totals should allow full read, got: {out3!r}"


def test_zero_cleared_guessed_and_quoted_text_do_not_falsely_clear_cache(rc, tmp_path):
    sid = "no-false-clear-1"
    f = _sample_file(tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    store = rc._make_store(sid)
    store.upsert_file_entry(str(f), {"mtime_ns": 1, "size_bytes": 100, "read_count": 1})

    # Zero-count edit, zero client_cut_tool_uses, guessed microcompactMetadata
    # without microcompact_boundary subtype, and quoted marker text in a
    # tool_result must NOT trigger invalidation.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "uuid": "ctu-zero",
            "message": {
                "role": "assistant",
                "context_management": {
                    "client_cut_tool_uses": 0,
                    "applied_edits": [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 0, "cleared_input_tokens": 0}],
                },
            },
        }) + "\n")
        fh.write(json.dumps({
            "type": "system",
            "subtype": "other",
            "uuid": "mc-meta-only",
            "microcompactMetadata": {"trigger": "auto"},
        }) + "\n")
        fh.write(json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "microcompact_boundary clear_tool_uses_20250919 client_cut_tool_uses"}]},
        }) + "\n")

    assert rc._scan_for_compaction(store, str(transcript)) is False
    assert store.get_file_entry(str(f)) is not None
