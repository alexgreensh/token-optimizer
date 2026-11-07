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


# --- a record that cannot land is logged and queued, never silently lost ----------

def _heal_actions(m, action):
    path = m._settings_heal_dir() / "heal_log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    return [e for e in map(json.loads, filter(None, lines)) if e.get("action") == action]


def _settings_contexts(hp, settings):
    return set(hp.iter_contexts(json.loads(Path(settings).read_text(encoding="utf-8"))))


def _missing_smart_compact_events(m, settings):
    status = m._is_smart_compact_installed(json.loads(Path(settings).read_text(encoding="utf-8")))
    return [event for event, present in status.items() if not present]


def test_smart_compact_record_lost_to_a_held_lock_lands_on_the_next_heal_pass(m, hp, installed, monkeypatch):
    installed.write_text(json.dumps({"_test_marker": True}))  # a script install, no hooks yet
    assert _missing_smart_compact_events(m, installed)
    before = _settings_contexts(hp, installed)
    real_acquire = hp._acquire
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)  # lock never available
    m.setup_smart_compact()
    added = _settings_contexts(hp, installed) - before
    assert added
    failed = _heal_actions(m, "hook_provenance_record_failed")
    assert [(e["source"], e["entries"], e["queued"]) for e in failed] == [
        ("setup_smart_compact", len(added), True)]
    prov = hp.Provenance.from_facade(m)
    assert not [c for c in added if prov.proof(c[3], c[:3])]
    monkeypatch.setattr(hp, "_acquire", real_acquire)
    m._fix_malformed_hook_commands()  # any later run: the heal pass merges the queue
    prov = hp.Provenance.from_facade(m)
    assert all(prov.proof(c[3], c[:3]) == "ledger" for c in added)
    assert not hp.has_pending(hp.ledger_path_for(m))


def test_queued_record_never_claims_a_preserved_identical_foreign_entry(m, hp, installed, monkeypatch):
    installed.write_text(json.dumps({"_test_marker": True, "hooks": {}}))
    missing = _missing_smart_compact_events(m, installed)
    assert len(missing) >= 2, missing
    event = missing[0]
    user_cmd = m._smart_compact_hook_commands()[event]
    data = json.loads(installed.read_text())
    data["hooks"].setdefault(event, []).append({"hooks": [{"type": "command", "command": user_cmd}]})
    installed.write_text(json.dumps(data))  # the user's own copy, there first
    before = _settings_contexts(hp, installed)
    real_acquire = hp._acquire
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)
    m.setup_smart_compact()  # adds the others, keeps the user's copy
    added = _settings_contexts(hp, installed) - before
    assert {c[0] for c in added} == set(missing[1:])
    failed = _heal_actions(m, "hook_provenance_record_failed")
    assert [(e["source"], e["entries"]) for e in failed] == [("setup_smart_compact", len(added))]
    monkeypatch.setattr(hp, "_acquire", real_acquire)
    assert hp.record_written(m, {}, {}, [], "noop")  # a later writer run flushes the queue
    assert not hp.has_pending(hp.ledger_path_for(m))
    prov = hp.Provenance.from_facade(m)
    assert all(prov.proof(c[3], c[:3]) == "ledger" for c in added)
    assert prov.proof(user_cmd, (event, "", "command")) is None


def test_record_that_cannot_even_queue_is_logged_as_lost(m, hp, installed, monkeypatch):
    ledger = hp.ledger_path_for(m)
    ledger.with_name(ledger.name + ".pending").write_text("not a directory")
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)
    m.setup_smart_compact()
    failed = _heal_actions(m, "hook_provenance_record_failed")
    assert [(e["source"], e["queued"]) for e in failed] == [("setup_smart_compact", False)]
    assert "never removed" in failed[0]["reason"]


def test_unreadable_ledger_queues_and_lands_once_readable(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    ledger.write_text(json.dumps({"version": 2}))  # another version: unreadable, kept
    context = ("Stop", "", "command", "x")
    assert hp.record_contexts(ledger, "s.json", [context], "t") is False
    assert hp.record_contexts(ledger, "s.json", [context], "t") is False
    assert len(list(hp._pending_dir(ledger).iterdir())) == 1  # a retry adds no second file
    ledger.unlink()
    assert hp.record(ledger, "s.json", ["other"], "t")
    data, status = hp.load_ledger(ledger)
    assert status == "ok" and hp.context_digest(context) in data["contexts"][hp.settings_key("s.json")]
    assert not hp.has_pending(ledger)


def test_cloud_record_failure_is_logged_in_the_profile_heal_log(hp, tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    after = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "cloud-cmd"}]}]}}
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)
    assert hp.record_cloud_write(settings, {}, after, after["hooks"]) is False
    log = tmp_path / "token-optimizer" / "settings_heal" / "heal_log.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["action"], e["source"], e["queued"]) for e in entries] == [
        ("hook_provenance_record_failed", "cloud_kit", True)]
