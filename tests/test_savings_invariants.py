"""Savings invariants from docs/METHODOLOGY.md.

R5: for non-negative parts, a longer window is never smaller than a shorter one,
and since install is never smaller than the last 30 days.
R15: the methodology names every headline part and every estimate key the code
adds up, so the document cannot drift from the code.

Run: python3 -m pytest tests/test_savings_invariants.py -v
"""
import importlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
METHODOLOGY = ROOT / "docs" / "METHODOLOGY.md"

SESSIONS = ("11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000")


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    (tmp_path / "home").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod._init_trends_db().close()
    yield mod
    sys.modules.pop("measure", None)


def _seed_history(m):
    """Logged savings spread over 120 days, install 130 days ago."""
    (m.SNAPSHOT_DIR / "snapshot_before.json").write_text(json.dumps(
        {"timestamp": (datetime.now() - timedelta(days=130)).isoformat(), "totals": {"estimated_total": 0}}))
    conn = sqlite3.connect(str(m.TRENDS_DB))
    for day in range(1, 121, 3):
        ts = (datetime.now() - timedelta(days=day, hours=1)).isoformat()
        sid = SESSIONS[day % 2]
        conn.execute(
            "INSERT INTO savings_events (timestamp, event_type, tokens_saved, cost_saved_usd, session_id, detail, model) "
            "VALUES (?, 'checkpoint_restore', ?, ?, ?, 'restore', 'claude-opus-5-5')",
            (ts, 1000 + day, (1000 + day) * 5e-6, sid))
        conn.execute(
            "INSERT INTO compression_events (timestamp, session_id, feature, command_pattern, original_tokens, "
            "compressed_tokens, verified, tier, model) VALUES (?, ?, 'bash_compress_git', 'git log', ?, 100, 1, 'measured', "
            "'claude-opus-5-5')",
            (ts, sid, 2000 + day))
    conn.commit()
    conn.close()


def _all_in(m, days):
    merged = m._get_merged_savings(days=days)
    counted = m._counted_cumulative_summary(days=days)
    return m._action_savings_components(merged, counted)


def test_longer_windows_are_never_smaller(m):
    _seed_history(m)
    previous = None
    for days in (30, 60, 90, 120):
        parts = _all_in(m, days)
        current = (parts["logged_usd"], parts["reread_usd"], parts["estimated_usd"], parts["total_usd"])
        assert all(v >= 0 for v in current)
        if previous is not None:
            assert all(c >= p - 1e-9 for c, p in zip(current, previous)), (days, previous, current)
        previous = current
    assert previous[0] > 0


def test_since_install_is_at_least_the_last_30_days(m):
    _seed_history(m)
    month = _all_in(m, 30)["display"]["total_usd"]
    since = m._savings_since_install()["all_in"]["total_usd"]
    assert month > 0 and since >= month


def test_methodology_names_every_part_the_code_adds_up(m):
    doc = METHODOLOGY.read_text(encoding="utf-8")
    parts = m._action_savings_components({}, {})
    estimate_keys = set(parts["estimates_usd"]) | {"hint_followed", "retrieval_serve"}
    missing = sorted(k for k in estimate_keys if f"`{k}`" not in doc)
    assert not missing, f"docs/METHODOLOGY.md does not name: {missing}"
    for heading in ("Logged actions", "Model routing", "Repeat reads avoided", "Other estimates", "Since install"):
        assert heading in doc
    for name in ("_FIXTURE_SESSION_GLOBS", "_MEDIA_TOOL_GLOBS", "_COUNTED_TWIN_WINDOW_SECONDS",
                 "_ROUTABLE_OPUS_FRACTION", "_TRANSFORMATION_FLAT_BAND", "_USAGE_PARSER_VERSION"):
        assert name in doc and hasattr(m, name), name


def test_every_test_the_methodology_cites_exists():
    doc = METHODOLOGY.read_text(encoding="utf-8")
    cited = set(re.findall(r"`(test_[a-z0-9_]+\.py)`", doc))
    assert cited
    missing = sorted(name for name in cited if not (ROOT / "tests" / name).exists())
    assert not missing, missing


def test_dated_consumption_windows_are_monotone_without_ingestion_fallback(m):
    conn = m._init_trends_db()
    for ago, tokens in ((1, 100), (45, 200), (75, 300)):
        day = (datetime.now() - timedelta(days=ago)).date().isoformat()
        usage = {day: {"claude-opus-5-5": {"fresh_input": tokens, "output": 10}}}
        conn.execute("INSERT INTO session_log(jsonl_path,date,daily_usage_json) VALUES (?,?,?)",
                     (f"missing-{ago}.jsonl", datetime.now().date().isoformat(), json.dumps(usage)))
    conn.execute("INSERT INTO session_log(jsonl_path,date,input_tokens,output_tokens) VALUES ('undated.jsonl',?,999999,100)",
                 (datetime.now().date().isoformat(),))
    conn.commit()
    assert [m._dashboard_spent_token_basis(conn, days)["tokens"] for days in (30, 60, 90)] == [110, 320, 630]
    assert m._dashboard_spent_token_basis(conn, 30)["complete"] is False
    conn.close()


def test_methodology_discloses_checkpoint_restore_bounds_and_scope():
    doc = METHODOLOGY.read_text(encoding="utf-8")
    assert "6,300" in doc and "4,000" in doc and "1,500" in doc and "800" in doc
    assert "OpenCode and OpenClaw" in doc
    assert "working set" in doc and "checkpoint_restore" in doc
