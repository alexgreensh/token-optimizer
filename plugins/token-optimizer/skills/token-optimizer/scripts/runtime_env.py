"""Runtime home detection shared by Claude Code, Codex, Hermes, OpenCode, Copilot,
Google Antigravity, and Grok Build adapters.

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
- Copilot activates when COPILOT_HOME or TOKEN_OPTIMIZER_COPILOT_HOME is set, a
  `copilot` ancestor process is detected, or TOKEN_OPTIMIZER_RUNTIME=copilot.
  The Copilot hook bridge always sets the explicit override; the other signals
  are a safety net so the skill never scans/mutates ~/.claude while actually
  running under GitHub Copilot. COPILOT_HOME is Copilot's OWN variable — TO
  reads it but never asks users to set it (issue #78); TOKEN_OPTIMIZER_COPILOT_HOME
  is TO's own collision-free override.
- Cursor activates when TOKEN_OPTIMIZER_CURSOR_HOME is set, or when the
  hook-spawned pair CURSOR_PROJECT_DIR + CURSOR_VERSION is present, or
  TOKEN_OPTIMIZER_RUNTIME=cursor. Cursor has no documented CURSOR_HOME of its
  own, so TO exposes only its namespaced TOKEN_OPTIMIZER_CURSOR_HOME override.
  There is deliberately NO ancestor-process scan for Cursor: the CLI binary is
  named `agent` (too generic to scan safely), so detection stays on env signals
  the host exports into every hook subprocess. The cursor_hook_bridge always
  sets the explicit override; the env pair is a weak safety net below the
  CLAUDECODE tier so a Cursor launched from a CC Bash tool still resolves to
  `cursor` only at the weak tier.
- Antigravity activates when TOKEN_OPTIMIZER_ANTIGRAVITY_HOME is set, an `agy`
  ancestor process is detected, or TOKEN_OPTIMIZER_RUNTIME=antigravity. The
  hook bridge always pins the explicit override (same safety-net pattern as
  Copilot); TOKEN_OPTIMIZER_ANTIGRAVITY_HOME sits above the CLAUDECODE tier so a
  genuine Antigravity session launched from a Claude Code Bash tool (which
  inherits CLAUDECODE=1) still resolves to antigravity, and the `agy` ancestor
  signal stays at the weak tier so it never shadows a coexisting Claude install.
  Antigravity IS distinct from Gemini CLI (different binary, different hooks
  format), so one home resolver covers only Antigravity, never Gemini CLI.
- Grok Build activates when GROK_HOME or TOKEN_OPTIMIZER_GROK_HOME is set, or
  TOKEN_OPTIMIZER_RUNTIME=grok. GROK_HOME is Grok Build's OWN variable — TO reads
  it but never asks users to set it; TOKEN_OPTIMIZER_GROK_HOME is TO's own
  collision-free override.
- Cowork is NOT a separate runtime: Claude Cowork runs the same Claude Code
  engine inside a cloud/local VM, reads ~/.claude, and uses the Claude model
  ladder. So ``detect_runtime()`` still returns ``"claude"`` inside Cowork.
  ``is_cowork()`` is a REFINEMENT signal exposed alongside it (mirroring how the
  runtime lattice layers signals rather than adding runtimes) — true when the
  Cowork host markers are present: ``CLAUDE_CODE_CONTAINER_ID`` (primary), a
  synced-plugin ``CLAUDE_PLUGIN_ROOT`` under ``/plugins/synced/``, or ``AI_AGENT``
  carrying the harness marker. Callers that need Cowork-specific behaviour read
  ``is_cowork()``; nothing about the existing ``detect_runtime()`` returns changes.
- Callers can keep legacy variable names while resolving to the correct home.

The goal is to let Token Optimizer share one Python core while platform
adapters grow feature-by-feature on top of it.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

_RUNTIME_OVERRIDE = "TOKEN_OPTIMIZER_RUNTIME"
_RUNTIME_CLAUDE = "claude"
_RUNTIME_CODEX = "codex"
_RUNTIME_HERMES = "hermes"
_RUNTIME_OPENCODE = "opencode"
_RUNTIME_COPILOT = "copilot"
_RUNTIME_CURSOR = "cursor"
_RUNTIME_ANTIGRAVITY = "antigravity"
_RUNTIME_GROK = "grok"
_VALID_RUNTIMES = frozenset(
    {
        _RUNTIME_CLAUDE,
        _RUNTIME_CODEX,
        _RUNTIME_HERMES,
        _RUNTIME_OPENCODE,
        _RUNTIME_COPILOT,
        _RUNTIME_CURSOR,
        _RUNTIME_ANTIGRAVITY,
        _RUNTIME_GROK,
    }
)
_CLAUDE_PLUGIN_ENVS = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
# Claude Cowork host markers. Cowork is Claude Code running in a cloud/local VM,
# so these REFINE the claude runtime (via is_cowork()) rather than name a new one.
#   - CLAUDE_CODE_REMOTE=true: the ONLY DOCUMENTED cloud-vs-desktop signal
#     (code.claude.com/docs/en/hooks.md: "$CLAUDE_CODE_REMOTE is set to 'true'
#     in cloud, unset locally"; docs-grounding.md §3). Primary signal.
#   - CLAUDE_CODE_CONTAINER_ID: set inside every Cowork VM (observed, undocumented).
#   - AI_AGENT: the Cowork VM sets the "_harness" variant
#     ("claude-code_2-1-231_harness"); the LOCAL Claude Code CLI sets the
#     "_agent" variant ("claude-code_2-1-229_agent"). So the distinguishing
#     token is "harness", NOT the bare "claude-code" prefix (which both share).
#   - CLAUDE_PLUGIN_ROOT under /plugins/synced/: Cowork plugins arrive via the
#     org admin console account-sync, landing under a synced-plugin path.
_COWORK_REMOTE_ENV = "CLAUDE_CODE_REMOTE"
_COWORK_CONTAINER_ENV = "CLAUDE_CODE_CONTAINER_ID"
_COWORK_AI_AGENT_ENV = "AI_AGENT"
_COWORK_AI_AGENT_MARKERS = ("claude-code", "harness")
_COWORK_SYNCED_PLUGIN_MARKER = "/plugins/synced/"
# Claude Code's own process-env signals. CLAUDECODE is inherited by every
# subprocess Claude Code spawns, so a genuine Codex/OpenCode/Copilot launched
# from a CC Bash tool inherits CLAUDECODE=1. This tier therefore sits BELOW the
# explicit CODEX_HOME/HERMES_HOME/COPILOT_HOME env checks (so a nested-Codex
# session with CODEX_HOME set, or a nested-Copilot session with COPILOT_HOME
# set, still resolves to its own runtime) but ABOVE the weak directory
# heuristics, so a host with CLAUDECODE=1 and a coexisting ~/.codex DIRECTORY
# resolves to claude, not codex (issue #120). Copilot's explicit-env tier is
# _COPILOT_HOME_ENVS below, NOT the ancestor-process signal (which stays at the
# weak tier) -- a process scan cannot run ahead of CLAUDECODE without
# reintroducing the #57 shadowing it was added to prevent.
_CLAUDE_CODE_ENVS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
# Claude Code's official config-dir override. When set, Claude stores
# projects/, settings.json, etc. under this directory instead of ~/.claude.
_CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_CODEX_HOME_ENV = "CODEX_HOME"
_HERMES_HOME_ENV = "HERMES_HOME"
# COPILOT_HOME is GitHub Copilot CLI's OWN config variable (GitHub docs: it
# "replaces the entire ~/.copilot path", and Copilot's session-state/,
# session.db, events.jsonl all live inside it). Token Optimizer must NOT ask
# users to set it: a WSL /mnt value set for TO's benefit is also read by the
# native-Windows Copilot CLI and breaks its own session logging (issue #78).
# So TO exposes its OWN namespaced override and only READS COPILOT_HOME as a
# back-compat location hint (with a guardrail warning for /mnt values).
_COPILOT_HOME_ENV = "COPILOT_HOME"
_TO_COPILOT_HOME_ENV = "TOKEN_OPTIMIZER_COPILOT_HOME"
# Cursor has no documented CURSOR_HOME of its own, so TO exposes only a
# namespaced override (never asks users to set a host var Cursor also reads).
_TO_CURSOR_HOME_ENV = "TOKEN_OPTIMIZER_CURSOR_HOME"
# The two env vars Cursor exports into every hook subprocess. Both must be
# present together to count (either alone is too weak). This is the hook-
# spawned-only weak signal; `agent` is too generic a binary name to scan.
_CURSOR_HOOK_ENVS = ("CURSOR_PROJECT_DIR", "CURSOR_VERSION")
# Copilot's explicit config-dir env vars. Like CODEX_HOME/HERMES_HOME these are
# the host's OWN variables, set by/for a genuine Copilot session. They sit ABOVE
# the CLAUDECODE tier so a Copilot session launched from a CC Bash tool (which
# inherits CLAUDECODE=1) still resolves to copilot, mirroring the Codex/Hermes
# guard. The ancestor-process signal (_copilot_signal's ps scan) stays at the
# weak tier below CLAUDECODE, since a process scan ahead of CLAUDECODE would
# re-introduce the #57 OpenCode-shadowing problem for the copilot path too.
_COPILOT_HOME_ENVS = (_COPILOT_HOME_ENV, _TO_COPILOT_HOME_ENV)
# Google Antigravity CLI/app/IDE home override. Antigravity is a distinct
# product from Gemini CLI and ships its own `agy` binary; its data lives under
# ~/.gemini (antigravity-cli/, antigravity/, antigravity-ide/). This is TO's own
# namespaced override so the adapter never depends on a host-owned variable.
_TO_ANTIGRAVITY_HOME_ENV = "TOKEN_OPTIMIZER_ANTIGRAVITY_HOME"
_AGY_BASENAMES = frozenset({"agy", "agy.exe"})
# GROK_HOME is Grok Build's OWN config variable (docs.x.ai/build/settings/reference:
# it relocates the whole ~/.grok tree). Token Optimizer reads it but never asks
# users to set it; TO exposes its own namespaced override for the same reason as
# Copilot (a WSL /mnt value set for TO's benefit would also break Grok's own
# session logging on native Windows).
_GROK_HOME_ENV = "GROK_HOME"
_TO_GROK_HOME_ENV = "TOKEN_OPTIMIZER_GROK_HOME"
_GROK_HOME_ENVS = (_GROK_HOME_ENV, _TO_GROK_HOME_ENV)
# Windows profile names under /mnt/c/Users that are never a real user home.
_WINDOWS_NONUSER_PROFILES = frozenset(
    {"public", "all users", "default", "default user", "windows", "wpsystem"}
)
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

# Warnings printed at most once per process. copilot_home()/_safe_home_from_env
# can be called several times in a single command (doctor, install, hook fire),
# and repeating the same warning reads as separate faults (issue #78,
# assafbem's report). Dedup by exact message text.
#
# The registry lives on ``sys`` — a guaranteed process-singleton — rather than a
# module global, because on Windows this module can be imported under two
# distinct identities (measure.py inserts scripts/ onto sys.path, and case- or
# separator-normalized path variants resolve to separate module objects). Two
# module objects mean two module-level sets, so a module global deduped the
# warning per-copy and it still printed twice. Anchoring the set on ``sys``
# makes every copy share one registry. (assafbem, native-Windows, #78.)
_WARN_REGISTRY_ATTR = "_token_optimizer_warned_messages"


def _warned_registry() -> set:
    """Return the process-singleton set of already-emitted warning messages.

    Anchored on ``sys`` so every imported copy of this module (see the Windows
    dual-identity note above) resolves to the same set. Also the reset/snapshot
    seam tests use to isolate per-call dedup assertions.
    """
    registry = getattr(sys, _WARN_REGISTRY_ATTR, None)
    if registry is None:
        registry = set()
        setattr(sys, _WARN_REGISTRY_ATTR, registry)
    return registry


def __getattr__(name):
    """Lazily expose ``_warned_messages`` as the process-singleton registry.

    Callers/tests reference ``runtime_env._warned_messages`` to snapshot/reset
    warning dedup. Resolving it through the module ``__getattr__`` (PEP 562)
    returns the same set ``_warn_once`` mutates, while keeping import
    side-effect-free: the ``sys`` registry is created on first warning or first
    access, never merely by importing this module (strict POSIX invariance).
    """
    if name == "_warned_messages":
        return _warned_registry()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _warn_once(msg: str) -> None:
    """Print ``msg`` to stderr the first time it is seen this process."""
    registry = _warned_registry()
    if msg in registry:
        return
    registry.add(msg)
    print(msg, file=sys.stderr)


def _safe_home() -> Path:
    """``Path.home()`` that never raises.

    ``Path.home()`` raises RuntimeError when it cannot resolve a home directory
    -- a Windows subprocess launched with no USERPROFILE, a stripped
    container/CI env. A hook must DEGRADE, not crash, so fall back to the
    platform home env vars and finally a temp dir. Real Claude Code sessions
    always have a home; this only bites stripped envs, where any writable dir
    keeps the hook alive instead of raising through claude_home()/runtime_home().
    """
    try:
        return Path.home()
    except (RuntimeError, OSError):
        for key in ("USERPROFILE", "HOME"):
            val = os.environ.get(key, "").strip()
            if val:
                return Path(val)
        drive = os.environ.get("HOMEDRIVE", "").strip()
        tail = os.environ.get("HOMEPATH", "").strip()
        if drive and tail:
            return Path(drive + tail)
        return Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")


def _home_root() -> Path:
    """Return the resolved user home used for env-path confinement."""
    return _safe_home().resolve(strict=False)


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


# Default-off explicit override: on containerized deploys (Docker/Umbrel)
# the runtime home may be a PARENT of $HOME (e.g. HERMES_HOME=/opt/data with
# HOME=/opt/data/home). This drops ONLY the under-$HOME containment policy;
# every other safety check (absolute, resolve, exists, is_dir, not symlink)
# is still enforced by _is_safe_home_dir_relaxed below.
_UNSAFE_RUNTIME_HOME_OVERRIDE_ENV = "TOKEN_OPTIMIZER_ALLOW_UNSAFE_RUNTIME_HOME"


def _truthy_env(env_var: str) -> bool:
    """Parse a truthy env var (1/true/yes/on, case-insensitive). Mirrors the
    _keepwarm_llm_ping_disabled() parser style in measure.py without importing
    it (keeps runtime_env dependency-light)."""
    val = os.environ.get(env_var)
    return isinstance(val, str) and val.strip().lower() in ("1", "true", "yes", "on")


def _is_safe_home_dir_relaxed(path: Path) -> bool:
    """Like _is_safe_home_dir but DROPS the under-$HOME containment check.

    Keeps every other guard: absolute path, resolve(strict=False), must exist,
    must be a real dir, must NOT be a symlink. Used only when the explicit
    TOKEN_OPTIMIZER_ALLOW_UNSAFE_RUNTIME_HOME override is truthy, for
    containerized deploys where the runtime home is a parent of $HOME.
    """
    try:
        if not path.is_absolute():
            return False
        path.resolve(strict=False)
        if path.exists():
            return path.is_dir() and not path.is_symlink()
        return False
    except (OSError, ValueError):
        return False


def _is_wsl_context() -> bool:
    """True when this process is running inside Windows Subsystem for Linux.

    Reads ``/proc/version`` and ``/proc/sys/kernel/osrelease`` and looks for
    the ``microsoft`` / ``WSL`` markers the WSL kernel emits. Never raises.

    This gates the WSL-root ``/mnt/`` opt-in (issue #78) so native-Linux
    ``/mnt`` mounts stay on the strict safe-home path and behavior there is
    byte-identical to before. Tests monkeypatch this function for
    determinism on non-Linux hosts.
    """
    if not sys.platform.startswith("linux"):
        return False
    for probe in ("/proc/version", "/proc/sys/kernel/osrelease"):
        try:
            with open(probe, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        low = text.lower()
        if "microsoft" in low or "wsl" in low:
            return True
    return False


def _wsl_mnt_safe_home(candidate: Path, *, mnt_root: Path | None = None) -> Path | None:
    """Return ``candidate`` resolved if it passes the WSL ``/mnt/`` opt-in.

    The opt-in (issue #78): a runtime-home env var value that FAILS the
    strict under-``$HOME`` guard is still accepted when ALL of:

      (a) we are running inside WSL (gated on ``/proc`` markers —
          native-Linux ``/mnt`` mounts stay on the strict path),
      (b) the path is absolute (no relative traversal),
      (c) the resolved path is under ``/mnt/`` (the WSL Windows-mount root;
          ``/mnt/wsl/``, ``/mnt/c/``, ``/mnt/d/`` … — not arbitrary
          filesystem locations),
      (d) the path is not a symlink (no traversal tricks),
      (e) the path exists and is a directory.

    ``mnt_root`` is a FUNCTION PARAMETER (not an env var) so it can ONLY be
    set by code calling this directly (tests pass a temp dir since macOS
    can't create ``/mnt/c/...``). Production always uses the real ``/mnt`` —
    there is no env var a user could set to widen the confinement.

    Returns the resolved Path on success, ``None`` on any failure (caller
    falls back to the strict path / default). Never raises.
    """
    if not _is_wsl_context():
        return None
    try:
        if not candidate.is_absolute():
            return None
        resolved = candidate.resolve(strict=False)
        root = Path(mnt_root) if mnt_root is not None else Path("/mnt")
        mnt_root_resolved = root.resolve(strict=False)
        if not resolved.is_relative_to(mnt_root_resolved):
            return None
        # Check the RESOLVED path for existence/dir (not the raw candidate):
        # a path like /mnt/c/sub/../x/.copilot resolves to /mnt/c/x/.copilot,
        # which exists and is under /mnt, but candidate.exists() fails when
        # the intermediate "sub" dir doesn't exist (the OS can't traverse
        # through a missing component).  The symlink check stays on the raw
        # candidate so a final-component symlink is still detected.
        if not resolved.exists():
            return None
        if not resolved.is_dir() or candidate.is_symlink():
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _wsl_root_context() -> bool:
    """True when running as root inside WSL — the issue #78 wrong-home case.

    Under `bash install.sh` launched from a Windows shell, WSL runs as root, so
    ``$HOME=/root`` while the user's real Copilot lives on the Windows profile
    at ``/mnt/c/Users/<you>/.copilot``. Gated on actual WSL detection so a
    native-Linux root session is unaffected. Never raises.
    """
    if not _is_wsl_context():
        return False
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return True
    except OSError:
        pass
    try:
        return _home_root() == Path("/root")
    except (OSError, ValueError):
        return False


def _autodetect_wsl_copilot_home(mnt_root: Path | None = None) -> Path | None:
    """Find the Windows-profile Copilot home from WSL-root (issue #78).

    When running as root under WSL, ``$HOME/.copilot`` is ``/root/.copilot`` —
    an empty dir the native-Windows Copilot CLI never reads. The real home is
    the Windows profile at ``/mnt/c/Users/<you>/.copilot``. Probe for it so the
    user never has to set an env var (and never has to set the collision-prone
    COPILOT_HOME that would break Copilot's own logging).

    Returns the resolved Path when EXACTLY ONE Windows profile carrying a
    ``.copilot`` dir is found. Returns ``None`` when zero (caller falls back to
    the default) or when several are found (ambiguous — the caller warns and
    the user disambiguates with TOKEN_OPTIMIZER_COPILOT_HOME). Never raises.

    ``mnt_root`` is a test-injection parameter (never set in production).
    """
    if not _wsl_root_context():
        return None
    root = Path(mnt_root) if mnt_root is not None else Path("/mnt")
    try:
        uniq: list[Path] = []
        for drive in sorted(root.glob("*")):
            users = drive / "Users"
            if not users.is_dir():
                continue
            for prof in sorted(users.iterdir()):
                if prof.name.lower() in _WINDOWS_NONUSER_PROFILES:
                    continue
                cand = prof / ".copilot"
                try:
                    if cand.is_dir() and not cand.is_symlink():
                        resolved = cand.resolve(strict=False)
                        if resolved not in uniq:
                            uniq.append(resolved)
                except OSError:
                    continue
        if len(uniq) == 1:
            return uniq[0]
        return None
    except (OSError, ValueError):
        return None


def _looks_like_mnt_path(raw: str) -> bool:
    """True when ``raw`` is an absolute WSL /mnt/ path (the #78 footgun value)."""
    try:
        return Path(raw).is_absolute() and raw.replace("\\", "/").startswith("/mnt/")
    except (OSError, ValueError):
        return raw.startswith("/mnt/")


def _warn_mnt_copilot_home(raw: str) -> None:
    """Warn that a /mnt COPILOT_HOME breaks native-Windows Copilot (issue #78).

    GitHub Copilot CLI reads COPILOT_HOME itself; a WSL ``/mnt/...`` value —
    meaningless on native Windows — makes Copilot relocate its own
    session-state/session.db/events.jsonl into a path that doesn't exist, so it
    silently stops logging. Steer the user to unset it and rely on
    auto-detection, or to use Token Optimizer's own TOKEN_OPTIMIZER_COPILOT_HOME.
    """
    if not _looks_like_mnt_path(raw):
        return
    _warn_once(
        f"[Token Optimizer] Warning: COPILOT_HOME={raw!r} is a WSL /mnt path. "
        "GitHub Copilot CLI reads COPILOT_HOME too, and a /mnt value breaks its "
        "own session logging on native Windows. Unset COPILOT_HOME (Token "
        "Optimizer auto-detects your Windows Copilot home), or use "
        "TOKEN_OPTIMIZER_COPILOT_HOME for Token Optimizer only."
    )


def _safe_home_from_env(env_var: str, fallback: Path, *, mnt_root: Path | None = None) -> Path:
    """Resolve a runtime-home env var without letting it escape user home.

    The WSL-root ``/mnt/`` opt-in (issue #78): under WSL only, a value that
    fails the strict under-``$HOME`` guard is still accepted when it points
    at an absolute, existing, non-symlink directory under ``/mnt/`` (the WSL
    Windows-mount root). This is the deliberate cross-filesystem opt-in that
    lets a WSL-root install (where ``$HOME=/root``) point
    ``COPILOT_HOME``/``CODEX_HOME``/``HERMES_HOME``/``OPENCODE_*`` at the
    Windows profile path ``/mnt/c/Users/<you>/.copilot`` (etc.). Native-Linux
    ``/mnt`` mounts stay on the strict path because the ``/mnt`` opt-in is
    gated on actual WSL detection (see ``_is_wsl_context``).

    ``mnt_root`` is a test-injection parameter (never set in production) so
    tests can substitute a temp dir for ``/mnt`` on non-Linux hosts. When the
    ``/mnt`` path is ACCEPTED, no "rejected" warning is printed — the warning
    fires only for genuinely-rejected values.
    """
    raw_val = os.environ.get(env_var, "").strip()
    if not raw_val:
        return fallback
    candidate = Path(raw_val).expanduser()
    if _is_safe_home_dir(candidate):
        return candidate.resolve(strict=False)
    # Strict guard rejected it. Try the WSL /mnt opt-in before warning so a
    # legit WSL-root /mnt/c/Users/<you>/.copilot value is accepted silently.
    mnt_result = _wsl_mnt_safe_home(candidate, mnt_root=mnt_root)
    if mnt_result is not None:
        return mnt_result
    # Override (scoped to HERMES_HOME only): on containerized
    # deploys (Docker/Umbrel) HERMES_HOME may be a PARENT of $HOME. When the
    # explicit override env var is truthy, re-validate with the under-$HOME
    # requirement DROPPED but all other checks kept (absolute, resolve, exists,
    # is_dir, not symlink). Deliberately gated to HERMES_HOME only so the opt-in
    # never relaxes CODEX_HOME/COPILOT_HOME/OPENCODE_* containment — those feed
    # write paths (e.g. codex hooks.json), and least-privilege says don't widen
    # them for a Hermes-only deployment need. Other runtimes get their own scoped
    # opt-in if/when a container issue is actually reported for them.
    if (
        env_var == _HERMES_HOME_ENV
        and _truthy_env(_UNSAFE_RUNTIME_HOME_OVERRIDE_ENV)
        and _is_safe_home_dir_relaxed(candidate)
    ):
        return candidate.resolve(strict=False)
    # Name the most common reason so the fix is obvious (assafbem, #78): a
    # ``/mnt/...`` value is a WSL mount path and is only honored inside WSL; on
    # native Windows it points nowhere, so it is rejected. The /mnt opt-in above
    # already accepted any legitimately-WSL value, so reaching here with a /mnt
    # path means we are not in that context.
    hint = ""
    if _looks_like_mnt_path(raw_val):
        hint = (
            " (a /mnt path is WSL-only; on native Windows use a real path like"
            " C:\\Users\\<you>\\.copilot, or unset it and let auto-detect find it)"
        )
    _warn_once(
        f"[Token Optimizer] Warning: {env_var}={raw_val!r} rejected (not a safe directory). Using default.{hint}"
    )
    return fallback


def _opencode_env_signal() -> bool:
    """True when an OpenCode launch/config env var is present in this process."""
    return any(os.environ.get(var) for var in _OPENCODE_ENV_SIGNALS)


# ---------------------------------------------------------------------------
# Shared process snapshot + negative-result ancestor-scan cache (hot path).
#
# Every hook invocation is a fresh Python process, and detect_runtime() used to
# spawn one `ps` per ancestor scanner on the hot path (~100 ms each measured).
# Two fixes, both behavior-preserving:
#
# 1. ONE `ps -Ao pid=,ppid=,comm=,args=` snapshot per process, shared by every
#    scanner. A process's ancestor chain is fixed for its lifetime, so a single
#    snapshot is exact; the OpenCode and Copilot scans read different columns
#    of the same table.
# 2. A short-TTL disk cache of the OpenCode scan's NEGATIVE result ("no
#    opencode ancestor"), keyed by parent pid + parent process start time
#    (Linux /proc only) + runtime-signal env signature. Only the negative
#    result is cached: a stale entry can then only ever reproduce the
#    pre-#57 claude-tier fallback for at most one TTL window, never flip a
#    genuine Claude/Codex session into another runtime's home. The start-time
#    component prevents PID reuse from suppressing a live OpenCode ancestor:
#    a reused PID has a different incarnation and produces a different key.
# ---------------------------------------------------------------------------

_ANCESTOR_CACHE_TTL_SECONDS = 120
_ANCESTOR_CACHE_VERSION = 1
# Bound reads of the ancestor-scan cache file. The payload this module writes
# is ~120 bytes; anything larger is corrupt or hostile and is ignored.
_MAX_CACHE_BYTES = 4096

# Every env var whose value can change which runtime a hook resolves to. The
# cache key hashes their values so two sessions with different signals never
# share an entry.
_ANCESTOR_CACHE_ENV_VARS = (
    _RUNTIME_OVERRIDE,
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    _CODEX_HOME_ENV,
    _HERMES_HOME_ENV,
    _COPILOT_HOME_ENV,
    _TO_COPILOT_HOME_ENV,
    *_OPENCODE_ENV_SIGNALS,
    _PROC_SCAN_DISABLE_ENV,
)

_PROC_SCAN_SNAPSHOT: dict | None = None


def _load_proc_snapshot() -> dict:
    """One process-table snapshot per process, shared by all ancestor scanners.

    Returns a dict with keys: disabled (bool), parents {pid: ppid},
    comms {pid: comm}, cmdlines {pid: args}. On any ps failure the tables are
    empty, which makes every scanner walk find nothing — the same result the
    previous per-scanner error handling produced.
    """
    global _PROC_SCAN_SNAPSHOT
    if _PROC_SCAN_SNAPSHOT is not None:
        return _PROC_SCAN_SNAPSHOT
    disabled = bool(
        os.environ.get(_PROC_SCAN_DISABLE_ENV, "").strip()
    ) or sys.platform.startswith("win")
    snapshot: dict = {
        "disabled": disabled,
        "parents": {},
        "comms": {},
        "cmdlines": {},
    }
    if not disabled:
        try:
            import subprocess

            proc = subprocess.run(
                ["ps", "-Ao", "pid=,ppid=,comm=,args="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
                # Unreachable on Windows (the disabled guard above returns
                # first), but carried anyway so every spawn in this file states
                # the #107 no-flash intent. 0 on POSIX, so a no-op here.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    parts = line.split(None, 3)
                    if len(parts) < 3:
                        continue
                    try:
                        pid, ppid = int(parts[0]), int(parts[1])
                    except ValueError:
                        continue
                    snapshot["parents"][pid] = ppid
                    # Defunct/kernel rows can carry a comm but no args.
                    snapshot["comms"][pid] = parts[2]
                    if len(parts) > 3:
                        snapshot["cmdlines"][pid] = parts[3]
        except Exception:
            pass
    _PROC_SCAN_SNAPSHOT = snapshot
    return snapshot


def _ancestor_cache_path() -> Path:
    """Runtime-neutral cache location (XDG cache), safe to compute pre-detection."""
    return _xdg_base("XDG_CACHE_HOME", ".cache") / "token-optimizer" / "ancestor-scan.json"


def _parent_starttime() -> str:
    """Best-effort parent process start time (incarnation marker for PID-reuse safety).

    On Linux, reads ``/proc/<ppid>/stat`` field 22 (starttime in clock ticks
    since boot) — a cheap file read with no ``ps`` spawn. On non-Linux the
    start time is unavailable cheaply; returns "" so the cache key falls back
    to ``pid:env_hash`` (the pre-fix behavior, no regression). Never raises.
    """
    if not sys.platform.startswith("linux"):
        return ""
    try:
        ppid = os.getppid()
        data = Path(f"/proc/{ppid}/stat").read_text()
        # comm is in parentheses and can contain spaces; split after the last ')'.
        rparen = data.rfind(")")
        if rparen < 0:
            return ""
        fields = data[rparen + 2:].split()
        # After comm: state ppid pgrp session tty_nr tpgid flags minflt cminflt
        # majflt cmajflt utime stime cutime cstime priority nice num_threads
        # itrealvalue starttime ...  — starttime is field 22, index 19 here.
        if len(fields) > 19:
            return fields[19]
    except (OSError, ValueError, IndexError):
        pass
    return ""


def _ancestor_cache_key() -> str:
    """Key binding the cached negative result to this parent + signal env.

    Includes the parent's process start time (Linux ``/proc`` only) so a
    reused PID with a different incarnation produces a different key and
    cannot suppress a live ancestor scan. On non-Linux the start time is
    unavailable cheaply; the key falls back to ``pid:env_hash``.
    """
    raw = "\n".join(
        f"{name}={os.environ.get(name, '')}" for name in _ANCESTOR_CACHE_ENV_VARS
    )
    start = _parent_starttime()
    return f"{os.getppid()}:{start}:{hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()}"


def _ancestor_negative_cached() -> bool:
    """True when a fresh cache entry says this session had no opencode ancestor."""
    try:
        path = _ancestor_cache_path()
        if not path.is_file() or path.stat().st_size > _MAX_CACHE_BYTES:
            return False
        with path.open("r", encoding="utf-8") as fh:
            entry = json.load(fh)
        if not isinstance(entry, dict):
            return False
        if entry.get("version") != _ANCESTOR_CACHE_VERSION:
            return False
        if entry.get("key") != _ancestor_cache_key():
            return False
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)):
            return False
        return (time.time() - ts) < _ANCESTOR_CACHE_TTL_SECONDS
    except (OSError, ValueError, RecursionError):
        return False


def _store_ancestor_negative() -> None:
    """Persist a negative opencode-ancestor scan result. Best-effort, atomic."""
    tmp = None
    try:
        path = _ancestor_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps({
            "version": _ANCESTOR_CACHE_VERSION,
            "key": _ancestor_cache_key(),
            "ts": time.time(),
        })
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except (OSError, ValueError):
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _ancestor_in_process_tree(basenames: frozenset) -> bool:
    """Best-effort: is one of ``basenames`` an ancestor of this process?

    Used only as a fallback signal when a host CLI runs this skill without
    exporting an identifying env var. Reads the shared one-shot process
    snapshot (``_load_proc_snapshot``) instead of spawning its own ``ps``:
    a process's ancestor chain is fixed for its lifetime, so one snapshot
    per process is exact for every scanner, and the OpenCode and Copilot
    scans no longer pay two separate spawns (hot-path latency).

    Never raises and never blocks for long: disabled on Windows, behind a
    short timeout, and skippable via TOKEN_OPTIMIZER_NO_PROC_SCAN.
    """
    snapshot = _load_proc_snapshot()
    if snapshot["disabled"]:
        return False
    parents = snapshot["parents"]
    comms = snapshot["comms"]
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
        comm = os.path.basename(comms.get(pid, "")).lower()
        if comm in basenames:
            return True
        pid = parents.get(pid, 0)
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
# The "run" subcommand (bun run opencode) is skipped so the npm script name
# after it is recognized. An absolute path to the opencode binary as a launcher
# argument (node /usr/local/bin/opencode) is also matched.
_JS_LAUNCHERS = frozenset(
    {"node", "nodejs", "bun", "deno", "node.exe", "bun.exe", "deno.exe"}
)
_JS_RUN_SUBCOMMANDS = frozenset({"run"})
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

    Deliberately tight (issue #57): we match the *entry script* or the
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
    # `opencode-ai` (false-positive guard).
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
        rest = [t.strip('"').strip("'") for t in tokens[1:]]
        # `bun run opencode` — the script name sits at rest[1] after the `run` subcommand.
        if rest and rest[0].lower() in _JS_RUN_SUBCOMMANDS:
            if len(rest) >= 2 and os.path.basename(rest[1]).lower() in _OPENCODE_EXE_BASENAMES:
                return True
        # `node /abs/path/opencode` — the opencode binary as the IMMEDIATE first argument
        # (rest[0]). Restricting to rest[0] (not a general scan) is what prevents a later
        # flag value like `--dir /home/me/opencode` from false-matching.
        elif rest and os.path.isabs(rest[0]) and os.path.basename(rest[0]).lower() in _OPENCODE_EXE_BASENAMES:
            return True
        # Named entry scripts (opencode.mjs/.js) and the installed npm package
        # (node_modules/opencode-ai) — tight check, safe to scan all tokens.
        for tok in tokens[1:]:
            if _looks_like_opencode_entrypoint(tok):
                return True
    return False


def _opencode_in_process_tree() -> bool:
    """Best-effort: is OpenCode an ancestor of this process?

    Scans the parent chain using full command lines so that OpenCode launched
    through ``node``/``bun`` is recognized, not only a bare ``opencode`` binary
    (issue #57). Reads the shared one-shot process snapshot (one ``ps`` per
    process, shared with the Copilot scanner). A fresh NEGATIVE result is
    persisted to a short-TTL disk cache keyed by parent pid + parent start
    time (Linux /proc only) + signal-env signature, so subsequent hook
    processes in the same session skip the scan entirely; only the negative
    result is cached, so a stale entry can never flip a Claude/Codex session
    into another runtime. On Linux the start-time binding also prevents PID
    reuse from flipping an OpenCode session into claude: a reused PID has a
    different incarnation and produces a different key. On non-Linux the
    start time is unavailable cheaply; a stale entry can flip an OpenCode
    session into claude for at most one TTL window (worst case: the pre-#57
    claude-tier fallback). Same safety envelope as
    ``_ancestor_in_process_tree``: disabled on Windows, behind a short timeout,
    skippable via TOKEN_OPTIMIZER_NO_PROC_SCAN, and never raises.
    """
    if _ancestor_negative_cached():
        return False
    snapshot = _load_proc_snapshot()
    if snapshot["disabled"]:
        return False
    parents = snapshot["parents"]
    cmdlines = snapshot["cmdlines"]
    pid = os.getpid()
    seen: set[int] = set()
    depth = 0
    found = False
    while pid and pid > 1 and pid not in seen and depth < 40:
        seen.add(pid)
        depth += 1
        if _is_opencode_command(cmdlines.get(pid, "")):
            found = True
            break
        pid = parents.get(pid, 0)
    if not found and not snapshot["disabled"]:
        # Cache only the negative outcome (see docstring): a positive finding
        # is re-derived live on every scan so a runtime can never be sticky.
        _store_ancestor_negative()
    return found


def _opencode_process_signal() -> bool:
    """Definitive OpenCode signal from the live process tree (issue #57).

    Ground truth for "running under OpenCode *right now*" — unlike an env var or
    a marker file, an OpenCode ancestor process can't be left behind by a prior
    session or a merely-installed copy. Evaluated ahead of the soft Claude
    plugin-env heuristic in ``detect_runtime``.
    """
    return _opencode_in_process_tree()


def _opencode_config_signal() -> bool:
    """Weak OpenCode signal: a populated ~/.config/opencode directory.

    Tertiary tier (issue #57): catches a real OpenCode install that exports
    neither an OPENCODE_* env var nor an opencode ancestor (e.g. a host CLI
    spawned outside OpenCode's process group). A populated config dir is a
    weak signal — a stale uninstalled copy leaves an empty dir, which does
    NOT trigger. Suppressed by Claude env vars (Claude takes priority) and by
    CODEX_HOME/HERMES_HOME/COPILOT_HOME (those genuine runtimes win over this
    weak tier), and by a real Claude Code home (settings.json or projects/).
    Never raises; OSError/PermissionError -> False.
    """
    if any(os.environ.get(v) for v in _CLAUDE_PLUGIN_ENVS):
        return False
    if (
        os.environ.get(_CODEX_HOME_ENV)
        or os.environ.get(_HERMES_HOME_ENV)
        or os.environ.get(_COPILOT_HOME_ENV)
        or os.environ.get(_TO_COPILOT_HOME_ENV)
        or os.environ.get(_TO_CURSOR_HOME_ENV)
        or (
            os.environ.get(_CURSOR_HOOK_ENVS[0])
            and os.environ.get(_CURSOR_HOOK_ENVS[1])
        )
        or os.environ.get(_TO_ANTIGRAVITY_HOME_ENV)
    ):
        return False
    # A real Claude Code home (settings.json or projects/) means this is a Claude
    # user; the weak config-dir tier must not flip them to opencode. A live OpenCode
    # session is already caught by the ancestor (step 2) / OPENCODE_* env (step 4)
    # tiers, so this only prevents a false stop, never masks a real OpenCode run.
    # Bare ~/.claude existence is NOT used (OpenCode loads ~/.claude/skills).
    ch = claude_home()
    if ch.is_dir() and ((ch / "settings.json").is_file() or (ch / "projects").is_dir()):
        return False
    try:
        d = opencode_config_home()
        if not d.is_dir():
            if os.environ.get("TOKEN_OPTIMIZER_DEBUG"):
                print(f"[_opencode_config_signal] config dir not present: {d}", file=sys.stderr)
            return False
        if not any(d.iterdir()):
            if os.environ.get("TOKEN_OPTIMIZER_DEBUG"):
                print(f"[_opencode_config_signal] config dir empty: {d}", file=sys.stderr)
            return False
        return True
    except OSError as exc:
        if os.environ.get("TOKEN_OPTIMIZER_DEBUG"):
            print(f"[_opencode_config_signal] OSError inspecting config dir: {exc}", file=sys.stderr)
        return False


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
    if os.environ.get(_COPILOT_HOME_ENV) or os.environ.get(_TO_COPILOT_HOME_ENV):
        return True
    return _ancestor_in_process_tree(_COPILOT_BASENAMES)


def _cursor_signal() -> bool:
    """True when both Cursor hook-spawned env vars are present.

    Cursor exports CURSOR_PROJECT_DIR and CURSOR_VERSION into every hook
    subprocess; both must be present together (either alone is too weak).
    There is deliberately no ancestor-process scan: the Cursor CLI binary is
    named ``agent`` — too generic to scan without false positives. This signal
    sits at the weak tier BELOW the CLAUDECODE env check, so a Cursor launched
    from a CC Bash tool (which inherits CLAUDECODE=1) is not stolen here; the
    bridge always pins TOKEN_OPTIMIZER_RUNTIME=cursor anyway.
    """
    return bool(
        os.environ.get(_CURSOR_HOOK_ENVS[0])
        and os.environ.get(_CURSOR_HOOK_ENVS[1])
    )


def _antigravity_signal() -> bool:
    """True when TOKEN_OPTIMIZER_ANTIGRAVITY_HOME is set or an `agy` ancestor runs.

    The Antigravity hook bridge always sets TOKEN_OPTIMIZER_RUNTIME=antigravity
    explicitly; this signal is the safety net for direct invocations from inside
    an Antigravity session (the same #57 class of bugs: never let an
    unrecognized host fall through to the Claude default and write ~/.claude).
    """
    if os.environ.get(_TO_ANTIGRAVITY_HOME_ENV):
        return True
    return _ancestor_in_process_tree(_AGY_BASENAMES)


@functools.lru_cache(maxsize=None)
def detect_runtime() -> str:
    """Return the active runtime name.

    Priority:
      1. Explicit override via TOKEN_OPTIMIZER_RUNTIME
      2. A definitive OpenCode signal — an opencode ancestor process — implies
         OpenCode, evaluated BEFORE the soft Claude plugin-env heuristic so a
         coexisting Claude Code install on the same host can't shadow it (#57)
      3. Claude plugin env vars imply Claude Code
      4. An OPENCODE_* env signal implies OpenCode (medium tier: beats
         Codex/Hermes so a leftover CODEX_HOME can't shadow a genuine
         OpenCode session — "Guy's bug", issue #57; still AFTER Claude env)
      5. CODEX_HOME implies Codex
      6. HERMES_HOME implies Hermes
      7. COPILOT_HOME / TOKEN_OPTIMIZER_COPILOT_HOME implies Copilot (explicit
         config-dir env; sits ABOVE the CLAUDECODE tier so a Copilot session
         launched from a CC Bash tool, which inherits CLAUDECODE=1, still
         resolves to copilot -- the same guard Codex/Hermes get. The
         ancestor-process signal stays at the weak tier below CLAUDECODE.)
      7b. TOKEN_OPTIMIZER_CURSOR_HOME implies Cursor (explicit config-dir env,
          same ABOVE-CLAUDECODE guard as Copilot; Cursor has no documented
          CURSOR_HOME of its own so this is TO's namespaced override only).
      8. Claude Code process env (CLAUDECODE / CLAUDE_CODE_ENTRYPOINT /
         CLAUDE_CODE_SESSION_ID) implies Claude. Sits BELOW CODEX_HOME/
         HERMES_HOME/COPILOT_HOME/TOKEN_OPTIMIZER_CURSOR_HOME so a nested-
         Codex/Hermes/Copilot/Cursor session launched from a CC Bash tool
         (which inherits CLAUDECODE=1) still resolves to its own runtime, but
         ABOVE the directory heuristics so a host with CLAUDECODE=1 and a
         coexisting ~/.codex DIRECTORY resolves to claude, not codex (#120)
      9. A populated opencode config dir implies OpenCode (weak tertiary
         tier; loses to Claude/Codex/Hermes/Copilot/Cursor env, beats default)
      10. COPILOT_HOME or a copilot ancestor process implies Copilot
      11. CURSOR_PROJECT_DIR + CURSOR_VERSION both set implies Cursor (weak
          hook-spawned tier, BELOW CLAUDECODE -- no ancestor scan since the
          Cursor CLI binary `agent` is too generic)
      11b. TOKEN_OPTIMIZER_ANTIGRAVITY_HOME or an `agy` ancestor process
           implies Antigravity (weak tier, below CLAUDECODE)
      12. Default to Claude Code for backward compatibility

    Why step 2 is ahead of the Claude env check (KTD-3, issue #57): on a host
    with BOTH Claude Code and OpenCode installed, a stray CLAUDE_PLUGIN_* env var
    would otherwise resolve a genuine OpenCode session to Claude and let the
    skill scan/mutate ~/.claude. An opencode ancestor process is ground truth
    for what's actually running, so it wins. It cannot steal a genuine Claude,
    Codex, or Hermes session: those have no opencode ancestor, so the scan
    returns False and resolution falls through unchanged. The OPENCODE_* env
    signal (step 4) sits AFTER the Claude env check so an exported OPENCODE_*
    var alone never overrides a real Claude session, but BEFORE Codex/Hermes
    so a leftover CODEX_HOME/HERMES_HOME can't shadow a genuine OpenCode
    session. The config-dir signal (step 7) is the weakest tier — it only
    fires when no Claude/Codex/Hermes/Copilot env is set and no real Claude
    Code home (settings.json or projects/) exists.
    """
    override = os.environ.get(_RUNTIME_OVERRIDE, "").strip().lower()
    if override in _VALID_RUNTIMES:
        return override

    if _opencode_process_signal():
        return _RUNTIME_OPENCODE

    if any(os.environ.get(env_var) for env_var in _CLAUDE_PLUGIN_ENVS):
        return _RUNTIME_CLAUDE

    if _opencode_env_signal():
        return _RUNTIME_OPENCODE

    if os.environ.get(_CODEX_HOME_ENV):
        return _RUNTIME_CODEX

    if os.environ.get(_HERMES_HOME_ENV):
        return _RUNTIME_HERMES

    # Copilot explicit config-dir env: ABOVE the CLAUDECODE tier so a Copilot
    # session launched from a CC Bash tool (inherits CLAUDECODE=1) still
    # resolves to copilot. Mirrors the CODEX_HOME/HERMES_HOME guard.
    if any(os.environ.get(v) for v in _COPILOT_HOME_ENVS):
        return _RUNTIME_COPILOT

    # Cursor explicit config-dir env: ABOVE the CLAUDECODE tier (same guard as
    # Codex/Hermes/Copilot). Cursor has no documented CURSOR_HOME, so this is
    # TO's namespaced override only.
    if os.environ.get(_TO_CURSOR_HOME_ENV):
        return _RUNTIME_CURSOR

    # Antigravity explicit config-dir env: ABOVE the CLAUDECODE tier (same
    # guard Codex/Hermes/Copilot get) so a genuine Antigravity session launched
    # from a Claude Code Bash tool (which inherits CLAUDECODE=1) still resolves
    # to antigravity, never writes ~/.claude.
    if os.environ.get(_TO_ANTIGRAVITY_HOME_ENV):
        return _RUNTIME_ANTIGRAVITY

    # Grok Build explicit config-dir env: same guard as Copilot — a Grok session
    # launched from a CC Bash tool (inherits CLAUDECODE=1) still resolves to grok.
    if any(os.environ.get(v) for v in _GROK_HOME_ENVS):
        return _RUNTIME_GROK

    if any(os.environ.get(v) for v in _CLAUDE_CODE_ENVS):
        return _RUNTIME_CLAUDE

    if _opencode_config_signal():
        return _RUNTIME_OPENCODE

    if _copilot_signal():
        return _RUNTIME_COPILOT

    if _cursor_signal():
        return _RUNTIME_CURSOR

    # `agy` ancestor process: the weak-tier safety net (same as the copilot
    # ancestor signal), below CLAUDECODE so a coexisting Claude install wins.
    if _antigravity_signal():
        return _RUNTIME_ANTIGRAVITY

    return _RUNTIME_CLAUDE


@functools.lru_cache(maxsize=None)
def is_cowork() -> bool:
    """True when running inside Claude Cowork (a Claude Code VM host).

    A REFINEMENT of the claude runtime, not a new runtime: ``detect_runtime()``
    still returns ``"claude"`` inside Cowork. Signals, any of which suffices:

      1. ``CLAUDE_CODE_REMOTE`` is truthy — the ONLY DOCUMENTED cloud signal
         (code.claude.com/docs/en/hooks.md; docs-grounding.md §3). Primary.
      2. ``CLAUDE_CODE_CONTAINER_ID`` is set (observed in every Cowork VM;
         undocumented). Belt-and-suspenders fallback.
      3. ``AI_AGENT`` carries the Claude Code VM harness marker
         (``claude-code`` + ``harness``, e.g. ``claude-code_2-1-231_harness``);
         the local CLI's ``..._agent`` value is deliberately NOT a match.
      4. ``CLAUDE_PLUGIN_ROOT``/``CLAUDE_PLUGIN_DATA`` points under
         ``/plugins/synced/`` — where org-console account-synced plugins land.

    Doc vs observed: only (1) is in the published docs; (2)-(4) are live-observed
    Cowork markers kept as fallback so detection still holds if a future build
    stops exporting CLAUDE_CODE_REMOTE into the hook env (issues #24529/#66557
    show env injection is not guaranteed). Never raises; a missing/blank env just
    contributes no signal.
    """
    if _truthy_env(_COWORK_REMOTE_ENV):
        return True
    if os.environ.get(_COWORK_CONTAINER_ENV, "").strip():
        return True
    ai_agent = os.environ.get(_COWORK_AI_AGENT_ENV, "").lower()
    if all(marker in ai_agent for marker in _COWORK_AI_AGENT_MARKERS):
        return True
    for env_var in _CLAUDE_PLUGIN_ENVS:
        val = os.environ.get(env_var, "")
        if val and _COWORK_SYNCED_PLUGIN_MARKER in val.replace("\\", "/"):
            return True
    return False


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
    fallback = _safe_home() / ".claude"
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
    return _safe_home_from_env(_CODEX_HOME_ENV, _safe_home() / ".codex")


def hermes_home() -> Path:
    """Return Hermes's home directory, safely honoring HERMES_HOME when valid."""
    return _safe_home_from_env(_HERMES_HOME_ENV, _safe_home() / ".hermes")


def copilot_home(*, mnt_root: Path | None = None) -> Path:
    """Return GitHub Copilot CLI's home directory (~/.copilot by default).

    Resolution precedence (issue #78 — COPILOT_HOME is Copilot's OWN variable,
    so TO must not depend on the user setting it):

      1. TOKEN_OPTIMIZER_COPILOT_HOME — Token Optimizer's own override. Honored
         under the strict under-``$HOME`` guard, or the WSL-root ``/mnt/``
         opt-in. This is the ONLY override users should set to disambiguate a
         multi-profile Windows host; it never collides with Copilot's config.
      2. COPILOT_HOME — read for back-compat as a location hint, but a WSL
         ``/mnt/`` value earns a loud guardrail warning because the native
         Windows Copilot CLI reads the same var and a /mnt value breaks its own
         logging.
      3. WSL-root auto-detect — when running as root under WSL (``$HOME=/root``)
         and neither override is set, probe ``/mnt/c/Users/*/.copilot`` and use
         the sole match, so no env var is needed at all.
      4. ``$HOME/.copilot`` — the default.

    This is where Token Optimizer's own Copilot data lives
    (``<home>/token-optimizer/``) — never ~/.claude. ``mnt_root`` is a
    test-injection parameter (never set in production) so tests can substitute a
    temp dir for ``/mnt`` on non-Linux hosts.
    """
    fallback = _safe_home() / ".copilot"

    # 1. TO's own namespaced override wins (no collision with Copilot's config).
    if os.environ.get(_TO_COPILOT_HOME_ENV, "").strip():
        return _safe_home_from_env(_TO_COPILOT_HOME_ENV, fallback, mnt_root=mnt_root)

    # 2. Copilot's own COPILOT_HOME — back-compat location hint, guarded.
    cp_raw = os.environ.get(_COPILOT_HOME_ENV, "").strip()
    if cp_raw:
        _warn_mnt_copilot_home(cp_raw)
        return _safe_home_from_env(_COPILOT_HOME_ENV, fallback, mnt_root=mnt_root)

    # 3. WSL-root: auto-detect the Windows-profile Copilot home (no env var).
    auto = _autodetect_wsl_copilot_home(mnt_root=mnt_root)
    if auto is not None:
        return auto

    # 4. Default.
    return fallback


def antigravity_home() -> Path:
    """Return Google Antigravity's home directory (~/.gemini by default).

    Honors TOKEN_OPTIMIZER_ANTIGRAVITY_HOME under the strict under-``$HOME``
    guard. This is where Antigravity's own data lives (antigravity-cli/,
    antigravity/, antigravity-ide/) AND where Token Optimizer's Antigravity data
    lives (<home>/token-optimizer/) — never ~/.claude.
    """
    return _safe_home_from_env(_TO_ANTIGRAVITY_HOME_ENV, _safe_home() / ".gemini")


def _warn_mnt_grok_home(raw: str) -> None:
    """Warn that a /mnt GROK_HOME breaks native-Windows Grok Build.

    Grok Build reads GROK_HOME itself; a WSL ``/mnt/...`` value — meaningless on
    native Windows — relocates Grok's own sessions/hooks/config into a path that
    doesn't exist, so it silently stops persisting. Steer the user to unset it
    or to use Token Optimizer's own TOKEN_OPTIMIZER_GROK_HOME.
    """
    if not _looks_like_mnt_path(raw):
        return
    _warn_once(
        f"[Token Optimizer] Warning: GROK_HOME={raw!r} is a WSL /mnt path. "
        "Grok Build reads GROK_HOME too, and a /mnt value breaks its own "
        "session persistence on native Windows. Unset GROK_HOME, or use "
        "TOKEN_OPTIMIZER_GROK_HOME for Token Optimizer only."
    )


def grok_home(*, mnt_root: Path | None = None) -> Path:
    """Return Grok Build's home directory (~/.grok by default).

    Resolution precedence:
      1. TOKEN_OPTIMIZER_GROK_HOME — Token Optimizer's own override (strict
         under-``$HOME`` guard, or the WSL-root ``/mnt/`` opt-in). The only var
         users should set for Token Optimizer's benefit.
      2. GROK_HOME — Grok Build's OWN config variable (back-compat location
         hint); a WSL ``/mnt/`` value earns a guardrail warning because the
         native Grok CLI reads the same var and a /mnt value breaks its own
         session persistence.
      3. ``$HOME/.grok`` — the default.

    This is where Token Optimizer's own Grok data lives
    (``<home>/token-optimizer/``) — never ~/.claude. ``mnt_root`` is a
    test-injection parameter (never set in production).
    """
    fallback = _safe_home() / ".grok"

    # 1. TO's own namespaced override wins (no collision with Grok's config).
    if os.environ.get(_TO_GROK_HOME_ENV, "").strip():
        return _safe_home_from_env(_TO_GROK_HOME_ENV, fallback, mnt_root=mnt_root)

    # 2. Grok's own GROK_HOME — back-compat location hint, guarded.
    grok_raw = os.environ.get(_GROK_HOME_ENV, "").strip()
    if grok_raw:
        _warn_mnt_grok_home(grok_raw)
        return _safe_home_from_env(_GROK_HOME_ENV, fallback, mnt_root=mnt_root)

    # 3. Default.
    return fallback


def _xdg_base(env_var: str, default_rel: str) -> Path:
    """Resolve an XDG base dir, falling back to ~/<default_rel>.

    Honors an absolute XDG_* override; otherwise uses the home-relative default.
    """
    raw = os.environ.get(env_var, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
    return _safe_home() / default_rel


def cursor_home() -> Path:
    """Return Cursor's home directory (~/.cursor by default).

    Cursor stores its hooks.json, transcripts and chats under ~/.cursor. Cursor
    has no documented CURSOR_HOME env var of its own, so the only override TO
    exposes is its namespaced TOKEN_OPTIMIZER_CURSOR_HOME, honoured under the
    strict under-``$HOME`` guard (a value outside $HOME falls back to ~/.cursor
    with the once-per-process warning). This is where Token Optimizer's own
    Cursor data lives (``<home>/token-optimizer/``) — never ~/.claude.
    """
    return _safe_home_from_env(_TO_CURSOR_HOME_ENV, _safe_home() / ".cursor")


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

    if runtime == _RUNTIME_CURSOR:
        return cursor_home()

    if runtime == _RUNTIME_ANTIGRAVITY:
        return antigravity_home()

    if runtime == _RUNTIME_GROK:
        return grok_home()

    return claude_home()


def plugin_data_env_vars() -> tuple[str, ...]:
    """Return plugin-data env vars in runtime-specific priority order."""
    if detect_runtime() in (
        _RUNTIME_CODEX,
        _RUNTIME_HERMES,
        _RUNTIME_OPENCODE,
        _RUNTIME_COPILOT,
        _RUNTIME_CURSOR,
        _RUNTIME_ANTIGRAVITY,
        _RUNTIME_GROK,
    ):
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
    if runtime == _RUNTIME_CURSOR:
        return "Cursor"
    if runtime == _RUNTIME_ANTIGRAVITY:
        return "Google Antigravity"
    if runtime == _RUNTIME_GROK:
        return "Grok Build"
    # Cowork is the claude runtime in a VM; label the refinement without changing
    # the runtime it resolves to.
    if runtime == _RUNTIME_CLAUDE and is_cowork():
        return "Claude Code (Cowork)"
    return "Claude Code"
