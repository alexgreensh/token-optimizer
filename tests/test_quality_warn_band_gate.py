"""Behavioral proof that `quality-cache --warn` no longer re-prints the plain
"[Token Optimizer] Context quality: N/100..." warning on every
UserPromptSubmit while a session sits in one quality band.

Why this exists: the sibling proactive nudge (_maybe_nudge, below the warn
gate in measure.py) has always had a session cap (_NUDGE_SESSION_CAP),
a cooldown (_NUDGE_COOLDOWN_SECONDS), and an "only if it got worse" check.
The --warn CLI path -- the plain-text message the UserPromptSubmit hook
installs via `quality-cache --warn --quiet` -- never got the same
guardrails: it fired on every tick where score < warn_threshold, with no
cap, no cooldown, no re-fire condition. A session hovering in the 60s under
the default warn_threshold=70 (e.g. 69 -> 66 -> 64 -> 62, all the same
10-point band) nagged on every single prompt.

_maybe_quality_warn fixes this by reusing _NUDGE_SESSION_CAP and
_NUDGE_COOLDOWN_SECONDS from the sibling nudge, plus a band-drop gate: it
only re-fires once quality crosses into a NEW lower 10-point band. These
tests exercise the gate function directly (fast, precise) and then drive the
real quality_cache(warn=True) path end-to-end (mirroring
test_quality_cache_release_on_timeout.py's pattern) to prove the actual CLI
print is suppressed/allowed the same way.

Run: python3 -m pytest tests/test_quality_warn_band_gate.py -v
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-qwarn-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


# --- unit tests: the pure gate function --------------------------------------

def test_first_drop_below_threshold_fires(m):
    result = {"score": 66}
    assert m._maybe_quality_warn(result, warn_threshold=70) is True
    assert result["_warn_count"] == 1
    assert result["_warn_last_band"] == 6


def test_same_band_does_not_refire(m):
    """THE reported bug: 69 -> 66 -> 64 -> 62 (all band 6) must warn once."""
    result = {"score": 69}
    assert m._maybe_quality_warn(result, warn_threshold=70) is True
    for score in (66, 64, 62):
        result["score"] = score
        assert m._maybe_quality_warn(result, warn_threshold=70) is False, (
            f"re-fired within the same band at score={score}"
        )
    assert result["_warn_count"] == 1


def test_drop_into_a_new_lower_band_fires_again(m):
    result = {"score": 62}
    assert m._maybe_quality_warn(result, warn_threshold=70) is True
    result["score"] = 47  # crosses from band 6 into band 4
    # Defeat the cooldown so the band drop -- not elapsed time -- is under test.
    result["_warn_last_epoch"] = 0
    assert m._maybe_quality_warn(result, warn_threshold=70) is True
    assert result["_warn_last_band"] == 4
    assert result["_warn_count"] == 2


def test_cooldown_blocks_even_a_genuine_band_drop(m):
    """Cooldown is unconditional, matching _maybe_nudge's own cooldown gate."""
    result = {"score": 62}
    assert m._maybe_quality_warn(result, warn_threshold=70) is True
    result["score"] = 30  # a big drop, well into a new band
    # _warn_last_epoch was just set to "now" -- cooldown has not elapsed yet.
    assert m._maybe_quality_warn(result, warn_threshold=70) is False


def test_session_cap_stops_after_the_cap(m):
    result = {"score": 65}
    fired = 0
    for band_score in (65, 55, 45, 35):  # 4 distinct, ever-lower bands
        result["score"] = band_score
        result["_warn_last_epoch"] = 0  # defeat cooldown each time
        if m._maybe_quality_warn(result, warn_threshold=70):
            fired += 1
    assert fired == m._NUDGE_SESSION_CAP == 3


def test_score_at_or_above_threshold_never_fires(m):
    result = {"score": 70}
    assert m._maybe_quality_warn(result, warn_threshold=70) is False
    assert "_warn_count" not in result


# --- end-to-end: quality_cache(warn=True) drives the real print path --------

def _quiet_quality_data():
    return {"messages": [], "decisions": [], "compactions": 0}


def test_quality_cache_warn_does_not_repeat_within_a_band(m, monkeypatch, tmp_path, capsys):
    cache_dir = tmp_path / "to-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", cache_dir)

    session = tmp_path / "sess-warn.jsonl"
    session.write_text(
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_parse_jsonl_for_quality", lambda fp: _quiet_quality_data())

    scores = iter([66, 64, 62])  # same band (6), three ticks in a row

    def _fake_score(quality_data, session_id=None):
        return {"score": next(scores), "breakdown": {}, "signals": {}}

    monkeypatch.setattr(m, "compute_quality_score", _fake_score)

    fired = []
    for _ in range(3):
        capsys.readouterr()
        m.quality_cache(
            throttle_seconds=0, quiet=True, session_jsonl=str(session),
            force=True, warn=True, warn_threshold=70,
        )
        out = capsys.readouterr().out
        fired.append("Context quality:" in out)

    assert fired == [True, False, False], (
        f"expected exactly one warning across three same-band ticks, got {fired}"
    )


def test_quality_cache_warn_fires_again_on_band_drop(m, monkeypatch, tmp_path, capsys):
    cache_dir = tmp_path / "to-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", cache_dir)

    session = tmp_path / "sess-warn2.jsonl"
    session.write_text(
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_parse_jsonl_for_quality", lambda fp: _quiet_quality_data())

    scores = iter([66, 45])  # band 6, then band 4

    def _fake_score(quality_data, session_id=None):
        return {"score": next(scores), "breakdown": {}, "signals": {}}

    monkeypatch.setattr(m, "compute_quality_score", _fake_score)

    m.quality_cache(
        throttle_seconds=0, quiet=True, session_jsonl=str(session),
        force=True, warn=True, warn_threshold=70,
    )
    capsys.readouterr()

    # Defeat the cooldown directly on the persisted per-session cache so the
    # band drop -- not elapsed time -- is what this test proves.
    cache_path = m._quality_cache_path_for(Path(session))
    cached = m._read_quality_cache(cache_path)
    cached["_warn_last_epoch"] = 0
    m._write_quality_cache(cache_path, cached)

    m.quality_cache(
        throttle_seconds=0, quiet=True, session_jsonl=str(session),
        force=True, warn=True, warn_threshold=70,
    )
    out = capsys.readouterr().out
    assert "Context quality:" in out, "a drop into a new lower band must re-warn"
    assert "45/100" in out


def test_quality_cache_warn_false_does_not_print(m, monkeypatch, tmp_path, capsys):
    """Sanity: passing warn=False (the non-hook default) never prints, no
    matter how low the score -- this is what SessionStart/PreCompact's
    `quality-cache --force --quiet` invocations rely on."""
    cache_dir = tmp_path / "to-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", cache_dir)

    session = tmp_path / "sess-noflag.jsonl"
    session.write_text(
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_parse_jsonl_for_quality", lambda fp: _quiet_quality_data())
    monkeypatch.setattr(
        m, "compute_quality_score",
        lambda quality_data, session_id=None: {"score": 10, "breakdown": {}, "signals": {}},
    )

    capsys.readouterr()
    m.quality_cache(
        throttle_seconds=0, quiet=True, session_jsonl=str(session), force=True,
    )
    out = capsys.readouterr().out
    assert "Context quality:" not in out
