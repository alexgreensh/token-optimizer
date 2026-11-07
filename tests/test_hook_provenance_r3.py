"""Hook provenance, round 3: regressions for the two adversarial reviews of
post/healer-provenance (Devin and Sol, on 91e49b49). Each test is the
reviewer's repro turned around: it asserts the safe outcome, so it failed
before the matching fix landed.

Fixtures come from tests/test_hook_provenance.py.

Run: python3 -m pytest tests/test_hook_provenance_r3.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(REPO / "tests"))

from test_hook_provenance import _commands, _malformed_shipped, hp, installed, m  # noqa: E402,F401

_POSIX_ONLY = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")


def _run(code, *args, timeout=20, env=None):
    """Run ``code`` in a fresh interpreter with the scripts dir importable.
    A hang is a failure, not a stuck suite."""
    try:
        return subprocess.run([sys.executable, "-c", code, str(SCRIPTS), *map(str, args)],
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, **(env or {})))
    except subprocess.TimeoutExpired as exc:
        pytest.fail("blocked for %ss: %s" % (timeout, exc))


# --- 1. Devin F1: a FIFO (or any blocking special file) at the ledger path ---

@_POSIX_ONLY
def test_fifo_ledger_load_is_unreadable_not_a_hang(tmp_path):
    fifo = tmp_path / "hook_provenance.json"
    os.mkfifo(str(fifo))
    out = _run("import sys; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
               "print(hp.load_ledger(sys.argv[2])[1])", fifo, timeout=10)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "unreadable"


@_POSIX_ONLY
def test_fifo_ledger_never_blocks_record_or_heal_view(tmp_path):
    """record() and finish() go through _update, which used to block inside
    the lock (convoying every other writer); Provenance() loads the ledger on
    every heal pass."""
    fifo = tmp_path / "hook_provenance.json"
    os.mkfifo(str(fifo))
    code = ("import sys, stat, os; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
            "led = sys.argv[2]\n"
            "assert hp.record(led, 'settings.json', ['echo $(x)'], 't') is False\n"
            "p = hp.Provenance(led, 'settings.json', [], [], False)\n"
            "assert p.status == 'unreadable'\n"
            "p.finish(['echo $(x)'], [('Stop', 'echo $(x)')])\n"
            "assert stat.S_ISFIFO(os.stat(led).st_mode)\n"
            "print('ok')\n")
    out = _run(code, fifo, timeout=15)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs /dev/zero")
def test_character_device_ledger_is_unreadable(tmp_path):
    link = tmp_path / "hook_provenance.json"
    link.symlink_to("/dev/zero")
    out = _run("import sys; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
               "print(hp.load_ledger(sys.argv[2])[1])", link, timeout=10)
    assert out.stdout.strip() == "unreadable", out.stderr


# --- 2. Sol P1-1: a live/runtime hooks.json never adds deletion authority ----

_REAL_LOOKUP = '''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
profile = Path(sys.argv[2])
other = profile / 'token-optimizer' / 'hooks' / 'hooks.json'
other.parent.mkdir(parents=True)
template = 'echo ${CLAUDE_PLUGIN_ROOT} $(printf NEVER_SHIPPED_FOREIGN)'
other.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': template}]}]}}))
import measure as m
import hook_provenance as hp
assert m._find_plugin_hooks_json() == other, m._find_plugin_hooks_json()
assert template not in hp.SHIPPED_HOOK_TEMPLATES
command = m._resolve_hook_command(template, Path(m.__file__).resolve().parents[3])
assert hp.load_ledger(hp.ledger_path_for(m))[1] == 'missing'
for event, matcher in (('PermissionRequest', 'FOREIGN_ONLY'), ('Stop', None)):
    group = {'hooks': [{'type': 'command', 'command': command}]}
    if matcher:
        group['matcher'] = matcher
    m.SETTINGS_PATH.write_text(json.dumps({'sentinel': True, 'hooks': {event: [group]}}))
    m._is_plugin_installed = lambda: True
    removed = m._fix_malformed_hook_commands()
    after = json.loads(m.SETTINGS_PATH.read_text())
    print(event, removed, command in json.dumps(after))
'''


def test_unshipped_live_hooks_json_command_is_never_removed(tmp_path):
    """Sol's real-lookup repro, no finder patch: a command that is only in the
    profile's runtime hooks.json (never shipped, never recorded) used to be
    proven and deleted. Only the frozen set and the ledger prove ownership;
    the live file may only keep the current command from churning."""
    profile = tmp_path / "profile"
    profile.mkdir()
    out = _run(_REAL_LOOKUP, profile, timeout=60,
               env={"CLAUDE_CONFIG_DIR": str(profile), "TOKEN_OPTIMIZER_RUNTIME": "claude",
                    "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap")})
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.split("\n")[:2] == ["PermissionRequest 0 True", "Stop 0 True"], out.stdout


def test_live_hooks_json_only_feeds_keep_current(m, hp, monkeypatch, tmp_path):
    live = tmp_path / "plug" / "hooks" / "hooks.json"
    live.parent.mkdir(parents=True)
    template = "echo ${CLAUDE_PLUGIN_ROOT} $(printf LIVE_ONLY)"
    live.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": template}]}]}}))
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: live)
    prov = hp.Provenance.from_facade(m)
    for root in (str(REPO), str(tmp_path / "plug")):
        resolved = m._resolve_hook_command(template, root)
        assert resolved not in prov.proven_templates
        assert prov.proof(resolved) is None
    assert m._resolve_hook_command(template, live.parent.parent) in prov.current_commands
