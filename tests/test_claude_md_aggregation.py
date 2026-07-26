#!/usr/bin/env python3
"""Regression test: CLAUDE.md quick-win must report per-file, not blended.

measure_components() keys each ancestor CLAUDE.md as its own component
(claude_md_global, claude_md_home, claude_md_project_<dir>), mirroring how
Claude Code loads CLAUDE.md files up the directory tree. The "Slim CLAUDE.md"
quick win and the quick_scan offender list used to sum every claude_md*-prefixed
key into one blended number and report it as if it were one file. On a layout
with 2-3 CLAUDE.md files up the tree that produced a combined token/line count
that wasn't any single file's size, plus a "slim to ~300 lines" recommendation
that wasn't actionable on any one file.

These tests assert the fix: each claude_md_* component over the threshold gets
its own recommendation / offender entry naming the specific file and its own
line count, and that two distinct components no longer collapse into one
misleading string.

Run: python3 -m pytest tests/test_claude_md_aggregation.py -v
"""

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

if "measure" in sys.modules:
    del sys.modules["measure"]
import measure  # noqa: E402


def _claude_md_component(path, tokens, lines):
    return {"path": path, "exists": True, "tokens": tokens, "lines": lines}


# Two distinct ancestor CLAUDE.md files, each independently over the 6,000-token
# quick tier. Paths deliberately do NOT start with the test machine's HOME so
# the ~-abbreviation in the label is a no-op and the test is hermetic.
_COMPONENTS_TWO_LARGE = {
    "claude_md_global": _claude_md_component("/workspace/CLAUDE.md", 7000, 420),
    "claude_md_project_repo": _claude_md_component("/workspace/repo/CLAUDE.md", 6500, 390),
}

# Two files whose SUM (6,000) crosses the old quick threshold but neither file
# alone does. Old blending code triggered a recommendation here; per-file code
# must not.
_COMPONENTS_SUM_ONLY = {
    "claude_md_global": _claude_md_component("/workspace/CLAUDE.md", 3000, 200),
    "claude_md_project_repo": _claude_md_component("/workspace/repo/CLAUDE.md", 3000, 200),
}


def test_generate_auto_recommendations_reports_per_file():
    """Two large claude_md_* components -> two quick entries, each naming its file."""
    plan_md, total = measure.generate_auto_recommendations(_COMPONENTS_TWO_LARGE)

    # Two per-file "Slim" recommendations (one per offending file), not one blended.
    slim_count = plan_md.count("**Slim ")
    assert slim_count == 2, (
        f"expected 2 per-file Slim entries, got {slim_count}:\n{plan_md}"
    )

    # Each entry names its specific file path and its OWN token count, not the sum.
    assert "/workspace/CLAUDE.md" in plan_md and "7,000" in plan_md, plan_md
    assert "/workspace/repo/CLAUDE.md" in plan_md and "6,500" in plan_md, plan_md

    # The blended total (13,500) must not appear — that's the bug signature.
    assert "13,500" not in plan_md, f"blended total leaked into per-file plan:\n{plan_md}"


def test_generate_auto_recommendations_no_blended_trigger():
    """Two files whose sum crosses the threshold but neither alone does -> no quick entry.

    Old code summed to 6,000 and emitted one 'Slim CLAUDE.md' recommendation.
    Per-file code must not trigger on either file (3,000 < 6,000 quick, < 5,000 medium).
    """
    plan_md, _ = measure.generate_auto_recommendations(_COMPONENTS_SUM_ONLY)
    assert "**Slim " not in plan_md, (
        f"per-file code must not trigger on a sum-only crossing:\n{plan_md}"
    )
    assert "Consider slimming" not in plan_md, (
        f"per-file code must not medium-trigger on a sum-only crossing:\n{plan_md}"
    )


def test_quick_scan_offenders_split_per_file(monkeypatch):
    """quick_scan's top_offenders lists each CLAUDE.md file separately, not blended."""
    monkeypatch.setattr(measure, "measure_components", lambda: _COMPONENTS_TWO_LARGE)
    monkeypatch.setattr(measure, "detect_context_window", lambda: (200_000, "test"))
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")

    result = measure.quick_scan(as_json=True)

    claude_offenders = [o for o in result["top_offenders"] if o["name"] == "claude_md"]
    assert len(claude_offenders) == 2, (
        f"expected 2 per-file claude_md offenders, got {len(claude_offenders)}: "
        f"{claude_offenders}"
    )
    details = {o["detail"] for o in claude_offenders}
    assert any("/workspace/CLAUDE.md" in d for d in details), details
    assert any("/workspace/repo/CLAUDE.md" in d for d in details), details
    # No blended "CLAUDE.md (810 lines)" entry collapsing both files.
    for d in details:
        assert "810 lines" not in d, f"blended line count leaked into offender: {d}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
