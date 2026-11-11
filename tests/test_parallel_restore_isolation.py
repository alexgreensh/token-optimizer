"""Restore must never mistake a session-id substring for session ownership."""
import importlib
import json
import sys
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


def test_session_lookup_uses_session_and_date_index(measure):
    conn = measure._init_trends_db()
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM session_log "
                            "WHERE session_uuid = ? ORDER BY date DESC LIMIT 1", ("test",)).fetchall()
        assert any("idx_session_log_uuid_date" in str(row) for row in plan)
        assert not any("TEMP B-TREE" in str(row) for row in plan)
    finally:
        conn.close()


def test_interleaved_parallel_sessions_restore_their_own_transcripts(tmp_path):
    from _hook_harness import REPO, SESSION_ID, consented_home, hook_command, payload, run_hook, transcript

    home = consented_home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    log = tmp_path / "hook-errors.jsonl"
    sessions = []
    for n in range(3):
        sid = f"{n + 1:08d}-1111-4222-8333-444444444444"
        path = transcript(project, home)
        text = path.read_text().replace(SESSION_ID, sid).replace(
            "Please refactor the parser in app.py and add tests.", f"Distinct task number {n}")
        path = path.with_name(sid + ".jsonl")
        path.write_text(text)
        request = payload("PreCompact", None, path, project)
        request["session_id"] = sid
        result = run_hook(hook_command("PreCompact", "compact-capture --trigger auto"), REPO,
                          request, home, log)
        assert result.returncode == 0, result.stderr
        sessions.append((sid, path, n))

    for sid, path, n in reversed(sessions):
        request = payload("SessionStart", None, path, project)
        request.update(session_id=sid, source="compact")
        result = run_hook(hook_command("SessionStart"), REPO, request, home, log)
        assert result.returncode == 0, result.stderr
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert f"Distinct task number {n}" in context
        assert all(f"Distinct task number {other}" not in context for other in range(3) if other != n)
        assert "Cross-session checkpoint" not in context
        request["source"] = "resume"
        resumed = run_hook(hook_command("SessionStart"), REPO, request, home, log)
        assert resumed.returncode == 0, resumed.stderr
        assert "Cross-session checkpoint" not in resumed.stdout
