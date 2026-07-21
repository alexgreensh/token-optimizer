#!/usr/bin/env python3
"""Verify the countable claims in our docs against the code that backs them.

Why this exists: on 2026-07-21 an audit found docs-site frozen at v5.11.29 while
the code had moved to v5.11.53. Four separate numeric claims were wrong, and one
page contradicted itself two rows apart ("60+ command patterns" in one table,
"30+ CLI families" in another, describing the same thing). None of it was caught
by review, because prose drifts silently -- nothing fails when a number goes
stale. Guards that nothing enforces are decoration, so this one runs in CI.

What it does: computes ground truth from the source, then scans every doc for
sentences that state that number and asserts they agree.

Two claim styles, deliberately treated differently:
  - Exact ("12 detectors")  -> must equal ground truth.
  - At-least ("60+ patterns") -> must not overstate, and must not *undersell* so
    badly the number is useless. Undersell is a real failure, not a nicety: the
    comparison table is a sales surface, and "60+" when the truth is 111 loses an
    argument we are actually winning.

Run locally:  python3 scripts/check_docs_claims.py
Exit 0 clean, 1 with findings printed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

# How far a "N+" claim may lag the truth before we call it underselling.
# 1.5x is judgement, not science: "60+" against 111 (1.85x) reads as stale,
# "100+" against 111 (1.11x) reads as a deliberate round-down.
UNDERSELL_RATIO = 1.5


# --------------------------------------------------------------------------
# Ground truth. Each function returns an int computed from the source, never a
# constant -- a hardcoded expectation here would rot exactly like the docs did.
# --------------------------------------------------------------------------

def count_detectors() -> int:
    """Behavioral waste detectors. registry.py and __init__.py are plumbing."""
    d = SCRIPTS / "detectors"
    return len([p for p in d.glob("*.py") if p.name not in ("registry.py", "__init__.py")])


def count_compressors() -> int:
    """Distinct _compress_* implementations in the bash compressor."""
    src = (SCRIPTS / "bash_compress.py").read_text(encoding="utf-8")
    return len(re.findall(r"^def _compress_", src, re.M))


def count_pattern_families() -> int:
    """Entries in _PATTERN_HANDLERS -- the 'families' a command routes into."""
    src = (SCRIPTS / "bash_compress.py").read_text(encoding="utf-8")
    block = re.search(r"^_PATTERN_HANDLERS = \{(.*?)^\}", src, re.M | re.S)
    if not block:
        raise RuntimeError("_PATTERN_HANDLERS not found in bash_compress.py")
    return len(re.findall(r'^\s+"', block.group(1), re.M))


def count_commands() -> int:
    """Distinct command strings _detect_pattern knows how to route."""
    src = (SCRIPTS / "bash_compress.py").read_text(encoding="utf-8")
    block = re.search(r"^def _detect_pattern.*?(?=^def )", src, re.M | re.S)
    if not block:
        raise RuntimeError("_detect_pattern not found in bash_compress.py")
    return len(set(re.findall(r'"([a-z0-9_.-]+)"', block.group(0))))


def _fixtures() -> list[ast.Dict]:
    """The built-in FIXTURES list, parsed with ast.

    Parsed, not counted by hand. The first version of this function walked the
    source counting braces at depth 1 and returned 55; the real answer is 87.
    Fixtures embed nested dicts and raw command output full of braces, so every
    hand-rolled scan gets a different wrong number -- this one produced three
    different answers across three attempts before it got parsed properly.
    If you cannot reproduce a count twice, you do not know it.
    """
    tree = ast.parse((SCRIPTS / "benchmark.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "FIXTURES":
            return [e for e in node.value.elts if isinstance(e, ast.Dict)]
    raise RuntimeError("FIXTURES list not found in benchmark.py")


def count_fixtures() -> int:
    return len(_fixtures())


def count_fixture_categories() -> int:
    cats = set()
    for entry in _fixtures():
        for k, v in zip(entry.keys, entry.values):
            if isinstance(k, ast.Constant) and k.value == "category" and isinstance(v, ast.Constant):
                cats.add(v.value)
    return len(cats)


# --------------------------------------------------------------------------
# Claims. (key, human label, truth fn, regexes that state it in prose)
# Add a row here when a doc starts quoting a new number.
# --------------------------------------------------------------------------

# Some claims are scoped to a platform. OpenClaw ships its OWN native TypeScript
# detector set (openclaw/src/waste-detectors.ts, three tiers) which is a different
# population from the Python detectors under skills/token-optimizer/scripts/detectors/.
# Comparing the two is apples to oranges, and the first version of this checker did
# exactly that and reported a correct doc as broken. A gate that cries wolf gets
# muted, so scope beats cleverness here.
EXCLUDE = {
    "detectors": ("openclaw/",),
}

CLAIMS = [
    ("detectors", "behavioral detectors", count_detectors, [
        r"(\d+)\+?\s+detectors",
    ]),
    ("fixtures", "benchmark fixtures", count_fixtures, [
        r"(\d+)\+?\s+fixtures",
        r"suite holds\s+(\d+)",
    ]),
    ("commands", "compressible commands", count_commands, [
        r"(\d+)\+?\s+command patterns",
        r"(\d+)\+?\s+commands\b",
    ]),
    ("families", "command pattern families", count_pattern_families, [
        r"(\d+)\+?\s+CLI families",
        r"(\d+)\+?\s+pattern families",
    ]),
    ("compressors", "compressor implementations", count_compressors, [
        r"(\d+)\+?\s+compressors",
    ]),
    ("fixture_categories", "fixture categories", count_fixture_categories, [
        r"(\d+)\+?\s+fixture categories",
        r"across\s+(\d+)\s+categories",
    ]),
]


def docs() -> list[Path]:
    """Every surface that quotes numbers at a reader: the docs site and all
    six READMEs (root plus the five per-platform ones)."""
    out = list((REPO / "docs-site" / "src" / "content" / "docs").rglob("*.mdx"))
    out += [p for p in REPO.rglob("README*.md")
            if ".git" not in p.parts
            and "node_modules" not in p.parts
            and ".pytest_cache" not in p.parts]
    return sorted(out)


def check() -> list[str]:
    findings: list[str] = []
    files = docs()

    for key, label, truth_fn, patterns in CLAIMS:
        truth = truth_fn()
        seen: dict[int, list[str]] = {}

        skip = EXCLUDE.get(key, ())

        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(REPO)
            if any(str(rel).startswith(prefix) for prefix in skip):
                continue

            for pat in patterns:
                for m in re.finditer(pat, text, re.I):
                    stated = int(m.group(1))
                    approx = m.group(0).rstrip().endswith("+") or "+" in m.group(0)
                    line = text[:m.start()].count("\n") + 1
                    where = f"{rel}:{line}"
                    seen.setdefault(stated, []).append(where)

                    if approx:
                        if stated > truth:
                            findings.append(
                                f"[{key}] {where}: claims '{m.group(0).strip()}' but only "
                                f"{truth} {label} exist (overstated)")
                        elif truth > stated * UNDERSELL_RATIO:
                            findings.append(
                                f"[{key}] {where}: claims '{m.group(0).strip()}' but there are "
                                f"{truth} {label} (underselling by {truth / stated:.1f}x)")
                    elif stated != truth:
                        findings.append(
                            f"[{key}] {where}: claims '{m.group(0).strip()}' but there are "
                            f"{truth} {label}")

        # Same fact, two different numbers across pages. This is the failure that
        # costs the most credibility, because a reader can see it without leaving
        # the page.
        if len(seen) > 1:
            spread = "; ".join(
                f"{n} at {', '.join(locs)}" for n, locs in sorted(seen.items()))
            findings.append(
                f"[{key}] INCONSISTENT: docs state different values for {label} -> {spread}")

    return findings


def main() -> int:
    try:
        findings = check()
    except Exception as exc:  # a broken checker must be loud, never silently green
        print(f"docs-claims check FAILED to run: {exc}", file=sys.stderr)
        return 1

    if not findings:
        print("docs claims: all countable claims match the code")
        return 0

    print(f"docs claims: {len(findings)} finding(s)\n")
    for f in findings:
        print(f"  {f}")
    print("\nFix the docs, or update scripts/check_docs_claims.py if the shape of "
          "the claim changed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
