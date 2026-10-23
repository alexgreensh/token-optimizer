#!/usr/bin/env python3
"""Install Token Optimizer Codex hooks globally or into a project workspace."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import codex_compact_prompt
import codex_io
import codex_statusline
from runtime_env import codex_home

TOKEN_OPTIMIZER_MARKER = "token-optimizer/scripts"
SUPPORTED_EVENTS = (
    "PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
    "SessionEnd", "StopFailure", "SubagentStart", "SubagentStop",
)

# A Codex marketplace install lives in a versioned directory
# (.../token-optimizer/<X.Y.Z>/) that the marketplace replaces on upgrade.
# Used to decide whether the baked hook command must resolve the active version
# at runtime instead of pinning it (which dies on the next update).
_SEMVER_DIR_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


# Inline POSIX bash-resolver prefix/suffix. When Claude/Codex runs a hook
# command under `/bin/sh -c` with a stripped/empty PATH, bare `bash` is not
# found → exit 127 spam on every tool call. The resolver probes `command -v`
# for bash in PATH then a fixed list of absolute locations (POSIX guarantees
# `command -v /abs/path` works without PATH). `exec` replaces the shell with
# bash; the trailing `exit 0` is a quiet no-op when no bash exists anywhere.
_BASH_RESOLVER_PREFIX = (
    'for b in bash /bin/bash /usr/bin/bash /usr/local/bin/bash /opt/homebrew/bin/bash; '
    'do command -v "$b" >/dev/null 2>&1 || continue; '
)
_BASH_RESOLVER_SUFFIX = "; done; exit 0"

# Marker baked into every generated Windows launcher command so install,
# uninstall, doctor, and dashboard ownership checks can recognize it. The
# launcher strips it from the argv it forwards to the runner.
_LAUNCHER_MARKER = "token-optimizer/scripts/windows-launcher"
# Filename of the readable launcher codex_install copies NEXT TO the
# versioned install dirs (issue #183); canonical source ships in hooks/.
_LAUNCHER_FILENAME = "windows-launcher.py"
# Unquoted first token for the Windows launcher command (see
# _windows_launcher_command for why the command must not start with a quote).
_WIN_UNQUOTED_PREFIX = 'set "TOKEN_OPTIMIZER_RUNTIME=codex" &&'
_LAUNCHER_B64_RE = re.compile(r"b64decode\('([A-Za-z0-9+/=]+)'\)")
# Sentinel version leaf used when normalizing a launcher for signature
# comparison (see _launcher_signature).
_SIGNATURE_VERSION = "0.0.0"


def _cmd_quote(token: str) -> str:
    """Quote a token so it survives BOTH cmd.exe's /C metachar parse and the
    MSVCRT argv parser the Python runner is launched through.

    list2cmdline gives MSVCRT-correct quoting but only wraps a token that
    contains whitespace, so a space-free install path carrying a cmd
    metacharacter (& | < > ^) reaches cmd bare and is parsed as a command
    separator (the base64 form never had this: paths lived inside the encoded
    payload). Force the wrap. Our values never contain a literal " (illegal in
    a Windows path; the generated argv/env values are fixed and quote-free) and
    never end in a backslash, so a plain double-quote wrap stays MSVCRT-safe.
    A literal % still expands inside quotes -- a documented, install-path-only
    limitation, unchanged here.
    """
    quoted = subprocess.list2cmdline([token])
    return quoted if quoted.startswith('"') else f'"{quoted}"'


# Characters a Windows path may carry and still parse the same in cmd.exe,
# Windows PowerShell 5.1 and PowerShell 7 when written as a bare first token.
_SHELL_NEUTRAL_EXE_RE = re.compile(r"^[A-Za-z0-9_.:\\/+~-]+$")


def _short_path(path: str) -> str | None:
    """Windows 8.3 short form of ``path``, or None when there is none."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        if 0 < n < len(buf) and os.path.exists(buf.value):
            return buf.value
    except (AttributeError, ImportError, OSError, ValueError):
        pass
    return None


def _shell_neutral_exe(path: str) -> str | None:
    """Return ``path`` as a bare token every Codex Windows hook shell parses
    identically, or None when no such spelling exists.

    Codex runs a command hook through the session shell: PowerShell
    (``powershell -NoProfile -Command <cmd>``, preferring pwsh) or, when no
    session shell is known, ``%COMSPEC% /C``. A quoted first token needs ``&``
    in PowerShell and is mangled by cmd's /C quote stripping; a ``set X=Y &&``
    prefix is not PowerShell 5.1 syntax at all (issue #278). A bare first token
    works in both, so a path that needs quoting goes through its 8.3 short name.
    """
    if _SHELL_NEUTRAL_EXE_RE.match(path):
        return path
    short = _short_path(path)
    if short and _SHELL_NEUTRAL_EXE_RE.match(short):
        return short
    return None


# PowerShell also treats the typographic quotes (U+2018..U+201E) as quote
# characters, so a path holding one would end the double-quoted token early.
_SHELL_UNSAFE_ARG_CHARS = '"$`%!\r\n\x00\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f'


