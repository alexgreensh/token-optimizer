"""Lean-resume carry weights each receiver turn by the user's own compaction sizes.

w = S(ctx + avoided) / S(avoided), S(x) = share of the user's main-transcript
compactions in the 60 days before the event with preTokens >= x. Fewer than 10
compactions at or above the avoided size keeps the fixed 500K rule.
"""
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
OLD = "abcd1234-1111-2222-3333-444455556666"
RECV = "efab5678-1111-2222-3333-444455556666"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-08-29")
    sys.path.insert(0, str(SCRIPTS))
    for name in ("measure", "lean_resume_carry"):
        sys.modules.pop(name, None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._apply_sonnet_intro_pricing()
    carry = importlib.import_module("lean_resume_carry")
    monkeypatch.setattr(mod, "_counted_event_utc",
                        lambda ts: datetime.strptime(str(ts).replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S"))
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path / "claude")
    yield mod, carry
    for name in ("measure", "lean_resume_carry"):
        sys.modules.pop(name, None)


def _turn(ts, rid, ctx, model="claude-opus-5"):
    return json.dumps({"type": "assistant", "timestamp": ts, "requestId": rid, "message": {
        "model": model, "usage": {"input_tokens": 2, "cache_read_input_tokens": max(0, ctx - 102),
                                  "cache_creation_input_tokens": 100, "output_tokens": 10}}})


def _boundary(ts, pre, trigger="auto", sidechain=False):
    return json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": ts,
                       "isSidechain": sidechain, "compactMetadata": {"trigger": trigger, "preTokens": pre}})


def _history(tmp_path, sizes, day="2026-03-01", name="hist"):
    proj = tmp_path / "claude" / "projects" / "-other"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [_boundary("%sT%02d:00:00Z" % (day, i % 24), size) for i, size in enumerate(sizes)]
    (proj / (name + ".jsonl")).write_text("\n".join(lines) + "\n")


def _sessions(tmp_path, old_ctx, receiver_ctxs):
    proj = tmp_path / "claude" / "projects" / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / (OLD + ".jsonl")).write_text(_turn("2026-03-10T09:50:00Z", "old1", old_ctx) + "\n")
    inject = json.dumps({"type": "attachment", "timestamp": "2026-03-10T10:00:00Z", "attachment": {
        "type": "hook_additional_context",
        "content": "[Token Optimizer] Cold-resume lean (session abcd1234, 2026-03-10): context"}})
    turns = [_turn("2026-03-10T10:00:%02dZ" % (5 + i), "new%d" % i, ctx) for i, ctx in enumerate(receiver_ctxs)]
    (proj / (RECV + ".jsonl")).write_text("\n".join([inject] + turns) + "\n")


def _row(avoided):
    return {"id": 1, "timestamp": "2026-03-10T10:00:00", "session_id": OLD, "session_uuid": OLD,
            "tokens_saved": avoided, "cost_saved_usd": 1, "model": "opus",
            "detail": "warm cache-read receiver=" + RECV}


def test_survival_weight_is_conditional_share_of_own_compactions():
    from _measure import compaction_survival as cs
    s = cs.Survival([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200], 300)
    assert s.above == 10 and s.usable
    assert s.weight(0) == 1.0
    assert s.weight(250) == pytest.approx(7 / 10)   # >= 550: 600..1200
    thin = cs.Survival([100, 900], 300)
    assert not thin.usable and thin.weight(10) is None


def test_event_above_fixed_cap_still_carries_when_history_compacts_higher(m, tmp_path):
    """The old 500K rule zeroed a 594K reload that the user's sessions routinely outlive."""
    mod, carry = m
    _history(tmp_path, [700000] * 10 + [900000] * 10)
    _sessions(tmp_path, 600000, [20000, 120000, 250000])
    tier = mod.PRICING_TIERS[mod._load_pricing_tier()]
    result = carry._score_verified_event(_row(600000), mod, tier)
    assert result["eligible"] and result["threshold_source"] == "user_compaction_survival"
    # ctx+600K: 620K -> 20/20, 720K -> 10/20, 850K -> 10/20
    assert result["weighted_turns"] == pytest.approx(2.0)
    assert result["carry_tokens"] == 1200000
    rate = mod._counted_exact_card("claude-opus-5", tier)["cache_read"]
    assert result["carry_cost_usd"] == pytest.approx(1200000 * rate / 1e6, rel=1e-4)


def test_thin_history_keeps_fixed_rule(m, tmp_path):
    mod, carry = m
    _history(tmp_path, [700000] * 9)
    _sessions(tmp_path, 300000, [150000, 210000])
    tier = mod.PRICING_TIERS[mod._load_pricing_tier()]
    result = carry._score_verified_event(_row(300000), mod, tier)
    assert result["threshold_source"] == "old_session_500k_cap"
    assert result["turns"] == 1 and result["carry_tokens"] == 300000


def test_history_window_sidechains_subagents_and_old_compactions_ignored(m, tmp_path):
    mod, carry = m
    _history(tmp_path, [700000] * 20, day="2025-12-01", name="too-old")       # > 60 days before
    proj = tmp_path / "claude" / "projects" / "-other"
    (proj / "side.jsonl").write_text("\n".join(_boundary("2026-03-01T00:00:00Z", 700000, sidechain=True) for _ in range(20)))
    sub = proj / "side" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text("\n".join(_boundary("2026-03-01T00:00:00Z", 700000) for _ in range(20)))
    _sessions(tmp_path, 300000, [150000, 210000])
    tier = mod.PRICING_TIERS[mod._load_pricing_tier()]
    assert carry._score_verified_event(_row(300000), mod, tier)["threshold_source"] == "old_session_500k_cap"


def test_history_cache_reuses_unchanged_files_and_rescans_changed(tmp_path):
    from _measure import compaction_survival as cs
    _history(tmp_path, [400000, 500000])
    cache = tmp_path / "snap" / cs.CACHE_NAME
    projects = tmp_path / "claude" / "projects"
    assert [s for _t, s in cs.history(projects, None, cache)] == [400000, 500000]
    assert json.loads(cache.read_text())["version"] == 1
    _history(tmp_path, [600000])
    assert [s for _t, s in cs.history(projects, None, cache)] == [600000]


def test_benchmark_transcripts_are_not_the_users_history(tmp_path):
    from _measure import compaction_survival as cs
    bench = tmp_path / "claude" / "projects" / "-private-var-folders-xy-T-to-bench-M1-run"
    bench.mkdir(parents=True)
    (bench / "s.jsonl").write_text(_boundary("2026-03-01T00:00:00Z", 700000))
    assert cs.history(tmp_path / "claude" / "projects") == []
