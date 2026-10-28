#!/usr/bin/env python3
"""Tests for the Cowork telemetry collector (cowork/collector/to_collector.py).

Covers the data-correctness fixes from the fable council review, all exercised
through the public `--db <tmp>` seam against a temp DB and synthetic OTLP
capture files. The real trends.db is never touched.
"""

import importlib.util
import io
import json
import os
import sqlite3
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
COLLECTOR = REPO / "cowork" / "collector" / "to_collector.py"
MEASURE = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

# Force a known non-UTC zone so the UTC-vs-local day-bucketing test is
# meaningful. Only while this module's tests run (and while LOCAL_DAY is
# computed at import, below): pytest imports every test module at collection,
# so a zone set at import and restored by a module fixture leaked into the
# whole run whenever this module was deselected (CI shards), and every later
# subprocess built with a fresh env then disagreed with the parent on "today".
_TEST_TZ = "America/Los_Angeles"
_ORIG_TZ = os.environ.get("TZ")


def _set_tz(value):
    if value is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = value
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.fixture(scope="module", autouse=True)
def _test_tz():
    _set_tz(_TEST_TZ)
    yield
    _set_tz(_ORIG_TZ)


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load("to_collector_under_test", COLLECTOR)
measure = _load("measure_under_test", MEASURE)

PRICED_MODEL = "claude-sonnet-4-20250514"


# --------------------------------------------------------------------------- #
# Synthetic OTLP capture builders (mirror what CollectorHandler writes).
# --------------------------------------------------------------------------- #

def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _log_record(session_id, event_name, ts_nano, **attrs):
    a = [_attr("event.name", event_name), _attr("session.id", session_id)]
    a.extend(_attr(k, v) for k, v in attrs.items())
    return {"timeUnixNano": str(ts_nano), "attributes": a}


def _api_record(session_id, ts_nano, model=PRICED_MODEL, input_tokens=1000,
                output_tokens=500, cache_read_tokens=200, cache_creation_tokens=100,
                **extra):
    return _log_record(
        session_id, "claude_code.api_request", ts_nano, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens, **extra,
    )


