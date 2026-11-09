#!/usr/bin/env python3
"""Read-only reader for Cursor session data (token-optimizer tally + Cursor data planes).

Cursor exposes two on-disk data planes, neither of which carries reliable,
authoritative per-session token totals:

- Hook tally — Token Optimizer's own record under
  ``~/.cursor/token-optimizer/sessions/<conversation_id>.json``. This is the
  primary record (tool calls, turns, compaction context numbers from
  ``preCompact``, cursor_version, transcript_path). The hook bridge writes it;
  this module reads it back.
- ``state.vscdb`` (IDE chats) — a SQLite DB at
  ``<cursor-home>/User/globalStorage/state.vscdb`` whose ``cursorDiskKV`` table
  stores ``bubbleId:<composerId>:<bubbleId>`` rows with a ``tokenCount`` field
  that Cursor staff call best-effort (timing-dependent; often zero). Read-only,
  key-prefix queries only, opened ``mode=ro`` (NOT ``immutable=1`` — a live IDE
  DB must not gain ``-shm`` side effects from an immutable-URI mismatch).
- Transcripts (CLI chats) — ``~/.cursor/projects/<slug>/agent-transcripts/
  <conversation_id>/*.jsonl``. No usage fields; only a chars-over-four size
  estimate is available.

Design constraints (mirroring copilot_state.py / hermes_state.py):

- **Pure stdlib only.** sqlite3 is OK; no Cursor imports.
- **Strictly read-only.** Never writes any file.
- **No hardcoded user paths.** The Cursor home comes from
  ``runtime_env.cursor_home()``; callers may pass an explicit ``home``
  override.
- **Schema-defensive.** Tally JSON and the community-reverse-engineered
  state.vscdb schema both drift; every reader degrades to a safe default and
  never raises.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TO_DIR = "token-optimizer"
_SESSIONS_SUBDIR = "sessions"

# Idle finalisation threshold (R17/KTD11): a conversation whose last activity
# is older than this and that never saw sessionEnd is finalised as idle.
_IDLE_SECS = 2 * 3600

# Bounded reads: a maliciously-crafted or pathological file must never stall a
# detached rollup. These caps match the streaming-reader spirit of
# copilot_state.py without pulling in its event-stream machinery.
_MAX_TALLY_BYTES = 4 * 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
_MAX_BUBBLE_ROWS = 2000
_MAX_COMPOSERS = 500


def _safe_home() -> Path:
    try:
        return Path.home()
    except (OSError, RuntimeError):
        for key in ("USERPROFILE", "HOME"):
            val = os.environ.get(key, "").strip()
            if val:
                return Path(val)
        return Path(".")


# ---------------------------------------------------------------------------
# Tally readers
# ---------------------------------------------------------------------------


def find_tallies(home: Optional[Path] = None) -> list:
    """Return tally JSON files under <home>/token-optimizer/sessions/.

    Returns [] when the directory does not exist. Never raises.
    """
    if home is None:
        try:
            from runtime_env import cursor_home

            home = cursor_home()
        except Exception as exc:
            logger.debug("[cursor_state] cannot import runtime_env: %s", exc)
            return []
    sessions_dir = home / _TO_DIR / _SESSIONS_SUBDIR
    if not sessions_dir.is_dir():
        return []
    try:
        return sorted(
            p for p in sessions_dir.iterdir()
            if p.is_file() and p.suffix == ".json"
        )
    except OSError as exc:
        logger.debug("[cursor_state] cannot list %s: %s", sessions_dir, exc)
        return []


def read_tally(path: Path) -> Optional[dict]:
    """Read a tally JSON file, returning the dict or None when unreadable."""
    try:
        if path.stat().st_size > _MAX_TALLY_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("[cursor_state] cannot read tally %s: %s", path, exc)
        return None


def idle_finalise(tally: dict, now: Optional[float] = None) -> dict:
    """Mark a never-finalised, stale tally as ``end_reason = "idle"``.

    Returns a COPY (never mutates the caller's dict). A tally whose latest
    activity is older than two hours and that never saw sessionEnd is
    finalised as idle; anything newer (or already final) is returned unchanged.
    The bridge clears the idle verdict on the next activity for that
    conversation (reopen), so an idle row is always upgradeable.

    ``now`` defaults to ``time.time()``; tests pass an explicit value.
    """
    import time

    if not isinstance(tally, dict) or tally.get("final"):
        return tally
    updated = tally.get("updated_at")
    if updated is None:
        return tally
    try:
        now_f = time.time() if now is None else float(now)
        if now_f - float(updated) >= _IDLE_SECS:
            out = dict(tally)
            out["final"] = True
            out["end_reason"] = "idle"
            return out
    except (TypeError, ValueError, OverflowError):
        return tally
    return tally


# ---------------------------------------------------------------------------
# Transcript estimate (CLI plane)
# ---------------------------------------------------------------------------


def transcript_estimate(transcript_path: Any, home: Path) -> Optional[int]:
    """Chars-over-four token estimate for a transcript, or None when absent.

    The transcript must live under ``<home>/projects`` (containment check so a
    crafted transcripts_path in the tally can't make the rollup read arbitrary
    files). Reads are capped; a missing/unreadable file returns None so the
    caller falls through to the tally-only source. Returns the char count // 4
    (possibly 0 for a tiny file) when a transcript IS present.
    """
    if not transcript_path:
        return None
    try:
        path = Path(str(transcript_path))
        root = (home / "projects").resolve(strict=False)
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_TRANSCRIPT_BYTES + 1)
        if len(raw) > _MAX_TRANSCRIPT_BYTES:
            raw = raw[:_MAX_TRANSCRIPT_BYTES]
    except OSError as exc:
        logger.debug("[cursor_state] cannot read transcript %s: %s", path, exc)
        return None
    text = raw.decode("utf-8", errors="replace")
    return len(text) // 4


# ---------------------------------------------------------------------------
# state.vscdb reader (IDE plane)
# ---------------------------------------------------------------------------


def state_vscdb_path() -> Path:
    """Default state.vscdb path per OS. May not exist."""
    home = _safe_home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Cursor"
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        base = Path(appdata) / "Cursor"
    else:
        base = home / ".config" / "Cursor"
    return base / "User" / "globalStorage" / "state.vscdb"


def state_vscdb_path_for_home(home: Path) -> Path:
    """Use an explicitly selected Cursor home, otherwise the IDE default."""
    home = Path(home).expanduser()
    if not os.environ.get("TOKEN_OPTIMIZER_CURSOR_HOME") and home == _safe_home() / ".cursor":
        return state_vscdb_path()
    return home / "User" / "globalStorage" / "state.vscdb"


def _ro_connect_vscdb(path: Path):
    """Open state.vscdb read-only. ``mode=ro`` only — not ``immutable=1``."""
    # Build the file: URI via Path.as_uri() rather than string interpolation
    # (same fix as codex_state._ro_connect, H-2/N-1): with uri=True, characters
    # like ?, #, % and spaces in the path are URI syntax, so a path containing
    # '?' would start the query string early and drop mode=ro — silently
    # opening a truncated path read-write. as_uri() percent-encodes those.
    # resolve() first (M-3) so a relative path doesn't raise an uncaught
    # ValueError past callers that only catch (sqlite3.Error, OSError).
    db_uri = Path(path).resolve().as_uri()
    conn = sqlite3.connect(
        f"{db_uri}?mode=ro",
        uri=True,
        timeout=0.25,
    )
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        conn.close()
        raise


def read_state_vscdb_tokens(
    composer_ids: Any, db_path: Optional[Path] = None
) -> dict:
    """Best-effort per-composer token/model read from state.vscdb.

    For each composer id, sums ``tokenCount.inputTokens`` / ``outputTokens``
    across ``bubbleId:<id>:`` rows and reads ``composerData:<id>`` for the model
    name and ``createdAt``.

    Returns ``{composer_id: {"input_tokens", "output_tokens", "model",
    "created_at_ms"}}``. Missing ids are omitted. A locked or missing DB yields
    ``{}`` (never raises) — a detached rollup must not stall on a live IDE DB.
    """
    result: dict = {}
    if db_path is None:
        db_path = state_vscdb_path()
    if not isinstance(db_path, Path):
        db_path = Path(str(db_path))
    if not db_path.is_file():
        return result
    ids = list(composer_ids) if composer_ids else []
    if not ids:
        return result

    try:
        conn = _ro_connect_vscdb(db_path)
    except (sqlite3.Error, OSError):
        # Locked (OperationalError: database is locked) -> skip this pass.
        return result

    # Exact ids we are looking for. Matching happens in Python on the id parsed
    # out of the key, so LIKE wildcards in an id (% _) can never widen the match
    # and misattribute another conversation's tokens.
    wanted = {}
    for cid in ids[:_MAX_COMPOSERS]:
        if isinstance(cid, str) and cid:
            wanted[cid] = {"input_tokens": 0, "output_tokens": 0, "model": None,
                           "created_at_ms": None, "_bubble_rows": 0}
    if not wanted:
        return result

    try:
        # ONE scan over the two key prefixes (was 2 queries per composer id:
        # N+1 full-table scans, 4.6s on a 55K-row DB with 500 composers).
        # With an index on cursorDiskKV(key) SQLite serves each LIKE prefix
        # from the index; without one this is a single pass instead of 2N.
        cur = conn.execute(
            "SELECT key, value FROM cursorDiskKV "
            "WHERE key LIKE 'bubbleId:%' OR key LIKE 'composerData:%'"
        )
        for row in cur:
            key = row["key"]
            if key.startswith("bubbleId:"):
                # Key shape is bubbleId:<composerId>:<bubbleId>; Cursor composer
                # ids are UUIDs (no ':'), so partition on the first colon is
                # exact. (The composerData branch needs no split.)
                rest = key[len("bubbleId:"):]
                cid, sep, _suffix = rest.partition(":")
                entry = wanted.get(cid)
                if entry is None or not sep:
                    continue
                if entry["_bubble_rows"] >= _MAX_BUBBLE_ROWS:
                    continue
                entry["_bubble_rows"] += 1
                try:
                    bubble = json.loads(row["value"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(bubble, dict):
                    continue
                # The DB knows about this id even if the bubble carries no
                # tokenCount: report it (zeroed) rather than omit it.
                entry["_found"] = True
                tc = bubble.get("tokenCount")
                if not isinstance(tc, dict):
                    continue
                try:
                    entry["input_tokens"] += int(tc.get("inputTokens") or 0)
                    entry["output_tokens"] += int(tc.get("outputTokens") or 0)
                except (TypeError, ValueError):
                    continue
            else:  # composerData:<id>
                cid = key[len("composerData:"):]
                entry = wanted.get(cid)
                if entry is None:
                    continue
                entry["_found"] = True
                try:
                    data = json.loads(row["value"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict):
                    mc = data.get("modelConfig")
                    if isinstance(mc, dict):
                        entry["model"] = mc.get("modelName") or entry["model"]
                    entry["created_at_ms"] = data.get("createdAt")
    except sqlite3.Error:
        # A mid-scan failure must not look like "no data": leave a signal.
        logger.warning("[cursor_state] vscdb scan aborted by sqlite error", exc_info=True)
    finally:
        conn.close()

    # Only report composers the DB actually knows about; a conversation
    # id with no bubble rows and no composerData is absent, not zero.
    for cid, entry in wanted.items():
        if entry.pop("_found", False):
            entry.pop("_bubble_rows", None)
            result[cid] = entry
    return result


if __name__ == "__main__":  # pragma: no cover - debug helper
    import json as _json

    home = None
    try:
        from runtime_env import cursor_home

        home = cursor_home()
    except Exception:
        home = _safe_home() / ".cursor"
    tallies = [read_tally(p) for p in find_tallies(home)]
    print(_json.dumps({"tallies": tallies, "db": str(state_vscdb_path())}, indent=2, default=str))
