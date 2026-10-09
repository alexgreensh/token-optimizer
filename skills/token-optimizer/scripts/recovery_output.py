"""Recognize outputs whose contract is to return a previously stored original.

Identity is narrow: ordinary search/retrieval tools still use compression.
This is an output-fidelity rule, not an authorization or security boundary.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

_RECOVERY_TOOLS = frozenset({'headroom_retrieve', 'caveman_retrieve'})
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


def is_expand_command(command: str) -> bool:
    """Only a simple invocation of this installation's measure.py expand.

    Shell operators are not interpreted. A compound command may also contain
    ordinary output, so it must keep the normal pipeline rather than granting
    the whole command a recovery exemption.
    """
    if not isinstance(command, str) or len(command) > 16_384:
        return False
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
    return (tool_name == 'Bash' and isinstance(tool_input, dict)
            and is_expand_command(tool_input.get('command', '')))
