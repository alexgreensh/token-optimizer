"""Prompt-time decisions and the evidence behind cold-session estimates."""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from test_userpromptsubmit_runner import _load_runner


OLD_SESSION = "32fd4317-a139-4d04-8bd2-8ac19a725600"
FRESH_SESSION = "7bd3b738-c8e0-40df-90c9-eac843d76581"
MODEL = "claude-opus-4-6"


def _stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _usage(ts, session_id=OLD_SESSION, context=412_000, write=2000, model=MODEL, ttl="1h"):
    return {
        "type": "assistant", "sessionId": session_id, "uuid": str(uuid4()),
        "timestamp": _stamp(ts), "isSidechain": False,
        "message": {
            "id": f"msg_{uuid4().hex}", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "Fixture response"}],
            "usage": {
                "input_tokens": 100, "output_tokens": 120,
                "cache_read_input_tokens": context - write - 100,
                "cache_creation_input_tokens": write,
                "cache_creation": {f"ephemeral_{ttl}_input_tokens": write},
            },
        },
    }


def _append(path, *records):
    with path.open("a", encoding="utf-8") as stream:
        for row in records:
            stream.write(json.dumps(row) + "\n")


def _user(ts):
    return {
        "type": "user", "timestamp": _stamp(ts),
        "message": {"role": "user", "content": "Start the fixture"},
    }


@pytest.fixture
def guard(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    from _measure import cold_guard, keepwarm_safety

    facade = runner.measure
    claude = tmp_path / "claude"
    snapshot = tmp_path / "state"
    snapshot.mkdir()
    for attr, value in {
        "CLAUDE_DIR": claude, "SNAPSHOT_DIR": snapshot, "CONFIG_DIR": snapshot,
        "CONFIG_PATH": snapshot / "config.json", "TRENDS_DB": snapshot / "trends.db",
        "_CONFIG_LOCK_PATH": snapshot / "config.lock",
        "CHECKPOINT_DIR": snapshot / "checkpoints",
    }.items():
        monkeypatch.setattr(facade, attr, value)
    for name in ("TOKEN_OPTIMIZER_COLD_GUARD", "TOKEN_OPTIMIZER_KEEPWARM_PING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(facade, "_load_pricing_tier", lambda: "anthropic")
    monkeypatch.setattr(keepwarm_safety, "session_owner", lambda *a: {"pid": 42, "start": "fixture"})
    project = claude / "projects" / "-work-example"
    project.mkdir(parents=True)
    transcript = project / f"{OLD_SESSION}.jsonl"
    hook_input = {
        "hook_event_name": "UserPromptSubmit", "session_id": OLD_SESSION,
        "transcript_path": str(transcript), "cwd": str(tmp_path),
        "prompt": "Resume the work verbatim",
    }
    now = time.time()
    _append(transcript, _user(now - 8042), _usage(now - 8041))
    marker = snapshot / "cold-guard" / (hashlib.sha256(OLD_SESSION.encode()).hexdigest() + ".json")
    monkeypatch.setattr(runner, "_read_hook_input", lambda: hook_input)
    monkeypatch.setattr(runner, "_check_consent", lambda: True)
    monkeypatch.setattr(runner, "_harness_only_context", lambda: True)
    calls = []
    for sub in ("prompt_continuity", "verbosity_steer", "quality_cache_warn",
                "ensure_health", "quality_cache_force", "compact_restore"):
        monkeypatch.setattr(runner, f"_sub_{sub}", lambda _, s=sub: calls.append(s))
    return runner, facade, cold_guard, hook_input, transcript, marker, calls


@pytest.mark.parametrize("case", ["warm", "small", "slash", "subagent", "off", "empty", "ping"])
def test_ineligible_prompts_pass(guard, case, monkeypatch, capsys):
    runner, _, _, hook_input, transcript, marker, calls = guard
    if case == "warm":
        _append(transcript, _usage(time.time() - 3000, ttl="5m"))
    elif case == "small":
        _append(transcript, _usage(time.time() - 5000, context=250_000))
    elif case == "slash":
        hook_input["prompt"] = "  /compact"
    elif case == "subagent":
        hook_input["agent_id"] = "agent-fixture"
    elif case == "off":
        monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "off")
    elif case == "empty":
        hook_input["prompt"] = " "
    elif case == "ping":
        monkeypatch.setenv("TOKEN_OPTIMIZER_KEEPWARM_PING", "")
    assert runner.main() == 0
    assert capsys.readouterr().out == ""
    assert not marker.exists()
    assert len(calls) == 6


def test_warn_once_with_bookkeeping(guard, capsys):
    runner, facade, _, hook_input, _, marker, calls = guard
    assert runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out) == ["systemMessage"]
    assert "\n" not in out["systemMessage"]
    assert "idle 2h 14m" in out["systemMessage"]
    assert "412K tokens at write price, about $4.12 at API rates" in out["systemMessage"]
    saved = json.loads(marker.read_text())
    assert saved["kind"] == "warn"
    assert saved["model"] == MODEL and saved["context"] == 412_000
    assert marker.stat().st_mode & 0o777 == 0o600
    assert hook_input["prompt"] not in marker.read_text()
    assert runner.main() == 0
    assert capsys.readouterr().out == ""
    assert len(calls) == 12
    with facade._open_trends_db_readonly() as conn:
        assert conn.execute(
            "SELECT tier, detail FROM compression_events WHERE feature='cold_guard'"
        ).fetchall() == [("estimated", "warn")]


