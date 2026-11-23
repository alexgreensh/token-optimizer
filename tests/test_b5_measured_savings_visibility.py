"""End-to-end visibility tests for B5 measured savings features.

Verifies that fake measured events for each B5 feature (`crossturn_dedup`,
`bash_search_noise_fold`) appear with their own human labels in:
  1. `measure.py savings --json` (`by_category` + totals + action_savings),
  2. `measure.py savings` CLI text output,
  3. `runway_all_in.extra_work` (plan-limits capacity view),
  4. `assets/savings_story.js` (dashboard breakdown rendering & labels).
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
ASSETS = ROOT / "skills" / "token-optimizer" / "assets"


@pytest.fixture()
def isolated_measure(monkeypatch, tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "b5-vis-session")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import measure
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(measure, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(measure, "TRENDS_DB", snap / "trends.db")
    monkeypatch.setattr(measure, "_TRENDS_DB_INITIALIZED", False, raising=False)
    conn = measure._init_trends_db()
    yield measure, conn, snap
    conn.close()


def test_b5_features_show_in_savings_summary_json_and_cli_with_human_labels(
    isolated_measure, capsys
):
    measure, conn, _ = isolated_measure
    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now().date().isoformat()

    # Seed one priced session so model pricing & plan-limits work cleanly.
    daily_usage = json.dumps({
        today: {
            "claude-sonnet-4-6": {
                "fresh_input": 100_000,
                "output": 20_000,
                "cache_read": 50_000,
                "cache_create": 10_000,
            }
        }
    })
    conn.execute(
        """INSERT INTO session_log
           (jsonl_path, session_uuid, date, project, daily_usage_json, parent_daily_usage_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("/tmp/b5-vis-session.jsonl", "b5-vis-session", today, "b5-proj", daily_usage, daily_usage),
    )

    # Insert fake measured events for B5 features.
    conn.execute(
        """INSERT INTO compression_events
           (timestamp, feature, session_id, command_pattern, original_tokens,
            compressed_tokens, quality_preserved, verified, tier, model)
           VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'measured', ?)""",
        (now_iso, "crossturn_dedup", "b5-vis-session", "git status", 4000, 400, "claude-sonnet-4-6"),
    )
    conn.execute(
        """INSERT INTO compression_events
           (timestamp, feature, session_id, command_pattern, original_tokens,
            compressed_tokens, quality_preserved, verified, tier, model)
           VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'measured', ?)""",
        (now_iso, "bash_search_noise_fold", "b5-vis-session", "rg -n createSession .", 5000, 1500, "claude-sonnet-4-6"),
    )
    conn.commit()

    # 1. savings_report --json
    measure.savings_report(days=30, as_json=True)
    raw_json = capsys.readouterr().out
    summary = json.loads(raw_json)
    by_cat = summary["by_category"]

    expected = {
        "crossturn_dedup": ("Cross-turn dedup", 3600),
        "bash_search_noise_fold": ("Bash search noise folding", 3500),
    }
    for key, (human_label, tok) in expected.items():
        assert key in by_cat, f"{key} missing from savings summary JSON by_category"
        entry = by_cat[key]
        assert entry["label"] == human_label
        assert entry["events"] == 1
        assert entry["tokens_saved"] == tok

    assert summary["total_tokens"] >= 3600 + 3500
    assert summary["action_savings"]["logged_usd"] > 0

    # 2. savings_report CLI text output
    measure.savings_report(days=30, as_json=False)
    cli_text = capsys.readouterr().out
    for _key, (human_label, _tok) in expected.items():
        assert human_label in cli_text, f"{human_label!r} missing from CLI savings output"

    # 3. Plan-limits / runway capacity view reflects measured savings
    from _measure import runway_all_in
    extra_pct = runway_all_in.extra_work(measure, conn, 30, summary, estimated_only=False)
    assert extra_pct is not None and extra_pct > 0


def test_dashboard_savings_story_renders_category_breakdown_and_labels():
    js = (ASSETS / "savings_story.js").read_text(encoding="utf-8")
    assert "Measured breakdown by feature" in js
    assert "crossturn_dedup" in js
    assert "bash_search_noise_fold" in js
    assert "Bash search noise folding" in js
