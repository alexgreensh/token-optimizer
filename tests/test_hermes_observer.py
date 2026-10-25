"""Contract tests for the standalone, read-only Hermes observer."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "hermes-observer"
OBSERVER = PACKAGE / "observer.py"
DOCTOR = PACKAGE / "doctor.py"


def _load_observer():
    spec = importlib.util.spec_from_file_location("standalone_hermes_observer", OBSERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_db(home: Path, rows: list[tuple] | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            model TEXT,
            started_at REAL,
            ended_at REAL,
            message_count INTEGER,
            tool_call_count INTEGER,
            api_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            title TEXT,
            cwd TEXT
        )
        """
    )
    for row in rows or []:
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()
    return db


def _row(
    session_id: str,
    started_at: object,
    *,
    ended_at: float | None = None,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 5,
    cache_write: int = 3,
    reasoning: int = 2,
    estimated_cost: float | None = 0.01,
    actual_cost: float | None = None,
    title: str = "TOP SECRET PROMPT",
    cwd: str = "/private/customer/project",
) -> tuple:
    return (
        session_id,
        "private/model-name",
        started_at,
        ended_at,
        4,
        3,
        2,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        reasoning,
        estimated_cost,
        actual_cost,
        title,
        cwd,
    )


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )


def _snapshot(root: Path) -> dict[str, tuple]:
    result: dict[str, tuple] = {}
    for path in sorted([root, *root.rglob("*")], key=lambda p: str(p)):
        rel = "." if path == root else str(path.relative_to(root))
        st = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        result[rel] = (st.st_mode, st.st_size, st.st_mtime_ns, digest, path.is_symlink())
    return result


def test_aggregate_report_from_realistic_sqlite_fixture(tmp_path):
    home = tmp_path / "profile"
    day1 = datetime(2026, 1, 2, 10, tzinfo=timezone.utc).timestamp()
    day2 = datetime(2026, 1, 3, 11, tzinfo=timezone.utc).timestamp()
    _create_db(
        home,
        [
            _row("raw-session-a", day1, ended_at=day1 + 60, input_tokens=1000,
                 output_tokens=200, cache_read=50, cache_write=25, reasoning=10,
                 estimated_cost=0.10, actual_cost=0.08),
            _row("raw-session-b", day2, ended_at=day2 + 60, input_tokens=3000,
                 output_tokens=400, cache_read=100, cache_write=50, reasoning=20,
                 estimated_cost=0.20, actual_cost=None),
            _row("raw-session-c", day2, ended_at=None, input_tokens=500,
                 output_tokens=60, cache_read=10, cache_write=5, reasoning=3,
                 estimated_cost=None, actual_cost=None),
        ],
    )
    proc = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["sessions"] == {
        "active": 1, "api_calls": 6, "completed": 2, "messages": 12,
        "tool_calls": 9, "total": 3,
    }
    assert report["tokens"] == {
        "cache_read": 160, "cache_write": 80, "input": 4500,
        "output": 660, "reasoning": 33, "total": 5160,
    }
    assert report["cost_usd"] == {"actual": 0.08, "effective": 0.28, "estimated": 0.3}
    assert report["context"]["occupancy_available"] is False
    assert report["source"] == {"sessions_scanned": 3, "truncated": False}
    assert [bucket["date"] for bucket in report["daily"]] == ["2026-01-02", "2026-01-03"]
    assert [bucket["tokens"] for bucket in report["daily"]] == [1200, 3960]


def test_total_means_input_plus_output_and_keeps_other_counters_separate(tmp_path):
    home = tmp_path / "profile"
    _create_db(home, [_row("id", 1_700_000_000, input_tokens=11, output_tokens=7,
                           cache_read=101, cache_write=103, reasoning=107)])
    report = _load_observer().inspect_database(str(home))
    assert report["tokens"] == {
        "cache_read": 101,
        "cache_write": 103,
        "input": 11,
        "output": 7,
        "reasoning": 107,
        "total": 18,
    }
    assert report["daily"][0]["tokens"] == 18


