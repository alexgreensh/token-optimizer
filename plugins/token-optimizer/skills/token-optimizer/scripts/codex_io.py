from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Write content to path atomically via tempfile+rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """Serialize data as JSON and write to path atomically."""
    atomic_write(path, json.dumps(data, indent=2, sort_keys=False) + "\n", mode=mode)


def validate_codex_path(path: Path, home: Path) -> None:
    """Raise ValueError if path or its parent escapes home or is a symlink."""
    if home.exists():
        if home.is_symlink() or not home.is_dir():
            raise ValueError(f"{home} must be a real directory")
        home_resolved = home.resolve(strict=True)
    else:
        home_resolved = home.resolve(strict=False)

    parent = path.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"{parent} must be a real directory")
        if not parent.resolve(strict=True).is_relative_to(home_resolved):
            raise ValueError(f"{parent} escapes Codex home")
    if path.exists():
        if path.is_symlink():
            raise ValueError(f"{path} must not be a symlink")
        if not path.resolve(strict=True).is_relative_to(home_resolved):
            raise ValueError(f"{path} escapes Codex home")


def ensure_codex_child(home: Path, *parts: str, create: bool = True) -> Path:
    """Return a path under home, creating missing directories and validating symlinks."""
    if home.exists():
        if home.is_symlink() or not home.is_dir():
            raise ValueError(f"{home} must be a real directory")
    elif create:
        home.mkdir(mode=0o700)
    home_resolved = home.resolve(strict=home.exists())

    target = home.joinpath(*parts)
    parent = target.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"{parent} must be a real directory")
        parent_resolved = parent.resolve(strict=True)
        if not parent_resolved.is_relative_to(home_resolved):
            raise ValueError(f"{parent} escapes Codex home")
    elif create:
        parent.mkdir(mode=0o700)
    if target.exists() and target.is_symlink():
        raise ValueError(f"{target} must not be a symlink")
    target_resolved = target.resolve(strict=target.exists())
    if not target_resolved.is_relative_to(home_resolved):
        raise ValueError(f"{target} escapes Codex home")
    return target
