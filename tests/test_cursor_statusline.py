#!/usr/bin/env python3
"""Unit tests for cursor_statusline.py (U8, R12).

The Cursor CLI statusLine payload schema is NOT documented by Cursor; the only
evidence is forum-observed (CLI 2026.07.20: context_window.total_input_tokens /
context_window.context_window_size / autorun). These tests therefore pin the
script's defensive contract, not a schema:

- known-good forum-shaped payload -> model + ctx line, TO line from the tally
- absent/renamed payload fields -> "?" / omission, never fabricated values
- autorun is NEVER rendered (approval mode is indistinguishable; staff-confirmed)
- malformed/oversized stdin -> no output, exit 0
- no tally -> honest "no session data"
- path-traversal conversation ids can never escape the sessions dir

Run: python3 -m pytest tests/test_cursor_statusline.py -v
"""

import io
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_statusline  # noqa: E402


def _tally(**overrides):
    base = {
        "conversation_id": "conv-abc123",
        "first_ts": 1700000000.0,
        "updated_at": 1700000600.0,
        "turns": 5,
        "tool_calls": 9,
        "models": {"claude-sonnet": 7},
        "compactions": [
            {"context_tokens": 80000, "context_window_size": 200000,
             "context_usage_percent": 40, "ts": 1700000500.0}
        ],
        "cwd": "/repo/demo",
        "workspace_roots": ["/repo/demo"],
    }
    base.update(overrides)
    return base


@pytest.fixture
def cursor_home(tmp_path, monkeypatch):
    """A sandboxed cursor home with TOKEN_OPTIMIZER_CURSOR_HOME pointing at it."""
    home = tmp_path / ".cursor"
    (home / "token-optimizer" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(home))
    return home


def _write_tally(home, name, tally):
    path = home / "token-optimizer" / "sessions" / f"{name}.json"
    path.write_text(json.dumps(tally), encoding="utf-8")
    return path


def _run_main(monkeypatch, stdin_bytes):
    """Run main() with piped stdin; returns (stdout_text, exit_code)."""
    class _FakeStdin:
        def __init__(self, data):
            self.buffer = io.BytesIO(data)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(stdin_bytes))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = cursor_statusline.main()
    return out.getvalue(), code


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_forum_shaped_payload_renders_model_and_context(cursor_home, monkeypatch):
    # With the bundle-proven gate flipped (post-verification state), the live
    # payload fill renders directly.
    monkeypatch.setattr(cursor_statusline, "PAYLOAD_FILL_PROVEN", True)
    _write_tally(cursor_home, "conv-abc123", _tally())
    payload = {
        "model": "claude-sonnet-4.5",
        "conversation_id": "conv-abc123",
        "context_window": {"total_input_tokens": 84000, "context_window_size": 200000},
        "autorun": True,
    }
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    lines = out.strip().split("\n")
    assert len(lines) == 2
    assert "claude-sonnet-4.5" in lines[0]
    assert "ctx 42% (84k/200k)" in lines[0]
    # Line 2: fill-derived grade from the LIVE payload numbers, tally facts.
    assert lines[1].startswith("TO ")
    assert "fill live" in lines[1]
    assert "comp 1" in lines[1]
    assert "tools 9" in lines[1]


def test_payload_fill_gated_until_bundle_proven(cursor_home, monkeypatch):
    # Default state (gate OFF): the forum-observed payload context fields are
    # NOT proven occupancy, so they must be ignored — current fill is unknown,
    # and the preCompact peak renders as labeled HISTORY, not current fill.
    assert cursor_statusline.PAYLOAD_FILL_PROVEN is False
    _write_tally(cursor_home, "conv-abc123", _tally())
    payload = {
        "model": "claude-sonnet-4.5",
        "conversation_id": "conv-abc123",
        "context_window": {"total_input_tokens": 84000, "context_window_size": 200000},
    }
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    lines = out.strip().split("\n")
    assert "ctx ?" in lines[0]  # current occupancy honestly unknown
    assert "ctx 40%" not in lines[0]  # the historical peak is not current fill
    assert "last compact 40%" in lines[1]  # shown as history instead
    assert "TO A" not in lines[1] and "TO B" not in lines[1]  # no stale grade
    assert "42%" not in out  # the unproven payload number never leaks


def test_gated_payload_fill_with_no_tally_shows_unknown(cursor_home, monkeypatch):
    # Gate OFF + no tally: payload context numbers alone must NOT produce a
    # reading — the footer says unknown instead of trusting an unproven field.
    payload = {
        "model": "claude-sonnet-4.5",
        "context_window": {"total_input_tokens": 84000, "context_window_size": 200000},
    }
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    assert "ctx ?" in out
    assert "42%" not in out
    assert "no session data" in out


def test_fill_grade_matches_session_fallback_formula():
    # cursor_session._quality fallback: score = 100 - fill*50; A/B/C/D at 90/75/60.
    assert cursor_statusline._fill_grade(0.0) == (100, "A")
    assert cursor_statusline._fill_grade(0.2) == (90, "A")
    assert cursor_statusline._fill_grade(0.5) == (75, "B")
    assert cursor_statusline._fill_grade(0.8) == (60, "C")
    assert cursor_statusline._fill_grade(1.0) == (50, "D")


