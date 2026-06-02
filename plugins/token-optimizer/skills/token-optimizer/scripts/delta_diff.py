#!/usr/bin/env python3
"""Delta diff computation for Token Optimizer v5 read cache.

Computes compact unified diffs between old and new file versions.
Imported by read_cache.py -- must stay lightweight (no heavy imports).

Security:
- Paths validated against project root boundary (os.path.commonpath)
- Binary files excluded by extension allowlist
- Output hard-capped at MAX_DELTA_CHARS
"""

import difflib
import hashlib
from pathlib import Path

MAX_DELTA_CHARS = 1500  # Must not exceed MAX_ADDITIONAL_CONTEXT_CHARS
MAX_DELTA_LINES = 2000  # Skip difflib if either file exceeds this (O(n^2) guard)
MAX_CONTENT_CACHE_BYTES = 50 * 1024  # 50KB per cached file content

# Code file extensions eligible for delta mode
CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".rs", ".go",
    ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".yaml", ".yml", ".toml", ".json", ".xml", ".html", ".css",
    ".scss", ".less", ".sql", ".md", ".txt", ".cfg", ".ini",
    ".vue", ".svelte", ".astro", ".ex", ".exs", ".erl", ".hs",
    ".lua", ".r", ".R", ".jl", ".dart", ".scala", ".clj",
    ".tf", ".hcl", ".dockerfile", ".makefile",
})


def is_delta_eligible(file_path):
    """Check if a file is eligible for delta mode (code file, not binary)."""
    p = Path(file_path)
    ext = p.suffix.lower()
    name = p.name.lower()

    # Special names without extensions
    # .env excluded: credentials files must never be cached in plaintext (SEC-F8)
    if name in ("makefile", "dockerfile", "gemfile", "rakefile", "procfile",
                "jenkinsfile", ".gitignore", ".dockerignore"):
        return True
    # Explicitly exclude credential files even if extension matches
    if name.startswith(".env"):
        return False

    return ext in CODE_EXTENSIONS


def content_hash(text):
    """Compute SHA-256 hash of text content."""
    if isinstance(text, str):
        text = text.encode("utf-8", errors="replace")
    return hashlib.sha256(text).hexdigest()


def compute_delta(old_content, new_content, filename="file"):
    """Compute a compact unified diff between old and new content.

    Returns (delta_text, stats_dict) or (None, None) if delta is not viable.
    stats_dict has keys: added, removed, changed_lines.

    Returns None if:
    - Either file exceeds MAX_DELTA_LINES (O(n^2) guard)
    - Diff output exceeds MAX_DELTA_CHARS
    - Content is identical
    """
    if old_content == new_content:
        return None, None

    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    # O(n^2) guard: skip difflib for large files
    if len(old_lines) > MAX_DELTA_LINES or len(new_lines) > MAX_DELTA_LINES:
        return None, None

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=1,  # 1 line of context (compact)
    ))

    if not diff_lines:
        return None, None

    # Count changes
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

    # Build compact output
    header = f"{filename}: {added + removed} lines changed (+{added}/-{removed})"
    diff_body = "".join(diff_lines)
    delta_text = f"{header}\n{diff_body}"

    # Hard cap
    if len(delta_text) > MAX_DELTA_CHARS:
        # Diff too large for additionalContext -- fall through to full re-read
        return None, None

    stats = {
        "added": added,
        "removed": removed,
        "changed_lines": added + removed,
    }

    return delta_text, stats
