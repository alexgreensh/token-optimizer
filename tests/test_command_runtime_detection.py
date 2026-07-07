#!/usr/bin/env python3
"""Regression test for issue #57 — OpenCode slash-command runtime detection.

The slash-command / skill markdown files each carry a bash preamble that
resolves the runtime and locates measure.py before running it. Before the #57
fix, that preamble only knew claude/codex and searched only ~/.codex and
~/.claude, so under OpenCode (which has full detection in runtime_env.py but is
invisible to this standalone bash snippet) it mis-resolved to codex/claude and
reached into ~/.claude — exactly the bug Guy reported and re-reported.

This test pins the fix: every markdown carrying the detection preamble must
(a) have an OpenCode branch, (b) check env signals before the ~/.codex directory
heuristic (so a host with BOTH ~/.codex and ~/.config/opencode running OpenCode
resolves to opencode), and (c) never hardcode `TOKEN_OPTIMIZER_RUNTIME=codex`
on the PRIMARY run line (it must use the resolved `$RUNTIME`).

Run: python3 -m pytest tests/test_command_runtime_detection.py -v
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Markdown files that carry the runtime-detection preamble.
PREAMBLE_FILES = [
    "commands/quick.md",
    "commands/health.md",
    "skills/token-coach/SKILL.md",
    "skills/fleet-auditor/SKILL.md",
    "skills/token-dashboard/SKILL.md",
]


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", PREAMBLE_FILES)
def test_has_opencode_branch(rel):
    text = _read(rel)
    assert 'RUNTIME="opencode"' in text, (
        f"{rel}: detection preamble has no OpenCode branch — under OpenCode it "
        "will mis-resolve to codex/claude and reach into ~/.claude (issue #57)."
    )


@pytest.mark.parametrize("rel", PREAMBLE_FILES)
def test_env_signal_checked_before_codex_dir_heuristic(rel):
    """The OPENCODE env-signal branch must appear before the `~/.codex` directory
    heuristic, so a host with both ~/.codex and ~/.config/opencode resolves to
    opencode when actually running OpenCode."""
    text = _read(rel)
    opencode_env = text.find('[ -n "$OPENCODE"')
    codex_dir = text.find('[ -d "$HOME/.codex" ]')
    assert opencode_env != -1, f"{rel}: missing OPENCODE env-signal branch"
    assert codex_dir != -1, f"{rel}: missing ~/.codex directory heuristic"
    assert opencode_env < codex_dir, (
        f"{rel}: OPENCODE env signal must be checked before the ~/.codex "
        "directory heuristic (else a codex-and-opencode host misroutes)."
    )


@pytest.mark.parametrize("rel", PREAMBLE_FILES)
def test_search_loop_has_opencode_path(rel):
    """Resolving RUNTIME=opencode is useless if the measure.py/fleet.py search glob
    never looks under the OpenCode plugin cache — the script resolves opencode and
    then fails 'not found'. Every preamble's search loop must include a
    ~/.config/opencode/plugins/cache path (issue #57, caught in torture)."""
    text = _read(rel)
    assert ".config/opencode/plugins/cache" in text, (
        f"{rel}: RUNTIME resolves opencode but the script-search loop has no "
        "~/.config/opencode/plugins/cache entry — it will fail 'not found' under OpenCode."
    )


@pytest.mark.parametrize("rel", ["commands/quick.md", "commands/health.md"])
def test_primary_run_uses_resolved_runtime_not_hardcoded_codex(rel):
    """The primary run instruction must invoke with TOKEN_OPTIMIZER_RUNTIME="$RUNTIME",
    never a hardcoded =codex (which is what forced Guy's OpenCode session to run as
    codex against ~/.claude)."""
    text = _read(rel)
    # The quick/health verbs are the primary invocation for these two commands.
    verb = "quick" if "quick" in rel else "health"
    # No line should hardcode `TOKEN_OPTIMIZER_RUNTIME=codex python3 ... <verb>`.
    bad = re.search(
        r'TOKEN_OPTIMIZER_RUNTIME=codex\s+python3\s+"?\$MEASURE_PY"?\s+' + verb,
        text,
    )
    assert bad is None, (
        f"{rel}: primary '{verb}' run line hardcodes TOKEN_OPTIMIZER_RUNTIME=codex; "
        'it must use TOKEN_OPTIMIZER_RUNTIME="$RUNTIME" so OpenCode is honored.'
    )
    assert 'TOKEN_OPTIMIZER_RUNTIME="$RUNTIME"' in text, (
        f"{rel}: expected the resolved-runtime run form "
        'TOKEN_OPTIMIZER_RUNTIME="$RUNTIME".'
    )