def _write_capture(data_dir: Path, log_records, name="otlp-logs.jsonl"):
    data_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": "2026-08-14T02:30:00Z",
        "path": "/v1/logs",
        "content_type": "application/json",
        "kind": "json",
        "body": {"resourceLogs": [{"scopeLogs": [{"logRecords": list(log_records)}]}]},
    }
    with (data_dir / name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _nanos(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


# A fixed UTC instant that is the previous local day in America/Los_Angeles:
# 2026-08-14 02:30 UTC == 2026-08-13 19:30 PDT.
EVENING_UTC = datetime(2026, 8, 14, 2, 30, tzinfo=timezone.utc)
EVENING_NANOS = _nanos(EVENING_UTC)
_set_tz(_TEST_TZ)
LOCAL_DAY = EVENING_UTC.astimezone().strftime("%Y-%m-%d")   # 2026-08-13
_set_tz(_ORIG_TZ)
UTC_DAY = EVENING_UTC.strftime("%Y-%m-%d")                  # 2026-08-14


def _ingest(data_dir, db):
    return tc.ingest(data_dir, measure_path=str(MEASURE), db_override=str(db), quiet=True)


def _rows(db, where="platform='cowork'"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            f"SELECT jsonl_path, date, input_tokens, output_tokens, cost_usd, "
            f"cost_source, quality_score, quality_grade, cache_create_5m_tokens, "
            f"session_uuid FROM session_log WHERE {where}"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_wellformed_ingest_lands_rows_and_derives_cost(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [
        _api_record("sess-alpha", EVENING_NANOS),
        _api_record("sess-alpha", EVENING_NANOS + 1_000_000),
    ])
    db = tmp_path / "trends.db"
    rc = _ingest(dd, db)
    assert rc == 0
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "cowork:sess-alpha"
    # two api_requests * (1000 fresh + 200 cache_read + 100 cache_create) input
    assert row[2] == 2 * (1000 + 200 + 100)
    assert row[3] == 2 * 500
    assert row[4] > 0            # cost derived from priced model
    assert row[5] == "otel_derived"
    # cache_create attributed to the 5m column, not hardcoded 0
    assert row[8] == 2 * 100


def test_help_and_costview_run_without_traceback(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-cv", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    # --cost-view path (also exercises the Path.as_uri() DB URI)
    assert tc.cost_view(measure_path=str(MEASURE), db_override=str(db),
                        days=0, as_json=True) == 0


def test_overflow_and_huge_int_row_is_skipped_not_fatal(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [
        _api_record("sess-good", EVENING_NANOS),
        # int(float("inf")) raises OverflowError; 2**64 dies at the SQLite bind.
        _api_record("sess-bad", EVENING_NANOS, input_tokens="inf",
                    cache_creation_tokens=str(2 ** 64)),
    ])
    db = tmp_path / "trends.db"
    rc = _ingest(dd, db)
    assert rc == 0                       # whole ingest did not abort
    paths = {r[0] for r in _rows(db)}
    assert "cowork:sess-good" in paths   # the good session still landed
    good = [r for r in _rows(db) if r[0] == "cowork:sess-good"][0]
    assert good[2] == 1000 + 200 + 100   # untouched by the bad row


def test_malformed_capture_does_not_block_later_valid_event(tmp_path):
    dd = tmp_path / "cap"
    dd.mkdir()
    capture = dd / "otlp-logs.jsonl"
    capture.write_text(json.dumps(["invalid top-level record"]) + "\n", encoding="utf-8")
    _write_capture(dd, [{"attributes": [{"key": [], "value": {"stringValue": "bad"}}]}])
    with capture.open("a", encoding="utf-8") as fh:
        fh.write('{"kind":"json","body":' + "[" * 1500 + "{}" + "]" * 1500 + "}\n")
    _write_capture(dd, [_api_record("sess-good-after-bad", EVENING_NANOS)])

    sessions, stats = tc.parse_cowork_sessions(dd)
    assert "sess-good-after-bad" in sessions
    # Python versions differ on whether the deeply nested record reaches the
    # decoder's recursion limit. The invalid top-level record always counts.
    assert stats["undecodable_lines"] in (1, 2)


def test_oversize_capture_line_is_drained_before_next_event(tmp_path, monkeypatch):
    dd = tmp_path / "cap"
    dd.mkdir()
    monkeypatch.setattr(tc, "MAX_LINE", 1024)
    (dd / "otlp-logs.jsonl").write_bytes(b"x" * (4 * 1024 * 1024) + b"\n")
    _write_capture(dd, [_api_record("sess-after-oversize", EVENING_NANOS)])

    tracemalloc.start()
    try:
        sessions, stats = tc.parse_cowork_sessions(dd)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert stats["oversize_lines"] == 1
    assert "sess-after-oversize" in sessions
    assert peak < 1024 * 1024


def test_concurrent_capture_writes_keep_jsonl_records_intact(tmp_path, monkeypatch):
    real_fdopen = os.fdopen

    class SplitWriter:
        def __init__(self, fd, *args, **kwargs):
            self.inner = real_fdopen(fd, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def write(self, text):
            middle = len(text) // 2
            self.inner.write(text[:middle])
            self.inner.flush()
            time.sleep(0.01)
            self.inner.write(text[middle:])
            self.inner.flush()

    monkeypatch.setattr(tc.os, "fdopen", SplitWriter)
    path = tmp_path / "otlp-logs.jsonl"
    start = threading.Barrier(8)

    def append(index):
        start.wait()
        tc._append_private_capture(path, json.dumps({"id": index, "pad": "x" * 1000}) + "\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(8)))

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert sorted(record["id"] for record in records) == list(range(8))


def test_int_helper_clamps_out_of_range(tmp_path):
    assert tc._int(float("inf")) == 0
    assert tc._int("inf") == 0
    assert tc._int(2 ** 64) == 0
    assert tc._int(-(2 ** 64)) == 0
    assert tc._int(-1) == 0
    assert tc._int("1000") == 1000
    assert tc._int("9007199254740993") == 9007199254740993
    assert tc._int("1.5") == 0
    assert tc._int(True) == 0
    assert tc._int(None) == 0


def test_quality_average_fold_is_not_nulled(tmp_path):
    db = tmp_path / "trends.db"
    # Pre-seed a Claude row on LOCAL_DAY with a real quality score.
    measure.TRENDS_DB = db
    measure.SNAPSHOT_DIR = db.parent
    conn = measure._init_trends_db()
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, platform, quality_score, "
        "quality_grade, input_tokens, output_tokens, session_uuid, cost_usd) "
        "VALUES (?, ?, 'claude', 82, 'B', 100, 50, 'claude-uuid-1', 0.0)",
        ("/home/u/.claude/projects/p/claude-uuid-1.jsonl", LOCAL_DAY),
    )
    conn.commit()
    conn.close()

    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-q", EVENING_NANOS)])
    assert _ingest(dd, db) == 0

    conn = sqlite3.connect(str(db))
    try:
        avg = conn.execute(
            "SELECT avg_quality_score FROM daily_stats WHERE date = ?", (LOCAL_DAY,)
        ).fetchone()
    finally:
        conn.close()
    assert avg is not None
    assert avg[0] is not None            # NOT NULLed by the cowork row
    assert avg[0] == pytest.approx((82 + 0) / 2)   # cowork folds in as 0, not NULL


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs time.tzset() to force a non-UTC local tz so the instant crosses "
    "local midnight; tzset is POSIX-only, so on Windows LOCAL_DAY==UTC_DAY and the "
    "cross-midnight scenario can't be constructed. Local-day bucketing itself is "
    "exercised on POSIX CI + a real Windows user's own tz.",
)
def test_local_day_bucketing(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-tz", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    row = _rows(db)[0]
    assert LOCAL_DAY != UTC_DAY           # sanity: the instant really crosses midnight
    assert row[1] == LOCAL_DAY            # bucketed to local day, not UTC day


def test_event_dedup_on_duplicated_batch(tmp_path):
    dd = tmp_path / "cap"
    rec = _api_record("sess-dup", EVENING_NANOS)
    # Same event delivered twice (retry) plus a second rotated file with it again.
    _write_capture(dd, [rec, dict(rec)], name="otlp-logs.jsonl")
    _write_capture(dd, [dict(rec)], name="otlp-logs-1.jsonl")

    sessions, stats = tc.parse_cowork_sessions(dd)
    assert stats["duplicate_events"] == 2
    s = sessions["sess-dup"]
    assert s["api_calls"] == 1           # counted once despite three copies
    bd = s["breakdown"][PRICED_MODEL]
    assert bd["fresh_input"] == 1000

    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    row = _rows(db)[0]
    assert row[2] == 1000 + 200 + 100    # single event's tokens, not tripled


def test_cross_platform_double_count_skip(tmp_path):
    db = tmp_path / "trends.db"
    # A Claude row already carries this session_uuid (TO's Stop hook fired in-VM).
    measure.TRENDS_DB = db
    measure.SNAPSHOT_DIR = db.parent
    conn = measure._init_trends_db()
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, platform, input_tokens, "
        "output_tokens, session_uuid, cost_usd) VALUES "
        "(?, ?, 'claude', 4242, 111, 'shared-sid', 0.99)",
        ("/home/u/.claude/projects/p/shared-sid.jsonl", LOCAL_DAY),
    )
    conn.commit()
    conn.close()

    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("shared-sid", EVENING_NANOS)])
    assert _ingest(dd, db) == 0
    # The cowork row must NOT have been written (would double-count the session).
    assert _rows(db, "jsonl_path='cowork:shared-sid'") == []
    # The original Claude row is untouched.
    claude = _rows(db, "session_uuid='shared-sid'")
    assert len(claude) == 1
    assert claude[0][0].endswith("shared-sid.jsonl")


def test_later_claude_row_replaces_previously_ingested_cowork_copy(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("late-claude-sid", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    assert len(_rows(db, "session_uuid='late-claude-sid'")) == 1

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO session_log (jsonl_path, date, platform, input_tokens, "
            "output_tokens, session_uuid, cost_usd) VALUES "
            "(?, ?, 'claude', 4242, 111, 'late-claude-sid', 0.99)",
            ("/home/u/.claude/projects/p/late-claude-sid.jsonl", LOCAL_DAY),
        )
        conn.commit()
    finally:
        conn.close()

    assert _ingest(dd, db) == 0
    assert _rows(db, "jsonl_path='cowork:late-claude-sid'") == []
    assert len(_rows(db, "session_uuid='late-claude-sid'")) == 1
    conn = sqlite3.connect(str(db))
    try:
        daily = conn.execute(
            "SELECT session_count, total_input FROM daily_stats WHERE date = ?", (LOCAL_DAY,)
        ).fetchone()
    finally:
        conn.close()
    assert daily == (1, 4242)


def test_failed_cowork_row_makes_quiet_ingest_fail(tmp_path):
    db = tmp_path / "trends.db"
    measure.TRENDS_DB = db
    measure.SNAPSHOT_DIR = db.parent
    conn = measure._init_trends_db()
    try:
        conn.execute(
            "CREATE TRIGGER reject_bad_cowork BEFORE INSERT ON session_log "
            "WHEN NEW.platform = 'cowork' AND NEW.session_uuid = 'sess-rejected' "
            "BEGIN SELECT RAISE(FAIL, 'synthetic write failure'); END"
        )
        conn.commit()
    finally:
        conn.close()

    dd = tmp_path / "cap"
    _write_capture(dd, [
        _api_record("sess-good", EVENING_NANOS),
        _api_record("sess-rejected", EVENING_NANOS + 1),
    ])
    assert _ingest(dd, db) == 1
    assert len(_rows(db, "jsonl_path='cowork:sess-good'")) == 1
    assert _rows(db, "jsonl_path='cowork:sess-rejected'") == []


def test_reported_cost_cumulative_is_rejected(tmp_path):
    dd = tmp_path / "cap"
    # A single call reports an absurd cumulative cost >> derived → reject reported.
    _write_capture(dd, [_api_record("sess-cost", EVENING_NANOS, cost_usd=999.0)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    row = _rows(db)[0]
    assert row[5] == "otel_derived"      # fell back to derived, not otel_reported
    assert row[4] < 1.0                  # derived, not the 999 cumulative figure


def test_user_version_stamped(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-ver", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    conn = sqlite3.connect(str(db))
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert ver == 3


def test_oversized_body_replies_413():
    # Unit-level check of the 413 branch without binding a socket: drive do_POST
    # with a stub that captures the reply status.
    handler = tc.CollectorHandler.__new__(tc.CollectorHandler)
    handler.headers = {"Content-Length": str(tc.MAX_BODY + 1)}
    handler.path = "/v1/logs"
    captured = {}

    def fake_reply(status, body):
        captured["status"] = status
        captured["body"] = body

    handler._reply = fake_reply
    handler.do_POST()
    assert captured["status"] == 413


def _post_handler(tmp_path, path="/v1/logs", body=b"{}", length=None):
    handler = tc.CollectorHandler.__new__(tc.CollectorHandler)
    handler.headers = {"Content-Length": str(len(body)) if length is None else length,
                       "Content-Type": "application/json"}
    handler.path = path
    handler.data_dir = tmp_path
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("127.0.0.1", 0)
    captured = {}
    handler._reply = lambda status, payload: captured.update(status=status, body=payload)
    return handler, captured


@pytest.mark.parametrize("length,status", [
    ("-1", 400), ("not-a-number", 400), ("", 400),
])
def test_invalid_body_length_is_rejected_before_read(tmp_path, length, status):
    handler, captured = _post_handler(tmp_path, length=length)
    handler.do_POST()
    assert captured["status"] == status
    assert list(tmp_path.iterdir()) == []


def test_missing_and_short_body_are_rejected(tmp_path):
    handler, captured = _post_handler(tmp_path)
    del handler.headers["Content-Length"]
    handler.do_POST()
    assert captured["status"] == 411

    handler, captured = _post_handler(tmp_path, body=b"{}", length="5")
    handler.do_POST()
    assert captured["status"] == 400
    assert list(tmp_path.iterdir()) == []


def test_chunked_or_timed_out_body_is_rejected(tmp_path):
    handler, captured = _post_handler(tmp_path)
    handler.headers["Transfer-Encoding"] = "chunked"
    handler.do_POST()
    assert captured["status"] == 400

    class TimeoutStream:
        def read(self, size):
            raise TimeoutError

    handler, captured = _post_handler(tmp_path)
    handler.rfile = TimeoutStream()
    handler.do_POST()
    assert captured["status"] == 408
    assert list(tmp_path.iterdir()) == []


def test_ambiguous_request_framing_is_rejected(tmp_path):
    handler, captured = _post_handler(tmp_path)
    headers = Message()
    headers.add_header("Content-Length", "2")
    headers.add_header("Content-Length", "100")
    handler.headers = headers
    handler.do_POST()
    assert captured["status"] == 400

    handler, captured = _post_handler(tmp_path)
    handler.headers["Transfer-Encoding"] = ""
    handler.do_POST()
    assert captured["status"] == 400
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("path", ["/v1/arbitrary", "/probe/extra", "/v1/logs?token=leak"])
def test_undocumented_post_paths_do_not_create_files(tmp_path, path):
    handler, captured = _post_handler(tmp_path, path=path)
    handler.do_POST()
    assert captured["status"] == 404
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("path,capture_name", tc.POST_CAPTURE_FILES.items())
def test_documented_post_paths_capture_to_expected_file(tmp_path, path, capture_name):
    handler, captured = _post_handler(tmp_path, path=path)
    handler.do_POST()
    assert captured["status"] == 200
    assert [item.name for item in tmp_path.iterdir()] == [capture_name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode and symlink semantics")
def test_capture_files_are_private_and_symlinks_are_refused(tmp_path):
    handler, captured = _post_handler(tmp_path)
    handler.headers["X-TO-Probe-Event"] = "Stop"
    handler.headers["X-TO-Token"] = "private-auth-token"
    handler.do_POST()
    capture = tmp_path / "otlp-logs.jsonl"
    assert captured["status"] == 200
    assert capture.stat().st_mode & 0o077 == 0
    record = json.loads(capture.read_text(encoding="utf-8"))
    assert record["headers"] == {"X-TO-Probe-Event": "Stop"}
    assert "private-auth-token" not in capture.read_text(encoding="utf-8")

    capture.unlink()
    target = tmp_path / "unrelated.txt"
    target.write_text("preserve", encoding="utf-8")
    capture.symlink_to(target)
    handler, captured = _post_handler(tmp_path)
    handler.do_POST()
    assert captured["status"] == 500
    assert target.read_text(encoding="utf-8") == "preserve"

    capture.unlink()
    capture.write_text("existing", encoding="utf-8")
    capture.chmod(0o644)
    handler, captured = _post_handler(tmp_path)
    handler.do_POST()
    assert captured["status"] == 500
    assert capture.read_text(encoding="utf-8") == "existing"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
def test_shared_capture_directory_is_refused(tmp_path):
    tmp_path.chmod(0o755)
    try:
        handler, captured = _post_handler(tmp_path)
        handler.do_POST()
        assert captured["status"] == 500
        assert list(tmp_path.iterdir()) == []
    finally:
        tmp_path.chmod(0o700)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
