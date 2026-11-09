"""Rollup/dashboard spawning and stop-throttle state for the Cursor bridge.

Split out of cursor_hook_bridge.py under the repo's 1,000-line file-size
ratchet (scripts/check_file_size.py). The measure.py locator, the detaching
spawner, the data-dir resolver and the atomic writer are INJECTED by the
bridge's same-name wrappers at call time, so tests monkeypatching
cursor_hook_bridge._locate_measure_py / .spawn_detached / ._to_dir /
._atomic_write_json keep working.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_STOP_ROLLUP_SECS = 120             # R12: at most one stop rollup per machine per 120s
_STALE_LOCK_SECS = 7 * 24 * 3600    # sessionStart lock sweep threshold


def _measure_env(interactive=False):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_RUNTIME"] = "cursor"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if interactive:
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
    return env


# The installer does NOT copy measure.py into the plugin dir (version-drift
# risk); it writes a one-line "measure-path" locator next to this bridge naming
# the canonical measure.py in the checkout. Resolution order: a sibling
# measure.py (dev/test checkout), then the locator. Mirrors
# hermes_hook_bridge._locate_measure_py.


def _spawn_rollup(locate_measure_py, spawn_detached):
    # cursor-doctor --probe replays the installed command to prove it fires;
    # it must not trigger a real detached rollup/dashboard as a side effect.
    if os.environ.get("TOKEN_OPTIMIZER_PROBE") == "1":
        return
    measure = locate_measure_py()
    if measure is None:
        return
    try:
        spawn_detached(
            [sys.executable, str(measure), "cursor-rollup", "--quiet"],
            env=_measure_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("[cursor_hook_bridge] detached rollup spawn failed", exc_info=True)


def _spawn_dashboard(locate_measure_py, spawn_detached):
    # Same probe guard as _spawn_rollup(): replay must not spawn detached work.
    if os.environ.get("TOKEN_OPTIMIZER_PROBE") == "1":
        return
    measure = locate_measure_py()
    if measure is None:
        return
    try:
        spawn_detached(
            [sys.executable, str(measure), "dashboard", "--quiet"],
            env=_measure_env(interactive=True),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("[cursor_hook_bridge] detached dashboard spawn failed", exc_info=True)


def _stop_rollup_due_locked(state, now, atomic_write):
    last = None
    try:
        if state.exists():
            data = json.loads(state.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last = float(data.get("last", 0) or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        last = None
    if last is not None and now - last < _STOP_ROLLUP_SECS:
        return False
    # Fail closed: if the throttle-state write fails (disk full, read-only data
    # dir), do NOT spawn — otherwise every stop would spawn a rollup+dashboard
    # storm precisely under the degraded conditions the throttle is meant to
    # contain. _atomic_write_json returns True only on a successful write.
    return atomic_write(state, {"last": now})


def _sweep_stale_locks(to_dir):
    """Remove stale lock/candidate artifacts (7d) on sessionStart. Never fatal."""
    if to_dir is None:
        return
    lock_dir = to_dir / ".locks"
    if not lock_dir.is_dir():
        return
    now = time.time()
    try:
        for pattern in ("*.lock", ".*.lock.candidate-*"):
            for p in lock_dir.glob(pattern):
                try:
                    if now - p.stat().st_mtime > _STALE_LOCK_SECS:
                        p.unlink()
                except OSError:
                    continue
    except OSError:
        pass
