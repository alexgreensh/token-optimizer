"""Shared path and config resolution for Token Optimizer.

Single source of truth for two recurring lookups that previously diverged across
hook scripts:

1. Plugin-data directory: env var > installed_plugins.json discovery > legacy
   _backups/ fallback. The installed_plugins.json walk lets dashboard CLI runs
   find live data when CLAUDE_PLUGIN_DATA is not set in the parent env.

2. v5 feature flag check: env var > user config > plugin-data config > default.
   User config wins over plugin-data config so manual edits to
   ~/.claude/token-optimizer/config.json take effect even after the plugin
   writes its own config (the dashboard toggle writes to plugin-data only,
   and missing keys there used to mask user-level enables).

Hot-path safe: only stdlib imports, no I/O at import time. Discovery results are
cached with lru_cache(maxsize=1) so repeated calls within a single hook process
share one filesystem traversal.

Security: all returned paths are confined under the active runtime home and
reject symlinks to prevent registry-key path traversal and symlink-based write
redirection.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from runtime_env import plugin_data_env_vars, runtime_home

_RUNTIME_HOME = runtime_home()
_USER_CONFIG_DIR = _RUNTIME_HOME / "token-optimizer"
_LEGACY_BACKUP_DIR = _RUNTIME_HOME / "_backups" / "token-optimizer"
_INSTALLED_PLUGINS = _RUNTIME_HOME / "plugins" / "installed_plugins.json"
_PLUGIN_DATA_BASE = _RUNTIME_HOME / "plugins" / "data"
_PLUGIN_NAME = "token-optimizer"
_PLUGIN_DATA_ENV_VARS = plugin_data_env_vars()

# Bound JSON reads in hot-path hooks. 1 MB is generous for plugin metadata and
# user config; larger files are treated as malformed and skipped silently.
_MAX_CONFIG_BYTES = 1_048_576

# Marketplace names map to filesystem paths. Allow only conservative chars.
_SAFE_MARKETPLACE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_subdir(candidate: Path, base: Path) -> bool:
    """True if candidate is a real directory inside base, not a symlink."""
    try:
        if not candidate.is_dir():
            return False
        if candidate.is_symlink():
            return False
        resolved = candidate.resolve(strict=True)
        base_resolved = base.resolve(strict=False)
        return resolved.is_relative_to(base_resolved)
    except (OSError, ValueError):
        return False


def _plugin_data_env_value() -> str | None:
    """Return the first runtime-appropriate plugin-data env value."""
    for env_var in _PLUGIN_DATA_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def _safe_load_json(path: Path):
    """Read and parse JSON with size + recursion guards. Returns None on failure."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None


@lru_cache(maxsize=1)
def resolve_plugin_data_dir() -> Path | None:
    """Return the active plugin-data directory.

    Priority:
      1. Runtime-appropriate plugin data env var
      2. installed_plugins.json lookup for the active marketplace install
      3. Glob fallback to most-recently-modified token-optimizer-* data dir
      4. None (caller falls back to the legacy _backups/ path)

    All discovered paths are confined under the active runtime's plugin-data
    tree and reject symlinks. The env-var path must resolve under that runtime
    home.
    """
    env_val = _plugin_data_env_value()
    if env_val:
        try:
            env_path = Path(env_val)
            resolved = env_path.resolve(strict=False)
            if _is_safe_subdir(resolved, _PLUGIN_DATA_BASE):
                return resolved
        except (OSError, ValueError):
            pass

    candidates: list[Path] = []

    registry = _safe_load_json(_INSTALLED_PLUGINS)
    if isinstance(registry, dict):
        plugins = registry.get("plugins", {})
        if isinstance(plugins, dict):
            for key in plugins:
                if not isinstance(key, str) or not key.startswith(_PLUGIN_NAME + "@"):
                    continue
                marketplace = key.split("@", 1)[1]
                if not _SAFE_MARKETPLACE_NAME.match(marketplace):
                    continue
                candidate = _PLUGIN_DATA_BASE / f"{_PLUGIN_NAME}-{marketplace}"
                if _is_safe_subdir(candidate, _PLUGIN_DATA_BASE):
                    candidates.append(candidate)

    if not candidates:
        try:
            if _PLUGIN_DATA_BASE.is_dir():
                for p in _PLUGIN_DATA_BASE.glob(f"{_PLUGIN_NAME}-*"):
                    if _is_safe_subdir(p, _PLUGIN_DATA_BASE):
                        candidates.append(p)
        except OSError:
            pass

    if not candidates:
        return None

    try:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return candidates[0]


def resolve_snapshot_dir() -> Path:
    """Return the data directory for snapshots, caches, and decision logs."""
    plugin_data = resolve_plugin_data_dir()
    if plugin_data is not None:
        return plugin_data / "data"
    return _LEGACY_BACKUP_DIR


# Common truthy/falsy strings accepted in env-var boolean checks.
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_FALSY_ENV = frozenset({"0", "false", "no", "off", ""})


def is_v5_flag_enabled(
    flag_name: str,
    env_var: str,
    *,
    default: bool,
    env_truthy_value: str | None = None,
) -> bool:
    """Check a v5 feature flag in priority order.

    1. Environment variable
    2. User config: <runtime-home>/token-optimizer/config.json
    3. Plugin-data config: <plugin-data>/config/config.json
    4. default

    Env parsing: when env_truthy_value is None (default), accepts the common
    boolean strings "1"/"true"/"yes"/"on" as True (case-insensitive) and
    "0"/"false"/"no"/"off"/"" as False; any other value falls through to
    config/default. When env_truthy_value is supplied (tri-state flags like
    structure-map "beta"), only an exact string match returns True.
    """
    env_val = os.environ.get(env_var)
    if env_val is not None:
        if env_truthy_value is not None:
            return env_val == env_truthy_value
        normalized = env_val.strip().lower()
        if normalized in _TRUTHY_ENV:
            return True
        if normalized in _FALSY_ENV:
            return False
        # Unrecognized value: don't guess, fall through to config/default.

    config_paths = [_USER_CONFIG_DIR / "config.json"]
    plugin_data = resolve_plugin_data_dir()
    if plugin_data is not None:
        config_paths.append(plugin_data / "config" / "config.json")

    for config_path in config_paths:
        cfg = _safe_load_json(config_path)
        if isinstance(cfg, dict) and flag_name in cfg:
            return bool(cfg[flag_name])

    return default
