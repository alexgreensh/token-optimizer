"""Cache-drop opportunity carries a dollar value by token class, outside every total."""
import json
import re
import sqlite3
from datetime import datetime

import pytest

from test_counted_cumulative import m, _db
from test_savings_card_young_install import measure
from test_savings_render import render, fixture

PREFIX = 100_000


def _drop_session(conn, n, breakdown):
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO session_log (jsonl_path,date,input_tokens,max_call_gap_seconds,is_sidechain,"
        "model_usage_breakdown_json) VALUES (?,?,?,?,0,?)",
        (f"/synthetic/{n}.jsonl", today, 5000, 10_000, json.dumps(breakdown)))


def _parts(created):
    return {"fresh_input": 1, "cache_read": 5, "cache_create": created, "output": 1}


def _estimate(m, tmp_path, monkeypatch, sessions):
    db = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_per_session_prefix_tokens", lambda fallback=0: PREFIX)
    with sqlite3.connect(db) as conn:
        for i, breakdown in enumerate(sessions):
            _drop_session(conn, i, breakdown)
    return m._estimate_cache_drop_savings(days=30)


def test_priced_by_class_at_the_exact_generation(m, tmp_path, monkeypatch):
    card = m.PRICING_TIERS["anthropic"]["claude_models"]["opus_5_5"]
    out = _estimate(m, tmp_path, monkeypatch, [{"claude-opus-5-5": _parts(900)}] * 2)
    tokens = 2 * PREFIX
    assert out["cost_saved_usd"] == pytest.approx(
        tokens * (card["cache_write"] - card["cache_read"]) / 1e6, abs=1e-4)
    assert out["by_class"]["cache_write_5m"] == {"tokens": tokens, "usd": pytest.approx(tokens * card["cache_write"] / 1e6, abs=1e-4)}
    assert out["by_class"]["cache_read_instead"]["usd"] < 0
    assert out["rate_basis"] == "session_models"
    assert out["unpriced_share"] == 0
    assert out["evidence"] == "opportunity"


def test_models_blend_by_their_cache_write_tokens(m, tmp_path, monkeypatch):
    cards = m.PRICING_TIERS["anthropic"]["claude_models"]
    out = _estimate(m, tmp_path, monkeypatch,
                    [{"claude-opus-5-5": _parts(300)}, {"claude-fable-5-1": _parts(100)}])
    net = lambda c: c["cache_write"] - c["cache_read"]
    expected = 2 * PREFIX * (0.75 * net(cards["opus_5_5"]) + 0.25 * net(cards["fable_5_1"])) / 1e6
    assert out["cost_saved_usd"] == pytest.approx(expected, abs=1e-4)


def test_unknown_generation_adds_nothing_and_is_disclosed(m, tmp_path, monkeypatch):
    card = m.PRICING_TIERS["anthropic"]["claude_models"]["opus_5_5"]
    out = _estimate(m, tmp_path, monkeypatch,
                    [{"claude-opus-5-5": _parts(300), "claude-opus-5-99": _parts(100)}])
    expected = PREFIX * 0.75 * (card["cache_write"] - card["cache_read"]) / 1e6
    assert out["cost_saved_usd"] == pytest.approx(expected, abs=1e-4)
    assert out["unpriced_share"] == 0.25


def test_text_shows_dollars_on_its_own_line_outside_the_subtotal(measure, monkeypatch, capsys):
    summary = {
        "period_days": 30, "total_cost_usd": 10.0, "total_events": 1, "by_category": {},
        "daily_avg_usd": 0.33, "structural_potential": {"cost_saved_usd": 10.0, "unused_skills": 2},
        "output_waste": {"cost_saved_usd": 5.0, "avoidable_writes": 1, "writes": 2},
        "cache_drop": {"drop_sessions": 4, "est_drops": 4, "prefix_tokens": 50_000,
                       "cost_saved_usd": 133.05, "evidence": "opportunity"},
    }
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: json.loads(json.dumps(summary)))
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: {"available": False})
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report()
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "Cache drops" in l)
    assert "an opportunity, not a measurement" in line
    assert "200,000 tokens/mo" in line and "about $133.05/mo at API rates" in line
    assert "= ~$15.00/mo from the items above" in out
    assert "ALL IN: ~$10.00/mo" in out
    measure.savings_report(as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["action_savings"]["total_usd"] == 10.0


def test_dashboard_card_shows_dollars_and_keeps_it_out_of_the_number():
    s = fixture()
    s["structural_potential"] = {"cost_saved_usd": 20, "unused_skills": 3}
    s["cache_drop"] = {"drop_sessions": 4, "est_drops": 4, "prefix_tokens": 50_000,
                       "cost_saved_usd": 133.05, "evidence": "opportunity"}
    html = render(s)
    card = html.split("Money on the table")[1]
    assert "Cache drops (an opportunity, not a measurement)" in card
    assert "$133" in card.split("cache-drop-opportunity")[1].split("</div>")[0]
    assert "still on the table" in card and "$20.0" in card.split("still on the table")[0]


def test_opportunity_items_add_up_to_their_printed_sum(measure, monkeypatch, capsys):
    summary = {
        "period_days": 30, "total_cost_usd": 10.0, "total_events": 1, "by_category": {},
        "daily_avg_usd": 0.33,
        "output_waste": {"cost_saved_usd": 32.625, "avoidable_writes": 1, "writes": 2},
        "stale_reads_reclaimable": {"cost_usd": 10.2449, "tokens": 1000, "sessions": 1},
    }
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: json.loads(json.dumps(summary)))
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: {"available": False})
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report()
    out = capsys.readouterr().out
    block = out.split("COULD SAVE")[1].split("from the items above")[0]
    parts = [float(x) for x in re.findall(r"~\$([0-9.]+)/mo (?:output|reclaimable)", block)]
    total = float(re.search(r"= ~\$([0-9.]+)/mo$", block.rstrip()).group(1))
    assert len(parts) == 2 and total == pytest.approx(42.87)
    assert sum(parts) == pytest.approx(total)


def test_dashboard_card_items_add_up_to_the_card_number():
    s = fixture()
    s.pop("structural_potential", None)
    s["output_waste"] = {"cost_saved_usd": 32.625, "avoidable_writes": 1, "writes": 2}
    s["stale_reads_reclaimable"] = {"cost_usd": 10.2449, "tokens": 1000, "sessions": 1}
    card = render(s).split("Money on the table")[1]
    assert "$42.87" in card.split("still on the table")[0]
    assert "worth about $32.63/mo in output" in card
    assert "worth about $10.24/mo" in card


def test_dashboard_and_text_round_half_cents_the_same_way(measure, monkeypatch, capsys):
    summary = {
        "period_days": 30, "total_cost_usd": 10.0, "total_events": 1, "by_category": {},
        "daily_avg_usd": 0.33,
        "output_waste": {"cost_saved_usd": 32.625, "avoidable_writes": 870, "writes": 2903},
        "stale_reads_reclaimable": {"cost_usd": 10.245, "tokens": 2049000, "sessions": 202},
    }
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: json.loads(json.dumps(summary)))
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: {"available": False})
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report()
    text = capsys.readouterr().out
    assert "~$32.63/mo output" in text and "~$10.24/mo reclaimable" in text
    s = fixture()
    s.pop("structural_potential", None)
    s["output_waste"] = summary["output_waste"]
    s["stale_reads_reclaimable"] = summary["stale_reads_reclaimable"]
    card = render(s).split("Money on the table")[1]
    assert "$42.87" in card.split("still on the table")[0]
    assert "worth about $32.63/mo in output" in card
    assert "are worth about $10.24/mo" in card
