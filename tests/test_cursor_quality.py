#!/usr/bin/env python3
"""Unit tests for the Cursor multi-signal quality scorer (cursor_quality.py).

Slice D / U6. The load-bearing contracts pinned here:

- preCompact ``context_tokens``/``context_window_size`` are the host's
  occupancy AT COMPACTION TIME - historical evidence, never current fill
  (the C-surface bug class: a stale 95% peak presented as "ctx 95% now").
- No lifetime token sum is ever a fill numerator, and historical preCompact
  occupancy is never graded as current fill: context_fill is omitted (fill
  reported as metadata) and the shared v3 score is unmeasured.
- Cursor disk transcripts carry NO tool results (bundle-proven writer,
  2026.11.05): no signal may read their absence as healthy execution, and
  ``bloated_results`` stays omitted with that named evidence.
- Terminal ``turn_ended`` status/error remains a diagnostic, not a v3 score input.
- hook_measured_tokens are quarantined (unproven per-response vs cumulative
  semantics) and must never enter tokens, fill, or the score.
- Cursor uses the shared v3 scorer; the earlier signal values are diagnostics.

Transcript fixtures are SYNTHETIC, built from the bundle-proven writer schema
(``synthetic-bundle-schema``) - not captures from a live Cursor install.

Run: python3 -m pytest tests/test_cursor_quality.py -v
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_quality  # noqa: E402
import hermes_session  # noqa: E402


def _tally(**overrides):
    base = {
        "conversation_id": "conv-quality-1",
        "first_ts": 1700000000.0,
        "updated_at": 1700003600.0,
        "final": True,
        "end_reason": "sessionEnd",
        "turns": 30,
        "tool_calls": 20,
        "tool_names": {"Shell": 12, "Read": 8},
        "models": {"claude-sonnet": 30},
        "compactions": [],
        "cwd": "/repo/demo",
        "cursor_version": "2026.11.05-86047df",
    }
    base.update(overrides)
    return base


def _write_transcript(home: Path, conversation_id: str, lines: list[dict]) -> Path:
    """Synthetic bundle-schema transcript under <home>/projects/... (0o600 dir tree)."""
    tdir = home / "projects" / "demo-slug" / "agent-transcripts" / conversation_id
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / f"{conversation_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _transcript_lines(*, terminal_status="success", terminal_error=None, messages=8):
    """Synthetic writer-schema lines: role/message.content + a terminal turn_ended."""
    lines = []
    for i in range(messages):
        if i % 2 == 0:
            lines.append({
                "role": "user",
                "message": {"content": [{"type": "text", "text": "please fix the flaky test in the parser module today"}]},
            })
        else:
            lines.append({
                "role": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "I will look at the parser and run the focused test suite first."},
                    {"type": "tool_use", "name": "Shell", "input": {"command": "pytest tests/test_parser.py -q"}},
                ]},
            })
    ended = {"type": "turn_ended", "status": terminal_status}
    if terminal_error:
        ended["error"] = terminal_error
    lines.append(ended)
    return lines


@pytest.fixture()
def cursor_home(tmp_path, monkeypatch):
    home = tmp_path / ".cursor"
    home.mkdir()
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Composite shape and provenance
# ---------------------------------------------------------------------------


def test_full_data_session_all_signals_active(cursor_home):
    cid = "conv-quality-1"
    tpath = _write_transcript(cursor_home, cid, _transcript_lines())
    tally = _tally(
        bubble_tokens={"input_tokens": 4000, "output_tokens": 900, "model": "claude-sonnet"},
        compactions=[{
            "trigger": "auto",
            "context_tokens": 150000,
            "context_window_size": 200000,
            "context_usage_percent": 75.0,
            "ts": 1700003000.0,
        }],
        failures={"Shell": 1},
        transcript_path=str(tpath),
    )
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert q is not None
    active = set(q["signals_active"])
    assert {
        "message_count",
        "output_input_ratio",
        "compaction_depth",
        "agent_efficiency",
        "decision_density",
    } <= active
    # context_fill is NEVER graded from historical preCompact occupancy: it
    # stays omitted (metadata only) until a current occupancy source exists.
    omitted = set(q["signals_omitted"])
    assert {"stale_reads", "bloated_results", "duplicates", "context_fill"} <= omitted
    for name in ("stale_reads", "bloated_results", "duplicates", "context_fill"):
        assert q["omitted_reasons"][name]
    # The historical fill still reaches the compaction_depth signal, which
    # describes the compaction event itself.
    assert q["signal_scores"]["compaction_depth"] is not None
    assert abs(sum(q["signal_weights"].values()) - 1.0) < 1e-3  # weights rounded to 4dp
    assert 0 <= q["score"] <= 100
    # V3 does not grade historical-only occupancy as a current measurement.
    assert q["grade"] == "?"
    assert q["measured"] is False
    assert q["quality_pending"] is False
    # Diagnostic signals remain available, but the score uses v3 weights.
    assert set(q["signal_weights"]) == {"context_fill", "absolute_waste_tokens"}
    assert abs(sum(q["signal_weights"].values()) - 1.0) < 1e-3  # weights rounded to 4dp
    # Per-signal provenance is present and names real Cursor sources.
    for name in active:
        assert q["signal_provenance"][name]


def test_historical_precompact_fill_provenance(cursor_home):
    tally = _tally(compactions=[{
        "trigger": "manual",
        "context_tokens": 190000,
        "context_window_size": 200000,
        "context_usage_percent": 95.0,
        "ts": 1700003000.0,
    }])
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    # A 95% pre-compaction peak must not poison the CURRENT composite: fill
    # is reported as historical metadata and context_fill is omitted, with
    # the remaining signals renormalized.
    assert "context_fill" not in q["signals_active"]
    assert "context_fill" in q["signals_omitted"]
    assert "never graded" in q["omitted_reasons"]["context_fill"]
    assert q["signal_scores"]["context_fill"] is None
    assert q["context_source"] == "cursor_precompact_historical"
    # Historical, never current: the measured timestamp is the compaction's.
    assert q["fill_is_current"] is False
    assert q["fill_measured_at"] == 1700003000.0
    assert q["fill_ratio"] == pytest.approx(0.95, abs=1e-3)
    assert any("historical" in c.lower() or "compaction" in c.lower() for c in q["caveats"])
    # The at-compaction fill still feeds compaction_depth (it describes the
    # compaction event, which is historical by nature).
    assert "compaction_depth" in q["signals_active"]
    # 1 compaction at a 95% peak: 100 - 0 (first compaction is fine) - 20 = 80.
    assert q["signal_scores"]["compaction_depth"] == 80
    # Weights renormalize over the remaining signals - no neutral fill.
    assert abs(sum(q["signal_weights"].values()) - 1.0) < 1e-3  # weights rounded to 4dp


def test_precompact_peak_never_moves_current_composite(cursor_home):
    # Controlled invariance: two IDENTICAL sessions differing only in the
    # preCompact occupancy (95% peak vs 40%). context_fill is omitted in
    # both, so the ONLY score difference allowed is compaction_depth's
    # event-heat term (a historical event metric - the 95% compaction was
    # genuinely hotter). Everything about the CURRENT composite is invariant.
    def _q(pct):
        return cursor_quality.compute_cursor_quality(
            _tally(
                turns=16,
                tool_calls=8,
                bubble_tokens={"input_tokens": 4000, "output_tokens": 900, "model": "claude-sonnet"},
                compactions=[{
                    "trigger": "auto",
                    "context_tokens": int(200000 * pct),
                    "context_window_size": 200000,
                    "context_usage_percent": pct * 100,
                    "ts": 1700003000.0,
                }],
            ),
            cursor_home=cursor_home,
        )

    hot, cool = _q(0.95), _q(0.40)
    for q in (hot, cool):
        assert "context_fill" not in q["signals_active"]
        assert q["signal_scores"]["context_fill"] is None
    # Identical weights and identical scores for every non-compaction signal.
    assert hot["signal_weights"] == cool["signal_weights"]
    for name, s in hot["signal_scores"].items():
        if name != "compaction_depth":
            assert s == cool["signal_scores"][name], name
    # The only movement is the event-heat term: 80 vs 100 on compaction_depth.
    assert hot["signal_scores"]["compaction_depth"] == 80
    assert cool["signal_scores"]["compaction_depth"] == 100
    # Historical peak is diagnostic only in the common v3 score.
    assert cool["score"] == hot["score"]
    assert not hot["measured"] and not cool["measured"]


def test_context_window_reported_only_for_single_known_model(cursor_home):
    # Single positive model fact: the model's window is reported.
    q = cursor_quality.compute_cursor_quality(
        _tally(bubble_tokens={"input_tokens": 4000, "output_tokens": 900, "model": "claude-sonnet"}),
        cursor_home=cursor_home,
    )
    assert q["context_window_used"] is not None
    # Multi-model tally: dominant-by-count would mislabel recency - unknown.
    q_multi = cursor_quality.compute_cursor_quality(
        _tally(models={"claude-sonnet": 9, "gpt-5": 3}),
        cursor_home=cursor_home,
    )
    assert q_multi["context_window_used"] is None
    # No model evidence at all: unknown, never a default presented as fact.
    q_none = cursor_quality.compute_cursor_quality(_tally(models={}), cursor_home=cursor_home)
    assert q_none["context_window_used"] is None


def test_no_snapshot_means_fill_omitted_never_lifetime_sum(cursor_home):
    # Huge lifetime vscdb tokens, but no preCompact snapshot: fill must be
    # omitted - a lifetime sum is never an occupancy measurement.
    tally = _tally(
        bubble_tokens={"input_tokens": 150000, "output_tokens": 2000, "model": "claude-sonnet"},
    )
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert "context_fill" not in q["signals_active"]
    assert "context_fill" in q["signals_omitted"]
    assert q["fill_ratio"] is None
    # The 150K lifetime figure must not drag the score via a fake fill.
    sparse = _tally(bubble_tokens={"input_tokens": 100, "output_tokens": 50, "model": "claude-sonnet"})
    q_sparse = cursor_quality.compute_cursor_quality(sparse, cursor_home=cursor_home)
    assert "context_fill" not in q_sparse["signals_active"]
    # Only the real differences (message/oi signals) may separate the scores.
    assert q["signal_scores"]["context_fill"] is None


def test_base_only_signals_match_hermes_scorer(cursor_home):
    # vscdb tokens, no compaction snapshot, no transcript: the active set is
    # exactly the Hermes base signals minus fill, and the score must equal
    # the shared scorer byte-for-byte (same composite, same grade scale).
    tally = _tally(
        turns=45,
        tool_calls=0,  # no tool evidence: agent_efficiency stays omitted
        bubble_tokens={"input_tokens": 4000, "output_tokens": 100, "model": "claude-sonnet"},
    )
    # Keep measure.py resident in sys.modules for the duration of the call:
    # its grade helpers resolve the facade at call time (conftest swaps it
    # out between tests), and the shared scorer calls them unguarded.
    import measure  # noqa: F401

    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert set(q["signals_active"]) == {"message_count", "output_input_ratio"}
    reference = hermes_session.compute_quality_score(
        input_tokens=4000,
        output_tokens=100,
        message_count=45,
        model="claude-sonnet",
        context_window=200000,
        cache_read=0,
        context_tokens=None,  # measured-but-unavailable: fill omitted
    )
    assert q["score"] == reference["score"]
    assert q["grade"] == reference["grade"]
    assert q["band"] == reference["band"]


def test_sparse_session_renormalizes_without_neutral_fill(cursor_home):
    # Almost no data: only turns and recorded tool calls exist. Unavailable
    # signals are omitted, never filled with a neutral-healthy default.
    tally = _tally(turns=5, tool_calls=2)
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    active = set(q["signals_active"])
    assert "message_count" in active
    assert "context_fill" not in active
    assert "output_input_ratio" not in active  # no verified output tokens
    assert "decision_density" not in active    # no transcript
    assert "compaction_depth" not in active    # no preCompact events observed
    assert abs(sum(q["signal_weights"].values()) - 1.0) < 1e-3  # weights rounded to 4dp


def test_hook_measured_tokens_stay_quarantined(cursor_home):
    tally = _tally(
        hook_measured_tokens={
            "input": 50000, "output": 9000, "cache_read": 100000,
            "cache_write": 0, "responses": 12, "source": "cursor_hook_measured",
        },
    )
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert "context_fill" not in q["signals_active"]
    assert "output_input_ratio" not in q["signals_active"]
    assert q["token_source"] == "cursor_tally_only"
    assert q["fill_ratio"] is None


def test_output_input_ratio_only_with_verified_tokens(cursor_home):
    # Transcript estimates carry input only: the oi signal would read a fake
    # 0 ratio (score 15), so it stays omitted instead of punishing on air.
    tally = _tally(transcript_tokens=5000)
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert q["token_source"] == "cursor_transcript_estimate"
    assert "output_input_ratio" not in q["signals_active"]


# ---------------------------------------------------------------------------
# Failure and terminal evidence
# ---------------------------------------------------------------------------


def test_turn_ended_error_reaches_score(cursor_home):
    cid = "conv-quality-1"
    ok_path = _write_transcript(cursor_home, cid, _transcript_lines(terminal_status="success"))
    tally_ok = _tally(transcript_path=str(ok_path))
    q_ok = cursor_quality.compute_cursor_quality(tally_ok, cursor_home=cursor_home)

    err_path = _write_transcript(
        cursor_home, cid, _transcript_lines(terminal_status="error", terminal_error="model overloaded")
    )
    tally_err = _tally(transcript_path=str(err_path))
    q_err = cursor_quality.compute_cursor_quality(tally_err, cursor_home=cursor_home)

    assert q_err["evidence"]["terminal_errors"] == 1
    assert q_ok["evidence"]["terminal_errors"] == 0
    assert q_err["signal_scores"]["agent_efficiency"] < q_ok["signal_scores"]["agent_efficiency"]


def test_recorded_tool_failures_lower_agent_efficiency(cursor_home):
    clean = _tally(tool_calls=40)
    failing = _tally(tool_calls=40, failures={"Shell": 8})  # 20% failure rate
    q_clean = cursor_quality.compute_cursor_quality(clean, cursor_home=cursor_home)
    q_fail = cursor_quality.compute_cursor_quality(failing, cursor_home=cursor_home)
    assert q_fail["signal_scores"]["agent_efficiency"] < q_clean["signal_scores"]["agent_efficiency"]
    assert q_fail["evidence"]["tool_failures"] == 8


def test_no_tool_results_is_never_healthy_evidence(cursor_home):
    cid = "conv-quality-1"
    tpath = _write_transcript(cursor_home, cid, _transcript_lines())
    tally = _tally(transcript_path=str(tpath))
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert "bloated_results" in q["signals_omitted"]
    assert "tool output" in q["omitted_reasons"]["bloated_results"].lower()
    # The no-tool-results caveat must be visible to every consumer.
    assert any("tool result" in c.lower() or "tool output" in c.lower() for c in q["caveats"])


def test_compaction_depth_rewards_shallow_single_compaction(cursor_home):
    shallow = _tally(compactions=[{
        "trigger": "auto", "context_tokens": 80000, "context_window_size": 200000,
        "context_usage_percent": 40.0, "ts": 1700003000.0,
    }])
    q = cursor_quality.compute_cursor_quality(shallow, cursor_home=cursor_home)
    assert q["signal_scores"]["compaction_depth"] == 100


def test_compaction_depth_penalizes_repeated_hot_compactions(cursor_home):
    comps = [{
        "trigger": "auto", "context_tokens": 190000, "context_window_size": 200000,
        "context_usage_percent": 95.0, "ts": 1700000000.0 + i * 600,
    } for i in range(4)]
    q = cursor_quality.compute_cursor_quality(_tally(compactions=comps), cursor_home=cursor_home)
    assert q["signal_scores"]["compaction_depth"] <= 50
    assert q["evidence"]["compactions"] == 4


# ---------------------------------------------------------------------------
# Transcript handling
# ---------------------------------------------------------------------------


def test_decision_density_from_transcript(cursor_home):
    cid = "conv-quality-1"
    tpath = _write_transcript(cursor_home, cid, _transcript_lines(messages=10))
    tally = _tally(transcript_path=str(tpath))
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert "decision_density" in q["signals_active"]
    assert q["signal_provenance"]["decision_density"].startswith("cursor_transcript")


def test_insufficient_transcript_omits_density(cursor_home):
    cid = "conv-quality-1"
    tpath = _write_transcript(cursor_home, cid, _transcript_lines(messages=2))
    tally = _tally(transcript_path=str(tpath))
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert "decision_density" not in q["signals_active"]
    assert "decision_density" in q["signals_omitted"]


def test_transcript_outside_home_is_refused(cursor_home, tmp_path):
    outside = tmp_path / "elsewhere" / "conv.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text("\n".join(json.dumps(l) for l in _transcript_lines()), encoding="utf-8")
    tally = _tally(transcript_path=str(outside))
    q = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    assert "decision_density" not in q["signals_active"]


def test_malformed_transcript_lines_never_fatal(cursor_home):
    cid = "conv-quality-1"
    tdir = cursor_home / "projects" / "demo-slug" / "agent-transcripts" / cid
    tdir.mkdir(parents=True)
    path = tdir / f"{cid}.jsonl"
    good = _transcript_lines()
    raw = "\n".join([json.dumps(good[0]), "{not json", json.dumps(good[1]), "[]", json.dumps(good[2])])
    path.write_text(raw + "\n", encoding="utf-8")
    q = cursor_quality.compute_cursor_quality(_tally(transcript_path=str(path)), cursor_home=cursor_home)
    assert q is not None  # never raises


def test_invalid_tally_returns_none(cursor_home):
    assert cursor_quality.compute_cursor_quality({}, cursor_home=cursor_home) is None
    assert cursor_quality.compute_cursor_quality(None, cursor_home=cursor_home) is None
    assert cursor_quality.compute_cursor_quality("nope", cursor_home=cursor_home) is None


def test_default_home_resolution(cursor_home):
    # TOKEN_OPTIMIZER_CURSOR_HOME is honored when no explicit home is passed.
    tally = _tally(compactions=[{
        "trigger": "auto", "context_tokens": 100000, "context_window_size": 200000,
        "context_usage_percent": 50.0, "ts": 1700003000.0,
    }])
    q = cursor_quality.compute_cursor_quality(tally)
    assert q is not None
    assert q["fill_ratio"] == pytest.approx(0.5, abs=1e-3)
    assert q["fill_is_current"] is False


def test_quality_for_normalizer_matches_direct_call(cursor_home):
    # The cursor_session integration seam: same tally in, same quality out.
    tally = _tally(
        bubble_tokens={"input_tokens": 4000, "output_tokens": 900, "model": "claude-sonnet"},
        compactions=[{
            "trigger": "auto", "context_tokens": 150000, "context_window_size": 200000,
            "context_usage_percent": 75.0, "ts": 1700003000.0,
        }],
    )
    direct = cursor_quality.compute_cursor_quality(tally, cursor_home=cursor_home)
    via_helper = cursor_quality.quality_for_session(tally, cursor_home=cursor_home)
    assert direct == via_helper
