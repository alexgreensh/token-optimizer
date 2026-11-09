"""Cursor doctor: hook-config recognition, payload/observed checks, and --probe.

The probe is the live-firing substitute for a headless Cursor: it shape-parses
the installed command and replays the documented payload for every wired
event as argv (no shell; POSIX and Windows) and asserts exit 0. Everything
else is pinned without a live Cursor by monkeypatching the home dir.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_doctor as cd  # noqa: E402

_WIRED = cd._WIRED_EVENTS


@pytest.fixture()
def cur(tmp_path):
    d = tmp_path / ".cursor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_hooks(cur, *, include_ours=True):
    hooks = {}
    if include_ours:
        bridge = SCRIPTS / "cursor_hook_bridge.py"
        py = sys.executable
        for event in _WIRED:
            hooks[event] = [{
                "command": f"TOKEN_OPTIMIZER_RUNTIME=cursor {py} {bridge} {event}",
                "type": "command",
                "timeout": 10,
            }]
    (cur / "hooks.json").write_text(json.dumps({"version": 1, "hooks": hooks}), encoding="utf-8")


def test_hook_config_recognizes_wired_events(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    _write_hooks(cur)
    checks = cd._hook_config_checks()
    assert any(c["name"] == "TO hook config" and c["status"] == "ok" for c in checks)
    # no missing-event warning
    assert not any(c["name"] == "hook event coverage" for c in checks)


def test_hook_config_reports_missing_when_uninstalled(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    _write_hooks(cur, include_ours=False)
    checks = cd._hook_config_checks()
    assert any(c["name"] == "TO hook config" and c["status"] == "fail" for c in checks)


def test_payload_checks(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    plugin = cur / "token-optimizer" / "plugin"
    plugin.mkdir(parents=True)
    for name in ("cursor_hook_bridge.py", "codex_io.py", "bash_compress.py"):
        (plugin / name).write_text("# stub\n")
    checks = cd._payload_checks()
    assert any(c["name"] == "hook payload" and c["status"] == "ok" for c in checks)


def test_observed_checks_counts_rewrite_statuses(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    to_dir = cur / "token-optimizer"
    to_dir.mkdir(parents=True)
    (to_dir / "observed-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            {"event": "preToolUse", "rewrite": "attempted", "cursor_version": "3.18.9"},
            {"event": "postToolUse", "rewrite": "honoured", "cursor_version": "3.18.9"},
            {"event": "postToolUse", "rewrite": "ignored", "cursor_version": "3.18.9"},
        ]) + "\n",
        encoding="utf-8",
    )
    checks = cd._observed_checks()
    detail = next(c["detail"] for c in checks if c["name"] == "observed events")
    assert "1 honoured, 1 ignored" in detail


def test_run_checks_includes_daemon_and_data(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    _write_hooks(cur)
    names = {c["name"] for c in cd.run_checks()}
    assert "dashboard daemon" in names
    assert "IDE token plane" in names
    assert "CLI transcript plane" in names


@pytest.mark.skipif(sys.platform == "win32",
                    reason="fixture writes unquoted POSIX-shaped commands; breaks when paths contain spaces")
def test_probe_fires_all_installed_events(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cur = home / ".cursor"
    cur.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(cur))
    # The probe now validates the persisted python through the trust gate
    # (B2-P2-1). On hosted-CI runners sys.executable may be world-writable
    # (hostedtoolcache), which the gate correctly rejects. This test is about
    # probe execution, not trust validation, so bypass the gate.
    monkeypatch.setattr(cd, "_py_path_is_trusted", lambda p: True)
    _write_hooks(cur)

    results = cd.run_probe()

    assert [r["event"] for r in results] == list(_WIRED)
    assert all(r["status"] == "ok" for r in results), results
    # The probe genuinely ran the bridge, but its writes are isolated to a
    # throwaway cursor home: replaying documented payloads must prove the hooks
    # can fire WITHOUT contaminating real session data with synthetic rows.
    ledger = cur / "token-optimizer" / "observed-events.jsonl"
    assert not ledger.exists()
    assert not (cur / "token-optimizer" / "sessions").exists()


def test_parse_hook_command_accepts_installed_shape():
    # abspath normalizes per platform: on Windows a root-relative "/x/..." is
    # NOT absolute (ntpath.isabs is False without a drive since 3.13), so the
    # test must use a platform-native absolute bridge path.
    bridge = os.path.abspath("/x/cursor_hook_bridge.py")
    cmd = f"TOKEN_OPTIMIZER_RUNTIME=cursor {sys.executable} {bridge} stop"
    assert cd._parse_hook_command(cmd) == [sys.executable, bridge, "stop"]


@pytest.mark.parametrize("cmd", [
    # shell injection via metacharacters: shlex keeps it one token -> shape fails
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /tmp/cursor_hook_bridge.py preToolUse; echo INJECTED > /tmp/pwned",
    # extra token
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /x/bridge.py stop extra",
    # relative python
    "TOKEN_OPTIMIZER_RUNTIME=cursor python3 /x/bridge.py stop",
    # unknown event
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /x/bridge.py notAnEvent",
    # wrong runtime pin
    "TOKEN_OPTIMIZER_RUNTIME=codex /usr/bin/python3 /x/bridge.py stop",
    # unbalanced quote (shlex ValueError)
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /x/bridge.py 'stop",
])
def test_parse_hook_command_rejects_malformed(cmd):
    assert cd._parse_hook_command(cmd) is None


@pytest.mark.skipif(sys.platform == "win32",
                    reason="fixture writes unquoted POSIX-shaped commands; breaks when paths contain spaces")
def test_probe_refuses_to_execute_injected_command(monkeypatch, tmp_path):
    """P0-2: a corrupted/malicious hooks.json entry must be reported, never run."""
    home = tmp_path / "home"
    cur = home / ".cursor"
    cur.mkdir(parents=True)
    marker = tmp_path / "pwned"
    (cur / "hooks.json").write_text(json.dumps({"hooks": {
        "stop": [{"command":
                  f"TOKEN_OPTIMIZER_RUNTIME=cursor {sys.executable} /tmp/cursor_hook_bridge.py stop; echo INJECTED > {marker}"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)

    results = cd.run_probe()

    stop = next(r for r in results if r["event"] == "stop")
    # The entry is not ours (bridge path is /tmp/...), so it is never replayed:
    # reported as a failure or a skip, but never executed.
    assert stop["status"] in ("fail", "skip")
    assert not marker.exists()


def test_parse_hook_command_handles_platform_native_paths():
    """Windows: shlex in POSIX mode eats backslashes, so a C:\\... python path
    must parse via posix=False. Runs everywhere using the real sys.executable,
    which carries backslashes exactly on Windows."""
    bridge = str(Path(cd.__file__).parent / "cursor_hook_bridge.py")
    cmd = f"TOKEN_OPTIMIZER_RUNTIME=cursor {sys.executable} {bridge} stop"
    assert cd._parse_hook_command(cmd) == [sys.executable, bridge, "stop"]


def test_probe_command_not_skipped_on_windows(monkeypatch, tmp_path):
    """F13 regression: _run_probe_command must not return the POSIX-only skip
    on win32 — the installed Windows shape parses to argv and runs natively."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cd, "_py_path_is_trusted", lambda p: True)
    probe_home = home / ".cursor-to-probe-test"
    probe_home.mkdir()
    bridge = str(SCRIPTS / "cursor_hook_bridge.py")
    cmd = f"TOKEN_OPTIMIZER_RUNTIME=cursor {sys.executable} {bridge} stop"
    result = cd._run_probe_command(cmd, {"hook_event_name": "stop"}, probe_home)
    assert result.get("detail") != "probe is POSIX-only"
    assert result["status"] != "skip"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX trust-gate semantics; on Windows _py_path_is_trusted only requires a real file")
