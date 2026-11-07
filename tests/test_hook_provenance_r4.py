"""Hook provenance, round 4: the ledger, its lock, recording failures, the
fossil and smart-compact paths, Codex context ownership and diagnostics
echoes. Each test asserts the safe outcome and failed before its fix.

Fixtures come from tests/test_hook_provenance.py.

Run: python3 -m pytest tests/test_hook_provenance_r4.py -v
"""
from __future__ import annotations

import errno
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(REPO / "tests"))

from test_hook_provenance import hp, installed, m  # noqa: E402,F401


# --- a ledger from a newer version is never moved aside or overwritten ----------

def test_unknown_ledger_version_is_unreadable_and_kept_byte_for_byte(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    newer = json.dumps({"version": 2, "files": {}, "contexts": {"k": {"d": {}}}}).encode()
    ledger.write_bytes(newer)
    assert hp.load_ledger(ledger)[1] == "unreadable"
    assert hp.record(ledger, "s.json", ["x"], "t") is False
    assert hp.record_contexts(ledger, "s.json", [("Stop", "", "command", "x")], "t") is False
    assert ledger.read_bytes() == newer
    assert not [p for p in tmp_path.iterdir() if "corrupt" in p.name]


def test_versionless_or_garbage_ledger_is_still_corrupt(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    for raw in (b'{"files": {}}', b'{"version": "1", "files": {}}', b'{"version": false}', b"[1]", b"{nope"):
        ledger.write_bytes(raw)
        assert hp.load_ledger(ledger)[1] == "corrupt", raw
