"""Compaction recovery scoring + context-size advisory (COMPACTION-QUALITY.md).

Pins the contract that replaced the count-only compaction penalty:

- Compaction count is descriptive metadata with ZERO quality weight.
  ``score_version`` is explicit so pre-v2 caches are never silently
  compared or dampened against v2 scores.
- A separate recovery panel reports observed post-boundary signals
  (matched 20-call windows: errors, re-reads, repeats, loops, restore
  delivery) with status observing / no_concerning_signal_observed /
  needs_attention / unmeasurable. Missing telemetry is unknown, never
  "healthy".
- No 65/88/95% loss arithmetic survives anywhere in the result.
- The 300-400K context-size advisory (default 350K) shares one
  absolute-token gate with every compaction-recommending nudge, uses
  current-context telemetry only, and never fires below 300K or on
  missing/contradictory telemetry.
"""
from __future__ import annotations

import importlib
import json
import sys
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
    cpdir = tmp_path / "checkpoints"
    cpdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cpdir, raising=True)
    yield mod, qc, tmp_path, cpdir
    if "measure" in sys.modules:
        del sys.modules["measure"]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _qdata(**kw):
    """Minimal quality_data: a few messages, no compactions, clean signals."""
    d = {
        "reads": [],
        "writes": [],
        "tool_results": [],
        "tool_result_meta": [],
        "system_reminders": [],
        "messages": [(0, "user", 400, True), (1, "assistant", 800, True),
                     (2, "user", 200, True)],
        "compactions": 0,
        "tool_calls": 0,
        "agent_dispatches": [],
        "decisions": [],
        "total_entries": 3,
        "context_tokens": None,
        "model": "claude-sonnet-4-6",
        "model_context_window": 1_000_000,
        "compaction_events": [],
        "topic": "fixture",
    }
    d.update(kw)
    return d


def _metrics(calls, errors=0, rereads=0, rue=0, repeats=0, rae=0,
             loops=0, reads=0):
    return {
        "calls": calls, "errors": errors, "shell_nonzero": 0,
        "repeats": repeats, "repeat_after_error": rae,
        "reads": reads, "rereads": rereads, "rereads_unchanged": rue,
        "expansions": 0, "loop_notices": loops,
        "first_line": 0, "last_line": 0,
    }


def _event(ordinal, pre=None, post=None, *, matched=20, eligible=True,
           restore="none", capture=False, post_calls=20, **kw):
    ev = {
        "boundary_id": f"b{ordinal}",
        "ordinal": ordinal,
        "line": ordinal * 100,
        "ts": "2026-11-05T00:00:00Z",
        "trigger": "auto",
        "pre_tokens": 300_000,
        "post_tokens": 30_000,
        "host_version": "2.0.0",
        "model": "claude-sonnet-4-6",
        "pre_seg_calls": matched,
        "post_seg_calls": post_calls,
        "matched_calls": matched,
        "eligible": eligible,
        "pre": pre if pre is not None else _metrics(matched),
        "post": post if post is not None else _metrics(matched),
        "restore_transcript": restore in ("transcript", "both"),
        "restore_db": restore in ("db", "both"),
        "restore_evidence": restore,
        "capture_notice": capture,
        "db_loops": 0,
        "flags": [],
        "status": None,
        "score": None,
    }
    ev.update(kw)
    return ev


# ---------------------------------------------------------------------------
# Score: count-only penalty is gone
# ---------------------------------------------------------------------------

def test_score_version_is_explicit(m):
    mod, *_ = m
    res = mod.compute_quality_score(_qdata())
    assert res["score_version"] == 2


def test_compaction_count_has_zero_quality_weight(m):
    """3 compactions with no measurable recovery signal must score exactly
    like 0 compactions when every other input is identical."""
    mod, *_ = m
    base = _qdata()
    res0 = mod.compute_quality_score(base)
    heavy = _qdata(compactions=5, compaction_events=[])
    res5 = mod.compute_quality_score(heavy)
    assert res5["score"] == res0["score"]
    assert res5["resource_health"] == res0["resource_health"]
    assert res5["session_efficiency"] == res0["session_efficiency"]


