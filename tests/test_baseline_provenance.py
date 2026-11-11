"""Every "before" baseline says where it came from and whether Token Optimizer
was already running inside it. Synthetic fixtures only.

Covers the user types the Savings page must be honest for: a fresh install with
older transcripts (a true before), a fresh install with none (the earliest
month already has the tool), an install part-way through the window, and a
ledger with no proof of the install date. Also: the routing estimate uses the
frozen baseline before the earliest days still in the ledger, the install date
uses the earliest proof on disk, and no refactor artifact reaches user copy.
"""
import importlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
STORY = ROOT / "skills" / "token-optimizer" / "assets" / "savings_story.js"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _measure import baseline_provenance as bp  # noqa: E402


class Facade:
    """The few facade attributes the provenance module reads."""
    _PRETOOL_ANCHOR_FILE = "workload_anchor_pretool.json"

    def __init__(self, root, early=None):
        self.SNAPSHOT_DIR = Path(root)
        self.TRENDS_DB = Path(root) / "trends.db"
        self._early = early

    def _open_trends_db_readonly(self, timeout=1.0):
        return sqlite3.connect(str(self.TRENDS_DB))

    def _init_trends_db(self):
        return sqlite3.connect(str(self.TRENDS_DB))

    def ensure_activity_view(self, conn):
        pass

    def _earliest_model_mix(self, lookback_days=14):
        return self._early


def _db(root, sessions=(), events=()):
    conn = sqlite3.connect(str(Path(root) / "trends.db"))
    conn.execute("CREATE TABLE session_activity (date TEXT)")
    conn.execute("CREATE TABLE savings_events (timestamp TEXT)")
    conn.execute("CREATE TABLE compression_events (timestamp TEXT)")
    conn.executemany("INSERT INTO session_activity VALUES (?)", [(d,) for d in sessions])
    conn.executemany("INSERT INTO savings_events VALUES (?)", [(t,) for t in events])
    conn.commit()
    conn.close()


def _snapshot(root, day, mix=None):
    body = {"timestamp": day + "T09:00:00"}
    if mix:
        body["model_mix"] = {"shares": mix, "activity_basis": 1}
    (Path(root) / "snapshot_before.json").write_text(json.dumps(body))


# ---------------------------------------------------------------- install date

def test_install_date_is_the_first_run_snapshot(tmp_path):
    _db(tmp_path, sessions=["2026-01-05"], events=["2026-02-03T10:00:00"])
    _snapshot(tmp_path, "2026-02-01")
    assert bp.install_evidence(Facade(tmp_path)) == {"date": "2026-02-01", "source": "snapshot"}


def test_a_retaken_snapshot_does_not_move_the_install_date_later(tmp_path):
    _db(tmp_path, sessions=["2026-01-05"], events=["2026-02-03T10:00:00"])
    _snapshot(tmp_path, "2026-05-20")
    assert bp.install_evidence(Facade(tmp_path)) == {"date": "2026-02-03", "source": "first_event"}


def test_earliest_session_is_a_fallback_date_but_not_proof_of_install(tmp_path):
    _db(tmp_path, sessions=["2026-01-05", "2026-01-09"])
    install = bp.install_evidence(Facade(tmp_path))
    assert install == {"date": "2026-01-05", "source": "earliest_session"}
    assert bp.window_coverage("2026-01-06", "2026-02-05", install)["coverage"] == "unknown"


def test_no_data_at_all(tmp_path):
    assert bp.install_evidence(Facade(tmp_path)) == {"date": None, "source": None}


# ------------------------------------------------------------ window coverage

@pytest.mark.parametrize("install_day, expected, post", [
    ("2026-02-05", "pre_install", 1),    # fresh install, 30 days of older transcripts
    ("2026-03-01", "pre_install", 0),    # installed after the window closed
    ("2026-01-20", "partial", 17),       # installed part-way through
    ("2026-01-06", "post_install", 31),  # no older transcripts: the tool ran all month
    ("2025-12-01", "post_install", 31),  # ledger rebuilt long after install
])
def test_window_is_classified_against_the_install_date(install_day, expected, post):
    cov = bp.window_coverage("2026-01-06", "2026-02-05", {"date": install_day, "source": "snapshot"})
    assert (cov["coverage"], cov["post_install_days"], cov["window_days"]) == (expected, post, 31)


def test_bad_windows_are_unknown_not_errors():
    install = {"date": "2026-01-01", "source": "snapshot"}
    for start, end in ((None, None), ("x", "y"), ("2026-02-05", "2026-01-06")):
        assert bp.window_coverage(start, end, install)["coverage"] == "unknown"


