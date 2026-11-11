"""Count in-window transcripts too large for the session parsers to read.

The parsers skip any transcript over codex_session.MAX_PARSE_FILE_BYTES, so
savings inside those sessions (cache drops included) are understated. The
count lets the dashboard say so. Read-only; never raises.
"""
from __future__ import annotations

import time
from pathlib import Path


def count(claude_dir, days=30, now=None, limit=None):
    """{"unscanned_large_sessions": n} for top-level session transcripts modified in the window."""
    try:
        if limit is None:
            from codex_session import MAX_PARSE_FILE_BYTES as limit
        cutoff = (now or time.time()) - max(1, int(days or 30)) * 86400
        n = 0
        for path in Path(claude_dir, "projects").glob("*/*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff and st.st_size > limit:
                n += 1
        return {"unscanned_large_sessions": n}
    except Exception:
        return {"unscanned_large_sessions": 0}
