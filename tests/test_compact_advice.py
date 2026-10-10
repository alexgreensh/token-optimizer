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
    assert rep["schema"] == 2
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
    assert "/autocompact" in out or "No candidate window" in out


def test_cli_too_thin_says_so_plainly(m):
    buf = io.StringIO()
    with redirect_stdout(buf):
        m._compact_advice_cli([])
    assert "too thin" in buf.getvalue().lower()


# ===========================================================================
# truthful baseline, measured re-read cost, presentation, coach block
# ===========================================================================

def _assistant_tool(ctx, tool, inp, tid, req, model="claude-sonnet-5"):
    rec = {"type": "assistant", "requestId": req,
           "message": {"id": req, "model": model, "usage": {
               "input_tokens": 100, "cache_read_input_tokens": ctx - 200,
               "cache_creation_input_tokens": 100},
               "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}}
    return json.dumps(rec)


def _tool_result(tid, text):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}})


def _text_of(n_chars):
    return "word " * (n_chars // 5)


def _seed_real_compacts(m, n=4, peak=500_000, model="claude-sonnet-5"):
    """n sessions where the user really compacted near `peak` (never reached 650K+)."""
    for i in range(n):
        _write_session(m, [
            _assistant(100_000, req=f"q{i}r1", model=model),
            _assistant(300_000, req=f"q{i}r2", model=model),
            _assistant(peak, req=f"q{i}r3", model=model),
            _boundary(),
            _assistant(60_000, req=f"q{i}r4", model=model),
            _assistant(90_000, req=f"q{i}r5", model=model),
        ], sid=f"cccccccc-0000-0000-0000-{i:012d}")


# --- 1. baseline: recorded compactions always happen, extras never negative --

def test_replay_candidate_above_real_compact_is_noop(m):
    # Real manual compact at 450K. A 650K candidate would never have fired.
    turns = [100_000, 300_000, 450_000, 60_000, 80_000]
    sim, avoided = m._advice_replay(turns, {3}, 650_000)
    assert sim == 0 and avoided == 0


def test_replay_never_negative_and_recorded_boundary_not_counted(m):
    turns = [100_000, 300_000, 450_000, 60_000, 80_000]
    for w in (300_000, 400_000, 500_000, 650_000, 800_000, 967_000):
        sim, avoided = m._advice_replay(turns, {3}, w)
        assert sim >= 0 and avoided >= 0
    # 400K fires once before the recorded boundary: exactly one EXTRA.
    sim, _ = m._advice_replay(turns, {3}, 400_000)
    assert sim == 1


def test_report_candidates_at_or_above_real_behaviour_have_zero_extras(m):
    _seed_real_compacts(m, n=4, peak=500_000)
    rep = m.compact_advice(days=30)
    for e in rep["windows"]:
        assert e["extra_compactions"] >= 0, e
        if e["window"] >= 500_000:
            assert e["extra_compactions"] == 0, e
            assert e["net_tokens"] == 0, e
            assert e["cache_read_tokens_avoided"] == 0, e
    below = {e["window"]: e for e in rep["windows"]}[300_000]
    assert below["extra_compactions"] > 0


def test_default_row_is_the_reference_zero_by_construction(m):
    # A session that blows past the default window with no recorded compaction
    # (e.g. a different setting at the time). The default row is still the zero.
    _write_session(m, [
        _assistant(100_000, req="a"), _assistant(600_000, req="b"),
        _assistant(995_000, req="c"), _assistant(998_000, req="d"),
    ])
    _seed_real_compacts(m, n=3)
    rep = m.compact_advice(days=30)
    default_rows = [e for e in rep["windows"] if e["is_default"]]
    assert len(default_rows) == 1
    d = default_rows[0]
    assert d["extra_compactions"] == 0
    assert d["net_tokens"] == 0
    assert d["net_usd"] == 0
    assert d["cache_read_tokens_avoided"] == 0
    assert d["compaction_cost_tokens"] == 0


# --- 2. cost side: measured re-read + post-compact context ------------------

def test_session_data_measures_rereads_and_post_compact_context(m):
    big = _text_of(8_000)
    p = _write_session(m, [
        _assistant_tool(100_000, "Read", {"file_path": "/p/a.py"}, "t1", "r1"),
        _tool_result("t1", "x"),
        _assistant_tool(110_000, "Read", {"file_path": "/p/b.py"}, "t2", "r2"),
        _tool_result("t2", "x"),
        _boundary(),
        # first request after the boundary: context 41_000
        _assistant_tool(41_000, "Read", {"file_path": "/p/a.py"}, "t3", "r3"),
        _tool_result("t3", big),                                   # counted (a.py read before)
        _assistant_tool(45_000, "Read", {"file_path": "/p/new.py"}, "t4", "r4"),
        _tool_result("t4", big),                                   # NOT counted (never read)
        _assistant_tool(50_000, "Grep", {"pattern": "x", "path": "/p/b.py"}, "t5", "r5"),
        _tool_result("t5", big),                                   # counted
        _assistant_tool(52_000, "Bash", {"command": "cat /p/a.py | head"}, "t6", "r6"),
        _tool_result("t6", big),                                   # counted (path token)
        _assistant_tool(53_000, "Read", {"file_path": "/p/./a.py"}, "t7", "r7"),
        _tool_result("t7", big),                                   # counted (normalised path)
    ])
    d = m._advice_session_data(p)
    assert d["boundaries"] == {2}
    assert d["post_compact_ctx"] == [41_000]
    assert len(d["rereads"]) == 1
    one = m._estimate_tokens(big)
    assert d["rereads"][0] == 4 * one


def test_reread_window_is_ten_assistant_turns(m):
    big = _text_of(4_000)
    lines = [_assistant_tool(100_000, "Read", {"file_path": "/p/a.py"}, "t0", "r0"),
             _tool_result("t0", "x"), _boundary()]
    for i in range(1, 13):
        lines.append(_assistant_tool(40_000 + i, "Read", {"file_path": "/p/a.py"}, f"u{i}", f"q{i}"))
        lines.append(_tool_result(f"u{i}", big))
    p = _write_session(m, lines)
    d = m._advice_session_data(p)
    assert d["rereads"][0] == 10 * m._estimate_tokens(big)


def test_sidechain_tool_results_are_ignored(m):
    big = _text_of(4_000)
    sc = json.loads(_assistant_tool(40_000, "Read", {"file_path": "/p/a.py"}, "s1", "sr1"))
    sc["isSidechain"] = True
    p = _write_session(m, [
        _assistant_tool(100_000, "Read", {"file_path": "/p/a.py"}, "t0", "r0"),
        _tool_result("t0", "x"), _boundary(),
        _assistant(40_000, req="r1"),
        json.dumps(sc), _tool_result("s1", big),
    ])
    d = m._advice_session_data(p)
    assert d["rereads"] == [0]


def test_assumptions_fall_back_when_fewer_than_five_real_compactions(m):
    a = m._advice_measured_assumptions([1_000, 2_000, 3_000, 4_000], [50_000] * 4)
    assert a["reread_tokens_per_compaction"]["n"] == 4
    assert a["reread_tokens_per_compaction"]["value"] == m._ADVICE_REREAD_DEFAULT
    assert "assumed, too few real compactions to measure" in a["reread_tokens_per_compaction"]["source"]
    assert a["post_compact_context_tokens"]["value"] == m._ADVICE_POST_COMPACT_CONTEXT
    assert "assumed, too few real compactions to measure" in a["post_compact_context_tokens"]["source"]


def test_assumptions_use_median_at_five_or_more(m):
    a = m._advice_measured_assumptions([0, 100, 5_000, 9_000, 200_000], [30_000, 40_000, 50_000, 60_000, 900_000])
    assert a["reread_tokens_per_compaction"]["value"] == 5_000
    assert a["reread_tokens_per_compaction"]["n"] == 5
    assert a["reread_tokens_per_compaction"]["source"] == "measured"
    assert a["post_compact_context_tokens"]["value"] == 50_000


def test_cost_charges_summary_prefix_and_reread(m):
    # 5 real compactions so the assumptions are measured: reread 8K, post ctx 50K.
    big = _text_of(8_000)
    for i in range(5):
        _write_session(m, [
            _assistant_tool(100_000, "Read", {"file_path": "/p/a.py"}, f"a{i}", f"x{i}a"),
            _tool_result(f"a{i}", "x"),
            _assistant(350_000, req=f"x{i}b"),
            _boundary(),
            _assistant_tool(50_000, "Read", {"file_path": "/p/a.py"}, f"b{i}", f"x{i}c"),
            _tool_result(f"b{i}", big),
            _assistant(60_000, req=f"x{i}d"),
        ], sid=f"bbbbbbbb-0000-0000-0000-{i:012d}")
    # one session that grows past 300K with no recorded compact
    _write_session(m, [_assistant(100_000, req="g1"), _assistant(250_000, req="g2"),
                       _assistant(340_000, req="g3")],
                   sid="bbbbbbbb-1111-0000-0000-000000000000")
    rep = m.compact_advice(days=30)
    a = rep["assumptions"]
    reread = m._estimate_tokens(big)
    assert a["reread_tokens_per_compaction"] == reread
    assert a["post_compact_context_tokens"] == 50_000
    assert rep["measurements"]["reread_tokens_per_compaction"]["n"] == 5
    w = {e["window"]: e for e in rep["windows"]}[300_000]
    assert w["extra_compactions"] >= 1
    per = m._ADVICE_SUMMARY_OUTPUT_TOKENS + 50_000 + reread
    assert w["compaction_cost_tokens"] == w["extra_compactions"] * per


# --- 3. presentation --------------------------------------------------------

def _run_cli(m, args=()):
    buf = io.StringIO()
    with redirect_stdout(buf):
        m._compact_advice_cli(list(args))
    return buf.getvalue()


def test_cli_leads_with_tokens_and_share_and_labels_dollars(m):
    _seed_sessions(m)
    out = _run_cli(m)
    assert "estimates only" in out.lower()
    assert "API-equivalent" in out
    assert "%" in out
    # no 4-decimal dollar figures anywhere
    import re
    assert not re.search(r"\$\d+\.\d{3,}", out), out
    # tokens column comes before dollars in the header
    head = [l for l in out.splitlines() if l.strip().startswith("window")][0]
    assert head.index("net tokens") < head.index("API-equivalent")


def test_cli_subscription_billing_says_not_a_bill(m, monkeypatch):
    _seed_sessions(m)
    monkeypatch.setattr(m, "keepwarm_billing_mode", lambda *a, **k: "subscription")
    assert "not a bill" in _run_cli(m)
    monkeypatch.setattr(m, "keepwarm_billing_mode", lambda *a, **k: "api")
    assert "not a bill" not in _run_cli(m)


def test_cli_prints_measured_assumptions_with_n(m):
    _seed_sessions(m)
    out = _run_cli(m)
    assert "reread_tokens_per_compaction" in out
    assert "post_compact_context_tokens" in out
    assert "n=4" in out
    assert "assumed, too few real compactions to measure" in out


def test_cli_quality_table_kept_with_dropped_instructions_line(m):
    _seed_sessions(m)
    qc = m._adv_claude / "token-optimizer"
    (qc / "quality-cache-s1.json").write_text(json.dumps({
        "score": 90, "breakdown": {"context_fill_degradation": {"model_fill_pct": 20}}}), encoding="utf-8")
    out = _run_cli(m)
    assert "Average quality score by model-fill band" in out
    line = "Compaction can drop early instructions; the quality score does not measure that."
    assert line in out
    assert out.index("Average quality score") < out.index(line)


def test_cli_closing_is_neutral_and_gives_command_form(m):
    _seed_sessions(m)
    rep = m.compact_advice(days=30)
    out = _run_cli(m)
    assert "Largest positive replay net" not in out
    assert "best" not in out.lower()
    if rep["smallest_positive_window"]:
        assert "To try one: /autocompact <n>" in out
        assert out.rstrip().endswith("/autocompact <n>")
    else:
        assert "To try one" not in out


def test_cli_positive_window_prints_command_form(m):
    # Sessions that sit at 990K with no compaction: any smaller window saves a lot.
    for i in range(4):
        _write_session(m, [
            _assistant(100_000, req=f"p{i}a"), _assistant(400_000, req=f"p{i}b"),
            _assistant(700_000, req=f"p{i}c"), _assistant(900_000, req=f"p{i}d"),
            _assistant(960_000, req=f"p{i}e"), _assistant(965_000, req=f"p{i}f"),
            _assistant(966_000, req=f"p{i}g"),
        ], sid=f"aaaaaaaa-0000-0000-0000-{i:012d}")
    rep = m.compact_advice(days=30)
    assert rep["smallest_positive_window"] is not None
    out = _run_cli(m)
    assert "Smallest window whose net stays positive after the measured costs:" in out
    assert out.rstrip().splitlines()[-1].strip() == "To try one: /autocompact <n>"


def test_smallest_positive_window_reported(m):
    _seed_sessions(m)  # 4 sessions growing to 900K then a real compact
    rep = m.compact_advice(days=30)
    nets = {e["window"]: e["net_usd_unrounded"] for e in rep["windows"] if not e["reference"]}
    pos = [w for w in sorted(nets) if all(nets[x] > 0 for x in nets if x >= w)]
    expect = pos[0] if pos else None
    assert rep["smallest_positive_window"] == expect
    out = _run_cli(m)
    if expect:
        assert f"{expect // 1000}K" in out


def test_avoided_share_percent_uses_total_cache_read(m):
    _seed_sessions(m)
    rep = m.compact_advice(days=30)
    total = rep["total_cache_read_tokens"]
    assert total > 0
    for e in rep["windows"]:
        assert e["avoided_share_pct"] == round(e["cache_read_tokens_avoided"] / total * 100, 1)


# --- 4. coach block ---------------------------------------------------------

def test_coach_block_shape_and_cache(m, monkeypatch):
    _seed_sessions(m)
    calls = {"n": 0}
    orig = m._advice_session_data

    def counting(path):
        calls["n"] += 1
        return orig(path)
    monkeypatch.setattr(m, "_advice_session_data", counting)
    blk = m._coach_compact_advice_block()
    assert blk["estimate"] is True
    assert blk["resolved_window"]["tokens"] and blk["resolved_window"]["source"]
    assert blk["rows"] and {"label", "extra_compactions", "net_tokens"} <= set(blk["rows"][0])
    assert "reread_tokens_per_compaction" in blk["assumptions"]
    first = calls["n"]
    assert first > 0
    blk2 = m._coach_compact_advice_block()
    assert calls["n"] == first, "second call must be served from the cache"
    assert blk2["rows"] == blk["rows"]


def test_coach_block_never_raises(m, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(m, "compact_advice", boom)
    assert m._coach_compact_advice_block() is None


def test_coach_block_respects_time_budget_and_session_cap(m):
    _seed_sessions(m, n=4)
    rep = m.compact_advice(days=30, max_sessions=2)
    assert rep["sessions_replayed"] <= 2 and rep["truncated"] is True
    rep = m.compact_advice(days=30, deadline_seconds=-1)
    assert rep["truncated"] is True


def test_generate_coach_data_includes_compact_advice_and_survives_failure(m, monkeypatch):
    _seed_sessions(m)
    monkeypatch.setattr(m, "measure_components", lambda: {
        "skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": False, "names": []}})
    monkeypatch.setattr(m, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(m, "parse_session_turns", lambda *a, **kw: [])
    monkeypatch.setattr(m, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(m, "_deterministic_candidates_data", lambda **kw: {"candidates": []})
    # Only the coach CLI asks for the history replays: the dashboard and rollup
    # callers use the default and must not pay for one.
    assert "compact_advice" not in m.generate_coach_data()
    data = m.generate_coach_data(include_deterministic=True)
    assert "compact_advice" in data and data["compact_advice"]["rows"]
    m._advice_cache_path().unlink()   # the first call cached its block
    monkeypatch.setattr(m, "compact_advice",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    data = m.generate_coach_data(include_deterministic=True)
    assert isinstance(data, dict) and "compact_advice" not in data


def test_usable_context_ends_at_the_users_compact_window(m, monkeypatch):
    """"Usable Context" is the room before auto-compact fires. With a smaller
    compact window set, it ends there, not at the model window."""
    monkeypatch.setattr(m, "measure_components", lambda: {
        "skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": False, "names": []}})
    monkeypatch.setattr(m, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(m, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(m, "parse_session_turns", lambda *a, **kw: [])
    monkeypatch.setattr(m, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(m, "detect_context_window", lambda: (1_000_000, "test"))
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    snap = m.generate_coach_data()["snapshot"]
    overhead = snap["total_overhead"]
    assert snap["usable_tokens"] == 1_000_000 - 33_000 - overhead
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    snap = m.generate_coach_data()["snapshot"]
    assert snap["usable_tokens"] == 500_000 - overhead
    assert snap["context_window"] == 1_000_000
