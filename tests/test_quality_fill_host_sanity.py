"""Reproduction + pin for the reported anomaly: fresh session, host context
bar 16%, zero compactions, ContextQ score 59.

Root cause: ``compute_quality_score`` divided transcript ``context_tokens``
by an INFERRED context window (200k from env/config detection) while the real
session ran on a 1M window. 140k/200k produced a phantom 70% fill (fill_score
~18, ResourceHealth ~59) even though the host's own used_percentage — the same
number the statusline bar renders — said 16%. The fresh live-fill gate (10s)
discards same-session host readings older than 10 seconds, which is most of
them during long tool runs, so the wrong-denominator arithmetic won silently.

Fix contract pinned here: a same-session host fill reading that is still
recent (<5min, matching the statusline staleness convention) is a sanity
anchor. When it disagrees with our computed fill by >10 points, the host
wins — the host measures the real window; our denominator is two layers of
inference. The disagreement is still recorded in the breakdown so a real
misdetection stays diagnosable.
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    qc = tmp_path / "qc"
    qc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", qc, raising=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path, raising=True)
    yield mod, qc, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _qdata(**kw):
    """Minimal fresh-session quality_data at a true ~16% fill on a 1M window:
    ~140-160k of usage, a handful of messages, no compactions, no waste."""
    d = {
        "messages": [(0, "user", 4000, True), (1, "assistant", 8000, True)],
        "tool_results": [(2, "t1", 3000, False)],
        "tool_result_meta": [],
        "system_reminders": [],
        "reads": [],
        "writes": [],
        "compactions": 0,
        "tool_calls": 1,
        "agent_dispatches": [],
        "decisions": [],
        "total_entries": 3,
        "context_tokens": 140_000,
        "model": "claude-sonnet-4-6",
        # Session supplies no window -> the inferred global wins (or loses).
        "model_context_window": None,
        "topic": "fresh session",
    }
    d.update(kw)
    return d


def _write_live_fill(qc: Path, sid: str, pct: float, age_s: float):
    (qc / "live-fill.json").write_text(json.dumps({
        "used_percentage": pct,
        "session_id": sid,
        # statusline writes a JS (ms) timestamp
        "timestamp": int((time.time() - age_s) * 1000),
    }), encoding="utf-8")


def test_wrong_denominator_reproduces_reported_59(m, monkeypatch):
    """THE anomaly: 140k tokens / inferred 200k window = phantom 70% fill ->
    score ~59 while the host bar reads 16%. Post-fix the stale-but-same-session
    host reading wins the sanity check and the score lands near the 16% curve."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    sid = "sess-anomaly-1"
    # Same-session host reading 45s old — past the 10s authoritative gate, so
    # pre-fix it was ignored entirely and the phantom 70% filled in.
    _write_live_fill(qc, sid, 16.0, age_s=45)

    result = mod.compute_quality_score(_qdata(), session_id=sid)

    # Pre-fix this was ~59; the corrected score must sit near the 16% curve.
    assert result["score"] >= 80, (
        f"score {result['score']} — phantom-fill bug reproduced, host sanity "
        "did not rescue the wrong denominator")
    cfd = result["breakdown"]["context_fill_degradation"]
    assert cfd["fill_pct"] < 25
    assert cfd["host_disagreement"] is not None


def test_disagreement_is_recorded_even_without_override(m, monkeypatch):
    """A host reading that agrees roughly with our arithmetic changes nothing
    and records nothing."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (1_000_000, "default (1M)"))
    sid = "sess-agree"
    _write_live_fill(qc, sid, 15.0, age_s=60)
    result = mod.compute_quality_score(_qdata(), session_id=sid)
    # 140k/1M = 14% vs host 15% — within margin, no disagreement, healthy score.
    assert result["breakdown"]["context_fill_degradation"]["host_disagreement"] is None
    assert result["score"] >= 80


def test_stale_host_beyond_sanity_window_does_not_override(m, monkeypatch):
    """A live-fill older than the sanity cap describes a different moment and
    must not drag a genuinely-filled session's score back up."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    sid = "sess-stale"
    _write_live_fill(qc, sid, 16.0, age_s=3600)
    result = mod.compute_quality_score(_qdata(), session_id=sid)
    # No rescue: the phantom 70% stands (that is its own bug to flag, but the
    # sanity anchor is correctly out of reach).
    assert result["score"] < 80


