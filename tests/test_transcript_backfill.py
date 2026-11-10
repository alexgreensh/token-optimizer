"""Transcript backfill (savings-truth U1): sessions where Token Optimizer ran
but the local ledger is empty get their events rebuilt from Token Optimizer's
own pointer markers. Synthetic fixtures only.
"""
import hashlib
import json
import sqlite3
import sys
from datetime import timedelta

import pytest

from test_counted_cumulative import SCRIPTS, _db, _iso_z, _local_iso, _utc_now, m  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
from _measure import transcript_backfill as tb  # noqa: E402

CLOUD = "c10d0000-1111-2222-3333-444455556666"
LOCAL = "10ca1000-1111-2222-3333-444455556666"
EM = "—"


def pointer(chars, key="0123456789abcdef", kind=None, tool=None):
    suffix = f" ({kind})" if kind else ""
    dont = f"Do NOT call {tool} again" if tool else "Do NOT re-run the original tool"
    return (f"\n\n[Full result archived ({chars:,} chars{suffix}) {EM} saved to disk, not lost.\n"
            f"{dont} to get this data {EM} read the saved copy by running this in Bash:\n"
            f"    python3 /home/user/measure.py expand {key}\n]")


# --------------------------------------------------------------------------
# Marker strictness
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,chars", [
    ("preview" + pointer(15675), 15675),
    ("preview" + pointer(18527, key="toolu_01ABC", kind="json", tool="mcp__x__y"), 18527),
    (pointer(900).lstrip(), 900),
    ("preview\n[Full result archived (197,728 chars).]", 197728),
    # an older footer with the bracket on the expand line
    (f"p\n\n[Full result archived (4,275 chars) {EM} saved to disk, not lost.\nRetrieve: expand abcdef0123456789]", 4275),
])
def test_pointer_matches_only_token_optimizer_tails(text, chars):
    match = tb.POINTER_RX.search(text)
    assert match and int(match.group("chars").replace(",", "")) == chars


@pytest.mark.parametrize("text", [
    # source code that builds the pointer
    'f"[Full result archived ({original_chars:,} chars) ' + EM + ' saved to disk, not lost.\\n"',
    # a real pointer followed by more output: not the tail, so not ours
    "x" + pointer(5000) + "\nreal\t0m0.099s",
    # escaped JSON echo of a pointer
    '{"t": "[Full result archived (15,990 chars (json)) \\u2014 saved to disk, not lost.\\nexpand call_2\\n]"}',
    # the loose words alone
    "lines omitted, expand with care, [compressed] output",
])
def test_quoted_or_mid_text_markers_never_count(text):
    assert tb.POINTER_RX.search(text) is None


# --------------------------------------------------------------------------
# Transcript fixtures
# --------------------------------------------------------------------------

def _write(tmp_path, sid, records):
    proj = tmp_path / "claude" / "projects" / "-Users-owner-cloud"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _session(tmp_path, sid, start=None, bash_chars=30_000, mcp_chars=12_000, extra=()):
    start = start or (_utc_now() - timedelta(hours=3))
    ts = lambda i: _iso_z(start + timedelta(minutes=i))  # noqa: E731
    usage = {"input_tokens": 2, "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 100}
    recs = [
        {"type": "assistant", "timestamp": ts(0), "requestId": "r0", "message": {
            "model": "claude-opus-5-5", "usage": usage, "content": [
                {"type": "tool_use", "id": "toolu_bash1", "name": "Bash", "input": {"command": "cargo test"}}]}},
        {"type": "user", "timestamp": ts(1), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_bash1",
             "content": "test a ... ok\n... 400 lines omitted ...\ntest result: ok" + pointer(bash_chars)}]}},
        {"type": "attachment", "timestamp": ts(1), "attachment": {
            "type": "hook_system_message", "hookName": "PostToolUse:Bash",
            "content": "[Token Optimizer] 54% full, quality 64.5."}},
        {"type": "assistant", "timestamp": ts(2), "requestId": "r1", "message": {
            "model": "claude-opus-5-5", "usage": usage, "content": [
                {"type": "tool_use", "id": "toolu_mcp1", "name": "mcp__docs__fetch", "input": {}}]}},
        {"type": "user", "timestamp": ts(3), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_mcp1", "content": [
                {"type": "text", "text": '{"a": 1}' + pointer(mcp_chars, key="toolu_mcp1", kind="json",
                                                               tool="mcp__docs__fetch")}]}]}},
        # a Read of Token Optimizer's own source: quotes the marker, not a compression
        {"type": "user", "timestamp": ts(4), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_read1",
             "content": 'x = f"[Full result archived ({n:,} chars) ' + EM + ' saved to disk, not lost.\\n"'}]}},
        # an unsized Token Optimizer trim (no pointer): counted, never written
        {"type": "user", "timestamp": ts(5), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_bash2", "content": "a\n... 12 lines omitted ...\nb"}]}},
        # sub-agent line: ignored
        {"type": "user", "isSidechain": True, "timestamp": ts(6), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_side", "content": "s" + pointer(9_000)}]}},
    ]
    for i in range(7, 12):
        recs.append({"type": "assistant", "timestamp": ts(i), "requestId": f"r{i}",
                     "message": {"model": "claude-opus-5-5", "usage": usage, "content": [{"type": "text", "text": "ok"}]}})
    recs.extend(extra)
    return _write(tmp_path, sid, recs), start


def _register(dbp, sid, path, date="2099-01-01"):
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO session_log (date, session_uuid, jsonl_path, project) VALUES (?,?,?,?)",
                 (_utc_now().strftime("%Y-%m-%d"), sid, str(path), "-any-dir"))
    conn.commit()
    conn.close()


