"""Compact-checkpoint capture and bounded restore reads for the Cursor bridge.

Split out of cursor_hook_bridge.py under the repo's 1,000-line file-size
ratchet (scripts/check_file_size.py). The Cursor home is RESOLVED BY THE
BRIDGE's same-name wrappers and injected, so tests monkeypatching
cursor_hook_bridge.cursor_home keep working. Confinement and byte budgets are
unchanged: only resolved paths under <cursor_home>/token-optimizer are read,
leaf symlinks refused, and the restore payload stays within its byte budget.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RESTORE_MAX_BYTES = 16_384        # restore-context files above this are ignored
_COMPACT_RESTORE_MAX_BYTES = 8_000  # restore payload cap (bounded inject)


def _import_measure_via_locator(locate_measure_py):
    """Resolve ``import measure`` for an installed bridge (no copied measure).

    The payload deliberately ships no measure.py (a plugin-dir copy is never
    refreshed, so it would drift from the checkout); the installer-written
    one-line locator names the canonical measure.py. Appending its validated
    parent dir lets the normal import machinery load measure + its _measure
    package from that single source of truth, registering sys.modules
    ['measure'] so the provider facade resolves. Trust boundary is unchanged:
    _locate_measure_py already gates the fire-and-forget rollup/dashboard
    spawns of the same file - a crafted locator could already execute code as
    the user. Fail-open: any error returns None and the capture degrades to
    numbers-only recording.
    """
    try:
        try:
            import measure
            return measure
        except ImportError:
            pass
        if locate_measure_py is None:
            return None
        located = locate_measure_py()
        if located is None:
            return None
        parent = str(Path(located).parent)
        if parent not in sys.path:
            sys.path.append(parent)  # append: payload modules keep precedence
        import measure
        return measure
    except Exception:
        return None


def _capture_compact_checkpoint(fields, home, locate_measure_py=None):
    """U3: translate the Cursor disk transcript and capture a checkpoint.

    Returns the checkpoint path (str) or None. Uses the U1 adapter's
    bundle-proven schema translation and compact_capture's trusted internal
    path; the translated temp file is always removed. Imported ``measure``
    resolves CHECKPOINT_DIR under the Cursor runtime home (CURSOR_VERSION is
    exported to every Cursor hook process), so checkpoint state never lands
    in Claude's ledger. Fail-open: any error returns None and preCompact
    degrades to today's numbers-only recording.
    """
    transcript_path = fields.get("transcript_path")
    sid = fields.get("conversation_id") or ""
    if not transcript_path or not sid:
        return None
    translated = None
    try:
        import cursor_transcript
        from cursor_thrash import _restore_env, _set_env

        if home is None:
            return None
        translated = cursor_transcript.translate_transcript(
            transcript_path, home, sid
        )
        if translated is None:
            return None
        old_snapshot = _set_env(
            "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
            str(home / "token-optimizer" / "snapshot"),
        )
        try:
            measure = _import_measure_via_locator(locate_measure_py)
            if measure is None:
                return None

            fill_pct = fields.get("context_usage_percent")
            try:
                fill_pct = float(fill_pct) if fill_pct is not None else None
            except (TypeError, ValueError):
                fill_pct = None
            checkpoint = measure.compact_capture(
                session_id=sid,
                trigger=fields.get("trigger") or "precompact",
                cwd=fields.get("cwd") or None,
                fill_pct=fill_pct,
                _trusted_transcript=translated,
            )
        finally:
            _restore_env("TOKEN_OPTIMIZER_SNAPSHOT_DIR", old_snapshot)
        return str(checkpoint) if checkpoint else None
    except Exception:
        return None
    finally:
        if translated is not None:
            try:
                translated.unlink()
            except OSError:
                pass


def _read_checkpoint_bounded(path_str, home):
    """Read a checkpoint for restore injection, byte-bounded. Never raises.

    The tally is forgeable local state: only paths that resolve under our own
    data dir (<cursor_home>/token-optimizer) may be read into model context.
    resolve() collapses parent-directory symlink escapes; the leaf symlink
    check above covers the file itself (G1).
    """
    try:
        path = Path(path_str)
        if not path.is_file() or path.is_symlink():
            return None
        if home is None:
            return None
        root = (home / "token-optimizer").resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            return None
        if resolved.stat().st_size > _RESTORE_MAX_BYTES:
            return None
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    body = text.strip()
    if not body:
        return None
    prefix = (
        "Token Optimizer checkpoint captured before compaction "
        "(restore after context rebuild):\n\n"
    )
    marker = "\n\n[truncated: checkpoint exceeds restore budget]"
    # The 8KB contract bounds the WHOLE emitted string (prefix + body +
    # marker) in UTF-8 bytes; cut encoded bytes so multibyte checkpoints
    # cannot inflate the inject (a character cut misses by up to ~4x).
    body_budget = _COMPACT_RESTORE_MAX_BYTES - len(prefix.encode("utf-8"))
    if len(body.encode("utf-8", "replace")) > body_budget:
        cut = body.encode("utf-8", "replace")[
            : body_budget - len(marker.encode("utf-8"))
        ].decode("utf-8", errors="ignore")
        body = cut + marker
    return prefix + body
