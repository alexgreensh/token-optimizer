#!/usr/bin/env python3
"""Token Optimizer v5: PreToolUse Bash Hook.

Rewrites safe, read-only CLI commands to pass through bash_compress.py.
Commands containing shell metacharacters are categorically excluded.

Exit behavior:
- No output = pass through (hook is transparent)
- JSON output = rewrite command via updatedInput
- Any error = exit silently (fail open)

Controlled by: TOKEN_OPTIMIZER_BASH_COMPRESS=0 to disable (default: ON)
"""

import json
import os
import shlex
import time
from pathlib import Path

from plugin_env import is_v5_flag_enabled, resolve_plugin_data_dir
from runtime_env import runtime_home

# The whitelist gate lives in a dependency-free module so the compression
# wrapper's startup self-check can import it without this module's
# environment-resolution import chain.
from bash_whitelist import (
    _DANGEROUS_CHARS,
    _GIT_WRITE_SUBCMDS,
    _SAFE_ENV_VARS,
    _WHITELIST_COMPOUND,
    _WHITELIST_SINGLE,
    has_dangerous_chars as _has_dangerous_chars_impl,
    is_whitelisted as _is_whitelisted_impl,
)


def _has_dangerous_chars(command_str):
    """Check if command contains shell metacharacters."""
    return _has_dangerous_chars_impl(command_str)


def _is_whitelisted(command_str):
    """Check if command matches the compression whitelist."""
    return _is_whitelisted_impl(command_str)


def _is_bash_compress_enabled():
    """Check if bash compression is enabled. Default ON since v5.5."""
    return is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=True)