def test_other_session_live_fill_never_used(m, monkeypatch):
    """The sanity anchor only applies to THIS session's host reading."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    _write_live_fill(qc, "somebody-elses-session", 16.0, age_s=30)
    result = mod.compute_quality_score(_qdata(), session_id="sess-mine")
    assert result["score"] < 80


def test_fresh_host_still_authoritative(m, monkeypatch):
    """The existing <10s path is unchanged: fresh host fill is used directly."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "default"))
    sid = "sess-live"
    _write_live_fill(qc, sid, 16.0, age_s=2)
    result = mod.compute_quality_score(_qdata(), session_id=sid)
    assert result["score"] >= 80
    assert result["breakdown"]["context_fill_degradation"]["fill_pct"] == 16.0


def _write_live_fill_tokens(qc: Path, sid: str, pct: float, age_s: float, tokens: int, window: int):
    (qc / "live-fill.json").write_text(json.dumps({
        "used_percentage": pct,
        "context_tokens": tokens,
        "context_window": window,
        "session_id": sid,
        "timestamp": int((time.time() - age_s) * 1000),
    }), encoding="utf-8")


def test_pre_compact_host_reading_does_not_override_post_compact_transcript(m, monkeypatch):
    """session_id survives /compact, so a 60s-old host reading from
    just before the compact (92%, 184k tokens) still "describes this session".
    The transcript now says 20k tokens: the reading describes a different
    moment (a numerator change, not a wrong denominator), so it must not win."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window", lambda: (200_000, "default"))
    sid = "sess-post-compact"
    _write_live_fill_tokens(qc, sid, 92.0, age_s=60, tokens=184_000, window=200_000)
    result = mod.compute_quality_score(_qdata(context_tokens=20_000, compactions=1), session_id=sid)
    fd = result["breakdown"]["context_fill_degradation"]
    assert fd.get("fill_source") != "host-stale-override"
    assert fd["fill_pct"] == pytest.approx(10.0, abs=0.5)
    assert result["score"] >= 80


def test_denominator_error_still_overrides_when_host_tokens_match(m, monkeypatch):
    """The override this check exists for is unchanged: the host's token count
    matches ours (same moment), only the window we inferred is wrong."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    sid = "sess-denominator"
    _write_live_fill_tokens(qc, sid, 14.0, age_s=45, tokens=145_000, window=1_000_000)
    result = mod.compute_quality_score(_qdata(context_tokens=140_000), session_id=sid)
    fd = result["breakdown"]["context_fill_degradation"]
    assert fd.get("fill_source") == "host-stale-override"
    assert result["score"] >= 80

# Advisor responses sum executor iterations for billing, not context occupancy.
def _advisor_usage():
    def executor(ctx, read):
        return {"type": "message", "input_tokens": 100,
                "cache_creation_input_tokens": ctx - read - 100,
                "cache_read_input_tokens": read, "output_tokens": 12}
    return {"input_tokens": 200, "cache_creation_input_tokens": 180_827,
            "cache_read_input_tokens": 800_000, "output_tokens": 24,
            "iterations": [executor(489_876, 400_000),
                           {"type": "advisor_message", "input_tokens": 494_410},
                           executor(491_151, 400_000)]}


def _advisor_transcript(mod, root, usage):
    project = root / "projects" / "-advisor"
    project.mkdir(parents=True, exist_ok=True)
    path = project / "advisor-session.jsonl"
    path.write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-10-27T10:00:00Z", "requestId": "advisor-request",
        "message": {"model": "claude-sonnet-5-5", "usage": usage,
                    "content": [{"type": "tool_use", "id": "tool-advisor",
                                 "name": "advisor", "input": {}}]}}) + "\n",
        encoding="utf-8")
    return path


