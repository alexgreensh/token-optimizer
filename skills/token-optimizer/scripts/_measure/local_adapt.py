"""Local self-adaptation: pause or tighten compressors with high local unfold rates (SPEC-local-adapt Part A).

Evaluated at SessionStart from the local ledger (savings_events / compression_events).
Hot-path PostToolUse hooks read at most ONE small JSON file (<4 KB) and fail open to defaults
on missing, oversize, or corrupt files. Never enables default-off features and never loosens.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_STATE_BYTES = 4096
MIN_REPLACEMENTS = 20
TIGHTEN_RATE = 0.20
PAUSE_RATE = 0.30
PAUSE_DAYS = 14


def _now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def feature_health(conn, feature: str, days: int = 14, now: datetime | None = None) -> dict[str, Any]:
    """Return replacements, re-expands attributed to *feature*, and unfold_rate."""
    cur_now = _now_utc(now)
    cutoff = (cur_now - timedelta(days=days)).isoformat()
    replacements = 0
    reexpands = 0
    try:
        r1 = conn.execute(
            "SELECT COUNT(*) FROM compression_events WHERE feature = ? AND timestamp >= ? "
            "AND (tier IS NULL OR tier = 'measured')",
            (feature, cutoff),
        ).fetchone()
        replacements += int(r1[0] or 0) if r1 else 0
    except Exception:
        pass
    try:
        r2 = conn.execute(
            "SELECT COUNT(*) FROM savings_events WHERE event_type = ? AND timestamp >= ?",
            (feature, cutoff),
        ).fetchone()
        replacements += int(r2[0] or 0) if r2 else 0
    except Exception:
        pass
    try:
        r3 = conn.execute(
            "SELECT COUNT(*) FROM savings_events WHERE event_type = 'tool_archive_reexpand' "
            "AND timestamp >= ? AND (detail LIKE ? OR detail LIKE ?)",
            (cutoff, f"%({feature})%", f"% {feature} %"),
        ).fetchone()
        reexpands = int(r3[0] or 0) if r3 else 0
    except Exception:
        pass
    rate = (reexpands / replacements) if replacements > 0 else 0.0
    return {
        "feature": feature,
        "days": days,
        "replacements": replacements,
        "reexpands": reexpands,
        "unfold_rate": round(rate, 4),
    }


def _load_state(state_path: Path) -> dict[str, Any]:
    try:
        if not state_path.is_file() or state_path.stat().st_size > MAX_STATE_BYTES:
            return {"features": {}}
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("features"), dict):
            return data
    except Exception:
        pass
    return {"features": {}}


def evaluate_session_start(
    conn,
    state_path: Path,
    features: tuple[str, ...] | list[str],
    *,
    plugin_version: str = "0.0.0",
    days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate local unfold rates at SessionStart and write one small (<4 KB) JSON state file."""
    cur_now = _now_utc(now)
    state = _load_state(state_path)
    fmap: dict[str, dict[str, Any]] = state.setdefault("features", {})

    for feat in features:
        entry = dict(fmap.get(feat) or {})
        pause_count = int(entry.get("pause_count") or 0)
        paused_until_ver = entry.get("paused_until_next_version")
        # Already permanently paused until next plugin version?
        if paused_until_ver and paused_until_ver == plugin_version:
            entry["paused"] = True
            fmap[feat] = entry
            continue
        elif paused_until_ver and paused_until_ver != plugin_version:
            # New plugin version arrived: resume at tightened step
            entry["paused"] = False
            entry["paused_until"] = None
            entry["paused_until_next_version"] = None
            entry["min_size_mult"] = max(2, int(entry.get("min_size_mult") or 1))
            entry["keep_lines_mult"] = max(2, int(entry.get("keep_lines_mult") or 1))

        # Check active 14-day pause expiry
        paused_until_s = entry.get("paused_until")
        if entry.get("paused") and paused_until_s:
            try:
                pu = datetime.fromisoformat(paused_until_s)
                if pu.tzinfo is None:
                    pu = pu.replace(tzinfo=timezone.utc)
                if cur_now < pu:
                    fmap[feat] = entry
                    continue
                # Pause expired -> resume at tightened step (never loosens)
                entry["paused"] = False
                entry["paused_until"] = None
                entry["min_size_mult"] = max(2, int(entry.get("min_size_mult") or 1))
                entry["keep_lines_mult"] = max(2, int(entry.get("keep_lines_mult") or 1))
            except ValueError:
                entry["paused"] = False

        h = feature_health(conn, feat, days=days, now=cur_now)
        entry["replacements"] = h["replacements"]
        entry["reexpands"] = h["reexpands"]
        entry["unfold_rate"] = h["unfold_rate"]

        if h["replacements"] >= MIN_REPLACEMENTS:
            if h["unfold_rate"] >= PAUSE_RATE:
                pause_count += 1
                entry["pause_count"] = pause_count
                entry["paused"] = True
                if pause_count >= 2:
                    entry["paused_until"] = None
                    entry["paused_until_next_version"] = plugin_version
                else:
                    entry["paused_until"] = (cur_now + timedelta(days=PAUSE_DAYS)).isoformat()
            elif h["unfold_rate"] >= TIGHTEN_RATE:
                entry["min_size_mult"] = max(2, int(entry.get("min_size_mult") or 1))
                entry["keep_lines_mult"] = max(2, int(entry.get("keep_lines_mult") or 1))

        fmap[feat] = entry

    state["evaluated_at"] = cur_now.isoformat()
    state["plugin_version"] = plugin_version
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True)
    if len(raw.encode("utf-8")) <= MAX_STATE_BYTES:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, state_path)
    return state


def read_hot_path_state(state_path: Path, feature: str) -> dict[str, Any]:
    """Hot-path reader for PostToolUse hooks: env override > single <4 KB JSON > defaults."""
    env_key = "TOKEN_OPTIMIZER_" + feature.upper()
    override = os.environ.get(env_key, "").strip()
    if override == "1":
        return {"paused": False, "min_size_mult": 1, "keep_lines_mult": 1, "override": "on"}
    if override == "0":
        return {"paused": True, "min_size_mult": 1, "keep_lines_mult": 1, "override": "off"}
    state = _load_state(state_path)
    entry = state.get("features", {}).get(feature)
    if not isinstance(entry, dict):
        return {"paused": False, "min_size_mult": 1, "keep_lines_mult": 1, "override": None}
    return {
        "paused": bool(entry.get("paused", False)),
        "min_size_mult": max(1, int(entry.get("min_size_mult") or 1)),
        "keep_lines_mult": max(1, int(entry.get("keep_lines_mult") or 1)),
        "override": None,
    }


def paused_summary(state_path: Path) -> list[str]:
    """Human-readable lines for savings report, dashboard, and `measure.py features`."""
    state = _load_state(state_path)
    lines: list[str] = []
    for feat, entry in sorted(state.get("features", {}).items()):
        if isinstance(entry, dict) and entry.get("paused"):
            reexp = int(entry.get("reexpands") or 0)
            repl = int(entry.get("replacements") or 0)
            lines.append(f"paused here: {feat}, unfolded {reexp} of {repl}")
    return lines
