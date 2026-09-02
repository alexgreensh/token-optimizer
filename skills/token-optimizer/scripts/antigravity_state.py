#!/usr/bin/env python3
"""Read-only reader for Google Antigravity's conversation store.

Antigravity (CLI ``agy``, the Antigravity 2.0 desktop app, and the IDE) keeps
per-conversation SQLite databases under three surface directories inside the
Antigravity home (``~/.gemini`` by default):

    <home>/antigravity-cli/conversations/<uuid>.db
    <home>/antigravity/conversations/<uuid>.db
    <home>/antigravity-ide/conversations/<uuid>.db

Each database carries a ``gen_metadata`` table (one protobuf blob per model
generation, decoded by ``antigravity_proto``) and a ``steps`` table (step_type
14 = user input; a step with a decoded tool name = tool call). Per-surface
topic/workspace metadata lives in ``<surface>/conversation_summaries.db``.

Design constraints (mirroring ``hermes_state.py`` / ``copilot_state.py``):

- **Pure stdlib only.**  No third-party imports.
- **Strictly read-only.**  Open with ``mode=ro`` (plus ``immutable=1`` for
  databases idle over 60 s, the Hermes precedent, so no ``-wal``/``-shm`` side
  files are created and no lock is taken across hundreds of databases).
- **Minimal reads.**  Only ``gen_metadata.data``, ``steps.step_type`` and step
  ``metadata``, and the summaries columns in R21. Prompt text
  (``history.jsonl`` ``display``, summaries ``preview``), tool arguments, and
  ``trajectory_metadata_blob`` are NEVER selected (R21).
- **Schema-defensive.**  Missing tables/columns degrade to zero/None; a locked
  or undecodable database is skipped or counted, never fatal.
- **No hardcoded user paths.**  Home comes from ``runtime_env.antigravity_home()``.

The three surface directories are SEPARATE populations and are never summed:
the reader tags every session with its ``surface``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SURFACES = ("antigravity-cli", "antigravity", "antigravity-ide")
_CONVERSATIONS_DIR = "conversations"
_SUMMARIES_DB = "conversation_summaries.db"
_BUSY_TIMEOUT_SECONDS = 0.25
_IDLE_SECONDS = 60.0

# step_type values observed in the current schema (2026-09-01). A step is a
# USER INPUT at step_type 14; a step is a TOOL CALL when its metadata decodes a
# tool name (metadata field 4 sub-field 2). We detect tool calls by the decoded
# name, not a fixed step_type list, because the tool step-type enum is large
# and has shifted between releases (e.g. run_command=21, view_file=8).
_USER_INPUT_STEP_TYPE = 14

# Summaries columns we read (R21); nothing else from that table.
_SUMMARY_COLUMNS = (
    "title",
    "workspace_uris",
    "killed",
    "not_fully_idle",
    "last_modified_time",
    "nesting_depth",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _db_is_idle(db_path: Path) -> bool:
    """True when the db was not modified in the last ``_IDLE_SECONDS``."""
    try:
        return (time.time() - db_path.stat().st_mtime) > _IDLE_SECONDS
    except OSError:
        return True


def _ro_uri(db_path: Path, *, immutable: bool) -> str:
    uri = f"file:{db_path}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri


def _connect(db_path: Path, *, immutable: bool = True) -> sqlite3.Connection:
    """Open a conversation db read-only. Falls back to plain ``mode=ro`` when
    ``immutable=1`` refuses the file (e.g. a live ``-wal`` beside an idle db)."""
    try:
        conn = sqlite3.connect(
            _ro_uri(db_path, immutable=immutable),
            uri=True,
            timeout=_BUSY_TIMEOUT_SECONDS,
        )
    except sqlite3.Error:
        conn = sqlite3.connect(
            _ro_uri(db_path, immutable=False),
            uri=True,
            timeout=_BUSY_TIMEOUT_SECONDS,
        )
    return conn


def _clean_workspace(raw: Any) -> Optional[str]:
    """Parse summaries ``workspace_uris`` (a JSON array of ``file://`` URIs)
    into the first local path, or None. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    for uri in parsed:
        if not isinstance(uri, str):
            continue
        if uri.startswith("file://"):
            path = uri[len("file://") :]
            if path:
                return path
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def home() -> Path:
    """Return Antigravity's home directory (no hardcoded user paths)."""
    try:
        from runtime_env import antigravity_home

        return antigravity_home()
    except Exception:
        return Path("~/.gemini").expanduser()


def surface_dirs(explicit_home: Optional[Path] = None) -> list[tuple[str, Path]]:
    """Return existing ``(surface, path)`` pairs for the three surface dirs."""
    base = explicit_home if explicit_home is not None else home()
    result: list[tuple[str, Path]] = []
    for surface in _SURFACES:
        surface_path = base / surface
        try:
            if surface_path.is_dir() and not surface_path.is_symlink():
                result.append((surface, surface_path))
        except OSError:
            continue
    return result


def read_summaries(surface_dir: Path) -> dict[str, dict]:
    """Return ``{conversation_id: summary}`` from ``conversation_summaries.db``.

    Reads only the R21 columns. Absent/malformed summaries db -> ``{}``.
    """
    db_path = surface_dir / _SUMMARIES_DB
    if not db_path.is_file():
        return {}
    result: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(
            _ro_uri(db_path, immutable=True), uri=True, timeout=_BUSY_TIMEOUT_SECONDS
        )
        try:
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(conversation_summaries)")
            }
        except sqlite3.Error:
            return result
        select_cols = [c for c in _SUMMARY_COLUMNS if c in cols]
        if "conversation_id" not in cols or not select_cols:
            return result
        sql = f"SELECT conversation_id, {', '.join(select_cols)} FROM conversation_summaries"
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            return result
        for row in rows:
            conv_id = str(row[0])
            summary: dict[str, Any] = {}
            for i, col in enumerate(select_cols):
                summary[col] = row[i + 1]
            summary["workspace"] = _clean_workspace(summary.get("workspace_uris"))
            summary["killed"] = bool(_safe_int(summary.get("killed")))
            summary["not_fully_idle"] = bool(_safe_int(summary.get("not_fully_idle")))
            result[conv_id] = summary
    except (sqlite3.Error, OSError) as exc:
        logger.debug("[antigravity_state] read_summaries(%s): %s", surface_dir, exc)
    return result


