#!/usr/bin/env python3
"""Install Token Optimizer Codex hooks globally or into a project workspace."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
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
    'do command -v "$b" >/dev/null 2>&1 && '
)
_BASH_RESOLVER_SUFFIX = "; done; exit 0"


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
        # Verified 2026-08-05 against Codex CLI source: codex-rs/hooks/src/
        # engine/command_runner.rs default_shell_command() spawns hooks as
        # `%COMSPEC% /C <command>` (fallback cmd.exe) on Windows unless the
        # user overrides the hook shell in config. So the cmd.exe syntax below
        # (for /f, 2^>NUL, >NUL 2>&1) is CORRECT here — do NOT
        # "bash-ify" it. The inverse bug: Claude Code runs hooks via
        # Git Bash, so measure.py's Claude-facing commands are POSIX-shaped.
        _win_env = ''.join(
            f'set "{k}={v}" && ' for k, v in extra_env.items()
        )
        if _SEMVER_DIR_RE.match(root.name):
            # CMD needs a Windows-native counterpart to the POSIX runtime
            # resolver below. cmd.exe parses a /C command line ONCE, before
            # anything on it runs: %VAR% expands to the pre-line value, and
            # `setlocal EnableDelayedExpansion` only takes effect from the
            # NEXT line, so a !VAR! reference on the same line is passed
            # through literally (issue #180: python received the verbatim
            # path "...\\!TOKEN_OPTIMIZER_RUNTIME_ROOT!\\hooks\\run.py" and
            # exited 1). Neither expansion form can see the for-loop
            # assignment on the same line. The one value that DOES land is
            # the FOR variable itself, so the runner path is built from %R
            # inside the do-body and no expansion form is needed.
            #
            # Fallback: the resolver always prints exactly one directory name
            # - the newest semver install, or the baked install directory
            # when the scan finds none - so the do-body still runs against
            # the baked path. (If powershell.exe itself cannot start, the
            # loop body never runs and the hook no-ops, the same fail-quiet
            # contract as the POSIX resolver's missing-bash path.)
            # The fallback is silent by design; TOKEN_OPTIMIZER_DEBUG=1 makes
            # it observable by appending a line to
            # <base>\token-optimizer-codex-resolver.log. A file is the only
            # channel that reaches the user here: resolver stderr is 2^>NUL'd
            # inside the for /f in-clause, and every extra stdout line would
            # run the do-body again with junk as %R. The added syntax is
            # deliberately free of ()<>%&| so cmd.exe's /C line parser never
            # sees it.
            #
            # Only the version LEAF NAME crosses the cmd/PowerShell pipe, and
            # it is always ASCII (it matched the semver regex); the base path
            # travels baked into the command line (Unicode-safe via
            # CreateProcessW), so non-ASCII install paths - e.g. a Hebrew or
            # CJK user profile name - are never mangled by a console-codepage
            # round-trip. Exactly one line is printed, so python runs once.
            base = str(root.parent)
            ps_base = base.replace("'", "''")
            ps_fallback = root.name.replace("'", "''")
            ps_command = (
                "$ErrorActionPreference='SilentlyContinue'; "
                f"$v = Get-ChildItem -LiteralPath '{ps_base}' -Directory | "
                "Where-Object { $_.Name -match '^\\d+\\.\\d+\\.\\d+$' } | "
                "Sort-Object { [version]$_.Name } -Descending | "
                "Select-Object -First 1 -ExpandProperty Name; "
                "if ($v) { $v } else { "
                "if ($env:TOKEN_OPTIMIZER_DEBUG) { $t = Get-Date -Format o; "
                f"Add-Content -LiteralPath '{ps_base}\\token-optimizer-codex-resolver.log' "
                f'-Value "$t codex hook resolver found no semver install; using baked {ps_fallback}" '
                "-ErrorAction SilentlyContinue }; "
                f"'{ps_fallback}' }}"
            )
            resolver = subprocess.list2cmdline(
                ["powershell", "-NoProfile", "-Command", ps_command]
            )
            python = subprocess.list2cmdline([sys.executable])
            script_args = subprocess.list2cmdline([script, *args])
            command = (
                'set "TOKEN_OPTIMIZER_RUNTIME=codex" && '
                f'for /f "delims=" %R in (\'{resolver} 2^>NUL\') '
                f'do @set "TOKEN_OPTIMIZER_RUNTIME_ROOT={base}\\%R" && '
                f'{_win_env}'
                f'{python} "{base}\\%R\\hooks\\run.py" '
                f"{script_args}"
            )
        else:
            prefix = f'set "TOKEN_OPTIMIZER_RUNTIME=codex" && {_win_env}'
            argv = [sys.executable, str(root / "hooks" / "run.py"), script, *args]
        redirect = " >NUL 2>&1" if redirect_quiet else ""
        if _SEMVER_DIR_RE.match(root.name):
            return f"{command}{redirect}"
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
            f'T="${{R}}hooks/python-launcher.sh"; [ -r "$T" ] || exit 0; '
            f'exec "$0" "$T" "${{R}}hooks/run.py" {command_args}{redirect}'
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
            f'TOKEN_OPTIMIZER_RUNTIME=codex {_posix_env_prefix}exec "$b" {launcher} {runner} {command_args}'
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
    compression stays explicit opt-in (Codex cannot rewrite command input yet).
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
                            "skills/token-optimizer/scripts/bash_hook.py",
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
    if not _WIN_ROOT_SET_RE.search(serialized):
        return False
    return (
        _WIN_RUNNER_VIA_VAR_RE.search(serialized) is not None
        or (
            _WIN_RUNNER_RE.search(serialized) is not None
            and _WIN_TO_DIR_RE.search(serialized) is not None
        )
    )


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
        "--profile",
        choices=("quiet", "balanced", "telemetry", "aggressive"),
        default="aggressive",
        help=(
            "Hook profile: aggressive=max savings, all silent hooks (default); "
            "balanced=Stop+prompt hooks; quiet=Stop only; telemetry=Stop+PostToolUse. "
            "Bash compression stays opt-in (--enable-bash-compression) on every profile "
            "because Codex cannot rewrite command input yet."
        ),
    )
    parser.add_argument("--skip-compact-prompt", action="store_true", help="Do not install Codex compact prompt")
    parser.add_argument("--force-compact-prompt", action="store_true", help="Replace existing compact-prompt settings")
    parser.add_argument(
        "--enable-bash-compression",
        action="store_true",
        help="Experimental visible PreToolUse(Bash) hook; Codex does not yet support command rewriting",
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
    is_global = args.project is None
    try:
        project = Path(".") if is_global else _resolve_project(Path(args.project))
        if args.uninstall:
            path, action, details = uninstall(project, is_global=is_global, dry_run=args.dry_run)
        else:
            enable_prompt_hooks = args.enable_prompt_hooks or args.profile in {"balanced", "aggressive"}
            enable_hot_path_hooks = args.enable_hot_path_hooks or args.profile in {"telemetry", "aggressive"}
            # Bash compression stays explicit opt-in on every profile (including
            # aggressive): Codex PreToolUse cannot rewrite command input yet, so the
            # hook is non-functional AND visible. Enabling it by default would add a
            # visible row per Bash call with no token saving. Re-couple to the
            # aggressive profile once Codex supports input rewriting.
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
