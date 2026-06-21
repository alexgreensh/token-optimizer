#!/usr/bin/env python3
"""Hermes hook bridge for Token Optimizer.

Thin shim that locates measure.py and shells into its subcommands (rollup,
summary, dashboard) on behalf of the Hermes plugin hooks and slash command.

Design mirrors codex_hook_bridge.py:
- Resolves measure.py via __file__ → scripts/ sibling, not hardcoded paths.
- Captures errors quietly and returns safe empty strings instead of raising.
- Never imports Hermes modules.

Assumption: this file lives in the same directory as measure.py (the scripts/
directory inside the Token Optimizer repo, or the same directory as __init__.py
in the Hermes install tree).  That directory is:
  ~/.hermes/plugins/token-optimizer/   (installed)
  skills/token-optimizer/scripts/      (repo checkout)

Either way, Path(__file__).parent / "measure.py" resolves correctly.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

# Dashboard port constant — single source of truth.
try:
    from hermes_doctor import DASHBOARD_PORT  # noqa: PLC0415
except Exception:
    DASHBOARD_PORT = 24844

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# measure.py location
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_MEASURE_PY = _SCRIPTS_DIR / "measure.py"

# v5.X.Y (#58): the installer does NOT copy measure.py into the plugin dir
# (version-drift risk). Instead it writes a one-line "measure-path" locator
# next to this bridge, naming the canonical measure.py in the checkout. We read
# it after the sibling check, before the hardcoded fallbacks.
_MEASURE_LOCATOR = _SCRIPTS_DIR / "measure-path"

# Fallback search paths if the primary location is missing (e.g. the bridge
# is bundled into the install tree but measure.py is in the repo checkout).
_FALLBACK_PATHS: list[Path] = [
    Path.home() / ".claude" / "skills" / "token-optimizer" / "scripts" / "measure.py",
    Path.home() / ".hermes" / "plugins" / "token-optimizer" / "measure.py",
]


_SENTINEL = object()  # "not yet resolved" marker for the locate cache
_locate_measure_py_cache: Path | None | object = _SENTINEL

# v5.11.1 (#58): emit the "measure.py not found - rollups paused" warning at
# most once per process so a paused install doesn't spam the Hermes terminal.
_rollup_missing_warned = False


def _locate_measure_py() -> Path | None:
    """Return the path to measure.py, or None if not found.

    Result is cached ONLY on success.  A None (measure.py not present at
    probe time) is NOT cached so subsequent calls retry the filesystem stat —
    important during install when the file may not yet exist.
    """
    global _locate_measure_py_cache
    if isinstance(_locate_measure_py_cache, Path):
        # v5.11.1 (#58): verify the cached path still exists before returning it.
        # A measure.py that was moved/deleted after first resolution would
        # otherwise have us shell to a dead path forever; one cheap stat per
        # call re-resolves it instead.
        if _locate_measure_py_cache.is_file():
            return _locate_measure_py_cache
        _locate_measure_py_cache = _SENTINEL
    if _MEASURE_PY.is_file():
        _locate_measure_py_cache = _MEASURE_PY
        return _MEASURE_PY
    # Locator file written by hermes_install.py (#58). Tolerate any read/parse
    # failure — a missing or garbage locator must never raise here.
    try:
        if _MEASURE_LOCATOR.is_file():
            located = Path(_MEASURE_LOCATOR.read_text(encoding="utf-8").strip())
            if located.is_file():
                _locate_measure_py_cache = located
                return located
    except (OSError, ValueError):
        pass
    for candidate in _FALLBACK_PATHS:
        if candidate.is_file():
            _locate_measure_py_cache = candidate
            return candidate
    logger.warning(
        "[hermes_hook_bridge] measure.py not found at %s or fallback paths. "
        "Rollup/summary/dashboard will be skipped.",
        _MEASURE_PY,
    )
    # Do NOT cache None — allow retry on next call.
    return None


# ---------------------------------------------------------------------------
# Internal subprocess helper
# ---------------------------------------------------------------------------

def _run_measure(args: list[str], *, capture_output: bool = True, timeout: int = 30) -> str:
    """Run `python3 measure.py <args>` and return stdout as a string.

    On any error returns an empty string without raising.
    """
    measure_py = _locate_measure_py()
    if measure_py is None:
        return ""
    cmd = [sys.executable, str(measure_py)] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "TOKEN_OPTIMIZER_RUNTIME": "hermes",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode != 0 and result.stderr:
            logger.debug(
                "[hermes_hook_bridge] measure.py %s exited %d: %s",
                args[0] if args else "",
                result.returncode,
                result.stderr[:200],
            )
        return (result.stdout or "").strip() if capture_output else ""
    except subprocess.TimeoutExpired:
        logger.debug("[hermes_hook_bridge] measure.py %s timed out", args[0] if args else "")
        return ""
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] measure.py %s error: %s", args[0] if args else "", exc)
        return ""


# ---------------------------------------------------------------------------
# Public API (called by hermes/__init__.py hooks and command handlers)
# ---------------------------------------------------------------------------

def run_rollup(session_id: str = "", platform: str = "hermes", reason: str = "") -> None:
    """Write a session rollup to TO's trends.db.

    Shells to: python3 measure.py hermes-rollup --session <id> [--reason <r>]
    Launched fire-and-forget (start_new_session=True) so it never blocks the
    Hermes terminal.  stdout/stderr are discarded (--quiet suppresses output).

    Never raises; errors are logged at DEBUG level only.
    """
    if not session_id:
        return
    measure_py = _locate_measure_py()
    if measure_py is None:
        # v5.11.1 (#58): a missing measure.py silently pauses rollups, which is
        # easy to miss. Surface it once to stderr with a remediation pointer.
        global _rollup_missing_warned
        if not _rollup_missing_warned:
            _rollup_missing_warned = True
            print(
                "[Token Optimizer] measure.py not found - rollups paused. "
                "Run hermes-doctor to diagnose.",
                file=sys.stderr,
            )
        return
    cmd = [sys.executable, str(measure_py), "hermes-rollup", "--quiet",
           "--session", session_id]
    if platform:
        cmd += ["--platform", platform]
    if reason:
        cmd += ["--reason", reason]
    try:
        subprocess.Popen(
            cmd,
            env={**os.environ, "TOKEN_OPTIMIZER_RUNTIME": "hermes",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] run_rollup Popen error: %s", exc)


def run_summary(session_id: str = "") -> str:
    """Return a text summary for the current or specified session.

    Shells to: python3 measure.py hermes-summary [--session <id>]

    Returns the summary string or an empty string on error.
    """
    args = ["hermes-summary"]
    if session_id:
        args += ["--session", session_id]
    return _run_measure(args, capture_output=True, timeout=15)


def run_dashboard(session_id: str = "", port: int = DASHBOARD_PORT) -> None:
    """Open / serve the Token Optimizer dashboard on the given port.

    Shells to: python3 measure.py dashboard [--port <port>]

    This is a long-running command; we launch it without capturing output and
    without waiting, so the CLI handler returns immediately.
    """
    measure_py = _locate_measure_py()
    if measure_py is None:
        print("[Token Optimizer] measure.py not found; cannot open dashboard.")
        return
    args = [sys.executable, str(measure_py), "dashboard", "--port", str(port)]
    if session_id:
        args += ["--session", session_id]
    try:
        subprocess.Popen(
            args,
            env={**os.environ, "TOKEN_OPTIMIZER_RUNTIME": "hermes",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] dashboard launch error: %s", exc)
        print(f"[Token Optimizer] Could not open dashboard: {exc}")


if __name__ == "__main__":
    try:
        from utf8_io import enforce_utf8_io
        enforce_utf8_io()
    except Exception:
        pass
    # Quick smoke test: locate measure.py and print status.
    p = _locate_measure_py()
    if p:
        print(f"[OK] measure.py found at {p}")
    else:
        print("[WARN] measure.py not found")
        sys.exit(1)
