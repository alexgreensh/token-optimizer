"""Build bounded periphery maps and return the paths actually included."""
from pathlib import Path
import hashlib
import json
import os
import time


def build(paths, limit, budget, summarize, shown_paths=None):
    parts = []
    total = 0
    for raw in paths:
        if len(parts) >= limit or total >= budget:
            break
        path = Path(raw)
        try:
            result = summarize(path)
        except Exception:
            continue
        if not result.replacement_text:
            continue
        chunk = f"# periphery: {path.name} ({result.replacement_type})\n{result.replacement_text}"
        if total + len(chunk) > budget:
            break
        parts.append(chunk)
        total += len(chunk)
        if shown_paths is not None:
            shown_paths.append(str(raw))
    return "\n\n".join(parts)


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def exposure_rows(session_id, tool_id, shown, language, band):
    """Stable unique IDs for shown paths; no source paths or content persisted."""
    return _rows(session_id, tool_id, shown, language, band, "map")


def control_rows(session_id, tool_id, path, language, band):
    return _rows(session_id, tool_id, [path], language, band, "no_map")


def _rows(session_id, tool_id, shown, language, band, arm):
    if not session_id or not tool_id:
        return []
    rows, seen = [], set()
    for path in shown:
        identity = json.dumps([session_id, tool_id, str(path), arm], separators=(",", ":"))
        exposure_id = _hash(identity)
        if exposure_id in seen:
            continue
        seen.add(exposure_id)
        rows.append({"exposure_id": exposure_id, "session_id": _hash(session_id),
                     "tool_use_id": _hash(tool_id),
                     "path_id": _hash(json.dumps([session_id, str(path)], separators=(",", ":"))),
                     "language": language, "band": band, "timestamp": time.time(),
                     "arm": arm, "read_confirmed": False})
    return rows


def record_exposures(snapshot_dir, session_id, tool_id, shown, language, band):
    _record(snapshot_dir, exposure_rows(session_id, tool_id, shown, language, band))


def record_control(snapshot_dir, session_id, tool_id, path, language, band):
    _record(snapshot_dir, control_rows(session_id, tool_id, path, language, band))


def _record(snapshot_dir, rows):
    if not rows:
        return
    directory = snapshot_dir / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(directory / "periphery_exposures.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
