"""`measure.py compact-advice`: deterministic replay of session history.

Replays the user's own main-conversation transcripts against candidate compact
windows, counting extra compactions and the cache-read tokens an earlier
compaction would have avoided, priced at the session model's card. Read-only,
estimate-labelled, never a recommendation.

Run: python3 -m pytest tests/test_compact_advice.py -q
"""
import importlib
import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"

SID = "dddddddd-1111-2222-3333-444444444444"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    home = tmp_path / "home"
    claude = home / ".claude"
    for d in (snap, claude / "projects" / "-proj", claude / "token-optimizer"):
        d.mkdir(parents=True, exist_ok=True)
    for k, v in {
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "CLAUDE_CONFIG_DIR": str(claude),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    mod._adv_claude = claude
    yield mod
    sys.modules.pop("measure", None)


def _assistant(ctx, model="claude-sonnet-5", req=None, sidechain=False):
    rec = {"type": "assistant", "timestamp": "2026-10-03T10:00:00Z",
           "message": {"model": model, "usage": {
               "input_tokens": 100,
               "cache_read_input_tokens": ctx - 200,
               "cache_creation_input_tokens": 100}}}
    if req:
        rec["requestId"] = req
    if sidechain:
        rec["isSidechain"] = True
        rec["agentId"] = "a1"
    return json.dumps(rec)


def _boundary():
    return json.dumps({"type": "system", "subtype": "compact_boundary",
                       "timestamp": "2026-10-03T10:30:00Z"})


def _write_session(m, lines, sid=SID):
    p = m._adv_claude / "projects" / "-proj" / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Turn extraction
# ---------------------------------------------------------------------------

def test_turns_extraction_skips_sidechain_and_dedups_stream(m):
    p = _write_session(m, [
        _assistant(10_000, req="r1"),
        _assistant(12_000, req="r1"),          # stream chunk of same request
        _assistant(50_000, req="r2"),
        _assistant(99_000, req="r3", sidechain=True),  # subagent row: skip
        _boundary(),
        _assistant(20_000, req="r4"),
    ])
    turns, boundaries, model = m._advice_session_turns(p)
    assert turns == [12_000, 50_000, 20_000]
    assert boundaries == {2}
    assert model == "claude-sonnet-5"


def test_turns_empty_file(m):
    p = _write_session(m, [])
    turns, boundaries, model = m._advice_session_turns(p)
    assert turns == [] and boundaries == set() and model is None


# ---------------------------------------------------------------------------
# Replay mechanics
# ---------------------------------------------------------------------------

def test_replay_compacts_earlier_and_counts_avoided(m):
    # Real history: contexts grow to 950K before a real compaction at turn 4.
    turns = [100_000, 200_000, 400_000, 700_000, 950_000, 40_000, 80_000]
    boundaries = {5}
    sim, avoided = m._advice_replay(turns, boundaries, 400_000)
    # v=100k, +100k=200k, +200k -> 400k => compact (sim=1), v = 30K + 200K = 230K
    # turn3: +300K -> 530K >= 400K => compact (sim=2), v = 330K, avoided 170K
    # turn4: +250K -> 580K >= 400K => compact (sim=3), v = 280K, avoided 370K+670K
    # turn5: real boundary resyncs v=40K; turn6: +40K -> 80K.
    assert sim == 3
    # turn2: 400K-230K=170K; turn3: 700K-330K=370K; turn4: 950K-280K=670K
    assert avoided == 170_000 + 370_000 + 670_000


def test_replay_no_compaction_under_large_window(m):
    turns = [10_000, 50_000, 120_000]
    sim, avoided = m._advice_replay(turns, set(), 400_000)
    assert sim == 0 and avoided == 0


def test_replay_context_drop_without_boundary_resyncs(m):
    turns = [100_000, 500_000, 60_000, 90_000]
    sim, avoided = m._advice_replay(turns, set(), 400_000)
    # turn1: v=500K >= 400K -> sim compact, v = 30K + 400K = 430K, avoided 70K
    # turn2: ctx 60K < prev 500K -> resync v = 60K (untagged reset)
    # turn3: v = 90K
    assert sim == 1
    assert avoided == 70_000


# ---------------------------------------------------------------------------
# End-to-end report
# ---------------------------------------------------------------------------

def _seed_sessions(m, n=4):
    """Four sessions that each grow past 500K then compact."""
    for i in range(n):
        _write_session(m, [
            _assistant(100_000, req=f"s{i}r1"),
            _assistant(300_000, req=f"s{i}r2"),
            _assistant(600_000, req=f"s{i}r3"),
            _assistant(900_000, req=f"s{i}r4"),
            _boundary(),
            _assistant(50_000, req=f"s{i}r5"),
        ], sid=f"dddddddd-0000-0000-0000-{i:012d}")


def test_report_replays_and_reports_estimates(m):
    _seed_sessions(m)
    rep = m.compact_advice(days=30)
    assert rep["estimate"] is True
    assert rep["sessions_replayed"] == 4
    assert rep["too_thin"] is False
    windows = {e["window"]: e for e in rep["windows"]}
    w300 = windows[300_000]
    assert w300["extra_compactions"] > 0
    assert w300["cache_read_tokens_avoided"] > 0
    assert w300["affected_sessions"] == 4
    assert w300["affected_share"] == 1.0
    assert w300["net_tokens"] == (
        w300["cache_read_tokens_avoided"] - w300["compaction_cost_tokens"])
    assert "assumptions" in rep and rep["assumptions"]["post_compact_context_tokens"]
    assert rep["default_window"]["tokens"]


def test_report_too_thin_without_sessions(m):
    rep = m.compact_advice(days=30)
    assert rep["too_thin"] is True
    assert rep["sessions_replayed"] == 0


def test_report_too_thin_when_no_session_reaches_compaction(m):
    _write_session(m, [_assistant(5_000, req="a"), _assistant(9_000, req="b")])
    _write_session(m, [_assistant(5_000, req="c"), _assistant(9_000, req="d")],
                   sid="eeeeeeee-0000-0000-0000-000000000001")
    rep = m.compact_advice(days=30)
    assert rep["too_thin"] is True


def test_quality_by_fill_band_aggregation(m):
    qc = m._adv_claude / "token-optimizer"
    (qc / "quality-cache-s1.json").write_text(json.dumps({
        "score": 90, "breakdown": {"context_fill_degradation": {"model_fill_pct": 20}}}), encoding="utf-8")
    (qc / "quality-cache-s2.json").write_text(json.dumps({
        "score": 60, "breakdown": {"context_fill_degradation": {"model_fill_pct": 75}}}), encoding="utf-8")
    (qc / "quality-cache-s3.json").write_text(json.dumps({
        "score": 70, "breakdown": {"context_fill_degradation": {"model_fill_pct": 90}}}), encoding="utf-8")
    bands = m._advice_quality_by_fill_band()
    assert bands["<50%"]["avg_score"] == 90 and bands["<50%"]["sessions"] == 1
    assert bands["70-80%"]["avg_score"] == 60
    assert bands["80%+"]["sessions"] == 1


def test_cli_json_shape_and_read_only(m, capsys=None):
    _seed_sessions(m)
    settings = m._adv_claude / "settings.json"
    settings.write_text('{"env": {"KEEP": "1"}}', encoding="utf-8")
    before = settings.read_bytes()
    buf = io.StringIO()
    with redirect_stdout(buf):
        m._compact_advice_cli(["--json", "--days", "30"])
    rep = json.loads(buf.getvalue())
    assert rep["schema"] == 1
    assert rep["estimate"] is True
    assert rep["windows"]
    assert rep["windows"][0]["estimate"] is True
    assert settings.read_bytes() == before, "compact-advice must not write settings"


def test_cli_text_mentions_command_and_estimates(m):
    _seed_sessions(m)
    buf = io.StringIO()
    with redirect_stdout(buf):
        m._compact_advice_cli(["--days", "30"])
    out = buf.getvalue()
    assert "estimates" in out.lower() or "estimate" in out.lower()
    assert "/autocompact" in out or "No candidate window beat" in out


def test_cli_too_thin_says_so_plainly(m):
    buf = io.StringIO()
    with redirect_stdout(buf):
        m._compact_advice_cli([])
    assert "too thin" in buf.getvalue().lower()
