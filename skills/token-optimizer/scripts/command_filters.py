#!/usr/bin/env python3
"""User-editable TOML command-filter schema + loader.

Lets a user opt NEW read-only commands into existing compression handlers
and exclude commands from compression, without weakening safety. The loader
is pure stdlib (tomllib, or a strict subset reader on Python 3.9/3.10), performs no file writes,
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
# TOML subset fallback (Python 3.9 and 3.10 have no tomllib)
# ---------------------------------------------------------------------------
# The command-filters file only needs tables, bare/quoted/dotted keys, strings,
# booleans, integers, arrays and single-line inline tables. This reader accepts
# exactly that and raises on anything else (multi-line strings, arrays of
# tables, floats, dates, ...), so an unsupported file fails open instead of
# being half-read. On Python 3.11+ the stdlib parser is always used.

class _SubsetError(ValueError):
    """The file uses TOML syntax outside the supported subset, or is invalid."""


_MAX_DEPTH = 32  # nesting of arrays / inline tables; real filter files use 2
_BARE_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


class _SubsetReader:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        # TOML forbids reopening a table that a dotted key or an inline table
        # created, and defining the same [header] twice. Track those by identity.
        self.inline_ids = set()
        self.dotted_ids = set()
        self.header_ids = set()
        self.depth = 0

    def fail(self, msg: str):
        line = self.s.count("\n", 0, self.i) + 1
        raise _SubsetError(f"{msg} (line {line})")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_inline_ws(self):
        while self.peek() in (" ", "\t"):
            self.i += 1

    def newline(self) -> bool:
        """Consume one LF or CRLF; a lone CR is not a newline in TOML."""
        if self.peek() == "\n":
            self.i += 1
            return True
        if self.peek() == "\r" and self.s[self.i + 1:self.i + 2] == "\n":
            self.i += 2
            return True
        return False

    def skip_comment(self):
        if self.peek() == "#":
            while self.peek() not in ("", "\n") and self.s[self.i:self.i + 2] != "\r\n":
                ch = self.peek()
                if ord(ch) < 0x20 and ch != "\t" or ch == "\x7f":
                    self.fail("control character in comment")
                self.i += 1

    def skip_ws_and_newlines(self):
        # Inside arrays: whitespace, newlines and comments are all insignificant.
        while True:
            self.skip_inline_ws()
            if self.peek() == "#":
                self.skip_comment()
            elif not self.newline():
                return

    def end_of_line(self):
        self.skip_inline_ws()
        self.skip_comment()
        if self.peek() != "" and not self.newline():
            self.fail("unexpected text after value")

    def key_part(self) -> str:
        self.skip_inline_ws()
        c = self.peek()
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        start = self.i
        while self.peek() and self.peek() in _BARE_KEY_CHARS:
            self.i += 1
        if start == self.i:
            self.fail("expected a key")
        return self.s[start:self.i]

    def dotted_key(self) -> list:
        parts = [self.key_part()]
        self.skip_inline_ws()
        while self.peek() == ".":
            self.i += 1
            parts.append(self.key_part())
            self.skip_inline_ws()
        return parts

    def basic_string(self) -> str:
        if self.s.startswith('"""', self.i):
            self.fail("multi-line strings are not supported")
        self.i += 1
        out = []
        while True:
            c = self.peek()
            if c in ("", "\n", "\r"):
                self.fail("unterminated string")
            self.i += 1
            if c == '"':
                return "".join(out)
            if c == "\\":
                e = self.peek()
                self.i += 1
                if e in _ESCAPES:
                    out.append(_ESCAPES[e])
                elif e in ("u", "U"):
                    n = 4 if e == "u" else 8
                    digits = self.s[self.i:self.i + n]
                    if len(digits) != n or any(d not in "0123456789abcdefABCDEF" for d in digits):
                        self.fail("invalid unicode escape")
                    code = int(digits, 16)
                    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                        self.fail("invalid unicode escape")
                    out.append(chr(code))
                    self.i += n
                else:
                    self.fail("invalid escape sequence")
            elif ord(c) < 0x20 and c != "\t" or c == "\x7f":
                self.fail("control character in string")
            else:
                out.append(c)

    def literal_string(self) -> str:
        if self.s.startswith("'''", self.i):
            self.fail("multi-line strings are not supported")
        end = self.i + 1
        while end < len(self.s) and self.s[end] not in ("'", "\n", "\r"):
            end += 1
        if end >= len(self.s) or self.s[end] != "'":
            self.fail("unterminated string")
        text = self.s[self.i + 1:end]
        if any(ord(ch) < 0x20 and ch != "\t" or ch == "\x7f" for ch in text):
            self.fail("control character in string")
        self.i = end + 1
        return text

    def value(self):
        self.skip_inline_ws()
        c = self.peek()
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        for word, val in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i):
                after = self.s[self.i + len(word):self.i + len(word) + 1]
                if after == "" or after in " \t\r\n,]}#":
                    self.i += len(word)
                    return val
        start = self.i
        if self.peek() in ("+", "-"):
            self.i += 1
        digits_start = self.i
        while self.peek().isdigit() and self.peek().isascii():
            self.i += 1
        text = self.s[digits_start:self.i]
        after = self.peek()
        if not text or (len(text) > 1 and text[0] == "0") or (after and after not in " \t\r\n,]}#"):
            self.i = start
            self.fail("unsupported value")
        return int(self.s[start:self.i])

    def enter(self):
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            self.fail("arrays and inline tables nested too deeply")

    def array(self) -> list:
        self.enter()
        try:
            return self._array()
        finally:
            self.depth -= 1

    def _array(self) -> list:
        self.i += 1
        items = []
        while True:
            self.skip_ws_and_newlines()
            if self.peek() == "]":
                self.i += 1
                return items
            items.append(self.value())
            self.skip_ws_and_newlines()
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "]":
                self.fail("expected ',' or ']' in array")

    def inline_table(self) -> dict:
        self.enter()
        try:
            return self._inline_table()
        finally:
            self.depth -= 1

    def _inline_table(self) -> dict:
        self.i += 1
        table: dict = {}
        self.inline_ids.add(id(table))
        self.skip_inline_ws()
        if self.peek() == "}":
            self.i += 1
            return table
        while True:
            keys = self.dotted_key()
            self.skip_inline_ws()
            if self.peek() != "=":
                self.fail("expected '='")
            self.i += 1
            self.assign(table, keys, self.value(), "inline table")
            self.skip_inline_ws()
            c = self.peek()
            self.i += 1
            if c == "}":
                return table
            if c != ",":
                self.fail("expected ',' or '}' in inline table")
            self.skip_inline_ws()
            if self.peek() == "}":
                self.fail("trailing comma in inline table")

    def assign(self, table: dict, keys: list, val, what: str):
        for part in keys[:-1]:
            if part in table:
                nxt = table[part]
                # A dotted key may extend a table made by dotted keys or created
                # implicitly as the parent of a [header]; not one a header, an
                # inline table or a value defined itself.
                if (not isinstance(nxt, dict) or id(nxt) in self.inline_ids
                        or id(nxt) in self.header_ids):
                    self.fail(f"key conflicts with an existing value in {what}")
                self.dotted_ids.add(id(nxt))
            else:
                nxt = table[part] = {}
                self.dotted_ids.add(id(nxt))
            table = nxt
        if keys[-1] in table:
            self.fail(f"duplicate key in {what}")
        table[keys[-1]] = val

    def parse(self) -> dict:
        root: dict = {}
        current = root
        while True:
            self.skip_inline_ws()
            c = self.peek()
            if c == "":
                return root
            if c == "#":
                self.skip_comment()
            if self.newline():
                continue
            if self.peek() == "":
                return root
            if self.peek() == "[":
                if self.s.startswith("[[", self.i):
                    self.fail("arrays of tables are not supported")
                self.i += 1
                keys = self.dotted_key()
                self.skip_inline_ws()
                if self.peek() != "]":
                    self.fail("expected ']'")
                self.i += 1
                current = root
                for n, part in enumerate(keys):
                    nxt = current.setdefault(part, {})
                    # A table made by dotted keys can still gain sub-tables, but
                    # the header may not name it itself; inline tables are closed.
                    if (not isinstance(nxt, dict) or id(nxt) in self.inline_ids
                            or (n == len(keys) - 1 and id(nxt) in self.dotted_ids)):
                        self.fail("table conflicts with an existing value")
                    current = nxt
                if id(current) in self.header_ids:
                    self.fail("table defined twice")
                self.header_ids.add(id(current))
                self.end_of_line()
                continue
            keys = self.dotted_key()
            self.skip_inline_ws()
            if self.peek() != "=":
                self.fail("expected '='")
            self.i += 1
            self.assign(current, keys, self.value(), "table")
            self.end_of_line()


