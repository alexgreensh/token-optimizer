#!/usr/bin/env python3
"""Cross-platform hook dispatcher.

Invoked DIRECTLY via shebang on POSIX (file is +x) and via the Windows
Python Launcher (py.exe) file association on Windows. Not prefixed with
`python3` in hooks.json because `python3` is unreliable on Windows --
python.org installer ships `python.exe` + `py.exe`, not `python3.exe`,
so the prefix would make the wrapper a no-op for many Windows users.

The wrapper resolves the target script under CLAUDE_PLUGIN_ROOT, checks
it exists, and runs it with the same interpreter that ran this file
(sys.executable). On timeout we kill the child (Popen.kill) to avoid
leaking a process holding the trends.db lock. Always exits 0 so hook
failures never block the user's tool call.

Usage in hooks.json:
  "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\" <script-relative-path> [args...]"

Requires: POSIX file +x (git preserves mode on clone) and Python 3 on
Windows with the Python Launcher installed (default with python.org and
Microsoft Store installers).
"""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        return 0

    script_rel = sys.argv[1]
    script_args = sys.argv[2:]

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        script_path = os.path.join(plugin_root, script_rel)
    else:
        # Fallback: relative to this wrapper's parent directory.
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", script_rel)

    script_path = os.path.normpath(script_path)
    if not os.path.isfile(script_path):
        return 0

    # Use the interpreter that ran this wrapper so we inherit the correct
    # Python across macOS/Linux/Windows without relying on PATH.
    cmd = [sys.executable, script_path, *script_args]
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