def test_no_cumulative_loss_percentage_anywhere(m):
    mod, *_ = m
    res = mod.compute_quality_score(_qdata(compactions=3, compaction_events=[]))
    blob = json.dumps(res)
    for banned in ("cumulative_loss_pct", "cumulativeLossPct", "65%",
                   "88%", "95%", "context loss"):
        assert banned not in blob, f"{banned} survived in quality result"


def test_breakdown_compactions_is_descriptive_metadata(m):
    mod, *_ = m
    res = mod.compute_quality_score(_qdata(compactions=2, compaction_events=[]))
    bd = res["breakdown"]
    assert "compaction_depth" not in bd
    assert bd["compactions"]["count"] == 2
    assert "compaction_recovery" in bd
    assert res["recovery_status"] == "unmeasurable"
    assert res["recovery_evidence"] == []
    assert res["recovery_score"] is None


def test_resource_health_weights_renormalized(m):
    mod, *_ = m
    total = sum(mod._RESOURCE_HEALTH_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9
    assert "compaction_depth" not in mod._RESOURCE_HEALTH_WEIGHTS
    assert abs(mod._RESOURCE_HEALTH_WEIGHTS["context_fill_degradation"] - 5 / 7) < 1e-9
    assert abs(mod._RESOURCE_HEALTH_WEIGHTS["absolute_waste_tokens"] - 2 / 7) < 1e-9


def test_top_drag_never_names_compactions(m):
    mod, *_ = m
    res = mod.compute_quality_score(_qdata(compactions=9, compaction_events=[]))
    td = res.get("top_drag")
    if td:
        assert "compact" not in str(td.get("key", ""))
        assert "loss" not in str(td.get("label", "")).lower()


# ---------------------------------------------------------------------------
# Recovery assessment
# ---------------------------------------------------------------------------

def test_recovery_clean_window_is_no_concerning_signal(m):
    mod, *_ = m
    ev = _event(1, restore="transcript")
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "no_concerning_signal_observed"
    assert res["recovery_score"] == 100
    assert res["recovery_evidence"][0]["status"] == "no_concerning_signal_observed"


def test_recovery_observing_while_window_fills(m):
    mod, *_ = m
    ev = _event(1, matched=4, eligible=False, post_calls=4,
                pre=_metrics(30), post=_metrics(4))
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "observing"


def test_recovery_unmeasurable_without_events(m):
    mod, *_ = m
    res = mod.compute_quality_score(_qdata(compactions=2, compaction_events=[]))
    assert res["recovery_status"] == "unmeasurable"


def test_recovery_needs_attention_on_repeat_after_error(m):
    mod, *_ = m
    post = _metrics(20, rae=3)
    ev = _event(1, post=post, restore="transcript")
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "needs_attention"
    assert res["recovery_score"] < 100
    assert "repeated_failed_action" in res["recovery_evidence"][0]["flags"]


def test_recovery_needs_attention_on_error_spike(m):
    mod, *_ = m
    pre = _metrics(20, errors=1)
    post = _metrics(20, errors=5)
    ev = _event(1, pre=pre, post=post)
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "needs_attention"
    assert "error_rate_increase" in res["recovery_evidence"][0]["flags"]


def test_recovery_unchanged_rereads_flag(m):
    mod, *_ = m
    pre = _metrics(20, rue=1, rereads=2)
    post = _metrics(20, rue=6, rereads=8)
    ev = _event(1, pre=pre, post=post)
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "needs_attention"
    assert "unchanged_artifact_rereads" in res["recovery_evidence"][0]["flags"]


def test_recovery_legitimate_rereads_after_edit_not_flagged(m):
    """rereads_unchanged counts only paths with NO observed edit; a plain
    reread bump without the unchanged qualifier must not trip attention."""
    mod, *_ = m
    pre = _metrics(20, rereads=1)
    post = _metrics(20, rereads=6)  # rereads but rue stays 0
    ev = _event(1, pre=pre, post=post)
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "no_concerning_signal_observed"


def test_recovery_capture_without_delivery_is_structural(m):
    mod, *_ = m
    ev = _event(1, capture=True, restore="none")
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "needs_attention"
    assert "restore_not_delivered" in res["recovery_evidence"][0]["flags"]


def test_recovery_no_restore_alone_is_unknown_not_bad(m):
    """'no restore observed' is missing evidence, not a failure: without a
    capture record there is nothing to deliver, so it must not flag."""
    mod, *_ = m
    ev = _event(1, capture=False, restore="none")
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[ev]))
    assert res["recovery_status"] == "no_concerning_signal_observed"