# ------------------------------------------------------- transformation copy

BASELINE = {"window": {"start": "2026-01-06", "end": "2026-02-05"},
            "opus_share": 0.8, "opus_share_source": "robust_earliest"}
PINNED = {"anchor_month": "your earliest tracked month (2026-01-06 to 2026-02-05)"}


def test_true_before_keeps_the_before_you_installed_claim(tmp_path):
    _snapshot(tmp_path, "2026-02-05")
    prov = bp.transformation(Facade(tmp_path), BASELINE, PINNED)
    assert prov["coverage"] == "pre_install" and prov["anchor"] == "pinned_window"
    assert "before you installed (2026-01-06 to 2026-02-05)" in prov["lead"]
    assert prov["note"] == ""


def test_no_older_transcripts_never_claims_a_before(tmp_path):
    _snapshot(tmp_path, "2026-01-05")
    prov = bp.transformation(Facade(tmp_path), BASELINE, PINNED)
    assert prov["coverage"] == "post_install"
    assert "before you installed" not in prov["lead"] + prov["note"]
    assert "earliest tracked month (2026-01-06 to 2026-02-05)" in prov["lead"]
    assert "already running" in prov["note"] and "understates" in prov["note"]
    assert "2026-01-05" in prov["note"]


def test_install_inside_the_window_is_disclosed_with_its_size(tmp_path):
    _snapshot(tmp_path, "2026-01-20")
    note = bp.transformation(Facade(tmp_path), BASELINE, PINNED)["note"]
    assert "17 of its 31 days" in note and "understates" in note


def test_unknown_install_date_is_said_plainly(tmp_path):
    prov = bp.transformation(Facade(tmp_path), BASELINE, PINNED)
    assert prov["coverage"] == "unknown" and "not on record" in prov["note"]


def test_calendar_month_anchor_is_dated_and_flags_a_baseline_still_building(tmp_path):
    _snapshot(tmp_path, "2026-01-05")
    (tmp_path / "workload_anchor_pretool.json").write_text(json.dumps({"status": "building"}))
    prov = bp.transformation(Facade(tmp_path), BASELINE, {"anchor_month": "2026-02"})
    assert (prov["anchor"], prov["start"], prov["end"]) == ("calendar_month", "2026-02-01", "2026-02-28")
    assert prov["pending_anchor"] == "building" and "still being built" in prov["note"]


def test_frozen_session_anchor_uses_the_baseline_window(tmp_path):
    _snapshot(tmp_path, "2026-02-05")
    prov = bp.transformation(Facade(tmp_path), BASELINE, None)
    assert (prov["anchor"], prov["coverage"]) == ("frozen_session", "pre_install")


@pytest.mark.parametrize("baseline, expected", [
    (dict(BASELINE, opus_share_source="pretool_baseline"), "stated"),
    (BASELINE, "measured"),
    (dict(BASELINE, opus_share=0.0), "current_mix"),
])
def test_mix_source_is_labelled(tmp_path, baseline, expected):
    prov = bp.transformation(Facade(tmp_path), baseline, PINNED)
    assert prov["mix_source"] == expected and prov["mix_note"]


def test_method_paragraph_carries_the_note_instead_of_a_fixed_claim(tmp_path):
    _snapshot(tmp_path, "2026-02-05")
    before = bp.weight_pool_caveat("label", bp.transformation(Facade(tmp_path), BASELINE, PINNED))
    assert "already had partial Token Optimizer coverage" not in before
    assert "Token Optimizer was" not in before
    _snapshot(tmp_path, "2026-01-05")
    after = bp.weight_pool_caveat("label", bp.transformation(Facade(tmp_path), BASELINE, PINNED))
    assert after.endswith("this figure understates it.")


# ---------------------------------------------------------- routing baseline

def test_routing_prefers_the_frozen_baseline_over_sliding_history(tmp_path):
    (tmp_path / "baseline_state.json").write_text(json.dumps({
        "activity_basis": 1, "model_shares": {"opus": 0.84, "sonnet": 0.16},
        "opus_share_source": "robust_earliest",
        "window": {"start": "2026-01-06", "end": "2026-02-05"}}))
    early = {"shares": {"opus": 0.6, "sonnet": 0.4}, "first_date": "2026-06-01"}
    shares, source, stated, early_mix = bp.routing_baseline(Facade(tmp_path, early))
    assert (shares["opus"], source, stated, early_mix) == (0.84, "frozen_baseline", False, None)


