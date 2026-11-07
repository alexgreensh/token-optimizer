#!/usr/bin/env python3
"""User-editable TOML command-filter schema + loader.

Lets a user opt NEW read-only commands into existing compression handlers
and exclude commands from compression, without weakening safety. The loader
is pure stdlib (tomllib with a no-tomllib fallback), performs no file writes,
and gates every ``add`` entry through ``_is_safe_add`` before it reaches the
effective allowlist.

File location: ``<runtime-home>/token-optimizer/command-filters.toml``
(sibling of the existing ``config.json``), overridable via
``TOKEN_OPTIMIZER_COMMAND_FILTERS``.

Schema (illustrative)::

    # Add read-only commands to an existing handler.
    # pattern_name = { command = "...", handler = "git_status|pytest|lint|..." }
    [filters.add]
    my-tests = { command = "cargo test", handler = "pytest" }
    my-lint = { command = "ruff check", handler = "lint" }

    # Exclude commands from compression (glob patterns supported).
    [filters.exclude]
    commands = ["git status", "ls -la"]

Rules:
  * ``add`` entries are appended only if they pass ``_is_safe_add``
    (read-only, not a shell interpreter, not sudo/su, no write subcommand).
  * ``exclude`` entries remove commands from the effective set.
  * Categorical exclusions (dangerous chars, git write subcmds, interpreters)
    are enforced downstream in bash_hook/bash_compress, never overridden
    here.
  * A missing file is a no-op (empty config). Malformed TOML is fail-open
    (empty config + one-time stderr warning).
"""

from __future__ import annotations

import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_FILTERS_PATH = "TOKEN_OPTIMIZER_COMMAND_FILTERS"
DEFAULT_FILTERS_FILENAME = "command-filters.toml"

# Valid handler names (the keys of bash_compress._PATTERN_HANDLERS). Listed
# here so the loader can reject unknown handlers without importing
# bash_compress (which would create a circular dependency on the hot path).
VALID_HANDLERS = frozenset({
    "git_status", "git_log", "git_diff", "pytest", "jest", "npm_install",
    "ls", "lint", "logs", "tree", "progress", "list", "build", "sqlite3",
    "disk_stats", "docker_output", "json", "csv", "stack_trace", "k8s",
    "cloud_cli", "search_results", "git_log_basic", "find", "find_raw",
})

# Commands that are NEVER safe to add (shell interpreters, privilege
# escalation, destructive utilities). Mirrors bash_hook's safety gates.
_UNSAFE_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
    "python", "python3", "python2", "ruby", "perl", "node", "deno", "bun",
    "lua", "tcl", "awk", "sed", "eval", "exec", "source", ".",
    "osascript", "php", "gawk", "mawk", "nawk", "rscript",
})
_UNSAFE_PREFIXES = frozenset({"sudo", "su", "doas", "pkexec", "gksudo"})
_UNSAFE_WRITE_SUBCMDS = frozenset({
    "rm", "mv", "cp", "chmod", "chown", "chgrp", "mkdir", "rmdir", "ln",
    "dd", "mkfs", "fdisk", "mount", "umount", "kill", "killall", "pkill",
    "shutdown", "reboot", "halt", "poweroff",
})
_DANGEROUS_CHARS = frozenset(";|&`$(){}><\n\r\x00")

