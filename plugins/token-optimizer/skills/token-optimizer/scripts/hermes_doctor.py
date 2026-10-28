#!/usr/bin/env python3
"""Hermes-specific install readiness checks for Token Optimizer."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime_env import detect_runtime, hermes_home, runtime_home

# Windows: a console-subsystem child (python.exe) flashes a console window
# unless the parent passes CREATE_NO_WINDOW. getattr -> 0 on POSIX,
# where creationflags=0 is an accepted no-op.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_PORT = 24844
_PLUGIN_NAME = "token-optimizer"

# The plugin's __init__.py imports these three from its own directory, so
# they MUST be present for /token-optimizer, the dashboard launcher, and rollups
# to work. Earlier doctor only checked plugin.yaml + __init__.py and reported OK
# on a broken (payload-only) install.
REQUIRED_PLUGIN_FILES = (
    "plugin.yaml",
    "__init__.py",
    "hermes_hook_bridge.py",
    "hermes_state.py",
    "hermes_session.py",
    "runtime_env.py",
    # v5.11.1: the installer always writes the measure-path locator when
    # measure.py exists; its absence means a broken / payload-only install.
    "measure-path",
)

# Hermes config: the plugin must be allow-listed under plugins.enabled.
_CONFIG_NAMES = ("config.yaml", "config.yml")
_ACTIVATION_SNIPPET = "plugins:\n  enabled:\n    - token-optimizer\n"

# Every hook the plugin registers (hermes/__init__.py HOOKS, hermes/plugin.yaml).
# A test pins all three lists together.
EXPECTED_HOOKS = {
    "pre_llm_call",
    "post_api_request",
    "pre_tool_call",
    "post_tool_call",
    "transform_terminal_output",
    "transform_tool_result",
    "pre_auxiliary_call",
    "post_compaction",
    "api_request_error",
    "subagent_start",
    "subagent_stop",
    "on_skill_lifecycle",
    "on_session_start",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "on_session_delete",
}

# Engine scripts the plugin runs from the checkout (found via measure-path).
# They are not copied into the plugin dir, so a checkout missing one silently
# switches that feature off; the doctor names it instead.
REQUIRED_ENGINE_SCRIPTS = (
    "hermes_tool_hooks.py",
    "bash_compress_hook.py",
    "archive_result.py",
    "context_intel.py",
    "read_cache.py",
    "refetch_guard.py",
    "credential_patterns.py",
)

# Copied into the plugin dir by hermes_install; a copy older than the checkout
# means the plugin runs stale code until the installer is re-run.
_COPIED_RUNTIME_MODULES = (
    "hermes_hook_bridge.py",
    "hermes_state.py",
    "hermes_session.py",
    "runtime_env.py",
    "spawn_utils.py",
)

# Loads the INSTALLED plugin in a child process with a stand-in Hermes context
# and prints what register() actually registered. A child keeps the plugin's
# sys.path change and module state out of the doctor.
_REGISTRATION_PROBE = r"""
import importlib.util, json, sys
plugin_dir = sys.argv[1]
spec = importlib.util.spec_from_file_location("to_hermes_doctor_probe", plugin_dir + "/__init__.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Ctx:
    def __init__(self):
        self.hooks, self.commands, self.cli, self.patterns = [], [], [], 0
    def register_hook(self, name, callback, **kw):
        if not callable(callback):
            raise TypeError(name)
        self.hooks.append(name)
    def register_command(self, name, *a, **k):
        self.commands.append(name)
    def register_cli_command(self, name, *a, **k):
        self.cli.append(name)
    def register_redaction_patterns(self, patterns):
        self.patterns = len(list(patterns))
        return self.patterns

ctx = Ctx()
module.register(ctx)
print(json.dumps({"hooks": ctx.hooks, "commands": ctx.commands, "cli": ctx.cli,
                  "patterns": ctx.patterns}))
"""

SKILL_INSTALL_FILES = (
    "SKILL.md",
    "scripts/hermes_install.py",
    "scripts/hermes_doctor.py",
    "scripts/measure.py",
    "scripts/runtime_env.py",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _check(status: str, name: str, detail: str) -> dict[str, str]:
    return {"status": status, "name": name, "detail": detail}


def _load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _plugin_install_dir(hermes_root: Path) -> Path:
    return hermes_root / "plugins" / _PLUGIN_NAME


# ---------------------------------------------------------------------------
# Individual check groups
# ---------------------------------------------------------------------------

def _hermes_home_checks() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    home = hermes_home()
    raw = os.environ.get("HERMES_HOME", "").strip()

    if raw:
        requested = Path(raw).expanduser()
        try:
            requested_resolved = requested.resolve(strict=False)
        except (OSError, ValueError):
            requested_resolved = requested
        if requested_resolved != home.resolve(strict=False):
            checks.append(_check("FAIL", "HERMES_HOME", f"ignored unsafe HERMES_HOME={raw!r}; using {home}"))
        else:
            checks.append(_check("OK", "HERMES_HOME", str(home)))
    else:
        checks.append(_check("OK", "HERMES_HOME", f"default {home}"))

    if home.exists():
        checks.append(_check("OK", "Hermes home exists", str(home)))
    else:
        checks.append(_check("WARN", "Hermes home exists", f"{home} does not exist yet"))

    parent = home if home.exists() else home.parent
    if os.access(parent, os.W_OK):
        checks.append(_check("OK", "Hermes storage writable", str(parent)))
    else:
        checks.append(_check("FAIL", "Hermes storage writable", f"{parent} is not writable"))

    return checks


def _plugin_dir_checks() -> list[dict[str, str]]:
    """Check whether the plugin payload is installed in Hermes's plugins dir."""
    checks: list[dict[str, str]] = []
    hermes_root = hermes_home()
    plugin_dir = _plugin_install_dir(hermes_root)

    if not plugin_dir.exists():
        checks.append(
            _check(
                "FAIL",
                "Plugin directory",
                f"{plugin_dir} not found; run: python3 hermes_install.py",
            )
        )
        return checks

    checks.append(_check("OK", "Plugin directory", str(plugin_dir)))

    # Check required files exist.
    missing = [f for f in REQUIRED_PLUGIN_FILES if not (plugin_dir / f).exists()]
    if missing:
        checks.append(
            _check("FAIL", "Plugin files", f"missing: {', '.join(missing)}")
        )
    else:
        checks.append(
            _check("OK", "Plugin files", f"{', '.join(REQUIRED_PLUGIN_FILES)} present")
        )

    return checks


def _plugin_yaml_checks() -> list[dict[str, str]]:
    """Parse plugin.yaml and verify required hooks are declared."""
    try:
        import yaml  # type: ignore[import]
        _has_yaml = True
    except ImportError:
        _has_yaml = False

    hermes_root = hermes_home()
    plugin_dir = _plugin_install_dir(hermes_root)
    yaml_path = plugin_dir / "plugin.yaml"

    if not yaml_path.exists():
        return [_check("WARN", "plugin.yaml", f"{yaml_path} not found; skip hook declaration check")]

    if not _has_yaml:
        # Fall back to a simple text scan.
        try:
            text = yaml_path.read_text(encoding="utf-8")
        except OSError as exc:
            return [_check("FAIL", "plugin.yaml", str(exc))]
        declared = {line.strip().lstrip("- ").strip() for line in text.splitlines() if any(h in line for h in EXPECTED_HOOKS)}
        missing_hooks = EXPECTED_HOOKS - declared
        if missing_hooks:
            return [_check(
                "WARN",
                "Declared hooks",
                f"could not verify (PyYAML not installed); text scan missing: {', '.join(sorted(missing_hooks))}",
            )]
        return [_check("OK", "Declared hooks", "text scan found all expected hooks")]

    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [_check("FAIL", "plugin.yaml parse", str(exc))]

    if not isinstance(data, dict):
        return [_check("FAIL", "plugin.yaml", "not a YAML mapping")]

    checks: list[dict[str, str]] = []
    declared_hooks = set(data.get("hooks") or [])
    missing_hooks = EXPECTED_HOOKS - declared_hooks
    if missing_hooks:
        checks.append(
            _check("WARN", "Declared hooks", f"missing: {', '.join(sorted(missing_hooks))}")
        )
    else:
        checks.append(
            _check("OK", "Declared hooks", f"all {len(EXPECTED_HOOKS)} expected hooks declared")
        )

    version = data.get("version")
    if isinstance(version, str) and version.strip():
        checks.append(_check("OK", "Plugin version", version))
    else:
        checks.append(_check("WARN", "Plugin version", "missing or blank version in plugin.yaml"))

    return checks


def _registration_checks() -> list[dict[str, str]]:
    """Load the installed plugin and check every expected hook is registered.

    Reports each hook by name, so a Hermes user can see exactly which Token
    Optimizer features are live.
    """
    plugin_dir = _plugin_install_dir(hermes_home())
    init = plugin_dir / "__init__.py"
    if not init.is_file():
        return [_check("FAIL", "Registered hooks", f"{init} not found; re-run: python3 hermes_install.py")]
    if init.is_symlink():
        return [_check("FAIL", "Registered hooks", f"{init} is a symlink; re-run hermes_install.py")]
    try:
        result = subprocess.run(
            [sys.executable, "-c", _REGISTRATION_PROBE, str(plugin_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [_check("WARN", "Registered hooks", f"could not load the plugin: {exc}")]
    data = None
    for line in reversed((result.stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        err = (result.stderr or "").strip().splitlines()
        return [_check("FAIL", "Registered hooks",
                       f"plugin failed to register: {err[-1] if err else 'no output'}")]
    registered = set(data.get("hooks") or [])
    missing = sorted(EXPECTED_HOOKS - registered)
    checks: list[dict[str, str]] = []
    if missing:
        checks.append(_check("FAIL", "Registered hooks",
                             f"{len(EXPECTED_HOOKS) - len(missing)}/{len(EXPECTED_HOOKS)}; "
                             f"not registered: {', '.join(missing)}; re-run: python3 hermes_install.py"))
    else:
        checks.append(_check("OK", "Registered hooks",
                             f"{len(EXPECTED_HOOKS)}/{len(EXPECTED_HOOKS)}: {', '.join(sorted(registered & EXPECTED_HOOKS))}"))
    if "token-optimizer" in (data.get("commands") or []):
        checks.append(_check("OK", "Slash command", "/token-optimizer registered"))
    else:
        checks.append(_check("WARN", "Slash command", "/token-optimizer not registered"))
    patterns = int(data.get("patterns") or 0)
    if patterns:
        checks.append(_check("OK", "Credential redaction", f"{patterns} patterns shared with Hermes's redactor"))
    else:
        checks.append(_check("WARN", "Credential redaction",
                             "no patterns registered (TOKEN_OPTIMIZER_HERMES_REDACTION=0, or credential_patterns.py not found)"))
    return checks


def _engine_checks() -> list[dict[str, str]]:
    """The checkout the plugin points at must hold every engine script."""
    plugin_dir = _plugin_install_dir(hermes_home())
    locator = plugin_dir / "measure-path"
    try:
        measure_py = Path(locator.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return [_check("FAIL", "Engine scripts", f"cannot read {locator}; re-run: python3 hermes_install.py")]
    sdir = measure_py.parent
    missing = [name for name in REQUIRED_ENGINE_SCRIPTS if not (sdir / name).is_file()]
    if missing:
        return [_check("FAIL", "Engine scripts",
                       f"missing in {sdir}: {', '.join(missing)}; update the Token Optimizer checkout")]
    return [_check("OK", "Engine scripts", f"{len(REQUIRED_ENGINE_SCRIPTS)} present in {sdir}")]


def _stale_files(plugin_dir: Path) -> list[str]:
    """Installed plugin files that differ from this checkout's copies."""
    import hashlib  # noqa: PLC0415

    here = Path(__file__).resolve().parent
    stale = []
    for name in _COPIED_RUNTIME_MODULES:
        src, dst = here / name, plugin_dir / name
        try:
            if src.is_file() and dst.is_file() and \
                    hashlib.sha256(src.read_bytes()).digest() != hashlib.sha256(dst.read_bytes()).digest():
                stale.append(name)
        except OSError:
            continue
    for name in ("__init__.py", "plugin.yaml"):
        src, dst = _repo_root() / "hermes" / name, plugin_dir / name
        try:
            if src.is_file() and dst.is_file() and src.read_bytes() != dst.read_bytes():
                stale.append(name)
        except OSError:
            continue
    return stale


def _freshness_checks() -> list[dict[str, str]]:
    """WARN when the installed copies or plugin version lag the checkout."""
    plugin_dir = _plugin_install_dir(hermes_home())
    if not plugin_dir.is_dir():
        return []
    stale = _stale_files(plugin_dir)
    if stale:
        return [_check("WARN", "Installed copy up to date",
                       f"differs from this checkout: {', '.join(stale)}; re-run: python3 hermes_install.py")]
    return [_check("OK", "Installed copy up to date", "plugin files match this checkout")]


def _bridge_smoke_check() -> list[dict[str, str]]:
    """Run the installed bridge's __main__ smoke test.

    hermes_hook_bridge.py's __main__ locates measure.py and exits 0 (OK) or 1
    (WARN/not found). We invoke it as a subprocess to confirm the installed copy
    can actually resolve measure.py via the measure-path locator.
    """
    hermes_root = hermes_home()
    plugin_dir = _plugin_install_dir(hermes_root)
    bridge = plugin_dir / "hermes_hook_bridge.py"

    if not bridge.exists():
        return [_check(
            "FAIL",
            "Bridge smoke test",
            f"{bridge} not found; re-run: python3 hermes_install.py",
        )]

    # v5.11.1: the installer copies a regular file. A symlink here means a
    # tampered/relocated install -- FAIL before executing it (don't run code
    # from an unexpected target).
    if bridge.is_symlink():
        return [_check(
            "FAIL",
            "Bridge smoke test",
            f"{bridge} is a symlink; re-run hermes_install.py",
        )]

    try:
        result = subprocess.run(
            [sys.executable, str(bridge)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [_check("WARN", "Bridge smoke test", f"could not run: {exc}")]

    if result.returncode == 0:
        return [_check("OK", "Bridge smoke test", (result.stdout or "").strip() or "bridge resolves measure.py")]
    return [_check(
        "FAIL",
        "Bridge smoke test",
        f"{(result.stdout or result.stderr or 'bridge could not locate measure.py').strip()}; "
        "re-run: python3 hermes_install.py",
    )]


def _activation_check() -> list[dict[str, str]]:
    """WARN if token-optimizer is not allow-listed under plugins.enabled.

    Hermes (v0.15.x) requires explicit allow-listing; directory presence is not
    enough. Uses the same dual approach as _plugin_yaml_checks (PyYAML if
    importable, else a conservative text scan).
    """
    hermes_root = hermes_home()
    cfg = _config_path(hermes_root)

    snippet_hint = f"add to Hermes config:\n{_ACTIVATION_SNIPPET}"

    if cfg is None:
        return [_check(
            "WARN",
            "Plugin activation",
            f"no Hermes config found in {hermes_root}; {snippet_hint}",
        )]

    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError as exc:
        return [_check("WARN", "Plugin activation", f"could not read {cfg}: {exc}")]

    if _config_enables_plugin(text):
        # v5.11.1: use the full path, not just the basename, so the OK
        # message is unambiguous about which config was checked.
        return [_check("OK", "Plugin activation", f"token-optimizer allow-listed in {cfg}")]
    return [_check(
        "WARN",
        "Plugin activation",
        f"token-optimizer not under plugins.enabled in {cfg}; {snippet_hint}",
    )]


def _config_path(hermes_root: Path) -> Path | None:
    for name in _CONFIG_NAMES:
        candidate = hermes_root / name
        if candidate.exists():
            return candidate
    return None


def _config_enables_plugin(text: str) -> bool:
    """Is token-optimizer listed under plugins.enabled in this config text?"""
    enabled = False
    try:
        import yaml  # type: ignore[import]  # noqa: PLC0415
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            plugins = data.get("plugins")
            if isinstance(plugins, dict):
                listed = plugins.get("enabled")
                if isinstance(listed, list):
                    enabled = _PLUGIN_NAME in [str(x).strip() for x in listed]
    except Exception:  # noqa: BLE001 — PyYAML absent or unparseable; fall back to text scan
        enabled = _activation_text_scan(text)
    return enabled


def _profile_homes(home: Path) -> list[tuple[str, Path]]:
    """Every Hermes profile next to ``home``: the root home plus profiles/<name>.

    Empty for a single-profile setup (no profiles directory), so the doctor
    only talks about profiles to people who have them.
    """
    root = home.parent.parent if home.parent.name == "profiles" else home
    profiles_dir = root / "profiles"
    try:
        named = sorted(
            (d.name, d) for d in profiles_dir.iterdir() if d.is_dir() and not d.is_symlink()
        )
    except OSError:
        return []
    if not named:
        return []
    return [("default", root), *named]


def _profile_checks() -> list[dict[str, str]]:
    """One line per Hermes profile: is the plugin installed, enabled, current?

    With gateway.multiplex_profiles one gateway runs every profile's hooks,
    and each profile only reports into its own dashboard when the plugin is
    installed and allow-listed THERE. A profile left on an older install keeps
    the old behavior, so name it.
    """
    profiles = _profile_homes(hermes_home())
    if not profiles:
        return []
    states: list[str] = []
    fixes: list[str] = []
    for name, root in profiles:
        if not (_plugin_install_dir(root) / "__init__.py").is_file():
            states.append(f"{name}: not installed")
            continue
        cfg = _config_path(root)
        try:
            enabled = cfg is not None and _config_enables_plugin(cfg.read_text(encoding="utf-8"))
        except OSError:
            enabled = False
        stale = _stale_files(_plugin_install_dir(root))
        problems = ([] if enabled else ["not enabled"]) + (["out of date"] if stale else [])
        states.append(f"{name}: {', '.join(problems) if problems else 'installed, enabled, current'}")
        if problems:
            fixes.append(f"HERMES_HOME={root} python3 measure.py hermes-install --enable")
    detail = "; ".join(states)
    if fixes:
        return [_check("WARN", "Hermes profiles", f"{detail}. Fix: " + " ; ".join(fixes))]
    return [_check("OK", "Hermes profiles", detail)]


def _activation_text_scan(text: str) -> bool:
    """Conservative scan for token-optimizer under plugins.enabled (no PyYAML).

    Single source of truth lives in hermes_install._enabled_in_text (the
    installer and doctor must agree on what "activated" means). The two
    scripts always ship side by side, so the import cannot miss in a healthy
    install; if it somehow does, report not-enabled (WARN) rather than crash.
    """
    try:
        from hermes_install import _enabled_in_text  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- v5.11.1: any import failure
        # (ImportError, but also a SyntaxError in a half-written install or a
        # surprise at module top-level) must degrade to not-enabled, not crash
        # the doctor.
        return False
    return _enabled_in_text(text)


def _state_db_checks() -> list[dict[str, str]]:
    """Check readability of ~/.hermes/state.db."""
    checks: list[dict[str, str]] = []
    hermes_root = hermes_home()
    db_path = hermes_root / "state.db"

    if not db_path.exists():
        checks.append(
            _check(
                "WARN",
                "state.db",
                f"{db_path} not found; Hermes creates it on first session",
            )
        )
        return checks

    conn = None
    try:
        # H-2: build the file: URI via Path.as_uri() rather than string
        # interpolation. A path containing '?' would start the query string
        # early and could drop or override mode=ro (opening read-write).
        uri = f"{Path(db_path).as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout=3000")
        row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        session_count = row[0] if row else 0
        checks.append(
            _check("OK", "state.db readable", f"{db_path} ({session_count} sessions)")
        )
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "no such table" in msg:
            checks.append(
                _check("WARN", "state.db", f"{db_path} exists but sessions table absent (no sessions yet)")
            )
        elif "unable to open" in msg or "locked" in msg:
            checks.append(_check("FAIL", "state.db", f"cannot open read-only: {exc}"))
        else:
            checks.append(_check("WARN", "state.db", f"unexpected error: {exc}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("FAIL", "state.db", f"unexpected error: {exc}"))
    finally:
        if conn is not None:
            conn.close()

    return checks


def _dashboard_port_check() -> dict[str, str]:
    """Check whether dashboard port 24844 is available (not already in use)."""
    port = DASHBOARD_PORT
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port))
        if result == 0:
            # Port is occupied — could be our own dashboard or something else.
            return _check("OK", f"Dashboard port {port}", f"port {port} in use (dashboard may already be running)")
        return _check("OK", f"Dashboard port {port}", f"port {port} is free")
    except OSError as exc:
        return _check("WARN", f"Dashboard port {port}", f"could not probe: {exc}")


def _skill_install_checks() -> list[dict[str, str]]:
    root = _skill_root()
    missing = [rel for rel in SKILL_INSTALL_FILES if not (root / rel).exists()]
    if missing:
        return [_check("FAIL", "Installed skill files", ", ".join(missing))]
    return [
        _check("OK", "Install shape", f"standalone Hermes skill at {root}"),
        _check("OK", "Installed skill files", f"{len(SKILL_INSTALL_FILES)} present"),
    ]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_checks() -> list[dict[str, str]]:
    checks = [
        _check("OK", "Detected runtime", detect_runtime()),
        _check("OK", "Runtime home", str(runtime_home())),
    ]
    checks.extend(_hermes_home_checks())
    checks.extend(_skill_install_checks())
    checks.extend(_plugin_dir_checks())
    checks.extend(_plugin_yaml_checks())
    checks.extend(_bridge_smoke_check())
    checks.extend(_registration_checks())
    checks.extend(_engine_checks())
    checks.extend(_freshness_checks())
    checks.extend(_activation_check())
    checks.extend(_profile_checks())
    checks.extend(_state_db_checks())
    checks.append(_dashboard_port_check())
    return checks


def _print_text(checks: list[dict[str, str]]) -> None:
    print("\nToken Optimizer Hermes Doctor")
    print("=" * 29)
    for check in checks:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    counts = {
        status: sum(1 for c in checks if c["status"] == status)
        for status in ("OK", "WARN", "FAIL")
    }
    print(f"\nSummary: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL")
    print("\nThis checks Token Optimizer's Hermes integration.")
    print("For generic Hermes runtime health, run the native Hermes diagnostics.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Token Optimizer Hermes adapter readiness."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checks = run_checks()
    if args.json:
        print(json.dumps({"checks": checks}, indent=2))
    else:
        _print_text(checks)
    return 1 if any(c["status"] == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
