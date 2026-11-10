"""Count observed setup changes on actual priced calls, never a projection."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure.setup_attachment_counter import measure, measure_sessions


def attachment(ts, kind, **fields):
    return {"type": "attachment", "timestamp": ts, "attachment": {"type": kind, **fields}}


def call(ts, key, read, write=0):
    return {"type": "assistant", "timestamp": ts, "requestId": key, "message": {
        "model": "test-model", "usage": {"cache_read_input_tokens": read,
        "cache_creation_input_tokens": write, "input_tokens": 10}}}


def card(model, prompt):
    return {"cache_read": 2.0, "cache_write": 10.0, "cache_write_1h": 20.0}


def test_listing_prices_each_deduplicated_post_change_call():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", isInitial=True, content="x" * 40)]
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", isInitial=True, content="x" * 20),
             call("2026-01-02T00:00:01Z", "r1", 100),
             call("2026-01-02T00:00:01Z", "r1", 100),
             call("2026-01-02T00:00:02Z", "r2", 0, 100)]
    result = measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card)
    assert result["calls"] == 2 and result["read_tokens"] == 20 and result["write_tokens"] == 20
    assert result["usd"] == (20 * 2 + 20 * 10) / 1_000_000


def test_one_hour_write_uses_the_observed_write_class():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)]
    billed = call("2026-01-02T00:00:01Z", "r1", 0, 100)
    billed["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 0}
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20), billed]
    result = measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card)
    assert result["write_tokens"] == 20 and result["write_1h_usd"] == 20 * 20 / 1_000_000


def test_connector_only_credits_named_observed_removal():
    before = [attachment("2026-01-01T00:00:00Z", "deferred_tools_delta",
                         addedNames=["a", "b"], addedLines=["aaaa", "bbbb"], removedNames=[])]
    after = [attachment("2026-01-02T00:00:00Z", "deferred_tools_delta",
                        addedNames=["b"], addedLines=["bbbb"], removedNames=["a"]),
             call("2026-01-02T00:00:01Z", "r1", 100)]
    result = measure(before, after, "2026-01-02T00:00:00Z", "deferred_tools_delta", len, card, targets=["a"])
    assert result["calls"] == 1 and result["read_tokens"] == 4
    assert measure(before, after, "2026-01-02T00:00:00Z", "deferred_tools_delta", len, card, targets=["b"])["read_tokens"] == 0


def test_confirmed_disconnect_can_use_a_new_session_initial_listing():
    before = [attachment("2026-01-01T00:00:00Z", "deferred_tools_delta",
                         addedNames=["a", "b"], addedLines=["aaaa", "bbbb"], removedNames=[])]
    initial = attachment("2026-01-02T00:00:00Z", "deferred_tools_delta",
                         addedNames=["b"], addedLines=["bbbb"], removedNames=[])
    after = [initial, call("2026-01-02T00:00:01Z", "r1", 100)]
    assert measure(before, after, "2026-01-02T00:00:00Z", "deferred_tools_delta", len, card, targets=["a"])["calls"] == 0
    measured = measure(before, after, "2026-01-02T00:00:00Z", "deferred_tools_delta", len, card,
                       targets=["a"], confirmed_disconnect=True)
    assert measured["read_tokens"] == 4
    late = [call("2026-01-02T00:00:00Z", "r0", 100), initial, call("2026-01-02T00:00:01Z", "r1", 100)]
    assert measure(before, late, "2026-01-02T00:00:00Z", "deferred_tools_delta", len, card,
                   targets=["a"], confirmed_disconnect=True)["calls"] == 0


def test_no_baseline_no_observation_and_compaction_credit_nothing():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", isInitial=True, content="x" * 40)]
    after = [call("2026-01-02T00:00:01Z", "r1", 100)]
    assert measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0
    observed = [attachment("2026-01-02T00:00:00Z", "skill_listing", isInitial=True, content="x" * 20),
                {"type": "system", "subtype": "compact_boundary"}, call("2026-01-02T00:00:01Z", "r1", 100)]
    assert measure(before, observed, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0
    assert measure([], observed, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0
    compacted_before = before + [{"type": "system", "subtype": "compact_boundary",
                                  "timestamp": "2026-01-01T01:00:00Z"}]
    valid_after = [observed[0], observed[-1]]
    assert measure(compacted_before, valid_after, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0


def test_mcp_reconnection_ends_credit_and_unknown_model_is_unpriced():
    before = [attachment("2026-01-01T00:00:00Z", "mcp_instructions_delta",
                         addedNames=["service"], addedBlocks=["12345"], removedNames=[])]
    after = [attachment("2026-01-02T00:00:00Z", "mcp_instructions_delta",
                        addedNames=[], addedBlocks=[], removedNames=["service"]),
             call("2026-01-02T00:00:01Z", "r1", 100),
             attachment("2026-01-02T00:00:02Z", "mcp_instructions_delta",
                        addedNames=["service"], addedBlocks=["12345"], removedNames=[]),
             call("2026-01-02T00:00:03Z", "r2", 100)]
    result = measure(before, after, "2026-01-02T00:00:00Z", "mcp_instructions_delta", len, card, targets=["service"])
    assert result["calls"] == 1 and result["read_tokens"] == 5
    unknown = measure(before, after, "2026-01-02T00:00:00Z", "mcp_instructions_delta", len, lambda *a: None, targets=["service"])
    assert unknown["calls"] == 0 and unknown["unpriced_calls"] == 1


def test_listing_increase_is_never_credited():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", isInitial=True, content="x" * 20)]
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", isInitial=True, content="x" * 40),
             call("2026-01-02T00:00:01Z", "r1", 100)]
    assert measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0


def test_malformed_replacement_invalidates_prior_observation():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)]
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
             attachment("2026-01-02T00:00:01Z", "skill_listing", content=None),
             call("2026-01-02T00:00:02Z", "r1", 100)]
    assert measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0
    broken_before = before + [attachment("2026-01-01T01:00:00Z", "skill_listing", content=None)]
    valid_after = [after[0], after[-1]]
    assert measure(broken_before, valid_after, "2026-01-02T00:00:00Z", "skill_listing", len, card)["calls"] == 0


def test_malformed_usage_never_creates_a_credit_or_breaks_later_calls():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)]
    bad = call("2026-01-02T00:00:01Z", "bad", 100)
    bad["message"]["usage"]["cache_read_input_tokens"] = "not-a-number"
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20), bad,
             call("2026-01-02T00:00:02Z", "good", 100)]
    result = measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card)
    assert result["calls"] == 1 and result["read_tokens"] == 20 and result["unpriced_calls"] == 1


def test_diagnostic_cli_reads_transcripts_without_booking(tmp_path, monkeypatch, capsys):
    from _measure import structural_report
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)) + "\n")
    after.write_text("\n".join(json.dumps(r) for r in [
        attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
        call("2026-01-02T00:00:01Z", "r1", 100)]))
    monkeypatch.setattr(structural_report._compat, "facade", lambda: object())
    monkeypatch.setattr(structural_report.value, "card_resolver", lambda facade: card)
    assert structural_report.cli(["observed-change", "--before", str(before), "--after", str(after),
        "--changed-at", "2026-01-02T00:00:00Z", "--component", "skill_listing"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["booked"] is False and output["calls"] == 1 and output["read_tokens"] > 0
    assert "xxxxx" not in json.dumps(output)


def test_multiple_sessions_count_once_per_session_and_request():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)]
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
             call("2026-01-02T00:00:01Z", "request", 100)]
    other = [after[0], call("2026-01-02T00:00:01Z", "other-request", 100)]
    sessions = [{"session_id": "one", "rows": after}, {"session_id": "one", "rows": after},
                {"session_id": "two", "rows": other}, {"session_id": "wrong", "rows": after}]
    result = measure_sessions(before, sessions, "2026-01-02T00:00:00Z", "skill_listing", len, card)
    assert result["calls"] == 2 and result["read_tokens"] == 40


def test_period_filter_prices_only_calls_in_window():
    before = [attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)]
    after = [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
             call("2026-01-02T00:00:01Z", "r1", 100), call("2026-01-03T00:00:01Z", "r2", 100)]
    result = measure(before, after, "2026-01-02T00:00:00Z", "skill_listing", len, card,
                     window_start="2026-01-03T00:00:00Z", window_end="2026-01-04T00:00:00Z")
    assert result["calls"] == 1 and result["read_tokens"] == 20


def test_diagnostic_cli_accepts_a_session_manifest(tmp_path, monkeypatch, capsys):
    from _measure import structural_report
    before, after, manifest = tmp_path / "before.jsonl", tmp_path / "after.jsonl", tmp_path / "manifest.json"
    before.write_text(json.dumps(attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)) + "\n")
    after.write_text("\n".join(json.dumps(r) for r in [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
                                                       call("2026-01-02T00:00:01Z", "r1", 100)]))
    other = tmp_path / "other.jsonl"
    other.write_text("\n".join(json.dumps(r) for r in [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20),
                                                       call("2026-01-02T00:00:01Z", "r2", 100)]))
    manifest.write_text(json.dumps([{"session_id": "a", "path": str(after)}, {"session_id": "a", "path": str(after)},
                                    {"session_id": "wrong", "path": str(after)}, {"session_id": "b", "path": str(other)}]))
    monkeypatch.setattr(structural_report._compat, "facade", lambda: object())
    monkeypatch.setattr(structural_report.value, "card_resolver", lambda facade: card)
    assert structural_report.cli(["observed-change", "--before", str(before), "--after-manifest", str(manifest),
        "--changed-at", "2026-01-02T00:00:00Z", "--component", "skill_listing"]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == 2
