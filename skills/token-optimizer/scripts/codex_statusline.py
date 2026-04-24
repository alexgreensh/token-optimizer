#!/usr/bin/env python3
"""Manage Token Optimizer's Codex CLI status line config."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from runtime_env import codex_home

MANAGED_BEGIN = "# BEGIN token-optimizer status line"
MANAGED_END = "# END token-optimizer status line"
STATUS_ITEMS = [
    "model-with-reasoning",
    "fast-mode",
    "context-remaining",
    "context-used",
    "used-tokens",
    "git-branch",
    "current-dir",
]
TERMINAL_TITLE_ITEMS = [
    "thread-title",
    "context-remaining",
    "git-branch",
]
TUI_HEADER_RE = re.compile(r"(?m)^[ \t]*\[tui\][ \t]*(?:#.*)?$")
TABLE_HEADER_RE = re.compile(r"(?m)^[ \t]*\[[^\]\n]+\][ \t]*(?:#.*)?$")
STATUS_LINE_RE = re.compile(r"(?m)^[ \t]*status_line[ \t]*=")
TERMINAL_TITLE_RE = re.compile(r"(?m)^[ \t]*terminal_title[ \t]*=")
SETTING_LINE_RE = re.compile(r"(?m)^([ \t]*)(status_line|terminal_title)([ \t]*=.*)$")


def _config_path() -> Path:
    return codex_home() / "config.toml"


def _validate_config_path(config_path: Path) -> None:
    home = codex_home()
    if home.exists():
        if home.is_symlink() or not home.is_dir():
            raise ValueError(f"{home} must be a real directory")
        home_resolved = home.resolve(strict=True)
    else:
        home_resolved = home.resolve(strict=False)

    parent = config_path.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"{parent} must be a real directory")
        if not parent.resolve(strict=True).is_relative_to(home_resolved):
            raise ValueError(f"{parent} escapes Codex home")
    if config_path.exists():
        if config_path.is_symlink():
            raise ValueError(f"{config_path} must not be a symlink")
        if not config_path.resolve(strict=True).is_relative_to(home_resolved):
            raise ValueError(f"{config_path} escapes Codex home")


def _ensure_config_path(config_path: Path) -> Path:
    home = codex_home()
    if home.exists():
        if home.is_symlink() or not home.is_dir():
            raise ValueError(f"{home} must be a real directory")
    else:
        home.mkdir(mode=0o700)
    _validate_config_path(config_path)
    return config_path


def _managed_block() -> str:
    status = json.dumps(STATUS_ITEMS)
    title = json.dumps(TERMINAL_TITLE_ITEMS)
    return "\n".join(
        [
            MANAGED_BEGIN,
            f"status_line = {status}",
            f"terminal_title = {title}",
            MANAGED_END,
            "",
        ]
    )


def _tui_span(text: str) -> tuple[int, int, int] | None:
    match = TUI_HEADER_RE.search(text)
    if not match:
        return None
    next_match = TABLE_HEADER_RE.search(text, match.end())
    end = next_match.start() if next_match else len(text)
    return match.start(), match.end(), end


def _comment_out_existing_settings(tui_body: str) -> str:
    return SETTING_LINE_RE.sub(r"\1# replaced by Token Optimizer: \2\3", tui_body)


def _replace_or_append_config(config_text: str, *, force: bool) -> tuple[str, str]:
    block = _managed_block()
    managed_re = re.compile(rf"(?ms)^{re.escape(MANAGED_BEGIN)}.*?^{re.escape(MANAGED_END)}\n?")
    if managed_re.search(config_text):
        return managed_re.sub(block, config_text), "updated"

    span = _tui_span(config_text)
    if span is None:
        suffix = "" if not config_text or config_text.endswith("\n") else "\n"
        return config_text + suffix + "\n[tui]\n" + block, "installed"

    _, header_end, table_end = span
    tui_body = config_text[header_end:table_end]
    has_status = STATUS_LINE_RE.search(tui_body)
    has_title = TERMINAL_TITLE_RE.search(tui_body)
    if (has_status or has_title) and not force:
        raise ValueError("config.toml already has [tui] status_line/terminal_title; rerun with --force to replace")
    if has_status or has_title:
        tui_body = _comment_out_existing_settings(tui_body)

    updated_table = config_text[:header_end] + "\n" + block + tui_body
    return updated_table + config_text[table_end:], "installed"


def plan_install(force: bool = False) -> dict[str, str | bool | list[str]]:
    """Validate a Codex CLI status-line install without writing files."""
    config_path = _config_path()
    _validate_config_path(config_path)
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    _, action = _replace_or_append_config(config_text, force=force)
    return {
        "action": action,
        "config_path": str(config_path),
        "would_create_config": not config_path.exists(),
        "status_line": STATUS_ITEMS,
        "terminal_title": TERMINAL_TITLE_ITEMS,
    }


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install(force: bool = False) -> str:
    config_path = _ensure_config_path(_config_path())
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    updated, action = _replace_or_append_config(config_text, force=force)
    _atomic_write(config_path, updated)
    return action


def status() -> str:
    config_path = _config_path()
    if not config_path.exists():
        return f"not configured: {config_path} not found"
    text = config_path.read_text(encoding="utf-8")
    if MANAGED_BEGIN in text and MANAGED_END in text:
        return "configured: Token Optimizer status line"
    span = _tui_span(text)
    if span is None:
        return "not configured"
    _, header_end, table_end = span
    tui_body = text[header_end:table_end]
    if STATUS_LINE_RE.search(tui_body) or TERMINAL_TITLE_RE.search(tui_body):
        return "configured: custom Codex TUI status line"
    return "not configured"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Token Optimizer's Codex CLI status line.")
    parser.add_argument("--install", action="store_true", help="Write Token Optimizer [tui] status_line settings")
    parser.add_argument("--force", action="store_true", help="Replace existing [tui] status_line/terminal_title settings")
    parser.add_argument("--status", action="store_true", help="Show current status-line config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status:
        print(status())
        return 0
    if args.install:
        try:
            action = install(force=args.force)
        except ValueError as exc:
            print(f"[Token Optimizer] {exc}", file=sys.stderr)
            return 1
        print(f"[Token Optimizer] Codex status line {action}: {_config_path()}")
        return 0
    print(_managed_block())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
