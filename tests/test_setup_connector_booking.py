"""Explicitly disconnected connector text is counted once per observed call."""
import json
from datetime import datetime

from test_structural_ledger import m, _mods, _db, _session, _trim
from test_setup_attachment_counter import attachment, call, card
from _measure.setup_connector_ledger import register


def test_confirmed_disconnect_books_observed_text_and_deduplicates(m, tmp_path, monkeypatch):
    _, _, report, _, _ = _mods()
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "deferred_tools_delta",
                      addedNames=["mcp__a", "mcp__b"], addedLines=["aaaa", "bbbb"], removedNames=[])))
    after.write_text("\n".join(json.dumps(row) for row in [
        attachment("2026-01-02T00:00:00Z", "deferred_tools_delta", addedNames=["mcp__b"], addedLines=["bbbb"], removedNames=[]),
        call("2026-01-02T00:00:01Z", "r1", 100), call("2026-01-02T00:00:01Z", "r1", 100)]))
    conn = _db(m)
    _session(conn, m, "new", "project", datetime(2026, 1, 2, 1), path=str(after))
    conn.commit()
    assert not register(conn, "project", "deferred_tools_delta", "2026-01-02T00:00:00Z", str(before), ["mcp__a"], False)
    assert register(conn, "project", "deferred_tools_delta", "2026-01-02T00:00:00Z", str(before), ["mcp__a"], True)
    assert not register(conn, "project", "deferred_tools_delta", "2026-01-02T00:00:00Z", str(before), ["mcp__a"], True)
    conn.close()
    import token_estimate
    monkeypatch.setattr(token_estimate, "estimate_tokens", len)
    monkeypatch.setattr(report.value, "card_resolver", lambda f: card)
    output = report.compute(days=5, now=datetime(2026, 1, 4), f=m, home="/home/tester", trims=[])
    assert output["tokens_saved"] == 4 and output["connector_observed"]["calls"] == 1
    assert report.compute(days=5, now=datetime(2026, 1, 4), f=m, home="/home/tester", trims=[])["tokens_saved"] == 4


def test_mcp_trim_overlap_prevents_double_credit(m, tmp_path, monkeypatch):
    ledger, _, report, _, _ = _mods()
    before = tmp_path / "before.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "mcp_instructions_delta",
                      addedNames=["service"], addedBlocks=["instructions"], removedNames=[])))
    conn = _db(m)
    assert register(conn, "project", "mcp_instructions_delta", "2026-01-02T00:00:00Z", str(before), ["service"], True)
    conn.close()
    trim = ledger.load_trims_from([_trim(ledger, "2026-01-01T00:00:00+00:00", tokens=100, comp="mcp_tools")])
    output = report.compute(days=5, now=datetime(2026, 1, 4), f=m, home="/home/tester", trims=trim)
    assert output["connector_observed"]["calls"] == 0 and output["connector_observed"]["overlap_suppressed"] is True


def test_disjoint_connectors_can_be_recorded_without_reusing_a_target(m, tmp_path):
    before = tmp_path / "before.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "deferred_tools_delta",
                      addedNames=["a", "b"], addedLines=["aaaa", "bbbb"], removedNames=[])))
    conn = _db(m)
    assert register(conn, "project", "deferred_tools_delta", "2026-01-02T00:00:00Z", str(before), ["a"], True)
    assert register(conn, "project", "deferred_tools_delta", "2026-01-03T00:00:00Z", str(before), ["b"], True)
    assert not register(conn, "project", "deferred_tools_delta", "2026-01-04T00:00:00Z", str(before), ["a"], True)
    conn.close()


def test_connector_registration_cli_is_dry_run_unless_applied(m, tmp_path, monkeypatch, capsys):
    import sqlite3
    _, _, report, _, _ = _mods()
    before = tmp_path / "before.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "mcp_instructions_delta",
                      addedNames=["service"], addedBlocks=["instructions"], removedNames=[])))
    monkeypatch.setattr(report._compat, "facade", lambda: m)
    args = ["connector-disconnected", "--project", "project", "--before", str(before),
            "--changed-at", "2026-01-02T00:00:00Z", "--component", "mcp_instructions_delta", "--target", "service"]
    assert report.cli(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] and preview["eligible"]
    assert report.cli(args + ["--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["registered"] is True
    with sqlite3.connect(str(m.TRENDS_DB)) as conn:
        assert conn.execute("SELECT count(*) FROM setup_connector_actions").fetchone()[0] == 1