def read_conversation(db_path: Path, *, surface: str = "") -> Optional[dict]:
    """Read one conversation database into a raw session dict, or None.

    Returns None when the file is a symlink, lacks a ``gen_metadata`` table,
    or cannot be opened. Aggregates token totals across generations and counts
    tool calls / user inputs from ``steps``. Never raises.
    """
    try:
        if not db_path.is_file() or db_path.is_symlink():
            return None
    except OSError:
        return None

    conversation_id = db_path.stem

    # Aggregates.
    generations: list[dict] = []
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
    }
    credit_cost = 0
    consumed_credits = 0
    model_volumes: dict[str, int] = {}
    undecodable_rows = 0
    last_fill: Optional[float] = None
    last_max_context: Optional[int] = None

    tool_call_count = 0
    user_input_count = 0
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    try:
        from antigravity_proto import decode_generation, decode_step_metadata
    except Exception:
        return None

    try:
        conn = _connect(db_path, immutable=_db_is_idle(db_path))
    except (sqlite3.Error, OSError) as exc:
        logger.debug("[antigravity_state] open %s: %s", db_path, exc)
        return None

    try:
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        except sqlite3.Error:
            return None
        if "gen_metadata" not in tables:
            return None

        try:
            gen_rows = conn.execute(
                "SELECT data FROM gen_metadata ORDER BY idx"
            ).fetchall()
        except sqlite3.Error:
            gen_rows = []
        for (data,) in gen_rows:
            rec = decode_generation(data)
            if rec is None:
                undecodable_rows += 1
                continue
            generations.append(rec)
            totals["input_tokens"] += rec["input_tokens"]
            totals["output_tokens"] += rec["output_tokens"]
            totals["cache_read_tokens"] += rec["cache_read_tokens"]
            totals["thinking_tokens"] += rec["thinking_tokens"]
            credit_cost += rec.get("credit_cost", 0)
            consumed_credits += rec.get("consumed_credits", 0)
            model = rec.get("model_display_name") or "unknown"
            model_volumes[model] = model_volumes.get(model, 0) + rec["output_tokens"]
            if rec["max_context_tokens"] and rec["max_context_tokens"] > 0:
                last_fill = rec["estimated_tokens_used"] / rec["max_context_tokens"]
                last_max_context = rec["max_context_tokens"]

        if "steps" in tables:
            try:
                step_rows = conn.execute(
                    "SELECT step_type, metadata FROM steps ORDER BY idx"
                ).fetchall()
            except sqlite3.Error:
                step_rows = []
            for step_type, metadata in step_rows:
                if step_type == _USER_INPUT_STEP_TYPE:
                    user_input_count += 1
                step_meta = decode_step_metadata(metadata) if metadata else None
                if step_meta and step_meta["tool_name"]:
                    tool_call_count += 1
                ts = step_meta["timestamp"] if step_meta else None
                if ts is not None:
                    if start_time is None or ts < start_time:
                        start_time = ts
                    if end_time is None or ts > end_time:
                        end_time = ts
    finally:
        conn.close()

    # Timestamp fallback: conversation db mtime when steps carried none.
    if start_time is None:
        try:
            start_time = end_time = db_path.stat().st_mtime
        except OSError:
            pass

    model_display_name = ""
    if model_volumes:
        model_display_name = max(model_volumes, key=model_volumes.get)  # type: ignore[arg-type]

    return {
        "conversation_id": conversation_id,
        "surface": surface,
        "generations": generations,
        "totals": totals,
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "thinking_tokens": totals["thinking_tokens"],
        "credit_cost": credit_cost,
        "consumed_credits": consumed_credits,
        "last_fill": last_fill,
        "last_max_context": last_max_context,
        "model_display_name": model_display_name,
        "undecodable_rows": undecodable_rows,
        "tool_call_count": tool_call_count,
        "user_input_count": user_input_count,
        "start_time": start_time,
        "end_time": end_time,
    }