def test_committed_wal_only_row_is_visible_while_writer_remains_open(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    db = home / "state.db"
    writer = sqlite3.connect(db)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, "
        "input_tokens INTEGER, output_tokens INTEGER)"
    )
    writer.commit()
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?)",
        ("wal-only-secret-id", 1_700_000_000, None, 777, 23),
    )
    writer.commit()
    try:
        assert (home / "state.db-wal").exists()
        proc = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "json")
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report["sessions"]["total"] == 1
        assert report["tokens"]["input"] == 777
    finally:
        writer.close()


def test_live_wal_checkpoint_can_grow_main_database_during_inspection(tmp_path):
    observer = _load_observer()
    home = tmp_path / "profile"
    db = _create_db(home)
    writer = sqlite3.connect(db)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_row(f"live-{index}", 1_700_000_000 + index) for index in range(500)],
    )
    writer.commit()
    size_before = db.stat().st_size
    checkpointed = False

    def checkpoint_during_inspection(stage, _db):
        nonlocal checkpointed
        if stage == "before_schema":
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            assert db.stat().st_size > size_before
            checkpointed = True

    try:
        report = observer.inspect_database(
            str(home), _identity_hook=checkpoint_during_inspection
        )
        assert checkpointed
        assert report["sessions"]["total"] == 500
    finally:
        writer.close()


def test_missing_symlink_nonregular_and_configured_oversize_are_rejected(tmp_path):
    observer = _load_observer()
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(observer.ObserverError, match="missing"):
        observer.validate_state_db(str(missing))

    external = tmp_path / "external.db"
    external.write_bytes(b"not sqlite")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "state.db").symlink_to(external)
    with pytest.raises(observer.ObserverError, match="symlink"):
        observer.validate_state_db(str(linked))

    nonregular = tmp_path / "nonregular"
    nonregular.mkdir()
    (nonregular / "state.db").mkdir()
    with pytest.raises(observer.ObserverError, match="regular"):
        observer.validate_state_db(str(nonregular))

    oversized = tmp_path / "oversized"
    db = _create_db(oversized)
    with pytest.raises(observer.ObserverError, match="size limit"):
        observer.validate_state_db(str(oversized), max_db_bytes=db.stat().st_size - 1)


def test_default_database_limit_accepts_realistic_size_and_rejects_above_limit(tmp_path):
    observer = _load_observer()
    realistic = tmp_path / "realistic"
    realistic_db = _create_db(realistic)
    with realistic_db.open("r+b") as stream:
        stream.truncate(487_473_152)
    assert observer.MAX_DB_BYTES >= realistic_db.stat().st_size
    assert observer.validate_state_db(str(realistic)) == realistic_db

    oversized = tmp_path / "over-default"
    oversized_db = _create_db(oversized)
    with oversized_db.open("r+b") as stream:
        stream.truncate(observer.MAX_DB_BYTES + 1)
    with pytest.raises(observer.ObserverError, match="size limit"):
        observer.validate_state_db(str(oversized))


@pytest.mark.parametrize("spelling", ["dot", "dotdot", "repeat", "trailing"])
def test_lexically_noncanonical_home_is_rejected_before_pathlib_normalizes_it(tmp_path, spelling):
    observer = _load_observer()
    home = tmp_path / "profile"
    _create_db(home)
    parent = str(home.parent)
    variants = {
        "dot": f"{parent}/./profile",
        "dotdot": f"{parent}/other/../profile",
        "repeat": f"{parent}//profile",
        "trailing": f"{home}/",
    }
    with pytest.raises(observer.ObserverError) as caught:
        observer.validate_state_db(variants[spelling])
    assert caught.value.code == "home_invalid"


def test_symlinked_home_and_symlinked_ancestor_are_rejected(tmp_path):
    observer = _load_observer()
    real_home = tmp_path / "real" / "profile"
    _create_db(real_home)
    alias = tmp_path / "alias"
    alias.symlink_to(real_home, target_is_directory=True)
    with pytest.raises(observer.ObserverError, match="symlink"):
        observer.validate_state_db(str(alias))

    real_parent = tmp_path / "real-parent"
    nested = real_parent / "profile"
    _create_db(nested)
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(observer.ObserverError, match="symlink"):
        observer.validate_state_db(str(parent_alias / "profile"))


