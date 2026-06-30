#!/usr/bin/env python3
"""Hermes-specific install readiness checks for Token Optimizer."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

from runtime_env import detect_runtime, hermes_home, runtime_home

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_PORT = 24844
_PLUGIN_NAME = "token-optimizer"

# #58: the plugin's __init__.py imports these three from its own directory, so
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
    # v5.11.1 (#58): the installer always writes the measure-path locator when
    # measure.py exists; its absence means a broken / payload-only install.
    "measure-path",
)

# Hermes config: the plugin must be allow-listed under plugins.enabled.
_CONFIG_NAMES = ("config.yaml", "config.yml")
_ACTIVATION_SNIPPET = "plugins:\n  enabled:\n    - token-optimizer\n"

EXPECTED_HOOKS = {
    "pre_llm_call",
    "post_api_request",
    "on_session_finalize",
    "on_session_end",
}

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


def _bridge_smoke_check() -> list[dict[str, str]]:
    """Run the installed bridge's __main__ smoke test (#58).

    hermes_hook_bridge.py's __main__ locates measure.py and exits 0 (OK) or 1
    (WARN/not found). We invoke it as a subprocess to confirm the installed copy
    can actually resolve measure.py via the measure-path locator.
    """
    import subprocess  # noqa: PLC0415

    hermes_root = hermes_home()
    plugin_dir = _plugin_install_dir(hermes_root)
    bridge = plugin_dir / "hermes_hook_bridge.py"

    if not bridge.exists():
        return [_check(
            "FAIL",
            "Bridge smoke test",
            f"{bridge} not found; re-run: python3 hermes_install.py",
        )]

    # v5.11.1 (#58): the installer copies a regular file. A symlink here means a
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
    """WARN if token-optimizer is not allow-listed under plugins.enabled (#58).

    Hermes (v0.15.x) requires explicit allow-listing; directory presence is not
    enough. Uses the same dual approach as _plugin_yaml_checks (PyYAML if
    importable, else a conservative text scan).
    """
    hermes_root = hermes_home()
    cfg = None
    for name in _CONFIG_NAMES:
        candidate = hermes_root / name
        if candidate.exists():
            cfg = candidate
            break

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

    if enabled:
        # v5.11.1 (#58): use the full path, not just the basename, so the OK
        # message is unambiguous about which config was checked.
        return [_check("OK", "Plugin activation", f"token-optimizer allow-listed in {cfg}")]
    return [_check(
        "WARN",
        "Plugin activation",
        f"token-optimizer not under plugins.enabled in {cfg}; {snippet_hint}",
    )]


def _activation_text_scan(text: str) -> bool:
    """Conservative scan for token-optimizer under plugins.enabled (no PyYAML).

    Single source of truth lives in hermes_install._enabled_in_text (the
    installer and doctor must agree on what "activated" means). The two
    scripts always ship side by side, so the import cannot miss in a healthy
    install; if it somehow does, report not-enabled (WARN) rather than crash.
    """
    try:
        from hermes_install import _enabled_in_text  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- v5.11.1 (#58): any import failure
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
        uri = f"file:{db_path}?mode=ro&immutable=1"
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
    checks.extend(_activation_check())
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