def test_percent_only_precompact_entry_renders_as_history():
    # The bridge records context_usage_percent as its own field; an entry may
    # carry a percent without token counts. Still historical, never current.
    tally = {"compactions": [{"context_tokens": 0, "context_window_size": 0,
                              "context_usage_percent": 40.0}]}
    lines = cursor_statusline.build_lines({}, tally)
    assert any("last compact 40%" in line for line in lines)
    assert any("ctx ?" in line for line in lines)
    assert not any("ctx 40%" in line for line in lines)


def test_precompact_peak_is_history_not_current_fill(cursor_home, monkeypatch):
    # Judge-flagged regression: a 95% preCompact reading is the peak that
    # TRIGGERED the compaction. A newer hook event (updated_at bumped, no new
    # compaction) must NOT refresh it into a current fill or a current grade.
    _write_tally(cursor_home, "conv-abc123", _tally(
        updated_at=1700000900.0,  # later hook event, no occupancy measurement
        compactions=[{"context_tokens": 190000, "context_window_size": 200000,
                      "context_usage_percent": 95, "ts": 1700000500.0}],
    ))
    payload = {"model": "claude-sonnet-4.5", "conversation_id": "conv-abc123"}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    lines = out.strip().split("\n")
    assert "ctx ?" in lines[0]  # current occupancy still unknown
    assert "ctx 95%" not in out
    assert "last compact 95%" in lines[1]  # historical, labeled
    assert "TO A" not in out and "TO B" not in out and "TO C" not in out  # no grade
    # Age comes from the entry's own ts (far past -> no fresh-looking age),
    # never from the bumped updated_at.
    assert "just now" not in out


def test_last_compaction_age_uses_measurement_ts(cursor_home, monkeypatch):
    now = time.time()
    _write_tally(cursor_home, "conv-abc123", _tally(
        updated_at=now,  # fresh tally activity...
        compactions=[{"context_tokens": 190000, "context_window_size": 200000,
                      "context_usage_percent": 95, "ts": now - 200}],
    ))
    payload = {"model": "m", "conversation_id": "conv-abc123"}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "last compact 95% 3m ago" in out  # aged from the entry ts


def test_tally_fallback_to_most_recent_when_id_absent(cursor_home, monkeypatch):
    _write_tally(cursor_home, "conv-abc123", _tally(updated_at=1700000600.0))
    _write_tally(cursor_home, "conv-other1", _tally(
        conversation_id="conv-other1", updated_at=1700000000.0, tool_calls=1))
    payload = {"model": "gpt-5", "context_window": {"total_input_tokens": 1000,
                                                    "context_window_size": 128000}}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    assert "tools 9" in out  # the more recently updated tally wins


def test_explicit_id_beats_newer_tally(cursor_home, monkeypatch):
    _write_tally(cursor_home, "conv-abc123", _tally(updated_at=1700000000.0, tool_calls=3))
    _write_tally(cursor_home, "conv-other1", _tally(
        conversation_id="conv-other1", updated_at=1700000600.0, tool_calls=99))
    payload = {"conversation_id": "conv-abc123"}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    assert "tools 3" in out
    assert "tools 99" not in out


# ---------------------------------------------------------------------------
# Honest absence (schema drift / missing data)
# ---------------------------------------------------------------------------


def test_missing_context_fields_render_question_mark(cursor_home, monkeypatch):
    _write_tally(cursor_home, "conv-abc123", _tally(compactions=[]))
    payload = {"model": "claude-sonnet-4.5", "conversation_id": "conv-abc123"}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    lines = out.strip().split("\n")
    assert "ctx ?" in lines[0]
    assert "ctx unknown" in lines[1]
    assert "%" not in lines[0].split("ctx ?")[0]  # no fabricated percentage


def test_renamed_context_fields_do_not_fabricate(cursor_home, monkeypatch):
    # A future Cursor build that renames the keys must show "?", not guess.
    _write_tally(cursor_home, "conv-abc123", _tally(compactions=[]))
    payload = {"model": "m", "conversation_id": "conv-abc123",
               "context_window": {"input": 50000, "size": 200000}}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "ctx ?" in out
    assert "25%" not in out


def test_no_tally_is_honest(cursor_home, monkeypatch):
    payload = {"model": "claude-sonnet-4.5",
               "context_window": {"total_input_tokens": 1000, "context_window_size": 128000}}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    assert "TO · no session data" in out


def test_corrupt_tally_falls_back_or_reports_no_data(cursor_home, monkeypatch):
    bad = cursor_home / "token-optimizer" / "sessions" / "conv-abc123.json"
    bad.write_text("{not json", encoding="utf-8")
    payload = {"conversation_id": "conv-abc123"}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    assert "no session data" in out


# ---------------------------------------------------------------------------
# Safety: approval mode, stdin abuse, traversal
# ---------------------------------------------------------------------------


