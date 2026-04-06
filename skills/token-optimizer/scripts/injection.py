"""Token Optimizer - CLAUDE.md managed block injection.

Injects and removes auto-generated advice blocks between managed markers.
Blocks are bounded by <!-- TOKEN_OPTIMIZER:{SECTION} --> markers and include
a 48h staleness TTL to prevent serving zombie advice.
"""

import os
import re
from datetime import datetime
from pathlib import Path


def _write_atomic(filepath, content):
    """Atomic write via tmp + rename."""
    filepath = Path(filepath)
    tmp = filepath.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, filepath)


# Marker format: <!-- TOKEN_OPTIMIZER:SECTION_NAME --> ... <!-- /TOKEN_OPTIMIZER:SECTION_NAME -->
_MARKER_OPEN = "<!-- TOKEN_OPTIMIZER:{section} -->"
_MARKER_CLOSE = "<!-- /TOKEN_OPTIMIZER:{section} -->"
_TIMESTAMP_PATTERN = re.compile(r"updated (\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")

DEFAULT_MAX_AGE_HOURS = 48


def inject_managed_block(filepath, section, content, dry_run=False):
    """Insert or replace a managed block in a file.

    Args:
        filepath: Path to the target file (e.g., CLAUDE.md)
        section: Block identifier (e.g., "MODEL_ROUTING", "COACH")
        content: Markdown content to inject (without markers)
        dry_run: If True, return the diff without writing

    Returns:
        dict with: action ("inserted"|"replaced"|"unchanged"), diff (str), filepath
    """
    filepath = Path(filepath)
    marker_open = _MARKER_OPEN.format(section=section)
    marker_close = _MARKER_CLOSE.format(section=section)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # Build the full block with markers and timestamp
    block = f"{marker_open}\n{content}\n<!-- updated {timestamp} -->\n{marker_close}"

    if not filepath.exists():
        if dry_run:
            return {"action": "inserted", "diff": f"+++ {block}", "filepath": str(filepath)}
        filepath.write_text(block + "\n", encoding="utf-8")
        return {"action": "inserted", "diff": f"+++ {block}", "filepath": str(filepath)}

    original = filepath.read_text(encoding="utf-8")

    # Check if block already exists
    pattern = re.compile(
        re.escape(marker_open) + r".*?" + re.escape(marker_close),
        re.DOTALL,
    )
    match = pattern.search(original)

    if match:
        old_block = match.group(0)
        if old_block.strip() == block.strip():
            return {"action": "unchanged", "diff": "", "filepath": str(filepath)}
        new_content = original[:match.start()] + block + original[match.end():]
        action = "replaced"
        diff = f"--- {old_block}\n+++ {block}"
    else:
        # Append before the last line (or at end)
        new_content = original.rstrip("\n") + "\n\n" + block + "\n"
        action = "inserted"
        diff = f"+++ {block}"

    if dry_run:
        return {"action": action, "diff": diff, "filepath": str(filepath)}

    _write_atomic(filepath, new_content)
    return {"action": action, "diff": diff, "filepath": str(filepath)}


def remove_managed_block(filepath, section, dry_run=False):
    """Remove a managed block from a file.

    Returns:
        dict with: action ("removed"|"not_found"), filepath
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return {"action": "not_found", "filepath": str(filepath)}

    original = filepath.read_text(encoding="utf-8")
    marker_open = _MARKER_OPEN.format(section=section)
    marker_close = _MARKER_CLOSE.format(section=section)

    pattern = re.compile(
        r"\n?" + re.escape(marker_open) + r".*?" + re.escape(marker_close) + r"\n?",
        re.DOTALL,
    )
    match = pattern.search(original)
    if not match:
        return {"action": "not_found", "filepath": str(filepath)}

    new_content = original[:match.start()] + original[match.end():]
    # Clean up double blank lines left behind
    new_content = re.sub(r"\n{3,}", "\n\n", new_content)

    if dry_run:
        return {"action": "removed", "filepath": str(filepath)}

    _write_atomic(filepath, new_content)
    return {"action": "removed", "filepath": str(filepath)}


def check_staleness(filepath, section, max_age_hours=DEFAULT_MAX_AGE_HOURS):
    """Check if a managed block is stale (older than max_age_hours).

    Returns:
        dict with: stale (bool), age_hours (float|None), exists (bool)
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return {"stale": False, "age_hours": None, "exists": False}

    content = filepath.read_text(encoding="utf-8")
    marker_open = _MARKER_OPEN.format(section=section)
    marker_close = _MARKER_CLOSE.format(section=section)

    pattern = re.compile(
        re.escape(marker_open) + r".*?" + re.escape(marker_close),
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        return {"stale": False, "age_hours": None, "exists": False}

    # Extract timestamp from block
    block_text = match.group(0)
    ts_match = _TIMESTAMP_PATTERN.search(block_text)
    if not ts_match:
        # No timestamp found, treat as stale
        return {"stale": True, "age_hours": None, "exists": True}

    try:
        block_time = datetime.fromisoformat(ts_match.group(1))
        age_hours = (datetime.now() - block_time).total_seconds() / 3600
        return {
            "stale": age_hours > max_age_hours,
            "age_hours": round(age_hours, 1),
            "exists": True,
        }
    except (ValueError, TypeError):
        return {"stale": True, "age_hours": None, "exists": True}