def test_routing_keeps_a_stated_share_that_only_survives_in_the_frozen_baseline(tmp_path):
    (tmp_path / "baseline_state.json").write_text(json.dumps({
        "activity_basis": 1, "model_shares": {"opus": 0.9, "sonnet": 0.1},
        "opus_share_source": "pretool_baseline"}))
    assert bp.routing_baseline(Facade(tmp_path))[1:3] == ("frozen_baseline", True)


def test_routing_snapshot_first_then_history_then_nothing(tmp_path):
    early = {"shares": {"opus": 0.6, "sonnet": 0.4}, "first_date": "2026-06-01"}
    assert bp.routing_baseline(Facade(tmp_path, early))[1] == "history"
    assert bp.routing_baseline(Facade(tmp_path)) == (None, None, False, None)
    (tmp_path / "baseline_state.json").write_text("{not json")
    assert bp.routing_baseline(Facade(tmp_path, early))[1] == "history"
    _snapshot(tmp_path, "2026-02-01", mix={"opus": 0.7, "sonnet": 0.3})
    assert bp.routing_baseline(Facade(tmp_path, early))[1] == "snapshot"


def test_routing_history_after_install_is_not_called_the_install_mix(tmp_path):
    _snapshot(tmp_path, "2026-01-05")
    early = {"shares": {"opus": 0.6}, "first_date": "2026-06-01"}
    prov = bp.routing(Facade(tmp_path, early), {"baseline_source": "history", "baseline_opus_share": 0.6})
    assert prov["coverage"] == "post_install" and prov["kind"] == "measured"
    assert "already running" in prov["lead"] and "2026-06-01 to 2026-06-15" in prov["lead"]
    assert "before install" not in prov["short"]


def test_routing_install_snapshot_mix_is_a_true_before(tmp_path):
    _snapshot(tmp_path, "2026-02-01", mix={"opus": 0.7})
    prov = bp.routing(Facade(tmp_path), {"baseline_source": "snapshot", "baseline_opus_share": 0.7})
    assert prov["coverage"] == "pre_install" and "70% Opus mix before install" in prov["short"]


def test_routing_stated_and_absent(tmp_path):
    stated = bp.routing(Facade(tmp_path), {"baseline_source": "pretool_baseline", "baseline_opus_share": 0.95})
    assert stated["kind"] == "stated" and stated["short"] == "assuming 95% Opus before Token Optimizer"
    assert bp.routing(Facade(tmp_path), {"baseline_source": None}) is None
    assert bp.routing(Facade(tmp_path), None) is None


# ------------------------------------------------------------- through measure

