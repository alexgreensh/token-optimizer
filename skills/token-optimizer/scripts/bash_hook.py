#!/usr/bin/env python3
"""Token Optimizer v5: PreToolUse Bash Hook.

Rewrites safe, read-only CLI commands to pass through bash_compress.py.
Commands containing shell metacharacters are categorically excluded.

Exit behavior:
- No output = pass through (hook is transparent)
- JSON output = rewrite command via updatedInput
- Any error = exit silently (fail open)

Controlled by: TOKEN_OPTIMIZER_BASH_COMPRESS=1 (default: OFF)
"""

import json
import os
import shlex
import sys
import time
from pathlib import Path

from plugin_env import is_v5_flag_enabled, resolve_plugin_data_dir

# Categorical exclusion: if ANY of these appear in the raw command string,
# never rewrite. Checked BEFORE shlex tokenization to catch all forms.
# Includes newlines/nulls to prevent multi-line command injection (SEC-F1).
_DANGEROUS_CHARS = frozenset(";|&`$(){}><\n\r\x00")

# Only these env var names are safe to pass through when stripping prefixes.
# LD_PRELOAD, DYLD_*, PATH etc. can be used for library injection (SEC-F2).
_SAFE_ENV_VARS = frozenset({
    "TERM", "LANG", "LC_ALL", "LC_CTYPE", "COLOR", "NO_COLOR", "FORCE_COLOR",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GIT_DIR", "GIT_WORK_TREE", "HOME", "USER", "LOGNAME",
})

# Commands eligible for compression (argv[0] or argv[0:2])
_WHITELIST_SINGLE = frozenset({
    "git", "pytest", "py.test", "jest", "vitest", "rspec", "ls", "find",
    # v5.1 lint handlers (read-only static analysis)
    "eslint", "flake8", "pylint", "shellcheck", "rubocop",
    # v5.1 logs handler (read-only log inspection)
    "tail", "journalctl",
    # v5.1 tree handler (read-only directory tree)
    "tree",
    # v5.1 build handler (type-check / bundler builds — read-only compile)
    "tsc", "webpack", "esbuild",
    # v5.1 extended test runners (read-only test execution)
    "mocha", "karma",
    # v5.5 read-only utilities
    "sqlite3", "wc", "du", "df", "printenv",
})
_WHITELIST_COMPOUND = {
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"), ("git", "branch"),
    ("python", "-m"), ("python3", "-m"),  # python -m pytest
    ("npx", "jest"), ("npx", "vitest"),
    ("npm", "install"), ("npm", "ci"), ("npm", "test"),
    ("pip", "install"), ("pip3", "install"),
    ("cargo", "test"), ("cargo", "build"),
    ("go", "test"),
    # v5.1 lint handlers (multi-word lint invocations)
    ("ruff", "check"),
    ("biome", "lint"),
    ("golangci-lint", "run"),
    # v5.1 progress handler (docker build/pull — read-only layer fetch)
    ("docker", "build"),
    ("docker", "pull"),
    # v5.1 list handlers (read-only inventory queries)
    ("pip", "list"), ("pip3", "list"),
    ("npm", "ls"),
    ("pnpm", "list"),
    ("docker", "ps"),
    ("brew", "list"),
    # v5.1 build handlers (multi-word build commands)
    ("vite", "build"),
    ("next", "build"),
    ("go", "build"),
    # v5.1 extended test runners (multi-word invocations)
    ("cypress", "run"),
    ("playwright", "test"),
    ("npx", "cypress"),
    ("npx", "playwright"),
    ("npx", "mocha"),
    ("npx", "karma"),
    # v5.5 docker/kubectl read-only inspection
    ("docker", "exec"),
    ("docker", "logs"),
    ("docker", "inspect"),
    ("kubectl", "get"),
    ("kubectl", "describe"),
    ("kubectl", "logs"),
}

