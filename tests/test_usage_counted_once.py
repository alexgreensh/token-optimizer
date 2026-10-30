"""Token usage is counted once per API request.

A reply is streamed as several transcript records that repeat the same usage.
Claude Code tags them with a `requestId`. Transcripts written by the SDK or
imported from a cloud session carry only the Anthropic `message.id`. Both
identify one request, so a reply counts once whichever id is present. A request
copied into a second session file (resume, fork) counts once across files, and
sessions stored before this rule are corrected from their transcript.
"""

import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
SID_A = "aaaaaaaa-2222-4333-8444-555555555551"
SID_B = "bbbbbbbb-2222-4333-8444-555555555552"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    (tmp_path / "claude_home" / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    for name in ("measure", "runtime_env", "plugin_env"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("measure")


def _noon(days_ago=1):
    return (datetime.now() - timedelta(days=days_ago)).replace(
        hour=12, minute=0, second=0, microsecond=0)


def _utc(local_dt):
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _chunk(when, *, req=None, mid=None, inp=1000, out=500, cr=0, cc=0, text="ok"):
    record = {
        "type": "assistant",
        "timestamp": _utc(when),
        "message": {
            "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc},
        },
    }
    if req:
        record["requestId"] = req
    if mid:
        record["message"]["id"] = mid
    return record


def _write(path, records, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    user = {"type": "user", "timestamp": records[0]["timestamp"], "message": {"content": "hi"}}
    path.write_text("\n".join(json.dumps(r) for r in [user, *records]) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _streamed(when, count, **kw):
    """One reply streamed as `count` records with growing output."""
    base_out = kw.pop("out", 100)
    return [_chunk(when, out=base_out * (i + 1), **kw) for i in range(count)]


def _share(inp=1000, out=500, n=1):
    """What another session has counted of a request, as the claims table holds it."""
    return {"n": n, "inp": inp, "out": out, "cr": 0, "cc": 0, "cc_1h": 0, "cc_5m": 0}


def _projects(tmp_path):
    return tmp_path / "claude_home" / "projects"


def _rows(measure):
    conn = measure._init_trends_db()
    try:
        return conn.execute(
            "SELECT jsonl_path, input_tokens, output_tokens, api_calls, usage_parser_version "
            "FROM session_log ORDER BY jsonl_path").fetchall()
    finally:
        conn.close()


# (a) requestId chunks --------------------------------------------------------

def test_request_id_chunks_count_once(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, _streamed(when, 3, req="req_1", out=100)
           + _streamed(when + timedelta(minutes=1), 2, req="req_2", out=200))

    parsed = measure._parse_session_jsonl(path)

    assert parsed["api_calls"] == 2
    assert parsed["total_input_tokens"] == 2000
    assert parsed["total_output_tokens"] == 300 + 400


# (b) message.id-only chunks ---------------------------------------------------

def test_message_id_only_chunks_count_once(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, _streamed(when, 4, mid="msg_1", out=100)
           + _streamed(when + timedelta(minutes=1), 3, mid="msg_2", out=200))

    parsed = measure._parse_session_jsonl(path)

    assert parsed["api_calls"] == 2
    assert parsed["total_input_tokens"] == 2000
    assert parsed["total_output_tokens"] == 400 + 600


def test_request_id_wins_over_message_id(tmp_path, monkeypatch):
    """Chunks that share a requestId are one request even if message ids differ."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, [_chunk(when, req="req_1", mid="msg_a", out=100),
                  _chunk(when, req="req_1", mid="msg_b", out=300)])

    parsed = measure._parse_session_jsonl(path)

    assert parsed["api_calls"] == 1
    assert parsed["total_output_tokens"] == 300


# (c) neither id ---------------------------------------------------------------

def test_records_without_any_id_each_count(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, [_chunk(when + timedelta(minutes=i)) for i in range(3)])

    parsed = measure._parse_session_jsonl(path)

    assert parsed["api_calls"] == 3
    assert parsed["total_input_tokens"] == 3000


def test_reported_basis_still_sums_every_record(tmp_path, monkeypatch):
    """The /stats-compatible basis is the raw per-record sum by design."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, _streamed(when, 4, mid="msg_1", out=100))

    parsed = measure._parse_session_jsonl(path)

    assert parsed["reported_input_tokens"] == 4000
    assert parsed["total_input_tokens"] == 1000


def test_turns_fold_streamed_chunks_into_one_turn(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    first = _chunk(when, mid="msg_1", out=100)
    last = _chunk(when, mid="msg_1", out=400)
    last["message"]["content"] = [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, [first, last, _chunk(when + timedelta(minutes=1), mid="msg_2", out=50)])

    turns = measure.parse_session_turns(str(path))

    assert len(turns) == 2
    assert turns[0]["output_tokens"] == 400
    assert turns[0]["tools_used"] == ["Read"]
    assert [t["turn_index"] for t in turns] == [0, 1]


# (d) the same message in two session files ------------------------------------

def _fork_fixture(tmp_path):
    when = _noon()
    shared = [_chunk(when + timedelta(minutes=i), mid=f"msg_{i}") for i in range(2)]
    own = [_chunk(when + timedelta(minutes=5), mid="msg_own")]
    projects = _projects(tmp_path)
    older = (datetime.now() - timedelta(hours=3)).timestamp()
    _write(projects / "-repo" / f"{SID_A}.jsonl", shared, mtime=older)
    _write(projects / "-repo" / f"{SID_B}.jsonl", shared + own)
    return when


def test_message_copied_into_a_forked_session_counts_once(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    _fork_fixture(tmp_path)

    measure.collect_sessions(days=30, quiet=True)

    rows = _rows(measure)
    assert len(rows) == 2
    assert sum(r[1] for r in rows) == 3000          # msg_0, msg_1, msg_own once
    assert sum(r[2] for r in rows) == 1500
    assert sum(r[3] for r in rows) == 3
    conn = measure._init_trends_db()
    daily = conn.execute("SELECT SUM(total_input), SUM(total_output) FROM daily_stats").fetchone()
    conn.close()
    assert tuple(daily) == (3000, 1500)


def test_fork_total_does_not_depend_on_collect_order(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    _fork_fixture(tmp_path)
    # Collect the parent alone first, then the fork: the opposite order.
    os.utime(_projects(tmp_path) / "-repo" / f"{SID_B}.jsonl", (1, 1))
    measure.collect_sessions(days=30, quiet=True)
    now = datetime.now().timestamp()
    os.utime(_projects(tmp_path) / "-repo" / f"{SID_B}.jsonl", (now, now))
    measure.collect_sessions(days=30, quiet=True)

    rows = _rows(measure)
    assert sum(r[1] for r in rows) == 3000
    assert sum(r[3] for r in rows) == 3


def _isolated_home(measure, tmp_path, monkeypatch):
    """Run from inside tmp_path: the rollup's component scan also looks for a
    CLAUDE.md in the working directory's parents, which must not be the real home."""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(work)


def test_fallback_rollup_counts_a_forked_message_once(tmp_path, monkeypatch):
    """The no-database dashboard rollup applies the same one-count rule across files."""
    measure = _load(tmp_path, monkeypatch)
    _isolated_home(measure, tmp_path, monkeypatch)
    when = _fork_fixture(tmp_path)

    trends = measure._collect_trends_from_jsonl(days=30)

    assert trends["total_input_tokens"] == 3000
    assert trends["total_output_tokens"] == 1500
    day = {d["date"]: d for d in trends["daily"]}[when.strftime("%Y-%m-%d")]
    assert day["total_input"] == 3000
    assert day["total_output"] == 1500


def test_fallback_rollup_total_does_not_depend_on_file_order(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    _isolated_home(measure, tmp_path, monkeypatch)
    _fork_fixture(tmp_path)
    projects = _projects(tmp_path) / "-repo"
    newer = datetime.now().timestamp() + 60
    os.utime(projects / f"{SID_A}.jsonl", (newer, newer))   # parent now listed after the fork

    trends = measure._collect_trends_from_jsonl(days=30)

    assert trends["total_input_tokens"] == 3000
    assert trends["total_output_tokens"] == 1500


def test_daily_usage_of_a_fork_leaves_out_the_parents_requests(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _fork_fixture(tmp_path)
    measure.collect_sessions(days=30, quiet=True)

    conn = measure._init_trends_db()
    per_day = {}
    for (blob,) in conn.execute("SELECT daily_usage_json FROM session_log"):
        for day, models in json.loads(blob).items():
            for buckets in models.values():
                per_day[day] = per_day.get(day, 0) + buckets["fresh_input"]
    conn.close()
    assert per_day == {when.strftime("%Y-%m-%d"): 3000}


def test_claim_held_by_a_session_without_a_row_is_ignored(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_B}.jsonl"
    _write(path, [_chunk(when, mid="msg_0")])
    conn = measure._init_trends_db()
    measure._claim_requests(conn, "a-session-that-was-deleted", {"msg_0": _share()})
    conn.commit()
    conn.close()

    measure.collect_sessions(days=30, quiet=True)

    assert [r[1] for r in _rows(measure)] == [1000]


# Stored history ---------------------------------------------------------------

def _inflate_like_an_old_collect(measure, path, inflated=999_999):
    conn = measure._init_trends_db()
    conn.execute(
        "UPDATE session_log SET input_tokens = ?, api_calls = 999, usage_parser_version = NULL "
        "WHERE jsonl_path = ?", (inflated, str(path)))
    conn.execute("DELETE FROM usage_request_claims")
    conn.commit()
    conn.close()


def test_rows_stored_with_old_counting_are_recomputed_once(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, _streamed(when, 5, mid="msg_1") + _streamed(when + timedelta(minutes=1), 5, mid="msg_2"))
    measure.collect_sessions(days=30, quiet=True)
    _inflate_like_an_old_collect(measure, path)

    measure.collect_sessions(days=30, quiet=True)
    first = _rows(measure)
    assert [(r[1], r[3], r[4]) for r in first] == [(2000, 2, measure._USAGE_PARSER_VERSION)]
    conn = measure._init_trends_db()
    stamp = conn.execute("SELECT collected_at FROM session_log").fetchone()[0]
    conn.close()

    # Idempotent: a current row is not re-parsed and nothing moves.
    measure.collect_sessions(days=30, quiet=True)
    conn = measure._init_trends_db()
    again = conn.execute("SELECT collected_at FROM session_log").fetchone()[0]
    conn.close()
    assert again == stamp
    assert _rows(measure) == first


def test_aggregate_tables_follow_the_recomputed_rows(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, _streamed(when, 3, mid="msg_1"))
    measure.collect_sessions(days=30, quiet=True)
    _inflate_like_an_old_collect(measure, path)

    measure.collect_sessions(days=30, quiet=True)

    conn = measure._init_trends_db()
    total = conn.execute("SELECT SUM(total_input) FROM daily_stats").fetchone()[0]
    conn.close()
    assert total == 1000


def test_row_without_its_transcript_keeps_its_numbers(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    kept = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    gone = _projects(tmp_path) / "-repo" / f"{SID_B}.jsonl"
    _write(kept, [_chunk(when, mid="msg_1")])
    _write(gone, [_chunk(when, mid="msg_2")])
    measure.collect_sessions(days=30, quiet=True)
    _inflate_like_an_old_collect(measure, kept)
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET input_tokens = 123456, usage_parser_version = NULL "
                 "WHERE jsonl_path = ?", (str(gone),))
    conn.commit()
    conn.close()
    gone.unlink()

    measure.collect_sessions(days=30, quiet=True)

    by_path = {r[0]: r for r in _rows(measure)}
    assert by_path[str(gone)][1] == 123456
    assert by_path[str(kept)][1] == 1000


def test_recompute_leaves_the_frozen_baseline_files_alone(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, _streamed(when, 3, mid="msg_1"))
    measure.collect_sessions(days=30, quiet=True)
    _inflate_like_an_old_collect(measure, path)
    snap = tmp_path / "snap"
    frozen = {"workload_anchor.json": '{"month": "2026-06", "metrics": {"sessions": 1}}',
              "baseline_state.json": '{"version": 4, "frozen_at": "2026-08-29T14:40:42"}'}
    for name, body in frozen.items():
        (snap / name).write_text(body, encoding="utf-8")

    measure.collect_sessions(days=30, quiet=True)

    for name, body in frozen.items():
        assert (snap / name).read_text(encoding="utf-8") == body


# Multi-file passes ------------------------------------------------------------

def test_seen_request_filter_counts_a_repeated_request_once(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    one = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    two = _projects(tmp_path) / "-repo" / f"{SID_B}.jsonl"
    _write(one, [_chunk(when, mid="msg_1"), _chunk(when, mid="msg_2")])
    _write(two, [_chunk(when, mid="msg_2"), _chunk(when, mid="msg_3")])

    ledger = {}
    first = measure._parsed_net_of_ledger(measure._parse_session_jsonl(one), ledger)
    second = measure._parsed_net_of_ledger(measure._parse_session_jsonl(two), ledger)

    assert first["total_input_tokens"] == 2000
    assert second["total_input_tokens"] == 1000
    assert second["api_calls"] == 1
    assert sum(second["model_usage_breakdown"]["claude-opus-4-8"].values()) > 0


def test_filtering_does_not_modify_the_cached_parse(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, [_chunk(when, mid="msg_1"), _chunk(when, mid="msg_2")])
    parsed = measure._parse_session_jsonl(path)

    filtered = measure._parsed_net_of_counted(parsed, {"msg_1": _share()})

    assert filtered["total_input_tokens"] == 1000
    assert measure._parse_session_jsonl(path)["total_input_tokens"] == 2000


# One request, different copies: the largest usage wins -----------------------

def _copies(tmp_path, out_a, out_b, a_older=True):
    """The same reply (msg_1) in two sessions, streamed to `out_a` tokens in one
    copy and `out_b` in the other."""
    when = _noon()
    now = datetime.now().timestamp()
    older, newer = now - 7200, now - 3600
    projects = _projects(tmp_path) / "-repo"
    _write(projects / f"{SID_A}.jsonl", [_chunk(when, mid="msg_1", out=out_a)],
           mtime=older if a_older else newer)
    _write(projects / f"{SID_B}.jsonl", [_chunk(when, mid="msg_1", out=out_b)],
           mtime=newer if a_older else older)
    return when


COPY_ORDERS = [(100, 1000, True), (100, 1000, False), (1000, 100, True), (1000, 100, False)]


@pytest.mark.parametrize("out_a,out_b,a_older", COPY_ORDERS)
def test_collect_keeps_the_largest_copy_of_a_request(tmp_path, monkeypatch, out_a, out_b, a_older):
    measure = _load(tmp_path, monkeypatch)
    _copies(tmp_path, out_a, out_b, a_older)

    measure.collect_sessions(days=30, quiet=True)

    rows = _rows(measure)
    assert sum(r[2] for r in rows) == 1000       # output: the larger copy, once
    assert sum(r[1] for r in rows) == 1000       # input: identical in both, once
    assert sum(r[3] for r in rows) == 1
    conn = measure._init_trends_db()
    daily = conn.execute("SELECT SUM(total_input), SUM(total_output) FROM daily_stats").fetchone()
    conn.close()
    assert tuple(daily) == (1000, 1000)


@pytest.mark.parametrize("out_a,out_b,a_older", COPY_ORDERS)
def test_fallback_rollup_keeps_the_largest_copy_of_a_request(tmp_path, monkeypatch, out_a, out_b, a_older):
    measure = _load(tmp_path, monkeypatch)
    _isolated_home(measure, tmp_path, monkeypatch)
    _copies(tmp_path, out_a, out_b, a_older)

    trends = measure._collect_trends_from_jsonl(days=30)

    assert trends["total_output_tokens"] == 1000
    assert trends["total_input_tokens"] == 1000


@pytest.mark.parametrize("out_a,out_b", [(100, 1000), (1000, 100)])
def test_ledger_pass_keeps_the_largest_copy_in_either_order(tmp_path, monkeypatch, out_a, out_b):
    measure = _load(tmp_path, monkeypatch)
    _copies(tmp_path, out_a, out_b)
    one = measure._parse_session_jsonl(_projects(tmp_path) / "-repo" / f"{SID_A}.jsonl")
    two = measure._parse_session_jsonl(_projects(tmp_path) / "-repo" / f"{SID_B}.jsonl")

    for first, second in ((one, two), (two, one)):
        ledger = {}
        a = measure._parsed_net_of_ledger(first, ledger)
        b = measure._parsed_net_of_ledger(second, ledger)
        assert a["total_output_tokens"] + b["total_output_tokens"] == 1000
        assert a["total_input_tokens"] + b["total_input_tokens"] == 1000
        assert a["api_calls"] + b["api_calls"] == 1


def test_a_larger_fork_collected_later_adds_only_the_difference(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    projects = _projects(tmp_path) / "-repo"
    _write(projects / f"{SID_A}.jsonl", [_chunk(when, mid="msg_1", out=100)])
    measure.collect_sessions(days=30, quiet=True)
    assert sum(r[2] for r in _rows(measure)) == 100

    _write(projects / f"{SID_B}.jsonl", [_chunk(when, mid="msg_1", out=1000)])
    measure.collect_sessions(days=30, quiet=True)

    rows = _rows(measure)
    assert sum(r[2] for r in rows) == 1000
    assert sum(r[3] for r in rows) == 1
    # The first session's stored row is history: it is not rewritten.
    assert dict((r[0].rsplit("/", 1)[1], r[2]) for r in rows)[f"{SID_A}.jsonl"] == 100


def test_a_copy_that_grows_after_collect_does_not_double_count(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    when = _copies(tmp_path, 100, 1000)
    measure.collect_sessions(days=30, quiet=True)
    assert sum(r[2] for r in _rows(measure)) == 1000

    # The shorter copy later streams to the full length and is collected again.
    path = _projects(tmp_path) / "-repo" / f"{SID_A}.jsonl"
    _write(path, [_chunk(when, mid="msg_1", out=1000)],
           mtime=datetime.now().timestamp() + 60)
    measure.collect_sessions(days=30, quiet=True)
    assert sum(r[2] for r in _rows(measure)) == 1000

    # And once more, past the full length, the total follows the largest copy.
    _write(path, [_chunk(when, mid="msg_1", out=1500)],
           mtime=datetime.now().timestamp() + 120)
    measure.collect_sessions(days=30, quiet=True)
    rows = _rows(measure)
    assert sum(r[2] for r in rows) == 1500
    assert sum(r[3] for r in rows) == 1


def test_recollecting_an_unchanged_copy_changes_nothing(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    _copies(tmp_path, 100, 1000)
    measure.collect_sessions(days=30, quiet=True)
    before = _rows(measure)
    conn = measure._init_trends_db()
    conn.execute("UPDATE session_log SET collected_at = NULL")
    conn.commit()
    conn.close()

    measure.collect_sessions(days=30, quiet=True)

    assert _rows(measure) == before


def test_net_of_counted_never_goes_below_zero(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    requests = {"msg_1": {"inp": 10, "out": 20, "cr": 0, "cc": 0, "cc_1h": 0, "cc_5m": 0,
                          "model": "claude-opus-4-8", "day": None}}

    net = measure._net_of_counted(requests, {"msg_1": _share(inp=50, out=5, n=1)})

    assert (net["msg_1"]["inp"], net["msg_1"]["out"], net["msg_1"]["n"]) == (0, 15, 0)


@pytest.mark.parametrize("smaller_first", [True, False])
def test_a_partial_share_survives_recollecting_its_session_for_another_reason(
        tmp_path, monkeypatch, smaller_first):
    """The session holding the shorter copy of a shared request gets a new,
    unrelated request and is collected again; the longer copy's session is
    untouched. The shared request must still count at its largest figure."""
    measure = _load(tmp_path, monkeypatch)
    when = _noon()
    projects = _projects(tmp_path) / "-repo"
    path_a, path_b = projects / f"{SID_A}.jsonl", projects / f"{SID_B}.jsonl"
    shorter, longer = _chunk(when, mid="msg_1", out=100), _chunk(when, mid="msg_1", out=1000)
    first, second = ((path_a, shorter), (path_b, longer)) if smaller_first else ((path_b, longer), (path_a, shorter))
    _write(first[0], [first[1]])
    measure.collect_sessions(days=30, quiet=True)
    _write(second[0], [second[1]])
    measure.collect_sessions(days=30, quiet=True)
    assert sum(r[2] for r in _rows(measure)) == 1000
    by_name = lambda rows: {r[0].rsplit("/", 1)[1]: r for r in rows}   # noqa: E731
    b_before = by_name(_rows(measure))[f"{SID_B}.jsonl"]

    # Session A grows by an unrelated request; B is not touched.
    _write(path_a, [shorter, _chunk(when + timedelta(minutes=5), mid="msg_2", out=500)],
           mtime=datetime.now().timestamp() + 60)
    measure.collect_sessions(days=30, quiet=True)
    rows = _rows(measure)
    assert sum(r[2] for r in rows) == 1500       # msg_1 at its largest, once, plus msg_2
    assert sum(r[3] for r in rows) == 2
    assert by_name(rows)[f"{SID_B}.jsonl"] == b_before

    # Collecting again, with or without a change, is stable.
    measure.collect_sessions(days=30, quiet=True)
    assert _rows(measure) == rows
    os.utime(path_a, (datetime.now().timestamp() + 120,) * 2)
    measure.collect_sessions(days=30, quiet=True)
    assert sum(r[2] for r in _rows(measure)) == 1500
    assert sum(r[3] for r in _rows(measure)) == 2


def test_a_session_keeps_its_earlier_share_when_it_is_collected_again(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    requests = {"msg_1": {"inp": 1000, "out": 100, "cr": 0, "cc": 0, "cc_1h": 0, "cc_5m": 0,
                          "model": "claude-opus-4-8", "day": None}}
    others = {"msg_1": _share(inp=0, out=900, n=0)}
    earlier = {"msg_1": _share(inp=1000, out=100, n=1)}

    net = measure._net_of_counted(requests, others, earlier)

    assert (net["msg_1"]["inp"], net["msg_1"]["out"], net["msg_1"]["n"]) == (1000, 100, 1)
