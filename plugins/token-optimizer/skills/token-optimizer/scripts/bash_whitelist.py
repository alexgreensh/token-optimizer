#!/usr/bin/env python3
"""Shared command whitelist gate for the Bash compression path.

Dependency-free by design: this module is imported by bash_compress.py's
startup self-check, which runs on every whitelisted Bash command, so it must
not pull in any module that does filesystem or environment resolution at
import time. Heavier concerns (plugin data dirs, runtimes) stay in their own
modules and are consulted by callers, not here.
"""

import shlex

# Categorical exclusion: if ANY of these appear in the raw command string,
# never rewrite. Checked BEFORE shlex tokenization to catch all forms.
# Includes newlines/nulls to prevent multi-line command injection.
_DANGEROUS_CHARS = frozenset(";|&`$(){}><\n\r\x00")

# Only these env var names are safe to pass through when stripping prefixes.
# LD_PRELOAD, DYLD_*, PATH etc. can be used for library injection.
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
    # v5.8 additional test runners (read-only test execution)
    "tox", "nox", "ava", "gradle", "gradlew", "mvn", "deno", "bun",
    # v5.5 read-only utilities
    "sqlite3", "wc", "du", "df",
    # v5.8 JSON/CSV handlers (read-only data inspection)
    "jq", "yq", "csvtool", "mlr", "csvcut",
    # v5.8 cloud CLI handlers (read-only inventory queries)
    "gcloud", "aws", "az",
    # v5.9 search results handler (read-only code search)
    "grep", "rg", "ag", "ack",
})
_WHITELIST_COMPOUND = {
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"), ("git", "branch"),
    ("python", "-m"), ("python3", "-m"),  # python -m pytest
    ("npx", "jest"), ("npx", "vitest"),
    # NOTE: npm install, npm ci, pip install, pip3 install, cargo build, docker build
    # are intentionally excluded. They execute postinstall/build scripts, produce
    # security-relevant output (vulnerability warnings, deprecation notices), and
    # are NOT read-only. Silent compression could hide important errors.
    ("npm", "test"),
    ("cargo", "test"),
    ("go", "test"),
    # v5.1 lint handlers (multi-word lint invocations)
    ("ruff", "check"),
    ("biome", "lint"),
    ("golangci-lint", "run"),
    # v5.1 progress handler (docker pull — read-only layer fetch)
    # docker build excluded: executes Dockerfile RUN instructions (write side-effects)
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
    # v5.8 additional test runners (multi-word invocations)
    ("npx", "ava"),
    ("gradle", "test"),
    ("gradlew", "test"),
    ("mvn", "test"),
    ("deno", "test"),
    ("bun", "test"),
    # v5.5 docker/kubectl read-only inspection
    ("docker", "logs"),
    ("docker", "inspect"),
    ("kubectl", "get"),
    ("kubectl", "describe"),
    ("kubectl", "logs"),
    # v5.8 kubectl extended read-only queries
    ("kubectl", "top"),
    ("kubectl", "events"),
    # v5.8 JSON inspection via node/deno/bun (read-only)
    # ("node", "-e") — intentionally excluded: arbitrary code execution
}

# Git write commands that should NOT be compressed
_GIT_WRITE_SUBCMDS = frozenset({
    "commit", "push", "pull", "merge", "rebase", "reset", "checkout",
    "switch", "stash", "tag", "cherry-pick", "revert", "am", "apply",
    "add", "rm", "mv", "restore", "bisect", "clean", "fetch", "clone",
    "init", "remote", "submodule", "worktree",
})


def has_dangerous_chars(command_str):
    """Check if command contains shell metacharacters."""
    for ch in command_str:
        if ch in _DANGEROUS_CHARS:
            return True
    return False


def is_whitelisted(command_str):
    """Check if command matches the compression whitelist."""
    # U9: consult user TOML command filters. User exclude overrides the
    # built-in whitelist (a user can remove "git status" from compression);
    # user add extends it (a user can add "cargo test" with a handler). The
    # _is_safe_add gate already ran in the loader, so add entries are
    # read-only and handler-validated. Categorical exclusions (dangerous
    # chars, git write subcmds, interpreters) are enforced by the caller
    # BEFORE this function and are not affected by user config.
    eff = None
    try:
        from command_filters import get_effective_filters
        eff = get_effective_filters()
        if eff.is_user_excluded(command_str):
            return False
    except Exception:
        pass

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
        if cmd == "kubectl":
            remaining = tokens[cmd_start + 2:]
            if any(arg == "secret" or arg == "secrets" or arg.startswith("secret/") or arg.startswith("secrets/") for arg in remaining):
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
            remaining = tokens[cmd_start + 1:]
            if any(t.startswith(".") for t in remaining):
                return False
        return True

    # Never rewrite shell interpreters or privilege-escalation wrappers (also prevents recursion on rewritten commands).
    if cmd in ("bash", "sh", "zsh", "dash", "fish", "sudo", "su"):
        return False

    # U9: user add entries extend the whitelist. The _is_safe_add gate already
    # ran in the loader (command_filters.load_filters), so the entry is
    # read-only and mapped to a known handler. Built-in detection ran first,
    # so user rules only extend, never replace a built-in handler.
    try:
        if eff is not None:
            add_entry = eff.find_user_add(command_str)
            if add_entry is not None:
                return True
    except Exception:
        pass

    return False
