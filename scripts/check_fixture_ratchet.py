#!/usr/bin/env python3
"""Fixture ratchet check (SPEC-local-adapt Part B).

1. Fixture count per compressor in benchmark.py + fixtures/*.json may never drop
   compared to a baseline ref (--base).
2. A commit that touches a compressor file without adding or modifying a fixture
   file fails unless its commit message carries `fixture: none needed (<reason>)`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO / "skills" / "token-optimizer" / "scripts" / "fixtures"
COMPRESSOR_FILES = {
    "skills/token-optimizer/scripts/bash_compress.py",
    "skills/token-optimizer/scripts/bash_compress_hook.py",
    "skills/token-optimizer/scripts/build_output_compress.py",
    "skills/token-optimizer/scripts/command_compress_extra.py",
    "skills/token-optimizer/scripts/builtin_tool_compress.py",
    "skills/token-optimizer/scripts/pipe_file_compress.py",
    "skills/token-optimizer/scripts/large_output_policy.py",
}
EXEMPT_RE = re.compile(r"fixture:\s*none needed\s*\([^)]+\)", re.IGNORECASE)


def count_fixtures_in_tree(root: Path = REPO) -> Counter[str]:
    counts: Counter[str] = Counter()
    fdir = root / "skills" / "token-optimizer" / "scripts" / "fixtures"
    if fdir.is_dir():
        for fp in sorted(fdir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict):
                    counts[it.get("compressor") or "bash_compress"] += 1
    return counts


def count_fixtures_at_ref(ref: str, root: Path = REPO) -> Counter[str]:
    counts: Counter[str] = Counter()
    r = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", ref,
         "skills/token-optimizer/scripts/fixtures"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return counts
    for rel in r.stdout.splitlines():
        if not rel.endswith(".json"):
            continue
        blob = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"], capture_output=True, text=True)
        if blob.returncode != 0:
            continue
        try:
            data = json.loads(blob.stdout)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict):
                counts[it.get("compressor") or "bash_compress"] += 1
    return counts


def check_commit_touch(changed_files: list[str], commit_message: str) -> list[str]:
    touched_comp = [f for f in changed_files if f in COMPRESSOR_FILES]
    if not touched_comp:
        return []
    touched_fix = any(
        f.startswith("skills/token-optimizer/scripts/fixtures/")
        or f == "skills/token-optimizer/scripts/benchmark.py"
        for f in changed_files
    )
    if touched_fix or EXEMPT_RE.search(commit_message or ""):
        return []
    return [
        f"Compressor file(s) modified ({', '.join(touched_comp)}) without a fixture update "
        "or 'fixture: none needed (<reason>)' in commit message."
    ]


def check_counts(current: Counter[str], baseline: Counter[str]) -> list[str]:
    errors = []
    for comp, base_n in sorted(baseline.items()):
        cur_n = current.get(comp, 0)
        if cur_n < base_n:
            errors.append(f"Fixture count for '{comp}' dropped: {base_n} -> {cur_n}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="", help="Git ref to compare fixture counts against")
    ap.add_argument("--check-head", action="store_true", help="Check HEAD commit touch rule")
    args = ap.parse_args(argv)

    cur = count_fixtures_in_tree(REPO)
    errors: list[str] = []
    if args.base:
        base = count_fixtures_at_ref(args.base, REPO)
        errors.extend(check_counts(cur, base))
    if args.check_head:
        files_r = subprocess.run(["git", "-C", str(REPO), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                                 capture_output=True, text=True)
        msg_r = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--pretty=%B"],
                               capture_output=True, text=True)
        if files_r.returncode == 0 and msg_r.returncode == 0:
            errors.extend(check_commit_touch(files_r.stdout.splitlines(), msg_r.stdout))
    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"OK: fixture ratchet passed ({sum(cur.values())} external fixtures across {len(cur)} compressors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