def test_ancestor_replacement_during_open_fails_closed(tmp_path):
    observer = _load_observer()
    ancestor = tmp_path / "ancestor"
    home = ancestor / "profile"
    _create_db(home, [_row("never-report-this", 1_700_000_000)])

    def replace_ancestor(stage, _db):
        if stage == "after_connect":
            parked = tmp_path / "parked"
            ancestor.rename(parked)
            shutil.copytree(parked, ancestor)

    with pytest.raises(observer.ObserverError) as caught:
        observer.inspect_database(str(home), _identity_hook=replace_ancestor)
    assert caught.value.code == "source_changed"


def test_database_replacement_during_open_fails_closed(tmp_path):
    observer = _load_observer()
    home = tmp_path / "profile"
    db = _create_db(home, [_row("never-report-this", 1_700_000_000)])

    def replace_database(stage, _db):
        if stage == "after_connect":
            parked = home / "parked.db"
            db.rename(parked)
            shutil.copy2(parked, db)

    with pytest.raises(observer.ObserverError) as caught:
        observer.inspect_database(str(home), _identity_hook=replace_database)
    assert caught.value.code == "source_changed"


def test_database_replacement_after_initial_validation_fails_before_open(tmp_path, monkeypatch):
    observer = _load_observer()
    home = tmp_path / "profile"
    db = _create_db(home, [_row("never-open-this", 1_700_000_000)])
    connect_calls = []
    real_connect = observer._connect_read_only

    def record_connect(path):
        connect_calls.append(path)
        return real_connect(path)

    def replace_database(stage, _db):
        if stage == "after_validation":
            parked = home / "parked.db"
            db.rename(parked)
            shutil.copy2(parked, db)

    monkeypatch.setattr(observer, "_connect_read_only", record_connect)
    with pytest.raises(observer.ObserverError) as caught:
        observer.inspect_database(str(home), _identity_hook=replace_database)
    assert caught.value.code == "source_changed"
    assert connect_calls == []


def test_newest_rows_normalize_mixed_real_and_iso_timestamps(tmp_path, monkeypatch):
    observer = _load_observer()
    monkeypatch.setattr(observer, "MAX_SCAN_ROWS", 2)
    home = tmp_path / "profile"
    _create_db(home, [
        _row("numeric-newest", datetime(2026, 1, 3, tzinfo=timezone.utc).timestamp(), input_tokens=30),
        _row("iso-second", "2026-01-02T00:00:00Z", input_tokens=20),
        _row("lexically-dominant-but-old", "2000-01-01T00:00:00Z", input_tokens=900),
        _row("numeric-older", datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp(), input_tokens=10),
    ])
    report = observer.inspect_database(str(home))
    assert report["source"] == {"sessions_scanned": 2, "truncated": True}
    assert report["tokens"]["input"] == 50
    assert "numeric-newest" not in json.dumps(report)
    assert "iso-second" not in json.dumps(report)


def test_equal_normalized_times_use_id_as_deterministic_nonoutput_tie_breaker(tmp_path, monkeypatch):
    observer = _load_observer()
    monkeypatch.setattr(observer, "MAX_SCAN_ROWS", 1)
    home = tmp_path / "profile"
    instant = datetime(2026, 1, 3, tzinfo=timezone.utc)
    _create_db(home, [
        _row("a-private-id", instant.timestamp(), input_tokens=7),
        _row("b-private-id", instant.isoformat(), input_tokens=99),
    ])
    report = observer.inspect_database(str(home))
    assert report["tokens"]["input"] == 7
    rendered = json.dumps(report)
    assert "a-private-id" not in rendered and "b-private-id" not in rendered


def test_runtime_does_not_import_or_invoke_network_or_subprocess_apis():
    forbidden_imports = {"socket", "subprocess", "urllib", "http", "requests", "ftplib"}
    forbidden_calls = {"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnv", "spawnve"}
    for path in (OBSERVER, DOCTOR):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        assert not (imported & forbidden_imports), path
        assert not (calls & forbidden_calls), path
        assert "http://" not in source and "https://" not in source


def test_report_and_doctor_make_no_filesystem_writes(tmp_path):
    home = tmp_path / "profile"
    _create_db(home, [_row("secret-id", 1_700_000_000)])
    before = _snapshot(tmp_path)
    package_before = _snapshot(PACKAGE)
    report = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "json")
    doctor = _run(DOCTOR, "--hermes-home", str(home), "--format", "json")
    after = _snapshot(tmp_path)
    package_after = _snapshot(PACKAGE)
    assert report.returncode == 0, report.stderr
    assert doctor.returncode == 0, doctor.stderr
    assert after == before
    assert package_after == package_before


