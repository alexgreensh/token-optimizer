#!/usr/bin/env python3
"""Cross-platform hook dispatcher.

Invoked from hooks.json via a small bash launcher that locates a usable
Python 3 interpreter on macOS, Linux, and Windows:

  "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh\" \"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\" <script-relative-path> [args...]"

The launcher handles Windows-specific gotchas (Program Files spaced paths,
Microsoft Store zero-byte stubs in WindowsApps, py launcher fallback) so
this file can assume it's running under a real Python 3.9+.

This dispatcher resolves the target script under CLAUDE_PLUGIN_ROOT,
checks it exists, and runs it with the same interpreter (sys.executable).
On timeout we kill the child (Popen.kill) to avoid leaking a process
holding the trends.db SQLite lock. Always exits 0 so hook failures never
block the user's tool call.

Windows reap note: module_runner.py runs measure.py IN-PROCESS via
runpy.run_module, so the child proc IS measure.py (the trends.db lock
holder), not a grandchild. On Windows we reap with plain proc.kill()
(TerminateProcess of proc.pid only), NOT taskkill /F /T which would walk
the PPID tree and wrongly kill the detached session-end-flush worker
(the one CREATE_BREAKAWAY_FROM_JOB exists to keep alive). The SIGINT/
SIGTERM handler is best effort on Windows: the detached child has no console
and never sees Ctrl+C, Popen.wait() there is a plain WaitForSingleObject that a
Python handler cannot interrupt (the handler runs once the wait returns), and
an external TerminateProcess from the host bypasses Python handlers entirely.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import traceback
from pathlib import Path

# Defense in depth: the launcher script already filters interpreters, but
# if a user's PATH has a stale Python 3.7 that slipped through, bail early
# so later imports don't explode with confusing SyntaxError noise.
if sys.version_info < (3, 9):
    sys.exit(0)


# Module-level handle so the signal handler can reach the active child when
# Claude Code (or any parent) sends SIGTERM/SIGINT to run.py itself. Without
# this, an external kill reaps run.py but orphans the measure.py grandchild,
# which keeps the inherited stdout pipe open and makes the parent hang waiting
# for EOF (the multi-minute stop-hook hang).
_child_proc: subprocess.Popen | None = None


def _reap(proc, posix_sig):
    """Reap the child process. Never raises.

    On Windows, the child proc IS measure.py (module_runner.py runs it
    in-process via runpy.run_module), so a plain ``proc.kill()``
    (TerminateProcess of proc.pid only) releases the trends.db lock without
    walking the PPID tree and killing the detached session-end-flush worker
    (the one CREATE_BREAKAWAY_FROM_JOB exists to keep alive).

    On POSIX, the child is started with ``start_new_session=True`` so it leads
    its own process group; killing the group reaps any grandchildren (the
    launcher chain uses ``exec``, so run.py's PID is the one the host tracks).
    Falls back to ``proc.kill()`` when the group is already gone.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            proc.kill()
        except OSError:
            try:
                sys.stderr.write("run.py: nt reap kill failed\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
    elif hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), posix_sig)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                try:
                    sys.stderr.write("run.py: posix reap kill failed\n")
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass
    else:
        try:
            proc.kill()
        except OSError:
            try:
                sys.stderr.write("run.py: fallback reap kill failed\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass


def _forward_and_exit(signum, frame):
    """Forward SIGTERM/SIGINT to the child, then exit.

    On Windows this is best effort: Popen.wait() is not interruptible by a
    Python handler, so it runs once the wait returns, and an external
    TerminateProcess from the host bypasses Python handlers entirely.
    """
    global _child_proc
    if _child_proc is not None:
        _reap(_child_proc, signal.SIGTERM)
    os._exit(0)


# Diagnostics log for unexpected errors in the consent gate. Mirrors the
# pattern in the hook runners: write to SNAPSHOT_DIR (never stderr, which
# the host captures into the model's session context). Capped at 256 KB.
_DIAGNOSTICS_LOG_NAME = "consent_diagnostics.log"
_DIAGNOSTICS_LOG_CAP = 256 * 1024


def _consent_diagnostics_log_path():
    """Resolve the diagnostics log path under measure.SNAPSHOT_DIR, or None."""
    try:
        import measure
        base = getattr(measure, "SNAPSHOT_DIR", None)
        if base is None:
            return None
        from pathlib import Path as _P
        return _P(base) / _DIAGNOSTICS_LOG_NAME
    except Exception:
        return None


def _consent_log_diagnostics(message):
    """Append a diagnostics chunk to the consent diagnostics log, capped."""
    path = _consent_diagnostics_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(message)
        if path.stat().st_size > _DIAGNOSTICS_LOG_CAP:
            data = path.read_bytes()[-_DIAGNOSTICS_LOG_CAP:]
            path.write_bytes(data)
    except OSError:
        pass


def _check_consent(plugin_root: Path | None = None) -> bool:
    """Return True if consent is given or assumed. Fail-open on any error."""
    try:
        home = Path.home()

        # Resolve config path from env (set by Claude Code before hook invocation)
        plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        config_path = None
        if plugin_data:
            pd = Path(plugin_data).resolve()
            if not str(pd).startswith(str(home)):
                return True  # Path outside home = skip (fail-open)
            # In-home CLAUDE_PLUGIN_DATA must still be a declared identity of
            # THIS plugin, not a foreign plugin's leaked value sharing the same
            # shared plugins/data root. Reuse the
            # SAME identity-checked resolver plugin_env.py uses for every other
            # hook script instead of trusting pd raw. If the resolver truly
            # can't be imported (unusual/non-standard plugin layout), fall back
            # to trusting pd -- matching prior behavior -- so a missing
            # import never blocks the consent check. If it CAN be imported but
            # rejects pd as foreign, fall through to the identical legacy chain
            # a session with no CLAUDE_PLUGIN_DATA at all would use below --
            # never re-trust the raw (possibly leaked) value.
            resolver_ran = False
            identity_checked = None
            if plugin_root is not None:
                try:
                    scripts_dir = plugin_root / "skills" / "token-optimizer" / "scripts"
                    scripts_str = str(scripts_dir)
                    if scripts_str not in sys.path:
                        sys.path.insert(0, scripts_str)
                    from plugin_env import resolve_claude_plugin_data_env
                    resolver_ran = True
                    identity_checked = resolve_claude_plugin_data_env()
                except Exception:
                    resolver_ran = False
            if identity_checked is not None:
                config_path = identity_checked / "config" / "config.json"
            elif not resolver_ran:
                config_path = pd / "config" / "config.json"
            # else: resolver ran and rejected pd as a foreign identity --
            # config_path stays None and falls through to the legacy chain.

        if config_path is None:
            # Legacy / Codex fallback
            codex_home = os.environ.get("CODEX_HOME", "")
            if codex_home:
                ch = Path(codex_home).resolve()
                if not str(ch).startswith(str(home)):
                    return True
                config_path = ch / "token-optimizer" / "config.json"
            else:
                # Honor CLAUDE_CONFIG_DIR (Claude Code's official config-dir
                # override) before falling back to ~/.claude. Mirrors
                # runtime_env.claude_home(): accept any absolute, existing,
                # non-symlink directory (CLAUDE_CONFIG_DIR may legitimately live
                # OUTSIDE $HOME — containers, CI), reject relative/symlink, else
                # fall back. The previous str.startswith($HOME) check both
                # excluded valid out-of-home dirs and sibling-prefix-matched
                # (/Users/alex-evil passing for /Users/alex).
                claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
                cc = None
                if claude_config:
                    candidate = Path(claude_config).expanduser()
                    try:
                        if candidate.is_absolute() and candidate.is_dir() and not candidate.is_symlink():
                            cc = candidate.resolve()
                    except OSError:
                        cc = None
                if cc is not None:
                    config_path = cc / "token-optimizer" / "config.json"
                else:
                    config_path = home / ".claude" / "token-optimizer" / "config.json"

        if not config_path.exists() or config_path.is_symlink():
            return True  # No config or symlink = fail-open

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        if config.get("enterprise_consent_shown"):
            return True

        # Backward compat backfill: existing users who saw v5 welcome have
        # implicitly consented -- but ONLY when enterprise_consent_shown was
        # never written. A present-and-False enterprise_consent_shown is an
        # explicit opt-out (`consent --reset`); backfilling over it would
        # silently re-enable a user who opted out.
        if config.get("v5_welcome_shown") and "enterprise_consent_shown" not in config:
            config["enterprise_consent_shown"] = True
            # Atomic write (tempfile + os.replace)
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(config, tf, indent=2)
                os.replace(tmp, str(config_path))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return True

        # Flags ABSENT is the SessionStart race window (v5.11.93 silent
        # no-op), NOT an opt-out: config.json can be created by a non-consent
        # writer (ensure-health's last_hook_heal_check, a v5 feature toggle)
        # before the consent bootstrap writes the flags. Fail OPEN so
        # non-exempt hooks -- the PreToolUse Bash compression rewrite
        # included -- keep working in that window; the exempt bootstrap
        # (ensure-health/consent/v5) writes the flags shortly after. A
        # consent key PRESENT and False is a genuine explicit opt-out
        # (`measure.py consent --reset`) and stays gated.
        if "enterprise_consent_shown" not in config and "v5_welcome_shown" not in config:
            return True

        return False  # Explicit opt-out (a consent key was written False)
    except (OSError, json.JSONDecodeError):
        return True  # Fail-open: expected (missing/corrupt config)
    except Exception:
        # Unexpected error — make corruption visible without blocking.
        # Log the traceback to the diagnostics file (never stderr, which
        # the host captures into the model's session context), then
        # fail-open so behavior is unchanged.
        import io
        try:
            buf = io.StringIO()
            buf.write("[Token Optimizer] _check_consent unexpected error, failing open\n")
            traceback.print_exc(file=buf)
            _consent_log_diagnostics(buf.getvalue())
        except Exception:
            pass
        return True  # Fail-open: never block on errors


def _windows_stdio_kwargs():
    """Return the standard handles for a detached (no console) child.

    A stream with a real OS handle is passed through. When at least one is
    usable, every unusable one gets ``subprocess.DEVNULL``: CPython otherwise
    falls back to ``GetStdHandle`` for a stream left out, and a stale non-NULL
    value there makes ``DuplicateHandle`` fail (WinError 6), so ``Popen`` raises
    and the hook never runs. With none usable nothing is passed (CPython then
    sets no STARTF_USESTDHANDLES and the child has no std handles at all).
    """
    kwargs = {}
    missing = []
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            missing.append(name)
            continue
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            missing.append(name)
            continue
        kwargs[name] = stream
    if kwargs:
        for name in missing:
            kwargs[name] = subprocess.DEVNULL
    return kwargs


def _claude_settings_path() -> Path | None:
    """Resolve the host's settings.json path, honoring CLAUDE_CONFIG_DIR.

    Mirrors the CLAUDE_CONFIG_DIR handling in the enterprise-consent resolver
    above and in runtime_env.claude_home(): accept any absolute, existing,
    non-symlink directory (CLAUDE_CONFIG_DIR may legitimately live OUTSIDE
    $HOME -- containers, CI runners, relocated config volumes), reject
    relative/symlink, else fall back to ~/.claude. Without this the disable
    self-check read the wrong file for every CLAUDE_CONFIG_DIR user and the
    feature silently no-opped for exactly the population the repo otherwise
    supports (test_claude_config_dir, test_host_safety_guard, etc.).
    Returns None when no usable settings.json exists.
    """
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = None
    if claude_config:
        candidate = Path(claude_config).expanduser()
        try:
            if candidate.is_absolute() and candidate.is_dir() and not candidate.is_symlink():
                base = candidate.resolve()
        except OSError:
            base = None
    if base is None:
        base = Path.home() / ".claude"
    settings_path = base / "settings.json"
    if not settings_path.is_file() or settings_path.is_symlink():
        return None
    return settings_path


def _plugin_disabled_by_host() -> bool:
    """Return True if the host explicitly turned this plugin off via the host
    settings.json's enabledPlugins map, so main() can no-op before spawning the
    dispatch subprocess at all.

    Claude Code's plugin loader does not appear to reliably stop invoking an
    already-registered plugin's hooks.json commands for existing sessions
    after enabledPlugins[<name>@<marketplace>] is flipped to false (observed:
    8/8 sessions started after such an edit still ran hooks and printed
    "[Token Optimizer]" output). This is a defensive self-check so the plugin
    honors its own disable flag even when the host does not enforce it,
    instead of silently continuing to spend tokens on every hook event.

    The settings path is resolved via _claude_settings_path(), which honors
    CLAUDE_CONFIG_DIR (the consent resolver and runtime_env.claude_home() both
    do); hardcoding ~/.claude/settings.json left every CLAUDE_CONFIG_DIR user
    reading the wrong file and never getting the disable honored.

    Fail-open (return False) on any error -- a missing/unreadable settings
    file, or a plugin/marketplace name we can't resolve, must never silently
    disable the plugin for users who never touched this setting.
    """
    try:
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
        if not plugin_root:
            return False
        meta_dir = Path(plugin_root) / ".claude-plugin"
        plugin_json = meta_dir / "plugin.json"
        marketplace_json = meta_dir / "marketplace.json"
        if not plugin_json.is_file() or not marketplace_json.is_file():
            return False
        # Cap reads: this runs on every hook event (a fresh process each time, so
        # nothing can be cached across events). settings.json is user-controlled and
        # could be pathologically large; an oversized config reads as "can't tell" ->
        # fail-open (plugin treated as enabled), the safe default.
        _CFG_MAX = 4_000_000
        for _cfg in (plugin_json, marketplace_json):
            if _cfg.stat().st_size > _CFG_MAX:
                return False
        plugin_name = json.loads(plugin_json.read_text(encoding="utf-8")).get("name", "").strip()
        marketplace_name = json.loads(marketplace_json.read_text(encoding="utf-8")).get("name", "").strip()
        if not plugin_name or not marketplace_name:
            return False
        settings_path = _claude_settings_path()
        if settings_path is None:
            return False
        if settings_path.stat().st_size > _CFG_MAX:
            return False
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        enabled_plugins = settings.get("enabledPlugins")
        if not isinstance(enabled_plugins, dict):
            return False
        key = f"{plugin_name}@{marketplace_name}"
        return enabled_plugins.get(key) is False
    except Exception:
        return False


_COWORK_PLUGIN_NAME = "token-optimizer-cowork"
_STANDARD_PLUGIN_NAME = "token-optimizer"
# Marker present in every hook command a script install writes into settings.json.
_SCRIPT_INSTALL_HOOK_MARKER = "skills/token-optimizer/scripts/"


def _cowork_copy_should_stand_down() -> bool:
    """True when this is the Cowork build running on a desktop host that already
    has the standard Token Optimizer installed.

    Account-synced plugins land in desktop Claude Code as well as Cowork, so a
    user with both the standard plugin and the Cowork build would otherwise get
    every hook twice: duplicate context injected on each prompt, duplicate
    archives, duplicate nudges. The Cowork build exists for Cowork, so on a
    desktop host it defers to the standard install.

    Desktop vs Cowork is decided only by the Cowork host environment
    (CLAUDE_CODE_REMOTE / CLAUDE_CODE_CONTAINER_ID), never by the plugin's
    install path: a synced plugin sits under /plugins/synced/ on desktop too.

    Fail-open (return False) on any error, so the Cowork build keeps running
    whenever we cannot tell.
    """
    try:
        if os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() in ("1", "true", "yes", "on"):
            return False
        if os.environ.get("CLAUDE_CODE_CONTAINER_ID", "").strip():
            return False
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
        if not plugin_root:
            return False
        plugin_json = Path(plugin_root) / ".claude-plugin" / "plugin.json"
        if not plugin_json.is_file() or plugin_json.stat().st_size > 4_000_000:
            return False
        name = json.loads(plugin_json.read_text(encoding="utf-8")).get("name", "")
        if not isinstance(name, str) or name.strip() != _COWORK_PLUGIN_NAME:
            return False
        settings_path = _claude_settings_path()
        if settings_path is None or settings_path.stat().st_size > 4_000_000:
            return False
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            return False
        enabled = settings.get("enabledPlugins")
        if isinstance(enabled, dict):
            for key, value in enabled.items():
                if value is True and isinstance(key, str) and key.split("@", 1)[0] == _STANDARD_PLUGIN_NAME:
                    return True
        hooks = settings.get("hooks")
        if hooks and _SCRIPT_INSTALL_HOOK_MARKER in json.dumps(hooks).replace("\\\\", "/"):
            return True
        return False
    except Exception:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    if _plugin_disabled_by_host():
        return 0
    if _cowork_copy_should_stand_down():
        return 0

    script_rel = sys.argv[1]
    script_args = sys.argv[2:]

    # Whole-UserPromptSubmit-path opt-out. Checked BEFORE the
    # module_runner command is built (no stdin read, no imports, no fs access)
    # so a user who sets TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT=0 pays zero
    # per-prompt cost. The consolidated dispatcher lives at this relative path;
    # the gate is exact-target so it never silences any other hook script.
    if script_rel == "hooks/userpromptsubmit_runner.py":
        if os.environ.get("TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT", "").strip() == "0":
            return 0

    rel_path = Path(script_rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        root_path = Path(plugin_root)
    else:
        # Fallback: relative to this wrapper's parent directory.
        root_path = Path(__file__).resolve().parent.parent

    try:
        root_resolved = root_path.resolve(strict=True)
        candidate = root_resolved / rel_path
        if not candidate.is_file():
            return 0
        script_path = candidate.resolve(strict=True)
        if not script_path.is_relative_to(root_resolved):
            return 0
    except (OSError, ValueError):
        return 0

    # Use the interpreter that ran this wrapper so we inherit the correct
    # Python across macOS/Linux/Windows without relying on PATH.
    #
    # Dispatch through module_runner.py rather than running script_path
    # directly: CPython never caches __pycache__ bytecode for a script run as
    # __main__, only for imported modules, so a direct `python script_path`
    # recompiles the target from source on every single hook invocation. For
    # measure.py (35k+ lines) that is ~0.3s of pure parse/compile paid on
    # nearly every tool call. module_runner.py runs it as a module instead, so
    # the import system's normal bytecode cache applies. See module_runner.py.
    module_runner = Path(__file__).resolve().parent / "module_runner.py"
    cmd = [sys.executable, str(module_runner), str(script_path.parent), script_path.stem, *script_args]

    # Consent gate: skip data collection until acknowledged.
    # EXEMPT: ensure-health and consent commands bootstrap the consent flag itself.
    # Blocking them creates a deadlock (config.json exists without flags -> ensure-health
    # can't run -> flags never written -> plugin permanently inert).
    exempt_commands = {"ensure-health", "consent", "v5"}
    is_exempt = any(arg in exempt_commands for arg in script_args[:2])
    # The consolidated UserPromptSubmit runner is dispatched as
    # `run.py hooks/userpromptsubmit_runner.py` with NO trailing args, so
    # script_args=[] and the exempt_commands check above never matches it. The
    # runner contains the ensure-health bootstrap itself, so it MUST be let
    # through this consent gate even when consent is False; the runner then
    # makes the per-subcommand consent decision internally (ensure-health
    # bootstraps the flags, the other five skip until consent is True). Without
    # this exemption the runner is never dispatched when consent is False ->
    # ensure-health never fires -> v5_welcome_shown / enterprise_consent_shown
    # never written -> consent False FOREVER -> all six subcommands permanently
    # dead. Latent on native Claude Code (SessionStart bootstraps consent
    # out-of-band), FATAL on Cowork (no SessionStart hook).
    # Same reasoning for the consolidated SessionStart dispatcher. Before
    # consolidation the `ensure-health --once-mark` SessionStart entry matched
    # exempt_commands on its literal arg and bootstrapped the consent flags;
    # the runner is dispatched as `run.py hooks/sessionstart_runner.py` with no
    # args, so it must be let through here and makes the per-subcommand consent
    # decision internally (ensure-health bootstraps, the other four skip until
    # consent is True).
    if script_rel in ("hooks/userpromptsubmit_runner.py",
                      "hooks/sessionstart_runner.py"):
        is_exempt = True
    if not is_exempt and not _check_consent(root_resolved):
        return 0

    # Force UTF-8 in every dispatched script regardless of the host locale, so
    # non-ASCII session paths / transcript content (Hebrew, CJK, accented names)
    # never crash a hook with UnicodeDecode/EncodeError. PYTHONUTF8 also makes the
    # child's default open() encoding UTF-8; PYTHONIOENCODING covers its std streams.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    # Hot-path latency: resolve the runtime HERE and hand the
    # answer to the child via TOKEN_OPTIMIZER_RUNTIME, the override tier
    # detect_runtime() already honors (the Copilot hook bridge uses the same
    # mechanism). Without this, runtime detection runs twice per hook event —
    # once in this process (during _check_consent's plugin_env import) and once
    # in the child (during its own plugin_env import) — and each run pays the
    # OpenCode ancestor `ps` scan (~100 ms measured). This process's parent is
    # the long-lived host CLI, so runtime_env's negative-result ancestor cache
    # (keyed by parent pid) makes the scan here near-free after the first hook
    # of a session; the child's parent is this ephemeral process, so its cache
    # can never hit and the export is the only way to spare it. The exported
    # value IS detect_runtime()'s own resolution for this env — the child's
    # process tree adds only this dispatcher and its child, never an
    # opencode/copilot ancestor, so the child would always resolve identically.
    # Fail-open: any error here leaves the child to detect normally.
    try:
        _scripts_dir = root_resolved / "skills" / "token-optimizer" / "scripts"
        if _scripts_dir.is_dir():
            if str(_scripts_dir) not in sys.path:
                sys.path.insert(0, str(_scripts_dir))
            import runtime_env as _runtime_env

            child_env["TOKEN_OPTIMIZER_RUNTIME"] = _runtime_env.detect_runtime()
    except Exception:
        pass

    proc = None
    global _child_proc
    # Install SIGTERM/SIGINT handlers BEFORE spawning the child so an external
    # kill from Claude Code reaps the whole child process group instead of
    # orphaning the grandchild. On Windows this is best effort: Popen.wait()
    # cannot be interrupted by a Python handler (it runs once the wait
    # returns) and an external TerminateProcess from the host bypasses Python
    # handlers entirely.
    signal.signal(signal.SIGTERM, _forward_and_exit)
    signal.signal(signal.SIGINT, _forward_and_exit)
    try:
        # start_new_session=True puts the child in its own process group so a
        # timeout/external kill can reap the whole group (grandchildren included)
        # via os.killpg. On POSIX do NOT add stdout=/stderr=/stdin= here:
        # several hooks inject via stdout and MUST inherit run.py's stdio.
        # On Windows, start_new_session is a no-op; use DETACHED_PROCESS so the
        # child allocates NO console at all. CREATE_NO_WINDOW only hides the
        # console -- it still spawns conhost.exe, which on Windows 11 25H2
        # leaks a kernel token reference per hook run (issue #215). Do NOT add
        # CREATE_NEW_PROCESS_GROUP (inert for reaping here since run.py never
        # sends GenerateConsoleCtrlEvent). A detached child has no console, so
        # it never sees Ctrl+C either, and its stdio MUST arrive via the three
        # explicit handles _windows_stdio_kwargs() adds (CPython passes them
        # via STARTF_USESTDHANDLES) -- without them the child's std handles
        # bind to NULL and every byte a hook writes to stdout would be
        # silently discarded.
        _popen_kwargs = dict(env=child_env)
        if os.name == "nt":
            _flags = getattr(subprocess, "DETACHED_PROCESS", 0)
            if _flags:
                _popen_kwargs["creationflags"] = _flags
            _popen_kwargs.update(_windows_stdio_kwargs())
        else:
            _popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **_popen_kwargs)
        _child_proc = proc
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            # Important: Popen.wait doesn't auto-kill on timeout. Leaving
            # the child alive would leak a process holding the trends.db
            # SQLite lock, starving the next hook invocation. _reap handles
            # the nt/posix split (see _reap docstring).
            try:
                # signal.SIGKILL does not exist on Windows (AttributeError
                # at the call site, before _reap even runs). Use getattr so
                # the nt branch of _reap (which ignores posix_sig) is never
                # blocked by a missing attribute. POSIX has SIGKILL.
                _reap(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                proc.wait(timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
    except (subprocess.SubprocessError, OSError) as exc:
        if proc is not None:
            try:
                proc.kill()
            except (subprocess.SubprocessError, OSError):
                pass
        else:
            # Popen itself failed: the hook did not run. Leave one line in the
            # diagnostics log (never stderr, which the host feeds to the model).
            try:
                _consent_log_diagnostics(
                    f"[Token Optimizer] run.py: Popen failed for {script_rel!r}: "
                    f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}\n"
                )
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
