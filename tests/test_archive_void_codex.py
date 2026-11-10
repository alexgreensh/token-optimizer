"""Archive rows on Codex threads count only when the rollout shows the pointer.

The rollout is what the model saw. When it runs past the event and holds no
pointer-sized output next to it, the "replaced" row saved nothing and is
excluded from counted totals through the void-evidence table (the ledger row
itself is never touched).
"""
import importlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
THREAD = "019e98d1-0848-7123-a5a4-0f87980fdd02"
CHILD = "019e98d1-9999-7123-a5a4-0f87980fdd03"
WHEN = datetime(2026, 6, 10, 10, 0, 30, tzinfo=timezone.utc)


@pytest.fixture()
def m(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._init_trends_db().close()
    yield mod, home
    sys.modules.pop("measure", None)


def _rollout(home, outputs, name=None, end=None):
    day = home / ".codex" / "sessions" / "2026" / "06" / "10"
    day.mkdir(parents=True, exist_ok=True)
    path = day / (name or ("rollout-2026-06-10T10-00-00-%s.jsonl" % THREAD))
    lines = [{"timestamp": "2026-06-10T10:00:00.000Z", "type": "session_meta", "payload": {"id": THREAD}}]
    for offset, output in outputs:
        lines.append({"timestamp": (WHEN + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
                      "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c", "output": output}})
    lines.append({"timestamp": (end or WHEN + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                  "type": "event_msg", "payload": {"type": "token_count"}})
    path.write_text("\n".join(map(json.dumps, lines)) + "\n")
    return path


def _archive(conn, sid=THREAD, when=WHEN, orig=40000, comp=1500):
    conn.execute("INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail) VALUES (?,?,?,?,?,?)",
                 (when.astimezone().replace(tzinfo=None).isoformat(), "tool_archive", 9000, 0.03, sid,
                  "replaced mcp__docs__fetch (%s chars -> %s chars)" % (format(orig, ","), format(comp, ","))))
    conn.commit()


def _counted(m, conn):
    return conn.execute("SELECT count(*) FROM " + m._counted_table(conn, "savings_events")).fetchone()[0]


def _collect(m):
    from _measure import archive_void_evidence as av
    conn = m._init_trends_db()
    try:
        return av.collect(conn, max_sessions=None)
    finally:
        conn.close()


def _count(m):
    conn = m._init_trends_db()
    try:
        return _counted(m, conn), conn.execute("SELECT count(*) FROM savings_events").fetchone()[0]
    finally:
        conn.close()


def test_full_output_in_rollout_voids_the_row(m):
    mod, home = m
    _rollout(home, [(1, "x" * 40000)])
    conn = mod._init_trends_db(); _archive(conn); conn.close()
    assert _collect(mod)["proven"] == 1
    assert _count(mod) == (0, 1)


def test_pointer_in_rollout_keeps_the_row(m):
    mod, home = m
    _rollout(home, [(1, "p" * 1480)])
    conn = mod._init_trends_db(); _archive(conn); conn.close()
    assert _collect(mod)["proven"] == 0
    assert _count(mod) == (1, 1)


def test_rollout_still_being_written_gives_no_verdict(m):
    mod, home = m
    _rollout(home, [], end=WHEN - timedelta(seconds=5))
    conn = mod._init_trends_db(); _archive(conn); conn.close()
    out = _collect(mod)
    assert out["proven"] == 0 and out["pending"] == 1
    assert _count(mod) == (1, 1)


def test_continued_thread_files_are_read_together(m):
    mod, home = m
    _rollout(home, [], end=WHEN - timedelta(minutes=10))
    _rollout(home, [(2, [{"type": "input_text", "text": "y" * 40000}])],
             name="rollout-2026-06-10T10-20-00-%s_%s.jsonl" % (THREAD, CHILD))
    conn = mod._init_trends_db(); _archive(conn); conn.close()
    assert _collect(mod)["proven"] == 1
    assert _count(mod) == (0, 1)


def test_evidence_goes_stale_when_the_rollout_changes(m):
    mod, home = m
    path = _rollout(home, [(1, "x" * 40000)])
    conn = mod._init_trends_db(); _archive(conn); conn.close()
    _collect(mod)
    assert _count(mod) == (0, 1)
    path.write_text(path.read_text() + "\n")
    assert _count(mod) == (1, 1)


def test_session_without_rollout_or_transcript_is_untouched(m):
    mod, home = m
    (home / ".codex" / "sessions").mkdir(parents=True)
    conn = mod._init_trends_db(); _archive(conn, sid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"); conn.close()
    assert _collect(mod) == {"scanned": 0, "proven": 0, "pending": 0}
    assert _count(mod) == (1, 1)


@pytest.mark.skipif(not shutil.which("zstd"), reason="zstd CLI not installed")
def test_compressed_rollout_is_read(m):
    import subprocess
    mod, home = m
    path = _rollout(home, [(1, "x" * 40000)])
    subprocess.run(["zstd", "-q", "--rm", str(path)], check=True)
    conn = mod._init_trends_db(); _archive(conn); conn.close()
    assert _collect(mod)["proven"] == 1
    assert _count(mod) == (0, 1)
