"""Observed listing reductions enter setup totals only once."""
import json
from datetime import datetime

from test_structural_ledger import m, _mods, _db, _trim
from test_setup_attachment_counter import attachment, call, card
from _measure.setup_budget_monitor import record


def test_observed_budget_drop_books_real_calls_and_is_idempotent(m, tmp_path, monkeypatch):
    _, _, report, _, _ = _mods()
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)))
    after.write_text("\n".join(json.dumps(row) for row in [
        attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
        call("2026-01-02T00:00:01Z", "r1", 100)]))
    conn = _db(m)
    record(conn, "old", "project", str(before), 10000, "2026-01-01T00:00:00Z")
    record(conn, "new", "project", str(after), 5000, "2026-01-02T00:00:00Z")
    conn.close()
    import token_estimate
    monkeypatch.setattr(token_estimate, "estimate_tokens", len)
    monkeypatch.setattr(report.value, "card_resolver", lambda f: card)
    first = report.compute(days=5, now=datetime(2026, 1, 4), f=m, home="/home/tester", trims=[])
    second = report.compute(days=5, now=datetime(2026, 1, 4), f=m, home="/home/tester", trims=[])
    assert first["tokens_saved"] == 20 and first["read_savings_usd"] == 0.00004
    assert second["tokens_saved"] == first["tokens_saved"]
    assert first["budget_observed"]["calls"] == 1


def test_budget_credit_is_suppressed_when_existing_listing_trims_may_overlap(m, tmp_path, monkeypatch):
    ledger, _, report, _, _ = _mods()
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)))
    after.write_text("\n".join(json.dumps(row) for row in [
        attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
        call("2026-01-02T00:00:01Z", "r1", 100)]))
    conn = _db(m)
    record(conn, "old", "project", str(before), 10000, "2026-01-01T00:00:00Z")
    record(conn, "new", "project", str(after), 5000, "2026-01-02T00:00:00Z")
    conn.close()
    import token_estimate
    monkeypatch.setattr(token_estimate, "estimate_tokens", len)
    monkeypatch.setattr(report.value, "card_resolver", lambda f: card)
    trim = ledger.load_trims_from([_trim(ledger, "2026-01-01T00:00:00+00:00", tokens=100)])
    monkeypatch.setattr(report.listing, "apply_listing_check", lambda trims, *a: trims)
    output = report.compute(days=5, now=datetime(2026, 1, 4), f=m, home="/home/tester", trims=trim)
    assert output["budget_observed"]["calls"] == 0
    assert output["budget_observed"]["overlap_suppressed"] is True
