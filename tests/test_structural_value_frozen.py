"""value_trims must price exactly like the loop it replaced (frozen below).

The pre-cache loop is pasted here as a fixed reference instead of read from
`git show origin/main` at run time: after merge that ref IS the new code, and
git may be missing. Intended pricing changes must update this reference on
purpose. Synthetic fixtures only.
"""
import random
from datetime import datetime, timedelta

import pytest

from test_structural_ledger import HOME, LOCAL, LOCAL_SUB, STUB, _card, _db, _mods, _req, _session, m  # noqa: F401

SONNET = "claude-sonnet-5-5"


def _frozen_price(card, s, cr, cc, cc_1h, cc_5m):
    return s, 0, s * float(card.get("cache_read", 0) or 0) / 1e6, 0.0, 0.0


def _frozen_value_trims(conn, trims, start_day, end_day, card_fn, home, projected=False):
    """structural_value.value_trims as of origin/main 2026-11-16, verbatim loop."""
    from _measure import structural_dedupe as dedupe, structural_value as V
    sessions = V.load_sessions(conn)
    rows = V.load_requests(conn, start_day, end_day)
    per = {t["id"]: {"read_tokens": 0, "write_tokens": 0, "usd": 0.0, "read_usd": 0.0,
                     "write_1h_usd": 0.0, "write_5m_usd": 0.0, "requests": 0,
                     "unpriced_tokens": 0} for t in trims}
    days = {}
    unmatched = 0
    live = [t for t in trims if t.get("tokens", 0) > 0 and not (projected and t.get("closed_at"))]
    by_scope = {}
    for t in live:
        by_scope.setdefault(t["scope"], []).append(t)
    trim_days = {} if projected else {
        t["id"]: (datetime.fromtimestamp(t["ts_epoch"]).strftime("%Y-%m-%d"),
                  datetime.fromtimestamp(t["closed_epoch"]).strftime("%Y-%m-%d")
                  if t.get("closed_epoch") is not None else None)
        for t in live
    }
    scope_cache = {}
    for owner, day, model, calls, cr, cc, cc_1h, cc_5m, inp in rows:
        sess = sessions.get(owner)
        if sess is None:
            unmatched += 1
            continue
        project, start, foreign = sess
        card = card_fn(model, (cr or 0) + (cc or 0) + (inp or 0))
        n = dedupe.request_weight(calls)
        if not n:
            continue
        for scope, group in by_scope.items():
            if foreign and scope == "global":
                continue
            key = (scope, project)
            if key not in scope_cache:
                scope_cache[key] = V.scope_matches(scope, project, home)
            if not scope_cache[key]:
                continue
            for t in group:
                if not projected:
                    trim_day, closed_day = trim_days[t["id"]]
                    if day < trim_day or (day == trim_day and (start is None or start < t["ts_epoch"])):
                        continue
                    if closed_day is not None and day >= closed_day:
                        continue
                acc = per[t["id"]]
                if card is None:
                    acc["unpriced_tokens"] += t["tokens"] * n
                    continue
                r, w, ur, u1, u5 = _frozen_price(card, t["tokens"], cr or 0, cc or 0, cc_1h or 0, cc_5m or 0)
                if not (r or w):
                    continue
                usd = ur + u1 + u5
                acc["read_tokens"] += r * n
                acc["write_tokens"] += w * n
                acc["usd"] += usd * n
                acc["read_usd"] += ur * n
                acc["write_1h_usd"] += u1 * n
                acc["write_5m_usd"] += u5 * n
                acc["requests"] += n
                d = days.setdefault(day, {"tokens": 0, "usd": 0.0})
                d["tokens"] += (r + w) * n
                d["usd"] += usd * n
    for acc in per.values():
        for k in ("usd", "read_usd", "write_1h_usd", "write_5m_usd"):
            acc[k] = round(acc[k], 4)
    for d in days.values():
        d["usd"] = round(d["usd"], 4)
    return {"trims": per, "days": dict(sorted(days.items())), "requests": len(rows),
            "unmatched_requests": unmatched,
            "unpriced_tokens": sum(a["unpriced_tokens"] for a in per.values())}


def _noon(days_ago):
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)


def _iso(dt):
    return dt.astimezone().isoformat()