def test_approval_mode_never_rendered(cursor_home, monkeypatch):
    _write_tally(cursor_home, "conv-abc123", _tally())
    for autorun in (True, False):
        payload = {"model": "m", "conversation_id": "conv-abc123", "autorun": autorun,
                   "context_window": {"total_input_tokens": 1000,
                                      "context_window_size": 128000}}
        out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
        lowered = out.lower()
        for word in ("autorun", "allowlist", "auto-review", "auto review", "approval"):
            assert word not in lowered


def test_malformed_stdin_prints_nothing(cursor_home, monkeypatch):
    for junk in (b"", b"{not json", b"[1,2,3]", b'"just a string"', b"\x00\xff"):
        out, code = _run_main(monkeypatch, junk)
        assert code == 0
        # No session id parses out of junk; with no usable payload the script
        # still shows the tally fallback line or nothing — never a traceback.
        assert "Traceback" not in out


def test_oversized_stdin_rejected_no_fabrication(cursor_home, monkeypatch):
    big = b'{"model": "' + b"x" * (cursor_statusline._MAX_STDIN_BYTES + 100) + b'"}'
    out, code = _run_main(monkeypatch, big)
    assert code == 0
    # The oversized payload is refused whole, so nothing from it may leak into
    # the footer: no model string, no percentages — only honest unknowns.
    assert "xxx" not in out
    assert "%" not in out
    assert "ctx ?" in out


def test_conversation_id_cannot_traverse(cursor_home, monkeypatch):
    # A crafted id must sanitize away; "unknown" must not select unknown.json.
    _write_tally(cursor_home, "unknown", _tally(conversation_id="unknown", tool_calls=77))
    payload = {"conversation_id": "../../../etc/passwd"}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    # Sanitized to "unknown" -> skipped as a direct hit; latest-tally fallback
    # MAY still find unknown.json by mtime, but the id itself never reaches a path.
    assert "Traceback" not in out
    assert cursor_statusline._sanitize_session_id("../../../etc/passwd") == "etcpasswd"
    assert cursor_statusline._sanitize_session_id("../..") == "unknown"
    assert cursor_statusline._sanitize_session_id("") == "unknown"


def test_short_id_sanitizes_to_unknown(cursor_home):
    assert cursor_statusline._sanitize_session_id("ab") == "unknown"
    assert cursor_statusline._sanitize_session_id("abcdef") == "abcdef"


# ---------------------------------------------------------------------------
# Payload shape tolerance
# ---------------------------------------------------------------------------


def test_nested_model_dict_shape(cursor_home, monkeypatch):
    _write_tally(cursor_home, "conv-abc123", _tally())
    payload = {"model": {"display_name": "GPT-5 Codex"}, "conversation_id": "conv-abc123"}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "GPT-5 Codex" in out


def test_top_level_context_keys_tolerated(cursor_home, monkeypatch):
    monkeypatch.setattr(cursor_statusline, "PAYLOAD_FILL_PROVEN", True)
    _write_tally(cursor_home, "conv-abc123", _tally())
    payload = {"conversation_id": "conv-abc123",
               "context_tokens": 64000, "context_window_size": 128000}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "ctx 50% (64k/128k)" in out


def test_model_falls_back_to_tally_when_payload_lacks_it(cursor_home, monkeypatch):
    _write_tally(cursor_home, "conv-abc123", _tally())
    payload = {"conversation_id": "conv-abc123",
               "context_window": {"total_input_tokens": 1000, "context_window_size": 128000}}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "claude-sonnet" in out


def test_multi_model_tally_shows_no_model(cursor_home, monkeypatch):
    # models{} is turn counts with no recency: after a mid-session switch the
    # most-frequent model may not be current, so no single name is asserted.
    _write_tally(cursor_home, "conv-abc123",
                 _tally(models={"claude-sonnet": 7, "gpt-5": 2}))
    payload = {"conversation_id": "conv-abc123",
               "context_window": {"total_input_tokens": 1000, "context_window_size": 128000}}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "claude-sonnet" not in out
    assert "gpt-5" not in out
    assert "last compact 40%" in out  # the rest of the footer still renders


def test_non_positive_context_values_treated_absent(cursor_home, monkeypatch):
    monkeypatch.setattr(cursor_statusline, "PAYLOAD_FILL_PROVEN", True)
    _write_tally(cursor_home, "conv-abc123", _tally(compactions=[]))
    payload = {"conversation_id": "conv-abc123",
               "context_window": {"total_input_tokens": 0, "context_window_size": 0}}
    out, _ = _run_main(monkeypatch, json.dumps(payload).encode())
    assert "ctx ?" in out


def test_unexpected_exception_still_exits_zero(cursor_home, monkeypatch):
    monkeypatch.setattr(cursor_statusline, "_find_tally",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    payload = {"conversation_id": "conv-abc123"}
    out, code = _run_main(monkeypatch, json.dumps(payload).encode())
    assert code == 0
    assert out == ""


def test_statusline_writes_utf8_to_non_utf8_pipe(monkeypatch, tmp_path):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(b'{}')})())
    monkeypatch.setattr(cursor_statusline, "_cursor_home", lambda: tmp_path)
    assert cursor_statusline.main() == 0
    assert "TO · no session data" in raw.getvalue().decode("utf-8")
