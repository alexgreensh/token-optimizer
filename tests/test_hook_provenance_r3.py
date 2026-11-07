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
