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
import shutil
import sys
import time
from functools import lru_cache
from pathlib import Path

from runtime_env import plugin_data_env_vars, project_config_value, runtime_home

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
    """True if candidate is a real directory STRICTLY inside base, not a symlink.

    ``Path.is_relative_to`` is reflexive (a path is "relative to" itself), so
    without the explicit inequality check a candidate that resolves to ``base``
    itself would pass this confinement test. That matters because every other
    plugin's data directory lives as a sibling subdir directly under the same
    shared ``plugins/data`` root: a misconfigured or malicious env var pointed
    straight at that root (rather than at a real per-plugin identity subdir)
    would otherwise be accepted as "confined" while actually granting access to
    every plugin's data, not just this one's.
    """
    try:
        if not candidate.is_dir():
            return False
        if candidate.is_symlink():
            return False
        resolved = candidate.resolve(strict=True)
        base_resolved = base.resolve(strict=False)
        return resolved != base_resolved and resolved.is_relative_to(base_resolved)
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


def _registered_plugin_data_dirs() -> list[Path]:
    """Return safe identities still referenced by installed_plugins.json."""
    candidates: list[Path] = []
    registry = _safe_load_json(_INSTALLED_PLUGINS)
    if not isinstance(registry, dict):
        return candidates
    plugins = registry.get("plugins", {})
    if not isinstance(plugins, dict):
        return candidates
    for key in plugins:
        if not isinstance(key, str) or not key.startswith(_PLUGIN_NAME + "@"):
            continue
        marketplace = key.split("@", 1)[1]
        if not _SAFE_MARKETPLACE_NAME.match(marketplace):
            continue
        candidate = _PLUGIN_DATA_BASE / f"{_PLUGIN_NAME}-{marketplace}"
        if _is_safe_subdir(candidate, _PLUGIN_DATA_BASE):
            candidates.append(candidate)
    return sorted(set(candidates), key=lambda path: path.name)


def resolve_claude_plugin_data_env() -> Path | None:
    """Identity-checked resolver for the raw CLAUDE_PLUGIN_DATA env var.

    Returns CLAUDE_PLUGIN_DATA's resolved path ONLY when it is a declared
    identity of THIS plugin (present in installed_plugins.json via
    _registered_plugin_data_dirs) and confined under the shared plugins/data
    root as a real subdir (not the root itself — see _is_safe_subdir). A name
    merely starting with "token-optimizer" is NOT proof; a foreign plugin's
    CLAUDE_PLUGIN_DATA sharing the same plugins/data/ root is rejected.

    This is the SINGLE identity check every raw-CLAUDE_PLUGIN_DATA site shares
    (identity check): resolve_plugin_data_dir() below calls it directly, and so do
    the sibling sites that read CLAUDE_PLUGIN_DATA outside this module —
    hooks/run.py's consent-config read and detectors/cache_instability.py's
    detector-state write. None of the three duplicate the identity logic; a
    leaked/foreign CLAUDE_PLUGIN_DATA can misdirect none of them.
    """
    env_val = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if not env_val:
        return None
    try:
        resolved = Path(env_val).resolve(strict=False)
    except (OSError, ValueError):
        return None
    if not _is_safe_subdir(resolved, _PLUGIN_DATA_BASE):
        return None
    registered = {r.resolve(strict=False) for r in _registered_plugin_data_dirs()}
    if resolved in registered:
        return resolved
    return None  # Foreign plugin's CLAUDE_PLUGIN_DATA leaked in.


