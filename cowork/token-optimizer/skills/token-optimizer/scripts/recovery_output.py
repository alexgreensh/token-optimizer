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
_PYTHON = re.compile(r'python(?:3(?:\.\d+)?)?(?:\.exe)?\Z', re.IGNORECASE)


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


# An id never starts with a dash, so --list and --search keep the normal pipeline.
_HINT_TAIL = re.compile(r'[a-zA-Z0-9_][a-zA-Z0-9_-]*(?: --session [a-zA-Z0-9_][a-zA-Z0-9_-]*)?\Z')


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
    for shown in {shell_path(raw), raw, raw.replace('\\', '/')}:
        prefix = f'python3 {shown} expand '
        if text.startswith(prefix) and _HINT_TAIL.match(text[len(prefix):]):
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
        tokens = shlex.split(command)
        if len(tokens) not in (4, 6) or tokens[2] != 'expand':
            return False
        if not _PYTHON.fullmatch(Path(tokens[0]).name):
            return False
        if tokens[3].startswith('-') or not re.fullmatch(r'[a-zA-Z0-9_-]+', tokens[3]):
            return False
        if len(tokens) == 6 and (tokens[4] != '--session' or not re.fullmatch(r'[a-zA-Z0-9_-]+', tokens[5])):
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
