#!/usr/bin/env python3
"""Verify the countable and threshold claims in our docs against the code.

Why this exists: on 2026-07-21 an audit found docs-site frozen at v5.11.29 while
the code had moved to v5.11.53. Four separate numeric claims were wrong, and one
page contradicted itself two rows apart ("60+ command patterns" in one table,
"30+ CLI families" in another, describing the same thing). None of it was caught
by review, because prose drifts silently -- nothing fails when a number goes
stale. Guards that nothing enforces are decoration, so this one runs in CI.

What it does: computes ground truth from the source, then scans every doc for
sentences that state that number and asserts they agree.

On 2026-07-21 the first version checked only COUNTS, and two threshold
regressions (archive purge 24h vs 48h, loop firing floor 0.7 vs 0.6) sailed
through while it printed "all countable claims match the code". So thresholds
are now claims too: numeric constants the docs quote (purge windows, confidence
floors, fill floors), each extracted from the defining line of source, each
matched exactly. What this still does NOT verify: prose descriptions of
behavior, benchmark results, and any claim that is not a number.

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
import os
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
        # `[\s-]` not `\s`: the hyphenated attributive form ("a 57-fixture
        # suite") is how the README phrased it, and a whitespace-only pattern
        # read straight past it while reporting success. Same failure shape as
        # the bare-"patterns" hole below -- the checker only sees the phrasings
        # someone thought to enumerate, so prefer the loosest separator that
        # cannot match an unrelated number.
        r"(\d+)\+?[\s-]fixtures?\b",
        r"suite holds\s+(\d+)",
    ]),
    ("commands", "compressible commands", count_commands, [
        r"(\d+)\+?\s+command patterns",
        r"(\d+)\+?\s+commands\b",
        # Bare "N+ patterns". Added 2026-07-26: README.md:182 carried "60+
        # patterns" through the entire July docs-accuracy sweep because every
        # pattern here required a qualifier ("command patterns", "pattern
        # families"). The docstring above used "60+ patterns" as its worked
        # example of an at-least claim while the checker could not see it.
        # A guard whose own example slips past it is decoration.
        r"(\d+)\+?\s+patterns\b",
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


# --------------------------------------------------------------------------
# Threshold claims: numeric constants the docs quote. Ground truth is the
# defining line of source, extracted by regex -- loudly, never a fallback, so a
# refactor that moves the constant breaks the CHECKER instead of silently
# passing everything. Matched exactly (no "+" semantics). These exist because
# the count-only first version let two threshold regressions through while
# printing a success message that read as full verification.
# --------------------------------------------------------------------------

def _source_value(relpath: str, pattern: str, what: str) -> float:
    src = (REPO / relpath).read_text(encoding="utf-8")
    m = re.search(pattern, src)
    if not m:
        raise RuntimeError(f"ground truth for {what} not found in {relpath} "
                           f"(pattern {pattern!r})")
    return float(m.group(1))


_MEASURE = "skills/token-optimizer/scripts/measure.py"
_ARCHIVER = "skills/token-optimizer/scripts/archive_result.py"
_REGISTRY = "skills/token-optimizer/scripts/detectors/registry.py"
_FLEET = "skills/fleet-auditor/scripts/fleet.py"

# Fleet Auditor carries its OWN confidence floor (fleet.py min_confidence = 0.4),
# a different population from the session-detector floor in registry.py (0.3).
# The same "Confidence floor | N |" table row appears on both pages, so each
# claim is scoped to its page -- the same apples-to-oranges lesson as the
# OpenClaw detector EXCLUDE above.
_FLEET_PAGE = "docs-site/src/content/docs/features/fleet-auditor"
THRESHOLD_EXCLUDE = {
    "detector_floor": (_FLEET_PAGE,),
}
THRESHOLD_ONLY = {
    "fleet_floor": (_FLEET_PAGE,),
}

# (key, human label, (source file, extraction regex), doc regexes stating it)
THRESHOLDS = [
    ("archive_auto_purge_h", "automatic archive purge window (hours)",
     (_ARCHIVER, r"_ARCHIVE_RETENTION_HOURS_DEFAULT = (\d+)"), [
        r"purges archives older than (\d+) hours",
        r"[Aa]utomatic purge after (\d+) hours",
        r"After (\d+) hours, applied when the hook",
        r"the automatic (\d+)-hour purge",
    ]),
    ("archive_manual_default_h", "manual archive-cleanup default (hours)",
     (_MEASURE, r'_int_env\("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS", (\d+)\)'), [
        r"stricter default of (\d+) hours",
        r"older than (\d+)h \(default",
        r"older than (\d+) hours by default",
        r"\(default (\d+); the automatic",
        r"trims on a (\d+)-hour default",
    ]),
    ("quality_cache_default_d", "quality-cache retention default (days)",
     (_MEASURE, r'_int_env\("TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS", (\d+)\)'), [
        r"TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS` \(default: (\d+) days\)",
        r"TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS`, default: (\d+) days",
        r"TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS` \| (\d+) \|",
        r"TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS` \(default (\d+)\)",
    ]),
    ("loop_firing_floor", "loop-warning firing floor",
     (_MEASURE, r'best\["confidence"\] < ([0-9]+(?:\.[0-9]+)?)'), [
        r"strongest signal is at least ([0-9]+(?:\.[0-9]+)?)",
        r"[Ss]trongest signal ≥ ([0-9]+(?:\.[0-9]+)?)",
    ]),
    ("detector_floor", "detector confidence floor",
     (_REGISTRY, r'"confidence", 0\) > ([0-9]+(?:\.[0-9]+)?)'), [
        r"suppressed at or below a ([0-9]+(?:\.[0-9]+)?) confidence threshold",
        r"Confidence floor \| ([0-9]+(?:\.[0-9]+)?) \|",
    ]),
    ("fleet_floor", "fleet-auditor confidence floor",
     (_FLEET, r"min_confidence = ([0-9]+(?:\.[0-9]+)?)"), [
        r"Confidence floor \| ([0-9]+(?:\.[0-9]+)?) \|",
    ]),
    ("verbosity_min_fill", "lean-output nudge fill floor (%)",
     (_MEASURE, r'_int_env\("TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL", (\d+)\)'), [
        r"TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL` \| `(\d+)`",
        r"TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL` \(default `(\d+)`\)",
        r"context fills past (\d+)%",
    ]),
    ("fresh_nudge_min_fill", "fresh-session nudge fill floor (%)",
     (_MEASURE, r'_int_env\("TOKEN_OPTIMIZER_FRESH_NUDGE_MIN_FILL", (\d+)\)'), [
        r"TOKEN_OPTIMIZER_FRESH_NUDGE_MIN_FILL` \| `(\d+)`",
        r"Fresh nudge minimum fill \| `(\d+)`",
    ]),
]


def docs() -> list[Path]:
    """Every surface that quotes numbers at a reader: the docs site and all
    seven READMEs (root plus the six per-platform ones)."""
    out = list((REPO / "docs-site" / "src" / "content" / "docs").rglob("*.mdx"))
    out += [p for p in REPO.rglob("README*.md") if not _is_scratch(p)]
    out += [REPO / "PRIVACY.md", REPO / "SECURITY.md"]
    return sorted(out)


# Skip every dot-directory rather than denylisting them one at a time. Scratch
# worktrees under dot-directories are not shipping documentation, and a general
# structural rule avoids maintaining a list of every scratch-tool directory.
_SCRATCH_DIRS = {"node_modules"}


def _is_scratch(p: Path) -> bool:
    return any(
        part.startswith(".") or part in _SCRATCH_DIRS
        for part in p.relative_to(REPO).parts[:-1]
    )


def _path_matches_prefix(path: Path | str, prefixes: tuple[str, ...]) -> bool:
    """Match repository-relative paths regardless of the host separator."""
    candidate = str(path)
    if os.name == "nt":
        candidate = candidate.replace("\\", "/")
        prefixes = tuple(prefix.replace("\\", "/") for prefix in prefixes)
    return any(candidate.startswith(prefix) for prefix in prefixes)


def check() -> tuple[list[str], dict[str, int]]:
    findings: list[str] = []
    stats = {"count_claims": 0, "threshold_claims": 0, "files": 0}
    files = docs()
    stats["files"] = len(files)

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
            if _path_matches_prefix(rel, skip):
                continue

            for pat in patterns:
                for m in re.finditer(pat, text, re.I):
                    stated = int(m.group(1))
                    stats["count_claims"] += 1
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

    for key, label, (relpath, src_pat), patterns in THRESHOLDS:
        truth = _source_value(relpath, src_pat, label)
        tseen: dict[float, list[str]] = {}
        skip = THRESHOLD_EXCLUDE.get(key, ())
        only = THRESHOLD_ONLY.get(key)

        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(REPO)
            if _path_matches_prefix(rel, skip):
                continue
            if only and not _path_matches_prefix(rel, only):
                continue

            for pat in patterns:
                for m in re.finditer(pat, text):
                    stated = float(m.group(1))
                    stats["threshold_claims"] += 1
                    line = text[:m.start()].count("\n") + 1
                    where = f"{rel}:{line}"
                    tseen.setdefault(stated, []).append(where)
                    if abs(stated - truth) > 1e-9:
                        findings.append(
                            f"[{key}] {where}: claims '{m.group(0).strip()}' but the "
                            f"code sets {label} to {truth:g}")

        if len(tseen) > 1:
            spread = "; ".join(
                f"{n:g} at {', '.join(locs)}" for n, locs in sorted(tseen.items()))
            findings.append(
                f"[{key}] INCONSISTENT: docs state different values for {label} -> {spread}")

    return findings, stats


def main() -> int:
    try:
        findings, stats = check()
    except Exception as exc:  # a broken checker must be loud, never silently green
        print(f"docs-claims check FAILED to run: {exc}", file=sys.stderr)
        return 1

    if not findings:
        print(
            f"docs claims: OK. Verified {stats['count_claims']} countable claims "
            f"(detectors, fixtures, categories, commands, families, compressors) and "
            f"{stats['threshold_claims']} threshold claims (archive purge windows, loop "
            f"firing floor, detector confidence floor, nudge fill floors) across "
            f"{stats['files']} docs. NOT verified: prose behavior descriptions, "
            f"benchmark results, and any claim that is not a number."
        )
        return 0

    print(f"docs claims: {len(findings)} finding(s)\n")
    for f in findings:
        print(f"  {f}")
    print("\nFix the docs, or update scripts/check_docs_claims.py if the shape of "
          "the claim changed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
