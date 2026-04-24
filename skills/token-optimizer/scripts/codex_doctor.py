#!/usr/bin/env python3
"""Codex-specific install readiness checks for Token Optimizer."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from runtime_env import codex_home, detect_runtime, runtime_home

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
    ".codex/hooks.json",
    "hooks/python-launcher.sh",
    "hooks/run.py",
    "skills/token-optimizer/scripts/codex_hook_bridge.py",
    "skills/token-optimizer/scripts/codex_compact_prompt.py",
    "skills/token-optimizer/scripts/context_intel.py",
    "skills/token-optimizer/scripts/archive_result.py",
    "skills/token-optimizer/scripts/measure.py",
    "skills/token-optimizer/scripts/outline.py",
    "skills/token-optimizer/scripts/runtime_env.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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


def _compact_prompt_check() -> dict[str, str]:
    config_path = codex_home() / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return _check("WARN", "Compact prompt", f"{config_path} not found")
    expected = codex_home() / "token-optimizer" / "codex-compact-prompt.md"
    if str(expected) in text and expected.exists():
        return _check("OK", "Compact prompt", str(expected))
    if "compact_prompt" in text or "experimental_compact_prompt_file" in text:
        return _check("WARN", "Compact prompt", "custom compact prompt configured")
    return _check("WARN", "Compact prompt", "not configured yet")


def run_checks() -> list[dict[str, str]]:
    root = _repo_root()
    checks = [
        _check("OK", "Repo root", str(root)),
        _check("OK", "Detected runtime", detect_runtime()),
        _check("OK", "Runtime home", str(runtime_home())),
    ]
    checks.extend(_codex_home_warnings())
    checks.extend(_required_file_checks(root))
    checks.extend(_manifest_checks(root))
    checks.extend(_hook_config_checks(root))
    checks.append(_compact_prompt_check())
    return checks


def _print_text(checks: list[dict[str, str]]) -> None:
    print("\nToken Optimizer Codex Doctor")
    print("=" * 28)
    for check in checks:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    counts = {status: sum(1 for check in checks if check["status"] == status) for status in ("OK", "WARN", "FAIL")}
    print(f"\nSummary: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL")


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    checks = run_checks()
    if as_json:
        print(json.dumps({"checks": checks}, indent=2))
    else:
        _print_text(checks)
    return 1 if any(check["status"] == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
