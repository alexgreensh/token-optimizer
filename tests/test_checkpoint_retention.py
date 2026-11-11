"""Checkpoint retention is per session.

Every session keeps its newest checkpoints however busy another session is.
RETENTION_MAX is only a disk ceiling, and under it each session's newest
checkpoint is the last to go. Concurrent cleanups racing new saves never
leave a session with nothing.

Run: python3 -m pytest tests/test_checkpoint_retention.py -q
"""
import importlib
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"

BUSY = "1bb3093b-48ae-4cc5-a275-dd7bf31f2098"
QUIET = "3476702a-f8be-461b-ad56-40d4aa7b5437"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for key in ("TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX", "TOKEN_OPTIMIZER_CHECKPOINT_PER_SESSION",
                "TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS", "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(key, raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir()
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir)
    yield mod
    sys.modules.pop("measure", None)


def _save(m, sid, age_s, trigger="stop", sidecar=True):
    """A checkpoint file `age_s` seconds old, named the way compact_capture names it."""
    when = time.time() - age_s
    suffix = f"-{trigger}" if trigger != "auto" else ""
    n = 0
    while True:
        stamp = (datetime.fromtimestamp(when) - timedelta(seconds=n)).strftime("%Y%m%d-%H%M%S")
        path = m.CHECKPOINT_DIR / f"{sid}-{stamp}{suffix}.md"
        if not path.exists():
            break
        n += 1
    try:
        path.write_text("# Session State Checkpoint\n", encoding="utf-8")
        os.utime(path, (when, when))
        if sidecar:
            path.with_suffix(".json").write_text("{}", encoding="utf-8")
            os.utime(path.with_suffix(".json"), (when, when))
    except FileNotFoundError:
        pass  # a concurrent cleanup already removed it
    return path


def _sessions(m):
    return sorted({m._checkpoint_session(p.name) for p in m.CHECKPOINT_DIR.glob("*.md")})


def test_a_busy_session_cannot_erase_a_quiet_one(m):
    # The field case: one session saving on every stop, another idle for hours.
    quiet = _save(m, QUIET, 6 * 3600, trigger="quality-80")
    for i in range(60):
        _save(m, BUSY, 60 + i * 30)
    m._cleanup_checkpoints()
    assert quiet.exists(), "the quiet session's only checkpoint was removed"
    assert _sessions(m) == sorted([BUSY, QUIET])
    busy = [p for p in m.CHECKPOINT_DIR.glob("*.md") if p.name.startswith(BUSY)]
    assert len(busy) == m._CHECKPOINT_PER_SESSION


def test_restore_preferred_trigger_survives_a_run_of_stop_saves(m):
    rich = _save(m, BUSY, 3600, trigger="quality-80")
    for i in range(20):
        _save(m, BUSY, 60 + i * 30)
    m._cleanup_checkpoints()
    assert rich.exists(), "a flood of stop saves pushed out the quality checkpoint restore prefers"


def test_pruned_checkpoint_takes_its_sidecar(m):
    for i in range(m._CHECKPOINT_PER_SESSION + 3):
        _save(m, BUSY, 60 + i * 30)
    m._cleanup_checkpoints()
    mds = {p.stem for p in m.CHECKPOINT_DIR.glob("*.md")}
    jsons = {p.stem for p in m.CHECKPOINT_DIR.glob("*.json")}
    assert mds == jsons


def test_age_window_still_applies(m):
    old = _save(m, QUIET, 8 * 86400)
    fresh = _save(m, BUSY, 60)
    m._cleanup_checkpoints()
    assert not old.exists() and fresh.exists()


def test_ceiling_drops_extras_before_any_sessions_newest(m):
    now = datetime.now()
    cps = []
    for s in range(4):
        for i in range(3):
            cps.append({"path": None, "filename": f"{s:08d}-aaaa-20261004-0{s}{i}000.md",
                        "created": now - timedelta(minutes=10 * s + i), "trigger": "auto"})
    cps.sort(key=lambda c: c["created"], reverse=True)
    prune = m._checkpoints_to_prune(cps, now=now, days=7, total_max=4, per_session=5)
    kept = [c for c in cps if c not in prune]
    assert len(kept) == 4
    assert {m._checkpoint_session(c["filename"]) for c in kept} == {f"{s:08d}-aaaa" for s in range(4)}


def test_ceiling_below_session_count_keeps_each_sessions_newest(m):
    now = datetime.now()
    cps = [{"path": None, "filename": f"{s:08d}-aaaa-20261004-120000.md",
            "created": now - timedelta(minutes=s), "trigger": "auto"} for s in range(5)]
    prune = m._checkpoints_to_prune(cps, now=now, days=7, total_max=2, per_session=5)
    assert not prune
    assert not m._checkpoints_to_prune(cps, now=now, days=7, total_max=0, per_session=0)


def test_session_ids_parse_from_every_name_shape(m):
    assert m._checkpoint_session(f"{BUSY}-20261004-091307-quality-80.md") == BUSY
    assert m._checkpoint_session(f"{BUSY}-20261004-091307.md") == BUSY
    assert m._checkpoint_session("unknown-20260713-195912-stop.md") == "unknown"
    assert m._checkpoint_session("codex_thread-abc123-20261004-091307-progressive-50.md") == "codex_thread-abc123"


def test_concurrent_cleanups_racing_new_saves_keep_every_session(m):
    sids = [f"{n:08d}-1111-2222-3333-444444444444" for n in range(6)]
    for sid in sids:
        _save(m, sid, 3600)
    errors = []
    stop = threading.Event()

    def writer(sid):
        try:
            age = 1000.0
            while not stop.is_set() and age > 1:
                _save(m, sid, age)
                age -= 7
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def cleaner():
        try:
            for _ in range(30):
                m._cleanup_checkpoints()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(s,)) for s in sids[:3]]
    threads += [threading.Thread(target=cleaner) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads[3:]:
        t.join(timeout=60)
    stop.set()
    for t in threads[:3]:
        t.join(timeout=60)
    assert not errors
    m._cleanup_checkpoints()
    assert _sessions(m) == sorted(sids), "a concurrent cleanup left a session with no checkpoint"
    for sid in sids:
        own = [p for p in m.CHECKPOINT_DIR.glob("*.md") if p.name.startswith(sid)]
        assert 1 <= len(own) <= m._CHECKPOINT_PER_SESSION