def test_scan_reads_only_token_optimizer_markers(tmp_path):
    path, _ = _session(tmp_path, CLOUD)
    scan = tb.scan_transcript(path)
    assert [e["tool"] for e in scan["sized"]] == ["Bash", "mcp__docs__fetch"]
    assert [e["original_chars"] for e in scan["sized"]] == [30_000, 12_000]
    assert scan["sized"][0]["model"] == "claude-opus-5-5"
    assert scan["unsized"] == {"lines_omitted": 1}
    assert scan["traces"] == {"[Token Optimizer]": 1}
    assert tb.token_optimizer_ran(scan)


def test_no_trace_means_no_target(tmp_path):
    path = _write(tmp_path, CLOUD, [{"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "plain output"}]}}])
    assert not tb.token_optimizer_ran(tb.scan_transcript(path))


def _rows(dbp):
    conn = sqlite3.connect(str(dbp))
    try:
        ce = conn.execute("SELECT feature, original_tokens, compressed_tokens, tier, verified, origin, "
                          "backfill_key, session_uuid, model, timestamp FROM compression_events "
                          "WHERE origin=?", (tb.ORIGIN,)).fetchall()
        se = conn.execute("SELECT event_type, tokens_saved, cost_saved_usd, detail, origin, backfill_key "
                          "FROM savings_events WHERE origin=?", (tb.ORIGIN,)).fetchall()
        return ce, se
    finally:
        conn.close()


def _digest(dbp):
    conn = sqlite3.connect(str(dbp))
    try:
        rows = conn.execute("SELECT * FROM savings_events WHERE COALESCE(origin,'') != ? ORDER BY id",
                            (tb.ORIGIN,)).fetchall()
        rows += conn.execute("SELECT * FROM compression_events WHERE COALESCE(origin,'') != ? ORDER BY id",
                             (tb.ORIGIN,)).fetchall()
        return hashlib.sha256(repr(rows).encode()).hexdigest()
    finally:
        conn.close()


def test_backfill_writes_measured_rows_once_and_never_touches_existing(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    path, start = _session(tmp_path, CLOUD)
    _register(dbp, CLOUD, path)
    lpath, lstart = _session(tmp_path, LOCAL)
    _register(dbp, LOCAL, lpath)
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,compressed_tokens,verified,"
                 "tier,session_uuid,session_id) VALUES(?,?,?,?,1,'measured',?,?)",
                 (_local_iso(lstart + timedelta(minutes=1)), "bash_compress_pipeline", 9091, 100, LOCAL, LOCAL))
    conn.execute("INSERT INTO savings_events(timestamp,event_type,tokens_saved,session_uuid,session_id,detail) VALUES(?,?,?,?,?,?)",
                 (_local_iso(lstart + timedelta(minutes=3)), 'tool_archive', 3500, LOCAL, LOCAL,
                  'replaced mcp__docs__fetch (12,000 chars -> 300 chars)'))
    tb.ensure_schema(conn)  # additive columns first, so the digest compares like with like
    conn.commit()
    before = _digest(dbp)

    conn = sqlite3.connect(str(dbp))
    report = {}
    first = tb.run(conn, m, "2000-01-01", report=report)
    second = tb.run(conn, m, "2000-01-01")
    conn.close()

    assert first["rows_written"] == 2 and first["to_ran_ledger_empty"] == 1
    assert first["mixed_sessions"] == 1 and report[LOCAL]["local_events"] is True
    assert second["rows_written"] == 0
    assert _digest(dbp) == before
    ce, se = _rows(dbp)
    from token_estimate import estimate_tokens_from_bytes
    (feature, orig, comp, tier, verified, origin, key, uuid, model, ts), = ce
    assert (feature, tier, verified, origin, uuid, model) == (
        "bash_compress_pipeline", "measured", 1, tb.ORIGIN, CLOUD, "claude-opus-5-5")
    assert orig == estimate_tokens_from_bytes(30_000) and 0 < comp < orig
    assert key == f"{CLOUD}:toolu_bash1"
    assert ts == _local_iso((start + timedelta(minutes=1)).replace(microsecond=0))
    (etype, saved, cost, detail, origin, key), = se
    assert etype == "tool_archive" and key == f"{CLOUD}:toolu_mcp1"
    assert detail.startswith("replaced mcp__docs__fetch (12,000 chars -> ")
    card = m._counted_exact_card("claude-opus-5-5", m.PRICING_TIERS[m._load_pricing_tier()])
    assert cost == pytest.approx(saved * card["input"] / 1e6)


def test_unique_guard_rejects_a_duplicate_key(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    conn = sqlite3.connect(str(dbp))
    tb.ensure_schema(conn)
    row = ("2026-11-01T10:00:00", CLOUD, "bash_compress_pipeline", 900, 100, 0.1, 1, 1, "d", CLOUD,
           None, "measured", tb.ORIGIN, "k1")
    assert tb.write_rows(conn, [row], []) == 1
    assert tb.write_rows(conn, [row], []) == 0
    assert conn.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0] == 1


def test_fixture_sessions_are_never_backfilled(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    sid = "test-session-123"
    path, _ = _session(tmp_path, sid)
    _register(dbp, sid, path)
    conn = sqlite3.connect(str(dbp))
    stats = tb.run(conn, m, "2000-01-01")
    conn.close()
    assert stats["sessions_considered"] == 0 and _rows(dbp) == ([], [])


def test_backfilled_rows_reach_the_counted_ledger(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    path, _ = _session(tmp_path, CLOUD)
    _register(dbp, CLOUD, path)
    conn = sqlite3.connect(str(dbp))
    tb.run(conn, m, "2000-01-01")
    m._update_counted_cumulative(conn, max_sessions=10, deadline_seconds=None, quiet=True)
    rows = conn.execute("SELECT event_key, reread_tokens, turns_counted FROM counted_reread").fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(r[1] > 0 and r[2] > 0 for r in rows)


def test_grown_transcript_is_rescanned_and_only_new_markers_added(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    path, start = _session(tmp_path, CLOUD)
    _register(dbp, CLOUD, path)
    conn = sqlite3.connect(str(dbp))
    assert tb.collect_pass(conn, m, None, {})["rows_written"] == 2
    assert tb.collect_pass(conn, m, None, {})["files_scanned"] == 0  # unchanged: cursor skip
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "assistant", "timestamp": _iso_z(start + timedelta(minutes=20)),
                                 "message": {"model": "claude-opus-5-5", "content": [
                                     {"type": "tool_use", "id": "toolu_bash9", "name": "Bash", "input": {}}]}}) + "\n")
        stream.write(json.dumps({"type": "user", "timestamp": _iso_z(start + timedelta(minutes=21)),
                                 "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_bash9",
                                                          "content": "z" + pointer(8_000)}]}}) + "\n")
    third = tb.collect_pass(conn, m, None, {})
    conn.close()
    assert third["files_scanned"] == 1 and third["rows_written"] == 1


