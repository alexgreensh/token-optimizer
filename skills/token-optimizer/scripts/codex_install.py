#!/usr/bin/env python3
"""Install Token Optimizer Codex hooks globally or into a project workspace."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import codex_compact_prompt
import codex_io
import codex_statusline
from runtime_env import codex_home

TOKEN_OPTIMIZER_MARKER = "token-optimizer/scripts"
SUPPORTED_EVENTS = ("PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _hook_command(script: str, *args: str, redirect_quiet: bool = False) -> str:
    root = _repo_root()
    launcher = shlex.quote(str(root / "hooks" / "python-launcher.sh"))
    runner = shlex.quote(str(root / "hooks" / "run.py"))
    command_args = " ".join(shlex.quote(arg) for arg in (script, *args))
    command = f"TOKEN_OPTIMIZER_RUNTIME=codex bash {launcher} {runner} {command_args}"
    if redirect_quiet:
        command += " >/dev/null 2>&1"
    return command


def _managed_hooks(
    *,
    enable_bash_compression: bool = False,
    enable_hot_path_hooks: bool = False,
    enable_prompt_hooks: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Build Codex project hooks.

    Codex Desktop currently renders every successful hook as a visible row.
    Default to balanced: session/prompt hooks for quality tracking plus Stop for
    refresh/continuity. Per-tool hot-path hooks remain explicit opt-in.
    """
    hooks = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/measure.py",
                            "session-end-flush",
                            "--trigger",
                            "stop",
                            "--quiet",
                            "--defer",
                            redirect_quiet=True,
                        ),
                        "timeout": 8,
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
                            "skills/token-optimizer/scripts/codex_hook_bridge.py",
                            "session-start",
                        ),
                        "timeout": 15,
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
                            "skills/token-optimizer/scripts/codex_hook_bridge.py",
                            "user-prompt-submit",
                        ),
                        "timeout": 12,
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
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/context_intel.py",
                            "--quiet",
                        ),
                        "timeout": 10,
                    }
                ],
            },
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/archive_result.py",
                            "--quiet",
                            redirect_quiet=True,
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
        raise ValueError(f"{hooks_path} must not be a symlink")
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


def _is_token_optimizer_group(group: Any) -> bool:
    return TOKEN_OPTIMIZER_MARKER in json.dumps(group, sort_keys=True)


def _merge_hooks(
    existing: dict[str, Any],
    *,
    enable_bash_compression: bool = False,
    enable_hot_path_hooks: bool = False,
    enable_prompt_hooks: bool = False,
) -> dict[str, Any]:
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    managed = _managed_hooks(
        enable_bash_compression=enable_bash_compression,
        enable_hot_path_hooks=enable_hot_path_hooks,
        enable_prompt_hooks=enable_prompt_hooks,
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
    )
    details: dict[str, Any] = {
        "hook_events": sorted(updated.get("hooks", {}).keys()),
        "bash_compression": enable_bash_compression,
        "hot_path_hooks": enable_hot_path_hooks,
        "prompt_hooks": enable_prompt_hooks,
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
    details = {"hook_events": sorted(updated.get("hooks", {}).keys())}
    if not dry_run:
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
        default="balanced",
        help=(
            "Hook profile: balanced=Stop+prompt hooks (default); quiet=Stop only; "
            "telemetry=Stop+PostToolUse; aggressive=all currently available hooks"
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
            enable_bash_compression = args.enable_bash_compression or args.profile == "aggressive"
            path, action, details = install(
                project,
                is_global=is_global,
                dry_run=args.dry_run,
                skip_compact_prompt=args.skip_compact_prompt,
                force_compact_prompt=args.force_compact_prompt,
                enable_bash_compression=enable_bash_compression,
                enable_hot_path_hooks=enable_hot_path_hooks,
                enable_prompt_hooks=enable_prompt_hooks,
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
