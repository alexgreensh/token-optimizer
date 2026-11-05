"""Regression coverage for the documented local-data retention contract."""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module, measure_source as _split_measure_source

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


def _load_archive_result(monkeypatch: pytest.MonkeyPatch, snapshot_dir: Path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_HOURS", "24")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES", "2")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES", "1000000")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("archive_result", SCRIPTS / "archive_result.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "archive_result", module)
    _split_exec_module(module)
    return module


def test_quality_cache_default_matches_seven_day_docs():
    source = _split_measure_source(SCRIPTS / "measure.py")
    assert (
        '_int_env("TOKEN_OPTIMIZER_QUALITY_CACHE_RETENTION_DAYS", 7)'
        in source
    )


def test_archive_cleanup_uses_env_and_enforces_file_cap(monkeypatch, tmp_path):
    module = _load_archive_result(monkeypatch, tmp_path)
    root = tmp_path / "tool-archive"
    now = time.time()
    for index in range(3):
        session = root / f"session-{index}"
        session.mkdir(parents=True)
        (session / "result.json").write_bytes(b"x" * 10)
        os.utime(session, (now - (30 - index), now - (30 - index)))

    removed = module.cleanup_old_archives()

    assert removed == 1
    assert len(list(root.glob("*/*.json"))) == 2
    assert not (root / "session-0").exists()


def test_archive_cleanup_enforces_total_byte_cap(monkeypatch, tmp_path):
    module = _load_archive_result(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES", "100")
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES", "15")
    root = tmp_path / "tool-archive"
    now = time.time()
    for index in range(2):
        session = root / f"session-{index}"
        session.mkdir(parents=True)
        (session / "result.json").write_bytes(b"x" * 10)
        os.utime(session, (now - (30 - index), now - (30 - index)))

    module.cleanup_old_archives()

    remaining_bytes = sum(
        p.stat().st_size for p in root.glob("session-*/*") if p.is_file()
    )
    assert remaining_bytes <= 15
    assert not (root / "session-0").exists()


def test_archive_cleanup_enforces_caps_across_plugin_identities(monkeypatch, tmp_path):
    active = tmp_path / "token-optimizer-active" / "data"
    sibling = tmp_path / "token-optimizer-old" / "data"
    module = _load_archive_result(monkeypatch, active)
    monkeypatch.setattr(module, "snapshot_dir_candidates", lambda: [active, sibling])
    now = time.time()
    for index, snapshot in enumerate((sibling, active, active)):
        session = snapshot / "tool-archive" / f"session-{index}"
        session.mkdir(parents=True)
        (session / "result.json").write_bytes(b"x" * 10)
        os.utime(session, (now - (30 - index), now - (30 - index)))

    removed = module.cleanup_old_archives()

    assert removed == 1
    assert not (sibling / "tool-archive" / "session-0").exists()
    assert len(list(tmp_path.glob("*/data/tool-archive/*/*.json"))) == 2


def test_archive_cleanup_never_removes_an_active_session(monkeypatch, tmp_path):
    module = _load_archive_result(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES", "1")
    root = tmp_path / "tool-archive"
    active = root / "active-session"
    caller = root / "caller-session"
    for session in (active, caller):
        session.mkdir(parents=True)
        (session / "result.json").write_bytes(b"x" * 10)
    (active / ".active").write_text(str(os.getpid()), encoding="utf-8")
    now = time.time()
    os.utime(active, (now - 30, now - 30))
    os.utime(caller, (now - 10, now - 10))

    removed = module.cleanup_old_archives(skip_session_id="caller-session")

    assert removed == 0
    assert active.exists()
    assert caller.exists()


def test_archive_cleanup_never_removes_a_session_with_a_live_writer_lease(
    monkeypatch, tmp_path
):
    module = _load_archive_result(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES", "1")
    root = tmp_path / "tool-archive"
    live = root / "live-session"
    other = root / "other-session"
    for session in (live, other):
        session.mkdir(parents=True)
        (session / "result.json").write_bytes(b"x" * 10)
    old = time.time() - 90_000
    os.utime(live, (old, old))

    writer = module.LeaseLock(
        module._session_lock_path(root, "live-session"),
        acquire_timeout=0,
    )
    assert writer.acquire()
    try:
        removed_while_live = module.cleanup_old_archives(
            skip_session_id="other-session"
        )
    finally:
        writer.release()

    assert removed_while_live == 0
    assert live.exists()
    assert other.exists()

    removed_after_release = module.cleanup_old_archives(
        skip_session_id="other-session"
    )
    assert removed_after_release == 1
    assert not live.exists()
    assert other.exists()


def test_active_session_entries_obey_age_and_aggregate_caps(monkeypatch, tmp_path):
    module = _load_archive_result(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_FILES", "3")
    root = tmp_path / "tool-archive"
    active = root / "active-session"
    active.mkdir(parents=True)
    (active / ".active").touch()
    now = time.time()
    manifest_lines = []
    for index in range(4):
        entry = active / f"tool-{index}.json"
        entry.write_bytes(b"x" * 10)
        manifest_lines.append(f'{{"tool_use_id":"tool-{index}"}}')
        entry_time = now - 90_000 if index == 0 else now - (30 - index)
        os.utime(entry, (entry_time, entry_time))
    (active / "manifest.jsonl").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )

    removed = module.cleanup_old_archives(skip_session_id="active-session")

    remaining_files, _remaining_bytes = module._archive_tree_usage(active)
    assert removed == 0
    assert active.exists()
    assert not (active / "tool-0.json").exists()
    assert remaining_files <= 3
    retained_ids = {
        json.loads(line)["tool_use_id"]
        for line in (active / "manifest.jsonl").read_text().splitlines()
    }
    assert retained_ids == {path.stem for path in active.glob("*.json")}


def test_archive_hot_path_does_not_scan_tree_before_each_write(monkeypatch, tmp_path):
    module = _load_archive_result(monkeypatch, tmp_path)
    payloads = iter([
        {
            "session_id": "session-1",
            "tool_use_id": "tool-1",
            "tool_name": "mcp__example",
            "tool_response": "x" * 5000,
        },
        {
            "session_id": "session-1",
            "tool_use_id": "tool-2",
            "tool_name": "mcp__example",
            "tool_response": "y" * 5000,
        },
    ])
    monkeypatch.setattr(module, "read_stdin_hook_input", lambda _cap: next(payloads))
    monkeypatch.setattr(
        module,
        "_archive_tree_usage",
        lambda _path: pytest.fail("synchronous archive write scanned the archive tree"),
    )
    cleanup_calls = 0

    def cleanup(**_kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return 0

    monkeypatch.setattr(module, "cleanup_old_archives", cleanup)
    module.archive_result(quiet=True)
    module.archive_result(quiet=True)

    assert cleanup_calls == 1
    assert (tmp_path / "tool-archive" / "session-1" / "tool-1.json").exists()
    assert (tmp_path / "tool-archive" / "session-1" / "tool-2.json").exists()


def test_hot_path_and_session_end_both_run_configured_cleanup():
    archiver = (SCRIPTS / "archive_result.py").read_text(encoding="utf-8")
    measure = _split_measure_source(SCRIPTS / "measure.py")
    hot_path = archiver[archiver.index("def archive_result(") :]
    worker = measure[
        measure.index("def _run_session_end_flush_worker(") :
        measure.index("def _defer_session_end_flush(")
    ]

    assert "_cleanup_archives_if_due(session_id)" in hot_path
    assert "max_age_hours=48" not in hot_path
    assert "cleanup_old_archives()" in worker