# Git write commands that should NOT be compressed
_GIT_WRITE_SUBCMDS = frozenset({
    "commit", "push", "pull", "merge", "rebase", "reset", "checkout",
    "switch", "stash", "tag", "cherry-pick", "revert", "am", "apply",
    "add", "rm", "mv", "restore", "bisect", "clean", "fetch", "clone",
    "init", "remote", "submodule", "worktree",
})


def _has_dangerous_chars(command_str):
    """Check if command contains shell metacharacters."""
    for ch in command_str:
        if ch in _DANGEROUS_CHARS:
            return True
    return False


def _is_whitelisted(command_str):
    """Check if command matches the compression whitelist."""
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        return False  # malformed quoting

    if not tokens:
        return False

    # Strip leading env var assignments (VAR=val), only safe var names
    cmd_start = 0
    while cmd_start < len(tokens) and "=" in tokens[cmd_start] and not tokens[cmd_start].startswith("-"):
        var_name = tokens[cmd_start].split("=", 1)[0]
        if var_name not in _SAFE_ENV_VARS:
            return False  # Unsafe env var (e.g., LD_PRELOAD), reject entirely
        cmd_start += 1

    if cmd_start >= len(tokens):
        return False

    cmd = tokens[cmd_start]
    subcmd = tokens[cmd_start + 1] if cmd_start + 1 < len(tokens) else ""

    # Check compound whitelist first (more specific)
    if (cmd, subcmd) in _WHITELIST_COMPOUND:
        if cmd == "git" and subcmd in _GIT_WRITE_SUBCMDS:
            return False
        if cmd == "docker" and subcmd == "exec":
            remaining = tokens[cmd_start + 2:]
            if any(arg in ("-it", "-i", "-t") for arg in remaining):
                return False
        return True

    # Check single command whitelist
    if cmd in _WHITELIST_SINGLE:
        if cmd == "git":
            if subcmd in _GIT_WRITE_SUBCMDS or not subcmd:
                return False
            if subcmd not in ("status", "log", "diff", "show", "branch"):
                return False
        if cmd == "sqlite3":
            cmd_lower = command_str.lower()
            if any(w in cmd_lower for w in ("insert", "update", "delete", "drop", "alter", "create")):
                return False
        return True

    # Check for sudo/su prefix (never rewrite)
    if cmd in ("sudo", "su"):
        return False

    return False


def _is_bash_compress_enabled():
    """Check if bash compression is enabled. Default OFF — opt-in via flag/env."""
    return is_v5_flag_enabled("v5_bash_compress", "TOKEN_OPTIMIZER_BASH_COMPRESS", default=False)


def main():
    if not _is_bash_compress_enabled():
        return  # Feature disabled, exit silently

    try:
        payload = json.loads(sys.stdin.read(1_000_000))
    except (json.JSONDecodeError, OSError):
        return  # Bad input, exit silently

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
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

    # Resolve bash_compress.py path from __file__ (never from env vars)
    script_dir = Path(__file__).resolve().parent
    compress_path = script_dir / "bash_compress.py"
    if not compress_path.exists():
        return  # Wrapper missing, exit silently

    # Build rewritten command with proper quoting for each token
    try:
        original_tokens = shlex.split(command)
    except ValueError:
        return

    # Re-quote each token to handle paths with spaces safely (ARCH-F3)
    rewritten = "python3 " + shlex.quote(str(compress_path)) + " " + " ".join(shlex.quote(t) for t in original_tokens)

    # Log rewrite event to sidecar JSONL
    try:
        log_dir = resolve_plugin_data_dir() or (Path.home() / ".claude" / "token-optimizer")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "bash-rewrites.jsonl"
        event = json.dumps({
            "timestamp": time.time(),
            "command": command[:100],
            "session_id": payload.get("session_id", ""),
        })
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(event + "\n")
    except Exception:
        pass  # Never fail on logging

    # Emit updatedInput response
    response = {
        "hookSpecificOutput": {
            "permissionDecision": "allow",
            "updatedInput": {
                "command": rewritten,
            },
        },
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
