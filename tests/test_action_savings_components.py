"""CLI, JSON and dashboard share the full action-savings breakdown."""
import copy
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from test_savings_card_young_install import measure
from test_savings_render import render
from test_counted_cumulative import _verified_pricing


def snapshot(hint_events=2, hint_cost=32):
    return {
        "period_days": 30,
        "total_cost_usd": 100,
        "total_tokens": 1000,
        "total_events": 5,
        "by_category": {},
        "daily_avg_usd": 100 / 30,
        "model_routing": {"realized_cost_usd": 7, "potential_cost_usd": 10000},
        "behavioral_estimate": {"cost_saved_usd": 1},
        "uncaptured_runtime": {"cost_saved_usd": 2},
        "mcp_cap_estimated": {"cost_saved_usd": 4},
        "contamination_exit": {"cost_saved_usd": 8},
        "handover_rerun": {"cost_saved_usd": 16},
        "hint_followed": {"cost_saved_usd": hint_cost, "events": hint_events},
        "retrieval_serve": {"cost_saved_usd": 512, "served_sessions": 3},
        "resume_lean_estimated": {"cost_saved_usd": 64},
        "verbosity_steer": {"cost_saved_usd": 128, "events": 4},
        "structural_potential": {"cost_saved_usd": 20000},
        "output_waste": {"cost_saved_usd": 30000},
        "cache_drop": {"cost_saved_usd": 40000},
        "stale_reads_reclaimable": {"cost_usd": 50000},
        "one_time_setup": {"cost_saved_usd": 60000},
        "before_after": {"monthly_savings_usd": 70000},
    }


def test_attested_realized_setup_is_included_once_in_action_total(measure):
    source = snapshot()
    source['structural_detail'] = {'estimated': {'cost_saved_usd': 12.5}}
    result = measure._action_savings_components(source, {'available': True, 'reread_usd': 0})
    assert result['estimates_usd']['structural_estimated'] == 12.5
    assert result['net_total_usd'] == 100 + 1 + 2 + 4 + 8 + 16 + 32 + 12.5


@pytest.mark.parametrize("hint_events,hint_cost,reread,expected", [
    (2, 32, 23, 186),
    (0, 32, 23, 666),
    (2, 0, 23, 154),
    (2, 32, -23, 140),
    (2, 32, -500, 0),
])
def test_same_snapshot_cli_json_dashboard(
    measure, monkeypatch, capsys, hint_events, hint_cost, reread, expected,
):
    source = snapshot(hint_events, hint_cost)
    counted = {"available": True, "reread_usd": reread,
               "reread_tokens": 200, "oneshot_usd": 99, "total_usd": 99 + reread}
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: copy.deepcopy(source))
    monkeypatch.setattr(measure, "_reread_savings_for_window", lambda days: {
        "available": True, "reread_usd": 999,
    })
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: counted.copy())
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)

    measure.savings_report()
    cli = capsys.readouterr().out
    assert f"= ALL IN: ~${expected:.2f}/mo" in cli
    assert "+ est. concise output nudges:" not in cli
    if hint_cost or not hint_events:
        label = "hints" if hint_events else "retrieval"
        assert f"+ est. avoided search ({label})" in cli
    if reread < 0:
        assert f"+ repeat reads avoided: ~${reread:.2f}/mo" in cli
    if expected == 0:
        assert "net total of ~$-337.00/mo; the headline has a $0 floor" in cli

    measure.savings_report(as_json=True)
    payload = json.loads(capsys.readouterr().out)
    dashboard = measure._dashboard_savings_data()
    assert payload["action_savings"] == dashboard["action_savings"]
    components = payload["action_savings"]
    assert components["total_usd"] == expected
    assert components["logged_usd"] == 100
    assert components["routing_usd"] == 7
    assert components["reread_usd"] == reread
    estimated = 31 + (hint_cost if hint_events else 512)
    assert components["estimated_usd"] == estimated
    assert components["net_total_usd"] == 100 + reread + estimated
    assert components["total_clamped"] is (expected == 0)
    assert payload["total_cost_usd"] == dashboard["total_cost_usd"] == 100
    assert dashboard["counted_cumulative"] == counted
    actions = render(dashboard).split('savings-actions')[1].split('savings-history')[0]
    amount = f"{expected:.2f}"
    assert f'"savings-amount">${amount}<' in actions
    assert f'Other estimates <strong>~${estimated}' in actions
    if expected == 0:
        assert 'net total of $-337.00; the headline has a $0 floor' in actions


