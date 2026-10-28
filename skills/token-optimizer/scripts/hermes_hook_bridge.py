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

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime_env import runtime_home
from spawn_utils import spawn_detached

# Dashboard port constant — single source of truth.
try:
    from hermes_doctor import DASHBOARD_PORT  # noqa: PLC0415
except Exception:
    DASHBOARD_PORT = 24844

logger = logging.getLogger(__name__)

# Windows: a console-subsystem child (python.exe, or an npm .cmd shim hosted by
# cmd.exe) allocates and flashes a console window unless the parent passes
# CREATE_NO_WINDOW. getattr -> 0 on POSIX, where creationflags=0 is an
# accepted no-op (CPython only rejects creationflags != 0 off-Windows).
# Fire-and-forget spawns go through spawn_utils.spawn_detached instead, whose
# DETACHED_PROCESS already suppresses console creation.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# measure.py location
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_MEASURE_PY = _SCRIPTS_DIR / "measure.py"

# v5.X.Y: the installer does NOT copy measure.py into the plugin dir
# (version-drift risk). Instead it writes a one-line "measure-path" locator
# next to this bridge, naming the canonical measure.py in the checkout. We read
# it after the sibling check, before the hardcoded fallbacks.
_MEASURE_LOCATOR = _SCRIPTS_DIR / "measure-path"

# Fallback search paths if the primary location is missing (e.g. the bridge
# is bundled into the install tree but measure.py is in the repo checkout).
_FALLBACK_PATHS: list[Path] = [
    runtime_home() / "skills" / "token-optimizer" / "scripts" / "measure.py",
    Path.home() / ".hermes" / "plugins" / "token-optimizer" / "measure.py",
]


_SENTINEL = object()  # "not yet resolved" marker for the locate cache
_locate_measure_py_cache: Path | None | object = _SENTINEL

# v5.11.1: emit the "measure.py not found - rollups paused" warning at
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
        # v5.11.1: verify the cached path still exists before returning it.
        # A measure.py that was moved/deleted after first resolution would
        # otherwise have us shell to a dead path forever; one cheap stat per
        # call re-resolves it instead.
        if _locate_measure_py_cache.is_file():
            return _locate_measure_py_cache
        _locate_measure_py_cache = _SENTINEL
    if _MEASURE_PY.is_file():
        _locate_measure_py_cache = _MEASURE_PY
        return _MEASURE_PY
    # Locator file written by hermes_install.py. Tolerate any read/parse
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
            creationflags=_NO_WINDOW,
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

