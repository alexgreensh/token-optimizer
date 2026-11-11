"""Re-inject lines a compressor must not lose (used by bash_compress.compress).

Error and warning lines come first and are never capped or evicted. The other
preserved lines (credential-bearing, distinctive tokens) follow in input order
and are capped, so a needle-dense output still compresses. Deterministic: the
order depends only on line positions, never on set iteration.
"""
from __future__ import annotations

import re

# Warning lines are signal the model must see, like error lines
# ("warning:", "DeprecationWarning", "1 warning in 2s").
WARNING_LINE_RE = re.compile(r"(?i)\bwarn(?:ing)?s?\b|\b[A-Za-z]+Warning\b")


def find_priority_lines(text, error_patterns):
    """Indices of error and warning lines."""
    out = set()
    for i, line in enumerate(text.splitlines()):
        if WARNING_LINE_RE.search(line) or any(p.search(line) for p in error_patterns):
            out.add(i)
    return out


def reinject(compressed, cleaned, preserved_lines, priority_lines, summarizer_pattern):
    """Return ``compressed`` with missing preserved lines appended."""
    cap = max(1, min(20, len(compressed.splitlines()) // 5))
    original_lines = cleaned.splitlines()
    present = set(compressed.splitlines())
    appended = []
    injected = 0
    skipped_other = 0
    for idx in sorted(priority_lines) + sorted(set(preserved_lines) - set(priority_lines)):
        if idx >= len(original_lines):
            continue
        line = original_lines[idx]
        if line in present:
            continue
        if idx not in priority_lines:
            if summarizer_pattern and line.strip() in compressed:
                continue  # already carried by a summary line
            if injected >= cap:
                skipped_other += 1
                continue
            injected += 1
        appended.append(line)
        present.add(line)
    if not appended:
        return compressed
    if skipped_other:
        appended.append(f"... {skipped_other} more credential-bearing lines "
                        f"archived in full original (retrieve via expand key) ...")
    return compressed + "\n" + "\n".join(appended)