def test_component_helper_is_pure_and_counts_only_rereads(measure):
    source = snapshot()
    before = copy.deepcopy(source)
    components = measure._action_savings_components(source, {"available": True, "reread_usd": 23})
    assert source == before
    assert components["total_usd"] == 186
    assert set(components["estimates_usd"]) == {
        "behavioral_estimate", "uncaptured_runtime", "mcp_cap_estimated",
        "contamination_exit", "handover_rerun", "resume_lean_estimated", "resume_lean_verified",
        "checkpoint_restore_estimated", "verbosity_steer", "verbosity_output_estimated",
        "hint_followed", "expand_overhead", "structural_estimated",
    }
    assert sum(components["estimates_usd"].values()) == 63
    assert measure._action_savings_components({})["total_usd"] == 0


def test_lean_resume_is_diagnostic_not_savings(measure):
    source = {"total_cost_usd": 7, "resume_lean_estimated": {"cost_saved_usd": 900, "tokens_saved": 10000}}
    components = measure._action_savings_components(source, {"available": True, "reread_usd": 3})
    assert components["total_usd"] == 10
    assert components["estimates_usd"].get("resume_lean_estimated", 0) == 0


def test_verbosity_is_excluded_from_logged_and_estimated(measure):
    source = {"total_cost_usd": 17, "by_category": {"verbosity_steer_measured": {"cost_saved_usd": 7}},
              "verbosity_steer": {"cost_saved_usd": 4}}
    components = measure._action_savings_components(source, {"available": True, "reread_usd": 3})
    assert components["total_usd"] == 13
    assert components["estimates_usd"].get("verbosity_steer", 0) == 0


def test_rotated_transcript_preserves_stored_action_rereads(measure, monkeypatch, tmp_path, capsys):
    db = tmp_path / "trends.db"
    monkeypatch.setattr(measure, "TRENDS_DB", db)
    monkeypatch.setattr(measure, "CLAUDE_DIR", tmp_path / "claude")
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_init_trends_db", lambda: sqlite3.connect(str(db)))
    sid = "cafe0000-1111-2222-3333-444455556666"
    stamp = (datetime.now() - timedelta(hours=1)).isoformat()
    missing = tmp_path / "rotated-session.jsonl"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(measure._SCHEMA)
        conn.execute(
            "INSERT INTO savings_events(id,timestamp,event_type,tokens_saved,cost_saved_usd,"
            "session_id,session_uuid) VALUES(1,?,'tool_archive',1000,2,?,?)", (stamp, sid, sid))
        conn.execute(
            "INSERT INTO session_log(jsonl_path,date,session_uuid) VALUES(?,?,?)",
            (str(missing), stamp[:10], sid))
        conn.execute(
            "INSERT INTO counted_reread(event_key,source,session_uuid,event_ts,event_month,"
            "tokens,oneshot_usd,reread_tokens,reread_usd,computed_at,pricing_detail) "
            "VALUES('se:1','se',?,?,?,1000,99,2000,23,?,?)", (sid, stamp, stamp[:7], stamp,
                _verified_pricing(measure)))
        assert conn.execute("SELECT reread_usd FROM counted_reread").fetchone() == (23,)
    assert not missing.exists()
    assert measure._reread_savings_for_window(30)["reread_usd"] == 0
    assert measure._counted_cumulative_summary(days=30)["reread_usd"] == 23
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: snapshot())
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report(as_json=True)
    cli = json.loads(capsys.readouterr().out)
    dashboard = measure._dashboard_savings_data()
    assert cli["action_savings"] == dashboard["action_savings"]
    assert cli["action_savings"]["reread_usd"] == 23
    assert cli["action_savings"]["total_usd"] == 186


