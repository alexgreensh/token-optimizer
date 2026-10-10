"""Pin the biggest-drag attribution: the displayed score IS resource_health,
so "what is dragging quality down" must be the signal with the largest
weighted deficit (weight * (100 - signal)), not whichever waste key exists.

Before this, every surface derived the drag from waste breakdown keys only —
a session at 80% fill with zero waste claimed "nothing is dragging quality
down" while losing ~10 points to fill.
"""
from __future__ import annotations

import importlib
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
    qc = tmp_path / "qc"
    qc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", qc, raising=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path, raising=True)
    # 1M window so context_tokens maps cleanly to a fill fraction.
    monkeypatch.setattr(mod, "detect_context_window", lambda: (1_000_000, "test"))
    # Detectors are not under test — the attribution math is. Zero them out so
    # each test controls exactly one deficit source.
    monkeypatch.setattr(mod, "detect_stale_reads", lambda q: {
        "stale_reads": [], "count": 0, "estimated_waste_tokens": 0})
    monkeypatch.setattr(mod, "detect_bloated_results", lambda q: {
        "bloated_results": [], "count": 0, "estimated_waste_tokens": 0})
    monkeypatch.setattr(mod, "detect_duplicates", lambda q: {
        "duplicates": 0, "estimated_waste_tokens": 0})
    monkeypatch.setattr(mod, "detect_reread_loops", lambda q: {
        "count": 0, "excess_reads": 0, "reread_paths": {},
        "estimated_waste_tokens": 0})
    yield mod, qc
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _qdata(**kw):
    d = {
        "messages": [(0, "user", 4000, True), (1, "assistant", 8000, True)],
        "tool_results": [(2, "t1", 3000, False)],
        "tool_result_meta": [],
        "system_reminders": [],
        "reads": [],
        "writes": [],
        "compactions": 0,
        "tool_calls": 1,
        "agent_dispatches": [],
        "decisions": [],
        "total_entries": 3,
        "context_tokens": 20_000,   # 2% of the 1M test window
        "model": "claude-sonnet-4-6",
        "model_context_window": 1_000_000,
        "topic": "drag test",
    }
    d.update(kw)
    return d


def test_fill_is_the_drag_when_fill_dominates(m):
    """80% fill, clean everything else: the drag is fill, not 'nothing'."""
    mod, _ = m
    result = mod.compute_quality_score(_qdata(context_tokens=800_000))
    drag = result["top_drag"]
    assert drag is not None
    assert drag["key"] == "context_fill"
    assert "context fill" in drag["label"]
    assert "80%" in drag["label"]
    # 0.5 weight on a fill_score of ~9 = ~45pt deficit — it must beat the
    # (zero) waste and compaction deficits.
    assert drag["points"] > 40


def test_compactions_are_the_drag_when_they_dominate(m):
    """3 compactions on a nearly-empty context: compaction depth drags."""
    mod, _ = m
    result = mod.compute_quality_score(_qdata(compactions=3))
    drag = result["top_drag"]
    assert drag is not None
    assert drag["key"] == "compactions"
    assert "3 compactions" in drag["label"]


def test_waste_is_the_drag_when_waste_dominates(m, monkeypatch):
    """Large bloated-result waste at low fill: the waste cause is named."""
    mod, _ = m
    monkeypatch.setattr(mod, "detect_bloated_results", lambda q: {
        "bloated_results": [(2, 0, 100)],
        "count": 1,
        "estimated_waste_tokens": 40_000,  # 4% of the 1M window -> waste 60
    })
    result = mod.compute_quality_score(_qdata())
    drag = result["top_drag"]
    assert drag is not None
    assert drag["key"] == "waste:bloated_results"
    assert drag["label"] == "bloated tool results"


def test_healthy_session_has_no_drag(m):
    """A clean low-fill session claims no drag — the old code could not lie
    here, the new gate must not either."""
    mod, _ = m
    result = mod.compute_quality_score(_qdata())
    assert result["score"] >= 85
    assert result["top_drag"] is None


def test_drag_keys_match_the_named_causes(m, monkeypatch):
    """Every waste cause maps to the same phrase the desktop band used."""
    mod, _ = m
    cases = {
        "detect_stale_reads": ({"stale_reads": [(0, "f", 0)], "count": 1,
                               "estimated_waste_tokens": 50_000},
                               "stale file reads"),
        "detect_duplicates": ({"duplicates": 3,
                               "estimated_waste_tokens": 50_000},
                              "repeated system reminders"),
    }
    zero = {
        "detect_stale_reads": {"stale_reads": [], "count": 0,
                               "estimated_waste_tokens": 0},
        "detect_bloated_results": {"bloated_results": [], "count": 0,
                                   "estimated_waste_tokens": 0},
        "detect_duplicates": {"duplicates": 0, "estimated_waste_tokens": 0},
        "detect_reread_loops": {"count": 0, "excess_reads": 0,
                                "reread_paths": {}, "estimated_waste_tokens": 0},
    }
    for det, (payload, phrase) in cases.items():
        for name, zp in zero.items():
            monkeypatch.setattr(mod, name, lambda q, p=zp: p)
        monkeypatch.setattr(mod, det, lambda q, p=payload: p)
        result = mod.compute_quality_score(_qdata())
        drag = result["top_drag"]
        assert drag is not None and drag["label"] == phrase, (
            f"{det} should surface '{phrase}', got {drag!r}")


def test_reread_waste_is_never_named_as_the_drag(m, monkeypatch):
    """reread-loop waste is diagnostic-only (not in total_waste / Resource
    Health), so it must not be attributed as a drag it did not cause."""
    mod, _ = m
    monkeypatch.setattr(mod, "detect_reread_loops", lambda q: {
        "count": 2, "excess_reads": 4, "reread_paths": {"f": 4},
        "estimated_waste_tokens": 50_000})
    result = mod.compute_quality_score(_qdata())
    drag = result["top_drag"]
    assert not (drag and "re-read" in drag["label"]), (
        f"reread waste wrongly named the drag: {drag!r}")
