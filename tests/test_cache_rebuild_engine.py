"""Cache rebuilds after idle gaps: one engine for cache health and the fold.

Synthetic transcripts only (isolated HOME and snapshot dir). Covers the
observed TTL boundary (1h user vs 5m user), pricing on the rebuilt prefix,
the per-turn window on a long-lived file, and transcripts copied into two
directories counting once.
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

OPUS = "claude-opus-5-5"
PREFIX = 100_000


@pytest.fixture
def m(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path / "claude", raising=True)
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path / "snap", raising=True)
    monkeypatch.setattr(mod, "_load_pricing_tier", lambda: "anthropic")
    monkeypatch.setattr(mod, "detect_runtime", lambda *a, **k: "claude")
    yield mod, tmp_path / "claude"
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _iso(ago_seconds):
    t = datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _turn(ago, read=0, w1h=0, w5m=0, req=None, model=OPUS):
    usage = {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": read,
             "cache_creation_input_tokens": w1h + w5m,
             "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m}}
    rec = {"type": "assistant", "timestamp": _iso(ago),
           "message": {"model": model, "usage": usage, "content": [{"type": "text", "text": "ok"}]}}
    if req:
        rec["requestId"] = req
    return rec


def _write(root, project, name, records):
    path = root / "projects" / project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _session(ttl, start_ago, gaps, tag="s", model=OPUS):
    """A first write, then one turn per gap. A gap of None is a warm read;
    a number is an idle gap (seconds) after which the whole prefix is re-written."""
    key = "w1h" if ttl == "1h" else "w5m"
    t = start_ago
    recs = [_turn(t, **{key: PREFIX}, req=f"{tag}-0", model=model)]
    for i, gap in enumerate(gaps, 1):
        if gap is None:
            t -= 60
            recs.append(_turn(t, read=PREFIX, req=f"{tag}-{i}", model=model))
        else:
            t -= gap
            recs.append(_turn(t, **{key: PREFIX}, req=f"{tag}-{i}", model=model))
    return recs


def _card(m, key="opus_5_5"):
    return m.PRICING_TIERS["anthropic"]["claude_models"][key]


def test_one_hour_user_counts_only_gaps_past_the_hour(m):
    mod, root = m
    # 10-minute idle + rewrite, then a 2-hour idle + rewrite. A 1h user's
    # cache survived the 10 minutes, so only the 2-hour gap is a rebuild.
    _write(root, "-proj", "a.jsonl", _session("1h", 5 * 3600, [None, 600, None, 7200]))
    out = mod._get_cache_ttl_waste(days=30)
    card = _card(mod)
    rb = out["rebuild"]
    assert rb["ttl_seconds"] == 3600
    assert rb["events"] == 1 and rb["tokens"] == PREFIX
    expected = PREFIX * (card["cache_write_1h"] - card["cache_read"]) / 1e6
    assert out["avoidable_behavioral_usd"] == pytest.approx(expected, abs=1e-4)
    assert rb["tokens_by_class"] == {"cache_write_1h": PREFIX, "cache_write_5m": 0}


def test_five_minute_user_counts_gaps_past_five_minutes_at_the_5m_rate(m):
    mod, root = m
    _write(root, "-proj", "a.jsonl", _session("5m", 5 * 3600, [None, 600, None, 7200]))
    out = mod._get_cache_ttl_waste(days=30)
    card = _card(mod)
    rb = out["rebuild"]
    assert rb["ttl_seconds"] == 300
    assert rb["events"] == 2 and rb["tokens"] == 2 * PREFIX
    expected = 2 * PREFIX * (card["cache_write"] - card["cache_read"]) / 1e6
    assert rb["cost_usd"] == pytest.approx(expected, abs=1e-4)
    assert rb["tokens_by_class"] == {"cache_write_1h": 0, "cache_write_5m": 2 * PREFIX}


def test_rebuild_is_priced_on_the_prior_prefix_not_new_content(m):
    mod, root = m
    recs = [_turn(4 * 3600, w1h=PREFIX, req="a"),
            # 2 hours later: re-writes the old 100K prefix plus 50K of new content.
            _turn(2 * 3600, w1h=PREFIX + 50_000, req="b")]
    _write(root, "-proj", "a.jsonl", recs)
    out = mod._get_cache_ttl_waste(days=30)
    assert out["rebuild"]["tokens"] == PREFIX
    card = _card(mod)
    assert out["wasted_cost_usd"] == pytest.approx(
        PREFIX * (card["cache_write_1h"] - card["cache_read"]) / 1e6, abs=1e-4)


def test_long_lived_file_counts_only_rebuilds_inside_the_window(m):
    mod, root = m
    day = 86400
    # Resumed file: rebuilds 40 and 39 days ago, then resumed 2 days ago (that
    # resume itself rebuilt the prefix) with one more 2-hour gap. Its mtime is
    # now, so the file is in scope, but only the two recent rebuilds count.
    recs = _session("1h", 40 * day, [7200], tag="old")
    recs += [_turn(39 * day, w1h=PREFIX, req="old-2")]
    recs += _session("1h", 2 * day + 3 * 3600, [None, 7200], tag="new")
    _write(root, "-proj", "long.jsonl", recs)
    out = mod._get_cache_ttl_waste(days=30)
    assert out["rebuild"]["events"] == 2
    assert out["affected_sessions"] == 1


def test_transcript_copied_into_two_dirs_counts_once(m):
    mod, root = m
    recs = _session("1h", 6 * 3600, [None, 7200, None, 7200])
    _write(root, "-proj", "a.jsonl", recs)
    _write(root, "-synced-copy-of-proj", "a.jsonl", recs)
    # A third copy that lost its request ids still dedupes on timestamp + sizes.
    stripped = [{k: v for k, v in r.items() if k != "requestId"} for r in recs]
    _write(root, "-another-copy", "a.jsonl", stripped)
    out = mod._get_cache_ttl_waste(days=30)
    assert out["rebuild"]["events"] == 2
    assert out["rebuild"]["sessions"] == 1
    card = _card(mod)
    assert out["avoidable_behavioral_usd"] == pytest.approx(
        2 * PREFIX * (card["cache_write_1h"] - card["cache_read"]) / 1e6, abs=1e-4)


def test_short_automation_session_still_counts(m):
    mod, root = m
    _write(root, "-proj", "a.jsonl", _session("1h", 3 * 3600, [7200]))
    assert mod._get_cache_ttl_waste(days=30)["rebuild"]["events"] == 1


def test_each_session_priced_at_its_own_model(m):
    mod, root = m
    _write(root, "-proj", "a.jsonl", _session("1h", 4 * 3600, [7200], tag="a"))
    _write(root, "-proj", "b.jsonl", _session("1h", 4 * 3600, [7200], tag="b", model="claude-fable-5-1"))
    out = mod._get_cache_ttl_waste(days=30)
    net = lambda c: c["cache_write_1h"] - c["cache_read"]
    expected = PREFIX * (net(_card(mod)) + net(_card(mod, "fable_5_1"))) / 1e6
    assert out["rebuild"]["cost_usd"] == pytest.approx(expected, abs=1e-4)


def test_unknown_model_adds_nothing(m):
    mod, root = m
    _write(root, "-proj", "a.jsonl", _session("1h", 4 * 3600, [7200], model="claude-opus-5-99"))
    out = mod._get_cache_ttl_waste(days=30)
    assert out["rebuild"]["cost_usd"] == 0
    assert mod._estimate_cache_drop_savings(days=30)["cost_saved_usd"] == 0


@pytest.mark.parametrize("ttl,seconds", [("1h", 3600), ("5m", 300)])
def test_fold_reads_the_same_engine(m, ttl, seconds):
    mod, root = m
    _write(root, "-proj", "a.jsonl", _session(ttl, 6 * 3600, [None, 7200, None, 7200]))
    health = mod._cache_ttl_waste_cached(days=30, fresh=True)
    fold = mod._estimate_cache_drop_savings(days=30)
    assert fold["ttl_seconds"] == seconds
    assert fold["cost_saved_usd"] == pytest.approx(health["rebuild"]["cost_usd"], abs=1e-4)
    assert fold["rebuilt_tokens"] == 2 * PREFIX and fold["est_drops"] == 2
    assert fold["drop_sessions"] == 1
    assert fold["price_basis"] == "api_rates"
    cls = "cache_write_1h" if ttl == "1h" else "cache_write_5m"
    assert fold["by_class"][cls]["tokens"] == 2 * PREFIX
    assert fold["by_class"]["cache_read_instead"]["usd"] < 0


def test_stale_sidecar_from_the_old_engine_is_recomputed(m):
    mod, root = m
    _write(root, "-proj", "a.jsonl", _session("1h", 4 * 3600, [7200]))
    snap = Path(mod.SNAPSHOT_DIR)
    snap.mkdir(parents=True, exist_ok=True)
    old = {"period_days": 30, "platform": "claude", "_cached_ts": 9e12,
           "avoidable_behavioral_usd": 999.0}
    (snap / "cache_health.json").write_text(json.dumps(old))
    mod._cache_health_memo = None
    out = mod._cache_ttl_waste_cached(days=30)
    assert out.get("engine") == 2 and out["avoidable_behavioral_usd"] != 999.0


@pytest.mark.parametrize("ttl,phrase,resume", [
    (3600, "idled past the 1-hour cache", "Resume within the hour"),
    (300, "idled past the 5-minute cache", "Resume within 5 minutes")])
def test_dashboard_copy_states_the_users_gap(ttl, phrase, resume):
    from test_savings_render import render, fixture
    s = fixture()
    s["cache_drop"] = {"drop_sessions": 3, "est_drops": 5, "prefix_tokens": 200_000,
                       "rebuilt_tokens": 1_000_000, "ttl_seconds": ttl,
                       "cost_saved_usd": 42.0, "evidence": "opportunity"}
    html = render(s)
    assert phrase in html
    assert resume in html or "cache-drop-opportunity" in html
    assert "at API rates" in html
