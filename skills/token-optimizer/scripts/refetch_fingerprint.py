#!/usr/bin/env python3
"""Shared re-fetch fingerprint for Token Optimizer (issue #88 self-healing).

SINGLE SOURCE OF TRUTH for the archive de-duplication fingerprint. Imported by
archive_result.py (writes the fingerprint into the archive manifest at
PostToolUse) and refetch_guard.py (recomputes it at PreToolUse to detect an
identical re-fetch). Keeping the writer and reader on one function prevents the
two from drifting apart — the class of gap that let #88 regress.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# SINGLE SOURCE OF TRUTH for the manifest field name the writer stores and the
# guard matches on. Referencing this constant from both sides means a rename is a
# one-line change that can't silently break the match (the exact #88 drift mode:
# a renamed field makes `entry.get("args_hash")` return None and the guard stops
# denying with zero error).
ARGS_HASH_KEY = "args_hash"


def tool_fingerprint(tool_name: str, tool_input) -> str:
    """Stable 16-hex fingerprint of an MCP tool call (name + normalized args).

    Two calls to the same tool with identical arguments produce the same
    fingerprint, so an exact re-fetch is detectable. Dict-key-order insensitive;
    never raises (falls back to repr for exotic, non-JSON-serializable inputs).
    """
    try:
        args = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args = repr(tool_input)
    raw = f"{tool_name}\x00{args}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def measure_py_path() -> str:
    """Absolute path to measure.py (this module ships in the same scripts dir).

    Shared by the archive footer and the guard's deny reason so the `expand`
    command they print can never diverge. Correct at any install layout because
    it resolves from this file's own location.
    """
    return str(Path(__file__).resolve().parent / "measure.py")


def expand_command(key: str) -> str:
    """The exact Bash command that retrieves an archived result — one source of
    truth for both the archive footer and the guard's deny reason."""
    return f"python3 {measure_py_path()} expand {key}"
