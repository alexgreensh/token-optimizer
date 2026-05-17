#!/usr/bin/env python3
"""Codex-specific install readiness checks for Token Optimizer."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from runtime_env import codex_home, detect_runtime, runtime_home
import codex_statusline

SUPPORTED_HOOK_EVENTS = {
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
}
BASH_ONLY_EVENTS = {"PreToolUse", "PermissionRequest", "PostToolUse"}
REQUIRED_FILES = (
    ".codex-plugin/plugin.json",
    "hooks/python-launcher.sh",
    "hooks/run.py",
    "skills/token-optimizer/scripts/codex_hook_bridge.py",
    "skills/token-optimizer/scripts/codex_session.py",
    "skills/token-optimizer/scripts/codex_compact_prompt.py",
    "skills/token-optimizer/scripts/codex_statusline.py",
    "skills/token-optimizer/scripts/codex_install.py",
    "skills/token-optimizer/scripts/bash_hook.py",
    "skills/token-optimizer/scripts/bash_compress.py",
    "skills/token-optimizer/scripts/context_intel.py",
    "skills/token-optimizer/scripts/archive_result.py",
    "skills/token-optimizer/scripts/measure.py",
    "skills/token-optimizer/scripts/outline.py",
    "skills/token-optimizer/scripts/runtime_env.py",
)

SKILL_INSTALL_FILES = (
    "SKILL.md",
    "scripts/codex_hook_bridge.py",
    "scripts/codex_session.py",
    "scripts/codex_compact_prompt.py",
    "scripts/codex_statusline.py",
    "scripts/codex_install.py",
    "scripts/measure.py",
    "scripts/runtime_env.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_plugin_repo(root: Path) -> bool:
    return (root / ".codex-plugin" / "plugin.json").exists()


def _check(status: str, name: str, detail: str) -> dict[str, str]:
    return {"status": status, "name": name, "detail": detail}


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _codex_home_warnings() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    home = codex_home()
    raw = os.environ.get("CODEX_HOME", "").strip()

    if raw:
        requested = Path(raw).expanduser()
        try:
            requested_resolved = requested.resolve(strict=False)
        except (OSError, ValueError):
            requested_resolved = requested
        if requested_resolved != home.resolve(strict=False):
            checks.append(_check("FAIL", "CODEX_HOME", f"ignored unsafe CODEX_HOME={raw!r}; using {home}"))
        else:
            checks.append(_check("OK", "CODEX_HOME", str(home)))
    else:
        checks.append(_check("OK", "CODEX_HOME", f"default {home}"))

    if home.exists():
        checks.append(_check("OK", "Codex home exists", str(home)))
    else:
        checks.append(_check("WARN", "Codex home exists", f"{home} does not exist yet"))

    parent = home if home.exists() else home.parent
    if os.access(parent, os.W_OK):
        checks.append(_check("OK", "Codex storage writable", str(parent)))
    else:
        checks.append(_check("FAIL", "Codex storage writable", f"{parent} is not writable"))

    return checks


def _manifest_checks(root: Path) -> list[dict[str, str]]:
    path = root / ".codex-plugin" / "plugin.json"
    data, error = _load_json(path)
    if error:
        return [_check("FAIL", "Plugin manifest", error)]
    if not isinstance(data, dict):
        return [_check("FAIL", "Plugin manifest", "manifest is not a JSON object")]

    checks = []
    name = data.get("name")
    version = data.get("version")
    if name == "token-optimizer":
        checks.append(_check("OK", "Plugin name", name))
    else:
        checks.append(_check("FAIL", "Plugin name", f"expected token-optimizer, got {name!r}"))
    if isinstance(version, str) and version.strip():
        checks.append(_check("OK", "Plugin version", version))
    else:
        checks.append(_check("FAIL", "Plugin version", "missing or blank version"))
    return checks


def _hook_config_checks(root: Path) -> list[dict[str, str]]:
    path = root / ".codex" / "hooks.json"
    data, error = _load_json(path)
    if error:
        return [_check("FAIL", "Codex hooks", error)]
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return [_check("FAIL", "Codex hooks", "expected top-level hooks object")]

    checks = []
    hooks = data["hooks"]
    unsupported = sorted(set(hooks) - SUPPORTED_HOOK_EVENTS)
    if unsupported:
        checks.append(_check("FAIL", "Hook events", f"unsupported events: {', '.join(unsupported)}"))
    else:
        checks.append(_check("OK", "Hook events", ", ".join(sorted(hooks))))

    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            checks.append(_check("FAIL", f"{event_name} groups", "expected list"))
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                checks.append(_check("FAIL", f"{event_name}[{group_index}]", "expected object"))
                continue
            matcher = group.get("matcher")
            if event_name in BASH_ONLY_EVENTS and matcher not in (None, "", "Bash", "^Bash$"):
                checks.append(
                    _check("FAIL", f"{event_name} matcher", f"Codex currently supports Bash hook payloads, got {matcher!r}")
                )
            for hook_index, hook in enumerate(group.get("hooks", [])):
                if not isinstance(hook, dict):
                    checks.append(_check("FAIL", f"{event_name} hook", "expected object"))
                    continue
                if hook.get("type") != "command":
                    checks.append(_check("FAIL", f"{event_name} hook", f"unsupported type {hook.get('type')!r}"))
                if hook.get("async"):
                    checks.append(_check("FAIL", f"{event_name} hook", "async hooks are skipped by current Codex"))
                timeout = hook.get("timeout")
                if isinstance(timeout, (int, float)) and timeout > 300:
                    checks.append(_check("WARN", f"{event_name} hook timeout", f"timeout={timeout} may be in milliseconds (Codex expects seconds)"))
                command = hook.get("command", "")
                if not isinstance(command, str) or not command.strip():
                    checks.append(_check("FAIL", f"{event_name} hook", "missing command"))
                elif _command_mentions_missing_path(root, command):
                    checks.append(_check("FAIL", f"{event_name} hook {hook_index}", f"missing file in command: {command}"))

    if not any(c["status"] == "FAIL" and c["name"].startswith("Hook") for c in checks):
        checks.append(_check("OK", "Hook commands", "all referenced repo files exist"))
    return checks


def _command_mentions_missing_path(root: Path, command: str) -> bool:
    for match in re.findall(r"(?:(?:\\.|/)?(?:hooks|skills)/[A-Za-z0-9_./-]+)", command):
        candidate = root / match.lstrip("./")
        if not candidate.exists():
            return True
    return False


def _required_file_checks(root: Path) -> list[dict[str, str]]:
    missing = [rel for rel in REQUIRED_FILES if not (root / rel).exists()]
    if missing:
        return [_check("FAIL", "Required files", ", ".join(missing))]
    return [_check("OK", "Required files", f"{len(REQUIRED_FILES)} present")]


def _skill_install_checks() -> list[dict[str, str]]:
    root = _skill_root()
    missing = [rel for rel in SKILL_INSTALL_FILES if not (root / rel).exists()]
    if missing:
        return [_check("FAIL", "Installed skill files", ", ".join(missing))]
    return [
        _check("OK", "Install shape", f"standalone Codex skill at {root}"),
        _check("OK", "Installed skill files", f"{len(SKILL_INSTALL_FILES)} present"),
    ]


def _compact_prompt_check() -> dict[str, str]:
    config_path = codex_home() / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return _check("FAIL", "Compact prompt", f"{config_path} not found; run measure.py codex-compact-prompt --install")
    expected = codex_home() / "token-optimizer" / "codex-compact-prompt.md"
    if str(expected) in text and expected.exists():
        return _check("OK", "Compact prompt", str(expected))
    if "compact_prompt" in text or "experimental_compact_prompt_file" in text:
        return _check("WARN", "Compact prompt", "custom compact prompt configured")
    return _check("FAIL", "Compact prompt", "not configured yet; run measure.py codex-compact-prompt --install")


def _status_line_check() -> dict[str, str]:
    state = codex_statusline.status()
    if state.startswith("configured: Token Optimizer"):
        return _check("OK", "Codex CLI status line", state)
    if state.startswith("configured: custom"):
        return _check("WARN", "Codex CLI status line", state)
    return _check("WARN", "Codex CLI status line", "not configured; rerun codex-install with --enable-status-line")


def _global_hook_check() -> dict[str, str]:
    hooks_path = codex_home() / "hooks.json"
    data, error = _load_json(hooks_path)
    if error:
        return _check("FAIL", "Global hooks", f"{hooks_path} not found; run measure.py codex-install")
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return _check("FAIL", "Global hooks", f"{hooks_path} has no hooks object")
    if "token-optimizer/scripts" in json.dumps(data, sort_keys=True):
        return _check("OK", "Global hooks", str(hooks_path))
    return _check("WARN", "Global hooks", f"no Token Optimizer hooks in {hooks_path}; run measure.py codex-install")


def _global_hook_config_checks() -> list[dict[str, str]]:
    hooks_path = codex_home() / "hooks.json"
    data, error = _load_json(hooks_path)
    if error:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return []
    return _validate_hook_structure(data, hooks_path)


def _validate_hook_structure(data: dict[str, Any], source_path: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    hooks = data["hooks"]
    unsupported = sorted(set(hooks) - SUPPORTED_HOOK_EVENTS)
    if unsupported:
        checks.append(_check("FAIL", f"Hook events ({source_path.name})", f"unsupported events: {', '.join(unsupported)}"))

    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                if hook.get("async"):
                    checks.append(_check("FAIL", f"{event_name} hook", "async hooks are skipped by current Codex"))
                timeout = hook.get("timeout")
                if isinstance(timeout, (int, float)) and timeout > 300:
                    checks.append(_check("WARN", f"{event_name} hook timeout", f"timeout={timeout} may be in milliseconds (Codex expects seconds)"))
    return checks


def _project_hook_check(project: Path) -> dict[str, str]:
    hooks_path = project / ".codex" / "hooks.json"
    data, error = _load_json(hooks_path)
    if error:
        return _check("WARN", "Project hooks", f"{hooks_path} not found; per-project hooks are optional when global hooks are installed")
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return _check("FAIL", "Project hooks", f"{hooks_path} has no hooks object")
    if "token-optimizer/scripts" in json.dumps(data, sort_keys=True):
        return _check("OK", "Project hooks", str(hooks_path))
    return _check("WARN", "Project hooks", f"no Token Optimizer hooks installed in {hooks_path}; manual refresh mode avoids visible Codex hook rows")


def _project_feature_checks(project: Path) -> list[dict[str, str]]:
    hooks_path = project / ".codex" / "hooks.json"
    data, error = _load_json(hooks_path)
    if error or not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return []

    hooks = data.get("hooks", {})
    checks = []
    if _has_project_hook(hooks, "PreToolUse", "Bash", "bash_hook.py"):
        checks.append(_check("OK", "Feature: Bash compression", "enabled for PreToolUse(Bash)"))
    else:
        checks.append(_check("OK", "Feature: Bash compression", "off by default to avoid visible Codex PreToolUse hook spam"))

    required_features = (
        ("Session continuity and dashboard refresh", "Stop", None, "session-end-flush"),
    )
    optional_noisy_features = (
        ("Prompt quality nudges", "UserPromptSubmit", None, "codex_hook_bridge.py"),
        ("Tool output archive", "PostToolUse", "Bash", "archive_result.py"),
        ("Context intelligence", "PostToolUse", "Bash", "context_intel.py"),
    )
    for feature, event, matcher, needle in required_features:
        if _has_project_hook(hooks, event, matcher, needle):
            checks.append(_check("OK", f"Feature: {feature}", "available in current Codex adapter"))
        else:
            checks.append(_check("WARN", f"Feature: {feature}", f"manual refresh mode; install low-noise Stop hook with measure.py codex-install --project {project}"))
    for feature, event, matcher, needle in optional_noisy_features:
        if _has_project_hook(hooks, event, matcher, needle):
            checks.append(_check("OK", f"Optional feature: {feature}", "enabled; Codex Desktop will show visible hook rows"))
        else:
            checks.append(_check("OK", f"Optional feature: {feature}", "off by default to avoid visible Codex hook rows"))

    repo_root = _repo_root()
    parser_path = (
        repo_root / "skills/token-optimizer/scripts/codex_session.py"
        if _is_plugin_repo(repo_root)
        else _skill_root() / "scripts/codex_session.py"
    )
    if parser_path.exists():
        checks.append(_check("OK", "Feature: Dashboard session parsing", "available in current Codex adapter"))
    else:
        checks.append(_check("FAIL", "Feature: Dashboard session parsing", f"missing {parser_path}"))

    checks.append(
        _check(
            "WARN",
            "Codex API limitations",
            "read deltas, structure maps, dynamic compaction, and StopFailure recovery need additional Codex hook payloads",
        )
    )

    return checks


def _has_project_hook(hooks: dict[str, Any], event: str, matcher: str | None, command_needle: str) -> bool:
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not isinstance(group, dict):
            continue
        if matcher is not None and group.get("matcher") not in (matcher, f"^{matcher}$"):
            continue
        for hook in group.get("hooks", []):
            if not isinstance(hook, dict) or hook.get("type") != "command":
                continue
            command = hook.get("command", "")
            if isinstance(command, str) and command_needle in command:
                return True
    return False


def run_checks(project: Path | None = None) -> list[dict[str, str]]:
    root = _repo_root()
    project = project or Path.cwd()
    checks = [
        _check("OK", "Repo root", str(root)),
        _check("OK", "Detected runtime", detect_runtime()),
        _check("OK", "Runtime home", str(runtime_home())),
    ]
    checks.extend(_codex_home_warnings())
    if _is_plugin_repo(root):
        checks.extend(_required_file_checks(root))
        checks.extend(_manifest_checks(root))
        checks.extend(_hook_config_checks(root))
    else:
        checks.extend(_skill_install_checks())
    checks.append(_compact_prompt_check())
    checks.append(_status_line_check())
    checks.append(_global_hook_check())
    checks.extend(_global_hook_config_checks())
    checks.append(_project_hook_check(project))
    checks.extend(_project_feature_checks(project))
    return checks


def _print_text(checks: list[dict[str, str]]) -> None:
    print("\nToken Optimizer Codex Doctor")
    print("=" * 28)
    for check in checks:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    counts = {status: sum(1 for check in checks if check["status"] == status) for status in ("OK", "WARN", "FAIL")}
    print(f"\nSummary: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Token Optimizer Codex adapter readiness.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    parser.add_argument("--project", default=".", help="Project directory whose .codex/hooks.json should be checked")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = Path(args.project).expanduser().resolve(strict=False)
    checks = run_checks(project=project)
    if args.json:
        print(json.dumps({"project": str(project), "checks": checks}, indent=2))
    else:
        _print_text(checks)
    return 1 if any(check["status"] == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
