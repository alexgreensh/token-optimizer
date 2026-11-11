"""The fresh-session nudge shows once per session, however the quality cache is rewritten.

Run: python3 -m pytest tests/test_fresh_nudge_once.py -q
"""
import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"

SID = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER = "bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    qdir = tmp_path / "quality"
    qdir.mkdir()
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", qdir)
    monkeypatch.setattr(mod, "_is_v5_feature_enabled", lambda name: True)
    monkeypatch.setattr(mod, "_log_compression_event", lambda **kw: None)
    monkeypatch.setattr(mod, "_fresh_session_turn_economics",
                        lambda saved, sid=None: {"per_turn_usd": 0.05,
                                                 "one_time_usd": 0.04,
                                                 "breakeven_turns": 1})
    yield mod
    sys.modules.pop("measure", None)


def _degraded():
    # Long and degraded, with a previous score (not just after a compaction).
    return {"score": 67.7, "fill_pct": 60.0, "model_context_window": 1_000_000, "_nudge_previous_score": 68.0}


def _qd():
    # Current-context telemetry above the shared 300K absolute-token gate.
    return {"context_tokens": 400_000, "model_context_window": 1_000_000}


def _cache(m, sid):
    return m.QUALITY_CACHE_DIR / f"quality-cache-{sid}.json"


def test_shows_once_even_when_the_cache_loses_its_flag(m):
    first = m._maybe_fresh_session_nudge(_degraded(), _cache(m, SID), _qd())
    assert first and "Fresh session reclaims" in first
    # A later write replaced the cache without the fired flag (the reported repeat).
    again = _degraded()
    assert m._maybe_fresh_session_nudge(again, _cache(m, SID), _qd()) is None
    assert again["_fresh_nudge_fired"] is True


def test_racing_processes_show_it_once(m):
    shown = []
    start = threading.Barrier(8)

    def prompt():
        start.wait()
        msg = m._maybe_fresh_session_nudge(_degraded(), _cache(m, SID), _qd())
        if msg:
            shown.append(msg)

    threads = [threading.Thread(target=prompt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(shown) == 1


def test_each_session_still_gets_its_own(m):
    assert m._maybe_fresh_session_nudge(_degraded(), _cache(m, SID), _qd())
    assert m._maybe_fresh_session_nudge(_degraded(), _cache(m, OTHER), _qd())


def test_unreadable_transcript_keeps_nudge_state(m, tmp_path, monkeypatch):
    transcript = tmp_path / f"{SID}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    cache = _cache(m, SID)
    # Use v3 so the nudge baseline is comparable across the failed parse.
    cache.write_text(json.dumps({"score": 67.7, "score_version": 3, "_fresh_nudge_fired": True,
                                 "_nudge_previous_score": 67.7, "_nudge_count": 2}), encoding="utf-8")
    # The parser gives up (a very large transcript): the blank cache it writes
    # must not re-arm nudges that already fired.
    monkeypatch.setattr(m, "_parse_jsonl_for_quality", lambda path: None)
    m.quality_cache(quiet=True, session_jsonl=str(transcript), force=True, session_id=SID)
    after = json.loads(cache.read_text(encoding="utf-8"))
    # A failed parse must retain the last measurement, not invent a perfect score.
    assert after["score"] <= 67.7
    assert after["measured"] is False
    assert after["grade"] != "S"
    assert after["_fresh_nudge_fired"] is True
    assert after["_nudge_previous_score"] == 67.7
    assert after["_nudge_count"] == 2
