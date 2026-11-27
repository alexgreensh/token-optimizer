"""Checkpoint telemetry must record Stop/PreCompact captures and zero-credit restores (#358).

The checkpoint-events writer was only reachable from the progressive/quality/
milestone paths via _record_checkpoint_metadata. A real Stop hook wrote a
checkpoint through compact_capture but never touched the telemetry log, so
`checkpoint-stats` reported zero events on installs with hundreds of stop
checkpoints. Separately, a compact restore whose working set was empty logged
no checkpoint_restore savings event at all, so a successful restore was
unauditable. compact_capture is now the single emission point for every
successful checkpoint write, and restores log even at zero credit.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir, raising=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", tmp_path / "qc", raising=True)
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path / "snap", raising=True)
    monkeypatch.setattr(mod, "TRENDS_DB", tmp_path / "snap" / "trends.db", raising=True)
    monkeypatch.setattr(mod, "CHECKPOINT_EVENT_LOG",
                        tmp_path / "checkpoint-events.jsonl", raising=True)
    monkeypatch.setattr(mod, "_CHECKPOINT_TELEMETRY_ENABLED", True, raising=True)
    monkeypatch.setattr(mod, "_capture_git_state", lambda cwd=None: (None, None), raising=True)
    yield mod, cp_dir, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _transcript(tmp_path: Path, name: str = "sess-telemetry.jsonl") -> Path:
    projects = tmp_path / "projects" / "-work-proj"
    projects.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "please fix the flaky login test"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I decided to mock the clock in the fixture."}]}},
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "ship it"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done, tests green"}]}},
    ]
    p = projects / name
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def _events(mod):
    log = Path(mod.CHECKPOINT_EVENT_LOG)
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_stop_capture_writes_telemetry_event(m):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path)
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-telemetry-stop", trigger="stop")
    assert out, "compact_capture returned no checkpoint path"
    events = _events(mod)
    assert len(events) == 1
    event = events[0]
    assert event["trigger"] == "stop"
    assert event["session_id"] == "sess-telemetry-stop"
    assert event["checkpoint_path"] == out
    assert event["platform"]


def test_progressive_path_is_not_double_counted(m, tmp_path):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path)
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-telemetry-prog",
                              trigger="progressive-50", fill_pct=52.0)
    assert out
    # The progressive/quality callers persist per-session metadata after a
    # capture; the event must not be emitted a second time from there.
    cache_path = tmp_path / "qc" / "quality-cache-sess-telemetry-prog.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    mod._record_checkpoint_metadata({}, cache_path, "progressive-50", out,
                                    fill_pct=52.0)
    events = _events(mod)
    assert len(events) == 1
    assert events[0]["trigger"] == "progressive-50"
    assert events[0]["fill_pct"] == 52.0


def test_telemetry_disabled_writes_nothing(m, monkeypatch):
    mod, cp_dir, tmp_path = m
    monkeypatch.setattr(mod, "_CHECKPOINT_TELEMETRY_ENABLED", False, raising=True)
    transcript = _transcript(tmp_path)
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-telemetry-off", trigger="stop")
    assert out
    assert _events(mod) == []


def test_checkpoint_stats_counts_stop_events(m, capsys):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path)
    assert mod.compact_capture(transcript_path=str(transcript),
                               session_id="sess-telemetry-stats", trigger="stop")
    mod.checkpoint_stats(days=1, as_json=True)
    summary = json.loads(capsys.readouterr().out)
    assert summary["enabled"] is True
    assert summary["total_events"] == 1
    assert summary["by_trigger"] == {"stop": 1}
    assert summary["last_event"]["trigger"] == "stop"


def test_zero_credit_restore_is_audited(m, monkeypatch, capsys):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path, name="sess-restore-zero.jsonl")
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-restore-zero", trigger="stop")
    assert out
    # A real restore whose working set is empty: recovery estimate is zero.
    monkeypatch.setattr(mod, "_checkpoint_restore_recovery_tokens",
                        lambda sid, floor: 0, raising=True)
    mod.compact_restore(session_id="sess-restore-zero", is_compact=True)
    body = capsys.readouterr().out
    assert "Post-compact recovery" in body
    conn = sqlite3.connect(str(mod.TRENDS_DB))
    try:
        rows = conn.execute(
            "SELECT event_type, tokens_saved, detail FROM savings_events "
            "WHERE event_type = 'checkpoint_restore'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][1] == 0
    assert "no working-set credit" in rows[0][2]


def test_placeholder_capture_is_labeled_not_substantive(m, capsys):
    """A 'No transcript data available' placeholder write is recorded (hook
    fired) but labeled so checkpoint-stats never reads it as a substantive,
    restorable checkpoint."""
    mod, cp_dir, tmp_path = m
    # No transcript_path and no inferable session transcript: the capture
    # falls through to the minimal placeholder write.
    out = mod.compact_capture(session_id="sess-placeholder", trigger="stop")
    assert out, "placeholder checkpoint was not written"
    events = _events(mod)
    assert len(events) == 1
    assert events[0]["placeholder"] is True
    mod.checkpoint_stats(days=1, as_json=True)
    summary = json.loads(capsys.readouterr().out)
    assert summary["recent_events"] == 1
    assert summary["placeholder_events"] == 1


def test_substantive_capture_carries_no_placeholder_flag(m, capsys):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path)
    assert mod.compact_capture(transcript_path=str(transcript),
                               session_id="sess-substantive", trigger="stop")
    events = _events(mod)
    assert len(events) == 1
    assert "placeholder" not in events[0]
    mod.checkpoint_stats(days=1, as_json=True)
    summary = json.loads(capsys.readouterr().out)
    assert summary["placeholder_events"] == 0


def _seed_quality_cache(tmp_path: Path, session_stem: str, blob: dict) -> None:
    qc = tmp_path / "qc"
    qc.mkdir(parents=True, exist_ok=True)
    (qc / f"quality-cache-{session_stem}.json").write_text(
        json.dumps(blob), encoding="utf-8")


def test_unmeasured_quality_omitted_and_marked_in_event(m):
    # An unmeasured session (grade '?') carries the scorer's fallback score
    # (last_measured_score, default 100) in its cache. The telemetry event
    # must not present that fallback as a measurement (#360 class).
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path, name="sess-unmeasured.jsonl")
    _seed_quality_cache(tmp_path, "sess-unmeasured", {
        "score": 100.0, "grade": "?", "measured": False,
        "signals": {}, "breakdown": {}, "topic": "t", "model": "m",
    })
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-unmeasured", trigger="stop")
    assert out
    events = _events(mod)
    assert len(events) == 1
    event = events[0]
    assert "quality_score" not in event
    assert event["quality_unmeasured"] is True


def test_measured_quality_still_recorded_in_event(m):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path, name="sess-measured.jsonl")
    _seed_quality_cache(tmp_path, "sess-measured", {
        "score": 77.3, "grade": "B", "measured": True,
        "signals": {}, "breakdown": {}, "topic": "t", "model": "m",
    })
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-measured", trigger="stop")
    assert out
    events = _events(mod)
    assert len(events) == 1
    event = events[0]
    assert event["quality_score"] == 77.3
    assert "quality_unmeasured" not in event


def test_checkpoint_stats_counts_unmeasured_quality(m, capsys):
    mod, cp_dir, tmp_path = m
    transcript = _transcript(tmp_path, name="sess-unmeasured-stats.jsonl")
    _seed_quality_cache(tmp_path, "sess-unmeasured-stats", {
        "score": 100.0, "grade": "?", "measured": False,
        "signals": {}, "breakdown": {}, "topic": "t", "model": "m",
    })
    out = mod.compact_capture(transcript_path=str(transcript),
                              session_id="sess-unmeasured-stats", trigger="stop")
    assert out
    capsys.readouterr()
    summary = mod.checkpoint_stats(days=7, as_json=False)
    assert summary["unmeasured_quality_events"] == 1
    assert summary["placeholder_events"] == 0
    printed = capsys.readouterr().out
    assert "Unmeasured-quality captures" in printed
