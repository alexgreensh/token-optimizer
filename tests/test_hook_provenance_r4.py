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


# --- only a lock another process holds is waited on ------------------------------

_POSIX_LOCK = pytest.mark.skipif(sys.platform == "win32", reason="injects fcntl.flock errors")


def _flock_raising(code):
    import fcntl

    def flock(fd, op):
        if op != fcntl.LOCK_UN:
            raise OSError(code, os.strerror(code))
    return flock


@_POSIX_LOCK
@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.ENOLCK, errno.EBADF])
def test_lock_error_that_is_not_busy_fails_at_once(hp, tmp_path, monkeypatch, code):
    import fcntl
    monkeypatch.setattr(fcntl, "flock", _flock_raising(code))
    t0 = time.monotonic()
    assert hp.record(tmp_path / "hook_provenance.json", "s.json", ["x"], "t") is False
    assert time.monotonic() - t0 < 0.5
    assert not (tmp_path / "hook_provenance.json").exists()


@_POSIX_LOCK
def test_busy_lock_is_still_waited_for(hp, tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(fcntl, "flock", _flock_raising(errno.EWOULDBLOCK))
    t0 = time.monotonic()
    assert hp.record(tmp_path / "hook_provenance.json", "s.json", ["x"], "t") is False
    assert time.monotonic() - t0 >= 1.9


# --- the lock context manager yields once, whatever the body raises ---------------

@pytest.mark.parametrize("shape", ["directory", "busy"])
def test_body_exception_propagates_as_itself_when_lock_not_taken(hp, tmp_path, monkeypatch, shape):
    lock = tmp_path / "l.flock"
    if shape == "directory":
        lock.mkdir()
    else:
        monkeypatch.setattr(hp, "_try_lock", lambda fd: False)
    with pytest.raises(OSError, match="boom"):
        with hp._exclusive(lock, timeout=0.05) as ok:
            assert ok is False
            raise OSError("boom")


def test_body_exception_propagates_and_releases_when_lock_taken(hp, tmp_path):
    lock = tmp_path / "l.flock"
    with pytest.raises(OSError, match="boom"):
        with hp._exclusive(lock) as ok:
            assert ok is True
            raise OSError("boom")
    with hp._exclusive(lock, timeout=0.05) as ok:
        assert ok is True