def test_unavailable_rereads_are_explicit_on_both_surfaces(measure, monkeypatch, capsys):
    monkeypatch.setattr(measure, "_get_merged_savings", lambda **kw: snapshot())
    monkeypatch.setattr(measure, "_counted_cumulative_summary", lambda **kw: {"available": False})
    monkeypatch.setattr(measure, "_savings_since_install", lambda: {"days": 90})
    monkeypatch.setattr(measure, "_install_date", lambda: None)
    measure.savings_report()
    cli = capsys.readouterr().out
    assert "repeat reads avoided: unavailable" in cli
    assert "ALL IN: ~$163.00/mo" in cli
    dashboard = measure._dashboard_savings_data()
    assert dashboard["action_savings"]["reread_available"] is False
    actions = render(dashboard).split('savings-actions')[1].split('savings-history')[0]
    assert 'Repeat reads avoided <strong>Unavailable' in actions
    assert '"savings-amount">$163.00<' in actions


def test_dashboard_prefers_server_computed_components():
    source = snapshot()
    source["action_savings"] = {
        "logged_usd": 200, "routing_usd": 11, "reread_usd": 31,
        "routing_measured_usd": 11, "routing_estimated_usd": 0,
        "estimated_usd": 131, "total_usd": 362,
    }
    actions = render(source).split('savings-actions')[1].split('savings-history')[0]
    assert '"savings-amount">$362<' in actions
    assert 'Logged actions <strong>$200' in actions
    assert 'Repeat reads avoided <strong>~$31.0' in actions
    assert 'Other estimates <strong>~$131' in actions


@pytest.mark.parametrize("evidence,measured,estimated", [
    ("measured", 7, 0), ("estimated", 0, 7),
])
def test_routing_evidence_changes_tier_without_changing_total(measure, evidence, measured, estimated):
    source = snapshot()
    source["model_routing"]["evidence"] = evidence
    components = measure._action_savings_components(source, {"available": True, "reread_usd": 23})
    assert components["routing_usd"] == 7
    assert components["routing_measured_usd"] == measured
    assert components["routing_estimated_usd"] == estimated
    assert components["estimated_usd"] == 63
    assert components["total_usd"] == 186
    source["action_savings"] = components
    actions = render(source).split('savings-actions')[1].split('savings-history')[0]
    assert '"savings-amount">$186.00<' in actions
    assert 'Logged actions <strong>$100' in actions
    assert 'Other estimates <strong>~$63' in actions


def test_verbosity_stays_out_of_savings_totals(measure):
    from _measure import headline
    data = snapshot()
    data['verbosity_steer']['tokens_saved'] = 400
    data['verbosity_output_estimated'] = {'cost_saved_usd': 30, 'tokens_saved': 600}
    result = measure._action_savings_components(data, {'available': True, 'reread_usd': 23})
    assert result['total_usd'] == 186
    assert result['estimates_usd']['verbosity_steer'] == 0
    assert result['estimates_usd']['verbosity_output_estimated'] == 0
    assert headline.token_headline(data)['removed_tokens'] == 1000
    assert measure._dashboard_saved_tokens(data)[0] == 1000


def test_unverified_checkpoint_restore_does_not_inflate_totals(measure):
    source = {'total_cost_usd': 7, 'checkpoint_restore_estimated': {'cost_saved_usd': 900, 'tokens_saved': 10000}}
    result = measure._action_savings_components(source, {'available': True, 'reread_usd': 3})
    assert result['total_usd'] == 10
    assert not result['estimates_usd'].get('checkpoint_restore_estimated')


def test_verified_lean_resume_enters_estimated_total(measure):
    source = {'total_cost_usd': 7, 'resume_lean_verified': {'cost_saved_usd': 4, 'tokens_saved': 2000, 'events': 1}}
    parts = measure._action_savings_components(source, {'available': True, 'reread_usd': 3})
    assert parts['total_usd'] == 14
    assert parts['estimates_usd']['resume_lean_verified'] == 4


def test_expand_call_overhead_reduces_action_total(measure):
    source = {'total_cost_usd': 7, 'session_effect': {'overhead': {'expand_usd': 2}}}
    result = measure._action_savings_components(source, {'available': True, 'reread_usd': 3})
    assert result['total_usd'] == 8
    assert result['estimates_usd']['expand_overhead'] == -2