@pytest.fixture
def measure(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-provenance-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    monkeypatch.setattr(mod, "_subagent_pool_savings", lambda **kw: {
        "actual_usd": 0.0, "counterfactual_usd": 0.0, "transformation_usd": 0.0,
        "premium_delegation_usd": 0.0, "sessions": 0, "by_model": {}})
    yield mod, Path(tmp)
    sys.modules.pop("measure", None)


def test_transformation_payload_discloses_a_post_install_baseline(measure):
    import test_baseline_savings as fixtures
    mod, tmp = measure
    fixtures._build_env(str(tmp), n_sessions=40, per_session_input=4_000_000, opus_share=0.56)
    _snapshot(tmp, "2026-03-03")  # installed two weeks before the frozen window opens
    result = mod._estimate_before_after_savings(days=30)
    prov = result["baseline_provenance"]
    assert prov["coverage"] == "post_install" and prov["install_date"] == "2026-03-03"
    assert prov["mix_source"] == "stated" and "understates" in prov["note"]
    assert mod._install_date() == "2026-03-03"


def test_routing_payload_uses_and_labels_the_frozen_baseline(measure):
    import test_baseline_savings as fixtures
    mod, tmp = measure
    fixtures._build_env(str(tmp), n_sessions=40, per_session_input=4_000_000, opus_share=0.56)
    routing = mod._compute_model_routing_savings(days=30)
    assert routing["baseline_source"] == "frozen_baseline" and routing["baseline_stated"] is True
    assert routing["baseline_opus_share"] == 0.95
    from _measure import savings_story
    assert savings_story.routing_baseline_kind(routing, "claude") == "stated"


# ------------------------------------------------------------------ user copy

def test_no_refactor_artifact_in_any_string_shown_to_users():
    """`_compat.facade()._headline` reached card and CLI copy once."""
    pattern = re.compile(r"""["'][^"'\n]*_compat\.facade\(\)[^"'\n]*["']""")
    hits = []
    for path in sorted((SCRIPTS / "_measure").glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and pattern.search(line):
                hits.append("%s:%d" % (path.name, number))
    assert hits == []


def _render(expr, data):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute the dashboard renderer")
    esc = ("const esc = x => String(x == null ? '' : x).replace(/[&<>\"']/g, "
           "c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n"
           "const fn = n => String(Math.round(Number(n) || 0));\n")
    script = esc + STORY.read_text() + "\nconst data = JSON.parse(process.argv[2]);\nprocess.stdout.write(" + expr + ");"
    return subprocess.run([node, "-", json.dumps(data)], input=script,
                          capture_output=True, text=True, check=True).stdout


def _card(provenance=None):
    ba = {"transformation_state": "ok", "monthly_savings_usd": 120.0, "counterfactual_monthly_usd": 600.0,
          "actual_monthly_usd": 480.0, "transformation_pct": 0.2, "before_mix_label": "80% Opus",
          "after_mix_label": "60% Opus"}
    if provenance:
        ba["baseline_provenance"] = provenance
    return {"savings": {"before_after": ba}}


def test_card_does_not_say_before_you_installed_for_a_post_install_baseline(tmp_path):
    _snapshot(tmp_path, "2026-01-05")
    html = _render("savingsTransformationHtml(data)",
                   _card(bp.transformation(Facade(tmp_path), BASELINE, PINNED)))
    assert "before you installed" not in html
    assert "your earliest tracked month (2026-01-06 to 2026-02-05)" in html
    above, fold = html.split("How this is calculated")
    assert "already running" in fold and "understates" in fold
    assert "already running" not in above  # the lead is true; the caveat lives in the fold
    assert "measured in your earliest tracked month" in html


def test_card_keeps_the_claim_for_a_true_before_and_for_old_payloads(tmp_path):
    _snapshot(tmp_path, "2026-02-05")
    html = _render("savingsTransformationHtml(data)",
                   _card(bp.transformation(Facade(tmp_path), BASELINE, PINNED)))
    assert "from before you installed (2026-01-06 to 2026-02-05)" in html
    old = _render("savingsTransformationHtml(data)", _card())
    assert "compared with your own baseline from before you installed." in old


def test_routing_basis_uses_the_payload_sentence(tmp_path):
    _snapshot(tmp_path, "2026-01-05")
    early = {"shares": {"opus": 0.6}, "first_date": "2026-06-01"}
    prov = bp.routing(Facade(tmp_path, early), {"baseline_source": "history", "baseline_opus_share": 0.6})
    data = {"savings": {"model_routing": {"baseline_source": "history", "baseline_opus_share": 0.6,
                                          "baseline_provenance": prov}}}
    lead = _render("savingsRoutingBasis(data).lead", data)
    assert "already running" in lead and "recorded when you installed" not in lead
    legacy = _render("savingsRoutingBasis(data).lead",
                     {"savings": {"model_routing": {"baseline_source": "history", "baseline_opus_share": 0.6}}})
    assert "recorded when you installed" in legacy


# ------------------------------------------------------- upgrade in progress

def _hero(savings):
    import test_savings_story_render as render
    out = render.story({"savings": savings})
    return out.split('class="card st-hero"')[1].split("<details")[0]


def test_hero_numbers_say_so_far_while_an_upgrade_rereads_history():
    import test_savings_story_render as render
    savings = render.full()
    savings["counted_period"] = dict(savings.get("counted_period") or {}, remeasuring=True, remeasuring_sessions=366)
    hero = _hero(savings)
    assert "days, so far</span>" in hero
    assert "Updating your history after an upgrade (366 sessions left)" in hero


def test_hero_numbers_carry_no_progress_label_when_history_is_complete():
    import test_savings_story_render as render
    hero = _hero(render.full())
    assert "so far" not in hero and "Updating your history" not in hero


def test_since_install_number_says_so_far_while_routing_is_pending():
    import test_savings_story_render as render
    savings = render.full()
    since = dict(savings.get("since_install") or {"days": 40, "install_date": "2026-01-05"})
    since["all_in"] = dict(since.get("all_in") or {"total_usd": 50.0, "components_usd": {"logged": 50.0}},
                           routing_pending=True, routing_pending_sessions=12)
    savings["since_install"] = since
    hero = _hero(savings)
    assert "saved since you installed, so far</span>" in hero and "(12 sessions left)" in hero
    assert "days, so far" not in hero


# ------------------------------------------------------------ fixed price card

def test_fixed_card_figure_is_never_called_what_you_actually_spent():
    data = _card()
    data["savings"]["before_after"]["session_weight_pool"] = {
        "flat_rates": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
        "engine_priced_actual_usd": 210.0}
    fold = _render("savingsTransformationHtml(data)", data).split("How this is calculated")[1]
    assert "What you actually spent" not in fold
    assert "This period, at the same fixed price card: $480.00 a month" in fold
    assert "$5 input, $25 output, $0.5 cache read, $6.25 cache write" in fold
    assert "not your bill" in fold and "this period comes to $210.00 at API rates" in fold


def test_exact_priced_card_keeps_its_wording():
    fold = _render("savingsTransformationHtml(data)", _card()).split("How this is calculated")[1]
    assert "What you actually spent: $480.00 a month" in fold and "fixed price card" not in fold


def test_weight_pool_payload_carries_the_card_it_was_priced_at(measure):
    mod, _tmp = measure
    pool = {"anchor_month": "2026-02", "before_cost_per_session": 1.0, "after_cost_per_session": 1.0,
            "before_tokens_per_session": 1, "after_tokens_per_session": 1, "before_sessions": 1,
            "sessions": 1, "actual_usd": 1.0, "counterfactual_usd": 1.0, "transformation_usd": 0.0,
            "capacity_assumption": ""}
    assert mod._weight_pool_payload(pool)["flat_rates"] == mod._WEIGHT_POOL_FLAT_RATES


# ------------------------------------------- user types, through the real code

def _history(tmp, first_days_ago, per_day=2):
    """A ledger with `per_day` human sessions every day since `first_days_ago`."""
    import test_baseline_savings as fixtures
    from _activity_fixtures import stamp_activity
    from datetime import datetime, timedelta
    conn = sqlite3.connect(str(Path(tmp) / "trends.db"))
    conn.executescript(fixtures._SCHEMA)
    today = datetime.now()
    for back in range(first_days_ago, -1, -1):
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        for n in range(per_day):
            conn.execute(
                "INSERT INTO session_log (jsonl_path, date, input_tokens, output_tokens, cache_hit_rate, "
                "cache_create_5m_tokens, cache_create_1h_tokens, all_model_usage_json, is_sidechain, "
                "duration_minutes, message_count, api_calls) VALUES (?,?,?,?,?,?,?,?,0,5.0,20,30)",
                ("/s/%s-%d.jsonl" % (day, n), day, 4_000_000, 40_000, 0.97, 120_000, 0,
                 json.dumps({fixtures.OPUS: 3_000_000, fixtures.SONNET: 1_000_000})))
    stamp_activity(conn)
    conn.commit()
    conn.close()
    return today


def _days_ago(today, n):
    from datetime import timedelta
    return (today - timedelta(days=n)).strftime("%Y-%m-%d")


def test_fresh_install_with_older_transcripts_gets_a_true_before(measure):
    mod, tmp = measure
    today = _history(tmp, first_days_ago=60)
    _snapshot(tmp, _days_ago(today, 28))  # installed 28 days ago, 32 days of history before it
    prov = mod._estimate_before_after_savings(days=30)["baseline_provenance"]
    assert prov["coverage"] == "pre_install" and prov["note"] == ""
    assert (prov["start"], prov["end"]) == (_days_ago(today, 59), _days_ago(today, 29))
    assert prov["mix_source"] == "measured"


def test_fresh_install_without_older_transcripts_is_told_the_tool_was_already_running(measure):
    mod, tmp = measure
    today = _history(tmp, first_days_ago=45)
    _snapshot(tmp, _days_ago(today, 45))  # history starts on install day
    result = mod._estimate_before_after_savings(days=30)
    prov = result["baseline_provenance"]
    assert prov["coverage"] == "post_install" and "already running" in prov["note"]
    assert "before you installed" not in prov["lead"]
    routing = mod._compute_model_routing_savings(days=30)
    label = bp.routing(mod, routing)
    assert routing["baseline_source"] == "frozen_baseline" and label["coverage"] == "post_install"
    assert "before install" not in label["short"]


def test_no_baseline_yet_reports_progress_instead_of_a_number(measure):
    mod, tmp = measure
    _history(tmp, first_days_ago=5)
    result = mod._estimate_before_after_savings(days=30)
    assert result["reason"] == "insufficient_history" and result["monthly_savings_usd"] == 0.0
    assert result["baseline_building"]["sessions_needed"] == 30