@lru_cache(maxsize=1)
def resolve_plugin_data_dir() -> Path | None:
    """Return the active plugin-data directory.

    Priority:
      1. TOKEN_OPTIMIZER_PLUGIN_DATA (dedicated, unconditional — but still
         confined to a REAL subdir of plugins/data, never the shared root
         itself). Checked FIRST regardless of CLAUDE_PLUGIN_DATA's validity,
         so it wins even when CLAUDE_PLUGIN_DATA ALSO happens to resolve to a
         valid registered identity (the common in-plugin hook case).
      2. CLAUDE_PLUGIN_DATA — accepted ONLY when the resolved path is a
         declared identity of THIS plugin, via resolve_claude_plugin_data_env().
      3. installed_plugins.json lookup for the active marketplace install
      4. Stable lexical glob fallback across token-optimizer-* data dirs
      5. None (caller falls back to the legacy _backups/ path)

    All discovered paths are confined under the active runtime's plugin-data
    tree and reject symlinks. The env-var path must resolve under that runtime
    home.
    """
    # TOKEN_OPTIMIZER_PLUGIN_DATA must win even when CLAUDE_PLUGIN_DATA ALSO
    # resolves to a valid registered identity. The previous implementation
    # iterated _PLUGIN_DATA_ENV_VARS (CLAUDE_PLUGIN_DATA listed first for the
    # Claude runtime) in a single for-loop and returned on the first env var
    # that matched — so a valid CLAUDE_PLUGIN_DATA silently shadowed the
    # dedicated override, exactly backwards from the documented priority.
    # Checking the dedicated var first, unconditionally, fixes that.
    if "TOKEN_OPTIMIZER_PLUGIN_DATA" in _PLUGIN_DATA_ENV_VARS:
        dedicated_val = os.environ.get("TOKEN_OPTIMIZER_PLUGIN_DATA")
        if dedicated_val:
            try:
                resolved = Path(dedicated_val).resolve(strict=False)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None and _is_safe_subdir(resolved, _PLUGIN_DATA_BASE):
                return resolved  # dedicated — unconditional, but a real subdir

    if "CLAUDE_PLUGIN_DATA" in _PLUGIN_DATA_ENV_VARS:
        claude_identity = resolve_claude_plugin_data_env()
        if claude_identity is not None:
            return claude_identity

    candidates = _registered_plugin_data_dirs()

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

    candidates.sort(key=lambda p: p.name)
    return candidates[0]


def _all_plugin_data_dirs() -> list[Path]:
    """Return every safe Token Optimizer identity in deterministic order."""
    candidates: list[Path] = []
    try:
        if _PLUGIN_DATA_BASE.is_dir():
            for path in _PLUGIN_DATA_BASE.glob(f"{_PLUGIN_NAME}-*"):
                if _is_safe_subdir(path, _PLUGIN_DATA_BASE):
                    candidates.append(path)
    except OSError:
        pass
    return sorted(set(candidates), key=lambda path: path.name)


def find_sibling_plugin_data_dirs(active: Path | None = None) -> list[Path]:
    """Return safe plugin-data identities other than the active identity."""
    active = active if active is not None else resolve_plugin_data_dir()
    try:
        active_resolved = active.resolve(strict=False) if active is not None else None
    except (OSError, ValueError):
        active_resolved = None
    siblings: list[Path] = []
    for candidate in _all_plugin_data_dirs():
        try:
            if active_resolved is not None and candidate.resolve(strict=False) == active_resolved:
                continue
        except (OSError, ValueError):
            continue
        siblings.append(candidate)
    return siblings


def snapshot_roots_for_home(home: Path) -> list[Path]:
    """Snapshot roots Token Optimizer may have used under another runtime's
    *home*: each safe ``plugins/data/token-optimizer-*`` identity's ``data``
    dir, then the legacy ``_backups/token-optimizer``. Read-side recovery only
    (expand's cross-runtime fallback); never a write target. Never raises."""
    roots: list[Path] = []
    try:
        base = home / "plugins" / "data"
        if base.is_dir() and not base.is_symlink():
            for path in sorted(base.glob(f"{_PLUGIN_NAME}-*"), key=lambda p: p.name):
                if _is_safe_subdir(path, base):
                    roots.append(path / "data")
    except OSError:
        pass
    roots.append(home / "_backups" / "token-optimizer")
    return roots


def snapshot_dir_candidates() -> list[Path]:
    """Return active then sibling snapshot roots for read-side recovery."""
    override = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()
    if override:
        return [Path(override).expanduser()]
    active = resolve_plugin_data_dir()
    candidates: list[Path] = []
    if active is not None:
        candidates.append(active / "data")
    candidates.extend(path / "data" for path in find_sibling_plugin_data_dirs(active))
    if not candidates:
        candidates.append(_LEGACY_BACKUP_DIR)
    return candidates


def _latest_tree_mtime(path: Path) -> float | None:
    """Return newest mtime, treating symlinks/read errors as non-reclaimable."""
    try:
        latest = os.lstat(path).st_mtime
        errors: list[OSError] = []
        for root, dirnames, filenames in os.walk(
            path,
            followlinks=False,
            onerror=errors.append,
        ):
            root_path = Path(root)
            for name in [*dirnames, *filenames]:
                item = root_path / name
                if item.is_symlink():
                    return None
                latest = max(latest, os.lstat(item).st_mtime)
        if errors:
            return None
        return latest
    except OSError:
        return None


def _announce_orphan_reclamation(message: str, active: Path | None) -> None:
    """Emit to stderr and persist destructive-action notices when possible."""
    print(message, file=sys.stderr)
    if active is None:
        return
    try:
        log_dir = active / "data"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(log_dir, 0o700)
        log_path = log_dir / "orphan-reclamation.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{int(time.time())} {message}\n")
        os.chmod(log_path, 0o600)
    except OSError:
        pass


