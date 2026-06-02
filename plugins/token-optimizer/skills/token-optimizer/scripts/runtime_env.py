"""Runtime home detection shared by Claude Code and Codex adapters.

This module keeps the first Codex integration step deliberately simple:

- Claude Code stays the default runtime unless Codex is clearly indicated.
- Codex activates when CODEX_HOME is set or TOKEN_OPTIMIZER_RUNTIME=codex.
- Callers can keep legacy variable names while resolving to the correct home.

The goal is to let Token Optimizer share one Python core while the Codex
adapter grows feature-by-feature on top of it.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

_RUNTIME_OVERRIDE = "TOKEN_OPTIMIZER_RUNTIME"
_RUNTIME_CLAUDE = "claude"
_RUNTIME_CODEX = "codex"
_VALID_RUNTIMES = frozenset({_RUNTIME_CLAUDE, _RUNTIME_CODEX})
_CLAUDE_PLUGIN_ENVS = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
_CODEX_HOME_ENV = "CODEX_HOME"


def _home_root() -> Path:
    """Return the resolved user home used for env-path confinement."""
    return Path.home().resolve(strict=False)


def _is_safe_home_dir(path: Path) -> bool:
    """True when path is a non-symlink directory under the user's home."""
    try:
        if not path.is_absolute():
            return False
        resolved = path.resolve(strict=False)
        home = _home_root()
        if resolved == home or not resolved.is_relative_to(home):
            return False
        if path.exists():
            return path.is_dir() and not path.is_symlink()
        return False
    except (OSError, ValueError):
        return False


def _safe_home_from_env(env_var: str, fallback: Path) -> Path:
    """Resolve a runtime-home env var without letting it escape user home."""
    raw_val = os.environ.get(env_var, "").strip()
    if not raw_val:
        return fallback
    candidate = Path(raw_val).expanduser()
    result: Path | None = candidate.resolve(strict=False) if _is_safe_home_dir(candidate) else None
    if result is None:
        print(f"[Token Optimizer] Warning: {env_var}={raw_val!r} rejected (not a safe directory). Using default.", file=sys.stderr)
        return fallback
    return result  # type: ignore[return-value]


@functools.lru_cache(maxsize=None)
def detect_runtime() -> str:
    """Return the active runtime name.

    Priority:
      1. Explicit override via TOKEN_OPTIMIZER_RUNTIME
      2. Claude plugin env vars imply Claude Code
      3. CODEX_HOME implies Codex
      4. Default to Claude Code for backward compatibility
    """
    override = os.environ.get(_RUNTIME_OVERRIDE, "").strip().lower()
    if override in _VALID_RUNTIMES:
        return override

    if any(os.environ.get(env_var) for env_var in _CLAUDE_PLUGIN_ENVS):
        return _RUNTIME_CLAUDE

    if os.environ.get(_CODEX_HOME_ENV):
        return _RUNTIME_CODEX

    return _RUNTIME_CLAUDE


def claude_home() -> Path:
    """Return Claude Code's home directory."""
    return Path.home() / ".claude"


def codex_home() -> Path:
    """Return Codex's home directory, safely honoring CODEX_HOME when valid."""
    return _safe_home_from_env(_CODEX_HOME_ENV, Path.home() / ".codex")


def runtime_home() -> Path:
    """Return the home directory used by the active runtime."""
    runtime = detect_runtime()

    if runtime == _RUNTIME_CODEX:
        return codex_home()

    return claude_home()


def plugin_data_env_vars() -> tuple[str, ...]:
    """Return plugin-data env vars in runtime-specific priority order."""
    if detect_runtime() == _RUNTIME_CODEX:
        return ("TOKEN_OPTIMIZER_PLUGIN_DATA",)
    return ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")


def runtime_name_for_humans() -> str:
    """Return a display label for logs and user-facing output."""
    return "Codex" if detect_runtime() == _RUNTIME_CODEX else "Claude Code"
