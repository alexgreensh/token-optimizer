"""Live-session activity from Claude Code's ``sessions/<pid>.json`` records.

Process age cannot tell a busy orchestrator apart from an abandoned one;
the record's ``status`` and timestamps can. Claude Code writes one JSON
record per live session under ``$CLAUDE_CONFIG_DIR/sessions/`` (default
``~/.claude/sessions/``) and the transcript under
``projects/<encoded-cwd>/<sessionId>.jsonl``.

Everything fails open: a missing or unreadable record returns
``source="none"`` so callers keep their elapsed-time behavior.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# Process start (ps elapsed, whole seconds) vs the record startedAt.
START_TOLERANCE_SECONDS = 10


def _config_dir(config_dir=None):
    """Claude Code's config dir: explicit override, else claude_home() ($CLAUDE_CONFIG_DIR or ~/.claude)."""
    if config_dir is not None:
        return Path(config_dir)
    from runtime_env import claude_home
    return claude_home()


def _ms(value):
    """Epoch-milliseconds field to epoch seconds, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) / 1000.0
    except (OverflowError, ValueError):
        return None


def _project_dir_name(cwd):
    """Claude Code's projects/ encoding: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _transcript_mtime(config_dir, record):
    """mtime of the session's transcript JSONL, or None when it cannot be located."""
    session_id = record.get("sessionId")
    cwd = record.get("cwd")
    if not (isinstance(session_id, str) and session_id
            and isinstance(cwd, str) and cwd):
        return None
    path = Path(config_dir) / "projects" / _project_dir_name(cwd) / f"{session_id}.jsonl"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def activity(pid, now=None, config_dir=None, elapsed_seconds=None):
    """Activity snapshot for one live session pid.

    Returns ``{"status", "last_active_ts", "idle_seconds", "tmux", "source"}``:
    ``status`` is the record's busy/idle (None when unknown),
    ``last_active_ts`` the newest of ``statusUpdatedAt``/``updatedAt`` and the
    transcript mtime in epoch seconds (None when unknowable),
    ``idle_seconds`` ``now - last_active_ts`` clamped at 0 (None when
    unknowable), ``tmux`` the orchestrator's tmux target when the record has
    one, and ``source`` "record" or "none".
    """
    if now is None:
        now = time.time()
    def _none(reason):
        return {"status": None, "last_active_ts": None, "idle_seconds": None,
                "tmux": None, "source": "none", "reason": reason, "verified": False}

    try:
        base = _config_dir(config_dir)
        path = base / "sessions" / f"{pid}.json"
    except Exception:
        # Resolve again with plain strings. "missing" (old behavior allowed)
        # only when that proves no record exists; any doubt is "unresolved",
        # which kill-stale never acts on.
        try:
            root = str(config_dir) if config_dir is not None else (
                os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"))
            if os.path.exists(os.path.join(root, "sessions", f"{pid}.json")):
                return _none("unreadable")
            return _none("missing")
        except Exception:
            return _none("unresolved")
    if not path.exists():
        return _none("missing")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _none("unreadable")
    if not isinstance(record, dict):
        return _none("unreadable")
    # A record left by a dead session whose pid was reused describes another
    # process: it counts only when its pid and start time match the live one.
    if record.get("pid") not in (None, pid):
        return _none("pid_mismatch")
    started = _ms(record.get("startedAt"))
    lifetime = float(elapsed_seconds) if isinstance(elapsed_seconds, (int, float)) and elapsed_seconds >= 0 else None
    if started is not None and lifetime is not None and abs((now - lifetime) - started) > START_TOLERANCE_SECONDS:
        return _none("start_mismatch")
    verified = record.get("pid") == pid and started is not None and lifetime is not None

    stamps = [ts for ts in (_ms(record.get("statusUpdatedAt")),
                            _ms(record.get("updatedAt")),
                            _transcript_mtime(base, record)) if ts is not None]
    last_active = max(stamps) if stamps else None
    idle = max(0.0, now - last_active) if last_active is not None else None
    if idle is not None and lifetime is not None:
        idle = min(idle, lifetime)  # never idle longer than the process has lived
    status = record.get("status")
    tmux = record.get("tmux")
    return {
        "status": status if status in ("busy", "idle") else None,
        "last_active_ts": last_active,
        "idle_seconds": int(idle) if idle is not None else None,
        "tmux": tmux if isinstance(tmux, str) and tmux else None,
        "source": "record",
        "reason": "ok",
        "verified": verified,
    }