def reclaim_orphaned_plugin_data_dirs(min_age_days: int | None = None) -> list[Path]:
    """Reclaim old sibling identities only with explicit user opt-in.

    Detection is always safe and announced. Deletion requires
    TOKEN_OPTIMIZER_RECLAIM_ORPHANED_DATA=1, a minimum seven-day age gate, a
    symlink-free readable tree, and an identity distinct from the active one.
    """
    if min_age_days is None:
        try:
            min_age_days = int(os.environ.get(
                "TOKEN_OPTIMIZER_ORPHAN_RETENTION_DAYS", "30"
            ))
        except (TypeError, ValueError):
            min_age_days = 30
    min_age_days = max(7, min_age_days)
    cutoff = time.time() - (min_age_days * 86400)
    active = resolve_plugin_data_dir()
    registered = {
        path.resolve(strict=False) for path in _registered_plugin_data_dirs()
    }
    if not registered:
        return []
    eligible = [
        path for path in find_sibling_plugin_data_dirs(active)
        if path.resolve(strict=False) not in registered
        and (latest := _latest_tree_mtime(path)) is not None
        and latest < cutoff
    ]
    if not eligible:
        return []
    opted_in = os.environ.get(
        "TOKEN_OPTIMIZER_RECLAIM_ORPHANED_DATA", ""
    ).strip().lower() in _TRUTHY_ENV
    if not opted_in:
        names = ", ".join(path.name for path in eligible)
        print(
            "[Token Optimizer] Old orphaned plugin data detected but not deleted: "
            f"{names}. Set TOKEN_OPTIMIZER_RECLAIM_ORPHANED_DATA=1 to reclaim "
            f"identities inactive for at least {min_age_days} days.",
            file=sys.stderr,
        )
        return []
    removed: list[Path] = []
    for path in eligible:
        # Eligibility is only a candidate snapshot. A reinstall can update the
        # registry, active identity, or directory contents while the initial
        # tree scan is running, so repeat every destructive precondition at
        # the point of deletion.
        resolve_plugin_data_dir.cache_clear()
        active_now = resolve_plugin_data_dir()
        try:
            target = path.resolve(strict=False)
            active_resolved = (
                active_now.resolve(strict=False)
                if active_now is not None else None
            )
            registered_now = {
                item.resolve(strict=False)
                for item in _registered_plugin_data_dirs()
            }
            latest_now = _latest_tree_mtime(path)
        except OSError:
            continue
        if (
            active_resolved == target
            or target in registered_now
            or latest_now is None
            or latest_now >= cutoff
        ):
            continue
        # Even with every precondition rechecked above, check-then-rmtree is not
        # atomic: rmtree resolves the PATHNAME, so an installer that re-registers
        # and recreates this identity inside the gap gets its live data deleted.
        # Claim the tree with an atomic rename first. After it succeeds the
        # original pathname is free -- a concurrent reinstall lands on a fresh
        # directory and is unaffected by what we do next -- and we are deleting a
        # name only this process knows.
        quarantine = path.with_name(
            f".{path.name}.reclaiming-{os.getpid()}-{int(time.time() * 1000)}"
        )
        try:
            os.rename(path, quarantine)
        except OSError:
            continue
        # Revalidate against the claimed tree. If it turns out someone touched it
        # or registered it in the gap, put it back exactly where it was.
        try:
            latest_claimed = _latest_tree_mtime(quarantine)
            resolve_plugin_data_dir.cache_clear()
            active_after = resolve_plugin_data_dir()
            active_after_resolved = (
                active_after.resolve(strict=False)
                if active_after is not None else None
            )
            registered_after = {
                item.resolve(strict=False)
                for item in _registered_plugin_data_dirs()
            }
        except OSError:
            latest_claimed, active_after_resolved, registered_after = None, None, set()
        if (
            latest_claimed is None
            or latest_claimed >= cutoff
            or active_after_resolved == target
            or target in registered_after
        ):
            try:
                os.rename(quarantine, path)
            except OSError:
                pass
            continue
        try:
            shutil.rmtree(quarantine)
            if not quarantine.exists():
                removed.append(path)
                _announce_orphan_reclamation(
                    "[Token Optimizer] Reclaimed orphaned plugin data identity: "
                    f"{path.name}",
                    active,
                )
        except OSError as exc:
            _announce_orphan_reclamation(
                "[Token Optimizer] Could not reclaim orphaned plugin data identity "
                f"{path.name}: {exc.__class__.__name__}",
                active,
            )
    return removed


