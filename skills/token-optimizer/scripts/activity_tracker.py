"""Token Optimizer v5.6 - Activity Mode Tracker.

Classifies session activity into modes (code/debug/review/infra/general) using
a sliding window of the last 10 tool calls. Mode labels feed into dynamic compact
instructions for mode-aware PRESERVE/DROP decisions.

Called from context_intel.py PostToolUse handler. Stores activity log and current
mode in the per-session SQLite store.
"""

from __future__ import annotations

import re
from typing import Optional

_WINDOW_SIZE = 10

_INFRA_BASH_RE = re.compile(
    r"\b(?:systemctl|nginx|docker|kubectl|service|daemon|launchctl"
    r"|brew|apt|apt-get|yum|dnf|pacman)\b"
)
_GIT_WRITE_RE = re.compile(r"\bgit\s+(?:push|pull|merge|rebase|cherry-pick|tag)\b")
_INSTALL_RE = re.compile(
    r"\b(?:pip|npm|pnpm|yarn|bun|cargo|go)\s+(?:install|add|update|upgrade)\b"
)

# Tool name to bucket mapping
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
_AGENT_TOOLS = frozenset({"Agent", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"})


def classify_tool(tool_name: str, command: str = "") -> str:
    """Classify a tool call into a bucket for mode detection."""
    if tool_name in _EDIT_TOOLS:
        return "edit"
    if tool_name in _READ_TOOLS:
        return "read"
    if tool_name in _AGENT_TOOLS:
        return "agent"
    if tool_name.startswith("mcp__"):
        return "mcp"
    if tool_name == "Bash":
        if _INFRA_BASH_RE.search(command):
            return "bash_infra"
        if _GIT_WRITE_RE.search(command):
            return "bash_git"
        if _INSTALL_RE.search(command):
            return "bash_install"
        return "bash_other"
    if tool_name == "WebSearch" or tool_name == "WebFetch":
        return "web"
    return "other"


def detect_mode(recent_buckets: list[str], has_recent_errors: bool = False) -> str:
    """Determine session mode from the last N tool buckets.

    Modes:
      code   - edit-heavy (4+ edits in window)
      debug  - error signals present + read-heavy
      review - read/grep-heavy, no edits
      infra  - bash infra/git/install heavy (3+ infra-class)
      general - default when no pattern dominates
    """
    if len(recent_buckets) < 3:
        return "general"

    edit_count = recent_buckets.count("edit")
    read_count = recent_buckets.count("read")
    infra_count = sum(1 for b in recent_buckets if b in ("bash_infra", "bash_git", "bash_install"))
    web_count = recent_buckets.count("web")
    bash_other = recent_buckets.count("bash_other")

    if infra_count >= 3:
        return "infra"
    if has_recent_errors and read_count >= 3 and edit_count <= 1:
        return "debug"
    if edit_count >= 4:
        return "code"
    if read_count >= 4 and edit_count == 0:
        return "review"
    if web_count >= 3:
        return "review"
    if edit_count >= 2 and (bash_other >= 2 or read_count >= 2):
        return "code"

    return "general"


_PRUNE_THRESHOLD = 30  # prune when table exceeds this many rows
_PRUNE_KEEP = 20  # keep the most recent N rows after pruning


def log_tool_use(store, tool_name: str, command: str = "", has_error: bool = False) -> Optional[str]:
    """Log a tool use to the activity tracker and return the current mode.

    Args:
        store: SessionStore instance (already connected)
        tool_name: Name of the tool used
        command: For Bash tools, the command string
        has_error: Whether the tool result contained error signals

    Returns:
        Current detected mode string, or None on failure
    """
    try:
        import time as _time
        bucket = classify_tool(tool_name, command)
        conn = store._connect()

        conn.execute(
            "INSERT INTO activity_log (tool_name, tool_bucket, has_error, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (tool_name[:64], bucket, 1 if has_error else 0, _time.time()),
        )

        rows = conn.execute(
            "SELECT tool_bucket, has_error FROM activity_log "
            "ORDER BY id DESC LIMIT ?",
            (_WINDOW_SIZE,),
        ).fetchall()

        row_count = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        if row_count > _PRUNE_THRESHOLD:
            conn.execute(
                "DELETE FROM activity_log WHERE id NOT IN "
                "(SELECT id FROM activity_log ORDER BY id DESC LIMIT ?)",
                (_PRUNE_KEEP,),
            )

        recent_buckets = [r[0] for r in rows]
        has_recent_errors = any(r[1] for r in rows)
        mode = detect_mode(recent_buckets, has_recent_errors)

        conn.execute(
            "INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, ?)",
            ("current_mode", mode),
        )
        conn.commit()
        return mode
    except Exception:
        try:
            store._connect().rollback()
        except Exception:
            pass
        return None
