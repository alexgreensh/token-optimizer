"""Hook command text for adapters whose host runs the command through a shell
(Antigravity, Cursor, Grok).

macOS / Linux: ``TOKEN_OPTIMIZER_RUNTIME=<rt> '<python>' [flags] '<bridge>' <event>``,
POSIX-quoted.

Native Windows: the host may run the command through PowerShell or cmd.exe, so
the text must parse the same in both. That means a bare first token, double-
quoted arguments and no ``VAR=value`` prefix (cmd.exe and PowerShell both reject
it). The bridges set TOKEN_OPTIMIZER_RUNTIME themselves, so the prefix is not
needed. Paths holding a character either shell expands go through their 8.3
short names, using the same rules as the Codex hooks.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import shlex


def _is_windows(windows: bool | None) -> bool:
    return (os.name == "nt") if windows is None else windows


def build(python: str, bridge: str, event: str, *, runtime: str,
          python_flags: tuple[str, ...] = (), windows: bool | None = None) -> str:
    """The persisted hook command. Raises RuntimeError on Windows when no
    spelling runs the same under PowerShell and cmd.exe."""
    if not _is_windows(windows):
        parts = [f"TOKEN_OPTIMIZER_RUNTIME={runtime}", shlex.quote(python), *python_flags,
                 shlex.quote(bridge), event]
        return " ".join(parts)
    from codex_install import _neutral_child_arg, _shell_neutral_exe  # noqa: PLC0415

    exe = _shell_neutral_exe(python)
    parent, leaf = ntpath.split(bridge)
    bridge_arg = _neutral_child_arg(parent, leaf) if parent and leaf else None
    if exe is None or bridge_arg is None:
        bad = python if exe is None else bridge
        raise RuntimeError(
            f"cannot write a Windows hook command for {bad}: the path holds a character "
            "PowerShell or cmd.exe would expand ($, backtick, %, !) and has no 8.3 short "
            "name. Move it to a plain folder, or set TOKEN_OPTIMIZER_PYTHON to a python "
            "in a plain folder, and re-run the install."
        )
    return " ".join([exe, *python_flags, bridge_arg, event])


def parse(command: str, *, runtime: str, events, python_flags: tuple[str, ...] = (),
          windows: bool | None = None) -> list | None:
    """Parse a command written by build() into ``[python, bridge, event]``, or
    None when it is not exactly that shape. Strict on purpose: doctors replay
    these commands, so a corrupted or foreign entry must never be accepted."""
    win = _is_windows(windows)
    try:
        # posix=False keeps Windows backslashes; the double quotes stay on the
        # token and are stripped below.
        tokens = shlex.split(command, posix=not win)
    except ValueError:
        return None
    pin = f"TOKEN_OPTIMIZER_RUNTIME={runtime}"
    if win:
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in tokens]
        # Older installs wrote the POSIX shape with Windows paths; still ours.
        if tokens and tokens[0] == pin:
            tokens = tokens[1:]
    else:
        if not tokens or tokens[0] != pin:
            return None
        tokens = tokens[1:]
    n = len(python_flags)
    if len(tokens) != 3 + n or tuple(tokens[1:1 + n]) != tuple(python_flags):
        return None
    py, bridge, event = tokens[0], tokens[1 + n], tokens[2 + n]
    isabs = ntpath.isabs if win else posixpath.isabs
    if not isabs(py) or not isabs(bridge) or event not in events:
        return None
    return [py, bridge, event]


def path_spellings(path: str, *, windows: bool | None = None) -> set[str]:
    """Every spelling build() may have written for ``path``: the path itself
    and, on Windows, the form through its folder's 8.3 short name. Use this
    to recognise our own entries in a shared hooks file."""
    out = {path}
    if _is_windows(windows):
        from codex_install import _short_path  # noqa: PLC0415

        parent, leaf = ntpath.split(path)
        short = _short_path(parent) if parent else None
        if short:
            out.add(short.rstrip("\\/") + "\\" + leaf)
    return out