# git subcommands that write. Superset of bash_whitelist._GIT_WRITE_SUBCMDS
# (a test pins the superset relation; importing it here would create a
# circular dependency on the hot path). Bounded, not a full git grammar:
# ambiguous subcommands that can write with the right flag (``branch -D``,
# ``config``, ``reflog delete``) are treated as writes outright.
_GIT_WRITE_SUBCMDS = frozenset({
    "commit", "push", "pull", "merge", "rebase", "reset", "checkout",
    "switch", "stash", "tag", "cherry-pick", "revert", "am", "apply",
    "add", "rm", "mv", "restore", "bisect", "clean", "fetch", "clone",
    "init", "remote", "submodule", "worktree",
    "branch", "config", "gc", "prune", "reflog", "notes", "replace",
    "filter-branch", "update-ref", "symbolic-ref", "repack", "write-tree",
    "commit-tree", "update-index", "read-tree", "hash-object",
})
# git subcommands treated as read-only. Anything outside
# _GIT_WRITE_SUBCMDS | _GIT_READONLY_SUBCMDS (aliases, plumbing, new
# subcommands) is refused, matching the compress hook's own stance that an
# unknown git invocation is not provably read-only.
_GIT_READONLY_SUBCMDS = frozenset({
    "status", "log", "diff", "show", "ls-files", "ls-tree", "blame",
    "annotate", "grep", "rev-parse", "rev-list", "describe", "shortlog",
    "show-ref", "cat-file", "count-objects", "var", "version", "whatchanged",
})
# find flags that delete, write or run another program.
_FIND_DESTRUCTIVE_FLAGS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprint0", "-fls", "-fprintf",
})
_GLOB_CHARS = frozenset("*?[")
# Runner / wrapper commands: they exec another command (``timeout 5 rm -rf x``,
# ``nice rm x``, ``xargs rm``), so the real command is not argv[0]. Refused
# outright (fail closed) rather than unwrapped, since each has its own option
# grammar. Windows shells are interpreters too.
_LAUNCHERS = frozenset({
    "timeout", "nice", "ionice", "nohup", "stdbuf", "time", "chrt", "taskset",
    "watch", "xargs", "parallel", "command", "exec", "builtin", "setsid",
    "unbuffer", "strace", "ltrace", "nsenter", "chroot", "flock", "busybox",
    "script", "runuser", "systemd-run", "at", "batch", "ssh", "wsl", "cmd",
    "powershell", "pwsh", "start", "sh.exe",
})
# Minimum set of known writers that are not in _UNSAFE_WRITE_SUBCMDS. This is
# a bounded denylist, not a claim that every other command is read-only.
_KNOWN_WRITERS = frozenset({
    "tee", "truncate", "shred", "touch", "unlink", "install", "rsync", "patch",
    "sponge", "scp", "rename", "del", "erase", "rd", "move", "copy", "ren",
})
_NEVER_COMPRESS_CMD0 = (
    _UNSAFE_INTERPRETERS | _UNSAFE_PREFIXES | _UNSAFE_WRITE_SUBCMDS
    | _LAUNCHERS | _KNOWN_WRITERS
)
_EXE_SUFFIXES = (".exe", ".com", ".bat", ".cmd")
# fnmatch candidates: bare names plus their Windows executable spellings, so
# ``rm.*`` / ``r*.exe`` are caught as well as ``r*``.
_GLOB_NAMES = frozenset(
    n + ext for n in _NEVER_COMPRESS_CMD0 for ext in ("",) + _EXE_SUFFIXES
)

_warned_paths: set = set()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AddEntry:
    """A user-declared add rule: map a command to an existing handler."""
    name: str
    command: str
    handler: str


@dataclass(frozen=True)
class CommandFilterConfig:
    """Normalized filter config loaded from TOML."""
    adds: Tuple[AddEntry, ...] = ()
    excludes: Tuple[str, ...] = ()

    def is_excluded(self, command_str: str) -> bool:
        """Return True if ``command_str`` matches any exclude glob/pattern."""
        for pattern in self.excludes:
            if fnmatch.fnmatch(command_str, pattern):
                return True
        return False

    def find_add(self, command_str: str) -> Optional[AddEntry]:
        """Return the first add entry whose command matches ``command_str``,
        or None. Never matches a destructive command."""
        if is_destructive_command(command_str):
            return None
        for entry in self.adds:
            if entry.command == command_str:
                return entry
            # Also support glob matching on the add command.
            if fnmatch.fnmatch(command_str, entry.command):
                return entry
        return None


@dataclass(frozen=True)
class EffectiveFilters:
    """The merged result of builtin + user filters, ready for bash_hook /
    bash_compress to consult on the hot path."""
    user_adds: Tuple[AddEntry, ...] = field(default_factory=tuple)
    user_excludes: Tuple[str, ...] = field(default_factory=tuple)

    def is_user_excluded(self, command_str: str) -> bool:
        for pattern in self.user_excludes:
            if fnmatch.fnmatch(command_str, pattern):
                return True
        return False

    def find_user_add(self, command_str: str) -> Optional[AddEntry]:
        # A glob in an add entry must never make a destructive command
        # eligible for compression (the wrapper would run it).
        if is_destructive_command(command_str):
            return None
        for entry in self.user_adds:
            if entry.command == command_str:
                return entry
            if fnmatch.fnmatch(command_str, entry.command):
                return entry
        return None


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------

