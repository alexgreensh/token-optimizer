#!/usr/bin/env python3
"""Install Token Optimizer Codex hooks into a project workspace."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

import codex_compact_prompt

TOKEN_OPTIMIZER_MARKER = "token-optimizer/scripts"
SUPPORTED_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _hook_command(script: str, *args: str, redirect_quiet: bool = False) -> str:
    root = _repo_root()
    launcher = shlex.quote(str(root / "hooks" / "python-launcher.sh"))
    runner = shlex.quote(str(root / "hooks" / "run.py"))
    command_args = " ".join(shlex.quote(arg) for arg in (script, *args))
    command = f"bash {launcher} {runner} {command_args}"
    if redirect_quiet:
        command += " >/dev/null 2>&1"
    return command


def _managed_hooks() -> dict[str, list[dict[str, Any]]]:
    return {
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
                ]
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
        "PostToolUse": [
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
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(
                            "skills/token-optimizer/scripts/measure.py",
                            "compact-capture",
                            "--trigger",
                            "stop",
                            "--quiet",
                        ),
                        "timeout": 20,
                    }
                ]
            }
        ],
    }


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


def _merge_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    managed = _managed_hooks()
    for event in SUPPORTED_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            groups = []
        hooks[event] = [group for group in groups if not _is_token_optimizer_group(group)]
        hooks[event].extend(managed[event])
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


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    if path.parent.exists():
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise ValueError(f"{path.parent} must be a real directory")
    else:
        path.parent.mkdir(mode=0o700)
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install(
    project: Path,
    *,
    dry_run: bool = False,
    skip_compact_prompt: bool = False,
    force_compact_prompt: bool = False,
) -> tuple[Path, str]:
    path = _hooks_path(project)
    existing = _load_hooks(path)
    updated = _merge_hooks(existing)
    if not dry_run:
        if not skip_compact_prompt:
            codex_compact_prompt.install(force=force_compact_prompt)
        _atomic_write_json(path, updated)
    return path, "installed"


def uninstall(project: Path, *, dry_run: bool = False) -> tuple[Path, str]:
    path = _hooks_path(project)
    existing = _load_hooks(path)
    updated = _remove_hooks(existing)
    if not dry_run:
        _atomic_write_json(path, updated)
    return path, "removed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Token Optimizer hooks into a Codex project.")
    parser.add_argument("--project", default=".", help="Project directory to configure")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print intended action without writing")
    parser.add_argument("--uninstall", action="store_true", help="Remove Token Optimizer hooks from the project")
    parser.add_argument("--skip-compact-prompt", action="store_true", help="Do not install Codex compact prompt")
    parser.add_argument("--force-compact-prompt", action="store_true", help="Replace existing compact-prompt settings")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project = _resolve_project(Path(args.project))
        if args.uninstall:
            path, action = uninstall(project, dry_run=args.dry_run)
        else:
            path, action = install(
                project,
                dry_run=args.dry_run,
                skip_compact_prompt=args.skip_compact_prompt,
                force_compact_prompt=args.force_compact_prompt,
            )
    except ValueError as exc:
        print(f"[Token Optimizer] {exc}", file=sys.stderr)
        return 1

    payload = {"action": action, "project": str(project), "hooks_path": str(path), "dry_run": args.dry_run}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        prefix = "Would update" if args.dry_run else "Updated"
        print(f"[Token Optimizer] {prefix} {path} ({action})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
