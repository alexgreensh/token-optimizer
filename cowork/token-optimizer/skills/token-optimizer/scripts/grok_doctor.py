#!/usr/bin/env python3
"""Token Optimizer — Grok Build readiness doctor.

Per-surface health report with fix-it hints, plus ``--probe`` which replays
the documented payload for each wired event through the exact installed
command string (POSIX ``/bin/sh -c`` not required — the command is
shlex-parsed and run directly) so a live Grok is not required to prove the
hooks can fire.

Built for NO-INSTALL / contract-only mode: every check degrades to a ``warn``
when the Grok host is absent (the state a future tester closes), never a spurious
``fail`` solely because Grok is not installed.

Checks:
  P0  Grok binary presence (``grok`` on PATH, or ~/.grok/bin/grok)
  P0  ~/.grok exists; hooks dir present + writable
  P1  Token Optimizer hook file installed (five events) + parseable
  P1  installed payload integrity (bridge, atomic-writer, compress)
  P1  observed-events ledger (which events fired)
  P2  session store (~/.grok/sessions readable)
  P2  daemon port 24848 availability

Usage:
    python3 grok_doctor.py [--json]
    python3 grok_doctor.py --probe [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from runtime_env import grok_home  # noqa: E402
from grok_install import _py_path_is_trusted  # noqa: E402

DAEMON_PORT = 24848
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The five events the installer wires (order = probe order = docs order).
_WIRED_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


def _check(status: str, name: str, detail: str, hint: str = "") -> dict:
    out = {"status": status, "name": name, "detail": detail}
    if hint:
        out["hint"] = hint
    return out


def _binary_checks() -> list:
    checks = []
    override = os.environ.get("TOKEN_OPTIMIZER_GROK_BIN", "").strip()
    candidates = [override] if override else []
    candidates.append(shutil.which("grok") or "")
    # postinstall drops the binary at <GROK_HOME>/bin/grok (versioned + a
    # symlink/copy at the bare name); check it directly, not via PATH.
    candidates.append(str(grok_home() / "bin" / "grok"))
    found = next((c for c in candidates if c and Path(c).is_file()), None)
    if found:
        checks.append(_check("ok", "grok binary", found))
    else:
        checks.append(
            _check(
                "warn",
                "grok binary",
                "No Grok Build binary found (checked PATH `grok` and ~/.grok/bin/grok).",
                "Install Grok Build, or set TOKEN_OPTIMIZER_GROK_BIN. "
                "Contract-only beta: this warn is expected until a live host is attached.",
            )
        )
    return checks


def _home_checks() -> list:
    checks = []
    root = grok_home()
    if not root.exists():
        checks.append(
            _check(
                "warn",
                "grok home",
                f"{root} does not exist yet.",
                "Run Grok once so it creates its home, then re-run install.",
            )
        )
        return checks
    checks.append(_check("ok", "grok home", str(root)))

    hooks_dir = root / "hooks"
    if not hooks_dir.exists():
        checks.append(
            _check("warn", "hooks dir", f"{hooks_dir} missing (created on install).",
                   "Run `python3 measure.py grok-install`.")
        )
    elif not hooks_dir.is_dir():
        checks.append(_check("fail", "hooks dir", f"{hooks_dir} exists but is not a directory."))
    elif not os.access(str(hooks_dir), os.W_OK):
        checks.append(_check("fail", "hooks dir", f"{hooks_dir} is not writable.",
                             "Fix permissions (chmod u+w)."))
    else:
        checks.append(_check("ok", "hooks dir", f"{hooks_dir} (writable)"))
    return checks


def _iter_commands(config: dict):
    """Yield (event, command) from Grok's nested hook shape.

    ``config["hooks"]`` -> ``{Event: [{matcher?, hooks: [{command, ...}]}]}``.
    """
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for h in handlers:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    yield str(event), h["command"]


def _hook_config_checks() -> list:
    checks = []
    hook_path = grok_home() / "hooks" / "token-optimizer.json"
    if not hook_path.exists():
        checks.append(_check("warn", "TO hook config", "Not installed.",
                             "Run `python3 measure.py grok-install`."))
        return checks
    try:
        config = json.loads(hook_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        checks.append(
            _check("fail", "TO hook config", f"{hook_path} unreadable/invalid: {exc}",
                   "Re-run `python3 measure.py grok-install` to rewrite it.")
        )
        return checks

    commands = {event: [c for _, c in _iter_commands(config) if _ == event]
                for event in _WIRED_EVENTS}
    ours = {event: [c for c in commands[event] if "grok_hook_bridge.py" in c]
            for event in _WIRED_EVENTS}
    wired = [e for e in _WIRED_EVENTS if ours[e]]
    if wired:
        checks.append(_check("ok", "TO hook config",
                             f"{hook_path} (wired events: {', '.join(wired)})"))
    else:
        checks.append(
            _check("fail", "TO hook config", "No Token Optimizer grok entries found.",
                   "Run `python3 measure.py grok-install`.")
        )
    missing = [e for e in _WIRED_EVENTS if not ours[e]]
    if missing and wired:
        checks.append(
            _check("warn", "hook event coverage", f"Missing events: {', '.join(missing)}.",
                   "Re-run install to restore the full five-event set.")
        )
    return checks


def _payload_checks() -> list:
    checks = []
    plugin_dir = grok_home() / "token-optimizer" / "plugin"
    if not plugin_dir.is_dir():
        checks.append(_check("warn", "hook payload", f"{plugin_dir} missing.",
                             "Run `python3 measure.py grok-install`."))
        return checks
    missing = [
        m for m in ("grok_hook_bridge.py", "codex_io.py", "bash_compress.py")
        if not (plugin_dir / m).exists()
    ]
    if missing:
        checks.append(
            _check("fail", "hook payload",
                   f"Installed bridge is missing modules: {', '.join(missing)}.",
                   "Re-run `python3 measure.py grok-install` to refresh the payload.")
        )
    else:
        checks.append(_check("ok", "hook payload", f"{plugin_dir} (complete)"))
    return checks


def _locator_checks() -> list:
    checks = []
    plugin_dir = grok_home() / "token-optimizer" / "plugin"
    locator = plugin_dir / "measure-path"
    if not locator.exists():
        checks.append(
            _check("warn", "measure-path locator", f"{locator} missing (rollups paused).",
                   "Run `python3 measure.py grok-install` to rewrite it.")
        )
        return checks
    try:
        target = Path(locator.read_text(encoding="utf-8").strip())
    except OSError:
        target = None
    if target is not None and target.is_file():
        checks.append(_check("ok", "measure-path locator", str(target)))
    else:
        checks.append(
            _check("fail", "measure-path locator",
                   f"{locator} does not name an existing measure.py.",
                   "Run `python3 measure.py grok-install` to rewrite it.")
        )
    return checks


def _persisted_python_check() -> list:
    checks = []
    commands = _installed_commands()
    if not commands:
        checks.append(
            _check("warn", "persisted python", "No wired hook command to inspect.",
                   "Run `python3 measure.py grok-install`.")
        )
        return checks
    for _event, cmd in commands.items():
        argv = _parse_hook_command(cmd)
        if argv and Path(argv[0]).is_file() and _py_path_is_trusted(argv[0]):
            checks.append(_check("ok", "persisted python", argv[0]))
            return checks
    checks.append(
        _check("fail", "persisted python",
               "The wired hook command does not use a trusted absolute python path.",
               "Run `python3 measure.py grok-install` to re-persist a trusted interpreter.")
    )
    return checks


def _read_observed(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    entries.append(data)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries


def _observed_checks() -> list:
    checks = []
    path = grok_home() / "token-optimizer" / "observed-events.jsonl"
    entries = _read_observed(path)
    if not entries:
        checks.append(
            _check("warn", "observed events", f"{path} empty or missing.",
                   "Run `python3 measure.py grok-doctor --probe` to prove hooks fire.")
        )
        return checks
    counts: dict = {}
    for e in entries:
        event = str(e.get("event") or "?")
        counts[event] = counts.get(event, 0) + 1
    summary = ", ".join(f"{k} ({v})" for k, v in sorted(counts.items()))
    checks.append(_check("ok", "observed events", f"{len(entries)} event(s): {summary}"))
    return checks


def _session_store_checks() -> list:
    checks = []
    sessions = grok_home() / "sessions"
    if sessions.is_dir():
        checks.append(_check("ok", "session store", str(sessions)))
    else:
        checks.append(
            _check("warn", "session store", f"{sessions} missing (no Grok sessions yet).")
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
    return _check("ok", "dashboard daemon",
                  f"port {DAEMON_PORT} free (daemon not running — optional)")


def run_checks() -> list:
    checks = []
    checks.extend(_binary_checks())
    checks.extend(_home_checks())
    checks.extend(_hook_config_checks())
    checks.extend(_payload_checks())
    checks.extend(_locator_checks())
    checks.extend(_persisted_python_check())
    checks.extend(_observed_checks())
    checks.extend(_session_store_checks())
    checks.append(_daemon_check())
    return checks


# ---------------------------------------------------------------------------
# --probe: replay documented payloads through the installed command
# ---------------------------------------------------------------------------


def _probe_payloads() -> dict:
    return {
        "SessionStart": {
            "hookEventName": "session_start",
            "hook_event_name": "SessionStart",
            "sessionId": "to-probe-session",
            "cwd": "/tmp/token-optimizer-probe",
            "workspaceRoot": "/tmp/token-optimizer-probe",
        },
        "UserPromptSubmit": {
            "hookEventName": "user_prompt_submit",
            "hook_event_name": "UserPromptSubmit",
            "sessionId": "to-probe-session",
            "cwd": "/tmp/token-optimizer-probe",
            "workspaceRoot": "/tmp/token-optimizer-probe",
            "promptId": "to-probe-prompt",
        },
        "PreToolUse": {
            "hookEventName": "pre_tool_use",
            "hook_event_name": "PreToolUse",
            "sessionId": "to-probe-session",
            "cwd": "/tmp/token-optimizer-probe",
            "workspaceRoot": "/tmp/token-optimizer-probe",
            "toolName": "run_terminal_command",
            "toolInput": {"command": "echo token-optimizer-probe"},
        },
        "PostToolUse": {
            "hookEventName": "post_tool_use",
            "hook_event_name": "PostToolUse",
            "sessionId": "to-probe-session",
            "cwd": "/tmp/token-optimizer-probe",
            "workspaceRoot": "/tmp/token-optimizer-probe",
            "toolName": "run_terminal_command",
            "toolInput": {"command": "echo token-optimizer-probe"},
        },
        "Stop": {
            "hookEventName": "stop",
            "hook_event_name": "Stop",
            "sessionId": "to-probe-session",
            "cwd": "/tmp/token-optimizer-probe",
            "workspaceRoot": "/tmp/token-optimizer-probe",
            "reason": "end_turn",
        },
    }


def _installed_commands() -> dict:
    hook_path = grok_home() / "hooks" / "token-optimizer.json"
    if not hook_path.exists():
        return {}
    try:
        config = json.loads(hook_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    # Identity, not substring: only entries whose bridge token is a bridge we
    # own qualify, so a corrupted entry pointing at /tmp/evil/grok_hook_bridge.py
    # is never replayed by --probe.
    ours = {
        str(grok_home() / "token-optimizer" / "plugin" / "grok_hook_bridge.py"),
        str(_SCRIPT_DIR / "grok_hook_bridge.py"),
    }
    out = {}
    by_event = {event: [c for e, c in _iter_commands(config) if e == event]
                for event in _WIRED_EVENTS}
    for event in _WIRED_EVENTS:
        for cmd in by_event[event]:
            argv = _parse_hook_command(cmd)
            if argv and argv[1] in ours:
                out[event] = cmd
                break
    return out


def _parse_hook_command(command: str) -> list | None:
    """Parse a persisted hook command into argv, or None if malformed.

    The installer writes exactly ``TOKEN_OPTIMIZER_RUNTIME=grok <abs-py>
    <abs-bridge> <Event>``. Anything else is rejected rather than executed:
    the hooks file is user-writable, and --probe must never become an injection
    vector for a corrupted or malicious entry.
    """
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return None
    if len(tokens) != 4:
        return None
    runtime, py, bridge, event = tokens
    if runtime != "TOKEN_OPTIMIZER_RUNTIME=grok":
        return None
    if not os.path.isabs(py) or not os.path.isabs(bridge):
        return None
    if event not in _WIRED_EVENTS:
        return None
    return tokens[1:]


def _run_probe_command(command: str, payload: dict, probe_home: Path) -> dict:
    """Run one installed command (no shell) with the payload on stdin.

    The command is shlex-parsed and shape-validated first (see
    _parse_hook_command); a malformed entry is reported, never executed.
    ``probe_home`` redirects the bridge's data writes (tallies, observed-events)
    to a throwaway dir so replaying the documented payloads proves the hooks can
    fire without contaminating real session data with synthetic probe rows.
    """
    if sys.platform == "win32":
        return {"status": "skip", "detail": "probe is POSIX-only"}
    argv = _parse_hook_command(command)
    if argv is None:
        return {"status": "fail",
                "detail": "hook command is not the expected "
                          "TOKEN_OPTIMIZER_RUNTIME=grok <abs-python> <abs-bridge> <event> shape; refusing to run it"}
    # Trust gate (same rule as the Cursor doctor): a tampered hooks file that keeps
    # a legitimate bridge path but points at an untrusted interpreter must NOT
    # be executed by --probe. The installer persists a realpath-resolved,
    # admin-owned interpreter; this check enforces that invariant at probe time.
    if not _py_path_is_trusted(argv[0]):
        return {"status": "fail",
                "detail": f"persisted python {argv[0]} is not trusted "
                          f"(ownership/writability); refusing to execute it"}
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "TOKEN_OPTIMIZER_PROBE": "1",
        "TOKEN_OPTIMIZER_GROK_HOME": str(probe_home),
    })
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"status": "fail", "detail": "timed out after 5s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "fail", "detail": str(exc)}
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def run_probe() -> list:
    results = []
    commands = _installed_commands()
    probe_home = Path(tempfile.mkdtemp(prefix=".grok-to-probe-", dir=Path.home()))
    try:
        for event, payload in _probe_payloads().items():
            command = commands.get(event)
            if not command:
                results.append({"event": event, "status": "skip",
                                "detail": "no grok hook entry installed for this event"})
                continue
            row = _run_probe_command(command, payload, probe_home)
            row["event"] = event
            results.append(row)
    finally:
        shutil.rmtree(probe_home, ignore_errors=True)
    return results


_BADGES = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_text(checks: list) -> None:
    print("Token Optimizer — Grok Build doctor")
    print()
    for c in checks:
        print(f"  {_BADGES.get(c['status'], '[?]   ')} {c['name']}: {c['detail']}")
        if c.get("hint"):
            print(f"         fix: {c['hint']}")
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    print()
    print(f"  {len(checks)} checks — {fails} fail, {warns} warn")


def _print_probe(results: list) -> None:
    print("Grok Build hook probe (replaying documented payloads through the installed commands)")
    print()
    for r in results:
        event = r.get("event", "?")
        status = r.get("status", "?")
        if status == "ok":
            print(f"  [OK]   {event}: exit 0" + (f" — {r['stdout']}" if r.get("stdout") else ""))
        elif status == "skip":
            print(f"  [SKIP] {event}: {r.get('detail', '')}")
        else:
            print(f"  [FAIL] {event}: {r.get('detail') or r.get('returncode')}")
            if r.get("stderr"):
                print(f"         stderr: {r['stderr']}")
    ran = [r for r in results if r.get("status") == "ok"]
    print()
    print(f"  {len(ran)}/{len(results)} wired events fired (exit 0)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--probe", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.probe:
        results = run_probe()
        if args.json:
            print(json.dumps(results, indent=1))
        else:
            _print_probe(results)
        failed = [r for r in results if r.get("status") == "fail"]
        return 1 if failed else 0

    checks = run_checks()
    if args.json:
        print(json.dumps(checks, indent=1))
    else:
        _print_text(checks)
    return 1 if any(c["status"] == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