def _shell_neutral_arg(token: str) -> str | None:
    """Double-quote ``token`` so cmd.exe and PowerShell pass it verbatim.

    Returns None for a token neither shell takes literally inside double
    quotes: ``$`` and the backtick expand in PowerShell, ``%`` and ``!`` can
    expand in cmd, and a literal quote, newline or trailing backslash breaks
    the MSVCRT argv split. PowerShell's typographic quotes are rejected too.
    (A literal ``%`` was already a documented limit.)
    """
    if any(ch in token for ch in _SHELL_UNSAFE_ARG_CHARS) or token.endswith("\\"):
        return None
    return f'"{token}"'


def _windows_launcher_command(root: Path, script: str, args, extra_env: dict,
                              *, silent: bool = False) -> str:
    """Build the baked Windows hook command for a versioned marketplace root.

    The command invokes the readable launcher file that install() copies to
    the STABLE parent of the version dirs (.../token-optimizer/
    windows-launcher.py, which survives the marketplace swapping
    .../token-optimizer/<X.Y.Z>/ on upgrade) -- issue #183: the retired base64
    `python -c` exec-bootstrap tripped generic-loader antivirus signatures on
    the literal encoded-exec string.

    Shell contract (issue #278): Codex hands the command to PowerShell on
    native Windows (``-Command``), or to ``%COMSPEC% /C`` when it has no session
    shell. The same text must run in both, so it is ``<python> "<arg>" ...``:
    a bare first token, double-quoted arguments, no ``set ... &&`` prefix, no
    ``>NUL`` redirect (``silent=True`` adds a TOKEN_OPTIMIZER_HOOK_SILENT env
    pair; the launcher then sends stdout/stderr to the null device in-process).
    The launcher sets TOKEN_OPTIMIZER_RUNTIME itself. When a path cannot be
    written that way (no 8.3 name for a spaced interpreter, or a ``$`` / ``%``
    in a path) the cmd.exe-only form of issue #276 is returned so the hook
    still works under cmd; the doctor flags it.
    """
    exe = _shell_neutral_exe(sys.executable)
    launcher = _shell_neutral_arg(str(root.parent / _LAUNCHER_FILENAME))
    baked = _shell_neutral_arg(str(root))
    tail = [_shell_neutral_arg(script), *(_shell_neutral_arg(a) for a in args),
            _shell_neutral_arg(_LAUNCHER_MARKER)]
    env = dict(extra_env)
    if silent:
        env["TOKEN_OPTIMIZER_HOOK_SILENT"] = "1"
    env_parts = [_shell_neutral_arg(f"{k}={v}") for k, v in env.items()]
    if exe and launcher and baked and all(tail) and all(env_parts):
        parts = [exe, launcher, "--baked-root", baked]
        for env_part in env_parts:
            parts += ["--env", env_part]
        parts += ["--", *tail]
        return " ".join(parts)
    return _cmd_only_launcher_command(root, script, args, extra_env, silent=silent)


def _cmd_only_launcher_command(root: Path, script: str, args, extra_env: dict,
                               *, silent: bool = False) -> str:
    """cmd.exe-only launcher command (issue #276 shape): an unquoted ``set``
    builtin first so cmd's /C quote stripping keeps the quoted paths intact.
    PowerShell cannot run it; used only when no shell-neutral spelling exists.
    """
    parts = [
        _WIN_UNQUOTED_PREFIX,
        _cmd_quote(sys.executable),
        _cmd_quote(str(root.parent / _LAUNCHER_FILENAME)),
        "--baked-root",
        _cmd_quote(str(root)),
    ]
    for key, value in extra_env.items():
        parts += ["--env", _cmd_quote(f"{key}={value}")]
    parts += ["--", _cmd_quote(script), *(_cmd_quote(a) for a in args),
              _cmd_quote(_LAUNCHER_MARKER)]
    command = " ".join(parts)
    return f"{command} >NUL 2>&1" if silent else command


_SET_PREFIX_RE = re.compile(r"^\s*set\s", re.IGNORECASE)


def is_shell_neutral_command(command: str) -> bool:
    """True when one of our Windows launcher commands runs under both
    PowerShell and cmd.exe (see _windows_launcher_command)."""
    if _LAUNCHER_MARKER not in command or _LAUNCHER_B64_RE.search(command):
        return False
    if command.startswith(('"', "'", "&", "@")) or _SET_PREFIX_RE.match(command):
        return False
    return not any(tok in command for tok in (" && ", " >NUL", " 2>&1"))


def decode_launcher_command(command: str) -> str | None:
    """Decode the embedded Python bootstrap of a LEGACY generated launcher.

    Only pre-launcher-file commands (the base64 `python -c` bootstrap retired
    for issue #183) carry a payload to decode; launcher-file commands are
    self-auditing (the invoked .py is plain source). Returns None when
    `command` is not a legacy one of ours (no launcher marker or no base64
    payload). Pure decode-and-print for audits; never executes.
    """
    if _LAUNCHER_MARKER not in command:
        return None
    match = _LAUNCHER_B64_RE.search(command)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1)).decode("utf-8")
    except (ValueError, UnicodeError):
        return None


