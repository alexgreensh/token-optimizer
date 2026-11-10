"""Budget observation is append-only and never changes the user's setting."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"))
from _measure.setup_budget_monitor import record, decreases, observe_hook, evaluate
from test_setup_attachment_counter import attachment, call, card
import json


def test_only_a_real_numeric_decrease_within_scope_is_a_candidate():
    db = sqlite3.connect(":memory:")
    assert record(db, "one", "project-a", "before.jsonl", 10000, "2026-01-01T00:00:00Z")
    assert record(db, "two", "project-b", "other.jsonl", 2000, "2026-01-01T01:00:00Z")
    assert record(db, "three", "project-a", "after.jsonl", 5000, "2026-01-02T00:00:00Z")
    assert not record(db, "three", "project-a", "after.jsonl", 5000, "2026-01-02T00:00:00Z")
    rows = decreases(db)
    assert len(rows) == 1 and rows[0]["old_budget"] == 10000 and rows[0]["new_budget"] == 5000
    assert rows[0]["before_transcript"] == "before.jsonl"


def test_missing_or_invalid_values_do_not_create_candidates():
    db = sqlite3.connect(":memory:")
    assert record(db, "one", "p", "path", None, "2026-01-01T00:00:00Z")
    assert not record(db, "one", "p", "path", "not-a-number", "2026-01-01T00:00:00Z")
    assert record(db, "two", "p", "path", 5000, "2026-01-02T00:00:00Z")
    assert decreases(db) == []


def test_unknown_sample_breaks_a_previous_numeric_chain():
    db = sqlite3.connect(":memory:")
    record(db, "one", "p", "path-1", 10000, "2026-01-01T00:00:00Z")
    record(db, "two", "p", "path-2", None, "2026-01-02T00:00:00Z")
    record(db, "three", "p", "path-3", 5000, "2026-01-03T00:00:00Z")
    assert decreases(db) == []


def test_simultaneous_conflicting_budgets_break_the_comparison_chain():
    db = sqlite3.connect(":memory:")
    record(db, "one", "p", "path-1", 10000, "2026-01-01T00:00:00Z")
    record(db, "two", "p", "path-2", 5000, "2026-01-02T00:00:00Z")
    record(db, "three", "p", "path-3", 7000, "2026-01-02T00:00:00Z")
    record(db, "four", "p", "path-4", 3000, "2026-01-03T00:00:00Z")
    assert decreases(db) == []


def test_hook_observes_without_mutating_environment_or_creating_database(tmp_path):
    db_path = tmp_path / "trends.db"
    hook = {"session_id": "s1", "cwd": "/project", "transcript_path": "/transcript-one"}
    env = {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "10000"}
    assert not observe_hook(hook, db_path, env, "2026-01-01T00:00:00Z")
    sqlite3.connect(str(db_path)).close()
    assert observe_hook(hook, db_path, env, "2026-01-01T00:00:00Z")
    assert env == {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "10000"}
    hook["session_id"], hook["transcript_path"] = "s2", "/transcript-two"
    env["SLASH_COMMAND_TOOL_CHAR_BUDGET"] = "5000"
    assert observe_hook(hook, db_path, env, "2026-01-02T00:00:00Z")
    with sqlite3.connect(str(db_path)) as conn:
        assert len(decreases(conn)) == 1


def test_remote_hook_does_not_record_local_budget(tmp_path):
    db_path = tmp_path / "trends.db"
    sqlite3.connect(str(db_path)).close()
    hook = {"session_id": "s", "cwd": "/project", "transcript_path": "/transcript"}
    assert not observe_hook(hook, db_path, {"CLAUDE_CODE_REMOTE": "true"}, "2026-01-01T00:00:00Z")


def test_sessionstart_wires_the_budget_observer(tmp_path, monkeypatch):
    from test_sessionstart_runner import _load_runner
    runner = _load_runner(monkeypatch, tmp_path)
    db_path = tmp_path / "trends.db"
    sqlite3.connect(str(db_path)).close()
    monkeypatch.setattr(runner.measure, "TRENDS_DB", db_path)
    monkeypatch.setattr(runner.measure, "detect_runtime", lambda: "claude")
    monkeypatch.setenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", "10000")
    runner._observe_setup_budget({"session_id": "s", "cwd": "/project", "transcript_path": "/transcript"})
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("SELECT budget FROM setup_budget_observations").fetchall() == [(10000,)]


def test_sessionstart_without_consent_does_not_observe_budget(tmp_path, monkeypatch):
    from test_sessionstart_runner import _load_runner
    runner = _load_runner(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {})
    monkeypatch.setattr(runner, "_check_consent", lambda: False)
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)
    monkeypatch.setattr(runner, "_runner_budget", lambda *a, **k: 8)
    monkeypatch.setattr(runner, "_sub_ensure_health", lambda *a: None)
    monkeypatch.setattr(runner, "_observe_setup_budget", lambda *a: calls.append(True))
    monkeypatch.setattr(runner, "_emit_session_start_stdout", lambda *a: None)
    assert runner.main() == 0 and calls == []


def test_candidate_uses_observed_calls_until_budget_changes(tmp_path):
    db = sqlite3.connect(":memory:")
    paths = [tmp_path / (str(i) + ".jsonl") for i in range(4)]
    rows = [
        [attachment("2026-01-01T00:00:00Z", "skill_listing", content="x" * 40)],
        [attachment("2026-01-02T00:00:00Z", "skill_listing", content="x" * 20), call("2026-01-02T00:00:01Z", "a", 100),
         call("2026-01-04T00:00:01Z", "after-restore", 100)],
        [attachment("2026-01-03T00:00:00Z", "skill_listing", content="x" * 20), call("2026-01-03T00:00:01Z", "b", 100)],
        [attachment("2026-01-04T00:00:00Z", "skill_listing", content="x" * 40), call("2026-01-04T00:00:01Z", "c", 100)],
    ]
    for i, path in enumerate(paths):
        path.write_text("\n".join(json.dumps(row) for row in rows[i]))
        record(db, str(i), "project", str(path), [10000, 5000, 5000, 10000][i], f"2026-01-0{i+1}T00:00:00Z")
    result = evaluate(db, len, card, "2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z")
    assert result["booked"] is False and len(result["candidates"]) == 1
    assert result["candidates"][0]["calls"] == 2 and result["candidates"][0]["read_tokens"] == 40
    assert "project" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


def test_budget_observations_cli_is_read_only(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from _measure import structural_report
    path = tmp_path / "trends.db"
    with sqlite3.connect(str(path)) as db:
        record(db, "one", "project", "/no-transcript", 10000, "2026-01-01T00:00:00Z")
    monkeypatch.setattr(structural_report._compat, "facade", lambda: SimpleNamespace(TRENDS_DB=path))
    monkeypatch.setattr(structural_report.value, "card_resolver", lambda facade: card)
    assert structural_report.cli(["budget-observations", "--days", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["booked"] is False
