#!/usr/bin/env python3
"""Token Optimizer — Cursor readiness doctor.

Per-surface health report with fix-it hints, plus ``--probe`` which replays
the documented payload for each wired event through the exact installed
command string (POSIX ``/bin/sh -c``) so a live Cursor is not required to prove
the hooks can fire.

Checks:
  P0  Cursor binary presence (CLI ``agent`` / ``cursor`` launcher)
  P0  ~/.cursor exists; hooks.json present + writable
  P1  Token Optimizer hook entries installed (six events) + parseable
  P1  installed payload integrity (bridge, atomic-writer, compress)
  P1  observed-events ledger (which events fired; rewrite_honoured vs ignored)
  P2  state.vscdb (IDE token plane) / transcript dir (CLI plane)
  P2  daemon port 24846 availability

Usage:
    python3 cursor_doctor.py [--json]
    python3 cursor_doctor.py --probe [--json]
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

from runtime_env import cursor_home, measure_cli as _measure_cli, with_measure_cli as _hint  # noqa: E402
from cursor_install import _py_path_is_trusted  # noqa: E402

DAEMON_PORT = 24846
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The six events the installer wires (order = probe order = docs order).
_WIRED_EVENTS = ("sessionStart", "preToolUse", "postToolUse", "preCompact", "stop", "sessionEnd")


def _check(status: str, name: str, detail: str, hint: str = "") -> dict:
    out = {"status": status, "name": name, "detail": detail}
    if hint:
        out["hint"] = hint
    return out


def _binary_checks() -> list:
    checks = []
    override = os.environ.get("TOKEN_OPTIMIZER_CURSOR_BIN", "").strip()
    candidates = [override] if override else []
    # `agent` is deliberately NOT PATH-scanned (too generic a binary name);
    # the documented CLI install path is checked directly instead.
    candidates.append(shutil.which("cursor") or "")
    candidates.append(str(Path.home() / ".local" / "bin" / "agent"))
    found = next((c for c in candidates if c and Path(c).is_file()), None)
    if found:
        checks.append(_check("ok", "cursor binary", found))
    else:
        checks.append(
            _check(
                "warn",
                "cursor binary",
                "No Cursor binary found (checked `cursor` on PATH and ~/.local/bin/agent).",
                "Install Cursor from https://cursor.com, or set TOKEN_OPTIMIZER_CURSOR_BIN.",
            )
        )
    return checks


def _home_checks() -> list:
    checks = []
    root = cursor_home()
    if not root.exists():
        checks.append(
            _check(
                "warn",
                "~/.cursor",
                f"{root} does not exist yet.",
                "Run Cursor once so it creates its home, then re-run install.",
            )
        )
        return checks
    checks.append(_check("ok", "~/.cursor", str(root)))

    hook_path = root / "hooks.json"
    if hook_path.exists() and not hook_path.is_file():
        checks.append(_check("fail", "hooks.json", f"{hook_path} exists but is not a file."))
    elif hook_path.exists() and not os.access(str(hook_path), os.W_OK):
        checks.append(_check("fail", "hooks.json", f"{hook_path} is not writable.", "Fix permissions (chmod u+w)."))
    elif hook_path.exists():
        checks.append(_check("ok", "hooks.json", f"{hook_path} (writable)"))
    else:
        checks.append(
            _check("warn", "hooks.json", f"{hook_path} missing (created on install).",
                   _hint("Run `python3 measure.py cursor-install`."))
        )
    return checks


def _str_entry_commands(config: dict) -> dict:
    """event -> [command strings], preserving only entries with a command."""
    hooks = config.get("hooks") if isinstance(config, dict) else None
    out: dict = {}
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        commands = []
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("command"), str):
                commands.append(e["command"])
        if commands:
            out[str(event)] = commands
    return out


def _hook_config_checks() -> list:
    checks = []
    hook_path = cursor_home() / "hooks.json"
    if not hook_path.exists():
        checks.append(_check("warn", "TO hook config", "Not installed.",
                             _hint("Run `python3 measure.py cursor-install`.")))
        return checks
    try:
        config = json.loads(hook_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        checks.append(
            _check("fail", "TO hook config", f"{hook_path} unreadable/invalid: {exc}",
                   _hint("Re-run `python3 measure.py cursor-install` to rewrite it."))
        )
        return checks

    commands = _str_entry_commands(config)
    ours = {
        event: [c for c in commands.get(event, []) if "cursor_hook_bridge.py" in c]
        for event in _WIRED_EVENTS
    }
    wired = [e for e in _WIRED_EVENTS if ours.get(e)]
    if wired:
        checks.append(_check("ok", "TO hook config",
                             f"{hook_path} (wired events: {', '.join(wired)})"))
    else:
        checks.append(
            _check("fail", "TO hook config", "No Token Optimizer cursor entries found.",
                   _hint("Run `python3 measure.py cursor-install`."))
        )
    missing = [e for e in _WIRED_EVENTS if not ours.get(e)]
    if missing and wired:
        checks.append(
            _check("warn", "hook event coverage", f"Missing events: {', '.join(missing)}.",
                   "Re-run install to restore the full six-event set.")
        )
    return checks


def _payload_checks() -> list:
    checks = []
    plugin_dir = cursor_home() / "token-optimizer" / "plugin"
    if not plugin_dir.is_dir():
        checks.append(_check("warn", "hook payload", f"{plugin_dir} missing.",
                             _hint("Run `python3 measure.py cursor-install`.")))
        return checks
    missing = [
        m for m in ("cursor_hook_bridge.py", "codex_io.py", "bash_compress.py")
        if not (plugin_dir / m).exists()
    ]
    if missing:
        checks.append(
            _check("fail", "hook payload",
                   f"Installed bridge is missing modules: {', '.join(missing)}.",
                   _hint("Re-run `python3 measure.py cursor-install` to refresh the payload."))
        )
    else:
        checks.append(_check("ok", "hook payload", f"{plugin_dir} (complete)"))
    return checks


def _locator_checks() -> list:
    """The bridge's detached rollup/dashboard spawns resolve measure.py via a
    one-line ``measure-path`` locator (measure.py is never copied into the
    plugin dir). Verify it exists and names a real file."""
    checks = []
    plugin_dir = cursor_home() / "token-optimizer" / "plugin"
    locator = plugin_dir / "measure-path"
    if not locator.exists():
        checks.append(
            _check(
                "warn",
                "measure-path locator",
                f"{locator} missing (rollups paused).",
                _hint("Run `python3 measure.py cursor-install` to rewrite it."),
            )
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
            _check(
                "fail",
                "measure-path locator",
                f"{locator} does not name an existing measure.py.",
                _hint("Run `python3 measure.py cursor-install` to rewrite it."),
            )
        )
    return checks


def _persisted_python_check() -> list:
    """The persisted hook command must use an absolute trusted interpreter,
    never a bare ``python3`` (PATH-hijack risk). Parse our command and verify."""
    checks = []
    commands = _installed_commands()
    if not commands:
        checks.append(
            _check("warn", "persisted python", "No wired hook command to inspect.",
                   _hint("Run `python3 measure.py cursor-install`."))
        )
        return checks
    for event, cmd in commands.items():
        # Same strict shape the probe enforces: runtime pin, absolute python,
        # absolute bridge, wired event.
        argv = _parse_hook_command(cmd)
        if argv and Path(argv[0]).is_file():
            checks.append(_check("ok", "persisted python", argv[0]))
            return checks
    checks.append(
        _check(
            "fail",
            "persisted python",
            "The wired hook command does not use an absolute python path.",
            _hint("Run `python3 measure.py cursor-install` to re-persist a trusted interpreter."),
        )
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
    path = cursor_home() / "token-optimizer" / "observed-events.jsonl"
    entries = _read_observed(path)
    if not entries:
        checks.append(
            _check("warn", "observed events", f"{path} empty or missing.",
                   _hint("Run `python3 measure.py cursor-doctor --probe` to prove hooks fire."))
        )
        return checks
    counts: dict = {}
    honoured = 0
    ignored = 0
    for e in entries:
        event = str(e.get("event") or "?")
        counts[event] = counts.get(event, 0) + 1
        rewrite = e.get("rewrite")
        if rewrite == "honoured":
            honoured += 1
        elif rewrite == "ignored":
            ignored += 1
    summary = ", ".join(f"{k} ({v})" for k, v in sorted(counts.items()))
    detail = f"{len(entries)} event(s): {summary}"
    if honoured or ignored:
        detail += f"; Shell rewrite: {honoured} honoured, {ignored} ignored"
    checks.append(_check("ok", "observed events", detail))
    return checks


def _data_checks() -> list:
    checks = []
    try:
        import cursor_state

        vscdb = cursor_state.state_vscdb_path()
    except Exception as exc:
        vscdb = None
        checks.append(_check("warn", "IDE token plane", f"reader unavailable: {exc}"))
    if vscdb is not None:
        if vscdb.exists():
            checks.append(_check("ok", "IDE token plane", str(vscdb)))
        else:
            checks.append(
                _check("warn", "IDE token plane", f"{vscdb} missing (no IDE chats yet).")
            )

    projects = cursor_home() / "projects"
    if projects.is_dir():
        checks.append(_check("ok", "CLI transcript plane", str(projects)))
    else:
        checks.append(
            _check("warn", "CLI transcript plane", f"{projects} missing (no CLI chats yet).")
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
    checks.extend(_payload_checks())
    checks.extend(_locator_checks())
    checks.extend(_persisted_python_check())
    checks.extend(_observed_checks())
    checks.extend(_data_checks())
    checks.append(_daemon_check())
    return checks


# ---------------------------------------------------------------------------
# --probe: replay documented payloads through the installed command
# ---------------------------------------------------------------------------


def _probe_payloads() -> dict:
    cv = os.environ.get("CURSOR_VERSION", "").strip()
    return {
        "sessionStart": {
            "hook_event_name": "sessionStart",
            "session_id": "to-probe-session",
            "workspace_roots": ["/tmp/token-optimizer-probe"],
            "cursor_version": cv or "3.18.9",
        },
        "preToolUse": {
            "hook_event_name": "preToolUse",
            "tool_name": "Shell",
            "tool_input": {"command": "echo token-optimizer-probe",
                           "working_directory": "/tmp/token-optimizer-probe"},
            "conversation_id": "to-probe-conv",
            "cursor_version": cv or "3.18.9",
            "workspace_roots": ["/tmp/token-optimizer-probe"],
        },
        "postToolUse": {
            "hook_event_name": "postToolUse",
            "tool_name": "Shell",
            "tool_input": {"command": "echo token-optimizer-probe",
                           "working_directory": "/tmp/token-optimizer-probe"},
            "conversation_id": "to-probe-conv",
            "cursor_version": cv or "3.18.9",
            "workspace_roots": ["/tmp/token-optimizer-probe"],
        },
        "preCompact": {
            "hook_event_name": "preCompact",
            "trigger": "manual",
            "context_usage_percent": 0.8,
            "context_tokens": 100_000,
            "context_window_size": 128_000,
            "conversation_id": "to-probe-conv",
            "cursor_version": cv or "3.18.9",
        },
        "stop": {
            "hook_event_name": "stop",
            "conversation_id": "to-probe-conv",
            "cursor_version": cv or "3.18.9",
        },
        "sessionEnd": {
            "hook_event_name": "sessionEnd",
            "conversation_id": "to-probe-conv",
            "reason": "probe",
            "cursor_version": cv or "3.18.9",
        },
    }


def _installed_commands() -> dict:
    hook_path = cursor_home() / "hooks.json"
    if not hook_path.exists():
        return {}
    try:
        config = json.loads(hook_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    commands = _str_entry_commands(config)
    # Identity, not substring: only entries whose bridge token is a bridge we
    # own (the installed plugin copy, or the sibling checkout bridge in dev)
    # qualify, so a corrupted entry pointing at /tmp/evil/cursor_hook_bridge.py
    # is never replayed by --probe.
    ours = {
        str(cursor_home() / "token-optimizer" / "plugin" / "cursor_hook_bridge.py"),
        str(_SCRIPT_DIR / "cursor_hook_bridge.py"),
    }
    out = {}
    for event in _WIRED_EVENTS:
        for cmd in commands.get(event, []):
            argv = _parse_hook_command(cmd)
            if argv and argv[1] in ours:
                out[event] = cmd
                break
    return out


def _parse_hook_command(command: str) -> list | None:
    """Parse a persisted hooks.json command into argv, or None if malformed.

    The installer writes exactly ``TOKEN_OPTIMIZER_RUNTIME=cursor <abs-py>
    <abs-bridge> <event>``. Anything else (extra tokens, relative paths, an
    unknown event, shell metacharacters that survive shlex) is rejected rather
    than executed: hooks.json is shared with other tools, and --probe must never
    become an injection vector for a corrupted or malicious entry.
    """
    try:
        # posix=False on Windows: shlex's POSIX mode eats backslashes, so
        # C:\Python314\python.exe would parse as "C:Python314python.exe".
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return None
    if len(tokens) != 4:
        return None
    runtime, py, bridge, event = tokens
    if runtime != "TOKEN_OPTIMIZER_RUNTIME=cursor":
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
        return {"event": None, "status": "skip", "detail": "probe is POSIX-only"}
    argv = _parse_hook_command(command)
    if argv is None:
        return {"event": None, "status": "fail",
                "detail": "hook command is not the expected "
                          "TOKEN_OPTIMIZER_RUNTIME=cursor <abs-python> <abs-bridge> <event> shape; refusing to run it"}
    # B2-P2-1: validate the persisted python interpreter through the same trust
    # gate the installer uses. A corrupted/tampered hooks.json could point at an
    # untrusted interpreter even with a legitimate bridge path; the probe must
    # never execute an untrusted binary.
    if not _py_path_is_trusted(argv[0]):
        return {"event": None, "status": "fail",
                "detail": f"persisted python {argv[0]} is not trusted (ownership/writability); refusing to execute it"}
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "TOKEN_OPTIMIZER_PROBE": "1",
        "TOKEN_OPTIMIZER_CURSOR_HOME": str(probe_home),
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
    # Throwaway data home (under $HOME so runtime_env's strict guard accepts it)
    # so probe replays never write synthetic tallies/observed-events into the
    # real ~/.cursor/token-optimizer data dir.
    probe_home = Path(tempfile.mkdtemp(prefix=".cursor-to-probe-", dir=Path.home()))
    try:
        for event, payload in _probe_payloads().items():
            command = commands.get(event)
            if not command:
                results.append({"event": event, "status": "skip",
                                "detail": "no cursor hook entry installed for this event"})
                continue
            row = _run_probe_command(command, payload, probe_home)
            row["event"] = event
            results.append(row)
    finally:
        shutil.rmtree(probe_home, ignore_errors=True)
    return results


_BADGES = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_text(checks: list) -> None:
    print("Token Optimizer — Cursor doctor")
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
    print("Cursor hook probe (replaying documented payloads through the installed commands)")
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
        ran = [r for r in results if r.get("status") == "ok"]
        failed = [r for r in results if r.get("status") == "fail"]
        # Skips are informational (not installed / Windows), not failures.
        return 1 if failed else 0

    checks = run_checks()
    if args.json:
        print(json.dumps(checks, indent=1))
    else:
        _print_text(checks)
    return 1 if any(c["status"] == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
