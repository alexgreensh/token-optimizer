#!/usr/bin/env python3
"""Discovery hint for the code map (SessionStart and SubagentStart).

Tells the agent, in about 80 tokens, to ask the code map before grepping or
reading whole files for structural questions, and warms the index in a
detached background process so the first lookup is fast. Silent (no output,
no spawn) when the feature is off, when the cwd is the home directory or the
filesystem root, or when the project has no Python/JS/TS files.

Run as a hook: ``codemap_hint.py`` reads the hook payload on stdin and prints
SubagentStart JSON (additionalContext). SessionStart uses ``session_text()``
from hooks/sessionstart_runner.py, which wraps plain text itself.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROBE_MAX_ENTRIES = 400
PROBE_MAX_S = 0.25
LOCK_FRESH_S = 300.0
# Subagents that never explore code: no hint for them.
SKIP_AGENT_TYPES = frozenset({"statusline-setup", "claude-code-guide", "web-fetch"})


def _has_code(root: Path) -> bool:
    import codemap_index as ci
    seen = 0
    deadline = time.monotonic() + PROBE_MAX_S
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ci.SKIP_DIRS and not d.startswith(".")]
        seen += 1 + len(dirnames)
        for fn in filenames:
            seen += 1
            if Path(fn).suffix.lower() in ci.SUPPORTED_SUFFIXES:
                return True
        if seen > PROBE_MAX_ENTRIES or time.monotonic() > deadline:
            return False
    return False


def _project_root(cwd: str) -> Optional[Path]:
    import codemap_index as ci
    if not cwd:
        return None  # hosts always send cwd; never guess (and never index) an unknown dir
    try:
        p = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    if p == Path.home().resolve() or p.parent == p or not p.is_dir():
        return None
    root = ci.find_project_root(p)
    return root if _has_code(root) else None


def warm_index(root: Path) -> bool:
    """Start a detached, niced index build unless one is fresh or running."""
    import codemap_index as ci
    db = ci.index_path(root)
    lock = db.with_suffix(".building")
    try:
        if db.exists():
            import sqlite3
            conn = sqlite3.connect(str(db), timeout=1.0)
            try:
                row = conn.execute("SELECT v FROM meta WHERE k='complete'").fetchone()
            finally:
                conn.close()
            if row and row[0] == "1":
                return False  # each lookup syncs incrementally; nothing to warm
        if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_FRESH_S:
            return False
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()), encoding="utf-8")
        from spawn_utils import spawn_detached
        script = str(Path(__file__).resolve().parent / "codemap.py")
        proc = spawn_detached([sys.executable, script, "refresh", "--root", str(root),
                               "--budget", "120", "--quiet", "--background"])
        return proc is not None
    except Exception:
        return False


def hint_text(root: Path) -> str:
    script = shlex.quote(str(Path(__file__).resolve().parent / "codemap.py"))
    py = shlex.quote(sys.executable or "python3")
    return (
        "[Token Optimizer] Code map available for this project (Python/JS/TS). For \"where is X "
        "defined\", \"what calls Y\" or \"what does this file import\", run it BEFORE Grep, grep/rg in "
        f"Bash, or reading whole files: `{py} {script} find|show|callers <symbol>` or `outline|deps <file>`. "
        "`show` prints one definition with Edit-ready line numbers; answers are small and match the files on disk."
    )


def session_text(hook_input: dict) -> str:
    """Plain hint text for SessionStart ('' when off or not a code project)."""
    try:
        import codemap_index as ci
        if not ci.codemap_enabled():
            return ""
        root = _project_root(str(hook_input.get("cwd") or ""))
        if root is None:
            return ""
        warm_index(root)
        return hint_text(root)
    except Exception:
        return ""


def subagent_payload(hook_input: dict) -> Optional[dict]:
    agent_type = str(hook_input.get("agent_type") or "")
    if agent_type in SKIP_AGENT_TYPES:
        return None
    text = session_text(hook_input)
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}}


def main() -> int:
    try:
        from hook_io import read_stdin_hook_input
        hook_input = read_stdin_hook_input() or {}
    except Exception:
        try:
            hook_input = json.loads(sys.stdin.read() or "{}")
        except Exception:
            hook_input = {}
    if not isinstance(hook_input, dict):
        return 0
    payload = subagent_payload(hook_input)
    if payload:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
