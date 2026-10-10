#!/usr/bin/env python3
"""Prove the regression guards still catch the regressions they exist for.

Each entry re-introduces one bug that has shipped before (in a throwaway copy
of the scripts), runs the guard tests against that copy, and requires them to
FAIL. A guard that passes on the broken code is decoration, so the check fails.
If a code edit moves the text a mutation targets, the check fails too, naming
the entry to update: a mutation that silently stops applying proves nothing.

Run from the repo root: python3 scripts/check_regression_guards.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = "measure.py"

# (name, tests that must catch it, [(old, new), ...] applied to measure.py)
MUTATIONS = [
    ("baseline slides onto post-install months",
     ["tests/test_golden_user.py", "tests/test_pinned_baseline.py", "tests/test_weekly_full_savings.py"],
     [("            pinned = _pinned_workload_anchor()\n", "            pinned = None\n"),
      ('str(frozen.get("month") or "") <= month', 'str(frozen.get("month") or "") == month')]),
    ("re-reads priced at the family card, not the exact model",
     ["tests/test_golden_user.py"],
     [("key = _claude_price_key(model, models) if model else None",
       "key = _normalize_model_name(model) if model else None")]),
    ("savings report leaves out avoided re-reads",
     ["tests/test_golden_user.py", "tests/test_pinned_baseline.py"],
     [('    summary["reread_avoided"] = _reread_savings_for_window(days)\n', "")]),
    ("one session stored under two paths counted twice",
     ["tests/test_golden_user.py", "tests/test_cost_accuracy_200.py"],
     [("            if any(_session_rank(r[1], r[2], r[3]) >= mine for r in other):", "            if False:"),
      ("def _dedupe_session_rows(conn):", "def _dedupe_session_rows(conn):\n    return 0\n\n\ndef _unused_dedupe(conn):"),
      ('                conn.executemany("DELETE FROM session_log WHERE id = ?", [(r[0],) for r in other])',
       "                pass")]),
    ("a flat whole-workload week hides the measured savings",
     ["tests/test_weekly_full_savings.py"],
     [('return value if float((value or {}).get("saved_usd") or 0.0) > 0 else None', "return value"),
      ('if wdays == 7 and weekly_full and float(weekly_full.get("saved_usd") or 0.0) > ctx + rt:',
       "if wdays == 7 and weekly_full:")]),
    ("a growing session keeps its stale per-day usage",
     ["tests/test_golden_user.py"],
     [("               daily_usage_json=excluded.daily_usage_json",
       "               daily_usage_json=session_log.daily_usage_json")]),
    ("re-collecting a grown session adds its old tokens again",
     ["tests/test_golden_user.py"],
     [("               input_tokens=excluded.input_tokens,",
       "               input_tokens=session_log.input_tokens + excluded.input_tokens,")]),
    ("stored re-read rows keep a pricing bug after it is fixed",
     ["tests/test_golden_user.py"],
     [("        _reprice_counted_rows_once(conn)\n", "")]),
    ("a thinner history silently shrinks the frozen baseline",
     ["tests/test_baseline_safety.py"],
     [("def _baseline_shrink_is_material(incumbent, candidate):",
       "def _baseline_shrink_is_material(incumbent, candidate):\n    return False\n\n\ndef _unused_shrink(incumbent, candidate):")]),
    ("a generation model priced at its family card",
     ["tests/test_bundled_prices.py", "tests/test_cost_accuracy_200.py"],
     [("    m = _CLAUDE_GENERATION_RE.search(model_id.lower())\n    if m:",
       "    m = None\n    if m:")]),
]


def _copy_repo(src: Path, dst: Path) -> None:
    """Copy the tree for mutation, skipping (with a warning) anything unreadable.

    One chmod-000 file must not abort the whole check: the guards never read it.
    """
    def _copy(s, d):
        try:
            return shutil.copy2(s, d)
        except OSError as exc:
            print(f"warning: skipping unreadable file {s}: {exc}", file=sys.stderr)
            return d

    try:
        shutil.copytree(src, dst, copy_function=_copy, ignore=shutil.ignore_patterns(
            ".git", "node_modules", "docs-site", "research", "__pycache__", "*.pyc"))
    except shutil.Error as exc:
        for entry in exc.args[0]:
            print(f"warning: skipping unreadable path {entry[0]}: {entry[2]}", file=sys.stderr)


def _run_guard(copy_root: Path, tests: list[str]) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
        cwd=copy_root, capture_output=True, text=True, timeout=900)
    return proc.returncode


def main() -> int:
    source = (SCRIPTS / MEASURE).read_text(encoding="utf-8")
    failures = []
    for name, tests, edits in MUTATIONS:
        mutated = source
        missing = [old for old, _new in edits if old not in mutated]
        if missing:
            failures.append(f"{name}: mutation no longer applies (code moved); update this entry")
            continue
        for old, new in edits:
            mutated = mutated.replace(old, new, 1)
        with tempfile.TemporaryDirectory() as tmp:
            copy_root = Path(tmp) / "repo"
            _copy_repo(REPO, copy_root)
            (copy_root / "skills" / "token-optimizer" / "scripts" / MEASURE).write_text(mutated, encoding="utf-8")
            code = _run_guard(copy_root, tests)
        if code == 0:
            failures.append(f"{name}: guard tests PASSED on the broken code; they no longer protect it")
            print(f"FAIL  {name}")
        else:
            print(f"ok    {name}")
    if failures:
        print("\nRegression guards are not guarding:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\nAll {len(MUTATIONS)} known regressions are caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
