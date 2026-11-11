"""``quality_cache`` releases its lease lock even when the wall-clock
deadline fires mid-computation.

``_HookTimeout`` inherits from ``BaseException`` so inner ``except Exception``
blocks cannot swallow it. Before the fix, ``_release_quality_lock`` was called
only at explicit returns, NOT in a ``finally``; a ``_HookTimeout`` raised
between acquire and release skipped release entirely, leaking the per-nonce
candidate hard link too. The fix wraps the locked region in ``try/finally`` so
``_release_quality_lock`` (idempotent + guarded) runs on every exit, including
the ``BaseException`` path.

This test injects ``_HookTimeout`` at ``compute_quality_score`` (mid-compute,
after acquire, before any inline release) and asserts the candidate is unlinked
in the finally.

Run: python3 -m pytest tests/test_quality_cache_release_on_timeout.py -v
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="to-qcache-release-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _candidate_files(cache_dir: Path):
    return list(cache_dir.glob(".*.candidate-*"))


def test_quality_cache_releases_lock_on_hook_timeout(m, monkeypatch, tmp_path):
    cache_dir = tmp_path / "to-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", cache_dir)

    # A real session jsonl filepath so quality_cache resolves a per-session path.
    session = tmp_path / "sess-abc.jsonl"
    session.write_text(
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )

    # _parse_jsonl_for_quality returns truthy data so we pass the empty-session
    # branch and reach compute_quality_score (the mid-compute injection point).
    monkeypatch.setattr(
        m,
        "_parse_jsonl_for_quality",
        lambda fp: {"messages": [], "decisions": [], "compactions": 0},
    )

    # Inject the deadline firing mid-computation (after acquire, before release).
    def _boom(*a, **k):
        raise m._HookTimeout()

    monkeypatch.setattr(m, "compute_quality_score", _boom)

    # No inline release exists on this path pre-fix; the finally is the only
    # release. The BaseException propagates out of quality_cache.
    with pytest.raises(m._HookTimeout):
        m.quality_cache(
            throttle_seconds=120,
            quiet=True,
            session_jsonl=str(session),
            force=True,
        )

    # The per-nonce candidate hard link MUST be unlinked by the finally release.
    # Pre-fix this leaked forever (the candidate stayed on disk).
    candidates = _candidate_files(cache_dir)
    assert candidates == [], (
        f"lock candidate leaked on _HookTimeout: {[p.name for p in candidates]}"
    )

    # The canonical .qlease tombstone is left by release() on purpose (reclaim
    # protocol) -- but it MUST be marked released:1, proving release() ran.
    qleases = list(cache_dir.glob("*.qlease"))
    assert qleases, "expected the .qlease tombstone to exist after release"
    released_seen = False
    for q in qleases:
        try:
            meta = json.loads(q.read_text(encoding="utf-8"))
            if meta.get("released") in (1, True):
                released_seen = True
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    assert released_seen, (
        ".qlease tombstone was not marked released:1 -- release() did not run"
    )


def test_quality_cache_releases_lock_on_normal_return(m, monkeypatch, tmp_path):
    """Sanity: the finally release also covers the normal return path, so a
    successful run does not leak the candidate either."""
    cache_dir = tmp_path / "to-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", cache_dir)

    session = tmp_path / "sess-ok.jsonl"
    session.write_text(
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        m,
        "_parse_jsonl_for_quality",
        lambda fp: {},
    )
    # A parser failure is unmeasured, even when no prior score is available.

    score = m.quality_cache(
        throttle_seconds=120, quiet=True, session_jsonl=str(session), force=True
    )
    assert score is not None
    cached = json.loads(next(cache_dir.glob("quality-cache-*.json")).read_text())
    assert cached["measured"] is False
    assert cached["grade"] != "S"
    assert _candidate_files(cache_dir) == [], "candidate leaked on normal return"
