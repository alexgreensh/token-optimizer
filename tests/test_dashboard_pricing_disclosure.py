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


def test_dashboard_states_input_rate_minimum_and_premium_in_total(measure, monkeypatch):
    # Pricing v5 (savings-truth U4): the measured write-class premium joins the
    # action total as its own part and is shown inside Logged actions, so the
    # old "not in the total" footnote is gone.
    with_disclosure(monkeypatch, measure)
    payload = measure._dashboard_savings_data()
    actions = payload["action_savings"]
    assert actions["class_premium_usd"] == 2.5
    assert actions["display"]["components_cents"]["class_premium"] == 250
    assert actions["total_usd"] == actions["net_total_usd"]
    card = hero(render(payload))
    assert "Initial removals above are priced at the input rate, a minimum." in card
    assert "7 removals. That write-class premium, $2.50, is measured and included in Logged actions." in card
    assert "not in the total" not in card
    assert "—" not in card


def test_premium_raises_the_total_by_exactly_its_amount(measure, monkeypatch):
    with_disclosure(monkeypatch, measure, premium=0.0, class_events=0)
    without = measure._dashboard_savings_data()["action_savings"]
    with_disclosure(monkeypatch, measure, premium=2.5, class_events=7)
    with_premium = measure._dashboard_savings_data()["action_savings"]
    assert with_premium["total_usd"] - without["total_usd"] == __import__("pytest").approx(2.5)
    assert with_premium["display"]["total_cents"] - without["display"]["total_cents"] == 250


def test_old_payload_without_premium_part_keeps_footnote(measure, monkeypatch):
    with_disclosure(monkeypatch, measure)
    payload = measure._dashboard_savings_data()
    payload["action_savings"].pop("class_premium_usd")
    payload["action_savings"]["display"]["components_cents"].pop("class_premium")
    card = hero(render(payload))
    assert "about $2.50 more, not in the total" in card


def test_dashboard_omits_premium_line_when_none(measure, monkeypatch):
    with_disclosure(monkeypatch, measure, premium=0.0, class_events=0)
    card = hero(render(measure._dashboard_savings_data()))
    assert "a minimum" in card
    assert "write-class premium" not in card and "not in the total" not in card


def test_dashboard_without_oneshot_block_has_no_minimum_line(measure, monkeypatch):
    patch(measure, monkeypatch)
    card = hero(render(measure._dashboard_savings_data()))
    assert "a minimum" not in card