def test_probe_refuses_untrusted_python(monkeypatch, tmp_path):
    """B2-P2-1: a hooks.json entry with our bridge path but an untrusted python
    (world-writable) must be reported as fail, never executed."""
    home = tmp_path / "home"
    home.mkdir()
    cur = home / ".cursor"
    cur.mkdir(parents=True)
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    # Create an untrusted python: world-writable file in a world-writable dir
    bad_dir = tmp_path / "bad-bin"
    bad_dir.mkdir(mode=0o777)
    bad_py = bad_dir / "python3"
    bad_py.write_bytes(b"#!/bin/sh\n")
    os.chmod(bad_py, 0o777)
    bridge = str(SCRIPTS / "cursor_hook_bridge.py")
    (cur / "hooks.json").write_text(json.dumps({"hooks": {
        "stop": [{"command":
                  f"TOKEN_OPTIMIZER_RUNTIME=cursor {bad_py} {bridge} stop",
                  "type": "command", "timeout": 10}],
    }}), encoding="utf-8")
    # _installed_commands accepts it (bridge path matches ours), but the probe
    # must refuse to execute because the python is untrusted.
    results = cd.run_probe()
    stop = next(r for r in results if r["event"] == "stop")
    assert stop["status"] == "fail", f"expected fail, got {stop}"
    assert "not trusted" in stop["detail"], f"detail: {stop['detail']}"
