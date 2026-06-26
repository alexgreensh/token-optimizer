"""Runtime home detection shared by Claude Code, Codex, Hermes, OpenCode, and Copilot adapters.

This module keeps runtime integration deliberately simple:

- Claude Code stays the default runtime unless another runtime is clearly indicated.
- Codex activates when CODEX_HOME is set or TOKEN_OPTIMIZER_RUNTIME=codex.
- Hermes activates when HERMES_HOME is set or TOKEN_OPTIMIZER_RUNTIME=hermes.
- OpenCode activates when an OPENCODE_* env signal or an opencode ancestor process
  is detected, or TOKEN_OPTIMIZER_RUNTIME=opencode. The ancestor scan reads full
  command lines, so OpenCode launched through node/bun (its real launch shape) is
  recognized, not only a bare ``opencode`` binary. OpenCode loads ~/.claude/skills
  by default, so this skill can be invoked from inside OpenCode; detecting it keeps
  the skill from scanning/mutating ~/.claude when the user is actually in OpenCode
  (issue #57). The ancestor signal is evaluated ahead of the Claude plugin-env
  heuristic so a coexisting Claude install on the same host can't shadow it.
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
import re
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

    Never raises and never blocks for long: disabled on Windows, behind a short
    timeout, and skippable via TOKEN_OPTIMIZER_NO_PROC_SCAN.
    """
    if os.environ.get(_PROC_SCAN_DISABLE_ENV, "").strip():
        return False
    if sys.platform.startswith("win"):
        return False
    try:
        import subprocess

        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        if proc.returncode != 0:
            return False
        parents: dict[int, int] = {}
        names: dict[int, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            parents[pid] = ppid
            names[pid] = parts[2]
        pid = os.getpid()
        seen: set[int] = set()
        depth = 0
        while pid and pid > 1 and pid not in seen and depth < 40:
            seen.add(pid)
            depth += 1
            # Exact basename match, not a substring: an unrelated binary like
            # "my-opencode-helper" or a repo dir named "opencode" in argv must
            # not flip a genuine Claude Code session into another runtime's
            # mode. The real CLIs run under their bare binary name (or
            # name.exe on Windows).
            comm = os.path.basename(names.get(pid, "")).lower()
            if comm in basenames:
                return True
            pid = parents.get(pid, 0)
        return False
    except Exception:
        return False


_OPENCODE_BASENAMES = frozenset({"opencode", "opencode.exe"})
_COPILOT_BASENAMES = frozenset({"copilot", "copilot.exe"})

# Bare ``opencode`` binary names — trusted ONLY in executable position (argv[0]).
# A bare "opencode" as a later argument is ambiguous (it could be a directory a
# Claude user passed as `--dir /home/me/opencode`), so it never matches there.
_OPENCODE_EXE_BASENAMES = frozenset({"opencode", "opencode.exe"})
# JS/TS runtimes OpenCode can be launched through. When an ancestor's executable
# is one of these, OpenCode's own basename ("opencode") is NOT the ancestor
# basename — the launcher is ("node"/"bun"/…). So a basename-only scan misses it
# (issue #57). We then inspect the launcher's arguments for an OpenCode entry.
_JS_LAUNCHERS = frozenset(
    {"node", "nodejs", "bun", "deno", "node.exe", "bun.exe", "deno.exe"}
)
# Named OpenCode entry SCRIPTS (carry a file extension). Unlike a bare
# ``opencode`` token, these are unambiguous as a launcher argument, so they are
# safe to match anywhere in the arg list.
_OPENCODE_SCRIPT_BASENAMES = frozenset(
    {"opencode.js", "opencode.mjs", "opencode.cjs", "opencode.ts"}
)
# OpenCode's npm package name (the dominant install shape:
# .../node_modules/opencode-ai/dist/index.js, incl. pnpm's `opencode-ai@x.y.z`).
_OPENCODE_PKG = "opencode-ai"
# Parent directories an installed package legitimately lives under. Requiring one
# of these stops a user's own project dir named `opencode-ai` from matching.
_OPENCODE_PKG_PARENTS = frozenset({"node_modules", ".pnpm"})
_PATH_SPLIT = re.compile(r"[\\/]+")


def _looks_like_opencode_entrypoint(path_token: str) -> bool:
    """True when an argument token is recognizably OpenCode's entry script.

    Deliberately tight (issue #57, KTD-2): we match the *entry script* or the
    *installed package*, never a bare occurrence of the word "opencode" anywhere
    in the command line. A Claude Code user whose project is named ``opencode``
    — even one with a stock ``index.js`` — must NOT be flipped into OpenCode
    mode, so a generic entry filename under an ``opencode`` directory is NOT a
    match. The trade-off: running OpenCode from an *uninstalled* source checkout
    (e.g. ``bun /opt/opencode/src/index.ts``) is not auto-detected; such dev runs
    set TOKEN_OPTIMIZER_RUNTIME=opencode explicitly. Installed users (npm package
    or the ``opencode`` binary) are always detected.
    """
    p = path_token.strip().strip('"').strip("'")
    if not p:
        return False
    base = os.path.basename(p).lower()
    # The script itself is a named OpenCode entry (opencode.mjs, opencode.js, …).
    if base in _OPENCODE_SCRIPT_BASENAMES:
        return True
    # The npm package directory — but only when it sits under node_modules / a
    # pnpm store, which is how every real install looks. Requiring that parent
    # rejects a user's own project directory that merely happens to be named
    # `opencode-ai` (KTD-2 false-positive guard).
    segs = [seg.lower() for seg in _PATH_SPLIT.split(p) if seg]
    for i, s in enumerate(segs):
        is_pkg = s == _OPENCODE_PKG or s.startswith(_OPENCODE_PKG + "@")
        if is_pkg and i > 0 and segs[i - 1] in _OPENCODE_PKG_PARENTS:
            return True
    return False


def _is_opencode_command(args: str) -> bool:
    """True when a process command line is an OpenCode invocation.

    ``args`` is a full command line (``ps -o args``). Matches either the bare
    ``opencode`` binary in executable position, or a JS-runtime launcher with an
    OpenCode entry script / package among ITS arguments. The launcher arg scan
    checks every token (not just the first non-flag one) so value-taking flags
    (``node --require x …``) and launcher subcommands (``bun run …``) don't hide
    the real entry script. The entry check is strict enough that flags, flag
    values, and bare directory arguments never match.
    """
    tokens = args.split()
    if not tokens:
        return False
    exe_base = os.path.basename(tokens[0].strip('"').strip("'")).lower()
    if exe_base in _OPENCODE_EXE_BASENAMES:
        return True
    if exe_base in _JS_LAUNCHERS:
        for tok in tokens[1:]:
            if _looks_like_opencode_entrypoint(tok):
                return True
    return False


def _opencode_in_process_tree() -> bool:
    """Best-effort: is OpenCode an ancestor of this process?

    Scans the parent chain using full command lines (``ps -o args``) so that
    OpenCode launched through ``node``/``bun`` is recognized, not only a bare
    ``opencode`` binary (issue #57). Same safety envelope as
    ``_ancestor_in_process_tree``: disabled on Windows, behind a short timeout,
    skippable via TOKEN_OPTIMIZER_NO_PROC_SCAN, and never raises.
    """
    if os.environ.get(_PROC_SCAN_DISABLE_ENV, "").strip():
        return False
    if sys.platform.startswith("win"):
        return False
    try:
        import subprocess

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
            if _is_opencode_command(cmdlines.get(pid, "")):
                return True
            pid = parents.get(pid, 0)
        return False
    except Exception:
        return False


def _opencode_process_signal() -> bool:
    """Definitive OpenCode signal from the live process tree (issue #57).

    Ground truth for "running under OpenCode *right now*" — unlike an env var or
    a marker file, an OpenCode ancestor process can't be left behind by a prior
    session or a merely-installed copy. Evaluated ahead of the soft Claude
    plugin-env heuristic in ``detect_runtime`` (KTD-3).
    """
    return _opencode_in_process_tree()


def _opencode_signal() -> bool:
    """True when an OpenCode env signal or an OpenCode ancestor process is found."""
    return _opencode_env_signal() or _opencode_process_signal()


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
      2. A definitive OpenCode signal — an opencode ancestor process — implies
         OpenCode, evaluated BEFORE the soft Claude plugin-env heuristic so a
         coexisting Claude Code install on the same host can't shadow it (#57)
      3. Claude plugin env vars imply Claude Code
      4. CODEX_HOME implies Codex
      5. HERMES_HOME implies Hermes
      6. An OPENCODE_* env signal implies OpenCode (env-only fallback)
      7. COPILOT_HOME or a copilot ancestor process implies Copilot
      8. Default to Claude Code for backward compatibility

    Why step 2 is ahead of the Claude env check (KTD-3, issue #57): on a host
    with BOTH Claude Code and OpenCode installed, a stray CLAUDE_PLUGIN_* env var
    would otherwise resolve a genuine OpenCode session to Claude and let the
    skill scan/mutate ~/.claude. An opencode ancestor process is ground truth
    for what's actually running, so it wins. It cannot steal a genuine Claude,
    Codex, or Hermes session: those have no opencode ancestor, so the scan
    returns False and resolution falls through unchanged. The weaker OPENCODE_*
    env signal stays at step 6 (after the Claude/Codex/Hermes env checks) so an
    exported OPENCODE_* var alone never overrides a real Claude session.
    """
    override = os.environ.get(_RUNTIME_OVERRIDE, "").strip().lower()
    if override in _VALID_RUNTIMES:
        return override

    if _opencode_process_signal():
        return _RUNTIME_OPENCODE

    if any(os.environ.get(env_var) for env_var in _CLAUDE_PLUGIN_ENVS):
        return _RUNTIME_CLAUDE

    if os.environ.get(_CODEX_HOME_ENV):
        return _RUNTIME_CODEX

    if os.environ.get(_HERMES_HOME_ENV):
        return _RUNTIME_HERMES

    if _opencode_env_signal():
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