def test_block_is_published_after_marker_and_skips_other_hooks(guard, monkeypatch, capsys):
    runner, facade, _, _, _, marker, calls = guard
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "ask")
    import sys

    class VerifyBeforeWrite:
        def write(self, value):
            if value.strip():
                assert json.loads(marker.read_text())["kind"] == "block"
            return original.write(value)

        def flush(self):
            original.flush()

    original = sys.stdout
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "stdout", VerifyBeforeWrite())
        assert runner.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"decision", "reason"}
    assert result["decision"] == "block"
    assert "A checkpoint is saved." in result["reason"]
    assert "send the same message again. It goes through." in result["reason"]
    assert list(facade.CHECKPOINT_DIR.glob("*.md"))
    assert not calls
    assert runner._RUNNER_DEADLINE is None
    for name in ("ensure-health", "quality-cache-force", "compact-restore-new-session"):
        once = facade._once_per_session_marker(name, OLD_SESSION)
        assert once is not None and not once.exists()
    assert runner.main() == 0
    assert capsys.readouterr().out == ""
    assert len(calls) == 6


def test_read_only_marker_directory_passes(guard, monkeypatch, capsys):
    runner, _, _, _, _, marker, calls = guard
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "ask")
    marker.parent.mkdir(mode=0o500)
    try:
        assert runner.main() == 0
        assert capsys.readouterr().out == ""
        assert not marker.exists()
        assert len(calls) == 6
    finally:
        marker.parent.chmod(0o700)


def test_new_gap_warns_during_cooldown_then_may_block(guard, monkeypatch):
    _, facade, cold_guard, hook_input, transcript, marker, _ = guard
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "ask")
    now = time.time()
    assert cold_guard.evaluate(facade, hook_input, now)["kind"] == "block"
    _append(transcript, _usage(now - 4000))
    assert cold_guard.evaluate(facade, hook_input, now + 1800)["kind"] == "warn"
    _append(transcript, _usage(now - 3800))
    assert cold_guard.evaluate(facade, hook_input, now + 3660)["kind"] == "block"
    assert json.loads(marker.read_text())["kind"] == "block"


@pytest.mark.parametrize("case", ["image", "pasted", "attachment", "no_owner", "checkpoint_failed"])
def test_ask_falls_back_to_warn(guard, case, monkeypatch, capsys):
    from _measure import keepwarm_safety

    runner, facade, _, hook_input, _, marker, calls = guard
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "ask")
    if case == "image":
        hook_input["prompt"] += " [Image #1]"
    elif case == "pasted":
        hook_input["prompt"] += " [Pasted text #2]"
    elif case == "attachment":
        hook_input["attachments"] = [{"type": "image"}]
    elif case == "no_owner":
        monkeypatch.setattr(keepwarm_safety, "session_owner", lambda *a: None)
    else:
        monkeypatch.setattr(facade, "compact_capture", lambda **kw: None)
    assert runner.main() == 0
    assert list(json.loads(capsys.readouterr().out)) == ["systemMessage"]
    assert json.loads(marker.read_text())["kind"] == "warn"
    assert len(calls) == 6