def test_collect_pass_respects_the_marker_backfill_file_cap(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path / "claude")
    monkeypatch.setattr(m, "_COUNTED_MARKER_FILES_PER_PASS", 1)
    for i in range(3):
        sid = f"c10d000{i}-1111-2222-3333-444455556666"
        path, _ = _session(tmp_path, sid)
        _register(dbp, sid, path)
    conn = sqlite3.connect(str(dbp))
    first = tb.collect_pass(conn, m, None, {})
    assert first["files_scanned"] == 1 and first["pending"] == 2
    tb.collect_pass(conn, m, None, {})
    tb.collect_pass(conn, m, None, {})
    assert tb.collect_pass(conn, m, None, {})["files_scanned"] == 0
    assert conn.execute("SELECT COUNT(DISTINCT session_uuid) FROM compression_events WHERE origin=?",
                        (tb.ORIGIN,)).fetchone()[0] == 3
    conn.close()


def test_local_naive_matches_the_ledger_clock():
    from datetime import datetime, timezone
    utc = datetime(2026, 10, 20, 16, 56, 20)
    assert tb.local_naive("2026-10-20T16:56:20.000Z") == utc.replace(
        tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()


def test_candidate_sessions_excludes_optimizer_benchmark_transcripts():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE session_log (id INTEGER, session_uuid TEXT, jsonl_path TEXT, date TEXT)')
    conn.executemany('INSERT INTO session_log VALUES (?, ?, ?, ?)', [
        (1, 'bench', '/projects/-private-var-folders-x-T-to-bench-M2-abc/bench.jsonl', '2026-08-01'),
        (2, 'real', '/projects/-Users-person-work/real.jsonl', '2026-08-01'),
    ])
    assert set(tb.candidate_sessions(conn, '2026-01-01', lambda _: False)) == {'real'}


def test_existing_benchmark_events_are_not_counted(m, tmp_path, monkeypatch):
    db = _db(m, tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO session_log (session_uuid, jsonl_path, date) VALUES (?, ?, ?)",
                     ('bench', '/projects/-private-var-folders-x-T-to-bench-M2-abc/bench.jsonl', '2026-08-01'))
        conn.execute("INSERT INTO compression_events (timestamp, feature, session_uuid, original_tokens, compressed_tokens, verified) VALUES (?, ?, ?, ?, ?, ?)",
                     ('2026-08-01', 'bash_compress_pipeline', 'bench', 100, 10, 1))
        assert m._ensure_counted_views(conn)
        assert conn.execute('SELECT count(*) FROM counted_compression_events').fetchone()[0] == 0


def test_mixed_session_repairs_only_the_lost_marker(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, 'CLAUDE_DIR', tmp_path / 'claude')
    path, start = _session(tmp_path, LOCAL)
    _register(dbp, LOCAL, path)
    from token_estimate import estimate_tokens_from_bytes
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,compressed_tokens,verified,tier,session_uuid,session_id) VALUES(?,?,?,?,1,'measured',?,?)", (_local_iso(start + timedelta(minutes=1)), 'bash_compress_pipeline', estimate_tokens_from_bytes(45000), 100, LOCAL, LOCAL))
    conn.commit()
    before = conn.execute('SELECT count(*) FROM compression_events').fetchone()[0]
    first = tb.run(conn, m, '2000-01-01')
    second = tb.run(conn, m, '2000-01-01')
    assert first['rows_written'] == 1 and second['rows_written'] == 0
    assert conn.execute('SELECT count(*) FROM compression_events').fetchone()[0] == before
    assert conn.execute("SELECT count(*) FROM savings_events WHERE origin=?", (tb.ORIGIN,)).fetchone()[0] == 1
    conn.close()


