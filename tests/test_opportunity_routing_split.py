#!/usr/bin/env python3
"""Opportunity ("Money on the table" / "COULD SAVE") keeps routing on its own line.

Model-routing potential rests on an assumed share of remaining Opus work
(`routable_fraction`), so it must never be folded into the concrete opportunity
total (unused skills, stale re-reads, full-file Writes). It is shown separately
with the assumption stated, tokens first, and dollars labelled as API-rate.

The dashboard is rendered by executing the shipped renderSavings() in Node with
a stub DOM; the CLI is rendered by calling savings_report().
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
ASSETS = REPO / "skills" / "token-optimizer" / "assets"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
        str(tmp_path / "base" / "token-optimizer-a" / "data"),
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


# ------------------------------------------------------------ payload fields


def test_routing_payload_exposes_assumption_and_token_volume_without_session_rows(measure, monkeypatch):
    monkeypatch.setattr(
        measure, "_model_mix_shares",
        lambda days=30, since=None: {"shares": {"opus": 0.8, "sonnet": 0.2}, "total_tokens": 10_000_000},
    )
    monkeypatch.setattr(measure, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    monkeypatch.setattr(measure, "_ROUTABLE_OPUS_FRACTION", 0.3)

    r = measure._compute_model_routing_savings(days=30)

    assert r["routable_fraction"] == 0.3
    assert r["potential_tokens"] == 2_400_000  # 10M * 0.8 Opus * 0.3
    assert r["potential_tokens_monthly"] == 2_400_000
    assert r["potential_basis"] == "api_rate_equivalent"
    assert r["potential_cost_usd"] > 0
    # Realized routing math is unchanged by the new fields.
    assert r["realized_cost_usd"] > 0


def test_routing_payload_tokens_scale_to_a_month(measure, monkeypatch):
    monkeypatch.setattr(
        measure, "_model_mix_shares",
        lambda days=30, since=None: {"shares": {"opus": 1.0}, "total_tokens": 10_000_000},
    )
    monkeypatch.setattr(measure, "_pretool_baseline_mix", lambda: {"opus": 1.0})
    monkeypatch.setattr(measure, "_ROUTABLE_OPUS_FRACTION", 0.3)

    r = measure._compute_model_routing_savings(days=15)

    assert r["potential_tokens"] == 3_000_000
    assert r["potential_tokens_monthly"] == 6_000_000


def test_routing_unavailable_payload_has_zero_tokens(measure, monkeypatch):
    monkeypatch.setattr(measure, "_model_mix_shares", lambda days=30, since=None: {})
    r = measure._compute_model_routing_savings(days=30)
    assert r["potential_tokens"] == 0
    assert r["potential_cost_usd"] == 0.0
    assert r["evidence"] == "unavailable"


# ------------------------------------------------------------------ CLI


def _cli_summary(routing=None, concrete=True):
    s = {
        "total_tokens": 120_000,
        "total_cost_usd": 12.34,
        "total_events": 5,
        "by_category": {},
        "daily_avg_usd": 0.41,
        "period_days": 30,
        "model_routing": {
            "realized_cost_usd": 2.5,
            "baseline_opus_share": 0.9,
            "current_opus_share": 0.4,
        },
    }
    if concrete:
        s["structural_potential"] = {"cost_saved_usd": 10.0, "unused_skills": 4, "recoverable_tokens": 8000}
        s["output_waste"] = {"cost_saved_usd": 5.0, "avoidable_writes": 3, "writes": 10}
    if routing is not None:
        s["model_routing"].update(routing)
    return s


_ROUTING = {
    "potential_cost_usd": 4000.0,
    "potential_tokens": 900_000_000,
    "potential_tokens_monthly": 900_000_000,
    "routable_fraction": 0.3,
}


def _cli_out(measure, monkeypatch, capsys, summary):
    long_ago = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    monkeypatch.setattr(measure, "_install_date", lambda: long_ago)
    monkeypatch.setattr(measure, "_get_merged_savings", lambda days=30: summary)
    measure.savings_report(days=30)
    return capsys.readouterr().out


def test_cli_routing_is_its_own_line_with_assumption(measure, monkeypatch, capsys):
    out = _cli_out(measure, monkeypatch, capsys, _cli_summary(_ROUTING))
    block = out[out.index("COULD SAVE"):]

    routing_line = next(l for l in block.splitlines() if "Routing" in l)
    assert "if about 30% of your remaining Opus work" in routing_line
    assert "40% of tokens" in routing_line
    assert "ran on lighter models" in routing_line
    # Tokens come first, dollars are labelled as API rates.
    assert routing_line.index("900,000,000 tokens/mo") < routing_line.index("$4000.00/mo")
    assert "at API rates" in routing_line

    # The concrete total sums only the concrete items, never routing.
    total_line = next(l for l in block.splitlines() if l.strip().startswith("= "))
    assert "~$15.00/mo" in total_line
    assert "4015" not in block
    assert "Recovers" not in block


def test_cli_routing_only_has_no_concrete_total(measure, monkeypatch, capsys):
    out = _cli_out(measure, monkeypatch, capsys, _cli_summary(_ROUTING, concrete=False))
    block = out[out.index("COULD SAVE"):]
    assert "Routing" in block
    assert not any(l.strip().startswith("= ") for l in block.splitlines())


def test_cli_single_concrete_item_prints_no_total(measure, monkeypatch, capsys):
    summary = _cli_summary(_ROUTING)
    del summary["output_waste"]
    out = _cli_out(measure, monkeypatch, capsys, summary)
    block = out[out.index("COULD SAVE"):]
    assert not any(l.strip().startswith("= ") for l in block.splitlines())


# ------------------------------------------------------------ dashboard render


def _render_fn_source():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("  function renderSavings() {")
    end = html.index("  function renderHealth() {")
    return html[start:end]


_HARNESS = r"""
const data = {savings: JSON.parse(process.argv[2]), generated_at: null, trends: {}};
function esc(s) { s = s == null ? '' : String(s); return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function runwayCardHtml() { return ''; }
function tokensSavedCardHtml() { return ''; }
const el = {innerHTML: '', querySelectorAll: () => []};
const document = {getElementById: () => el};
"""


def _render(savings):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute the dashboard renderer")
    script = _HARNESS + _render_fn_source() + "\nrenderSavings();\nprocess.stdout.write(el.innerHTML);\n"
    result = subprocess.run(
        [node, "-", json.dumps(savings)], input=script,
        capture_output=True, text=True, check=True, timeout=30,
    )
    return result.stdout


def _text(html):
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"\s+", " ", t.replace("&middot;", "·").replace("&mdash;", "-"))
    return re.sub(r" ([,.:])", r"\1", t)


def _savings(**over):
    s = {
        "period_days": 30,
        "billing_mode": "api",
        "total_cost_usd": 10.0,
        "since_install": {"days": 90, "measured_usd": 10.0, "estimated_usd": 0.0},
        "model_routing": {
            "realized_cost_usd": 0.0,
            "current_opus_share": 0.8,
            "potential_cost_usd": 4071.0,
            "potential_tokens": 900_000_000,
            "potential_tokens_monthly": 900_000_000,
            "routable_fraction": 0.3,
        },
        "structural_potential": {"cost_saved_usd": 20.0, "unused_skills": 5},
        "stale_reads_reclaimable": {"cost_usd": 15.0, "tokens": 120_000, "sessions": 6},
        "output_waste": {"cost_saved_usd": 8.0, "avoidable_writes": 9, "writes": 30},
    }
    s.update(over)
    return s


def _big_number(html):
    m = re.search(r'font-size:76px[^>]*>([^<]+)</span>', html)
    return m.group(1) if m else None


def test_big_number_is_concrete_items_only():
    html = _render(_savings())
    # 20 + 15 + 8 = $43; the $4,071 routing guess must not be in it.
    assert _big_number(html) == "$43.0"
    assert "$4,114" not in html and "$4,071" not in _big_number(html)
    assert "still on the table" in html


def test_routing_line_states_assumption_tokens_first_then_api_rate_dollars():
    text = _text(_render(_savings()))
    assert ("If about 30% of your remaining Opus work ran on lighter models: "
            "~900.0M tokens/mo, about $4,071 at API rates.") in text
    assert "Recovers about" not in text


def test_routing_only_shows_card_without_big_number():
    html = _render(_savings(
        structural_potential={}, stale_reads_reclaimable={}, output_waste={},
    ))
    assert "Money on the table" in html
    assert _big_number(html) is None
    assert "still on the table" not in html
    assert "If about 30% of your remaining Opus work ran on lighter models" in _text(html)


def test_no_routing_potential_means_no_routing_line():
    s = _savings()
    s["model_routing"] = {"realized_cost_usd": 0.0}
    html = _render(s)
    assert _big_number(html) == "$43.0"
    assert "remaining Opus work" not in html


def test_routing_line_uses_recorded_fraction_and_tolerates_missing_tokens():
    s = _savings()
    s["model_routing"].update({"routable_fraction": 0.5, "potential_cost_usd": 100.0})
    del s["model_routing"]["potential_tokens"]
    del s["model_routing"]["potential_tokens_monthly"]
    text = _text(_render(s))
    assert "If about 50% of your remaining Opus work ran on lighter models: about $100 at API rates." in text


def test_billing_caption_is_reused_in_opportunity_card():
    sub = _text(_render(_savings(billing_mode="subscription")))
    api = _text(_render(_savings(billing_mode="api")))
    assert "capacity freed inside your limits" in sub
    assert "$0 on" not in sub
    assert "billed spend you did not incur" in api


def test_routing_fold_lists_each_token_class_and_b_scale_tokens():
    s = _savings()
    s["model_routing"].update({
        "potential_tokens": 13_300_000_000,
        "potential_tokens_monthly": 13_300_000_000,
        "potential_by_class": {
            "cache_read": {"tokens": 13_000_000_000, "usd": 1300.0},
            "output": {"tokens": 40_000_000, "usd": 400.0},
            "cache_create_1h": {"tokens": 150_000_000, "usd": 600.0},
        },
    })
    text = _text(_render(s))
    assert "~13.3B tokens/mo" in text
    assert "How the routing figure is priced" in text
    assert "Cache reads: ~13.0B tokens/mo, about $1,300 at API rates." in text
    assert "Output: ~40.0M tokens/mo, about $400 at API rates." in text
    assert "Cache writes (1 hour): ~150.0M tokens/mo, about $600 at API rates." in text
    # Largest dollar class first.
    assert text.index("Cache reads:") < text.index("Cache writes (1 hour):") < text.index("Output:")


def test_routing_fold_absent_without_class_split():
    text = _text(_render(_savings()))
    assert "How the routing figure is priced" not in text


def test_dashboard_discloses_premium_models_left_out_of_routing():
    s = _savings()
    s["model_routing"].update({"premium_models": ["fable"], "premium_share": 0.061, "premium_cost_usd": 37.4})
    text = _text(_render(s))
    assert ("Premium models you chose (Fable, 6% of tokens, about $37.4/mo at API rates) are left out, "
            "so they do not offset the shift toward lighter models.") in text


def test_dashboard_has_no_premium_line_without_premium_models():
    assert "Premium models you chose" not in _text(_render(_savings()))


def test_cli_discloses_premium_models_left_out_of_routing(measure, monkeypatch, capsys):
    summary = _cli_summary({**_ROUTING, "premium_models": ["fable"], "premium_share": 0.061, "premium_cost_usd": 37.42})
    out = _cli_out(measure, monkeypatch, capsys, summary)
    assert ("premium models you chose (Fable, 6% of tokens, ~$37.42/mo at API rates) "
            "are left out of the routing comparison") in out


def test_dashboard_discloses_unpriced_models_left_out_of_routing():
    s = _savings()
    s["model_routing"].update({"unpriced_models": ["acme/mystery-1"], "unpriced_tokens": 1500000, "free_models": [], "free_tokens": 0})
    text = _text(_render(s))
    assert ("Models without a known price (acme/mystery-1, 1,500,000 tokens) are left out of the routing "
            "comparison, not counted as free.") in text


def test_dashboard_has_no_unpriced_line_when_everything_is_priced():
    assert "Models without a known price" not in _text(_render(_savings()))


def test_cli_discloses_unpriced_models_left_out_of_routing(measure, monkeypatch, capsys):
    summary = _cli_summary({**_ROUTING, "unpriced_models": ["acme/mystery-1"], "unpriced_tokens": 1500000,
                            "free_models": ["vendor/m:free"], "free_tokens": 500000})
    out = _cli_out(measure, monkeypatch, capsys, summary)
    assert ("models without a known price (acme/mystery-1, vendor/m:free; 2,000,000 tokens) "
            "are left out of the routing comparison, not counted as free") in out
