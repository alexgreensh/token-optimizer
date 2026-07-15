#!/usr/bin/env python3
"""Cross-platform hook dispatcher.

Invoked from hooks.json via a small bash launcher that locates a usable
Python 3 interpreter on macOS, Linux, and Windows:

  "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh\" \"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\" <script-relative-path> [args...]"

The launcher handles Windows-specific gotchas (Program Files spaced paths,
Microsoft Store zero-byte stubs in WindowsApps, py launcher fallback) so
this file can assume it's running under a real Python 3.9+.

This dispatcher resolves the target script under CLAUDE_PLUGIN_ROOT,
checks it exists, and runs it with the same interpreter (sys.executable).
On timeout we kill the child (Popen.kill) to avoid leaking a process
holding the trends.db SQLite lock. Always exits 0 so hook failures never
block the user's tool call.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

# Defense in depth: the launcher script already filters interpreters, but
# if a user's PATH has a stale Python 3.7 that slipped through, bail early
# so later imports don't explode with confusing SyntaxError noise.
if sys.version_info < (3, 9):
    sys.exit(0)


# Module-level handle so the signal handler can reach the active child when
# Claude Code (or any parent) sends SIGTERM/SIGINT to run.py itself. Without
# this, an external kill reaps run.py but orphans the measure.py grandchild,
# which keeps the inherited stdout pipe open and makes the parent hang waiting
# for EOF (the multi-minute stop-hook hang).
_child_proc: subprocess.Popen | None = None


def _forward_and_exit(signum, frame):
    """Forward SIGTERM/SIGINT to the child's process group, then exit.

    The child is started with ``start_new_session=True`` so it leads its own
    process group; killing the group reaps any grandchildren (the launcher
    chain uses ``exec``, so run.py's PID is the one the host tracks). Falls
    back to a single ``proc.kill()`` on platforms without ``os.killpg`` or
    when the group is already gone.
    """
    global _child_proc
    if _child_proc is not None and _child_proc.poll() is None:
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(_child_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    _child_proc.kill()
                except OSError:
                    pass
        else:
            try:
                _child_proc.kill()
            except OSError:
                pass
    os._exit(0)


def _check_consent() -> bool:
    """Return True if consent is given or assumed. Fail-open on any error."""
    try:
        home = Path.home()

        # Resolve config path from env (set by Claude Code before hook invocation)
        plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if plugin_data:
            pd = Path(plugin_data).resolve()
            if not str(pd).startswith(str(home)):
                return True  # Path outside home = skip (fail-open)
            config_path = pd / "config" / "config.json"
        else:
            # Legacy / Codex fallback
            codex_home = os.environ.get("CODEX_HOME", "")
            if codex_home:
                ch = Path(codex_home).resolve()
                if not str(ch).startswith(str(home)):
                    return True
                config_path = ch / "token-optimizer" / "config.json"
            else:
                # Honor CLAUDE_CONFIG_DIR (Claude Code's official config-dir
                # override) before falling back to ~/.claude. Mirrors
                # runtime_env.claude_home(): accept any absolute, existing,
                # non-symlink directory (CLAUDE_CONFIG_DIR may legitimately live
                # OUTSIDE $HOME — containers, CI), reject relative/symlink, else
                # fall back. The previous str.startswith($HOME) check both
                # excluded valid out-of-home dirs and sibling-prefix-matched
                # (/Users/alex-evil passing for /Users/alex).
                claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
                cc = None
                if claude_config:
                    candidate = Path(claude_config).expanduser()
                    try:
                        if candidate.is_absolute() and candidate.is_dir() and not candidate.is_symlink():
                            cc = candidate.resolve()
                    except OSError:
                        cc = None
                if cc is not None:
                    config_path = cc / "token-optimizer" / "config.json"
                else:
                    config_path = home / ".claude" / "token-optimizer" / "config.json"

        if not config_path.exists() or config_path.is_symlink():
            return True  # No config or symlink = fail-open

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        if config.get("enterprise_consent_shown"):
            return True

        # Backward compat backfill: existing users who saw v5 welcome have implicitly consented
        if config.get("v5_welcome_shown"):
            config["enterprise_consent_shown"] = True
            # Atomic write (tempfile + os.replace)
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(config, tf, indent=2)
                os.replace(tmp, str(config_path))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return True

        return False  # No consent and no v5_welcome_shown = skip data collection
    except Exception:
        return True  # Fail-open: never block on errors


def main() -> int:
    if len(sys.argv) < 2:
        return 0

    script_rel = sys.argv[1]
    script_args = sys.argv[2:]

    rel_path = Path(script_rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        root_path = Path(plugin_root)
    else:
        # Fallback: relative to this wrapper's parent directory.
        root_path = Path(__file__).resolve().parent.parent

    try:
        root_resolved = root_path.resolve(strict=True)
        candidate = root_resolved / rel_path
        if not candidate.is_file():
            return 0
        script_path = candidate.resolve(strict=True)
        if not script_path.is_relative_to(root_resolved):
            return 0
    except (OSError, ValueError):
        return 0

    # Use the interpreter that ran this wrapper so we inherit the correct
    # Python across macOS/Linux/Windows without relying on PATH.
    #
    # Dispatch through module_runner.py rather than running script_path
    # directly: CPython never caches __pycache__ bytecode for a script run as
    # __main__, only for imported modules, so a direct `python script_path`
    # recompiles the target from source on every single hook invocation. For
    # measure.py (35k+ lines) that is ~0.3s of pure parse/compile paid on
    # nearly every tool call. module_runner.py runs it as a module instead, so
    # the import system's normal bytecode cache applies. See module_runner.py.
    module_runner = Path(__file__).resolve().parent / "module_runner.py"
    cmd = [sys.executable, str(module_runner), str(script_path.parent), script_path.stem, *script_args]

    # Consent gate: skip data collection until acknowledged.
    # EXEMPT: ensure-health and consent commands bootstrap the consent flag itself.
    # Blocking them creates a deadlock (config.json exists without flags -> ensure-health
    # can't run -> flags never written -> plugin permanently inert).
    exempt_commands = {"ensure-health", "consent", "v5"}
    is_exempt = any(arg in exempt_commands for arg in script_args[:2])
    if not is_exempt and not _check_consent():
        return 0

    # Force UTF-8 in every dispatched script regardless of the host locale, so
    # non-ASCII session paths / transcript content (Hebrew, CJK, accented names)
    # never crash a hook with UnicodeDecode/EncodeError. PYTHONUTF8 also makes the
    # child's default open() encoding UTF-8; PYTHONIOENCODING covers its std streams.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    proc = None
    global _child_proc
    # Install SIGTERM/SIGINT handlers BEFORE spawning the child so an external
    # kill from Claude Code reaps the whole child process group instead of
    # orphaning the grandchild.
    signal.signal(signal.SIGTERM, _forward_and_exit)
    signal.signal(signal.SIGINT, _forward_and_exit)
    try:
        # start_new_session=True puts the child in its own process group so a
        # timeout/external kill can reap the whole group (grandchildren included)
        # via os.killpg. Do NOT add stdout=/stderr=/stdin= here: several hooks
        # inject via stdout and MUST inherit run.py's stdio.
        proc = subprocess.Popen(cmd, env=child_env, start_new_session=True)
        _child_proc = proc
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            # Important: Popen.wait doesn't auto-kill on timeout. Leaving
            # the child alive would leak a process holding the trends.db
            # SQLite lock, starving the next hook invocation. Kill the whole
            # process group so any grandchildren die with the child.
            try:
                if proc.poll() is None:
                    if hasattr(os, "killpg"):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            proc.kill()
                    else:
                        proc.kill()
                proc.wait(timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
    except (subprocess.SubprocessError, OSError):
        if proc is not None:
            try:
                proc.kill()
            except (subprocess.SubprocessError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
