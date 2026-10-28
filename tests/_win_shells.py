"""Small, conservative models of how Windows shells turn a hook command string
into the argv a native program receives (issue #278).

They exist so the Linux test suite can prove a generated Codex hook command
means the same thing in every shell Codex may hand it to:

* cmd.exe, the way Codex's COMSPEC fallback runs it: ``cmd.exe /C`` plus the
  whole command wrapped in one more pair of quotes, passed raw.
* Windows PowerShell 5.1 and PowerShell 7, the way Codex runs a configured
  session shell: ``powershell -NoProfile -Command <command>`` with the command
  as ONE argument (Rust MSVC quoting, which PowerShell undoes).

"Conservative" means: anything the model is not sure a shell keeps literal
(a ``$`` in a PowerShell string, an unquoted ``&`` in cmd, a backtick, ...)
raises ShellError instead of guessing. A command that passes the models only
uses syntax whose meaning is the same in all of them.
"""

from __future__ import annotations

import re


class ShellError(Exception):
    """The shell would reject, split, or rewrite this command."""


# ---- MSVCRT / CommandLineToArgvW ----------------------------------------

def msvcrt_split(line: str) -> list[str]:
    """Split a Windows command line the way the MSVC runtime (and Python's
    own startup code) builds argv. Post-2008 rules: 2n backslashes + quote ->
    n backslashes and a quote toggle; 2n+1 -> n backslashes and a literal
    quote; ``""`` inside a quoted run -> a literal quote."""
    args: list[str] = []
    i, n = 0, len(line)
    while True:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            return args
        cur: list[str] = []
        in_q = False
        while i < n and (in_q or line[i] not in " \t"):
            c = line[i]
            if c == "\\":
                j = i
                while j < n and line[j] == "\\":
                    j += 1
                count = j - i
                if j < n and line[j] == '"':
                    cur.append("\\" * (count // 2))
                    if count % 2:
                        cur.append('"')
                        i = j + 1
                    else:
                        i = j
                    continue
                cur.append("\\" * count)
                i = j
                continue
            if c == '"':
                if in_q and i + 1 < n and line[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_q = not in_q
                i += 1
                continue
            cur.append(c)
            i += 1
        args.append("".join(cur))


def msvcrt_quote(arg: str) -> str:
    """Quote one argument so msvcrt_split returns it unchanged (what Rust's
    ``Command::arg`` and Python's list2cmdline do on Windows)."""
    if arg and not any(c in arg for c in ' \t"'):
        return arg
    out = ['"']
    backslashes = 0
    for c in arg:
        if c == "\\":
            backslashes += 1
            continue
        if c == '"':
            out.append("\\" * (backslashes * 2 + 1) + '"')
        else:
            out.append("\\" * backslashes + c)
        backslashes = 0
    out.append("\\" * (backslashes * 2) + '"')
    return "".join(out)


# ---- cmd.exe -------------------------------------------------------------

_CMD_VAR_RE = re.compile(r"%([^%\r\n]+)%")
_CMD_DELAYED_RE = re.compile(r"!([^!\r\n]+)!")


def cmd_run(command: str, env: dict[str, str], *, delayed: bool = False) -> list[str]:
    """argv the program receives when Codex runs ``cmd.exe /C "<command>"``.

    Steps: cmd strips the first and last quote of the /C text (the host's own
    wrapping pair); expands ``%NAME%`` for every defined NAME (case-insensitive)
    once; optionally expands ``!NAME!`` (delayed expansion turned on in the
    registry); then refuses unquoted ``& | < >`` and a leading ``(`` / ``@``,
    removes unquoted ``^`` escapes, and hands the line to the program.
    """
    line = f'"{command}"'
    line = line[1:]
    last = line.rfind('"')
    line = line[:last] + line[last + 1:]
    upper = {k.upper(): v for k, v in env.items()}

    def expand(m: re.Match) -> str:
        return upper.get(m.group(1).upper(), m.group(0))

    line = _CMD_VAR_RE.sub(expand, line)
    if delayed:
        line = _CMD_DELAYED_RE.sub(expand, line)
        if "!" in line:
            raise ShellError("delayed expansion would drop or rewrite '!'")
    stripped = line.lstrip()
    if stripped[:1] in ("(", "@"):
        raise ShellError("leading ( or @ changes how cmd runs the line")
    out: list[str] = []
    in_q = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            in_q = not in_q
        elif not in_q and c == "^":
            i += 1
            if i < len(line):
                out.append(line[i])
            i += 1
            continue
        elif not in_q and c in "&|<>":
            raise ShellError(f"unquoted {c!r} splits or redirects the cmd line")
        out.append(c)
        i += 1
    if in_q:
        raise ShellError("unbalanced quote in cmd line")
    argv = msvcrt_split("".join(out))
    if not argv:
        raise ShellError("empty command")
    return argv


# ---- PowerShell ------------------------------------------------------------

_PS_DQ = '"“”„'
_PS_SQ = "'‘’‚‛"
_PS_BARE_SPECIAL = set(";|&<>(){}$`,") | set(_PS_DQ) | set(_PS_SQ)


def _ps_tokens(command: str) -> list[tuple[str, bool]]:
    """Split a PowerShell command-mode statement into (value, was_bare) args.

    Adjacent segments concatenate into one argument as PowerShell does
    (``a"b c"`` is ``ab c``). Raises on anything that would expand, escape,
    start a sub-expression, or end the statement.
    """
    tokens: list[tuple[str, bool]] = []
    i, n = 0, len(command)
    while True:
        while i < n and command[i] in " \t":
            i += 1
        if i >= n:
            return tokens
        if command[i] in "#@":
            raise ShellError("comment or splat at token start")
        cur: list[str] = []
        bare = True
        while i < n and command[i] not in " \t":
            c = command[i]
            if c in _PS_DQ:
                bare = False
                i += 1
                while True:
                    if i >= n:
                        raise ShellError("unterminated double-quoted string")
                    c = command[i]
                    if c in _PS_DQ:
                        if i + 1 < n and command[i + 1] in _PS_DQ:
                            cur.append('"')
                            i += 2
                            continue
                        i += 1
                        break
                    if c in "$`":
                        raise ShellError(f"{c!r} expands inside a PowerShell double-quoted string")
                    cur.append(c)
                    i += 1
            elif c in _PS_SQ:
                bare = False
                i += 1
                while True:
                    if i >= n:
                        raise ShellError("unterminated single-quoted string")
                    c = command[i]
                    if c in _PS_SQ:
                        if i + 1 < n and command[i + 1] in _PS_SQ:
                            cur.append("'")
                            i += 2
                            continue
                        i += 1
                        break
                    cur.append(c)
                    i += 1
            elif c in _PS_BARE_SPECIAL:
                raise ShellError(f"unquoted {c!r} is PowerShell syntax")
            else:
                cur.append(c)
                i += 1
        tokens.append(("".join(cur), bare))


def powershell_run(command: str, version: str) -> list[str]:
    """argv the program receives when Codex runs
    ``<powershell|pwsh> -NoProfile -Command <command>``.

    ``version`` is "5.1" (Windows PowerShell: drops the first bare ``--`` and
    empty arguments, and quotes native arguments the legacy way: wraps a
    spaced value in quotes without escaping) or "7" (PowerShell 7.3+, standard
    argument passing for python.exe).
    """
    # Codex passes the command as one argument; PowerShell undoes the quoting.
    assert msvcrt_split(msvcrt_quote(command)) == [command]
    stripped = command.lstrip()
    if not stripped:
        raise ShellError("empty command")
    if stripped[0] in _PS_DQ + _PS_SQ + "$(&.[{":
        # A quoted first token is a string EXPRESSION; anything after it is
        # "Unexpected token". (& and . would be call operators: not neutral.)
        raise ShellError("first token is an expression, not a command")
    tokens = _ps_tokens(command)
    program, args = tokens[0][0], tokens[1:]
    if version == "5.1":
        kept: list[str] = []
        dropped_dashdash = False
        for value, bare in args:
            if bare and value == "--" and not dropped_dashdash:
                dropped_dashdash = True
                continue
            if value == "":
                continue
            kept.append(value)
        parts = [program]
        for value in kept:
            if '"' in value:
                raise ShellError("legacy native passing mangles embedded quotes")
            if any(ch in value for ch in " \t"):
                if value.endswith("\\"):
                    raise ShellError("legacy native passing breaks a trailing backslash")
                parts.append(f'"{value}"')
            else:
                parts.append(value)
        return msvcrt_split(" ".join(parts))
    if version == "7":
        return [program, *(value for value, _ in args)]
    raise ValueError(version)


SHELLS = ("cmd", "cmd-delayed", "powershell-5.1", "pwsh-7")


def run_in(shell: str, command: str, env: dict[str, str]) -> list[str]:
    if shell == "cmd":
        return cmd_run(command, env)
    if shell == "cmd-delayed":
        return cmd_run(command, env, delayed=True)
    if shell == "powershell-5.1":
        return powershell_run(command, "5.1")
    if shell == "pwsh-7":
        return powershell_run(command, "7")
    raise ValueError(shell)
