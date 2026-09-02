#!/usr/bin/env python3
"""Refuse to ship a README that is gutted, truncated, or stripped of license.

README.md is a shipped interface read by people and coding agents. This gate
checks structural invariants in CI so destructive truncation and malformed HTML
cannot pass as ordinary documentation edits.

What it checks, on every README the project ships:
  1. SIZE FLOOR      -- a mass deletion fails. The floors below are tripwires set
                        well under current length, not targets to grow toward.
  2. HTML TERMINATION -- every opened tag closes, and quotes inside a tag balance.
  3. REQUIRED MARKERS -- license and attribution survive in the root README.

What it does NOT check: prose quality, link liveness, or whether the content is
accurate. Numeric accuracy is scripts/check_docs_claims.py's job; these two are
deliberately separate so a failure names the right problem.

Run locally:  python3 scripts/check_readme_integrity.py
Exit 0 clean, 1 with findings printed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Floors are tripwires, not targets. They allow ordinary editing while rejecting
# a README whose substantive structure has been removed.
MIN_LINES = {
    "README.md": 400,
    "openclaw/README.md": 60,
    "opencode/README.md": 40,
    "hermes/README.md": 40,
    "antigravity/README.md": 40,
}

# Substrings that preserve required licensing and attribution information.
REQUIRED_MARKERS = {
    "README.md": [
        ("LICENSE", "license reference"),
        ("PolyForm Noncommercial", "license name"),
        ("Alex Greenshpun", "author attribution"),
    ],
}

_SCRATCH_DIRS = {"node_modules"}
# Bare `<` that is arithmetic or prose ("<400 tokens") is not a tag open. Only
# treat `<` as a tag when a tag name or a closing slash follows immediately.
_TAG_OPEN = re.compile(r"<(/?[a-zA-Z][a-zA-Z0-9-]*)")


def _is_scratch(p: Path) -> bool:
    return any(
        part.startswith(".") or part in _SCRATCH_DIRS
        for part in p.relative_to(REPO).parts[:-1]
    )


def shipped_readmes() -> list[Path]:
    return sorted(p for p in REPO.rglob("README*.md") if not _is_scratch(p))


def check_html_termination(text: str, rel: str) -> list[str]:
    """Every tag that opens must close, and its quotes must balance.

    Walks tag by tag rather than counting `<` against `>` across the file,
    because a truncated tag at EOF is invisible to a global count once any
    later `>` exists (for example, in a Markdown blockquote).
    """
    findings: list[str] = []
    for m in _TAG_OPEN.finditer(text):
        close = text.find(">", m.start())
        if close == -1:
            line = text.count("\n", 0, m.start()) + 1
            snippet = text[m.start():m.start() + 40].replace("\n", "\\n")
            findings.append(
                f"{rel}:{line}: unterminated <{m.group(1)}> tag -- no '>' before "
                f"end of file: {snippet!r}"
            )
            break  # one truncated tag invalidates everything after it; don't spam
        if text.count('"', m.start(), close) % 2:
            line = text.count("\n", 0, m.start()) + 1
            findings.append(
                f"{rel}:{line}: unbalanced quote inside <{m.group(1)}> tag"
            )
    return findings


def check() -> list[str]:
    findings: list[str] = []
    seen: set[str] = set()

    for path in shipped_readmes():
        rel = path.relative_to(REPO).as_posix()
        seen.add(rel)
        text = path.read_text(encoding="utf-8")

        floor = MIN_LINES.get(rel)
        if floor is not None:
            n = len(text.splitlines())
            if n < floor:
                findings.append(
                    f"{rel}: {n} lines, below the {floor}-line floor. A README "
                    f"does not lose this much by accident -- if the shrink is "
                    f"intended, lower the floor in scripts/check_readme_integrity.py "
                    f"in the same commit."
                )

        findings.extend(check_html_termination(text, rel))

        for marker, what in REQUIRED_MARKERS.get(rel, []):
            if marker not in text:
                findings.append(f"{rel}: {what} is missing (expected {marker!r})")

    # A floor for a file that no longer exists is a guard watching an empty
    # room. Fail loudly rather than quietly checking nothing.
    for rel in MIN_LINES:
        if rel not in seen:
            findings.append(
                f"{rel}: listed in MIN_LINES but not found. Either the file moved "
                f"(update the map) or it was deleted (say so deliberately)."
            )

    return findings


def main() -> int:
    try:
        findings = check()
    except Exception as exc:  # a broken checker must be loud, never silently green
        print(f"readme-integrity check FAILED to run: {exc}", file=sys.stderr)
        return 1

    if not findings:
        print(
            f"readme integrity: OK. Verified size floors, HTML termination, and "
            f"required license/attribution markers across "
            f"{len(shipped_readmes())} READMEs."
        )
        return 0

    print(f"readme integrity: {len(findings)} finding(s)\n")
    for f in findings:
        print(f"  {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
