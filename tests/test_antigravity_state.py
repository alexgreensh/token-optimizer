"""U3 — read-only reader for Antigravity's conversation store."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"))

from antigravity_state import (  # noqa: E402
    read_all_sessions,
    read_conversation,
    read_summaries,
    surface_dirs,
)


# --- tiny protobuf encoders (test-only) ------------------------------------


def _varint(value):
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _fv(num, value):
    return _varint((num << 3) | 0) + _varint(value)


def _fb(num, payload):
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def _usage(inp, out, cache_read=0, thinking=0, response=None):
    m = b""
    if inp:
        m += _fv(2, inp)
    if out:
        m += _fv(3, out)
    if cache_read:
        m += _fv(5, cache_read)
    if thinking:
        m += _fv(9, thinking)
    if response is None:
        response = out - thinking
    if response:
        m += _fv(10, response)
    return m


def _gen_blob(inp, out, cache_read=0, thinking=0, model="Gemini 3.5 Flash (Medium)",
              est_used=0, max_ctx=0):
    gen = _fb(4, _usage(inp, out, cache_read, thinking))
    if model:
        gen += _fb(21, model.encode())
    if est_used or max_ctx:
        cw = b""
        if est_used:
            cw += _fv(1, est_used)
        if max_ctx:
            cw += _fv(4, max_ctx)
        gen += _fb(9, _fb(10, cw))
    return _fb(1, gen)


def _step_blob(seconds, tool_name=""):
    m = _fb(1, _fv(1, seconds))
    if tool_name:
        m += _fb(4, _fb(2, tool_name.encode()))
    return m


def _make_db(db_path, gens=(), steps=()):
    """Create a conversation db with gen_metadata + steps tables and rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, metadata BLOB, step_payload BLOB)")
    for i, blob in enumerate(gens):
        con.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (?,?,?)", (i, blob, len(blob)))
    for i, (step_type, metadata, payload) in enumerate(steps):
        con.execute(
            "INSERT INTO steps (idx, step_type, metadata, step_payload) VALUES (?,?,?,?)",
            (i, step_type, metadata, payload),
        )
    con.commit()
    con.close()


def test_one_session_with_summed_tokens(tmp_path):
    home = tmp_path / ".gemini"
    db = home / "antigravity-cli" / "conversations" / "a.db"
    _make_db(
        db,
        gens=[
            _gen_blob(100, 50, cache_read=10, thinking=20),
            _gen_blob(200, 60, cache_read=5, thinking=30),
            _gen_blob(300, 10, cache_read=0, thinking=0),
        ],
        steps=[
            (14, _step_blob(1000), b""),
            (21, _step_blob(1010, "run_command"), b"tool-args-secret"),
            (8, _step_blob(1020, "view_file"), b""),
        ],
    )
    sessions = read_all_sessions(home)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["surface"] == "antigravity-cli"
    assert s["conversation_id"] == "a"
    assert s["input_tokens"] == 600
    assert s["output_tokens"] == 120
    assert s["cache_read_tokens"] == 15
    assert s["thinking_tokens"] == 50
    assert s["tool_call_count"] == 2
    assert s["user_input_count"] == 1
    assert s["start_time"] == 1000
    assert s["end_time"] == 1020


def test_same_id_two_surfaces_not_merged(tmp_path):
    home = tmp_path / ".gemini"
    _make_db(home / "antigravity-cli" / "conversations" / "x.db", gens=[_gen_blob(10, 10)])
    _make_db(home / "antigravity" / "conversations" / "x.db", gens=[_gen_blob(20, 20)])
    sessions = read_all_sessions(home)
    assert len(sessions) == 2
    assert {s["surface"] for s in sessions} == {"antigravity-cli", "antigravity"}


def test_wal_idle_read_leaves_no_side_files(tmp_path):
    db = tmp_path / "a.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    con.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (_gen_blob(5, 5),))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    # backdate mtime so the reader treats it as idle (immutable=1 path)
    old = time.time() - 120
    os.utime(db, (old, old))
    before = {p.name for p in db.parent.iterdir()}
    s = read_conversation(db, surface="antigravity-cli")
    after = {p.name for p in db.parent.iterdir()}
    assert s is not None
    assert s["output_tokens"] == 5
    assert before == after


