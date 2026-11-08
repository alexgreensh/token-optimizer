#!/usr/bin/env python3
"""Token Optimizer — Cursor CLI ``statusLine`` command (opt-in).

Cursor CLI (changelog v2026.08.11) supports a custom ``statusLine`` command in
the CLI config, shaped ``{ "statusLine": { "type": "command", "command": ... } }``.
The command receives ONE JSON object on stdin and its stdout renders as the
footer; each printed line renders as its own row.

Evidence state (2026-11-09), kept honest per R13:

- Cursor's official CLI configuration reference does NOT document the
  statusLine payload schema. The only payload evidence is a user report
  against CLI 2026.07.20 (forum.cursor.com/t/166364): stdin carries
  ``context_window.total_input_tokens``, ``context_window.context_window_size``
  and ``autorun``. Every payload read below is therefore schema-defensive: an
  absent or renamed field renders as "?" or is omitted — never a fabricated
  value.
- Live-fill gate (architecture judge, 2026-11-09): a "context X% full" number
  may only come from a proven host occupancy snapshot. The statusLine payload's
  ``context_window`` fields are forum-observed but NOT yet proven to be current
  occupancy (vs. a cumulative/usage counter) on current builds, so payload fill
  is gated behind ``PAYLOAD_FILL_PROVEN = False`` below. The integration owner
  flips it to True only when the extracted CLI bundle contract proves
  ``context_window.total_input_tokens`` is present-window occupancy. Until
  then the only fill source is the tally's last ``preCompact`` snapshot, which
  IS documented occupancy. A lifetime token sum is never a fill numerator.
- Cursor staff (kevinn, same thread) confirmed the custom command REPLACES the
  native two-row footer (there is no append/prepend mode) and that multiline
  stdout renders as separate rows. Line 1 below therefore reconstructs the
  model/context information the native footer would have shown.
- The payload's ``autorun`` boolean cannot distinguish Allowlist from
  Auto-review approval modes (staff-confirmed), so this script NEVER renders
  approval-mode state.

Wiring is strictly opt-in (R12/KTD7): the installer offers this command only on
explicit request because it replaces the native footer. This script itself is
read-only — it writes nothing, keeps the ~40ms hook-path budget (stdlib only,
no heavy imports), and always exits 0. Any failure prints nothing rather than
garbage into the footer.

Line 1 (native-footer reconstruction): ``<model> · ctx <pct>% (<used>/<window>)``.
Line 2 (Token Optimizer): fill-derived grade, compaction and tool-call counts
from the session tally under ``<cursor_home>/token-optimizer/sessions/``.

The grade here is the fill-only formula mirrored from cursor_session's
documented fallback (score = 100 - fill*50). The dashboard's multi-signal
quality score lives in cursor_session/hermes_session; when the fuller scorer
is what the user wants, the dashboard carries it — this footer stays a cheap,
honest fill gauge.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Bounds: a statusline runs on the CLI's refresh cadence, so every read is
# capped. A pathological payload or tally must never stall the footer.
_MAX_STDIN_BYTES = 1024 * 1024
_MAX_TALLY_BYTES = 4 * 1024 * 1024
_MAX_TALLY_CANDIDATES = 8
_MAX_MODEL_LEN = 40
_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")

# Live-fill gate (see module docstring): False until the installed CLI bundle's
# statusLine contract proves context_window.total_input_tokens is present-window
# occupancy. Flipping this without that evidence is a contract violation.
PAYLOAD_FILL_PROVEN = False


# ---------------------------------------------------------------------------
# Payload extraction (schema-defensive; see module docstring for evidence)
# ---------------------------------------------------------------------------


def _read_stdin_payload() -> dict:
    """Read and parse the single JSON object Cursor pipes on stdin; {} on any failure."""
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    except (OSError, ValueError):
        return {}
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _payload_model(payload: dict) -> Optional[str]:
    """Model name from tolerant key shapes; None when the payload carries none."""
    for key in ("model", "model_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_MODEL_LEN]
        if isinstance(value, dict):
            for sub in ("display_name", "name", "id"):
                name = value.get(sub)
                if isinstance(name, str) and name.strip():
                    return name.strip()[:_MAX_MODEL_LEN]
    return None


def _payload_context(payload: dict) -> tuple[Optional[int], Optional[int]]:
    """(used_tokens, window_tokens) from the payload, or (None, None).

    The forum-observed shape is ``context_window.{total_input_tokens,
    context_window_size}``; top-level ``context_tokens``/``context_window_size``
    (the preCompact hook's documented keys) are tolerated as well. Non-positive
    or non-numeric values are treated as absent.
    """
    candidates = []
    cw = payload.get("context_window")
    if isinstance(cw, dict):
        candidates.append((cw.get("total_input_tokens"), cw.get("context_window_size")))
    candidates.append((payload.get("context_tokens"), payload.get("context_window_size")))
    for used_raw, window_raw in candidates:
        try:
            used = int(float(used_raw)) if used_raw is not None else 0
            window = int(float(window_raw)) if window_raw is not None else 0
        except (TypeError, ValueError, OverflowError):
            continue
        if used > 0 and window > 0:
            return used, window
    return None, None


def _payload_conversation_id(payload: dict) -> str:
    for key in ("conversation_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Session tally (read-only; written by cursor_hook_bridge.py)
# ---------------------------------------------------------------------------


def _sanitize_session_id(sid: str) -> str:
    """Mirror cursor_hook_bridge._sanitize_session_id (path-traversal guard)."""
    if not sid:
        return "unknown"
    cleaned = _SESSION_ID_RE.sub("", sid)[:64]
    return cleaned if len(cleaned) >= 6 else "unknown"


def _cursor_home() -> Path:
    try:
        from runtime_env import cursor_home

        return cursor_home()
    except Exception:  # broken/partial install: read the default location
        return Path.home() / ".cursor"


def _read_tally(path: Path) -> Optional[dict]:
    try:
        if path.stat().st_size > _MAX_TALLY_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _find_tally(sessions_dir: Path, sid: str) -> Optional[dict]:
    """Tally for this conversation, else the most recently updated tally.

    A sanitized id of "unknown" never selects the literal unknown.json bucket —
    that file mixes every id-less session, so the latest-real-tally fallback is
    the honest choice.
    """
    if not sessions_dir.is_dir():
        return None
    clean = _sanitize_session_id(sid)
    if clean != "unknown":
        direct = _read_tally(sessions_dir / f"{clean}.json")
        if direct is not None:
            return direct
    try:
        candidates = sorted(
            (p for p in sessions_dir.iterdir() if p.is_file() and p.suffix == ".json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:_MAX_TALLY_CANDIDATES]
    except OSError:
        return None
    best: Optional[dict] = None
    best_updated = -1.0
    for path in candidates:
        tally = _read_tally(path)
        if tally is None:
            continue
        updated = _safe_float(tally.get("updated_at")) or 0.0
        if updated > best_updated:
            best, best_updated = tally, updated
    return best


def _tally_fill(tally: dict) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """(used_tokens, window_tokens, usage_percent) from the newest usable preCompact entry.

    The bridge records context_usage_percent as its own field, so an entry may
    carry a percent without token counts; either shape is a proven occupancy
    snapshot. Token counts are preferred because they also render the
    used/window detail.
    """
    compactions = tally.get("compactions")
    if not isinstance(compactions, list):
        return None, None, None
    for entry in reversed(compactions):
        if not isinstance(entry, dict):
            continue
        used = _safe_float(entry.get("context_tokens"))
        window = _safe_float(entry.get("context_window_size"))
        if used and window and used > 0 and window > 0:
            return int(used), int(window), None
        pct = _safe_float(entry.get("context_usage_percent"))
        if pct and pct > 0:
            return None, None, pct
    return None, None, None


# ---------------------------------------------------------------------------
# Fill-derived grade (mirrors cursor_session._quality's documented fallback)
# ---------------------------------------------------------------------------


def _fill_grade(fill_frac: float) -> tuple[int, str]:
    fill = min(1.0, max(0.0, fill_frac))
    score = max(0, round(100.0 - fill * 50.0))
    grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))
    return score, grade


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


# ---------------------------------------------------------------------------
# Line assembly
# ---------------------------------------------------------------------------


def build_lines(payload: dict, tally: Optional[dict]) -> list[str]:
    """Assemble the footer rows. Empty list => print nothing.

    Never renders approval-mode state: the payload's ``autorun`` boolean cannot
    distinguish Allowlist from Auto-review (staff-confirmed), so it is read by
    no code path here on purpose.
    """
    used, window = _payload_context(payload) if PAYLOAD_FILL_PROVEN else (None, None)
    pct_direct: Optional[float] = None
    fill_source = "live"
    if used is None and tally is not None:
        used, window, pct_direct = _tally_fill(tally)
        fill_source = "preCompact"

    # Single fill fraction behind every consumer (percent, grade), from
    # whichever proven shape exists; never a lifetime sum.
    fill_frac: Optional[float] = None
    if used is not None and window:
        fill_frac = min(1.0, used / window)
    elif pct_direct is not None:
        fill_frac = min(1.0, pct_direct / 100.0)

    # Line 1: native-footer reconstruction (model + context fill).
    segments = []
    model = _payload_model(payload)
    if model is None and tally is not None:
        models = tally.get("models")
        if isinstance(models, dict) and len(models) == 1:
            # The tally records per-model turn COUNTS with no recency, so after
            # a mid-session model switch max-by-count could name a model the
            # session has moved away from. Only a single recorded model is a
            # safe display; anything richer needs a last_model tally field
            # (requested from the bridge owner in the wiring spec).
            model = str(next(iter(models)))[:_MAX_MODEL_LEN]
    if model:
        segments.append(model)
    if fill_frac is not None:
        pct = min(100, round(100 * fill_frac))
        if used is not None and window:
            segments.append(f"ctx {pct}% ({_fmt_tokens(used)}/{_fmt_tokens(window)})")
        else:
            segments.append(f"ctx {pct}%")
    else:
        segments.append("ctx ?")
    line1 = " · ".join(segments) if segments else ""

    # Line 2: Token Optimizer session facts from the tally.
    if tally is None:
        line2 = "TO · no session data"
    else:
        parts = []
        if fill_frac is not None:
            score, grade = _fill_grade(fill_frac)
            parts.append(f"TO {grade} {score}")
            parts.append(f"fill {fill_source}")
        else:
            parts.append("TO · fill ?")
        compactions = tally.get("compactions")
        if isinstance(compactions, list):
            parts.append(f"comp {len(compactions)}")
        tool_calls = tally.get("tool_calls")
        if isinstance(tool_calls, (int, float)) and tool_calls > 0:
            parts.append(f"tools {int(tool_calls)}")
        line2 = " · ".join(parts)

    return [line for line in (line1, line2) if line]


def main() -> int:
    try:
        payload = _read_stdin_payload()
        home = _cursor_home()
        tally = _find_tally(home / "token-optimizer" / "sessions",
                            _payload_conversation_id(payload))
        lines = build_lines(payload, tally)
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
    except Exception:  # never break the footer; fail silent per docstring
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
