"""Displayed cents add up to the displayed total in text, JSON and the dashboard."""
import json
import re

import pytest

from test_savings_card_young_install import measure
from test_savings_render import render


def snapshot():
    # Rounded one by one these parts print 39.84 + 393.29 + 47.72 + 35.52 = 516.37,
    # while the exact total 516.375284 prints 516.38.
    return {
        "period_days": 30,
        "total_cost_usd": 39.8352,
        "total_tokens": 9_879_355,
        "total_events": 4984,
        "by_category": {
            "tool_archive": {"events": 1, "tokens_saved": 10, "cost_saved_usd": 20.0001},
            "delta_read": {"events": 1, "tokens_saved": 10, "cost_saved_usd": 19.8351},
        },
        "daily_avg_usd": 1.3278,
        "model_routing": {"realized_cost_usd": 393.2909, "evidence": "estimated"},
        "behavioral_estimate": {"cost_saved_usd": 0.36, "loop_events": 1, "prevented_iterations": 1},
        "uncaptured_runtime": {"cost_saved_usd": 6.6906, "subagent_dispatches": 3},
        "mcp_cap_estimated": {"cost_saved_usd": 0.075, "events": 1},
        "handover_rerun": {"cost_saved_usd": 0.4406},
        "resume_lean_estimated": {"cost_saved_usd": 27.9481, "events": 2},
        "retrieval_serve": {"cost_saved_usd": 0.01, "served_sessions": 1},
    }


COUNTED = {"available": True, "reread_usd": 47.724884, "reread_tokens": 5,
           "oneshot_usd": 1, "total_usd": 48.724884}


def patch(measure, monkeypatch):
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: snapshot())
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: dict(COUNTED))
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)


def cents(text):
    return round(float(text.replace(",", "")) * 100)


def test_allocate_cents_parts_sum_to_total(measure):
    parts, total = measure._allocate_cents([39.8352, 393.2909, 47.724884, 35.5243])
    assert total == 51638
    assert sum(parts) == total
    assert parts == [3984, 39329, 4773, 3552]


@pytest.mark.parametrize("amounts", [
    [0.004, 0.004, 0.004], [1.005, 2.005, 3.005], [0.0, 0.0], [10.0], [-1.234, 5.678, 0.001],
    [39.8352, 393.2909, 47.724884, 0.36, 6.6906, 0.075, 0.4406, 27.9481, 0.01],
])
def test_allocation_invariants(measure, amounts):
    parts, total = measure._allocate_cents(amounts)
    assert sum(parts) == total
    assert all(abs(p - a * 100) < 1 for p, a in zip(parts, amounts))


def test_text_lines_add_up_to_all_in(measure, monkeypatch, capsys):
    patch(measure, monkeypatch)
    measure.savings_report()
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines()
             if re.search(r"^\s+\+ (model routing|repeat reads|est\.)", ln)]
    amounts = [cents(re.search(r"~\$([\d,.]+)/mo", ln).group(1)) for ln in lines]
    monthly = cents(re.search(r"Estimated monthly: \$([\d,.]+)", out).group(1))
    all_in = cents(re.search(r"ALL IN: ~\$([\d,.]+)/mo", out).group(1))
    assert len(amounts) == 8
    assert monthly + sum(amounts) == all_in == 51638
    # The measured table adds up as well.
    table = out.split("Cost Saved")[1].split("TOTAL (measured)")[0]
    rows = [cents(m) for m in re.findall(r"\$([\d.]+)$", table, re.M)]
    total = cents(re.search(r"TOTAL \(measured\)\s+[\d,]+\s+[\d,]+\s+\$([\d.]+)", out).group(1))
    assert rows and sum(rows) == total == 3984


def test_json_display_fields_add_up_and_keep_exact_values(measure, monkeypatch, capsys):
    patch(measure, monkeypatch)
    measure.savings_report(as_json=True)
    action = json.loads(capsys.readouterr().out)["action_savings"]
    display = action["display"]
    assert action["net_total_usd"] == pytest.approx(516.375284)
    assert action["logged_usd"] == 39.8352
    parts = list(display["components_cents"].values()) + list(display["estimates_cents"].values())
    assert sum(parts) == display["total_cents"] == 51638
    assert display["total_usd"] == 516.38


def test_dashboard_hero_parts_add_up(measure, monkeypatch):
    patch(measure, monkeypatch)
    payload = measure._dashboard_savings_data()
    actions = render(payload).split("savings-actions")[1].split("savings-history")[0]
    total = cents(re.search(r'"savings-amount">\$([\d,.]+)<', actions).group(1))
    logged = cents(re.search(r"Logged actions <strong>\$([\d,.]+)", actions).group(1))
    repeat = cents(re.search(r"Repeat reads avoided <strong>~\$([\d,.]+)", actions).group(1))
    other = cents(re.search(r"Other estimates <strong>~\$([\d,.]+)", actions).group(1))
    assert total == 51638
    assert logged + repeat + other == total