def test_json_and_text_are_deterministic_bounded_and_cap_daily_rows(tmp_path):
    home = tmp_path / "profile"
    base = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
    rows = [_row(f"secret-{i}", base + i * 86400, input_tokens=i + 1) for i in range(90)]
    _create_db(home, rows)
    json_a = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "json")
    json_b = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "json")
    text_a = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "text")
    text_b = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", "text")
    assert json_a.returncode == text_a.returncode == 0
    assert json_a.stdout == json_b.stdout
    assert text_a.stdout == text_b.stdout
    report = json.loads(json_a.stdout)
    observer = _load_observer()
    assert len(report["daily"]) == observer.MAX_DATE_BUCKETS
    assert len(json_a.stdout.encode()) <= observer.MAX_OUTPUT_BYTES
    assert len(text_a.stdout.encode()) <= observer.MAX_TEXT_BYTES


def test_output_never_contains_content_private_paths_models_or_raw_ids(tmp_path):
    home = tmp_path / "customer-secret-home"
    secret_id = "session-ultra-private-123"
    secret_title = "PROMPT: launch code word swordfish"
    secret_cwd = "/private/acme/merger"
    _create_db(home, [_row(secret_id, 1_700_000_000, title=secret_title, cwd=secret_cwd)])
    for fmt in ("json", "text"):
        proc = _run(OBSERVER, "report", "--hermes-home", str(home), "--format", fmt)
        assert proc.returncode == 0, proc.stderr
        for forbidden in (str(home), secret_id, secret_title, secret_cwd, "private/model-name", "swordfish"):
            assert forbidden not in proc.stdout
            assert forbidden not in proc.stderr


def test_doctor_success_and_failure_json_do_not_expose_paths(tmp_path):
    good = tmp_path / "good-secret-profile"
    _create_db(good, [_row("secret-id", 1_700_000_000)])
    success = _run(DOCTOR, "--hermes-home", str(good), "--format", "json")
    assert success.returncode == 0, success.stderr
    success_doc = json.loads(success.stdout)
    assert success_doc == {
        "checks": {"database": "ok", "home": "ok", "schema": "ok"},
        "ok": True,
        "schema_version": 1,
        "session_count": 1,
    }
    assert str(good) not in success.stdout

    bad = tmp_path / "bad-secret-profile"
    bad.mkdir()
    failure = _run(DOCTOR, "--hermes-home", str(bad), "--format", "json")
    assert failure.returncode != 0
    failure_doc = json.loads(failure.stdout)
    assert failure_doc["ok"] is False
    assert failure_doc["error"]["code"] == "database_missing"
    assert str(bad) not in failure.stdout + failure.stderr


def test_malformed_or_unsupported_schema_fails_clearly(tmp_path):
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "state.db").write_bytes(b"this is not sqlite")
    proc = _run(OBSERVER, "report", "--hermes-home", str(malformed), "--format", "json")
    assert proc.returncode != 0
    assert json.loads(proc.stdout)["error"]["code"] == "database_malformed"

    unsupported = tmp_path / "unsupported"
    unsupported.mkdir()
    conn = sqlite3.connect(unsupported / "state.db")
    conn.execute("CREATE TABLE something_else (secret TEXT)")
    conn.commit()
    conn.close()
    proc = _run(OBSERVER, "report", "--hermes-home", str(unsupported), "--format", "json")
    assert proc.returncode != 0
    assert json.loads(proc.stdout)["error"]["code"] == "unsupported_schema"


def test_manifest_has_exact_contract_and_hashes():
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version", "name", "entrypoints", "runtime_files", "network", "writes_by_default"
    }
    assert manifest["schema_version"] == 1
    assert manifest["name"] == "token-optimizer-observer"
    assert manifest["entrypoints"] == {"doctor": "doctor.py", "report": "observer.py"}
    assert manifest["network"] == "none"
    assert manifest["writes_by_default"] is False
    assert set(manifest["runtime_files"]) == {"doctor.py", "observer.py"}
    for name, expected in manifest["runtime_files"].items():
        actual = hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest()
        assert expected == actual