def _hook_command(script: str, *args: str, redirect_quiet: bool = False,
                   extra_env: dict[str, str] | None = None) -> str:
    root = _repo_root()
    extra_env = extra_env or {}
    if sys.platform == "win32":
        # Codex runs command hooks through cmd.exe on native Windows. Invoke the
        # current interpreter directly so the hot path does not traverse MSYS
        # bash and python-launcher.sh (several CreateProcess calls per hook).
        # list2cmdline applies native Windows quoting for paths with spaces.
        #
        # Codex uses %COMSPEC% /C on native Windows, not Git Bash. Avoid
        # batch-only SETLOCAL semantics and nested shell quoting here.
        _win_env = ''.join(
            f'set "{k}={v}" && ' for k, v in extra_env.items()
        )
        if _SEMVER_DIR_RE.match(root.name):
            # CMD needs a Windows-native counterpart to the POSIX runtime
            # resolver below. cmd.exe parses a /C command line ONCE, before
            # anything on it runs, so neither %VAR% nor a same-line !VAR!
            # (setlocal does not apply outside batch files) can carry the
            # resolved version into the runner path (issue #180). The command
            # instead invokes the readable launcher file installed next to the
            # version dirs by quoted path (issue #183: the base64 -c bootstrap
            # tripped generic-loader antivirus signatures) -- the launcher
            # resolves the newest semver install in the Python process we are
            # already launching, which removes the nested-quoting and
            # console-codepage problems entirely. Audit by reading the file;
            # legacy base64 commands still decode via --decode-launcher.
            return _windows_launcher_command(
                root, script, args, extra_env, silent=redirect_quiet)
        else:
            prefix = f'set "TOKEN_OPTIMIZER_RUNTIME=codex" && {_win_env}'
            argv = [sys.executable, str(root / "hooks" / "run.py"), script, *args]
        redirect = " >NUL 2>&1" if redirect_quiet else ""
        return f"{prefix}{subprocess.list2cmdline(argv)}{redirect}"

    command_args = " ".join(shlex.quote(arg) for arg in (script, *args))
    redirect = " >/dev/null 2>&1" if redirect_quiet else ""
    _posix_env = " ".join(f"{k}={shlex.quote(v)}" for k, v in extra_env.items())
    _posix_env_prefix = f"{_posix_env} " if _posix_env else ""
    if _SEMVER_DIR_RE.match(root.name):
        # Marketplace install: root is .../token-optimizer/<X.Y.Z>/, which is
        # deleted when Codex installs a newer version. Pinning it here makes
        # hooks.json point at a missing directory after every upgrade, so each
        # Codex tool call fails. Resolve the newest installed version at
        # runtime from the stable parent dir, falling back to the baked path.
        base = shlex.quote(str(root.parent))
        fallback = shlex.quote(str(root) + "/")
        # Only consider semver-named subdirs so a stray sibling (latest/, backup/,
        # __pycache__/) can't be picked by `sort -V | tail` over the real version.
        # $0 is set to the resolved bash binary (passed as the first arg after
        # `-c`), so `exec "$0"` re-execs the same bash without needing PATH.
        inner = (
            f'R="$(ls -d {base}/*/ 2>/dev/null | grep -E \'/[0-9]+[.][0-9]+[.][0-9]+/$\' '
            f'| sort -V | tail -n 1)"; '
            f'[ -n "$R" ] || R={fallback}; '
            # bash READS the launcher script (exec "$0" = bash "$T" ...), so it
            # only needs to be readable, not executable; -x would wrongly no-op a
            # readable-but-not-+x launcher (e.g. after an extraction dropped the bit).
            f'T="${{R}}hooks/python-launcher.sh"; P="${{R}}hooks/run.py"; '
            f'[ -r "$T" ] && [ -r "$P" ] || exit 0; exec "$0" "$T" "$P" {command_args}{redirect}'
        )
        command = (
            f"{_BASH_RESOLVER_PREFIX}"
            f'TOKEN_OPTIMIZER_RUNTIME=codex {_posix_env_prefix}exec "$b" -c {shlex.quote(inner)} "$b"'
            f"{_BASH_RESOLVER_SUFFIX}"
        )
    else:
        launcher = shlex.quote(str(root / "hooks" / "python-launcher.sh"))
        runner = shlex.quote(str(root / "hooks" / "run.py"))
        command = (
            f"{_BASH_RESOLVER_PREFIX}"
            f'[ -r {launcher} ] && [ -r {runner} ] || exit 0; TOKEN_OPTIMIZER_RUNTIME=codex {_posix_env_prefix}exec "$b" {launcher} {runner} {command_args}'
            f"{redirect}{_BASH_RESOLVER_SUFFIX}"
        )
    return command