def test_live_db_open_writer_reads_via_mode_ro(tmp_path):
    db = tmp_path / "a.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    con.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (_gen_blob(7, 7),))
    con.commit()
    # writer still open; mtime is fresh (now)
    s = read_conversation(db, surface="antigravity-cli")
    con.close()
    assert s is not None
    assert s["output_tokens"] == 7


def test_prompt_and_secrets_not_read(tmp_path):
    home = tmp_path / ".gemini"
    db = home / "antigravity-cli" / "conversations" / "a.db"
    _make_db(
        db,
        gens=[_gen_blob(10, 10)],
        steps=[(21, _step_blob(1, "run_command"), b"SECRET_TOOL_PAYLOAD")],
    )
    # reminders of untrusted content the reader must never surface:
    (home / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    (home / "antigravity-cli" / "history.jsonl").write_text(
        json.dumps({"display": "SECRET_PROMPT_TEXT", "timestamp": 1234, "workspace": "/home/me"}) + "\n"
    )
    # summaries preview (never read)
    sdb = home / "antigravity-cli" / "conversation_summaries.db"
    con = sqlite3.connect(str(sdb))
    con.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, preview TEXT)")
    con.execute("INSERT INTO conversation_summaries VALUES ('a', 'SECRET_PREVIEW')")
    con.commit()
    con.close()
    s = read_all_sessions(home)[0]
    joined = json.dumps(s, default=str)
    assert "SECRET_TOOL_PAYLOAD" not in joined
    assert "SECRET_PROMPT_TEXT" not in joined
    assert "SECRET_PREVIEW" not in joined


def test_killed_flag_from_summaries(tmp_path):
    home = tmp_path / ".gemini"
    _make_db(home / "antigravity-cli" / "conversations" / "a.db", gens=[_gen_blob(10, 10)])
    sdb = home / "antigravity-cli" / "conversation_summaries.db"
    con = sqlite3.connect(str(sdb))
    con.execute("CREATE TABLE conversation_summaries "
                "(conversation_id TEXT, title TEXT, killed numeric, not_fully_idle numeric)")
    con.execute("INSERT INTO conversation_summaries VALUES ('a', 'My Title', 1, 0)")
    con.commit()
    con.close()
    s = read_all_sessions(home)[0]
    assert s["killed"] is True
    assert s["title"] == "My Title"


def test_db_without_gen_metadata_skipped(tmp_path):
    home = tmp_path / ".gemini"
    db = home / "antigravity-cli" / "conversations" / "b.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE other (x INTEGER)")
    con.commit()
    con.close()
    assert read_all_sessions(home) == []


def test_symlinked_db_skipped(tmp_path):
    home = tmp_path / ".gemini"
    real = tmp_path / "real.db"
    real.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(real))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    con.commit()
    con.close()
    conv = home / "antigravity-cli" / "conversations"
    conv.mkdir(parents=True)
    (conv / "link.db").symlink_to(real)
    assert read_all_sessions(home) == []


def test_missing_home_returns_empty(tmp_path):
    assert read_all_sessions(tmp_path / "nope") == []


def test_undecodable_row_counted_and_excluded(tmp_path):
    db = tmp_path / "a.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
    con.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (_gen_blob(10, 10),))
    con.execute("INSERT INTO gen_metadata VALUES (1, ?, 0)", (b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01",))
    con.commit()
    con.close()
    s = read_conversation(db, surface="antigravity-cli")
    assert s["undecodable_rows"] == 1
    assert s["output_tokens"] == 10


def test_surface_dirs_lists_existing(tmp_path):
    home = tmp_path / ".gemini"
    (home / "antigravity-cli").mkdir(parents=True)
    (home / "antigravity").mkdir(parents=True)
    dirs = surface_dirs(home)
    assert {s for s, _ in dirs} == {"antigravity-cli", "antigravity"}
