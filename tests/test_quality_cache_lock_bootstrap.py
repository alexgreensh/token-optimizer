#!/usr/bin/env python3
"""Quality-cache lease: bootstrap must be able to WIN the lock.

Symptom: context-quality (ContextQ) and session-efficiency freeze or never show
on long/busy sessions. Root cause (measured, not the disproved "parse >8s"):
`_acquire_quality_lock` used a fixed 75ms acquire timeout, but the real
recompute HOLDS the lease ~200ms on a large transcript (scales with size). A
refresh serving stale one tick is fine (the holder is writing a fresh value),
but BOOTSTRAP — which owns the first score and which PostToolUse refuses to do
(only UserPromptSubmit bootstraps) — could never win the lease under parallel-
agent contention, so the session stayed "ContextQ:--" forever.

Fix: bootstrap (no cache yet) waits a generous, env-overridable timeout to win;
refresh stays short. Plus a stderr breadcrumb so a lost lease is diagnosable
instead of silent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    # claude_home() honors CLAUDE_CONFIG_DIR only when the directory exists;
    # a missing dir is rejected and falls back to the host's real ~/.claude.
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def _session(tmp_path):
    tx = tmp_path / "sess.jsonl"
    tx.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n',
                  encoding="utf-8")
    return tx


def test_acquire_lock_accepts_and_uses_timeout(measure, tmp_path):
    """The lock helper takes a caller-tuned acquire_timeout (default preserved)."""
    lock = measure._acquire_quality_lock(tmp_path / "cache", acquire_timeout=0.5)
    assert lock is not False  # uncontended fresh path acquires
    measure._release_quality_lock(lock)


def test_bootstrap_waits_longer_than_refresh(measure, tmp_path, monkeypatch):
    """Bootstrap (no cache) requests the long timeout; refresh (cache present) the
    short one, and bootstrap > refresh."""
    tx = _session(tmp_path)
    cache = tmp_path / "quality-cache-sess.json"
    monkeypatch.setattr(measure, "_quality_cache_path_for", lambda fp: cache)
    seen = []

    def fake_acquire(cp, acquire_timeout=0.075):
        seen.append(acquire_timeout)
        return False  # force the early return right after the timeout is chosen

    monkeypatch.setattr(measure, "_acquire_quality_lock", fake_acquire)

    # BOOTSTRAP: cache absent
    measure.quality_cache(session_jsonl=str(tx), quiet=True, force=True)
    # REFRESH: cache now present
    cache.write_text('{"score":90}', encoding="utf-8")
    measure.quality_cache(session_jsonl=str(tx), quiet=True, force=True)

    assert seen[0] == 2.0, f"bootstrap timeout {seen[0]}"
    assert seen[1] == 0.15, f"refresh timeout {seen[1]}"
    assert seen[0] > seen[1]


def test_bootstrap_timeout_env_overridable(measure, tmp_path, monkeypatch):
    tx = _session(tmp_path)
    cache = tmp_path / "quality-cache-sess.json"
    monkeypatch.setattr(measure, "_quality_cache_path_for", lambda fp: cache)
    monkeypatch.setenv("TOKEN_OPTIMIZER_QUALITY_BOOTSTRAP_LOCK_TIMEOUT", "3.5")
    seen = []
    monkeypatch.setattr(
        measure, "_acquire_quality_lock",
        lambda cp, acquire_timeout=0.075: seen.append(acquire_timeout) or False,
    )
    measure.quality_cache(session_jsonl=str(tx), quiet=True, force=True)
    assert seen[0] == 3.5


def test_breadcrumb_on_lease_busy(measure, tmp_path, monkeypatch, capsys):
    """A busy lease leaves a stderr trace, distinct wording for bootstrap vs refresh."""
    tx = _session(tmp_path)
    cache = tmp_path / "quality-cache-sess.json"
    monkeypatch.setattr(measure, "_quality_cache_path_for", lambda fp: cache)
    monkeypatch.setattr(measure, "_acquire_quality_lock",
                        lambda cp, acquire_timeout=0.075: False)

    # bootstrap busy
    measure.quality_cache(session_jsonl=str(tx), quiet=True, force=True)
    err = capsys.readouterr().err
    assert "quality-cache: lease busy" in err
    assert "bootstrap skipped" in err

    # refresh busy
    cache.write_text('{"score":90}', encoding="utf-8")
    measure.quality_cache(session_jsonl=str(tx), quiet=True, force=True)
    err2 = capsys.readouterr().err
    assert "served stale score" in err2


def test_double_checked_read_skips_redundant_recompute(measure, tmp_path, monkeypatch):
    """Gauntlet MEDIUM (double-checked locking): if a bootstrap waiter acquires the
    lease AFTER the holder already wrote the cache, it must return that fresh score,
    not recompute the same thing ~200ms again."""
    tx = _session(tmp_path)
    cache = tmp_path / "quality-cache-sess.json"
    monkeypatch.setattr(measure, "_quality_cache_path_for", lambda fp: cache)

    class _FakeLock:
        def release(self):
            pass

    def fake_acquire(cp, acquire_timeout=0.15):
        # the holder wrote a fresh cache while we "waited" for the lease
        cp.write_text('{"score":77}', encoding="utf-8")
        return _FakeLock()

    monkeypatch.setattr(measure, "_acquire_quality_lock", fake_acquire)
    # recompute must NOT run: if it does, this raises and fails the test
    monkeypatch.setattr(
        measure, "_parse_jsonl_for_quality",
        lambda f: (_ for _ in ()).throw(AssertionError("recomputed despite fresh cache")),
    )
    # cache absent at entry -> _bootstrapping True; fake_acquire creates it -> re-read hits
    result = measure.quality_cache(session_jsonl=str(tx), force=True)
    assert result == 77
