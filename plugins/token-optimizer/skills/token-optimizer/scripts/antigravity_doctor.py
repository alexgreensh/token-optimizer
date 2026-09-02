#!/usr/bin/env python3
"""Token Optimizer — Google Antigravity readiness doctor.

Per-check readiness report with fix hints (R17). The Antigravity data plane is
read-only and version-volatile, so the doctor names exactly which source is
ready and which needs a fix rather than a single pass/fail.

Checks:
  agy binary on PATH + version
  Antigravity home + the three surface directories
  plugin directory + hooks.json shape + payload completeness
  plugin enabled state in config.json (read-only)
  consent record present
  conversation store readable + decodable on the newest database per surface
  conversation summaries database readable per surface
  dashboard daemon port (24846)

Usage:
    python3 antigravity_doctor.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from runtime_env import antigravity_home  # noqa: E402

DAEMON_PORT = 24846
_VERSION_TIMEOUT_SECONDS = 5
_SURFACES = ("antigravity-cli", "antigravity", "antigravity-ide")


def _check(status: str, name: str, detail: str, hint: str = "") -> dict:
    out = {"status": status, "name": name, "detail": detail}
    if hint:
        out["hint"] = hint
    return out


def _binary_checks() -> list:
    checks = []
    override = os.environ.get("TOKEN_OPTIMIZER_ANTIGRAVITY_BIN", "").strip()
    if override:
        if not os.path.isabs(override):
            checks.append(
                _check(
                    "fail",
                    "agy binary",
                    f"TOKEN_OPTIMIZER_ANTIGRAVITY_BIN ({override!r}) is not an absolute path.",
                    "Set it to the absolute path of the agy binary.",
                )
            )
            return checks
        try:
            from antigravity_install import _py_path_is_trusted
        except Exception:
            _py_path_is_trusted = None
        if _py_path_is_trusted is None or not _py_path_is_trusted(override):
            checks.append(
                _check(
                    "fail",
                    "agy binary",
                    f"TOKEN_OPTIMIZER_ANTIGRAVITY_BIN ({override}) failed the trust check "
                    "(must be under a system prefix or user-owned and not group/other-writable).",
                    "Point it at a trusted agy binary, or unset to resolve `agy` from PATH.",
                )
            )
            return checks
        exe = override
        source = f"{exe} (override)"
    else:
        exe = shutil.which("agy")
        if not exe:
            checks.append(
                _check(
                    "fail",
                    "agy binary",
                    "`agy` not found on PATH.",
                    "Install Antigravity (https://antigravity.google/), or set "
                    "TOKEN_OPTIMIZER_ANTIGRAVITY_BIN to its location.",
                )
            )
            return checks
        source = exe

    version = ""
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        version = first[0] if first else ""
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        version = ""
    if version:
        checks.append(_check("ok", "agy binary", f"{source} — {version}"))
    else:
        checks.append(
            _check(
                "warn",
                "agy binary",
                f"{source} found but version not detected.",
                "Run `agy --version` manually; auth may be required.",
            )
        )
    return checks


def _home_checks() -> list:
    checks = []
    root = antigravity_home()
    if not root.exists():
        checks.append(
            _check(
                "fail",
                "Antigravity home",
                f"{root} does not exist.",
                "Run `agy` once so it creates its home, then re-run install.",
            )
        )
        return checks
    checks.append(_check("ok", "Antigravity home", str(root)))

    try:
        from antigravity_state import surface_dirs
    except Exception:
        checks.append(_check("warn", "surface directories", "antigravity_state unavailable."))
        return checks
    surfaces = surface_dirs(root)
    present = sorted(s for s, _ in surfaces)
    if not present:
        checks.append(
            _check(
                "warn",
                "surface directories",
                "none of antigravity-cli / antigravity / antigravity-ide exist yet.",
                "Run an Antigravity CLI/app/IDE session first.",
            )
        )
    else:
        checks.append(_check("ok", "surface directories", ", ".join(present)))
    return checks


def _plugin_checks() -> list:
    checks = []
    try:
        from antigravity_install import plugin_dir as _plugin_dir, _PAYLOAD_MODULES
    except Exception:
        checks.append(_check("warn", "plugin directory", "antigravity_install unavailable."))
        return checks

    pdir = _plugin_dir(antigravity_home())
    if not pdir.is_dir():
        checks.append(
            _check(
                "warn",
                "plugin directory",
                f"{pdir} missing.",
                "Run `python3 measure.py antigravity-install`.",
            )
        )
        return checks

    hooks_path = pdir / "hooks.json"
    if not hooks_path.is_file():
        checks.append(
            _check(
                "fail",
                "plugin hooks",
                f"{hooks_path} missing.",
                "Re-run `python3 measure.py antigravity-install` to rewrite it.",
            )
        )
    else:
        try:
            config = json.loads(hooks_path.read_text(encoding="utf-8"))
            group = config.get("token-optimizer") if isinstance(config, dict) else None
            events = sorted(group.keys()) if isinstance(group, dict) else []
            expected = {"PreInvocation", "PreToolUse", "Stop"}
            if not expected <= set(events):
                raise ValueError(f"missing events: {sorted(expected - set(events))}")
            checks.append(_check("ok", "plugin hooks", f"{hooks_path} (events: {', '.join(events)})"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            checks.append(
                _check(
                    "fail",
                    "plugin hooks",
                    f"{hooks_path} invalid: {exc}.",
                    "Re-run `python3 measure.py antigravity-install` to rewrite it.",
                )
            )

    missing = [m for m in _PAYLOAD_MODULES if not (pdir / m).is_file()]
    if missing:
        checks.append(
            _check(
                "warn",
                "plugin payload",
                f"Missing module(s): {', '.join(missing)}.",
                "Re-run `python3 measure.py antigravity-install` to refresh the payload.",
            )
        )
    else:
        checks.append(_check("ok", "plugin payload", f"{pdir} (complete)"))
    return checks


def _config_enabled_check() -> list:
    root = antigravity_home()
    config_path = root / "config" / "config.json"
    if not config_path.is_file():
        return [_check("ok", "plugin enabled", f"{config_path} absent (plugins enabled by default)")]
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return [_check("warn", "plugin enabled", f"{config_path} unreadable (read-only check).")]
    plugins = config.get("plugins") if isinstance(config, dict) else None
    entry = plugins.get("token-optimizer") if isinstance(plugins, dict) else None
    enabled = entry.get("enabled") if isinstance(entry, dict) else None
    if enabled is False:
        return [
            _check(
                "fail",
                "plugin enabled",
                f"{config_path} marks plugins.token-optimizer.enabled = false.",
                "Run `agy plugin enable token-optimizer` to re-enable.",
            )
        ]
    return [_check("ok", "plugin enabled", f"{config_path} (enabled or default)")]


def _consent_check() -> list:
    try:
        from antigravity_install import data_dir as _data_dir
    except Exception:
        return [_check("warn", "consent record", "antigravity_install unavailable.")]
    config_path = _data_dir(antigravity_home()) / "config.json"
    try:
        if not config_path.is_file():
            return [
                _check(
                    "warn",
                    "consent record",
                    f"{config_path} missing — bridge and rollup are inert.",
                    "Run `bash install.sh --antigravity` to record consent.",
                )
            ]
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        ok = bool(isinstance(cfg, dict) and cfg.get("antigravity_consent"))
    except (OSError, json.JSONDecodeError, ValueError):
        ok = False
    if ok:
        return [_check("ok", "consent record", str(config_path))]
    return [
        _check(
            "warn",
            "consent record",
            f"{config_path} does not record antigravity_consent.",
            "Run `bash install.sh --antigravity` to record consent.",
        )
    ]


def _conversation_store_checks() -> list:
    checks = []
    try:
        from antigravity_install import plugin_dir  # noqa: F401  (unused, keeps imports local)
        from antigravity_state import surface_dirs, read_conversation
        from antigravity_proto import DECODER_VERSION
    except Exception:
        checks.append(_check("warn", "conversation store", "antigravity_state unavailable."))
        return checks

    root = antigravity_home()
    any_surface = False
    for surface, surface_path in surface_dirs(root):
        conv_dir = surface_path / "conversations"
        if not conv_dir.is_dir():
            continue
        any_surface = True
        try:
            dbs = sorted(conv_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            dbs = []
        if not dbs:
            checks.append(
                _check(
                    "warn",
                    f"conversation store ({surface})",
                    f"{conv_dir} has no conversation databases yet.",
                    "Run an Antigravity session on this surface.",
                )
            )
            continue
        newest = dbs[0]
        session = read_conversation(newest, surface=surface)
        if session is None:
            checks.append(
                _check(
                    "warn",
                    f"conversation store ({surface})",
                    f"newest database {newest.name} unreadable (no gen_metadata or not SQLite).",
                    "Run an Antigravity session, then re-check.",
                )
            )
            continue
        total = len(session["generations"]) + session["undecodable_rows"]
        detail = (
            f"{len(dbs)} database(s); newest {newest.name}: "
            f"{len(session['generations'])} decodable, {session['undecodable_rows']} undecodable "
            f"of {total} gen_metadata rows"
        )
        if session["undecodable_rows"]:
            checks.append(
                _check(
                    "warn",
                    f"conversation store ({surface})",
                    detail + ".",
                    f"Decoder is {DECODER_VERSION}; update Token Optimizer if Antigravity changed "
                    "gen_metadata field numbers.",
                )
            )
        else:
            checks.append(_check("ok", f"conversation store ({surface})", detail))
    if not any_surface:
        checks.append(_check("warn", "conversation store", "no surface directories with conversations/ found."))
    return checks


def _summaries_checks() -> list:
    checks = []
    try:
        from antigravity_state import surface_dirs, read_summaries
    except Exception:
        checks.append(_check("warn", "summaries database", "antigravity_state unavailable."))
        return checks
    root = antigravity_home()
    surfaces = surface_dirs(root)
    for surface, surface_path in surfaces:
        db_path = surface_path / "conversation_summaries.db"
        if not db_path.is_file():
            checks.append(
                _check(
                    "warn",
                    f"summaries database ({surface})",
                    f"{db_path} absent.",
                    "Run an Antigravity session; summaries seed automatically.",
                )
            )
            continue
        summaries = read_summaries(surface_path)
        checks.append(_check("ok", f"summaries database ({surface})", f"{len(summaries)} conversation summary row(s)"))
    if not surfaces:
        checks.append(_check("warn", "summaries database", "no surface directories to inspect."))
    return checks


def _daemon_check() -> dict:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            in_use = sock.connect_ex(("127.0.0.1", DAEMON_PORT)) == 0
    except OSError:
        in_use = False
    if in_use:
        return _check("ok", "dashboard daemon", f"port {DAEMON_PORT} serving")
    return _check("ok", "dashboard daemon", f"port {DAEMON_PORT} free (daemon not running — optional)")


def run_checks() -> list:
    checks = []
    checks.extend(_binary_checks())
    checks.extend(_home_checks())
    checks.extend(_plugin_checks())
    checks.extend(_config_enabled_check())
    checks.extend(_consent_check())
    checks.extend(_conversation_store_checks())
    checks.extend(_summaries_checks())
    checks.append(_daemon_check())
    return checks


_BADGES = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_text(checks: list) -> None:
    print("Token Optimizer — Google Antigravity doctor")
    print()
    for c in checks:
        print(f"  {_BADGES.get(c['status'], '[?]   ')} {c['name']}: {c['detail']}")
        if c.get("hint"):
            print(f"         fix: {c['hint']}")
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    print()
    print(f"  {len(checks)} checks — {fails} fail, {warns} warn")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    checks = run_checks()
    if args.json:
        print(json.dumps(checks, indent=1))
    else:
        _print_text(checks)
    return 1 if any(c["status"] == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
