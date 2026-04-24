#!/usr/bin/env python3
"""Cross-platform hook dispatcher.

Invoked from hooks.json via a small bash launcher that locates a usable
Python 3 interpreter on macOS, Linux, and Windows:

  "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh\" \"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\" <script-relative-path> [args...]"

The launcher handles Windows-specific gotchas (Program Files spaced paths,
Microsoft Store zero-byte stubs in WindowsApps, py launcher fallback) so
this file can assume it's running under a real Python 3.8+.

This dispatcher resolves the target script under CLAUDE_PLUGIN_ROOT,
checks it exists, and runs it with the same interpreter (sys.executable).
On timeout we kill the child (Popen.kill) to avoid leaking a process
holding the trends.db SQLite lock. Always exits 0 so hook failures never
block the user's tool call.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Defense in depth: the launcher script already filters interpreters, but
# if a user's PATH has a stale Python 3.7 that slipped through, bail early
# so later imports don't explode with confusing SyntaxError noise.
if sys.version_info < (3, 8):
    sys.exit(0)


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
