"""The transformation is priced at each user's own current prices, not at one
fixed Opus card, and says so. Synthetic fixtures only.

Also covers: a baseline pinned before class sums were kept gets them rebuilt
without being unpinned (and keeps the fixed card when its transcripts no longer
reproduce it), and the card wording when Token Optimizer was already running in
the baseline month.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "token-optimizer" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _measure import baseline_provenance as bp  # noqa: E402
from _measure import own_price_card as opc  # noqa: E402
from test_baseline_provenance import (  # noqa: E402,F401
    BASELINE, PINNED, Facade, _card, _history, _render, _snapshot, measure)

FIXED = {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}
OPUS = {"fresh_input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_create_5m": 6.25, "cache_create_1h": 10.0}
SONNET = {"fresh_input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_create_5m": 2.5, "cache_create_1h": 4.0}
HAIKU = {"fresh_input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_create_5m": 1.25, "cache_create_1h": 2.0}


class Prices:
    def __init__(self, usage, cards):
        self.usage, self.cards = usage, cards

    def _routing_usage_by_model(self, days=30):
        return self.usage

    def _routing_class_rates(self, model):
        return self.cards.get(model)


def _use(fresh=0, out=0, read=0, w5=0, w1=0):
    return {"fresh_input": fresh, "output": out, "cache_read": read, "cache_create_5m": w5, "cache_create_1h": w1}


def _arm(fresh, out, read, write):
    classes = {"input": fresh, "output": out, "cache_read": read, "cache_write": write}
    return {"classes": classes, "flat_usd": sum(classes[k] * FIXED[k] for k in FIXED) / 1e6}


# --------------------------------------------------------------- the card

def test_single_model_user_gets_that_models_card():
    card = opc.card(Prices({"s": _use(100, 50, 1000, 20)}, {"s": SONNET}), 30)
    assert card == {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}


def test_mixed_user_gets_token_weighted_rates_per_class():
    card = opc.card(Prices({"o": _use(300, 100, 0, 0), "h": _use(100, 100, 0, 0)}, {"o": OPUS, "h": HAIKU}), 30)
    assert card["input"] == pytest.approx((300 * 5 + 100 * 1) / 400)
    assert card["output"] == pytest.approx(15.0)


def test_cache_writes_blend_the_five_minute_and_one_hour_rates_as_used():
    card = opc.card(Prices({"o": _use(10, 10, 10, w5=300, w1=100)}, {"o": OPUS}), 30)
    assert card["cache_write"] == pytest.approx((300 * 6.25 + 100 * 10.0) / 400)


def test_a_class_with_no_tokens_takes_the_average_card_of_the_models_that_ran():
    card = opc.card(Prices({"o": _use(100, 100), "h": _use(100, 100)}, {"o": OPUS, "h": HAIKU}), 30)
    assert card["cache_read"] == pytest.approx(0.3) and card["cache_write"] == pytest.approx(3.75)


def test_unpriced_models_set_no_price_and_no_priced_usage_means_no_card():
    card = opc.card(Prices({"local": _use(9000, 9000), "s": _use(10, 10, 10, 10)}, {"s": SONNET}), 30)
    assert card["input"] == 2.0
    assert opc.card(Prices({"local": _use(9000, 9000)}, {}), 30) is None
    assert opc.card(Prices({}, {}), 30) is None


# ------------------------------------------------------- both arms, one card

@pytest.mark.parametrize("rates, factor", [(SONNET, 2.5), (HAIKU, 5.0)])
def test_lighter_model_users_no_longer_get_opus_dollars(rates, factor):
    before, now = _arm(1000, 4000, 900000, 30000), _arm(800, 3500, 700000, 12000)
    own = opc.apply(Prices({"m": _use(1, 1, 1, 1)}, {"m": rates}), before, now, 30)
    assert own["basis"] == "own_prices"
    assert own["before_usd"] == pytest.approx(before["flat_usd"] / factor)
    assert own["now_usd"] == pytest.approx(now["flat_usd"] / factor)


def test_the_saving_follows_the_classes_that_moved_at_their_own_price():
    # Only cache writes fell. On a card where writes cost more against reads than
    # on the fixed card, the same change is worth more of the bill.
    before, now = _arm(0, 0, 1_000_000, 100_000), _arm(0, 0, 1_000_000, 50_000)
    rates = dict(OPUS, cache_read=0.2, cache_create_5m=5.0)
    own = opc.apply(Prices({"o": _use(1, 1, 1, 1)}, {"o": rates}), before, now, 30)
    assert own["before_usd"] - own["now_usd"] == pytest.approx(50_000 * 5.0 / 1e6)
    assert own["now_usd"] == pytest.approx((1_000_000 * 0.2 + 50_000 * 5.0) / 1e6)


@pytest.mark.parametrize("before, now", [
    ({"flat_usd": 5.0}, _arm(1, 1, 1, 1)),                       # baseline pinned before class sums
    (_arm(1, 1, 1, 1), {"classes": {"input": 1}}),               # malformed
    (_arm(1, 1, 1, 1), {"classes": dict.fromkeys(FIXED, 0.0)}),  # empty
])
def test_without_class_sums_on_both_arms_the_fixed_card_stays(before, now):
    assert opc.apply(Prices({"m": _use(1, 1, 1, 1)}, {"m": SONNET}), before, now, 30) is None


def test_no_priced_usage_keeps_the_fixed_card():
    assert opc.apply(Prices({}, {}), _arm(1, 1, 1, 1), _arm(1, 1, 1, 1), 30) is None


# ------------------------------------------------------------- through measure

def test_priced_rows_carry_class_sums_that_reproduce_the_fixed_card_total(measure):
    mod, _tmp = measure
    rows = [(1_000_000, 20_000, 30_000, 10_000, 0.9, json.dumps({"claude-sonnet-4-6": 1}), None, 12, 8)]
    priced = mod._price_parent_rows(rows, "anthropic")
    classes = priced["classes"]
    assert sum(classes.values()) == pytest.approx(priced["tokens"])
    assert sum(classes[k] * FIXED[k] for k in FIXED) / 1e6 == pytest.approx(priced["flat_usd"])
    assert classes["cache_write"] == 40_000 and classes["cache_read"] == pytest.approx(900_000)


def test_work_normalized_pool_uses_the_users_own_prices(measure):
    mod, tmp = measure
    today = _history(tmp, first_days_ago=100, per_day=4)
    cutoff = (today.replace(hour=0, minute=0, second=0, microsecond=0)).strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    pool = mod._session_weight_pool_savings(cutoff, days=30)
    assert pool is not None, "fixture should give a calendar-month anchor"
    card = pool["price_card"]
    assert card and card["basis"] == "own_prices"
    payload = mod._weight_pool_payload(pool)
    assert payload["price_basis"] == "own_prices" and payload["flat_rates"] == card["rates"]
    # The fixture runs 75% Opus 4.7 and 25% Sonnet 4.6 by tokens: the card must sit
    # strictly between the two, and the "now" arm is the pool's reported actual.
    assert 3.0 < card["rates"]["input"] < 5.0
    assert pool["actual_usd"] == pytest.approx(card["now_usd"])
    assert pool["counterfactual_usd"] - pool["actual_usd"] == pytest.approx(pool["transformation_usd"])


def test_pool_falls_back_to_the_fixed_card_and_says_so(measure, monkeypatch):
    mod, tmp = measure
    from datetime import timedelta
    today = _history(tmp, first_days_ago=100, per_day=4)
    monkeypatch.setattr(mod, "_routing_usage_by_model", lambda days=30, since=None: {})
    pool = mod._session_weight_pool_savings((today - timedelta(days=30)).strftime("%Y-%m-%d"), days=30)
    assert pool["price_card"] is None
    payload = mod._weight_pool_payload(pool)
    assert payload["price_basis"] == "fixed_card" and payload["flat_rates"] == mod._WEIGHT_POOL_FLAT_RATES


# -------------------------------------- a baseline pinned before class sums

class Anchor:
    """Facade stub: three transcripts, each priced from a table."""
    _PRETOOL_ANCHOR_FILE = "workload_anchor_pretool.json"
    _WEIGHT_POOL_FLAT_RATES = FIXED
    _SESSION_WEIGHT_MIN_ANCHOR_SESSIONS = 2
    json, time, datetime = json, time, datetime

    def __init__(self, root, per_file):
        self.SNAPSHOT_DIR, self.per_file = Path(root), per_file

    def _find_all_jsonl_files(self, days):
        return [(name, 0, "p") for name in sorted(self.per_file)]

    def _canonical_session_uuid(self, path):
        return path

    def _pretool_session_row(self, path, start, end, ledger):
        return path

    def _price_parent_rows(self, rows, tier):
        fresh, out, read, write = self.per_file[rows[0]]
        return dict(_arm(fresh, out, read, write), sessions=1, usd=1.0, tokens=fresh + out + read + write,
                    api_calls=10, messages=5)

    def _write_baseline_state(self, path, data):
        path.write_text(json.dumps(data))

    def _load_pricing_tier(self):
        return "anthropic"

    def _pretool_window(self):
        return ("2026-01-06", "2026-02-05")


FILES = {"a.jsonl": (100, 200, 90_000, 4_000), "b.jsonl": (50, 100, 60_000, 2_000), "c.jsonl": (10, 20, 30_000, 500)}


def _pinned(tmp_path, flat_usd):
    state = {"status": "pinned", "window": ["2026-01-06", "2026-02-05"], "rates": FIXED, "activity_basis": 1,
             "metrics": {"sessions": 3, "usd": 3.0, "tokens": 187_000.0, "flat_usd": flat_usd,
                         "api_calls": 30, "messages": 15}, "label": "your earliest tracked month (2026-01-06 to 2026-02-05)"}
    (tmp_path / Anchor._PRETOOL_ANCHOR_FILE).write_text(json.dumps(state))
    return state


def _state(tmp_path):
    return json.loads((tmp_path / Anchor._PRETOOL_ANCHOR_FILE).read_text())


def _true_flat():
    return sum(_arm(*v)["flat_usd"] for v in FILES.values())


def test_a_new_anchor_is_pinned_with_its_class_sums(tmp_path):
    assert opc.anchor_step(Anchor(tmp_path, FILES), budget_seconds=5.0) == "pinned"
    metrics = _state(tmp_path)["metrics"]
    assert metrics["sessions"] == 3 and metrics["flat_usd"] == pytest.approx(_true_flat())
    assert metrics["classes"]["cache_read"] == 180_000 and metrics["classes"]["cache_write"] == 6_500


def test_an_older_pinned_anchor_gets_its_class_split_without_being_unpinned(tmp_path):
    old = _pinned(tmp_path, _true_flat() * 1.01)  # transcripts reproduce it within tolerance
    assert opc.anchor_step(Anchor(tmp_path, FILES), budget_seconds=5.0) == "pinned"
    state = _state(tmp_path)
    kept = {k: state["metrics"][k] for k in old["metrics"]}
    assert kept == old["metrics"], "pinned figures must not move"
    classes = state["metrics"]["classes"]
    assert sum(classes[k] * FIXED[k] for k in FIXED) / 1e6 == pytest.approx(old["metrics"]["flat_usd"])
    assert "class_fill" not in state
    assert opc.classes_of(state["metrics"])


def test_the_rebuild_resumes_across_passes_and_the_anchor_stays_usable_meanwhile(tmp_path):
    _pinned(tmp_path, _true_flat())
    assert opc.anchor_step(Anchor(tmp_path, FILES), budget_seconds=0.0) == "pinned"
    state = _state(tmp_path)
    assert state["status"] == "pinned" and "classes" not in state["metrics"] and "class_fill" in state
    assert opc.anchor_step(Anchor(tmp_path, FILES), budget_seconds=5.0) == "pinned"
    assert opc.classes_of(_state(tmp_path)["metrics"])


def test_transcripts_that_no_longer_reproduce_the_anchor_leave_it_on_the_fixed_card(tmp_path):
    old = _pinned(tmp_path, _true_flat() * 2)  # half the window's transcripts are gone
    facade = Anchor(tmp_path, FILES)
    assert opc.anchor_step(facade, budget_seconds=5.0) == "pinned"
    state = _state(tmp_path)
    assert state["metrics"] == old["metrics"] and state["classes_status"] == "unavailable"
    facade.per_file = {}  # a later pass must not walk again
    assert opc.anchor_step(facade, budget_seconds=5.0) == "pinned"


def test_a_frozen_month_borrows_its_split_only_from_the_same_month_while_it_agrees():
    fresh = _arm(100, 200, 90_000, 4_000)
    frozen = {"flat_usd": fresh["flat_usd"] * 1.01}
    assert opc.lend_classes(frozen, fresh) is True
    assert sum(frozen["classes"][k] * FIXED[k] for k in FIXED) / 1e6 == pytest.approx(frozen["flat_usd"])
    assert opc.lend_classes(frozen, fresh) is False  # already has one
    assert opc.lend_classes({"flat_usd": fresh["flat_usd"] * 1.5}, fresh) is False
    assert opc.lend_classes({"flat_usd": 1.0}, None) is False


# ------------------------------------------------------------------- the page

def _own_card():
    data = _card()
    data["savings"]["before_after"]["session_weight_pool"] = {
        "price_basis": "own_prices",
        "flat_rates": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.75}}
    return data


def test_fold_names_the_users_own_prices():
    fold = _render("savingsTransformationHtml(data)", _own_card()).split("How this is calculated")[1]
    assert "This period, at your current prices: $480.00 a month" in fold
    assert "$2 input, $10 output, $0.2 cache read, $2.75 cache write, averaged over the models you ran" in fold
    assert "fixed" not in fold and "not your bill" not in fold


def test_fold_still_names_the_fixed_card_when_that_is_what_was_used():
    data = _own_card()
    data["savings"]["before_after"]["session_weight_pool"]["price_basis"] = "fixed_card"
    fold = _render("savingsTransformationHtml(data)", data).split("How this is calculated")[1]
    assert "at the same fixed price card" in fold and "your current prices" not in fold


@pytest.mark.parametrize("install_day", ["2026-01-05", "2026-01-20"])  # ran all month, or installed inside it
def test_card_does_not_say_without_token_optimizer_when_it_ran_in_the_baseline(tmp_path, install_day):
    _snapshot(tmp_path, install_day)
    prov = bp.transformation(Facade(tmp_path), BASELINE, PINNED)
    html = _render("savingsTransformationHtml(data)", _card(prov))
    assert "Without Token Optimizer" not in html
    assert "at what each request cost you in your earliest tracked month (Jan 6 to Feb 5)" in html
    assert "before you installed" not in html
    assert "At your earliest month&#39;s cost per request: $600.00 a month" in html


def test_card_keeps_without_token_optimizer_for_a_true_before(tmp_path):
    _snapshot(tmp_path, "2026-02-05")
    prov = bp.transformation(Facade(tmp_path), BASELINE, PINNED)
    assert "claim" not in prov and "versus" not in prov
    html = _render("savingsTransformationHtml(data)", _card(prov))
    assert "at what each request cost you before you installed (Jan 6 to Feb 5)" in html
    assert "Without Token Optimizer: $600.00 a month" in html


def test_per_session_method_names_sessions_not_requests(tmp_path):
    _snapshot(tmp_path, "2026-01-05")
    assert "cost per session" in bp.transformation(Facade(tmp_path), BASELINE, None)["claim"]


def _limits(prov):
    data = _card(prov)
    data["runway"] = {"windows": [{"key": "seven_day", "label": "7d", "used_pct": 60}]}
    return _render("savingsLimitsHtml(data)", data)


def test_limits_card_says_when_token_optimizer_ran_in_the_baseline(tmp_path):
    """The approved card always labels the bar "Without Token Optimizer". When the tool
    already ran in the baseline month, the basis and the fold must say so, and that the
    real gap is larger: the shown figure may undersell, never oversell."""
    _snapshot(tmp_path, "2026-01-05")
    html = _limits(bp.transformation(Facade(tmp_path), BASELINE, PINNED))
    assert "Compared with your earliest tracked month (Jan 6 to Feb 5)." in html
    assert "before you installed" not in html
    assert "already running in that month, so the full gap is larger than shown" in html
    _snapshot(tmp_path, "2026-02-05")
    html = _limits(bp.transformation(Facade(tmp_path), BASELINE, PINNED))
    assert "before you installed (Jan 6 to Feb 5)" in html
    assert "already running" not in html
