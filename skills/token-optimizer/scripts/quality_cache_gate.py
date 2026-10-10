#!/usr/bin/env python3
"""Quality-cache throttle gate — lightweight pre-check before importing measure.py.

The PostToolUse ``quality-cache --throttle-only`` hook fires on EVERY tool call.
In the common case (throttle window active, ~97% of calls), it does nothing but
one stat() of a throttle marker file. But importing measure.py (44k+ lines, 2MB)
costs 682ms cold — paid on every single tool call before the throttle check
even runs. In a read-only-scripts container (the benchmark steady state),
__pycache__ never persists, so every call pays the cold import.

This gate mirrors the throttle-marker stat() that measure.py's
``_quality_cache_tick_due`` performs, but WITHOUT importing measure.py. If the
throttle window is still active (the common case), it exits 0 immediately.
Only when the throttle has EXPIRED does it fall through to import measure.py
and run the full quality-cache computation — exactly as today.

Mirrors the pattern every other hook script already uses to stay measure-free:
read_cache.py, bash_hook.py, bash_compress_hook.py, archive_result.py,
context_intel.py, refetch_guard.py all avoid importing measure.py deliberately.

HARD INVARIANT preserved: a throttle-only cache MISS (missing marker) never
parses a transcript. The gate treats a missing marker as "not due" (OSError →
False, mirroring ``_quality_cache_tick_due``) and exits 0 without importing
measure.py at all.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path

# --- Lightweight path resolution (mirrors measure.py's _STATE_BASE / QUALITY_CACHE_DIR) ---
# These imports are deliberately from lightweight stdlib-only modules that do
# NOT import measure.py. This is the same pattern refetch_guard.py and
# archive_result.py use. If they fail to load, _ENV_RESOLVED is False and the
# gate falls through to the full measure.py path (correctness over speed) rather
# than silently guessing a marker path that might never exist.
try:
    from runtime_env import runtime_home, is_cowork
    from plugin_env import resolve_plugin_data_dir

    _ENV_RESOLVED = True
except Exception:
    _ENV_RESOLVED = False
    runtime_home = None
    is_cowork = None
    resolve_plugin_data_dir = None


@lru_cache(maxsize=1)
def _resolve_quality_cache_dir() -> Path:
    """Resolve QUALITY_CACHE_DIR exactly as measure.py does.

    Mirrors measure.py lines ~33616:
      _STATE_BASE = _RESOLVED_PLUGIN_DATA if (cowork + plugin_data + no override) else RUNTIME_DIR
      QUALITY_CACHE_DIR = _STATE_BASE / "token-optimizer"

    Cached so repeated in-process calls (via the runner's delegation) do not
    re-resolve. ``resolve_plugin_data_dir`` is called LAZILY — only after
    ``is_cowork()`` is True and no snapshot override is set — so the common
    desktop/local case never pays the ``installed_plugins.json`` stat/read cost.
    """
    runtime_dir = runtime_home()
    snapshot_override = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()

    # Lazy: only resolve plugin_data when it could actually be used.
    # resolve_plugin_data_dir() can stat/read installed_plugins.json, which is
    # wasted work in the common non-Cowork case.
    if (
        not snapshot_override
        and is_cowork is not None
        and is_cowork()
        and resolve_plugin_data_dir is not None
    ):
        plugin_data = resolve_plugin_data_dir()
        if plugin_data is not None:
            return plugin_data / "token-optimizer"

    return runtime_dir / "token-optimizer"


def _sanitize_session_id(sid: str) -> str:
    """Mirror measure.py.sanitize_session_id for the throttle marker path."""
    if not sid:
        return "unknown"
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", sid)
    return sanitized if len(sanitized) >= 6 else "unknown"


def _coerce_str(value) -> str | None:
    """Coerce a stdin payload value to str, or None if not string-like.

    The hook payload is untrusted JSON. transcript_path and session_id should
    be strings, but a malformed or malicious payload could send int, list, null,
    etc. Path() and re.sub() raise TypeError on non-string input, which would
    crash the hook. This guard prevents that.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _throttle_marker(
    filepath=None, session_id=None, cache_dir: Path | None = None
) -> Path:
    """Mirror measure.py._quality_cache_throttle_marker.

    Returns the throttle marker path for one session (legacy global if unknown).
    """
    if cache_dir is None:
        cache_dir = _resolve_quality_cache_dir()

    identity = None
    if filepath:
        identity = Path(filepath).stem
    elif session_id:
        identity = _sanitize_session_id(session_id)

    if identity:
        identity = re.sub(r"[^a-zA-Z0-9_-]", "_", str(identity))[:128]
        return cache_dir / f".quality-cache-throttle-{identity}"
    return cache_dir / ".quality-cache-throttle"


def _tick_due(
    throttle_seconds: int,
    filepath=None,
    session_id=None,
    cache_dir: Path | None = None,
) -> bool:
    """Mirror measure.py._quality_cache_tick_due.

    One-stat gate: True when the marker is older than throttle_seconds.
    A missing marker is a cache miss, NOT permission to parse a transcript.
    Returns False (not due) on OSError, preserving the invariant that a
    throttle-only cache MISS never parses a transcript.

    Also catches TypeError/ValueError from non-string payload values, so a
    malformed payload (e.g. transcript_path as int) fails open as "not due"
    rather than crashing the hook.
    """
    try:
        marker = _throttle_marker(filepath, session_id, cache_dir)
        age = time.time() - marker.stat().st_mtime
        return age >= max(0, throttle_seconds)
    except (OSError, TypeError, ValueError):
        return False


def _read_stdin_payload() -> dict:
    """Read the hook payload from stdin (transcript_path, session_id).

    Mirrors measure.py._read_throttle_only_hook_input: on Windows, avoids
    reading an open stdin pipe (PeekNamedPipe first); on POSIX, uses the
    shared hook_io reader with select-based non-blocking semantics.
    """
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt

            stream = getattr(sys.stdin, "buffer", sys.stdin)
            fd = stream.fileno()
            available = ctypes.c_ulong()
            handle = msvcrt.get_osfhandle(fd)
            ok = ctypes.windll.kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(available), None
            )
            if not ok or not available.value:
                return {}
            raw = os.read(fd, min(1_000_000, available.value))
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (
            AttributeError,
            ImportError,
            OSError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return {}
    try:
        from hook_io import read_stdin_hook_input

        return read_stdin_hook_input(max_bytes=1_000_000)
    except Exception:
        return {}


def main() -> int:
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    warn = "--warn" in args
    force = "--force" in args
    throttle = 120
    warn_threshold = 70
    for i, a in enumerate(args):
        if a == "--throttle" and i + 1 < len(args):
            try:
                throttle = int(args[i + 1])
            except ValueError:
                pass
        if a == "--warn-threshold" and i + 1 < len(args):
            try:
                warn_threshold = int(args[i + 1])
            except ValueError:
                pass

    # Read the hook payload from stdin (transcript_path, session_id)
    payload = _read_stdin_payload()

    # Coerce payload values to strings — the hook payload is untrusted JSON and
    # non-string values (int, list, null) would crash Path() or re.sub().
    filepath = _coerce_str(payload.get("transcript_path"))
    session_id = _coerce_str(payload.get("session_id"))

    # Force bypasses the throttle check — always fall through to full computation.
    # Also, if the lightweight env modules failed to import, we cannot resolve
    # the marker path safely — fall through to measure.py (correctness over speed)
    # rather than guessing a path that might never exist and silently freezing.
    if not force and _ENV_RESOLVED:
        cache_dir = _resolve_quality_cache_dir()

        if not _tick_due(throttle, filepath, session_id, cache_dir):
            # Throttle ACTIVE (common case, ~97% of calls) — exit 0 immediately.
            # No measure.py import, no transcript parse, no cache write.
            # This is the entire point of the gate: a stat() instead of 682ms.
            return 0
        # Throttle EXPIRED — fall through to the full quality-cache computation.
        # This is the rare case (~3% of calls), so the 682ms cold import is
        # acceptable here, exactly as today.

    # --- Throttle expired (or forced, or env unresolved) — full computation path ---
    # Import measure.py and run the full quality-cache computation. This
    # mirrors the original dispatch's quality-cache --throttle-only path:
    # quality_cache() + evaluate_cohort_tripwire() piggyback.
    try:
        import measure

        # Mid-session dashboard-daemon liveness pulse (best-effort, same as
        # the original dispatch).
        try:
            measure._daemon_midsession_pulse()
        except Exception:
            pass

        # Self-heal: if the quality-cache hook is missing from settings.json
        # and this is NOT a plugin install and quality_bar_disabled is unset,
        # reinstall it. Mirrors the original dispatch's self-heal block and
        # the runner's _quality_cache_self_heal. Only runs on the expired
        # path (after the throttle check), exactly as in the dispatch.
        try:
            _quality_cache_self_heal(measure)
        except Exception:
            pass

        measure.quality_cache(
            throttle_seconds=throttle,
            warn_threshold=warn_threshold,
            quiet=quiet,
            pure_time_throttle=True,
            session_jsonl=filepath,
            session_id=session_id,
            force=force,
            warn=warn,
        )

        # Tripwire piggyback: the --throttle-only invocation fires on the
        # PostToolUse Edit/Write path, so refresh the per-cohort live edit-rate
        # verdict. Mtime-gated to 5 min, so the common case is a single sidecar
        # stat() — near-zero added cost.
        try:
            measure.evaluate_cohort_tripwire()
        except Exception:
            pass
    except Exception:
        # Fail-open: never crash a hook
        pass

    return 0


def _quality_cache_self_heal(measure) -> None:
    """Replicate the quality-cache dispatch's self-healing block.

    If the quality-cache hook is missing from settings.json and this is NOT a
    plugin install and quality_bar_disabled is unset, reinstall it. Mirrors
    hooks/posttooluse_runner.py:_quality_cache_self_heal and the original
    measure.py dispatch block. Fail-open: never raises.
    """
    if measure._is_running_from_plugin_cache() or measure._is_plugin_installed():
        return
    if not measure.CONFIG_PATH.exists():
        return
    try:
        _qb_cfg = json.loads(measure.CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        _qb_cfg = {}
    if _qb_cfg.get("quality_bar_disabled"):
        return
    # a FIFO at settings.json would block read_text() on the per-turn hot path
    if not measure._is_regular_file(measure.SETTINGS_PATH):
        return
    try:
        _sh_hooks = json.loads(measure.SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if not measure._quality_cache_hook_present(_sh_hooks):
        measure.setup_quality_bar(quiet=True)


if __name__ == "__main__":
    sys.exit(main())