def run_rollup(
    session_id: str = "", platform: str = "hermes", reason: str = "",
    context_tokens: int | None = None,
    skills: dict | None = None,
) -> None:
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
        # v5.11.1: a missing measure.py silently pauses rollups, which is
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
    if context_tokens is not None and context_tokens > 0:
        cmd += ["--context-tokens", str(int(context_tokens))]
    if skills:
        try:
            cmd += ["--skills-json", json.dumps(skills, ensure_ascii=True)]
        except (TypeError, ValueError):
            pass
    try:
        _proc = spawn_detached(
            cmd,
            env={**os.environ, "TOKEN_OPTIMIZER_RUNTIME": "hermes",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] run_rollup Popen error: %s", exc)
        return
    if _proc is None:
        logger.warning("[hermes_hook_bridge] run_rollup spawn_detached returned None")
    # Parity Gap 2: also refresh the dashboard on session-end so a Hermes user
    # sees current data without a manual open. Detached + INTERACTIVE=1 so it runs
    # UNBOUNDED off the hot path (never blocks, never truncates on the hook budget).
    try:
        spawn_detached(
            [sys.executable, str(measure_py), "dashboard", "--quiet"],
            env={**os.environ, "TOKEN_OPTIMIZER_RUNTIME": "hermes",
                 "TOKEN_OPTIMIZER_INTERACTIVE": "1",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] run_rollup dashboard regen error: %s", exc)


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
        _proc = spawn_detached(
            args,
            env={**os.environ, "TOKEN_OPTIMIZER_RUNTIME": "hermes",
                 "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] dashboard launch error: %s", exc)
        print(f"[Token Optimizer] Could not open dashboard: {exc}")
        return
    if _proc is None:
        logger.warning("[hermes_hook_bridge] run_dashboard spawn_detached returned None")
        print("[Token Optimizer] Could not open dashboard: spawn failed")


# ---------------------------------------------------------------------------
# Per-tool engines and session continuity (parity with Claude Code / Codex)
# ---------------------------------------------------------------------------
#
# The plugin runs in the Hermes process. Every engine call below runs in a
# short child process (the same scripts Claude Code's hooks run), so a slow or
# broken engine can never stall or crash Hermes. All helpers fail open: they
# return None / "" on any problem and the caller keeps the original output.

_HERMES_ENV = {"TOKEN_OPTIMIZER_RUNTIME": "hermes", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def scripts_dir() -> Path | None:
    """Directory holding measure.py and the engine scripts, or None."""
    measure_py = _locate_measure_py()
    return measure_py.parent if measure_py is not None else None


def _run_json(cmd: list[str], payload: dict, timeout: float) -> str:
    """Run ``cmd`` with ``payload`` as JSON on stdin; return stdout ("" on failure)."""
    try:
        data = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""
    try:
        result = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, float(timeout)),
            env={**os.environ, **_HERMES_ENV},
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        logger.debug("[hermes_hook_bridge] %s timed out", cmd[1:3])
        return ""
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] %s error: %s", cmd[1:3], exc)
        return ""
    return result.stdout or ""


def _last_json_object(text: str) -> dict | None:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def run_tool_hooks(event: str, payload: dict, timeout: float = 8.0) -> dict | None:
    """Run ``hermes_tool_hooks.py <event>`` on a Claude-Code-shaped payload.

    Returns the normalized result dict, or None when nothing applies or on any
    failure (missing script, timeout, bad output).
    """
    sdir = scripts_dir()
    if sdir is None:
        return None
    script = sdir / "hermes_tool_hooks.py"
    if not script.is_file():
        return None
    out = _run_json([sys.executable, str(script), event], payload, timeout)
    return _last_json_object(out)


def redaction_patterns(timeout: float = 10.0) -> list[str]:
    """Credential regexes for Hermes's redactor, loaded under the Hermes runtime."""
    res = run_tool_hooks("redaction-patterns", {}, timeout=timeout)
    pats = res.get("patterns") if isinstance(res, dict) else None
    return [p for p in (pats or []) if isinstance(p, str)]


def run_measure_context(args: list[str], payload: dict, timeout: float = 10.0) -> str:
    """Run a measure.py hook subcommand and return its additionalContext text.

    Used for ``compact-restore`` and ``prompt-continuity``, which read a hook
    payload on stdin and answer with a hookSpecificOutput envelope.
    """
    measure_py = _locate_measure_py()
    if measure_py is None:
        return ""
    out = _run_json([sys.executable, str(measure_py), *args], payload, timeout)
    obj = _last_json_object(out)
    if not obj:
        return ""
    hso = obj.get("hookSpecificOutput")
    ctx = hso.get("additionalContext") if isinstance(hso, dict) else None
    return ctx.strip() if isinstance(ctx, str) else ""


def run_turn_context(payload: dict, timeout: float = 10.0) -> dict | None:
    """Run ``measure.py hermes-turn-context``: continuity pointer, prior-session
    hint and lean-output nudge for one Hermes turn, in one child process."""
    measure_py = _locate_measure_py()
    if measure_py is None:
        return None
    out = _run_json([sys.executable, str(measure_py), "hermes-turn-context"], payload, timeout)
    obj = _last_json_object(out)
    return obj if isinstance(obj, dict) else None


def spawn_checkpoint(
    session_id: str, trigger: str, *, fill_pct: float | None = None, cwd: str | None = None,
) -> bool:
    """Write a checkpoint for a Hermes session in the background.

    Fire-and-forget ``measure.py hermes-checkpoint``; returns True when the
    child was started. Never blocks the Hermes turn.
    """
    if not session_id or not trigger:
        return False
    measure_py = _locate_measure_py()
    if measure_py is None:
        return False
    cmd = [sys.executable, str(measure_py), "hermes-checkpoint", "--quiet",
           "--session", str(session_id), "--trigger", str(trigger)]
    if fill_pct is not None:
        cmd += ["--fill-pct", f"{float(fill_pct):.1f}"]
    if cwd:
        cmd += ["--cwd", str(cwd)]
    try:
        proc = spawn_detached(
            cmd,
            env={**os.environ, **_HERMES_ENV},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.debug("[hermes_hook_bridge] spawn_checkpoint error: %s", exc)
        return False
    return proc is not None


def run_command_text(args: list[str], timeout: int = 20) -> str:
    """Run a read-only measure.py command for the slash command; return stdout."""
    return _run_measure(list(args), capture_output=True, timeout=timeout)


def to_claude_payload(
    tool_name: str,
    args: Any,
    *,
    session_id: str,
    tool_call_id: str = "",
    result: Any = None,
    cwd: str = "",
) -> list[dict]:
    """Translate a Hermes tool call into Claude-Code-shaped hook payloads.

    One Hermes call can map to several (a multi-file patch), so a list is
    returned. Tool output, when given, goes in ``tool_response``.
    """
    try:
        from hermes_session import hermes_tool_to_claude
    except Exception:
        return []
    out = []
    for cc_name, cc_input in hermes_tool_to_claude(tool_name, args):
        payload: dict = {
            "hook_event_name": "PostToolUse" if result is not None else "PreToolUse",
            "tool_name": cc_name,
            "tool_input": cc_input,
            "session_id": session_id,
            "tool_use_id": tool_call_id,
        }
        if cwd:
            payload["cwd"] = cwd
        if result is not None:
            payload["tool_response"] = result
        out.append(payload)
    return out


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
