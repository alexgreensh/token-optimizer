#!/usr/bin/env python3
"""Token Optimizer - control-group (holdout) assignment. OFF unless asked.

``TOKEN_OPTIMIZER_HOLDOUT=<share>`` (0 to 1, default 0) puts that share of
sessions in a control arm. A control session runs every wired hook in shadow:
the decision is logged at the opportunity tier and nothing the model sees is
replaced. This is the only defensible way to put a dollar figure on features
that cannot be A/B tested per item (loop detection, checkpoints, nudges), and
it costs the user the savings in control sessions, so it ships built and off.

Rules:
  * Unset, empty, zero, negative, non-numeric, above 1 or NaN all mean OFF.
    A bad value never turns the experiment on.
  * Assignment is deterministic from the session id (sha256 bucket), so a
    session lands in the same arm in every hook process.
  * When the share is 0 nothing is read or written: no database, no files.
  * When on, the first assignment is stored (``holdout_sessions``) and the
    stored arm wins afterwards, so changing the share mid-session cannot move
    a session between arms.
  * Sessions with no usable id are never assigned (treated as treatment).

Hosts wired today: the Bash PostToolUse hook. Other hooks adopt the same single
call, ``arm(session_id)``, and must replace nothing when it returns "control".
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ENV = "TOKEN_OPTIMIZER_HOLDOUT"
OFF, CONTROL, TREATMENT = "off", "control", "treatment"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS holdout_sessions (
    session_uuid TEXT PRIMARY KEY,
    arm TEXT NOT NULL,
    share REAL NOT NULL,
    assigned_at TEXT NOT NULL
);
"""
_UUID_PAT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def share(env=None) -> float:
    """The configured control share, or 0.0 (off) for anything unusable."""
    raw = (env if env is not None else os.environ).get(ENV, "")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0.0 or value > 1.0:
        return 0.0
    return value


def bucket(session_id: str) -> float:
    """Stable position in [0, 1) for a session id."""
    digest = hashlib.sha256(("holdout:" + session_id).encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def decide(session_id, share_value=None) -> str:
    """Pure arm decision for a share: ``control``, ``treatment`` or ``off``."""
    s = share() if share_value is None else share_value
    if not s or s <= 0.0:
        return OFF
    if not isinstance(session_id, str) or not session_id.strip() or session_id == "unknown":
        return TREATMENT
    return CONTROL if bucket(session_id) < s else TREATMENT


def arm(session_id, snapshot_dir=None, env=None) -> str:
    """The arm this session is in. Stored on first sight when the holdout is on.

    Returns ``off`` without touching disk when the share is 0. Fail-open: a
    ledger that cannot be read or written falls back to the pure decision.
    """
    s = share(env)
    if s <= 0.0:
        return OFF
    decided = decide(session_id, s)
    uuid = session_id if isinstance(session_id, str) and _UUID_PAT.match(session_id) else None
    if uuid is None:
        return decided
    conn = None
    try:
        if snapshot_dir is None:
            from plugin_env import resolve_snapshot_dir
            snapshot_dir = resolve_snapshot_dir()
        directory = Path(snapshot_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(str(directory / "trends.db"), timeout=0.05)
        conn.execute("PRAGMA busy_timeout=50")
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT arm FROM holdout_sessions WHERE session_uuid = ?", (uuid,)).fetchone()
        if row and row[0] in (CONTROL, TREATMENT):
            return row[0]
        conn.execute("INSERT OR IGNORE INTO holdout_sessions (session_uuid, arm, share, assigned_at) "
                     "VALUES (?, ?, ?, ?)", (uuid, decided, s, datetime.now().isoformat()))
        conn.commit()
        row = conn.execute("SELECT arm FROM holdout_sessions WHERE session_uuid = ?", (uuid,)).fetchone()
        return row[0] if row and row[0] in (CONTROL, TREATMENT) else decided
    except Exception:
        return decided
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def is_control(session_id, snapshot_dir=None, env=None) -> bool:
    return arm(session_id, snapshot_dir, env) == CONTROL