def _is_all_wildcard(token: str) -> bool:
    """True when ``token`` is a non-empty run of only ``*`` (e.g. ``*``/``**``).
    Such a pattern matches every command via fnmatch and is far too broad to
    opt into a compression handler."""
    return bool(token) and set(token) <= {"*"}


def _effective_cmd_token(tokens: List[str]) -> Optional[str]:
    """Return the token of the REAL command being run.

    Normalizes away the path (so ``/bin/bash``, ``./python``, ``/usr/bin/sudo``
    are recognized as ``bash``/``python``/``sudo``) and resolves a leading
    ``env`` invocation by skipping ``VAR=val`` assignments and env's own options
    (``-i``, and the operand-taking ``-u``/``-C``/``--chdir``) to reach the
    command env would exec, looping through nested ``env``. ``-S`` /
    ``--split-string`` and any unrecognized ``-`` option fail closed (None).
    Returns None when no command token remains.
    """
    if not tokens:
        return None
    i = 0
    # ``env`` may be nested (``env env rm x``) and may carry assignments and
    # options before the real command, so unwrap repeatedly. Any option token
    # this parser does not model exactly -- ``--opt=val``, joined
    # ``-uVAL``/``-CVAL``/``-SVAL``, or ``-S``/``--split-string`` whose operand
    # is itself a command string -- fails closed by returning None rather
    # than guessing which later token is the command.
    while i < len(tokens) and _norm_cmd(tokens[i]) == "env":
        i += 1
        while i < len(tokens):
            tok = tokens[i]
            if "=" in tok and not tok.startswith("-"):
                i += 1  # VAR=val assignment
                continue
            if tok in ("-i", "--ignore-environment", "-", "-v", "--debug"):
                i += 1
                continue
            if tok in ("-u", "--unset", "-C", "--chdir"):
                i += 2  # option consumes its operand (``--`` included: it is
                # a legal operand, e.g. ``env -u -- rm`` unsets ``--``)
                continue
            if tok == "--":
                i += 1
                break  # env end-of-options: the next token is the command
            if tok.startswith("-"):
                return None
            break  # first non-option, non-assignment token = the command
    if i >= len(tokens):
        return None
    return tokens[i]


def _effective_cmd0(tokens: List[str]) -> Optional[str]:
    tok = _effective_cmd_token(tokens)
    return None if tok is None else _norm_cmd(tok)


def _raw_cmd_word(tokens: List[str]) -> str:
    """Lower-cased basename of the command word, backslash paths included,
    WITHOUT stripping an executable suffix (so ``rm.*`` stays a glob)."""
    cmd = _effective_cmd_token(tokens)
    return os.path.basename(cmd.replace("\\", "/")).lower() if cmd else ""


