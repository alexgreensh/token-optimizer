"""Setup trims credit each real session once (A3 dedupe). Synthetic fixtures only."""
import json
from datetime import datetime, timedelta

import pytest

from test_structural_ledger import HOME, LOCAL, STUB, _card, _db, _mods, _req, _session, _trim, m  # noqa: F401


def _day():
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)


def _value(m, conn, tokens=500):
    L, _, _, _, V = _mods()
    day = _day()
    d = day.strftime("%Y-%m-%d")
    trims = L.load_trims_from([_trim(L, (day - timedelta(hours=2)).astimezone().isoformat(), tokens=tokens)])
    res = V.value_trims(conn, trims, d, d, lambda model, p: _card(m, model), HOME)
    return res["trims"][trims[0]["id"]]


def test_same_session_in_two_project_dirs_is_credited_once(m):
    conn = _db(m)
    day, d = _day(), _day().strftime("%Y-%m-%d")
    _session(conn, m, "dup", LOCAL, day, path="/p/-home-tester/dup.jsonl")
    _session(conn, m, "dup", STUB, day, path="/p/-home-tester--claude-stubs/dup.jsonl")
    for i in range(3):
        _req(conn, m, "dup", 10 + i, d, cr=50_000)
    conn.commit()
    got = _value(m, conn)
    assert got["requests"] == 3
    assert got["read_tokens"] == 1500


def test_copy_with_zero_call_claims_adds_nothing(m):
    """A copy under another uuid/path holds the same requests with calls = 0."""
    conn = _db(m)
    day, d = _day(), _day().strftime("%Y-%m-%d")
    _session(conn, m, "orig", LOCAL, day)
    _session(conn, m, "copy", "-home-tester-other", day)
    for i in range(4):
        _req(conn, m, "orig", 20 + i, d, cr=50_000)
        conn.execute(
            "INSERT INTO usage_request_claims (request_hash, owner_hash, calls, inp, out, cr, cc, cc_1h, cc_5m, model, day) "
            "VALUES (?,?,0,10,10,50000,0,0,0,?,?)", (20 + i, m._stable_hash63("copy"), "claude-opus-5-5", d))
    conn.commit()
    got = _value(m, conn)
    assert got["requests"] == 4
    assert got["read_tokens"] == 2000


def test_distinct_sessions_are_all_credited(m):
    conn = _db(m)
    day, d = _day(), _day().strftime("%Y-%m-%d")
    for n, u in enumerate(("a", "b", "c")):
        _session(conn, m, u, LOCAL, day)
        _req(conn, m, u, 30 + n, d, cr=50_000)
    conn.commit()
    assert _value(m, conn)["requests"] == 3


def test_call_weight_zero_is_a_duplicate_and_null_is_legacy_one():
    from _measure import structural_dedupe as D
    assert D.request_weight(None) == 1 and D.request_weight(0) == 0 and D.request_weight(3) == 3


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_most_complete_copy_wins_whatever_the_read_order(m, order):
    """The richer transcript decides project scope; row order must not."""
    from _measure import structural_dedupe as D
    conn = _db(m)
    day = _day()
    rows = [("big", "-home-tester-big", "/p/a/big.jsonl", 50), ("big", "-workspace-small", "/p/b/big.jsonl", 2)]
    for i in order:
        uuid, project, path, calls = rows[i]
        _session(conn, m, uuid, project, day, path=path)
        conn.execute("UPDATE session_log SET api_calls = ? WHERE jsonl_path = ?", (calls, path))
    conn.commit()
    winners, canon = D.canonical_sessions(conn, lambda uuid, path: False)
    assert len(winners) == 1
    assert winners[0]["project"] == "-home-tester-big"
    assert {canon["/p/a/big.jsonl"]["path"], canon["/p/b/big.jsonl"]["path"]} == {"/p/a/big.jsonl"}


def test_uuidless_copies_group_by_first_request(m):
    from _measure import structural_dedupe as D
    conn = _db(m)
    day, d = _day(), _day().strftime("%Y-%m-%d")
    for path, project, calls in (("/p/x/one.jsonl", LOCAL, 9), ("/p/y/two.jsonl", "-elsewhere", 3), ("/p/z/other.jsonl", LOCAL, 5)):
        _session(conn, m, "", project, day, path=path)
        conn.execute("UPDATE session_log SET session_uuid = NULL, api_calls = ? WHERE jsonl_path = ?", (calls, path))
    for path, req in (("/p/x/one.jsonl", 70), ("/p/y/two.jsonl", 70), ("/p/z/other.jsonl", 71)):
        conn.execute(
            "INSERT INTO usage_request_claims (request_hash, owner_hash, calls, inp, out, cr, cc, cc_1h, cc_5m, model, day) "
            "VALUES (?,?,1,1,1,0,0,0,0,'claude-opus-5-5',?)", (req, m._stable_hash63(path), d))
    conn.commit()
    winners, canon = D.canonical_sessions(conn, lambda uuid, path: False)
    assert sorted(w["path"] for w in winners) == ["/p/x/one.jsonl", "/p/z/other.jsonl"]
    assert canon["/p/y/two.jsonl"]["path"] == "/p/x/one.jsonl"