def _fixture(m, seed):
    L, _, _, _, _ = _mods()
    rnd = random.Random(seed)
    conn = _db(m)
    projects = (LOCAL, LOCAL_SUB, STUB, "-elsewhere")
    models = ("claude-opus-5-5", SONNET, "unknown-model")
    n = 0
    for s in range(10):
        start = _noon(rnd.randint(0, 6)) - timedelta(minutes=rnd.randint(0, 600))
        uuid = "rand-%d-%d" % (seed, s)
        _session(conn, m, uuid, rnd.choice(projects), start)
        for _ in range(rnd.randint(2, 8)):
            n += 1
            day = (start + timedelta(days=rnd.randint(0, 2))).strftime("%Y-%m-%d")
            _req(conn, m, uuid, 10_000 * seed + n, day, cr=rnd.randint(0, 120_000), cc=rnd.randint(0, 20_000),
                 model=rnd.choice(models))
            if rnd.random() < .2:
                conn.execute("UPDATE usage_request_claims SET calls = ? WHERE request_hash = ?",
                             (rnd.choice((0, 2, 3)), 10_000 * seed + n))
    # Requests owned by no session count as unmatched.
    _req(conn, m, "nobody", 10_000 * seed + 9_999, _noon(1).strftime("%Y-%m-%d"), cr=5)
    conn.commit()
    rows = []
    specs = (("skills", "a", "global", 7, None), ("skills", "b", "global", 5, 2),
             ("mcp", "c", "project:" + LOCAL, 6, None), ("rules", "d", "tree:" + LOCAL, 4, 1),
             ("skills", "e", "global", 3, 3))
    for comp, item, scope, opened, closed in specs:
        ts = _iso(_noon(opened) - timedelta(hours=rnd.randint(0, 10)))
        trim = L.make_trim(comp, item, scope, rnd.randint(200, 6000), 0, ts, "audit_action", evidence="/b/" + item)
        rows.append(trim)
        if closed is not None:
            rows.append({"kind": "close", "trim_id": trim["id"], "ts": _iso(_noon(closed)), "reason": "restored"})
    return conn, L.load_trims_from(rows)


def _card_fn(m):
    return lambda model, prompt: None if model == "unknown-model" else _card(m, model)


@pytest.mark.parametrize("seed", [5721, 11, 42, 2026])
@pytest.mark.parametrize("projected", [False, True])
def test_value_trims_matches_frozen_reference(m, seed, projected):
    _, _, _, _, V = _mods()
    conn, trims = _fixture(m, seed)
    start, end = _noon(8).strftime("%Y-%m-%d"), _noon(0).strftime("%Y-%m-%d")
    card = _card_fn(m)
    want = _frozen_value_trims(conn, trims, start, end, card, HOME, projected=projected)
    got = V.value_trims(conn, trims, start, end, card, HOME, projected=projected)
    assert got == want
    # The fixture must exercise what the cache could get wrong.
    assert want["unmatched_requests"] >= 1
    assert sum(1 for t in want["trims"].values() if t["requests"]) >= 2
    assert len(want["days"]) >= 2


def test_fixture_covers_two_cards_closed_trims_and_unpriced(m):
    conn, trims = _fixture(m, 5721)
    assert any(t["closed_at"] for t in trims) and any(not t["closed_at"] for t in trims)
    card = _card_fn(m)
    rates = {card(model, 0)["cache_read"] for model in ("claude-opus-5-5", SONNET)}
    assert len(rates) == 2, "the two cards must price differently"
    start, end = _noon(8).strftime("%Y-%m-%d"), _noon(0).strftime("%Y-%m-%d")
    assert _frozen_value_trims(conn, trims, start, end, card, HOME)["unpriced_tokens"] > 0


def test_write_savings_fail_loudly_even_under_optimize(m, monkeypatch):
    """The cache drops write fields, so a _price that ever returns write value
    must raise (a plain assert would vanish under python -O)."""
    _, _, _, _, V = _mods()
    conn, trims = _fixture(m, 11)
    monkeypatch.setattr(V, "_price", lambda card, s, cr, cc, h1, m5: (s, 7, 0.1, 0.2, 0.0))
    start, end = _noon(8).strftime("%Y-%m-%d"), _noon(0).strftime("%Y-%m-%d")
    with pytest.raises(RuntimeError, match="cache-write"):
        V.value_trims(conn, trims, start, end, _card_fn(m), HOME, projected=True)
