"""Disclose large transcripts whose resumable scan is unfinished or failed."""
from __future__ import annotations

import time
from pathlib import Path


def count(claude_dir, days=30, now=None, limit=None):
    """Unfinished large transcripts in the active runtime's session roots."""
    try:
        if limit is None:
            from codex_session import MAX_PARSE_FILE_BYTES as limit
        cutoff = (now or time.time()) - max(1, int(days or 30)) * 86400
        n = 0
        from runtime_env import detect_runtime
        from _measure.transcript_stream import pending
        paths = Path(claude_dir, "projects").glob("*/*.jsonl")
        if detect_runtime() == 'codex':
            from codex_session import session_roots
            from codex_log_index import pending
            paths = (path for root in session_roots() for path in root.rglob('*.jsonl'))
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff and st.st_size > limit:
                n += int(pending(path))
        return {"unscanned_large_sessions": n}
    except Exception:
        return {"unscanned_large_sessions": 0}