def test_recovery_status_uses_latest_window(m):
    """An old needs_attention boundary superseded by a clean latest window
    resolves: the panel describes the CURRENT recovery state, not a
    lifetime penalty."""
    mod, *_ = m
    bad = _event(1, post=_metrics(20, rae=3), restore="transcript")
    good = _event(2, restore="transcript")
    res = mod.compute_quality_score(
        _qdata(compactions=2, compaction_events=[bad, good]))
    assert res["recovery_status"] == "no_concerning_signal_observed"
    assert res["recovery_evidence"][0]["status"] == "needs_attention"
    assert res["recovery_evidence"][1]["status"] == "no_concerning_signal_observed"


def test_recovery_coverage_marks_uncolated_signals(m):
    mod, *_ = m
    res = mod.compute_quality_score(_qdata(compactions=1, compaction_events=[_event(1)]))
    cov = res["recovery_coverage"]
    assert cov["errors"] is True
    assert cov["corrections"] is False  # review-only, never scored


# ---------------------------------------------------------------------------
# Parser: compaction_events extraction
# ---------------------------------------------------------------------------

def _row(typ, content=None, **kw):
    r = {"type": typ, "timestamp": "2026-11-05T00:00:00Z",
         "uuid": kw.pop("uuid", f"u{len(kw)}-{typ}")}
    if content is not None:
        role = "user" if typ == "user" else "assistant"
        r["message"] = {"role": role, "content": content}
    r.update(kw)
    return r


def _call(rows, tid, name="Bash", inp=None, err=False, ts="2026-11-05T00:00:00Z"):
    rows.append(_row("assistant", [{"type": "tool_use", "id": tid, "name": name,
                                    "input": inp if inp is not None else {"command": f"cmd-{tid}"}}],
                     timestamp=ts, uuid=f"a-{tid}"))
    rows.append(_row("user", [{"type": "tool_result", "tool_use_id": tid,
                               "content": "Exit code: 1" if err else "ok",
                               "is_error": err}],
                     timestamp=ts, uuid=f"r-{tid}"))


