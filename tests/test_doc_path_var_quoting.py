#!/usr/bin/env python3
"""Docs must quote path variables in shell command lines (#301 follow-up).

The skill docs hand agents copy-paste bash. A path variable left unquoted —
`"$PY" $MEASURE_PY report` — word-splits under a Windows home like
`C:\\Users\\First Last`, breaking the path the same way the PR fixed for the
interpreter itself. Every command-line use of a resolved path variable must be
quoted: `"$PY" "$MEASURE_PY" report`.

This test scans every .md under skills/ and commands/ (the canonical docs;
plugins/ and cowork/ are generated mirrors checked by check-mirror-sync.sh),
extracts fenced code blocks and inline code spans — the places agents actually
run commands — and flags any occurrence of a known path variable that is not
inside shell quotes. Prose mentions (`` `$VAR` ``), comments, and variables
inside "..." or '...' are all fine.

Run: python3 -m pytest tests/test_doc_path_var_quoting.py -q
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("skills", "commands")

# Variables the docs resolve to filesystem paths (or, for $PY, an interpreter
# the docs always quote for consistency). $RUNTIME and loop temporaries like
# $_cand are deliberately absent: they hold runtime names or are quoted inline.
PATH_VARS = (
    "MEASURE_PY", "PY", "COORD_PATH", "COACH_DIR", "BACKUP_DIR",
    "TO_LAUNCHER", "FLEET_PY", "PULL_PY", "CLAUDE_PLUGIN_ROOT", "PWD",
)
_VAR_RE = re.compile(r"\$\{?(" + "|".join(PATH_VARS) + r")\}?\b")
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _md_files():
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.md")):
            yield path


def _code_segments(path):
    """Yield (lineno, text) for every fenced-block line and inline code span.

    Prose outside code spans is not scanned: `` `$MEASURE_PY` `` mentions and
    sentences like "$PY is the verified interpreter" are not commands.
    """
    in_fence = False
    fence_marker = ""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence = False
            continue
        if in_fence:
            yield lineno, line
            continue
        for span in _SPAN_RE.finditer(line):
            # A span that is only `$VAR` is a name mention, not a command.
            if re.fullmatch(r"\s*\$\w+\s*", span.group(1)):
                continue
            yield lineno, span.group(1)


def _unquoted_vars(text):
    """Return the PATH_VARS names used unquoted in one code segment.

    Tracks shell single/double quotes, including quotes nested inside $(...)
    command substitutions (e.g. "$(ls -A "$BACKUP_DIR")"), so a variable that
    is quoted for the shell is never flagged. A `` `$VAR` `` mention is a
    backtick-quoted name, not a command, and is skipped.
    """
    hits = []
    saved = []          # (in_dq, in_sq) per open $( level
    in_dq = in_sq = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if in_sq:
            if c == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            if c == '"':
                in_dq = False
            elif c == "$" and i + 1 < n and text[i + 1] == "(":
                saved.append((in_dq, in_sq))
                in_dq = in_sq = False
                i += 2
                continue
            i += 1
            continue
        if c == '"':
            in_dq = True
        elif c == "'":
            in_sq = True
        elif c == "$" and i + 1 < n and text[i + 1] == "(":
            saved.append((in_dq, in_sq))
            in_dq = in_sq = False
            i += 2
            continue
        elif c == ")" and saved:
            in_dq, in_sq = saved.pop()
        elif c == "$":
            m = _VAR_RE.match(text, i)
            if m:
                j = m.end()
                before = text[i - 1] if i else ""
                after = text[j] if j < n else ""
                if not (before == "`" and after == "`"):
                    hits.append(m.group(1))
                i = j
                continue
        i += 1
    return hits


@pytest.mark.parametrize("path", list(_md_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_path_vars_are_quoted_in_commands(path):
    findings = []
    for lineno, segment in _code_segments(path):
        if segment.strip().startswith("#"):
            continue  # shell comment or prompt heading, not a command
        for var in _unquoted_vars(segment):
            findings.append(f"{path.relative_to(REPO)}:{lineno}: unquoted ${var} in `{segment.strip()[:100]}`")
    assert not findings, "Unquoted path variable in a command line (word-splits on spaced paths):\n" + "\n".join(findings)


@pytest.mark.parametrize("segment,expected", [
    ("${MEASURE_PY} report", ["MEASURE_PY"]),
    ("${PY} -c pass", ["PY"]),
    ("${COORD_PATH}/state.json --json", ["COORD_PATH"]),
    ('"${MEASURE_PY}" report --json', []),
    ("echo '${COORD_PATH}/state.json'", []),
    ("${PYTHONPATH:-/x} tool", []),
])
def test_brace_form_vars_follow_the_same_quote_rules(segment, expected):
    """${VAR} word-splits exactly like $VAR, so the guard must check both
    forms."""
    assert _unquoted_vars(segment) == expected