def _norm_cmd(token: str) -> str:
    """Basename, lower-cased, without a Windows executable suffix, so
    ``RM``, ``/bin/rm`` and ``rm.exe`` all compare as ``rm``."""
    name = os.path.basename(token.replace("\\", "/")).lower()
    for ext in (".exe", ".com", ".bat", ".cmd"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _git_subcommand_tokens(tokens: List[str]) -> List[str]:
    """Return the tokens after ``git``'s global options, starting at the
    subcommand word (skips ``-C <dir>``, ``-c k=v``, ``--git-dir <d>``...)."""
    idx = next((i for i, t in enumerate(tokens) if _norm_cmd(t) == "git"), None)
    if idx is None:
        return []
    rest = tokens[idx + 1:]
    j = 0
    while j < len(rest) and rest[j].startswith("-"):
        j += 2 if rest[j] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    return rest[j:]


def _git_has_config_options(tokens: List[str]) -> bool:
    """True when git global options other than ``-C <dir>`` / ``--no-pager``
    / ``--no-optional-locks`` precede the subcommand. ``-c alias.x=!cmd``,
    ``--config-env``, ``--exec-path`` and friends can run arbitrary programs."""
    idx = next((i for i, t in enumerate(tokens) if _norm_cmd(t) == "git"), None)
    if idx is None:
        return False
    rest = tokens[idx + 1:]
    j = 0
    while j < len(rest) and rest[j].startswith("-"):
        if rest[j] == "-C":
            j += 2
        elif rest[j] in ("--no-pager", "--no-optional-locks"):
            j += 1
        else:
            return True
    return False


def is_destructive_command(command_str: str) -> bool:
    """True when ``command_str`` runs something that writes, deletes or
    launches another program (rm, mv, sudo, an interpreter, a git write
    subcommand, find -delete/-exec, ...).

    Used at match time so a glob in a user ``add`` entry can never make such
    a command eligible for compression, whatever the glob expands to.
    Fails closed: an unparseable command counts as destructive, and unknown
    git subcommands (possible aliases) count as writes.

    Bounded by design: this module ships inside the Devin payload closed
    set, so it cannot depend on the hook's read-only classifier
    (pipeline_analyzer); the closed-set policy test forbids that import.
    The checks are self-contained denylists, not a full shell parser;
    commands they do not recognize stay eligible for user ``add`` entries,
    as before.
    """
    # Multi-line input hides extra commands (``mytool --flag\nrm -rf x``)
    # and command/process substitution executes whatever it nests
    # (``echo $(rm -rf x)``, ``cat <(rm -rf x)``): refuse on the raw string.
    if any(c in command_str for c in ("\n", "\r")):
        return True
    if "$(" in command_str or "`" in command_str or "<(" in command_str:
        return True
    import shlex
    try:
        # punctuation_chars splits glued shell operators (``cat;rm -rf x``)
        # into real separator tokens so the per-stage checks below see them.
        tokens = list(shlex.shlex(command_str, posix=True,
                                  punctuation_chars="|&;<>(){}"))
    except ValueError:
        return True
    # POSIX shlex eats backslashes (``C:\\bin\\rm.exe`` -> ``C:binrm.exe``), so
    # also judge the raw whitespace split; destructive if either view is.
    raw = command_str.split()
    if raw != tokens and _tokens_destructive(raw):
        return True
    return _tokens_destructive(tokens)


def _segment_destructive(tokens: List[str]) -> bool:
    # Skip leading VAR=val assignments; _effective_cmd0 then resolves env.
    start = 0
    while start < len(tokens) and "=" in tokens[start] and not tokens[start].startswith("-"):
        start += 1
    tokens = tokens[start:]
    # A bare ``--`` ends option parsing, but inside ``env`` it is also a
    # legal option operand (``env -u -- rm`` unsets a var named ``--`` and
    # runs ``rm``) or env's own end-of-options marker, so only non-env
    # segments get the blanket strip; the env unwrapper models ``--``.
    if not tokens or _norm_cmd(tokens[0]) != "env":
        tokens = [t for t in tokens if t != "--"]
    if not tokens:
        return False
    cmd0 = _effective_cmd0(tokens)
    if cmd0 is None or cmd0 in _NEVER_COMPRESS_CMD0:
        return True
    if tokens and tokens[0] in _SHELL_STRUCT_TOKENS:
        return True
    if cmd0 == "git":
        if _git_has_config_options(tokens):
            return True
        sub = _git_subcommand_tokens(tokens)
        if not sub:
            return True
        name = sub[0].lower()
        if name in _GIT_WRITE_SUBCMDS:
            return True
        if name not in _GIT_READONLY_SUBCMDS:
            # Unknown subcommand: could be an alias defined in repo config.
            return True
    if cmd0 == "find" and any(t in _FIND_DESTRUCTIVE_FLAGS for t in tokens):
        return True
    if cmd0 == "sed" and any(t == "-i" or t.startswith("-i")
                             or t.startswith("--in-place") for t in tokens):
        return True
    return False


_PIPELINE_SEPARATORS = frozenset({
    "|", "||", "&", "&&", ";", ";;", ";&", ";;&", "|&", "(", ")", "{", "}",
})
_PUNCT_CHARS = frozenset("|&;<>(){}")
# Shell grammar keywords and groupers: a segment LED by one of these hides
# the real command behind it (``if true; then rm x; fi``, ``{ rm x; }``).
# Refused outright (fail closed on recognized grammar) rather than
# mini-interpreting the shell.
_SHELL_STRUCT_TOKENS = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do",
    "done", "case", "esac", "select", "coproc", "!", "{", "}", "(", ")",
})


def _tokens_destructive(tokens: List[str]) -> bool:
    # shlex splits runs of punctuation into single tokens (``|&``, ``;;``).
    # Only the exact recognized separators split a stage; ANY other
    # all-punctuation run (every redirect, fd dup, glued or invalid grammar)
    # fails closed rather than risking an exception that hides a writer.
    segments = [[]]
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t and all(c in _PUNCT_CHARS for c in t):
            if t in _PIPELINE_SEPARATORS:
                segments.append([])
                i += 1
                continue
            # Unrecognized punctuation run (incl. all redirects): fail closed.
            return True
        segments[-1].append(t)
        i += 1
    # Check every pipeline stage, not just argv[0]: ``ls | tee out`` writes
    # through the second stage.
    return any(_segment_destructive(seg) for seg in segments if seg)


