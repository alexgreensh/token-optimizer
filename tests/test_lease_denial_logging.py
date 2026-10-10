"""Lease denial must be logged, not completely silent.

_write_settings_atomic returns False on two paths:
1. Guard refusal (would drop a top-level key) -- LOUD (prints to stderr)
2. Lease denial (advisory lock not acquired within 75ms) -- SILENT

The silence is a discoverability problem: when a settings write fails, the
caller reports "refused" but there's no way to tell if it was a guard refusal
(serious, means the payload was destructive) or a lease denial (transient,
just contention). The guard refusal leaves a stderr message and a
last_refusal breadcrumb; the lease denial leaves nothing.

This test verifies that a lease denial writes a durable breadcrumb to the
daemon log dir so it is discoverable post-hoc, and sets last_refusal so the
existing reporting infrastructure can distinguish the two paths.

Run: python3 -m pytest tests/test_lease_denial_logging.py -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

USER_SETTINGS = {
    "model": "opus",
    "theme": "dark",
    "env": {"MY_KEY": "keep-me"},
}


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "claude"
    home.mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"
    settings.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(mod, "CLAUDE_DIR", home)
    yield mod, settings, tmp_path / "data"
    sys.modules.pop("measure", None)


def test_lease_denial_is_logged_to_durable_file(measure, monkeypatch):
    """When the settings lease is denied, a breadcrumb must be written to
    the daemon log dir so the denial is discoverable post-hoc.
    """
    mod, settings, data_dir = measure

    class _Denied:
        def __enter__(self):
            return False

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_settings_lock", lambda **kw: _Denied())

    result = mod._write_settings_atomic(dict(USER_SETTINGS, a=1))
    assert result is False, "lease denial must return False"

    # The log file must exist and contain a lease denial entry
    log_path = mod.DAEMON_LOG_DIR / "settings-lease-denials.log"
    assert log_path.exists(), (
        f"Lease denial log not written. Expected {log_path} to exist. "
        f"The lease denial was completely silent -- no durable breadcrumb."
    )
    log_text = log_path.read_text(encoding="utf-8")
    assert "lease" in log_text.lower() or "denied" in log_text.lower(), (
        f"Lease denial log entry doesn't mention lease/denied: {log_text!r}"
    )


def test_lease_denial_sets_last_refusal(measure, monkeypatch):
    """The lease denial must set _SETTINGS_WRITE_READ_STATE.last_refusal so
    the existing reporting infrastructure can distinguish lease denial from
    guard refusal.
    """
    mod, settings, data_dir = measure

    class _Denied:
        def __enter__(self):
            return False

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_settings_lock", lambda **kw: _Denied())

    # Clear any prior refusal
    if hasattr(mod._SETTINGS_WRITE_READ_STATE, "last_refusal"):
        del mod._SETTINGS_WRITE_READ_STATE.last_refusal

    result = mod._write_settings_atomic(dict(USER_SETTINGS, a=1))
    assert result is False

    last_refusal = getattr(mod._SETTINGS_WRITE_READ_STATE, "last_refusal", None)
    assert last_refusal is not None, (
        "last_refusal was not set on lease denial. "
        "Callers cannot distinguish lease denial from guard refusal."
    )
    assert "lease" in last_refusal.lower() or "denied" in last_refusal.lower(), (
        f"last_refusal doesn't mention lease/denied: {last_refusal!r}"
    )


def test_guard_refusal_does_not_log_lease_denial(measure, monkeypatch):
    """A guard refusal (not a lease denial) must NOT write to the lease
    denials log. The two failure paths must be distinguishable in the logs.
    """
    mod, settings, data_dir = measure

    # Ensure the lease IS acquired (real lock), but the payload drops a key
    # so the guard refuses. Write a payload that drops "model".
    result = mod._write_settings_atomic({"theme": "dark"})
    assert result is False, "guard should refuse a key-dropping payload"

    log_path = mod.DAEMON_LOG_DIR / "settings-lease-denials.log"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8")
        assert "lease" not in log_text.lower(), (
            f"Guard refusal was logged as a lease denial: {log_text!r}. "
            f"The two failure paths must be distinguishable."
        )
