"""Shared I/O utilities for Token Optimizer hook scripts.

Single source of truth for reading JSON hook input from stdin.
Each hook script runs as a separate subprocess; this module
provides a consistent, bounded stdin reader across all of them.
"""

from __future__ import annotations

import json
import sys


def read_stdin_hook_input(max_bytes: int = 1_048_576) -> dict:
    """Read JSON hook input from stdin non-blocking.

    Returns parsed dict or empty dict on failure.
    Bounds read size to max_bytes (default 1MB for PostToolUse payloads
    that include tool_response). PreToolUse callers can pass a lower cap.
    Works on Unix; returns empty dict on Windows where select() doesn't
    support file descriptors.
    """
    try:
        import select
        if select.select([sys.stdin], [], [], 0.1)[0]:
            if max_bytes is not None and max_bytes <= 0:
                return {}
            data = sys.stdin.read(max_bytes)
            return json.loads(data) if data else {}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}