def test_advisor_context_uses_final_executor_and_bills_advisor_on_top(m, monkeypatch):
    mod, _, root = m
    usage = _advisor_usage()
    path = _advisor_transcript(mod, root, usage)
    q = mod._parse_jsonl_for_quality(path)
    assert q["context_tokens"] == 491_151  # not the 981,027 billed sum
    q["model_context_window"] = 1_000_000
    monkeypatch.setattr(mod, "detect_context_window", lambda: (1_000_000, "test"))
    score = mod.compute_quality_score(q)
    assert score["breakdown"]["context_fill_degradation"]["fill_pct"] == pytest.approx(49.1151, abs=.1)
    assert mod._transcript_last_turn("advisor-session")[0] == 491_151
    replay = mod._advice_session_data(path)
    assert replay["turns"] == [491_151]
    assert replay["cache_reads"] == [800_000]
    assert mod._keepwarm_prefix_proxy(usage) == 491_051
    # These are billing APIs: every paid executor iteration still counts.
    billed_session = mod._parse_session_jsonl(path)
    billed_turn = mod.parse_session_turns(path)[0]
    assert billed_turn["input_tokens"] == 200
    assert billed_turn["cache_read"] == 800_000
    assert billed_turn["cache_creation"] == 180_827
    assert billed_turn["output_tokens"] == 24
    plain = dict(usage)
    plain.pop("iterations")
    baseline = _advisor_transcript(mod, root, plain)
    # Executor billing is unchanged; the advisor iteration (no model on it,
    # so priced at the executor's) is added on top, never dropped.
    advisor_cost = mod._get_model_cost("claude-sonnet-5-5", 494_410, 0, per_request=True)
    assert billed_turn["cost_usd"] == pytest.approx(
        mod.parse_session_turns(baseline)[0]["cost_usd"] + advisor_cost, abs=1e-6)
    plain_session = mod._parse_session_jsonl(baseline)
    assert billed_session["total_input_tokens"] == plain_session["total_input_tokens"] + 494_410
    assert billed_session["total_output_tokens"] == plain_session["total_output_tokens"]
    assert billed_session["api_calls"] == plain_session["api_calls"] == 1


@pytest.mark.parametrize("iterations", [None, "x", {}, [], [None, 5],
    [{"type": "advisor_message", "input_tokens": 600_000}],
    [{"type": "message"}],
    [{"type": "message", "input_tokens": 50}, {"type": "message"}],
    [{"type": "message", "input_tokens": "bad"}],
    [{"type": "message", "input_tokens": float("inf")}],
])
def test_unusable_executor_usage_keeps_aggregate_fallback(m, iterations):
    mod, _, root = m
    usage = {"input_tokens": 100, "cache_read_input_tokens": 200,
             "cache_creation_input_tokens": 30, "iterations": iterations}
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 330
    assert mod._advice_session_data(path)["turns"] == [330]
    assert mod._transcript_last_turn("advisor-session")[0] == 330
    assert mod._keepwarm_prefix_proxy(usage) == 230


def test_advisor_tail_is_not_executor_and_nested_cache_is_not_lost(m):
    mod, _, root = m
    usage = _advisor_usage()
    last = usage["iterations"][-1]
    last.pop("cache_creation_input_tokens")
    last["cache_creation"] = {"ephemeral_1h_input_tokens": 50_000,
                              "ephemeral_5m_input_tokens": 41_051}
    usage["iterations"] += [None, {"type": "advisor_message", "input_tokens": 900_000}]
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 491_151
    assert mod._advice_session_data(path)["turns"] == [491_151]
    assert mod._keepwarm_prefix_proxy(usage) == 491_051


@pytest.mark.parametrize("usage, expected", [
    ({"input_tokens": 10, "cache_creation": {
        "ephemeral_1h_input_tokens": "20", "ephemeral_5m_input_tokens": 30}}, 60),
    ({"input_tokens": 10, "cache_creation_input_tokens": 40,
      "cache_creation": {"ephemeral_1h_input_tokens": 40}}, 50),
    ({"input_tokens": "bad", "cache_read_input_tokens": float("nan")}, 0),
    (None, 0), ([], 0),
])
def test_context_token_count_handles_cache_shapes(m, usage, expected):
    mod, _, _ = m
    assert mod._context_token_count(usage) == expected


def test_second_reported_advisor_record_uses_latest_executor(m):
    mod, _, root = m
    usage = {"input_tokens": 1_032_706, "iterations": [
        {"type": "message", "input_tokens": 515_846},
        {"type": "advisor_message", "input_tokens": 900_000},
        {"type": "message", "input_tokens": 516_860}]}
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 516_860
    assert mod._advice_session_data(path)["turns"] == [516_860]
    assert mod._transcript_last_turn("advisor-session")[0] == 516_860