def _glob_can_match_unsafe(token: str, names) -> bool:
    """True when ``token`` contains a glob and fnmatch could expand it to one
    of ``names`` (e.g. ``r*``, ``?m``, ``[r]m`` all match ``rm``)."""
    if not any(ch in _GLOB_CHARS for ch in token):
        return False
    return any(fnmatch.fnmatchcase(name, token) for name in names)


def _is_safe_add(command: str, handler: str) -> bool:
    """Gate a user ``add`` entry before it reaches the effective allowlist.

    Rejects:
      * Empty command or handler.
      * Unknown handler (not in VALID_HANDLERS).
      * Commands containing dangerous shell metacharacters.
      * Bare/overly-broad wildcard patterns (just ``*`` or ``**``).
      * Shell interpreters (bash, python, node, etc.) as argv[0] -- recognized
        even when given by absolute/relative path or via ``env``.
      * Privilege escalation prefixes (sudo, su, doas, pkexec) -- likewise
        path- and ``env``-normalized.
      * Destructive write subcommands (rm, mv, chmod, etc.) as argv[0].
    Returns True only for read-only commands mapped to a known handler.

    Defense-in-depth: dangerous shell metacharacters are already blocked
    upstream (bash_hook main, before the whitelist), so there is no injection
    here; this gate exists to deliver exactly what its docstring claims.
    """
    if not command or not command.strip():
        return False
    if handler not in VALID_HANDLERS:
        return False
    for ch in command:
        if ch in _DANGEROUS_CHARS:
            return False
    stripped = command.strip()
    if _is_all_wildcard(stripped):
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    cmd0 = _effective_cmd0(tokens)
    if cmd0 is None:
        return False
    if _is_all_wildcard(cmd0):
        return False
    cmd0 = cmd0.lower()
    # _raw_cmd_word already lower-cases and takes the basename of any
    # backslash path, so one glob check against the dangerous names suffices.
    raw0 = _raw_cmd_word(tokens)
    if raw0 and _glob_can_match_unsafe(raw0, _GLOB_NAMES):
        return False
    if cmd0 in _SHELL_STRUCT_TOKENS or _glob_can_match_unsafe(cmd0, _SHELL_STRUCT_TOKENS):
        return False
    if raw0 and _glob_can_match_unsafe(raw0, _SHELL_STRUCT_TOKENS):
        return False
    if cmd0 in _UNSAFE_INTERPRETERS:
        return False
    if cmd0 in _UNSAFE_PREFIXES:
        return False
    if cmd0 in _UNSAFE_WRITE_SUBCMDS:
        return False
    # Globs: the command string is later matched with fnmatch, so ``r*`` also
    # matches ``rm -rf target``. Reject a glob command word that could match
    # any destructive / interpreter / privilege command name.
    if cmd0 in _NEVER_COMPRESS_CMD0 or _glob_can_match_unsafe(cmd0, _GLOB_NAMES):
        return False
    # ``git <glob>``: the subcommand word must not be able to match a write.
    if cmd0 == "git":
        if _git_has_config_options(tokens):
            return False
        sub = _git_subcommand_tokens(tokens)
        if sub and (sub[0].lower() in _GIT_WRITE_SUBCMDS
                    or _glob_can_match_unsafe(sub[0].lower(), _GIT_WRITE_SUBCMDS)):
            return False
    return True


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _filters_path() -> Optional[Path]:
    """Resolve the TOML file path from env override or the default runtime
    home. Returns None when no path is resolvable."""
    env_path = os.environ.get(ENV_FILTERS_PATH)
    if env_path:
        return Path(env_path)
    # Default: <runtime-home>/token-optimizer/command-filters.toml
    # The runtime home is the same dir as config.json. We resolve it via
    # the same plugin_env helper used by the config loader, but avoid
    # importing plugin_env on the hot path when the file doesn't exist.
    try:
        from plugin_env import resolve_plugin_data_dir, _USER_CONFIG_DIR
        candidates = [_USER_CONFIG_DIR / DEFAULT_FILTERS_FILENAME]
        pdd = resolve_plugin_data_dir()
        if pdd is not None:
            candidates.append(pdd / "config" / DEFAULT_FILTERS_FILENAME)
        for c in candidates:
            if c.exists():
                return c
    except Exception:
        pass
    return None


