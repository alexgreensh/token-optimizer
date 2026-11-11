"""bash_compress.compress never drops error or warning lines.

Before this fix the re-injection cap walked an unordered set shared with
"distinctive" lines, so a lone `warning:` or `error:` line in a large git diff,
git status or pytest output could be evicted. Synthetic inputs only.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bash_compress  # noqa: E402

WORDS = "alpha beta gamma delta epsilon zeta theta kappa lambda sigma omega render parse".split()


def _name(i):
    return f"src/{WORDS[i % len(WORDS)]}_{i:03d}.py"


def _git_status(n=40):
    lines = ["On branch feature/x", "", "Changes not staged for commit:",
             '  (use "git add <file>..." to update what will be committed)']
    lines += [f"\tmodified:   {_name(i)}" for i in range(n)]
    lines += ["", "Untracked files:"] + [f"\t{_name(100 + i)}" for i in range(n // 2)]
    return lines


def _git_diff(files=8, hunks=3):
    lines = []
    for f in range(files):
        p = _name(f)
        lines += [f"diff --git a/{p} b/{p}", f"index {f:07x}1..{f:07x}2 100644", f"--- a/{p}", f"+++ b/{p}"]
        for h in range(hunks):
            lines.append(f"@@ -{10 + h * 40},12 +{10 + h * 40},13 @@ def fn_{f}_{h}():")
            lines += [f"{' ' if k % 4 else '+'}    value_{f}_{h}_{k} = token_{k}({f * 100 + h * 10 + k})"
                      for k in range(12)]
    return lines


def _pytest(n=120):
    rows = ["============================= test session starts ==============================",
            f"collected {n} items", ""]
    rows += [f"tests/test_{WORDS[i % len(WORDS)]}.py::test_case_{i:03d} PASSED  [{(i + 1) * 100 // n:3d}%]"
             for i in range(n)]
    return rows + ["", f"============================== {n} passed in 4.21s =============================="]


def _with(lines, at, extra):
    out = list(lines)
    out.insert(at, extra)
    return "\n".join(out) + "\n"


CASES = [
    ("git status", _with(_git_status(), 12, "warning: could not open directory 'cache/': Permission denied")),
    ("git diff", _with(_git_diff(), 33, "warning: in the working copy of 'src/x.py', LF will be replaced by CRLF")),
    ("pytest -v tests", _with(_pytest(), 60, "tests/test_kappa.py:12: DeprecationWarning: old api used")),
    ("git diff", _with(_git_diff(), 33, "error: could not read 'src/x.py': No such file or directory")),
]


@pytest.mark.parametrize("command,raw", CASES, ids=["warn-git-status", "warn-git-diff", "warn-pytest", "error-git-diff"])
def test_lone_error_or_warning_line_survives(command, raw):
    target = next(ln for ln in raw.splitlines() if "warning" in ln.lower() or ln.startswith("error:"))
    out = bash_compress.compress(command, raw)
    assert len(out) < len(raw) * 0.9, "the case must actually compress"
    assert target in out.splitlines()


def test_cap_never_evicts_priority_lines():
    """Many distinctive lines and several warnings: every warning stays."""
    lines = _git_diff(10, 4)
    for k, at in enumerate((20, 90, 160, 230)):
        lines.insert(at, f"warning: synthetic warning number {k}")
    raw = "\n".join(lines) + "\n"
    out = bash_compress.compress("git diff", raw).splitlines()
    for k in range(4):
        assert f"warning: synthetic warning number {k}" in out


def test_output_is_deterministic_across_processes():
    command, raw = CASES[1]
    code = ("import sys; sys.path.insert(0, %r); import bash_compress; "
            "sys.stdout.write(bash_compress.compress(%r, sys.stdin.read()))" % (str(SCRIPTS), command))
    outs = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.add(subprocess.run([sys.executable, "-c", code], input=raw, capture_output=True,
                                text=True, env=env, check=True).stdout)
    assert len(outs) == 1
    assert bash_compress.compress(command, raw) == outs.pop()