def test_mixed_session_nearby_different_bash_command_does_not_hide_loss(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, 'CLAUDE_DIR', tmp_path / 'claude')
    path, start = _session(tmp_path, LOCAL)
    _register(dbp, LOCAL, path)
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,compressed_tokens,command_pattern,verified,tier,session_uuid,session_id) VALUES(?,?,?,?,?,1,'measured',?,?)", (_local_iso(start + timedelta(minutes=1)), 'bash_compress_pipeline', 12000, 100, 'git log', LOCAL, LOCAL))
    conn.commit()
    assert tb.run(conn, m, '2000-01-01')['rows_written'] == 2
    assert conn.execute("SELECT count(*) FROM compression_events WHERE origin=?", (tb.ORIGIN,)).fetchone()[0] == 1
    conn.close()


def test_mixed_backfill_deduplicates_equivalent_shell_quoting(m, tmp_path, monkeypatch):
    dbp = _db(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, 'CLAUDE_DIR', tmp_path / 'claude')
    path, start = _session(tmp_path, LOCAL)
    _register(dbp, LOCAL, path)
    conn = sqlite3.connect(str(dbp))
    conn.execute("INSERT INTO compression_events(timestamp,feature,original_tokens,compressed_tokens,command_pattern,verified,tier,session_uuid,session_id) VALUES(?,?,?,?,?,1,'measured',?,?)", (_local_iso(start + timedelta(minutes=1)), 'bash_compress_pipeline', 12000, 100, "cargo 'test'", LOCAL, LOCAL))
    conn.commit()
    assert tb.run(conn, m, '2000-01-01')['rows_written'] == 1
    assert conn.execute("SELECT count(*) FROM compression_events WHERE origin=?", (tb.ORIGIN,)).fetchone()[0] == 0
    conn.close()