def _boundary(rows, n, **md):
    meta = {"preTokens": 300_000, "postTokens": 30_000, "trigger": "auto"}
    meta.update(md)
    rows.append({"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2026-11-05T00:00:01Z", "uuid": f"b-{n}",
                 "version": "2.0.0", "compactMetadata": meta})


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def test_parser_emits_compaction_events(m, tmp_path):
    mod, *_ = m
    rows = [_row("user", "start the task"), _row("assistant", "ok")]
    # 12 clean calls pre-boundary
    for i in range(12):
        _call(rows, f"pre{i}")
    _boundary(rows, 1)
    # restore delivery marker right after the boundary
    rows.append({"type": "attachment", "uuid": "restore-1",
                 "timestamp": "2026-11-05T00:00:02Z",
                 "attachment": {"type": "hook_additional_context",
                                "content": ["[Token Optimizer] Post-compact recovery (auto checkpoint): state"]}})
    # 12 post calls: 2 errors, 1 reread of /fixture/a, 1 repeat-after-error
    _call(rows, "p0", name="Read", inp={"file_path": "/fixture/a"})
    _call(rows, "p1", err=True)
    for i in range(2, 10):
        _call(rows, f"p{i}")
    _call(rows, "p10", name="Read", inp={"file_path": "/fixture/a"})  # reread
    _call(rows, "p11", inp={"command": "cmd-p1"}, err=True)  # repeat of failed sig
    _row("user", "still going")
    fp = _write_jsonl(tmp_path / "sess.jsonl", rows)

    qd = mod._parse_jsonl_for_quality(fp)
    assert qd is not None
    events = qd["compaction_events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["ordinal"] == 1
    assert ev["matched_calls"] == 12
    assert ev["eligible"] is True
    assert ev["pre_tokens"] == 300_000 and ev["post_tokens"] == 30_000
    assert ev["host_version"] == "2.0.0"
    assert ev["restore_transcript"] is True
    assert ev["pre"]["calls"] == 12 and ev["pre"]["errors"] == 0
    assert ev["post"]["calls"] == 12
    assert ev["post"]["errors"] == 2
    assert ev["post"]["rereads"] == 1
    assert ev["post"]["repeat_after_error"] >= 1


def test_parser_no_compactions_empty_events(m, tmp_path):
    mod, *_ = m
    rows = [_row("user", "hi"), _row("assistant", "hello")]
    fp = _write_jsonl(tmp_path / "plain.jsonl", rows)
    qd = mod._parse_jsonl_for_quality(fp)
    assert qd["compactions"] == 0
    assert qd["compaction_events"] == []


def test_parser_crossing_call_excluded(m, tmp_path):
    """A tool call issued before the boundary whose result lands after it
    must not count on either side of the matched window."""
    mod, *_ = m
    rows = [_row("user", "start")]
    for i in range(12):
        _call(rows, f"pre{i}")
    # call issued pre-boundary, result arrives post-boundary
    rows.append(_row("assistant", [{"type": "tool_use", "id": "cross",
                                    "name": "Bash", "input": {"command": "x"}}],
                     timestamp="2026-11-05T00:00:00Z", uuid="a-cross"))
    _boundary(rows, 1)
    rows.append(_row("user", [{"type": "tool_result", "tool_use_id": "cross",
                               "content": "ok", "is_error": False}],
                     timestamp="2026-11-05T00:00:02Z", uuid="r-cross"))
    for i in range(12):
        _call(rows, f"post{i}")
    fp = _write_jsonl(tmp_path / "cross.jsonl", rows)
    qd = mod._parse_jsonl_for_quality(fp)
    ev = qd["compaction_events"][0]
    assert ev["pre"]["calls"] == 12
    assert ev["post"]["calls"] == 12  # crossing call excluded


def test_parser_second_boundary_uses_segment_windows(m, tmp_path):
    mod, *_ = m
    rows = [_row("user", "start")]
    for i in range(15):
        _call(rows, f"a{i}")
    _boundary(rows, 1)
    for i in range(15):
        _call(rows, f"b{i}")
    _boundary(rows, 2)
    for i in range(15):
        _call(rows, f"c{i}")
    _row("user", "done")
    fp = _write_jsonl(tmp_path / "two.jsonl", rows)
    qd = mod._parse_jsonl_for_quality(fp)
    events = qd["compaction_events"]
    assert len(events) == 2
    assert events[0]["matched_calls"] == 15
    assert events[1]["matched_calls"] == 15
    # boundary 2's pre window is segment 1 (the b* calls), not segment 0
    assert events[1]["pre"]["calls"] == 15


# ---------------------------------------------------------------------------
# Absolute-token gate + advisory
# ---------------------------------------------------------------------------

def _advisory_qdata(tokens=360_000, window=1_000_000, **kw):
    d = _qdata(context_tokens=tokens, model_context_window=window,
               tool_calls=20)
    # safe pause: last message is a user turn
    d["messages"] = [(0, "user", 400, True), (1, "assistant", 800, True),
                     (2, "user", 200, True), (3, "assistant", 900, True),
                     (4, "user", 150, True)]
    d.update(kw)
    return d


def _write_checkpoint(cpdir, sid, name="auto"):
    fn = f"{sid}-20261105-120000-{name}.md"
    p = cpdir / fn
    p.write_text("# checkpoint\n")
    return p


def test_advisory_threshold_clamped(m, monkeypatch):
    mod, *_ = m
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_ADVISORY_TOKENS", "500000")
    importlib.reload(mod)
    assert mod._context_advisory_threshold() == 400_000
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_ADVISORY_TOKENS", "100000")
    assert mod._context_advisory_threshold() == 300_000
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_ADVISORY_TOKENS", "350000")
    assert mod._context_advisory_threshold() == 350_000
    monkeypatch.delenv("TOKEN_OPTIMIZER_CONTEXT_ADVISORY_TOKENS")
    assert mod._context_advisory_threshold() == 350_000


def test_absolute_tokens_missing_telemetry_is_none(m):
    mod, *_ = m
    qd = _qdata(context_tokens=None, model_context_window=1_000_000)
    assert mod._absolute_context_tokens(qd, "sess-1") is None


def test_absolute_tokens_contradictory_window_is_none(m):
    mod, *_ = m
    # tokens exceed the session's own window -> contradictory, never nudge
    qd = _qdata(context_tokens=1_500_000, model_context_window=1_000_000)
    assert mod._absolute_context_tokens(qd, "sess-1") is None


def test_absolute_tokens_valid(m):
    mod, *_ = m
    tel = mod._absolute_context_tokens(_advisory_qdata(), "sess-1")
    assert tel is not None
    assert tel["tokens"] == 360_000
    assert tel["window"] == 1_000_000


def test_advisory_fires_at_threshold_with_checkpoint(m):
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-1"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata()
    result = {"score": 80, "fill_pct": 36.0}
    msg = mod._maybe_context_size_advisory(result, cache_path, qd)
    assert msg is not None
    assert "360K tokens in use" in msg
    assert "checkpoint" in msg.lower()
    # once per session: second call must not repeat
    msg2 = mod._maybe_context_size_advisory(result, cache_path, qd)
    assert msg2 is None


def test_advisory_below_threshold_silent(m):
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-2"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata(tokens=340_000)
    assert mod._maybe_context_size_advisory({"score": 80}, cache_path, qd) is None


def test_advisory_never_below_300k(m):
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-3"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    # even with threshold env forced to its floor, 299K must not fire
    qd = _advisory_qdata(tokens=299_000)
    assert mod._maybe_context_size_advisory({"score": 80}, cache_path, qd) is None


def test_advisory_no_checkpoint_no_fire(m):
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-4"
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata()
    assert mod._maybe_context_size_advisory({"score": 80}, cache_path, qd) is None


def test_advisory_mid_turn_no_fire(m):
    """Last message is an assistant turn -> active work, not a safe pause."""
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-5"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata()
    qd["messages"] = [(0, "user", 400, True), (1, "assistant", 800, True),
                      (2, "assistant", 900, True)]
    assert mod._maybe_context_size_advisory({"score": 80}, cache_path, qd) is None


def test_advisory_first_prompt_no_fire(m):
    """A large first prompt with no progress beyond it is a normal workload."""
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-6"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata(tool_calls=0)
    qd["messages"] = [(0, "user", 90000, True)]
    assert mod._maybe_context_size_advisory({"score": 80}, cache_path, qd) is None


def test_advisory_missing_telemetry_no_fire(m):
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-7"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata(context_tokens=None)
    assert mod._maybe_context_size_advisory({"score": 80}, cache_path, qd) is None


def test_advisory_suppressed_by_remedy_keeps_its_slot(m):
    """A stronger remedy firing the same turn suppresses the advisory WITHOUT
    consuming the once-per-session slot: the user never saw it, so a later
    quiet turn may still deliver it."""
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-8"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata()
    result = {"score": 80}
    assert mod._maybe_context_size_advisory(
        result, cache_path, qd, suppressed=True) is None
    assert result.get("_context_advisory_fired") is not True
    # Next turn, no remedy: the advisory may still fire.
    msg = mod._maybe_context_size_advisory(result, cache_path, qd)
    assert msg is not None
    assert "tokens in use" in msg


def test_advisory_shadow_logs_all_three_thresholds(m):
    """Shadow evaluation runs for 300K/350K/400K regardless of the configured
    threshold, so the weekly eval can compare candidates."""
    mod, qc, tmp_path, cpdir = m
    sid = "sess-adv-9"
    _write_checkpoint(cpdir, sid)
    cache_path = qc / f"quality-cache-{sid}.json"
    qd = _advisory_qdata(tokens=360_000)
    events = []
    orig = mod._log_compression_event
    mod._log_compression_event = lambda **kw: events.append(kw)
    try:
        mod._maybe_context_size_advisory({"score": 80}, cache_path, qd)
    finally:
        mod._log_compression_event = orig
    shadow = [e for e in events if e.get("feature") == "context_advisory_shadow"]
    assert shadow, "expected a context_advisory_shadow event"
    detail = json.loads(shadow[-1]["detail"])
    assert set(detail["thresholds"]) == {"300000", "350000", "400000"}
    # 360K clears 300K and 350K, not 400K
    assert detail["thresholds"]["300000"]["eligible"] is True
    assert detail["thresholds"]["350000"]["eligible"] is True
    assert detail["thresholds"]["400000"]["below"] is True


# ---------------------------------------------------------------------------
# Shared absolute-token gate on existing nudges
# ---------------------------------------------------------------------------

def _nudge_result(score=55, prev=80, **kw):
    r = {"score": score, "fill_pct": 40.0,
         "_nudge_previous_score": prev, "_nudge_count": 0,
         "_nudge_last_epoch": 0}
    r.update(kw)
    return r


def test_maybe_nudge_gated_below_300k(m):
    mod, qc, tmp_path, _cp = m
    cache_path = qc / "quality-cache-sess-n1.json"
    qd = _qdata(context_tokens=200_000, model_context_window=1_000_000)
    res = _nudge_result()
    assert mod._maybe_nudge(res, cache_path, qd) is None
    assert res["_nudge_count"] == 0  # not consumed


def test_maybe_nudge_fires_at_300k_plus(m):
    mod, qc, tmp_path, _cp = m
    cache_path = qc / "quality-cache-sess-n2.json"
    qd = _qdata(context_tokens=320_000, model_context_window=1_000_000)
    res = _nudge_result()
    msg = mod._maybe_nudge(res, cache_path, qd)
    assert msg is not None
    assert "/compact" in msg


def test_maybe_nudge_missing_telemetry_gated(m):
    mod, qc, tmp_path, _cp = m
    cache_path = qc / "quality-cache-sess-n3.json"
    qd = _qdata(context_tokens=None, model_context_window=1_000_000)
    res = _nudge_result()
    assert mod._maybe_nudge(res, cache_path, qd) is None


def test_fresh_nudge_gated_below_300k(m):
    mod, qc, tmp_path, _cp = m
    cache_path = qc / "quality-cache-sess-f1.json"
    qd = _qdata(context_tokens=200_000, model_context_window=1_000_000)
    res = _nudge_result(score=50, prev=80, fill_pct=60.0)
    assert mod._maybe_fresh_session_nudge(res, cache_path, qd) is None
    assert res.get("_fresh_nudge_fired") is not True


# ---------------------------------------------------------------------------
# Score-version boundary: no dampening / nudge carry across versions
# ---------------------------------------------------------------------------

def test_versions_not_comparable_across_score_version(m):
    mod, *_ = m
    prev_v1 = {"score_version": 1, "resource_health": 40.0}
    cur_v2 = {"score_version": 2, "resource_health": 90.0}
    assert mod._quality_scores_comparable(prev_v1, cur_v2) is False
    prev_v2 = {"score_version": 2, "resource_health": 40.0}
    assert mod._quality_scores_comparable(prev_v2, cur_v2) is True
    # legacy unversioned cache -> not comparable to v2
    assert mod._quality_scores_comparable({}, cur_v2) is False
