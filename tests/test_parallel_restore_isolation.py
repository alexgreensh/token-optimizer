"""Restore must never mistake a session-id substring for session ownership."""
import importlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def measure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(mod, "TRENDS_DB", tmp_path / "trends.db")
    return mod


def test_compaction_does_not_restore_a_session_with_a_containing_id(measure, capsys):
    sibling = "abc12345-extra"
    cp = measure.CHECKPOINT_DIR / f"{sibling}-20261111-120000.md"
    cp.write_text("# checkpoint\nGenerated: test\n## Active Task\nSIBLING SECRET\n")
    measure.compact_restore(session_id="abc12345", cwd=str(cp.parent), is_compact=True)
    out = capsys.readouterr().out
    assert "SIBLING SECRET" not in out
    assert "Recent:" not in out
    assert measure._best_checkpoint_for_session("abc12345") is None


def test_ceiling_keeps_latest_of_every_live_session(measure):
    now = datetime.now()
    checkpoints = [dict(filename=f"session-{i}-20261111-120000.md", created=now,
                        trigger="auto") for i in range(3)]
    assert measure._checkpoints_to_prune(checkpoints, now=now, total_max=1) == []


def test_active_session_can_restore_its_old_checkpoint(measure, capsys):
    cp = measure.CHECKPOINT_DIR / "active-20260101-120000.md"
    cp.write_text("# checkpoint\nGenerated: test\n## Active Task\nOld but still active\n")
    old = time.time() - 8 * 86400
    os.utime(cp, (old, old))
    measure.compact_restore(session_id="active", is_compact=True)
    assert "Old but still active" in capsys.readouterr().out


def test_session_lookup_uses_session_and_date_index(measure):
    conn = measure._init_trends_db()
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM session_log "
                            "WHERE session_uuid = ? ORDER BY date DESC LIMIT 1", ("test",)).fetchall()
        assert any("idx_session_log_uuid_date" in str(row) for row in plan)
        assert not any("TEMP B-TREE" in str(row) for row in plan)
    finally:
        conn.close()


def test_resume_candidates_use_activity_index(measure):
    conn = measure._init_trends_db()
    try:
        query = ("SELECT session_uuid,date FROM session_log WHERE session_uuid IS NOT NULL "
                 "AND session_uuid != '' AND COALESCE(NULLIF(last_activity_day,''),NULLIF(date,'')) >= date('now', ?) "
                 "ORDER BY COALESCE(NULLIF(last_activity_day,''),NULLIF(date,'')) DESC, "
                 "COALESCE(collected_at,'') DESC,id DESC LIMIT ?")
        plan = conn.execute("EXPLAIN QUERY PLAN " + query, ("-7 days", 30)).fetchall()
        assert any("idx_session_log_resume_activity" in str(row) for row in plan)
        assert not any("TEMP B-TREE" in str(row) for row in plan)
    finally:
        conn.close()


def test_simultaneous_captures_in_same_second_keep_both(measure, monkeypatch, tmp_path):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 11, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(measure, "datetime", FrozenDatetime)
    monkeypatch.setattr(measure, "_cleanup_checkpoints", lambda: None)
    paths = []
    for n in range(2):
        path = tmp_path / f"transcript-{n}.jsonl"
        path.write_text(json.dumps({"type": "user", "uuid": str(n),
                                   "message": {"role": "user", "content": f"Distinct capture {n}"}}) + "\n")
        paths.append(path)
    def capture(path):
        return measure.compact_capture(session_id="same-session", trigger="auto", _trusted_transcript=str(path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        captures = list(pool.map(capture, paths))
    assert all(captures) and len(set(captures)) == 2
    bodies = [Path(path).read_text() for path in captures]
    assert all(any(f"Distinct capture {n}" in body for body in bodies) for n in range(2))
    assert all(Path(path).with_suffix(".json").exists() for path in captures)


def test_forked_transcript_restores_its_own_continuation(measure, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(measure, "_cleanup_checkpoints", lambda: None)
    def user(text, ident):
        return json.dumps({"type": "user", "uuid": ident, "message": {"role": "user", "content": text}})
    original = user("Original shared objective", "root")
    parent = tmp_path / "parent.jsonl"
    fork = tmp_path / "fork.jsonl"
    parent.write_text(original + "\n" + user("Parent private direction", "parent") + "\n")
    fork.write_text(original + "\n" + user("Fork private direction", "fork") + "\n")
    for sid, path in (("parent-session", parent), ("fork-session", fork)):
        assert measure.compact_capture(session_id=sid, trigger="auto", _trusted_transcript=str(path))
    measure.compact_restore(session_id="fork-session", is_compact=True)
    result = capsys.readouterr().out
    assert "Fork private direction" in result
    assert "Parent private direction" not in result


def test_interleaved_parallel_sessions_restore_their_own_transcripts(tmp_path):
    from _hook_harness import REPO, SESSION_ID, consented_home, hook_command, payload, run_hook, transcript

    home = consented_home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    git = ["git", "-C", str(project), "-c", "core.hooksPath=/dev/null"]
    subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                          "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    other = tmp_path / "other-worktree"
    subprocess.run(git + ["worktree", "add", "-b", "other", str(other)], check=True, capture_output=True)
    log = tmp_path / "hook-errors.jsonl"
    sessions = []
    for n in range(3):
        cwd = other if n == 1 else project
        sid = f"{n + 1:08d}-1111-4222-8333-444444444444"
        path = transcript(cwd, home)
        text = path.read_text().replace(SESSION_ID, sid).replace(
            "Please refactor the parser in app.py and add tests.", f"Distinct task number {n}")
        path = path.with_name(sid + ".jsonl")
        sidechain = {"type": "user", "isSidechain": True, "sessionId": sid,
                     "uuid": "sidechain", "message": {"role": "user", "content": "Sidechain instruction not for parent"}}
        path.write_text(text + json.dumps(sidechain) + "\n")
        request = payload("PreCompact", None, path, cwd)
        request["session_id"] = sid
        sessions.append((sid, path, n, cwd, request))

    def capture(session):
        return run_hook(hook_command("PreCompact", "compact-capture --trigger auto"), REPO,
                        session[4], home, log)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(capture, sessions))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]

    for sid, path, n, cwd, _ in reversed(sessions):
        request = payload("SessionStart", None, path, cwd)
        request.update(session_id=sid, source="compact")
        result = run_hook(hook_command("SessionStart"), REPO, request, home, log)
        assert result.returncode == 0, result.stderr
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert f"Distinct task number {n}" in context
        assert "Sidechain instruction not for parent" not in context
        assert all(f"Distinct task number {other}" not in context for other in range(3) if other != n)
        assert "Cross-session checkpoint" not in context
        request["source"] = "resume"
        resumed = run_hook(hook_command("SessionStart"), REPO, request, home, log)
        assert resumed.returncode == 0, resumed.stderr
        assert "Cross-session checkpoint" not in resumed.stdout

    fresh = payload("SessionStart", None, sessions[0][1], project)
    fresh.update(session_id="00000009-1111-4222-8333-444444444444", source="startup")
    result = run_hook(hook_command("SessionStart"), REPO, fresh, home, log)
    assert result.returncode == 0, result.stderr
    assert "Cross-session checkpoint" in result.stdout and "Not your session" in result.stdout
