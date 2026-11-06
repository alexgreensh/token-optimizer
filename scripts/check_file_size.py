#!/usr/bin/env python3
"""Code-file size ratchet: no code file may grow past MAX_LINES (default 1000).

Rule:
  * A NEW code file over the limit fails.
  * An existing file already over the limit (legacy) may shrink or stay put, never grow.
  * Files matching a glob in `.file-size-allow` (repo root) are exempt. Every line
    there needs a `# reason` so the exemption is visible in review.

The baseline is git itself (the file as it was at the base ref), so there is no
baseline file to maintain or game.

Usage (vendored into each repo as scripts/check_file_size.py):
  python3 scripts/check_file_size.py               # changes vs merge-base with origin/main
  python3 scripts/check_file_size.py --base REF    # changes vs REF (CI: the PR base sha)
  python3 scripts/check_file_size.py --staged      # pre-commit: staged vs HEAD
  python3 scripts/check_file_size.py --report      # list every code file over the limit
Exit 1 on violation. Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

MAX_LINES = int(os.environ.get("FILE_SIZE_MAX_LINES", "1000"))

CODE_EXTS = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".go",
    ".sh", ".bash", ".zsh", ".ps1", ".swift", ".kt", ".java", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".vue", ".svelte", ".lua", ".dart",
}
# Generated, vendored or minified paths are never authored by hand.
DEFAULT_EXCLUDES = (
    "*/node_modules/*", "node_modules/*", "*/dist/*", "dist/*", "*/build/*", "build/*",
    "*/vendor/*", "vendor/*", "*/.venv/*", ".venv/*", "*/target/*", "target/*",
    "*.min.js", "*.bundle.js", "*_pb2.py", "*.generated.*", "*/generated/*",
)
ALLOW_FILE = ".file-size-allow"


def is_code(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXTS


def load_allow(root: Path, extra: Path | None = None) -> list[str]:
    globs = list(DEFAULT_EXCLUDES)
    for f in (root / ALLOW_FILE, extra):
        if f and f.is_file():
            for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.split("#", 1)[0].strip()
                if line:
                    globs.append(line)
    return globs


def allowed(rel: str, globs: list[str], abs_path: str | None = None) -> bool:
    rel = rel.replace("\\", "/")
    for g in globs:
        if fnmatch.fnmatch(rel, g) or (abs_path and fnmatch.fnmatch(abs_path, g)):
            return True
    return False


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def lines_at(root: Path, ref: str | None, rel: str) -> int | None:
    """Line count of rel at ref (None = file absent there). ref '' = index."""
    spec = f":{rel}" if ref == "" else f"{ref}:{rel}"
    r = subprocess.run(["git", "-C", str(root), "show", spec], capture_output=True)
    if r.returncode != 0:
        return None
    return count_lines(r.stdout.decode("utf-8", errors="replace"))


def verdict(rel: str, new: int, old: int | None, limit: int = MAX_LINES) -> str | None:
    """Return a violation message, or None when the change is fine."""
    if new <= limit:
        return None
    if old is None:
        return f"{rel}: new file has {new} lines (limit {limit}). Split it into modules."
    if old <= limit:
        return (f"{rel}: would grow {old} -> {new} lines, past the {limit}-line limit. "
                f"Put the new code in a new module.")
    if new > old:
        return (f"{rel}: grew {old} -> {new} lines; it is already over the {limit}-line limit, "
                f"so it may only shrink. Put the new code in a new module.")
    return None


def default_base(root: Path) -> str:
    for cand in ("origin/main", "origin/master", "main", "master"):
        r = subprocess.run(["git", "-C", str(root), "merge-base", "HEAD", cand],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return "HEAD"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", help="git ref to compare against")
    ap.add_argument("--staged", action="store_true", help="check staged changes vs HEAD")
    ap.add_argument("--report", action="store_true", help="list all code files over the limit")
    ap.add_argument("--max", type=int, default=MAX_LINES)
    a = ap.parse_args(argv)
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    globs = load_allow(root)

    if a.report:
        over = []
        for rel in git(root, "ls-files").splitlines():
            if is_code(rel) and not allowed(rel, globs):
                p = root / rel
                if p.is_file():
                    n = count_lines(p.read_text(encoding="utf-8", errors="replace"))
                    if n > a.max:
                        over.append((n, rel))
        for n, rel in sorted(over, reverse=True):
            print(f"{n:>7}  {rel}")
        print(f"{len(over)} code file(s) over {a.max} lines (legacy: may shrink, never grow).")
        return 0

    if a.staged:
        names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
        base, head = "HEAD", ""
    else:
        base = a.base or default_base(root)
        names = git(root, "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD").splitlines()
        head = "HEAD"

    bad = []
    for rel in names:
        if not is_code(rel) or allowed(rel, globs):
            continue
        new = lines_at(root, head, rel)
        if new is None:
            continue
        msg = verdict(rel, new, lines_at(root, base, rel), a.max)
        if msg:
            bad.append(msg)
    for m in bad:
        print(f"FILE-SIZE FAIL: {m}", file=sys.stderr)
    if bad:
        print(f"{len(bad)} file(s) break the {a.max}-line rule. Exemptions go in {ALLOW_FILE} "
              f"with a # reason.", file=sys.stderr)
        return 1
    print(f"file-size: OK ({len(names)} changed file(s) checked, limit {a.max}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
