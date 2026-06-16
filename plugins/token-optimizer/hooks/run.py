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
import subprocess
import sys
from pathlib import Path

# Defense in depth: the launcher script already filters interpreters, but
# if a user's PATH has a stale Python 3.7 that slipped through, bail early
# so later imports don't explode with confusing SyntaxError noise.
if sys.version_info < (3, 9):
    sys.exit(0)


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
                # override) before falling back to ~/.claude.
                claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "")
                if claude_config:
                    cc = Path(claude_config).resolve()
                    if not str(cc).startswith(str(home)):
                        return True  # Path outside home = skip (fail-open)
                    config_path = cc / "token-optimizer" / "config.json"
                else:
                    config_path = home / ".claude" / "token-optimizer" / "config.json"

        if not config_path.exists() or config_path.is_symlink():
            return True  # No config or symlink = fail-open

        with open(config_path) as f:
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
                with os.fdopen(fd, "w") as tf:
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
    cmd = [sys.executable, str(script_path), *script_args]

    # Consent gate: skip data collection until acknowledged.
    # EXEMPT: ensure-health and consent commands bootstrap the consent flag itself.
    # Blocking them creates a deadlock (config.json exists without flags -> ensure-health
    # can't run -> flags never written -> plugin permanently inert).
    exempt_commands = {"ensure-health", "consent", "v5"}
    is_exempt = any(arg in exempt_commands for arg in script_args[:2])
    if not is_exempt and not _check_consent():
        return 0

    proc = None
    try:
        proc = subprocess.Popen(cmd)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            # Important: Popen.wait doesn't auto-kill on timeout. Leaving
            # the child alive would leak a process holding the trends.db
            # SQLite lock, starving the next hook invocation.
            try:
                # Guard: check if process already exited between TimeoutExpired
                # and this point — avoids killing a reused PID on some POSIX impl.
                if proc.poll() is None:
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