def main():
    if not _is_bash_compress_enabled():
        return  # Feature disabled, exit silently

    try:
        from hook_io import read_stdin_hook_input
        payload = read_stdin_hook_input()
        if not payload:
            return
    except (json.JSONDecodeError, OSError, ImportError):
        return  # Bad input, exit silently

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        return

    # Issue #141: in worktree-isolated sessions (cwd under .claude/worktrees/),
    # Claude Code's isolation guard statically parses every Bash command and
    # REFUSES anything it can't classify as "simple" — the bash_compress
    # for-loop wrapper is refused as "too complex", so every whitelisted
    # command fails there. Skip the rewrite entirely: a rewrite is a guaranteed
    # refusal, so losing compression inside worktrees is the correct tradeoff.
    session_cwd = str(payload.get("cwd") or "").replace("\\", "/")
    if "/.claude/worktrees/" in session_cwd:
        return

    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")
    if not command:
        return

    # Categorical exclusion: shell metacharacters
    if _has_dangerous_chars(command):
        return

    # Whitelist check
    if not _is_whitelisted(command):
        return

    # Resolve bash_compress.py path from __file__ (stable, not from env vars).
    # CLAUDE_PLUGIN_ROOT is used for cross-checking only — we do not derive
    # the primary path from it to avoid env var injection attacks.
    script_dir = Path(__file__).resolve().parent
    compress_path = script_dir / "bash_compress.py"
    if not compress_path.exists():
        return  # Wrapper missing, exit silently

    # Route through python-launcher.sh so Windows Store shim / py launcher are handled.
    plugin_root = script_dir.parent.parent.parent
    launcher_path = plugin_root / "hooks" / "python-launcher.sh"
    if not launcher_path.exists():
        return  # Launcher missing, exit silently

    # Cross-check: when CLAUDE_PLUGIN_ROOT is set by the dispatcher, verify that
    # the __file__-derived paths land within the declared plugin root.  A mismatch
    # means the hook is running from a symlinked or relocated path and we should
    # fail closed rather than execute an unexpected binary.
    _env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if _env_root:
        try:
            _declared_root = Path(_env_root).resolve(strict=True)
            if not compress_path.is_relative_to(_declared_root):
                return  # compress_path outside declared root — refuse to run
            if not launcher_path.is_relative_to(_declared_root):
                return  # launcher_path outside declared root — refuse to run
        except (OSError, ValueError):
            return  # CLAUDE_PLUGIN_ROOT unresolvable — fail closed

    # Build rewritten command with proper quoting for each token
    try:
        original_tokens = shlex.split(command)
    except ValueError:
        return

    # Re-quote each token to handle paths with spaces safely (ARCH-F3).
    # Use the #80 bash-resolver form so the rewritten command survives a
    # stripped/empty PATH (Claude runs updatedInput under `/bin/sh -c`).
    #
    # CRITICAL: this rewrites the USER's real Bash tool command, not an internal
    # plugin hook. If no bash can be resolved (stripped PATH *and* bash absent
    # from every probed path), we must NOT `exit 0` — that returns success with
    # no output, and the agent reads it as "the command ran and produced nothing"
    # (e.g. `git status` -> clean tree) when it never ran at all. Instead, when
    # the resolver exhausts its candidates, fall through to running the ORIGINAL
    # command unchanged under the current shell: compression degrades to plain
    # execution, and a genuine failure still surfaces loudly. The leading `exec`
    # on a hit means this fallback only runs when no bash was found.
    #
    # Thread the real session id (delivered in the hook payload, ABSENT from the
    # wrapper's own env) into the wrapper process so bash_compress.py attributes
    # its savings event to THIS session instead of "". An empty session_id priced
    # every bash_compress event oneshot-only, dropping its reread annuity (GLM
    # current-week-undercount finding). The export sits INSIDE the `&&` chain,
    # right before `exec`, on the branch that found a bash -- so the string still
    # begins with `for b in bash` (a contract other consumers rely on) and the
    # var never depends on `env` resolving under a stripped PATH. Charset-gated +
    # shell-quoted; empty string (no-op) if the id is absent or looks unusual, so
    # the chain is byte-identical to before for a missing/odd id.
    _sid = str(payload.get("session_id", "") or "")
    _sid_export = ""
    if _sid and len(_sid) <= 64 and all(c.isalnum() or c in "._-" for c in _sid):
        _sid_export = "export CLAUDE_SESSION_ID=" + shlex.quote(_sid) + " && "
    rewritten = (
        'for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash /opt/homebrew/bin/bash; '
        'do command -v "$b" >/dev/null 2>&1 && ' + _sid_export + 'exec "$b" '
        + shlex.quote(str(launcher_path))
        + " " + shlex.quote(str(compress_path))
        + " " + " ".join(shlex.quote(t) for t in original_tokens)
        + '; done; ' + command
    )

    # Log rewrite event to sidecar JSONL.
    # Security: only log metadata (command name + arg count), never the raw command
    # text, which may contain package names, file paths, or other sensitive tokens.
    # Rotation: cap at 1MB; rotate to .1 (single rotated copy = 2MB max on disk).
    try:
        log_dir = resolve_plugin_data_dir() or (runtime_home() / "token-optimizer")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "bash-rewrites.jsonl"
        _MAX_LOG_BYTES = 1 * 1024 * 1024  # 1MB
        if log_path.exists() and log_path.stat().st_size >= _MAX_LOG_BYTES:
            rotated = log_path.with_suffix(".jsonl.1")
            log_path.replace(rotated)  # atomic rename; overwrites any existing .1
        tokens_split = command.split()
        event = json.dumps({
            "timestamp": time.time(),
            "command_name": tokens_split[0] if tokens_split else "",
            "arg_count": len(tokens_split) - 1,
            "compressed": True,
            "session_id": str(payload.get("session_id", ""))[:64],
        })
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(event + "\n")
    except Exception:
        pass  # Never fail on logging

    # Gate under context pressure (token-saving: suppressed only at critical)
    try:
        from context_pressure import should_inject, get_pressure_level, log_suppression
        sid = (payload.get("session_id") or "")[:64]
        if not should_inject(session_id=sid or None, priority="token-saving"):
            log_suppression("bash_rewrite", get_pressure_level(session_id=sid or None))
            return
    except Exception:
        pass

    # Emit updatedInput response
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "command": rewritten,
            },
        },
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