@pytest.mark.parametrize("selected", [False, True])
def test_top_level_ttl_splits_describe_plain_and_executor_context(m, selected):
    mod, _, root = m
    executor = {"type": "message", "input_tokens": 100,
                "cache_read_input_tokens": 400_000,
                "ephemeral_1h_input_tokens": 50_000,
                "ephemeral_5m_input_tokens": 41_051}
    usage = ({"input_tokens": 200, "cache_read_input_tokens": 800_000,
              "iterations": [executor]} if selected else executor)
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 491_151
    assert mod._advice_session_data(path)["turns"] == [491_151]
    assert mod._transcript_last_turn("advisor-session")[0] == 491_151
    assert mod._keepwarm_prefix_proxy(usage) == 491_051


@pytest.mark.parametrize("bucket", ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "nested_1h", "top_5m"])
@pytest.mark.parametrize("bad", ["bad", float("inf"), float("nan"), None, -1])
def test_corrupt_executor_bucket_with_positive_counts_falls_back(m, bucket, bad):
    mod, _, root = m
    usage = _advisor_usage()
    executor = usage["iterations"][-1]
    if bucket.startswith("nested"):
        executor["cache_creation_input_tokens"] = 0
        executor["cache_creation"] = {"ephemeral_1h_input_tokens": bad,
                                      "ephemeral_5m_input_tokens": 91_051}
    elif bucket.startswith("top"):
        executor.pop("cache_creation_input_tokens")
        executor["ephemeral_5m_input_tokens"] = bad
        executor["ephemeral_1h_input_tokens"] = 91_051
    else:
        executor[bucket] = bad
    path = _advisor_transcript(mod, root, usage)
    assert mod._context_usage(usage) is usage
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 981_027
    assert mod._transcript_last_turn("advisor-session")[0] == 981_027
    assert mod._advice_session_data(path)["turns"] == [981_027]
    assert mod._keepwarm_prefix_proxy(usage) == 980_827


@pytest.mark.parametrize("executor, expected", [
    ({"type": "message", "input_tokens": "100", "cache_read_input_tokens": "400000", "cache_creation_input_tokens": 91051.0}, 491151),
    ({"type": "message", "input_tokens": 100}, 100),
    ({"type": "message", "cache_read_input_tokens": 400000}, 400000),
    ({"type": "message", "input_tokens": 100, "cache_creation_input_tokens": 20,
      "cache_creation": {"ephemeral_1h_input_tokens": "unused"}}, 120),
    ({"type": "message", "input_tokens": 100, "cache_creation_input_tokens": 20,
      "cache_creation": None}, 120),
    ({"type": "message", "input_tokens": 100, "cache_creation": {
        "ephemeral_1h_input_tokens": 20}, "ephemeral_1h_input_tokens": "unused"}, 120),
])
def test_executor_numeric_encodings_and_omitted_buckets_remain_supported(m, executor, expected):
    mod, _, _ = m
    usage = _advisor_usage()
    usage["iterations"][-1] = executor
    assert mod._context_usage(usage) is executor
    assert mod._context_token_count(executor) == expected


@pytest.mark.parametrize("nested", [None, "bad", [], 5])
def test_malformed_executor_cache_creation_container_falls_back(m, nested):
    mod, _, _ = m
    usage = _advisor_usage()
    executor = usage["iterations"][-1]
    executor.pop("cache_creation_input_tokens")
    executor["cache_creation"] = nested
    assert mod._context_usage(usage) is usage