def _managed_hooks(
    *,
    enable_bash_compression: bool = False,
    enable_hot_path_hooks: bool = False,
    enable_prompt_hooks: bool = False,
    enable_subagent_hooks: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Build Codex project hooks.

    Default is the aggressive profile (max savings). SessionStart,
    UserPromptSubmit, PostToolUse, and Stop are routed through the consolidated
    runner scripts (hooks/sessionstart_runner.py, hooks/userpromptsubmit_runner.py,
    hooks/posttooluse_runner.py, hooks/stop_runner.py) so Codex users receive the
    same diagnostics-routing, systemMessage-preservation, and process-
    consolidation fixes as Claude Code. SubagentStart/Stop remain on the
    Codex-specific bridge (no runner exists for subagent events). Bash
    compression uses Codex's PreToolUse updatedInput and remains an explicit
    opt-in; it is inert on Codex builds that do not honor updatedInput.
    """
    hooks = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "hooks/stop_runner.py",
                            redirect_quiet=True,
                        ),
                        "timeout": 15,
                    }
                ]
            }
        ],
    }
    if enable_prompt_hooks:
        hooks.update({
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "hooks/sessionstart_runner.py",
                        ),
                        "timeout": 20,
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "hooks/userpromptsubmit_runner.py",
                        ),
                        "timeout": 20,
                    }
                ]
            }
        ],
        })
    if enable_subagent_hooks:
        hooks.update({
        "SubagentStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        # Not redirect_quiet: the sprawl nudge is emitted on
                        # stdout (only when the threshold is crossed; silent
                        # otherwise) so Codex can inject it as context.
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/codex_hook_bridge.py",
                            "subagent-start",
                        ),
                        "timeout": 6,
                    }
                ]
            }
        ],
        "SubagentStop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/codex_hook_bridge.py",
                            "subagent-stop",
                            redirect_quiet=True,
                        ),
                        "timeout": 6,
                    }
                ]
            }
        ],
        })
    if enable_hot_path_hooks:
        hooks["PostToolUse"] = [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        # TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT gates off the
                        # updatedToolOutput emission in bash_compress_hook (Codex
                        # does not honor that field). thrash_guard recording and
                        # archive_result still run -- only the emission is skipped.
                        "command": _hook_command(
                            "hooks/posttooluse_runner.py",
                            redirect_quiet=True,
                            extra_env={"TOKEN_OPTIMIZER_NO_UPDATED_TOOL_OUTPUT": "1"},
                        ),
                        "timeout": 10,
                    }
                ],
            },
        ]
    if enable_bash_compression:
        hooks["PreToolUse"] = [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/codex_command_compress.py",
                            "--quiet",
                        ),
                        "timeout": 8,
                    }
                ],
            }
        ]
    return hooks


def _resolve_project(project: Path) -> Path:
    try:
        resolved = project.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"project is not accessible: {project}") from exc
    if not resolved.is_dir():
        raise ValueError(f"project is not a directory: {resolved}")
    return resolved


def _hooks_path(project: Path) -> Path:
    project_root = _resolve_project(project)
    codex_dir = project_root / ".codex"
    if codex_dir.exists():
        if codex_dir.is_symlink() or not codex_dir.is_dir():
            raise ValueError(f"{codex_dir} must be a real directory, not a symlink or file")
        try:
            codex_resolved = codex_dir.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{codex_dir} is not accessible") from exc
        if not codex_resolved.is_relative_to(project_root):
            raise ValueError(f"{codex_dir} escapes project root")

    hooks_path = codex_dir / "hooks.json"
    if hooks_path.exists() and hooks_path.is_symlink():
        raise ValueError(f"{hooks_path} must not be a symlink")
    try:
        hooks_resolved = hooks_path.resolve(strict=hooks_path.exists())
    except OSError as exc:
        raise ValueError(f"{hooks_path} is not accessible") from exc
    if not hooks_resolved.is_relative_to(project_root):
        raise ValueError(f"{hooks_path} escapes project root")
    return hooks_path


def _global_hooks_path(*, ensure_dir: bool = False) -> Path:
    home = codex_home()
    if home.exists() and home.is_symlink():
        raise ValueError(f"{home} must not be a symlink")
    if ensure_dir:
        home.mkdir(parents=True, exist_ok=True)
    hooks_path = home / "hooks.json"
    if hooks_path.exists() and hooks_path.is_symlink():
        resolved = hooks_path.resolve(strict=False)
        user_home = Path.home().resolve(strict=True)
        if not resolved.is_relative_to(user_home):
            raise ValueError(f"{hooks_path} symlink target escapes user home")
        return hooks_path
    resolved = hooks_path.resolve(strict=False)
    if not resolved.is_relative_to(Path.home()):
        raise ValueError(f"{hooks_path} escapes user home")
    return hooks_path


def _load_hooks(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"hooks": {}}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} must contain a top-level hooks object")
    return data


# Anchors for the generated Windows versioned-install command signature.
# _hook_command has emitted exactly two such shapes: the current one, which
# invokes the consolidated runner through the FOR variable
# ("{base}\%R\hooks\run.py"), and the pre-5.13.12 broken one, which invoked it
# through the delayed-expansion var ("!TOKEN_OPTIMIZER_RUNTIME_ROOT!\hooks\
# run.py"). Both always carry the quoted set-assignment AND a hooks\run.py
# runner invocation rooted under a token-optimizer directory (or through one
# of OUR OWN variables, which a foreign command cannot use). Matching on the
# assignment alone silently deletes a user's own hook that merely references
# our env var, so all anchors must hold TOGETHER. (The patterns run on the
# JSON-serialized group, where a literal " arrives as \" and \ as \\, hence
# the escaped-quote and separator-class spellings.)
_WIN_ROOT_SET_RE = re.compile(r'set \\"TOKEN_OPTIMIZER_RUNTIME_ROOT=')
_WIN_RUNNER_RE = re.compile(r'hooks[/\\]+run\.py')
_WIN_TO_DIR_RE = re.compile(r'token-optimizer[/\\]')
_WIN_RUNNER_VIA_VAR_RE = re.compile(
    r'(?:!TOKEN_OPTIMIZER_RUNTIME_ROOT!|%R)[/\\]+hooks[/\\]+run\.py'
)


# The consolidated runners (hooks/<event>_runner.py) carry no
# "token-optimizer/scripts" text on POSIX marketplace installs, so without this
# signature a reinstall appended a second copy of each runner hook and uninstall
# left them all behind. Ours = a runner name AND our hooks/run.py (or
# python-launcher.sh) AND a path under a token-optimizer folder, together.
_RUNNER_SCRIPT_RE = re.compile(
    r'hooks[/\\]+(?:sessionstart|userpromptsubmit|posttooluse|stop)_runner\.py'
)
_RUNNER_ENTRY_RE = re.compile(r'hooks[/\\]+(?:run\.py|python-launcher\.sh)|\$\{R\}hooks/')


def _is_token_optimizer_group(group: Any) -> bool:
    # The path marker misses generated Windows commands: on versioned
    # marketplace installs the baked paths use backslashes
    # (...\token-optimizer\X.Y.Z\hooks\run.py) and consolidated-runner args
    # like hooks/stop_runner.py, so "token-optimizer/scripts" never appears
    # in them. Those commands are instead identified by their full generated
    # signature, so a foreign hook that merely contains
    # set "TOKEN_OPTIMIZER_RUNTIME_ROOT= (or %TOKEN_OPTIMIZER_RUNTIME_ROOT%,
    # or a POSIX-style VAR=x prefix) is never swept up by reinstall/uninstall.
    serialized = json.dumps(group, sort_keys=True)
    if TOKEN_OPTIMIZER_MARKER in serialized:
        return True
    if (
        _RUNNER_SCRIPT_RE.search(serialized)
        and _RUNNER_ENTRY_RE.search(serialized)
        and _WIN_TO_DIR_RE.search(serialized)
    ):
        return True
    if not _WIN_ROOT_SET_RE.search(serialized):
        return False
    return (
        _WIN_RUNNER_VIA_VAR_RE.search(serialized) is not None
        or (
            _WIN_RUNNER_RE.search(serialized) is not None
            and _WIN_TO_DIR_RE.search(serialized) is not None
        )
    )


# Matches the --baked-root token of a generated launcher-file command, as
# list2cmdline emitted it: quoted when the path carries spaces or cmd
# metacharacters, bare otherwise.
_BAKED_ROOT_ARG_RE = re.compile(
    r'--baked-root\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>[^\s]+))'
)


def _normalize_baked_root(match: "re.Match[str]") -> str:
    """Replace a semver --baked-root leaf with the signature sentinel,
    preserving the original separator style and quoting."""
    value = match.group("quoted")
    quoted = value is not None
    if not quoted:
        value = match.group("bare")
    leaf = re.search(r"^(.*[/\\])\d+\.\d+\.\d+$", value)
    if leaf is None:
        return match.group(0)
    normalized = f"--baked-root {leaf.group(1)}{_SIGNATURE_VERSION}"
    return f'--baked-root "{leaf.group(1)}{_SIGNATURE_VERSION}"' if quoted else normalized


def _launcher_signature(command):
    """Canonical form of a generated Windows launcher, modulo the version dir.

    Preserving an equivalent installed command avoids an unnecessary trust
    review on upgrade. Launcher-file shape: the only versioned path in the
    command is the --baked-root fallback (the launcher itself lives at the
    stable parent of the version dirs), so normalize just that leaf to a
    sentinel. Everything else -- interpreter, launcher path, env, script,
    args, redirect, marker -- must match verbatim, so any tampering forces
    the trust review it should. Legacy base64 launchers compare verbatim:
    they are never equivalent to the launcher-file shape, so the next
    install replaces them (the one-time change that ships the issue #183
    fix), and a blob tampered in any way differs from the untampered
    command.
    """
    if _LAUNCHER_MARKER not in command:
        return command
    if _LAUNCHER_B64_RE.search(command):
        return command
    return _BAKED_ROOT_ARG_RE.sub(_normalize_baked_root, command)


def _merge_hooks(
    existing: dict[str, Any],
    *,
    enable_bash_compression: bool = False,
    enable_hot_path_hooks: bool = False,
    enable_prompt_hooks: bool = False,
    enable_subagent_hooks: bool = False,
) -> dict[str, Any]:
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    managed = _managed_hooks(
        enable_bash_compression=enable_bash_compression,
        enable_hot_path_hooks=enable_hot_path_hooks,
        enable_prompt_hooks=enable_prompt_hooks,
        enable_subagent_hooks=enable_subagent_hooks,
    )
    for event in SUPPORTED_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            groups = []
        for fresh_group in managed.get(event, []):
            for old_group in groups:
                if not _is_token_optimizer_group(old_group):
                    continue
                old_handlers = old_group.get('hooks', [])
                fresh_handlers = fresh_group.get('hooks', [])
                if len(old_handlers) == len(fresh_handlers) == 1:
                    old_handler, fresh_handler = old_handlers[0], fresh_handlers[0]
                    if _launcher_signature(old_handler.get('command', '')) == _launcher_signature(fresh_handler.get('command', '')):
                        fresh_handler['command'] = old_handler['command']
        hooks[event] = [group for group in groups if not _is_token_optimizer_group(group)]
        hooks[event].extend(managed.get(event, []))
        if not hooks[event]:
            hooks.pop(event, None)
    return result


def _remove_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _is_token_optimizer_group(group)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    return result


def _windows_launcher_install_path(root: Path) -> Path:
    """Stable launcher location: NEXT TO the versioned install dirs, so the
    marketplace swapping .../token-optimizer/<X.Y.Z>/ on upgrade never takes
    the file the baked commands invoke."""
    return root.parent / _LAUNCHER_FILENAME


def _install_windows_launcher(root: Path) -> str:
    """Copy the canonical hooks/windows-launcher.py to its stable install
    path. Idempotent (no rewrite when already current) and atomic
    (unique same-directory temp + replace, always cleaned up), so a
    concurrent hook run never reads a half-written launcher."""
    source = _repo_root() / "hooks" / _LAUNCHER_FILENAME
    target = _windows_launcher_install_path(root)
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read Windows launcher {source}: {exc}") from exc
    try:
        if target.read_bytes() == content:
            return f"already current: {target}"
    except OSError:
        pass
    # Unique same-directory temp (concurrent installers never contend on a
    # fixed name), always cleaned up; os.replace within one filesystem is
    # atomic, so a concurrent hook run never reads a half-written launcher.
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f"{target.name}.", suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(content)
        os.replace(tmp, target)
    except OSError as exc:
        raise ValueError(
            f"cannot install Windows launcher to {target}: {exc}"
        ) from exc
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return f"installed: {target}"


def install(
    project: Path,
    *,
    is_global: bool = False,
    dry_run: bool = False,
    skip_compact_prompt: bool = False,
    force_compact_prompt: bool = False,
    enable_bash_compression: bool = False,
    enable_hot_path_hooks: bool = False,
    enable_prompt_hooks: bool = False,
    enable_subagent_hooks: bool = False,
    enable_status_line: bool = False,
    force_status_line: bool = False,
) -> tuple[Path, str, dict[str, Any]]:
    path = _global_hooks_path(ensure_dir=not dry_run) if is_global else _hooks_path(project)
    existing = _load_hooks(path)
    updated = _merge_hooks(
        existing,
        enable_bash_compression=enable_bash_compression,
        enable_hot_path_hooks=enable_hot_path_hooks,
        enable_prompt_hooks=enable_prompt_hooks,
        enable_subagent_hooks=enable_subagent_hooks,
    )
    details: dict[str, Any] = {
        "hook_events": sorted(updated.get("hooks", {}).keys()),
        "bash_compression": enable_bash_compression,
        "hot_path_hooks": enable_hot_path_hooks,
        "prompt_hooks": enable_prompt_hooks,
        "subagent_hooks": enable_subagent_hooks,
        "compact_prompt": "skipped" if skip_compact_prompt else None,
        "status_line": "skipped" if not enable_status_line else None,
    }
    root = _repo_root()
    if sys.platform == "win32" and _SEMVER_DIR_RE.match(root.name):
        # The baked commands invoke the launcher by path; it must exist
        # before hooks.json points at it. A failed write aborts the install
        # loudly instead of leaving hooks that cannot start.
        details["windows_launcher"] = (
            f"would install: {_windows_launcher_install_path(root)}"
            if dry_run
            else _install_windows_launcher(root)
        )
    if dry_run and not skip_compact_prompt:
        details["compact_prompt"] = codex_compact_prompt.plan_install(force=force_compact_prompt)
    if dry_run and enable_status_line:
        details["status_line"] = codex_statusline.plan_install(force=force_status_line)
    if not dry_run:
        if not skip_compact_prompt:
            details["compact_prompt"] = codex_compact_prompt.install(force=force_compact_prompt)
        if enable_status_line:
            details["status_line"] = codex_statusline.install(force=force_status_line)
        codex_io.atomic_write_json(path, updated)
    return path, "installed", details


_CMD_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def neutralize_launcher_command(command: str) -> str | None:
    """Rewrite one of our cmd.exe-only launcher commands (issue #276 shape or
    the older quoted-first-token shape) into the shell-neutral form of issue
    #278, or return None when it is already neutral, not ours, or cannot be
    written that way (the caller then falls back to the #276 repair).

    Pure text transform over the generated shape: optional
    ``set "TOKEN_OPTIMIZER_RUNTIME=codex" &&`` prefix, quoted interpreter,
    quoted launcher, ``--baked-root``, ``--env`` pairs, ``--``, quoted script
    and args, marker, optional ``>NUL 2>&1``. Anything that does not fit is
    left alone rather than guessed at.
    """
    if _LAUNCHER_MARKER not in command or _LAUNCHER_B64_RE.search(command):
        return None
    if is_shell_neutral_command(command):
        return None
    tokens = _CMD_TOKEN_RE.findall(command.strip())
    silent = False
    if tokens[-2:] == [">NUL", "2>&1"]:
        silent = True
        tokens = tokens[:-2]
    if tokens[:2] == ['set', '"TOKEN_OPTIMIZER_RUNTIME=codex"'] and tokens[2:3] == ["&&"]:
        tokens = tokens[3:]
    if len(tokens) < 6 or "--" not in tokens:
        return None
    if tokens[-1] != f'"{_LAUNCHER_MARKER}"':
        return None
    exe_raw = tokens[0].strip('"')
    exe = _shell_neutral_exe(exe_raw)
    if exe is None or tokens[0].count('"') not in (0, 2):
        return None
    out = [exe]
    for tok in tokens[1:]:
        if tok.startswith('"'):
            neutral = _shell_neutral_arg(tok[1:-1]) if tok.endswith('"') and len(tok) >= 2 else None
            if neutral is None:
                return None
            out.append(neutral)
        elif tok in ("--baked-root", "--env", "--"):
            out.append(tok)
        else:
            return None
    if silent:
        sep = out.index("--")
        out[sep:sep] = ["--env", _shell_neutral_arg("TOKEN_OPTIMIZER_HOOK_SILENT=1")]
    return " ".join(out)


def heal_quoted_launcher_commands(paths=None) -> int:
    """Repair Windows hook commands that PowerShell or cmd.exe cannot run.

    Versions before 5.13.57 saved launcher commands that open with the quoted
    python.exe path, which cmd.exe mangles (issue #276). 5.13.57 added a cmd-only
    ``set ... &&`` prefix, which PowerShell -- the shell Codex normally runs
    hooks in -- cannot parse (issue #278). Either way the hook fails before
    Python starts and can never repair itself, so any Token Optimizer run
    rewrites our own launcher commands into the shell-neutral form (or, if no
    such spelling exists, the cmd-only repair) and returns how many it
    fixed. Never raises.
    """
    if paths is None:
        paths = []
        try:
            paths.append(_global_hooks_path())
        except Exception:
            pass
        paths.append(Path.cwd() / ".codex" / "hooks.json")
    fixed = 0
    for path in paths:
        try:
            if not path.is_file():
                continue
            data = _load_hooks(path)
            changed = 0
            for groups in data.get("hooks", {}).values():
                if not isinstance(groups, list):
                    continue
                for group in groups:
                    if not _is_token_optimizer_group(group):
                        continue
                    for handler in group.get("hooks", []):
                        command = handler.get("command") if isinstance(handler, dict) else None
                        if not isinstance(command, str):
                            continue
                        neutral = neutralize_launcher_command(command)
                        if neutral is not None:
                            handler["command"] = neutral
                            changed += 1
                        elif (command.startswith('"') and _LAUNCHER_MARKER in command):
                            handler["command"] = f"{_WIN_UNQUOTED_PREFIX} {command}"
                            changed += 1
            if changed:
                codex_io.atomic_write_json(path, data)
                fixed += changed
        except Exception:
            continue
    return fixed


def uninstall(project: Path, *, is_global: bool = False, dry_run: bool = False) -> tuple[Path, str, dict[str, Any]]:
    path = _global_hooks_path() if is_global else _hooks_path(project)
    existing = _load_hooks(path)
    updated = _remove_hooks(existing)
    details: dict[str, Any] = {"hook_events": sorted(updated.get("hooks", {}).keys())}
    # Reverse the config.toml writes the installer made: the compact-prompt
    # managed block + prompt file and the [tui]
    # status-line managed block. Both are idempotent and scoped to TO's own
    # managed markers, so user-authored keys are never clobbered.
    if dry_run:
        details["compact_prompt"] = codex_compact_prompt.plan_uninstall()
        details["status_line"] = codex_statusline.plan_uninstall()
    else:
        details["compact_prompt"] = codex_compact_prompt.uninstall()
        details["status_line"] = codex_statusline.uninstall()
        codex_io.atomic_write_json(path, updated)
    return path, "removed", details


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Token Optimizer hooks for Codex (globally by default).")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--global", dest="use_global", action="store_true", default=True, help="Install hooks globally to ~/.codex/hooks.json (default)")
    target.add_argument("--project", default=None, help="Install hooks to a specific project directory instead of globally")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print intended action without writing")
    parser.add_argument("--uninstall", action="store_true", help="Remove Token Optimizer hooks")
    parser.add_argument(
        "--decode-launcher",
        metavar="COMMAND",
        default=None,
        help=(
            "Print the decoded Python bootstrap embedded in a LEGACY generated "
            "Windows launcher command (the base64 `python -c` exec-bootstrap retired "
            "for issue #183). Audit/diagnostic only: decodes and prints, "
            "never executes. Launcher-file commands are self-auditing: the "
            "invoked .py is plain source at the path in the command."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("quiet", "balanced", "telemetry", "aggressive"),
        default="aggressive",
        help=(
            "Hook profile: aggressive=max savings, all silent hooks (default); "
            "balanced=Stop+prompt hooks; quiet=Stop only; telemetry=Stop+PostToolUse. "
            "Bash compression stays opt-in (--enable-bash-compression) on every profile "
            "to preserve the user's existing command-output workflow."
        ),
    )
    parser.add_argument("--skip-compact-prompt", action="store_true", help="Do not install Codex compact prompt")
    parser.add_argument("--force-compact-prompt", action="store_true", help="Replace existing compact-prompt settings")
    parser.add_argument(
        "--enable-bash-compression",
        action="store_true",
        help="Compress eligible inspection commands through Codex updatedInput; preserves full originals and failures",
    )
    parser.add_argument("--disable-bash-compression", action="store_true", help="Deprecated no-op; Bash compression is off by default")
    parser.add_argument("--enable-hot-path-hooks", action="store_true", help="Opt into visible PostToolUse tool-output hooks")
    parser.add_argument(
        "--enable-prompt-hooks",
        action="store_true",
        help="Add visible SessionStart/UserPromptSubmit hooks when using --profile quiet; balanced already includes them",
    )
    parser.add_argument(
        "--enable-subagent-hooks",
        action="store_true",
        help="Add SubagentStart/SubagentStop hooks for real-time subagent sprawl nudges when using --profile quiet; balanced/aggressive include them",
    )
    parser.add_argument(
        "--no-subagent-hooks",
        action="store_true",
        help="Install without SubagentStart/SubagentStop hooks even on balanced/aggressive profiles",
    )
    parser.add_argument("--enable-status-line", action="store_true", help="Opt into Codex CLI context/status visibility")
    parser.add_argument("--force-status-line", action="store_true", help="Replace existing Codex [tui] status_line settings")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.decode_launcher is not None:
        decoded = decode_launcher_command(args.decode_launcher)
        if decoded is None:
            if _LAUNCHER_MARKER in args.decode_launcher:
                # Launcher-file shape: the invoked .py IS the audit trail.
                print(
                    "[Token Optimizer] launcher-file command: nothing embedded "
                    "to decode -- audit the plain source at the invoked "
                    f"{_LAUNCHER_FILENAME} path"
                )
                return 0
            print("[Token Optimizer] not a generated windows-launcher command", file=sys.stderr)
            return 1
        print(decoded, end="")
        return 0
    is_global = args.project is None
    try:
        project = Path(".") if is_global else _resolve_project(Path(args.project))
        if args.uninstall:
            path, action, details = uninstall(project, is_global=is_global, dry_run=args.dry_run)
        else:
            enable_prompt_hooks = args.enable_prompt_hooks or args.profile in {"balanced", "aggressive"}
            enable_hot_path_hooks = args.enable_hot_path_hooks or args.profile in {"telemetry", "aggressive"}
            # Command rewriting is supported by current Codex. Keep its activation
            # explicit because it changes the model-visible output of commands.
            enable_bash_compression = args.enable_bash_compression
            enable_subagent_hooks = (
                args.enable_subagent_hooks or args.profile in {"balanced", "aggressive"}
            ) and not args.no_subagent_hooks
            path, action, details = install(
                project,
                is_global=is_global,
                dry_run=args.dry_run,
                skip_compact_prompt=args.skip_compact_prompt,
                force_compact_prompt=args.force_compact_prompt,
                enable_bash_compression=enable_bash_compression,
                enable_hot_path_hooks=enable_hot_path_hooks,
                enable_prompt_hooks=enable_prompt_hooks,
                enable_subagent_hooks=enable_subagent_hooks,
                enable_status_line=args.enable_status_line,
                force_status_line=args.force_status_line,
            )
            details["profile"] = args.profile
    except ValueError as exc:
        print(f"[Token Optimizer] {exc}", file=sys.stderr)
        return 1

    target = "global" if is_global else str(project)
    payload = {
        "action": action,
        "target": target,
        "hooks_path": str(path),
        "dry_run": args.dry_run,
        "details": details,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        prefix = "Would update" if args.dry_run else "Updated"
        print(f"[Token Optimizer] {prefix} {path} ({action})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
