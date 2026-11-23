"""Tests for SPEC-prefix-cost: advisory prefix cost report in limit units."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
from _measure import prefix_cost as pc  # noqa: E402


def _seed_conn(model: str = "claude-opus-5-5", requests: int = 200,
               cache_read: int = 20_000_000, cache_create_1h: int = 2_000_000) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE session_log (session_id TEXT, parent_daily_usage_json TEXT, daily_calls_json TEXT)")
    today = datetime.now().date().isoformat()
    usage = {
        today: {
            model: {
                "fresh_input": 10_000,
                "output": 50_000,
                "cache_read": cache_read,
                "cache_create_1h": cache_create_1h,
                "cache_create": cache_create_1h,
                "requests": requests,
            }
        }
    }
    conn.execute("INSERT INTO session_log VALUES ('s1', ?, ?)",
                 (json.dumps(usage), json.dumps({today: requests})))
    return conn


def test_prefix_cost_ranking_unused_and_r2_sum():
    conn = _seed_conn()
    inv = [
        {"name": "mcp_giant", "kind": "mcp_server", "tokens": 12000, "tool_count": 30},
        {"name": "mcp_used", "kind": "mcp_server", "tokens": 8000, "tool_count": 20},
        {"name": "rule_global", "kind": "rule", "tokens": 2000},
    ]
    rep = pc.prefix_cost_report(measure, conn, days=30, inventory=inv,
                                usage_counts={"mcp_giant": 0, "mcp_used": 1, "rule_global": 0})
    assert rep["advisory"] is True
    assert rep["auto_applied"] is False
    # Ranking descending by cost
    names = [r["name"] for r in rep["top_10"]]
    assert names == ["mcp_giant", "mcp_used", "rule_global"]
    # An item with 1 use is NEVER "unused" and never proposed for removal
    used_row = next(r for r in rep["top_10"] if r["name"] == "mcp_used")
    assert used_row["unused_here"] is False
    assert used_row["proposed_fix"] is None
    # Unused item gets approve-to-apply fix
    giant_row = next(r for r in rep["top_10"] if r["name"] == "mcp_giant")
    assert giant_row["unused_here"] is True
    assert "mcp/disable mcp_giant" in giant_row["proposed_fix"]
    # R2: parts add up to total
    assert round(sum(r["limit_units"] for r in rep["all_components"]), 2) == rep["total_prefix_limit_units"]
    # Top unused > 2% triggers status-bar hint
    assert rep["status_bar_hint"] is not None


def test_prefix_cost_unknown_model_unpriced_and_zero_mcp_and_hint_silent():
    conn_unk = _seed_conn(model="unknown-model-x")
    rep_unk = pc.prefix_cost_report(
        measure, conn_unk, days=30,
        inventory=[{"name": "claude_md", "kind": "claude_md", "tokens": 1000}],
    )
    assert rep_unk["unpriced_tokens"] > 0
    assert rep_unk["total_prefix_limit_units"] == 0.0

    # Zero MCP servers + small unused item under 2% -> hint stays silent
    conn = _seed_conn(requests=10, cache_read=50_000_000, cache_create_1h=5_000_000)
    rep_small = pc.prefix_cost_report(
        measure, conn, days=30,
        inventory=[{"name": "tiny_rule", "kind": "rule", "tokens": 100}],
        usage_counts={"tiny_rule": 0},
    )
    assert len(rep_small["top_10"]) == 1
    assert rep_small["status_bar_hint"] is None


def test_prefix_cost_deferred_tools_counted_smaller():
    conn = _seed_conn()
    inv = [{"name": "mcp_big", "kind": "mcp_server", "tokens": 12000, "tool_count": 20}]
    full = pc.prefix_cost_report(measure, conn, inventory=inv, deferred_tools=False)
    deferred = pc.prefix_cost_report(measure, conn, inventory=inv, deferred_tools=True)
    assert deferred["top_10"][0]["tokens"] < full["top_10"][0]["tokens"]
    assert deferred["total_prefix_limit_units"] < full["total_prefix_limit_units"]
