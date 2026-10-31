"""The dashboard states the input-rate minimum and the credit/debit split like the text report."""
import json

from test_savings_card_young_install import measure
from test_savings_display_cents import COUNTED, patch, snapshot
from test_savings_render import render


def with_disclosure(monkeypatch, measure, premium=2.5, class_events=7):
    def snap():
        data = snapshot()
        data["compression_unpriced_tokens"] = 1500
        data["compression_unpriced_credit_tokens"] = 1000
        data["compression_unpriced_debit_tokens"] = 500
        data["oneshot_pricing"] = {"basis": "input_rate_minimum", "removal_events": 12,
                                   "removal_tokens": 90_000}
        return data
    patch(measure, monkeypatch)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: snap())
    counted = dict(COUNTED, oneshot_class_events=class_events, oneshot_class_premium_usd=premium,
                   oneshot_floor_events=3, oneshot_floor_tokens=40_000)
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: dict(counted))


def hero(html):
    return html.split("savings-actions")[1].split("savings-history")[0]


def test_dashboard_names_credits_and_debits(measure, monkeypatch):
    with_disclosure(monkeypatch, measure)
    card = hero(render(measure._dashboard_savings_data()))
    assert "1,500 compression tokens" in card
    assert "(1,000 credits, 500 debits)" in card


def test_dashboard_states_input_rate_minimum_and_premium_outside_total(measure, monkeypatch):
    with_disclosure(monkeypatch, measure)
    payload = measure._dashboard_savings_data()
    total_before = payload["action_savings"]["total_usd"]
    card = hero(render(payload))
    assert "Initial removals above are priced at the input rate, a minimum." in card
    assert "about $2.50 more, not in the total" in card
    assert "7 removals" in card
    assert payload["action_savings"]["total_usd"] == total_before
    assert "—" not in card


def test_dashboard_omits_premium_line_when_none(measure, monkeypatch):
    with_disclosure(monkeypatch, measure, premium=0.0, class_events=0)
    card = hero(render(measure._dashboard_savings_data()))
    assert "a minimum" in card
    assert "more, not in the total" not in card


def test_dashboard_without_oneshot_block_has_no_minimum_line(measure, monkeypatch):
    patch(measure, monkeypatch)
    card = hero(render(measure._dashboard_savings_data()))
    assert "a minimum" not in card