def read_live_conversation(db_path: Path) -> Optional[dict]:
    """Read a single, possibly-live conversation (the nudge hot path).

    Same shape as ``read_conversation`` but always opens with plain ``mode=ro``
    (never ``immutable=1``) and a short busy-timeout, for the conversation the
    agent is writing RIGHT NOW. Returns None on any failure. May leave a
    ``-shm`` file beside a live database (documented carve-out, KTD6).
    """
    if db_path is None:
        return None
    db_path = Path(db_path)
    try:
        if not db_path.is_file() or db_path.is_symlink():
            return None
    except OSError:
        return None
    # Read through a fresh connection; force mode=ro (no immutable) regardless
    # of idleness. We reuse read_conversation's core by temporarily computing
    # liveness is irrelevant here -> small wrapper opens without immutable.
    try:
        from antigravity_proto import decode_generation
    except Exception:
        return None

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS
        )
    except (sqlite3.Error, OSError):
        return None
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "gen_metadata" not in tables:
            return None
        gen_rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
    }
    last_fill: Optional[float] = None
    for (data,) in gen_rows:
        rec = decode_generation(data)
        if rec is None:
            continue
        totals["input_tokens"] += rec["input_tokens"]
        totals["output_tokens"] += rec["output_tokens"]
        totals["cache_read_tokens"] += rec["cache_read_tokens"]
        totals["thinking_tokens"] += rec["thinking_tokens"]
        if rec["max_context_tokens"] and rec["max_context_tokens"] > 0:
            last_fill = rec["estimated_tokens_used"] / rec["max_context_tokens"]
    return {"last_fill": last_fill, "totals": totals, "conversation_id": db_path.stem}


def read_all_sessions(explicit_home: Optional[Path] = None) -> list[dict]:
    """Read every conversation across the three surfaces into raw dicts.

    Attaches summaries (title/workspace/killed/...) to each session by
    conversation id. Returns [] when home is absent or empty. Never raises.
    """
    results: list[dict] = []
    for surface, surface_path in surface_dirs(explicit_home):
        summaries = read_summaries(surface_path)
        conv_dir = surface_path / _CONVERSATIONS_DIR
        if not conv_dir.is_dir():
            continue
        try:
            dbs = sorted(conv_dir.glob("*.db"))
        except OSError:
            continue
        for db_path in dbs:
            try:
                session = read_conversation(db_path, surface=surface)
            except Exception as exc:
                logger.debug(
                    "[antigravity_state] read_conversation(%s): %s", db_path, exc
                )
                continue
            if session is None:
                continue
            summary = summaries.get(session["conversation_id"], {})
            session["title"] = summary.get("title") or ""
            session["workspace"] = summary.get("workspace")
            session["killed"] = bool(summary.get("killed"))
            session["not_fully_idle"] = bool(summary.get("not_fully_idle"))
            session["last_modified_time"] = summary.get("last_modified_time")
            session["nesting_depth"] = _safe_int(summary.get("nesting_depth"))
            results.append(session)
    return results


if __name__ == "__main__":
    sessions = read_all_sessions()
    print(f"surfaces: {[s for s, _ in surface_dirs()]}")
    print(f"sessions: {len(sessions)}")
    with_tokens = [
        s
        for s in sessions
        if s["totals"]["input_tokens"] or s["totals"]["output_tokens"]
    ]
    print(f"sessions with any tokens: {len(with_tokens)}")
    if with_tokens:
        s = with_tokens[0]
        print(
            "sample:",
            json.dumps(
                {
                    k: s[k]
                    for k in (
                        "conversation_id",
                        "surface",
                        "totals",
                        "model_display_name",
                        "tool_call_count",
                        "user_input_count",
                        "last_fill",
                        "killed",
                    )
                },
                default=str,
                indent=2,
            ),
        )
