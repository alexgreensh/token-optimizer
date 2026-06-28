"""Runtime home detection shared by Claude Code, Codex, Hermes, OpenCode, and Copilot adapters.

This module keeps runtime integration deliberately simple:

- Claude Code stays the default runtime unless another runtime is clearly indicated.
- Codex activates when CODEX_HOME is set or TOKEN_OPTIMIZER_RUNTIME=codex.
- Hermes activates when HERMES_HOME is set or TOKEN_OPTIMIZER_RUNTIME=hermes.
- OpenCode activates when an OPENCODE_* env signal or an opencode ancestor process
  is detected, or TOKEN_OPTIMIZER_RUNTIME=opencode. OpenCode loads ~/.claude/skills
  by default, so this skill can be invoked from inside OpenCode; detecting it keeps
  the skill from scanning/mutating ~/.claude when the user is actually in OpenCode
  (issue #57).
- Copilot activates when COPILOT_HOME is set, a `copilot` ancestor process is
  detected, or TOKEN_OPTIMIZER_RUNTIME=copilot. The Copilot hook bridge always
  sets the explicit override; the other signals are a safety net so the skill
  never scans/mutates ~/.claude while actually running under GitHub Copilot.
- Callers can keep legacy variable names while resolving to the correct home.

The goal is to let Token Optimizer share one Python core while platform
adapters grow feature-by-feature on top of it.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

_RUNTIME_OVERRIDE = "TOKEN_OPTIMIZER_RUNTIME"
_RUNTIME_CLAUDE = "claude"
_RUNTIME_CODEX = "codex"
_RUNTIME_HERMES = "hermes"
_RUNTIME_OPENCODE = "opencode"
_RUNTIME_COPILOT = "copilot"
_VALID_RUNTIMES = frozenset(
    {_RUNTIME_CLAUDE, _RUNTIME_CODEX, _RUNTIME_HERMES, _RUNTIME_OPENCODE, _RUNTIME_COPILOT}
)
_CLAUDE_PLUGIN_ENVS = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
# Claude Code's official config-dir override. When set, Claude stores
# projects/, settings.json, etc. under this directory instead of ~/.claude.
_CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_CODEX_HOME_ENV = "CODEX_HOME"
_HERMES_HOME_ENV = "HERMES_HOME"
_COPILOT_HOME_ENV = "COPILOT_HOME"
# OpenCode launch/config env vars. Their presence in this process's environment
# is a strong signal we were spawned from within OpenCode. These are OpenCode's
# own documented variables (config/data/bin/client), not anything we set.
_OPENCODE_ENV_SIGNALS = (
    "OPENCODE_BIN",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CLIENT",
)
# Set to a truthy value to skip the (cheap, best-effort) process-tree scan used
# as a fallback OpenCode signal. Useful in tests/CI and locked-down sandboxes.
_PROC_SCAN_DISABLE_ENV = "TOKEN_OPTIMIZER_NO_PROC_SCAN"


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


def _opencode_env_signal() -> bool:
    """True when an OpenCode launch/config env var is present in this process."""
    return any(os.environ.get(var) for var in _OPENCODE_ENV_SIGNALS)


def _ancestor_in_process_tree(basenames: frozenset) -> bool:
    """Best-effort: is one of ``basenames`` an ancestor of this process?

    Used only as a fallback signal when a host CLI runs this skill without
    exporting an identifying env var. A single ``ps`` call is parsed in memory
    and the parent chain is walked from this PID upward.

    Checks both the executable name (comm) and the full command line (args),
    because some runtimes launch via a wrapper: OpenCode may run as
    ``node /path/to/opencode`` or ``bun run opencode``, where comm is ``node``
    or ``bun`` — not ``opencode``. For each ancestor process, the basename of
    the executable AND the basename of each command-line argument is checked
    against ``basenames`` (exact match, not substring, so a repo dir named
    ``opencode`` in a file path does not trigger a false positive).

    Never raises and never blocks for long: disabled on Windows, behind a short
    timeout, and skippable via TOKEN_OPTIMIZER_NO_PROC_SCAN.
    """
    if os.environ.get(_PROC_SCAN_DISABLE_ENV, "").strip():
        return False
    if sys.platform.startswith("win"):
        return False
    try:
        import subprocess

        # Use args= (full command line) instead of comm= (executable name only).
        # comm is limited to 15 chars on Linux and misses wrapper-launched
        # processes (node, bun, npx). args= gives the full command line so we
        # can check every token's basename.
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        if proc.returncode != 0:
            return False
        parents: dict[int, int] = {}
        cmdlines: dict[int, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            parents[pid] = ppid
            cmdlines[pid] = parts[2]
        pid = os.getpid()
        seen: set[int] = set()
        depth = 0
        while pid and pid > 1 and pid not in seen and depth < 40:
            seen.add(pid)
            depth += 1
            cmdline = cmdlines.get(pid, "")
            # Check the basename of every token in the command line.
            # This catches both direct invocation (``opencode``) and wrapper
            # launches (``node /usr/local/bin/opencode``, ``bun run opencode``).
            # Exact basename match, not substring: a path like
            # ``/home/user/projects/opencode/server.js`` has basename
            # ``server.js`` and will NOT match.
            for token in cmdline.split():
                token_base = os.path.basename(token).lower()
                if token_base in basenames:
                    return True
            pid = parents.get(pid, 0)
        return False
    except (OSError, subprocess.SubprocessError, ValueError):
        # OSError: ps not found or not executable (non-POSIX, restricted env).
        # SubprocessError: timeout, broken pipe, etc.
        # ValueError: unexpected ps output format.
        return False


_OPENCODE_BASENAMES = frozenset({"opencode", "opencode.exe"})
_COPILOT_BASENAMES = frozenset({"copilot", "copilot.exe"})


def _opencode_in_process_tree() -> bool:
    """Best-effort: is an ``opencode`` binary an ancestor of this process?"""
    return _ancestor_in_process_tree(_OPENCODE_BASENAMES)


def _opencode_config_signal() -> bool:
    """Tertiary fallback: does OpenCode's config directory exist?

    When both env vars and the process-tree scan fail (e.g. OpenCode runs
    inside a container, or ``ps`` doesn't list the ancestor), the presence of
    ``~/.config/opencode/`` with real content is a reasonable hint that the
    user is on an OpenCode host. This is intentionally weak — it only fires
    when no Claude plugin env vars are set (those short-circuit earlier in
    detect_runtime) and no other runtime was detected.

    We require the directory to contain at least one file (not just exist),
    so a stale empty dir from an uninstalled OpenCode doesn't trigger it.
    """
    if any(os.environ.get(env_var) for env_var in _CLAUDE_PLUGIN_ENVS):
        return False
    try:
        cfg = opencode_config_home()
        # Go straight to iterdir() — eliminates the is_dir() + iterdir() TOCTOU
        # race. If the dir doesn't exist or disappears between check and use,
        # iterdir() raises and we catch it.
        return next(cfg.iterdir(), None) is not None
    except (OSError, ValueError):
        # OSError: dir doesn't exist, permission denied, or disappeared.
        # ValueError: opencode_config_home returned an invalid path.
        if os.environ.get("TOKEN_OPTIMIZER_DEBUG", "").strip():
            sys.stderr.write(
                "token-optimizer: _opencode_config_signal: "
                f"{opencode_config_home()} not accessible\n"
            )
        return False


def _opencode_signal() -> bool:
    """True when an OpenCode signal is found (env, process tree, or config dir)."""
    return (
        _opencode_env_signal()
        or _opencode_in_process_tree()
        or _opencode_config_signal()
    )


def _copilot_signal() -> bool:
    """True when COPILOT_HOME is set or a ``copilot`` ancestor process is found.

    The Copilot hook bridge always sets TOKEN_OPTIMIZER_RUNTIME=copilot
    explicitly; this signal is the safety net for direct invocations from
    inside a Copilot CLI session (issue #57 class of bugs: never let an
    unrecognized host fall through to the Claude default and write ~/.claude).
    """
    if os.environ.get(_COPILOT_HOME_ENV):
        return True
    return _ancestor_in_process_tree(_COPILOT_BASENAMES)


@functools.lru_cache(maxsize=None)
def detect_runtime() -> str:
    """Return the active runtime name.

    Priority:
      1. Explicit override via TOKEN_OPTIMIZER_RUNTIME
      2. Claude plugin env vars imply Claude Code
      3. CODEX_HOME implies Codex
      4. HERMES_HOME implies Hermes
      5. OpenCode env signal or opencode ancestor process implies OpenCode
      6. COPILOT_HOME or a copilot ancestor process implies Copilot
      7. Default to Claude Code for backward compatibility

    Genuine Claude Code, Codex, and Hermes invocations all short-circuit before
    the OpenCode and Copilot checks (steps 2-4), so adding those detections
    cannot change how the earlier runtimes are detected. The process-tree scans
    in steps 5-6 only run when none of those env signals are present.
    """
    override = os.environ.get(_RUNTIME_OVERRIDE, "").strip().lower()
    if override in _VALID_RUNTIMES:
        return override

    if any(os.environ.get(env_var) for env_var in _CLAUDE_PLUGIN_ENVS):
        return _RUNTIME_CLAUDE

    if os.environ.get(_CODEX_HOME_ENV):
        return _RUNTIME_CODEX

    if os.environ.get(_HERMES_HOME_ENV):
        return _RUNTIME_HERMES

    if _opencode_signal():
        return _RUNTIME_OPENCODE

    if _copilot_signal():
        return _RUNTIME_COPILOT

    return _RUNTIME_CLAUDE


def claude_home() -> Path:
    """Return Claude Code's home directory.

    Honors CLAUDE_CONFIG_DIR — Claude Code's official override for where it
    stores projects/, settings.json, etc. Unlike CODEX_HOME/HERMES_HOME (which
    route through ``_safe_home_from_env`` and are confined under ``$HOME``),
    Claude Code permits CLAUDE_CONFIG_DIR to point ANYWHERE: containers, CI
    runners, a relocated config volume, a non-home ``$HOME``. Confining it to
    ``$HOME`` would silently re-pin those users to a stale ~/.claude — the exact
    failure this is meant to fix. So this resolver accepts any ABSOLUTE,
    EXISTING, NON-SYMLINK directory (keeping the symlink + relative-path
    rejection for traversal safety) and only falls back to ~/.claude when the
    override is unset or unusable.
    """
    fallback = Path.home() / ".claude"
    raw = os.environ.get(_CLAUDE_CONFIG_DIR_ENV, "").strip()
    if not raw:
        return fallback
    candidate = Path(raw).expanduser()
    try:
        if candidate.is_absolute() and candidate.is_dir() and not candidate.is_symlink():
            return candidate.resolve(strict=False)
    except OSError:
        pass
    print(
        f"[Token Optimizer] Warning: {_CLAUDE_CONFIG_DIR_ENV}={raw!r} rejected "
        "(not an absolute, existing, non-symlink directory). Using default.",
        file=sys.stderr,
    )
    return fallback


def codex_home() -> Path:
    """Return Codex's home directory, safely honoring CODEX_HOME when valid."""
    return _safe_home_from_env(_CODEX_HOME_ENV, Path.home() / ".codex")


def hermes_home() -> Path:
    """Return Hermes's home directory, safely honoring HERMES_HOME when valid."""
    return _safe_home_from_env(_HERMES_HOME_ENV, Path.home() / ".hermes")


def copilot_home() -> Path:
    """Return GitHub Copilot CLI's home directory (~/.copilot by default).

    Honors COPILOT_HOME when it points at a safe directory under the user
    home. This is where Token Optimizer's own Copilot data lives
    (~/.copilot/token-optimizer/) — never ~/.claude.
    """
    return _safe_home_from_env(_COPILOT_HOME_ENV, Path.home() / ".copilot")


def _xdg_base(env_var: str, default_rel: str) -> Path:
    """Resolve an XDG base dir, falling back to ~/<default_rel>.

    Honors an absolute XDG_* override; otherwise uses the home-relative default.
    """
    raw = os.environ.get(env_var, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / default_rel


def opencode_config_home() -> Path:
    """Return OpenCode's config directory (~/.config/opencode by default).

    Honors OPENCODE_CONFIG_DIR when it points at a safe directory under home,
    else XDG_CONFIG_HOME/opencode, else ~/.config/opencode.
    """
    default = _xdg_base("XDG_CONFIG_HOME", ".config") / "opencode"
    return _safe_home_from_env("OPENCODE_CONFIG_DIR", default)


def opencode_data_home() -> Path:
    """Return OpenCode's data directory (~/.local/share/opencode by default).

    Honors OPENCODE_DATA_DIR when it points at a safe directory under home,
    else XDG_DATA_HOME/opencode, else ~/.local/share/opencode. This is where
    Token Optimizer's own data would live under OpenCode — never ~/.claude.
    """
    default = _xdg_base("XDG_DATA_HOME", ".local/share") / "opencode"
    return _safe_home_from_env("OPENCODE_DATA_DIR", default)


def runtime_home() -> Path:
    """Return the home directory used by the active runtime."""
    runtime = detect_runtime()

    if runtime == _RUNTIME_CODEX:
        return codex_home()

    if runtime == _RUNTIME_HERMES:
        return hermes_home()

    if runtime == _RUNTIME_OPENCODE:
        return opencode_data_home()

    if runtime == _RUNTIME_COPILOT:
        return copilot_home()

    return claude_home()


def plugin_data_env_vars() -> tuple[str, ...]:
    """Return plugin-data env vars in runtime-specific priority order."""
    if detect_runtime() in (_RUNTIME_CODEX, _RUNTIME_HERMES, _RUNTIME_OPENCODE, _RUNTIME_COPILOT):
        return ("TOKEN_OPTIMIZER_PLUGIN_DATA",)
    return ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")


def runtime_name_for_humans() -> str:
    """Return a display label for logs and user-facing output."""
    runtime = detect_runtime()
    if runtime == _RUNTIME_CODEX:
        return "Codex"
    if runtime == _RUNTIME_HERMES:
        return "Hermes"
    if runtime == _RUNTIME_OPENCODE:
        return "OpenCode"
    if runtime == _RUNTIME_COPILOT:
        return "GitHub Copilot"
    return "Claude Code"
