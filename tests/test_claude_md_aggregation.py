#!/usr/bin/env python3
"""Regression test: CLAUDE.md findings must report per-file, not blended.

Reimplements the direction of external PR #100 by danikdanik
(https://github.com/alexgreensh/token-optimizer/pull/100), reconciled onto the
current tree (our savings math moved from the ~4,500-token internal heuristic
to Anthropic's documented 200-line guidance, so the assertions here check the
per-file BEHAVIOR rather than the PR's original wording).

measure_components() keys each ancestor CLAUDE.md as its own component
(claude_md_global, claude_md_home, claude_md_project_<dir>), mirroring how
Claude Code loads CLAUDE.md files up the directory tree. quick_scan's offender
list, its quick_win, and generate_auto_recommendations' Rule 2 all used to sum
every claude_md*-prefixed key into one blended number and report it as if it
were a single file. On a layout with 2-3 CLAUDE.md files that produced a
token/line count matching no real file, plus a "slim to N lines" action that
was not actionable on any one of them -- and it could fire on a combined total
when no individual file was over threshold.

Run: python3 -m pytest tests/test_claude_md_aggregation.py -q
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def _cmd(path, tokens, lines):
    return {"path": path, "exists": True, "tokens": tokens, "lines": lines}


# Two distinct ancestor CLAUDE.md files, each independently over the quick tier
# (>6,000 tokens AND >200 lines). Paths deliberately do not start with the test
# machine's HOME, so the ~-abbreviation is a no-op and the test stays hermetic.
_TWO_LARGE = {
    "claude_md_global": _cmd("/workspace/CLAUDE.md", 7000, 420),
    "claude_md_project_repo": _cmd("/workspace/repo/CLAUDE.md", 6500, 390),
}

# Neither file crosses the threshold alone; only their SUM would. The blended
# code fired here. Per-file code must stay silent.
_SUM_ONLY = {
    "claude_md_global": _cmd("/workspace/CLAUDE.md", 3000, 150),
    "claude_md_project_repo": _cmd("/workspace/repo/CLAUDE.md", 3000, 150),
}


def test_recommendations_report_each_file_separately(measure):
    plan_md, _total = measure.generate_auto_recommendations(_TWO_LARGE)

    assert plan_md.count("**Slim ") == 2, (
        f"expected one Slim entry per offending file, got:\n{plan_md}"
    )
    assert "/workspace/CLAUDE.md" in plan_md and "7,000" in plan_md, plan_md
    assert "/workspace/repo/CLAUDE.md" in plan_md and "6,500" in plan_md, plan_md
    # The blended total must never appear.
    assert "13,500" not in plan_md, f"blended total leaked:\n{plan_md}"


def test_recommendations_do_not_fire_on_a_blended_total(measure):
    plan_md, _total = measure.generate_auto_recommendations(_SUM_ONLY)

    assert "**Slim " not in plan_md, (
        f"fired on the SUM of two under-threshold files:\n{plan_md}"
    )
    assert "Consider slimming" not in plan_md, plan_md


def test_single_file_still_reported(measure):
    """The common case must be unaffected by the per-file change."""
    plan_md, _total = measure.generate_auto_recommendations(
        {"claude_md_global": _cmd("/workspace/CLAUDE.md", 9000, 600)})

    assert plan_md.count("**Slim ") == 1
    assert "/workspace/CLAUDE.md" in plan_md


def test_savings_math_stays_per_file_200_line_guidance(measure):
    """Recoverable tokens are computed from THAT file, not a blended rate."""
    plan_md, _total = measure.generate_auto_recommendations(
        {"claude_md_global": _cmd("/workspace/CLAUDE.md", 8000, 400)})

    # 8000 tokens / 400 lines = 20 tokens/line; target 200 lines = 4,000 tokens;
    # recoverable = 4,000.
    assert "4,000 tokens recoverable" in plan_md, plan_md
    assert "20.0 tokens/line" in plan_md, plan_md


def test_short_but_dense_file_gets_no_contradictory_entry(measure):
    """>6,000 tokens but <=200 lines: no 'slim to 200 lines' recommendation."""
    plan_md, _total = measure.generate_auto_recommendations(
        {"claude_md_global": _cmd("/workspace/CLAUDE.md", 9000, 120)})

    assert "**Slim " not in plan_md, plan_md


def test_quick_scan_offenders_split_per_file(measure, monkeypatch, capsys):
    """quick_scan's offender list names each file instead of one blended row."""
    import json as _json

    monkeypatch.setattr(measure, "measure_components", lambda *a, **kw: dict(_TWO_LARGE))
    measure.quick_scan(as_json=True)  # prints the structured payload
    payload = _json.loads(capsys.readouterr().out)

    claude_offenders = [
        o for o in (payload.get("top_offenders") or []) if o.get("name") == "claude_md"
    ]
    assert len(claude_offenders) == 2, (
        f"expected 2 per-file claude_md offenders, got {claude_offenders}"
    )
    details = [o.get("detail", "") for o in claude_offenders]
    assert "/workspace/CLAUDE.md (420 lines)" in details, details
    assert "/workspace/repo/CLAUDE.md (390 lines)" in details, details
    # No blended row. (payload["overhead_tokens"] legitimately totals all
    # components, so the blend check is scoped to the offender rows.)
    blob = _json.dumps(claude_offenders)
    assert "810" not in blob and "13500" not in blob, blob
    assert "CLAUDE.md (810 lines)" not in blob, blob

    # quick_win names the LARGEST single file, not a blended one.
    qw = payload.get("quick_win") or {}
    assert "/workspace/CLAUDE.md" in qw.get("action", ""), qw
    assert "420 lines" in qw.get("action", ""), qw
