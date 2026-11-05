"""Recognize outputs whose contract is to return a previously stored original.

Identity is narrow: ordinary search/retrieval tools still use compression.
This is an output-fidelity rule, not an authorization or security boundary.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

_RECOVERY_TOOLS = frozenset({'headroom_retrieve', 'caveman_retrieve'})
# Shell tools whose `command` can be our own `measure.py expand` (the archive matcher
# covers both; PowerShell is the Windows-native one).
_SHELL_TOOLS = frozenset({'Bash', 'PowerShell'})
_QUOTE_OR_EXPANSION = re.compile(r"""['"$`\\]""")
# Every interpreter name a printed hint may use (python3 on macOS/Linux, python on
# Windows, py for the Windows launcher) is accepted on every OS: a transcript can
# be read on a different machine from the one that printed it.
_INTERPRETERS = ('python3', 'python', 'py')
_PYTHON = re.compile(r'(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?\Z', re.IGNORECASE)


def is_recovery_tool(tool_name: str) -> bool:
    """Match a known bare identity or the tool segment of an MCP identity."""
    if not isinstance(tool_name, str):
        return False
    if tool_name.startswith('mcp__'):
        parts = tool_name.split('__')
        if len(parts) != 3 or not parts[1]:
            return False
        tool_name = parts[2]
    return tool_name in _RECOVERY_TOOLS


# Bounded-read flags `expand` accepts after the key; each takes exactly one
# value token. --search/--list/--grep stay outside: their output is a result
# list, not the stored original, so it keeps the normal pipeline.
_EXPAND_VALUE_FLAGS = frozenset({
    '--session', '--lines', '--start', '--chars',
    '--match', '--context', '--skip',
})
# A printed hint may pin its runtime with a leading `--runtime NAME` (Hermes and
# Codex archives live under their own homes; see refetch_fingerprint.expand_command).
_RUNTIME_PIN = re.compile(r'--runtime [a-z][a-z0-9-]{0,31} ')
_RUNTIME_NAME = re.compile(r'[a-z][a-z0-9-]{0,31}')
# Shell operators that split a command even when glued to a word (`--match a;b`
# runs `b` as a second command). Quoted text keeps them inside the value token.
_EXPAND_PUNCT = ';&|<>()'


def _expand_split(text):
    """shlex.split, but unquoted ``;&|<>()`` surface as their own tokens.

    Plain shlex keeps ``a;b`` inside one word, which would let a compound
    command pass as a single ``--match`` value; the shell still splits on it.
    """
    lex = shlex.shlex(text, posix=True, punctuation_chars=_EXPAND_PUNCT)
    lex.whitespace_split = True
    return list(lex)


def _expand_tail_ok(tokens):
    """``expand`` tail tokens: <key> then --flag value pairs, nothing else.

    One optional trailing ']' is tolerated on the key or the last value: pointers
    shipped before the bracket moved to its own line ended "expand <id>]", and
    those stale hints are still in live transcripts. No shell operator can pass —
    every token must be the key, a known flag, or that flag's value.
    """
    if not tokens:
        return False
    key = tokens[0]
    if key.endswith(']'):
        key = key[:-1]
    if key.startswith('-') or not re.fullmatch(r'[a-zA-Z0-9_-]+', key):
        return False
    rest = list(tokens[1:])
    if rest and rest[-1].endswith(']'):
        rest[-1] = rest[-1][:-1]
    i = 0
    while i < len(rest):
        if rest[i] not in _EXPAND_VALUE_FLAGS or i + 1 >= len(rest):
            return False
        i += 2
    return True


def _is_printed_hint(command: str) -> bool:
    """The command exactly as Token Optimizer prints it, on any OS.

    shlex reads POSIX shell: a Windows path loses its backslashes and a path
    with a space splits, so the printed hint is matched as text first. The
    older unquoted form is kept because archived pointers in a live transcript
    still carry it. The tail pattern admits no shell operator.
    """
    try:
        from refetch_fingerprint import measure_py_path, shell_path
        raw = measure_py_path()
    except Exception:
        return False
    text = command.strip()
    fwd = raw.replace('\\', '/')
    shown_forms = {shell_path(raw), raw, fwd}
    # Plainly quoted forms: single quotes (what a Windows install printed before
    # cmd.exe-safe double quotes) and double quotes. A path that itself holds a
    # quote or a shell-expanding character has no plain quoted form, so only the
    # printer's own escaped form above can match it.
    if not _QUOTE_OR_EXPANSION.search(fwd):
        shown_forms.update((f"'{raw}'", f"'{fwd}'", f'"{fwd}"'))
    for interp in _INTERPRETERS:
        for shown in shown_forms:
            head = f'{interp} {shown} '
            if not text.startswith(head):
                continue
            rest = text[len(head):]
            pinned = _RUNTIME_PIN.match(rest)
            if pinned:
                rest = rest[pinned.end():]
            if rest.startswith('expand '):
                try:
                    tail = _expand_split(rest[len('expand '):])
                except ValueError:
                    continue
                if _expand_tail_ok(tail):
                    return True
    return False


def is_expand_command(command: str) -> bool:
    """Only a simple invocation of this installation's measure.py expand.

    Shell operators are not interpreted. A compound command may also contain
    ordinary output, so it must keep the normal pipeline rather than granting
    the whole command a recovery exemption.
    """
    if not isinstance(command, str) or len(command) > 16_384:
        return False
    if _is_printed_hint(command):
        return True
    try:
        tokens = _expand_split(command)
        if (len(tokens) >= 4 and tokens[2] == '--runtime'
                and _RUNTIME_NAME.fullmatch(tokens[3])):
            tokens = tokens[:2] + tokens[4:]
        if len(tokens) < 4 or tokens[2] != 'expand':
            return False
        if not _PYTHON.fullmatch(Path(tokens[0]).name):
            return False
        if not _expand_tail_ok(tokens[3:]):
            return False
        expected = Path(__file__).resolve().parent / 'measure.py'
        return Path(tokens[1]).is_absolute() and Path(tokens[1]).resolve() == expected
    except (ValueError, OSError, RuntimeError):
        return False


def is_recovery_output(tool_name: str, tool_input) -> bool:
    if is_recovery_tool(tool_name):
        return True
    return (tool_name in _SHELL_TOOLS and isinstance(tool_input, dict)
            and is_expand_command(tool_input.get('command', '')))
