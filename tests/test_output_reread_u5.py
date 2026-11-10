"""Savings-truth U5: nudged replies, their rereads, and an un-nudged control.

Synthetic transcripts and databases only, all under pytest's tmp_path.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

T0 = datetime(2026, 1, 5, 12, 0, 0)
MODEL = "claude-opus-4-5"  # cache read $0.50/M, output $25/M on the anthropic tier


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


@pytest.fixture
def o(m):
    from _measure import output_reread
    return output_reread


def _ts(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _assistant(key, seconds, out, lines=1, sidechain=False):
    rows = []
    for _ in range(lines):
        rows.append({"type": "assistant", "requestId": key, "timestamp": _ts(seconds),
                     "isSidechain": sidechain,
                     "message": {"model": MODEL, "usage": {
                         "input_tokens": 10, "cache_read_input_tokens": 1000,
                         "output_tokens": out}}})
    return rows


def _prompt(seconds, text="next step"):
    return [{"type": "user", "timestamp": _ts(seconds), "message": {"content": text}}]


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _session(outputs, per_prompt=3, start=0, lines=1):
    """Requests in groups of ``per_prompt``, each group after a user prompt."""
    rows, t = [], start
    for i, out in enumerate(outputs):
        if i % per_prompt == 0:
            rows += _prompt(t)
            t += 1
        rows += _assistant(f"r{start}-{i}", t, out, lines=lines)
        t += 1
    return rows


def test_walk_cache_reuses_unchanged_and_invalidates_changed_transcript(o, tmp_path, monkeypatch):
    path = _write(tmp_path / "transcript.jsonl", _session([100, 200]))
    cache = tmp_path / "cache"
    expected = o.walk_requests(path)
    calls = []
    real_walk = o.walk_requests
    monkeypatch.setattr(o, "walk_requests", lambda p: calls.append(p) or real_walk(p))
    assert o.cached_walk(path, cache) == expected
    assert o.cached_walk(path, cache) == expected
    assert len(calls) == 1
    _write(path, _session([100, 200, 300]))
    assert o.cached_walk(path, cache) == real_walk(path)
    assert len(calls) == 2


def test_walk_folds_streamed_lines_and_finds_prompts_and_compactions(o, tmp_path):
    rows = _prompt(0) + _assistant("a", 1, 5) + _assistant("a", 1, 40) + _assistant("b", 2, 30)
    rows += [{"type": "system", "subtype": "compact_boundary", "timestamp": _ts(3)}]
    rows += _prompt(4) + _assistant("c", 5, 20) + _assistant("side", 6, 99, sidechain=True)
    rows += [{"type": "user", "timestamp": _ts(7), "message": {"content": [
        {"type": "tool_result", "content": "x"}]}}]
    walk = o.walk_requests(_write(tmp_path / "t.jsonl", rows))
    assert [(r[0], r[2]) for r in walk["outputs"]] == [("a", 40), ("b", 30), ("c", 20), ("side", 99)]
    assert [k for k, _ in walk["lines"]] == ["a", "a", "b", "c", "side"]
    assert [t[3] for t in walk["turns"]] == ["a", "b", "c"]  # sidechain is not a parent call
    assert walk["compacts"] == [T0 + timedelta(seconds=3)]
    assert [p for p, _ in walk["prompts"]] == [0, 2]  # tool results are not prompts


def test_rereads_ride_later_calls_until_compaction_by_request_key(o, tmp_path):
    # Two calls in the SAME second: matching by key, never by time.
    rows = (_assistant("p1", 0, 100) + _assistant("p2", 0, 100) + _assistant("n1", 1, 10)
            + _assistant("n2", 2, 10)
            + [{"type": "system", "subtype": "compact_boundary", "timestamp": _ts(3)}]
            + _assistant("n3", 4, 10))
    walk = o.walk_requests(_write(tmp_path / "t.jsonl", rows))
    post = [walk["outputs"][0], walk["outputs"][1]]
    score = o.score_rereads(walk, post, [40, -10], lambda turn: 0.5)
    # p1's 40 rides p2, n1, n2; p2's -10 rides n1, n2; n3 is after compaction.
    assert score["tokens"] == 40 + 30 + 30
    assert score["usd"] == pytest.approx(100 * 0.5 / 1e6)
    assert score["unmapped_requests"] == 0
    unknown = o.score_rereads(walk, post, [40, -10], lambda turn: None)
    assert unknown["usd"] == 0 and unknown["unpriced_tokens"] == 40 + 30 + 30


def test_legacy_line_unit_window_reproduces_booking_then_maps_to_requests(o, tmp_path):
    # Each request streams over two lines; the old nudge counted lines.
    rows = _session([100] * 4 + [40, 40], per_prompt=2, lines=2)
    walk = o.walk_requests(_write(tmp_path / "t.jsonl", rows))
    # pre_turns=8 lines (4 requests), post 4 lines (2 requests), baseline 100:
    # booked by the old code = (100 - 40) x 4 lines = 240, twice the real 120.
    got = o.legacy_line_window(walk, 8, 4, 100, 240)
    assert got is not None
    rows_out, base = got
    assert [r[2] for r in rows_out] == [40, 40] and base == 100
    assert o.legacy_line_window(walk, 8, 4, 100, 239) is None  # must reproduce exactly


def test_boundary_changes_and_nearest_controls(o, tmp_path):
    walk = o.walk_requests(_write(tmp_path / "t.jsonl", _session([300] * 30 + [150] * 3)))
    changes = o.boundary_changes(walk)
    assert changes[-1][:4] == (30, 3, 300, 150.0)
    assert all(c[3] == 0 for c in changes[:-1])
    treated = [(30, 3, 300, 100.0, 1010)]
    controls = [(30, 3, 300, 20.0, 1010), (3000, 40, 9000, 999.0, 900000)]
    assert o.match_controls(treated, controls, k=1) == [20.0]


def _db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE savings_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,"
                 " tokens_saved INTEGER, cost_saved_usd REAL, session_uuid TEXT, detail TEXT)")
    conn.execute("CREATE TABLE compression_events (id INTEGER PRIMARY KEY, timestamp TEXT, session_uuid TEXT)")
    for table, values in rows:
        cols = "timestamp,event_type,tokens_saved,session_uuid,detail" if table == "savings_events" else "timestamp,session_uuid"
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(values))})", values)
    conn.commit()
    return conn


def _study_fixture(m, o, tmp_path, post_outputs):
    nudge_local = "2026-01-05T12:00:00"
    calib_local = "2026-01-05T12:30:00"
    treated = _session([500] * 30 + post_outputs + [500] * 6)
    paths = {"T": _write(tmp_path / "T.jsonl", treated)}
    for name in ("C1", "C2"):
        paths[name] = _write(tmp_path / f"{name}.jsonl", _session([500] * 60))
    booked = max(0, int((500 - sum(post_outputs) / 3) * 3))
    conn = _db(tmp_path / "s.db", [
        ("savings_events", (nudge_local, "verbosity_steer", 0, "T", "fill=80% pre_avg_out=500 pre_turns=30 unit=req")),
        ("savings_events", (calib_local, "verbosity_steer_measured", booked, "T",
                            f"calibrates_ts={nudge_local} post_turns=3 pre_avg_out=500")),
        ("compression_events", (nudge_local, "C1")),
        ("compression_events", (nudge_local, "C2")),
    ])
    tier = m.PRICING_TIERS["anthropic"]
    study = o.verbosity_study(conn, datetime(2025, 12, 1), datetime(2026, 2, 1), tier, paths.get)
    return study, booked


def test_study_credits_a_real_reduction_net_of_flat_controls(m, o, tmp_path):
    study, booked = _study_fixture(m, o, tmp_path, [200, 200, 200])
    assert booked == 900
    assert study["status"]["verified"] == 1 and study["matched"] == 1
    assert study["control_boundaries"] >= o.MIN_CONTROL_SAMPLES
    assert study["output_tokens"]["net"] == 900
    # 300 fewer tokens per reply, re-read by the 8, 7 and 6 later parent calls.
    assert study["reread_tokens"]["net"] == 300 * (8 + 7 + 6)
    assert study["output_usd"]["net"] == pytest.approx(900 * 25 / 1e6)
    credit = o.credited(study)
    assert credit["tokens"] == 900 and credit["basis"] == "net_of_control"


def test_study_never_credits_growth_and_keeps_it_signed(m, o, tmp_path):
    study, booked = _study_fixture(m, o, tmp_path, [800, 800, 800])
    assert booked == 0  # the old clamp booked nothing
    assert study["output_tokens"]["signed"] == -900  # but the debit is kept
    assert study["reread_tokens"]["net"] < 0
    assert o.credited(study)["tokens"] == 0
    assert o.credited(study)["basis"] == "no_reduction_net_of_control"


def test_summary_pool_is_not_measured_without_a_study_and_scales_per_event(o):
    entry = {"events": 4, "tokens_saved": 4000, "cost_saved_usd": 0.1}
    pool = o.summary_pool(entry, study=None)
    assert pool["basis"] == "not_measured" and pool["cost_saved_usd"] == 0.0
    assert pool["booked_tokens"] == 4000
    assert "NOT MEASURED" in o.cli_line(pool)
    study = {"matched": 2, "control_boundaries": 50, "window_utc": ["a", "b"],
             "output_tokens": {"net": 600}, "output_usd": {"net": 0.015},
             "reread_tokens": {"net": 6000}, "reread_usd": {"net": 0.003},
             "per_event": {k: {"mean": 300.0, "ci95": [100.0, 500.0]} for k in ("net", "booked", "control")}}
    pool = o.summary_pool(entry, study=study)
    assert pool["output_tokens"] == 1200 and pool["reread_tokens"] == 12000
    assert pool["cost_saved_usd"] == pytest.approx(0.036)
    assert "net of an un-nudged control" in o.cli_line(pool, "0.04")
    study["output_tokens"]["net"] = -600
    study["output_usd"]["net"] = -0.015
    study["reread_tokens"]["net"] = -1
    study["reread_usd"]["net"] = -0.001
    pool = o.summary_pool(entry, study=study)
    assert pool["cost_saved_usd"] == 0.0 and pool["basis"] == "no_reduction_net_of_control"
    assert "$0 credited" in o.cli_line(pool)


def test_booked_verbosity_rows_leave_the_realized_total(m):
    rows = [("tool_archive", 2, 1000, 0.5), ("verbosity_steer_measured", 3, 900, 0.02)]
    by_category, tokens, cost, events, popped, _ = m._net_realized_savings(rows, {})
    assert "verbosity_steer_measured" not in by_category
    assert (tokens, events) == (1000, 2) and cost == pytest.approx(0.5)
    assert popped["verbosity_steer_measured"]["tokens_saved"] == 900


def test_study_refresh_writes_one_file_and_respects_age(m, o, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(o, "verbosity_study", lambda *a, **k: calls.append(1) or {"matched": 0})
    conn = sqlite3.connect(":memory:")
    from datetime import timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    assert o.refresh_study(conn, now_utc=now)["computed_at_utc"] == now.isoformat()
    assert (tmp_path / o.STUDY_FILE).exists()
    assert o.refresh_study(conn, now_utc=now + timedelta(hours=1)) is None
    assert len(calls) == 1