def _loads_subset(text: str) -> dict:
    """Parse the supported TOML subset; raise _SubsetError on anything else."""
    return _SubsetReader(text).parse()


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
})
_UNSAFE_PREFIXES = frozenset({"sudo", "su", "doas", "pkexec", "gksudo"})
_UNSAFE_WRITE_SUBCMDS = frozenset({
    "rm", "mv", "cp", "chmod", "chown", "chgrp", "mkdir", "rmdir", "ln",
    "dd", "mkfs", "fdisk", "mount", "umount", "kill", "killall", "pkill",
    "shutdown", "reboot", "halt", "poweroff",
})
_DANGEROUS_CHARS = frozenset(";|&`$(){}><\n\r\x00")

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
        or None."""
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


def _effective_cmd0(tokens: List[str]) -> Optional[str]:
    """Return the basename of the REAL command being run.

    Normalizes away the path (so ``/bin/bash``, ``./python``, ``/usr/bin/sudo``
    are recognized as ``bash``/``python``/``sudo``) and resolves a leading
    ``env`` invocation by skipping ``VAR=val`` assignments and env's own options
    (``-i``, and the operand-taking ``-u``/``-C``/``-S``) to reach the command
    env would exec. Returns None when no command token remains.
    """
    if not tokens:
        return None
    i = 0
    if os.path.basename(tokens[0]) == "env":
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if "=" in tok and not tok.startswith("-"):
                i += 1  # VAR=val assignment
                continue
            if tok in ("-i", "--ignore-environment", "-", "-v", "--debug"):
                i += 1
                continue
            if tok in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                i += 2  # option consumes its operand
                continue
            break  # first non-option, non-assignment token = the command
    if i >= len(tokens):
        return None
    return os.path.basename(tokens[i])


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
    if cmd0 in _UNSAFE_INTERPRETERS:
        return False
    if cmd0 in _UNSAFE_PREFIXES:
        return False
    if cmd0 in _UNSAFE_WRITE_SUBCMDS:
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
    try:
        if tomllib is not None:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        else:
            # Python 3.9/3.10: no stdlib TOML. Read the supported subset, and
            # fail open on anything outside it.
            with open(path, "rb") as handle:
                data = _loads_subset(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        # tomllib.TOMLDecodeError and _SubsetError are both ValueError; a deeply
        # nested file can also exhaust the stdlib parser's recursion.
        _warn_once(path, f"malformed or unsupported TOML ({exc}); ignoring")
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
    "load_filters",
    "merge_filters",
    "reset_cache",
]