@pytest.mark.parametrize("case", ["exception", "overrun"])
def test_guard_failure_leaves_other_hooks_running(guard, case, monkeypatch, capsys):
    runner, _, cold_guard, _, _, marker, calls = guard

    def fail(*args):
        if case == "exception":
            raise RuntimeError("fixture")
        time.sleep(3)
        return {"kind": "block", "text": "Must never be printed"}

    monkeypatch.setattr(cold_guard, "evaluate", fail)
    started = time.monotonic()
    assert runner.main() == 0
    assert time.monotonic() - started < 2.8
    out = capsys.readouterr()
    assert out.out == out.err == ""
    assert not marker.exists()
    assert len(calls) == 6


def test_warn_survives_with_other_subcommand_output(guard, monkeypatch, capsys):
    runner, _, _, _, _, _, _ = guard
    monkeypatch.setattr(runner, "_sub_prompt_continuity", lambda _: print("Continue here"))
    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["systemMessage"].startswith("Token Optimizer: cache went cold")
    assert payload["hookSpecificOutput"]["additionalContext"] == "Continue here"


@pytest.mark.parametrize("choice,credited,expected", [
    ("continue", False, 0.0), ("compact", False, 0.0), ("unknown", False, 0.0),
    ("fresh", False, 3.80), ("fresh", True, 0.0),
])
def test_resolve_and_value_only_fresh_outcomes(guard, monkeypatch, choice, credited, expected):
    _, facade, cold_guard, hook_input, transcript, marker, _ = guard
    now = time.time() - 670
    _append(transcript, _usage(now - 3900, context=431_579))
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "ask")
    assert cold_guard.evaluate(facade, hook_input, now)["kind"] == "block"
    if choice == "compact":
        _append(transcript, {"type": "system", "subtype": "compact_boundary", "timestamp": _stamp(now + 40)})
    if choice in {"compact", "continue"}:
        _append(transcript, _usage(now + 90))
    elif choice == "fresh":
        fresh = transcript.with_name(f"{FRESH_SESSION}.jsonl")
        _append(fresh, _user(now + 30), _usage(now + 38, FRESH_SESSION, context=31_000, write=30_000))
    if credited:
        with facade._init_trends_db() as conn:
            conn.execute("INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id) "
                         "VALUES (?, 'resume_lean', 1, 1, ?)", (_stamp(now), OLD_SESSION))
    cold_guard.resolve(facade, now + 599)
    assert "choice" not in json.loads(marker.read_text())
    summary = {"period_days": 30}
    cold_guard.on_savings_refresh(facade, summary)
    assert json.loads(marker.read_text())["choice"] == choice
    pool = summary["cold_guard_estimated"]
    assert pool["cost_saved_usd"] == pytest.approx(expected)
    if choice == "fresh" and not credited:
        assert pool["gross_usd"] == pytest.approx(4.10, abs=0.0001)
        assert pool["handover_cost_usd"] == pytest.approx(0.30)


def test_unknown_model_omits_price_and_pruning_stops_at_50(guard, capsys):
    runner, _, _, _, transcript, marker, _ = guard
    _append(transcript, _usage(time.time() - 4800, model="claude-unknown-fixture"))
    marker.parent.mkdir()
    old = time.time() - 15 * 86400
    for i in range(52):
        stale = marker.parent / f"{i:064x}.json"
        stale.write_text("{}")
        os.utime(stale, (old, old))
    assert runner.main() == 0
    text = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "tokens at write price." in text and "$" not in text
    assert len(list(marker.parent.glob("*.json"))) == 3