@pytest.mark.parametrize("warm", [True, False])
@pytest.mark.parametrize("flip_final", [True, False])
def test_advisor_keepwarm_books_executor_resume_once(m, monkeypatch, warm, flip_final):
    mod, _, root = m
    read, write = (350_000, 150_000) if warm else (150_000, 350_000)
    executor = {"type": "message", "input_tokens": 100,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write}
    final = dict(executor)
    if flip_final:
        final["cache_read_input_tokens"] = 0 if warm else 500_000
        final["cache_creation_input_tokens"] = 500_000 if warm else 0
    usage = {"input_tokens": 200,
             "cache_read_input_tokens": read + final["cache_read_input_tokens"],
             "cache_creation_input_tokens": write + final["cache_creation_input_tokens"],
             "iterations": [dict(executor),
                 {"type": "advisor_message", "input_tokens": 900_000}, final]}
    path = _advisor_transcript(mod, root, usage)
    arm_ts = mod._keepwarm_iso_to_epoch("2026-10-27T09:00:00Z")
    turn = mod._keepwarm_first_usage_after(path, arm_ts)
    assert turn["cache_read"] == usage["cache_read_input_tokens"]
    assert turn["cache_creation"] == usage["cache_creation_input_tokens"]
    billed_cost = turn["cost_usd"]
    assert mod.keepwarm_resume_is_warm(turn, 500_000) is warm
    markers, bookings = [], []
    monkeypatch.setattr(mod, "classify_keepwarm_record", lambda *a, **kw: "resumed")
    monkeypatch.setattr(mod, "_keepwarm_load_ledger_rows", lambda: markers)
    monkeypatch.setattr(mod, "_keepwarm_ledger_append", lambda row: markers.append(row))
    def book(**kw):
        bookings.append(kw)
        return 1
    monkeypatch.setattr(mod, "_keepwarm_log_realized_savings", book)
    rec = {"session_id": "advisor-session", "ts": arm_ts,
           "transcript_path": str(path), "prefix_proxy": 500_000,
           "model": "claude-sonnet-5-5", "pings_fired": [arm_ts + 30]}
    result = mod.keepwarm_detect_realized(now=arm_ts + 7200, records=[rec])
    assert result["realized"] == int(warm)
    assert result["loss"] == int(not warm)
    assert markers[0]["status"] == ("realized" if warm else "loss")
    assert len(bookings) == int(warm)
    repeat = mod.keepwarm_detect_realized(now=arm_ts + 7300, records=[rec])
    assert repeat["realized"] == 0
    assert len(markers) == 1
    assert len(bookings) == int(warm)
    plain = dict(usage)
    plain.pop("iterations")
    _advisor_transcript(mod, root, plain)
    advisor_cost = mod._get_model_cost("claude-sonnet-5-5", 900_000, 0, per_request=True)
    assert billed_cost == pytest.approx(
        mod.parse_session_turns(path)[0]["cost_usd"] + advisor_cost, abs=1e-6)


def test_keepwarm_ordinary_and_invalid_executor_keep_aggregate_comparison(m):
    mod, _, root = m
    usage = {"input_tokens": 100, "cache_read_input_tokens": 350_000,
             "cache_creation_input_tokens": 150_000}
    for iterations in (None, [{"type": "message", "input_tokens": 100,
                              "cache_read_input_tokens": "bad"}]):
        if iterations is not None:
            usage["iterations"] = iterations
        path = _advisor_transcript(mod, root, usage)
        turn = mod.parse_session_turns(path)[0]
        assert "resume_cache_read" not in turn
        assert "resume_cache_creation" not in turn
        assert mod.keepwarm_resume_is_warm(turn, 500_000) is True


@pytest.mark.parametrize("first", [None, {"type": "message"},
    {"type": "message", "input_tokens": 100, "cache_read_input_tokens": "bad"}])
def test_resume_never_skips_unusable_first_executor_for_warm_later_one(m, first):
    mod, _, root = m
    first = first if first is not None else {"type": "message", "input_tokens": None}
    usage = {"input_tokens": 200, "cache_creation_input_tokens": 500_000,
        "cache_read_input_tokens": 500_000, "iterations": [first,
            {"type": "message", "input_tokens": 100,
             "cache_read_input_tokens": 500_000}]}
    path = _advisor_transcript(mod, root, usage)
    turn = mod.parse_session_turns(path)[0]
    assert "resume_cache_read" not in turn
    assert mod.keepwarm_resume_is_warm(turn, 500_000) is False


def test_initial_cold_write_cannot_be_credited_when_final_executor_reads_it(m):
    mod, _, root = m
    usage = {"input_tokens": 200, "cache_read_input_tokens": 500_000,
        "cache_creation_input_tokens": 500_000, "iterations": [
            {"type": "message", "input_tokens": 100, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 500_000},
            {"type": "message", "input_tokens": 100, "cache_read_input_tokens": 500_000,
             "cache_creation_input_tokens": 0}]}
    path = _advisor_transcript(mod, root, usage)
    turn = mod.parse_session_turns(path)[0]
    assert turn["resume_cache_read"] == 0
    assert turn["resume_cache_creation"] == 500_000
    assert mod.keepwarm_resume_is_warm(turn, 500_000) is False
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 500_100