def resolve_snapshot_dir() -> Path:
    """Return the data directory for snapshots, caches, and decision logs.

    v5.11.1: TOKEN_OPTIMIZER_SNAPSHOT_DIR overrides the resolved location when
    set, so tests (and any sandboxed caller) can pin a private data dir without
    monkeypatching every module that resolves SNAPSHOT_DIR independently
    (measure/archive_result/read_cache each call this). Production no-op when
    unset. Any module that imports this resolver therefore honors the sandbox.
    """
    override = os.environ.get("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    plugin_data = resolve_plugin_data_dir()
    if plugin_data is not None:
        return plugin_data / "data"
    return _LEGACY_BACKUP_DIR


# Common truthy/falsy strings accepted in env-var boolean checks.
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_FALSY_ENV = frozenset({"0", "false", "no", "off", ""})

_warned_flag_values: set = set()


def _warn_unrecognized_flag_value(flag_name: str, value, config_path) -> None:
    """One-time stderr warning for an unrecognized config flag value.

    Diagnostics go to stderr only (stdout must stay clean for hook contracts).
    Deduped per (flag, type) so a misconfigured config.json warns once, not on
    every hook invocation. The raw value is NEVER echoed in full — only its type
    and length — so a secret mistakenly placed as a flag value can't leak into
    logs/CI captures. The dedup key is a string so an unhashable
    JSON value (list/dict) can't crash the caller.
    """
    sig = (flag_name, type(value).__name__)
    if sig in _warned_flag_values:
        return
    _warned_flag_values.add(sig)
    try:
        try:
            length = len(value)
        except TypeError:
            length = "n/a"
        print(
            f"[Token Optimizer] WARNING: {config_path}: flag '{flag_name}' has an "
            f"unrecognized value (type={type(value).__name__}, length={length}); "
            f"expected a boolean or one of {sorted(_TRUTHY_ENV | _FALSY_ENV)}. "
            "Ignoring and using the next source/default.",
            file=sys.stderr,
        )
    except Exception:
        pass


def interpret_flag_value(value, *, env_truthy_value: str | None = None):
    """Single source of truth for reading ONE feature-flag value.

    Accepts an env string OR a config JSON bool/number/string. Returns:
      * True / False when the value decisively enables or disables, or
      * None when the value is unrecognized/unset, so the CALLER falls through
        to its next source (env -> config -> default).

    Binary flags accept "1"/"true"/"yes"/"on" as True and "0"/"false"/"no"/
    "off"/"" as False, case-insensitively; a genuine JSON bool/number is taken
    as-is. Tri-state flags (env_truthy_value set, e.g. structure-map "beta") are
    ALWAYS decisive: only the exact token (case-insensitive) enables, everything
    else disables. Both readers (this module's hot path and measure.py's
    dashboard) call this so they can never disagree.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip()
    if env_truthy_value is not None:
        return s.lower() == env_truthy_value.strip().lower()
    low = s.lower()
    if low in _TRUTHY_ENV:
        return True
    if low in _FALSY_ENV:  # includes ""
        return False
    return None


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
    4. Project config: $CLAUDE_PROJECT_DIR/.claude/token-optimizer.json
       (runtime_env.project_config: committed, allowlisted keys only)
    5. default

    Value interpretation is delegated to interpret_flag_value so the env path,
    the config path, and measure.py's dashboard reader all share one gate. An
    unrecognized value at any source falls through to the next (warned once for
    config sources); the default is the final fallback.
    """
    env_val = os.environ.get(env_var)
    if env_val is not None:
        r = interpret_flag_value(env_val, env_truthy_value=env_truthy_value)
        if r is not None:
            return r
        # Unrecognized env value: don't guess, fall through to config/default.

    config_paths = [_USER_CONFIG_DIR / "config.json"]
    plugin_data = resolve_plugin_data_dir()
    if plugin_data is not None:
        config_paths.append(plugin_data / "config" / "config.json")

    for config_path in config_paths:
        cfg = _safe_load_json(config_path)
        if isinstance(cfg, dict) and flag_name in cfg:
            r = interpret_flag_value(cfg[flag_name], env_truthy_value=env_truthy_value)
            if r is not None:
                return r
            # Unrecognized string / unusable JSON type: warn once, fall through.
            _warn_unrecognized_flag_value(flag_name, cfg[flag_name], config_path)
            continue

    project_val = project_config_value(flag_name)
    if project_val is not None:
        r = interpret_flag_value(project_val, env_truthy_value=env_truthy_value)
        if r is not None:
            return r

    return default