def _warn_once(path: Path, message: str) -> None:
    """One-time stderr warning per path (avoids spamming every hook call)."""
    key = str(path)
    if key in _warned_paths:
        return
    _warned_paths.add(key)
    print(f"[Command Filters] {message}: {path}", file=sys.stderr)


def load_filters() -> CommandFilterConfig:
    """Load and validate the TOML command-filter file.

    Returns a normalized ``CommandFilterConfig``. A missing file is a no-op
    (empty config). Malformed TOML is fail-open (empty config + one-time
    warning). Malformed entries are skipped with a one-time warning. Never
    raises. Performs no file writes.
    """
    path = _filters_path()
    if path is None or not path.exists():
        return CommandFilterConfig()
    if tomllib is None:
        _warn_once(path, "tomllib not available; ignoring")
        return CommandFilterConfig()
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _warn_once(path, f"malformed TOML ({exc}); ignoring")
        return CommandFilterConfig()
    return _parse_filters(data, path)


def _parse_filters(data: dict, path: Path) -> CommandFilterConfig:
    """Parse the raw TOML dict into a normalized CommandFilterConfig.

    Validates structure; skips malformed entries with a one-time warning.
    """
    filters = data.get("filters")
    if not isinstance(filters, dict):
        return CommandFilterConfig()
    adds: List[AddEntry] = []
    excludes: List[str] = []
    # [filters.add] — mapping of pattern_name = { command, handler }
    add_section = filters.get("add")
    if isinstance(add_section, dict):
        for name, spec in add_section.items():
            if not isinstance(spec, dict):
                _warn_once(path, f"add entry '{name}' is not a table; skipping")
                continue
            command = spec.get("command")
            handler = spec.get("handler")
            if not isinstance(command, str) or not isinstance(handler, str):
                _warn_once(path, f"add entry '{name}' missing command/handler; skipping")
                continue
            if not _is_safe_add(command, handler):
                _warn_once(path, f"add entry '{name}' failed safety gate; skipping")
                continue
            adds.append(AddEntry(name=name, command=command.strip(), handler=handler))
    # [filters.exclude] — table with a `commands` array
    exclude_section = filters.get("exclude")
    if isinstance(exclude_section, dict):
        commands = exclude_section.get("commands")
        if isinstance(commands, list):
            for item in commands:
                if isinstance(item, str) and item.strip():
                    excludes.append(item.strip())
                else:
                    _warn_once(path, "exclude entry is not a string; skipping")
    return CommandFilterConfig(adds=tuple(adds), excludes=tuple(excludes))


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_filters(user: CommandFilterConfig) -> EffectiveFilters:
    """Compute the effective filter set from user config.

    The builtin allowlist/dispatch is not passed in (it lives in bash_hook /
    bash_compress and is consulted first on the hot path). This function
    simply wraps the user config into an ``EffectiveFilters`` object ready
    for downstream consultation. User ``add`` entries have already passed
    ``_is_safe_add`` in the loader; user ``exclude`` entries are glob
    patterns checked against the command string.
    """
    return EffectiveFilters(
        user_adds=user.adds,
        user_excludes=user.excludes,
    )


# ---------------------------------------------------------------------------
# Cached accessor for the hot path
# ---------------------------------------------------------------------------

_cached: Optional[EffectiveFilters] = None


def get_effective_filters() -> EffectiveFilters:
    """Return the merged effective filters, loading once and caching for the
    process lifetime. The hot path (bash_hook / bash_compress) calls this
    instead of load_filters + merge_filters directly."""
    global _cached
    if _cached is not None:
        return _cached
    _cached = merge_filters(load_filters())
    return _cached


def reset_cache() -> None:
    """Reset the process-level cache (for tests)."""
    global _cached
    _cached = None


__all__ = [
    "AddEntry",
    "CommandFilterConfig",
    "ENV_FILTERS_PATH",
    "EffectiveFilters",
    "VALID_HANDLERS",
    "get_effective_filters",
    "is_destructive_command",
    "load_filters",
    "merge_filters",
    "reset_cache",
]
