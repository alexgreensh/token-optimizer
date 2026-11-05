#!/usr/bin/env python3
"""Model routing is priced per token class at each model's own generation card.

Realized routing prices each token class separately. Potential routing covers
fresh input and output, or unsplit usage priced at the input rate.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

OPUS_55 = "claude-opus-5-5"
OPUS_41 = "claude-opus-4-1-20250805"
SONNET_55 = "claude-sonnet-5-5"


@pytest.fixture()
def m(tmp_path, monkeypatch):
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
    monkeypatch.setattr(mod, "_ROUTABLE_OPUS_FRACTION", 0.3)
    yield mod
    sys.modules.pop("measure", None)


def _cards(m):
    return m.PRICING_TIERS[m._load_pricing_tier()]["claude_models"]


class _Db:
    def __init__(self, m, tmp_path, monkeypatch):
        self.path = tmp_path / "trends.db"
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(
            """
            CREATE TABLE session_log (id INTEGER PRIMARY KEY, date TEXT,
                daily_usage_json TEXT, model_usage_breakdown_json TEXT,
                all_model_usage_json TEXT, model_usage_json TEXT, first_activity_day TEXT, last_activity_day TEXT);
            CREATE TABLE model_daily (date TEXT, model TEXT, total_tokens INTEGER,
                PRIMARY KEY (date, model));
            """
        )
        self.conn.commit()
        path = self.path
        monkeypatch.setattr(m, "TRENDS_DB", path)
        monkeypatch.setattr(m, "_init_trends_db", lambda: sqlite3.connect(str(path)))

    def day(self, offset=0):
        return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")

    def session(self, daily=None, breakdown=None, totals=None, date=None):
        self.conn.execute(
            "INSERT INTO session_log (date, daily_usage_json, model_usage_breakdown_json, "
            "all_model_usage_json, model_usage_json, first_activity_day, last_activity_day) VALUES (?,?,?,?,?,?,?)",
            (date or self.day(0),
             json.dumps(daily) if daily is not None else None,
             json.dumps(breakdown) if breakdown is not None else None,
             json.dumps(totals) if totals is not None else None,
             None, date or self.day(0), date or self.day(0)),
        )
        self.conn.commit()

    def mix(self, **tokens):
        for model, n in tokens.items():
            self.conn.execute("INSERT INTO model_daily VALUES (?,?,?)", (self.day(0), model, n))
        self.conn.commit()


@pytest.fixture()
def db(m, tmp_path, monkeypatch):
    # The baseline mix is not under test here unless a test sets it.
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.5, "sonnet": 0.5})
    return _Db(m, tmp_path, monkeypatch)


def _buckets(fresh=0, read=0, cc=0, cc_1h=0, cc_5m=0, out=0):
    return {"fresh_input": fresh, "cache_read": read, "cache_create": cc,
            "cache_create_1h": cc_1h, "cache_create_5m": cc_5m, "output": out}


# ------------------------------------------------------------ class pricing


def test_potential_excludes_all_cache_classes(m, db):
    db.session(daily={db.day(): {OPUS_55: _buckets(
        fresh=1_000_000, read=10_000_000, cc=4_000_000,
        cc_1h=1_000_000, cc_5m=3_000_000, out=2_000_000)}})
    db.mix(opus=7_000_000)
    c = _cards(m)
    o, target = c["opus_5_5"], c["sonnet_5_5"]
    r = m._compute_model_routing_savings(days=30)
    assert r["potential_cost_usd"] == pytest.approx(
        0.3 * ((o["input"] - target["input"]) + 2 * (o["output"] - target["output"])), abs=1e-3)
    assert r["potential_tokens"] == 900_000
    assert r["potential_billable_tokens"] == 900_000
    assert set(r["potential_by_class"]) == {"fresh_input", "output"}
    assert r["potential_token_classes"] == ["fresh_input", "output"]


def test_cache_only_has_no_potential_but_realized_is_unchanged(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    db.session(daily={db.day(): {
        OPUS_55: _buckets(read=10_000_000, cc=4_000_000, cc_5m=4_000_000),
        SONNET_55: _buckets(read=10_000_000, cc=4_000_000, cc_5m=4_000_000),
    }})
    db.mix(opus=14_000_000, sonnet=14_000_000)
    r = m._compute_model_routing_savings(days=30)
    monkeypatch.setattr(m, "_ROUTABLE_OPUS_FRACTION", 0.7)
    override = m._compute_model_routing_savings(days=30)
    assert r["potential_cost_usd"] == override["potential_cost_usd"] == 0
    assert r["potential_tokens"] == override["potential_tokens"] == 0
    assert r["realized_cost_usd"] > 0
    assert r["realized_cost_usd"] == override["realized_cost_usd"]


def test_monthly_tokens_scale_the_window(m, db):
    db.session(daily={db.day(): {OPUS_55: _buckets(out=10_000_000)}})
    db.mix(opus=10_000_000)

    r = m._compute_model_routing_savings(days=15)

    assert r["potential_tokens"] == 3_000_000
    assert r["potential_tokens_monthly"] == 6_000_000


def test_days_outside_the_window_are_excluded(m, db):
    db.session(daily={
        db.day(0): {OPUS_55: _buckets(out=1_000_000)},
        db.day(40): {OPUS_55: _buckets(out=50_000_000)},
    })
    db.mix(opus=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["potential_tokens"] == 300_000


# -------------------------------------------------------- generation cards


def test_each_opus_generation_uses_its_own_card(m, db):
    db.session(daily={db.day(): {
        OPUS_55: _buckets(out=1_000_000),
        OPUS_41: _buckets(out=1_000_000),
    }})
    db.mix(opus=2_000_000)
    c = _cards(m)
    target = c["sonnet_5_5"]

    r = m._compute_model_routing_savings(days=30)

    expected = 0.3 * ((c["opus_5_5"]["output"] - target["output"]) + (c["opus_4_1"]["output"] - target["output"]))
    assert c["opus_4_1"]["output"] != c["opus_5_5"]["output"]
    assert r["potential_cost_usd"] == pytest.approx(expected, abs=1e-3)


def test_lighter_arm_is_the_newest_sonnet_card(m):
    assert m._routing_target_model() == "claude-sonnet-5-5"
    assert m._claude_price_key(m._routing_target_model(), _cards(m)) == "sonnet_5_5"


def test_class_rates_follow_the_generation_card(m):
    c = _cards(m)["opus_5_5"]
    rates = m._routing_class_rates(OPUS_55)
    assert rates["fresh_input"] == pytest.approx(c["input"])
    assert rates["output"] == pytest.approx(c["output"])
    assert rates["cache_read"] == pytest.approx(c["cache_read"])
    assert rates["cache_create_5m"] == pytest.approx(c["cache_write"])
    assert rates["cache_create_1h"] == pytest.approx(c["cache_write_1h"])
    assert rates["unsplit"] == rates["fresh_input"]


# ------------------------------------------------------- unsplit fallback


def test_rows_without_a_class_split_stay_out_of_the_routing_potential(m, db):
    db.session(totals={OPUS_55: 4_000_000})
    db.mix(opus=4_000_000)
    c = _cards(m)

    r = m._compute_model_routing_savings(days=30)

    assert r["unsplit_tokens"] == 4_000_000
    assert "unsplit" not in r["potential_by_class"]
    assert r["potential_cost_usd"] == 0
    assert r["potential_unsplit_excluded_tokens"] == 4_000_000


def test_breakdown_rows_keep_their_split_and_flag_the_rest(m, db):
    db.session(
        breakdown={OPUS_55: _buckets(out=1_000_000)},
        totals={OPUS_55: 3_000_000},  # includes 2M of subagent work with no split
    )
    db.mix(opus=3_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["unsplit_tokens"] == 2_000_000
    assert r["potential_by_class"]["output"]["tokens"] == 300_000
    assert "unsplit" not in r["potential_by_class"]
    assert r["potential_unsplit_excluded_tokens"] == 2_000_000


def test_split_rows_report_no_unsplit_tokens(m, db):
    db.session(daily={db.day(): {OPUS_55: _buckets(out=1_000_000)}})
    db.mix(opus=1_000_000)
    assert m._compute_model_routing_savings(days=30)["unsplit_tokens"] == 0


def test_no_session_rows_falls_back_to_the_model_mix(m, db):
    db.mix(opus=4_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["unsplit_tokens"] == 4_000_000
    assert r["potential_tokens"] == 0
    assert r["potential_cost_usd"] == 0
    assert r["potential_unsplit_excluded_tokens"] == 4_000_000


# ------------------------------------------------------------ realized


def test_realized_prices_the_mix_shift_per_class(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    db.session(daily={db.day(): {OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000)}})
    db.mix(opus=1_000_000, sonnet=1_000_000)
    c = _cards(m)

    r = m._compute_model_routing_savings(days=30)

    out_o, out_s = c["opus_5_5"]["output"], c["sonnet_5_5"]["output"]
    expected = 2.0 * ((0.95 - 0.5) * out_o + (0.05 - 0.5) * out_s)
    assert r["realized_cost_usd"] == pytest.approx(expected, abs=1e-3)
    assert r["realized_cost_usd"] > 0


def test_realized_survives_a_missing_target_rate(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    monkeypatch.setattr(m, "_routing_target_model", lambda: "no-such-model-1")
    db.session(daily={db.day(): {OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000)}})
    db.mix(opus=1_000_000, sonnet=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["realized_cost_usd"] > 0
    assert r["potential_cost_usd"] == 0
    assert r["potential_by_class"] == {}
    assert r["evidence"] == "estimated"


def test_realized_is_never_negative(m, db, monkeypatch):
    # More Opus now than in the baseline: a shift the wrong way reads zero.
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.2, "sonnet": 0.8})
    db.session(daily={db.day(): {OPUS_55: _buckets(out=9_000_000), SONNET_55: _buckets(out=1_000_000)}})
    db.mix(opus=9_000_000, sonnet=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["realized_cost_usd"] == 0.0
    assert r["potential_cost_usd"] > 0


def test_realized_ignores_price_changes_between_eras(m, db, monkeypatch):
    # Same mix as the baseline: nothing moved, so nothing is saved, whatever
    # the cards say.
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.5, "sonnet": 0.5})
    db.session(daily={db.day(): {OPUS_55: _buckets(read=5_000_000, out=1_000_000),
                                 SONNET_55: _buckets(read=5_000_000, out=1_000_000)}})
    db.mix(opus=1_000_000, sonnet=1_000_000)

    assert m._compute_model_routing_savings(days=30)["realized_cost_usd"] == 0.0


def test_no_usage_at_all_reports_unavailable(m, db):
    r = m._compute_model_routing_savings(days=30)
    assert r["evidence"] == "unavailable"
    assert r["potential_by_class"] == {}
    assert r["potential_tokens"] == 0


# ---------------------------------------------- premium models are the user's choice

FABLE = "claude-fable-5-1"


def _out_rates(m):
    c = _cards(m)
    return c["opus_5_5"]["output"], c["sonnet_5_5"]["output"], m._routing_class_rates(FABLE)["output"]


def test_premium_model_is_excluded_from_realized_routing(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    o, s, f = _out_rates(m)
    assert f > o, "fixture assumes the premium model costs more than Opus"
    db.session(daily={db.day(): {
        OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000), FABLE: _buckets(out=1_000_000)}})
    db.mix(opus=1_000_000, sonnet=1_000_000, fable=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    # Opus/Sonnet alone are 50/50 once Fable is out: the same shift as a window with no Fable.
    expected = 2.0 * ((0.95 - 0.5) * o + (0.05 - 0.5) * s)
    assert expected > 0
    assert r["realized_cost_usd"] == pytest.approx(expected, abs=1e-3)
    assert r["premium_models"] == ["fable"]
    assert r["premium_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert r["premium_cost_usd"] == pytest.approx(f, abs=1e-3)  # 1M output tokens


def test_remaining_mix_is_renormalized_without_premium(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    o, s, _ = _out_rates(m)
    db.session(daily={db.day(): {
        OPUS_55: _buckets(out=2_000_000), SONNET_55: _buckets(out=1_000_000), FABLE: _buckets(out=1_000_000)}})
    db.mix(opus=2_000_000, sonnet=1_000_000, fable=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    # Kept shares: opus 2/3, sonnet 1/3 over the 3M non-premium output tokens.
    expected = 3.0 * ((0.95 - 2 / 3) * o + (0.05 - 1 / 3) * s)
    assert r["realized_cost_usd"] == pytest.approx(expected, abs=1e-3)
    assert r["premium_share"] == pytest.approx(0.25, abs=1e-3)


def test_without_a_premium_model_realized_math_is_unchanged(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    o, s, _ = _out_rates(m)
    db.session(daily={db.day(): {OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000)}})
    db.mix(opus=1_000_000, sonnet=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["realized_cost_usd"] == pytest.approx(2.0 * ((0.95 - 0.5) * o + (0.05 - 0.5) * s), abs=1e-3)
    assert r["premium_models"] == []
    assert r["premium_share"] == 0.0
    assert r["premium_cost_usd"] == 0.0


def test_a_model_the_baseline_already_used_is_not_premium(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.5, "fable": 0.5})
    db.session(daily={db.day(): {OPUS_55: _buckets(out=1_000_000), FABLE: _buckets(out=1_000_000)}})
    db.mix(opus=1_000_000, fable=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["premium_models"] == []
    assert r["premium_share"] == 0.0


def test_realized_with_premium_is_never_negative(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.2, "sonnet": 0.8})
    db.session(daily={db.day(): {
        OPUS_55: _buckets(out=9_000_000), SONNET_55: _buckets(out=1_000_000), FABLE: _buckets(out=5_000_000)}})
    db.mix(opus=9_000_000, sonnet=1_000_000, fable=5_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["realized_cost_usd"] == 0.0
    assert r["premium_share"] > 0


def test_all_premium_usage_reports_no_realized_routing(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    db.session(daily={db.day(): {FABLE: _buckets(out=1_000_000)}})
    db.mix(fable=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["realized_cost_usd"] == 0.0
    assert r["premium_share"] == pytest.approx(1.0)


def test_premium_fields_in_unavailable_payload(m, db):
    r = m._compute_model_routing_savings(days=30)
    assert r["premium_models"] == [] and r["premium_share"] == 0.0 and r["premium_cost_usd"] == 0.0


# ------------------------------------------- actual arm prices actual usage


def test_realized_prices_each_models_own_class_volumes(m, db, monkeypatch):
    # Opus ran output, Sonnet ran fresh input. Baseline cost: 95% Opus on the
    # whole volume + 5% Sonnet = 0.95 * (20 + 4) + 0.05 * (10 + 2). Actual cost:
    # 20 + 2. A global model share applied to every class would invent $18.
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    db.session(daily={db.day(): {OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(fresh=1_000_000)}})
    db.mix(opus=1_000_000, sonnet=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    assert r["realized_cost_usd"] == pytest.approx(0.95 * (20 + 4) + 0.05 * (10 + 2) - (20 + 2), abs=1e-3)


def test_cache_read_distribution_is_priced_from_the_models_that_read_it(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    c = _cards(m)
    o, s = c["opus_5_5"], c["sonnet_5_5"]
    db.session(daily={db.day(): {OPUS_55: _buckets(read=100_000_000), SONNET_55: _buckets(out=1_000_000)}})
    db.mix(opus=100_000_000, sonnet=1_000_000)

    r = m._compute_model_routing_savings(days=30)

    totals = {"read": 100.0, "out": 1.0}
    baseline = 0.95 * (totals["read"] * o["cache_read"] + totals["out"] * o["output"]) \
        + 0.05 * (totals["read"] * s["cache_read"] + totals["out"] * s["output"])
    actual = totals["read"] * o["cache_read"] + totals["out"] * s["output"]
    assert r["realized_cost_usd"] == pytest.approx(max(0.0, baseline - actual), abs=1e-3)


# --------------------------------------------------------- mixed generations


def _realized(m, db, usage, baseline=None, mix=None):
    db.session(daily={db.day(): usage})
    db.mix(**(mix or {"opus": 1_000_000, "sonnet": 1_000_000}))
    return m._compute_model_routing_savings(days=30)


def test_generation_order_does_not_change_realized(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    usage_a = {OPUS_41: _buckets(out=1_000_000), OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=2_000_000)}
    usage_b = {OPUS_55: usage_a[OPUS_55], OPUS_41: usage_a[OPUS_41], SONNET_55: usage_a[SONNET_55]}
    a = _realized(m, db, usage_a)["realized_cost_usd"]
    db.conn.execute("DELETE FROM session_log"); db.conn.execute("DELETE FROM model_daily"); db.conn.commit()
    b = _realized(m, db, usage_b)["realized_cost_usd"]

    # Standardized: every family at its newest card, so Opus 4.1 counts as Opus 5.5.
    c = _cards(m)
    o, s = c["opus_5_5"]["output"], c["sonnet_5_5"]["output"]
    expected = 4 * (0.95 * o + 0.05 * s) - (2 * o + 2 * s)
    assert a == b
    assert a == pytest.approx(expected, abs=1e-3)


def _reset(db):
    db.conn.execute("DELETE FROM session_log"); db.conn.execute("DELETE FROM model_daily"); db.conn.commit()


def test_routing_is_valued_on_one_reference_schedule_and_disclosed(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    r = _realized(m, db, {SONNET_55: _buckets(out=1_000_000)})
    c = _cards(m)
    assert r["realized_cost_usd"] == pytest.approx(
        0.95 * c["opus_5_5"]["output"] + 0.05 * c["sonnet_5_5"]["output"] - c["sonnet_5_5"]["output"], abs=1e-3)
    assert r["realized_basis"] == "standardized_reference_card"
    assert r["baseline_rate_basis"] == {"opus": "reference:claude-opus-5-5", "sonnet": "reference:claude-sonnet-5-5"}
    assert r["baseline_reference_priced"] == ["opus", "sonnet"]
    assert r["evidence"] == "estimated"


@pytest.mark.parametrize("model", [OPUS_55, OPUS_41, "claude-opus-4-8"])
def test_one_token_of_any_generation_changes_routing_only_by_its_own_contribution(m, db, monkeypatch, model):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    absent = _realized(m, db, {SONNET_55: _buckets(out=1_000_000)})["realized_cost_usd"]
    _reset(db)
    r = _realized(m, db, {SONNET_55: _buckets(out=1_000_000), model: _buckets(out=1)})
    # The token is valued at the Opus reference card whatever generation it ran on.
    assert r["realized_cost_usd"] == pytest.approx(absent, abs=1e-4)
    assert r["baseline_rate_basis"]["opus"] == "reference:claude-opus-5-5"


def test_equal_volumes_with_only_generation_ids_changed_give_identical_routing(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    volumes = dict(read=7_000_000, cc=2_000_000, cc_5m=2_000_000, out=1_000_000)
    results = []
    for opus, sonnet in ((OPUS_55, SONNET_55), (OPUS_41, "claude-sonnet-4-6"), ("claude-opus-4-8", "claude-sonnet-4-5")):
        _reset(db)
        r = _realized(m, db, {opus: _buckets(**volumes), sonnet: _buckets(**volumes)},
                      mix={"opus": 10_000_000, "sonnet": 10_000_000} if sonnet == SONNET_55 else
                          {"opus": 10_000_000, "sonnet_legacy": 10_000_000})
        results.append(r["realized_cost_usd"])
    assert results[1] == pytest.approx(results[0], abs=1e-6)
    assert results[2] == pytest.approx(results[0], abs=1e-6)


def test_actual_generation_cost_is_reported_apart_from_routing(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    r = _realized(m, db, {OPUS_41: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000)})
    c = _cards(m)
    assert r["actual_cost_usd"] == pytest.approx(c["opus_4_1"]["output"] + c["sonnet_5_5"]["output"], abs=1e-3)
    assert r["standardized_actual_cost_usd"] == pytest.approx(c["opus_5_5"]["output"] + c["sonnet_5_5"]["output"], abs=1e-3)
    assert r["generation_price_delta_usd"] == pytest.approx(c["opus_4_1"]["output"] - c["opus_5_5"]["output"], abs=1e-3)
    # Realized routing is the standardized difference only.
    assert r["realized_cost_usd"] == pytest.approx(
        r["standardized_baseline_cost_usd"] - r["standardized_actual_cost_usd"], abs=1e-3)


def test_near_tied_generations_price_continuously(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    even = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), OPUS_41: _buckets(out=1_000_000),
                             SONNET_55: _buckets(out=2_000_000)})["realized_cost_usd"]
    db.conn.execute("DELETE FROM session_log"); db.conn.execute("DELETE FROM model_daily"); db.conn.commit()
    tip = _realized(m, db, {OPUS_55: _buckets(out=1_000_001), OPUS_41: _buckets(out=1_000_000),
                            SONNET_55: _buckets(out=2_000_000)})["realized_cost_usd"]
    assert tip == pytest.approx(even, abs=0.01)


def test_price_change_between_eras_is_not_attributed_to_routing(m, db, monkeypatch):
    # Baseline and current share the same mix; whichever generation the family
    # ran on, nothing moved so nothing is saved.
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.5, "sonnet": 0.5})
    r = _realized(m, db, {OPUS_41: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000)})
    assert r["realized_cost_usd"] == 0.0


# ------------------------------------------------------------ unpriced models


def test_unknown_model_is_unpriced_not_free(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 1.0})
    r = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), "acme/mystery-1": _buckets(out=1_000_000)},
                  mix={"opus": 1_000_000, "acme/mystery-1": 1_000_000})
    assert r["realized_cost_usd"] == 0.0
    assert r["unpriced_models"] == ["acme/mystery-1"]
    assert r["unpriced_tokens"] == 1_000_000
    assert r["unpriced_share"] == pytest.approx(0.5)


def test_unknown_model_does_not_change_the_priced_comparison(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    base = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000)})
    db.conn.execute("DELETE FROM session_log"); db.conn.execute("DELETE FROM model_daily"); db.conn.commit()
    with_unknown = _realized(
        m, db,
        {OPUS_55: _buckets(out=1_000_000), SONNET_55: _buckets(out=1_000_000), "acme/mystery-1": _buckets(out=9_000_000)},
        mix={"opus": 1_000_000, "sonnet": 1_000_000, "acme/mystery-1": 9_000_000})
    assert with_unknown["realized_cost_usd"] == base["realized_cost_usd"]
    assert with_unknown["potential_cost_usd"] == base["potential_cost_usd"]
    assert with_unknown["unpriced_share"] == pytest.approx(9 / 11, abs=1e-3)


def test_unknown_baseline_family_is_left_out_of_the_baseline(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.5, "acme/old-thing": 0.5})
    r = _realized(m, db, {OPUS_55: _buckets(out=1_000_000)}, mix={"opus": 1_000_000})
    assert r["realized_cost_usd"] == 0.0


def test_unknown_claude_generation_is_unpriced(m, db, monkeypatch):
    assert m._routing_class_rates("claude-opus-99") is None
    assert m._routing_class_rates("claude-fable-9") is None
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 1.0})
    r = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), "claude-opus-99": _buckets(out=1_000_000)},
                  mix={"opus": 2_000_000})
    assert r["unpriced_models"] == ["claude-opus-99"]
    assert r["potential_tokens"] == 300_000, "only the priced Opus model is routable"


def test_known_generations_and_family_labels_stay_priced(m):
    for model in (OPUS_55, OPUS_41, SONNET_55, "claude-sonnet-4-6", "claude-sonnet-4-20250514",
                  "claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022", "opus", "sonnet_legacy", FABLE):
        assert m._routing_class_rates(model) is not None, model


def test_free_tier_models_are_reported_apart_and_never_credited(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 1.0})
    r = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), "vendor/model:free": _buckets(out=4_000_000)},
                  mix={"opus": 1_000_000, "vendor/model:free": 4_000_000})
    assert r["free_models"] == ["vendor/model:free"]
    assert r["free_tokens"] == 4_000_000
    assert r["unpriced_models"] == []
    assert r["realized_cost_usd"] == 0.0


def test_only_unknown_models_report_coverage_and_no_dollars(m, db):
    r = _realized(m, db, {"acme/mystery-1": _buckets(out=1_000_000)}, mix={"acme/mystery-1": 1_000_000})
    assert r["realized_cost_usd"] == 0.0 and r["potential_cost_usd"] == 0.0
    assert r["unpriced_models"] == ["acme/mystery-1"] and r["unpriced_share"] == 1.0


def test_coverage_fields_in_unavailable_payload(m, db):
    r = m._compute_model_routing_savings(days=30)
    assert r["unpriced_models"] == [] and r["unpriced_tokens"] == 0 and r["free_models"] == []


# ------------------------------------------ premium with a cache-heavy window


def test_cache_heavy_premium_model_is_excluded_and_costed(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    f = _cards(m)["fable_5_1"]
    r = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), FABLE: _buckets(out=1_000_000, read=500_000_000)},
                  mix={"opus": 1_000_000, "fable": 501_000_000})
    assert r["premium_models"] == ["fable"]
    assert r["premium_cost_usd"] == pytest.approx(f["output"] + 500 * f["cache_read"], abs=1e-3)
    assert r["realized_cost_usd"] == 0.0


def test_older_lighter_model_is_not_premium_on_cache_reads_alone(m, db, monkeypatch):
    # Sonnet 4.6 reads cache dearer than Opus 5.5, yet it is the lighter model.
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.95, "sonnet": 0.05})
    c = _cards(m)
    assert c["sonnet_4_6"]["cache_read"] > c["opus_5_5"]["cache_read"]
    r = _realized(m, db, {OPUS_55: _buckets(read=100_000_000, out=1_000_000),
                          "claude-sonnet-4-6": _buckets(read=100_000_000, out=1_000_000)},
                  mix={"opus": 1_000_000, "sonnet_legacy": 1_000_000})
    assert r["premium_models"] == []


def test_moving_to_a_newer_cheaper_generation_is_not_routing(m, db, monkeypatch):
    # The earlier mix ran legacy Sonnet and Fable; the window is cache-read heavy
    # Opus 5.5. Legacy Sonnet reads cache dearer than Opus 5.5, but the shift is
    # toward the heavier model, so nothing is saved.
    monkeypatch.setattr(m, "_pretool_baseline_mix",
                        lambda: {"opus": 0.57, "sonnet_legacy": 0.33, "fable": 0.07, "haiku": 0.03})
    r = _realized(m, db, {OPUS_55: _buckets(read=900_000_000, out=5_000_000)},
                  mix={"opus": 905_000_000})
    assert r["realized_cost_usd"] == 0.0


def test_legacy_sonnet_family_is_priced_at_the_sonnet_reference_when_absent(m):
    assert m._routing_reference_model("sonnet_legacy") == m._routing_reference_model("sonnet")
    assert m._routing_reference_model("opus") == "claude-opus-5-5"


def test_generation_price_change_alone_is_zero_in_either_direction(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 0.5, "sonnet_legacy": 0.5})
    older = _realized(m, db, {OPUS_41: _buckets(out=1_000_000, read=10_000_000),
                              "claude-sonnet-4-6": _buckets(out=1_000_000, read=10_000_000)},
                      mix={"opus": 1_000_000, "sonnet_legacy": 1_000_000})
    assert older["realized_cost_usd"] == 0.0
    db.conn.execute("DELETE FROM session_log"); db.conn.execute("DELETE FROM model_daily"); db.conn.commit()
    newer = _realized(m, db, {OPUS_55: _buckets(out=1_000_000, read=10_000_000),
                              SONNET_55: _buckets(out=1_000_000, read=10_000_000)},
                      mix={"opus": 1_000_000, "sonnet": 1_000_000})
    assert newer["realized_cost_usd"] == 0.0


# ------------------------------------------------- exact generation cards only


@pytest.mark.parametrize("model", [
    "claude-opus-99",
    "claude-opus-5-99",          # opus_5 exists, opus_5_99 does not
    "claude-opus-5-99-20260101",
    "claude-sonnet-4-9",
    "claude-sonnet-4-9-20260101",
    "claude-fable-5-9",
    "claude-mythos-9",
    "claude-3-9-opus",           # no such legacy card
    "claude-haiku-5-99",
])
def test_unknown_generation_never_borrows_a_broader_card(m, model):
    assert m._routing_model_status(model) == "unpriced", model
    assert m._routing_class_rates(model) is None


@pytest.mark.parametrize("model", [
    "claude-opus-5-5-20260301",       # dated id of a known generation
    "claude-opus-5",                  # the Opus 5 card exists
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1",
    "claude-mythos-5-1",              # Mythos shares the Fable cards
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
    "claude-3-sonnet-20240229",       # legacy Sonnet 3 / 3.5 / 3.7 aliases
    "claude-3-5-sonnet-20241022",
    "claude-3-7-sonnet-20250219",
])
def test_known_generations_including_dated_ids_are_exact(m, model):
    assert m._routing_model_status(model) == "priced", model
    assert m._routing_class_rates(model) is not None


def test_unknown_minor_generation_is_reported_not_priced_as_its_major(m, db, monkeypatch):
    monkeypatch.setattr(m, "_pretool_baseline_mix", lambda: {"opus": 1.0})
    r = _realized(m, db, {OPUS_55: _buckets(out=1_000_000), "claude-opus-5-99": _buckets(out=1_000_000)},
                  mix={"opus": 2_000_000})
    assert r["unpriced_models"] == ["claude-opus-5-99"]
    assert r["potential_tokens"] == 300_000

@pytest.mark.parametrize('model', ['gpt-5.4', 'gemini-2.5-pro'])
def test_aggregate_rate_cards_do_not_trigger_long_prompt_surcharge(m, model):
    base = m._get_model_cost(model, 1, 0) * 1_000_000
    assert m._routing_class_rates(model)['fresh_input'] == pytest.approx(base)
    assert m._compression_row_rate(model, 0, {}) == pytest.approx(base)
    assert m._compression_mix_input_terms(json.dumps({model: 1}))[0][1] == pytest.approx(base)
    assert m._get_model_cost(model, 1_000_000, 0) > base

@pytest.mark.parametrize('model', ['gpt-5.4-unlisted-variant', 'gpt-5.6-unlisted-variant', 'gemini-2.5-pro-unlisted-variant'])
def test_foreign_unknown_variants_are_not_priced(m, model):
    assert not m._routing_card_exact(model)
    assert not m._compression_known_model(model)

@pytest.mark.parametrize('model', ['openai/gpt-5.4-2026-03-05', 'google/gemini-2.5-pro', 'gpt-5.6'])
def test_foreign_documented_aliases_remain_priced(m, model):
    assert m._routing_card_exact(model)
    assert m._compression_known_model(model)
