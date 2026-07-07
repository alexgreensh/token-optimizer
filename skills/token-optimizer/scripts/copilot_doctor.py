#!/usr/bin/env python3
"""Token Optimizer — GitHub Copilot readiness doctor.

Per-source health report with fix-it hints. Copilot's data surfaces are
version-volatile (weekly CLI releases; hook fields break and regress
upstream), so the doctor names exactly WHICH source works and which broke
rather than a single pass/fail.

Checks:
  P0  copilot binary on PATH + version
  P0  ~/.copilot exists; hooks dir present, a directory, and writable
  P1  Token Optimizer hook config installed + parseable
  P1  capabilities.json freshness vs installed CLI version + matrix age
  P1  session-state presence (CLI data plane)
  P2  VS Code debug-logs / OTel presence (editor data plane)
  P2  daemon port availability

Usage:
    python3 copilot_doctor.py [--json]
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import shutil
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from runtime_env import copilot_home  # noqa: E402

_MATRIX_STALE_DAYS = 60
DAEMON_PORT = 24845


def _check(status: str, name: str, detail: str, hint: str = "") -> dict:
    # Intentional extension over codex_doctor/hermes_doctor._check: Copilot's
    # data surfaces are version-volatile, so every failure carries a fix hint.
    out = {"status": status, "name": name, "detail": detail}
    if hint:
        out["hint"] = hint
    return out


def _binary_checks() -> list:
    checks = []
    exe = os.environ.get("TOKEN_OPTIMIZER_COPILOT_BIN") or "copilot"
    path = shutil.which(exe)
    if not path:
        checks.append(
            _check(
                "fail",
                "copilot binary",
                f"`{exe}` not found on PATH.",
                "Install the Copilot CLI (https://docs.github.com/copilot/copilot-cli), "
                "or set TOKEN_OPTIMIZER_COPILOT_BIN to its location.",
            )
        )
        return checks
    try:
        import copilot_hook_bridge

        version, raw = copilot_hook_bridge._copilot_cli_version()
    except Exception:
        version, raw = None, ""
    if version:
        checks.append(_check("ok", "copilot binary", f"{path} (v{'.'.join(map(str, version))})"))
    else:
        checks.append(
            _check(
                "warn",
                "copilot binary",
                f"{path} found but version not detected ({raw or 'no output'}).",
                "Run `copilot --version` manually; auth may be required.",
            )
        )
    return checks


def _home_checks() -> list:
    checks = []
    root = copilot_home()
    if not root.exists():
        checks.append(
            _check(
                "fail",
                "~/.copilot",
                f"{root} does not exist.",
                "Run the Copilot CLI once (`copilot`) so it creates its home, then re-run install.",
            )
        )
        return checks
    checks.append(_check("ok", "~/.copilot", str(root)))

    hooks_dir = root / "hooks"
    if hooks_dir.exists() and not hooks_dir.is_dir():
        checks.append(
            _check(
                "fail",
                "hooks dir",
                f"{hooks_dir} exists but is NOT a directory.",
                "Move the file aside; hooks must live in a directory of *.json files.",
            )
        )
    elif hooks_dir.exists() and not os.access(str(hooks_dir), os.W_OK):
        checks.append(
            _check("fail", "hooks dir", f"{hooks_dir} is not writable.", "Fix permissions (chmod u+w).")
        )
    elif not hooks_dir.exists():
        checks.append(
            _check(
                "warn",
                "hooks dir",
                f"{hooks_dir} missing (created on install).",
                "Run `python3 measure.py copilot-install`.",
            )
        )
    else:
        checks.append(_check("ok", "hooks dir", f"{hooks_dir} (writable)"))
    return checks


def _hook_config_checks() -> list:
    checks = []
    hook_path = copilot_home() / "hooks" / "token-optimizer.json"
    if not hook_path.exists():
        checks.append(
            _check(
                "warn",
                "TO hook config",
                "Not installed.",
                "Run `python3 measure.py copilot-install`.",
            )
        )
        return checks
    try:
        config = json.loads(hook_path.read_text(encoding="utf-8"))
        events = sorted((config.get("hooks") or {}).keys())
        checks.append(_check("ok", "TO hook config", f"{hook_path} (events: {', '.join(events)})"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        checks.append(
            _check(
                "fail",
                "TO hook config",
                f"{hook_path} unreadable/invalid: {exc}",
                "Re-run `python3 measure.py copilot-install` to rewrite it.",
            )
        )

    # Payload integrity: the bridge needs codex_io (atomic_write_json) or its
    # tally + capability writes silently no-op (crash recovery dies).
    plugin_dir = copilot_home() / "token-optimizer" / "plugin"
    if plugin_dir.is_dir():
        missing = [
            m for m in ("copilot_hook_bridge.py", "codex_io.py", "bash_compress.py")
            if not (plugin_dir / m).exists()
        ]
        if missing:
            checks.append(
                _check(
                    "fail",
                    "hook payload",
                    f"Installed bridge is missing modules: {', '.join(missing)}.",
                    "Re-run `python3 measure.py copilot-install` to refresh the payload.",
                )
            )
        else:
            checks.append(_check("ok", "hook payload", f"{plugin_dir} (complete)"))
    return checks


def _capability_checks() -> list:
    checks = []
    cap_path = copilot_home() / "token-optimizer" / "capabilities.json"
    if not cap_path.exists():
        checks.append(
            _check(
                "warn",
                "capabilities",
                "capabilities.json missing (seeded on install / first sessionStart).",
                "Run `python3 measure.py copilot-install`.",
            )
        )
        return checks
    try:
        data = json.loads(cap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        checks.append(_check("fail", "capabilities", "capabilities.json is corrupt.", "Delete it; it reseeds on next session."))
        return checks

    caps = data.get("caps") or {}
    on = sorted(k for k, v in caps.items() if v)
    off = sorted(k for k, v in caps.items() if not v)
    checks.append(
        _check(
            "ok",
            "capabilities",
            f"CLI {data.get('cli_version', '?')} — enabled: {', '.join(on) or 'none'}"
            + (f"; gated off: {', '.join(off)}" if off else ""),
        )
    )

    # Version drift: installed CLI vs the version the caps were seeded for.
    try:
        import copilot_hook_bridge

        version, raw = copilot_hook_bridge._copilot_cli_version()
        if version:
            current = ".".join(map(str, version))
            if data.get("cli_version") not in (current, None):
                # The doctor runs in the native shell where `copilot` IS on PATH,
                # so it can resolve the real version even when the WSL-root
                # sessionStart hook could only seed "unknown" (issue #78). Rather
                # than tell the user to "start a session" (which, in that WSL-root
                # context, would just re-seed "unknown" again), self-heal now:
                # persist the correct matrix so postToolUse/allow/etc. stop being
                # gated off on a capable CLI.
                healed = False
                try:
                    new_caps = copilot_hook_bridge.reseed_capabilities(version, raw)
                    healed = isinstance(new_caps, dict) and bool(new_caps)
                except Exception:
                    healed = False
                if healed:
                    checks.append(
                        _check(
                            "ok",
                            "capabilities freshness",
                            f"Reseeded {data.get('cli_version')} -> {current} "
                            "(matrix now matches the installed CLI).",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "warn",
                            "capabilities freshness",
                            f"Seeded for {data.get('cli_version')}, installed CLI is {current}.",
                            "Re-run `python3 measure.py copilot-install` to reseed the matrix.",
                        )
                    )
    except Exception:
        pass

    research = data.get("matrix_research_date", "")
    try:
        # timegm parses as UTC; mktime would skew the age by the local offset.
        age_days = (time.time() - calendar.timegm(time.strptime(research, "%Y-%m-%d"))) / 86400
        if age_days > _MATRIX_STALE_DAYS:
            checks.append(
                _check(
                    "warn",
                    "capability matrix age",
                    f"Matrix research is {int(age_days)} days old; upstream hook bugs may have been fixed since.",
                    "Update Token Optimizer, or override via TOKEN_OPTIMIZER_COPILOT_CAPS_JSON.",
                )
            )
    except (ValueError, OverflowError):
        pass
    return checks


def _cli_data_checks() -> list:
    checks = []
    state_dir = copilot_home() / "session-state"
    if not state_dir.exists():
        checks.append(
            _check(
                "warn",
                "CLI session data",
                f"{state_dir} missing — no Copilot CLI sessions recorded yet.",
                "Run a Copilot CLI session; analytics activate automatically.",
            )
        )
        return checks
    try:
        sessions = [p for p in state_dir.iterdir() if p.is_dir()]
    except OSError:
        sessions = []
    incomplete = 0
    try:
        for s in sessions[:200]:
            events = s / "events.jsonl"
            if not events.exists():
                continue
            # Seek the tail only — events.jsonl can be tens of MB on long
            # sessions; reading the whole file 200x would stall or OOM.
            try:
                with events.open("rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(-min(65536, size), 2)
                    tail = fh.read()
            except OSError:
                continue
            # Quote the marker so a log line that merely mentions the phrase
            # ("waiting for session.shutdown") can't read as a real event.
            if b'"session.shutdown"' not in tail:
                incomplete += 1
    except Exception:
        incomplete = -1
    detail = f"{len(sessions)} session(s)"
    if incomplete > 0:
        detail += f"; {incomplete} without a shutdown event (crash/kill — recovered via partial data)"
    checks.append(_check("ok", "CLI session data", detail))
    return checks


def _vscode_data_checks() -> list:
    checks = []
    try:
        import copilot_vscode

        status = copilot_vscode.enablement_status()
    except Exception as exc:
        checks.append(_check("warn", "VS Code data plane", f"reader unavailable: {exc}"))
        return checks
    if status.get("debug_logs_found"):
        checks.append(_check("ok", "VS Code debug-logs", "found (per-request cost active — authoritative source)"))
    elif status.get("otel_db_found"):
        checks.append(
            _check(
                "ok",
                "VS Code OTel DB",
                "agent-traces.db found (fallback source; token metadata only)",
            )
        )
    else:
        checks.append(
            _check(
                "warn",
                "VS Code data plane",
                "No debug-logs or OTel DB found.",
                'Enable both "github.copilot.chat.agentDebugLog" settings in VS Code '
                "(note: debug logs store full prompt text on disk).",
            )
        )
    return checks


def _daemon_check() -> dict:
    import socket

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
    checks.extend(_hook_config_checks())
    checks.extend(_capability_checks())
    checks.extend(_cli_data_checks())
    checks.extend(_vscode_data_checks())
    checks.append(_daemon_check())
    return checks


_BADGES = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_text(checks: list) -> None:
    print("Token Optimizer — GitHub Copilot doctor")
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
