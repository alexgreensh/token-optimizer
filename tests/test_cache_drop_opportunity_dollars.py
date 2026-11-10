"""Cache-drop opportunity carries a dollar value by token class, outside every total."""
import json
import re

import pytest

from test_savings_card_young_install import measure
from test_savings_render import render, fixture

# The estimator's pricing (rebuilt prefix, observed TTL, per-model rates,
# unknown models add nothing) is covered on synthetic transcripts in
# test_cache_rebuild_engine.py; this file keeps the text and card rendering.


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


def test_measured_table_total_matches_the_monthly_logged_line(measure, monkeypatch, capsys):
    summary = {
        "period_days": 30, "total_cost_usd": 79.094, "total_events": 2, "daily_avg_usd": 2.64,
        "by_category": {"checkpoint_restore": {"events": 1, "tokens_saved": 10, "cost_saved_usd": 0.6496},
                        "verbosity_steer_measured": {"events": 1, "tokens_saved": 10, "cost_saved_usd": 78.4444}},
        "model_routing": {"realized_cost_usd": 393.973, "evidence": "estimated"},
        "uncaptured_runtime": {"cost_saved_usd": 6.703, "evidence": "estimated"},
    }
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: json.loads(json.dumps(summary)))
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: {"available": False})
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report()
    out = capsys.readouterr().out
    table = re.search(r"TOTAL \(measured\)\s+\S+\s+\S+\s+\$([0-9.]+)", out).group(1)
    monthly = re.search(r"Estimated monthly: \$([0-9.]+)", out).group(1)
    assert table == monthly


@pytest.mark.parametrize("ttl, cache_name, resume", [
    (3600, "the 1-hour cache", "Resume within the hour"),
    (300, "the 5-minute cache", "Resume within 5 minutes"),
    (None, "the cache window", "Resume within the hour"),
])
def test_text_advice_follows_the_observed_cache_ttl(measure, monkeypatch, capsys, ttl, cache_name, resume):
    # Claude Code already writes the 1-hour cache, so "use the 1h cache" is wrong advice.
    summary = {"period_days": 30, "total_cost_usd": 0.0, "total_events": 0, "by_category": {},
               "cache_drop": {"drop_sessions": 4, "est_drops": 4, "prefix_tokens": 50_000,
                              "cost_saved_usd": 1.0, "ttl_seconds": ttl, "evidence": "opportunity"}}
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: json.loads(json.dumps(summary)))
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: {"available": False})
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report()
    line = next(l for l in capsys.readouterr().out.splitlines() if "Cache drops" in l)
    assert f"idled past {cache_name} and re-wrote" in line and resume in line
    assert "1h cache" not in line