def test_config_cli_and_plan_item(guard, monkeypatch, capsys):
    _, facade, cold_guard, _, _, _, _ = guard
    monkeypatch.setattr(facade, "_estimate_cache_drop_savings", lambda days: {
        "est_drops": 4, "cost_saved_usd": 5.25,
    })
    item = cold_guard.plan_item(facade)
    assert item["id"] == "cold_guard:ask" and item["net_usd_month"] is None
    assert item["basis"] == "4 cold resumes cost $5.25 in your last 30 days; the saving depends on what you choose each time"
    assert cold_guard.apply_item(facade, item) == {'ok': True, 'before': 'warn', 'after': 'ask'}
    assert cold_guard.plan_item(facade) is None
    assert cold_guard.main(facade, ["set", "off"]) == 0
    assert facade._read_config_flag("cold_guard_mode") == "off"
    capsys.readouterr()
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "warn")
    assert cold_guard.main(facade, ["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mode"] == "warn"
    assert status["pool"]["events"] == 4
    assert status["counts"] == dict.fromkeys(("continue", "compact", "fresh", "unknown"), 0)
    assert cold_guard.main(facade, ["pool", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cost_saved_usd"] == 5.25


def test_savings_json_keeps_cold_guard_in_estimated_tier(guard, monkeypatch, capsys):
    _, facade, cold_guard, _, _, _, _ = guard
    facade._init_trends_db().close()
    monkeypatch.setattr(cold_guard, "estimate", lambda *a, **kw: {
        "label": "Cold-session guard", "cost_saved_usd": 3.80, "events": 1,
    })
    facade.savings_report(days=30, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["cold_guard_estimated"]["label"] == "Cold-session guard"
    assert payload["action_savings"]["estimates_usd"]["cold_guard_estimated"] == 3.80
    assert "cold_guard_estimated" not in payload["by_category"]


def test_signed_mixed_handover_increases_no_measured_limit_savings(guard, monkeypatch):
    from _measure import limit_units

    _, facade, cold_guard, hook_input, transcript, _, _ = guard
    now = time.time() - 670
    monkeypatch.setenv("TOKEN_OPTIMIZER_COLD_GUARD", "ask")
    assert cold_guard.evaluate(facade, hook_input, now)["kind"] == "block"
    fresh = transcript.with_name(f"{FRESH_SESSION}.jsonl")
    reply = _usage(now + 60, FRESH_SESSION, context=510_000, write=500_000)
    reply["message"]["usage"]["cache_creation"] = {
        "ephemeral_1h_input_tokens": 400_000, "ephemeral_5m_input_tokens": 100_000,
    }
    _append(fresh, _user(now + 30), reply)
    summary = {"period_days": 30, "total_tokens": 0}
    cold_guard.on_savings_refresh(facade, summary)
    estimate = summary["cold_guard_estimated"]
    assert estimate["cost_saved_usd"] == pytest.approx(3.914 - 4.625)
    assert estimate["limit_units_saved"] == pytest.approx(-142_200)

    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE session_log (parent_daily_usage_json TEXT)")
        activity = {datetime.now().date().isoformat(): {MODEL: {"fresh_input": 2_000_000}}}
        conn.execute("INSERT INTO session_log VALUES (?)", (json.dumps(activity),))
        limits = limit_units.plan_limits_summary(facade, conn, 30, summary=summary)
    assert limits["saved_units"] == 0.0
    assert limits["estimated_by_feature"]["cold_guard_estimated"] == {
        "label": "Cold-session guard", "saved_units": -142_200, "share_pct": -7.11,
    }


@pytest.mark.parametrize("story_active", [False, True])
def test_dashboard_renders_estimate_label(guard, story_active):
    from test_savings_story_render import render_savings

    _, facade, _, _, _, _, _ = guard
    summary = {
        "period_days": 30, "total_cost_usd": 20, "session_effect": {"available": story_active},
        "cold_guard_estimated": {"label": "Cold-session guard", "events": 1, "cost_saved_usd": -3.80},
    }
    summary["action_savings"] = facade._action_savings_components(summary)
    html = render_savings(summary, with_story=story_active)
    assert "Cold-session guard" in html
    assert "-$3.80" in html
