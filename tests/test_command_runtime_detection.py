#!/usr/bin/env python3
"""Regression test for OpenCode slash-command runtime detection.

The slash-command / skill markdown files each carry a bash preamble that
resolves the runtime and locates measure.py before running it. Before the fix,
that preamble only knew claude/codex and searched only ~/.codex and
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
        "will mis-resolve to codex/claude and reach into ~/.claude."
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
    ~/.config/opencode/plugins/cache path."""
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


# Docs whose resolver block echoes resolved values for later commands. The
# echoes must stay off stdout — a --json command chained in the same Bash call
# keeps parseable stdout only when the echoes go to stderr — and must be
# paste-safe (printf %q), because a bare `echo "MEASURE_PY=$MEASURE_PY"` prints
# a spaced Windows path unquoted and word-splits when the agent reuses it.
ECHO_DOCS = [
    "skills/token-dashboard/SKILL.md",
    "skills/token-coach/SKILL.md",
    "skills/token-optimizer/SKILL.md",
    "skills/fleet-auditor/SKILL.md",
    "skills/token-optimizer/references/codex-workflow.md",
]
_RESOLVED_VAR_RE = re.compile(
    r"\$(?:MEASURE_PY|PY|COACH_DIR|FLEET_PY|RUNTIME|COORD_PATH|TO_LAUNCHER|PULL_PY|BACKUP_DIR)\b")


@pytest.mark.parametrize("rel", ECHO_DOCS)
def test_resolver_echoes_stay_off_stdout_and_paste_safe(rel):
    for lineno, line in enumerate(_read(rel).splitlines(), 1):
        s = line.strip()
        m = re.search(r"\b(?:echo|printf)\b", s)
        if not m:
            continue
        # Only the echo/printf tail matters: a resolved var inside the
        # `[ -z "$VAR" ]` guard or an earlier command on the line is not
        # something the echo prints.
        emitted = s[m.start():]
        if not _RESOLVED_VAR_RE.search(emitted):
            continue
        assert ">&2" in emitted, (
            f"{rel}:{lineno}: resolver echo writes to stdout — a --json command "
            f"chained in the same Bash call becomes unparseable: {s}"
        )
        assert not re.match(r'echo "[A-Z_]+=[$]', emitted), (
            f"{rel}:{lineno}: bare VAR=value echo is not paste-safe on spaced "
            f"paths; use printf 'NAME=%q\\n' \"$VAR\" >&2: {s}"
        )
